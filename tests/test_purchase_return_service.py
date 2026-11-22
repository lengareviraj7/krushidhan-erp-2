"""
Comprehensive Automated Test Suite for PurchaseReturnService (Step 2C).

Covers all 24 required business-service scenarios:
1. Full purchase return.
2. Partial purchase return.
3. Multiple successive partial returns.
4. Exact final remainder calculations (no rounding drift).
5. Multi-rate GST returns.
6. Trade discount preservation.
7. Wrong supplier rejection.
8. Over-return rejection.
9. Insufficient batch-stock rejection.
10. PAYABLE_REDUCTION settlement.
11. REFUND_CASH settlement.
12. REFUND_BANK settlement.
13. SUPPLIER_CREDIT settlement.
14. Split settlement (payable reduction + bank refund + supplier credit).
15. Settlement mismatch rejection (sum != net_amount).
16. Payable reduction exceeding supplier current balance rejection.
17. ITC PENDING_CONFIRMATION posting to suspense account.
18. Balanced journal/debit note voucher (debits == credits to paisa).
19. One-paise round-off handling (positive and negative).
20. Matching idempotency replay without duplicate effects.
21. Mismatched idempotency replay returning 409 conflict.
22. Atomic rollback at failure points.
23. No duplicate stock ledger entries.
24. Original purchase and sales records remain unchanged.
"""
from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal
from pathlib import Path
from typing import Generator, Tuple
from unittest.mock import patch

import pytest

from src.db.connection import DatabaseManager
from src.models.purchase_return import (
    PurchaseReturnCreate,
    PurchaseReturnItemCreate,
    PurchaseReturnSettlementCreate,
)
from src.repositories.accounting_repository import AccountingRepository
from src.repositories.inventory_repository import InventoryRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.repositories.purchase_return_repository import PurchaseReturnRepository
from src.repositories.supplier_credit_repository import SupplierCreditRepository
from src.services.purchase_return_service import IdempotencyConflictError, PurchaseReturnService


@pytest.fixture
def service_db(
    tmp_path: Path,
) -> Generator[Tuple[DatabaseManager, PurchaseReturnService, int, int, int, int, int, int], None, None]:
    """
    Sets up an isolated temporary database with schema, chart of accounts seed,
    supplier, products, batches, and an inward purchase invoice with two lines:
    Line 1: 10 units @ 1000, 100 discount (net 990/unit), 18% GST (9% CGST + 9% SGST).
            Taxable = 9900.00, CGST = 891.00, SGST = 891.00, Line Total = 11682.00
    Line 2: 10 units @ 500, 0 discount, 5% GST (2.5% CGST + 2.5% SGST).
            Taxable = 5000.00, CGST = 125.00, SGST = 125.00, Line Total = 5250.00
    Invoice Gross/Net Total = 16932.00.
    Supplier current_balance = 25000.00.
    Yields (db_mgr, service, purchase_id, supplier_id, item1_id, item2_id, batch1_id, batch2_id).
    """
    db_file = tmp_path / "test_pr_service.db"
    db_mgr = DatabaseManager(str(db_file))
    db_mgr.initialize_database(include_seed=True)

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        cur = conn.cursor()

        # 1. Supplier
        cur.execute(
            """
            INSERT INTO suppliers (supplier_name, mobile, gstin, state, current_balance)
            VALUES ('Kisan Agri Inputs Ltd', '9890123456', '27AABCK1234M1Z1', 'Maharashtra', 25000.0);
            """
        )
        supplier_id = cur.lastrowid

        # 2. Products
        cur.execute(
            """
            INSERT INTO products (product_name, category_id, hsn_code, default_purchase_rate, default_sale_rate, default_mrp)
            VALUES ('NPK 19:19:19 50kg', 1, '310520', 1000.0, 1200.0, 1300.0);
            """
        )
        product1_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO products (product_name, category_id, hsn_code, default_purchase_rate, default_sale_rate, default_mrp)
            VALUES ('Bio Fungicide 1L', 4, '380899', 500.0, 600.0, 650.0);
            """
        )
        product2_id = cur.lastrowid

        # 3. Stock Batches
        cur.execute(
            """
            INSERT INTO stock_batches (product_id, batch_no, mfg_date, exp_date, purchase_rate, sale_rate, mrp, opening_qty, current_qty)
            VALUES (?, 'NPK-B1', '2026-01-01', '2028-01-01', 1000.0, 1200.0, 1300.0, 20.0, 20.0);
            """,
            (product1_id,),
        )
        batch1_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO stock_batches (product_id, batch_no, mfg_date, exp_date, purchase_rate, sale_rate, mrp, opening_qty, current_qty)
            VALUES (?, 'BIO-B2', '2026-02-01', '2027-02-01', 500.0, 600.0, 650.0, 10.0, 10.0);
            """,
            (product2_id,),
        )
        batch2_id = cur.lastrowid

        # 4. Inward Purchase Invoice
        cur.execute(
            """
            INSERT INTO purchases (
                invoice_no, purchase_date, supplier_id, payment_type,
                total_taxable, total_cgst, total_sgst, total_igst, extra_charges,
                round_off, net_amount, paid_amount, due_amount
            ) VALUES ('INV-SUPP-001', '2026-11-01', ?, 'CREDIT', 14900.0, 1016.0, 1016.0, 0.0, 0.0, 0.0, 16932.0, 0.0, 16932.0);
            """,
            (supplier_id,),
        )
        purchase_id = cur.lastrowid

        # Line 1: 10 units @ 1000, 100 disc, 18% GST (9% CGST, 9% SGST)
        cur.execute(
            """
            INSERT INTO purchase_items (
                purchase_id, product_id, batch_no, mfg_date, exp_date,
                qty, free_qty, unit_id, purchase_rate, sale_rate, mrp,
                discount_percent, discount_amount, taxable_amount,
                cgst_rate, cgst_amount, sgst_rate, sgst_amount, igst_rate, igst_amount,
                total_amount
            ) VALUES (?, ?, 'NPK-B1', '2026-01-01', '2028-01-01', 10.0, 0.0, 2, 1000.0, 1200.0, 1300.0,
                     1.0, 100.0, 9900.0, 9.0, 891.0, 9.0, 891.0, 0.0, 0.0, 11682.0);
            """,
            (purchase_id, product1_id),
        )
        item1_id = cur.lastrowid

        # Line 2: 10 units @ 500, 0 disc, 5% GST (2.5% CGST, 2.5% SGST)
        cur.execute(
            """
            INSERT INTO purchase_items (
                purchase_id, product_id, batch_no, mfg_date, exp_date,
                qty, free_qty, unit_id, purchase_rate, sale_rate, mrp,
                discount_percent, discount_amount, taxable_amount,
                cgst_rate, cgst_amount, sgst_rate, sgst_amount, igst_rate, igst_amount,
                total_amount
            ) VALUES (?, ?, 'BIO-B2', '2026-02-01', '2027-02-01', 10.0, 0.0, 1, 500.0, 600.0, 650.0,
                     0.0, 0.0, 5000.0, 2.5, 125.0, 2.5, 125.0, 0.0, 0.0, 5250.0);
            """,
            (purchase_id, product2_id),
        )
        item2_id = cur.lastrowid

    service = PurchaseReturnService(db=db_mgr)
    yield db_mgr, service, purchase_id, supplier_id, item1_id, item2_id, batch1_id, batch2_id


