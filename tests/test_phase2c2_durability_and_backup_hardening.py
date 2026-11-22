"""
Isolated Verification Suite for Phase 2C-2:
SQLite Durability (WAL + synchronous=FULL + wal_autocheckpoint=1000),
Accurate WAL Checkpoint Reporting, Chunked Online Backup Execution,
Comprehensive Backup Verification (Integrity + FK + Schema + SHA-256 + fsync),
Disk-Space Protection, Strict Restore Confirmation, and Cautious Orphan Cleanup.

Strictly uses temporary SQLite databases and temporary backup directories.
Never touches live data/agri_erp.db or repository backups/.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
from collections import namedtuple
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, reset_db_manager
from src.models.master_data import Product
from src.repositories.master_data_repository import MasterDataRepository
from src.services.auth_service import AuthService
from src.services.backup_service import (
    BackupService,
    InsufficientDiskSpaceError,
    register_active_staging_path,
)

_DiskUsage = namedtuple("usage", ["total", "used", "free"])


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Create a completely isolated SQLite DB + backup directory + admin JWT."""
    db_file = tmp_path / "data" / "phase2c2_erp.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("JWT_SECRET", "phase2c2-test-secret-0123456789abcdef0123456789abcdef")
    reset_db_manager()

    db = DatabaseManager(db_path=db_file, app_mode="local", backup_dir=backup_dir, clean_install=True)
    db.initialize_database(include_seed=True)

    # Remove any migration pre-upgrade backups from clean init if present
    for f in backup_dir.glob("*.db"):
        f.unlink()

    admin_token = AuthService.create_access_token(
        user_id=1,
        username="phase2c2_admin",
        full_name="Phase 2C-2 Admin",
        role="ADMIN",
    )

    yield {
        "db": db,
        "db_file": db_file,
        "backup_dir": backup_dir,
        "admin_token": admin_token,
        "admin_headers": {"Authorization": f"Bearer {admin_token}"},
    }
    reset_db_manager()


# =====================================================================
# 1. SQLite Durability PRAGMAs Across Independent Connections
# =====================================================================
def test_sqlite_durability_pragmas_on_multiple_independent_connections(isolated_env: Dict[str, Any]):
    """Verify WAL, synchronous=FULL (2), foreign_keys=ON, busy_timeout=10000, wal_autocheckpoint=1000."""
    db: DatabaseManager = isolated_env["db"]

    def _assert_pragmas(conn: sqlite3.Connection) -> None:
        cur = conn.cursor()
        assert str(cur.execute("PRAGMA journal_mode;").fetchone()[0]).lower() == "wal"
        assert int(cur.execute("PRAGMA synchronous;").fetchone()[0]) == 2  # FULL
        assert int(cur.execute("PRAGMA foreign_keys;").fetchone()[0]) == 1
        assert int(cur.execute("PRAGMA busy_timeout;").fetchone()[0]) == 10000
        assert int(cur.execute("PRAGMA wal_autocheckpoint;").fetchone()[0]) == 1000
        cur.close()

    # Check 3 independently opened connections via get_connection()
    conns = [db.get_connection() for _ in range(3)]
    try:
        for c in conns:
            _assert_pragmas(c)
    finally:
        for c in conns:
            c.close()

    # Check inside transaction() and transaction(mode="IMMEDIATE")
    with db.transaction() as tx_conn:
        _assert_pragmas(tx_conn)

    with db.transaction(mode="IMMEDIATE") as imm_conn:
        _assert_pragmas(imm_conn)

    # Check a fresh DatabaseManager instance pointing at the same file
    db2 = DatabaseManager(
        db_path=isolated_env["db_file"],
        app_mode="local",
        backup_dir=isolated_env["backup_dir"],
    )
    c2 = db2.get_connection()
    try:
        _assert_pragmas(c2)
    finally:
        c2.close()


