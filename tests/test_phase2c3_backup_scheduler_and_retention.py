"""
Isolated Verification Suite for Phase 2C-3:
Single-Instance Automatic Local Backup Scheduler, Lightweight Change Detection,
Conservative GFS Retention, Atomic Metadata Manifest Caching, and Backup Manager UI Status.

Strictly uses temporary SQLite databases, temporary backup directories,
and injectable deterministic clocks (zero real-time sleeps, zero live DB/backup touches).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, reset_db_manager
from src.models.accounting import Expense
from src.models.master_data import Customer, Product, Supplier
from src.models.purchase import Purchase, PurchaseItem
from src.models.sales import Sale, SaleItem
from src.repositories.master_data_repository import MasterDataRepository
from src.services.accounting_service import AccountingService
from src.services.auth_service import AuthService
from src.services.backup_scheduler import (
    BackupScheduler,
    start_backup_scheduler,
    stop_backup_scheduler,
)
from src.services.backup_service import BackupService, pinned_backup_file
from src.services.purchase_service import PurchaseService
from src.services.sales_service import SalesService

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML_PATH = PROJECT_ROOT / "src" / "web" / "templates" / "index.html"
APP_JS_PATH = PROJECT_ROOT / "src" / "web" / "static" / "js" / "app.js"

_DiskUsage = namedtuple("usage", ["total", "used", "free"])


class FakeClock:
    """Deterministic injectable wall clock for scheduler and retention tests."""

    def __init__(self, start_dt: datetime):
        self._now = float(start_dt.timestamp())

    def __call__(self) -> float:
        return self._now

    def advance(self, *, seconds: float = 0, minutes: float = 0, hours: float = 0, days: float = 0) -> float:
        self._now += float(seconds) + float(minutes) * 60.0 + float(hours) * 3600.0 + float(days) * 86400.0
        return self._now

    def set_dt(self, dt: datetime) -> float:
        self._now = float(dt.timestamp())
        return self._now

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self._now)


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Provide an isolated SQLite database and isolated backup directory."""
    stop_backup_scheduler(timeout=2.0)
    reset_db_manager()

    db_file = tmp_path / "data" / "phase2c3_erp.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("JWT_SECRET", "phase2c3-test-secret-0123456789abcdef0123456789abcdef")

    db = DatabaseManager(db_path=db_file, app_mode="local", backup_dir=backup_dir, clean_install=True)
    db.initialize_database(include_seed=True)

    # Clear any clean-init migration backups so tests start from a known empty backup folder
    for p in list(backup_dir.iterdir()):
        if p.is_file():
            p.unlink()

    admin_token = AuthService.create_access_token(
        user_id=1,
        username="sched_admin",
        full_name="Scheduler Admin",
        role="ADMIN",
    )

    yield {
        "db": db,
        "db_file": db_file,
        "backup_dir": backup_dir,
        "admin_token": admin_token,
        "admin_headers": {"Authorization": f"Bearer {admin_token}"},
    }
    stop_backup_scheduler(timeout=2.0)
    reset_db_manager()


def _seed_verified_backup_fixture(
    svc: BackupService,
    backup_dir: Path,
    base_snapshot_bytes: bytes,
    base_meta: Dict[str, Any],
    *,
    filename: str,
    backup_type: str,
    dt: datetime,
    managed_by_policy: bool = True,
) -> Path:
    """Fast deterministic retention fixture: copies a real verified SQLite snapshot and sets mtime + manifest."""
    target = backup_dir / filename
    target.write_bytes(base_snapshot_bytes)
    ts = float(dt.timestamp())
    os.utime(target, (ts, ts))
    st = target.stat()

    manifest = svc.load_manifest(backup_dir)
    entries = dict(manifest.get("entries") or {})
    entries[filename] = {
        **base_meta,
        "filename": filename,
        "backup_type": backup_type,
        "managed_by_policy": bool(managed_by_policy),
        "size_bytes": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
        "mtime_epoch": float(st.st_mtime),
        "modified_at": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at_iso": dt.isoformat(timespec="seconds"),
    }
    manifest["entries"] = entries
    svc._save_manifest_atomic(backup_dir, manifest)
    return target


# =====================================================================
# 1. Single-Instance Startup & Duplicate-Start Protection (Tests 1 & 10)
# =====================================================================
def test_01_and_10_scheduler_starts_once_and_stops_cleanly(isolated_env: Dict[str, Any]):
    """Tests 1 & 10: Repeated initialization calls start exactly one worker; shutdown stops it cleanly."""
    db: DatabaseManager = isolated_env["db"]
    backup_dir: Path = isolated_env["backup_dir"]

    sched1 = start_backup_scheduler(
        db_manager=db,
        backup_dir=backup_dir,
        check_interval_seconds=60.0,
        startup_delay_seconds=30.0,
    )
    assert sched1 is not None
    assert sched1.is_running is True
    assert sched1.start_count == 1
    worker_ident = sched1._worker_thread.ident

    # Simulate duplicate startup calls (e.g., sidecar + FastAPI lifespan + repeated init)
    sched2 = start_backup_scheduler(db_manager=db, backup_dir=backup_dir, startup_delay_seconds=30.0)
    assert sched2 is sched1
    assert sched1.start() is False
    assert sched1.start_count == 1
    assert sched1._worker_thread is not None and sched1._worker_thread.ident == worker_ident

    # Clean shutdown with bounded timeout
    assert stop_backup_scheduler(timeout=3.0) is True
    assert sched1.is_running is False
    assert sched1._worker_thread is None


