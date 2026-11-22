"""
Comprehensive Isolated Test Suite for Phase 2B-4:
Core Transaction Concurrency Hardening (C3).

Verifies against strictly isolated temporary SQLite databases:
1. Sales invoice creation uses BEGIN IMMEDIATE + 1 shared connection across all repo calls.
2. Inward purchase creation uses BEGIN IMMEDIATE + 1 shared connection across all repo calls.
3. Customer receipt uses BEGIN IMMEDIATE + 1 shared connection across all repo calls.
4. Supplier payment uses BEGIN IMMEDIATE + 1 shared connection across all repo calls.
5. Shop expense uses BEGIN IMMEDIATE + 1 shared connection across all repo calls.
6. Daily expense inherits single BEGIN IMMEDIATE transaction via delegation (no nested tx).
7. Direct stock addition uses BEGIN IMMEDIATE + 1 shared connection (product read inside tx).
8. DatabaseManager.transaction() default mode remains DEFERRED.
9. Controlled multithreaded concurrency for independent & stock-contending sales.
10. Controlled multithreaded concurrency for inward purchases.
11. Controlled multithreaded concurrency for mixed accounting (receipts, payments, expenses).
12. Forced-failure atomic rollback across sales, purchases, receipts, payments, expenses, direct stock.
"""
from __future__ import annotations

import inspect
import sqlite3
import threading
from typing import Dict, List

import pytest

from src.db.connection import DatabaseManager, reset_db_manager
from src.models.accounting import Expense
from src.models.inventory import StockBatch
from src.models.master_data import Customer, Product, Supplier
from src.models.purchase import Purchase, PurchaseItem
from src.models.sales import Sale, SaleItem
from src.repositories.accounting_repository import AccountingRepository
from src.repositories.inventory_repository import InventoryRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.services.accounting_service import AccountingService
from src.services.inventory_service import InventoryService
from src.services.purchase_service import PurchaseService
from src.services.sales_service import SalesService


@pytest.fixture
def c3_env(tmp_path, monkeypatch):
    """Provides an isolated temporary SQLite database and domain services."""
    db_path = tmp_path / "test_phase2b4_c3.db"
    bkp_dir = tmp_path / "test_phase2b4_backups"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    reset_db_manager()

    db = DatabaseManager(db_path=db_path, backup_dir=bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)

    master_repo = MasterDataRepository(db)
    inv_repo = InventoryRepository(db)
    acc_repo = AccountingRepository(db)
    sales_svc = SalesService(db)
    purchase_svc = PurchaseService(db)
    acc_svc = AccountingService(db)
    inv_svc = InventoryService(db)

    cust_id = master_repo.create_customer(
        Customer(customer_name="Prakash Jadhav", mobile="9822556677", village="Sangli", opening_balance=2000.0)
    )
    supp_id = master_repo.create_supplier(
        Supplier(supplier_name="UPL Agro Distributors", gstin="27AAACU1234F1Z7", city="Pune", opening_balance=3000.0)
    )
    prod_id = master_repo.create_product(
        Product(
            product_name="UPL Saaf Fungicide 500g",
            category_id=4,
            unit_id=3,
            tax_group_id=4,  # 18% GST
            default_purchase_rate=200.0,
            default_sale_rate=295.0,
            default_mrp=320.0,
        )
    )
    batch_id = inv_repo.upsert_batch(
        StockBatch(
            product_id=prod_id,
            batch_no="UPL-2026-01",
            mfg_date="2026-01-01",
            exp_date="2027-12-31",
            purchase_rate=200.0,
            mrp=320.0,
            sale_rate=295.0,
            current_qty=100.0,
        )
    )

    yield {
        "db": db,
        "master_repo": master_repo,
        "inv_repo": inv_repo,
        "acc_repo": acc_repo,
        "sales_svc": sales_svc,
        "purchase_svc": purchase_svc,
        "acc_svc": acc_svc,
        "inv_svc": inv_svc,
        "cust_id": cust_id,
        "supp_id": supp_id,
        "prod_id": prod_id,
        "batch_id": batch_id,
    }
    reset_db_manager()


