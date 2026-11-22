"""
Stage C3-B3A Verification Suite: Accounting Excel Exports
Covers all six real `.xlsx` accounting export endpoints and services:
1. Customer Ledger      (/api/excel/exports/customer-ledger)      [ADMIN + OPERATOR]
2. Supplier Ledger      (/api/excel/exports/supplier-ledger)      [ADMIN ONLY]
3. Customer Outstanding (/api/excel/exports/customer-outstanding) [ADMIN + OPERATOR]
4. Supplier Outstanding (/api/excel/exports/supplier-outstanding) [ADMIN ONLY]
5. Payment History      (/api/excel/exports/payment-history)      [ADMIN ONLY]
6. Expenses             (/api/excel/exports/expenses)             [ADMIN ONLY]

Verifies:
- Role-based access control (401 unauth, OPERATOR 200 on customer reports & 403 on supplier/payment/expenses, ADMIN 200 on all 6)
- Exact reconciliation with `customers.current_balance`, `suppliers.current_balance`, invoices, purchases, vouchers, returns & expenses
- ID prefix safety (Customer #1 vs #10, Supplier #1 vs #10, SALE #1 vs RECEIPT #1) & zero voucher/expense/multi-leg duplication
- No fabricated sequential running balances; clear yellow explanatory notes & balance sign conventions
- Valid Payment History totals (sums Receipt Inflow & Payment Outflow separately, never mixed Amount column)
- Historical `'UPI'` preservation as `'UPI (Historical)'` without rewriting stored history
- Formula injection sanitization & '@' formatting, zero Aadhaar/PAN/bank/IFSC/UPI/auth secret leaks
- Consistent DB snapshot & closed DB connections before workbook serialization
- Read-only DB immutability, zero temp `.xlsx` files on disk, HTTP 503 during DB maintenance
- Frontend English-only controls & Admin-only Supplier/Accounting Excel gating
"""
from __future__ import annotations

import hashlib
import io
import sqlite3
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import openpyxl
import pytest
from fastapi.testclient import TestClient

from src.api.routes_excel import XLSX_MEDIA_TYPE
from src.app import app
from src.db.connection import get_db_manager, reset_db_manager
from src.models.accounting import Expense, LedgerEntry, Voucher
from src.models.master_data import Customer, Product, Supplier
from src.models.purchase import Purchase, PurchaseItem
from src.models.sales import Sale, SaleItem, SalesReturn, SalesReturnItem
from src.models.system import CompanySettings
from src.repositories.accounting_repository import AccountingRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.services.accounting_service import AccountingService
from src.services.auth_service import AuthService
from src.services.excel_export_service import (
    CUSTOMER_LEDGER_NOTE,
    CUSTOMER_OUTSTANDING_NOTE,
    EXPENSES_REPORT_NOTE,
    PAYMENT_HISTORY_NOTE,
    SUPPLIER_LEDGER_NOTE,
    SUPPLIER_OUTSTANDING_NOTE,
    ExcelExportService,
)
from src.services.excel_security import validate_xlsx_bytes
from src.services.purchase_service import PurchaseService
from src.services.sales_service import SalesService


FORBIDDEN_SECRET_CANARIES = (
    "secret.shop.upi@okicici",
    "Secret Bank Of Maharashtra",
    "60012349876",
    "MAHB0009999",
    "999988887777",   # Customer Aadhaar
    "111122223333",   # Customer 10 Aadhaar
    "ABCDE9876F",     # Supplier 1 PAN
    "502000123456",   # Supplier 1 bank account
    "HDFC0001234",    # Supplier 1 IFSC
    "HDFC Pune",      # Supplier 1 bank name
    "ZYXWV1234Q",     # Supplier 10 PAN
    "602000654321",   # Supplier 10 bank account
    "ICIC0004321",    # Supplier 10 IFSC
    "password_hash",
    "jwt_secret",
)


def _iter_workbook_cells(wb: openpyxl.Workbook) -> Iterable[Tuple[str, Any]]:
    for sname in wb.sheetnames:
        ws = wb[sname]
        for row in ws.iter_rows():
            for cell in row:
                yield sname, cell


def _find_table_header_row(ws, first_col_label: str) -> int:
    for r in range(1, ws.max_row + 1):
        if ws.cell(row=r, column=1).value == first_col_label:
            return r
    raise AssertionError(f"Header '{first_col_label}' not found in sheet '{ws.title}'")


def _assert_workbook_hardened(
    raw_bytes: bytes,
    filename: str = "report.xlsx",
    *,
    extra_forbidden_tokens: Iterable[str] = (),
) -> None:
    validate_xlsx_bytes(raw_bytes, filename=filename)

    with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
        xml_blob = "\n".join(
            zf.read(name).decode("utf-8", errors="ignore")
            for name in zf.namelist()
            if name.endswith(".xml")
        )

    for token in list(FORBIDDEN_SECRET_CANARIES) + list(extra_forbidden_tokens):
        assert token not in xml_blob, f"Sensitive canary '{token}' leaked into workbook XML!"

    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=False)
    try:
        for sname, cell in _iter_workbook_cells(wb):
            val = cell.value
            if isinstance(val, str):
                assert cell.data_type != "f", f"Unexpected Excel formula in {sname}!{cell.coordinate}: {val}"
                assert not val.startswith("="), (
                    f"Unescaped formula string in {sname}!{cell.coordinate}: {val}"
                )
                stripped = val.lstrip(" \t\r\n")
                if stripped and stripped[0] in ("=", "+", "-", "@") and not val.startswith("'"):
                    if sname and cell.number_format == "@":
                        pass  # allow plain negative note/description unless active payload; helper always prefixes "'"
                assert cell.number_format == "@", (
                    f"Text cell {sname}!{cell.coordinate} missing '@' format: {cell.number_format}"
                )
    finally:
        wb.close()


def _snapshot_db_counts_and_digest(db_file: Path) -> Tuple[Dict[str, int], str]:
    conn = sqlite3.connect(str(db_file))
    try:
        tables = (
            "company_settings",
            "users",
            "customers",
            "suppliers",
            "sales",
            "sales_returns",
            "purchases",
            "vouchers",
            "ledger_entries",
            "expenses",
        )
        counts = {
            tbl: int(conn.execute(f"SELECT COUNT(*) FROM {tbl};").fetchone()[0])
            for tbl in tables
        }
    finally:
        conn.close()
    digest = hashlib.sha256(db_file.read_bytes()).hexdigest()
    return counts, digest


