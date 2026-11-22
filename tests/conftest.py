"""
Pytest configuration and test database isolation fixture.
Ensures tests run against an isolated test database in /tmp and never dirty data/agri_erp.db.
"""
import os
import secrets
from pathlib import Path
import pytest
from src.db.connection import DatabaseManager, get_db_manager


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    import shutil

    test_db_path = Path("/tmp/test_agri_erp.db")
    test_backup_dir = Path("/tmp/test_agri_erp_backups")
    if test_db_path.exists():
        test_db_path.unlink()
    if test_backup_dir.exists():
        shutil.rmtree(test_backup_dir, ignore_errors=True)
    test_backup_dir.mkdir(parents=True, exist_ok=True)

    os.environ["DB_PATH"] = str(test_db_path)
    prev_backup_dir = os.environ.get("BACKUP_DIR")
    os.environ["BACKUP_DIR"] = str(test_backup_dir)

    injected_jwt_secret = False
    if "JWT_SECRET" not in os.environ:
        os.environ["JWT_SECRET"] = secrets.token_urlsafe(48)
        injected_jwt_secret = True

    # Initialize fresh schema & seed for test run
    db = DatabaseManager(db_path=test_db_path, backup_dir=test_backup_dir)
    db.initialize_database(include_seed=True)

    yield

    if injected_jwt_secret:
        os.environ.pop("JWT_SECRET", None)
    if prev_backup_dir is None:
        os.environ.pop("BACKUP_DIR", None)
    else:
        os.environ["BACKUP_DIR"] = prev_backup_dir

    # Clean up test database and test backups after test session
    for candidate in (
        test_db_path,
        Path(f"{test_db_path}-wal"),
        Path(f"{test_db_path}-shm"),
    ):
        if candidate.exists():
            try:
                candidate.unlink()
            except Exception:
                pass
    if test_backup_dir.exists():
        shutil.rmtree(test_backup_dir, ignore_errors=True)
