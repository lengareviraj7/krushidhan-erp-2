"""
Comprehensive Test Suite for Krushidhan ERP Core Double-Entry Accounting Foundation.
Verifies Step 2B-1 milestone requirements:
1. Cash sale accounting (Dr Cash/Bank, Cr Sales, Cr Output GST).
2. Credit sale accounting (Dr Sundry Debtors, Cr Sales, Cr Output GST).
3. Cash purchase accounting (Dr Purchase Account, Dr Input GST, Cr Cash/Bank).
4. Credit purchase accounting (Dr Purchase Account, Dr Input GST, Cr Sundry Creditors).
5. Customer receipt accounting (Dr Cash/Bank, Cr Sundry Debtors - eliminates self-cancelling entry).
6. Supplier payment accounting (Dr Sundry Creditors, Cr Cash/Bank - eliminates self-cancelling entry).
7. Shop expense accounting (Dr Expense Account, Cr Cash/Bank).
8. Cash and bank balance movements verified via ledger statements.
9. Customer outstanding balance reconciliation (Khata == sum(due) - sum(receipts) == Sundry Debtors).
10. Supplier outstanding balance reconciliation (Payable == sum(due) - sum(payments) == Sundry Creditors).
11. Double-entry balance integrity for every voucher (sum(dr) == sum(cr) to the exact paise).
12. Atomic transaction rollback when voucher posting fails.
13. Duplicate voucher number prevention & unique numbering.
14. Idempotent Migration v3 (re-running adds zero duplicates, leaves existing intact).
15. Historical transactions and master records remain 100% unaltered.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
import pytest

from src.db.connection import DatabaseManager, reset_db_manager
from src.db.migrations import (
    Migration,
    MigrationRunner,
    _migrate_v3_core_ledger_accounts,
)
from src.models.accounting import Expense, LedgerAccount, LedgerEntry, Voucher
from src.models.inventory import StockBatch
from src.models.master_data import Customer, Product, Supplier
from src.models.purchase import Purchase, PurchaseItem
from src.models.sales import Sale, SaleItem
from src.repositories.accounting_repository import AccountingRepository
from src.repositories.inventory_repository import InventoryRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.repositories.sales_repository import SalesRepository
from src.services.accounting_service import AccountingService
from src.services.purchase_service import PurchaseService
from src.services.sales_service import SalesService


@pytest.fixture
def acc_env(tmp_path, monkeypatch):
    """Provides an isolated SQLite database environment with seed data and initialized services."""
    test_db_path = tmp_path / "test_core_accounting.db"
    test_bkp_dir = tmp_path / "test_acc_backups"
    monkeypatch.setenv("DB_PATH", str(test_db_path))
    monkeypatch.setenv("BACKUP_DIR", str(test_bkp_dir))
    reset_db_manager()

    db = DatabaseManager(db_path=test_db_path, backup_dir=test_bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)

    master_repo = MasterDataRepository(db)
    inv_repo = InventoryRepository(db)
    sales_repo = SalesRepository(db)
    acc_repo = AccountingRepository(db)
    sales_svc = SalesService(db)
    purchase_svc = PurchaseService(db)
    acc_svc = AccountingService(db)

    # Setup base entities
    cust_id = master_repo.create_customer(
        Customer(customer_name="Ramesh Patil", mobile="9822001122", village="Baramati", opening_balance=0.0)
    )
    supp_id = master_repo.create_supplier(
        Supplier(supplier_name="Kaveri Seed Company", gstin="27AAACK1234F1Z5", city="Hyderabad", opening_balance=0.0)
    )
    prod_id = master_repo.create_product(
        Product(
            product_name="Kaveri Corn Hybrid KMH-258",
            category_id=2,
            unit_id=9,
            tax_group_id=4,  # GST 18% (9% CGST, 9% SGST)
            default_purchase_rate=100.0,
            default_sale_rate=150.0,
        )
    )
    batch_id = inv_repo.upsert_batch(
        StockBatch(
            product_id=prod_id,
            batch_no="KVR-2026-01",
            mfg_date="2026-01-01",
            exp_date="2027-12-31",
            purchase_rate=100.0,
            sale_rate=150.0,
            mrp=160.0,
            opening_qty=200.0,
            current_qty=200.0,
        )
    )

    env = {
        "db": db,
        "master_repo": master_repo,
        "inv_repo": inv_repo,
        "sales_repo": sales_repo,
        "acc_repo": acc_repo,
        "sales_svc": sales_svc,
        "purchase_svc": purchase_svc,
        "acc_svc": acc_svc,
        "cust_id": cust_id,
        "supp_id": supp_id,
        "prod_id": prod_id,
        "batch_id": batch_id,
    }
    yield env
    reset_db_manager()


# ---------------------------------------------------------------------------
# Test 1: Cash Sale Accounting
# ---------------------------------------------------------------------------
def test_01_cash_sale_accounting(acc_env):
    """
    Cash sale must:
    - Debit Cash in Hand (net amount)
    - Credit Sales Account (taxable)
    - Credit Output GST (CGST/SGST)
    - Balance round-off to exact paise if present
    - Leave customer balance unchanged (0.0)
    """
    sales_svc = acc_env["sales_svc"]
    acc_repo = acc_env["acc_repo"]
    master_repo = acc_env["master_repo"]

    cash_ac_before = acc_repo.get_cash_account().current_balance

    # 10 units @ 150 = 1500 net (taxable 1271.19, cgst 114.41, sgst 114.41, round_off -0.01)
    sale_item = SaleItem(
        product_id=acc_env["prod_id"],
        batch_id=acc_env["batch_id"],
        qty=10.0,
        unit_id=9,
        sale_rate=150.0,
        mrp=160.0,
        taxable_amount=1271.19,
        cgst_rate=9.0,
        cgst_amount=114.41,
        sgst_rate=9.0,
        sgst_amount=114.41,
        total_amount=1500.0,
    )
    sale = Sale(
        invoice_no="INV-CS-001",
        sale_date="2026-10-01",
        customer_id=acc_env["cust_id"],
        payment_mode="CASH",
        total_taxable=1271.19,
        total_cgst=114.41,
        total_sgst=114.41,
        total_igst=0.0,
        net_amount=1500.0,
        paid_amount=1500.0,
        items=[sale_item],
    )
    sale_id = sales_svc.process_sales_invoice(sale)
    assert sale_id > 0

    # Verify voucher entries
    vch = acc_env["db"].fetch_one("SELECT * FROM vouchers WHERE reference_type = 'SALE' AND reference_id = ?;", (sale_id,))
    assert vch is not None
    entries = acc_env["db"].fetch_all("SELECT * FROM ledger_entries WHERE voucher_id = ?;", (vch["voucher_id"],))

    # Verify double-entry balance sum(dr) == sum(cr)
    tot_dr = sum(e["debit_amount"] for e in entries)
    tot_cr = sum(e["credit_amount"] for e in entries)
    assert round(tot_dr, 2) == round(tot_cr, 2)
    assert round(tot_dr, 2) == round(vch["total_amount"], 2)

    # Verify Cash in Hand debited 1500.0
    cash_ac = acc_repo.get_cash_account()
    cash_entries = [e for e in entries if e["account_id"] == cash_ac.account_id]
    assert len(cash_entries) == 1
    assert cash_entries[0]["debit_amount"] == 1500.0
    assert cash_ac.current_balance == cash_ac_before + 1500.0

    # Verify Sales Account credited 1271.19
    sales_ac = acc_repo.get_sales_account()
    sales_entries = [e for e in entries if e["account_id"] == sales_ac.account_id]
    assert len(sales_entries) == 1
    assert sales_entries[0]["credit_amount"] == 1271.19

    # Verify Customer current_balance is untouched (cash sale)
    cust = master_repo.get_customer_by_id(acc_env["cust_id"])
    assert cust.current_balance == 0.0


# ---------------------------------------------------------------------------
# Test 2: Credit Sale Accounting
# ---------------------------------------------------------------------------
def test_02_credit_sale_accounting(acc_env):
    """
    Credit sale must:
    - Debit Sundry Debtors (due amount) - NEVER debit Sales Account
    - Credit Sales Account (taxable)
    - Credit Output GST (CGST/SGST)
    - Increase customer current_balance
    - Leave Cash in Hand unchanged
    """
    sales_svc = acc_env["sales_svc"]
    acc_repo = acc_env["acc_repo"]
    master_repo = acc_env["master_repo"]

    cash_ac_before = acc_repo.get_cash_account().current_balance
    debtors_ac_before = acc_repo.get_debtors_account().current_balance

    sale_item = SaleItem(
        product_id=acc_env["prod_id"],
        batch_id=acc_env["batch_id"],
        qty=5.0,
        unit_id=9,
        sale_rate=150.0,
        mrp=160.0,
        taxable_amount=635.59,
        cgst_rate=9.0,
        cgst_amount=57.20,
        sgst_rate=9.0,
        sgst_amount=57.20,
        total_amount=750.0,
    )
    sale = Sale(
        invoice_no="INV-CR-001",
        sale_date="2026-10-02",
        customer_id=acc_env["cust_id"],
        payment_mode="CREDIT",
        total_taxable=635.59,
        total_cgst=57.20,
        total_sgst=57.20,
        total_igst=0.0,
        net_amount=750.0,
        paid_amount=0.0,
        items=[sale_item],
    )
    sale_id = sales_svc.process_sales_invoice(sale)
    assert sale_id > 0

    vch = acc_env["db"].fetch_one("SELECT * FROM vouchers WHERE reference_type = 'SALE' AND reference_id = ?;", (sale_id,))
    entries = acc_env["db"].fetch_all("SELECT * FROM ledger_entries WHERE voucher_id = ?;", (vch["voucher_id"],))

    # Verify double entry
    tot_dr = sum(e["debit_amount"] for e in entries)
    tot_cr = sum(e["credit_amount"] for e in entries)
    assert round(tot_dr, 2) == round(tot_cr, 2) == 750.0

    # Verify Sundry Debtors debited 750.0
    debtors_ac = acc_repo.get_debtors_account()
    debtor_entries = [e for e in entries if e["account_id"] == debtors_ac.account_id]
    assert len(debtor_entries) == 1
    assert debtor_entries[0]["debit_amount"] == 750.0
    assert debtors_ac.current_balance == debtors_ac_before + 750.0

    # Verify Cash in Hand is unchanged
    assert acc_repo.get_cash_account().current_balance == cash_ac_before

    # Verify Customer current_balance increased by 750.0
    cust = master_repo.get_customer_by_id(acc_env["cust_id"])
    assert cust.current_balance == 750.0


# ---------------------------------------------------------------------------
# Test 3: Cash Purchase Accounting
# ---------------------------------------------------------------------------
def test_03_cash_purchase_accounting(acc_env):
    """
    Cash purchase must:
    - Debit Purchase Account (taxable)
    - Debit Input GST (CGST/SGST)
    - Credit Cash in Hand (paid amount)
    - Decrease Cash in Hand
    - Leave supplier balance unchanged (0.0)
    """
    purchase_svc = acc_env["purchase_svc"]
    acc_repo = acc_env["acc_repo"]
    master_repo = acc_env["master_repo"]

    cash_ac_before = acc_repo.get_cash_account().current_balance

    pur_item = PurchaseItem(
        product_id=acc_env["prod_id"],
        batch_no="KVR-2026-02",
        mfg_date="2026-02-01",
        exp_date="2027-12-31",
        qty=20.0,
        unit_id=9,
        purchase_rate=100.0,
        sale_rate=150.0,
        mrp=160.0,
        taxable_amount=2000.0,
        cgst_rate=9.0,
        cgst_amount=180.0,
        sgst_rate=9.0,
        sgst_amount=180.0,
        total_amount=2360.0,
    )
    purchase = Purchase(
        invoice_no="PUR-CS-001",
        purchase_date="2026-10-03",
        supplier_id=acc_env["supp_id"],
        payment_type="CASH",
        total_taxable=2000.0,
        total_cgst=180.0,
        total_sgst=180.0,
        total_igst=0.0,
        net_amount=2360.0,
        paid_amount=2360.0,
        due_amount=0.0,
        items=[pur_item],
    )
    pur_id = purchase_svc.process_inward_purchase(purchase)
    assert pur_id > 0

    vch = acc_env["db"].fetch_one("SELECT * FROM vouchers WHERE reference_type = 'PURCHASE' AND reference_id = ?;", (pur_id,))
    entries = acc_env["db"].fetch_all("SELECT * FROM ledger_entries WHERE voucher_id = ?;", (vch["voucher_id"],))

    tot_dr = sum(e["debit_amount"] for e in entries)
    tot_cr = sum(e["credit_amount"] for e in entries)
    assert round(tot_dr, 2) == round(tot_cr, 2) == 2360.0

    # Verify Cash in Hand credited 2360.0 (decreased)
    cash_ac = acc_repo.get_cash_account()
    cash_entries = [e for e in entries if e["account_id"] == cash_ac.account_id]
    assert len(cash_entries) == 1
    assert cash_entries[0]["credit_amount"] == 2360.0
    assert cash_ac.current_balance == cash_ac_before - 2360.0

    # Verify Purchase Account debited 2000.0
    pur_ac = acc_repo.get_purchase_account()
    pur_entries = [e for e in entries if e["account_id"] == pur_ac.account_id]
    assert len(pur_entries) == 1
    assert pur_entries[0]["debit_amount"] == 2000.0

    # Verify Supplier balance untouched
    supp = master_repo.get_supplier_by_id(acc_env["supp_id"])
    assert supp.current_balance == 0.0


# ---------------------------------------------------------------------------
# Test 4: Credit Purchase Accounting
# ---------------------------------------------------------------------------
def test_04_credit_purchase_accounting(acc_env):
    """
    Credit purchase must:
    - Debit Purchase Account (taxable)
    - Debit Input GST (CGST/SGST)
    - Credit Sundry Creditors (due amount) - NEVER credit Purchase Account
    - Increase supplier payable balance
    - Leave Cash in Hand unchanged
    """
    purchase_svc = acc_env["purchase_svc"]
    acc_repo = acc_env["acc_repo"]
    master_repo = acc_env["master_repo"]

    cash_ac_before = acc_repo.get_cash_account().current_balance
    creditors_ac_before = acc_repo.get_creditors_account().current_balance

    pur_item = PurchaseItem(
        product_id=acc_env["prod_id"],
        batch_no="KVR-2026-03",
        mfg_date="2026-02-01",
        exp_date="2027-12-31",
        qty=50.0,
        unit_id=9,
        purchase_rate=100.0,
        sale_rate=150.0,
        mrp=160.0,
        taxable_amount=5000.0,
        cgst_rate=9.0,
        cgst_amount=450.0,
        sgst_rate=9.0,
        sgst_amount=450.0,
        total_amount=5900.0,
    )
    purchase = Purchase(
        invoice_no="PUR-CR-001",
        purchase_date="2026-10-04",
        supplier_id=acc_env["supp_id"],
        payment_type="CREDIT",
        total_taxable=5000.0,
        total_cgst=450.0,
        total_sgst=450.0,
        total_igst=0.0,
        net_amount=5900.0,
        paid_amount=0.0,
        due_amount=5900.0,
        items=[pur_item],
    )
    pur_id = purchase_svc.process_inward_purchase(purchase)
    assert pur_id > 0

    vch = acc_env["db"].fetch_one("SELECT * FROM vouchers WHERE reference_type = 'PURCHASE' AND reference_id = ?;", (pur_id,))
    entries = acc_env["db"].fetch_all("SELECT * FROM ledger_entries WHERE voucher_id = ?;", (vch["voucher_id"],))

    tot_dr = sum(e["debit_amount"] for e in entries)
    tot_cr = sum(e["credit_amount"] for e in entries)
    assert round(tot_dr, 2) == round(tot_cr, 2) == 5900.0

    # Verify Sundry Creditors credited 5900.0
    creditors_ac = acc_repo.get_creditors_account()
    cred_entries = [e for e in entries if e["account_id"] == creditors_ac.account_id]
    assert len(cred_entries) == 1
    assert cred_entries[0]["credit_amount"] == 5900.0
    assert creditors_ac.current_balance == creditors_ac_before + 5900.0

    # Verify Cash in Hand is unchanged
    assert acc_repo.get_cash_account().current_balance == cash_ac_before

    # Verify Supplier balance increased by 5900.0
    supp = master_repo.get_supplier_by_id(acc_env["supp_id"])
    assert supp.current_balance == 5900.0


# ---------------------------------------------------------------------------
# Test 5: Customer Receipt Accounting
# ---------------------------------------------------------------------------
def test_05_customer_receipt_accounting(acc_env):
    """
    Customer receipt must:
    - Debit Cash in Hand (or Bank)
    - Credit Sundry Debtors
    - Eliminate self-cancelling cash entry
    - Reduce customer balance
    """
    acc_svc = acc_env["acc_svc"]
    acc_repo = acc_env["acc_repo"]
    master_repo = acc_env["master_repo"]

    # Pre-condition: Set customer balance to 1000.0
    master_repo.update_customer_balance(acc_env["cust_id"], 1000.0)
    debtors_ac_before = acc_repo.get_debtors_account().current_balance
    cash_ac_before = acc_repo.get_cash_account().current_balance

    vch_id = acc_svc.record_customer_receipt(
        customer_id=acc_env["cust_id"],
        receipt_date="2026-10-05",
        amount=400.0,
        payment_mode="CASH",
        narration="Partial payment received on Khata",
    )
    assert vch_id > 0

    entries = acc_env["db"].fetch_all("SELECT * FROM ledger_entries WHERE voucher_id = ?;", (vch_id,))
    assert len(entries) == 2

    # Dr Cash in Hand 400.0
    cash_ac = acc_repo.get_cash_account()
    dr_cash = [e for e in entries if e["account_id"] == cash_ac.account_id and e["debit_amount"] == 400.0]
    assert len(dr_cash) == 1
    assert cash_ac.current_balance == cash_ac_before + 400.0

    # Cr Sundry Debtors 400.0
    debtors_ac = acc_repo.get_debtors_account()
    cr_debtors = [e for e in entries if e["account_id"] == debtors_ac.account_id and e["credit_amount"] == 400.0]
    assert len(cr_debtors) == 1
    assert debtors_ac.current_balance == debtors_ac_before - 400.0

    # NO self-cancelling Cash entry (Cash credited 400)
    cr_cash = [e for e in entries if e["account_id"] == cash_ac.account_id and e["credit_amount"] > 0]
    assert len(cr_cash) == 0

    # Customer balance reduced from 1000 to 600
    cust = master_repo.get_customer_by_id(acc_env["cust_id"])
    assert cust.current_balance == 600.0


# ---------------------------------------------------------------------------
# Test 6: Supplier Payment Accounting
# ---------------------------------------------------------------------------
def test_06_supplier_payment_accounting(acc_env):
    """
    Supplier payment must:
    - Debit Sundry Creditors
    - Credit Bank Account (or Cash)
    - Eliminate self-cancelling cash entry
    - Reduce supplier payable balance
    """
    acc_svc = acc_env["acc_svc"]
    acc_repo = acc_env["acc_repo"]
    master_repo = acc_env["master_repo"]

    # Pre-condition: Set supplier balance to 5000.0
    master_repo.update_supplier_balance(acc_env["supp_id"], 5000.0)
    creditors_ac_before = acc_repo.get_creditors_account().current_balance
    bank_ac_before = acc_repo.get_bank_account().current_balance

    vch_id = acc_svc.record_supplier_payment(
        supplier_id=acc_env["supp_id"],
        payment_date="2026-10-06",
        amount=2000.0,
        payment_mode="BANK_TRANSFER",
        reference_no="NEFT-991188",
        narration="Part payment to distributor",
    )
    assert vch_id > 0

    entries = acc_env["db"].fetch_all("SELECT * FROM ledger_entries WHERE voucher_id = ?;", (vch_id,))
    assert len(entries) == 2

    # Dr Sundry Creditors 2000.0
    creditors_ac = acc_repo.get_creditors_account()
    dr_cred = [e for e in entries if e["account_id"] == creditors_ac.account_id and e["debit_amount"] == 2000.0]
    assert len(dr_cred) == 1
    assert creditors_ac.current_balance == creditors_ac_before - 2000.0

    # Cr Bank Account 2000.0
    bank_ac = acc_repo.get_bank_account()
    cr_bank = [e for e in entries if e["account_id"] == bank_ac.account_id and e["credit_amount"] == 2000.0]
    assert len(cr_bank) == 1
    assert bank_ac.current_balance == bank_ac_before - 2000.0

    # NO self-cancelling Bank entry
    dr_bank = [e for e in entries if e["account_id"] == bank_ac.account_id and e["debit_amount"] > 0]
    assert len(dr_bank) == 0

    # Supplier balance reduced from 5000 to 3000
    supp = master_repo.get_supplier_by_id(acc_env["supp_id"])
    assert supp.current_balance == 3000.0


# ---------------------------------------------------------------------------
# Test 7: Shop Expense Accounting
# ---------------------------------------------------------------------------
def test_07_shop_expense_accounting(acc_env):
    """
    Shop expense must:
    - Debit Operating Expenses (or category expense account)
    - Credit Cash in Hand (or Bank)
    - Reduce cash balance
    - Record in expenses table
    """
    acc_svc = acc_env["acc_svc"]
    acc_repo = acc_env["acc_repo"]

    cash_ac_before = acc_repo.get_cash_account().current_balance

    exp = Expense(
        category_id=1,  # Shop Rent
        expense_date="2026-10-07",
        amount=1200.0,
        remarks="October Shop Rent",
    )
    exp_id = acc_svc.record_shop_expense(exp)
    assert exp_id > 0

    vch = acc_env["db"].fetch_one("SELECT * FROM vouchers WHERE reference_type = 'EXPENSE' AND reference_id = ?;", (exp_id,))
    assert vch is not None
    entries = acc_env["db"].fetch_all("SELECT * FROM ledger_entries WHERE voucher_id = ?;", (vch["voucher_id"],))

    tot_dr = sum(e["debit_amount"] for e in entries)
    tot_cr = sum(e["credit_amount"] for e in entries)
    assert round(tot_dr, 2) == round(tot_cr, 2) == 1200.0

    # Dr Expense Account 1200.0
    exp_ac = acc_repo.get_expense_account("Shop Rent")
    dr_exp = [e for e in entries if e["account_id"] == exp_ac.account_id]
    assert len(dr_exp) == 1
    assert dr_exp[0]["debit_amount"] == 1200.0

    # Cr Cash in Hand 1200.0
    cash_ac = acc_repo.get_cash_account()
    cr_cash = [e for e in entries if e["account_id"] == cash_ac.account_id]
    assert len(cr_cash) == 1
    assert cr_cash[0]["credit_amount"] == 1200.0
    assert cash_ac.current_balance == cash_ac_before - 1200.0


# ---------------------------------------------------------------------------
# Test 8: Cash and Bank Balance Movements Verified via Ledger Statements
# ---------------------------------------------------------------------------
def test_08_cash_and_bank_balance_movements(acc_env):
    """
    Verify chronological ledger statements for Cash and Bank match exact transactions:
    Ending Balance == Opening Balance + sum(Debits) - sum(Credits).
    """
    sales_svc = acc_env["sales_svc"]
    acc_svc = acc_env["acc_svc"]
    acc_repo = acc_env["acc_repo"]

    cash_ac = acc_repo.get_cash_account()
    bank_ac = acc_repo.get_bank_account()

    # 1. Cash sale: +500 Cash
    sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-MIX-01",
            sale_date="2026-10-08",
            customer_id=acc_env["cust_id"],
            payment_mode="CASH",
            total_taxable=500.0,
            net_amount=500.0,
            paid_amount=500.0,
            items=[SaleItem(product_id=acc_env["prod_id"], batch_id=acc_env["batch_id"], qty=1.0, sale_rate=500.0, mrp=550.0, taxable_amount=500.0, total_amount=500.0)],
        )
    )

    # 2. Bank customer receipt: +800 Bank
    acc_svc.record_customer_receipt(
        customer_id=acc_env["cust_id"],
        receipt_date="2026-10-08",
        amount=800.0,
        payment_mode="BANK_TRANSFER",
    )

    # 3. Cash expense: -200 Cash
    acc_svc.record_shop_expense(
        Expense(category_id=5, expense_date="2026-10-08", amount=200.0, remarks="Tea & Water")
    )

    # 4. Bank supplier payment: -600 Bank
    acc_env["master_repo"].update_supplier_balance(acc_env["supp_id"], 1000.0)
    acc_svc.record_supplier_payment(
        supplier_id=acc_env["supp_id"],
        payment_date="2026-10-08",
        amount=600.0,
        payment_mode="BANK_TRANSFER",
    )

    # Verify Cash ledger statement
    cash_stmt = acc_repo.get_account_ledger_statement(cash_ac.account_id)
    cash_dr = sum(row["debit_amount"] for row in cash_stmt)
    cash_cr = sum(row["credit_amount"] for row in cash_stmt)
    assert cash_dr - cash_cr == 300.0  # +500 - 200
    assert acc_repo.get_cash_account().current_balance == cash_ac.opening_balance + (cash_dr - cash_cr)

    # Verify Bank ledger statement
    bank_stmt = acc_repo.get_account_ledger_statement(bank_ac.account_id)
    bank_dr = sum(row["debit_amount"] for row in bank_stmt)
    bank_cr = sum(row["credit_amount"] for row in bank_stmt)
    assert bank_dr - bank_cr == 200.0  # +800 - 600
    assert acc_repo.get_bank_account().current_balance == bank_ac.opening_balance + (bank_dr - bank_cr)


# ---------------------------------------------------------------------------
# Test 9: Customer Outstanding Reconciliation
# ---------------------------------------------------------------------------
def test_09_customer_outstanding_reconciliation(acc_env):
    """
    Customer Khata outstanding must perfectly match:
    sum(Credit Sales Dues) - sum(Customer Receipts).
    Sundry Debtors ledger must reflect the exact net receivables.
    """
    sales_svc = acc_env["sales_svc"]
    acc_svc = acc_env["acc_svc"]
    acc_repo = acc_env["acc_repo"]
    master_repo = acc_env["master_repo"]

    debtors_ac_start = acc_repo.get_debtors_account().current_balance

    # Credit Sale 1: 1000 due
    sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-RECON-01",
            sale_date="2026-10-09",
            customer_id=acc_env["cust_id"],
            payment_mode="CREDIT",
            total_taxable=1000.0,
            net_amount=1000.0,
            paid_amount=0.0,
            items=[SaleItem(product_id=acc_env["prod_id"], batch_id=acc_env["batch_id"], qty=2.0, sale_rate=500.0, mrp=550.0, taxable_amount=1000.0, total_amount=1000.0)],
        )
    )

    # Credit Sale 2: 600 due
    sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-RECON-02",
            sale_date="2026-10-10",
            customer_id=acc_env["cust_id"],
            payment_mode="CREDIT",
            total_taxable=600.0,
            net_amount=600.0,
            paid_amount=0.0,
            items=[SaleItem(product_id=acc_env["prod_id"], batch_id=acc_env["batch_id"], qty=1.0, sale_rate=600.0, mrp=650.0, taxable_amount=600.0, total_amount=600.0)],
        )
    )

    # Receipt 1: 400 paid
    acc_svc.record_customer_receipt(
        customer_id=acc_env["cust_id"],
        receipt_date="2026-10-11",
        amount=400.0,
    )

    # Receipt 2: 300 paid
    acc_svc.record_customer_receipt(
        customer_id=acc_env["cust_id"],
        receipt_date="2026-10-12",
        amount=300.0,
    )

    # Expected balance = (1000 + 600) - (400 + 300) = 900.0
    cust = master_repo.get_customer_by_id(acc_env["cust_id"])
    assert cust.current_balance == 900.0

    debtors_ac = acc_repo.get_debtors_account()
    assert debtors_ac.current_balance == debtors_ac_start + 900.0


# ---------------------------------------------------------------------------
# Test 10: Supplier Outstanding Reconciliation
# ---------------------------------------------------------------------------
def test_10_supplier_outstanding_reconciliation(acc_env):
    """
    Supplier payable outstanding must perfectly match:
    sum(Credit Purchases Dues) - sum(Supplier Payments).
    Sundry Creditors ledger must reflect the exact net payables.
    """
    purchase_svc = acc_env["purchase_svc"]
    acc_svc = acc_env["acc_svc"]
    acc_repo = acc_env["acc_repo"]
    master_repo = acc_env["master_repo"]

    creditors_ac_start = acc_repo.get_creditors_account().current_balance

    # Credit Purchase 1: 4000 due
    purchase_svc.process_inward_purchase(
        Purchase(
            invoice_no="PUR-RECON-01",
            purchase_date="2026-10-09",
            supplier_id=acc_env["supp_id"],
            payment_type="CREDIT",
            total_taxable=4000.0,
            net_amount=4000.0,
            paid_amount=0.0,
            due_amount=4000.0,
            items=[PurchaseItem(product_id=acc_env["prod_id"], batch_no="B-REC-1", qty=40.0, purchase_rate=100.0, sale_rate=150.0, mrp=160.0, taxable_amount=4000.0, total_amount=4000.0)],
        )
    )

    # Credit Purchase 2: 2500 due
    purchase_svc.process_inward_purchase(
        Purchase(
            invoice_no="PUR-RECON-02",
            purchase_date="2026-10-10",
            supplier_id=acc_env["supp_id"],
            payment_type="CREDIT",
            total_taxable=2500.0,
            net_amount=2500.0,
            paid_amount=0.0,
            due_amount=2500.0,
            items=[PurchaseItem(product_id=acc_env["prod_id"], batch_no="B-REC-2", qty=25.0, purchase_rate=100.0, sale_rate=150.0, mrp=160.0, taxable_amount=2500.0, total_amount=2500.0)],
        )
    )

    # Payment 1: 1500 paid
    acc_svc.record_supplier_payment(
        supplier_id=acc_env["supp_id"],
        payment_date="2026-10-11",
        amount=1500.0,
        payment_mode="BANK_TRANSFER",
    )

    # Payment 2: 1000 paid
    acc_svc.record_supplier_payment(
        supplier_id=acc_env["supp_id"],
        payment_date="2026-10-12",
        amount=1000.0,
        payment_mode="CASH",
    )

    # Expected payable = (4000 + 2500) - (1500 + 1000) = 4000.0
    supp = master_repo.get_supplier_by_id(acc_env["supp_id"])
    assert supp.current_balance == 4000.0

    creditors_ac = acc_repo.get_creditors_account()
    assert creditors_ac.current_balance == creditors_ac_start + 4000.0


# ---------------------------------------------------------------------------
# Test 11: Double-Entry Balance Integrity for Every Voucher
# ---------------------------------------------------------------------------
def test_11_double_entry_balance_for_all_vouchers(acc_env):
    """
    Every single voucher created in the database MUST satisfy:
    round(sum(debit_amount), 2) == round(sum(credit_amount), 2) == total_amount.
    """
    sales_svc = acc_env["sales_svc"]
    purchase_svc = acc_env["purchase_svc"]
    acc_svc = acc_env["acc_svc"]
    db = acc_env["db"]

    # 1. Cash Sale
    sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-BAL-01",
            sale_date="2026-10-10",
            customer_id=acc_env["cust_id"],
            payment_mode="CASH",
            total_taxable=847.46,
            total_cgst=76.27,
            total_sgst=76.27,
            net_amount=1000.0,
            paid_amount=1000.0,
            items=[SaleItem(product_id=acc_env["prod_id"], batch_id=acc_env["batch_id"], qty=5.0, sale_rate=200.0, mrp=220.0, taxable_amount=847.46, cgst_rate=9.0, cgst_amount=76.27, sgst_rate=9.0, sgst_amount=76.27, total_amount=1000.0)],
        )
    )

    # 2. Credit Purchase
    purchase_svc.process_inward_purchase(
        Purchase(
            invoice_no="PUR-BAL-01",
            purchase_date="2026-10-10",
            supplier_id=acc_env["supp_id"],
            payment_type="CREDIT",
            total_taxable=1500.0,
            total_cgst=135.0,
            total_sgst=135.0,
            net_amount=1770.0,
            paid_amount=0.0,
            due_amount=1770.0,
            items=[PurchaseItem(product_id=acc_env["prod_id"], batch_no="B-BAL-1", qty=15.0, purchase_rate=100.0, sale_rate=150.0, mrp=160.0, taxable_amount=1500.0, cgst_rate=9.0, cgst_amount=135.0, sgst_rate=9.0, sgst_amount=135.0, total_amount=1770.0)],
        )
    )

    # 3. Customer Receipt
    acc_svc.record_customer_receipt(customer_id=acc_env["cust_id"], receipt_date="2026-10-10", amount=500.0)

    # 4. Supplier Payment
    acc_svc.record_supplier_payment(supplier_id=acc_env["supp_id"], payment_date="2026-10-10", amount=700.0)

    # 5. Shop Expense
    acc_svc.record_shop_expense(Expense(category_id=1, expense_date="2026-10-10", amount=350.0))

    vouchers = db.fetch_all("SELECT * FROM vouchers;")
    assert len(vouchers) >= 5

    for v in vouchers:
        vid = v["voucher_id"]
        vch_total = round(v["total_amount"], 2)
        entries = db.fetch_all("SELECT debit_amount, credit_amount FROM ledger_entries WHERE voucher_id = ?;", (vid,))
        assert len(entries) >= 2, f"Voucher {v['voucher_no']} has fewer than 2 entries"

        tot_dr = round(sum(e["debit_amount"] for e in entries), 2)
        tot_cr = round(sum(e["credit_amount"] for e in entries), 2)

        assert tot_dr == tot_cr, f"Unbalanced voucher {v['voucher_no']}: Dr {tot_dr} != Cr {tot_cr}"
        assert tot_dr == vch_total, f"Voucher {v['voucher_no']} total {vch_total} != entries sum {tot_dr}"


# ---------------------------------------------------------------------------
# Test 12: Atomic Transaction Rollback on Failure
# ---------------------------------------------------------------------------
def test_12_atomic_transaction_rollback_on_failure(acc_env):
    """
    If voucher creation or entry validation fails mid-transaction:
    - Entire transaction rolls back cleanly.
    - No orphaned voucher, ledger entries, or customer balance changes persist.
    """
    acc_repo = acc_env["acc_repo"]
    master_repo = acc_env["master_repo"]

    cust_before = master_repo.get_customer_by_id(acc_env["cust_id"]).current_balance
    vch_count_before = acc_env["db"].fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"]

    # Attempt to post an intentionally unbalanced voucher
    unbalanced_vch = Voucher(
        voucher_no="VCH-UNBAL-001",
        voucher_date="2026-10-13",
        voucher_type="JOURNAL",
        total_amount=500.0,
        narration="Broken unbalanced voucher",
        entries=[
            LedgerEntry(account_id=1, debit_amount=500.0, credit_amount=0.0, particulars="Debit only"),
            LedgerEntry(account_id=2, debit_amount=0.0, credit_amount=300.0, particulars="Incomplete credit"),
        ],
    )

    with pytest.raises(ValueError, match="Unbalanced voucher entries"):
        with acc_env["db"].transaction() as conn:
            master_repo.update_customer_balance(acc_env["cust_id"], 500.0, conn=conn)
            acc_repo.create_voucher(unbalanced_vch, conn=conn)

    # Verify rollback
    cust_after = master_repo.get_customer_by_id(acc_env["cust_id"]).current_balance
    assert cust_after == cust_before

    vch_count_after = acc_env["db"].fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"]
    assert vch_count_after == vch_count_before


# ---------------------------------------------------------------------------
# Test 13: Duplicate Voucher Prevention
# ---------------------------------------------------------------------------
def test_13_duplicate_voucher_prevention(acc_env):
    """
    Voucher numbers must be unique:
    Attempting to insert a duplicate voucher_no raises sqlite3.IntegrityError.
    """
    acc_repo = acc_env["acc_repo"]

    vch1 = Voucher(
        voucher_no="VCH-UNIQUE-001",
        voucher_date="2026-10-14",
        voucher_type="JOURNAL",
        total_amount=100.0,
        narration="First voucher",
        entries=[
            LedgerEntry(account_id=1, debit_amount=100.0, credit_amount=0.0, particulars="Dr"),
            LedgerEntry(account_id=2, debit_amount=0.0, credit_amount=100.0, particulars="Cr"),
        ],
    )
    vid1 = acc_repo.create_voucher(vch1)
    assert vid1 > 0

    vch_dup = Voucher(
        voucher_no="VCH-UNIQUE-001",  # Same voucher_no
        voucher_date="2026-10-14",
        voucher_type="JOURNAL",
        total_amount=100.0,
        narration="Duplicate voucher",
        entries=[
            LedgerEntry(account_id=1, debit_amount=100.0, credit_amount=0.0, particulars="Dr"),
            LedgerEntry(account_id=2, debit_amount=0.0, credit_amount=100.0, particulars="Cr"),
        ],
    )

    with pytest.raises(sqlite3.IntegrityError):
        acc_repo.create_voucher(vch_dup)


# ---------------------------------------------------------------------------
# Test 14: Idempotent System-Account Migration v3
# ---------------------------------------------------------------------------
def test_14_idempotent_system_account_migration(tmp_path):
    """
    Running _migrate_v3_core_ledger_accounts multiple times:
    - Adds Sundry Debtors, Sundry Creditors, Operating Expenses if missing.
    - Leaves existing accounts untouched with zero duplicate name errors.
    """
    test_db = tmp_path / "test_mig_v3.db"
    db = DatabaseManager(db_path=test_db, clean_install=True)
    db.initialize_database(include_seed=True)

    # Initial state after initialize_database (runs v1, v2, v3)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT account_name, account_group FROM ledger_accounts ORDER BY account_id ASC;")
        accounts_first = cur.fetchall()
        names_first = [a[0] for a in accounts_first]
        assert "Sundry Debtors" in names_first
        assert "Sundry Creditors" in names_first
        assert "Operating Expenses" in names_first

        # Re-run migration handler directly on connection
        _migrate_v3_core_ledger_accounts(conn)
        _migrate_v3_core_ledger_accounts(conn)

        cur.execute("SELECT account_name, account_group FROM ledger_accounts ORDER BY account_id ASC;")
        accounts_second = cur.fetchall()
        assert len(accounts_second) == len(accounts_first)
        assert accounts_second == accounts_first
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 15: Existing Historical Transactions Remain Unchanged
# ---------------------------------------------------------------------------
def test_15_existing_historical_transactions_remain_unchanged(acc_env):
    """
    Verify that executing new transactions and accounting vouchers does not
    alter any historical sale, purchase, customer, or voucher rows.
    """
    db = acc_env["db"]

    # Record baseline state of all sales and purchases
    sales_before = db.fetch_all("SELECT * FROM sales;")
    purchases_before = db.fetch_all("SELECT * FROM purchases;")
    customers_before = {c["customer_id"]: c["current_balance"] for c in db.fetch_all("SELECT * FROM customers;")}

    # Perform a new customer receipt for a separate customer
    new_cust_id = acc_env["master_repo"].create_customer(
        Customer(customer_name="Suresh Shinde", mobile="9876543210", village="Daund", opening_balance=500.0)
    )
    acc_env["acc_svc"].record_customer_receipt(
        customer_id=new_cust_id,
        receipt_date="2026-10-15",
        amount=200.0,
    )

    # Verify historical sales unchanged
    sales_after = db.fetch_all("SELECT * FROM sales;")
    assert len(sales_after) == len(sales_before)
    for sb, sa in zip(sales_before, sales_after):
        assert dict(sb) == dict(sa)

    # Verify historical purchases unchanged
    purchases_after = db.fetch_all("SELECT * FROM purchases;")
    assert len(purchases_after) == len(purchases_before)
    for pb, pa in zip(purchases_before, purchases_after):
        assert dict(pb) == dict(pa)

    # Verify existing customer balances unchanged
    customers_after = {c["customer_id"]: c["current_balance"] for c in db.fetch_all("SELECT * FROM customers;")}
    for cid, bal in customers_before.items():
        assert customers_after[cid] == bal
