"""
Runtime Mode, Storage Path, and Security Configuration for Krushidhan Agri-Input Shop ERP.

Supports three isolated runtime modes via APP_MODE:
- "local"   : Local developer / single-PC server using repo ./data and ./backups
- "desktop" : Offline-first Tauri/PyInstaller desktop app using %LOCALAPPDATA%\\com.krushidhan.erp
              and strictly bypassing all MongoDB / Vercel cloud sync mechanisms
- "vercel"  : Vercel serverless deployment using /tmp and optional MongoDB cloud snapshot sync
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sys
import threading
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple, Union

APP_ID = "com.krushidhan.erp"
APP_VERSION = "1.0.0"
APP_MODES = ("local", "desktop", "vercel")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIN_JWT_SECRET_LENGTH = 32

# Mandatory bundled read-only resources (preserved under identical relative paths in source & frozen bundles)
REQUIRED_DESKTOP_ASSETS: Tuple[Tuple[str, ...], ...] = (
    ("src", "db", "schema.sql"),
    ("src", "db", "seed_clean.sql"),
    ("src", "printing", "fonts", "NotoSansDevanagari-Regular.ttf"),
    ("src", "printing", "fonts", "NotoSansDevanagari-Bold.ttf"),
    ("src", "web", "templates", "index.html"),
    ("src", "web", "static", "css", "app.css"),
    ("src", "web", "static", "js", "app.js"),
)

# SHA-256 digests of retired / leaked default secrets that must NEVER be accepted again
_REVOKED_JWT_SECRET_HASHES = frozenset(
    {
        "7c43c7b2ec26bd3d2626857f075a6969a3a351cfaba69f1d96f154d820fc2dec",
    }
)
_JWT_SECRET_LOCK = threading.Lock()


class JWTConfigurationError(RuntimeError):
    """Raised when JWT signing configuration is missing, weak, revoked, or unreadable."""


def resolve_app_mode(
    app_mode: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Resolve effective APP_MODE ('local', 'desktop', or 'vercel')."""
    env_map = env if env is not None else os.environ
    explicit = app_mode if app_mode is not None else env_map.get("APP_MODE")

    if explicit is not None and str(explicit).strip():
        normalized = str(explicit).strip().lower()
        if normalized not in APP_MODES:
            raise ValueError(
                f"Invalid APP_MODE '{explicit}'. Expected one of: {', '.join(APP_MODES)}."
            )
        return normalized

    vercel_flag = str(env_map.get("VERCEL", "")).strip().lower()
    if vercel_flag and vercel_flag not in ("0", "false", "no"):
        return "vercel"

    return "local"


def load_runtime_dotenv(
    app_mode: Optional[str] = None,
    project_root: Optional[Path] = None,
) -> bool:
    """Load .env ONLY when not running in desktop mode so desktop never imports cloud credentials."""
    mode = resolve_app_mode(app_mode=app_mode)
    if mode == "desktop":
        return False

    env_file = (project_root or PROJECT_ROOT) / ".env"
    if not env_file.exists():
        return False

    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
        return True
    except ImportError:
        return False


def get_desktop_app_dir(
    env: Optional[Mapping[str, str]] = None,
    platform_name: Optional[str] = None,
) -> Path:
    """
    Resolve per-user application data directory for desktop mode.
    On Windows this resolves to %LOCALAPPDATA%\\com.krushidhan.erp.
    """
    env_map = env if env is not None else os.environ
    plat = (platform_name or sys.platform).lower()

    local_app_data = env_map.get("LOCALAPPDATA")
    if local_app_data and str(local_app_data).strip():
        return Path(str(local_app_data).strip()) / APP_ID

    if plat.startswith("win"):
        return Path.home() / "AppData" / "Local" / APP_ID

    if plat == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_ID

    xdg_data_home = env_map.get("XDG_DATA_HOME")
    base = (
        Path(str(xdg_data_home).strip())
        if xdg_data_home and str(xdg_data_home).strip()
        else (Path.home() / ".local" / "share")
    )
    return base / APP_ID


