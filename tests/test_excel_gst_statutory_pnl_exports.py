"""
Stage C3-B3C Verification Suite: GST Summary, GSTR-1 Working Paper, Statutory Registers & P&L Excel Exports
Covers all four ADMIN-only financial/compliance Excel reports:
1. GST Summary                  (/api/excel/exports/gst-summary)
2. GSTR-1 Accountant Working Paper (/api/excel/exports/gstr1-working-paper)
3. Statutory Registers          (/api/excel/exports/statutory-registers)
4. Profit & Loss Workbook       (/api/excel/exports/profit-and-loss)

Verifies:
- ADMIN-only RBAC enforcement (401 unauthenticated, 403 OPERATOR, 200 ADMIN) & attachment headers
- Exact parity with existing GSTService, StatutoryService, and AccountingService outputs
- Empirical HSN grouping limitation verification (`GSTService.get_hsn_summary`) & honest disclosures
- Exact GSTR-1 working paper disclosure note, B2B/B2C classification, and separate Sales Returns sheet
- Statutory Registers coverage & limitations (zero customer Aadhaar, no fabricated CIB/RC columns,
  documented omission of Purchase Returns because no workflow exists)
- Exact Profit & Loss parity, verbatim sales-returns disclosure, separate `Sales Returns Reference`
  sheet without auto-netting, negative net loss period safety, and zero return-adjusted mislabeling
- Formula-injection neutralization, zero '<f>' formulas, '@' text formatting, zero secret leakage
- Validation 422, empty ranges, closed DB connection before serialization, non-mutation, zero temp
  files, HTTP 503 during DB maintenance, frontend ADMIN-only wiring & English-only policy
"""
from __future__ import annotations

import hashlib
import io
import re
import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import openpyxl
import pytest
from fastapi.testclient import TestClient

import src.services.excel_export_service as excel_export_mod
from src.api.routes_excel import XLSX_MEDIA_TYPE
from src.app import app
from src.db.connection import get_db_manager, reset_db_manager
from src.models.master_data import Customer, Product, Supplier
from src.models.purchase import Purchase, PurchaseItem
from src.models.sales import Sale, SaleItem, SalesReturn, SalesReturnItem
from src.models.system import CompanySettings
from src.repositories.master_data_repository import MasterDataRepository
from src.services.accounting_service import AccountingService
from src.services.auth_service import AuthService
from src.services.excel_export_service import (
    GSTR1_WORKING_PAPER_NOTE,
    GST_SUMMARY_NOTE,
    HSN_LIMITATION_NOTE,
    PNL_RETURN_DISCLOSURE_NOTE,
    STATUTORY_REGISTERS_NOTE,
    ExcelExportService,
)
from src.services.excel_security import validate_xlsx_bytes
from src.services.gst_service import GSTService
from src.services.purchase_service import PurchaseService
from src.services.sales_service import SalesService
from src.services.statutory_service import StatutoryService


FORBIDDEN_SECRET_CANARIES = (
    # Company secrets
    "secret.shop.upi@okicici",
    "Secret Bank Of Maharashtra",
    "60012349876",
    "MAHB0009999",
    # Customer Aadhaar canaries
    "999988887777",
    "444455556666",
    "111122223333",
    # Supplier PAN / bank / IFSC canaries
    "ZPQRS5544M",
    "HDFC Pune Secret",
    "502000123456",
    "HDFC0001234",
    # Auth / schema secrets
    "password_hash",
    "jwt_secret",
)


def _iter_workbook_cells(wb: openpyxl.Workbook) -> Iterable[Tuple[str, Any]]:
    for sname in wb.sheetnames:
        ws = wb[sname]
        for row in ws.iter_rows():
            for cell in row:
                yield sname, cell


def _find_row_with_col1_prefix(ws, prefix: str) -> int:
    for r in range(1, ws.max_row + 1):
        val = ws.cell(row=r, column=1).value
        if isinstance(val, str) and val.startswith(prefix):
            return r
    raise AssertionError(f"Row starting with '{prefix}' not found in sheet '{ws.title}'")


def _collect_all_strings(wb: openpyxl.Workbook) -> List[str]:
    out: List[str] = []
    for _sname, cell in _iter_workbook_cells(wb):
        if isinstance(cell.value, str):
            out.append(cell.value)
    return out


def _assert_workbook_hardened(raw_bytes: bytes, filename: str = "report.xlsx") -> None:
    validate_xlsx_bytes(raw_bytes, filename=filename)

    with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
        xml_blob = "\n".join(
            zf.read(name).decode("utf-8", errors="ignore")
            for name in zf.namelist()
            if name.endswith(".xml")
        )
        sheet_xml = "\n".join(
            zf.read(name).decode("utf-8", errors="ignore")
            for name in zf.namelist()
            if name.startswith("xl/worksheets/") and name.endswith(".xml")
        )

    assert not re.search(r"<f(?:\s|>)", sheet_xml), "Unexpected formula <f> element in worksheet XML!"
    for token in FORBIDDEN_SECRET_CANARIES:
        assert token not in xml_blob, f"Sensitive canary '{token}' leaked into workbook XML!"

    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=False)
    try:
        for sname, cell in _iter_workbook_cells(wb):
            val = cell.value
            if isinstance(val, str):
                assert cell.data_type != "f", (
                    f"Unexpected formula in {sname}!{cell.coordinate}: {val}"
                )
                assert not val.startswith("="), (
                    f"Unescaped '=' string in {sname}!{cell.coordinate}: {val}"
                )
                assert cell.number_format == "@", (
                    f"Text cell {sname}!{cell.coordinate} missing '@' format: {cell.number_format}"
                )
    finally:
        wb.close()


def _snapshot_counts_and_digest(db_file: Path) -> Tuple[Dict[str, int], str]:
    conn = sqlite3.connect(str(db_file))
    try:
        counts = {
            tbl: int(conn.execute(f"SELECT COUNT(*) FROM {tbl};").fetchone()[0])
            for tbl in (
                "company_settings",
                "users",
                "customers",
                "suppliers",
                "products",
                "purchases",
                "purchase_items",
                "stock_batches",
                "sales",
                "sale_items",
                "sales_returns",
                "sales_return_items",
                "expenses",
            )
        }
    finally:
        conn.close()
    return counts, hashlib.sha256(db_file.read_bytes()).hexdigest()


