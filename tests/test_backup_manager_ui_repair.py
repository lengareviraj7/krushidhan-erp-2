"""
Phase 1A Regression Tests: Backup Manager UI Repair, Confirm Dialogs & Isolated Restore Flow.

Strictly uses isolated temporary SQLite databases and temporary BACKUP_DIR directories.
Never touches the live application database or repository backups folder.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, reset_db_manager
from src.services.auth_service import AuthService
from src.services.backup_service import BackupService

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML_PATH = PROJECT_ROOT / "src" / "web" / "templates" / "index.html"
APP_JS_PATH = PROJECT_ROOT / "src" / "web" / "static" / "js" / "app.js"


@pytest.fixture
def isolated_backup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Isolated temp SQLite database + isolated temp BACKUP_DIR (zero live DB/backup touches)."""
    db_path = tmp_path / "isolated_erp.db"
    backup_dir = tmp_path / "isolated_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    reset_db_manager()

    db_mgr = DatabaseManager(db_path=db_path, backup_dir=backup_dir)
    db_mgr.initialize_database(include_seed=True)

    # Clear any migration pre-upgrade backups so empty-state test starts clean
    for f in backup_dir.glob("*.db"):
        f.unlink()

    def _token(role: str, uid: int) -> str:
        return AuthService.create_access_token(
            user_id=uid,
            username=role.lower(),
            full_name=f"Test {role.title()}",
            role=role,
        )

    yield {
        "db_mgr": db_mgr,
        "db_path": db_path,
        "backup_dir": backup_dir,
        "tokens": {
            "ADMIN": _token("ADMIN", 1),
            "OPERATOR": _token("OPERATOR", 2),
            "MANAGER": _token("MANAGER", 3),
            "ACCOUNTANT": _token("ACCOUNTANT", 4),
        },
    }
    reset_db_manager()


def _client(token: str | None = None) -> TestClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, headers=headers)