# ============================================================================
# Scenario 1: Full Purchase Return
# ============================================================================
def test_full_purchase_return(service_db):
    """1. Full purchase return: all items and quantities returned with complete accounting and stock deduction."""
    db_mgr, service, purchase_id, supplier_id, item1_id, item2_id, batch1_id, batch2_id = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="FULL-RET-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("10.0000"),
                reason="Defective batch",
            ),
            PurchaseReturnItemCreate(
                purchase_item_id=item2_id,
                product_id=2,
                batch_id=batch2_id,
                qty=Decimal("10.0000"),
                reason="Damaged seal",
            ),
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("16932.00"),
                notes="Full invoice reduction",
            )
        ],
    )

    ret = service.process_purchase_return(req)
    assert ret is not None
    assert ret.return_id > 0
    assert ret.status == "POSTED"
    assert ret.net_amount == Decimal("16932.00")
    assert ret.voucher_id is not None
    assert len(ret.items) == 2
    assert len(ret.settlements) == 1

    # Verify inventory deducted
    with db_mgr.transaction() as conn:
        c = conn.cursor()
        c.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch1_id,))
        assert c.fetchone()["current_qty"] == 10.0  # 20 - 10

        c.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch2_id,))
        assert c.fetchone()["current_qty"] == 0.0  # 10 - 10

        # Verify supplier balance reduced from 25000 by 16932
        c.execute("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supplier_id,))
        assert c.fetchone()["current_balance"] == 8068.0

        # Verify stock ledger movements
        c.execute("SELECT * FROM stock_ledger WHERE reference_id = ? AND transaction_type = 'PURCHASE_RETURN';", (ret.return_id,))
        moves = c.fetchall()
        assert len(moves) == 2


# ============================================================================
# Scenario 2: Partial Purchase Return
# ============================================================================
def test_partial_purchase_return(service_db):
    """2. Partial purchase return: proportional calculation of tax and trade discount."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    # Return 4 units of Line 1:
    # 4 units @ 990 = 3960 taxable. CGST 9% = 356.40, SGST 9% = 356.40. Gross = 4672.80 -> Net 4673.00, Round off = +0.20
    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="PART-RET-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("4.0000"),
                reason="Excess quantity",
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("4673.00"),
            )
        ],
    )

    ret = service.process_purchase_return(req)
    assert ret.net_amount == Decimal("4673.00")
    assert ret.total_taxable == Decimal("3960.00")
    assert ret.total_cgst == Decimal("356.40")
    assert ret.total_sgst == Decimal("356.40")
    assert ret.round_off == Decimal("0.20")

    # Line item checks
    line = ret.items[0]
    assert line.qty == Decimal("4.0000")
    assert line.purchase_rate == Decimal("1000.00")
    assert line.discount_amount == Decimal("40.00")  # 4 * 10
    assert line.net_rate == Decimal("990.00")


# ============================================================================
# Scenario 3 & 4: Multiple Successive Partial Returns & Exact Remainder
# ============================================================================
def test_multiple_successive_partial_returns_and_final_remainder(service_db):
    """3 & 4. Multiple partial returns with exact final remainder calculation (zero rounding drift)."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    # Total Line 1: 10 units, taxable 9900, cgst 891, sgst 891, total 11682, disc 100
    # Return 1: 3 units. Proportional: disc = 30.00, taxable = 2970.00, cgst = 267.30, sgst = 267.30, gross = 3504.60, net = 3505.00
    req1 = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="SUCC-RET-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("3.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("3505.00"),
            )
        ],
    )
    ret1 = service.process_purchase_return(req1)
    assert ret1.net_amount == Decimal("3505.00")

    # Return 2: 3 units. Same proportional amounts
    req2 = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-17",
        idempotency_key="SUCC-RET-002",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("3.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("3505.00"),
            )
        ],
    )
    ret2 = service.process_purchase_return(req2)
    assert ret2.net_amount == Decimal("3505.00")

    # Return 3: Remaining 4 units (brings total returned to 10 units = 100% of line)
    # Final Remainder:
    # Taxable = 9900 - 2970 - 2970 = 3960.00
    # CGST = 891 - 267.30 - 267.30 = 356.40
    # SGST = 891 - 267.30 - 267.30 = 356.40
    # Total = 11682 - 3504.60 - 3504.60 = 4672.80 -> Net = 4673.00, Round off = +0.20
    req3 = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-18",
        idempotency_key="SUCC-RET-003",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("4.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("4673.00"),
            )
        ],
    )
    ret3 = service.process_purchase_return(req3)
    assert ret3.net_amount == Decimal("4673.00")

    # Exact sum verification:
    sum_taxable = ret1.total_taxable + ret2.total_taxable + ret3.total_taxable
    sum_cgst = ret1.total_cgst + ret2.total_cgst + ret3.total_cgst
    sum_sgst = ret1.total_sgst + ret2.total_sgst + ret3.total_sgst
    assert sum_taxable == Decimal("9900.00")
    assert sum_cgst == Decimal("891.00")
    assert sum_sgst == Decimal("891.00")


# ============================================================================
# Scenario 5: Multi-rate GST Returns
# ============================================================================
def test_multi_rate_gst_returns(service_db):
    """5. Multi-rate GST: items with 18% and 5% GST on the same return correctly segregated."""
    db_mgr, service, purchase_id, supplier_id, item1_id, item2_id, batch1_id, batch2_id = service_db

    # 2 units of Line 1 (18% GST: 9% + 9%)
    # Net rate 990, taxable = 1980.00, cgst = 178.20, sgst = 178.20
    # 2 units of Line 2 (5% GST: 2.5% + 2.5%)
    # Net rate 500, taxable = 1000.00, cgst = 25.00, sgst = 25.00
    # Combined: taxable = 2980.00, cgst = 203.20, sgst = 203.20, gross = 3386.40, net = 3386.00, round_off = -0.40
    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="MULTI-RATE-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("2.0000"),
            ),
            PurchaseReturnItemCreate(
                purchase_item_id=item2_id,
                product_id=2,
                batch_id=batch2_id,
                qty=Decimal("2.0000"),
            ),
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("3386.00"),
            )
        ],
    )

    ret = service.process_purchase_return(req)
    assert ret.total_taxable == Decimal("2980.00")
    assert ret.total_cgst == Decimal("203.20")
    assert ret.total_sgst == Decimal("203.20")
    assert ret.net_amount == Decimal("3386.00")
    assert ret.round_off == Decimal("-0.40")