def _setup_c3b3c_fixture(tmp_path: Path, monkeypatch) -> Dict[str, Any]:
    reset_db_manager()
    db_file = tmp_path / "c3b3c_reports.db"
    bkp_dir = tmp_path / "c3b3c_bkps"
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    monkeypatch.setenv("CLEAN_INSTALL", "1")
    reset_db_manager()

    db = get_db_manager()
    db.initialize_database(include_seed=True, clean_install=True)

    auth = AuthService(db)
    admin_user = auth.setup_initial_admin(
        username="shop_owner",
        plain_password="OwnerPassword#2026",
        full_name="Rajeshwar Deshmukh",
        company_settings=CompanySettings(
            company_name="Sahyadri Krishi Kendra",
            owner_name="Rajeshwar Deshmukh",
            address="Plot 18, APMC Market Yard",
            city="Baramati",
            state="Maharashtra",
            pincode="413102",
            mobile="9822112233",
            email="contact@sahyadriagro.in",
            gstin="27ABCDE1234F1Z5",
            dl_fertilizer="FERT-PUNE-101",
            dl_pesticide="PEST-PUNE-202",
            dl_seed="SEED-PUNE-303",
            upi_id="secret.shop.upi@okicici",
            bank_name="Secret Bank Of Maharashtra",
            account_no="60012349876",
            ifsc_code="MAHB0009999",
        ),
    )
    op_id = auth.register_user("counter_staff", "StaffPassword#2026", "Anil Kale", role="OPERATOR")
    admin_token = auth.create_access_token(
        admin_user.user_id or 1, "shop_owner", "Rajeshwar Deshmukh", "ADMIN"
    )
    op_token = auth.create_access_token(op_id, "counter_staff", "Anil Kale", "OPERATOR")

    repo = MasterDataRepository(db)
    pur_svc = PurchaseService(db)
    sales_svc = SalesService(db)
    acc_svc = AccountingService(db)

    # Customers: Registered Intra-State B2B, Registered Inter-State B2B, Unregistered B2C, Formula B2C
    cust_b2b_mh = repo.create_customer(
        Customer(
            customer_name="Kisan Agro Traders",
            mobile="9811002233",
            village="Malegaon",
            state="Maharashtra",
            aadhar_no="999988887777",
            gstin="27BBBBB1111B1Z5",
        )
    )
    cust_b2c_farmer = repo.create_customer(
        Customer(
            customer_name="Tukaram Shinde",
            mobile="9822001122",
            village="Nimgaon",
            state="Maharashtra",
            aadhar_no="444455556666",
            gstin=None,
        )
    )
    cust_b2b_gj = repo.create_customer(
        Customer(
            customer_name="Sardar Agri Depot",
            mobile="9833004455",
            village="Anand",
            state="Gujarat",
            aadhar_no=None,
            gstin="24CCCCC2222C1Z8",
        )
    )
    cust_b2c_formula = repo.create_customer(
        Customer(
            customer_name="=CMD|'/C calc'!A0 Farmer",
            mobile="+9844005566",
            village="@Katewadi",
            state="Maharashtra",
            aadhar_no="111122223333",
            gstin="",
        )
    )

    supp_id = repo.create_supplier(
        Supplier(
            supplier_name="Mahadhan Fertilizers Dist",
            contact_person="Suresh Kulkarni",
            mobile="9922003344",
            city="Pune",
            state="Maharashtra",
            gstin="27DDDDD3333D1Z1",
            pan="ZPQRS5544M",
            dl_number="WH-FERT-777",
            bank_name="HDFC Pune Secret",
            account_no="502000123456",
            ifsc_code="HDFC0001234",
        )
    )

    # Units in seeded DB: 1=BTL, 2=BAG, 3=PKT, 4=KG, 5=GM, 6=LTR, 7=ML, 8=QTL, 9=NOS
    # Tax groups in seeded DB: 1=Exempt 0%, 2=GST 5%, 3=GST 12%, 4=GST 18%, 5=GST 28%
    prod_fert_5 = repo.create_product(
        Product(
            product_name="Neem Coated Urea",
            category_id=1,  # FERT
            manufacturer_id=1,
            hsn_code="31021000",
            unit_id=2,  # BAG
            tax_group_id=2,  # 5%
            default_purchase_rate=240.0,
            default_sale_rate=266.5,
            default_mrp=270.0,
        )
    )
    prod_fert_18 = repo.create_product(
        Product(
            product_name="Special Micronutrient Blend",
            category_id=1,  # FERT — SAME HSN 31021000 & BAG unit as prod_fert_5, DIFFERENT 18% GST!
            manufacturer_id=1,
            hsn_code="31021000",
            unit_id=2,  # BAG
            tax_group_id=4,  # 18%
            default_purchase_rate=400.0,
            default_sale_rate=590.0,
            default_mrp=600.0,
        )
    )
    prod_pest_18 = repo.create_product(
        Product(
            product_name="Confidor Insecticide",
            category_id=3,  # PEST
            manufacturer_id=1,
            hsn_code="38089199",
            unit_id=1,  # BTL
            tax_group_id=4,  # 18%
            default_purchase_rate=300.0,
            default_sale_rate=450.0,
            default_mrp=480.0,
        )
    )
    prod_seed_0 = repo.create_product(
        Product(
            product_name="Hybrid Cotton BG-II Seed",
            category_id=2,  # SEED
            manufacturer_id=1,
            hsn_code="12099190",
            unit_id=3,  # PKT
            tax_group_id=1,  # 0% Exempt
            default_purchase_rate=650.0,
            default_sale_rate=850.0,
            default_mrp=864.0,
        )
    )
    prod_hard_null_hsn = repo.create_product(
        Product(
            product_name="Drip Lateral Connector",
            category_id=6,  # HARD
            manufacturer_id=1,
            hsn_code=None,
            unit_id=9,  # NOS
            tax_group_id=3,  # 12%
            default_purchase_rate=15.0,
            default_sale_rate=25.0,
            default_mrp=30.0,
        )
    )

    # Purchase 1 inside Sept 2026 creating FEFO stock batches
    pur1_id = pur_svc.process_inward_purchase(
        Purchase(
            invoice_no="SUP-SEP-001",
            purchase_date="2026-09-01",
            supplier_id=supp_id,
            challan_no="CH-01",
            payment_type="CREDIT",
            total_taxable=25000.0,
            total_cgst=1250.0,
            total_sgst=1250.0,
            total_igst=0.0,
            net_amount=27500.0,
            paid_amount=5000.0,
            due_amount=22500.0,
            remarks="Monsoon Opening Stock",
            items=[
                PurchaseItem(
                    product_id=prod_fert_5,
                    batch_no="FERT-B1",
                    mfg_date="2026-06-01",
                    exp_date="2027-06-01",
                    qty=30.0,
                    unit_id=2,
                    purchase_rate=240.0,
                    sale_rate=266.5,
                    mrp=270.0,
                    taxable_amount=6857.14,
                    cgst_rate=2.5,
                    cgst_amount=171.43,
                    sgst_rate=2.5,
                    sgst_amount=171.43,
                    total_amount=7200.0,
                ),
                PurchaseItem(
                    product_id=prod_fert_18,
                    batch_no="MIC-B1",
                    mfg_date="2026-06-15",
                    exp_date="2027-09-15",
                    qty=20.0,
                    unit_id=2,
                    purchase_rate=400.0,
                    sale_rate=590.0,
                    mrp=600.0,
                    taxable_amount=6779.66,
                    cgst_rate=9.0,
                    cgst_amount=610.17,
                    sgst_rate=9.0,
                    sgst_amount=610.17,
                    total_amount=8000.0,
                ),
                PurchaseItem(
                    product_id=prod_pest_18,
                    batch_no="PEST-B1",
                    mfg_date="2026-05-01",
                    exp_date="2027-05-01",
                    qty=20.0,
                    unit_id=1,
                    purchase_rate=300.0,
                    sale_rate=450.0,
                    mrp=480.0,
                    taxable_amount=5084.75,
                    cgst_rate=9.0,
                    cgst_amount=457.63,
                    sgst_rate=9.0,
                    sgst_amount=457.62,
                    total_amount=6000.0,
                ),
                PurchaseItem(
                    product_id=prod_seed_0,
                    batch_no="SEED-LOT1",
                    mfg_date="2026-07-01",
                    exp_date="2027-03-31",
                    qty=10.0,
                    unit_id=3,
                    purchase_rate=650.0,
                    sale_rate=850.0,
                    mrp=864.0,
                    taxable_amount=6500.0,
                    total_amount=6500.0,
                ),
                PurchaseItem(
                    product_id=prod_hard_null_hsn,
                    batch_no="HARD-B1",
                    mfg_date="2026-04-01",
                    exp_date="2030-04-01",
                    qty=20.0,
                    unit_id=9,
                    purchase_rate=15.0,
                    sale_rate=25.0,
                    mrp=30.0,
                    taxable_amount=267.86,
                    cgst_rate=6.0,
                    cgst_amount=16.07,
                    sgst_rate=6.0,
                    sgst_amount=16.07,
                    total_amount=300.0,
                ),
            ],
        )
    )

    # Fetch batch IDs via sqlite
    conn = db.get_connection()
    batches = {
        int(r["product_id"]): int(r["batch_id"])
        for r in conn.execute("SELECT product_id, batch_id FROM stock_batches;").fetchall()
    }
    conn.close()

    # Sale 1 (2026-09-05): B2B Intra-State with BOTH HSN 31021000 items (5% & 18%) + Pesticide (18%)
    sale1_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-SEP-001",
            sale_date="2026-09-05",
            customer_id=cust_b2b_mh,
            payment_mode="CREDIT",
            total_taxable=2500.0,
            total_cgst=160.0,
            total_sgst=160.0,
            total_igst=0.0,
            total_discount=0.0,
            extra_charges=0.0,
            round_off=0.0,
            net_amount=2820.0,
            paid_amount=0.0,
            due_amount=2820.0,
            items=[
                SaleItem(
                    product_id=prod_fert_5,
                    batch_id=batches[prod_fert_5],
                    qty=4.0,
                    unit_id=2,
                    sale_rate=262.5,
                    mrp=270.0,
                    taxable_amount=1000.0,
                    cgst_rate=2.5,
                    cgst_amount=25.0,
                    sgst_rate=2.5,
                    sgst_amount=25.0,
                    total_amount=1050.0,
                ),
                SaleItem(
                    product_id=prod_fert_18,
                    batch_id=batches[prod_fert_18],
                    qty=2.0,
                    unit_id=2,
                    sale_rate=590.0,
                    mrp=600.0,
                    taxable_amount=1000.0,
                    cgst_rate=9.0,
                    cgst_amount=90.0,
                    sgst_rate=9.0,
                    sgst_amount=90.0,
                    total_amount=1180.0,
                ),
                SaleItem(
                    product_id=prod_pest_18,
                    batch_id=batches[prod_pest_18],
                    qty=1.0,
                    unit_id=1,
                    sale_rate=590.0,
                    mrp=590.0,
                    taxable_amount=500.0,
                    cgst_rate=9.0,
                    cgst_amount=45.0,
                    sgst_rate=9.0,
                    sgst_amount=45.0,
                    total_amount=590.0,
                ),
            ],
        )
    )

    # Sale 2 (2026-09-10): B2C Unregistered Farmer (Seed 0% + Urea 5% + extra charges + round_off)
    sale2_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-SEP-002",
            sale_date="2026-09-10",
            customer_id=cust_b2c_farmer,
            payment_mode="CASH",
            total_taxable=1900.0,
            total_cgst=5.0,
            total_sgst=5.0,
            total_igst=0.0,
            total_discount=20.0,
            extra_charges=15.0,
            round_off=-0.10,
            net_amount=1924.90,
            paid_amount=1924.90,
            due_amount=0.0,
            items=[
                SaleItem(
                    product_id=prod_seed_0,
                    batch_id=batches[prod_seed_0],
                    qty=2.0,
                    unit_id=3,
                    sale_rate=850.0,
                    mrp=864.0,
                    taxable_amount=1700.0,
                    cgst_rate=0.0,
                    cgst_amount=0.0,
                    sgst_rate=0.0,
                    sgst_amount=0.0,
                    total_amount=1700.0,
                ),
                SaleItem(
                    product_id=prod_fert_5,
                    batch_id=batches[prod_fert_5],
                    qty=1.0,
                    unit_id=2,
                    sale_rate=210.0,
                    mrp=270.0,
                    discount_amount=20.0,
                    taxable_amount=200.0,
                    cgst_rate=2.5,
                    cgst_amount=5.0,
                    sgst_rate=2.5,
                    sgst_amount=5.0,
                    total_amount=210.0,
                ),
            ],
        )
    )

    # Sale 3 (2026-09-15): B2B Inter-State IGST Buyer (Gujarat)
    sale3_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-SEP-003",
            sale_date="2026-09-15",
            customer_id=cust_b2b_gj,
            payment_mode="CREDIT",
            total_taxable=1000.0,
            total_cgst=0.0,
            total_sgst=0.0,
            total_igst=180.0,
            total_discount=0.0,
            extra_charges=0.0,
            round_off=0.0,
            net_amount=1180.0,
            paid_amount=500.0,
            due_amount=680.0,
            items=[
                SaleItem(
                    product_id=prod_pest_18,
                    batch_id=batches[prod_pest_18],
                    qty=2.0,
                    unit_id=1,
                    sale_rate=590.0,
                    mrp=590.0,
                    taxable_amount=1000.0,
                    cgst_rate=0.0,
                    cgst_amount=0.0,
                    sgst_rate=0.0,
                    sgst_amount=0.0,
                    igst_rate=18.0,
                    igst_amount=180.0,
                    total_amount=1180.0,
                ),
            ],
        )
    )

    # Sale 4 (2026-09-20): B2C Formula Injection Customer + NULL unit_id on NULL HSN item
    sale4_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-SEP-004",
            sale_date="2026-09-20",
            customer_id=cust_b2c_formula,
            payment_mode="CASH",
            total_taxable=200.0,
            total_cgst=12.0,
            total_sgst=12.0,
            total_igst=0.0,
            net_amount=224.0,
            paid_amount=224.0,
            due_amount=0.0,
            items=[
                SaleItem(
                    product_id=prod_hard_null_hsn,
                    batch_id=batches[prod_hard_null_hsn],
                    qty=10.0,
                    unit_id=None,  # tests si.unit_id IS NULL while product has unit_id=9
                    sale_rate=22.4,
                    mrp=30.0,
                    taxable_amount=200.0,
                    cgst_rate=6.0,
                    cgst_amount=12.0,
                    sgst_rate=6.0,
                    sgst_amount=12.0,
                    total_amount=224.0,
                ),
            ],
        )
    )

    # Sale 5 (2026-10-05): OUTSIDE Sept 2026 window
    sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-OCT-005",
            sale_date="2026-10-05",
            customer_id=cust_b2b_mh,
            payment_mode="CASH",
            total_taxable=500.0,
            total_cgst=45.0,
            total_sgst=45.0,
            net_amount=590.0,
            paid_amount=590.0,
            due_amount=0.0,
            items=[
                SaleItem(
                    product_id=prod_pest_18,
                    batch_id=batches[prod_pest_18],
                    qty=1.0,
                    unit_id=1,
                    sale_rate=590.0,
                    mrp=590.0,
                    taxable_amount=500.0,
                    cgst_rate=9.0,
                    cgst_amount=45.0,
                    sgst_rate=9.0,
                    sgst_amount=45.0,
                    total_amount=590.0,
                ),
            ],
        )
    )

    # Recorded Sales Return inside Sept 2026 (against B2B Sale 1)
    ret_sep_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-SEP-001",
            return_date="2026-09-22",
            sale_id=sale1_id,
            customer_id=cust_b2b_mh,
            total_taxable=250.0,
            total_tax=12.50,
            round_off=0.0,
            net_amount=262.50,
            remarks="=SUM(1,2) Damaged bag returned",
            items=[
                SalesReturnItem(
                    product_id=prod_fert_5,
                    batch_id=batches[prod_fert_5],
                    qty=1.0,
                    sale_rate=262.5,
                    taxable_amount=250.0,
                    tax_amount=12.50,
                    total_amount=262.50,
                    reason="Damaged bag",
                )
            ],
        )
    )

    # Recorded Sales Return outside Sept 2026
    sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-OCT-002",
            return_date="2026-10-06",
            sale_id=sale2_id,
            customer_id=cust_b2c_farmer,
            total_taxable=850.0,
            total_tax=0.0,
            round_off=0.0,
            net_amount=850.0,
            remarks="Oct return",
            items=[
                SalesReturnItem(
                    product_id=prod_seed_0,
                    batch_id=batches[prod_seed_0],
                    qty=1.0,
                    sale_rate=850.0,
                    taxable_amount=850.0,
                    tax_amount=0.0,
                    total_amount=850.0,
                    reason="Oct return",
                )
            ],
        )
    )

    # Operational Expenses inside Sept 2026, Oct 2026 (out-of-range), and Nov 2026 (pure loss period!)
    acc_svc.record_daily_expense(
        category_id=1,
        amount=600.0,
        expense_date="2026-09-18",
        payment_mode="CASH",
        reference_no="EXP-SEP-1",
        remarks="Sept Shop Electricity",
    )
    acc_svc.record_daily_expense(
        category_id=2,
        amount=400.0,
        expense_date="2026-09-25",
        payment_mode="CASH",
        reference_no="EXP-SEP-2",
        remarks="Sept Loading Labour",
    )
    acc_svc.record_daily_expense(
        category_id=1,
        amount=300.0,
        expense_date="2026-10-08",
        payment_mode="CASH",
        reference_no="EXP-OCT-1",
        remarks="Oct Expense",
    )
    # Pure loss month Nov 2026 (0 sales, Rs 1500 expense)
    acc_svc.record_daily_expense(
        category_id=2,
        amount=1500.0,
        expense_date="2026-11-05",
        payment_mode="CASH",
        reference_no="EXP-NOV-LOSS",
        remarks="Nov Off-season Rent",
    )

    return {
        "db": db,
        "db_file": db_file,
        "admin_token": admin_token,
        "op_token": op_token,
        "pur1_id": pur1_id,
        "sale1_id": sale1_id,
        "ret_sep_id": ret_sep_id,
        "cust_b2b_mh": cust_b2b_mh,
        "cust_b2b_gj": cust_b2b_gj,
        "cust_b2c_farmer": cust_b2c_farmer,
    }