def _attach_tx_and_conn_spies(db: DatabaseManager, monkeypatch):
    tx_modes: List[str] = []
    opened_conns: List[sqlite3.Connection] = []

    orig_tx = db.transaction
    orig_get_conn = db.get_connection

    def _spy_tx(mode="DEFERRED"):
        tx_modes.append(mode)
        return orig_tx(mode=mode)

    def _spy_get_conn(*args, **kwargs):
        c = orig_get_conn(*args, **kwargs)
        opened_conns.append(c)
        return c

    monkeypatch.setattr(db, "transaction", _spy_tx)
    monkeypatch.setattr(db, "get_connection", _spy_get_conn)
    return tx_modes, opened_conns


def _spy_repo_methods(monkeypatch, targets):
    seen: Dict[str, List[sqlite3.Connection]] = {}
    for obj, attr_name in targets:
        orig = getattr(obj, attr_name)
        seen[attr_name] = []

        def _make_wrapper(fn, key):
            def _wrapped(*args, **kwargs):
                seen[key].append(kwargs.get("conn"))
                return fn(*args, **kwargs)
            return _wrapped

        monkeypatch.setattr(obj, attr_name, _make_wrapper(orig, attr_name))
    return seen


def _assert_all_vouchers_balanced(db: DatabaseManager):
    unbalanced = db.fetch_all(
        """
        SELECT v.voucher_id, v.voucher_no,
               ROUND(SUM(le.debit_amount), 2) AS dr,
               ROUND(SUM(le.credit_amount), 2) AS cr
        FROM vouchers v
        JOIN ledger_entries le ON v.voucher_id = le.voucher_id
        GROUP BY v.voucher_id
        HAVING ABS(dr - cr) > 0.001;
        """
    )
    assert unbalanced == []


# ---------------------------------------------------------------------------
# 1–8. IMMEDIATE mode, single shared connection, and untouched default mode
# ---------------------------------------------------------------------------

def test_database_manager_default_transaction_mode_remains_deferred():
    sig = inspect.signature(DatabaseManager.transaction)
    assert sig.parameters["mode"].default is None


def test_sales_invoice_uses_immediate_and_single_shared_conn(c3_env, monkeypatch):
    db = c3_env["db"]
    sales_svc = c3_env["sales_svc"]

    tx_modes, opened_conns = _attach_tx_and_conn_spies(db, monkeypatch)
    seen = _spy_repo_methods(
        monkeypatch,
        [
            (sales_svc.inventory_repo, "get_or_create_batch_for_sale"),
            (sales_svc.inventory_repo, "deduct_batch_stock"),
            (sales_svc.sales_repo, "create_sale"),
            (sales_svc.inventory_repo, "get_product_total_stock"),
            (sales_svc.inventory_repo, "record_stock_movement"),
            (sales_svc.master_repo, "update_customer_balance"),
            (sales_svc.accounting_repo, "get_account_by_name"),
            (sales_svc.accounting_repo, "generate_next_voucher_no"),
            (sales_svc.accounting_repo, "create_voucher"),
        ],
    )

    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-IMM-01",
            sale_date="2026-11-17",
            customer_id=c3_env["cust_id"],
            payment_mode="CREDIT",
            total_taxable=500.0,
            total_cgst=45.0,
            total_sgst=45.0,
            net_amount=590.0,
            paid_amount=0.0,
            due_amount=590.0,
            items=[
                SaleItem(
                    product_id=c3_env["prod_id"],
                    batch_id=c3_env["batch_id"],
                    qty=2.0,
                    sale_rate=295.0,
                    mrp=320.0,
                    taxable_amount=500.0,
                    cgst_rate=9.0,
                    cgst_amount=45.0,
                    sgst_rate=9.0,
                    sgst_amount=45.0,
                    total_amount=590.0,
                )
            ],
        )
    )
    assert sale_id > 0
    assert tx_modes == ["IMMEDIATE"]
    assert len(opened_conns) == 1
    active = opened_conns[0]
    for name, conns in seen.items():
        assert len(conns) >= 1, f"{name} not called"
        assert all(c is active for c in conns), f"{name} did not use active conn"