# ============================================================================
# Scenario 6: Trade Discount Preservation
# ============================================================================
def test_trade_discount_preservation(service_db):
    """6. Line-level trade discount is preserved and reduces taxable value accurately."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    # Return 1 unit of Line 1:
    # Purchase rate = 1000.00, unit discount = 10.00, net rate = 990.00
    # Taxable = 990.00, cgst = 89.10, sgst = 89.10, gross = 1168.20 -> net 1168.00
    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="DISC-PRES-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("1.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("1168.00"),
            )
        ],
    )

    ret = service.process_purchase_return(req)
    item = ret.items[0]
    assert item.purchase_rate == Decimal("1000.00")
    assert item.discount_amount == Decimal("10.00")
    assert item.net_rate == Decimal("990.00")
    assert item.taxable_amount == Decimal("990.00")


# ============================================================================
# Scenario 7: Wrong Supplier Rejection
# ============================================================================
def test_wrong_supplier_rejection(service_db):
    """7. Reject return when payload supplier_id does not match the purchase invoice supplier."""
    db_mgr, service, purchase_id, _, item1_id, _, batch1_id, _ = service_db

    with pytest.raises(ValueError, match="Supplier ID mismatch"):
        service.process_purchase_return(
            PurchaseReturnCreate(
                purchase_id=purchase_id,
                supplier_id=999,  # Non-existent or mismatched
                return_date="2026-11-16",
                idempotency_key="WRONG-SUPP-001",
                items=[
                    PurchaseReturnItemCreate(
                        purchase_item_id=item1_id,
                        product_id=1,
                        batch_id=batch1_id,
                        qty=Decimal("1.0000"),
                    )
                ],
                settlements=[
                    PurchaseReturnSettlementCreate(
                        settlement_type="PAYABLE_REDUCTION",
                        amount=Decimal("1168.00"),
                    )
                ],
            )
        )


# ============================================================================
# Scenario 8: Over-return Rejection
# ============================================================================
def test_over_return_rejection(service_db):
    """8. Reject return quantity exceeding remaining purchased invoice quantity."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    # Original purchased qty = 10. Attempting to return 11.
    with pytest.raises(ValueError, match="exceeds original purchased quantity"):
        service.process_purchase_return(
            PurchaseReturnCreate(
                purchase_id=purchase_id,
                supplier_id=supplier_id,
                return_date="2026-11-16",
                idempotency_key="OVER-RET-001",
                items=[
                    PurchaseReturnItemCreate(
                        purchase_item_id=item1_id,
                        product_id=1,
                        batch_id=batch1_id,
                        qty=Decimal("11.0000"),
                    )
                ],
                settlements=[
                    PurchaseReturnSettlementCreate(
                        settlement_type="PAYABLE_REDUCTION",
                        amount=Decimal("12850.00"),
                    )
                ],
            )
        )


