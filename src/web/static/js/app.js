/*
  Offline Agri-Input Shop ERP - Client Application Logic
  Secured with 256-Bit Cryptographic Session Tokens & RBAC
*/

// Authentication State
let currentUser = null;

// ============================================================
// DESKTOP SIDECAR RUNTIME BRIDGE, FETCH WRAPPER & CUSTOM DIALOGS
// ============================================================
const _originalFetch = window.fetch;
let _desktopRuntimeInitPromise = null;
let _domReadyStarted = false;
let _initialBootstrapCompleted = false;
const _desktopRuntimeState = {
    mode: "web",
    apiBaseUrl: "",
    launchToken: "",
    saveFile: null,
    openExternal: null,
    initialized: false,
};
let _lastMaintenanceToastAt = 0;

function _normalizeApiBaseUrl(rawUrl, fallbackPort = null) {
    let candidate = (rawUrl === null || rawUrl === undefined) ? "" : String(rawUrl).trim();
    if (!candidate && fallbackPort !== null && fallbackPort !== undefined && String(fallbackPort).trim() !== "") {
        const portNum = parseInt(String(fallbackPort).trim(), 10);
        if (Number.isInteger(portNum) && portNum > 0 && portNum <= 65535) {
            candidate = `http://127.0.0.1:${portNum}`;
        }
    }
    if (!candidate) return "";
    return candidate.replace(/\/+$/, "");
}

function _applyDesktopRuntimeObject(rawCtx) {
    if (!rawCtx || typeof rawCtx !== "object") return false;
    const prevBase = _desktopRuntimeState.apiBaseUrl;
    const prevHasToken = Boolean(_desktopRuntimeState.launchToken);

    const nextBase = _normalizeApiBaseUrl(
        rawCtx.apiBaseUrl ?? rawCtx.api_base_url ?? rawCtx.baseUrl ?? "",
        rawCtx.port ?? rawCtx.sidecarPort ?? null
    );
    if (nextBase) {
        _desktopRuntimeState.apiBaseUrl = nextBase;
    }

    const rawToken = rawCtx.launchToken ?? rawCtx.launch_token ?? "";
    if (typeof rawToken === "string" && rawToken.trim()) {
        // Store per-launch secret strictly in module memory closure (never in localStorage/sessionStorage/DOM/URLs).
        _desktopRuntimeState.launchToken = rawToken.trim();
        try {
            if ("launchToken" in rawCtx) rawCtx.launchToken = "";
            if ("launch_token" in rawCtx) rawCtx.launch_token = "";
        } catch (e) {
            // Ignore immutable host objects
        }
    }

    if (typeof rawCtx.mode === "string" && rawCtx.mode.trim()) {
        _desktopRuntimeState.mode = rawCtx.mode.trim();
    } else if (_desktopRuntimeState.launchToken || _desktopRuntimeState.apiBaseUrl) {
        _desktopRuntimeState.mode = "desktop";
    }

    if (typeof rawCtx.saveFile === "function") {
        _desktopRuntimeState.saveFile = rawCtx.saveFile;
    }
    if (typeof rawCtx.openExternal === "function") {
        _desktopRuntimeState.openExternal = rawCtx.openExternal;
    }

    _desktopRuntimeState.initialized = Boolean(
        _desktopRuntimeState.apiBaseUrl ||
        _desktopRuntimeState.launchToken ||
        _desktopRuntimeState.mode === "desktop"
    );

    return (
        (prevBase !== _desktopRuntimeState.apiBaseUrl) ||
        (!prevHasToken && Boolean(_desktopRuntimeState.launchToken))
    );
}

function isDesktopRuntime() {
    return Boolean(
        _desktopRuntimeState.mode === "desktop" ||
        _desktopRuntimeState.launchToken ||
        _desktopRuntimeState.apiBaseUrl
    );
}

function getKrushidhanDesktopContext() {
    return {
        isDesktop: isDesktopRuntime(),
        mode: _desktopRuntimeState.mode,
        apiBaseUrl: _desktopRuntimeState.apiBaseUrl,
        hasLaunchToken: Boolean(_desktopRuntimeState.launchToken),
    };
}

function setKrushidhanDesktopContext(ctx) {
    const changed = _applyDesktopRuntimeObject(ctx);
    if (changed && _domReadyStarted && typeof document !== "undefined" && !hasValidAuthSession()) {
        Promise.resolve().then(async () => {
            if (typeof checkAuthState === "function" && !hasValidAuthSession()) {
                const authed = await checkAuthState();
                if (authed && !_initialBootstrapCompleted && typeof bootstrapDashboard === "function") {
                    _initialBootstrapCompleted = true;
                    await bootstrapDashboard();
                }
            }
        });
    }
    return getKrushidhanDesktopContext();
}

window.getKrushidhanDesktopContext = getKrushidhanDesktopContext;
window.setKrushidhanDesktopContext = setKrushidhanDesktopContext;

async function initDesktopRuntimeContext() {
    if (typeof window !== "undefined" && window.__KRUSHIDHAN_DESKTOP__) {
        _applyDesktopRuntimeObject(window.__KRUSHIDHAN_DESKTOP__);
    }
    if (_desktopRuntimeState.launchToken && _desktopRuntimeState.apiBaseUrl) {
        return getKrushidhanDesktopContext();
    }
    if (_desktopRuntimeInitPromise) {
        return _desktopRuntimeInitPromise;
    }
    const tauriInvoke =
        (typeof window !== "undefined" && window.__TAURI__?.core?.invoke) ||
        (typeof window !== "undefined" && window.__TAURI_INTERNALS__?.invoke) ||
        null;
    if (typeof tauriInvoke !== "function") {
        return getKrushidhanDesktopContext();
    }
    _desktopRuntimeInitPromise = (async () => {
        const timeoutPromise = new Promise((resolve) => setTimeout(() => resolve(null), 1500));
        for (const cmd of ["get_sidecar_runtime", "get_sidecar_context", "get_sidecar_config"]) {
            try {
                const payload = await Promise.race([tauriInvoke(cmd), timeoutPromise]);
                if (payload && typeof payload === "object") {
                    _applyDesktopRuntimeObject(payload);
                    if (_desktopRuntimeState.launchToken || _desktopRuntimeState.apiBaseUrl) break;
                }
            } catch (e) {
                // Fail soft if host runs in standard browser mode or exposes another command
            }
        }
        return getKrushidhanDesktopContext();
    })();
    return _desktopRuntimeInitPromise;
}

if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    window.addEventListener("krushidhan-desktop-ready", (ev) => {
        const detail = ev && ev.detail ? ev.detail : window.__KRUSHIDHAN_DESKTOP__;
        setKrushidhanDesktopContext(detail);
    });
}

function isApiRequestUrl(url) {
    if (typeof url !== "string") return false;
    if (url === "/api" || url.startsWith("/api/")) return true;
    if (_desktopRuntimeState.apiBaseUrl) {
        const prefix = `${_desktopRuntimeState.apiBaseUrl}/api`;
        return url === prefix || url.startsWith(`${prefix}/`);
    }
    return false;
}

function resolveApiUrl(url) {
    if (typeof url !== "string") return url;
    if (_desktopRuntimeState.apiBaseUrl && (url === "/api" || url.startsWith("/api/"))) {
        return `${_desktopRuntimeState.apiBaseUrl}${url}`;
    }
    return url;
}

function _normalizeHeadersObject(rawHeaders) {
    const out = {};
    if (!rawHeaders) return out;
    if (typeof Headers !== "undefined" && rawHeaders instanceof Headers) {
        rawHeaders.forEach((val, key) => { out[key] = val; });
        return out;
    }
    if (Array.isArray(rawHeaders)) {
        rawHeaders.forEach((pair) => {
            if (Array.isArray(pair) && pair.length >= 2) {
                out[String(pair[0])] = String(pair[1]);
            }
        });
        return out;
    }
    Object.entries(rawHeaders).forEach(([k, v]) => {
        if (v !== undefined && v !== null) out[k] = v;
    });
    return out;
}

function _hasHeaderCaseInsensitive(headersObj, targetKey) {
    const lowerTarget = targetKey.toLowerCase();
    return Object.keys(headersObj).some((k) => k.toLowerCase() === lowerTarget && String(headersObj[k] || "").trim() !== "");
}

function _inferToastTone(message, requestedTone) {
    const valid = ["success", "error", "warning", "info"];
    if (valid.includes(requestedTone)) return requestedTone;
    const lower = String(message || "").toLowerCase();
    if (
        lower.includes("error") ||
        lower.includes("failed") ||
        lower.includes("access denied") ||
        lower.includes("insufficient") ||
        lower.includes("could not") ||
        lower.includes("invalid") ||
        lower.includes("unhealthy") ||
        lower.includes("corrupt")
    ) {
        return "error";
    }
    if (
        lower.includes("offline") ||
        lower.includes("required") ||
        lower.includes("expired") ||
        lower.includes("please select") ||
        lower.includes("please enter") ||
        lower.includes("must be") ||
        lower.includes("cannot") ||
        lower.includes("no ready") ||
        lower.includes("zero pending") ||
        lower.includes("maintenance")
    ) {
        return "warning";
    }
    if (
        lower.includes("success") ||
        lower.includes("saved") ||
        lower.includes("recorded") ||
        lower.includes("updated") ||
        lower.includes("created") ||
        lower.includes("deleted") ||
        lower.includes("added") ||
        lower.includes("restored") ||
        lower.includes("verified") ||
        String(message || "").trim().startsWith("✓") ||
        String(message || "").trim().startsWith("✅")
    ) {
        return "success";
    }
    return "info";
}

function showToast(message, tone = "auto", durationMs = 4800) {
    const text = String(message ?? "").trim();
    if (!text) return null;
    const resolvedTone = _inferToastTone(text, tone);
    if (typeof document === "undefined") return null;

    let container = document.getElementById("krushidhan-toast-container");
    if (!container && document.body) {
        container = document.createElement("div");
        container.id = "krushidhan-toast-container";
        container.className = "toast-container no-print";
        document.body.appendChild(container);
    }
    if (!container) return null;

    while (container.children && container.children.length >= 5) {
        container.removeChild(container.firstChild);
    }

    const toast = document.createElement("div");
    toast.className = `krushidhan-toast toast-${resolvedTone}`;
    toast.setAttribute("role", resolvedTone === "error" ? "alert" : "status");

    const body = document.createElement("div");
    body.className = "krushidhan-toast-body";
    body.textContent = text;

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "krushidhan-toast-close";
    closeBtn.setAttribute("aria-label", "Dismiss notification");
    closeBtn.textContent = "✕";

    let timerId = null;
    const removeToast = () => {
        if (timerId) {
            clearTimeout(timerId);
            timerId = null;
        }
        if (toast.parentNode) {
            toast.parentNode.removeChild(toast);
        }
    };
    closeBtn.addEventListener("click", removeToast);

    toast.appendChild(body);
    toast.appendChild(closeBtn);
    container.appendChild(toast);

    if (durationMs > 0) {
        timerId = setTimeout(removeToast, durationMs);
    }
    return toast;
}
window.showToast = showToast;

let _activeConfirmResolver = null;
function showConfirmDialog({
    title = "Confirm Action",
    message = "",
    confirmText = "Confirm",
    cancelText = "Cancel",
    danger = false,
} = {}) {
    if (_activeConfirmResolver) {
        const prevResolve = _activeConfirmResolver;
        _activeConfirmResolver = null;
        prevResolve(false);
    }

    const modal = document.getElementById("modal-app-confirm");
    const header = document.getElementById("app-confirm-header");
    const titleEl = document.getElementById("app-confirm-title");
    const msgEl = document.getElementById("app-confirm-message");
    const okBtn = document.getElementById("app-confirm-ok");
    const cancelBtn = document.getElementById("app-confirm-cancel");
    const closeXBtn = document.getElementById("app-confirm-close-x");

    if (!modal || !okBtn || !cancelBtn) {
        return Promise.resolve(false);
    }

    if (titleEl) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = message;
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;
    okBtn.className = danger ? "btn btn-danger" : "btn btn-primary";
    if (header) header.style.background = danger ? "#991b1b" : "";

    modal.style.display = "flex";

    return new Promise((resolve) => {
        let settled = false;
        const finish = (accepted) => {
            if (settled) return;
            settled = true;
            _activeConfirmResolver = null;
            modal.style.display = "none";
            okBtn.removeEventListener("click", onOk);
            cancelBtn.removeEventListener("click", onCancel);
            if (closeXBtn) closeXBtn.removeEventListener("click", onCancel);
            modal.removeEventListener("click", onBackdrop);
            if (typeof window !== "undefined") window.removeEventListener("keydown", onKey);
            resolve(Boolean(accepted));
        };
        _activeConfirmResolver = finish;

        const onOk = () => finish(true);
        const onCancel = () => finish(false);
        const onBackdrop = (ev) => {
            if (ev.target === modal) finish(false);
        };
        const onKey = (ev) => {
            if (ev.key === "Escape") finish(false);
        };

        okBtn.addEventListener("click", onOk);
        cancelBtn.addEventListener("click", onCancel);
        if (closeXBtn) closeXBtn.addEventListener("click", onCancel);
        modal.addEventListener("click", onBackdrop);
        if (typeof window !== "undefined") window.addEventListener("keydown", onKey);

        setTimeout(() => {
            try { okBtn.focus(); } catch (e) {}
        }, 30);
    });
}
window.showConfirmDialog = showConfirmDialog;

let _activeInputResolver = null;
function showInputDialog({
    title = "Enter Value",
    message = "",
    placeholder = "",
    defaultValue = "",
    confirmText = "Submit",
    cancelText = "Cancel",
    inputType = "text",
    minLength = 0,
    emptyMessage = "Value is required.",
    shortMessage = "Value is too short.",
} = {}) {
    if (_activeInputResolver) {
        const prevResolve = _activeInputResolver;
        _activeInputResolver = null;
        prevResolve(null);
    }

    const modal = document.getElementById("modal-app-input");
    const form = document.getElementById("app-input-form");
    const titleEl = document.getElementById("app-input-title");
    const msgEl = document.getElementById("app-input-message");
    const inputEl = document.getElementById("app-input-field");
    const pwdToggle = document.getElementById("app-input-pwd-toggle");
    const errEl = document.getElementById("app-input-error");
    const okBtn = document.getElementById("app-input-ok");
    const cancelBtn = document.getElementById("app-input-cancel");
    const closeXBtn = document.getElementById("app-input-close-x");

    if (!modal || !form || !inputEl || !okBtn || !cancelBtn) {
        return Promise.resolve(null);
    }

    if (titleEl) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = message;
    inputEl.type = inputType || "text";
    inputEl.placeholder = placeholder || "";
    inputEl.value = defaultValue || "";
    if (pwdToggle) {
        pwdToggle.style.display = (inputType === "password") ? "block" : "none";
    }
    if (errEl) {
        errEl.style.display = "none";
        errEl.textContent = "";
    }
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;
    modal.style.display = "flex";

    return new Promise((resolve) => {
        let settled = false;
        const finish = (value) => {
            if (settled) return;
            settled = true;
            _activeInputResolver = null;
            modal.style.display = "none";
            form.removeEventListener("submit", onSubmit);
            cancelBtn.removeEventListener("click", onCancel);
            if (closeXBtn) closeXBtn.removeEventListener("click", onCancel);
            modal.removeEventListener("click", onBackdrop);
            if (typeof window !== "undefined") window.removeEventListener("keydown", onKey);
            resolve(value);
        };
        _activeInputResolver = finish;

        const onSubmit = (ev) => {
            ev.preventDefault();
            const val = inputEl.value.trim();
            if (!val) {
                if (errEl) {
                    errEl.textContent = emptyMessage;
                    errEl.style.display = "block";
                }
                inputEl.focus();
                return;
            }
            if (minLength > 0 && val.length < minLength) {
                if (errEl) {
                    errEl.textContent = shortMessage || `Must be at least ${minLength} characters.`;
                    errEl.style.display = "block";
                }
                inputEl.focus();
                return;
            }
            finish(val);
        };
        const onCancel = () => finish(null);
        const onBackdrop = (ev) => {
            if (ev.target === modal) finish(null);
        };
        const onKey = (ev) => {
            if (ev.key === "Escape") finish(null);
        };

        form.addEventListener("submit", onSubmit);
        cancelBtn.addEventListener("click", onCancel);
        if (closeXBtn) closeXBtn.addEventListener("click", onCancel);
        modal.addEventListener("click", onBackdrop);
        if (typeof window !== "undefined") window.addEventListener("keydown", onKey);

        setTimeout(() => {
            try { inputEl.focus(); } catch (e) {}
        }, 40);
    });
}
window.showInputDialog = showInputDialog;

async function openExternalUrl(url, label = "external link") {
    const cleanUrl = String(url || "").trim();
    if (!cleanUrl) return false;
    if (typeof navigator !== "undefined" && navigator.onLine === false) {
        showToast(`Cannot open ${label} while offline. Please connect to the internet first.`, "warning");
        return false;
    }

    const hook = _desktopRuntimeState.openExternal ||
        (typeof window !== "undefined" && window.__KRUSHIDHAN_DESKTOP__?.openExternal) ||
        null;
    if (typeof hook === "function") {
        try {
            await hook(cleanUrl);
            return true;
        } catch (err) {
            showToast(`Could not open ${label}: ${err.message}`, "warning");
            return false;
        }
    }

    if (typeof document !== "undefined" && document.body) {
        const link = document.createElement("a");
        link.href = cleanUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.style.display = "none";
        document.body.appendChild(link);
        link.click();
        setTimeout(() => {
            if (link.parentNode) link.parentNode.removeChild(link);
        }, 1000);
        return true;
    }
    return false;
}

async function _rawDesktopFetch(url, options = {}, { skipAuthRedirect = false, includeAuth = true } = {}) {
    const opt = options || {};
    const isApi = isApiRequestUrl(url);
    const targetUrl = resolveApiUrl(url);
    const headers = _normalizeHeadersObject(opt.headers);

    if (isApi) {
        if (_desktopRuntimeState.launchToken && !_hasHeaderCaseInsensitive(headers, "X-Krushidhan-Launch-Token")) {
            headers["X-Krushidhan-Launch-Token"] = _desktopRuntimeState.launchToken;
        }
        if (includeAuth) {
            const token = typeof localStorage !== "undefined" ? localStorage.getItem("krushidhan_auth_token") : null;
            if (token && !_hasHeaderCaseInsensitive(headers, "Authorization")) {
                headers["Authorization"] = `Bearer ${token}`;
            }
        }
    }

    const fetchOpts = { ...opt, headers };
    if ("credentials" in fetchOpts && fetchOpts.credentials === "include" && isDesktopRuntime()) {
        delete fetchOpts.credentials;
    }

    try {
        const res = await _originalFetch(targetUrl, fetchOpts);
        if (res.status === 503 && isApi) {
            const now = Date.now();
            if (now - _lastMaintenanceToastAt > 4000) {
                _lastMaintenanceToastAt = now;
                showToast("Database maintenance or restore is in progress. Please wait a moment and retry.", "warning");
            }
        }
        if (
            !skipAuthRedirect &&
            res.status === 401 &&
            isApi &&
            !String(url).includes("/api/auth/login") &&
            !String(url).includes("/api/auth/setup-admin") &&
            !String(url).includes("/api/auth/setup-status")
        ) {
            const errBody = (typeof res.clone === "function")
                ? await res.clone().json().catch(() => ({}))
                : {};
            const detail = String(errBody.detail || "");
            if (detail.includes("Launch-Token") || detail.toLowerCase().includes("launch token")) {
                showToast("Desktop sidecar security handshake pending. Please wait or restart the app.", "warning");
            } else {
                stopAutoSyncPolling();
                currentUser = null;
                console.warn("Unauthorized API call, showing login portal:", url);
                showLoginOverlay("Session expired. Please login again.");
            }
        }
        return res;
    } catch (err) {
        console.error("Network or API Fetch error:", err);
        throw err;
    }
}

window.fetch = async function(url, options = {}) {
    return _rawDesktopFetch(url, options, { skipAuthRedirect: false, includeAuth: true });
};

// Global State
let currentCart = [];
let allProducts = [];
let allCustomers = [];
let allSuppliers = [];
let allLookups = {};
let farmerStatusList = [];
let isOnlyCreditFilter = false;
let activeFarmerId = null;
let currentCompanySettings = null;

function escapeHtml(val) {
    if (val === null || val === undefined) return "";
    return String(val)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

let pnlAllProducts = [];

// ----------------- Real-Time Multi-Tab & Device Auto-Sync Controller -----------------
const erpSyncChannel = (typeof BroadcastChannel !== "undefined") ? new BroadcastChannel("agri_erp_channel") : null;
if (erpSyncChannel) {
    erpSyncChannel.onmessage = (event) => {
        if (event.data && event.data.action === "REFRESH_ALL") {
            refreshAllDataSilently();
        }
    };
}

function notifyOtherTabsDataChanged() {
    if (erpSyncChannel) {
        try {
            erpSyncChannel.postMessage({ action: "REFRESH_ALL", timestamp: Date.now() });
        } catch (e) {
            console.error("BroadcastChannel error:", e);
        }
    }
}

let autoSyncInterval = null;
let authSessionVersion = 0;

function hasValidAuthSession() {
    return Boolean(currentUser && localStorage.getItem("krushidhan_auth_token"));
}

function stopAutoSyncPolling() {
    if (autoSyncInterval) {
        clearInterval(autoSyncInterval);
        autoSyncInterval = null;
    }
}

function startAutoSyncPolling() {
    if (autoSyncInterval || !hasValidAuthSession()) return;
    autoSyncInterval = setInterval(() => {
        if (!document.hidden && hasValidAuthSession()) {
            refreshAllDataSilently();
        }
    }, 8000);
}

async function refreshAllDataSilently() {
    if (!hasValidAuthSession()) return;
    try {
        await refreshProductList();
        await refreshCustomerList();
        await loadInventorySilently();
    } catch (e) {
        // Silent catch for background auto-polling
    }
}

async function loadInventorySilently() {
    if (!hasValidAuthSession()) return;
    try {
        const res = await fetch("/api/inventory/stock-summary");
        if (!res.ok || !hasValidAuthSession()) return;
        const data = await res.json();
        if (Array.isArray(data)) {
            rawInventoryItems = data;
            filterInventoryTable();
        }
    } catch (err) {
        // Silent
    }
}

document.addEventListener("DOMContentLoaded", async () => {
    _domReadyStarted = true;
    await initDesktopRuntimeContext();
    initTabs();
    initKeyboardShortcuts();
    initDefaultDates();
    initSettingsDirtyTracking();
    
    // Authenticate & Verify Session before loading sensitive ERP records
    const isAuthed = await checkAuthState();
    if (isAuthed) {
        _initialBootstrapCompleted = true;
        await bootstrapDashboard();
    }
});

async function bootstrapDashboard() {
    if (!hasValidAuthSession()) return;
    const sessionVer = authSessionVersion;
    await loadInitialLookups();
    if (!hasValidAuthSession() || sessionVer !== authSessionVersion) return;
    await loadSettings(true);
    if (!hasValidAuthSession() || sessionVer !== authSessionVersion) return;
    await initPosBilling();
    if (!hasValidAuthSession() || sessionVer !== authSessionVersion) return;
    loadInventory();
    loadDayBook();
    if (currentUser && currentUser.role === "ADMIN") {
        loadProfitAndLoss();
        loadAuthUsers();
        if (typeof loadBackupManagerData === "function") {
            loadBackupManagerData();
        }
    }
    loadFarmerStatusList();
    loadMasterTables();
    updatePendingBillsReviewIndicator();
    updatePosOfflinePendingBanner();
    if (typeof restorePendingFinancialDrafts === "function") {
        restorePendingFinancialDrafts();
    }
    startAutoSyncPolling();
}


function initDefaultDates() {
    const today = new Date().toISOString().split("T")[0];
    const firstDayMonth = today.substring(0, 8) + "01";
    
    const pnlFrom = document.getElementById("pnl-from-date");
    const pnlTo = document.getElementById("pnl-to-date");
    if (pnlFrom && !pnlFrom.value) pnlFrom.value = firstDayMonth;
    if (pnlTo && !pnlTo.value) pnlTo.value = today;

    const statFrom = document.getElementById("stat-from-date");
    const statTo = document.getElementById("stat-to-date");
    if (statFrom && !statFrom.value) statFrom.value = firstDayMonth;
    if (statTo && !statTo.value) statTo.value = today;

    const cdDate = document.getElementById("cd-recon-date");
    if (cdDate && !cdDate.value) cdDate.value = today;

    const expDate = document.getElementById("acc-exp-date");
    if (expDate && !expDate.value) expDate.value = today;
}

// ----------------- Mobile Drawer & Touch Helpers -----------------
function toggleMobileMenu() {
    const drawer = document.getElementById("mobile-drawer");
    const backdrop = document.getElementById("mobile-drawer-backdrop");
    if (drawer && backdrop) {
        const isOpen = drawer.classList.contains("open");
        if (isOpen) {
            drawer.classList.remove("open");
            backdrop.classList.remove("show");
        } else {
            drawer.classList.add("open");
            backdrop.classList.add("show");
        }
    }
}

function closeMobileMenu() {
    const drawer = document.getElementById("mobile-drawer");
    const backdrop = document.getElementById("mobile-drawer-backdrop");
    if (drawer) drawer.classList.remove("open");
    if (backdrop) backdrop.classList.remove("show");
}

function stepPosQty(delta) {
    const qtyInput = document.getElementById("pos-item-qty");
    if (!qtyInput) return;
    let val = parseFloat(qtyInput.value) || 1;
    val = Math.max(0.1, val + delta);
    qtyInput.value = Math.round(val * 100) / 100;
}

// ----------------- Tab Navigation -----------------
function initTabs() {
    const tabs = document.querySelectorAll(".nav-tab, .mobile-drawer-item, .mobile-bottom-nav-item");
    tabs.forEach(tab => {
        tab.addEventListener("click", () => {
            const targetId = tab.getAttribute("data-tab");
            if (!targetId) return;

            document.querySelectorAll(".nav-tab, .mobile-drawer-item, .mobile-bottom-nav-item").forEach(t => {
                if (t.getAttribute("data-tab") === targetId) {
                    t.classList.add("active");
                } else {
                    t.classList.remove("active");
                }
            });

            document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));
            const targetPane = document.getElementById(targetId);
            if (targetPane) targetPane.classList.add("active");

            closeMobileMenu();

            // Auto-refresh relevant tab data
            if (targetId === "tab-sales-returns") loadSalesReturns();
            if (targetId === "tab-farmer-status") loadFarmerStatusList();
            if (targetId === "tab-supplier-khata") loadSupplierKhata();
            if (targetId === "tab-inventory") loadInventory();
            if (targetId === "tab-accounts") { loadDayBook(); loadCashReconciliation(); loadExpenseCategoryFilter(); }
            if (targetId === "tab-pnl") loadProfitAndLoss();
            if (targetId === "tab-gst") loadGSTR1();
            if (targetId === "tab-statutory") loadStatutoryRegister();
            if (targetId === "tab-purchase-returns") {
                if (!canAccessPurchaseReturns()) {
                    showToast("Access restricted: Purchase Returns require Admin, Owner, Manager, or Accountant permissions.", "error");
                    navigateToTab("tab-purchase");
                    return;
                }
                loadPurchaseReturns();
            }
            if (targetId === "tab-masters") loadMasterTables();
            if (targetId === "tab-settings") {
                settingsDirtyFields.clear();
                loadSettings(false);
                if (currentUser && currentUser.role === "ADMIN" && typeof loadBackupManagerData === "function") {
                    loadBackupManagerData();
                }
            }
        });
    });
}


// ----------------- Keyboard Shortcuts -----------------
function initKeyboardShortcuts() {
    window.addEventListener("keydown", (e) => {
        if (e.key === "F2") {
            e.preventDefault();
            if (!isSubmittingSale) resetPosCart();
        } else if (e.ctrlKey && e.key.toLowerCase() === "p") {
            e.preventDefault();
            submitSalesBill();
        }
    });
}

// ----------------- Data Loading -----------------
async function loadInitialLookups() {
    if (!hasValidAuthSession()) return;
    try {
        const res = await fetch("/api/masters/all");
        if (!res.ok) {
            const taxSelect = document.getElementById("m-prod-tax");
            if (taxSelect) taxSelect.innerHTML = '<option value="">⚠️ Failed to load tax rates</option>';
            return;
        }
        allLookups = await res.json();
        
        // Populate Categories in master forms and filters
        const catSelects = ["pos-prod-cat", "inv-filter-cat", "m-prod-cat", "m-prod-filter-cat"];
        catSelects.forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                const defaultLabel = (id === "m-prod-cat") ? "Select Category" : "All Categories";
                el.innerHTML = `<option value="">${defaultLabel}</option>` + 
                    allLookups.categories.map(c => `<option value="${c.category_id}">${c.category_name}</option>`).join("");
            }
        });

        // Populate Manufacturers in product master
        const mfgSelect = document.getElementById("m-prod-mfg");
        if (mfgSelect && allLookups.manufacturers) {
            const mfgOptions = ['<option value="">Select Manufacturer</option>']
                .concat(allLookups.manufacturers.map(m => `<option value="${m.manufacturer_id}">${m.manufacturer_name}</option>`))
                .concat(['<option value="__ADD_NEW__" style="font-weight: 700; color: #15803d;">➕ Add New Manufacturer...</option>']);
            mfgSelect.innerHTML = mfgOptions.join("");
        }

        // Populate Units in product master
        const unitSelect = document.getElementById("m-prod-unit");
        if (unitSelect && allLookups.units) {
            const unitOptions = allLookups.units.map(u => `<option value="${u.unit_id}">${u.unit_name} (${u.symbol})</option>`)
                .concat(['<option value="__ADD_NEW__" style="font-weight: 700; color: #15803d;">➕ Add New Unit...</option>']);
            unitSelect.innerHTML = unitOptions.join("");
            updatePackSizeOptions();
        }

        // Populate Tax Groups / GST Rates in product master
        const taxSelect = document.getElementById("m-prod-tax");
        if (taxSelect) {
            if (allLookups.tax_groups && allLookups.tax_groups.length > 0) {
                const taxOptions = ['<option value="">Select GST Rate</option>']
                    .concat(allLookups.tax_groups.map(tg => {
                        const totalRate = (Number(tg.cgst_rate || 0) + Number(tg.sgst_rate || 0)) || Number(tg.igst_rate || 0);
                        const label = (tg.tax_group_name || "").includes("%")
                            ? tg.tax_group_name
                            : `${tg.tax_group_name} (${totalRate}%)`;
                        return `<option value="${tg.tax_group_id}">${escapeHtml(label)}</option>`;
                    }));
                taxSelect.innerHTML = taxOptions.join("");
            } else {
                taxSelect.innerHTML = '<option value="">⚠️ Failed to load tax rates</option>';
            }
        }

        // Load Products, Customers, Suppliers & Expense Categories for dropdowns
        await refreshProductList();
        await refreshCustomerList();
        await refreshSupplierList();
        await loadExpenseCategoryFilter();
    } catch (err) {
        console.error("Failed to load lookups:", err);
        const taxSelect = document.getElementById("m-prod-tax");
        if (taxSelect) taxSelect.innerHTML = '<option value="">⚠️ Failed to load tax rates</option>';
    }
}

async function refreshProductList() {
    if (!hasValidAuthSession()) return;
    try {
        const res = await fetch("/api/masters/products");
        if (res.ok) {
            const data = await res.json();
            allProducts = Array.isArray(data) ? data : [];
            populateProductDropdown("pos-item-product", allProducts);
            populateProductDropdown("pur-item-product", allProducts);
            populateStockProductDropdown();
            filterProductCatalog();
            if (typeof refreshInventoryList === "function") {
                refreshInventoryList();
            }
            const multiModal = document.getElementById("modal-multi-product");
            if (multiModal && multiModal.style.display === "flex") {
                renderMultiProductList();
            }
        }
    } catch (err) {
        console.error("Error refreshing product list:", err);
    }
}

async function refreshCustomerList() {
    if (!hasValidAuthSession()) return;
    try {
        const res = await fetch("/api/masters/customers");
        if (!res.ok) return;
        const data = await res.json();
        allCustomers = Array.isArray(data) ? data : [];
        
        // Populate datalist for POS autocomplete
        const datalist = document.getElementById("pos-customer-datalist");
        if (datalist) {
            datalist.innerHTML = allCustomers.map(c => {
                const due = parseFloat(c.current_balance) || 0;
                const dueStr = due > 0 ? ` [⚠️ Due: ₹${due.toFixed(2)}]` : ' [Due: ₹0.00]';
                return `<option value="${c.customer_name}">${c.village ? c.village + ' • ' : ''}${c.mobile ? c.mobile + ' • ' : ''}${dueStr}</option>`;
            }).join("");
        }

        // Populate accounts receipt dropdown
        const accCustSelect = document.getElementById("acc-receipt-cust");
        if (accCustSelect) {
            const prevAccCust = accCustSelect.value;
            accCustSelect.innerHTML = '<option value="">Select Farmer / Customer</option>' + 
                allCustomers.map(c => `<option value="${c.customer_id}">${escapeHtml(c.customer_name)} (${escapeHtml(c.village || 'Local')}) - ₹${c.current_balance} Due</option>`).join("");
            if (typeof restorePendingCustomerReceiptDraft === "function") {
                const restored = restorePendingCustomerReceiptDraft();
                if (!restored && prevAccCust) accCustSelect.value = prevAccCust;
            } else if (prevAccCust) {
                accCustSelect.value = prevAccCust;
            }
        }

        // Populate Customer Ledger export dropdown
        const custExportSelect = document.getElementById("cust-export-customer-id");
        if (custExportSelect) {
            const curVal = custExportSelect.value;
            custExportSelect.innerHTML = '<option value="">All Customers</option>' +
                allCustomers.map(c => `<option value="${c.customer_id}">${escapeHtml(c.customer_name)}${c.village ? ' (' + escapeHtml(c.village) + ')' : ''}</option>`).join("");
            if (curVal) custExportSelect.value = curVal;
        }

        // Re-check currently typed customer in POS if any
        const custInput = document.getElementById("pos-cust-name");
        if (custInput && custInput.value.trim()) {
            const typed = custInput.value.trim();
            const matched = allCustomers.find(c => c.customer_name.toLowerCase() === typed.toLowerCase());
            if (typeof updateCustomerStatusBanner === "function") {
                updateCustomerStatusBanner(matched, typed);
            }
        }
    } catch (e) {
        console.error("Error loading customers:", e);
    }
}

async function refreshSupplierList() {
    if (!hasValidAuthSession()) return;
    const res = await fetch("/api/masters/suppliers");
    if (!res.ok) return;
    const data = await res.json();
    allSuppliers = Array.isArray(data) ? data : [];
    const suppSelect = document.getElementById("pur-supplier");
    if (suppSelect) {
        suppSelect.innerHTML = '<option value="">Select Supplier</option>' + 
            allSuppliers.map(s => `<option value="${s.supplier_id}">${s.supplier_name} (${s.city||''})</option>`).join("");
    }
    const accSuppSelect = document.getElementById("acc-pay-supp");
    if (accSuppSelect) {
        accSuppSelect.innerHTML = suppSelect.innerHTML;
    }
    const suppExportSelect = document.getElementById("supp-export-supplier-id");
    if (suppExportSelect) {
        const curVal = suppExportSelect.value;
        suppExportSelect.innerHTML = '<option value="">All Suppliers</option>' +
            allSuppliers.map(s => `<option value="${s.supplier_id}">${escapeHtml(s.supplier_name)}${s.city ? ' (' + escapeHtml(s.city) + ')' : ''}</option>`).join("");
        if (curVal) suppExportSelect.value = curVal;
    }
    const prFilterSupp = document.getElementById("pr-filter-supplier");
    if (prFilterSupp) {
        const curVal = prFilterSupp.value;
        prFilterSupp.innerHTML = '<option value="">All Suppliers</option>' +
            allSuppliers.map(s => `<option value="${s.supplier_id}">${escapeHtml(s.supplier_name)}${s.city ? ' (' + escapeHtml(s.city) + ')' : ''}</option>`).join("");
        if (curVal) prFilterSupp.value = curVal;
    }
}

async function loadExpenseCategoryFilter() {
    if (!hasValidAuthSession()) return;
    const catSelect = document.getElementById("acc-export-exp-cat");
    if (!catSelect) return;
    try {
        const res = await fetch("/api/accounting/expense-categories");
        if (!res.ok || !hasValidAuthSession()) return;
        const data = await res.json();
        const cats = Array.isArray(data) ? data : [];
        const curVal = catSelect.value;
        catSelect.innerHTML = '<option value="">All Expense Categories</option>' +
            cats.map(ec => `<option value="${ec.category_id}">${escapeHtml(ec.category_name)}</option>`).join("");
        if (curVal) catSelect.value = curVal;
    } catch (e) {
        console.warn("Could not load expense categories for export filter:", e);
    }
}

function populateProductDropdown(elementId, products) {
    const el = document.getElementById(elementId);
    if (!el) return;
    el.innerHTML = '<option value="">Select Product...</option>' + 
        products.map(p => `<option value="${p.product_id}" data-rate="${p.default_sale_rate}" data-mrp="${p.default_mrp}" data-hsn="${p.hsn_code||''}" data-cgst="${p.cgst_rate||0}" data-sgst="${p.sgst_rate||0}" data-igst="${p.igst_rate||0}" data-unit="${p.unit_symbol||''}">${p.product_name} (Stock: ${p.total_stock} ${p.unit_symbol||''})</option>`).join("");
    if (elementId === "pos-item-product") {
        const batchSelect = document.getElementById("pos-item-batch");
        if (batchSelect) {
            batchSelect.innerHTML = '<option value="">-- Select Product First --</option>';
        }
        const rateInput = document.getElementById("pos-item-rate");
        if (rateInput) rateInput.value = "";
    }
}

// ----------------- POS Billing Tab -----------------
let selectedPosFarmerId = null;

function updateCustomerStatusBanner(matched, typedText) {
    const statusBanner = document.getElementById("pos-cust-status-banner");
    const statusText = document.getElementById("pos-cust-status-text");
    const stmtBtn = document.getElementById("pos-cust-statement-btn");
    if (!statusBanner || !statusText) return;

    if (matched) {
        selectedPosFarmerId = matched.customer_id;
        const due = parseFloat(matched.current_balance) || 0;
        const safeName = escapeHtml(matched.customer_name);
        const villageTag = matched.village ? ` (${escapeHtml(matched.village)})` : "";
        statusBanner.style.display = "flex";
        if (due > 0) {
            statusBanner.className = "customer-status-banner has-due";
            statusText.innerHTML = `⚠️ <strong>${safeName}</strong>${villageTag} • Previous Due: <span style="font-size: 13.5px; text-decoration: underline; color: #dc2626; font-weight: 800;">₹${due.toFixed(2)}</span>`;
            if (stmtBtn) stmtBtn.style.display = "inline-flex";
        } else {
            statusBanner.className = "customer-status-banner no-due";
            statusText.innerHTML = `✓ <strong>${safeName}</strong>${villageTag} • Previous Due: <span style="color: #16a34a; font-weight: 800;">₹0.00 (Account Cleared)</span>`;
            if (stmtBtn) stmtBtn.style.display = "inline-flex";
        }
    } else if (typedText && typedText.length >= 2) {
        selectedPosFarmerId = null;
        statusBanner.style.display = "flex";
        statusBanner.className = "customer-status-banner new-cust";
        statusText.innerHTML = `✨ New Customer: "<strong>${escapeHtml(typedText)}</strong>" (Will be auto-registered when saving the bill)`;
        if (stmtBtn) stmtBtn.style.display = "none";
    } else {
        selectedPosFarmerId = null;
        statusBanner.style.display = "none";
        if (stmtBtn) stmtBtn.style.display = "none";
    }
}

function viewSelectedFarmerStatement() {
    if (selectedPosFarmerId && typeof openFarmerDetail === "function") {
        openFarmerDetail(selectedPosFarmerId);
    }
}

function setupCustomerAutocomplete() {
    const custInput = document.getElementById("pos-cust-name");
    const suggestionsBox = document.getElementById("pos-cust-suggestions");
    if (!custInput || !suggestionsBox) return;

    function renderSuggestions(query) {
        if (!query || query.length < 1) {
            suggestionsBox.style.display = "none";
            suggestionsBox.innerHTML = "";
            return;
        }

        const q = query.toLowerCase();
        const matches = allCustomers.filter(c => 
            (c.customer_name && c.customer_name.toLowerCase().includes(q)) ||
            (c.mobile && c.mobile.includes(q)) ||
            (c.village && c.village.toLowerCase().includes(q))
        ).slice(0, 8);

        if (matches.length === 0) {
            suggestionsBox.innerHTML = `
                <div style="padding: 10px 12px; font-size: 12px; color: #1e40af; background: #eff6ff;">
                    ✨ <strong>"${escapeHtml(query)}"</strong> is a new customer. (Will be auto-saved on billing)
                </div>
            `;
            suggestionsBox.style.display = "block";
            return;
        }

        suggestionsBox.innerHTML = matches.map(c => {
            const due = parseFloat(c.current_balance) || 0;
            const dueClass = due > 0 ? 'has-due' : 'no-due';
            const dueText = due > 0 ? `Due: ₹${due.toFixed(2)}` : 'Due: ₹0.00';
            const villageText = escapeHtml(c.village || 'Local');
            const mobileText = c.mobile ? `• 📱 ${escapeHtml(c.mobile)}` : '';
            return `
                <div class="autocomplete-item" data-id="${c.customer_id}" data-name="${escapeHtml(c.customer_name)}" data-mobile="${escapeHtml(c.mobile || '')}" data-village="${escapeHtml(c.village || '')}">
                    <div>
                        <div class="autocomplete-item-name">👨‍🌾 ${escapeHtml(c.customer_name)}</div>
                        <div class="autocomplete-item-sub">📍 ${villageText} ${mobileText}</div>
                    </div>
                    <div class="autocomplete-item-due ${dueClass}">
                        ${dueText}
                    </div>
                </div>
            `;
        }).join("");

        suggestionsBox.style.display = "block";

        suggestionsBox.querySelectorAll(".autocomplete-item").forEach(item => {
            item.addEventListener("click", () => {
                const name = item.getAttribute("data-name");
                const mobile = item.getAttribute("data-mobile");
                const village = item.getAttribute("data-village");
                const id = parseInt(item.getAttribute("data-id"));

                custInput.value = name;
                document.getElementById("pos-cust-mobile").value = mobile;
                document.getElementById("pos-cust-village").value = village;

                const matched = allCustomers.find(c => c.customer_id === id);
                updateCustomerStatusBanner(matched, name);
                suggestionsBox.style.display = "none";
            });
        });
    }

    custInput.addEventListener("input", (e) => {
        const val = e.target.value.trim();
        const matched = allCustomers.find(c => c.customer_name.toLowerCase() === val.toLowerCase());
        if (matched) {
            document.getElementById("pos-cust-mobile").value = matched.mobile || "";
            document.getElementById("pos-cust-village").value = matched.village || "";
        }
        updateCustomerStatusBanner(matched, val);
        renderSuggestions(val);
    });

    custInput.addEventListener("focus", (e) => {
        const val = e.target.value.trim();
        if (val) renderSuggestions(val);
    });

    document.addEventListener("click", (e) => {
        if (!custInput.contains(e.target) && !suggestionsBox.contains(e.target)) {
            suggestionsBox.style.display = "none";
        }
    });
}

async function initPosBilling() {
    // Set today's date
    const dateInput = document.getElementById("pos-date");
    if (dateInput) dateInput.value = new Date().toISOString().split("T")[0];

    await fetchNextInvoiceNo();
    setupCustomerAutocomplete();

    // Event listener for product change -> fetch FEFO batches
    const prodSelect = document.getElementById("pos-item-product");
    let posBatchFetchSeq = 0;
    if (prodSelect) {
        prodSelect.addEventListener("change", async () => {
            const seq = ++posBatchFetchSeq;
            const prodId = prodSelect.value;
            const batchSelect = document.getElementById("pos-item-batch");
            const rateInput = document.getElementById("pos-item-rate");
            if (!prodId) {
                if (batchSelect) batchSelect.innerHTML = '<option value="">-- Select Product First --</option>';
                if (rateInput) rateInput.value = "";
                return;
            }
            const selectedOpt = prodSelect.options[prodSelect.selectedIndex];
            if (rateInput) rateInput.value = selectedOpt?.getAttribute("data-rate") || "0";
            if (batchSelect) {
                batchSelect.innerHTML = '<option value="">Loading batches...</option>';
            }

            try {
                // Fetch batches
                const res = await fetch(`/api/inventory/batches/${prodId}`);
                if (seq !== posBatchFetchSeq || prodSelect.value !== String(prodId)) return;
                if (!res.ok) {
                    if (batchSelect) batchSelect.innerHTML = '<option value="">FAILED TO LOAD BATCHES</option>';
                    return;
                }
                const batches = await res.json();
                if (seq !== posBatchFetchSeq || prodSelect.value !== String(prodId)) return;
                if (!Array.isArray(batches) || batches.length === 0) {
                    batchSelect.innerHTML = '<option value="">NO ACTIVE STOCK</option>';
                } else {
                    batchSelect.innerHTML = batches.map((b, i) => 
                        `<option value="${b.batch_id}" data-productid="${prodId}" data-qty="${b.current_qty}" data-rate="${b.sale_rate}" data-mrp="${b.mrp}" data-exp="${b.exp_date||''}" data-batchno="${b.batch_no}">${i === 0 ? '⭐ [FEFO] ' : ''}${b.batch_no} (Exp: ${b.exp_date||'N/A'}) - Qty: ${b.current_qty}</option>`
                    ).join("");
                }
            } catch (err) {
                if (seq === posBatchFetchSeq && batchSelect) {
                    batchSelect.innerHTML = '<option value="">FAILED TO LOAD BATCHES</option>';
                }
            }
        });
    }
}

async function fetchNextInvoiceNo() {
    try {
        const res = await fetch("/api/sales/next-invoice-no");
        if (!res.ok) return;
        const data = await res.json();
        const invEl = document.getElementById("pos-inv-no");
        if (invEl && data && data.next_invoice_no) {
            invEl.value = data.next_invoice_no;
        }
    } catch (err) {
        console.error(err);
    }
}

function calculatePosLineTax(qty, rate, discPct, cgstRate, sgstRate, igstRate) {
    const gross = qty * rate;
    const discAmt = Math.round(gross * (discPct / 100.0) * 100) / 100;
    const netAfterDiscount = gross - discAmt;

    // tax_groups stores both intra-state split (cgst+sgst) and full igst on the same row.
    // Match SalesService.calculate_item_tax: charge CGST+SGST when intra-state rate > 0, else IGST.
    const intraRate = (cgstRate || 0) + (sgstRate || 0);
    const effCgstRate = intraRate > 0 ? (cgstRate || 0) : 0;
    const effSgstRate = intraRate > 0 ? (sgstRate || 0) : 0;
    const effIgstRate = intraRate > 0 ? 0 : (igstRate || 0);
    const totalTaxRate = effCgstRate + effSgstRate + effIgstRate;

    const taxableAmount = totalTaxRate > 0
        ? Math.round((netAfterDiscount / (1.0 + (totalTaxRate / 100.0))) * 100) / 100
        : Math.round(netAfterDiscount * 100) / 100;

    let cgstAmt = 0;
    let sgstAmt = 0;
    let igstAmt = 0;
    if (effIgstRate > 0) {
        igstAmt = Math.round((netAfterDiscount - taxableAmount) * 100) / 100;
    } else {
        cgstAmt = Math.round(taxableAmount * (effCgstRate / 100.0) * 100) / 100;
        sgstAmt = Math.round(taxableAmount * (effSgstRate / 100.0) * 100) / 100;
    }
    const totalLine = Math.round((taxableAmount + cgstAmt + sgstAmt + igstAmt) * 100) / 100;

    return {
        discount_amount: discAmt,
        taxable_amount: taxableAmount,
        cgst_rate: effCgstRate,
        cgst_amount: cgstAmt,
        sgst_rate: effSgstRate,
        sgst_amount: sgstAmt,
        igst_rate: effIgstRate,
        igst_amount: igstAmt,
        total_amount: totalLine
    };
}

function addPosItem() {
    const prodSelect = document.getElementById("pos-item-product");
    const batchSelect = document.getElementById("pos-item-batch");
    const qtyInput = document.getElementById("pos-item-qty");
    const rateInput = document.getElementById("pos-item-rate");
    const discInput = document.getElementById("pos-item-disc");

    const prodId = parseInt(prodSelect.value);
    const batchId = parseInt(batchSelect.value);
    const qty = parseFloat(qtyInput.value);
    const rate = parseFloat(rateInput.value);
    const disc = parseFloat(discInput.value) || 0;

    if (!prodId || isNaN(qty) || qty <= 0 || !batchId) {
        showToast("Please select a product, a valid batch, and quantity greater than 0.", "warning");
        return;
    }

    const prodOpt = prodSelect.options[prodSelect.selectedIndex];
    const batchOpt = batchSelect.options[batchSelect.selectedIndex];
    const batchProdId = parseInt(batchOpt?.getAttribute("data-productid") || "0");
    if (batchProdId && batchProdId !== prodId) {
        showToast("Batches are still loading for the selected product. Please wait a moment and try again.", "warning");
        return;
    }
    const availQty = parseFloat(batchOpt.getAttribute("data-qty"));

    if (qty > availQty) {
        showToast(`Insufficient stock in this batch! Available: ${availQty}, Requested: ${qty}`, "warning");
        return;
    }

    const cgst = parseFloat(prodOpt.getAttribute("data-cgst")) || 0;
    const sgst = parseFloat(prodOpt.getAttribute("data-sgst")) || 0;
    const igst = parseFloat(prodOpt.getAttribute("data-igst")) || 0;

    const tax = calculatePosLineTax(qty, rate, disc, cgst, sgst, igst);

    const item = {
        product_id: prodId,
        product_name: prodOpt.text.split(" (Stock:")[0],
        batch_id: batchId,
        batch_no: batchOpt.getAttribute("data-batchno"),
        exp_date: batchOpt.getAttribute("data-exp"),
        hsn_code: prodOpt.getAttribute("data-hsn"),
        unit_name: prodOpt.getAttribute("data-unit"),
        qty: qty,
        sale_rate: rate,
        mrp: parseFloat(batchOpt.getAttribute("data-mrp")) || rate,
        discount_percent: disc,
        discount_amount: tax.discount_amount,
        taxable_amount: tax.taxable_amount,
        cgst_rate: tax.cgst_rate,
        cgst_amount: tax.cgst_amount,
        sgst_rate: tax.sgst_rate,
        sgst_amount: tax.sgst_amount,
        igst_rate: tax.igst_rate,
        igst_amount: tax.igst_amount,
        total_amount: tax.total_amount
    };

    currentCart.push(item);
    renderPosCart();
    
    // Reset item inputs
    qtyInput.value = "1";
    discInput.value = "0";
}

function removePosItem(index) {
    currentCart.splice(index, 1);
    renderPosCart();
}

function renderPosCart() {
    const tbody = document.getElementById("pos-cart-tbody");
    const mobileCardsContainer = document.getElementById("pos-cart-mobile-cards");

    if (currentCart.length === 0) {
        if (tbody) tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted" style="padding: 24px;">No items added yet. Search or select a product above.</td></tr>';
        if (mobileCardsContainer) mobileCardsContainer.innerHTML = '<div class="text-center text-muted card" style="padding: 20px; font-size: 13px;">No items added yet. Search or select a product above.</div>';
        updatePosTotals(0, 0, 0, 0, 0);
        return;
    }

    let totTaxable = 0, totCgst = 0, totSgst = 0, totIgst = 0, totGross = 0;

    const tableRowsHtml = [];
    const mobileCardsHtml = [];

    currentCart.forEach((item, idx) => {
        totTaxable += item.taxable_amount;
        totCgst += item.cgst_amount;
        totSgst += item.sgst_amount;
        totIgst += (item.igst_amount || 0);
        totGross += item.total_amount;

        tableRowsHtml.push(`
            <tr>
                <td class="text-center">${idx + 1}</td>
                <td><b>${escapeHtml(item.product_name)}</b><br/><small class="text-muted">HSN: ${escapeHtml(item.hsn_code||'-')}</small></td>
                <td><span class="badge badge-fefo">${escapeHtml(item.batch_no)}</span></td>
                <td>${escapeHtml(item.exp_date||'-')}</td>
                <td class="text-center">${item.qty} ${escapeHtml(item.unit_name||'')}</td>
                <td class="text-right">₹${item.sale_rate.toFixed(2)}</td>
                <td class="text-right">₹${item.taxable_amount.toFixed(2)}</td>
                <td class="text-right"><b>₹${item.total_amount.toFixed(2)}</b></td>
                <td class="text-center"><button class="btn btn-danger btn-sm" onclick="removePosItem(${idx})">✕</button></td>
            </tr>
        `);

        mobileCardsHtml.push(`
            <div class="cart-item-card">
                <div class="cart-item-card-header">
                    <span class="cart-item-title">${idx + 1}. ${escapeHtml(item.product_name)}</span>
                    <button class="btn btn-danger btn-sm" onclick="removePosItem(${idx})" title="Remove item" style="padding: 3px 8px; font-size: 12px;">✕</button>
                </div>
                <div class="cart-item-card-meta">
                    <span class="badge badge-fefo">${escapeHtml(item.batch_no)}</span>
                    <span class="text-muted">Exp: ${escapeHtml(item.exp_date||'-')}</span>
                    <span class="text-muted">HSN: ${escapeHtml(item.hsn_code||'-')}</span>
                </div>
                <div class="cart-item-card-calc">
                    <span>${item.qty} ${escapeHtml(item.unit_name||'')} × ₹${item.sale_rate.toFixed(2)} ${item.discount_percent > 0 ? `(${item.discount_percent}% off)` : ''}</span>
                    <span class="cart-item-total">₹${item.total_amount.toFixed(2)}</span>
                </div>
            </div>
        `);
    });

    if (tbody) tbody.innerHTML = tableRowsHtml.join("");
    if (mobileCardsContainer) mobileCardsContainer.innerHTML = mobileCardsHtml.join("");

    updatePosTotals(totTaxable, totCgst, totSgst, totIgst, totGross);
}

function updatePosTotals(taxable, cgst, sgst, igstOrGross, maybeGross) {
    const igst = maybeGross !== undefined ? igstOrGross : 0;
    const gross = maybeGross !== undefined ? maybeGross : igstOrGross;
    const netRound = Math.round(gross);
    const roundOff = Math.round((netRound - gross) * 100) / 100;

    document.getElementById("pos-tot-taxable").innerText = `₹${taxable.toFixed(2)}`;
    document.getElementById("pos-tot-gst").innerText = `₹${(cgst + sgst + igst).toFixed(2)}`;
    document.getElementById("pos-tot-round").innerText = `₹${roundOff.toFixed(2)}`;
    document.getElementById("pos-grand-total").innerText = `₹${netRound.toFixed(2)}`;

    const mobTotal = document.getElementById("pos-mobile-grand-total");
    if (mobTotal) mobTotal.innerText = `₹${netRound.toFixed(2)}`;

    const paidInput = document.getElementById("pos-paid-amount");
    const payMode = document.getElementById("pos-pay-mode")?.value || "CASH";
    if (paidInput) {
        paidInput.value = payMode === "CREDIT" ? 0 : netRound;
    }
}

function handlePosPayModeChange() {
    const payMode = document.getElementById("pos-pay-mode")?.value || "CASH";
    const paidInput = document.getElementById("pos-paid-amount");
    if (!paidInput) return;
    if (payMode === "CREDIT") {
        paidInput.value = 0;
    } else {
        const gross = currentCart.reduce((acc, item) => acc + (Number(item.total_amount) || 0), 0);
        paidInput.value = Math.round(gross);
    }
}

// ----------------- Phase 2B-5C: Sales & Purchase Idempotency State -----------------
let currentPosIdempotencyKey = null;
let isSubmittingSale = false;
let currentPurchaseIdempotencyKey = null;
let isSubmittingPurchase = false;

function getOrCreatePosIdempotencyKey() {
    if (!currentPosIdempotencyKey) {
        currentPosIdempotencyKey = generateIdempotencyKey();
    }
    return currentPosIdempotencyKey;
}

function getOrCreatePurchaseIdempotencyKey() {
    if (!currentPurchaseIdempotencyKey) {
        currentPurchaseIdempotencyKey = generateIdempotencyKey();
    }
    return currentPurchaseIdempotencyKey;
}

function extractApiErrorMessage(data, fallback = "Request failed") {
    if (!data) return fallback;
    if (typeof data.detail === "string" && data.detail.trim()) {
        return data.detail.trim();
    }
    if (Array.isArray(data.detail) && data.detail.length > 0) {
        const msgs = data.detail
            .map(item => (item && typeof item === "object" && item.msg) ? String(item.msg) : String(item))
            .filter(Boolean);
        if (msgs.length > 0) return msgs.join("; ");
    }
    if (typeof data.message === "string" && data.message.trim()) {
        return data.message.trim();
    }
    return fallback;
}

function setPosSubmitButtonsState(submitting) {
    const buttons = [];
    const desktopBtn = document.getElementById("btn-pos-save-bill");
    const mobileBtn = document.getElementById("btn-pos-save-bill-mobile");
    if (desktopBtn) buttons.push(desktopBtn);
    if (mobileBtn) buttons.push(mobileBtn);
    if (buttons.length === 0 && typeof document.querySelectorAll === "function") {
        document.querySelectorAll('button[onclick="submitSalesBill()"]').forEach(b => buttons.push(b));
    }
    buttons.forEach(btn => {
        if (submitting) {
            if (!btn.dataset.origText) {
                btn.dataset.origText = btn.textContent || "";
            }
            btn.disabled = true;
            btn.textContent = "Saving...";
        } else {
            btn.disabled = false;
            if (btn.dataset.origText) {
                btn.textContent = btn.dataset.origText;
            }
        }
    });
}

function setPurchaseSubmitButtonState(submitting) {
    const btn = document.getElementById("btn-pur-save")
        || (typeof document.querySelector === "function" ? document.querySelector('button[onclick="submitPurchase()"]') : null);
    if (!btn) return;
    if (submitting) {
        if (!btn.dataset.origText) {
            btn.dataset.origText = btn.textContent || "";
        }
        btn.disabled = true;
        btn.textContent = "Saving...";
    } else {
        btn.disabled = false;
        if (btn.dataset.origText) {
            btn.textContent = btn.dataset.origText;
        }
    }
}

function showPosIdempotencyConflictBanner(serverDetail) {
    const banner = document.getElementById("pos-idempotency-conflict-banner");
    const msgEl = document.getElementById("pos-idempotency-conflict-msg");
    const msg = "A bill from this earlier attempt was already saved with different contents. Please check the Sales list before continuing."
        + (serverDetail ? ` (${serverDetail})` : "");
    if (msgEl) msgEl.textContent = msg;
    if (banner) banner.style.display = "block";
    showToast(msg, "error", 7500);
}

function hidePosIdempotencyConflictBanner() {
    const banner = document.getElementById("pos-idempotency-conflict-banner");
    if (banner) banner.style.display = "none";
}

async function continuePosAsNewBill() {
    if (isSubmittingSale) return false;
    const confirmed = await showConfirmDialog({
        title: "Continue as New Bill?",
        message: "A bill from your earlier attempt was already saved with different contents. Please verify the Sales list first so you do not create a duplicate bill.\n\nOnly confirm if you want to save the current cart as a brand-new bill.",
        confirmText: "Continue as New Bill",
        cancelText: "Cancel",
        danger: true,
    });
    if (!confirmed) return false;
    currentPosIdempotencyKey = generateIdempotencyKey();
    hidePosIdempotencyConflictBanner();
    showToast("Ready to save as a new bill. Cart contents have been preserved; press Save when ready.", "info", 5500);
    return true;
}

function showPurchaseIdempotencyConflictBanner(serverDetail) {
    const banner = document.getElementById("pur-idempotency-conflict-banner");
    const msgEl = document.getElementById("pur-idempotency-conflict-msg");
    const msg = "A purchase from this earlier attempt was already saved with different contents. Please check the Purchase list before continuing."
        + (serverDetail ? ` (${serverDetail})` : "");
    if (msgEl) msgEl.textContent = msg;
    if (banner) banner.style.display = "block";
    showToast(msg, "error", 7500);
}

function hidePurchaseIdempotencyConflictBanner() {
    const banner = document.getElementById("pur-idempotency-conflict-banner");
    if (banner) banner.style.display = "none";
}

async function continuePurchaseAsNewBill() {
    if (isSubmittingPurchase) return false;
    const confirmed = await showConfirmDialog({
        title: "Continue as New Purchase?",
        message: "A purchase from your earlier attempt was already saved with different contents. Please verify the Purchase list first so you do not create a duplicate purchase.\n\nOnly confirm if you want to save the current form as a brand-new purchase.",
        confirmText: "Continue as New Purchase",
        cancelText: "Cancel",
        danger: true,
    });
    if (!confirmed) return false;
    currentPurchaseIdempotencyKey = generateIdempotencyKey();
    hidePurchaseIdempotencyConflictBanner();
    showToast("Ready to save as a new purchase. Form contents have been preserved; press Save when ready.", "info", 5500);
    return true;
}

function isTransientHttpStatus(status) {
    return status === 408 || status === 429 || status >= 500;
}

async function submitSalesBill() {
    if (isSubmittingSale) return;

    if (currentCart.length === 0) {
        showToast("Cart is empty! Add at least one item.", "warning");
        return;
    }

    const custNameEl = document.getElementById("pos-cust-name");
    const custName = custNameEl ? custNameEl.value.trim() : "";
    if (!custName) {
        showToast("Please enter Farmer / Customer Name.", "warning");
        if (custNameEl && typeof custNameEl.focus === "function") custNameEl.focus();
        return;
    }

    const mobile = (document.getElementById("pos-cust-mobile")?.value || "").trim();
    const village = (document.getElementById("pos-cust-village")?.value || "").trim();

    // Acquire submit lock and lazy draft idempotency key BEFORE any awaited work
    isSubmittingSale = true;
    setPosSubmitButtonsState(true);
    const idempotencyKey = getOrCreatePosIdempotencyKey();

    try {
        // Resolve customer without duplicating on retry: check selectedPosFarmerId first, then case-insensitive name match
        let custId = null;
        const normalizedName = custName.toLowerCase();
        let existing = null;
        if (selectedPosFarmerId) {
            const byId = allCustomers.find(c => Number(c.customer_id) === Number(selectedPosFarmerId));
            if (byId && String(byId.customer_name || "").trim().toLowerCase() === normalizedName) {
                existing = byId;
            }
        }
        if (!existing) {
            existing = allCustomers.find(c => String(c.customer_name || "").trim().toLowerCase() === normalizedName) || null;
        }

        if (existing) {
            custId = existing.customer_id;
            selectedPosFarmerId = custId;
        } else {
            try {
                const custRes = await fetch("/api/masters/customers", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        customer_name: custName,
                        mobile: mobile,
                        village: village,
                        opening_balance: 0.0
                    })
                });
                const custData = await custRes.json().catch(() => ({}));
                if (custRes.ok && custData.success) {
                    custId = custData.customer_id;
                    selectedPosFarmerId = custId;
                    const alreadyLocal = allCustomers.some(c => Number(c.customer_id) === Number(custId));
                    if (!alreadyLocal) {
                        allCustomers.push({
                            customer_id: custId,
                            customer_name: custName,
                            mobile: mobile,
                            village: village,
                            current_balance: 0.0
                        });
                    }
                    try {
                        await refreshCustomerList();
                    } catch (refreshErr) {
                        console.warn("Customer list refresh failed after auto-create; keeping local entry:", refreshErr);
                    }
                } else {
                    showToast("Failed to auto-register farmer: " + extractApiErrorMessage(custData, "Error"), "error");
                    return;
                }
            } catch (err) {
                showToast("Error auto-saving farmer: " + err.message, "error");
                return;
            }
        }

        const saleDate = document.getElementById("pos-date")?.value || "";
        const payMode = document.getElementById("pos-pay-mode")?.value || "CASH";
        const paidAmt = parseFloat(document.getElementById("pos-paid-amount")?.value) || 0;
        const remarks = document.getElementById("pos-remarks")?.value || "";
        const crop = document.getElementById("pos-cust-crop") ? document.getElementById("pos-cust-crop").value.trim() : "";

        let totTaxable = 0, totCgst = 0, totSgst = 0, totIgst = 0, totDisc = 0, gross = 0;
        currentCart.forEach(i => {
            totTaxable += i.taxable_amount;
            totCgst += i.cgst_amount;
            totSgst += i.sgst_amount;
            totIgst += i.igst_amount;
            totDisc += i.discount_amount;
            gross += i.total_amount;
        });

        const netAmount = Math.round(gross);
        const roundOff = Math.round((netAmount - gross) * 100) / 100;

        // Send blank invoice_no so the server authoritatively assigns sequential INV-xxxxx,
        // and send idempotency_key in JSON body ONLY (never via HTTP header).
        const salePayload = {
            invoice_no: "",
            sale_date: saleDate,
            customer_id: custId,
            doctor_or_officer: crop || null,
            payment_mode: payMode,
            total_taxable: Math.round(totTaxable * 100) / 100,
            total_cgst: Math.round(totCgst * 100) / 100,
            total_sgst: Math.round(totSgst * 100) / 100,
            total_igst: Math.round(totIgst * 100) / 100,
            total_discount: Math.round(totDisc * 100) / 100,
            round_off: roundOff,
            net_amount: netAmount,
            paid_amount: paidAmt,
            due_amount: Math.max(0, netAmount - paidAmt),
            remarks: remarks,
            idempotency_key: idempotencyKey,
            items: currentCart.map(item => ({ ...item }))
        };

        const queueMeta = {
            customer_name: custName,
            customer_mobile: mobile || null,
            customer_village: village || null,
            customer_gstin: existing ? (existing.gstin || null) : null
        };

        const fallbackPreviewSale = {
            ...salePayload,
            ...queueMeta
        };

        let res = null;
        let result = {};
        try {
            res = await fetch("/api/sales/create", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(salePayload)
            });
            result = await res.json().catch(() => ({}));
        } catch (err) {
            console.error("Network error submitting POS bill:", err);
            queuePendingOfflineBill(salePayload, queueMeta);
            updatePosOfflinePendingBanner();
            showToast(
                "Could not confirm whether this bill was saved due to a connection error. Queued locally (Pending - invoice number assigned when saved) for safe retry.",
                "warning",
                7000
            );
            // Do NOT open or print an official invoice with a preview number for offline-queued bills.
            resetPosCart();
            return;
        }

        if (res.ok && result.success) {
            const confirmedInvoiceNo = String(result.invoice_no || "").trim();
            const isReplay = result.idempotent_replay === true;

            currentSavedSaleId = result.sale_id;
            saveBillToLocalBackup({ ...salePayload, invoice_no: confirmedInvoiceNo }, result.sale_id);
            hidePosIdempotencyConflictBanner();

            if (!isReplay) {
                if (!currentCompanySettings && typeof loadSettings === "function") {
                    try { await loadSettings(); } catch (e) {}
                }

                let authoritativeSale = {
                    ...fallbackPreviewSale,
                    sale_id: result.sale_id,
                    invoice_no: confirmedInvoiceNo
                };
                try {
                    const detailRes = await fetch(`/api/sales/${result.sale_id}`);
                    if (detailRes.ok) {
                        const detailData = await detailRes.json();
                        if (detailData) {
                            authoritativeSale = {
                                ...detailData,
                                invoice_no: detailData.invoice_no || confirmedInvoiceNo
                            };
                        }
                    }
                } catch (fetchErr) {
                    console.warn("Could not fetch persisted sale detail, using fallback preview:", fetchErr);
                }

                showToast(`Bill saved! Invoice ${authoritativeSale.invoice_no || confirmedInvoiceNo}`, "success");
                try {
                    openInvoicePreviewModal(authoritativeSale, result.sale_id);
                } catch (previewErr) {
                    console.error("Could not render invoice preview modal after save:", previewErr);
                }
            } else {
                // Replay: bill was already saved earlier; do not open or auto-print a second copy.
                showToast(
                    `Bill was already saved as Invoice ${confirmedInvoiceNo || ("#" + result.sale_id)}. Duplicate prevented.`,
                    "info",
                    6500
                );
            }

            resetPosCart();
            try { await refreshProductList(); } catch (e) {}
            try { await refreshCustomerList(); } catch (e) {}
            try { await fetchNextInvoiceNo(); } catch (e) {}
            try { await loadInventorySilently(); } catch (e) {}
            if (typeof notifyOtherTabsDataChanged === "function") notifyOtherTabsDataChanged();
        } else if (res.status === 409 && result.error_code === "IDEMPOTENCY_KEY_CONFLICT") {
            // Keep same key & preserve cart; require explicit confirmed action to continue as new bill.
            showPosIdempotencyConflictBanner(extractApiErrorMessage(result, ""));
        } else if (res.status === 409 && result.error_code === "DUPLICATE_INVOICE_NO") {
            // Keep same key & preserve cart; do not queue.
            showToast(extractApiErrorMessage(result, "Duplicate invoice number."), "error", 6500);
        } else if (isTransientHttpStatus(res.status)) {
            // Response outcome unknown / transient server unavailability -> queue offline with same key
            queuePendingOfflineBill(salePayload, queueMeta);
            updatePosOfflinePendingBanner();
            showToast(
                "Could not confirm whether this bill was saved. Queued locally (Pending - invoice number assigned when saved) for safe retry.",
                "warning",
                7000
            );
            resetPosCart();
        } else {
            // 400 / 422 / other 4xx: keep key and cart intact so operator can fix and retry safely
            showToast(`Error: ${extractApiErrorMessage(result, "Failed to save bill")}`, "error");
        }
    } finally {
        isSubmittingSale = false;
        setPosSubmitButtonsState(false);
    }
}

function resetPosCart() {
    currentCart = [];
    currentPosIdempotencyKey = null;
    hidePosIdempotencyConflictBanner();
    const payModeEl = document.getElementById("pos-pay-mode");
    if (payModeEl) payModeEl.value = "CASH";
    renderPosCart();
    const custNameEl = document.getElementById("pos-cust-name");
    if (custNameEl) custNameEl.value = "";
    const custMobileEl = document.getElementById("pos-cust-mobile");
    if (custMobileEl) custMobileEl.value = "";
    const custVillageEl = document.getElementById("pos-cust-village");
    if (custVillageEl) custVillageEl.value = "";
    if (document.getElementById("pos-cust-crop")) document.getElementById("pos-cust-crop").value = "";
    const remarksEl = document.getElementById("pos-remarks");
    if (remarksEl) remarksEl.value = "";

    const prodSelect = document.getElementById("pos-item-product");
    if (prodSelect) prodSelect.value = "";
    const batchSelect = document.getElementById("pos-item-batch");
    if (batchSelect) batchSelect.innerHTML = '<option value="">-- Select Product First --</option>';
    const rateInput = document.getElementById("pos-item-rate");
    if (rateInput) rateInput.value = "";
    const qtyInput = document.getElementById("pos-item-qty");
    if (qtyInput) qtyInput.value = "1";
    const discInput = document.getElementById("pos-item-disc");
    if (discInput) discInput.value = "0";

    const statusBanner = document.getElementById("pos-cust-status-banner");
    if (statusBanner) statusBanner.style.display = "none";
    const suggestionsBox = document.getElementById("pos-cust-suggestions");
    if (suggestionsBox) {
        suggestionsBox.style.display = "none";
        suggestionsBox.innerHTML = "";
    }
    selectedPosFarmerId = null;
}

// ----------------- Inward Purchase Tab -----------------
function clearPurchaseFormFieldsAfterSave() {
    const clearIds = ["pur-inv-no", "pur-batch-no", "pur-qty", "pur-rate", "pur-sale-rate", "pur-mrp", "pur-exp-date"];
    clearIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = "";
    });
    const freeQtyEl = document.getElementById("pur-free-qty");
    if (freeQtyEl) freeQtyEl.value = "0";
}

function resetPurchaseForm() {
    if (isSubmittingPurchase) return;
    currentPurchaseIdempotencyKey = null;
    hidePurchaseIdempotencyConflictBanner();
    clearPurchaseFormFieldsAfterSave();
}

async function submitPurchase() {
    if (isSubmittingPurchase) return;

    const suppId = parseInt(document.getElementById("pur-supplier")?.value);
    const invNo = (document.getElementById("pur-inv-no")?.value || "").trim();
    const purDate = document.getElementById("pur-date")?.value || "";
    const prodId = parseInt(document.getElementById("pur-item-product")?.value);
    const batchNo = (document.getElementById("pur-batch-no")?.value || "").trim();
    const expDate = document.getElementById("pur-exp-date")?.value || "";
    const qty = parseFloat(document.getElementById("pur-qty")?.value);
    const freeQty = parseFloat(document.getElementById("pur-free-qty")?.value) || 0;
    const purRate = parseFloat(document.getElementById("pur-rate")?.value);
    const saleRate = parseFloat(document.getElementById("pur-sale-rate")?.value);
    const mrp = parseFloat(document.getElementById("pur-mrp")?.value);

    if (!suppId || !invNo || !prodId || !batchNo || isNaN(qty) || qty <= 0 || isNaN(purRate)) {
        showToast("Please fill in all mandatory purchase fields.", "warning");
        return;
    }

    isSubmittingPurchase = true;
    setPurchaseSubmitButtonState(true);
    const idempotencyKey = getOrCreatePurchaseIdempotencyKey();

    const taxable = qty * purRate;
    const net = taxable; // simplified standard purchase calculation

    const purchasePayload = {
        invoice_no: invNo,
        purchase_date: purDate,
        supplier_id: suppId,
        payment_type: "CREDIT",
        total_taxable: taxable,
        net_amount: net,
        paid_amount: 0.0,
        due_amount: net,
        idempotency_key: idempotencyKey,
        items: [
            {
                product_id: prodId,
                batch_no: batchNo,
                exp_date: expDate,
                qty: qty,
                free_qty: freeQty,
                purchase_rate: purRate,
                sale_rate: saleRate,
                mrp: mrp,
                taxable_amount: taxable,
                total_amount: net
            }
        ]
    };

    const safeRetryMsg = "Could not confirm whether this purchase was saved. It is safe to press Save again; the same request will not be saved twice. You can also check the Purchase list.";

    try {
        const res = await fetch("/api/purchase/create", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(purchasePayload)
        });
        const data = await res.json().catch(() => ({}));

        if (res.ok && data.success !== false) {
            const savedInvNo = String(data.invoice_no || invNo).trim();
            const isReplay = data.idempotent_replay === true;

            hidePurchaseIdempotencyConflictBanner();
            currentPurchaseIdempotencyKey = null;
            clearPurchaseFormFieldsAfterSave();

            if (isReplay) {
                showToast(
                    `Purchase was already saved (Invoice ${savedInvNo}, Purchase #${data.purchase_id || ""}). Duplicate prevented.`,
                    "info",
                    6500
                );
            } else {
                showToast(
                    `Inward Purchase recorded! Invoice ${savedInvNo} (Batch ${batchNo}) added to stock.`,
                    "success"
                );
            }
            await refreshProductList();
        } else if (res.status === 409 && data.error_code === "IDEMPOTENCY_KEY_CONFLICT") {
            // Keep key and form intact; require explicit confirmed action to continue as new purchase.
            showPurchaseIdempotencyConflictBanner(extractApiErrorMessage(data, ""));
        } else if (isTransientHttpStatus(res.status)) {
            // Do not queue purchases; keep key and form contents for safe manual retry.
            showToast(safeRetryMsg, "warning", 7500);
        } else {
            // 422 / 400: show server message, keep form and key intact.
            showToast("Error: " + extractApiErrorMessage(data, "Could not record purchase."), "error");
        }
    } catch (err) {
        console.error("Network error submitting purchase:", err);
        showToast(safeRetryMsg, "warning", 7500);
    } finally {
        isSubmittingPurchase = false;
        setPurchaseSubmitButtonState(false);
    }
}

// ----------------- Purchase Returns Module (Step 2E-A: Navigation & List) -----------------
let allPurchaseReturns = [];
let purchaseReturnsCurrentPage = 1;
const purchaseReturnsPageSize = 25;
let prSearchDebounceTimer = null;
let isLoadingPurchaseReturns = false;

function canAccessPurchaseReturns() {
    if (!currentUser) return false;
    const role = (currentUser.role || "").toUpperCase();
    return ["ADMIN", "OWNER", "MANAGER", "ACCOUNTANT"].includes(role);
}

function navigateToTab(targetId) {
    if (!targetId) return;
    const tabEl = document.querySelector(`.nav-tab[data-tab="${targetId}"], .mobile-drawer-item[data-tab="${targetId}"]`);
    if (tabEl) {
        tabEl.click();
    } else {
        document.querySelectorAll(".nav-tab, .mobile-drawer-item, .mobile-bottom-nav-item").forEach(t => {
            if (t.getAttribute("data-tab") === targetId) {
                t.classList.add("active");
            } else {
                t.classList.remove("active");
            }
        });
        document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));
        const targetPane = document.getElementById(targetId);
        if (targetPane) targetPane.classList.add("active");
        if (typeof closeMobileMenu === "function") closeMobileMenu();
    }
}

function formatSettlementType(type) {
    if (!type) return "-";
    switch (type.toUpperCase()) {
        case "PAYABLE_REDUCTION": return "Payable Reduction";
        case "REFUND_CASH": return "Cash Refund";
        case "REFUND_BANK": return "Bank Refund";
        case "SUPPLIER_CREDIT": return "Supplier Credit";
        default: return type.replace(/_/g, " ");
    }
}

async function loadPurchaseReturns() {
    if (!hasValidAuthSession()) return;
    if (!canAccessPurchaseReturns()) {
        showToast("Access restricted: Purchase Returns require Admin, Owner, Manager, or Accountant permissions.", "error");
        return;
    }

    const tbody = document.getElementById("pr-table-tbody");
    if (!tbody) return;

    isLoadingPurchaseReturns = true;
    tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted" style="padding: 24px;">⏳ Loading purchase returns...</td></tr>';

    try {
        const params = new URLSearchParams();

        const fromDate = document.getElementById("pr-filter-from-date")?.value;
        const toDate = document.getElementById("pr-filter-to-date")?.value;
        const supplierId = document.getElementById("pr-filter-supplier")?.value;
        const status = document.getElementById("pr-filter-status")?.value;

        if (fromDate) params.append("from_date", fromDate);
        if (toDate) params.append("to_date", toDate);
        if (supplierId) params.append("supplier_id", supplierId);
        if (status && status !== "ALL") params.append("status", status);

        const url = `/api/purchase/returns${params.toString() ? '?' + params.toString() : ''}`;
        const res = await fetch(url);

        if (!res.ok) {
            let errorMsg = "Failed to load purchase returns.";
            try {
                const errData = await res.json();
                if (errData && errData.detail) errorMsg = errData.detail;
            } catch (_) {}
            tbody.innerHTML = `<tr><td colspan="9" class="text-center text-danger" style="padding: 24px;">
                ⚠️ ${escapeHtml(errorMsg)}
                <div style="margin-top: 8px;">
                    <button type="button" class="btn btn-secondary btn-sm" onclick="loadPurchaseReturns()">🔄 Retry</button>
                </div>
            </td></tr>`;
            showToast(errorMsg, "error");
            return;
        }

        const data = await res.json();
        allPurchaseReturns = Array.isArray(data) ? data : [];
        purchaseReturnsCurrentPage = 1;
        renderPurchaseReturnsTable();
    } catch (err) {
        console.error("Error loading purchase returns:", err);
        tbody.innerHTML = `<tr><td colspan="9" class="text-center text-danger" style="padding: 24px;">
            ⚠️ Network or server error loading purchase returns: ${escapeHtml(err.message || String(err))}
            <div style="margin-top: 8px;">
                <button type="button" class="btn btn-secondary btn-sm" onclick="loadPurchaseReturns()">🔄 Retry</button>
            </div>
        </td></tr>`;
        showToast("Error loading purchase returns: " + (err.message || err), "error");
    } finally {
        isLoadingPurchaseReturns = false;
    }
}

function renderPurchaseReturnsTable() {
    const tbody = document.getElementById("pr-table-tbody");
    if (!tbody) return;

    const searchTerm = (document.getElementById("pr-filter-search")?.value || "").trim().toLowerCase();

    // Client-side text search filtering
    const filtered = allPurchaseReturns.filter(ret => {
        if (!searchTerm) return true;
        const returnNo = (ret.return_no || "").toLowerCase();
        const supplierName = (ret.supplier_name || "").toLowerCase();
        const invoiceNo = (ret.purchase_invoice_no || "").toLowerCase();
        const voucherRef = String(ret.voucher_id || "");
        const remarks = (ret.remarks || "").toLowerCase();
        return returnNo.includes(searchTerm) || 
               supplierName.includes(searchTerm) || 
               invoiceNo.includes(searchTerm) || 
               voucherRef.includes(searchTerm) || 
               remarks.includes(searchTerm);
    });

    const totalCount = filtered.length;
    const totalPages = Math.max(1, Math.ceil(totalCount / purchaseReturnsPageSize));

    if (purchaseReturnsCurrentPage > totalPages) purchaseReturnsCurrentPage = totalPages;
    if (purchaseReturnsCurrentPage < 1) purchaseReturnsCurrentPage = 1;

    const startIndex = (purchaseReturnsCurrentPage - 1) * purchaseReturnsPageSize;
    const endIndex = Math.min(startIndex + purchaseReturnsPageSize, totalCount);
    const pageItems = filtered.slice(startIndex, endIndex);

    // Update pagination controls
    const pageInfoEl = document.getElementById("pr-page-info");
    const prevBtn = document.getElementById("pr-btn-prev");
    const nextBtn = document.getElementById("pr-btn-next");

    if (pageInfoEl) {
        if (totalCount === 0) {
            pageInfoEl.textContent = "Showing 0 returns";
        } else {
            pageInfoEl.textContent = `Showing ${startIndex + 1}–${endIndex} of ${totalCount} returns | Page ${purchaseReturnsCurrentPage} of ${totalPages}`;
        }
    }
    if (prevBtn) prevBtn.disabled = (purchaseReturnsCurrentPage <= 1);
    if (nextBtn) nextBtn.disabled = (purchaseReturnsCurrentPage >= totalPages || totalCount === 0);

    // Empty state
    if (pageItems.length === 0) {
        const hasFilters = searchTerm || 
            (document.getElementById("pr-filter-supplier")?.value) || 
            (document.getElementById("pr-filter-status")?.value) || 
            (document.getElementById("pr-filter-from-date")?.value) || 
            (document.getElementById("pr-filter-to-date")?.value);

        if (hasFilters) {
            tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted" style="padding: 28px;">No purchase returns match your search/filter criteria. <button type="button" class="btn btn-outline-secondary btn-sm" onclick="resetPurchaseReturnFilters()" style="margin-left: 8px;">Reset Filters</button></td></tr>';
        } else {
            tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted" style="padding: 28px;">No purchase returns recorded yet.</td></tr>';
        }
        return;
    }

    // Build rows
    tbody.innerHTML = pageItems.map(ret => {
        const returnNo = escapeHtml(ret.return_no || `#${ret.return_id}`);
        const returnDate = escapeHtml(ret.return_date || '-');
        const purchaseInv = escapeHtml(ret.purchase_invoice_no || `Inv #${ret.purchase_id}`);
        const supplierName = escapeHtml(ret.supplier_name || `Supplier #${ret.supplier_id}`);
        const returnAmount = Number(ret.net_amount || 0).toFixed(2);

        // Settlement summary
        let settlementSummaryHtml = '';
        if (Array.isArray(ret.settlements) && ret.settlements.length > 0) {
            settlementSummaryHtml = ret.settlements.map(s => {
                const label = formatSettlementType(s.settlement_type);
                const amt = Number(s.amount || 0).toFixed(2);
                return `<span class="badge-settlement" title="${escapeHtml(s.settlement_type)}${s.reference_no ? ' Ref: ' + escapeHtml(s.reference_no) : ''}">${escapeHtml(label)}: ₹${amt}</span>`;
            }).join(' ');
        } else if (ret.settlement_type) {
            settlementSummaryHtml = `<span class="badge-settlement">${escapeHtml(formatSettlementType(ret.settlement_type))}</span>`;
        } else {
            settlementSummaryHtml = '<span class="text-muted">-</span>';
        }

        // Status badge
        let statusBadgeHtml = '';
        const status = (ret.status || 'POSTED').toUpperCase();
        if (status === 'POSTED') {
            statusBadgeHtml = '<span class="badge badge-success">POSTED</span>';
        } else if (status === 'CANCELLED') {
            statusBadgeHtml = '<span class="badge badge-danger">CANCELLED</span>';
        } else {
            statusBadgeHtml = `<span class="badge">${escapeHtml(status)}</span>`;
        }

        // Voucher reference
        const voucherHtml = ret.voucher_id
            ? `<span style="font-weight: 600; color: #1e40af;">#${escapeHtml(String(ret.voucher_id))}</span>`
            : '<span class="text-muted">-</span>';

        return `
            <tr>
                <td><strong style="color: var(--primary);">${returnNo}</strong></td>
                <td>${returnDate}</td>
                <td><span>${purchaseInv}</span></td>
                <td><b>${supplierName}</b></td>
                <td class="text-right"><strong style="color: #b91c1c;">₹${returnAmount}</strong></td>
                <td>${settlementSummaryHtml}</td>
                <td class="text-center">${statusBadgeHtml}</td>
                <td class="text-center">${voucherHtml}</td>
                <td class="text-center">
                    <button type="button" class="btn btn-secondary btn-sm" onclick="openPurchaseReturnDetail(${ret.return_id})" title="View Purchase Return Details">
                        👁️ View
                    </button>
                </td>
            </tr>
        `;
    }).join("");
}

function applyPurchaseReturnFilters() {
    purchaseReturnsCurrentPage = 1;
    loadPurchaseReturns();
}

function resetPurchaseReturnFilters() {
    const searchInput = document.getElementById("pr-filter-search");
    const suppSelect = document.getElementById("pr-filter-supplier");
    const statusSelect = document.getElementById("pr-filter-status");
    const fromDate = document.getElementById("pr-filter-from-date");
    const toDate = document.getElementById("pr-filter-to-date");

    if (searchInput) searchInput.value = "";
    if (suppSelect) suppSelect.value = "";
    if (statusSelect) statusSelect.value = "";
    if (fromDate) fromDate.value = "";
    if (toDate) toDate.value = "";

    purchaseReturnsCurrentPage = 1;
    loadPurchaseReturns();
}

function changePurchaseReturnsPage(delta) {
    purchaseReturnsCurrentPage += delta;
    renderPurchaseReturnsTable();
}

function debouncePurchaseReturnSearch() {
    if (prSearchDebounceTimer) clearTimeout(prSearchDebounceTimer);
    prSearchDebounceTimer = setTimeout(() => {
        purchaseReturnsCurrentPage = 1;
        renderPurchaseReturnsTable();
    }, 250);
}

// ----------------- Step 2E-B: Purchase Return Creation Workflow -----------------
let allPurchasesForReturn = [];
let currentEligiblePurchaseLines = [];
let currentSelectedReturnItems = {}; // { [itemId]: { qty: number, reason: string } }
let currentReturnPreview = null; // Stores authoritative backend preview response
let isPreviewLoading = false;
let previewErrorMessage = null;
let previewDebounceTimer = null;
let previewRequestSeq = 0;
let currentSettlements = []; // [ { id: 1, type: "PAYABLE_REDUCTION", amount: 0, ref: "" } ]
let nextSettlementRowId = 2;
let currentSupplierPayableBalance = 0;
let currentSelectedPurchase = null;
let currentPurchaseReturnIdempotencyKey = null;
let isSubmittingPurchaseReturn = false;

function generateIdempotencyKey() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
    }
    if (window.crypto && typeof window.crypto.getRandomValues === "function") {
        const bytes = new Uint8Array(16);
        window.crypto.getRandomValues(bytes);
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        const hex = Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
        return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
    }
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function(c) {
        const r = (Math.random() * 16) | 0;
        const v = c === "x" ? r : (r & 0x3) | 0x8;
        return v.toString(16);
    });
}

async function openCreatePurchaseReturnModal(preselectedPurchaseId) {
    if (!hasValidAuthSession()) return;
    if (!canAccessPurchaseReturns()) {
        showToast("Access restricted: Purchase Returns require Admin, Owner, Manager, or Accountant permissions.", "error");
        return;
    }

    const modal = document.getElementById("modal-create-purchase-return");
    if (!modal) return;
    modal.style.display = "flex";

    // 1. Reset state
    currentEligiblePurchaseLines = [];
    currentSelectedReturnItems = {};
    currentReturnPreview = null;
    isPreviewLoading = false;
    previewErrorMessage = null;
    if (previewDebounceTimer) {
        clearTimeout(previewDebounceTimer);
        previewDebounceTimer = null;
    }
    currentSettlements = [{ id: 1, type: "PAYABLE_REDUCTION", amount: 0.00, ref: "" }];
    nextSettlementRowId = 2;
    currentSupplierPayableBalance = 0;
    currentSelectedPurchase = null;
    currentPurchaseReturnIdempotencyKey = generateIdempotencyKey();
    isSubmittingPurchaseReturn = false;

    // 2. Reset form elements
    const returnDateInput = document.getElementById("pr-create-return-date");
    if (returnDateInput) returnDateInput.value = new Date().toISOString().split("T")[0];

    const scnNoInput = document.getElementById("pr-create-scn-no");
    if (scnNoInput) scnNoInput.value = "";

    const scnDateInput = document.getElementById("pr-create-scn-date");
    if (scnDateInput) scnDateInput.value = "";

    const remarksInput = document.getElementById("pr-create-remarks");
    if (remarksInput) remarksInput.value = "";

    const searchInput = document.getElementById("pr-create-purchase-search");
    if (searchInput) searchInput.value = "";

    const prIdemBanner = document.getElementById("pr-idem-conflict-banner");
    if (prIdemBanner) prIdemBanner.style.display = "none";

    const itcSelect = document.getElementById("pr-create-itc-status");
    if (itcSelect) itcSelect.value = "";
    onItcStatusChange();

    const infoBar = document.getElementById("pr-selected-purchase-info");
    if (infoBar) infoBar.style.display = "none";

    const warningEl = document.getElementById("pr-items-warning");
    if (warningEl) {
        warningEl.style.display = "none";
        warningEl.textContent = "";
    }

    const itemsTbody = document.getElementById("pr-items-tbody");
    if (itemsTbody) {
        itemsTbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted" style="padding: 16px;">⏳ Loading inward purchases...</td></tr>';
    }

    updateCalculationsPreviewDisplay();
    renderSettlementRows();

    const submitBtn = document.getElementById("btn-submit-purchase-return");
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "✓ Confirm & Post Purchase Return";
    }

    // 3. Load purchase invoices for selection
    try {
        const res = await fetch("/api/purchase/list");
        if (!res.ok) {
            showToast("Failed to load purchase invoices list.", "error");
            if (itemsTbody) itemsTbody.innerHTML = '<tr><td colspan="10" class="text-center text-danger" style="padding: 16px;">Failed to load purchase invoices.</td></tr>';
            return;
        }

        const data = await res.json();
        allPurchasesForReturn = Array.isArray(data) ? data : [];

        renderPurchaseDropdownOptions(allPurchasesForReturn, preselectedPurchaseId);
        if (preselectedPurchaseId) {
            onPurchaseSelectedForReturn();
        } else if (itemsTbody) {
            itemsTbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted" style="padding: 16px;">Please select an inward purchase invoice above to load eligible items.</td></tr>';
        }
    } catch (err) {
        console.error("Error loading purchases for return modal:", err);
        showToast("Error loading purchase invoices: " + (err.message || err), "error");
    }
}

function renderPurchaseDropdownOptions(purchasesList, preserveValue = null) {
    const selectEl = document.getElementById("pr-create-purchase-select");
    if (!selectEl) return;
    const currentVal = preserveValue !== null && preserveValue !== undefined
        ? String(preserveValue)
        : selectEl.value;
    selectEl.innerHTML = '<option value="">-- Choose Purchase Invoice --</option>' +
        (purchasesList || []).map(p => {
            const invNo = escapeHtml(p.invoice_no || `ID #${p.purchase_id}`);
            const purDate = escapeHtml(p.purchase_date || '-');
            const suppName = escapeHtml(p.supplier_name || `Supplier #${p.supplier_id}`);
            const amt = Number(p.net_amount || p.total_amount || 0).toFixed(2);
            return `<option value="${p.purchase_id}">${invNo} (${purDate}) — ${suppName} (₹${amt})</option>`;
        }).join("");
    if (currentVal && (purchasesList || []).some(p => String(p.purchase_id) === String(currentVal))) {
        selectEl.value = String(currentVal);
    }
}

function filterPurchasesForReturnDropdown() {
    const q = (document.getElementById("pr-create-purchase-search")?.value || "").trim().toLowerCase();
    if (!q) {
        renderPurchaseDropdownOptions(allPurchasesForReturn);
        return;
    }
    const filtered = allPurchasesForReturn.filter(p => {
        const hay = `${p.invoice_no || ''} ${p.supplier_name || ''} ${p.purchase_date || ''} ${p.purchase_id || ''}`.toLowerCase();
        return hay.includes(q);
    });
    renderPurchaseDropdownOptions(filtered);
}

function closeCreatePurchaseReturnModal() {
    const modal = document.getElementById("modal-create-purchase-return");
    if (modal) modal.style.display = "none";
    if (previewDebounceTimer) {
        clearTimeout(previewDebounceTimer);
        previewDebounceTimer = null;
    }
}

async function onPurchaseSelectedForReturn() {
    const selectEl = document.getElementById("pr-create-purchase-select");
    const purchaseId = selectEl ? selectEl.value : "";

    const infoBar = document.getElementById("pr-selected-purchase-info");
    const itemsTbody = document.getElementById("pr-items-tbody");
    const warningEl = document.getElementById("pr-items-warning");

    currentSelectedReturnItems = {};
    currentEligiblePurchaseLines = [];
    currentSelectedPurchase = null;
    currentSupplierPayableBalance = 0;
    invalidateReturnPreview();

    if (!purchaseId) {
        if (infoBar) infoBar.style.display = "none";
        if (warningEl) warningEl.style.display = "none";
        if (itemsTbody) {
            itemsTbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted" style="padding: 16px;">Please select an inward purchase invoice above to load eligible items.</td></tr>';
        }
        updateCalculationsPreviewDisplay();
        updateSettlementReconciliation();
        return;
    }

    currentSelectedPurchase = allPurchasesForReturn.find(p => String(p.purchase_id) === String(purchaseId)) || null;

    if (itemsTbody) {
        itemsTbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted" style="padding: 16px;">⏳ Loading eligible return lines...</td></tr>';
    }

    try {
        const [linesRes, suppRes] = await Promise.all([
            fetch(`/api/purchase/${purchaseId}/eligible-returns`),
            fetch(`/api/accounting/suppliers`)
        ]);

        if (!linesRes.ok) {
            let errorMsg = "Could not load eligible return lines for selected purchase.";
            try {
                const errData = await linesRes.json();
                if (errData && errData.detail) errorMsg = errData.detail;
            } catch (_) {}
            if (itemsTbody) {
                itemsTbody.innerHTML = `<tr><td colspan="10" class="text-center text-danger" style="padding: 16px;">⚠️ ${escapeHtml(errorMsg)}</td></tr>`;
            }
            showToast(errorMsg, "error");
            return;
        }

        const linesData = await linesRes.json();
        currentEligiblePurchaseLines = Array.isArray(linesData) ? linesData : [];

        if (suppRes.ok && currentSelectedPurchase) {
            const suppliers = await suppRes.json();
            if (Array.isArray(suppliers)) {
                const s = suppliers.find(item => item.supplier_id === currentSelectedPurchase.supplier_id);
                if (s) {
                    currentSupplierPayableBalance = Math.max(0, parseFloat(s.outstanding_payable || 0));
                }
            }
        }

        if (infoBar && currentSelectedPurchase) {
            document.getElementById("pr-info-supplier-name").textContent = currentSelectedPurchase.supplier_name || "-";
            document.getElementById("pr-info-supplier-gstin").textContent = currentSelectedPurchase.supplier_gstin || "Unregistered";
            document.getElementById("pr-info-inv-date").textContent = currentSelectedPurchase.purchase_date || "-";
            document.getElementById("pr-info-inv-total").textContent = `₹${Number(currentSelectedPurchase.net_amount || currentSelectedPurchase.total_amount || 0).toFixed(2)}`;
            document.getElementById("pr-info-supplier-balance").textContent = `₹${currentSupplierPayableBalance.toFixed(2)}`;
            infoBar.style.display = "block";
        }

        renderEligiblePurchaseLinesTable();
        updateCalculationsPreviewDisplay();
        updateSettlementReconciliation();
    } catch (err) {
        console.error("Error loading eligible return lines:", err);
        if (itemsTbody) {
            itemsTbody.innerHTML = `<tr><td colspan="10" class="text-center text-danger" style="padding: 16px;">⚠️ Error loading eligible items: ${escapeHtml(err.message || String(err))}</td></tr>`;
        }
        showToast("Error loading eligible items: " + (err.message || err), "error");
    }
}

function renderEligiblePurchaseLinesTable() {
    const itemsTbody = document.getElementById("pr-items-tbody");
    const warningEl = document.getElementById("pr-items-warning");
    if (!itemsTbody) return;

    if (currentEligiblePurchaseLines.length === 0) {
        itemsTbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted" style="padding: 18px;">No purchase line items found for this invoice.</td></tr>';
        return;
    }

    let hasFreeQuantityLines = false;

    itemsTbody.innerHTML = currentEligiblePurchaseLines.map((line) => {
        const itemId = line.item_id;
        const prodName = escapeHtml(line.product_name || `Product #${line.product_id}`);
        const hsnText = line.hsn_code ? `<br><small style="color: #64748b;">HSN: ${escapeHtml(line.hsn_code)}</small>` : '';
        const batchNo = escapeHtml(line.batch_no || '-');
        const expText = line.exp_date ? `<br><small style="color: #64748b;">Exp: ${escapeHtml(line.exp_date)}</small>` : '';
        const purchasedQty = parseFloat(line.purchased_qty || 0);
        const freeQty = parseFloat(line.free_qty || 0);
        const priorReturned = parseFloat(line.prior_returned_qty || 0);
        const remainingReturnable = parseFloat(line.remaining_purchased_qty || 0);
        const stockQty = parseFloat(line.current_stock_qty || 0);
        const maxReturnable = parseFloat(line.max_returnable_qty || 0);
        const purchaseRate = Number(line.purchase_rate || 0).toFixed(2);

        const gstRate = (parseFloat(line.cgst_rate || 0) + parseFloat(line.sgst_rate || 0)) || parseFloat(line.igst_rate || 0);
        const gstBadge = gstRate > 0 ? `<br><small style="color: #475569;">GST: ${gstRate}%</small>` : '';

        const isFreeLine = freeQty > 0;
        if (isFreeLine) hasFreeQuantityLines = true;

        const isNoStock = !isFreeLine && maxReturnable <= 0;
        const isRowDisabled = isFreeLine || isNoStock;

        let statusHint = '';
        if (isFreeLine) {
            statusHint = `<span class="badge" style="background: #fef3c7; color: #92400e; font-size: 10.5px; display: block; margin-top: 3px;" title="Returns on lines with bonus/free quantities are disabled">⚠️ Free Qty (${freeQty}) — Return Disabled</span>`;
        } else if (remainingReturnable <= 0) {
            statusHint = `<span class="badge" style="background: #e2e8f0; color: #475569; font-size: 10.5px; display: block; margin-top: 3px;">Fully Returned</span>`;
        } else if (stockQty <= 0) {
            statusHint = `<span class="badge" style="background: #fee2e2; color: #991b1b; font-size: 10.5px; display: block; margin-top: 3px;">Out of Stock</span>`;
        }

        return `
            <tr style="${isRowDisabled ? 'background: #f8fafc; opacity: 0.72;' : ''}">
                <td style="text-align: center;">
                    <input type="checkbox" id="pr-chk-${itemId}" ${isRowDisabled ? 'disabled' : ''} onchange="onReturnItemCheckToggle(${itemId})">
                </td>
                <td>
                    <b>${prodName}</b>${hsnText}
                    ${statusHint}
                </td>
                <td><b>${batchNo}</b>${expText}</td>
                <td class="text-right">${purchasedQty}${freeQty > 0 ? ' <small style="color: #d97706;">(+' + freeQty + ')</small>' : ''}</td>
                <td class="text-right">${priorReturned}</td>
                <td class="text-right"><b>${remainingReturnable}</b></td>
                <td class="text-right"><span style="color: ${stockQty < remainingReturnable ? '#b91c1c' : '#15803d'}; font-weight: 600;">${stockQty}</span></td>
                <td class="text-right">₹${purchaseRate}${gstBadge}</td>
                <td>
                    <input type="number" id="pr-qty-${itemId}" min="0.0001" max="${maxReturnable}" step="any" disabled
                           oninput="onReturnItemQtyChange(${itemId})" placeholder="0"
                           style="width: 90px; padding: 4px 6px; font-size: 12px; text-align: right; font-weight: 600;">
                </td>
                <td>
                    <input type="text" id="pr-reason-${itemId}" list="return-reason-presets" disabled
                           oninput="onReturnItemReasonChange(${itemId})" placeholder="Select or type reason"
                           style="width: 130px; padding: 4px 6px; font-size: 11.5px;">
                </td>
            </tr>
        `;
    }).join("");

    if (warningEl) {
        if (hasFreeQuantityLines) {
            warningEl.style.display = "block";
            warningEl.innerHTML = "ℹ️ <b>Bonus / Free Quantity Notice:</b> Lines containing bonus/free quantities (<code>free_qty &gt; 0</code>) are protected and cannot be returned in this release.";
        } else {
            warningEl.style.display = "none";
            warningEl.textContent = "";
        }
    }
}

function onReturnItemCheckToggle(itemId) {
    const chk = document.getElementById(`pr-chk-${itemId}`);
    const qtyInput = document.getElementById(`pr-qty-${itemId}`);
    const reasonInput = document.getElementById(`pr-reason-${itemId}`);
    const line = currentEligiblePurchaseLines.find(l => l.item_id === itemId);

    if (!chk || !qtyInput || !line) return;

    if (chk.checked) {
        qtyInput.disabled = false;
        if (reasonInput) reasonInput.disabled = false;

        const defaultQty = line.max_returnable_qty > 0 ? line.max_returnable_qty : 1;
        qtyInput.value = defaultQty;
        currentSelectedReturnItems[itemId] = {
            qty: parseFloat(defaultQty),
            reason: reasonInput ? reasonInput.value.trim() : ""
        };
    } else {
        qtyInput.disabled = true;
        qtyInput.value = "";
        qtyInput.style.borderColor = "";
        if (reasonInput) {
            reasonInput.disabled = true;
            reasonInput.value = "";
        }
        delete currentSelectedReturnItems[itemId];
    }

    invalidateReturnPreview();
    scheduleBackendReturnPreview();
}

function onReturnItemQtyChange(itemId) {
    const qtyInput = document.getElementById(`pr-qty-${itemId}`);
    const line = currentEligiblePurchaseLines.find(l => l.item_id === itemId);
    if (!qtyInput || !line) return;

    const val = parseFloat(qtyInput.value);
    const maxVal = parseFloat(line.max_returnable_qty);

    if (isNaN(val) || val <= 0 || val > maxVal) {
        qtyInput.style.borderColor = "#ef4444";
        showToast(`Return quantity for ${line.product_name} must be > 0 and ≤ ${maxVal}.`, "warning");
        if (currentSelectedReturnItems[itemId]) {
            currentSelectedReturnItems[itemId].qty = 0;
        }
    } else {
        qtyInput.style.borderColor = "";
        if (!currentSelectedReturnItems[itemId]) {
            currentSelectedReturnItems[itemId] = { qty: val, reason: "" };
        } else {
            currentSelectedReturnItems[itemId].qty = val;
        }
    }

    invalidateReturnPreview();
    scheduleBackendReturnPreview();
}

function onReturnItemReasonChange(itemId) {
    const reasonInput = document.getElementById(`pr-reason-${itemId}`);
    if (reasonInput && currentSelectedReturnItems[itemId]) {
        currentSelectedReturnItems[itemId].reason = reasonInput.value.trim();
    }
}

// ----------------- Authoritative Backend Return Preview -----------------
function invalidateReturnPreview() {
    previewRequestSeq++;
    currentReturnPreview = null;
    previewErrorMessage = null;
    isPreviewLoading = false;
    if (previewDebounceTimer) {
        clearTimeout(previewDebounceTimer);
        previewDebounceTimer = null;
    }
    updateCalculationsPreviewDisplay();
    updateSettlementReconciliation();
}

function scheduleBackendReturnPreview() {
    if (previewDebounceTimer) clearTimeout(previewDebounceTimer);
    previewDebounceTimer = setTimeout(() => {
        requestBackendReturnPreview();
    }, 200);
}

function recomputeReturnCalculations() {
    // Authoritative calculations are performed on backend via /api/purchase/returns/preview
    scheduleBackendReturnPreview();
}

async function requestBackendReturnPreview() {
    if (!currentSelectedPurchase) {
        invalidateReturnPreview();
        return;
    }

    const selectedEntries = Object.entries(currentSelectedReturnItems).filter(([_, data]) => parseFloat(data.qty) > 0);
    if (selectedEntries.length === 0) {
        invalidateReturnPreview();
        return;
    }

    const previewItems = selectedEntries.map(([itemIdStr, data]) => {
        const itemId = parseInt(itemIdStr);
        const line = currentEligiblePurchaseLines.find(l => l.item_id === itemId);
        return {
            purchase_item_id: itemId,
            qty: parseFloat(parseFloat(data.qty).toFixed(4)),
            product_id: line ? parseInt(line.product_id) : undefined,
            batch_id: line ? (line.batch_id || undefined) : undefined,
            reason: data.reason ? data.reason.trim() : null
        };
    });

    const mySeq = ++previewRequestSeq;
    isPreviewLoading = true;
    previewErrorMessage = null;
    updateCalculationsPreviewDisplay();
    updateSettlementReconciliation();

    try {
        const res = await fetch("/api/purchase/returns/preview", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                purchase_id: parseInt(currentSelectedPurchase.purchase_id),
                supplier_id: parseInt(currentSelectedPurchase.supplier_id),
                items: previewItems
            })
        });

        if (mySeq !== previewRequestSeq) return;

        if (!res.ok) {
            let errorDetail = "Backend preview calculation failed.";
            try {
                const errData = await res.json();
                if (errData && errData.detail) errorDetail = errData.detail;
            } catch (_) {}
            throw new Error(errorDetail);
        }

        const data = await res.json();
        if (mySeq !== previewRequestSeq) return;

        currentReturnPreview = data;
        previewErrorMessage = null;

        if (data.supplier_balance !== undefined) {
            currentSupplierPayableBalance = Math.max(0, parseFloat(data.supplier_balance));
            const suppBalEl = document.getElementById("pr-info-supplier-balance");
            if (suppBalEl) suppBalEl.textContent = `₹${currentSupplierPayableBalance.toFixed(2)}`;
        }

        // Auto-fill single settlement row with exact authoritative net amount
        if (currentSettlements.length === 1) {
            currentSettlements[0].amount = parseFloat(data.net_amount || 0);
            const firstAmtInput = document.getElementById(`pr-settle-amt-${currentSettlements[0].id}`);
            if (firstAmtInput) firstAmtInput.value = Number(data.net_amount || 0).toFixed(2);
        }
    } catch (err) {
        if (mySeq !== previewRequestSeq) return;
        currentReturnPreview = null;
        previewErrorMessage = err.message || String(err);
        console.error("Return preview calculation error:", err);
    } finally {
        if (mySeq === previewRequestSeq) {
            isPreviewLoading = false;
            updateCalculationsPreviewDisplay();
            updateSettlementReconciliation();
        }
    }
}

function updateCalculationsPreviewDisplay() {
    const setVal = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.textContent = val;
    };

    const statusEl = document.getElementById("pr-calc-preview-status");
    const badgeEl = document.getElementById("pr-calc-preview-badge");
    const targetDisplay = document.getElementById("pr-settle-target-display");

    if (currentReturnPreview) {
        setVal("pr-calc-taxable", `₹${Number(currentReturnPreview.total_taxable || 0).toFixed(2)}`);
        setVal("pr-calc-cgst", `₹${Number(currentReturnPreview.total_cgst || 0).toFixed(2)}`);
        setVal("pr-calc-sgst", `₹${Number(currentReturnPreview.total_sgst || 0).toFixed(2)}`);
        setVal("pr-calc-igst", `₹${Number(currentReturnPreview.total_igst || 0).toFixed(2)}`);
        const ro = Number(currentReturnPreview.round_off || 0);
        setVal("pr-calc-roundoff", `${ro >= 0 ? '+' : ''}₹${ro.toFixed(2)}`);
        setVal("pr-calc-net", `₹${Number(currentReturnPreview.net_amount || 0).toFixed(2)}`);

        if (targetDisplay) targetDisplay.textContent = `₹${Number(currentReturnPreview.net_amount || 0).toFixed(2)}`;

        if (statusEl) {
            statusEl.style.display = "block";
            statusEl.innerHTML = `<div style="color: #15803d; font-weight: 600; background: #f0fdf4; border: 1px solid #bbf7d0; padding: 4px 8px; border-radius: 4px;">✓ Authoritative preview calculated by server (includes exact rounding &amp; cumulative tax remainder)</div>`;
        }
        if (badgeEl) {
            badgeEl.textContent = "Server Verified";
            badgeEl.style.color = "#15803d";
            badgeEl.style.background = "#dcfce7";
        }
    } else {
        setVal("pr-calc-taxable", "₹0.00");
        setVal("pr-calc-cgst", "₹0.00");
        setVal("pr-calc-sgst", "₹0.00");
        setVal("pr-calc-igst", "₹0.00");
        setVal("pr-calc-roundoff", "₹0.00");
        setVal("pr-calc-net", "₹0.00");

        if (targetDisplay) targetDisplay.textContent = "₹0.00";

        if (previewErrorMessage) {
            if (statusEl) {
                statusEl.style.display = "block";
                statusEl.innerHTML = `<div style="color: #b91c1c; font-weight: 600; background: #fef2f2; border: 1px solid #fecaca; padding: 4px 8px; border-radius: 4px;">⚠️ Preview Error: ${escapeHtml(previewErrorMessage)}</div>`;
            }
            if (badgeEl) {
                badgeEl.textContent = "Preview Error";
                badgeEl.style.color = "#b91c1c";
                badgeEl.style.background = "#fee2e2";
            }
        } else if (isPreviewLoading) {
            if (statusEl) {
                statusEl.style.display = "block";
                statusEl.innerHTML = `<div style="color: #4f46e5; font-weight: 600; background: #eef2ff; border: 1px solid #c7d2fe; padding: 4px 8px; border-radius: 4px;">⏳ Requesting authoritative backend preview from server...</div>`;
            }
            if (badgeEl) {
                badgeEl.textContent = "Calculating...";
                badgeEl.style.color = "#4f46e5";
                badgeEl.style.background = "#e0e7ff";
            }
        } else {
            if (statusEl) {
                statusEl.style.display = "none";
                statusEl.textContent = "";
            }
            if (badgeEl) {
                badgeEl.textContent = "Select Line Items";
                badgeEl.style.color = "#64748b";
                badgeEl.style.background = "#f1f5f9";
            }
        }
    }
}

// ----------------- Settlements Split Management -----------------
function renderSettlementRows() {
    const container = document.getElementById("pr-settlements-container");
    if (!container) return;

    container.innerHTML = currentSettlements.map((s) => {
        const isOnlyOne = currentSettlements.length === 1;
        return `
            <div class="settlement-row" id="pr-settle-row-${s.id}" style="display: flex; gap: 10px; align-items: center; margin-bottom: 8px; flex-wrap: wrap;">
                <div style="flex: 2; min-width: 220px;">
                    <select id="pr-settle-type-${s.id}" onchange="onSettlementRowChange(${s.id})" style="width: 100%; font-size: 12.5px; font-weight: 600;">
                        <option value="PAYABLE_REDUCTION" ${s.type === 'PAYABLE_REDUCTION' ? 'selected' : ''}>Payable Reduction (Deduct from supplier balance)</option>
                        <option value="REFUND_CASH" ${s.type === 'REFUND_CASH' ? 'selected' : ''}>Cash Refund (Cash received from supplier)</option>
                        <option value="REFUND_BANK" ${s.type === 'REFUND_BANK' ? 'selected' : ''}>Bank Refund (Bank transfer / Cheque received)</option>
                        <option value="SUPPLIER_CREDIT" ${s.type === 'SUPPLIER_CREDIT' ? 'selected' : ''}>Supplier Credit Note (Park as credit balance for future purchases)</option>
                    </select>
                </div>
                <div style="flex: 1; min-width: 140px;">
                    <input type="number" id="pr-settle-amt-${s.id}" value="${Number(s.amount || 0).toFixed(2)}"
                           min="0.01" step="0.01" oninput="onSettlementRowChange(${s.id})" placeholder="0.00"
                           style="width: 100%; font-size: 13px; font-weight: 700; text-align: right;">
                </div>
                <div style="flex: 1.5; min-width: 180px;">
                    <input type="text" id="pr-settle-ref-${s.id}" value="${escapeHtml(s.ref || '')}"
                           oninput="onSettlementRowChange(${s.id})" placeholder="Cheque # / Txn ID / Ref (opt)"
                           style="width: 100%; font-size: 12px;">
                </div>
                <div style="width: 36px; text-align: center;">
                    <button type="button" class="btn btn-outline-danger btn-sm" onclick="removeSettlementRow(${s.id})"
                            ${isOnlyOne ? 'disabled style="opacity: 0.35; cursor: not-allowed;"' : ''} title="Remove row">✕</button>
                </div>
            </div>
        `;
    }).join("");

    updateSettlementReconciliation();
}

function addSettlementRow() {
    const targetNet = currentReturnPreview ? parseFloat(currentReturnPreview.net_amount || 0) : 0;
    const allocated = currentSettlements.reduce((sum, s) => sum + (parseFloat(s.amount) || 0), 0);
    const unallocated = Math.max(0, Math.round((targetNet - allocated) * 100) / 100);

    let defaultType = "REFUND_CASH";
    if (currentSettlements.some(s => s.type === "REFUND_CASH")) defaultType = "REFUND_BANK";

    currentSettlements.push({
        id: nextSettlementRowId++,
        type: defaultType,
        amount: unallocated,
        ref: ""
    });

    renderSettlementRows();
}

function removeSettlementRow(rowId) {
    if (currentSettlements.length <= 1) return;
    currentSettlements = currentSettlements.filter(s => s.id !== rowId);
    renderSettlementRows();
}

function onSettlementRowChange(rowId) {
    const s = currentSettlements.find(item => item.id === rowId);
    if (!s) return;

    const typeEl = document.getElementById(`pr-settle-type-${rowId}`);
    const amtEl = document.getElementById(`pr-settle-amt-${rowId}`);
    const refEl = document.getElementById(`pr-settle-ref-${rowId}`);

    if (typeEl) s.type = typeEl.value;
    if (amtEl) s.amount = parseFloat(amtEl.value) || 0;
    if (refEl) s.ref = refEl.value;

    updateSettlementReconciliation();
}

function updateSettlementReconciliation() {
    const allocatedTotal = Math.round(currentSettlements.reduce((sum, s) => sum + (parseFloat(s.amount) || 0), 0) * 100) / 100;
    const targetNet = currentReturnPreview ? parseFloat(currentReturnPreview.net_amount || 0) : 0;
    const diff = Math.round((allocatedTotal - targetNet) * 100) / 100;

    const allocDisplay = document.getElementById("pr-settle-allocated-display");
    if (allocDisplay) allocDisplay.textContent = `₹${allocatedTotal.toFixed(2)}`;

    const diffBadge = document.getElementById("pr-settle-diff-badge");
    if (diffBadge) {
        if (!currentReturnPreview || targetNet === 0) {
            diffBadge.innerHTML = '<span style="color: #64748b;">No items selected (₹0.00)</span>';
        } else if (diff === 0) {
            diffBadge.innerHTML = '<span style="color: #15803d; font-weight: 700;">✓ Balanced (₹0.00 Difference)</span>';
        } else if (diff > 0) {
            diffBadge.innerHTML = `<span style="color: #b91c1c; font-weight: 700;">⚠️ Over-allocated by ₹${diff.toFixed(2)}</span>`;
        } else {
            diffBadge.innerHTML = `<span style="color: #b91c1c; font-weight: 700;">⚠️ Under-allocated by ₹${Math.abs(diff).toFixed(2)}</span>`;
        }
    }

    const scCount = currentSettlements.filter(s => s.type === "SUPPLIER_CREDIT").length;
    if (scCount > 1) {
        showToast("At most one Supplier Credit Note settlement allocation is permitted.", "warning");
    }

    const payableAllocated = currentSettlements
        .filter(s => s.type === "PAYABLE_REDUCTION")
        .reduce((sum, s) => sum + (parseFloat(s.amount) || 0), 0);

    if (payableAllocated > currentSupplierPayableBalance) {
        const excess = (payableAllocated - currentSupplierPayableBalance).toFixed(2);
        showToast(`Payable Reduction (₹${payableAllocated.toFixed(2)}) exceeds supplier's outstanding balance (₹${currentSupplierPayableBalance.toFixed(2)}). Excess: ₹${excess}.`, "warning");
    }

    // Submit button state strictly controlled:
    // Requires: valid preview, balanced settlement (diff === 0), explicit ITC treatment, not loading, not submitting
    const submitBtn = document.getElementById("btn-submit-purchase-return");
    const itcSelect = document.getElementById("pr-create-itc-status");
    const isItcSelected = itcSelect && itcSelect.value && ["CLAIMED_REVERSED", "NOT_CLAIMED", "PENDING_CONFIRMATION"].includes(itcSelect.value);
    const isBalanced = currentReturnPreview && targetNet > 0 && diff === 0;

    if (submitBtn) {
        submitBtn.disabled = !isBalanced || !isItcSelected || isSubmittingPurchaseReturn || isPreviewLoading;
    }
}

// ----------------- ITC Status Selection -----------------
function onItcStatusChange() {
    const itcSelect = document.getElementById("pr-create-itc-status");
    const explanationEl = document.getElementById("pr-itc-explanation");
    if (!itcSelect || !explanationEl) return;

    const val = itcSelect.value;
    if (val === "CLAIMED_REVERSED") {
        explanationEl.innerHTML = "Reverses GST Input Tax Credit in accounting ledgers (CGST/SGST Input accounts). Applicable when ITC on this purchase was previously claimed in GSTR-3B.<br><small style='color: #64748b;'>Note: ERP ledger posting only; statutory filing must be completed on the GST portal.</small>";
    } else if (val === "NOT_CLAIMED") {
        explanationEl.innerHTML = "No GST ITC reversal entry will be posted. Applicable when ITC was not claimed on original purchase (e.g. exempt supplies, blocked credits under Sec 17(5)).";
    } else if (val === "PENDING_CONFIRMATION") {
        explanationEl.innerHTML = "Debit note tax is parked in an ITC Suspense account pending receipt of supplier's official GST credit note.";
    } else {
        explanationEl.innerHTML = "<span style='color: #ef4444; font-weight: 600;'>Selection Required:</span> Please explicitly select how Input Tax Credit will be treated for this return.";
    }

    updateSettlementReconciliation();
}

// ----------------- Submit Purchase Return -----------------
async function submitCreatePurchaseReturn() {
    if (isSubmittingPurchaseReturn) return;

    // 1. Basic validation
    const selectEl = document.getElementById("pr-create-purchase-select");
    const purchaseId = selectEl ? selectEl.value : "";
    if (!purchaseId || !currentSelectedPurchase) {
        showToast("Please select an original inward purchase invoice.", "warning");
        return;
    }

    const returnDateInput = document.getElementById("pr-create-return-date");
    const returnDate = returnDateInput ? returnDateInput.value : "";
    if (!returnDate) {
        showToast("Please specify a valid return date.", "warning");
        return;
    }

    // 2. Authoritative backend preview validation
    if (isPreviewLoading) {
        showToast("Preview calculation is still in progress. Please wait...", "info");
        return;
    }

    if (!currentReturnPreview || parseFloat(currentReturnPreview.net_amount || 0) <= 0) {
        showToast("Cannot submit: A valid authoritative backend preview is required before submission.", "error");
        return;
    }

    // 3. Line items validation
    const selectedEntries = Object.entries(currentSelectedReturnItems).filter(([_, sel]) => parseFloat(sel.qty) > 0);
    if (selectedEntries.length === 0) {
        showToast("Please select at least one eligible line item and enter a return quantity.", "warning");
        return;
    }

    for (const [itemIdStr, sel] of selectedEntries) {
        const itemId = parseInt(itemIdStr);
        const line = currentEligiblePurchaseLines.find(l => l.item_id === itemId);
        if (!line) {
            showToast(`Selected line #${itemId} is not part of this purchase.`, "error");
            return;
        }
        if (parseFloat(line.free_qty || 0) > 0) {
            showToast(`Line '${line.product_name}' contains free/bonus quantity and cannot be returned.`, "error");
            return;
        }
        const qty = parseFloat(sel.qty);
        const maxVal = parseFloat(line.max_returnable_qty);
        if (isNaN(qty) || qty <= 0 || qty > maxVal) {
            showToast(`Invalid return quantity (${qty}) for '${line.product_name}'. Must be > 0 and ≤ ${maxVal}.`, "error");
            return;
        }
    }

    // 4. Settlements reconciliation validation against authoritative backend preview
    const allocatedTotal = Math.round(currentSettlements.reduce((sum, s) => sum + (parseFloat(s.amount) || 0), 0) * 100) / 100;
    const targetNet = parseFloat(currentReturnPreview.net_amount);
    if (allocatedTotal !== targetNet) {
        const diff = (allocatedTotal - targetNet).toFixed(2);
        showToast(`Settlement allocations (₹${allocatedTotal.toFixed(2)}) do not match Authoritative Net Return Amount (₹${targetNet.toFixed(2)}). Difference: ₹${diff}`, "error");
        return;
    }

    for (const s of currentSettlements) {
        if (!s.amount || parseFloat(s.amount) <= 0) {
            showToast("Each settlement allocation amount must be strictly greater than ₹0.00.", "warning");
            return;
        }
    }

    const scCount = currentSettlements.filter(s => s.type === "SUPPLIER_CREDIT").length;
    if (scCount > 1) {
        showToast("At most one Supplier Credit Note allocation is permitted per purchase return.", "error");
        return;
    }

    const payableAllocated = currentSettlements
        .filter(s => s.type === "PAYABLE_REDUCTION")
        .reduce((sum, s) => sum + (parseFloat(s.amount) || 0), 0);

    if (payableAllocated > currentSupplierPayableBalance) {
        showToast(`Payable reduction (₹${payableAllocated.toFixed(2)}) cannot exceed supplier current payable balance (₹${currentSupplierPayableBalance.toFixed(2)}).`, "error");
        return;
    }

    // 5. Mandatory explicit ITC status
    const itcSelect = document.getElementById("pr-create-itc-status");
    const itcStatus = (itcSelect ? itcSelect.value : "").trim();
    if (!itcStatus || !["CLAIMED_REVERSED", "NOT_CLAIMED", "PENDING_CONFIRMATION"].includes(itcStatus)) {
        showToast("Please make an explicit selection for ITC Treatment before submitting.", "error");
        if (itcSelect) itcSelect.focus();
        return;
    }

    // 6. Resolve account_id for Cash/Bank refunds from authoritative backend preview
    const builtSettlements = [];
    for (const s of currentSettlements) {
        const entry = {
            settlement_type: s.type,
            amount: parseFloat(parseFloat(s.amount).toFixed(2)),
            reference_no: s.ref ? s.ref.trim() : null
        };
        if (s.type === "REFUND_CASH") {
            const cashAccId = currentReturnPreview?.cash_account_id;
            if (!cashAccId) {
                showToast("Cash account is not configured for Cash Refund settlement.", "error");
                return;
            }
            entry.account_id = parseInt(cashAccId);
        } else if (s.type === "REFUND_BANK") {
            const bankAccId = currentReturnPreview?.bank_account_id;
            if (!bankAccId) {
                showToast("Bank account is not configured for Bank Refund settlement.", "error");
                return;
            }
            entry.account_id = parseInt(bankAccId);
        }
        builtSettlements.push(entry);
    }

    // 7. Final custom confirmation dialog (zero writes if cancelled)
    const settlementDesc = builtSettlements
        .map(s => `${formatSettlementType(s.settlement_type)}: ₹${Number(s.amount).toFixed(2)}`)
        .join(", ");
    const confirmed = await showConfirmDialog({
        title: "Confirm Purchase Return",
        message: `Post Purchase Return against Invoice ${currentSelectedPurchase.invoice_no || "#" + purchaseId} (${currentSelectedPurchase.supplier_name || "Supplier"})?\n\nItems: ${selectedEntries.length} line(s)\nNet Return Amount: ₹${targetNet.toFixed(2)}\nSettlement: ${settlementDesc}\nITC Treatment: ${itcStatus}\n\nThis will reduce batch stock and post a Debit Note voucher atomically.`,
        confirmText: "Confirm & Post Return",
        cancelText: "Cancel",
    });
    if (!confirmed) return;

    // 8. Ensure an idempotency key is present (reused on retry)
    if (!currentPurchaseReturnIdempotencyKey) {
        currentPurchaseReturnIdempotencyKey = generateIdempotencyKey();
    }

    const scnNo = (document.getElementById("pr-create-scn-no")?.value || "").trim();
    const scnDate = document.getElementById("pr-create-scn-date")?.value || null;
    const remarks = (document.getElementById("pr-create-remarks")?.value || "").trim();

    // 9. Construct payload (strictly conforming to PurchaseReturnCreate schema)
    const payload = {
        purchase_id: parseInt(purchaseId),
        supplier_id: parseInt(currentSelectedPurchase.supplier_id),
        return_date: returnDate,
        idempotency_key: currentPurchaseReturnIdempotencyKey,
        supplier_credit_note_no: scnNo || null,
        supplier_credit_note_date: scnDate || null,
        remarks: remarks || null,
        itc_status: itcStatus,
        items: selectedEntries.map(([itemIdStr, sel]) => {
            const itemId = parseInt(itemIdStr);
            const line = currentEligiblePurchaseLines.find(l => l.item_id === itemId);
            return {
                purchase_item_id: itemId,
                product_id: parseInt(line.product_id),
                batch_id: line.batch_id || null,
                qty: parseFloat(parseFloat(sel.qty).toFixed(4)),
                reason: sel.reason ? sel.reason.trim() : null
            };
        }),
        settlements: builtSettlements
    };

    const submitBtn = document.getElementById("btn-submit-purchase-return");
    const conflictBanner = document.getElementById("pr-idem-conflict-banner");
    if (conflictBanner) conflictBanner.style.display = "none";

    isSubmittingPurchaseReturn = true;
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "⏳ Confirming & Posting Return...";
    }

    try {
        const res = await fetch("/api/purchase/returns", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(payload)
        });

        if (res.status === 200 || res.status === 201) {
            const created = await res.json();
            showToast(`✓ Purchase Return ${created.return_no || '#' + created.return_id} posted successfully! Net: ₹${Number(created.net_amount || 0).toFixed(2)}`, "success", 6000);
            
            currentPurchaseReturnIdempotencyKey = null;
            closeCreatePurchaseReturnModal();

            // Refresh return history, affected inventory batches, and supplier balances
            await Promise.all([
                loadPurchaseReturns(),
                typeof refreshProductList === "function" ? refreshProductList() : Promise.resolve(),
                typeof loadInventory === "function" ? loadInventory() : Promise.resolve(),
                typeof refreshSupplierList === "function" ? refreshSupplierList() : Promise.resolve(),
                typeof loadSupplierKhata === "function" ? loadSupplierKhata() : Promise.resolve(),
            ]);

            if (typeof openPurchaseReturnDetail === "function" && created.return_id) {
                openPurchaseReturnDetail(created.return_id);
            }
        } else if (res.status === 409) {
            const errData = await res.json().catch(() => ({}));
            const msg = errData.detail || "This return request key was already used with different details.";
            if (conflictBanner) {
                const msgEl = document.getElementById("pr-idem-conflict-msg");
                if (msgEl) msgEl.textContent = `Idempotency Conflict (409): ${msg}`;
                conflictBanner.style.display = "block";
            }
            showToast(`Duplicate/conflict detected (409): ${msg}`, "warning", 7000);
        } else {
            let errorMsg = `Server error (${res.status})`;
            try {
                const errData = await res.json();
                if (errData && errData.detail) {
                    errorMsg = Array.isArray(errData.detail)
                        ? errData.detail.map(d => d.msg || JSON.stringify(d)).join("; ")
                        : String(errData.detail);
                }
            } catch (_) {}
            showToast(`Submission rejected: ${errorMsg}`, "error", 7000);
        }
    } catch (networkErr) {
        console.error("Network error during purchase return submission:", networkErr);
        showToast(`Network error submitting return: ${networkErr.message || networkErr}. You can retry safely with the same request.`, "error", 7000);
    } finally {
        isSubmittingPurchaseReturn = false;
        if (submitBtn) {
            submitBtn.textContent = "✓ Confirm & Post Purchase Return";
        }
        updateSettlementReconciliation();
    }
}

function continuePurchaseReturnAsNew() {
    currentPurchaseReturnIdempotencyKey = generateIdempotencyKey();
    const banner = document.getElementById("pr-idem-conflict-banner");
    if (banner) banner.style.display = "none";
    showToast("Generated a fresh request key. Review details and submit again if intended.", "info");
}

// ----------------- Purchase Return Detail & Debit Note View -----------------
async function openPurchaseReturnDetail(returnId) {
    if (!returnId || !hasValidAuthSession()) return;
    const modal = document.getElementById("modal-purchase-return-detail");
    if (!modal) return;

    try {
        const res = await fetch(`/api/purchase/returns/${returnId}`);
        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            showToast(errData.detail || "Could not load purchase return details.", "error");
            return;
        }
        const ret = await res.json();
        const s = currentCompanySettings || {};
        const shopName = (s.company_name || "").trim() || "Agri-Input Shop";

        const setEl = (id, text) => {
            const el = document.getElementById(id);
            if (el) el.textContent = text;
        };

        setEl("prd-modal-title", `📄 Purchase Return Detail — ${ret.return_no || "#" + ret.return_id}`);
        setEl("prd-shop-name", shopName);
        setEl("prd-shop-details", typeof buildShopAddressText === "function" ? (buildShopAddressText(s) || "Agri-Input Dealer") : "Agri-Input Dealer");
        setEl("prd-shop-gstin", s.gstin || "-");
        setEl("prd-auth-shop", `For ${shopName}`);

        setEl("prd-return-no", ret.return_no || `#${ret.return_id}`);
        setEl("prd-return-date", ret.return_date || "-");
        const statusEl = document.getElementById("prd-status-badge");
        if (statusEl) {
            const st = (ret.status || "POSTED").toUpperCase();
            statusEl.textContent = st;
            statusEl.className = st === "POSTED" ? "badge badge-success" : "badge badge-danger";
        }

        setEl("prd-supplier-name", ret.supplier_name || `Supplier #${ret.supplier_id}`);
        setEl("prd-supplier-city", ret.supplier_city || "-");
        setEl("prd-supplier-gstin", ret.supplier_gstin || "Unregistered");
        setEl("prd-supplier-mobile", ret.supplier_mobile || "-");

        setEl("prd-purchase-inv", ret.purchase_invoice_no || `Purchase #${ret.purchase_id}`);
        setEl("prd-supplier-cn", ret.supplier_credit_note_no ? `${ret.supplier_credit_note_no}${ret.supplier_credit_note_date ? " (" + ret.supplier_credit_note_date + ")" : ""}` : "-");
        setEl("prd-voucher-ref", ret.voucher_id ? `Voucher #${ret.voucher_id}` : "-");
        setEl("prd-itc-status", ret.itc_status || "-");

        const itemsTbody = document.getElementById("prd-items-tbody");
        if (itemsTbody) {
            const items = Array.isArray(ret.items) ? ret.items : [];
            itemsTbody.innerHTML = items.length === 0
                ? '<tr><td colspan="10" class="text-center text-muted">No returned items found.</td></tr>'
                : items.map((it, idx) => {
                    const gstRate = (Number(it.cgst_rate || 0) + Number(it.sgst_rate || 0)) || Number(it.igst_rate || 0);
                    const gstAmt = Number(it.cgst_amount || 0) + Number(it.sgst_amount || 0) + Number(it.igst_amount || 0);
                    return `
                        <tr style="border-bottom: 1px solid #e2e8f0;">
                            <td style="padding: 6px 8px;">${idx + 1}</td>
                            <td style="padding: 6px 8px;"><strong>${escapeHtml(it.product_name || "Product #" + it.product_id)}</strong>${it.reason ? '<br><small style="color:#64748b;">Reason: ' + escapeHtml(it.reason) + '</small>' : ''}</td>
                            <td style="padding: 6px 8px;">${escapeHtml(it.hsn_code || "-")}</td>
                            <td style="padding: 6px 8px;">${escapeHtml(it.batch_no || "-")}</td>
                            <td style="padding: 6px 8px; text-align: right;">${Number(it.qty || 0)}</td>
                            <td style="padding: 6px 8px; text-align: right;">₹${Number(it.net_rate || it.purchase_rate || 0).toFixed(2)}</td>
                            <td style="padding: 6px 8px; text-align: right;">₹${Number(it.taxable_amount || 0).toFixed(2)}</td>
                            <td style="padding: 6px 8px; text-align: right;">${gstRate}%</td>
                            <td style="padding: 6px 8px; text-align: right;">₹${gstAmt.toFixed(2)}</td>
                            <td style="padding: 6px 8px; text-align: right; font-weight: 700;">₹${Number(it.total_amount || 0).toFixed(2)}</td>
                        </tr>
                    `;
                }).join("");
        }

        const stlEl = document.getElementById("prd-settlements-list");
        if (stlEl) {
            const stls = Array.isArray(ret.settlements) ? ret.settlements : [];
            stlEl.innerHTML = stls.length === 0
                ? '<span class="text-muted">No settlements recorded</span>'
                : stls.map(st => `
                    <div style="display: flex; justify-content: space-between; padding: 3px 0; border-bottom: 1px dashed #e2e8f0;">
                        <span>${escapeHtml(formatSettlementType(st.settlement_type))}${st.reference_no ? " (Ref: " + escapeHtml(st.reference_no) + ")" : ""}</span>
                        <strong>₹${Number(st.amount || 0).toFixed(2)}</strong>
                    </div>
                `).join("");
        }

        const remEl = document.getElementById("prd-remarks-row");
        if (remEl) {
            remEl.textContent = ret.remarks ? `Remarks: ${ret.remarks}` : "";
        }

        setEl("prd-total-taxable", `₹${Number(ret.total_taxable || 0).toFixed(2)}`);
        setEl("prd-total-cgst", `₹${Number(ret.total_cgst || 0).toFixed(2)}`);
        setEl("prd-total-sgst", `₹${Number(ret.total_sgst || 0).toFixed(2)}`);
        setEl("prd-total-igst", `₹${Number(ret.total_igst || 0).toFixed(2)}`);
        const ro = Number(ret.round_off || 0);
        setEl("prd-total-roundoff", `${ro >= 0 ? "+" : "-"}₹${Math.abs(ro).toFixed(2)}`);
        setEl("prd-total-net", `₹${Number(ret.net_amount || 0).toFixed(2)}`);

        modal.style.display = "flex";
    } catch (err) {
        console.error("Failed to open purchase return detail:", err);
        showToast("Error loading purchase return detail: " + (err.message || err), "error");
    }
}

function closePurchaseReturnDetailModal() {
    const modal = document.getElementById("modal-purchase-return-detail");
    if (modal) modal.style.display = "none";
    if (typeof clearPrintTargetClasses === "function") clearPrintTargetClasses();
}

function printPurchaseReturnDetail() {
    printSection("purchase-return");
}

// ============================================================================
// CUSTOMER SALES RETURNS WORKFLOW (PHASE 2D-2)
// ============================================================================

let salesReturnsData = [];
let srSearchDebounceTimer = null;
let allSalesForReturn = [];
let srSelectedSale = null;
let srEligibleLines = [];
let srSelectedItems = {}; // lineKey -> { product_id, batch_id, qty, reason }
let srPreviewData = null;
let srPreviewRequestSeq = 0;
let srPreviewDebounceTimer = null;
let srIsPreviewLoading = false;
let srPreviewErrorMessage = null;
let srIdempotencyKey = null;
let srIsSubmitting = false;

const SALES_RETURN_REASONS = [
    "Damaged / Leaking Pack",
    "Wrong Product / Pack Size",
    "Near Expiry / Expired",
    "Excess Quantity Purchased",
    "Crop Plan Changed",
    "Quality Issue",
    "Other",
];

function _salesReturnLineKey(productId, batchId) {
    return `${productId}_${batchId || 0}`;
}

async function loadSalesReturns() {
    if (!hasValidAuthSession()) return;
    const tbody = document.getElementById("sr-table-tbody");
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted" style="padding: 24px;">⏳ Loading sales returns...</td></tr>';

    const fromDate = document.getElementById("sr-filter-from-date")?.value || "";
    const toDate = document.getElementById("sr-filter-to-date")?.value || "";
    const params = new URLSearchParams();
    if (fromDate) params.set("from_date", fromDate);
    if (toDate) params.set("to_date", toDate);
    const qs = params.toString();

    try {
        const res = await fetch(qs ? `/api/sales/returns?${qs}` : "/api/sales/returns");
        if (!res.ok) {
            tbody.innerHTML = '<tr><td colspan="10" class="text-center text-danger" style="padding: 20px;">Failed to load sales returns.</td></tr>';
            return;
        }
        const data = await res.json();
        salesReturnsData = Array.isArray(data) ? data : [];
        renderSalesReturnsTable();
    } catch (err) {
        console.error("Error loading sales returns:", err);
        tbody.innerHTML = `<tr><td colspan="10" class="text-center text-danger" style="padding: 20px;">Error: ${escapeHtml(err.message || String(err))}</td></tr>`;
    }
}

function renderSalesReturnsTable() {
    const tbody = document.getElementById("sr-table-tbody");
    if (!tbody) return;
    const q = (document.getElementById("sr-filter-search")?.value || "").trim().toLowerCase();

    const filtered = salesReturnsData.filter(r => {
        if (!q) return true;
        const hay = `${r.return_no || ""} ${r.original_invoice_no || ""} ${r.customer_name || ""} ${r.remarks || ""} ${r.voucher_no || ""}`.toLowerCase();
        return hay.includes(q);
    });

    if (filtered.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted" style="padding: 24px;">No sales returns recorded yet. Click "+ New Sales Return" to process one.</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map(r => `
        <tr>
            <td><strong style="color: var(--primary);">${escapeHtml(r.return_no || "#" + r.return_id)}</strong></td>
            <td>${escapeHtml(r.return_date || "-")}</td>
            <td>${escapeHtml(r.original_invoice_no || (r.sale_id ? "Sale #" + r.sale_id : "-"))}</td>
            <td><b>${escapeHtml(r.customer_name || "Customer #" + r.customer_id)}</b></td>
            <td class="text-right">₹${Number(r.total_taxable || 0).toFixed(2)}</td>
            <td class="text-right">₹${Number(r.total_tax || 0).toFixed(2)}</td>
            <td class="text-right"><strong style="color: #15803d;">₹${Number(r.net_amount || 0).toFixed(2)}</strong></td>
            <td>${r.voucher_no ? '<span style="font-weight: 600; color: #1e40af;">' + escapeHtml(r.voucher_no) + '</span>' : '<span class="text-muted">-</span>'}</td>
            <td style="font-size: 12px; color: #475569;">${escapeHtml(r.remarks || "-")}</td>
            <td class="text-center">
                <button type="button" class="btn btn-secondary btn-sm" onclick="openSalesReturnDetail(${r.return_id})" title="View Sales Return Summary">👁️ View</button>
            </td>
        </tr>
    `).join("");
}

function applySalesReturnFilters() {
    loadSalesReturns();
}

function resetSalesReturnFilters() {
    const s = document.getElementById("sr-filter-search");
    const fd = document.getElementById("sr-filter-from-date");
    const td = document.getElementById("sr-filter-to-date");
    if (s) s.value = "";
    if (fd) fd.value = "";
    if (td) td.value = "";
    loadSalesReturns();
}

function debounceSalesReturnSearch() {
    if (srSearchDebounceTimer) clearTimeout(srSearchDebounceTimer);
    srSearchDebounceTimer = setTimeout(() => renderSalesReturnsTable(), 180);
}

async function openCreateSalesReturnModal(preselectedSaleId = null) {
    if (!hasValidAuthSession()) return;
    const modal = document.getElementById("modal-create-sales-return");
    if (!modal) return;
    modal.style.display = "flex";

    srSelectedSale = null;
    srEligibleLines = [];
    srSelectedItems = {};
    srPreviewData = null;
    srIsPreviewLoading = false;
    srPreviewErrorMessage = null;
    if (srPreviewDebounceTimer) {
        clearTimeout(srPreviewDebounceTimer);
        srPreviewDebounceTimer = null;
    }
    srIdempotencyKey = generateIdempotencyKey();
    srIsSubmitting = false;

    const dateEl = document.getElementById("sr-create-return-date");
    if (dateEl) dateEl.value = new Date().toISOString().split("T")[0];
    const searchEl = document.getElementById("sr-create-sale-search");
    if (searchEl) searchEl.value = "";
    const remEl = document.getElementById("sr-create-remarks");
    if (remEl) remEl.value = "";
    const infoBar = document.getElementById("sr-selected-sale-info");
    if (infoBar) infoBar.style.display = "none";
    const conflictBanner = document.getElementById("sr-idem-conflict-banner");
    if (conflictBanner) conflictBanner.style.display = "none";

    const itemsTbody = document.getElementById("sr-items-tbody");
    if (itemsTbody) {
        itemsTbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted" style="padding: 16px;">⏳ Loading sales invoices...</td></tr>';
    }
    updateSalesReturnPreviewDisplay();

    try {
        const res = await fetch("/api/sales/list");
        if (!res.ok) {
            showToast("Failed to load sales invoices.", "error");
            if (itemsTbody) itemsTbody.innerHTML = '<tr><td colspan="9" class="text-center text-danger" style="padding: 16px;">Failed to load sales invoices.</td></tr>';
            return;
        }
        const data = await res.json();
        allSalesForReturn = Array.isArray(data) ? data : [];
        renderSalesDropdownOptions(allSalesForReturn, preselectedSaleId);
        if (preselectedSaleId) {
            await onSaleSelectedForReturn();
        } else if (itemsTbody) {
            itemsTbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted" style="padding: 16px;">Please select an original sales invoice above to load eligible items.</td></tr>';
        }
    } catch (err) {
        console.error("Error loading sales list for return:", err);
        showToast("Error loading sales invoices: " + (err.message || err), "error");
    }
}

function closeCreateSalesReturnModal() {
    const modal = document.getElementById("modal-create-sales-return");
    if (modal) modal.style.display = "none";
    if (srPreviewDebounceTimer) {
        clearTimeout(srPreviewDebounceTimer);
        srPreviewDebounceTimer = null;
    }
}

function renderSalesDropdownOptions(salesList, preserveValue = null) {
    const selectEl = document.getElementById("sr-create-sale-select");
    if (!selectEl) return;
    const currentVal = preserveValue !== null && preserveValue !== undefined
        ? String(preserveValue)
        : selectEl.value;
    selectEl.innerHTML = '<option value="">-- Choose Sales Invoice --</option>' +
        (salesList || []).map(s => {
            const invNo = escapeHtml(s.invoice_no || `Sale #${s.sale_id}`);
            const saleDate = escapeHtml(s.sale_date || "-");
            const custName = escapeHtml(s.customer_name || `Customer #${s.customer_id}`);
            const modeLabel = escapeHtml(formatPaymentModeLabel(s.payment_mode));
            const net = Number(s.net_amount || 0).toFixed(2);
            return `<option value="${s.sale_id}">${invNo} (${saleDate}) — ${custName} [${modeLabel}] (₹${net})</option>`;
        }).join("");
    if (currentVal && (salesList || []).some(s => String(s.sale_id) === String(currentVal))) {
        selectEl.value = String(currentVal);
    }
}

function filterSalesForReturnDropdown() {
    const q = (document.getElementById("sr-create-sale-search")?.value || "").trim().toLowerCase();
    if (!q) {
        renderSalesDropdownOptions(allSalesForReturn);
        return;
    }
    const filtered = allSalesForReturn.filter(s => {
        const hay = `${s.invoice_no || ""} ${s.customer_name || ""} ${s.mobile || ""} ${s.sale_date || ""} ${s.payment_mode || ""}`.toLowerCase();
        return hay.includes(q);
    });
    renderSalesDropdownOptions(filtered);
}

async function onSaleSelectedForReturn() {
    const selectEl = document.getElementById("sr-create-sale-select");
    const saleId = selectEl ? selectEl.value : "";
    const infoBar = document.getElementById("sr-selected-sale-info");
    const itemsTbody = document.getElementById("sr-items-tbody");

    srSelectedSale = null;
    srEligibleLines = [];
    srSelectedItems = {};
    srIdempotencyKey = generateIdempotencyKey();
    const conflictBanner = document.getElementById("sr-idem-conflict-banner");
    if (conflictBanner) conflictBanner.style.display = "none";
    invalidateSalesReturnPreview();

    if (!saleId) {
        if (infoBar) infoBar.style.display = "none";
        if (itemsTbody) {
            itemsTbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted" style="padding: 16px;">Please select an original sales invoice above to load eligible items.</td></tr>';
        }
        return;
    }

    if (itemsTbody) {
        itemsTbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted" style="padding: 16px;">⏳ Loading eligible return items...</td></tr>';
    }

    try {
        const res = await fetch(`/api/sales/${saleId}/eligible-returns`);
        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            const errMsg = errData.detail || "Could not load eligible return items for selected invoice.";
            if (itemsTbody) {
                itemsTbody.innerHTML = `<tr><td colspan="9" class="text-center text-danger" style="padding: 16px;">⚠️ ${escapeHtml(errMsg)}</td></tr>`;
            }
            showToast(errMsg, "error");
            return;
        }
        const data = await res.json();
        srSelectedSale = data;
        srEligibleLines = Array.isArray(data.items) ? data.items : [];

        if (infoBar) {
            document.getElementById("sr-info-cust-name").textContent = data.customer_name || "-";
            document.getElementById("sr-info-cust-mobile").textContent = data.customer_mobile || "-";
            document.getElementById("sr-info-sale-date").textContent = data.sale_date || "-";
            document.getElementById("sr-info-pay-mode").textContent = formatPaymentModeLabel(data.payment_mode);
            document.getElementById("sr-info-net-amt").textContent = `₹${Number(data.net_amount || 0).toFixed(2)}`;
            document.getElementById("sr-info-paid-due").textContent = `₹${Number(data.paid_amount || 0).toFixed(2)} / ₹${Number(data.due_amount || 0).toFixed(2)}`;
            document.getElementById("sr-info-cust-balance").textContent = `₹${Number(data.customer_current_balance || 0).toFixed(2)}`;
            infoBar.style.display = "block";
        }

        renderEligibleSaleLinesTable();
        updateSalesReturnPreviewDisplay();
    } catch (err) {
        console.error("Error fetching eligible sale return lines:", err);
        showToast("Error loading invoice items: " + (err.message || err), "error");
    }
}

function renderEligibleSaleLinesTable() {
    const tbody = document.getElementById("sr-items-tbody");
    if (!tbody) return;
    if (srEligibleLines.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted" style="padding: 18px;">No line items found on this invoice.</td></tr>';
        return;
    }

    const reasonOptionsHtml = SALES_RETURN_REASONS
        .map(r => `<option value="${escapeHtml(r)}">${escapeHtml(r)}</option>`)
        .join("");

    tbody.innerHTML = srEligibleLines.map(line => {
        const key = _salesReturnLineKey(line.product_id, line.batch_id);
        const soldQty = parseFloat(line.sold_qty || 0);
        const returnedQty = parseFloat(line.already_returned_qty || 0);
        const eligibleQty = parseFloat(line.eligible_return_qty || 0);
        const isDisabled = eligibleQty <= 0;
        const gstRate = (parseFloat(line.cgst_rate || 0) + parseFloat(line.sgst_rate || 0)) || parseFloat(line.igst_rate || 0);

        return `
            <tr style="${isDisabled ? "background: #f8fafc; opacity: 0.7;" : ""}">
                <td style="text-align: center;">
                    <input type="checkbox" id="sr-chk-${key}" ${isDisabled ? "disabled" : ""} onchange="onSalesReturnItemCheckToggle('${key}')">
                </td>
                <td>
                    <b>${escapeHtml(line.product_name || "Product #" + line.product_id)}</b>
                    ${line.hsn_code ? `<br><small style="color:#64748b;">HSN: ${escapeHtml(line.hsn_code)}</small>` : ""}
                    ${isDisabled ? '<br><span class="badge" style="background:#e2e8f0; color:#475569; font-size:10.5px;">Fully Returned</span>' : ""}
                </td>
                <td>
                    <b>${escapeHtml(line.batch_no || "-")}</b>
                    ${line.exp_date ? `<br><small style="color:#64748b;">Exp: ${escapeHtml(line.exp_date)}</small>` : ""}
                </td>
                <td class="text-right">${soldQty}</td>
                <td class="text-right">${returnedQty}</td>
                <td class="text-right"><strong style="color: ${isDisabled ? "#64748b" : "#15803d"};">${eligibleQty}</strong></td>
                <td class="text-right">₹${Number(line.sale_rate || 0).toFixed(2)}<br><small style="color:#475569;">GST: ${gstRate}%</small></td>
                <td>
                    <input type="number" id="sr-qty-${key}" min="0.0001" max="${eligibleQty}" step="any" disabled
                           oninput="onSalesReturnItemQtyChange('${key}')" placeholder="0"
                           style="width: 88px; padding: 4px 6px; font-size: 12px; text-align: right; font-weight: 600;">
                </td>
                <td>
                    <select id="sr-reason-${key}" disabled onchange="onSalesReturnItemReasonChange('${key}')" style="width: 165px; padding: 4px 6px; font-size: 11.5px;">
                        ${reasonOptionsHtml}
                    </select>
                </td>
            </tr>
        `;
    }).join("");
}

function _findSaleLineByKey(lineKey) {
    return srEligibleLines.find(l => _salesReturnLineKey(l.product_id, l.batch_id) === lineKey) || null;
}

function onSalesReturnItemCheckToggle(lineKey) {
    const chk = document.getElementById(`sr-chk-${lineKey}`);
    const qtyInput = document.getElementById(`sr-qty-${lineKey}`);
    const reasonSelect = document.getElementById(`sr-reason-${lineKey}`);
    const line = _findSaleLineByKey(lineKey);
    if (!chk || !qtyInput || !line) return;

    if (chk.checked) {
        qtyInput.disabled = false;
        if (reasonSelect) reasonSelect.disabled = false;
        const defaultQty = parseFloat(line.eligible_return_qty || 0);
        qtyInput.value = defaultQty;
        qtyInput.style.borderColor = "";
        srSelectedItems[lineKey] = {
            product_id: parseInt(line.product_id),
            batch_id: line.batch_id ? parseInt(line.batch_id) : null,
            qty: defaultQty,
            reason: reasonSelect ? reasonSelect.value : SALES_RETURN_REASONS[0],
        };
    } else {
        qtyInput.disabled = true;
        qtyInput.value = "";
        qtyInput.style.borderColor = "";
        if (reasonSelect) reasonSelect.disabled = true;
        delete srSelectedItems[lineKey];
    }
    invalidateSalesReturnPreview();
    scheduleBackendSalesReturnPreview();
}

function onSalesReturnItemQtyChange(lineKey) {
    const qtyInput = document.getElementById(`sr-qty-${lineKey}`);
    const line = _findSaleLineByKey(lineKey);
    if (!qtyInput || !line) return;

    const val = parseFloat(qtyInput.value);
    const maxVal = parseFloat(line.eligible_return_qty || 0);
    if (isNaN(val) || val <= 0 || val > maxVal) {
        qtyInput.style.borderColor = "#ef4444";
        showToast(`Return quantity for ${line.product_name} must be > 0 and ≤ ${maxVal}.`, "warning");
        if (srSelectedItems[lineKey]) {
            srSelectedItems[lineKey].qty = 0;
        }
    } else {
        qtyInput.style.borderColor = "";
        if (!srSelectedItems[lineKey]) {
            const reasonSelect = document.getElementById(`sr-reason-${lineKey}`);
            srSelectedItems[lineKey] = {
                product_id: parseInt(line.product_id),
                batch_id: line.batch_id ? parseInt(line.batch_id) : null,
                qty: val,
                reason: reasonSelect ? reasonSelect.value : SALES_RETURN_REASONS[0],
            };
        } else {
            srSelectedItems[lineKey].qty = val;
        }
    }
    invalidateSalesReturnPreview();
    scheduleBackendSalesReturnPreview();
}

function onSalesReturnItemReasonChange(lineKey) {
    const reasonSelect = document.getElementById(`sr-reason-${lineKey}`);
    if (reasonSelect && srSelectedItems[lineKey]) {
        srSelectedItems[lineKey].reason = reasonSelect.value;
    }
}

function invalidateSalesReturnPreview() {
    srPreviewRequestSeq++;
    srPreviewData = null;
    srPreviewErrorMessage = null;
    srIsPreviewLoading = false;
    if (srPreviewDebounceTimer) {
        clearTimeout(srPreviewDebounceTimer);
        srPreviewDebounceTimer = null;
    }
    updateSalesReturnPreviewDisplay();
}

function scheduleBackendSalesReturnPreview() {
    if (srPreviewDebounceTimer) clearTimeout(srPreviewDebounceTimer);
    srPreviewDebounceTimer = setTimeout(() => requestBackendSalesReturnPreview(), 180);
}

async function requestBackendSalesReturnPreview() {
    if (!srSelectedSale) {
        invalidateSalesReturnPreview();
        return;
    }
    const activeEntries = Object.values(srSelectedItems).filter(it => parseFloat(it.qty) > 0);
    if (activeEntries.length === 0) {
        invalidateSalesReturnPreview();
        return;
    }

    const mySeq = ++srPreviewRequestSeq;
    srIsPreviewLoading = true;
    srPreviewErrorMessage = null;
    updateSalesReturnPreviewDisplay();

    try {
        const res = await fetch("/api/sales/returns/preview", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                sale_id: parseInt(srSelectedSale.sale_id),
                items: activeEntries.map(it => ({
                    product_id: it.product_id,
                    batch_id: it.batch_id,
                    qty: parseFloat(parseFloat(it.qty).toFixed(4)),
                    reason: it.reason || null,
                })),
            }),
        });
        if (mySeq !== srPreviewRequestSeq) return;
        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || "Sales return preview calculation failed.");
        }
        const data = await res.json();
        if (mySeq !== srPreviewRequestSeq) return;
        srPreviewData = data;
        srPreviewErrorMessage = null;
    } catch (err) {
        if (mySeq !== srPreviewRequestSeq) return;
        srPreviewData = null;
        srPreviewErrorMessage = err.message || String(err);
    } finally {
        if (mySeq === srPreviewRequestSeq) {
            srIsPreviewLoading = false;
            updateSalesReturnPreviewDisplay();
        }
    }
}

function updateSalesReturnPreviewDisplay() {
    const setEl = (id, text) => {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    };
    const statusEl = document.getElementById("sr-calc-preview-status");
    const badgeEl = document.getElementById("sr-calc-preview-badge");
    const settlePanel = document.getElementById("sr-settlement-panel");
    const submitBtn = document.getElementById("btn-submit-sales-return");

    if (srPreviewData) {
        setEl("sr-calc-taxable", `₹${Number(srPreviewData.total_taxable || 0).toFixed(2)}`);
        setEl("sr-calc-cgst", `₹${Number(srPreviewData.total_cgst || 0).toFixed(2)}`);
        setEl("sr-calc-sgst", `₹${Number(srPreviewData.total_sgst || 0).toFixed(2)}`);
        setEl("sr-calc-igst", `₹${Number(srPreviewData.total_igst || 0).toFixed(2)}`);
        const ro = Number(srPreviewData.round_off || 0);
        setEl("sr-calc-roundoff", `${ro >= 0 ? "+" : "-"}₹${Math.abs(ro).toFixed(2)}`);
        setEl("sr-calc-net", `₹${Number(srPreviewData.net_amount || 0).toFixed(2)}`);

        if (statusEl) {
            statusEl.style.display = "block";
            statusEl.innerHTML = `<div style="color: #15803d; font-weight: 600; background: #f0fdf4; border: 1px solid #bbf7d0; padding: 4px 8px; border-radius: 4px;">✓ Authoritative snapshot preview calculated by server (original invoice GST &amp; pricing preserved)</div>`;
        }
        if (badgeEl) {
            badgeEl.textContent = "Server Verified";
            badgeEl.style.color = "#15803d";
            badgeEl.style.background = "#dcfce7";
        }

        if (settlePanel) {
            if (srPreviewData.settlement_supported) {
                settlePanel.style.background = "#f0fdf4";
                settlePanel.style.borderColor = "#86efac";
                settlePanel.style.color = "#166534";
                settlePanel.innerHTML = `
                    <div style="font-weight: 700; margin-bottom: 4px;">✓ Supported Settlement / Refund Treatment</div>
                    <div>${escapeHtml(srPreviewData.settlement_summary || "-")}</div>
                `;
            } else {
                settlePanel.style.background = "#fef2f2";
                settlePanel.style.borderColor = "#fca5a5";
                settlePanel.style.color = "#991b1b";
                settlePanel.innerHTML = `
                    <div style="font-weight: 700; margin-bottom: 4px;">⛔ Unsupported / Ambiguous Refund Blocked</div>
                    <div>${escapeHtml(srPreviewData.unsupported_reason || "Refund method cannot be safely inferred from original invoice.")}</div>
                `;
            }
        }
    } else {
        setEl("sr-calc-taxable", "₹0.00");
        setEl("sr-calc-cgst", "₹0.00");
        setEl("sr-calc-sgst", "₹0.00");
        setEl("sr-calc-igst", "₹0.00");
        setEl("sr-calc-roundoff", "₹0.00");
        setEl("sr-calc-net", "₹0.00");

        if (srPreviewErrorMessage) {
            if (statusEl) {
                statusEl.style.display = "block";
                statusEl.innerHTML = `<div style="color: #b91c1c; font-weight: 600; background: #fef2f2; border: 1px solid #fecaca; padding: 4px 8px; border-radius: 4px;">⚠️ Preview Error: ${escapeHtml(srPreviewErrorMessage)}</div>`;
            }
            if (badgeEl) {
                badgeEl.textContent = "Preview Error";
                badgeEl.style.color = "#b91c1c";
                badgeEl.style.background = "#fee2e2";
            }
        } else if (srIsPreviewLoading) {
            if (statusEl) {
                statusEl.style.display = "block";
                statusEl.innerHTML = `<div style="color: #4f46e5; font-weight: 600; background: #eef2ff; border: 1px solid #c7d2fe; padding: 4px 8px; border-radius: 4px;">⏳ Calculating authoritative return preview...</div>`;
            }
            if (badgeEl) {
                badgeEl.textContent = "Calculating...";
                badgeEl.style.color = "#4f46e5";
                badgeEl.style.background = "#e0e7ff";
            }
        } else {
            if (statusEl) {
                statusEl.style.display = "none";
                statusEl.textContent = "";
            }
            if (badgeEl) {
                badgeEl.textContent = "Select Line Items";
                badgeEl.style.color = "#64748b";
                badgeEl.style.background = "#f1f5f9";
            }
        }

        if (settlePanel) {
            settlePanel.style.background = "#f8fafc";
            settlePanel.style.borderColor = "#e2e8f0";
            settlePanel.style.color = "#334155";
            settlePanel.textContent = "Select returned items above to verify eligible credit adjustment or refund channel.";
        }
    }

    if (submitBtn) {
        const canSubmit = Boolean(
            srPreviewData &&
            srPreviewData.settlement_supported &&
            Number(srPreviewData.net_amount || 0) > 0 &&
            !srIsPreviewLoading &&
            !srIsSubmitting
        );
        submitBtn.disabled = !canSubmit;
    }
}

function continueSalesReturnAsNew() {
    srIdempotencyKey = generateIdempotencyKey();
    const banner = document.getElementById("sr-idem-conflict-banner");
    if (banner) banner.style.display = "none";
    showToast("Generated a fresh request key. Review details and submit again if intended.", "info");
}

async function submitCreateSalesReturn() {
    if (srIsSubmitting) return;
    if (!srSelectedSale) {
        showToast("Please select an original sales invoice.", "warning");
        return;
    }
    const returnDate = document.getElementById("sr-create-return-date")?.value || "";
    if (!returnDate) {
        showToast("Please specify a valid return date.", "warning");
        return;
    }
    if (srIsPreviewLoading) {
        showToast("Return calculation is still in progress. Please wait...", "info");
        return;
    }
    if (!srPreviewData || Number(srPreviewData.net_amount || 0) <= 0) {
        showToast("Please select valid returned items before submitting.", "warning");
        return;
    }
    if (!srPreviewData.settlement_supported) {
        showToast(srPreviewData.unsupported_reason || "This refund channel is not supported.", "error", 7000);
        return;
    }

    // Validate all checked items against eligible bounds
    const activeLines = Object.entries(srSelectedItems).filter(([_, it]) => parseFloat(it.qty) > 0);
    if (activeLines.length === 0) {
        showToast("Please select at least one item and enter a valid return quantity.", "warning");
        return;
    }
    for (const [key, sel] of activeLines) {
        const origLine = _findSaleLineByKey(key);
        const maxVal = origLine ? parseFloat(origLine.eligible_return_qty || 0) : 0;
        const qty = parseFloat(sel.qty);
        if (isNaN(qty) || qty <= 0 || qty > maxVal) {
            showToast(`Invalid return quantity (${qty}) for '${origLine?.product_name || "item"}'. Must be > 0 and ≤ ${maxVal}.`, "error");
            return;
        }
    }

    // Final custom confirmation dialog (zero writes if cancelled)
    const confirmed = await showConfirmDialog({
        title: "Confirm Sales Return",
        message: `Post Sales Return against Invoice ${srSelectedSale.invoice_no} (${srSelectedSale.customer_name || "Customer"})?\n\nItems: ${srPreviewData.items.length} line(s)\nNet Return Amount: ₹${Number(srPreviewData.net_amount).toFixed(2)}\nTreatment: ${srPreviewData.settlement_summary}\n\nThis will restore original batch stock and post a balanced accounting voucher atomically.`,
        confirmText: "Confirm & Post Return",
        cancelText: "Cancel",
    });
    if (!confirmed) return;

    if (!srIdempotencyKey) {
        srIdempotencyKey = generateIdempotencyKey();
    }

    const remarks = (document.getElementById("sr-create-remarks")?.value || "").trim();
    const payload = {
        return_no: "",
        return_date: returnDate,
        sale_id: parseInt(srSelectedSale.sale_id),
        customer_id: parseInt(srSelectedSale.customer_id),
        total_taxable: Number(srPreviewData.total_taxable || 0),
        total_tax: Number(srPreviewData.total_tax || 0),
        round_off: Number(srPreviewData.round_off || 0),
        net_amount: Number(srPreviewData.net_amount || 0),
        remarks: remarks || null,
        idempotency_key: srIdempotencyKey,
        items: (srPreviewData.items || []).map(it => ({
            product_id: parseInt(it.product_id),
            batch_id: it.batch_id ? parseInt(it.batch_id) : null,
            qty: Number(it.qty),
            sale_rate: Number(it.sale_rate),
            taxable_amount: Number(it.taxable_amount),
            tax_amount: Number(it.tax_amount),
            total_amount: Number(it.total_amount),
            reason: it.reason || null,
        })),
    };

    const submitBtn = document.getElementById("btn-submit-sales-return");
    const conflictBanner = document.getElementById("sr-idem-conflict-banner");
    if (conflictBanner) conflictBanner.style.display = "none";

    srIsSubmitting = true;
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "⏳ Posting Sales Return...";
    }

    try {
        const res = await fetch("/api/sales/returns", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Idempotency-Key": srIdempotencyKey,
            },
            body: JSON.stringify(payload),
        });

        if (res.ok) {
            const created = await res.json();
            const replayTag = created.idempotent_replay ? " (already processed)" : "";
            showToast(`✓ Sales Return ${created.return_no || "#" + created.return_id} posted${replayTag}! Net: ₹${Number(srPreviewData.net_amount).toFixed(2)}`, "success", 6000);
            srIdempotencyKey = null;
            closeCreateSalesReturnModal();

            // Refresh return history, customer balances, inventory stock, and day book
            await Promise.all([
                loadSalesReturns(),
                typeof refreshProductList === "function" ? refreshProductList() : Promise.resolve(),
                typeof loadInventory === "function" ? loadInventory() : Promise.resolve(),
                typeof refreshCustomerList === "function" ? refreshCustomerList() : Promise.resolve(),
                typeof loadFarmerStatusList === "function" ? loadFarmerStatusList() : Promise.resolve(),
                typeof loadDayBook === "function" ? loadDayBook() : Promise.resolve(),
            ]);

            if (created.return_id) {
                openSalesReturnDetail(created.return_id);
            }
        } else if (res.status === 409) {
            const errData = await res.json().catch(() => ({}));
            const msg = errData.detail || "Idempotency key was already used with different sales return details.";
            if (conflictBanner) {
                const msgEl = document.getElementById("sr-idem-conflict-msg");
                if (msgEl) msgEl.textContent = `Idempotency Conflict (409): ${msg}`;
                conflictBanner.style.display = "block";
            }
            showToast(`Conflict detected (409): ${msg}`, "warning", 7000);
        } else {
            const errData = await res.json().catch(() => ({}));
            const detail = errData.detail
                ? (Array.isArray(errData.detail) ? errData.detail.map(d => d.msg || JSON.stringify(d)).join("; ") : String(errData.detail))
                : `Server error (${res.status})`;
            showToast(`Sales return rejected: ${detail}`, "error", 7000);
        }
    } catch (err) {
        console.error("Network error posting sales return:", err);
        showToast(`Network error posting sales return: ${err.message || err}. You can safely retry with the same request.`, "error", 7000);
    } finally {
        srIsSubmitting = false;
        if (submitBtn) {
            submitBtn.textContent = "✓ Confirm & Post Sales Return";
        }
        updateSalesReturnPreviewDisplay();
    }
}

async function openSalesReturnDetail(returnId) {
    if (!returnId || !hasValidAuthSession()) return;
    const modal = document.getElementById("modal-sales-return-detail");
    if (!modal) return;

    try {
        const res = await fetch(`/api/sales/returns/${returnId}`);
        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            showToast(errData.detail || "Could not load sales return details.", "error");
            return;
        }
        const ret = await res.json();
        const s = currentCompanySettings || {};
        const shopName = (s.company_name || "").trim() || "Agri-Input Shop";

        const setEl = (id, text) => {
            const el = document.getElementById(id);
            if (el) el.textContent = text;
        };

        setEl("srd-modal-title", `📄 Sales Return Summary — ${ret.return_no || "#" + ret.return_id}`);
        setEl("srd-shop-name", shopName);
        setEl("srd-shop-details", typeof buildShopAddressText === "function" ? (buildShopAddressText(s) || "Agri-Input Shop") : "Agri-Input Shop");
        setEl("srd-shop-gstin", s.gstin || "-");
        setEl("srd-auth-shop", `For ${shopName}`);

        setEl("srd-return-no", ret.return_no || `#${ret.return_id}`);
        setEl("srd-return-date", ret.return_date || "-");
        setEl("srd-customer-name", ret.customer_name || `Customer #${ret.customer_id}`);
        const custMeta = [ret.customer_village, ret.customer_mobile ? `Mobile: ${ret.customer_mobile}` : ""].filter(Boolean).join(" | ") || "-";
        setEl("srd-customer-meta", custMeta);

        setEl("srd-orig-invoice", ret.original_invoice_no || (ret.sale_id ? `Sale #${ret.sale_id}` : "-"));
        setEl("srd-orig-mode", formatPaymentModeLabel(ret.original_payment_mode));
        setEl("srd-voucher-ref", ret.voucher_no || (ret.voucher_id ? `Voucher #${ret.voucher_id}` : "-"));

        const itemsTbody = document.getElementById("srd-items-tbody");
        if (itemsTbody) {
            const items = Array.isArray(ret.items) ? ret.items : [];
            itemsTbody.innerHTML = items.length === 0
                ? '<tr><td colspan="9" class="text-center text-muted">No returned items found.</td></tr>'
                : items.map((it, idx) => `
                    <tr style="border-bottom: 1px solid #e2e8f0;">
                        <td style="padding: 6px 8px;">${idx + 1}</td>
                        <td style="padding: 6px 8px;"><strong>${escapeHtml(it.product_name || "Product #" + it.product_id)}</strong></td>
                        <td style="padding: 6px 8px;">${escapeHtml(it.batch_no || "-")}</td>
                        <td style="padding: 6px 8px; text-align: right;">${Number(it.qty || 0)}</td>
                        <td style="padding: 6px 8px; text-align: right;">₹${Number(it.sale_rate || 0).toFixed(2)}</td>
                        <td style="padding: 6px 8px; text-align: right;">₹${Number(it.taxable_amount || 0).toFixed(2)}</td>
                        <td style="padding: 6px 8px; text-align: right;">₹${Number(it.tax_amount || 0).toFixed(2)}</td>
                        <td style="padding: 6px 8px; text-align: right; font-weight: 700;">₹${Number(it.total_amount || 0).toFixed(2)}</td>
                        <td style="padding: 6px 8px; font-size: 11.5px; color: #475569;">${escapeHtml(it.reason || "-")}</td>
                    </tr>
                `).join("");
        }

        const settleParts = [];
        if (Number(ret.receivable_reduction || 0) > 0) {
            settleParts.push(`Receivable / Credit Adjustment: ₹${Number(ret.receivable_reduction).toFixed(2)} deducted from customer balance`);
        }
        if (Number(ret.refund_amount || 0) > 0) {
            settleParts.push(`Refund: ₹${Number(ret.refund_amount).toFixed(2)} via ${ret.refund_account_name || "Cash/Bank"}`);
        }
        setEl("srd-settlement-summary", settleParts.length > 0 ? settleParts.join(" + ") : "Recorded via accounting voucher");

        const remEl = document.getElementById("srd-remarks-row");
        if (remEl) remEl.textContent = ret.remarks ? `Remarks: ${ret.remarks}` : "";

        setEl("srd-total-taxable", `₹${Number(ret.total_taxable || 0).toFixed(2)}`);
        setEl("srd-total-cgst", `₹${Number(ret.total_cgst || 0).toFixed(2)}`);
        setEl("srd-total-sgst", `₹${Number(ret.total_sgst || 0).toFixed(2)}`);
        setEl("srd-total-igst", `₹${Number(ret.total_igst || 0).toFixed(2)}`);
        const ro = Number(ret.round_off || 0);
        setEl("srd-total-roundoff", `${ro >= 0 ? "+" : "-"}₹${Math.abs(ro).toFixed(2)}`);
        setEl("srd-total-net", `₹${Number(ret.net_amount || 0).toFixed(2)}`);

        modal.style.display = "flex";
    } catch (err) {
        console.error("Failed to open sales return detail:", err);
        showToast("Error loading sales return detail: " + (err.message || err), "error");
    }
}

function closeSalesReturnDetailModal() {
    const modal = document.getElementById("modal-sales-return-detail");
    if (modal) modal.style.display = "none";
    if (typeof clearPrintTargetClasses === "function") clearPrintTargetClasses();
}

function printSalesReturnDetail() {
    printSection("sales-return");
}

// ----------------- Inventory Tab & Stock Management -----------------
let rawInventoryItems = [];

async function loadInventory() {
    if (!hasValidAuthSession()) return;
    const sessionVer = authSessionVersion;
    try {
        await refreshProductList();
        populateStockProductDropdown();
        if (!hasValidAuthSession() || sessionVer !== authSessionVersion) return;

        const res = await fetch("/api/inventory/stock-summary");
        if (!res.ok || !hasValidAuthSession() || sessionVer !== authSessionVersion) return;
        const stockData = await res.json();
        if (!hasValidAuthSession() || sessionVer !== authSessionVersion) return;
        rawInventoryItems = Array.isArray(stockData) ? stockData : [];
        filterInventoryTable();

        // Load Expiry Alerts
        const alertRes = await fetch("/api/inventory/expiry-alerts?days=90");
        if (!alertRes.ok || !hasValidAuthSession() || sessionVer !== authSessionVersion) return;
        const alertsData = await alertRes.json();
        const alerts = Array.isArray(alertsData) ? alertsData : [];
        const alertBox = document.getElementById("inv-expiry-alerts");
        if (alertBox) {
            if (alerts.length === 0) {
                alertBox.innerHTML = '<span class="badge badge-success">✓ No products expiring within 90 days.</span>';
            } else {
                alertBox.innerHTML = alerts.map(a => `
                    <span class="badge badge-expiring" style="margin-right: 6px; padding: 4px 8px; display: inline-block; margin-bottom: 4px;">
                        ⚠️ <b>${escapeHtml(a.product_name)}</b> (Batch: ${escapeHtml(a.batch_no)}) Exp: ${escapeHtml(a.exp_date)} (In ${a.days_to_expiry} days) - Stock: ${a.current_qty}
                    </span>
                `).join("");
            }
        }
    } catch (err) {
        console.error("Failed to load inventory:", err);
    }
}

function filterInventoryTable() {
    const tbody = document.getElementById("inv-table-tbody");
    if (!tbody) return;

    const catFilter = document.getElementById("inv-filter-cat")?.value || "";
    const search = document.getElementById("inv-search-input")?.value.trim().toLowerCase() || "";

    let filtered = Array.isArray(rawInventoryItems) ? rawInventoryItems : [];
    if (catFilter) {
        filtered = filtered.filter(item => String(item.category_id) === String(catFilter));
    }
    if (search) {
        filtered = filtered.filter(item => 
            (item.product_name || "").toLowerCase().includes(search) ||
            (item.batch_no || "").toLowerCase().includes(search) ||
            (item.manufacturer_name || "").toLowerCase().includes(search) ||
            (item.category_name || "").toLowerCase().includes(search)
        );
    }

    if (filtered.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted" style="padding: 20px;">No stock records found matching filters. Click <b>+ Add Stock</b> to add new inventory.</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map(item => {
        const hasStock = item.current_qty > 0 && item.batch_no && item.batch_no !== 'N/A';
        const batchBadge = hasStock 
            ? `<span class="badge badge-fefo">${item.batch_no}</span>`
            : `<span class="badge" style="background:#fee2e2; color:#991b1b; font-weight:700;">Out of Stock (No Batch)</span>`;
        const expDisplay = hasStock && item.exp_date ? item.exp_date : `<span class="text-muted">-</span>`;
        const qtyColor = item.current_qty <= 0 ? '#dc2626' : (item.current_qty <= 5 ? '#d97706' : '#15803d');
        const passBatch = hasStock ? item.batch_no : '';
        const passExp = hasStock && item.exp_date ? item.exp_date : '';

        return `
            <tr style="${!hasStock ? 'background: #fffbeb;' : ''}">
                <td><b>${item.product_name}</b></td>
                <td><span class="badge" style="background:#e0f2fe; color:#0369a1; font-size:11px;">${item.category_name || '-'}</span></td>
                <td>${item.manufacturer_name || '-'}</td>
                <td>${batchBadge}</td>
                <td>${expDisplay}</td>
                <td class="text-center font-bold" style="color: ${qtyColor}; font-size: 13.5px;">
                    <b>${item.current_qty}</b> ${item.unit_symbol || ''}
                </td>
                <td class="text-right">₹${Number(item.purchase_rate || 0).toFixed(2)}</td>
                <td class="text-right" style="font-weight:600; color:#047857;">₹${Number(item.sale_rate || 0).toFixed(2)}</td>
                <td class="text-right" style="font-weight:700;">₹${Number(item.purchase_value || 0).toFixed(2)}</td>
                <td class="text-center" style="white-space: nowrap;">
                    <button class="btn btn-primary btn-sm" onclick="openAddStockModal(${item.product_id}, '${passBatch}', ${item.purchase_rate || 0}, ${item.sale_rate || 0}, ${item.mrp || item.sale_rate || 0}, '${passExp}')" style="padding: 4px 10px; font-size: 11px; font-weight: 700; ${!hasStock ? 'background: #15803d; border-color: #15803d;' : ''}">
                        ${hasStock ? '+ Add Qty' : '+ Add Stock'}
                    </button>
                    <button class="btn btn-sm btn-outline-danger" onclick="deleteProductCatalogItem(${item.product_id}, '${(item.product_name || "").replace(/'/g, "\\'")}')" style="padding: 3px 8px; font-size: 11px; font-weight: 700; margin-left: 4px;">
                        🗑️ Delete
                    </button>
                </td>
            </tr>
        `;
    }).join("");
}

// ----------------- Add Direct Stock Modal Logic -----------------
function populateStockProductDropdown() {
    const sel = document.getElementById("stock-prod-select");
    if (!sel) return;

    if (!allProducts || allProducts.length === 0) {
        sel.innerHTML = '<option value="">No products available. Please add products in catalog first.</option>';
        return;
    }

    sel.innerHTML = '<option value="">-- Select Product --</option>' + 
        allProducts.map(p => `<option value="${p.product_id}">${p.product_name} (${p.category_name || ''} • ${p.unit_symbol || ''})</option>`).join("");
}

async function openAddStockModal(productId = null, batchNo = "", purRate = 0, saleRate = 0, mrp = 0, expDate = "") {
    await refreshProductList();
    populateStockProductDropdown();
    const modal = document.getElementById("modal-add-stock");
    if (!modal) return;
    modal.style.display = "flex";

    const prodSelect = document.getElementById("stock-prod-select");
    const qtyInput = document.getElementById("stock-add-qty");
    const batchInput = document.getElementById("stock-add-batch");
    const purInput = document.getElementById("stock-add-pur-rate");
    const saleInput = document.getElementById("stock-add-sale-rate");
    const mrpInput = document.getElementById("stock-add-mrp");
    const expInput = document.getElementById("stock-add-exp");
    const mfgInput = document.getElementById("stock-add-mfg");
    const remarksInput = document.getElementById("stock-add-remarks");

    qtyInput.value = "";
    remarksInput.value = "Direct Stock Addition";

    const todayStr = new Date().toISOString().split("T")[0];
    if (mfgInput) mfgInput.value = todayStr;

    if (productId) {
        prodSelect.value = productId;
        batchInput.value = batchNo || "";
        purInput.value = purRate || "";
        saleInput.value = saleRate || "";
        mrpInput.value = mrp || "";
        expInput.value = (expDate && expDate !== "-") ? expDate : "";
    } else {
        prodSelect.value = "";
        batchInput.value = "";
        purInput.value = "";
        saleInput.value = "";
        mrpInput.value = "";
        expInput.value = "";
    }

    setTimeout(() => {
        if (productId) {
            qtyInput.focus();
        } else {
            prodSelect.focus();
        }
    }, 150);
}

function closeAddStockModal() {
    const modal = document.getElementById("modal-add-stock");
    if (modal) modal.style.display = "none";
}

function onStockProductChanged() {
    const prodId = Number(document.getElementById("stock-prod-select")?.value);
    if (!prodId) return;

    const prod = allProducts.find(p => Number(p.product_id) === prodId);
    if (!prod) return;

    const purInput = document.getElementById("stock-add-pur-rate");
    const saleInput = document.getElementById("stock-add-sale-rate");
    const mrpInput = document.getElementById("stock-add-mrp");
    const batchInput = document.getElementById("stock-add-batch");

    if (purInput) purInput.value = prod.default_purchase_rate || 0;
    if (saleInput) saleInput.value = prod.default_sale_rate || 0;
    if (mrpInput) mrpInput.value = prod.default_mrp || prod.default_sale_rate || 0;

    if (batchInput && !batchInput.value) {
        const todayNum = new Date().toISOString().slice(2,10).replace(/-/g, "");
        batchInput.value = `STK-${todayNum}-${prodId}`;
    }
}

async function submitAddDirectStock() {
    const prodId = Number(document.getElementById("stock-prod-select")?.value);
    const qty = parseFloat(document.getElementById("stock-add-qty")?.value || "0");
    const batchNo = document.getElementById("stock-add-batch")?.value.trim() || "";
    const purRate = parseFloat(document.getElementById("stock-add-pur-rate")?.value || "0");
    const saleRate = parseFloat(document.getElementById("stock-add-sale-rate")?.value || "0");
    const mrp = parseFloat(document.getElementById("stock-add-mrp")?.value || "0");
    const expDate = document.getElementById("stock-add-exp")?.value || null;
    const mfgDate = document.getElementById("stock-add-mfg")?.value || null;
    const remarks = document.getElementById("stock-add-remarks")?.value.trim() || "Manual Stock Addition";
    const btnSubmit = document.getElementById("btn-save-stock");

    if (!prodId) {
        showToast("Please select a product.", "warning");
        return;
    }
    if (!qty || qty <= 0) {
        showToast("Quantity must be greater than 0.", "warning");
        return;
    }

    if (btnSubmit) {
        btnSubmit.disabled = true;
        btnSubmit.textContent = "⏳ Saving stock...";
    }

    try {
        const res = await fetch("/api/inventory/add-stock", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                product_id: prodId,
                qty: qty,
                batch_no: batchNo || null,
                purchase_rate: purRate,
                sale_rate: saleRate,
                mrp: mrp,
                mfg_date: mfgDate,
                exp_date: expDate,
                remarks: remarks
            })
        });

        const data = await res.json();
        if (res.ok && data.success) {
            showToast(`✓ ${data.message || "Stock added to inventory successfully!"}`, "success");
            closeAddStockModal();
            await refreshProductList();
            await loadInventory();
            await loadMasterTables();
            notifyOtherTabsDataChanged();
        } else {
            showToast("Error: " + (data.detail || "Could not add stock."), "error");
        }
    } catch (err) {
        showToast("Network error: " + err.message, "error");
    } finally {
        if (btnSubmit) {
            btnSubmit.disabled = false;
            btnSubmit.textContent = "+ Save Stock";
        }
    }
}


// ----------------- Accounts & Day Book Tab -----------------
async function loadDayBook() {
    if (!hasValidAuthSession()) return;
    const today = new Date().toISOString().split("T")[0];
    const dateInput = document.getElementById("acc-daybook-date");
    const dateStr = dateInput ? dateInput.value || today : today;
    if (dateInput && !dateInput.value) dateInput.value = today;

    try {
        const res = await fetch(`/api/accounting/day-book?date=${dateStr}`);
        if (!res.ok || !hasValidAuthSession()) return;
        const data = await res.json();

        document.getElementById("db-sales").innerText = `₹${data.total_sales.toFixed(2)} (${data.sales_count} bills)`;
        document.getElementById("db-purchases").innerText = `₹${data.total_purchases.toFixed(2)} (${data.purchases_count} inwards)`;
        document.getElementById("db-expenses").innerText = `₹${data.total_expenses.toFixed(2)}`;
    } catch (err) {
        console.error(err);
    }
}

// ----------------- Phase 2B-5D-3: Financial Entry Idempotency State & Persistence -----------------
let currentCustomerReceiptIdempotencyKey = null;
let currentQuickFarmerPaymentIdempotencyKey = null;
let currentSupplierPaymentIdempotencyKey = null;
let currentExpenseIdempotencyKey = null;

let isSubmittingCustomerReceipt = false;
let isSubmittingQuickFarmerPayment = false;
let isSubmittingSupplierPayment = false;
let isSubmittingExpense = false;

const FIN_PENDING_STORAGE_KEYS = {
    customerReceipt: "krushidhan_pending_customer_receipt",
    quickFarmerPayments: "krushidhan_pending_quick_farmer_payments",
    supplierPayments: "krushidhan_pending_supplier_payments",
    shopExpense: "krushidhan_pending_shop_expense",
};

function _readSinglePendingFinRecord(storageKey) {
    try {
        const raw = localStorage.getItem(storageKey);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== "object" || !parsed.idempotency_key || !parsed.payload) {
            return null;
        }
        return parsed;
    } catch (e) {
        console.warn("Could not read pending financial record:", storageKey, e);
        return null;
    }
}

function _writeSinglePendingFinRecord(storageKey, record) {
    try {
        localStorage.setItem(storageKey, JSON.stringify(record));
        return true;
    } catch (e) {
        console.warn("Could not persist pending financial record:", storageKey, e);
        return false;
    }
}

function _clearSinglePendingFinRecord(storageKey) {
    try {
        localStorage.removeItem(storageKey);
    } catch (e) {
        console.warn("Could not clear pending financial record:", storageKey, e);
    }
}

function _readMapPendingFinRecords(storageKey) {
    try {
        const raw = localStorage.getItem(storageKey);
        if (!raw) return {};
        const parsed = JSON.parse(raw);
        return (parsed && typeof parsed === "object" && !Array.isArray(parsed)) ? parsed : {};
    } catch (e) {
        console.warn("Could not read pending financial map:", storageKey, e);
        return {};
    }
}

function _getMapPendingFinRecord(storageKey, entityId) {
    if (entityId === null || entityId === undefined || entityId === "") return null;
    const map = _readMapPendingFinRecords(storageKey);
    const rec = map[String(entityId)];
    if (!rec || typeof rec !== "object" || !rec.idempotency_key || !rec.payload) return null;
    return rec;
}

function _setMapPendingFinRecord(storageKey, entityId, record) {
    if (entityId === null || entityId === undefined || entityId === "") return false;
    try {
        const map = _readMapPendingFinRecords(storageKey);
        map[String(entityId)] = record;
        localStorage.setItem(storageKey, JSON.stringify(map));
        return true;
    } catch (e) {
        console.warn("Could not persist pending financial map entry:", storageKey, e);
        return false;
    }
}

function _clearMapPendingFinRecord(storageKey, entityId) {
    if (entityId === null || entityId === undefined || entityId === "") return;
    try {
        const map = _readMapPendingFinRecords(storageKey);
        delete map[String(entityId)];
        if (Object.keys(map).length === 0) {
            localStorage.removeItem(storageKey);
        } else {
            localStorage.setItem(storageKey, JSON.stringify(map));
        }
    } catch (e) {
        console.warn("Could not clear pending financial map entry:", storageKey, e);
    }
}

function _setFinSubmitButtonState(btnId, fallbackSelector, submitting, savingLabel = "Saving...") {
    const btn = document.getElementById(btnId)
        || (fallbackSelector && typeof document.querySelector === "function" ? document.querySelector(fallbackSelector) : null);
    if (!btn) return;
    if (submitting) {
        if (!btn.dataset.origText) {
            btn.dataset.origText = btn.textContent || "";
        }
        btn.disabled = true;
        btn.textContent = savingLabel;
    } else {
        btn.disabled = false;
        if (btn.dataset.origText) {
            btn.textContent = btn.dataset.origText;
        }
    }
}

function _showFinIdempotencyBanner(bannerId, msgId, retryBtnId, { status = "uncertain", message = "", showRetry = true } = {}) {
    const banner = document.getElementById(bannerId);
    const msgEl = document.getElementById(msgId);
    const retryBtn = retryBtnId ? document.getElementById(retryBtnId) : null;
    if (msgEl) msgEl.textContent = message;
    if (retryBtn) retryBtn.style.display = showRetry ? "inline-flex" : "none";
    if (banner) {
        if (status === "conflict") {
            banner.style.background = "#fef2f2";
            banner.style.border = "1px solid #ef4444";
            banner.style.color = "#991b1b";
        } else {
            banner.style.background = "#fffbeb";
            banner.style.border = "1px solid #f59e0b";
            banner.style.color = "#92400e";
        }
        banner.style.display = "block";
    }
}

function _hideFinIdempotencyBanner(bannerId) {
    const banner = document.getElementById(bannerId);
    if (banner) banner.style.display = "none";
}

function _isNavigatorOffline() {
    return typeof navigator !== "undefined" && navigator && navigator.onLine === false;
}

function _buildFinUncertainMessage(label, historyHint) {
    return `Could not confirm whether this ${label} was saved (it may already have been recorded). Press Retry Same Request or Save to retry safely without duplicating, or check ${historyHint}.`;
}

function _buildFinConflictMessage(label, historyHint, serverDetail) {
    return `A ${label} from this earlier attempt was already saved with different details. Please inspect ${historyHint} before proceeding.`
        + (serverDetail ? ` (${serverDetail})` : "");
}

function restorePendingCustomerReceiptDraft() {
    const pending = _readSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.customerReceipt);
    if (!pending) return false;

    currentCustomerReceiptIdempotencyKey = pending.idempotency_key;
    const p = pending.payload || {};
    const custEl = document.getElementById("acc-receipt-cust");
    const dateEl = document.getElementById("acc-receipt-date");
    const amtEl = document.getElementById("acc-receipt-amt");
    const modeEl = document.getElementById("acc-receipt-mode");
    if (custEl && p.customer_id !== undefined && p.customer_id !== null) custEl.value = String(p.customer_id);
    if (dateEl && p.receipt_date) dateEl.value = p.receipt_date;
    if (amtEl && p.amount !== undefined && p.amount !== null) amtEl.value = String(p.amount);
    if (modeEl && p.payment_mode) modeEl.value = p.payment_mode;

    const isConflict = pending.status === "conflict";
    _showFinIdempotencyBanner("acc-receipt-idem-banner", "acc-receipt-idem-msg", "btn-acc-receipt-retry", {
        status: isConflict ? "conflict" : "uncertain",
        showRetry: !isConflict && pending.status !== "validation_error",
        message: isConflict
            ? _buildFinConflictMessage("customer receipt", "the Farmer Statement / Day Book", pending.error_detail)
            : (pending.status === "validation_error"
                ? `Previous customer receipt could not be saved: ${pending.error_detail || "Validation error"}. Fix the details or check the Day Book before starting a new receipt.`
                : _buildFinUncertainMessage("customer receipt", "the Farmer Statement / Day Book")),
    });
    return true;
}

function restorePendingShopExpenseDraft() {
    const pending = _readSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.shopExpense);
    if (!pending) return false;

    currentExpenseIdempotencyKey = pending.idempotency_key;
    const p = pending.payload || {};
    const catEl = document.getElementById("acc-exp-cat");
    const amtEl = document.getElementById("acc-exp-amt");
    const dateEl = document.getElementById("acc-exp-date");
    const modeEl = document.getElementById("acc-exp-mode");
    const remEl = document.getElementById("acc-exp-remarks");
    if (catEl && p.category_id !== undefined && p.category_id !== null) catEl.value = String(p.category_id);
    if (amtEl && p.amount !== undefined && p.amount !== null) amtEl.value = String(p.amount);
    if (dateEl && p.expense_date) dateEl.value = p.expense_date;
    if (modeEl && p.payment_mode) modeEl.value = p.payment_mode;
    if (remEl && p.remarks !== undefined && p.remarks !== null) remEl.value = String(p.remarks);

    const isConflict = pending.status === "conflict";
    _showFinIdempotencyBanner("acc-exp-idem-banner", "acc-exp-idem-msg", "btn-acc-exp-retry", {
        status: isConflict ? "conflict" : "uncertain",
        showRetry: !isConflict && pending.status !== "validation_error",
        message: isConflict
            ? _buildFinConflictMessage("daily expense", "the Day Book / Expense History", pending.error_detail)
            : (pending.status === "validation_error"
                ? `Previous expense could not be saved: ${pending.error_detail || "Validation error"}. Fix the details or check Expense History before starting a new expense.`
                : _buildFinUncertainMessage("daily expense", "the Day Book / Expense History")),
    });
    return true;
}

function restorePendingFinancialDrafts() {
    // Restore pending drafts and keys ONLY — NEVER auto-submit financial requests on startup!
    restorePendingCustomerReceiptDraft();
    restorePendingShopExpenseDraft();
}

async function continueCustomerReceiptAsNew() {
    if (isSubmittingCustomerReceipt) return false;
    const confirmed = await showConfirmDialog({
        title: "Continue as New Customer Receipt?",
        message: "A customer receipt from your earlier attempt may already have been saved. Please inspect the Farmer Statement or Day Book first to avoid recording a duplicate receipt.\n\nOnly confirm if you want to start a brand-new receipt.",
        confirmText: "Continue as New Receipt",
        cancelText: "Cancel",
        danger: true,
    });
    if (!confirmed) return false;
    _clearSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.customerReceipt);
    currentCustomerReceiptIdempotencyKey = generateIdempotencyKey();
    _hideFinIdempotencyBanner("acc-receipt-idem-banner");
    showToast("Ready to record as a new customer receipt. Form values have been preserved.", "info", 5500);
    return true;
}

async function retryPendingCustomerReceipt() {
    const pending = _readSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.customerReceipt);
    if (!pending) {
        return submitCustomerReceipt();
    }
    return submitCustomerReceipt(pending);
}

async function submitCustomerReceipt(pendingOverride = null) {
    if (isSubmittingCustomerReceipt) return;

    let reqPayload;
    let idempotencyKey;

    if (pendingOverride && pendingOverride.payload && pendingOverride.idempotency_key) {
        idempotencyKey = pendingOverride.idempotency_key;
        currentCustomerReceiptIdempotencyKey = idempotencyKey;
        reqPayload = { ...pendingOverride.payload, idempotency_key: idempotencyKey };
    } else {
        const custId = parseInt(document.getElementById("acc-receipt-cust")?.value);
        const amount = parseFloat(document.getElementById("acc-receipt-amt")?.value);
        const mode = document.getElementById("acc-receipt-mode")?.value || "CASH";
        const dateStr = document.getElementById("acc-receipt-date")?.value || new Date().toISOString().split("T")[0];

        if (!custId || isNaN(amount) || amount <= 0) {
            showToast("Please select customer and valid amount.", "warning");
            return;
        }

        const existingPending = _readSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.customerReceipt);
        if (!currentCustomerReceiptIdempotencyKey) {
            currentCustomerReceiptIdempotencyKey = existingPending
                ? existingPending.idempotency_key
                : generateIdempotencyKey();
        }
        idempotencyKey = currentCustomerReceiptIdempotencyKey;
        reqPayload = {
            customer_id: custId,
            receipt_date: dateStr,
            amount: amount,
            payment_mode: mode,
            idempotency_key: idempotencyKey,
        };
    }

    // Lock and persist BEFORE any await so page reload mid-flight preserves key & exact payload
    isSubmittingCustomerReceipt = true;
    _setFinSubmitButtonState("btn-acc-save-receipt", 'button[onclick="submitCustomerReceipt()"]', true, "Saving receipt...");
    const pendingRecord = {
        idempotency_key: idempotencyKey,
        payload: reqPayload,
        status: "pending",
        error_code: null,
        error_detail: null,
        saved_at: new Date().toISOString(),
    };
    _writeSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.customerReceipt, pendingRecord);

    try {
        if (_isNavigatorOffline()) {
            const offlineMsg = "You appear to be offline. Receipt cannot be confirmed right now; details and key are preserved locally for safe retry once reconnected.";
            _writeSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.customerReceipt, {
                ...pendingRecord,
                status: "uncertain",
                error_detail: offlineMsg,
            });
            _showFinIdempotencyBanner("acc-receipt-idem-banner", "acc-receipt-idem-msg", "btn-acc-receipt-retry", {
                status: "uncertain",
                showRetry: true,
                message: offlineMsg,
            });
            showToast(offlineMsg, "warning", 7000);
            return;
        }

        let res = null;
        let data = {};
        try {
            res = await fetch("/api/accounting/customer-receipt", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotencyKey,
                },
                body: JSON.stringify(reqPayload),
            });
            data = await res.json().catch(() => ({}));
        } catch (err) {
            const uncertainMsg = _buildFinUncertainMessage("customer receipt", "the Farmer Statement / Day Book");
            _writeSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.customerReceipt, {
                ...pendingRecord,
                status: "uncertain",
                error_detail: err.message || "Network error",
            });
            _showFinIdempotencyBanner("acc-receipt-idem-banner", "acc-receipt-idem-msg", "btn-acc-receipt-retry", {
                status: "uncertain",
                showRetry: true,
                message: uncertainMsg,
            });
            showToast(uncertainMsg, "warning", 7500);
            return;
        }

        if (res.ok && data.success !== false) {
            const vchRef = data.voucher_no || (data.voucher_id ? `#${data.voucher_id}` : "");
            const isReplay = data.idempotent_replay === true;

            _clearSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.customerReceipt);
            currentCustomerReceiptIdempotencyKey = null;
            _hideFinIdempotencyBanner("acc-receipt-idem-banner");

            const amtEl = document.getElementById("acc-receipt-amt");
            if (amtEl) amtEl.value = "";

            if (isReplay) {
                showToast(
                    `Customer receipt was already saved (Voucher ${vchRef}). Duplicate prevented.`,
                    "info",
                    6500
                );
            } else {
                showToast(
                    `Receipt of ₹${reqPayload.amount} recorded (Voucher ${vchRef})! Customer ledger updated.`,
                    "success",
                    6000
                );
            }

            // Isolated post-save refreshes: UI errors here never invalidate a confirmed HTTP 200
            try { await refreshCustomerList(); } catch (e) {}
            try { await loadDayBook(); } catch (e) {}
            try { if (typeof loadCashReconciliation === "function") await loadCashReconciliation(); } catch (e) {}
            try { if (typeof loadFarmerStatusList === "function") await loadFarmerStatusList(); } catch (e) {}
        } else if (res.status === 409 && data.error_code === "IDEMPOTENCY_KEY_CONFLICT") {
            const detail = extractApiErrorMessage(data, "");
            const msg = _buildFinConflictMessage("customer receipt", "the Farmer Statement / Day Book", detail);
            _writeSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.customerReceipt, {
                ...pendingRecord,
                status: "conflict",
                error_code: "IDEMPOTENCY_KEY_CONFLICT",
                error_detail: detail,
            });
            _showFinIdempotencyBanner("acc-receipt-idem-banner", "acc-receipt-idem-msg", "btn-acc-receipt-retry", {
                status: "conflict",
                showRetry: false,
                message: msg,
            });
            showToast(msg, "error", 7500);
        } else if (isTransientHttpStatus(res.status)) {
            const uncertainMsg = _buildFinUncertainMessage("customer receipt", "the Farmer Statement / Day Book");
            _writeSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.customerReceipt, {
                ...pendingRecord,
                status: "uncertain",
                error_detail: `HTTP ${res.status}`,
            });
            _showFinIdempotencyBanner("acc-receipt-idem-banner", "acc-receipt-idem-msg", "btn-acc-receipt-retry", {
                status: "uncertain",
                showRetry: true,
                message: uncertainMsg,
            });
            showToast(uncertainMsg, "warning", 7500);
        } else {
            const errMsg = extractApiErrorMessage(data, `HTTP ${res.status}`);
            _writeSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.customerReceipt, {
                ...pendingRecord,
                status: "validation_error",
                error_detail: errMsg,
            });
            _showFinIdempotencyBanner("acc-receipt-idem-banner", "acc-receipt-idem-msg", "btn-acc-receipt-retry", {
                status: "validation_error",
                showRetry: false,
                message: `Receipt could not be recorded: ${errMsg}. Correct the details or inspect the Day Book before starting a new receipt.`,
            });
            showToast("Failed to record receipt: " + errMsg, "error", 6500);
        }
    } finally {
        isSubmittingCustomerReceipt = false;
        _setFinSubmitButtonState("btn-acc-save-receipt", 'button[onclick="submitCustomerReceipt()"]', false);
    }
}

// ----------------- GST Reports Tab -----------------
async function loadGSTR1() {
    const fromDate = document.getElementById("gst-from-date").value || "2026-01-01";
    const toDate = document.getElementById("gst-to-date").value || new Date().toISOString().split("T")[0];

    try {
        // 1. GSTR-1 B2B
        const b2bRes = await fetch(`/api/gst/gstr1-b2b?from_date=${fromDate}&to_date=${toDate}`);
        const b2bData = await b2bRes.json();
        const b2bTbody = document.getElementById("gst-b2b-tbody");
        if (b2bTbody) {
            b2bTbody.innerHTML = b2bData.map(r => `
                <tr>
                    <td><b>${r.receiver_gstin}</b></td>
                    <td>${r.receiver_name}</td>
                    <td>${r.invoice_no}</td>
                    <td>${r.invoice_date}</td>
                    <td class="text-right">₹${r.invoice_value.toFixed(2)}</td>
                    <td class="text-right">₹${r.total_taxable.toFixed(2)}</td>
                    <td class="text-right">₹${(r.total_cgst + r.total_sgst).toFixed(2)}</td>
                </tr>
            `).join("");
        }

        // 2. HSN Summary
        const hsnRes = await fetch(`/api/gst/hsn-summary?from_date=${fromDate}&to_date=${toDate}`);
        const hsnData = await hsnRes.json();
        const hsnTbody = document.getElementById("gst-hsn-tbody");
        if (hsnTbody) {
            hsnTbody.innerHTML = hsnData.map(r => `
                <tr>
                    <td><b>${r.hsn_code}</b></td>
                    <td>${r.product_name}</td>
                    <td>${r.uqc||''}</td>
                    <td class="text-center">${r.total_qty}</td>
                    <td class="text-right">₹${r.total_taxable.toFixed(2)}</td>
                    <td class="text-right">₹${(r.total_cgst + r.total_sgst).toFixed(2)}</td>
                    <td class="text-right">₹${r.total_amount.toFixed(2)}</td>
                </tr>
            `).join("");
        }
    } catch (err) {
        console.error(err);
    }
}

// ----------------- Master Data Tab -----------------
async function loadMasterTables() {
    await refreshProductList();
    filterProductCatalog();
}

function filterProductCatalog() {
    const searchInput = document.getElementById("m-prod-search");
    const catSelect = document.getElementById("m-prod-filter-cat");
    const countEl = document.getElementById("m-prod-count");
    const tbody = document.getElementById("m-prod-tbody");

    if (!tbody) return;

    const query = searchInput ? searchInput.value.trim().toLowerCase() : "";
    const catFilter = catSelect ? catSelect.value : "";

    let filtered = allProducts || [];
    if (query) {
        filtered = filtered.filter(p => 
            (p.product_name && p.product_name.toLowerCase().includes(query)) ||
            (p.hsn_code && p.hsn_code.toLowerCase().includes(query)) ||
            (p.manufacturer_name && p.manufacturer_name.toLowerCase().includes(query)) ||
            (p.category_name && p.category_name.toLowerCase().includes(query))
        );
    }
    if (catFilter) {
        filtered = filtered.filter(p => String(p.category_id) === String(catFilter));
    }

    if (countEl) {
        countEl.textContent = filtered.length;
    }

    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="8" class="text-center" style="padding: 24px; color: #64748b;">No products found matching "${query || 'filter'}".</td></tr>`;
        return;
    }

    tbody.innerHTML = filtered.map(p => {
        const stockVal = parseFloat(p.total_stock) || 0;
        let stockBadge = `<span class="badge badge-success">${stockVal} ${p.unit_symbol || 'Nos'}</span>`;
        if (stockVal <= 0) {
            stockBadge = `<span class="badge badge-danger">0 Out of Stock</span>`;
        } else if (stockVal <= (p.min_stock_alert || 5)) {
            stockBadge = `<span class="badge badge-warning">${stockVal} ${p.unit_symbol || 'Nos'} (Low)</span>`;
        }

        const purRate = parseFloat(p.default_purchase_rate) || 0;
        const saleRate = parseFloat(p.default_sale_rate) || 0;
        const mrp = parseFloat(p.default_mrp) || 0;
        const totalTaxRate = (Number(p.cgst_rate || 0) + Number(p.sgst_rate || 0)) || Number(p.igst_rate || 0);

        return `
            <tr>
                <td>
                    <div style="font-weight: 700; color: #1e293b;">${p.product_name}</div>
                    <small style="color: #64748b;">ID: #${p.product_id} • GST: ${totalTaxRate}%</small>
                </td>
                <td><span class="badge" style="background:#e0f2fe; color:#0369a1; font-weight:600;">${p.category_name || 'General'}</span></td>
                <td><span style="font-weight: 500;">${p.manufacturer_name || '-'}</span></td>
                <td><code>${p.hsn_code || '-'}</code></td>
                <td class="text-right" style="color: #475569;">₹${purRate.toFixed(2)}</td>
                <td class="text-right" style="font-weight: 700; color: #047857;">₹${saleRate.toFixed(2)}</td>
                <td class="text-right" style="color: #334155;">₹${mrp.toFixed(2)}</td>
                <td class="text-center">${stockBadge}</td>
                <td class="text-center">
                    <button class="btn btn-sm btn-outline-danger" onclick="deleteProductCatalogItem(${p.product_id}, '${(p.product_name || "").replace(/'/g, "\\'")}')" style="padding: 3px 8px; font-size: 11px; font-weight: 700;">
                        🗑️ Delete
                    </button>
                </td>
            </tr>
        `;
    }).join("");
}

async function createMasterProduct() {
    const rawName = document.getElementById("m-prod-name").value.trim();
    const catId = parseInt(document.getElementById("m-prod-cat").value) || null;
    const mfgId = parseInt(document.getElementById("m-prod-mfg").value) || null;
    const hsn = document.getElementById("m-prod-hsn").value.trim();
    const unitId = parseInt(document.getElementById("m-prod-unit").value) || 1;
    const taxSelect = document.getElementById("m-prod-tax");
    const taxVal = taxSelect ? taxSelect.value.trim() : "";
    const taxId = taxVal ? parseInt(taxVal, 10) : null;
    const purRate = parseFloat(document.getElementById("m-prod-pur-rate").value) || 0;
    const saleRate = parseFloat(document.getElementById("m-prod-sale-rate").value) || 0;
    const mrp = parseFloat(document.getElementById("m-prod-mrp").value) || 0;
    const alertQty = parseFloat(document.getElementById("m-prod-alert")?.value) || 5;

    let packSize = "";
    const packSelect = document.getElementById("m-prod-pack-size");
    const packCustom = document.getElementById("m-prod-pack-custom");
    if (packSelect) {
        if (packSelect.value === "__CUSTOM__" && packCustom) {
            packSize = packCustom.value.trim();
        } else {
            packSize = packSelect.value.trim();
        }
    }

    if (!rawName) {
        showToast("Product Name is required!", "warning");
        const nameEl = document.getElementById("m-prod-name");
        if (nameEl) nameEl.focus();
        return;
    }

    if (!catId) {
        showToast("Category is required!", "warning");
        const catEl = document.getElementById("m-prod-cat");
        if (catEl) catEl.focus();
        return;
    }

    if (!taxId || isNaN(taxId) || taxId <= 0) {
        showToast("Please select a GST Rate / Tax Group!", "warning");
        if (taxSelect) taxSelect.focus();
        return;
    }

    let finalName = rawName;
    if (packSize && !finalName.toLowerCase().includes(packSize.toLowerCase())) {
        finalName = `${finalName} (${packSize})`;
    }

    try {
        const res = await fetch("/api/masters/products", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                product_name: finalName,
                category_id: catId,
                manufacturer_id: mfgId,
                hsn_code: hsn,
                unit_id: unitId,
                tax_group_id: taxId,
                default_purchase_rate: purRate,
                default_sale_rate: saleRate,
                default_mrp: mrp,
                min_stock_alert: alertQty
            })
        });
        if (res.ok) {
            const resData = await res.json();
            showToast(`✅ Product "${finalName}" saved to catalog successfully!`, "success");
            document.getElementById("m-prod-name").value = "";
            document.getElementById("m-prod-hsn").value = "";
            if (packCustom) packCustom.value = "";
            document.getElementById("m-prod-pur-rate").value = "0";
            document.getElementById("m-prod-sale-rate").value = "0";
            document.getElementById("m-prod-mrp").value = "0";
            if (document.getElementById("m-prod-alert")) document.getElementById("m-prod-alert").value = "5";
            if (taxSelect) taxSelect.value = "";
            await refreshProductList();
            if (resData && resData.product_id) {
                const stockProdSelect = document.getElementById("stock-prod-select");
                if (stockProdSelect) stockProdSelect.value = resData.product_id;
            }
            notifyOtherTabsDataChanged();
        } else {
            const err = await res.json();
            showToast("Failed to save product: " + (err.detail || "Server error"), "error");
        }
    } catch (err) {
        showToast("Network error: " + err.message, "error");
    }
}

// ----------------- Farmer Status & Ledger Tab -----------------
async function loadFarmerStatusList() {
    if (!hasValidAuthSession()) return;
    try {
        // 1. Fetch villages for filter dropdown
        const villRes = await fetch("/api/masters/farmers/villages");
        if (!villRes.ok || !hasValidAuthSession()) return;
        const villData = await villRes.json();
        const villages = Array.isArray(villData) ? villData : [];
        const villSelect = document.getElementById("fs-filter-village");
        if (villSelect) {
            const curVal = villSelect.value;
            villSelect.innerHTML = '<option value="">All Villages</option>' + 
                villages.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
            villSelect.value = curVal;
        }

        // 2. Fetch all farmers status
        if (!hasValidAuthSession()) return;
        const res = await fetch("/api/masters/farmers/status-list");
        if (!res.ok || !hasValidAuthSession()) return;
        const statusData = await res.json();
        farmerStatusList = Array.isArray(statusData) ? statusData : [];
        filterFarmerStatusList();
    } catch (err) {
        console.error("Error loading farmer status list:", err);
    }
}

function toggleOnlyCreditFilter() {
    isOnlyCreditFilter = !isOnlyCreditFilter;
    const btn = document.getElementById("fs-btn-credit-toggle");
    if (btn) {
        if (isOnlyCreditFilter) {
            btn.className = "btn btn-danger";
            btn.innerText = "✓ Showing Pending Credit Only (Clear)";
        } else {
            btn.className = "btn btn-secondary";
            btn.innerText = "⚠️ Show Only Pending Credit";
        }
    }
    filterFarmerStatusList();
}

function filterFarmerStatusList() {
    const searchName = (document.getElementById("fs-search-name")?.value || "").trim().toLowerCase();
    const selectedVillage = (document.getElementById("fs-filter-village")?.value || "").trim().toLowerCase();
    const list = Array.isArray(farmerStatusList) ? farmerStatusList : [];

    let filtered = list.filter(f => {
        const matchesName = !searchName || 
            f.customer_name.toLowerCase().includes(searchName) || 
            (f.mobile && f.mobile.includes(searchName));
        const matchesVillage = !selectedVillage || (f.village && f.village.toLowerCase() === selectedVillage);
        const matchesCredit = !isOnlyCreditFilter || (f.current_balance > 0);

        return matchesName && matchesVillage && matchesCredit;
    });

    // Update Top KPIs
    let totSales = 0, totPending = 0;
    list.forEach(f => {
        totSales += f.total_purchases_amount;
        if (f.current_balance > 0) totPending += f.current_balance;
    });

    document.getElementById("fs-total-farmers").innerText = list.length;
    document.getElementById("fs-total-sales").innerText = `₹${totSales.toFixed(2)}`;
    document.getElementById("fs-total-pending").innerText = `₹${totPending.toFixed(2)}`;

    // Render Table
    const tbody = document.getElementById("fs-farmers-tbody");
    if (!tbody) return;

    if (filtered.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted" style="padding: 24px;">No matching farmers found.</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map(f => {
        const hasDue = f.current_balance > 0;
        return `
            <tr style="cursor: pointer;" onclick="openFarmerDetail(${f.customer_id})">
                <td><b>${f.customer_name}</b></td>
                <td><span class="badge badge-fefo">${f.village}</span></td>
                <td>${f.mobile || '-'}</td>
                <td class="text-center">${f.total_bills_count} bills</td>
                <td class="text-right">₹${f.total_purchases_amount.toFixed(2)}</td>
                <td class="text-right">₹${f.total_paid_amount.toFixed(2)}</td>
                <td class="text-right">
                    ${hasDue ? `<b style="color: var(--danger); font-size: 13.5px;">₹${f.current_balance.toFixed(2)}</b>` : `<span class="badge badge-success">₹0.00 (Nil)</span>`}
                </td>
                <td class="text-center">
                    <button class="btn btn-primary btn-sm" onclick="event.stopPropagation(); openFarmerDetail(${f.customer_id})">
                        🔍 View Statement
                    </button>
                </td>
            </tr>
        `;
    }).join("");
}

// ----------------- Farmer Statement Modal -----------------
async function openFarmerDetail(customerId) {
    activeFarmerId = customerId;
    try {
        const res = await fetch(`/api/masters/farmers/${customerId}/statement`);
        const data = await res.json();
        if (!res.ok) {
            showToast("Failed to load farmer statement.", "error");
            return;
        }

        const c = data.customer;
        document.getElementById("fd-name").innerText = `👨‍🌾 ${c.customer_name}`;
        document.getElementById("fd-meta").innerText = `Village: ${c.village || 'Local'} | Taluka: ${c.taluka || '-'} | Mobile: ${c.mobile || '-'}`;
        document.getElementById("fd-pending-balance").innerText = `₹${c.current_balance.toFixed(2)}`;
        
        let totPurchases = 0;
        data.invoices.forEach(inv => totPurchases += inv.net_amount);
        document.getElementById("fd-tot-purchases").innerText = `₹${totPurchases.toFixed(2)}`;

        // Restore pending unconfirmed quick payment for THIS farmer if any, else pre-fill pending balance
        const pendingFarmerPay = _getMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.quickFarmerPayments, customerId);
        if (pendingFarmerPay) {
            currentQuickFarmerPaymentIdempotencyKey = pendingFarmerPay.idempotency_key;
            const fp = pendingFarmerPay.payload || {};
            const amtInput = document.getElementById("fd-pay-amount");
            const modeInput = document.getElementById("fd-pay-mode");
            if (amtInput && fp.amount !== undefined && fp.amount !== null) amtInput.value = String(fp.amount);
            if (modeInput && fp.payment_mode) modeInput.value = fp.payment_mode;
            const isConf = pendingFarmerPay.status === "conflict";
            _showFinIdempotencyBanner("fd-pay-idem-banner", "fd-pay-idem-msg", "btn-fd-pay-retry", {
                status: isConf ? "conflict" : "uncertain",
                showRetry: !isConf && pendingFarmerPay.status !== "validation_error",
                message: isConf
                    ? _buildFinConflictMessage("farmer receipt", "this Farmer Statement's Payment Receipts table", pendingFarmerPay.error_detail)
                    : (pendingFarmerPay.status === "validation_error"
                        ? `Previous farmer receipt could not be saved: ${pendingFarmerPay.error_detail || "Validation error"}. Fix the amount or inspect Payment Receipts before starting a new receipt.`
                        : _buildFinUncertainMessage("farmer receipt", "this Farmer Statement's Payment Receipts table")),
            });
        } else {
            currentQuickFarmerPaymentIdempotencyKey = null;
            _hideFinIdempotencyBanner("fd-pay-idem-banner");
            document.getElementById("fd-pay-amount").value = c.current_balance > 0 ? c.current_balance : "";
        }

        // Render Invoices & Goods List
        const invTbody = document.getElementById("fd-invoices-tbody");
        if (data.invoices.length === 0) {
            invTbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted" style="padding: 16px;">No bills generated yet for this farmer.</td></tr>';
        } else {
            invTbody.innerHTML = data.invoices.map(inv => {
                const goodsListHtml = inv.items.map(it => 
                    `<div>• <b>${it.product_name}</b> <span class="text-muted">(Batch: ${it.batch_no})</span> — <b>${it.qty} ${it.unit_symbol||''}</b> @ ₹${it.sale_rate} = ₹${it.total_amount}</div>`
                ).join("");

                return `
                    <tr>
                        <td><b>${inv.invoice_no}</b></td>
                        <td>${inv.sale_date}</td>
                        <td style="font-size: 12px;">${goodsListHtml}</td>
                        <td class="text-right">₹${inv.net_amount.toFixed(2)}</td>
                        <td class="text-right">₹${inv.paid_amount.toFixed(2)}</td>
                        <td class="text-right" style="color: ${inv.due_amount > 0 ? 'var(--danger)' : 'inherit'}; font-weight: 700;">₹${inv.due_amount.toFixed(2)}</td>
                        <td class="text-center no-print">
                            <button type="button" class="btn btn-secondary btn-sm" onclick="openSavedInvoicePreview(${inv.sale_id})" style="margin-right: 4px;">👁️ Bill</button>
                            <button type="button" class="btn btn-primary btn-sm" onclick="downloadInvoicePdf(${inv.sale_id})">🖨️ PDF</button>
                        </td>
                    </tr>
                `;
            }).join("");
        }

        // Render Receipts
        const recTbody = document.getElementById("fd-receipts-tbody");
        if (data.receipts.length === 0) {
            recTbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted" style="padding: 12px;">No payment receipts recorded yet.</td></tr>';
        } else {
            recTbody.innerHTML = data.receipts.map(r => `
                <tr>
                    <td><b>${r.voucher_no}</b></td>
                    <td>${r.voucher_date}</td>
                    <td>${r.narration || 'Payment received'}</td>
                    <td class="text-right" style="color: var(--success); font-weight: 700;">₹${r.total_amount.toFixed(2)}</td>
                </tr>
            `).join("");
        }

        // Show Modal
        document.getElementById("modal-farmer-detail").classList.add("show");
    } catch (err) {
        console.error(err);
        showToast("Error: " + err.message, "error");
    }
}

function closeFarmerDetailModal() {
    document.getElementById("modal-farmer-detail").classList.remove("show");
    // Preserve any unconfirmed pending payment in localStorage; only clear active in-memory pointers
    // so opening a different farmer can never reuse this farmer's key.
    activeFarmerId = null;
    currentQuickFarmerPaymentIdempotencyKey = null;
    _hideFinIdempotencyBanner("fd-pay-idem-banner");
}

async function continueQuickFarmerPaymentAsNew() {
    if (isSubmittingQuickFarmerPayment || !activeFarmerId) return false;
    const confirmed = await showConfirmDialog({
        title: "Continue as New Farmer Receipt?",
        message: "A receipt from your earlier attempt for this farmer may already have been saved. Please inspect the Payment Receipts list in this Farmer Statement first to avoid recording a duplicate receipt.\n\nOnly confirm if you want to start a brand-new receipt.",
        confirmText: "Continue as New Receipt",
        cancelText: "Cancel",
        danger: true,
    });
    if (!confirmed) return false;
    _clearMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.quickFarmerPayments, activeFarmerId);
    currentQuickFarmerPaymentIdempotencyKey = generateIdempotencyKey();
    _hideFinIdempotencyBanner("fd-pay-idem-banner");
    showToast("Ready to record as a new farmer receipt. Form values have been preserved.", "info", 5500);
    return true;
}

async function retryPendingQuickFarmerPayment() {
    if (!activeFarmerId) return;
    const pending = _getMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.quickFarmerPayments, activeFarmerId);
    if (!pending) return submitQuickFarmerPayment();
    return submitQuickFarmerPayment(pending);
}

async function submitQuickFarmerPayment(pendingOverride = null) {
    if (isSubmittingQuickFarmerPayment) return;

    const farmerId = (pendingOverride && pendingOverride.payload && pendingOverride.payload.customer_id)
        ? Number(pendingOverride.payload.customer_id)
        : activeFarmerId;
    if (!farmerId) return;

    let reqPayload;
    let idempotencyKey;

    if (pendingOverride && pendingOverride.payload && pendingOverride.idempotency_key) {
        idempotencyKey = pendingOverride.idempotency_key;
        currentQuickFarmerPaymentIdempotencyKey = idempotencyKey;
        reqPayload = { ...pendingOverride.payload, idempotency_key: idempotencyKey };
    } else {
        const amount = parseFloat(document.getElementById("fd-pay-amount")?.value);
        const mode = document.getElementById("fd-pay-mode")?.value || "CASH";
        const today = new Date().toISOString().split("T")[0];

        if (isNaN(amount) || amount <= 0) {
            showToast("Please enter a valid payment amount greater than 0.", "warning");
            return;
        }

        const existingPending = _getMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.quickFarmerPayments, farmerId);
        if (!currentQuickFarmerPaymentIdempotencyKey) {
            currentQuickFarmerPaymentIdempotencyKey = existingPending
                ? existingPending.idempotency_key
                : generateIdempotencyKey();
        }
        idempotencyKey = currentQuickFarmerPaymentIdempotencyKey;
        reqPayload = {
            customer_id: farmerId,
            receipt_date: today,
            amount: amount,
            payment_mode: mode,
            narration: "Direct collection from Farmer Status Ledger",
            idempotency_key: idempotencyKey,
        };
    }

    isSubmittingQuickFarmerPayment = true;
    _setFinSubmitButtonState("btn-fd-save-receipt", 'button[onclick="submitQuickFarmerPayment()"]', true, "Saving...");
    const pendingRecord = {
        customer_id: farmerId,
        idempotency_key: idempotencyKey,
        payload: reqPayload,
        status: "pending",
        error_code: null,
        error_detail: null,
        saved_at: new Date().toISOString(),
    };
    _setMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.quickFarmerPayments, farmerId, pendingRecord);

    try {
        if (_isNavigatorOffline()) {
            const offlineMsg = "You appear to be offline. Farmer receipt cannot be confirmed right now; details and key are preserved locally for safe retry once reconnected.";
            _setMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.quickFarmerPayments, farmerId, {
                ...pendingRecord,
                status: "uncertain",
                error_detail: offlineMsg,
            });
            _showFinIdempotencyBanner("fd-pay-idem-banner", "fd-pay-idem-msg", "btn-fd-pay-retry", {
                status: "uncertain",
                showRetry: true,
                message: offlineMsg,
            });
            showToast(offlineMsg, "warning", 7000);
            return;
        }

        let res = null;
        let data = {};
        try {
            res = await fetch("/api/accounting/customer-receipt", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotencyKey,
                },
                body: JSON.stringify(reqPayload),
            });
            data = await res.json().catch(() => ({}));
        } catch (err) {
            const uncertainMsg = _buildFinUncertainMessage("farmer receipt", "this Farmer Statement's Payment Receipts table");
            _setMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.quickFarmerPayments, farmerId, {
                ...pendingRecord,
                status: "uncertain",
                error_detail: err.message || "Network error",
            });
            _showFinIdempotencyBanner("fd-pay-idem-banner", "fd-pay-idem-msg", "btn-fd-pay-retry", {
                status: "uncertain",
                showRetry: true,
                message: uncertainMsg,
            });
            showToast(uncertainMsg, "warning", 7500);
            return;
        }

        if (res.ok && data.success !== false) {
            const vchRef = data.voucher_no || (data.voucher_id ? `#${data.voucher_id}` : "");
            const isReplay = data.idempotent_replay === true;

            _clearMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.quickFarmerPayments, farmerId);
            currentQuickFarmerPaymentIdempotencyKey = null;
            _hideFinIdempotencyBanner("fd-pay-idem-banner");

            if (isReplay) {
                showToast(
                    `Farmer receipt was already saved (Voucher ${vchRef}). Duplicate prevented.`,
                    "info",
                    6500
                );
            } else {
                showToast(
                    `Payment of ₹${reqPayload.amount} recorded successfully (Voucher ${vchRef})!`,
                    "success",
                    6000
                );
            }

            try { await openFarmerDetail(farmerId); } catch (e) {}
            try { await loadFarmerStatusList(); } catch (e) {}
            try { await refreshCustomerList(); } catch (e) {}
            try { await loadDayBook(); } catch (e) {}
            try { if (typeof loadCashReconciliation === "function") await loadCashReconciliation(); } catch (e) {}
        } else if (res.status === 409 && data.error_code === "IDEMPOTENCY_KEY_CONFLICT") {
            const detail = extractApiErrorMessage(data, "");
            const msg = _buildFinConflictMessage("farmer receipt", "this Farmer Statement's Payment Receipts table", detail);
            _setMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.quickFarmerPayments, farmerId, {
                ...pendingRecord,
                status: "conflict",
                error_code: "IDEMPOTENCY_KEY_CONFLICT",
                error_detail: detail,
            });
            _showFinIdempotencyBanner("fd-pay-idem-banner", "fd-pay-idem-msg", "btn-fd-pay-retry", {
                status: "conflict",
                showRetry: false,
                message: msg,
            });
            showToast(msg, "error", 7500);
        } else if (isTransientHttpStatus(res.status)) {
            const uncertainMsg = _buildFinUncertainMessage("farmer receipt", "this Farmer Statement's Payment Receipts table");
            _setMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.quickFarmerPayments, farmerId, {
                ...pendingRecord,
                status: "uncertain",
                error_detail: `HTTP ${res.status}`,
            });
            _showFinIdempotencyBanner("fd-pay-idem-banner", "fd-pay-idem-msg", "btn-fd-pay-retry", {
                status: "uncertain",
                showRetry: true,
                message: uncertainMsg,
            });
            showToast(uncertainMsg, "warning", 7500);
        } else {
            const errMsg = extractApiErrorMessage(data, `HTTP ${res.status}`);
            _setMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.quickFarmerPayments, farmerId, {
                ...pendingRecord,
                status: "validation_error",
                error_detail: errMsg,
            });
            _showFinIdempotencyBanner("fd-pay-idem-banner", "fd-pay-idem-msg", "btn-fd-pay-retry", {
                status: "validation_error",
                showRetry: false,
                message: `Receipt could not be recorded: ${errMsg}. Correct the amount or inspect Payment Receipts before starting a new receipt.`,
            });
            showToast("Failed to record payment: " + errMsg, "error", 6500);
        }
    } finally {
        isSubmittingQuickFarmerPayment = false;
        _setFinSubmitButtonState("btn-fd-save-receipt", 'button[onclick="submitQuickFarmerPayment()"]', false);
    }
}

function sendWhatsAppPaymentReminder() {
    if (!activeFarmerId) return;
    const farmer = farmerStatusList.find(f => f.customer_id === activeFarmerId);
    if (!farmer) return;

    if (farmer.current_balance <= 0) {
        showToast("This farmer has zero pending balance (Nil).", "info");
        return;
    }

    const mobile = (farmer.mobile || "").replace(/\D/g, "");
    const phoneParam = mobile.length === 10 ? `91${mobile}` : mobile;
    const shopTitle = (currentCompanySettings && currentCompanySettings.company_name)
        ? currentCompanySettings.company_name
        : "Our Agri-Input Shop";
    const text = encodeURIComponent(
        `Hello ${farmer.customer_name}, you have a pending credit balance of ₹${farmer.current_balance.toFixed(2)} with ${shopTitle}. Please settle the payment at your earliest convenience. Thank you!`
    );

    const waUrl = phoneParam 
        ? `https://api.whatsapp.com/send?phone=${phoneParam}&text=${text}`
        : `https://api.whatsapp.com/send?text=${text}`;

    openExternalUrl(waUrl, "WhatsApp payment reminder");
}

// ----------------- Settings & Backup Tab -----------------
function applyLoadedCompanyProfileChrome(s) {
    if (!s) return;
    currentCompanySettings = s;
    window.currentCompanySettings = s;

    const brandBadge = document.getElementById("header-brand-badge");
    if (brandBadge && s.company_name) {
        const contactText = s.mobile ? ` • Mobile: ${s.mobile}` : "";
        brandBadge.textContent = `${s.company_name}${contactText} • Fertilizer • Seed • Pesticide`;
    }
    const loginShopName = document.getElementById("login-shop-name");
    if (loginShopName && s.company_name) {
        loginShopName.textContent = s.company_name;
    }
    const loginPropBadge = document.getElementById("login-prop-badge");
    if (loginPropBadge) {
        if (s.owner_name && s.mobile) {
            loginPropBadge.textContent = `👤 ${s.owner_name} • 📞 Mobile: ${s.mobile}`;
        } else if (s.owner_name) {
            loginPropBadge.textContent = `👤 ${s.owner_name}`;
        } else if (s.mobile) {
            loginPropBadge.textContent = `📞 Mobile: ${s.mobile}`;
        }
    }
    if (s.company_name) {
        document.title = `${s.company_name} - Agri-Input Shop ERP`;
    }
}

let settingsDirtyFields = new Set();

function initSettingsDirtyTracking() {
    const settingsTab = document.getElementById("tab-settings");
    if (!settingsTab || settingsTab.dataset.dirtyBound === "1") return;
    settingsTab.dataset.dirtyBound = "1";
    const markDirty = (e) => {
        if (e.target && e.target.id && e.target.id.startsWith("set-")) {
            settingsDirtyFields.add(e.target.id);
        }
    };
    settingsTab.addEventListener("input", markDirty);
    settingsTab.addEventListener("change", markDirty);
}

async function loadSettings(forceOverwrite = false) {
    if (!hasValidAuthSession()) return null;
    try {
        const res = await fetch("/api/system/settings");
        if (!res.ok || !hasValidAuthSession()) return null;
        const s = await res.json();

        const setVal = (id, val) => {
            if (!forceOverwrite && settingsDirtyFields.has(id)) return;
            const el = document.getElementById(id);
            if (el) el.value = val ?? "";
        };

        setVal("set-shop-name", s.company_name || "");
        setVal("set-owner-name", s.owner_name || "");
        setVal("set-gstin", s.gstin || "");
        setVal("set-address", s.address || "");
        setVal("set-city", s.city || "");
        setVal("set-state", s.state || "Maharashtra");
        setVal("set-pincode", s.pincode || "");
        setVal("set-mobile", s.mobile || "");
        setVal("set-email", s.email || "");
        setVal("set-lic-fert", s.dl_fertilizer || "");
        setVal("set-lic-pest", s.dl_pesticide || "");
        setVal("set-lic-seed", s.dl_seed || "");
        setVal("set-terms", s.invoice_terms || "");

        applyLoadedCompanyProfileChrome(s);
        return s;
    } catch (err) {
        console.error(err);
        return null;
    }
}

async function saveSettings() {
    const getVal = (id) => (document.getElementById(id)?.value ?? "").trim();
    const shopName = getVal("set-shop-name");
    if (!shopName) {
        showToast("Shop / Enterprise Name is required.", "warning");
        document.getElementById("set-shop-name")?.focus();
        return;
    }

    const payload = {
        company_name: shopName,
        owner_name: getVal("set-owner-name"),
        gstin: getVal("set-gstin").toUpperCase(),
        address: getVal("set-address"),
        city: getVal("set-city"),
        state: getVal("set-state") || "Maharashtra",
        pincode: getVal("set-pincode"),
        mobile: getVal("set-mobile"),
        email: getVal("set-email"),
        dl_fertilizer: getVal("set-lic-fert"),
        dl_pesticide: getVal("set-lic-pest"),
        dl_seed: getVal("set-lic-seed"),
        invoice_terms: document.getElementById("set-terms")?.value ?? "",
    };

    try {
        const res = await fetch("/api/system/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.success) {
            settingsDirtyFields.clear();
            if (data.settings) {
                applyLoadedCompanyProfileChrome(data.settings);
            }
            await loadSettings(true);
            showToast("Shop settings and profile saved successfully!", "success");
        } else {
            showToast("Error saving shop profile: " + (data.detail || `HTTP ${res.status}`), "error");
        }
    } catch (err) {
        showToast("Error saving shop profile: " + err.message, "error");
    }
}

async function triggerDatabaseBackup(buttonEl = null) {
    if (!hasValidAuthSession()) {
        showToast("Session expired. Please login again.", "warning");
        return false;
    }
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Database backup is restricted to administrators.", "error");
        return false;
    }

    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Backing up...";
    }

    try {
        const res = await fetch("/api/system/backup", { method: "POST" });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.success) {
            const label = data.filename || data.file_path || "backup.db";
            const sizeNote = data.size_display ? ` (${data.size_display})` : "";
            showToast(`✓ Verified backup created: ${label}${sizeNote}`, "success");
            if (typeof _setBackupManagerFeedback === "function") {
                _setBackupManagerFeedback(
                    `Created verified offline backup "${label}"${sizeNote}. Saved in: ${data.file_path || "-"}`,
                    "success"
                );
            }
            if (typeof loadBackupManagerData === "function") {
                await loadBackupManagerData();
            }
            return true;
        } else {
            const msg = "Backup failed: " + (data.detail || `HTTP ${res.status}`);
            showToast(msg, "error");
            if (typeof _setBackupManagerFeedback === "function") {
                _setBackupManagerFeedback(msg, "danger");
            }
            return false;
        }
    } catch (err) {
        const msg = "Backup error: " + err.message;
        showToast(msg, "error");
        if (typeof _setBackupManagerFeedback === "function") {
            _setBackupManagerFeedback(msg, "danger");
        }
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

// ----------------- Profit & Loss Workspace -----------------
function setPnLPeriod(period) {
    const today = new Date();
    const todayStr = today.toISOString().split("T")[0];
    const fromInput = document.getElementById("pnl-from-date");
    const toInput = document.getElementById("pnl-to-date");

    if (period === "today") {
        fromInput.value = todayStr;
        toInput.value = todayStr;
    } else if (period === "week") {
        const weekAgo = new Date(today);
        weekAgo.setDate(today.getDate() - 7);
        fromInput.value = weekAgo.toISOString().split("T")[0];
        toInput.value = todayStr;
    } else if (period === "month") {
        fromInput.value = todayStr.substring(0, 8) + "01";
        toInput.value = todayStr;
    } else if (period === "year") {
        const curYear = today.getFullYear();
        fromInput.value = `${curYear}-04-01`;
        toInput.value = `${curYear + 1}-03-31`;
    } else if (period === "all") {
        fromInput.value = "2020-01-01";
        toInput.value = "2030-12-31";
    }
    loadProfitAndLoss();
}

async function loadProfitAndLoss() {
    if (!hasValidAuthSession()) return;
    const fromDate = document.getElementById("pnl-from-date")?.value || new Date().toISOString().split("T")[0].substring(0, 8) + "01";
    const toDate = document.getElementById("pnl-to-date")?.value || new Date().toISOString().split("T")[0];

    try {
        // 1. Fetch High-Level P&L Summary & Categories
        const res = await fetch(`/api/accounting/profit-and-loss?from_date=${fromDate}&to_date=${toDate}`);
        if (!res.ok || !hasValidAuthSession()) return;
        const data = await res.json();

        // 2. Update KPI Cards
        document.getElementById("pnl-kpi-sales").innerText = `₹${(data.sales_revenue || 0).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
        document.getElementById("pnl-kpi-invoices").innerText = data.total_invoices || 0;
        document.getElementById("pnl-kpi-disc").innerText = `₹${(data.total_discount || 0).toFixed(0)}`;
        
        document.getElementById("pnl-kpi-cogs").innerText = `₹${(data.cogs || 0).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
        
        document.getElementById("pnl-kpi-gp").innerText = `₹${(data.gross_profit || 0).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
        document.getElementById("pnl-kpi-gp-pct").innerText = `${data.gross_margin_pct || 0}% Margin`;
        
        document.getElementById("pnl-kpi-exp").innerText = `₹${(data.total_expenses || 0).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
        
        const npEl = document.getElementById("pnl-kpi-np");
        const npVal = data.net_profit || 0;
        npEl.innerText = `₹${npVal.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
        if (npVal < 0) {
            npEl.style.color = "#dc2626"; // Red for Loss
        } else {
            npEl.style.color = "#7e22ce"; // Purple/Green for Net Profit
        }
        document.getElementById("pnl-kpi-np-pct").innerText = `${data.net_margin_pct || 0}% Net Margin`;

        // 3. Render Multi-Step Trading & P&L Statement
        const stmtTbody = document.getElementById("pnl-statement-tbody");
        if (stmtTbody) {
            let expDetails = "";
            if (data.expenses_breakdown && data.expenses_breakdown.length > 0) {
                expDetails = data.expenses_breakdown.map(e => `
                    <tr style="background: #fafafa;">
                        <td style="padding-left: 24px; color: #475569;">• ${e.category_name}</td>
                        <td class="text-right" style="color: #dc2626;">- ₹${parseFloat(e.total_amount).toFixed(2)}</td>
                    </tr>
                `).join("");
            } else {
                expDetails = `
                    <tr style="background: #fafafa;">
                        <td style="padding-left: 24px; color: #64748b;">• No shop expenses recorded</td>
                        <td class="text-right">₹0.00</td>
                    </tr>
                `;
            }

            stmtTbody.innerHTML = `
                <tr>
                    <td><b>A. Revenue from Operations (Taxable Turnover)</b></td>
                    <td class="text-right" style="font-weight: 700;">₹${(data.sales_revenue || 0).toFixed(2)}</td>
                </tr>
                <tr>
                    <td style="color: #dc2626;"><b>Less: Cost of Goods Sold (COGS - Batch Cost)</b></td>
                    <td class="text-right" style="color: #dc2626; font-weight: 700;">- ₹${(data.cogs || 0).toFixed(2)}</td>
                </tr>
                <tr style="background: #f0fdf4; border-top: 2px solid #86efac; border-bottom: 2px solid #86efac;">
                    <td><b style="color: #047857;">GROSS PROFIT (A - COGS) [${data.gross_margin_pct}% Margin]</b></td>
                    <td class="text-right"><b style="color: #047857; font-size: 14px;">₹${(data.gross_profit || 0).toFixed(2)}</b></td>
                </tr>
                <tr>
                    <td colspan="2" style="font-weight: 700; color: #334155; padding-top: 10px;">B. Operating / Shop Expenses</td>
                </tr>
                ${expDetails}
                <tr style="background: ${npVal >= 0 ? '#faf5ff' : '#fef2f2'}; border-top: 2px solid #cbd5e1; border-bottom: 3px double #334155;">
                    <td><b style="font-size: 13.5px; color: ${npVal >= 0 ? '#7e22ce' : '#dc2626'};">NET OPERATING PROFIT (NP) [${data.net_margin_pct}%]</b></td>
                    <td class="text-right"><b style="font-size: 15px; color: ${npVal >= 0 ? '#7e22ce' : '#dc2626'};">₹${npVal.toFixed(2)}</b></td>
                </tr>
                <tr>
                    <td style="color: #64748b; font-size: 11px;">📦 Closing Stock Valuation (Inventory Asset at Shop)</td>
                    <td class="text-right" style="color: #64748b; font-size: 11px; font-weight: 600;">₹${(data.closing_stock_value || 0).toFixed(2)}</td>
                </tr>
            `;
        }

        // 4. Render Category Margin Breakdown
        const catTbody = document.getElementById("pnl-category-tbody");
        if (catTbody) {
            if (!data.category_breakdown || data.category_breakdown.length === 0) {
                catTbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted" style="padding: 20px;">No sales by category in this period.</td></tr>';
            } else {
                catTbody.innerHTML = data.category_breakdown.map(c => `
                    <tr>
                        <td><b>${c.category_name}</b></td>
                        <td class="text-right">₹${c.sales_revenue.toFixed(2)}</td>
                        <td class="text-right" style="color: #dc2626;">₹${c.cogs.toFixed(2)}</td>
                        <td class="text-right" style="font-weight: 700; color: #047857;">₹${c.gross_profit.toFixed(2)}</td>
                        <td class="text-center"><span class="badge ${c.margin_pct >= 15 ? 'badge-success' : 'badge-warning'}">${c.margin_pct}%</span></td>
                    </tr>
                `).join("");
            }
        }

        // 5. Fetch Product-Wise Profitability Breakdown
        if (!hasValidAuthSession()) return;
        const prodRes = await fetch(`/api/accounting/profit-and-loss/products?from_date=${fromDate}&to_date=${toDate}`);
        if (!prodRes.ok || !hasValidAuthSession()) return;
        const prodData = await prodRes.json();
        pnlAllProducts = Array.isArray(prodData) ? prodData : [];
        filterPnLProducts();

    } catch (err) {
        console.error("Failed to load P&L:", err);
    }
}

function filterPnLProducts() {
    const search = document.getElementById("pnl-prod-search")?.value.trim().toLowerCase() || "";
    const tbody = document.getElementById("pnl-products-tbody");
    if (!tbody) return;

    let filtered = Array.isArray(pnlAllProducts) ? pnlAllProducts : [];
    if (search) {
        filtered = filtered.filter(p => 
            p.product_name.toLowerCase().includes(search) ||
            p.category_name.toLowerCase().includes(search) ||
            p.manufacturer_name.toLowerCase().includes(search)
        );
    }

    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="8" class="text-center text-muted" style="padding: 20px;">No product sales matching "${search}".</td></tr>`;
        return;
    }

    tbody.innerHTML = filtered.map(p => `
        <tr>
            <td><b>${p.product_name}</b></td>
            <td><span class="badge" style="background:#e0f2fe; color:#0369a1;">${p.category_name}</span></td>
            <td>${p.manufacturer_name}</td>
            <td class="text-center"><b>${p.qty_sold}</b> ${p.unit_symbol}</td>
            <td class="text-right">₹${p.sales_revenue.toFixed(2)}</td>
            <td class="text-right" style="color: #dc2626;">₹${p.cogs.toFixed(2)}</td>
            <td class="text-right" style="font-weight: 700; color: #047857;">₹${p.gross_profit.toFixed(2)}</td>
            <td class="text-center"><span class="badge ${p.margin_pct >= 15 ? 'badge-success' : 'badge-fefo'}">${p.margin_pct}%</span></td>
        </tr>
    `).join("");
}

// ============================================================
// SECURITY, AUTHENTICATION & ACCESS CONTROL
// ============================================================

async function loadSetupStatus() {
    try {
        const res = await _rawDesktopFetch("/api/auth/setup-status", {}, { skipAuthRedirect: true, includeAuth: false });
        if (!res.ok) return { needs_setup: false };
        const statusData = await res.json();

        const loginShopName = document.getElementById("login-shop-name");
        const loginPropBadge = document.getElementById("login-prop-badge");
        const headerBrandBadge = document.getElementById("header-brand-badge");

        if (loginShopName && statusData.shop_name) {
            loginShopName.textContent = statusData.shop_name;
        }
        if (headerBrandBadge && statusData.has_company_profile && statusData.shop_name) {
            const mob = statusData.shop_mobile ? ` • Mobile: ${statusData.shop_mobile}` : "";
            headerBrandBadge.textContent = `${statusData.shop_name}${mob} • Fertilizer • Seed • Pesticide`;
        }
        if (loginPropBadge) {
            if (statusData.needs_setup) {
                loginPropBadge.textContent = "🛠️ First-Time Shop Owner Administrator Setup";
            } else if (statusData.shop_mobile) {
                loginPropBadge.textContent = `📞 Mobile: ${statusData.shop_mobile}`;
            } else {
                loginPropBadge.textContent = "🔒 Secure Offline Billing & Inventory";
            }
        }
        return statusData;
    } catch (err) {
        console.warn("Setup status check failed:", err);
        return { needs_setup: false };
    }
}

function configureAuthForms(needsSetup) {
    const setupForm = document.getElementById("erp-first-run-setup-form");
    const loginForm = document.getElementById("erp-login-form");
    if (setupForm) setupForm.style.display = needsSetup ? "block" : "none";
    if (loginForm) loginForm.style.display = needsSetup ? "none" : "block";
}

async function checkAuthState() {
    const setupStatus = await loadSetupStatus();
    if (setupStatus && setupStatus.needs_setup) {
        localStorage.removeItem("krushidhan_auth_token");
        currentUser = null;
        configureAuthForms(true);
        showLoginOverlay("");
        return false;
    }

    configureAuthForms(false);
    const token = localStorage.getItem("krushidhan_auth_token");
    if (!token) {
        currentUser = null;
        showLoginOverlay();
        return false;
    }

    try {
        const res = await _rawDesktopFetch("/api/auth/me", {
            headers: { "Authorization": `Bearer ${token}` }
        }, { skipAuthRedirect: true, includeAuth: true });

        if (res.ok) {
            const data = await res.json();
            currentUser = data.user;
            hideLoginOverlay();
            updateUserInterfaceForRole();
            return true;
        } else {
            currentUser = null;
            if (res.status === 401) {
                const errPayload = await res.json().catch(() => ({}));
                const detail = String(errPayload.detail || "");
                if (!detail.includes("Launch-Token")) {
                    localStorage.removeItem("krushidhan_auth_token");
                }
            }
            showLoginOverlay("Session expired. Please log in again.");
            return false;
        }
    } catch (e) {
        console.error("Auth check failed:", e);
        showLoginOverlay();
        return false;
    }
}

function showLoginOverlay(errorMessage = "") {
    const overlay = document.getElementById("login-screen-overlay");
    if (overlay) {
        overlay.style.display = "flex";
    }
    const errBox = document.getElementById("login-error-box");
    if (errBox) {
        if (errorMessage) {
            errBox.textContent = errorMessage;
            errBox.style.display = "block";
        } else {
            errBox.style.display = "none";
        }
    }
    const setupForm = document.getElementById("erp-first-run-setup-form");
    const isSetupVisible = setupForm && setupForm.style.display !== "none";
    const focusTarget = document.getElementById(isSetupVisible ? "setup-shop-name" : "login-username");
    if (focusTarget) {
        setTimeout(() => focusTarget.focus(), 150);
    }
}

function hideLoginOverlay() {
    const overlay = document.getElementById("login-screen-overlay");
    if (overlay) {
        overlay.style.display = "none";
    }
}

async function submitFirstRunSetup() {
    const shopName = document.getElementById("setup-shop-name")?.value.trim() || "";
    const fullName = document.getElementById("setup-full-name")?.value.trim() || "";
    const mobile = document.getElementById("setup-shop-mobile")?.value.trim() || "";
    const username = document.getElementById("setup-username")?.value.trim() || "";
    const password = document.getElementById("setup-password")?.value || "";
    const confirmPassword = document.getElementById("setup-confirm-password")?.value || "";
    const btnSubmit = document.getElementById("btn-setup-admin-action");
    const errBox = document.getElementById("login-error-box");

    if (!shopName || !fullName || !username || !password) {
        if (errBox) {
            errBox.textContent = "Please fill in all required fields.";
            errBox.style.display = "block";
        }
        return;
    }
    if (password.length < 6) {
        if (errBox) {
            errBox.textContent = "Password must be at least 6 characters.";
            errBox.style.display = "block";
        }
        return;
    }
    if (password !== confirmPassword) {
        if (errBox) {
            errBox.textContent = "Passwords do not match.";
            errBox.style.display = "block";
        }
        return;
    }

    if (btnSubmit) {
        btnSubmit.disabled = true;
        btnSubmit.textContent = "⏳ Creating account...";
    }
    if (errBox) errBox.style.display = "none";

    try {
        const res = await _rawDesktopFetch("/api/auth/setup-admin", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                shop_name: shopName,
                owner_name: fullName,
                full_name: fullName,
                mobile: mobile || null,
                username,
                password
            })
        }, { skipAuthRedirect: true, includeAuth: false });
        const data = await res.json();
        if (res.ok && data.success && data.token) {
            localStorage.setItem("krushidhan_auth_token", data.token);
            currentUser = data.user;
            authSessionVersion++;
            configureAuthForms(false);
            hideLoginOverlay();
            updateUserInterfaceForRole();
            _initialBootstrapCompleted = true;
            await bootstrapDashboard();
        } else {
            if (errBox) {
                errBox.textContent = data.detail || "Setup failed. Please check inputs.";
                errBox.style.display = "block";
            }
        }
    } catch (err) {
        console.error("First-run setup error:", err);
        if (errBox) {
            errBox.textContent = "Unable to connect to the server. Please try again.";
            errBox.style.display = "block";
        }
    } finally {
        if (btnSubmit) {
            btnSubmit.disabled = false;
            btnSubmit.textContent = "✓ Complete Setup";
        }
    }
}

async function submitLogin() {
    const usernameInput = document.getElementById("login-username");
    const passwordInput = document.getElementById("login-password");
    const btnSubmit = document.getElementById("btn-login-action");
    const errBox = document.getElementById("login-error-box");

    const username = usernameInput?.value.trim() || "";
    const password = passwordInput?.value || "";

    if (!username || !password) {
        if (errBox) {
            errBox.textContent = "Please enter both User ID and Password.";
            errBox.style.display = "block";
        }
        return;
    }

    if (btnSubmit) {
        btnSubmit.disabled = true;
        btnSubmit.textContent = "⏳ Verifying...";
    }
    if (errBox) errBox.style.display = "none";

    try {
        const res = await _rawDesktopFetch("/api/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password })
        }, { skipAuthRedirect: true, includeAuth: false });

        const data = await res.json();
        if (res.ok && data.success && data.token) {
            localStorage.setItem("krushidhan_auth_token", data.token);
            currentUser = data.user;
            authSessionVersion++;
            hideLoginOverlay();
            updateUserInterfaceForRole();
            if (passwordInput) passwordInput.value = "";
            _initialBootstrapCompleted = true;
            await bootstrapDashboard();
        } else {
            const msg = data.detail || "Invalid User ID or Password.";
            if (errBox) {
                errBox.textContent = msg;
                errBox.style.display = "block";
            }
        }
    } catch (err) {
        console.error("Login request error:", err);
        if (errBox) {
            errBox.textContent = "Unable to connect to the server. Please try again.";
            errBox.style.display = "block";
        }
    } finally {
        if (btnSubmit) {
            btnSubmit.disabled = false;
            btnSubmit.textContent = "🔒 Secure Login";
        }
    }
}

async function handleLogout() {
    const confirmed = await showConfirmDialog({
        title: "Confirm Logout",
        message: "Do you want to log out of your current Krushidhan ERP session?",
        confirmText: "Logout",
        cancelText: "Stay Signed In",
        danger: true,
    });
    if (!confirmed) return;

    authSessionVersion++;
    _initialBootstrapCompleted = false;
    stopAutoSyncPolling();
    localStorage.removeItem("krushidhan_auth_token");
    currentUser = null;
    const purchaseExcelCard = document.getElementById("purchase-excel-export-card");
    if (purchaseExcelCard) purchaseExcelCard.style.display = "none";
    const supplierExcelCard = document.getElementById("supplier-excel-export-card");
    if (supplierExcelCard) supplierExcelCard.style.display = "none";
    const accountingExcelCard = document.getElementById("accounting-excel-export-card");
    if (accountingExcelCard) accountingExcelCard.style.display = "none";
    const singleSuppLedgerBtn = document.getElementById("btn-export-single-supplier-ledger-excel");
    if (singleSuppLedgerBtn) singleSuppLedgerBtn.style.display = "none";
    const gstExcelCard = document.getElementById("gst-excel-export-card");
    if (gstExcelCard) gstExcelCard.style.display = "none";
    const statutoryExcelCard = document.getElementById("statutory-excel-export-card");
    if (statutoryExcelCard) statutoryExcelCard.style.display = "none";
    const pnlExcelBtn = document.getElementById("btn-export-pnl-excel");
    if (pnlExcelBtn) pnlExcelBtn.style.display = "none";
    const productImportCard = document.getElementById("product-excel-import-card");
    if (productImportCard) productImportCard.style.display = "none";
    if (typeof resetProductImportState === "function") resetProductImportState();
    const customerImportCard = document.getElementById("customer-excel-import-card");
    if (customerImportCard) customerImportCard.style.display = "none";
    if (typeof resetCustomerImportState === "function") resetCustomerImportState();
    const supplierImportCard = document.getElementById("supplier-excel-import-card");
    if (supplierImportCard) supplierImportCard.style.display = "none";
    if (typeof resetSupplierImportState === "function") resetSupplierImportState();
    const backupManagerCard = document.getElementById("settings-backup-manager-card");
    if (backupManagerCard) backupManagerCard.style.display = "none";
    const headerQuickBackupBtn = document.getElementById("btn-header-quick-backup");
    if (headerQuickBackupBtn) headerQuickBackupBtn.style.display = "none";
    const headerBackupMgrBtn = document.getElementById("btn-header-backup-manager");
    if (headerBackupMgrBtn) headerBackupMgrBtn.style.display = "none";
    const drawerQuickBackupBtn = document.getElementById("btn-drawer-quick-backup");
    if (drawerQuickBackupBtn) drawerQuickBackupBtn.style.display = "none";
    const drawerBackupMgrBtn = document.getElementById("btn-drawer-backup-manager");
    if (drawerBackupMgrBtn) drawerBackupMgrBtn.style.display = "none";
    if (typeof resetBackupManagerUiState === "function") resetBackupManagerUiState();
    showLoginOverlay("Logged out successfully.");
    showToast("Logged out successfully.", "info");
}

function updateUserInterfaceForRole() {
    if (!currentUser) return;

    const nameEl = document.getElementById("user-display-name");
    const roleEl = document.getElementById("user-display-role");
    const drawerNameEl = document.getElementById("drawer-user-name");
    const drawerRoleEl = document.getElementById("drawer-user-role");

    const displayName = currentUser.full_name || currentUser.username;
    const displayRole = currentUser.role || "OPERATOR";
    const roleClass = currentUser.role === "ADMIN" ? "user-role-tag role-admin" : "user-role-tag role-operator";

    if (nameEl) nameEl.textContent = displayName;
    if (roleEl) {
        roleEl.textContent = displayRole;
        roleEl.className = roleClass;
    }
    if (drawerNameEl) drawerNameEl.textContent = displayName;
    if (drawerRoleEl) {
        drawerRoleEl.textContent = displayRole;
        drawerRoleEl.className = roleClass;
    }

    // Role Based Navigation Tabs and Elements
    const pnlTab = document.querySelector('.nav-tab[data-tab="tab-pnl"]');
    const drawerPnlTab = document.querySelector('.mobile-drawer-item[data-tab="tab-pnl"]');
    const userMgmtCard = document.getElementById("settings-user-mgmt-card");
    const backupManagerCard = document.getElementById("settings-backup-manager-card");
    const headerQuickBackupBtn = document.getElementById("btn-header-quick-backup");
    const headerBackupMgrBtn = document.getElementById("btn-header-backup-manager");
    const drawerQuickBackupBtn = document.getElementById("btn-drawer-quick-backup");
    const drawerBackupMgrBtn = document.getElementById("btn-drawer-backup-manager");
    const purchaseExcelCard = document.getElementById("purchase-excel-export-card");
    const supplierExcelCard = document.getElementById("supplier-excel-export-card");
    const accountingExcelCard = document.getElementById("accounting-excel-export-card");
    const singleSuppLedgerBtn = document.getElementById("btn-export-single-supplier-ledger-excel");
    const gstExcelCard = document.getElementById("gst-excel-export-card");
    const statutoryExcelCard = document.getElementById("statutory-excel-export-card");
    const pnlExcelBtn = document.getElementById("btn-export-pnl-excel");
    const productImportCard = document.getElementById("product-excel-import-card");
    const customerImportCard = document.getElementById("customer-excel-import-card");
    const supplierImportCard = document.getElementById("supplier-excel-import-card");
    const saveSettingsBtn = document.getElementById("btn-save-shop-profile");
    const settingsHint = document.getElementById("settings-role-hint");

    if (currentUser.role !== "ADMIN") {
        if (pnlTab) pnlTab.style.display = "none";
        if (drawerPnlTab) drawerPnlTab.style.display = "none";
        if (userMgmtCard) userMgmtCard.style.display = "none";
        if (backupManagerCard) backupManagerCard.style.display = "none";
        if (headerQuickBackupBtn) headerQuickBackupBtn.style.display = "none";
        if (headerBackupMgrBtn) headerBackupMgrBtn.style.display = "none";
        if (drawerQuickBackupBtn) drawerQuickBackupBtn.style.display = "none";
        if (drawerBackupMgrBtn) drawerBackupMgrBtn.style.display = "none";
        if (typeof resetBackupManagerUiState === "function") resetBackupManagerUiState();
        if (purchaseExcelCard) purchaseExcelCard.style.display = "none";
        if (supplierExcelCard) supplierExcelCard.style.display = "none";
        if (accountingExcelCard) accountingExcelCard.style.display = "none";
        if (singleSuppLedgerBtn) singleSuppLedgerBtn.style.display = "none";
        if (gstExcelCard) gstExcelCard.style.display = "none";
        if (statutoryExcelCard) statutoryExcelCard.style.display = "none";
        if (pnlExcelBtn) pnlExcelBtn.style.display = "none";
        if (productImportCard) productImportCard.style.display = "none";
        if (typeof resetProductImportState === "function") resetProductImportState();
        if (customerImportCard) customerImportCard.style.display = "none";
        if (typeof resetCustomerImportState === "function") resetCustomerImportState();
        if (supplierImportCard) supplierImportCard.style.display = "none";
        if (typeof resetSupplierImportState === "function") resetSupplierImportState();
        if (saveSettingsBtn) saveSettingsBtn.disabled = true;
        if (settingsHint) settingsHint.textContent = "Read-only view: Only Administrator can update Shop Profile.";
    } else {
        if (pnlTab) pnlTab.style.display = "flex";
        if (drawerPnlTab) drawerPnlTab.style.display = "flex";
        if (userMgmtCard) userMgmtCard.style.display = "block";
        if (backupManagerCard) backupManagerCard.style.display = "block";
        if (headerQuickBackupBtn) headerQuickBackupBtn.style.display = "inline-flex";
        if (headerBackupMgrBtn) headerBackupMgrBtn.style.display = "inline-flex";
        if (drawerQuickBackupBtn) drawerQuickBackupBtn.style.display = "block";
        if (drawerBackupMgrBtn) drawerBackupMgrBtn.style.display = "block";
        if (purchaseExcelCard) purchaseExcelCard.style.display = "block";
        if (supplierExcelCard) supplierExcelCard.style.display = "block";
        if (accountingExcelCard) accountingExcelCard.style.display = "block";
        if (singleSuppLedgerBtn) singleSuppLedgerBtn.style.display = "inline-flex";
        if (gstExcelCard) gstExcelCard.style.display = "block";
        if (statutoryExcelCard) statutoryExcelCard.style.display = "block";
        if (pnlExcelBtn) pnlExcelBtn.style.display = "inline-flex";
        if (productImportCard) productImportCard.style.display = "block";
        if (customerImportCard) customerImportCard.style.display = "block";
        if (supplierImportCard) supplierImportCard.style.display = "block";
        if (saveSettingsBtn) saveSettingsBtn.disabled = false;
        if (settingsHint) settingsHint.textContent = "All changes are stored locally in this shop's SQLite database.";
    }

    // Role-based visibility for Purchase Returns (Admin, Owner, Manager, Accountant only; Operator forbidden)
    const canReturns = canAccessPurchaseReturns();
    const prNavTab = document.querySelector('.nav-tab[data-tab="tab-purchase-returns"]');
    const prDrawerTab = document.querySelector('.mobile-drawer-item[data-tab="tab-purchase-returns"]');
    const prPurBtn = document.getElementById("btn-goto-purchase-returns");
    if (prNavTab) prNavTab.style.display = canReturns ? "flex" : "none";
    if (prDrawerTab) prDrawerTab.style.display = canReturns ? "flex" : "none";
    if (prPurBtn) prPurBtn.style.display = canReturns ? "inline-flex" : "none";
}

function togglePasswordVisibility(fieldId) {
    const input = document.getElementById(fieldId);
    if (!input) return;
    input.type = (input.type === "password") ? "text" : "password";
}

// ----------------- Change Password Modal -----------------
function openChangePasswordModal() {
    document.getElementById("modal-change-password").style.display = "flex";
    document.getElementById("pwd-old").value = "";
    document.getElementById("pwd-new").value = "";
    document.getElementById("pwd-confirm").value = "";
}

function closeChangePasswordModal() {
    document.getElementById("modal-change-password").style.display = "none";
}

async function submitChangePassword() {
    const oldPwd = document.getElementById("pwd-old").value;
    const newPwd = document.getElementById("pwd-new").value;
    const confirmPwd = document.getElementById("pwd-confirm").value;

    if (newPwd !== confirmPwd) {
        showToast("New password and confirm password do not match!", "warning");
        return;
    }

    try {
        const res = await fetch("/api/auth/change-password", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ old_password: oldPwd, new_password: newPwd })
        });

        const data = await res.json();
        if (res.ok && data.success) {
            showToast("✓ " + (data.message || "Password updated successfully!"), "success");
            closeChangePasswordModal();
        } else {
            showToast("Error: " + (data.detail || "Failed to update password."), "error");
        }
    } catch (e) {
        showToast("Error changing password: " + e.message, "error");
    }
}

// ----------------- User Management (Admin Only) -----------------
async function loadAuthUsers() {
    const activeUser = currentUser;
    const sessionVer = authSessionVersion;
    if (!activeUser || activeUser.role !== "ADMIN" || !hasValidAuthSession()) return;
    const tbody = document.getElementById("auth-users-tbody");
    if (!tbody) return;

    try {
        const res = await fetch("/api/auth/users");
        if (!res.ok || sessionVer !== authSessionVersion || !currentUser) return;
        const users = await res.json();

        if (!Array.isArray(users) || users.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No users registered yet.</td></tr>';
            return;
        }

        const currentUserId = currentUser.user_id;
        tbody.innerHTML = users.map(u => `
            <tr>
                <td><b>#${u.user_id}</b></td>
                <td><b>${escapeHtml(u.username)}</b></td>
                <td>${escapeHtml(u.full_name || "-")}</td>
                <td>
                    <span class="user-role-tag ${u.role === 'ADMIN' ? 'role-admin' : 'role-operator'}">${escapeHtml(u.role)}</span>
                </td>
                <td>
                    <span class="badge ${u.is_active ? 'badge-success' : 'badge-danger'}">${u.is_active ? 'Active' : 'Disabled'}</span>
                </td>
                <td class="text-center">
                    ${u.user_id !== currentUserId ? `
                        <button class="btn btn-secondary btn-sm" onclick="toggleUserStatus(${u.user_id}, ${u.is_active})" style="padding: 3px 8px; font-size: 11px;">
                            ${u.is_active ? 'Disable' : 'Enable'}
                        </button>
                        <button class="btn btn-primary btn-sm" onclick="adminResetPassword(${u.user_id}, '${u.username}')" style="padding: 3px 8px; font-size: 11px;">
                            🔑 Reset PIN
                        </button>
                    ` : '<span style="font-size:11px; color:#64748b;">(Current Account)</span>'}
                </td>
            </tr>
        `).join("");
    } catch (e) {
        console.error("Failed to load auth users:", e);
    }
}

function openCreateUserModal() {
    document.getElementById("modal-create-user").style.display = "flex";
    document.getElementById("new-user-username").value = "";
    document.getElementById("new-user-fullname").value = "";
    document.getElementById("new-user-password").value = "";
    document.getElementById("new-user-role").value = "OPERATOR";
}

function closeCreateUserModal() {
    document.getElementById("modal-create-user").style.display = "none";
}

async function submitCreateUser() {
    const username = document.getElementById("new-user-username").value.trim();
    const full_name = document.getElementById("new-user-fullname").value.trim();
    const password = document.getElementById("new-user-password").value;
    const role = document.getElementById("new-user-role").value;

    if (!username || !password || !full_name) {
        showToast("Please fill in all required fields.", "warning");
        return;
    }

    try {
        const res = await fetch("/api/auth/users", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password, full_name, role })
        });

        const data = await res.json();
        if (res.ok && data.success) {
            showToast(`✓ User '${username}' created successfully!`, "success");
            closeCreateUserModal();
            loadAuthUsers();
        } else {
            showToast("Error: " + (data.detail || "Could not create user."), "error");
        }
    } catch (e) {
        showToast("Error creating user: " + e.message, "error");
    }
}

async function toggleUserStatus(userId, currentStatus) {
    const newStatus = !currentStatus;
    const actionName = newStatus ? "Activate" : "Deactivate";
    const confirmed = await showConfirmDialog({
        title: `${actionName} User Account`,
        message: `Are you sure you want to ${actionName.toLowerCase()} this user account?`,
        confirmText: actionName,
        cancelText: "Cancel",
        danger: !newStatus,
    });
    if (!confirmed) return;

    try {
        const res = await fetch(`/api/auth/users/${userId}/status`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ is_active: newStatus })
        });
        const data = await res.json();
        if (res.ok && data.success) {
            showToast(`✓ User ${actionName.toLowerCase()}d successfully.`, "success");
            loadAuthUsers();
        } else {
            showToast(data.detail || "Status update failed", "error");
        }
    } catch (e) {
        showToast("Error: " + e.message, "error");
    }
}

async function adminResetPassword(userId, username) {
    const newPwd = await showInputDialog({
        title: `Reset Password — ${username}`,
        message: `Enter a new password (at least 4 characters) for user '${username}':`,
        placeholder: "Enter new password",
        confirmText: "Update Password",
        cancelText: "Cancel",
        inputType: "password",
        minLength: 4,
        emptyMessage: "Please enter a new password.",
        shortMessage: "Password must be at least 4 characters.",
    });
    if (!newPwd) return;

    try {
        const res = await fetch(`/api/auth/users/${userId}/reset-password`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ new_password: newPwd.trim() })
        });
        const data = await res.json();
        if (res.ok && data.success) {
            showToast(`✓ Password updated for '${username}'!`, "success");
        } else {
            showToast("Error: " + (data.detail || "Password reset failed"), "error");
        }
    } catch (e) {
        showToast("Error: " + e.message, "error");
    }
}

// ============================================================
// 1. SUPPLIER & DISTRIBUTOR LEDGER
// ============================================================
let allSuppliersKhata = [];
let activeSupplierStatementId = null;

async function loadSupplierKhata() {
    const tbody = document.getElementById("sk-suppliers-tbody");
    if (!tbody) return;

    try {
        const res = await fetch("/api/accounting/suppliers");
        allSuppliersKhata = await res.json();
        
        let totalPayable = 0;
        let totalPurchases = 0;

        allSuppliersKhata.forEach(s => {
            totalPayable += Number(s.outstanding_payable || 0);
            totalPurchases += Number(s.total_purchases || 0);
        });

        const payEl = document.getElementById("sk-total-payable");
        const purEl = document.getElementById("sk-total-purchases");
        const countEl = document.getElementById("sk-suppliers-count");

        if (payEl) payEl.textContent = `₹${totalPayable.toFixed(2)}`;
        if (purEl) purEl.textContent = `₹${totalPurchases.toFixed(2)}`;
        if (countEl) countEl.textContent = allSuppliersKhata.length;

        filterSupplierList();
    } catch (err) {
        console.error("Failed to load supplier ledger:", err);
        if (tbody) tbody.innerHTML = '<tr><td colspan="9" class="text-center text-danger">Failed to load supplier ledger.</td></tr>';
    }
}

function filterSupplierList() {
    const tbody = document.getElementById("sk-suppliers-tbody");
    if (!tbody) return;

    const query = document.getElementById("sk-search-input")?.value.trim().toLowerCase() || "";
    let filtered = allSuppliersKhata || [];

    if (query) {
        filtered = filtered.filter(s => 
            (s.supplier_name || "").toLowerCase().includes(query) ||
            (s.contact_person || "").toLowerCase().includes(query) ||
            (s.city || "").toLowerCase().includes(query) ||
            (s.gstin || "").toLowerCase().includes(query)
        );
    }

    if (filtered.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted" style="padding: 24px;">No distributors matching search.</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map(s => `
        <tr>
            <td><b>${s.supplier_name}</b></td>
            <td>${s.contact_person || '-'}</td>
            <td>${s.city || '-'}</td>
            <td>${s.mobile || '-'}</td>
            <td><code>${s.gstin || '-'}</code></td>
            <td class="text-center"><b>${s.bills_count || 0}</b></td>
            <td class="text-right font-bold">₹${Number(s.total_purchases || 0).toFixed(2)}</td>
            <td class="text-right font-bold" style="color: ${s.outstanding_payable > 0 ? '#dc2626' : '#15803d'}; font-size: 14px;">
                ₹${Number(s.outstanding_payable || 0).toFixed(2)}
            </td>
            <td class="text-center">
                <button class="btn btn-primary btn-sm" onclick="openSupplierPaymentModal(${s.supplier_id}, '${s.supplier_name.replace(/'/g, "\\'")}', ${s.outstanding_payable || 0})" style="padding: 3px 8px; font-size: 11px; margin-right: 4px;">
                    💵 Pay
                </button>
                <button class="btn btn-secondary btn-sm" onclick="openSupplierStatementModal(${s.supplier_id})" style="padding: 3px 8px; font-size: 11px;">
                    📑 Statement
                </button>
            </td>
        </tr>
    `).join("");
}

function openSupplierPaymentModal(supplierId, supplierName, currentBalance) {
    document.getElementById("modal-supplier-payment").style.display = "flex";
    document.getElementById("sp-supplier-id").value = supplierId;
    document.getElementById("sp-supplier-name").textContent = supplierName;
    document.getElementById("sp-current-balance").textContent = `₹${Number(currentBalance || 0).toFixed(2)}`;

    const today = new Date().toISOString().split("T")[0];
    const pendingPay = _getMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.supplierPayments, supplierId);
    if (pendingPay) {
        currentSupplierPaymentIdempotencyKey = pendingPay.idempotency_key;
        const p = pendingPay.payload || {};
        document.getElementById("sp-pay-amount").value = p.amount !== undefined && p.amount !== null ? String(p.amount) : "";
        document.getElementById("sp-pay-date").value = p.payment_date || today;
        const modeEl = document.getElementById("sp-pay-mode");
        if (modeEl && p.payment_mode) modeEl.value = p.payment_mode;
        document.getElementById("sp-pay-ref").value = p.reference_no || "";
        document.getElementById("sp-pay-narration").value = p.narration || "";

        const isConf = pendingPay.status === "conflict";
        _showFinIdempotencyBanner("sp-pay-idem-banner", "sp-pay-idem-msg", "btn-sp-pay-retry", {
            status: isConf ? "conflict" : "uncertain",
            showRetry: !isConf && pendingPay.status !== "validation_error",
            message: isConf
                ? _buildFinConflictMessage("supplier payment", "the Supplier Statement / Day Book", pendingPay.error_detail)
                : (pendingPay.status === "validation_error"
                    ? `Previous supplier payment could not be saved: ${pendingPay.error_detail || "Validation error"}. Fix the details or inspect the Supplier Statement before starting a new payment.`
                    : _buildFinUncertainMessage("supplier payment", "the Supplier Statement / Day Book")),
        });
    } else {
        currentSupplierPaymentIdempotencyKey = null;
        _hideFinIdempotencyBanner("sp-pay-idem-banner");
        document.getElementById("sp-pay-amount").value = "";
        document.getElementById("sp-pay-ref").value = "";
        document.getElementById("sp-pay-narration").value = "";
        document.getElementById("sp-pay-date").value = today;
    }
    setTimeout(() => {
        const el = document.getElementById("sp-pay-amount");
        if (el && typeof el.focus === "function") el.focus();
    }, 150);
}

function closeSupplierPaymentModal() {
    document.getElementById("modal-supplier-payment").style.display = "none";
    // Preserve unconfirmed draft in localStorage; clear only in-memory key so other suppliers never reuse it.
    currentSupplierPaymentIdempotencyKey = null;
    _hideFinIdempotencyBanner("sp-pay-idem-banner");
}

async function continueSupplierPaymentAsNew() {
    if (isSubmittingSupplierPayment) return false;
    const supplierId = Number(document.getElementById("sp-supplier-id")?.value);
    const confirmed = await showConfirmDialog({
        title: "Continue as New Supplier Payment?",
        message: "A supplier payment from your earlier attempt may already have been saved. Please inspect the Supplier Statement or Day Book first to avoid recording a duplicate payment.\n\nOnly confirm if you want to start a brand-new payment.",
        confirmText: "Continue as New Payment",
        cancelText: "Cancel",
        danger: true,
    });
    if (!confirmed) return false;
    if (supplierId) _clearMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.supplierPayments, supplierId);
    currentSupplierPaymentIdempotencyKey = generateIdempotencyKey();
    _hideFinIdempotencyBanner("sp-pay-idem-banner");
    showToast("Ready to record as a new supplier payment. Form values have been preserved.", "info", 5500);
    return true;
}

async function retryPendingSupplierPayment() {
    const supplierId = Number(document.getElementById("sp-supplier-id")?.value);
    const pending = supplierId ? _getMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.supplierPayments, supplierId) : null;
    if (!pending) return submitSupplierPayment();
    return submitSupplierPayment(pending);
}

async function submitSupplierPayment(pendingOverride = null) {
    if (isSubmittingSupplierPayment) return;

    let supplierId;
    let reqPayload;
    let idempotencyKey;

    if (pendingOverride && pendingOverride.payload && pendingOverride.idempotency_key) {
        supplierId = Number(pendingOverride.payload.supplier_id);
        idempotencyKey = pendingOverride.idempotency_key;
        currentSupplierPaymentIdempotencyKey = idempotencyKey;
        reqPayload = { ...pendingOverride.payload, idempotency_key: idempotencyKey };
    } else {
        supplierId = Number(document.getElementById("sp-supplier-id")?.value);
        const amount = parseFloat(document.getElementById("sp-pay-amount")?.value || "0");
        const paymentDate = document.getElementById("sp-pay-date")?.value || "";
        const paymentMode = document.getElementById("sp-pay-mode")?.value || "BANK_TRANSFER";
        const refNo = (document.getElementById("sp-pay-ref")?.value || "").trim();
        const narration = (document.getElementById("sp-pay-narration")?.value || "").trim();

        if (!supplierId || !amount || isNaN(amount) || amount <= 0) {
            showToast("Please enter a valid payment amount.", "warning");
            return;
        }

        const existingPending = _getMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.supplierPayments, supplierId);
        if (!currentSupplierPaymentIdempotencyKey) {
            currentSupplierPaymentIdempotencyKey = existingPending
                ? existingPending.idempotency_key
                : generateIdempotencyKey();
        }
        idempotencyKey = currentSupplierPaymentIdempotencyKey;
        reqPayload = {
            supplier_id: supplierId,
            amount: amount,
            payment_date: paymentDate || null,
            payment_mode: paymentMode,
            reference_no: refNo || null,
            narration: narration || null,
            idempotency_key: idempotencyKey,
        };
    }

    isSubmittingSupplierPayment = true;
    _setFinSubmitButtonState("btn-save-supp-pay", null, true, "Recording payment...");
    const pendingRecord = {
        supplier_id: supplierId,
        idempotency_key: idempotencyKey,
        payload: reqPayload,
        status: "pending",
        error_code: null,
        error_detail: null,
        saved_at: new Date().toISOString(),
    };
    _setMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.supplierPayments, supplierId, pendingRecord);

    try {
        if (_isNavigatorOffline()) {
            const offlineMsg = "You appear to be offline. Supplier payment cannot be confirmed right now; details and key are preserved locally for safe retry once reconnected.";
            _setMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.supplierPayments, supplierId, {
                ...pendingRecord,
                status: "uncertain",
                error_detail: offlineMsg,
            });
            _showFinIdempotencyBanner("sp-pay-idem-banner", "sp-pay-idem-msg", "btn-sp-pay-retry", {
                status: "uncertain",
                showRetry: true,
                message: offlineMsg,
            });
            showToast(offlineMsg, "warning", 7000);
            return;
        }

        let res = null;
        let data = {};
        try {
            res = await fetch("/api/accounting/supplier-payment", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotencyKey,
                },
                body: JSON.stringify(reqPayload),
            });
            data = await res.json().catch(() => ({}));
        } catch (err) {
            const uncertainMsg = _buildFinUncertainMessage("supplier payment", "the Supplier Statement / Day Book");
            _setMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.supplierPayments, supplierId, {
                ...pendingRecord,
                status: "uncertain",
                error_detail: err.message || "Network error",
            });
            _showFinIdempotencyBanner("sp-pay-idem-banner", "sp-pay-idem-msg", "btn-sp-pay-retry", {
                status: "uncertain",
                showRetry: true,
                message: uncertainMsg,
            });
            showToast(uncertainMsg, "warning", 7500);
            return;
        }

        if (res.ok && data.success !== false) {
            const vchRef = data.voucher_no || (data.voucher_id ? `#${data.voucher_id}` : "");
            const isReplay = data.idempotent_replay === true;

            _clearMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.supplierPayments, supplierId);
            currentSupplierPaymentIdempotencyKey = null;
            _hideFinIdempotencyBanner("sp-pay-idem-banner");

            if (isReplay) {
                showToast(
                    `Supplier payment was already saved (Voucher ${vchRef}). Duplicate prevented.`,
                    "info",
                    6500
                );
            } else {
                showToast(
                    `Supplier payment recorded successfully (Voucher ${vchRef})!`,
                    "success",
                    6000
                );
            }

            closeSupplierPaymentModal();
            try { await loadSupplierKhata(); } catch (e) {}
            try { await loadDayBook(); } catch (e) {}
            try { if (typeof loadCashReconciliation === "function") await loadCashReconciliation(); } catch (e) {}
        } else if (res.status === 409 && data.error_code === "IDEMPOTENCY_KEY_CONFLICT") {
            const detail = extractApiErrorMessage(data, "");
            const msg = _buildFinConflictMessage("supplier payment", "the Supplier Statement / Day Book", detail);
            _setMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.supplierPayments, supplierId, {
                ...pendingRecord,
                status: "conflict",
                error_code: "IDEMPOTENCY_KEY_CONFLICT",
                error_detail: detail,
            });
            _showFinIdempotencyBanner("sp-pay-idem-banner", "sp-pay-idem-msg", "btn-sp-pay-retry", {
                status: "conflict",
                showRetry: false,
                message: msg,
            });
            showToast(msg, "error", 7500);
        } else if (isTransientHttpStatus(res.status)) {
            const uncertainMsg = _buildFinUncertainMessage("supplier payment", "the Supplier Statement / Day Book");
            _setMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.supplierPayments, supplierId, {
                ...pendingRecord,
                status: "uncertain",
                error_detail: `HTTP ${res.status}`,
            });
            _showFinIdempotencyBanner("sp-pay-idem-banner", "sp-pay-idem-msg", "btn-sp-pay-retry", {
                status: "uncertain",
                showRetry: true,
                message: uncertainMsg,
            });
            showToast(uncertainMsg, "warning", 7500);
        } else {
            const errMsg = extractApiErrorMessage(data, `HTTP ${res.status}`);
            _setMapPendingFinRecord(FIN_PENDING_STORAGE_KEYS.supplierPayments, supplierId, {
                ...pendingRecord,
                status: "validation_error",
                error_detail: errMsg,
            });
            _showFinIdempotencyBanner("sp-pay-idem-banner", "sp-pay-idem-msg", "btn-sp-pay-retry", {
                status: "validation_error",
                showRetry: false,
                message: `Payment could not be recorded: ${errMsg}. Correct the details or inspect the Supplier Statement before starting a new payment.`,
            });
            showToast("Error: " + errMsg, "error", 6500);
        }
    } finally {
        isSubmittingSupplierPayment = false;
        _setFinSubmitButtonState("btn-save-supp-pay", null, false);
    }
}

async function openSupplierStatementModal(supplierId) {
    const modal = document.getElementById("modal-supplier-statement");
    if (!modal) return;
    activeSupplierStatementId = supplierId;
    modal.style.display = "flex";

    const nameEl = document.getElementById("stmt-supp-name");
    const metaEl = document.getElementById("stmt-supp-meta");
    const dueEl = document.getElementById("stmt-supp-due");
    const purTbody = document.getElementById("stmt-purchases-tbody");
    const payTbody = document.getElementById("stmt-payments-tbody");

    purTbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">Loading purchase bills...</td></tr>';
    payTbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">Loading payment history...</td></tr>';

    try {
        const res = await fetch(`/api/accounting/suppliers/${supplierId}/statement`);
        if (!res.ok) throw new Error("Failed to load statement");
        const data = await res.json();

        const s = data.supplier;
        if (nameEl) nameEl.textContent = s.supplier_name;
        if (metaEl) metaEl.textContent = `Contact: ${s.contact_person || '-'} • Mobile: ${s.mobile || '-'} • GSTIN: ${s.gstin || '-'}`;
        if (dueEl) dueEl.textContent = `₹${Number(s.current_balance || 0).toFixed(2)}`;

        // Render Inward Purchases
        if (!data.purchases || data.purchases.length === 0) {
            purTbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No purchase bills found.</td></tr>';
        } else {
            purTbody.innerHTML = data.purchases.map(p => `
                <tr>
                    <td><b>${p.invoice_no}</b></td>
                    <td>${p.purchase_date}</td>
                    <td><span class="badge" style="background:#e0f2fe; color:#0369a1;">${formatPaymentModeLabel(p.payment_mode)}</span></td>
                    <td class="text-right font-bold">₹${Number(p.net_amount).toFixed(2)}</td>
                    <td class="text-right" style="color:#15803d;">₹${Number(p.paid_amount).toFixed(2)}</td>
                    <td class="text-right" style="color:#dc2626; font-weight:700;">₹${Number(p.due_amount).toFixed(2)}</td>
                </tr>
            `).join("");
        }

        // Render Payments
        if (!data.payments || data.payments.length === 0) {
            payTbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">No payment records found.</td></tr>';
        } else {
            payTbody.innerHTML = data.payments.map(v => `
                <tr>
                    <td><b>${v.voucher_no}</b></td>
                    <td>${v.voucher_date}</td>
                    <td>${v.narration || v.reference_no || '-'}</td>
                    <td class="text-right font-bold" style="color:#15803d;">₹${Number(v.total_amount).toFixed(2)}</td>
                </tr>
            `).join("");
        }
    } catch (e) {
        console.error("Statement error:", e);
    }
}

function closeSupplierStatementModal() {
    document.getElementById("modal-supplier-statement").style.display = "none";
    activeSupplierStatementId = null;
}

// ============================================================
// 2. STATUTORY AGRICULTURE DEPARTMENT REGISTERS
// ============================================================
let currentStatutoryType = 'fertilizer';

function switchStatutoryRegister(type) {
    currentStatutoryType = type;
    document.querySelectorAll(".stat-reg-btn").forEach(btn => btn.className = "btn btn-secondary btn-sm stat-reg-btn");

    const activeBtn = document.getElementById(`btn-stat-${type === 'fertilizer' ? 'fert' : type === 'pesticide' ? 'pest' : 'seed'}`);
    if (activeBtn) activeBtn.className = "btn btn-primary btn-sm stat-reg-btn active";

    const titleEl = document.getElementById("stat-register-title");
    const subEl = document.getElementById("stat-register-subtitle");
    const thead = document.getElementById("stat-table-thead");

    if (type === 'fertilizer') {
        if (titleEl) titleEl.textContent = "🌱 Fertilizer Control Order (FCO) - Sale Register";
        if (subEl) subEl.textContent = "Official statutory register under Fertilizer Control Order (FCO 1985)";
        if (thead) {
            thead.innerHTML = `
                <tr>
                    <th>Inv No</th>
                    <th>Date</th>
                    <th>Farmer Name</th>
                    <th>Village</th>
                    <th>Fertilizer Particulars</th>
                    <th>Company</th>
                    <th>Batch No</th>
                    <th>Expiry</th>
                    <th class="text-center">Qty Sold</th>
                    <th class="text-right">Rate (₹)</th>
                    <th class="text-right">Total (₹)</th>
                </tr>
            `;
        }
    } else if (type === 'pesticide') {
        if (titleEl) titleEl.textContent = "🧪 Insecticides Act 1968 - Pesticide & Fungicide Sale Register";
        if (subEl) subEl.textContent = "Statutory Insecticides and Pesticides Register with Technical Name & CIB/RC Reg Nos";
        if (thead) {
            thead.innerHTML = `
                <tr>
                    <th>Inv No</th>
                    <th>Date</th>
                    <th>Farmer Name</th>
                    <th>Pesticide / Technical Name</th>
                    <th>CIB/RC Reg No</th>
                    <th>Company</th>
                    <th>Batch No</th>
                    <th>Mfg / Exp Date</th>
                    <th class="text-center">Qty Sold</th>
                    <th class="text-right">Rate (₹)</th>
                    <th class="text-right">Total (₹)</th>
                </tr>
            `;
        }
    } else if (type === 'seed') {
        if (titleEl) titleEl.textContent = "🌾 Seeds Act 1966 - Certified & Hybrid Seed Sale Register";
        if (subEl) subEl.textContent = "Statutory Seeds Register with Lot Number, Germination validity, and Farmer details";
        if (thead) {
            thead.innerHTML = `
                <tr>
                    <th>Inv No</th>
                    <th>Date</th>
                    <th>Farmer Name</th>
                    <th>Village</th>
                    <th>Seed Variety</th>
                    <th>Company</th>
                    <th>Lot / Batch No</th>
                    <th>Valid Upto</th>
                    <th class="text-center">Qty Sold</th>
                    <th class="text-right">Rate (₹)</th>
                    <th class="text-right">Total (₹)</th>
                </tr>
            `;
        }
    }

    loadStatutoryRegister();
}

async function loadStatutoryRegister() {
    if (!hasValidAuthSession()) return;
    const tbody = document.getElementById("stat-table-tbody");
    if (!tbody) return;

    const fromDate = document.getElementById("stat-from-date")?.value || "2026-01-01";
    const toDate = document.getElementById("stat-to-date")?.value || new Date().toISOString().split("T")[0];

    tbody.innerHTML = '<tr><td colspan="11" class="text-center text-muted" style="padding: 20px;">Loading statutory register...</td></tr>';

    try {
        const endpoint = `/api/statutory/${currentStatutoryType}-register?from_date=${fromDate}&to_date=${toDate}`;
        const res = await fetch(endpoint);
        if (!res.ok || !hasValidAuthSession()) return;
        const rawData = await res.json();
        const data = Array.isArray(rawData) ? rawData : [];

        const countBadge = document.getElementById("stat-records-count");
        if (countBadge) countBadge.textContent = `${data.length} Records`;

        if (!data || data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="11" class="text-center text-muted" style="padding: 24px;">No sale records found for this date range.</td></tr>';
            return;
        }

        if (currentStatutoryType === 'fertilizer') {
            tbody.innerHTML = data.map(r => `
                <tr>
                    <td><b>${r.invoice_no}</b></td>
                    <td>${r.sale_date}</td>
                    <td><b>${r.farmer_name}</b><br><small style="color:#64748b;">${r.farmer_mobile || ''}</small></td>
                    <td>${r.farmer_village || '-'}</td>
                    <td><b>${r.fertilizer_name}</b></td>
                    <td>${r.company_name || '-'}</td>
                    <td><span class="badge badge-fefo">${r.batch_no || '-'}</span></td>
                    <td>${r.exp_date || '-'}</td>
                    <td class="text-center font-bold">${r.qty} ${r.unit_symbol || ''}</td>
                    <td class="text-right">₹${Number(r.sale_rate).toFixed(2)}</td>
                    <td class="text-right font-bold">₹${Number(r.total_amount).toFixed(2)}</td>
                </tr>
            `).join("");
        } else if (currentStatutoryType === 'pesticide') {
            tbody.innerHTML = data.map(r => `
                <tr>
                    <td><b>${r.invoice_no}</b></td>
                    <td>${r.sale_date}</td>
                    <td><b>${r.farmer_name}</b></td>
                    <td><b>${r.pesticide_name}</b><br><small style="color:#0284c7;">${r.technical_name || ''}</small></td>
                    <td><code>${r.cib_rc_reg_no || '-'}</code></td>
                    <td>${r.company_name || '-'}</td>
                    <td><span class="badge badge-fefo">${r.batch_no || '-'}</span></td>
                    <td>${r.exp_date || r.mfg_date || '-'}</td>
                    <td class="text-center font-bold">${r.qty} ${r.unit_symbol || ''}</td>
                    <td class="text-right">₹${Number(r.sale_rate).toFixed(2)}</td>
                    <td class="text-right font-bold">₹${Number(r.total_amount).toFixed(2)}</td>
                </tr>
            `).join("");
        } else if (currentStatutoryType === 'seed') {
            tbody.innerHTML = data.map(r => `
                <tr>
                    <td><b>${r.invoice_no}</b></td>
                    <td>${r.sale_date}</td>
                    <td><b>${r.farmer_name}</b></td>
                    <td>${r.farmer_village || '-'}</td>
                    <td><b>${r.seed_variety}</b></td>
                    <td>${r.seed_company || '-'}</td>
                    <td><span class="badge badge-fefo">${r.lot_no || '-'}</span></td>
                    <td>${r.valid_upto || '-'}</td>
                    <td class="text-center font-bold">${r.qty} ${r.unit_symbol || ''}</td>
                    <td class="text-right">₹${Number(r.sale_rate).toFixed(2)}</td>
                    <td class="text-right font-bold">₹${Number(r.total_amount).toFixed(2)}</td>
                </tr>
            `).join("");
        }
    } catch (e) {
        console.error("Failed to load statutory register:", e);
        tbody.innerHTML = '<tr><td colspan="11" class="text-center text-danger">Error loading statutory register. Please try again.</td></tr>';
    }
}

// ============================================================
// 3. CASH DRAWER CLOSING & OPERATIONAL EXPENSES
// ============================================================
let expectedDrawerCashAmount = 5000.0;

async function loadCashReconciliation() {
    const today = new Date().toISOString().split("T")[0];
    const dateInput = document.getElementById("cd-recon-date");
    const dateStr = dateInput ? dateInput.value || today : today;
    if (dateInput && !dateInput.value) dateInput.value = today;

    try {
        const res = await fetch(`/api/accounting/cash-closing-summary?date=${dateStr}`);
        const data = await res.json();

        document.getElementById("cd-sys-opening").textContent = `₹${data.opening_cash.toFixed(2)}`;
        document.getElementById("cd-sys-sales").textContent = `₹${data.cash_sales.toFixed(2)}`;
        document.getElementById("cd-sys-receipts").textContent = `₹${data.farmer_cash_receipts.toFixed(2)}`;
        document.getElementById("cd-sys-expenses").textContent = `₹${data.cash_expenses.toFixed(2)}`;
        document.getElementById("cd-sys-supp-payments").textContent = `₹${data.cash_supplier_payments.toFixed(2)}`;
        document.getElementById("cd-sys-expected").textContent = `₹${data.expected_drawer_cash.toFixed(2)}`;

        expectedDrawerCashAmount = data.expected_drawer_cash;
        calculatePhysicalCash();
    } catch (e) {
        console.error("Cash reconciliation error:", e);
    }
}

function calculatePhysicalCash() {
    const n500 = parseInt(document.getElementById("denom-500")?.value || "0") || 0;
    const n200 = parseInt(document.getElementById("denom-200")?.value || "0") || 0;
    const n100 = parseInt(document.getElementById("denom-100")?.value || "0") || 0;
    const n50 = parseInt(document.getElementById("denom-50")?.value || "0") || 0;

    const totalPhysical = (n500 * 500) + (n200 * 200) + (n100 * 100) + (n50 * 50);
    const totalEl = document.getElementById("cd-physical-total");
    const badgeEl = document.getElementById("cd-variance-badge");

    if (totalEl) totalEl.textContent = `₹${totalPhysical.toFixed(2)}`;

    if (!badgeEl) return;
    if (totalPhysical === 0 && n500 === 0 && n200 === 0 && n100 === 0 && n50 === 0) {
        badgeEl.textContent = "Enter note counts above to verify closing";
        badgeEl.style.background = "#e0f2fe";
        badgeEl.style.color = "#0369a1";
        return;
    }

    const diff = totalPhysical - expectedDrawerCashAmount;
    if (Math.abs(diff) < 0.01) {
        badgeEl.textContent = "✓ Cash Drawer Balanced! (Exact Match ₹0.00 Variance)";
        badgeEl.style.background = "#dcfce7";
        badgeEl.style.color = "#15803d";
    } else if (diff < 0) {
        badgeEl.textContent = `⚠️ Cash Drawer Shortage: ₹${Math.abs(diff).toFixed(2)} Short`;
        badgeEl.style.background = "#fef2f2";
        badgeEl.style.color = "#dc2626";
    } else {
        badgeEl.textContent = `ℹ️ Cash Drawer Excess: ₹${diff.toFixed(2)} Over`;
        badgeEl.style.background = "#fef9c3";
        badgeEl.style.color = "#854d0e";
    }
}

async function continueShopExpenseAsNew() {
    if (isSubmittingExpense) return false;
    const confirmed = await showConfirmDialog({
        title: "Continue as New Daily Expense?",
        message: "An expense from your earlier attempt may already have been saved. Please inspect the Day Book or Expense History first to avoid recording a duplicate expense.\n\nOnly confirm if you want to start a brand-new expense.",
        confirmText: "Continue as New Expense",
        cancelText: "Cancel",
        danger: true,
    });
    if (!confirmed) return false;
    _clearSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.shopExpense);
    currentExpenseIdempotencyKey = generateIdempotencyKey();
    _hideFinIdempotencyBanner("acc-exp-idem-banner");
    showToast("Ready to record as a new expense. Form values have been preserved.", "info", 5500);
    return true;
}

async function retryPendingShopExpense() {
    const pending = _readSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.shopExpense);
    if (!pending) return submitShopExpense();
    return submitShopExpense(pending);
}

async function submitShopExpense(pendingOverride = null) {
    if (isSubmittingExpense) return;

    let reqPayload;
    let idempotencyKey;

    if (pendingOverride && pendingOverride.payload && pendingOverride.idempotency_key) {
        idempotencyKey = pendingOverride.idempotency_key;
        currentExpenseIdempotencyKey = idempotencyKey;
        reqPayload = { ...pendingOverride.payload, idempotency_key: idempotencyKey };
    } else {
        const catId = Number(document.getElementById("acc-exp-cat")?.value);
        const amount = parseFloat(document.getElementById("acc-exp-amt")?.value || "0");
        const expDate = document.getElementById("acc-exp-date")?.value || new Date().toISOString().split("T")[0];
        const mode = document.getElementById("acc-exp-mode")?.value || "CASH";
        const remarks = (document.getElementById("acc-exp-remarks")?.value || "").trim();

        if (!catId || !amount || isNaN(amount) || amount <= 0) {
            showToast("Please enter a valid expense amount.", "warning");
            return;
        }

        const existingPending = _readSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.shopExpense);
        if (!currentExpenseIdempotencyKey) {
            currentExpenseIdempotencyKey = existingPending
                ? existingPending.idempotency_key
                : generateIdempotencyKey();
        }
        idempotencyKey = currentExpenseIdempotencyKey;
        reqPayload = {
            category_id: catId,
            amount: amount,
            expense_date: expDate,
            payment_mode: mode,
            remarks: remarks,
            idempotency_key: idempotencyKey,
        };
    }

    isSubmittingExpense = true;
    _setFinSubmitButtonState("btn-acc-save-expense", 'button[onclick="submitShopExpense()"]', true, "Saving expense...");
    const pendingRecord = {
        idempotency_key: idempotencyKey,
        payload: reqPayload,
        status: "pending",
        error_code: null,
        error_detail: null,
        saved_at: new Date().toISOString(),
    };
    _writeSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.shopExpense, pendingRecord);

    try {
        if (_isNavigatorOffline()) {
            const offlineMsg = "You appear to be offline. Daily expense cannot be confirmed right now; details and key are preserved locally for safe retry once reconnected.";
            _writeSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.shopExpense, {
                ...pendingRecord,
                status: "uncertain",
                error_detail: offlineMsg,
            });
            _showFinIdempotencyBanner("acc-exp-idem-banner", "acc-exp-idem-msg", "btn-acc-exp-retry", {
                status: "uncertain",
                showRetry: true,
                message: offlineMsg,
            });
            showToast(offlineMsg, "warning", 7000);
            return;
        }

        let res = null;
        let data = {};
        try {
            res = await fetch("/api/accounting/expenses", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotencyKey,
                },
                body: JSON.stringify(reqPayload),
            });
            data = await res.json().catch(() => ({}));
        } catch (err) {
            const uncertainMsg = _buildFinUncertainMessage("daily expense", "the Day Book / Expense History");
            _writeSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.shopExpense, {
                ...pendingRecord,
                status: "uncertain",
                error_detail: err.message || "Network error",
            });
            _showFinIdempotencyBanner("acc-exp-idem-banner", "acc-exp-idem-msg", "btn-acc-exp-retry", {
                status: "uncertain",
                showRetry: true,
                message: uncertainMsg,
            });
            showToast(uncertainMsg, "warning", 7500);
            return;
        }

        if (res.ok && data.success !== false) {
            const vchRef = data.voucher_no || (data.voucher_id ? `Voucher #${data.voucher_id}` : `Expense #${data.expense_id || ""}`);
            const isReplay = data.idempotent_replay === true;

            _clearSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.shopExpense);
            currentExpenseIdempotencyKey = null;
            _hideFinIdempotencyBanner("acc-exp-idem-banner");

            const amtEl = document.getElementById("acc-exp-amt");
            const remEl = document.getElementById("acc-exp-remarks");
            if (amtEl) amtEl.value = "";
            if (remEl) remEl.value = "";

            if (isReplay) {
                showToast(
                    `Daily expense was already saved (Expense #${data.expense_id || ""}, Voucher ${vchRef}). Duplicate prevented.`,
                    "info",
                    6500
                );
            } else {
                showToast(
                    `Daily expense recorded successfully (Voucher ${vchRef})!`,
                    "success",
                    6000
                );
            }

            try { await loadDayBook(); } catch (e) {}
            try { if (typeof loadCashReconciliation === "function") await loadCashReconciliation(); } catch (e) {}
            try {
                if (currentUser && currentUser.role === "ADMIN" && typeof loadProfitAndLoss === "function") {
                    await loadProfitAndLoss();
                }
            } catch (e) {}
        } else if (res.status === 409 && data.error_code === "IDEMPOTENCY_KEY_CONFLICT") {
            const detail = extractApiErrorMessage(data, "");
            const msg = _buildFinConflictMessage("daily expense", "the Day Book / Expense History", detail);
            _writeSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.shopExpense, {
                ...pendingRecord,
                status: "conflict",
                error_code: "IDEMPOTENCY_KEY_CONFLICT",
                error_detail: detail,
            });
            _showFinIdempotencyBanner("acc-exp-idem-banner", "acc-exp-idem-msg", "btn-acc-exp-retry", {
                status: "conflict",
                showRetry: false,
                message: msg,
            });
            showToast(msg, "error", 7500);
        } else if (isTransientHttpStatus(res.status)) {
            const uncertainMsg = _buildFinUncertainMessage("daily expense", "the Day Book / Expense History");
            _writeSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.shopExpense, {
                ...pendingRecord,
                status: "uncertain",
                error_detail: `HTTP ${res.status}`,
            });
            _showFinIdempotencyBanner("acc-exp-idem-banner", "acc-exp-idem-msg", "btn-acc-exp-retry", {
                status: "uncertain",
                showRetry: true,
                message: uncertainMsg,
            });
            showToast(uncertainMsg, "warning", 7500);
        } else {
            const errMsg = extractApiErrorMessage(data, `HTTP ${res.status}`);
            _writeSinglePendingFinRecord(FIN_PENDING_STORAGE_KEYS.shopExpense, {
                ...pendingRecord,
                status: "validation_error",
                error_detail: errMsg,
            });
            _showFinIdempotencyBanner("acc-exp-idem-banner", "acc-exp-idem-msg", "btn-acc-exp-retry", {
                status: "validation_error",
                showRetry: false,
                message: `Expense could not be recorded: ${errMsg}. Correct the details or inspect Expense History before starting a new expense.`,
            });
            showToast("Error: " + errMsg, "error", 6500);
        }
    } finally {
        isSubmittingExpense = false;
        _setFinSubmitButtonState("btn-acc-save-expense", 'button[onclick="submitShopExpense()"]', false);
    }
}

// ============================================================
// 4. PAYMENT MODE DISPLAY FORMATTER
// ============================================================
function formatPaymentModeLabel(mode) {
    if (!mode) return "CASH";
    const normalized = String(mode).trim().toUpperCase();
    const labels = {
        CASH: "CASH",
        CREDIT: "CREDIT",
        BANK_TRANSFER: "BANK TRANSFER",
        BANK_NON_CASH: "BANK / NON-CASH",
        CHEQUE: "CHEQUE",
        CARD: "CARD",
        SPLIT: "SPLIT",
        UPI: "UPI (Historical)"
    };
    return labels[normalized] || normalized.replace(/_/g, " ");
}

// ============================================================
// 5. OFFICIAL COMMERCIAL GST TAX INVOICE MODAL & PRINT LOGIC
// ============================================================
let currentSavedSaleId = null;

function numberToIndianWords(amount) {
    const roundedPaise = Math.round(Math.max(0, Number(amount || 0)) * 100);
    const rupeesInt = Math.floor(roundedPaise / 100);
    const paiseInt = roundedPaise % 100;
    if (isNaN(rupeesInt) || (rupeesInt === 0 && paiseInt === 0)) {
        return "Zero Rupees Only";
    }

    const ones = ['', 'One ', 'Two ', 'Three ', 'Four ', 'Five ', 'Six ', 'Seven ', 'Eight ', 'Nine ', 'Ten ', 'Eleven ', 'Twelve ', 'Thirteen ', 'Fourteen ', 'Fifteen ', 'Sixteen ', 'Seventeen ', 'Eighteen ', 'Nineteen '];
    const tens = ['', '', 'Twenty ', 'Thirty ', 'Forty ', 'Fifty ', 'Sixty ', 'Seventy ', 'Eighty ', 'Ninety '];

    function lessThanThousand(n) {
        let out = '';
        if (n >= 100) {
            out += ones[Math.floor(n / 100)] + 'Hundred ';
            n %= 100;
        }
        if (n >= 20) {
            out += tens[Math.floor(n / 10)] + ones[n % 10];
        } else if (n > 0) {
            out += ones[n];
        }
        return out.trim();
    }

    function intToWords(val) {
        if (val <= 0) return '';
        const crore = Math.floor(val / 10000000);
        let rem = val % 10000000;
        const lakh = Math.floor(rem / 100000);
        rem %= 100000;
        const thousand = Math.floor(rem / 1000);
        const hundreds = rem % 1000;

        const parts = [];
        if (crore > 0) parts.push(`${intToWords(crore)} Crore`);
        if (lakh > 0) parts.push(`${lessThanThousand(lakh)} Lakh`);
        if (thousand > 0) parts.push(`${lessThanThousand(thousand)} Thousand`);
        if (hundreds > 0) parts.push(lessThanThousand(hundreds));
        return parts.join(' ').trim();
    }

    const rupeeText = rupeesInt > 0 ? `Rupees ${intToWords(rupeesInt)}` : "Rupees Zero";
    if (paiseInt > 0) {
        return `${rupeeText} and ${lessThanThousand(paiseInt)} Paise Only`;
    }
    return `${rupeeText} Only`;
}

function buildShopAddressText(settings) {
    if (!settings) return "";
    const addr = (settings.address || "").trim();
    const city = (settings.city || "").trim();
    const state = (settings.state || "").trim();
    const pincode = (settings.pincode || "").trim();
    const hasSpecificLoc = Boolean(addr || city || pincode);

    const parts = [];
    if (addr) parts.push(addr);
    if (city && !addr.toLowerCase().includes(city.toLowerCase())) parts.push(city);
    if (hasSpecificLoc && state && !parts.join(" ").toLowerCase().includes(state.toLowerCase())) {
        parts.push(pincode && !state.includes(pincode) ? `${state} - ${pincode}` : state);
    } else if (pincode && !parts.join(" ").includes(pincode)) {
        parts.push(`PIN: ${pincode}`);
    }
    return parts.join(", ");
}

function buildCustomerAddressText(saleData) {
    if (!saleData) return "";
    const raw = [
        (saleData.customer_village || "").trim(),
        (saleData.customer_taluka || "").trim(),
        (saleData.customer_district || "").trim()
    ];
    const deduped = [];
    const seen = new Set();
    raw.forEach(part => {
        if (part && !seen.has(part.toLowerCase())) {
            deduped.push(part);
            seen.add(part.toLowerCase());
        }
    });
    return deduped.join(", ");
}

function computeInvoicePaymentStatus(netAmount, paidAmount, dueAmount) {
    const net = Number(netAmount || 0);
    const paid = Number(paidAmount || 0);
    const rawDue = Number(dueAmount || 0);
    const effectiveDue = rawDue > 0.005 ? rawDue : Math.max(0, Math.round((net - paid) * 100) / 100);
    if (effectiveDue <= 0.01) {
        return { label: "PAID", cssClass: "status-paid", color: "#15803d", effectiveDue: 0 };
    }
    if (paid > 0.01) {
        return { label: "PARTIAL", cssClass: "status-partial", color: "#b45309", effectiveDue };
    }
    return { label: "CREDIT / UNPAID", cssClass: "status-unpaid", color: "#b91c1c", effectiveDue };
}

function toggleOptionalRow(rowId, valId, value) {
    const row = document.getElementById(rowId);
    const valEl = valId ? document.getElementById(valId) : null;
    const clean = (value === null || value === undefined) ? "" : String(value).trim();
    if (clean) {
        if (valEl) valEl.textContent = clean;
        if (row) row.style.display = "";
        return true;
    }
    if (valEl) valEl.textContent = "";
    if (row) row.style.display = "none";
    return false;
}

function applyDynamicShopToInvoicePreview(settings) {
    const s = settings || {};
    const shopName = (s.company_name || "").trim() || "Agri-Input Shop";

    const modalTitle = document.getElementById("inv-prev-modal-title");
    if (modalTitle) modalTitle.textContent = `📄 Tax Invoice - ${shopName}`;

    const shopNameEl = document.getElementById("inv-prev-shop-name");
    if (shopNameEl) shopNameEl.textContent = shopName;

    toggleOptionalRow("inv-prev-shop-owner-row", "inv-prev-shop-owner", s.owner_name);

    const addrText = buildShopAddressText(s);
    const addrEl = document.getElementById("inv-prev-shop-address");
    if (addrEl) {
        addrEl.textContent = addrText;
        addrEl.style.display = addrText ? "" : "none";
    }

    const contactBits = [];
    if ((s.mobile || "").trim()) contactBits.push(`Mobile: ${s.mobile.trim()}`);
    if ((s.email || "").trim()) contactBits.push(`Email: ${s.email.trim()}`);
    const contactEl = document.getElementById("inv-prev-shop-contact");
    if (contactEl) {
        contactEl.textContent = contactBits.join("  |  ");
        contactEl.style.display = contactBits.length ? "" : "none";
    }

    const hasGstin = toggleOptionalRow("inv-prev-gstin-row", "inv-prev-gstin", s.gstin);
    const hasFert = toggleOptionalRow("inv-prev-lic-fert-row", "inv-prev-lic-fert", s.dl_fertilizer);
    const hasPest = toggleOptionalRow("inv-prev-lic-pest-row", "inv-prev-lic-pest", s.dl_pesticide);
    const hasSeed = toggleOptionalRow("inv-prev-lic-seed-row", "inv-prev-lic-seed", s.dl_seed);

    const hasAnyStatutory = hasGstin || hasFert || hasPest || hasSeed;
    const licBox = document.getElementById("inv-prev-lic-box");
    const hdrGrid = document.getElementById("inv-prev-header-grid");
    if (licBox) licBox.style.display = hasAnyStatutory ? "" : "none";
    if (hdrGrid) {
        if (hasAnyStatutory) hdrGrid.classList.remove("single-col");
        else hdrGrid.classList.add("single-col");
    }

    const bankBits = [];
    if ((s.bank_name || "").trim()) bankBits.push(`Bank: ${s.bank_name.trim()}`);
    if ((s.account_no || "").trim()) bankBits.push(`A/c: ${s.account_no.trim()}`);
    if ((s.ifsc_code || "").trim()) bankBits.push(`IFSC: ${s.ifsc_code.trim()}`);
    toggleOptionalRow("inv-prev-bank-row", "inv-prev-bank-details", bankBits.join(" | "));

    const termsEl = document.getElementById("inv-prev-terms");
    if (termsEl) {
        const configuredTerms = (s.invoice_terms || "").trim();
        termsEl.textContent = configuredTerms ||
            "1. Please verify batch number, expiry, and quantity at the time of delivery. 2. Store agricultural inputs safely away from food, children, and livestock.";
    }

    const authCoEl = document.getElementById("inv-prev-auth-company");
    if (authCoEl) authCoEl.textContent = `For ${shopName}`;

    const jurEl = document.getElementById("inv-prev-jurisdiction");
    if (jurEl) {
        const city = (s.city || "").trim();
        jurEl.textContent = city
            ? `Subject to ${city} Jurisdiction • Computer Generated Tax Invoice`
            : "Computer Generated Tax Invoice";
    }
}

function openInvoicePreviewModal(saleData, saleId) {
    currentSavedSaleId = saleId;
    const modal = document.getElementById("modal-invoice-preview");
    if (!modal || !saleData) return;

    applyDynamicShopToInvoicePreview(currentCompanySettings);

    // Buyer / Customer Metadata strictly from authoritative sale record (zero hardcoded locations)
    const custName = (saleData.customer_name || "").trim() || "Cash Customer";
    const custAddr = buildCustomerAddressText(saleData);
    const custMobile = (saleData.customer_mobile || "").trim();
    const custGstin = (saleData.customer_gstin || "").trim();
    const cropRef = (saleData.doctor_or_officer || "").trim();

    document.getElementById("inv-prev-cust-name").textContent = custName;
    toggleOptionalRow("inv-prev-cust-addr-row", "inv-prev-cust-addr", custAddr || "-");
    toggleOptionalRow("inv-prev-cust-mobile-row", "inv-prev-cust-mobile", custMobile || "-");
    toggleOptionalRow("inv-prev-cust-gstin-row", "inv-prev-cust-gstin", custGstin);
    toggleOptionalRow("inv-prev-crop-row", "inv-prev-crop", cropRef);

    // Invoice Metadata
    const isOfflinePending = Boolean(saleData.is_offline_pending) || String(saleId || "").startsWith("OFFLINE-");
    const payModeLabel = formatPaymentModeLabel(saleData.payment_mode);
    const statusInfo = computeInvoicePaymentStatus(saleData.net_amount, saleData.paid_amount, saleData.due_amount);

    document.getElementById("inv-prev-inv-no").textContent = isOfflinePending
        ? "Pending - invoice number assigned when saved"
        : (saleData.invoice_no || "-");
    document.getElementById("inv-prev-date").textContent = saleData.sale_date || "-";
    document.getElementById("inv-prev-paymode").textContent = payModeLabel;
    const payModeBadge = document.getElementById("inv-prev-paymode-badge");
    if (payModeBadge) payModeBadge.textContent = payModeLabel;

    const statusBadge = document.getElementById("inv-prev-pay-status");
    if (statusBadge) {
        statusBadge.textContent = statusInfo.label;
        statusBadge.className = `inv-status-badge ${statusInfo.cssClass}`;
    }
    const statusMetaEl = document.getElementById("inv-prev-paystatus-meta");
    if (statusMetaEl) {
        statusMetaEl.textContent = statusInfo.label;
        statusMetaEl.style.color = statusInfo.color;
    }

    // 13-Column Commercial Product Line Items (strictly from backend/saved items)
    const tbody = document.getElementById("inv-prev-items-tbody");
    const items = Array.isArray(saleData.items) ? saleData.items : [];
    if (tbody) {
        tbody.innerHTML = "";
        items.forEach((item, index) => {
            const tr = document.createElement("tr");
            const qty = Number(item.qty || item.quantity || 0);
            const rate = Number(item.sale_rate || item.rate || 0);
            const discAmt = Number(item.discount_amount || 0);
            const taxableAmt = Number(item.taxable_amount || 0);
            const gstPct = Number(item.cgst_rate || 0) + Number(item.sgst_rate || 0) + Number(item.igst_rate || 0);
            const taxAmt = Number(item.cgst_amount || 0) + Number(item.sgst_amount || 0) + Number(item.igst_amount || 0);
            const lineTotal = Number(item.total_amount || 0);

            tr.innerHTML = `
                <td style="text-align: center;">${index + 1}</td>
                <td><strong>${escapeHtml(item.product_name || "-")}</strong></td>
                <td style="text-align: center;">${escapeHtml(item.hsn_code || "-")}</td>
                <td>${escapeHtml(item.batch_no || "-")}</td>
                <td style="text-align: center;">${escapeHtml(item.exp_date || "-")}</td>
                <td style="text-align: right;">${Number.isInteger(qty) ? qty : qty.toFixed(2).replace(/\.00$/, "")}</td>
                <td style="text-align: center;">${escapeHtml(item.unit_name || item.unit || "NOS")}</td>
                <td style="text-align: right;">${rate.toFixed(2)}</td>
                <td style="text-align: right;">${discAmt.toFixed(2)}</td>
                <td style="text-align: right;">${taxableAmt.toFixed(2)}</td>
                <td style="text-align: right;">${gstPct}%</td>
                <td style="text-align: right;">${taxAmt.toFixed(2)}</td>
                <td style="text-align: right; font-weight: 700;">${lineTotal.toFixed(2)}</td>
            `;
            tbody.appendChild(tr);
        });
    }

    // Totals Breakdown strictly from backend/authoritative values
    const totalDisc = Number(saleData.total_discount || 0);
    const totalTaxable = Number(saleData.total_taxable || 0);
    const totalCgst = Number(saleData.total_cgst || 0);
    const totalSgst = Number(saleData.total_sgst || 0);
    const totalIgst = Number(saleData.total_igst || 0);
    const extraCharges = Number(saleData.extra_charges || 0);
    const roundOff = Number(saleData.round_off || 0);
    const netAmount = Number(saleData.net_amount || 0);
    const paidAmount = Number(saleData.paid_amount || 0);
    const effectiveDue = statusInfo.effectiveDue;

    const itemsLineSum = items.reduce((acc, it) => acc + Number(it.total_amount || 0), 0);
    const grossSubtotal = items.length > 0
        ? itemsLineSum + totalDisc
        : totalTaxable + totalCgst + totalSgst + totalIgst + totalDisc;

    const _setInvText = (id, text) => {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    };

    _setInvText("inv-prev-subtotal", `₹${grossSubtotal.toFixed(2)}`);
    _setInvText("inv-prev-disc", totalDisc > 0 ? `-₹${totalDisc.toFixed(2)}` : "₹0.00");
    _setInvText("inv-prev-taxable", `₹${totalTaxable.toFixed(2)}`);
    _setInvText("inv-prev-cgst", `₹${totalCgst.toFixed(2)}`);
    _setInvText("inv-prev-sgst", `₹${totalSgst.toFixed(2)}`);

    const igstRow = document.getElementById("inv-prev-igst-row");
    if (igstRow) {
        igstRow.style.display = totalIgst > 0.004 ? "" : "none";
        _setInvText("inv-prev-igst", `₹${totalIgst.toFixed(2)}`);
    }
    const extraRow = document.getElementById("inv-prev-extra-row");
    if (extraRow) {
        extraRow.style.display = extraCharges > 0.004 ? "" : "none";
        _setInvText("inv-prev-extra", `₹${extraCharges.toFixed(2)}`);
    }

    _setInvText("inv-prev-round", roundOff < 0 ? `-₹${Math.abs(roundOff).toFixed(2)}` : `₹${roundOff.toFixed(2)}`);
    _setInvText("inv-prev-net", `₹${netAmount.toFixed(2)}`);
    _setInvText("inv-prev-paid", `₹${paidAmount.toFixed(2)}`);

    const balEl = document.getElementById("inv-prev-balance");
    if (balEl) {
        balEl.textContent = `₹${effectiveDue.toFixed(2)}`;
        balEl.style.color = effectiveDue > 0.01 ? "#dc2626" : "#15803d";
    }

    _setInvText("inv-prev-words", numberToIndianWords(netAmount));
    toggleOptionalRow("inv-prev-remarks-row", "inv-prev-remarks", saleData.remarks);

    modal.style.display = "flex";
}

async function openSavedInvoicePreview(saleId) {
    if (!saleId) return;
    try {
        if (!currentCompanySettings) {
            await loadSettings();
        }
        const res = await fetch(`/api/sales/${saleId}`);
        if (!res.ok) {
            showToast("Could not load invoice details.", "error");
            return;
        }
        const sale = await res.json();
        openInvoicePreviewModal(sale, saleId);
    } catch (err) {
        showToast("Failed to load invoice preview: " + err.message, "error");
    }
}

function clearPrintTargetClasses() {
    if (!document.body) return;
    document.body.classList.remove(
        "print-target-invoice",
        "print-target-farmer",
        "print-target-pnl",
        "print-target-statutory",
        "print-target-purchase-return",
        "print-target-sales-return"
    );
}

function buildPrintHeaderShopMeta(settings) {
    const s = settings || currentCompanySettings || {};
    const bits = [];
    const addr = buildShopAddressText(s);
    if (addr) bits.push(addr);
    if ((s.gstin || "").trim()) bits.push(`GSTIN: ${s.gstin.trim()}`);
    if ((s.mobile || "").trim()) bits.push(`Mobile: ${s.mobile.trim()}`);
    if ((s.email || "").trim()) bits.push(`Email: ${s.email.trim()}`);
    return bits.join(" | ");
}

function populateFarmerPrintHeader() {
    const s = currentCompanySettings || {};
    const nameEl = document.getElementById("fd-print-shop-name");
    const metaEl = document.getElementById("fd-print-shop-meta");
    if (nameEl) nameEl.textContent = (s.company_name || "").trim() || "Agri-Input Shop";
    if (metaEl) metaEl.textContent = buildPrintHeaderShopMeta(s);
}

function populateReportPrintHeader(type) {
    const s = currentCompanySettings || {};
    const shopName = (s.company_name || "").trim() || "Agri-Input Shop";
    const metaText = buildPrintHeaderShopMeta(s);

    if (type === "pnl") {
        const nameEl = document.getElementById("pnl-print-shop-name");
        const metaEl = document.getElementById("pnl-print-shop-meta");
        const periodEl = document.getElementById("pnl-print-period");
        const fromDate = document.getElementById("pnl-from-date")?.value || "-";
        const toDate = document.getElementById("pnl-to-date")?.value || "-";
        if (nameEl) nameEl.textContent = shopName;
        if (metaEl) metaEl.textContent = metaText;
        if (periodEl) periodEl.textContent = `${fromDate} to ${toDate}`;
    } else if (type === "statutory") {
        const nameEl = document.getElementById("stat-print-shop-name");
        const metaEl = document.getElementById("stat-print-shop-meta");
        const docTitleEl = document.getElementById("stat-print-doc-title");
        const regTitle = document.getElementById("stat-register-title")?.textContent?.trim() || "STATUTORY SALE REGISTER";
        const fromDate = document.getElementById("stat-from-date")?.value || "-";
        const toDate = document.getElementById("stat-to-date")?.value || "-";
        if (nameEl) nameEl.textContent = shopName;
        if (metaEl) metaEl.textContent = metaText;
        if (docTitleEl) docTitleEl.textContent = `${regTitle} (${fromDate} to ${toDate})`;
    }
}

function printSection(target) {
    clearPrintTargetClasses();
    if (target === "invoice") {
        document.body.classList.add("print-target-invoice");
    } else if (target === "farmer") {
        populateFarmerPrintHeader();
        document.body.classList.add("print-target-farmer");
    } else if (target === "pnl") {
        populateReportPrintHeader("pnl");
        document.body.classList.add("print-target-pnl");
    } else if (target === "statutory") {
        populateReportPrintHeader("statutory");
        document.body.classList.add("print-target-statutory");
    } else if (target === "purchase-return") {
        document.body.classList.add("print-target-purchase-return");
    } else if (target === "sales-return") {
        document.body.classList.add("print-target-sales-return");
    }
    window.print();
}

function closeInvoicePreviewModal() {
    const modal = document.getElementById("modal-invoice-preview");
    if (modal) modal.style.display = "none";
    document.body.classList.remove("print-target-invoice");
}

function printInvoicePreview() {
    if (String(currentSavedSaleId || "").startsWith("OFFLINE-")) {
        showToast("Pending - invoice number assigned when saved. Official invoice can be printed only after sync succeeds.", "warning", 6000);
        return;
    }
    printSection("invoice");
}

function printFarmerStatement() {
    printSection("farmer");
}

function printPnLReport() {
    printSection("pnl");
}

function printStatutoryRegister() {
    printSection("statutory");
}

window.addEventListener("beforeprint", () => {
    const cls = document.body.classList;
    if (
        cls.contains("print-target-invoice") ||
        cls.contains("print-target-farmer") ||
        cls.contains("print-target-pnl") ||
        cls.contains("print-target-statutory") ||
        cls.contains("print-target-purchase-return") ||
        cls.contains("print-target-sales-return") ||
        cls.contains("print-target-recovery-card")
    ) {
        return;
    }
    const invModal = document.getElementById("modal-invoice-preview");
    const fdModal = document.getElementById("modal-farmer-detail");
    if (invModal && invModal.style.display === "flex") {
        cls.add("print-target-invoice");
    } else if (fdModal && fdModal.classList.contains("show")) {
        populateFarmerPrintHeader();
        cls.add("print-target-farmer");
    } else if (document.getElementById("tab-pnl")?.classList.contains("active")) {
        populateReportPrintHeader("pnl");
        cls.add("print-target-pnl");
    } else if (document.getElementById("tab-statutory")?.classList.contains("active")) {
        populateReportPrintHeader("statutory");
        cls.add("print-target-statutory");
    }
});

window.addEventListener("afterprint", clearPrintTargetClasses);

function _parseContentDispositionFilename(disp, fallbackFilename) {
    const rawDisp = String(disp || "");
    const starMatch = rawDisp.match(/filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;\r\n]+)/i);
    let candidate = "";
    if (starMatch && starMatch[1]) {
        try {
            candidate = decodeURIComponent(starMatch[1].replace(/["']/g, "").trim());
        } catch (e) {
            candidate = starMatch[1].replace(/["']/g, "").trim();
        }
    } else {
        const plainMatch = rawDisp.match(/filename\s*=\s*"?([^";\r\n]+)"?/i);
        if (plainMatch && plainMatch[1]) {
            candidate = plainMatch[1].replace(/["']/g, "").trim();
        }
    }
    const base = candidate.replace(/^.*[/\\]/, "").trim();
    return base || fallbackFilename;
}

async function _deliverDownloadedBlob(blob, filename, fallbackMime = "application/octet-stream") {
    const saveHook = _desktopRuntimeState.saveFile ||
        (typeof window !== "undefined" && window.__KRUSHIDHAN_DESKTOP__?.saveFile) ||
        null;
    if (typeof saveHook === "function") {
        let bytes = null;
        if (typeof blob.arrayBuffer === "function") {
            bytes = new Uint8Array(await blob.arrayBuffer());
        }
        const hookResult = await saveHook({
            filename,
            mimeType: blob.type || fallbackMime,
            bytes,
            blob,
        });
        if (hookResult && hookResult.canceled === true) {
            return false;
        }
        return true;
    }

    const blobUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = blobUrl;
    link.download = filename;
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    setTimeout(() => {
        if (link.parentNode) link.parentNode.removeChild(link);
        URL.revokeObjectURL(blobUrl);
    }, 15000);
    return true;
}

async function downloadInvoicePdf(saleId = null) {
    const id = saleId || currentSavedSaleId;
    if (!id) {
        showToast("Invoice ID not found.", "warning");
        return;
    }
    if (String(id).startsWith("OFFLINE-")) {
        showToast("Pending - invoice number assigned when saved. Download PDF after the bill syncs.", "warning");
        return;
    }
    try {
        const res = await fetch(`/api/sales/${id}/pdf`);
        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            showToast("Could not generate PDF: " + (errData.detail || `HTTP ${res.status}`), "error");
            return;
        }
        const disp = res.headers.get("Content-Disposition") || "";
        const domInvNo = (!saleId && document.getElementById("inv-prev-inv-no")?.textContent?.trim()) || "";
        const fallbackPdfName = (domInvNo && domInvNo !== "-")
            ? `Invoice_${domInvNo.replace(/[^A-Za-z0-9_-]/g, "_")}.pdf`
            : `Invoice_${id}.pdf`;
        let filename = _parseContentDispositionFilename(disp, fallbackPdfName);
        if (!filename.toLowerCase().endsWith(".pdf")) {
            filename += ".pdf";
        }

        const blob = await res.blob();
        const saveHook = _desktopRuntimeState.saveFile ||
            (typeof window !== "undefined" && window.__KRUSHIDHAN_DESKTOP__?.saveFile) ||
            null;
        if (typeof saveHook === "function") {
            let bytes = null;
            if (typeof blob.arrayBuffer === "function") {
                bytes = new Uint8Array(await blob.arrayBuffer());
            }
            const hookResult = await saveHook({
                filename,
                mimeType: blob.type || "application/pdf",
                bytes,
                blob,
            });
            if (hookResult && hookResult.canceled === true) {
                return;
            }
            showToast(`Saved invoice PDF: ${filename}`, "success");
            return;
        }

        const blobUrl = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = blobUrl;
        link.download = filename;
        link.style.display = "none";
        document.body.appendChild(link);
        link.click();
        setTimeout(() => {
            if (link.parentNode) link.parentNode.removeChild(link);
            URL.revokeObjectURL(blobUrl);
        }, 15000);
        showToast(`Downloaded invoice PDF: ${filename}`, "success");
    } catch (err) {
        showToast("Error downloading invoice PDF: " + err.message, "error");
    }
}

/* ==========================================
   EXCEL REPORT EXPORTS (STAGES C3-B2A & C3-B2B)
========================================== */
async function downloadExcelExport(endpointUrl, fallbackFilename, buttonEl = null) {
    if (!hasValidAuthSession()) {
        showToast("Session expired. Please login again.", "warning");
        return;
    }

    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Exporting...";
    }

    try {
        const res = await fetch(endpointUrl);
        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            showToast("Could not export Excel report: " + (errData.detail || `HTTP ${res.status}`), "error");
            return;
        }

        const disp = res.headers.get("Content-Disposition") || "";
        let filename = _parseContentDispositionFilename(disp, fallbackFilename);
        if (!filename.toLowerCase().endsWith(".xlsx")) {
            filename += ".xlsx";
        }

        const blob = await res.blob();
        const delivered = await _deliverDownloadedBlob(
            blob,
            filename,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        );
        if (delivered) {
            showToast(`Exported Excel workbook: ${filename}`, "success");
        }
    } catch (err) {
        showToast("Error exporting Excel report: " + err.message, "error");
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

function buildExcelExportUrl(basePath, paramsObj) {
    const params = new URLSearchParams();
    Object.entries(paramsObj || {}).forEach(([k, v]) => {
        if (v !== null && v !== undefined && String(v).trim() !== "") {
            params.set(k, String(v).trim());
        }
    });
    const qs = params.toString();
    return qs ? `${basePath}?${qs}` : basePath;
}

function exportProductCatalogExcel(buttonEl = null) {
    const query = document.getElementById("m-prod-search")?.value?.trim() || "";
    const categoryId = document.getElementById("m-prod-filter-cat")?.value || "";
    const url = buildExcelExportUrl("/api/excel/exports/products", {
        query,
        category_id: categoryId,
    });
    downloadExcelExport(url, "Product_Catalog.xlsx", buttonEl);
}

function exportStockSummaryExcel(buttonEl = null) {
    const categoryId = document.getElementById("inv-filter-cat")?.value || "";
    const query = document.getElementById("inv-search-input")?.value?.trim() || "";
    const url = buildExcelExportUrl("/api/excel/exports/stock-summary", {
        category_id: categoryId,
        query,
    });
    downloadExcelExport(url, "Current_Stock_Summary.xlsx", buttonEl);
}

function exportBatchStockExpiryExcel(buttonEl = null) {
    const categoryId = document.getElementById("inv-filter-cat")?.value || "";
    const query = document.getElementById("inv-search-input")?.value?.trim() || "";
    const url = buildExcelExportUrl("/api/excel/exports/stock-batches", {
        category_id: categoryId,
        query,
    });
    downloadExcelExport(url, "Batch_Wise_Stock_Expiry.xlsx", buttonEl);
}

function exportStockLedgerExcel(buttonEl = null) {
    const fromDate = document.getElementById("inv-ledger-from-date")?.value?.trim() || "";
    const toDate = document.getElementById("inv-ledger-to-date")?.value?.trim() || "";
    const categoryId = document.getElementById("inv-filter-cat")?.value || "";

    if (fromDate && toDate && fromDate > toDate) {
        showToast("Stock Ledger 'From Date' cannot be later than 'To Date'.", "warning");
        return;
    }

    const url = buildExcelExportUrl("/api/excel/exports/stock-ledger", {
        from_date: fromDate,
        to_date: toDate,
        category_id: categoryId,
    });
    downloadExcelExport(url, "Stock_Ledger.xlsx", buttonEl);
}

function _getValidatedExportDateRange(fromInputId, toInputId, reportLabel) {
    const fromDate = document.getElementById(fromInputId)?.value?.trim() || "";
    const toDate = document.getElementById(toInputId)?.value?.trim() || "";
    if (fromDate && toDate && fromDate > toDate) {
        showToast(`${reportLabel} 'From Date' cannot be later than 'To Date'.`, "warning");
        return null;
    }
    return { from_date: fromDate, to_date: toDate };
}

function exportSalesRegisterExcel(buttonEl = null) {
    const range = _getValidatedExportDateRange("sales-export-from-date", "sales-export-to-date", "Sales Register");
    if (!range) return;
    const url = buildExcelExportUrl("/api/excel/exports/sales-register", range);
    downloadExcelExport(url, "Sales_Register.xlsx", buttonEl);
}

function exportSalesItemDetailsExcel(buttonEl = null) {
    const range = _getValidatedExportDateRange("sales-export-from-date", "sales-export-to-date", "Sales Item Details");
    if (!range) return;
    const url = buildExcelExportUrl("/api/excel/exports/sales-items", range);
    downloadExcelExport(url, "Sales_Item_Details.xlsx", buttonEl);
}

function exportPurchaseRegisterExcel(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Purchase Excel exports are restricted to administrators.", "error");
        return;
    }
    const range = _getValidatedExportDateRange("pur-export-from-date", "pur-export-to-date", "Purchase Register");
    if (!range) return;
    const url = buildExcelExportUrl("/api/excel/exports/purchase-register", range);
    downloadExcelExport(url, "Purchase_Register.xlsx", buttonEl);
}

function exportPurchaseItemDetailsExcel(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Purchase Excel exports are restricted to administrators.", "error");
        return;
    }
    const range = _getValidatedExportDateRange("pur-export-from-date", "pur-export-to-date", "Purchase Item Details");
    if (!range) return;
    const url = buildExcelExportUrl("/api/excel/exports/purchase-items", range);
    downloadExcelExport(url, "Purchase_Item_Details.xlsx", buttonEl);
}

/* ==========================================
   EXCEL ACCOUNTING EXPORTS (STAGE C3-B3A)
========================================== */
function exportCustomerLedgerExcel(buttonEl = null) {
    const range = _getValidatedExportDateRange("cust-ledger-from-date", "cust-ledger-to-date", "Customer Ledger");
    if (!range) return;
    const customerId = document.getElementById("cust-export-customer-id")?.value || "";
    const url = buildExcelExportUrl("/api/excel/exports/customer-ledger", {
        ...range,
        customer_id: customerId,
    });
    downloadExcelExport(url, "Customer_Ledger.xlsx", buttonEl);
}

function exportActiveFarmerLedgerExcel(buttonEl = null) {
    if (!activeFarmerId) {
        showToast("Please open a farmer statement first.", "warning");
        return;
    }
    const url = buildExcelExportUrl("/api/excel/exports/customer-ledger", {
        customer_id: activeFarmerId,
    });
    downloadExcelExport(url, "Customer_Ledger.xlsx", buttonEl);
}

function exportCustomerOutstandingExcel(buttonEl = null) {
    const query = document.getElementById("fs-search-name")?.value?.trim() || "";
    const village = document.getElementById("fs-filter-village")?.value?.trim() || "";
    const onlyOutstanding = isOnlyCreditFilter ? "true" : "";
    const url = buildExcelExportUrl("/api/excel/exports/customer-outstanding", {
        query,
        village,
        only_outstanding: onlyOutstanding,
    });
    downloadExcelExport(url, "Customer_Outstanding.xlsx", buttonEl);
}

function exportSupplierLedgerExcel(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Supplier Ledger Excel export is restricted to administrators.", "error");
        return;
    }
    const range = _getValidatedExportDateRange("supp-ledger-from-date", "supp-ledger-to-date", "Supplier Ledger");
    if (!range) return;
    const supplierId = document.getElementById("supp-export-supplier-id")?.value || "";
    const url = buildExcelExportUrl("/api/excel/exports/supplier-ledger", {
        ...range,
        supplier_id: supplierId,
    });
    downloadExcelExport(url, "Supplier_Ledger.xlsx", buttonEl);
}

function exportActiveSupplierLedgerExcel(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Supplier Ledger Excel export is restricted to administrators.", "error");
        return;
    }
    if (!activeSupplierStatementId) {
        showToast("Please open a supplier statement first.", "warning");
        return;
    }
    const url = buildExcelExportUrl("/api/excel/exports/supplier-ledger", {
        supplier_id: activeSupplierStatementId,
    });
    downloadExcelExport(url, "Supplier_Ledger.xlsx", buttonEl);
}

function exportSupplierOutstandingExcel(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Supplier Outstanding Excel export is restricted to administrators.", "error");
        return;
    }
    const query = document.getElementById("sk-search-input")?.value?.trim() || "";
    const url = buildExcelExportUrl("/api/excel/exports/supplier-outstanding", {
        query,
    });
    downloadExcelExport(url, "Supplier_Outstanding.xlsx", buttonEl);
}

function exportPaymentHistoryExcel(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Payment History Excel export is restricted to administrators.", "error");
        return;
    }
    const range = _getValidatedExportDateRange("acc-export-from-date", "acc-export-to-date", "Payment History");
    if (!range) return;
    const paymentMode = document.getElementById("acc-export-pay-mode")?.value || "";
    const url = buildExcelExportUrl("/api/excel/exports/payment-history", {
        ...range,
        payment_mode: paymentMode,
    });
    downloadExcelExport(url, "Payment_History.xlsx", buttonEl);
}

function exportExpensesExcel(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Expenses Excel export is restricted to administrators.", "error");
        return;
    }
    const range = _getValidatedExportDateRange("acc-export-from-date", "acc-export-to-date", "Expenses");
    if (!range) return;
    const categoryId = document.getElementById("acc-export-exp-cat")?.value || "";
    const url = buildExcelExportUrl("/api/excel/exports/expenses", {
        ...range,
        category_id: categoryId,
    });
    downloadExcelExport(url, "Expenses.xlsx", buttonEl);
}

/* ==========================================
   EXCEL MASTER DATA EXPORTS (STAGE C3-B3B)
========================================== */
function exportCustomerMasterExcel(buttonEl = null) {
    const query = document.getElementById("fs-search-name")?.value?.trim() || "";
    const village = document.getElementById("fs-filter-village")?.value?.trim() || "";
    const includeInactiveEl = document.getElementById("cust-master-include-inactive");
    const includeInactive = includeInactiveEl ? Boolean(includeInactiveEl.checked) : true;
    const url = buildExcelExportUrl("/api/excel/exports/customers", {
        query,
        village,
        include_inactive: includeInactive ? "true" : "false",
    });
    downloadExcelExport(url, "Customer_Master.xlsx", buttonEl);
}

function exportSupplierMasterExcel(buttonEl = null) {
    const query = document.getElementById("sk-search-input")?.value?.trim() || "";
    const includeInactiveEl = document.getElementById("supp-master-include-inactive");
    const includeInactive = includeInactiveEl ? Boolean(includeInactiveEl.checked) : true;
    const url = buildExcelExportUrl("/api/excel/exports/suppliers", {
        query,
        include_inactive: includeInactive ? "true" : "false",
    });
    downloadExcelExport(url, "Supplier_Master.xlsx", buttonEl);
}

/* ==========================================
   EXCEL GST, STATUTORY & P&L EXPORTS (STAGE C3-B3C)
========================================== */
function exportGstSummaryExcel(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: GST Excel exports are restricted to administrators.", "error");
        return;
    }
    const range = _getValidatedExportDateRange("gst-from-date", "gst-to-date", "GST Summary");
    if (!range) return;
    const url = buildExcelExportUrl("/api/excel/exports/gst-summary", range);
    downloadExcelExport(url, "GST_Summary.xlsx", buttonEl);
}

function exportGstr1WorkingPaperExcel(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: GSTR-1 Working Paper Excel export is restricted to administrators.", "error");
        return;
    }
    const range = _getValidatedExportDateRange("gst-from-date", "gst-to-date", "GSTR-1 Working Paper");
    if (!range) return;
    const url = buildExcelExportUrl("/api/excel/exports/gstr1-working-paper", range);
    downloadExcelExport(url, "GSTR1_Accountant_Working_Paper.xlsx", buttonEl);
}

function exportStatutoryRegistersExcel(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Statutory Registers Excel export is restricted to administrators.", "error");
        return;
    }
    const range = _getValidatedExportDateRange("stat-from-date", "stat-to-date", "Statutory Registers");
    if (!range) return;
    const url = buildExcelExportUrl("/api/excel/exports/statutory-registers", range);
    downloadExcelExport(url, "Statutory_Registers.xlsx", buttonEl);
}

function exportProfitAndLossExcel(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Profit & Loss Excel export is restricted to administrators.", "error");
        return;
    }
    const range = _getValidatedExportDateRange("pnl-from-date", "pnl-to-date", "Profit & Loss");
    if (!range) return;
    const url = buildExcelExportUrl("/api/excel/exports/profit-and-loss", range);
    downloadExcelExport(url, "Profit_And_Loss.xlsx", buttonEl);
}

/* ==========================================
   PRODUCT MASTER EXCEL IMPORT (STAGE C3-C1)
========================================== */
let productImportPreviewState = null;

function _setProductImportFeedback(message, tone = "info") {
    const banner = document.getElementById("product-import-feedback-banner");
    if (!banner) return;
    if (!message) {
        banner.style.display = "none";
        banner.textContent = "";
        return;
    }
    const palettes = {
        info: { bg: "#eff6ff", border: "#93c5fd", color: "#1e3a8a" },
        success: { bg: "#f0fdf4", border: "#86efac", color: "#166534" },
        warning: { bg: "#fffbeb", border: "#fcd34d", color: "#92400e" },
        danger: { bg: "#fef2f2", border: "#fca5a5", color: "#991b1b" },
    };
    const p = palettes[tone] || palettes.info;
    banner.style.display = "block";
    banner.style.background = p.bg;
    banner.style.border = `1px solid ${p.border}`;
    banner.style.color = p.color;
    banner.textContent = message;
}

function resetProductImportState(keepSelectedFile = false) {
    productImportPreviewState = null;

    const fileInput = document.getElementById("product-import-file-input");
    const fileLabel = document.getElementById("product-import-file-label");
    const previewBtn = document.getElementById("btn-preview-product-import");
    const confirmBtn = document.getElementById("btn-confirm-product-import");
    const clearBtn = document.getElementById("btn-clear-product-import");
    const panel = document.getElementById("product-import-results-panel");
    const tbody = document.getElementById("product-import-preview-tbody");
    const sampleNote = document.getElementById("prod-import-sample-note");
    const metaHint = document.getElementById("prod-import-meta-hint");

    if (!keepSelectedFile) {
        if (fileInput) fileInput.value = "";
        if (fileLabel) fileLabel.textContent = "No file selected";
        if (previewBtn) previewBtn.disabled = true;
        if (clearBtn) clearBtn.style.display = "none";
    }
    if (confirmBtn) confirmBtn.disabled = true;
    if (panel) panel.style.display = "none";
    if (sampleNote) {
        sampleNote.style.display = "none";
        sampleNote.textContent = "";
    }
    if (metaHint) metaHint.textContent = "";
    ["prod-import-ready-count", "prod-import-duplicate-count", "prod-import-invalid-count"].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.textContent = "0";
    });
    if (tbody) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted">Select a .xlsx file and click Preview Import.</td></tr>';
    }
    _setProductImportFeedback("", "info");
}

function downloadProductImportTemplate(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Product Import Template is restricted to administrators.", "error");
        return;
    }
    downloadExcelExport("/api/excel/imports/products/template", "Product_Import_Template.xlsx", buttonEl);
}

function triggerProductImportFileSelect() {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Product Import is restricted to administrators.", "error");
        return;
    }
    const fileInput = document.getElementById("product-import-file-input");
    if (fileInput) fileInput.click();
}

function handleProductImportFileSelected(inputEl) {
    // Selecting a file must NEVER auto-preview or auto-commit; clear any previous preview token.
    resetProductImportState(true);
    const fileLabel = document.getElementById("product-import-file-label");
    const previewBtn = document.getElementById("btn-preview-product-import");
    const clearBtn = document.getElementById("btn-clear-product-import");
    const file = inputEl && inputEl.files ? inputEl.files[0] : null;

    if (!file) {
        if (fileLabel) fileLabel.textContent = "No file selected";
        if (previewBtn) previewBtn.disabled = true;
        if (clearBtn) clearBtn.style.display = "none";
        return;
    }

    if (fileLabel) fileLabel.textContent = file.name;
    if (clearBtn) clearBtn.style.display = "inline-flex";

    if (!file.name.toLowerCase().endsWith(".xlsx")) {
        if (previewBtn) previewBtn.disabled = true;
        _setProductImportFeedback(
            `Unsupported file "${file.name}". Please select a valid .xlsx Excel workbook.`,
            "danger"
        );
        return;
    }

    if (previewBtn) previewBtn.disabled = false;
    _setProductImportFeedback(
        `Selected "${file.name}". Click "Preview Import" to validate rows before importing.`,
        "info"
    );
}

async function _computeBrowserFileSha256(file) {
    if (!file || typeof window.crypto === "undefined" || !window.crypto.subtle) {
        return null;
    }
    try {
        const buffer = await file.arrayBuffer();
        const hashBuf = await window.crypto.subtle.digest("SHA-256", buffer);
        const bytes = Array.from(new Uint8Array(hashBuf));
        return bytes.map((b) => b.toString(16).padStart(2, "0")).join("");
    } catch (e) {
        return null;
    }
}

function _formatProductImportStatusBadge(status) {
    if (status === "Ready to Import" || status === "Imported") {
        return `<span class="badge badge-success">${escapeHtml(status)}</span>`;
    }
    if (status && status.startsWith("Duplicate")) {
        return `<span class="badge" style="background:#fef3c7; color:#92400e;">${escapeHtml(status)}</span>`;
    }
    return `<span class="badge badge-danger">${escapeHtml(status || "Invalid")}</span>`;
}

function _renderProductImportRows(rows) {
    const tbody = document.getElementById("product-import-preview-tbody");
    if (!tbody) return;
    if (!Array.isArray(rows) || rows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted" style="padding: 16px;">No product data rows found in the uploaded sheet.</td></tr>';
        return;
    }
    tbody.innerHTML = rows.map((r) => {
        const saleNum = Number(r.sale_rate);
        const mrpNum = Number(r.mrp);
        const saleText = Number.isFinite(saleNum) ? `₹${saleNum.toFixed(2)}` : escapeHtml(String(r.sale_rate ?? "-"));
        const mrpText = Number.isFinite(mrpNum) ? `₹${mrpNum.toFixed(2)}` : escapeHtml(String(r.mrp ?? "-"));
        return `
            <tr>
                <td class="text-center"><strong>${escapeHtml(String(r.row_number ?? ""))}</strong></td>
                <td>${_formatProductImportStatusBadge(r.status)}</td>
                <td style="font-weight: 600;">${escapeHtml(r.product_name || "-")}</td>
                <td>${escapeHtml(r.tax_group || "-")}</td>
                <td>${escapeHtml(r.category || "-")}</td>
                <td>${escapeHtml(r.unit || "-")}</td>
                <td><code>${escapeHtml(r.hsn_code || "-")}</code></td>
                <td class="text-right">${saleText}</td>
                <td class="text-right">${mrpText}</td>
                <td style="font-size: 11.5px; color: #334155;">${escapeHtml(r.message || "")}</td>
            </tr>
        `;
    }).join("");
}

async function previewProductImport(buttonEl = null) {
    if (!hasValidAuthSession()) {
        showToast("Session expired. Please login again.", "warning");
        return;
    }
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Product Import is restricted to administrators.", "error");
        return;
    }

    const fileInput = document.getElementById("product-import-file-input");
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (!file) {
        showToast("Please select a .xlsx file first.", "warning");
        return;
    }

    productImportPreviewState = null;
    const confirmBtn = document.getElementById("btn-confirm-product-import");
    if (confirmBtn) confirmBtn.disabled = true;

    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Previewing...";
    }

    try {
        const formData = new FormData();
        formData.append("file", file);

        const res = await fetch("/api/excel/imports/products/preview", {
            method: "POST",
            body: formData,
        });

        const payload = await res.json().catch(() => ({}));
        if (!res.ok) {
            const panel = document.getElementById("product-import-results-panel");
            if (panel) panel.style.display = "none";
            _setProductImportFeedback(
                "Preview failed: " + (payload.detail || `HTTP ${res.status}`),
                "danger"
            );
            return;
        }

        const summary = payload.summary || {};
        const readyCount = Number(summary.ready_count || 0);
        const dupCount = Number(summary.duplicate_count || 0);
        const invalidCount = Number(summary.invalid_count || 0);
        const sampleSkipped = Number(payload.sample_rows_skipped || 0);

        const elReady = document.getElementById("prod-import-ready-count");
        const elDup = document.getElementById("prod-import-duplicate-count");
        const elInv = document.getElementById("prod-import-invalid-count");
        const sampleNote = document.getElementById("prod-import-sample-note");
        const metaHint = document.getElementById("prod-import-meta-hint");
        const panel = document.getElementById("product-import-results-panel");

        if (elReady) elReady.textContent = String(readyCount);
        if (elDup) elDup.textContent = String(dupCount);
        if (elInv) elInv.textContent = String(invalidCount);
        if (sampleNote) {
            if (sampleSkipped > 0) {
                sampleNote.style.display = "inline";
                sampleNote.textContent = `(${sampleSkipped} template sample row ignored)`;
            } else {
                sampleNote.style.display = "none";
            }
        }
        if (metaHint) {
            metaHint.textContent = `File: ${payload.filename || file.name} • SHA-256: ${String(payload.file_sha256 || "").slice(0, 12)}...`;
        }
        if (panel) panel.style.display = "block";
        _renderProductImportRows(payload.rows || []);

        productImportPreviewState = {
            preview_token: payload.preview_token,
            file_sha256: payload.file_sha256,
            file_name: file.name,
            file_size: file.size,
            file_last_modified: file.lastModified,
            summary: summary,
        };

        if (confirmBtn) {
            confirmBtn.disabled = !(readyCount > 0 && payload.preview_token);
        }

        if (readyCount > 0) {
            _setProductImportFeedback(
                `Preview completed: ${readyCount} product(s) ready to import, ${dupCount} duplicate(s) skipped, ${invalidCount} invalid row(s). Click "Confirm Import" to save Ready products.`,
                "success"
            );
        } else {
            _setProductImportFeedback(
                `Preview completed with 0 products ready to import (${dupCount} duplicate, ${invalidCount} invalid). Fix errors or add new rows before importing.`,
                "warning"
            );
        }
    } catch (err) {
        _setProductImportFeedback("Network error during Excel preview: " + err.message, "danger");
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

async function commitProductImport(buttonEl = null) {
    if (!hasValidAuthSession()) {
        showToast("Session expired. Please login again.", "warning");
        return;
    }
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Product Import commit is restricted to administrators.", "error");
        return;
    }
    if (!productImportPreviewState || !productImportPreviewState.preview_token) {
        showToast("Please run Preview Import first.", "warning");
        return;
    }

    const summary = productImportPreviewState.summary || {};
    const readyCount = Number(summary.ready_count || 0);
    if (readyCount <= 0) {
        showToast("No Ready products are available to import.", "warning");
        return;
    }

    // Verify selected file has not changed since preview
    const fileInput = document.getElementById("product-import-file-input");
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (file) {
        if (
            file.name !== productImportPreviewState.file_name ||
            file.size !== productImportPreviewState.file_size
        ) {
            _setProductImportFeedback("Selected file changed after preview. Please click Preview Import again.", "warning");
            productImportPreviewState = null;
            if (buttonEl) buttonEl.disabled = true;
            return;
        }
        const liveSha = await _computeBrowserFileSha256(file);
        if (liveSha && liveSha.toLowerCase() !== String(productImportPreviewState.file_sha256 || "").toLowerCase()) {
            _setProductImportFeedback("Workbook contents changed on disk after preview. Please run Preview Import again.", "danger");
            productImportPreviewState = null;
            if (buttonEl) buttonEl.disabled = true;
            return;
        }
    }

    const confirmed = await showConfirmDialog({
        title: "Confirm Product Import",
        message:
            `Ready to Import: ${readyCount} product(s)\n` +
            `Skipped Duplicates: ${Number(summary.duplicate_count || 0)}\n` +
            `Invalid Rows: ${Number(summary.invalid_count || 0)}\n\n` +
            `Only Ready rows will be inserted into Product Catalog. Existing products will never be overwritten, and no stock quantities will be added.`,
        confirmText: "Confirm Import",
        cancelText: "Cancel",
    });
    if (!confirmed) {
        return;
    }

    const activeToken = productImportPreviewState.preview_token;
    const activeSha = productImportPreviewState.file_sha256;
    // Consume local UI preview state immediately so double-clicks cannot resubmit single-use token
    productImportPreviewState = null;

    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Importing...";
    }

    try {
        const res = await fetch("/api/excel/imports/products/commit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                preview_token: activeToken,
                file_sha256: activeSha,
            }),
        });
        const result = await res.json().catch(() => ({}));
        if (!res.ok) {
            _setProductImportFeedback(
                "Import failed: " + (result.detail || `HTTP ${res.status}`),
                "danger"
            );
            return;
        }

        const importedCount = Number(result.imported_count || 0);
        const dupCount = Number(result.skipped_duplicate_count || 0);
        const invalidCount = Number(result.invalid_count || 0);
        const failedCount = Number(result.failed_count || 0);

        const elReady = document.getElementById("prod-import-ready-count");
        const elDup = document.getElementById("prod-import-duplicate-count");
        const elInv = document.getElementById("prod-import-invalid-count");
        if (elReady) elReady.textContent = String(importedCount);
        if (elDup) elDup.textContent = String(dupCount);
        if (elInv) elInv.textContent = String(invalidCount);

        if (Array.isArray(result.rows) && result.rows.length > 0) {
            const tbody = document.getElementById("product-import-preview-tbody");
            if (tbody) {
                tbody.innerHTML = result.rows.map((r) => `
                    <tr>
                        <td class="text-center"><strong>${escapeHtml(String(r.row_number ?? ""))}</strong></td>
                        <td>${_formatProductImportStatusBadge(r.status)}</td>
                        <td style="font-weight: 600;">${escapeHtml(r.product_name || "-")}</td>
                        <td colspan="6" style="color: #475569; font-size: 12px;">Commit Result</td>
                        <td style="font-size: 11.5px; color: #334155;">${escapeHtml(r.message || "")}</td>
                    </tr>
                `).join("");
            }
        }

        _setProductImportFeedback(
            `Import Complete — Imported: ${importedCount}, Skipped Duplicates: ${dupCount}, Invalid: ${invalidCount}, Failed: ${failedCount}.`,
            importedCount > 0 ? "success" : "warning"
        );

        await refreshProductList();
        await loadMasterTables();
        notifyOtherTabsDataChanged();
    } catch (err) {
        _setProductImportFeedback("Network error during Product Import commit: " + err.message, "danger");
    } finally {
        if (buttonEl) {
            buttonEl.textContent = prevText;
            buttonEl.disabled = true; // Single-use token spent; require new preview before another commit
        }
    }
}

/* ==========================================
   CUSTOMER MASTER EXCEL IMPORT (STAGE C3-C2)
========================================== */
let customerImportPreviewState = null;

function _setCustomerImportFeedback(message, tone = "info") {
    const banner = document.getElementById("customer-import-feedback-banner");
    if (!banner) return;
    if (!message) {
        banner.style.display = "none";
        banner.textContent = "";
        return;
    }
    const palettes = {
        info: { bg: "#eff6ff", border: "#93c5fd", color: "#1e3a8a" },
        success: { bg: "#f0fdf4", border: "#86efac", color: "#166534" },
        warning: { bg: "#fffbeb", border: "#fcd34d", color: "#92400e" },
        danger: { bg: "#fef2f2", border: "#fca5a5", color: "#991b1b" },
    };
    const p = palettes[tone] || palettes.info;
    banner.style.display = "block";
    banner.style.background = p.bg;
    banner.style.border = `1px solid ${p.border}`;
    banner.style.color = p.color;
    banner.textContent = message;
}

function resetCustomerImportState(keepSelectedFile = false) {
    customerImportPreviewState = null;

    const fileInput = document.getElementById("customer-import-file-input");
    const fileLabel = document.getElementById("customer-import-file-label");
    const previewBtn = document.getElementById("btn-preview-customer-import");
    const confirmBtn = document.getElementById("btn-confirm-customer-import");
    const clearBtn = document.getElementById("btn-clear-customer-import");
    const panel = document.getElementById("customer-import-results-panel");
    const tbody = document.getElementById("customer-import-preview-tbody");
    const sampleNote = document.getElementById("cust-import-sample-note");
    const metaHint = document.getElementById("cust-import-meta-hint");

    if (!keepSelectedFile) {
        if (fileInput) fileInput.value = "";
        if (fileLabel) fileLabel.textContent = "No file selected";
        if (previewBtn) previewBtn.disabled = true;
        if (clearBtn) clearBtn.style.display = "none";
    }
    if (confirmBtn) confirmBtn.disabled = true;
    if (panel) panel.style.display = "none";
    if (sampleNote) {
        sampleNote.style.display = "none";
        sampleNote.textContent = "";
    }
    if (metaHint) metaHint.textContent = "";
    ["cust-import-ready-count", "cust-import-duplicate-count", "cust-import-invalid-count"].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.textContent = "0";
    });
    if (tbody) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted">Select a .xlsx file and click Preview Import.</td></tr>';
    }
    _setCustomerImportFeedback("", "info");
}

function downloadCustomerImportTemplate(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Customer Import Template is restricted to administrators.", "error");
        return;
    }
    downloadExcelExport("/api/excel/imports/customers/template", "Customer_Import_Template.xlsx", buttonEl);
}

function triggerCustomerImportFileSelect() {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Customer Import is restricted to administrators.", "error");
        return;
    }
    const fileInput = document.getElementById("customer-import-file-input");
    if (fileInput) fileInput.click();
}

function handleCustomerImportFileSelected(inputEl) {
    // Selecting a file must NEVER auto-preview or auto-commit; clear any previous preview token.
    resetCustomerImportState(true);
    const fileLabel = document.getElementById("customer-import-file-label");
    const previewBtn = document.getElementById("btn-preview-customer-import");
    const clearBtn = document.getElementById("btn-clear-customer-import");
    const file = inputEl && inputEl.files ? inputEl.files[0] : null;

    if (!file) {
        if (fileLabel) fileLabel.textContent = "No file selected";
        if (previewBtn) previewBtn.disabled = true;
        if (clearBtn) clearBtn.style.display = "none";
        return;
    }

    if (fileLabel) fileLabel.textContent = file.name;
    if (clearBtn) clearBtn.style.display = "inline-flex";

    if (!file.name.toLowerCase().endsWith(".xlsx")) {
        if (previewBtn) previewBtn.disabled = true;
        _setCustomerImportFeedback(
            `Unsupported file "${file.name}". Please select a valid .xlsx Excel workbook.`,
            "danger"
        );
        return;
    }

    if (previewBtn) previewBtn.disabled = false;
    _setCustomerImportFeedback(
        `Selected "${file.name}". Click "Preview Import" to validate customer rows before importing.`,
        "info"
    );
}

function _formatCustomerImportStatusBadge(status) {
    if (status === "Ready to Import" || status === "Imported") {
        return `<span class="badge badge-success">${escapeHtml(status)}</span>`;
    }
    if (status && status.startsWith("Duplicate")) {
        return `<span class="badge" style="background:#fef3c7; color:#92400e;">${escapeHtml(status)}</span>`;
    }
    return `<span class="badge badge-danger">${escapeHtml(status || "Invalid")}</span>`;
}

function _renderCustomerImportRows(rows) {
    const tbody = document.getElementById("customer-import-preview-tbody");
    if (!tbody) return;
    if (!Array.isArray(rows) || rows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted" style="padding: 16px;">No customer data rows found in the uploaded sheet.</td></tr>';
        return;
    }
    tbody.innerHTML = rows.map((r) => {
        const districtState = [r.district, r.state].filter(Boolean).join(" / ") || "-";
        return `
            <tr>
                <td class="text-center"><strong>${escapeHtml(String(r.row_number ?? ""))}</strong></td>
                <td>${_formatCustomerImportStatusBadge(r.status)}</td>
                <td style="font-weight: 600;">${escapeHtml(r.customer_name || "-")}</td>
                <td>${escapeHtml(r.mobile || "-")}</td>
                <td>${escapeHtml(r.village || "-")}</td>
                <td>${escapeHtml(r.taluka || "-")}</td>
                <td>${escapeHtml(districtState)}</td>
                <td><code>${escapeHtml(r.gstin || "-")}</code></td>
                <td>${escapeHtml(r.customer_group || "-")}</td>
                <td style="font-size: 11.5px; color: #334155;">${escapeHtml(r.message || "")}</td>
            </tr>
        `;
    }).join("");
}

async function previewCustomerImport(buttonEl = null) {
    if (!hasValidAuthSession()) {
        showToast("Session expired. Please login again.", "warning");
        return;
    }
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Customer Import is restricted to administrators.", "error");
        return;
    }

    const fileInput = document.getElementById("customer-import-file-input");
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (!file) {
        showToast("Please select a .xlsx file first.", "warning");
        return;
    }

    customerImportPreviewState = null;
    const confirmBtn = document.getElementById("btn-confirm-customer-import");
    if (confirmBtn) confirmBtn.disabled = true;

    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Previewing...";
    }

    try {
        const formData = new FormData();
        formData.append("file", file);

        const res = await fetch("/api/excel/imports/customers/preview", {
            method: "POST",
            body: formData,
        });

        const payload = await res.json().catch(() => ({}));
        if (!res.ok) {
            const panel = document.getElementById("customer-import-results-panel");
            if (panel) panel.style.display = "none";
            _setCustomerImportFeedback(
                "Preview failed: " + (payload.detail || `HTTP ${res.status}`),
                "danger"
            );
            return;
        }

        const summary = payload.summary || {};
        const readyCount = Number(summary.ready_count || 0);
        const dupCount = Number(summary.duplicate_count || 0);
        const invalidCount = Number(summary.invalid_count || 0);
        const sampleSkipped = Number(payload.sample_rows_skipped || 0);

        const elReady = document.getElementById("cust-import-ready-count");
        const elDup = document.getElementById("cust-import-duplicate-count");
        const elInv = document.getElementById("cust-import-invalid-count");
        const sampleNote = document.getElementById("cust-import-sample-note");
        const metaHint = document.getElementById("cust-import-meta-hint");
        const panel = document.getElementById("customer-import-results-panel");

        if (elReady) elReady.textContent = String(readyCount);
        if (elDup) elDup.textContent = String(dupCount);
        if (elInv) elInv.textContent = String(invalidCount);
        if (sampleNote) {
            if (sampleSkipped > 0) {
                sampleNote.style.display = "inline";
                sampleNote.textContent = `(${sampleSkipped} template sample row ignored)`;
            } else {
                sampleNote.style.display = "none";
            }
        }
        if (metaHint) {
            metaHint.textContent = `File: ${payload.filename || file.name} • SHA-256: ${String(payload.file_sha256 || "").slice(0, 12)}...`;
        }
        if (panel) panel.style.display = "block";
        _renderCustomerImportRows(payload.rows || []);

        customerImportPreviewState = {
            preview_token: payload.preview_token,
            file_sha256: payload.file_sha256,
            file_name: file.name,
            file_size: file.size,
            file_last_modified: file.lastModified,
            summary: summary,
        };

        if (confirmBtn) {
            confirmBtn.disabled = !(readyCount > 0 && payload.preview_token);
        }

        if (readyCount > 0) {
            _setCustomerImportFeedback(
                `Preview completed: ${readyCount} customer(s) ready to import, ${dupCount} duplicate(s) skipped, ${invalidCount} invalid row(s). Click "Confirm Import" to save Ready customers.`,
                "success"
            );
        } else {
            _setCustomerImportFeedback(
                `Preview completed with 0 customers ready to import (${dupCount} duplicate, ${invalidCount} invalid). Fix errors or add new rows before importing.`,
                "warning"
            );
        }
    } catch (err) {
        _setCustomerImportFeedback("Network error during Excel preview: " + err.message, "danger");
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

async function commitCustomerImport(buttonEl = null) {
    if (!hasValidAuthSession()) {
        showToast("Session expired. Please login again.", "warning");
        return;
    }
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Customer Import commit is restricted to administrators.", "error");
        return;
    }
    if (!customerImportPreviewState || !customerImportPreviewState.preview_token) {
        showToast("Please run Preview Import first.", "warning");
        return;
    }

    const summary = customerImportPreviewState.summary || {};
    const readyCount = Number(summary.ready_count || 0);
    if (readyCount <= 0) {
        showToast("No Ready customers are available to import.", "warning");
        return;
    }

    // Verify selected file has not changed since preview
    const fileInput = document.getElementById("customer-import-file-input");
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (file) {
        if (
            file.name !== customerImportPreviewState.file_name ||
            file.size !== customerImportPreviewState.file_size
        ) {
            _setCustomerImportFeedback("Selected file changed after preview. Please click Preview Import again.", "warning");
            customerImportPreviewState = null;
            if (buttonEl) buttonEl.disabled = true;
            return;
        }
        const liveSha = await _computeBrowserFileSha256(file);
        if (liveSha && liveSha.toLowerCase() !== String(customerImportPreviewState.file_sha256 || "").toLowerCase()) {
            _setCustomerImportFeedback("Workbook contents changed on disk after preview. Please run Preview Import again.", "danger");
            customerImportPreviewState = null;
            if (buttonEl) buttonEl.disabled = true;
            return;
        }
    }

    const confirmed = await showConfirmDialog({
        title: "Confirm Customer Import",
        message:
            `Ready to Import: ${readyCount} customer(s)\n` +
            `Skipped Duplicates: ${Number(summary.duplicate_count || 0)}\n` +
            `Invalid Rows: ${Number(summary.invalid_count || 0)}\n\n` +
            `Only Ready rows will be inserted into Customer Directory with zero opening/current balances and no Aadhaar data. Existing customers and balances will never be overwritten.`,
        confirmText: "Confirm Import",
        cancelText: "Cancel",
    });
    if (!confirmed) {
        return;
    }

    const activeToken = customerImportPreviewState.preview_token;
    const activeSha = customerImportPreviewState.file_sha256;
    // Consume local UI preview state immediately so double-clicks cannot resubmit single-use token
    customerImportPreviewState = null;

    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Importing...";
    }

    try {
        const res = await fetch("/api/excel/imports/customers/commit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                preview_token: activeToken,
                file_sha256: activeSha,
            }),
        });
        const result = await res.json().catch(() => ({}));
        if (!res.ok) {
            _setCustomerImportFeedback(
                "Import failed: " + (result.detail || `HTTP ${res.status}`),
                "danger"
            );
            return;
        }

        const importedCount = Number(result.imported_count || 0);
        const dupCount = Number(result.skipped_duplicate_count || 0);
        const invalidCount = Number(result.invalid_count || 0);
        const failedCount = Number(result.failed_count || 0);

        const elReady = document.getElementById("cust-import-ready-count");
        const elDup = document.getElementById("cust-import-duplicate-count");
        const elInv = document.getElementById("cust-import-invalid-count");
        if (elReady) elReady.textContent = String(importedCount);
        if (elDup) elDup.textContent = String(dupCount);
        if (elInv) elInv.textContent = String(invalidCount);

        if (Array.isArray(result.rows) && result.rows.length > 0) {
            const tbody = document.getElementById("customer-import-preview-tbody");
            if (tbody) {
                tbody.innerHTML = result.rows.map((r) => `
                    <tr>
                        <td class="text-center"><strong>${escapeHtml(String(r.row_number ?? ""))}</strong></td>
                        <td>${_formatCustomerImportStatusBadge(r.status)}</td>
                        <td style="font-weight: 600;">${escapeHtml(r.customer_name || "-")}</td>
                        <td colspan="6" style="color: #475569; font-size: 12px;">Commit Result</td>
                        <td style="font-size: 11.5px; color: #334155;">${escapeHtml(r.message || "")}</td>
                    </tr>
                `).join("");
            }
        }

        _setCustomerImportFeedback(
            `Import Complete — Imported: ${importedCount}, Skipped Duplicates: ${dupCount}, Invalid: ${invalidCount}, Failed: ${failedCount}.`,
            importedCount > 0 ? "success" : "warning"
        );

        await loadFarmerStatusList();
        await refreshCustomerList();
        notifyOtherTabsDataChanged();
    } catch (err) {
        _setCustomerImportFeedback("Network error during Customer Import commit: " + err.message, "danger");
    } finally {
        if (buttonEl) {
            buttonEl.textContent = prevText;
            buttonEl.disabled = true; // Single-use token spent; require new preview before another commit
        }
    }
}

/* ==========================================
   SUPPLIER MASTER EXCEL IMPORT (STAGE C3-C3)
========================================== */
let supplierImportPreviewState = null;

function _setSupplierImportFeedback(message, tone = "info") {
    const banner = document.getElementById("supplier-import-feedback-banner");
    if (!banner) return;
    if (!message) {
        banner.style.display = "none";
        banner.textContent = "";
        return;
    }
    const palettes = {
        info: { bg: "#eff6ff", border: "#93c5fd", color: "#1e3a8a" },
        success: { bg: "#f0fdf4", border: "#86efac", color: "#166534" },
        warning: { bg: "#fffbeb", border: "#fcd34d", color: "#92400e" },
        danger: { bg: "#fef2f2", border: "#fca5a5", color: "#991b1b" },
    };
    const p = palettes[tone] || palettes.info;
    banner.style.display = "block";
    banner.style.background = p.bg;
    banner.style.border = `1px solid ${p.border}`;
    banner.style.color = p.color;
    banner.textContent = message;
}

function resetSupplierImportState(keepSelectedFile = false) {
    supplierImportPreviewState = null;

    const fileInput = document.getElementById("supplier-import-file-input");
    const fileLabel = document.getElementById("supplier-import-file-label");
    const previewBtn = document.getElementById("btn-preview-supplier-import");
    const confirmBtn = document.getElementById("btn-confirm-supplier-import");
    const clearBtn = document.getElementById("btn-clear-supplier-import");
    const panel = document.getElementById("supplier-import-results-panel");
    const tbody = document.getElementById("supplier-import-preview-tbody");
    const sampleNote = document.getElementById("supp-import-sample-note");
    const metaHint = document.getElementById("supp-import-meta-hint");

    if (!keepSelectedFile) {
        if (fileInput) fileInput.value = "";
        if (fileLabel) fileLabel.textContent = "No file selected";
        if (previewBtn) previewBtn.disabled = true;
        if (clearBtn) clearBtn.style.display = "none";
    }
    if (confirmBtn) confirmBtn.disabled = true;
    if (panel) panel.style.display = "none";
    if (sampleNote) {
        sampleNote.style.display = "none";
        sampleNote.textContent = "";
    }
    if (metaHint) metaHint.textContent = "";
    ["supp-import-ready-count", "supp-import-duplicate-count", "supp-import-invalid-count"].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.textContent = "0";
    });
    if (tbody) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted">Select a .xlsx file and click Preview Import.</td></tr>';
    }
    _setSupplierImportFeedback("", "info");
}

function downloadSupplierImportTemplate(buttonEl = null) {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Supplier Import Template is restricted to administrators.", "error");
        return;
    }
    downloadExcelExport("/api/excel/imports/suppliers/template", "Supplier_Import_Template.xlsx", buttonEl);
}

function triggerSupplierImportFileSelect() {
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Supplier Import is restricted to administrators.", "error");
        return;
    }
    const fileInput = document.getElementById("supplier-import-file-input");
    if (fileInput) fileInput.click();
}

function handleSupplierImportFileSelected(inputEl) {
    // Selecting a file must NEVER auto-preview or auto-commit; clear any previous preview token.
    resetSupplierImportState(true);
    const fileLabel = document.getElementById("supplier-import-file-label");
    const previewBtn = document.getElementById("btn-preview-supplier-import");
    const clearBtn = document.getElementById("btn-clear-supplier-import");
    const file = inputEl && inputEl.files ? inputEl.files[0] : null;

    if (!file) {
        if (fileLabel) fileLabel.textContent = "No file selected";
        if (previewBtn) previewBtn.disabled = true;
        if (clearBtn) clearBtn.style.display = "none";
        return;
    }

    if (fileLabel) fileLabel.textContent = file.name;
    if (clearBtn) clearBtn.style.display = "inline-flex";

    if (!file.name.toLowerCase().endsWith(".xlsx")) {
        if (previewBtn) previewBtn.disabled = true;
        _setSupplierImportFeedback(
            `Unsupported file "${file.name}". Please select a valid .xlsx Excel workbook.`,
            "danger"
        );
        return;
    }

    if (previewBtn) previewBtn.disabled = false;
    _setSupplierImportFeedback(
        `Selected "${file.name}". Click "Preview Import" to validate supplier rows before importing.`,
        "info"
    );
}

function _formatSupplierImportStatusBadge(status) {
    if (status === "Ready to Import" || status === "Imported") {
        return `<span class="badge badge-success">${escapeHtml(status)}</span>`;
    }
    if (status && status.startsWith("Duplicate")) {
        return `<span class="badge" style="background:#fef3c7; color:#92400e;">${escapeHtml(status)}</span>`;
    }
    return `<span class="badge badge-danger">${escapeHtml(status || "Invalid")}</span>`;
}

function _renderSupplierImportRows(rows) {
    const tbody = document.getElementById("supplier-import-preview-tbody");
    if (!tbody) return;
    if (!Array.isArray(rows) || rows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted" style="padding: 16px;">No supplier data rows found in the uploaded sheet.</td></tr>';
        return;
    }
    tbody.innerHTML = rows.map((r) => {
        const cityState = [r.city, r.state].filter(Boolean).join(" / ") || "-";
        const titleAddr = r.address ? ` title="Address: ${escapeHtml(r.address)}"` : "";
        return `
            <tr>
                <td class="text-center"><strong>${escapeHtml(String(r.row_number ?? ""))}</strong></td>
                <td>${_formatSupplierImportStatusBadge(r.status)}</td>
                <td style="font-weight: 600;">${escapeHtml(r.supplier_name || "-")}</td>
                <td>${escapeHtml(r.contact_person || "-")}</td>
                <td>${escapeHtml(r.mobile || "-")}</td>
                <td style="font-size: 11.5px;">${escapeHtml(r.email || "-")}</td>
                <td${titleAddr}>${escapeHtml(cityState)}</td>
                <td><code>${escapeHtml(r.gstin || "-")}</code></td>
                <td><code>${escapeHtml(r.dl_number || "-")}</code></td>
                <td style="font-size: 11.5px; color: #334155;">${escapeHtml(r.message || "")}</td>
            </tr>
        `;
    }).join("");
}

async function previewSupplierImport(buttonEl = null) {
    if (!hasValidAuthSession()) {
        showToast("Session expired. Please login again.", "warning");
        return;
    }
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Supplier Import is restricted to administrators.", "error");
        return;
    }

    const fileInput = document.getElementById("supplier-import-file-input");
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (!file) {
        showToast("Please select a .xlsx file first.", "warning");
        return;
    }

    supplierImportPreviewState = null;
    const confirmBtn = document.getElementById("btn-confirm-supplier-import");
    if (confirmBtn) confirmBtn.disabled = true;

    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Previewing...";
    }

    try {
        const formData = new FormData();
        formData.append("file", file);

        const res = await fetch("/api/excel/imports/suppliers/preview", {
            method: "POST",
            body: formData,
        });

        const payload = await res.json().catch(() => ({}));
        if (!res.ok) {
            const panel = document.getElementById("supplier-import-results-panel");
            if (panel) panel.style.display = "none";
            _setSupplierImportFeedback(
                "Preview failed: " + (payload.detail || `HTTP ${res.status}`),
                "danger"
            );
            return;
        }

        const summary = payload.summary || {};
        const readyCount = Number(summary.ready_count || 0);
        const dupCount = Number(summary.duplicate_count || 0);
        const invalidCount = Number(summary.invalid_count || 0);
        const sampleSkipped = Number(payload.sample_rows_skipped || 0);

        const elReady = document.getElementById("supp-import-ready-count");
        const elDup = document.getElementById("supp-import-duplicate-count");
        const elInv = document.getElementById("supp-import-invalid-count");
        const sampleNote = document.getElementById("supp-import-sample-note");
        const metaHint = document.getElementById("supp-import-meta-hint");
        const panel = document.getElementById("supplier-import-results-panel");

        if (elReady) elReady.textContent = String(readyCount);
        if (elDup) elDup.textContent = String(dupCount);
        if (elInv) elInv.textContent = String(invalidCount);
        if (sampleNote) {
            if (sampleSkipped > 0) {
                sampleNote.style.display = "inline";
                sampleNote.textContent = `(${sampleSkipped} template sample row ignored)`;
            } else {
                sampleNote.style.display = "none";
            }
        }
        if (metaHint) {
            metaHint.textContent = `File: ${payload.filename || file.name} • SHA-256: ${String(payload.file_sha256 || "").slice(0, 12)}...`;
        }
        if (panel) panel.style.display = "block";
        _renderSupplierImportRows(payload.rows || []);

        supplierImportPreviewState = {
            preview_token: payload.preview_token,
            file_sha256: payload.file_sha256,
            file_name: file.name,
            file_size: file.size,
            file_last_modified: file.lastModified,
            summary: summary,
        };

        if (confirmBtn) {
            confirmBtn.disabled = !(readyCount > 0 && payload.preview_token);
        }

        if (readyCount > 0) {
            _setSupplierImportFeedback(
                `Preview completed: ${readyCount} supplier(s) ready to import, ${dupCount} duplicate(s) skipped, ${invalidCount} invalid row(s). Click "Confirm Import" to save Ready suppliers.`,
                "success"
            );
        } else {
            _setSupplierImportFeedback(
                `Preview completed with 0 suppliers ready to import (${dupCount} duplicate, ${invalidCount} invalid). Fix errors or add new rows before importing.`,
                "warning"
            );
        }
    } catch (err) {
        _setSupplierImportFeedback("Network error during Excel preview: " + err.message, "danger");
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

async function commitSupplierImport(buttonEl = null) {
    if (!hasValidAuthSession()) {
        showToast("Session expired. Please login again.", "warning");
        return;
    }
    if (!currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Supplier Import commit is restricted to administrators.", "error");
        return;
    }
    if (!supplierImportPreviewState || !supplierImportPreviewState.preview_token) {
        showToast("Please run Preview Import first.", "warning");
        return;
    }

    const summary = supplierImportPreviewState.summary || {};
    const readyCount = Number(summary.ready_count || 0);
    if (readyCount <= 0) {
        showToast("No Ready suppliers are available to import.", "warning");
        return;
    }

    // Verify selected file has not changed since preview
    const fileInput = document.getElementById("supplier-import-file-input");
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (file) {
        if (
            file.name !== supplierImportPreviewState.file_name ||
            file.size !== supplierImportPreviewState.file_size
        ) {
            _setSupplierImportFeedback("Selected file changed after preview. Please click Preview Import again.", "warning");
            supplierImportPreviewState = null;
            if (buttonEl) buttonEl.disabled = true;
            return;
        }
        const liveSha = await _computeBrowserFileSha256(file);
        if (liveSha && liveSha.toLowerCase() !== String(supplierImportPreviewState.file_sha256 || "").toLowerCase()) {
            _setSupplierImportFeedback("Workbook contents changed on disk after preview. Please run Preview Import again.", "danger");
            supplierImportPreviewState = null;
            if (buttonEl) buttonEl.disabled = true;
            return;
        }
    }

    const confirmed = await showConfirmDialog({
        title: "Confirm Supplier Import?",
        message:
            `Ready to Import: ${readyCount} supplier(s)\n` +
            `Skipped Duplicates: ${Number(summary.duplicate_count || 0)}\n` +
            `Invalid Rows: ${Number(summary.invalid_count || 0)}\n\n` +
            `Only Ready rows will be inserted into Supplier Directory with zero opening/current balances and no PAN or banking details. Existing suppliers and accounting history will never be overwritten.`,
        confirmText: "Confirm Import",
        cancelText: "Cancel",
    });
    if (!confirmed) {
        return;
    }

    const activeToken = supplierImportPreviewState.preview_token;
    const activeSha = supplierImportPreviewState.file_sha256;
    // Consume local UI preview state immediately so double-clicks cannot resubmit single-use token
    supplierImportPreviewState = null;

    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Importing...";
    }

    try {
        const res = await fetch("/api/excel/imports/suppliers/commit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                preview_token: activeToken,
                file_sha256: activeSha,
            }),
        });
        const result = await res.json().catch(() => ({}));
        if (!res.ok) {
            _setSupplierImportFeedback(
                "Import failed: " + (result.detail || `HTTP ${res.status}`),
                "danger"
            );
            return;
        }

        const importedCount = Number(result.imported_count || 0);
        const dupCount = Number(result.skipped_duplicate_count || 0);
        const invalidCount = Number(result.invalid_count || 0);
        const failedCount = Number(result.failed_count || 0);

        const elReady = document.getElementById("supp-import-ready-count");
        const elDup = document.getElementById("supp-import-duplicate-count");
        const elInv = document.getElementById("supp-import-invalid-count");
        if (elReady) elReady.textContent = String(importedCount);
        if (elDup) elDup.textContent = String(dupCount);
        if (elInv) elInv.textContent = String(invalidCount);

        if (Array.isArray(result.rows) && result.rows.length > 0) {
            const tbody = document.getElementById("supplier-import-preview-tbody");
            if (tbody) {
                tbody.innerHTML = result.rows.map((r) => `
                    <tr>
                        <td class="text-center"><strong>${escapeHtml(String(r.row_number ?? ""))}</strong></td>
                        <td>${_formatSupplierImportStatusBadge(r.status)}</td>
                        <td style="font-weight: 600;">${escapeHtml(r.supplier_name || "-")}</td>
                        <td colspan="6" style="color: #475569; font-size: 12px;">Commit Result</td>
                        <td style="font-size: 11.5px; color: #334155;">${escapeHtml(r.message || "")}</td>
                    </tr>
                `).join("");
            }
        }

        _setSupplierImportFeedback(
            `Import Complete — Imported: ${importedCount}, Skipped Duplicates: ${dupCount}, Invalid: ${invalidCount}, Failed: ${failedCount}.`,
            importedCount > 0 ? "success" : "warning"
        );

        await loadSupplierKhata();
        await refreshSupplierList();
        notifyOtherTabsDataChanged();
    } catch (err) {
        _setSupplierImportFeedback("Network error during Supplier Import commit: " + err.message, "danger");
    } finally {
        if (buttonEl) {
            buttonEl.textContent = prevText;
            buttonEl.disabled = true; // Single-use token spent; require new preview before another commit
        }
    }
}

/* ==========================================
   MANUFACTURER & UNIT QUICK ADD FUNCTIONS
========================================== */
function handleMfgSelectChange(selectEl) {
    if (selectEl.value === "__ADD_NEW__") {
        selectEl.value = "";
        openAddManufacturerModal();
    }
}

function handleUnitSelectChange(selectEl) {
    if (selectEl.value === "__ADD_NEW__") {
        selectEl.value = "";
        openAddUnitModal();
    } else {
        updatePackSizeOptions();
    }
}

function updatePackSizeOptions() {
    const unitSelect = document.getElementById("m-prod-unit");
    const packSelect = document.getElementById("m-prod-pack-size");
    const packCustom = document.getElementById("m-prod-pack-custom");
    if (!unitSelect || !packSelect) return;

    const selectedText = unitSelect.options[unitSelect.selectedIndex] ? unitSelect.options[unitSelect.selectedIndex].text.toLowerCase() : "";

    let options = [];
    if (selectedText.includes("ltr") || selectedText.includes("ml") || selectedText.includes("litre") || selectedText.includes("bottle") || selectedText.includes("btl")) {
        options = [
            '<option value="1 LTR">1 Litre (1 LTR)</option>',
            '<option value="500 ML">500 Millilitre (500 ML)</option>',
            '<option value="250 ML">250 Millilitre (250 ML)</option>',
            '<option value="100 ML">100 Millilitre (100 ML)</option>',
            '<option value="50 ML">50 Millilitre (50 ML)</option>',
            '<option value="2 LTR">2 Litre (2 LTR)</option>',
            '<option value="5 LTR">5 Litre (5 LTR)</option>',
            '<option value="10 LTR">10 Litre (10 LTR)</option>',
            '<option value="20 LTR">20 Litre (20 LTR)</option>'
        ];
    } else if (selectedText.includes("kg") || selectedText.includes("gm") || selectedText.includes("kilogram") || selectedText.includes("gram") || selectedText.includes("bag") || selectedText.includes("qtl") || selectedText.includes("quintal")) {
        options = [
            '<option value="50 KG">50 Kilogram (50 KG Bag)</option>',
            '<option value="25 KG">25 Kilogram (25 KG)</option>',
            '<option value="10 KG">10 Kilogram (10 KG)</option>',
            '<option value="5 KG">5 Kilogram (5 KG)</option>',
            '<option value="2 KG">2 Kilogram (2 KG)</option>',
            '<option value="1 KG">1 Kilogram (1 KG)</option>',
            '<option value="500 GM">500 Gram (500 GM)</option>',
            '<option value="250 GM">250 Gram (250 GM)</option>',
            '<option value="100 GM">100 Gram (100 GM)</option>',
            '<option value="50 GM">50 Gram (50 GM)</option>',
            '<option value="1 QTL">1 Quintal (100 KG / 1 QTL)</option>'
        ];
    } else {
        options = [
            '<option value="1 Unit">1 Unit / Pack</option>',
            '<option value="100 GM">100 Gram (100 GM)</option>',
            '<option value="250 GM">250 Gram (250 GM)</option>',
            '<option value="500 GM">500 Gram (500 GM)</option>',
            '<option value="1 KG">1 Kilogram (1 KG)</option>',
            '<option value="100 ML">100 Millilitre (100 ML)</option>',
            '<option value="250 ML">250 Millilitre (250 ML)</option>',
            '<option value="500 ML">500 Millilitre (500 ML)</option>',
            '<option value="1 LTR">1 Litre (1 LTR)</option>'
        ];
    }

    options.push('<option value="__CUSTOM__">✏️ Custom Pack Size...</option>');
    packSelect.innerHTML = options.join("");

    if (packCustom) packCustom.style.display = "none";
}

function handlePackSizeChange(selectEl) {
    const packCustom = document.getElementById("m-prod-pack-custom");
    if (!packCustom) return;

    if (selectEl.value === "__CUSTOM__") {
        packCustom.style.display = "block";
        packCustom.focus();
    } else {
        packCustom.style.display = "none";
    }
}

function openAddManufacturerModal() {
    document.getElementById("new-mfg-name").value = "";
    document.getElementById("new-mfg-contact").value = "";
    document.getElementById("new-mfg-mobile").value = "";
    const modal = document.getElementById("modal-add-manufacturer");
    if (modal) modal.style.display = "flex";
    setTimeout(() => {
        const inp = document.getElementById("new-mfg-name");
        if (inp) inp.focus();
    }, 100);
}

function closeAddManufacturerModal() {
    const modal = document.getElementById("modal-add-manufacturer");
    if (modal) modal.style.display = "none";
}

async function saveNewManufacturer() {
    const nameInp = document.getElementById("new-mfg-name");
    const name = nameInp ? nameInp.value.trim() : "";
    const contact = document.getElementById("new-mfg-contact")?.value.trim() || "";
    const mobile = document.getElementById("new-mfg-mobile")?.value.trim() || "";

    if (!name) {
        showToast("Manufacturer / Company Name is required!", "warning");
        return;
    }

    try {
        const res = await fetch("/api/masters/manufacturers", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                manufacturer_name: name,
                contact_person: contact || null,
                mobile: mobile || null
            })
        });

        if (res.ok) {
            const data = await res.json();
            showToast(`✅ Manufacturer "${name}" added successfully!`, "success");
            closeAddManufacturerModal();
            await loadLookups();
            const mfgSelect = document.getElementById("m-prod-mfg");
            if (mfgSelect && data.manufacturer_id) {
                mfgSelect.value = data.manufacturer_id;
            }
        } else {
            const err = await res.json();
            showToast("Failed to add manufacturer: " + (err.detail || "Server error"), "error");
        }
    } catch (err) {
        showToast("Error saving manufacturer: " + err.message, "error");
    }
}

function openAddUnitModal() {
    document.getElementById("new-unit-name").value = "";
    document.getElementById("new-unit-symbol").value = "";
    const modal = document.getElementById("modal-add-unit");
    if (modal) modal.style.display = "flex";
    setTimeout(() => {
        const inp = document.getElementById("new-unit-name");
        if (inp) inp.focus();
    }, 100);
}

function closeAddUnitModal() {
    const modal = document.getElementById("modal-add-unit");
    if (modal) modal.style.display = "none";
}

async function saveNewUnit() {
    const nameInp = document.getElementById("new-unit-name");
    const symbolInp = document.getElementById("new-unit-symbol");
    const name = nameInp ? nameInp.value.trim() : "";
    const symbol = symbolInp ? symbolInp.value.trim() : "";

    if (!name || !symbol) {
        showToast("Both Unit Name and Unit Symbol are required!", "warning");
        return;
    }

    try {
        const res = await fetch("/api/masters/units", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                unit_name: name,
                symbol: symbol
            })
        });

        if (res.ok) {
            const data = await res.json();
            showToast(`✅ Unit "${name} (${symbol})" added successfully!`, "success");
            closeAddUnitModal();
            await loadLookups();
            const unitSelect = document.getElementById("m-prod-unit");
            if (unitSelect && data.unit_id) {
                unitSelect.value = data.unit_id;
            }
        } else {
            const err = await res.json();
            showToast("Failed to add unit: " + (err.detail || "Server error"), "error");
        }
    } catch (err) {
        showToast("Error saving unit: " + err.message, "error");
    }
}

/* ==========================================
   PRODUCT DELETE & MULTI-PRODUCT SELECTION
========================================== */

async function deleteProductCatalogItem(productId, productName) {
    const confirmed = await showConfirmDialog({
        title: "Delete Product from Catalog",
        message: `Are you sure you want to delete product "${productName}"?\n\nThis will remove it from the Product Catalog and Live Stock listing.`,
        confirmText: "Delete Product",
        cancelText: "Cancel",
        danger: true,
    });
    if (!confirmed) {
        return;
    }

    try {
        const res = await fetch(`/api/masters/products/${productId}`, {
            method: "DELETE"
        });
        if (res.ok) {
            showToast(`✅ Product "${productName}" deleted successfully!`, "success");
            await refreshProductList();
            await loadMasterTables();
            await refreshInventoryList();
            populateStockProductDropdown();
        } else {
            const err = await res.json();
            showToast("Failed to delete product: " + (err.detail || "Server error"), "error");
        }
    } catch (err) {
        showToast("Error deleting product: " + err.message, "error");
    }
}

let multiSelectedItems = {}; // product_id -> { product, qty }

function openMultiProductModal() {
    multiSelectedItems = {};
    const searchInp = document.getElementById("multi-prod-search");
    if (searchInp) searchInp.value = "";
    
    const catSelect = document.getElementById("multi-prod-cat");
    if (catSelect && allLookups.categories) {
        catSelect.innerHTML = '<option value="">All Categories</option>' + 
            allLookups.categories.map(c => `<option value="${c.category_id}">${c.category_name}</option>`).join("");
    }

    const modal = document.getElementById("modal-multi-product");
    if (modal) modal.style.display = "flex";

    renderMultiProductList();
    setTimeout(() => {
        if (searchInp) searchInp.focus();
    }, 100);
}

function closeMultiProductModal() {
    const modal = document.getElementById("modal-multi-product");
    if (modal) modal.style.display = "none";
}

function renderMultiProductList() {
    const container = document.getElementById("multi-prod-list-container");
    if (!container) return;

    const search = (document.getElementById("multi-prod-search")?.value || "").trim().toLowerCase();
    const catFilter = document.getElementById("multi-prod-cat")?.value || "";

    let items = allProducts || [];

    if (search) {
        items = items.filter(p => 
            (p.product_name && p.product_name.toLowerCase().includes(search)) ||
            (p.hsn_code && p.hsn_code.toLowerCase().includes(search)) ||
            (p.manufacturer_name && p.manufacturer_name.toLowerCase().includes(search)) ||
            (p.category_name && p.category_name.toLowerCase().includes(search))
        );
    }
    if (catFilter) {
        items = items.filter(p => String(p.category_id) === String(catFilter));
    }

    if (items.length === 0) {
        container.innerHTML = `<div style="grid-column: 1 / -1; padding: 24px; text-align: center; color: #64748b;">No products found matching "${search || 'filters'}".</div>`;
        updateMultiProductSelectionSummary();
        return;
    }

    container.innerHTML = items.map(p => {
        const isSelected = !!multiSelectedItems[p.product_id];
        const currentQty = isSelected ? multiSelectedItems[p.product_id].qty : 1;
        const stockVal = parseFloat(p.total_stock) || 0;
        const saleRate = parseFloat(p.default_sale_rate) || 0;

        let stockBadge = `<span class="badge badge-success">${stockVal} ${p.unit_symbol || 'Nos'}</span>`;
        if (stockVal <= 0) {
            stockBadge = `<span class="badge badge-danger">0 Out of Stock</span>`;
        }

        return `
            <div style="border: ${isSelected ? '2px solid #16a34a' : '1px solid #cbd5e1'}; background: ${isSelected ? '#f0fdf4' : '#ffffff'}; border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; justify-content: space-between; gap: 8px; transition: all 0.15s ease;">
                <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 8px;">
                    <div style="display: flex; gap: 8px; align-items: flex-start;">
                        <input type="checkbox" id="chk-multi-${p.product_id}" ${isSelected ? 'checked' : ''} onchange="toggleMultiProductSelection(${p.product_id})" style="width: 18px; height: 18px; margin-top: 2px; cursor: pointer; accent-color: #16a34a;">
                        <div>
                            <div style="font-weight: 700; font-size: 14px; color: #1e293b; line-height: 1.2;">${p.product_name}</div>
                            <div style="font-size: 11.5px; color: #64748b; margin-top: 2px;">
                                ${p.category_name || 'General'} • ${p.manufacturer_name || '-'}
                            </div>
                        </div>
                    </div>
                    ${stockBadge}
                </div>

                <div style="display: flex; justify-content: space-between; align-items: center; background: #f8fafc; padding: 6px 8px; border-radius: 6px; border: 1px solid #f1f5f9;">
                    <div>
                        <span style="font-size: 11px; color: #64748b; display: block;">Rate:</span>
                        <span style="font-weight: 800; color: #047857; font-size: 14px;">₹${saleRate.toFixed(2)}</span>
                    </div>

                    <div style="display: flex; align-items: center; gap: 4px;">
                        <button type="button" class="btn btn-sm btn-secondary" onclick="stepMultiProdQty(${p.product_id}, -1)" style="padding: 2px 8px; font-weight: 800;">-</button>
                        <input type="number" id="qty-multi-${p.product_id}" value="${currentQty}" min="0.1" step="any" onchange="updateMultiProdQtyFromInput(${p.product_id}, this.value)" style="width: 50px; text-align: center; font-weight: 700; padding: 3px 2px; border: 1px solid #cbd5e1; border-radius: 4px;">
                        <button type="button" class="btn btn-sm btn-secondary" onclick="stepMultiProdQty(${p.product_id}, 1)" style="padding: 2px 8px; font-weight: 800;">+</button>
                    </div>
                </div>
            </div>
        `;
    }).join("");

    updateMultiProductSelectionSummary();
}

function toggleMultiProductSelection(prodId) {
    const p = (allProducts || []).find(x => x.product_id === prodId);
    if (!p) return;

    if (multiSelectedItems[prodId]) {
        delete multiSelectedItems[prodId];
    } else {
        const qtyInp = document.getElementById(`qty-multi-${prodId}`);
        const qty = qtyInp ? parseFloat(qtyInp.value) || 1 : 1;
        multiSelectedItems[prodId] = { product: p, qty: qty };
    }
    renderMultiProductList();
}

function stepMultiProdQty(prodId, delta) {
    const qtyInp = document.getElementById(`qty-multi-${prodId}`);
    let cur = qtyInp ? parseFloat(qtyInp.value) || 1 : 1;
    cur = Math.max(0.1, cur + delta);
    if (qtyInp) qtyInp.value = cur;

    if (multiSelectedItems[prodId]) {
        multiSelectedItems[prodId].qty = cur;
    } else {
        const p = (allProducts || []).find(x => x.product_id === prodId);
        if (p) multiSelectedItems[prodId] = { product: p, qty: cur };
    }
    renderMultiProductList();
}

function updateMultiProdQtyFromInput(prodId, val) {
    const num = Math.max(0.1, parseFloat(val) || 1);
    if (multiSelectedItems[prodId]) {
        multiSelectedItems[prodId].qty = num;
    } else {
        const p = (allProducts || []).find(x => x.product_id === prodId);
        if (p) multiSelectedItems[prodId] = { product: p, qty: num };
    }
    updateMultiProductSelectionSummary();
}

function updateMultiProductSelectionSummary() {
    const countEl = document.getElementById("multi-prod-selected-count");
    const totalEl = document.getElementById("multi-prod-selected-total");

    const selectedKeys = Object.keys(multiSelectedItems);
    let totalAmt = 0;

    selectedKeys.forEach(k => {
        const item = multiSelectedItems[k];
        const rate = parseFloat(item.product.default_sale_rate) || 0;
        totalAmt += (item.qty * rate);
    });

    if (countEl) countEl.textContent = selectedKeys.length;
    if (totalEl) totalEl.textContent = `₹${totalAmt.toFixed(2)}`;
}

async function addSelectedMultiProductsToCart() {
    const selectedKeys = Object.keys(multiSelectedItems);
    if (selectedKeys.length === 0) {
        showToast("Please select at least 1 product.", "warning");
        return;
    }

    let addedCount = 0;

    for (const key of selectedKeys) {
        const { product, qty } = multiSelectedItems[key];
        const prodId = product.product_id;

        try {
            const res = await fetch(`/api/inventory/batches/${prodId}`);
            const batches = await res.json();

            if (!batches || batches.length === 0) {
                console.warn(`No stock batch found for product: ${product.product_name}`);
                continue;
            }

            const topBatch = batches[0];
            const availQty = parseFloat(topBatch.current_qty) || 0;
            const useQty = Math.min(qty, availQty > 0 ? availQty : qty);
            const rate = parseFloat(topBatch.sale_rate) || parseFloat(product.default_sale_rate) || 0;
            const mrp = parseFloat(topBatch.mrp) || rate;

            const cgst = parseFloat(product.cgst_rate) || 0;
            const sgst = parseFloat(product.sgst_rate) || 0;
            const igst = parseFloat(product.igst_rate) || 0;

            const tax = calculatePosLineTax(useQty, rate, 0, cgst, sgst, igst);

            const cartItem = {
                product_id: prodId,
                product_name: product.product_name,
                batch_id: topBatch.batch_id,
                batch_no: topBatch.batch_no,
                exp_date: topBatch.exp_date || '',
                hsn_code: product.hsn_code || '',
                unit_name: product.unit_symbol || 'Nos',
                qty: useQty,
                sale_rate: rate,
                mrp: mrp,
                discount_percent: 0,
                discount_amount: tax.discount_amount,
                taxable_amount: tax.taxable_amount,
                cgst_rate: tax.cgst_rate,
                cgst_amount: tax.cgst_amount,
                sgst_rate: tax.sgst_rate,
                sgst_amount: tax.sgst_amount,
                igst_rate: tax.igst_rate,
                igst_amount: tax.igst_amount,
                total_amount: tax.total_amount
            };

            currentCart.push(cartItem);
            addedCount++;
        } catch (err) {
            console.error(`Error adding product ${product.product_name} to cart:`, err);
        }
    }

    renderPosCart();
    closeMultiProductModal();
    showToast(`✅ Added ${addedCount} selected product(s) to Counter Bill!`, addedCount > 0 ? "success" : "warning");
}

/* ==========================================
   OFFLINE & MULTI-DEVICE BILL PERSISTENCE
========================================== */

function saveBillToLocalBackup(salePayload, saleId) {
    try {
        const stored = JSON.parse(localStorage.getItem("krushidhan_saved_bills") || "[]");
        stored.unshift({
            sale_id: saleId,
            payload: salePayload,
            saved_at: new Date().toISOString()
        });
        if (stored.length > 150) stored.length = 150;
        localStorage.setItem("krushidhan_saved_bills", JSON.stringify(stored));
    } catch (e) {
        console.warn("Could not save bill to localStorage:", e);
    }
}

const PENDING_BILLS_STORAGE_KEY = "krushidhan_pending_bills";
const PENDING_BILLS_REVIEW_STORAGE_KEY = "krushidhan_pending_bills_review";
let isSyncingPendingBills = false;

function readPendingBillsQueue() {
    try {
        const raw = localStorage.getItem(PENDING_BILLS_STORAGE_KEY);
        const parsed = JSON.parse(raw || "[]");
        return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
        console.warn("Could not read pending bills queue from localStorage:", e);
        return [];
    }
}

function writePendingBillsQueue(entries) {
    try {
        localStorage.setItem(PENDING_BILLS_STORAGE_KEY, JSON.stringify(Array.isArray(entries) ? entries : []));
        return true;
    } catch (e) {
        console.warn("Could not write pending bills queue to localStorage:", e);
        return false;
    }
}

function readPendingBillsReview() {
    try {
        const raw = localStorage.getItem(PENDING_BILLS_REVIEW_STORAGE_KEY);
        const parsed = JSON.parse(raw || "[]");
        return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
        console.warn("Could not read pending bills review list from localStorage:", e);
        return [];
    }
}

function writePendingBillsReview(entries) {
    try {
        localStorage.setItem(PENDING_BILLS_REVIEW_STORAGE_KEY, JSON.stringify(Array.isArray(entries) ? entries : []));
        return true;
    } catch (e) {
        console.warn("Could not write pending bills review list to localStorage:", e);
        return false;
    }
}

function _entryMatchesTarget(candidate, target) {
    if (!candidate || !target) return false;
    if (candidate.queue_id && target.queue_id) {
        return candidate.queue_id === target.queue_id;
    }
    const candKey = candidate.idempotency_key || candidate.payload?.idempotency_key;
    const targKey = target.idempotency_key || target.payload?.idempotency_key;
    if (candKey && targKey) {
        return candKey === targKey;
    }
    return false;
}

function _removePendingBillEntrySafe(targetEntry) {
    // Race-safe: always re-read latest queue immediately before removing ONLY this entry.
    const latest = readPendingBillsQueue();
    const filtered = latest.filter(item => !_entryMatchesTarget(item, targetEntry));
    return writePendingBillsQueue(filtered);
}

function _movePendingBillToReviewSafe(targetEntry, httpStatus, errorCode, errorDetail) {
    const reviewList = readPendingBillsReview();
    const reviewItem = {
        ...targetEntry,
        review_id: targetEntry.review_id || targetEntry.queue_id || ("rev-" + generateIdempotencyKey()),
        moved_to_review_at: new Date().toISOString(),
        http_status: httpStatus,
        error_code: errorCode || null,
        error_detail: errorDetail || `HTTP ${httpStatus}`
    };

    const existingIdx = reviewList.findIndex(r => _entryMatchesTarget(r, reviewItem));
    if (existingIdx >= 0) {
        reviewList[existingIdx] = reviewItem;
    } else {
        reviewList.push(reviewItem);
    }

    // Write review list FIRST; never remove from pending unless review persistence succeeded.
    const savedReview = writePendingBillsReview(reviewList);
    if (savedReview) {
        _removePendingBillEntrySafe(targetEntry);
    }
    return savedReview;
}

function queuePendingOfflineBill(salePayload, meta = {}) {
    try {
        const pending = readPendingBillsQueue();
        const key = (salePayload && salePayload.idempotency_key) || generateIdempotencyKey();
        const payloadCopy = {
            ...(salePayload || {}),
            idempotency_key: key
        };
        const nowIso = new Date().toISOString();
        const entry = {
            queue_id: "q-" + generateIdempotencyKey(),
            idempotency_key: key,
            queued_at: nowIso,
            created_at: nowIso,
            customer_name: meta.customer_name || salePayload?.customer_name || "",
            customer_mobile: meta.customer_mobile || salePayload?.customer_mobile || null,
            customer_village: meta.customer_village || salePayload?.customer_village || null,
            payload: payloadCopy
        };
        pending.push(entry);
        writePendingBillsQueue(pending);
        updatePosOfflinePendingBanner();
        return entry;
    } catch (e) {
        console.warn("Could not queue pending bill to localStorage:", e);
        return null;
    }
}

function updatePosOfflinePendingBanner() {
    const banner = document.getElementById("pos-offline-pending-banner");
    const textEl = document.getElementById("pos-offline-pending-text");
    const pending = readPendingBillsQueue();
    if (!banner) return;
    if (pending.length > 0) {
        if (textEl) {
            textEl.textContent = `${pending.length} bill(s) queued offline: Pending - invoice number assigned when saved.`;
        }
        banner.style.display = "block";
    } else {
        banner.style.display = "none";
    }
}

function updatePendingBillsReviewIndicator() {
    const reviewItems = readPendingBillsReview();
    const count = reviewItems.length;

    const headerBtn = document.getElementById("btn-header-pending-review");
    const headerCount = document.getElementById("pending-review-header-count");
    if (headerCount) headerCount.textContent = String(count);
    if (headerBtn) headerBtn.style.display = count > 0 ? "inline-flex" : "none";

    const topBanner = document.getElementById("pending-bills-review-banner");
    const bannerCount = document.getElementById("pending-review-banner-count");
    const bannerText = document.getElementById("pending-bills-review-banner-text");
    if (bannerCount) bannerCount.textContent = String(count);
    if (bannerText) {
        bannerText.textContent = count === 1
            ? "1 offline bill could not be auto-synced and requires review."
            : `${count} offline bills could not be auto-synced and require review.`;
    }
    if (topBanner) topBanner.style.display = count > 0 ? "flex" : "none";
}

function _ensureLegacyPendingEntriesHavePersistedKeys() {
    const current = readPendingBillsQueue();
    if (current.length === 0) return [];

    let mutated = false;
    const normalized = current.map(item => {
        if (!item || typeof item !== "object") return item;
        const payload = (item.payload && typeof item.payload === "object") ? { ...item.payload } : {};
        let key = item.idempotency_key || payload.idempotency_key;
        let queueId = item.queue_id;
        let queuedAt = item.queued_at || item.created_at;

        if (!key) {
            // Legacy entry queued before Phase 2B-5C: assign a fresh key and KEEP its explicit invoice_no untouched.
            key = generateIdempotencyKey();
            mutated = true;
        }
        if (payload.idempotency_key !== key) {
            payload.idempotency_key = key;
            mutated = true;
        }
        if (!queueId) {
            queueId = "q-" + generateIdempotencyKey();
            mutated = true;
        }
        if (!queuedAt) {
            queuedAt = new Date().toISOString();
            mutated = true;
        }

        return {
            ...item,
            queue_id: queueId,
            idempotency_key: key,
            queued_at: queuedAt,
            created_at: item.created_at || queuedAt,
            payload
        };
    });

    if (mutated) {
        // Persist assigned keys BEFORE the first network retry attempt.
        writePendingBillsQueue(normalized);
    }
    return normalized;
}

async function syncPendingOfflineBills() {
    if (!hasValidAuthSession()) return;
    if (isSyncingPendingBills) return;

    isSyncingPendingBills = true;
    try {
        const snapshot = _ensureLegacyPendingEntriesHavePersistedKeys();
        if (!snapshot || snapshot.length === 0) {
            updatePosOfflinePendingBanner();
            updatePendingBillsReviewIndicator();
            return;
        }

        const syncedInvoices = [];
        let movedToReviewCount = 0;

        for (const entry of snapshot) {
            if (!entry || !entry.payload) continue;
            try {
                const res = await fetch("/api/sales/create", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(entry.payload)
                });

                if (res.ok) {
                    const result = await res.json().catch(() => ({}));
                    const confirmedInv = String(result.invoice_no || entry.payload.invoice_no || ("#" + (result.sale_id || ""))).trim();
                    _removePendingBillEntrySafe(entry);
                    saveBillToLocalBackup(
                        { ...entry.payload, invoice_no: confirmedInv },
                        result.sale_id
                    );
                    syncedInvoices.push(confirmedInv);
                } else if (res.status === 401) {
                    // Session expired: keep entry queued and stop until re-authenticated.
                    break;
                } else if (isTransientHttpStatus(res.status)) {
                    // Transient error (5xx / 408 / 429): keep entry queued for later retry.
                    continue;
                } else if (res.status >= 400 && res.status < 500) {
                    // Non-retryable 4xx (409 / 422 / 400 / etc.): move to review list, do NOT delete or retry forever.
                    const errData = await res.json().catch(() => ({}));
                    const detail = extractApiErrorMessage(errData, `HTTP ${res.status}`);
                    const errCode = errData.error_code || null;
                    if (_movePendingBillToReviewSafe(entry, res.status, errCode, detail)) {
                        movedToReviewCount += 1;
                    }
                }
            } catch (err) {
                // Network error / timeout: keep entry queued for later retry.
                console.warn("Pending bill sync transient network error:", err);
            }
        }

        updatePosOfflinePendingBanner();
        updatePendingBillsReviewIndicator();

        if (syncedInvoices.length > 0) {
            const invSummary = syncedInvoices.join(", ");
            showToast(
                `Synced ${syncedInvoices.length} pending offline bill(s): ${invSummary}.`,
                "success",
                6500
            );
            await refreshProductList();
            await refreshCustomerList();
            if (typeof fetchNextInvoiceNo === "function") await fetchNextInvoiceNo();
            await loadInventorySilently();
            if (typeof notifyOtherTabsDataChanged === "function") notifyOtherTabsDataChanged();
        }
        if (movedToReviewCount > 0) {
            showToast(
                `${movedToReviewCount} offline bill(s) could not be auto-synced and moved to Review Offline Bills.`,
                "warning",
                7500
            );
        }
    } catch (e) {
        console.error("Error syncing pending bills:", e);
    } finally {
        isSyncingPendingBills = false;
    }
}

function openPendingBillsReviewModal() {
    renderPendingBillsReviewList();
    const modal = document.getElementById("modal-pending-bills-review");
    if (modal) modal.style.display = "flex";
}

function closePendingBillsReviewModal() {
    const modal = document.getElementById("modal-pending-bills-review");
    if (modal) modal.style.display = "none";
}

function toggleReviewBillDetails(reviewId) {
    const detailEl = document.getElementById(`review-detail-${reviewId}`);
    if (!detailEl) return;
    detailEl.style.display = detailEl.style.display === "none" ? "block" : "none";
}

function renderPendingBillsReviewList() {
    const container = document.getElementById("pending-review-list");
    if (!container) return;
    const items = readPendingBillsReview();
    if (items.length === 0) {
        container.innerHTML = '<div style="padding: 18px; text-align: center; color: #64748b;">No offline bills require review.</div>';
        return;
    }

    container.innerHTML = items.map((item, idx) => {
        const rid = escapeHtml(item.review_id || item.queue_id || String(idx));
        const rawRid = item.review_id || item.queue_id || String(idx);
        const payload = item.payload || {};
        const custLabel = escapeHtml(item.customer_name || `Customer #${payload.customer_id || "-"}`);
        const billDate = escapeHtml(payload.sale_date || (item.queued_at ? item.queued_at.split("T")[0] : "-"));
        const total = Number(payload.net_amount || 0).toFixed(2);
        const reason = escapeHtml(item.error_detail || `HTTP ${item.http_status || "Error"}`);
        const codeTag = item.error_code
            ? `<span style="display:inline-block; background:#fee2e2; color:#991b1b; padding:1px 6px; border-radius:4px; font-size:11px; font-weight:700; margin-right:6px;">${escapeHtml(item.error_code)}</span>`
            : "";
        const lineItemsHtml = (Array.isArray(payload.items) ? payload.items : []).map(it => {
            const pname = escapeHtml(it.product_name || `Product #${it.product_id}`);
            return `<li>${pname} - Qty: ${escapeHtml(String(it.qty || 0))}, Total: Rs. ${Number(it.total_amount || 0).toFixed(2)}</li>`;
        }).join("");

        return `
            <div style="border: 1px solid #fecaca; border-radius: 8px; padding: 12px; margin-bottom: 10px; background: #fff;">
                <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; flex-wrap: wrap;">
                    <div>
                        <div style="font-weight: 700; font-size: 14px; color: #0f172a;">${custLabel}</div>
                        <div style="font-size: 12px; color: #475569; margin-top: 2px;">
                            Date: <strong>${billDate}</strong> &bull; Total: <strong>Rs. ${total}</strong> &bull; Mode: <strong>${escapeHtml(payload.payment_mode || "CASH")}</strong>
                        </div>
                        <div style="font-size: 12px; color: #991b1b; margin-top: 6px;">
                            ${codeTag}${reason}
                        </div>
                    </div>
                    <div style="display: flex; gap: 6px; flex-wrap: wrap;">
                        <button type="button" class="btn btn-secondary btn-sm" onclick="toggleReviewBillDetails('${rid}')">View Details</button>
                        <button type="button" class="btn btn-primary btn-sm" onclick="resubmitReviewBillAsNew('${rid}')">Save as New Bill</button>
                        <button type="button" class="btn btn-danger btn-sm" onclick="markReviewBillCheckedAndRemove('${rid}')">Mark as Checked and Remove</button>
                    </div>
                </div>
                <div id="review-detail-${rid}" style="display: none; margin-top: 10px; padding-top: 10px; border-top: 1px dashed #cbd5e1; font-size: 12px; color: #334155;">
                    <div><strong>Queued At:</strong> ${escapeHtml(item.queued_at || "-")}</div>
                    <div><strong>Moved to Review:</strong> ${escapeHtml(item.moved_to_review_at || "-")}</div>
                    <div><strong>Submitted Invoice No:</strong> ${escapeHtml(payload.invoice_no || "(blank - server assigned)")}</div>
                    <div><strong>Idempotency Key:</strong> <code>${escapeHtml(item.idempotency_key || payload.idempotency_key || "-")}</code></div>
                    <div style="margin-top: 6px;"><strong>Items:</strong></div>
                    <ul style="margin: 4px 0 0 18px; padding: 0;">${lineItemsHtml || "<li>No line items recorded</li>"}</ul>
                </div>
            </div>
        `;
    }).join("");
}

async function markReviewBillCheckedAndRemove(reviewId) {
    const items = readPendingBillsReview();
    const target = items.find(r => (r.review_id || r.queue_id) === reviewId);
    if (!target) return false;

    const confirmed = await showConfirmDialog({
        title: "Mark Bill as Checked and Remove?",
        message: "Before removing this offline bill from the review list, please check the Sales list to confirm whether the bill was already saved.\n\nOnce removed, this queued copy cannot be recovered.",
        confirmText: "Mark as Checked and Remove",
        cancelText: "Cancel",
        danger: true,
    });
    if (!confirmed) return false;

    const latest = readPendingBillsReview();
    const filtered = latest.filter(r => (r.review_id || r.queue_id) !== reviewId);
    writePendingBillsReview(filtered);
    updatePendingBillsReviewIndicator();
    renderPendingBillsReviewList();
    showToast("Offline bill removed from review list.", "info");
    return true;
}

let isResubmittingReviewBill = false;

async function resubmitReviewBillAsNew(reviewId) {
    if (isResubmittingReviewBill) return false;
    const items = readPendingBillsReview();
    const target = items.find(r => (r.review_id || r.queue_id) === reviewId);
    if (!target || !target.payload) return false;

    const confirmed = await showConfirmDialog({
        title: "Save as New Bill?",
        message: "This will submit the bill with a brand-new idempotency key and a blank invoice number so the server assigns a new invoice number.\n\nPlease check the Sales list first to ensure this bill was not already saved.",
        confirmText: "Save as New Bill",
        cancelText: "Cancel",
        danger: true,
    });
    if (!confirmed) return false;

    isResubmittingReviewBill = true;
    const freshKey = generateIdempotencyKey();
    const newPayload = {
        ...target.payload,
        invoice_no: "",
        idempotency_key: freshKey
    };

    try {
        const res = await fetch("/api/sales/create", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(newPayload)
        });
        const result = await res.json().catch(() => ({}));
        if (res.ok && result.success) {
            const confirmedInv = String(result.invoice_no || "").trim();
            const latest = readPendingBillsReview();
            const filtered = latest.filter(r => (r.review_id || r.queue_id) !== reviewId);
            writePendingBillsReview(filtered);
            saveBillToLocalBackup({ ...newPayload, invoice_no: confirmedInv }, result.sale_id);
            updatePendingBillsReviewIndicator();
            renderPendingBillsReviewList();
            showToast(`Saved as new bill: Invoice ${confirmedInv || ("#" + result.sale_id)}.`, "success", 6500);
            await refreshProductList();
            await refreshCustomerList();
            if (typeof fetchNextInvoiceNo === "function") await fetchNextInvoiceNo();
            await loadInventorySilently();
            return true;
        } else {
            const errMsg = extractApiErrorMessage(result, `HTTP ${res.status}`);
            showToast(`Could not save as new bill: ${errMsg}`, "error", 7000);
            return false;
        }
    } catch (err) {
        showToast("Network error saving bill: " + err.message, "error", 6500);
        return false;
    } finally {
        isResubmittingReviewBill = false;
    }
}

// Auto-sync whenever internet connects or on initial load
window.addEventListener("online", syncPendingOfflineBills);
setTimeout(() => {
    updatePendingBillsReviewIndicator();
    updatePosOfflinePendingBanner();
    syncPendingOfflineBills();
}, 2000);

/* ==========================================
   DESKTOP DATABASE BACKUP & RECOVERY MANAGER
========================================== */
let _pendingBackupRestore = null;
let _backupRestoreInProgress = false;

function _setBackupManagerFeedback(message, tone = "info") {
    const banner = document.getElementById("bkp-mgr-feedback");
    if (!banner) return;
    if (!message) {
        banner.style.display = "none";
        banner.textContent = "";
        banner.className = "bkp-feedback-banner";
        return;
    }
    const toneClass = ["success", "warning", "danger"].includes(tone) ? `tone-${tone}` : "";
    banner.className = `bkp-feedback-banner ${toneClass}`.trim();
    banner.textContent = message;
    banner.style.display = "block";
}

function resetBackupManagerUiState() {
    _pendingBackupRestore = null;
    _backupRestoreInProgress = false;
    const modal = document.getElementById("modal-backup-restore-confirm");
    if (modal) modal.style.display = "none";
    const check = document.getElementById("bkp-restore-confirm-check");
    if (check) check.checked = false;
    const confirmBtn = document.getElementById("btn-confirm-backup-restore");
    if (confirmBtn) confirmBtn.disabled = true;
    const fileInput = document.getElementById("bkp-external-file-input");
    if (fileInput) fileInput.value = "";
    const tbody = document.getElementById("bkp-mgr-tbody");
    if (tbody) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">Open Shop Profile &amp; Settings as Admin to view local backups.</td></tr>';
    }
    const modeBadge = document.getElementById("bkp-runtime-mode");
    if (modeBadge) modeBadge.textContent = "Checking...";
    const dirEl = document.getElementById("bkp-runtime-dir-path");
    if (dirEl) dirEl.textContent = "-";
    const summaryEl = document.getElementById("bkp-runtime-summary");
    if (summaryEl) summaryEl.textContent = "-";
    const schedBadge = document.getElementById("bkp-sched-status-badge");
    if (schedBadge) {
        schedBadge.textContent = "Checking...";
        schedBadge.className = "badge badge-info";
    }
    const schedLastAuto = document.getElementById("bkp-sched-last-auto");
    if (schedLastAuto) schedLastAuto.textContent = "No automatic backup recorded yet.";
    const schedNext = document.getElementById("bkp-sched-next-check");
    if (schedNext) schedNext.textContent = "-";
    const lowDiskEl = document.getElementById("bkp-sched-low-disk-warning");
    if (lowDiskEl) {
        lowDiskEl.style.display = "none";
        lowDiskEl.textContent = "";
    }
    const lastErrEl = document.getElementById("bkp-sched-last-error");
    if (lastErrEl) {
        lastErrEl.style.display = "none";
        lastErrEl.textContent = "";
    }
    _setBackupManagerFeedback("", "info");
    if (typeof clearSensitiveRecoveryKeyUiState === "function") {
        clearSensitiveRecoveryKeyUiState();
    }
}

function _renderBackupSchedulerStatus(scheduler) {
    const badgeEl = document.getElementById("bkp-sched-status-badge");
    const lastAutoEl = document.getElementById("bkp-sched-last-auto");
    const nextEl = document.getElementById("bkp-sched-next-check");
    const lowDiskEl = document.getElementById("bkp-sched-low-disk-warning");
    const errEl = document.getElementById("bkp-sched-last-error");

    if (!scheduler || typeof scheduler !== "object") {
        if (badgeEl) {
            badgeEl.textContent = "Stopped";
            badgeEl.className = "badge badge-warning";
        }
        return;
    }

    if (badgeEl) {
        if (scheduler.backup_in_progress) {
            badgeEl.textContent = "Running • Backup in Progress";
            badgeEl.className = "badge badge-info";
        } else if (scheduler.running) {
            badgeEl.textContent = "Running";
            badgeEl.className = "badge badge-success";
        } else {
            badgeEl.textContent = "Stopped";
            badgeEl.className = "badge badge-warning";
        }
    }

    if (lastAutoEl) {
        const lastAuto = scheduler.last_auto_backup;
        if (lastAuto && lastAuto.filename) {
            const btype = String(lastAuto.backup_type || "AUTO").toUpperCase();
            const sizeTxt = lastAuto.size_display || "-";
            const whenTxt = lastAuto.created_at || "-";
            lastAutoEl.textContent = `${whenTxt} • ${btype} • ${sizeTxt}`;
        } else {
            lastAutoEl.textContent = "No automatic backup recorded yet.";
        }
    }

    if (nextEl) {
        if (!scheduler.running) {
            nextEl.textContent = "Scheduler stopped";
        } else {
            const nextChk = scheduler.next_check_at || "-";
            const estNext = scheduler.estimated_next_backup_at;
            nextEl.textContent = estNext
                ? `Next check: ${nextChk} | Est. backup: ${estNext}`
                : `Next check: ${nextChk}`;
        }
    }

    if (lowDiskEl) {
        if (scheduler.low_disk_warning) {
            lowDiskEl.textContent = `Low Disk Space Warning: ${scheduler.low_disk_warning}`;
            lowDiskEl.style.display = "block";
        } else {
            lowDiskEl.textContent = "";
            lowDiskEl.style.display = "none";
        }
    }

    if (errEl) {
        if (scheduler.last_error && scheduler.last_error !== scheduler.low_disk_warning) {
            const errTime = scheduler.last_error_at ? ` (${scheduler.last_error_at})` : "";
            errEl.textContent = `Latest Automatic Backup Error${errTime}: ${scheduler.last_error}`;
            errEl.style.display = "block";
        } else {
            errEl.textContent = "";
            errEl.style.display = "none";
        }
    }
}

function openBackupManagerView() {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Backup Manager is restricted to administrators.", "error");
        return false;
    }

    const targetId = "tab-settings";
    document.querySelectorAll(".nav-tab, .mobile-drawer-item, .mobile-bottom-nav-item").forEach((t) => {
        if (t.getAttribute("data-tab") === targetId) {
            t.classList.add("active");
        } else {
            t.classList.remove("active");
        }
    });
    document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
    const targetPane = document.getElementById(targetId);
    if (targetPane) targetPane.classList.add("active");
    if (typeof closeMobileMenu === "function") closeMobileMenu();

    settingsDirtyFields.clear();
    loadSettings(false);
    loadBackupManagerData();

    const card = document.getElementById("settings-backup-manager-card");
    if (card && typeof card.scrollIntoView === "function") {
        setTimeout(() => {
            try { card.scrollIntoView({ behavior: "smooth", block: "start" }); } catch (e) {}
        }, 60);
    }
    return true;
}

function _bindBackupManagerTableEvents() {
    const tbody = document.getElementById("bkp-mgr-tbody");
    if (!tbody || tbody.dataset.bkpDelegated === "1") return;
    tbody.dataset.bkpDelegated = "1";
    tbody.addEventListener("click", (ev) => {
        const btn = ev.target && typeof ev.target.closest === "function"
            ? ev.target.closest("button[data-bkp-action]")
            : null;
        if (!btn) return;
        const action = btn.getAttribute("data-bkp-action");
        const filename = btn.getAttribute("data-filename") || "";
        if (!filename) return;
        if (action === "inspect") {
            verifySingleBackup(filename, btn);
        } else if (action === "download") {
            downloadBackupFile(filename, btn);
        } else if (action === "restore") {
            openBackupRestoreModal(filename, btn);
        }
    });
}

function _renderBackupRows(backups) {
    const tbody = document.getElementById("bkp-mgr-tbody");
    if (!tbody) return;
    _bindBackupManagerTableEvents();

    if (!Array.isArray(backups) || backups.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted" style="padding: 18px;">No backup files found yet. Click "Create Backup Now" to create your first verified snapshot.</td></tr>';
        return;
    }

    tbody.innerHTML = backups.map((b) => {
        const fname = String(b.filename || "");
        const btype = String(b.backup_type || "EXTERNAL").toUpperCase();
        const typeClass = `bkp-type-${btype.toLowerCase().replace(/[^a-z_]/g, "")}`;
        const isValid = Boolean(b.integrity_ok && b.valid_for_restore);
        const statusBadge = isValid
            ? '<span class="badge badge-success">Verified OK</span>'
            : (b.integrity_ok
                ? '<span class="badge badge-warning">Incomplete Schema</span>'
                : '<span class="badge badge-danger">Unhealthy / Corrupt</span>');
        const safeFileAttr = escapeHtml(fname);
        return `
            <tr>
                <td>
                    <code style="font-weight: 700; color: #0f172a;">${safeFileAttr}</code>
                </td>
                <td><span class="bkp-type-badge ${escapeHtml(typeClass)}">${escapeHtml(btype)}</span></td>
                <td>${escapeHtml(b.modified_at || b.created_at || "-")}</td>
                <td class="text-right font-bold">${escapeHtml(b.size_display || "-")}</td>
                <td class="text-center">${statusBadge}</td>
                <td class="text-center" style="white-space: nowrap;">
                    <button type="button" class="btn btn-secondary btn-sm" data-bkp-action="inspect" data-filename="${safeFileAttr}" title="Run SQLite integrity &amp; schema check">🔍 Verify</button>
                    <button type="button" class="btn btn-outline-primary btn-sm" data-bkp-action="download" data-filename="${safeFileAttr}" title="Export / copy .db backup file" ${b.integrity_ok ? "" : "disabled"}>⬇️ Export</button>
                    <button type="button" class="btn btn-outline-danger btn-sm" data-bkp-action="restore" data-filename="${safeFileAttr}" title="Inspect and restore database from this backup" ${isValid ? "" : "disabled"}>⏪ Restore</button>
                </td>
            </tr>
        `;
    }).join("");
}

async function loadBackupManagerData(buttonEl = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") return null;
    const sessionVer = authSessionVersion;
    const tbody = document.getElementById("bkp-mgr-tbody");
    const modeBadge = document.getElementById("bkp-runtime-mode");
    const dirEl = document.getElementById("bkp-runtime-dir-path");
    const summaryEl = document.getElementById("bkp-runtime-summary");
    const prevBtnText = buttonEl ? buttonEl.textContent : "";

    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Loading...";
    }
    if (tbody) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted" style="padding: 16px;">⏳ Loading verified local backups...</td></tr>';
    }

    try {
        const [runtimeRes, backupsRes] = await Promise.all([
            fetch("/api/system/runtime-info"),
            fetch("/api/system/backups"),
        ]);
        if (sessionVer !== authSessionVersion || !hasValidAuthSession()) return null;

        if (runtimeRes.ok) {
            const rt = await runtimeRes.json();
            if (modeBadge) {
                const modeLabel = String(rt.app_mode || "local").toUpperCase();
                const verLabel = rt.app_version ? ` v${rt.app_version}` : "";
                modeBadge.textContent = `${modeLabel}${verLabel} • Offline SQLite`;
                modeBadge.className = "badge badge-success";
            }
            if (dirEl && rt.backup_dir) {
                dirEl.textContent = rt.backup_dir;
            }
        }

        if (!backupsRes.ok) {
            const errData = await backupsRes.json().catch(() => ({}));
            const msg = "Could not load backups list: " + (errData.detail || `HTTP ${backupsRes.status}`);
            if (tbody) {
                tbody.innerHTML = `<tr><td colspan="6" class="text-center text-danger">${escapeHtml(msg)}</td></tr>`;
            }
            return null;
        }

        const data = await backupsRes.json();
        const backups = Array.isArray(data.backups) ? data.backups : [];
        if (dirEl && data.backup_dir && (dirEl.textContent === "-" || !dirEl.textContent)) {
            dirEl.textContent = data.backup_dir;
        }
        if (summaryEl) {
            const validCount = backups.filter((x) => x.integrity_ok && x.valid_for_restore).length;
            summaryEl.textContent = `${validCount} verified / ${backups.length} total`;
        }
        if (data.scheduler) {
            _renderBackupSchedulerStatus(data.scheduler);
        }
        _renderBackupRows(backups);
        if (typeof _populateEncryptedExportSourceSelect === "function") {
            _populateEncryptedExportSourceSelect(backups);
        }
        if (typeof loadRecoveryKeyStatus === "function") {
            loadRecoveryKeyStatus();
        }
        if (typeof loadOffsiteBackupStatus === "function") {
            loadOffsiteBackupStatus();
        }
        return data;
    } catch (err) {
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="6" class="text-center text-danger">Error loading backup list: ${escapeHtml(err.message)}</td></tr>`;
        }
        return null;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevBtnText;
        }
    }
}

async function createManualBackupFromManager(buttonEl = null) {
    return triggerDatabaseBackup(buttonEl);
}

async function verifySingleBackup(filename, buttonEl = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Backup verification is restricted to administrators.", "error");
        return null;
    }
    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Checking...";
    }
    try {
        const res = await fetch(`/api/system/backups/${encodeURIComponent(filename)}/inspect`);
        const info = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = "Verification failed: " + (info.detail || `HTTP ${res.status}`);
            _setBackupManagerFeedback(msg, "danger");
            showToast(msg, "error");
            return null;
        }

        const isHealthy = Boolean(info.valid_for_restore);
        const shopInfo = info.shop_name ? ` • Shop: ${info.shop_name}` : "";
        const msg = isHealthy
            ? `Verified OK: "${info.filename}" (${info.size_display}) — SQLite integrity OK, 0 FK issues, ${info.table_count} tables (Schema v${info.schema_version ?? "-"})${shopInfo}.`
            : `Backup "${info.filename}" failed safety checks — Integrity: ${info.integrity_ok ? "OK" : "FAIL"}, FK Violations: ${info.foreign_key_violation_count ?? "?"}, Required Tables: ${info.has_required_tables ? "OK" : "MISSING"}.`;
        _setBackupManagerFeedback(msg, isHealthy ? "success" : "danger");
        showToast(msg, isHealthy ? "success" : "error", 6000);
        return info;
    } catch (err) {
        const msg = "Verification error: " + err.message;
        _setBackupManagerFeedback(msg, "danger");
        showToast(msg, "error");
        return null;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

async function downloadBackupFile(filename, buttonEl = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Backup export is restricted to administrators.", "error");
        return false;
    }
    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Exporting...";
    }
    try {
        const res = await fetch(`/api/system/backups/${encodeURIComponent(filename)}/download`);
        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            const msg = "Could not export backup: " + (errData.detail || `HTTP ${res.status}`);
            _setBackupManagerFeedback(msg, "danger");
            showToast(msg, "error");
            return false;
        }
        const disp = res.headers.get("Content-Disposition") || "";
        let dlName = _parseContentDispositionFilename(disp, filename || "krushidhan_backup.db");
        if (!dlName.toLowerCase().endsWith(".db")) {
            dlName += ".db";
        }
        const blob = await res.blob();
        const delivered = await _deliverDownloadedBlob(blob, dlName, "application/vnd.sqlite3");
        if (!delivered) return false;
        _setBackupManagerFeedback(`Exported verified backup copy "${dlName}".`, "success");
        showToast(`Exported backup file: ${dlName}`, "success");
        return true;
    } catch (err) {
        const msg = "Backup export error: " + err.message;
        _setBackupManagerFeedback(msg, "danger");
        showToast(msg, "error");
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

function _populateBackupRestoreConfirmModal(sourceLabel, info) {
    const modal = document.getElementById("modal-backup-restore-confirm");
    if (!modal) return;
    const setText = (id, text) => {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    };

    setText("bkp-confirm-source", sourceLabel);
    setText("bkp-confirm-filename", info.filename || "-");
    setText("bkp-confirm-date", info.modified_at || "-");
    setText("bkp-confirm-size", info.size_display || "-");
    setText(
        "bkp-confirm-integrity",
        info.integrity_ok ? "PASS (PRAGMA integrity_check = ok)" : "FAIL"
    );
    setText(
        "bkp-confirm-fk",
        info.foreign_key_ok
            ? "PASS (0 violations)"
            : `${info.foreign_key_violation_count ?? "Unknown"} violation(s)`
    );
    setText(
        "bkp-confirm-schema",
        `Schema v${info.schema_version ?? "-"} (${info.table_count ?? 0} tables verified)`
    );
    setText("bkp-confirm-shop", info.shop_name || "-");
    const keyRowEl = document.getElementById("bkp-confirm-key-row");
    if (keyRowEl) {
        if (info.key_id) {
            keyRowEl.style.display = "";
            setText("bkp-confirm-key-id", info.key_id);
        } else {
            keyRowEl.style.display = "none";
            setText("bkp-confirm-key-id", "-");
        }
    }

    const checkEl = document.getElementById("bkp-restore-confirm-check");
    const confirmBtn = document.getElementById("btn-confirm-backup-restore");
    const cancelBtn = document.getElementById("btn-cancel-backup-restore");
    if (checkEl) checkEl.checked = false;
    if (confirmBtn) {
        confirmBtn.disabled = true;
        confirmBtn.textContent = "⏪ Restore Database";
    }
    if (cancelBtn) cancelBtn.disabled = false;
    modal.style.display = "flex";
}

function onBackupRestoreConfirmToggle() {
    const checkEl = document.getElementById("bkp-restore-confirm-check");
    const confirmBtn = document.getElementById("btn-confirm-backup-restore");
    if (confirmBtn) {
        confirmBtn.disabled = !(checkEl && checkEl.checked && _pendingBackupRestore && !_backupRestoreInProgress);
    }
}

function closeBackupRestoreModal() {
    if (_backupRestoreInProgress) return;
    _pendingBackupRestore = null;
    const modal = document.getElementById("modal-backup-restore-confirm");
    if (modal) modal.style.display = "none";
    const checkEl = document.getElementById("bkp-restore-confirm-check");
    if (checkEl) checkEl.checked = false;
    const confirmBtn = document.getElementById("btn-confirm-backup-restore");
    if (confirmBtn) confirmBtn.disabled = true;
}

async function openBackupRestoreModal(filename, buttonEl = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Database restore is restricted to administrators.", "error");
        return false;
    }
    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Verifying...";
    }
    try {
        const res = await fetch(`/api/system/backups/${encodeURIComponent(filename)}/inspect`);
        const info = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = "Cannot inspect backup: " + (info.detail || `HTTP ${res.status}`);
            _setBackupManagerFeedback(msg, "danger");
            showToast(msg, "error");
            return false;
        }
        if (!info.valid_for_restore) {
            const msg = `Restore blocked: "${info.filename || filename}" failed SQLite integrity or schema checks.`;
            _setBackupManagerFeedback(msg, "danger");
            showToast(msg, "error", 6000);
            return false;
        }
        _pendingBackupRestore = {
            sourceType: "local",
            filename: info.filename || filename,
            file: null,
            info,
        };
        _populateBackupRestoreConfirmModal("Local Backup Directory", info);
        return true;
    } catch (err) {
        showToast("Error preparing restore: " + err.message, "error");
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

async function inspectExternalBackupFile(buttonEl = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: External backup restore is restricted to administrators.", "error");
        return false;
    }
    const fileInput = document.getElementById("bkp-external-file-input");
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (!file) {
        showToast("Please select a .db backup file first.", "warning");
        return false;
    }
    if (!String(file.name || "").toLowerCase().endsWith(".db")) {
        const msg = `Unsupported file "${file.name}". Only SQLite .db backup files are accepted.`;
        _setBackupManagerFeedback(msg, "danger");
        showToast(msg, "error");
        return false;
    }

    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Validating...";
    }
    try {
        const formData = new FormData();
        formData.append("file", file);
        const res = await fetch("/api/system/backups/inspect-upload", {
            method: "POST",
            body: formData,
        });
        const info = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = "External backup validation failed: " + (info.detail || `HTTP ${res.status}`);
            _setBackupManagerFeedback(msg, "danger");
            showToast(msg, "error", 6000);
            return false;
        }
        if (!info.valid_for_restore) {
            const msg = `Restore blocked: uploaded file "${info.filename || file.name}" failed SQLite integrity or schema checks.`;
            _setBackupManagerFeedback(msg, "danger");
            showToast(msg, "error", 6000);
            return false;
        }

        _pendingBackupRestore = {
            sourceType: "upload",
            filename: info.filename || file.name,
            file,
            info,
        };
        _populateBackupRestoreConfirmModal("External / USB Backup File (.db)", info);
        return true;
    } catch (err) {
        const msg = "Error validating external backup: " + err.message;
        _setBackupManagerFeedback(msg, "danger");
        showToast(msg, "error");
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

async function executeConfirmedBackupRestore(buttonEl = null) {
    if (_backupRestoreInProgress || !_pendingBackupRestore) return false;
    const checkEl = document.getElementById("bkp-restore-confirm-check");
    if (!checkEl || !checkEl.checked) {
        showToast("Please tick the confirmation checkbox before restoring the database.", "warning");
        return false;
    }

    const state = _pendingBackupRestore;
    const cancelBtn = document.getElementById("btn-cancel-backup-restore");
    const confirmBtn = buttonEl || document.getElementById("btn-confirm-backup-restore");
    const prevText = confirmBtn ? confirmBtn.textContent : "⏪ Restore Database";

    _backupRestoreInProgress = true;
    if (confirmBtn) {
        confirmBtn.disabled = true;
        confirmBtn.textContent = "⏳ Restoring Database...";
    }
    if (cancelBtn) cancelBtn.disabled = true;

    try {
        let res;
        if (state.sourceType === "offsite_folder" && state.filename) {
            const payload = {
                filename: state.filename,
                folder_path: state.folderPath || null,
                confirm_restore: true,
            };
            if (state.manualKey && String(state.manualKey).trim()) {
                payload.recovery_key = String(state.manualKey).trim();
            }
            res = await fetch("/api/system/offsite-backup/restore-folder-archive", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
        } else if (state.sourceType === "encrypted_upload" && state.file) {
            const formData = new FormData();
            formData.append("file", state.file);
            formData.append("confirm_restore", "true");
            if (state.manualKey && String(state.manualKey).trim()) {
                formData.append("recovery_key", String(state.manualKey).trim());
            }
            res = await fetch("/api/system/backups/encrypted/restore-upload", {
                method: "POST",
                body: formData,
            });
        } else if (state.sourceType === "upload" && state.file) {
            const formData = new FormData();
            formData.append("file", state.file);
            formData.append("confirm_restore", "true");
            res = await fetch("/api/system/backup/restore-upload", {
                method: "POST",
                body: formData,
            });
        } else {
            res = await fetch("/api/system/backup/restore", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    backup_filename: state.filename,
                    confirm_restore: true,
                }),
            });
        }

        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.success) {
            const msg = "Restore failed: " + (data.detail || `HTTP ${res.status}`);
            _setBackupManagerFeedback(msg, "danger");
            showToast(msg, "error", 6500);
            return false;
        }

        _backupRestoreInProgress = false;
        closeBackupRestoreModal();
        const extInput = document.getElementById("bkp-external-file-input");
        if (extInput) extInput.value = "";
        if (state.sourceType === "encrypted_upload" && typeof clearEncryptedInspectResult === "function") {
            clearEncryptedInspectResult(true);
        }
        if (state.sourceType === "offsite_folder" && typeof clearOffsiteRecoveryState === "function") {
            clearOffsiteRecoveryState();
        }

        // Reset local view state so no stale pre-restore data lingers
        currentCart = [];
        allProducts = [];
        allCustomers = [];
        allSuppliers = [];
        farmerStatusList = [];
        allSuppliersKhata = [];
        rawInventoryItems = [];
        pnlAllProducts = [];
        settingsDirtyFields.clear();
        try {
            if (typeof renderPosCart === "function") renderPosCart();
        } catch (uiErr) {
            console.warn("Non-fatal cart reset UI refresh warning after restore:", uiErr);
        }

        const safetyNote = data.pre_restore_filename
            ? ` Safety snapshot saved: ${data.pre_restore_filename}.`
            : "";
        const okMsg = `Database restored from "${data.restored_from || state.filename}".${safetyNote}`;
        _setBackupManagerFeedback(okMsg, "success");
        showToast(okMsg, "success", 6500);

        // Re-verify active session against the restored database
        try {
            const stillAuthed = await checkAuthState();
            if (stillAuthed) {
                _initialBootstrapCompleted = true;
                await bootstrapDashboard();
                notifyOtherTabsDataChanged();
            } else {
                _initialBootstrapCompleted = false;
                stopAutoSyncPolling();
                showLoginOverlay("Database restored successfully. Please sign in to continue.");
            }
        } catch (refreshErr) {
            console.warn("Post-restore UI refresh warning:", refreshErr);
        }
        return true;
    } catch (err) {
        const msg = "Restore error: " + err.message;
        _setBackupManagerFeedback(msg, "danger");
        showToast(msg, "error", 6500);
        return false;
    } finally {
        _backupRestoreInProgress = false;
        if (cancelBtn) cancelBtn.disabled = false;
        if (confirmBtn) {
            confirmBtn.textContent = prevText;
            confirmBtn.disabled = !(_pendingBackupRestore && checkEl && checkEl.checked);
        }
    }
}

window.openBackupManagerView = openBackupManagerView;
window.loadBackupManagerData = loadBackupManagerData;
window.createManualBackupFromManager = createManualBackupFromManager;
window.verifySingleBackup = verifySingleBackup;
window.downloadBackupFile = downloadBackupFile;
window.openBackupRestoreModal = openBackupRestoreModal;
window.onBackupRestoreConfirmToggle = onBackupRestoreConfirmToggle;
window.closeBackupRestoreModal = closeBackupRestoreModal;
window.inspectExternalBackupFile = inspectExternalBackupFile;
window.executeConfirmedBackupRestore = executeConfirmedBackupRestore;

if (typeof document !== "undefined") {
    document.addEventListener("click", (ev) => {
        const bkpModal = document.getElementById("modal-backup-restore-confirm");
        if (bkpModal && ev.target === bkpModal && !_backupRestoreInProgress) {
            closeBackupRestoreModal();
        }
        const rkImportModal = document.getElementById("modal-recovery-key-import");
        if (rkImportModal && ev.target === rkImportModal) {
            closeImportRecoveryKeyModal();
        }
    });
}

// =========================================================================
// Phase 2C-4C: OS-Protected Recovery Keys, Recovery Card & Encrypted Backups
// Security invariant: NEVER store any Recovery Key in localStorage,
// sessionStorage, cookies, or logs. Clear sensitive DOM/memory on close & logout.
// =========================================================================
let _rkStoreStatusCache = null;
let _rkSetupState = null; // Ephemeral in-memory only: { operation_id, mode, key_id, created_at_utc, expires_at_ms }
let _rkExpiryTimerId = null;
let _encInspectedState = null; // Ephemeral in-memory only: { file, info, manualKey }

function _setEncryptedBackupFeedback(message, tone = "info") {
    const el = document.getElementById("enc-bkp-feedback");
    if (!el) return;
    if (!message) {
        el.style.display = "none";
        el.textContent = "";
        el.className = "bkp-feedback-banner";
        return;
    }
    el.textContent = message;
    el.className = `bkp-feedback-banner tone-${tone}`;
    el.style.display = "block";
}

function _formatKeyStoreBackendLabel(backend) {
    const map = {
        windows_dpapi: "Windows DPAPI (Current User)",
        macos_keychain: "macOS Keychain",
        in_memory_test_only: "Test Key Store",
        unsupported: "Unsupported Platform",
    };
    return map[String(backend || "").toLowerCase()] || String(backend || "-");
}

function _wipeRecoveryCardPrintDom() {
    const ids = ["rk-card-key-value", "rk-card-key-id", "rk-card-key-id-repeat", "rk-card-created-at"];
    ids.forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.textContent = "-";
    });
    if (typeof document !== "undefined" && document.body) {
        document.body.classList.remove("print-target-recovery-card");
    }
}

function _clearRecoveryKeySetupModalDom() {
    if (_rkExpiryTimerId) {
        clearTimeout(_rkExpiryTimerId);
        _rkExpiryTimerId = null;
    }
    _rkSetupState = null;
    const modal = document.getElementById("modal-recovery-key-setup");
    if (modal) modal.style.display = "none";
    const keyDisp = document.getElementById("rk-generated-key-display");
    if (keyDisp) keyDisp.textContent = "-";
    const keyIdEl = document.getElementById("rk-generated-key-id");
    if (keyIdEl) keyIdEl.textContent = "-";
    const createdEl = document.getElementById("rk-generated-created-at");
    if (createdEl) createdEl.textContent = "-";
    const chk = document.getElementById("rk-confirm-saved-check");
    if (chk) chk.checked = false;
    const reenter = document.getElementById("rk-confirm-reenter-input");
    if (reenter) reenter.value = "";
    const errEl = document.getElementById("rk-setup-error");
    if (errEl) {
        errEl.style.display = "none";
        errEl.textContent = "";
    }
    const confirmBtn = document.getElementById("btn-rk-confirm-activate");
    if (confirmBtn) confirmBtn.disabled = true;
    _wipeRecoveryCardPrintDom();
}

function clearEncryptedInspectResult(clearFileInput = false) {
    _encInspectedState = null;
    const panel = document.getElementById("enc-inspect-result-panel");
    if (panel) panel.style.display = "none";
    const restoreBtn = document.getElementById("btn-enc-open-restore-confirm");
    if (restoreBtn) restoreBtn.disabled = true;
    const manualKeyInput = document.getElementById("enc-bkp-manual-key");
    if (manualKeyInput) {
        manualKeyInput.value = "";
        manualKeyInput.type = "password";
    }
    if (clearFileInput) {
        const fileInput = document.getElementById("enc-bkp-file-input");
        if (fileInput) fileInput.value = "";
    }
}

function clearSensitiveRecoveryKeyUiState() {
    _rkStoreStatusCache = null;
    _clearRecoveryKeySetupModalDom();
    closeImportRecoveryKeyModal();
    clearEncryptedInspectResult(true);
    if (typeof clearOffsiteRecoveryState === "function") {
        clearOffsiteRecoveryState();
    }
    _setEncryptedBackupFeedback("", "info");
    const warnBanner = document.getElementById("rk-store-warning-banner");
    if (warnBanner) {
        warnBanner.style.display = "none";
        warnBanner.textContent = "";
    }
    const statusBadge = document.getElementById("rk-store-status-badge");
    if (statusBadge) {
        statusBadge.textContent = "Checking...";
        statusBadge.className = "badge badge-info";
    }
    const backendEl = document.getElementById("rk-store-backend");
    if (backendEl) backendEl.textContent = "-";
    const activeEl = document.getElementById("rk-active-key-id");
    if (activeEl) activeEl.textContent = "Not Configured";
    const countEl = document.getElementById("rk-key-count");
    if (countEl) countEl.textContent = "0";
    const tbody = document.getElementById("rk-history-tbody");
    if (tbody) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No Recovery Keys stored yet. Click "Create Recovery Key" or "Import Recovery Key".</td></tr>';
    }
}

function _populateEncryptedExportSourceSelect(backups) {
    const sel = document.getElementById("enc-export-source-select");
    if (!sel) return;
    const prevVal = sel.value;
    const verified = (Array.isArray(backups) ? backups : []).filter(
        (b) => b && b.filename && b.integrity_ok && b.valid_for_restore
    );
    if (verified.length === 0) {
        sel.innerHTML = '<option value="">No verified local .db backups available yet</option>';
        return;
    }
    sel.innerHTML = verified
        .map((b) => {
            const fname = escapeHtml(String(b.filename));
            const label = escapeHtml(`${b.filename} (${b.backup_type || "DB"} • ${b.size_display || "-"})`);
            return `<option value="${fname}">${label}</option>`;
        })
        .join("");
    if (prevVal && verified.some((b) => b.filename === prevVal)) {
        sel.value = prevVal;
    }
}

function _renderRecoveryKeyStatus(status) {
    _rkStoreStatusCache = status || null;
    const badgeEl = document.getElementById("rk-store-status-badge");
    const backendEl = document.getElementById("rk-store-backend");
    const activeEl = document.getElementById("rk-active-key-id");
    const countEl = document.getElementById("rk-key-count");
    const warnBanner = document.getElementById("rk-store-warning-banner");
    const createBtn = document.getElementById("btn-rk-create");
    const rotateBtn = document.getElementById("btn-rk-rotate");
    const importBtn = document.getElementById("btn-rk-import");
    const verifyBtn = document.getElementById("btn-rk-verify");
    const exportBtn = document.getElementById("btn-enc-export-now");
    const tbody = document.getElementById("rk-history-tbody");

    const available = Boolean(status && status.available);
    const hasActive = Boolean(status && status.has_active_key && status.active_key_id);
    const keys = Array.isArray(status && status.keys) ? status.keys : [];

    if (badgeEl) {
        badgeEl.textContent = available ? "Operational" : "Unavailable (Manual Key Only)";
        badgeEl.className = available ? "badge badge-success" : "badge badge-danger";
    }
    if (backendEl) {
        backendEl.textContent = _formatKeyStoreBackendLabel(status && status.backend);
    }
    if (activeEl) {
        activeEl.textContent = hasActive ? String(status.active_key_id) : "Not Configured";
    }
    if (countEl) {
        countEl.textContent = `${keys.length} stored (${hasActive ? "1 active" : "0 active"})`;
    }

    if (warnBanner) {
        if (!available) {
            warnBanner.textContent =
                (status && status.message) ||
                "OS-protected key storage is unavailable. Stored-key export and key creation are disabled; use your printed Recovery Card manually for archive inspection or restoration.";
            warnBanner.className = "bkp-feedback-banner tone-danger";
            warnBanner.style.display = "block";
        } else if (!hasActive) {
            warnBanner.textContent =
                "No Recovery Key is configured on this computer yet. Click 'Create Recovery Key' to generate and print your Recovery Card, or 'Import Recovery Key' if setting up a replacement computer.";
            warnBanner.className = "bkp-feedback-banner tone-warning";
            warnBanner.style.display = "block";
        } else {
            warnBanner.style.display = "none";
            warnBanner.textContent = "";
        }
    }

    if (createBtn) {
        createBtn.style.display = !hasActive ? "" : "none";
        createBtn.disabled = !available;
    }
    if (rotateBtn) {
        rotateBtn.style.display = hasActive ? "" : "none";
        rotateBtn.disabled = !available;
    }
    if (importBtn) {
        importBtn.disabled = !available;
    }
    if (verifyBtn) {
        verifyBtn.style.display = hasActive ? "" : "none";
        verifyBtn.disabled = !available;
    }
    if (exportBtn) {
        exportBtn.disabled = !(available && hasActive);
        exportBtn.title = available && hasActive
            ? "Encrypt the selected verified .db backup using your active OS-stored Recovery Key"
            : "Configure or import an active Recovery Key in OS-protected storage first";
    }

    if (tbody) {
        if (!available) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center text-danger">OS-protected key storage is unavailable on this system. Enter your Recovery Key manually below to inspect or restore encrypted archives.</td></tr>';
        } else if (keys.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No Recovery Keys stored yet. Click "Create Recovery Key" or "Import Recovery Key".</td></tr>';
        } else {
            tbody.innerHTML = keys
                .map((k) => {
                    const statusTag = k.is_active
                        ? '<span class="badge badge-success">Active (Future Backups)</span>'
                        : '<span class="badge badge-secondary">Historical (Preserved)</span>';
                    return `
                        <tr>
                            <td><code style="font-weight: 700;">${escapeHtml(k.key_id || "-")}</code></td>
                            <td>${statusTag}</td>
                            <td>${escapeHtml(String(k.source || "generated").toUpperCase())}</td>
                            <td>${escapeHtml(k.created_at_utc || "-")}</td>
                            <td>${escapeHtml(_formatKeyStoreBackendLabel(k.storage_backend))}</td>
                            <td>${escapeHtml(k.last_used_at_utc || "Never")}</td>
                        </tr>
                    `;
                })
                .join("");
        }
    }
}

async function loadRecoveryKeyStatus() {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") return null;
    const sessionVer = authSessionVersion;
    try {
        const res = await fetch("/api/system/recovery-keys/status");
        if (sessionVer !== authSessionVersion || !hasValidAuthSession()) return null;
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            _renderRecoveryKeyStatus({
                available: false,
                backend: "unavailable",
                has_active_key: false,
                active_key_id: null,
                keys: [],
                message: data.detail || `Unable to check OS key storage (HTTP ${res.status}).`,
            });
            return null;
        }
        _renderRecoveryKeyStatus(data);
        return data;
    } catch (err) {
        _renderRecoveryKeyStatus({
            available: false,
            backend: "unavailable",
            has_active_key: false,
            active_key_id: null,
            keys: [],
            message: "Unable to contact OS key storage status endpoint.",
        });
        return null;
    }
}

async function startRecoveryKeyWorkflow(mode, buttonEl = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Recovery Key management is restricted to administrators.", "error");
        return false;
    }
    const isRotate = mode === "rotate";
    const endpoint = isRotate ? "/api/system/recovery-keys/rotate/start" : "/api/system/recovery-keys/setup/start";
    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Generating...";
    }
    try {
        const res = await fetch(endpoint, { method: "POST" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.operation_id || !data.recovery_key) {
            const msg = data.detail || `Failed to start Recovery Key ${mode} (HTTP ${res.status}).`;
            _setEncryptedBackupFeedback(msg, "danger");
            showToast(msg, "error", 6000);
            return false;
        }

        _clearRecoveryKeySetupModalDom();
        const ttlMs = Math.max(30, Number(data.expires_in_seconds || 600)) * 1000;
        _rkSetupState = {
            operation_id: data.operation_id,
            mode: data.mode || mode,
            key_id: data.key_id,
            created_at_utc: data.created_at_utc,
            expires_at_ms: Date.now() + ttlMs,
        };

        const titleEl = document.getElementById("rk-setup-modal-title");
        if (titleEl) {
            titleEl.textContent = isRotate
                ? "🔄 Rotate Recovery Key (New Key for Future Backups)"
                : "🔑 Create First-Time Recovery Key";
        }
        const rotWarn = document.getElementById("rk-setup-rotation-warning");
        if (rotWarn) rotWarn.style.display = isRotate ? "block" : "none";

        const keyDisp = document.getElementById("rk-generated-key-display");
        if (keyDisp) keyDisp.textContent = data.recovery_key;
        const keyIdEl = document.getElementById("rk-generated-key-id");
        if (keyIdEl) keyIdEl.textContent = data.key_id || "-";
        const createdEl = document.getElementById("rk-generated-created-at");
        if (createdEl) createdEl.textContent = data.created_at_utc || "-";

        // Populate dedicated Recovery Card print sheet (never saved automatically to disk)
        const cardKey = document.getElementById("rk-card-key-value");
        if (cardKey) cardKey.textContent = data.recovery_key;
        const cardId = document.getElementById("rk-card-key-id");
        if (cardId) cardId.textContent = data.key_id || "-";
        const cardIdRep = document.getElementById("rk-card-key-id-repeat");
        if (cardIdRep) cardIdRep.textContent = data.key_id || "-";
        const cardCreated = document.getElementById("rk-card-created-at");
        if (cardCreated) cardCreated.textContent = data.created_at_utc || "-";

        const modal = document.getElementById("modal-recovery-key-setup");
        if (modal) modal.style.display = "flex";

        _rkExpiryTimerId = setTimeout(() => {
            _clearRecoveryKeySetupModalDom();
            showToast("Pending Recovery Key setup expired and was cleared. Generate a new key to continue.", "warning", 6500);
        }, ttlMs);

        updateRecoveryKeyConfirmButtonState();
        return true;
    } catch (err) {
        const msg = "Error starting Recovery Key setup: " + err.message;
        _setEncryptedBackupFeedback(msg, "danger");
        showToast(msg, "error");
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

function updateRecoveryKeyConfirmButtonState() {
    const chk = document.getElementById("rk-confirm-saved-check");
    const reenter = document.getElementById("rk-confirm-reenter-input");
    const btn = document.getElementById("btn-rk-confirm-activate");
    if (!btn) return;
    const hasInput = Boolean(reenter && String(reenter.value || "").trim().length >= 20);
    btn.disabled = !(Boolean(_rkSetupState) && Boolean(chk && chk.checked) && hasInput);
}

function printRecoveryCard() {
    if (!_rkSetupState || typeof window === "undefined" || typeof document === "undefined") {
        showToast("No active Recovery Key is available to print.", "warning");
        return false;
    }
    const cardKeyEl = document.getElementById("rk-card-key-value");
    if (!cardKeyEl || !cardKeyEl.textContent || cardKeyEl.textContent === "-") {
        showToast("Recovery Card content is empty.", "warning");
        return false;
    }
    const cleanup = () => {
        if (document.body) {
            document.body.classList.remove("print-target-recovery-card");
        }
        window.removeEventListener("afterprint", cleanup);
    };
    window.addEventListener("afterprint", cleanup);
    if (document.body) {
        document.body.classList.add("print-target-recovery-card");
    }
    try {
        window.print();
    } finally {
        setTimeout(cleanup, 1500);
    }
    return true;
}

async function confirmRecoveryKeyWorkflow(buttonEl = null) {
    if (!_rkSetupState) return false;
    const chk = document.getElementById("rk-confirm-saved-check");
    const reenter = document.getElementById("rk-confirm-reenter-input");
    const errEl = document.getElementById("rk-setup-error");
    if (errEl) {
        errEl.style.display = "none";
        errEl.textContent = "";
    }
    if (!chk || !chk.checked) {
        if (errEl) {
            errEl.textContent = "Please confirm that you have printed or securely retained your Recovery Card.";
            errEl.style.display = "block";
        }
        return false;
    }
    const enteredKey = reenter ? String(reenter.value || "").trim() : "";
    if (!enteredKey) {
        if (errEl) {
            errEl.textContent = "Please re-enter your complete Recovery Key to confirm.";
            errEl.style.display = "block";
        }
        return false;
    }

    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Saving & Verifying...";
    }
    try {
        const res = await fetch("/api/system/recovery-keys/confirm", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                operation_id: _rkSetupState.operation_id,
                recovery_key: enteredKey,
                acknowledged_card_saved: true,
            }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.success) {
            const msg = data.detail || `Confirmation failed (HTTP ${res.status}).`;
            if (errEl) {
                errEl.textContent = msg;
                errEl.style.display = "block";
            }
            return false;
        }

        // Clear displayed Recovery Key & print sheet from DOM immediately on activation
        _clearRecoveryKeySetupModalDom();
        const okMsg = data.message || `Recovery Key (${data.key_id}) activated in OS-protected storage.`;
        _setEncryptedBackupFeedback(okMsg, "success");
        showToast(okMsg, "success", 6500);
        await loadRecoveryKeyStatus();
        return true;
    } catch (err) {
        if (errEl) {
            errEl.textContent = "Confirmation error: " + err.message;
            errEl.style.display = "block";
        }
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.textContent = prevText;
            updateRecoveryKeyConfirmButtonState();
        }
    }
}

async function cancelRecoveryKeyWorkflow() {
    const opId = _rkSetupState ? _rkSetupState.operation_id : null;
    _clearRecoveryKeySetupModalDom();
    if (opId && hasValidAuthSession() && currentUser && currentUser.role === "ADMIN") {
        try {
            await fetch("/api/system/recovery-keys/cancel", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ operation_id: opId }),
            });
        } catch (e) {}
    }
    return true;
}

function openImportRecoveryKeyModal() {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Recovery Key import is restricted to administrators.", "error");
        return false;
    }
    const keyInput = document.getElementById("rk-import-key-input");
    if (keyInput) {
        keyInput.value = "";
        keyInput.type = "password";
    }
    const activeChk = document.getElementById("rk-import-set-active-check");
    if (activeChk) activeChk.checked = false;
    const confirmChk = document.getElementById("rk-import-confirm-check");
    if (confirmChk) confirmChk.checked = false;
    const errEl = document.getElementById("rk-import-error");
    if (errEl) {
        errEl.style.display = "none";
        errEl.textContent = "";
    }
    const modal = document.getElementById("modal-recovery-key-import");
    if (modal) modal.style.display = "flex";
    updateImportRecoveryKeyButtonState();
    return true;
}

function closeImportRecoveryKeyModal() {
    const modal = document.getElementById("modal-recovery-key-import");
    if (modal) modal.style.display = "none";
    const keyInput = document.getElementById("rk-import-key-input");
    if (keyInput) {
        keyInput.value = "";
        keyInput.type = "password";
    }
    const activeChk = document.getElementById("rk-import-set-active-check");
    if (activeChk) activeChk.checked = false;
    const confirmChk = document.getElementById("rk-import-confirm-check");
    if (confirmChk) confirmChk.checked = false;
    const errEl = document.getElementById("rk-import-error");
    if (errEl) {
        errEl.style.display = "none";
        errEl.textContent = "";
    }
    const submitBtn = document.getElementById("btn-rk-import-submit");
    if (submitBtn) submitBtn.disabled = true;
}

function updateImportRecoveryKeyButtonState() {
    const keyInput = document.getElementById("rk-import-key-input");
    const confirmChk = document.getElementById("rk-import-confirm-check");
    const submitBtn = document.getElementById("btn-rk-import-submit");
    if (!submitBtn) return;
    const hasKey = Boolean(keyInput && String(keyInput.value || "").trim().length >= 20);
    submitBtn.disabled = !(hasKey && Boolean(confirmChk && confirmChk.checked));
}

async function submitImportRecoveryKey(buttonEl = null) {
    const keyInput = document.getElementById("rk-import-key-input");
    const activeChk = document.getElementById("rk-import-set-active-check");
    const confirmChk = document.getElementById("rk-import-confirm-check");
    const errEl = document.getElementById("rk-import-error");
    if (errEl) {
        errEl.style.display = "none";
        errEl.textContent = "";
    }
    const rawKey = keyInput ? String(keyInput.value || "").trim() : "";
    if (!rawKey || !confirmChk || !confirmChk.checked) {
        if (errEl) {
            errEl.textContent = "Enter a valid Recovery Key and confirm storing it in OS-protected storage.";
            errEl.style.display = "block";
        }
        return false;
    }

    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Importing...";
    }
    try {
        const res = await fetch("/api/system/recovery-keys/import", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                recovery_key: rawKey,
                confirm_import: true,
                set_as_active: Boolean(activeChk && activeChk.checked),
            }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.success) {
            const msg = data.detail || `Import failed (HTTP ${res.status}).`;
            if (errEl) {
                errEl.textContent = msg;
                errEl.style.display = "block";
            }
            return false;
        }
        closeImportRecoveryKeyModal();
        const okMsg = data.message || `Recovery Key (${data.key_id}) imported safely.`;
        _setEncryptedBackupFeedback(okMsg, "success");
        showToast(okMsg, "success", 6000);
        await loadRecoveryKeyStatus();
        return true;
    } catch (err) {
        if (errEl) {
            errEl.textContent = "Import error: " + err.message;
            errEl.style.display = "block";
        }
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.textContent = prevText;
            updateImportRecoveryKeyButtonState();
        }
    }
}

async function verifyStoredRecoveryKeyFromUi(buttonEl = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") return false;
    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Verifying...";
    }
    try {
        const res = await fetch("/api/system/recovery-keys/verify", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.valid) {
            const msg = "Stored Recovery Key verification failed: " + (data.detail || `HTTP ${res.status}`);
            _setEncryptedBackupFeedback(msg, "danger");
            showToast(msg, "error", 6500);
            return false;
        }
        const okMsg = `Verified OK: Active Recovery Key (${data.key_id}) retrieved and validated from ${_formatKeyStoreBackendLabel(data.storage_backend)}.`;
        _setEncryptedBackupFeedback(okMsg, "success");
        showToast(okMsg, "success", 5500);
        return true;
    } catch (err) {
        const msg = "Verification error: " + err.message;
        _setEncryptedBackupFeedback(msg, "danger");
        showToast(msg, "error");
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

async function exportEncryptedBackupFromManager(buttonEl = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Encrypted backup export is restricted to administrators.", "error");
        return false;
    }
    const sel = document.getElementById("enc-export-source-select");
    const srcName = sel ? String(sel.value || "").trim() : "";
    if (!srcName) {
        showToast("Please select a verified local .db backup first (or click 'Create Backup Now').", "warning");
        return false;
    }
    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Encrypting...";
    }
    try {
        // Uses active OS-stored key inside backend (no raw Recovery Key sent to frontend!)
        const res = await fetch("/api/system/backups/encrypt", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ backup_filename: srcName }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.success || !data.filename) {
            const msg = "Encrypted export failed: " + (data.detail || `HTTP ${res.status}`);
            _setEncryptedBackupFeedback(msg, "danger");
            showToast(msg, "error", 6500);
            return false;
        }

        const dlRes = await fetch(`/api/system/backups/encrypted/${encodeURIComponent(data.filename)}/download`);
        if (!dlRes.ok) {
            const errData = await dlRes.json().catch(() => ({}));
            const msg = "Encrypted archive created, but download failed: " + (errData.detail || `HTTP ${dlRes.status}`);
            _setEncryptedBackupFeedback(msg, "warning");
            showToast(msg, "error", 6500);
            return false;
        }
        const disp = dlRes.headers.get("Content-Disposition") || "";
        let dlName = _parseContentDispositionFilename(disp, data.filename);
        if (!dlName.toLowerCase().endsWith(".kdb.enc")) {
            dlName += ".kdb.enc";
        }
        const blob = await dlRes.blob();
        const delivered = await _deliverDownloadedBlob(blob, dlName, "application/octet-stream");
        if (!delivered) return false;
        const okMsg = `Encrypted backup "${dlName}" exported (Key ID: ${data.key_id}, ${data.size_display}). Source .db backup preserved untouched.`;
        _setEncryptedBackupFeedback(okMsg, "success");
        showToast(okMsg, "success", 6500);
        await loadRecoveryKeyStatus();
        return true;
    } catch (err) {
        const msg = "Encrypted backup export error: " + err.message;
        _setEncryptedBackupFeedback(msg, "danger");
        showToast(msg, "error");
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

function onEncryptedBackupFileSelected() {
    // Selecting a file NEVER triggers automatic inspection or automatic restore.
    _encInspectedState = null;
    const panel = document.getElementById("enc-inspect-result-panel");
    if (panel) panel.style.display = "none";
    const restoreBtn = document.getElementById("btn-enc-open-restore-confirm");
    if (restoreBtn) restoreBtn.disabled = true;
}

async function inspectUploadedEncryptedArchive(buttonEl = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") {
        showToast("Access denied: Encrypted archive inspection is restricted to administrators.", "error");
        return false;
    }
    const fileInput = document.getElementById("enc-bkp-file-input");
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (!file) {
        showToast("Please select a .kdb.enc encrypted backup archive first.", "warning");
        return false;
    }
    if (!String(file.name || "").toLowerCase().endsWith(".kdb.enc")) {
        const msg = `Unsupported file "${file.name}". Only .kdb.enc encrypted backup archives are accepted.`;
        _setEncryptedBackupFeedback(msg, "danger");
        showToast(msg, "error");
        return false;
    }

    const manualKeyInput = document.getElementById("enc-bkp-manual-key");
    const manualKey = manualKeyInput ? String(manualKeyInput.value || "").trim() : "";

    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Inspecting...";
    }
    try {
        const formData = new FormData();
        formData.append("file", file);
        if (manualKey) {
            formData.append("recovery_key", manualKey);
        }
        const res = await fetch("/api/system/backups/encrypted/inspect-upload", {
            method: "POST",
            body: formData,
        });
        const info = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = "Encrypted archive inspection failed: " + (info.detail || `HTTP ${res.status}`);
            _setEncryptedBackupFeedback(msg, "danger");
            showToast(msg, "error", 6500);
            return false;
        }

        const isAuth = Boolean(info.authenticated && info.metadata_authenticated);
        const canRestore = Boolean(isAuth && info.valid_for_restore);
        _encInspectedState = { file, info, manualKey };

        const setText = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };
        setText("enc-insp-filename", info.filename || file.name);
        setText("enc-insp-key-id", info.key_id || "-");
        setText(
            "enc-insp-meta-auth",
            isAuth
                ? "AUTHENTICATED & VERIFIED (AEAD + SHA-256 + SQLite OK)"
                : "UNAUTHENTICATED HEADER ONLY (Key required to verify)"
        );
        setText("enc-insp-created", info.created_at_utc || "-");
        setText("enc-insp-size", info.size_display || "-");
        setText(
            "enc-insp-key-source",
            info.key_source === "os_secure_store"
                ? "Matched automatically in OS Secure Store"
                : (info.key_source === "manual"
                    ? "Verified via manual Recovery Key"
                    : "Key not found in OS Secure Store — manual key required")
        );
        setText(
            "enc-insp-integrity",
            isAuth
                ? (info.integrity_ok && info.foreign_key_ok ? "PASS (Integrity + 0 FK violations)" : "FAIL")
                : "Not verified yet (awaiting Recovery Key)"
        );
        setText(
            "enc-insp-schema",
            isAuth
                ? `Schema v${info.schema_version ?? "-"} (${info.table_count ?? 0} tables${info.shop_name ? " • " + info.shop_name : ""})`
                : `Unauthenticated header claims Schema v${info.schema_version ?? "-"}`
        );

        const badge = document.getElementById("enc-inspect-auth-badge");
        if (badge) {
            badge.textContent = canRestore
                ? "Authenticated & Ready for Restore"
                : (isAuth ? "Authenticated (Failed SQLite Checks)" : "Unauthenticated Header Only");
            badge.className = canRestore
                ? "badge badge-success"
                : (isAuth ? "badge badge-danger" : "badge badge-warning");
        }
        const msgEl = document.getElementById("enc-inspect-message");
        if (msgEl) {
            msgEl.textContent = canRestore
                ? "Archive authenticated and SQLite backup verified. Click 'Restore Verified Encrypted Archive...' to review and confirm restoration."
                : (info.store_status_message ||
                    `Header parsed (unauthenticated). Enter the Recovery Key for Key ID ${info.key_id || "-"} above and click 'Inspect Encrypted Archive' again to authenticate before restoring.`);
        }

        const restoreBtn = document.getElementById("btn-enc-open-restore-confirm");
        if (restoreBtn) restoreBtn.disabled = !canRestore;

        const panel = document.getElementById("enc-inspect-result-panel");
        if (panel) panel.style.display = "block";
        _setEncryptedBackupFeedback(
            canRestore
                ? `Encrypted archive "${info.filename || file.name}" authenticated and verified (Key ID: ${info.key_id}).`
                : `Header read (unauthenticated) for "${info.filename || file.name}". Recovery Key for Key ID ${info.key_id || "-"} is required to verify.`,
            canRestore ? "success" : "warning"
        );
        return true;
    } catch (err) {
        const msg = "Encrypted archive inspection error: " + err.message;
        _setEncryptedBackupFeedback(msg, "danger");
        showToast(msg, "error");
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

function openEncryptedRestoreConfirmModal() {
    if (!_encInspectedState || !_encInspectedState.info || !_encInspectedState.info.authenticated || !_encInspectedState.info.valid_for_restore) {
        showToast("Inspect and authenticate the encrypted archive before restoring.", "warning");
        return false;
    }
    const { file, info, manualKey } = _encInspectedState;
    _pendingBackupRestore = {
        sourceType: "encrypted_upload",
        filename: info.filename || file.name,
        file,
        manualKey: manualKey || "",
        info,
    };
    _populateBackupRestoreConfirmModal("Encrypted Backup Archive (.kdb.enc)", {
        ...info,
        modified_at: info.created_at_utc || "-",
    });
    return true;
}

window.loadRecoveryKeyStatus = loadRecoveryKeyStatus;
window.clearSensitiveRecoveryKeyUiState = clearSensitiveRecoveryKeyUiState;
window.startRecoveryKeyWorkflow = startRecoveryKeyWorkflow;
window.updateRecoveryKeyConfirmButtonState = updateRecoveryKeyConfirmButtonState;
window.printRecoveryCard = printRecoveryCard;
window.confirmRecoveryKeyWorkflow = confirmRecoveryKeyWorkflow;
window.cancelRecoveryKeyWorkflow = cancelRecoveryKeyWorkflow;
window.openImportRecoveryKeyModal = openImportRecoveryKeyModal;
window.closeImportRecoveryKeyModal = closeImportRecoveryKeyModal;
window.updateImportRecoveryKeyButtonState = updateImportRecoveryKeyButtonState;
window.submitImportRecoveryKey = submitImportRecoveryKey;
window.verifyStoredRecoveryKeyFromUi = verifyStoredRecoveryKeyFromUi;
window.exportEncryptedBackupFromManager = exportEncryptedBackupFromManager;
window.onEncryptedBackupFileSelected = onEncryptedBackupFileSelected;
window.clearEncryptedInspectResult = clearEncryptedInspectResult;
window.inspectUploadedEncryptedArchive = inspectUploadedEncryptedArchive;
window.openEncryptedRestoreConfirmModal = openEncryptedRestoreConfirmModal;

// =========================================================================
// Phase 2C-5A: Encrypted Off-Site Backup via Synchronized Folder
// =========================================================================
let _offsiteStatusCache = null;
let _offsiteRecoveryState = null; // Ephemeral in-memory only: { filename, folderPath, info, manualKey }

function _setOffsiteFeedback(message, tone = "info") {
    const el = document.getElementById("offsite-feedback");
    if (!el) return;
    if (!message) {
        el.style.display = "none";
        el.textContent = "";
        el.className = "bkp-feedback-banner";
        return;
    }
    el.textContent = message;
    el.className = `bkp-feedback-banner tone-${tone}`;
    el.style.display = "block";
}

function _formatOffsiteStatusLabel(statusCode) {
    const labels = {
        DISABLED: ["Disabled", "badge-secondary"],
        IDLE: ["Enabled • Ready", "badge-info"],
        LOCAL_BACKUP_VERIFIED: ["Local Backup Verified", "badge-info"],
        ENCRYPTION_COMPLETED: ["Encryption Completed", "badge-info"],
        COPIED_TO_SYNC_FOLDER: ["Copied to Sync Folder", "badge-info"],
        DESTINATION_CHECKSUM_VERIFIED: ["Checksum Verified", "badge-success"],
        CLOUD_SYNC_NOT_CONFIRMED: ["Copied & Verified • Cloud Sync Not Confirmed", "badge-warning"],
        DESTINATION_UNAVAILABLE: ["Destination Unavailable", "badge-danger"],
        RECOVERY_KEY_UNAVAILABLE: ["Recovery Key Unavailable", "badge-danger"],
        OPERATION_FAILED: ["Operation Failed", "badge-danger"],
    };
    return labels[String(statusCode || "DISABLED").toUpperCase()] || [String(statusCode || "Unknown"), "badge-secondary"];
}

function clearOffsiteRecoveryState() {
    _offsiteRecoveryState = null;
    const panel = document.getElementById("offsite-recovery-panel");
    if (panel) panel.style.display = "none";
    const keyInput = document.getElementById("offsite-rec-manual-key");
    if (keyInput) {
        keyInput.value = "";
        keyInput.type = "password";
    }
    const restoreBtn = document.getElementById("btn-offsite-open-restore-confirm");
    if (restoreBtn) restoreBtn.disabled = true;
}

function _renderOffsiteArchivesTable(archives, folderPath) {
    const tbody = document.getElementById("offsite-archives-tbody");
    if (!tbody) return;
    if (!Array.isArray(archives) || archives.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No encrypted .kdb.enc archives found in this synchronized folder yet.</td></tr>';
        return;
    }
    tbody.innerHTML = archives
        .map((a) => {
            const fname = escapeHtml(String(a.filename || ""));
            const fpath = escapeHtml(String(folderPath || ""));
            const hdrBadge = a.header_valid
                ? '<span class="badge badge-info">Header Valid (Unauthenticated)</span>'
                : '<span class="badge badge-danger">Invalid Header</span>';
            return `
                <tr>
                    <td><code style="font-weight: 700;">${fname}</code></td>
                    <td><code>${escapeHtml(a.key_id || "-")}</code></td>
                    <td>${escapeHtml(a.created_at_utc || a.modified_at || "-")}</td>
                    <td class="text-right font-bold">${escapeHtml(a.size_display || "-")}</td>
                    <td class="text-center">${hdrBadge}</td>
                    <td class="text-center">
                        <button type="button" class="btn btn-outline-primary btn-sm" onclick="inspectOffsiteArchiveRow('${fname}', '${fpath}', this)" ${a.header_valid ? "" : "disabled"}>🔍 Inspect &amp; Recover</button>
                    </td>
                </tr>
            `;
        })
        .join("");
}

function _renderOffsiteBackupStatus(data) {
    _offsiteStatusCache = data || null;
    const enabledChk = document.getElementById("offsite-enabled-toggle");
    const folderInput = document.getElementById("offsite-folder-path-input");
    const destBadge = document.getElementById("offsite-dest-badge");
    const statusBadge = document.getElementById("offsite-status-badge");
    const lastCopyEl = document.getElementById("offsite-last-copy-info");
    const runBtn = document.getElementById("btn-offsite-run-now");
    const localSel = document.getElementById("offsite-local-enc-select");
    const copyLocalBtn = document.getElementById("btn-offsite-copy-local-enc");

    if (enabledChk) enabledChk.checked = Boolean(data && data.enabled);
    if (folderInput && data && data.destination_folder && !folderInput.value) {
        folderInput.value = data.destination_folder;
    } else if (folderInput && data && data.destination_folder) {
        folderInput.value = data.destination_folder;
    }

    const destAvail = Boolean(data && data.destination_available);
    if (destBadge) {
        if (!data || !data.destination_folder) {
            destBadge.textContent = "Not Configured";
            destBadge.className = "badge badge-secondary";
        } else if (destAvail) {
            destBadge.textContent = `Available & Writable${data.destination_free_display ? " (" + data.destination_free_display + " free)" : ""}`;
            destBadge.className = "badge badge-success";
        } else {
            destBadge.textContent = "Destination Unavailable";
            destBadge.className = "badge badge-danger";
        }
    }

    if (statusBadge) {
        const [label, cls] = _formatOffsiteStatusLabel(data && data.status);
        statusBadge.textContent = label;
        statusBadge.className = `badge ${cls}`;
    }

    if (lastCopyEl) {
        if (data && data.last_published_filename) {
            const shaShort = data.last_published_sha256 ? String(data.last_published_sha256).slice(0, 16) + "..." : "-";
            const chkText = data.last_checksum_verified ? "Checksum Verified" : "Unverified";
            lastCopyEl.textContent = `${data.last_published_at_utc || "-"} • ${data.last_published_filename} (${data.last_published_size_display || "-"}) • ${chkText} • SHA-256: ${shaShort} • Cloud Sync: Not Confirmed`;
        } else {
            lastCopyEl.textContent = "No encrypted off-site copy recorded yet.";
        }
    }

    const rkAvail = Boolean(data && data.recovery_key_available);
    if (runBtn) {
        runBtn.disabled = !(destAvail && rkAvail);
        runBtn.title = destAvail && rkAvail
            ? "Encrypt latest verified local backup and copy .kdb.enc to synchronized folder"
            : (!destAvail ? "Select and validate a writable synchronized folder first" : "Configure an active OS-protected Recovery Key first");
    }

    const localEncs = Array.isArray(data && data.local_encrypted_archives) ? data.local_encrypted_archives : [];
    if (localSel) {
        if (localEncs.length === 0) {
            localSel.innerHTML = '<option value="">No local .kdb.enc archives available</option>';
        } else {
            localSel.innerHTML = localEncs
                .map((e) => `<option value="${escapeHtml(e.filename)}">${escapeHtml(e.filename)} (${escapeHtml(e.size_display || "-")})</option>`)
                .join("");
        }
    }
    if (copyLocalBtn) {
        copyLocalBtn.disabled = !(destAvail && localEncs.length > 0);
    }

    _renderOffsiteArchivesTable(
        Array.isArray(data && data.published_archives) ? data.published_archives : [],
        data && data.destination_folder ? data.destination_folder : ""
    );
}

async function loadOffsiteBackupStatus() {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") return null;
    const sessionVer = authSessionVersion;
    try {
        const res = await fetch("/api/system/offsite-backup/status");
        if (sessionVer !== authSessionVersion || !hasValidAuthSession()) return null;
        const data = await res.json().catch(() => ({}));
        if (!res.ok) return null;
        _renderOffsiteBackupStatus(data);
        return data;
    } catch (err) {
        return null;
    }
}

async function pickOffsiteSyncFolder() {
    const input = document.getElementById("offsite-folder-path-input");
    const desktopCtx = typeof window !== "undefined" ? window.__KRUSHIDHAN_DESKTOP__ : null;
    if (desktopCtx && typeof desktopCtx.pickFolder === "function") {
        try {
            const res = await desktopCtx.pickFolder("Select Synchronized Backup Folder");
            if (res && !res.canceled && res.path) {
                if (input) input.value = res.path;
                await validateOffsiteFolderFromUi();
                return res.path;
            }
            return null;
        } catch (err) {
            showToast("Could not open desktop folder dialog: " + err.message, "error");
            return null;
        }
    }
    if (input) {
        input.focus();
        showToast("Enter the full absolute path of your synchronized folder and click 'Validate Destination'.", "info", 5000);
    }
    return null;
}

async function validateOffsiteFolderFromUi(buttonEl = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") return null;
    const input = document.getElementById("offsite-folder-path-input");
    const folderPath = input ? String(input.value || "").trim() : "";
    if (!folderPath) {
        showToast("Enter or select a synchronized folder path first.", "warning");
        return null;
    }
    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Validating...";
    }
    try {
        const res = await fetch("/api/system/offsite-backup/validate-folder", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ folder_path: folderPath, include_archives: true }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.available) {
            const msg = data.message || data.detail || "Selected folder is unavailable or not writable.";
            _setOffsiteFeedback(msg, "danger");
            showToast(msg, "error", 6000);
            return data;
        }
        if (input && data.destination_folder) input.value = data.destination_folder;
        const okMsg = `Destination validated: "${data.destination_folder}" is writable (${data.free_display || "available"}). Found ${(data.archives || []).length} encrypted archive(s).`;
        _setOffsiteFeedback(okMsg, "success");
        showToast(okMsg, "success", 5500);
        if (Array.isArray(data.archives)) {
            _renderOffsiteArchivesTable(data.archives, data.destination_folder);
        }
        return data;
    } catch (err) {
        _setOffsiteFeedback("Folder validation error: " + err.message, "danger");
        return null;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

async function saveOffsiteBackupConfig(buttonEl = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") return false;
    const enabledChk = document.getElementById("offsite-enabled-toggle");
    const input = document.getElementById("offsite-folder-path-input");
    const enabled = Boolean(enabledChk && enabledChk.checked);
    const folderPath = input ? String(input.value || "").trim() : "";

    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Saving...";
    }
    try {
        const res = await fetch("/api/system/offsite-backup/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                enabled,
                destination_folder: folderPath || null,
            }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = data.detail || `Could not save off-site settings (HTTP ${res.status}).`;
            _setOffsiteFeedback(msg, "danger");
            showToast(msg, "error", 6000);
            return false;
        }
        _renderOffsiteBackupStatus(data);
        const okMsg = enabled
            ? `Off-site encrypted backup enabled for "${data.destination_folder}". Cloud upload completion remains provider-managed.`
            : "Off-site encrypted backup disabled. Existing archives in your synchronized folder remain untouched.";
        _setOffsiteFeedback(okMsg, "success");
        showToast(okMsg, "success", 5500);
        return true;
    } catch (err) {
        _setOffsiteFeedback("Error saving off-site config: " + err.message, "danger");
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

async function runOffsiteBackupNow(allowDuplicate = false, buttonEl = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") return false;
    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Encrypting & Copying...";
    }
    try {
        const res = await fetch("/api/system/offsite-backup/run-now", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ allow_duplicate: Boolean(allowDuplicate) }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = data.detail || `Off-site backup failed (HTTP ${res.status}).`;
            _setOffsiteFeedback(msg, "danger");
            showToast(msg, "error", 6500);
            await loadOffsiteBackupStatus();
            return false;
        }
        const okMsg = data.action === "skipped"
            ? (data.message || "Unchanged verified backup already published.")
            : `Archive "${data.filename}" copied & checksum-verified (SHA-256: ${String(data.sha256 || "").slice(0, 16)}...). ${data.cloud_sync_disclaimer || ""}`;
        _setOffsiteFeedback(okMsg, "success");
        showToast(okMsg, "success", 7000);
        await loadOffsiteBackupStatus();
        return true;
    } catch (err) {
        _setOffsiteFeedback("Off-site backup error: " + err.message, "danger");
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

async function copyLocalEncryptedArchiveOffsite(buttonEl = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") return false;
    const sel = document.getElementById("offsite-local-enc-select");
    const fname = sel ? String(sel.value || "").trim() : "";
    if (!fname) {
        showToast("Select a local .kdb.enc archive first.", "warning");
        return false;
    }
    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Copying...";
    }
    try {
        const res = await fetch("/api/system/offsite-backup/copy-local-encrypted", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ encrypted_filename: fname }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = data.detail || `Copy failed (HTTP ${res.status}).`;
            _setOffsiteFeedback(msg, "danger");
            showToast(msg, "error", 6000);
            return false;
        }
        const okMsg = `Copied "${data.filename}" to synchronized folder (checksum verified; existing archives preserved). ${data.cloud_sync_disclaimer || ""}`;
        _setOffsiteFeedback(okMsg, "success");
        showToast(okMsg, "success", 6500);
        await loadOffsiteBackupStatus();
        return true;
    } catch (err) {
        _setOffsiteFeedback("Copy error: " + err.message, "danger");
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

async function inspectOffsiteArchiveRow(filename, folderPath, buttonEl = null, manualKeyOverride = null) {
    if (!hasValidAuthSession() || !currentUser || currentUser.role !== "ADMIN") return false;
    const prevText = buttonEl ? buttonEl.textContent : "";
    if (buttonEl) {
        buttonEl.disabled = true;
        buttonEl.textContent = "⏳ Inspecting...";
    }
    try {
        const payload = { filename, folder_path: folderPath || null };
        if (manualKeyOverride && String(manualKeyOverride).trim()) {
            payload.recovery_key = String(manualKeyOverride).trim();
        }
        const res = await fetch("/api/system/offsite-backup/inspect-folder-archive", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const info = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = "Archive inspection failed: " + (info.detail || `HTTP ${res.status}`);
            _setOffsiteFeedback(msg, "danger");
            showToast(msg, "error", 6000);
            return false;
        }

        const isAuth = Boolean(info.authenticated && info.metadata_authenticated);
        const canRestore = Boolean(isAuth && info.valid_for_restore);
        _offsiteRecoveryState = {
            filename: info.filename || filename,
            folderPath: info.destination_folder || folderPath || null,
            info,
            manualKey: manualKeyOverride ? String(manualKeyOverride).trim() : "",
        };

        const setText = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };
        setText("offsite-rec-filename", info.filename || filename);
        setText("offsite-rec-key-id", info.key_id || "-");
        setText(
            "offsite-rec-integrity",
            isAuth
                ? (info.integrity_ok && info.foreign_key_ok ? "PASS (Integrity + 0 FK violations)" : "FAIL")
                : "Not verified yet (awaiting Recovery Key)"
        );
        setText(
            "offsite-rec-schema",
            isAuth
                ? `Schema v${info.schema_version ?? "-"} (${info.table_count ?? 0} tables${info.shop_name ? " • " + info.shop_name : ""})`
                : `Unauthenticated header claims Schema v${info.schema_version ?? "-"}`
        );

        const badge = document.getElementById("offsite-rec-auth-badge");
        if (badge) {
            badge.textContent = canRestore
                ? "Authenticated & Ready for Restore"
                : (isAuth ? "Authenticated (SQLite Check Failed)" : "Unauthenticated Header Only");
            badge.className = canRestore ? "badge badge-success" : (isAuth ? "badge badge-danger" : "badge badge-warning");
        }
        const msgEl = document.getElementById("offsite-rec-message");
        if (msgEl) {
            msgEl.textContent = canRestore
                ? "Archive authenticated and verified. Click 'Restore Verified Archive...' to review and confirm restoration. Original archive will not be modified."
                : (info.store_status_message || `Import or enter the Recovery Key for Key ID ${info.key_id || "-"} to authenticate before restoring.`);
        }
        const restoreBtn = document.getElementById("btn-offsite-open-restore-confirm");
        if (restoreBtn) restoreBtn.disabled = !canRestore;
        const panel = document.getElementById("offsite-recovery-panel");
        if (panel) panel.style.display = "block";
        return true;
    } catch (err) {
        _setOffsiteFeedback("Inspection error: " + err.message, "danger");
        return false;
    } finally {
        if (buttonEl) {
            buttonEl.disabled = false;
            buttonEl.textContent = prevText;
        }
    }
}

async function reverifySelectedOffsiteArchive(buttonEl = null) {
    if (!_offsiteRecoveryState) return false;
    const keyInput = document.getElementById("offsite-rec-manual-key");
    const manualKey = keyInput ? String(keyInput.value || "").trim() : "";
    return inspectOffsiteArchiveRow(
        _offsiteRecoveryState.filename,
        _offsiteRecoveryState.folderPath,
        buttonEl,
        manualKey || null
    );
}

function openOffsiteFolderRestoreConfirmModal() {
    if (!_offsiteRecoveryState || !_offsiteRecoveryState.info || !_offsiteRecoveryState.info.authenticated || !_offsiteRecoveryState.info.valid_for_restore) {
        showToast("Inspect and authenticate the synchronized-folder archive first.", "warning");
        return false;
    }
    const { filename, folderPath, info, manualKey } = _offsiteRecoveryState;
    _pendingBackupRestore = {
        sourceType: "offsite_folder",
        filename,
        folderPath,
        manualKey: manualKey || "",
        info,
    };
    _populateBackupRestoreConfirmModal("Synchronized Folder Encrypted Archive (.kdb.enc)", {
        ...info,
        modified_at: info.created_at_utc || "-",
    });
    return true;
}

window.loadOffsiteBackupStatus = loadOffsiteBackupStatus;
window.clearOffsiteRecoveryState = clearOffsiteRecoveryState;
window.pickOffsiteSyncFolder = pickOffsiteSyncFolder;
window.validateOffsiteFolderFromUi = validateOffsiteFolderFromUi;
window.saveOffsiteBackupConfig = saveOffsiteBackupConfig;
window.runOffsiteBackupNow = runOffsiteBackupNow;
window.copyLocalEncryptedArchiveOffsite = copyLocalEncryptedArchiveOffsite;
window.inspectOffsiteArchiveRow = inspectOffsiteArchiveRow;
window.reverifySelectedOffsiteArchive = reverifySelectedOffsiteArchive;
window.openOffsiteFolderRestoreConfirmModal = openOffsiteFolderRestoreConfirmModal;






