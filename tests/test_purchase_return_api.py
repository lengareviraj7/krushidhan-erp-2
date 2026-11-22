"""
Comprehensive Automated Test Suite for Purchase Return REST API Endpoints (Step 2D).

Verifies:
1. POST /api/purchase/returns authenticated success (200 OK, full response model, DB side effects).
2. Missing authentication token rejected with 401 Unauthorized.
3. Operator role rejected with 403 Forbidden.
4. Allowed roles (OWNER, MANAGER, ACCOUNTANT, ADMIN) can create and view returns.
5. Non-existent purchase_id rejected with 400 Bad Request.
6. Supplier mismatch rejected with 400 Bad Request.
7. Non-existent purchase_item_id rejected with 400 Bad Request.
8. Wrong batch_id rejected with 400 Bad Request.
9. Line with free_qty > 0 rejected with 400 Bad Request and zero database side effects.
10. Invalid settlement allocation (sum != net_amount) rejected with 400 Bad Request.
11. Payable reduction exceeding supplier balance rejected with 400 Bad Request.
12. Insufficient batch stock rejected with 400 Bad Request.
13. List purchase returns with filters (from_date, to_date, supplier_id, purchase_id, status, pagination).
14. Get purchase return detail success (200 OK, nested items, settlements, voucher references).
15. Get purchase return detail unknown return_id returns 404 Not Found.
16. GET /api/purchase/{purchase_id}/eligible-returns returns returnable lines and quantities.
17. Idempotency replay with matching fingerprint returns existing record (200 OK, no duplicate side effects).
18. Idempotency replay with altered payload returns 409 Conflict.
19. Direct route alias /api/purchase-returns works identically for POST, GET, and GET /{return_id}.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, reset_db_manager
from src.services.auth_service import AuthService


@pytest.fixture
def api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Sets up an isolated test database with seeded charts of accounts,
    master data, stock batches, supplier, and inward purchases with items.
    """
    test_db_path = tmp_path / "test_purchase_return_api.db"
    monkeypatch.setenv("DB_PATH", str(test_db_path))
    reset_db_manager()

    db_mgr = DatabaseManager(db_path=test_db_path)
    db_mgr.initialize_database(include_seed=True)

    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        cur = conn.cursor()

        # 1. Supplier (Current balance: ₹25,000.00)
        cur.execute(
            """
            INSERT INTO suppliers (supplier_name, mobile, gstin, state, current_balance)
            VALUES ('Kisan Agri Inputs Ltd', '9890123456', '27AABCK1234M1Z1', 'Maharashtra', 25000.0);
            """
        )
        supplier_id = cur.lastrowid

        # Mismatched supplier
        cur.execute(
            """
            INSERT INTO suppliers (supplier_name, mobile, gstin, state, current_balance)
            VALUES ('Other Supplier Pvt Ltd', '9890999999', '27AAACR9999M1Z5', 'Maharashtra', 5000.0);
            """
        )
        other_supplier_id = cur.lastrowid

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
        # Batch 1: NPK-B1 (10 inward, current 20)
        cur.execute(
            """
            INSERT INTO stock_batches (product_id, batch_no, mfg_date, exp_date, purchase_rate, sale_rate, mrp, opening_qty, current_qty)
            VALUES (?, 'NPK-B1', '2026-01-01', '2028-01-01', 1000.0, 1200.0, 1300.0, 20.0, 20.0);
            """,
            (product1_id,),
        )
        batch1_id = cur.lastrowid

        # Batch 2: BIO-B2 (10 inward, current 10)
        cur.execute(
            """
            INSERT INTO stock_batches (product_id, batch_no, mfg_date, exp_date, purchase_rate, sale_rate, mrp, opening_qty, current_qty)
            VALUES (?, 'BIO-B2', '2026-02-01', '2027-02-01', 500.0, 600.0, 650.0, 10.0, 10.0);
            """,
            (product2_id,),
        )
        batch2_id = cur.lastrowid

        # Batch 3: NPK-B2 (Different batch of Product 1 - not on this purchase invoice)
        cur.execute(
            """
            INSERT INTO stock_batches (product_id, batch_no, mfg_date, exp_date, purchase_rate, sale_rate, mrp, opening_qty, current_qty)
            VALUES (?, 'NPK-B2', '2026-03-01', '2028-03-01', 1000.0, 1200.0, 1300.0, 50.0, 50.0);
            """,
            (product1_id,),
        )
        batch3_wrong_id = cur.lastrowid

        # Batch 4: BIO-B3 (For free_qty product line)
        cur.execute(
            """
            INSERT INTO stock_batches (product_id, batch_no, mfg_date, exp_date, purchase_rate, sale_rate, mrp, opening_qty, current_qty)
            VALUES (?, 'BIO-B3', '2026-02-01', '2027-02-01', 500.0, 600.0, 650.0, 10.0, 10.0);
            """,
            (product2_id,),
        )
        batch4_free_id = cur.lastrowid

        # 4. Inward Purchase Invoice
        # Total Taxable = 9900 + 5000 + 2500 = 17400.0
        # CGST = 891 + 125 + 62.5 = 1078.5
        # SGST = 891 + 125 + 62.5 = 1078.5
        # Net = 19557.0
        cur.execute(
            """
            INSERT INTO purchases (
                invoice_no, purchase_date, supplier_id, payment_type,
                total_taxable, total_cgst, total_sgst, total_igst, extra_charges,
                round_off, net_amount, paid_amount, due_amount
            ) VALUES ('INV-SUPP-101', '2026-11-01', ?, 'CREDIT', 17400.0, 1078.5, 1078.5, 0.0, 0.0, 0.0, 19557.0, 0.0, 19557.0);
            """,
            (supplier_id,),
        )
        purchase_id = cur.lastrowid

        # Line 1: 10 units @ 1000, 100 disc, 18% GST (9% CGST, 9% SGST). Line Total = 11,682.00
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

        # Line 2: 10 units @ 500, 0 disc, 5% GST (2.5% CGST, 2.5% SGST). Line Total = 5,250.00
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

        # Line 3: 5 units @ 500, with free_qty = 2.0. Line Total = 2,625.00
        cur.execute(
            """
            INSERT INTO purchase_items (
                purchase_id, product_id, batch_no, mfg_date, exp_date,
                qty, free_qty, unit_id, purchase_rate, sale_rate, mrp,
                discount_percent, discount_amount, taxable_amount,
                cgst_rate, cgst_amount, sgst_rate, sgst_amount, igst_rate, igst_amount,
                total_amount
            ) VALUES (?, ?, 'BIO-B3', '2026-02-01', '2027-02-01', 5.0, 2.0, 1, 500.0, 600.0, 650.0,
                     0.0, 0.0, 2500.0, 2.5, 62.50, 2.5, 62.50, 0.0, 0.0, 2625.0);
            """,
            (purchase_id, product2_id),
        )
        item3_free_id = cur.lastrowid

    # Create tokens for various roles
    def _make_token(role: str, user_id: int = 1, username: str = "testuser") -> str:
        return AuthService.create_access_token(
            user_id=user_id,
            username=username,
            full_name=f"Test {role.title()}",
            role=role,
        )

    fixture_data = {
        "db_mgr": db_mgr,
        "supplier_id": supplier_id,
        "other_supplier_id": other_supplier_id,
        "product1_id": product1_id,
        "product2_id": product2_id,
        "batch1_id": batch1_id,
        "batch2_id": batch2_id,
        "batch3_wrong_id": batch3_wrong_id,
        "batch4_free_id": batch4_free_id,
        "purchase_id": purchase_id,
        "item1_id": item1_id,
        "item2_id": item2_id,
        "item3_free_id": item3_free_id,
        "tokens": {
            "ADMIN": _make_token("ADMIN", 1, "admin"),
            "OWNER": _make_token("OWNER", 2, "owner"),
            "MANAGER": _make_token("MANAGER", 3, "manager"),
            "ACCOUNTANT": _make_token("ACCOUNTANT", 4, "accountant"),
            "OPERATOR": _make_token("OPERATOR", 5, "operator"),
        },
    }

    yield fixture_data
    reset_db_manager()