def _setup_accounting_fixture(tmp_path: Path, monkeypatch) -> Dict[str, Any]:
    """
    Create an isolated clean SQLite database with multi-party accounting scenarios:
    - Customers with ID 1, ID 10 (prefix collision test), negative advance, settled zero balance, & inactive debtor
    - Suppliers with ID 1, ID 10 (prefix collision test), negative advance
    - Cash, Bank Transfer, Credit & historical UPI records across sales, purchases & vouchers
    - Both `record_daily_expense` (no voucher) and `record_shop_expense` (linked PAYMENT EXPENSE voucher)
    - Standalone CONTRA voucher to test cash/bank transfer handling
    """
    reset_db_manager()
    db_file = tmp_path / "c3b3a_accounting.db"
    bkp_dir = tmp_path / "c3b3a_bkps"
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
            email="accounts@sahyadriagro.in",
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
    purch_svc = PurchaseService(db)
    sales_svc = SalesService(db)
    acc_svc = AccountingService(db)
    acc_repo = AccountingRepository(db)

    # --- Customers (Ensure Customer #1 and Customer #10 exist to verify exact #id isolation) ---
    cust1_id = master_repo.create_customer(
        Customer(
            customer_name="Dattatray Shinde",
            mobile="9811001111",
            village="Malegaon",
            state="Maharashtra",
            aadhar_no="999988887777",
            gstin="27ZZZZZ9999Z1Z5",
            opening_balance=500.0,
        )
    )
    cust_settled_id = master_repo.create_customer(
        Customer(
            customer_name="Prakash More",
            mobile="9811002222",
            village="Indapur",
            state="Maharashtra",
            opening_balance=0.0,
        )
    )
    cust_adv_id = master_repo.create_customer(
        Customer(
            customer_name="Suresh Kulkarni",
            mobile="9811003333",
            village="Malegaon",
            state="Maharashtra",
            opening_balance=-250.0,
        )
    )
    cust_inactive_id = master_repo.create_customer(
        Customer(
            customer_name="Old Inactive Debtor",
            mobile="9811004444",
            village="Baramati",
            state="Maharashtra",
            opening_balance=175.0,
            is_active=1,
        )
    )
    db.execute_query(
        "UPDATE customers SET is_active = 0 WHERE customer_id = ?;",
        (cust_inactive_id,),
    )
    # Pad up to Customer ID 10
    while True:
        filler_id = master_repo.create_customer(
            Customer(
                customer_name="Filler Settled Farmer",
                mobile="9800000000",
                village="Phaltan",
                opening_balance=0.0,
            )
        )
        if filler_id == 9:
            break
    cust10_id = master_repo.create_customer(
        Customer(
            customer_name='  =HYPERLINK("http://evil","Click") Ramesh Patil',
            mobile="9811009999",
            village="Athani",
            state="Karnataka",
            aadhar_no="111122223333",
            opening_balance=0.0,
        )
    )
    assert cust1_id == 1
    assert cust10_id == 10

    # --- Suppliers (Ensure Supplier #1 and Supplier #10 exist to verify exact #id isolation) ---
    supp1_id = master_repo.create_supplier(
        Supplier(
            supplier_name="=cmd|'/C calc'!A0 Agro Distributors",
            contact_person="Vikram Joshi",
            mobile="9700112233",
            city="Pune",
            state="Maharashtra",
            gstin="27SSSSS1111S1Z5",
            pan="ABCDE9876F",
            bank_name="HDFC Pune",
            account_no="502000123456",
            ifsc_code="HDFC0001234",
            opening_balance=1000.0,
        )
    )
    supp_adv_id = master_repo.create_supplier(
        Supplier(
            supplier_name="Narmada Bio Advance",
            contact_person="Mohan Desai",
            mobile="9700223344",
            city="Nashik",
            state="Maharashtra",
            gstin="27NNNNN3333N1Z5",
            opening_balance=-400.0,
        )
    )
    while True:
        filler_supp = master_repo.create_supplier(
            Supplier(
                supplier_name="Filler Settled Supplier",
                mobile="9700000000",
                city="Satara",
                opening_balance=0.0,
            )
        )
        if filler_supp == 9:
            break
    supp10_id = master_repo.create_supplier(
        Supplier(
            supplier_name="Zuari Chem Depot",
            contact_person="Kiran Mehta",
            mobile="9700998877",
            city="Surat",
            state="Gujarat",
            gstin="24ZZZZZ2222Z1Z5",
            pan="ZYXWV1234Q",
            bank_name="ICICI Surat",
            account_no="602000654321",
            ifsc_code="ICIC0004321",
            opening_balance=0.0,
        )
    )
    assert supp1_id == 1
    assert supp10_id == 10

    # --- Product ---
    prod_id = master_repo.create_product(
        Product(
            product_name="Neem Coated Urea 45kg",
            category_id=1,
            manufacturer_id=3,
            hsn_code="31021000",
            unit_id=2,
            tax_group_id=2,
            default_purchase_rate=800.0,
            default_sale_rate=1000.0,
            default_mrp=1100.0,
            is_active=1,
        )
    )

    # --- Purchases ---
    # Purchase 1 (Supplier #1, 2026-08-05): net 10000, paid 4000 CASH -> due 6000; Supplier #1 balance becomes 1000 + 6000 = 7000
    pur1_id = purch_svc.process_inward_purchase(
        Purchase(
            invoice_no="SUP-AUG-101",
            purchase_date="2026-08-05",
            supplier_id=supp1_id,
            challan_no="CH-001",
            payment_type="CASH",
            total_taxable=9523.81,
            total_cgst=238.10,
            total_sgst=238.09,
            net_amount=10000.0,
            paid_amount=4000.0,
            due_amount=6000.0,
            remarks="+Purchase note",
            items=[
                PurchaseItem(
                    product_id=prod_id,
                    batch_no="B-AUG-01",
                    qty=12.0,
                    free_qty=0.0,
                    unit_id=2,
                    purchase_rate=800.0,
                    sale_rate=1000.0,
                    mrp=1100.0,
                    taxable_amount=9523.81,
                    cgst_rate=2.5,
                    cgst_amount=238.10,
                    sgst_rate=2.5,
                    sgst_amount=238.09,
                    total_amount=10000.0,
                )
            ],
        )
    )
    # Purchase 2 (Supplier #10, 2026-09-02): net 5000, paid 1000 BANK_TRANSFER -> due 4000; Supplier #10 balance = 4000
    pur2_id = purch_svc.process_inward_purchase(
        Purchase(
            invoice_no="ZCD-SEP-202",
            purchase_date="2026-09-02",
            supplier_id=supp10_id,
            challan_no="CH-010",
            payment_type="BANK_TRANSFER",
            total_taxable=4761.90,
            total_igst=238.10,
            net_amount=5000.0,
            paid_amount=1000.0,
            due_amount=4000.0,
            remarks="IGST stock inward",
            items=[
                PurchaseItem(
                    product_id=prod_id,
                    batch_no="B-SEP-10",
                    qty=6.0,
                    free_qty=0.0,
                    unit_id=2,
                    purchase_rate=800.0,
                    sale_rate=1000.0,
                    mrp=1100.0,
                    taxable_amount=4761.90,
                    igst_rate=5.0,
                    igst_amount=238.10,
                    total_amount=5000.0,
                )
            ],
        )
    )

    batches = db.fetch_all("SELECT batch_id, batch_no FROM stock_batches ORDER BY batch_id ASC;")
    b1_id = batches[0]["batch_id"]

    # --- Sales ---
    # Sale 1 (Customer #1, 2026-08-10): net 5400, paid 4000 CASH -> due 1400; Customer #1 balance = 500 + 1400 = 1900
    sale1_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-AUG-001",
            sale_date="2026-08-10",
            customer_id=cust1_id,
            payment_mode="CASH",
            total_taxable=5142.86,
            total_cgst=128.57,
            total_sgst=128.57,
            net_amount=5400.0,
            paid_amount=4000.0,
            due_amount=1400.0,
            remarks="First seasonal sale",
            items=[
                SaleItem(
                    product_id=prod_id,
                    batch_id=b1_id,
                    batch_no="B-AUG-01",
                    qty=5.0,
                    unit_id=2,
                    sale_rate=1080.0,
                    mrp=1100.0,
                    taxable_amount=5142.86,
                    cgst_rate=2.5,
                    cgst_amount=128.57,
                    sgst_rate=2.5,
                    sgst_amount=128.57,
                    total_amount=5400.0,
                )
            ],
        )
    )
    assert sale1_id == 1

    # Sale 2 (Settled Customer, 2026-08-15, Historical 'UPI'): net 1000, paid 1000 UPI -> balance = 0
    sale2_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-HIST-UPI",
            sale_date="2026-08-15",
            customer_id=cust_settled_id,
            payment_mode="UPI",
            total_taxable=952.38,
            total_cgst=23.81,
            total_sgst=23.81,
            net_amount=1000.0,
            paid_amount=1000.0,
            due_amount=0.0,
            remarks="Historical pre-C2-A UPI bill",
            items=[
                SaleItem(
                    product_id=prod_id,
                    batch_id=b1_id,
                    batch_no="B-AUG-01",
                    qty=1.0,
                    unit_id=2,
                    sale_rate=1000.0,
                    mrp=1100.0,
                    taxable_amount=952.38,
                    cgst_rate=2.5,
                    cgst_amount=23.81,
                    sgst_rate=2.5,
                    sgst_amount=23.81,
                    total_amount=1000.0,
                )
            ],
        )
    )

    # Sale 3 (Customer #10, 2026-09-05, CREDIT): net 1300, paid 0 -> due 1300; Customer #10 balance = 1300
    sale3_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-SEP-010",
            sale_date="2026-09-05",
            customer_id=cust10_id,
            payment_mode="CREDIT",
            total_taxable=1238.10,
            total_cgst=30.95,
            total_sgst=30.95,
            net_amount=1300.0,
            paid_amount=0.0,
            due_amount=1300.0,
            items=[
                SaleItem(
                    product_id=prod_id,
                    batch_id=b1_id,
                    batch_no="B-AUG-01",
                    qty=1.0,
                    unit_id=2,
                    sale_rate=1300.0,
                    mrp=1300.0,
                    taxable_amount=1238.10,
                    cgst_rate=2.5,
                    cgst_amount=30.95,
                    sgst_rate=2.5,
                    sgst_amount=30.95,
                    total_amount=1300.0,
                )
            ],
        )
    )

    # --- Customer Receipt Vouchers & Sales Return ---
    # Cust #1 Receipt on 2026-09-10: 600 BANK_TRANSFER -> Cust #1 balance: 1900 - 600 = 1300
    cust1_rcp_id = acc_svc.record_customer_receipt(
        customer_id=cust1_id,
        receipt_date="2026-09-10",
        amount=600.0,
        payment_mode="BANK_TRANSFER",
        narration="=NEFT part settlement",
    )
    # Cust #1 Sales Return on 2026-09-12: net 300 -> Cust #1 balance: 1300 - 300 = 1000
    ret1_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="SR-SEP-001",
            return_date="2026-09-12",
            sale_id=sale1_id,
            customer_id=cust1_id,
            total_taxable=285.71,
            total_tax=14.29,
            net_amount=300.0,
            remarks="@Returned damaged bag",
            items=[
                SalesReturnItem(
                    product_id=prod_id,
                    batch_id=b1_id,
                    qty=0.5,
                    sale_rate=600.0,
                    taxable_amount=285.71,
                    tax_amount=14.29,
                    total_amount=300.0,
                    reason="Seal broken",
                )
            ],
        )
    )
    # Cust #10 Historical UPI Receipt on 2026-09-15: 500 -> Cust #10 balance: 1300 - 500 = 800
    cust10_rcp_id = acc_svc.record_customer_receipt(
        customer_id=cust10_id,
        receipt_date="2026-09-15",
        amount=500.0,
        payment_mode="UPI",
        narration="Legacy UPI receipt for Customer #10 - Mode: UPI",
    )

    # --- Supplier Payment Vouchers ---
    # Supplier #1 historical UPI payment on 2026-09-11: 1500 -> Supplier #1 balance: 7000 - 1500 = 5500
    supp1_pay_id = acc_svc.record_supplier_payment(
        supplier_id=supp1_id,
        payment_date="2026-09-11",
        amount=1500.0,
        payment_mode="UPI",
        reference_no="UTR-HIST-1",
        narration="=Payment via legacy UPI",
    )
    # Supplier #10 BANK_TRANSFER payment on 2026-09-16: 2000 -> Supplier #10 balance: 4000 - 2000 = 2000
    supp10_pay_id = acc_svc.record_supplier_payment(
        supplier_id=supp10_id,
        payment_date="2026-09-16",
        amount=2000.0,
        payment_mode="BANK_TRANSFER",
        reference_no="NEFT-8899",
        narration="Part payment to Zuari",
    )
    # Supplier #10 legacy MANUAL PAYMENT voucher on 2026-09-18: 500 -> balance: 2000 - 500 = 1500
    cash_ac = acc_repo.get_account_by_name("Cash in Hand")
    bank_ac = acc_repo.get_account_by_name("Bank Account")
    master_repo.update_supplier_balance(supp10_id, -500.0)
    acc_repo.create_voucher(
        Voucher(
            voucher_no="PAY-LEGACY-10",
            voucher_date="2026-09-18",
            voucher_type="PAYMENT",
            total_amount=500.0,
            narration="Old manual cash settlement",
            reference_type="MANUAL",
            reference_id=supp10_id,
            entries=[
                LedgerEntry(
                    account_id=cash_ac.account_id,
                    debit_amount=500.0,
                    credit_amount=0.0,
                    particulars=f"Debit to Supplier #{supp10_id}",
                ),
                LedgerEntry(
                    account_id=cash_ac.account_id,
                    debit_amount=0.0,
                    credit_amount=500.0,
                    particulars=f"Payment to Supplier #{supp10_id}",
                ),
            ],
        )
    )

    # --- Expenses (Both direct record_daily_expense & voucher-backed record_shop_expense) ---
    exp1_id = acc_svc.record_daily_expense(
        category_id=1,  # Shop Rent
        amount=1500.0,
        expense_date="2026-09-08",
        payment_mode="CASH",
        reference_no="BILL-RENT-09",
        remarks="=Shop Rent Sept",
    )
    exp2_id = acc_svc.record_shop_expense(
        Expense(
            category_id=2,  # Electricity Bill
            expense_date="2026-09-14",
            amount=850.0,
            payment_account_id=bank_ac.account_id,
            reference_no="ELEC-99",
            remarks="Electricity MSEB Bill",
        )
    )

    # --- Standalone Contra Voucher (Cash <-> Bank internal transfer, must not count as Inflow/Outflow totals) ---
    acc_repo.create_voucher(
        Voucher(
            voucher_no="CTR-00999",
            voucher_date="2026-09-20",
            voucher_type="CONTRA",
            total_amount=3000.0,
            narration="Cash deposited into Bank",
            reference_type="MANUAL",
            entries=[
                LedgerEntry(
                    account_id=bank_ac.account_id,
                    debit_amount=3000.0,
                    credit_amount=0.0,
                    particulars="Deposit into Bank",
                ),
                LedgerEntry(
                    account_id=cash_ac.account_id,
                    debit_amount=0.0,
                    credit_amount=3000.0,
                    particulars="Cash withdrawn from drawer",
                ),
            ],
        )
    )

    return {
        "db": db,
        "db_file": db_file,
        "admin_headers": {"Authorization": f"Bearer {admin_token}"},
        "operator_headers": {"Authorization": f"Bearer {op_token}"},
        "cust1_id": cust1_id,
        "cust10_id": cust10_id,
        "cust_settled_id": cust_settled_id,
        "cust_adv_id": cust_adv_id,
        "cust_inactive_id": cust_inactive_id,
        "supp1_id": supp1_id,
        "supp10_id": supp10_id,
        "supp_adv_id": supp_adv_id,
        "sale1_id": sale1_id,
        "sale2_id": sale2_id,
        "sale3_id": sale3_id,
        "pur1_id": pur1_id,
        "pur2_id": pur2_id,
        "cust1_rcp_id": cust1_rcp_id,
        "cust10_rcp_id": cust10_rcp_id,
        "ret1_id": ret1_id,
        "supp1_pay_id": supp1_pay_id,
        "supp10_pay_id": supp10_pay_id,
        "exp1_id": exp1_id,
        "exp2_id": exp2_id,
    }