# =====================================================================
# 2. Accurate WAL Checkpoint Reporting
# =====================================================================
def test_wal_checkpoint_reports_success_and_detects_busy_incomplete(isolated_env: Dict[str, Any]):
    """
    Verify checkpoint_wal():
    - Returns True with busy==0 and checkpointed>=log when unblocked.
    - Returns False (does NOT falsely report success) when a held reader blocks TRUNCATE.
    """
    db: DatabaseManager = isolated_env["db"]

    # Insert data to generate WAL frames
    master = MasterDataRepository(db)
    master.create_product(Product(product_name="Checkpoint Baseline Urea", default_sale_rate=270.0))

    # Clean unblocked checkpoint must succeed and populate last_checkpoint_result
    assert db.checkpoint_wal("TRUNCATE") is True
    assert db.last_checkpoint_result is not None
    assert db.last_checkpoint_result["completed"] is True
    assert db.last_checkpoint_result["busy"] == 0
    assert db.last_checkpoint_result["log"] >= 0
    assert db.last_checkpoint_result["checkpointed"] >= db.last_checkpoint_result["log"]

    # Hold an open read transaction snapshot so WAL frames committed AFTER it cannot be truncated
    blocker = sqlite3.connect(str(isolated_env["db_file"]), timeout=10.0)
    try:
        blocker.isolation_level = None
        blocker.execute("BEGIN DEFERRED;")
        blocker.execute("SELECT COUNT(*) FROM products;").fetchone()

        # Commit a new write while blocker holds the older WAL snapshot
        master.create_product(Product(product_name="Uncheckpointed WAL Item", default_sale_rate=420.0))

        # TRUNCATE checkpoint cannot complete because blocker still reads the old snapshot
        result = db.checkpoint_wal("TRUNCATE")
        assert result is False
        assert db.last_checkpoint_result is not None
        assert db.last_checkpoint_result["completed"] is False
        assert db.last_checkpoint_result["busy"] == 1
    finally:
        try:
            blocker.execute("ROLLBACK;")
        except Exception:
            pass
        blocker.close()

    # Once blocker is gone, checkpoint_wal("TRUNCATE") succeeds cleanly
    assert db.checkpoint_wal("TRUNCATE") is True
    assert db.last_checkpoint_result is not None
    assert db.last_checkpoint_result["completed"] is True
    assert db.last_checkpoint_result["busy"] == 0
    assert db.last_checkpoint_result["checkpointed"] >= db.last_checkpoint_result["log"]