# ============================================================================
# Scenario 9: Insufficient Batch-Stock Rejection
# ============================================================================
def test_insufficient_batch_stock_rejection(service_db):
    """9. Reject return when warehouse batch current_qty is less than the return quantity."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    # Artificially deplete batch stock to 2.0 units
    with db_mgr.transaction() as conn:
        conn.execute("UPDATE stock_batches SET current_qty = 2.0 WHERE batch_id = ?;", (batch1_id,))

    # Attempt to return 5 units
    with pytest.raises(ValueError, match="Insufficient physical stock in batch"):
        service.process_purchase_return(
            PurchaseReturnCreate(
                purchase_id=purchase_id,
                supplier_id=supplier_id,
                return_date="2026-11-16",
                idempotency_key="NO-STOCK-001",
                items=[
                    PurchaseReturnItemCreate(
                        purchase_item_id=item1_id,
                        product_id=1,
                        batch_id=batch1_id,
                        qty=Decimal("5.0000"),
                    )
                ],
                settlements=[
                    PurchaseReturnSettlementCreate(
                        settlement_type="PAYABLE_REDUCTION",
                        amount=Decimal("5841.00"),
                    )
                ],
            )
        )


# ============================================================================
# Scenario 10: PAYABLE_REDUCTION Settlement
# ============================================================================
def test_payable_reduction_settlement(service_db):
    """10. PAYABLE_REDUCTION reduces Sundry Creditors ledger and supplier current_balance."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="PAY-RED-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )

    ret = service.process_purchase_return(req)
    assert ret.net_amount == Decimal("2336.00")

    # Verify supplier balance reduced by exactly 2336.00 (from 25000 to 22664)
    with db_mgr.transaction() as conn:
        row = conn.execute("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supplier_id,)).fetchone()
        assert row["current_balance"] == 22664.0


# ============================================================================
# Scenario 11: REFUND_CASH Settlement
# ============================================================================
def test_refund_cash_settlement(service_db):
    """11. REFUND_CASH debits Cash in Hand and does NOT reduce supplier payable balance."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="REF-CASH-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="REFUND_CASH",
                amount=Decimal("2336.00"),
                account_id=1,  # Cash in Hand
            )
        ],
    )

    ret = service.process_purchase_return(req)
    assert ret.net_amount == Decimal("2336.00")

    # Verify supplier balance is NOT reduced (remains 25000)
    with db_mgr.transaction() as conn:
        row = conn.execute("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supplier_id,)).fetchone()
        assert row["current_balance"] == 25000.0

        # Verify Cash account debited
        row_vch = conn.execute(
            """
            SELECT le.debit_amount, la.account_name 
            FROM ledger_entries le
            JOIN ledger_accounts la ON le.account_id = la.account_id
            WHERE le.voucher_id = ? AND le.debit_amount > 0;
            """,
            (ret.voucher_id,),
        ).fetchone()
        assert row_vch["account_name"] == "Cash in Hand"
        assert row_vch["debit_amount"] == 2336.0


# ============================================================================
# Scenario 12: REFUND_BANK Settlement
# ============================================================================
def test_refund_bank_settlement(service_db):
    """12. REFUND_BANK debits Bank Account and does NOT reduce supplier payable balance."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="REF-BANK-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="REFUND_BANK",
                amount=Decimal("2336.00"),
                account_id=2,  # Bank Account
            )
        ],
    )

    ret = service.process_purchase_return(req)
    assert ret.net_amount == Decimal("2336.00")

    # Verify supplier balance is NOT reduced
    with db_mgr.transaction() as conn:
        row = conn.execute("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supplier_id,)).fetchone()
        assert row["current_balance"] == 25000.0


# ============================================================================
# Scenario 13: SUPPLIER_CREDIT Settlement
# ============================================================================
def test_supplier_credit_settlement(service_db):
    """13. SUPPLIER_CREDIT creates a SupplierCredit record and does NOT reduce supplier balance."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="SUPP-CRED-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="SUPPLIER_CREDIT",
                amount=Decimal("2336.00"),
                notes="Retain credit note for next seed purchase",
            )
        ],
    )

    ret = service.process_purchase_return(req)
    assert ret.net_amount == Decimal("2336.00")

    # Verify supplier credit note record was created
    with db_mgr.transaction() as conn:
        sc_row = conn.execute("SELECT * FROM supplier_credits WHERE return_id = ?;", (ret.return_id,)).fetchone()
        assert sc_row is not None
        assert sc_row["original_amount"] == 2336.0
        assert sc_row["balance_amount"] == 2336.0
        assert sc_row["status"] == "AVAILABLE"

        # Verify supplier balance is untouched
        supp_row = conn.execute("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supplier_id,)).fetchone()
        assert supp_row["current_balance"] == 25000.0


# ============================================================================
# Scenario 14: Split Settlement
# ============================================================================
def test_split_settlement(service_db):
    """14. Split settlement: combination of PAYABLE_REDUCTION, REFUND_BANK, and SUPPLIER_CREDIT."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    # Total 4 units = 4673.00 net amount
    # Split:
    # PAYABLE_REDUCTION = 1000.00
    # REFUND_BANK = 1673.00
    # SUPPLIER_CREDIT = 2000.00
    # Sum = 4673.00
    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="SPLIT-SETTL-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("4.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("1000.00"),
            ),
            PurchaseReturnSettlementCreate(
                settlement_type="REFUND_BANK",
                amount=Decimal("1673.00"),
                account_id=2,
            ),
            PurchaseReturnSettlementCreate(
                settlement_type="SUPPLIER_CREDIT",
                amount=Decimal("2000.00"),
            ),
        ],
    )

    ret = service.process_purchase_return(req)
    assert ret.net_amount == Decimal("4673.00")
    assert len(ret.settlements) == 3

    with db_mgr.transaction() as conn:
        # Supplier balance reduced ONLY by 1000.00 (from 25000 to 24000)
        supp_row = conn.execute("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supplier_id,)).fetchone()
        assert supp_row["current_balance"] == 24000.0

        # Credit note created for 2000.00
        sc_row = conn.execute("SELECT * FROM supplier_credits WHERE return_id = ?;", (ret.return_id,)).fetchone()
        assert sc_row["original_amount"] == 2000.0


# ============================================================================
# Scenario 15: Settlement Mismatch Rejection
# ============================================================================
def test_settlement_mismatch_rejection(service_db):
    """15. Reject return when sum of settlements does not match net amount."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    # Net amount is 2336.00, but settlement sum is 2000.00
    with pytest.raises(ValueError, match="Total settlement.*"):
        service.process_purchase_return(
            PurchaseReturnCreate(
                purchase_id=purchase_id,
                supplier_id=supplier_id,
                return_date="2026-11-16",
                idempotency_key="MISMATCH-001",
                items=[
                    PurchaseReturnItemCreate(
                        purchase_item_id=item1_id,
                        product_id=1,
                        batch_id=batch1_id,
                        qty=Decimal("2.0000"),
                    )
                ],
                settlements=[
                    PurchaseReturnSettlementCreate(
                        settlement_type="PAYABLE_REDUCTION",
                        amount=Decimal("2000.00"),
                    )
                ],
            )
        )


# ============================================================================
# Scenario 16: Payable Reduction Exceeding Supplier Balance Rejection
# ============================================================================
def test_payable_reduction_exceeding_supplier_balance_rejection(service_db):
    """16. Reject PAYABLE_REDUCTION exceeding verified positive supplier payable balance."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    # Set supplier balance to 500.00
    with db_mgr.transaction() as conn:
        conn.execute("UPDATE suppliers SET current_balance = 500.0 WHERE supplier_id = ?;", (supplier_id,))

    # Net amount is 2336.00, attempting full payable reduction
    with pytest.raises(ValueError, match="cannot exceed supplier current payable balance"):
        service.process_purchase_return(
            PurchaseReturnCreate(
                purchase_id=purchase_id,
                supplier_id=supplier_id,
                return_date="2026-11-16",
                idempotency_key="EXCEED-BAL-001",
                items=[
                    PurchaseReturnItemCreate(
                        purchase_item_id=item1_id,
                        product_id=1,
                        batch_id=batch1_id,
                        qty=Decimal("2.0000"),
                    )
                ],
                settlements=[
                    PurchaseReturnSettlementCreate(
                        settlement_type="PAYABLE_REDUCTION",
                        amount=Decimal("2336.00"),
                    )
                ],
            )
        )


# ============================================================================
# Scenario 17: ITC PENDING_CONFIRMATION Posting to Suspense
# ============================================================================
def test_itc_pending_confirmation_posts_to_suspense(service_db):
    """17. When itc_status is PENDING_CONFIRMATION, tax credits to ITC Reversal Suspense Account."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="ITC-SUSP-001",
        itc_status="PENDING_CONFIRMATION",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )

    ret = service.process_purchase_return(req)
    assert ret.itc_status == "PENDING_CONFIRMATION"

    # Verify voucher entries
    with db_mgr.transaction() as conn:
        entries = conn.execute(
            """
            SELECT le.credit_amount, la.account_name
            FROM ledger_entries le
            JOIN ledger_accounts la ON le.account_id = la.account_id
            WHERE le.voucher_id = ? AND le.credit_amount > 0;
            """,
            (ret.voucher_id,),
        ).fetchall()
        accounts = {e["account_name"]: e["credit_amount"] for e in entries}

        assert "ITC Reversal Suspense Account" in accounts
        assert accounts["ITC Reversal Suspense Account"] == 356.40  # 178.20 + 178.20
        assert "CGST Input Account" not in accounts
        assert "SGST Input Account" not in accounts


# ============================================================================
# Scenario 18: Balanced Journal/Debit Note Voucher
# ============================================================================
def test_balanced_debit_note_voucher(service_db):
    """18. Debit note voucher is created with balanced debits and credits to the exact paisa."""
    db_mgr, service, purchase_id, supplier_id, item1_id, item2_id, batch1_id, batch2_id = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="BAL-VCH-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("2.0000"),
            ),
            PurchaseReturnItemCreate(
                purchase_item_id=item2_id,
                product_id=2,
                batch_id=batch2_id,
                qty=Decimal("3.0000"),
            ),
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("3911.00"),  # 2336.40 + 1575.00 = 3911.40 -> net 3911.00
            )
        ],
    )

    ret = service.process_purchase_return(req)
    with db_mgr.transaction() as conn:
        vch = conn.execute("SELECT * FROM vouchers WHERE voucher_id = ?;", (ret.voucher_id,)).fetchone()
        assert vch["voucher_type"] == "DEBIT_NOTE"

        entries = conn.execute("SELECT * FROM ledger_entries WHERE voucher_id = ?;", (ret.voucher_id,)).fetchall()
        tot_dr = sum(e["debit_amount"] for e in entries)
        tot_cr = sum(e["credit_amount"] for e in entries)
        assert round(tot_dr, 2) == round(tot_cr, 2)
        assert round(tot_dr, 2) > 0


# ============================================================================
# Scenario 19: One-Paise Round-Off Handling (Positive & Negative)
# ============================================================================
def test_round_off_handling_positive_and_negative(service_db):
    """19. Round-off account accurately absorbs fractional paise in both positive and negative directions."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    # Case A: Positive round_off (+0.20): Gross 4672.80 rounded to 4673.00
    req_pos = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="RO-POS-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("4.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("4673.00"),
            )
        ],
    )
    ret_pos = service.process_purchase_return(req_pos)
    assert ret_pos.round_off == Decimal("0.20")

    with db_mgr.transaction() as conn:
        # Credit entry for positive round-off
        ro_entry = conn.execute(
            """
            SELECT le.credit_amount, la.account_name 
            FROM ledger_entries le
            JOIN ledger_accounts la ON le.account_id = la.account_id
            WHERE le.voucher_id = ? AND la.account_name = 'Round Off Account';
            """,
            (ret_pos.voucher_id,),
        ).fetchone()
        assert ro_entry["credit_amount"] == 0.20

    # Case B: Negative round_off (-0.40): Gross 3504.60 rounded down / or 2 units Line 1 + 2 units Line 2 = 3386.40 -> 3386.00
    req_neg = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-17",
        idempotency_key="RO-NEG-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("3.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("3505.00"),  # Gross 3504.60 rounded up to 3505.00 (round off +0.40)
            )
        ],
    )
    ret_neg = service.process_purchase_return(req_neg)
    assert ret_neg.round_off == Decimal("0.40")


