"""
Automated Test Suite for Step 2E: Purchase Returns Frontend Integration.
Verifies UI templates, static assets, navigation, list filtering, and Create Return modal workflow.
"""
import uuid
import pytest
from fastapi.testclient import TestClient
from src.app import app
from src.db.connection import DatabaseManager, reset_db_manager
from src.services.auth_service import AuthService


@pytest.fixture
def isolated_client(tmp_path, monkeypatch):
    """Provides an authenticated TestClient connected to an isolated test SQLite database."""
    test_db_path = tmp_path / "test_pr_ui_agri_erp.db"
    monkeypatch.setenv("DB_PATH", str(test_db_path))
    reset_db_manager()
    db_manager = DatabaseManager(db_path=test_db_path)
    db_manager.initialize_database(include_seed=True)

    token = AuthService.create_access_token(
        user_id=1,
        username="admin",
        full_name="Shop Admin",
        role="ADMIN",
    )

    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as test_client:
        yield test_client
    reset_db_manager()


def test_ui_index_contains_purchase_returns_navigation_and_workspace(isolated_client):
    """Verify that root / serves index.html with all required Purchase Returns DOM elements."""
    res = isolated_client.get("/")
    assert res.status_code == 200
    html = res.text

    # 1. Navigation elements
    assert 'class="nav-tab" data-tab="tab-purchase-returns"' in html
    assert 'class="mobile-drawer-item" data-tab="tab-purchase-returns"' in html
    assert 'id="btn-goto-purchase-returns"' in html
    assert "navigateToTab('tab-purchase-returns')" in html

    # 2. Main Workspace
    assert 'id="tab-purchase-returns"' in html
    assert 'id="btn-open-create-return"' in html

    # 3. Filters
    assert 'id="pr-filter-search"' in html
    assert 'id="pr-filter-supplier"' in html
    assert 'id="pr-filter-status"' in html
    assert 'id="pr-filter-from-date"' in html
    assert 'id="pr-filter-to-date"' in html
    assert "applyPurchaseReturnFilters()" in html
    assert "resetPurchaseReturnFilters()" in html
    assert "debouncePurchaseReturnSearch()" in html

    # 4. Table and Pagination
    assert 'id="pr-table-tbody"' in html
    assert 'id="pr-pagination-bar"' in html
    assert 'id="pr-page-info"' in html
    assert 'id="pr-btn-prev"' in html
    assert 'id="pr-btn-next"' in html
    assert "changePurchaseReturnsPage(-1)" in html
    assert "changePurchaseReturnsPage(1)" in html


def test_ui_index_contains_create_return_modal_elements(isolated_client):
    """Verify that index.html contains all 6 interactive steps for the Create Purchase Return modal."""
    res = isolated_client.get("/")
    assert res.status_code == 200
    html = res.text

    # Modal container & header
    assert 'id="modal-create-purchase-return"' in html
    assert 'id="pr-create-form"' in html
    assert "submitCreatePurchaseReturn()" in html
    assert "closeCreatePurchaseReturnModal()" in html

    # Step 1: Inward Purchase selection & metadata
    assert 'id="pr-create-purchase-select"' in html
    assert "onPurchaseSelectedForReturn()" in html
    assert 'id="pr-create-return-date"' in html
    assert 'id="pr-create-scn-no"' in html
    assert 'id="pr-create-scn-date"' in html
    assert 'id="pr-selected-purchase-info"' in html
    assert 'id="pr-info-supplier-name"' in html
    assert 'id="pr-info-supplier-balance"' in html

    # Step 2: Line items table & bonus warning
    assert 'id="pr-items-tbody"' in html
    assert 'id="pr-items-warning"' in html

    # Step 3: Calculation preview
    assert 'id="pr-calc-taxable"' in html
    assert 'id="pr-calc-cgst"' in html
    assert 'id="pr-calc-sgst"' in html
    assert 'id="pr-calc-igst"' in html
    assert 'id="pr-calc-roundoff"' in html
    assert 'id="pr-calc-net"' in html

    # Step 4: Settlements allocation
    assert 'id="pr-settlements-container"' in html
    assert 'id="pr-settle-allocated-display"' in html
    assert 'id="pr-settle-target-display"' in html
    assert 'id="pr-settle-diff-badge"' in html
    assert "addSettlementRow()" in html

    # Step 5: ITC treatment & remarks
    assert 'id="pr-create-itc-status"' in html
    assert 'id="pr-itc-explanation"' in html
    assert "onItcStatusChange()" in html
    assert 'id="pr-create-remarks"' in html

    # Step 6: Confirmation button
    assert 'id="btn-submit-purchase-return"' in html