# =====================================================================
# 3. Chunked WAL-Aware Backup, Sidecar Absence, Integrity, FK & SHA-256
# =====================================================================
def test_chunked_wal_aware_backup_verification_and_sha256(
    isolated_env: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Verify:
    - Chunked online backup (pages=100, sleep=0.01) captures rows still sitting in active -wal.
    - Live DB stays in WAL mode and remains readable/writable.
    - Finalized backup is in DELETE journal mode with zero WAL/SHM/journal sidecars.
    - Integrity check, FK check, required tables, schema_version, and SHA-256 all pass and match.
    """
    db: DatabaseManager = isolated_env["db"]
    db_file: Path = isolated_env["db_file"]
    backup_dir: Path = isolated_env["backup_dir"]
    svc = BackupService(db, backup_dir=backup_dir)

    # Spy on _sqlite_online_backup arguments without skipping real execution
    recorded_calls = []
    orig_online_backup = BackupService._sqlite_online_backup

    def _spy_online_backup(src_conn, dst_path, *, pages=None, sleep=None):
        recorded_calls.append(
            {
                "pages": BackupService.ONLINE_BACKUP_PAGES_PER_STEP if pages is None else pages,
                "sleep": BackupService.ONLINE_BACKUP_SLEEP_SECONDS if sleep is None else sleep,
            }
        )
        return orig_online_backup(src_conn, dst_path, pages=pages, sleep=sleep)

    monkeypatch.setattr(BackupService, "_sqlite_online_backup", staticmethod(_spy_online_backup))

    # Hold a read snapshot so our newly committed row stays inside the -wal file (uncheckpointed)
    reader = sqlite3.connect(str(db_file), timeout=10.0)
    try:
        reader.isolation_level = None
        reader.execute("BEGIN DEFERRED;")
        reader.execute("SELECT COUNT(*) FROM products;").fetchone()

        master = MasterDataRepository(db)
        wal_prod_id = master.create_product(
            Product(product_name="Active WAL Only DAP 50kg", default_sale_rate=1350.0)
        )

        wal_file = Path(f"{db_file}-wal")
        assert wal_file.exists() and wal_file.stat().st_size > 0, "Expected committed row to reside in active -wal"

        # Create backup while WAL is actively uncheckpointed
        bkp_path = svc.create_backup(backup_type="MANUAL")

        # Verify live DB stayed in WAL mode and still serves reads while backup ran
        live_conn = db.get_connection()
        try:
            assert str(live_conn.execute("PRAGMA journal_mode;").fetchone()[0]).lower() == "wal"
            assert live_conn.execute(
                "SELECT product_name FROM products WHERE product_id = ?;", (wal_prod_id,)
            ).fetchone()[0] == "Active WAL Only DAP 50kg"
        finally:
            live_conn.close()
    finally:
        try:
            reader.execute("ROLLBACK;")
        except Exception:
            pass
        reader.close()

    # Confirm chunked backup parameters were used
    assert len(recorded_calls) == 1
    assert recorded_calls[0]["pages"] == 100
    assert recorded_calls[0]["sleep"] == pytest.approx(0.01)

    # Confirm finalized backup file has ZERO SQLite sidecars and is in DELETE journal mode
    assert bkp_path.is_file()
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{bkp_path}{suffix}").exists(), f"Unexpected sidecar {suffix} next to backup"

    bkp_conn = sqlite3.connect(f"{bkp_path.resolve().as_uri()}?immutable=1", uri=True)
    try:
        assert str(bkp_conn.execute("PRAGMA journal_mode;").fetchone()[0]).lower() == "delete"
        row = bkp_conn.execute(
            "SELECT product_name, default_sale_rate FROM products WHERE product_id = ?;",
            (wal_prod_id,),
        ).fetchone()
        assert row is not None, "WAL-resident committed row MUST be present inside backup!"
        assert row[0] == "Active WAL Only DAP 50kg"
        assert float(row[1]) == pytest.approx(1350.0)
    finally:
        bkp_conn.close()

    # Verify inspection & SHA-256 match
    insp = svc.inspect_backup(bkp_path)
    assert insp["integrity_ok"] is True
    assert insp["foreign_key_ok"] is True
    assert insp["has_required_tables"] is True
    assert insp["schema_version"] >= 1
    assert svc.last_backup_sha256 is not None
    assert svc.last_backup_sha256 == _sha256_file(bkp_path)
    assert svc.last_backup_metadata is not None
    assert svc.last_backup_metadata["sha256"] == svc.last_backup_sha256

    # Confirm SUCCESS recorded in backup_log
    log_row = db.fetch_one(
        "SELECT status, backup_size_bytes FROM backup_log WHERE file_path = ? ORDER BY backup_id DESC LIMIT 1;",
        (str(bkp_path),),
    )
    assert log_row is not None
    assert log_row["status"] == "SUCCESS"
    assert int(log_row["backup_size_bytes"]) == bkp_path.stat().st_size


# =====================================================================
# 4. Foreign-Key Failure Rejection & Older Backup Preservation
# =====================================================================
def test_backup_rejects_foreign_key_violations_and_preserves_older_backups(isolated_env: Dict[str, Any]):
    """
    If the live database contains a foreign-key violation, create_backup() must:
    - Refuse to report SUCCESS and raise ValueError.
    - Clean up ONLY its own staging temp file.
    - Preserve all previously created valid backups untouched.
    - Record FAILED_FOREIGN_KEYS in backup_log.
    """
    db: DatabaseManager = isolated_env["db"]
    db_file: Path = isolated_env["db_file"]
    backup_dir: Path = isolated_env["backup_dir"]
    svc = BackupService(db, backup_dir=backup_dir)

    # First create a healthy older backup that must remain untouched
    older_backup = svc.create_backup(backup_type="MANUAL")
    older_hash = _sha256_file(older_backup)

    # Deliberately introduce an orphan FK row via raw sqlite3 with foreign_keys=OFF
    raw_conn = sqlite3.connect(str(db_file))
    try:
        raw_conn.execute("PRAGMA foreign_keys = OFF;")
        raw_conn.execute(
            "INSERT INTO products (product_name, category_id, default_sale_rate) "
            "VALUES ('Broken FK Product', 999999, 100.0);"
        )
        raw_conn.commit()
    finally:
        raw_conn.close()

    with pytest.raises(ValueError, match="foreign_key_check"):
        svc.create_backup(backup_type="MANUAL")

    # Older valid backup is completely intact
    assert older_backup.is_file()
    assert _sha256_file(older_backup) == older_hash

    # No new finalized .db file and zero leftover .tmp staging files in backup_dir
    db_files = sorted(p.name for p in backup_dir.iterdir() if p.name.endswith(".db"))
    assert db_files == [older_backup.name]
    tmp_files = [p.name for p in backup_dir.iterdir() if ".tmp" in p.name]
    assert tmp_files == []

    # Clear failure status recorded in backup_log
    last_log = db.fetch_one("SELECT status FROM backup_log ORDER BY backup_id DESC LIMIT 1;")
    assert last_log is not None
    assert last_log["status"] == "FAILED_FOREIGN_KEYS"


# =====================================================================
# 5. Disk-Space Protection (Service & HTTP 507 API)
# =====================================================================
def test_low_disk_space_aborts_safely_preserves_backups_and_pos_writes(
    isolated_env: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Verify (db + wal) * 2.5 + 100 MB disk-space check:
    - Aborts cleanly before creating staging files.
    - Preserves older valid backups (never auto-deletes).
    - Records FAILED_LOW_DISK in backup_log.
    - Returns HTTP 507 with a clear warning via POST /api/system/backup.
    - Live DB remains usable for normal POS / master writes.
    """
    db: DatabaseManager = isolated_env["db"]
    backup_dir: Path = isolated_env["backup_dir"]
    svc = BackupService(db, backup_dir=backup_dir)

    # Create a baseline backup before simulating low disk space
    existing_bkp = svc.create_backup(backup_type="MANUAL")
    existing_hash = _sha256_file(existing_bkp)

    required_bytes, db_size, wal_size = svc.estimate_required_backup_space()
    assert required_bytes >= int((db_size + wal_size) * 2.5) + 100 * 1024 * 1024

    # Simulate only 10 MB free (well below the 100 MB safety margin)
    low_free = 10 * 1024 * 1024
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: _DiskUsage(total=100 * 1024 * 1024 * 1024, used=99 * 1024 * 1024 * 1024, free=low_free),
    )

    with pytest.raises(InsufficientDiskSpaceError) as exc_info:
        svc.create_backup(backup_type="MANUAL")
    assert exc_info.value.available_bytes == low_free
    assert exc_info.value.required_bytes == required_bytes

    # Older backup untouched & zero staging files created
    assert existing_bkp.is_file()
    assert _sha256_file(existing_bkp) == existing_hash
    assert [p for p in backup_dir.iterdir() if ".tmp" in p.name] == []

    # Logged cleanly as FAILED_LOW_DISK
    last_log = db.fetch_one("SELECT status FROM backup_log ORDER BY backup_id DESC LIMIT 1;")
    assert last_log is not None
    assert last_log["status"] == "FAILED_LOW_DISK"

    # API returns HTTP 507 with understandable warning text
    with TestClient(app) as client:
        res = client.post("/api/system/backup", headers=isolated_env["admin_headers"])
        assert res.status_code == 507
        detail = res.json()["detail"]
        assert "Insufficient disk space" in detail
        assert "Existing backups were not modified" in detail

    # Normal DB writes are completely unaffected
    master = MasterDataRepository(db)
    pid = master.create_product(Product(product_name="POS Continues During Low Disk", default_sale_rate=99.0))
    assert db.fetch_one("SELECT product_name FROM products WHERE product_id = ?;", (pid,)) is not None