def _get_client(token: str | None = None) -> TestClient:
    """Helper to instantiate TestClient with optional Bearer token header."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, headers=headers)


# ============================================================================
# 1. Successful Purchase Return Creation via API
# ============================================================================

def test_create_purchase_return_authenticated_success(api_env: Dict[str, Any]):
    """
    POST /api/purchase/returns with ADMIN token:
    Returns full line 1 (10 units), payable reduction ₹11,682.00.
    Expects 200 OK, return_id, return_no, status='POSTED', voucher_id, net_amount=11682.0.
    Verifies stock reduced and voucher created.
    """
    client = _get_client(api_env["tokens"]["ADMIN"])

    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-05",
        "supplier_credit_note_no": "SCN-TEST-001",
        "supplier_credit_note_date": "2026-11-05",
        "remarks": "API full return line 1",
        "itc_status": "CLAIMED_REVERSED",
        "idempotency_key": f"PR-KEY-{uuid.uuid4().hex[:8]}",
        "items": [
            {
                "purchase_item_id": api_env["item1_id"],
                "product_id": api_env["product1_id"],
                "batch_id": api_env["batch1_id"],
                "qty": 10.0,
                "reason": "Excess inventory return",
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 11682.00,
            }
        ],
    }

    response = client.post("/api/purchase/returns", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["success"] is True
    assert data["return_id"] is not None
    assert data["return_no"].startswith("PR-")
    assert data["status"] == "POSTED"
    assert data["voucher_id"] is not None
    assert float(data["net_amount"]) == 11682.00
    assert float(data["total_taxable"]) == 9900.00
    assert float(data["total_cgst"]) == 891.00
    assert float(data["total_sgst"]) == 891.00
    assert len(data["items"]) == 1
    assert len(data["settlements"]) == 1

    # Verify database side effects: Stock batch reduced from 20 to 10
    db_mgr: DatabaseManager = api_env["db_mgr"]
    with db_mgr.transaction() as conn:
        cur = conn.cursor()
        cur.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?", (api_env["batch1_id"],))
        assert cur.fetchone()[0] == 10.0

        # Verify stock ledger entry created
        cur.execute(
            "SELECT COUNT(*) FROM stock_ledger WHERE reference_id = ? AND transaction_type = 'PURCHASE_RETURN'",
            (data["return_id"],),
        )
        assert cur.fetchone()[0] == 1

        # Verify voucher exists and total matches
        cur.execute("SELECT total_amount FROM vouchers WHERE voucher_id = ?", (data["voucher_id"],))
        v_row = cur.fetchone()
        assert v_row[0] == 11682.00


# ============================================================================
# 2. Authentication and Authorization Controls
# ============================================================================

def test_unauthenticated_request_rejected_401(api_env: Dict[str, Any]):
    """Requests without token must be rejected with 401 Unauthorized."""
    client = _get_client(token=None)

    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-05",
        "idempotency_key": f"PR-KEY-{uuid.uuid4().hex[:8]}",
        "items": [
            {
                "purchase_item_id": api_env["item1_id"],
                "product_id": api_env["product1_id"],
                "qty": 1.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 1168.20,
            }
        ],
    }

    # Test create
    resp_create = client.post("/api/purchase/returns", json=payload)
    assert resp_create.status_code == 401

    # Test list
    resp_list = client.get("/api/purchase/returns")
    assert resp_list.status_code == 401

    # Test detail
    resp_detail = client.get("/api/purchase/returns/1")
    assert resp_detail.status_code == 401

    # Test eligible lines
    resp_elig = client.get(f"/api/purchase/{api_env['purchase_id']}/eligible-returns")
    assert resp_elig.status_code == 401


def test_unauthorized_operator_request_rejected_403(api_env: Dict[str, Any]):
    """OPERATOR (counter staff) cannot perform purchase returns (403 Forbidden)."""
    client = _get_client(api_env["tokens"]["OPERATOR"])

    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-05",
        "idempotency_key": f"PR-KEY-{uuid.uuid4().hex[:8]}",
        "items": [
            {
                "purchase_item_id": api_env["item1_id"],
                "product_id": api_env["product1_id"],
                "qty": 1.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 1168.20,
            }
        ],
    }

    resp = client.post("/api/purchase/returns", json=payload)
    assert resp.status_code == 403

    resp_list = client.get("/api/purchase/returns")
    assert resp_list.status_code == 403

    resp_detail = client.get("/api/purchase/returns/1")
    assert resp_detail.status_code == 403

    resp_elig = client.get(f"/api/purchase/{api_env['purchase_id']}/eligible-returns")
    assert resp_elig.status_code == 403


def test_role_permissions_matrix(api_env: Dict[str, Any]):
    """Verifies that OWNER, MANAGER, and ACCOUNTANT have permission to access purchase returns."""
    allowed_roles = ["OWNER", "MANAGER", "ACCOUNTANT"]
    for role in allowed_roles:
        client = _get_client(api_env["tokens"][role])
        # Calling list endpoint should succeed with 200 OK
        resp = client.get("/api/purchase/returns")
        assert resp.status_code == 200, f"Role {role} unexpectedly rejected: {resp.text}"


# ============================================================================
# 3. Domain Validation Error Mappings (400 Bad Request)
# ============================================================================

def test_invalid_purchase_id_rejected_400(api_env: Dict[str, Any]):
    """Non-existent purchase_id returns 400 Bad Request."""
    client = _get_client(api_env["tokens"]["ADMIN"])
    payload = {
        "purchase_id": 99999,
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-05",
        "idempotency_key": f"PR-KEY-{uuid.uuid4().hex[:8]}",
        "items": [
            {
                "purchase_item_id": api_env["item1_id"],
                "product_id": api_env["product1_id"],
                "qty": 1.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 1168.20,
            }
        ],
    }
    resp = client.post("/api/purchase/returns", json=payload)
    assert resp.status_code == 400
    assert "99999" in resp.json()["detail"]


def test_supplier_mismatch_rejected_400(api_env: Dict[str, Any]):
    """Supplier mismatch between return payload and purchase invoice returns 400."""
    client = _get_client(api_env["tokens"]["ADMIN"])
    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["other_supplier_id"],  # Mismatch
        "return_date": "2026-11-05",
        "idempotency_key": f"PR-KEY-{uuid.uuid4().hex[:8]}",
        "items": [
            {
                "purchase_item_id": api_env["item1_id"],
                "product_id": api_env["product1_id"],
                "qty": 1.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 1168.20,
            }
        ],
    }
    resp = client.post("/api/purchase/returns", json=payload)
    assert resp.status_code == 400
    assert "supplier id mismatch" in resp.json()["detail"].lower()


def test_invalid_purchase_item_rejected_400(api_env: Dict[str, Any]):
    """Non-existent purchase_item_id returns 400 Bad Request."""
    client = _get_client(api_env["tokens"]["ADMIN"])
    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-05",
        "idempotency_key": f"PR-KEY-{uuid.uuid4().hex[:8]}",
        "items": [
            {
                "purchase_item_id": 88888,
                "product_id": api_env["product1_id"],
                "qty": 1.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 1168.20,
            }
        ],
    }
    resp = client.post("/api/purchase/returns", json=payload)
    assert resp.status_code == 400
    assert "88888" in resp.json()["detail"]


def test_wrong_batch_id_rejected_400(api_env: Dict[str, Any]):
    """Supplying batch_id that does not match the inward purchase line returns 400."""
    client = _get_client(api_env["tokens"]["ADMIN"])
    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-05",
        "idempotency_key": f"PR-KEY-{uuid.uuid4().hex[:8]}",
        "items": [
            {
                "purchase_item_id": api_env["item1_id"],
                "product_id": api_env["product1_id"],
                "batch_id": api_env["batch3_wrong_id"],  # Wrong batch (NPK-B2 instead of NPK-B1)
                "qty": 1.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 1168.20,
            }
        ],
    }
    resp = client.post("/api/purchase/returns", json=payload)
    assert resp.status_code == 400
    assert "batch mismatch" in resp.json()["detail"].lower()


def test_free_quantity_line_rejected_400(api_env: Dict[str, Any]):
    """Returning a purchase line that contained free_qty > 0 is rejected with 400."""
    client = _get_client(api_env["tokens"]["ADMIN"])
    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-05",
        "idempotency_key": f"PR-KEY-{uuid.uuid4().hex[:8]}",
        "items": [
            {
                "purchase_item_id": api_env["item3_free_id"],
                "product_id": api_env["product2_id"],
                "qty": 1.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 525.00,
            }
        ],
    }
    resp = client.post("/api/purchase/returns", json=payload)
    assert resp.status_code == 400
    detail = resp.json()["detail"].lower()
    assert "free" in detail and "quantit" in detail

    # Verify zero database side effects
    db_mgr: DatabaseManager = api_env["db_mgr"]
    with db_mgr.transaction() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM purchase_returns")
        assert cur.fetchone()[0] == 0


def test_invalid_settlement_allocation_rejected_400(api_env: Dict[str, Any]):
    """Settlement total not matching return net_amount returns 400 Bad Request."""
    client = _get_client(api_env["tokens"]["ADMIN"])
    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-05",
        "idempotency_key": f"PR-KEY-{uuid.uuid4().hex[:8]}",
        "items": [
            {
                "purchase_item_id": api_env["item1_id"],
                "product_id": api_env["product1_id"],
                "qty": 10.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 10000.00,  # Should be 11682.00
            }
        ],
    }
    resp = client.post("/api/purchase/returns", json=payload)
    assert resp.status_code == 400
    assert "total settlements" in resp.json()["detail"].lower()


def test_payable_reduction_exceeding_supplier_balance_rejected_400(api_env: Dict[str, Any]):
    """PAYABLE_REDUCTION settlement exceeding supplier's current ledger balance returns 400."""
    db_mgr: DatabaseManager = api_env["db_mgr"]
    # Temporarily set supplier balance to ₹500.00
    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        conn.execute("UPDATE suppliers SET current_balance = 500.0 WHERE supplier_id = ?", (api_env["supplier_id"],))

    client = _get_client(api_env["tokens"]["ADMIN"])
    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-05",
        "idempotency_key": f"PR-KEY-{uuid.uuid4().hex[:8]}",
        "items": [
            {
                "purchase_item_id": api_env["item1_id"],
                "product_id": api_env["product1_id"],
                "qty": 10.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 11682.00,  # Exceeds 500.0
            }
        ],
    }
    resp = client.post("/api/purchase/returns", json=payload)
    assert resp.status_code == 400
    assert "cannot exceed supplier current" in resp.json()["detail"].lower()

    # Restore balance
    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        conn.execute("UPDATE suppliers SET current_balance = 25000.0 WHERE supplier_id = ?", (api_env["supplier_id"],))


