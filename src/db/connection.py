"""
SQLite Database Connection Manager with WAL mode, foreign keys, atomic transactions,
safe versioned migrations, and mode-isolated cloud persistence (Vercel-only).
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Sequence, Set, Tuple, Union

from src.config import (
    PROJECT_ROOT,
    get_bundle_root,
    is_cloud_sync_allowed,
    load_runtime_dotenv,
    resolve_app_mode,
    resolve_backup_dir,
    resolve_bundled_resource,
    resolve_db_path,
)
from src.db.migrations import Migration, MigrationReport, MigrationRunner, MigrationSafetyError

logger = logging.getLogger("agri_erp.db")


class DatabaseInitializationError(RuntimeError):
    """Raised when SQLite schema initialization, provisioning, or migration fails."""


class DatabaseMaintenanceError(RuntimeError):
    """Raised when database access is blocked during exclusive maintenance."""


_SQL_FILE_CACHE: Dict[str, str] = {}


def _read_sql_file_cached(file_path: Path) -> str:
    """
    Read and cache SQL asset files in memory to avoid repeated filesystem
    and macOS iCloud File Provider coordination overhead across database initializations.
    Includes retry logic to withstand transient filesystem timeouts (Errno 60).
    """
    path_key = str(file_path.resolve())
    cached = _SQL_FILE_CACHE.get(path_key)
    if cached is not None:
        return cached

    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            _SQL_FILE_CACHE[path_key] = content
            return content
        except (TimeoutError, OSError) as err:
            last_err = err
            time.sleep(0.1 * (2 ** attempt))

    if last_err is not None:
        raise last_err
    return ""


def _finalize_tracked_connection(manager_ref: weakref.ref, tracking_id: int) -> None:
    """Safety-net finalizer releasing connection tracking if a caller forgot .close()."""
    mgr = manager_ref()
    if mgr is not None:
        mgr._mark_connection_closed(tracking_id)


class TrackedSQLiteConnection(sqlite3.Connection):
    """SQLite connection subclass supporting weak references and deterministic close tracking."""

    _manager_ref: Optional[weakref.ref] = None
    _tracking_id: Optional[int] = None
    _is_tracked_closed: bool = False

    def close(self) -> None:
        was_closed = getattr(self, "_is_tracked_closed", False)
        try:
            super().close()
        finally:
            if not was_closed:
                self._is_tracked_closed = True
                mgr_ref = getattr(self, "_manager_ref", None)
                tracking_id = getattr(self, "_tracking_id", None)
                if mgr_ref is not None and tracking_id is not None:
                    mgr = mgr_ref()
                    if mgr is not None:
                        mgr._mark_connection_closed(tracking_id)


class DatabaseManager:
    """Manages SQLite database connections, maintenance locks, schema migrations, and transactions."""

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        app_mode: Optional[str] = None,
        backup_dir: Optional[Union[str, Path]] = None,
        clean_install: Optional[bool] = None,
    ):
        self._init_lock = threading.Lock()
        self._conn_lock = threading.RLock()
        self._drain_cond = threading.Condition(self._conn_lock)
        self._next_conn_seq = 0
        self._active_connections: Dict[int, weakref.ref] = {}
        self._connection_owners: Dict[int, int] = {}
        self._active_tx_threads: Dict[int, int] = {}
        self._maintenance_depth = 0
        self._maintenance_owner_thread: Optional[int] = None
        self._maintenance_reason: Optional[str] = None
        self._write_generation: int = 0
        self.last_checkpoint_result: Optional[Dict[str, Any]] = None
        self.app_mode = resolve_app_mode(app_mode=app_mode)
        load_runtime_dotenv(app_mode=self.app_mode, project_root=PROJECT_ROOT)

        self.bundled_db = get_bundle_root() / "data" / "agri_erp.db"
        self.is_desktop = self.app_mode == "desktop"
        self.is_vercel = self.app_mode == "vercel"
        # Preserve legacy private attribute for compatibility
        self._is_vercel = self.is_vercel
        self._has_mongo = is_cloud_sync_allowed(app_mode=self.app_mode)
        if clean_install is not None:
            self.clean_install_mode = bool(clean_install)
        elif self.is_desktop:
            self.clean_install_mode = True
        else:
            env_clean = os.environ.get("CLEAN_INSTALL", "").strip().lower()
            self.clean_install_mode = env_clean in ("1", "true", "yes")

        self.db_path = resolve_db_path(
            db_path=db_path,
            app_mode=self.app_mode,
            project_root=PROJECT_ROOT,
        )
        self.backup_dir = resolve_backup_dir(
            backup_dir=backup_dir,
            app_mode=self.app_mode,
            project_root=PROJECT_ROOT,
        )

        # In Vercel mode (using default /tmp target), attempt cloud snapshot restore first
        if (
            self.is_vercel
            and db_path is None
            and not os.environ.get("DB_PATH")
            and self.db_path != ":memory:"
        ):
            self._provision_vercel_db()

        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def _provision_vercel_db(self) -> None:
        """
        On Vercel cold start ONLY, restore from MongoDB snapshot first.
        Falls back to the bundled seed database if present; otherwise schema/seed runs on startup.
        Never runs in desktop or local mode.
        """
        if not self.is_vercel or self.is_desktop:
            return

        if isinstance(self.db_path, Path) and self.db_path.exists() and self.db_path.stat().st_size > 100:
            return  # Already provisioned (warm container)

        # Attempt 1: Restore full DB from MongoDB cloud snapshot
        if self._has_mongo and is_cloud_sync_allowed(app_mode=self.app_mode):
            try:
                from src.db.cloud_sync import get_cloud_sync_manager

                mgr = get_cloud_sync_manager()
                if mgr.restore_db_snapshot(Path(self.db_path)):
                    return  # Success — full database restored from cloud
            except Exception as exc:
                logger.warning(
                    "Vercel cold-start MongoDB restore failed (%s); falling back to bundled DB/schema.",
                    exc,
                    exc_info=True,
                )

        # Attempt 2: Copy bundled database if available
        if self.bundled_db.exists() and self.bundled_db.stat().st_size > 0:
            try:
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(self.bundled_db), str(self.db_path))
            except Exception as exc:
                logger.exception(
                    "Failed to copy bundled database '%s' to '%s'.",
                    self.bundled_db,
                    self.db_path,
                )
                raise DatabaseInitializationError(
                    f"Failed to provision bundled database to '{self.db_path}': {exc}"
                ) from exc

    def _prune_dead_connections_locked(self) -> int:
        dead_ids = [cid for cid, ref in self._active_connections.items() if ref() is None]
        for cid in dead_ids:
            self._active_connections.pop(cid, None)
            self._connection_owners.pop(cid, None)
        return len(self._active_connections)

    def _pending_drain_ids_locked(self, owner_tid: int, preexisting_ids: Set[int]) -> List[int]:
        self._prune_dead_connections_locked()
        return [
            cid
            for cid in self._active_connections
            if (cid in preexisting_ids) or (self._connection_owners.get(cid) != owner_tid)
        ]

    @property
    def write_generation(self) -> int:
        """Monotonic counter incremented after committed transactions in this process."""
        with self._conn_lock:
            return self._write_generation

    @property
    def active_connection_count(self) -> int:
        """Number of currently open tracked connections managed by this DatabaseManager."""
        with self._conn_lock:
            return self._prune_dead_connections_locked()

    @property
    def is_in_maintenance(self) -> bool:
        """True while exclusive database maintenance (backup restore or migration) is active."""
        with self._conn_lock:
            return self._maintenance_depth > 0

    def _assert_operation_allowed_locked(self, allow_in_flight_tx: bool = True) -> None:
        if self._maintenance_depth <= 0:
            return
        tid = threading.get_ident()
        if tid == self._maintenance_owner_thread:
            return
        if allow_in_flight_tx and self._active_tx_threads.get(tid, 0) > 0:
            return
        raise DatabaseMaintenanceError(
            f"Database temporarily unavailable during {self._maintenance_reason or 'maintenance'}. Please retry shortly."
        )

    def _mark_connection_closed(self, tracking_id: int) -> None:
        with self._conn_lock:
            removed = self._active_connections.pop(tracking_id, None)
            self._connection_owners.pop(tracking_id, None)
            if removed is not None:
                self._drain_cond.notify_all()

    def _register_connection(
        self,
        conn: TrackedSQLiteConnection,
        allow_in_flight_tx: bool = True,
    ) -> None:
        tid = threading.get_ident()
        with self._conn_lock:
            self._assert_operation_allowed_locked(allow_in_flight_tx=allow_in_flight_tx)
            self._next_conn_seq += 1
            tracking_id = self._next_conn_seq
            conn._manager_ref = weakref.ref(self)
            conn._tracking_id = tracking_id
            conn._is_tracked_closed = False
            self._active_connections[tracking_id] = weakref.ref(conn)
            self._connection_owners[tracking_id] = tid
            weakref.finalize(conn, _finalize_tracked_connection, weakref.ref(self), tracking_id)

    @contextmanager
    def exclusive_maintenance(
        self,
        timeout: float = 10.0,
        reason: str = "maintenance",
    ) -> Generator[None, None, None]:
        """
        Enter exclusive database maintenance mode:
        - Immediately blocks new database operations from external threads (DatabaseMaintenanceError).
        - Allows already in-flight transactions to finish cleanly.
        - Waits up to `timeout` seconds for active external connections to close.
        - Fails safely without yielding if connections remain open after `timeout`.
        """
        tid = threading.get_ident()
        preexisting_ids: Set[int] = set()
        entered_outermost = False

        with self._conn_lock:
            if self._maintenance_depth > 0:
                if self._maintenance_owner_thread != tid:
                    raise DatabaseMaintenanceError(
                        f"Database maintenance already in progress ({self._maintenance_reason})."
                    )
                self._maintenance_depth += 1
            else:
                entered_outermost = True
                self._prune_dead_connections_locked()
                preexisting_ids = set(self._active_connections.keys())
                self._maintenance_depth = 1
                self._maintenance_owner_thread = tid
                self._maintenance_reason = reason

                deadline = time.monotonic() + max(0.0, float(timeout))
                while True:
                    pending = self._pending_drain_ids_locked(tid, preexisting_ids)
                    if not pending:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._maintenance_depth = 0
                        self._maintenance_owner_thread = None
                        self._maintenance_reason = None
                        self._drain_cond.notify_all()
                        raise DatabaseMaintenanceError(
                            f"Timed out waiting for {len(pending)} active database connection(s) "
                            f"to close before '{reason}'."
                        )
                    self._drain_cond.wait(timeout=min(remaining, 0.1))

        try:
            yield
        finally:
            with self._conn_lock:
                if self._maintenance_depth > 1:
                    self._maintenance_depth -= 1
                elif entered_outermost or self._maintenance_depth == 1:
                    self._maintenance_depth = 0
                    self._maintenance_owner_thread = None
                    self._maintenance_reason = None
                    self._drain_cond.notify_all()

    def _create_raw_connection(
        self,
        track: bool = True,
        allow_in_flight_tx: bool = True,
    ) -> TrackedSQLiteConnection:
        with self._conn_lock:
            self._assert_operation_allowed_locked(allow_in_flight_tx=allow_in_flight_tx)

        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            factory=TrackedSQLiteConnection,
        )
        try:
            conn.row_factory = sqlite3.Row

            # Configure SQLite pragmas for durability & offline desktop integrity
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys = ON;")
            if self.db_path != ":memory:":
                cursor.execute("PRAGMA journal_mode = WAL;")
                cursor.execute("PRAGMA synchronous = FULL;")
                cursor.execute("PRAGMA busy_timeout = 10000;")
                cursor.execute("PRAGMA wal_autocheckpoint = 1000;")
            cursor.close()
        except Exception as exc:
            logger.exception("Failed configuring SQLite connection for %s", self.db_path)
            try:
                conn.close()
            except Exception:
                pass
            raise DatabaseInitializationError(
                f"Failed to open/configure SQLite database at '{self.db_path}': {exc}"
            ) from exc

        if track:
            try:
                self._register_connection(conn, allow_in_flight_tx=allow_in_flight_tx)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                raise
        return conn

    def get_connection(self) -> sqlite3.Connection:
        """Create and configure a new SQLite connection (failing loudly if DB initialization fails)."""
        conn = self._create_raw_connection(track=True)

        # Auto-verify that essential tables exist; if not, initialize schema + seed cleanly
        if self.db_path != ":memory:":
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='products' LIMIT 1;"
                )
                has_tables = cur.fetchone()
                cur.close()
                if not has_tables:
                    conn.close()
                    with self._init_lock:
                        check_conn = self._create_raw_connection(track=True)
                        try:
                            chk = check_conn.cursor()
                            chk.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='products' LIMIT 1;"
                            )
                            still_missing = chk.fetchone() is None
                            chk.close()
                        finally:
                            check_conn.close()

                        if still_missing:
                            self.initialize_database(
                                include_seed=True,
                                clean_install=self.clean_install_mode,
                            )
                    conn = self._create_raw_connection(track=True)
            except (MigrationSafetyError, DatabaseInitializationError, DatabaseMaintenanceError):
                try:
                    conn.close()
                except Exception:
                    pass
                raise
            except Exception as exc:
                logger.exception("Database verification/initialization failed for %s", self.db_path)
                try:
                    conn.close()
                except Exception:
                    pass
                raise DatabaseInitializationError(
                    f"Failed to verify or initialize SQLite database at '{self.db_path}': {exc}"
                ) from exc

        return conn

    def initialize_database(
        self,
        include_seed: bool = True,
        clean_install: Optional[bool] = None,
        run_migrations: bool = True,
    ) -> None:
        """
        Runs the schema DDL, optional seed data, and pending non-destructive migrations.
        When clean_install is True (default in APP_MODE='desktop'), uses seed_clean.sql
        with zero developer shop records and zero predictable default passwords.
        """
        with self._conn_lock:
            self._assert_operation_allowed_locked()

        db_assets_dir = resolve_bundled_resource("src", "db")
        schema_file = db_assets_dir / "schema.sql"
        use_clean = self.clean_install_mode if clean_install is None else bool(clean_install)
        if clean_install is not None:
            self.clean_install_mode = use_clean

        seed_file = db_assets_dir / ("seed_clean.sql" if use_clean else "seed.sql")

        if not schema_file.exists():
            raise FileNotFoundError(f"Schema file not found at {schema_file}")
        if include_seed and use_clean and not seed_file.exists():
            raise FileNotFoundError(f"Clean seed file not found at {seed_file}")

        conn = self._create_raw_connection(track=True)
        try:
            cursor = conn.cursor()
            schema_sql = _read_sql_file_cached(schema_file)
            cursor.executescript(schema_sql)

            if include_seed and seed_file.exists():
                seed_sql = _read_sql_file_cached(seed_file)
                cursor.executescript(seed_sql)
            conn.commit()
            cursor.close()
        finally:
            conn.close()

        if run_migrations:
            self.run_migrations()

    def run_migrations(
        self,
        migrations: Optional[Sequence[Migration]] = None,
        backup_dir: Optional[Union[str, Path]] = None,
    ) -> MigrationReport:
        """Run any pending non-destructive migrations with pre-migration backups."""
        runner = MigrationRunner(self)
        return runner.run_pending_migrations(
            migrations=migrations,
            backup_dir=backup_dir if backup_dir is not None else self.backup_dir,
        )

    def _save_cloud_snapshot(self) -> None:
        """After a successful write in Vercel mode, push the full DB file to MongoDB (async)."""
        if self.is_desktop or not (self.is_vercel and self._has_mongo):
            return
        if not is_cloud_sync_allowed(app_mode=self.app_mode):
            return
        if self.db_path == ":memory:":
            return
        try:
            from src.db.cloud_sync import get_cloud_sync_manager

            mgr = get_cloud_sync_manager()
            if mgr.is_cloud_enabled:
                mgr.save_db_snapshot_async(Path(self.db_path))
        except Exception as exc:
            logger.warning("Failed to trigger Vercel cloud DB snapshot sync: %s", exc, exc_info=True)

    @contextmanager
    def transaction(
        self,
        mode: Optional[str] = None,
    ) -> Generator[sqlite3.Connection, None, None]:
        """
        Atomic transaction context manager. Commits on success, rolls back on error.

        Args:
            mode: Optional transaction mode:
                - None: Default deferred transaction using standard connection isolation_level.
                - "DEFERRED": Explicit deferred transaction.
                - "IMMEDIATE": SQLite immediate-write transaction (`BEGIN IMMEDIATE`), acquiring
                  reserved write lock immediately to serialize writes and prevent deadlocks.
        """
        if mode is not None:
            if not isinstance(mode, str):
                raise ValueError(f"Invalid transaction mode: {mode!r}. Expected str or None.")
            normalized_mode = mode.strip().upper()
            if normalized_mode not in ("IMMEDIATE", "DEFERRED"):
                raise ValueError(
                    f"Unsupported transaction mode: '{mode}'. Supported modes are None, 'DEFERRED', or 'IMMEDIATE'."
                )
        else:
            normalized_mode = None

        tid = threading.get_ident()

        with self._conn_lock:
            current_tx_depth = self._active_tx_threads.get(tid, 0)
            if normalized_mode == "IMMEDIATE" and current_tx_depth > 0:
                raise RuntimeError(
                    f"Cannot start an IMMEDIATE transaction on thread {tid}: "
                    f"an active transaction is already in progress (depth={current_tx_depth})."
                )

        conn: Optional[sqlite3.Connection] = None
        tx_tracked = False
        tx_started = False
        old_isolation: Optional[str] = None

        try:
            conn = self.get_connection()
            with self._conn_lock:
                self._active_tx_threads[tid] = self._active_tx_threads.get(tid, 0) + 1
                tx_tracked = True

            if normalized_mode == "IMMEDIATE":
                old_isolation = conn.isolation_level
                conn.isolation_level = None  # Disable implicit transactions in sqlite3
                conn.execute("BEGIN IMMEDIATE;")
                tx_started = True
            else:
                # Default / DEFERRED transaction mode: Python manages BEGIN implicitly
                tx_started = True

            try:
                yield conn
                if normalized_mode == "IMMEDIATE":
                    conn.execute("COMMIT;")
                else:
                    conn.commit()
                with self._conn_lock:
                    self._write_generation += 1
            except Exception:
                if tx_started:
                    try:
                        if normalized_mode == "IMMEDIATE":
                            conn.execute("ROLLBACK;")
                        else:
                            conn.rollback()
                    except Exception:
                        pass
                raise
        finally:
            if old_isolation is not None and conn is not None:
                try:
                    conn.isolation_level = old_isolation
                except Exception:
                    pass

            if tx_tracked:
                with self._conn_lock:
                    count = self._active_tx_threads.get(tid, 0)
                    if count <= 1:
                        self._active_tx_threads.pop(tid, None)
                    else:
                        self._active_tx_threads[tid] = count - 1

            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        # After connection is closed (WAL flushed), persist to cloud in Vercel mode only
        with self._conn_lock:
            should_sync = self._maintenance_depth == 0
        if should_sync:
            self._save_cloud_snapshot()

    def execute_query(self, sql: str, params: Union[Tuple, List] = ()) -> int:
        """Execute an INSERT, UPDATE, or DELETE query and return the lastrowid or rowcount."""
        with self.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return cursor.lastrowid or cursor.rowcount

    def fetch_one(self, sql: str, params: Union[Tuple, List] = ()) -> Optional[sqlite3.Row]:
        """Fetch a single record as sqlite3.Row."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return cursor.fetchone()
        finally:
            conn.close()

    def fetch_all(self, sql: str, params: Union[Tuple, List] = ()) -> List[sqlite3.Row]:
        """Fetch all matching records as a list of sqlite3.Row."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return cursor.fetchall()
        finally:
            conn.close()

    def check_integrity(self) -> List[str]:
        """Runs SQLite PRAGMA integrity_check and returns results (e.g. ['ok'])."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check;")
            rows = cursor.fetchall()
            return [str(r[0]) for r in rows if r]
        finally:
            conn.close()

    def check_foreign_keys(self) -> List[Tuple[Any, ...]]:
        """Runs SQLite PRAGMA foreign_key_check and returns constraint violation tuples."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_key_check;")
            rows = cursor.fetchall()
            return [tuple(r) for r in rows]
        finally:
            conn.close()

    def get_schema_version(self) -> int:
        """Returns the current database schema version from schema_version table."""
        conn = self._create_raw_connection(track=True)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version' LIMIT 1;"
            )
            if not cursor.fetchone():
                cursor.close()
                return 0
            cursor.execute("SELECT MAX(version_number) FROM schema_version;")
            row = cursor.fetchone()
            cursor.close()
            return int(row[0]) if row and row[0] is not None else 0
        except DatabaseMaintenanceError:
            raise
        except Exception as exc:
            logger.exception("Failed reading schema_version from %s", self.db_path)
            raise DatabaseInitializationError(
                f"Failed to read schema version from '{self.db_path}': {exc}"
            ) from exc
        finally:
            conn.close()

    def wait_for_connections_drain(self, timeout: float = 3.0) -> int:
        """
        Wait up to `timeout` seconds for active tracked SQLite connections to close.
        Returns the count of remaining open connections (0 on clean drain).
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._conn_lock:
            while True:
                remaining_count = self._prune_dead_connections_locked()
                if remaining_count == 0:
                    return 0
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    return remaining_count
                self._drain_cond.wait(timeout=min(remaining_time, 0.1))

    def checkpoint_wal(self, mode: str = "TRUNCATE") -> bool:
        """
        Execute bounded PRAGMA wal_checkpoint safely (used on graceful sidecar shutdown).
        Inspects the returned (busy, log, checkpointed) tuple and returns True ONLY when
        the checkpoint completed without a busy condition and with zero uncheckpointed frames.
        """
        cleaned_mode = str(mode or "TRUNCATE").strip().upper()
        if cleaned_mode not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
            raise ValueError(f"Invalid SQLite wal_checkpoint mode: {mode}")

        self.last_checkpoint_result = None
        if self.db_path == ":memory:":
            return False
        db_file = Path(self.db_path)
        if not db_file.exists():
            return False

        conn: Optional[TrackedSQLiteConnection] = None
        try:
            conn = self._create_raw_connection(track=True)
            cur = conn.cursor()
            cur.execute("PRAGMA busy_timeout = 2000;")
            cur.execute(f"PRAGMA wal_checkpoint({cleaned_mode});")
            row = cur.fetchone()
            cur.close()

            if row is None or len(row) < 3:
                self.last_checkpoint_result = {
                    "mode": cleaned_mode,
                    "busy": None,
                    "log": None,
                    "checkpointed": None,
                    "completed": False,
                }
                logger.warning(
                    "SQLite WAL checkpoint (%s) returned no status tuple; treating as incomplete.",
                    cleaned_mode,
                )
                return False

            busy = int(row[0])
            log_frames = int(row[1])
            checkpointed_frames = int(row[2])
            completed = (
                busy == 0
                and log_frames >= 0
                and checkpointed_frames >= 0
                and checkpointed_frames >= log_frames
            )
            self.last_checkpoint_result = {
                "mode": cleaned_mode,
                "busy": busy,
                "log": log_frames,
                "checkpointed": checkpointed_frames,
                "completed": completed,
            }
            if not completed:
                logger.warning(
                    "SQLite WAL checkpoint (%s) incomplete: busy=%s, log=%s, checkpointed=%s",
                    cleaned_mode,
                    busy,
                    log_frames,
                    checkpointed_frames,
                )
            return completed
        except (DatabaseMaintenanceError, sqlite3.Error, OSError) as exc:
            self.last_checkpoint_result = {
                "mode": cleaned_mode,
                "busy": None,
                "log": None,
                "checkpointed": None,
                "completed": False,
                "error": str(exc),
            }
            logger.warning("SQLite WAL checkpoint (%s) skipped/incomplete: %s", cleaned_mode, exc)
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


