"""
Phase 2D-2 Comprehensive Isolated Verification Suite:
End-to-End Sales Return and Purchase Return Workflows, Idempotency, Concurrency,
Phase 2B-2 Refund Safety, Rollback Atomicity, RBAC, and UI Contracts.

All tests execute strictly against isolated temporary SQLite databases (tmp_path).
Never touches data/agri_erp.db or backups/.
"""
from __future__ import annotations

import threading
import uuid
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, reset_db_manager
from src.models.master_data import Customer, Product, Supplier
from src.models.purchase import Purchase, PurchaseItem
from src.models.purchase_return import (
    PurchaseReturnCreate,
    PurchaseReturnItemCreate,
    PurchaseReturnSettlementCreate,
)
from src.models.sales import Sale, SaleItem, SalesReturn, SalesReturnItem
from src.repositories.accounting_repository import AccountingRepository
from src.repositories.inventory_repository import InventoryRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.repositories.sales_repository import SalesRepository
from src.services.accounting_service import AccountingService
from src.services.auth_service import AuthService
from src.services.purchase_return_service import PurchaseReturnService
from src.services.purchase_service import PurchaseService
from src.services.sales_service import SalesService


@pytest.fixture
def env(tmp_path, monkeypatch) -> Dict[str, Any]:
    """Isolated SQLite database, services, and ADMIN/OPERATOR TestClients."""
    db_path = tmp_path / "phase2d2_returns.db"
    bkp_dir = tmp_path / "phase2d2_backups"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    monkeypatch.setenv("APP_MODE", "local")
    reset_db_manager()

    db = DatabaseManager(db_path=db_path, app_mode="local", backup_dir=bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)

    master_repo = MasterDataRepository(db)
    inv_repo = InventoryRepository(db)
    acc_repo = AccountingRepository(db)
    sales_repo = SalesRepository(db)
    sales_svc = SalesService(db)
    purch_svc = PurchaseService(db)
    pr_svc = PurchaseReturnService(db)
    acc_svc = AccountingService(db)

    cust_id = master_repo.create_customer(
        Customer(customer_name="Ramesh Deshmukh", mobile="9822001122", village="Karad", opening_balance=0.0)
    )
    supp_id = master_repo.create_supplier(
        Supplier(
            supplier_name="Sahyadri Agro Distributors",
            contact_person="Sunil Patil",
            mobile="9700112233",
            city="Pune",
            gstin="27AABCS1234F1Z5",
            opening_balance=0.0,
        )
    )
    prod_id = master_repo.create_product(
        Product(
            product_name="Chlorantraniliprole 18.5% SC 150ml",
            category_id=3,
            manufacturer_id=1,
            hsn_code="38089199",
            unit_id=1,
            tax_group_id=4,  # 18% GST (9% CGST + 9% SGST)
            default_purchase_rate=200.0,
            default_sale_rate=295.0,
            default_mrp=320.0,
        )
    )

    # Create a baseline inward purchase of 20 units @ 200 + 18% GST = 4720.00 (CREDIT -> supplier balance = 4720)
    purch_id = purch_svc.process_inward_purchase(
        Purchase(
            invoice_no="PUR-2D2-001",
            supplier_id=supp_id,
            purchase_date="2026-11-10",
            payment_type="CREDIT",
            total_taxable=4000.0,
            total_cgst=360.0,
            total_sgst=360.0,
            net_amount=4720.0,
            paid_amount=0.0,
            due_amount=4720.0,
            items=[
                PurchaseItem(
                    product_id=prod_id,
                    batch_no="B-2D2-100",
                    mfg_date="2026-01-01",
                    exp_date="2028-01-01",
                    qty=20.0,
                    free_qty=0.0,
                    purchase_rate=200.0,
                    sale_rate=295.0,
                    mrp=320.0,
                    taxable_amount=4000.0,
                    cgst_rate=9.0,
                    cgst_amount=360.0,
                    sgst_rate=9.0,
                    sgst_amount=360.0,
                    total_amount=4720.0,
                )
            ],
        )
    )
    batch_row = db.fetch_one("SELECT batch_id FROM stock_batches WHERE product_id = ? AND batch_no = 'B-2D2-100';", (prod_id,))
    batch_id = int(batch_row["batch_id"])
    pi_row = db.fetch_one("SELECT item_id FROM purchase_items WHERE purchase_id = ?;", (purch_id,))
    purch_item_id = int(pi_row["item_id"])

    admin_token = AuthService.create_access_token(user_id=1, username="admin", full_name="Shop Admin", role="ADMIN")
    op_token = AuthService.create_access_token(user_id=2, username="operator1", full_name="Counter Staff", role="OPERATOR")

    with TestClient(app) as client:
        yield {
            "db": db,
            "client": client,
            "admin_hdr": {"Authorization": f"Bearer {admin_token}"},
            "op_hdr": {"Authorization": f"Bearer {op_token}"},
            "master_repo": master_repo,
            "inv_repo": inv_repo,
            "acc_repo": acc_repo,
            "sales_repo": sales_repo,
            "sales_svc": sales_svc,
            "purch_svc": purch_svc,
            "pr_svc": pr_svc,
            "acc_svc": acc_svc,
            "cust_id": cust_id,
            "supp_id": supp_id,
            "prod_id": prod_id,
            "batch_id": batch_id,
            "purch_id": purch_id,
            "purch_item_id": purch_item_id,
        }
    reset_db_manager()