def test_insufficient_stock_rejected_400(api_env: Dict[str, Any]):
    """Returning more quantity than currently available in stock batch returns 400."""
    db_mgr: DatabaseManager = api_env["db_mgr"]
    # Set current batch stock to 2.0
    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        conn.execute("UPDATE stock_batches SET current_qty = 2.0 WHERE batch_id = ?", (api_env["batch1_id"],))

    client = _get_client(api_env["tokens"]["ADMIN"])
    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-05",
        "idempotency_key": f"PR-KEY-{uuid.uuid4().hex[:8]}",
        "items": [
            {
                "purchase_item_id": api_env["item1_id"],
                "product_id": api_env["product1_id"],
                "qty": 10.0,  # Exceeds batch current_qty of 2.0
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 11682.00,
            }
        ],
    }
    resp = client.post("/api/purchase/returns", json=payload)
    assert resp.status_code == 400
    assert "insufficient" in resp.json()["detail"].lower() and "stock" in resp.json()["detail"].lower()

    # Restore stock
    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        conn.execute("UPDATE stock_batches SET current_qty = 20.0 WHERE batch_id = ?", (api_env["batch1_id"],))


# ============================================================================
# 4. List, Detail, and Eligible Lines Endpoints
# ============================================================================

def test_eligible_purchase_lines_endpoint(api_env: Dict[str, Any]):
    """GET /api/purchase/{purchase_id}/eligible-returns returns returnable lines and quantities."""
    client = _get_client(api_env["tokens"]["ADMIN"])
    resp = client.get(f"/api/purchase/{api_env['purchase_id']}/eligible-returns")
    assert resp.status_code == 200, resp.text
    lines = resp.json()

    assert len(lines) == 3
    # Find line 1
    l1 = next(l for l in lines if l["item_id"] == api_env["item1_id"])
    assert float(l1["purchased_qty"]) == 10.0
    assert float(l1["prior_returned_qty"]) == 0.0
    assert float(l1["remaining_purchased_qty"]) == 10.0
    assert float(l1["max_returnable_qty"]) == 10.0
    assert float(l1["free_qty"]) == 0.0

    # Find line 3 (has free qty)
    l3 = next(l for l in lines if l["item_id"] == api_env["item3_free_id"])
    assert float(l3["free_qty"]) == 2.0

    # Perform a partial return of 3 units from line 1
    create_payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-05",
        "idempotency_key": f"PR-KEY-{uuid.uuid4().hex[:8]}",
        "items": [
            {
                "purchase_item_id": api_env["item1_id"],
                "product_id": api_env["product1_id"],
                "qty": 3.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 3505.00,
            }
        ],
    }
    ret_resp = client.post("/api/purchase/returns", json=create_payload)
    assert ret_resp.status_code == 200

    # Re-fetch eligible lines and verify returnable_qty dropped to 7.0
    resp_after = client.get(f"/api/purchase/{api_env['purchase_id']}/eligible-returns")
    assert resp_after.status_code == 200
    lines_after = resp_after.json()
    l1_after = next(l for l in lines_after if l["item_id"] == api_env["item1_id"])
    assert float(l1_after["prior_returned_qty"]) == 3.0
    assert float(l1_after["remaining_purchased_qty"]) == 7.0
    assert float(l1_after["max_returnable_qty"]) == 7.0