def test_inward_purchase_uses_immediate_and_single_shared_conn(c3_env, monkeypatch):
    db = c3_env["db"]
    purchase_svc = c3_env["purchase_svc"]

    tx_modes, opened_conns = _attach_tx_and_conn_spies(db, monkeypatch)
    seen = _spy_repo_methods(
        monkeypatch,
        [
            (purchase_svc.purchase_repo, "create_purchase"),
            (purchase_svc.inventory_repo, "upsert_batch"),
            (purchase_svc.inventory_repo, "get_product_total_stock"),
            (purchase_svc.inventory_repo, "record_stock_movement"),
            (purchase_svc.master_repo, "update_supplier_balance"),
            (purchase_svc.accounting_repo, "get_account_by_name"),
            (purchase_svc.accounting_repo, "generate_next_voucher_no"),
            (purchase_svc.accounting_repo, "create_voucher"),
        ],
    )

    purchase_id = purchase_svc.process_inward_purchase(
        Purchase(
            supplier_id=c3_env["supp_id"],
            invoice_no="PUR-IMM-01",
            purchase_date="2026-11-17",
            payment_type="CREDIT",
            total_taxable=1000.0,
            total_cgst=90.0,
            total_sgst=90.0,
            net_amount=1180.0,
            paid_amount=0.0,
            due_amount=1180.0,
            items=[
                PurchaseItem(
                    product_id=c3_env["prod_id"],
                    batch_no="UPL-2026-02",
                    qty=5.0,
                    purchase_rate=200.0,
                    sale_rate=295.0,
                    mrp=320.0,
                    taxable_amount=1000.0,
                    cgst_rate=9.0,
                    cgst_amount=90.0,
                    sgst_rate=9.0,
                    sgst_amount=90.0,
                    total_amount=1180.0,
                )
            ],
        )
    )
    assert purchase_id > 0
    assert tx_modes == ["IMMEDIATE"]
    assert len(opened_conns) == 1
    active = opened_conns[0]
    for name, conns in seen.items():
        assert len(conns) >= 1, f"{name} not called"
        assert all(c is active for c in conns), f"{name} did not use active conn"


def test_customer_receipt_uses_immediate_and_single_shared_conn(c3_env, monkeypatch):
    db = c3_env["db"]
    acc_svc = c3_env["acc_svc"]

    tx_modes, opened_conns = _attach_tx_and_conn_spies(db, monkeypatch)
    seen = _spy_repo_methods(
        monkeypatch,
        [
            (acc_svc.accounting_repo, "get_account_by_name"),
            (acc_svc.master_repo, "update_customer_balance"),
            (acc_svc.accounting_repo, "generate_next_voucher_no"),
            (acc_svc.accounting_repo, "create_voucher"),
        ],
    )

    vch_id = acc_svc.record_customer_receipt(
        customer_id=c3_env["cust_id"],
        receipt_date="2026-11-17",
        amount=500.0,
        payment_mode="CASH",
    )
    assert vch_id > 0
    assert tx_modes == ["IMMEDIATE"]
    assert len(opened_conns) == 1
    active = opened_conns[0]
    for name, conns in seen.items():
        assert len(conns) >= 1
        assert all(c is active for c in conns)


def test_supplier_payment_uses_immediate_and_single_shared_conn(c3_env, monkeypatch):
    db = c3_env["db"]
    acc_svc = c3_env["acc_svc"]

    tx_modes, opened_conns = _attach_tx_and_conn_spies(db, monkeypatch)
    seen = _spy_repo_methods(
        monkeypatch,
        [
            (acc_svc.accounting_repo, "get_account_by_name"),
            (acc_svc.master_repo, "update_supplier_balance"),
            (acc_svc.accounting_repo, "generate_next_voucher_no"),
            (acc_svc.accounting_repo, "create_voucher"),
        ],
    )

    vch_id = acc_svc.record_supplier_payment(
        supplier_id=c3_env["supp_id"],
        amount=600.0,
        payment_date="2026-11-17",
        payment_mode="BANK_TRANSFER",
    )
    assert vch_id > 0
    assert tx_modes == ["IMMEDIATE"]
    assert len(opened_conns) == 1
    active = opened_conns[0]
    for name, conns in seen.items():
        assert len(conns) >= 1
        assert all(c is active for c in conns)


