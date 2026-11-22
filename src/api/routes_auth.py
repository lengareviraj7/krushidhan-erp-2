"""
Authentication API Endpoints for Krushidhan ERP.
Handles First-Run Admin Setup, Login, Session verification, Password Changes, and User Management.
"""
from __future__ import annotations

import hmac
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from src.api.auth_middleware import get_current_user, require_admin_user
from src.db.connection import get_db_manager
from src.models.system import (
    CompanySettings,
    normalize_and_validate_email,
    normalize_and_validate_upi_id,
)
from src.repositories.system_repository import SystemRepository
from src.services.auth_service import AuthService, InitialSetupAlreadyCompletedError

router = APIRouter(prefix="/api/auth", tags=["Authentication & Security"])

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
_ALLOWED_DESKTOP_ORIGINS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "tauri.localhost",
    }
)


class LoginRequest(BaseModel):
    username: str
    password: str


class FirstRunSetupRequest(BaseModel):
    username: str
    password: str
    full_name: str
    shop_name: Optional[str] = None
    owner_name: Optional[str] = None
    mobile: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: str = "Maharashtra"
    pincode: Optional[str] = None
    gstin: Optional[str] = None
    dl_fertilizer: Optional[str] = None
    dl_pesticide: Optional[str] = None
    dl_seed: Optional[str] = None
    upi_id: Optional[str] = None
    setup_token: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class AdminResetPasswordRequest(BaseModel):
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    full_name: str
    role: str = "OPERATOR"


class UpdateUserStatusRequest(BaseModel):
    is_active: bool


def _build_shop_metadata(db) -> Dict[str, str]:
    settings = SystemRepository(db).get_company_settings()
    return {
        "name": settings.company_name or "Agri-Input Shop",
        "owner": getattr(settings, "owner_name", "") or "",
        "contact": settings.mobile or "",
    }


def _extract_client_host(request: Request) -> str:
    raw = (request.client.host if request.client else "") or ""
    cleaned = raw.strip().lower()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    if cleaned.startswith("::ffff:"):
        cleaned = cleaned[7:]
    return cleaned


def _has_remote_proxy_headers(request: Request) -> bool:
    for header_name in ("x-forwarded-for", "x-real-ip", "forwarded"):
        val = request.headers.get(header_name)
        if val and val.strip():
            return True
    return False


def _enforce_setup_admin_access(request: Request, db, payload_token: Optional[str]) -> None:
    """
    Prevent unauthorized network/remote clients from claiming the initial administrator account:
    - In 'desktop' mode: permit loopback clients only (no reverse-proxy headers, no remote origins).
    - If SETUP_ADMIN_TOKEN is configured: require constant-time match via X-Setup-Token or payload setup_token.
    - For non-desktop remote or Vercel initial setup without SETUP_ADMIN_TOKEN: fail closed (403).
    """
    app_mode = getattr(db, "app_mode", "local")
    client_host = _extract_client_host(request)
    is_loopback = (client_host in _LOOPBACK_HOSTS) and not _has_remote_proxy_headers(request)

    origin = request.headers.get("origin")
    if origin and app_mode == "desktop":
        parsed_origin = urlparse(origin.strip())
        origin_host = (parsed_origin.hostname or "").lower()
        if origin_host not in _ALLOWED_DESKTOP_ORIGINS:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cross-origin initial administrator setup is forbidden in desktop mode.",
            )

    if app_mode == "desktop" and not is_loopback:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Initial administrator setup in desktop mode is restricted to the local machine.",
        )

    configured_token = os.environ.get("SETUP_ADMIN_TOKEN", "").strip()
    provided_token = (request.headers.get("x-setup-token") or payload_token or "").strip()

    if configured_token:
        if not provided_token or not hmac.compare_digest(provided_token, configured_token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Valid setup authorization token required to claim initial administrator.",
            )
        return

    if app_mode == "vercel" or not is_loopback:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Remote initial administrator setup is blocked unless SETUP_ADMIN_TOKEN is configured "
                "or setup is performed locally on the host machine."
            ),
        )


@router.get("/setup-status")
def get_setup_status():
    """Return non-sensitive first-run installation setup state without exposing phone PII."""
    db = get_db_manager()
    svc = AuthService(db)
    sys_repo = SystemRepository(db)
    has_profile = svc.has_company_profile()
    settings = sys_repo.get_company_settings() if has_profile else CompanySettings(company_name="Agri-Input Shop ERP")
    return {
        "needs_setup": svc.needs_initial_setup(),
        "has_company_profile": has_profile,
        "app_mode": getattr(db, "app_mode", "local"),
        "shop_name": settings.company_name,
        "shop_mobile": "",
        "setup_token_required": bool(os.environ.get("SETUP_ADMIN_TOKEN", "").strip()),
    }


