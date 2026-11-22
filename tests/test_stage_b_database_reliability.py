"""
Comprehensive Verification Suite for Stage B: Database Reliability.

Covers:
1. TrackedSQLiteConnection weakref compatibility & GC finalizer / close tracking.
2. SalesRepository leak-free connection & transaction error cleanup.
3. Exclusive database maintenance lock (draining in-flight txs, blocking new ops, bounded timeout).
4. HTTP 503 Maintenance Guard across middleware and FastAPI exception handlers.
5. Windows-safe SQLite backup restoration (WAL checkpoint -> DELETE swap -> PermissionError retries -> rollback).
6. State-machine SQL migration lexer, @reviewed_migration_handler gating, and C SQLite authorizer.
7. Additive v2 migration (`owner_name`, `upi_id`) on isolated copies of v1/pre-v1 DBs and fresh installs.
"""
from __future__ import annotations

import concurrent.futures
import gc
import hashlib
import os
import shutil
import sqlite3
import threading
import time
import weakref
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import (
    DatabaseMaintenanceError,
    DatabaseManager,
    TrackedSQLiteConnection,
    get_db_manager,
    reset_db_manager,
)
from src.db.migrations import (
    BUILTIN_MIGRATIONS,
    Migration,
    MigrationSafetyError,
    assert_non_destructive_sql,
    reviewed_migration_handler,
)

LATEST_SCHEMA_VERSION = max(m.version for m in BUILTIN_MIGRATIONS)
EXPECTED_UPGRADES_FROM_V1 = sum(1 for m in BUILTIN_MIGRATIONS if m.version > 1)
from src.models.master_data import Customer, Product
from src.models.sales import Sale, SaleItem, SalesReturn, SalesReturnItem
from src.repositories.master_data_repository import MasterDataRepository
from src.repositories.sales_repository import SalesRepository
from src.services.backup_service import BackupService

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# =====================================================================
# 1. Tracked Connections & Repository Leak Prevention
# =====================================================================
def test_tracked_sqlite_connection_supports_weakref_and_gc_cleanup(tmp_path):
    """
    Native C sqlite3.Connection rejects weakref.ref; TrackedSQLiteConnection supports weakrefs,
    deterministic .close() decrementing, and GC finalizer cleanup if a caller forgets .close().
    """
    plain_path = tmp_path / "plain.db"
    plain_conn = sqlite3.connect(str(plain_path))
    try:
        with pytest.raises(TypeError):
            weakref.ref(plain_conn)
    finally:
        plain_conn.close()

    db = DatabaseManager(
        db_path=tmp_path / "tracked.db",
        app_mode="local",
        backup_dir=tmp_path / "bkps",
        clean_install=True,
    )
    db.initialize_database(include_seed=True)
    assert db.active_connection_count == 0

    conn_a = db.get_connection()
    assert isinstance(conn_a, TrackedSQLiteConnection)
    ref_a = weakref.ref(conn_a)
    assert ref_a() is conn_a
    assert db.active_connection_count == 1

    conn_a.close()
    assert db.active_connection_count == 0

    # Simulate forgotten close() where object goes out of scope and is GC collected
    def _open_and_forget():
        forgotten = db.get_connection()
        assert db.active_connection_count == 1
        return weakref.ref(forgotten)

    forgotten_ref = _open_and_forget()
    gc.collect()
    assert forgotten_ref() is None
    assert db.active_connection_count == 0