# =====================================================================
# 2–6. Startup, Idle, Changed-DB, 15-min Check & 4-Hour Interval Rules
# =====================================================================
def test_02_03_04_05_06_schedule_policy_and_change_detection(isolated_env: Dict[str, Any]):
    """
    Tests 2, 3, 4, 5, 6:
    - Startup creates AUTO_DAILY backup when needed and captures live DB rows.
    - Repeat startup same day creates NO duplicate.
    - Idle DB after >= 4h creates NO unnecessary scheduled backup.
    - 15-min checks before 4h do NOT create a backup even after DB changes.
    - After >= 4h with DB changes, creates AUTO_SCHEDULED backup with new rows.
    - New day startup with unchanged DB creates NO backup; after a change, creates next AUTO_DAILY/AUTO_WEEKLY.
    """
    db: DatabaseManager = isolated_env["db"]
    backup_dir: Path = isolated_env["backup_dir"]
    master = MasterDataRepository(db)
    clock = FakeClock(datetime(2026, 11, 16, 9, 0, 0))  # Monday 09:00

    p1 = master.create_product(Product(product_name="Startup Urea Bag", default_sale_rate=266.0))
    sched = BackupScheduler(
        db_manager=db,
        backup_dir=backup_dir,
        check_interval_seconds=900,
        min_backup_interval_seconds=4 * 3600,
        startup_delay_seconds=0.0,
        clock=clock,
    )

    # Test 2: Startup backup created when none exists for today
    r1 = sched.run_check(is_startup=True)
    assert r1["action"] == "created"
    assert r1["backup_type"] == "AUTO_DAILY"
    bkp1 = Path(r1["file_path"])
    assert bkp1.is_file()
    assert bkp1.name.startswith("agri_erp_auto_daily_")
    # Verify actual DB content & integrity inside backup
    conn = sqlite3.connect(f"{bkp1.resolve().as_uri()}?immutable=1", uri=True)
    try:
        assert conn.execute("SELECT product_name FROM products WHERE product_id = ?;", (p1,)).fetchone()[0] == "Startup Urea Bag"
    finally:
        conn.close()
    assert sched.backup_service.inspect_backup(bkp1)["valid_for_restore"] is True

    # Test 3: Second startup check same day -> skipped (today's verified backup already exists)
    clock.advance(minutes=30)
    r2 = sched.run_check(is_startup=True)
    assert r2["action"] == "skipped"
    assert r2["reason"] == "today_backup_exists"

    # Test 4: Idle DB after 5 hours -> skipped (database unchanged, even after backup_log write!)
    clock.advance(hours=5)
    r3 = sched.run_check(is_startup=False)
    assert r3["action"] == "skipped"
    assert r3["reason"] == "database_unchanged"

    # Even a fresh scheduler instance (simulating restart on next day with idle DB) must NOT back up idle DB
    clock.set_dt(datetime(2026, 11, 17, 9, 0, 0))  # Tuesday 09:00 (no writes since Monday backup)
    sched_restarted = BackupScheduler(
        db_manager=db,
        backup_dir=backup_dir,
        check_interval_seconds=900,
        min_backup_interval_seconds=4 * 3600,
        startup_delay_seconds=0.0,
        clock=clock,
    )
    r_idle_next_day = sched_restarted.run_check(is_startup=True)
    assert r_idle_next_day["action"] == "skipped"
    assert r_idle_next_day["reason"] == "database_unchanged"

    # Write a real change on Tuesday 09:05 -> startup check now creates Tuesday AUTO_DAILY
    p2 = master.create_product(Product(product_name="Tuesday Seed Pack", default_sale_rate=850.0))
    clock.advance(minutes=5)
    r_tue = sched_restarted.run_check(is_startup=True)
    assert r_tue["action"] == "created"
    assert r_tue["backup_type"] == "AUTO_DAILY"

    # Test 6: Write another change 15 minutes later -> periodic checks at +15m and +3h45m MUST skip (< 4h)
    p3 = master.create_product(Product(product_name="Afternoon Pesticide", default_sale_rate=640.0))
    clock.advance(minutes=15)
    r_15m = sched_restarted.run_check(is_startup=False)
    assert r_15m["action"] == "skipped"
    assert r_15m["reason"] == "interval_not_elapsed"

    clock.advance(hours=3, minutes=30)  # total 3h45m since Tuesday 09:05
    r_345 = sched_restarted.run_check(is_startup=False)
    assert r_345["action"] == "skipped"
    assert r_345["reason"] == "interval_not_elapsed"

    # Test 5: Advance past 4 hours (+20m -> total 4h05m) -> creates AUTO_SCHEDULED backup with p3
    clock.advance(minutes=20)
    r_4h = sched_restarted.run_check(is_startup=False)
    assert r_4h["action"] == "created"
    assert r_4h["backup_type"] == "AUTO_SCHEDULED"
    bkp_sched = Path(r_4h["file_path"])
    assert bkp_sched.name.startswith("agri_erp_auto_scheduled_")
    conn2 = sqlite3.connect(f"{bkp_sched.resolve().as_uri()}?immutable=1", uri=True)
    try:
        assert conn2.execute("SELECT product_name FROM products WHERE product_id = ?;", (p3,)).fetchone()[0] == "Afternoon Pesticide"
    finally:
        conn2.close()

    # Next ISO week with DB change -> creates AUTO_WEEKLY
    clock.advance(days=7)
    master.create_product(Product(product_name="Next Week Potash", default_sale_rate=1700.0))
    r_wk = sched_restarted.run_check(is_startup=True)
    assert r_wk["action"] == "created"
    assert r_wk["backup_type"] == "AUTO_WEEKLY"
    assert Path(r_wk["file_path"]).name.startswith("agri_erp_auto_weekly_")


