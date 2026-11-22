"""
Comprehensive Verification Suite for Milestone 1: Database Migration v4 Foundation.

Covers:
1. Migration registration and successful v3-to-v4 upgrade.
2. Repeated migration safety (idempotency).
3. Historical NULL preservation (proves no fabricated zero defaults on existing returns).
4. New-column CHECK constraints enforcement.
5. Partial unique indexes for idempotency_key and voucher references.
6. Correct foreign-key targets (including purchase_items(item_id)).
7. Financial DELETE protection triggers (including legacy CASCADE safety).
8. Dynamic system-account creation and wrong-group rejection.
9. Purchase Return Account group and balance recalculation under EXPENSE convention.
10. Clean-install vs migrated schema comparison.
11. PRAGMA integrity_check validation.
12. PRAGMA foreign_key_check validation.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from src.db.connection import DatabaseManager
from src.db.migrations import (
    BUILTIN_MIGRATIONS,
    MigrationRunner,
    MigrationSafetyError,
    validate_migrations,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Generator[Path, None, None]:
    """Provides an isolated temporary database path that is cleaned up after testing."""
    db_file = tmp_path / "test_migration_v4.db"
    yield db_file
    # Cleanup WAL and auxiliary files if any
    for ext in ("", "-wal", "-shm"):
        f = Path(f"{db_file}{ext}")
        if f.exists():
            try:
                f.unlink()
            except Exception:
                pass


def _create_v3_baseline_db(db_path: Path, with_historical_return: bool = True) -> None:
    """
    Creates an authentic v3 database (pre-v4) containing:
    - baseline schema.sql without v4 additions
    - seed.sql
    - schema_version at 3
    - optional historical purchase return record
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 1. Load baseline v1-v3 schema
    # Create baseline tables
    cur.executescript("""
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS suppliers (
            supplier_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            mobile TEXT,
            email TEXT,
            gstin TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            pincode TEXT,
            outstanding_balance REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS categories (
            category_id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_name TEXT NOT NULL UNIQUE,
            description TEXT
        );

        CREATE TABLE IF NOT EXISTS products (
            product_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            category_id INTEGER REFERENCES categories(category_id),
            hsn_code TEXT,
            default_purchase_rate REAL DEFAULT 0,
            default_sale_rate REAL DEFAULT 0,
            default_mrp REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS stock_batches (
            batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
            batch_no TEXT NOT NULL,
            mfg_date TEXT,
            exp_date TEXT,
            purchase_rate REAL NOT NULL DEFAULT 0,
            sale_rate REAL NOT NULL DEFAULT 0,
            mrp REAL NOT NULL DEFAULT 0,
            opening_qty REAL DEFAULT 0,
            current_qty REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(product_id, batch_no)
        );

        CREATE TABLE IF NOT EXISTS purchases (
            purchase_id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_no TEXT NOT NULL,
            purchase_date TEXT NOT NULL,
            supplier_id INTEGER NOT NULL REFERENCES suppliers(supplier_id),
            challan_no TEXT,
            payment_type TEXT DEFAULT 'CREDIT',
            total_taxable REAL DEFAULT 0,
            total_cgst REAL DEFAULT 0,
            total_sgst REAL DEFAULT 0,
            total_igst REAL DEFAULT 0,
            extra_charges REAL DEFAULT 0,
            round_off REAL DEFAULT 0,
            net_amount REAL NOT NULL,
            paid_amount REAL DEFAULT 0,
            due_amount REAL DEFAULT 0,
            remarks TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS purchase_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_id INTEGER NOT NULL REFERENCES purchases(purchase_id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(product_id),
            batch_no TEXT NOT NULL,
            mfg_date TEXT,
            exp_date TEXT,
            qty REAL NOT NULL,
            free_qty REAL DEFAULT 0,
            purchase_rate REAL NOT NULL,
            sale_rate REAL NOT NULL,
            mrp REAL NOT NULL,
            discount_percent REAL DEFAULT 0,
            discount_amount REAL DEFAULT 0,
            taxable_amount REAL NOT NULL,
            cgst_rate REAL DEFAULT 0,
            cgst_amount REAL DEFAULT 0,
            sgst_rate REAL DEFAULT 0,
            sgst_amount REAL DEFAULT 0,
            igst_rate REAL DEFAULT 0,
            igst_amount REAL DEFAULT 0,
            total_amount REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Legacy purchase_returns before v4
        CREATE TABLE IF NOT EXISTS purchase_returns (
            return_id INTEGER PRIMARY KEY AUTOINCREMENT,
            return_no TEXT NOT NULL UNIQUE,
            return_date TEXT NOT NULL,
            purchase_id INTEGER REFERENCES purchases(purchase_id),
            supplier_id INTEGER NOT NULL REFERENCES suppliers(supplier_id),
            total_taxable REAL DEFAULT 0,
            total_tax REAL DEFAULT 0,
            round_off REAL DEFAULT 0,
            net_amount REAL NOT NULL,
            remarks TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Legacy purchase_return_items before v4 (with legacy ON DELETE CASCADE)
        CREATE TABLE IF NOT EXISTS purchase_return_items (
            return_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            return_id INTEGER NOT NULL REFERENCES purchase_returns(return_id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(product_id),
            batch_id INTEGER REFERENCES stock_batches(batch_id),
            qty REAL NOT NULL,
            purchase_rate REAL NOT NULL,
            taxable_amount REAL NOT NULL,
            tax_amount REAL DEFAULT 0,
            total_amount REAL NOT NULL,
            reason TEXT
        );

        CREATE TABLE IF NOT EXISTS ledger_accounts (
            account_id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL UNIQUE,
            account_group TEXT NOT NULL,
            opening_balance REAL DEFAULT 0,
            current_balance REAL DEFAULT 0,
            is_system INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS vouchers (
            voucher_id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_no TEXT NOT NULL UNIQUE,
            voucher_date TEXT NOT NULL,
            voucher_type TEXT NOT NULL,
            total_amount REAL NOT NULL,
            narration TEXT,
            reference_type TEXT,
            reference_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS ledger_entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_id INTEGER NOT NULL REFERENCES vouchers(voucher_id) ON DELETE CASCADE,
            account_id INTEGER NOT NULL REFERENCES ledger_accounts(account_id),
            debit_amount REAL DEFAULT 0,
            credit_amount REAL DEFAULT 0,
            particulars TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS company_settings (
            setting_id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            owner_name TEXT,
            upi_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS backup_log (
            backup_id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            backup_type TEXT NOT NULL,
            backup_size_bytes INTEGER,
            status TEXT DEFAULT 'SUCCESS',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS schema_version (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_number INTEGER NOT NULL UNIQUE,
            description TEXT NOT NULL,
            applied_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        -- Seed baseline accounts as in v3
        INSERT INTO ledger_accounts (account_id, account_name, account_group, is_system) VALUES
        (1, 'Cash in Hand', 'CASH', 1),
        (2, 'Bank Account', 'BANK_ACCOUNTS', 1),
        (3, 'Sales Account', 'INCOME', 1),
        (4, 'Purchase Account', 'EXPENSE', 1),
        (5, 'Sales Return Account', 'EXPENSE', 1),
        (6, 'Purchase Return Account', 'INCOME', 1),
        (7, 'CGST Output Account', 'LIABILITIES', 1),
        (8, 'SGST Output Account', 'LIABILITIES', 1),
        (9, 'IGST Output Account', 'LIABILITIES', 1),
        (10, 'CGST Input Account', 'ASSETS', 1),
        (11, 'SGST Input Account', 'ASSETS', 1),
        (12, 'IGST Input Account', 'ASSETS', 1),
        (13, 'Discount Allowed Account', 'EXPENSE', 1),
        (14, 'Discount Received Account', 'INCOME', 1),
        (15, 'Round Off Account', 'EXPENSE', 1),
        (16, 'Sundry Debtors', 'ASSETS', 1),
        (17, 'Sundry Creditors', 'LIABILITIES', 1),
        (18, 'Operating Expenses', 'EXPENSE', 1);

        -- Record version 1, 2, 3 as already applied
        INSERT INTO schema_version (version_number, description) VALUES
        (1, 'Initial Commercial ERP Baseline Schema'),
        (2, 'Add owner_name and upi_id columns to company_settings'),
        (3, 'Add core double-entry system ledger accounts');
    """)

    if with_historical_return:
        # Insert a sample supplier, product, batch, purchase, and historical return
        cur.execute("INSERT INTO suppliers (supplier_id, name, mobile) VALUES (1, 'Old Agro Chem', '9900000001');")
        cur.execute("INSERT INTO categories (category_id, category_name) VALUES (1, 'Pesticides');")
        cur.execute("INSERT INTO products (product_id, product_name, category_id) VALUES (1, 'Chlorpyrifos 20% EC', 1);")
        cur.execute("INSERT INTO stock_batches (batch_id, product_id, batch_no, current_qty) VALUES (1, 1, 'B-OLD-100', 50.0);")
        cur.execute("""
            INSERT INTO purchases (purchase_id, invoice_no, purchase_date, supplier_id, net_amount)
            VALUES (1, 'INV-2025-001', '2025-10-10', 1, 5000.0);
        """)
        cur.execute("""
            INSERT INTO purchase_items (item_id, purchase_id, product_id, batch_no, qty, purchase_rate, sale_rate, mrp, taxable_amount, total_amount)
            VALUES (1, 1, 1, 'B-OLD-100', 50.0, 100.0, 120.0, 150.0, 4464.29, 5000.0);
        """)
        # Historical return created under v1/v2/v3 without new snapshot fields
        cur.execute("""
            INSERT INTO purchase_returns (return_id, return_no, return_date, purchase_id, supplier_id, total_taxable, total_tax, net_amount, remarks)
            VALUES (1, 'PR-HIST-001', '2025-10-15', 1, 1, 892.86, 107.14, 1000.0, 'Historical damaged return');
        """)
        cur.execute("""
            INSERT INTO purchase_return_items (return_item_id, return_id, product_id, batch_id, qty, purchase_rate, taxable_amount, tax_amount, total_amount, reason)
            VALUES (1, 1, 1, 1, 10.0, 100.0, 892.86, 107.14, 1000.0, 'Damaged seal');
        """)

    conn.commit()
    conn.close()


