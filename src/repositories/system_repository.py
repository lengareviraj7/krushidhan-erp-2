"""
Data Access Layer (Repository) for System Users, Company Profile, and Audit Trails.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union
from src.models.system import (
    AuditLogEntry,
    BackupLogEntry,
    CompanySettings,
    CompanySettingsUpdate,
    User,
    normalize_and_validate_email,
    normalize_and_validate_upi_id,
)
from src.repositories.base_repository import BaseRepository

_COMPANY_SETTING_FIELDS: Tuple[str, ...] = (
    "company_name",
    "owner_name",
    "address",
    "city",
    "state",
    "pincode",
    "mobile",
    "email",
    "gstin",
    "dl_fertilizer",
    "dl_pesticide",
    "dl_seed",
    "upi_id",
    "bank_name",
    "account_no",
    "ifsc_code",
    "invoice_terms",
    "is_thermal_print",
    "default_invoice_size",
)

_NULLABLE_TEXT_FIELDS = frozenset(
    {
        "owner_name",
        "address",
        "city",
        "pincode",
        "mobile",
        "dl_fertilizer",
        "dl_pesticide",
        "dl_seed",
        "bank_name",
        "account_no",
        "invoice_terms",
    }
)

_ALLOWED_INVOICE_SIZES = frozenset({"A4", "A5", "3INCH_THERMAL"})


def _row_to_company_settings(row_data: Mapping[str, Any]) -> CompanySettings:
    """Safely convert a SQLite row or dict into CompanySettings without failing on NULL defaults."""
    data: Dict[str, Any] = dict(row_data)
    if not data.get("company_name"):
        data["company_name"] = "Krishi Agri Shop"
    if not data.get("state"):
        data["state"] = "Maharashtra"
    data["is_thermal_print"] = 0 if data.get("is_thermal_print") is None else int(data["is_thermal_print"])
    if not data.get("default_invoice_size"):
        data["default_invoice_size"] = "A4"
    return CompanySettings(**data)


def _normalize_company_updates(raw_updates: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize explicitly supplied company_settings fields."""
    normalized: Dict[str, Any] = {}
    for key, val in raw_updates.items():
        if key not in _COMPANY_SETTING_FIELDS:
            continue

        if key == "company_name":
            cleaned = val.strip() if isinstance(val, str) else ""
            if not cleaned:
                raise ValueError("Shop / Enterprise name cannot be empty.")
            normalized[key] = cleaned
        elif key == "owner_name" or key in _NULLABLE_TEXT_FIELDS:
            if val is None:
                normalized[key] = None
            else:
                cleaned = str(val).strip()
                normalized[key] = cleaned if cleaned else None
        elif key == "email":
            normalized[key] = normalize_and_validate_email(val if val is None else str(val))
        elif key == "upi_id":
            normalized[key] = normalize_and_validate_upi_id(val if val is None else str(val))
        elif key in ("gstin", "ifsc_code"):
            if val is None:
                normalized[key] = None
            else:
                cleaned = str(val).strip().upper()
                normalized[key] = cleaned if cleaned else None
        elif key == "state":
            cleaned = str(val).strip() if val is not None else ""
            normalized[key] = cleaned if cleaned else "Maharashtra"
        elif key == "is_thermal_print":
            normalized[key] = 1 if int(val or 0) else 0
        elif key == "default_invoice_size":
            cleaned = str(val).strip().upper() if val is not None else ""
            size_val = cleaned if cleaned else "A4"
            if size_val not in _ALLOWED_INVOICE_SIZES:
                raise ValueError(
                    f"Invalid default_invoice_size '{size_val}'. Allowed: {', '.join(sorted(_ALLOWED_INVOICE_SIZES))}."
                )
            normalized[key] = size_val
    return normalized