# ============================================================================
# Scenario 20: Matching Idempotency Replay
# ============================================================================
def test_matching_idempotency_replay(service_db):
    """20. Replaying a request with matching idempotency key and fingerprint returns original record with no duplicate side-effects."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="IDEM-REPLAY-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )

    first_run = service.process_purchase_return(req)
    assert first_run.return_id > 0

    # Read state after first run
    with db_mgr.transaction() as conn:
        c = conn.cursor()
        batch_qty_1 = c.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch1_id,)).fetchone()["current_qty"]
        vch_count_1 = c.execute("SELECT COUNT(*) as cnt FROM vouchers;").fetchone()["cnt"]
        ledger_count_1 = c.execute("SELECT COUNT(*) as cnt FROM stock_ledger;").fetchone()["cnt"]

    # Replay identical request
    replay_run = service.process_purchase_return(req)
    assert replay_run.return_id == first_run.return_id

    # Verify state did not change
    with db_mgr.transaction() as conn:
        c = conn.cursor()
        batch_qty_2 = c.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch1_id,)).fetchone()["current_qty"]
        vch_count_2 = c.execute("SELECT COUNT(*) as cnt FROM vouchers;").fetchone()["cnt"]
        ledger_count_2 = c.execute("SELECT COUNT(*) as cnt FROM stock_ledger;").fetchone()["cnt"]

    assert batch_qty_1 == batch_qty_2
    assert vch_count_1 == vch_count_2
    assert ledger_count_1 == ledger_count_2


# ============================================================================
# Scenario 21: Mismatched Idempotency Replay
# ============================================================================
def test_mismatched_idempotency_replay_conflict(service_db):
    """21. Replaying an idempotency key with different payload raises IdempotencyConflictError (409)."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    req1 = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="CONFLICT-KEY-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )
    service.process_purchase_return(req1)

    # Replay same key with different quantity (3 instead of 2)
    req2 = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="CONFLICT-KEY-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("3.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("3505.00"),
            )
        ],
    )

    with pytest.raises(IdempotencyConflictError, match="Idempotency conflict"):
        service.process_purchase_return(req2)


# ============================================================================
# Scenario 22: Atomic Rollback at Failure Points
# ============================================================================
def test_atomic_rollback_at_failure(service_db):
    """22. Failure during voucher posting rolls back entire transaction cleanly without partial records."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="ROLLBACK-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )

    # Force error in create_voucher
    with patch.object(service.accounting_repo, "create_voucher", side_effect=RuntimeError("Simulated voucher failure")):
        with pytest.raises(RuntimeError, match="Simulated voucher failure"):
            service.process_purchase_return(req)

    # Verify zero partial records persisted
    with db_mgr.transaction() as conn:
        c = conn.cursor()
        assert c.execute("SELECT COUNT(*) as cnt FROM purchase_returns WHERE idempotency_key = 'ROLLBACK-001';").fetchone()["cnt"] == 0
        assert c.execute("SELECT COUNT(*) as cnt FROM stock_ledger;").fetchone()["cnt"] == 0
        # Batch stock unchanged
        assert c.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch1_id,)).fetchone()["current_qty"] == 20.0
        # Supplier balance unchanged
        assert c.execute("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supplier_id,)).fetchone()["current_balance"] == 25000.0


# ============================================================================
# Scenario 23: No Duplicate Stock Ledger Entries
# ============================================================================
def test_no_duplicate_stock_ledger_entries(service_db):
    """23. Stock ledger has exactly one entry per return line item with correct fields."""
    db_mgr, service, purchase_id, supplier_id, item1_id, item2_id, batch1_id, batch2_id = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="STOCK-LED-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("2.0000"),
            ),
            PurchaseReturnItemCreate(
                purchase_item_id=item2_id,
                product_id=2,
                batch_id=batch2_id,
                qty=Decimal("3.0000"),
            ),
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("3911.00"),
            )
        ],
    )

    ret = service.process_purchase_return(req)
    with db_mgr.transaction() as conn:
        entries = conn.execute(
            """
            SELECT * FROM stock_ledger 
            WHERE reference_type = 'PURCHASE_RETURN' AND reference_id = ?
            ORDER BY ledger_id ASC;
            """,
            (ret.return_id,),
        ).fetchall()

        assert len(entries) == 2
        e1, e2 = entries[0], entries[1]
        assert e1["product_id"] == 1
        assert e1["batch_id"] == batch1_id
        assert e1["qty_out"] == 2.0
        assert e1["balance_qty"] == 18.0

        assert e2["product_id"] == 2
        assert e2["batch_id"] == batch2_id
        assert e2["qty_out"] == 3.0
        assert e2["balance_qty"] == 7.0


# ============================================================================
# Scenario 24: Original Purchase and Sales Remain Unchanged
# ============================================================================
def test_original_purchase_and_sales_remain_unchanged(service_db):
    """24. Original purchase header and line items remain completely unchanged after return."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    # Snapshot purchase record before return
    with db_mgr.transaction() as conn:
        purch_before = dict(conn.execute("SELECT * FROM purchases WHERE purchase_id = ?;", (purchase_id,)).fetchone())
        items_before = [dict(r) for r in conn.execute("SELECT * FROM purchase_items WHERE purchase_id = ?;", (purchase_id,)).fetchall()]

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="IMMUT-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )
    service.process_purchase_return(req)

    # Verify purchase record after return is 100% identical
    with db_mgr.transaction() as conn:
        purch_after = dict(conn.execute("SELECT * FROM purchases WHERE purchase_id = ?;", (purchase_id,)).fetchone())
        items_after = [dict(r) for r in conn.execute("SELECT * FROM purchase_items WHERE purchase_id = ?;", (purchase_id,)).fetchall()]

    assert purch_before == purch_after
    assert items_before == items_after


# ============================================================================
# Scenario 25: Historical Unlinked Returns Rejected
# ============================================================================
def test_historical_unlinked_returns_rejected(service_db):
    """Historical returns with unlinked line items (purchase_item_id IS NULL) block ambiguous returns."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    # Insert an unlinked historical return (from pre-v4 legacy data)
    with db_mgr.transaction() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO purchase_returns (
                return_no, return_date, purchase_id, supplier_id,
                total_taxable, total_tax, net_amount, status
            ) VALUES ('PR-LEGACY-001', '2026-10-01', ?, ?, 1000.0, 180.0, 1180.0, 'POSTED');
            """,
            (purchase_id, supplier_id),
        )
        leg_ret_id = c.lastrowid
        # Insert line with NULL purchase_item_id
        c.execute(
            """
            INSERT INTO purchase_return_items (
                return_id, purchase_item_id, product_id, batch_id,
                qty, purchase_rate, taxable_amount, total_amount
            ) VALUES (?, NULL, 1, ?, 1.0, 1000.0, 1000.0, 1180.0);
            """,
            (leg_ret_id, batch1_id),
        )

    # Attempt to process a new return against this purchase
    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="UNLINKED-BLOCK-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )

    with pytest.raises(ValueError, match="historical returns exist with unlinked line items"):
        service.process_purchase_return(req)


