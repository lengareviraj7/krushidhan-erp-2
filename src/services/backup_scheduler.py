"""
Single-Instance Automatic Local Backup Scheduler for Krushidhan ERP (Phase 2C-3).

Runs a lightweight background worker thread that reuses the hardened Phase 2C-2
BackupService (WAL-safe chunked online backup, DELETE-mode checkpoint verification,
PRAGMA integrity_check + foreign_key_check + schema validation, SHA-256, fsync,
and disk-space protection) plus conservative GFS retention pruning.

Guarantees:
- Starts at most once per application process (idempotent against sidecar + lifespan init).
- Never blocks app startup, billing, stock, purchases, or accounting operations.
- Coalesces concurrent scheduled and manual backups via non-blocking locks.
- Skips cleanly whenever exclusive database maintenance (migrations/restore) is active.
- Never crashes the application on backup or low-disk errors; never prunes on failure.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from src.db.connection import DatabaseMaintenanceError, DatabaseManager, get_db_manager
from src.desktop_runtime import redact_sensitive_text
from src.services.backup_service import (
    BackupInProgressError,
    BackupService,
    InsufficientDiskSpaceError,
)

logger = logging.getLogger("agri_erp.backup_scheduler")

DEFAULT_CHECK_INTERVAL_SECONDS = 15 * 60       # Check every 15 minutes
DEFAULT_MIN_BACKUP_INTERVAL_SECONDS = 4 * 3600 # Create periodic backup only after >= 4 hours
DEFAULT_STARTUP_DELAY_SECONDS = 5.0            # Non-blocking startup grace delay
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class BackupScheduler:
    """
    Single-instance background scheduler managing automatic local SQLite backups
    and conservative post-verification GFS retention.
    """

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        backup_dir: Optional[Union[str, Path]] = None,
        *,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        min_backup_interval_seconds: float = DEFAULT_MIN_BACKUP_INTERVAL_SECONDS,
        startup_delay_seconds: float = DEFAULT_STARTUP_DELAY_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        self.db = db_manager or get_db_manager()
        self._configured_backup_dir = Path(backup_dir) if backup_dir is not None else None
        self.backup_service = BackupService(self.db, backup_dir=self._configured_backup_dir)
        self.check_interval_seconds = max(0.05, float(check_interval_seconds))
        self.min_backup_interval_seconds = max(0.0, float(min_backup_interval_seconds))
        self.startup_delay_seconds = max(0.0, float(startup_delay_seconds))
        self._clock = clock

        self._state_lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False
        self._backup_in_progress = False
        self._start_count = 0

        # Observable runtime telemetry for Backup Manager UI
        self._last_check_epoch: Optional[float] = None
        self._next_check_epoch: Optional[float] = None
        self._last_auto_backup: Optional[Dict[str, Any]] = None
        self._last_error: Optional[str] = None
        self._last_error_epoch: Optional[float] = None
        self._low_disk_warning: Optional[str] = None
        self._last_retention_pruned_count: int = 0
        # Track in-process write_generation recorded at last verified backup for this DB instance
        self._last_verified_write_gen: Optional[int] = None
        self._last_verified_fingerprint_in_mem: Optional[str] = None
        self._last_weekly_write_gen: Optional[int] = None
        self._last_weekly_fingerprint_in_mem: Optional[str] = None
        self._offsite_manager: Optional[Any] = None

    def _get_offsite_manager(self):
        if self._offsite_manager is None:
            from src.services.offsite_backup_service import OffsiteBackupManager

            self._offsite_manager = OffsiteBackupManager(
                db_manager=self.db,
                backup_dir=self._configured_backup_dir,
                clock=self._clock,
            )
        return self._offsite_manager

    def _trigger_offsite_publish_safely(
        self,
        backup_path: Optional[Path] = None,
        *,
        sync_offsite: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Optionally enqueue encrypted off-site publishing after a verified local backup, or
        process a due bounded retry when the synchronized folder becomes available again.
        Never raises or blocks ERP operations.
        """
        try:
            mgr = self._get_offsite_manager()
            state = mgr.load_state()
            if not state.get("enabled") or not state.get("destination_folder"):
                return None
            if backup_path is not None:
                if sync_offsite:
                    return mgr.publish_verified_backup(backup_path, allow_duplicate=False)
                enqueued = mgr.enqueue_async_publish(backup_path)
                return {"action": "enqueued" if enqueued else "skipped"}
            # Retry pending deferred publish only when backoff has elapsed
            if state.get("pending_retry"):
                if sync_offsite:
                    return mgr.process_pending_retry_if_due()
                pending = state["pending_retry"]
                next_ep = float(pending.get("next_retry_epoch") or 0.0)
                if self._now() >= next_ep and pending.get("source_backup_path"):
                    enqueued = mgr.enqueue_async_publish(Path(pending["source_backup_path"]))
                    return {"action": "retry_enqueued" if enqueued else "skipped"}
        except Exception as exc:
            logger.warning("Non-fatal off-site backup scheduler hook warning: %s", redact_sensitive_text(str(exc)))
        return None

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return bool(self._running and self._worker_thread is not None and self._worker_thread.is_alive())

    @property
    def start_count(self) -> int:
        with self._state_lock:
            return self._start_count

    def _now(self) -> float:
        return float(self._clock())

    @staticmethod
    def _fmt_iso(epoch: Optional[float]) -> Optional[str]:
        if epoch is None:
            return None
        try:
            return datetime.fromtimestamp(float(epoch)).isoformat(timespec="seconds")
        except (OSError, OverflowError, ValueError):
            return None

    @staticmethod
    def _fmt_display(epoch: Optional[float]) -> Optional[str]:
        if epoch is None:
            return None
        try:
            return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return None

    def start(self) -> bool:
        """
        Start the background scheduler thread once. Strictly idempotent:
        repeated calls while already running return False without spawning duplicate threads.
        """
        if self.db.db_path == ":memory:" or getattr(self.db, "is_vercel", False):
            return False

        with self._state_lock:
            if self._running and self._worker_thread is not None and self._worker_thread.is_alive():
                return False

            self._stop_event.clear()
            now = self._now()
            self._next_check_epoch = now + self.startup_delay_seconds
            worker = threading.Thread(
                target=self._worker_loop,
                name="krushidhan-backup-scheduler",
                daemon=True,
            )
            self._worker_thread = worker
            self._running = True
            self._start_count += 1
            worker.start()
            logger.info(
                "BackupScheduler started (check_interval=%ds, min_backup_interval=%ds, startup_delay=%.1fs).",
                int(self.check_interval_seconds),
                int(self.min_backup_interval_seconds),
                self.startup_delay_seconds,
            )
            return True

    def stop(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> bool:
        """Signal the background worker to stop and join it within a bounded timeout."""
        with self._state_lock:
            self._stop_event.set()
            worker = self._worker_thread
            self._running = False
            self._next_check_epoch = None

        if worker is None:
            return True

        if worker is not threading.current_thread():
            worker.join(timeout=max(0.1, float(timeout)))

        stopped_cleanly = not worker.is_alive()
        with self._state_lock:
            if stopped_cleanly:
                self._worker_thread = None
        if not stopped_cleanly:
            logger.warning("BackupScheduler worker thread did not finish within %.1fs shutdown timeout.", timeout)
        else:
            logger.info("BackupScheduler stopped cleanly.")
        return stopped_cleanly

    def _worker_loop(self) -> None:
        try:
            # Non-blocking startup grace wait so app boot & UI stay instantaneous
            if self.startup_delay_seconds > 0:
                if self._stop_event.wait(timeout=self.startup_delay_seconds):
                    return

            if not self._stop_event.is_set():
                self.run_check(is_startup=True)

            while not self._stop_event.is_set():
                with self._state_lock:
                    self._next_check_epoch = self._now() + self.check_interval_seconds
                if self._stop_event.wait(timeout=self.check_interval_seconds):
                    break
                if not self._stop_event.is_set():
                    self.run_check(is_startup=False)
        except Exception as exc:
            safe_err = redact_sensitive_text(str(exc))
            logger.error("Unexpected BackupScheduler worker loop error: %s", safe_err)
            with self._state_lock:
                self._last_error = f"Scheduler loop warning: {safe_err}"
                self._last_error_epoch = self._now()
        finally:
            with self._state_lock:
                self._running = False
                self._next_check_epoch = None

    def _has_database_changed_since_last_backup(
        self,
        manifest: Dict[str, Any],
        verified_backups: List[Dict[str, Any]],
    ) -> bool:
        """
        Determine using O(1) metadata whether the live database has changed since
        the most recent verified backup (without scanning database contents).
        """
        if not verified_backups:
            return True

        current_fp = self.backup_service.compute_live_db_fingerprint()
        if current_fp is None:
            return False

        last_fp = manifest.get("last_verified_source_fingerprint") or self._last_verified_fingerprint_in_mem
        if last_fp is None:
            # Legacy backups exist but have no recorded source fingerprint yet:
            # compare live DB/WAL mtime against the latest verified backup mtime as a safe fallback.
            latest_bkp_mtime = max(float(b["mtime_epoch"]) for b in verified_backups)
            live_db = Path(self.db.db_path)
            db_mtime = live_db.stat().st_mtime if live_db.exists() else 0.0
            wal_file = Path(f"{live_db}-wal")
            wal_mtime = wal_file.stat().st_mtime if (wal_file.exists() and wal_file.stat().st_size > 0) else 0.0
            return max(db_mtime, wal_mtime) > (latest_bkp_mtime + 1.0)

        if current_fp != last_fp:
            return True

        # Same-process write generation check catches rare in-place WAL frame rewrites
        with self._state_lock:
            mem_gen = self._last_verified_write_gen
        if mem_gen is not None and self.db.write_generation > mem_gen:
            return True

        return False

    def _has_changed_since_last_weekly(
        self,
        manifest: Dict[str, Any],
        verified_backups: List[Dict[str, Any]],
    ) -> bool:
        """
        Return True when live database state has changed since the most recent verified
        AUTO_WEEKLY snapshot (or when no verified AUTO_WEEKLY snapshot exists yet).
        Prevents redundant weekly backups on a completely idle database while ensuring
        mid-week changes already captured by AUTO_DAILY still produce a new week's AUTO_WEEKLY.
        """
        weekly_backups = [
            b for b in verified_backups if str(b.get("backup_type") or "").upper() == "AUTO_WEEKLY"
        ]
        if not weekly_backups:
            return True

        current_fp = self.backup_service.compute_live_db_fingerprint()
        if current_fp is None:
            return False

        weekly_backups.sort(key=lambda b: (float(b["mtime_epoch"]), str(b["filename"])), reverse=True)
        latest_weekly = weekly_backups[0]
        entries = manifest.get("entries") or {}
        weekly_entry = entries.get(latest_weekly["filename"]) if isinstance(entries, dict) else None
        last_weekly_fp = (
            (weekly_entry.get("source_fingerprint") if isinstance(weekly_entry, dict) else None)
            or self._last_weekly_fingerprint_in_mem
        )

        if last_weekly_fp is None:
            latest_weekly_mtime = float(latest_weekly["mtime_epoch"])
            live_db = Path(self.db.db_path)
            db_mtime = live_db.stat().st_mtime if live_db.exists() else 0.0
            wal_file = Path(f"{live_db}-wal")
            wal_mtime = wal_file.stat().st_mtime if (wal_file.exists() and wal_file.stat().st_size > 0) else 0.0
            return max(db_mtime, wal_mtime) > (latest_weekly_mtime + 1.0)

        if current_fp != last_weekly_fp:
            return True

        with self._state_lock:
            mem_wk_gen = self._last_weekly_write_gen
        if mem_wk_gen is not None and self.db.write_generation > mem_wk_gen:
            return True

        return False

    @staticmethod
    def _iso_week_key(epoch_or_dt: Union[float, datetime]) -> Tuple[int, int]:
        dt = epoch_or_dt if isinstance(epoch_or_dt, datetime) else datetime.fromtimestamp(float(epoch_or_dt))
        iso = dt.isocalendar()
        return (int(iso[0]), int(iso[1]))

    @classmethod
    def _select_auto_backup_type(
        cls,
        now_dt: datetime,
        verified_backups: List[Dict[str, Any]],
        *,
        weekly_due: bool = False,
    ) -> str:
        """
        Select unambiguous automatic backup type:
        - AUTO_WEEKLY: when weekly_due is True (new ISO week with no verified AUTO_WEEKLY yet
          and changes since the previous weekly snapshot).
        - AUTO_DAILY: if no verified backup exists for the current local calendar day.
        - AUTO_SCHEDULED: intraday periodic backup when today already has a verified backup.
        """
        if weekly_due:
            return "AUTO_WEEKLY"

        today_str = now_dt.strftime("%Y-%m-%d")
        has_today_verified = any(
            datetime.fromtimestamp(float(b["mtime_epoch"])).strftime("%Y-%m-%d") == today_str
            for b in verified_backups
        )
        if not has_today_verified:
            return "AUTO_DAILY"
        return "AUTO_SCHEDULED"

    def run_check(self, *, is_startup: bool = False, sync_offsite: bool = False) -> Dict[str, Any]:
        """
        Evaluate backup schedule policy and execute at most one non-overlapping backup if due.
        Safe to invoke directly from tests with an injectable clock.
        """
        now_epoch = self._now()
        now_dt = datetime.fromtimestamp(now_epoch)

        # Non-blocking single-flight guard: coalesce concurrent scheduler invocations immediately
        if not self._run_lock.acquire(blocking=False):
            return {"action": "skipped", "reason": "scheduler_busy"}

        try:
            with self._state_lock:
                self._last_check_epoch = now_epoch

            if self.db.db_path == ":memory:" or getattr(self.db, "is_vercel", False):
                return {"action": "skipped", "reason": "unsupported_mode"}

            if self.db.is_in_maintenance:
                return {"action": "skipped", "reason": "maintenance_active"}

            live_db = Path(self.db.db_path)
            if not live_db.exists():
                return {"action": "skipped", "reason": "database_missing"}

            target_dir = self.backup_service.get_backup_dir()
            all_backups = self.backup_service.list_backups(target_dir)
            verified_backups = [
                b for b in all_backups if b.get("integrity_ok") and b.get("valid_for_restore")
            ]
            manifest = self.backup_service.load_manifest(target_dir)

            # Hydrate in-memory write_generation baseline if manifest already reflects latest DB
            if self._last_verified_write_gen is None and manifest.get("last_verified_source_fingerprint"):
                if manifest.get("last_verified_source_fingerprint") == self.backup_service.compute_live_db_fingerprint():
                    self._last_verified_write_gen = self.db.write_generation
                    self._last_verified_fingerprint_in_mem = manifest.get("last_verified_source_fingerprint")

            today_str = now_dt.strftime("%Y-%m-%d")
            cur_week = self._iso_week_key(now_dt)
            has_today_verified = any(
                datetime.fromtimestamp(float(b["mtime_epoch"])).strftime("%Y-%m-%d") == today_str
                for b in verified_backups
            )
            has_this_week_auto_weekly = any(
                str(b.get("backup_type") or "").upper() == "AUTO_WEEKLY"
                and self._iso_week_key(float(b["mtime_epoch"])) == cur_week
                for b in verified_backups
            )
            has_prior_week_verified = any(
                self._iso_week_key(float(b["mtime_epoch"])) < cur_week
                for b in verified_backups
            )
            weekly_due = (
                (not has_this_week_auto_weekly)
                and has_prior_week_verified
                and self._has_changed_since_last_weekly(manifest, verified_backups)
            )
            db_changed = self._has_database_changed_since_last_backup(manifest, verified_backups)

            if is_startup:
                if not weekly_due:
                    if has_today_verified:
                        self._trigger_offsite_publish_safely(None, sync_offsite=sync_offsite)
                        return {"action": "skipped", "reason": "today_backup_exists"}
                    if not db_changed:
                        self._trigger_offsite_publish_safely(None, sync_offsite=sync_offsite)
                        return {"action": "skipped", "reason": "database_unchanged"}
            else:
                if verified_backups:
                    latest_verified_mtime = max(float(b["mtime_epoch"]) for b in verified_backups)
                    elapsed = now_epoch - latest_verified_mtime
                    if elapsed < self.min_backup_interval_seconds:
                        self._trigger_offsite_publish_safely(None, sync_offsite=sync_offsite)
                        return {
                            "action": "skipped",
                            "reason": "interval_not_elapsed",
                            "elapsed_seconds": elapsed,
                        }
                if not (db_changed or weekly_due):
                    self._trigger_offsite_publish_safely(None, sync_offsite=sync_offsite)
                    return {"action": "skipped", "reason": "database_unchanged"}

            if self.db.is_in_maintenance:
                return {"action": "skipped", "reason": "maintenance_active"}

            chosen_type = self._select_auto_backup_type(now_dt, verified_backups, weekly_due=weekly_due)

            with self._state_lock:
                self._backup_in_progress = True

            try:
                backup_path = self.backup_service.create_backup(
                    backup_dir=target_dir,
                    backup_type=chosen_type,
                    blocking=False,
                    timestamp_dt=now_dt,
                    managed_by_policy=True,
                )
            except BackupInProgressError:
                return {"action": "skipped", "reason": "backup_already_in_progress"}
            except DatabaseMaintenanceError:
                return {"action": "skipped", "reason": "maintenance_active"}
            except InsufficientDiskSpaceError as exc:
                safe_msg = redact_sensitive_text(str(exc))
                with self._state_lock:
                    self._low_disk_warning = safe_msg
                    self._last_error = safe_msg
                    self._last_error_epoch = self._now()
                logger.warning("Scheduled backup skipped due to low disk space: %s", safe_msg)
                return {"action": "failed", "reason": "low_disk_space", "error": safe_msg}
            except Exception as exc:
                safe_msg = redact_sensitive_text(str(exc))
                with self._state_lock:
                    self._last_error = safe_msg
                    self._last_error_epoch = self._now()
                logger.error("Scheduled automatic backup (%s) failed safely: %s", chosen_type, safe_msg)
                return {"action": "failed", "reason": "backup_error", "error": safe_msg}
            finally:
                with self._state_lock:
                    self._backup_in_progress = False

            # Record post-backup state & telemetry
            meta = dict(self.backup_service.last_backup_metadata or {})
            st = backup_path.stat()
            auto_summary = {
                "filename": backup_path.name,
                "backup_type": chosen_type,
                "size_bytes": int(st.st_size),
                "size_display": BackupService._format_size_display(st.st_size),
                "created_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "created_at_iso": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "sha256": self.backup_service.last_backup_sha256 or meta.get("sha256"),
            }
            post_fp = self.backup_service.compute_live_db_fingerprint()
            post_gen = self.db.write_generation

            # Run conservative GFS retention ONLY AFTER new backup passed all verification stages
            retention_report = self.backup_service.apply_retention_policy(
                backup_dir=target_dir,
                now_epoch=now_epoch,
            )

            with self._state_lock:
                self._last_auto_backup = auto_summary
                self._last_error = None
                self._last_error_epoch = None
                self._low_disk_warning = None
                self._last_verified_fingerprint_in_mem = post_fp
                self._last_verified_write_gen = post_gen
                if chosen_type == "AUTO_WEEKLY":
                    self._last_weekly_fingerprint_in_mem = post_fp
                    self._last_weekly_write_gen = post_gen
                self._last_retention_pruned_count = len(retention_report.get("pruned") or [])

            logger.info(
                "Automatic local backup created: %s (%s, %s); retention pruned %d file(s).",
                backup_path.name,
                chosen_type,
                auto_summary["size_display"],
                len(retention_report.get("pruned") or []),
            )
            offsite_result = self._trigger_offsite_publish_safely(backup_path, sync_offsite=sync_offsite)
            return {
                "action": "created",
                "backup_type": chosen_type,
                "filename": backup_path.name,
                "file_path": str(backup_path),
                "sha256": auto_summary["sha256"],
                "retention": retention_report,
                "offsite": offsite_result,
            }
        finally:
            self._run_lock.release()

    def _compute_disk_status(self) -> Optional[Dict[str, Any]]:
        if self.db.db_path == ":memory:":
            return None
        try:
            target_dir = self.backup_service.get_backup_dir()
            check_dir = target_dir if target_dir.exists() else target_dir.parent
            if not check_dir.exists():
                return None
            required_bytes, _, _ = self.backup_service.estimate_required_backup_space()
            available_bytes = int(shutil.disk_usage(str(check_dir)).free)
            low_space = available_bytes < required_bytes
            return {
                "available_bytes": available_bytes,
                "required_bytes": required_bytes,
                "available_display": BackupService._format_size_display(available_bytes),
                "required_display": BackupService._format_size_display(required_bytes),
                "low_space": low_space,
            }
        except OSError:
            return None

    def get_status(self, *, backups: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Return safe, non-sensitive scheduler status & telemetry for the Backup Manager UI."""
        all_backups = backups
        if all_backups is None:
            try:
                all_backups = self.backup_service.list_backups()
            except Exception:
                all_backups = []

        verified_backups = [
            b for b in all_backups if b.get("integrity_ok") and b.get("valid_for_restore")
        ]
        auto_verified = [
            b for b in verified_backups
            if str(b.get("backup_type") or "").upper() in BackupService.AUTO_BACKUP_TYPES
        ]

        with self._state_lock:
            running = self.is_running
            backup_in_progress = self._backup_in_progress
            last_check_epoch = self._last_check_epoch
            next_check_epoch = self._next_check_epoch if running else None
            last_auto = dict(self._last_auto_backup) if self._last_auto_backup else None
            last_error = self._last_error
            last_error_epoch = self._last_error_epoch
            low_disk_warning = self._low_disk_warning
            last_pruned = self._last_retention_pruned_count

        if last_auto is None and auto_verified:
            newest_auto = auto_verified[0]
            last_auto = {
                "filename": newest_auto["filename"],
                "backup_type": newest_auto["backup_type"],
                "size_bytes": newest_auto["size_bytes"],
                "size_display": newest_auto["size_display"],
                "created_at": newest_auto["modified_at"],
                "created_at_iso": newest_auto["created_at_iso"],
                "sha256": newest_auto.get("sha256"),
            }

        last_verified: Optional[Dict[str, Any]] = None
        estimated_next_backup_epoch: Optional[float] = None
        if verified_backups:
            newest_v = verified_backups[0]
            last_verified = {
                "filename": newest_v["filename"],
                "backup_type": newest_v["backup_type"],
                "size_bytes": newest_v["size_bytes"],
                "size_display": newest_v["size_display"],
                "created_at": newest_v["modified_at"],
                "created_at_iso": newest_v["created_at_iso"],
            }
            if running:
                candidate_next = float(newest_v["mtime_epoch"]) + self.min_backup_interval_seconds
                if next_check_epoch is not None:
                    estimated_next_backup_epoch = max(candidate_next, next_check_epoch)
                else:
                    estimated_next_backup_epoch = candidate_next
        elif running:
            estimated_next_backup_epoch = next_check_epoch

        disk_space = self._compute_disk_status()
        if disk_space and disk_space.get("low_space") and not low_disk_warning:
            low_disk_warning = (
                f"Low disk space warning: {disk_space['available_display']} free, "
                f"minimum {disk_space['required_display']} recommended for verified backups. "
                "Existing backups and normal billing are unaffected."
            )

        return {
            "running": running,
            "status_label": "Running" if running else "Stopped",
            "backup_in_progress": backup_in_progress,
            "check_interval_seconds": int(self.check_interval_seconds),
            "min_backup_interval_seconds": int(self.min_backup_interval_seconds),
            "last_check_at": self._fmt_display(last_check_epoch),
            "last_check_at_iso": self._fmt_iso(last_check_epoch),
            "next_check_at": self._fmt_display(next_check_epoch),
            "next_check_at_iso": self._fmt_iso(next_check_epoch),
            "estimated_next_backup_at": self._fmt_display(estimated_next_backup_epoch),
            "estimated_next_backup_at_iso": self._fmt_iso(estimated_next_backup_epoch),
            "last_auto_backup": last_auto,
            "last_verified_backup": last_verified,
            "last_error": last_error,
            "last_error_at": self._fmt_display(last_error_epoch),
            "low_disk_warning": low_disk_warning,
            "disk_space": disk_space,
            "last_retention_pruned_count": last_pruned,
        }


# ---------------------------------------------------------------------------
# Process-Wide Single-Instance Scheduler Management
# ---------------------------------------------------------------------------
_SCHEDULER_REGISTRY_LOCK = threading.RLock()
_GLOBAL_SCHEDULER: Optional[BackupScheduler] = None
_GLOBAL_SCHEDULER_KEY: Optional[Tuple[str, str]] = None


def _scheduler_key_for_db(
    db: DatabaseManager,
    backup_dir: Optional[Union[str, Path]] = None,
) -> Tuple[str, str]:
    db_str = str(Path(db.db_path).resolve()) if db.db_path != ":memory:" else ":memory:"
    bdir = BackupService(db, backup_dir=backup_dir).get_backup_dir()
    return (db_str, str(bdir.resolve()))


def get_backup_scheduler(
    db_manager: Optional[DatabaseManager] = None,
    backup_dir: Optional[Union[str, Path]] = None,
) -> Optional[BackupScheduler]:
    """Return the active process-wide BackupScheduler if initialized for the active DB."""
    db = db_manager or get_db_manager()
    if db.db_path == ":memory:" or getattr(db, "is_vercel", False):
        return None
    target_key = _scheduler_key_for_db(db, backup_dir=backup_dir)
    with _SCHEDULER_REGISTRY_LOCK:
        if _GLOBAL_SCHEDULER is not None and _GLOBAL_SCHEDULER_KEY == target_key:
            return _GLOBAL_SCHEDULER
        return None


def start_backup_scheduler(
    db_manager: Optional[DatabaseManager] = None,
    backup_dir: Optional[Union[str, Path]] = None,
    *,
    check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
    min_backup_interval_seconds: float = DEFAULT_MIN_BACKUP_INTERVAL_SECONDS,
    startup_delay_seconds: float = DEFAULT_STARTUP_DELAY_SECONDS,
    clock: Callable[[], float] = time.time,
) -> Optional[BackupScheduler]:
    """
    Ensure exactly ONE BackupScheduler is running for the active database in this process.
    Repeated invocations (e.g. sidecar + FastAPI lifespan or multiple init paths) return the
    already-running singleton without creating duplicate threads.
    """
    global _GLOBAL_SCHEDULER, _GLOBAL_SCHEDULER_KEY
    db = db_manager or get_db_manager()
    if db.db_path == ":memory:" or getattr(db, "is_vercel", False):
        return None

    target_key = _scheduler_key_for_db(db, backup_dir=backup_dir)
    with _SCHEDULER_REGISTRY_LOCK:
        if _GLOBAL_SCHEDULER is not None:
            if _GLOBAL_SCHEDULER_KEY == target_key:
                _GLOBAL_SCHEDULER.start()
                return _GLOBAL_SCHEDULER
            # Active DB/backup_dir changed (e.g. isolated test env switch): cleanly stop prior scheduler
            _GLOBAL_SCHEDULER.stop(timeout=2.0)
            _GLOBAL_SCHEDULER = None
            _GLOBAL_SCHEDULER_KEY = None

        scheduler = BackupScheduler(
            db_manager=db,
            backup_dir=backup_dir,
            check_interval_seconds=check_interval_seconds,
            min_backup_interval_seconds=min_backup_interval_seconds,
            startup_delay_seconds=startup_delay_seconds,
            clock=clock,
        )
        scheduler.start()
        _GLOBAL_SCHEDULER = scheduler
        _GLOBAL_SCHEDULER_KEY = target_key
        return scheduler


def stop_backup_scheduler(timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> bool:
    """Stop the active process-wide BackupScheduler cleanly with bounded shutdown timeout."""
    global _GLOBAL_SCHEDULER, _GLOBAL_SCHEDULER_KEY
    with _SCHEDULER_REGISTRY_LOCK:
        scheduler = _GLOBAL_SCHEDULER
        _GLOBAL_SCHEDULER = None
        _GLOBAL_SCHEDULER_KEY = None
    if scheduler is None:
        return True
    return scheduler.stop(timeout=timeout)