def test_c3b3c_rbac_and_http_headers(tmp_path: Path, monkeypatch):
    ctx = _setup_c3b3c_fixture(tmp_path, monkeypatch)
    endpoints = [
        "/api/excel/exports/gst-summary?from_date=2026-09-01&to_date=2026-09-30",
        "/api/excel/exports/gstr1-working-paper?from_date=2026-09-01&to_date=2026-09-30",
        "/api/excel/exports/statutory-registers?from_date=2026-09-01&to_date=2026-09-30",
        "/api/excel/exports/profit-and-loss?from_date=2026-09-01&to_date=2026-09-30",
    ]
    expected_prefixes = [
        "GST_Summary_",
        "GSTR1_Accountant_Working_Paper_",
        "Statutory_Registers_",
        "Profit_And_Loss_",
    ]
    try:
        with TestClient(app) as client:
            for url in endpoints:
                assert client.get(url).status_code == 401
                assert (
                    client.get(url, headers={"Authorization": f"Bearer {ctx['op_token']}"}).status_code
                    == 403
                )

            admin_hdr = {"Authorization": f"Bearer {ctx['admin_token']}"}
            for url, prefix in zip(endpoints, expected_prefixes):
                res = client.get(url, headers=admin_hdr)
                assert res.status_code == 200
                assert res.headers["content-type"].startswith(XLSX_MEDIA_TYPE)
                assert res.headers.get("x-content-type-options") == "nosniff"
                assert "no-store" in (res.headers.get("cache-control") or "")
                disp = res.headers.get("content-disposition") or ""
                assert f'filename="{prefix}' in disp and disp.endswith('.xlsx"')
                _assert_workbook_hardened(res.content, f"{prefix}test.xlsx")
    finally:
        reset_db_manager()


