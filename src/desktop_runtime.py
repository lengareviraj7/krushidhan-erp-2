"""
Desktop Sidecar Runtime Security, Redacted Rotating Logging, and OS Single-Instance Locking.

All components here activate cleanly without altering `local` or `vercel` browser/cloud modes:
- Desktop HTTP security middleware validates Launch Token (header-only, constant-time),
  Host header (strict loopback + bound port), Origin header (Tauri/loopback allowlist),
  and safe CORS preflight headers.
- Rotating file logger writes bounded logs under %LOCALAPPDATA%\\com.krushidhan.erp\\logs\\sidecar.log
  while redacting secrets, tokens, passwords, and raw headers.
- SingleInstanceLock uses OS file locks (msvcrt.locking / fcntl.flock) held open for process
  lifetime so concurrent instances fail cleanly without touching SQLite.
"""
from __future__ import annotations

import hmac
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import FrozenSet, Iterable, List, Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from src.config import get_desktop_lock_path, resolve_log_dir

LAUNCH_TOKEN_HEADER = "X-Krushidhan-Launch-Token"
ALLOWED_DESKTOP_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
ALLOWED_DESKTOP_HEADERS = f"Authorization, Content-Type, {LAUNCH_TOKEN_HEADER}"
EXPOSED_DESKTOP_HEADERS = "Content-Disposition"
TRUSTED_TAURI_ORIGINS: FrozenSet[str] = frozenset(
    {
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
    }
)
SUPPORTED_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
DEFAULT_LOG_BACKUP_COUNT = 5

logger = logging.getLogger("agri_erp.desktop")


# ---------------------------------------------------------------------------
# 1. Secret Redaction & Bounded Rotating File Logging
# ---------------------------------------------------------------------------
_SECRET_LOCK = threading.Lock()
_REDACTED_SECRETS: set[str] = set()

_HEADER_AUTH_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[A-Za-z0-9\-_.=+/~]{4,}"
)
_HEADER_LAUNCH_RE = re.compile(
    r"(?i)(x-krushidhan-launch-token\s*[:=]\s*)[A-Za-z0-9\-_.=+/~]{4,}"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9\-_.=+/~]{8,}")
_KV_SECRET_RE = re.compile(
    r"""(?i)(["']?(?:password|old_password|new_password|jwt_secret|launch_token|setup_token|access_token|recovery_key|confirmed_recovery_key)["']?\s*[:=]\s*["']?)([^"',\s}\]\r\n]+)"""
)
_QUERY_SECRET_RE = re.compile(
    r"""(?i)([?&](?:token|launch_token|jwt|secret|password|recovery_key|confirmed_recovery_key)=)([^&\s"'#]+)"""
)
_RECOVERY_KEY_TOKEN_RE = re.compile(r"(?i)\bKDHN(?:-[A-Z2-7]{4,6}){6,}\b")


def register_redacted_secret(secret: Optional[str]) -> None:
    """Register an in-memory runtime secret so log filters always scrub it."""
    if not secret:
        return
    cleaned = str(secret).strip()
    if len(cleaned) < 6:
        return
    with _SECRET_LOCK:
        _REDACTED_SECRETS.add(cleaned)


def clear_redacted_secrets() -> None:
    """Clear registered secret strings (primarily for isolated tests)."""
    with _SECRET_LOCK:
        _REDACTED_SECRETS.clear()


def redact_sensitive_text(text: Optional[str], extra_secrets: Iterable[str] = ()) -> str:
    """Scrub registered runtime secrets, auth headers, passwords, and tokens from log text."""
    if text is None:
        return ""
    out = str(text)
    if not out:
        return out

    with _SECRET_LOCK:
        candidates = sorted(_REDACTED_SECRETS, key=len, reverse=True)
    for sec in extra_secrets:
        if sec and len(str(sec).strip()) >= 6:
            candidates.append(str(sec).strip())

    for sec in candidates:
        if sec in out:
            out = out.replace(sec, "[REDACTED]")

    out = _HEADER_AUTH_RE.sub(r"\1[REDACTED]", out)
    out = _HEADER_LAUNCH_RE.sub(r"\1[REDACTED]", out)
    out = _BEARER_TOKEN_RE.sub(r"\1[REDACTED]", out)
    out = _KV_SECRET_RE.sub(r"\1[REDACTED]", out)
    out = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", out)
    out = _RECOVERY_KEY_TOKEN_RE.sub("[REDACTED_RECOVERY_KEY]", out)
    return out


