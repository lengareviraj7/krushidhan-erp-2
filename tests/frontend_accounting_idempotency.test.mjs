import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const APP_JS_PATH = path.resolve(__dirname, "../src/web/static/js/app.js");
const APP_JS_SOURCE = fs.readFileSync(APP_JS_PATH, "utf8");

function makeElement(id, tag = "div", initialValue = "") {
    return {
        id,
        tagName: tag.toUpperCase(),
        value: initialValue,
        textContent: "",
        innerText: "",
        innerHTML: "",
        disabled: false,
        style: { display: "", background: "", border: "", color: "" },
        dataset: {},
        className: "",
        classList: {
            _set: new Set(),
            add(...cls) { cls.forEach(c => this._set.add(c)); },
            remove(...cls) { cls.forEach(c => this._set.delete(c)); },
            contains(c) { return this._set.has(c); },
        },
        focus() { this._focused = true; },
        addEventListener() {},
        removeEventListener() {},
    };
}

function createAccountingSandbox({ fetchImpl, confirmResult = true, initialStorage = {}, online = true } = {}) {
    const storageMap = new Map(Object.entries(initialStorage));
    const localStorage = {
        getItem(k) { return storageMap.has(k) ? storageMap.get(k) : null; },
        setItem(k, v) { storageMap.set(k, String(v)); },
        removeItem(k) { storageMap.delete(k); },
        clear() { storageMap.clear(); },
        _dump() { return Object.fromEntries(storageMap.entries()); },
    };

    const elements = new Map();
    const defs = [
        // Accounts Receipt
        ["acc-receipt-cust", "select", "1"],
        ["acc-receipt-date", "input", "2026-11-17"],
        ["acc-receipt-amt", "input", "500"],
        ["acc-receipt-mode", "select", "CASH"],
        ["btn-acc-save-receipt", "button", ""],
        ["acc-receipt-idem-banner", "div", ""],
        ["acc-receipt-idem-msg", "div", ""],
        ["btn-acc-receipt-retry", "button", ""],
        ["btn-acc-receipt-continue-new", "button", ""],
        // Farmer Detail Quick Payment
        ["modal-farmer-detail", "div", ""],
        ["fd-name", "div", ""],
        ["fd-meta", "div", ""],
        ["fd-pending-balance", "div", ""],
        ["fd-tot-purchases", "div", ""],
        ["fd-invoices-tbody", "tbody", ""],
        ["fd-receipts-tbody", "tbody", ""],
        ["fd-pay-amount", "input", "300"],
        ["fd-pay-mode", "select", "CASH"],
        ["btn-fd-save-receipt", "button", ""],
        ["fd-pay-idem-banner", "div", ""],
        ["fd-pay-idem-msg", "div", ""],
        ["btn-fd-pay-retry", "button", ""],
        ["btn-fd-pay-continue-new", "button", ""],
        // Supplier Payment Modal
        ["modal-supplier-payment", "div", ""],
        ["sp-supplier-id", "input", "10"],
        ["sp-supplier-name", "div", "Agro Dist Ltd"],
        ["sp-current-balance", "span", ""],
        ["sp-pay-amount", "input", "1200"],
        ["sp-pay-date", "input", "2026-11-17"],
        ["sp-pay-mode", "select", "BANK_TRANSFER"],
        ["sp-pay-ref", "input", "UTR-8811"],
        ["sp-pay-narration", "input", "Part settle"],
        ["btn-save-supp-pay", "button", ""],
        ["sp-pay-idem-banner", "div", ""],
        ["sp-pay-idem-msg", "div", ""],
        ["btn-sp-pay-retry", "button", ""],
        ["btn-sp-pay-continue-new", "button", ""],
        // Daily Shop Expense
        ["acc-exp-cat", "select", "1"],
        ["acc-exp-amt", "input", "250"],
        ["acc-exp-date", "input", "2026-11-17"],
        ["acc-exp-mode", "select", "CASH"],
        ["acc-exp-remarks", "input", "Loading labor"],
        ["btn-acc-save-expense", "button", ""],
        ["acc-exp-idem-banner", "div", ""],
        ["acc-exp-idem-msg", "div", ""],
        ["btn-acc-exp-retry", "button", ""],
        ["btn-acc-exp-continue-new", "button", ""],
    ];
    for (const [id, tag, val] of defs) {
        const el = makeElement(id, tag, val);
        if (id === "btn-acc-save-receipt") el.textContent = "Record Customer Receipt";
        if (id === "btn-fd-save-receipt") el.textContent = "Record Receipt";
        if (id === "btn-save-supp-pay") el.textContent = "Record Payment";
        if (id === "btn-acc-save-expense") el.textContent = "Save Expense";
        if (id.includes("banner") || id.startsWith("modal-")) el.style.display = "none";
        elements.set(id, el);
    }

    const document = {
        body: makeElement("body", "body"),
        getElementById: (id) => elements.get(id) || null,
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: (tag) => makeElement("dyn-" + Math.random(), tag),
        addEventListener() {},
        removeEventListener() {},
    };

    const fetchCalls = [];
    const customFetch = async (url, opts = {}) => {
        fetchCalls.push({ url, opts });
        if (fetchImpl) return fetchImpl(url, opts);
        return { ok: true, status: 200, headers: { get: () => "" }, json: async () => ({ success: true }) };
    };

    const navigatorObj = { onLine: online };
    const windowObj = {
        location: { origin: "http://127.0.0.1:8000", href: "http://127.0.0.1:8000/", protocol: "http:" },
        crypto: { randomUUID: () => crypto.randomUUID() },
        localStorage,
        sessionStorage: localStorage,
        document,
        navigator: navigatorObj,
        fetch: customFetch,
        addEventListener() {},
        removeEventListener() {},
        setTimeout: () => 0,
        clearTimeout() {},
        setInterval: () => 0,
        clearInterval() {},
        Headers: globalThis.Headers,
    };

    const sandbox = {
        window: windowObj,
        globalThis: windowObj,
        self: windowObj,
        document,
        navigator: navigatorObj,
        localStorage,
        sessionStorage: localStorage,
        fetch: customFetch,
        console,
        crypto: windowObj.crypto,
        setTimeout: () => 0,
        clearTimeout() {},
        setInterval: () => 0,
        clearInterval() {},
        Headers: globalThis.Headers,
        Uint8Array, Promise, JSON, Date, Math, Number, String, Boolean, Array, Object,
        Set, Map, RegExp, Error, parseFloat, parseInt, isNaN,
    };

    vm.createContext(sandbox);
    vm.runInContext(APP_JS_SOURCE, sandbox, { filename: "app.js" });

    const toasts = [];
    const confirmCalls = [];
    const refreshCounts = {
        customers: 0,
        dayBook: 0,
        cashRecon: 0,
        farmerStatus: 0,
        supplierKhata: 0,
        pnl: 0,
        farmerDetailOpens: [],
    };
    let nextConfirmValue = confirmResult;

    sandbox.showToast = (msg, tone = "auto") => toasts.push({ msg, tone });
    sandbox.window.showToast = sandbox.showToast;
    sandbox.showConfirmDialog = async (opts) => {
        confirmCalls.push(opts);
        return typeof nextConfirmValue === "function" ? nextConfirmValue(opts) : Boolean(nextConfirmValue);
    };
    sandbox.window.showConfirmDialog = sandbox.showConfirmDialog;

    vm.runInContext(`
        currentUser = { user_id: 1, username: "admin", role: "ADMIN" };
        authToken = "secret-jwt-should-never-be-stored-in-pending";
        activeFarmerId = 1;
        hasValidAuthSession = () => true;
    `, sandbox);

    sandbox.refreshCustomerList = async () => { refreshCounts.customers += 1; };
    sandbox.loadDayBook = async () => { refreshCounts.dayBook += 1; };
    sandbox.loadCashReconciliation = async () => { refreshCounts.cashRecon += 1; };
    sandbox.loadFarmerStatusList = async () => { refreshCounts.farmerStatus += 1; };
    sandbox.loadSupplierKhata = async () => { refreshCounts.supplierKhata += 1; };
    sandbox.loadProfitAndLoss = async () => { refreshCounts.pnl += 1; };

    const origOpenFarmerDetail = sandbox.openFarmerDetail;
    sandbox._origOpenFarmerDetail = origOpenFarmerDetail;
    sandbox.openFarmerDetail = async (id) => {
        refreshCounts.farmerDetailOpens.push(id);
        return origOpenFarmerDetail(id);
    };

    return {
        sandbox,
        elements,
        localStorage,
        navigatorObj,
        toasts,
        confirmCalls,
        fetchCalls,
        refreshCounts,
        setNextConfirm(v) { nextConfirmValue = v; },
        getVar(name) { return vm.runInContext(name, sandbox); },
        evalJs(code) { return vm.runInContext(code, sandbox); },
    };
}