def test_sales_repository_closes_connections_cleanly_on_error(tmp_path, monkeypatch):
    """
    Verify SalesRepository.create_sale and create_sale_return close owned connections
    and roll back cleanly even when errors occur mid-operation.
    """
    db = DatabaseManager(
        db_path=tmp_path / "sales_leak.db",
        app_mode="local",
        backup_dir=tmp_path / "bkps",
        clean_install=True,
    )
    db.initialize_database(include_seed=True)
    master = MasterDataRepository(db)
    prod_id = master.create_product(Product(product_name="Potash Leak Check", default_sale_rate=500.0))
    cust_id = master.create_customer(Customer(customer_name="Ramesh Patil", mobile="9822001234"))

    # Create valid batch for FK constraint
    batch_id = db.execute_query(
        """
        INSERT INTO stock_batches (product_id, batch_no, purchase_rate, sale_rate, mrp, opening_qty, current_qty)
        VALUES (?, 'BATCH-B1', 400.0, 500.0, 520.0, 10.0, 10.0);
        """,
        (prod_id,),
    )
    assert db.active_connection_count == 0

    sales_repo = SalesRepository(db)
    sale = Sale(
        invoice_no="",
        sale_date="2026-11-09",
        customer_id=cust_id,
        payment_mode="CASH",
        total_taxable=500.0,
        net_amount=500.0,
        paid_amount=500.0,
        items=[
            SaleItem(
                product_id=prod_id,
                batch_id=batch_id,
                qty=1.0,
                unit_id=2,
                sale_rate=500.0,
                mrp=520.0,
                taxable_amount=500.0,
                total_amount=500.0,
            )
        ],
    )
    sale_id = sales_repo.create_sale(sale)
    assert sale_id > 0
    assert db.active_connection_count == 0

    # Simulate failure right before commit inside create_sale (bad FK on item) -> rollback + closed handle
    bad_sale = Sale(
        invoice_no="",
        sale_date="2026-11-09",
        customer_id=cust_id,
        payment_mode="CASH",
        total_taxable=500.0,
        net_amount=500.0,
        paid_amount=500.0,
        items=[
            SaleItem(
                product_id=prod_id,
                batch_id=999999,  # Non-existent FK triggers sqlite3.IntegrityError after header insert
                qty=1.0,
                unit_id=2,
                sale_rate=500.0,
                mrp=520.0,
                taxable_amount=500.0,
                total_amount=500.0,
            )
        ],
    )
    before_count = db.fetch_one("SELECT COUNT(*) AS cnt FROM sales;")["cnt"]
    with pytest.raises(sqlite3.IntegrityError):
        sales_repo.create_sale(bad_sale)
    assert db.active_connection_count == 0
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM sales;")["cnt"] == before_count

    # Simulate mid-return failure in create_sale_return -> rollback + closed handle
    monkeypatch.setattr(
        sales_repo,
        "generate_next_return_no",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Simulated return number error")),
    )
    bad_ret = SalesReturn(
        return_no="",
        return_date="2026-11-09",
        sale_id=sale_id,
        customer_id=cust_id,
        net_amount=500.0,
        items=[
            SalesReturnItem(
                product_id=prod_id,
                batch_id=batch_id,
                qty=1.0,
                sale_rate=500.0,
                taxable_amount=500.0,
                total_amount=500.0,
            )
        ],
    )
    with pytest.raises(RuntimeError, match="Simulated return number error"):
        sales_repo.create_sale_return(bad_ret)
    assert db.active_connection_count == 0