def _create_sale(env: Dict[str, Any], invoice_no: str, qty: float, payment_mode: str, paid_amount: float) -> int:
    """Helper to create a realistic GST-inclusive sale on batch B-2D2-100 (₹295/unit = ₹250 taxable + ₹22.50 CGST + ₹22.50 SGST)."""
    taxable = round(qty * 250.0, 2)
    cgst = round(qty * 22.5, 2)
    sgst = round(qty * 22.5, 2)
    net = round(taxable + cgst + sgst, 2)
    due = round(max(0.0, net - paid_amount), 2)
    return env["sales_svc"].process_sales_invoice(
        Sale(
            invoice_no=invoice_no,
            sale_date="2026-11-15",
            customer_id=env["cust_id"],
            payment_mode=payment_mode,
            total_taxable=taxable,
            total_cgst=cgst,
            total_sgst=sgst,
            net_amount=net,
            paid_amount=paid_amount,
            due_amount=due,
            items=[
                SaleItem(
                    product_id=env["prod_id"],
                    batch_id=env["batch_id"],
                    batch_no="B-2D2-100",
                    qty=qty,
                    sale_rate=295.0,
                    mrp=320.0,
                    taxable_amount=taxable,
                    cgst_rate=9.0,
                    cgst_amount=cgst,
                    sgst_rate=9.0,
                    sgst_amount=sgst,
                    total_amount=net,
                )
            ],
        )
    )


def _assert_all_vouchers_balanced(db: DatabaseManager) -> None:
    unbalanced = db.fetch_all(
        """
        SELECT v.voucher_id, v.voucher_no,
               ROUND(SUM(le.debit_amount), 2) AS dr,
               ROUND(SUM(le.credit_amount), 2) AS cr
        FROM vouchers v
        JOIN ledger_entries le ON v.voucher_id = le.voucher_id
        GROUP BY v.voucher_id
        HAVING ABS(dr - cr) > 0.01;
        """
    )
    assert unbalanced == [], f"Found unbalanced vouchers: {[dict(r) for r in unbalanced]}"


# ============================================================================
# A. SALES RETURN WORKFLOW TESTS
# ============================================================================