# =====================================================================
# 6. Cautious Orphan Staging-File Cleanup
# =====================================================================
def test_orphan_staging_cleanup_is_strict_and_conservative(
    isolated_env: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Verify cleanup_orphaned_staging_files():
    - Removes ONLY known stale staging patterns whose PID is dead and age >= threshold.
    - NEVER removes finalized .db backups, .corrupt_raw.bak forensic copies, unrelated files,
      fresh staging files, live-PID staging files, or active in-flight staging files.
    - Skips cleanly during maintenance and on Windows-style PermissionError.
    """
    db: DatabaseManager = isolated_env["db"]
    db_file: Path = isolated_env["db_file"]
    data_dir = db_file.parent
    backup_dir: Path = isolated_env["backup_dir"]
    svc = BackupService(db, backup_dir=backup_dir)

    now = time.time()
    old_mtime = now - 3600.0  # 1 hour old (> 15 min threshold)
    fresh_mtime = now - 60.0  # 1 minute old (< 15 min threshold)
    dead_pid = 99999999
    live_pid = os.getpid()

    # Deterministic process-liveness mock for cross-platform reliability
    monkeypatch.setattr(
        "src.services.backup_service._is_process_alive",
        lambda pid: pid == live_pid,
    )

    def _touch(path: Path, mtime: float, content: bytes = b"partial") -> Path:
        path.write_bytes(content)
        os.utime(path, (mtime, mtime))
        return path

    # Files that SHOULD be cleaned up (stale, dead PID or old upload temp)
    stale_bkp_tmp = _touch(backup_dir / f".agri_erp_backup_20260101_120000.db.{dead_pid}.tmp", old_mtime)
    stale_bkp_tmp_wal = _touch(backup_dir / f".agri_erp_backup_20260101_120000.db.{dead_pid}.tmp-wal", old_mtime)
    stale_upgrade_tmp = _touch(backup_dir / f".agri_erp_before_upgrade_20260101_120000.db.{dead_pid}.tmp", old_mtime)
    stale_upload_tmp = _touch(backup_dir / ".upload_inspect_abc12345.db.tmp", old_mtime)
    stale_restore_tmp = _touch(data_dir / f".phase2c2_erp.db.restore.{dead_pid}.tmp", old_mtime)
    stale_rollback_tmp = _touch(data_dir / f".phase2c2_erp.db.rollback.{dead_pid}.tmp", old_mtime)

    # Files that MUST NEVER be deleted
    finalized_db = svc.create_backup(backup_type="MANUAL")
    os.utime(finalized_db, (old_mtime, old_mtime))
    corrupt_raw = _touch(backup_dir / "agri_erp_before_restore_20260101_120000.corrupt_raw.bak", old_mtime)
    corrupt_raw_wal = _touch(backup_dir / "agri_erp_before_restore_20260101_120000.corrupt_raw.bak-wal", old_mtime)
    unrelated_tmp = _touch(backup_dir / ".custom_user_note.tmp", old_mtime)
    non_hidden_tmp = _touch(backup_dir / "ignore_temp.db.tmp", old_mtime)
    fresh_dead_tmp = _touch(backup_dir / f".agri_erp_backup_20260101_130000.db.{dead_pid}.tmp", fresh_mtime)
    fresh_upload_tmp = _touch(backup_dir / ".upload_inspect_recent99.db.tmp", fresh_mtime)
    live_pid_tmp = _touch(backup_dir / f".agri_erp_backup_20260101_140000.db.{live_pid}.tmp", old_mtime)
    active_tmp = _touch(backup_dir / f".agri_erp_backup_20260101_150000.db.{dead_pid}.tmp", old_mtime)

    # 1. During exclusive maintenance, cleanup must be a complete no-op
    with db.exclusive_maintenance(timeout=5.0, reason="test_skip"):
        maint_report = svc.cleanup_orphaned_staging_files(now_epoch=now)
        assert maint_report == {"removed": [], "skipped": [], "errors": []}
        assert stale_bkp_tmp.exists()

    # 2. Normal cleanup while active_tmp is registered as in-flight
    with register_active_staging_path(active_tmp):
        report = svc.cleanup_orphaned_staging_files(now_epoch=now)

    removed_set = set(report["removed"])
    for expected_removed in (
        stale_bkp_tmp,
        stale_bkp_tmp_wal,
        stale_upgrade_tmp,
        stale_upload_tmp,
        stale_restore_tmp,
        stale_rollback_tmp,
    ):
        assert str(expected_removed) in removed_set
        assert not expected_removed.exists()

    for expected_preserved in (
        finalized_db,
        corrupt_raw,
        corrupt_raw_wal,
        unrelated_tmp,
        non_hidden_tmp,
        fresh_dead_tmp,
        fresh_upload_tmp,
        live_pid_tmp,
        active_tmp,
    ):
        assert expected_preserved.exists(), f"Must NOT delete preserved file: {expected_preserved.name}"
    assert report["errors"] == []

    # 3. Windows-style PermissionError on unlink must skip cleanly without raising
    locked_tmp = _touch(backup_dir / f".agri_erp_backup_20260101_160000.db.{dead_pid}.tmp", old_mtime)
    orig_unlink = Path.unlink

    def _deny_locked(self_path: Path, *args, **kwargs):
        if self_path.name == locked_tmp.name:
            raise PermissionError("Simulated Windows open file handle")
        return orig_unlink(self_path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _deny_locked)
    lock_report = svc.cleanup_orphaned_staging_files(now_epoch=now)
    assert locked_tmp.exists()
    assert str(locked_tmp) in lock_report["skipped"]
    assert lock_report["errors"] == []


# =====================================================================
# 7. Restore Confirmation Enforcement & Failed-Restore Rollback Data Check
# =====================================================================
def test_restore_requires_explicit_true_confirmation_and_verifies_restored_data(
    isolated_env: Dict[str, Any],
):
    """
    Verify POST /api/system/backup/restore:
    - Rejects missing confirm_restore, confirm_restore=False, and confirm_restore=None (HTTP 400).
    - Accepts confirm_restore=True, restores actual DB rows, and saves BEFORE_RESTORE snapshot.
    """
    db: DatabaseManager = isolated_env["db"]
    headers = isolated_env["admin_headers"]
    master = MasterDataRepository(db)
    baseline_pid = master.create_product(Product(product_name="Baseline Confirmed Product", default_sale_rate=550.0))

    with TestClient(app) as client:
        bkp_res = client.post("/api/system/backup", headers=headers)
        assert bkp_res.status_code == 200
        bkp_data = bkp_res.json()
        bkp_name = bkp_data["filename"]
        assert bkp_data["sha256"] == _sha256_file(isolated_env["backup_dir"] / bkp_name)
        assert bkp_data["schema_version"] >= 1

        # Add a post-backup product directly to live DB
        post_pid = master.create_product(Product(product_name="Post-Backup Should Revert", default_sale_rate=999.0))
        assert db.fetch_one("SELECT 1 FROM products WHERE product_id = ?;", (post_pid,)) is not None

        # Missing, False, and None confirmations must ALL be rejected with HTTP 400
        for bad_payload in (
            {"backup_filename": bkp_name},
            {"backup_filename": bkp_name, "confirm_restore": False},
            {"backup_filename": bkp_name, "confirm_restore": None},
        ):
            bad_res = client.post("/api/system/backup/restore", headers=headers, json=bad_payload)
            assert bad_res.status_code == 400, f"Expected 400 for payload {bad_payload}, got {bad_res.status_code}"
            # Verify live DB was untouched
            assert db.fetch_one("SELECT 1 FROM products WHERE product_id = ?;", (post_pid,)) is not None

        # Explicit confirm_restore=True succeeds and restores exact data
        ok_res = client.post(
            "/api/system/backup/restore",
            headers=headers,
            json={"backup_filename": bkp_name, "confirm_restore": True},
        )
        assert ok_res.status_code == 200, ok_res.text
        ok_data = ok_res.json()
        assert ok_data["success"] is True
        assert ok_data["pre_restore_filename"]
        pre_restore_path = isolated_env["backup_dir"] / ok_data["pre_restore_filename"]
        assert pre_restore_path.is_file()

    # Verify ACTUAL database rows in live DB (not just HTTP 200)
    assert db.fetch_one("SELECT product_name FROM products WHERE product_id = ?;", (baseline_pid,))[0] == (
        "Baseline Confirmed Product"
    )
    assert db.fetch_one("SELECT 1 FROM products WHERE product_id = ?;", (post_pid,)) is None
    assert db.check_integrity() == ["ok"]
    assert db.check_foreign_keys() == []

    # Verify BEFORE_RESTORE snapshot retained the post-backup product
    pre_db = DatabaseManager(db_path=pre_restore_path, app_mode="local")
    assert pre_db.fetch_one("SELECT product_name FROM products WHERE product_id = ?;", (post_pid,))[0] == (
        "Post-Backup Should Revert"
    )


def test_failed_restore_rolls_back_and_preserves_original_data(
    isolated_env: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """
    If live-DB verification fails immediately after swapping files during restore_backup(),
    automatic rollback from BEFORE_RESTORE must restore the exact original live data.
    """
    db: DatabaseManager = isolated_env["db"]
    svc = BackupService(db, backup_dir=isolated_env["backup_dir"])
    master = MasterDataRepository(db)

    master.create_product(Product(product_name="Older Snapshot Seed", default_sale_rate=110.0))
    older_backup = svc.create_backup(backup_type="MANUAL")

    live_only_pid = master.create_product(Product(product_name="Critical Live Data Must Survive", default_sale_rate=888.0))

    # Fail specifically on the post-swap live_db integrity verification step
    orig_verify = svc.verify_database_integrity

    def _fail_post_swap(db_file_path=None, check_foreign_keys=False):
        if db_file_path is not None and Path(db_file_path).resolve() == isolated_env["db_file"].resolve():
            return False
        return orig_verify(db_file_path=db_file_path, check_foreign_keys=check_foreign_keys)

    monkeypatch.setattr(svc, "verify_database_integrity", _fail_post_swap)

    with pytest.raises(RuntimeError, match="Post-restore integrity verification failed"):
        svc.restore_backup(older_backup)

    # Rollback must have restored the original live database completely intact
    row = db.fetch_one("SELECT product_name, default_sale_rate FROM products WHERE product_id = ?;", (live_only_pid,))
    assert row is not None, "Rollback failed to preserve original live DB record!"
    assert row["product_name"] == "Critical Live Data Must Survive"
    assert float(row["default_sale_rate"]) == pytest.approx(888.0)
    assert db.check_integrity() == ["ok"]
    assert db.check_foreign_keys() == []
    assert svc.last_pre_restore_backup is not None and svc.last_pre_restore_backup.is_file()