def resolve_db_path(
    db_path: Optional[Union[str, Path]] = None,
    app_mode: Optional[str] = None,
    project_root: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Union[Path, str]:
    """
    Resolve active SQLite database path cleanly:
    1. Explicit db_path argument
    2. DB_PATH environment variable (always respected for tooling, tests, custom deployments)
    3. Mode-based location:
       - desktop: %LOCALAPPDATA%\\com.krushidhan.erp\\data\\agri_erp.db
       - vercel : /tmp/agri_erp.db
       - local  : <repo>/data/agri_erp.db
    """
    env_map = env if env is not None else os.environ

    if db_path is not None:
        if isinstance(db_path, str) and db_path.strip() == ":memory:":
            return ":memory:"
        return Path(db_path)

    env_db_path = env_map.get("DB_PATH")
    if env_db_path and str(env_db_path).strip():
        cleaned = str(env_db_path).strip()
        if cleaned == ":memory:":
            return ":memory:"
        return Path(cleaned)

    mode = resolve_app_mode(app_mode=app_mode, env=env_map)
    if mode == "desktop":
        return get_desktop_app_dir(env=env_map) / "data" / "agri_erp.db"
    if mode == "vercel":
        return Path("/tmp/agri_erp.db")
    return (project_root or PROJECT_ROOT) / "data" / "agri_erp.db"


def resolve_backup_dir(
    backup_dir: Optional[Union[str, Path]] = None,
    app_mode: Optional[str] = None,
    project_root: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """
    Resolve SQLite backup directory cleanly:
    1. Explicit backup_dir argument
    2. BACKUP_DIR environment variable
    3. Mode-based location:
       - desktop: %LOCALAPPDATA%\\com.krushidhan.erp\\backups
       - vercel : /tmp/krushidhan_backups
       - local  : <repo>/backups
    """
    env_map = env if env is not None else os.environ

    if backup_dir is not None:
        return Path(backup_dir)

    env_backup_dir = env_map.get("BACKUP_DIR")
    if env_backup_dir and str(env_backup_dir).strip():
        return Path(str(env_backup_dir).strip())

    mode = resolve_app_mode(app_mode=app_mode, env=env_map)
    if mode == "desktop":
        return get_desktop_app_dir(env=env_map) / "backups"
    if mode == "vercel":
        return Path("/tmp/krushidhan_backups")
    return (project_root or PROJECT_ROOT) / "backups"


def is_cloud_sync_allowed(
    app_mode: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """
    MongoDB cloud snapshot sync is ONLY allowed in Vercel mode.
    Desktop mode and local mode strictly forbid cloud sync even if MONGODB_URI is present.
    """
    env_map = env if env is not None else os.environ
    mode = resolve_app_mode(app_mode=app_mode, env=env_map)
    if mode != "vercel":
        return False
    return bool(env_map.get("MONGODB_URI") or env_map.get("MONGO_URL"))


def validate_jwt_secret(secret: Optional[str], source: str = "JWT_SECRET") -> str:
    """
    Validate that a configured JWT signing key meets cryptographic requirements.
    Rejects missing, blank, short (<32 chars), low-entropy, or revoked default secrets.
    """
    if secret is None:
        raise JWTConfigurationError(f"{source} is not configured.")

    cleaned = str(secret).strip()
    if not cleaned:
        raise JWTConfigurationError(f"{source} cannot be blank.")

    if len(cleaned) < MIN_JWT_SECRET_LENGTH:
        raise JWTConfigurationError(
            f"{source} is too short ({len(cleaned)} chars). Require at least {MIN_JWT_SECRET_LENGTH} characters."
        )

    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    if digest in _REVOKED_JWT_SECRET_HASHES:
        raise JWTConfigurationError(
            f"{source} uses a retired/leaked default key. Rotate to a strong random secret."
        )

    if len(set(cleaned)) < 4:
        raise JWTConfigurationError(
            f"{source} has insufficient entropy. Use a high-entropy random secret."
        )

    return cleaned


def _load_or_create_secret_file(secret_file: Path) -> str:
    """Thread-safely load and validate a persistent per-instance secret file, or create one atomically."""
    with _JWT_SECRET_LOCK:
        if secret_file.exists():
            try:
                existing = secret_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise JWTConfigurationError(
                    f"Unable to read JWT secret file at '{secret_file}': {exc}"
                ) from exc
            return validate_jwt_secret(existing, source=str(secret_file))

        secret_file.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_urlsafe(48)
        tmp_file = secret_file.with_name(f".jwt_secret.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp_file.write_text(generated, encoding="utf-8")
            try:
                os.chmod(tmp_file, 0o600)
            except OSError:
                pass
            os.replace(str(tmp_file), str(secret_file))
            try:
                os.chmod(secret_file, 0o600)
            except OSError:
                pass
        finally:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError:
                    pass
        return generated


def get_jwt_secret(
    app_mode: Optional[str] = None,
    project_root: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """
    Retrieve and validate cryptographic JWT signing key (fail-closed, zero hardcoded fallbacks):
    - If JWT_SECRET env var is set (even if empty), strictly validate it and fail closed on weak/revoked values.
    - In 'vercel' mode or APP_ENV='production' (non-desktop): require JWT_SECRET env var explicitly.
    - In 'desktop' mode: persist/validate per-install random key at %LOCALAPPDATA%\\com.krushidhan.erp\\.jwt_secret.
    - In 'local' non-production dev mode: persist/validate random key at <project_root>/data/.jwt_secret.
    """
    if env is None:
        load_runtime_dotenv(app_mode=app_mode, project_root=project_root)

    env_map = env if env is not None else os.environ
    mode = resolve_app_mode(app_mode=app_mode, env=env_map)

    if "JWT_SECRET" in env_map:
        return validate_jwt_secret(env_map.get("JWT_SECRET"), source="JWT_SECRET")

    app_env = str(env_map.get("APP_ENV", "")).strip().lower()
    if mode == "vercel" or (app_env == "production" and mode != "desktop"):
        raise JWTConfigurationError(
            "JWT_SECRET environment variable (>= 32 chars) is required in Vercel and production deployments."
        )

    if mode == "desktop":
        app_dir = get_desktop_app_dir(env=env_map)
        return _load_or_create_secret_file(app_dir / ".jwt_secret")

    root = project_root or PROJECT_ROOT
    return _load_or_create_secret_file(root / "data" / ".jwt_secret")


def is_frozen_bundle() -> bool:
    """Return True when executing inside a frozen PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def get_bundle_root() -> Path:
    """
    Resolve read-only bundled resource root:
    - Frozen PyInstaller runtime (one-file or one-dir): sys._MEIPASS (or executable dir fallback)
    - Source checkout: PROJECT_ROOT
    """
    if is_frozen_bundle():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


def resolve_bundled_resource(*parts: str) -> Path:
    """Resolve a bundled read-only asset relative to bundle/project root."""
    return get_bundle_root().joinpath(*parts)


def validate_bundled_assets(bundle_root: Optional[Path] = None) -> List[Path]:
    """
    Verify all mandatory read-only application assets exist and are non-empty.
    Raises FileNotFoundError loudly if any required asset is missing.
    """
    root = bundle_root if bundle_root is not None else get_bundle_root()
    missing: List[str] = []
    verified: List[Path] = []
    for parts in REQUIRED_DESKTOP_ASSETS:
        candidate = root.joinpath(*parts)
        try:
            is_valid = candidate.is_file() and candidate.stat().st_size > 0
        except OSError:
            is_valid = False
        if not is_valid:
            missing.append("/".join(parts))
        else:
            verified.append(candidate)

    if missing:
        raise FileNotFoundError(
            "Required application assets are missing from bundle root "
            f"'{root}': {', '.join(missing)}"
        )
    return verified


def resolve_log_dir(
    log_dir: Optional[Union[str, Path]] = None,
    app_mode: Optional[str] = None,
    project_root: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """
    Resolve runtime log directory cleanly:
    1. Explicit log_dir argument
    2. LOG_DIR environment variable
    3. Mode-based location:
       - desktop: %LOCALAPPDATA%\\com.krushidhan.erp\\logs
       - vercel : /tmp/krushidhan_logs
       - local  : <repo>/logs
    """
    env_map = env if env is not None else os.environ
    if log_dir is not None:
        return Path(log_dir)

    env_log_dir = env_map.get("LOG_DIR")
    if env_log_dir and str(env_log_dir).strip():
        return Path(str(env_log_dir).strip())

    mode = resolve_app_mode(app_mode=app_mode, env=env_map)
    if mode == "desktop":
        return get_desktop_app_dir(env=env_map) / "logs"
    if mode == "vercel":
        return Path("/tmp/krushidhan_logs")
    return (project_root or PROJECT_ROOT) / "logs"


def get_desktop_lock_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """Resolve OS single-instance lock file path inside the desktop application data directory."""
    return get_desktop_app_dir(env=env) / ".sidecar.lock"


def get_runtime_summary(
    db_path: Optional[Union[str, Path]] = None,
    backup_dir: Optional[Union[str, Path]] = None,
    app_mode: Optional[str] = None,
) -> Dict[str, str]:
    """Return a clean diagnostic summary of resolved mode and storage paths."""
    mode = resolve_app_mode(app_mode=app_mode)
    resolved_db = resolve_db_path(db_path=db_path, app_mode=mode)
    resolved_backup = resolve_backup_dir(backup_dir=backup_dir, app_mode=mode)
    return {
        "app_mode": mode,
        "db_path": str(resolved_db),
        "backup_dir": str(resolved_backup),
        "cloud_sync_allowed": str(is_cloud_sync_allowed(app_mode=mode)),
    }