# =====================================================================
# 2. Exclusive Maintenance Lock & HTTP 503 Concurrency Guard
# =====================================================================
def test_exclusive_maintenance_drains_inflight_tx_and_times_out_safely(tmp_path):
    """
    Verify exclusive_maintenance:
    - Lets an in-flight transaction finish its helper queries and commit cleanly.
    - Blocks new external DB calls immediately with DatabaseMaintenanceError.
    - Times out safely BEFORE yielding if a non-closing external handle remains open.
    """
    db = DatabaseManager(
        db_path=tmp_path / "maint_lock.db",
        app_mode="local",
        backup_dir=tmp_path / "bkps",
        clean_install=True,
    )
    db.initialize_database(include_seed=True)

    # Part A: In-flight transaction drains cleanly while new external ops are blocked
    tx_started = threading.Event()
    maint_waiting = threading.Event()
    tx_can_finish = threading.Event()
    tx_helper_ok = {"ok": False}

    def inflight_worker():
        with db.transaction() as tx_conn:
            tx_conn.execute(
                "INSERT INTO categories (category_name, short_name) VALUES (?, ?);",
                ("Inflight Tx Category", "ITX"),
            )
            tx_started.set()
            assert maint_waiting.wait(timeout=3.0)
            time.sleep(0.05)  # Ensure Thread 2 has entered exclusive_maintenance wait state
            # Same in-flight transaction thread MUST still be allowed to run helper queries
            chk_conn = db.get_connection()
            try:
                cur = chk_conn.cursor()
                cur.execute("SELECT COUNT(*) FROM categories;")
                cur.fetchone()
                cur.close()
                tx_helper_ok["ok"] = True
            finally:
                chk_conn.close()
            tx_can_finish.wait(timeout=3.0)

    def maintenance_worker():
        assert tx_started.wait(timeout=3.0)
        maint_waiting.set()
        with db.exclusive_maintenance(timeout=3.0, reason="unit_drain_test"):
            assert db.is_in_maintenance is True
            assert db.active_connection_count == 0
            return "ENTERED"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_tx = pool.submit(inflight_worker)
        f_maint = pool.submit(maintenance_worker)

        assert maint_waiting.wait(timeout=3.0)
        deadline = time.monotonic() + 2.0
        while not db.is_in_maintenance and time.monotonic() < deadline:
            time.sleep(0.01)
        assert db.is_in_maintenance is True

        # New external DB call from outside thread MUST fail immediately with DatabaseMaintenanceError
        with pytest.raises(DatabaseMaintenanceError):
            db.fetch_one("SELECT 1;")

        tx_can_finish.set()
        f_tx.result(timeout=3.0)
        assert f_maint.result(timeout=3.0) == "ENTERED"

    assert tx_helper_ok["ok"] is True
    assert db.is_in_maintenance is False
    assert db.fetch_one("SELECT * FROM categories WHERE short_name = 'ITX';") is not None

    # Part B: Timeout fails safely BEFORE entering critical section when handle remains open
    held_conn = db.get_connection()
    entered_body = False
    try:
        with pytest.raises(DatabaseMaintenanceError, match="Timed out waiting"):
            with db.exclusive_maintenance(timeout=0.15, reason="leaked_handle_test"):
                entered_body = True
        assert entered_body is False
        assert db.is_in_maintenance is False
    finally:
        held_conn.close()
    assert db.active_connection_count == 0