function jsonResp(body, status = 200) {
    return { ok: status >= 200 && status < 300, status, headers: { get: () => "" }, json: async () => body };
}

// =========================================================================
// Test 1: Double-click protection & button disable/re-enable on ALL 4 entry points
// =========================================================================
test("1. All 4 entry points block double-clicks and disable/re-enable buttons on success & failure", async () => {
    const resolvers = {};
    const callCounts = { rcp: 0, pay: 0, exp: 0 };
    const ctx = createAccountingSandbox({
        fetchImpl: async (url) => {
            if (url === "/api/accounting/customer-receipt") {
                callCounts.rcp += 1;
                return await new Promise(r => { resolvers.rcp = r; });
            }
            if (url === "/api/accounting/supplier-payment") {
                callCounts.pay += 1;
                return await new Promise(r => { resolvers.pay = r; });
            }
            if (url === "/api/accounting/expenses") {
                callCounts.exp += 1;
                return await new Promise(r => { resolvers.exp = r; });
            }
            if (url.startsWith("/api/masters/farmers/")) {
                return jsonResp({ customer: { customer_id: 1, customer_name: "Ramesh", current_balance: 0 }, invoices: [], receipts: [] });
            }
            return jsonResp({ success: true });
        },
    });

    // (a) Accounts Customer Receipt
    const r1 = ctx.sandbox.submitCustomerReceipt();
    const r2 = ctx.sandbox.submitCustomerReceipt();
    assert.equal(ctx.elements.get("btn-acc-save-receipt").disabled, true);
    assert.equal(callCounts.rcp, 1);
    resolvers.rcp(jsonResp({ success: true, voucher_id: 1, voucher_no: "RCP-00001", idempotent_replay: false }));
    await Promise.all([r1, r2]);
    assert.equal(ctx.elements.get("btn-acc-save-receipt").disabled, false);

    // (b) Quick Farmer Payment
    const q1 = ctx.sandbox.submitQuickFarmerPayment();
    const q2 = ctx.sandbox.submitQuickFarmerPayment();
    assert.equal(ctx.elements.get("btn-fd-save-receipt").disabled, true);
    assert.equal(callCounts.rcp, 2);
    resolvers.rcp(jsonResp({ success: true, voucher_id: 2, voucher_no: "RCP-00002", idempotent_replay: false }));
    await Promise.all([q1, q2]);
    assert.equal(ctx.elements.get("btn-fd-save-receipt").disabled, false);

    // (c) Supplier Payment (also simulates rapid Enter-key form submit + click)
    const s1 = ctx.sandbox.submitSupplierPayment();
    const s2 = ctx.sandbox.submitSupplierPayment();
    assert.equal(ctx.elements.get("btn-save-supp-pay").disabled, true);
    assert.equal(callCounts.pay, 1);
    resolvers.pay(jsonResp({ success: true, voucher_id: 3, voucher_no: "PAY-00003", idempotent_replay: false }));
    await Promise.all([s1, s2]);
    assert.equal(ctx.elements.get("btn-save-supp-pay").disabled, false);

    // (d) Daily Shop Expense — verify re-enable even on failure (500)
    const e1 = ctx.sandbox.submitShopExpense();
    const e2 = ctx.sandbox.submitShopExpense();
    assert.equal(ctx.elements.get("btn-acc-save-expense").disabled, true);
    assert.equal(callCounts.exp, 1);
    resolvers.exp(jsonResp({ detail: "Temporary error" }, 500));
    await Promise.all([e1, e2]);
    assert.equal(ctx.elements.get("btn-acc-save-expense").disabled, false);
});