def test_shop_expense_and_daily_expense_use_single_immediate_conn(c3_env, monkeypatch):
    db = c3_env["db"]
    acc_svc = c3_env["acc_svc"]

    # 1. record_shop_expense
    tx_modes, opened_conns = _attach_tx_and_conn_spies(db, monkeypatch)
    seen = _spy_repo_methods(
        monkeypatch,
        [
            (acc_svc.accounting_repo, "get_account_by_name"),
            (acc_svc.accounting_repo, "create_expense"),
            (acc_svc.accounting_repo, "generate_next_voucher_no"),
            (acc_svc.accounting_repo, "create_voucher"),
        ],
    )

    exp_id_1 = acc_svc.record_shop_expense(
        Expense(category_id=1, expense_date="2026-11-17", amount=250.0, remarks="Shop rent")
    )
    assert exp_id_1 > 0
    assert tx_modes == ["IMMEDIATE"]
    assert len(opened_conns) == 1
    active_1 = opened_conns[0]
    for name, conns in seen.items():
        assert len(conns) >= 1
        assert all(c is active_1 for c in conns)

    # 2. record_daily_expense delegates cleanly to record_shop_expense (still strictly 1 IMMEDIATE tx & 1 conn)
    tx_modes.clear()
    opened_conns.clear()
    for lst in seen.values():
        lst.clear()

    exp_id_2 = acc_svc.record_daily_expense(
        category_id=2,
        amount=300.0,
        expense_date="2026-11-17",
        payment_mode="UPI",
        remarks="Electricity UPI",
    )
    assert exp_id_2 > 0
    assert tx_modes == ["IMMEDIATE"]
    assert len(opened_conns) == 1
    active_2 = opened_conns[0]
    for name, conns in seen.items():
        assert len(conns) >= 1
        assert all(c is active_2 for c in conns)


def test_add_direct_stock_uses_immediate_and_single_shared_conn(c3_env, monkeypatch):
    db = c3_env["db"]
    inv_svc = c3_env["inv_svc"]

    tx_modes, opened_conns = _attach_tx_and_conn_spies(db, monkeypatch)
    seen = _spy_repo_methods(
        monkeypatch,
        [
            (inv_svc.inv_repo, "upsert_batch"),
            (inv_svc.inv_repo, "record_stock_movement"),
        ],
    )

    res = inv_svc.add_direct_stock(
        product_id=c3_env["prod_id"],
        qty=15.0,
        batch_no="UPL-DIRECT-01",
    )
    assert res["success"] is True
    assert tx_modes == ["IMMEDIATE"]
    assert len(opened_conns) == 1
    active = opened_conns[0]
    for name, conns in seen.items():
        assert len(conns) == 1
        assert conns[0] is active


# ---------------------------------------------------------------------------
# 9. Controlled multithreaded concurrency: Sales (independent + stock contention)
# ---------------------------------------------------------------------------

def test_concurrent_independent_sales_complete_cleanly_without_lock_errors(c3_env):
    db = c3_env["db"]
    inv_repo = c3_env["inv_repo"]
    acc_repo = c3_env["acc_repo"]

    num_threads = 8
    barrier = threading.Barrier(num_threads)
    sale_ids: List[int] = []
    errors: List[Exception] = []
    lock = threading.Lock()

    def _worker(idx: int):
        svc = SalesService(db)
        sale = Sale(
            invoice_no="",  # Auto-generated inside IMMEDIATE transaction
            sale_date="2026-11-17",
            customer_id=c3_env["cust_id"],
            payment_mode="CASH",
            total_taxable=500.0,
            total_cgst=45.0,
            total_sgst=45.0,
            net_amount=590.0,
            paid_amount=590.0,
            due_amount=0.0,
            items=[
                SaleItem(
                    product_id=c3_env["prod_id"],
                    batch_id=c3_env["batch_id"],
                    qty=2.0,
                    sale_rate=295.0,
                    mrp=320.0,
                    taxable_amount=500.0,
                    cgst_rate=9.0,
                    cgst_amount=45.0,
                    sgst_rate=9.0,
                    sgst_amount=45.0,
                    total_amount=590.0,
                )
            ],
        )
        barrier.wait()
        try:
            sid = svc.process_sales_invoice(sale)
            with lock:
                sale_ids.append(sid)
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert len(sale_ids) == num_threads
    assert len(set(sale_ids)) == num_threads

    # Verify exact DB state: 8 sales, 8 unique invoices, 8 balanced vouchers, stock 100 - 16 = 84
    assert db.fetch_one("SELECT COUNT(*) as c FROM sales;")["c"] == num_threads
    assert db.fetch_one("SELECT COUNT(DISTINCT invoice_no) as c FROM sales;")["c"] == num_threads
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers WHERE reference_type = 'SALE';")["c"] == num_threads
    assert inv_repo.get_batch_by_id(c3_env["batch_id"]).current_qty == pytest.approx(84.0)
    assert acc_repo.get_cash_account().current_balance == pytest.approx(num_threads * 590.0)
    _assert_all_vouchers_balanced(db)


