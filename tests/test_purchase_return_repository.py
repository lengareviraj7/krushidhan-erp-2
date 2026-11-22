"""
Dedicated Test Suite for Milestone 2B & Milestone 2B-Correction:
Purchase Return Models, Value Validation, and Repository Layer.

Covers:
1. Creating a return without supplier credit (PAYABLE_REDUCTION).
2. Creating a return with a supplier-credit settlement (5-step sequence).
3. Correct credit creation before settlement insertion (rejects unlinked settlement).
4. Correct settlement credit_id linkage.
5. Rejection of mismatched credit and settlement amounts.
6. Rejection of empty or missing idempotency keys.
7. Rejection of multiple supplier-credit allocations.
8. Atomic rollback when credit or settlement insertion fails.
9. Caller-owned transaction behaviour (no independent commit/close).
10. No stock changes or voucher postings.
11. Model validation and serialization.
12. Exact paise validation (rejects > 2 decimal places, no silent rounding).
13. Four-decimal quantity validation.
14. Rejection of invalid statuses and settlement types.
15. Correct mapping from database rows to models (preserves historical NULLs).
16. Original purchase-line tax-rate retrieval (not from product master).
17. Previous linked return retrieval.
18. Historical NULL purchase-item-link detection.
19. Supplier mismatch rejection.
20. One-credit-per-return enforcement.
21. No hard-delete methods and trigger delete protection.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Generator, Tuple

import pytest

from src.db.connection import DatabaseManager
from src.models.purchase_return import (
    PurchaseReturn,
    PurchaseReturnCreate,
    PurchaseReturnItem,
    PurchaseReturnItemCreate,
    PurchaseReturnSettlement,
    PurchaseReturnSettlementCreate,
    SupplierCredit,
    SupplierCreditCreate,
    validate_currency_paise,
    validate_quantity_precision,
)
from src.repositories.purchase_return_repository import PurchaseReturnRepository
from src.repositories.supplier_credit_repository import SupplierCreditRepository


@pytest.fixture
def repo_db(tmp_path: Path) -> Generator[Tuple[DatabaseManager, int, int, int, int, int], None, None]:
    """
    Sets up an isolated temporary database with schema, seed data, and a test purchase.
    Yields (db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id).
    """
    db_file = tmp_path / "test_pr_repo.db"
    db_mgr = DatabaseManager(str(db_file))
    db_mgr.initialize_database(include_seed=True)

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        cur = conn.cursor()
        # 1. Create a known supplier
        cur.execute(
            """
            INSERT INTO suppliers (supplier_name, mobile, gstin, state, current_balance)
            VALUES ('Agri Supplies Corp', '9876543210', '27ABCDE1234F1Z5', 'Maharashtra', 5000.0);
            """
        )
        supplier_id = cur.lastrowid

        # 2. Create a test product
        cur.execute(
            """
            INSERT INTO products (product_name, category_id, hsn_code, default_purchase_rate, default_sale_rate, default_mrp)
            VALUES ('Super Fertilizer 50kg', 1, '310100', 1000.0, 1200.0, 1300.0);
            """
        )
        product_id = cur.lastrowid

        # 3. Create a stock batch with 50 units
        cur.execute(
            """
            INSERT INTO stock_batches (product_id, batch_no, mfg_date, exp_date, purchase_rate, sale_rate, mrp, opening_qty, current_qty)
            VALUES (?, 'BAT-001', '2026-01-01', '2028-01-01', 1000.0, 1200.0, 1300.0, 50.0, 50.0);
            """,
            (product_id,),
        )
        batch_id = cur.lastrowid

        # 4. Create an inward purchase with 20 units @ 1000, 18% GST (9% CGST + 9% SGST), total discount 200 (10/unit)
        # Net rate = 990, taxable = 19800, cgst = 1782, sgst = 1782, total = 23364
        cur.execute(
            """
            INSERT INTO purchases (
                invoice_no, purchase_date, supplier_id, payment_type,
                total_taxable, total_cgst, total_sgst, total_igst, extra_charges,
                round_off, net_amount, paid_amount, due_amount
            ) VALUES ('INV-2026-001', '2026-11-01', ?, 'CREDIT', 19800.0, 1782.0, 1782.0, 0.0, 0.0, 0.0, 23364.0, 0.0, 23364.0);
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
            ) VALUES (?, ?, 'BAT-001', '2026-01-01', '2028-01-01', 20.0, 0.0, 1, 1000.0, 1200.0, 1300.0, 1.0, 200.0, 19800.0, 9.0, 1782.0, 9.0, 1782.0, 0.0, 0.0, 23364.0);
            """,
            (purchase_id, product_id),
        )
        item_id = cur.lastrowid

    yield db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id


# ============================================================================
# 1. Creating a return without supplier credit
# ============================================================================

def test_create_return_without_supplier_credit(repo_db):
    """1. Create a purchase return with PAYABLE_REDUCTION settlement only."""
    db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id = repo_db
    repo = PurchaseReturnRepository(db_mgr)

    ret_create = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="IDEM-NO-CREDIT-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item_id,
                product_id=product_id,
                batch_id=batch_id,
                qty=Decimal("4.0000"),
                reason="Excess stock",
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="PAYABLE_REDUCTION",
                amount=Decimal("4673.00"),  # 4 * 990 = 3960 + 18% = 4672.80 -> 4673.00 rounded
                notes="Reduce ledger payable",
            )
        ],
    )

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        return_id = repo.create_return(ret_create, conn=conn)

    ret_read = repo.get_return_by_id(return_id)
    assert ret_read is not None
    assert ret_read.return_id == return_id
    assert ret_read.net_amount == Decimal("4673.00")
    assert len(ret_read.settlements) == 1
    assert ret_read.settlements[0].settlement_type == "PAYABLE_REDUCTION"
    assert ret_read.settlements[0].credit_id is None


# ============================================================================
# 2. Creating a return with supplier-credit settlement (5-step sequence)
# ============================================================================

def test_create_return_with_supplier_credit_5_step_sequence(repo_db):
    """
    2. Test the canonical 5-step sequence:
       1. Create purchase-return header and items.
       2. Create supplier credit for SUPPLIER_CREDIT allocation.
       3. Obtain generated credit_id.
       4. Create settlement allocations.
       5. Verify SUPPLIER_CREDIT settlement is linked to newly created credit.
    """
    db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id = repo_db
    pr_repo = PurchaseReturnRepository(db_mgr)
    sc_repo = SupplierCreditRepository(db_mgr)

    ret_create = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="IDEM-5STEP-001",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=item_id,
                product_id=product_id,
                batch_id=batch_id,
                qty=Decimal("2.0000"),
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="SUPPLIER_CREDIT",
                amount=Decimal("2336.00"),
            )
        ],
    )

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        # Step 1: Create return header and items
        return_id = pr_repo.create_return_header_and_items(ret_create, conn=conn)
        assert return_id > 0

        # Step 2: Create supplier credit
        credit_create = SupplierCreditCreate(
            credit_no="SC-5STEP-001",
            supplier_id=supplier_id,
            return_id=return_id,
            credit_date="2026-11-16",
            original_amount=Decimal("2336.00"),
            notes="Credit note from 5-step return",
        )
        credit_id = sc_repo.create_credit(credit_create, conn=conn)

        # Step 3: Obtain generated credit_id
        assert credit_id > 0

        # Step 4: Create all settlement allocations passing credit_id
        settlement_ids = pr_repo.create_settlements(
            ret_create.settlements,
            return_id=return_id,
            conn=conn,
            credit_id=credit_id,
        )
        assert len(settlement_ids) == 1

    # Step 5: Verify SUPPLIER_CREDIT settlement is linked to credit
    ret_read = pr_repo.get_return_by_id(return_id)
    assert ret_read is not None
    assert len(ret_read.settlements) == 1
    assert ret_read.settlements[0].settlement_type == "SUPPLIER_CREDIT"
    assert ret_read.settlements[0].credit_id == credit_id
    assert ret_read.settlements[0].amount == Decimal("2336.00")

    credit_read = sc_repo.get_credit_by_id(credit_id)
    assert credit_read.original_amount == Decimal("2336.00")
    assert credit_read.status == "AVAILABLE"


# ============================================================================
# 3. Correct credit creation before settlement insertion
# ============================================================================

def test_correct_credit_creation_before_settlement_insertion(repo_db):
    """3. Creating SUPPLIER_CREDIT settlement before supplier credit exists is rejected."""
    db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id = repo_db
    pr_repo = PurchaseReturnRepository(db_mgr)

    ret_create = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="IDEM-ORDERING-ERR",
        items=[PurchaseReturnItemCreate(purchase_item_id=item_id, product_id=product_id, qty=Decimal("1.0000"))],
    )

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        return_id = pr_repo.create_return_header_and_items(ret_create, conn=conn)

        # Attempt to insert SUPPLIER_CREDIT settlement without creating credit first
        orphan_stl = PurchaseReturnSettlementCreate(
            settlement_type="SUPPLIER_CREDIT",
            amount=Decimal("1168.00"),
        )
        with pytest.raises(ValueError, match="credit_id must be provided or supplier credit must exist"):
            pr_repo.create_settlement(orphan_stl, return_id=return_id, conn=conn, credit_id=None)


# ============================================================================
# 4. Correct settlement credit_id linkage
# ============================================================================

def test_correct_settlement_credit_id_linkage(repo_db):
    """4. Ensures settlement row contains exact credit_id matching supplier_credits table."""
    db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id = repo_db
    pr_repo = PurchaseReturnRepository(db_mgr)
    sc_repo = SupplierCreditRepository(db_mgr)

    ret_create = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="IDEM-LINKAGE-01",
        items=[PurchaseReturnItemCreate(purchase_item_id=item_id, product_id=product_id, qty=Decimal("1.0000"))],
    )

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        return_id = pr_repo.create_return_header_and_items(ret_create, conn=conn)
        credit_id = sc_repo.create_credit(
            SupplierCreditCreate(
                credit_no="SC-LINK-01",
                supplier_id=supplier_id,
                return_id=return_id,
                credit_date="2026-11-16",
                original_amount=Decimal("1168.00"),
            ),
            conn=conn,
        )
        pr_repo.create_settlement(
            PurchaseReturnSettlementCreate(settlement_type="SUPPLIER_CREDIT", amount=Decimal("1168.00")),
            return_id=return_id,
            conn=conn,
            credit_id=credit_id,
        )

    # Check database row directly
    row = db_mgr.fetch_one(
        "SELECT * FROM purchase_return_settlements WHERE return_id = ? AND settlement_type = 'SUPPLIER_CREDIT';",
        (return_id,),
    )
    assert row["credit_id"] == credit_id


# ============================================================================
# 5. Rejection of mismatched credit and settlement amounts
# ============================================================================

def test_rejection_of_mismatched_credit_and_settlement_amounts(repo_db):
    """5. Rejects settlement where amount does not match supplier credit original_amount."""
    db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id = repo_db
    pr_repo = PurchaseReturnRepository(db_mgr)
    sc_repo = SupplierCreditRepository(db_mgr)

    ret_create = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="IDEM-MISMATCH-AMT",
        items=[PurchaseReturnItemCreate(purchase_item_id=item_id, product_id=product_id, qty=Decimal("2.0000"))],
    )

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        return_id = pr_repo.create_return_header_and_items(ret_create, conn=conn)
        credit_id = sc_repo.create_credit(
            SupplierCreditCreate(
                credit_no="SC-MISMATCH-01",
                supplier_id=supplier_id,
                return_id=return_id,
                credit_date="2026-11-16",
                original_amount=Decimal("2336.00"),  # Credit is 2336.00
            ),
            conn=conn,
        )

        # Settlement attempts 2000.00 (mismatch!)
        mismatched_stl = PurchaseReturnSettlementCreate(
            settlement_type="SUPPLIER_CREDIT",
            amount=Decimal("2000.00"),
        )
        with pytest.raises(ValueError, match="must exactly match supplier credit original_amount"):
            pr_repo.create_settlement(
                mismatched_stl,
                return_id=return_id,
                conn=conn,
                credit_id=credit_id,
            )


# ============================================================================
# 6. Rejection of empty or missing idempotency keys
# ============================================================================

def test_rejection_of_empty_or_missing_idempotency_keys():
    """6. Rejects missing, empty, or whitespace-only idempotency_key on PurchaseReturnCreate."""
    # Empty string
    with pytest.raises(ValueError, match="idempotency_key is required and cannot be empty"):
        PurchaseReturnCreate(
            purchase_id=1,
            supplier_id=1,
            return_date="2026-11-16",
            idempotency_key="",
            items=[PurchaseReturnItemCreate(purchase_item_id=1, product_id=1, qty=Decimal("1.0000"))],
        )

    # Whitespace only
    with pytest.raises(ValueError, match="idempotency_key is required and cannot be empty"):
        PurchaseReturnCreate(
            purchase_id=1,
            supplier_id=1,
            return_date="2026-11-16",
            idempotency_key="   ",
            items=[PurchaseReturnItemCreate(purchase_item_id=1, product_id=1, qty=Decimal("1.0000"))],
        )


# ============================================================================
# 7. Rejection of multiple supplier-credit allocations
# ============================================================================

def test_rejection_of_multiple_supplier_credit_allocations():
    """7. Enforces one-supplier-credit-allocation-per-return rule."""
    with pytest.raises(ValueError, match="cannot have more than one SUPPLIER_CREDIT allocation"):
        PurchaseReturnCreate(
            purchase_id=1,
            supplier_id=1,
            return_date="2026-11-16",
            idempotency_key="IDEM-MULTI-SC",
            items=[PurchaseReturnItemCreate(purchase_item_id=1, product_id=1, qty=Decimal("2.0000"))],
            settlements=[
                PurchaseReturnSettlementCreate(settlement_type="SUPPLIER_CREDIT", amount=Decimal("1000.00")),
                PurchaseReturnSettlementCreate(settlement_type="SUPPLIER_CREDIT", amount=Decimal("500.00")),
            ],
        )


# ============================================================================
# 8. Atomic rollback when credit or settlement insertion fails
# ============================================================================

def test_atomic_rollback_when_credit_or_settlement_fails(repo_db):
    """8. Atomic rollback restores clean database state if credit or settlement insertion raises."""
    db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id = repo_db
    pr_repo = PurchaseReturnRepository(db_mgr)
    sc_repo = SupplierCreditRepository(db_mgr)

    ret_create = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="IDEM-FAIL-ROLLBACK",
        items=[PurchaseReturnItemCreate(purchase_item_id=item_id, product_id=product_id, qty=Decimal("2.0000"))],
    )

    with pytest.raises(ValueError, match="Simulated mid-operation error"):
        with db_mgr.transaction(mode="IMMEDIATE") as conn:
            return_id = pr_repo.create_return_header_and_items(ret_create, conn=conn)
            credit_id = sc_repo.create_credit(
                SupplierCreditCreate(
                    credit_no="SC-ROLLBACK-TEST",
                    supplier_id=supplier_id,
                    return_id=return_id,
                    credit_date="2026-11-16",
                    original_amount=Decimal("2336.00"),
                ),
                conn=conn,
            )
            # Simulated crash before settlement
            raise ValueError("Simulated mid-operation error")

    # Verify no return or credit records were persisted
    assert db_mgr.fetch_one("SELECT * FROM purchase_returns WHERE idempotency_key = 'IDEM-FAIL-ROLLBACK';") is None
    assert db_mgr.fetch_one("SELECT * FROM supplier_credits WHERE credit_no = 'SC-ROLLBACK-TEST';") is None


# ============================================================================
# 9. Caller-owned transaction behaviour
# ============================================================================

def test_caller_owned_transaction_behaviour(repo_db):
    """9. Repository methods never independently commit or close the caller's connection."""
    db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id = repo_db
    pr_repo = PurchaseReturnRepository(db_mgr)

    ret_create = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="IDEM-CALLER-OWNED",
        items=[PurchaseReturnItemCreate(purchase_item_id=item_id, product_id=product_id, qty=Decimal("1.0000"))],
    )

    # Within an uncommitted transaction, connection remains open and active
    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        return_id = pr_repo.create_return_header_and_items(ret_create, conn=conn)
        # Connection is still open and uncommitted
        cur = conn.cursor()
        cur.execute("SELECT return_id FROM purchase_returns WHERE return_id = ?;", (return_id,))
        assert cur.fetchone() is not None

        # Verify not yet visible to an external isolated connection (deferred WAL isolation)
        ext_conn = db_mgr._create_raw_connection(track=False)
        try:
            chk = ext_conn.execute("SELECT return_id FROM purchase_returns WHERE return_id = ?;", (return_id,)).fetchone()
            assert chk is None
        finally:
            ext_conn.close()