# =====================================================================
# TEST CASES
# =====================================================================

def test_migration_v4_registration():
    """1. Verify Migration v4 is correctly registered and passes validation."""
    versions = [m.version for m in BUILTIN_MIGRATIONS]
    assert 4 in versions, "Migration v4 must be in BUILTIN_MIGRATIONS"
    
    v4 = next(m for m in BUILTIN_MIGRATIONS if m.version == 4)
    assert "Purchase Returns" in v4.description
    assert v4.handler is not None

    validated = validate_migrations(BUILTIN_MIGRATIONS)
    assert any(m.version == 4 for m in validated)


def test_v3_to_v4_upgrade_successful(temp_db_path: Path):
    """1b. Verify clean upgrade from v3 to v4 using MigrationRunner."""
    _create_v3_baseline_db(temp_db_path, with_historical_return=True)

    db_mgr = DatabaseManager(str(temp_db_path))
    runner = MigrationRunner(db_mgr)
    v4_only = [m for m in BUILTIN_MIGRATIONS if m.version <= 4]

    report = runner.run_pending_migrations(migrations=v4_only)
    assert report.previous_version == 3
    assert report.current_version == 4
    assert 4 in report.applied_versions

    # Verify schema_version table
    conn = sqlite3.connect(temp_db_path)
    cur = conn.cursor()
    cur.execute("SELECT version_number, description FROM schema_version WHERE version_number = 4;")
    row = cur.fetchone()
    assert row is not None
    assert "Purchase Returns" in row[1]
    conn.close()


