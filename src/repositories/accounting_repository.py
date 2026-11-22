"""
Data Access Layer (Repository) for Chart of Accounts, Vouchers, and Expenses.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional
from src.models.accounting import Expense, ExpenseCategory, LedgerAccount, LedgerEntry, Voucher
from src.repositories.base_repository import BaseRepository


class AccountingRepository(BaseRepository):
    """Repository for double-entry financial accounts, vouchers, and expenses."""

    # ---------------- Chart of Accounts ----------------
    def create_account(self, account: LedgerAccount) -> int:
        sql = "INSERT INTO ledger_accounts (account_name, account_group, opening_balance, current_balance, is_system) VALUES (?, ?, ?, ?, ?);"
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                sql,
                (
                    account.account_name,
                    account.account_group,
                    account.opening_balance,
                    account.opening_balance,
                    account.is_system,
                ),
            )
            return cursor.lastrowid

    def get_all_accounts(self) -> List[LedgerAccount]:
        rows = self.db.fetch_all("SELECT * FROM ledger_accounts ORDER BY account_name ASC;")
        return [LedgerAccount(**dict(r)) for r in rows]

    def get_account_by_name(self, name: str, conn: Optional[sqlite3.Connection] = None) -> Optional[LedgerAccount]:
        if conn is not None:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ledger_accounts WHERE account_name = ?;", (name,))
            row = cursor.fetchone()
            return LedgerAccount(**dict(row)) if row else None
        row = self.db.fetch_one("SELECT * FROM ledger_accounts WHERE account_name = ?;", (name,))
        return LedgerAccount(**dict(row)) if row else None

    def get_account_by_id(self, account_id: int, conn: Optional[sqlite3.Connection] = None) -> Optional[LedgerAccount]:
        if conn is not None:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ledger_accounts WHERE account_id = ?;", (account_id,))
            row = cursor.fetchone()
            return LedgerAccount(**dict(row)) if row else None
        row = self.db.fetch_one("SELECT * FROM ledger_accounts WHERE account_id = ?;", (account_id,))
        return LedgerAccount(**dict(row)) if row else None

    def _fetch_all_accounts_helper(
        self, sql: str, params: tuple = (), conn: Optional[sqlite3.Connection] = None
    ) -> List[Any]:
        if conn is not None:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return cursor.fetchall()
        return self.db.fetch_all(sql, params)

    def get_debtors_account(self, conn: Optional[sqlite3.Connection] = None) -> LedgerAccount:
        """Resolve Sundry Debtors system account dynamically by name or fallback."""
        for name in ("Sundry Debtors", "Accounts Receivable"):
            ac = self.get_account_by_name(name, conn=conn)
            if ac:
                return ac
        rows = self._fetch_all_accounts_helper(
            "SELECT * FROM ledger_accounts WHERE account_group = 'ASSETS' AND is_system = 1;",
            conn=conn,
        )
        for r in rows:
            if "debtor" in r["account_name"].lower() or "receivable" in r["account_name"].lower():
                return LedgerAccount(**dict(r))
        raise ValueError("Sundry Debtors ledger account not configured.")

    def get_creditors_account(self, conn: Optional[sqlite3.Connection] = None) -> LedgerAccount:
        """Resolve Sundry Creditors system account dynamically by name or fallback."""
        for name in ("Sundry Creditors", "Accounts Payable"):
            ac = self.get_account_by_name(name, conn=conn)
            if ac:
                return ac
        rows = self._fetch_all_accounts_helper(
            "SELECT * FROM ledger_accounts WHERE account_group = 'LIABILITIES' AND is_system = 1;",
            conn=conn,
        )
        for r in rows:
            if "creditor" in r["account_name"].lower() or "payable" in r["account_name"].lower():
                return LedgerAccount(**dict(r))
        raise ValueError("Sundry Creditors ledger account not configured.")

    def get_cash_account(self, conn: Optional[sqlite3.Connection] = None) -> LedgerAccount:
        """Resolve Cash in Hand account dynamically."""
        ac = self.get_account_by_name("Cash in Hand", conn=conn)
        if ac:
            return ac
        rows = self._fetch_all_accounts_helper(
            "SELECT * FROM ledger_accounts WHERE account_group = 'CASH' AND is_system = 1;",
            conn=conn,
        )
        if rows:
            return LedgerAccount(**dict(rows[0]))
        raise ValueError("Cash in Hand ledger account not configured.")

    def get_bank_account(self, conn: Optional[sqlite3.Connection] = None) -> LedgerAccount:
        """Resolve Bank Account dynamically."""
        ac = self.get_account_by_name("Bank Account", conn=conn)
        if ac:
            return ac
        rows = self._fetch_all_accounts_helper(
            "SELECT * FROM ledger_accounts WHERE account_group = 'BANK_ACCOUNTS' AND is_system = 1;",
            conn=conn,
        )
        if rows:
            return LedgerAccount(**dict(rows[0]))
        raise ValueError("Bank ledger account not configured.")

    def get_sales_account(self, conn: Optional[sqlite3.Connection] = None) -> LedgerAccount:
        """Resolve Sales Account dynamically."""
        ac = self.get_account_by_name("Sales Account", conn=conn)
        if ac:
            return ac
        rows = self._fetch_all_accounts_helper(
            "SELECT * FROM ledger_accounts WHERE account_group = 'INCOME' AND is_system = 1;",
            conn=conn,
        )
        if rows:
            return LedgerAccount(**dict(rows[0]))
        raise ValueError("Sales ledger account not configured.")

    def get_sales_return_account(self, conn: Optional[sqlite3.Connection] = None) -> LedgerAccount:
        """Resolve Sales Return Account dynamically, falling back to Sales Account."""
        ac = self.get_account_by_name("Sales Return Account", conn=conn)
        if ac:
            return ac
        return self.get_sales_account(conn=conn)

    def get_purchase_account(self, conn: Optional[sqlite3.Connection] = None) -> LedgerAccount:
        """Resolve Purchase Account dynamically."""
        ac = self.get_account_by_name("Purchase Account", conn=conn)
        if ac:
            return ac
        rows = self._fetch_all_accounts_helper(
            "SELECT * FROM ledger_accounts WHERE account_group = 'EXPENSE' AND is_system = 1;",
            conn=conn,
        )
        if rows:
            return LedgerAccount(**dict(rows[0]))
        raise ValueError("Purchase ledger account not configured.")

    def get_purchase_return_account(
        self, conn: Optional[sqlite3.Connection] = None
    ) -> LedgerAccount:
        """Resolve Purchase Return Account dynamically."""
        ac = self.get_account_by_name("Purchase Return Account", conn=conn)
        if ac:
            return ac
        rows = self._fetch_all_accounts_helper(
            "SELECT * FROM ledger_accounts WHERE account_name LIKE '%Purchase Return%' LIMIT 1;",
            conn=conn,
        )
        if rows:
            return LedgerAccount(**dict(rows[0]))
        raise ValueError("Purchase Return ledger account not configured.")

    def get_supplier_credits_receivable_account(
        self, conn: Optional[sqlite3.Connection] = None
    ) -> LedgerAccount:
        """Resolve Supplier Credits Receivable Account dynamically."""
        ac = self.get_account_by_name("Supplier Credits Receivable", conn=conn)
        if ac:
            return ac
        rows = self._fetch_all_accounts_helper(
            "SELECT * FROM ledger_accounts WHERE account_name LIKE '%Supplier Credit%' LIMIT 1;",
            conn=conn,
        )
        if rows:
            return LedgerAccount(**dict(rows[0]))
        raise ValueError("Supplier Credits Receivable ledger account not configured.")

    def get_itc_suspense_account(
        self, conn: Optional[sqlite3.Connection] = None
    ) -> LedgerAccount:
        """Resolve ITC Reversal Suspense Account dynamically."""
        ac = self.get_account_by_name("ITC Reversal Suspense Account", conn=conn)
        if ac:
            return ac
        rows = self._fetch_all_accounts_helper(
            "SELECT * FROM ledger_accounts WHERE account_name LIKE '%ITC%Suspense%' LIMIT 1;",
            conn=conn,
        )
        if rows:
            return LedgerAccount(**dict(rows[0]))
        raise ValueError("ITC Reversal Suspense Account not configured.")

    def get_round_off_account(self, conn: Optional[sqlite3.Connection] = None) -> LedgerAccount:
        """Resolve Round Off Account dynamically."""
        ac = self.get_account_by_name("Round Off Account", conn=conn)
        if ac:
            return ac
        rows = self._fetch_all_accounts_helper(
            "SELECT * FROM ledger_accounts WHERE account_name LIKE '%Round Off%' LIMIT 1;",
            conn=conn,
        )
        if rows:
            return LedgerAccount(**dict(rows[0]))
        raise ValueError("Round Off ledger account not configured.")

    def get_output_gst_account(
        self, tax_type: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[LedgerAccount]:
        """Resolve CGST/SGST/IGST Output account."""
        name = f"{tax_type.upper()} Output Account"
        return self.get_account_by_name(name, conn=conn)

    def get_input_gst_account(
        self, tax_type: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[LedgerAccount]:
        """Resolve CGST/SGST/IGST Input account."""
        name = f"{tax_type.upper()} Input Account"
        return self.get_account_by_name(name, conn=conn)

    def get_expense_account(
        self, category_name: Optional[str] = None, conn: Optional[sqlite3.Connection] = None
    ) -> LedgerAccount:
        """Resolve expense account for a category, falling back to Operating Expenses or any expense account."""
        if category_name:
            ac = self.get_account_by_name(category_name, conn=conn)
            if ac:
                return ac
        for fallback in ("Operating Expenses", "Shop Operational Expenses"):
            ac = self.get_account_by_name(fallback, conn=conn)
            if ac:
                return ac
        rows = self._fetch_all_accounts_helper(
            "SELECT * FROM ledger_accounts WHERE account_group = 'EXPENSE' ORDER BY is_system DESC, account_id ASC;",
            conn=conn,
        )
        if rows:
            return LedgerAccount(**dict(rows[0]))
        raise ValueError("Operating Expenses ledger account not configured.")

    def update_account_balance(
        self, account_id: int, debit: float, credit: float, conn: Optional[sqlite3.Connection] = None
    ) -> None:
        """
        Update account balance based on account normal balance rules:
        Assets / Expenses: Normal Debit (Increase with Debit, Decrease with Credit)
        Liabilities / Income / Equity: Normal Credit (Increase with Credit, Decrease with Debit)
        """
        executor = conn if conn is not None else self.db.get_connection()
        try:
            cursor = executor.cursor()
            cursor.execute("SELECT account_group FROM ledger_accounts WHERE account_id = ?;", (account_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"Account ID {account_id} not found!")

            group = row["account_group"]
            if group in ("ASSETS", "EXPENSE", "BANK_ACCOUNTS", "CASH"):
                delta = debit - credit
            else:
                delta = credit - debit

            cursor.execute(
                "UPDATE ledger_accounts SET current_balance = current_balance + ? WHERE account_id = ?;",
                (delta, account_id),
            )
            if conn is None:
                executor.commit()
        finally:
            if conn is None:
                executor.close()

    # ---------------- Vouchers & Double Entry Posting ----------------
    def generate_next_voucher_no(
        self, voucher_type: str, conn: Optional[sqlite3.Connection] = None
    ) -> str:
        """Generate voucher number like RCP-0001, PAY-0001, JRN-0001."""
        prefix_map = {
            "RECEIPT": "RCP-",
            "PAYMENT": "PAY-",
            "CONTRA": "CTR-",
            "JOURNAL": "JRN-",
            "DEBIT_NOTE": "DN-",
            "CREDIT_NOTE": "CN-",
        }
        prefix = prefix_map.get(voucher_type, "VCH-")
        sql = "SELECT MAX(voucher_id) as last_id FROM vouchers;"
        if conn is not None:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                row = cursor.fetchone()
            finally:
                cursor.close()
        else:
            row = self.db.fetch_one(sql)
        last_id = row["last_id"] if row and row["last_id"] else 0
        return f"{prefix}{last_id + 1:05d}"

    def get_voucher_by_idempotency_key(
        self, idempotency_key: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Dict[str, Any]]:
        """Look up an existing voucher by its idempotency_key using the supplied connection if provided."""
        sql = """
            SELECT
                voucher_id,
                voucher_no,
                voucher_date,
                voucher_type,
                total_amount,
                narration,
                reference_type,
                reference_id,
                idempotency_key,
                request_fingerprint
            FROM vouchers
            WHERE idempotency_key = ?
            LIMIT 1;
        """
        if conn is not None:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, (idempotency_key,))
                row = cursor.fetchone()
                return dict(row) if row else None
            finally:
                cursor.close()
        row = self.db.fetch_one(sql, (idempotency_key,))
        return dict(row) if row else None

    def create_voucher(
        self,
        voucher: Voucher,
        conn: Optional[sqlite3.Connection] = None,
        *,
        idempotency_key: Optional[str] = None,
        request_fingerprint: Optional[str] = None,
    ) -> int:
        """Persist an accounting voucher and post all balanced ledger debit/credit entries."""
        # Validate that total debits == total credits
        total_debit = sum(e.debit_amount for e in voucher.entries)
        total_credit = sum(e.credit_amount for e in voucher.entries)
        if round(total_debit, 2) != round(total_credit, 2):
            raise ValueError(
                f"Unbalanced voucher entries: Total Debit ({total_debit}) != Total Credit ({total_credit})"
            )

        effective_key = idempotency_key if idempotency_key is not None else voucher.idempotency_key
        effective_fp = request_fingerprint if effective_key is not None else None

        sql_vch = """
            INSERT INTO vouchers (
                voucher_no, voucher_date, voucher_type, total_amount, narration,
                reference_type, reference_id, idempotency_key, request_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        sql_entry = """
            INSERT INTO ledger_entries (
                voucher_id, account_id, debit_amount, credit_amount, particulars
            ) VALUES (?, ?, ?, ?, ?);
        """

        executor = conn if conn is not None else self.db.get_connection()
        try:
            cursor = executor.cursor()
            cursor.execute(
                sql_vch,
                (
                    voucher.voucher_no,
                    voucher.voucher_date,
                    voucher.voucher_type,
                    voucher.total_amount,
                    voucher.narration,
                    voucher.reference_type,
                    voucher.reference_id,
                    effective_key,
                    effective_fp,
                ),
            )
            voucher_id = cursor.lastrowid

            for entry in voucher.entries:
                cursor.execute(
                    sql_entry,
                    (
                        voucher_id,
                        entry.account_id,
                        entry.debit_amount,
                        entry.credit_amount,
                        entry.particulars,
                    ),
                )
                # Update ledger account current balance
                self.update_account_balance(
                    account_id=entry.account_id,
                    debit=entry.debit_amount,
                    credit=entry.credit_amount,
                    conn=executor,
                )

            if conn is None:
                executor.commit()
            return voucher_id
        finally:
            if conn is None:
                executor.close()

    def get_account_ledger_statement(
        self, account_id: int, from_date: Optional[str] = None, to_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Fetch chronological ledger entries for a specific account."""
        sql = """
            SELECT 
                le.entry_id,
                v.voucher_no,
                v.voucher_date,
                v.voucher_type,
                le.particulars,
                le.debit_amount,
                le.credit_amount
            FROM ledger_entries le
            JOIN vouchers v ON le.voucher_id = v.voucher_id
            WHERE le.account_id = ?
              AND (? IS NULL OR v.voucher_date >= ?)
              AND (? IS NULL OR v.voucher_date <= ?)
            ORDER BY v.voucher_date ASC, v.voucher_id ASC;
        """
        rows = self.db.fetch_all(sql, (account_id, from_date, from_date, to_date, to_date))
        return [dict(r) for r in rows]

    # ---------------- Expenses ----------------
    def get_expense_by_idempotency_key(
        self, idempotency_key: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Look up an existing expense by its idempotency_key along with its linked EXPENSE voucher
        ID and voucher_no using the supplied connection if provided.
        """
        sql = """
            SELECT
                e.expense_id,
                e.category_id,
                e.expense_date,
                e.amount,
                e.payment_account_id,
                e.reference_no,
                e.remarks,
                e.idempotency_key,
                e.request_fingerprint,
                v.voucher_id,
                v.voucher_no
            FROM expenses e
            LEFT JOIN vouchers v
              ON v.voucher_id = (
                  SELECT v2.voucher_id
                  FROM vouchers v2
                  WHERE UPPER(COALESCE(v2.reference_type, '')) = 'EXPENSE'
                    AND v2.reference_id = e.expense_id
                  ORDER BY v2.voucher_id DESC
                  LIMIT 1
              )
            WHERE e.idempotency_key = ?
            LIMIT 1;
        """
        if conn is not None:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, (idempotency_key,))
                row = cursor.fetchone()
                return dict(row) if row else None
            finally:
                cursor.close()
        row = self.db.fetch_one(sql, (idempotency_key,))
        return dict(row) if row else None

    def create_expense(
        self,
        expense: Expense,
        conn: Optional[sqlite3.Connection] = None,
        *,
        idempotency_key: Optional[str] = None,
        request_fingerprint: Optional[str] = None,
    ) -> int:
        effective_key = idempotency_key if idempotency_key is not None else expense.idempotency_key
        effective_fp = request_fingerprint if effective_key is not None else None
        sql = """
            INSERT INTO expenses (
                category_id, expense_date, amount, payment_account_id, reference_no, remarks,
                idempotency_key, request_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            expense.category_id,
            expense.expense_date,
            expense.amount,
            expense.payment_account_id,
            expense.reference_no,
            expense.remarks,
            effective_key,
            effective_fp,
        )
        if conn is not None:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, params)
                return cursor.lastrowid
            finally:
                cursor.close()

        with self.db.transaction() as tx_conn:
            cursor = tx_conn.cursor()
            try:
                cursor.execute(sql, params)
                return cursor.lastrowid
            finally:
                cursor.close()

    def get_expenses_list(
        self, from_date: Optional[str] = None, to_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        sql = """
            SELECT 
                e.*,
                ec.category_name,
                la.account_name as payment_account_name
            FROM expenses e
            JOIN expense_categories ec ON e.category_id = ec.category_id
            LEFT JOIN ledger_accounts la ON e.payment_account_id = la.account_id
            WHERE (? IS NULL OR e.expense_date >= ?)
              AND (? IS NULL OR e.expense_date <= ?)
            ORDER BY e.expense_date DESC, e.expense_id DESC;
        """
        rows = self.db.fetch_all(sql, (from_date, from_date, to_date, to_date))
        return [dict(r) for r in rows]