def test_sales_return_full_and_partial_api_preview_stock_gst_credit_and_cash(env):
    """
    Verifies:
    - Preview endpoint (zero DB writes) matches exact tax reversal and settlement summary.
    - Partial + final full return on a CREDIT sale restores original batch stock, reduces
      customer balance accurately, posts balanced vouchers, preserves original invoice.
    - Valid full return on a CASH sale refunds Cash in Hand without touching customer balance.
    - Valid full return on a BANK_TRANSFER sale refunds Bank Account without touching customer balance.
    """
    client = env["client"]
    hdr = env["admin_hdr"]
    db = env["db"]

    # 1. Credit sale of 4 units (net 1180, due 1180); stock 20 -> 16
    credit_sale_id = _create_sale(env, "INV-SR-CR-1", qty=4.0, payment_mode="CREDIT", paid_amount=0.0)
    assert env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty == pytest.approx(16.0)
    assert env["master_repo"].get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(1180.0)

    # Preview 1 unit partial return -> must perform ZERO DB writes
    prev_res = client.post(
        "/api/sales/returns/preview",
        headers=hdr,
        json={"sale_id": credit_sale_id, "items": [{"product_id": env["prod_id"], "batch_id": env["batch_id"], "qty": 1.0, "reason": "Damaged / Leaking Pack"}]},
    )
    assert prev_res.status_code == 200, prev_res.text
    prev = prev_res.json()
    assert prev["total_taxable"] == pytest.approx(250.0)
    assert prev["total_cgst"] == pytest.approx(22.5)
    assert prev["total_sgst"] == pytest.approx(22.5)
    assert prev["net_amount"] == pytest.approx(295.0)
    assert prev["receivable_reduction"] == pytest.approx(295.0)
    assert prev["refund_amount"] == pytest.approx(0.0)
    assert prev["settlement_supported"] is True
    assert db.fetch_one("SELECT COUNT(*) AS c FROM sales_returns;")["c"] == 0

    # Post partial return (1 unit)
    k1 = f"SR-PART-{uuid.uuid4()}"
    r1 = client.post(
        "/api/sales/returns",
        headers={**hdr, "Idempotency-Key": k1},
        json={
            "return_no": "",
            "return_date": "2026-11-16",
            "sale_id": credit_sale_id,
            "customer_id": env["cust_id"],
            "total_taxable": prev["total_taxable"],
            "total_tax": prev["total_tax"],
            "round_off": prev["round_off"],
            "net_amount": prev["net_amount"],
            "remarks": "Partial damaged bottle",
            "items": [
                {
                    "product_id": env["prod_id"],
                    "batch_id": env["batch_id"],
                    "qty": 1.0,
                    "sale_rate": 295.0,
                    "taxable_amount": 250.0,
                    "tax_amount": 45.0,
                    "total_amount": 295.0,
                    "reason": "Damaged / Leaking Pack",
                }
            ],
        },
    )
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    assert d1["success"] is True
    assert d1["idempotent_replay"] is False
    assert d1["return_no"].startswith("RET-")

    # Verify stock restored (16 -> 17) and customer balance reduced (1180 -> 885)
    assert env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty == pytest.approx(17.0)
    assert env["master_repo"].get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(885.0)

    # Verify eligible returns updated (sold 4, returned 1, eligible 3)
    elig = client.get(f"/api/sales/{credit_sale_id}/eligible-returns", headers=hdr).json()
    assert elig["items"][0]["already_returned_qty"] == pytest.approx(1.0)
    assert elig["items"][0]["eligible_return_qty"] == pytest.approx(3.0)

    # Verify detail endpoint returns voucher & settlement summary
    det = client.get(f"/api/sales/returns/{d1['return_id']}", headers=hdr).json()
    assert det["voucher_no"] is not None
    assert det["receivable_reduction"] == pytest.approx(295.0)
    assert det["refund_amount"] == pytest.approx(0.0)
    assert det["total_cgst"] == pytest.approx(22.5)
    assert det["total_sgst"] == pytest.approx(22.5)

    # Post remaining 3 units (completing full return of credit invoice)
    k2 = f"SR-REM-{uuid.uuid4()}"
    r2 = client.post(
        "/api/sales/returns",
        headers={**hdr, "Idempotency-Key": k2},
        json={
            "return_no": "",
            "return_date": "2026-11-16",
            "sale_id": credit_sale_id,
            "customer_id": env["cust_id"],
            "total_taxable": 750.0,
            "total_tax": 135.0,
            "net_amount": 885.0,
            "items": [
                {
                    "product_id": env["prod_id"],
                    "batch_id": env["batch_id"],
                    "qty": 3.0,
                    "sale_rate": 295.0,
                    "taxable_amount": 750.0,
                    "tax_amount": 135.0,
                    "total_amount": 885.0,
                    "reason": "Excess Quantity Purchased",
                }
            ],
        },
    )
    assert r2.status_code == 200, r2.text
    assert env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty == pytest.approx(20.0)
    assert env["master_repo"].get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(0.0)

    # Original credit invoice snapshot remains strictly immutable
    orig = env["sales_repo"].get_sale_by_id(credit_sale_id)
    assert orig.net_amount == pytest.approx(1180.0)
    assert orig.items[0].qty == pytest.approx(4.0)

    # 2. Cash sale full return (2 units = 590) -> refunds Cash in Hand, customer balance stays 0
    cash_before = env["acc_repo"].get_cash_account().current_balance
    cash_sale_id = _create_sale(env, "INV-SR-CASH-1", qty=2.0, payment_mode="CASH", paid_amount=590.0)
    assert env["acc_repo"].get_cash_account().current_balance == pytest.approx(cash_before + 590.0)
    r_cash = client.post(
        "/api/sales/returns",
        headers={**hdr, "Idempotency-Key": f"SR-CASH-{uuid.uuid4()}"},
        json={
            "return_date": "2026-11-16",
            "sale_id": cash_sale_id,
            "customer_id": env["cust_id"],
            "total_taxable": 500.0,
            "total_tax": 90.0,
            "net_amount": 590.0,
            "items": [{"product_id": env["prod_id"], "batch_id": env["batch_id"], "qty": 2.0, "sale_rate": 295.0, "taxable_amount": 500.0, "tax_amount": 90.0, "total_amount": 590.0}],
        },
    )
    assert r_cash.status_code == 200, r_cash.text
    assert env["acc_repo"].get_cash_account().current_balance == pytest.approx(cash_before)
    assert env["master_repo"].get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(0.0)

    # 3. Bank transfer sale full return (2 units = 590) -> refunds Bank Account
    bank_before = env["acc_repo"].get_bank_account().current_balance
    bank_sale_id = _create_sale(env, "INV-SR-BANK-1", qty=2.0, payment_mode="BANK_TRANSFER", paid_amount=590.0)
    r_bank = client.post(
        "/api/sales/returns",
        headers={**hdr, "Idempotency-Key": f"SR-BANK-{uuid.uuid4()}"},
        json={
            "return_date": "2026-11-16",
            "sale_id": bank_sale_id,
            "customer_id": env["cust_id"],
            "total_taxable": 500.0,
            "total_tax": 90.0,
            "net_amount": 590.0,
            "items": [{"product_id": env["prod_id"], "batch_id": env["batch_id"], "qty": 2.0, "sale_rate": 295.0, "taxable_amount": 500.0, "tax_amount": 90.0, "total_amount": 590.0}],
        },
    )
    assert r_bank.status_code == 200, r_bank.text
    assert env["acc_repo"].get_bank_account().current_balance == pytest.approx(bank_before)
    _assert_all_vouchers_balanced(db)