def test_restore_returns_http_503_to_concurrent_api_requests(tmp_path, monkeypatch):
    """
    Verify concurrent `/api/*` requests receive HTTP 503 with Retry-After header
    while backup restoration holds exclusive maintenance.
    """
    reset_db_manager()
    local_appdata = tmp_path / "Http503Desktop"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    with TestClient(app) as client:
        setup_res = client.post(
            "/api/auth/setup-admin",
            json={
                "username": "maint_admin",
                "password": "StrongPassword#503",
                "full_name": "Shop Owner",
                "shop_name": "503 Guard Shop",
            },
        )
        assert setup_res.status_code == 200
        headers = {"Authorization": f"Bearer {setup_res.json()['token']}"}

        bkp_res = client.post("/api/system/backup", headers=headers)
        assert bkp_res.status_code == 200
        bkp_filename = bkp_res.json()["filename"]

        # Pause inside BackupService._sqlite_online_backup specifically during restore_backup
        restore_entered = threading.Event()
        release_restore = threading.Event()
        orig_online_backup = BackupService._sqlite_online_backup

        def paused_online_backup(src_conn, dst_path):
            if ".restore." in Path(dst_path).name:
                restore_entered.set()
                assert release_restore.wait(timeout=5.0)
            return orig_online_backup(src_conn, dst_path)

        monkeypatch.setattr(BackupService, "_sqlite_online_backup", staticmethod(paused_online_backup))

        def run_restore():
            return client.post(
                "/api/system/backup/restore",
                headers=headers,
                json={"backup_filename": bkp_filename, "confirm_restore": True},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            restore_future = pool.submit(run_restore)
            assert restore_entered.wait(timeout=3.0)

            concurrent_get = client.get("/api/masters/products", headers=headers)
            assert concurrent_get.status_code == 503
            assert concurrent_get.headers.get("retry-after") == "2"

            concurrent_post = client.post(
                "/api/masters/products",
                headers=headers,
                json={
                    "product_name": "Blocked During Maintenance",
                    "category_id": 1,
                    "unit_id": 2,
                    "tax_group_id": 2,
                    "default_sale_rate": 100.0,
                },
            )
            assert concurrent_post.status_code == 503

            release_restore.set()
            restore_resp = restore_future.result(timeout=5.0)
            assert restore_resp.status_code == 200
            assert restore_resp.json()["success"] is True

        # Traffic resumes cleanly with HTTP 200 immediately after maintenance ends
        after_res = client.get("/api/masters/products", headers=headers)
        assert after_res.status_code == 200

        # Direct exception handler path: DatabaseMaintenanceError raised inside route handler -> HTTP 503
        orig_search_products = MasterDataRepository.search_products
        orig_get_or_create_mfg = MasterDataRepository.get_or_create_manufacturer

        def raise_maint(*args, **kwargs):
            raise DatabaseMaintenanceError("Simulated mid-request maintenance lock")

        monkeypatch.setattr(MasterDataRepository, "search_products", raise_maint)
        res_direct_503 = client.get("/api/masters/products", headers=headers)
        assert res_direct_503.status_code == 503
        assert res_direct_503.headers.get("retry-after") == "2"
        assert "maintenance lock" in res_direct_503.json()["detail"]

        monkeypatch.setattr(MasterDataRepository, "get_or_create_manufacturer", raise_maint)
        res_reraised_503 = client.post(
            "/api/masters/manufacturers",
            headers=headers,
            json={"manufacturer_name": "Should Return 503"},
        )
        assert res_reraised_503.status_code == 503
        assert res_reraised_503.headers.get("retry-after") == "2"

        monkeypatch.setattr(MasterDataRepository, "search_products", orig_search_products)
        monkeypatch.setattr(MasterDataRepository, "get_or_create_manufacturer", orig_get_or_create_mfg)

    reset_db_manager()


# =====================================================================
# 3. Windows-Safe Backup Restoration, Retries & Rollback
# =====================================================================
def test_restore_wal_checkpoint_retries_and_rollback_safety(tmp_path, monkeypatch):
    """
    Verify backup restoration:
    1. Checkpoints active WAL frames before atomic swap and re-enables WAL afterward.
    2. Retries transient PermissionError on file replacement (simulated Windows lock).
    3. Rolls back automatically to preserved BEFORE_RESTORE backup on post-swap failure.
    """
    db_file = tmp_path / "win_restore_shop.db"
    bkp_dir = tmp_path / "win_backups"
    db = DatabaseManager(db_path=db_file, app_mode="local", backup_dir=bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)
    master = MasterDataRepository(db)
    svc = BackupService(db, backup_dir=bkp_dir)

    p_orig_id = master.create_product(Product(product_name="Original Baseline Product", default_sale_rate=250.0))
    baseline_bkp = svc.create_backup(backup_type="MANUAL")

    # Add WAL frames right before restore without manual checkpointing
    master.create_product(Product(product_name="Product Only In PreRestore WAL", default_sale_rate=390.0))

    # 1. Transient PermissionError (2 failures then success) on live DB swap
    orig_replace = os.replace
    attempts = {"count": 0}

    def flaky_win_replace(src, dst):
        if Path(dst).resolve() == db_file.resolve() and ".restore." in Path(src).name:
            attempts["count"] += 1
            if attempts["count"] <= 2:
                raise PermissionError(13, "Simulated WinError 32 file lock")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_win_replace)

    assert svc.restore_backup(
        baseline_bkp,
        file_retry_attempts=4,
        file_retry_delay=0.01,
    ) is True
    assert attempts["count"] == 3
    assert svc.last_pre_restore_backup is not None and svc.last_pre_restore_backup.exists()
    assert db.fetch_one("SELECT * FROM products WHERE product_id = ?;", (p_orig_id,)) is not None
    assert db.fetch_one("SELECT * FROM products WHERE product_name = 'Product Only In PreRestore WAL';") is None
    assert db.fetch_one("PRAGMA journal_mode;")[0].lower() == "wal"
    assert db.check_integrity() == ["ok"]
    assert db.check_foreign_keys() == []
    assert not list(db_file.parent.glob("*.tmp"))

    # 2. Post-swap verification failure triggers automatic rollback preserving BEFORE_RESTORE backup
    master.create_product(Product(product_name="Must Survive Failed Swap", default_sale_rate=777.0))
    monkeypatch.setattr(os, "replace", orig_replace)

    orig_verify = BackupService.verify_database_integrity
    verify_calls = {"live_checked": False}

    def failing_post_swap_verify(self_svc, db_file_path=None, check_foreign_keys=False):
        if db_file_path is not None and Path(db_file_path).resolve() == db_file.resolve():
            verify_calls["live_checked"] = True
            return False
        return orig_verify(self_svc, db_file_path=db_file_path, check_foreign_keys=check_foreign_keys)

    monkeypatch.setattr(BackupService, "verify_database_integrity", failing_post_swap_verify)

    with pytest.raises(RuntimeError, match="Post-restore integrity verification failed"):
        svc.restore_backup(baseline_bkp, file_retry_attempts=2, file_retry_delay=0.01)

    monkeypatch.setattr(BackupService, "verify_database_integrity", orig_verify)
    assert verify_calls["live_checked"] is True
    assert db.is_in_maintenance is False
    # Live DB automatically rolled back to state containing 'Must Survive Failed Swap'
    assert db.fetch_one("SELECT * FROM products WHERE product_name = 'Must Survive Failed Swap';") is not None
    assert svc.last_pre_restore_backup is not None and svc.last_pre_restore_backup.exists()
    assert svc.verify_database_integrity(svc.last_pre_restore_backup, check_foreign_keys=True) is True
    assert db.check_integrity() == ["ok"]
    assert db.check_foreign_keys() == []

    # 3. Exhausted PermissionError during swap raises PermissionError cleanly while preserving live DB & backup
    master.create_product(Product(product_name="Must Survive Permanent Lock", default_sale_rate=888.0))

    def always_locked_replace(src, dst):
        if Path(dst).resolve() == db_file.resolve() and ".restore." in Path(src).name:
            raise PermissionError(13, "Simulated permanent WinError 32 lock")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", always_locked_replace)
    with pytest.raises(PermissionError):
        svc.restore_backup(baseline_bkp, file_retry_attempts=2, file_retry_delay=0.01)

    monkeypatch.setattr(os, "replace", orig_replace)
    assert db.is_in_maintenance is False
    assert db.fetch_one("SELECT * FROM products WHERE product_name = 'Must Survive Permanent Lock';") is not None
    assert svc.last_pre_restore_backup is not None and svc.last_pre_restore_backup.exists()
    assert not list(db_file.parent.glob("*.tmp"))