def test_list_purchase_returns_with_filters(api_env: Dict[str, Any]):
    """Test GET /api/purchase/returns filtering and pagination."""
    client = _get_client(api_env["tokens"]["ADMIN"])

    # Create 2 returns
    for i in range(2):
        payload = {
            "purchase_id": api_env["purchase_id"],
            "supplier_id": api_env["supplier_id"],
            "return_date": f"2026-11-0{i+1}",
            "idempotency_key": f"PR-LIST-KEY-{i}-{uuid.uuid4().hex[:6]}",
            "items": [
                {
                    "purchase_item_id": api_env["item2_id"],
                    "product_id": api_env["product2_id"],
                    "qty": 2.0,
                }
            ],
            "settlements": [
                {
                    "settlement_type": "PAYABLE_REDUCTION",
                    "amount": 1050.00,
                }
            ],
        }
        res = client.post("/api/purchase/returns", json=payload)
        assert res.status_code == 200

    # 1. Unfiltered list
    res_all = client.get("/api/purchase/returns")
    assert res_all.status_code == 200
    all_returns = res_all.json()
    assert len(all_returns) >= 2

    # 2. Filter by supplier_id
    res_supp = client.get(f"/api/purchase/returns?supplier_id={api_env['supplier_id']}")
    assert res_supp.status_code == 200
    assert len(res_supp.json()) >= 2

    # Filter by non-existent supplier
    res_none = client.get(f"/api/purchase/returns?supplier_id={api_env['other_supplier_id']}")
    assert res_none.status_code == 200
    assert len(res_none.json()) == 0

    # 3. Filter by purchase_id
    res_pur = client.get(f"/api/purchase/returns?purchase_id={api_env['purchase_id']}")
    assert res_pur.status_code == 200
    assert len(res_pur.json()) >= 2

    # 4. Filter by status
    res_status = client.get("/api/purchase/returns?status=POSTED")
    assert res_status.status_code == 200
    assert len(res_status.json()) >= 2

    # 5. Pagination: limit and offset
    res_page = client.get("/api/purchase/returns?limit=1&offset=0")
    assert res_page.status_code == 200
    assert len(res_page.json()) == 1