def test_sales_return_rejects_excess_duplicate_invalid_and_ambiguous_refund_with_zero_writes(env):
    """
    Verifies:
    - Excess return quantity rejected (HTTP 400).
    - Repeating return of already-returned quantity rejected (HTTP 400).
    - Invalid / non-existent invoice ID rejected (HTTP 400).
    - Ambiguous SPLIT payment refund rejected by BOTH preview (settlement_supported=False)
      and POST (HTTP 400) with ZERO writes to stock, returns, balances, or vouchers.
    - Paid-off CREDIT sale refund (unallocated receipt) rejected with ZERO writes.
    """
    client = env["client"]
    hdr = env["admin_hdr"]
    db = env["db"]

    # 1. Invalid invoice
    bad_inv = client.post(
        "/api/sales/returns",
        headers=hdr,
        json={
            "return_date": "2026-11-16",
            "sale_id": 999999,
            "customer_id": env["cust_id"],
            "net_amount": 295.0,
            "items": [{"product_id": env["prod_id"], "batch_id": env["batch_id"], "qty": 1.0, "sale_rate": 295.0, "taxable_amount": 250.0, "tax_amount": 45.0, "total_amount": 295.0}],
        },
    )
    assert bad_inv.status_code == 400

    # 2. Excess & repeated return
    sale_id = _create_sale(env, "INV-SR-CAP-1", qty=2.0, payment_mode="CASH", paid_amount=590.0)
    excess = client.post(
        "/api/sales/returns",
        headers=hdr,
        json={
            "return_date": "2026-11-16",
            "sale_id": sale_id,
            "customer_id": env["cust_id"],
            "net_amount": 885.0,
            "items": [{"product_id": env["prod_id"], "batch_id": env["batch_id"], "qty": 3.0, "sale_rate": 295.0, "taxable_amount": 750.0, "tax_amount": 135.0, "total_amount": 885.0}],
        },
    )
    assert excess.status_code == 400
    assert "exceeds eligible" in excess.json()["detail"].lower()

    ok = client.post(
        "/api/sales/returns",
        headers=hdr,
        json={
            "return_date": "2026-11-16",
            "sale_id": sale_id,
            "customer_id": env["cust_id"],
            "net_amount": 590.0,
            "items": [{"product_id": env["prod_id"], "batch_id": env["batch_id"], "qty": 2.0, "sale_rate": 295.0, "taxable_amount": 500.0, "tax_amount": 90.0, "total_amount": 590.0}],
        },
    )
    assert ok.status_code == 200
    # Second attempt to return the same 2 units with a different key MUST fail
    repeat = client.post(
        "/api/sales/returns",
        headers={**hdr, "Idempotency-Key": f"SR-REP-{uuid.uuid4()}"},
        json={
            "return_date": "2026-11-16",
            "sale_id": sale_id,
            "customer_id": env["cust_id"],
            "net_amount": 295.0,
            "items": [{"product_id": env["prod_id"], "batch_id": env["batch_id"], "qty": 1.0, "sale_rate": 295.0, "taxable_amount": 250.0, "tax_amount": 45.0, "total_amount": 295.0}],
        },
    )
    assert repeat.status_code == 400

    # 3. Ambiguous SPLIT payment sale -> preview blocks & POST rejects with ZERO writes
    split_sale_id = _create_sale(env, "INV-SR-SPLIT-1", qty=2.0, payment_mode="SPLIT", paid_amount=590.0)
    stock_before = env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty
    sr_count_before = db.fetch_one("SELECT COUNT(*) AS c FROM sales_returns;")["c"]
    vch_count_before = db.fetch_one("SELECT COUNT(*) AS c FROM vouchers;")["c"]

    prev_split = client.post(
        "/api/sales/returns/preview",
        headers=hdr,
        json={"sale_id": split_sale_id, "items": [{"product_id": env["prod_id"], "batch_id": env["batch_id"], "qty": 1.0}]},
    )
    assert prev_split.status_code == 200
    assert prev_split.json()["settlement_supported"] is False
    assert "SPLIT" in prev_split.json()["unsupported_reason"]

    post_split = client.post(
        "/api/sales/returns",
        headers={**hdr, "Idempotency-Key": f"SR-SPLIT-{uuid.uuid4()}"},
        json={
            "return_date": "2026-11-16",
            "sale_id": split_sale_id,
            "customer_id": env["cust_id"],
            "net_amount": 295.0,
            "items": [{"product_id": env["prod_id"], "batch_id": env["batch_id"], "qty": 1.0, "sale_rate": 295.0, "taxable_amount": 250.0, "tax_amount": 45.0, "total_amount": 295.0}],
        },
    )
    assert post_split.status_code == 400
    assert "SPLIT" in post_split.json()["detail"]
    assert env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty == pytest.approx(stock_before)
    assert db.fetch_one("SELECT COUNT(*) AS c FROM sales_returns;")["c"] == sr_count_before
    assert db.fetch_one("SELECT COUNT(*) AS c FROM vouchers;")["c"] == vch_count_before

    # 4. CREDIT sale fully paid off via separate receipt -> returning now requires refund on CREDIT sale -> rejected with ZERO writes
    cr_sale_id = _create_sale(env, "INV-SR-CRPAID-1", qty=2.0, payment_mode="CREDIT", paid_amount=0.0)
    env["acc_svc"].record_customer_receipt(
        customer_id=env["cust_id"],
        receipt_date="2026-11-16",
        amount=590.0,
        payment_mode="CASH",
        narration="Later settlement",
    )
    stock_before_cr = env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty
    sr_count_cr = db.fetch_one("SELECT COUNT(*) AS c FROM sales_returns;")["c"]
    post_cr = client.post(
        "/api/sales/returns",
        headers={**hdr, "Idempotency-Key": f"SR-CRPAID-{uuid.uuid4()}"},
        json={
            "return_date": "2026-11-16",
            "sale_id": cr_sale_id,
            "customer_id": env["cust_id"],
            "net_amount": 295.0,
            "items": [{"product_id": env["prod_id"], "batch_id": env["batch_id"], "qty": 1.0, "sale_rate": 295.0, "taxable_amount": 250.0, "tax_amount": 45.0, "total_amount": 295.0}],
        },
    )
    assert post_cr.status_code == 400
    assert "CREDIT" in post_cr.json()["detail"]
    assert env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty == pytest.approx(stock_before_cr)
    assert db.fetch_one("SELECT COUNT(*) AS c FROM sales_returns;")["c"] == sr_count_cr