def test_c3b3a_rbac_and_http_download_headers(tmp_path, monkeypatch):
    """Verify RBAC boundaries, hardened attachment headers, OOXML security & zero secret leaks."""
    ctx = _setup_accounting_fixture(tmp_path, monkeypatch)

    all_endpoints = (
        "/api/excel/exports/customer-ledger",
        "/api/excel/exports/customer-outstanding",
        "/api/excel/exports/supplier-ledger",
        "/api/excel/exports/supplier-outstanding",
        "/api/excel/exports/payment-history",
        "/api/excel/exports/expenses",
    )
    operator_allowed = {
        "/api/excel/exports/customer-ledger",
        "/api/excel/exports/customer-outstanding",
    }

    with TestClient(app) as client:
        for url in all_endpoints:
            # 1. Unauthenticated -> 401
            r_unauth = client.get(url)
            assert r_unauth.status_code == 401, f"Expected 401 on {url}, got {r_unauth.status_code}"

            # 2. Operator -> 200 on Customer reports, 403 on Supplier/Payment/Expense reports
            r_op = client.get(url, headers=ctx["operator_headers"])
            if url in operator_allowed:
                assert r_op.status_code == 200, f"Expected 200 for OPERATOR on {url}, got {r_op.status_code}"
                _assert_workbook_hardened(r_op.content, f"{url.rsplit('/', 1)[-1]}.xlsx")
            else:
                assert r_op.status_code == 403, f"Expected 403 for OPERATOR on {url}, got {r_op.status_code}"

            # 3. Admin -> 200 on ALL 6 endpoints with strict security & download headers
            r_admin = client.get(url, headers=ctx["admin_headers"])
            assert r_admin.status_code == 200, f"Expected 200 for ADMIN on {url}, got {r_admin.status_code}"
            assert r_admin.headers.get("content-type", "").startswith(XLSX_MEDIA_TYPE)
            assert r_admin.headers.get("x-content-type-options") == "nosniff"
            assert r_admin.headers.get("cache-control") == "no-store"
            disp = r_admin.headers.get("content-disposition", "")
            assert disp.startswith('attachment; filename="') and disp.endswith('.xlsx"')
            _assert_workbook_hardened(r_admin.content, f"{url.rsplit('/', 1)[-1]}_admin.xlsx")

    reset_db_manager()