def test_c3b3c_gst_summary_and_empirical_hsn_limitation(tmp_path: Path, monkeypatch):
    ctx = _setup_c3b3c_fixture(tmp_path, monkeypatch)
    db = ctx["db"]
    try:
        # 1. Empirically confirm existing GSTService.get_hsn_summary limitation WITHOUT modifying gst_service.py
        gst_svc = GSTService(db)
        erp_hsn = gst_svc.get_hsn_summary("2026-09-01", "2026-09-30")
        erp_rates = gst_svc.get_gst_rate_wise_summary("2026-09-01", "2026-09-30")

        hsn_3102 = [r for r in erp_hsn if r["hsn_code"] == "31021000"]
        assert len(hsn_3102) == 1, "Expected ERP HSN query to collapse 5% & 18% products sharing HSN 31021000"
        assert pytest.approx(hsn_3102[0]["total_taxable"], abs=0.01) == 2200.0
        assert "applied_rate" not in hsn_3102[0] and "gst_rate" not in hsn_3102[0]

        hsn_null = [r for r in erp_hsn if r["hsn_code"] == "NA"]
        assert len(hsn_null) == 1 and hsn_null[0]["uqc"] is None

        # 2. Export GST Summary workbook
        svc = ExcelExportService(db)
        raw_bytes, filename = svc.export_gst_summary(
            from_date="2026-09-01", to_date="2026-09-30", generated_by="Rajeshwar"
        )
        assert filename == "GST_Summary_2026-09-01_to_2026-09-30.xlsx"
        _assert_workbook_hardened(raw_bytes, filename)

        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=False)
        try:
            assert wb.sheetnames == ["GST Summary", "HSN Reference (Unverified)"]
            ws = wb["GST Summary"]
            all_text = "\n".join(_collect_all_strings(wb))
            assert "Sahyadri Krishi Kendra" in all_text
            assert "27ABCDE1234F1Z5" in all_text
            assert GST_SUMMARY_NOTE in all_text

            # Reconcile outward totals in Section 1:
            # B2B = Sale 1 (2500 taxbl, 160 CGST, 160 SGST, 0 IGST, 2820 net) + Sale 3 (1000 taxbl, 180 IGST, 1180 net)
            b2b_row = _find_row_with_col1_prefix(ws, "Registered Buyers (B2B")
            assert ws.cell(row=b2b_row, column=2).value == 2
            assert pytest.approx(ws.cell(row=b2b_row, column=3).value, abs=0.01) == 3500.0
            assert pytest.approx(ws.cell(row=b2b_row, column=4).value, abs=0.01) == 160.0
            assert pytest.approx(ws.cell(row=b2b_row, column=5).value, abs=0.01) == 160.0
            assert pytest.approx(ws.cell(row=b2b_row, column=6).value, abs=0.01) == 180.0
            assert pytest.approx(ws.cell(row=b2b_row, column=7).value, abs=0.01) == 500.0
            assert pytest.approx(ws.cell(row=b2b_row, column=8).value, abs=0.01) == 4000.0

            # B2C = Sale 2 (1900 taxbl, 5 CGST, 5 SGST, net 1924.90) + Sale 4 (200 taxbl, 12 CGST, 12 SGST, net 224.0)
            b2c_row = _find_row_with_col1_prefix(ws, "Unregistered Buyers (B2C")
            assert ws.cell(row=b2c_row, column=2).value == 2
            assert pytest.approx(ws.cell(row=b2c_row, column=3).value, abs=0.01) == 2100.0
            assert pytest.approx(ws.cell(row=b2c_row, column=8).value, abs=0.01) == 2148.90

            # Total Outward Supplies = 4 invoices (Oct Sale 5 excluded!)
            tot_row = _find_row_with_col1_prefix(ws, "Total Outward Supplies")
            assert ws.cell(row=tot_row, column=2).value == 4
            assert pytest.approx(ws.cell(row=tot_row, column=3).value, abs=0.01) == 5600.0
            assert pytest.approx(ws.cell(row=tot_row, column=7).value, abs=0.01) == 534.0
            assert pytest.approx(ws.cell(row=tot_row, column=8).value, abs=0.01) == 6148.90

            # Separately Recorded Sales Returns = 1 (RET-SEP-001: 250 taxbl, 12.50 tax, 262.50 net; NOT netted!)
            ret_row = _find_row_with_col1_prefix(ws, "Separately Recorded Sales Returns")
            assert ws.cell(row=ret_row, column=2).value == 1
            assert pytest.approx(ws.cell(row=ret_row, column=3).value, abs=0.01) == 250.0
            assert pytest.approx(ws.cell(row=ret_row, column=7).value, abs=0.01) == 12.50
            assert pytest.approx(ws.cell(row=ret_row, column=8).value, abs=0.01) == 262.50

            # Reconcile Tax Rate-Wise table rows directly against GSTService.get_gst_rate_wise_summary
            rate_hdr_row = _find_row_with_col1_prefix(ws, "Tax Group")
            for idx, erp_r in enumerate(erp_rates):
                r_idx = rate_hdr_row + 1 + idx
                assert pytest.approx(ws.cell(row=r_idx, column=2).value, abs=0.01) == erp_r["applied_rate"]
                assert pytest.approx(ws.cell(row=r_idx, column=4).value, abs=0.01) == erp_r["total_taxable"]
                assert pytest.approx(ws.cell(row=r_idx, column=9).value, abs=0.01) == erp_r["total_gross"]

            # Inspect Unverified HSN Reference sheet warnings
            ws_hsn = wb["HSN Reference (Unverified)"]
            hsn_text = "\n".join(
                cell.value
                for row in ws_hsn.iter_rows()
                for cell in row
                if isinstance(cell.value, str)
            )
            assert HSN_LIMITATION_NOTE in hsn_text
            assert "Mixed GST rates collapsed" in hsn_text
            assert "2 distinct products merged" in hsn_text
            assert "Missing/uncoalesced UQC" in hsn_text
        finally:
            wb.close()
    finally:
        reset_db_manager()


