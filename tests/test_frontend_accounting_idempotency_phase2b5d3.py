"""
Phase 2B-5D-3 Automated Verification Suite:
1. Runs executable Node behavioural tests (tests/frontend_accounting_idempotency.test.mjs)
   against the real src/web/static/js/app.js for all 4 financial entry points.
2. Live-FastAPI + isolated temporary SQLite E2E lost-response replay tests for ALL THREE
   backend operation types (Customer Receipt [both Accounts & Quick Farmer], Supplier Payment,
   and Daily Shop Expense), verifying actual SQLite row counts, vouchers, and balances.
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

from src.app import app
from src.db.connection import DatabaseManager, reset_db_manager
from src.models.master_data import Customer, Supplier
from src.repositories.master_data_repository import MasterDataRepository
from src.services.auth_service import AuthService

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NODE_ACCOUNTING_TESTS = PROJECT_ROOT / "tests" / "frontend_accounting_idempotency.test.mjs"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_node_frontend_accounting_idempotency_suite():
    """Run all executable Node behavioural tests for receipts, supplier payments, and expenses."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node runtime not available")

    proc = subprocess.run(
        [node_bin, "--test", str(NODE_ACCOUNTING_TESTS)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"Node accounting idempotency tests failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_e2e_lost_response_replay_all_three_accounting_operations(tmp_path, monkeypatch):
    """
    Live-FastAPI + isolated SQLite end-to-end lost-response & reload-replay test for:
    1. Customer Receipt (Accounts tab) -> lost response -> reload -> retry -> replay
    2. Quick Farmer Payment (Farmer modal) -> lost response -> close/reopen -> retry -> replay
    3. Supplier Payment -> lost response -> close/reopen -> retry -> replay
    4. Daily Shop Expense -> lost response -> reload -> retry -> replay

    Verifies exact SQLite row counts, voucher balances, customer balance, supplier balance,
    and cash balance.
    """
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node runtime not available")

    db_path = tmp_path / "e2e_accounting_2b5d3.db"
    bkp_dir = tmp_path / "backups"
    bkp_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    reset_db_manager()

    db = DatabaseManager(db_path=db_path, app_mode="local", backup_dir=bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)

    master = MasterDataRepository(db)
    cust_id = master.create_customer(
        Customer(customer_name="Ramesh Patil", mobile="9876543210", opening_balance=5000.0)
    )
    supp_id = master.create_supplier(
        Supplier(supplier_name="Agro Dist Ltd", mobile="9999900001", opening_balance=8000.0)
    )
    cat_row = db.fetch_one("SELECT category_id FROM expense_categories ORDER BY category_id ASC LIMIT 1;")
    cat_id = int(cat_row["category_id"])
    cash_before = float(
        db.fetch_one("SELECT current_balance FROM ledger_accounts WHERE account_name = 'Cash in Hand';")["current_balance"]
    )
    token = AuthService(db).create_access_token(1, "admin", "Administrator", "ADMIN")

    port = _find_free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10.0
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "Isolated uvicorn test server did not start"

    e2e_js = r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import crypto from "node:crypto";

const baseUrl = process.env.E2E_BASE_URL;
const token = process.env.E2E_TOKEN;
const custId = Number(process.env.E2E_CUST_ID);
const suppId = Number(process.env.E2E_SUPP_ID);
const catId = Number(process.env.E2E_CAT_ID);
const appJs = fs.readFileSync(process.env.E2E_APP_JS, "utf8");

function mkEl(id, val = "") {
    return {
        id, value: val, textContent: "", innerText: "", innerHTML: "", disabled: false,
        style: { display: "none" }, dataset: {},
        classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); } },
        focus() {}, addEventListener() {}, removeEventListener() {},
    };
}

