"""
Phase 2B-5C Verification Suite:
1. Backend machine-readable 409 error_code tests on /api/sales/create and /api/purchase/create
   (keeping detail as a plain string unchanged, and leaving Purchase Return routes untouched).
2. True end-to-end lost-response simulation: real app.js executed in Node against a real
   FastAPI/uvicorn server backed by an isolated temporary SQLite database.
3. Executable Node behavioral test suite (tests/frontend_idempotency.test.mjs) covering
   submit locks, keyboard shortcuts, key lifecycle, conflict handling, offline queue,
   legacy key persistence, review screen actions, purchase retry UX, and auto-create dedup.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, reset_db_manager
from src.models.master_data import Customer, Product, Supplier
from src.models.purchase import Purchase, PurchaseItem
from src.repositories.master_data_repository import MasterDataRepository
from src.services.auth_service import AuthService
from src.services.purchase_service import PurchaseService

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NODE_TEST_FILE = PROJECT_ROOT / "tests" / "frontend_idempotency.test.mjs"


def _setup_isolated_db(tmp_path: Path):
    """Create a fresh isolated SQLite DB with 1 supplier, 1 customer, 1 product, and stock=20."""
    db_path = tmp_path / "phase2b5c_isolated.db"
    bkp_dir = tmp_path / "backups"
    bkp_dir.mkdir(parents=True, exist_ok=True)

    db = DatabaseManager(db_path=db_path, app_mode="local", backup_dir=bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)

    master = MasterDataRepository(db)
    supp_id = master.create_supplier(Supplier(supplier_name="Agro Dist Ltd", mobile="9999900001"))
    cust_id = master.create_customer(Customer(customer_name="Ramesh Patil", mobile="9876543210", village="Indapur"))
    prod_id = master.create_product(Product(product_name="Urea 50kg", default_sale_rate=250.0, default_mrp=260.0))

    pur_svc = PurchaseService(db)
    pur_svc.process_inward_purchase(
        Purchase(
            invoice_no="SEED-PUR-001",
            purchase_date="2026-11-17",
            supplier_id=supp_id,
            payment_type="CREDIT",
            total_taxable=4000.0,
            net_amount=4000.0,
            paid_amount=0.0,
            due_amount=4000.0,
            items=[
                PurchaseItem(
                    product_id=prod_id,
                    batch_no="SEED-B1",
                    exp_date="2027-12-31",
                    qty=20.0,
                    free_qty=0.0,
                    purchase_rate=200.0,
                    sale_rate=250.0,
                    mrp=260.0,
                    taxable_amount=4000.0,
                    total_amount=4000.0,
                )
            ],
        )
    )
    batch = db.fetch_one("SELECT batch_id FROM stock_batches WHERE product_id = ? LIMIT 1;", (prod_id,))
    auth_svc = AuthService(db)
    token = auth_svc.create_access_token(1, "admin", "Administrator", "ADMIN")
    return db, db_path, bkp_dir, supp_id, cust_id, prod_id, int(batch["batch_id"]), token


def test_backend_409_error_codes_sales_and_purchases(tmp_path, monkeypatch):
    """
    Section 3 & Test #12:
    - /api/sales/create returns {"detail": str, "error_code": "IDEMPOTENCY_KEY_CONFLICT"} on key conflict.
    - /api/sales/create returns {"detail": str, "error_code": "DUPLICATE_INVOICE_NO"} on duplicate explicit invoice_no.
    - /api/purchase/create returns {"detail": str, "error_code": "IDEMPOTENCY_KEY_CONFLICT"} on key conflict.
    """
    db, db_path, bkp_dir, supp_id, cust_id, prod_id, batch_id, token = _setup_isolated_db(tmp_path)
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    reset_db_manager()

    headers = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(app) as client:
            # 1. Sales: create with explicit invoice_no and idempotency_key
            sale_body = {
                "invoice_no": "INV-EXPLICIT-01",
                "sale_date": "2026-11-17",
                "customer_id": cust_id,
                "payment_mode": "CASH",
                "total_taxable": 500.0,
                "net_amount": 500.0,
                "paid_amount": 500.0,
                "due_amount": 0.0,
                "idempotency_key": "sale-key-errcode-1",
                "items": [
                    {
                        "product_id": prod_id,
                        "batch_id": batch_id,
                        "qty": 2.0,
                        "sale_rate": 250.0,
                        "mrp": 260.0,
                        "taxable_amount": 500.0,
                        "total_amount": 500.0,
                    }
                ],
            }
            r_ok = client.post("/api/sales/create", json=sale_body, headers=headers)
            assert r_ok.status_code == 200, r_ok.text

            # 2. Sales: same idempotency_key with changed payload -> 409 IDEMPOTENCY_KEY_CONFLICT
            sale_conflict = dict(sale_body)
            sale_conflict["net_amount"] = 750.0
            sale_conflict["paid_amount"] = 750.0
            sale_conflict["items"] = [dict(sale_body["items"][0], qty=3.0, taxable_amount=750.0, total_amount=750.0)]
            r_conflict = client.post("/api/sales/create", json=sale_conflict, headers=headers)
            assert r_conflict.status_code == 409
            c_json = r_conflict.json()
            assert isinstance(c_json["detail"], str) and len(c_json["detail"]) > 0
            assert c_json["error_code"] == "IDEMPOTENCY_KEY_CONFLICT"

            # 3. Sales: different/no idempotency_key with duplicate explicit invoice_no -> 409 DUPLICATE_INVOICE_NO
            sale_dup_inv = dict(sale_body)
            sale_dup_inv["idempotency_key"] = "sale-key-errcode-2"
            r_dup = client.post("/api/sales/create", json=sale_dup_inv, headers=headers)
            assert r_dup.status_code == 409
            d_json = r_dup.json()
            assert isinstance(d_json["detail"], str) and len(d_json["detail"]) > 0
            assert d_json["error_code"] == "DUPLICATE_INVOICE_NO"

            # 4. Purchase: create with idempotency_key, then conflict -> 409 IDEMPOTENCY_KEY_CONFLICT
            pur_body = {
                "invoice_no": "PUR-ERRCODE-01",
                "purchase_date": "2026-11-17",
                "supplier_id": supp_id,
                "payment_type": "CREDIT",
                "total_taxable": 1000.0,
                "net_amount": 1000.0,
                "paid_amount": 0.0,
                "due_amount": 1000.0,
                "idempotency_key": "pur-key-errcode-1",
                "items": [
                    {
                        "product_id": prod_id,
                        "batch_no": "BATCH-EC-1",
                        "exp_date": "2028-01-01",
                        "qty": 5.0,
                        "free_qty": 0.0,
                        "purchase_rate": 200.0,
                        "sale_rate": 250.0,
                        "mrp": 260.0,
                        "taxable_amount": 1000.0,
                        "total_amount": 1000.0,
                    }
                ],
            }
            rp_ok = client.post("/api/purchase/create", json=pur_body, headers=headers)
            assert rp_ok.status_code == 200, rp_ok.text

            pur_conflict = dict(pur_body)
            pur_conflict["net_amount"] = 1200.0
            pur_conflict["due_amount"] = 1200.0
            pur_conflict["items"] = [dict(pur_body["items"][0], qty=6.0, taxable_amount=1200.0, total_amount=1200.0)]
            rp_conflict = client.post("/api/purchase/create", json=pur_conflict, headers=headers)
            assert rp_conflict.status_code == 409
            pc_json = rp_conflict.json()
            assert isinstance(pc_json["detail"], str) and len(pc_json["detail"]) > 0
            assert pc_json["error_code"] == "IDEMPOTENCY_KEY_CONFLICT"
    finally:
        reset_db_manager()


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_e2e_lost_response_queue_and_sync_replay_against_live_server(tmp_path, monkeypatch):
    """
    Test #7 (End-to-End Lost-Response Simulation):
    - Real FastAPI server backed by an isolated temp SQLite database.
    - Real app.js executed in Node calls submitSalesBill(): the HTTP POST reaches FastAPI and
      commits the sale in SQLite, then the client transport drops the response (simulating a
      timeout / lost response).
    - app.js queues the bill in localStorage["krushidhan_pending_bills"] WITH its idempotency_key.
    - app.js then calls syncPendingOfflineBills() against the same live FastAPI server.
    - Server returns HTTP 200 with idempotent_replay: true and the original invoice_no.
    - Queue is emptied, exactly 1 sale row exists in SQLite, and stock decreased exactly once.
    """
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node runtime not available")

    db, db_path, bkp_dir, _supp_id, cust_id, prod_id, batch_id, token = _setup_isolated_db(tmp_path)
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    reset_db_manager()

    port = _find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait until server accepts connections
    deadline = time.time() + 10.0
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "Isolated uvicorn test server did not start in time"

    e2e_script = r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import crypto from "node:crypto";

const baseUrl = process.env.E2E_BASE_URL;
const token = process.env.E2E_TOKEN;
const custId = Number(process.env.E2E_CUST_ID);
const prodId = Number(process.env.E2E_PROD_ID);
const batchId = Number(process.env.E2E_BATCH_ID);
const appJs = fs.readFileSync(process.env.E2E_APP_JS, "utf8");

const store = new Map();
const localStorage = {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: k => store.delete(k),
};
function mkEl(id, val = "") {
    return { id, value: val, textContent: "", innerHTML: "", disabled: false, style: { display: "none" }, dataset: {}, classList: { add() {}, remove() {}, contains: () => false }, focus() {}, addEventListener() {}, removeEventListener() {} };
}
const els = new Map([
    ["pos-inv-no", mkEl("pos-inv-no", "INV-PREVIEW-01")],
    ["pos-date", mkEl("pos-date", "2026-11-17")],
    ["pos-cust-name", mkEl("pos-cust-name", "Ramesh Patil")],
    ["pos-cust-mobile", mkEl("pos-cust-mobile", "9876543210")],
    ["pos-cust-village", mkEl("pos-cust-village", "Indapur")],
    ["pos-cust-crop", mkEl("pos-cust-crop", "")],
    ["pos-pay-mode", mkEl("pos-pay-mode", "CASH")],
    ["pos-paid-amount", mkEl("pos-paid-amount", "500")],
    ["pos-remarks", mkEl("pos-remarks", "E2E lost response")],
    ["pos-cart-tbody", mkEl("pos-cart-tbody")],
    ["pos-cart-mobile-cards", mkEl("pos-cart-mobile-cards")],
    ["pos-tot-taxable", mkEl("pos-tot-taxable")],
    ["pos-tot-gst", mkEl("pos-tot-gst")],
    ["pos-tot-round", mkEl("pos-tot-round")],
    ["pos-grand-total", mkEl("pos-grand-total")],
    ["pos-mobile-grand-total", mkEl("pos-mobile-grand-total")],
    ["btn-pos-save-bill", mkEl("btn-pos-save-bill")],
    ["btn-pos-save-bill-mobile", mkEl("btn-pos-save-bill-mobile")],
    ["pos-offline-pending-banner", mkEl("pos-offline-pending-banner")],
    ["pos-offline-pending-text", mkEl("pos-offline-pending-text")],
    ["pos-idempotency-conflict-banner", mkEl("pos-idempotency-conflict-banner")],
    ["pos-idempotency-conflict-msg", mkEl("pos-idempotency-conflict-msg")],
    ["pending-bills-review-banner", mkEl("pending-bills-review-banner")],
    ["pending-review-banner-count", mkEl("pending-review-banner-count")],
    ["pending-bills-review-banner-text", mkEl("pending-bills-review-banner-text")],
    ["btn-header-pending-review", mkEl("btn-header-pending-review")],
    ["pending-review-header-count", mkEl("pending-review-header-count")],
]);
const document = {
    body: mkEl("body"),
    getElementById: id => els.get(id) || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: tag => mkEl(tag),
    addEventListener() {},
};

let dropFirstCreateResponse = true;
let syncReplayResponse = null;

const wrappedFetch = async (url, opts = {}) => {
    const fullUrl = url.startsWith("http") ? url : `${baseUrl}${url}`;
    const headers = { ...(opts.headers || {}), Authorization: `Bearer ${token}` };
    const res = await globalThis.fetch(fullUrl, { ...opts, headers });
    if (url === "/api/sales/create" && dropFirstCreateResponse) {
        dropFirstCreateResponse = false;
        // Wait for server response body so we know commit succeeded on server, THEN throw to simulate dropped response!
        const committed = await res.json();
        assert.equal(res.status, 200);
        assert.equal(committed.success, true);
        throw new Error("Simulated dropped HTTP response after server commit");
    }
    if (url === "/api/sales/create" && !dropFirstCreateResponse) {
        const cloneText = await res.text();
        const parsed = JSON.parse(cloneText);
        syncReplayResponse = parsed;
        return {
            ok: res.ok,
            status: res.status,
            headers: res.headers,
            json: async () => parsed,
        };
    }
    return res;
};

const win = {
    location: { origin: baseUrl, href: baseUrl, protocol: "http:" },
    crypto: { randomUUID: () => crypto.randomUUID() },
    localStorage,
    sessionStorage: localStorage,
    document,
    fetch: wrappedFetch,
    addEventListener() {},
    removeEventListener() {},
    setTimeout: () => 0,
    clearTimeout() {},
    setInterval: () => 0,
    clearInterval() {},
    Headers: globalThis.Headers,
};
const sandbox = {
    window: win, globalThis: win, self: win, document, localStorage, sessionStorage: localStorage,
    fetch: wrappedFetch, console, crypto: win.crypto, setTimeout: () => 0, clearTimeout() {},
    setInterval: () => 0, clearInterval() {}, Headers: globalThis.Headers, Uint8Array, Promise,
    JSON, Date, Math, Number, String, Boolean, Array, Object, Set, Map, RegExp, Error,
    parseFloat, parseInt, isNaN,
};
vm.createContext(sandbox);
vm.runInContext(appJs, sandbox, { filename: "app.js" });

const toasts = [];
sandbox.showToast = (m, t) => toasts.push({ m, t });
sandbox.window.showToast = sandbox.showToast;

vm.runInContext(`
    currentUser = { user_id: 1, username: "admin", role: "ADMIN" };
    authToken = ${JSON.stringify(token)};
    allCustomers = [{ customer_id: ${custId}, customer_name: "Ramesh Patil", mobile: "9876543210", village: "Indapur" }];
    currentCart = [{
        product_id: ${prodId},
        batch_id: ${batchId},
        product_name: "Urea 50kg",
        batch_no: "SEED-B1",
        exp_date: "2027-12-31",
        qty: 2,
        sale_rate: 250,
        mrp: 260,
        discount_percent: 0,
        discount_amount: 0,
        taxable_amount: 500,
        cgst_rate: 0,
        cgst_amount: 0,
        sgst_rate: 0,
        sgst_amount: 0,
        igst_rate: 0,
        igst_amount: 0,
        total_amount: 500
    }];
    hasValidAuthSession = () => true;
    refreshProductList = async () => {};
    refreshCustomerList = async () => {};
    loadInventorySilently = async () => {};
    notifyOtherTabsDataChanged = () => {};
`, sandbox);

// Step 1: submitSalesBill() commits on server, then client drops response -> bill queued with key
await sandbox.submitSalesBill();
const queuedBeforeSync = JSON.parse(localStorage.getItem("krushidhan_pending_bills") || "[]");
assert.equal(queuedBeforeSync.length, 1, "Bill must be queued after dropped response");
assert.ok(queuedBeforeSync[0].idempotency_key, "Queued bill must carry its idempotency_key");
assert.equal(queuedBeforeSync[0].payload.invoice_no, "", "Queued new bill must keep blank invoice_no");

// Step 2: syncPendingOfflineBills() replays against live FastAPI server -> server returns idempotent_replay: true
await sandbox.syncPendingOfflineBills();
assert.ok(syncReplayResponse, "Sync must have received server response");
assert.equal(syncReplayResponse.success, true);
assert.equal(syncReplayResponse.idempotent_replay, true, "Server must report idempotent_replay=true");
assert.ok(syncReplayResponse.invoice_no.startsWith("INV-"), "Server must return real invoice_no");

const queuedAfterSync = JSON.parse(localStorage.getItem("krushidhan_pending_bills") || "[]");
assert.equal(queuedAfterSync.length, 0, "Pending queue must be empty after replay");
const reviewAfterSync = JSON.parse(localStorage.getItem("krushidhan_pending_bills_review") || "[]");
assert.equal(reviewAfterSync.length, 0, "Review list must stay empty after clean replay");

console.log(JSON.stringify({ ok: true, replay: syncReplayResponse }));
"""

    try:
        env = dict(os.environ)
        env["E2E_BASE_URL"] = f"http://127.0.0.1:{port}"
        env["E2E_TOKEN"] = token
        env["E2E_CUST_ID"] = str(cust_id)
        env["E2E_PROD_ID"] = str(prod_id)
        env["E2E_BATCH_ID"] = str(batch_id)
        env["E2E_APP_JS"] = str(PROJECT_ROOT / "src" / "web" / "static" / "js" / "app.js")

        proc = subprocess.run(
            [node_bin, "--input-type=module", "-e", e2e_script],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"E2E Node script failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload["ok"] is True
        assert payload["replay"]["idempotent_replay"] is True

        # Verify SQLite state directly: exactly 1 sale and stock deducted once (20 - 2 = 18)
        sales_count = db.fetch_one("SELECT COUNT(*) AS c FROM sales;")["c"]
        assert sales_count == 1, f"Expected exactly 1 sale in DB, got {sales_count}"
        batch_qty = db.fetch_one("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch_id,))["current_qty"]
        assert batch_qty == pytest.approx(18.0), f"Expected stock 18.0 (deducted once), got {batch_qty}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        reset_db_manager()


def test_node_frontend_idempotency_behavioral_suite():
    """Run all executable Node behavioral tests against src/web/static/js/app.js."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node runtime not available")

    proc = subprocess.run(
        [node_bin, "--test", str(NODE_TEST_FILE)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"Node frontend tests failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
