"""
Automated Verification Suite for STEP 4B-2:
Desktop Frontend Experience, Zero Blocking Browser Dialogs,
Reliable Desktop File Downloads, Sidecar Dynamic Runtime Integration,
and Full Backup & Recovery Manager (Backend + Live Sidecar + Node.js JS Runtime).
"""
from __future__ import annotations

import http.client
import json
import os
import queue
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.config import APP_ID, APP_VERSION
from src.db.connection import DatabaseManager, get_db_manager, reset_db_manager
from src.desktop_runtime import LAUNCH_TOKEN_HEADER, reset_desktop_security
from src.services.auth_service import AuthService

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_JS_PATH = PROJECT_ROOT / "src" / "web" / "static" / "js" / "app.js"
INDEX_HTML_PATH = PROJECT_ROOT / "src" / "web" / "templates" / "index.html"
APP_CSS_PATH = PROJECT_ROOT / "src" / "web" / "static" / "css" / "app.css"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _multipart_file_body(
    field_name: str,
    filename: str,
    payload: bytes,
    content_type: str = "application/vnd.sqlite3",
    extra_fields: Optional[Dict[str, str]] = None,
) -> Tuple[bytes, str]:
    boundary = f"----KrushidhanStep4B2Boundary{secrets.token_hex(8)}"
    parts = []
    for k, v in (extra_fields or {}).items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"),
                f"{v}\r\n".encode("utf-8"),
            ]
        )
    parts.extend(
        [
            f"--{boundary}\r\n".encode("ascii"),
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode(
                "utf-8"
            ),
            f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
            payload,
            f"\r\n--{boundary}--\r\n".encode("ascii"),
        ]
    )
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _raw_http_request(
    port: int,
    method: str,
    path: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = 12.0,
) -> Tuple[int, Dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=dict(headers or {}))
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, raw
    finally:
        conn.close()


def _read_first_line_with_timeout(stream, timeout: float = 45.0) -> str:
    q: queue.Queue = queue.Queue()

    def _reader() -> None:
        try:
            q.put(("line", stream.readline()))
        except Exception as exc:
            q.put(("err", exc))

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    kind, val = q.get(timeout=timeout)
    if kind == "err":
        raise val
    return val


def _terminate_proc_safely(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        proc.wait(timeout=6.0)
        return
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=4.0)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2.0)
        except Exception:
            pass


def _setup_isolated_env_with_users(tmp_path: Path, monkeypatch) -> Dict[str, Any]:
    reset_desktop_security()
    reset_db_manager()

    db_file = tmp_path / "isolated_erp.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("JWT_SECRET", "step4b2-test-secret-0123456789abcdef0123456789abcdef")

    db = DatabaseManager(db_path=db_file, app_mode="local", backup_dir=backup_dir)
    db.initialize_database(include_seed=True, clean_install=False)

    # Ensure shop profile has recognizable shop_name for backup inspect tests
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            "UPDATE company_settings SET company_name = 'Krushidhan Step4B2 Seeds & Agro' WHERE setting_id = 1"
        )
        conn.commit()
    finally:
        conn.close()

    auth = AuthService(db)
    admin_user = auth.authenticate_user("admin", "krushidhan@2026")
    if admin_user is None:
        admin_user = auth.setup_initial_admin(
            username="bkp_admin",
            plain_password="AdminStrongPassword#1",
            full_name="Backup Admin",
        )
    op_uid = auth.register_user(
        username="bkp_operator",
        plain_password="OperatorStrongPassword#1",
        full_name="Counter Operator",
        role="OPERATOR",
    )
    op_user = auth.authenticate_user("bkp_operator", "OperatorStrongPassword#1")
    assert admin_user is not None and op_user is not None and op_uid > 0

    admin_token = AuthService.create_access_token(
        user_id=admin_user.user_id,
        username=admin_user.username,
        full_name=admin_user.full_name,
        role=admin_user.role,
    )
    op_token = AuthService.create_access_token(
        user_id=op_user.user_id,
        username=op_user.username,
        full_name=op_user.full_name,
        role=op_user.role,
    )
    return {
        "db_file": db_file,
        "backup_dir": backup_dir,
        "admin_token": admin_token,
        "operator_token": op_token,
    }