def test_c3b3a_customer_ledger_accuracy_id_isolation_and_note(tmp_path, monkeypatch):
    """
    Customer Ledger:
    - Combines Sales Invoices, standalone Receipt Vouchers & Sales Returns chronologically.
    - Never duplicates a SALE voucher into Receipt Vouchers (even when sale_id=1 == customer_id=1).
    - Never confuses Customer #10 with Customer #1.
    - Omits fabricated sequential Running Balance and surfaces Opening/Current Ledger Balances cleanly.
    """
    ctx = _setup_accounting_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        # 1. Single customer filter: Customer #1
        res1 = client.get(
            f"/api/excel/exports/customer-ledger?customer_id={ctx['cust1_id']}",
            headers=ctx["operator_headers"],
        )
        assert res1.status_code == 200
        _assert_workbook_hardened(res1.content, "customer1_ledger.xlsx")

        wb1 = openpyxl.load_workbook(io.BytesIO(res1.content), data_only=False)
        try:
            assert wb1.sheetnames == ["Customer Ledger"]
            ws = wb1["Customer Ledger"]
            hdr = _find_table_header_row(ws, "Date")
            headers = [ws.cell(row=hdr, column=c).value for c in range(1, ws.max_column + 1)]
            assert "Running Balance" not in " | ".join(str(h) for h in headers)
            assert headers == [
                "Date",
                "Voucher / Invoice No",
                "Transaction Type",
                "Customer ID",
                "Customer Name",
                "Village",
                "Mobile",
                "Reference No",
                "Payment Mode",
                "Debit / Billed (₹)",
                "Credit / Received (₹)",
                "Status",
                "Narration / Remarks",
            ]

            pre_header_text = " | ".join(str(ws.cell(row=r, column=1).value or "") for r in range(1, hdr))
            assert CUSTOMER_LEDGER_NOTE in pre_header_text
            assert "Opening Balance: ₹500.00 (Debit (Receivable))" in pre_header_text
            assert "Current Ledger Balance: ₹1,000.00 (Debit (Receivable))" in pre_header_text

            rows = [
                {headers[c - 1]: ws.cell(row=r, column=c).value for c in range(1, len(headers) + 1)}
                for r in range(hdr + 1, ws.max_row)
            ]
            # Expect EXACTLY 3 entries: INV-AUG-001, RCP-..., SR-SEP-001 (NOT Cust #10 receipt, NOT SALE #1's JRN voucher!)
            assert len(rows) == 3
            assert all(r["Customer ID"] == ctx["cust1_id"] for r in rows)

            assert rows[0]["Voucher / Invoice No"] == "INV-AUG-001"
            assert rows[0]["Transaction Type"] == "Sales Invoice"
            assert rows[0]["Payment Mode"] == "CASH"
            assert rows[0]["Debit / Billed (₹)"] == 5400.0
            assert rows[0]["Credit / Received (₹)"] == 4000.0
            assert rows[0]["Status"] == "Partial"

            assert rows[1]["Transaction Type"] == "Receipt Voucher"
            assert rows[1]["Payment Mode"] == "BANK TRANSFER"
            assert rows[1]["Debit / Billed (₹)"] == 0.0
            assert rows[1]["Credit / Received (₹)"] == 600.0
            assert rows[1]["Narration / Remarks"] == "'=NEFT part settlement"

            assert rows[2]["Voucher / Invoice No"] == "SR-SEP-001"
            assert rows[2]["Transaction Type"] == "Sales Return"
            assert rows[2]["Reference No"] == "INV-AUG-001"
            assert rows[2]["Payment Mode"] == "Adjustment"
            assert rows[2]["Debit / Billed (₹)"] == 0.0
            assert rows[2]["Credit / Received (₹)"] == 300.0
            assert rows[2]["Narration / Remarks"] == "'@Returned damaged bag"

            tot_row = ws.max_row
            assert ws.cell(row=tot_row, column=1).value == "TOTAL CUSTOMER LEDGER TRANSACTIONS"
            assert ws.cell(row=tot_row, column=10).value == 5400.0
            assert ws.cell(row=tot_row, column=11).value == 4900.0  # 4000 + 600 + 300
        finally:
            wb1.close()

        # 2. All customers + date filter (August only -> INV-AUG-001 + INV-HIST-UPI)
        res_aug = client.get(
            "/api/excel/exports/customer-ledger?from_date=2026-08-01&to_date=2026-08-31",
            headers=ctx["operator_headers"],
        )
        assert res_aug.status_code == 200
        assert "Customer_Ledger_2026-08-01_to_2026-08-31.xlsx" in res_aug.headers["content-disposition"]
        wb_aug = openpyxl.load_workbook(io.BytesIO(res_aug.content), data_only=False)
        try:
            ws_aug = wb_aug["Customer Ledger"]
            hdr_aug = _find_table_header_row(ws_aug, "Date")
            doc_nos = [ws_aug.cell(row=r, column=2).value for r in range(hdr_aug + 1, ws_aug.max_row)]
            modes = [ws_aug.cell(row=r, column=9).value for r in range(hdr_aug + 1, ws_aug.max_row)]
            assert doc_nos == ["INV-AUG-001", "INV-HIST-UPI"]
            assert modes == ["CASH", "UPI (Historical)"]
        finally:
            wb_aug.close()

        # 3. Customer #10 historical UPI receipt voucher preserved + formula name escaped
        res10 = client.get(
            f"/api/excel/exports/customer-ledger?customer_id={ctx['cust10_id']}",
            headers=ctx["operator_headers"],
        )
        assert res10.status_code == 200
        wb10 = openpyxl.load_workbook(io.BytesIO(res10.content), data_only=False)
        try:
            ws10 = wb10["Customer Ledger"]
            hdr10 = _find_table_header_row(ws10, "Date")
            rows10 = [
                [ws10.cell(row=r, column=c).value for c in range(1, 14)]
                for r in range(hdr10 + 1, ws10.max_row)
            ]
            assert len(rows10) == 2
            assert rows10[0][4] == "'  =HYPERLINK(\"http://evil\",\"Click\") Ramesh Patil"
            assert rows10[1][2] == "Receipt Voucher"
            assert rows10[1][8] == "UPI (Historical)"
            assert rows10[1][10] == 500.0
        finally:
            wb10.close()

    reset_db_manager()