// =========================================================================
// Test 2: Independent keys across all 4 forms + Body & HTTP Header + No JWT stored
// =========================================================================
test("2. Independent keys across all 4 entry points, sent in BOTH body & Idempotency-Key header, zero auth tokens persisted", async () => {
    const seen = [];
    const ctx = createAccountingSandbox({
        fetchImpl: async (url, opts) => {
            if (url.startsWith("/api/accounting/")) {
                const body = JSON.parse(opts.body);
                seen.push({ url, bodyKey: body.idempotency_key, headerKey: opts.headers["Idempotency-Key"] });
                throw new Error("Network timeout");
            }
            return jsonResp({});
        },
    });

    await ctx.sandbox.submitCustomerReceipt();
    await ctx.sandbox.submitQuickFarmerPayment();
    await ctx.sandbox.submitSupplierPayment();
    await ctx.sandbox.submitShopExpense();

    assert.equal(seen.length, 4);
    for (const call of seen) {
        assert.ok(call.bodyKey && call.bodyKey.length > 10);
        assert.equal(call.bodyKey, call.headerKey, "Body idempotency_key and HTTP Idempotency-Key header must match");
    }
    // All 4 keys must be strictly distinct (especially Accounts receipt vs Quick Farmer receipt!)
    assert.equal(new Set(seen.map(s => s.bodyKey)).size, 4);

    // Zero auth tokens stored in localStorage & nothing added to POS krushidhan_pending_bills
    const dumpStr = JSON.stringify(ctx.localStorage._dump());
    assert.ok(!dumpStr.includes("secret-jwt"), "Auth token must NEVER be stored in localStorage pending records");
    assert.equal(ctx.localStorage.getItem("krushidhan_pending_bills"), null, "Financial ops must NOT use POS offline queue");
});

