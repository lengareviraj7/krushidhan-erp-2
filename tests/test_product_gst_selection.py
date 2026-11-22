"""
Automated Test Suite for Product GST Slab / Tax Group Selection.
Verifies Stage 1B commercial release requirements:
- UI contract: index.html has required #m-prod-tax selector with placeholder.
- JS contract: app.js populates tax groups dynamically and rejects missing selection.
- Backend API contract: POST /api/masters/products validates tax_group_id.
- Exact Master Data verification: 5 tax groups (0%, 5%, 12%, 18%, 28%).
- Invoice Tax Calculation preservation for intra-state (CGST/SGST) and inter-state (IGST).
"""
import re
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, reset_db_manager
from src.repositories.master_data_repository import MasterDataRepository
from src.services.auth_service import AuthService
from src.services.sales_service import SalesService


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Provides an isolated SQLite database and authenticated test client."""
    test_db_path = tmp_path / "test_gst_selection.db"
    monkeypatch.setenv("DB_PATH", str(test_db_path))
    reset_db_manager()
    db_manager = DatabaseManager(db_path=test_db_path)
    db_manager.initialize_database(include_seed=True)

    token = AuthService.create_access_token(
        user_id=1,
        username="admin",
        full_name="आकाश लेंगारे (Admin)",
        role="ADMIN",
    )

    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        yield {
            "client": client,
            "db_manager": db_manager,
            "db_path": test_db_path,
        }
    reset_db_manager()


# ---------------------------------------------------------------------------
# Test 1: Frontend Template & JS Contract Verification
# ---------------------------------------------------------------------------
def test_ui_template_and_js_contract():
    """Verify that index.html and app.js implement the required GST selector."""
    repo_root = Path(__file__).resolve().parent.parent

    # 1. Inspect index.html
    html_file = repo_root / "src" / "web" / "templates" / "index.html"
    assert html_file.exists(), "index.html must exist"
    html_content = html_file.read_text(encoding="utf-8")

    # Check for #m-prod-tax select element
    assert 'id="m-prod-tax"' in html_content, "index.html must contain select with id='m-prod-tax'"
    assert "GST Rate / Tax Group" in html_content, "index.html must contain GST Rate / Tax Group label"
    assert "Select GST Rate" in html_content, "index.html must have placeholder option"

    # 2. Inspect app.js
    js_file = repo_root / "src" / "web" / "static" / "js" / "app.js"
    assert js_file.exists(), "app.js must exist"
    js_content = js_file.read_text(encoding="utf-8")

    # Verify dynamic population in loadInitialLookups
    assert 'document.getElementById("m-prod-tax")' in js_content, "app.js must reference m-prod-tax"
    assert "allLookups.tax_groups" in js_content, "app.js must use allLookups.tax_groups to populate options"
    assert "Failed to load tax rates" in js_content, "app.js must handle tax group loading failure"

    # Verify validation in createMasterProduct
    assert "Please select a GST Rate / Tax Group!" in js_content, "app.js must show toast warning on empty GST"
    assert "tax_group_id: taxId" in js_content, "app.js must pass tax_group_id in POST body"


# ---------------------------------------------------------------------------
# Test 2: Master Tax Groups Lookup API Verification
# ---------------------------------------------------------------------------
def test_get_all_tax_groups_lookup(test_env):
    """Verify GET /api/masters/all returns all 5 statutory GST tax groups."""
    client = test_env["client"]
    res = client.get("/api/masters/all")
    assert res.status_code == 200
    data = res.json()

    assert "tax_groups" in data
    tax_groups = data["tax_groups"]
    assert len(tax_groups) == 5, f"Expected 5 tax groups, got {len(tax_groups)}"

    # Map tax groups by ID
    tg_by_id = {tg["tax_group_id"]: tg for tg in tax_groups}

    # Verify 1: Exempted (0%)
    tg1 = tg_by_id[1]
    assert "Exempted (0%)" in tg1["tax_group_name"]
    assert tg1["cgst_rate"] == 0.0
    assert tg1["sgst_rate"] == 0.0
    assert tg1["igst_rate"] == 0.0
    assert tg1["is_active"] == 1

    # Verify 2: GST 5% (Fertilizers & Bulk Inputs)
    tg2 = tg_by_id[2]
    assert "5%" in tg2["tax_group_name"]
    assert tg2["cgst_rate"] == 2.5
    assert tg2["sgst_rate"] == 2.5
    assert tg2["igst_rate"] == 5.0
    assert tg2["is_active"] == 1

    # Verify 3: GST 12% (Agricultural Equipment & Implements)
    tg3 = tg_by_id[3]
    assert "12%" in tg3["tax_group_name"]
    assert tg3["cgst_rate"] == 6.0
    assert tg3["sgst_rate"] == 6.0
    assert tg3["igst_rate"] == 12.0
    assert tg3["is_active"] == 1

    # Verify 4: GST 18% (Pesticides, Insecticides & Sprayers)
    tg4 = tg_by_id[4]
    assert "18%" in tg4["tax_group_name"]
    assert tg4["cgst_rate"] == 9.0
    assert tg4["sgst_rate"] == 9.0
    assert tg4["igst_rate"] == 18.0
    assert tg4["is_active"] == 1

    # Verify 5: GST 28% (Luxury & Specialized Machinery)
    tg5 = tg_by_id[5]
    assert "28%" in tg5["tax_group_name"]
    assert tg5["cgst_rate"] == 14.0
    assert tg5["sgst_rate"] == 14.0
    assert tg5["igst_rate"] == 28.0
    assert tg5["is_active"] == 1


# ---------------------------------------------------------------------------
# Test 3: Backend API Validation for Product Creation
# ---------------------------------------------------------------------------
def test_create_product_tax_group_validation(test_env):
    """Verify backend enforces valid tax_group_id when creating products."""
    client = test_env["client"]

    base_payload = {
        "product_name": "Validation Test Product",
        "category_id": 1,
        "manufacturer_id": 1,
        "hsn_code": "38089910",
        "unit_id": 1,
        "default_purchase_rate": 400.0,
        "default_sale_rate": 500.0,
        "default_mrp": 550.0,
    }

    # 1. Missing tax_group_id -> HTTP 400
    res_missing = client.post("/api/masters/products", json=base_payload)
    assert res_missing.status_code == 400
    assert "tax_group_id" in res_missing.json()["detail"].lower()

    # 2. None / null tax_group_id -> HTTP 400
    p_null = dict(base_payload)
    p_null["tax_group_id"] = None
    res_null = client.post("/api/masters/products", json=p_null)
    assert res_null.status_code == 400

    # 3. Invalid zero or negative tax_group_id -> HTTP 400
    p_zero = dict(base_payload)
    p_zero["tax_group_id"] = 0
    res_zero = client.post("/api/masters/products", json=p_zero)
    assert res_zero.status_code == 400

    # 4. Non-existent tax_group_id -> HTTP 400
    p_nonexistent = dict(base_payload)
    p_nonexistent["tax_group_id"] = 9999
    res_nonexistent = client.post("/api/masters/products", json=p_nonexistent)
    assert res_nonexistent.status_code == 400
    assert "exist" in res_nonexistent.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test 4: Creation of Products with Each Distinct GST Slab
# ---------------------------------------------------------------------------
def test_create_products_with_each_gst_slab(test_env):
    """Verify creating products with 0%, 5%, 12%, 18%, and 28% GST."""
    client = test_env["client"]

    slabs = [
        (1, "Organic Vermicompost 50kg", "31010099", 0.0, 0.0, 0.0),
        (2, "Neem Coated Urea 45kg", "31021000", 2.5, 2.5, 5.0),
        (3, "Manual Knapsack Sprayer 16L", "84248100", 6.0, 6.0, 12.0),
        (4, "Coragen Chlorantraniliprole 60ml", "38089910", 9.0, 9.0, 18.0),
        (5, "Automated Heavy Harvester Blade", "84339000", 14.0, 14.0, 28.0),
    ]

    created_ids = []
    for tax_id, name, hsn, exp_cgst, exp_sgst, exp_igst in slabs:
        payload = {
            "product_name": name,
            "category_id": 1,
            "manufacturer_id": 1,
            "hsn_code": hsn,
            "unit_id": 1,
            "tax_group_id": tax_id,
            "default_purchase_rate": 100.0 * tax_id,
            "default_sale_rate": 120.0 * tax_id,
            "default_mrp": 130.0 * tax_id,
        }
        res = client.post("/api/masters/products", json=payload)
        assert res.status_code == 200, f"Failed for tax_group_id {tax_id}: {res.text}"
        data = res.json()
        assert data["success"] is True
        created_ids.append((data["product_id"], tax_id, exp_cgst, exp_sgst, exp_igst))

    # Verify search_products returns the exact tax group and rates
    res_list = client.get("/api/masters/products")
    assert res_list.status_code == 200
    all_prods = {p["product_id"]: p for p in res_list.json()}

    for pid, expected_tax_id, exp_cgst, exp_sgst, exp_igst in created_ids:
        assert pid in all_prods
        p_data = all_prods[pid]
        assert p_data["tax_group_id"] == expected_tax_id
        assert p_data["cgst_rate"] == exp_cgst
        assert p_data["sgst_rate"] == exp_sgst
        assert p_data["igst_rate"] == exp_igst
        assert p_data["tax_group_name"] is not None


# ---------------------------------------------------------------------------
# Test 5: Invoice Tax Calculation Preservation
# ---------------------------------------------------------------------------
def test_invoice_tax_calculation_preservation(test_env):
    """Verify sales tax calculations are accurately preserved for intra-state and inter-state."""
    sales_service = SalesService(test_env["db_manager"])

    # Case A: Intrastate 18% GST (Pesticide) - Inclusive Rate ₹708, Qty 1
    # Taxable = 708 / 1.18 = 600.00, CGST 9% = 54.00, SGST 9% = 54.00, Total = 708.00
    calc_18 = sales_service.calculate_item_tax(
        qty=1.0,
        rate=708.0,
        discount_percent=0.0,
        cgst_rate=9.0,
        sgst_rate=9.0,
        igst_rate=0.0,
        is_rate_tax_inclusive=True,
    )
    assert calc_18["taxable_amount"] == 600.0
    assert calc_18["cgst_amount"] == 54.0
    assert calc_18["sgst_amount"] == 54.0
    assert calc_18["igst_amount"] == 0.0
    assert calc_18["total_amount"] == 708.0

    # Case B: Interstate 18% GST (Pesticide) - Inclusive Rate ₹708, Qty 1 (cgst=0, sgst=0, igst=18)
    # Taxable = 600.00, IGST 18% = 108.00, Total = 708.00
    calc_18_igst = sales_service.calculate_item_tax(
        qty=1.0,
        rate=708.0,
        discount_percent=0.0,
        cgst_rate=0.0,
        sgst_rate=0.0,
        igst_rate=18.0,
        is_rate_tax_inclusive=True,
    )
    assert calc_18_igst["taxable_amount"] == 600.0
    assert calc_18_igst["cgst_amount"] == 0.0
    assert calc_18_igst["sgst_amount"] == 0.0
    assert calc_18_igst["igst_amount"] == 108.0
    assert calc_18_igst["total_amount"] == 708.0

    # Case C: Intrastate 5% GST (Fertilizer) - Inclusive Rate ₹262.50, Qty 10 = ₹2,625
    # Taxable = 2625 / 1.05 = 2500.00, CGST 2.5% = 62.50, SGST 2.5% = 62.50, Total = 2625.00
    calc_5 = sales_service.calculate_item_tax(
        qty=10.0,
        rate=262.50,
        discount_percent=0.0,
        cgst_rate=2.5,
        sgst_rate=2.5,
        igst_rate=0.0,
        is_rate_tax_inclusive=True,
    )
    assert calc_5["taxable_amount"] == 2500.0
    assert calc_5["cgst_amount"] == 62.50
    assert calc_5["sgst_amount"] == 62.50
    assert calc_5["igst_amount"] == 0.0
    assert calc_5["total_amount"] == 2625.00

    # Case D: Exempted 0% GST (Seeds) - Rate ₹500, Qty 2 = ₹1,000
    # Taxable = 1000.00, Tax = 0.00, Total = 1000.00
    calc_0 = sales_service.calculate_item_tax(
        qty=2.0,
        rate=500.0,
        discount_percent=0.0,
        cgst_rate=0.0,
        sgst_rate=0.0,
        igst_rate=0.0,
        is_rate_tax_inclusive=True,
    )
    assert calc_0["taxable_amount"] == 1000.0
    assert calc_0["cgst_amount"] == 0.0
    assert calc_0["sgst_amount"] == 0.0
    assert calc_0["igst_amount"] == 0.0
    assert calc_0["total_amount"] == 1000.0


# ---------------------------------------------------------------------------
# Test 6: Historical Products & Invoices Unchanged
# ---------------------------------------------------------------------------
def test_historical_products_and_invoices_unchanged(test_env):
    """Verify that existing historical products and historical invoice data remain untouched when new products are created."""
    client = test_env["client"]
    db = test_env["db_manager"]

    # 1. Setup a pre-existing customer and historical product with 5% GST (tax_group_id = 2)
    with db.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO customers (customer_name, mobile, village) VALUES (?, ?, ?);
        """, ("Historical Farmer", "9876543210", "Old Village"))
        hist_cust_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO products (
                product_name, category_id, manufacturer_id, hsn_code, unit_id,
                tax_group_id, default_purchase_rate, default_sale_rate, default_mrp, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, ("Historical NPK 10:26:26 50kg", 1, 1, "31052000", 1, 2, 1400.0, 1470.0, 1470.0, 1))
        hist_prod_id = cursor.lastrowid

        # Insert a pre-existing historical sales invoice
        cursor.execute("""
            INSERT INTO sales (
                invoice_no, customer_id, sale_date, payment_mode,
                total_taxable, total_cgst, total_sgst, total_igst, total_discount,
                round_off, net_amount, paid_amount, due_amount
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, ("INV-HIST-001", hist_cust_id, "2026-01-15 10:30:00", "CASH",
              1400.0, 35.0, 35.0, 0.0, 0.0, 0.0, 1470.0, 1470.0, 0.0))
        hist_sale_id = cursor.lastrowid

        # Insert a pre-existing stock batch for foreign key reference
        cursor.execute("""
            INSERT INTO stock_batches (
                product_id, batch_no, purchase_rate, sale_rate, mrp, current_qty
            ) VALUES (?, ?, ?, ?, ?, ?);
        """, (hist_prod_id, "BATCH-HIST-01", 1400.0, 1470.0, 1470.0, 10.0))
        hist_batch_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO sale_items (
                sale_id, product_id, batch_id, qty, sale_rate, mrp,
                taxable_amount, cgst_rate, cgst_amount, sgst_rate, sgst_amount,
                igst_rate, igst_amount, total_amount
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (hist_sale_id, hist_prod_id, hist_batch_id, 1.0, 1470.0, 1470.0,
              1400.0, 2.5, 35.0, 2.5, 35.0, 0.0, 0.0, 1470.0))

    # Read snapshot of historical records
    hist_prod_before = dict(db.fetch_one("SELECT * FROM products WHERE product_id = ?;", (hist_prod_id,)))
    hist_sale_before = dict(db.fetch_one("SELECT * FROM sales WHERE sale_id = ?;", (hist_sale_id,)))
    hist_item_before = dict(db.fetch_one("SELECT * FROM sale_items WHERE sale_id = ?;", (hist_sale_id,)))

    # 2. Register a new product using the new GST selection workflow (18% GST)
    new_res = client.post("/api/masters/products", json={
        "product_name": "New Pesticide 2026",
        "category_id": 3,
        "manufacturer_id": 1,
        "hsn_code": "38089910",
        "unit_id": 1,
        "tax_group_id": 4,  # 18% GST
        "default_purchase_rate": 500.0,
        "default_sale_rate": 600.0,
        "default_mrp": 650.0,
    })
    assert new_res.status_code == 200

    # 3. Verify historical product and invoice records remain completely identical
    hist_prod_after = dict(db.fetch_one("SELECT * FROM products WHERE product_id = ?;", (hist_prod_id,)))
    hist_sale_after = dict(db.fetch_one("SELECT * FROM sales WHERE sale_id = ?;", (hist_sale_id,)))
    hist_item_after = dict(db.fetch_one("SELECT * FROM sale_items WHERE sale_id = ?;", (hist_sale_id,)))

    assert hist_prod_before == hist_prod_after, "Historical product must remain byte-for-byte unchanged"
    assert hist_sale_before == hist_sale_after, "Historical sale invoice must remain byte-for-byte unchanged"
    assert hist_item_before == hist_item_after, "Historical sale item must remain byte-for-byte unchanged"
    assert hist_prod_after["tax_group_id"] == 2
    assert hist_item_after["cgst_amount"] == 35.0
    assert hist_item_after["sgst_amount"] == 35.0


# ---------------------------------------------------------------------------
# Test 7: Direct DB Verification of tax_group_id Persistence
# ---------------------------------------------------------------------------
def test_direct_db_verification_of_tax_group_id_persistence(test_env):
    """Directly query SQLite to verify products table stores the exact selected tax_group_id."""
    client = test_env["client"]
    db = test_env["db_manager"]

    # Create product with 18% GST (tax_group_id = 4)
    res = client.post("/api/masters/products", json={
        "product_name": "Direct DB Persistence Verify 18%",
        "category_id": 3,
        "manufacturer_id": 1,
        "hsn_code": "38089910",
        "unit_id": 1,
        "tax_group_id": 4,
        "default_purchase_rate": 800.0,
        "default_sale_rate": 950.0,
        "default_mrp": 1000.0,
    })
    assert res.status_code == 200
    new_pid = res.json()["product_id"]

    # Direct SQLite query to check the row
    row = db.fetch_one("SELECT product_id, product_name, tax_group_id FROM products WHERE product_id = ?;", (new_pid,))
    assert row is not None
    assert row["product_id"] == new_pid
    assert row["tax_group_id"] == 4, f"Expected tax_group_id=4, got {row['tax_group_id']}"

    # Also verify join with tax_groups produces 9% CGST and 9% SGST
    joined = db.fetch_one("""
        SELECT p.product_name, tg.tax_group_name, tg.cgst_rate, tg.sgst_rate, tg.igst_rate
        FROM products p
        JOIN tax_groups tg ON p.tax_group_id = tg.tax_group_id
        WHERE p.product_id = ?;
    """, (new_pid,))
    assert joined is not None
    assert joined["cgst_rate"] == 9.0
    assert joined["sgst_rate"] == 9.0
    assert joined["igst_rate"] == 18.0
    assert "18%" in joined["tax_group_name"]

