"""
Comprehensive Isolated Test Suite for Phase 2B-5B:
Backend Sales & Purchase Idempotency + Migration v5.

All 19 required test cases run strictly against isolated temporary SQLite databases.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Tuple
import uuid

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from src.app import app
from src.db.connection import DatabaseManager, reset_db_manager
from src.db.migrations import BUILTIN_MIGRATIONS, MigrationRunner
from src.models.inventory import StockBatch
from src.models.master_data import Customer, Product, Supplier
from src.models.purchase import Purchase, PurchaseItem
from src.models.sales import Sale, SaleItem
from src.repositories.accounting_repository import AccountingRepository
from src.repositories.inventory_repository import InventoryRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.repositories.purchase_repository import PurchaseRepository
from src.repositories.sales_repository import SalesRepository
from src.services.auth_service import AuthService
from src.services.backup_service import BackupService
from src.services.idempotency import DuplicateInvoiceError, IdempotencyConflictError
from src.services.purchase_service import PurchaseService
import src.services.purchase_service as purchase_service_mod
from src.services.sales_service import SalesService
import src.services.sales_service as sales_service_mod


@pytest.fixture
def idem_env(tmp_path, monkeypatch):
    """Provides a strictly isolated temporary SQLite DB, services, and authenticated client."""
    db_path = tmp_path / "test_phase2b5b_idempotency.db"
    bkp_dir = tmp_path / "test_phase2b5b_backups"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    reset_db_manager()

    db = DatabaseManager(db_path=db_path, backup_dir=bkp_dir)
    db.initialize_database(include_seed=True)

    master_repo = MasterDataRepository(db)
    inv_repo = InventoryRepository(db)
    sales_repo = SalesRepository(db)
    purchase_repo = PurchaseRepository(db)
    acc_repo = AccountingRepository(db)
    sales_svc = SalesService(db)
    purchase_svc = PurchaseService(db)

    cust_id = master_repo.create_customer(
        Customer(customer_name="Ganesh Pawar", mobile="9822998877", village="Solapur", opening_balance=0.0)
    )
    cust2_id = master_repo.create_customer(
        Customer(customer_name="Mahesh Kadam", mobile="9822998866", village="Satara", opening_balance=0.0)
    )
    supp_id = master_repo.create_supplier(
        Supplier(supplier_name="Coromandel Agro Supply", gstin="27AAACC1234F1Z1", city="Pune", opening_balance=0.0)
    )
    prod1_id = master_repo.create_product(
        Product(
            product_name="Coromandel DAP 50kg",
            category_id=1,
            unit_id=2,
            tax_group_id=2,  # 5% GST
            default_purchase_rate=1200.0,
            default_sale_rate=1350.0,
            default_mrp=1400.0,
        )
    )
    prod2_id = master_repo.create_product(
        Product(
            product_name="Bayer Confidor 100ml",
            category_id=3,
            unit_id=1,
            tax_group_id=4,  # 18% GST
            default_purchase_rate=300.0,
            default_sale_rate=400.0,
            default_mrp=420.0,
        )
    )
    batch1_id = inv_repo.upsert_batch(
        StockBatch(
            product_id=prod1_id,
            batch_no="DAP-2026-A",
            mfg_date="2026-01-01",
            exp_date="2027-12-31",
            purchase_rate=1200.0,
            mrp=1400.0,
            sale_rate=1350.0,
            current_qty=50.0,
        )
    )
    batch2_id = inv_repo.upsert_batch(
        StockBatch(
            product_id=prod2_id,
            batch_no="CNF-2026-B",
            mfg_date="2026-01-01",
            exp_date="2027-12-31",
            purchase_rate=300.0,
            mrp=420.0,
            sale_rate=400.0,
            current_qty=50.0,
        )
    )

    auth_svc = AuthService(db)
    user = auth_svc.authenticate_user("admin", "krushidhan@2026")
    assert user is not None
    token = auth_svc.create_access_token(user.user_id, user.username, user.full_name, user.role)
    headers = {"Authorization": f"Bearer {token}"}

    yield {
        "db": db,
        "master_repo": master_repo,
        "inv_repo": inv_repo,
        "sales_repo": sales_repo,
        "purchase_repo": purchase_repo,
        "acc_repo": acc_repo,
        "sales_svc": sales_svc,
        "purchase_svc": purchase_svc,
        "cust_id": cust_id,
        "cust2_id": cust2_id,
        "supp_id": supp_id,
        "prod1_id": prod1_id,
        "prod2_id": prod2_id,
        "batch1_id": batch1_id,
        "batch2_id": batch2_id,
        "headers": headers,
    }
    reset_db_manager()


def _make_sale(
    env: Dict[str, Any],
    *,
    invoice_no: Optional[str] = "",
    sale_date: Optional[str] = "2026-11-17",
    customer_id: Optional[int] = None,
    qty: float = 2.0,
    net_amount: float = 2700.0,
    paid_amount: float = 0.0,
    payment_mode: str = "CREDIT",
    idempotency_key: Optional[str] = None,
    items: Optional[List[SaleItem]] = None,
) -> Sale:
    taxable = round(net_amount / 1.05, 2)
    half_tax = round((net_amount - taxable) / 2.0, 2)
    return Sale(
        invoice_no=invoice_no,
        sale_date=sale_date,
        customer_id=customer_id if customer_id is not None else env["cust_id"],
        payment_mode=payment_mode,
        total_taxable=taxable,
        total_cgst=half_tax,
        total_sgst=half_tax,
        net_amount=net_amount,
        paid_amount=paid_amount,
        due_amount=max(0.0, net_amount - paid_amount),
        idempotency_key=idempotency_key,
        items=items
        if items is not None
        else [
            SaleItem(
                product_id=env["prod1_id"],
                batch_id=env["batch1_id"],
                qty=qty,
                sale_rate=1350.0,
                mrp=1400.0,
                taxable_amount=taxable,
                cgst_rate=2.5,
                cgst_amount=half_tax,
                sgst_rate=2.5,
                sgst_amount=half_tax,
                total_amount=net_amount,
            )
        ],
    )


def _make_purchase(
    env: Dict[str, Any],
    *,
    invoice_no: str = "SUP-INV-2026-01",
    purchase_date: Optional[str] = "2026-11-17",
    qty: float = 5.0,
    net_amount: float = 6300.0,
    paid_amount: float = 0.0,
    idempotency_key: Optional[str] = None,
    batch_no: str = "DAP-2026-A",
) -> Purchase:
    taxable = round(net_amount / 1.05, 2)
    half_tax = round((net_amount - taxable) / 2.0, 2)
    return Purchase(
        invoice_no=invoice_no,
        purchase_date=purchase_date,
        supplier_id=env["supp_id"],
        payment_type="CREDIT",
        total_taxable=taxable,
        total_cgst=half_tax,
        total_sgst=half_tax,
        net_amount=net_amount,
        paid_amount=paid_amount,
        due_amount=max(0.0, net_amount - paid_amount),
        idempotency_key=idempotency_key,
        items=[
            PurchaseItem(
                product_id=env["prod1_id"],
                batch_no=batch_no,
                qty=qty,
                purchase_rate=1200.0,
                sale_rate=1350.0,
                mrp=1400.0,
                taxable_amount=taxable,
                cgst_rate=2.5,
                cgst_amount=half_tax,
                sgst_rate=2.5,
                sgst_amount=half_tax,
                total_amount=net_amount,
            )
        ],
    )


def _build_pre_v5_database(db_path: Path) -> None:
    """Create a realistic v4 database (without v5 columns/indexes on sales & purchases) with historical rows."""
    schema_sql = (Path(__file__).resolve().parent.parent / "src" / "db" / "schema.sql").read_text(
        encoding="utf-8"
    )
    # Strip the v5 additions from schema.sql to build an authentic v4 database
    v4_sql = schema_sql.replace(
        "    created_at TEXT DEFAULT CURRENT_TIMESTAMP,\n    idempotency_key TEXT,\n    request_fingerprint TEXT",
        "    created_at TEXT DEFAULT CURRENT_TIMESTAMP",
    )
    v4_sql = v4_sql.replace(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_sales_idempotency_key ON sales(idempotency_key) WHERE idempotency_key IS NOT NULL;\n",
        "",
    )
    v4_sql = v4_sql.replace(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_purchases_idempotency_key ON purchases(idempotency_key) WHERE idempotency_key IS NOT NULL;\n",
        "",
    )

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(v4_sql)
        # Mark v2, v3, v4 applied (as an existing v4 shop database)
        conn.execute("INSERT OR IGNORE INTO schema_version (version_number, description) VALUES (2, 'v2');")
        conn.execute("INSERT OR IGNORE INTO schema_version (version_number, description) VALUES (3, 'v3');")
        conn.execute("INSERT OR IGNORE INTO schema_version (version_number, description) VALUES (4, 'v4');")
        # Insert historical customer, supplier, sale, and purchase rows
        conn.execute(
            "INSERT INTO customers (customer_id, customer_name, mobile) VALUES (1, 'Legacy Farmer', '9800001111');"
        )
        conn.execute(
            "INSERT INTO suppliers (supplier_id, supplier_name, mobile) VALUES (1, 'Legacy Supplier', '9800002222');"
        )
        conn.execute(
            """
            INSERT INTO sales (invoice_no, sale_date, customer_id, payment_mode, total_taxable, net_amount, paid_amount, due_amount)
            VALUES ('INV-LEGACY-01', '2026-10-01', 1, 'CASH', 1000.0, 1000.0, 1000.0, 0.0);
            """
        )
        conn.execute(
            """
            INSERT INTO purchases (invoice_no, purchase_date, supplier_id, payment_type, total_taxable, net_amount, paid_amount, due_amount)
            VALUES ('PUR-LEGACY-01', '2026-10-01', 1, 'CREDIT', 2000.0, 2000.0, 0.0, 2000.0);
            """
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# MIGRATION v5 TESTS (Tests 1 & 2)
# ============================================================================

def test_01_v4_to_v5_upgrade_backup_null_preservation_and_integrity(tmp_path):
    """Test 1: v4 -> v5 creates BEFORE_UPGRADE backup, preserves legacy rows with NULL keys, passes integrity & FK checks."""
    v4_db = tmp_path / "upgrade_v4_to_v5.db"
    bkp_dir = tmp_path / "upgrade_bkps"
    _build_pre_v5_database(v4_db)

    db_mgr = DatabaseManager(db_path=v4_db, backup_dir=bkp_dir)
    assert db_mgr.get_schema_version() == 4

    v5_migrations = [m for m in BUILTIN_MIGRATIONS if m.version <= 5]
    report = MigrationRunner(db_mgr).run_pending_migrations(migrations=v5_migrations, backup_dir=bkp_dir)
    assert report.previous_version == 4
    assert report.current_version == 5
    assert report.applied_versions == [5]
    assert len(report.backups_created) == 1
    assert report.backups_created[0].exists()
    assert BackupService(db_mgr, backup_dir=bkp_dir).verify_database_integrity(
        report.backups_created[0], check_foreign_keys=True
    )

    assert db_mgr.get_schema_version() == 5
    assert db_mgr.check_integrity() == ["ok"]
    assert db_mgr.check_foreign_keys() == []

    # Historical rows preserved verbatim with NULL idempotency_key & request_fingerprint
    s_row = db_mgr.fetch_one("SELECT invoice_no, net_amount, idempotency_key, request_fingerprint FROM sales;")
    assert s_row["invoice_no"] == "INV-LEGACY-01"
    assert s_row["net_amount"] == pytest.approx(1000.0)
    assert s_row["idempotency_key"] is None
    assert s_row["request_fingerprint"] is None

    p_row = db_mgr.fetch_one("SELECT invoice_no, net_amount, idempotency_key, request_fingerprint FROM purchases;")
    assert p_row["invoice_no"] == "PUR-LEGACY-01"
    assert p_row["net_amount"] == pytest.approx(2000.0)
    assert p_row["idempotency_key"] is None
    assert p_row["request_fingerprint"] is None


def test_02_v5_migration_idempotent_and_clean_install_matches_upgraded_db(tmp_path):
    """Test 2: Re-running migration is safe no-op; clean install and v4-upgraded DB have identical columns & indexes."""
    # Upgraded DB (pinned specifically to v5 migrations)
    upgraded_path = tmp_path / "upgraded_v5.db"
    upg_bkps = tmp_path / "upg_bkps"
    _build_pre_v5_database(upgraded_path)
    upg_db = DatabaseManager(db_path=upgraded_path, backup_dir=upg_bkps)
    runner = MigrationRunner(upg_db)
    v5_migrations = [m for m in BUILTIN_MIGRATIONS if m.version <= 5]
    r1 = runner.run_pending_migrations(migrations=v5_migrations, backup_dir=upg_bkps)
    assert r1.applied_versions == [5]
    assert len(r1.backups_created) == 1

    # Re-run must be a safe no-op with 0 new backups
    r2 = runner.run_pending_migrations(migrations=v5_migrations, backup_dir=upg_bkps)
    assert r2.applied_versions == []
    assert r2.backups_created == []
    assert r2.current_version == 5

    # Clean install DB
    clean_path = tmp_path / "clean_v5.db"
    clean_bkps = tmp_path / "clean_bkps"
    clean_db = DatabaseManager(db_path=clean_path, backup_dir=clean_bkps, clean_install=True)
    clean_db.initialize_database(include_seed=True)
    assert clean_db.get_schema_version() >= 5

    # Compare columns (names, types, exact order) and indexes on sales & purchases
    for table in ("sales", "purchases"):
        upg_cols = [(r["name"], r["type"]) for r in upg_db.fetch_all(f"PRAGMA table_info({table});")]
        cln_cols = [(r["name"], r["type"]) for r in clean_db.fetch_all(f"PRAGMA table_info({table});")]
        assert upg_cols == cln_cols, f"Column mismatch on {table}: {upg_cols} vs {cln_cols}"
        assert ("idempotency_key", "TEXT") in cln_cols
        assert ("request_fingerprint", "TEXT") in cln_cols

        upg_idxs = sorted(
            (r["name"], r["sql"])
            for r in upg_db.fetch_all(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL;",
                (table,),
            )
        )
        cln_idxs = sorted(
            (r["name"], r["sql"])
            for r in clean_db.fetch_all(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL;",
                (table,),
            )
        )
        assert upg_idxs == cln_idxs, f"Index mismatch on {table}: {upg_idxs} vs {cln_idxs}"


# ============================================================================
# SALES IDEMPOTENCY TESTS (Tests 3–12)
# ============================================================================

def test_03_sales_same_key_and_payload_replays_with_zero_side_effects(idem_env):
    """Test 3: Same key + same payload returns original sale_id/invoice_no with idempotent_replay=True; 1 sale/stock/balance/voucher."""
    db = idem_env["db"]
    sales_svc = idem_env["sales_svc"]
    inv_repo = idem_env["inv_repo"]
    master_repo = idem_env["master_repo"]

    key = f"SALE-{uuid.uuid4()}"
    sale1 = _make_sale(idem_env, invoice_no="", idempotency_key=key)
    sale_id_1, replay_1 = sales_svc.process_sales_invoice_with_status(sale1)
    assert sale_id_1 > 0
    assert replay_1 is False
    assert sale1.invoice_no == "INV-00001"

    sale2 = _make_sale(idem_env, invoice_no="", idempotency_key=key)
    sale_id_2, replay_2 = sales_svc.process_sales_invoice_with_status(sale2)
    assert sale_id_2 == sale_id_1
    assert replay_2 is True
    assert sale2.invoice_no == "INV-00001"

    assert db.fetch_one("SELECT COUNT(*) as c FROM sales;")["c"] == 1
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers WHERE reference_type = 'SALE';")["c"] == 1
    assert inv_repo.get_batch_by_id(idem_env["batch1_id"]).current_qty == pytest.approx(48.0)
    assert master_repo.get_customer_by_id(idem_env["cust_id"]).current_balance == pytest.approx(2700.0)


def test_04_sales_same_key_with_changed_payload_raises_conflict_with_zero_changes(idem_env):
    """Test 4: Same key + changed qty/amount/customer/item raises IdempotencyConflictError with zero DB side effects."""
    db = idem_env["db"]
    sales_svc = idem_env["sales_svc"]
    inv_repo = idem_env["inv_repo"]

    key = f"SALE-CONF-{uuid.uuid4()}"
    sale1 = _make_sale(idem_env, invoice_no="", idempotency_key=key, qty=2.0, net_amount=2700.0)
    sale_id_1, _ = sales_svc.process_sales_invoice_with_status(sale1)

    mutations = [
        _make_sale(idem_env, invoice_no="", idempotency_key=key, qty=3.0, net_amount=2700.0),
        _make_sale(idem_env, invoice_no="", idempotency_key=key, qty=2.0, net_amount=2800.0),
        _make_sale(idem_env, invoice_no="", idempotency_key=key, customer_id=idem_env["cust2_id"]),
        _make_sale(
            idem_env,
            invoice_no="",
            idempotency_key=key,
            items=[
                SaleItem(
                    product_id=idem_env["prod2_id"],
                    batch_id=idem_env["batch2_id"],
                    qty=2.0,
                    sale_rate=400.0,
                    mrp=420.0,
                    taxable_amount=677.97,
                    cgst_rate=9.0,
                    cgst_amount=61.02,
                    sgst_rate=9.0,
                    sgst_amount=61.02,
                    total_amount=800.0,
                )
            ],
            net_amount=800.0,
        ),
    ]
    for mutated in mutations:
        with pytest.raises(IdempotencyConflictError):
            sales_svc.process_sales_invoice_with_status(mutated)

    assert db.fetch_one("SELECT COUNT(*) as c FROM sales;")["c"] == 1
    assert db.fetch_one("SELECT sale_id FROM sales;")["sale_id"] == sale_id_1
    assert inv_repo.get_batch_by_id(idem_env["batch1_id"]).current_qty == pytest.approx(48.0)


def test_05_sales_lost_response_simulation_returns_original_sale(idem_env):
    """Test 5: First call commits and response is discarded; retry with same key returns original sale."""
    db = idem_env["db"]
    sales_svc = idem_env["sales_svc"]

    key = f"SALE-LOST-{uuid.uuid4()}"
    # Commit and discard return value (simulating dropped HTTP response)
    _discarded = sales_svc.process_sales_invoice_with_status(
        _make_sale(idem_env, invoice_no="", idempotency_key=key)
    )
    persisted = db.fetch_one("SELECT sale_id, invoice_no FROM sales WHERE idempotency_key = ?;", (key,))
    assert persisted is not None

    retry_sale = _make_sale(idem_env, invoice_no="", idempotency_key=key)
    retry_id, is_replay = sales_svc.process_sales_invoice_with_status(retry_sale)
    assert is_replay is True
    assert retry_id == persisted["sale_id"]
    assert retry_sale.invoice_no == persisted["invoice_no"]
    assert db.fetch_one("SELECT COUNT(*) as c FROM sales;")["c"] == 1


def test_06_sales_replay_succeeds_even_after_stock_depleted_by_other_sales(idem_env):
    """Test 6: After first sale, other sales deplete stock to 0; replay still succeeds with no error & no writes."""
    db = idem_env["db"]
    sales_svc = idem_env["sales_svc"]
    inv_repo = idem_env["inv_repo"]

    key = f"SALE-DEPLETE-{uuid.uuid4()}"
    s1 = _make_sale(idem_env, invoice_no="", idempotency_key=key, qty=5.0, net_amount=6750.0)
    s1_id, r1 = sales_svc.process_sales_invoice_with_status(s1)
    assert r1 is False
    assert inv_repo.get_batch_by_id(idem_env["batch1_id"]).current_qty == pytest.approx(45.0)

    # Another sale depletes all remaining 45 units -> stock is now 0
    s_other = _make_sale(
        idem_env,
        invoice_no="",
        idempotency_key=f"SALE-OTHER-{uuid.uuid4()}",
        qty=45.0,
        net_amount=60750.0,
    )
    sales_svc.process_sales_invoice(s_other)
    assert inv_repo.get_batch_by_id(idem_env["batch1_id"]).current_qty == pytest.approx(0.0)

    # Replay of s1 MUST succeed cleanly without triggering Insufficient stock
    s1_retry = _make_sale(idem_env, invoice_no="", idempotency_key=key, qty=5.0, net_amount=6750.0)
    s1_retry_id, r2 = sales_svc.process_sales_invoice_with_status(s1_retry)
    assert r2 is True
    assert s1_retry_id == s1_id
    assert s1_retry.invoice_no == s1.invoice_no
    assert inv_repo.get_batch_by_id(idem_env["batch1_id"]).current_qty == pytest.approx(0.0)
    assert db.fetch_one("SELECT COUNT(*) as c FROM sales;")["c"] == 2


def test_07_sales_replay_next_day_with_omitted_date_does_not_false_conflict(idem_env, monkeypatch):
    """Test 7: Client omits sale_date; retry the next day still matches original fingerprint (no false 409)."""
    sales_svc = idem_env["sales_svc"]
    key = f"SALE-MIDNIGHT-{uuid.uuid4()}"

    class _Day1Datetime:
        @classmethod
        def now(cls):
            return datetime(2026, 11, 17, 23, 59, 55)

    class _Day2Datetime:
        @classmethod
        def now(cls):
            return datetime(2026, 11, 18, 0, 0, 5)

    monkeypatch.setattr(sales_service_mod, "datetime", _Day1Datetime)
    s1 = _make_sale(idem_env, invoice_no="", sale_date=None, idempotency_key=key)
    s1_id, r1 = sales_svc.process_sales_invoice_with_status(s1)
    assert r1 is False
    assert s1.sale_date == "2026-11-17"

    monkeypatch.setattr(sales_service_mod, "datetime", _Day2Datetime)
    s2 = _make_sale(idem_env, invoice_no="", sale_date=None, idempotency_key=key)
    s2_id, r2 = sales_svc.process_sales_invoice_with_status(s2)
    assert r2 is True
    assert s2_id == s1_id
    assert s2.invoice_no == s1.invoice_no


def test_08_sales_failed_first_attempt_leaves_no_key_so_retry_after_fix_succeeds(idem_env):
    """Test 8: Insufficient stock rollback leaves no key stored; retrying same key after stock added succeeds."""
    db = idem_env["db"]
    sales_svc = idem_env["sales_svc"]
    inv_repo = idem_env["inv_repo"]

    key = f"SALE-RETRY-FIX-{uuid.uuid4()}"
    with pytest.raises(ValueError, match="Insufficient"):
        sales_svc.process_sales_invoice(
            _make_sale(idem_env, invoice_no="", idempotency_key=key, qty=60.0, net_amount=81000.0)
        )

    assert db.fetch_one("SELECT * FROM sales WHERE idempotency_key = ?;", (key,)) is None

    # Replenish stock, retry with EXACT SAME key -> succeeds!
    inv_repo.add_batch_stock(idem_env["batch1_id"], 20.0)
    sale_ok = _make_sale(idem_env, invoice_no="", idempotency_key=key, qty=60.0, net_amount=81000.0)
    sid, is_replay = sales_svc.process_sales_invoice_with_status(sale_ok)
    assert sid > 0
    assert is_replay is False
    assert inv_repo.get_batch_by_id(idem_env["batch1_id"]).current_qty == pytest.approx(10.0)


def test_09_sales_duplicate_explicit_invoice_no_rejected_and_blank_auto_generates(idem_env):
    """Test 9: Explicit duplicate invoice_no raises DuplicateInvoiceError (never renumbered); blank auto-generates."""
    db = idem_env["db"]
    sales_svc = idem_env["sales_svc"]

    s1 = _make_sale(idem_env, invoice_no="INV-EXPLICIT-99")
    s1_id = sales_svc.process_sales_invoice(s1)
    assert s1_id > 0

    # Duplicate explicit invoice_no without matching key MUST be rejected, never silently renumbered!
    with pytest.raises(DuplicateInvoiceError, match="INV-EXPLICIT-99"):
        sales_svc.process_sales_invoice(_make_sale(idem_env, invoice_no="INV-EXPLICIT-99"))

    assert db.fetch_one("SELECT COUNT(*) as c FROM sales;")["c"] == 1

    # Blank invoice_no still auto-generates sequential numbers cleanly
    auto1 = _make_sale(idem_env, invoice_no="")
    auto2 = _make_sale(idem_env, invoice_no="   ")
    sales_svc.process_sales_invoice(auto1)
    sales_svc.process_sales_invoice(auto2)
    assert auto1.invoice_no.startswith("INV-")
    assert auto2.invoice_no.startswith("INV-")
    assert auto1.invoice_no != auto2.invoice_no


def test_10_sales_six_thread_barrier_same_key_produces_single_sale(idem_env):
    """Test 10: 6 concurrent threads with Barrier + same key -> all return same sale_id/invoice_no, 1 DB sale."""
    db = idem_env["db"]
    inv_repo = idem_env["inv_repo"]
    master_repo = idem_env["master_repo"]

    key = f"SALE-CONC-{uuid.uuid4()}"
    barrier = threading.Barrier(6)
    results: List[Tuple[int, str, bool]] = []
    errors: List[Exception] = []
    lock = threading.Lock()

    def _worker():
        svc = SalesService(db)
        sale = _make_sale(idem_env, invoice_no="", idempotency_key=key, qty=3.0, net_amount=4050.0)
        barrier.wait()
        try:
            sid, is_replay = svc.process_sales_invoice_with_status(sale)
            with lock:
                results.append((sid, sale.invoice_no, is_replay))
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert len(results) == 6
    assert len({r[0] for r in results}) == 1
    assert len({r[1] for r in results}) == 1
    assert sum(1 for r in results if r[2] is False) == 1
    assert sum(1 for r in results if r[2] is True) == 5

    assert db.fetch_one("SELECT COUNT(*) as c FROM sales;")["c"] == 1
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers WHERE reference_type = 'SALE';")["c"] == 1
    assert inv_repo.get_batch_by_id(idem_env["batch1_id"]).current_qty == pytest.approx(47.0)
    assert master_repo.get_customer_by_id(idem_env["cust_id"]).current_balance == pytest.approx(4050.0)


def test_11_sales_identical_content_different_keys_create_two_bills(idem_env):
    """Test 11: Two identical sales with DIFFERENT keys create two separate valid bills."""
    db = idem_env["db"]
    sales_svc = idem_env["sales_svc"]
    inv_repo = idem_env["inv_repo"]

    s1 = _make_sale(idem_env, invoice_no="", idempotency_key=f"SALE-A-{uuid.uuid4()}")
    s2 = _make_sale(idem_env, invoice_no="", idempotency_key=f"SALE-B-{uuid.uuid4()}")
    id1, r1 = sales_svc.process_sales_invoice_with_status(s1)
    id2, r2 = sales_svc.process_sales_invoice_with_status(s2)

    assert id1 != id2
    assert s1.invoice_no != s2.invoice_no
    assert r1 is False and r2 is False
    assert db.fetch_one("SELECT COUNT(*) as c FROM sales;")["c"] == 2
    assert inv_repo.get_batch_by_id(idem_env["batch1_id"]).current_qty == pytest.approx(46.0)


def test_12_sales_without_key_preserves_backward_compatibility(idem_env):
    """Test 12: Requests without idempotency_key continue to work with NULL key/fingerprint."""
    db = idem_env["db"]
    sales_svc = idem_env["sales_svc"]

    s = _make_sale(idem_env, invoice_no="", idempotency_key=None)
    sid, is_replay = sales_svc.process_sales_invoice_with_status(s)
    assert sid > 0 and is_replay is False
    row = db.fetch_one("SELECT idempotency_key, request_fingerprint FROM sales WHERE sale_id = ?;", (sid,))
    assert row["idempotency_key"] is None
    assert row["request_fingerprint"] is None


# ============================================================================
# PURCHASE IDEMPOTENCY TESTS (Tests 13–18)
# ============================================================================

def test_13_purchase_same_key_and_payload_replays_with_zero_side_effects(idem_env):
    """Test 13: Same key + same payload replays original purchase_id/invoice_no with 0 extra stock/balance/vouchers."""
    db = idem_env["db"]
    purchase_svc = idem_env["purchase_svc"]
    inv_repo = idem_env["inv_repo"]
    master_repo = idem_env["master_repo"]

    key = f"PUR-{uuid.uuid4()}"
    p1 = _make_purchase(idem_env, idempotency_key=key)
    pid1, r1 = purchase_svc.process_inward_purchase_with_status(p1)
    assert pid1 > 0 and r1 is False

    p2 = _make_purchase(idem_env, idempotency_key=key)
    pid2, r2 = purchase_svc.process_inward_purchase_with_status(p2)
    assert pid2 == pid1 and r2 is True
    assert p2.invoice_no == p1.invoice_no

    assert db.fetch_one("SELECT COUNT(*) as c FROM purchases;")["c"] == 1
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers WHERE reference_type = 'PURCHASE';")["c"] == 1
    assert db.fetch_one("SELECT COUNT(*) as c FROM stock_ledger WHERE transaction_type = 'PURCHASE';")["c"] == 1
    assert inv_repo.get_batch_by_id(idem_env["batch1_id"]).current_qty == pytest.approx(55.0)
    assert master_repo.get_supplier_by_id(idem_env["supp_id"]).current_balance == pytest.approx(6300.0)


def test_14_purchase_same_key_changed_payload_raises_conflict_with_zero_changes(idem_env):
    """Test 14: Same key + changed payload raises IdempotencyConflictError with zero DB changes."""
    db = idem_env["db"]
    purchase_svc = idem_env["purchase_svc"]
    inv_repo = idem_env["inv_repo"]

    key = f"PUR-CONF-{uuid.uuid4()}"
    purchase_svc.process_inward_purchase(_make_purchase(idem_env, idempotency_key=key, qty=5.0, net_amount=6300.0))

    with pytest.raises(IdempotencyConflictError):
        purchase_svc.process_inward_purchase(
            _make_purchase(idem_env, idempotency_key=key, qty=6.0, net_amount=7560.0)
        )
    with pytest.raises(IdempotencyConflictError):
        purchase_svc.process_inward_purchase(
            _make_purchase(idem_env, invoice_no="SUP-DIFFERENT", idempotency_key=key)
        )

    assert db.fetch_one("SELECT COUNT(*) as c FROM purchases;")["c"] == 1
    assert inv_repo.get_batch_by_id(idem_env["batch1_id"]).current_qty == pytest.approx(55.0)


def test_15_purchase_lost_response_next_day_omitted_date_and_failed_retry(idem_env, monkeypatch):
    """Test 15: Lost-response replay, omitted purchase_date next-day replay, and failed-first-attempt retry."""
    db = idem_env["db"]
    purchase_svc = idem_env["purchase_svc"]

    # (a) Lost response + next day with omitted purchase_date
    key_lost = f"PUR-LOST-{uuid.uuid4()}"

    class _Day1:
        @classmethod
        def now(cls):
            return datetime(2026, 11, 17, 23, 59, 50)

    class _Day2:
        @classmethod
        def now(cls):
            return datetime(2026, 11, 18, 0, 0, 10)

    monkeypatch.setattr(purchase_service_mod, "datetime", _Day1)
    _discarded = purchase_svc.process_inward_purchase_with_status(
        _make_purchase(idem_env, purchase_date=None, idempotency_key=key_lost)
    )
    persisted = db.fetch_one("SELECT purchase_id, invoice_no FROM purchases WHERE idempotency_key = ?;", (key_lost,))
    assert persisted is not None

    monkeypatch.setattr(purchase_service_mod, "datetime", _Day2)
    retry_p = _make_purchase(idem_env, purchase_date=None, idempotency_key=key_lost)
    retry_id, is_replay = purchase_svc.process_inward_purchase_with_status(retry_p)
    assert is_replay is True
    assert retry_id == persisted["purchase_id"]
    assert db.fetch_one("SELECT COUNT(*) as c FROM purchases;")["c"] == 1

    # (b) Failed first attempt leaves no key behind -> retry succeeds after failure removed
    key_fail = f"PUR-FAIL-{uuid.uuid4()}"
    orig_create_vch = purchase_svc.accounting_repo.create_voucher
    monkeypatch.setattr(
        purchase_svc.accounting_repo,
        "create_voucher",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Simulated voucher failure")),
    )
    with pytest.raises(RuntimeError, match="Simulated voucher failure"):
        purchase_svc.process_inward_purchase(_make_purchase(idem_env, invoice_no="SUP-RETRY-1", idempotency_key=key_fail))
    assert db.fetch_one("SELECT * FROM purchases WHERE idempotency_key = ?;", (key_fail,)) is None

    monkeypatch.setattr(purchase_svc.accounting_repo, "create_voucher", orig_create_vch)
    pid_ok, is_replay_ok = purchase_svc.process_inward_purchase_with_status(
        _make_purchase(idem_env, invoice_no="SUP-RETRY-1", idempotency_key=key_fail)
    )
    assert pid_ok > 0 and is_replay_ok is False


def test_16_purchase_six_thread_barrier_same_key_produces_single_purchase(idem_env):
    """Test 16: 6 threads with Barrier + same key -> single purchase, single stock addition, single balance, single voucher."""
    db = idem_env["db"]
    inv_repo = idem_env["inv_repo"]
    master_repo = idem_env["master_repo"]

    key = f"PUR-CONC-{uuid.uuid4()}"
    barrier = threading.Barrier(6)
    results: List[Tuple[int, str, bool]] = []
    errors: List[Exception] = []
    lock = threading.Lock()

    def _worker():
        svc = PurchaseService(db)
        pur = _make_purchase(idem_env, invoice_no="SUP-CONC-01", idempotency_key=key, qty=4.0, net_amount=5040.0)
        barrier.wait()
        try:
            pid, is_replay = svc.process_inward_purchase_with_status(pur)
            with lock:
                results.append((pid, pur.invoice_no, is_replay))
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert len(results) == 6
    assert len({r[0] for r in results}) == 1
    assert sum(1 for r in results if r[2] is False) == 1
    assert sum(1 for r in results if r[2] is True) == 5

    assert db.fetch_one("SELECT COUNT(*) as c FROM purchases;")["c"] == 1
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers WHERE reference_type = 'PURCHASE';")["c"] == 1
    assert inv_repo.get_batch_by_id(idem_env["batch1_id"]).current_qty == pytest.approx(54.0)
    assert master_repo.get_supplier_by_id(idem_env["supp_id"]).current_balance == pytest.approx(5040.0)


def test_17_purchase_identical_content_different_keys_create_two_records(idem_env):
    """Test 17: Two purchases with different keys and identical content (even same supplier invoice_no) create 2 records."""
    db = idem_env["db"]
    purchase_svc = idem_env["purchase_svc"]

    p1 = _make_purchase(idem_env, invoice_no="SUP-SHARED-001", idempotency_key=f"PUR-A-{uuid.uuid4()}")
    p2 = _make_purchase(idem_env, invoice_no="SUP-SHARED-001", idempotency_key=f"PUR-B-{uuid.uuid4()}")
    pid1, r1 = purchase_svc.process_inward_purchase_with_status(p1)
    pid2, r2 = purchase_svc.process_inward_purchase_with_status(p2)

    assert pid1 != pid2
    assert r1 is False and r2 is False
    assert db.fetch_one("SELECT COUNT(*) as c FROM purchases;")["c"] == 2


def test_18_purchase_blank_invoice_no_rejected_and_whitespace_stripped(idem_env):
    """Test 18: Blank/whitespace purchase invoice_no is rejected; padded invoice_no is stripped before storing."""
    db = idem_env["db"]
    purchase_svc = idem_env["purchase_svc"]

    for bad_inv in ("", "   ", "\t\n"):
        with pytest.raises(ValidationError, match="invoice_no"):
            _make_purchase(idem_env, invoice_no=bad_inv)

    padded = _make_purchase(idem_env, invoice_no="  SUP-TRIM-01  ", idempotency_key=f"PUR-TRIM-{uuid.uuid4()}")
    assert padded.invoice_no == "SUP-TRIM-01"
    pid = purchase_svc.process_inward_purchase(padded)
    row = db.fetch_one("SELECT invoice_no FROM purchases WHERE purchase_id = ?;", (pid,))
    assert row["invoice_no"] == "SUP-TRIM-01"


# ============================================================================
# API ROUTE-LEVEL TESTS (Test 19)
# ============================================================================

def test_19_api_sales_and_purchase_idempotency_http_contracts(idem_env):
    """
    Test 19: Route-level HTTP checks for /api/sales/create and /api/purchase/create:
    - 200 first call (idempotent_replay=false)
    - 200 replay (idempotent_replay=true)
    - 409 on idempotency payload conflict
    - 409 on duplicate explicit sales invoice_no
    - 422 on invalid idempotency_key (blank, >128 chars, non-ASCII) and client-supplied request_fingerprint
    """
    headers = idem_env["headers"]
    sale_key = f"API-SALE-{uuid.uuid4()}"
    pur_key = f"API-PUR-{uuid.uuid4()}"

    sale_body = {
        "invoice_no": "",
        "sale_date": "2026-11-17",
        "customer_id": idem_env["cust_id"],
        "payment_mode": "CREDIT",
        "total_taxable": 2571.43,
        "total_cgst": 64.29,
        "total_sgst": 64.29,
        "net_amount": 2700.0,
        "paid_amount": 0.0,
        "due_amount": 2700.0,
        "idempotency_key": sale_key,
        "items": [
            {
                "product_id": idem_env["prod1_id"],
                "batch_id": idem_env["batch1_id"],
                "qty": 2.0,
                "sale_rate": 1350.0,
                "mrp": 1400.0,
                "taxable_amount": 2571.43,
                "cgst_rate": 2.5,
                "cgst_amount": 64.29,
                "sgst_rate": 2.5,
                "sgst_amount": 64.29,
                "total_amount": 2700.0,
            }
        ],
    }

    pur_body = {
        "invoice_no": "API-SUP-001",
        "purchase_date": "2026-11-17",
        "supplier_id": idem_env["supp_id"],
        "payment_type": "CREDIT",
        "total_taxable": 6000.0,
        "total_cgst": 150.0,
        "total_sgst": 150.0,
        "net_amount": 6300.0,
        "paid_amount": 0.0,
        "due_amount": 6300.0,
        "idempotency_key": pur_key,
        "items": [
            {
                "product_id": idem_env["prod1_id"],
                "batch_no": "DAP-2026-A",
                "qty": 5.0,
                "purchase_rate": 1200.0,
                "sale_rate": 1350.0,
                "mrp": 1400.0,
                "taxable_amount": 6000.0,
                "cgst_rate": 2.5,
                "cgst_amount": 150.0,
                "sgst_rate": 2.5,
                "sgst_amount": 150.0,
                "total_amount": 6300.0,
            }
        ],
    }

    with TestClient(app) as client:
        # Sales: 200 first call -> 200 replay -> 409 conflict
        r1 = client.post("/api/sales/create", json=sale_body, headers=headers)
        assert r1.status_code == 200, r1.text
        d1 = r1.json()
        assert d1["success"] is True and d1["idempotent_replay"] is False
        orig_sale_id = d1["sale_id"]
        orig_sale_inv = d1["invoice_no"]

        r2 = client.post("/api/sales/create", json=sale_body, headers=headers)
        assert r2.status_code == 200, r2.text
        d2 = r2.json()
        assert d2["sale_id"] == orig_sale_id
        assert d2["invoice_no"] == orig_sale_inv
        assert d2["idempotent_replay"] is True

        sale_conflict = dict(sale_body, net_amount=2800.0)
        r_conf = client.post("/api/sales/create", json=sale_conflict, headers=headers)
        assert r_conf.status_code == 409

        # Sales: duplicate explicit invoice_no -> 409
        dup_inv_body = dict(sale_body, invoice_no=orig_sale_inv, idempotency_key=f"OTHER-{uuid.uuid4()}")
        r_dup = client.post("/api/sales/create", json=dup_inv_body, headers=headers)
        assert r_dup.status_code == 409

        # Sales: invalid idempotency_key & client-supplied request_fingerprint -> 400
        for bad_key in ("", "   ", "x" * 129, "bad\nkey", "non-ascii-₹"):
            res_bad = client.post("/api/sales/create", json=dict(sale_body, idempotency_key=bad_key), headers=headers)
            assert res_bad.status_code == 400, f"Expected 400 for bad key {bad_key!r}, got {res_bad.status_code}"

        res_fp = client.post(
            "/api/sales/create",
            json=dict(sale_body, idempotency_key=f"FP-{uuid.uuid4()}", request_fingerprint="forged"),
            headers=headers,
        )
        assert res_fp.status_code == 400

        # Purchases: 200 first call -> 200 replay -> 409 conflict
        pr1 = client.post("/api/purchase/create", json=pur_body, headers=headers)
        assert pr1.status_code == 200, pr1.text
        pd1 = pr1.json()
        assert pd1["success"] is True and pd1["idempotent_replay"] is False

        pr2 = client.post("/api/purchase/create", json=pur_body, headers=headers)
        assert pr2.status_code == 200, pr2.text
        pd2 = pr2.json()
        assert pd2["purchase_id"] == pd1["purchase_id"]
        assert pd2["invoice_no"] == "API-SUP-001"
        assert pd2["idempotent_replay"] is True

        pur_conflict = dict(pur_body, net_amount=6500.0)
        pr_conf = client.post("/api/purchase/create", json=pur_conflict, headers=headers)
        assert pr_conf.status_code == 409

        # Purchases: invalid key & client request_fingerprint -> 400; blank invoice_no -> 422
        for bad_key in ("", "x" * 129, "bad\tkey", "emoji-\u2603"):
            res_pbad = client.post("/api/purchase/create", json=dict(pur_body, idempotency_key=bad_key), headers=headers)
            assert res_pbad.status_code == 400

        res_pfp = client.post(
            "/api/purchase/create",
            json=dict(pur_body, idempotency_key=f"PFP-{uuid.uuid4()}", request_fingerprint="forged"),
            headers=headers,
        )
        assert res_pfp.status_code == 400

        res_blank_inv = client.post(
            "/api/purchase/create",
            json=dict(pur_body, invoice_no="   ", idempotency_key=f"BL-{uuid.uuid4()}"),
            headers=headers,
        )
        assert res_blank_inv.status_code == 422


def test_20_http_idempotency_key_header_and_body_matrix(idem_env):
    """
    Test 20 (Phase 2B-5 Final Corrections):
    Verify every row of the Body vs HTTP Idempotency-Key header matrix on BOTH
    POST /api/sales/create and POST /api/purchase/create:
    - Body Key A, Header Missing -> 200 (Accept Key A)
    - Body Missing, Header Key A -> 200 (Accept Key A, replays with same fingerprint)
    - Body Key A, Header Key A -> 200 (Accept, replays)
    - Body Key A, Header Key B -> 400
    - Body Missing, Header Missing -> 200 (Legacy compatibility)
    - Blank or invalid (body OR header) -> 400
    - Client-supplied request_fingerprint (with or without header) -> 400
    """
    base_headers = idem_env["headers"]
    db = idem_env["db"]

    sale_base = {
        "invoice_no": "",
        "sale_date": "2026-11-17",
        "customer_id": idem_env["cust_id"],
        "payment_mode": "CREDIT",
        "total_taxable": 1285.71,
        "total_cgst": 32.14,
        "total_sgst": 32.15,
        "net_amount": 1350.0,
        "paid_amount": 0.0,
        "due_amount": 1350.0,
        "items": [
            {
                "product_id": idem_env["prod1_id"],
                "batch_id": idem_env["batch1_id"],
                "qty": 1.0,
                "sale_rate": 1350.0,
                "mrp": 1400.0,
                "taxable_amount": 1285.71,
                "cgst_rate": 2.5,
                "cgst_amount": 32.14,
                "sgst_rate": 2.5,
                "sgst_amount": 32.15,
                "total_amount": 1350.0,
            }
        ],
    }

    pur_base = {
        "invoice_no": "HDR-SUP-001",
        "purchase_date": "2026-11-17",
        "supplier_id": idem_env["supp_id"],
        "payment_type": "CREDIT",
        "total_taxable": 2400.0,
        "total_cgst": 60.0,
        "total_sgst": 60.0,
        "net_amount": 2520.0,
        "paid_amount": 0.0,
        "due_amount": 2520.0,
        "items": [
            {
                "product_id": idem_env["prod1_id"],
                "batch_no": "DAP-2026-A",
                "qty": 2.0,
                "purchase_rate": 1200.0,
                "sale_rate": 1350.0,
                "mrp": 1400.0,
                "taxable_amount": 2400.0,
                "cgst_rate": 2.5,
                "cgst_amount": 60.0,
                "sgst_rate": 2.5,
                "sgst_amount": 60.0,
                "total_amount": 2520.0,
            }
        ],
    }

    with TestClient(app) as client:
        # ---------- SALES MATRIX ----------
        s_key_hdr = f"SALE-HDR-{uuid.uuid4()}"
        s_key_body = f"SALE-BODY-{uuid.uuid4()}"

        # 1. Missing body, Header Key A -> Accept Key A (fresh save)
        r_hdr1 = client.post("/api/sales/create", json=sale_base, headers={**base_headers, "Idempotency-Key": s_key_hdr})
        assert r_hdr1.status_code == 200, r_hdr1.text
        assert r_hdr1.json()["idempotent_replay"] is False
        orig_hdr_sale_id = r_hdr1.json()["sale_id"]

        # 2. Missing body, Header Key A replay -> 200 replay
        r_hdr2 = client.post("/api/sales/create", json=sale_base, headers={**base_headers, "Idempotency-Key": s_key_hdr})
        assert r_hdr2.status_code == 200, r_hdr2.text
        assert r_hdr2.json()["idempotent_replay"] is True
        assert r_hdr2.json()["sale_id"] == orig_hdr_sale_id

        # 3. Body Key A + Header Key A (matching) -> 200 replay of same sale
        r_both = client.post(
            "/api/sales/create",
            json=dict(sale_base, idempotency_key=s_key_hdr),
            headers={**base_headers, "Idempotency-Key": f"  {s_key_hdr}  "},
        )
        assert r_both.status_code == 200, r_both.text
        assert r_both.json()["idempotent_replay"] is True
        assert r_both.json()["sale_id"] == orig_hdr_sale_id

        # 4. Body Key A, Header Missing -> Accept Key A
        r_body1 = client.post("/api/sales/create", json=dict(sale_base, idempotency_key=s_key_body), headers=base_headers)
        assert r_body1.status_code == 200, r_body1.text
        assert r_body1.json()["idempotent_replay"] is False
        assert r_body1.json()["sale_id"] != orig_hdr_sale_id

        # 5. Body Key A, Header Key B -> HTTP 400
        r_mismatch = client.post(
            "/api/sales/create",
            json=dict(sale_base, idempotency_key=s_key_body),
            headers={**base_headers, "Idempotency-Key": f"DIFF-{uuid.uuid4()}"},
        )
        assert r_mismatch.status_code == 400

        # 6. Missing body, Missing header -> Legacy compatibility (creates new sale)
        r_legacy = client.post("/api/sales/create", json=sale_base, headers=base_headers)
        assert r_legacy.status_code == 200, r_legacy.text
        assert r_legacy.json()["idempotent_replay"] is False

        # 7. Blank or invalid header -> HTTP 400
        for bad_hdr in ("", "   ", "y" * 129, "bad\tval"):
            r_bh = client.post("/api/sales/create", json=sale_base, headers={**base_headers, "Idempotency-Key": bad_hdr})
            assert r_bh.status_code == 400, f"Expected 400 for bad header {bad_hdr!r}, got {r_bh.status_code}"

        # 8. Client-supplied request_fingerprint with valid header -> HTTP 400
        r_sfp = client.post(
            "/api/sales/create",
            json=dict(sale_base, request_fingerprint="forged-fp"),
            headers={**base_headers, "Idempotency-Key": f"SFP-{uuid.uuid4()}"},
        )
        assert r_sfp.status_code == 400

        # ---------- PURCHASE MATRIX ----------
        p_key_hdr = f"PUR-HDR-{uuid.uuid4()}"
        p_key_body = f"PUR-BODY-{uuid.uuid4()}"

        # 1. Missing body, Header Key A -> Accept Key A
        pr_hdr1 = client.post("/api/purchase/create", json=pur_base, headers={**base_headers, "Idempotency-Key": p_key_hdr})
        assert pr_hdr1.status_code == 200, pr_hdr1.text
        assert pr_hdr1.json()["idempotent_replay"] is False
        orig_hdr_pur_id = pr_hdr1.json()["purchase_id"]

        # 2. Body Key A + Header Key A (matching) -> 200 replay
        pr_both = client.post(
            "/api/purchase/create",
            json=dict(pur_base, idempotency_key=p_key_hdr),
            headers={**base_headers, "Idempotency-Key": p_key_hdr},
        )
        assert pr_both.status_code == 200, pr_both.text
        assert pr_both.json()["idempotent_replay"] is True
        assert pr_both.json()["purchase_id"] == orig_hdr_pur_id

        # 3. Body Key A, Header Missing -> Accept Key A
        pr_body1 = client.post("/api/purchase/create", json=dict(pur_base, idempotency_key=p_key_body), headers=base_headers)
        assert pr_body1.status_code == 200, pr_body1.text
        assert pr_body1.json()["idempotent_replay"] is False
        assert pr_body1.json()["purchase_id"] != orig_hdr_pur_id

        # 4. Body Key A, Header Key B -> HTTP 400
        pr_mismatch = client.post(
            "/api/purchase/create",
            json=dict(pur_base, idempotency_key=p_key_body),
            headers={**base_headers, "Idempotency-Key": f"PDIFF-{uuid.uuid4()}"},
        )
        assert pr_mismatch.status_code == 400

        # 5. Missing body, Missing header -> Legacy compatibility
        pr_legacy = client.post("/api/purchase/create", json=pur_base, headers=base_headers)
        assert pr_legacy.status_code == 200, pr_legacy.text
        assert pr_legacy.json()["idempotent_replay"] is False

        # 6. Blank or invalid header -> HTTP 400
        for bad_hdr in ("", "   ", "z" * 129):
            pr_bh = client.post("/api/purchase/create", json=pur_base, headers={**base_headers, "Idempotency-Key": bad_hdr})
            assert pr_bh.status_code == 400

        # 7. Client-supplied request_fingerprint with valid header -> HTTP 400
        pr_fp = client.post(
            "/api/purchase/create",
            json=dict(pur_base, request_fingerprint="forged-fp"),
            headers={**base_headers, "Idempotency-Key": f"PFP-{uuid.uuid4()}"},
        )
        assert pr_fp.status_code == 400

        # Verify persisted keys stored on header-only rows
        s_row = db.fetch_one("SELECT idempotency_key FROM sales WHERE sale_id = ?;", (orig_hdr_sale_id,))
        assert s_row["idempotency_key"] == s_key_hdr
        p_row = db.fetch_one("SELECT idempotency_key FROM purchases WHERE purchase_id = ?;", (orig_hdr_pur_id,))
        assert p_row["idempotency_key"] == p_key_hdr


def test_21_fresh_process_imports_no_circular_import():
    """
    Test 21 (Phase 2B-5 Final Corrections):
    Verify model imports, repository imports, service imports, and PurchaseReturn compatibility
    succeed cleanly in independent fresh Python processes with zero circular-import errors.
    """
    import subprocess
    import sys

    project_root = str(Path(__file__).resolve().parent.parent)
    checks = [
        "from src.models.master_data import Customer, Product, Supplier",
        "from src.models.purchase import Purchase; from src.models.sales import Sale",
        "from src.repositories.sales_repository import SalesRepository; from src.repositories.purchase_repository import PurchaseRepository",
        (
            "import src.idempotency as a, src.services.idempotency as b, "
            "src.services.purchase_return_service as c, src.services as d; "
            "assert a.IdempotencyConflictError is b.IdempotencyConflictError is c.IdempotencyConflictError is d.IdempotencyConflictError"
        ),
    ]
    for expr in checks:
        proc = subprocess.run(
            [sys.executable, "-c", expr],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert proc.returncode == 0, f"Fresh import failed for `{expr}`:\nSTDERR:\n{proc.stderr}"