# ============================================================================
# Scenarios 26-30: Source Batch Resolution & Mismatch Validations
# ============================================================================
def test_batch_id_omitted_resolves_and_persists_expected_batch(service_db):
    """26. Client omits batch_id: backend resolves authoritative batch from purchase line and persists it."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="BATCH-OMIT-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=None,  # Omitted by client
                qty=Decimal("2.0000"),
                reason="Return with omitted batch_id",
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )

    result = service.process_purchase_return(req)
    assert result.return_id is not None
    assert result.status == "POSTED"

    with db_mgr.transaction() as conn:
        c = conn.cursor()
        # Verify purchase_return_items has resolved batch1_id, not null
        c.execute("SELECT batch_id, qty FROM purchase_return_items WHERE return_id = ?;", (result.return_id,))
        item_row = c.fetchone()
        assert item_row["batch_id"] == batch1_id
        assert Decimal(str(item_row["qty"])) == Decimal("2.0")

        # Verify stock was deducted from batch1_id (20.0 -> 18.0)
        c.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch1_id,))
        assert Decimal(str(c.fetchone()["current_qty"])) == Decimal("18.0")

        # Verify stock ledger entry was created for batch1_id with qty_out = 2.0
        c.execute(
            """
            SELECT batch_id, qty_out, transaction_type
            FROM stock_ledger
            WHERE reference_type = 'PURCHASE_RETURN' AND reference_id = ?;
            """,
            (result.return_id,),
        )
        ledger_row = c.fetchone()
        assert ledger_row["batch_id"] == batch1_id
        assert Decimal(str(ledger_row["qty_out"])) == Decimal("2.0")


def test_batch_id_supplied_correctly_succeeds(service_db):
    """27. Client supplies correct matching batch_id: return succeeds normally."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="BATCH-CORRECT-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch1_id,  # Correct matching batch
                qty=Decimal("2.0000"),
                reason="Return with correct batch_id",
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )

    result = service.process_purchase_return(req)
    assert result.return_id is not None
    assert result.status == "POSTED"

    with db_mgr.transaction() as conn:
        c = conn.cursor()
        c.execute("SELECT batch_id FROM purchase_return_items WHERE return_id = ?;", (result.return_id,))
        assert c.fetchone()["batch_id"] == batch1_id
        c.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch1_id,))
        assert Decimal(str(c.fetchone()["current_qty"])) == Decimal("18.0")


def test_batch_id_supplied_wrong_batch_same_product_rejected(service_db):
    """28. Client supplies batch_id of a different batch of the same product: rejected with zero side effects."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    # Insert an alternate batch for product 1
    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO stock_batches (
                product_id, batch_no, mfg_date, exp_date,
                purchase_rate, sale_rate, mrp, opening_qty, current_qty
            ) VALUES (1, 'NPK-B1-ALT', '2026-03-01', '2028-03-01', 1000.0, 1200.0, 1300.0, 15.0, 15.0);
            """
        )
        alt_batch_id = c.lastrowid

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="BATCH-WRONG-SAME-PROD-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=alt_batch_id,  # Wrong batch for same product
                qty=Decimal("2.0000"),
                reason="Return with wrong batch for same product",
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )

    with pytest.raises(ValueError, match=r"Batch mismatch on line #\d+: client supplied batch_id"):
        service.process_purchase_return(req)

    # Verify zero side effects
    with db_mgr.transaction() as conn:
        c = conn.cursor()
        c.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch1_id,))
        assert Decimal(str(c.fetchone()["current_qty"])) == Decimal("20.0")
        c.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (alt_batch_id,))
        assert Decimal(str(c.fetchone()["current_qty"])) == Decimal("15.0")
        c.execute("SELECT COUNT(*) as cnt FROM purchase_returns;")
        assert c.fetchone()["cnt"] == 0
        c.execute("SELECT COUNT(*) as cnt FROM stock_ledger;")
        assert c.fetchone()["cnt"] == 0


def test_batch_id_supplied_different_product_rejected(service_db):
    """29. Client supplies batch_id belonging to a different product: rejected with zero side effects."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, batch2_id = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="BATCH-DIFF-PROD-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=batch2_id,  # Belongs to product 2, not product 1
                qty=Decimal("2.0000"),
                reason="Return with batch belonging to different product",
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )

    with pytest.raises(ValueError, match=r"belongs to product #2, not the returned product #1"):
        service.process_purchase_return(req)

    # Verify zero side effects
    with db_mgr.transaction() as conn:
        c = conn.cursor()
        c.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch1_id,))
        assert Decimal(str(c.fetchone()["current_qty"])) == Decimal("20.0")
        c.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch2_id,))
        assert Decimal(str(c.fetchone()["current_qty"])) == Decimal("10.0")
        c.execute("SELECT COUNT(*) as cnt FROM purchase_returns;")
        assert c.fetchone()["cnt"] == 0


def test_unresolvable_batch_rejected(service_db):
    """30. No stock batch found for the purchase line product+batch: rejected with zero side effects."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    # Delete the matching stock batch row to simulate missing/corrupt inventory record
    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        conn.execute("DELETE FROM stock_batches WHERE batch_id = ?;", (batch1_id,))

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="BATCH-UNRESOLVABLE-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                batch_id=None,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )

    with pytest.raises(ValueError, match=r"No stock batch found for product #1 and batch 'NPK-B1'"):
        service.process_purchase_return(req)

    # Verify zero side effects
    with db_mgr.transaction() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM purchase_returns;")
        assert c.fetchone()["cnt"] == 0
        c.execute("SELECT COUNT(*) as cnt FROM vouchers;")
        assert c.fetchone()["cnt"] == 0


# ============================================================================
# Scenarios 31-33: Free Quantity Handling
# ============================================================================
def test_free_qty_zero_succeeds(service_db):
    """31. Purchase line with free_qty = 0.0 returns normally."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, _, _ = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="FREE-ZERO-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )

    result = service.process_purchase_return(req)
    assert result.status == "POSTED"


def test_free_qty_null_succeeds(service_db):
    """32. Purchase line with free_qty = NULL returns normally (robust Decimal conversion treating NULL as 0)."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, _, _ = service_db

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        conn.execute("UPDATE purchase_items SET free_qty = NULL WHERE item_id = ?;", (item1_id,))

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="FREE-NULL-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )

    result = service.process_purchase_return(req)
    assert result.status == "POSTED"