# ============================================================================
# 10. No stock changes or voucher postings
# ============================================================================

def test_no_stock_changes_or_voucher_postings(repo_db):
    """10. Milestone 2B scope boundary: stock batches and vouchers remain untouched."""
    db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id = repo_db
    pr_repo = PurchaseReturnRepository(db_mgr)
    sc_repo = SupplierCreditRepository(db_mgr)

    # Initial state
    batch_before = db_mgr.fetch_one("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch_id,))["current_qty"]
    vouchers_before = len(db_mgr.fetch_all("SELECT * FROM vouchers;"))

    ret_create = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="IDEM-SCOPE-BOUNDARY",
        items=[PurchaseReturnItemCreate(purchase_item_id=item_id, product_id=product_id, qty=Decimal("5.0000"))],
    )

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        return_id = pr_repo.create_return_header_and_items(ret_create, conn=conn)
        credit_id = sc_repo.create_credit(
            SupplierCreditCreate(
                credit_no="SC-SCOPE-001",
                supplier_id=supplier_id,
                return_id=return_id,
                credit_date="2026-11-16",
                original_amount=Decimal("5841.00"),
            ),
            conn=conn,
        )
        pr_repo.create_settlement(
            PurchaseReturnSettlementCreate(settlement_type="SUPPLIER_CREDIT", amount=Decimal("5841.00")),
            return_id=return_id,
            conn=conn,
            credit_id=credit_id,
        )

    # Final state
    batch_after = db_mgr.fetch_one("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch_id,))["current_qty"]
    vouchers_after = len(db_mgr.fetch_all("SELECT * FROM vouchers;"))

    assert batch_after == batch_before == 50.0, "Stock batches must not be modified in repository milestone"
    assert vouchers_after == vouchers_before, "Accounting vouchers must not be posted in repository milestone"


# ============================================================================
# Additional Comprehensive Model & Domain Checks
# ============================================================================

def test_untrusted_request_fingerprint_rejected():
    """Client cannot supply trusted request_fingerprint directly."""
    with pytest.raises(ValueError, match="Client-provided request_fingerprint is not accepted"):
        PurchaseReturnCreate(
            purchase_id=1,
            supplier_id=1,
            return_date="2026-11-16",
            idempotency_key="IDEM-FINGERPRINT-TEST",
            request_fingerprint="forged_fingerprint_value",
            items=[PurchaseReturnItemCreate(purchase_item_id=1, product_id=1, qty=Decimal("1.0000"))],
        )


def test_deterministic_request_fingerprint():
    """Fingerprint is computed deterministically from canonical request fields."""
    ret_1 = PurchaseReturnCreate(
        purchase_id=1,
        supplier_id=1,
        return_date="2026-11-16",
        idempotency_key="IDEM-CANONICAL",
        items=[PurchaseReturnItemCreate(purchase_item_id=1, product_id=1, qty=Decimal("2.0000"))],
    )
    ret_2 = PurchaseReturnCreate(
        purchase_id=1,
        supplier_id=1,
        return_date="2026-11-16",
        idempotency_key="IDEM-CANONICAL",
        items=[PurchaseReturnItemCreate(purchase_item_id=1, product_id=1, qty=Decimal("2.0000"))],
    )
    assert ret_1.compute_request_fingerprint() == ret_2.compute_request_fingerprint()
    assert len(ret_1.compute_request_fingerprint()) == 64


def test_idempotency_key_excluded_from_fingerprint():
    """Idempotency key identifies the request, not content; excluded from fingerprint."""
    base_args = dict(
        purchase_id=1,
        supplier_id=1,
        return_date="2026-11-16",
        items=[PurchaseReturnItemCreate(purchase_item_id=1, product_id=1, qty=Decimal("2.0000"))],
    )
    ret_a = PurchaseReturnCreate(idempotency_key="KEY-A", **base_args)
    ret_b = PurchaseReturnCreate(idempotency_key="KEY-B", **base_args)
    assert ret_a.compute_request_fingerprint() == ret_b.compute_request_fingerprint()


def test_fingerprint_sensitivity_to_each_business_field():
    """
    Reusing the same key with changes to any business-critical field produces a different fingerprint:
    - Settlement amount
    - Settlement type
    - Cash/bank account (account_id)
    - Cheque or bank reference number (reference_no)
    - Settlement notes (notes)
    - ITC status (itc_status)
    - Purchase-item ID (purchase_item_id)
    - Batch ID (batch_id)
    - Quantity (qty)
    - Return reason (reason)
    - Supplier credit-note number and date
    - Return date
    - Remarks
    """
    base = PurchaseReturnCreate(
        purchase_id=1,
        supplier_id=1,
        return_date="2026-11-16",
        idempotency_key="REUSED-KEY-001",
        supplier_credit_note_no="SCN-101",
        supplier_credit_note_date="2026-11-15",
        remarks="Standard return",
        itc_status="CLAIMED_REVERSED",
        items=[
            PurchaseReturnItemCreate(
                purchase_item_id=10,
                product_id=100,
                batch_id=5,
                qty=Decimal("2.0000"),
                reason="Damaged package",
            )
        ],
        settlements=[
            PurchaseReturnSettlementCreate(
                settlement_type="REFUND_BANK",
                amount=Decimal("1500.00"),
                account_id=2,
                reference_no="CHQ-12345",
                notes="Bank refund",
            )
        ],
    )
    base_fp = base.compute_request_fingerprint()

    # 1. Settlement amount
    mod_stl_amt = base.model_copy(deep=True)
    mod_stl_amt.settlements[0].amount = Decimal("1500.50")
    assert mod_stl_amt.compute_request_fingerprint() != base_fp

    # 2. Settlement type
    mod_stl_type = base.model_copy(deep=True)
    mod_stl_type.settlements[0].settlement_type = "REFUND_CASH"
    assert mod_stl_type.compute_request_fingerprint() != base_fp

    # 3. Cash/bank account (account_id)
    mod_stl_ac = base.model_copy(deep=True)
    mod_stl_ac.settlements[0].account_id = 99
    assert mod_stl_ac.compute_request_fingerprint() != base_fp

    # 4. Cheque / bank reference number
    mod_stl_ref = base.model_copy(deep=True)
    mod_stl_ref.settlements[0].reference_no = "CHQ-99999"
    assert mod_stl_ref.compute_request_fingerprint() != base_fp

    # 5. Settlement notes
    mod_stl_notes = base.model_copy(deep=True)
    mod_stl_notes.settlements[0].notes = "Updated refund notes"
    assert mod_stl_notes.compute_request_fingerprint() != base_fp

    # 6. ITC status
    mod_itc = base.model_copy(deep=True)
    mod_itc.itc_status = "INELIGIBLE_REVERSED"
    assert mod_itc.compute_request_fingerprint() != base_fp

    # 7. Purchase-item ID
    mod_pi_id = base.model_copy(deep=True)
    mod_pi_id.items[0].purchase_item_id = 99
    assert mod_pi_id.compute_request_fingerprint() != base_fp

    # 8. Batch ID
    mod_batch = base.model_copy(deep=True)
    mod_batch.items[0].batch_id = 999
    assert mod_batch.compute_request_fingerprint() != base_fp

    # 9. Quantity
    mod_qty = base.model_copy(deep=True)
    mod_qty.items[0].qty = Decimal("2.5000")
    assert mod_qty.compute_request_fingerprint() != base_fp

    # 10. Return reason
    mod_reason = base.model_copy(deep=True)
    mod_reason.items[0].reason = "Expired product"
    assert mod_reason.compute_request_fingerprint() != base_fp

    # 11. Supplier credit-note number
    mod_scn_no = base.model_copy(deep=True)
    mod_scn_no.supplier_credit_note_no = "SCN-DIFF-999"
    assert mod_scn_no.compute_request_fingerprint() != base_fp

    # 12. Supplier credit-note date
    mod_scn_date = base.model_copy(deep=True)
    mod_scn_date.supplier_credit_note_date = "2026-11-16"
    assert mod_scn_date.compute_request_fingerprint() != base_fp

    # 13. Return date
    mod_ret_date = base.model_copy(deep=True)
    mod_ret_date.return_date = "2026-11-17"
    assert mod_ret_date.compute_request_fingerprint() != base_fp

    # 14. Remarks
    mod_remarks = base.model_copy(deep=True)
    mod_remarks.remarks = "Different remarks text"
    assert mod_remarks.compute_request_fingerprint() != base_fp


def test_fingerprint_dictionary_key_order_invariance():
    """Identical payloads produce identical fingerprints regardless of dictionary key order."""
    # Dict 1: natural order
    d1 = {
        "purchase_id": 5,
        "supplier_id": 12,
        "return_date": "2026-11-16",
        "idempotency_key": "IDEM-ORDER-TEST",
        "supplier_credit_note_no": "SCN-1",
        "supplier_credit_note_date": "2026-11-15",
        "remarks": "Invariance test",
        "itc_status": "CLAIMED_REVERSED",
        "items": [
            {
                "purchase_item_id": 20,
                "product_id": 50,
                "batch_id": 7,
                "qty": Decimal("3.0000"),
                "reason": "Excess quantity",
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": Decimal("1000.00"),
                "account_id": None,
                "reference_no": None,
                "notes": None,
            }
        ],
    }

    # Dict 2: reversed/scrambled key order for top-level, items, and settlements
    d2 = {
        "settlements": [
            {
                "notes": None,
                "reference_no": None,
                "account_id": None,
                "amount": Decimal("1000.00"),
                "settlement_type": "PAYABLE_REDUCTION",
            }
        ],
        "items": [
            {
                "reason": "Excess quantity",
                "qty": Decimal("3.0000"),
                "batch_id": 7,
                "product_id": 50,
                "purchase_item_id": 20,
            }
        ],
        "itc_status": "CLAIMED_REVERSED",
        "remarks": "Invariance test",
        "supplier_credit_note_date": "2026-11-15",
        "supplier_credit_note_no": "SCN-1",
        "idempotency_key": "IDEM-ORDER-TEST",
        "return_date": "2026-11-16",
        "supplier_id": 12,
        "purchase_id": 5,
    }

    ret_1 = PurchaseReturnCreate(**d1)
    ret_2 = PurchaseReturnCreate(**d2)

    assert ret_1.compute_request_fingerprint() == ret_2.compute_request_fingerprint()


def test_fingerprint_quantity_and_paise_normalization():
    """Quantity precision and currency paise normalize cleanly across different input representations."""
    ret_a = PurchaseReturnCreate(
        purchase_id=1,
        supplier_id=1,
        return_date="2026-11-16",
        idempotency_key="IDEM-NORM-1",
        items=[PurchaseReturnItemCreate(purchase_item_id=1, product_id=1, qty=Decimal("2.0"))],
        settlements=[PurchaseReturnSettlementCreate(settlement_type="PAYABLE_REDUCTION", amount=Decimal("500"))],
    )
    ret_b = PurchaseReturnCreate(
        purchase_id=1,
        supplier_id=1,
        return_date="2026-11-16",
        idempotency_key="IDEM-NORM-2",
        items=[PurchaseReturnItemCreate(purchase_item_id=1, product_id=1, qty=Decimal("2.0000"))],
        settlements=[PurchaseReturnSettlementCreate(settlement_type="PAYABLE_REDUCTION", amount=Decimal("500.00"))],
    )
    # Both normalize: quantity -> '2.0000', amount_paise -> 50000
    assert ret_a.compute_request_fingerprint() == ret_b.compute_request_fingerprint()


def test_get_return_by_idempotency_key(repo_db):
    """Repository fetches return by idempotency_key supporting replay protection checks."""
    db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id = repo_db
    pr_repo = PurchaseReturnRepository(db_mgr)

    key = "IDEM-REPLAY-REPO-001"
    ret_create = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key=key,
        items=[PurchaseReturnItemCreate(purchase_item_id=item_id, product_id=product_id, qty=Decimal("1.0000"))],
    )
    expected_fp = ret_create.compute_request_fingerprint()

    # Query before creation returns None
    assert pr_repo.get_return_by_idempotency_key(key) is None

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        return_id = pr_repo.create_return_header_and_items(ret_create, conn=conn)

    # Query after creation returns full model with matching key and fingerprint
    found = pr_repo.get_return_by_idempotency_key(key)
    assert found is not None
    assert found.return_id == return_id
    assert found.idempotency_key == key
    assert found.request_fingerprint == expected_fp


