"""
Tests for Phase 2B-1: Shared SQLite Transaction Connections (C1 & C2).

Verifies:
1. create_expense() and generate_next_voucher_no() work with a caller-supplied connection
   without committing, rolling back, or closing the caller-owned connection.
2. Both methods preserve standalone behavior when no connection is supplied.
3. A caller rollback also rolls back the expense insert (both repository-level and
   inside AccountingService.record_shop_expense()).
4. Voucher-number generation sees uncommitted voucher inserts on the supplied connection.
5. Multiple voucher generations + inserts inside one transaction do not produce duplicate numbers.
6. All transactional services (sales, sales returns, purchases, purchase returns,
   customer receipts, supplier payments, shop expenses) pass their active transaction
   connection to generate_next_voucher_no() / create_expense().
7. No extra database connection is opened when a caller connection is supplied.
"""
from __future__ import annotations

from decimal import Decimal
import sqlite3
import uuid
import pytest

from src.db.connection import DatabaseManager, reset_db_manager
from src.models.accounting import Expense, LedgerEntry, Voucher
from src.models.inventory import StockBatch
from src.models.master_data import Customer, Product, Supplier
from src.models.purchase import Purchase, PurchaseItem
from src.models.purchase_return import (
    PurchaseReturnCreate,
    PurchaseReturnItemCreate,
    PurchaseReturnSettlementCreate,
)
from src.models.sales import Sale, SaleItem, SalesReturn, SalesReturnItem
from src.repositories.accounting_repository import AccountingRepository
from src.repositories.inventory_repository import InventoryRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.services.accounting_service import AccountingService
from src.services.purchase_return_service import PurchaseReturnService
from src.services.purchase_service import PurchaseService
from src.services.sales_service import SalesService


@pytest.fixture
def tx_env(tmp_path, monkeypatch):
    """Provides a strictly isolated temporary SQLite database and services."""
    test_db_path = tmp_path / "test_shared_tx_conn.db"
    test_bkp_dir = tmp_path / "test_shared_tx_backups"
    monkeypatch.setenv("DB_PATH", str(test_db_path))
    monkeypatch.setenv("BACKUP_DIR", str(test_bkp_dir))
    reset_db_manager()

    db = DatabaseManager(db_path=test_db_path, backup_dir=test_bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)

    master_repo = MasterDataRepository(db)
    inv_repo = InventoryRepository(db)
    acc_repo = AccountingRepository(db)
    sales_svc = SalesService(db)
    purchase_svc = PurchaseService(db)
    purchase_return_svc = PurchaseReturnService(db)
    acc_svc = AccountingService(db)

    cust_id = master_repo.create_customer(
        Customer(customer_name="Suresh Deshmukh", mobile="9822112233", village="Satara", opening_balance=500.0)
    )
    supp_id = master_repo.create_supplier(
        Supplier(supplier_name="Mahyco Seeds Pvt Ltd", gstin="27AAACM1234F1Z2", city="Jalna", opening_balance=1000.0)
    )
    prod_id = master_repo.create_product(
        Product(
            product_name="Hybrid Cotton Packet",
            category_id=2,
            unit_id=9,
            tax_group_id=4,
            default_purchase_rate=100.0,
            default_sale_rate=150.0,
        )
    )
    batch_id = inv_repo.upsert_batch(
        StockBatch(
            product_id=prod_id,
            batch_no="MHY-2026-01",
            mfg_date="2026-01-01",
            exp_date="2027-12-31",
            purchase_rate=100.0,
            mrp=180.0,
            sale_rate=150.0,
            current_qty=50.0,
        )
    )

    cat_row = db.fetch_one("SELECT category_id FROM expense_categories ORDER BY category_id ASC LIMIT 1;")
    assert cat_row is not None
    exp_cat_id = int(cat_row["category_id"])

    yield {
        "db": db,
        "master_repo": master_repo,
        "inv_repo": inv_repo,
        "acc_repo": acc_repo,
        "sales_svc": sales_svc,
        "purchase_svc": purchase_svc,
        "purchase_return_svc": purchase_return_svc,
        "acc_svc": acc_svc,
        "cust_id": cust_id,
        "supp_id": supp_id,
        "prod_id": prod_id,
        "batch_id": batch_id,
        "exp_cat_id": exp_cat_id,
    }

    reset_db_manager()