def test_concurrent_sales_on_scarce_batch_prevent_negative_stock(c3_env):
    """6 threads simultaneously try to sell 4 units from a 10-unit batch -> strictly 2 succeed, 4 rejected."""
    db = c3_env["db"]
    inv_repo = c3_env["inv_repo"]
    scarce_batch_id = inv_repo.upsert_batch(
        StockBatch(
            product_id=c3_env["prod_id"],
            batch_no="UPL-SCARCE-10",
            purchase_rate=200.0,
            mrp=320.0,
            sale_rate=295.0,
            current_qty=10.0,
        )
    )

    num_threads = 6
    barrier = threading.Barrier(num_threads)
    successes: List[int] = []
    failures: List[Exception] = []
    lock = threading.Lock()

    def _worker():
        svc = SalesService(db)
        sale = Sale(
            invoice_no="",
            sale_date="2026-11-17",
            customer_id=c3_env["cust_id"],
            payment_mode="CASH",
            total_taxable=1000.0,
            total_cgst=90.0,
            total_sgst=90.0,
            net_amount=1180.0,
            paid_amount=1180.0,
            due_amount=0.0,
            items=[
                SaleItem(
                    product_id=c3_env["prod_id"],
                    batch_id=scarce_batch_id,
                    qty=4.0,
                    sale_rate=295.0,
                    mrp=320.0,
                    taxable_amount=1000.0,
                    cgst_rate=9.0,
                    cgst_amount=90.0,
                    sgst_rate=9.0,
                    sgst_amount=90.0,
                    total_amount=1180.0,
                )
            ],
        )
        barrier.wait()
        try:
            sid = svc.process_sales_invoice(sale)
            with lock:
                successes.append(sid)
        except Exception as exc:
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(successes) == 2
    assert len(failures) == 4
    assert all(isinstance(e, ValueError) and "Insufficient" in str(e) for e in failures)
    assert inv_repo.get_batch_by_id(scarce_batch_id).current_qty == pytest.approx(2.0)
    assert db.fetch_one("SELECT COUNT(*) as c FROM sales;")["c"] == 2
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers WHERE reference_type = 'SALE';")["c"] == 2
    _assert_all_vouchers_balanced(db)


# ---------------------------------------------------------------------------
# 10. Controlled multithreaded concurrency: Purchases
# ---------------------------------------------------------------------------

def test_concurrent_purchases_complete_cleanly_without_lock_errors(c3_env):
    db = c3_env["db"]
    inv_repo = c3_env["inv_repo"]
    master_repo = c3_env["master_repo"]
    acc_repo = c3_env["acc_repo"]

    num_threads = 8
    barrier = threading.Barrier(num_threads)
    purchase_ids: List[int] = []
    errors: List[Exception] = []
    lock = threading.Lock()

    def _worker(idx: int):
        svc = PurchaseService(db)
        pur = Purchase(
            supplier_id=c3_env["supp_id"],
            invoice_no=f"PUR-CONC-{idx:02d}",
            purchase_date="2026-11-17",
            payment_type="CREDIT",
            total_taxable=1000.0,
            total_cgst=90.0,
            total_sgst=90.0,
            net_amount=1180.0,
            paid_amount=0.0,
            due_amount=1180.0,
            items=[
                PurchaseItem(
                    product_id=c3_env["prod_id"],
                    batch_no="UPL-2026-01",  # Concurrent upsert on same batch!
                    qty=5.0,
                    purchase_rate=200.0,
                    sale_rate=295.0,
                    mrp=320.0,
                    taxable_amount=1000.0,
                    cgst_rate=9.0,
                    cgst_amount=90.0,
                    sgst_rate=9.0,
                    sgst_amount=90.0,
                    total_amount=1180.0,
                )
            ],
        )
        barrier.wait()
        try:
            pid = svc.process_inward_purchase(pur)
            with lock:
                purchase_ids.append(pid)
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert len(purchase_ids) == num_threads
    assert db.fetch_one("SELECT COUNT(*) as c FROM purchases;")["c"] == num_threads
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers WHERE reference_type = 'PURCHASE';")["c"] == num_threads
    assert inv_repo.get_batch_by_id(c3_env["batch_id"]).current_qty == pytest.approx(100.0 + num_threads * 5.0)
    assert master_repo.get_supplier_by_id(c3_env["supp_id"]).current_balance == pytest.approx(
        3000.0 + num_threads * 1180.0
    )
    assert acc_repo.get_creditors_account().current_balance == pytest.approx(num_threads * 1180.0)
    _assert_all_vouchers_balanced(db)


