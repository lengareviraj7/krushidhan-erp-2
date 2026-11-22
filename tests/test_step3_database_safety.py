"""
Comprehensive Verification Suite for Step 3: Database Safety, Storage Layout,
Runtime Mode Separation, Clean First-Run Setup, Safe Backup/Restore, and Migration Safety.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.config import (
    get_desktop_app_dir,
    is_cloud_sync_allowed,
    load_runtime_dotenv,
    resolve_app_mode,
    resolve_backup_dir,
    resolve_db_path,
)
from src.db.cloud_sync import (
    CloudSyncManager,
    get_cloud_sync_manager,
    reset_cloud_sync_manager,
)
from src.db.connection import DatabaseManager, get_db_manager, reset_db_manager
from src.db.migrations import (
    BUILTIN_MIGRATIONS,
    Migration,
    MigrationSafetyError,
    assert_non_destructive_sql,
)

LATEST_SCHEMA_VERSION = max(m.version for m in BUILTIN_MIGRATIONS)
from src.models.master_data import Product
from src.models.system import CompanySettings
from src.printing.invoice_printer import InvoicePrinter
from src.repositories.master_data_repository import MasterDataRepository
from src.services.auth_service import AuthService
from src.services.backup_service import BackupService

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ALL_31_TABLES = {
    "categories",
    "manufacturers",
    "units",
    "unit_conversions",
    "tax_groups",
    "crops",
    "products",
    "customer_groups",
    "customers",
    "suppliers",
    "extra_charges_master",
    "stock_batches",
    "stock_ledger",
    "purchases",
    "purchase_items",
    "purchase_returns",
    "purchase_return_items",
    "sales",
    "sale_items",
    "sales_returns",
    "sales_return_items",
    "ledger_accounts",
    "vouchers",
    "ledger_entries",
    "expense_categories",
    "expenses",
    "users",
    "company_settings",
    "audit_log",
    "backup_log",
    "schema_version",
}


def _sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# =====================================================================
# 1. Existing Live Database Preservation, 31 Tables & FK Integrity (RO)
# =====================================================================
def test_live_database_preserved_31_tables_and_integrity():
    """
    Verify (read-only) that existing live data/agri_erp.db is preserved intact with
    all 31 tables, PRAGMA integrity_check == ['ok'], and zero foreign key violations.
    Skipped only if data/agri_erp.db does not exist (e.g. CI fresh clone without DB).
    """
    live_db = PROJECT_ROOT / "data" / "agri_erp.db"
    if not live_db.exists():
        pytest.skip("Original live data/agri_erp.db not present in environment")

    hash_before = _sha256(live_db)

    conn = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
            ).fetchall()
        }
        assert tables == ALL_31_TABLES, f"Missing/extra tables: {ALL_31_TABLES ^ tables}"
        assert len(tables) == 31

        integrity = [r[0] for r in conn.execute("PRAGMA integrity_check;").fetchall()]
        assert integrity == ["ok"]

        conn.execute("PRAGMA foreign_keys = ON;")
        fk_violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
        assert fk_violations == [], f"Unexpected FK violations: {fk_violations}"

        version = conn.execute("SELECT MAX(version_number) FROM schema_version;").fetchone()[0]
        assert version >= 1
    finally:
        conn.close()

    assert _sha256(live_db) == hash_before, "Read-only inspection must never alter live DB!"


# =====================================================================
# 2. Config, APP_MODE, AppData Locations & DB_PATH / BACKUP_DIR Overrides
# =====================================================================
def test_app_mode_and_storage_path_resolution(tmp_path):
    """Verify APP_MODE resolution, Windows AppData paths, and explicit overrides."""
    # Default -> local
    assert resolve_app_mode(env={}) == "local"
    assert resolve_app_mode(env={"VERCEL": "1"}) == "vercel"
    assert resolve_app_mode(env={"APP_MODE": "desktop", "VERCEL": "1"}) == "desktop"
    assert resolve_app_mode(env={"APP_MODE": "LOCAL"}) == "local"

    with pytest.raises(ValueError, match="Invalid APP_MODE"):
        resolve_app_mode(env={"APP_MODE": "invalid_mode"})

    # Windows %LOCALAPPDATA%\com.krushidhan.erp\data\agri_erp.db and backups
    win_localappdata = str(tmp_path / "Users" / "Owner" / "AppData" / "Local")
    win_env = {"APP_MODE": "desktop", "LOCALAPPDATA": win_localappdata}
    assert get_desktop_app_dir(env=win_env, platform_name="win32") == Path(win_localappdata) / "com.krushidhan.erp"
    assert resolve_db_path(env=win_env, project_root=tmp_path) == (
        Path(win_localappdata) / "com.krushidhan.erp" / "data" / "agri_erp.db"
    )
    assert resolve_backup_dir(env=win_env, project_root=tmp_path) == (
        Path(win_localappdata) / "com.krushidhan.erp" / "backups"
    )

    # DB_PATH and BACKUP_DIR overrides always win
    custom_db = tmp_path / "custom" / "shop.db"
    custom_bkp = tmp_path / "custom_backups"
    override_env = {**win_env, "DB_PATH": str(custom_db), "BACKUP_DIR": str(custom_bkp)}
    assert resolve_db_path(env=override_env, project_root=tmp_path) == custom_db
    assert resolve_backup_dir(env=override_env, project_root=tmp_path) == custom_bkp

    # Local & Vercel mode paths
    assert resolve_db_path(env={"APP_MODE": "local"}, project_root=tmp_path) == tmp_path / "data" / "agri_erp.db"
    assert resolve_backup_dir(env={"APP_MODE": "local"}, project_root=tmp_path) == tmp_path / "backups"
    assert resolve_db_path(env={"APP_MODE": "vercel"}, project_root=tmp_path) == Path("/tmp/agri_erp.db")
    assert resolve_backup_dir(env={"APP_MODE": "vercel"}, project_root=tmp_path) == Path("/tmp/krushidhan_backups")


# =====================================================================
# 3. Desktop Mode vs Vercel Mode (MongoDB Cloud Sync Isolation)
# =====================================================================
def test_desktop_mode_strictly_bypasses_mongodb_and_dotenv(tmp_path, monkeypatch):
    """
    Verify desktop mode never loads .env and never activates MongoDB/CloudSyncManager,
    even if MONGODB_URI or VERCEL environment variables are present.
    """
    reset_db_manager()
    reset_cloud_sync_manager()

    # Create fake .env in temp root to confirm load_runtime_dotenv skips it in desktop mode
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    (fake_root / ".env").write_text("MONGODB_URI=mongodb://should-never-load\n", encoding="utf-8")

    local_appdata = tmp_path / "AppDataLocal"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setenv("MONGODB_URI", "mongodb://invalid-host-must-never-be-called:27017")
    monkeypatch.setenv("VERCEL", "1")

    assert load_runtime_dotenv(app_mode="desktop", project_root=fake_root) is False
    assert is_cloud_sync_allowed() is False

    # Fail test immediately if pymongo.MongoClient is ever called in desktop mode
    import src.db.cloud_sync as cloud_mod

    monkeypatch.setattr(
        cloud_mod.pymongo,
        "MongoClient",
        MagicMock(side_effect=AssertionError("MongoClient must NEVER be invoked in desktop mode!")),
    )

    mgr = CloudSyncManager()
    assert mgr.is_cloud_enabled is False
    assert mgr.restore_db_snapshot(tmp_path / "anything.db") is False
    assert mgr.save_db_snapshot(tmp_path / "anything.db") is False

    global_mgr = get_cloud_sync_manager()
    assert global_mgr.is_cloud_enabled is False

    db = DatabaseManager()
    assert db.app_mode == "desktop"
    assert db.is_desktop is True
    assert db.is_vercel is False
    assert db._has_mongo is False
    assert db.db_path == local_appdata / "com.krushidhan.erp" / "data" / "agri_erp.db"
    assert db.backup_dir == local_appdata / "com.krushidhan.erp" / "backups"

    db.initialize_database(include_seed=True)
    # Write transaction must never call MongoDB
    db.execute_query(
        "INSERT INTO categories (category_name, short_name) VALUES (?, ?);",
        ("Desktop Offline Cat", "OFF"),
    )
    row = db.fetch_one("SELECT * FROM categories WHERE short_name = 'OFF';")
    assert row is not None

    reset_db_manager()
    reset_cloud_sync_manager()


def test_vercel_mode_retains_cloud_snapshot_restore_and_save(tmp_path, monkeypatch):
    """Verify Vercel mode retains cold-start MongoDB snapshot restore & save hooks cleanly."""
    reset_db_manager()
    reset_cloud_sync_manager()

    # Build a valid small SQLite snapshot bytes payload
    snap_src = tmp_path / "cloud_seed.db"
    src_mgr = DatabaseManager(db_path=snap_src, app_mode="local")
    src_mgr.initialize_database(include_seed=True)
    snap_src.write_bytes(snap_src.read_bytes())
    # Ensure WAL merged before capturing snapshot bytes
    c = sqlite3.connect(str(snap_src))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    c.close()
    snapshot_bytes = snap_src.read_bytes()

    stored_docs = {"agri_erp_main": {"_id": "agri_erp_main", "db_bytes": snapshot_bytes}}

    saved_event = threading.Event()
    fake_col = MagicMock()
    fake_col.find_one.side_effect = lambda q: stored_docs.get(q.get("_id"))

    def fake_replace_one(flt, doc, upsert=False):
        stored_docs[flt["_id"]] = doc
        saved_event.set()

    fake_col.replace_one.side_effect = fake_replace_one

    fake_client = MagicMock()
    fake_client.admin.command.return_value = {"ok": 1}
    fake_client.__getitem__.return_value = {"db_snapshots": fake_col}

    import src.db.cloud_sync as cloud_mod

    monkeypatch.setattr(cloud_mod.pymongo, "MongoClient", MagicMock(return_value=fake_client))

    vercel_tmp_db = tmp_path / "vercel_tmp_agri_erp.db"
    monkeypatch.setenv("APP_MODE", "vercel")
    monkeypatch.setenv("MONGODB_URI", "mongodb://mock-vercel-cluster")
    monkeypatch.delenv("DB_PATH", raising=False)

    # Restore directly on Vercel CloudSyncManager
    cloud_mgr = get_cloud_sync_manager()
    assert cloud_mgr.is_cloud_enabled is True
    assert is_cloud_sync_allowed(app_mode="vercel") is True

    restored = cloud_mgr.restore_db_snapshot(vercel_tmp_db)
    assert restored is True
    assert vercel_tmp_db.exists()

    # Perform transaction on Vercel DB — automatically triggers async cloud snapshot save
    v_db = DatabaseManager(db_path=vercel_tmp_db, app_mode="vercel")
    v_db.execute_query(
        "INSERT INTO categories (category_name, short_name) VALUES (?, ?);",
        ("Vercel Sync Category", "VSL"),
    )
    assert saved_event.wait(timeout=3.0) is True
    assert fake_col.replace_one.called
    # Wait briefly until lock releases and verify explicit synchronous snapshot save succeeds too
    with cloud_mgr._save_lock:
        pass
    assert cloud_mgr.save_db_snapshot(vercel_tmp_db) is True

    reset_db_manager()
    reset_cloud_sync_manager()


# =====================================================================
# 4. Online Backup API, Integrity Verification & Safe Restoration
# =====================================================================
def test_online_backup_integrity_and_safe_restoration(tmp_path, monkeypatch):
    """
    Verify SQLite online backup API, PRAGMA integrity_check enforcement,
    no overwriting of backups, rejection of corrupted backups, and recoverable
    pre-restore safety copy during WAL-aware restoration.
    """
    db_file = tmp_path / "live_shop.db"
    backup_dir = tmp_path / "shop_backups"
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))

    db = DatabaseManager(db_path=db_file, app_mode="local", clean_install=True)
    db.initialize_database(include_seed=True)

    master = MasterDataRepository(db)
    p1_id = master.create_product(Product(product_name="Urea 45kg Before Backup", default_sale_rate=266.5))

    svc = BackupService(db)
    assert svc.get_backup_dir() == backup_dir

    # Create Backup #1 using SQLite online backup API while WAL is active
    bkp_1 = svc.create_backup(backup_type="MANUAL")
    assert bkp_1.exists()
    assert bkp_1.parent == backup_dir
    assert svc.verify_database_integrity(bkp_1, check_foreign_keys=True) is True
    inspection = svc.inspect_backup(bkp_1)
    assert inspection["table_count"] in (31, 33)
    assert inspection["integrity_ok"] is True
    assert inspection["foreign_key_ok"] is True

    # Creating Backup #2 within the same second MUST NOT overwrite Backup #1
    master.create_product(Product(product_name="DAP 50kg Modified After Backup", default_sale_rate=1350.0))
    bkp_2 = svc.create_backup(backup_type="MANUAL")
    assert bkp_2.exists()
    assert bkp_1.exists()
    assert bkp_2 != bkp_1

    # Add a third record that exists ONLY in the live DB prior to restore
    master.create_product(Product(product_name="Potash Latest Live Record", default_sale_rate=1700.0))
    assert db.fetch_one("SELECT * FROM products WHERE product_name = 'Potash Latest Live Record';") is not None

    # Corrupt backup file MUST be rejected and live DB untouched
    corrupt_file = tmp_path / "corrupt_backup.db"
    corrupt_file.write_bytes(b"NOT A VALID SQLITE DATABASE" * 100)
    assert svc.verify_database_integrity(corrupt_file) is False
    with pytest.raises(ValueError, match="integrity_check|SQLite"):
        svc.restore_backup(corrupt_file)
    assert db.fetch_one("SELECT * FROM products WHERE product_name = 'Potash Latest Live Record';") is not None

    # Non-ERP empty SQLite DB MUST be rejected before touching live DB
    empty_sqlite = tmp_path / "empty_unrelated.db"
    sqlite3.connect(str(empty_sqlite)).close()
    with pytest.raises(ValueError):
        svc.restore_backup(empty_sqlite)

    # Restore safely back to Backup #1
    assert svc.restore_backup(bkp_1) is True
    assert svc.last_pre_restore_backup is not None
    assert svc.last_pre_restore_backup.exists()
    assert svc.verify_database_integrity(svc.last_pre_restore_backup) is True

    # Live DB now has Backup #1 state cleanly (p1 exists, post-backup products absent)
    assert db.fetch_one("SELECT * FROM products WHERE product_id = ?;", (p1_id,)) is not None
    assert db.fetch_one("SELECT * FROM products WHERE product_name = 'Potash Latest Live Record';") is None
    assert db.check_integrity() == ["ok"]
    assert db.check_foreign_keys() == []

    # Recoverable pre-restore copy MUST still retain 'Potash Latest Live Record'
    pre_restore_mgr = DatabaseManager(db_path=svc.last_pre_restore_backup, app_mode="local")
    assert (
        pre_restore_mgr.fetch_one("SELECT * FROM products WHERE product_name = 'Potash Latest Live Record';")
        is not None
    )


def test_backup_dir_resolution_in_desktop_mode(tmp_path, monkeypatch):
    """Verify BackupService automatically writes to %LOCALAPPDATA%\\com.krushidhan.erp\\backups in desktop mode."""
    reset_db_manager()
    local_appdata = tmp_path / "WinLocalAppData"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    db = DatabaseManager()
    db.initialize_database(include_seed=True)
    svc = BackupService(db)
    bkp = svc.create_backup()

    expected_dir = local_appdata / "com.krushidhan.erp" / "backups"
    assert bkp.parent == expected_dir
    assert svc.verify_database_integrity(bkp) is True
    reset_db_manager()


# =====================================================================
# 5. Clean First-Run New Shop Installation & First-Run Owner Onboarding
# =====================================================================
def test_clean_new_shop_desktop_setup_flow(tmp_path, monkeypatch):
    """
    Verify desktop clean first-run setup:
    - Zero developer shop records (no कृषीधन, no आकाश लेंगारे, no personal GSTIN/phone)
    - Zero predictable default passwords/users seeded (krushidhan@2026 / billing123 rejected)
    - Shop owner creates their own Administrator account via /api/auth/setup-admin
    - Second setup-admin call is permanently blocked (403)
    """
    reset_db_manager()
    reset_cloud_sync_manager()

    local_appdata = tmp_path / "CustomerPC" / "Local"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    with TestClient(app) as client:
        db = get_db_manager()
        assert db.app_mode == "desktop"
        assert db.clean_install_mode is True

        # All 31 tables must exist cleanly
        tables = {
            r["name"]
            for r in db.fetch_all(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
            )
        }
        assert tables in (ALL_31_TABLES, ALL_31_TABLES | {"supplier_credits", "purchase_return_settlements"})

        # Generic lookups + double-entry COA seeded
        assert len(db.fetch_all("SELECT * FROM units;")) >= 9
        assert len(db.fetch_all("SELECT * FROM tax_groups;")) >= 5
        assert len(db.fetch_all("SELECT * FROM ledger_accounts;")) >= 15

        # ZERO users & ZERO developer shop records
        assert db.fetch_one("SELECT COUNT(*) AS cnt FROM users;")["cnt"] == 0
        assert db.fetch_one("SELECT COUNT(*) AS cnt FROM company_settings;")["cnt"] == 0

        # Ensure AuthService does NOT backdoor bootstrap users in desktop mode
        auth_svc = AuthService(db)
        auth_svc.ensure_bootstrap_users()
        assert db.fetch_one("SELECT COUNT(*) AS cnt FROM users;")["cnt"] == 0

        # Default predictable credentials fail
        bad_login = client.post("/api/auth/login", json={"username": "admin", "password": "krushidhan@2026"})
        assert bad_login.status_code == 401
        bad_login_2 = client.post("/api/auth/login", json={"username": "billing", "password": "billing123"})
        assert bad_login_2.status_code == 401

        # Setup status says onboarding required & contains zero developer details
        status_res = client.get("/api/auth/setup-status")
        assert status_res.status_code == 200
        st = status_res.json()
        assert st["needs_setup"] is True
        assert st["has_company_profile"] is False
        assert st["app_mode"] == "desktop"
        assert "कृषीधन" not in st["shop_name"]
        assert "9503" not in st["shop_mobile"]

        # Protected endpoints stay locked until owner setup completes
        assert client.get("/api/masters/products").status_code == 401

        # Weak short password rejected
        weak_res = client.post(
            "/api/auth/setup-admin",
            json={
                "username": "mauli_owner",
                "password": "123",
                "full_name": "Suresh Deshmukh",
                "shop_name": "Mauli Krishi Seva Kendra",
            },
        )
        assert weak_res.status_code == 400

        # Valid owner administrator setup succeeds
        setup_res = client.post(
            "/api/auth/setup-admin",
            json={
                "username": "mauli_owner",
                "password": "StrongShopPassword#2026",
                "full_name": "Suresh Deshmukh",
                "shop_name": "Mauli Krishi Seva Kendra",
                "mobile": "9822001100",
                "city": "Baramati",
            },
        )
        assert setup_res.status_code == 200
        setup_data = setup_res.json()
        assert setup_data["success"] is True
        assert setup_data["user"]["username"] == "mauli_owner"
        assert setup_data["user"]["role"] == "ADMIN"
        assert setup_data["shop"]["name"] == "Mauli Krishi Seva Kendra"
        assert "कृषीधन" not in setup_data["shop"]["name"]

        # Stored password hash MUST use bcrypt ($2...)
        stored_user = db.fetch_one("SELECT password_hash FROM users WHERE username = 'mauli_owner';")
        assert stored_user is not None
        assert stored_user["password_hash"].startswith("$2")

        # Subsequent setup-admin attempt is permanently forbidden
        second_setup = client.post(
            "/api/auth/setup-admin",
            json={
                "username": "attacker",
                "password": "AttackerPassword123",
                "full_name": "Hacker",
                "shop_name": "Evil Shop",
            },
        )
        assert second_setup.status_code == 403

        # Setup status is now False and reflects customer's shop
        status_after = client.get("/api/auth/setup-status").json()
        assert status_after["needs_setup"] is False
        assert status_after["has_company_profile"] is True
        assert status_after["shop_name"] == "Mauli Krishi Seva Kendra"

        # Token issued by setup works cleanly for protected routes
        token = setup_data["token"]
        me_res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me_res.status_code == 200
        assert me_res.json()["user"]["username"] == "mauli_owner"

        # Default InvoicePrinter does not inject developer details when settings blank
        blank_printer = InvoicePrinter()
        assert blank_printer.settings.gstin is None
        assert "कृषीधन" not in blank_printer.settings.company_name

    reset_db_manager()
    reset_cloud_sync_manager()


# =====================================================================
# 6. Non-Destructive Versioned Migrations & Pre-Migration Backups
# =====================================================================
def test_migration_runner_creates_backups_and_blocks_destructive_sql(tmp_path):
    """
    Verify:
    - Safe migrations back up database BEFORE EACH migration step via online backup API
    - Schema versions and data survive cleanly across upgrades
    - Destructive DDL/DML is rejected before modifying anything
    - Failed migrations roll back atomically leaving pre-migration backup intact
    """
    db_file = tmp_path / "migrate_shop.db"
    bkp_dir = tmp_path / "migrate_backups"

    db = DatabaseManager(db_path=db_file, app_mode="local", backup_dir=bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)
    init_version = db.get_schema_version()
    assert init_version == LATEST_SCHEMA_VERSION

    master = MasterDataRepository(db)
    prod_id = master.create_product(Product(product_name="Migration Protected Product", default_sale_rate=500.0))

    migrations = [
        Migration(
            version=init_version + 1,
            description="Add audit_log created_at index",
            statements=("CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at);",),
        ),
        Migration(
            version=init_version + 2,
            description="Add non-destructive notes column to customers",
            statements=("ALTER TABLE customers ADD COLUMN notes TEXT DEFAULT '';",),
        ),
    ]

    report = db.run_migrations(migrations=migrations, backup_dir=bkp_dir)
    assert report.previous_version == init_version
    assert report.current_version == init_version + 2
    assert report.applied_versions == [init_version + 1, init_version + 2]
    assert len(report.backups_created) == 2  # One BEFORE_UPGRADE backup BEFORE EACH migration!
    for bkp_path in report.backups_created:
        assert bkp_path.exists()
        assert bkp_path.parent == bkp_dir
        assert "before_upgrade" in bkp_path.name
        assert BackupService(db).verify_database_integrity(bkp_path) is True

    # Data & schema integrity remain intact at v5/v6
    assert db.get_schema_version() == init_version + 2
    assert db.fetch_one("SELECT * FROM products WHERE product_id = ?;", (prod_id,)) is not None
    assert db.check_integrity() == ["ok"]
    assert db.check_foreign_keys() == []

    # Re-running is idempotent (0 new backups, 0 applied versions)
    report_2 = db.run_migrations(migrations=migrations, backup_dir=bkp_dir)
    assert report_2.applied_versions == []
    assert report_2.backups_created == []

    # Destructive migrations must be blocked BEFORE touching DB or creating changes
    for bad_sql in (
        "DROP TABLE products;",
        "DROP VIEW IF EXISTS some_view;",
        "TRUNCATE TABLE customers;",
        "ALTER TABLE products DROP COLUMN hsn_code;",
        "DELETE FROM products;",
        "DELETE FROM products WHERE 1=1;",
        "UPDATE products SET default_sale_rate = 0;",
        "UPDATE products SET default_sale_rate = 0 WHERE 1=1;",
    ):
        with pytest.raises(MigrationSafetyError):
            assert_non_destructive_sql(bad_sql)

    unsafe_migration = [
        *migrations,
        Migration(
            version=init_version + 3,
            description="Destructive attempt",
            statements=("DROP TABLE products;",),
        ),
    ]
    with pytest.raises(MigrationSafetyError, match="Unsafe migration rejected"):
        db.run_migrations(migrations=unsafe_migration, backup_dir=bkp_dir)
    assert db.get_schema_version() == init_version + 2
    assert db.fetch_one("SELECT * FROM products WHERE product_id = ?;", (prod_id,)) is not None

    # Failed mid-migration rolls back atomically and leaves pre-upgrade backup available
    failing_migration = [
        *migrations,
        Migration(
            version=init_version + 3,
            description="Failing runtime migration",
            statements=(
                "CREATE TABLE IF NOT EXISTS temp_mig_test (id INTEGER PRIMARY KEY);",
                "SELECT * FROM non_existent_table_triggering_error;",
            ),
        ),
    ]
    with pytest.raises(MigrationSafetyError, match="failed cleanly with rollback"):
        db.run_migrations(migrations=failing_migration, backup_dir=bkp_dir)

    assert db.get_schema_version() == init_version + 2
    assert (
        db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='temp_mig_test';") is None
    )
    assert db.check_integrity() == ["ok"]


# =====================================================================
# 7. System Runtime Info, Restore Endpoints & Git Data Protection
# =====================================================================
def test_system_runtime_and_restore_endpoints(tmp_path, monkeypatch):
    """Verify admin-only /api/system/runtime-info and /api/system/backup/restore APIs."""
    reset_db_manager()
    reset_cloud_sync_manager()

    local_appdata = tmp_path / "ShopRuntimeData"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    with TestClient(app) as client:
        setup_res = client.post(
            "/api/auth/setup-admin",
            json={
                "username": "shop_admin",
                "password": "StrongPassword#99",
                "full_name": "Shop Owner",
                "shop_name": "Green Valley Agro",
            },
        )
        assert setup_res.status_code == 200
        admin_headers = {"Authorization": f"Bearer {setup_res.json()['token']}"}

        info_res = client.get("/api/system/runtime-info", headers=admin_headers)
        assert info_res.status_code == 200
        info = info_res.json()
        assert info["app_mode"] == "desktop"
        assert info["cloud_sync_enabled"] is False
        assert str(local_appdata / "com.krushidhan.erp" / "data" / "agri_erp.db") == info["db_path"]
        assert str(local_appdata / "com.krushidhan.erp" / "backups") == info["backup_dir"]
        assert info["schema_version"] >= 1

        # Trigger backup & insert subsequent product, then restore via API
        bkp_res = client.post("/api/system/backup", headers=admin_headers)
        assert bkp_res.status_code == 200
        bkp_filename = bkp_res.json()["filename"]

        prod_res = client.post(
            "/api/masters/products",
            headers=admin_headers,
            json={
                "product_name": "Post-Backup Ephemeral Product",
                "category_id": 1,
                "unit_id": 2,
                "tax_group_id": 2,
                "default_purchase_rate": 100.0,
                "default_sale_rate": 120.0,
                "default_mrp": 120.0,
            },
        )
        assert prod_res.status_code == 200

        # Missing confirmation must be rejected (Phase 2C-2 hardening)
        unconfirmed_res = client.post(
            "/api/system/backup/restore",
            headers=admin_headers,
            json={"backup_filename": bkp_filename},
        )
        assert unconfirmed_res.status_code == 400

        restore_res = client.post(
            "/api/system/backup/restore",
            headers=admin_headers,
            json={"backup_filename": bkp_filename, "confirm_restore": True},
        )
        assert restore_res.status_code == 200
        rdata = restore_res.json()
        assert rdata["success"] is True
        assert rdata["pre_restore_backup"] is not None
        assert Path(rdata["pre_restore_backup"]).exists()

        # Verify restored DB no longer contains post-backup product
        prods_after = client.get("/api/masters/products", headers=admin_headers).json()
        assert all(p["product_name"] != "Post-Backup Ephemeral Product" for p in prods_after)

        # Operator role cannot call runtime-info or restore
        op_token = AuthService.create_access_token(999, "op_user", "Operator", "OPERATOR")
        op_headers = {"Authorization": f"Bearer {op_token}"}
        assert client.get("/api/system/runtime-info", headers=op_headers).status_code == 403
        assert (
            client.post(
                "/api/system/backup/restore",
                headers=op_headers,
                json={"backup_filename": bkp_filename, "confirm_restore": True},
            ).status_code
            == 403
        )

    reset_db_manager()
    reset_cloud_sync_manager()


def test_git_data_protection():
    """Verify Git index tracks zero SQLite DB/WAL/SHM or backup files and .gitignore ignores them."""
    ls_proc = subprocess.run(
        ["git", "ls-files"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    tracked_files = [line.strip() for line in ls_proc.stdout.splitlines() if line.strip()]
    forbidden_tracked = [
        f
        for f in tracked_files
        if f.endswith((".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3"))
        or f.startswith("backups/")
        or f.startswith("data/backups/")
    ]
    assert forbidden_tracked == [], f"Database/backup files must not be tracked in Git: {forbidden_tracked}"

    for sample_untracked in (
        "data/agri_erp.db",
        "data/agri_erp.db-wal",
        "data/agri_erp.db-shm",
        "backups/agri_erp_backup_20260916_135957.db",
        "random_shop.sqlite3",
    ):
        chk = subprocess.run(
            ["git", "check-ignore", "-q", sample_untracked],
            cwd=str(PROJECT_ROOT),
        )
        assert chk.returncode == 0, f"Expected .gitignore to ignore {sample_untracked}"