# ---------------------------------------------------------------------------
# 1. Backend Backup Manager API, Upload Inspection & Restore Rollback
# ---------------------------------------------------------------------------
def test_backup_manager_api_create_list_inspect_download_and_restore(tmp_path, monkeypatch):
    env_info = _setup_isolated_env_with_users(tmp_path, monkeypatch)
    admin_hdr = {"Authorization": f"Bearer {env_info['admin_token']}"}
    op_hdr = {"Authorization": f"Bearer {env_info['operator_token']}"}
    backup_dir: Path = env_info["backup_dir"]

    try:
        with TestClient(app) as client:
            # 1. Create verified baseline manual backup
            res_create = client.post("/api/system/backup", headers=admin_hdr)
            assert res_create.status_code == 200
            created = res_create.json()
            assert created["success"] is True
            baseline_name = created["filename"]
            assert baseline_name.endswith(".db")
            assert created["size_bytes"] > 0
            assert created["size_display"]
            baseline_path = backup_dir / baseline_name
            assert baseline_path.is_file()
            # Clean standalone .db: no leftover -wal/-shm sidecars in BACKUP_DIR
            assert not (backup_dir / f"{baseline_name}-wal").exists()
            assert not (backup_dir / f"{baseline_name}-shm").exists()

            # 2. Noise files & stale DB backup_log rows must NOT fool /api/system/backups
            (backup_dir / "ignore_temp.db.tmp").write_bytes(b"temp")
            (backup_dir / "notes.txt").write_text("not a sqlite backup", encoding="utf-8")
            corrupt_path = backup_dir / "corrupted_bad.db"
            corrupt_path.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)

            non_erp_path = backup_dir / "foreign_empty.db"
            c_foreign = sqlite3.connect(str(non_erp_path))
            c_foreign.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
            c_foreign.commit()
            c_foreign.close()

            conn = sqlite3.connect(str(env_info["db_file"]))
            try:
                conn.execute(
                    "INSERT INTO backup_log (backup_type, file_path, status) VALUES ('MANUAL', ?, 'SUCCESS')",
                    (str(backup_dir / "deleted_missing_file.db"),),
                )
                conn.commit()
            finally:
                conn.close()

            res_list = client.get("/api/system/backups", headers=admin_hdr)
            assert res_list.status_code == 200
            list_payload = res_list.json()
            assert list_payload["backup_dir"] == str(backup_dir)
            filenames = [b["filename"] for b in list_payload["backups"]]
            assert baseline_name in filenames
            assert "corrupted_bad.db" in filenames
            assert "foreign_empty.db" in filenames
            assert "ignore_temp.db.tmp" not in filenames
            assert "notes.txt" not in filenames
            assert "deleted_missing_file.db" not in filenames

            by_name = {b["filename"]: b for b in list_payload["backups"]}
            assert by_name[baseline_name]["integrity_ok"] is True
            assert by_name[baseline_name]["valid_for_restore"] is True
            assert by_name[baseline_name]["backup_type"] == "MANUAL"
            assert by_name["corrupted_bad.db"]["integrity_ok"] is False
            assert by_name["corrupted_bad.db"]["valid_for_restore"] is False
            assert by_name["foreign_empty.db"]["integrity_ok"] is True
            assert by_name["foreign_empty.db"]["valid_for_restore"] is False

            # 3. Inspect endpoint on healthy, foreign, corrupt, missing, and path traversal
            res_insp = client.get(f"/api/system/backups/{baseline_name}/inspect", headers=admin_hdr)
            assert res_insp.status_code == 200
            insp = res_insp.json()
            assert insp["integrity_ok"] is True
            assert insp["foreign_key_ok"] is True
            assert insp["foreign_key_violation_count"] == 0
            assert insp["has_required_tables"] is True
            assert insp["valid_for_restore"] is True
            assert insp["schema_version"] >= 2
            assert insp["table_count"] >= 10
            assert insp["shop_name"] == "Krushidhan Step4B2 Seeds & Agro"

            res_insp_foreign = client.get("/api/system/backups/foreign_empty.db/inspect", headers=admin_hdr)
            assert res_insp_foreign.status_code == 200
            assert res_insp_foreign.json()["integrity_ok"] is True
            assert res_insp_foreign.json()["has_required_tables"] is False
            assert res_insp_foreign.json()["valid_for_restore"] is False

            res_insp_corrupt = client.get("/api/system/backups/corrupted_bad.db/inspect", headers=admin_hdr)
            assert res_insp_corrupt.status_code == 200
            assert res_insp_corrupt.json()["integrity_ok"] is False
            assert res_insp_corrupt.json()["valid_for_restore"] is False

            assert client.get("/api/system/backups/missing_file.db/inspect", headers=admin_hdr).status_code == 404
            for bad_name in ("..%2Fx.db", "..\\x.db", "sub/x.db", "bad.txt"):
                st_trav = client.get(f"/api/system/backups/{bad_name}/inspect", headers=admin_hdr).status_code
                assert st_trav in (400, 404)

            # 4. Download endpoint (verified bytes + security headers + reject corrupt)
            res_dl = client.get(f"/api/system/backups/{baseline_name}/download", headers=admin_hdr)
            assert res_dl.status_code == 200
            assert res_dl.headers.get("x-content-type-options") == "nosniff"
            assert res_dl.headers.get("cache-control") == "no-store"
            assert f'filename="{baseline_name}"' in res_dl.headers.get("content-disposition", "")
            assert res_dl.content == baseline_path.read_bytes()

            assert (
                client.get("/api/system/backups/corrupted_bad.db/download", headers=admin_hdr).status_code
                == 400
            )

            # 5. Mutate database after baseline backup, then restore & confirm rollback + BEFORE_RESTORE snapshot
            res_add = client.post(
                "/api/masters/products",
                headers=admin_hdr,
                json={
                    "product_name": "Post-Backup Ephemeral Spray",
                    "category_id": 1,
                    "unit_id": 2,
                    "tax_group_id": 2,
                    "hsn_code": "3808",
                    "default_purchase_rate": 100.0,
                    "default_sale_rate": 120.0,
                    "default_mrp": 125.0,
                },
            )
            assert res_add.status_code == 200
            added_id = res_add.json()["product_id"]
            names_before_restore = [
                p["product_name"] for p in client.get("/api/masters/products", headers=admin_hdr).json()
            ]
            assert "Post-Backup Ephemeral Spray" in names_before_restore

            # Reject restore without explicit confirmation
            res_unconfirmed = client.post(
                "/api/system/backup/restore",
                headers=admin_hdr,
                json={"backup_filename": baseline_name, "confirm_restore": False},
            )
            assert res_unconfirmed.status_code == 400

            # Reject corrupt or non-ERP restores even with confirmation
            assert (
                client.post(
                    "/api/system/backup/restore",
                    headers=admin_hdr,
                    json={"backup_filename": "corrupted_bad.db", "confirm_restore": True},
                ).status_code
                == 400
            )
            assert (
                client.post(
                    "/api/system/backup/restore",
                    headers=admin_hdr,
                    json={"backup_filename": "foreign_empty.db", "confirm_restore": True},
                ).status_code
                == 400
            )

            # Confirmed restore from baseline backup
            res_restore = client.post(
                "/api/system/backup/restore",
                headers=admin_hdr,
                json={"backup_filename": baseline_name, "confirm_restore": True},
            )
            assert res_restore.status_code == 200
            restore_json = res_restore.json()
            assert restore_json["success"] is True
            assert restore_json["restored_from"] == baseline_name
            pre_restore_filename = restore_json["pre_restore_filename"]
            assert pre_restore_filename and "before_restore" in pre_restore_filename.lower()
            pre_restore_path = backup_dir / pre_restore_filename
            assert pre_restore_path.is_file()

            # Ephemeral product must be gone from live DB after restore
            names_after_restore = [
                p["product_name"] for p in client.get("/api/masters/products", headers=admin_hdr).json()
            ]
            assert "Post-Backup Ephemeral Spray" not in names_after_restore

            # 6. External .db inspect-upload & restore-upload (restoring from exported BEFORE_RESTORE copy!)
            external_copy = tmp_path / "usb_drive_copy.db"
            shutil.copy2(pre_restore_path, external_copy)
            ext_bytes = external_copy.read_bytes()

            mp_insp, ct_insp = _multipart_file_body("file", "usb_drive_copy.db", ext_bytes)
            res_ext_insp = client.post(
                "/api/system/backups/inspect-upload",
                headers={**admin_hdr, "Content-Type": ct_insp},
                content=mp_insp,
            )
            assert res_ext_insp.status_code == 200
            ext_info = res_ext_insp.json()
            assert ext_info["filename"] == "usb_drive_copy.db"
            assert ext_info["valid_for_restore"] is True
            assert ext_info["has_required_tables"] is True

            # Reject bad extension, non-SQLite magic, and unconfirmed upload restore
            bad_ext_mp, bad_ext_ct = _multipart_file_body("file", "evil.txt", ext_bytes)
            assert (
                client.post(
                    "/api/system/backups/inspect-upload",
                    headers={**admin_hdr, "Content-Type": bad_ext_ct},
                    content=bad_ext_mp,
                ).status_code
                == 400
            )
            fake_db_mp, fake_db_ct = _multipart_file_body("file", "fake.db", b"NOT A SQLITE FILE" * 20)
            assert (
                client.post(
                    "/api/system/backups/inspect-upload",
                    headers={**admin_hdr, "Content-Type": fake_db_ct},
                    content=fake_db_mp,
                ).status_code
                == 400
            )
            unconf_up_mp, unconf_up_ct = _multipart_file_body(
                "file", "usb_drive_copy.db", ext_bytes, extra_fields={"confirm_restore": "false"}
            )
            assert (
                client.post(
                    "/api/system/backup/restore-upload",
                    headers={**admin_hdr, "Content-Type": unconf_up_ct},
                    content=unconf_up_mp,
                ).status_code
                == 400
            )
            bad_up_mp, bad_up_ct = _multipart_file_body(
                "file", "corrupted_ext.db", corrupt_path.read_bytes(), extra_fields={"confirm_restore": "true"}
            )
            assert (
                client.post(
                    "/api/system/backup/restore-upload",
                    headers={**admin_hdr, "Content-Type": bad_up_ct},
                    content=bad_up_mp,
                ).status_code
                == 400
            )

            # Confirmed upload restore brings back the post-backup product cleanly!
            conf_up_mp, conf_up_ct = _multipart_file_body(
                "file", "usb_drive_copy.db", ext_bytes, extra_fields={"confirm_restore": "true"}
            )
            res_up_restore = client.post(
                "/api/system/backup/restore-upload",
                headers={**admin_hdr, "Content-Type": conf_up_ct},
                content=conf_up_mp,
            )
            assert res_up_restore.status_code == 200
            assert res_up_restore.json()["success"] is True
            names_after_up = [
                p["product_name"] for p in client.get("/api/masters/products", headers=admin_hdr).json()
            ]
            assert "Post-Backup Ephemeral Spray" in names_after_up
            assert added_id > 0

            # No leftover staging files in BACKUP_DIR
            leftover_staging = [p.name for p in backup_dir.iterdir() if p.name.startswith(".upload_inspect_")]
            assert leftover_staging == []

            # 7. Strict RBAC (Operator = 403, Unauthenticated = 401)
            protected_gets = [
                "/api/system/backups",
                f"/api/system/backups/{baseline_name}/inspect",
                f"/api/system/backups/{baseline_name}/download",
                "/api/system/runtime-info",
            ]
            for path in protected_gets:
                assert client.get(path).status_code == 401
                assert client.get(path, headers=op_hdr).status_code == 403

            assert client.post("/api/system/backup").status_code == 401
            assert client.post("/api/system/backup", headers=op_hdr).status_code == 403
            assert (
                client.post(
                    "/api/system/backup/restore",
                    headers=op_hdr,
                    json={"backup_filename": baseline_name, "confirm_restore": True},
                ).status_code
                == 403
            )
    finally:
        reset_desktop_security()
        reset_db_manager()