def _make_balanced_voucher(acc_repo: AccountingRepository, vch_no: str, vch_type: str, amount: float = 100.0, conn=None) -> Voucher:
    cash_ac = acc_repo.get_cash_account(conn=conn)
    sales_ac = acc_repo.get_sales_account(conn=conn)
    return Voucher(
        voucher_no=vch_no,
        voucher_date="2026-11-17",
        voucher_type=vch_type,
        total_amount=amount,
        narration=f"Test voucher {vch_no}",
        reference_type="MANUAL",
        reference_id=1,
        entries=[
            LedgerEntry(account_id=cash_ac.account_id, debit_amount=amount, credit_amount=0.0, particulars="Dr Cash"),
            LedgerEntry(account_id=sales_ac.account_id, debit_amount=0.0, credit_amount=amount, particulars="Cr Sales"),
        ],
    )


# ---------------------------------------------------------------------------
# 1. Caller-supplied connection behavior (no commit / rollback / close)
# ---------------------------------------------------------------------------

def test_create_expense_uses_caller_connection_without_committing_or_closing(tx_env):
    db = tx_env["db"]
    acc_repo = tx_env["acc_repo"]
    cash_ac = acc_repo.get_cash_account()

    expense = Expense(
        category_id=tx_env["exp_cat_id"],
        expense_date="2026-11-17",
        amount=250.0,
        payment_account_id=cash_ac.account_id,
        remarks="Hamali loading",
    )

    with db.transaction() as conn:
        exp_id = acc_repo.create_expense(expense, conn=conn)
        assert exp_id > 0

        # Caller connection must still be open and inside an uncommitted transaction
        assert conn.in_transaction is True
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM expenses WHERE expense_id = ?;", (exp_id,))
        assert cur.fetchone()["cnt"] == 1
        cur.close()

        # A separate connection must NOT see the uncommitted insert yet
        external_row = db.fetch_one("SELECT COUNT(*) as cnt FROM expenses WHERE expense_id = ?;", (exp_id,))
        assert external_row["cnt"] == 0

    # After caller commits, the row is durably visible
    committed_row = db.fetch_one("SELECT amount, remarks FROM expenses WHERE expense_id = ?;", (exp_id,))
    assert committed_row is not None
    assert committed_row["amount"] == pytest.approx(250.0)
    assert committed_row["remarks"] == "Hamali loading"


def test_generate_next_voucher_no_with_caller_connection_keeps_connection_open(tx_env):
    db = tx_env["db"]
    acc_repo = tx_env["acc_repo"]

    with db.transaction() as conn:
        vch_no = acc_repo.generate_next_voucher_no("RECEIPT", conn=conn)
        assert vch_no == "RCP-00001"
        # Connection remains open and usable
        cur = conn.cursor()
        cur.execute("SELECT 1 as ok;")
        assert cur.fetchone()["ok"] == 1
        cur.close()


# ---------------------------------------------------------------------------
# 2. Standalone behavior without a supplied connection
# ---------------------------------------------------------------------------

def test_create_expense_standalone_without_conn(tx_env):
    db = tx_env["db"]
    acc_repo = tx_env["acc_repo"]
    cash_ac = acc_repo.get_cash_account()

    exp_id = acc_repo.create_expense(
        Expense(
            category_id=tx_env["exp_cat_id"],
            expense_date="2026-11-17",
            amount=175.0,
            payment_account_id=cash_ac.account_id,
            remarks="Standalone electricity bill",
        )
    )
    assert exp_id > 0

    row = db.fetch_one("SELECT amount, remarks FROM expenses WHERE expense_id = ?;", (exp_id,))
    assert row is not None
    assert row["amount"] == pytest.approx(175.0)
    assert row["remarks"] == "Standalone electricity bill"


def test_generate_next_voucher_no_standalone_without_conn(tx_env):
    acc_repo = tx_env["acc_repo"]

    assert acc_repo.generate_next_voucher_no("RECEIPT") == "RCP-00001"
    assert acc_repo.generate_next_voucher_no("PAYMENT") == "PAY-00001"
    assert acc_repo.generate_next_voucher_no("CONTRA") == "CTR-00001"
    assert acc_repo.generate_next_voucher_no("JOURNAL") == "JRN-00001"
    assert acc_repo.generate_next_voucher_no("DEBIT_NOTE") == "DN-00001"
    assert acc_repo.generate_next_voucher_no("CREDIT_NOTE") == "CN-00001"
    assert acc_repo.generate_next_voucher_no("CUSTOM") == "VCH-00001"

    # Insert one committed voucher standalone, verify next number increments
    vch = _make_balanced_voucher(acc_repo, "RCP-00001", "RECEIPT")
    acc_repo.create_voucher(vch)
    assert acc_repo.generate_next_voucher_no("PAYMENT") == "PAY-00002"


