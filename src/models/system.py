"""
Domain Data Models for System Users, Company Profile, and Audit Logs.
"""
from __future__ import annotations

import re
from typing import Optional
from pydantic import BaseModel

_UPI_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,255}@[A-Za-z][A-Za-z0-9.-]{1,63}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_and_validate_upi_id(upi_id: Optional[str]) -> Optional[str]:
    """Strip and validate a UPI VPA string; returns None when empty/None, raises ValueError if invalid."""
    if upi_id is None:
        return None
    cleaned = upi_id.strip()
    if not cleaned:
        return None
    if any(ch.isspace() for ch in cleaned) or cleaned.count("@") != 1 or ".." in cleaned:
        raise ValueError("Invalid UPI ID format. Expected format like 'shopname@upi' or '9876543210@ybl'.")
    local, provider = cleaned.split("@", 1)
    if local.endswith((".", "-", "_")) or provider.endswith((".", "-")) or not _UPI_ID_RE.match(cleaned):
        raise ValueError("Invalid UPI ID format. Expected format like 'shopname@upi' or '9876543210@ybl'.")
    return cleaned


def normalize_and_validate_email(email: Optional[str]) -> Optional[str]:
    """Strip and lightly validate an email string; returns None when empty/None."""
    if email is None:
        return None
    cleaned = email.strip()
    if not cleaned:
        return None
    if not _EMAIL_RE.match(cleaned):
        raise ValueError("Invalid shop email address format.")
    return cleaned


class User(BaseModel):
    user_id: Optional[int] = None
    username: str
    password_hash: Optional[str] = None
    full_name: Optional[str] = None
    role: str = "OPERATOR"  # 'ADMIN', 'MANAGER', 'OPERATOR'
    is_active: int = 1
    created_at: Optional[str] = None


class CompanySettings(BaseModel):
    setting_id: Optional[int] = None
    company_name: str
    owner_name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: str = "Maharashtra"
    pincode: Optional[str] = None
    mobile: Optional[str] = None
    email: Optional[str] = None
    gstin: Optional[str] = None
    dl_fertilizer: Optional[str] = None
    dl_pesticide: Optional[str] = None
    dl_seed: Optional[str] = None
    upi_id: Optional[str] = None
    bank_name: Optional[str] = None
    account_no: Optional[str] = None
    ifsc_code: Optional[str] = None
    invoice_terms: Optional[str] = None
    is_thermal_print: int = 0
    default_invoice_size: str = "A4"  # 'A4', 'A5', '3INCH_THERMAL'
    updated_at: Optional[str] = None


class CompanySettingsUpdate(BaseModel):
    """Partial/full update payload for shop settings; omitted fields preserve stored values."""

    company_name: Optional[str] = None
    owner_name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    mobile: Optional[str] = None
    email: Optional[str] = None
    gstin: Optional[str] = None
    dl_fertilizer: Optional[str] = None
    dl_pesticide: Optional[str] = None
    dl_seed: Optional[str] = None
    upi_id: Optional[str] = None
    bank_name: Optional[str] = None
    account_no: Optional[str] = None
    ifsc_code: Optional[str] = None
    invoice_terms: Optional[str] = None
    is_thermal_print: Optional[int] = None
    default_invoice_size: Optional[str] = None


class AuditLogEntry(BaseModel):
    log_id: Optional[int] = None
    user_id: Optional[int] = None
    action_type: str  # 'INSERT', 'UPDATE', 'DELETE', 'LOGIN', 'BACKUP'
    table_name: Optional[str] = None
    record_id: Optional[int] = None
    details: Optional[str] = None
    created_at: Optional[str] = None


class BackupLogEntry(BaseModel):
    backup_id: Optional[int] = None
    file_path: str
    backup_type: str  # 'MANUAL', 'SCHEDULED', 'BEFORE_UPGRADE'
    backup_size_bytes: Optional[int] = None
    status: str = "SUCCESS"
    created_at: Optional[str] = None