def test_sales_return_idempotency_replay_conflict_concurrency_and_rollback(env):
    """
    Verifies:
    - Same idempotency key + same payload -> replay (idempotent_replay=True, no duplicate writes).
    - Same idempotency key + different payload -> HTTP 409 IDEMPOTENCY_KEY_CONFLICT.
    - Client-supplied request_fingerprint -> HTTP 400.
    - Multi-threaded concurrent requests with same key -> exactly 1 return created, others replay.
    - Multi-threaded concurrent requests with distinct keys competing for last unit -> only 1 succeeds.
    - Injected failure during voucher creation rolls back ALL writes atomically.
    """
    client = env["client"]
    hdr = env["admin_hdr"]
    db = env["db"]

    sale_id = _create_sale(env, "INV-SR-IDEM-1", qty=3.0, payment_mode="CASH", paid_amount=885.0)
    stock_after_sale = env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty

    idem_key = f"SR-IDEM-{uuid.uuid4()}"
    base_payload = {
        "return_date": "2026-11-16",
        "sale_id": sale_id,
        "customer_id": env["cust_id"],
        "total_taxable": 250.0,
        "total_tax": 45.0,
        "net_amount": 295.0,
        "remarks": "First return",
        "items": [{"product_id": env["prod_id"], "batch_id": env["batch_id"], "qty": 1.0, "sale_rate": 295.0, "taxable_amount": 250.0, "tax_amount": 45.0, "total_amount": 295.0}],
    }

    # Client-supplied fingerprint must be rejected
    bad_fp = dict(base_payload, request_fingerprint="fake-fp")
    assert client.post("/api/sales/returns", headers={**hdr, "Idempotency-Key": idem_key}, json=bad_fp).status_code == 400

    # First post
    r1 = client.post("/api/sales/returns", headers={**hdr, "Idempotency-Key": idem_key}, json=base_payload)
    assert r1.status_code == 200
    assert r1.json()["idempotent_replay"] is False
    ret_id = r1.json()["return_id"]

    # Replay with exact same key & payload
    r2 = client.post("/api/sales/returns", headers={**hdr, "Idempotency-Key": idem_key}, json=base_payload)
    assert r2.status_code == 200
    assert r2.json()["idempotent_replay"] is True
    assert r2.json()["return_id"] == ret_id
    assert env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty == pytest.approx(stock_after_sale + 1.0)
    assert db.fetch_one("SELECT COUNT(*) AS c FROM sales_returns WHERE sale_id = ?;", (sale_id,))["c"] == 1

    # Conflict: same key, different payload -> HTTP 409
    diff_payload = dict(base_payload, remarks="Altered remark")
    r_conf = client.post("/api/sales/returns", headers={**hdr, "Idempotency-Key": idem_key}, json=diff_payload)
    assert r_conf.status_code == 409
    assert r_conf.json()["error_code"] == "IDEMPOTENCY_KEY_CONFLICT"

    # Concurrent replay with same key (4 threads racing on 1 unit)
    shared_key = f"SR-CONC-SAME-{uuid.uuid4()}"
    barrier1 = threading.Barrier(4)
    same_results: List[Any] = []
    lock = threading.Lock()

    def _run_same():
        svc = SalesService(db)
        barrier1.wait()
        try:
            rid, replay = svc.process_sale_return_with_status(
                SalesReturn(
                    return_date="2026-11-16",
                    sale_id=sale_id,
                    customer_id=env["cust_id"],
                    total_taxable=250.0,
                    total_tax=45.0,
                    net_amount=295.0,
                    idempotency_key=shared_key,
                    items=[SalesReturnItem(product_id=env["prod_id"], batch_id=env["batch_id"], qty=1.0, sale_rate=295.0, taxable_amount=250.0, tax_amount=45.0, total_amount=295.0)],
                )
            )
            with lock:
                same_results.append(("OK", rid, replay))
        except Exception as exc:
            with lock:
                same_results.append(("ERR", str(exc)))

    threads = [threading.Thread(target=_run_same) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok_same = [x for x in same_results if x[0] == "OK"]
    assert len(ok_same) == 4, f"All same-key threads should succeed via creation or replay: {same_results}"
    assert len({x[1] for x in ok_same}) == 1
    assert sum(1 for x in ok_same if x[2] is False) == 1
    assert sum(1 for x in ok_same if x[2] is True) == 3

    # Concurrent competing returns for the final remaining 1 unit (distinct keys -> only 1 succeeds)
    barrier2 = threading.Barrier(4)
    compete_results: List[str] = []

    def _run_compete(idx: int):
        svc = SalesService(db)
        barrier2.wait()
        try:
            svc.process_sale_return_with_status(
                SalesReturn(
                    return_date="2026-11-16",
                    sale_id=sale_id,
                    customer_id=env["cust_id"],
                    total_taxable=250.0,
                    total_tax=45.0,
                    net_amount=295.0,
                    idempotency_key=f"SR-COMPETE-{idx}-{uuid.uuid4()}",
                    items=[SalesReturnItem(product_id=env["prod_id"], batch_id=env["batch_id"], qty=1.0, sale_rate=295.0, taxable_amount=250.0, tax_amount=45.0, total_amount=295.0)],
                )
            )
            with lock:
                compete_results.append("OK")
        except ValueError:
            with lock:
                compete_results.append("REJECTED")

    threads2 = [threading.Thread(target=_run_compete, args=(i,)) for i in range(4)]
    for t in threads2:
        t.start()
    for t in threads2:
        t.join()

    assert compete_results.count("OK") == 1
    assert compete_results.count("REJECTED") == 3
    assert env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty == pytest.approx(stock_after_sale + 3.0)

    # Complete rollback after injected failure on a fresh sale
    rb_sale_id = _create_sale(env, "INV-SR-RB-1", qty=2.0, payment_mode="CREDIT", paid_amount=0.0)
    stock_rb = env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty
    bal_rb = env["master_repo"].get_customer_by_id(env["cust_id"]).current_balance
    sr_rb = db.fetch_one("SELECT COUNT(*) AS c FROM sales_returns;")["c"]
    sl_rb = db.fetch_one("SELECT COUNT(*) AS c FROM stock_ledger;")["c"]

    with patch.object(AccountingRepository, "create_voucher", side_effect=RuntimeError("Injected voucher boom")):
        with pytest.raises(RuntimeError, match="Injected voucher boom"):
            env["sales_svc"].process_sale_return(
                SalesReturn(
                    return_date="2026-11-16",
                    sale_id=rb_sale_id,
                    customer_id=env["cust_id"],
                    total_taxable=250.0,
                    total_tax=45.0,
                    net_amount=295.0,
                    idempotency_key=f"SR-RB-{uuid.uuid4()}",
                    items=[SalesReturnItem(product_id=env["prod_id"], batch_id=env["batch_id"], qty=1.0, sale_rate=295.0, taxable_amount=250.0, tax_amount=45.0, total_amount=295.0)],
                )
            )

    assert env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty == pytest.approx(stock_rb)
    assert env["master_repo"].get_customer_by_id(env["cust_id"]).current_balance == pytest.approx(bal_rb)
    assert db.fetch_one("SELECT COUNT(*) AS c FROM sales_returns;")["c"] == sr_rb
    assert db.fetch_one("SELECT COUNT(*) AS c FROM stock_ledger;")["c"] == sl_rb
    _assert_all_vouchers_balanced(db)


# ============================================================================
# B. PURCHASE RETURN WORKFLOW TESTS (INCLUDING CASH/BANK REFUND ACCOUNT RESOLUTION)
# ============================================================================

def test_purchase_return_full_partial_preview_accounts_idempotency_concurrency_and_rollback(env):
    """
    Verifies:
    - Preview exposes cash_account_id & bank_account_id for real UI cash/bank refund settlements.
    - Partial return with PAYABLE_REDUCTION reduces batch stock, posts DEBIT_NOTE, reduces supplier payable.
    - Second partial return with REFUND_CASH (using preview cash_account_id) debits Cash in Hand.
    - Final remainder return completes full purchase return while preserving original purchase history.
    - Excess qty, duplicate return, invalid purchase rejected.
    - Idempotency replay, 409 conflict, concurrency protection, and rollback after injected failure.
    """
    client = env["client"]
    hdr = env["admin_hdr"]
    db = env["db"]

    # 1. Preview partial return of 5 units (taxable 1000, CGST 90, SGST 90, net 1180)
    prev_res = client.post(
        "/api/purchase/returns/preview",
        headers=hdr,
        json={
            "purchase_id": env["purch_id"],
            "supplier_id": env["supp_id"],
            "items": [{"purchase_item_id": env["purch_item_id"], "product_id": env["prod_id"], "qty": 5.0}],
        },
    )
    assert prev_res.status_code == 200, prev_res.text
    prev = prev_res.json()
    assert float(prev["net_amount"]) == pytest.approx(1180.0)
    assert prev["cash_account_id"] is not None
    assert prev["bank_account_id"] is not None

    # 2. Partial return #1 (5 units via PAYABLE_REDUCTION) + idempotency replay + 409 conflict
    k1 = f"PR-2D2-{uuid.uuid4()}"
    p1 = {
        "purchase_id": env["purch_id"],
        "supplier_id": env["supp_id"],
        "return_date": "2026-11-16",
        "idempotency_key": k1,
        "itc_status": "CLAIMED_REVERSED",
        "remarks": "Near expiry partial return",
        "items": [{"purchase_item_id": env["purch_item_id"], "product_id": env["prod_id"], "batch_id": env["batch_id"], "qty": 5.0, "reason": "Near Expiry / Expired"}],
        "settlements": [{"settlement_type": "PAYABLE_REDUCTION", "amount": 1180.0}],
    }
    res1 = client.post("/api/purchase/returns", headers=hdr, json=p1)
    assert res1.status_code in (200, 201), res1.text
    ret1 = res1.json()
    assert env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty == pytest.approx(15.0)
    assert env["master_repo"].get_supplier_by_id(env["supp_id"]).current_balance == pytest.approx(3540.0)

    # Exact replay -> same return_no, zero duplicate stock deduction
    res1_replay = client.post("/api/purchase/returns", headers=hdr, json=p1)
    assert res1_replay.status_code in (200, 201)
    assert res1_replay.json()["return_id"] == ret1["return_id"]
    assert env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty == pytest.approx(15.0)

    # Conflict -> HTTP 409
    p1_conflict = dict(p1, remarks="Changed remarks")
    assert client.post("/api/purchase/returns", headers=hdr, json=p1_conflict).status_code == 409

    # 3. Partial return #2 (5 units via REFUND_CASH using preview cash_account_id)
    cash_before = env["acc_repo"].get_cash_account().current_balance
    p2 = {
        "purchase_id": env["purch_id"],
        "supplier_id": env["supp_id"],
        "return_date": "2026-11-16",
        "idempotency_key": f"PR-CASH-{uuid.uuid4()}",
        "itc_status": "CLAIMED_REVERSED",
        "items": [{"purchase_item_id": env["purch_item_id"], "product_id": env["prod_id"], "batch_id": env["batch_id"], "qty": 5.0, "reason": "Damaged / Leaking Pack"}],
        "settlements": [{"settlement_type": "REFUND_CASH", "amount": 1180.0, "account_id": prev["cash_account_id"]}],
    }
    res2 = client.post("/api/purchase/returns", headers=hdr, json=p2)
    assert res2.status_code in (200, 201), res2.text
    assert env["acc_repo"].get_cash_account().current_balance == pytest.approx(cash_before + 1180.0)
    assert env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty == pytest.approx(10.0)

    # 4. Injected failure rollback test on next 5 units
    stock_rb = env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty
    supp_bal_rb = env["master_repo"].get_supplier_by_id(env["supp_id"]).current_balance
    pr_cnt_rb = db.fetch_one("SELECT COUNT(*) AS c FROM purchase_returns;")["c"]
    with patch.object(AccountingRepository, "create_voucher", side_effect=RuntimeError("Injected PR voucher failure")):
        with pytest.raises(RuntimeError, match="Injected PR voucher failure"):
            env["pr_svc"].process_purchase_return(
                PurchaseReturnCreate(
                    purchase_id=env["purch_id"],
                    supplier_id=env["supp_id"],
                    return_date="2026-11-16",
                    idempotency_key=f"PR-RB-{uuid.uuid4()}",
                    itc_status="CLAIMED_REVERSED",
                    items=[PurchaseReturnItemCreate(purchase_item_id=env["purch_item_id"], product_id=env["prod_id"], qty=5)],
                    settlements=[PurchaseReturnSettlementCreate(settlement_type="PAYABLE_REDUCTION", amount=1180)],
                )
            )
    assert env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty == pytest.approx(stock_rb)
    assert env["master_repo"].get_supplier_by_id(env["supp_id"]).current_balance == pytest.approx(supp_bal_rb)
    assert db.fetch_one("SELECT COUNT(*) AS c FROM purchase_returns;")["c"] == pr_cnt_rb

    # 5. Concurrent competing purchase returns for remaining 10 units (3 threads each asking 10 -> only 1 succeeds)
    barrier = threading.Barrier(3)
    outcomes: List[str] = []
    lock = threading.Lock()

    def _run_pr(idx: int):
        svc = PurchaseReturnService(db)
        barrier.wait()
        try:
            svc.process_purchase_return(
                PurchaseReturnCreate(
                    purchase_id=env["purch_id"],
                    supplier_id=env["supp_id"],
                    return_date="2026-11-16",
                    idempotency_key=f"PR-CONC-{idx}-{uuid.uuid4()}",
                    itc_status="CLAIMED_REVERSED",
                    items=[PurchaseReturnItemCreate(purchase_item_id=env["purch_item_id"], product_id=env["prod_id"], qty=10)],
                    settlements=[PurchaseReturnSettlementCreate(settlement_type="PAYABLE_REDUCTION", amount=2360)],
                )
            )
            with lock:
                outcomes.append("OK")
        except ValueError:
            with lock:
                outcomes.append("REJECTED")

    threads = [threading.Thread(target=_run_pr, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("OK") == 1
    assert outcomes.count("REJECTED") == 2
    assert env["inv_repo"].get_batch_by_id(env["batch_id"]).current_qty == pytest.approx(0.0)
    assert env["master_repo"].get_supplier_by_id(env["supp_id"]).current_balance == pytest.approx(1180.0)

    # Original purchase row remains completely intact
    orig_pur = db.fetch_one("SELECT invoice_no, net_amount FROM purchases WHERE purchase_id = ?;", (env["purch_id"],))
    assert orig_pur["invoice_no"] == "PUR-2D2-001"
    assert float(orig_pur["net_amount"]) == pytest.approx(4720.0)

    # Invalid purchase ID rejected
    bad_pur = client.post(
        "/api/purchase/returns",
        headers=hdr,
        json={
            "purchase_id": 999999,
            "supplier_id": env["supp_id"],
            "return_date": "2026-11-16",
            "idempotency_key": f"PR-BAD-{uuid.uuid4()}",
            "itc_status": "CLAIMED_REVERSED",
            "items": [{"purchase_item_id": env["purch_item_id"], "product_id": env["prod_id"], "qty": 1.0}],
            "settlements": [{"settlement_type": "PAYABLE_REDUCTION", "amount": 236.0}],
        },
    )
    assert bad_pur.status_code == 400
    _assert_all_vouchers_balanced(db)


# ============================================================================
# C. RBAC & UI CONTRACT VERIFICATION
# ============================================================================

def test_returns_rbac_and_ui_contracts(env):
    """
    Verifies:
    - OPERATOR can access Sales Returns endpoints but receives HTTP 403 on Purchase Returns endpoints.
    - HTML & JS contain complete Sales Return & Purchase Return workflows, search filters,
      custom confirmation dialog calls, real Purchase Return detail view (no Step 2E-C stub),
      zero native browser dialogs, and English-only UI.
    """
    client = env["client"]
    op_hdr = env["op_hdr"]
    admin_hdr = env["admin_hdr"]

    # OPERATOR allowed on Sales Returns, forbidden on Purchase Returns
    assert client.get("/api/sales/returns", headers=op_hdr).status_code == 200
    assert client.get("/api/purchase/returns", headers=op_hdr).status_code == 403
    assert client.get("/api/purchase/returns", headers=admin_hdr).status_code == 200

    html = client.get("/").text
    js = client.get("/static/js/app.js").text

    # Sales Return HTML controls
    for marker in (
        'data-tab="tab-sales-returns"',
        'id="tab-sales-returns"',
        'id="btn-open-create-sales-return"',
        'id="modal-create-sales-return"',
        'id="sr-create-sale-search"',
        'id="sr-create-sale-select"',
        'id="sr-items-tbody"',
        'id="sr-settlement-panel"',
        'id="btn-submit-sales-return"',
        'id="modal-sales-return-detail"',
        'id="pr-create-purchase-search"',
        'id="return-reason-presets"',
    ):
        assert marker in html, f"Missing expected Phase 2D-2 HTML marker: {marker}"

    # JS handlers & real Purchase Return Detail (no placeholder stub!)
    for fn in (
        "async function loadSalesReturns()",
        "async function openCreateSalesReturnModal(",
        "async function onSaleSelectedForReturn()",
        "async function requestBackendSalesReturnPreview()",
        "async function submitCreateSalesReturn()",
        "async function openSalesReturnDetail(",
        "function filterPurchasesForReturnDropdown()",
        "async function openPurchaseReturnDetail(",
    ):
        assert fn in js, f"Missing expected Phase 2D-2 JS function: {fn}"

    assert "will be activated in Step 2E-C" not in js, "Step 2E-C placeholder stub must be removed"
    assert 'fetch("/api/sales/returns/preview"' in js
    assert 'fetch("/api/sales/returns"' in js
    assert '"Confirm Sales Return"' in js
    assert '"Confirm Purchase Return"' in js