# ---------------------------------------------------------------------------
# 2. Live Sidecar Subprocess E2E Backup Manager + Exports + Restore Workflow
# ---------------------------------------------------------------------------
def test_live_sidecar_backup_manager_export_and_restore_e2e(tmp_path):
    app_data_root = tmp_path / "Sidecar4B2LocalAppData"
    env = os.environ.copy()
    for var in ("DB_PATH", "BACKUP_DIR", "JWT_SECRET", "PORT", "HOST"):
        env.pop(var, None)
    env["LOCALAPPDATA"] = str(app_data_root)
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    proc = subprocess.Popen(
        [sys.executable, "-m", "src.desktop_sidecar"],
        cwd=str(tmp_path),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        hs_line = _read_first_line_with_timeout(proc.stdout, timeout=45.0)
        handshake = json.loads(hs_line.strip())
        assert handshake["status"] == "ready"
        port = int(handshake["port"])
        launch_token = handshake["launch_token"]

        sidecar_hdr = {
            "Origin": "http://tauri.localhost",
            LAUNCH_TOKEN_HEADER: launch_token,
        }

        # First-run setup via sidecar
        owner_pw = "SidecarBackup#2026"
        st_setup, _, raw_setup = _raw_http_request(
            port,
            "POST",
            "/api/auth/setup-admin",
            headers={**sidecar_hdr, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "username": "sahyadri_admin",
                    "password": owner_pw,
                    "full_name": "Ramesh Deshmukh",
                    "shop_name": "Sahyadri Krishi Kendra",
                    "mobile": "9811223344",
                }
            ).encode("utf-8"),
        )
        assert st_setup == 200
        jwt = json.loads(raw_setup.decode("utf-8"))["token"]
        api_hdr = {**sidecar_hdr, "Authorization": f"Bearer {jwt}"}

        # Seed initial product -> create baseline backup
        st_p1, _, _ = _raw_http_request(
            port,
            "POST",
            "/api/masters/products",
            headers={**api_hdr, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "product_name": "Baseline Sidecar DAP",
                    "category_id": 1,
                    "unit_id": 2,
                    "tax_group_id": 2,
                    "hsn_code": "3105",
                    "default_purchase_rate": 1200.0,
                    "default_sale_rate": 1350.0,
                    "default_mrp": 1350.0,
                }
            ).encode("utf-8"),
        )
        assert st_p1 == 200

        st_bkp, _, raw_bkp = _raw_http_request(port, "POST", "/api/system/backup", headers=api_hdr)
        assert st_bkp == 200
        bkp_name = json.loads(raw_bkp.decode("utf-8"))["filename"]

        # Verify list, inspect, and download through live desktop sidecar CORS headers
        st_list, _, raw_list = _raw_http_request(port, "GET", "/api/system/backups", headers=api_hdr)
        assert st_list == 200
        assert any(b["filename"] == bkp_name for b in json.loads(raw_list.decode("utf-8"))["backups"])

        st_insp, _, raw_insp = _raw_http_request(
            port, "GET", f"/api/system/backups/{bkp_name}/inspect", headers=api_hdr
        )
        assert st_insp == 200
        insp_data = json.loads(raw_insp.decode("utf-8"))
        assert insp_data["valid_for_restore"] is True
        assert insp_data["shop_name"] == "Sahyadri Krishi Kendra"

        st_dl, hdr_dl, raw_dl = _raw_http_request(
            port, "GET", f"/api/system/backups/{bkp_name}/download", headers=api_hdr
        )
        assert st_dl == 200
        assert "Content-Disposition" in hdr_dl.get("access-control-expose-headers", "")
        assert raw_dl.startswith(b"SQLite format 3\x00")

        # Add post-backup product -> confirm present -> restore baseline -> confirm rolled back
        st_p2, _, _ = _raw_http_request(
            port,
            "POST",
            "/api/masters/products",
            headers={**api_hdr, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "product_name": "Temporary Post-Backup Product",
                    "category_id": 1,
                    "unit_id": 2,
                    "tax_group_id": 2,
                    "hsn_code": "3102",
                    "default_purchase_rate": 240.0,
                    "default_sale_rate": 266.0,
                    "default_mrp": 266.0,
                }
            ).encode("utf-8"),
        )
        assert st_p2 == 200

        st_rest, _, raw_rest = _raw_http_request(
            port,
            "POST",
            "/api/system/backup/restore",
            headers={**api_hdr, "Content-Type": "application/json"},
            body=json.dumps({"backup_filename": bkp_name, "confirm_restore": True}).encode("utf-8"),
        )
        assert st_rest == 200
        assert "before_restore" in json.loads(raw_rest.decode("utf-8"))["pre_restore_filename"].lower()

        st_prods, _, raw_prods = _raw_http_request(port, "GET", "/api/masters/products", headers=api_hdr)
        assert st_prods == 200
        prod_names = [p["product_name"] for p in json.loads(raw_prods.decode("utf-8"))]
        assert "Baseline Sidecar DAP" in prod_names
        assert "Temporary Post-Backup Product" not in prod_names

        # Verify Excel export continues cleanly after restore
        st_xls, hdr_xls, raw_xls = _raw_http_request(
            port, "GET", "/api/excel/exports/products", headers=api_hdr
        )
        assert st_xls == 200
        assert raw_xls.startswith(b"PK\x03\x04")
        assert ".xlsx" in hdr_xls.get("content-disposition", "")

        proc.stdin.close()
        assert proc.wait(timeout=10.0) == 0

        log_text = (app_data_root / APP_ID / "logs" / "sidecar.log").read_text(encoding="utf-8")
        assert launch_token not in log_text
        assert jwt not in log_text
        assert owner_pw not in log_text
    finally:
        _terminate_proc_safely(proc)