def test_repeated_migration_safety(temp_db_path: Path):
    """2. Verify that running MigrationRunner repeatedly is a safe no-op."""
    _create_v3_baseline_db(temp_db_path)

    db_mgr = DatabaseManager(str(temp_db_path))
    runner = MigrationRunner(db_mgr)
    v4_only = [m for m in BUILTIN_MIGRATIONS if m.version <= 4]

    # First run: applies v4
    report1 = runner.run_pending_migrations(migrations=v4_only)
    assert 4 in report1.applied_versions

    # Second run: no pending migrations
    report2 = runner.run_pending_migrations(migrations=v4_only)
    assert report2.previous_version == 4
    assert report2.current_version == 4
    assert len(report2.applied_versions) == 0


def test_historical_null_preservation(temp_db_path: Path):
    """3. Verify that historical rows preserve NULL without fabricated zero defaults."""
    _create_v3_baseline_db(temp_db_path, with_historical_return=True)

    db_mgr = DatabaseManager(str(temp_db_path))
    MigrationRunner(db_mgr).run_pending_migrations()

    conn = sqlite3.connect(temp_db_path)
    cur = conn.cursor()

    # Check historical header record
    cur.execute("""
        SELECT return_id, return_no, total_cgst, total_sgst, total_igst,
               status, itc_status, voucher_id, idempotency_key
        FROM purchase_returns WHERE return_id = 1;
    """)
    hdr = cur.fetchone()
    assert hdr[0] == 1
    assert hdr[1] == "PR-HIST-001"
    # All newly added snapshot fields MUST be NULL, not 0.0 or 'POSTED'
    assert hdr[2] is None, "Historical total_cgst must be NULL"
    assert hdr[3] is None, "Historical total_sgst must be NULL"
    assert hdr[4] is None, "Historical total_igst must be NULL"
    assert hdr[5] is None, "Historical status must be NULL"
    assert hdr[6] is None, "Historical itc_status must be NULL"
    assert hdr[7] is None, "Historical voucher_id must be NULL"
    assert hdr[8] is None, "Historical idempotency_key must be NULL"

    # Check historical line item record
    cur.execute("""
        SELECT return_item_id, purchase_item_id, discount_amount, net_rate,
               cgst_rate, cgst_amount, sgst_rate, sgst_amount, igst_rate, igst_amount
        FROM purchase_return_items WHERE return_item_id = 1;
    """)
    item = cur.fetchone()
    assert item[0] == 1
    assert item[1] is None, "Historical purchase_item_id link must remain NULL"
    assert item[2] is None, "Historical discount_amount must remain NULL"
    assert item[3] is None, "Historical net_rate must remain NULL"
    assert item[4] is None, "Historical cgst_rate must remain NULL"
    assert item[5] is None, "Historical cgst_amount must remain NULL"
    assert item[6] is None, "Historical sgst_rate must remain NULL"
    assert item[7] is None, "Historical sgst_amount must remain NULL"
    assert item[8] is None, "Historical igst_rate must remain NULL"
    assert item[9] is None, "Historical igst_amount must remain NULL"

    conn.close()