def test_c3b3c_gstr1_working_paper_classifications_and_returns(tmp_path: Path, monkeypatch):
    ctx = _setup_c3b3c_fixture(tmp_path, monkeypatch)
    db = ctx["db"]
    try:
        gst_svc = GSTService(db)
        erp_b2b = gst_svc.get_gstr1_b2b("2026-09-01", "2026-09-30")
        erp_b2c = gst_svc.get_gstr1_b2c("2026-09-01", "2026-09-30")
        erp_rates = gst_svc.get_gst_rate_wise_summary("2026-09-01", "2026-09-30")

        svc = ExcelExportService(db)
        raw_bytes, filename = svc.export_gstr1_working_paper(
            from_date="2026-09-01", to_date="2026-09-30", generated_by="Rajeshwar"
        )
        assert filename == "GSTR1_Accountant_Working_Paper_2026-09-01_to_2026-09-30.xlsx"
        _assert_workbook_hardened(raw_bytes, filename)

        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=False)
        try:
            assert wb.sheetnames == [
                "Summary",
                "B2B Invoices",
                "B2C Small Summary",
                "Credit Notes - Sales Returns",
                "Tax Rate Summary",
                "HSN Reference (Unverified)",
            ]
            all_text = "\n".join(_collect_all_strings(wb))
            # Verbatim required disclosure note
            assert GSTR1_WORKING_PAPER_NOTE in all_text
            # Must NOT claim official GST filing, portal upload, certified return, or CA-approved
            lowered = all_text.lower()
            for forbidden in ("official gst filing", "portal upload", "certified return", "ca-approved"):
                assert forbidden not in lowered

            # Check Summary totals
            ws_sum = wb["Summary"]
            sum_b2b = _find_row_with_col1_prefix(ws_sum, "Table 4 — B2B Invoices")
            assert ws_sum.cell(row=sum_b2b, column=3).value == 2
            assert pytest.approx(ws_sum.cell(row=sum_b2b, column=4).value, abs=0.01) == 3500.0
            sum_b2c = _find_row_with_col1_prefix(ws_sum, "Table 7 — B2C Small Summary")
            assert pytest.approx(ws_sum.cell(row=sum_b2c, column=4).value, abs=0.01) == 2100.0
            sum_ret = _find_row_with_col1_prefix(ws_sum, "Table 9B (Ref) — Sales Returns")
            assert ws_sum.cell(row=sum_ret, column=3).value == 1
            assert pytest.approx(ws_sum.cell(row=sum_ret, column=9).value, abs=0.01) == 262.50

            # B2B Invoices exact parity
            ws_b2b = wb["B2B Invoices"]
            b2b_hdr = _find_row_with_col1_prefix(ws_b2b, "Invoice No")
            for idx, erp_row in enumerate(erp_b2b):
                r_idx = b2b_hdr + 1 + idx
                assert ws_b2b.cell(row=r_idx, column=1).value == erp_row["invoice_no"]
                assert ws_b2b.cell(row=r_idx, column=3).value == erp_row["receiver_gstin"].upper()
                assert pytest.approx(ws_b2b.cell(row=r_idx, column=14).value, abs=0.01) == erp_row["invoice_value"]
            assert ws_b2b.cell(row=b2b_hdr + 2, column=6).value == "Inter-State (IGST)"

            # B2C Small Summary exact parity
            ws_b2c = wb["B2C Small Summary"]
            b2c_hdr = _find_row_with_col1_prefix(ws_b2c, "Supply Classification")
            for idx, erp_row in enumerate(erp_b2c):
                r_idx = b2c_hdr + 1 + idx
                assert pytest.approx(ws_b2c.cell(row=r_idx, column=2).value, abs=0.01) == erp_row["gst_rate"]
                assert pytest.approx(ws_b2c.cell(row=r_idx, column=4).value, abs=0.01) == erp_row["total_taxable"]

            # Credit Notes - Sales Returns separate & unnetted; Oct return excluded; formula neutralized
            ws_cn = wb["Credit Notes - Sales Returns"]
            cn_hdr = _find_row_with_col1_prefix(ws_cn, "Return No")
            assert ws_cn.cell(row=cn_hdr + 1, column=1).value == "RET-SEP-001"
            assert pytest.approx(ws_cn.cell(row=cn_hdr + 1, column=12).value, abs=0.01) == 262.50
            assert "RET-OCT-002" not in all_text

            # Tax Rate Summary exact parity
            ws_rt = wb["Tax Rate Summary"]
            rt_hdr = _find_row_with_col1_prefix(ws_rt, "Tax Group Name")
            for idx, erp_row in enumerate(erp_rates):
                r_idx = rt_hdr + 1 + idx
                assert pytest.approx(ws_rt.cell(row=r_idx, column=2).value, abs=0.01) == erp_row["applied_rate"]
                assert pytest.approx(ws_rt.cell(row=r_idx, column=4).value, abs=0.01) == erp_row["total_taxable"]
        finally:
            wb.close()
    finally:
        reset_db_manager()