# ---------------------------------------------------------------------------
# 3. Frontend Static Contract + Real Node.js Runtime Behavioral Tests
# ---------------------------------------------------------------------------
def test_frontend_desktop_ux_static_contracts():
    app_js = APP_JS_PATH.read_text(encoding="utf-8")
    index_html = INDEX_HTML_PATH.read_text(encoding="utf-8")
    app_css = APP_CSS_PATH.read_text(encoding="utf-8")

    # Zero blocking dialogs or popup windows anywhere in JS/HTML
    for label, text in (("app.js", app_js), ("index.html", index_html)):
        for pat in (r"\balert\s*\(", r"\bconfirm\s*\(", r"\bprompt\s*\(", r"window\.open\s*\("):
            matches = re.findall(pat, text)
            assert not matches, f"Found forbidden blocking browser dialog pattern {pat} in {label}: {matches}"

    # Preserve strict zero Devanagari & zero hardcoded 8008 across src/
    for src_file in (PROJECT_ROOT / "src").rglob("*"):
        if src_file.is_file() and src_file.suffix in {".py", ".js", ".html", ".css"}:
            body = src_file.read_text(encoding="utf-8", errors="ignore")
            assert not re.search(r"[\u0900-\u097F]", body), f"Devanagari character leaked into {src_file}"
            assert "8008" not in body, f"Hardcoded port 8008 leaked into {src_file}"

    # Required Backup Manager & Custom Dialog markup in index.html
    for required_id in (
        'id="btn-header-quick-backup"',
        'id="btn-header-backup-manager"',
        'id="btn-drawer-quick-backup"',
        'id="btn-drawer-backup-manager"',
        'id="settings-backup-manager-card"',
        'id="bkp-mgr-tbody"',
        'id="bkp-external-file-input"',
        'id="modal-backup-restore-confirm"',
        'id="bkp-restore-confirm-check"',
        'id="btn-confirm-backup-restore"',
        'id="modal-app-confirm"',
        'id="modal-app-input"',
        'id="krushidhan-toast-container"',
    ):
        assert required_id in index_html, f"Missing required Step 4B-2 element {required_id}"

    # Required Desktop UX / Toast / Modal / Backup CSS classes in app.css
    for css_marker in (
        ".btn-outline-primary",
        ".btn-outline-danger",
        ".modal-backdrop-priority",
        ".modal-backdrop-top",
        ".toast-container",
        ".krushidhan-toast",
        ".bkp-runtime-strip",
        ".bkp-feedback-banner",
        ".bkp-inspect-grid",
    ):
        assert css_marker in app_css, f"Missing required CSS selector {css_marker}"


