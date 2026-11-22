"""
Stage A Security Verification Suite:
- Hardcoded JWT fallback removal & strict secret validation
- Atomic concurrent first-admin setup protection (service & HTTP layers)
- Network/origin/token access control for /api/auth/setup-admin & run.py loopback binding
- Removal of developer PII from backend modules, seed templates, and pre-login responses
- Loud failure semantics for database initialization errors
"""
from __future__ import annotations

import base64
import concurrent.futures
import json
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Tuple

import pytest
from fastapi.testclient import TestClient

import run as launcher
from src import config as config_mod
from src.app import app
from src.config import (
    JWTConfigurationError,
    get_jwt_secret,
    validate_jwt_secret,
)
from src.db.connection import (
    DatabaseInitializationError,
    DatabaseManager,
    get_db_manager,
    reset_db_manager,
)
from src.models.system import CompanySettings
from src.services.auth_service import (
    AuthService,
    InitialSetupAlreadyCompletedError,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _b64u(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _make_remote_client_app(remote_host: str, remote_port: int = 51234):
    """Wrap FastAPI app to simulate HTTP requests arriving from a specific client IP."""

    async def wrapper(scope, receive, send):
        if scope["type"] == "http":
            patched_scope = dict(scope)
            patched_scope["client"] = (remote_host, remote_port)
            await app(patched_scope, receive, send)
        else:
            await app(scope, receive, send)

    return wrapper


# =====================================================================
# 1. JWT Secret Hardening & Startup Validation
# =====================================================================
def test_no_hardcoded_fallback_and_strict_jwt_validation(tmp_path, monkeypatch):
    """Ensure fallback secret is deleted, retired keys rejected, and production fails closed."""
    assert not hasattr(config_mod, "_NON_DESKTOP_FALLBACK_JWT_SECRET")

    # Reject empty, short, low-entropy, and retired/leaked default secrets
    with pytest.raises(JWTConfigurationError):
        validate_jwt_secret("", source="JWT_SECRET")
    with pytest.raises(JWTConfigurationError):
        validate_jwt_secret("   ", source="JWT_SECRET")
    with pytest.raises(JWTConfigurationError):
        validate_jwt_secret("too_short_key_1234567890", source="JWT_SECRET")
    with pytest.raises(JWTConfigurationError):
        validate_jwt_secret("a" * 48, source="JWT_SECRET")

    revoked_secret = "_".join(
        ["krushidhan", "erp", "secure", "key", "2026", "akash", "lengare", "9503673620"]
    )
    with pytest.raises(JWTConfigurationError, match="retired/leaked"):
        validate_jwt_secret(revoked_secret, source="JWT_SECRET")

    # Vercel mode or APP_ENV=production MUST fail closed without JWT_SECRET
    with pytest.raises(JWTConfigurationError):
        get_jwt_secret(env={"APP_MODE": "vercel"}, project_root=tmp_path)
    with pytest.raises(JWTConfigurationError):
        get_jwt_secret(env={"APP_MODE": "local", "APP_ENV": "production"}, project_root=tmp_path)

    # Explicit invalid JWT_SECRET must NEVER silently fall back to file key
    localappdata = tmp_path / "LocalApp"
    with pytest.raises(JWTConfigurationError):
        get_jwt_secret(
            env={"APP_MODE": "desktop", "LOCALAPPDATA": str(localappdata), "JWT_SECRET": "short"},
            project_root=tmp_path,
        )
    with pytest.raises(JWTConfigurationError):
        get_jwt_secret(
            env={"APP_MODE": "desktop", "LOCALAPPDATA": str(localappdata), "JWT_SECRET": ""},
            project_root=tmp_path,
        )

    # Desktop mode generates and persists unique strong secret
    desktop_secret_1 = get_jwt_secret(
        env={"APP_MODE": "desktop", "LOCALAPPDATA": str(localappdata)},
        project_root=tmp_path,
    )
    desktop_secret_2 = get_jwt_secret(
        env={"APP_MODE": "desktop", "LOCALAPPDATA": str(localappdata)},
        project_root=tmp_path,
    )
    assert desktop_secret_1 == desktop_secret_2
    assert len(desktop_secret_1) >= 32
    secret_file = localappdata / "com.krushidhan.erp" / ".jwt_secret"
    assert secret_file.exists()

    # Corrupt / shortened .jwt_secret file fails closed
    secret_file.write_text("tampered_short", encoding="utf-8")
    with pytest.raises(JWTConfigurationError):
        get_jwt_secret(
            env={"APP_MODE": "desktop", "LOCALAPPDATA": str(localappdata)},
            project_root=tmp_path,
        )

    # Non-production local mode persists instance key under <project_root>/data/.jwt_secret
    local_secret = get_jwt_secret(env={"APP_MODE": "local"}, project_root=tmp_path)
    assert len(local_secret) >= 32
    assert (tmp_path / "data" / ".jwt_secret").exists()

    # Verify .gitignore protects data/.jwt_secret in real repo
    chk = subprocess.run(["git", "check-ignore", "-q", "data/.jwt_secret"], cwd=str(PROJECT_ROOT))
    assert chk.returncode == 0

    # Startup fails closed if APP_MODE=vercel without JWT_SECRET
    reset_db_manager()
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("APP_MODE", "vercel")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "vercel_no_jwt.db"))
    with pytest.raises(JWTConfigurationError):
        with TestClient(app):
            pass
    reset_db_manager()


