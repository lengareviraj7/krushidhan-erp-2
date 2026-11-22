"""
Service Layer for Sales Invoicing, Tax Calculations, Stock Deductions, and Customer Accounting.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
from src.db.connection import DatabaseManager, get_db_manager
from src.models.accounting import LedgerEntry, Voucher
from src.models.inventory import StockLedgerEntry
from src.models.sales import Sale, SaleItem, SalesReturn, SalesReturnItem

from src.repositories.accounting_repository import AccountingRepository
from src.repositories.inventory_repository import InventoryRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.repositories.sales_repository import SalesRepository
from src.services.idempotency import DuplicateInvoiceError, IdempotencyConflictError


class SalesService:
    """Business logic for sales billing, tax calculations, FEFO batch deduction, and accounting."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or get_db_manager()
        self.sales_repo = SalesRepository(self.db)
        self.inventory_repo = InventoryRepository(self.db)
        self.master_repo = MasterDataRepository(self.db)
        self.accounting_repo = AccountingRepository(self.db)

    def calculate_item_tax(
        self,
        qty: float,
        rate: float,
        discount_percent: float,
        cgst_rate: float,
        sgst_rate: float,
        igst_rate: float,
        is_rate_tax_inclusive: bool = True,
    ) -> dict:
        """
        Calculate taxable amount and GST breakdown for a line item.
        In Agri retail, rates quoted to farmers are typically GST-inclusive.
        """
        gross_amount = qty * rate
        discount_amount = round(gross_amount * (discount_percent / 100.0), 2)
        net_after_discount = gross_amount - discount_amount

        total_tax_rate = (cgst_rate + sgst_rate) if (cgst_rate + sgst_rate) > 0 else igst_rate

        if is_rate_tax_inclusive:
            if total_tax_rate > 0:
                taxable_amount = round(net_after_discount / (1.0 + (total_tax_rate / 100.0)), 2)
                total_tax = round(net_after_discount - taxable_amount, 2)
            else:
                taxable_amount = round(net_after_discount, 2)
                total_tax = 0.0

            if igst_rate > 0:
                igst_amount = total_tax
                cgst_amount = 0.0
                sgst_amount = 0.0
            else:
                cgst_amount = round(taxable_amount * (cgst_rate / 100.0), 2)
                sgst_amount = round(taxable_amount * (sgst_rate / 100.0), 2)
                igst_amount = 0.0

            total_line_amount = round(taxable_amount + cgst_amount + sgst_amount + igst_amount, 2)
        else:
            taxable_amount = round(net_after_discount, 2)
            cgst_amount = round(taxable_amount * (cgst_rate / 100.0), 2)
            sgst_amount = round(taxable_amount * (sgst_rate / 100.0), 2)
            igst_amount = round(taxable_amount * (igst_rate / 100.0), 2)
            total_line_amount = round(taxable_amount + cgst_amount + sgst_amount + igst_amount, 2)

        return {
            "discount_amount": discount_amount,
            "taxable_amount": taxable_amount,
            "cgst_amount": cgst_amount,
            "sgst_amount": sgst_amount,
            "igst_amount": igst_amount,
            "total_amount": total_line_amount,
        }

    def process_sales_invoice(self, sale: Sale) -> int:
        """
        Process and persist sales invoice atomically (returns sale_id).
        For idempotency replay status, see process_sales_invoice_with_status().
        """
        sale_id, _ = self.process_sales_invoice_with_status(sale)
        return sale_id

    def process_sales_invoice_with_status(self, sale: Sale) -> Tuple[int, bool]:
        """
        Process and persist sales invoice atomically inside a BEGIN IMMEDIATE transaction.
        Returns (sale_id, idempotent_replay).
        1. Compute canonical fingerprint from raw client-supplied fields BEFORE any server defaults.
        2. Inside BEGIN IMMEDIATE, check idempotency_key BEFORE any stock check or write.
        3. Validate stock availability and deduct batch current_qty.
        4. Insert sale header (with idempotency_key + request_fingerprint) and line items.
        5. Record stock movement in stock_ledger.
        6. Update customer balance for any due credit amount.
        7. Post double-entry accounting voucher.
        """
        idem_key = sale.idempotency_key
        fingerprint = sale.compute_request_fingerprint() if idem_key else None

        try:
            with self.db.transaction(mode="IMMEDIATE") as conn:
                # 0. Idempotency lookup BEFORE any state-dependent validation or write
                if idem_key:
                    existing = self.sales_repo.get_sale_by_idempotency_key(idem_key, conn=conn)
                    if existing:
                        if existing["request_fingerprint"] == fingerprint:
                            sale.sale_id = int(existing["sale_id"])
                            sale.invoice_no = existing["invoice_no"]
                            return int(existing["sale_id"]), True
                        raise IdempotencyConflictError(
                            f"Idempotency conflict: key '{idem_key}' has already been processed "
                            f"with a different sales payload."
                        )

                # Fill server-defaulted sale_date AFTER fingerprinting & replay check
                if not sale.sale_date or not sale.sale_date.strip():
                    sale.sale_date = datetime.now().strftime("%Y-%m-%d")

                # 1 & 2. Validate and deduct batch stock
                for item in sale.items:
                    batch = self.inventory_repo.get_or_create_batch_for_sale(
                        product_id=item.product_id,
                        batch_id=item.batch_id,
                        batch_no=item.batch_no,
                        sale_qty=item.qty,
                        sale_rate=item.sale_rate,
                        conn=conn,
                    )
                    item.batch_id = batch.batch_id
                    item.batch_no = batch.batch_no
                    if not item.exp_date and batch.exp_date:
                        item.exp_date = batch.exp_date

                    # Deduct batch stock
                    self.inventory_repo.deduct_batch_stock(batch.batch_id, item.qty, conn=conn)

                # 3. Create sales record
                sale_id = self.sales_repo.create_sale(
                    sale, conn=conn, request_fingerprint=fingerprint
                )
                sale.sale_id = sale_id

                # 4. Record stock movements
                for item in sale.items:
                    curr_stock = self.inventory_repo.get_product_total_stock(item.product_id, conn=conn)

                    self.inventory_repo.record_stock_movement(
                        StockLedgerEntry(
                            product_id=item.product_id,
                            batch_id=item.batch_id,
                            transaction_type="SALE",
                            reference_type="INVOICE",
                            reference_id=sale_id,
                            qty_in=0.0,
                            qty_out=item.qty,
                            balance_qty=curr_stock,
                            rate=item.sale_rate,
                            remarks=f"Sale Invoice: {sale.invoice_no}",
                        ),
                        conn=conn,
                    )

                # 5. Update Customer Balance if credit sale
                unpaid_due = sale.net_amount - sale.paid_amount
                if unpaid_due > 0:
                    self.master_repo.update_customer_balance(sale.customer_id, unpaid_due, conn=conn)

                # 6. Post double-entry accounting voucher
                self._post_sales_voucher(sale, sale_id, conn)

                return sale_id, False
        except sqlite3.IntegrityError as exc:
            # Defensive fallback: if unique index on idempotency_key tripped, re-read and resolve cleanly
            if idem_key:
                existing = self.sales_repo.get_sale_by_idempotency_key(idem_key)
                if existing:
                    if existing["request_fingerprint"] == fingerprint:
                        sale.sale_id = int(existing["sale_id"])
                        sale.invoice_no = existing["invoice_no"]
                        return int(existing["sale_id"]), True
                    raise IdempotencyConflictError(
                        f"Idempotency conflict: key '{idem_key}' has already been processed "
                        f"with a different sales payload."
                    ) from exc
            if "sales.invoice_no" in str(exc):
                raise DuplicateInvoiceError(
                    f"Invoice number '{sale.invoice_no}' already exists."
                ) from exc
            raise

    def _post_sales_voucher(self, sale: Sale, sale_id: int, conn) -> None:
        """Post balanced double-entry voucher for sale."""
        sales_ac = self.accounting_repo.get_sales_account(conn=conn)
        debtors_ac = self.accounting_repo.get_debtors_account(conn=conn)
        cash_ac = self.accounting_repo.get_cash_account(conn=conn)
        bank_ac = self.accounting_repo.get_bank_account(conn=conn)
        round_off_ac = self.accounting_repo.get_round_off_account(conn=conn)

        cgst_out_ac = self.accounting_repo.get_output_gst_account("CGST", conn=conn)
        sgst_out_ac = self.accounting_repo.get_output_gst_account("SGST", conn=conn)
        igst_out_ac = self.accounting_repo.get_output_gst_account("IGST", conn=conn)

        entries = []

        # Debit Cash/Bank for paid amount
        if sale.paid_amount > 0:
            pay_ac = cash_ac if sale.payment_mode in ("CASH", "SPLIT") else bank_ac
            entries.append(
                LedgerEntry(
                    account_id=pay_ac.account_id,
                    debit_amount=round(sale.paid_amount, 2),
                    credit_amount=0.0,
                    particulars=f"Sale Receipt Inv {sale.invoice_no}",
                )
            )

        # Debit Sundry Debtors for credit/unpaid due amount
        due = round(sale.net_amount - sale.paid_amount, 2)
        if due > 0:
            entries.append(
                LedgerEntry(
                    account_id=debtors_ac.account_id,
                    debit_amount=due,
                    credit_amount=0.0,
                    particulars=f"Credit Sale to Cust ID: {sale.customer_id}",
                )
            )

        # Credit Sales Account with taxable value
        if sale.total_taxable > 0:
            entries.append(
                LedgerEntry(
                    account_id=sales_ac.account_id,
                    debit_amount=0.0,
                    credit_amount=round(sale.total_taxable, 2),
                    particulars="Sales Revenue",
                )
            )

        # Credit Output GST
        if sale.total_cgst > 0 and cgst_out_ac:
            entries.append(
                LedgerEntry(
                    account_id=cgst_out_ac.account_id,
                    debit_amount=0.0,
                    credit_amount=round(sale.total_cgst, 2),
                    particulars="Output CGST",
                )
            )
        if sale.total_sgst > 0 and sgst_out_ac:
            entries.append(
                LedgerEntry(
                    account_id=sgst_out_ac.account_id,
                    debit_amount=0.0,
                    credit_amount=round(sale.total_sgst, 2),
                    particulars="Output SGST",
                )
            )
        if sale.total_igst > 0 and igst_out_ac:
            entries.append(
                LedgerEntry(
                    account_id=igst_out_ac.account_id,
                    debit_amount=0.0,
                    credit_amount=round(sale.total_igst, 2),
                    particulars="Output IGST",
                )
            )

        # Round-off balancing to guarantee sum(dr) == sum(cr)
        credit_components = round(
            sale.total_taxable
            + (sale.total_cgst or 0.0)
            + (sale.total_sgst or 0.0)
            + (sale.total_igst or 0.0),
            2,
        )
        round_diff = round(sale.net_amount - credit_components, 2)
        if round_diff > 0:
            entries.append(
                LedgerEntry(
                    account_id=round_off_ac.account_id,
                    debit_amount=0.0,
                    credit_amount=round_diff,
                    particulars="Round Off Difference",
                )
            )
        elif round_diff < 0:
            entries.append(
                LedgerEntry(
                    account_id=round_off_ac.account_id,
                    debit_amount=abs(round_diff),
                    credit_amount=0.0,
                    particulars="Round Off Difference",
                )
            )

        tot_dr = round(sum(e.debit_amount for e in entries), 2)
        tot_cr = round(sum(e.credit_amount for e in entries), 2)
        if tot_dr != tot_cr or tot_dr == 0:
            raise ValueError(
                f"Unbalanced sales voucher: Total Debit ({tot_dr}) != Total Credit ({tot_cr})"
            )

        vch_no = self.accounting_repo.generate_next_voucher_no(
            "RECEIPT" if due == 0 else "JOURNAL", conn=conn
        )
        vch = Voucher(
            voucher_no=vch_no,
            voucher_date=sale.sale_date,
            voucher_type="RECEIPT" if due == 0 else "JOURNAL",
            total_amount=tot_dr,
            narration=f"Sales Bill #{sale.invoice_no}",
            reference_type="SALE",
            reference_id=sale_id,
            entries=entries,
        )
        self.accounting_repo.create_voucher(vch, conn=conn)

    def _evaluate_sale_return_settlement(
        self,
        orig_sale: Sale,
        customer,
        ret_net_dec: Decimal,
        prior_returned_amount: float,
        prior_refunded_amount: float,
        conn: sqlite3.Connection,
    ) -> Dict[str, Any]:
        """
        Shared Phase 2B-2 settlement & refund-safety evaluator used identically by
        preview_sale_return() and process_sale_return_with_status().
        1. Reduce unpaid receivable first (bounded by remaining invoice due & live customer balance).
        2. For any remaining refund amount > 0, route ONLY when recorded invoice payment data
           reliably establishes a single payment channel (CASH or BANK) with sufficient
           invoice-recorded paid amount. Never infer a channel for CREDIT, SPLIT, or unallocated
           subsequent customer receipts.
        """
        orig_net_dec = Decimal(str(orig_sale.net_amount or 0.0)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        orig_paid_dec = Decimal(str(orig_sale.paid_amount or 0.0)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        orig_due_dec = max(
            Decimal("0.00"),
            (orig_net_dec - orig_paid_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        prior_ret_amt_dec = Decimal(str(prior_returned_amount or 0.0)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        prior_refunded_dec = Decimal(str(prior_refunded_amount or 0.0)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        prior_receivable_reduced = max(Decimal("0.00"), prior_ret_amt_dec - prior_refunded_dec)
        remaining_invoice_due = max(Decimal("0.00"), orig_due_dec - prior_receivable_reduced)
        remaining_invoice_paid = max(Decimal("0.00"), orig_paid_dec - prior_refunded_dec)
        customer_bal_dec = max(
            Decimal("0.00"),
            Decimal(str(customer.current_balance or 0.0)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
        )

        receivable_reduction = min(ret_net_dec, remaining_invoice_due, customer_bal_dec)
        refund_amount = (ret_net_dec - receivable_reduction).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        refund_account = None
        refund_channel = "NONE"
        settlement_supported = True
        unsupported_reason: Optional[str] = None

        if refund_amount > Decimal("0.00"):
            orig_mode = (orig_sale.payment_mode or "").strip().upper()
            if orig_mode == "CASH":
                if refund_amount > remaining_invoice_paid:
                    settlement_supported = False
                    refund_channel = "UNSUPPORTED"
                    unsupported_reason = (
                        f"Refund method must be specified: refund amount ({refund_amount}) exceeds "
                        f"remaining invoice-recorded CASH payment ({remaining_invoice_paid}) on "
                        f"invoice {orig_sale.invoice_no}. Subsequent customer receipts are not "
                        f"allocated to individual invoices, so the refund channel cannot be safely inferred."
                    )
                else:
                    refund_account = self.accounting_repo.get_cash_account(conn=conn)
                    refund_channel = "CASH"
            elif orig_mode in ("UPI", "CARD", "BANK_TRANSFER", "BANK"):
                if refund_amount > remaining_invoice_paid:
                    settlement_supported = False
                    refund_channel = "UNSUPPORTED"
                    unsupported_reason = (
                        f"Refund method must be specified: refund amount ({refund_amount}) exceeds "
                        f"remaining invoice-recorded {orig_mode} payment ({remaining_invoice_paid}) on "
                        f"invoice {orig_sale.invoice_no}. Subsequent customer receipts are not "
                        f"allocated to individual invoices, so the refund channel cannot be safely inferred."
                    )
                else:
                    refund_account = self.accounting_repo.get_bank_account(conn=conn)
                    refund_channel = "BANK"
            elif orig_mode == "SPLIT":
                settlement_supported = False
                refund_channel = "UNSUPPORTED"
                unsupported_reason = (
                    f"Refund method must be specified: original invoice {orig_sale.invoice_no} "
                    f"was recorded as SPLIT payment without per-channel amounts, so the refund "
                    f"channel for {refund_amount} cannot be reliably determined."
                )
            elif orig_mode == "CREDIT":
                settlement_supported = False
                refund_channel = "UNSUPPORTED"
                unsupported_reason = (
                    f"Refund method must be specified: original invoice {orig_sale.invoice_no} "
                    f"was a CREDIT sale and remaining refund amount ({refund_amount}) exceeds "
                    f"reducible receivable ({receivable_reduction}). Subsequent customer receipts "
                    f"are not reliably linked to this invoice's refund channel."
                )
            else:
                settlement_supported = False
                refund_channel = "UNSUPPORTED"
                unsupported_reason = (
                    f"Refund method must be specified: payment mode '{orig_mode}' on invoice "
                    f"{orig_sale.invoice_no} does not reliably establish a refund channel for {refund_amount}."
                )

        summary_parts: List[str] = []
        if receivable_reduction > Decimal("0.00"):
            summary_parts.append(
                f"Receivable / Credit Adjustment: ₹{float(receivable_reduction):.2f} deducted from customer balance"
            )
        if refund_amount > Decimal("0.00"):
            if settlement_supported and refund_account is not None:
                channel_label = "Cash Refund" if refund_channel == "CASH" else "Bank Refund"
                summary_parts.append(
                    f"{channel_label}: ₹{float(refund_amount):.2f} via {refund_account.account_name}"
                )
            else:
                summary_parts.append(
                    f"Unsupported Refund: ₹{float(refund_amount):.2f} cannot be safely refunded automatically"
                )
        settlement_summary = " + ".join(summary_parts) if summary_parts else "No adjustment required"

        return {
            "receivable_reduction": receivable_reduction,
            "refund_amount": refund_amount,
            "refund_account": refund_account,
            "refund_account_name": refund_account.account_name if refund_account else None,
            "refund_channel": refund_channel,
            "settlement_supported": settlement_supported,
            "settlement_summary": settlement_summary,
            "unsupported_reason": unsupported_reason,
            "remaining_invoice_due": remaining_invoice_due,
            "remaining_invoice_paid": remaining_invoice_paid,
            "customer_current_balance": customer_bal_dec,
        }

    def preview_sale_return(
        self,
        sale_id: int,
        items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Read-only, zero-write authoritative preview of sales return calculations and
        Phase 2B-2 refund/adjustment eligibility. Never mutates stock, balances,
        vouchers, return numbers, or idempotency keys.
        """
        if not sale_id:
            raise ValueError("Original sale invoice ID is required.")
        if not items:
            raise ValueError("Sales return preview requires at least one line item.")

        conn = self.db.get_connection()
        try:
            orig_sale = self.sales_repo.get_sale_by_id(sale_id, conn=conn)
            if not orig_sale:
                raise ValueError(f"Original sale invoice ID #{sale_id} not found.")

            customer = self.master_repo.get_customer_by_id(orig_sale.customer_id, conn=conn)
            if not customer:
                raise ValueError(f"Customer ID #{orig_sale.customer_id} not found.")

            returned_map = self.sales_repo.get_already_returned_qty_map(sale_id, conn=conn)
            returned_batch_map = self.sales_repo.get_already_returned_qty_by_batch_map(
                sale_id, conn=conn
            )
            prior_returned_amount = self.sales_repo.get_prior_returned_amount(
                sale_id, conn=conn
            )
            prior_refunded_amount = self.sales_repo.get_prior_refunded_amount(
                sale_id, conn=conn
            )

            sold_by_prod_batch: Dict[Tuple[int, int], SaleItem] = {}
            sold_by_prod: Dict[int, List[SaleItem]] = {}
            for orig_item in orig_sale.items:
                if orig_item.batch_id:
                    sold_by_prod_batch[(orig_item.product_id, orig_item.batch_id)] = orig_item
                sold_by_prod.setdefault(orig_item.product_id, []).append(orig_item)

            in_req_prod_qty: Dict[int, Decimal] = {}
            in_req_batch_qty: Dict[Tuple[int, int], Decimal] = {}
            sum_taxable = Decimal("0.00")
            sum_cgst = Decimal("0.00")
            sum_sgst = Decimal("0.00")
            sum_igst = Decimal("0.00")
            sum_line_totals = Decimal("0.00")
            preview_lines: List[Dict[str, Any]] = []

            for raw_item in items:
                prod_id = int(raw_item["product_id"] if isinstance(raw_item, dict) else raw_item.product_id)
                raw_batch_id = raw_item.get("batch_id") if isinstance(raw_item, dict) else raw_item.batch_id
                req_qty = float(raw_item["qty"] if isinstance(raw_item, dict) else raw_item.qty)
                req_reason = raw_item.get("reason") if isinstance(raw_item, dict) else raw_item.reason

                if req_qty <= 0:
                    raise ValueError("Return quantity must be greater than 0.")
                if prod_id not in sold_by_prod:
                    raise ValueError(f"Product ID #{prod_id} was not in original invoice.")

                if raw_batch_id:
                    orig_item = sold_by_prod_batch.get((prod_id, int(raw_batch_id)))
                    if orig_item is None:
                        raise ValueError(
                            f"Batch ID #{raw_batch_id} for Product ID #{prod_id} was not in original invoice."
                        )
                else:
                    matching_lines = sold_by_prod[prod_id]
                    if len(matching_lines) > 1:
                        raise ValueError(
                            f"Multiple batches exist for Product ID #{prod_id} in original invoice; batch_id must be specified."
                        )
                    orig_item = matching_lines[0]

                batch_id = int(raw_batch_id or orig_item.batch_id or 0)
                if not batch_id:
                    raise ValueError(f"Batch ID could not be resolved for Product ID #{prod_id}.")

                batch_obj = self.inventory_repo.get_batch_by_id(batch_id, conn=conn)
                if not batch_obj or batch_obj.product_id != prod_id:
                    raise ValueError(f"Stock batch ID #{batch_id} is invalid for Product ID #{prod_id}.")

                ret_qty_dec = Decimal(str(req_qty))
                orig_qty_dec = Decimal(str(orig_item.qty))

                prior_batch_ret = Decimal(str(returned_batch_map.get((prod_id, batch_id), 0.0)))
                req_batch_so_far = in_req_batch_qty.get((prod_id, batch_id), Decimal("0"))
                eligible_batch_qty = orig_qty_dec - prior_batch_ret - req_batch_so_far

                total_sold_prod_qty = sum(Decimal(str(li.qty)) for li in sold_by_prod[prod_id])
                prior_prod_ret = Decimal(str(returned_map.get(prod_id, 0.0)))
                req_prod_so_far = in_req_prod_qty.get(prod_id, Decimal("0"))
                eligible_prod_qty = total_sold_prod_qty - prior_prod_ret - req_prod_so_far

                eligible_qty = min(eligible_batch_qty, eligible_prod_qty)
                if ret_qty_dec.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) > eligible_qty.quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                ):
                    raise ValueError(
                        f"Return qty {req_qty} exceeds eligible qty {float(max(Decimal('0'), eligible_qty))}."
                    )

                in_req_batch_qty[(prod_id, batch_id)] = req_batch_so_far + ret_qty_dec
                in_req_prod_qty[prod_id] = req_prod_so_far + ret_qty_dec

                proportion = ret_qty_dec / orig_qty_dec
                orig_taxable_dec = Decimal(str(orig_item.taxable_amount or 0.0))
                orig_cgst_dec = Decimal(str(orig_item.cgst_amount or 0.0))
                orig_sgst_dec = Decimal(str(orig_item.sgst_amount or 0.0))
                orig_igst_dec = Decimal(str(orig_item.igst_amount or 0.0))

                line_taxable = (orig_taxable_dec * proportion).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                if orig_cgst_dec > 0 or orig_sgst_dec > 0 or orig_igst_dec > 0:
                    line_cgst = (orig_cgst_dec * proportion).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    line_sgst = (orig_sgst_dec * proportion).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    line_igst = (orig_igst_dec * proportion).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                else:
                    orig_cgst_rate = Decimal(str(orig_item.cgst_rate or 0.0))
                    orig_sgst_rate = Decimal(str(orig_item.sgst_rate or 0.0))
                    orig_igst_rate = Decimal(str(orig_item.igst_rate or 0.0))
                    line_cgst = (line_taxable * orig_cgst_rate / Decimal("100")).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    line_sgst = (line_taxable * orig_sgst_rate / Decimal("100")).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    line_igst = (line_taxable * orig_igst_rate / Decimal("100")).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )

                line_tax = (line_cgst + line_sgst + line_igst).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                line_total = (line_taxable + line_tax).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

                sum_taxable += line_taxable
                sum_cgst += line_cgst
                sum_sgst += line_sgst
                sum_igst += line_igst
                sum_line_totals += line_total

                preview_lines.append({
                    "product_id": prod_id,
                    "product_name": orig_item.product_name,
                    "batch_id": batch_id,
                    "batch_no": orig_item.batch_no or batch_obj.batch_no,
                    "qty": float(ret_qty_dec),
                    "sale_rate": float(orig_item.sale_rate),
                    "taxable_amount": float(line_taxable),
                    "cgst_amount": float(line_cgst),
                    "sgst_amount": float(line_sgst),
                    "igst_amount": float(line_igst),
                    "tax_amount": float(line_tax),
                    "total_amount": float(line_total),
                    "reason": req_reason,
                })

            total_tax = (sum_cgst + sum_sgst + sum_igst).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            # If returning 100% of every line on an invoice with no prior returns, mirror original round_off
            is_full_single_return = (
                Decimal(str(prior_returned_amount or 0.0)) == Decimal("0.00")
                and all(
                    in_req_batch_qty.get((li.product_id, int(li.batch_id or 0)), Decimal("0"))
                    .quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                    == Decimal(str(li.qty)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                    for li in orig_sale.items
                )
            )
            if is_full_single_return:
                net_amount = Decimal(str(orig_sale.net_amount or 0.0)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                round_off = (net_amount - (sum_taxable + total_tax)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            else:
                round_off = Decimal("0.00")
                net_amount = (sum_taxable + total_tax).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

            settlement = self._evaluate_sale_return_settlement(
                orig_sale=orig_sale,
                customer=customer,
                ret_net_dec=net_amount,
                prior_returned_amount=prior_returned_amount,
                prior_refunded_amount=prior_refunded_amount,
                conn=conn,
            )

            return {
                "success": True,
                "sale_id": sale_id,
                "invoice_no": orig_sale.invoice_no,
                "sale_date": orig_sale.sale_date,
                "customer_id": orig_sale.customer_id,
                "customer_name": orig_sale.customer_name or customer.customer_name,
                "payment_mode": orig_sale.payment_mode,
                "total_taxable": float(sum_taxable),
                "total_cgst": float(sum_cgst),
                "total_sgst": float(sum_sgst),
                "total_igst": float(sum_igst),
                "total_tax": float(total_tax),
                "round_off": float(round_off),
                "net_amount": float(net_amount),
                "receivable_reduction": float(settlement["receivable_reduction"]),
                "refund_amount": float(settlement["refund_amount"]),
                "refund_channel": settlement["refund_channel"],
                "refund_account_name": settlement["refund_account_name"],
                "settlement_supported": settlement["settlement_supported"],
                "settlement_summary": settlement["settlement_summary"],
                "unsupported_reason": settlement["unsupported_reason"],
                "remaining_invoice_due": float(settlement["remaining_invoice_due"]),
                "remaining_invoice_paid": float(settlement["remaining_invoice_paid"]),
                "customer_current_balance": float(settlement["customer_current_balance"]),
                "items": preview_lines,
            }
        finally:
            conn.close()

    def process_sale_return(self, ret: SalesReturn) -> int:
        """
        Process and persist sales return invoice atomically (returns return_id).
        For idempotency replay status, see process_sale_return_with_status().
        """
        return_id, _ = self.process_sale_return_with_status(ret)
        return return_id

    def process_sale_return_with_status(self, ret: SalesReturn) -> Tuple[int, bool]:
        """
        Process and persist sales return invoice atomically inside a BEGIN IMMEDIATE transaction:
        0. Compute canonical fingerprint from raw client-supplied fields BEFORE server defaults;
           inside IMMEDIATE lock, check idempotency_key BEFORE any validation or write.
        1. Validate original sale invoice, customer match, and return lines inside IMMEDIATE lock.
        2. Validate returned quantities (cumulative across prior returns and within this return)
           do not exceed original sold quantities.
        3. Validate target batch exists and belongs to the returned product, restore batch stock,
           and record stock movement in stock_ledger (transaction_type='SALE_RETURN').
        4. Compute Output GST (CGST/SGST/IGST) reversal strictly from original sale item snapshots.
        5. Compute settlement split between receivable reduction (Sundry Debtors + customer balance)
           and Cash/Bank refund without double-counting any amount (Phase 2B-2 refund safety).
        6. Persist return header & items, update customer balance consistently, and post balanced voucher.
        Returns (return_id, idempotent_replay).
        """
        idem_key = ret.idempotency_key
        fingerprint = ret.compute_request_fingerprint() if idem_key else None

        if not ret.sale_id:
            raise ValueError("Original sale invoice ID is required.")
        if not ret.items:
            raise ValueError("Sales return must contain at least one line item.")
        if ret.net_amount is None or ret.net_amount <= 0:
            raise ValueError("Return net amount must be greater than 0.")

        try:
            with self.db.transaction(mode="IMMEDIATE") as conn:
                # 0. Idempotency lookup BEFORE any state-dependent validation or write
                if idem_key:
                    existing = self.accounting_repo.get_voucher_by_idempotency_key(idem_key, conn=conn)
                    if existing is not None:
                        if (
                            existing.get("reference_type") == "SALE_RETURN"
                            and existing.get("request_fingerprint") == fingerprint
                        ):
                            existing_return_id = int(existing["reference_id"])
                            cur = conn.cursor()
                            try:
                                cur.execute(
                                    "SELECT return_no FROM sales_returns WHERE return_id = ?;",
                                    (existing_return_id,),
                                )
                                sr_row = cur.fetchone()
                                if sr_row:
                                    ret.return_no = sr_row["return_no"]
                            finally:
                                cur.close()
                            ret.return_id = existing_return_id
                            return existing_return_id, True
                        raise IdempotencyConflictError(
                            f"Idempotency conflict: key '{idem_key}' has already been processed "
                            f"with a different sales return payload."
                        )

                if not ret.return_date or not ret.return_date.strip():
                    ret.return_date = datetime.now().strftime("%Y-%m-%d")

                orig_sale = self.sales_repo.get_sale_by_id(ret.sale_id, conn=conn)
                if not orig_sale:
                    raise ValueError(f"Original sale invoice ID #{ret.sale_id} not found.")

                if orig_sale.customer_id != ret.customer_id:
                    raise ValueError("Customer ID does not match the original sale invoice.")

                customer = self.master_repo.get_customer_by_id(ret.customer_id, conn=conn)
                if not customer:
                    raise ValueError(f"Customer ID #{ret.customer_id} not found.")

                returned_map = self.sales_repo.get_already_returned_qty_map(ret.sale_id, conn=conn)
                returned_batch_map = self.sales_repo.get_already_returned_qty_by_batch_map(
                    ret.sale_id, conn=conn
                )
                prior_returned_amount = self.sales_repo.get_prior_returned_amount(
                    ret.sale_id, conn=conn
                )
                prior_refunded_amount = self.sales_repo.get_prior_refunded_amount(
                    ret.sale_id, conn=conn
                )

                # Build original sale line lookup maps
                sold_by_prod_batch: Dict[Tuple[int, int], SaleItem] = {}
                sold_by_prod: Dict[int, List[SaleItem]] = {}
                for orig_item in orig_sale.items:
                    if orig_item.batch_id:
                        sold_by_prod_batch[(orig_item.product_id, orig_item.batch_id)] = orig_item
                    sold_by_prod.setdefault(orig_item.product_id, []).append(orig_item)

                # Track quantities accumulated within this single return request
                in_req_prod_qty: Dict[int, Decimal] = {}
                in_req_batch_qty: Dict[Tuple[int, int], Decimal] = {}

                sum_taxable = Decimal("0.00")
                sum_cgst = Decimal("0.00")
                sum_sgst = Decimal("0.00")
                sum_igst = Decimal("0.00")
                resolved_lines: List[Tuple[SalesReturnItem, int]] = []

                for ret_item in ret.items:
                    if ret_item.qty <= 0:
                        raise ValueError("Return quantity must be greater than 0.")

                    if ret_item.product_id not in sold_by_prod:
                        raise ValueError(
                            f"Product ID #{ret_item.product_id} was not in original invoice."
                        )

                    if ret_item.batch_id:
                        orig_item = sold_by_prod_batch.get((ret_item.product_id, ret_item.batch_id))
                        if orig_item is None:
                            raise ValueError(
                                f"Batch ID #{ret_item.batch_id} for Product ID #{ret_item.product_id} "
                                f"was not in original invoice."
                            )
                    else:
                        matching_lines = sold_by_prod[ret_item.product_id]
                        if len(matching_lines) > 1:
                            raise ValueError(
                                f"Multiple batches exist for Product ID #{ret_item.product_id} in original invoice; "
                                f"batch_id must be specified."
                            )
                        orig_item = matching_lines[0]

                    batch_id = ret_item.batch_id or orig_item.batch_id
                    if not batch_id:
                        raise ValueError(
                            f"Batch ID could not be resolved for Product ID #{ret_item.product_id}."
                        )

                    # Validate target stock batch inside the IMMEDIATE transaction
                    batch_obj = self.inventory_repo.get_batch_by_id(batch_id, conn=conn)
                    if not batch_obj:
                        raise ValueError(f"Stock batch ID #{batch_id} not found.")
                    if batch_obj.product_id != ret_item.product_id:
                        raise ValueError(
                            f"Batch ID #{batch_id} belongs to Product ID #{batch_obj.product_id}, "
                            f"not Product ID #{ret_item.product_id}."
                        )

                    # Validate returnable quantity against both per-batch and per-product remaining qty
                    ret_qty_dec = Decimal(str(ret_item.qty))
                    orig_qty_dec = Decimal(str(orig_item.qty))

                    prior_batch_ret = Decimal(
                        str(returned_batch_map.get((ret_item.product_id, batch_id), 0.0))
                    )
                    req_batch_so_far = in_req_batch_qty.get(
                        (ret_item.product_id, batch_id), Decimal("0")
                    )
                    eligible_batch_qty = orig_qty_dec - prior_batch_ret - req_batch_so_far

                    total_sold_prod_qty = sum(
                        Decimal(str(li.qty)) for li in sold_by_prod[ret_item.product_id]
                    )
                    prior_prod_ret = Decimal(str(returned_map.get(ret_item.product_id, 0.0)))
                    req_prod_so_far = in_req_prod_qty.get(ret_item.product_id, Decimal("0"))
                    eligible_prod_qty = total_sold_prod_qty - prior_prod_ret - req_prod_so_far

                    eligible_qty = min(eligible_batch_qty, eligible_prod_qty)
                    if ret_qty_dec.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) > eligible_qty.quantize(
                        Decimal("0.0001"), rounding=ROUND_HALF_UP
                    ):
                        raise ValueError(
                            f"Return qty {ret_item.qty} exceeds eligible qty {float(max(Decimal('0'), eligible_qty))}."
                        )

                    in_req_batch_qty[(ret_item.product_id, batch_id)] = req_batch_so_far + ret_qty_dec
                    in_req_prod_qty[ret_item.product_id] = req_prod_so_far + ret_qty_dec
                    ret_item.batch_id = batch_id

                    # Compute taxable and Output GST reversal strictly from original sale item snapshot
                    proportion = ret_qty_dec / orig_qty_dec
                    orig_taxable_dec = Decimal(str(orig_item.taxable_amount or 0.0))
                    orig_cgst_dec = Decimal(str(orig_item.cgst_amount or 0.0))
                    orig_sgst_dec = Decimal(str(orig_item.sgst_amount or 0.0))
                    orig_igst_dec = Decimal(str(orig_item.igst_amount or 0.0))

                    line_taxable = (orig_taxable_dec * proportion).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    if orig_cgst_dec > 0 or orig_sgst_dec > 0 or orig_igst_dec > 0:
                        line_cgst = (orig_cgst_dec * proportion).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                        line_sgst = (orig_sgst_dec * proportion).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                        line_igst = (orig_igst_dec * proportion).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                    else:
                        # Fallback only to original sale line's recorded tax rates (never current product tax)
                        orig_cgst_rate = Decimal(str(orig_item.cgst_rate or 0.0))
                        orig_sgst_rate = Decimal(str(orig_item.sgst_rate or 0.0))
                        orig_igst_rate = Decimal(str(orig_item.igst_rate or 0.0))
                        line_cgst = (line_taxable * orig_cgst_rate / Decimal("100")).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                        line_sgst = (line_taxable * orig_sgst_rate / Decimal("100")).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                        line_igst = (line_taxable * orig_igst_rate / Decimal("100")).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )

                    sum_taxable += line_taxable
                    sum_cgst += line_cgst
                    sum_sgst += line_sgst
                    sum_igst += line_igst

                    resolved_lines.append((ret_item, batch_id))

                # Determine settlement split & validate refund channel BEFORE ANY DATABASE WRITES:
                ret_net_dec = Decimal(str(ret.net_amount)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                settlement = self._evaluate_sale_return_settlement(
                    orig_sale=orig_sale,
                    customer=customer,
                    ret_net_dec=ret_net_dec,
                    prior_returned_amount=prior_returned_amount,
                    prior_refunded_amount=prior_refunded_amount,
                    conn=conn,
                )
                if not settlement["settlement_supported"]:
                    raise ValueError(settlement["unsupported_reason"])

                receivable_reduction = settlement["receivable_reduction"]
                refund_amount = settlement["refund_amount"]
                refund_account = settlement["refund_account"]

                # Restore batch stock and record stock movements atomically (only after all validations pass)
                for ret_item, batch_id in resolved_lines:
                    self.inventory_repo.add_batch_stock(batch_id, ret_item.qty, conn=conn)
                    curr_stock = self.inventory_repo.get_product_total_stock(
                        ret_item.product_id, conn=conn
                    )
                    self.inventory_repo.record_stock_movement(
                        StockLedgerEntry(
                            product_id=ret_item.product_id,
                            batch_id=batch_id,
                            transaction_type="SALE_RETURN",
                            reference_type="RETURN",
                            reference_id=ret.sale_id,
                            qty_in=ret_item.qty,
                            qty_out=0.0,
                            balance_qty=curr_stock,
                            rate=ret_item.sale_rate,
                            remarks=f"Sale Return for Inv {orig_sale.invoice_no}",
                        ),
                        conn=conn,
                    )

                # Persist return record
                return_id = self.sales_repo.create_sale_return(ret, conn=conn)
                ret.return_id = return_id

                # Update customer balance strictly matching the Sundry Debtors credit
                if receivable_reduction > Decimal("0.00"):
                    self.master_repo.update_customer_balance(
                        ret.customer_id, -float(receivable_reduction), conn=conn
                    )

                # Post balanced double-entry accounting voucher on the active connection
                self._post_sale_return_voucher(
                    ret=ret,
                    return_id=return_id,
                    orig_sale=orig_sale,
                    sum_taxable=sum_taxable,
                    sum_cgst=sum_cgst,
                    sum_sgst=sum_sgst,
                    sum_igst=sum_igst,
                    receivable_reduction=receivable_reduction,
                    refund_amount=refund_amount,
                    refund_account=refund_account,
                    conn=conn,
                    idempotency_key=idem_key,
                    request_fingerprint=fingerprint,
                )

                return return_id, False
        except sqlite3.IntegrityError as exc:
            if idem_key and "idempotency_key" in str(exc).lower():
                existing = self.accounting_repo.get_voucher_by_idempotency_key(idem_key)
                if existing is not None:
                    if (
                        existing.get("reference_type") == "SALE_RETURN"
                        and existing.get("request_fingerprint") == fingerprint
                    ):
                        existing_return_id = int(existing["reference_id"])
                        sr_row = self.db.fetch_one(
                            "SELECT return_no FROM sales_returns WHERE return_id = ?;",
                            (existing_return_id,),
                        )
                        if sr_row:
                            ret.return_no = sr_row["return_no"]
                        ret.return_id = existing_return_id
                        return existing_return_id, True
                    raise IdempotencyConflictError(
                        f"Idempotency conflict: key '{idem_key}' has already been processed "
                        f"with a different sales return payload."
                    ) from exc
            raise

    def _post_sale_return_voucher(
        self,
        ret: SalesReturn,
        return_id: int,
        orig_sale: Sale,
        sum_taxable: Decimal,
        sum_cgst: Decimal,
        sum_sgst: Decimal,
        sum_igst: Decimal,
        receivable_reduction: Decimal,
        refund_amount: Decimal,
        refund_account,
        conn,
        idempotency_key: Optional[str] = None,
        request_fingerprint: Optional[str] = None,
    ) -> None:
        """Post balanced double-entry voucher for sale return using caller's active connection."""
        return_ac = self.accounting_repo.get_sales_return_account(conn=conn)
        debtors_ac = self.accounting_repo.get_debtors_account(conn=conn)
        round_off_ac = self.accounting_repo.get_round_off_account(conn=conn)

        cgst_out_ac = self.accounting_repo.get_output_gst_account("CGST", conn=conn)
        sgst_out_ac = self.accounting_repo.get_output_gst_account("SGST", conn=conn)
        igst_out_ac = self.accounting_repo.get_output_gst_account("IGST", conn=conn)

        entries: List[LedgerEntry] = []

        # --- DEBIT ENTRIES (Sales Return contra-revenue + Output GST Reversals) ---
        if sum_taxable > Decimal("0.00"):
            entries.append(
                LedgerEntry(
                    account_id=return_ac.account_id,
                    debit_amount=float(sum_taxable),
                    credit_amount=0.0,
                    particulars=f"Sale Return for Inv {orig_sale.invoice_no}",
                )
            )

        if sum_cgst > Decimal("0.00"):
            if not cgst_out_ac:
                raise ValueError("CGST Output Account not configured.")
            entries.append(
                LedgerEntry(
                    account_id=cgst_out_ac.account_id,
                    debit_amount=float(sum_cgst),
                    credit_amount=0.0,
                    particulars=f"Output CGST Reversal for Inv {orig_sale.invoice_no}",
                )
            )

        if sum_sgst > Decimal("0.00"):
            if not sgst_out_ac:
                raise ValueError("SGST Output Account not configured.")
            entries.append(
                LedgerEntry(
                    account_id=sgst_out_ac.account_id,
                    debit_amount=float(sum_sgst),
                    credit_amount=0.0,
                    particulars=f"Output SGST Reversal for Inv {orig_sale.invoice_no}",
                )
            )

        if sum_igst > Decimal("0.00"):
            if not igst_out_ac:
                raise ValueError("IGST Output Account not configured.")
            entries.append(
                LedgerEntry(
                    account_id=igst_out_ac.account_id,
                    debit_amount=float(sum_igst),
                    credit_amount=0.0,
                    particulars=f"Output IGST Reversal for Inv {orig_sale.invoice_no}",
                )
            )

        # --- CREDIT ENTRIES (Sundry Debtors reduction and/or verified Cash/Bank refund) ---
        if receivable_reduction > Decimal("0.00"):
            entries.append(
                LedgerEntry(
                    account_id=debtors_ac.account_id,
                    debit_amount=0.0,
                    credit_amount=float(receivable_reduction),
                    particulars=f"Receivable adjustment for Customer #{ret.customer_id} (Inv {orig_sale.invoice_no})",
                )
            )

        if refund_amount > Decimal("0.00"):
            if refund_account is None:
                raise ValueError(
                    f"Refund method must be specified: no verified refund account for {refund_amount}."
                )
            entries.append(
                LedgerEntry(
                    account_id=refund_account.account_id,
                    debit_amount=0.0,
                    credit_amount=float(refund_amount),
                    particulars=f"Refund ({refund_account.account_name}) for Customer #{ret.customer_id} (Inv {orig_sale.invoice_no})",
                )
            )

        # --- ROUND-OFF BALANCING (exact mirror of _post_sales_voucher) ---
        ret_net_dec = (receivable_reduction + refund_amount).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        debit_components = (sum_taxable + sum_cgst + sum_sgst + sum_igst).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        round_diff = (ret_net_dec - debit_components).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if round_diff > Decimal("0.00"):
            entries.append(
                LedgerEntry(
                    account_id=round_off_ac.account_id,
                    debit_amount=float(round_diff),
                    credit_amount=0.0,
                    particulars="Round Off Difference",
                )
            )
        elif round_diff < Decimal("0.00"):
            entries.append(
                LedgerEntry(
                    account_id=round_off_ac.account_id,
                    debit_amount=0.0,
                    credit_amount=float(abs(round_diff)),
                    particulars="Round Off Difference",
                )
            )

        tot_dr = round(sum(e.debit_amount for e in entries), 2)
        tot_cr = round(sum(e.credit_amount for e in entries), 2)
        if tot_dr != tot_cr or tot_dr == 0:
            raise ValueError(
                f"Unbalanced sale return voucher: Total Debit ({tot_dr}) != Total Credit ({tot_cr})"
            )

        vch_no = self.accounting_repo.generate_next_voucher_no("JOURNAL", conn=conn)
        vch = Voucher(
            voucher_no=vch_no,
            voucher_date=ret.return_date,
            voucher_type="JOURNAL",
            total_amount=tot_dr,
            narration=f"Sale Return #{ret.return_no} for Inv {orig_sale.invoice_no}",
            reference_type="SALE_RETURN",
            reference_id=return_id,
            entries=entries,
        )
        self.accounting_repo.create_voucher(
            vch,
            conn=conn,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )

