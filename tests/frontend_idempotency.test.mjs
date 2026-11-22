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
    const listeners = {};
    return {
        id,
        tagName: tag.toUpperCase(),
        value: initialValue,
        textContent: "",
        innerHTML: "",
        disabled: false,
        checked: false,
        style: { display: "" },
        dataset: {},
        className: "",
        classList: {
            _set: new Set(),
            add(...cls) { cls.forEach(c => this._set.add(c)); },
            remove(...cls) { cls.forEach(c => this._set.delete(c)); },
            contains(c) { return this._set.has(c); },
        },
        focus() { this._focused = true; },
        appendChild(child) {
            this._children = this._children || [];
            this._children.push(child);
            return child;
        },
        removeChild(child) {
            this._children = (this._children || []).filter(c => c !== child);
        },
        addEventListener(type, fn) {
            listeners[type] = listeners[type] || [];
            listeners[type].push(fn);
        },
        removeEventListener(type, fn) {
            listeners[type] = (listeners[type] || []).filter(f => f !== fn);
        },
        dispatchEvent(ev) {
            (listeners[ev.type] || []).forEach(fn => fn(ev));
        },
        click() {
            this.dispatchEvent({ type: "click", target: this });
        },
        getAttribute() { return null; },
    };
}

function createAppSandbox({ fetchImpl, confirmResult = true, initialStorage = {} } = {}) {
    const storageMap = new Map(Object.entries(initialStorage));
    const localStorage = {
        getItem(k) { return storageMap.has(k) ? storageMap.get(k) : null; },
        setItem(k, v) { storageMap.set(k, String(v)); },
        removeItem(k) { storageMap.delete(k); },
        clear() { storageMap.clear(); },
        _dump() { return Object.fromEntries(storageMap.entries()); },
    };

    const elements = new Map();
    const defaultIds = [
        ["pos-inv-no", "input", "INV-PREVIEW-999"],
        ["pos-date", "input", "2026-11-17"],
        ["pos-cust-name", "input", "Ramesh Patil"],
        ["pos-cust-mobile", "input", "9876543210"],
        ["pos-cust-village", "input", "Indapur"],
        ["pos-cust-crop", "input", "Sugarcane"],
        ["pos-pay-mode", "select", "CASH"],
        ["pos-paid-amount", "input", "500"],
        ["pos-remarks", "textarea", ""],
        ["pos-item-product", "select", ""],
        ["pos-item-batch", "select", ""],
        ["pos-item-rate", "input", ""],
        ["pos-item-qty", "input", "1"],
        ["pos-item-disc", "input", "0"],
        ["pos-cust-status-banner", "div", ""],
        ["pos-cust-suggestions", "div", ""],
        ["pos-cart-tbody", "tbody", ""],
        ["pos-cart-mobile-cards", "div", ""],
        ["pos-tot-taxable", "span", ""],
        ["pos-tot-gst", "span", ""],
        ["pos-tot-round", "span", ""],
        ["pos-grand-total", "span", ""],
        ["pos-mobile-grand-total", "span", ""],
        ["btn-pos-save-bill", "button", ""],
        ["btn-pos-save-bill-mobile", "button", ""],
        ["pos-idempotency-conflict-banner", "div", ""],
        ["pos-idempotency-conflict-msg", "div", ""],
        ["pos-offline-pending-banner", "div", ""],
        ["pos-offline-pending-text", "div", ""],
        ["pur-supplier", "select", "1"],
        ["pur-inv-no", "input", "SUP-INV-101"],
        ["pur-date", "input", "2026-11-17"],
        ["pur-item-product", "select", "10"],
        ["pur-batch-no", "input", "BATCH-01"],
        ["pur-exp-date", "input", "2027-12-31"],
        ["pur-qty", "input", "5"],
        ["pur-free-qty", "input", "0"],
        ["pur-rate", "input", "100"],
        ["pur-sale-rate", "input", "120"],
        ["pur-mrp", "input", "130"],
        ["btn-pur-save", "button", ""],
        ["pur-idempotency-conflict-banner", "div", ""],
        ["pur-idempotency-conflict-msg", "div", ""],
        ["btn-header-pending-review", "button", ""],
        ["pending-review-header-count", "span", ""],
        ["pending-bills-review-banner", "div", ""],
        ["pending-review-banner-count", "span", ""],
        ["pending-bills-review-banner-text", "div", ""],
        ["modal-pending-bills-review", "div", ""],
        ["pending-review-list", "div", ""],
        ["modal-invoice-preview", "div", ""],
        ["inv-prev-cust-name", "span", ""],
        ["inv-prev-cust-addr", "span", ""],
        ["inv-prev-cust-mobile", "span", ""],
        ["inv-prev-cust-gstin", "span", ""],
        ["inv-prev-crop", "span", ""],
        ["inv-prev-inv-no", "span", ""],
        ["inv-prev-date", "span", ""],
        ["inv-prev-paymode", "span", ""],
        ["inv-prev-paymode-badge", "span", ""],
        ["inv-prev-pay-status", "span", ""],
        ["inv-prev-paystatus-meta", "span", ""],
        ["inv-prev-items-tbody", "tbody", ""],
        ["inv-prev-subtotal", "span", ""],
        ["inv-prev-disc", "span", ""],
        ["inv-prev-taxable", "span", ""],
        ["inv-prev-cgst", "span", ""],
        ["inv-prev-sgst", "span", ""],
        ["inv-prev-igst", "span", ""],
        ["inv-prev-extra", "span", ""],
        ["inv-prev-round", "span", ""],
        ["inv-prev-net", "span", ""],
        ["inv-prev-paid", "span", ""],
        ["inv-prev-balance", "span", ""],
        ["inv-prev-words", "span", ""],
        ["inv-prev-remarks", "span", ""],
        ["krushidhan-toast-container", "div", ""],
        ["modal-app-confirm", "div", ""],
        ["app-confirm-header", "div", ""],
        ["app-confirm-title", "span", ""],
        ["app-confirm-message", "div", ""],
        ["app-confirm-ok", "button", ""],
        ["app-confirm-cancel", "button", ""],
        ["app-confirm-close-x", "button", ""],
    ];
    for (const [id, tag, val] of defaultIds) {
        const el = makeElement(id, tag, val);
        if (id === "btn-pos-save-bill") el.textContent = "SAVE & PRINT INVOICE (Ctrl+P)";
        if (id === "btn-pos-save-bill-mobile") el.textContent = "Save & Print Bill";
        if (id === "btn-pur-save") el.textContent = "Save Inward Stock & Update Supplier";
        if (id.includes("banner") || id.startsWith("modal-")) el.style.display = "none";
        elements.set(id, el);
    }

    const windowListeners = {};
    const docListeners = {};
    const document = {
        body: makeElement("body", "body"),
        getElementById(id) { return elements.get(id) || null; },
        querySelector(sel) {
            if (sel === 'button[onclick="submitPurchase()"]') return elements.get("btn-pur-save");
            return null;
        },
        querySelectorAll(sel) {
            if (sel === 'button[onclick="submitSalesBill()"]') {
                return [elements.get("btn-pos-save-bill"), elements.get("btn-pos-save-bill-mobile")];
            }
            return [];
        },
        createElement(tag) { return makeElement("dyn-" + Math.random(), tag); },
        addEventListener(type, fn) {
            docListeners[type] = docListeners[type] || [];
            docListeners[type].push(fn);
        },
        removeEventListener(type, fn) {
            docListeners[type] = (docListeners[type] || []).filter(f => f !== fn);
        },
    };

    const rawFetchCalls = [];
    const customFetch = async (url, opts = {}) => {
        rawFetchCalls.push({ url, opts });
        if (fetchImpl) return fetchImpl(url, opts);
        return {
            ok: true,
            status: 200,
            headers: { get: () => "" },
            json: async () => ({}),
        };
    };

    const windowObj = {
        location: { origin: "http://127.0.0.1:8000", href: "http://127.0.0.1:8000/", protocol: "http:" },
        crypto: { randomUUID: () => crypto.randomUUID() },
        localStorage,
        sessionStorage: localStorage,
        document,
        fetch: customFetch,
        print: () => { windowObj._printCount = (windowObj._printCount || 0) + 1; },
        addEventListener(type, fn) {
            windowListeners[type] = windowListeners[type] || [];
            windowListeners[type].push(fn);
        },
        removeEventListener(type, fn) {
            windowListeners[type] = (windowListeners[type] || []).filter(f => f !== fn);
        },
        dispatchEvent(ev) {
            (windowListeners[ev.type] || []).forEach(fn => fn(ev));
        },
        setTimeout: () => 0,
        clearTimeout: () => {},
        setInterval: () => 0,
        clearInterval: () => {},
        URL: { createObjectURL: () => "blob:test", revokeObjectURL: () => {} },
        Headers: globalThis.Headers,
        BroadcastChannel: undefined,
    };

    const sandbox = {
        window: windowObj,
        globalThis: windowObj,
        self: windowObj,
        document,
        localStorage,
        sessionStorage: localStorage,
        fetch: customFetch,
        console,
        crypto: windowObj.crypto,
        setTimeout: () => 0,
        clearTimeout: () => {},
        setInterval: () => 0,
        clearInterval: () => {},
        URL: windowObj.URL,
        Headers: globalThis.Headers,
        Uint8Array,
        Promise,
        JSON,
        Date,
        Math,
        Number,
        String,
        Boolean,
        Array,
        Object,
        Set,
        Map,
        RegExp,
        Error,
        parseFloat,
        parseInt,
        isNaN,
        decodeURIComponent,
        encodeURIComponent,
    };

    vm.createContext(sandbox);
    vm.runInContext(APP_JS_SOURCE, sandbox, { filename: "app.js" });

    // Record toasts and confirm calls cleanly; wire stubbed session/refresh hooks
    const toasts = [];
    const confirmCalls = [];
    let nextConfirmValue = confirmResult;
    const modalOpens = [];

    sandbox.showToast = (msg, tone = "auto") => {
        toasts.push({ msg, tone });
    };
    sandbox.window.showToast = sandbox.showToast;

    sandbox.showConfirmDialog = async (opts) => {
        confirmCalls.push(opts);
        return typeof nextConfirmValue === "function" ? nextConfirmValue(opts) : Boolean(nextConfirmValue);
    };
    sandbox.window.showConfirmDialog = sandbox.showConfirmDialog;

    const origOpenInvoicePreviewModal = sandbox.openInvoicePreviewModal;
    sandbox.openInvoicePreviewModal = (saleData, saleId) => {
        modalOpens.push({ saleData, saleId });
        return origOpenInvoicePreviewModal(saleData, saleId);
    };

    // Active authenticated session & baseline lookups
    vm.runInContext(`
        currentUser = { user_id: 1, username: "admin", role: "ADMIN" };
        authToken = "test-token";
        currentCompanySettings = { shop_name: "Test Agri Shop" };
        allCustomers = [{ customer_id: 7, customer_name: "Ramesh Patil", mobile: "9876543210", village: "Indapur" }];
        allProducts = [];
        currentCart = [{
            product_id: 10,
            batch_id: 100,
            product_name: "Urea 50kg",
            batch_no: "B-01",
            exp_date: "2027-12-31",
            hsn_code: "3102",
            qty: 2,
            unit_id: 1,
            unit_name: "BAG",
            sale_rate: 250,
            mrp: 260,
            discount_percent: 0,
            discount_amount: 0,
            taxable_amount: 476.19,
            cgst_rate: 2.5,
            cgst_amount: 11.90,
            sgst_rate: 2.5,
            sgst_amount: 11.91,
            igst_rate: 0,
            igst_amount: 0,
            total_amount: 500
        }];
        hasValidAuthSession = () => true;
        refreshProductList = async () => {};
        refreshCustomerList = async () => {};
        loadInventorySilently = async () => {};
        notifyOtherTabsDataChanged = () => {};
        loadSettings = async () => {};
    `, sandbox);

    return {
        sandbox,
        elements,
        localStorage,
        toasts,
        confirmCalls,
        modalOpens,
        rawFetchCalls,
        windowObj,
        setNextConfirm(val) { nextConfirmValue = val; },
        getVar(name) { return vm.runInContext(name, sandbox); },
        evalJs(code) { return vm.runInContext(code, sandbox); },
    };
}

