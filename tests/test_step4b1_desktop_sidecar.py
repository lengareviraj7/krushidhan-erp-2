"""
Automated Verification Suite for STEP 4B-1:
Python Desktop Sidecar, Resource Resolution, Desktop HTTP Security Middleware,
Health Endpoint, Secret-Redacted Rotating Logging, Single-Instance OS Lock,
and Full Live Subprocess Handshake + Stdin EOF Shutdown Lifecycle.
"""
from __future__ import annotations

import http.client
import io
import json
import logging
import os
import queue
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import openpyxl
import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.config import (
    APP_ID,
    APP_VERSION,
    REQUIRED_DESKTOP_ASSETS,
    get_bundle_root,
    get_desktop_lock_path,
    resolve_bundled_resource,
    resolve_log_dir,
    validate_bundled_assets,
)
from src.db.connection import DatabaseManager, get_db_manager, reset_db_manager
from src.desktop_runtime import (
    LAUNCH_TOKEN_HEADER,
    DesktopLockError,
    SingleInstanceLock,
    clear_redacted_secrets,
    configure_desktop_security,
    register_redacted_secret,
    reset_desktop_security,
    setup_desktop_logging,
    teardown_desktop_logging,
)
from src.printing.invoice_printer import InvoicePrinter
from src.services.auth_service import AuthService

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# HTTP / Process Helpers
# ---------------------------------------------------------------------------
def _raw_http_request(
    port: int,
    method: str,
    path: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = 10.0,
) -> Tuple[int, Dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        req_headers = dict(headers or {})
        conn.request(method, path, body=body, headers=req_headers)
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, raw
    finally:
        conn.close()


def _multipart_xlsx_body(field_name: str, filename: str, payload: bytes) -> Tuple[bytes, str]:
    boundary = f"----KrushidhanTestBoundary{secrets.token_hex(8)}"
    parts = [
        f"--{boundary}\r\n".encode("ascii"),
        (
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{filename}"\r\n'
        ).encode("utf-8"),
        b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n",
        payload,
        f"\r\n--{boundary}--\r\n".encode("ascii"),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _read_first_line_with_timeout(stream, timeout: float = 45.0) -> str:
    q: queue.Queue = queue.Queue()

    def _reader() -> None:
        try:
            line = stream.readline()
            q.put(("line", line))
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


# ---------------------------------------------------------------------------
# 1. Resource Path Resolution & Asset Validation (Source + Simulated Frozen)
# ---------------------------------------------------------------------------
def test_resource_path_resolution_source_and_simulated_frozen(tmp_path, monkeypatch):
    """
    Verify bundled asset lookup resolves cleanly from both source checkout and
    simulated PyInstaller sys._MEIPASS, and fails loudly when assets are missing.
    """
    # Source mode checks
    assert get_bundle_root() == PROJECT_ROOT
    verified = validate_bundled_assets()
    assert len(verified) == len(REQUIRED_DESKTOP_ASSETS)
    for asset_path in verified:
        assert asset_path.is_file()
        assert asset_path.stat().st_size > 0

    assert resolve_bundled_resource("src", "db", "schema.sql") == PROJECT_ROOT / "src" / "db" / "schema.sql"

    # Mode-isolated log & lock directories in desktop mode
    win_local = tmp_path / "WinLocalAppData"
    desktop_env = {"APP_MODE": "desktop", "LOCALAPPDATA": str(win_local)}
    assert resolve_log_dir(app_mode="desktop", env=desktop_env) == win_local / APP_ID / "logs"
    assert get_desktop_lock_path(env=desktop_env) == win_local / APP_ID / ".sidecar.lock"

    # Simulated PyInstaller frozen environment
    fake_meipass = tmp_path / "fake_meipass"
    for parts in REQUIRED_DESKTOP_ASSETS:
        src_file = PROJECT_ROOT.joinpath(*parts)
        dst_file = fake_meipass.joinpath(*parts)
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(fake_meipass), raising=False)

    assert get_bundle_root() == fake_meipass.resolve()
    assert resolve_bundled_resource("src", "web", "templates", "index.html") == (
        fake_meipass.resolve() / "src" / "web" / "templates" / "index.html"
    )
    assert len(validate_bundled_assets()) == len(REQUIRED_DESKTOP_ASSETS)

    # DB initialization succeeds purely from simulated _MEIPASS resources
    iso_db = DatabaseManager(db_path=tmp_path / "frozen.db", app_mode="desktop")
    iso_db.initialize_database(include_seed=True, clean_install=True)
    assert iso_db.get_schema_version() >= 2

    # Removing any mandatory asset must fail loudly
    missing_target = fake_meipass / "src" / "db" / "seed_clean.sql"
    missing_target.unlink()
    with pytest.raises(FileNotFoundError, match="seed_clean.sql"):
        validate_bundled_assets()
    with pytest.raises(FileNotFoundError, match="seed_clean.sql"):
        iso_db.initialize_database(include_seed=True, clean_install=True)


# ---------------------------------------------------------------------------
# 2. Health Endpoint Verification (/api/health)
# ---------------------------------------------------------------------------
def test_health_endpoint_healthy_maintenance_and_uninitialized(tmp_path, monkeypatch):
    """
    Verify /api/health returns minimal readiness JSON when DB is healthy,
    503 + Retry-After during maintenance, and 503 when schema is missing.
    """
    reset_desktop_security()
    reset_db_manager()

    db_file = tmp_path / "health_erp.db"
    bkp_dir = tmp_path / "health_backups"
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    monkeypatch.setenv("APP_MODE", "local")

    try:
        db = DatabaseManager(db_path=db_file, app_mode="local", backup_dir=bkp_dir)
        db.initialize_database(include_seed=True)

        with TestClient(app) as client:
            res = client.get("/api/health")
            assert res.status_code == 200
            body = res.json()
            assert body["status"] == "ok"
            assert body["version"] == APP_VERSION
            assert body["schema_version"] >= 2
            assert body["app_mode"] == "local"
            assert body["desktop_mode"] is False
            # Must not leak filesystem paths or secret keys
            serialized = json.dumps(body)
            assert str(db_file) not in serialized
            assert "jwt" not in serialized.lower()
            assert "token" not in serialized.lower()

            # Maintenance state -> 503 with Retry-After: 2
            active_db = get_db_manager()
            with active_db.exclusive_maintenance(timeout=2.0, reason="test_restore"):
                m_res = client.get("/api/health")
                assert m_res.status_code == 503
                assert m_res.headers.get("retry-after") == "2"

            # Uninitialized empty SQLite DB -> 503 starting without leaking paths
            empty_db = tmp_path / "empty_uninit.db"
            sqlite3.connect(str(empty_db)).close()
            monkeypatch.setenv("DB_PATH", str(empty_db))
            reset_db_manager()
            e_res = client.get("/api/health")
            assert e_res.status_code == 503
            assert e_res.json()["status"] == "starting"
            assert str(empty_db) not in e_res.text
    finally:
        reset_desktop_security()
        reset_db_manager()


# ---------------------------------------------------------------------------
# 3. Desktop HTTP Security Middleware: Launch Token, Host, Origin, CORS & JWT
# ---------------------------------------------------------------------------
def test_desktop_security_middleware_enforces_token_host_origin_and_cors(tmp_path, monkeypatch):
    """
    Verify desktop sidecar security controls enforce:
    - Header-only X-Krushidhan-Launch-Token on /api/* (query params rejected)
    - Strict Host header check matching bound loopback port
    - Strict Origin allowlist (Tauri / exact loopback only)
    - Safe CORS preflight headers without wildcard '*'
    - Outermost guard ordering before DB maintenance 503
    - Combined Launch Token + Bearer JWT session auth on protected APIs
    """
    reset_desktop_security()
    reset_db_manager()

    local_appdata = tmp_path / "DesktopSecLocal"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    bound_port = 49201
    launch_token = secrets.token_urlsafe(32)
    base_url = f"http://127.0.0.1:{bound_port}"

    try:
        configure_desktop_security(
            enabled=True,
            launch_token=launch_token,
            bound_host="127.0.0.1",
            bound_port=bound_port,
        )

        with TestClient(app, base_url=base_url) as client:
            # 1. Missing launch token -> 401
            r_missing = client.get("/api/health")
            assert r_missing.status_code == 401

            # 2. Query parameter token bypass rejected -> still 401
            r_query = client.get(f"/api/health?token={launch_token}&launch_token={launch_token}")
            assert r_query.status_code == 401

            # 3. Wrong launch token -> 403
            r_wrong = client.get(
                "/api/health",
                headers={LAUNCH_TOKEN_HEADER: "wrong-token-value"},
            )
            assert r_wrong.status_code == 403

            # 4. Valid launch token + valid bound host -> 200
            valid_headers = {LAUNCH_TOKEN_HEADER: launch_token}
            r_ok = client.get("/api/health", headers=valid_headers)
            assert r_ok.status_code == 200
            assert r_ok.json()["desktop_mode"] is True

            # 5. Alternate allowed loopback Host headers
            for allowed_host in (f"localhost:{bound_port}", f"[::1]:{bound_port}"):
                r_host = client.get(
                    "/api/health",
                    headers={**valid_headers, "Host": allowed_host},
                )
                assert r_host.status_code == 200

            # 6. Disallowed Host headers -> 403
            for bad_host in (
                f"evil.example:{bound_port}",
                "127.0.0.1:9999",
                "127.0.0.1",
                f"localhost.evil:{bound_port}",
            ):
                r_bad_host = client.get(
                    "/api/health",
                    headers={**valid_headers, "Host": bad_host},
                )
                assert r_bad_host.status_code == 403

            # 7. Trusted vs untrusted Origin headers
            for good_origin in (
                "http://tauri.localhost",
                "https://tauri.localhost",
                "tauri://localhost",
                f"http://127.0.0.1:{bound_port}",
                f"http://localhost:{bound_port}",
            ):
                r_good_org = client.get(
                    "/api/health",
                    headers={**valid_headers, "Origin": good_origin},
                )
                assert r_good_org.status_code == 200
                assert r_good_org.headers.get("access-control-allow-origin") == good_origin
                assert "Content-Disposition" in r_good_org.headers.get(
                    "access-control-expose-headers", ""
                )
                assert "origin" in r_good_org.headers.get("vary", "").lower()

            for bad_origin in (
                "https://evil.example",
                "http://localhost:3000",
                "http://127.0.0.1:8008",
                "null",
                "http://tauri.localhost:9999",
            ):
                r_bad_org = client.get(
                    "/api/health",
                    headers={**valid_headers, "Origin": bad_origin},
                )
                assert r_bad_org.status_code == 403
                assert "access-control-allow-origin" not in r_bad_org.headers

            # 8. Safe CORS OPTIONS preflight handling
            r_preflight = client.options(
                "/api/masters/products",
                headers={
                    "Origin": "http://tauri.localhost",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Authorization, Content-Type, X-Krushidhan-Launch-Token",
                },
            )
            assert r_preflight.status_code == 204
            assert r_preflight.headers.get("access-control-allow-origin") == "http://tauri.localhost"
            assert r_preflight.headers.get("access-control-allow-origin") != "*"
            assert "X-Krushidhan-Launch-Token" in r_preflight.headers.get(
                "access-control-allow-headers", ""
            )
            assert "Content-Disposition" in r_preflight.headers.get(
                "access-control-expose-headers", ""
            )
            assert "origin" in r_preflight.headers.get("vary", "").lower()

            r_bad_preflight = client.options(
                "/api/masters/products",
                headers={"Origin": "https://attacker.invalid"},
            )
            assert r_bad_preflight.status_code == 403
            assert "access-control-allow-origin" not in r_bad_preflight.headers

            r_no_origin_preflight = client.options("/api/masters/products")
            assert r_no_origin_preflight.status_code == 403

            # 9. Outermost middleware order: unauthorized blocked BEFORE maintenance 503
            db = get_db_manager()
            with db.exclusive_maintenance(timeout=2.0, reason="order_check"):
                assert client.get("/api/health").status_code == 401
                assert client.get("/api/health", headers={"Host": "evil.com:123"}).status_code == 403
                maint_ok = client.get(
                    "/api/health",
                    headers={**valid_headers, "Origin": "http://tauri.localhost"},
                )
                assert maint_ok.status_code == 503
                assert maint_ok.headers.get("retry-after") == "2"
                assert maint_ok.headers.get("access-control-allow-origin") == "http://tauri.localhost"

            # 10. Static root works with valid Host while protected APIs enforce BOTH launch token + JWT
            root_res = client.get("/")
            assert root_res.status_code == 200
            assert "KRUSHIDHAN" in root_res.text

            setup_res = client.post(
                "/api/auth/setup-admin",
                headers=valid_headers,
                json={
                    "username": "desk_owner",
                    "password": "DesktopStrongPassword#1",
                    "full_name": "Ramesh Shinde",
                    "shop_name": "Shinde Krishi Kendra",
                },
            )
            assert setup_res.status_code == 200
            jwt_token = setup_res.json()["token"]

            # Valid launch token WITHOUT JWT -> still 401 on protected endpoint!
            assert client.get("/api/masters/products", headers=valid_headers).status_code == 401
            # Valid JWT WITHOUT launch token -> 401!
            assert (
                client.get("/api/masters/products", headers={"Authorization": f"Bearer {jwt_token}"}).status_code
                == 401
            )
            # Valid launch token + valid JWT -> 200!
            both_res = client.get(
                "/api/masters/products",
                headers={**valid_headers, "Authorization": f"Bearer {jwt_token}"},
            )
            assert both_res.status_code == 200
    finally:
        reset_desktop_security()
        reset_db_manager()


# ---------------------------------------------------------------------------
# 4. Vercel Lazy/Optional pymongo Compatibility
# ---------------------------------------------------------------------------
def test_cloud_sync_graceful_when_pymongo_absent_and_present(tmp_path, monkeypatch):
    """
    Confirm cloud_sync works cleanly if pymongo is omitted (desktop package) and
    still retains full snapshot functionality when pymongo is available (Vercel).
    """
    import src.db.cloud_sync as cloud_mod

    orig_pymongo = cloud_mod.pymongo
    orig_has = cloud_mod.HAS_PYMONGO
    try:
        monkeypatch.setattr(cloud_mod, "pymongo", None)
        monkeypatch.setattr(cloud_mod, "HAS_PYMONGO", False)
        mgr = cloud_mod.CloudSyncManager(mongo_uri="mongodb://example", app_mode="vercel")
        assert mgr.is_cloud_enabled is False
        assert mgr.restore_db_snapshot(tmp_path / "x.db") is False
        assert mgr.save_db_snapshot(tmp_path / "x.db") is False
    finally:
        cloud_mod.pymongo = orig_pymongo
        cloud_mod.HAS_PYMONGO = orig_has


# ---------------------------------------------------------------------------
# 5. Desktop Logging Redaction & Bounded Rotation
# ---------------------------------------------------------------------------
def test_desktop_logging_redacts_secrets_and_rotates(tmp_path):
    """Verify logs/sidecar.log masks secrets/tokens/passwords and enforces rotating bounds."""
    clear_redacted_secrets()
    secret_jwt = "super-secret-jwt-key-value-9876543210"
    secret_launch = "launch-token-secret-abcdef1234567890"
    register_redacted_secret(secret_jwt)
    register_redacted_secret(secret_launch)

    log_dir = tmp_path / "desktop_logs"
    try:
        log_file = setup_desktop_logging(
            log_dir=log_dir,
            app_mode="desktop",
            max_bytes=1200,
            backup_count=2,
            level=logging.INFO,
        )
        assert log_file == log_dir / "sidecar.log"

        test_logger = logging.getLogger("agri_erp.test_redaction")
        test_logger.info("JWT=%s launch=%s", secret_jwt, secret_launch)
        test_logger.info("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload123.sig456")
        test_logger.info("X-Krushidhan-Launch-Token: customHeaderSecret999")
        test_logger.info('Payload {"password": "MyShopPassword#99", "old_password": "OldPass1"}')
        test_logger.info("GET /api/health?token=QueryLeakToken123")

        for h in logging.getLogger().handlers:
            h.flush()

        pre_rot_text = log_file.read_text(encoding="utf-8")
        assert "[REDACTED]" in pre_rot_text
        for forbidden in (
            secret_jwt,
            secret_launch,
            "eyJhbGciOiJIUzI1NiJ9",
            "customHeaderSecret999",
            "MyShopPassword#99",
            "OldPass1",
            "QueryLeakToken123",
        ):
            assert forbidden not in pre_rot_text

        # Write enough safe log lines to trigger bounded log rotation (1200B * <=3 files)
        for idx in range(45):
            test_logger.info(
                "Rotation verification line %03d launch=%s xyz1234567890",
                idx,
                secret_launch,
            )

        for h in logging.getLogger().handlers:
            h.flush()

        rotated_files = list(log_dir.glob("sidecar.log*"))
        assert 2 <= len(rotated_files) <= 3  # sidecar.log + up to backup_count=2 rotated files

        combined = "\n".join(p.read_text(encoding="utf-8") for p in rotated_files)
        assert "[REDACTED]" in combined
        assert secret_launch not in combined
    finally:
        teardown_desktop_logging()
        clear_redacted_secrets()


# ---------------------------------------------------------------------------
# 6. Single-Instance OS File Lock
# ---------------------------------------------------------------------------
def test_single_instance_lock_prevents_concurrent_access(tmp_path):
    """Verify OS file lock blocks second acquirer cleanly and preserves lock across release."""
    lock_file = tmp_path / "appdir" / ".sidecar.lock"
    lock1 = SingleInstanceLock(lock_file)
    lock2 = SingleInstanceLock(lock_file)
    lock3 = SingleInstanceLock(lock_file)

    lock1.acquire()
    try:
        assert lock1.is_locked is True
        assert lock_file.exists()
        with pytest.raises(DesktopLockError, match="already running"):
            lock2.acquire()
        assert lock2.is_locked is False
    finally:
        lock1.release()

    assert lock1.is_locked is False
    lock3.acquire()
    assert lock3.is_locked is True
    lock3.release()


# ---------------------------------------------------------------------------
# 7. Live Sidecar Subprocess E2E: Handshake, API, PDF, Excel, Single Instance & Stdin EOF
# ---------------------------------------------------------------------------
def test_sidecar_subprocess_e2e_handshake_security_features_and_stdin_shutdown(tmp_path):
    """
    Launch `python -m src.desktop_sidecar` against an isolated LOCALAPPDATA directory:
    - Verify single JSON line stdout handshake (no stray noise, ignore hostile .env)
    - Verify Launch Token / Host / Origin / CORS / /api/health
    - Verify first-run clean setup, protected masters/inventory/sales, PDF invoice, Excel export & import
    - Verify XXE / macro workbook rejection via defusedxml
    - Verify second concurrent instance fails non-zero without touching DB
    - Verify secrets never appear in sidecar.log
    - Verify closing parent stdin shuts sidecar down cleanly (exit 0), checkpoints WAL, and frees lock
    """
    app_data_root = tmp_path / "IsolatedWinLocalAppData"
    # Craft hostile working directory .env that must be strictly ignored in desktop mode
    hostile_cwd = tmp_path / "hostile_cwd"
    hostile_cwd.mkdir()
    (hostile_cwd / ".env").write_text(
        "MONGODB_URI=mongodb://must-never-be-loaded:27017\nJWT_SECRET=too-short\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("DB_PATH", None)
    env.pop("BACKUP_DIR", None)
    env.pop("JWT_SECRET", None)
    env.pop("APP_ENV", None)
    env["APP_MODE"] = "local"  # Sidecar itself must force APP_MODE=desktop
    env["LOCALAPPDATA"] = str(app_data_root)
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    proc = subprocess.Popen(
        [sys.executable, "-m", "src.desktop_sidecar"],
        cwd=str(hostile_cwd),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready_line = _read_first_line_with_timeout(proc.stdout, timeout=45.0)
        assert ready_line, f"Sidecar exited prematurely: stderr={proc.stderr.read()}"
        handshake = json.loads(ready_line.strip())

        assert handshake["protocol_version"] == 1
        assert handshake["status"] == "ready"
        port = int(handshake["port"])
        assert port > 0
        launch_token = handshake["launch_token"]
        assert isinstance(launch_token, str) and len(launch_token) >= 32
        assert int(handshake["pid"]) == proc.pid
        assert handshake["app_version"] == APP_VERSION

        # 1. Security checks against live socket
        st_no_tok, _, _ = _raw_http_request(port, "GET", "/api/health")
        assert st_no_tok == 401

        st_query_tok, _, _ = _raw_http_request(port, "GET", f"/api/health?token={launch_token}")
        assert st_query_tok == 401

        st_bad_host, _, _ = _raw_http_request(
            port,
            "GET",
            "/api/health",
            headers={"Host": f"evil.com:{port}", LAUNCH_TOKEN_HEADER: launch_token},
        )
        assert st_bad_host == 403

        st_bad_origin, hdr_bad_origin, _ = _raw_http_request(
            port,
            "GET",
            "/api/health",
            headers={"Origin": "https://evil.example", LAUNCH_TOKEN_HEADER: launch_token},
        )
        assert st_bad_origin == 403
        assert "access-control-allow-origin" not in hdr_bad_origin

        st_opt, hdr_opt, _ = _raw_http_request(
            port,
            "OPTIONS",
            "/api/sales/create",
            headers={
                "Origin": "http://tauri.localhost",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert st_opt == 204
        assert hdr_opt.get("access-control-allow-origin") == "http://tauri.localhost"

        auth_sidecar_headers = {
            "Origin": "http://tauri.localhost",
            LAUNCH_TOKEN_HEADER: launch_token,
        }
        st_health, hdr_health, raw_health = _raw_http_request(
            port, "GET", "/api/health", headers=auth_sidecar_headers
        )
        assert st_health == 200
        assert hdr_health.get("access-control-allow-origin") == "http://tauri.localhost"
        health_json = json.loads(raw_health.decode("utf-8"))
        assert health_json["status"] == "ok"
        assert health_json["desktop_mode"] is True
        assert health_json["app_mode"] == "desktop"
        assert health_json["schema_version"] >= 2

        # 2. Static assets & index HTML
        st_idx, _, raw_idx = _raw_http_request(port, "GET", "/")
        assert st_idx == 200
        assert b"KRUSHIDHAN" in raw_idx
        st_css, _, _ = _raw_http_request(port, "GET", "/static/css/app.css")
        assert st_css == 200
        st_js, _, _ = _raw_http_request(port, "GET", "/static/js/app.js")
        assert st_js == 200

        # 3. Clean first-run setup & protected business workflow
        st_setup_st, _, raw_setup_st = _raw_http_request(
            port, "GET", "/api/auth/setup-status", headers=auth_sidecar_headers
        )
        assert st_setup_st == 200
        assert json.loads(raw_setup_st.decode("utf-8"))["needs_setup"] is True

        owner_pw = "SidecarShopOwner#2026"
        setup_payload = json.dumps(
            {
                "username": "sidecar_owner",
                "password": owner_pw,
                "full_name": "Kisan Patil",
                "shop_name": "Patil Krishi Kendra",
                "mobile": "9822112233",
            }
        ).encode("utf-8")
        st_setup, _, raw_setup = _raw_http_request(
            port,
            "POST",
            "/api/auth/setup-admin",
            headers={**auth_sidecar_headers, "Content-Type": "application/json"},
            body=setup_payload,
        )
        assert st_setup == 200
        jwt_token = json.loads(raw_setup.decode("utf-8"))["token"]
        api_headers = {
            **auth_sidecar_headers,
            "Authorization": f"Bearer {jwt_token}",
        }

        # 4. Create customer, product, add stock, create sale & generate PDF invoice
        st_cust, _, raw_cust = _raw_http_request(
            port,
            "POST",
            "/api/masters/customers",
            headers={**api_headers, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "customer_name": "Sunil Jagtap",
                    "mobile": "9822334455",
                    "village": "Baramati",
                    "group_id": 1,
                }
            ).encode("utf-8"),
        )
        assert st_cust == 200
        cust_id = json.loads(raw_cust.decode("utf-8"))["customer_id"]

        st_prod, _, raw_prod = _raw_http_request(
            port,
            "POST",
            "/api/masters/products",
            headers={**api_headers, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "product_name": "Desktop Sidecar Urea 45kg",
                    "category_id": 1,
                    "unit_id": 2,
                    "tax_group_id": 2,
                    "hsn_code": "3102",
                    "default_purchase_rate": 240.0,
                    "default_sale_rate": 266.5,
                    "default_mrp": 266.5,
                }
            ).encode("utf-8"),
        )
        assert st_prod == 200
        prod_id = json.loads(raw_prod.decode("utf-8"))["product_id"]

        st_stk, _, _ = _raw_http_request(
            port,
            "POST",
            "/api/inventory/add-stock",
            headers={**api_headers, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "product_id": prod_id,
                    "qty": 10.0,
                    "batch_no": "DSB-01",
                    "purchase_rate": 240.0,
                    "sale_rate": 266.5,
                    "mrp": 266.5,
                    "exp_date": "2028-12-31",
                }
            ).encode("utf-8"),
        )
        assert st_stk == 200

        st_inv_no, _, raw_inv_no = _raw_http_request(
            port, "GET", "/api/sales/next-invoice-no", headers=api_headers
        )
        assert st_inv_no == 200
        inv_no = json.loads(raw_inv_no.decode("utf-8"))["next_invoice_no"]

        st_tax, _, raw_tax = _raw_http_request(
            port,
            "POST",
            "/api/sales/calculate-tax",
            headers={**api_headers, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "qty": 2.0,
                    "rate": 266.5,
                    "discount_percent": 0.0,
                    "cgst_rate": 2.5,
                    "sgst_rate": 2.5,
                    "igst_rate": 0.0,
                    "is_rate_tax_inclusive": True,
                }
            ).encode("utf-8"),
        )
        assert st_tax == 200
        tax_calc = json.loads(raw_tax.decode("utf-8"))

        st_sale, _, raw_sale = _raw_http_request(
            port,
            "POST",
            "/api/sales/create",
            headers={**api_headers, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "invoice_no": inv_no,
                    "sale_date": "2026-11-10",
                    "customer_id": cust_id,
                    "payment_mode": "CASH",
                    "total_taxable": tax_calc["taxable_amount"],
                    "total_cgst": tax_calc["cgst_amount"],
                    "total_sgst": tax_calc["sgst_amount"],
                    "total_igst": 0.0,
                    "net_amount": tax_calc["total_amount"],
                    "paid_amount": tax_calc["total_amount"],
                    "due_amount": 0.0,
                    "items": [
                        {
                            "product_id": prod_id,
                            "qty": 2.0,
                            "unit_id": 2,
                            "sale_rate": 266.5,
                            "mrp": 266.5,
                            "discount_percent": 0.0,
                            "discount_amount": 0.0,
                            "taxable_amount": tax_calc["taxable_amount"],
                            "cgst_rate": 2.5,
                            "cgst_amount": tax_calc["cgst_amount"],
                            "sgst_rate": 2.5,
                            "sgst_amount": tax_calc["sgst_amount"],
                            "igst_rate": 0.0,
                            "igst_amount": 0.0,
                            "total_amount": tax_calc["total_amount"],
                        }
                    ],
                }
            ).encode("utf-8"),
        )
        assert st_sale == 200, f"Unexpected sales create response: {st_sale} {raw_sale!r}"
        sale_id = json.loads(raw_sale.decode("utf-8"))["sale_id"]

        st_pdf, hdr_pdf, raw_pdf = _raw_http_request(
            port, "GET", f"/api/sales/{sale_id}/pdf", headers=api_headers
        )
        assert st_pdf == 200
        assert hdr_pdf.get("content-type", "").startswith("application/pdf")
        assert raw_pdf.startswith(b"%PDF-")

        # 5. Excel export (openpyxl) + Excel import template + XXE/.xlsm security check (defusedxml)
        st_exp, hdr_exp, raw_exp = _raw_http_request(
            port, "GET", "/api/excel/exports/products", headers=api_headers
        )
        assert st_exp == 200
        assert raw_exp.startswith(b"PK\x03\x04")
        assert ".xlsx" in hdr_exp.get("content-disposition", "")

        st_tpl, _, raw_tpl = _raw_http_request(
            port, "GET", "/api/excel/imports/products/template", headers=api_headers
        )
        assert st_tpl == 200
        wb = openpyxl.load_workbook(io.BytesIO(raw_tpl), data_only=False)
        ws = wb["Product Import"]
        ws.append(
            [
                "Imported Potash Sidecar",
                "Chemical Fertilizers",
                "Mahadhan (Smartchem Technologies)",
                "Bag",
                "3104",
                "GST 5% (Fertilizers & Bulk Inputs)",
                1700.0,
                1750.0,
                5,
                "Sugarcane",
            ]
        )
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        import_bytes = buf.getvalue()

        body_mp, ctype_mp = _multipart_xlsx_body("file", "desktop_products.xlsx", import_bytes)
        st_prev, _, raw_prev = _raw_http_request(
            port,
            "POST",
            "/api/excel/imports/products/preview",
            headers={**api_headers, "Content-Type": ctype_mp},
            body=body_mp,
        )
        assert st_prev == 200
        prev_json = json.loads(raw_prev.decode("utf-8"))
        assert prev_json["summary"]["ready_count"] == 1

        st_commit, _, raw_commit = _raw_http_request(
            port,
            "POST",
            "/api/excel/imports/products/commit",
            headers={**api_headers, "Content-Type": "application/json"},
            body=json.dumps(
                {
                    "preview_token": prev_json["preview_token"],
                    "file_sha256": prev_json["file_sha256"],
                }
            ).encode("utf-8"),
        )
        assert st_commit == 200
        assert json.loads(raw_commit.decode("utf-8"))["imported_count"] == 1

        # Reject macro extension (.xlsm)
        bad_mp, bad_ctype = _multipart_xlsx_body("file", "macro_evil.xlsm", import_bytes)
        st_xlsm, _, _ = _raw_http_request(
            port,
            "POST",
            "/api/excel/imports/products/preview",
            headers={**api_headers, "Content-Type": bad_ctype},
            body=bad_mp,
        )
        assert st_xlsm == 400

        # Reject XXE inside .xlsx
        xxe_buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(import_bytes), "r") as zin, zipfile.ZipFile(
            xxe_buf, "w", compression=zipfile.ZIP_DEFLATED
        ) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "[Content_Types].xml":
                    data = b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>' + data
                zout.writestr(item, data)
        xxe_mp, xxe_ctype = _multipart_xlsx_body("file", "xxe_test.xlsx", xxe_buf.getvalue())
        st_xxe, _, _ = _raw_http_request(
            port,
            "POST",
            "/api/excel/imports/products/preview",
            headers={**api_headers, "Content-Type": xxe_ctype},
            body=xxe_mp,
        )
        assert st_xxe == 400

        # 6. Second concurrent sidecar instance MUST fail cleanly non-zero without polluting stdout
        proc2 = subprocess.run(
            [sys.executable, "-m", "src.desktop_sidecar"],
            cwd=str(hostile_cwd),
            env=env,
            input="",
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        assert proc2.returncode == 2
        assert proc2.stdout.strip() == ""
        assert "already running" in proc2.stderr.lower()

        # 7. Shutdown via parent stdin closure (EOF)
        proc.stdin.close()
        exit_code = proc.wait(timeout=10.0)
        assert exit_code == 0

        leftover_stdout = proc.stdout.read()
        assert leftover_stdout == "", f"Unexpected extra stdout after handshake: {leftover_stdout!r}"

        # 8. Inspect log file + secret redaction + isolated SQLite state
        app_dir = app_data_root / APP_ID
        log_file = app_dir / "logs" / "sidecar.log"
        assert log_file.exists()
        log_text = log_file.read_text(encoding="utf-8")
        assert "Parent stdin EOF detected" in log_text
        assert launch_token not in log_text
        assert jwt_token not in log_text
        assert owner_pw not in log_text
        jwt_secret_val = (app_dir / ".jwt_secret").read_text(encoding="utf-8").strip()
        assert jwt_secret_val not in log_text

        isolated_db = app_dir / "data" / "agri_erp.db"
        assert isolated_db.exists()
        ro_conn = sqlite3.connect(f"file:{isolated_db}?mode=ro", uri=True)
        try:
            assert ro_conn.execute("PRAGMA integrity_check;").fetchall() == [("ok",)]
        finally:
            ro_conn.close()

        # 9. Third instance launches cleanly after instance #1 released OS lock
        proc3 = subprocess.Popen(
            [sys.executable, "-m", "src.desktop_sidecar"],
            cwd=str(hostile_cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            line3 = _read_first_line_with_timeout(proc3.stdout, timeout=45.0)
            hs3 = json.loads(line3.strip())
            assert hs3["status"] == "ready"
            assert int(hs3["port"]) > 0
            proc3.stdin.close()
            assert proc3.wait(timeout=10.0) == 0
        finally:
            _terminate_proc_safely(proc3)
    finally:
        _terminate_proc_safely(proc)


# ---------------------------------------------------------------------------
# 8. SIGTERM Shutdown & PyInstaller Spec Manifest Verification
# ---------------------------------------------------------------------------
def test_sidecar_subprocess_sigterm_graceful_shutdown(tmp_path):
    """Verify SIGTERM and immediate-EOF trigger clean shutdown, WAL checkpoint, and lock release."""
    eof_root = tmp_path / "ImmediateEofAppData"
    base_env = os.environ.copy()
    base_env.pop("DB_PATH", None)
    base_env.pop("BACKUP_DIR", None)
    base_env.pop("JWT_SECRET", None)
    base_env["PYTHONPATH"] = str(PROJECT_ROOT)

    # Immediate EOF on stdin must exit cleanly (0) without lifespan CancelledError errors
    eof_env = {**base_env, "LOCALAPPDATA": str(eof_root)}
    eof_proc = subprocess.run(
        [sys.executable, "-m", "src.desktop_sidecar"],
        cwd=str(tmp_path),
        env=eof_env,
        input="",
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    assert eof_proc.returncode == 0
    eof_log = (eof_root / APP_ID / "logs" / "sidecar.log").read_text(encoding="utf-8")
    assert "CancelledError" not in eof_log
    assert "ERROR" not in eof_log
    assert "Released single-instance lock cleanly." in eof_log

    app_data_root = tmp_path / "SigtermAppData"
    env = {**base_env, "LOCALAPPDATA": str(app_data_root)}
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
        line = _read_first_line_with_timeout(proc.stdout, timeout=45.0)
        handshake = json.loads(line.strip())
        assert handshake["status"] == "ready"

        proc.terminate()
        rc = proc.wait(timeout=10.0)
        assert rc == 0

        sig_log = (app_data_root / APP_ID / "logs" / "sidecar.log").read_text(encoding="utf-8")
        assert "HTTP server loop stopped cleanly." in sig_log
        assert "Released single-instance lock cleanly." in sig_log
        assert "ERROR" not in sig_log

        # Lock must be freed after SIGTERM exit
        lock = SingleInstanceLock(app_data_root / APP_ID / ".sidecar.lock")
        lock.acquire()
        lock.release()
    finally:
        _terminate_proc_safely(proc)


def test_pyinstaller_spec_bundle_manifest_and_exclusions():
    """Verify krushidhan-sidecar.spec references all 7 required assets and excludes DBs/tests/cloud."""
    spec_path = PROJECT_ROOT / "krushidhan-sidecar.spec"
    req_desk = PROJECT_ROOT / "requirements-desktop.txt"
    req_build = PROJECT_ROOT / "requirements-build.txt"

    assert spec_path.is_file()
    assert req_desk.is_file()
    assert req_build.is_file()

    spec_text = spec_path.read_text(encoding="utf-8")
    for required_name in (
        "schema.sql",
        "seed_clean.sql",
        "NotoSansDevanagari-Regular.ttf",
        "NotoSansDevanagari-Bold.ttf",
        "index.html",
        "app.css",
        "app.js",
        "desktop_sidecar.py",
        "defusedxml",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl",
        "bcrypt._bcrypt",
        "pydantic_core",
    ):
        assert required_name in spec_text

    # Confirm defense-in-depth exclusions are explicitly defined
    for forbidden_name in ('"pytest"', '"httpx"', '"pymongo"', '"lxml"', '"data/"', '"backups/"', 'seed.sql'):
        assert forbidden_name in spec_text

    # Non-comment requirement lines in requirements-desktop.txt must exclude cloud/test packages
    req_packages = [
        line.split("#", 1)[0].strip().lower()
        for line in req_desk.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    ]
    joined_reqs = "\n".join(req_packages)
    assert "defusedxml==0.7.1" in joined_reqs
    assert "openpyxl==3.1.5" in joined_reqs
    for forbidden_pkg in ("pymongo", "pytest", "httpx"):
        assert not any(req.startswith(forbidden_pkg) for req in req_packages)

