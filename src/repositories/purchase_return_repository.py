"""
Data Access Layer (Repository) for Purchase Returns, Debit Notes, and Settlement Allocations.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple, Union

from src.models.purchase_return import (
    PurchaseReturn,
    PurchaseReturnCreate,
    PurchaseReturnItem,
    PurchaseReturnItemCreate,
    PurchaseReturnSettlement,
    PurchaseReturnSettlementCreate,
)
from src.repositories.base_repository import BaseRepository


class PurchaseReturnRepository(BaseRepository):
    """
    Repository for managing purchase return transactions, original purchase line queries,
    and multi-settlement allocations.
    """

    def _get_executor(self, conn: Optional[sqlite3.Connection]) -> Tuple[Any, bool]:
        """Returns (connection_or_cursor, is_external)."""
        if conn is not None:
            return conn, True
        return self.db.get_connection(), False

    # ------------------------------------------------------------------------
    # 1. Original Purchase & Eligible Line Queries
    # ------------------------------------------------------------------------

    def get_original_purchase(
        self,
        purchase_id: int,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetches an original purchase header and all its line items with supplier metadata.
        """
        sql_head = """
            SELECT 
                p.*,
                s.supplier_name,
                s.gstin as supplier_gstin,
                s.state as supplier_state
            FROM purchases p
            JOIN suppliers s ON p.supplier_id = s.supplier_id
            WHERE p.purchase_id = ?;
        """
        sql_items = """
            SELECT 
                pi.*,
                pr.product_name,
                pr.hsn_code
            FROM purchase_items pi
            JOIN products pr ON pi.product_id = pr.product_id
            WHERE pi.purchase_id = ?
            ORDER BY pi.item_id ASC;
        """

        executor, is_external = self._get_executor(conn)
        try:
            cur = executor.cursor()
            cur.execute(sql_head, (purchase_id,))
            row = cur.fetchone()
            if not row:
                return None

            purchase_dict = dict(row)
            cur.execute(sql_items, (purchase_id,))
            item_rows = cur.fetchall()
            purchase_dict["items"] = [dict(ir) for ir in item_rows]
            return purchase_dict
        finally:
            if not is_external:
                executor.close()

    def get_eligible_purchase_lines(
        self,
        purchase_id: int,
        conn: Optional[sqlite3.Connection] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves purchase lines for an invoice with remaining returnable quantities,
        deducting previous non-cancelled returns and joining stock batch current_qty.
        """
        sql = """
            SELECT 
                pi.item_id,
                pi.purchase_id,
                pi.product_id,
                p.product_name,
                p.hsn_code,
                pi.batch_no,
                pi.mfg_date,
                pi.exp_date,
                pi.qty as purchased_qty,
                pi.free_qty,
                pi.unit_id,
                pi.purchase_rate,
                pi.discount_percent,
                pi.discount_amount,
                pi.taxable_amount,
                pi.cgst_rate,
                pi.cgst_amount,
                pi.sgst_rate,
                pi.sgst_amount,
                pi.igst_rate,
                pi.igst_amount,
                pi.total_amount,
                COALESCE(sb.batch_id, NULL) as batch_id,
                COALESCE(sb.current_qty, 0.0) as current_stock_qty,
                COALESCE((
                    SELECT SUM(pri.qty)
                    FROM purchase_return_items pri
                    JOIN purchase_returns pr ON pri.return_id = pr.return_id
                    WHERE pri.purchase_item_id = pi.item_id
                      AND (pr.status IS NULL OR pr.status != 'CANCELLED')
                ), 0.0) as prior_returned_qty
            FROM purchase_items pi
            JOIN products p ON pi.product_id = p.product_id
            LEFT JOIN stock_batches sb 
                   ON sb.product_id = pi.product_id 
                  AND sb.batch_no = pi.batch_no
            WHERE pi.purchase_id = ?
            ORDER BY pi.item_id ASC;
        """

        executor, is_external = self._get_executor(conn)
        try:
            cur = executor.cursor()
            cur.execute(sql, (purchase_id,))
            rows = cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                purchased_qty = float(d["purchased_qty"] or 0.0)
                prior_returned_qty = float(d["prior_returned_qty"] or 0.0)
                current_stock_qty = float(d["current_stock_qty"] or 0.0)
                remaining_purchased_qty = max(0.0, purchased_qty - prior_returned_qty)
                # Bounded by both remaining invoice quantity and warehouse batch stock
                max_returnable_qty = min(remaining_purchased_qty, current_stock_qty)

                d["remaining_purchased_qty"] = remaining_purchased_qty
                d["max_returnable_qty"] = max_returnable_qty
                result.append(d)
            return result
        finally:
            if not is_external:
                executor.close()

    def get_prior_linked_returns(
        self,
        purchase_id: int,
        conn: Optional[sqlite3.Connection] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves all prior return records and line items associated with a purchase.
        """
        sql = """
            SELECT 
                pri.return_item_id,
                pri.return_id,
                pr.return_no,
                pr.return_date,
                pr.status,
                pr.itc_status,
                pri.purchase_item_id,
                pri.product_id,
                p.product_name,
                pri.batch_id,
                sb.batch_no,
                pri.qty,
                pri.purchase_rate,
                pri.net_rate,
                pri.taxable_amount,
                pri.total_amount,
                pri.reason
            FROM purchase_return_items pri
            JOIN purchase_returns pr ON pri.return_id = pr.return_id
            JOIN products p ON pri.product_id = p.product_id
            LEFT JOIN stock_batches sb ON pri.batch_id = sb.batch_id
            WHERE pr.purchase_id = ?
            ORDER BY pr.return_date DESC, pr.return_id DESC, pri.return_item_id ASC;
        """

        executor, is_external = self._get_executor(conn)
        try:
            cur = executor.cursor()
            cur.execute(sql, (purchase_id,))
            return [dict(r) for r in cur.fetchall()]
        finally:
            if not is_external:
                executor.close()

    def detect_historical_unlinked_returns(
        self,
        purchase_id: int,
        conn: Optional[sqlite3.Connection] = None,
    ) -> List[Dict[str, Any]]:
        """
        Detects historical return line rows whose purchase_item_id is NULL.
        Ensures operators and services never silently ignore potentially relevant historical returns.
        """
        sql = """
            SELECT 
                pri.return_item_id,
                pri.return_id,
                pr.return_no,
                pr.return_date,
                pr.status,
                pri.product_id,
                p.product_name,
                pri.batch_id,
                sb.batch_no,
                pri.qty,
                pri.total_amount,
                pri.reason
            FROM purchase_return_items pri
            JOIN purchase_returns pr ON pri.return_id = pr.return_id
            JOIN products p ON pri.product_id = p.product_id
            LEFT JOIN stock_batches sb ON pri.batch_id = sb.batch_id
            WHERE pr.purchase_id = ?
              AND pri.purchase_item_id IS NULL
              AND (pr.status IS NULL OR pr.status != 'CANCELLED')
            ORDER BY pri.return_item_id ASC;
        """

        executor, is_external = self._get_executor(conn)
        try:
            cur = executor.cursor()
            cur.execute(sql, (purchase_id,))
            return [dict(r) for r in cur.fetchall()]
        finally:
            if not is_external:
                executor.close()

    # ------------------------------------------------------------------------
    # 2. Return Creation & Persistence (Caller-Supplied Connection Mandatory)
    # ------------------------------------------------------------------------

    def calculate_return_breakdown(
        self,
        purchase_id: int,
        items: List[Any],
        conn: Optional[sqlite3.Connection] = None,
        supplier_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Authoritative calculation of return line taxes, discounts, cumulative remainders,
        and header totals without persisting any records.
        Reused identically by both preview endpoint and return persistence.
        """
        if not items:
            raise ValueError("Purchase return must contain at least one line item.")

        item_ids = [
            itm.purchase_item_id if hasattr(itm, "purchase_item_id") else itm["purchase_item_id"]
            for itm in items
        ]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Duplicate purchase item found in return items list: multiple entries for the same purchase_item_id are not allowed.")

        executor, is_external = self._get_executor(conn)
        try:
            cur = executor.cursor()

            # 1. Validate original purchase exists and verify supplier match
            cur.execute(
                "SELECT purchase_id, supplier_id, invoice_no FROM purchases WHERE purchase_id = ?;",
                (purchase_id,),
            )
            purch_row = cur.fetchone()
            if not purch_row:
                raise ValueError(f"Original purchase invoice #{purchase_id} does not exist.")

            if supplier_id is not None and purch_row["supplier_id"] != supplier_id:
                raise ValueError(
                    f"Supplier ID mismatch: return supplier_id ({supplier_id}) "
                    f"does not match purchase supplier_id ({purch_row['supplier_id']})."
                )

            # Query supplier balance
            cur.execute(
                "SELECT supplier_name, current_balance FROM suppliers WHERE supplier_id = ?;",
                (purch_row["supplier_id"],),
            )
            supp_row = cur.fetchone()
            supplier_name = supp_row["supplier_name"] if supp_row else None
            supplier_balance = Decimal(str(supp_row["current_balance"] or 0.0)) if supp_row else Decimal("0.00")

            # 2. Check for unlinked historical returns
            cur.execute(
                """
                SELECT COUNT(*) as unlinked_count
                FROM purchase_return_items pri
                JOIN purchase_returns pr ON pri.return_id = pr.return_id
                WHERE pr.purchase_id = ?
                  AND pri.purchase_item_id IS NULL
                  AND (pr.status IS NULL OR pr.status != 'CANCELLED');
                """,
                (purchase_id,),
            )
            if cur.fetchone()["unlinked_count"] > 0:
                raise ValueError(
                    f"Cannot process return for purchase #{purchase_id}: "
                    f"historical returns exist with unlinked line items (purchase_item_id is NULL)."
                )

            # 3. Process and calculate line items against original purchase_items
            cur.execute(
                """
                SELECT pi.*, p.product_name
                FROM purchase_items pi
                JOIN products p ON pi.product_id = p.product_id
                WHERE pi.purchase_id = ?;
                """,
                (purchase_id,),
            )
            pi_map = {r["item_id"]: dict(r) for r in cur.fetchall()}

            calculated_items: List[Dict[str, Any]] = []
            sum_taxable = Decimal("0.00")
            sum_cgst = Decimal("0.00")
            sum_sgst = Decimal("0.00")
            sum_igst = Decimal("0.00")

            for item in items:
                p_item_id = item.purchase_item_id if hasattr(item, "purchase_item_id") else item["purchase_item_id"]
                p_qty = Decimal(str(item.qty if hasattr(item, "qty") else item["qty"]))
                p_prod_id = item.product_id if hasattr(item, "product_id") else item.get("product_id")
                p_batch_id = item.batch_id if hasattr(item, "batch_id") else item.get("batch_id")
                p_reason = item.reason if hasattr(item, "reason") else item.get("reason")

                pi = pi_map.get(p_item_id)
                if not pi:
                    raise ValueError(
                        f"purchase_item_id #{p_item_id} does not belong to purchase #{purchase_id}."
                    )

                if p_prod_id is not None and pi["product_id"] != p_prod_id:
                    raise ValueError(
                        f"Product ID mismatch on line #{p_item_id}: "
                        f"item product_id={p_prod_id} but purchase_items product_id={pi['product_id']}."
                    )

                if p_qty <= Decimal("0"):
                    raise ValueError(f"Return quantity must be strictly greater than 0 for item #{p_item_id}.")

                purchased_qty = Decimal(str(pi["qty"]))
                if p_qty > purchased_qty:
                    raise ValueError(
                        f"Return quantity ({p_qty}) exceeds original purchased quantity ({purchased_qty}) "
                        f"for item #{p_item_id}."
                    )

                # Check free quantity on purchase line
                raw_free_qty = pi.get("free_qty")
                free_qty = Decimal(str(raw_free_qty)) if raw_free_qty is not None else Decimal("0.0000")
                if free_qty > Decimal("0"):
                    raise ValueError(
                        f"Purchase line #{p_item_id} contains free/bonus quantity ({free_qty}). "
                        f"Returns on items with free quantities are not supported in this release."
                    )

                # Resolve authoritative batch identity from original purchase line
                cur.execute(
                    """
                    SELECT batch_id, product_id, batch_no, current_qty
                    FROM stock_batches
                    WHERE product_id = ? AND batch_no = ?;
                    """,
                    (pi["product_id"], pi["batch_no"]),
                )
                expected_batch = cur.fetchone()
                if not expected_batch:
                    raise ValueError(
                        f"No stock batch found for product #{pi['product_id']} and batch '{pi['batch_no']}' "
                        f"originating from purchase line #{p_item_id}."
                    )
                expected_batch_id = int(expected_batch["batch_id"])

                if p_batch_id is not None and p_batch_id != expected_batch_id:
                    cur.execute(
                        "SELECT product_id, batch_no FROM stock_batches WHERE batch_id = ?;",
                        (p_batch_id,),
                    )
                    supplied_sb = cur.fetchone()
                    if not supplied_sb:
                        raise ValueError(
                            f"Client-supplied batch_id #{p_batch_id} does not exist in stock batches."
                        )
                    if supplied_sb["product_id"] != pi["product_id"]:
                        raise ValueError(
                            f"Client-supplied batch_id #{p_batch_id} belongs to product #{supplied_sb['product_id']}, "
                            f"not the returned product #{pi['product_id']}."
                        )
                    raise ValueError(
                        f"Batch mismatch on line #{p_item_id}: client supplied batch_id #{p_batch_id} "
                        f"('{supplied_sb['batch_no']}'), but original purchase line is for batch '{pi['batch_no']}' "
                        f"(expected batch_id #{expected_batch_id})."
                    )

                # Check physical batch stock availability
                avail_batch_stock = Decimal(str(expected_batch["current_qty"]))
                if avail_batch_stock < p_qty:
                    raise ValueError(
                        f"Insufficient physical stock in batch '{expected_batch['batch_no']}': "
                        f"available {avail_batch_stock}, requested return {p_qty}."
                    )

                # Query prior returned quantity and cumulative amounts
                cur.execute(
                    """
                    SELECT 
                        COALESCE(SUM(pri.qty), 0.0) as total_returned,
                        COALESCE(SUM(pri.taxable_amount), 0.0) as cum_taxable,
                        COALESCE(SUM(pri.cgst_amount), 0.0) as cum_cgst,
                        COALESCE(SUM(pri.sgst_amount), 0.0) as cum_sgst,
                        COALESCE(SUM(pri.igst_amount), 0.0) as cum_igst,
                        COALESCE(SUM(pri.discount_amount), 0.0) as cum_disc,
                        COALESCE(SUM(pri.total_amount), 0.0) as cum_total
                    FROM purchase_return_items pri
                    JOIN purchase_returns pr ON pri.return_id = pr.return_id
                    WHERE pri.purchase_item_id = ?
                      AND (pr.status IS NULL OR pr.status != 'CANCELLED');
                    """,
                    (p_item_id,),
                )
                prior_row = cur.fetchone()
                prior_ret = Decimal(str(prior_row["total_returned"]))
                if prior_ret + p_qty > purchased_qty:
                    raise ValueError(
                        f"Cumulative return quantity ({prior_ret + p_qty}) exceeds original purchased quantity "
                        f"({purchased_qty}) for item #{p_item_id}."
                    )

                orig_qty = purchased_qty
                orig_rate = Decimal(str(pi["purchase_rate"]))
                orig_disc = Decimal(str(pi["discount_amount"] or 0.0))
                orig_taxable = Decimal(str(pi["taxable_amount"]))
                orig_cgst = Decimal(str(pi["cgst_amount"] or 0.0))
                orig_sgst = Decimal(str(pi["sgst_amount"] or 0.0))
                orig_igst = Decimal(str(pi["igst_amount"] or 0.0))
                orig_total = Decimal(str(pi["total_amount"]))
                cgst_rate = Decimal(str(pi["cgst_rate"] or 0.0))
                sgst_rate = Decimal(str(pi["sgst_rate"] or 0.0))
                igst_rate = Decimal(str(pi["igst_rate"] or 0.0))

                is_final_remainder = (prior_ret + p_qty == orig_qty)
                if is_final_remainder:
                    cum_taxable = Decimal(str(prior_row["cum_taxable"]))
                    cum_cgst = Decimal(str(prior_row["cum_cgst"]))
                    cum_sgst = Decimal(str(prior_row["cum_sgst"]))
                    cum_igst = Decimal(str(prior_row["cum_igst"]))
                    cum_disc = Decimal(str(prior_row["cum_disc"]))
                    cum_total = Decimal(str(prior_row["cum_total"]))

                    taxable = orig_taxable - cum_taxable
                    cgst_amt = orig_cgst - cum_cgst
                    sgst_amt = orig_sgst - cum_sgst
                    igst_amt = orig_igst - cum_igst
                    line_disc = orig_disc - cum_disc
                    total_amt = orig_total - cum_total
                    tax_amt = cgst_amt + sgst_amt + igst_amt
                    net_rate = orig_rate - ((line_disc / p_qty) if p_qty > Decimal("0") else Decimal("0.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                else:
                    ratio = p_qty / orig_qty
                    line_disc = (orig_disc * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    taxable = (orig_taxable * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    cgst_amt = (taxable * cgst_rate / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    sgst_amt = (taxable * sgst_rate / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    igst_amt = (taxable * igst_rate / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    tax_amt = cgst_amt + sgst_amt + igst_amt
                    total_amt = taxable + tax_amt
                    net_rate = orig_rate - ((line_disc / p_qty) if p_qty > Decimal("0") else Decimal("0.00")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                sum_taxable += taxable
                sum_cgst += cgst_amt
                sum_sgst += sgst_amt
                sum_igst += igst_amt

                calculated_items.append({
                    "purchase_item_id": p_item_id,
                    "product_id": pi["product_id"],
                    "product_name": pi.get("product_name"),
                    "batch_id": expected_batch_id,
                    "batch_no": expected_batch["batch_no"],
                    "qty": float(p_qty),
                    "purchase_rate": float(orig_rate),
                    "discount_amount": float(line_disc),
                    "net_rate": float(net_rate),
                    "taxable_amount": float(taxable),
                    "cgst_rate": float(cgst_rate),
                    "cgst_amount": float(cgst_amt),
                    "sgst_rate": float(sgst_rate),
                    "sgst_amount": float(sgst_amt),
                    "igst_rate": float(igst_rate),
                    "igst_amount": float(igst_amt),
                    "tax_amount": float(tax_amt),
                    "total_amount": float(total_amt),
                    "reason": p_reason,
                })

            # 4. Derive return header totals & rounding
            total_tax = sum_cgst + sum_sgst + sum_igst
            gross_total = sum_taxable + total_tax
            net_amount = gross_total.quantize(Decimal("1"), rounding=ROUND_HALF_UP).quantize(Decimal("0.01"))
            round_off = (net_amount - gross_total).quantize(Decimal("0.01"))

            return {
                "purchase_id": purchase_id,
                "supplier_id": purch_row["supplier_id"],
                "supplier_name": supplier_name,
                "supplier_balance": float(supplier_balance),
                "total_taxable": float(sum_taxable),
                "total_cgst": float(sum_cgst),
                "total_sgst": float(sum_sgst),
                "total_igst": float(sum_igst),
                "total_tax": float(total_tax),
                "gross_total": float(gross_total),
                "round_off": float(round_off),
                "net_amount": float(net_amount),
                "items": calculated_items,
            }
        finally:
            if not is_external:
                executor.close()

    def create_return_header_and_items(
        self,
        return_data: PurchaseReturnCreate,
        conn: sqlite3.Connection,
        request_fingerprint: Optional[str] = None,
    ) -> int:
        """
        Creates purchase return header and line items only,
        without inserting settlements. Used in multi-step workflows.
        Does NOT commit, roll back, or close the connection.
        """
        return self._persist_return_header_and_lines(
            return_data,
            conn=conn,
            request_fingerprint=request_fingerprint,
        )

    def create_return(
        self,
        return_data: PurchaseReturnCreate,
        conn: sqlite3.Connection,
        credit_id: Optional[int] = None,
        request_fingerprint: Optional[str] = None,
    ) -> int:
        """
        Persists a purchase return header, line items, and settlement allocations
        using the caller's SQLite connection.
        If a SUPPLIER_CREDIT settlement is included, credit_id may be supplied
        or pre-created on the return.
        Does NOT commit, roll back, or close the connection.
        """
        return_id = self._persist_return_header_and_lines(
            return_data,
            conn=conn,
            request_fingerprint=request_fingerprint,
        )

        # Insert settlements if present
        if return_data.settlements:
            self.create_settlements(
                return_data.settlements,
                return_id=return_id,
                conn=conn,
                credit_id=credit_id,
            )

        return return_id

    def _persist_return_header_and_lines(
        self,
        return_data: PurchaseReturnCreate,
        conn: sqlite3.Connection,
        request_fingerprint: Optional[str] = None,
    ) -> int:
        """Internal helper implementing return header and items persistence."""
        if conn is None:
            raise ValueError("A valid SQLite connection must be provided.")

        cur = conn.cursor()

        # Authoritative calculation of taxes, rounding, and item breakdowns
        breakdown = self.calculate_return_breakdown(
            purchase_id=return_data.purchase_id,
            items=return_data.items,
            conn=conn,
            supplier_id=return_data.supplier_id,
        )

        net_amount = Decimal(str(breakdown["net_amount"]))

        # Validate settlements total must match net_amount
        if return_data.settlements:
            total_settlement_alloc = sum(s.amount for s in return_data.settlements)
            if total_settlement_alloc != net_amount:
                raise ValueError(
                    f"Total settlements ({total_settlement_alloc}) must equal return net amount ({net_amount})."
                )

        # Generate sequential return_no (strictly backend-controlled, monotonic per day)
        if return_data.return_no:
            raise ValueError(
                "Client-supplied return_no is not permitted; return numbers are strictly system-generated."
            )
        date_prefix = return_data.return_date.replace("-", "")[:8]
        cur.execute(
            """
            SELECT return_no FROM purchase_returns
            WHERE return_no LIKE ?
            ORDER BY return_no DESC LIMIT 1;
            """,
            (f"PR-{date_prefix}-%",),
        )
        last_ret_row = cur.fetchone()
        if last_ret_row and last_ret_row["return_no"]:
            try:
                last_seq = int(str(last_ret_row["return_no"]).split("-")[-1])
                next_seq = last_seq + 1
            except (ValueError, IndexError):
                next_seq = 1
        else:
            next_seq = 1
        return_no = f"PR-{date_prefix}-{next_seq:04d}"

        # Insert header
        fingerprint = request_fingerprint or return_data.compute_request_fingerprint()
        sql_insert_head = """
            INSERT INTO purchase_returns (
                return_no, return_date, purchase_id, supplier_id,
                total_taxable, total_cgst, total_sgst, total_igst, total_tax,
                round_off, net_amount, supplier_credit_note_no, supplier_credit_note_date,
                remarks, status, itc_status, idempotency_key, request_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'POSTED', ?, ?, ?);
        """
        cur.execute(
            sql_insert_head,
            (
                return_no,
                return_data.return_date,
                return_data.purchase_id,
                return_data.supplier_id,
                breakdown["total_taxable"],
                breakdown["total_cgst"],
                breakdown["total_sgst"],
                breakdown["total_igst"],
                breakdown["total_tax"],
                breakdown["round_off"],
                breakdown["net_amount"],
                return_data.supplier_credit_note_no,
                return_data.supplier_credit_note_date,
                return_data.remarks,
                return_data.itc_status,
                return_data.idempotency_key,
                fingerprint,
            ),
        )
        return_id = cur.lastrowid

        # Insert lines
        sql_insert_item = """
            INSERT INTO purchase_return_items (
                return_id, purchase_item_id, product_id, batch_id,
                qty, purchase_rate, discount_amount, net_rate,
                taxable_amount, cgst_rate, cgst_amount, sgst_rate, sgst_amount,
                igst_rate, igst_amount, tax_amount, total_amount, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        for itm in breakdown["items"]:
            cur.execute(
                sql_insert_item,
                (
                    return_id,
                    itm["purchase_item_id"],
                    itm["product_id"],
                    itm["batch_id"],
                    itm["qty"],
                    itm["purchase_rate"],
                    itm["discount_amount"],
                    itm["net_rate"],
                    itm["taxable_amount"],
                    itm["cgst_rate"],
                    itm["cgst_amount"],
                    itm["sgst_rate"],
                    itm["sgst_amount"],
                    itm["igst_rate"],
                    itm["igst_amount"],
                    itm["tax_amount"],
                    itm["total_amount"],
                    itm["reason"],
                ),
            )

        return return_id

        return return_id

    def create_settlements(
        self,
        settlements: List[PurchaseReturnSettlementCreate],
        return_id: int,
        conn: sqlite3.Connection,
        credit_id: Optional[int] = None,
    ) -> List[int]:
        """Creates multiple settlement allocations for a return."""
        ids: List[int] = []
        for stl in settlements:
            cid = credit_id if stl.settlement_type == "SUPPLIER_CREDIT" else None
            sid = self.create_settlement(stl, return_id=return_id, conn=conn, credit_id=cid)
            ids.append(sid)
        return ids

    def create_settlement(
        self,
        settlement: PurchaseReturnSettlementCreate,
        return_id: int,
        conn: sqlite3.Connection,
        credit_id: Optional[int] = None,
    ) -> int:
        """
        Creates a single settlement allocation for a purchase return using the caller's connection.
        Enforces database CHECK constraints on settlement_type, account_id, and credit_id.
        Ensures SUPPLIER_CREDIT settlements strictly match credit original_amount.
        """
        if conn is None:
            raise ValueError("A valid SQLite connection must be provided for create_settlement.")

        cur = conn.cursor()

        # Verify return exists
        cur.execute("SELECT return_id, supplier_id, net_amount FROM purchase_returns WHERE return_id = ?;", (return_id,))
        ret_row = cur.fetchone()
        if not ret_row:
            raise ValueError(f"Purchase return #{return_id} does not exist.")

        effective_credit_id: Optional[int] = None

        if settlement.settlement_type == "SUPPLIER_CREDIT":
            # 1. Enforce only one SUPPLIER_CREDIT settlement per return
            cur.execute(
                "SELECT settlement_id FROM purchase_return_settlements WHERE return_id = ? AND settlement_type = 'SUPPLIER_CREDIT';",
                (return_id,),
            )
            if cur.fetchone():
                raise ValueError(
                    f"Return #{return_id} already has a SUPPLIER_CREDIT settlement allocation. "
                    "Only one SUPPLIER_CREDIT allocation is permitted per return."
                )

            # 2. Resolve credit_id: either explicitly passed or queried from supplier_credits
            if credit_id is not None:
                effective_credit_id = credit_id
            else:
                cur.execute(
                    "SELECT credit_id FROM supplier_credits WHERE return_id = ?;",
                    (return_id,),
                )
                sc_row = cur.fetchone()
                if sc_row:
                    effective_credit_id = sc_row["credit_id"]
                else:
                    raise ValueError(
                        "credit_id must be provided or supplier credit must exist before creating SUPPLIER_CREDIT settlement."
                    )

            # 3. Validate credit existence, return_id match, and EXACT AMOUNT MATCH
            cur.execute(
                "SELECT credit_id, return_id, supplier_id, original_amount FROM supplier_credits WHERE credit_id = ?;",
                (effective_credit_id,),
            )
            cred_row = cur.fetchone()
            if not cred_row:
                raise ValueError(f"Supplier credit #{effective_credit_id} does not exist.")

            if cred_row["return_id"] != return_id:
                raise ValueError(
                    f"Credit #{effective_credit_id} originates from return #{cred_row['return_id']}, "
                    f"not return #{return_id}."
                )

            if Decimal(str(cred_row["original_amount"])) != settlement.amount:
                raise ValueError(
                    f"SUPPLIER_CREDIT settlement amount ({settlement.amount}) must exactly match "
                    f"supplier credit original_amount ({cred_row['original_amount']})."
                )

        else:
            # Non-SUPPLIER_CREDIT: credit_id must be None
            if credit_id is not None:
                raise ValueError(
                    f"credit_id must be None for non-SUPPLIER_CREDIT settlement type ({settlement.settlement_type})."
                )
            if settlement.settlement_type in ("REFUND_CASH", "REFUND_BANK") and not settlement.account_id:
                raise ValueError(f"account_id is required for settlement type {settlement.settlement_type}.")

        sql = """
            INSERT INTO purchase_return_settlements (
                return_id, settlement_type, amount, account_id, credit_id, reference_no, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
        """
        cur.execute(
            sql,
            (
                return_id,
                settlement.settlement_type,
                float(settlement.amount),
                settlement.account_id,
                effective_credit_id,
                settlement.reference_no,
                settlement.notes,
            ),
        )
        return cur.lastrowid

    def update_voucher_id(
        self,
        return_id: int,
        voucher_id: int,
        conn: sqlite3.Connection,
    ) -> None:
        """Links the generated accounting voucher to the purchase return header."""
        if conn is None:
            raise ValueError("A valid SQLite connection must be provided for update_voucher_id.")
        cur = conn.cursor()
        cur.execute(
            "UPDATE purchase_returns SET voucher_id = ? WHERE return_id = ?;",
            (voucher_id, return_id),
        )

    # ------------------------------------------------------------------------
    # 3. Return Retrieval & Listing
    # ------------------------------------------------------------------------

    def get_return_by_id(
        self,
        return_id: int,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Optional[PurchaseReturn]:
        """
        Fetches a purchase return header with its line items and settlement allocations.
        Preserves NULL for historical snapshot fields.
        """
        sql_head = """
            SELECT 
                pr.*,
                s.supplier_name,
                s.gstin as supplier_gstin,
                p.invoice_no as purchase_invoice_no
            FROM purchase_returns pr
            JOIN suppliers s ON pr.supplier_id = s.supplier_id
            JOIN purchases p ON pr.purchase_id = p.purchase_id
            WHERE pr.return_id = ?;
        """
        sql_items = """
            SELECT 
                pri.*,
                prod.product_name,
                sb.batch_no
            FROM purchase_return_items pri
            JOIN products prod ON pri.product_id = prod.product_id
            LEFT JOIN stock_batches sb ON pri.batch_id = sb.batch_id
            WHERE pri.return_id = ?
            ORDER BY pri.return_item_id ASC;
        """
        sql_settlements = """
            SELECT 
                prs.*,
                la.account_name,
                sc.credit_no
            FROM purchase_return_settlements prs
            LEFT JOIN ledger_accounts la ON prs.account_id = la.account_id
            LEFT JOIN supplier_credits sc ON prs.credit_id = sc.credit_id
            WHERE prs.return_id = ?
            ORDER BY prs.settlement_id ASC;
        """

        executor, is_external = self._get_executor(conn)
        try:
            cur = executor.cursor()
            cur.execute(sql_head, (return_id,))
            head_row = cur.fetchone()
            if not head_row:
                return None

            return_dict = dict(head_row)

            cur.execute(sql_items, (return_id,))
            return_dict["items"] = [
                PurchaseReturnItem(**dict(r)) for r in cur.fetchall()
            ]

            cur.execute(sql_settlements, (return_id,))
            return_dict["settlements"] = [
                PurchaseReturnSettlement(**dict(r)) for r in cur.fetchall()
            ]

            return PurchaseReturn(**return_dict)
        finally:
            if not is_external:
                executor.close()

    def get_return_by_idempotency_key(
        self,
        idempotency_key: str,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Optional[PurchaseReturn]:
        """
        Fetches a purchase return by its idempotency_key for replay protection.
        Enables the service layer to determine:
        - Same key + same fingerprint = return existing return
        - Same key + different fingerprint = HTTP 409 Conflict
        - New key = process as new return
        """
        executor, is_external = self._get_executor(conn)
        try:
            cur = executor.cursor()
            cur.execute(
                "SELECT return_id FROM purchase_returns WHERE idempotency_key = ?;",
                (idempotency_key,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self.get_return_by_id(row["return_id"], conn=conn)
        finally:
            if not is_external:
                executor.close()

    def list_returns(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        supplier_id: Optional[int] = None,
        purchase_id: Optional[int] = None,
        status: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> List[PurchaseReturn]:
        """
        Lists purchase returns filtered by date range, supplier, purchase, and status.
        """
        conditions = ["1=1"]
        params: List[Any] = []

        if from_date:
            conditions.append("pr.return_date >= ?")
            params.append(from_date)
        if to_date:
            conditions.append("pr.return_date <= ?")
            params.append(to_date)
        if supplier_id:
            conditions.append("pr.supplier_id = ?")
            params.append(supplier_id)
        if purchase_id:
            conditions.append("pr.purchase_id = ?")
            params.append(purchase_id)
        if status:
            conditions.append("pr.status = ?")
            params.append(status)

        sql = f"""
            SELECT 
                pr.*,
                s.supplier_name,
                s.gstin as supplier_gstin,
                p.invoice_no as purchase_invoice_no
            FROM purchase_returns pr
            JOIN suppliers s ON pr.supplier_id = s.supplier_id
            JOIN purchases p ON pr.purchase_id = p.purchase_id
            WHERE {' AND '.join(conditions)}
            ORDER BY pr.return_date DESC, pr.return_id DESC;
        """

        executor, is_external = self._get_executor(conn)
        try:
            cur = executor.cursor()
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            results: List[PurchaseReturn] = []
            for r in rows:
                results.append(PurchaseReturn(**dict(r)))
            return results
        finally:
            if not is_external:
                executor.close()