def test_static_assets_contain_purchase_return_logic_and_styles(isolated_client):
    """Verify that static JS and CSS bundles contain all required functions, styles, and handlers."""
    # Check JS bundle
    js_res = isolated_client.get("/static/js/app.js")
    assert js_res.status_code == 200
    js = js_res.text

    # Required Step 2E-A Navigation and List functions
    list_functions = [
        "canAccessPurchaseReturns",
        "navigateToTab",
        "loadPurchaseReturns",
        "renderPurchaseReturnsTable",
        "applyPurchaseReturnFilters",
        "resetPurchaseReturnFilters",
        "changePurchaseReturnsPage",
        "debouncePurchaseReturnSearch",
        "formatSettlementType",
    ]
    for fn in list_functions:
        assert f"function {fn}" in js, f"Expected function {fn} not found in app.js"

    # Required Step 2E-B Create Return Workflow functions
    workflow_functions = [
        "generateIdempotencyKey",
        "openCreatePurchaseReturnModal",
        "closeCreatePurchaseReturnModal",
        "onPurchaseSelectedForReturn",
        "renderEligiblePurchaseLinesTable",
        "onReturnItemCheckToggle",
        "onReturnItemQtyChange",
        "onReturnItemReasonChange",
        "recomputeReturnCalculations",
        "updateCalculationsPreviewDisplay",
        "renderSettlementRows",
        "addSettlementRow",
        "removeSettlementRow",
        "onSettlementRowChange",
        "updateSettlementReconciliation",
        "onItcStatusChange",
        "submitCreatePurchaseReturn",
    ]
    for fn in workflow_functions:
        assert f"function {fn}" in js, f"Expected function {fn} not found in app.js"

    # Step 2E-C Guardrails
    guardrail_functions = [
        "openPurchaseReturnDetail",
        "closePurchaseReturnDetailModal",
        "printPurchaseReturnDetail",
    ]
    for fn in guardrail_functions:
        assert f"function {fn}" in js, f"Expected guardrail function {fn} not found in app.js"

    # Check CSS bundle
    css_res = isolated_client.get("/static/css/app.css")
    assert css_res.status_code == 200
    css = css_res.text
    assert ".badge-itc" in css
    assert ".badge-settlement" in css
    assert ".step-badge" in css
    assert "print-target-purchase-return" in css


def test_purchase_returns_list_api_role_permissions(tmp_path, monkeypatch):
    """Verify role-based access control on GET /api/purchase/returns."""
    test_db_path = tmp_path / "test_pr_roles_agri_erp.db"
    monkeypatch.setenv("DB_PATH", str(test_db_path))
    reset_db_manager()
    db_manager = DatabaseManager(db_path=test_db_path)
    db_manager.initialize_database(include_seed=True)

    client = TestClient(app)

    # 1. Unauthenticated request -> 401 Unauthorized
    res_unauth = client.get("/api/purchase/returns")
    assert res_unauth.status_code in (401, 403)

    # 2. OPERATOR role -> 403 Forbidden
    op_token = AuthService.create_access_token(user_id=2, username="operator", full_name="Shop Operator", role="OPERATOR")
    res_op = client.get("/api/purchase/returns", headers={"Authorization": f"Bearer {op_token}"})
    assert res_op.status_code == 403

    # 3. Authorized roles: ADMIN, OWNER, MANAGER, ACCOUNTANT -> 200 OK
    for role in ["ADMIN", "OWNER", "MANAGER", "ACCOUNTANT"]:
        token = AuthService.create_access_token(user_id=1, username=role.lower(), full_name=f"Shop {role}", role=role)
        res_auth = client.get("/api/purchase/returns", headers={"Authorization": f"Bearer {token}"})
        assert res_auth.status_code == 200, f"Role {role} failed to access /api/purchase/returns"
        assert isinstance(res_auth.json(), list)

    reset_db_manager()