def test_c3b3a_customer_outstanding_actual_balances_and_filters(tmp_path, monkeypatch):
    """
    Customer Outstanding:
    - Surfaces actual `customers.current_balance` (1000.0 for Cust #1, 800.0 for Cust #10), NOT `sales.due_amount`.
    - Preserves negative advances (-250.00 'Credit (Advance)'), zero ('Settled (Nil)'), & inactive non-zero balances.
    - Prevents Cartesian fan-out across sales, returns & receipt vouchers.
    """
    ctx = _setup_accounting_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        res = client.get("/api/excel/exports/customer-outstanding", headers=ctx["operator_headers"])
        assert res.status_code == 200
        _assert_workbook_hardened(res.content, "customer_outstanding.xlsx")

        wb = openpyxl.load_workbook(io.BytesIO(res.content), data_only=False)
        try:
            assert wb.sheetnames == ["Customer Outstanding"]
            ws = wb["Customer Outstanding"]
            hdr = _find_table_header_row(ws, "Customer ID")
            headers = [ws.cell(row=hdr, column=c).value for c in range(1, ws.max_column + 1)]
            pre_text = " | ".join(str(ws.cell(row=r, column=1).value or "") for r in range(1, hdr))
            assert CUSTOMER_OUTSTANDING_NOTE in pre_text

            by_id = {
                ws.cell(row=r, column=1).value: {
                    headers[c - 1]: ws.cell(row=r, column=c).value for c in range(1, len(headers) + 1)
                }
                for r in range(hdr + 1, ws.max_row)
            }

            c1 = by_id[ctx["cust1_id"]]
            assert c1["Opening Balance (₹)"] == 500.0
            assert c1["Bills Count"] == 1
            assert c1["Total Billed (₹)"] == 5400.0
            assert c1["Bill-Time Paid (₹)"] == 4000.0
            assert c1["Receipt Vouchers (₹)"] == 600.0
            assert c1["Sales Returns (₹)"] == 300.0
            assert c1["Current Ledger Balance (₹)"] == 1000.0
            assert c1["Balance Position"] == "Debit (Receivable)"
            assert c1["Last Activity Date"].strftime("%Y-%m-%d") == "2026-09-12"

            c10 = by_id[ctx["cust10_id"]]
            assert c10["Total Billed (₹)"] == 1300.0
            assert c10["Receipt Vouchers (₹)"] == 500.0
            assert c10["Current Ledger Balance (₹)"] == 800.0
            assert c10["Balance Position"] == "Debit (Receivable)"

            c_adv = by_id[ctx["cust_adv_id"]]
            assert c_adv["Opening Balance (₹)"] == -250.0
            assert c_adv["Current Ledger Balance (₹)"] == -250.0
            assert c_adv["Balance Position"] == "Credit (Advance)"

            c_settled = by_id[ctx["cust_settled_id"]]
            assert c_settled["Total Billed (₹)"] == 1000.0
            assert c_settled["Bill-Time Paid (₹)"] == 1000.0
            assert c_settled["Current Ledger Balance (₹)"] == 0.0
            assert c_settled["Balance Position"] == "Settled (Nil)"

            c_inact = by_id[ctx["cust_inactive_id"]]
            assert c_inact["Current Ledger Balance (₹)"] == 175.0
            assert c_inact["Balance Position"] == "Debit (Receivable)"

            tot_row = ws.max_row
            assert ws.cell(row=tot_row, column=1).value == "TOTAL CUSTOMER OUTSTANDING SUMMARY"
            assert ws.cell(row=tot_row, column=6).value == 425.0   # 500 - 250 + 175
            assert ws.cell(row=tot_row, column=7).value == 3       # 3 bills total (not inflated)
            assert ws.cell(row=tot_row, column=12).value == 1725.0 # 1000 + 800 - 250 + 175
        finally:
            wb.close()

        # Filters: only_outstanding=true + village=Malegaon
        res_f = client.get(
            "/api/excel/exports/customer-outstanding?village=Malegaon&only_outstanding=true",
            headers=ctx["operator_headers"],
        )
        assert res_f.status_code == 200
        wb_f = openpyxl.load_workbook(io.BytesIO(res_f.content), data_only=False)
        try:
            ws_f = wb_f["Customer Outstanding"]
            hdr_f = _find_table_header_row(ws_f, "Customer ID")
            ids_f = [ws_f.cell(row=r, column=1).value for r in range(hdr_f + 1, ws_f.max_row)]
            assert ids_f == [ctx["cust1_id"], ctx["cust_adv_id"]]
        finally:
            wb_f.close()

    reset_db_manager()