function bootSandbox(storageData, dropSet) {
    const store = new Map(Object.entries(storageData || {}));
    const localStorage = {
        getItem: k => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: k => store.delete(k),
        _dump: () => Object.fromEntries(store.entries()),
    };
    const els = new Map([
        ["acc-receipt-cust", mkEl("acc-receipt-cust", String(custId))],
        ["acc-receipt-date", mkEl("acc-receipt-date", "2026-11-17")],
        ["acc-receipt-amt", mkEl("acc-receipt-amt", "600")],
        ["acc-receipt-mode", mkEl("acc-receipt-mode", "CASH")],
        ["btn-acc-save-receipt", mkEl("btn-acc-save-receipt")],
        ["acc-receipt-idem-banner", mkEl("acc-receipt-idem-banner")],
        ["acc-receipt-idem-msg", mkEl("acc-receipt-idem-msg")],
        ["btn-acc-receipt-retry", mkEl("btn-acc-receipt-retry")],
        ["modal-farmer-detail", mkEl("modal-farmer-detail")],
        ["fd-name", mkEl("fd-name")], ["fd-meta", mkEl("fd-meta")],
        ["fd-pending-balance", mkEl("fd-pending-balance")], ["fd-tot-purchases", mkEl("fd-tot-purchases")],
        ["fd-invoices-tbody", mkEl("fd-invoices-tbody")], ["fd-receipts-tbody", mkEl("fd-receipts-tbody")],
        ["fd-pay-amount", mkEl("fd-pay-amount", "400")],
        ["fd-pay-mode", mkEl("fd-pay-mode", "CASH")],
        ["btn-fd-save-receipt", mkEl("btn-fd-save-receipt")],
        ["fd-pay-idem-banner", mkEl("fd-pay-idem-banner")],
        ["fd-pay-idem-msg", mkEl("fd-pay-idem-msg")],
        ["btn-fd-pay-retry", mkEl("btn-fd-pay-retry")],
        ["modal-supplier-payment", mkEl("modal-supplier-payment")],
        ["sp-supplier-id", mkEl("sp-supplier-id", String(suppId))],
        ["sp-supplier-name", mkEl("sp-supplier-name", "Agro Dist Ltd")],
        ["sp-current-balance", mkEl("sp-current-balance")],
        ["sp-pay-amount", mkEl("sp-pay-amount", "1500")],
        ["sp-pay-date", mkEl("sp-pay-date", "2026-11-17")],
        ["sp-pay-mode", mkEl("sp-pay-mode", "BANK_TRANSFER")],
        ["sp-pay-ref", mkEl("sp-pay-ref", "UTR-E2E-1")],
        ["sp-pay-narration", mkEl("sp-pay-narration", "E2E supplier pay")],
        ["btn-save-supp-pay", mkEl("btn-save-supp-pay")],
        ["sp-pay-idem-banner", mkEl("sp-pay-idem-banner")],
        ["sp-pay-idem-msg", mkEl("sp-pay-idem-msg")],
        ["btn-sp-pay-retry", mkEl("btn-sp-pay-retry")],
        ["acc-exp-cat", mkEl("acc-exp-cat", String(catId))],
        ["acc-exp-amt", mkEl("acc-exp-amt", "200")],
        ["acc-exp-date", mkEl("acc-exp-date", "2026-11-17")],
        ["acc-exp-mode", mkEl("acc-exp-mode", "CASH")],
        ["acc-exp-remarks", mkEl("acc-exp-remarks", "E2E freight")],
        ["btn-acc-save-expense", mkEl("btn-acc-save-expense")],
        ["acc-exp-idem-banner", mkEl("acc-exp-idem-banner")],
        ["acc-exp-idem-msg", mkEl("acc-exp-idem-msg")],
        ["btn-acc-exp-retry", mkEl("btn-acc-exp-retry")],
    ]);
    const document = {
        body: mkEl("body"),
        getElementById: id => els.get(id) || null,
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: tag => mkEl(tag),
        addEventListener() {},
    };
    const responses = [];
    const wrappedFetch = async (url, opts = {}) => {
        const fullUrl = url.startsWith("http") ? url : `${baseUrl}${url}`;
        const headers = { ...(opts.headers || {}), Authorization: `Bearer ${token}` };
        const res = await globalThis.fetch(fullUrl, { ...opts, headers });
        const text = await res.text();
        const parsed = text ? JSON.parse(text) : {};
        if (dropSet.has(url)) {
            dropSet.delete(url);
            assert.equal(res.status, 200, `Expected server commit 200 before simulated drop on ${url}: ${text}`);
            throw new Error(`Simulated dropped response on ${url}`);
        }
        responses.push({ url, status: res.status, body: parsed });
        return { ok: res.ok, status: res.status, headers: res.headers, json: async () => parsed };
    };
    const win = {
        location: { origin: baseUrl, href: baseUrl, protocol: "http:" },
        crypto: { randomUUID: () => crypto.randomUUID() },
        localStorage, sessionStorage: localStorage, document, navigator: { onLine: true },
        fetch: wrappedFetch, addEventListener() {}, removeEventListener() {},
        setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
        Headers: globalThis.Headers,
    };
    const sandbox = {
        window: win, globalThis: win, self: win, document, navigator: win.navigator,
        localStorage, sessionStorage: localStorage, fetch: wrappedFetch, console, crypto: win.crypto,
        setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
        Headers: globalThis.Headers, Uint8Array, Promise, JSON, Date, Math, Number, String,
        Boolean, Array, Object, Set, Map, RegExp, Error, parseFloat, parseInt, isNaN,
    };
    vm.createContext(sandbox);
    vm.runInContext(appJs, sandbox, { filename: "app.js" });
    sandbox.showToast = () => {};
    sandbox.window.showToast = () => {};
    vm.runInContext(`
        currentUser = { user_id: 1, username: "admin", role: "ADMIN" };
        authToken = ${JSON.stringify(token)};
        activeFarmerId = ${custId};
        hasValidAuthSession = () => true;
        refreshCustomerList = async () => {};
        loadDayBook = async () => {};
        loadCashReconciliation = async () => {};
        loadFarmerStatusList = async () => {};
        loadSupplierKhata = async () => {};
        loadProfitAndLoss = async () => {};
    `, sandbox);
    return { sandbox, localStorage, els, responses };
}