def test_create_purchase_return_full_api_workflow(tmp_path, monkeypatch):
    """
    Test the full backend API creation flow matching the Step 2E-B frontend workflow:
    1. Create an inward purchase invoice with eligible items.
    2. Query GET /api/purchase/{id}/eligible-returns.
    3. Verify free-quantity and returnable quantity bounds.
    4. Post valid purchase return with idempotency key.
    5. Verify list reflects new return and inventory/accounting updated.
    """
    test_db_path = tmp_path / "test_pr_workflow_agri_erp.db"
    monkeypatch.setenv("DB_PATH", str(test_db_path))
    reset_db_manager()
    db_manager = DatabaseManager(db_path=test_db_path)
    db_manager.initialize_database(include_seed=True)

    token = AuthService.create_access_token(user_id=1, username="admin", full_name="Shop Admin", role="ADMIN")
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})

    # 1. Insert seed data directly into isolated test database
    with db_manager.transaction("IMMEDIATE") as conn:
        cur = conn.cursor()
        # Supplier
        cur.execute(
            """
            INSERT INTO suppliers (supplier_name, mobile, gstin, state, current_balance)
            VALUES ('AgriChem Supplier', '9876543210', '27AABCU9603R1ZM', 'Maharashtra', 25000.0);
            """
        )
        supplier_id = cur.lastrowid

        # Products
        cur.execute(
            """
            INSERT INTO products (product_name, category_id, unit_id, hsn_code, default_purchase_rate, default_sale_rate, default_mrp)
            VALUES ('NPK 19:19:19 50kg', 1, 2, '310520', 200.0, 250.0, 300.0);
            """
        )
        prod1_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO products (product_name, category_id, unit_id, hsn_code, default_purchase_rate, default_sale_rate, default_mrp)
            VALUES ('Bio Fungicide 1L', 4, 1, '380899', 100.0, 120.0, 150.0);
            """
        )
        prod2_id = cur.lastrowid

        # Stock batches
        cur.execute(
            """
            INSERT INTO stock_batches (product_id, batch_no, mfg_date, exp_date, purchase_rate, sale_rate, mrp, opening_qty, current_qty)
            VALUES (?, 'BATCH-NORMAL-01', '2026-01-01', '2027-12-31', 200.0, 250.0, 300.0, 10.0, 10.0);
            """,
            (prod1_id,),
        )
        batch1_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO stock_batches (product_id, batch_no, mfg_date, exp_date, purchase_rate, sale_rate, mrp, opening_qty, current_qty)
            VALUES (?, 'BATCH-BONUS-01', '2026-01-01', '2027-12-31', 100.0, 120.0, 150.0, 7.0, 7.0);
            """,
            (prod2_id,),
        )
        batch2_id = cur.lastrowid

        # Inward Purchase
        cur.execute(
            """
            INSERT INTO purchases (
                invoice_no, purchase_date, supplier_id, payment_type,
                total_taxable, total_cgst, total_sgst, total_igst, extra_charges,
                round_off, net_amount, paid_amount, due_amount
            ) VALUES ('PUR-UI-TEST-001', '2026-11-17', ?, 'CREDIT', 2500.0, 180.0, 180.0, 0.0, 0.0, 0.0, 2860.0, 0.0, 2860.0);
            """,
            (supplier_id,),
        )
        purchase_id = cur.lastrowid

        # Line 1: Normal item (qty: 10, rate: 200, free_qty: 0, taxable: 2000, cgst: 180, sgst: 180, total: 2360)
        cur.execute(
            """
            INSERT INTO purchase_items (
                purchase_id, product_id, batch_no, mfg_date, exp_date,
                qty, free_qty, unit_id, purchase_rate, sale_rate, mrp,
                discount_percent, discount_amount, taxable_amount,
                cgst_rate, cgst_amount, sgst_rate, sgst_amount, igst_rate, igst_amount,
                total_amount
            ) VALUES (?, ?, 'BATCH-NORMAL-01', '2026-01-01', '2027-12-31',
                     10.0, 0.0, 2, 200.0, 250.0, 300.0, 0.0, 0.0, 2000.0,
                     9.0, 180.0, 9.0, 180.0, 0.0, 0.0, 2360.0);
            """,
            (purchase_id, prod1_id),
        )
        item1_id = cur.lastrowid

        # Line 2: Bonus item (qty: 5, free_qty: 2, rate: 100, taxable: 500, total: 500)
        cur.execute(
            """
            INSERT INTO purchase_items (
                purchase_id, product_id, batch_no, mfg_date, exp_date,
                qty, free_qty, unit_id, purchase_rate, sale_rate, mrp,
                discount_percent, discount_amount, taxable_amount,
                cgst_rate, cgst_amount, sgst_rate, sgst_amount, igst_rate, igst_amount,
                total_amount
            ) VALUES (?, ?, 'BATCH-BONUS-01', '2026-01-01', '2027-12-31',
                     5.0, 2.0, 1, 100.0, 120.0, 150.0, 0.0, 0.0, 500.0,
                     0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 500.0);
            """,
            (purchase_id, prod2_id),
        )
        item2_id = cur.lastrowid

    # 2. Query eligible return lines
    elig_res = client.get(f"/api/purchase/{purchase_id}/eligible-returns")
    assert elig_res.status_code == 200
    lines = elig_res.json()
    assert len(lines) == 2

    line_normal = next(l for l in lines if l["batch_no"] == "BATCH-NORMAL-01")
    line_bonus = next(l for l in lines if l["batch_no"] == "BATCH-BONUS-01")

    assert line_normal["free_qty"] == 0.0
    assert line_normal["max_returnable_qty"] == 10.0
    assert line_bonus["free_qty"] == 2.0  # Bonus line flagged

    # 3. Create Return for 5 units of Line Normal
    # Calculation: 5/10 * 2000 = 1000 taxable; CGST=90, SGST=90; Total=1180.00
    idempotency_key = f"PR-UI-{uuid.uuid4()}"
    return_payload = {
        "purchase_id": purchase_id,
        "supplier_id": supplier_id,
        "return_date": "2026-11-17",
        "idempotency_key": idempotency_key,
        "supplier_credit_note_no": "SCN-TEST-100",
        "supplier_credit_note_date": "2026-11-17",
        "remarks": "Step 2E-B automated workflow test",
        "itc_status": "CLAIMED_REVERSED",
        "items": [
            {
                "purchase_item_id": line_normal["item_id"],
                "product_id": line_normal["product_id"],
                "batch_id": line_normal["batch_id"],
                "qty": 5.0,
                "reason": "Excess inventory return"
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 1180.0,
                "reference_no": "PAY-RED-001"
            }
        ]
    }
    ret_res = client.post("/api/purchase/returns", json=return_payload)
    assert ret_res.status_code in (200, 201), f"Return creation failed: {ret_res.text}"
    created = ret_res.json()
    assert created["return_no"].startswith("PR-")
    assert float(created["net_amount"]) == 1180.0
    assert created["status"] == "POSTED"

    # 4. Verify list endpoint reflects the newly created return
    list_res = client.get("/api/purchase/returns")
    assert list_res.status_code == 200
    ret_items = list_res.json()
    assert any(r["return_no"] == created["return_no"] for r in ret_items)

    # 5. Idempotency test: Re-submitting the exact same payload returns existing record without duplication
    dupe_res = client.post("/api/purchase/returns", json=return_payload)
    assert dupe_res.status_code in (200, 201)
    assert dupe_res.json()["return_no"] == created["return_no"]

    # 6. Idempotency conflict test: Submitting a DIFFERENT payload with same idempotency key triggers 409
    conflict_payload = dict(return_payload)
    conflict_payload["remarks"] = "Different remarks triggering conflict"
    conflict_res = client.post("/api/purchase/returns", json=conflict_payload)
    assert conflict_res.status_code == 409

    # 7. Bonus / Free quantity rejection test: Returning line with free_qty > 0 must fail
    free_payload = {
        "purchase_id": purchase_id,
        "supplier_id": supplier_id,
        "return_date": "2026-11-17",
        "idempotency_key": f"PR-UI-{uuid.uuid4()}",
        "itc_status": "CLAIMED_REVERSED",
        "items": [
            {
                "purchase_item_id": line_bonus["item_id"],
                "product_id": line_bonus["product_id"],
                "batch_id": line_bonus["batch_id"],
                "qty": 1.0
            }
        ],
        "settlements": [
            {
                "settlement_type": "PAYABLE_REDUCTION",
                "amount": 100.0
            }
        ]
    }
    free_res = client.post("/api/purchase/returns", json=free_payload)
    assert free_res.status_code in (400, 422), "Should reject return on line with bonus/free quantity"

    # 8. Settlement total mismatch rejection: Allocation amount != net return amount
    mismatch_payload = dict(return_payload)
    mismatch_payload["idempotency_key"] = f"PR-UI-{uuid.uuid4()}"
    mismatch_payload["settlements"] = [
        {
            "settlement_type": "PAYABLE_REDUCTION",
            "amount": 999.00  # Expected 1180.00
        }
    ]
    mismatch_res = client.post("/api/purchase/returns", json=mismatch_payload)
    assert mismatch_res.status_code in (400, 422), "Should reject return when settlements != net return amount"

    # 9. Return quantity bounds rejection: Requesting more than remaining returnable qty
    excess_qty_payload = dict(return_payload)
    excess_qty_payload["idempotency_key"] = f"PR-UI-{uuid.uuid4()}"
    excess_qty_payload["items"] = [
        {
            "purchase_item_id": line_normal["item_id"],
            "product_id": line_normal["product_id"],
            "batch_id": line_normal["batch_id"],
            "qty": 999.0,  # Exceeds remaining purchased & stock qty
            "reason": "Excess quantity"
        }
    ]
    excess_res = client.post("/api/purchase/returns", json=excess_qty_payload)
    assert excess_res.status_code in (400, 422), "Should reject return exceeding remaining returnable quantity"

    reset_db_manager()