def test_c3b3c_statutory_registers_no_aadhaar_and_scope(tmp_path: Path, monkeypatch):
    ctx = _setup_c3b3c_fixture(tmp_path, monkeypatch)
    db = ctx["db"]
    try:
        stat_svc = StatutoryService(db)
        erp_fert = stat_svc.get_fertilizer_register("2026-09-01", "2026-09-30")
        erp_pest = stat_svc.get_pesticide_register("2026-09-01", "2026-09-30")
        erp_seed = stat_svc.get_seed_register("2026-09-01", "2026-09-30")

        svc = ExcelExportService(db)
        raw_bytes, filename = svc.export_statutory_registers(
            from_date="2026-09-01", to_date="2026-09-30", generated_by="Rajeshwar"
        )
        assert filename == "Statutory_Registers_2026-09-01_to_2026-09-30.xlsx"
        _assert_workbook_hardened(raw_bytes, filename)

        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=False)
        try:
            assert wb.sheetnames == [
                "Summary & Scope",
                "Fertilizer Register (FCO)",
                "Pesticide Register",
                "Seed Register",
                "Sales Register",
                "Purchase Register",
                "Stock Register",
                "Sales Returns",
            ]
            # Confirm unsupported Purchase Returns sheet is NOT invented
            assert "Purchase Returns" not in wb.sheetnames

            all_text = "\n".join(_collect_all_strings(wb))
            assert STATUTORY_REGISTERS_NOTE in all_text
            assert "does not constitute legal certification" in all_text.lower() or "not" in all_text.lower()

            # Summary & Scope explicitly discloses Purchase Returns omission & Pesticide schema gaps
            ws_scope = wb["Summary & Scope"]
            pr_row = _find_row_with_col1_prefix(ws_scope, "Purchase Returns")
            assert ws_scope.cell(row=pr_row, column=3).value == "Not Supported (Omitted)"
            fert_scope_row = _find_row_with_col1_prefix(ws_scope, "Fertilizer Register")
            assert ws_scope.cell(row=fert_scope_row, column=4).value == len(erp_fert)

            # Fertilizer Register (FCO): matches StatutoryService count/totals & NEVER includes Aadhaar!
            ws_fert = wb["Fertilizer Register (FCO)"]
            f_hdr = _find_row_with_col1_prefix(ws_fert, "Invoice No")
            f_headers = [ws_fert.cell(row=f_hdr, column=c).value for c in range(1, ws_fert.max_column + 1)]
            assert all("aadhar" not in (h or "").lower() and "aadhaar" not in (h or "").lower() for h in f_headers)
            assert len(erp_fert) == 3
            assert ws_fert.cell(row=f_hdr + 1, column=1).value == erp_fert[0]["invoice_no"]

            # Pesticide Register: real DB columns only (no fabricated Technical Name or CIB/RC columns)
            ws_pest = wb["Pesticide Register"]
            p_hdr = _find_row_with_col1_prefix(ws_pest, "Invoice No")
            p_headers = [ws_pest.cell(row=p_hdr, column=c).value for c in range(1, ws_pest.max_column + 1)]
            assert all("cib" not in (h or "").lower() and "technical name" not in (h or "").lower() for h in p_headers)
            assert len(erp_pest) == 2

            # Seed Register matches StatutoryService
            ws_seed = wb["Seed Register"]
            s_hdr = _find_row_with_col1_prefix(ws_seed, "Invoice No")
            assert len(erp_seed) == 1
            assert ws_seed.cell(row=s_hdr + 1, column=6).value == erp_seed[0]["seed_variety"]

            # Formula injection customer & return remarks sanitized on Sales Register / Sales Returns
            ws_sales = wb["Sales Register"]
            sl_hdr = _find_row_with_col1_prefix(ws_sales, "Invoice No")
            assert str(ws_sales.cell(row=sl_hdr + 4, column=3).value).startswith("'=")
        finally:
            wb.close()
    finally:
        reset_db_manager()