def test_app_js_runtime_desktop_bridge_dialogs_downloads_and_backup_manager():
    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js is required to run app.js runtime behavioral tests"

    node_harness = r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const appJsCode = fs.readFileSync(process.env.APP_JS_PATH, "utf-8");

class FakeElement {
    constructor(id = "", tagName = "div") {
        this.id = id;
        this.tagName = tagName.toUpperCase();
        this.style = {};
        this.dataset = {};
        this.children = [];
        this.parentNode = null;
        this.textContent = "";
        this.innerText = "";
        this._innerHTML = "";
        this.value = "";
        this.checked = false;
        this.disabled = false;
        this.type = "text";
        this.placeholder = "";
        this.options = [];
        this.selectedIndex = -1;
        this.listeners = {};
        this.attrs = {};
        this.clicked = 0;
        const clsSet = new Set();
        this.classList = {
            add: (...c) => c.forEach((x) => clsSet.add(x)),
            remove: (...c) => c.forEach((x) => clsSet.delete(x)),
            contains: (c) => clsSet.has(c),
        };
    }
    set className(val) {
        this._className = val;
    }
    get className() {
        return this._className || "";
    }
    set innerHTML(val) {
        this._innerHTML = String(val);
    }
    get innerHTML() {
        return this._innerHTML;
    }
    get firstChild() {
        return this.children[0] || null;
    }
    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }
    removeChild(child) {
        this.children = this.children.filter((c) => c !== child);
        child.parentNode = null;
        return child;
    }
    addEventListener(type, fn) {
        if (!this.listeners[type]) this.listeners[type] = [];
        this.listeners[type].push(fn);
    }
    removeEventListener(type, fn) {
        if (!this.listeners[type]) return;
        this.listeners[type] = this.listeners[type].filter((f) => f !== fn);
    }
    dispatchEvent(type, ev = {}) {
        const list = (this.listeners[type] || []).slice();
        const eventObj = { target: this, preventDefault: () => {}, ...ev };
        for (const fn of list) fn(eventObj);
    }
    setAttribute(k, v) {
        this.attrs[k] = String(v);
    }
    getAttribute(k) {
        return k in this.attrs ? this.attrs[k] : null;
    }
    focus() {}
    scrollIntoView() {}
    click() {
        this.clicked++;
        this.dispatchEvent("click", {});
    }
    querySelectorAll() {
        return [];
    }
}

const elements = new Map();
function ensureEl(id, tagName = "div") {
    if (!elements.has(id)) {
        elements.set(id, new FakeElement(id, tagName));
    }
    return elements.get(id);
}

const htmlText = fs.readFileSync(process.env.INDEX_HTML_PATH, "utf-8");
for (const m of htmlText.matchAll(/id="([^"]+)"/g)) {
    ensureEl(m[1]);
}
["krushidhan-toast-container"].forEach((id) => ensureEl(id));

const createdElements = [];
const windowListeners = {};
const docListeners = {};
const storage = new Map();
const fetchLog = [];
const revokedUrls = [];
let nextBlobIdx = 1;
const createdBlobUrls = [];
let forceMaintenance503 = false;
let forceMissingLaunchTokenOnce = false;
let nativeSavedFiles = [];

const fakeDocument = {
    body: new FakeElement("body", "body"),
    hidden: false,
    getElementById: (id) => elements.get(id) || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag) => {
        const el = new FakeElement("", tag);
        createdElements.push(el);
        return el;
    },
    addEventListener: (type, fn) => {
        if (!docListeners[type]) docListeners[type] = [];
        docListeners[type].push(fn);
    },
};

function makeHeaders(obj) {
    const map = {};
    Object.entries(obj || {}).forEach(([k, v]) => {
        map[k.toLowerCase()] = String(v);
    });
    return {
        get: (k) => map[String(k).toLowerCase()] || null,
    };
}

function makeResponse(status, jsonBody, headersObj = {}, rawBytes = null) {
    return {
        ok: status >= 200 && status < 300,
        status,
        headers: makeHeaders(headersObj),
        clone: () => makeResponse(status, jsonBody, headersObj, rawBytes),
        json: async () => jsonBody,
        blob: async () => ({
            type: headersObj["Content-Type"] || "application/octet-stream",
            size: rawBytes ? rawBytes.length : 10,
            arrayBuffer: async () => (rawBytes ? rawBytes.buffer.slice(rawBytes.byteOffset, rawBytes.byteOffset + rawBytes.byteLength) : new ArrayBuffer(4)),
        }),
    };
}