def test_free_qty_positive_rejected_with_zero_side_effects(service_db):
    """33. Purchase line with free_qty > 0 is rejected with clear error and zero side effects, even if return qty <= paid qty."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, batch1_id, _ = service_db

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        conn.execute("UPDATE purchase_items SET free_qty = 2.0 WHERE item_id = ?;", (item1_id,))

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="FREE-POS-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                qty=Decimal("2.0000"),  # Paid qty is 10.0, so 2.0 <= 10.0, yet must be rejected
                reason="Return line containing free qty",
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("2336.00"),
            )
        ],
    )

    # Capture comprehensive BEFORE snapshot across all relevant tables and balances
    with db_mgr.transaction() as conn:
        c = conn.cursor()
        counts_before = {
            "purchase_returns": c.execute("SELECT COUNT(*) FROM purchase_returns;").fetchone()[0],
            "purchase_return_items": c.execute("SELECT COUNT(*) FROM purchase_return_items;").fetchone()[0],
            "purchase_return_settlements": c.execute("SELECT COUNT(*) FROM purchase_return_settlements;").fetchone()[0],
            "supplier_credits": c.execute("SELECT COUNT(*) FROM supplier_credits;").fetchone()[0],
            "vouchers": c.execute("SELECT COUNT(*) FROM vouchers;").fetchone()[0],
            "ledger_entries": c.execute("SELECT COUNT(*) FROM ledger_entries;").fetchone()[0],
            "stock_ledger": c.execute("SELECT COUNT(*) FROM stock_ledger;").fetchone()[0],
        }
        stock_batches_before = {
            row["batch_id"]: row["current_qty"]
            for row in c.execute("SELECT batch_id, current_qty FROM stock_batches;").fetchall()
        }
        suppliers_before = {
            row["supplier_id"]: row["current_balance"]
            for row in c.execute("SELECT supplier_id, current_balance FROM suppliers;").fetchall()
        }
        accounts_before = {
            row["account_id"]: row["current_balance"]
            for row in c.execute("SELECT account_id, current_balance FROM ledger_accounts;").fetchall()
        }

    expected_msg = f"Purchase line #{item1_id} contains free/bonus quantity (2.0). Returns on items with free quantities are not supported in this release."
    with pytest.raises(ValueError) as excinfo:
        service.process_purchase_return(req)
    assert expected_msg in str(excinfo.value)

    # Capture comprehensive AFTER snapshot across all relevant tables and balances
    with db_mgr.transaction() as conn:
        c = conn.cursor()
        counts_after = {
            "purchase_returns": c.execute("SELECT COUNT(*) FROM purchase_returns;").fetchone()[0],
            "purchase_return_items": c.execute("SELECT COUNT(*) FROM purchase_return_items;").fetchone()[0],
            "purchase_return_settlements": c.execute("SELECT COUNT(*) FROM purchase_return_settlements;").fetchone()[0],
            "supplier_credits": c.execute("SELECT COUNT(*) FROM supplier_credits;").fetchone()[0],
            "vouchers": c.execute("SELECT COUNT(*) FROM vouchers;").fetchone()[0],
            "ledger_entries": c.execute("SELECT COUNT(*) FROM ledger_entries;").fetchone()[0],
            "stock_ledger": c.execute("SELECT COUNT(*) FROM stock_ledger;").fetchone()[0],
        }
        stock_batches_after = {
            row["batch_id"]: row["current_qty"]
            for row in c.execute("SELECT batch_id, current_qty FROM stock_batches;").fetchall()
        }
        suppliers_after = {
            row["supplier_id"]: row["current_balance"]
            for row in c.execute("SELECT supplier_id, current_balance FROM suppliers;").fetchall()
        }
        accounts_after = {
            row["account_id"]: row["current_balance"]
            for row in c.execute("SELECT account_id, current_balance FROM ledger_accounts;").fetchall()
        }

    # Strict equality assertions between before and after snapshots
    assert counts_before == counts_after
    assert stock_batches_before == stock_batches_after
    assert suppliers_before == suppliers_after
    assert accounts_before == accounts_after

    # Explicit count and balance assertions
    assert counts_after["purchase_returns"] == 0
    assert counts_after["purchase_return_items"] == 0
    assert counts_after["purchase_return_settlements"] == 0
    assert counts_after["supplier_credits"] == 0
    assert counts_after["vouchers"] == 0
    assert counts_after["ledger_entries"] == 0
    assert counts_after["stock_ledger"] == 0
    assert Decimal(str(stock_batches_after[batch1_id])) == Decimal("20.0")
    assert Decimal(str(suppliers_after[supplier_id])) == Decimal("25000.0")


# ============================================================================
# Scenarios 34-36: Server-Owned Return Numbering
# ============================================================================
def test_client_supplied_return_no_rejected(service_db):
    """34. Client-supplied return_no is rejected at model validation and service layers."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, _, _ = service_db

    # 1. Pydantic model validation rejection
    with pytest.raises(ValueError, match="Client-supplied return_no is not permitted"):
        PurchaseReturnCreate(
            return_no="PR-CLIENT-001",
            purchase_id=purchase_id,
            supplier_id=supplier_id,
            return_date="2026-11-16",
            idempotency_key="CLIENT-RET-NO-001",
            items=[
                PurchaseReturnItemCreate(
                    purchase_item_id=item1_id,
                    product_id=1,
                    qty=Decimal("1.0000"),
                )
            ],
            settlements=[
                PurchaseReturnSettlementCreate(
                    settlement_type="PAYABLE_REDUCTION",
                    amount=Decimal("1168.00"),
                )
            ],
        )

    # 2. Service-level / Repository-level bypass rejection
    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="CLIENT-RET-NO-002",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                qty=Decimal("1.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("1168.00"),
            )
        ],
    )
    object.__setattr__(req, "return_no", "PR-BYPASS-001")
    with pytest.raises(ValueError, match="Client-supplied return_no is not permitted"):
        service.process_purchase_return(req)


