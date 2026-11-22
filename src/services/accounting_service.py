"""
Service Layer for Accounting Operations, Vouchers, Day Book, and Financial Statements.
"""
from __future__ import annotations

from datetime import datetime
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
from src.db.connection import DatabaseManager, get_db_manager
from src.idempotency import (
    IdempotencyConflictError,
    clean_str,
    decimal_to_paise,
    sha256_canonical_json,
    validate_optional_idempotency_key,
)
from src.models.accounting import Expense, LedgerEntry, Voucher
from src.repositories.accounting_repository import AccountingRepository
from src.repositories.master_data_repository import MasterDataRepository

_UNSET = object()


def _compute_customer_receipt_fingerprint(
    *,
    customer_id: int,
    raw_receipt_date: Optional[str],
    amount: float,
    payment_mode: str,
    narration: Optional[str],
) -> str:
    payload = {
        "operation": "CUSTOMER_RECEIPT",
        "customer_id": int(customer_id),
        "receipt_date": clean_str(raw_receipt_date),
        "amount_paise": decimal_to_paise(amount),
        "payment_mode": (clean_str(payment_mode) or "CASH").upper(),
        "narration": clean_str(narration),
    }
    return sha256_canonical_json(payload)


def _compute_supplier_payment_fingerprint(
    *,
    supplier_id: int,
    raw_payment_date: Optional[str],
    amount: float,
    payment_mode: str,
    reference_no: Optional[str],
    narration: Optional[str],
) -> str:
    payload = {
        "operation": "SUPPLIER_PAYMENT",
        "supplier_id": int(supplier_id),
        "payment_date": clean_str(raw_payment_date),
        "amount_paise": decimal_to_paise(amount),
        "payment_mode": (clean_str(payment_mode) or "BANK_TRANSFER").upper(),
        "reference_no": clean_str(reference_no),
        "narration": clean_str(narration),
    }
    return sha256_canonical_json(payload)


def _compute_shop_expense_fingerprint(
    *,
    category_id: int,
    amount: float,
    raw_expense_date: Optional[str],
    payment_mode: Optional[str],
    payment_account_id: Optional[int],
    reference_no: Optional[str],
    remarks: Optional[str],
) -> str:
    norm_mode = clean_str(payment_mode)
    payload = {
        "operation": "SHOP_EXPENSE",
        "category_id": int(category_id),
        "amount_paise": decimal_to_paise(amount),
        "expense_date": clean_str(raw_expense_date),
        "payment_mode": norm_mode.upper() if norm_mode else None,
        "payment_account_id": int(payment_account_id) if payment_account_id is not None else None,
        "reference_no": clean_str(reference_no),
        "remarks": clean_str(remarks),
    }
    return sha256_canonical_json(payload)