const fakeWindow = {
    location: { origin: "http://tauri.localhost" },
    addEventListener: (type, fn) => {
        if (!windowListeners[type]) windowListeners[type] = [];
        windowListeners[type].push(fn);
    },
    removeEventListener: (type, fn) => {
        if (!windowListeners[type]) return;
        windowListeners[type] = windowListeners[type].filter((f) => f !== fn);
    },
    dispatchEvent: (ev) => {
        const list = (windowListeners[ev.type] || []).slice();
        for (const fn of list) fn(ev);
    },
    fetch: async (url, opts = {}) => {
        fetchLog.push({ url: String(url), opts });
        const headers = opts.headers || {};
        const hasLaunch = Boolean(headers["X-Krushidhan-Launch-Token"]);
        if (forceMissingLaunchTokenOnce) {
            forceMissingLaunchTokenOnce = false;
            return makeResponse(401, { detail: "Missing X-Krushidhan-Launch-Token header" });
        }
        if (String(url).startsWith("http://127.0.0.1:54321") && !hasLaunch) {
            return makeResponse(401, { detail: "Missing X-Krushidhan-Launch-Token header" });
        }
        if (forceMaintenance503) {
            return makeResponse(503, { detail: "Database maintenance in progress" }, { "Retry-After": "2" });
        }
        if (String(url).endsWith("/api/auth/setup-status")) {
            return makeResponse(200, { needs_setup: false, shop_name: "Test Shop", has_company_profile: true });
        }
        if (String(url).endsWith("/api/auth/login")) {
            return makeResponse(200, {
                success: true,
                token: "jwt-admin-abc123",
                user: { user_id: 1, username: "admin", full_name: "Shop Admin", role: "ADMIN" },
            });
        }
        if (String(url).endsWith("/api/auth/me")) {
            return makeResponse(200, {
                user: { user_id: 1, username: "admin", full_name: "Shop Admin", role: "ADMIN" },
            });
        }
        if (String(url).endsWith("/api/system/runtime-info")) {
            return makeResponse(200, {
                app_mode: "desktop",
                app_version: "1.0.0",
                backup_dir: "C:\\\\Users\\\\Shop\\\\AppData\\\\Local\\\\com.krushidhan.erp\\\\backups",
            });
        }
        if (String(url).endsWith("/api/system/backups")) {
            return makeResponse(200, {
                backup_dir: "C:\\\\Backups",
                backups: [
                    {
                        filename: "krushidhan_MANUAL_20261111_090000.db",
                        backup_type: "MANUAL",
                        modified_at: "2026-11-11 09:00:00",
                        size_bytes: 65536,
                        size_display: "64.0 KB",
                        integrity_ok: true,
                        valid_for_restore: true,
                    },
                ],
            });
        }
        if (String(url).endsWith("/inspect")) {
            return makeResponse(200, {
                filename: "krushidhan_MANUAL_20261111_090000.db",
                modified_at: "2026-11-11 09:00:00",
                size_display: "64.0 KB",
                integrity_ok: true,
                foreign_key_ok: true,
                foreign_key_violation_count: 0,
                schema_version: 2,
                table_count: 14,
                shop_name: "Test Shop",
                valid_for_restore: true,
            });
        }
        if (String(url).endsWith("/download")) {
            return makeResponse(
                200,
                {},
                {
                    "Content-Type": "application/vnd.sqlite3",
                    "Content-Disposition": "attachment; filename*=UTF-8''krushidhan_MANUAL_20261111_090000.db",
                },
                new Uint8Array([0x53, 0x51, 0x4c, 0x69, 0x74, 0x65])
            );
        }
        if (String(url).endsWith("/api/system/backup/restore")) {
            return makeResponse(200, {
                success: true,
                restored_from: "krushidhan_MANUAL_20261111_090000.db",
                pre_restore_filename: "krushidhan_BEFORE_RESTORE_20261111_090500.db",
            });
        }
        if (String(url).includes("/pdf")) {
            return makeResponse(
                200,
                {},
                {
                    "Content-Type": "application/pdf",
                    "Content-Disposition": 'attachment; filename="Invoice_INV-2026-0001.pdf"',
                },
                new Uint8Array([0x25, 0x50, 0x44, 0x46])
            );
        }
        if (String(url).includes("/api/excel/exports/")) {
            return makeResponse(
                200,
                {},
                {
                    "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "Content-Disposition": 'attachment; filename="Product_Catalog_2026.xlsx"',
                },
                new Uint8Array([0x50, 0x4b, 0x03, 0x04])
            );
        }
        if (String(url).endsWith("/api/masters/all")) {
            return makeResponse(200, { categories: [], manufacturers: [], units: [] });
        }
        if (String(url).endsWith("/api/system/settings")) {
            return makeResponse(200, { company_name: "Test Shop" });
        }
        if (String(url).endsWith("/api/sales/next-invoice-no")) {
            return makeResponse(200, { next_invoice_no: "INV-2026-0001" });
        }
        if (String(url).includes("/api/accounting/profit-and-loss")) {
            return makeResponse(200, { expenses_breakdown: [], category_breakdown: [] });
        }
        if (String(url).includes("/api/accounting/profit-and-loss/products")) {
            if (String(url).includes("/api/accounting/day-book")) {
            return makeResponse(200, {
                total_sales: 0, total_receipts: 0, total_expenses: 0,
                total_supplier_payments: 0, net_cash_flow: 0, transactions: [], entries: [],
            });
        }
        return makeResponse(200, []);
        }
        if (String(url).endsWith("/api/accounting/cash-closing-summary")) {
            return makeResponse(200, {
                opening_cash: 0, cash_sales: 0, farmer_cash_receipts: 0,
                cash_expenses: 0, cash_supplier_payments: 0, expected_drawer_cash: 0,
            });
        }
        if (String(url).includes("/api/accounting/day-book")) {
            return makeResponse(200, {
                total_sales: 0, total_receipts: 0, total_expenses: 0,
                total_supplier_payments: 0, net_cash_flow: 0, transactions: [], entries: [],
            });
        }
        return makeResponse(200, []);
    },
    __KRUSHIDHAN_DESKTOP__: {
        mode: "desktop",
        port: 54321,
        launchToken: "secret-launch-tok-987654",
    },
};

const context = {
    ...fakeWindow,
    document: fakeDocument,
    localStorage: {
        getItem: (k) => (storage.has(k) ? storage.get(k) : null),
        setItem: (k, v) => storage.set(k, String(v)),
        removeItem: (k) => storage.delete(k),
    },
    navigator: { onLine: true },
    Headers: Map,
    URL: {
        createObjectURL: () => {
            const u = `blob:tauri/${nextBlobIdx++}`;
            createdBlobUrls.push(u);
            return u;
        },
        revokeObjectURL: (u) => revokedUrls.push(u),
    },
    URLSearchParams,
    FormData: class FakeFormData {
        constructor() { this.fields = []; }
        append(k, v) { this.fields.push([k, v]); }
    },
    setTimeout: (fn, ms) => {
        if (ms <= 50) return global.setTimeout(fn, 1);
        return { unref() {} };
    },
    clearTimeout: (id) => global.clearTimeout(id),
    setInterval: () => 1,
    clearInterval: () => {},
    console,
};
// Match browser semantics: window === self === globalThis === context
context.window = context;
context.self = context;
context.globalThis = context;
context.alert = () => { throw new Error("Blocking alert() must never be called!"); };
context.confirm = () => { throw new Error("Blocking confirm() must never be called!"); };
context.prompt = () => { throw new Error("Blocking prompt() must never be called!"); };