# ---------------------------------------------------------------------------
# 1. HTML Structural Integrity & ID / Handler Wiring Verification
# ---------------------------------------------------------------------------
def test_backup_manager_html_structure_and_ids():
    """Verify repaired Backup Manager, Restore Confirm, App Confirm & Input modals parse cleanly with zero duplicate IDs."""
    html = INDEX_HTML_PATH.read_text(encoding="utf-8")
    js = APP_JS_PATH.read_text(encoding="utf-8")

    ids = []
    elements_by_id: Dict[str, Dict[str, str]] = {}

    class StrictHtmlInspector(HTMLParser):
        def handle_starttag(self, tag: str, attrs):
            attr_map = {k: (v or "") for k, v in attrs}
            el_id = attr_map.get("id")
            if el_id:
                ids.append(el_id)
                elements_by_id[el_id] = {"tag": tag, **attr_map}

    inspector = StrictHtmlInspector()
    inspector.feed(html)

    duplicates = {k: v for k, v in Counter(ids).items() if v > 1}
    assert not duplicates, f"Duplicate HTML IDs found in index.html: {duplicates}"

    required_ids = (
        # Header & drawer admin controls
        "btn-header-quick-backup",
        "btn-header-backup-manager",
        "btn-drawer-quick-backup",
        "btn-drawer-backup-manager",
        # Backup Manager card
        "settings-backup-manager-card",
        "btn-bkp-refresh",
        "btn-bkp-create-now",
        "bkp-runtime-mode",
        "bkp-runtime-dir-path",
        "bkp-runtime-summary",
        "bkp-mgr-feedback",
        "bkp-external-file-input",
        "btn-bkp-inspect-external",
        "bkp-mgr-tbody",
        # Restore confirmation modal
        "modal-backup-restore-confirm",
        "bkp-confirm-source",
        "bkp-confirm-filename",
        "bkp-confirm-date",
        "bkp-confirm-size",
        "bkp-confirm-integrity",
        "bkp-confirm-fk",
        "bkp-confirm-schema",
        "bkp-confirm-shop",
        "bkp-restore-confirm-check",
        "btn-cancel-backup-restore",
        "btn-confirm-backup-restore",
        # Reusable non-blocking confirm & input modals + toast host
        "modal-app-confirm",
        "app-confirm-header",
        "app-confirm-title",
        "app-confirm-close-x",
        "app-confirm-message",
        "app-confirm-cancel",
        "app-confirm-ok",
        "modal-app-input",
        "app-input-title",
        "app-input-close-x",
        "app-input-form",
        "app-input-message",
        "app-input-field",
        "app-input-pwd-toggle",
        "app-input-error",
        "app-input-cancel",
        "app-input-ok",
        "krushidhan-toast-container",
    )
    for req_id in required_ids:
        assert req_id in elements_by_id, f"Missing required element id='{req_id}' in index.html"

    # Admin-only backup elements must default to hidden in initial static HTML
    for admin_only_id in (
        "btn-header-quick-backup",
        "btn-header-backup-manager",
        "btn-drawer-quick-backup",
        "btn-drawer-backup-manager",
        "settings-backup-manager-card",
    ):
        style = elements_by_id[admin_only_id].get("style", "").replace(" ", "").lower()
        assert "display:none" in style, f"Admin-only element '{admin_only_id}' should default to display:none"

    # Confirm restore button starts disabled until explicit checkbox confirmation
    assert "disabled" in elements_by_id["btn-confirm-backup-restore"]

    # Every backup/dialog handler referenced in HTML exists in app.js
    for fn_name in (
        "triggerDatabaseBackup",
        "openBackupManagerView",
        "loadBackupManagerData",
        "createManualBackupFromManager",
        "inspectExternalBackupFile",
        "closeBackupRestoreModal",
        "onBackupRestoreConfirmToggle",
        "executeConfirmedBackupRestore",
        "togglePasswordVisibility",
        "handleLogout",
    ):
        assert re.search(rf"\bfunction\s+{fn_name}\s*\(", js), f"Missing JS function {fn_name} in app.js"