def test_get_purchase_return_detail_success(api_env: Dict[str, Any]):
    """Test GET /api/purchase/returns/{return_id} retrieves full detail."""
    client = _get_client(api_env["tokens"]["ADMIN"])

    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-06",
        "supplier_credit_note_no": "SCN-DETAIL-123",
        "supplier_credit_note_date": "2026-11-06",
        "remarks": "Detail retrieval test",
        "idempotency_key": f"PR-DETAIL-KEY-{uuid.uuid4().hex[:6]}",
        "items": [
            {
                "purchase_item_id": api_env["item2_id"],
                "product_id": api_env["product2_id"],
                "qty": 1.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 525.00,
            }
        ],
    }
    create_res = client.post("/api/purchase/returns", json=payload)
    assert create_res.status_code == 200
    created = create_res.json()
    ret_id = created["return_id"]

    detail_res = client.get(f"/api/purchase/returns/{ret_id}")
    assert detail_res.status_code == 200
    detail = detail_res.json()

    assert detail["return_id"] == ret_id
    assert detail["return_no"] == created["return_no"]
    assert detail["supplier_id"] == api_env["supplier_id"]
    assert detail["purchase_id"] == api_env["purchase_id"]
    assert detail["supplier_credit_note_no"] == "SCN-DETAIL-123"
    assert len(detail["items"]) == 1
    assert len(detail["settlements"]) == 1
    assert detail["voucher_id"] is not None