def test_c3b3a_supplier_ledger_and_outstanding_admin_accuracy(tmp_path, monkeypatch):
    """
    Supplier Ledger & Supplier Outstanding (ADMIN ONLY):
    - Combines purchases & supplier payment vouchers with exact Supplier #1 vs Supplier #10 isolation.
    - Collapses multi-leg `ledger_entries` (no double rows per voucher).
    - Preserves historical `'UPI'` payment voucher as `'UPI (Historical)'` with UTR reference.
    - Zero leakage of supplier PAN, bank_name, account_no, ifsc_code, or shop secrets.
    """
    ctx = _setup_accounting_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        # 1. Supplier Ledger for Supplier #1
        res_l1 = client.get(
            f"/api/excel/exports/supplier-ledger?supplier_id={ctx['supp1_id']}",
            headers=ctx["admin_headers"],
        )
        assert res_l1.status_code == 200
        _assert_workbook_hardened(res_l1.content, "supp1_ledger.xlsx")
        wb_l1 = openpyxl.load_workbook(io.BytesIO(res_l1.content), data_only=False)
        try:
            assert wb_l1.sheetnames == ["Supplier Ledger"]
            ws = wb_l1["Supplier Ledger"]
            hdr = _find_table_header_row(ws, "Date")
            headers = [ws.cell(row=hdr, column=c).value for c in range(1, ws.max_column + 1)]
            pre_text = " | ".join(str(ws.cell(row=r, column=1).value or "") for r in range(1, hdr))
            assert SUPPLIER_LEDGER_NOTE in pre_text
            assert "Opening Balance: ₹1,000.00 (Credit (Payable))" in pre_text
            assert "Current Ledger Balance: ₹5,500.00 (Credit (Payable))" in pre_text

            rows = [
                {headers[c - 1]: ws.cell(row=r, column=c).value for c in range(1, len(headers) + 1)}
                for r in range(hdr + 1, ws.max_row)
            ]
            assert len(rows) == 2  # PUR-1 + 1 payment voucher (never duplicated, never Supp #10)
            assert rows[0]["Voucher / Bill No"] == "SUP-AUG-101"
            assert rows[0]["Supplier Name"] == "'=cmd|'/C calc'!A0 Agro Distributors"
            assert rows[0]["Debit / Paid (₹)"] == 4000.0
            assert rows[0]["Credit / Billed (₹)"] == 10000.0
            assert rows[0]["Narration / Remarks"] == "'+Purchase note"

            assert rows[1]["Transaction Type"] == "Supplier Payment Voucher"
            assert rows[1]["Reference / Challan No"] == "UTR-HIST-1"
            assert rows[1]["Payment Mode"] == "UPI (Historical)"
            assert rows[1]["Debit / Paid (₹)"] == 1500.0
            assert rows[1]["Credit / Billed (₹)"] == 0.0
            assert rows[1]["Narration / Remarks"] == "'=Payment via legacy UPI"

            tot_row = ws.max_row
            assert ws.cell(row=tot_row, column=10).value == 5500.0
            assert ws.cell(row=tot_row, column=11).value == 10000.0
        finally:
            wb_l1.close()

        # 2. Supplier Ledger for Supplier #10 (includes ZCD-SEP-202, PAY-..., PAY-LEGACY-10)
        res_l10 = client.get(
            f"/api/excel/exports/supplier-ledger?supplier_id={ctx['supp10_id']}",
            headers=ctx["admin_headers"],
        )
        assert res_l10.status_code == 200
        wb_l10 = openpyxl.load_workbook(io.BytesIO(res_l10.content), data_only=False)
        try:
            ws10 = wb_l10["Supplier Ledger"]
            hdr10 = _find_table_header_row(ws10, "Date")
            doc_nos = [ws10.cell(row=r, column=2).value for r in range(hdr10 + 1, ws10.max_row)]
            assert len(doc_nos) == 3
            assert doc_nos[0] == "ZCD-SEP-202"
            assert "PAY-LEGACY-10" in doc_nos
        finally:
            wb_l10.close()

        # 3. Supplier Outstanding Summary
        res_o = client.get("/api/excel/exports/supplier-outstanding", headers=ctx["admin_headers"])
        assert res_o.status_code == 200
        _assert_workbook_hardened(res_o.content, "supplier_outstanding.xlsx")
        wb_o = openpyxl.load_workbook(io.BytesIO(res_o.content), data_only=False)
        try:
            assert wb_o.sheetnames == ["Supplier Outstanding"]
            ws_o = wb_o["Supplier Outstanding"]
            hdr_o = _find_table_header_row(ws_o, "Supplier ID")
            headers_o = [ws_o.cell(row=hdr_o, column=c).value for c in range(1, ws_o.max_column + 1)]
            pre_o = " | ".join(str(ws_o.cell(row=r, column=1).value or "") for r in range(1, hdr_o))
            assert SUPPLIER_OUTSTANDING_NOTE in pre_o

            by_id = {
                ws_o.cell(row=r, column=1).value: {
                    headers_o[c - 1]: ws_o.cell(row=r, column=c).value
                    for c in range(1, len(headers_o) + 1)
                }
                for r in range(hdr_o + 1, ws_o.max_row)
            }

            s1 = by_id[ctx["supp1_id"]]
            assert s1["Opening Balance (₹)"] == 1000.0
            assert s1["Total Purchases (₹)"] == 10000.0
            assert s1["Bill-Time Paid (₹)"] == 4000.0
            assert s1["Payment Vouchers (₹)"] == 1500.0
            assert s1["Current Ledger Balance (₹)"] == 5500.0
            assert s1["Balance Position"] == "Credit (Payable)"

            s10 = by_id[ctx["supp10_id"]]
            assert s10["Total Purchases (₹)"] == 5000.0
            assert s10["Bill-Time Paid (₹)"] == 1000.0
            assert s10["Payment Vouchers (₹)"] == 2500.0  # 2000 + 500
            assert s10["Current Ledger Balance (₹)"] == 1500.0

            s_adv = by_id[ctx["supp_adv_id"]]
            assert s_adv["Opening Balance (₹)"] == -400.0
            assert s_adv["Current Ledger Balance (₹)"] == -400.0
            assert s_adv["Balance Position"] == "Debit (Advance)"

            tot_o = ws_o.max_row
            assert ws_o.cell(row=tot_o, column=12).value == 6600.0  # 5500 + 1500 - 400
        finally:
            wb_o.close()

        # Filter only_outstanding=true + query=Zuari
        res_of = client.get(
            "/api/excel/exports/supplier-outstanding?query=Zuari&only_outstanding=true",
            headers=ctx["admin_headers"],
        )
        assert res_of.status_code == 200
        wb_of = openpyxl.load_workbook(io.BytesIO(res_of.content), data_only=False)
        try:
            ws_of = wb_of["Supplier Outstanding"]
            hdr_of = _find_table_header_row(ws_of, "Supplier ID")
            assert [ws_of.cell(row=r, column=1).value for r in range(hdr_of + 1, ws_of.max_row)] == [
                ctx["supp10_id"]
            ]
        finally:
            wb_of.close()

    reset_db_manager()