def test_c3b3c_profit_and_loss_parity_and_returns_disclosure(tmp_path: Path, monkeypatch):
    ctx = _setup_c3b3c_fixture(tmp_path, monkeypatch)
    db = ctx["db"]
    try:
        acc_svc = AccountingService(db)
        erp_pnl = acc_svc.get_profit_and_loss("2026-09-01", "2026-09-30")
        erp_prods = acc_svc.get_product_profitability("2026-09-01", "2026-09-30")

        svc = ExcelExportService(db)
        raw_bytes, filename = svc.export_profit_and_loss(
            from_date="2026-09-01", to_date="2026-09-30", generated_by="Rajeshwar"
        )
        assert filename == "Profit_And_Loss_2026-09-01_to_2026-09-30.xlsx"
        _assert_workbook_hardened(raw_bytes, filename)

        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=False)
        try:
            assert wb.sheetnames == [
                "Profit & Loss",
                "Category & Product Margins",
                "Sales Returns Reference",
            ]
            all_text = "\n".join(_collect_all_strings(wb))
            # Verbatim required returns disclosure
            assert PNL_RETURN_DISCLOSURE_NOTE in all_text
            # Must NOT claim Net Profit is return-adjusted or final net profit
            lowered = all_text.lower()
            assert "return-adjusted" not in lowered
            assert "final net profit" not in lowered

            ws_pnl = wb["Profit & Loss"]
            rev_row = _find_row_with_col1_prefix(ws_pnl, "A. Revenue from Operations")
            assert pytest.approx(ws_pnl.cell(row=rev_row, column=2).value, abs=0.01) == erp_pnl["sales_revenue"]
            assert ws_pnl.cell(row=rev_row, column=3).value == erp_pnl["total_invoices"]

            cogs_row = _find_row_with_col1_prefix(ws_pnl, "Less: Cost of Goods Sold")
            assert pytest.approx(ws_pnl.cell(row=cogs_row, column=2).value, abs=0.01) == erp_pnl["cogs"]

            gp_row = _find_row_with_col1_prefix(ws_pnl, "Reported Gross Profit")
            assert pytest.approx(ws_pnl.cell(row=gp_row, column=2).value, abs=0.01) == erp_pnl["gross_profit"]
            assert pytest.approx(ws_pnl.cell(row=gp_row, column=3).value, abs=0.01) == erp_pnl["gross_margin_pct"]

            exp_row = _find_row_with_col1_prefix(ws_pnl, "B. Operating / Shop Expenses")
            assert pytest.approx(ws_pnl.cell(row=exp_row, column=2).value, abs=0.01) == erp_pnl["total_expenses"]

            np_row = _find_row_with_col1_prefix(ws_pnl, "Reported Net Operating Profit")
            assert pytest.approx(ws_pnl.cell(row=np_row, column=2).value, abs=0.01) == erp_pnl["net_profit"]
            assert pytest.approx(ws_pnl.cell(row=np_row, column=3).value, abs=0.01) == erp_pnl["net_margin_pct"]

            # Closing stock memo row matches AccountingService
            stk_row = _find_row_with_col1_prefix(ws_pnl, "Current Inventory Valuation")
            assert pytest.approx(ws_pnl.cell(row=stk_row, column=2).value, abs=0.01) == erp_pnl["closing_stock_value"]

            # Separate Sales Returns Reference sheet matches Sept return ONLY (NOT netted into revenue/COGS!)
            ws_ret = wb["Sales Returns Reference"]
            r_hdr = _find_row_with_col1_prefix(ws_ret, "Return No")
            assert ws_ret.cell(row=r_hdr + 1, column=1).value == "RET-SEP-001"
            assert pytest.approx(ws_ret.cell(row=r_hdr + 1, column=10).value, abs=0.01) == 262.50
            assert "RET-OCT-002" not in all_text

            # Category & Product Margins exact parity
            ws_m = wb["Category & Product Margins"]
            p_hdr = _find_row_with_col1_prefix(ws_m, "Product ID")
            for idx, ep in enumerate(erp_prods):
                r_idx = p_hdr + 1 + idx
                assert ws_m.cell(row=r_idx, column=1).value == ep["product_id"]
                assert pytest.approx(ws_m.cell(row=r_idx, column=7).value, abs=0.01) == ep["sales_revenue"]
                assert pytest.approx(ws_m.cell(row=r_idx, column=9).value, abs=0.01) == ep["gross_profit"]
        finally:
            wb.close()

        # Negative Net Operating Loss period test (Nov 2026: 0 sales, 1500 expense -> Net Loss -1500.00)
        loss_bytes, loss_name = svc.export_profit_and_loss(
            from_date="2026-11-01", to_date="2026-11-30", generated_by="Rajeshwar"
        )
        _assert_workbook_hardened(loss_bytes, loss_name)
        wb_loss = openpyxl.load_workbook(io.BytesIO(loss_bytes), data_only=False)
        try:
            ws_l = wb_loss["Profit & Loss"]
            loss_row = _find_row_with_col1_prefix(ws_l, "Reported Net Operating Loss")
            assert pytest.approx(ws_l.cell(row=loss_row, column=2).value, abs=0.01) == -1500.0
            assert pytest.approx(ws_l.cell(row=loss_row, column=3).value, abs=0.01) == 0.0
        finally:
            wb_loss.close()
    finally:
        reset_db_manager()