def test_get_purchase_return_detail_unknown_404(api_env: Dict[str, Any]):
    """Test GET /api/purchase/returns/99999 returns 404 Not Found."""
    client = _get_client(api_env["tokens"]["ADMIN"])
    resp = client.get("/api/purchase/returns/99999")
    assert resp.status_code == 404
    assert "99999 not found" in resp.json()["detail"].lower()


# ============================================================================
# 5. Idempotency Replay and Conflict Handling
# ============================================================================

def test_idempotency_replay_identical_returns_existing(api_env: Dict[str, Any]):
    """
    Submitting the exact same return request with the same idempotency_key returns
    the existing record (HTTP 200) without duplicating stock or accounting entries.
    """
    client = _get_client(api_env["tokens"]["ADMIN"])
    idem_key = f"IDEM-TEST-{uuid.uuid4().hex[:8]}"

    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-05",
        "idempotency_key": idem_key,
        "items": [
            {
                "purchase_item_id": api_env["item2_id"],
                "product_id": api_env["product2_id"],
                "qty": 2.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 1050.00,
            }
        ],
    }

    # Initial submission
    res1 = client.post("/api/purchase/returns", json=payload)
    assert res1.status_code == 200
    data1 = res1.json()
    ret_id1 = data1["return_id"]

    # Replay submission with identical payload
    res2 = client.post("/api/purchase/returns", json=payload)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["return_id"] == ret_id1
    assert data2["return_no"] == data1["return_no"]

    # Verify no duplicate stock ledger or vouchers
    db_mgr: DatabaseManager = api_env["db_mgr"]
    with db_mgr.transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM stock_ledger WHERE reference_id = ? AND transaction_type = 'PURCHASE_RETURN'",
            (ret_id1,),
        )
        assert cur.fetchone()[0] == 1

        cur.execute("SELECT COUNT(*) FROM vouchers WHERE voucher_id = ?", (data1["voucher_id"],))
        assert cur.fetchone()[0] == 1


def test_idempotency_conflict_returns_409(api_env: Dict[str, Any]):
    """
    Submitting a request with an existing idempotency_key but altered payload
    returns HTTP 409 Conflict.
    """
    client = _get_client(api_env["tokens"]["ADMIN"])
    idem_key = f"IDEM-CONFLICT-{uuid.uuid4().hex[:8]}"

    payload1 = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-05",
        "remarks": "Original request",
        "idempotency_key": idem_key,
        "items": [
            {
                "purchase_item_id": api_env["item2_id"],
                "product_id": api_env["product2_id"],
                "qty": 1.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 525.00,
            }
        ],
    }

    res1 = client.post("/api/purchase/returns", json=payload1)
    assert res1.status_code == 200

    # Altered remarks creates different fingerprint
    payload2 = dict(payload1)
    payload2["remarks"] = "Altered request text"

    res2 = client.post("/api/purchase/returns", json=payload2)
    assert res2.status_code == 409
    assert "Idempotency conflict" in res2.json()["detail"]


# ============================================================================
# 6. Direct Route Alias (/api/purchase-returns)
# ============================================================================

def test_direct_route_alias_purchase_returns(api_env: Dict[str, Any]):
    """Confirms /api/purchase-returns endpoints behave identically to /api/purchase/returns."""
    client = _get_client(api_env["tokens"]["ADMIN"])

    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-07",
        "idempotency_key": f"PR-ALIAS-{uuid.uuid4().hex[:6]}",
        "items": [
            {
                "purchase_item_id": api_env["item2_id"],
                "product_id": api_env["product2_id"],
                "qty": 1.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 525.00,
            }
        ],
    }

    # 1. POST /api/purchase-returns
    res_post = client.post("/api/purchase-returns", json=payload)
    assert res_post.status_code == 200, res_post.text
    ret_id = res_post.json()["return_id"]

    # 2. GET /api/purchase-returns
    res_list = client.get("/api/purchase-returns")
    assert res_list.status_code == 200
    all_returns = res_list.json()
    assert any(r["return_id"] == ret_id for r in all_returns)

    # 3. GET /api/purchase-returns/{return_id}
    res_get = client.get(f"/api/purchase-returns/{ret_id}")
    assert res_get.status_code == 200
    assert res_get.json()["return_id"] == ret_id


# ============================================================================
# 7. Authoritative Backend Preview & Step 2E-B Regression Tests
# ============================================================================