function jsonResponse(body, status = 200, headersMap = {}) {
    return {
        ok: status >= 200 && status < 300,
        status,
        headers: { get: (k) => headersMap[k] || headersMap[k.toLowerCase()] || "" },
        json: async () => body,
    };
}

// =========================================================================
// Test 1: POS double-click guard + button disable/enable on success & failure
// =========================================================================
test("1. submitSalesBill double-click guard & button disable/re-enable (success & failure)", async () => {
    let resolveCreate;
    let createCalls = 0;
    const ctx = createAppSandbox({
        fetchImpl: async (url) => {
            if (url === "/api/sales/create") {
                createCalls += 1;
                return await new Promise(r => { resolveCreate = r; });
            }
            if (url === "/api/sales/55") {
                return jsonResponse({ sale_id: 55, invoice_no: "INV-00055", customer_name: "Ramesh Patil", net_amount: 500, paid_amount: 500, due_amount: 0, payment_mode: "CASH", items: [] });
            }
            if (url === "/api/sales/next-invoice-no") {
                return jsonResponse({ next_invoice_no: "INV-00056" });
            }
            return jsonResponse({});
        },
    });

    const p1 = ctx.sandbox.submitSalesBill();
    // Both Save buttons must be disabled immediately while in flight
    assert.equal(ctx.elements.get("btn-pos-save-bill").disabled, true);
    assert.equal(ctx.elements.get("btn-pos-save-bill").textContent, "Saving...");
    assert.equal(ctx.elements.get("btn-pos-save-bill-mobile").disabled, true);
    assert.equal(ctx.elements.get("btn-pos-save-bill-mobile").textContent, "Saving...");

    // Rapid second & third triggers while in flight must be ignored
    const p2 = ctx.sandbox.submitSalesBill();
    const p3 = ctx.sandbox.submitSalesBill();
    assert.equal(createCalls, 1);

    resolveCreate(jsonResponse({ success: true, sale_id: 55, invoice_no: "INV-00055", idempotent_replay: false }));
    await Promise.all([p1, p2, p3]);

    assert.equal(createCalls, 1);
    assert.equal(ctx.elements.get("btn-pos-save-bill").disabled, false);
    assert.equal(ctx.elements.get("btn-pos-save-bill-mobile").disabled, false);
    assert.equal(ctx.elements.get("pos-inv-no").value, "INV-00056");

    // Now verify buttons also re-enable after a failure (400 error)
    ctx.evalJs(`currentCart = [{ product_id: 10, batch_id: 100, qty: 1, sale_rate: 100, mrp: 100, discount_amount: 0, taxable_amount: 100, cgst_amount: 0, sgst_amount: 0, igst_amount: 0, total_amount: 100 }];`);
    ctx.elements.get("pos-cust-name").value = "Ramesh Patil";
    let failResolve;
    const origFetch = ctx.sandbox.window.fetch;
    ctx.sandbox.window.fetch = async (url) => {
        if (url === "/api/sales/create") return await new Promise(r => { failResolve = r; });
        return origFetch(url);
    };
    ctx.sandbox.fetch = ctx.sandbox.window.fetch;
    const pFail = ctx.sandbox.submitSalesBill();
    assert.equal(ctx.elements.get("btn-pos-save-bill").disabled, true);
    failResolve(jsonResponse({ detail: "Insufficient stock" }, 400));
    await pFail;
    assert.equal(ctx.elements.get("btn-pos-save-bill").disabled, false);
    assert.equal(ctx.elements.get("btn-pos-save-bill-mobile").disabled, false);
});

// =========================================================================
// Test 2: Purchase double-click guard & button disable/re-enable
// =========================================================================
test("2. submitPurchase double-click guard & button disable/re-enable", async () => {
    let resolvePur;
    let calls = 0;
    const ctx = createAppSandbox({
        fetchImpl: async (url) => {
            if (url === "/api/purchase/create") {
                calls += 1;
                return await new Promise(r => { resolvePur = r; });
            }
            return jsonResponse({});
        },
    });

    const p1 = ctx.sandbox.submitPurchase();
    assert.equal(ctx.elements.get("btn-pur-save").disabled, true);
    assert.equal(ctx.elements.get("btn-pur-save").textContent, "Saving...");

    const p2 = ctx.sandbox.submitPurchase();
    assert.equal(calls, 1);

    resolvePur(jsonResponse({ success: true, purchase_id: 12, invoice_no: "SUP-INV-101", idempotent_replay: false }));
    await Promise.all([p1, p2]);

    assert.equal(calls, 1);
    assert.equal(ctx.elements.get("btn-pur-save").disabled, false);
});

// =========================================================================
// Test 3: Keyboard shortcut Ctrl+P is guarded by submit lock
// =========================================================================
test("3. Keyboard shortcut Ctrl+P respects isSubmittingSale lock", async () => {
    let resolveCreate;
    let createCalls = 0;
    const ctx = createAppSandbox({
        fetchImpl: async (url) => {
            if (url === "/api/sales/create") {
                createCalls += 1;
                return await new Promise(r => { resolveCreate = r; });
            }
            return jsonResponse({ next_invoice_no: "INV-00002" });
        },
    });

    ctx.sandbox.initKeyboardShortcuts();
    const fireCtrlP = () => ctx.windowObj.dispatchEvent({
        type: "keydown",
        ctrlKey: true,
        key: "p",
        preventDefault() {},
    });

    fireCtrlP();
    fireCtrlP();
    fireCtrlP();
    assert.equal(createCalls, 1);

    resolveCreate(jsonResponse({ success: true, sale_id: 1, invoice_no: "INV-00001", idempotent_replay: false }));
    await new Promise(r => setImmediate(r));
    assert.equal(createCalls, 1);
});

// =========================================================================
// Test 4: Key lifecycle across failures, edits, 200 (replay true/false) & reset
// =========================================================================
test("4. Key persists across 422, 400 insufficient stock, and cart edits; resets only on 200 or explicit reset", async () => {
    const sentKeys = [];
    let mode = "422";
    const ctx = createAppSandbox({
        fetchImpl: async (url, opts) => {
            if (url === "/api/sales/create") {
                const body = JSON.parse(opts.body);
                sentKeys.push(body.idempotency_key);
                if (mode === "422") return jsonResponse({ detail: [{ msg: "Invalid qty" }] }, 422);
                if (mode === "400") return jsonResponse({ detail: "Insufficient stock" }, 400);
                if (mode === "replay") return jsonResponse({ success: true, sale_id: 9, invoice_no: "INV-00009", idempotent_replay: true });
                return jsonResponse({ success: true, sale_id: 10, invoice_no: "INV-00010", idempotent_replay: false });
            }
            return jsonResponse({ next_invoice_no: "INV-00011" });
        },
    });

    // Initially null (lazy generation)
    assert.equal(ctx.getVar("currentPosIdempotencyKey"), null);

    // First attempt -> 422 validation error
    await ctx.sandbox.submitSalesBill();
    const key1 = ctx.getVar("currentPosIdempotencyKey");
    assert.ok(key1 && key1.length > 10);

    // Operator edits cart and retries -> 400 insufficient stock -> key MUST NOT rotate!
    ctx.evalJs(`currentCart[0].qty = 1; currentCart[0].total_amount = 250;`);
    mode = "400";
    await ctx.sandbox.submitSalesBill();
    assert.equal(ctx.getVar("currentPosIdempotencyKey"), key1);
    assert.equal(sentKeys[0], sentKeys[1]);

    // Operator edits remarks and retries -> 200 idempotent_replay: true -> key resets & no second preview modal!
    ctx.elements.get("pos-remarks").value = "Edited remarks";
    mode = "replay";
    await ctx.sandbox.submitSalesBill();
    assert.equal(sentKeys[2], key1);
    assert.equal(ctx.getVar("currentPosIdempotencyKey"), null);
    assert.equal(ctx.modalOpens.length, 0, "idempotent_replay=true must NOT auto-open/print a second copy");
    assert.ok(ctx.toasts.some(t => t.msg.includes("INV-00009")));

    // Explicit resetPosCart resets key on a fresh draft
    ctx.evalJs(`currentCart = [{ product_id: 10, batch_id: 100, qty: 1, sale_rate: 100, mrp: 100, discount_amount: 0, taxable_amount: 100, cgst_amount: 0, sgst_amount: 0, igst_amount: 0, total_amount: 100 }];`);
    ctx.elements.get("pos-cust-name").value = "Ramesh Patil";
    mode = "400";
    await ctx.sandbox.submitSalesBill();
    const key2 = ctx.getVar("currentPosIdempotencyKey");
    assert.notEqual(key2, key1);
    ctx.sandbox.resetPosCart();
    assert.equal(ctx.getVar("currentPosIdempotencyKey"), null);
});

// =========================================================================
// Test 5: Key-conflict 409 preserves key & cart; explicit continue rotates ONLY after confirm
// =========================================================================
test("5. Key-conflict 409 preserves key & cart; continuePosAsNewBill rotates only after confirm", async () => {
    const ctx = createAppSandbox({
        fetchImpl: async (url) => {
            if (url === "/api/sales/create") {
                return jsonResponse(
                    { detail: "Idempotency key already used with different payload", error_code: "IDEMPOTENCY_KEY_CONFLICT" },
                    409
                );
            }
            return jsonResponse({});
        },
        confirmResult: false,
    });

    await ctx.sandbox.submitSalesBill();
    const originalKey = ctx.getVar("currentPosIdempotencyKey");
    assert.ok(originalKey);
    assert.equal(ctx.getVar("currentCart").length, 1, "Cart must be preserved on 409 conflict");
    assert.equal(ctx.elements.get("pos-idempotency-conflict-banner").style.display, "block");
    assert.equal(ctx.localStorage.getItem("krushidhan_pending_bills"), null, "409 must NOT queue bill offline");

    // Cancel confirmation -> key MUST NOT rotate, cart preserved
    ctx.setNextConfirm(false);
    const canceled = await ctx.sandbox.continuePosAsNewBill();
    assert.equal(canceled, false);
    assert.equal(ctx.getVar("currentPosIdempotencyKey"), originalKey);
    assert.equal(ctx.getVar("currentCart").length, 1);

    // Confirm -> key rotates to a new key, cart still preserved, banner hidden
    ctx.setNextConfirm(true);
    const confirmed = await ctx.sandbox.continuePosAsNewBill();
    assert.equal(confirmed, true);
    const rotatedKey = ctx.getVar("currentPosIdempotencyKey");
    assert.ok(rotatedKey && rotatedKey !== originalKey);
    assert.equal(ctx.getVar("currentCart").length, 1);
    assert.equal(ctx.elements.get("pos-idempotency-conflict-banner").style.display, "none");
});

// =========================================================================
// Test 6: Blank invoice_no + body-only key + server invoice_no everywhere + offline receipt rule
// =========================================================================
test("6. Sales sends blank invoice_no & body-only idempotency_key; UI uses server invoice_no; offline blocks printing", async () => {
    let capturedBody = null;
    let capturedHeaders = null;
    const ctx = createAppSandbox({
        fetchImpl: async (url, opts) => {
            if (url === "/api/sales/create") {
                capturedBody = JSON.parse(opts.body);
                capturedHeaders = opts.headers || {};
                return jsonResponse({ success: true, sale_id: 77, invoice_no: "INV-00077", idempotent_replay: false });
            }
            if (url === "/api/sales/77") {
                return jsonResponse({
                    sale_id: 77,
                    invoice_no: "INV-00077",
                    sale_date: "2026-11-17",
                    customer_name: "Ramesh Patil",
                    payment_mode: "CASH",
                    net_amount: 500,
                    paid_amount: 500,
                    due_amount: 0,
                    items: [],
                });
            }
            if (url === "/api/sales/next-invoice-no") {
                return jsonResponse({ next_invoice_no: "INV-00078" });
            }
            return jsonResponse({});
        },
    });

    ctx.elements.get("pos-inv-no").value = "INV-STALE-PREVIEW";
    await ctx.sandbox.submitSalesBill();

    assert.equal(capturedBody.invoice_no, "", "invoice_no must be sent blank");
    assert.ok(capturedBody.idempotency_key, "idempotency_key must be in JSON body");
    assert.equal(capturedHeaders["Idempotency-Key"], undefined, "Must NOT send Idempotency-Key HTTP header");

    // Toast, preview modal, local backup, and refreshed preview all use server invoice_no
    assert.equal(ctx.elements.get("inv-prev-inv-no").textContent, "INV-00077");
    assert.ok(ctx.toasts.some(t => t.msg.includes("INV-00077")));
    const savedBackup = JSON.parse(ctx.localStorage.getItem("krushidhan_saved_bills") || "[]");
    assert.equal(savedBackup[0].payload.invoice_no, "INV-00077");
    assert.equal(ctx.elements.get("pos-inv-no").value, "INV-00078");

    // Offline-queued bill must NOT open official invoice preview or allow printing preview number
    ctx.modalOpens.length = 0;
    ctx.evalJs(`currentCart = [{ product_id: 10, batch_id: 100, qty: 1, sale_rate: 100, mrp: 100, discount_amount: 0, taxable_amount: 100, cgst_amount: 0, sgst_amount: 0, igst_amount: 0, total_amount: 100 }];`);
    ctx.elements.get("pos-cust-name").value = "Ramesh Patil";
    ctx.sandbox.fetch = async () => { throw new Error("Network down"); };
    ctx.sandbox.window.fetch = ctx.sandbox.fetch;

    await ctx.sandbox.submitSalesBill();
    assert.equal(ctx.modalOpens.length, 0, "Offline-queued bill must NOT open official invoice preview");
    assert.ok(ctx.elements.get("pos-offline-pending-text").textContent.includes("Pending - invoice number assigned when saved"));

    // Defensive check: even if printInvoicePreview is invoked with OFFLINE- ID, it blocks window.print()
    ctx.evalJs(`currentSavedSaleId = "OFFLINE-12345";`);
    ctx.sandbox.printInvoicePreview();
    assert.equal(ctx.windowObj._printCount || 0, 0);
});

// =========================================================================
// Test 8: Queue legacy key persistence, 409/422 -> review, transient stays, 200 removes, review actions
// =========================================================================
test("8. Offline queue: legacy key persisted pre-fetch (invoice_no preserved), 409/422 -> review, transient stays, 200 removes", async () => {
    const legacyEntry = {
        payload: { invoice_no: "INV-00012", customer_id: 7, net_amount: 200, items: [] },
        created_at: "2026-11-16T10:00:00.000Z",
    };
    const transientEntry = {
        queue_id: "q-transient",
        idempotency_key: "key-transient",
        queued_at: "2026-11-17T08:00:00.000Z",
        customer_name: "Farmer B",
        payload: { invoice_no: "", idempotency_key: "key-transient", customer_id: 7, net_amount: 300, items: [] },
    };
    const okEntry = {
        queue_id: "q-ok",
        idempotency_key: "key-ok",
        queued_at: "2026-11-17T08:05:00.000Z",
        customer_name: "Farmer C",
        payload: { invoice_no: "", idempotency_key: "key-ok", customer_id: 7, net_amount: 400, items: [] },
    };
    const bad422Entry = {
        queue_id: "q-422",
        idempotency_key: "key-422",
        queued_at: "2026-11-17T08:06:00.000Z",
        customer_name: "Farmer D",
        payload: { invoice_no: "", idempotency_key: "key-422", customer_id: 7, net_amount: 150, items: [] },
    };

    let persistedBeforeLegacyFetch = null;
    let legacySentInvoiceNo = null;
    let legacySentKey = null;

    const ctx = createAppSandbox({
        initialStorage: {
            krushidhan_pending_bills: JSON.stringify([legacyEntry, transientEntry, okEntry, bad422Entry]),
        },
        fetchImpl: async (url, opts) => {
            if (url === "/api/sales/create") {
                const body = JSON.parse(opts.body);
                if (body.invoice_no === "INV-00012") {
                    // Check storage RIGHT BEFORE responding to verify legacy key was persisted pre-fetch!
                    const stored = JSON.parse(ctx.localStorage.getItem("krushidhan_pending_bills"));
                    persistedBeforeLegacyFetch = stored[0].idempotency_key;
                    legacySentInvoiceNo = body.invoice_no;
                    legacySentKey = body.idempotency_key;
                    return jsonResponse({ detail: "Duplicate invoice number", error_code: "DUPLICATE_INVOICE_NO" }, 409);
                }
                if (body.idempotency_key === "key-transient") {
                    return jsonResponse({ detail: "Server busy" }, 503);
                }
                if (body.idempotency_key === "key-ok") {
                    return jsonResponse({ success: true, sale_id: 88, invoice_no: "INV-00088", idempotent_replay: false });
                }
                if (body.idempotency_key === "key-422") {
                    return jsonResponse({ detail: [{ msg: "Validation failed" }] }, 422);
                }
                // Save as New Bill from review modal
                return jsonResponse({ success: true, sale_id: 99, invoice_no: "INV-00099", idempotent_replay: false });
            }
            return jsonResponse({ next_invoice_no: "INV-00100" });
        },
    });

    await ctx.sandbox.syncPendingOfflineBills();

    // Legacy entry got a fresh key persisted BEFORE fetch AND kept explicit invoice_no
    assert.ok(persistedBeforeLegacyFetch, "Legacy key must be persisted BEFORE first retry fetch");
    assert.equal(legacySentKey, persistedBeforeLegacyFetch);
    assert.equal(legacySentInvoiceNo, "INV-00012", "Legacy explicit invoice_no must NEVER be blanked out");

    // Pending queue retains ONLY the transient 503 entry
    const pendingAfter = JSON.parse(ctx.localStorage.getItem("krushidhan_pending_bills"));
    assert.equal(pendingAfter.length, 1);
    assert.equal(pendingAfter[0].idempotency_key, "key-transient");

    // Review list received BOTH the 409 legacy entry and the 422 entry (not deleted, not retrying forever)
    const reviewAfter = JSON.parse(ctx.localStorage.getItem("krushidhan_pending_bills_review"));
    assert.equal(reviewAfter.length, 2);
    assert.equal(reviewAfter[0].error_code, "DUPLICATE_INVOICE_NO");
    assert.equal(reviewAfter[1].http_status, 422);
    assert.equal(ctx.elements.get("pending-review-banner-count").textContent, "2");
    assert.equal(ctx.elements.get("pending-bills-review-banner").style.display, "flex");

    // Test "Save as New Bill" with confirmation -> sends blank invoice_no & fresh key, removes from review
    ctx.setNextConfirm(true);
    const savedNew = await ctx.sandbox.resubmitReviewBillAsNew(reviewAfter[0].review_id);
    assert.equal(savedNew, true);
    const lastCreateCall = ctx.rawFetchCalls.filter(c => c.url === "/api/sales/create").at(-1);
    const newBody = JSON.parse(lastCreateCall.opts.body);
    assert.equal(newBody.invoice_no, "");
    assert.notEqual(newBody.idempotency_key, legacySentKey);

    // Test "Mark as Checked and Remove" requires confirmation
    ctx.setNextConfirm(false);
    assert.equal(await ctx.sandbox.markReviewBillCheckedAndRemove(reviewAfter[1].review_id), false);
    assert.equal(JSON.parse(ctx.localStorage.getItem("krushidhan_pending_bills_review")).length, 1);

    ctx.setNextConfirm(true);
    assert.equal(await ctx.sandbox.markReviewBillCheckedAndRemove(reviewAfter[1].review_id), true);
    assert.equal(JSON.parse(ctx.localStorage.getItem("krushidhan_pending_bills_review")).length, 0);
    assert.equal(ctx.elements.get("pending-bills-review-banner").style.display, "none");
});

// =========================================================================
// Test 9: Sync in-flight lock + concurrent queue entry not lost
// =========================================================================
test("9. syncPendingOfflineBills lock prevents overlap AND bill queued mid-sync is never lost", async () => {
    let resolveFirstFetch;
    let createCalls = 0;
    const ctx = createAppSandbox({
        initialStorage: {
            krushidhan_pending_bills: JSON.stringify([
                {
                    queue_id: "q-1",
                    idempotency_key: "key-1",
                    queued_at: "2026-11-17T09:00:00.000Z",
                    payload: { invoice_no: "", idempotency_key: "key-1", customer_id: 7, net_amount: 100, items: [] },
                },
            ]),
        },
        fetchImpl: async (url) => {
            if (url === "/api/sales/create") {
                createCalls += 1;
                return await new Promise(r => { resolveFirstFetch = r; });
            }
            return jsonResponse({ next_invoice_no: "INV-00050" });
        },
    });

    // Launch two simultaneous sync triggers
    const s1 = ctx.sandbox.syncPendingOfflineBills();
    const s2 = ctx.sandbox.syncPendingOfflineBills();
    assert.equal(createCalls, 1, "Overlapping sync triggers must process each entry only once");

    // While sync #1 is mid-flight, a new bill is queued offline!
    ctx.sandbox.queuePendingOfflineBill({ invoice_no: "", idempotency_key: "key-mid-sync", customer_id: 7, net_amount: 250, items: [] });

    // Finish sync #1 for key-1
    resolveFirstFetch(jsonResponse({ success: true, sale_id: 50, invoice_no: "INV-00050", idempotent_replay: false }));
    await Promise.all([s1, s2]);

    // key-1 removed, but key-mid-sync MUST still be safely in localStorage!
    const remaining = JSON.parse(ctx.localStorage.getItem("krushidhan_pending_bills"));
    assert.equal(remaining.length, 1);
    assert.equal(remaining[0].idempotency_key, "key-mid-sync");
});