# ---------------------------------------------------------------------------
# 2. Isolated End-to-End Backup, Inspect, Export, Restore & RBAC API Tests
# ---------------------------------------------------------------------------
def test_isolated_backup_manager_api_lifecycle_and_restore(isolated_backup_env: Dict[str, Any]):
    """
    Tests empty state -> create -> list -> inspect -> export -> unconfirmed restore rejected
    -> isolated restore rolls back state & creates recoverable BEFORE_RESTORE snapshot.
    """
    admin = _client(isolated_backup_env["tokens"]["ADMIN"])
    db_mgr: DatabaseManager = isolated_backup_env["db_mgr"]
    backup_dir: Path = isolated_backup_env["backup_dir"]

    # 1. Empty state
    res_empty = admin.get("/api/system/backups")
    assert res_empty.status_code == 200
    assert res_empty.json()["backups"] == []
    assert Path(res_empty.json()["backup_dir"]).resolve() == backup_dir.resolve()

    # 2. Create backup
    res_create = admin.post("/api/system/backup")
    assert res_create.status_code == 200, res_create.text
    created = res_create.json()
    assert created["success"] is True
    bkp_name = created["filename"]
    assert bkp_name.endswith(".db")
    assert (backup_dir / bkp_name).is_file()

    # 3. List backups shows verified snapshot
    res_list = admin.get("/api/system/backups")
    assert res_list.status_code == 200
    items = res_list.json()["backups"]
    assert len(items) == 1
    assert items[0]["filename"] == bkp_name
    assert items[0]["integrity_ok"] is True
    assert items[0]["valid_for_restore"] is True

    # 4. Inspect backup
    res_inspect = admin.get(f"/api/system/backups/{bkp_name}/inspect")
    assert res_inspect.status_code == 200
    info = res_inspect.json()
    assert info["integrity_ok"] is True
    assert info["foreign_key_ok"] is True
    assert info["valid_for_restore"] is True
    assert info["has_required_tables"] is True

    # 5. Export / download backup returns valid SQLite 3 binary
    res_dl = admin.get(f"/api/system/backups/{bkp_name}/download")
    assert res_dl.status_code == 200
    assert res_dl.content.startswith(BackupService.SQLITE_HEADER_MAGIC)
    assert f'filename="{bkp_name}"' in res_dl.headers.get("content-disposition", "")
    assert res_dl.headers.get("x-content-type-options") == "nosniff"

    # 6. Insert a post-backup marker record into isolated DB
    with db_mgr.transaction(mode="IMMEDIATE") as conn:
        conn.execute(
            "INSERT INTO products (product_name, category_id, hsn_code, default_purchase_rate, default_sale_rate, default_mrp) "
            "VALUES ('POST_BACKUP_MARKER_ITEM', 1, '310599', 100.0, 120.0, 130.0);"
        )
    assert db_mgr.fetch_one("SELECT 1 FROM products WHERE product_name = 'POST_BACKUP_MARKER_ITEM';") is not None

    # 7. Unconfirmed restore (confirm_restore=False) rejected and leaves DB unchanged
    res_unconfirmed = admin.post(
        "/api/system/backup/restore",
        json={"backup_filename": bkp_name, "confirm_restore": False},
    )
    assert res_unconfirmed.status_code == 400
    assert db_mgr.fetch_one("SELECT 1 FROM products WHERE product_name = 'POST_BACKUP_MARKER_ITEM';") is not None

    # 8. Confirmed restore against isolated temp DB reverts marker & saves BEFORE_RESTORE snapshot
    res_restore = admin.post(
        "/api/system/backup/restore",
        json={"backup_filename": bkp_name, "confirm_restore": True},
    )
    assert res_restore.status_code == 200, res_restore.text
    restore_data = res_restore.json()
    assert restore_data["success"] is True
    pre_restore_name = restore_data["pre_restore_filename"]
    assert pre_restore_name and pre_restore_name.startswith("agri_erp_before_restore")
    assert (backup_dir / pre_restore_name).is_file()

    # Live isolated DB is back to pre-marker snapshot
    assert db_mgr.fetch_one("SELECT 1 FROM products WHERE product_name = 'POST_BACKUP_MARKER_ITEM';") is None


def test_backup_manager_rbac_blocks_unauthorized_roles(isolated_backup_env: Dict[str, Any]):
    """Unauthenticated, OPERATOR, MANAGER, and ACCOUNTANT users cannot access administrative backup endpoints."""
    admin = _client(isolated_backup_env["tokens"]["ADMIN"])
    created_name = admin.post("/api/system/backup").json()["filename"]

    # Unauthenticated -> 401
    unauth = _client(None)
    assert unauth.get("/api/system/backups").status_code == 401
    assert unauth.post("/api/system/backup").status_code == 401

    # Non-admin roles -> 403
    for role in ("OPERATOR", "MANAGER", "ACCOUNTANT"):
        c = _client(isolated_backup_env["tokens"][role])
        assert c.get("/api/system/backups").status_code == 403
        assert c.post("/api/system/backup").status_code == 403
        assert c.get(f"/api/system/backups/{created_name}/inspect").status_code == 403
        assert c.get(f"/api/system/backups/{created_name}/download").status_code == 403
        assert c.post(
            "/api/system/backup/restore",
            json={"backup_filename": created_name, "confirm_restore": True},
        ).status_code == 403


# ---------------------------------------------------------------------------
# 3. Node.js Runtime UI Integration Tests (Real index.html + app.js)
# ---------------------------------------------------------------------------
def test_backup_manager_and_logout_confirm_node_runtime():
    """
    Executes real app.js against IDs parsed from real index.html in Node.js to verify:
    - Empty state rendering
    - Delegated listener registered only once (no duplicate invocations)
    - XSS-safe escaping of filenames in table rows
    - Restore cancellation leaves DB untouched (no restore POST)
    - Logout confirm modal cancel vs accept behavior
    - Native desktop saveFile cancellation & success handling
    - Non-admin role blocked from Backup Manager actions with zero API calls
    """
    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js is required for frontend runtime regression test"

    script = r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const appJsCode = fs.readFileSync(process.env.APP_JS_PATH, "utf-8");
const htmlText = fs.readFileSync(process.env.INDEX_HTML_PATH, "utf-8");

class FakeElement {
    constructor(id = "", tagName = "div") {
        this.id = id;
        this.tagName = tagName.toUpperCase();
        this.style = {};
        this.dataset = {};
        this.children = [];
        this.parentNode = null;
        this.textContent = "";
        this._innerHTML = "";
        this.value = "";
        this.checked = false;
        this.disabled = false;
        this.listeners = {};
        this.attrs = {};
        const cls = new Set();
        this.classList = {
            add: (...c) => c.forEach((x) => cls.add(x)),
            remove: (...c) => c.forEach((x) => cls.delete(x)),
            contains: (c) => cls.has(c),
        };
    }
    set className(v) { this._className = v; }
    get className() { return this._className || ""; }
    set innerHTML(v) { this._innerHTML = String(v); }
    get innerHTML() { return this._innerHTML; }
    get firstChild() { return this.children[0] || null; }
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
    removeChild(child) { this.children = this.children.filter((c) => c !== child); child.parentNode = null; return child; }
    addEventListener(type, fn) {
        if (!this.listeners[type]) this.listeners[type] = [];
        this.listeners[type].push(fn);
    }
    removeEventListener(type, fn) {
        if (!this.listeners[type]) return;
        this.listeners[type] = this.listeners[type].filter((f) => f !== fn);
    }
    dispatchEvent(type, ev = {}) {
        const eventObj = { target: this, preventDefault: () => {}, ...ev };
        for (const fn of [...(this.listeners[type] || [])]) fn(eventObj);
    }
    click() { this.dispatchEvent("click", { target: this }); }
    focus() {}
    scrollIntoView() {}
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return this.attrs[k] ?? null; }
    closest(sel) { return this._closestTarget || null; }
}

const elements = new Map();
for (const m of htmlText.matchAll(/id="([^"]+)"/g)) {
    elements.set(m[1], new FakeElement(m[1], "div"));
}

const storage = new Map([["krushidhan_auth_token", "jwt-admin-token"]]);
const fetchCalls = [];
let mockBackups = [];
let saveHookReturn = { saved: true, canceled: false, path: "/tmp/exported.db" };
const nativeSaves = [];

function makeRes(status, body, headers = {}) {
    const hmap = {};
    for (const [k, v] of Object.entries(headers)) hmap[k.toLowerCase()] = v;
    const raw = typeof body === "string" ? body : JSON.stringify(body);
    return {
        status,
        ok: status >= 200 && status < 300,
        headers: { get: (k) => hmap[String(k).toLowerCase()] || null },
        json: async () => (typeof body === "string" ? JSON.parse(body) : body),
        text: async () => raw,
        blob: async () => ({ type: hmap["content-type"] || "application/vnd.sqlite3", arrayBuffer: async () => Buffer.from("SQLite format 3\0mock") }),
        clone: () => makeRes(status, body, headers),
    };
}