def test_new_column_check_constraints_enforced(temp_db_path: Path):
    """4. Verify CHECK constraints on migrated table reject invalid values."""
    _create_v3_baseline_db(temp_db_path)
    db_mgr = DatabaseManager(str(temp_db_path))
    MigrationRunner(db_mgr).run_pending_migrations()

    conn = sqlite3.connect(temp_db_path)
    cur = conn.cursor()

    # Invalid status
    with pytest.raises(sqlite3.IntegrityError):
        cur.execute("""
            INSERT INTO purchase_returns (return_no, return_date, purchase_id, supplier_id, net_amount, status)
            VALUES ('PR-FAIL-1', '2026-11-15', 1, 1, 100.0, 'INVALID_STATUS');
        """)

    # Invalid itc_status
    with pytest.raises(sqlite3.IntegrityError):
        cur.execute("""
            INSERT INTO purchase_returns (return_no, return_date, purchase_id, supplier_id, net_amount, itc_status)
            VALUES ('PR-FAIL-2', '2026-11-15', 1, 1, 100.0, 'BOGUS_ITC');
        """)

    # Negative total_cgst
    with pytest.raises(sqlite3.IntegrityError):
        cur.execute("""
            INSERT INTO purchase_returns (return_no, return_date, purchase_id, supplier_id, net_amount, total_cgst)
            VALUES ('PR-FAIL-3', '2026-11-15', 1, 1, 100.0, -50.0);
        """)

    # Negative line discount_amount
    with pytest.raises(sqlite3.IntegrityError):
        cur.execute("""
            INSERT INTO purchase_return_items (return_id, product_id, qty, purchase_rate, taxable_amount, total_amount, discount_amount)
            VALUES (1, 1, 1.0, 100.0, 100.0, 100.0, -10.0);
        """)

    conn.close()