vm.createContext(context);
vm.runInContext(appJsCode, context, { filename: "app.js" });

(async () => {
    // 1. Desktop runtime bridge init + launch token secrecy + DOMContentLoaded recovery
    await context.initDesktopRuntimeContext();
    const ctxSummary = context.getKrushidhanDesktopContext();
    assert.strictEqual(ctxSummary.isDesktop, true);
    assert.strictEqual(ctxSummary.apiBaseUrl, "http://127.0.0.1:54321");
    assert.strictEqual(ctxSummary.hasLaunchToken, true);
    assert.ok(!JSON.stringify(ctxSummary).includes("secret-launch-tok-987654"));
    assert.strictEqual(storage.has("launchToken"), false);
    assert.strictEqual(context.__KRUSHIDHAN_DESKTOP__.launchToken, "");

    // Trigger DOMContentLoaded before login with stale token; verify launch-token injection works
    for (const fn of (docListeners["DOMContentLoaded"] || [])) {
        await fn();
    }

    // 2. Auth flow routes through _rawDesktopFetch with launch token, without stale Bearer on setup/login
    storage.set("krushidhan_auth_token", "stale-jwt-should-not-hit-login");
    const statusData = await context.loadSetupStatus();
    assert.strictEqual(statusData.shop_name, "Test Shop");
    const setupCall = fetchLog.find((c) => c.url.endsWith("/api/auth/setup-status"));
    assert.strictEqual(setupCall.url, "http://127.0.0.1:54321/api/auth/setup-status");
    assert.strictEqual(setupCall.opts.headers["X-Krushidhan-Launch-Token"], "secret-launch-tok-987654");
    assert.strictEqual(setupCall.opts.headers["Authorization"], undefined);

    elements.get("login-username").value = "admin";
    elements.get("login-password").value = "pass123";
    await context.submitLogin();
    const loginCall = fetchLog.find((c) => c.url.endsWith("/api/auth/login"));
    assert.strictEqual(loginCall.url, "http://127.0.0.1:54321/api/auth/login");
    assert.strictEqual(loginCall.opts.headers["X-Krushidhan-Launch-Token"], "secret-launch-tok-987654");
    assert.strictEqual(storage.get("krushidhan_auth_token"), "jwt-admin-abc123");

    // Verify Admin backup controls visible, Operator hides them
    assert.strictEqual(elements.get("settings-backup-manager-card").style.display, "block");
    assert.strictEqual(elements.get("btn-header-backup-manager").style.display, "inline-flex");
    vm.runInContext('currentUser = { user_id: 2, username: "op", role: "OPERATOR" }; updateUserInterfaceForRole();', context);
    assert.strictEqual(elements.get("settings-backup-manager-card").style.display, "none");
    assert.strictEqual(elements.get("btn-header-backup-manager").style.display, "none");
    vm.runInContext('currentUser = { user_id: 1, username: "admin", role: "ADMIN" }; updateUserInterfaceForRole();', context);

    // 3. HTTP 503 maintenance toast without throwing or logging out
    forceMaintenance503 = true;
    const mRes = await context.fetch("/api/system/backups");
    assert.strictEqual(mRes.status, 503);
    assert.ok(elements.get("krushidhan-toast-container").children.length >= 1);
    forceMaintenance503 = false;

    // 4. Custom non-blocking confirm + input dialogs
    const pCancel = context.showConfirmDialog({ title: "Delete", message: "Sure?" });
    assert.strictEqual(elements.get("modal-app-confirm").style.display, "flex");
    elements.get("app-confirm-cancel").click();
    assert.strictEqual(await pCancel, false);
    assert.strictEqual(elements.get("modal-app-confirm").style.display, "none");

    const pAccept = context.showConfirmDialog({ title: "Delete", message: "Sure?", danger: true });
    elements.get("app-confirm-ok").click();
    assert.strictEqual(await pAccept, true);

    const pInput = context.showInputDialog({
        title: "Reset Password",
        minLength: 6,
        inputType: "password",
        shortMessage: "Too short!",
    });
    assert.strictEqual(elements.get("modal-app-input").style.display, "flex");
    elements.get("app-input-field").value = "abc";
    elements.get("app-input-form").dispatchEvent("submit", {});
    assert.strictEqual(elements.get("app-input-error").textContent, "Too short!");
    assert.strictEqual(elements.get("app-input-error").style.display, "block");
    elements.get("app-input-field").value = "validSecret123";
    elements.get("app-input-form").dispatchEvent("submit", {});
    assert.strictEqual(await pInput, "validSecret123");

    // 5. Downloads in WebView <a download> fallback mode + native desktop saveFile hook mode
    await context.downloadInvoicePdf(42);
    await context.downloadExcelExport("/api/excel/exports/products", "Fallback.xlsx");
    await context.downloadBackupFile("krushidhan_MANUAL_20261111_090000.db");
    const dlNames = createdElements.filter((e) => e.tagName === "A").map((e) => e.download);
    assert.deepStrictEqual(dlNames, [
        "Invoice_INV-2026-0001.pdf",
        "Product_Catalog_2026.xlsx",
        "krushidhan_MANUAL_20261111_090000.db",
    ]);
    assert.strictEqual(createdBlobUrls.length, 3);

    context.setKrushidhanDesktopContext({
        saveFile: async (payload) => nativeSavedFiles.push(payload.filename),
    });
    await context.downloadInvoicePdf(42);
    await context.downloadBackupFile("krushidhan_MANUAL_20261111_090000.db");
    assert.deepStrictEqual(nativeSavedFiles, [
        "Invoice_INV-2026-0001.pdf",
        "krushidhan_MANUAL_20261111_090000.db",
    ]);

    // 6. Backup Manager load, inspect modal checkbox gating, and confirmed restore
    const listData = await context.loadBackupManagerData();
    assert.strictEqual(listData.backups.length, 1);
    assert.ok(elements.get("bkp-mgr-tbody").innerHTML.includes("krushidhan_MANUAL_20261111_090000.db"));

    const prepared = await context.openBackupRestoreModal("krushidhan_MANUAL_20261111_090000.db");
    assert.strictEqual(prepared, true);
    assert.strictEqual(elements.get("modal-backup-restore-confirm").style.display, "flex");
    assert.strictEqual(elements.get("btn-confirm-backup-restore").disabled, true);
    assert.strictEqual(elements.get("bkp-confirm-integrity").textContent, "PASS (PRAGMA integrity_check = ok)");

    elements.get("bkp-restore-confirm-check").checked = true;
    context.onBackupRestoreConfirmToggle();
    assert.strictEqual(elements.get("btn-confirm-backup-restore").disabled, false);

    const restored = await context.executeConfirmedBackupRestore();
    assert.strictEqual(restored, true);
    assert.strictEqual(elements.get("modal-backup-restore-confirm").style.display, "none");
    assert.ok(elements.get("bkp-mgr-feedback").textContent.includes("krushidhan_BEFORE_RESTORE_20261111_090500.db"));

    // 7. Delayed krushidhan-desktop-ready recovery in a fresh session (DOMContentLoaded before runtime ready)
    const lateStorage = new Map([["krushidhan_auth_token", "valid-existing-jwt"]]);
    const lateDocListeners = {};
    const lateWinListeners = {};
    const lateFetchLog = [];
    const lateElements = new Map();
    for (const m of htmlText.matchAll(/id="([^"]+)"/g)) {
        lateElements.set(m[1], new FakeElement(m[1], "div"));
    }
    const lateCtx = {
        location: { origin: "http://tauri.localhost" },
        document: {
            body: new FakeElement("body", "body"),
            hidden: false,
            getElementById: (id) => lateElements.get(id) || null,
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: (tag) => new FakeElement("", tag),
            addEventListener: (t, fn) => {
                if (!lateDocListeners[t]) lateDocListeners[t] = [];
                lateDocListeners[t].push(fn);
            },
        },
        localStorage: {
            getItem: (k) => (lateStorage.has(k) ? lateStorage.get(k) : null),
            setItem: (k, v) => lateStorage.set(k, String(v)),
            removeItem: (k) => lateStorage.delete(k),
        },
        navigator: { onLine: true },
        Headers: Map,
        URL,
        URLSearchParams,
        FormData: class {},
        setTimeout: (fn, ms) => (ms <= 50 ? global.setTimeout(fn, 1) : { unref() {} }),
        clearTimeout: (id) => global.clearTimeout(id),
        setInterval: () => 1,
        clearInterval: () => {},
        console,
        addEventListener: (t, fn) => {
            if (!lateWinListeners[t]) lateWinListeners[t] = [];
            lateWinListeners[t].push(fn);
        },
        removeEventListener: () => {},
        dispatchEvent: (ev) => {
            for (const fn of (lateWinListeners[ev.type] || [])) fn(ev);
        },
        fetch: async (url, opts = {}) => {
            lateFetchLog.push({ url: String(url), opts });
            const hasLaunch = Boolean((opts.headers || {})["X-Krushidhan-Launch-Token"]);
            if (!hasLaunch) {
                return makeResponse(401, { detail: "Missing X-Krushidhan-Launch-Token header" });
            }
            if (String(url).endsWith("/api/auth/setup-status")) {
                return makeResponse(200, { needs_setup: false, shop_name: "Late Ready Shop" });
            }
            if (String(url).endsWith("/api/auth/me")) {
                return makeResponse(200, {
                    user: { user_id: 1, username: "admin", full_name: "Late Admin", role: "ADMIN" },
                });
            }
            if (String(url).endsWith("/api/masters/all")) {
                return makeResponse(200, { categories: [], manufacturers: [], units: [] });
            }
            if (String(url).endsWith("/api/system/backups")) {
                return makeResponse(200, { backup_dir: "C:\\\\Backups", backups: [] });
            }
            if (String(url).includes("/api/accounting/day-book")) {
                return makeResponse(200, { total_sales: 0, total_receipts: 0, total_expenses: 0, total_supplier_payments: 0, net_cash_flow: 0, entries: [] });
            }
            if (String(url).includes("/api/accounting/profit-and-loss")) {
                return makeResponse(200, { expenses_breakdown: [], category_breakdown: [] });
            }
            return makeResponse(200, []);
        },
    };
    lateCtx.window = lateCtx;
    lateCtx.self = lateCtx;
    lateCtx.globalThis = lateCtx;
    vm.createContext(lateCtx);
    vm.runInContext(appJsCode, lateCtx, { filename: "app.js" });
    for (const fn of (lateDocListeners["DOMContentLoaded"] || [])) {
        await fn();
    }
    // Before desktop runtime arrived, stored JWT must be preserved & user not yet authed
    assert.strictEqual(lateCtx.hasValidAuthSession(), false);
    assert.strictEqual(lateStorage.get("krushidhan_auth_token"), "valid-existing-jwt");
    // Dispatch krushidhan-desktop-ready -> recovers BOTH auth and dashboard bootstrap!
    lateCtx.dispatchEvent({
        type: "krushidhan-desktop-ready",
        detail: { apiBaseUrl: "http://127.0.0.1:59999", launchToken: "late-token-xyz123" },
    });
    await new Promise((resolve) => global.setTimeout(resolve, 40));
    assert.strictEqual(lateCtx.hasValidAuthSession(), true);
    assert.ok(lateFetchLog.some((c) => c.url === "http://127.0.0.1:59999/api/masters/all"));

    console.log("NODE_STEP4B2_OK");
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
"""

    env = os.environ.copy()
    env["APP_JS_PATH"] = str(APP_JS_PATH)
    env["INDEX_HTML_PATH"] = str(INDEX_HTML_PATH)
    proc = subprocess.run(
        [node_bin, "-e", node_harness],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=20.0,
    )
    assert proc.returncode == 0, f"Node runtime verification failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "NODE_STEP4B2_OK" in proc.stdout