# ---------------------------------------------------------------------------
# 11. Controlled multithreaded concurrency: Mixed Accounting Operations
# ---------------------------------------------------------------------------

def test_concurrent_accounting_operations_preserve_exact_balances_and_unique_vouchers(c3_env):
    """
    12 concurrent threads at Barrier:
    - 4 customer receipts of 100 CASH (+400 Cash, -400 Customer/Debtors)
    - 4 supplier payments of 150 BANK_TRANSFER (-600 Bank, -600 Supplier/Creditors)
    - 2 daily expenses of 50 CASH (-100 Cash, +100 Operating Expenses)
    - 2 daily expenses of 75 UPI (-150 Bank, +150 Operating Expenses)
    """
    db = c3_env["db"]
    master_repo = c3_env["master_repo"]
    acc_repo = c3_env["acc_repo"]

    tasks = (
        [("receipt", i) for i in range(4)]
        + [("payment", i) for i in range(4)]
        + [("exp_cash", i) for i in range(2)]
        + [("exp_upi", i) for i in range(2)]
    )
    barrier = threading.Barrier(len(tasks))
    errors: List[Exception] = []
    lock = threading.Lock()

    def _worker(kind: str, idx: int):
        svc = AccountingService(db)
        barrier.wait()
        try:
            if kind == "receipt":
                svc.record_customer_receipt(
                    customer_id=c3_env["cust_id"],
                    receipt_date="2026-11-17",
                    amount=100.0,
                    payment_mode="CASH",
                    narration=f"Concurrent receipt {idx}",
                )
            elif kind == "payment":
                svc.record_supplier_payment(
                    supplier_id=c3_env["supp_id"],
                    amount=150.0,
                    payment_date="2026-11-17",
                    payment_mode="BANK_TRANSFER",
                    reference_no=f"UTR-{idx}",
                )
            elif kind == "exp_cash":
                svc.record_daily_expense(
                    category_id=5,
                    amount=50.0,
                    expense_date="2026-11-17",
                    payment_mode="CASH",
                    remarks=f"Cash exp {idx}",
                )
            elif kind == "exp_upi":
                svc.record_daily_expense(
                    category_id=2,
                    amount=75.0,
                    expense_date="2026-11-17",
                    payment_mode="UPI",
                    remarks=f"UPI exp {idx}",
                )
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=t) for t in tasks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"] == 12
    assert db.fetch_one("SELECT COUNT(DISTINCT voucher_no) as c FROM vouchers;")["c"] == 12
    assert db.fetch_one("SELECT COUNT(*) as c FROM expenses;")["c"] == 4
    _assert_all_vouchers_balanced(db)

    # Exact final balances across master tables & general ledger
    assert master_repo.get_customer_by_id(c3_env["cust_id"]).current_balance == pytest.approx(2000.0 - 400.0)
    assert master_repo.get_supplier_by_id(c3_env["supp_id"]).current_balance == pytest.approx(3000.0 - 600.0)
    assert acc_repo.get_cash_account().current_balance == pytest.approx(400.0 - 100.0)
    assert acc_repo.get_bank_account().current_balance == pytest.approx(-600.0 - 150.0)
    assert acc_repo.get_expense_account().current_balance == pytest.approx(100.0 + 150.0)


# ---------------------------------------------------------------------------
# 12. Forced-failure atomic rollback across every hardened workflow
# ---------------------------------------------------------------------------