@router.post("/setup-admin")
def setup_initial_admin(payload: FirstRunSetupRequest, request: Request):
    """
    Create the very first shop owner Administrator account on a clean installation.
    Enforces origin/loopback/token checks and commits atomically inside BEGIN IMMEDIATE so
    concurrent requests cannot create duplicate first administrators.
    """
    db = get_db_manager()
    _enforce_setup_admin_access(request, db, payload.setup_token)
    svc = AuthService(db)
    if not svc.needs_initial_setup():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Initial administrator setup has already been completed. Please log in.",
        )

    company_settings = None
    try:
        if payload.shop_name and payload.shop_name.strip():
            owner_val = (payload.owner_name or payload.full_name or "").strip() or None
            company_settings = CompanySettings(
                company_name=payload.shop_name.strip(),
                owner_name=owner_val,
                mobile=payload.mobile.strip() if payload.mobile and payload.mobile.strip() else None,
                email=normalize_and_validate_email(payload.email),
                address=payload.address.strip() if payload.address and payload.address.strip() else None,
                city=payload.city.strip() if payload.city and payload.city.strip() else None,
                state=(payload.state or "Maharashtra").strip(),
                pincode=payload.pincode.strip() if payload.pincode and payload.pincode.strip() else None,
                gstin=payload.gstin.strip().upper() if payload.gstin and payload.gstin.strip() else None,
                dl_fertilizer=payload.dl_fertilizer.strip() if payload.dl_fertilizer and payload.dl_fertilizer.strip() else None,
                dl_pesticide=payload.dl_pesticide.strip() if payload.dl_pesticide and payload.dl_pesticide.strip() else None,
                dl_seed=payload.dl_seed.strip() if payload.dl_seed and payload.dl_seed.strip() else None,
                upi_id=normalize_and_validate_upi_id(payload.upi_id),
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    try:
        user = svc.setup_initial_admin(
            username=payload.username,
            plain_password=payload.password,
            full_name=payload.full_name,
            company_settings=company_settings,
        )
    except InitialSetupAlreadyCompletedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    token = svc.create_access_token(
        user_id=user.user_id or 1,
        username=user.username,
        full_name=user.full_name or user.username,
        role=user.role or "ADMIN",
    )

    return {
        "success": True,
        "token": token,
        "user": {
            "user_id": user.user_id,
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role,
        },
        "shop": _build_shop_metadata(db),
    }


@router.post("/login")
def login(payload: LoginRequest):
    """Authenticate User ID & Password and return secure JWT token."""
    db = get_db_manager()
    svc = AuthService(db)
    user = svc.authenticate_user(payload.username, payload.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid User ID or Password",
        )

    token = svc.create_access_token(
        user_id=user.user_id or 1,
        username=user.username,
        full_name=user.full_name or user.username,
        role=user.role or "OPERATOR",
    )

    return {
        "success": True,
        "token": token,
        "user": {
            "user_id": user.user_id,
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role,
        },
        "shop": _build_shop_metadata(db),
    }


@router.get("/me")
def get_current_user_profile(user: Dict[str, Any] = Depends(get_current_user)):
    """Return active authenticated user profile."""
    return {"authenticated": True, "user": user}


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Change logged-in user's password."""
    svc = AuthService(get_db_manager())
    user_id = int(current_user["user_id"])
    success = svc.change_password(user_id, payload.old_password, payload.new_password)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )
    return {"success": True, "message": "Password updated successfully"}


@router.get("/users")
def list_system_users(admin: Dict[str, Any] = Depends(require_admin_user)):
    """Admin Only: List all registered operators and administrators."""
    svc = AuthService(get_db_manager())
    users = svc.list_users()
    return [
        {
            "user_id": u.user_id,
            "username": u.username,
            "full_name": u.full_name,
            "role": u.role,
            "is_active": bool(u.is_active),
            "created_at": str(u.created_at or ""),
        }
        for u in users
    ]


@router.post("/users")
def create_system_user(
    payload: CreateUserRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Admin Only: Create new billing operator or sub-admin."""
    svc = AuthService(get_db_manager())
    try:
        new_id = svc.register_user(
            username=payload.username,
            plain_password=payload.password,
            full_name=payload.full_name,
            role=payload.role,
        )
        return {"success": True, "user_id": new_id, "message": f"User '{payload.username}' created successfully"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/users/{user_id}/status")
def update_user_status(
    user_id: int,
    payload: UpdateUserStatusRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Admin Only: Enable or Disable operator account."""
    if user_id == int(admin.get("user_id", 0)):
        raise HTTPException(status_code=400, detail="Cannot deactivate your own admin account.")
    svc = AuthService(get_db_manager())
    svc.set_user_active_status(user_id, payload.is_active)
    status_text = "Active" if payload.is_active else "Disabled"
    return {"success": True, "message": f"User status updated to {status_text}"}


@router.post("/users/{user_id}/reset-password")
def reset_user_password(
    user_id: int,
    payload: AdminResetPasswordRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Admin Only: Reset password for an operator."""
    svc = AuthService(get_db_manager())
    success = svc.reset_password_by_admin(user_id, payload.new_password)
    if not success:
        raise HTTPException(status_code=404, detail="User not found.")
    return {"success": True, "message": "Password reset successfully"}
