"""
Dedicated Test Suite for Milestone 2A: Safe SQLite Immediate Write Transactions.

Validates:
1. Existing default transaction behaviour (mode=None).
2. Successful immediate transaction commit (mode="IMMEDIATE").
3. Immediate transaction rollback after an exception.
4. Immediate transaction setup failure cleanup.
5. Nested immediate transaction rejection (both outer default and outer immediate).
6. Unsupported transaction mode rejection (e.g. EXCLUSIVE, invalid types).
7. Connection isolation-level restoration on all exit paths.
8. Active transaction tracking cleanup on every exit path (commit, error, setup fail).
9. SQLite serialization with competing concurrent connections (no SQLITE_BUSY deadlocks).
10. Timeout handling when write lock is held.
11. Module-level transaction() shortcut.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Generator

import pytest

from src.db.connection import DatabaseManager, transaction


@pytest.fixture
def temp_db(tmp_path: Path) -> Generator[DatabaseManager, None, None]:
    """Provides an isolated DatabaseManager instance backed by a temporary file."""
    db_file = tmp_path / "test_immediate_tx.db"
    db_mgr = DatabaseManager(str(db_file))
    db_mgr.initialize_database(include_seed=True)
    yield db_mgr
    # Clean drain
    db_mgr.wait_for_connections_drain(timeout=2.0)


def test_default_transaction_behaviour(temp_db: DatabaseManager):
    """1. Existing default transaction behaviour (mode=None) works without regression."""
    # Commit case
    with temp_db.transaction() as conn:
        assert conn.isolation_level == ""  # Python's default deferred mode
        conn.execute(
            "INSERT INTO categories (category_name, short_name) VALUES (?, ?);",
            ("Default Tx Cat", "DEF"),
        )

    # Verify visibility via fresh connection
    row = temp_db.fetch_one("SELECT category_name FROM categories WHERE category_name = ?;", ("Default Tx Cat",))
    assert row is not None
    assert row["category_name"] == "Default Tx Cat"

    # Rollback case
    with pytest.raises(RuntimeError, match="Simulated default failure"):
        with temp_db.transaction() as conn:
            conn.execute(
                "INSERT INTO categories (category_name, short_name) VALUES (?, ?);",
                ("Rollback Cat", "ROL"),
            )
            raise RuntimeError("Simulated default failure")

    row = temp_db.fetch_one("SELECT category_name FROM categories WHERE category_name = ?;", ("Rollback Cat",))
    assert row is None


def test_immediate_transaction_commit_success(temp_db: DatabaseManager):
    """2. Successful immediate transaction commit (mode='IMMEDIATE')."""
    with temp_db.transaction(mode="IMMEDIATE") as conn:
        # Inside context, isolation_level is None for autocommit raw execution
        assert conn.isolation_level is None
        conn.execute(
            "INSERT INTO categories (category_name, short_name) VALUES (?, ?);",
            ("Immediate Cat", "IMM"),
        )

    # Verify persisted data in separate connection
    row = temp_db.fetch_one("SELECT category_name FROM categories WHERE category_name = ?;", ("Immediate Cat",))
    assert row is not None
    assert row["category_name"] == "Immediate Cat"


def test_immediate_transaction_rollback_on_exception(temp_db: DatabaseManager):
    """3. Immediate transaction rollback after an exception in the with block."""
    with pytest.raises(ValueError, match="Simulated mid-transaction failure"):
        with temp_db.transaction(mode="IMMEDIATE") as conn:
            conn.execute(
                "INSERT INTO categories (category_name, short_name) VALUES (?, ?);",
                ("Failed Immediate Cat", "FIC"),
            )
            raise ValueError("Simulated mid-transaction failure")

    # Verify category was rolled back
    row = temp_db.fetch_one("SELECT category_name FROM categories WHERE category_name = ?;", ("Failed Immediate Cat",))
    assert row is None


def test_immediate_transaction_setup_failure_cleanup(temp_db: DatabaseManager):
    """4. If BEGIN IMMEDIATE fails during setup, connection is closed and tracking is cleaned up."""
    tid = threading.get_ident()
    real_get_conn = temp_db.get_connection

    class FailingConn:
        def __init__(self, inner):
            self._inner = inner
            self.isolation_level = inner.isolation_level

        def execute(self, sql, *args, **kwargs):
            if "BEGIN IMMEDIATE" in str(sql).upper():
                raise sqlite3.OperationalError("database is locked (simulated setup failure)")
            return self._inner.execute(sql, *args, **kwargs)

        def close(self):
            return self._inner.close()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    temp_db.get_connection = lambda: FailingConn(real_get_conn())

    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            with temp_db.transaction(mode="IMMEDIATE"):
                pass
    finally:
        temp_db.get_connection = real_get_conn

    # Verify tracking was fully cleaned up
    assert temp_db._active_tx_threads.get(tid, 0) == 0
    # Verify no dangling active connections
    assert temp_db.active_connection_count == 0


def test_nested_immediate_transaction_rejection(temp_db: DatabaseManager):
    """5. Detect an already-active transaction and reject illegal nested immediate transactions safely."""
    tid = threading.get_ident()

    # Case A: Outer is mode=None, inner attempts mode="IMMEDIATE"
    with temp_db.transaction() as outer_conn:
        assert temp_db._active_tx_threads.get(tid, 0) == 1
        with pytest.raises(RuntimeError, match="Cannot start an IMMEDIATE transaction"):
            with temp_db.transaction(mode="IMMEDIATE"):
                pass
        # Depth is preserved for outer transaction
        assert temp_db._active_tx_threads.get(tid, 0) == 1

    # After outer completes, tracking is 0
    assert temp_db._active_tx_threads.get(tid, 0) == 0

    # Case B: Outer is mode="IMMEDIATE", inner attempts mode="IMMEDIATE"
    with temp_db.transaction(mode="IMMEDIATE") as outer_conn:
        assert temp_db._active_tx_threads.get(tid, 0) == 1
        with pytest.raises(RuntimeError, match="Cannot start an IMMEDIATE transaction"):
            with temp_db.transaction(mode="IMMEDIATE"):
                pass
        # Depth is preserved for outer transaction
        assert temp_db._active_tx_threads.get(tid, 0) == 1

    assert temp_db._active_tx_threads.get(tid, 0) == 0


def test_unsupported_mode_rejection(temp_db: DatabaseManager):
    """6. Reject unsupported transaction modes with clear ValueError."""
    tid = threading.get_ident()

    with pytest.raises(ValueError, match="Unsupported transaction mode: 'EXCLUSIVE'"):
        with temp_db.transaction(mode="EXCLUSIVE"):
            pass

    with pytest.raises(ValueError, match="Unsupported transaction mode: 'SNAPSHOT'"):
        with temp_db.transaction(mode="SNAPSHOT"):
            pass

    with pytest.raises(ValueError, match="Invalid transaction mode"):
        with temp_db.transaction(mode=123):  # Non-string type
            pass

    # Verify no tracking created and no connections opened
    assert temp_db._active_tx_threads.get(tid, 0) == 0
    assert temp_db.active_connection_count == 0


def test_connection_isolation_level_restoration(temp_db: DatabaseManager):
    """7. Verify connection isolation_level is None during IMMEDIATE mode and restored upon exit."""
    captured_conn = None

    # Case A: Normal commit exit
    with temp_db.transaction(mode="IMMEDIATE") as conn:
        captured_conn = conn
        assert conn.isolation_level is None

    assert captured_conn.isolation_level == ""

    # Case B: Exception exit
    captured_conn_err = None
    with pytest.raises(RuntimeError):
        with temp_db.transaction(mode="IMMEDIATE") as conn:
            captured_conn_err = conn
            assert conn.isolation_level is None
            raise RuntimeError("Failure trigger")

    assert captured_conn_err.isolation_level == ""


def test_active_transaction_tracking_cleanup_on_all_exit_paths(temp_db: DatabaseManager):
    """8. Active transaction tracking cleanup on every exit path."""
    tid = threading.get_ident()

    # Path 1: Normal commit mode=None
    with temp_db.transaction():
        pass
    assert temp_db._active_tx_threads.get(tid, 0) == 0

    # Path 2: Normal commit mode="IMMEDIATE"
    with temp_db.transaction(mode="IMMEDIATE"):
        pass
    assert temp_db._active_tx_threads.get(tid, 0) == 0

    # Path 3: Exception mode=None
    with pytest.raises(ZeroDivisionError):
        with temp_db.transaction():
            _ = 1 / 0
    assert temp_db._active_tx_threads.get(tid, 0) == 0

    # Path 4: Exception mode="IMMEDIATE"
    with pytest.raises(ZeroDivisionError):
        with temp_db.transaction(mode="IMMEDIATE"):
            _ = 1 / 0
    assert temp_db._active_tx_threads.get(tid, 0) == 0

    # Path 5: Commit failure inside transaction wrapper
    real_get_conn = temp_db.get_connection

    class CommitFailingConn:
        def __init__(self, inner):
            self._inner = inner
            self.isolation_level = inner.isolation_level

        def execute(self, sql, *args, **kwargs):
            if "COMMIT" in str(sql).upper():
                raise sqlite3.OperationalError("Simulated COMMIT failure")
            return self._inner.execute(sql, *args, **kwargs)

        def close(self):
            return self._inner.close()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    temp_db.get_connection = lambda: CommitFailingConn(real_get_conn())
    try:
        with pytest.raises(sqlite3.OperationalError, match="Simulated COMMIT failure"):
            with temp_db.transaction(mode="IMMEDIATE"):
                pass
    finally:
        temp_db.get_connection = real_get_conn

    assert temp_db._active_tx_threads.get(tid, 0) == 0


def test_concurrent_connections_serialization(temp_db: DatabaseManager):
    """9. Two separate connections competing for writes, demonstrating SQLite serialization."""
    execution_order = []
    t1_acquired = threading.Event()
    t1_can_commit = threading.Event()
    t2_finished = threading.Event()

    def worker_1():
        with temp_db.transaction(mode="IMMEDIATE") as conn:
            execution_order.append("w1_acquired")
            conn.execute(
                "INSERT INTO categories (category_name, short_name) VALUES (?, ?);",
                ("Cat Worker 1", "W1"),
            )
            t1_acquired.set()
            # Wait for worker 2 to start waiting
            t1_can_commit.wait(timeout=2.0)
            execution_order.append("w1_committing")
        execution_order.append("w1_committed")

    def worker_2():
        assert t1_acquired.wait(timeout=2.0)
        execution_order.append("w2_requesting")
        # Give worker_2 slight moment to enter BEGIN IMMEDIATE and wait on lock
        threading.Timer(0.05, t1_can_commit.set).start()
        with temp_db.transaction(mode="IMMEDIATE") as conn:
            execution_order.append("w2_acquired")
            conn.execute(
                "INSERT INTO categories (category_name, short_name) VALUES (?, ?);",
                ("Cat Worker 2", "W2"),
            )
            execution_order.append("w2_committing")
        execution_order.append("w2_committed")
        t2_finished.set()

    th1 = threading.Thread(target=worker_1)
    th2 = threading.Thread(target=worker_2)

    th1.start()
    th2.start()

    th1.join(timeout=5.0)
    th2.join(timeout=5.0)

    assert not th1.is_alive()
    assert not th2.is_alive()

    # Serialization check: Worker 2 MUST NOT acquire lock until Worker 1 commits
    assert execution_order == [
        "w1_acquired",
        "w2_requesting",
        "w1_committing",
        "w1_committed",
        "w2_acquired",
        "w2_committing",
        "w2_committed",
    ]

    # Verify both records persisted cleanly
    rows = temp_db.fetch_all(
        "SELECT category_name FROM categories WHERE category_name IN (?, ?) ORDER BY category_name;",
        ("Cat Worker 1", "Cat Worker 2"),
    )
    assert len(rows) == 2
    assert rows[0]["category_name"] == "Cat Worker 1"
    assert rows[1]["category_name"] == "Cat Worker 2"


def test_lock_timeout_handling(tmp_path: Path):
    """10. Competing immediate write times out cleanly if busy_timeout is exceeded."""
    db_file = tmp_path / "timeout_test.db"
    db_mgr = DatabaseManager(str(db_file))
    db_mgr.initialize_database(include_seed=True)

    t1_has_lock = threading.Event()
    t1_release = threading.Event()

    def lock_holder():
        conn = sqlite3.connect(str(db_file), timeout=5.0)
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE;")
        t1_has_lock.set()
        t1_release.wait(timeout=3.0)
        conn.execute("ROLLBACK;")
        conn.close()

    th = threading.Thread(target=lock_holder)
    th.start()
    try:
        assert t1_has_lock.wait(timeout=2.0)

        # Worker with very short timeout (e.g. 50ms) should fail cleanly with OperationalError
        short_mgr = DatabaseManager(str(db_file))
        # Monkeypatch busy_timeout pragma for this test
        original_create = short_mgr._create_raw_connection

        def create_short_timeout(*args, **kwargs):
            c = original_create(*args, **kwargs)
            c.execute("PRAGMA busy_timeout = 100;")
            return c

        short_mgr._create_raw_connection = create_short_timeout

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            with short_mgr.transaction(mode="IMMEDIATE"):
                pass

        # Verify tracking was released on timeout failure
        tid = threading.get_ident()
        assert short_mgr._active_tx_threads.get(tid, 0) == 0
    finally:
        t1_release.set()
        th.join(timeout=3.0)


def test_top_level_transaction_shortcut(temp_db: DatabaseManager):
    """11. Module-level transaction() shortcut accepts mode argument and operates on default manager."""
    with transaction(temp_db.db_path, mode="IMMEDIATE") as conn:
        assert conn.isolation_level is None
        conn.execute(
            "INSERT INTO categories (category_name, short_name) VALUES (?, ?);",
            ("Shortcut Cat", "SC"),
        )

    row = temp_db.fetch_one("SELECT category_name FROM categories WHERE category_name = ?;", ("Shortcut Cat",))
    assert row is not None
    assert row["category_name"] == "Shortcut Cat"
