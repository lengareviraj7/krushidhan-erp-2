"""
Stage C3-B2B Verification Suite: Sales and Purchase Excel Exports
Covers all four real `.xlsx` export endpoints and services:
1. Sales Register         (/api/excel/exports/sales-register)   [ADMIN + OPERATOR]
2. Sales Item Details     (/api/excel/exports/sales-items)      [ADMIN + OPERATOR]
3. Purchase Register      (/api/excel/exports/purchase-register) [ADMIN ONLY]
4. Purchase Item Details  (/api/excel/exports/purchase-items)   [ADMIN ONLY]

Verifies:
- Role-based access control (401 unauth, OPERATOR 200 on sales / 403 on purchase, ADMIN 200 on all 4)
- HTTP attachment headers, OOXML container validity, openpyxl loadability
- Accuracy vs SQLite `sales`, `sale_items`, `purchases`, `purchase_items` & historical rate preservation
- Separate `Sales Returns` / `Sales Return Items` sheets when recorded returns exist (no silent netting)
- Date-range, customer, supplier, product & payment-mode filtering + empty datasets + HTTP 422 errors
- Zero Excel formulas, hostile formula-injection escaping & '@' formatting
- Zero leakage of purchase costs/margins in Sales exports; zero Aadhaar/PAN/bank/IFSC/UPI/auth secrets
- Consistent DB snapshot & closed DB connections before workbook serialization
- Read-only non-mutation, zero temp .xlsx files on disk, HTTP 503 during DB maintenance
- Frontend English-only HTML controls, Admin-only purchase UI gating, & authenticated Blob JS helpers
"""
from __future__ import annotations

import hashlib
import io
import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import openpyxl
import pytest
from fastapi.testclient import TestClient

from src.api.routes_excel import XLSX_MEDIA_TYPE
from src.app import app
from src.db.connection import get_db_manager, reset_db_manager
from src.models.master_data import Customer, Product, Supplier
from src.models.purchase import Purchase, PurchaseItem
from src.models.sales import Sale, SaleItem, SalesReturn, SalesReturnItem
from src.models.system import CompanySettings
from src.repositories.inventory_repository import InventoryRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.services.accounting_service import AccountingService
from src.services.auth_service import AuthService
from src.services.excel_export_service import (
    PURCHASE_RECORDED_DUE_NOTE,
    SALES_RECORDED_DUE_NOTE,
    ExcelExportService,
)
from src.services.excel_security import validate_xlsx_bytes
from src.services.excel_workbook_builder import CURRENCY_FORMAT_INR, PERCENT_FORMAT_DEFAULT
from src.services.purchase_service import PurchaseService
from src.services.sales_service import SalesService


# Secrets that must NEVER appear in ANY C3-B2B export (Sales or Purchase)
COMMON_FORBIDDEN_CANARIES = (
    "secret.shop.upi@okicici",
    "Secret Bank Of Maharashtra",
    "60012349876",    # Shop bank account
    "MAHB0009999",    # Shop IFSC
    "999988887777",   # Customer Aadhaar
    "ABCDE9876F",     # Supplier PAN
    "502000123456",   # Supplier bank account
    "HDFC0001234",    # Supplier IFSC
    "HDFC Pune",      # Supplier bank name
    "password_hash",
    "jwt_secret",
)

# Additional cost/margin tokens that must NEVER appear in Sales exports
SALES_COST_FORBIDDEN_CANARIES = (
    "888.77",
    "444.33",
    "910.25",
    "9999.99",
    "Purchase Rate",
    "Cost Price",
    "COGS",
    "Profit Margin",
)


