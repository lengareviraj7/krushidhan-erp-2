"""
Service Layer for Inward Purchase Processing, Stock Inwarding, and Supplier Accounting.
"""
from __future__ import annotations

from datetime import datetime
import sqlite3
from typing import Optional, Tuple
from src.db.connection import DatabaseManager, get_db_manager
from src.models.accounting import LedgerEntry, Voucher
from src.models.inventory import StockBatch, StockLedgerEntry
from src.models.purchase import Purchase
from src.repositories.accounting_repository import AccountingRepository
from src.repositories.inventory_repository import InventoryRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.repositories.purchase_repository import PurchaseRepository
from src.services.idempotency import IdempotencyConflictError


class PurchaseService:
    """Business logic for inward purchase recording, batch updating, and accounting postings."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or get_db_manager()
        self.purchase_repo = PurchaseRepository(self.db)
        self.inventory_repo = InventoryRepository(self.db)
        self.master_repo = MasterDataRepository(self.db)
        self.accounting_repo = AccountingRepository(self.db)

    def process_inward_purchase(self, purchase: Purchase) -> int:
        """
        Processes inward purchase invoice atomically (returns purchase_id).
        For idempotency replay status, see process_inward_purchase_with_status().
        """
        purchase_id, _ = self.process_inward_purchase_with_status(purchase)
        return purchase_id

    def process_inward_purchase_with_status(self, purchase: Purchase) -> Tuple[int, bool]:
        """
        Processes inward purchase invoice atomically inside a BEGIN IMMEDIATE transaction.
        Returns (purchase_id, idempotent_replay).
        1. Compute canonical fingerprint from raw client-supplied fields BEFORE any server defaults.
        2. Inside BEGIN IMMEDIATE, check idempotency_key BEFORE any stock, ledger, balance, or voucher write.
        3. Inserts purchase header (with idempotency_key + request_fingerprint) & items.
        4. Upserts stock batches (increases stock by qty + free_qty).
        5. Records stock ledger entries.
        6. Updates supplier balance.
        7. Posts double-entry accounting vouchers.
        """
        idem_key = purchase.idempotency_key
        fingerprint = purchase.compute_request_fingerprint() if idem_key else None

        try:
            with self.db.transaction(mode="IMMEDIATE") as conn:
                # 0. Idempotency lookup BEFORE any state-dependent validation or write
                if idem_key:
                    existing = self.purchase_repo.get_purchase_by_idempotency_key(
                        idem_key, conn=conn
                    )
                    if existing:
                        if existing["request_fingerprint"] == fingerprint:
                            purchase.purchase_id = int(existing["purchase_id"])
                            purchase.invoice_no = existing["invoice_no"]
                            return int(existing["purchase_id"]), True
                        raise IdempotencyConflictError(
                            f"Idempotency conflict: key '{idem_key}' has already been processed "
                            f"with a different purchase payload."
                        )

                # Fill server-defaulted purchase_date AFTER fingerprinting & replay check
                if not purchase.purchase_date or not purchase.purchase_date.strip():
                    purchase.purchase_date = datetime.now().strftime("%Y-%m-%d")

                # 1. Persist Purchase Invoice
                purchase_id = self.purchase_repo.create_purchase(
                    purchase, conn=conn, request_fingerprint=fingerprint
                )
                purchase.purchase_id = purchase_id

                # 2 & 3. Update stock batches & record stock movements
                for item in purchase.items:
                    total_inward_qty = item.qty + item.free_qty
                    batch = StockBatch(
                        product_id=item.product_id,
                        batch_no=item.batch_no,
                        mfg_date=item.mfg_date,
                        exp_date=item.exp_date,
                        purchase_rate=item.purchase_rate,
                        sale_rate=item.sale_rate,
                        mrp=item.mrp,
                        current_qty=total_inward_qty,
                    )
                    batch_id = self.inventory_repo.upsert_batch(batch, conn=conn)

                    # Get product current balance for ledger
                    curr_stock = self.inventory_repo.get_product_total_stock(item.product_id, conn=conn)

                    self.inventory_repo.record_stock_movement(
                        StockLedgerEntry(
                            product_id=item.product_id,
                            batch_id=batch_id,
                            transaction_type="PURCHASE",
                            reference_type="INVOICE",
                            reference_id=purchase_id,
                            qty_in=total_inward_qty,
                            qty_out=0.0,
                            balance_qty=curr_stock,
                            rate=item.purchase_rate,
                            remarks=f"Inward Inv: {purchase.invoice_no}",
                        ),
                        conn=conn,
                    )

                # 4. Update Supplier Balance (Payable increases by unpaid due amount)
                unpaid_amount = purchase.net_amount - purchase.paid_amount
                if unpaid_amount > 0:
                    self.master_repo.update_supplier_balance(purchase.supplier_id, unpaid_amount, conn=conn)

                # 5. Post Accounting Voucher
                self._post_purchase_voucher(purchase, purchase_id, conn)

                return purchase_id, False
        except sqlite3.IntegrityError as exc:
            # Defensive fallback: if unique index on idempotency_key tripped, re-read and resolve cleanly
            if idem_key:
                existing = self.purchase_repo.get_purchase_by_idempotency_key(idem_key)
                if existing:
                    if existing["request_fingerprint"] == fingerprint:
                        purchase.purchase_id = int(existing["purchase_id"])
                        purchase.invoice_no = existing["invoice_no"]
                        return int(existing["purchase_id"]), True
                    raise IdempotencyConflictError(
                        f"Idempotency conflict: key '{idem_key}' has already been processed "
                        f"with a different purchase payload."
                    ) from exc
            raise

    def _post_purchase_voucher(self, purchase: Purchase, purchase_id: int, conn) -> None:
        """Post balanced double-entry voucher for purchase."""
        purchase_ac = self.accounting_repo.get_purchase_account(conn=conn)
        creditors_ac = self.accounting_repo.get_creditors_account(conn=conn)
        cash_ac = self.accounting_repo.get_cash_account(conn=conn)
        bank_ac = self.accounting_repo.get_bank_account(conn=conn)
        round_off_ac = self.accounting_repo.get_round_off_account(conn=conn)

        cgst_in_ac = self.accounting_repo.get_input_gst_account("CGST", conn=conn)
        sgst_in_ac = self.accounting_repo.get_input_gst_account("SGST", conn=conn)
        igst_in_ac = self.accounting_repo.get_input_gst_account("IGST", conn=conn)

        entries = []
        # Debit Purchase Account with taxable amount
        if purchase.total_taxable > 0:
            entries.append(
                LedgerEntry(
                    account_id=purchase_ac.account_id,
                    debit_amount=round(purchase.total_taxable, 2),
                    credit_amount=0.0,
                    particulars=f"Purchase Inv {purchase.invoice_no}",
                )
            )

        # Debit Input GST
        if purchase.total_cgst > 0 and cgst_in_ac:
            entries.append(
                LedgerEntry(
                    account_id=cgst_in_ac.account_id,
                    debit_amount=round(purchase.total_cgst, 2),
                    credit_amount=0.0,
                    particulars="Input CGST",
                )
            )
        if purchase.total_sgst > 0 and sgst_in_ac:
            entries.append(
                LedgerEntry(
                    account_id=sgst_in_ac.account_id,
                    debit_amount=round(purchase.total_sgst, 2),
                    credit_amount=0.0,
                    particulars="Input SGST",
                )
            )
        if purchase.total_igst > 0 and igst_in_ac:
            entries.append(
                LedgerEntry(
                    account_id=igst_in_ac.account_id,
                    debit_amount=round(purchase.total_igst, 2),
                    credit_amount=0.0,
                    particulars="Input IGST",
                )
            )

        # Credit Cash/Bank if paid
        if purchase.paid_amount > 0:
            pay_ac = cash_ac if purchase.payment_type in ("CASH", "SPLIT") else bank_ac
            entries.append(
                LedgerEntry(
                    account_id=pay_ac.account_id,
                    debit_amount=0.0,
                    credit_amount=round(purchase.paid_amount, 2),
                    particulars="Paid for Purchase",
                )
            )

        # Credit Sundry Creditors for credit/unpaid due amount
        due = round(purchase.net_amount - purchase.paid_amount, 2)
        if due > 0:
            entries.append(
                LedgerEntry(
                    account_id=creditors_ac.account_id,
                    debit_amount=0.0,
                    credit_amount=due,
                    particulars=f"Supplier Credit Due (Supp ID: {purchase.supplier_id})",
                )
            )

        # Round-off balancing to guarantee sum(dr) == sum(cr)
        debit_components = round(
            purchase.total_taxable
            + (purchase.total_cgst or 0.0)
            + (purchase.total_sgst or 0.0)
            + (purchase.total_igst or 0.0),
            2,
        )
        round_diff = round(purchase.net_amount - debit_components, 2)
        if round_diff > 0:
            entries.append(
                LedgerEntry(
                    account_id=round_off_ac.account_id,
                    debit_amount=round_diff,
                    credit_amount=0.0,
                    particulars="Round Off Difference",
                )
            )
        elif round_diff < 0:
            entries.append(
                LedgerEntry(
                    account_id=round_off_ac.account_id,
                    debit_amount=0.0,
                    credit_amount=abs(round_diff),
                    particulars="Round Off Difference",
                )
            )

        # Verify entry balance before inserting
        tot_dr = round(sum(e.debit_amount for e in entries), 2)
        tot_cr = round(sum(e.credit_amount for e in entries), 2)
        if tot_dr != tot_cr or tot_dr == 0:
            raise ValueError(
                f"Unbalanced purchase voucher: Total Debit ({tot_dr}) != Total Credit ({tot_cr})"
            )

        vch_no = self.accounting_repo.generate_next_voucher_no("JOURNAL", conn=conn)
        vch = Voucher(
            voucher_no=vch_no,
            voucher_date=purchase.purchase_date,
            voucher_type="JOURNAL",
            total_amount=tot_dr,
            narration=f"Purchase from Supplier #{purchase.supplier_id} Inv: {purchase.invoice_no}",
            reference_type="PURCHASE",
            reference_id=purchase_id,
            entries=entries,
        )
        self.accounting_repo.create_voucher(vch, conn=conn)