# ---------------------------------------------------------------------------
# 3. Caller rollback also rolls back expense insert
# ---------------------------------------------------------------------------

def test_caller_rollback_rolls_back_create_expense(tx_env):
    db = tx_env["db"]
    acc_repo = tx_env["acc_repo"]
    cash_ac = acc_repo.get_cash_account()

    with pytest.raises(RuntimeError, match="Simulated failure"):
        with db.transaction() as conn:
            acc_repo.create_expense(
                Expense(
                    category_id=tx_env["exp_cat_id"],
                    expense_date="2026-11-17",
                    amount=500.0,
                    payment_account_id=cash_ac.account_id,
                    remarks="Should roll back",
                ),
                conn=conn,
            )
            raise RuntimeError("Simulated failure")

    row = db.fetch_one("SELECT COUNT(*) as cnt FROM expenses;")
    assert row["cnt"] == 0


def test_record_shop_expense_rolls_back_expense_when_voucher_posting_fails(tx_env, monkeypatch):
    db = tx_env["db"]
    acc_svc = tx_env["acc_svc"]

    def _boom(*args, **kwargs):
        raise RuntimeError("Forced voucher posting failure")

    monkeypatch.setattr(acc_svc.accounting_repo, "create_voucher", _boom)

    with pytest.raises(RuntimeError, match="Forced voucher posting failure"):
        acc_svc.record_shop_expense(
            Expense(
                category_id=tx_env["exp_cat_id"],
                expense_date="2026-11-17",
                amount=320.0,
                remarks="Must not persist partially",
            )
        )

    assert db.fetch_one("SELECT COUNT(*) as cnt FROM expenses;")["cnt"] == 0
    assert db.fetch_one("SELECT COUNT(*) as cnt FROM vouchers;")["cnt"] == 0


# ---------------------------------------------------------------------------
# 4. Voucher-number generation sees uncommitted inserts on supplied connection
# ---------------------------------------------------------------------------

def test_generate_next_voucher_no_sees_uncommitted_insert_on_supplied_conn(tx_env):
    db = tx_env["db"]
    acc_repo = tx_env["acc_repo"]

    with db.transaction() as conn:
        first_no = acc_repo.generate_next_voucher_no("PAYMENT", conn=conn)
        assert first_no == "PAY-00001"

        v1 = _make_balanced_voucher(acc_repo, first_no, "PAYMENT", conn=conn)
        acc_repo.create_voucher(v1, conn=conn)

        # Separate standalone connection still cannot see the uncommitted voucher
        assert acc_repo.generate_next_voucher_no("PAYMENT") == "PAY-00001"

        # Supplied transaction connection MUST see the uncommitted voucher and increment
        second_no = acc_repo.generate_next_voucher_no("PAYMENT", conn=conn)
        assert second_no == "PAY-00002"


# ---------------------------------------------------------------------------
# 5. Multiple voucher generations inside one transaction produce unique numbers
# ---------------------------------------------------------------------------

def test_multiple_voucher_generations_inside_single_transaction_are_unique(tx_env):
    db = tx_env["db"]
    acc_repo = tx_env["acc_repo"]

    generated_nos = []
    with db.transaction() as conn:
        for vtype in ("PAYMENT", "PAYMENT", "RECEIPT", "JOURNAL", "DEBIT_NOTE"):
            vch_no = acc_repo.generate_next_voucher_no(vtype, conn=conn)
            vch = _make_balanced_voucher(acc_repo, vch_no, vtype, amount=50.0, conn=conn)
            acc_repo.create_voucher(vch, conn=conn)
            generated_nos.append(vch_no)

    assert generated_nos == [
        "PAY-00001",
        "PAY-00002",
        "RCP-00003",
        "JRN-00004",
        "DN-00005",
    ]
    assert len(set(generated_nos)) == 5
    assert db.fetch_one("SELECT COUNT(*) as cnt FROM vouchers;")["cnt"] == 5


# ---------------------------------------------------------------------------
# 6 & 7. No extra connection opened when conn is supplied, and all service callers pass conn
# ---------------------------------------------------------------------------