# Default singleton instance for convenience
_default_db_manager: Optional[DatabaseManager] = None
_default_config_key: Optional[Tuple[str, str, str, str]] = None


def reset_db_manager() -> None:
    """Reset the singleton DatabaseManager (useful for mode switches or tests)."""
    global _default_db_manager, _default_config_key
    _default_db_manager = None
    _default_config_key = None


def get_db_manager(
    db_path: Optional[Union[str, Path]] = None,
    app_mode: Optional[str] = None,
    backup_dir: Optional[Union[str, Path]] = None,
    clean_install: Optional[bool] = None,
) -> DatabaseManager:
    """Get or create the global DatabaseManager instance (reacts cleanly if APP_MODE/DB_PATH/BACKUP_DIR change)."""
    global _default_db_manager, _default_config_key
    if (
        db_path is not None
        or app_mode is not None
        or backup_dir is not None
        or clean_install is not None
    ):
        return DatabaseManager(
            db_path=db_path,
            app_mode=app_mode,
            backup_dir=backup_dir,
            clean_install=clean_install,
        )

    current_mode = resolve_app_mode()
    current_key = (
        current_mode,
        str(resolve_db_path(app_mode=current_mode)),
        str(resolve_backup_dir(app_mode=current_mode)),
        os.environ.get("CLEAN_INSTALL", "").strip().lower(),
    )
    if _default_db_manager is None or _default_config_key != current_key:
        _default_db_manager = DatabaseManager()
        _default_config_key = current_key
    return _default_db_manager


@contextmanager
def transaction(
    db_path: Optional[Union[str, Path]] = None,
    mode: Optional[str] = None,
) -> Generator[sqlite3.Connection, None, None]:
    """Shortcut context manager for transactions using the default or specified database."""
    manager = get_db_manager(db_path)
    with manager.transaction(mode=mode) as conn:
        yield conn