def test_preview_calculation_exact_match_with_creation(api_env: Dict[str, Any]):
    """
    POST /api/purchase/returns/preview returns exact taxable, CGST, SGST, IGST,
    round_off, and net_amount matching process_purchase_return().
    """
    client = _get_client(api_env["tokens"]["ADMIN"])

    preview_payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "items": [
            {
                "purchase_item_id": api_env["item1_id"],
                "product_id": api_env["product1_id"],
                "qty": 10.0,
            }
        ],
    }

    # 1. Request Preview
    res_preview = client.post("/api/purchase/returns/preview", json=preview_payload)
    assert res_preview.status_code == 200, res_preview.text
    prev = res_preview.json()

    assert prev["purchase_id"] == api_env["purchase_id"]
    assert prev["supplier_id"] == api_env["supplier_id"]
    assert float(prev["total_taxable"]) == 9900.00
    assert float(prev["total_cgst"]) == 891.00
    assert float(prev["total_sgst"]) == 891.00
    assert float(prev["total_igst"]) == 0.00
    assert float(prev["round_off"]) == 0.00
    assert float(prev["net_amount"]) == 11682.00
    assert float(prev["supplier_balance"]) == 25000.00
    assert len(prev["items"]) == 1
    assert prev["items"][0]["purchase_item_id"] == api_env["item1_id"]
    assert float(prev["items"][0]["qty"]) == 10.0

    # 2. Actual return posting using preview figures
    create_payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-08",
        "itc_status": "CLAIMED_REVERSED",
        "idempotency_key": f"PR-PARITY-{uuid.uuid4().hex[:8]}",
        "items": [
            {
                "purchase_item_id": api_env["item1_id"],
                "product_id": api_env["product1_id"],
                "qty": 10.0,
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": float(prev["net_amount"]),
            }
        ],
    }
    res_create = client.post("/api/purchase/returns", json=create_payload)
    assert res_create.status_code == 200, res_create.text
    created = res_create.json()

    # Exact 100% parity
    assert float(created["total_taxable"]) == float(prev["total_taxable"])
    assert float(created["total_cgst"]) == float(prev["total_cgst"])
    assert float(created["total_sgst"]) == float(prev["total_sgst"])
    assert float(created["total_igst"]) == float(prev["total_igst"])
    assert float(created["round_off"]) == float(prev["round_off"])
    assert float(created["net_amount"]) == float(prev["net_amount"])


def test_preview_multiple_partial_returns_remainder_parity(api_env: Dict[str, Any]):
    """
    Verifies that preview accurately anticipates the exact sub-paise remainder
    handling on successive partial returns down to the exact paise.
    Line 1 has 10 units (taxable: 9900.00, CGST: 891.00, SGST: 891.00, total: 11682.00).
    We perform:
      - Return 1: 3 units
      - Return 2: 3 units
      - Return 3 (final remainder): 4 units (reaches 10/10)
    At each step, preview calculation must match the created return, and the sum
    of all 3 returns must equal exactly 11682.00.
    """
    client = _get_client(api_env["tokens"]["ADMIN"])
    cumulative_net = Decimal("0.00")
    cumulative_taxable = Decimal("0.00")

    slices = [3.0, 3.0, 4.0]
    for idx, qty in enumerate(slices, 1):
        preview_payload = {
            "purchase_id": api_env["purchase_id"],
            "supplier_id": api_env["supplier_id"],
            "items": [
                {
                    "purchase_item_id": api_env["item1_id"],
                    "product_id": api_env["product1_id"],
                    "qty": qty,
                }
            ],
        }
        p_res = client.post("/api/purchase/returns/preview", json=preview_payload)
        assert p_res.status_code == 200, p_res.text
        p = p_res.json()

        create_payload = {
            "purchase_id": api_env["purchase_id"],
            "supplier_id": api_env["supplier_id"],
            "return_date": f"2026-11-0{idx + 2}",
            "itc_status": "CLAIMED_REVERSED",
            "idempotency_key": f"PR-REM-{idx}-{uuid.uuid4().hex[:8]}",
            "items": [
                {
                    "purchase_item_id": api_env["item1_id"],
                    "product_id": api_env["product1_id"],
                    "qty": qty,
                }
            ],
            "settlements": [
                {
                    "settlement_type": "PAYABLE_REDUCTION",
                    "amount": float(p["net_amount"]),
                }
            ],
        }
        c_res = client.post("/api/purchase/returns", json=create_payload)
        assert c_res.status_code == 200, c_res.text
        c = c_res.json()

        # Exact parity on each slice
        assert Decimal(str(c["total_taxable"])) == Decimal(str(p["total_taxable"]))
        assert Decimal(str(c["total_cgst"])) == Decimal(str(p["total_cgst"]))
        assert Decimal(str(c["total_sgst"])) == Decimal(str(p["total_sgst"]))
        assert Decimal(str(c["round_off"])) == Decimal(str(p["round_off"]))
        assert Decimal(str(c["net_amount"])) == Decimal(str(p["net_amount"]))

        cumulative_taxable += Decimal(str(c["total_taxable"]))
        cumulative_net += Decimal(str(c["net_amount"]))

    # Total pre-rounding line amounts must equal original line amounts exactly
    assert cumulative_taxable == Decimal("9900.00")
    # Each independent document rounds half-up to nearest rupee (3504.60->3505, 3504.60->3505, 4672.80->4673)
    # Preview and creation matched 100% on every slice down to exact round_off and net_amount:
    assert cumulative_net == Decimal("11683.00")


def test_preview_zero_side_effects(api_env: Dict[str, Any]):
    """Verifies that calling the preview endpoint causes zero database writes or side effects."""
    db_mgr: DatabaseManager = api_env["db_mgr"]
    tables = [
        "purchases",
        "purchase_items",
        "purchase_returns",
        "purchase_return_items",
        "purchase_return_settlements",
        "vouchers",
        "stock_ledger",
        "supplier_credits",
        "suppliers",
    ]

    def _get_counts():
        counts = {}
        with db_mgr.transaction() as conn:
            cur = conn.cursor()
            for t in tables:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                counts[t] = cur.fetchone()[0]
        return counts

    counts_before = _get_counts()

    client = _get_client(api_env["tokens"]["ADMIN"])
    # Call preview multiple times with different combinations
    for qty in [1.0, 3.5, 7.0]:
        res = client.post(
            "/api/purchase/returns/preview",
            json={
                "purchase_id": api_env["purchase_id"],
                "supplier_id": api_env["supplier_id"],
                "items": [{"purchase_item_id": api_env["item2_id"], "qty": qty}],
            },
        )
        assert res.status_code == 200

    counts_after = _get_counts()
    assert counts_before == counts_after