def test_forced_failures_roll_back_all_hardened_workflows_atomically(c3_env, monkeypatch):
    db = c3_env["db"]
    sales_svc = c3_env["sales_svc"]
    purchase_svc = c3_env["purchase_svc"]
    acc_svc = c3_env["acc_svc"]
    inv_svc = c3_env["inv_svc"]
    inv_repo = c3_env["inv_repo"]
    master_repo = c3_env["master_repo"]

    def _boom(*args, **kwargs):
        raise RuntimeError("Forced transactional failure")

    # 1. Sales invoice rollback
    monkeypatch.setattr(sales_svc.accounting_repo, "create_voucher", _boom)
    with pytest.raises(RuntimeError, match="Forced transactional failure"):
        sales_svc.process_sales_invoice(
            Sale(
                invoice_no="INV-RB-1",
                sale_date="2026-11-17",
                customer_id=c3_env["cust_id"],
                payment_mode="CREDIT",
                total_taxable=500.0,
                total_cgst=45.0,
                total_sgst=45.0,
                net_amount=590.0,
                paid_amount=0.0,
                due_amount=590.0,
                items=[
                    SaleItem(
                        product_id=c3_env["prod_id"],
                        batch_id=c3_env["batch_id"],
                        qty=2.0,
                        sale_rate=295.0,
                        mrp=320.0,
                        taxable_amount=500.0,
                        cgst_rate=9.0,
                        cgst_amount=45.0,
                        sgst_rate=9.0,
                        sgst_amount=45.0,
                        total_amount=590.0,
                    )
                ],
            )
        )
    assert db.fetch_one("SELECT COUNT(*) as c FROM sales;")["c"] == 0
    assert inv_repo.get_batch_by_id(c3_env["batch_id"]).current_qty == pytest.approx(100.0)
    assert master_repo.get_customer_by_id(c3_env["cust_id"]).current_balance == pytest.approx(2000.0)

    # 2. Inward purchase rollback
    monkeypatch.setattr(purchase_svc.accounting_repo, "create_voucher", _boom)
    with pytest.raises(RuntimeError, match="Forced transactional failure"):
        purchase_svc.process_inward_purchase(
            Purchase(
                supplier_id=c3_env["supp_id"],
                invoice_no="PUR-RB-1",
                purchase_date="2026-11-17",
                payment_type="CREDIT",
                total_taxable=1000.0,
                total_cgst=90.0,
                total_sgst=90.0,
                net_amount=1180.0,
                paid_amount=0.0,
                due_amount=1180.0,
                items=[
                    PurchaseItem(
                        product_id=c3_env["prod_id"],
                        batch_no="UPL-2026-01",
                        qty=5.0,
                        purchase_rate=200.0,
                        sale_rate=295.0,
                        mrp=320.0,
                        taxable_amount=1000.0,
                        cgst_rate=9.0,
                        cgst_amount=90.0,
                        sgst_rate=9.0,
                        sgst_amount=90.0,
                        total_amount=1180.0,
                    )
                ],
            )
        )
    assert db.fetch_one("SELECT COUNT(*) as c FROM purchases;")["c"] == 0
    assert inv_repo.get_batch_by_id(c3_env["batch_id"]).current_qty == pytest.approx(100.0)
    assert master_repo.get_supplier_by_id(c3_env["supp_id"]).current_balance == pytest.approx(3000.0)

    # 3. Customer receipt, supplier payment, and daily/shop expense rollback
    monkeypatch.setattr(acc_svc.accounting_repo, "create_voucher", _boom)
    with pytest.raises(RuntimeError):
        acc_svc.record_customer_receipt(c3_env["cust_id"], "2026-11-17", 400.0, "CASH")
    assert master_repo.get_customer_by_id(c3_env["cust_id"]).current_balance == pytest.approx(2000.0)

    with pytest.raises(RuntimeError):
        acc_svc.record_supplier_payment(c3_env["supp_id"], amount=500.0, payment_date="2026-11-17")
    assert master_repo.get_supplier_by_id(c3_env["supp_id"]).current_balance == pytest.approx(3000.0)

    with pytest.raises(RuntimeError):
        acc_svc.record_daily_expense(category_id=1, amount=200.0, expense_date="2026-11-17", payment_mode="CASH")
    assert db.fetch_one("SELECT COUNT(*) as c FROM expenses;")["c"] == 0

    # 4. Direct stock addition rollback
    monkeypatch.setattr(inv_svc.inv_repo, "record_stock_movement", _boom)
    with pytest.raises(RuntimeError):
        inv_svc.add_direct_stock(product_id=c3_env["prod_id"], qty=25.0, batch_no="UPL-2026-01")
    assert inv_repo.get_batch_by_id(c3_env["batch_id"]).current_qty == pytest.approx(100.0)
    assert db.fetch_one("SELECT COUNT(*) as c FROM stock_ledger;")["c"] == 0
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"] == 0