# =====================================================================
# 4. Hardened Migration Lexer, Reviewed Handlers & SQLite Authorizer
# =====================================================================
def test_migration_sql_lexer_edge_cases_and_destructive_rejections():
    """Verify state-machine SQL lexer handles strings/comments safely while blocking obfuscated attacks."""
    # Safe statements (including destructive keywords inside strings, comments, or quoted identifiers)
    safe_sql_cases = (
        "-- DROP TABLE products; DELETE FROM users;\nALTER TABLE customers ADD COLUMN nick TEXT DEFAULT 'DROP TABLE fake;';",
        "/* TRUNCATE TABLE audit_log; */ ALTER TABLE customers ADD COLUMN tagline TEXT DEFAULT 'DELETE ''1=1'' safe';",
        'CREATE INDEX IF NOT EXISTS "DROP TABLE idx" ON customers(customer_name);',
        "CREATE TABLE IF NOT EXISTS [ATTACH DATABASE] (id INTEGER PRIMARY KEY, note TEXT DEFAULT 'WHERE 1=1');",
        "UPDATE customers SET village = 'Baramati' WHERE customer_id = 1;",
        "DELETE FROM audit_log WHERE log_id IN (SELECT log_id FROM audit_log WHERE action_type = 'NOOP');",
        "PRAGMA foreign_keys = ON;",
    )
    for sql in safe_sql_cases:
        assert_non_destructive_sql(sql)

    # Destructive / obfuscated statements that MUST be rejected
    unsafe_sql_cases = (
        "DROP TABLE products;",
        "DROP VIEW v_sales;",
        "DROP INDEX idx_test;",
        "DROP TRIGGER trg_test;",
        "TRUNCATE TABLE customers;",
        "ALTER TABLE customers DROP COLUMN mobile;",
        "ALTER TABLE customers DROP mobile;",  # Omit COLUMN keyword
        "ALTER TABLE main.customers DROP COLUMN mobile;",
        "ATTACH DATABASE '/tmp/other.db' AS evil;",
        "DETACH DATABASE main;",
        "PRAGMA writable_schema = ON;",
        "PRAGMA trusted_schema = 0;",
        "DELETE FROM products;",
        "UPDATE products SET default_sale_rate = 0;",
        "UPDATE products SET default_sale_rate = 0 WHERE 1 = 1;",
        "UPDATE products SET default_sale_rate = 0 WHERE TRUE;",
        "UPDATE products SET default_sale_rate = 0 WHERE 'a' = 'a';",
        "DELETE FROM products WHERE product_id = 1 OR 1 = 1;",
        "DELETE FROM products WHERE product_id = product_id;",
        "UPDATE customers SET village = 'X' WHERE (customer_id) = (customer_id);",
        "DELETE FROM customers WHERE LENGTH('abc') = 3;",
        "WITH cte AS (SELECT 1) DELETE FROM products;",
        "WITH cte AS (SELECT 1) UPDATE products SET default_mrp = 1 WHERE 1=1;",
        "CREATE TRIGGER bad_trg AFTER INSERT ON products BEGIN DELETE FROM users; END;",
    )
    for sql in unsafe_sql_cases:
        with pytest.raises(MigrationSafetyError, match="Unsafe migration rejected"):
            assert_non_destructive_sql(sql)


