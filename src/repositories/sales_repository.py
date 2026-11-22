"""
Data Access Layer (Repository) for Sales Invoices and Sales Returns.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional
from src.models.sales import Sale, SaleItem, SalesReturn, SalesReturnItem
from src.repositories.base_repository import BaseRepository
from src.idempotency import DuplicateInvoiceError


class SalesRepository(BaseRepository):
    """Repository for managing sales billing, tax invoices, and sales returns."""

    def generate_next_invoice_no(
        self,
        prefix: str = "INV-",
        conn: Optional[sqlite3.Connection] = None,
    ) -> str:
        """Generate guaranteed unused sequential invoice number (e.g., INV-00001, INV-00002)."""
        executor = conn if conn is not None else self.db.get_connection()
        cursor: Optional[sqlite3.Cursor] = None
        try:
            cursor = executor.cursor()
            cursor.execute("SELECT invoice_no FROM sales WHERE invoice_no LIKE ?;", (f"{prefix}%",))
            rows = cursor.fetchall()
            max_num = 0
            for r in rows:
                inv = str(r["invoice_no"])
                num_str = inv[len(prefix):]
                if num_str.isdigit():
                    max_num = max(max_num, int(num_str))

            cursor.execute("SELECT MAX(sale_id) as last_id FROM sales;")
            row = cursor.fetchone()
            last_id = row["last_id"] if row and row["last_id"] else 0
            next_num = max(max_num + 1, last_id + 1, 1)

            while True:
                candidate = f"{prefix}{next_num:05d}"
                cursor.execute("SELECT 1 FROM sales WHERE invoice_no = ?;", (candidate,))
                if not cursor.fetchone():
                    return candidate
                next_num += 1
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is None:
                executor.close()

    def generate_next_return_no(
        self,
        prefix: str = "RET-",
        conn: Optional[sqlite3.Connection] = None,
    ) -> str:
        """Generate sequential sales return number."""
        executor = conn if conn is not None else self.db.get_connection()
        cursor: Optional[sqlite3.Cursor] = None
        try:
            cursor = executor.cursor()
            cursor.execute("SELECT return_no FROM sales_returns WHERE return_no LIKE ?;", (f"{prefix}%",))
            rows = cursor.fetchall()
            max_num = 0
            for r in rows:
                ret = str(r["return_no"])
                num_str = ret[len(prefix):]
                if num_str.isdigit():
                    max_num = max(max_num, int(num_str))

            cursor.execute("SELECT MAX(return_id) as last_id FROM sales_returns;")
            row = cursor.fetchone()
            last_id = row["last_id"] if row and row["last_id"] else 0
            next_num = max(max_num + 1, last_id + 1, 1)

            while True:
                candidate = f"{prefix}{next_num:05d}"
                cursor.execute("SELECT 1 FROM sales_returns WHERE return_no = ?;", (candidate,))
                if not cursor.fetchone():
                    return candidate
                next_num += 1
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is None:
                executor.close()

    def get_sale_by_idempotency_key(
        self, idempotency_key: str, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Dict[str, Any]]:
        """Look up an existing sale by its idempotency_key on the supplied or standalone connection."""
        sql = """
            SELECT sale_id, invoice_no, request_fingerprint
            FROM sales
            WHERE idempotency_key = ?;
        """
        executor = conn if conn is not None else self.db.get_connection()
        cursor: Optional[sqlite3.Cursor] = None
        try:
            cursor = executor.cursor()
            cursor.execute(sql, (idempotency_key,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is None:
                executor.close()

    def create_sale(
        self,
        sale: Sale,
        conn: Optional[sqlite3.Connection] = None,
        request_fingerprint: Optional[str] = None,
    ) -> int:
        """Persist sales invoice header and line items with leak-free connection lifecycle."""
        sql_head = """
            INSERT INTO sales (
                invoice_no, sale_date, customer_id, doctor_or_officer, payment_mode,
                total_taxable, total_cgst, total_sgst, total_igst, total_discount,
                extra_charges, round_off, net_amount, paid_amount, due_amount,
                print_count, remarks, idempotency_key, request_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        sql_item = """
            INSERT INTO sale_items (
                sale_id, product_id, batch_id, qty, unit_id,
                sale_rate, mrp, discount_percent, discount_amount, taxable_amount,
                cgst_rate, cgst_amount, sgst_rate, sgst_amount, igst_rate, igst_amount,
                total_amount
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """

        executor = conn if conn is not None else self.db.get_connection()
        cursor: Optional[sqlite3.Cursor] = None
        try:
            cursor = executor.cursor()

            # Blank/omitted invoice_no -> server generates sequential INV-xxxxx inside active tx.
            # Explicit colliding invoice_no -> reject loudly (never silently renumber).
            cleaned_inv = (sale.invoice_no or "").strip()
            if not cleaned_inv:
                sale.invoice_no = self.generate_next_invoice_no(conn=executor)
            else:
                sale.invoice_no = cleaned_inv
                cursor.execute("SELECT 1 FROM sales WHERE invoice_no = ?;", (sale.invoice_no,))
                if cursor.fetchone():
                    raise DuplicateInvoiceError(
                        f"Invoice number '{sale.invoice_no}' already exists."
                    )

            cursor.execute(
                sql_head,
                (
                    sale.invoice_no,
                    sale.sale_date,
                    sale.customer_id,
                    sale.doctor_or_officer,
                    sale.payment_mode,
                    sale.total_taxable,
                    sale.total_cgst,
                    sale.total_sgst,
                    sale.total_igst,
                    sale.total_discount,
                    sale.extra_charges,
                    sale.round_off,
                    sale.net_amount,
                    sale.paid_amount,
                    sale.due_amount,
                    sale.print_count,
                    sale.remarks,
                    sale.idempotency_key,
                    request_fingerprint if sale.idempotency_key else None,
                ),
            )
            sale_id = cursor.lastrowid

            for item in sale.items:
                cursor.execute(
                    sql_item,
                    (
                        sale_id,
                        item.product_id,
                        item.batch_id,
                        item.qty,
                        item.unit_id,
                        item.sale_rate,
                        item.mrp,
                        item.discount_percent,
                        item.discount_amount,
                        item.taxable_amount,
                        item.cgst_rate,
                        item.cgst_amount,
                        item.sgst_rate,
                        item.sgst_amount,
                        item.igst_rate,
                        item.igst_amount,
                        item.total_amount,
                    ),
                )

            if conn is None:
                executor.commit()
            return sale_id
        except Exception:
            if conn is None:
                try:
                    executor.rollback()
                except Exception:
                    pass
            raise
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is None:
                executor.close()

    def get_sale_by_id(
        self, sale_id: int, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Sale]:
        """Fetch complete sale invoice with customer details and line items."""
        sql_sale = """
            SELECT 
                s.*,
                c.customer_name,
                c.mobile as customer_mobile,
                c.village as customer_village,
                c.taluka as customer_taluka,
                c.district as customer_district,
                c.state as customer_state,
                c.gstin as customer_gstin
            FROM sales s
            JOIN customers c ON s.customer_id = c.customer_id
            WHERE s.sale_id = ?;
        """
        sql_items = """
            SELECT 
                si.*,
                p.product_name,
                p.hsn_code,
                sb.batch_no,
                sb.exp_date,
                u.symbol as unit_name
            FROM sale_items si
            JOIN products p ON si.product_id = p.product_id
            JOIN stock_batches sb ON si.batch_id = sb.batch_id
            LEFT JOIN units u ON COALESCE(si.unit_id, p.unit_id) = u.unit_id
            WHERE si.sale_id = ?
            ORDER BY si.sale_item_id ASC;
        """
        executor = conn if conn is not None else self.db.get_connection()
        cursor: Optional[sqlite3.Cursor] = None
        try:
            cursor = executor.cursor()
            cursor.execute(sql_sale, (sale_id,))
            row = cursor.fetchone()
            if not row:
                return None

            sale_dict = dict(row)
            sale_dict.pop("request_fingerprint", None)
            cursor.execute(sql_items, (sale_id,))
            item_rows = cursor.fetchall()
            sale_dict["items"] = [SaleItem(**dict(ir)) for ir in item_rows]
            return Sale(**sale_dict)
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is None:
                executor.close()

    def get_sales_list(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        customer_id: Optional[int] = None,
        payment_mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get list of sales invoices with customer summary."""
        sql = """
            SELECT 
                s.*,
                c.customer_name,
                c.village,
                c.mobile
            FROM sales s
            JOIN customers c ON s.customer_id = c.customer_id
            WHERE (? IS NULL OR s.sale_date >= ?)
              AND (? IS NULL OR s.sale_date <= ?)
              AND (? IS NULL OR s.customer_id = ?)
              AND (? IS NULL OR s.payment_mode = ?)
            ORDER BY s.sale_date DESC, s.sale_id DESC;
        """
        rows = self.db.fetch_all(
            sql,
            (from_date, from_date, to_date, to_date, customer_id, customer_id, payment_mode, payment_mode),
        )
        return [dict(r) for r in rows]

    def increment_print_count(self, sale_id: int) -> None:
        """Increment reprint count."""
        with self.db.transaction() as conn:
            conn.execute("UPDATE sales SET print_count = print_count + 1 WHERE sale_id = ?;", (sale_id,))

    def get_already_returned_qty_map(self, sale_id: int, conn: Optional[sqlite3.Connection] = None) -> Dict[int, float]:
        """Returns map of {product_id: total_returned_qty} for a specific sale_id."""
        executor = conn if conn is not None else self.db.get_connection()
        cursor: Optional[sqlite3.Cursor] = None
        try:
            cursor = executor.cursor()
            sql = """
                SELECT sri.product_id, COALESCE(SUM(sri.qty), 0) as total_returned
                FROM sales_return_items sri
                JOIN sales_returns sr ON sri.return_id = sr.return_id
                WHERE sr.sale_id = ?
                GROUP BY sri.product_id;
            """
            cursor.execute(sql, (sale_id,))
            rows = cursor.fetchall()
            return {int(r["product_id"]): float(r["total_returned"]) for r in rows}
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is None:
                executor.close()

    def get_already_returned_qty_by_batch_map(
        self, sale_id: int, conn: Optional[sqlite3.Connection] = None
    ) -> Dict[tuple, float]:
        """Returns map of {(product_id, batch_id): total_returned_qty} for a specific sale_id."""
        executor = conn if conn is not None else self.db.get_connection()
        cursor: Optional[sqlite3.Cursor] = None
        try:
            cursor = executor.cursor()
            sql = """
                SELECT sri.product_id, sri.batch_id, COALESCE(SUM(sri.qty), 0) as total_returned
                FROM sales_return_items sri
                JOIN sales_returns sr ON sri.return_id = sr.return_id
                WHERE sr.sale_id = ? AND sri.batch_id IS NOT NULL
                GROUP BY sri.product_id, sri.batch_id;
            """
            cursor.execute(sql, (sale_id,))
            rows = cursor.fetchall()
            return {
                (int(r["product_id"]), int(r["batch_id"])): float(r["total_returned"])
                for r in rows
            }
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is None:
                executor.close()

    def get_prior_returned_amount(
        self, sale_id: int, conn: Optional[sqlite3.Connection] = None
    ) -> float:
        """Returns cumulative net_amount of prior sales returns linked to a specific sale_id."""
        executor = conn if conn is not None else self.db.get_connection()
        cursor: Optional[sqlite3.Cursor] = None
        try:
            cursor = executor.cursor()
            cursor.execute(
                "SELECT COALESCE(SUM(net_amount), 0.0) as total_returned_amount FROM sales_returns WHERE sale_id = ?;",
                (sale_id,),
            )
            row = cursor.fetchone()
            return float(row["total_returned_amount"]) if row else 0.0
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is None:
                executor.close()

    def get_prior_refunded_amount(
        self, sale_id: int, conn: Optional[sqlite3.Connection] = None
    ) -> float:
        """
        Returns cumulative Cash/Bank refund amount already credited on SALE_RETURN vouchers
        linked to a specific sale_id.
        """
        sql = """
            SELECT COALESCE(SUM(le.credit_amount), 0.0) as total_refunded
            FROM sales_returns sr
            JOIN vouchers v ON v.reference_type = 'SALE_RETURN' AND v.reference_id = sr.return_id
            JOIN ledger_entries le ON le.voucher_id = v.voucher_id
            JOIN ledger_accounts la ON le.account_id = la.account_id
            WHERE sr.sale_id = ?
              AND la.account_group IN ('CASH', 'BANK_ACCOUNTS');
        """
        executor = conn if conn is not None else self.db.get_connection()
        cursor: Optional[sqlite3.Cursor] = None
        try:
            cursor = executor.cursor()
            cursor.execute(sql, (sale_id,))
            row = cursor.fetchone()
            return float(row["total_refunded"]) if row else 0.0
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is None:
                executor.close()

    def create_sale_return(self, ret: SalesReturn, conn: Optional[sqlite3.Connection] = None) -> int:
        """Persist sales return header and line items with leak-free connection lifecycle."""
        sql_head = """
            INSERT INTO sales_returns (
                return_no, return_date, sale_id, customer_id,
                total_taxable, total_tax, round_off, net_amount, remarks
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        sql_item = """
            INSERT INTO sales_return_items (
                return_id, product_id, batch_id, qty, sale_rate,
                taxable_amount, tax_amount, total_amount, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """

        executor = conn if conn is not None else self.db.get_connection()
        cursor: Optional[sqlite3.Cursor] = None
        try:
            cursor = executor.cursor()

            if not ret.return_no:
                ret.return_no = self.generate_next_return_no(conn=executor)
            else:
                cursor.execute("SELECT 1 FROM sales_returns WHERE return_no = ?;", (ret.return_no,))
                if cursor.fetchone():
                    ret.return_no = self.generate_next_return_no(conn=executor)

            cursor.execute(
                sql_head,
                (
                    ret.return_no,
                    ret.return_date,
                    ret.sale_id,
                    ret.customer_id,
                    ret.total_taxable,
                    ret.total_tax,
                    ret.round_off,
                    ret.net_amount,
                    ret.remarks,
                ),
            )
            return_id = cursor.lastrowid

            for item in ret.items:
                cursor.execute(
                    sql_item,
                    (
                        return_id,
                        item.product_id,
                        item.batch_id,
                        item.qty,
                        item.sale_rate,
                        item.taxable_amount,
                        item.tax_amount,
                        item.total_amount,
                        item.reason,
                    ),
                )

            if conn is None:
                executor.commit()
            return return_id
        except Exception:
            if conn is None:
                try:
                    executor.rollback()
                except Exception:
                    pass
            raise
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is None:
                executor.close()

    def get_sale_return_by_id(
        self, return_id: int, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch sale return header, items detail, and linked accounting voucher summary."""
        sql_head = """
            SELECT 
                sr.*,
                c.customer_name,
                c.mobile as customer_mobile,
                c.village as customer_village,
                s.invoice_no as original_invoice_no,
                s.sale_date as original_sale_date,
                s.payment_mode as original_payment_mode
            FROM sales_returns sr
            JOIN customers c ON sr.customer_id = c.customer_id
            LEFT JOIN sales s ON sr.sale_id = s.sale_id
            WHERE sr.return_id = ?;
        """
        sql_items = """
            SELECT 
                sri.*,
                p.product_name,
                p.hsn_code,
                sb.batch_no,
                sb.exp_date
            FROM sales_return_items sri
            JOIN products p ON sri.product_id = p.product_id
            LEFT JOIN stock_batches sb ON sri.batch_id = sb.batch_id
            WHERE sri.return_id = ?;
        """
        sql_vch = """
            SELECT voucher_id, voucher_no, voucher_type, voucher_date, total_amount, narration
            FROM vouchers
            WHERE reference_type = 'SALE_RETURN' AND reference_id = ?
            ORDER BY voucher_id DESC
            LIMIT 1;
        """
        sql_vch_entries = """
            SELECT la.account_name, la.account_group, le.debit_amount, le.credit_amount, le.particulars
            FROM ledger_entries le
            JOIN ledger_accounts la ON le.account_id = la.account_id
            WHERE le.voucher_id = ?;
        """

        executor = conn if conn is not None else self.db.get_connection()
        cursor: Optional[sqlite3.Cursor] = None
        try:
            cursor = executor.cursor()
            cursor.execute(sql_head, (return_id,))
            row = cursor.fetchone()
            if not row:
                return None

            ret_dict = dict(row)
            cursor.execute(sql_items, (return_id,))
            ret_dict["items"] = [dict(i) for i in cursor.fetchall()]

            cursor.execute(sql_vch, (return_id,))
            vch_row = cursor.fetchone()
            receivable_reduction = 0.0
            refund_amount = 0.0
            refund_account_name = None
            total_cgst = 0.0
            total_sgst = 0.0
            total_igst = 0.0

            if vch_row:
                ret_dict["voucher_id"] = int(vch_row["voucher_id"])
                ret_dict["voucher_no"] = vch_row["voucher_no"]
                cursor.execute(sql_vch_entries, (vch_row["voucher_id"],))
                for ent in cursor.fetchall():
                    acct_name = str(ent["account_name"] or "")
                    acct_grp = str(ent["account_group"] or "")
                    dr = float(ent["debit_amount"] or 0.0)
                    cr = float(ent["credit_amount"] or 0.0)
                    if acct_name == "Sundry Debtors" and cr > 0:
                        receivable_reduction = round(receivable_reduction + cr, 2)
                    elif acct_grp in ("CASH", "BANK_ACCOUNTS") and cr > 0:
                        refund_amount = round(refund_amount + cr, 2)
                        refund_account_name = acct_name
                    elif "CGST Output" in acct_name and dr > 0:
                        total_cgst = round(total_cgst + dr, 2)
                    elif "SGST Output" in acct_name and dr > 0:
                        total_sgst = round(total_sgst + dr, 2)
                    elif "IGST Output" in acct_name and dr > 0:
                        total_igst = round(total_igst + dr, 2)
            else:
                ret_dict["voucher_id"] = None
                ret_dict["voucher_no"] = None

            ret_dict["receivable_reduction"] = receivable_reduction
            ret_dict["refund_amount"] = refund_amount
            ret_dict["refund_account_name"] = refund_account_name
            ret_dict["total_cgst"] = total_cgst
            ret_dict["total_sgst"] = total_sgst
            ret_dict["total_igst"] = total_igst
            return ret_dict
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is None:
                executor.close()

    def get_sale_returns_list(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        customer_id: Optional[int] = None,
        sale_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch list of all past sale returns with optional filters and voucher reference."""
        sql = """
            SELECT 
                sr.*,
                c.customer_name,
                c.mobile as customer_mobile,
                s.invoice_no as original_invoice_no,
                s.payment_mode as original_payment_mode,
                (
                    SELECT v.voucher_no
                    FROM vouchers v
                    WHERE v.reference_type = 'SALE_RETURN' AND v.reference_id = sr.return_id
                    ORDER BY v.voucher_id DESC
                    LIMIT 1
                ) as voucher_no
            FROM sales_returns sr
            JOIN customers c ON sr.customer_id = c.customer_id
            LEFT JOIN sales s ON sr.sale_id = s.sale_id
            WHERE (? IS NULL OR sr.return_date >= ?)
              AND (? IS NULL OR sr.return_date <= ?)
              AND (? IS NULL OR sr.customer_id = ?)
              AND (? IS NULL OR sr.sale_id = ?)
            ORDER BY sr.return_date DESC, sr.return_id DESC;
        """
        rows = self.db.fetch_all(
            sql,
            (from_date, from_date, to_date, to_date, customer_id, customer_id, sale_id, sale_id),
        )
        return [dict(r) for r in rows]

