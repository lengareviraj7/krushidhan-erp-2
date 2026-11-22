"""
Service Layer for Offline SQLite Database Backup, Integrity Checks, and Safe Restore Operations.

Uses SQLite's native online backup API (`sqlite3.Connection.backup`), enforces
PRAGMA integrity_check validation on every backup and restore candidate, and preserves
a recoverable pre-modification snapshot before any restoration.
"""
from __future__ import annotations

import contextlib
import errno
import gc
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union

from src.config import resolve_backup_dir
from src.db.connection import DatabaseManager, get_db_manager
from src.models.system import BackupLogEntry
from src.repositories.system_repository import SystemRepository

logger = logging.getLogger("agri_erp.backup")

# Process-wide registry of in-flight backup/restore/upload staging files so orphan
# cleanup can never touch an active operation's temporary files.
_ACTIVE_STAGING_LOCK = threading.Lock()
_ACTIVE_STAGING_PATHS: Set[Path] = set()

# Process-wide registry of actively pinned backup files (download / inspect / restore)
# so retention pruning can NEVER delete a file currently in use.
_PINNED_FILES_LOCK = threading.Lock()
_PINNED_BACKUP_FILES: Dict[Path, int] = {}

# Per-database single-flight backup execution locks (coalesces / serializes manual & scheduled backups)
_BACKUP_EXEC_LOCKS_GUARD = threading.Lock()
_BACKUP_EXEC_LOCKS: Dict[Path, threading.Lock] = {}

# Per-directory manifest write locks
_MANIFEST_LOCKS_GUARD = threading.Lock()
_MANIFEST_LOCKS: Dict[Path, threading.RLock] = {}


class InsufficientDiskSpaceError(RuntimeError):
    """Raised when the backup target volume lacks safe free space before starting a backup."""

    def __init__(
        self,
        message: str,
        *,
        required_bytes: int,
        available_bytes: int,
        target_dir: Path,
    ):
        super().__init__(message)
        self.required_bytes = int(required_bytes)
        self.available_bytes = int(available_bytes)
        self.target_dir = Path(target_dir)


class BackupInProgressError(RuntimeError):
    """Raised when a non-blocking backup attempt is skipped because another backup is running."""


def get_backup_execution_lock(db_path: Union[str, Path]) -> threading.Lock:
    """Return the process-wide execution lock for a given SQLite database path."""
    resolved = Path(db_path).resolve()
    with _BACKUP_EXEC_LOCKS_GUARD:
        lock = _BACKUP_EXEC_LOCKS.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _BACKUP_EXEC_LOCKS[resolved] = lock
        return lock


def _get_manifest_lock(backup_dir: Union[str, Path]) -> threading.RLock:
    resolved = Path(backup_dir).resolve()
    with _MANIFEST_LOCKS_GUARD:
        lock = _MANIFEST_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _MANIFEST_LOCKS[resolved] = lock
        return lock


def pin_backup_file(file_path: Union[str, Path]) -> Path:
    """Increment active pin count for a backup file (protecting it from retention deletion)."""
    resolved = Path(file_path).resolve()
    with _PINNED_FILES_LOCK:
        _PINNED_BACKUP_FILES[resolved] = _PINNED_BACKUP_FILES.get(resolved, 0) + 1
    return resolved


def unpin_backup_file(file_path: Union[str, Path]) -> None:
    """Decrement active pin count for a backup file."""
    try:
        resolved = Path(file_path).resolve()
    except OSError:
        resolved = Path(file_path)
    with _PINNED_FILES_LOCK:
        count = _PINNED_BACKUP_FILES.get(resolved, 0)
        if count <= 1:
            _PINNED_BACKUP_FILES.pop(resolved, None)
        else:
            _PINNED_BACKUP_FILES[resolved] = count - 1


def is_backup_file_pinned(file_path: Union[str, Path]) -> bool:
    """Return True if the backup file is actively being downloaded, inspected, or restored."""
    try:
        resolved = Path(file_path).resolve()
    except OSError:
        resolved = Path(file_path)
    with _PINNED_FILES_LOCK:
        return _PINNED_BACKUP_FILES.get(resolved, 0) > 0


@contextlib.contextmanager
def pinned_backup_file(file_path: Union[str, Path]) -> Iterator[Path]:
    """Context manager pinning a backup file against retention pruning while in active use."""
    resolved = pin_backup_file(file_path)
    try:
        yield resolved
    finally:
        unpin_backup_file(resolved)


@contextlib.contextmanager
def register_active_staging_path(staging_path: Union[str, Path]) -> Iterator[Path]:
    """Mark a staging file path as actively owned by an in-flight backup/restore/upload operation."""
    resolved = Path(staging_path).resolve()
    with _ACTIVE_STAGING_LOCK:
        _ACTIVE_STAGING_PATHS.add(resolved)
    try:
        yield Path(staging_path)
    finally:
        with _ACTIVE_STAGING_LOCK:
            _ACTIVE_STAGING_PATHS.discard(resolved)


def _is_active_staging_path(candidate: Path) -> bool:
    """Return True if candidate (or its base staging file before SQLite sidecar suffix) is active."""
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate

    base_candidates = [resolved]
    name = resolved.name
    for suffix in ("-wal", "-shm", "-journal"):
        if name.endswith(suffix):
            base_candidates.append(resolved.with_name(name[: -len(suffix)]))
            break

    with _ACTIVE_STAGING_LOCK:
        return any(c in _ACTIVE_STAGING_PATHS for c in base_candidates)