class SystemRepository(BaseRepository):
    """Repository for system authentication, company profile, and audit logs."""

    # ---------------- Users & Auth ----------------
    def create_user(self, user: User) -> int:
        sql = "INSERT INTO users (username, password_hash, full_name, role, is_active) VALUES (?, ?, ?, ?, ?);"
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (user.username, user.password_hash, user.full_name, user.role, user.is_active))
            return cursor.lastrowid

    def get_user_by_username(self, username: str) -> Optional[User]:
        row = self.db.fetch_one("SELECT * FROM users WHERE username = ?;", (username,))
        return User(**dict(row)) if row else None

    def get_user_by_id(self, user_id: int) -> Optional[User]:
        row = self.db.fetch_one("SELECT * FROM users WHERE user_id = ?;", (user_id,))
        return User(**dict(row)) if row else None

    def list_users(self) -> List[User]:
        rows = self.db.fetch_all("SELECT user_id, username, full_name, role, is_active, created_at FROM users ORDER BY user_id ASC;")
        return [User(**dict(r)) for r in rows]

    def update_user_password(self, user_id: int, password_hash: str) -> None:
        sql = "UPDATE users SET password_hash = ? WHERE user_id = ?;"
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (password_hash, user_id))

    def update_user_status(self, user_id: int, is_active: int) -> None:
        sql = "UPDATE users SET is_active = ? WHERE user_id = ?;"
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (is_active, user_id))

    def delete_user(self, user_id: int) -> None:
        sql = "DELETE FROM users WHERE user_id = ?;"
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (user_id,))


    # ---------------- Company Profile ----------------
    def get_company_settings(self) -> CompanySettings:
        row = self.db.fetch_one("SELECT * FROM company_settings WHERE setting_id = 1;")
        if row:
            return _row_to_company_settings(dict(row))
        return CompanySettings(company_name="Krishi Agri Shop")

    def update_company_settings(
        self, settings: Union[CompanySettings, CompanySettingsUpdate, Dict[str, Any]]
    ) -> CompanySettings:
        """
        Persist shop profile settings non-destructively inside an atomic BEGIN IMMEDIATE transaction.
        Only explicitly supplied fields are updated; any omitted fields preserve their existing
        SQLite values so saving one setting never accidentally overwrites other settings.
        """
        if isinstance(settings, Mapping):
            raw_updates = {k: v for k, v in settings.items() if k in _COMPANY_SETTING_FIELDS}
        else:
            raw_fields_set = getattr(settings, "model_fields_set", None)
            if raw_fields_set is None:
                raw_fields_set = getattr(settings, "__fields_set__", set())
            fields_set = set(raw_fields_set)
            raw_updates = {
                k: getattr(settings, k)
                for k in fields_set
                if k in _COMPANY_SETTING_FIELDS
            }

        updates = _normalize_company_updates(raw_updates)

        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cursor = conn.cursor()
            try:
                existing_row = cursor.execute(
                    "SELECT * FROM company_settings WHERE setting_id = 1;"
                ).fetchone()

                if not updates and existing_row is not None:
                    return _row_to_company_settings(dict(existing_row))

                table_cols = {
                    str(col_row["name"])
                    for col_row in cursor.execute("PRAGMA table_info(company_settings);").fetchall()
                }
                writable_cols = [c for c in _COMPANY_SETTING_FIELDS if c in table_cols]

                merged: Dict[str, Any] = {
                    "company_name": "Krishi Agri Shop",
                    "owner_name": None,
                    "address": None,
                    "city": None,
                    "state": "Maharashtra",
                    "pincode": None,
                    "mobile": None,
                    "email": None,
                    "gstin": None,
                    "dl_fertilizer": None,
                    "dl_pesticide": None,
                    "dl_seed": None,
                    "upi_id": None,
                    "bank_name": None,
                    "account_no": None,
                    "ifsc_code": None,
                    "invoice_terms": None,
                    "is_thermal_print": 0,
                    "default_invoice_size": "A4",
                }
                if existing_row is not None:
                    merged.update(dict(existing_row))
                    if not merged.get("company_name"):
                        merged["company_name"] = "Krishi Agri Shop"
                    if not merged.get("state"):
                        merged["state"] = "Maharashtra"
                    if merged.get("is_thermal_print") is None:
                        merged["is_thermal_print"] = 0
                    if not merged.get("default_invoice_size"):
                        merged["default_invoice_size"] = "A4"

                merged.update(updates)

                col_names_sql = ", ".join(["setting_id", *writable_cols, "updated_at"])
                placeholders_sql = ", ".join(["1", *["?" for _ in writable_cols], "CURRENT_TIMESTAMP"])
                update_assign_sql = ", ".join(
                    [f"{col} = excluded.{col}" for col in writable_cols] + ["updated_at = CURRENT_TIMESTAMP"]
                )
                sql = (
                    f"INSERT INTO company_settings ({col_names_sql}) "
                    f"VALUES ({placeholders_sql}) "
                    f"ON CONFLICT(setting_id) DO UPDATE SET {update_assign_sql};"
                )
                cursor.execute(sql, tuple(merged.get(col) for col in writable_cols))

                saved_row = cursor.execute(
                    "SELECT * FROM company_settings WHERE setting_id = 1;"
                ).fetchone()
                if saved_row is not None:
                    return _row_to_company_settings(dict(saved_row))
                return _row_to_company_settings(merged)
            finally:
                cursor.close()

    # ---------------- Audit Trail ----------------
    def log_action(self, entry: AuditLogEntry) -> int:
        sql = "INSERT INTO audit_log (user_id, action_type, table_name, record_id, details) VALUES (?, ?, ?, ?, ?);"
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                sql,
                (entry.user_id, entry.action_type, entry.table_name, entry.record_id, entry.details),
            )
            return cursor.lastrowid

    # ---------------- Backup Log ----------------
    def record_backup(self, entry: BackupLogEntry) -> int:
        sql = "INSERT INTO backup_log (file_path, backup_type, backup_size_bytes, status) VALUES (?, ?, ?, ?);"
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                sql,
                (entry.file_path, entry.backup_type, entry.backup_size_bytes, entry.status),
            )
            return cursor.lastrowid