// =========================================================================
// Test 3: Page reload restores pending payloads & keys with ZERO automatic submission
// =========================================================================
test("3. Reload restores pending receipts/expenses with original key and NEVER auto-submits on startup", async () => {
    const ctx1 = createAccountingSandbox({
        fetchImpl: async () => { throw new Error("Connection lost"); },
    });

    await ctx1.sandbox.submitCustomerReceipt();
    await ctx1.sandbox.submitShopExpense();
    const rcpKey = ctx1.getVar("currentCustomerReceiptIdempotencyKey");
    const expKey = ctx1.getVar("currentExpenseIdempotencyKey");
    const persistedStorage = ctx1.localStorage._dump();

    // Simulate full page reload with fresh sandbox initialized from persistedStorage
    let startupFetches = 0;
    const ctx2 = createAccountingSandbox({
        initialStorage: persistedStorage,
        fetchImpl: async (url, opts) => {
            startupFetches += 1;
            const body = JSON.parse(opts.body);
            if (url === "/api/accounting/customer-receipt") {
                return jsonResp({ success: true, voucher_id: 11, voucher_no: "RCP-00011", idempotent_replay: true, echoed_key: body.idempotency_key });
            }
            if (url === "/api/accounting/expenses") {
                return jsonResp({ success: true, expense_id: 21, voucher_id: 12, voucher_no: "PAY-00012", idempotent_replay: true, echoed_key: body.idempotency_key });
            }
            return jsonResp({});
        },
    });

    // Reset inputs to empty/different values to prove reload actually restores them from storage
    ctx2.elements.get("acc-receipt-amt").value = "";
    ctx2.elements.get("acc-exp-amt").value = "";
    ctx2.elements.get("acc-exp-remarks").value = "";

    ctx2.sandbox.restorePendingFinancialDrafts();

    assert.equal(startupFetches, 0, "Startup restoration MUST NOT auto-submit financial requests");
    assert.equal(ctx2.getVar("currentCustomerReceiptIdempotencyKey"), rcpKey);
    assert.equal(ctx2.getVar("currentExpenseIdempotencyKey"), expKey);
    assert.equal(ctx2.elements.get("acc-receipt-amt").value, "500");
    assert.equal(ctx2.elements.get("acc-exp-amt").value, "250");
    assert.equal(ctx2.elements.get("acc-exp-remarks").value, "Loading labor");
    assert.equal(ctx2.elements.get("acc-receipt-idem-banner").style.display, "block");
    assert.equal(ctx2.elements.get("acc-exp-idem-banner").style.display, "block");

    // Operator explicitly clicks Retry Same Request -> replays with exact original key & clears storage on 200
    await ctx2.sandbox.retryPendingCustomerReceipt();
    await ctx2.sandbox.retryPendingShopExpense();

    assert.equal(ctx2.fetchCalls.length, 2);
    assert.equal(JSON.parse(ctx2.fetchCalls[0].opts.body).idempotency_key, rcpKey);
    assert.equal(JSON.parse(ctx2.fetchCalls[1].opts.body).idempotency_key, expKey);
    assert.equal(ctx2.localStorage.getItem("krushidhan_pending_customer_receipt"), null);
    assert.equal(ctx2.localStorage.getItem("krushidhan_pending_shop_expense"), null);
    assert.equal(ctx2.getVar("currentCustomerReceiptIdempotencyKey"), null);
    assert.equal(ctx2.getVar("currentExpenseIdempotencyKey"), null);
    assert.ok(ctx2.toasts.some(t => t.msg.includes("RCP-00011") && t.msg.includes("Duplicate prevented")));
    assert.ok(ctx2.toasts.some(t => t.msg.includes("PAY-00012") && t.msg.includes("Duplicate prevented")));
    assert.ok(ctx2.refreshCounts.dayBook >= 2);
    assert.ok(ctx2.refreshCounts.cashRecon >= 2);
    assert.ok(ctx2.refreshCounts.customers >= 1);
    assert.ok(ctx2.refreshCounts.pnl >= 1);
});

