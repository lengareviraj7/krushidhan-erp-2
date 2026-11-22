"""
Data Access Layer (Repository) for Supplier Credit Notes.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from src.models.purchase_return import SupplierCredit, SupplierCreditCreate
from src.repositories.base_repository import BaseRepository


class SupplierCreditRepository(BaseRepository):
    """
    Repository for managing supplier credit notes arising from returns and invoice adjustments.
    """

    def _get_executor(self, conn: Optional[sqlite3.Connection]) -> Tuple[Any, bool]:
        """Returns (connection_or_cursor, is_external)."""
        if conn is not None:
            return conn, True
        return self.db.get_connection(), False

    # ------------------------------------------------------------------------
    # 1. Supplier Credit Creation (Caller Connection Mandatory)
    # ------------------------------------------------------------------------

    def create_credit(
        self,
        credit_data: SupplierCreditCreate,
        conn: sqlite3.Connection,
    ) -> int:
        """
        Creates a supplier credit record using the caller's SQLite connection.
        Enforces:
        - Originating purchase return must exist.
        - One-credit-per-return constraint.
        - Automatically links credit_id to any pending SUPPLIER_CREDIT settlement.
        Does NOT commit, roll back, or close the connection.
        """
        if conn is None:
            raise ValueError("A valid SQLite connection must be provided for create_credit.")

        cur = conn.cursor()

        # 1. Validate originating return exists and supplier matches
        cur.execute(
            "SELECT return_id, purchase_id, supplier_id, return_no, net_amount FROM purchase_returns WHERE return_id = ?;",
            (credit_data.return_id,),
        )
        ret_row = cur.fetchone()
        if not ret_row:
            raise ValueError(f"Originating purchase return #{credit_data.return_id} does not exist.")

        if ret_row["supplier_id"] != credit_data.supplier_id:
            raise ValueError(
                f"Supplier ID mismatch: credit supplier_id ({credit_data.supplier_id}) "
                f"does not match return supplier_id ({ret_row['supplier_id']})."
            )

        # Validate original purchase exists and matches supplier
        cur.execute(
            "SELECT purchase_id, supplier_id FROM purchases WHERE purchase_id = ?;",
            (ret_row["purchase_id"],),
        )
        purch_row = cur.fetchone()
        if not purch_row:
            raise ValueError(f"Original purchase invoice #{ret_row['purchase_id']} not found.")
        if purch_row["supplier_id"] != credit_data.supplier_id:
            raise ValueError(
                f"Supplier ID mismatch: credit supplier_id ({credit_data.supplier_id}) "
                f"does not match purchase supplier_id ({purch_row['supplier_id']})."
            )

        # 2. Enforce one-credit-per-return constraint
        cur.execute(
            "SELECT credit_id, credit_no FROM supplier_credits WHERE return_id = ?;",
            (credit_data.return_id,),
        )
        existing = cur.fetchone()
        if existing:
            raise ValueError(
                f"A supplier credit ({existing['credit_no']} [id={existing['credit_id']}]) "
                f"already exists for return #{credit_data.return_id}."
            )

        # 3. Validate amount consistency if a SUPPLIER_CREDIT settlement is already recorded
        cur.execute(
            """
            SELECT settlement_id, amount
            FROM purchase_return_settlements
            WHERE return_id = ? AND settlement_type = 'SUPPLIER_CREDIT';
            """,
            (credit_data.return_id,),
        )
        stl_row = cur.fetchone()
        if stl_row:
            if Decimal(str(stl_row["amount"])) != credit_data.original_amount:
                raise ValueError(
                    f"Supplier credit original_amount ({credit_data.original_amount}) must match the return's "
                    f"SUPPLIER_CREDIT settlement amount ({stl_row['amount']})."
                )

        # 3. Insert supplier credit
        balance = credit_data.balance_amount if credit_data.balance_amount is not None else credit_data.original_amount
        sql_insert = """
            INSERT INTO supplier_credits (
                credit_no, supplier_id, return_id, credit_date,
                original_amount, balance_amount, status, notes
            ) VALUES (?, ?, ?, ?, ?, ?, 'AVAILABLE', ?);
        """
        cur.execute(
            sql_insert,
            (
                credit_data.credit_no,
                credit_data.supplier_id,
                credit_data.return_id,
                credit_data.credit_date,
                float(credit_data.original_amount),
                float(balance),
                credit_data.notes,
            ),
        )
        credit_id = cur.lastrowid

        # 4. Link credit_id to matching SUPPLIER_CREDIT settlement record if present
        cur.execute(
            """
            UPDATE purchase_return_settlements
            SET credit_id = ?
            WHERE return_id = ?
              AND settlement_type = 'SUPPLIER_CREDIT'
              AND credit_id IS NULL;
            """,
            (credit_id, credit_data.return_id),
        )

        return credit_id

    def generate_next_credit_no(self, conn: Optional[sqlite3.Connection] = None) -> str:
        """Generate sequential credit note number like SCN-00001."""
        executor, is_external = self._get_executor(conn)
        try:
            cur = executor.cursor()
            cur.execute("SELECT MAX(credit_id) as last_id FROM supplier_credits;")
            row = cur.fetchone()
            last_id = row["last_id"] if row and row["last_id"] else 0
            return f"SCN-{last_id + 1:05d}"
        finally:
            if not is_external:
                executor.close()

    # ------------------------------------------------------------------------
    # 2. Retrieval & Query Operations
    # ------------------------------------------------------------------------

    def get_credit_by_id(
        self,
        credit_id: int,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Optional[SupplierCredit]:
        """Fetches a single supplier credit by ID."""
        sql = """
            SELECT 
                sc.*,
                s.supplier_name,
                pr.return_no
            FROM supplier_credits sc
            JOIN suppliers s ON sc.supplier_id = s.supplier_id
            JOIN purchase_returns pr ON sc.return_id = pr.return_id
            WHERE sc.credit_id = ?;
        """
        executor, is_external = self._get_executor(conn)
        try:
            cur = executor.cursor()
            cur.execute(sql, (credit_id,))
            row = cur.fetchone()
            if not row:
                return None
            return SupplierCredit(**dict(row))
        finally:
            if not is_external:
                executor.close()

    def get_credit_by_return_id(
        self,
        return_id: int,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Optional[SupplierCredit]:
        """Fetches the supplier credit originating from a specific purchase return."""
        sql = """
            SELECT 
                sc.*,
                s.supplier_name,
                pr.return_no
            FROM supplier_credits sc
            JOIN suppliers s ON sc.supplier_id = s.supplier_id
            JOIN purchase_returns pr ON sc.return_id = pr.return_id
            WHERE sc.return_id = ?;
        """
        executor, is_external = self._get_executor(conn)
        try:
            cur = executor.cursor()
            cur.execute(sql, (return_id,))
            row = cur.fetchone()
            if not row:
                return None
            return SupplierCredit(**dict(row))
        finally:
            if not is_external:
                executor.close()

    def list_credits(
        self,
        supplier_id: Optional[int] = None,
        status: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> List[SupplierCredit]:
        """Lists supplier credit notes filtered by supplier, status, and date range."""
        conditions = ["1=1"]
        params: List[Any] = []

        if supplier_id:
            conditions.append("sc.supplier_id = ?")
            params.append(supplier_id)
        if status:
            conditions.append("sc.status = ?")
            params.append(status)
        if from_date:
            conditions.append("sc.credit_date >= ?")
            params.append(from_date)
        if to_date:
            conditions.append("sc.credit_date <= ?")
            params.append(to_date)

        sql = f"""
            SELECT 
                sc.*,
                s.supplier_name,
                pr.return_no
            FROM supplier_credits sc
            JOIN suppliers s ON sc.supplier_id = s.supplier_id
            JOIN purchase_returns pr ON sc.return_id = pr.return_id
            WHERE {' AND '.join(conditions)}
            ORDER BY sc.credit_date DESC, sc.credit_id DESC;
        """

        executor, is_external = self._get_executor(conn)
        try:
            cur = executor.cursor()
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            return [SupplierCredit(**dict(r)) for r in rows]
        finally:
            if not is_external:
                executor.close()

    def get_credit_with_details(
        self,
        credit_id: int,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieves detailed credit information with joined supplier and return metadata.
        """
        sql = """
            SELECT 
                sc.credit_id,
                sc.credit_no,
                sc.credit_date,
                sc.original_amount,
                sc.balance_amount,
                sc.status,
                sc.notes,
                sc.created_at,
                s.supplier_id,
                s.supplier_name,
                s.gstin as supplier_gstin,
                s.mobile as supplier_mobile,
                pr.return_id,
                pr.return_no,
                pr.return_date,
                pr.net_amount as return_net_amount,
                pr.purchase_id,
                p.invoice_no as purchase_invoice_no,
                p.purchase_date as purchase_invoice_date
            FROM supplier_credits sc
            JOIN suppliers s ON sc.supplier_id = s.supplier_id
            JOIN purchase_returns pr ON sc.return_id = pr.return_id
            JOIN purchases p ON pr.purchase_id = p.purchase_id
            WHERE sc.credit_id = ?;
        """
        executor, is_external = self._get_executor(conn)
        try:
            cur = executor.cursor()
            cur.execute(sql, (credit_id,))
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)
        finally:
            if not is_external:
                executor.close()