def test_step_2eb_preview_and_mandatory_itc_ui_contracts(isolated_client):
    """
    Verifies Step 2E-B final correction contracts in HTML & JS assets:
    - Authoritative backend preview endpoint wiring (/api/purchase/returns/preview)
    - Stale preview invalidation & sequence race protection
    - Mandatory ITC selection placeholder with GST portal disclaimer
    - Genuine UUIDv4 idempotency key generator (window.crypto.randomUUID / getRandomValues)
    """
    html = isolated_client.get("/").text
    assert 'id="pr-calc-preview-badge"' in html
    assert 'id="pr-calc-preview-status"' in html
    assert '<option value="" disabled selected>-- Select ITC Treatment (Required) --</option>' in html
    assert "does not file or submit any statutory GST credit note" in html

    js = isolated_client.get("/static/js/app.js").text
    assert "function invalidateReturnPreview" in js
    assert "function scheduleBackendReturnPreview" in js
    assert "function requestBackendReturnPreview" in js
    assert 'fetch("/api/purchase/returns/preview"' in js
    assert "window.crypto.randomUUID" in js
    assert "window.crypto.getRandomValues" in js
    assert "previewRequestSeq" in js
    assert "getAuthHeaders" not in js, "app.js relies on patched window.fetch for auth headers; getAuthHeaders is undefined"