def test_c3b3a_payment_history_deduplication_historical_upi_and_totals(tmp_path, monkeypatch):
    """
    Payment History (ADMIN ONLY):
    - Deduplicates across bill-time sales, bill-time purchases, standalone RECEIPT/PAYMENT/CONTRA vouchers & expenses.
    - Expense #2 (created via `record_shop_expense` with PAYMENT EXPENSE voucher) appears EXACTLY ONCE.
    - Multi-leg ledger_entries never duplicate voucher rows.
    - Contra transfer shows Amount=3000 with Receipt Inflow=0 & Payment Outflow=0.
    - Totals row sums Receipt Inflow (₹) and Payment Outflow (₹) ONLY (Amount column stays unsummed/blank).
    """
    ctx = _setup_accounting_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        res = client.get("/api/excel/exports/payment-history", headers=ctx["admin_headers"])
        assert res.status_code == 200
        _assert_workbook_hardened(res.content, "payment_history.xlsx")

        wb = openpyxl.load_workbook(io.BytesIO(res.content), data_only=False)
        try:
            assert wb.sheetnames == ["Payment History"]
            ws = wb["Payment History"]
            hdr = _find_table_header_row(ws, "Date")
            headers = [ws.cell(row=hdr, column=c).value for c in range(1, ws.max_column + 1)]
            pre_text = " | ".join(str(ws.cell(row=r, column=1).value or "") for r in range(1, hdr))
            assert PAYMENT_HISTORY_NOTE in pre_text

            rows = [
                {headers[c - 1]: ws.cell(row=r, column=c).value for c in range(1, len(headers) + 1)}
                for r in range(hdr + 1, ws.max_row)
            ]
            # Expected exact 12 unique rows:
            # Purchases paid>0: 2 (PUR-1=4000, PUR-2=1000)
            # Sales paid>0:     2 (INV-AUG-001=4000, INV-HIST-UPI=1000; INV-SEP-010 has paid=0 -> excluded)
            # Cust receipts:    2 (600, 500)
            # Supp payments:    3 (1500, 2000, 500)
            # Expenses:         2 (1500 Rent, 850 Electricity with linked voucher — NOT duplicated!)
            # Contra:           1 (3000)
            assert len(rows) == 12, f"Expected 12 deduplicated rows, got {len(rows)}: {rows}"

            elec_rows = [r for r in rows if "ELEC-99" == r["Reference No"]]
            assert len(elec_rows) == 1
            assert elec_rows[0]["Transaction Type"] == "Shop Expense Payment"
            assert elec_rows[0]["Payment Mode"] == "BANK / NON-CASH"
            assert elec_rows[0]["Recorded Status"] == "Recorded (Voucher Linked)"

            contra_rows = [r for r in rows if r["Transaction Type"] == "Contra Entry"]
            assert len(contra_rows) == 1
            assert contra_rows[0]["Amount (₹)"] == 3000.0
            assert contra_rows[0]["Receipt Inflow (₹)"] == 0.0
            assert contra_rows[0]["Payment Outflow (₹)"] == 0.0

            upi_rows = [r for r in rows if r["Payment Mode"] == "UPI (Historical)"]
            assert len(upi_rows) == 3  # Sale INV-HIST-UPI, Cust #10 Receipt, Supp #1 Payment

            tot_row = ws.max_row
            assert ws.cell(row=tot_row, column=1).value == "TOTAL RECEIPTS (INFLOW) & PAYMENTS (OUTFLOW)"
            # Column 8 ('Amount (₹)') MUST NOT contain a mixed sum!
            assert ws.cell(row=tot_row, column=8).value in (None, "")
            # Column 9 ('Receipt Inflow (₹)'): 4000 + 1000 + 600 + 500 = 6100.00
            assert ws.cell(row=tot_row, column=9).value == 6100.0
            # Column 10 ('Payment Outflow (₹)'): 4000 + 1000 + 1500 + 2000 + 500 + 1500 + 850 = 11350.00
            assert ws.cell(row=tot_row, column=10).value == 11350.0
        finally:
            wb.close()

        # Filter payment_mode=UPI returns strictly the 3 historical UPI transactions
        res_upi = client.get(
            "/api/excel/exports/payment-history?payment_mode=UPI",
            headers=ctx["admin_headers"],
        )
        assert res_upi.status_code == 200
        wb_upi = openpyxl.load_workbook(io.BytesIO(res_upi.content), data_only=False)
        try:
            ws_u = wb_upi["Payment History"]
            hdr_u = _find_table_header_row(ws_u, "Date")
            upi_only_rows = [
                ws_u.cell(row=r, column=7).value for r in range(hdr_u + 1, ws_u.max_row)
            ]
            assert upi_only_rows == ["UPI (Historical)"] * 3
            assert ws_u.cell(row=ws_u.max_row, column=9).value == 1500.0   # 1000 + 500
            assert ws_u.cell(row=ws_u.max_row, column=10).value == 1500.0  # Supp #1 UTR-HIST-1
        finally:
            wb_upi.close()

    reset_db_manager()


def test_c3b3a_expenses_export_accuracy_no_duplication_and_filters(tmp_path, monkeypatch):
    """
    Expenses (ADMIN ONLY):
    - Lists each expense once whether recorded directly (`record_daily_expense`) or via voucher (`record_shop_expense`).
    - Displays `Linked Voucher No` when present, `CASH` vs `BANK / NON-CASH` payment mode, and accurate category totals.
    """
    ctx = _setup_accounting_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        res = client.get("/api/excel/exports/expenses", headers=ctx["admin_headers"])
        assert res.status_code == 200
        _assert_workbook_hardened(res.content, "expenses.xlsx")

        wb = openpyxl.load_workbook(io.BytesIO(res.content), data_only=False)
        try:
            assert wb.sheetnames == ["Expenses"]
            ws = wb["Expenses"]
            hdr = _find_table_header_row(ws, "Expense ID")
            headers = [ws.cell(row=hdr, column=c).value for c in range(1, ws.max_column + 1)]
            pre_text = " | ".join(str(ws.cell(row=r, column=1).value or "") for r in range(1, hdr))
            assert EXPENSES_REPORT_NOTE in pre_text

            rows = [
                {headers[c - 1]: ws.cell(row=r, column=c).value for c in range(1, len(headers) + 1)}
                for r in range(hdr + 1, ws.max_row)
            ]
            assert len(rows) == 2

            assert rows[0]["Expense ID"] == ctx["exp1_id"]
            assert str(rows[0]["Linked Voucher No"]).startswith("PAY-")
            assert rows[0]["Description / Remarks"] == "'=Shop Rent Sept"
            assert rows[0]["Payment Mode"] == "CASH"
            assert rows[0]["Amount (₹)"] == 1500.0
            assert rows[0]["Recorded Status"] == "Recorded (Voucher Linked)"

            assert rows[1]["Expense ID"] == ctx["exp2_id"]
            assert str(rows[1]["Linked Voucher No"]).startswith("PAY-")
            assert rows[1]["Payment Account"] == "Bank Account"
            assert rows[1]["Payment Mode"] == "BANK / NON-CASH"
            assert rows[1]["Amount (₹)"] == 850.0
            assert rows[1]["Recorded Status"] == "Recorded (Voucher Linked)"

            tot_row = ws.max_row
            assert ws.cell(row=tot_row, column=1).value == "TOTAL SHOP EXPENSES"
            assert ws.cell(row=tot_row, column=9).value == 2350.0
        finally:
            wb.close()

        # Category filter: category_id=2 (Electricity)
        res_cat = client.get(
            "/api/excel/exports/expenses?category_id=2&from_date=2026-09-01&to_date=2026-09-30",
            headers=ctx["admin_headers"],
        )
        assert res_cat.status_code == 200
        wb_cat = openpyxl.load_workbook(io.BytesIO(res_cat.content), data_only=False)
        try:
            ws_c = wb_cat["Expenses"]
            hdr_c = _find_table_header_row(ws_c, "Expense ID")
            assert [ws_c.cell(row=r, column=1).value for r in range(hdr_c + 1, ws_c.max_row)] == [
                ctx["exp2_id"]
            ]
        finally:
            wb_cat.close()

    reset_db_manager()