def test_jwt_verification_rejects_alg_none_and_invalid_claims():
    """Ensure forged alg=none, missing exp, boolean exp, and missing claims are rejected."""
    forged_none = f"{_b64u({'alg': 'none', 'typ': 'JWT'})}.{_b64u({'user_id': 1, 'username': 'admin', 'role': 'ADMIN', 'exp': 9999999999})}."
    assert AuthService.verify_access_token(forged_none) is None

    valid_token = AuthService.create_access_token(1, "admin", "Shop Admin", "ADMIN", expires_in=60)
    parts = valid_token.split(".")
    assert AuthService.verify_access_token(valid_token) is not None

    # Expired token rejected
    expired = AuthService.create_access_token(1, "admin", "Shop Admin", "ADMIN", expires_in=-5)
    assert AuthService.verify_access_token(expired) is None

    # Truncated signature rejected
    assert AuthService.verify_access_token(f"{parts[0]}.{parts[1]}.bad") is None


# =====================================================================
# 2. Concurrent First-Admin Setup Protection
# =====================================================================
def test_concurrent_setup_initial_admin_service_race(tmp_path):
    """Multiple concurrent threads calling AuthService.setup_initial_admin produce strictly 1 admin."""
    db_file = tmp_path / "race_service.db"
    db = DatabaseManager(db_path=db_file, app_mode="local", clean_install=True)
    db.initialize_database(include_seed=True)
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM users;")["cnt"] == 0

    num_threads = 6
    barrier = threading.Barrier(num_threads)

    def worker(idx: int) -> Tuple[str, str]:
        svc = AuthService(db)
        barrier.wait(timeout=5)
        try:
            user = svc.setup_initial_admin(
                username=f"owner_{idx}",
                plain_password=f"StrongPass#{idx}23",
                full_name=f"Owner {idx}",
                company_settings=CompanySettings(company_name=f"Agri Shop {idx}"),
            )
            return ("CREATED", user.username)
        except InitialSetupAlreadyCompletedError:
            return ("BLOCKED", f"owner_{idx}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as pool:
        results = list(pool.map(worker, range(num_threads)))

    created = [r for r in results if r[0] == "CREATED"]
    blocked = [r for r in results if r[0] == "BLOCKED"]
    assert len(created) == 1
    assert len(blocked) == num_threads - 1

    users = db.fetch_all("SELECT username, role FROM users;")
    assert len(users) == 1
    assert users[0]["username"] == created[0][1]
    assert users[0]["role"] == "ADMIN"

    settings_row = db.fetch_one("SELECT company_name FROM company_settings WHERE setting_id = 1;")
    winner_idx = created[0][1].split("_")[1]
    assert settings_row["company_name"] == f"Agri Shop {winner_idx}"

    audit_rows = db.fetch_all("SELECT * FROM audit_log WHERE action_type = 'SETUP_ADMIN';")
    assert len(audit_rows) == 1


def test_concurrent_setup_admin_http_endpoint_race(tmp_path, monkeypatch):
    """Concurrent HTTP POST /api/auth/setup-admin requests yield exactly one 200 and remaining 403."""
    reset_db_manager()
    local_appdata = tmp_path / "RaceHttpLocal"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.delenv("SETUP_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    num_threads = 6
    barrier = threading.Barrier(num_threads)

    with TestClient(app) as client:
        def send_setup(idx: int):
            barrier.wait(timeout=5)
            return idx, client.post(
                "/api/auth/setup-admin",
                json={
                    "username": f"http_owner_{idx}",
                    "password": f"StrongHttp#{idx}23",
                    "full_name": f"HTTP Owner {idx}",
                    "shop_name": f"Shop Winner {idx}",
                },
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as pool:
            responses = list(pool.map(send_setup, range(num_threads)))

        ok_resps = [(i, r) for i, r in responses if r.status_code == 200]
        forbidden_resps = [(i, r) for i, r in responses if r.status_code == 403]
        assert len(ok_resps) == 1, f"Unexpected status codes: {[r.status_code for _, r in responses]}"
        assert len(forbidden_resps) == num_threads - 1

        winner_idx, winner_resp = ok_resps[0]
        winner_data = winner_resp.json()
        assert winner_data["user"]["username"] == f"http_owner_{winner_idx}"
        assert winner_data["shop"]["name"] == f"Shop Winner {winner_idx}"

        db = get_db_manager()
        assert db.fetch_one("SELECT COUNT(*) AS cnt FROM users;")["cnt"] == 1

        me_res = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {winner_data['token']}"},
        )
        assert me_res.status_code == 200

    reset_db_manager()


# =====================================================================
# 3. Unauthorized Setup Requests & Loopback Binding Enforcement
# =====================================================================
def test_setup_admin_blocks_unauthorized_network_clients_and_bad_tokens(tmp_path, monkeypatch):
    """Verify non-loopback clients, spoofed proxy headers, bad origins, and bad tokens are rejected with 403."""
    reset_db_manager()
    local_appdata = tmp_path / "GuardAppData"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.delenv("SETUP_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    valid_payload = {
        "username": "legit_owner",
        "password": "StrongShopPassword#1",
        "full_name": "Legit Owner",
        "shop_name": "Legit Agro Kendra",
    }

    # Pre-initialize clean desktop DB explicitly since wrapped ASGI app uses TestClient
    db = DatabaseManager()
    db.initialize_database(include_seed=True)

    # 1. Remote LAN client blocked in desktop mode
    remote_client = TestClient(_make_remote_client_app("192.168.1.50"))
    r_remote = remote_client.post("/api/auth/setup-admin", json=valid_payload)
    assert r_remote.status_code == 403

    # 2. Localhost with X-Forwarded-For blocked in desktop mode
    local_client = TestClient(app)
    r_proxy = local_client.post(
        "/api/auth/setup-admin",
        json=valid_payload,
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    assert r_proxy.status_code == 403

    # 3. External browser Origin blocked in desktop mode
    r_origin = local_client.post(
        "/api/auth/setup-admin",
        json=valid_payload,
        headers={"Origin": "https://attacker.example"},
    )
    assert r_origin.status_code == 403

    # Still 0 users after all rejected attempts
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM users;")["cnt"] == 0

    # 4. Valid Tauri desktop origin from loopback succeeds
    r_ok = local_client.post(
        "/api/auth/setup-admin",
        json=valid_payload,
        headers={"Origin": "http://tauri.localhost"},
    )
    assert r_ok.status_code == 200
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM users;")["cnt"] == 1
    reset_db_manager()


def test_setup_admin_token_enforcement_for_local_and_vercel(tmp_path, monkeypatch):
    """Remote local setup or Vercel clean setup requires SETUP_ADMIN_TOKEN."""
    reset_db_manager()
    local_db = tmp_path / "remote_local_clean.db"
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(local_db))
    monkeypatch.setenv("CLEAN_INSTALL", "1")
    monkeypatch.delenv("SETUP_ADMIN_TOKEN", raising=False)
    reset_db_manager()
    DatabaseManager().initialize_database(include_seed=True)

    remote_client = TestClient(_make_remote_client_app("10.0.0.42"))
    payload = {
        "username": "remote_admin",
        "password": "StrongPassword#2026",
        "full_name": "Shop Owner",
        "shop_name": "Kisan Kendra",
    }

    # Without SETUP_ADMIN_TOKEN, remote caller is blocked
    assert remote_client.post("/api/auth/setup-admin", json=payload).status_code == 403

    # With SETUP_ADMIN_TOKEN configured, missing/wrong token returns 403
    monkeypatch.setenv("SETUP_ADMIN_TOKEN", "one-time-setup-secret-9988")
    assert remote_client.post("/api/auth/setup-admin", json=payload).status_code == 403
    assert (
        remote_client.post(
            "/api/auth/setup-admin",
            json=payload,
            headers={"X-Setup-Token": "wrong-token"},
        ).status_code
        == 403
    )

    # Correct X-Setup-Token authorizes remote initial admin setup
    res_ok = remote_client.post(
        "/api/auth/setup-admin",
        json=payload,
        headers={"X-Setup-Token": "one-time-setup-secret-9988"},
    )
    assert res_ok.status_code == 200

    # Vercel mode without SETUP_ADMIN_TOKEN blocks even without proxy headers
    reset_db_manager()
    vercel_db = tmp_path / "vercel_clean.db"
    DatabaseManager(db_path=vercel_db, app_mode="local", clean_install=True).initialize_database(
        include_seed=True
    )
    monkeypatch.delenv("SETUP_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("APP_MODE", "vercel")
    monkeypatch.setenv("DB_PATH", str(vercel_db))
    monkeypatch.setenv("JWT_SECRET", "vercel_strong_jwt_secret_for_stage_a_test_123456")
    reset_db_manager()

    v_client = TestClient(app)
    assert v_client.post("/api/auth/setup-admin", json=payload).status_code == 403

    monkeypatch.setenv("SETUP_ADMIN_TOKEN", "vercel-init-token-7766")
    assert (
        v_client.post(
            "/api/auth/setup-admin",
            json={**payload, "setup_token": "vercel-init-token-7766"},
        ).status_code
        == 200
    )
    reset_db_manager()


def test_run_py_desktop_loopback_enforcement():
    """Desktop launcher must refuse non-loopback bind addresses."""
    assert launcher.resolve_bind_address("desktop", "127.0.0.1", 8008) == ("127.0.0.1", 8008)
    assert launcher.resolve_bind_address("desktop", "localhost", 8010) == ("localhost", 8010)
    assert launcher.resolve_bind_address("desktop", "::1", 8011) == ("::1", 8011)
    assert launcher.resolve_bind_address("local", "0.0.0.0", 8008) == ("0.0.0.0", 8008)

    with pytest.raises(RuntimeError, match="non-loopback"):
        launcher.resolve_bind_address("desktop", "0.0.0.0", 8008)
    with pytest.raises(RuntimeError, match="non-loopback"):
        launcher.resolve_bind_address("desktop", "192.168.1.25", 8008)
    with pytest.raises(ValueError, match="Invalid server port"):
        launcher.resolve_bind_address("desktop", "127.0.0.1", 70000)


# =====================================================================
# 4. Removal of Developer Personal Information (Stage A Backend & Pre-Login)
# =====================================================================
def test_no_developer_personal_info_in_stage_a_files_or_prelogin(tmp_path, monkeypatch):
    """Ensure backend code, seeds, setup-status, and pre-login chrome contain zero developer PII."""
    forbidden_markers = (
        "आकाश",
        "लेंगारे",
        "9503673620",
        "9503573620",
        "7218409780",
        "akashlengare",
        "27BSXPL3414R1Z5",
        "409002609103",
        "IBKL0000055",
        "LCFRD0920250506SNG",
        "LCID0920250497SNG",
        "LCSD0920250506SNG",
        "krushidhan_erp_secure_key",
    )

    stage_a_files = (
        PROJECT_ROOT / "src" / "config.py",
        PROJECT_ROOT / "src" / "services" / "auth_service.py",
        PROJECT_ROOT / "src" / "api" / "routes_auth.py",
        PROJECT_ROOT / "src" / "db" / "seed.sql",
        PROJECT_ROOT / "src" / "db" / "seed_clean.sql",
    )
    for file_path in stage_a_files:
        text = file_path.read_text(encoding="utf-8")
        for marker in forbidden_markers:
            assert marker not in text, f"Developer PII marker '{marker}' leaked in {file_path}"

    # Verify desktop clean install + pre-login setup-status & HTML chrome
    reset_db_manager()
    local_appdata = tmp_path / "NoPiiLocal"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    with TestClient(app) as client:
        status_res = client.get("/api/auth/setup-status")
        assert status_res.status_code == 200
        status_data = status_res.json()
        assert status_data["shop_mobile"] == ""
        assert "कृषीधन" not in status_data["shop_name"]

        html = client.get("/").text
        assert "or akash" not in html
        assert '<span id="user-display-name" class="user-name">User</span>' in html
        assert '<span id="drawer-user-name">User</span>' in html

    # Verify bootstrap local mode does NOT recreate removed developer 'akash' user
    reset_db_manager()
    local_db = tmp_path / "local_bootstrap.db"
    local_backups = tmp_path / "local_bootstrap_backups"
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(local_db))
    monkeypatch.setenv("BACKUP_DIR", str(local_backups))
    reset_db_manager()
    mgr = DatabaseManager(
        db_path=local_db,
        app_mode="local",
        backup_dir=local_backups,
        clean_install=False,
    )
    mgr.initialize_database(include_seed=False)
    svc = AuthService(mgr)
    assert mgr.fetch_one("SELECT * FROM users WHERE username = 'akash';") is None
    assert mgr.fetch_one("SELECT * FROM users WHERE username = 'admin';") is not None
    reset_db_manager()


# =====================================================================
# 5. Loud Failure Semantics for Database Initialization Errors
# =====================================================================
def test_database_initialization_errors_surface_loudly(tmp_path, monkeypatch):
    """Corrupt DBs, missing schema.sql, and startup DB failures must fail loudly, never silently."""
    # 1. Corrupt database file fails loudly on get_connection() and get_schema_version()
    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_bytes(b"DEFINITELY_NOT_SQLITE_BYTES" * 50)
    mgr = DatabaseManager(db_path=corrupt_db, app_mode="local")
    with pytest.raises(DatabaseInitializationError):
        mgr.get_connection()
    with pytest.raises(DatabaseInitializationError):
        mgr.get_schema_version()

    # 2. FastAPI app startup fails loudly on corrupt DB
    reset_db_manager()
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(corrupt_db))
    with pytest.raises(DatabaseInitializationError):
        with TestClient(app):
            pass
    reset_db_manager()

    # 3. Missing schema.sql during lazy get_connection() raises DatabaseInitializationError
    fresh_db = tmp_path / "missing_schema.db"
    orig_exists = Path.exists

    def fake_exists(self_path: Path) -> bool:
        if self_path.name == "schema.sql":
            return False
        return orig_exists(self_path)

    monkeypatch.setattr(Path, "exists", fake_exists)
    missing_mgr = DatabaseManager(db_path=fresh_db, app_mode="local")
    with pytest.raises(DatabaseInitializationError, match="Schema file not found"):
        missing_mgr.get_connection()