def test_no_extra_connection_opened_when_caller_conn_supplied(tx_env, monkeypatch):
    db = tx_env["db"]
    acc_repo = tx_env["acc_repo"]
    cash_ac = acc_repo.get_cash_account()

    with db.transaction() as conn:
        open_calls = 0
        orig_get_conn = db.get_connection

        def _counting_get_connection(*args, **kwargs):
            nonlocal open_calls
            open_calls += 1
            return orig_get_conn(*args, **kwargs)

        monkeypatch.setattr(db, "get_connection", _counting_get_connection)

        exp_id = acc_repo.create_expense(
            Expense(
                category_id=tx_env["exp_cat_id"],
                expense_date="2026-11-17",
                amount=120.0,
                payment_account_id=cash_ac.account_id,
                remarks="No extra conn check",
            ),
            conn=conn,
        )
        vch_no = acc_repo.generate_next_voucher_no("PAYMENT", conn=conn)

        assert exp_id > 0
        assert vch_no == "PAY-00001"
        assert open_calls == 0


def test_all_service_callers_pass_active_transaction_conn(tx_env, monkeypatch):
    """Verify every transactional service passes its active conn into generate_next_voucher_no / create_expense."""
    db = tx_env["db"]
    sales_svc = tx_env["sales_svc"]
    purchase_svc = tx_env["purchase_svc"]
    purchase_return_svc = tx_env["purchase_return_svc"]
    acc_svc = tx_env["acc_svc"]

    def _spy_on_repo(repo):
        calls = {"voucher": [], "expense": []}
        orig_vch = repo.generate_next_voucher_no
        orig_exp = repo.create_expense

        def wrapped_vch(voucher_type, conn=None):
            calls["voucher"].append((voucher_type, conn))
            return orig_vch(voucher_type, conn=conn)

        def wrapped_exp(expense, conn=None):
            calls["expense"].append(conn)
            return orig_exp(expense, conn=conn)

        monkeypatch.setattr(repo, "generate_next_voucher_no", wrapped_vch)
        monkeypatch.setattr(repo, "create_expense", wrapped_exp)
        return calls

    sales_calls = _spy_on_repo(sales_svc.accounting_repo)
    purchase_calls = _spy_on_repo(purchase_svc.accounting_repo)
    pr_calls = _spy_on_repo(purchase_return_svc.accounting_repo)
    acc_calls = _spy_on_repo(acc_svc.accounting_repo)

    # Track total connections opened per single-transaction service call
    conn_counter = {"count": 0}
    orig_get_conn = db.get_connection

    def _counting_get_conn(*args, **kwargs):
        conn_counter["count"] += 1
        return orig_get_conn(*args, **kwargs)

    monkeypatch.setattr(db, "get_connection", _counting_get_conn)

    # 1. Sales invoice (1 transaction -> 1 connection)
    conn_counter["count"] = 0
    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-00001",
            sale_date="2026-11-17",
            customer_id=tx_env["cust_id"],
            payment_mode="CASH",
            total_taxable=150.0,
            total_cgst=13.5,
            total_sgst=13.5,
            total_igst=0.0,
            net_amount=177.0,
            paid_amount=177.0,
            due_amount=0.0,
            items=[
                SaleItem(
                    product_id=tx_env["prod_id"],
                    batch_id=tx_env["batch_id"],
                    qty=1.0,
                    unit_id=9,
                    sale_rate=150.0,
                    mrp=180.0,
                    taxable_amount=150.0,
                    cgst_rate=9.0,
                    cgst_amount=13.5,
                    sgst_rate=9.0,
                    sgst_amount=13.5,
                    total_amount=177.0,
                )
            ],
        )
    )
    assert sale_id > 0
    assert conn_counter["count"] == 1
    assert len(sales_calls["voucher"]) == 1
    assert isinstance(sales_calls["voucher"][0][1], sqlite3.Connection)

    # 2. Sales return (verify generate_next_voucher_no receives active conn)
    ret_id = sales_svc.process_sale_return(
        SalesReturn(
            return_no="RET-00001",
            return_date="2026-11-17",
            sale_id=sale_id,
            customer_id=tx_env["cust_id"],
            total_taxable=150.0,
            total_cgst=13.5,
            total_sgst=13.5,
            total_igst=0.0,
            net_amount=177.0,
            items=[
                SalesReturnItem(
                    product_id=tx_env["prod_id"],
                    batch_id=tx_env["batch_id"],
                    qty=1.0,
                    sale_rate=150.0,
                    taxable_amount=150.0,
                    cgst_rate=9.0,
                    cgst_amount=13.5,
                    sgst_rate=9.0,
                    sgst_amount=13.5,
                    total_amount=177.0,
                )
            ],
        )
    )
    assert ret_id > 0
    assert len(sales_calls["voucher"]) == 2
    assert isinstance(sales_calls["voucher"][1][1], sqlite3.Connection)

    # 3. Inward purchase (1 transaction -> 1 connection)
    conn_counter["count"] = 0
    purchase_id = purchase_svc.process_inward_purchase(
        Purchase(
            supplier_id=tx_env["supp_id"],
            invoice_no="SUP-INV-1001",
            purchase_date="2026-11-17",
            payment_mode="CREDIT",
            total_taxable=1000.0,
            total_cgst=90.0,
            total_sgst=90.0,
            total_igst=0.0,
            net_amount=1180.0,
            paid_amount=0.0,
            due_amount=1180.0,
            items=[
                PurchaseItem(
                    product_id=tx_env["prod_id"],
                    batch_no="MHY-2026-02",
                    mfg_date="2026-02-01",
                    exp_date="2027-12-31",
                    qty=10.0,
                    unit_id=9,
                    purchase_rate=100.0,
                    mrp=180.0,
                    sale_rate=150.0,
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
    assert conn_counter["count"] == 1
    assert len(purchase_calls["voucher"]) == 1
    assert isinstance(purchase_calls["voucher"][0][1], sqlite3.Connection)

    # 4. Purchase return (1 transaction -> 1 connection)
    pi_row = db.fetch_one(
        "SELECT item_id FROM purchase_items WHERE purchase_id = ? LIMIT 1;", (purchase_id,)
    )
    assert pi_row is not None
    conn_counter["count"] = 0
    pr = purchase_return_svc.process_purchase_return(
        PurchaseReturnCreate(
            purchase_id=purchase_id,
            supplier_id=tx_env["supp_id"],
            return_date="2026-11-17",
            itc_status="CLAIMED_REVERSED",
            idempotency_key=str(uuid.uuid4()),
            items=[
                PurchaseReturnItemCreate(
                    purchase_item_id=int(pi_row["item_id"]),
                    product_id=tx_env["prod_id"],
                    qty=Decimal("2"),
                    reason="Damaged",
                )
            ],
            settlements=[
                PurchaseReturnSettlementCreate(
                    settlement_type="PAYABLE_REDUCTION",
                    amount=Decimal("236.00"),
                )
            ],
        )
    )
    assert pr.return_id is not None and pr.return_id > 0
    assert conn_counter["count"] == 1
    assert len(pr_calls["voucher"]) == 1
    assert isinstance(pr_calls["voucher"][0][1], sqlite3.Connection)

    # 5. Customer receipt (1 transaction -> 1 connection)
    conn_counter["count"] = 0
    rcp_vch_id = acc_svc.record_customer_receipt(
        customer_id=tx_env["cust_id"],
        receipt_date="2026-11-17",
        amount=200.0,
        payment_mode="CASH",
    )
    assert rcp_vch_id > 0
    assert conn_counter["count"] == 1
    assert len(acc_calls["voucher"]) == 1
    assert isinstance(acc_calls["voucher"][0][1], sqlite3.Connection)

    # 6. Supplier payment (1 transaction -> 1 connection)
    conn_counter["count"] = 0
    pay_vch_id = acc_svc.record_supplier_payment(
        supplier_id=tx_env["supp_id"],
        amount=300.0,
        payment_date="2026-11-17",
        payment_mode="BANK_TRANSFER",
    )
    assert pay_vch_id > 0
    assert conn_counter["count"] == 1
    assert len(acc_calls["voucher"]) == 2
    assert isinstance(acc_calls["voucher"][1][1], sqlite3.Connection)

    # 7. Shop expense (1 transaction -> 1 connection for BOTH create_expense and generate_next_voucher_no)
    conn_counter["count"] = 0
    exp_id = acc_svc.record_shop_expense(
        Expense(
            category_id=tx_env["exp_cat_id"],
            expense_date="2026-11-17",
            amount=150.0,
            remarks="Transport freight",
        )
    )
    assert exp_id > 0
    assert conn_counter["count"] == 1
    assert len(acc_calls["expense"]) == 1
    assert isinstance(acc_calls["expense"][0], sqlite3.Connection)
    assert len(acc_calls["voucher"]) == 3
    assert isinstance(acc_calls["voucher"][2][1], sqlite3.Connection)
    assert acc_calls["expense"][0] is acc_calls["voucher"][2][1]