const ctx = {
    location: { origin: "http://127.0.0.1:51234", protocol: "http:" },
    navigator: { onLine: true },
    URL: { createObjectURL: () => "blob:mock", revokeObjectURL: () => {} },
    Blob: class FakeBlob { constructor(p, o = {}) { this.p = p; this.type = o.type || ""; } },
    FormData: class FakeFormData { constructor() { this.fields = []; } append(k, v) { this.fields.push([k, v]); } },
    Uint8Array,
    ArrayBuffer,
    JSON,
    Date,
    Math,
    Promise,
    Error,
    setTimeout: (fn) => { fn(); return 1; },
    clearTimeout: () => {},
    setInterval: () => 1,
    clearInterval: () => {},
    console,
    document: {
        body: new FakeElement("body", "body"),
        hidden: false,
        getElementById: (id) => elements.get(id) || null,
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: (tag) => new FakeElement("", tag),
        addEventListener: () => {},
    },
    localStorage: {
        getItem: (k) => (storage.has(k) ? storage.get(k) : null),
        setItem: (k, v) => storage.set(k, String(v)),
        removeItem: (k) => storage.delete(k),
    },
    addEventListener: () => {},
    removeEventListener: () => {},
    fetch: async (url, opts = {}) => {
        const u = String(url);
        const method = (opts.method || "GET").toUpperCase();
        fetchCalls.push({ method, url: u, opts });
        if (u.endsWith("/api/system/runtime-info")) {
            return makeRes(200, { app_mode: "desktop", app_version: "1.0.0", backup_dir: "/tmp/test_backups" });
        }
        if (u.endsWith("/api/system/backups")) {
            return makeRes(200, { backup_dir: "/tmp/test_backups", backups: mockBackups });
        }
        if (u.includes("/inspect")) {
            return makeRes(200, {
                filename: "agri_erp_backup_20261117.db",
                size_display: "300.0 KB",
                modified_at: "2026-11-17 16:00:00",
                integrity_ok: true,
                foreign_key_ok: true,
                foreign_key_violation_count: 0,
                table_count: 33,
                schema_version: 4,
                shop_name: "Isolated Test Shop",
                has_required_tables: true,
                valid_for_restore: true,
            });
        }
        if (u.includes("/download")) {
            return makeRes(200, "SQLite format 3\0mock", {
                "Content-Type": "application/vnd.sqlite3",
                "Content-Disposition": 'attachment; filename="agri_erp_backup_20261117.db"',
            });
        }
        return makeRes(200, { success: true });
    },
};
ctx.window = ctx;
ctx.self = ctx;
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(appJsCode, ctx, { filename: "app.js" });