// =========================================================================
// Test 10: Purchase clears form on 200; network error keeps key & form & safe-retry message
// =========================================================================
test("10. submitPurchase: network error keeps key & form with safe-retry msg; 200 clears form & resets key", async () => {
    let shouldFail = true;
    const sentKeys = [];
    const ctx = createAppSandbox({
        fetchImpl: async (url, opts) => {
            if (url === "/api/purchase/create") {
                const body = JSON.parse(opts.body);
                sentKeys.push(body.idempotency_key);
                if (shouldFail) throw new Error("Connection reset");
                return jsonResponse({ success: true, purchase_id: 31, invoice_no: "SUP-INV-101", idempotent_replay: false });
            }
            return jsonResponse({});
        },
    });

    // Network error
    await ctx.sandbox.submitPurchase();
    const key1 = ctx.getVar("currentPurchaseIdempotencyKey");
    assert.ok(key1);
    assert.equal(ctx.elements.get("pur-inv-no").value, "SUP-INV-101", "Form must be preserved on network error");
    assert.equal(ctx.elements.get("pur-batch-no").value, "BATCH-01");
    assert.equal(ctx.elements.get("pur-qty").value, "5");
    assert.equal(ctx.localStorage.getItem("krushidhan_pending_bills"), null, "Purchases must NOT be queued offline");
    assert.ok(
        ctx.toasts.some(t => t.msg.includes("Could not confirm whether this purchase was saved. It is safe to press Save again")),
        "Safe-retry message must be displayed"
    );

    // Retry succeeds with the SAME key -> form cleared & key reset
    shouldFail = false;
    await ctx.sandbox.submitPurchase();
    assert.equal(sentKeys[0], sentKeys[1]);
    assert.equal(ctx.getVar("currentPurchaseIdempotencyKey"), null);
    assert.equal(ctx.elements.get("pur-inv-no").value, "");
    assert.equal(ctx.elements.get("pur-batch-no").value, "");
    assert.equal(ctx.elements.get("pur-qty").value, "");
});

// =========================================================================
// Test 11: Auto-created customer is NOT duplicated on double-click or retry
// =========================================================================
test("11. Auto-created farmer is created once and reused on retry after bill failure", async () => {
    let customerCreateCalls = 0;
    let saleAttempt = 0;
    let capturedSaleCustIds = [];
    const ctx = createAppSandbox({
        fetchImpl: async (url, opts) => {
            if (url === "/api/masters/customers") {
                customerCreateCalls += 1;
                return jsonResponse({ success: true, customer_id: 404 });
            }
            if (url === "/api/sales/create") {
                saleAttempt += 1;
                const body = JSON.parse(opts.body);
                capturedSaleCustIds.push(body.customer_id);
                if (saleAttempt === 1) {
                    return jsonResponse({ detail: "Temporary validation error" }, 400);
                }
                return jsonResponse({ success: true, sale_id: 120, invoice_no: "INV-00120", idempotent_replay: false });
            }
            return jsonResponse({ next_invoice_no: "INV-00121" });
        },
    });

    // Enter a brand-new farmer not in allCustomers
    ctx.elements.get("pos-cust-name").value = "Suresh Jadhav";
    ctx.elements.get("pos-cust-mobile").value = "9000011111";
    ctx.elements.get("pos-cust-village").value = "Baramati";

    // Attempt 1: auto-creates farmer #404, then sale returns 400
    await ctx.sandbox.submitSalesBill();
    assert.equal(customerCreateCalls, 1);
    assert.equal(ctx.getVar("selectedPosFarmerId"), 404);

    // Attempt 2 (retry): MUST reuse customer #404 and NOT POST /api/masters/customers again
    await ctx.sandbox.submitSalesBill();
    assert.equal(customerCreateCalls, 1, "Retry must NEVER duplicate auto-created customer");
    assert.deepEqual(capturedSaleCustIds, [404, 404]);
});