// =========================================================================
// Test 4: Closing/reopening Farmer Detail & switching farmers preserves uncertain receipt per farmer
// =========================================================================
test("4. Farmer Detail modal preserves uncertain receipt across close/reopen and isolates keys between farmers", async () => {
    const sent = [];
    let failNext = true;
    const ctx = createAccountingSandbox({
        fetchImpl: async (url, opts) => {
            if (url === "/api/accounting/customer-receipt") {
                const body = JSON.parse(opts.body);
                sent.push(body);
                if (failNext) throw new Error("Socket dropped");
                return jsonResp({ success: true, voucher_id: 50, voucher_no: "RCP-00050", idempotent_replay: true });
            }
            if (url === "/api/masters/farmers/1/statement") {
                return jsonResp({ customer: { customer_id: 1, customer_name: "Farmer One", current_balance: 999 }, invoices: [], receipts: [] });
            }
            if (url === "/api/masters/farmers/2/statement") {
                return jsonResp({ customer: { customer_id: 2, customer_name: "Farmer Two", current_balance: 777 }, invoices: [], receipts: [] });
            }
            return jsonResp({});
        },
    });

    // Submit for Farmer 1 -> network error
    ctx.evalJs("activeFarmerId = 1;");
    ctx.elements.get("fd-pay-amount").value = "350";
    await ctx.sandbox.submitQuickFarmerPayment();
    const farmer1Key = ctx.getVar("currentQuickFarmerPaymentIdempotencyKey");
    assert.ok(farmer1Key);

    // Close modal -> localStorage MUST keep Farmer 1's pending receipt; in-memory key cleared
    ctx.sandbox.closeFarmerDetailModal();
    assert.equal(ctx.getVar("currentQuickFarmerPaymentIdempotencyKey"), null);
    assert.ok(ctx.localStorage.getItem("krushidhan_pending_quick_farmer_payments"));

    // Open Farmer 2 -> must NOT reuse Farmer 1's key or amount!
    await ctx.sandbox.openFarmerDetail(2);
    assert.equal(ctx.getVar("currentQuickFarmerPaymentIdempotencyKey"), null);
    assert.equal(ctx.elements.get("fd-pay-amount").value, 777);
    assert.equal(ctx.elements.get("fd-pay-idem-banner").style.display, "none");

    // Reopen Farmer 1 -> MUST restore Farmer 1's exact pending amount (350, not 999) and exact key!
    await ctx.sandbox.openFarmerDetail(1);
    assert.equal(ctx.getVar("currentQuickFarmerPaymentIdempotencyKey"), farmer1Key);
    assert.equal(ctx.elements.get("fd-pay-amount").value, "350");
    assert.equal(ctx.elements.get("fd-pay-idem-banner").style.display, "block");

    // Retry Farmer 1 -> succeeds with exact original key & clears pending entry
    failNext = false;
    await ctx.sandbox.retryPendingQuickFarmerPayment();
    assert.equal(sent[0].idempotency_key, sent[1].idempotency_key);
    assert.equal(ctx.localStorage.getItem("krushidhan_pending_quick_farmer_payments"), null);
    assert.ok(ctx.refreshCounts.farmerDetailOpens.includes(1));
});