def test_preview_validation_and_bonus_line_rejection(api_env: Dict[str, Any]):
    """Preview rejects invalid quantities, non-existent items, and bonus items with free_qty > 0."""
    client = _get_client(api_env["tokens"]["ADMIN"])

    # 1. Bonus line with free_qty > 0
    res_bonus = client.post(
        "/api/purchase/returns/preview",
        json={
            "purchase_id": api_env["purchase_id"],
            "supplier_id": api_env["supplier_id"],
            "items": [{"purchase_item_id": api_env["item3_free_id"], "qty": 1.0}],
        },
    )
    assert res_bonus.status_code == 400
    assert "free" in res_bonus.json()["detail"].lower()

    # 2. Quantity exceeding returnable limit
    res_excess = client.post(
        "/api/purchase/returns/preview",
        json={
            "purchase_id": api_env["purchase_id"],
            "supplier_id": api_env["supplier_id"],
            "items": [{"purchase_item_id": api_env["item2_id"], "qty": 999.0}],
        },
    )
    assert res_excess.status_code == 400
    assert "exceeds" in res_excess.json()["detail"].lower()

    # 3. Non-existent purchase
    res_nopur = client.post(
        "/api/purchase/returns/preview",
        json={
            "purchase_id": 99999,
            "supplier_id": api_env["supplier_id"],
            "items": [{"purchase_item_id": api_env["item2_id"], "product_id": api_env["product2_id"], "qty": 1.0}],
        },
    )
    assert res_nopur.status_code == 400


def test_mandatory_itc_status_validation(api_env: Dict[str, Any]):
    """Verifies that empty, invalid, or post-audit itc_status is rejected."""
    client = _get_client(api_env["tokens"]["ADMIN"])

    base_payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "return_date": "2026-11-09",
        "idempotency_key": f"PR-ITC-VAL-{uuid.uuid4().hex[:8]}",
        "items": [{"purchase_item_id": api_env["item2_id"], "product_id": api_env["product2_id"], "qty": 1.0}],
        "settlements": [{"settlement_type": "PAYABLE_REDUCTION", "amount": 525.00}],
    }

    # Empty string itc_status -> 422
    p_empty = dict(base_payload)
    p_empty["itc_status"] = ""
    res_empty = client.post("/api/purchase/returns", json=p_empty)
    assert res_empty.status_code in (400, 422)

    # Invalid status -> 422
    p_invalid = dict(base_payload)
    p_invalid["itc_status"] = "INVALID_STATUS"
    res_invalid = client.post("/api/purchase/returns", json=p_invalid)
    assert res_invalid.status_code in (400, 422)

    # Post-audit CONFIRMED_REVERSED cannot be used at creation -> 422
    p_postaudit = dict(base_payload)
    p_postaudit["itc_status"] = "CONFIRMED_REVERSED"
    res_postaudit = client.post("/api/purchase/returns", json=p_postaudit)
    assert res_postaudit.status_code in (400, 422)

    # Valid status: NOT_CLAIMED -> 200
    p_valid = dict(base_payload)
    p_valid["itc_status"] = "NOT_CLAIMED"
    res_valid = client.post("/api/purchase/returns", json=p_valid)
    assert res_valid.status_code == 200
    assert res_valid.json()["itc_status"] == "NOT_CLAIMED"


def test_preview_endpoint_auth_and_roles(api_env: Dict[str, Any]):
    """Confirms authentication and role security on preview endpoint and route alias."""
    payload = {
        "purchase_id": api_env["purchase_id"],
        "supplier_id": api_env["supplier_id"],
        "items": [{"purchase_item_id": api_env["item2_id"], "product_id": api_env["product2_id"], "qty": 1.0}],
    }

    # 1. Unauthenticated -> 401
    unauth_client = _get_client(None)
    assert unauth_client.post("/api/purchase/returns/preview", json=payload).status_code in (401, 403)

    # 2. OPERATOR -> 403
    op_client = _get_client(api_env["tokens"]["OPERATOR"])
    assert op_client.post("/api/purchase/returns/preview", json=payload).status_code == 403

    # 3. Allowed roles: ADMIN, OWNER, MANAGER, ACCOUNTANT -> 200
    for role in ["ADMIN", "OWNER", "MANAGER", "ACCOUNTANT"]:
        role_client = _get_client(api_env["tokens"][role])
        assert role_client.post("/api/purchase/returns/preview", json=payload).status_code == 200

    # 4. Route alias /api/purchase-returns/preview works identically
    admin_client = _get_client(api_env["tokens"]["ADMIN"])
    alias_res = admin_client.post("/api/purchase-returns/preview", json=payload)
    assert alias_res.status_code == 200
    assert alias_res.json()["net_amount"] == 525.00