def test_c3b3c_validation_empty_ranges_snapshot_and_maintenance(tmp_path: Path, monkeypatch):
    ctx = _setup_c3b3c_fixture(tmp_path, monkeypatch)
    db = ctx["db"]
    db_file = ctx["db_file"]
    repo_root = Path(__file__).resolve().parent.parent

    try:
        before_tmp = set(tmp_path.rglob("*.xlsx")) | set(tmp_path.rglob("*.tmp"))

        seen_active_conns: List[int] = []
        orig_save = excel_export_mod.save_workbook_to_bytes

        def _spy_save(wb):
            seen_active_conns.append(db.active_connection_count)
            return orig_save(wb)

        monkeypatch.setattr(excel_export_mod, "save_workbook_to_bytes", _spy_save)

        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {ctx['admin_token']}"}
            base_paths = [
                "/api/excel/exports/gst-summary",
                "/api/excel/exports/gstr1-working-paper",
                "/api/excel/exports/statutory-registers",
                "/api/excel/exports/profit-and-loss",
            ]

            # Snapshot DB counts & file SHA-256 AFTER FastAPI lifespan startup completes
            before_counts, before_digest = _snapshot_counts_and_digest(db_file)

            # Empty date range returns clean 200 workbooks
            for bp in base_paths:
                res = client.get(f"{bp}?from_date=2025-01-01&to_date=2025-01-31", headers=headers)
                assert res.status_code == 200
                _assert_workbook_hardened(res.content, "empty.xlsx")
            assert seen_active_conns == [0, 0, 0, 0]

            # Valid populated date range returns 200 without mutating DB
            for bp in base_paths:
                res = client.get(f"{bp}?from_date=2026-09-01&to_date=2026-09-30", headers=headers)
                assert res.status_code == 200
                _assert_workbook_hardened(res.content, "sept.xlsx")
            assert seen_active_conns == [0, 0, 0, 0, 0, 0, 0, 0]

            # Invalid dates / inverted range -> 422
            for bp in base_paths:
                assert client.get(f"{bp}?from_date=2026-02-30", headers=headers).status_code == 422
                assert client.get(f"{bp}?from_date=2026-10-10&to_date=2026-09-01", headers=headers).status_code == 422

            after_counts, after_digest = _snapshot_counts_and_digest(db_file)
            assert after_counts == before_counts
            assert after_digest == before_digest

            # Maintenance mode -> 503 + Retry-After
            with db.exclusive_maintenance():
                for bp in base_paths:
                    m_res = client.get(bp, headers=headers)
                    assert m_res.status_code == 503
                    assert "retry-after" in m_res.headers

        after_tmp = set(tmp_path.rglob("*.xlsx")) | set(tmp_path.rglob("*.tmp"))
        assert after_tmp == before_tmp == set()
        assert list(repo_root.glob("*.xlsx")) == []
    finally:
        reset_db_manager()


def test_c3b3c_frontend_ui_controls_and_english_only():
    repo_root = Path(__file__).resolve().parent.parent
    html_text = (repo_root / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    js_text = (repo_root / "src" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    # 1. Admin-only GST export card & buttons
    assert 'id="gst-excel-export-card"' in html_text
    assert 'id="btn-export-gst-summary-excel"' in html_text
    assert 'onclick="exportGstSummaryExcel(this)"' in html_text
    assert 'id="btn-export-gstr1-working-paper-excel"' in html_text
    assert 'onclick="exportGstr1WorkingPaperExcel(this)"' in html_text

    # 2. Admin-only Statutory export card & button
    assert 'id="statutory-excel-export-card"' in html_text
    assert 'id="btn-export-statutory-registers-excel"' in html_text
    assert 'onclick="exportStatutoryRegistersExcel(this)"' in html_text

    # 3. Admin-only P&L export button inside #tab-pnl
    assert 'id="btn-export-pnl-excel"' in html_text
    assert 'onclick="exportProfitAndLossExcel(this)"' in html_text

    # 4. JS export handlers & role hiding
    for fn_name, ep in (
        ("function exportGstSummaryExcel(", '"/api/excel/exports/gst-summary"'),
        ("function exportGstr1WorkingPaperExcel(", '"/api/excel/exports/gstr1-working-paper"'),
        ("function exportStatutoryRegistersExcel(", '"/api/excel/exports/statutory-registers"'),
        ("function exportProfitAndLossExcel(", '"/api/excel/exports/profit-and-loss"'),
    ):
        assert fn_name in js_text
        assert ep in js_text

    for dom_id in ("gst-excel-export-card", "statutory-excel-export-card", "btn-export-pnl-excel"):
        assert js_text.count(dom_id) >= 2  # hidden on logout AND toggled in updateUserInterfaceForRole

    # 5. Strict English-only policy across entire `src/`
    for path in (repo_root / "src").rglob("*"):
        if path.is_file() and path.suffix in (".py", ".html", ".js", ".css", ".sql"):
            text = path.read_text(encoding="utf-8")
            for ch in text:
                assert not ("\u0900" <= ch <= "\u097f"), (
                    f"Unexpected Devanagari character '{ch}' found in {path}"
                )
