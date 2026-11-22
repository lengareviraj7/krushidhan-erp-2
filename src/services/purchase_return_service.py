"""
Purchase Return Business Service Orchestration.

Manages purchase returns, inventory deduction, settlement processing,
supplier credit creation, and double-entry accounting voucher posting.
All write operations execute strictly within SQLite BEGIN IMMEDIATE transactions.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from src.db.connection import DatabaseManager, get_db_manager
from src.models.accounting import LedgerEntry, Voucher
from src.models.inventory import StockLedgerEntry
from src.models.purchase_return import (
    PurchaseReturn,
    PurchaseReturnCreate,
    PurchaseReturnPreviewRequest,
    PurchaseReturnPreviewResponse,
    PurchaseReturnSettlementCreate,
    SupplierCreditCreate,
)
from src.repositories.accounting_repository import AccountingRepository
from src.repositories.inventory_repository import InventoryRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.repositories.purchase_return_repository import PurchaseReturnRepository
from src.repositories.supplier_credit_repository import SupplierCreditRepository
from src.services.idempotency import IdempotencyConflictError


class PurchaseReturnService:
    """
    Domain service for orchestrating purchase returns and all financial/inventory side-effects.
    """

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        purchase_return_repo: Optional[PurchaseReturnRepository] = None,
        supplier_credit_repo: Optional[SupplierCreditRepository] = None,
        inventory_repo: Optional[InventoryRepository] = None,
        accounting_repo: Optional[AccountingRepository] = None,
        master_repo: Optional[MasterDataRepository] = None,
    ):
        self.db = db or get_db_manager()
        self.purchase_return_repo = purchase_return_repo or PurchaseReturnRepository(self.db)
        self.supplier_credit_repo = supplier_credit_repo or SupplierCreditRepository(self.db)
        self.inventory_repo = inventory_repo or InventoryRepository(self.db)
        self.accounting_repo = accounting_repo or AccountingRepository(self.db)
        self.master_repo = master_repo or MasterDataRepository(self.db)

    def process_purchase_return(self, request: PurchaseReturnCreate) -> PurchaseReturn:
        """
        Executes a 18-step atomic purchase return workflow:
        1. Validate mandatory idempotency_key.
        2. Compute canonical request_fingerprint internally.
        3. Query key inside immediate transaction.
        4. If key exists and fingerprint matches, return existing record (idempotent replay).
        5. If key exists with different fingerprint, reject with IdempotencyConflictError (HTTP 409).
        6. Validate original purchase exists and supplier matches.
        7. Detect unlinked historical returns and block ambiguous quantity calculations.
        8. Calculate eligible return quantities and check for over-returns.
        9. Verify sufficient physical stock exists in warehouse batches.
        10. Calculate taxable values, trade discounts, and GST snapshots (proportional or exact remainder).
        11. Create return header and line items.
        12. Deduct stock using InventoryRepository.deduct_batch_stock().
        13. Record stock movement in stock_ledger (transaction_type="PURCHASE_RETURN").
        14. If SUPPLIER_CREDIT is present, create supplier credit note.
        15. Create settlements linked to credit_id.
        16. Post balanced double-entry accounting voucher (voucher_type="DEBIT_NOTE").
        17. Save voucher reference (voucher_id) on purchase_returns header.
        18. Commit transaction atomically and return hydrated PurchaseReturn model.
        """
        # Step 1: Validate mandatory idempotency_key
        if not request.idempotency_key or not request.idempotency_key.strip():
            raise ValueError("idempotency_key is required and cannot be empty.")

        if request.return_no:
            raise ValueError(
                "Client-supplied return_no is not permitted; return numbers are strictly system-generated."
            )

        if not request.items:
            raise ValueError("Purchase return must contain at least one line item.")

        if not request.settlements:
            raise ValueError("Purchase return must contain at least one settlement allocation.")

        # Step 2: Compute canonical request_fingerprint internally
        fingerprint = request.compute_request_fingerprint()

        # Execute all writes within an IMMEDIATE transaction for write-safety
        with self.db.transaction(mode="IMMEDIATE") as conn:
            cur = conn.cursor()

            # Step 3: Query key inside immediate transaction
            cur.execute(
                "SELECT return_id, request_fingerprint FROM purchase_returns WHERE idempotency_key = ?;",
                (request.idempotency_key,),
            )
            existing_row = cur.fetchone()
            if existing_row:
                # Step 4: If key exists and fingerprint matches, return existing record
                if existing_row["request_fingerprint"] == fingerprint:
                    return self.purchase_return_repo.get_return_by_id(existing_row["return_id"], conn=conn)
                # Step 5: If key exists with different fingerprint, reject with 409 Conflict
                raise IdempotencyConflictError(
                    f"Idempotency conflict: key '{request.idempotency_key}' has already been processed "
                    f"with a different request payload."
                )

            # Step 6: Validate original purchase exists and supplier matches
            cur.execute(
                "SELECT purchase_id, supplier_id, invoice_no, purchase_date FROM purchases WHERE purchase_id = ?;",
                (request.purchase_id,),
            )
            purch_row = cur.fetchone()
            if not purch_row:
                raise ValueError(f"Original purchase invoice #{request.purchase_id} does not exist.")

            if purch_row["supplier_id"] != request.supplier_id:
                raise ValueError(
                    f"Supplier ID mismatch: return supplier_id ({request.supplier_id}) "
                    f"does not match purchase supplier_id ({purch_row['supplier_id']})."
                )

            # Step 7: Detect unlinked historical returns and block ambiguous quantity calculations
            cur.execute(
                """
                SELECT COUNT(*) as unlinked_count
                FROM purchase_return_items pri
                JOIN purchase_returns pr ON pri.return_id = pr.return_id
                WHERE pr.purchase_id = ?
                  AND pri.purchase_item_id IS NULL
                  AND (pr.status IS NULL OR pr.status != 'CANCELLED');
                """,
                (request.purchase_id,),
            )
            if cur.fetchone()["unlinked_count"] > 0:
                raise ValueError(
                    f"Cannot process return for purchase #{request.purchase_id}: "
                    f"historical returns exist with unlinked line items (purchase_item_id is NULL)."
                )

            # Step 8 & 9: Pre-validate item quantities, free_qty, and exact batch stock
            cur.execute(
                "SELECT * FROM purchase_items WHERE purchase_id = ?;",
                (request.purchase_id,),
            )
            pi_map = {r["item_id"]: dict(r) for r in cur.fetchall()}

            normalized_items: List[PurchaseReturnItemCreate] = []

            for itm in request.items:
                pi = pi_map.get(itm.purchase_item_id)
                if not pi:
                    raise ValueError(
                        f"purchase_item_id #{itm.purchase_item_id} does not belong to purchase #{request.purchase_id}."
                    )
                if pi["product_id"] != itm.product_id:
                    raise ValueError(
                        f"Product ID mismatch on line #{itm.purchase_item_id}: "
                        f"payload product_id={itm.product_id}, purchase product_id={pi['product_id']}."
                    )

                # Requirement 2: Explicitly defer returns for lines containing free quantity
                raw_free_qty = pi.get("free_qty")
                free_qty = Decimal(str(raw_free_qty)) if raw_free_qty is not None else Decimal("0.0000")
                if free_qty > Decimal("0"):
                    raise ValueError(
                        f"Purchase line #{itm.purchase_item_id} contains free/bonus quantity ({free_qty}). "
                        f"Returns on items with free quantities are not supported in this release."
                    )

                purchased_qty = Decimal(str(pi["qty"]))
                # Check prior returned quantity
                cur.execute(
                    """
                    SELECT COALESCE(SUM(pri.qty), 0.0) as total_returned
                    FROM purchase_return_items pri
                    JOIN purchase_returns pr ON pri.return_id = pr.return_id
                    WHERE pri.purchase_item_id = ?
                      AND (pr.status IS NULL OR pr.status != 'CANCELLED');
                    """,
                    (itm.purchase_item_id,),
                )
                prior_ret = Decimal(str(cur.fetchone()["total_returned"]))
                if prior_ret + itm.qty > purchased_qty:
                    raise ValueError(
                        f"Cumulative return quantity ({prior_ret + itm.qty}) exceeds original purchased quantity "
                        f"({purchased_qty}) for item #{itm.purchase_item_id}."
                    )

                # Requirement 1: Resolve authoritative batch identity from original purchase line
                cur.execute(
                    """
                    SELECT batch_id, product_id, batch_no, current_qty
                    FROM stock_batches
                    WHERE product_id = ? AND batch_no = ?;
                    """,
                    (pi["product_id"], pi["batch_no"]),
                )
                expected_batch_row = cur.fetchone()
                if not expected_batch_row:
                    raise ValueError(
                        f"No stock batch found for product #{pi['product_id']} and batch '{pi['batch_no']}' "
                        f"originating from purchase line #{itm.purchase_item_id}."
                    )

                expected_batch_id = int(expected_batch_row["batch_id"])

                # Verify resolved stock batch row matches purchase line metadata
                if (
                    expected_batch_row["product_id"] != pi["product_id"]
                    or expected_batch_row["batch_no"] != pi["batch_no"]
                ):
                    raise ValueError(
                        f"Resolved batch #{expected_batch_id} metadata does not match purchase line #{itm.purchase_item_id}."
                    )

                # If client supplied batch_id, verify it exactly matches the backend-resolved ID
                if itm.batch_id is not None:
                    if itm.batch_id != expected_batch_id:
                        cur.execute(
                            "SELECT product_id, batch_no FROM stock_batches WHERE batch_id = ?;",
                            (itm.batch_id,),
                        )
                        supplied_sb = cur.fetchone()
                        if not supplied_sb:
                            raise ValueError(
                                f"Client-supplied batch_id #{itm.batch_id} does not exist in stock batches."
                            )
                        if supplied_sb["product_id"] != pi["product_id"]:
                            raise ValueError(
                                f"Client-supplied batch_id #{itm.batch_id} belongs to product #{supplied_sb['product_id']}, "
                                f"not the returned product #{pi['product_id']}."
                            )
                        raise ValueError(
                            f"Batch mismatch on line #{itm.purchase_item_id}: client supplied batch_id #{itm.batch_id} "
                            f"('{supplied_sb['batch_no']}'), but original purchase line is for batch '{pi['batch_no']}' "
                            f"(expected batch_id #{expected_batch_id})."
                        )

                # Step 9: Verify physical batch stock availability using the authoritative batch
                avail_batch_stock = Decimal(str(expected_batch_row["current_qty"]))
                if avail_batch_stock < itm.qty:
                    raise ValueError(
                        f"Insufficient physical stock in batch '{expected_batch_row['batch_no']}': "
                        f"available {avail_batch_stock}, requested return {itm.qty}."
                    )

                normalized_items.append(
                    itm.model_copy(update={"batch_id": expected_batch_id})
                )

            normalized_request = request.model_copy(update={"items": normalized_items})

            # Pre-validate PAYABLE_REDUCTION settlement against supplier positive current_balance
            payable_reduction = sum(
                s.amount for s in request.settlements if s.settlement_type == "PAYABLE_REDUCTION"
            )
            if payable_reduction > Decimal("0.00"):
                cur.execute(
                    "SELECT current_balance FROM suppliers WHERE supplier_id = ?;",
                    (request.supplier_id,),
                )
                supp_row = cur.fetchone()
                if not supp_row:
                    raise ValueError(f"Supplier #{request.supplier_id} not found.")
                supp_balance = Decimal(str(supp_row["current_balance"] or 0.0))
                if supp_balance <= Decimal("0.00"):
                    raise ValueError(
                        f"Cannot reduce payable: supplier #{request.supplier_id} has non-positive balance ({supp_balance})."
                    )
                if payable_reduction > supp_balance:
                    raise ValueError(
                        f"Payable reduction ({payable_reduction}) cannot exceed supplier current payable balance ({supp_balance})."
                    )

            # Step 10 & 11: Create return header and items using repository with normalized_request
            return_id = self.purchase_return_repo.create_return_header_and_items(
                normalized_request,
                conn=conn,
                request_fingerprint=fingerprint,
            )

            # Retrieve created return header details
            cur.execute("SELECT * FROM purchase_returns WHERE return_id = ?;", (return_id,))
            created_head = dict(cur.fetchone())
            return_no = created_head["return_no"]
            sum_taxable = Decimal(str(created_head["total_taxable"]))
            sum_cgst = Decimal(str(created_head["total_cgst"] or 0.0))
            sum_sgst = Decimal(str(created_head["total_sgst"] or 0.0))
            sum_igst = Decimal(str(created_head["total_igst"] or 0.0))
            total_tax = Decimal(str(created_head["total_tax"] or 0.0))
            round_off = Decimal(str(created_head["round_off"] or 0.0))
            net_amount = Decimal(str(created_head["net_amount"]))

            # Validate settlement allocations sum matches net_amount exactly
            total_settlements = sum(s.amount for s in request.settlements)
            if total_settlements != net_amount:
                raise ValueError(
                    f"Total settlement amount ({total_settlements}) does not match return net amount ({net_amount})."
                )

            # Step 12 & 13: Deduct batch stock and record stock movement for each line
            cur.execute(
                "SELECT return_item_id, product_id, batch_id, qty, net_rate FROM purchase_return_items WHERE return_id = ?;",
                (return_id,),
            )
            persisted_items = cur.fetchall()

            for itm_row in persisted_items:
                b_id = itm_row["batch_id"]
                line_qty = float(itm_row["qty"])

                # Deduct batch stock
                self.inventory_repo.deduct_batch_stock(b_id, line_qty, conn=conn)

                # Fetch updated batch balance for stock ledger
                cur.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (b_id,))
                new_batch_balance = float(cur.fetchone()["current_qty"])

                # Record stock movement (transaction_type="PURCHASE_RETURN")
                self.inventory_repo.record_stock_movement(
                    StockLedgerEntry(
                        product_id=itm_row["product_id"],
                        batch_id=b_id,
                        transaction_type="PURCHASE_RETURN",
                        reference_type="PURCHASE_RETURN",
                        reference_id=return_id,
                        qty_in=0.0,
                        qty_out=line_qty,
                        balance_qty=new_batch_balance,
                        rate=float(itm_row["net_rate"]),
                        remarks=f"Purchase Return #{return_no} for Inv #{purch_row['invoice_no']}",
                    ),
                    conn=conn,
                )

            # Step 14: If SUPPLIER_CREDIT is present, create supplier credit note
            sc_settlement = next((s for s in request.settlements if s.settlement_type == "SUPPLIER_CREDIT"), None)
            credit_id: Optional[int] = None
            if sc_settlement:
                credit_no = self.supplier_credit_repo.generate_next_credit_no(conn=conn)
                credit_create = SupplierCreditCreate(
                    credit_no=credit_no,
                    supplier_id=request.supplier_id,
                    return_id=return_id,
                    credit_date=request.return_date,
                    original_amount=sc_settlement.amount,
                    balance_amount=sc_settlement.amount,
                    notes=sc_settlement.notes or f"Credit note from return {return_no}",
                )
                credit_id = self.supplier_credit_repo.create_credit(credit_create, conn=conn)

            # Step 15: Create settlements linked to credit_id
            self.purchase_return_repo.create_settlements(
                request.settlements,
                return_id=return_id,
                conn=conn,
                credit_id=credit_id,
            )

            # Step 16: Post balanced double-entry accounting voucher (voucher_type="DEBIT_NOTE")
            purchase_return_ac = self.accounting_repo.get_purchase_return_account(conn=conn)
            creditors_ac = self.accounting_repo.get_creditors_account(conn=conn)
            cash_ac = self.accounting_repo.get_cash_account(conn=conn)
            bank_ac = self.accounting_repo.get_bank_account(conn=conn)
            round_off_ac = self.accounting_repo.get_round_off_account(conn=conn)
            cgst_in_ac = self.accounting_repo.get_input_gst_account("CGST", conn=conn)
            sgst_in_ac = self.accounting_repo.get_input_gst_account("SGST", conn=conn)
            igst_in_ac = self.accounting_repo.get_input_gst_account("IGST", conn=conn)
            credits_receivable_ac = self.accounting_repo.get_supplier_credits_receivable_account(conn=conn)
            itc_suspense_ac = self.accounting_repo.get_itc_suspense_account(conn=conn)

            entries: List[LedgerEntry] = []

            # --- DEBIT ENTRIES (Settlements + Round-Off if negative) ---
            for stl in request.settlements:
                stl_val = float(stl.amount)
                if stl.settlement_type == "PAYABLE_REDUCTION":
                    entries.append(
                        LedgerEntry(
                            account_id=creditors_ac.account_id,
                            debit_amount=stl_val,
                            credit_amount=0.0,
                            particulars=f"Payable reduction against Purchase #{purch_row['invoice_no']}",
                        )
                    )
                elif stl.settlement_type == "REFUND_CASH":
                    target_acc_id = stl.account_id or cash_ac.account_id
                    entries.append(
                        LedgerEntry(
                            account_id=target_acc_id,
                            debit_amount=stl_val,
                            credit_amount=0.0,
                            particulars=f"Cash refund for Purchase Return #{return_no}",
                        )
                    )
                elif stl.settlement_type == "REFUND_BANK":
                    target_acc_id = stl.account_id or bank_ac.account_id
                    entries.append(
                        LedgerEntry(
                            account_id=target_acc_id,
                            debit_amount=stl_val,
                            credit_amount=0.0,
                            particulars=f"Bank refund for Purchase Return #{return_no}",
                        )
                    )
                elif stl.settlement_type == "SUPPLIER_CREDIT":
                    entries.append(
                        LedgerEntry(
                            account_id=credits_receivable_ac.account_id,
                            debit_amount=stl_val,
                            credit_amount=0.0,
                            particulars=f"Supplier Credit Receivable for Purchase Return #{return_no}",
                        )
                    )

            if round_off < Decimal("0.00"):
                entries.append(
                    LedgerEntry(
                        account_id=round_off_ac.account_id,
                        debit_amount=float(abs(round_off)),
                        credit_amount=0.0,
                        particulars=f"Round off difference for Purchase Return #{return_no}",
                    )
                )

            # --- CREDIT ENTRIES (Taxable + GST + Round-Off if positive) ---
            if sum_taxable > Decimal("0.00"):
                entries.append(
                    LedgerEntry(
                        account_id=purchase_return_ac.account_id,
                        debit_amount=0.0,
                        credit_amount=float(sum_taxable),
                        particulars=f"Purchase Return #{return_no}",
                    )
                )

            itc_status = request.itc_status or "CLAIMED_REVERSED"
            if itc_status == "PENDING_CONFIRMATION":
                if total_tax > Decimal("0.00"):
                    entries.append(
                        LedgerEntry(
                            account_id=itc_suspense_ac.account_id,
                            debit_amount=0.0,
                            credit_amount=float(total_tax),
                            particulars=f"ITC reversal pending confirmation for Purchase Return #{return_no}",
                        )
                    )
            else:
                if sum_cgst > Decimal("0.00"):
                    if not cgst_in_ac:
                        raise ValueError("CGST Input Account not configured.")
                    entries.append(
                        LedgerEntry(
                            account_id=cgst_in_ac.account_id,
                            debit_amount=0.0,
                            credit_amount=float(sum_cgst),
                            particulars=f"CGST Input Reversal for Purchase Return #{return_no}",
                        )
                    )
                if sum_sgst > Decimal("0.00"):
                    if not sgst_in_ac:
                        raise ValueError("SGST Input Account not configured.")
                    entries.append(
                        LedgerEntry(
                            account_id=sgst_in_ac.account_id,
                            debit_amount=0.0,
                            credit_amount=float(sum_sgst),
                            particulars=f"SGST Input Reversal for Purchase Return #{return_no}",
                        )
                    )
                if sum_igst > Decimal("0.00"):
                    if not igst_in_ac:
                        raise ValueError("IGST Input Account not configured.")
                    entries.append(
                        LedgerEntry(
                            account_id=igst_in_ac.account_id,
                            debit_amount=0.0,
                            credit_amount=float(sum_igst),
                            particulars=f"IGST Input Reversal for Purchase Return #{return_no}",
                        )
                    )

            if round_off > Decimal("0.00"):
                entries.append(
                    LedgerEntry(
                        account_id=round_off_ac.account_id,
                        debit_amount=0.0,
                        credit_amount=float(round_off),
                        particulars=f"Round off difference for Purchase Return #{return_no}",
                    )
                )

            # Validate double-entry balance before inserting
            tot_dr = round(sum(e.debit_amount for e in entries), 2)
            tot_cr = round(sum(e.credit_amount for e in entries), 2)
            if tot_dr != tot_cr or tot_dr == 0:
                raise ValueError(
                    f"Unbalanced purchase return voucher: Total Debit ({tot_dr}) != Total Credit ({tot_cr})"
                )

            vch_no = self.accounting_repo.generate_next_voucher_no("DEBIT_NOTE", conn=conn)
            vch = Voucher(
                voucher_no=vch_no,
                voucher_date=request.return_date,
                voucher_type="DEBIT_NOTE",
                total_amount=tot_dr,
                narration=f"Purchase Return #{return_no} for Inv #{purch_row['invoice_no']}",
                reference_type="PURCHASE_RETURN",
                reference_id=return_id,
                entries=entries,
            )
            voucher_id = self.accounting_repo.create_voucher(vch, conn=conn)

            # Update supplier balance for PAYABLE_REDUCTION only
            if payable_reduction > Decimal("0.00"):
                self.master_repo.update_supplier_balance(
                    request.supplier_id, -float(payable_reduction), conn=conn
                )

            # Step 17: Save voucher reference on purchase return header
            self.purchase_return_repo.update_voucher_id(return_id, voucher_id, conn=conn)

            # Step 18: Fetch and return full PurchaseReturn model
            return self.purchase_return_repo.get_return_by_id(return_id, conn=conn)

    def get_return_by_id(self, return_id: int) -> Optional[PurchaseReturn]:
        """Fetches a purchase return by ID."""
        return self.purchase_return_repo.get_return_by_id(return_id)

    def list_returns(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        supplier_id: Optional[int] = None,
        purchase_id: Optional[int] = None,
        status: Optional[str] = None,
    ) -> List[PurchaseReturn]:
        """Lists purchase returns with filtering."""
        return self.purchase_return_repo.list_returns(
            from_date=from_date,
            to_date=to_date,
            supplier_id=supplier_id,
            purchase_id=purchase_id,
            status=status,
        )

    def get_eligible_purchase_lines(self, purchase_id: int) -> List[Dict[str, Any]]:
        """Retrieves eligible return lines for a purchase invoice."""
        return self.purchase_return_repo.get_eligible_purchase_lines(purchase_id)

    def preview_purchase_return(
        self,
        request: PurchaseReturnPreviewRequest,
    ) -> Dict[str, Any]:
        """
        Calculates authoritative preview of taxes, rounding, net return amount,
        and line-level breakdown using the exact domain calculation logic.
        Executes zero database writes, does not allocate return numbers,
        and does not record or consume idempotency keys.
        """
        result = self.purchase_return_repo.calculate_return_breakdown(
            purchase_id=request.purchase_id,
            items=request.items,
            supplier_id=request.supplier_id,
        )
        cash_ac = self.accounting_repo.get_cash_account()
        bank_ac = self.accounting_repo.get_bank_account()
        result["cash_account_id"] = cash_ac.account_id if cash_ac else None
        result["cash_account_name"] = cash_ac.account_name if cash_ac else None
        result["bank_account_id"] = bank_ac.account_id if bank_ac else None
        result["bank_account_name"] = bank_ac.account_name if bank_ac else None
        result["success"] = True
        return result