def test_migration_unreviewed_handler_and_sqlite_authorizer_blocks(tmp_path):
    """
    Verify:
    - Unreviewed Python handlers are rejected immediately before backup or execution.
    - Even @reviewed_migration_handler functions cannot execute DROP, ALTER DROP COLUMN,
      or ATTACH thanks to SQLite C-engine conn.set_authorizer.
    - Connection authorizer cleanly resets afterwards (Python 3.9 compatible) so commits work.
    """
    db_file = tmp_path / "mig_authz.db"
    bkp_dir = tmp_path / "mig_authz_bkps"
    db = DatabaseManager(db_path=db_file, app_mode="local", backup_dir=bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)
    v_base = db.get_schema_version()
    assert v_base == LATEST_SCHEMA_VERSION
    v_test = v_base + 1
    master = MasterDataRepository(db)
    prod_id = master.create_product(Product(product_name="Authorizer Guarded Seed", default_sale_rate=100.0))

    # 1. Unreviewed Python callable rejected
    def unreviewed_handler(conn):
        conn.execute("ALTER TABLE customers ADD COLUMN extra_note TEXT;")

    with pytest.raises(MigrationSafetyError, match="reviewed_migration_handler"):
        db.run_migrations(
            migrations=[Migration(version=v_test, description="Unreviewed test", handler=unreviewed_handler)],
            backup_dir=bkp_dir,
        )
    assert db.get_schema_version() == v_base

    # 2. Reviewed handler trying DROP TABLE blocked at C engine level
    @reviewed_migration_handler(reviewer="security_test", reason="Intentional authorizer penetration check")
    def sneaky_drop_handler(conn):
        conn.execute("DROP TABLE products;")

    with pytest.raises(MigrationSafetyError, match="SQLite authorizer"):
        db.run_migrations(
            migrations=[Migration(version=v_test, description="Sneaky drop", handler=sneaky_drop_handler)],
            backup_dir=bkp_dir,
        )
    assert db.get_schema_version() == v_base
    assert db.fetch_one("SELECT * FROM products WHERE product_id = ?;", (prod_id,)) is not None

    # 3. Reviewed handler trying ALTER TABLE ... DROP COLUMN blocked at C engine level
    @reviewed_migration_handler(reviewer="security_test", reason="Intentional ALTER DROP COLUMN check")
    def sneaky_drop_col_handler(conn):
        conn.execute("ALTER TABLE company_settings DROP COLUMN gstin;")

    with pytest.raises(MigrationSafetyError, match="SQLite authorizer"):
        db.run_migrations(
            migrations=[Migration(version=v_test, description="Sneaky drop col", handler=sneaky_drop_col_handler)],
            backup_dir=bkp_dir,
        )
    cols = {r["name"] for r in db.fetch_all("PRAGMA table_info(company_settings);")}
    assert "gstin" in cols

    # 4. Reviewed handler trying ATTACH DATABASE blocked at C engine level
    @reviewed_migration_handler(reviewer="security_test", reason="Intentional ATTACH authorizer check")
    def sneaky_attach_handler(conn):
        conn.execute("ATTACH DATABASE ':memory:' AS extra;")

    with pytest.raises(MigrationSafetyError, match="SQLite authorizer"):
        db.run_migrations(
            migrations=[Migration(version=v_test, description="Sneaky attach", handler=sneaky_attach_handler)],
            backup_dir=bkp_dir,
        )

    # 5. Concurrent maintenance blocks pending migrations safely before creating backups
    with db.exclusive_maintenance(timeout=1.0, reason="external_blocker"):
        def _blocked_migrate():
            return db.run_migrations(
                migrations=[
                    Migration(
                        version=v_test,
                        description="Blocked by maintenance",
                        statements=("ALTER TABLE customers ADD COLUMN blocked_tag TEXT;",),
                    )
                ],
                backup_dir=bkp_dir,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(DatabaseMaintenanceError):
                pool.submit(_blocked_migrate).result(timeout=2.0)

    # 6. Subsequent normal migration + DB operations commit normally (no lingering authorizer block)
    report = db.run_migrations(
        migrations=[
            Migration(
                version=v_test,
                description="Safe post-authorizer column",
                statements=("ALTER TABLE customers ADD COLUMN post_authz_tag TEXT DEFAULT '';",),
            )
        ],
        backup_dir=bkp_dir,
    )
    assert report.current_version == v_test
    assert db.get_schema_version() == v_test
    assert db.execute_query("INSERT INTO categories (category_name, short_name) VALUES (?, ?);", ("Post Mig", "PMG")) > 0


# =====================================================================
# 5. Real v1 -> v2 & Pre-v1 -> v2 Upgrade Safety and Fresh v2 Install
# =====================================================================
def test_v1_to_v2_upgrade_on_copied_live_db_preserves_all_records(tmp_path):
    """
    Copy existing repo v1 database to tmp_path (NEVER touching repo data/agri_erp.db),
    upgrade automatically to v2 via initialize_database, verify BEFORE_UPGRADE backup creation,
    owner_name + upi_id addition, and 100% row preservation across all 31 tables.
    """
    live_db = PROJECT_ROOT / "data" / "agri_erp.db"
    if not live_db.exists():
        pytest.skip("Original live data/agri_erp.db not present in environment")

    live_hash_before = _sha256(live_db)

    copied_db = tmp_path / "copied_live_shop.db"
    shutil.copy2(str(live_db), str(copied_db))
    live_wal = Path(f"{live_db}-wal")
    if live_wal.exists() and live_wal.stat().st_size > 0:
        shutil.copy2(str(live_wal), f"{copied_db}-wal")

    # Inspect v1 baseline state on copied_db BEFORE upgrade
    ro_before = sqlite3.connect(str(copied_db))
    try:
        tables = [
            r[0]
            for r in ro_before.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
            ).fetchall()
        ]
        counts_before = {
            t: ro_before.execute(f'SELECT COUNT(*) FROM "{t}";').fetchone()[0]
            for t in tables
        }
        ro_before.row_factory = sqlite3.Row
        row_cs = ro_before.execute("SELECT * FROM company_settings WHERE setting_id = 1;").fetchone()
        v1_company = dict(row_cs) if row_cs else {}
        version_before = ro_before.execute("SELECT MAX(version_number) FROM schema_version;").fetchone()[0]
    finally:
        ro_before.close()

    assert version_before == 1
    assert "owner_name" not in v1_company
    assert "upi_id" not in v1_company

    bkp_dir = tmp_path / "upgrade_backups"
    db = DatabaseManager(db_path=copied_db, app_mode="desktop", backup_dir=bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)

    assert db.get_schema_version() == LATEST_SCHEMA_VERSION
    assert db.check_integrity() == ["ok"]
    assert db.check_foreign_keys() == []

    # BEFORE_UPGRADE backup created cleanly before each upgrade step after v1
    upgrade_backups = list(bkp_dir.glob("agri_erp_before_upgrade_*.db"))
    assert len(upgrade_backups) == EXPECTED_UPGRADES_FROM_V1
    for ub in upgrade_backups:
        assert BackupService(db, backup_dir=bkp_dir).verify_database_integrity(
            ub, check_foreign_keys=True
        )

    # New v2 columns exist on company_settings AND all original fields are preserved verbatim
    cs_cols = {r["name"] for r in db.fetch_all("PRAGMA table_info(company_settings);")}
    assert {"owner_name", "upi_id"}.issubset(cs_cols)

    upgraded_cs = db.fetch_one("SELECT * FROM company_settings WHERE setting_id = 1;")
    assert upgraded_cs is not None
    for orig_col, orig_val in v1_company.items():
        assert upgraded_cs[orig_col] == orig_val

    # Every original row in every application table survived intact (plus new migration rows)
    for t, cnt in counts_before.items():
        if t == "schema_version":
            assert db.fetch_one('SELECT COUNT(*) AS c FROM "schema_version";')["c"] == cnt + EXPECTED_UPGRADES_FROM_V1
        elif t == "ledger_accounts":
            assert db.fetch_one('SELECT COUNT(*) AS c FROM "ledger_accounts";')["c"] in (cnt + 3, cnt + 5)
        elif t == "backup_log":
            assert db.fetch_one('SELECT COUNT(*) AS c FROM "backup_log";')["c"] >= cnt
        else:
            assert db.fetch_one(f'SELECT COUNT(*) AS c FROM "{t}";')["c"] == cnt

    # Re-running initialize_database is idempotent (0 new BEFORE_UPGRADE backups)
    db.initialize_database(include_seed=True)
    assert len(list(bkp_dir.glob("agri_erp_before_upgrade_*.db"))) == EXPECTED_UPGRADES_FROM_V1
    assert _sha256(live_db) == live_hash_before