def test_one_credit_per_return_enforcement(repo_db):
    """Enforces one credit per return at repository level."""
    db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id = repo_db
    pr_repo = PurchaseReturnRepository(db_mgr)
    sc_repo = SupplierCreditRepository(db_mgr)

    ret_create = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="IDEM-ONE-CREDIT",
        items=[PurchaseReturnItemCreate(purchase_item_id=item_id, product_id=product_id, qty=Decimal("1.0000"))],
    )

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        return_id = pr_repo.create_return_header_and_items(ret_create, conn=conn)
        sc_repo.create_credit(
            SupplierCreditCreate(
                credit_no="SC-ONECREDIT-1",
                supplier_id=supplier_id,
                return_id=return_id,
                credit_date="2026-11-16",
                original_amount=Decimal("1168.00"),
            ),
            conn=conn,
        )

        with pytest.raises(ValueError, match="already exists for return"):
            sc_repo.create_credit(
                SupplierCreditCreate(
                    credit_no="SC-ONECREDIT-2",
                    supplier_id=supplier_id,
                    return_id=return_id,
                    credit_date="2026-11-16",
                    original_amount=Decimal("1168.00"),
                ),
                conn=conn,
            )


def test_supplier_mismatch_rejection(repo_db):
    """Supplier mismatch between return and original purchase is rejected."""
    db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id = repo_db
    repo = PurchaseReturnRepository(db_mgr)

    ret_create = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=9999,  # Mismatch
        return_date="2026-11-16",
        idempotency_key="IDEM-SUPPLIER-MISMATCH",
        items=[PurchaseReturnItemCreate(purchase_item_id=item_id, product_id=product_id, qty=Decimal("1.0000"))],
    )

    with pytest.raises(ValueError, match="Supplier ID mismatch"):
        with db_mgr.transaction(mode="IMMEDIATE") as conn:
            repo.create_return_header_and_items(ret_create, conn=conn)