// =========================================================================
// Test 5: 409 conflict & explicit confirmation, 400/422, 408/429/5xx, offline, and modal close for Supplier
// =========================================================================
test("5. 409 conflict requires confirmation before rotating key; 400/422/408/429/5xx & offline preserve form & key", async () => {
    let statusToReturn = 409;
    const ctx = createAccountingSandbox({
        fetchImpl: async (url) => {
            if (url === "/api/accounting/supplier-payment") {
                if (statusToReturn === 409) {
                    return jsonResp({ detail: "Payload differs", error_code: "IDEMPOTENCY_KEY_CONFLICT" }, 409);
                }
                if (statusToReturn === 422) return jsonResp({ detail: [{ msg: "Invalid amount" }] }, 422);
                if (statusToReturn === 400) return jsonResp({ detail: "Supplier not found" }, 400);
                if (statusToReturn === 408 || statusToReturn === 429 || statusToReturn === 503) {
                    return jsonResp({ detail: "Transient" }, statusToReturn);
                }
                return jsonResp({ success: true, voucher_id: 90, voucher_no: "PAY-00090", idempotent_replay: false });
            }
            return jsonResp({});
        },
        confirmResult: false,
    });

    // 409 conflict -> key NOT rotated, banner shown, form preserved
    await ctx.sandbox.submitSupplierPayment();
    const kOrig = ctx.getVar("currentSupplierPaymentIdempotencyKey");
    assert.ok(kOrig);
    assert.equal(ctx.elements.get("sp-pay-amount").value, "1200");
    assert.equal(ctx.elements.get("sp-pay-idem-banner").style.display, "block");

    // Closing & reopening same supplier preserves conflict state & key
    ctx.sandbox.closeSupplierPaymentModal();
    ctx.sandbox.openSupplierPaymentModal(10, "Agro Dist Ltd", 5000);
    assert.equal(ctx.getVar("currentSupplierPaymentIdempotencyKey"), kOrig);
    assert.equal(ctx.elements.get("sp-pay-amount").value, "1200");

    // Cancel confirmation -> key MUST stay unchanged
    ctx.setNextConfirm(false);
    assert.equal(await ctx.sandbox.continueSupplierPaymentAsNew(), false);
    assert.equal(ctx.getVar("currentSupplierPaymentIdempotencyKey"), kOrig);

    // Confirm -> key rotates, banner hidden, form preserved
    ctx.setNextConfirm(true);
    assert.equal(await ctx.sandbox.continueSupplierPaymentAsNew(), true);
    const kNew = ctx.getVar("currentSupplierPaymentIdempotencyKey");
    assert.ok(kNew && kNew !== kOrig);
    assert.equal(ctx.elements.get("sp-pay-amount").value, "1200");

    // Transient 408, 429, 503 keep key & form
    for (const code of [408, 429, 503]) {
        statusToReturn = code;
        await ctx.sandbox.submitSupplierPayment();
        assert.equal(ctx.getVar("currentSupplierPaymentIdempotencyKey"), kNew);
        assert.equal(ctx.elements.get("sp-pay-amount").value, "1200");
        assert.equal(ctx.elements.get("sp-pay-idem-banner").style.display, "block");
    }

    // 400 and 422 preserve key & form without silent key rotation
    for (const code of [400, 422]) {
        statusToReturn = code;
        await ctx.sandbox.submitSupplierPayment();
        assert.equal(ctx.getVar("currentSupplierPaymentIdempotencyKey"), kNew);
        assert.equal(ctx.elements.get("sp-pay-amount").value, "1200");
    }

    // Offline mode preserves payload & key locally without calling fetch
    const callsBeforeOffline = ctx.fetchCalls.length;
    ctx.navigatorObj.onLine = false;
    await ctx.sandbox.submitSupplierPayment();
    assert.equal(ctx.fetchCalls.length, callsBeforeOffline, "Offline submit must not issue fetch");
    assert.equal(ctx.getVar("currentSupplierPaymentIdempotencyKey"), kNew);
    assert.equal(ctx.localStorage.getItem("krushidhan_pending_bills"), null);

    // Reconnect and succeed -> clears pending record, resets key, refreshes Supplier Khata
    ctx.navigatorObj.onLine = true;
    statusToReturn = 200;
    await ctx.sandbox.retryPendingSupplierPayment();
    assert.equal(ctx.getVar("currentSupplierPaymentIdempotencyKey"), null);
    assert.equal(ctx.localStorage.getItem("krushidhan_pending_supplier_payments"), null);
    assert.ok(ctx.toasts.some(t => t.msg.includes("PAY-00090")));
    assert.equal(ctx.refreshCounts.supplierKhata, 1);
});

// =========================================================================
// Test 6: Post-save UI refresh failure after HTTP 200 NEVER marks transaction failed
// =========================================================================
test("6. Post-save UI refresh exception after HTTP 200 never leaves transaction stuck in pending storage", async () => {
    const ctx = createAccountingSandbox({
        fetchImpl: async () => jsonResp({ success: true, expense_id: 77, voucher_id: 78, voucher_no: "PAY-00078", idempotent_replay: false }),
    });
    ctx.sandbox.loadDayBook = async () => { throw new Error("UI refresh crash"); };
    ctx.sandbox.loadCashReconciliation = async () => { throw new Error("UI cash crash"); };

    await ctx.sandbox.submitShopExpense();
    assert.equal(ctx.localStorage.getItem("krushidhan_pending_shop_expense"), null);
    assert.equal(ctx.getVar("currentExpenseIdempotencyKey"), null);
    assert.equal(ctx.elements.get("acc-exp-amt").value, "");
    assert.equal(ctx.elements.get("acc-exp-idem-banner").style.display, "none");
    assert.ok(ctx.toasts.some(t => t.msg.includes("PAY-00078")));
});