def _is_process_alive(pid: int) -> bool:
    """
    Cross-platform conservative check for whether a process ID is currently alive.
    Returns True whenever the process is alive or liveness cannot be disproved safely.
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True

    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            ERROR_ACCESS_DENIED = 5
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int

            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
            if not handle:
                last_err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
                # Access denied means the process exists under another privilege level
                return last_err == ERROR_ACCESS_DENIED
            try:
                exit_code = ctypes.c_uint32(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # Conservative fallback: assume alive if Windows API call fails unexpectedly
            return True

    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists, owned by another user
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True


class BackupService:
    """Business logic for local SQLite database backups, automated timestamping, and safe restoration."""

    _DEFAULT_FILE_OP_RETRIES = 6
    _DEFAULT_FILE_OP_DELAY_SECONDS = 0.15
    ONLINE_BACKUP_PAGES_PER_STEP = 100
    ONLINE_BACKUP_SLEEP_SECONDS = 0.01
    DISK_SPACE_MULTIPLIER = 2.5
    DISK_SPACE_MARGIN_BYTES = 100 * 1024 * 1024  # 100 MB safety margin
    DEFAULT_ORPHAN_MIN_AGE_SECONDS = 900.0  # 15 minutes conservative safety threshold
    MANIFEST_FILENAME = ".backup_manifest.json"
    MANIFEST_VERSION = 1

    # Conservative GFS retention limits
    RETENTION_INTRADAY_KEEP = 6
    RETENTION_INTRADAY_WINDOW_SECONDS = 24 * 3600
    RETENTION_DAILY_KEEP = 14
    RETENTION_WEEKLY_KEEP = 8
    RETENTION_MANUAL_KEEP = 10
    RETENTION_BEFORE_UPGRADE_KEEP = 10
    RETENTION_BEFORE_RESTORE_KEEP = 10
    RETENTION_MIN_VERIFIED_FLOOR = 2

    AUTO_BACKUP_TYPES = frozenset({"AUTO_DAILY", "AUTO_SCHEDULED", "AUTO_WEEKLY"})
    PROTECTED_BACKUP_TYPES = frozenset({"MANUAL", "BEFORE_UPGRADE", "BEFORE_RESTORE"})
    KNOWN_POLICY_TYPES = AUTO_BACKUP_TYPES | PROTECTED_BACKUP_TYPES

    SQLITE_HEADER_MAGIC = b"SQLite format 3\x00"
    REQUIRED_ERP_TABLES = (
        "products",
        "customers",
        "suppliers",
        "sales",
        "users",
        "company_settings",
        "schema_version",
    )
    MAX_BACKUP_UPLOAD_BYTES = 250 * 1024 * 1024  # 250 MB

    # Exact temporary staging file patterns created by Krushidhan ERP backup/restore/upload workflows.
    # Finalized .db files, .corrupt_raw.bak forensic copies, and .backup_manifest.json NEVER match.
    _ORPHAN_BACKUP_STAGING_RE = re.compile(
        r"^\.agri_erp_(?:backup|before_upgrade|before_restore|auto_daily|auto_scheduled|auto_weekly)_[0-9_]+\.db\.(\d+)\.tmp(?:-wal|-shm|-journal)?$"
    )
    _ORPHAN_UPLOAD_STAGING_RE = re.compile(
        r"^\.upload_inspect_[A-Za-z0-9_-]+\.db\.tmp(?:-wal|-shm|-journal)?$"
    )
    _ORPHAN_RESTORE_STAGING_RE = re.compile(
        r"^\..+\.db\.(?:restore|rollback)\.(\d+)\.tmp(?:-wal|-shm|-journal)?$"
    )

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        backup_dir: Optional[Union[str, Path]] = None,
    ):
        self.db = db_manager or get_db_manager()
        self.system_repo = SystemRepository(self.db)
        self._configured_backup_dir = Path(backup_dir) if backup_dir is not None else None
        self.last_pre_restore_backup: Optional[Path] = None
        self.last_backup_sha256: Optional[str] = None
        self.last_backup_metadata: Optional[Dict[str, Any]] = None

    def get_backup_dir(self, backup_dir: Optional[Union[str, Path]] = None) -> Path:
        """
        Resolve target backup directory respecting:
        1. Method argument backup_dir
        2. Constructor backup_dir
        3. BACKUP_DIR environment variable
        4. Mode-specific directory (%LOCALAPPDATA%\\com.krushidhan.erp\\backups in desktop mode)
        """
        if backup_dir is not None:
            return Path(backup_dir)
        if self._configured_backup_dir is not None:
            return self._configured_backup_dir
        env_backup_dir = os.environ.get("BACKUP_DIR")
        if env_backup_dir and env_backup_dir.strip():
            return Path(env_backup_dir.strip())
        if getattr(self.db, "backup_dir", None) is not None:
            return Path(self.db.backup_dir)
        return resolve_backup_dir(app_mode=getattr(self.db, "app_mode", None))

    @staticmethod
    def _unique_backup_path(
        target_dir: Path,
        backup_type: str,
        *,
        timestamp_dt: Optional[datetime] = None,
    ) -> Path:
        """Generate a unique timestamped filename so existing backups are never overwritten."""
        normalized_type = (backup_type or "MANUAL").strip().upper()
        if normalized_type == "BEFORE_UPGRADE":
            prefix = "agri_erp_before_upgrade"
        elif normalized_type == "BEFORE_RESTORE":
            prefix = "agri_erp_before_restore"
        elif normalized_type == "AUTO_DAILY":
            prefix = "agri_erp_auto_daily"
        elif normalized_type == "AUTO_SCHEDULED":
            prefix = "agri_erp_auto_scheduled"
        elif normalized_type == "AUTO_WEEKLY":
            prefix = "agri_erp_auto_weekly"
        else:
            prefix = "agri_erp_backup"

        ts_dt = timestamp_dt if timestamp_dt is not None else datetime.now()
        timestamp = ts_dt.strftime("%Y%m%d_%H%M%S")
        candidate = target_dir / f"{prefix}_{timestamp}.db"
        counter = 1
        while candidate.exists():
            candidate = target_dir / f"{prefix}_{timestamp}_{counter}.db"
            counter += 1
        return candidate

    @classmethod
    def _sqlite_online_backup(
        cls,
        src_conn: sqlite3.Connection,
        dst_path: Path,
        *,
        pages: Optional[int] = None,
        sleep: Optional[float] = None,
    ) -> None:
        """
        Copy an open SQLite database cleanly in chunks using SQLite's online backup API.
        Chunked stepping (default pages=100, sleep=0.01) captures committed WAL frames
        without locking out concurrent POS reads/writes or altering the live DB journal mode.
        """
        step_pages = cls.ONLINE_BACKUP_PAGES_PER_STEP if pages is None else int(pages)
        step_sleep = cls.ONLINE_BACKUP_SLEEP_SECONDS if sleep is None else float(sleep)
        dst_conn: Optional[sqlite3.Connection] = None
        try:
            dst_conn = sqlite3.connect(str(dst_path), timeout=30.0)
            dst_conn.execute("PRAGMA busy_timeout = 10000;")
            src_conn.backup(dst_conn, pages=step_pages, sleep=step_sleep)
            dst_conn.commit()
        finally:
            if dst_conn is not None:
                try:
                    dst_conn.close()
                except Exception:
                    pass

    @staticmethod
    def compute_file_sha256(file_path: Union[str, Path]) -> str:
        """Compute lowercase SHA-256 hex digest of a file using streaming 64 KB chunks."""
        digest = hashlib.sha256()
        with open(Path(file_path), "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _fsync_file(file_path: Path) -> None:
        """Flush a staged file's data and metadata to persistent storage where supported."""
        try:
            with open(file_path, "r+b") as fh:
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            if exc.errno in (errno.EINVAL, errno.ENOSYS, errno.EBADF, errno.EPERM):
                logger.debug("Skipping unsupported os.fsync on file '%s': %s", file_path, exc)
                return
            raise

    @staticmethod
    def _fsync_directory(dir_path: Path) -> None:
        """Flush directory metadata after atomic rename on POSIX; safe no-op on Windows."""
        if os.name == "nt":
            return
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        fd: Optional[int] = None
        try:
            fd = os.open(str(dir_path), flags)
            os.fsync(fd)
        except OSError as exc:
            if exc.errno in (errno.EINVAL, errno.ENOSYS, errno.EBADF, errno.EPERM, errno.EACCES):
                logger.debug("Skipping unsupported directory os.fsync on '%s': %s", dir_path, exc)
                return
            raise
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def estimate_required_backup_space(self, live_db: Optional[Path] = None) -> Tuple[int, int, int]:
        """
        Estimate required free bytes before creating a backup:
            (db_size + wal_size) * 2.5 + 100 MB
        Returns (required_bytes, db_size_bytes, wal_size_bytes).
        """
        db_file = live_db if live_db is not None else Path(self.db.db_path)
        db_size = int(db_file.stat().st_size) if db_file.exists() else 0
        wal_file = Path(f"{db_file}-wal")
        wal_size = int(wal_file.stat().st_size) if wal_file.exists() else 0
        required = int(math.ceil((db_size + wal_size) * self.DISK_SPACE_MULTIPLIER)) + self.DISK_SPACE_MARGIN_BYTES
        return required, db_size, wal_size

    def check_disk_space_for_backup(
        self,
        target_dir: Path,
        live_db: Optional[Path] = None,
    ) -> Tuple[int, int]:
        """
        Verify target_dir volume has enough free disk space before starting a backup.
        Returns (available_bytes, required_bytes) or raises InsufficientDiskSpaceError.
        """
        required_bytes, db_size, wal_size = self.estimate_required_backup_space(live_db=live_db)
        usage = shutil.disk_usage(str(target_dir))
        available_bytes = int(usage.free)
        if available_bytes < required_bytes:
            req_display = self._format_size_display(required_bytes)
            avail_display = self._format_size_display(available_bytes)
            src_display = self._format_size_display(db_size + wal_size)
            raise InsufficientDiskSpaceError(
                f"Insufficient disk space to create backup safely: need at least {req_display} free "
                f"(database + WAL: {src_display}), but only {avail_display} is available in '{target_dir}'. "
                f"Free up disk space and retry. Existing backups were not modified.",
                required_bytes=required_bytes,
                available_bytes=available_bytes,
                target_dir=target_dir,
            )
        return available_bytes, required_bytes

    @classmethod
    def _replace_with_retry(
        cls,
        src: Path,
        dst: Path,
        retries: Optional[int] = None,
        delay_seconds: Optional[float] = None,
    ) -> None:
        """
        Atomically replace `dst` with `src`, retrying briefly on PermissionError
        (common on Windows when antivirus/indexers/file handles release briefly after close).
        """
        max_retries = cls._DEFAULT_FILE_OP_RETRIES if retries is None else max(1, int(retries))
        base_delay = cls._DEFAULT_FILE_OP_DELAY_SECONDS if delay_seconds is None else max(0.0, float(delay_seconds))
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                os.replace(str(src), str(dst))
                return
            except PermissionError as exc:
                last_exc = exc
                if attempt == max_retries - 1:
                    break
                gc.collect()
                if base_delay > 0:
                    time.sleep(base_delay * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    @classmethod
    def _unlink_with_retry(
        cls,
        target: Path,
        retries: Optional[int] = None,
        delay_seconds: Optional[float] = None,
        ignore_missing: bool = True,
    ) -> None:
        """Delete a file with bounded PermissionError retries for Windows handle cleanup."""
        if ignore_missing and not target.exists():
            return
        max_retries = cls._DEFAULT_FILE_OP_RETRIES if retries is None else max(1, int(retries))
        base_delay = cls._DEFAULT_FILE_OP_DELAY_SECONDS if delay_seconds is None else max(0.0, float(delay_seconds))
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                target.unlink()
                return
            except FileNotFoundError:
                if ignore_missing:
                    return
                raise
            except PermissionError as exc:
                last_exc = exc
                if attempt == max_retries - 1:
                    break
                gc.collect()
                if base_delay > 0:
                    time.sleep(base_delay * (attempt + 1))
        if last_exc is not None:
            raise last_exc

    @classmethod
    def _remove_sidecars(
        cls,
        db_path: Path,
        strict: bool = False,
        retries: Optional[int] = None,
        delay_seconds: Optional[float] = None,
    ) -> None:
        """Remove SQLite WAL/SHM/journal sidecar files for a database path."""
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{db_path}{suffix}")
            if not sidecar.exists():
                continue
            try:
                cls._unlink_with_retry(
                    sidecar,
                    retries=retries,
                    delay_seconds=delay_seconds,
                    ignore_missing=True,
                )
            except OSError:
                if strict:
                    raise

    @staticmethod
    def _switch_to_delete_journal_mode(db_path: Path, *, strict: bool = False) -> bool:
        """
        Checkpoint WAL frames cleanly and switch SQLite to DELETE journal mode before closing.
        When strict=True, raises RuntimeError if WAL checkpointing is busy/incomplete or
        if journal_mode fails to transition to DELETE.
        Returns True when checkpoint and DELETE transition completed cleanly.
        """
        if not db_path.exists():
            return False
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            cur = conn.cursor()
            cur.execute("PRAGMA busy_timeout = 10000;")
            cur.execute("PRAGMA journal_mode;")
            mode_row = cur.fetchone()
            initial_mode = str(mode_row[0]).strip().lower() if mode_row and mode_row[0] is not None else ""

            if initial_mode == "wal":
                cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                ckpt_row = cur.fetchone()
                if ckpt_row is None or len(ckpt_row) < 3:
                    if strict:
                        raise RuntimeError("Staging WAL checkpoint returned no status tuple.")
                    return False
                busy = int(ckpt_row[0])
                log_frames = int(ckpt_row[1])
                checkpointed_frames = int(ckpt_row[2])
                if busy != 0 or log_frames < 0 or checkpointed_frames < 0 or checkpointed_frames < log_frames:
                    msg = (
                        f"Staging WAL checkpoint incomplete for '{db_path.name}': "
                        f"busy={busy}, log={log_frames}, checkpointed={checkpointed_frames}"
                    )
                    if strict:
                        raise RuntimeError(msg)
                    logger.warning("%s", msg)
                    return False

            cur.execute("PRAGMA journal_mode = DELETE;")
            after_row = cur.fetchone()
            final_mode = str(after_row[0]).strip().lower() if after_row and after_row[0] is not None else ""
            cur.close()
            conn.commit()

            if final_mode != "delete":
                msg = f"Expected DELETE journal mode for '{db_path.name}', got '{final_mode}'."
                if strict:
                    raise RuntimeError(msg)
                logger.warning("%s", msg)
                return False
            return True
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def _existing_sidecars(db_path: Path) -> List[Path]:
        """Return any existing SQLite WAL/SHM/journal sidecar paths for db_path."""
        found: List[Path] = []
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{db_path}{suffix}")
            if sidecar.exists():
                found.append(sidecar)
        return found

    def _record_backup_log_best_effort(
        self,
        *,
        file_path: Path,
        backup_type: str,
        size_bytes: int,
        status: str,
    ) -> None:
        """Record a backup outcome in backup_log without masking the underlying backup failure."""
        try:
            self.system_repo.record_backup(
                BackupLogEntry(
                    file_path=str(file_path),
                    backup_type=backup_type,
                    backup_size_bytes=max(0, int(size_bytes)),
                    status=status,
                )
            )
        except Exception as exc:
            logger.debug("Could not write backup_log row (%s): %s", status, exc)

    # ------------------------------------------------------------------
    # Lightweight Live-DB Change Fingerprint & Atomic Backup Manifest
    # ------------------------------------------------------------------
    def compute_live_db_fingerprint(self) -> Optional[str]:
        """
        Compute a lightweight O(1) change fingerprint of the live SQLite database + WAL
        without scanning database tables or full file bytes:
        - Main DB: file size, mtime_ns, SQLite header change counter (24..28) & schema cookie (40..44)
        - WAL file (if present & non-empty): WAL size, mtime_ns, 32-byte WAL header & trailing 64 bytes
        - Read-only `sqlite_sequence` metadata (excluding `backup_log`): detects WAL-checkpointed
          inserts reliably across restarts even when SQLite WAL leaves header bytes 24..28 unchanged.
        Returns a stable SHA-256 hex digest string, or None for in-memory / missing DBs.
        """
        if self.db.db_path == ":memory:":
            return None
        live_db = Path(self.db.db_path)
        if not live_db.exists() or not live_db.is_file():
            return None

        h = hashlib.sha256()
        try:
            db_st = live_db.stat()
            h.update(f"db_size={db_st.st_size};db_mtime_ns={db_st.st_mtime_ns};".encode("ascii"))
            with open(live_db, "rb") as fh:
                hdr = fh.read(100)
            # Bytes 24..28 = file change counter, 40..44 = schema cookie
            h.update(hdr[24:28] if len(hdr) >= 28 else b"")
            h.update(hdr[40:44] if len(hdr) >= 44 else b"")

            wal_file = Path(f"{live_db}-wal")
            if wal_file.exists() and wal_file.is_file():
                wal_st = wal_file.stat()
                wal_size = int(wal_st.st_size)
                h.update(f";wal_size={wal_size};wal_mtime_ns={wal_st.st_mtime_ns};".encode("ascii"))
                if wal_size > 0:
                    with open(wal_file, "rb") as wfh:
                        wal_head = wfh.read(min(32, wal_size))
                        h.update(wal_head)
                        if wal_size > 32:
                            tail_len = min(64, wal_size)
                            wfh.seek(wal_size - tail_len)
                            h.update(wfh.read(tail_len))
            else:
                h.update(b";wal_size=0;wal_mtime_ns=0;")

            # Lightweight O(1) read-only sequence snapshot (never modifies DB/WAL or scans data tables)
            ro_conn: Optional[sqlite3.Connection] = None
            try:
                ro_conn = sqlite3.connect(f"{live_db.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
                seq_rows = ro_conn.execute(
                    "SELECT name, seq FROM sqlite_sequence WHERE name != 'backup_log' ORDER BY name;"
                ).fetchall()
                h.update(b";seq=")
                for row in seq_rows:
                    h.update(f"{row[0]}:{row[1]},".encode("utf-8"))
            except sqlite3.Error:
                pass
            finally:
                if ro_conn is not None:
                    try:
                        ro_conn.close()
                    except Exception:
                        pass

            return h.hexdigest()
        except OSError as exc:
            logger.debug("Unable to compute live DB change fingerprint: %s", exc)
            return None

    @classmethod
    def _manifest_path(cls, target_dir: Path) -> Path:
        return target_dir / cls.MANIFEST_FILENAME

    def load_manifest(self, backup_dir: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
        """Load the backup directory's metadata manifest, returning an empty structure if missing/corrupt."""
        target_dir = self.get_backup_dir(backup_dir)
        mpath = self._manifest_path(target_dir)
        empty: Dict[str, Any] = {
            "manifest_version": self.MANIFEST_VERSION,
            "updated_at": None,
            "last_verified_source_fingerprint": None,
            "last_verified_write_generation": None,
            "last_verified_backup_filename": None,
            "entries": {},
        }
        if not mpath.exists() or not mpath.is_file():
            return empty
        try:
            raw = mpath.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return empty
            entries = parsed.get("entries")
            if not isinstance(entries, dict):
                entries = {}
            return {
                "manifest_version": int(parsed.get("manifest_version") or self.MANIFEST_VERSION),
                "updated_at": parsed.get("updated_at"),
                "last_verified_source_fingerprint": parsed.get("last_verified_source_fingerprint"),
                "last_verified_write_generation": parsed.get("last_verified_write_generation"),
                "last_verified_backup_filename": parsed.get("last_verified_backup_filename"),
                "entries": entries,
            }
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Backup manifest at '%s' unreadable (%s); will rebuild.", mpath, exc)
            return empty

    def _save_manifest_atomic(self, target_dir: Path, manifest: Dict[str, Any]) -> None:
        """Atomically persist .backup_manifest.json via temp file + fsync + replace + dir fsync."""
        target_dir.mkdir(parents=True, exist_ok=True)
        mpath = self._manifest_path(target_dir)
        payload = {
            "manifest_version": self.MANIFEST_VERSION,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "last_verified_source_fingerprint": manifest.get("last_verified_source_fingerprint"),
            "last_verified_write_generation": manifest.get("last_verified_write_generation"),
            "last_verified_backup_filename": manifest.get("last_verified_backup_filename"),
            "entries": manifest.get("entries") or {},
        }
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        tmp_path = target_dir / f".backup_manifest.{os.getpid()}.{threading.get_ident()}.tmp"
        lock = _get_manifest_lock(target_dir)
        with lock:
            try:
                tmp_path.write_text(serialized, encoding="utf-8")
                self._fsync_file(tmp_path)
                self._replace_with_retry(tmp_path, mpath)
                self._fsync_directory(target_dir)
            except Exception as exc:
                logger.warning("Failed saving backup manifest atomically at '%s': %s", mpath, exc)
                try:
                    self._unlink_with_retry(tmp_path, ignore_missing=True)
                except OSError:
                    pass

    @staticmethod
    def _manifest_entry_matches_stat(entry: Any, size_bytes: int, mtime_ns: int) -> bool:
        if not isinstance(entry, dict):
            return False
        required_keys = (
            "filename",
            "backup_type",
            "size_bytes",
            "mtime_ns",
            "mtime_epoch",
            "modified_at",
            "created_at_iso",
            "sha256",
            "sqlite_header_valid",
            "integrity_ok",
            "foreign_key_ok",
            "has_required_tables",
            "valid_for_restore",
            "table_count",
        )
        if any(k not in entry for k in required_keys):
            return False
        try:
            return int(entry["size_bytes"]) == int(size_bytes) and int(entry["mtime_ns"]) == int(mtime_ns)
        except (TypeError, ValueError):
            return False

    def create_backup(
        self,
        backup_dir: Optional[Union[str, Path]] = None,
        backup_type: str = "MANUAL",
        *,
        blocking: bool = True,
        timestamp_dt: Optional[datetime] = None,
        managed_by_policy: bool = True,
    ) -> Path:
        """
        Creates a consistent, fully verified snapshot backup using SQLite's chunked online
        backup API (`Connection.backup`):
        0. Acquires single-flight per-DB execution lock (non-blocking for scheduler, serialized for manual)
           and pre-checks free disk space ((db + wal) * 2.5 + 100 MB) without touching older backups.
        1. Streams a consistent snapshot to a temporary staging file (leaving live DB in WAL mode).
        2. Converts staging file to self-contained DELETE journal mode & verifies checkpointing.
        3. Confirms zero WAL/SHM/journal sidecars remain on staging file.
        4. Verifies PRAGMA integrity_check, PRAGMA foreign_key_check, required ERP tables & schema_version.
        5. Computes SHA-256 checksum of the verified staging file.
        6. Flushes staged file with os.fsync(), atomically replaces final backup path, and flushes dir.
        7. Logs SUCCESS in backup_log only after every verification step passes, then atomically updates
           the metadata manifest with post-backup change fingerprint.
        """
        if self.db.db_path == ":memory:":
            raise ValueError("Cannot backup an in-memory database to file!")

        live_db = Path(self.db.db_path)
        if not live_db.exists():
            raise FileNotFoundError(f"Source database does not exist: {live_db}")

        target_dir = self.get_backup_dir(backup_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        exec_lock = get_backup_execution_lock(live_db)
        acquired = exec_lock.acquire(blocking=bool(blocking))
        if not acquired:
            raise BackupInProgressError("Another backup operation is already in progress.")

        try:
            return self._create_backup_locked(
                live_db=live_db,
                target_dir=target_dir,
                backup_type=backup_type,
                timestamp_dt=timestamp_dt,
                managed_by_policy=managed_by_policy,
            )
        finally:
            exec_lock.release()

    def _create_backup_locked(
        self,
        *,
        live_db: Path,
        target_dir: Path,
        backup_type: str,
        timestamp_dt: Optional[datetime],
        managed_by_policy: bool,
    ) -> Path:
        backup_file = self._unique_backup_path(
            target_dir,
            backup_type=backup_type,
            timestamp_dt=timestamp_dt,
        )
        self.last_backup_sha256 = None
        self.last_backup_metadata = None

        # 0. Pre-flight disk space protection BEFORE creating staging files or starting copy
        try:
            self.check_disk_space_for_backup(target_dir=target_dir, live_db=live_db)
        except InsufficientDiskSpaceError as exc:
            logger.warning("Backup aborted due to insufficient disk space: %s", exc)
            self._record_backup_log_best_effort(
                file_path=backup_file,
                backup_type=backup_type,
                size_bytes=0,
                status="FAILED_LOW_DISK",
            )
            raise

        staging_file = target_dir / f".{backup_file.name}.{os.getpid()}.tmp"
        self._remove_sidecars(staging_file, strict=False)
        self._unlink_with_retry(staging_file, ignore_missing=True)

        failure_status = "FAILED_BACKUP"
        source_tables: Set[str] = set()
        normalized_bkp_type = str(backup_type or "MANUAL").strip().upper()
        with register_active_staging_path(staging_file):
            try:
                # 1. Chunked SQLite online backup (live DB stays in WAL mode and usable)
                src_conn: Optional[sqlite3.Connection] = None
                try:
                    src_conn = self.db.get_connection()
                    source_tables = {
                        str(r[0])
                        for r in src_conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
                        ).fetchall()
                    }
                    self._sqlite_online_backup(src_conn, staging_file)
                finally:
                    if src_conn is not None:
                        try:
                            src_conn.close()
                        except Exception:
                            pass

                if not staging_file.exists() or staging_file.stat().st_size <= 0:
                    failure_status = "FAILED_EMPTY"
                    raise ValueError("Backup creation failed: resulting backup file is empty.")

                # 2–3. Convert staged copy to self-contained DELETE mode & verify checkpoint completed
                failure_status = "FAILED_CHECKPOINT"
                self._switch_to_delete_journal_mode(staging_file, strict=True)

                # 4. Ensure zero WAL/SHM/journal sidecars remain on the staging copy
                self._remove_sidecars(staging_file, strict=True)
                leftover_sidecars = self._existing_sidecars(staging_file)
                if leftover_sidecars:
                    names = ", ".join(p.name for p in leftover_sidecars)
                    raise RuntimeError(
                        f"Backup staging verification failed: unexpected sidecar files remain ({names})."
                    )

                # 5–7. Full verification: integrity_check, foreign_key_check, required tables, schema_version
                inspection = self.inspect_backup(staging_file)
                if not inspection["integrity_ok"]:
                    failure_status = "FAILED_INTEGRITY"
                    raise ValueError(
                        "Backup verification failed: PRAGMA integrity_check did not return 'ok' "
                        f"({inspection['integrity_results']})."
                    )
                if not inspection["foreign_key_ok"]:
                    failure_status = "FAILED_FOREIGN_KEYS"
                    raise ValueError(
                        "Backup verification failed: PRAGMA foreign_key_check reported "
                        f"{len(inspection['foreign_key_violations'])} violation(s)."
                    )
                staged_tables = set(inspection.get("tables") or [])
                missing_from_source = sorted(source_tables - staged_tables)
                if missing_from_source:
                    failure_status = "FAILED_SCHEMA"
                    raise ValueError(
                        f"Backup verification failed: missing source tables ({', '.join(missing_from_source)})."
                    )
                if normalized_bkp_type == "BEFORE_UPGRADE":
                    required_for_upgrade = [t for t in self.REQUIRED_ERP_TABLES if t in source_tables]
                    if "schema_version" not in required_for_upgrade:
                        required_for_upgrade.append("schema_version")
                    missing_req = [t for t in required_for_upgrade if t not in staged_tables]
                    if missing_req:
                        failure_status = "FAILED_SCHEMA"
                        raise ValueError(
                            f"Backup verification failed: missing required ERP tables ({', '.join(missing_req)})."
                        )
                elif not inspection["has_required_tables"]:
                    failure_status = "FAILED_SCHEMA"
                    missing = ", ".join(inspection["missing_required_tables"])
                    raise ValueError(
                        f"Backup verification failed: missing required ERP tables ({missing})."
                    )
                schema_ver = inspection.get("schema_version")
                if schema_ver is None or int(schema_ver) < 1:
                    failure_status = "FAILED_SCHEMA"
                    raise ValueError(
                        f"Backup verification failed: invalid or missing schema_version ({schema_ver!r})."
                    )

                # 8. Calculate SHA-256 of verified staging copy
                failure_status = "FAILED_IO"
                sha256_hex = self.compute_file_sha256(staging_file)

                # 9. Flush staged backup file to disk before atomic rename
                self._fsync_file(staging_file)

                # 10. Atomically promote staged file to final backup path
                self._replace_with_retry(staging_file, backup_file)

                # Confirm zero sidecars on finalized backup path
                final_sidecars = self._existing_sidecars(backup_file)
                if final_sidecars:
                    self._remove_sidecars(backup_file, strict=False)
                    if self._existing_sidecars(backup_file):
                        raise RuntimeError(
                            f"Unexpected SQLite sidecars detected alongside finalized backup '{backup_file.name}'."
                        )

                # 11. Flush backup directory metadata where supported (safe no-op on Windows)
                self._fsync_directory(target_dir)

                if timestamp_dt is not None:
                    ts_epoch = float(timestamp_dt.timestamp())
                    try:
                        os.utime(backup_file, (ts_epoch, ts_epoch))
                    except OSError:
                        pass

                st_final = backup_file.stat()
                size_bytes = int(st_final.st_size)
                mtime_ns = int(st_final.st_mtime_ns)
                mtime_epoch = float(st_final.st_mtime)
                dt_final = datetime.fromtimestamp(mtime_epoch)
                self.last_backup_sha256 = sha256_hex
                self.last_backup_metadata = {
                    "file_path": str(backup_file),
                    "filename": backup_file.name,
                    "backup_type": normalized_bkp_type,
                    "managed_by_policy": bool(managed_by_policy),
                    "size_bytes": size_bytes,
                    "mtime_ns": mtime_ns,
                    "mtime_epoch": mtime_epoch,
                    "modified_at": dt_final.strftime("%Y-%m-%d %H:%M:%S"),
                    "created_at_iso": dt_final.isoformat(timespec="seconds"),
                    "sha256": sha256_hex,
                    "schema_version": int(schema_ver),
                    "table_count": int(inspection["table_count"]),
                    "shop_name": inspection.get("shop_name"),
                    "sqlite_header_valid": True,
                    "integrity_ok": True,
                    "foreign_key_ok": True,
                    "has_required_tables": bool(inspection["has_required_tables"]),
                    "valid_for_restore": bool(inspection["valid_for_restore"]),
                }
            except Exception as exc:
                logger.error("Backup creation failed (%s): %s", failure_status, exc)
                # Clean up ONLY this operation's incomplete staging files; preserve all existing valid backups
                try:
                    self._remove_sidecars(staging_file, strict=False)
                    self._unlink_with_retry(staging_file, ignore_missing=True)
                except OSError:
                    pass
                self._record_backup_log_best_effort(
                    file_path=backup_file,
                    backup_type=backup_type,
                    size_bytes=0,
                    status=failure_status,
                )
                raise

        self.system_repo.record_backup(
            BackupLogEntry(
                file_path=str(backup_file),
                backup_type=normalized_bkp_type,
                backup_size_bytes=size_bytes,
                status="SUCCESS",
            )
        )

        # Compute post-backup source fingerprint AFTER record_backup() so the backup_log
        # insert itself is not misinterpreted as an uncommitted business change later.
        post_fingerprint = self.compute_live_db_fingerprint()
        post_write_gen = self.db.write_generation
        try:
            lock = _get_manifest_lock(target_dir)
            with lock:
                manifest = self.load_manifest(target_dir)
                entries = dict(manifest.get("entries") or {})
                meta = dict(self.last_backup_metadata or {})
                meta["source_fingerprint"] = post_fingerprint
                entries[backup_file.name] = meta
                manifest["entries"] = entries
                manifest["last_verified_source_fingerprint"] = post_fingerprint
                manifest["last_verified_write_generation"] = post_write_gen
                manifest["last_verified_backup_filename"] = backup_file.name
                self._save_manifest_atomic(target_dir, manifest)
        except Exception as exc:
            logger.debug("Non-fatal manifest update warning after backup: %s", exc)

        return backup_file

    @staticmethod
    def _format_size_display(size_bytes: int) -> str:
        size = max(0, int(size_bytes or 0))
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.2f} MB"

    @classmethod
    def has_sqlite_header(cls, file_path: Union[str, Path]) -> bool:
        """Verify file exists, is at least 100 bytes, and starts with SQLite 3 magic header."""
        target = Path(file_path)
        try:
            if not target.is_file() or target.stat().st_size < 100:
                return False
            with open(target, "rb") as fh:
                return fh.read(len(cls.SQLITE_HEADER_MAGIC)) == cls.SQLITE_HEADER_MAGIC
        except OSError:
            return False

    @staticmethod
    def _infer_backup_type(filename: str) -> str:
        lower = filename.lower()
        if lower.startswith("agri_erp_before_restore"):
            return "BEFORE_RESTORE"
        if lower.startswith("agri_erp_before_upgrade"):
            return "BEFORE_UPGRADE"
        if lower.startswith("agri_erp_auto_daily"):
            return "AUTO_DAILY"
        if lower.startswith("agri_erp_auto_scheduled"):
            return "AUTO_SCHEDULED"
        if lower.startswith("agri_erp_auto_weekly"):
            return "AUTO_WEEKLY"
        if lower.startswith("agri_erp_backup"):
            return "MANUAL"
        return "EXTERNAL"

    def resolve_safe_backup_path(
        self,
        filename: str,
        backup_dir: Optional[Union[str, Path]] = None,
    ) -> Path:
        """
        Safely resolve a backup filename inside BACKUP_DIR while rejecting traversal,
        subdirectories, hidden temp files, or non-.db filenames.
        """
        raw = str(filename or "").strip()
        if not raw:
            raise ValueError("Backup filename is required.")
        if any(sep in raw for sep in ("/", "\\", "\x00")) or ".." in raw or raw.startswith("."):
            raise ValueError("Invalid backup filename: path separators and hidden names are not allowed.")
        if Path(raw).name != raw or not raw.lower().endswith(".db"):
            raise ValueError("Backup filename must be a plain .db file inside the backup folder.")

        target_dir = self.get_backup_dir(backup_dir).resolve()
        candidate = (target_dir / raw).resolve()
        if candidate.parent != target_dir:
            raise ValueError("Invalid backup filename: target escapes backup directory.")
        return candidate

    def inspect_backup(self, backup_file_path: Union[str, Path]) -> Dict[str, Any]:
        """Inspect a SQLite database/backup file for integrity, FK health, and ERP schema compatibility."""
        target = Path(backup_file_path)
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"Database/backup file does not exist: {target}")

        with pinned_backup_file(target):
            return self._inspect_backup_pinned(target)

    def _inspect_backup_pinned(self, target: Path) -> Dict[str, Any]:
        st = target.stat()
        size_bytes = int(st.st_size)
        modified_at = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        base_info: Dict[str, Any] = {
            "path": str(target),
            "filename": target.name,
            "size_bytes": size_bytes,
            "size_display": self._format_size_display(size_bytes),
            "modified_at": modified_at,
            "integrity_ok": False,
            "integrity_results": [],
            "foreign_key_ok": False,
            "foreign_key_violations": [],
            "table_count": 0,
            "tables": [],
            "schema_version": None,
            "shop_name": None,
            "has_required_tables": False,
            "missing_required_tables": list(self.REQUIRED_ERP_TABLES),
            "valid_for_restore": False,
        }

        if not self.has_sqlite_header(target):
            base_info["integrity_results"] = [
                "Not a valid SQLite database file (missing SQLite format 3 header)."
            ]
            return base_info

        conn: Optional[sqlite3.Connection] = None
        try:
            has_sidecar = Path(f"{target}-wal").exists() or Path(f"{target}-shm").exists()
            if not has_sidecar:
                conn = sqlite3.connect(f"{target.resolve().as_uri()}?immutable=1", uri=True, timeout=30.0)
            else:
                conn = sqlite3.connect(str(target), timeout=30.0)
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            integrity_rows = [str(r[0]) for r in cur.fetchall() if r]

            cur.execute("PRAGMA foreign_keys = ON;")
            cur.execute("PRAGMA foreign_key_check;")
            fk_violations = [tuple(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
            )
            tables = [str(r[0]) for r in cur.fetchall()]

            schema_version: Optional[int] = None
            if "schema_version" in tables:
                try:
                    cur.execute("SELECT MAX(version_number) FROM schema_version;")
                    sv_row = cur.fetchone()
                    if sv_row and sv_row[0] is not None:
                        schema_version = int(sv_row[0])
                except sqlite3.Error:
                    schema_version = None

            shop_name: Optional[str] = None
            if "company_settings" in tables:
                try:
                    cur.execute("SELECT company_name FROM company_settings WHERE setting_id = 1;")
                    cs_row = cur.fetchone()
                    if cs_row and cs_row[0]:
                        shop_name = str(cs_row[0]).strip()
                except sqlite3.Error:
                    shop_name = None

            cur.close()
            missing_required = [t for t in self.REQUIRED_ERP_TABLES if t not in tables]
            integrity_ok = integrity_rows == ["ok"]
            fk_ok = len(fk_violations) == 0
            has_required = len(missing_required) == 0

            base_info.update(
                {
                    "integrity_ok": integrity_ok,
                    "integrity_results": integrity_rows,
                    "foreign_key_ok": fk_ok,
                    "foreign_key_violations": fk_violations,
                    "table_count": len(tables),
                    "tables": tables,
                    "schema_version": schema_version,
                    "shop_name": shop_name,
                    "has_required_tables": has_required,
                    "missing_required_tables": missing_required,
                    "valid_for_restore": bool(integrity_ok and fk_ok and has_required),
                }
            )
            return base_info
        except sqlite3.Error as exc:
            base_info["integrity_results"] = [f"SQLite error: {exc}"]
            return base_info
        finally:
            if conn is not None:
                conn.close()

    def verify_database_integrity(
        self,
        db_file_path: Optional[Union[str, Path]] = None,
        check_foreign_keys: bool = False,
    ) -> bool:
        """Runs SQLite PRAGMA integrity_check (and optional foreign_key_check) on target DB."""
        target_path = db_file_path if db_file_path is not None else self.db.db_path
        if target_path == ":memory:":
            return True

        target = Path(target_path)
        if not self.has_sqlite_header(target):
            return False

        conn: Optional[sqlite3.Connection] = None
        try:
            has_sidecar = Path(f"{target}-wal").exists() or Path(f"{target}-shm").exists()
            if not has_sidecar:
                conn = sqlite3.connect(f"{target.resolve().as_uri()}?immutable=1", uri=True, timeout=30.0)
            else:
                conn = sqlite3.connect(str(target), timeout=30.0)
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check;")
            results = [str(r[0]) for r in cursor.fetchall() if r]
            if results != ["ok"]:
                cursor.close()
                return False

            if check_foreign_keys:
                cursor.execute("PRAGMA foreign_keys = ON;")
                cursor.execute("PRAGMA foreign_key_check;")
                if cursor.fetchall():
                    cursor.close()
                    return False

            cursor.close()
            return True
        except sqlite3.Error:
            return False
        finally:
            if conn is not None:
                conn.close()

    def _rollback_from_pre_restore(
        self,
        live_db: Path,
        retries: Optional[int] = None,
        delay_seconds: Optional[float] = None,
    ) -> None:
        """
        Restore live_db from preserved `self.last_pre_restore_backup` without modifying or removing
        the safety backup itself.
        """
        pre_restore = self.last_pre_restore_backup
        if pre_restore is None or not pre_restore.exists():
            return

        rollback_tmp: Optional[Path] = None
        try:
            try:
                self._switch_to_delete_journal_mode(live_db)
            except sqlite3.Error:
                pass
            gc.collect()
            self._remove_sidecars(live_db, strict=False, retries=retries, delay_seconds=delay_seconds)

            if pre_restore.suffix == ".db":
                rollback_tmp = live_db.parent / f".{live_db.name}.rollback.{os.getpid()}.tmp"
                with register_active_staging_path(rollback_tmp):
                    self._unlink_with_retry(rollback_tmp, retries=retries, delay_seconds=delay_seconds, ignore_missing=True)
                    shutil.copy2(str(pre_restore), str(rollback_tmp))
                    self._remove_sidecars(rollback_tmp, strict=False)
                    self._fsync_file(rollback_tmp)
                    self._replace_with_retry(rollback_tmp, live_db, retries=retries, delay_seconds=delay_seconds)
                    self._fsync_directory(live_db.parent)

                # Re-open cleanly in WAL mode if healthy
                conn: Optional[sqlite3.Connection] = None
                try:
                    conn = self.db.get_connection()
                finally:
                    if conn is not None:
                        conn.close()
            else:
                shutil.copy2(str(pre_restore), str(live_db))
                for suffix in ("-wal", "-shm"):
                    raw_sidecar = Path(f"{pre_restore}{suffix}")
                    if raw_sidecar.exists():
                        shutil.copy2(str(raw_sidecar), f"{live_db}{suffix}")
        except Exception:
            if rollback_tmp is not None and rollback_tmp.exists():
                try:
                    self._unlink_with_retry(rollback_tmp, retries=retries, delay_seconds=delay_seconds, ignore_missing=True)
                except OSError:
                    pass
            # Last-resort copy fallback while preserving pre_restore intact
            if pre_restore.suffix == ".db":
                try:
                    self._remove_sidecars(live_db, strict=False, retries=retries, delay_seconds=delay_seconds)
                    shutil.copy2(str(pre_restore), str(live_db))
                except Exception:
                    pass

    def restore_backup(
        self,
        backup_file_path: Union[str, Path],
        backup_dir: Optional[Union[str, Path]] = None,
        verify_foreign_keys: bool = True,
        maintenance_timeout: float = 10.0,
        file_retry_attempts: Optional[int] = None,
        file_retry_delay: Optional[float] = None,
    ) -> bool:
        """
        Safely restore a validated backup database under exclusive maintenance lock:
        1. Validates candidate backup via PRAGMA integrity_check (& schema/FK check).
        2. Acquires exclusive maintenance lock (drains active connections, blocks new ops).
        3. Preserves a recoverable BEFORE_RESTORE snapshot before modifying anything.
        4. Stages restoration via SQLite online backup API in DELETE journal mode.
        5. Checkpoints live WAL, switches live DB to DELETE mode, closes all handles,
           removes sidecars, and atomically replaces live DB with bounded PermissionError retries.
        6. Verifies integrity/FKs and re-enables WAL (auto-rolling back on any failure).
        """
        if self.db.db_path == ":memory:":
            raise ValueError("Cannot restore a file backup onto an in-memory database!")

        backup_path = Path(backup_file_path)
        if not backup_path.exists() or not backup_path.is_file():
            raise FileNotFoundError(f"Backup file does not exist: {backup_path}")

        live_db = Path(self.db.db_path)
        if backup_path.resolve() == live_db.resolve():
            raise ValueError("Cannot restore a live database file onto itself.")

        with pinned_backup_file(backup_path):
            return self._restore_backup_pinned(
                backup_path=backup_path,
                live_db=live_db,
                backup_dir=backup_dir,
                verify_foreign_keys=verify_foreign_keys,
                maintenance_timeout=maintenance_timeout,
                file_retry_attempts=file_retry_attempts,
                file_retry_delay=file_retry_delay,
            )

    def _restore_backup_pinned(
        self,
        *,
        backup_path: Path,
        live_db: Path,
        backup_dir: Optional[Union[str, Path]],
        verify_foreign_keys: bool,
        maintenance_timeout: float,
        file_retry_attempts: Optional[int],
        file_retry_delay: Optional[float],
    ) -> bool:
        # 1. Verify candidate backup file integrity & structure BEFORE blocking traffic (NEVER trust manifest)
        inspection = self.inspect_backup(backup_path)
        if not inspection["integrity_ok"]:
            raise ValueError(
                f"Backup file failed PRAGMA integrity_check: {inspection['integrity_results']}"
            )
        if verify_foreign_keys and not inspection["foreign_key_ok"]:
            raise ValueError("Backup file contains foreign key constraint violations.")
        if not inspection["has_required_tables"]:
            missing_list = ", ".join(inspection["missing_required_tables"])
            raise ValueError(
                f"Backup file is missing core ERP tables and cannot be restored ({missing_list})."
            )

        with self.db.exclusive_maintenance(timeout=maintenance_timeout, reason="backup_restore"):
            # 2. Preserve a recoverable pre-restore copy of the quiescent live database
            safety_dir = self.get_backup_dir(backup_dir)
            safety_dir.mkdir(parents=True, exist_ok=True)
            self.last_pre_restore_backup = None

            if live_db.exists() and live_db.stat().st_size > 0:
                try:
                    self.last_pre_restore_backup = self.create_backup(
                        backup_dir=safety_dir,
                        backup_type="BEFORE_RESTORE",
                    )
                except InsufficientDiskSpaceError:
                    raise
                except Exception:
                    # If the live DB is corrupted and cannot open cleanly in SQLite,
                    # still keep an exact raw forensic copy before overwriting it.
                    raw_copy = self._unique_backup_path(safety_dir, backup_type="BEFORE_RESTORE")
                    raw_copy = raw_copy.with_suffix(".corrupt_raw.bak")
                    shutil.copy2(str(live_db), str(raw_copy))
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(f"{live_db}{suffix}")
                        if sidecar.exists():
                            shutil.copy2(str(sidecar), f"{raw_copy}{suffix}")
                    self.last_pre_restore_backup = raw_copy

            # 3. Stage restore cleanly via SQLite online backup API into a temporary file
            live_db.parent.mkdir(parents=True, exist_ok=True)
            temp_restore = live_db.parent / f".{live_db.name}.restore.{os.getpid()}.tmp"
            self._remove_sidecars(temp_restore, strict=False, retries=file_retry_attempts, delay_seconds=file_retry_delay)
            self._unlink_with_retry(temp_restore, retries=file_retry_attempts, delay_seconds=file_retry_delay, ignore_missing=True)

            with register_active_staging_path(temp_restore):
                src_backup_conn: Optional[sqlite3.Connection] = None
                try:
                    src_backup_conn = sqlite3.connect(str(backup_path), timeout=30.0)
                    self._sqlite_online_backup(src_backup_conn, temp_restore)
                except Exception:
                    self._remove_sidecars(temp_restore, strict=False)
                    self._unlink_with_retry(temp_restore, ignore_missing=True)
                    raise
                finally:
                    if src_backup_conn is not None:
                        src_backup_conn.close()

                if not self.verify_database_integrity(temp_restore, check_foreign_keys=verify_foreign_keys):
                    self._remove_sidecars(temp_restore, strict=False)
                    self._unlink_with_retry(temp_restore, ignore_missing=True)
                    raise ValueError("Staged restore file failed post-copy integrity verification.")

                # Ensure temp_restore is in clean DELETE journal mode with zero WAL sidecars
                try:
                    self._switch_to_delete_journal_mode(temp_restore, strict=True)
                    self._remove_sidecars(
                        temp_restore,
                        strict=True,
                        retries=file_retry_attempts,
                        delay_seconds=file_retry_delay,
                    )
                    self._fsync_file(temp_restore)
                except Exception:
                    self._remove_sidecars(temp_restore, strict=False)
                    self._unlink_with_retry(temp_restore, ignore_missing=True)
                    raise

                # 4. Checkpoint live DB WAL, switch to DELETE journal mode, close handles & swap atomically
                live_swap_started = False
                try:
                    live_swap_started = True
                    if live_db.exists():
                        try:
                            self._switch_to_delete_journal_mode(live_db)
                        except sqlite3.Error:
                            pass

                    gc.collect()
                    self._remove_sidecars(
                        live_db,
                        strict=True,
                        retries=file_retry_attempts,
                        delay_seconds=file_retry_delay,
                    )

                    self._replace_with_retry(
                        temp_restore,
                        live_db,
                        retries=file_retry_attempts,
                        delay_seconds=file_retry_delay,
                    )
                    self._fsync_directory(live_db.parent)

                    # 5. Re-verify restored live DB & re-enable WAL pragmas
                    if not self.verify_database_integrity(live_db, check_foreign_keys=verify_foreign_keys):
                        raise RuntimeError("Post-restore integrity verification failed on live DB.")

                    verify_conn: Optional[sqlite3.Connection] = None
                    try:
                        verify_conn = self.db.get_connection()
                        cur = verify_conn.cursor()
                        cur.execute("PRAGMA journal_mode;")
                        cur.fetchone()
                        cur.close()
                    finally:
                        if verify_conn is not None:
                            verify_conn.close()

                    self.db._save_cloud_snapshot()
                    return True
                except Exception:
                    if temp_restore.exists():
                        try:
                            self._remove_sidecars(temp_restore, strict=False)
                            self._unlink_with_retry(
                                temp_restore,
                                retries=file_retry_attempts,
                                delay_seconds=file_retry_delay,
                                ignore_missing=True,
                            )
                        except OSError:
                            pass
                    if live_swap_started:
                        self._rollback_from_pre_restore(
                            live_db=live_db,
                            retries=file_retry_attempts,
                            delay_seconds=file_retry_delay,
                        )
                    raise

    def list_backups(self, backup_dir: Optional[Union[str, Path]] = None) -> List[Dict[str, Any]]:
        """
        Scan the real BACKUP_DIR on disk for `.db` backup files and enrich them with
        atomic manifest metadata and backup_log metadata without trusting stale log rows
        for deleted/missing files.

        Performance: Known unchanged files (matching size_bytes + mtime_ns in `.backup_manifest.json`)
        are served directly from the manifest without running repeated full SQLite integrity scans.
        Explicit `/inspect` and `restore_backup()` ALWAYS bypass the manifest and run live inspection.
        """
        target_dir = self.get_backup_dir(backup_dir)
        log_by_name: Dict[str, Dict[str, Any]] = {}
        try:
            rows = self.db.fetch_all(
                "SELECT backup_id, file_path, backup_type, backup_size_bytes, status, created_at "
                "FROM backup_log ORDER BY created_at DESC, backup_id DESC;"
            )
            for row in rows:
                row_dict = dict(row)
                raw_path = str(row_dict.get("file_path") or "").strip()
                if not raw_path:
                    continue
                name = Path(raw_path).name
                if name and name not in log_by_name:
                    log_by_name[name] = row_dict
        except sqlite3.Error:
            log_by_name = {}

        if not target_dir.exists() or not target_dir.is_dir():
            return []

        manifest_lock = _get_manifest_lock(target_dir)
        with manifest_lock:
            manifest = self.load_manifest(target_dir)
            cached_entries: Dict[str, Any] = dict(manifest.get("entries") or {})
            updated_entries: Dict[str, Dict[str, Any]] = {}
            manifest_dirty = False
            disk_filenames: Set[str] = set()
            backups: List[Dict[str, Any]] = []

            for entry in target_dir.iterdir():
                fname = entry.name
                if (
                    not entry.is_file()
                    or fname.startswith(".")
                    or not fname.lower().endswith(".db")
                    or ".restore." in fname
                ):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue

                disk_filenames.add(fname)
                size_bytes = int(st.st_size)
                mtime_ns = int(st.st_mtime_ns)
                mtime_epoch = float(st.st_mtime)
                dt = datetime.fromtimestamp(mtime_epoch)
                log_meta = log_by_name.get(fname)

                cached = cached_entries.get(fname)
                if self._manifest_entry_matches_stat(cached, size_bytes, mtime_ns):
                    meta = dict(cached)
                    bkp_type = (
                        str(meta.get("backup_type") or "").strip().upper()
                        or (str(log_meta.get("backup_type") or "").strip().upper() if log_meta else "")
                        or self._infer_backup_type(fname)
                    )
                    meta["backup_type"] = bkp_type
                    updated_entries[fname] = meta
                else:
                    # Cache miss, changed file, or missing/corrupt manifest -> inspect & index
                    bkp_type = (
                        str(log_meta.get("backup_type") or "").strip().upper()
                        if log_meta
                        else ""
                    ) or self._infer_backup_type(fname)
                    header_valid = self.has_sqlite_header(entry)
                    info = self.inspect_backup(entry)
                    try:
                        sha256_hex = self.compute_file_sha256(entry)
                    except OSError:
                        sha256_hex = None
                    # Preserve legacy/unknown files as unmanaged unless explicitly recorded by policy
                    prior_managed = bool(isinstance(cached, dict) and cached.get("managed_by_policy") is True)
                    meta = {
                        "filename": fname,
                        "backup_type": bkp_type,
                        "managed_by_policy": prior_managed,
                        "size_bytes": size_bytes,
                        "mtime_ns": mtime_ns,
                        "mtime_epoch": mtime_epoch,
                        "modified_at": info["modified_at"],
                        "created_at_iso": dt.isoformat(timespec="seconds"),
                        "sha256": sha256_hex,
                        "sqlite_header_valid": header_valid,
                        "integrity_ok": bool(info["integrity_ok"]),
                        "foreign_key_ok": bool(info["foreign_key_ok"]),
                        "has_required_tables": bool(info["has_required_tables"]),
                        "valid_for_restore": bool(info["valid_for_restore"]),
                        "table_count": info["table_count"],
                        "schema_version": info["schema_version"],
                        "shop_name": info["shop_name"],
                    }
                    updated_entries[fname] = meta
                    manifest_dirty = True

                backups.append(
                    {
                        "filename": fname,
                        "backup_type": meta["backup_type"],
                        "managed_by_policy": bool(meta.get("managed_by_policy", False)),
                        "size_bytes": size_bytes,
                        "size_display": self._format_size_display(size_bytes),
                        "created_at": meta["modified_at"],
                        "modified_at": meta["modified_at"],
                        "created_at_iso": meta["created_at_iso"],
                        "mtime_epoch": mtime_epoch,
                        "sha256": meta.get("sha256"),
                        "sqlite_header_valid": bool(meta["sqlite_header_valid"]),
                        "integrity_ok": bool(meta["integrity_ok"]),
                        "foreign_key_ok": bool(meta["foreign_key_ok"]),
                        "has_required_tables": bool(meta["has_required_tables"]),
                        "valid_for_restore": bool(meta["valid_for_restore"]),
                        "table_count": meta["table_count"],
                        "schema_version": meta["schema_version"],
                        "shop_name": meta["shop_name"],
                        "can_export": bool(meta["integrity_ok"]),
                        "log_status": str(log_meta.get("status") or "DISK_FILE") if log_meta else "DISK_FILE",
                    }
                )

            if set(cached_entries.keys()) != disk_filenames:
                manifest_dirty = True

            if manifest_dirty:
                manifest["entries"] = updated_entries
                self._save_manifest_atomic(target_dir, manifest)

        backups.sort(key=lambda item: (item["mtime_epoch"], item["filename"]), reverse=True)
        return backups

    def apply_retention_policy(
        self,
        backup_dir: Optional[Union[str, Path]] = None,
        *,
        now_epoch: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Apply conservative GFS local backup retention policy:
        - AUTO_SCHEDULED (intraday): keep latest 6 within the last 24 hours.
        - AUTO_DAILY: keep latest 14 distinct local calendar days (newest per day).
        - AUTO_WEEKLY: keep latest 8 distinct ISO weeks (newest per week).
        - Protected classes: keep latest 10 MANUAL, 10 BEFORE_UPGRADE, 10 BEFORE_RESTORE.

        Strict safety guarantees:
        - NEVER deletes legacy/unmanaged (`managed_by_policy=False`) or unknown (`EXTERNAL`) backups.
        - NEVER deletes unverified backups or `.corrupt_raw.bak` forensic files.
        - NEVER deletes backups currently pinned by download, inspection, or restore.
        - ALWAYS preserves at least 2 verified backups overall (never prunes the sole verified backup).
        - Deletes oldest eligible expired backups first; preserves all `backup_log` audit records.
        """
        target_dir = self.get_backup_dir(backup_dir)
        report: Dict[str, Any] = {
            "pruned": [],
            "preserved_count": 0,
            "skipped_pinned": [],
            "skipped_floor": [],
            "errors": [],
        }
        if not target_dir.exists() or not target_dir.is_dir():
            return report

        now = time.time() if now_epoch is None else float(now_epoch)
        all_backups = self.list_backups(target_dir)

        verified_all = [
            b for b in all_backups if b.get("integrity_ok") and b.get("valid_for_restore")
        ]
        remaining_verified_count = len(verified_all)
        if remaining_verified_count <= self.RETENTION_MIN_VERIFIED_FLOOR:
            report["preserved_count"] = len(all_backups)
            return report

        # Only positively classified, verified, policy-managed backups are eligible for pruning.
        # Legacy backups (managed_by_policy=False), unknown/EXTERNAL backups, and unverified files
        # are unconditionally preserved.
        eligible_by_type: Dict[str, List[Dict[str, Any]]] = {t: [] for t in self.KNOWN_POLICY_TYPES}
        for item in verified_all:
            btype = str(item.get("backup_type") or "").strip().upper()
            if not item.get("managed_by_policy"):
                continue
            if btype not in eligible_by_type:
                continue
            eligible_by_type[btype].append(item)

        # Sort each category newest-first by (mtime_epoch, filename)
        for items in eligible_by_type.values():
            items.sort(key=lambda x: (float(x["mtime_epoch"]), str(x["filename"])), reverse=True)

        keep_filenames: Set[str] = set()

        # 1. Intraday AUTO_SCHEDULED: keep latest 6 within the last 24 hours
        intraday_cutoff = now - float(self.RETENTION_INTRADAY_WINDOW_SECONDS)
        intraday_within_window = [
            b for b in eligible_by_type["AUTO_SCHEDULED"] if float(b["mtime_epoch"]) >= intraday_cutoff
        ]
        for b in intraday_within_window[: self.RETENTION_INTRADAY_KEEP]:
            keep_filenames.add(b["filename"])

        # 2. Daily AUTO_DAILY: keep newest per local calendar day for the latest 14 distinct days
        kept_days: Set[str] = set()
        for b in eligible_by_type["AUTO_DAILY"]:
            day_key = datetime.fromtimestamp(float(b["mtime_epoch"])).strftime("%Y-%m-%d")
            if day_key not in kept_days and len(kept_days) < self.RETENTION_DAILY_KEEP:
                kept_days.add(day_key)
                keep_filenames.add(b["filename"])

        # 3. Weekly AUTO_WEEKLY: keep newest per ISO week for the latest 8 distinct ISO weeks
        kept_weeks: Set[Tuple[int, int]] = set()
        for b in eligible_by_type["AUTO_WEEKLY"]:
            iso = datetime.fromtimestamp(float(b["mtime_epoch"])).isocalendar()
            week_key = (int(iso[0]), int(iso[1]))
            if week_key not in kept_weeks and len(kept_weeks) < self.RETENTION_WEEKLY_KEEP:
                kept_weeks.add(week_key)
                keep_filenames.add(b["filename"])

        # 4. Protected categories: keep latest 10 each
        for protected_type, limit in (
            ("MANUAL", self.RETENTION_MANUAL_KEEP),
            ("BEFORE_UPGRADE", self.RETENTION_BEFORE_UPGRADE_KEEP),
            ("BEFORE_RESTORE", self.RETENTION_BEFORE_RESTORE_KEEP),
        ):
            for b in eligible_by_type[protected_type][:limit]:
                keep_filenames.add(b["filename"])

        # Collect expired eligible backups and delete OLDEST FIRST
        expired_candidates: List[Dict[str, Any]] = []
        for items in eligible_by_type.values():
            for b in items:
                if b["filename"] not in keep_filenames:
                    expired_candidates.append(b)
        expired_candidates.sort(key=lambda x: (float(x["mtime_epoch"]), str(x["filename"])))

        pruned_names: Set[str] = set()
        for candidate in expired_candidates:
            fname = str(candidate["filename"])
            if remaining_verified_count <= self.RETENTION_MIN_VERIFIED_FLOOR:
                report["skipped_floor"].append(fname)
                continue

            if not fname.lower().endswith(".db") or ".corrupt_raw.bak" in fname.lower():
                continue

            target_path = target_dir / fname
            if is_backup_file_pinned(target_path):
                report["skipped_pinned"].append(fname)
                continue

            try:
                self._unlink_with_retry(target_path, ignore_missing=True)
                self._remove_sidecars(target_path, strict=False)
                pruned_names.add(fname)
                report["pruned"].append(fname)
                remaining_verified_count -= 1
            except PermissionError:
                report["skipped_pinned"].append(fname)
            except OSError as exc:
                report["errors"].append(f"{fname}: {exc}")

        if pruned_names:
            try:
                manifest_lock = _get_manifest_lock(target_dir)
                with manifest_lock:
                    manifest = self.load_manifest(target_dir)
                    entries = dict(manifest.get("entries") or {})
                    for fname in pruned_names:
                        entries.pop(fname, None)
                    manifest["entries"] = entries
                    self._save_manifest_atomic(target_dir, manifest)
            except Exception as exc:
                logger.debug("Non-fatal manifest prune sync warning: %s", exc)
            logger.info(
                "Backup retention pruned %d expired backup(s): %s",
                len(pruned_names),
                ", ".join(sorted(pruned_names)),
            )

        report["preserved_count"] = len(all_backups) - len(pruned_names)
        return report

    @classmethod
    def _match_known_staging_file(cls, filename: str) -> Tuple[bool, Optional[int]]:
        """
        Return (matched, embedded_pid) ONLY for exact known Krushidhan staging temp files.
        Finalized .db files, .corrupt_raw.bak forensic copies, and unrelated files always return (False, None).
        """
        if not filename or not filename.startswith("."):
            return False, None
        lower = filename.lower()
        if lower.endswith(".db") or ".corrupt_raw.bak" in lower:
            return False, None

        m_bkp = cls._ORPHAN_BACKUP_STAGING_RE.match(filename)
        if m_bkp:
            return True, int(m_bkp.group(1))

        m_rest = cls._ORPHAN_RESTORE_STAGING_RE.match(filename)
        if m_rest:
            return True, int(m_rest.group(1))

        if cls._ORPHAN_UPLOAD_STAGING_RE.match(filename):
            return True, None

        return False, None

    def cleanup_orphaned_staging_files(
        self,
        backup_dir: Optional[Union[str, Path]] = None,
        *,
        include_data_dir: bool = True,
        min_age_seconds: Optional[float] = None,
        now_epoch: Optional[float] = None,
    ) -> Dict[str, List[str]]:
        """
        Cautiously remove abandoned temporary staging files left behind by interrupted
        backup, upload-inspection, restore, or rollback operations.

        Safety guarantees:
        - Recognizes ONLY exact known hidden staging filename patterns.
        - NEVER deletes finalized `.db` backups, `.corrupt_raw.bak` forensic files, or symlinks.
        - NEVER runs while exclusive maintenance is active.
        - NEVER deletes files actively registered by an in-flight operation.
        - NEVER deletes files whose embedded PID is still alive.
        - Requires conservative age threshold (default 15 minutes) even for dead PIDs or upload temps.
        - Skips locked files cleanly on Windows/macOS without raising.
        """
        report: Dict[str, List[str]] = {"removed": [], "skipped": [], "errors": []}
        if self.db.is_in_maintenance:
            return report

        age_threshold = (
            self.DEFAULT_ORPHAN_MIN_AGE_SECONDS
            if min_age_seconds is None
            else max(0.0, float(min_age_seconds))
        )
        now = time.time() if now_epoch is None else float(now_epoch)

        scan_dirs: List[Path] = []
        target_backup_dir = self.get_backup_dir(backup_dir)
        if target_backup_dir.exists() and target_backup_dir.is_dir():
            scan_dirs.append(target_backup_dir)

        if include_data_dir and self.db.db_path != ":memory:":
            data_dir = Path(self.db.db_path).parent
            if data_dir.exists() and data_dir.is_dir():
                try:
                    same_dir = any(d.resolve() == data_dir.resolve() for d in scan_dirs)
                except OSError:
                    same_dir = data_dir in scan_dirs
                if not same_dir:
                    scan_dirs.append(data_dir)

        for directory in scan_dirs:
            try:
                entries = list(directory.iterdir())
            except OSError as exc:
                report["errors"].append(f"{directory}: {exc}")
                continue

            for entry in entries:
                fname = entry.name
                matched, embedded_pid = self._match_known_staging_file(fname)
                if not matched:
                    continue

                try:
                    if entry.is_symlink() or not entry.is_file():
                        report["skipped"].append(str(entry))
                        continue
                except OSError:
                    report["skipped"].append(str(entry))
                    continue

                if _is_active_staging_path(entry):
                    report["skipped"].append(str(entry))
                    continue

                if embedded_pid is not None and _is_process_alive(embedded_pid):
                    report["skipped"].append(str(entry))
                    continue

                try:
                    st = entry.stat()
                except OSError:
                    report["skipped"].append(str(entry))
                    continue

                file_age = now - float(st.st_mtime)
                if file_age < age_threshold:
                    report["skipped"].append(str(entry))
                    continue

                try:
                    entry.unlink()
                    report["removed"].append(str(entry))
                except FileNotFoundError:
                    continue
                except PermissionError:
                    # File is likely held open on Windows or locked externally; skip conservatively
                    report["skipped"].append(str(entry))
                except OSError as exc:
                    report["errors"].append(f"{entry}: {exc}")

        if report["removed"]:
            logger.info(
                "Cleaned up %d orphaned backup/restore staging file(s).",
                len(report["removed"]),
            )
        return report

    def get_backup_history(self) -> List[Dict[str, Any]]:
        """Fetch list of all past backups."""
        rows = self.db.fetch_all("SELECT * FROM backup_log ORDER BY created_at DESC;")
        return [dict(r) for r in rows]