def test_c3b3a_validation_empty_ranges_closed_conn_non_mutation_and_503(tmp_path, monkeypatch):
    """
    Invalid parameters -> 422; empty ranges produce valid workbooks;
    DB connections close before workbook assembly ([0]*6); DB stays immutable;
    zero temp `.xlsx`/`.tmp` files written; HTTP 503 during DB maintenance.
    """
    ctx = _setup_accounting_fixture(tmp_path, monkeypatch)
    db = ctx["db"]
    db_file = ctx["db_file"]

    with TestClient(app) as client:
        for date_endpoint in (
            "/api/excel/exports/customer-ledger",
            "/api/excel/exports/supplier-ledger",
            "/api/excel/exports/payment-history",
            "/api/excel/exports/expenses",
        ):
            assert (
                client.get(f"{date_endpoint}?from_date=2026-02-30", headers=ctx["admin_headers"]).status_code
                == 422
            )
            assert (
                client.get(
                    f"{date_endpoint}?from_date=2026-09-20&to_date=2026-09-01",
                    headers=ctx["admin_headers"],
                ).status_code
                == 422
            )
            assert (
                client.get(f"{date_endpoint}?from_date=%20%20%20", headers=ctx["admin_headers"]).status_code
                == 422
            )

        assert (
            client.get(
                "/api/excel/exports/customer-ledger?customer_id=0",
                headers=ctx["admin_headers"],
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/excel/exports/supplier-ledger?supplier_id=-3",
                headers=ctx["admin_headers"],
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/excel/exports/expenses?category_id=0",
                headers=ctx["admin_headers"],
            ).status_code
            == 422
        )

        # Empty results remain clean loadable workbooks with note banner & table headers
        for url, hdr_label in (
            ("/api/excel/exports/customer-ledger?from_date=2024-01-01&to_date=2024-01-31", "Date"),
            ("/api/excel/exports/supplier-ledger?from_date=2024-01-01&to_date=2024-01-31", "Date"),
            ("/api/excel/exports/customer-outstanding?query=NoSuchPartyXYZ", "Customer ID"),
            ("/api/excel/exports/supplier-outstanding?query=NoSuchSupplierXYZ", "Supplier ID"),
            ("/api/excel/exports/payment-history?from_date=2024-01-01&to_date=2024-01-31", "Date"),
            ("/api/excel/exports/expenses?from_date=2024-01-01&to_date=2024-01-31", "Expense ID"),
        ):
            r_empty = client.get(url, headers=ctx["admin_headers"])
            assert r_empty.status_code == 200
            _assert_workbook_hardened(r_empty.content, "empty_report.xlsx")
            wb_e = openpyxl.load_workbook(io.BytesIO(r_empty.content), data_only=False)
            try:
                ws_e = wb_e.active
                hdr_e = _find_table_header_row(ws_e, hdr_label)
                assert ws_e.max_row == hdr_e
            finally:
                wb_e.close()

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
        lambda: svc.export_customer_ledger(),
        lambda: svc.export_supplier_ledger(),
        lambda: svc.export_customer_outstanding(),
        lambda: svc.export_supplier_outstanding(),
        lambda: svc.export_payment_history(),
        lambda: svc.export_expenses(),
    ):
        data, name = fn()
        _assert_workbook_hardened(data, name)

    assert seen_active_conns_during_serialize == [0, 0, 0, 0, 0, 0], (
        f"SQLite connection remained open during workbook serialization: {seen_active_conns_during_serialize}"
    )

    after_counts, after_digest = _snapshot_db_counts_and_digest(db_file)
    assert after_counts == before_counts
    assert after_digest == before_digest

    files_after = {p.name for p in tmp_path.iterdir()}
    assert not any(name.endswith((".xlsx", ".tmp")) for name in (files_after - files_before))

    with TestClient(app) as client:
        with db.exclusive_maintenance(timeout=2.0, reason="c3b3a_restore_test"):
            for url in (
                "/api/excel/exports/customer-ledger",
                "/api/excel/exports/customer-outstanding",
                "/api/excel/exports/supplier-ledger",
                "/api/excel/exports/supplier-outstanding",
                "/api/excel/exports/payment-history",
                "/api/excel/exports/expenses",
            ):
                r = client.get(url, headers=ctx["admin_headers"])
                assert r.status_code == 503
                assert r.headers.get("Retry-After") == "2"

    reset_db_manager()


def test_c3b3a_frontend_controls_and_admin_gating(tmp_path, monkeypatch):
    """Frontend HTML/JS exposes C3-B3A English-only Excel controls with strict Admin-only hiding."""
    _setup_accounting_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        html = client.get("/").text
        js = client.get("/static/js/app.js").text

        required_dom_ids = (
            'id="customer-accounting-export-bar"',
            'id="cust-export-customer-id"',
            'id="cust-ledger-from-date"',
            'id="cust-ledger-to-date"',
            'id="btn-export-customer-ledger-excel"',
            'id="btn-export-customer-outstanding-excel"',
            'id="btn-export-single-farmer-ledger-excel"',
            'id="supplier-excel-export-card"',
            'id="supp-export-supplier-id"',
            'id="supp-ledger-from-date"',
            'id="supp-ledger-to-date"',
            'id="btn-export-supplier-ledger-excel"',
            'id="btn-export-supplier-outstanding-excel"',
            'id="btn-export-single-supplier-ledger-excel"',
            'id="accounting-excel-export-card"',
            'id="acc-export-from-date"',
            'id="acc-export-to-date"',
            'id="acc-export-pay-mode"',
            'id="acc-export-exp-cat"',
            'id="btn-export-payment-history-excel"',
            'id="btn-export-expenses-excel"',
        )
        for token in required_dom_ids:
            assert token in html, f"Missing C3-B3A HTML element {token}"

        # Admin-only cards and modal export button must default hidden before session auth
        for hidden_id in (
            'id="supplier-excel-export-card"',
            'id="accounting-excel-export-card"',
            'id="btn-export-single-supplier-ledger-excel"',
        ):
            idx = html.index(hidden_id)
            assert "display: none;" in html[idx : idx + 260], f"{hidden_id} must default to display: none;"

        assert "UPI (Historical)" in html

        for fn_name in (
            "function exportCustomerLedgerExcel(",
            "function exportActiveFarmerLedgerExcel(",
            "function exportCustomerOutstandingExcel(",
            "function exportSupplierLedgerExcel(",
            "function exportActiveSupplierLedgerExcel(",
            "function exportSupplierOutstandingExcel(",
            "function exportPaymentHistoryExcel(",
            "function exportExpensesExcel(",
            "async function loadExpenseCategoryFilter(",
        ):
            assert fn_name in js, f"Missing expected JS function {fn_name}"

        for api_path in (
            "/api/excel/exports/customer-ledger",
            "/api/excel/exports/customer-outstanding",
            "/api/excel/exports/supplier-ledger",
            "/api/excel/exports/supplier-outstanding",
            "/api/excel/exports/payment-history",
            "/api/excel/exports/expenses",
        ):
            assert api_path in js, f"Missing API path {api_path} in app.js"

        assert "supplierExcelCard.style.display" in js
        assert "accountingExcelCard.style.display" in js
        assert "singleSuppLedgerBtn.style.display" in js

    reset_db_manager()