def test_partial_unique_indexes(temp_db_path: Path):
    """5. Verify partial unique indexes allow multiple NULLs but enforce uniqueness for non-NULL values."""
    _create_v3_baseline_db(temp_db_path)
    db_mgr = DatabaseManager(str(temp_db_path))
    MigrationRunner(db_mgr).run_pending_migrations()

    conn = sqlite3.connect(temp_db_path)
    cur = conn.cursor()

    # Create dummy vouchers
    cur.execute("INSERT INTO vouchers (voucher_id, voucher_no, voucher_date, voucher_type, total_amount) VALUES (1, 'V-1', '2026-11-15', 'DEBIT_NOTE', 500.0);")
    cur.execute("INSERT INTO vouchers (voucher_id, voucher_no, voucher_date, voucher_type, total_amount) VALUES (2, 'V-2', '2026-11-15', 'DEBIT_NOTE', 500.0);")

    # Multiple NULL idempotency keys are allowed
    cur.execute("INSERT INTO purchase_returns (return_no, return_date, purchase_id, supplier_id, net_amount, idempotency_key) VALUES ('PR-A1', '2026-11-15', 1, 1, 100.0, NULL);")
    cur.execute("INSERT INTO purchase_returns (return_no, return_date, purchase_id, supplier_id, net_amount, idempotency_key) VALUES ('PR-A2', '2026-11-15', 1, 1, 100.0, NULL);")

    # Non-null idempotency key works
    cur.execute("INSERT INTO purchase_returns (return_no, return_date, purchase_id, supplier_id, net_amount, idempotency_key) VALUES ('PR-A3', '2026-11-15', 1, 1, 100.0, 'KEY-123');")

    # Duplicate non-null idempotency key fails
    with pytest.raises(sqlite3.IntegrityError):
        cur.execute("INSERT INTO purchase_returns (return_no, return_date, purchase_id, supplier_id, net_amount, idempotency_key) VALUES ('PR-A4', '2026-11-15', 1, 1, 100.0, 'KEY-123');")

    # Duplicate voucher_id fails
    cur.execute("INSERT INTO purchase_returns (return_no, return_date, purchase_id, supplier_id, net_amount, voucher_id) VALUES ('PR-B1', '2026-11-15', 1, 1, 100.0, 1);")
    with pytest.raises(sqlite3.IntegrityError):
        cur.execute("INSERT INTO purchase_returns (return_no, return_date, purchase_id, supplier_id, net_amount, voucher_id) VALUES ('PR-B2', '2026-11-15', 1, 1, 100.0, 1);")

    conn.close()