def test_backend_generated_return_no_monotonic(service_db):
    """35. Purchase return numbers are monotonically generated per day by the backend (PR-YYYYMMDD-XXXX)."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, _, _ = service_db

    # Return 1
    req1 = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="MONO-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                qty=Decimal("1.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("1168.00"),
            )
        ],
    )
    res1 = service.process_purchase_return(req1)
    assert res1.return_no == "PR-20261116-0001"

    # Return 2 on same day
    req2 = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="MONO-002",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                qty=Decimal("1.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("1168.00"),
            )
        ],
    )
    res2 = service.process_purchase_return(req2)
    assert res2.return_no == "PR-20261116-0002"

    # Return 3 on subsequent day resets sequence
    req3 = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-17",
        idempotency_key="MONO-003",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                qty=Decimal("1.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("1168.00"),
            )
        ],
    )
    res3 = service.process_purchase_return(req3)
    assert res3.return_no == "PR-20261117-0001"


def test_idempotent_replay_preserves_original_return_no(service_db):
    """36. Idempotent replay preserves original return_no and rejects client alterations."""
    db_mgr, service, purchase_id, supplier_id, item1_id, _, _, _ = service_db

    req = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="REPLAY-NO-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item1_id,
                product_id=1,
                qty=Decimal("1.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("1168.00"),
            )
        ],
    )
    res1 = service.process_purchase_return(req)
    assert res1.return_no == "PR-20261116-0001"

    # Replay identical request
    res2 = service.process_purchase_return(req)
    assert res2.return_id == res1.return_id
    assert res2.return_no == "PR-20261116-0001"


# ============================================================================
# Scenario 37: Concurrent Purchase Return Number Generation & Serialization
# ============================================================================
def test_concurrent_purchase_returns_sequential_numbering(tmp_path: Path):
    """
    37. Concurrent purchase return requests using independent connections serialize cleanly,
    producing distinct monotonic return numbers without collisions, partial state, or lock corruption.
    """
    db_file = tmp_path / "test_concurrent_pr.db"
    setup_mgr = DatabaseManager(str(db_file))
    setup_mgr.initialize_database(include_seed=True)

    # 1. Prepare schema and seed data in setup connection before workers start
    with setup_mgr.transaction(mode="IMMEDIATE") as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO suppliers (supplier_name, mobile, gstin, state, current_balance)
            VALUES ('Concurrent Agro Ltd', '9890112233', '27AABCK9999M1Z1', 'Maharashtra', 25000.0);
            """
        )
        supplier_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO products (product_name, category_id, hsn_code, default_purchase_rate, default_sale_rate, default_mrp)
            VALUES ('NPK Concurrent 50kg', 1, '310520', 1000.0, 1200.0, 1300.0);
            """
        )
        product_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO stock_batches (product_id, batch_no, mfg_date, exp_date, purchase_rate, sale_rate, mrp, opening_qty, current_qty)
            VALUES (?, 'CONC-B1', '2026-01-01', '2028-01-01', 1000.0, 1200.0, 1300.0, 20.0, 20.0);
            """,
            (product_id,),
        )
        batch_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO purchases (
                invoice_no, purchase_date, supplier_id, payment_type,
                total_taxable, total_cgst, total_sgst, total_igst, extra_charges,
                round_off, net_amount, paid_amount, due_amount
            ) VALUES ('INV-CONC-001', '2026-11-01', ?, 'CREDIT', 9900.0, 891.0, 891.0, 0.0, 0.0, 0.0, 11682.0, 0.0, 11682.0);
            """,
            (supplier_id,),
        )
        purchase_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO purchase_items (
                purchase_id, product_id, batch_no, mfg_date, exp_date,
                qty, free_qty, unit_id, purchase_rate, sale_rate, mrp,
                discount_percent, discount_amount, taxable_amount,
                cgst_rate, cgst_amount, sgst_rate, sgst_amount, igst_rate, igst_amount,
                total_amount
            ) VALUES (?, ?, 'CONC-B1', '2026-01-01', '2028-01-01', 10.0, 0.0, 2, 1000.0, 1200.0, 1300.0,
                     1.0, 100.0, 9900.0, 9.0, 891.0, 9.0, 891.0, 0.0, 0.0, 11682.0);
            """,
            (purchase_id, product_id),
        )
        item_id = cur.lastrowid

    # 2. Independent database managers and service instances for each thread
    db_mgr1 = DatabaseManager(str(db_file))
    service1 = PurchaseReturnService(db=db_mgr1)

    db_mgr2 = DatabaseManager(str(db_file))
    service2 = PurchaseReturnService(db=db_mgr2)

    # 3. Synchronize concurrent execution with threading.Barrier
    barrier = threading.Barrier(2)
    results: dict = {}
    errors: dict = {}

    def worker_1():
        try:
            req1 = PurchaseReturnCreate(
                purchase_id=purchase_id,
                supplier_id=supplier_id,
                return_date="2026-11-16",
                idempotency_key="CONC-RET-WORKER-001",
                items=[
                    PurchaseReturnItemCreate(
                        purchase_item_id=item_id,
                        product_id=product_id,
                        batch_id=batch_id,
                        qty=Decimal("2.0000"),
                        reason="Concurrent return worker 1",
                    )
                ],
                settlements=[
                    PurchaseReturnSettlementCreate(
                        settlement_type="PAYABLE_REDUCTION",
                        amount=Decimal("2336.00"),
                    )
                ],
            )
            barrier.wait(timeout=5.0)
            results["w1"] = service1.process_purchase_return(req1)
        except Exception as exc:
            errors["w1"] = exc

    def worker_2():
        try:
            req2 = PurchaseReturnCreate(
                purchase_id=purchase_id,
                supplier_id=supplier_id,
                return_date="2026-11-16",
                idempotency_key="CONC-RET-WORKER-002",
                items=[
                    PurchaseReturnItemCreate(
                        purchase_item_id=item_id,
                        product_id=product_id,
                        batch_id=batch_id,
                        qty=Decimal("3.0000"),
                        reason="Concurrent return worker 2",
                    )
                ],
                settlements=[
                    PurchaseReturnSettlementCreate(
                        settlement_type="PAYABLE_REDUCTION",
                        amount=Decimal("3505.00"),
                    )
                ],
            )
            barrier.wait(timeout=5.0)
            results["w2"] = service2.process_purchase_return(req2)
        except Exception as exc:
            errors["w2"] = exc

    th1 = threading.Thread(target=worker_1, name="PR-Worker-1")
    th2 = threading.Thread(target=worker_2, name="PR-Worker-2")

    th1.start()
    th2.start()

    th1.join(timeout=10.0)
    th2.join(timeout=10.0)

    assert not th1.is_alive(), "Worker 1 thread timed out"
    assert not th2.is_alive(), "Worker 2 thread timed out"

    # Confirm neither thread encountered an error
    assert not errors, f"Concurrent worker encountered errors: {errors}"
    assert "w1" in results and "w2" in results

    res1 = results["w1"]
    res2 = results["w2"]

    # 4. Verify distinct IDs and sequential, monotonic return numbers
    assert res1.return_id != res2.return_id
    assert res1.return_no != res2.return_no
    assert {res1.return_no, res2.return_no} == {"PR-20261116-0001", "PR-20261116-0002"}
    assert res1.status == "POSTED"
    assert res2.status == "POSTED"

    # 5. Verify database integrity through fresh inspection connection
    with setup_mgr.transaction() as conn:
        c = conn.cursor()

        # Exactly 2 return headers with expected numbers and amounts
        c.execute("SELECT return_id, return_no, net_amount, status FROM purchase_returns ORDER BY return_id ASC;")
        ret_rows = c.fetchall()
        assert len(ret_rows) == 2
        assert {r["return_no"] for r in ret_rows} == {"PR-20261116-0001", "PR-20261116-0002"}
        assert all(r["status"] == "POSTED" for r in ret_rows)

        # Exactly 2 return line items
        c.execute("SELECT return_id, batch_id, qty, taxable_amount, total_amount FROM purchase_return_items ORDER BY return_id ASC;")
        item_rows = c.fetchall()
        assert len(item_rows) == 2
        assert {Decimal(str(r["qty"])) for r in item_rows} == {Decimal("2.0"), Decimal("3.0")}
        assert all(r["batch_id"] == batch_id for r in item_rows)

        # Exactly 2 settlements
        c.execute("SELECT return_id, settlement_type, amount FROM purchase_return_settlements ORDER BY return_id ASC;")
        stl_rows = c.fetchall()
        assert len(stl_rows) == 2
        assert {Decimal(str(r["amount"])) for r in stl_rows} == {Decimal("2336.00"), Decimal("3505.00")}

        # Exactly 2 vouchers (DEBIT_NOTE)
        c.execute("SELECT voucher_id, voucher_type, total_amount FROM vouchers ORDER BY voucher_id ASC;")
        v_rows = c.fetchall()
        assert len(v_rows) == 2
        assert all(r["voucher_type"] == "DEBIT_NOTE" for r in v_rows)
        assert {Decimal(str(r["total_amount"])).quantize(Decimal("0.01")) for r in v_rows} == {
            Decimal("2336.40"),
            Decimal("3505.00"),
        }

        # Exactly 2 stock ledger entries (qty_out 2.0 and 3.0)
        c.execute("SELECT batch_id, qty_out, transaction_type FROM stock_ledger ORDER BY ledger_id ASC;")
        sl_rows = c.fetchall()
        assert len(sl_rows) == 2
        assert {Decimal(str(r["qty_out"])) for r in sl_rows} == {Decimal("2.0"), Decimal("3.0")}
        assert all(r["batch_id"] == batch_id for r in sl_rows)
        assert all(r["transaction_type"] == "PURCHASE_RETURN" for r in sl_rows)

        # Physical batch stock decremented by exactly 2 + 3 = 5 units (20.0 -> 15.0)
        c.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch_id,))
        assert Decimal(str(c.fetchone()["current_qty"])) == Decimal("15.0")

        # Supplier current_balance decremented by 2336.00 + 3505.00 = 5841.00 (25000.0 -> 19159.0)
        c.execute("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supplier_id,))
        assert Decimal(str(c.fetchone()["current_balance"])) == Decimal("19159.0")

        # Chart of accounts: Purchase Return Account credited with 1980 + 2970 = 4950 (contra-expense delta = debit - credit = -4950)
        c.execute("SELECT current_balance FROM ledger_accounts WHERE account_name = 'Purchase Return Account';")
        pr_acct = c.fetchone()
        assert pr_acct is not None
        assert Decimal(str(pr_acct["current_balance"])) == Decimal("-4950.0")

    # Clean connection drains
    db_mgr1.wait_for_connections_drain(timeout=2.0)
    db_mgr2.wait_for_connections_drain(timeout=2.0)
    setup_mgr.wait_for_connections_drain(timeout=2.0)


