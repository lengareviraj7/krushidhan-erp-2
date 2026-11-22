"""
FastAPI Application Entrypoint for Offline Agri-Input Shop ERP.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from src.api import api_router
from src.config import (
    APP_VERSION,
    get_jwt_secret,
    is_frozen_bundle,
    load_runtime_dotenv,
    resolve_app_mode,
    resolve_bundled_resource,
    validate_bundled_assets,
)
from src.db.connection import DatabaseMaintenanceError, get_db_manager
from src.desktop_runtime import (
    apply_desktop_cors_headers,
    inspect_desktop_request,
    redact_sensitive_text,
    register_redacted_secret,
)

logger = logging.getLogger("agri_erp.app")

BASE_DIR = resolve_bundled_resource("src")
WEB_DIR = resolve_bundled_resource("src", "web")
STATIC_DIR = WEB_DIR / "static"
TEMPLATES_DIR = WEB_DIR / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ensure assets, JWT config, schema, mode-appropriate seed data, and migrations succeed on start."""
    mode = resolve_app_mode()
    if is_frozen_bundle() or mode == "desktop":
        validate_bundled_assets()
    load_runtime_dotenv()
    jwt_secret = get_jwt_secret()
    register_redacted_secret(jwt_secret)
    db = get_db_manager()
    db.initialize_database(include_seed=True)
    if db.db_path != ":memory:":
        try:
            from src.services.backup_service import BackupService

            BackupService(db).cleanup_orphaned_staging_files()
        except Exception:
            logger.debug("Ignoring non-fatal startup orphan staging cleanup error.", exc_info=True)
        if not db.is_vercel:
            try:
                from src.services.backup_scheduler import start_backup_scheduler

                start_backup_scheduler(db_manager=db)
            except Exception:
                logger.warning("Non-fatal BackupScheduler startup warning.", exc_info=True)
    try:
        yield
    finally:
        try:
            from src.services.backup_scheduler import stop_backup_scheduler

            stop_backup_scheduler(timeout=5.0)
        except Exception:
            logger.debug("Ignoring lifespan BackupScheduler shutdown error.", exc_info=True)
        try:
            db.checkpoint_wal("PASSIVE")
        except Exception:
            logger.debug("Ignoring lifespan shutdown WAL checkpoint error.", exc_info=True)


app = FastAPI(
    title="Krishi Agri-Input Shop ERP",
    description="Offline Billing & Inventory Management for Fertilizers, Seeds, and Pesticides",
    version=APP_VERSION,
    lifespan=lifespan,
)


def _maintenance_503_response(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": detail},
        headers={"Retry-After": "2"},
    )


@app.exception_handler(DatabaseMaintenanceError)
async def database_maintenance_exception_handler(request: Request, exc: DatabaseMaintenanceError):
    """Return HTTP 503 whenever database operations are blocked for exclusive maintenance."""
    return _maintenance_503_response(str(exc))


_SENSITIVE_VALIDATION_FIELDS = frozenset(
    {
        "recovery_key",
        "confirmed_recovery_key",
        "password",
        "old_password",
        "new_password",
        "jwt_secret",
        "token",
    }
)


def _sanitize_validation_value(val):
    if isinstance(val, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in _SENSITIVE_VALIDATION_FIELDS else _sanitize_validation_value(v))
            for k, v in val.items()
        }
    if isinstance(val, list):
        return [_sanitize_validation_value(v) for v in val]
    if isinstance(val, tuple):
        return [_sanitize_validation_value(v) for v in val]
    if isinstance(val, BaseException):
        return redact_sensitive_text(str(val))
    if isinstance(val, str):
        return redact_sensitive_text(val)
    if isinstance(val, bytes):
        return "<binary>"
    return val


@app.exception_handler(RequestValidationError)
async def sanitized_validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return standard HTTP 422 while guaranteeing secrets/Recovery Keys are never echoed back."""
    sanitized_errors = []
    for err in exc.errors():
        cleaned = {}
        for k, v in err.items():
            if k == "input":
                loc_parts = {str(p).lower() for p in (err.get("loc") or ())}
                if loc_parts & _SENSITIVE_VALIDATION_FIELDS:
                    cleaned[k] = "[REDACTED]"
                else:
                    cleaned[k] = _sanitize_validation_value(v)
            elif k == "msg" and isinstance(v, str):
                cleaned[k] = redact_sensitive_text(v)
            elif k == "ctx" and isinstance(v, dict):
                cleaned[k] = _sanitize_validation_value(v)
            else:
                cleaned[k] = _sanitize_validation_value(v)
        sanitized_errors.append(cleaned)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(sanitized_errors)})


@app.middleware("http")
async def database_maintenance_middleware(request: Request, call_next):
    """Block new API requests with HTTP 503 while backup restoration or schema migration holds maintenance."""
    if request.url.path.startswith("/api/") and get_db_manager().is_in_maintenance:
        return _maintenance_503_response(
            "Database maintenance in progress (backup restoration or schema upgrade). Please retry shortly."
        )
    return await call_next(request)


@app.middleware("http")
async def desktop_security_middleware(request: Request, call_next):
    """
    Outermost desktop sidecar HTTP guard (active only when configured by sidecar runtime):
    validates Host, Origin, CORS preflights, and X-Krushidhan-Launch-Token on /api/*.
    """
    early_response, cors_origin = inspect_desktop_request(request)
    if early_response is not None:
        return early_response
    response = await call_next(request)
    return apply_desktop_cors_headers(response, cors_origin)


# Validate or resolve static files & templates (never auto-mkdir missing dirs in frozen/desktop runtime)
if not STATIC_DIR.exists():
    if is_frozen_bundle() or resolve_app_mode() == "desktop":
        raise FileNotFoundError(f"Required static assets directory is missing: {STATIC_DIR}")
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
if not TEMPLATES_DIR.exists():
    if is_frozen_bundle() or resolve_app_mode() == "desktop":
        raise FileNotFoundError(f"Required templates directory is missing: {TEMPLATES_DIR}")
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Include API routes
app.include_router(api_router)


@app.get("/api/health", tags=["System"])
def health_check():
    """Lightweight readiness endpoint verifying SQLite connectivity and initialized schema version."""
    db = get_db_manager()
    if db.is_in_maintenance:
        return _maintenance_503_response(
            "Database maintenance in progress (backup restoration or schema upgrade). Please retry shortly."
        )

    try:
        conn = db._create_raw_connection(track=True)
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1;")
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='products' LIMIT 1;"
            )
            has_products = cur.fetchone() is not None
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version' LIMIT 1;"
            )
            has_sv = cur.fetchone() is not None
            schema_version = 0
            if has_sv:
                cur.execute("SELECT MAX(version_number) FROM schema_version;")
                row = cur.fetchone()
                schema_version = int(row[0]) if row and row[0] is not None else 0
            cur.close()
        finally:
            conn.close()
    except DatabaseMaintenanceError:
        raise
    except Exception:
        logger.exception("Health check failed while verifying SQLite readiness.")
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "detail": "Database not ready."},
            headers={"Cache-Control": "no-store", "Retry-After": "2"},
        )

    if not has_products or schema_version < 1:
        return JSONResponse(
            status_code=503,
            content={"status": "starting", "detail": "Database schema is not initialized yet."},
            headers={"Cache-Control": "no-store", "Retry-After": "2"},
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "version": APP_VERSION,
            "schema_version": schema_version,
            "app_mode": db.app_mode,
            "desktop_mode": db.is_desktop,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse)
def index_view(request: Request):
    """Render the single-page desktop billing & ERP dashboard."""
    return templates.TemplateResponse(request=request, name="index.html")