class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs credentials/tokens from formatted log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            record.msg = redact_sensitive_text(rendered)
            record.args = ()
        except Exception:
            record.msg = redact_sensitive_text(str(record.msg))
            record.args = ()
        return True


class SecretRedactingFormatter(logging.Formatter):
    """Formatter ensuring final formatted message and traceback are redacted."""

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return redact_sensitive_text(formatted)


_DESKTOP_LOG_HANDLER: Optional[RotatingFileHandler] = None
_DESKTOP_LOG_MANAGED_LOGGERS = (
    "",
    "agri_erp",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "py.warnings",
)


def setup_desktop_logging(
    log_dir: Optional[Path] = None,
    app_mode: str = "desktop",
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
    level: int = logging.INFO,
) -> Path:
    """
    Configure rotating file logging under %LOCALAPPDATA%\\com.krushidhan.erp\\logs\\sidecar.log.
    Detaches stdout/stderr handlers from root/uvicorn loggers so stdout remains reserved
    for the single readiness JSON line.
    """
    global _DESKTOP_LOG_HANDLER

    teardown_desktop_logging()

    target_dir = resolve_log_dir(log_dir=log_dir, app_mode=app_mode)
    target_dir.mkdir(parents=True, exist_ok=True)
    log_file = target_dir / "sidecar.log"

    handler = RotatingFileHandler(
        filename=str(log_file),
        maxBytes=max(1024, int(max_bytes)),
        backupCount=max(1, int(backup_count)),
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.addFilter(SecretRedactingFilter())
    handler.setFormatter(
        SecretRedactingFormatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logging.captureWarnings(True)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    root_logger.addHandler(handler)

    for logger_name in _DESKTOP_LOG_MANAGED_LOGGERS:
        if not logger_name:
            continue
        lgr = logging.getLogger(logger_name)
        lgr.handlers.clear()
        lgr.setLevel(level)
        lgr.propagate = True

    _DESKTOP_LOG_HANDLER = handler
    return log_file


def teardown_desktop_logging() -> None:
    """Flush and close the managed desktop rotating file handler cleanly."""
    global _DESKTOP_LOG_HANDLER
    if _DESKTOP_LOG_HANDLER is not None:
        root_logger = logging.getLogger()
        if _DESKTOP_LOG_HANDLER in root_logger.handlers:
            root_logger.removeHandler(_DESKTOP_LOG_HANDLER)
        try:
            _DESKTOP_LOG_HANDLER.flush()
            _DESKTOP_LOG_HANDLER.close()
        except Exception:
            pass
        _DESKTOP_LOG_HANDLER = None


# ---------------------------------------------------------------------------
# 2. OS-Level Single-Instance Lock (POSIX flock / Windows msvcrt)
# ---------------------------------------------------------------------------
class DesktopLockError(RuntimeError):
    """Raised when another sidecar instance already holds the app-dir lock."""


class SingleInstanceLock:
    """
    Cross-platform OS file lock scoped to the active desktop application directory.
    Held open for the process lifetime so OS cleanup happens automatically on crash.
    Does not unlink the lock file on release to eliminate unlink-inode lock races.
    """

    def __init__(self, lock_path: Optional[Path] = None):
        self.lock_path = Path(lock_path) if lock_path is not None else get_desktop_lock_path()
        self._fd: Optional[int] = None

    @property
    def is_locked(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
        fd = os.open(str(self.lock_path), flags, 0o600)
        try:
            # Ensure >= 1 byte so Windows msvcrt.locking locks byte 0 reliably
            st = os.fstat(fd)
            if st.st_size == 0:
                os.write(fd, b"\n")
            os.lseek(fd, 0, os.SEEK_SET)

            if sys.platform.startswith("win"):
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            info = f"pid={os.getpid()} started={int(time.time())}\n".encode("ascii")
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, info)
            os.ftruncate(fd, len(info))
            self._fd = fd
        except (BlockingIOError, PermissionError, OSError) as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise DesktopLockError(
                f"Another Krushidhan ERP sidecar instance is already running for "
                f"'{self.lock_path.parent}'."
            ) from exc

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if sys.platform.startswith("win"):
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


# ---------------------------------------------------------------------------
# 3. Desktop HTTP Security Middleware State & Verification
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DesktopSecurityConfig:
    """Immutable snapshot of desktop sidecar HTTP security enforcement settings."""

    enabled: bool = False
    launch_token: Optional[str] = None
    bound_host: str = "127.0.0.1"
    bound_port: Optional[int] = None
    allowed_hosts: FrozenSet[str] = field(default_factory=frozenset)
    allowed_origins: FrozenSet[str] = field(default_factory=frozenset)


_SECURITY_LOCK = threading.Lock()
_SECURITY_CONFIG = DesktopSecurityConfig()


def _build_allowed_hosts(bound_port: int) -> FrozenSet[str]:
    return frozenset(f"{host}:{bound_port}" for host in SUPPORTED_LOOPBACK_HOSTS)


def _build_allowed_origins(bound_port: int) -> FrozenSet[str]:
    loopback_origins = {f"http://{host}:{bound_port}" for host in SUPPORTED_LOOPBACK_HOSTS}
    return TRUSTED_TAURI_ORIGINS | frozenset(loopback_origins)


def configure_desktop_security(
    *,
    enabled: bool = True,
    launch_token: Optional[str],
    bound_port: int,
    bound_host: str = "127.0.0.1",
) -> DesktopSecurityConfig:
    """Configure in-memory desktop sidecar HTTP guard rules for the current runtime."""
    global _SECURITY_CONFIG

    if enabled:
        if bound_port is None or not (1 <= int(bound_port) <= 65535):
            raise ValueError(f"Invalid desktop sidecar bound_port: {bound_port}")
        port_int = int(bound_port)
        token_val = str(launch_token) if launch_token else None
        if token_val:
            register_redacted_secret(token_val)
        cfg = DesktopSecurityConfig(
            enabled=True,
            launch_token=token_val,
            bound_host=str(bound_host or "127.0.0.1"),
            bound_port=port_int,
            allowed_hosts=_build_allowed_hosts(port_int),
            allowed_origins=_build_allowed_origins(port_int),
        )
    else:
        cfg = DesktopSecurityConfig(enabled=False)

    with _SECURITY_LOCK:
        _SECURITY_CONFIG = cfg
    return cfg


def reset_desktop_security() -> None:
    """Reset desktop sidecar HTTP security configuration back to disabled (local/test default)."""
    global _SECURITY_CONFIG
    with _SECURITY_LOCK:
        _SECURITY_CONFIG = DesktopSecurityConfig(enabled=False)


def get_desktop_security_config() -> DesktopSecurityConfig:
    with _SECURITY_LOCK:
        return _SECURITY_CONFIG


def _json_security_error(
    status_code: int,
    detail: str,
    *,
    cors_origin: Optional[str] = None,
) -> JSONResponse:
    headers = {"Cache-Control": "no-store", "Vary": "Origin"}
    if cors_origin:
        headers["Access-Control-Allow-Origin"] = cors_origin
        headers["Access-Control-Expose-Headers"] = EXPOSED_DESKTOP_HEADERS
    return JSONResponse(status_code=status_code, content={"detail": detail}, headers=headers)


def apply_desktop_cors_headers(response: Response, cors_origin: Optional[str]) -> Response:
    """Attach exact-origin CORS exposure headers cleanly to valid desktop responses."""
    if cors_origin:
        response.headers["Access-Control-Allow-Origin"] = cors_origin
        response.headers["Access-Control-Expose-Headers"] = EXPOSED_DESKTOP_HEADERS
        existing_vary = response.headers.get("Vary", "")
        if "origin" not in existing_vary.lower():
            response.headers["Vary"] = f"{existing_vary}, Origin".strip(", ")
    return response


def inspect_desktop_request(
    request: Request,
    config: Optional[DesktopSecurityConfig] = None,
) -> tuple[Optional[Response], Optional[str]]:
    """
    Enforce desktop sidecar HTTP security controls when enabled:
    - Strict Host header validation against loopback + bound port
    - Strict Origin allowlist when Origin header is present
    - Safe CORS OPTIONS preflight handling for trusted origins
    - Constant-time X-Krushidhan-Launch-Token validation on all /api/* routes (header-only)

    Returns `(early_response, validated_cors_origin)`. If `early_response` is not None,
    the caller MUST return it immediately instead of invoking the route handler.
    """
    cfg = config if config is not None else get_desktop_security_config()
    if not cfg.enabled:
        return None, None

    # 1. Strict single Host header check matching exact loopback host + bound port
    host_headers: List[str] = request.headers.getlist("host")
    if len(host_headers) != 1:
        return _json_security_error(403, "Forbidden: invalid Host header."), None
    normalized_host = host_headers[0].strip().lower()
    if normalized_host not in cfg.allowed_hosts:
        return _json_security_error(403, "Forbidden: unexpected Host header for desktop sidecar."), None

    # 2. Strict Origin validation whenever Origin header is present
    origin_headers: List[str] = request.headers.getlist("origin")
    if len(origin_headers) > 1:
        return _json_security_error(403, "Forbidden: multiple Origin headers rejected."), None

    validated_origin: Optional[str] = None
    if len(origin_headers) == 1:
        raw_origin = origin_headers[0].strip()
        normalized_origin = raw_origin.lower()
        if (
            not raw_origin
            or normalized_origin == "null"
            or normalized_origin not in cfg.allowed_origins
        ):
            return _json_security_error(403, "Forbidden: untrusted Origin in desktop mode."), None
        validated_origin = raw_origin

    # 3. Minimal safe CORS preflight handling (OPTIONS)
    if request.method.upper() == "OPTIONS":
        if validated_origin is None:
            return _json_security_error(403, "Forbidden: CORS preflight requires a trusted Origin."), None
        preflight_headers = {
            "Access-Control-Allow-Origin": validated_origin,
            "Access-Control-Allow-Methods": ALLOWED_DESKTOP_METHODS,
            "Access-Control-Allow-Headers": ALLOWED_DESKTOP_HEADERS,
            "Access-Control-Expose-Headers": EXPOSED_DESKTOP_HEADERS,
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
            "Cache-Control": "no-store",
        }
        return Response(status_code=204, headers=preflight_headers), validated_origin

    # 4. Launch-Token guard on ALL /api/* paths (strictly header-only; query params ignored)
    path = request.url.path or ""
    if path == "/api" or path.startswith("/api/"):
        if not cfg.launch_token:
            return _json_security_error(
                503,
                "Sidecar launch security token is not initialized.",
                cors_origin=validated_origin,
            ), validated_origin

        token_headers = request.headers.getlist(LAUNCH_TOKEN_HEADER.lower())
        if len(token_headers) == 0:
            return _json_security_error(
                401,
                f"Unauthorized: missing {LAUNCH_TOKEN_HEADER} header.",
                cors_origin=validated_origin,
            ), validated_origin
        if len(token_headers) != 1:
            return _json_security_error(
                403,
                f"Forbidden: duplicate {LAUNCH_TOKEN_HEADER} header rejected.",
                cors_origin=validated_origin,
            ), validated_origin

        provided = token_headers[0].strip()
        if not provided:
            return _json_security_error(
                401,
                f"Unauthorized: empty {LAUNCH_TOKEN_HEADER} header.",
                cors_origin=validated_origin,
            ), validated_origin

        try:
            valid = hmac.compare_digest(
                provided.encode("utf-8"),
                cfg.launch_token.encode("utf-8"),
            )
        except Exception:
            valid = False

        if not valid:
            return _json_security_error(
                403,
                f"Forbidden: invalid {LAUNCH_TOKEN_HEADER}.",
                cors_origin=validated_origin,
            ), validated_origin

    return None, validated_origin