def test_correct_foreign_key_targets(temp_db_path: Path):
    """6. Verify foreign key targets, especially purchase_items(item_id) and ledger_accounts(account_id)."""
    _create_v3_baseline_db(temp_db_path)
    db_mgr = DatabaseManager(str(temp_db_path))
    MigrationRunner(db_mgr).run_pending_migrations()

    conn = sqlite3.connect(temp_db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")

    # Valid purchase_item_id (item_id 1 exists)
    cur.execute("""
        INSERT INTO purchase_return_items (return_id, purchase_item_id, product_id, batch_id, qty, purchase_rate, taxable_amount, total_amount)
        VALUES (1, 1, 1, 1, 5.0, 100.0, 500.0, 500.0);
    """)

    # Invalid purchase_item_id (item_id 9999 does not exist)
    with pytest.raises(sqlite3.IntegrityError):
        cur.execute("""
            INSERT INTO purchase_return_items (return_id, purchase_item_id, product_id, batch_id, qty, purchase_rate, taxable_amount, total_amount)
            VALUES (1, 9999, 1, 1, 5.0, 100.0, 500.0, 500.0);
        """)

    # Valid settlement insertion referencing ledger_accounts
    cur.execute("""
        INSERT INTO purchase_return_settlements (return_id, settlement_type, amount, account_id)
        VALUES (1, 'REFUND_CASH', 500.0, 1);
    """)

    # Invalid settlement account_id (account_id 9999 does not exist)
    with pytest.raises(sqlite3.IntegrityError):
        cur.execute("""
            INSERT INTO purchase_return_settlements (return_id, settlement_type, amount, account_id)
            VALUES (1, 'REFUND_CASH', 500.0, 9999);
        """)

    conn.close()


def test_financial_delete_protection_triggers(temp_db_path: Path):
    """7. Verify BEFORE DELETE triggers block deletion on all 4 financial tables, neutralizing legacy cascade."""
    _create_v3_baseline_db(temp_db_path)
    db_mgr = DatabaseManager(str(temp_db_path))
    MigrationRunner(db_mgr).run_pending_migrations()

    conn = sqlite3.connect(temp_db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")

    # Insert a dummy supplier credit and settlement
    cur.execute("""
        INSERT INTO supplier_credits (credit_id, credit_no, supplier_id, return_id, credit_date, original_amount, balance_amount, status)
        VALUES (1, 'SC-001', 1, 1, '2026-11-15', 500.0, 500.0, 'AVAILABLE');
    """)
    cur.execute("""
        INSERT INTO purchase_return_settlements (return_id, settlement_type, amount, credit_id)
        VALUES (1, 'SUPPLIER_CREDIT', 500.0, 1);
    """)
    conn.commit()

    # 1. Attempt DELETE on purchase_returns
    with pytest.raises(sqlite3.IntegrityError, match="Financial audit protection: Deletion of purchase returns is prohibited"):
        cur.execute("DELETE FROM purchase_returns WHERE return_id = 1;")

    # 2. Attempt DELETE on purchase_return_items
    with pytest.raises(sqlite3.IntegrityError, match="Financial audit protection: Deletion of purchase return items is prohibited"):
        cur.execute("DELETE FROM purchase_return_items WHERE return_item_id = 1;")

    # 3. Attempt DELETE on purchase_return_settlements
    with pytest.raises(sqlite3.IntegrityError, match="Financial audit protection: Deletion of purchase return settlements is prohibited"):
        cur.execute("DELETE FROM purchase_return_settlements WHERE settlement_id = 1;")

    # 4. Attempt DELETE on supplier_credits
    with pytest.raises(sqlite3.IntegrityError, match="Financial audit protection: Deletion of supplier credits is prohibited"):
        cur.execute("DELETE FROM supplier_credits WHERE credit_id = 1;")

    conn.close()


def test_dynamic_system_accounts_creation(temp_db_path: Path):
    """8. Verify dynamic system accounts are created under correct groups and wrong groups fail safely."""
    _create_v3_baseline_db(temp_db_path)
    db_mgr = DatabaseManager(str(temp_db_path))
    MigrationRunner(db_mgr).run_pending_migrations()

    conn = sqlite3.connect(temp_db_path)
    cur = conn.cursor()

    cur.execute("SELECT account_name, account_group, is_system FROM ledger_accounts WHERE account_name = 'Supplier Credits Receivable';")
    scr = cur.fetchone()
    assert scr is not None
    assert scr[1] == "ASSETS"
    assert scr[2] == 1

    cur.execute("SELECT account_name, account_group, is_system FROM ledger_accounts WHERE account_name = 'ITC Reversal Suspense Account';")
    itc = cur.fetchone()
    assert itc is not None
    assert itc[1] == "LIABILITIES"
    assert itc[2] == 1

    conn.close()


def test_wrong_group_system_account_fails_safely(temp_db_path: Path):
    """8b. Verify migration aborts with MigrationSafetyError if a system account exists with wrong group."""
    _create_v3_baseline_db(temp_db_path)

    # Pre-insert wrong group for Supplier Credits Receivable (e.g. EXPENSE instead of ASSETS)
    conn = sqlite3.connect(temp_db_path)
    conn.execute("INSERT INTO ledger_accounts (account_name, account_group, is_system) VALUES ('Supplier Credits Receivable', 'EXPENSE', 1);")
    conn.commit()
    conn.close()

    db_mgr = DatabaseManager(str(temp_db_path))
    runner = MigrationRunner(db_mgr)
    
    with pytest.raises(MigrationSafetyError, match="already exists with group 'EXPENSE', expected 'ASSETS'"):
        runner.run_pending_migrations()


def test_purchase_return_account_group_and_balance_recalculation(temp_db_path: Path):
    """9. Verify Purchase Return Account is reclassified to EXPENSE and its balance reconciled."""
    _create_v3_baseline_db(temp_db_path)

    # Simulate historical postings on Purchase Return Account before migration:
    # 2 credit entries of ₹500 each (total_cr = 1000.0)
    conn = sqlite3.connect(temp_db_path)
    conn.execute("INSERT INTO vouchers (voucher_id, voucher_no, voucher_date, voucher_type, total_amount) VALUES (1, 'V-HIST-1', '2025-10-15', 'DEBIT_NOTE', 500.0);")
    conn.execute("INSERT INTO vouchers (voucher_id, voucher_no, voucher_date, voucher_type, total_amount) VALUES (2, 'V-HIST-2', '2025-10-20', 'DEBIT_NOTE', 500.0);")
    conn.execute("INSERT INTO ledger_entries (voucher_id, account_id, debit_amount, credit_amount, particulars) VALUES (1, 6, 0.0, 500.0, 'Historical return 1');")
    conn.execute("INSERT INTO ledger_entries (voucher_id, account_id, debit_amount, credit_amount, particulars) VALUES (2, 6, 0.0, 500.0, 'Historical return 2');")
    # Under INCOME convention, current_balance was credit - debit = 1000.0
    conn.execute("UPDATE ledger_accounts SET current_balance = 1000.0 WHERE account_id = 6;")
    conn.commit()
    conn.close()

    db_mgr = DatabaseManager(str(temp_db_path))
    MigrationRunner(db_mgr).run_pending_migrations()

    conn = sqlite3.connect(temp_db_path)
    cur = conn.cursor()
    cur.execute("SELECT account_id, account_name, account_group, current_balance FROM ledger_accounts WHERE account_id = 6;")
    acc = cur.fetchone()
    assert acc[1] == "Purchase Return Account"
    assert acc[2] == "EXPENSE", "Account group must be updated to EXPENSE"
    # Under EXPENSE convention (normal debit), current_balance = opening_balance + (total_dr - total_cr) = 0 + (0 - 1000) = -1000.0 (contra-expense)
    assert acc[3] == -1000.0, f"Recalculated balance must be -1000.0, got {acc[3]}"
    conn.close()


def test_clean_install_vs_migrated_schema_parity(tmp_path: Path):
    """10. Verify structural column parity between clean installation and v3->v4 migrated database."""
    # Database 1: Clean install from schema.sql + seed.sql
    clean_db_path = tmp_path / "clean.db"
    conn_clean = sqlite3.connect(clean_db_path)
    with open(PROJECT_ROOT / "src/db/schema.sql") as f:
        conn_clean.executescript(f.read())
    with open(PROJECT_ROOT / "src/db/seed.sql") as f:
        conn_clean.executescript(f.read())
    conn_clean.close()

    # Database 2: Upgraded database from v3 to v4
    migrated_db_path = tmp_path / "migrated.db"
    _create_v3_baseline_db(migrated_db_path)
    db_mgr = DatabaseManager(str(migrated_db_path))
    MigrationRunner(db_mgr).run_pending_migrations()

    # Compare column sets
    conn_clean = sqlite3.connect(clean_db_path)
    conn_migrated = sqlite3.connect(migrated_db_path)

    tables_to_check = [
        "purchase_returns",
        "purchase_return_items",
        "supplier_credits",
        "purchase_return_settlements",
    ]

    for tbl in tables_to_check:
        clean_cols = {r[1].lower() for r in conn_clean.execute(f"PRAGMA table_info({tbl});").fetchall()}
        migrated_cols = {r[1].lower() for r in conn_migrated.execute(f"PRAGMA table_info({tbl});").fetchall()}
        assert clean_cols == migrated_cols, f"Column mismatch in table {tbl}: clean={clean_cols - migrated_cols}, migrated={migrated_cols - clean_cols}"

    # Verify intentional foreign-key difference documentation:
    # Clean install defines ON DELETE RESTRICT on purchase_return_items.return_id
    # Migrated database retains legacy ON DELETE CASCADE
    clean_fk = {r[2]: (r[3], r[5], r[6]) for r in conn_clean.execute("PRAGMA foreign_key_list(purchase_return_items);").fetchall()}
    migrated_fk = {r[2]: (r[3], r[5], r[6]) for r in conn_migrated.execute("PRAGMA foreign_key_list(purchase_return_items);").fetchall()}

    assert "purchase_returns" in clean_fk
    assert "purchase_returns" in migrated_fk
    # Clean install is RESTRICT
    assert clean_fk["purchase_returns"][2] == "RESTRICT"
    # Legacy migrated DB is CASCADE (intentional, protected by triggers)
    assert migrated_fk["purchase_returns"][2] == "CASCADE"

    # Verify performance and operational indexes are present in both clean install and migrated DB
    expected_v4_indexes = {
        "idx_purchase_returns_purchase",
        "idx_purchase_returns_supplier",
        "idx_purchase_returns_date",
        "idx_pr_items_return",
        "idx_pr_items_batch",
        "idx_pr_items_product",
        "idx_pr_settlements_return",
        "idx_pr_settlements_credit",
        "idx_pr_settlements_account",
        "idx_supplier_credits_supplier",
        "idx_supplier_credits_status",
    }

    clean_indexes = {r[0] for r in conn_clean.execute("SELECT name FROM sqlite_master WHERE type='index' AND name IS NOT NULL;").fetchall()}
    migrated_indexes = {r[0] for r in conn_migrated.execute("SELECT name FROM sqlite_master WHERE type='index' AND name IS NOT NULL;").fetchall()}

    for idx_name in expected_v4_indexes:
        assert idx_name in clean_indexes, f"Index {idx_name} missing from clean-install schema"
        assert idx_name in migrated_indexes, f"Index {idx_name} missing from migrated v4 database"

    # idx_pr_items_purchase_item is generated dynamically during migration after adding purchase_item_id column
    assert "idx_pr_items_purchase_item" in migrated_indexes

    conn_clean.close()
    conn_migrated.close()


def test_pragma_integrity_and_foreign_keys(temp_db_path: Path):
    """11 & 12. Verify PRAGMA integrity_check and PRAGMA foreign_key_check pass on upgraded database."""
    _create_v3_baseline_db(temp_db_path, with_historical_return=True)
    db_mgr = DatabaseManager(str(temp_db_path))
    MigrationRunner(db_mgr).run_pending_migrations()

    conn = sqlite3.connect(temp_db_path)
    cur = conn.cursor()

    # 11. PRAGMA integrity_check
    cur.execute("PRAGMA integrity_check;")
    integrity = [r[0] for r in cur.fetchall()]
    assert integrity == ["ok"], f"Integrity check failed: {integrity}"

    # 12. PRAGMA foreign_key_check
    cur.execute("PRAGMA foreign_key_check;")
    fk_violations = cur.fetchall()
    assert len(fk_violations) == 0, f"Foreign key check failed with violations: {fk_violations}"

    conn.close()