def _setup_sales_purchase_fixture(tmp_path: Path, monkeypatch) -> Dict[str, Any]:
    """Build an isolated DB with multi-date GST/IGST sales, purchases, partial/credit dues & returns."""
    reset_db_manager()
    db_file = tmp_path / "c3b2b_exports.db"
    bkp_dir = tmp_path / "c3b2b_bkps"
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

    master_repo = MasterDataRepository(db)
    cust1_id = master_repo.create_customer(
        Customer(
            customer_name="Dattatray Shinde",
            mobile="9811002233",
            village="Malegaon",
            state="Maharashtra",
            aadhar_no="999988887777",
            gstin="27ZZZZZ9999Z1Z5",
            opening_balance=0.0,
        )
    )
    cust2_id = master_repo.create_customer(
        Customer(
            customer_name="  =HYPERLINK(\"http://evil\",\"Click\") Ramesh Patil",
            mobile="9811004455",
            village="Athani",
            state="Karnataka",
            opening_balance=0.0,
        )
    )

    supp1_id = master_repo.create_supplier(
        Supplier(
            supplier_name="=cmd|'/C calc'!A0 Agro Distributors",
            mobile="9700112233",
            city="Pune",
            state="Maharashtra",
            gstin="27SSSSS1111S1Z5",
            pan="ABCDE9876F",
            bank_name="HDFC Pune",
            account_no="502000123456",
            ifsc_code="HDFC0001234",
            opening_balance=0.0,
        )
    )
    supp2_id = master_repo.create_supplier(
        Supplier(
            supplier_name="Zuari Chem Depot",
            mobile="9700223344",
            city="Surat",
            state="Gujarat",
            gstin="24ZZZZZ2222Z1Z5",
            opening_balance=0.0,
        )
    )

    p1_id = master_repo.create_product(
        Product(
            product_name="=SUM(1,1) Neem Coated Urea",
            category_id=1,
            manufacturer_id=3,
            hsn_code="31021000",
            unit_id=2,  # BAG
            tax_group_id=2,  # 5%
            default_purchase_rate=888.77,
            default_sale_rate=1200.0,
            default_mrp=1300.0,
            min_stock_alert=10.0,
            is_active=1,
        )
    )
    p2_id = master_repo.create_product(
        Product(
            product_name="@CropGuard Systemic Spray",
            category_id=3,
            manufacturer_id=1,
            hsn_code="38089199",
            unit_id=6,  # LTR
            tax_group_id=4,  # 18%
            default_purchase_rate=444.33,
            default_sale_rate=650.0,
            default_mrp=700.0,
            min_stock_alert=5.0,
            is_active=1,
        )
    )

    purch_svc = PurchaseService(db)
    inv_repo = InventoryRepository(db)
    sales_svc = SalesService(db)

    # Purchase 1 (2026-08-05, CASH Paid, Intra-State CGST+SGST, Free Qty & Item Discount)
    pur1_id = purch_svc.process_inward_purchase(
        Purchase(
            invoice_no="SUP-AUG-101",
            purchase_date="2026-08-05",
            supplier_id=supp1_id,
            challan_no="CH-001",
            payment_type="CASH",
            total_taxable=22218.70,
            total_cgst=844.29,
            total_sgst=844.29,
            total_igst=0.0,
            extra_charges=100.0,
            round_off=-0.28,
            net_amount=24007.00,
            paid_amount=24007.00,
            due_amount=0.0,
            remarks="+SUM(1,1) Received in good condition",
            items=[
                PurchaseItem(
                    product_id=p1_id,
                    batch_no="=B1-PUR",
                    mfg_date="2026-01-10",
                    exp_date="2027-08-01",
                    qty=20.0,
                    free_qty=2.0,
                    unit_id=2,
                    purchase_rate=888.77,
                    sale_rate=1200.0,
                    mrp=1300.0,
                    discount_percent=5.0,
                    discount_amount=935.55,
                    taxable_amount=17775.40,
                    cgst_rate=2.5,
                    cgst_amount=444.39,
                    sgst_rate=2.5,
                    sgst_amount=444.39,
                    igst_rate=0.0,
                    igst_amount=0.0,
                    total_amount=18664.18,
                ),
                PurchaseItem(
                    product_id=p2_id,
                    batch_no="B2-PUR",
                    mfg_date="2026-02-01",
                    exp_date="2027-10-15",
                    qty=10.0,
                    free_qty=0.0,
                    unit_id=6,
                    purchase_rate=444.33,
                    sale_rate=650.0,
                    mrp=700.0,
                    discount_percent=0.0,
                    discount_amount=0.0,
                    taxable_amount=4443.30,
                    cgst_rate=9.0,
                    cgst_amount=399.90,
                    sgst_rate=9.0,
                    sgst_amount=399.90,
                    igst_rate=0.0,
                    igst_amount=0.0,
                    total_amount=5243.10,
                ),
            ],
        )
    )

    # Purchase 2 (2026-09-08, BANK_TRANSFER Partial, Inter-State IGST, SAME batch '=B1-PUR' at NEW rate 910.25!)
    # Upserting '=B1-PUR' overwrites stock_batches.purchase_rate to 910.25, proving Purchase 1 historical item rate
    # (888.77) must be read exclusively from `purchase_items`, NOT `stock_batches`.
    pur2_id = purch_svc.process_inward_purchase(
        Purchase(
            invoice_no="IGST-SEP-202",
            purchase_date="2026-09-08",
            supplier_id=supp2_id,
            challan_no="CH-009",
            payment_type="BANK_TRANSFER",
            total_taxable=9102.50,
            total_cgst=0.0,
            total_sgst=0.0,
            total_igst=455.13,
            extra_charges=0.0,
            round_off=0.37,
            net_amount=9558.00,
            paid_amount=3000.00,
            due_amount=6558.00,
            remarks="Inter-state lot 2",
            items=[
                PurchaseItem(
                    product_id=p1_id,
                    batch_no="=B1-PUR",
                    mfg_date="2026-06-01",
                    exp_date="2028-06-01",
                    qty=10.0,
                    free_qty=1.0,
                    unit_id=2,
                    purchase_rate=910.25,
                    sale_rate=1250.0,
                    mrp=1350.0,
                    discount_percent=0.0,
                    discount_amount=0.0,
                    taxable_amount=9102.50,
                    cgst_rate=0.0,
                    cgst_amount=0.0,
                    sgst_rate=0.0,
                    sgst_amount=0.0,
                    igst_rate=5.0,
                    igst_amount=455.13,
                    total_amount=9557.63,
                )
            ],
        )
    )

    b1 = next(b for b in inv_repo.get_available_batches_fefo(p1_id) if b.batch_no == "=B1-PUR")
    b2 = next(b for b in inv_repo.get_available_batches_fefo(p2_id) if b.batch_no == "B2-PUR")

    # Sale 1 (2026-08-12, CASH Paid, Intra-state B2B, Discount + Extra Charges + Round Off)
    sale1_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-AUG-001",
            sale_date="2026-08-12",
            customer_id=cust1_id,
            payment_mode="CASH",
            total_taxable=5142.86,
            total_cgst=128.57,
            total_sgst=128.57,
            total_igst=0.0,
            total_discount=600.00,
            extra_charges=50.00,
            round_off=0.0,
            net_amount=5450.00,
            paid_amount=5450.00,
            due_amount=0.0,
            remarks="=CMD Cash bill",
            items=[
                SaleItem(
                    product_id=p1_id,
                    batch_id=b1.batch_id,
                    qty=5.0,
                    unit_id=2,
                    sale_rate=1200.00,
                    mrp=1300.00,
                    discount_percent=10.0,
                    discount_amount=600.00,
                    taxable_amount=5142.86,
                    cgst_rate=2.5,
                    cgst_amount=128.57,
                    sgst_rate=2.5,
                    sgst_amount=128.57,
                    igst_rate=0.0,
                    igst_amount=0.0,
                    total_amount=5400.00,
                )
            ],
        )
    )

    # Sale 2 (2026-09-14, CREDIT Unpaid, Inter-state IGST, default due_amount=0 in input -> derived 1300.00)
    sale2_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-SEP-002",
            sale_date="2026-09-14",
            customer_id=cust2_id,
            payment_mode="CREDIT",
            total_taxable=1101.69,
            total_cgst=0.0,
            total_sgst=0.0,
            total_igst=198.31,
            total_discount=0.0,
            extra_charges=0.0,
            round_off=0.0,
            net_amount=1300.00,
            paid_amount=0.0,
            due_amount=0.0,
            remarks="@Alert Inter-state credit",
            items=[
                SaleItem(
                    product_id=p2_id,
                    batch_id=b2.batch_id,
                    qty=2.0,
                    unit_id=6,
                    sale_rate=650.00,
                    mrp=700.00,
                    discount_percent=0.0,
                    discount_amount=0.0,
                    taxable_amount=1101.69,
                    cgst_rate=0.0,
                    cgst_amount=0.0,
                    sgst_rate=0.0,
                    sgst_amount=0.0,
                    igst_rate=18.0,
                    igst_amount=198.31,
                    total_amount=1300.00,
                )
            ],
        )
    )

    # Sale 3 (2026-09-20, BANK_TRANSFER Partial payment)
    sale3_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-SEP-003",
            sale_date="2026-09-20",
            customer_id=cust1_id,
            payment_mode="BANK_TRANSFER",
            total_taxable=2285.72,
            total_cgst=57.14,
            total_sgst=57.14,
            total_igst=0.0,
            total_discount=0.0,
            extra_charges=0.0,
            round_off=0.0,
            net_amount=2400.00,
            paid_amount=1000.00,
            due_amount=1400.00,
            remarks="-Part payment by NEFT",
            items=[
                SaleItem(
                    product_id=p1_id,
                    batch_id=b1.batch_id,
                    qty=2.0,
                    unit_id=2,
                    sale_rate=1200.00,
                    mrp=1300.00,
                    discount_percent=0.0,
                    discount_amount=0.0,
                    taxable_amount=2285.72,
                    cgst_rate=2.5,
                    cgst_amount=57.14,
                    sgst_rate=2.5,
                    sgst_amount=57.14,
                    igst_rate=0.0,
                    igst_amount=0.0,
                    total_amount=2400.00,
                )
            ],
        )
    )

    # Recorded Sale Return on 2026-09-16 for INV-AUG-001 (must NOT be silently netted into Sales Register)
    sales_svc.process_sale_return(
        SalesReturn(
            return_no="SR-SEP-001",
            return_date="2026-09-16",
            sale_id=sale1_id,
            customer_id=cust1_id,
            total_taxable=1028.57,
            total_tax=51.43,
            round_off=0.0,
            net_amount=1080.00,
            remarks="=BAD Damaged bag returned",
            items=[
                SalesReturnItem(
                    product_id=p1_id,
                    batch_id=b1.batch_id,
                    qty=1.0,
                    sale_rate=1200.00,
                    taxable_amount=1028.57,
                    tax_amount=51.43,
                    total_amount=1080.00,
                    reason="=Seal broken",
                )
            ],
        )
    )

    # Mutate master `products` prices and tax group AFTER transactions to prove historical line-item immutability
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE products
               SET default_purchase_rate = 9999.99,
                   default_sale_rate = 5555.55,
                   default_mrp = 6666.66,
                   tax_group_id = 4
             WHERE product_id = ?;
            """,
            (p1_id,),
        )

    return {
        "db": db,
        "db_file": db_file,
        "admin_headers": {"Authorization": f"Bearer {admin_token}"},
        "operator_headers": {"Authorization": f"Bearer {op_token}"},
        "cust1_id": cust1_id,
        "cust2_id": cust2_id,
        "supp1_id": supp1_id,
        "supp2_id": supp2_id,
        "p1_id": p1_id,
        "p2_id": p2_id,
        "pur1_id": pur1_id,
        "pur2_id": pur2_id,
        "sale1_id": sale1_id,
        "sale2_id": sale2_id,
        "sale3_id": sale3_id,
    }


def _find_table_header_row(ws, first_col_label: str) -> int:
    for r_idx in range(1, min(20, ws.max_row + 1)):
        if ws.cell(row=r_idx, column=1).value == first_col_label:
            return r_idx
    raise AssertionError(
        f"Could not find header row starting with '{first_col_label}' in sheet {ws.title}"
    )


def _assert_workbook_hardened(
    xlsx_bytes: bytes,
    filename: str,
    *,
    extra_forbidden_tokens: Iterable[str] = (),
) -> None:
    validate_xlsx_bytes(xlsx_bytes, filename=filename)
    forbidden = tuple(COMMON_FORBIDDEN_CANARIES) + tuple(extra_forbidden_tokens)

    with zipfile.ZipFile(io.BytesIO(xlsx_bytes), "r") as zf:
        for name in zf.namelist():
            if name.lower().endswith((".xml", ".rels")):
                xml_text = zf.read(name).decode("utf-8", errors="ignore")
                assert "<f>" not in xml_text and "<f " not in xml_text, (
                    f"Formula element found in '{name}' of '{filename}'"
                )
                for canary in forbidden:
                    assert canary not in xml_text, (
                        f"Forbidden canary '{canary}' leaked into '{name}' of '{filename}'"
                    )

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=False)
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    assert cell.data_type != "f", (
                        f"Cell {ws.title}!{cell.coordinate} contains Excel formula {cell.value!r}"
                    )
                    if isinstance(cell.value, str):
                        assert not cell.value.startswith("="), (
                            f"Unescaped formula string {cell.value!r} in {ws.title}!{cell.coordinate}"
                        )
    finally:
        wb.close()


def _snapshot_db_counts_and_digest(db_file: Path) -> Tuple[Dict[str, int], bytes]:
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
            ).fetchall()
        ]
        counts = {
            t: conn.execute(f'SELECT COUNT(*) FROM "{t}";').fetchone()[0]
            for t in tables
        }
        dump = (
            conn.execute("SELECT * FROM sales ORDER BY sale_id;").fetchall(),
            conn.execute("SELECT * FROM sale_items ORDER BY sale_item_id;").fetchall(),
            conn.execute("SELECT * FROM sales_returns ORDER BY return_id;").fetchall(),
            conn.execute("SELECT * FROM purchases ORDER BY purchase_id;").fetchall(),
            conn.execute("SELECT * FROM purchase_items ORDER BY item_id;").fetchall(),
        )
        digest = hashlib.sha256(repr(dump).encode("utf-8")).digest()
        return counts, digest
    finally:
        conn.close()


def test_c3b2b_rbac_and_http_attachment_headers(tmp_path, monkeypatch):
    """Unauth->401; OPERATOR->200 on Sales, 403 on Purchase; ADMIN->200 on all 4."""
    ctx = _setup_sales_purchase_fixture(tmp_path, monkeypatch)

    sales_endpoints = (
        ("/api/excel/exports/sales-register", "Sales_Register_"),
        ("/api/excel/exports/sales-items", "Sales_Item_Details_"),
    )
    purchase_endpoints = (
        ("/api/excel/exports/purchase-register", "Purchase_Register_"),
        ("/api/excel/exports/purchase-items", "Purchase_Item_Details_"),
    )

    with TestClient(app) as client:
        for url, prefix in sales_endpoints:
            assert client.get(url).status_code == 401
            for hdrs in (ctx["operator_headers"], ctx["admin_headers"]):
                res = client.get(url, headers=hdrs)
                assert res.status_code == 200, f"{url} failed: {res.text}"
                assert res.headers["content-type"].startswith(XLSX_MEDIA_TYPE)
                assert res.headers.get("x-content-type-options") == "nosniff"
                assert res.headers.get("cache-control") == "no-store"
                disp = res.headers.get("content-disposition", "")
                assert disp.startswith(f'attachment; filename="{prefix}')
                assert disp.endswith('.xlsx"')
                _assert_workbook_hardened(
                    res.content,
                    f"{prefix}test.xlsx",
                    extra_forbidden_tokens=SALES_COST_FORBIDDEN_CANARIES,
                )

        for url, prefix in purchase_endpoints:
            assert client.get(url).status_code == 401
            # OPERATOR MUST BE BLOCKED with 403 Forbidden
            op_res = client.get(url, headers=ctx["operator_headers"])
            assert op_res.status_code == 403, (
                f"Expected 403 Forbidden for OPERATOR on {url}, got {op_res.status_code}"
            )
            # ADMIN Permitted
            adm_res = client.get(url, headers=ctx["admin_headers"])
            assert adm_res.status_code == 200, f"{url} failed for ADMIN: {adm_res.text}"
            assert adm_res.headers["content-type"].startswith(XLSX_MEDIA_TYPE)
            assert adm_res.headers.get("x-content-type-options") == "nosniff"
            assert adm_res.headers.get("cache-control") == "no-store"
            disp = adm_res.headers.get("content-disposition", "")
            assert disp.startswith(f'attachment; filename="{prefix}')
            assert disp.endswith('.xlsx"')
            _assert_workbook_hardened(adm_res.content, f"{prefix}test.xlsx")

    reset_db_manager()


def test_c3b2b_sales_register_accuracy_returns_and_no_cost_or_secret_leakage(tmp_path, monkeypatch):
    """Sales Register verifies GST splits, payment statuses, formula escaping & separate Sales Returns sheet."""
    ctx = _setup_sales_purchase_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        # 1. Full export (OPERATOR permitted; returns exist in September so sheet 2 'Sales Returns' appears)
        res = client.get("/api/excel/exports/sales-register", headers=ctx["operator_headers"])
        assert res.status_code == 200
        _assert_workbook_hardened(
            res.content,
            "sales_register.xlsx",
            extra_forbidden_tokens=SALES_COST_FORBIDDEN_CANARIES,
        )

        wb = openpyxl.load_workbook(io.BytesIO(res.content), data_only=False)
        try:
            assert wb.sheetnames == ["Sales Register", "Sales Returns"]
            ws = wb["Sales Register"]
            assert ws["A1"].value == "Sahyadri Krishi Kendra"
            hdr = _find_table_header_row(ws, "Invoice No")
            # Clearly visible explanatory note above table headers (inside merged callout banner)
            note_cells = [
                str(ws.cell(row=r, column=1).value or "")
                for r in range(1, hdr)
            ]
            assert any(SALES_RECORDED_DUE_NOTE in txt for txt in note_cells), (
                f"Expected C3-B2B1 Sales explanatory note above headers, got {note_cells!r}"
            )
            assert any(
                ws.cell(row=r, column=1).fill.fgColor.rgb == "00FFFBEB"
                for r in range(1, hdr)
                if SALES_RECORDED_DUE_NOTE in str(ws.cell(row=r, column=1).value or "")
            )

            headers = [ws.cell(row=hdr, column=c).value for c in range(1, 20)]
            assert "Outstanding Amount (₹)" not in headers
            assert headers == [
                "Invoice No",
                "Invoice Date",
                "Customer ID",
                "Customer Name",
                "Customer GSTIN",
                "Place of Supply",
                "Taxable Value (₹)",
                "CGST (₹)",
                "SGST (₹)",
                "IGST (₹)",
                "Discount (₹)",
                "Extra Charges (₹)",
                "Round Off (₹)",
                "Invoice Total (₹)",
                "Amount Paid (₹)",
                "Recorded Invoice Due (₹)",
                "Payment Mode",
                "Invoice Status",
                "Remarks",
            ]
            assert ws.freeze_panes == f"A{hdr + 1}"
            assert ws.auto_filter.ref is not None

            rows_by_inv = {}
            for r in range(hdr + 1, ws.max_row):
                inv_no = ws.cell(row=r, column=1).value
                rows_by_inv[inv_no] = {headers[c - 1]: ws.cell(row=r, column=c) for c in range(1, 20)}

            assert list(rows_by_inv.keys()) == ["INV-AUG-001", "INV-SEP-002", "INV-SEP-003"]

            # Row 1: Cash Paid + Discount + Extra Charges + Formula escaped remark
            r1 = rows_by_inv["INV-AUG-001"]
            assert isinstance(r1["Invoice Date"].value, (date, datetime))
            assert r1["Invoice Date"].value.strftime("%Y-%m-%d") == "2026-08-12"
            assert r1["Customer GSTIN"].value == "27ZZZZZ9999Z1Z5"
            assert r1["Place of Supply"].value == "Maharashtra"
            assert r1["Taxable Value (₹)"].value == 5142.86
            assert r1["Taxable Value (₹)"].number_format == CURRENCY_FORMAT_INR
            assert r1["CGST (₹)"].value == 128.57
            assert r1["SGST (₹)"].value == 128.57
            assert r1["Discount (₹)"].value == 600.0
            assert r1["Extra Charges (₹)"].value == 50.0
            assert r1["Invoice Total (₹)"].value == 5450.0
            assert r1["Amount Paid (₹)"].value == 5450.0
            assert r1["Recorded Invoice Due (₹)"].value == 0.0
            assert r1["Payment Mode"].value == "CASH"
            assert r1["Invoice Status"].value == "Paid"
            assert r1["Remarks"].value == "'=CMD Cash bill"

            # Row 2: Inter-state IGST Credit/Unpaid with formula injection leading spaces on customer name
            r2 = rows_by_inv["INV-SEP-002"]
            assert r2["Customer Name"].value.startswith("'  =HYPERLINK")
            assert r2["Customer Name"].number_format == "@"
            assert r2["Place of Supply"].value == "Karnataka"
            assert r2["IGST (₹)"].value == 198.31
            assert r2["Invoice Total (₹)"].value == 1300.0
            assert r2["Recorded Invoice Due (₹)"].value == 1300.0
            assert r2["Payment Mode"].value == "CREDIT"
            assert r2["Invoice Status"].value == "Credit / Unpaid"
            assert r2["Remarks"].value == "'@Alert Inter-state credit"

            # Row 3: BANK_TRANSFER Partial
            r3 = rows_by_inv["INV-SEP-003"]
            assert r3["Payment Mode"].value == "BANK TRANSFER"
            assert r3["Amount Paid (₹)"].value == 1000.0
            assert r3["Recorded Invoice Due (₹)"].value == 1400.0
            assert r3["Invoice Status"].value == "Partial"
            assert r3["Remarks"].value == "'-Part payment by NEFT"

            # Totals row (NOT netted with 1080.00 return; clear label matching renamed due column)
            tot_row = ws.max_row
            assert ws.cell(row=tot_row, column=1).value == "TOTAL SALES & RECORDED INVOICE DUE"
            assert ws.cell(row=tot_row, column=7).value == 8530.27   # 5142.86 + 1101.69 + 2285.72
            assert ws.cell(row=tot_row, column=14).value == 9150.00  # 5450 + 1300 + 2400
            assert ws.cell(row=tot_row, column=15).value == 6450.00  # 5450 + 0 + 1000
            assert ws.cell(row=tot_row, column=16).value == 2700.00  # 0 + 1300 + 1400

            # Separate Sheet 2: Sales Returns
            ws_ret = wb["Sales Returns"]
            ret_hdr = _find_table_header_row(ws_ret, "Return No")
            assert ws_ret.cell(row=ret_hdr + 1, column=1).value == "SR-SEP-001"
            assert ws_ret.cell(row=ret_hdr + 1, column=3).value == "INV-AUG-001"
            assert ws_ret.cell(row=ret_hdr + 1, column=9).value == 1080.0
            assert ws_ret.cell(row=ret_hdr + 1, column=10).value == "'=BAD Damaged bag returned"
            assert ws_ret.cell(row=ws_ret.max_row, column=1).value == "TOTAL RETURNS"
            assert ws_ret.cell(row=ws_ret.max_row, column=9).value == 1080.0
        finally:
            wb.close()

        # 2. August date filter (returns occurred in September -> only 1 sheet 'Sales Register')
        res_aug = client.get(
            "/api/excel/exports/sales-register?from_date=2026-08-01&to_date=2026-08-31",
            headers=ctx["admin_headers"],
        )
        assert res_aug.status_code == 200
        assert "Sales_Register_2026-08-01_to_2026-08-31.xlsx" in res_aug.headers["content-disposition"]
        wb_aug = openpyxl.load_workbook(io.BytesIO(res_aug.content), data_only=False)
        try:
            assert wb_aug.sheetnames == ["Sales Register"]
            ws_aug = wb_aug["Sales Register"]
            hdr_aug = _find_table_header_row(ws_aug, "Invoice No")
            invs = [ws_aug.cell(row=r, column=1).value for r in range(hdr_aug + 1, ws_aug.max_row)]
            assert invs == ["INV-AUG-001"]
        finally:
            wb_aug.close()

        # 3. Payment mode filter
        res_mode = client.get(
            "/api/excel/exports/sales-register?payment_mode=bank%20transfer",
            headers=ctx["admin_headers"],
        )
        assert res_mode.status_code == 200
        wb_mode = openpyxl.load_workbook(io.BytesIO(res_mode.content), data_only=False)
        try:
            ws_m = wb_mode["Sales Register"]
            hdr_m = _find_table_header_row(ws_m, "Invoice No")
            invs_m = [ws_m.cell(row=r, column=1).value for r in range(hdr_m + 1, ws_m.max_row)]
            assert invs_m == ["INV-SEP-003"]
        finally:
            wb_mode.close()

    reset_db_manager()


def test_c3b2b_sales_item_details_historical_rates_and_zero_cost_leakage(tmp_path, monkeypatch):
    """Sales Item Details preserves saved line-item rates/taxes after master changes and leaks zero costs."""
    ctx = _setup_sales_purchase_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        res = client.get("/api/excel/exports/sales-items", headers=ctx["operator_headers"])
        assert res.status_code == 200
        _assert_workbook_hardened(
            res.content,
            "sales_items.xlsx",
            extra_forbidden_tokens=SALES_COST_FORBIDDEN_CANARIES,
        )

        wb = openpyxl.load_workbook(io.BytesIO(res.content), data_only=False)
        try:
            assert wb.sheetnames == ["Sales Item Details", "Sales Return Items"]
            ws = wb["Sales Item Details"]
            hdr = _find_table_header_row(ws, "Invoice No")
            headers = [ws.cell(row=hdr, column=c).value for c in range(1, 22)]
            assert "Purchase Rate" not in headers and "Margin" not in headers

            rows = [
                {headers[c - 1]: ws.cell(row=r, column=c) for c in range(1, 22)}
                for r in range(hdr + 1, ws.max_row)
            ]
            assert len(rows) == 3

            # Verify historical rates preserved despite products.default_sale_rate=5555.55 & tax_group_id=4
            assert rows[0]["Invoice No"].value == "INV-AUG-001"
            assert rows[0]["Product Name"].value == "'=SUM(1,1) Neem Coated Urea"
            assert rows[0]["HSN Code"].value == "31021000"
            assert rows[0]["Batch No"].value == "'=B1-PUR"
            assert rows[0]["Qty"].value == 5
            assert rows[0]["Unit"].value == "BAG"
            assert rows[0]["Sale Rate (₹)"].value == 1200.0
            assert rows[0]["MRP (₹)"].value == 1300.0
            assert rows[0]["Discount (%)"].value == 10.0
            assert rows[0]["Discount (%)"].number_format == PERCENT_FORMAT_DEFAULT
            assert rows[0]["Discount (₹)"].value == 600.0
            assert rows[0]["GST Rate (%)"].value == 5.0
            assert rows[0]["CGST (₹)"].value == 128.57
            assert rows[0]["SGST (₹)"].value == 128.57
            assert rows[0]["Line Total (₹)"].value == 5400.0

            assert rows[1]["Invoice No"].value == "INV-SEP-002"
            assert rows[1]["Product Name"].value == "'@CropGuard Systemic Spray"
            assert rows[1]["GST Rate (%)"].value == 18.0
            assert rows[1]["IGST (₹)"].value == 198.31

            tot_row = ws.max_row
            assert ws.cell(row=tot_row, column=1).value == "TOTAL SALES ITEMS"
            assert ws.cell(row=tot_row, column=15).value == 600.00   # Discount
            assert ws.cell(row=tot_row, column=16).value == 8530.27  # Taxable
            assert ws.cell(row=tot_row, column=21).value == 9100.00  # Line Total sum (5400+1300+2400)

            ws_ri = wb["Sales Return Items"]
            ri_hdr = _find_table_header_row(ws_ri, "Return No")
            assert ws_ri.cell(row=ri_hdr + 1, column=1).value == "SR-SEP-001"
            assert ws_ri.cell(row=ri_hdr + 1, column=15).value == "'=Seal broken"
        finally:
            wb.close()

        # Filter by product_id=p2 & date range
        res_f = client.get(
            f"/api/excel/exports/sales-items?product_id={ctx['p2_id']}&from_date=2026-09-01&to_date=2026-09-30",
            headers=ctx["admin_headers"],
        )
        assert res_f.status_code == 200
        wb_f = openpyxl.load_workbook(io.BytesIO(res_f.content), data_only=False)
        try:
            assert wb_f.sheetnames == ["Sales Item Details"]
            ws_f = wb_f["Sales Item Details"]
            hdr_f = _find_table_header_row(ws_f, "Invoice No")
            assert [ws_f.cell(row=r, column=1).value for r in range(hdr_f + 1, ws_f.max_row)] == ["INV-SEP-002"]
        finally:
            wb_f.close()

    reset_db_manager()


def test_c3b2b_purchase_register_admin_accuracy_and_zero_bank_pan_leakage(tmp_path, monkeypatch):
    """ADMIN Purchase Register verifies historical bills, item discounts, statuses & zero bank/PAN leakage."""
    ctx = _setup_sales_purchase_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        res = client.get("/api/excel/exports/purchase-register", headers=ctx["admin_headers"])
        assert res.status_code == 200
        _assert_workbook_hardened(res.content, "purchase_register.xlsx")

        wb = openpyxl.load_workbook(io.BytesIO(res.content), data_only=False)
        try:
            assert wb.sheetnames == ["Purchase Register"]
            ws = wb["Purchase Register"]
            hdr = _find_table_header_row(ws, "Purchase ID")
            note_cells = [
                str(ws.cell(row=r, column=1).value or "")
                for r in range(1, hdr)
            ]
            assert any(PURCHASE_RECORDED_DUE_NOTE in txt for txt in note_cells), (
                f"Expected C3-B2B1 Purchase explanatory note above headers, got {note_cells!r}"
            )
            assert any(
                ws.cell(row=r, column=1).fill.fgColor.rgb == "00FFFBEB"
                for r in range(1, hdr)
                if PURCHASE_RECORDED_DUE_NOTE in str(ws.cell(row=r, column=1).value or "")
            )

            headers = [ws.cell(row=hdr, column=c).value for c in range(1, 21)]
            assert "Outstanding Amount (₹)" not in headers
            assert headers == [
                "Purchase ID",
                "Purchase Date",
                "Supplier Invoice No",
                "Challan No",
                "Supplier ID",
                "Supplier Name",
                "Supplier GSTIN",
                "Taxable Value (₹)",
                "CGST (₹)",
                "SGST (₹)",
                "IGST (₹)",
                "Item Discount (₹)",
                "Extra Charges (₹)",
                "Round Off (₹)",
                "Purchase Total (₹)",
                "Amount Paid (₹)",
                "Recorded Purchase Due (₹)",
                "Payment Mode",
                "Purchase Status",
                "Remarks",
            ]

            rows = [
                {headers[c - 1]: ws.cell(row=r, column=c) for c in range(1, 21)}
                for r in range(hdr + 1, ws.max_row)
            ]
            assert len(rows) == 2

            assert rows[0]["Purchase ID"].value == ctx["pur1_id"]
            assert rows[0]["Supplier Invoice No"].value == "SUP-AUG-101"
            assert rows[0]["Challan No"].value == "CH-001"
            assert rows[0]["Supplier Name"].value == "'=cmd|'/C calc'!A0 Agro Distributors"
            assert rows[0]["Supplier GSTIN"].value == "27SSSSS1111S1Z5"
            assert rows[0]["Item Discount (₹)"].value == 935.55
            assert rows[0]["Extra Charges (₹)"].value == 100.0
            assert rows[0]["Round Off (₹)"].value == -0.28
            assert rows[0]["Purchase Total (₹)"].value == 24007.0
            assert rows[0]["Purchase Status"].value == "Paid"
            assert rows[0]["Remarks"].value == "'+SUM(1,1) Received in good condition"

            assert rows[1]["Purchase ID"].value == ctx["pur2_id"]
            assert rows[1]["Supplier Invoice No"].value == "IGST-SEP-202"
            assert rows[1]["IGST (₹)"].value == 455.13
            assert rows[1]["Purchase Total (₹)"].value == 9558.0
            assert rows[1]["Amount Paid (₹)"].value == 3000.0
            assert rows[1]["Recorded Purchase Due (₹)"].value == 6558.0
            assert rows[1]["Payment Mode"].value == "BANK TRANSFER"
            assert rows[1]["Purchase Status"].value == "Partial"

            tot_row = ws.max_row
            assert ws.cell(row=tot_row, column=1).value == "TOTAL PURCHASES & RECORDED PURCHASE DUE"
            assert ws.cell(row=tot_row, column=8).value == 31321.20   # 22218.70 + 9102.50
            assert ws.cell(row=tot_row, column=12).value == 935.55
            assert ws.cell(row=tot_row, column=15).value == 33565.00  # 24007 + 9558
            assert ws.cell(row=tot_row, column=17).value == 6558.00
        finally:
            wb.close()

        # Filter by supplier_id & date range
        res_s2 = client.get(
            f"/api/excel/exports/purchase-register?supplier_id={ctx['supp2_id']}&from_date=2026-09-01&to_date=2026-09-30",
            headers=ctx["admin_headers"],
        )
        assert res_s2.status_code == 200
        assert "Purchase_Register_2026-09-01_to_2026-09-30.xlsx" in res_s2.headers["content-disposition"]
        wb_s2 = openpyxl.load_workbook(io.BytesIO(res_s2.content), data_only=False)
        try:
            ws_s2 = wb_s2["Purchase Register"]
            hdr_s2 = _find_table_header_row(ws_s2, "Purchase ID")
            assert [
                ws_s2.cell(row=r, column=3).value for r in range(hdr_s2 + 1, ws_s2.max_row)
            ] == ["IGST-SEP-202"]
        finally:
            wb_s2.close()

    reset_db_manager()


def test_c3b2b_purchase_item_details_preserves_historical_rates_against_batch_and_master_changes(
    tmp_path, monkeypatch
):
    """
    Purchase Item Details MUST read saved `purchase_items` historical rates (888.77 on Purchase 1)
    even after Purchase 2 overwrote `stock_batches.purchase_rate` to 910.25 and `products` default changed to 9999.99.
    """
    ctx = _setup_sales_purchase_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        res = client.get("/api/excel/exports/purchase-items", headers=ctx["admin_headers"])
        assert res.status_code == 200
        _assert_workbook_hardened(res.content, "purchase_items.xlsx")

        wb = openpyxl.load_workbook(io.BytesIO(res.content), data_only=False)
        try:
            assert wb.sheetnames == ["Purchase Item Details"]
            ws = wb["Purchase Item Details"]
            hdr = _find_table_header_row(ws, "Purchase ID")
            headers = [ws.cell(row=hdr, column=c).value for c in range(1, 24)]

            rows = [
                {headers[c - 1]: ws.cell(row=r, column=c) for c in range(1, 24)}
                for r in range(hdr + 1, ws.max_row)
            ]
            assert len(rows) == 3

            # Item 1 (Purchase 1, Product 1): historical 888.77 preserved despite later batch upsert & master update
            r0 = rows[0]
            assert r0["Purchase ID"].value == ctx["pur1_id"]
            assert r0["Supplier Invoice No"].value == "SUP-AUG-101"
            assert r0["Batch No"].value == "'=B1-PUR"
            assert r0["Mfg Date"].value.strftime("%Y-%m-%d") == "2026-01-10"
            assert r0["Expiry Date"].value.strftime("%Y-%m-%d") == "2027-08-01"
            assert r0["Qty"].value == 20
            assert r0["Free Qty"].value == 2
            assert r0["Unit"].value == "BAG"
            assert r0["Purchase Rate (₹)"].value == 888.77
            assert r0["Discount (%)"].value == 5.0
            assert r0["Discount (₹)"].value == 935.55
            assert r0["GST Rate (%)"].value == 5.0
            assert r0["Line Total (₹)"].value == 18664.18

            # Item 2 (Purchase 1, Product 2): historical 444.33
            assert rows[1]["Purchase Rate (₹)"].value == 444.33
            assert rows[1]["GST Rate (%)"].value == 18.0

            # Item 3 (Purchase 2, Product 1): second historical rate 910.25 + IGST 5%
            assert rows[2]["Purchase ID"].value == ctx["pur2_id"]
            assert rows[2]["Supplier Invoice No"].value == "IGST-SEP-202"
            assert rows[2]["Purchase Rate (₹)"].value == 910.25
            assert rows[2]["Free Qty"].value == 1
            assert rows[2]["IGST (₹)"].value == 455.13

            tot_row = ws.max_row
            assert ws.cell(row=tot_row, column=1).value == "TOTAL PURCHASE ITEMS"
            assert ws.cell(row=tot_row, column=17).value == 935.55
            assert ws.cell(row=tot_row, column=18).value == 31321.20
            assert ws.cell(row=tot_row, column=23).value == 33464.91  # 18664.18 + 5243.10 + 9557.63
        finally:
            wb.close()

        # Empty range produces valid header-only sheet
        res_empty = client.get(
            "/api/excel/exports/purchase-items?from_date=2025-01-01&to_date=2025-01-31",
            headers=ctx["admin_headers"],
        )
        assert res_empty.status_code == 200
        wb_empty = openpyxl.load_workbook(io.BytesIO(res_empty.content), data_only=False)
        try:
            ws_e = wb_empty["Purchase Item Details"]
            hdr_e = _find_table_header_row(ws_e, "Purchase ID")
            assert ws_e.max_row == hdr_e
        finally:
            wb_empty.close()

    reset_db_manager()


def test_c3b2b_validation_non_mutation_closed_conn_and_503(tmp_path, monkeypatch):
    """Invalid filters -> 422; exports keep DB immutable, zero disk files, close DB conn, & honor 503."""
    ctx = _setup_sales_purchase_fixture(tmp_path, monkeypatch)
    db = ctx["db"]
    db_file = ctx["db_file"]

    with TestClient(app) as client:
        for base in (
            "/api/excel/exports/sales-register",
            "/api/excel/exports/sales-items",
            "/api/excel/exports/purchase-register",
            "/api/excel/exports/purchase-items",
        ):
            assert client.get(f"{base}?from_date=2026-02-30", headers=ctx["admin_headers"]).status_code == 422
            assert client.get(
                f"{base}?from_date=2026-09-20&to_date=2026-09-01",
                headers=ctx["admin_headers"],
            ).status_code == 422
            assert client.get(f"{base}?from_date=%20%20%20", headers=ctx["admin_headers"]).status_code == 422

        assert (
            client.get(
                "/api/excel/exports/sales-register?customer_id=0",
                headers=ctx["admin_headers"],
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/excel/exports/purchase-register?supplier_id=-5",
                headers=ctx["admin_headers"],
            ).status_code
            == 422
        )

    before_counts, before_digest = _snapshot_db_counts_and_digest(db_file)
    files_before = {p.name for p in tmp_path.iterdir()}

    seen_active_conns_during_serialize: List[int] = []
    import src.services.excel_export_service as export_mod

    orig_save = export_mod.save_workbook_to_bytes

    def _spy_save(wb):
        seen_active_conns_during_serialize.append(db.active_connection_count)
        return orig_save(wb)

    monkeypatch.setattr(export_mod, "save_workbook_to_bytes", _spy_save)

    svc = ExcelExportService(db)
    for fn in (
        lambda: svc.export_sales_register(),
        lambda: svc.export_sales_item_details(),
        lambda: svc.export_purchase_register(),
        lambda: svc.export_purchase_item_details(),
    ):
        data, name = fn()
        _assert_workbook_hardened(data, name)

    assert seen_active_conns_during_serialize == [0, 0, 0, 0], (
        f"DB connection was still open while serializing Excel: {seen_active_conns_during_serialize}"
    )

    after_counts, after_digest = _snapshot_db_counts_and_digest(db_file)
    assert after_counts == before_counts
    assert after_digest == before_digest

    files_after = {p.name for p in tmp_path.iterdir()}
    assert not any(name.endswith((".xlsx", ".tmp")) for name in (files_after - files_before))

    with TestClient(app) as client:
        with db.exclusive_maintenance(timeout=2.0, reason="c3b2b_restore_test"):
            for url in (
                "/api/excel/exports/sales-register",
                "/api/excel/exports/sales-items",
                "/api/excel/exports/purchase-register",
                "/api/excel/exports/purchase-items",
            ):
                r = client.get(url, headers=ctx["admin_headers"])
                assert r.status_code == 503
                assert r.headers.get("Retry-After") == "2"

    reset_db_manager()


def test_c3b2b_frontend_ui_controls_and_admin_gating(tmp_path, monkeypatch):
    """Frontend HTML/JS exposes English-only Sales/Purchase Excel buttons with Admin-only Purchase gating."""
    _setup_sales_purchase_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        html = client.get("/").text
        js = client.get("/static/js/app.js").text

        for dom_id in (
            'id="sales-export-from-date"',
            'id="sales-export-to-date"',
            'id="btn-export-sales-register-excel"',
            'id="btn-export-sales-items-excel"',
            'id="purchase-excel-export-card"',
            'id="pur-export-from-date"',
            'id="pur-export-to-date"',
            'id="btn-export-purchase-register-excel"',
            'id="btn-export-purchase-items-excel"',
        ):
            assert dom_id in html, f"Missing expected C3-B2B HTML control {dom_id}"

        # Purchase Excel card must default hidden in static HTML before ADMIN session verification
        card_idx = html.index('id="purchase-excel-export-card"')
        card_snippet = html[card_idx : card_idx + 220]
        assert "display: none;" in card_snippet

        for fn_name in (
            "function exportSalesRegisterExcel(",
            "function exportSalesItemDetailsExcel(",
            "function exportPurchaseRegisterExcel(",
            "function exportPurchaseItemDetailsExcel(",
        ):
            assert fn_name in js, f"Missing expected JS function {fn_name}"

        for api_path in (
            "/api/excel/exports/sales-register",
            "/api/excel/exports/sales-items",
            "/api/excel/exports/purchase-register",
            "/api/excel/exports/purchase-items",
        ):
            assert api_path in js, f"Missing API path {api_path} in app.js"

        # Role gating inside JS
        assert 'currentUser.role !== "ADMIN"' in js
        assert "purchaseExcelCard.style.display" in js

    reset_db_manager()


def test_c3b2b1_recorded_due_labels_notes_and_voucher_independence(tmp_path, monkeypatch):
    """
    C3-B2B1: Verify renamed 'Recorded Invoice Due (₹)' / 'Recorded Purchase Due (₹)' columns,
    clearly visible explanatory notes (even on empty ranges), aligned totals row labels,
    and unchanged historical invoice/purchase due values when party vouchers update ledger balances.
    """
    ctx = _setup_sales_purchase_fixture(tmp_path, monkeypatch)
    db = ctx["db"]
    acc_svc = AccountingService(db)

    # Record separate party receipt & supplier payment vouchers that change party ledger balances
    # without altering historical invoice/purchase due columns on `sales` / `purchases`.
    acc_svc.record_customer_receipt(
        customer_id=ctx["cust2_id"],
        receipt_date="2026-09-22",
        amount=500.0,
        payment_mode="CASH",
        narration="Partial receipt on Khata",
    )
    acc_svc.record_supplier_payment(
        supplier_id=ctx["supp2_id"],
        payment_date="2026-09-22",
        amount=2000.0,
        payment_mode="BANK_TRANSFER",
        narration="Part payment to distributor",
    )

    cust2_row = db.fetch_one(
        "SELECT current_balance FROM customers WHERE customer_id = ?;",
        (ctx["cust2_id"],),
    )
    supp2_row = db.fetch_one(
        "SELECT current_balance FROM suppliers WHERE supplier_id = ?;",
        (ctx["supp2_id"],),
    )
    assert round(float(cust2_row["current_balance"]), 2) == 800.0   # 1300 - 500
    assert round(float(supp2_row["current_balance"]), 2) == 4558.0  # 6558 - 2000

    with TestClient(app) as client:
        # 1. Sales Register keeps historical recorded invoice due (2700.00 total, 1300.00 on INV-SEP-002)
        res_s = client.get("/api/excel/exports/sales-register", headers=ctx["operator_headers"])
        assert res_s.status_code == 200
        _assert_workbook_hardened(
            res_s.content,
            "sales_register_b2b1.xlsx",
            extra_forbidden_tokens=SALES_COST_FORBIDDEN_CANARIES,
        )
        wb_s = openpyxl.load_workbook(io.BytesIO(res_s.content), data_only=False)
        try:
            ws_s = wb_s["Sales Register"]
            hdr_s = _find_table_header_row(ws_s, "Invoice No")
            s_headers = [ws_s.cell(row=hdr_s, column=c).value for c in range(1, 20)]
            assert "Recorded Invoice Due (₹)" in s_headers
            assert "Outstanding Amount (₹)" not in s_headers

            due_col_s = s_headers.index("Recorded Invoice Due (₹)") + 1
            assert due_col_s == 16
            inv2_row = next(
                r
                for r in range(hdr_s + 1, ws_s.max_row)
                if ws_s.cell(row=r, column=1).value == "INV-SEP-002"
            )
            assert ws_s.cell(row=inv2_row, column=due_col_s).value == 1300.0
            assert ws_s.cell(row=ws_s.max_row, column=1).value == "TOTAL SALES & RECORDED INVOICE DUE"
            assert ws_s.cell(row=ws_s.max_row, column=due_col_s).value == 2700.0
            assert float(ws_s.column_dimensions["A"].width or 0) < 30.0

            s_pre_header_texts = [str(ws_s.cell(row=r, column=1).value or "") for r in range(1, hdr_s)]
            assert any(SALES_RECORDED_DUE_NOTE in txt for txt in s_pre_header_texts)
        finally:
            wb_s.close()

        # 2. Purchase Register remains ADMIN-only and shows Recorded Purchase Due + explanatory note
        assert (
            client.get("/api/excel/exports/purchase-register", headers=ctx["operator_headers"]).status_code
            == 403
        )
        res_p = client.get("/api/excel/exports/purchase-register", headers=ctx["admin_headers"])
        assert res_p.status_code == 200
        _assert_workbook_hardened(res_p.content, "purchase_register_b2b1.xlsx")
        wb_p = openpyxl.load_workbook(io.BytesIO(res_p.content), data_only=False)
        try:
            ws_p = wb_p["Purchase Register"]
            hdr_p = _find_table_header_row(ws_p, "Purchase ID")
            p_headers = [ws_p.cell(row=hdr_p, column=c).value for c in range(1, 21)]
            assert "Recorded Purchase Due (₹)" in p_headers
            assert "Outstanding Amount (₹)" not in p_headers

            due_col_p = p_headers.index("Recorded Purchase Due (₹)") + 1
            assert due_col_p == 17
            pur2_row = next(
                r
                for r in range(hdr_p + 1, ws_p.max_row)
                if ws_p.cell(row=r, column=3).value == "IGST-SEP-202"
            )
            assert ws_p.cell(row=pur2_row, column=due_col_p).value == 6558.0
            assert (
                ws_p.cell(row=ws_p.max_row, column=1).value
                == "TOTAL PURCHASES & RECORDED PURCHASE DUE"
            )
            assert ws_p.cell(row=ws_p.max_row, column=due_col_p).value == 6558.0
            assert float(ws_p.column_dimensions["A"].width or 0) < 30.0

            p_pre_header_texts = [str(ws_p.cell(row=r, column=1).value or "") for r in range(1, hdr_p)]
            assert any(PURCHASE_RECORDED_DUE_NOTE in txt for txt in p_pre_header_texts)
        finally:
            wb_p.close()

        # 3. Empty filtered date range STILL shows renamed column & explanatory note clearly
        for url, first_col, expected_col, expected_note in (
            (
                "/api/excel/exports/sales-register?from_date=2025-01-01&to_date=2025-01-31",
                "Invoice No",
                "Recorded Invoice Due (₹)",
                SALES_RECORDED_DUE_NOTE,
            ),
            (
                "/api/excel/exports/purchase-register?from_date=2025-01-01&to_date=2025-01-31",
                "Purchase ID",
                "Recorded Purchase Due (₹)",
                PURCHASE_RECORDED_DUE_NOTE,
            ),
        ):
            r_empty = client.get(url, headers=ctx["admin_headers"])
            assert r_empty.status_code == 200
            wb_e = openpyxl.load_workbook(io.BytesIO(r_empty.content), data_only=False)
            try:
                ws_e = wb_e.active
                hdr_e = _find_table_header_row(ws_e, first_col)
                hdrs_e = [ws_e.cell(row=hdr_e, column=c).value for c in range(1, ws_e.max_column + 1)]
                assert expected_col in hdrs_e
                assert any(
                    expected_note in str(ws_e.cell(row=r, column=1).value or "")
                    for r in range(1, hdr_e)
                )
            finally:
                wb_e.close()

    reset_db_manager()
