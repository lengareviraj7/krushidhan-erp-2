"""
Phase 2B-5D-2 Comprehensive Isolated Verification Suite:
Backend Idempotency for Customer Receipts, Supplier Payments, and Daily Shop Expenses (Migration v6).

All tests run strictly against isolated temporary SQLite databases (tmp_path).
Never touches data/agri_erp.db or backups/.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, reset_db_manager
from src.db.migrations import (
    BUILTIN_MIGRATIONS,
    Migration,
    MigrationRunner,
    MigrationSafetyError,
    reviewed_migration_handler,
)
from src.idempotency import IdempotencyConflictError
from src.models.accounting import Expense
from src.models.master_data import Customer, Supplier
from src.repositories.accounting_repository import AccountingRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.services.accounting_service import AccountingService
from src.services.auth_service import AuthService
from src.services.backup_service import BackupService

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ============================================================================
# Fixtures & Helpers
# ============================================================================

def _build_pre_v6_database(db_path: Path) -> None:
    """Create a realistic v5 database (without v6 columns/indexes on vouchers & expenses) with historical rows."""
    schema_sql = (PROJECT_ROOT / "src" / "db" / "schema.sql").read_text(encoding="utf-8")

    # Strip v6 idempotency columns from vouchers and expenses only (keeping v5 on sales & purchases)
    def _strip_table_v6_cols(sql_text: str, table_marker: str) -> str:
        idx = sql_text.find(table_marker)
        assert idx >= 0, f"Marker not found: {table_marker}"
        end_idx = sql_text.find(");", idx)
        block = sql_text[idx:end_idx]
        stripped = block.replace(
            "    created_at TEXT DEFAULT CURRENT_TIMESTAMP,\n    idempotency_key TEXT,\n    request_fingerprint TEXT",
            "    created_at TEXT DEFAULT CURRENT_TIMESTAMP",
        )
        return sql_text[:idx] + stripped + sql_text[end_idx:]

    v5_sql = _strip_table_v6_cols(schema_sql, "CREATE TABLE IF NOT EXISTS vouchers (")
    v5_sql = _strip_table_v6_cols(v5_sql, "CREATE TABLE IF NOT EXISTS expenses (")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(v5_sql)
        # Mark v2..v5 applied and create v5 partial unique indexes
        for v in (2, 3, 4, 5):
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version_number, description) VALUES (?, ?);",
                (v, f"v{v}"),
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_sales_idempotency_key "
            "ON sales(idempotency_key) WHERE idempotency_key IS NOT NULL;"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_purchases_idempotency_key "
            "ON purchases(idempotency_key) WHERE idempotency_key IS NOT NULL;"
        )
        # Seed baseline accounts, category, customer, supplier, historical voucher & historical expense
        conn.execute(
            "INSERT INTO ledger_accounts (account_id, account_name, account_group, is_system) "
            "VALUES (1, 'Cash in Hand', 'CASH', 1);"
        )
        conn.execute(
            "INSERT INTO ledger_accounts (account_id, account_name, account_group, is_system) "
            "VALUES (2, 'Sundry Debtors', 'ASSETS', 1);"
        )
        conn.execute(
            "INSERT INTO ledger_accounts (account_id, account_name, account_group, is_system) "
            "VALUES (3, 'Operating Expenses', 'EXPENSE', 1);"
        )
        conn.execute(
            "INSERT INTO expense_categories (category_id, category_name) VALUES (1, 'Shop Rent');"
        )
        conn.execute(
            "INSERT INTO vouchers (voucher_no, voucher_date, voucher_type, total_amount, narration, reference_type, reference_id) "
            "VALUES ('RCP-00001', '2026-10-01', 'RECEIPT', 750.0, 'Historical receipt', 'MANUAL', 1);"
        )
        conn.execute(
            "INSERT INTO ledger_entries (voucher_id, account_id, debit_amount, credit_amount, particulars) "
            "VALUES (1, 1, 750.0, 0.0, 'Historical cash Dr'), (1, 2, 0.0, 750.0, 'Historical debtors Cr');"
        )
        conn.execute(
            "INSERT INTO expenses (category_id, expense_date, amount, payment_account_id, reference_no, remarks) "
            "VALUES (1, '2026-10-01', 300.0, 1, 'RENT-OCT', 'Historical shop rent');"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def acct_env(tmp_path, monkeypatch) -> Dict[str, Any]:
    """Fresh isolated v6 SQLite database with 1 customer (bal=5000), 1 supplier (bal=8000), accounts, and auth token."""
    db_path = tmp_path / "accounting_idem_v6.db"
    bkp_dir = tmp_path / "backups"
    bkp_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    reset_db_manager()

    db = DatabaseManager(db_path=db_path, app_mode="local", backup_dir=bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)

    master = MasterDataRepository(db)
    cust_id = master.create_customer(
        Customer(customer_name="Ramesh Patil", mobile="9876543210", opening_balance=5000.0)
    )
    supp_id = master.create_supplier(
        Supplier(supplier_name="Agro Dist Ltd", mobile="9999900001", opening_balance=8000.0)
    )
    cat_row = db.fetch_one("SELECT category_id FROM expense_categories ORDER BY category_id ASC LIMIT 1;")
    assert cat_row is not None
    cat_id = int(cat_row["category_id"])

    auth = AuthService(db)
    token = auth.create_access_token(1, "admin", "Administrator", "ADMIN")
    headers = {"Authorization": f"Bearer {token}"}

    yield {
        "db": db,
        "db_path": db_path,
        "bkp_dir": bkp_dir,
        "cust_id": cust_id,
        "supp_id": supp_id,
        "cat_id": cat_id,
        "svc": AccountingService(db),
        "repo": AccountingRepository(db),
        "headers": headers,
    }
    reset_db_manager()


# ============================================================================
# MIGRATION v6 TESTS (Tests 1 - 3)
# ============================================================================

def test_01_v5_to_v6_upgrade_preserves_history_null_keys_indexes_and_backup(tmp_path):
    """v5 -> v6 upgrade creates verified backup, preserves legacy rows with NULL keys, and adds partial unique indexes."""
    v5_db = tmp_path / "upgrade_v5_to_v6.db"
    bkp_dir = tmp_path / "upgrade_bkps"
    _build_pre_v6_database(v5_db)

    db_mgr = DatabaseManager(db_path=v5_db, backup_dir=bkp_dir)
    assert db_mgr.get_schema_version() == 5

    v6_migrations = [m for m in BUILTIN_MIGRATIONS if m.version <= 6]
    report = MigrationRunner(db_mgr).run_pending_migrations(migrations=v6_migrations, backup_dir=bkp_dir)
    assert report.previous_version == 5
    assert report.current_version == 6
    assert report.applied_versions == [6]
    assert len(report.backups_created) == 1
    assert report.backups_created[0].exists()
    assert BackupService(db_mgr, backup_dir=bkp_dir).verify_database_integrity(
        report.backups_created[0], check_foreign_keys=True
    )

    assert db_mgr.get_schema_version() == 6
    assert db_mgr.check_integrity() == ["ok"]
    assert db_mgr.check_foreign_keys() == []

    # Historical rows preserved verbatim with NULL idempotency_key & request_fingerprint
    v_row = db_mgr.fetch_one(
        "SELECT voucher_no, total_amount, idempotency_key, request_fingerprint FROM vouchers;"
    )
    assert v_row["voucher_no"] == "RCP-00001"
    assert v_row["total_amount"] == pytest.approx(750.0)
    assert v_row["idempotency_key"] is None
    assert v_row["request_fingerprint"] is None

    e_row = db_mgr.fetch_one(
        "SELECT reference_no, amount, idempotency_key, request_fingerprint FROM expenses;"
    )
    assert e_row["reference_no"] == "RENT-OCT"
    assert e_row["amount"] == pytest.approx(300.0)
    assert e_row["idempotency_key"] is None
    assert e_row["request_fingerprint"] is None

    # Verify partial unique indexes allow multiple NULLs and block duplicate non-NULL keys
    idxs = {
        r["name"]: r["sql"]
        for r in db_mgr.fetch_all(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL;"
        )
    }
    assert "uq_vouchers_idempotency_key" in idxs
    assert "WHERE idempotency_key IS NOT NULL" in idxs["uq_vouchers_idempotency_key"]
    assert "uq_expenses_idempotency_key" in idxs
    assert "WHERE idempotency_key IS NOT NULL" in idxs["uq_expenses_idempotency_key"]

    # Multiple NULL keys permitted
    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        conn.execute(
            "INSERT INTO vouchers (voucher_no, voucher_date, voucher_type, total_amount, idempotency_key) "
            "VALUES ('RCP-00002', '2026-10-02', 'RECEIPT', 100.0, NULL);"
        )
        conn.execute(
            "INSERT INTO expenses (category_id, expense_date, amount, idempotency_key) "
            "VALUES (1, '2026-10-02', 50.0, NULL);"
        )

    # Duplicate non-NULL keys rejected by partial unique index
    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        conn.execute(
            "INSERT INTO vouchers (voucher_no, voucher_date, voucher_type, total_amount, idempotency_key) "
            "VALUES ('RCP-00003', '2026-10-03', 'RECEIPT', 100.0, 'V-DUP-KEY');"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with db_mgr.transaction(mode="IMMEDIATE") as conn:
            conn.execute(
                "INSERT INTO vouchers (voucher_no, voucher_date, voucher_type, total_amount, idempotency_key) "
                "VALUES ('RCP-00004', '2026-10-03', 'RECEIPT', 100.0, 'V-DUP-KEY');"
            )

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        conn.execute(
            "INSERT INTO expenses (category_id, expense_date, amount, idempotency_key) "
            "VALUES (1, '2026-10-03', 50.0, 'E-DUP-KEY');"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with db_mgr.transaction(mode="IMMEDIATE") as conn:
            conn.execute(
                "INSERT INTO expenses (category_id, expense_date, amount, idempotency_key) "
                "VALUES (1, '2026-10-03', 50.0, 'E-DUP-KEY');"
            )


def test_02_v6_migration_idempotent_and_clean_install_schema_parity(tmp_path):
    """Re-running Migration v6 is a no-op; clean install and v5-upgraded DB have identical columns & indexes."""
    upgraded_path = tmp_path / "upgraded_v6.db"
    upg_bkps = tmp_path / "upg_bkps"
    _build_pre_v6_database(upgraded_path)
    upg_db = DatabaseManager(db_path=upgraded_path, backup_dir=upg_bkps)
    runner = MigrationRunner(upg_db)
    v6_migs = [m for m in BUILTIN_MIGRATIONS if m.version <= 6]

    r1 = runner.run_pending_migrations(migrations=v6_migs, backup_dir=upg_bkps)
    assert r1.applied_versions == [6]
    assert len(r1.backups_created) == 1

    # Second run is a clean no-op with 0 backups
    r2 = runner.run_pending_migrations(migrations=v6_migs, backup_dir=upg_bkps)
    assert r2.applied_versions == []
    assert r2.backups_created == []
    assert r2.current_version == 6

    # Clean install DB
    clean_path = tmp_path / "clean_v6.db"
    clean_bkps = tmp_path / "clean_bkps"
    clean_db = DatabaseManager(db_path=clean_path, backup_dir=clean_bkps, clean_install=True)
    clean_db.initialize_database(include_seed=True)
    assert clean_db.get_schema_version() == 6

    for table in ("vouchers", "expenses"):
        upg_cols = [(r["name"], r["type"]) for r in upg_db.fetch_all(f"PRAGMA table_info({table});")]
        cln_cols = [(r["name"], r["type"]) for r in clean_db.fetch_all(f"PRAGMA table_info({table});")]
        assert upg_cols == cln_cols, f"Column mismatch on {table}: {upg_cols} vs {cln_cols}"
        assert upg_cols[-2:] == [("idempotency_key", "TEXT"), ("request_fingerprint", "TEXT")]

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


def test_03_v6_migration_rollback_on_failure_preserves_pre_upgrade_backup(tmp_path):
    """If a migration handler fails mid-way, MigrationRunner rolls back atomically and leaves backup intact."""
    v5_db = tmp_path / "rollback_v5.db"
    bkp_dir = tmp_path / "rollback_bkps"
    _build_pre_v6_database(v5_db)
    db_mgr = DatabaseManager(db_path=v5_db, backup_dir=bkp_dir)

    @reviewed_migration_handler(reviewer="test", reason="Intentional rollback verification")
    def failing_v6(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE vouchers ADD COLUMN idempotency_key TEXT;")
        raise RuntimeError("Simulated migration crash mid-v6")

    with pytest.raises(MigrationSafetyError, match="rolled back|rollback"):
        MigrationRunner(db_mgr).run_pending_migrations(
            migrations=[Migration(version=6, description="Failing v6 test", handler=failing_v6)],
            backup_dir=bkp_dir,
        )

    assert db_mgr.get_schema_version() == 5
    v_cols = {r["name"] for r in db_mgr.fetch_all("PRAGMA table_info(vouchers);")}
    assert "idempotency_key" not in v_cols
    backups = list(bkp_dir.glob("agri_erp_before_upgrade_*.db"))
    assert len(backups) == 1
    assert BackupService(db_mgr, backup_dir=bkp_dir).verify_database_integrity(backups[0], check_foreign_keys=True)


# ============================================================================
# CUSTOMER RECEIPT TESTS (Tests 4 - 9)
# ============================================================================

def test_04_customer_receipt_same_key_replays_with_zero_side_effects(acct_env):
    db, svc, cust_id = acct_env["db"], acct_env["svc"], acct_env["cust_id"]
    key = f"RCP-{uuid.uuid4()}"

    v1, no1, rep1 = svc.record_customer_receipt_with_status(
        customer_id=cust_id, receipt_date="2026-11-17", amount=1200.0,
        payment_mode="CASH", narration="Part payment", idempotency_key=key,
    )
    assert rep1 is False
    assert no1.startswith("RCP-")

    bal_after_1 = db.fetch_one("SELECT current_balance FROM customers WHERE customer_id = ?;", (cust_id,))["current_balance"]
    cash_after_1 = db.fetch_one("SELECT current_balance FROM ledger_accounts WHERE account_name = 'Cash in Hand';")["current_balance"]
    vch_cnt_1 = db.fetch_one("SELECT COUNT(*) AS c FROM vouchers;")["c"]
    le_cnt_1 = db.fetch_one("SELECT COUNT(*) AS c FROM ledger_entries;")["c"]
    assert bal_after_1 == pytest.approx(3800.0)

    # Replay with exact same key & payload
    v2, no2, rep2 = svc.record_customer_receipt_with_status(
        customer_id=cust_id, receipt_date="2026-11-17", amount=1200.0,
        payment_mode="cash", narration="  Part payment  ", idempotency_key=key,
    )
    assert rep2 is True
    assert v2 == v1
    assert no2 == no1

    assert db.fetch_one("SELECT current_balance FROM customers WHERE customer_id = ?;", (cust_id,))["current_balance"] == pytest.approx(bal_after_1)
    assert db.fetch_one("SELECT current_balance FROM ledger_accounts WHERE account_name = 'Cash in Hand';")["current_balance"] == pytest.approx(cash_after_1)
    assert db.fetch_one("SELECT COUNT(*) AS c FROM vouchers;")["c"] == vch_cnt_1
    assert db.fetch_one("SELECT COUNT(*) AS c FROM ledger_entries;")["c"] == le_cnt_1


def test_05_customer_receipt_changed_payload_and_cross_op_key_conflict(acct_env):
    db, svc, cust_id, supp_id = acct_env["db"], acct_env["svc"], acct_env["cust_id"], acct_env["supp_id"]
    key_rcp = f"RCP-CONF-{uuid.uuid4()}"
    svc.record_customer_receipt_with_status(
        customer_id=cust_id, receipt_date="2026-11-17", amount=500.0,
        payment_mode="CASH", narration="Note A", idempotency_key=key_rcp,
    )
    bal_before = db.fetch_one("SELECT current_balance FROM customers WHERE customer_id = ?;", (cust_id,))["current_balance"]
    vch_before = db.fetch_one("SELECT COUNT(*) AS c FROM vouchers;")["c"]

    # Changed amount -> conflict
    with pytest.raises(IdempotencyConflictError):
        svc.record_customer_receipt_with_status(
            customer_id=cust_id, receipt_date="2026-11-17", amount=600.0,
            payment_mode="CASH", narration="Note A", idempotency_key=key_rcp,
        )

    # Cross-operation reuse: supplier payment key reused on customer receipt -> conflict!
    key_pay = f"SHARED-VCH-KEY-{uuid.uuid4()}"
    svc.record_supplier_payment_with_status(
        supplier_id=supp_id, amount=500.0, payment_date="2026-11-17",
        payment_mode="CASH", idempotency_key=key_pay,
    )
    with pytest.raises(IdempotencyConflictError):
        svc.record_customer_receipt_with_status(
            customer_id=cust_id, receipt_date="2026-11-17", amount=500.0,
            payment_mode="CASH", idempotency_key=key_pay,
        )

    # Zero side effects on customer balance
    assert db.fetch_one("SELECT current_balance FROM customers WHERE customer_id = ?;", (cust_id,))["current_balance"] == pytest.approx(bal_before)
    assert db.fetch_one("SELECT COUNT(*) AS c FROM vouchers;")["c"] == vch_before + 1  # only the valid supplier payment


def test_06_customer_receipt_six_thread_barrier_produces_single_voucher(acct_env):
    db, svc, cust_id = acct_env["db"], acct_env["svc"], acct_env["cust_id"]
    key = f"RCP-CONC-{uuid.uuid4()}"
    n = 6
    barrier = threading.Barrier(n)
    results: List[Any] = []
    errors: List[Exception] = []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait()
            res = svc.record_customer_receipt_with_status(
                customer_id=cust_id, receipt_date="2026-11-17", amount=1000.0,
                payment_mode="CASH", idempotency_key=key,
            )
            with lock:
                results.append(res)
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Unexpected concurrency errors: {errors}"
    assert len(results) == n
    assert len({r[0] for r in results}) == 1
    assert sum(1 for r in results if r[2] is False) == 1
    assert sum(1 for r in results if r[2] is True) == n - 1
    assert db.fetch_one("SELECT COUNT(*) AS c FROM vouchers WHERE voucher_type = 'RECEIPT';")["c"] == 1
    assert db.fetch_one("SELECT current_balance FROM customers WHERE customer_id = ?;", (cust_id,))["current_balance"] == pytest.approx(4000.0)


def test_07_customer_receipt_unknown_customer_rejected_with_no_writes(acct_env):
    db, svc = acct_env["db"], acct_env["svc"]
    key = f"RCP-NOCUST-{uuid.uuid4()}"
    vch_before = db.fetch_one("SELECT COUNT(*) AS c FROM vouchers;")["c"]
    le_before = db.fetch_one("SELECT COUNT(*) AS c FROM ledger_entries;")["c"]

    with pytest.raises(ValueError, match="Customer ID 999999 not found"):
        svc.record_customer_receipt_with_status(
            customer_id=999999, receipt_date="2026-11-17", amount=500.0,
            payment_mode="CASH", idempotency_key=key,
        )

    assert db.fetch_one("SELECT COUNT(*) AS c FROM vouchers;")["c"] == vch_before
    assert db.fetch_one("SELECT COUNT(*) AS c FROM ledger_entries;")["c"] == le_before
    assert db.fetch_one("SELECT * FROM vouchers WHERE idempotency_key = ?;", (key,)) is None


def test_08_customer_receipt_next_day_retry_with_omitted_date_replays(acct_env):
    svc, cust_id = acct_env["svc"], acct_env["cust_id"]
    key = f"RCP-MIDNIGHT-{uuid.uuid4()}"

    with patch("src.services.accounting_service.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 11, 17, 23, 59, 55)
        v1, no1, rep1 = svc.record_customer_receipt_with_status(
            customer_id=cust_id, receipt_date=None, amount=400.0,
            payment_mode="CASH", idempotency_key=key,
        )
        assert rep1 is False

        mock_dt.now.return_value = datetime(2026, 11, 18, 0, 0, 5)
        v2, no2, rep2 = svc.record_customer_receipt_with_status(
            customer_id=cust_id, receipt_date=None, amount=400.0,
            payment_mode="CASH", idempotency_key=key,
        )
        assert rep2 is True
        assert v2 == v1
        assert no2 == no1


def test_09_customer_receipt_failed_attempt_leaves_no_key_and_two_legitimate_succeed(acct_env):
    db, svc, cust_id = acct_env["db"], acct_env["svc"], acct_env["cust_id"]
    retry_key = f"RCP-RETRY-{uuid.uuid4()}"

    # Failed first attempt (amount <= 0) leaves no key
    with pytest.raises(ValueError):
        svc.record_customer_receipt_with_status(
            customer_id=cust_id, receipt_date="2026-11-17", amount=0.0,
            payment_mode="CASH", idempotency_key=retry_key,
        )
    assert db.fetch_one("SELECT * FROM vouchers WHERE idempotency_key = ?;", (retry_key,)) is None

    # Same key succeeds after fixing amount
    v1, _no1, rep1 = svc.record_customer_receipt_with_status(
        customer_id=cust_id, receipt_date="2026-11-17", amount=500.0,
        payment_mode="CASH", idempotency_key=retry_key,
    )
    assert rep1 is False

    # Second legitimate identical receipt with different key creates second voucher
    v2, _no2, rep2 = svc.record_customer_receipt_with_status(
        customer_id=cust_id, receipt_date="2026-11-17", amount=500.0,
        payment_mode="CASH", idempotency_key=f"RCP-LEGIT-2-{uuid.uuid4()}",
    )
    assert rep2 is False
    assert v2 != v1
    assert db.fetch_one("SELECT current_balance FROM customers WHERE customer_id = ?;", (cust_id,))["current_balance"] == pytest.approx(4000.0)


# ============================================================================
# SUPPLIER PAYMENT TESTS (Tests 10 - 13)
# ============================================================================

def test_10_supplier_payment_replay_and_conflict(acct_env):
    db, svc, supp_id = acct_env["db"], acct_env["svc"], acct_env["supp_id"]
    key = f"PAY-{uuid.uuid4()}"

    v1, no1, rep1 = svc.record_supplier_payment_with_status(
        supplier_id=supp_id, amount=2500.0, payment_date="2026-11-17",
        payment_mode="BANK_TRANSFER", reference_no="UTR-1001", narration="Bill settle",
        idempotency_key=key,
    )
    assert rep1 is False
    assert no1.startswith("PAY-")
    assert db.fetch_one("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supp_id,))["current_balance"] == pytest.approx(5500.0)

    # Replay returns same voucher, zero balance change
    v2, no2, rep2 = svc.record_supplier_payment_with_status(
        supplier_id=supp_id, amount=2500.0, payment_date="2026-11-17",
        payment_mode="bank_transfer", reference_no=" UTR-1001 ", narration="Bill settle",
        idempotency_key=key,
    )
    assert rep2 is True
    assert v2 == v1 and no2 == no1
    assert db.fetch_one("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supp_id,))["current_balance"] == pytest.approx(5500.0)

    # Changed payload -> conflict
    with pytest.raises(IdempotencyConflictError):
        svc.record_supplier_payment_with_status(
            supplier_id=supp_id, amount=2600.0, payment_date="2026-11-17",
            payment_mode="BANK_TRANSFER", reference_no="UTR-1001", narration="Bill settle",
            idempotency_key=key,
        )
    assert db.fetch_one("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supp_id,))["current_balance"] == pytest.approx(5500.0)


def test_11_supplier_payment_six_thread_barrier_produces_single_voucher(acct_env):
    db, svc, supp_id = acct_env["db"], acct_env["svc"], acct_env["supp_id"]
    key = f"PAY-CONC-{uuid.uuid4()}"
    n = 6
    barrier = threading.Barrier(n)
    results: List[Any] = []
    errors: List[Exception] = []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait()
            res = svc.record_supplier_payment_with_status(
                supplier_id=supp_id, amount=1500.0, payment_date="2026-11-17",
                payment_mode="CASH", idempotency_key=key,
            )
            with lock:
                results.append(res)
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len({r[0] for r in results}) == 1
    assert sum(1 for r in results if r[2] is False) == 1
    assert db.fetch_one("SELECT COUNT(*) AS c FROM vouchers WHERE voucher_type = 'PAYMENT';")["c"] == 1
    assert db.fetch_one("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supp_id,))["current_balance"] == pytest.approx(6500.0)


def test_12_supplier_payment_next_day_retry_with_omitted_date_replays(acct_env):
    svc, supp_id = acct_env["svc"], acct_env["supp_id"]
    key = f"PAY-MIDNIGHT-{uuid.uuid4()}"

    with patch("src.services.accounting_service.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 11, 17, 23, 59, 50)
        v1, no1, rep1 = svc.record_supplier_payment_with_status(
            supplier_id=supp_id, amount=900.0, payment_date=None,
            payment_mode="BANK_TRANSFER", idempotency_key=key,
        )
        assert rep1 is False

        mock_dt.now.return_value = datetime(2026, 11, 18, 0, 1, 0)
        v2, no2, rep2 = svc.record_supplier_payment_with_status(
            supplier_id=supp_id, amount=900.0, payment_date=None,
            payment_mode="BANK_TRANSFER", idempotency_key=key,
        )
        assert rep2 is True
        assert v2 == v1 and no2 == no1


def test_13_supplier_payment_failed_attempt_leaves_no_key_and_two_legitimate_succeed(acct_env):
    db, svc, supp_id = acct_env["db"], acct_env["svc"], acct_env["supp_id"]
    key1 = f"PAY-RETRY-{uuid.uuid4()}"

    # Unknown supplier fails and leaves no key
    with pytest.raises(ValueError, match="Supplier ID 888888 not found"):
        svc.record_supplier_payment_with_status(
            supplier_id=888888, amount=1000.0, payment_mode="CASH", idempotency_key=key1
        )
    assert db.fetch_one("SELECT * FROM vouchers WHERE idempotency_key = ?;", (key1,)) is None

    # Same key succeeds for valid supplier
    v1, _no1, rep1 = svc.record_supplier_payment_with_status(
        supplier_id=supp_id, amount=1000.0, payment_mode="CASH", idempotency_key=key1
    )
    assert rep1 is False

    # Second legitimate payment with different key succeeds
    v2, _no2, rep2 = svc.record_supplier_payment_with_status(
        supplier_id=supp_id, amount=1000.0, payment_mode="CASH", idempotency_key=f"PAY-LEGIT-2-{uuid.uuid4()}"
    )
    assert rep2 is False and v2 != v1
    assert db.fetch_one("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supp_id,))["current_balance"] == pytest.approx(6000.0)


# ============================================================================
# DAILY / SHOP EXPENSE TESTS (Tests 14 - 19)
# ============================================================================

def test_14_daily_expense_same_key_replays_original_and_child_voucher_has_null_key(acct_env):
    db, svc, cat_id = acct_env["db"], acct_env["svc"], acct_env["cat_id"]
    key = f"EXP-{uuid.uuid4()}"

    e1, v1, no1, rep1 = svc.record_daily_expense_with_status(
        category_id=cat_id, amount=350.0, expense_date="2026-11-17",
        payment_mode="CASH", reference_no="BILL-7", remarks="Tea & loading",
        idempotency_key=key,
    )
    assert rep1 is False
    assert e1 > 0 and v1 > 0 and no1.startswith("PAY-")

    # Verify idempotency_key stored ONLY on expenses, NOT on linked EXPENSE voucher
    exp_row = db.fetch_one("SELECT idempotency_key, request_fingerprint FROM expenses WHERE expense_id = ?;", (e1,))
    vch_row = db.fetch_one("SELECT idempotency_key, request_fingerprint, reference_type, reference_id FROM vouchers WHERE voucher_id = ?;", (v1,))
    assert exp_row["idempotency_key"] == key
    assert exp_row["request_fingerprint"] is not None
    assert vch_row["reference_type"] == "EXPENSE" and vch_row["reference_id"] == e1
    assert vch_row["idempotency_key"] is None
    assert vch_row["request_fingerprint"] is None

    cash_after_1 = db.fetch_one("SELECT current_balance FROM ledger_accounts WHERE account_name = 'Cash in Hand';")["current_balance"]

    # Replay returns original expense_id, voucher_id, voucher_no with zero side effects
    e2, v2, no2, rep2 = svc.record_daily_expense_with_status(
        category_id=cat_id, amount=350.0, expense_date="2026-11-17",
        payment_mode="cash", reference_no="BILL-7", remarks="Tea & loading",
        idempotency_key=key,
    )
    assert rep2 is True
    assert (e2, v2, no2) == (e1, v1, no1)
    assert db.fetch_one("SELECT COUNT(*) AS c FROM expenses;")["c"] == 1
    assert db.fetch_one("SELECT COUNT(*) AS c FROM vouchers WHERE reference_type = 'EXPENSE';")["c"] == 1
    assert db.fetch_one("SELECT current_balance FROM ledger_accounts WHERE account_name = 'Cash in Hand';")["current_balance"] == pytest.approx(cash_after_1)


def test_15_daily_expense_changed_payload_and_direct_payment_account_id_conflict(acct_env):
    svc, repo, cat_id = acct_env["svc"], acct_env["repo"], acct_env["cat_id"]
    key1 = f"EXP-CONF-{uuid.uuid4()}"
    svc.record_daily_expense_with_status(
        category_id=cat_id, amount=200.0, expense_date="2026-11-17",
        payment_mode="CASH", remarks="Freight", idempotency_key=key1,
    )
    # Changed payment mode -> conflict
    with pytest.raises(IdempotencyConflictError):
        svc.record_daily_expense_with_status(
            category_id=cat_id, amount=200.0, expense_date="2026-11-17",
            payment_mode="BANK_TRANSFER", remarks="Freight", idempotency_key=key1,
        )

    # Direct record_shop_expense_with_status with payment_account_id: different account_id -> conflict!
    cash_id = repo.get_cash_account().account_id
    bank_id = repo.get_bank_account().account_id
    key2 = f"EXP-DIRECT-{uuid.uuid4()}"
    svc.record_shop_expense_with_status(
        Expense(category_id=cat_id, expense_date="2026-11-17", amount=150.0, payment_account_id=cash_id, remarks="Direct"),
        idempotency_key=key2,
    )
    with pytest.raises(IdempotencyConflictError):
        svc.record_shop_expense_with_status(
            Expense(category_id=cat_id, expense_date="2026-11-17", amount=150.0, payment_account_id=bank_id, remarks="Direct"),
            idempotency_key=key2,
        )


def test_16_daily_expense_six_thread_barrier_produces_single_expense_and_voucher(acct_env):
    db, svc, cat_id = acct_env["db"], acct_env["svc"], acct_env["cat_id"]
    key = f"EXP-CONC-{uuid.uuid4()}"
    cash_before = db.fetch_one("SELECT current_balance FROM ledger_accounts WHERE account_name = 'Cash in Hand';")["current_balance"]
    n = 6
    barrier = threading.Barrier(n)
    results: List[Any] = []
    errors: List[Exception] = []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait()
            res = svc.record_daily_expense_with_status(
                category_id=cat_id, amount=500.0, expense_date="2026-11-17",
                payment_mode="CASH", remarks="Concurrent expense", idempotency_key=key,
            )
            with lock:
                results.append(res)
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len({r[0] for r in results}) == 1
    assert len({r[1] for r in results}) == 1
    assert sum(1 for r in results if r[3] is False) == 1
    assert db.fetch_one("SELECT COUNT(*) AS c FROM expenses;")["c"] == 1
    assert db.fetch_one("SELECT COUNT(*) AS c FROM vouchers WHERE reference_type = 'EXPENSE';")["c"] == 1
    cash_after = db.fetch_one("SELECT current_balance FROM ledger_accounts WHERE account_name = 'Cash in Hand';")["current_balance"]
    assert cash_after == pytest.approx(cash_before - 500.0)


def test_17_daily_expense_next_day_retry_with_omitted_date_replays(acct_env):
    svc, cat_id = acct_env["svc"], acct_env["cat_id"]
    key = f"EXP-MIDNIGHT-{uuid.uuid4()}"

    with patch("src.services.accounting_service.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 11, 17, 23, 59, 50)
        e1, v1, no1, rep1 = svc.record_daily_expense_with_status(
            category_id=cat_id, amount=125.0, expense_date=None,
            payment_mode="CASH", remarks="Midnight tea", idempotency_key=key,
        )
        assert rep1 is False

        mock_dt.now.return_value = datetime(2026, 11, 18, 0, 0, 20)
        e2, v2, no2, rep2 = svc.record_daily_expense_with_status(
            category_id=cat_id, amount=125.0, expense_date=None,
            payment_mode="CASH", remarks="Midnight tea", idempotency_key=key,
        )
        assert rep2 is True
        assert (e2, v2, no2) == (e1, v1, no1)


def test_18_daily_expense_failed_voucher_rolls_back_expense_and_key_and_two_legitimate_succeed(acct_env):
    db, svc, cat_id = acct_env["db"], acct_env["svc"], acct_env["cat_id"]
    key1 = f"EXP-ROLLBACK-{uuid.uuid4()}"

    # Simulate failure during voucher posting -> ENTIRE transaction rolls back (expense + key gone!)
    with patch.object(svc.accounting_repo, "create_voucher", side_effect=RuntimeError("Simulated voucher failure")):
        with pytest.raises(RuntimeError, match="Simulated voucher failure"):
            svc.record_daily_expense_with_status(
                category_id=cat_id, amount=250.0, expense_date="2026-11-17",
                payment_mode="CASH", remarks="Retryable", idempotency_key=key1,
            )

    assert db.fetch_one("SELECT COUNT(*) AS c FROM expenses;")["c"] == 0
    assert db.fetch_one("SELECT * FROM expenses WHERE idempotency_key = ?;", (key1,)) is None

    # Retry with same key now succeeds cleanly
    e1, _v1, _no1, rep1 = svc.record_daily_expense_with_status(
        category_id=cat_id, amount=250.0, expense_date="2026-11-17",
        payment_mode="CASH", remarks="Retryable", idempotency_key=key1,
    )
    assert rep1 is False

    # Second legitimate expense with different key succeeds
    e2, _v2, _no2, rep2 = svc.record_daily_expense_with_status(
        category_id=cat_id, amount=250.0, expense_date="2026-11-17",
        payment_mode="CASH", remarks="Retryable", idempotency_key=f"EXP-LEGIT-2-{uuid.uuid4()}",
    )
    assert rep2 is False and e2 != e1
    assert db.fetch_one("SELECT COUNT(*) AS c FROM expenses;")["c"] == 2
    assert db.fetch_one("SELECT COUNT(*) AS c FROM vouchers WHERE reference_type = 'EXPENSE';")["c"] == 2


def test_19_shared_connection_enforcement_no_extra_connections_opened(acct_env):
    """Prove all idempotency lookups and writes run on the single active BEGIN IMMEDIATE connection (no nested connections)."""
    db, svc, cust_id, supp_id, cat_id = (
        acct_env["db"], acct_env["svc"], acct_env["cust_id"], acct_env["supp_id"], acct_env["cat_id"]
    )

    orig_get_conn = db.get_connection
    conn_opens = [0]

    def counting_get_conn(*args, **kwargs):
        conn_opens[0] += 1
        return orig_get_conn(*args, **kwargs)

    with patch.object(db, "get_connection", side_effect=counting_get_conn):
        with patch.object(db, "fetch_one", side_effect=AssertionError("Nested fetch_one() opened inside transaction!")):
            with patch.object(db, "fetch_all", side_effect=AssertionError("Nested fetch_all() opened inside transaction!")):
                # Each call must open EXACTLY 1 connection (for BEGIN IMMEDIATE) and 0 nested connections
                k_rcp = f"SC-RCP-{uuid.uuid4()}"
                conn_opens[0] = 0
                svc.record_customer_receipt_with_status(cust_id, "2026-11-17", 100.0, "CASH", idempotency_key=k_rcp)
                assert conn_opens[0] == 1
                conn_opens[0] = 0
                _, _, r_rep = svc.record_customer_receipt_with_status(cust_id, "2026-11-17", 100.0, "CASH", idempotency_key=k_rcp)
                assert r_rep is True and conn_opens[0] == 1

                k_pay = f"SC-PAY-{uuid.uuid4()}"
                conn_opens[0] = 0
                svc.record_supplier_payment_with_status(supp_id, amount=100.0, payment_date="2026-11-17", payment_mode="CASH", idempotency_key=k_pay)
                assert conn_opens[0] == 1
                conn_opens[0] = 0
                _, _, p_rep = svc.record_supplier_payment_with_status(supp_id, amount=100.0, payment_date="2026-11-17", payment_mode="CASH", idempotency_key=k_pay)
                assert p_rep is True and conn_opens[0] == 1

                k_exp = f"SC-EXP-{uuid.uuid4()}"
                conn_opens[0] = 0
                svc.record_daily_expense_with_status(cat_id, 100.0, "2026-11-17", "CASH", idempotency_key=k_exp)
                assert conn_opens[0] == 1
                conn_opens[0] = 0
                _, _, _, e_rep = svc.record_daily_expense_with_status(cat_id, 100.0, "2026-11-17", "CASH", idempotency_key=k_exp)
                assert e_rep is True and conn_opens[0] == 1


# ============================================================================
# API CONTRACT TESTS (Test 20)
# ============================================================================

def test_20_api_accounting_idempotency_contracts_all_three_endpoints(acct_env):
    """
    Verify full HTTP contract on /api/accounting/customer-receipt, /api/accounting/supplier-payment,
    and /api/accounting/expenses:
    - Body-only, Header-only, Matching Body+Header -> 200 & replay
    - Mismatched Body+Header -> 400
    - Blank/invalid/oversized key (body or header) -> 400
    - Client-supplied request_fingerprint -> 400
    - Payload conflict -> 409 with error_code="IDEMPOTENCY_KEY_CONFLICT"
    - Missing key -> legacy compatibility (200, idempotent_replay=False)
    - Unknown customer on customer-receipt -> 400
    """
    headers = acct_env["headers"]
    cust_id = acct_env["cust_id"]
    supp_id = acct_env["supp_id"]
    cat_id = acct_env["cat_id"]

    with TestClient(app) as client:
        # -------- 1. CUSTOMER RECEIPT --------
        rcp_base = {
            "customer_id": cust_id,
            "receipt_date": "2026-11-17",
            "amount": 300.0,
            "payment_mode": "CASH",
            "narration": "API receipt",
        }
        k_rcp_hdr = f"API-RCP-HDR-{uuid.uuid4()}"
        r1 = client.post("/api/accounting/customer-receipt", json=rcp_base, headers={**headers, "Idempotency-Key": k_rcp_hdr})
        assert r1.status_code == 200, r1.text
        d1 = r1.json()
        assert d1["success"] is True and d1["idempotent_replay"] is False
        assert d1["voucher_id"] > 0 and d1["voucher_no"].startswith("RCP-")

        # Matching body + header replays
        r2 = client.post(
            "/api/accounting/customer-receipt",
            json=dict(rcp_base, idempotency_key=k_rcp_hdr),
            headers={**headers, "Idempotency-Key": f"  {k_rcp_hdr}  "},
        )
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["idempotent_replay"] is True
        assert d2["voucher_id"] == d1["voucher_id"] and d2["voucher_no"] == d1["voucher_no"]

        # Body-only key
        k_rcp_body = f"API-RCP-BODY-{uuid.uuid4()}"
        r3 = client.post("/api/accounting/customer-receipt", json=dict(rcp_base, idempotency_key=k_rcp_body), headers=headers)
        assert r3.status_code == 200 and r3.json()["idempotent_replay"] is False

        # Mismatched body + header -> 400
        assert client.post(
            "/api/accounting/customer-receipt",
            json=dict(rcp_base, idempotency_key=k_rcp_body),
            headers={**headers, "Idempotency-Key": f"DIFF-{uuid.uuid4()}"},
        ).status_code == 400

        # Blank/invalid/oversized key (body & header) & client fingerprint -> 400
        for bad in ("", "   ", "a" * 129, "bad\nkey", "non-ascii-₹"):
            assert client.post("/api/accounting/customer-receipt", json=dict(rcp_base, idempotency_key=bad), headers=headers).status_code == 400
        for bad_h in ("", "   ", "b" * 129):
            assert client.post("/api/accounting/customer-receipt", json=rcp_base, headers={**headers, "Idempotency-Key": bad_h}).status_code == 400
        assert client.post("/api/accounting/customer-receipt", json=dict(rcp_base, request_fingerprint="forged"), headers=headers).status_code == 400

        # Changed payload -> 409 IDEMPOTENCY_KEY_CONFLICT
        r_conf = client.post(
            "/api/accounting/customer-receipt",
            json=dict(rcp_base, amount=350.0, idempotency_key=k_rcp_hdr),
            headers=headers,
        )
        assert r_conf.status_code == 409
        assert r_conf.json()["error_code"] == "IDEMPOTENCY_KEY_CONFLICT"
        assert isinstance(r_conf.json()["detail"], str)

        # Unknown customer -> 400
        assert client.post(
            "/api/accounting/customer-receipt",
            json=dict(rcp_base, customer_id=999999, idempotency_key=f"UNK-{uuid.uuid4()}"),
            headers=headers,
        ).status_code == 400

        # Legacy missing key -> 200
        r_leg = client.post("/api/accounting/customer-receipt", json=rcp_base, headers=headers)
        assert r_leg.status_code == 200 and r_leg.json()["idempotent_replay"] is False

        # -------- 2. SUPPLIER PAYMENT --------
        pay_base = {
            "supplier_id": supp_id,
            "amount": 500.0,
            "payment_date": "2026-11-17",
            "payment_mode": "BANK_TRANSFER",
            "reference_no": "UTR-77",
            "narration": "API supp pay",
        }
        k_pay_hdr = f"API-PAY-HDR-{uuid.uuid4()}"
        p1 = client.post("/api/accounting/supplier-payment", json=pay_base, headers={**headers, "Idempotency-Key": k_pay_hdr})
        assert p1.status_code == 200, p1.text
        pd1 = p1.json()
        assert pd1["success"] is True and pd1["idempotent_replay"] is False
        assert pd1["voucher_id"] > 0 and pd1["voucher_no"].startswith("PAY-")
        assert pd1["message"] == "Payment recorded successfully"

        p2 = client.post("/api/accounting/supplier-payment", json=dict(pay_base, idempotency_key=k_pay_hdr), headers={**headers, "Idempotency-Key": k_pay_hdr})
        assert p2.status_code == 200 and p2.json()["idempotent_replay"] is True
        assert p2.json()["voucher_id"] == pd1["voucher_id"] and p2.json()["voucher_no"] == pd1["voucher_no"]

        # Mismatch, invalid, fingerprint -> 400
        assert client.post("/api/accounting/supplier-payment", json=dict(pay_base, idempotency_key="K1"), headers={**headers, "Idempotency-Key": "K2"}).status_code == 400
        assert client.post("/api/accounting/supplier-payment", json=dict(pay_base, idempotency_key=""), headers=headers).status_code == 400
        assert client.post("/api/accounting/supplier-payment", json=pay_base, headers={**headers, "Idempotency-Key": "x" * 129}).status_code == 400
        assert client.post("/api/accounting/supplier-payment", json=dict(pay_base, request_fingerprint="forged"), headers=headers).status_code == 400

        # Conflict -> 409 IDEMPOTENCY_KEY_CONFLICT (both payload change and cross-op receipt key reuse)
        p_conf = client.post("/api/accounting/supplier-payment", json=dict(pay_base, amount=550.0, idempotency_key=k_pay_hdr), headers=headers)
        assert p_conf.status_code == 409 and p_conf.json()["error_code"] == "IDEMPOTENCY_KEY_CONFLICT"
        p_cross = client.post("/api/accounting/supplier-payment", json=dict(pay_base, idempotency_key=k_rcp_hdr), headers=headers)
        assert p_cross.status_code == 409 and p_cross.json()["error_code"] == "IDEMPOTENCY_KEY_CONFLICT"

        # Legacy missing key -> 200
        assert client.post("/api/accounting/supplier-payment", json=pay_base, headers=headers).status_code == 200

        # -------- 3. DAILY EXPENSES --------
        exp_base = {
            "category_id": cat_id,
            "amount": 180.0,
            "expense_date": "2026-11-17",
            "payment_mode": "CASH",
            "reference_no": "EXP-REF-1",
            "remarks": "Loading",
        }
        k_exp_hdr = f"API-EXP-HDR-{uuid.uuid4()}"
        e1 = client.post("/api/accounting/expenses", json=exp_base, headers={**headers, "Idempotency-Key": k_exp_hdr})
        assert e1.status_code == 200, e1.text
        ed1 = e1.json()
        assert ed1["success"] is True and ed1["idempotent_replay"] is False
        assert ed1["expense_id"] > 0 and ed1["voucher_id"] > 0 and ed1["voucher_no"].startswith("PAY-")
        assert ed1["message"] == "Expense recorded successfully"

        # Replay via matching body + header (and description alias matching remarks)
        exp_desc_alias = dict(exp_base)
        exp_desc_alias.pop("remarks")
        exp_desc_alias["description"] = "Loading"
        exp_desc_alias["idempotency_key"] = k_exp_hdr
        e2 = client.post("/api/accounting/expenses", json=exp_desc_alias, headers={**headers, "Idempotency-Key": k_exp_hdr})
        assert e2.status_code == 200
        ed2 = e2.json()
        assert ed2["idempotent_replay"] is True
        assert (ed2["expense_id"], ed2["voucher_id"], ed2["voucher_no"]) == (ed1["expense_id"], ed1["voucher_id"], ed1["voucher_no"])

        # Mismatch, invalid, fingerprint -> 400
        assert client.post("/api/accounting/expenses", json=dict(exp_base, idempotency_key="EK1"), headers={**headers, "Idempotency-Key": "EK2"}).status_code == 400
        assert client.post("/api/accounting/expenses", json=dict(exp_base, idempotency_key="   "), headers=headers).status_code == 400
        assert client.post("/api/accounting/expenses", json=exp_base, headers={**headers, "Idempotency-Key": "z" * 129}).status_code == 400
        assert client.post("/api/accounting/expenses", json=dict(exp_base, request_fingerprint="forged"), headers=headers).status_code == 400

        # Conflict -> 409 IDEMPOTENCY_KEY_CONFLICT
        e_conf = client.post("/api/accounting/expenses", json=dict(exp_base, amount=220.0, idempotency_key=k_exp_hdr), headers=headers)
        assert e_conf.status_code == 409 and e_conf.json()["error_code"] == "IDEMPOTENCY_KEY_CONFLICT"

        # Legacy missing key -> 200
        e_leg = client.post("/api/accounting/expenses", json=exp_base, headers=headers)
        assert e_leg.status_code == 200 and e_leg.json()["idempotent_replay"] is False