// =========================================================================
// Extra edge-case coverage: Purchase 409/422/replay, Sales DUPLICATE_INVOICE_NO,
// Auto-create customer double-click, and 408/429/network queue retention
// =========================================================================
test("12. Purchase 409 conflict & 422 preserve key/form; Sales DUPLICATE_INVOICE_NO keeps key/cart; auto-create customer double-click creates once", async () => {
    // (a) Purchase 409 conflict + continuePurchaseAsNewBill Cancel vs Confirm
    let purMode = "422";
    const ctxPur = createAppSandbox({
        fetchImpl: async (url) => {
            if (url === "/api/purchase/create") {
                if (purMode === "422") return jsonResponse({ detail: [{ msg: "Blank invoice_no" }] }, 422);
                if (purMode === "409") return jsonResponse({ detail: "Conflict", error_code: "IDEMPOTENCY_KEY_CONFLICT" }, 409);
                return jsonResponse({ success: true, purchase_id: 44, invoice_no: "SUP-INV-101", idempotent_replay: true });
            }
            return jsonResponse({});
        },
        confirmResult: false,
    });

    await ctxPur.sandbox.submitPurchase();
    const pKey = ctxPur.getVar("currentPurchaseIdempotencyKey");
    assert.ok(pKey);
    assert.equal(ctxPur.elements.get("pur-inv-no").value, "SUP-INV-101");

    // Edit form -> key must stay the same
    ctxPur.elements.get("pur-qty").value = "8";
    purMode = "409";
    await ctxPur.sandbox.submitPurchase();
    assert.equal(ctxPur.getVar("currentPurchaseIdempotencyKey"), pKey);
    assert.equal(ctxPur.elements.get("pur-idempotency-conflict-banner").style.display, "block");

    // Cancel continue -> key unchanged
    ctxPur.setNextConfirm(false);
    assert.equal(await ctxPur.sandbox.continuePurchaseAsNewBill(), false);
    assert.equal(ctxPur.getVar("currentPurchaseIdempotencyKey"), pKey);

    // Confirm continue -> key rotated, form preserved
    ctxPur.setNextConfirm(true);
    assert.equal(await ctxPur.sandbox.continuePurchaseAsNewBill(), true);
    assert.notEqual(ctxPur.getVar("currentPurchaseIdempotencyKey"), pKey);
    assert.equal(ctxPur.elements.get("pur-qty").value, "8");

    // Replay 200 clears form & resets key
    purMode = "replay";
    await ctxPur.sandbox.submitPurchase();
    assert.equal(ctxPur.getVar("currentPurchaseIdempotencyKey"), null);
    assert.equal(ctxPur.elements.get("pur-inv-no").value, "");

    // (b) Sales DUPLICATE_INVOICE_NO keeps key & cart, does not queue
    const ctxDup = createAppSandbox({
        fetchImpl: async () => jsonResponse({ detail: "Invoice INV-1 already exists", error_code: "DUPLICATE_INVOICE_NO" }, 409),
    });
    await ctxDup.sandbox.submitSalesBill();
    assert.ok(ctxDup.getVar("currentPosIdempotencyKey"));
    assert.equal(ctxDup.getVar("currentCart").length, 1);
    assert.equal(ctxDup.localStorage.getItem("krushidhan_pending_bills"), null);

    // (c) Auto-create customer double-click creates customer exactly once
    let resolveCust;
    let custCalls = 0;
    const ctxCust = createAppSandbox({
        fetchImpl: async (url) => {
            if (url === "/api/masters/customers") {
                custCalls += 1;
                return await new Promise(r => { resolveCust = r; });
            }
            if (url === "/api/sales/create") {
                return jsonResponse({ success: true, sale_id: 200, invoice_no: "INV-00200", idempotent_replay: false });
            }
            return jsonResponse({ next_invoice_no: "INV-00201" });
        },
    });
    ctxCust.elements.get("pos-cust-name").value = "Mahesh Pawar";
    const c1 = ctxCust.sandbox.submitSalesBill();
    const c2 = ctxCust.sandbox.submitSalesBill();
    assert.equal(custCalls, 1);
    resolveCust(jsonResponse({ success: true, customer_id: 505 }));
    await Promise.all([c1, c2]);
    assert.equal(custCalls, 1);

    // (d) Queue retains entries on 408, 429, and network throw
    const ctxTrans = createAppSandbox({
        initialStorage: {
            krushidhan_pending_bills: JSON.stringify([
                { queue_id: "q-408", idempotency_key: "k-408", queued_at: "2026-11-17T00:00:00Z", payload: { invoice_no: "", idempotency_key: "k-408", customer_id: 7, net_amount: 100, items: [] } },
                { queue_id: "q-429", idempotency_key: "k-429", queued_at: "2026-11-17T00:00:00Z", payload: { invoice_no: "", idempotency_key: "k-429", customer_id: 7, net_amount: 100, items: [] } },
                { queue_id: "q-net", idempotency_key: "k-net", queued_at: "2026-11-17T00:00:00Z", payload: { invoice_no: "", idempotency_key: "k-net", customer_id: 7, net_amount: 100, items: [] } },
            ]),
        },
        fetchImpl: async (_url, opts) => {
            const k = JSON.parse(opts.body).idempotency_key;
            if (k === "k-408") return jsonResponse({ detail: "Timeout" }, 408);
            if (k === "k-429") return jsonResponse({ detail: "Too many requests" }, 429);
            throw new Error("ECONNREFUSED");
        },
    });
    await ctxTrans.sandbox.syncPendingOfflineBills();
    assert.equal(JSON.parse(ctxTrans.localStorage.getItem("krushidhan_pending_bills")).length, 3);
    assert.equal(ctxTrans.localStorage.getItem("krushidhan_pending_bills_review"), null);
});

// =========================================================================
// Test 13: Post-save preview modal DOM error NEVER falsely queues confirmed sale offline
// =========================================================================
test("13. Confirmed HTTP 200 sale with missing inv-prev-* summary DOM nodes never falsely queues offline", async () => {
    const ctx = createAppSandbox({
        fetchImpl: async (url) => {
            if (url === "/api/sales/create") {
                return jsonResponse({ success: true, sale_id: 301, invoice_no: "INV-00301", idempotent_replay: false });
            }
            if (url === "/api/sales/301") {
                return jsonResponse({
                    sale_id: 301,
                    invoice_no: "INV-00301",
                    sale_date: "2026-11-17",
                    customer_name: "Ramesh Patil",
                    payment_mode: "CASH",
                    net_amount: 500,
                    paid_amount: 500,
                    due_amount: 0,
                    items: [],
                });
            }
            if (url === "/api/sales/next-invoice-no") {
                return jsonResponse({ next_invoice_no: "INV-00302" });
            }
            return jsonResponse({});
        },
    });

    // Simulate drifted index.html missing summary DOM nodes (the exact bug caught in live browser test)
    ctx.elements.delete("inv-prev-disc");
    ctx.elements.delete("inv-prev-taxable");
    ctx.elements.delete("inv-prev-cgst");
    ctx.elements.delete("inv-prev-sgst");

    await ctx.sandbox.submitSalesBill();

    assert.equal(ctx.elements.get("modal-invoice-preview").style.display, "flex", "Preview modal must still open cleanly");
    assert.equal(ctx.elements.get("inv-prev-inv-no").textContent, "INV-00301");
    assert.equal(ctx.localStorage.getItem("krushidhan_pending_bills"), null, "Confirmed sale must NEVER be queued offline");
    assert.equal(ctx.getVar("currentPosIdempotencyKey"), null);
    assert.equal(ctx.elements.get("pos-inv-no").value, "INV-00302");
});