# =====================================================================
# 7, 8, 9. Concurrency Coalescing, No Overlap With Manual, Maintenance Skip
# =====================================================================
def test_07_08_09_concurrency_coalescing_manual_non_overlap_and_maintenance_skip(
    isolated_env: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """Tests 7, 8, 9: Concurrent scheduler calls coalesce; manual & auto never overlap; maintenance skips."""
    db: DatabaseManager = isolated_env["db"]
    backup_dir: Path = isolated_env["backup_dir"]
    clock = FakeClock(datetime(2026, 11, 18, 10, 0, 0))
    sched = BackupScheduler(db_manager=db, backup_dir=backup_dir, clock=clock)

    # Test 9: Exclusive maintenance active -> scheduler skips cleanly
    with db.exclusive_maintenance(timeout=5.0, reason="test_migration"):
        res_maint = sched.run_check(is_startup=True)
        assert res_maint["action"] == "skipped"
        assert res_maint["reason"] == "maintenance_active"
    assert list(backup_dir.glob("*.db")) == []

    # Test 7: Two concurrent scheduler calls -> at most ONE backup created
    entered = threading.Event()
    release = threading.Event()
    active_count = {"cur": 0, "max": 0}
    counter_lock = threading.Lock()
    orig_online = BackupService._sqlite_online_backup

    def _tracked_online_backup(src_conn, dst_path, *, pages=None, sleep=None):
        with counter_lock:
            active_count["cur"] += 1
            active_count["max"] = max(active_count["max"], active_count["cur"])
        entered.set()
        release.wait(timeout=3.0)
        try:
            return orig_online(src_conn, dst_path, pages=pages, sleep=sleep)
        finally:
            with counter_lock:
                active_count["cur"] -= 1

    monkeypatch.setattr(BackupService, "_sqlite_online_backup", staticmethod(_tracked_online_backup))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(sched.run_check, is_startup=True)
        assert entered.wait(timeout=3.0)
        f2 = pool.submit(sched.run_check, is_startup=True)
        r_second = f2.result(timeout=3.0)
        release.set()
        r_first = f1.result(timeout=5.0)

    assert r_first["action"] == "created"
    assert r_second["action"] == "skipped"
    assert r_second["reason"] in ("scheduler_busy", "backup_already_in_progress")
    assert active_count["max"] == 1
    assert len(list(backup_dir.glob("*.db"))) == 1

    # Test 8: Manual backup running while automatic request arrives -> ZERO overlap, auto skips cleanly
    entered.clear()
    release.clear()
    active_count["cur"] = 0
    active_count["max"] = 0
    clock.advance(hours=5)
    MasterDataRepository(db).create_product(Product(product_name="Manual Concurrent Item", default_sale_rate=100.0))

    manual_svc = BackupService(db, backup_dir=backup_dir)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_manual = pool.submit(manual_svc.create_backup, backup_type="MANUAL")
        assert entered.wait(timeout=3.0)
        f_auto = pool.submit(sched.run_check, is_startup=False)
        r_auto = f_auto.result(timeout=3.0)
        release.set()
        manual_path = f_manual.result(timeout=5.0)

    assert manual_path.is_file()
    assert r_auto["action"] == "skipped"
    assert r_auto["reason"] == "backup_already_in_progress"
    assert active_count["max"] == 1


# =====================================================================
# 11–17. Conservative GFS Retention Rules, Legacy & Pinned Protection
# =====================================================================
def test_11_12_13_14_15_16_17_gfs_retention_safety_and_pinning(isolated_env: Dict[str, Any]):
    """
    Tests 11–17:
    - Intraday AUTO_SCHEDULED keeps latest 6 within 24h.
    - Daily AUTO_DAILY keeps latest 14 distinct days.
    - Weekly AUTO_WEEKLY keeps latest 8 distinct ISO weeks.
    - Protected MANUAL / BEFORE_UPGRADE / BEFORE_RESTORE keep latest 10 each.
    - Unknown/EXTERNAL, unmanaged legacy backups (17 simulated legacy files), and .corrupt_raw.bak preserved.
    - At least 2 verified backups always remain (floor).
    - Pinned (active download/inspect/restore) backup is NEVER deleted even if expired.
    """
    db: DatabaseManager = isolated_env["db"]
    backup_dir: Path = isolated_env["backup_dir"]
    svc = BackupService(db, backup_dir=backup_dir)

    # Create one real verified snapshot to clone deterministically for fixtures
    seed_bkp = svc.create_backup(backup_type="MANUAL")
    seed_bytes = seed_bkp.read_bytes()
    seed_meta = dict(svc.last_backup_metadata or {})
    seed_bkp.unlink()
    manifest_file = backup_dir / BackupService.MANIFEST_FILENAME
    if manifest_file.exists():
        manifest_file.unlink()

    now_dt = datetime(2026, 11, 18, 18, 0, 0)
    now_epoch = now_dt.timestamp()

    # 11. Intraday AUTO_SCHEDULED: 8 within last 24h + 2 older than 24h -> keep latest 6, prune oldest 4
    intraday_files: List[Path] = []
    for idx in range(8):
        dt = now_dt - timedelta(hours=idx * 2)  # 0h..14h ago (all within 24h)
        intraday_files.append(
            _seed_verified_backup_fixture(
                svc, backup_dir, seed_bytes, seed_meta,
                filename=f"agri_erp_auto_scheduled_{dt.strftime('%Y%m%d_%H%M%S')}.db",
                backup_type="AUTO_SCHEDULED", dt=dt, managed_by_policy=True,
            )
        )
    for idx in range(2):
        dt = now_dt - timedelta(hours=30 + idx * 4)  # > 24h ago
        intraday_files.append(
            _seed_verified_backup_fixture(
                svc, backup_dir, seed_bytes, seed_meta,
                filename=f"agri_erp_auto_scheduled_{dt.strftime('%Y%m%d_%H%M%S')}.db",
                backup_type="AUTO_SCHEDULED", dt=dt, managed_by_policy=True,
            )
        )

    # 12. Daily AUTO_DAILY: 17 distinct days -> keep latest 14, prune oldest 3
    daily_files: List[Path] = []
    for day_offset in range(17):
        dt = now_dt - timedelta(days=day_offset)
        daily_files.append(
            _seed_verified_backup_fixture(
                svc, backup_dir, seed_bytes, seed_meta,
                filename=f"agri_erp_auto_daily_{dt.strftime('%Y%m%d_%H%M%S')}.db",
                backup_type="AUTO_DAILY", dt=dt, managed_by_policy=True,
            )
        )

    # 13. Weekly AUTO_WEEKLY: 10 distinct ISO weeks -> keep latest 8, prune oldest 2
    weekly_files: List[Path] = []
    for wk_offset in range(10):
        dt = now_dt - timedelta(weeks=wk_offset)
        weekly_files.append(
            _seed_verified_backup_fixture(
                svc, backup_dir, seed_bytes, seed_meta,
                filename=f"agri_erp_auto_weekly_{dt.strftime('%Y%m%d_%H%M%S')}.db",
                backup_type="AUTO_WEEKLY", dt=dt, managed_by_policy=True,
            )
        )

    # 14. Protected classes: 12 MANUAL, 12 BEFORE_UPGRADE, 12 BEFORE_RESTORE -> keep latest 10 each
    manual_files: List[Path] = []
    upgrade_files: List[Path] = []
    restore_files: List[Path] = []
    for idx in range(12):
        dt = now_dt - timedelta(days=idx, minutes=10)
        manual_files.append(
            _seed_verified_backup_fixture(
                svc, backup_dir, seed_bytes, seed_meta,
                filename=f"agri_erp_backup_{dt.strftime('%Y%m%d_%H%M%S')}.db",
                backup_type="MANUAL", dt=dt, managed_by_policy=True,
            )
        )
        upgrade_files.append(
            _seed_verified_backup_fixture(
                svc, backup_dir, seed_bytes, seed_meta,
                filename=f"agri_erp_before_upgrade_{dt.strftime('%Y%m%d_%H%M%S')}.db",
                backup_type="BEFORE_UPGRADE", dt=dt, managed_by_policy=True,
            )
        )
        restore_files.append(
            _seed_verified_backup_fixture(
                svc, backup_dir, seed_bytes, seed_meta,
                filename=f"agri_erp_before_restore_{dt.strftime('%Y%m%d_%H%M%S')}.db",
                backup_type="BEFORE_RESTORE", dt=dt, managed_by_policy=True,
            )
        )

    # 15. Legacy unmanaged backups (15 BEFORE_UPGRADE + 1 unknown + 1 manual) + forensic .corrupt_raw.bak
    legacy_files: List[Path] = []
    for idx in range(15):
        dt = now_dt - timedelta(days=60 + idx)
        legacy_files.append(
            _seed_verified_backup_fixture(
                svc, backup_dir, seed_bytes, seed_meta,
                filename=f"agri_erp_before_upgrade_legacy_{idx:02d}.db",
                backup_type="BEFORE_UPGRADE", dt=dt, managed_by_policy=False,
            )
        )
    unknown_legacy = _seed_verified_backup_fixture(
        svc, backup_dir, seed_bytes, seed_meta,
        filename="agri_erp_before_fresh_start_20260916_102335.db",
        backup_type="EXTERNAL", dt=now_dt - timedelta(days=90), managed_by_policy=False,
    )
    corrupt_forensic = backup_dir / "agri_erp_before_restore_20260916_100000.corrupt_raw.bak"
    corrupt_forensic.write_bytes(b"forensic_raw_bytes")

    # 17. Pin the oldest expired daily backup (simulating active download/restore)
    pinned_expired_daily = daily_files[-1]
    with pinned_backup_file(pinned_expired_daily):
        report = svc.apply_retention_policy(backup_dir=backup_dir, now_epoch=now_epoch)

    # Assertions for 11–15 & 17
    remaining_intraday = [p for p in intraday_files if p.exists()]
    assert len(remaining_intraday) == 6
    assert all(p.exists() for p in intraday_files[:6])

    remaining_daily = [p for p in daily_files if p.exists()]
    # 14 kept + 1 pinned expired daily preserved = 15
    assert len(remaining_daily) == 15
    assert pinned_expired_daily.exists()
    assert pinned_expired_daily.name in report["skipped_pinned"]

    remaining_weekly = [p for p in weekly_files if p.exists()]
    assert len(remaining_weekly) == 8
    assert all(p.exists() for p in weekly_files[:8])

    assert len([p for p in manual_files if p.exists()]) == 10
    assert len([p for p in upgrade_files if p.exists()]) == 10
    assert len([p for p in restore_files if p.exists()]) == 10

    # All legacy unmanaged + unknown + forensic files untouched
    assert all(p.exists() for p in legacy_files)
    assert unknown_legacy.exists()
    assert corrupt_forensic.exists()

    # 16. Verify minimum verified backup floor (>= 2 verified backups ALWAYS preserved)
    floor_dir = backup_dir.parent / "floor_test_backups"
    floor_dir.mkdir(parents=True, exist_ok=True)
    # Place 3 expired (> 24h old) AUTO_SCHEDULED backups: only 1 may be pruned, leaving >= 2 verified!
    for idx in range(3):
        dt = now_dt - timedelta(days=10 + idx)
        _seed_verified_backup_fixture(
            svc, floor_dir, seed_bytes, seed_meta,
            filename=f"agri_erp_auto_scheduled_{dt.strftime('%Y%m%d_%H%M%S')}.db",
            backup_type="AUTO_SCHEDULED", dt=dt, managed_by_policy=True,
        )
    floor_report = svc.apply_retention_policy(backup_dir=floor_dir, now_epoch=now_epoch)
    assert len(floor_report["pruned"]) == 1
    assert len(list(floor_dir.glob("*.db"))) == 2
    assert len(floor_report["skipped_floor"]) == 2


# =====================================================================
# 18 & 19. Manifest Rebuild & 50-Backup Listing Performance
# =====================================================================
def test_18_and_19_manifest_cache_rebuilds_and_lists_50_backups_without_full_scans(
    isolated_env: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Tests 18 & 19:
    - Listing 50 known backups serves from .backup_manifest.json with ZERO inspect_backup() calls.
    - Deleting a file, modifying a file, or corrupting the manifest automatically rebuilds cleanly.
    - Explicit inspect_backup() and restore_backup() NEVER trust a stale/tampered manifest.
    """
    db: DatabaseManager = isolated_env["db"]
    backup_dir: Path = isolated_env["backup_dir"]
    svc = BackupService(db, backup_dir=backup_dir)

    seed_bkp = svc.create_backup(backup_type="MANUAL")
    seed_bytes = seed_bkp.read_bytes()
    seed_meta = dict(svc.last_backup_metadata or {})
    seed_bkp.unlink()

    base_dt = datetime(2026, 11, 18, 12, 0, 0)
    files: List[Path] = []
    for i in range(50):
        dt = base_dt - timedelta(minutes=i)
        files.append(
            _seed_verified_backup_fixture(
                svc, backup_dir, seed_bytes, seed_meta,
                filename=f"agri_erp_backup_{dt.strftime('%Y%m%d_%H%M%S')}_{i:02d}.db",
                backup_type="MANUAL", dt=dt, managed_by_policy=True,
            )
        )

    # Spy on inspect_backup calls
    inspect_calls: List[str] = []
    orig_inspect = svc.inspect_backup

    def _spy_inspect(path):
        inspect_calls.append(Path(path).name)
        return orig_inspect(path)

    monkeypatch.setattr(svc, "inspect_backup", _spy_inspect)

    # Test 19: Warm manifest listing of 50 known backups -> ZERO inspect_backup() calls!
    t0 = time.perf_counter()
    listed = svc.list_backups(backup_dir)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert len(listed) == 50
    assert inspect_calls == [], f"Expected 0 full integrity inspections for 50 cached backups, got {len(inspect_calls)}"
    assert elapsed_ms < 500.0, f"Cached 50-backup list unexpectedly slow: {elapsed_ms:.1f}ms"

    # Test 18a: Delete 1 file & mutate 1 file on disk -> ONLY the 1 modified file is re-inspected
    deleted_file = files[0]
    deleted_file.unlink()
    mutated_file = files[1]
    mutated_file.write_bytes(b"SQLite format 3\x00" + b"\xff" * 2048)

    listed_after = svc.list_backups(backup_dir)
    assert len(listed_after) == 49
    assert inspect_calls == [mutated_file.name]
    by_name = {x["filename"]: x for x in listed_after}
    assert deleted_file.name not in by_name
    assert by_name[mutated_file.name]["integrity_ok"] is False

    # Test 18b: Tamper manifest claiming mutated_file is healthy -> restore_backup() MUST NOT trust manifest!
    manifest = svc.load_manifest(backup_dir)
    manifest["entries"][mutated_file.name]["integrity_ok"] = True
    manifest["entries"][mutated_file.name]["valid_for_restore"] = True
    svc._save_manifest_atomic(backup_dir, manifest)

    with pytest.raises(ValueError, match="integrity_check"):
        svc.restore_backup(mutated_file)

    # Test 18c: Corrupt manifest JSON -> automatically rebuilds cleanly on next list_backups()
    (backup_dir / BackupService.MANIFEST_FILENAME).write_text("{broken json!!!", encoding="utf-8")
    inspect_calls.clear()
    rebuilt = svc.list_backups(backup_dir)
    assert len(rebuilt) == 49
    assert len(inspect_calls) == 49
    # Next call is warm again (0 inspect calls)
    inspect_calls.clear()
    assert len(svc.list_backups(backup_dir)) == 49
    assert inspect_calls == []


# =====================================================================
# 20. Backup Failure Preserves Existing Backups & Reports UI Warnings
# =====================================================================
def test_20_backup_failure_preserves_existing_backups_and_surfaces_status(
    isolated_env: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """Test 20: Low-disk or backup error does not delete older backups or crash scheduler."""
    db: DatabaseManager = isolated_env["db"]
    backup_dir: Path = isolated_env["backup_dir"]
    clock = FakeClock(datetime(2026, 11, 18, 8, 0, 0))
    sched = BackupScheduler(db_manager=db, backup_dir=backup_dir, clock=clock)

    first = sched.run_check(is_startup=True)
    assert first["action"] == "created"
    existing = Path(first["file_path"])
    assert existing.is_file()

    # Trigger DB change + advance > 4h, then simulate low disk space
    MasterDataRepository(db).create_product(Product(product_name="Low Disk Era Item", default_sale_rate=200.0))
    clock.advance(hours=5)
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _p: _DiskUsage(total=50 * 1024**3, used=49 * 1024**3, free=5 * 1024 * 1024),
    )

    fail_res = sched.run_check(is_startup=False)
    assert fail_res["action"] == "failed"
    assert fail_res["reason"] == "low_disk_space"
    assert existing.is_file()  # Older verified backup untouched!

    status = sched.get_status()
    assert status["low_disk_warning"] is not None
    assert "Insufficient disk space" in status["low_disk_warning"]
    assert status["last_auto_backup"]["filename"] == existing.name


# =====================================================================
# 21. Normal POS Sales, Purchases & Accounting Continue During Backup
# =====================================================================
def test_21_sales_purchases_and_accounting_work_while_scheduled_backup_runs(
    isolated_env: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """Test 21: Normal billing/purchase/accounting transactions succeed concurrently with a scheduled backup."""
    db: DatabaseManager = isolated_env["db"]
    backup_dir: Path = isolated_env["backup_dir"]
    master = MasterDataRepository(db)
    acct = AccountingService(db)
    purchase_svc = PurchaseService(db)
    sales_svc = SalesService(db)

    # Seed baseline customer, supplier, and product
    cust_id = master.create_customer(Customer(customer_name="Ramesh Farmer", mobile="9822110011"))
    supp_id = master.create_supplier(Supplier(supplier_name="Agro Wholesale Co", mobile="9822001122"))
    prod_id = master.create_product(
        Product(
            product_name="Concurrent Urea 45kg",
            category_id=1,
            unit_id=2,
            tax_group_id=2,
            default_purchase_rate=220.0,
            default_sale_rate=266.0,
            default_mrp=266.0,
        )
    )

    clock = FakeClock(datetime(2026, 11, 18, 11, 0, 0))
    sched = BackupScheduler(db_manager=db, backup_dir=backup_dir, clock=clock)

    backup_started = threading.Event()
    allow_backup_finish = threading.Event()
    orig_online = BackupService._sqlite_online_backup

    def _slow_online_backup(src_conn, dst_path, *, pages=None, sleep=None):
        backup_started.set()
        allow_backup_finish.wait(timeout=4.0)
        return orig_online(src_conn, dst_path, pages=pages, sleep=sleep)

    monkeypatch.setattr(BackupService, "_sqlite_online_backup", staticmethod(_slow_online_backup))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        bkp_future = pool.submit(sched.run_check, is_startup=True)
        assert backup_started.wait(timeout=3.0)

        # While scheduled backup holds its read snapshot, run REAL purchase, sale & accounting ops!
        pur_id = purchase_svc.process_inward_purchase(
            Purchase(
                supplier_id=supp_id,
                invoice_no="PINV-2C3-001",
                purchase_date="2026-11-18",
                payment_mode="CREDIT",
                total_taxable=2200.0,
                total_cgst=55.0,
                total_sgst=55.0,
                net_amount=2310.0,
                paid_amount=0.0,
                due_amount=2310.0,
                items=[
                    PurchaseItem(
                        product_id=prod_id,
                        batch_no="B-2C3-01",
                        qty=10.0,
                        unit_id=2,
                        purchase_rate=220.0,
                        mrp=266.0,
                        sale_rate=266.0,
                        taxable_amount=2200.0,
                        cgst_rate=2.5,
                        cgst_amount=55.0,
                        sgst_rate=2.5,
                        sgst_amount=55.0,
                        total_amount=2310.0,
                    )
                ],
            )
        )
        batch_row = db.fetch_one("SELECT batch_id FROM stock_batches WHERE product_id = ? LIMIT 1;", (prod_id,))
        assert batch_row is not None
        batch_id = int(batch_row["batch_id"])

        sale_id = sales_svc.process_sales_invoice(
            Sale(
                invoice_no="INV-2C3-001",
                sale_date="2026-11-18",
                customer_id=cust_id,
                payment_mode="CREDIT",
                total_taxable=506.67,
                total_cgst=12.67,
                total_sgst=12.66,
                net_amount=532.0,
                paid_amount=0.0,
                due_amount=532.0,
                items=[
                    SaleItem(
                        product_id=prod_id,
                        batch_id=batch_id,
                        qty=2.0,
                        unit_id=2,
                        sale_rate=266.0,
                        mrp=266.0,
                        taxable_amount=506.67,
                        cgst_rate=2.5,
                        cgst_amount=12.67,
                        sgst_rate=2.5,
                        sgst_amount=12.66,
                        total_amount=532.0,
                    )
                ],
            )
        )
        exp_id = acct.record_shop_expense(
            Expense(
                expense_date="2026-11-18",
                category_id=1,
                amount=250.0,
                payment_mode="CASH",
                description="Shop tea & transport during backup",
            )
        )
        rcpt_id = acct.record_customer_receipt(
            customer_id=cust_id,
            receipt_date="2026-11-18",
            amount=500.0,
            payment_mode="CASH",
            narration="Farmer payment during backup",
        )
        pay_id = acct.record_supplier_payment(
            supplier_id=supp_id,
            payment_date="2026-11-18",
            amount=300.0,
            payment_mode="CASH",
            narration="Supplier payment during backup",
        )

        allow_backup_finish.set()
        bkp_res = bkp_future.result(timeout=5.0)

    assert bkp_res["action"] == "created"
    assert pur_id > 0 and sale_id > 0 and exp_id > 0 and rcpt_id > 0 and pay_id > 0
    assert db.fetch_one("SELECT net_amount FROM purchases WHERE purchase_id = ?;", (pur_id,))["net_amount"] == pytest.approx(2310.0)
    assert db.fetch_one("SELECT net_amount FROM sales WHERE sale_id = ?;", (sale_id,))["net_amount"] == pytest.approx(532.0)
    assert db.fetch_one("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch_id,))["current_qty"] == pytest.approx(8.0)
    assert db.fetch_one("SELECT amount FROM expenses WHERE expense_id = ?;", (exp_id,))["amount"] == pytest.approx(250.0)
    assert db.fetch_one("SELECT total_amount FROM vouchers WHERE voucher_id = ?;", (rcpt_id,))["total_amount"] == pytest.approx(500.0)
    assert db.fetch_one("SELECT total_amount FROM vouchers WHERE voucher_id = ?;", (pay_id,))["total_amount"] == pytest.approx(300.0)
    assert db.check_integrity() == ["ok"]
    assert db.check_foreign_keys() == []
    assert BackupService(db, backup_dir=backup_dir).inspect_backup(Path(bkp_res["file_path"]))["valid_for_restore"] is True


# =====================================================================
# 22. Backup Manager API & Frontend UI Contract Check
# =====================================================================
def test_22_backup_manager_scheduler_api_and_ui_contract(isolated_env: Dict[str, Any]):
    """Verify /api/system/backups, /api/system/backup-status, and new English-only UI status IDs."""
    headers = isolated_env["admin_headers"]
    with TestClient(app) as client:
        res_list = client.get("/api/system/backups", headers=headers)
        assert res_list.status_code == 200
        payload = res_list.json()
        assert "scheduler" in payload
        assert payload["scheduler"]["running"] is True
        assert payload["scheduler"]["status_label"] == "Running"
        assert payload["scheduler"]["check_interval_seconds"] == 900
        assert payload["scheduler"]["min_backup_interval_seconds"] == 14400

        res_status = client.get("/api/system/backup-status", headers=headers)
        assert res_status.status_code == 200
        assert res_status.json()["running"] is True

    # Verify new UI elements exist with unique IDs, English-only text, and zero native dialogs
    html = INDEX_HTML_PATH.read_text(encoding="utf-8")
    js = APP_JS_PATH.read_text(encoding="utf-8")
    for elem_id in (
        "bkp-scheduler-strip",
        "bkp-sched-status-badge",
        "bkp-sched-last-auto",
        "bkp-sched-next-check",
        "bkp-sched-low-disk-warning",
        "bkp-sched-last-error",
    ):
        assert f'id="{elem_id}"' in html
    assert "function _renderBackupSchedulerStatus(" in js
    for pat in (r"\balert\s*\(", r"\bconfirm\s*\(", r"\bprompt\s*\(", r"window\.open\s*\("):
        assert not re.findall(pat, js)


# =====================================================================
# 23. Dedicated Weekly Automatic Backup Generation & Restart Deduplication
# =====================================================================
def test_23_weekly_backup_generation_coexistence_and_restart_deduplication(
    isolated_env: Dict[str, Any],
):
    """
    Dedicated verification of AUTO_WEEKLY generation:
    1. Creates at most ONE verified AUTO_WEEKLY snapshot per ISO week when data changed
       since the previous weekly snapshot (even if mid-week changes were already backed up
       by AUTO_DAILY in the prior week).
    2. Prevents duplicate AUTO_WEEKLY snapshots across application restarts within the same ISO week.
    3. Coexists cleanly with 4-hour AUTO_SCHEDULED and subsequent-day AUTO_DAILY backups.
    4. Skips redundant AUTO_WEEKLY in a new ISO week when the DB remained completely unchanged
       since the previous AUTO_WEEKLY snapshot.
    """
    db: DatabaseManager = isolated_env["db"]
    backup_dir: Path = isolated_env["backup_dir"]
    master = MasterDataRepository(db)

    # Week 47 (Mon 2026-11-16): initial startup -> AUTO_DAILY, then Friday write -> Friday AUTO_DAILY
    clock = FakeClock(datetime(2026, 11, 16, 9, 0, 0))  # ISO (2026, 47)
    master.create_product(Product(product_name="Week47 Initial Item", default_sale_rate=100.0))
    sched = BackupScheduler(db_manager=db, backup_dir=backup_dir, clock=clock)

    r_w47_mon = sched.run_check(is_startup=True)
    assert r_w47_mon["action"] == "created"
    assert r_w47_mon["backup_type"] == "AUTO_DAILY"

    # Mid-week Friday change + Friday AUTO_DAILY backup
    clock.set_dt(datetime(2026, 11, 20, 10, 0, 0))  # Friday ISO (2026, 47)
    fri_pid = master.create_product(Product(product_name="Week47 Friday Sale Item", default_sale_rate=320.0))
    r_w47_fri = sched.run_check(is_startup=True)
    assert r_w47_fri["action"] == "created"
    assert r_w47_fri["backup_type"] == "AUTO_DAILY"

    # Week 48 (Mon 2026-11-23): NO new writes since Friday's AUTO_DAILY,
    # YET data HAS changed since prior weekly (none existed yet) -> MUST generate Week 48 AUTO_WEEKLY!
    clock.set_dt(datetime(2026, 11, 23, 9, 0, 0))  # Monday ISO (2026, 48)
    r_w48_wk = sched.run_check(is_startup=True)
    assert r_w48_wk["action"] == "created"
    assert r_w48_wk["backup_type"] == "AUTO_WEEKLY"
    w48_path = Path(r_w48_wk["file_path"])
    assert w48_path.name.startswith("agri_erp_auto_weekly_")
    assert sched.backup_service.inspect_backup(w48_path)["valid_for_restore"] is True
    # Verify Week 47 Friday item is actually stored inside Week 48 AUTO_WEEKLY
    c_wk = sqlite3.connect(f"{w48_path.resolve().as_uri()}?immutable=1", uri=True)
    try:
        assert c_wk.execute("SELECT product_name FROM products WHERE product_id = ?;", (fri_pid,)).fetchone()[0] == (
            "Week47 Friday Sale Item"
        )
    finally:
        c_wk.close()

    # Restart simulation (fresh BackupScheduler instance) same day & later same week:
    # MUST NOT create a duplicate AUTO_WEEKLY in ISO week 48!
    clock.advance(minutes=45)
    sched_restarted = BackupScheduler(db_manager=db, backup_dir=backup_dir, clock=clock)
    r_dup_restart = sched_restarted.run_check(is_startup=True)
    assert r_dup_restart["action"] == "skipped"
    assert r_dup_restart["reason"] == "today_backup_exists"

    # Coexistence with 4-hour AUTO_SCHEDULED on Monday afternoon of Week 48
    master.create_product(Product(product_name="Week48 Afternoon Change", default_sale_rate=410.0))
    clock.advance(hours=4, minutes=15)
    r_w48_sched = sched_restarted.run_check(is_startup=False)
    assert r_w48_sched["action"] == "created"
    assert r_w48_sched["backup_type"] == "AUTO_SCHEDULED"

    # Coexistence with Tuesday AUTO_DAILY in Week 48 (still only ONE AUTO_WEEKLY in Week 48)
    clock.set_dt(datetime(2026, 11, 24, 9, 0, 0))  # Tuesday ISO (2026, 48)
    tue_pid = master.create_product(Product(product_name="Week48 Tuesday Change", default_sale_rate=515.0))
    r_w48_tue = sched_restarted.run_check(is_startup=True)
    assert r_w48_tue["action"] == "created"
    assert r_w48_tue["backup_type"] == "AUTO_DAILY"

    w48_weeklies = [
        b for b in sched_restarted.backup_service.list_backups(backup_dir)
        if b["backup_type"] == "AUTO_WEEKLY"
    ]
    assert len(w48_weeklies) == 1

    # Week 49 (Mon 2026-11-30): NO new write on Monday morning, BUT Tuesday's change occurred
    # AFTER Week 48's AUTO_WEEKLY -> MUST generate Week 49's AUTO_WEEKLY capturing Tuesday's item!
    clock.set_dt(datetime(2026, 11, 30, 9, 0, 0))  # Monday ISO (2026, 49)
    sched_w49 = BackupScheduler(db_manager=db, backup_dir=backup_dir, clock=clock)
    r_w49_wk = sched_w49.run_check(is_startup=True)
    assert r_w49_wk["action"] == "created"
    assert r_w49_wk["backup_type"] == "AUTO_WEEKLY"
    w49_path = Path(r_w49_wk["file_path"])
    c_w49 = sqlite3.connect(f"{w49_path.resolve().as_uri()}?immutable=1", uri=True)
    try:
        assert c_w49.execute("SELECT product_name FROM products WHERE product_id = ?;", (tue_pid,)).fetchone()[0] == (
            "Week48 Tuesday Change"
        )
    finally:
        c_w49.close()

    # Restart on Wednesday of Week 49 after NO new writes -> MUST NOT duplicate Week 49 AUTO_WEEKLY
    clock.set_dt(datetime(2026, 12, 2, 9, 0, 0))  # Wednesday ISO (2026, 49)
    sched_w49_wed = BackupScheduler(db_manager=db, backup_dir=backup_dir, clock=clock)
    r_w49_idle = sched_w49_wed.run_check(is_startup=True)
    assert r_w49_idle["action"] == "skipped"
    assert r_w49_idle["reason"] == "database_unchanged"

    # Week 50 (Mon 2026-12-07): ZERO writes occurred anywhere since Week 49's AUTO_WEEKLY ->
    # MUST NOT create a redundant AUTO_WEEKLY on a completely unchanged database!
    clock.set_dt(datetime(2026, 12, 7, 9, 0, 0))  # Monday ISO (2026, 50)
    sched_w50 = BackupScheduler(db_manager=db, backup_dir=backup_dir, clock=clock)
    r_w50_idle = sched_w50.run_check(is_startup=True)
    assert r_w50_idle["action"] == "skipped"
    assert r_w50_idle["reason"] == "database_unchanged"

    all_weeklies = [
        b for b in sched_w50.backup_service.list_backups(backup_dir)
        if b["backup_type"] == "AUTO_WEEKLY"
    ]
    assert len(all_weeklies) == 2