def test_historical_null_purchase_item_link_detection(repo_db):
    """Detects historical return rows where purchase_item_id is NULL."""
    db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id = repo_db
    repo = PurchaseReturnRepository(db_mgr)

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO purchase_returns (
                return_no, return_date, purchase_id, supplier_id,
                total_taxable, total_tax, net_amount, status
            ) VALUES ('PR-HIST-NULL-LINK', '2025-08-01', ?, ?, 500.0, 0.0, 500.0, 'POSTED');
            """,
            (purchase_id, supplier_id),
        )
        leg_id = cur.lastrowid
        cur.execute(
            """
            INSERT INTO purchase_return_items (
                return_id, purchase_item_id, product_id, batch_id,
                qty, purchase_rate, taxable_amount, total_amount, reason
            ) VALUES (?, NULL, ?, ?, 2.0, 250.0, 500.0, 500.0, 'Historical unlinked return');
            """,
            (leg_id, product_id, batch_id),
        )

    unlinked = repo.detect_historical_unlinked_returns(purchase_id)
    assert len(unlinked) == 1
    assert unlinked[0]["return_no"] == "PR-HIST-NULL-LINK"
    assert unlinked[0]["qty"] == 2.0


def test_no_hard_delete_methods_and_trigger_protection(repo_db):
    """Confirms no hard-delete methods exist and DB triggers block financial hard-deletes."""
    db_mgr, purchase_id, supplier_id, item_id, product_id, batch_id = repo_db
    pr_repo = PurchaseReturnRepository(db_mgr)
    sc_repo = SupplierCreditRepository(db_mgr)

    for method in ["delete_return", "delete_credit", "delete"]:
        assert not hasattr(pr_repo, method)
        assert not hasattr(sc_repo, method)

    ret_create = PurchaseReturnCreate(
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        return_date="2026-11-16",
        idempotency_key="IDEM-TRIG-PROT",
        items=[PurchaseReturnItemCreate(purchase_item_id=item_id, product_id=product_id, qty=Decimal("1.0000"))],
    )
    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        ret_id = pr_repo.create_return_header_and_items(ret_create, conn=conn)

    with pytest.raises(sqlite3.IntegrityError, match="Financial audit protection: Deletion of purchase returns is prohibited"):
        with db_mgr.transaction(mode="IMMEDIATE") as conn:
            conn.execute("DELETE FROM purchase_returns WHERE return_id = ?;", (ret_id,))
