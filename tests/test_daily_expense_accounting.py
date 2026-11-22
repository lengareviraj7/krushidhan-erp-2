"""
Comprehensive Isolated Test Suite for Phase 2B-3:
Daily Expense Double-Entry Accounting Repair (C5).

Verifies against strictly isolated temporary SQLite databases:
1. record_daily_expense() creates exactly one row in expenses.
2. record_daily_expense() creates exactly one balanced PAYMENT voucher (reference_type='EXPENSE').
3. Correct expense account is debited (Operating Expenses or matching category account).
4. Correct Cash or Bank account is credited for every supported payment_mode
   (CASH -> Cash in Hand; UPI / BANK_TRANSFER / BANK / CARD / CHEQUE -> Bank Account),
   while unsupported/ambiguous modes (SPLIT, CREDIT, unknown) are rejected with zero writes.
5. Date, amount, description/remarks, reference_no, and category are preserved accurately.
6. POST /api/accounting/expenses API response & frontend-compatible payload remain compatible,
   and cash-drawer reconciliation counts cash expenses exactly once (never double-counted).
7. Forced voucher-posting failure rolls back the expense record atomically on a single connection.
8. Repeated service executions create strictly 1 expense + 1 unique voucher per call (no duplicates).
9. Historical pre-existing expenses without vouchers are untouched and remain reportable.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, reset_db_manager
from src.models.accounting import Expense, LedgerAccount
from src.repositories.accounting_repository import AccountingRepository
from src.services.accounting_service import AccountingService
from src.services.auth_service import AuthService


@pytest.fixture
def exp_env(tmp_path, monkeypatch):
    """Provides an isolated SQLite DB, AccountingService, and authenticated API client."""
    db_path = tmp_path / "test_phase2b3_daily_expenses.db"
    bkp_dir = tmp_path / "test_phase2b3_backups"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    reset_db_manager()

    db = DatabaseManager(db_path=db_path, backup_dir=bkp_dir)
    db.initialize_database(include_seed=True)

    acc_repo = AccountingRepository(db)
    acc_svc = AccountingService(db)
    auth_svc = AuthService(db)
    user = auth_svc.authenticate_user("admin", "krushidhan@2026")
    assert user is not None
    token = auth_svc.create_access_token(
        user.user_id, user.username, user.full_name, user.role
    )
    headers = {"Authorization": f"Bearer {token}"}

    yield {
        "db": db,
        "acc_repo": acc_repo,
        "acc_svc": acc_svc,
        "headers": headers,
    }
    reset_db_manager()


# ---------------------------------------------------------------------------
# 1, 2, 3, 4, 6. Exact 1 expense + 1 balanced voucher + preserved fields
# ---------------------------------------------------------------------------

def test_record_daily_expense_cash_creates_one_expense_and_one_balanced_voucher(exp_env):
    db = exp_env["db"]
    acc_svc = exp_env["acc_svc"]
    acc_repo = exp_env["acc_repo"]

    cash_before = acc_repo.get_cash_account().current_balance
    exp_ac_before = acc_repo.get_expense_account("Shop Rent").current_balance

    exp_id = acc_svc.record_daily_expense(
        category_id=1,  # Shop Rent
        amount=1500.0,
        expense_date="2026-11-17",
        payment_mode="CASH",
        reference_no="RENT-NOV-01",
        remarks="November shop rent paid in cash",
    )
    assert isinstance(exp_id, int) and exp_id > 0

    # Exactly 1 expense row with all fields intact
    exp_rows = db.fetch_all("SELECT * FROM expenses;")
    assert len(exp_rows) == 1
    row = exp_rows[0]
    assert row["expense_id"] == exp_id
    assert row["category_id"] == 1
    assert row["expense_date"] == "2026-11-17"
    assert row["amount"] == pytest.approx(1500.0)
    assert row["payment_account_id"] == acc_repo.get_cash_account().account_id
    assert row["reference_no"] == "RENT-NOV-01"
    assert row["remarks"] == "November shop rent paid in cash"

    # Exactly 1 balanced PAYMENT voucher linked to this expense
    vch_rows = db.fetch_all(
        "SELECT * FROM vouchers WHERE reference_type = 'EXPENSE' AND reference_id = ?;",
        (exp_id,),
    )
    assert len(vch_rows) == 1
    vch = vch_rows[0]
    assert vch["voucher_type"] == "PAYMENT"
    assert vch["voucher_no"].startswith("PAY-")
    assert vch["voucher_date"] == "2026-11-17"
    assert vch["total_amount"] == pytest.approx(1500.0)
    assert vch["narration"] == "November shop rent paid in cash"

    entries = db.fetch_all(
        """
        SELECT la.account_name, le.debit_amount, le.credit_amount
        FROM ledger_entries le
        JOIN ledger_accounts la ON le.account_id = la.account_id
        WHERE le.voucher_id = ?;
        """,
        (vch["voucher_id"],),
    )
    assert len(entries) == 2
    tot_dr = round(sum(float(e["debit_amount"]) for e in entries), 2)
    tot_cr = round(sum(float(e["credit_amount"]) for e in entries), 2)
    assert tot_dr == tot_cr == 1500.0

    by_acct = {e["account_name"]: (float(e["debit_amount"]), float(e["credit_amount"])) for e in entries}
    assert by_acct == {
        "Operating Expenses": (1500.0, 0.0),
        "Cash in Hand": (0.0, 1500.0),
    }

    # Ledger balances updated consistently
    assert acc_repo.get_cash_account().current_balance == pytest.approx(cash_before - 1500.0)
    assert acc_repo.get_expense_account("Shop Rent").current_balance == pytest.approx(exp_ac_before + 1500.0)


def test_record_daily_expense_debits_category_specific_account_when_configured(exp_env):
    db = exp_env["db"]
    acc_svc = exp_env["acc_svc"]
    acc_repo = exp_env["acc_repo"]

    # Configure a dedicated ledger account matching category #2 ('Electricity Bill')
    elec_ac_id = acc_repo.create_account(
        LedgerAccount(account_name="Electricity Bill", account_group="EXPENSE", opening_balance=0.0)
    )

    exp_id = acc_svc.record_daily_expense(
        category_id=2,  # Electricity Bill
        amount=875.50,
        expense_date="2026-11-17",
        payment_mode="UPI",
        remarks="MSEB Bill",
    )

    vch = db.fetch_one("SELECT * FROM vouchers WHERE reference_type = 'EXPENSE' AND reference_id = ?;", (exp_id,))
    assert vch is not None
    entries = db.fetch_all("SELECT * FROM ledger_entries WHERE voucher_id = ?;", (vch["voucher_id"],))
    dr_entry = [e for e in entries if e["debit_amount"] > 0][0]
    cr_entry = [e for e in entries if e["credit_amount"] > 0][0]

    assert dr_entry["account_id"] == elec_ac_id
    assert dr_entry["debit_amount"] == pytest.approx(875.50)
    assert cr_entry["account_id"] == acc_repo.get_bank_account().account_id
    assert cr_entry["credit_amount"] == pytest.approx(875.50)


# ---------------------------------------------------------------------------
# 5. All supported payment methods & rejection of unsupported/ambiguous modes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payment_mode,expected_credit_account",
    [
        ("CASH", "Cash in Hand"),
        ("cash", "Cash in Hand"),
        ("UPI", "Bank Account"),
        ("BANK_TRANSFER", "Bank Account"),
        ("BANK", "Bank Account"),
        ("CARD", "Bank Account"),
        ("CHEQUE", "Bank Account"),
    ],
)
def test_supported_payment_modes_credit_correct_cash_or_bank_account(exp_env, payment_mode, expected_credit_account):
    db = exp_env["db"]
    acc_svc = exp_env["acc_svc"]
    acc_repo = exp_env["acc_repo"]

    exp_id = acc_svc.record_daily_expense(
        category_id=3,
        amount=250.0,
        expense_date="2026-11-17",
        payment_mode=payment_mode,
        remarks=f"Expense via {payment_mode}",
    )

    expected_ac = acc_repo.get_account_by_name(expected_credit_account)
    assert expected_ac is not None

    exp_row = db.fetch_one("SELECT * FROM expenses WHERE expense_id = ?;", (exp_id,))
    assert exp_row["payment_account_id"] == expected_ac.account_id

    vch = db.fetch_one("SELECT * FROM vouchers WHERE reference_type = 'EXPENSE' AND reference_id = ?;", (exp_id,))
    assert vch is not None
    cr_row = db.fetch_one(
        "SELECT * FROM ledger_entries WHERE voucher_id = ? AND credit_amount > 0;",
        (vch["voucher_id"],),
    )
    assert cr_row["account_id"] == expected_ac.account_id
    assert cr_row["credit_amount"] == pytest.approx(250.0)


@pytest.mark.parametrize("bad_mode", ["SPLIT", "CREDIT", "UNKNOWN_MODE", ""])
def test_ambiguous_or_unsupported_payment_mode_rejected_with_zero_writes(exp_env, bad_mode):
    db = exp_env["db"]
    acc_svc = exp_env["acc_svc"]

    with pytest.raises(ValueError, match="Unsupported payment mode"):
        acc_svc.record_daily_expense(
            category_id=1,
            amount=100.0,
            expense_date="2026-11-17",
            payment_mode=bad_mode,
            remarks="Should fail",
        )

    assert db.fetch_one("SELECT COUNT(*) as c FROM expenses;")["c"] == 0
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"] == 0
    assert db.fetch_one("SELECT COUNT(*) as c FROM ledger_entries;")["c"] == 0


# ---------------------------------------------------------------------------
# 7. API compatibility, frontend payload, and Cash Drawer non-double-counting
# ---------------------------------------------------------------------------

def test_api_endpoint_and_cash_drawer_reconciliation_no_double_counting(exp_env):
    db = exp_env["db"]
    headers = exp_env["headers"]

    with TestClient(app) as client:
        # Frontend form payload (CASH)
        res_cash = client.post(
            "/api/accounting/expenses",
            json={
                "category_id": 5,
                "amount": 180.0,
                "expense_date": "2026-11-17",
                "payment_mode": "CASH",
                "remarks": "Farmer tea & water",
            },
            headers=headers,
        )
        assert res_cash.status_code == 200
        body_cash = res_cash.json()
        assert body_cash["success"] is True
        assert isinstance(body_cash["expense_id"], int) and body_cash["expense_id"] > 0
        assert "message" in body_cash

        # Legacy payload using `description` instead of `remarks` (BANK_TRANSFER)
        res_bank = client.post(
            "/api/accounting/expenses",
            json={
                "category_id": 4,
                "amount": 420.0,
                "expense_date": "2026-11-17",
                "payment_mode": "BANK_TRANSFER",
                "description": "Freight transport NEFT",
            },
            headers=headers,
        )
        assert res_bank.status_code == 200
        bank_exp_id = res_bank.json()["expense_id"]
        bank_exp_row = db.fetch_one("SELECT remarks FROM expenses WHERE expense_id = ?;", (bank_exp_id,))
        assert bank_exp_row["remarks"] == "Freight transport NEFT"

        # Cash drawer reconciliation MUST count CASH expense (180) ONCE — NOT also in cash_supplier_payments!
        cd_res = client.get("/api/accounting/cash-closing-summary?date=2026-11-17", headers=headers)
        assert cd_res.status_code == 200
        cd = cd_res.json()
        assert cd["cash_expenses"] == pytest.approx(180.0)
        assert cd["cash_supplier_payments"] == pytest.approx(0.0)
        assert cd["total_cash_outflow"] == pytest.approx(180.0)
        assert cd["expected_drawer_cash"] == pytest.approx(5000.0 - 180.0)

        # Day Book reports both expenses (180 + 420 = 600) and both linked vouchers
        db_res = client.get("/api/accounting/day-book?date=2026-11-17", headers=headers)
        assert db_res.status_code == 200
        day_book = db_res.json()
        assert day_book["expenses_count"] == 2
        assert day_book["total_expenses"] == pytest.approx(600.0)
        assert len(day_book["vouchers"]) == 2


# ---------------------------------------------------------------------------
# 8. Atomic rollback on voucher failure + single SQLite connection
# ---------------------------------------------------------------------------

def test_forced_voucher_failure_rolls_back_daily_expense_atomically_single_conn(exp_env, monkeypatch):
    db = exp_env["db"]
    acc_svc = exp_env["acc_svc"]
    acc_repo = exp_env["acc_repo"]

    cash_before = acc_repo.get_cash_account().current_balance
    exp_ac_before = acc_repo.get_expense_account().current_balance

    conn_opens = {"count": 0}
    orig_get_conn = db.get_connection

    def _counting_conn(*args, **kwargs):
        conn_opens["count"] += 1
        return orig_get_conn(*args, **kwargs)

    monkeypatch.setattr(db, "get_connection", _counting_conn)

    def _boom(*args, **kwargs):
        raise RuntimeError("Forced daily expense voucher failure")

    monkeypatch.setattr(acc_svc.accounting_repo, "create_voucher", _boom)

    with pytest.raises(RuntimeError, match="Forced daily expense voucher failure"):
        acc_svc.record_daily_expense(
            category_id=1,
            amount=999.0,
            expense_date="2026-11-17",
            payment_mode="CASH",
            remarks="Must roll back cleanly",
        )

    # Strictly 1 connection opened for the entire daily expense transaction
    assert conn_opens["count"] == 1
    assert db.fetch_one("SELECT COUNT(*) as c FROM expenses;")["c"] == 0
    assert db.fetch_one("SELECT COUNT(*) as c FROM vouchers;")["c"] == 0
    assert db.fetch_one("SELECT COUNT(*) as c FROM ledger_entries;")["c"] == 0
    assert acc_repo.get_cash_account().current_balance == pytest.approx(cash_before)
    assert acc_repo.get_expense_account().current_balance == pytest.approx(exp_ac_before)


# ---------------------------------------------------------------------------
# 9. Repeated executions create strictly 1 expense + 1 unique voucher per call
# ---------------------------------------------------------------------------

def test_repeated_daily_expense_executions_create_exact_one_to_one_rows_no_duplicates(exp_env):
    db = exp_env["db"]
    acc_svc = exp_env["acc_svc"]

    exp_ids = []
    for idx, (mode, amt) in enumerate([("CASH", 100.0), ("UPI", 200.0), ("BANK_TRANSFER", 300.0)], start=1):
        eid = acc_svc.record_daily_expense(
            category_id=idx,
            amount=amt,
            expense_date="2026-11-17",
            payment_mode=mode,
            remarks=f"Expense #{idx}",
        )
        exp_ids.append(eid)

    assert len(exp_ids) == 3
    assert len(set(exp_ids)) == 3
    assert db.fetch_one("SELECT COUNT(*) as c FROM expenses;")["c"] == 3

    vouchers = db.fetch_all(
        "SELECT voucher_no, reference_id, total_amount FROM vouchers WHERE reference_type = 'EXPENSE' ORDER BY voucher_id ASC;"
    )
    assert len(vouchers) == 3
    assert [v["reference_id"] for v in vouchers] == exp_ids
    assert len({v["voucher_no"] for v in vouchers}) == 3
    assert [float(v["total_amount"]) for v in vouchers] == [100.0, 200.0, 300.0]


# ---------------------------------------------------------------------------
# 10. Historical unlinked expense rows are preserved without auto-backfill
# ---------------------------------------------------------------------------

def test_historical_unlinked_expense_is_preserved_without_auto_backfill(exp_env):
    db = exp_env["db"]
    acc_repo = exp_env["acc_repo"]
    acc_svc = exp_env["acc_svc"]

    # Simulate a historical pre-Phase-2B-3 expense row with no voucher
    cash_ac = acc_repo.get_cash_account()
    hist_id = acc_repo.create_expense(
        Expense(
            category_id=1,
            expense_date="2026-10-01",
            amount=500.0,
            payment_account_id=cash_ac.account_id,
            remarks="Historical pre-fix expense",
        )
    )

    # Record a new daily expense after the fix
    new_id = acc_svc.record_daily_expense(
        category_id=2,
        amount=300.0,
        expense_date="2026-11-17",
        payment_mode="CASH",
        remarks="Post-fix daily expense",
    )

    # Historical expense remains unaltered and still has NO auto-generated voucher
    hist_vch = db.fetch_one(
        "SELECT * FROM vouchers WHERE reference_type = 'EXPENSE' AND reference_id = ?;",
        (hist_id,),
    )
    assert hist_vch is None

    # New expense has its linked voucher
    new_vch = db.fetch_one(
        "SELECT * FROM vouchers WHERE reference_type = 'EXPENSE' AND reference_id = ?;",
        (new_id,),
    )
    assert new_vch is not None
    assert new_vch["total_amount"] == pytest.approx(300.0)

    # Both expenses appear in expense listing & historical day cash drawer
    listed = acc_repo.get_expenses_list()
    assert {r["expense_id"] for r in listed} == {hist_id, new_id}
    hist_cd = acc_svc.get_cash_drawer_reconciliation("2026-10-01")
    assert hist_cd["cash_expenses"] == pytest.approx(500.0)