def test_pre_v1_30_table_backup_upgrades_cleanly_to_v2(tmp_path):
    """Verify legacy 30-table DB copy (without schema_version) upgrades cleanly to latest schema."""
    legacy_bkp = PROJECT_ROOT / "backups" / "agri_erp_before_fresh_start_20260916_102335.db"
    if not legacy_bkp.exists():
        pytest.skip("Legacy pre-v1 backup not present in environment")

    legacy_hash_before = _sha256(legacy_bkp)
    copied_legacy = tmp_path / "copied_legacy_30.db"
    shutil.copy2(str(legacy_bkp), str(copied_legacy))

    bkp_dir = tmp_path / "legacy_upgrade_bkps"
    db = DatabaseManager(db_path=copied_legacy, app_mode="local", backup_dir=bkp_dir, clean_install=False)
    assert db.get_schema_version() == 0

    db.initialize_database(include_seed=False)
    assert db.get_schema_version() == LATEST_SCHEMA_VERSION
    assert db.check_integrity() == ["ok"]
    assert db.check_foreign_keys() == []
    assert db.fetch_one("SELECT COUNT(*) AS c FROM users;")["c"] == 2
    cs_cols = {r["name"] for r in db.fetch_all("PRAGMA table_info(company_settings);")}
    assert {"owner_name", "upi_id"}.issubset(cs_cols)
    assert _sha256(legacy_bkp) == legacy_hash_before


def test_fresh_clean_install_reaches_schema_v2(tmp_path):
    """Fresh desktop install cleanly reaches latest schema version with owner_name & upi_id."""
    fresh_db = tmp_path / "fresh_v2.db"
    bkp_dir = tmp_path / "fresh_v2_bkps"
    db = DatabaseManager(db_path=fresh_db, app_mode="desktop", backup_dir=bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)

    assert db.get_schema_version() == LATEST_SCHEMA_VERSION
    cs_cols = {r["name"] for r in db.fetch_all("PRAGMA table_info(company_settings);")}
    assert {"owner_name", "upi_id"}.issubset(cs_cols)
    assert db.check_integrity() == ["ok"]
    assert db.check_foreign_keys() == []