(async () => {
    vm.runInContext('currentUser = { user_id: 1, username: "admin", full_name: "Shop Admin", role: "ADMIN" };', ctx);
    ctx.setKrushidhanDesktopContext({
        mode: "desktop",
        apiBaseUrl: "http://127.0.0.1:51234",
        launchToken: "tok-12345678901234567890123456789012",
        saveFile: async (payload) => { nativeSaves.push(payload.filename); return saveHookReturn; },
    });

    // 1. Empty state displays cleanly
    mockBackups = [];
    await ctx.loadBackupManagerData();
    assert.ok(elements.get("bkp-mgr-tbody").innerHTML.includes("No backup files found yet"));
    assert.strictEqual(elements.get("bkp-runtime-summary").textContent, "0 verified / 0 total");

    // 2. Repeated renders register delegated listener ONLY ONCE + XSS escaping verified
    mockBackups = [{
        filename: 'safe_<img src=x onerror=alert(1)>.db',
        backup_type: "MANUAL",
        modified_at: "2026-11-17 16:00:00",
        size_display: "300.0 KB",
        integrity_ok: true,
        valid_for_restore: true,
    }];
    await ctx.loadBackupManagerData();
    await ctx.loadBackupManagerData();
    const tbody = elements.get("bkp-mgr-tbody");
    assert.strictEqual((tbody.listeners["click"] || []).length, 1, "Delegated click listener must not duplicate");
    assert.ok(!tbody.innerHTML.includes("<img"), "Filename HTML metacharacters must be escaped");
    assert.ok(tbody.innerHTML.includes("&lt;img"));

    // 3. Open restore modal, then CANCEL -> modal closes, checkbox reset, zero restore POSTs
    const opened = await ctx.openBackupRestoreModal("agri_erp_backup_20261117.db");
    assert.strictEqual(opened, true);
    assert.strictEqual(elements.get("modal-backup-restore-confirm").style.display, "flex");
    assert.strictEqual(elements.get("btn-confirm-backup-restore").disabled, true);
    elements.get("bkp-restore-confirm-check").checked = true;
    ctx.onBackupRestoreConfirmToggle();
    assert.strictEqual(elements.get("btn-confirm-backup-restore").disabled, false);

    ctx.closeBackupRestoreModal();
    assert.strictEqual(elements.get("modal-backup-restore-confirm").style.display, "none");
    assert.strictEqual(elements.get("bkp-restore-confirm-check").checked, false);
    assert.strictEqual(elements.get("btn-confirm-backup-restore").disabled, true);
    assert.strictEqual(fetchCalls.some((c) => c.url.includes("/backup/restore")), false);

    // 4. Native Tauri export dialog success & user cancellation handling
    saveHookReturn = { saved: false, canceled: true };
    const canceledExport = await ctx.downloadBackupFile("agri_erp_backup_20261117.db");
    assert.strictEqual(canceledExport, false);
    saveHookReturn = { saved: true, canceled: false, path: "/tmp/exported.db" };
    const okExport = await ctx.downloadBackupFile("agri_erp_backup_20261117.db");
    assert.strictEqual(okExport, true);
    assert.strictEqual(nativeSaves.length, 2);

    // 5. Operator role blocked from Backup Manager & actions without making API calls
    const beforeOpCalls = fetchCalls.length;
    vm.runInContext('currentUser = { user_id: 2, username: "op", full_name: "Operator", role: "OPERATOR" }; updateUserInterfaceForRole();', ctx);
    assert.strictEqual(elements.get("settings-backup-manager-card").style.display, "none");
    assert.strictEqual(elements.get("btn-header-quick-backup").style.display, "none");
    assert.strictEqual(elements.get("btn-header-backup-manager").style.display, "none");
    assert.strictEqual(ctx.openBackupManagerView(), false);
    assert.strictEqual(await ctx.triggerDatabaseBackup(), false);
    assert.strictEqual(await ctx.verifySingleBackup("agri_erp_backup_20261117.db"), null);
    assert.strictEqual(await ctx.downloadBackupFile("agri_erp_backup_20261117.db"), false);
    assert.strictEqual(await ctx.openBackupRestoreModal("agri_erp_backup_20261117.db"), false);
    assert.strictEqual(fetchCalls.length, beforeOpCalls, "Non-admin actions must not issue backup API requests");

    // 6. Logout confirmation dialog via #modal-app-confirm (Cancel keeps session, OK logs out)
    vm.runInContext('currentUser = { user_id: 1, username: "admin", full_name: "Shop Admin", role: "ADMIN" };', ctx);
    const pLogoutCancel = ctx.handleLogout();
    assert.strictEqual(elements.get("modal-app-confirm").style.display, "flex");
    elements.get("app-confirm-cancel").click();
    await pLogoutCancel;
    assert.strictEqual(storage.get("krushidhan_auth_token"), "jwt-admin-token");
    assert.strictEqual(elements.get("modal-app-confirm").style.display, "none");

    const pLogoutConfirm = ctx.handleLogout();
    assert.strictEqual(elements.get("modal-app-confirm").style.display, "flex");
    elements.get("app-confirm-ok").click();
    await pLogoutConfirm;
    assert.strictEqual(storage.has("krushidhan_auth_token"), false);
    assert.strictEqual(elements.get("modal-app-confirm").style.display, "none");

    console.log("PHASE_1A_NODE_OK");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
"""

    env = os.environ.copy()
    env["APP_JS_PATH"] = str(APP_JS_PATH)
    env["INDEX_HTML_PATH"] = str(INDEX_HTML_PATH)
    proc = subprocess.run(
        [node_bin, "-e", script],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=20.0,
    )
    assert proc.returncode == 0, f"Node runtime test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "PHASE_1A_NODE_OK" in proc.stdout