// Phase 1: Submit all 4 with dropped responses AFTER server commit
const drops1 = new Set(["/api/accounting/customer-receipt", "/api/accounting/supplier-payment", "/api/accounting/expenses"]);
const b1 = bootSandbox({}, drops1);
await b1.sandbox.submitCustomerReceipt();
// Drop next customer-receipt for quick farmer payment too
drops1.add("/api/accounting/customer-receipt");
await b1.sandbox.submitQuickFarmerPayment();
await b1.sandbox.submitSupplierPayment();
await b1.sandbox.submitShopExpense();

// Verify all 4 persisted in localStorage
const savedStore = b1.localStorage._dump();
assert.ok(savedStore.krushidhan_pending_customer_receipt);
assert.ok(savedStore.krushidhan_pending_quick_farmer_payments);
assert.ok(savedStore.krushidhan_pending_supplier_payments);
assert.ok(savedStore.krushidhan_pending_shop_expense);

// Phase 2: Full reload simulation -> restore -> retry all 4 -> ALL MUST REPLAY (idempotent_replay: true)
const b2 = bootSandbox(savedStore, new Set());
b2.sandbox.restorePendingFinancialDrafts();
await b2.sandbox.retryPendingCustomerReceipt();
await b2.sandbox.openFarmerDetail(custId);
await b2.sandbox.retryPendingQuickFarmerPayment();
b2.sandbox.openSupplierPaymentModal(suppId, "Agro Dist Ltd", 6500);
await b2.sandbox.retryPendingSupplierPayment();
await b2.sandbox.retryPendingShopExpense();

const acctResponses = b2.responses.filter(r => r.url.startsWith("/api/accounting/"));
assert.equal(acctResponses.length, 4);
for (const r of acctResponses) {
    assert.equal(r.status, 200);
    assert.equal(r.body.idempotent_replay, true, `Expected idempotent_replay=true for ${r.url}`);
}
assert.equal(b2.localStorage.getItem("krushidhan_pending_customer_receipt"), null);
assert.equal(b2.localStorage.getItem("krushidhan_pending_quick_farmer_payments"), null);
assert.equal(b2.localStorage.getItem("krushidhan_pending_supplier_payments"), null);
assert.equal(b2.localStorage.getItem("krushidhan_pending_shop_expense"), null);

console.log(JSON.stringify({ ok: true, replays: acctResponses.map(r => r.body) }));
"""

    try:
        env = dict(os.environ)
        env["E2E_BASE_URL"] = f"http://127.0.0.1:{port}"
        env["E2E_TOKEN"] = token
        env["E2E_CUST_ID"] = str(cust_id)
        env["E2E_SUPP_ID"] = str(supp_id)
        env["E2E_CAT_ID"] = str(cat_id)
        env["E2E_APP_JS"] = str(PROJECT_ROOT / "src" / "web" / "static" / "js" / "app.js")

        proc = subprocess.run(
            [node_bin, "--input-type=module", "-e", e2e_js],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"E2E accounting script failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        assert out["ok"] is True

        # Verify actual SQLite DB state:
        # 2 receipts (600 + 400 = 1000), 1 supplier payment (1500), 1 expense (200) -> 4 vouchers total, 1 expense row
        assert db.fetch_one("SELECT COUNT(*) AS c FROM vouchers WHERE voucher_type = 'RECEIPT';")["c"] == 2
        assert db.fetch_one("SELECT COUNT(*) AS c FROM vouchers WHERE voucher_type = 'PAYMENT' AND reference_type = 'PURCHASE';")["c"] == 1
        assert db.fetch_one("SELECT COUNT(*) AS c FROM expenses;")["c"] == 1
        assert db.fetch_one("SELECT COUNT(*) AS c FROM vouchers WHERE reference_type = 'EXPENSE' AND idempotency_key IS NULL;")["c"] == 1

        # Exact customer & supplier & cash balances
        cust_bal = db.fetch_one("SELECT current_balance FROM customers WHERE customer_id = ?;", (cust_id,))["current_balance"]
        supp_bal = db.fetch_one("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supp_id,))["current_balance"]
        cash_after = db.fetch_one("SELECT current_balance FROM ledger_accounts WHERE account_name = 'Cash in Hand';")["current_balance"]
        assert cust_bal == pytest.approx(4000.0)   # 5000 - 600 - 400
        assert supp_bal == pytest.approx(6500.0)   # 8000 - 1500
        assert cash_after == pytest.approx(cash_before + 600.0 + 400.0 - 200.0)

        # Every voucher balanced (Dr == Cr)
        unbalanced = db.fetch_all(
            "SELECT voucher_id FROM ledger_entries GROUP BY voucher_id HAVING ABS(SUM(debit_amount) - SUM(credit_amount)) > 0.01;"
        )
        assert unbalanced == []
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        reset_db_manager()