class AccountingService:
    """Business logic for financial vouchers, cash management, day book, and financial statements."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or get_db_manager()
        self.accounting_repo = AccountingRepository(self.db)
        self.master_repo = MasterDataRepository(self.db)

    def record_customer_receipt_with_status(
        self,
        customer_id: int,
        receipt_date: Optional[str] = None,
        amount: float = 0.0,
        payment_mode: str = "CASH",
        narration: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[int, str, bool]:
        """
        Record receipt from customer atomically with optional idempotency protection:
        1. Validate idempotency key and compute canonical fingerprint BEFORE defaulting receipt_date.
        2. Inside BEGIN IMMEDIATE, replay existing voucher on matching key/fingerprint or raise 409 on conflict.
        3. Verify customer exists before any writes.
        4. Reduce customer current_balance and post balanced RECEIPT voucher atomically.
        Returns (voucher_id, voucher_no, idempotent_replay).
        """
        validated_key = validate_optional_idempotency_key(idempotency_key)
        if amount <= 0:
            raise ValueError("Receipt amount must be greater than 0.")

        fingerprint = _compute_customer_receipt_fingerprint(
            customer_id=customer_id,
            raw_receipt_date=receipt_date,
            amount=amount,
            payment_mode=payment_mode,
            narration=narration,
        )

        try:
            with self.db.transaction(mode="IMMEDIATE") as conn:
                if validated_key is not None:
                    existing = self.accounting_repo.get_voucher_by_idempotency_key(validated_key, conn=conn)
                    if existing is not None:
                        if existing.get("request_fingerprint") == fingerprint:
                            return int(existing["voucher_id"]), str(existing["voucher_no"]), True
                        raise IdempotencyConflictError(
                            f"Idempotency key '{validated_key}' was already used with a different request payload."
                        )

                # Validate customer existence before any balance or voucher writes
                cursor = conn.cursor()
                try:
                    cursor.execute("SELECT customer_id FROM customers WHERE customer_id = ?;", (customer_id,))
                    cust_row = cursor.fetchone()
                finally:
                    cursor.close()
                if not cust_row:
                    raise ValueError(f"Customer ID {customer_id} not found.")

                final_date = clean_str(receipt_date) or datetime.now().strftime("%Y-%m-%d")

                cash_ac = self.accounting_repo.get_cash_account(conn=conn)
                bank_ac = self.accounting_repo.get_bank_account(conn=conn)
                debtors_ac = self.accounting_repo.get_debtors_account(conn=conn)
                pay_ac = cash_ac if (clean_str(payment_mode) or "CASH").upper() in ("CASH", "SPLIT") else bank_ac

                # Reduce customer balance
                self.master_repo.update_customer_balance(customer_id, -amount, conn=conn)

                vch_no = self.accounting_repo.generate_next_voucher_no("RECEIPT", conn=conn)
                entries = [
                    LedgerEntry(
                        account_id=pay_ac.account_id,
                        debit_amount=round(amount, 2),
                        credit_amount=0.0,
                        particulars=f"Receipt from Customer #{customer_id}",
                    ),
                    LedgerEntry(
                        account_id=debtors_ac.account_id,
                        debit_amount=0.0,
                        credit_amount=round(amount, 2),
                        particulars=f"Credit to Customer #{customer_id}",
                    ),
                ]
                voucher = Voucher(
                    voucher_no=vch_no,
                    voucher_date=final_date,
                    voucher_type="RECEIPT",
                    total_amount=round(amount, 2),
                    narration=narration or f"Payment received from customer #{customer_id}",
                    reference_type="MANUAL",
                    reference_id=customer_id,
                    idempotency_key=validated_key,
                    entries=entries,
                )
                if validated_key is not None:
                    vch_id = self.accounting_repo.create_voucher(
                        voucher,
                        conn=conn,
                        idempotency_key=validated_key,
                        request_fingerprint=fingerprint,
                    )
                else:
                    vch_id = self.accounting_repo.create_voucher(voucher, conn=conn)
                return vch_id, vch_no, False
        except sqlite3.IntegrityError:
            if validated_key is not None:
                existing = self.accounting_repo.get_voucher_by_idempotency_key(validated_key)
                if existing is not None:
                    if existing.get("request_fingerprint") == fingerprint:
                        return int(existing["voucher_id"]), str(existing["voucher_no"]), True
                    raise IdempotencyConflictError(
                        f"Idempotency key '{validated_key}' was already used with a different request payload."
                    )
            raise

    def record_customer_receipt(
        self,
        customer_id: int,
        receipt_date: Optional[str] = None,
        amount: float = 0.0,
        payment_mode: str = "CASH",
        narration: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> int:
        """Record receipt from customer and return voucher_id (backward-compatible wrapper)."""
        vch_id, _vch_no, _is_replay = self.record_customer_receipt_with_status(
            customer_id=customer_id,
            receipt_date=receipt_date,
            amount=amount,
            payment_mode=payment_mode,
            narration=narration,
            idempotency_key=idempotency_key,
        )
        return vch_id

    def record_supplier_payment_with_status(
        self,
        supplier_id: int,
        arg1: Any = None,
        arg2: Any = None,
        payment_mode: str = "BANK_TRANSFER",
        payment_date: Optional[str] = None,
        amount: Optional[float] = None,
        reference_no: Optional[str] = None,
        narration: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        **kwargs: Any,
    ) -> Tuple[int, str, bool]:
        """
        Record payment to supplier atomically with optional idempotency protection:
        1. Validate idempotency key and compute canonical fingerprint BEFORE defaulting payment_date.
        2. Inside BEGIN IMMEDIATE, replay existing voucher on match or raise IdempotencyConflictError on mismatch.
        3. Verify supplier exists, reduce supplier payable, and post balanced PAYMENT voucher atomically.
        Returns (voucher_id, voucher_no, idempotent_replay).
        """
        raw_key = idempotency_key if idempotency_key is not None else kwargs.get("idempotency_key")
        validated_key = validate_optional_idempotency_key(raw_key)

        # Resolve amount
        final_amount: Optional[float] = None
        if amount is not None:
            final_amount = float(amount)
        elif "amount" in kwargs:
            final_amount = float(kwargs["amount"])
        elif isinstance(arg1, (int, float)) and not isinstance(arg1, bool):
            final_amount = float(arg1)
        elif isinstance(arg2, (int, float)) and not isinstance(arg2, bool):
            final_amount = float(arg2)

        if final_amount is None or final_amount <= 0:
            raise ValueError("Payment amount must be greater than 0.")

        # Resolve raw payment_date (keep None when omitted so next-day replay does not false-conflict)
        raw_date: Optional[str] = None
        if payment_date is not None:
            raw_date = str(payment_date)
        elif "payment_date" in kwargs and kwargs["payment_date"] is not None:
            raw_date = str(kwargs["payment_date"])
        elif isinstance(arg1, str) and ("-" in arg1 or "/" in arg1):
            raw_date = arg1
        elif isinstance(arg2, str) and ("-" in arg2 or "/" in arg2):
            raw_date = arg2

        # Resolve payment_mode
        final_mode = kwargs.get("payment_mode", payment_mode)
        if isinstance(arg2, str) and not ("-" in arg2 or "/" in arg2):
            final_mode = arg2

        final_ref = kwargs.get("reference_no", reference_no)
        final_narration = kwargs.get("narration", narration)

        fingerprint = _compute_supplier_payment_fingerprint(
            supplier_id=supplier_id,
            raw_payment_date=raw_date,
            amount=final_amount,
            payment_mode=final_mode,
            reference_no=final_ref,
            narration=final_narration,
        )

        try:
            with self.db.transaction(mode="IMMEDIATE") as conn:
                if validated_key is not None:
                    existing = self.accounting_repo.get_voucher_by_idempotency_key(validated_key, conn=conn)
                    if existing is not None:
                        if existing.get("request_fingerprint") == fingerprint:
                            return int(existing["voucher_id"]), str(existing["voucher_no"]), True
                        raise IdempotencyConflictError(
                            f"Idempotency key '{validated_key}' was already used with a different request payload."
                        )

                cursor = conn.cursor()
                try:
                    cursor.execute("SELECT supplier_name FROM suppliers WHERE supplier_id = ?;", (supplier_id,))
                    row = cursor.fetchone()
                finally:
                    cursor.close()
                if not row:
                    raise ValueError(f"Supplier ID {supplier_id} not found.")
                supp_name = row["supplier_name"]

                final_date = clean_str(raw_date) or datetime.now().strftime("%Y-%m-%d")

                cash_ac = self.accounting_repo.get_cash_account(conn=conn)
                bank_ac = self.accounting_repo.get_bank_account(conn=conn)
                creditors_ac = self.accounting_repo.get_creditors_account(conn=conn)
                pay_ac = cash_ac if (clean_str(final_mode) or "BANK_TRANSFER").upper() in ("CASH", "SPLIT") else bank_ac

                # Reduce supplier payable balance
                self.master_repo.update_supplier_balance(supplier_id, -final_amount, conn=conn)

                vch_no = self.accounting_repo.generate_next_voucher_no("PAYMENT", conn=conn)
                entries = [
                    LedgerEntry(
                        account_id=creditors_ac.account_id,
                        debit_amount=round(final_amount, 2),
                        credit_amount=0.0,
                        particulars=f"Debit to Supplier #{supplier_id} ({supp_name})",
                    ),
                    LedgerEntry(
                        account_id=pay_ac.account_id,
                        debit_amount=0.0,
                        credit_amount=round(final_amount, 2),
                        particulars=f"Paid via {final_mode} ref: {final_ref or 'Direct'}",
                    ),
                ]
                voucher = Voucher(
                    voucher_no=vch_no,
                    voucher_date=final_date,
                    voucher_type="PAYMENT",
                    total_amount=round(final_amount, 2),
                    narration=final_narration or f"Payment to {supp_name} - Mode: {final_mode}",
                    reference_type="PURCHASE",
                    reference_id=supplier_id,
                    idempotency_key=validated_key,
                    entries=entries,
                )
                if validated_key is not None:
                    vch_id = self.accounting_repo.create_voucher(
                        voucher,
                        conn=conn,
                        idempotency_key=validated_key,
                        request_fingerprint=fingerprint,
                    )
                else:
                    vch_id = self.accounting_repo.create_voucher(voucher, conn=conn)
                return vch_id, vch_no, False
        except sqlite3.IntegrityError:
            if validated_key is not None:
                existing = self.accounting_repo.get_voucher_by_idempotency_key(validated_key)
                if existing is not None:
                    if existing.get("request_fingerprint") == fingerprint:
                        return int(existing["voucher_id"]), str(existing["voucher_no"]), True
                    raise IdempotencyConflictError(
                        f"Idempotency key '{validated_key}' was already used with a different request payload."
                    )
            raise

    def record_supplier_payment(
        self,
        supplier_id: int,
        arg1: Any = None,
        arg2: Any = None,
        payment_mode: str = "BANK_TRANSFER",
        payment_date: Optional[str] = None,
        amount: Optional[float] = None,
        reference_no: Optional[str] = None,
        narration: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        **kwargs: Any,
    ) -> int:
        """Record payment to supplier and return voucher_id (backward-compatible wrapper)."""
        vch_id, _vch_no, _is_replay = self.record_supplier_payment_with_status(
            supplier_id,
            arg1,
            arg2,
            payment_mode=payment_mode,
            payment_date=payment_date,
            amount=amount,
            reference_no=reference_no,
            narration=narration,
            idempotency_key=idempotency_key,
            **kwargs,
        )
        return vch_id

    def record_shop_expense_with_status(
        self,
        expense: Expense,
        payment_mode: Optional[str] = None,
        *,
        idempotency_key: Optional[str] = None,
        raw_expense_date: Any = _UNSET,
    ) -> Tuple[int, int, str, bool]:
        """
        Record operational expense and linked double-entry PAYMENT voucher atomically with idempotency.
        The `expenses` row is the single idempotency authority; the child EXPENSE voucher stores NULL key.
        Returns (expense_id, voucher_id, voucher_no, idempotent_replay).
        """
        raw_key = idempotency_key if idempotency_key is not None else expense.idempotency_key
        validated_key = validate_optional_idempotency_key(raw_key)

        if expense.amount <= 0:
            raise ValueError("Expense amount must be greater than 0.")

        fp_date = expense.expense_date if raw_expense_date is _UNSET else raw_expense_date
        raw_payment_account_id = expense.payment_account_id

        fingerprint = _compute_shop_expense_fingerprint(
            category_id=expense.category_id,
            amount=expense.amount,
            raw_expense_date=fp_date,
            payment_mode=payment_mode,
            payment_account_id=raw_payment_account_id,
            reference_no=expense.reference_no,
            remarks=expense.remarks,
        )

        try:
            with self.db.transaction(mode="IMMEDIATE") as conn:
                if validated_key is not None:
                    existing = self.accounting_repo.get_expense_by_idempotency_key(validated_key, conn=conn)
                    if existing is not None:
                        if existing.get("request_fingerprint") == fingerprint:
                            return (
                                int(existing["expense_id"]),
                                int(existing["voucher_id"]) if existing.get("voucher_id") is not None else 0,
                                str(existing["voucher_no"] or ""),
                                True,
                            )
                        raise IdempotencyConflictError(
                            f"Idempotency key '{validated_key}' was already used with a different request payload."
                        )

                cash_ac = self.accounting_repo.get_cash_account(conn=conn)
                bank_ac = self.accounting_repo.get_bank_account(conn=conn)

                if raw_payment_account_id is not None:
                    pay_ac_id = raw_payment_account_id
                elif payment_mode is not None:
                    mode_norm = payment_mode.strip().upper()
                    if mode_norm == "CASH":
                        pay_ac_id = cash_ac.account_id
                    elif mode_norm in ("UPI", "BANK_TRANSFER", "BANK", "CARD", "CHEQUE"):
                        pay_ac_id = bank_ac.account_id
                    else:
                        raise ValueError(
                            f"Unsupported payment mode '{payment_mode}' for daily expense. "
                            f"Supported modes: CASH, UPI, BANK_TRANSFER, BANK, CARD, CHEQUE."
                        )
                else:
                    pay_ac_id = cash_ac.account_id
                expense.payment_account_id = pay_ac_id

                final_date = clean_str(expense.expense_date) or datetime.now().strftime("%Y-%m-%d")
                expense.expense_date = final_date

                # Look up category name if category_id given
                cat_name = None
                if expense.category_id:
                    cursor = conn.cursor()
                    try:
                        cursor.execute(
                            "SELECT category_name FROM expense_categories WHERE category_id = ?;",
                            (expense.category_id,),
                        )
                        row = cursor.fetchone()
                        if row:
                            cat_name = row["category_name"]
                    finally:
                        cursor.close()

                expense_ac = self.accounting_repo.get_expense_account(cat_name, conn=conn)

                # Persist expense record with idempotency key & fingerprint (when key supplied)
                if validated_key is not None:
                    expense_id = self.accounting_repo.create_expense(
                        expense,
                        conn=conn,
                        idempotency_key=validated_key,
                        request_fingerprint=fingerprint,
                    )
                else:
                    expense_id = self.accounting_repo.create_expense(expense, conn=conn)

                # Post double-entry voucher for expense (child voucher MUST NOT carry idempotency_key)
                vch_no = self.accounting_repo.generate_next_voucher_no("PAYMENT", conn=conn)
                entries = [
                    LedgerEntry(
                        account_id=expense_ac.account_id,
                        debit_amount=round(expense.amount, 2),
                        credit_amount=0.0,
                        particulars=f"Expense: {expense.remarks or cat_name or 'Shop operational expense'}",
                    ),
                    LedgerEntry(
                        account_id=pay_ac_id,
                        debit_amount=0.0,
                        credit_amount=round(expense.amount, 2),
                        particulars="Cash/Bank paid for expense",
                    ),
                ]
                vch = Voucher(
                    voucher_no=vch_no,
                    voucher_date=final_date,
                    voucher_type="PAYMENT",
                    total_amount=round(expense.amount, 2),
                    narration=expense.remarks or f"Expense for {cat_name or 'Operations'}",
                    reference_type="EXPENSE",
                    reference_id=expense_id,
                    idempotency_key=None,
                    entries=entries,
                )
                voucher_id = self.accounting_repo.create_voucher(vch, conn=conn)
                return expense_id, voucher_id, vch_no, False
        except sqlite3.IntegrityError:
            if validated_key is not None:
                existing = self.accounting_repo.get_expense_by_idempotency_key(validated_key)
                if existing is not None:
                    if existing.get("request_fingerprint") == fingerprint:
                        return (
                            int(existing["expense_id"]),
                            int(existing["voucher_id"]) if existing.get("voucher_id") is not None else 0,
                            str(existing["voucher_no"] or ""),
                            True,
                        )
                    raise IdempotencyConflictError(
                        f"Idempotency key '{validated_key}' was already used with a different request payload."
                    )
            raise

    def record_shop_expense(
        self,
        expense: Expense,
        payment_mode: Optional[str] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> int:
        """Record operational expense and return expense_id (backward-compatible wrapper)."""
        exp_id, _vch_id, _vch_no, _is_replay = self.record_shop_expense_with_status(
            expense,
            payment_mode=payment_mode,
            idempotency_key=idempotency_key,
        )
        return exp_id

    def get_day_book(self, date_str: str) -> Dict[str, Any]:
        """Fetch complete Day Book of cash, bank, sales, purchases, and expenses for a given day."""
        sales = self.db.fetch_all("SELECT * FROM sales WHERE sale_date = ?;", (date_str,))
        purchases = self.db.fetch_all("SELECT * FROM purchases WHERE purchase_date = ?;", (date_str,))
        expenses = self.db.fetch_all("SELECT * FROM expenses WHERE expense_date = ?;", (date_str,))
        vouchers = self.db.fetch_all("SELECT * FROM vouchers WHERE voucher_date = ?;", (date_str,))

        total_sales = sum(s["net_amount"] for s in sales)
        total_purchases = sum(p["net_amount"] for p in purchases)
        total_expenses = sum(e["amount"] for e in expenses)

        return {
            "date": date_str,
            "sales_count": len(sales),
            "total_sales": total_sales,
            "purchases_count": len(purchases),
            "total_purchases": total_purchases,
            "expenses_count": len(expenses),
            "total_expenses": total_expenses,
            "vouchers": [dict(v) for v in vouchers],
        }

    def get_profit_and_loss(self, from_date: str, to_date: str) -> Dict[str, Any]:
        """
        Calculate Comprehensive Profit & Loss statement based on true batch-level COGS,
        category margin analysis, and operating expense itemization.
        """
        # 1. Total Sales Revenue (Taxable Turnover and Gross Sales)
        sql_sales = """
            SELECT 
                COALESCE(SUM(total_taxable), 0) as total_taxable,
                COALESCE(SUM(net_amount), 0) as gross_sales,
                COALESCE(SUM(total_discount), 0) as total_discount,
                COUNT(sale_id) as total_invoices
            FROM sales 
            WHERE sale_date BETWEEN ? AND ?;
        """
        sales_row = self.db.fetch_one(sql_sales, (from_date, to_date))
        sales_rev = float(sales_row["total_taxable"]) if sales_row else 0.0
        gross_sales = float(sales_row["gross_sales"]) if sales_row else 0.0
        total_discount = float(sales_row["total_discount"]) if sales_row else 0.0
        total_invoices = int(sales_row["total_invoices"]) if sales_row else 0

        # 2. True Cost of Goods Sold (COGS) based on batch purchase rates of items sold
        sql_cogs = """
            SELECT 
                COALESCE(SUM(si.qty * COALESCE(sb.purchase_rate, p.default_purchase_rate)), 0) as cogs
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN products p ON si.product_id = p.product_id
            LEFT JOIN stock_batches sb ON si.batch_id = sb.batch_id
            WHERE s.sale_date BETWEEN ? AND ?;
        """
        cogs_row = self.db.fetch_one(sql_cogs, (from_date, to_date))
        cogs = float(cogs_row["cogs"]) if cogs_row else 0.0

        # 3. Direct / Operating Expenses
        sql_expenses = """
            SELECT 
                COALESCE(ec.category_name, 'General Expenses') as category_name,
                COALESCE(SUM(e.amount), 0) as total_amount
            FROM expenses e
            LEFT JOIN expense_categories ec ON e.category_id = ec.category_id
            WHERE e.expense_date BETWEEN ? AND ?
            GROUP BY ec.category_name
            ORDER BY total_amount DESC;
        """
        expense_rows = self.db.fetch_all(sql_expenses, (from_date, to_date))
        expenses_breakdown = [dict(r) for r in expense_rows]
        total_expenses = sum(r["total_amount"] for r in expenses_breakdown)

        # 4. Gross Profit & Net Profit
        gross_profit = round(sales_rev - cogs, 2)
        gross_margin_pct = round((gross_profit / sales_rev * 100), 2) if sales_rev > 0 else 0.0
        net_profit = round(gross_profit - total_expenses, 2)
        net_margin_pct = round((net_profit / sales_rev * 100), 2) if sales_rev > 0 else 0.0

        # 5. Category-wise Breakdown
        sql_cat = """
            SELECT 
                COALESCE(c.category_name, 'General') as category_name,
                COALESCE(SUM(si.taxable_amount), 0) as sales_revenue,
                COALESCE(SUM(si.qty * COALESCE(sb.purchase_rate, p.default_purchase_rate)), 0) as cogs,
                COALESCE(SUM(si.taxable_amount) - SUM(si.qty * COALESCE(sb.purchase_rate, p.default_purchase_rate)), 0) as gross_profit
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN products p ON si.product_id = p.product_id
            LEFT JOIN categories c ON p.category_id = c.category_id
            LEFT JOIN stock_batches sb ON si.batch_id = sb.batch_id
            WHERE s.sale_date BETWEEN ? AND ?
            GROUP BY c.category_name
            ORDER BY gross_profit DESC;
        """
        cat_rows = self.db.fetch_all(sql_cat, (from_date, to_date))
        category_breakdown = []
        for r in cat_rows:
            cat_rev = float(r["sales_revenue"])
            cat_gp = float(r["gross_profit"])
            cat_margin = round((cat_gp / cat_rev * 100), 1) if cat_rev > 0 else 0.0
            category_breakdown.append({
                "category_name": r["category_name"],
                "sales_revenue": cat_rev,
                "cogs": float(r["cogs"]),
                "gross_profit": cat_gp,
                "margin_pct": cat_margin
            })

        # 6. Current Inventory Valuation (Closing Stock Value)
        sql_inv_val = """
            SELECT 
                COALESCE(SUM(current_qty * purchase_rate), 0) as total_valuation,
                COALESCE(SUM(current_qty), 0) as total_units
            FROM stock_batches
            WHERE current_qty > 0;
        """
        inv_val_row = self.db.fetch_one(sql_inv_val)
        closing_stock_value = float(inv_val_row["total_valuation"]) if inv_val_row else 0.0

        return {
            "from_date": from_date,
            "to_date": to_date,
            "sales_revenue": sales_rev,
            "gross_sales": gross_sales,
            "total_discount": total_discount,
            "total_invoices": total_invoices,
            "cogs": cogs,
            "gross_profit": gross_profit,
            "gross_margin_pct": gross_margin_pct,
            "total_expenses": total_expenses,
            "expenses_breakdown": expenses_breakdown,
            "net_profit": net_profit,
            "net_margin_pct": net_margin_pct,
            "category_breakdown": category_breakdown,
            "closing_stock_value": closing_stock_value,
        }

    def get_product_profitability(self, from_date: str, to_date: str, query: str = "") -> List[Dict[str, Any]]:
        """Fetch product-by-product sales volume, cost, profit, and margin percentage."""
        sql = """
            SELECT 
                p.product_id,
                p.product_name,
                COALESCE(c.category_name, 'General') as category_name,
                COALESCE(m.manufacturer_name, '-') as manufacturer_name,
                COALESCE(SUM(si.qty), 0) as qty_sold,
                u.symbol as unit_symbol,
                COALESCE(SUM(si.taxable_amount), 0) as sales_revenue,
                COALESCE(SUM(si.qty * COALESCE(sb.purchase_rate, p.default_purchase_rate)), 0) as cogs,
                COALESCE(SUM(si.taxable_amount) - SUM(si.qty * COALESCE(sb.purchase_rate, p.default_purchase_rate)), 0) as gross_profit
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN products p ON si.product_id = p.product_id
            LEFT JOIN categories c ON p.category_id = c.category_id
            LEFT JOIN manufacturers m ON p.manufacturer_id = m.manufacturer_id
            LEFT JOIN units u ON p.unit_id = u.unit_id
            LEFT JOIN stock_batches sb ON si.batch_id = sb.batch_id
            WHERE s.sale_date BETWEEN ? AND ?
              AND (p.product_name LIKE ? OR c.category_name LIKE ? OR m.manufacturer_name LIKE ?)
            GROUP BY p.product_id
            ORDER BY gross_profit DESC;
        """
        pattern = f"%{query}%"
        rows = self.db.fetch_all(sql, (from_date, to_date, pattern, pattern, pattern))
        results = []
        for r in rows:
            rev = float(r["sales_revenue"])
            gp = float(r["gross_profit"])
            margin = round((gp / rev * 100), 1) if rev > 0 else 0.0
            results.append({
                "product_id": r["product_id"],
                "product_name": r["product_name"],
                "category_name": r["category_name"],
                "manufacturer_name": r["manufacturer_name"],
                "qty_sold": float(r["qty_sold"]),
                "unit_symbol": r["unit_symbol"] or "Nos",
                "sales_revenue": rev,
                "cogs": float(r["cogs"]),
                "gross_profit": gp,
                "margin_pct": margin
            })
        return results

    # ==================== SUPPLIER & DISTRIBUTOR LEDGER ====================
    def get_suppliers_with_balances(self) -> List[Dict[str, Any]]:
        """Fetch all distributors/suppliers with total purchases, paid amounts, and outstanding payable balances."""
        sql = """
            SELECT 
                s.supplier_id,
                s.supplier_name,
                s.contact_person,
                s.mobile,
                s.city,
                s.gstin,
                s.current_balance as outstanding_payable,
                COALESCE(SUM(p.net_amount), 0) as total_purchases,
                COUNT(p.purchase_id) as bills_count
            FROM suppliers s
            LEFT JOIN purchases p ON s.supplier_id = p.supplier_id
            GROUP BY s.supplier_id
            ORDER BY s.current_balance DESC, s.supplier_name ASC;
        """
        rows = self.db.fetch_all(sql)
        return [dict(r) for r in rows]

    def get_supplier_statement(self, supplier_id: int) -> Dict[str, Any]:
        """Fetch supplier profile, list of purchase bills, and payment records."""
        supplier = self.db.fetch_one("SELECT * FROM suppliers WHERE supplier_id = ?;", (supplier_id,))
        if not supplier:
            raise ValueError(f"Supplier ID {supplier_id} not found.")

        # Inward Purchase Invoices
        purchases = self.db.fetch_all(
            "SELECT purchase_id, invoice_no, purchase_date, net_amount, paid_amount, due_amount, payment_type as payment_mode FROM purchases WHERE supplier_id = ? ORDER BY purchase_date DESC;",
            (supplier_id,),
        )

        # Payment Vouchers
        vouchers = self.db.fetch_all(
            """
            SELECT DISTINCT v.voucher_id, v.voucher_no, v.voucher_date, v.total_amount, v.narration, v.reference_type, v.reference_id
            FROM vouchers v
            JOIN ledger_entries le ON v.voucher_id = le.voucher_id
            WHERE v.voucher_type = 'PAYMENT' AND le.particulars LIKE ?
            ORDER BY v.voucher_date DESC;
            """,
            (f"%Supplier #{supplier_id}%",),
        )

        p_list = [dict(p) for p in purchases]
        v_list = [dict(v) for v in vouchers]
        ledger = []
        for p in p_list:
            ledger.append({
                "type": "PURCHASE",
                "date": p["purchase_date"],
                "ref_no": p["invoice_no"],
                "debit": 0.0,
                "credit": p["net_amount"],
                "paid": p["paid_amount"],
                "due": p["due_amount"],
            })
        for v in v_list:
            ledger.append({
                "type": "PAYMENT",
                "date": v["voucher_date"],
                "ref_no": v["voucher_no"],
                "debit": v["total_amount"],
                "credit": 0.0,
                "narration": v["narration"],
            })
        ledger.sort(key=lambda x: x["date"], reverse=True)

        return {
            "supplier": dict(supplier),
            "purchases": p_list,
            "payments": v_list,
            "ledger": ledger,
        }

    # ==================== DAILY EXPENSES & CASH CLOSING ====================
    def record_daily_expense_with_status(
        self,
        category_id: int,
        amount: float,
        expense_date: Optional[str] = None,
        payment_mode: str = "CASH",
        reference_no: Optional[str] = None,
        remarks: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[int, int, str, bool]:
        """
        Record an operational shop expense atomically with its balanced double-entry PAYMENT voucher
        and idempotency protection via record_shop_expense_with_status().
        Passes raw `expense_date` (None when omitted) so fingerprinting precedes date defaulting.
        Returns (expense_id, voucher_id, voucher_no, idempotent_replay).
        """
        if amount <= 0:
            raise ValueError("Expense amount must be greater than 0.")

        expense = Expense(
            category_id=category_id,
            expense_date=expense_date,
            amount=amount,
            payment_account_id=None,
            reference_no=reference_no,
            remarks=remarks,
            idempotency_key=idempotency_key,
        )
        return self.record_shop_expense_with_status(
            expense,
            payment_mode=payment_mode,
            idempotency_key=idempotency_key,
            raw_expense_date=expense_date,
        )

    def record_daily_expense(
        self,
        category_id: int,
        amount: float,
        expense_date: Optional[str] = None,
        payment_mode: str = "CASH",
        reference_no: Optional[str] = None,
        remarks: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> int:
        """
        Record an operational shop expense and return expense_id (backward-compatible wrapper).
        """
        exp_id, _vch_id, _vch_no, _is_replay = self.record_daily_expense_with_status(
            category_id=category_id,
            amount=amount,
            expense_date=expense_date,
            payment_mode=payment_mode,
            reference_no=reference_no,
            remarks=remarks,
            idempotency_key=idempotency_key,
        )
        return exp_id

    def get_expense_categories(self) -> List[Dict[str, Any]]:
        """Fetch all operational expense categories."""
        rows = self.db.fetch_all("SELECT * FROM expense_categories ORDER BY category_id ASC;")
        return [dict(r) for r in rows]

    def get_cash_drawer_reconciliation(self, date_str: str) -> Dict[str, Any]:
        """
        Calculates daily cash drawer breakdown:
        Opening Cash + Cash Sales + Farmer Cash Receipts - Cash Expenses - Cash Supplier Payments = Expected Drawer Cash.
        """
        # 1. Cash Sales on date
        cash_sales_row = self.db.fetch_one(
            "SELECT COALESCE(SUM(paid_amount), 0) as total FROM sales WHERE sale_date = ? AND payment_mode IN ('CASH', 'SPLIT');",
            (date_str,),
        )
        cash_sales = float(cash_sales_row["total"]) if cash_sales_row else 0.0

        # 2. Farmer Receipts in Cash (Debit Cash in Hand Account 1)
        farmer_receipts_row = self.db.fetch_one(
            """
            SELECT COALESCE(SUM(DISTINCT v.total_amount), 0) as total 
            FROM vouchers v 
            JOIN ledger_entries le ON v.voucher_id = le.voucher_id
            WHERE v.voucher_date = ? AND v.voucher_type = 'RECEIPT' AND le.account_id = 1 AND le.debit_amount > 0;
            """,
            (date_str,),
        )
        farmer_receipts = float(farmer_receipts_row["total"]) if farmer_receipts_row else 0.0

        # 3. Cash Expenses
        cash_exp_row = self.db.fetch_one(
            """
            SELECT COALESCE(SUM(amount), 0) as total 
            FROM expenses 
            WHERE expense_date = ? AND payment_account_id = 1;
            """,
            (date_str,),
        )
        cash_expenses = float(cash_exp_row["total"]) if cash_exp_row else 0.0

        # 4. Cash Supplier Payments (Credit Cash in Hand Account 1; exclude EXPENSE vouchers already counted above)
        cash_supp_row = self.db.fetch_one(
            """
            SELECT COALESCE(SUM(DISTINCT v.total_amount), 0) as total 
            FROM vouchers v 
            JOIN ledger_entries le ON v.voucher_id = le.voucher_id
            WHERE v.voucher_date = ?
              AND v.voucher_type = 'PAYMENT'
              AND UPPER(COALESCE(v.reference_type, '')) != 'EXPENSE'
              AND le.account_id = 1
              AND le.credit_amount > 0;
            """,
            (date_str,),
        )
        cash_supp_payments = float(cash_supp_row["total"]) if cash_supp_row else 0.0

        # Opening balance constant or estimate
        opening_cash = 5000.0  # standard base float
        net_inflow = (cash_sales + farmer_receipts) - (cash_expenses + cash_supp_payments)
        expected_cash = opening_cash + net_inflow

        return {
            "date": date_str,
            "opening_cash": opening_cash,
            "cash_sales": cash_sales,
            "farmer_cash_receipts": farmer_receipts,
            "total_cash_inflow": cash_sales + farmer_receipts,
            "cash_expenses": cash_expenses,
            "cash_supplier_payments": cash_supp_payments,
            "total_cash_outflow": cash_expenses + cash_supp_payments,
            "net_cash_flow": net_inflow,
            "expected_drawer_cash": max(0.0, expected_cash),
            "expected_cash_in_hand": max(0.0, expected_cash),
        }

