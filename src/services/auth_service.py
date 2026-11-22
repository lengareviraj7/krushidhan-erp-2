"""
Service Layer for User Authentication, Role-Based Access, First-Run Setup, and Cryptographic Session Tokens.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from src.config import get_jwt_secret
from src.db.connection import DatabaseManager, get_db_manager
from src.models.system import AuditLogEntry, CompanySettings, User
from src.repositories.system_repository import SystemRepository

logger = logging.getLogger("agri_erp.auth")
_INITIAL_SETUP_LOCK = threading.Lock()


class InitialSetupAlreadyCompletedError(ValueError):
    """Raised when attempting first-run admin setup after a user already exists."""


try:
    import bcrypt
    HAS_BCRYPT = True
except ImportError:
    HAS_BCRYPT = False

JWT_ALGORITHM = "HS256"
DEFAULT_TOKEN_EXPIRY_SECONDS = 7 * 24 * 3600  # 7 days
_USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (4 - (len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


class AuthService:
    """User authentication, role-based checks, cryptographic tokens, and secure password management."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or get_db_manager()
        self.system_repo = SystemRepository(self.db)
        if not self._is_clean_shop_mode():
            self.ensure_bootstrap_users()

    def _is_clean_shop_mode(self) -> bool:
        """
        Return True when predictable default passwords / auto-bootstrap users are strictly forbidden
        (desktop application mode, clean new-shop installation, or APP_ENV=production).
        """
        app_env = os.environ.get("APP_ENV", "").strip().lower()
        return bool(
            getattr(self.db, "app_mode", "local") == "desktop"
            or getattr(self.db, "clean_install_mode", False)
            or app_env == "production"
        )

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash password using bcrypt if available or salted SHA-256 fallback."""
        if HAS_BCRYPT:
            salt = bcrypt.gensalt()
            return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")
        else:
            salt = "krushidhan_agri_erp_salt_2026"
            return hashlib.sha256(f"{salt}{password}".encode("utf-8")).hexdigest()

    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """Verify plain password against hashed password."""
        if not plain_password or not hashed_password:
            return False

        if HAS_BCRYPT and hashed_password.startswith("$2"):
            try:
                if bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8")):
                    return True
            except Exception:
                pass

        # Legacy SHA-256 check for compatibility with previously created records
        salt = "krushidhan_agri_erp_salt_2026"
        expected = hashlib.sha256(f"{salt}{plain_password}".encode("utf-8")).hexdigest()
        if hmac.compare_digest(expected, hashed_password):
            return True

        old_salt = "offline_agri_erp_salt"
        expected_old = hashlib.sha256(f"{old_salt}{plain_password}".encode("utf-8")).hexdigest()
        if hmac.compare_digest(expected_old, hashed_password):
            return True

        return False

    @staticmethod
    def create_access_token(
        user_id: int,
        username: str,
        full_name: str,
        role: str,
        expires_in: int = DEFAULT_TOKEN_EXPIRY_SECONDS,
    ) -> str:
        """Generate an HMAC-SHA256 cryptographically signed JWT session token."""
        secret = get_jwt_secret()
        header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
        now = int(time.time())
        payload = {
            "sub": str(user_id),
            "user_id": user_id,
            "username": username,
            "full_name": full_name,
            "role": role,
            "iat": now,
            "exp": now + expires_in,
        }

        header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signing_input = f"{header_b64}.{payload_b64}"

        signature = hmac.new(
            secret.encode("utf-8"),
            signing_input.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sig_b64 = _b64url_encode(signature)

        return f"{signing_input}.{sig_b64}"

    @staticmethod
    def verify_access_token(token: str) -> Optional[Dict[str, Any]]:
        """Verify cryptographic HMAC-SHA256 signature, header algorithm, claims, and expiration of JWT token."""
        if not token or "." not in token:
            return None

        parts = token.strip().split(".")
        if len(parts) != 3:
            return None

        secret = get_jwt_secret()
        header_b64, payload_b64, sig_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}"

        expected_sig = hmac.new(
            secret.encode("utf-8"),
            signing_input.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        try:
            header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
            if not isinstance(header, dict) or header.get("alg") != JWT_ALGORITHM:
                return None
            typ = header.get("typ")
            if typ is not None and typ != "JWT":
                return None

            actual_sig = _b64url_decode(sig_b64)
            if not hmac.compare_digest(expected_sig, actual_sig):
                return None

            payload_bytes = _b64url_decode(payload_b64)
            payload = json.loads(payload_bytes.decode("utf-8"))
            if not isinstance(payload, dict):
                return None

            exp = payload.get("exp")
            if not isinstance(exp, (int, float)) or isinstance(exp, bool) or time.time() > exp:
                return None  # Missing, malformed, or expired exp claim

            if "user_id" not in payload or not payload.get("username") or not payload.get("role"):
                return None

            return payload
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error):
            return None

    def ensure_bootstrap_users(self) -> None:
        """
        Ensure neutral local/Vercel test users exist in non-desktop development mode.
        Strictly disabled in desktop mode and clean new-shop installations so no
        predictable default passwords or developer personal info are ever shipped to customer machines.
        """
        if self._is_clean_shop_mode():
            return

        users = self.system_repo.list_users()
        if users:
            return

        admin_hash = self.hash_password("krushidhan@2026")
        operator_hash = self.hash_password("billing123")
        with _INITIAL_SETUP_LOCK:
            with self.db.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE;")
                row = conn.execute("SELECT COUNT(*) AS cnt FROM users;").fetchone()
                if row and int(row["cnt"]) > 0:
                    return
                conn.execute(
                    "INSERT INTO users (username, password_hash, full_name, role, is_active) VALUES (?, ?, ?, ?, ?);",
                    ("admin", admin_hash, "Shop Admin", "ADMIN", 1),
                )
                conn.execute(
                    "INSERT INTO users (username, password_hash, full_name, role, is_active) VALUES (?, ?, ?, ?, ?);",
                    ("billing", operator_hash, "Counter Staff", "OPERATOR", 1),
                )

    def needs_initial_setup(self) -> bool:
        """Return True when the database has zero user accounts and requires owner setup."""
        row = self.db.fetch_one("SELECT COUNT(*) AS cnt FROM users;")
        return (int(row["cnt"]) if row else 0) == 0

    def has_company_profile(self) -> bool:
        """Return True when company_settings row 1 exists."""
        row = self.db.fetch_one("SELECT setting_id FROM company_settings WHERE setting_id = 1;")
        return row is not None

    def setup_initial_admin(
        self,
        username: str,
        plain_password: str,
        full_name: str,
        company_settings: Optional[CompanySettings] = None,
    ) -> User:
        """
        First-run onboarding for a clean shop installation:
        Creates the initial shop owner administrator account (role='ADMIN') and optionally
        initializes the shop profile in `company_settings` inside a single atomic BEGIN IMMEDIATE transaction.
        Permitted ONLY when zero users exist in the database.
        """
        normalized_username = (username or "").strip().lower()
        if not _USERNAME_RE.match(normalized_username):
            raise ValueError(
                "Username must be 3-32 characters using only lowercase letters, numbers, '.', '_', or '-'."
            )

        cleaned_name = (full_name or "").strip()
        if len(cleaned_name) < 2:
            raise ValueError("Administrator full name must be at least 2 characters.")

        if not plain_password or len(plain_password) < 6:
            raise ValueError("Password must be at least 6 characters long.")

        # Perform expensive bcrypt work BEFORE acquiring the SQLite write lock
        hashed = self.hash_password(plain_password)

        with _INITIAL_SETUP_LOCK:
            conn = self.db.get_connection()
            try:
                if conn.in_transaction:
                    conn.rollback()
                conn.execute("BEGIN IMMEDIATE;")
                cur = conn.cursor()

                row = cur.execute("SELECT COUNT(*) AS cnt FROM users;").fetchone()
                if (int(row["cnt"]) if row else 0) > 0:
                    conn.rollback()
                    raise InitialSetupAlreadyCompletedError(
                        "Initial administrator setup has already been completed. Please log in."
                    )

                cur.execute(
                    "INSERT INTO users (username, password_hash, full_name, role, is_active) VALUES (?, ?, ?, ?, ?);",
                    (normalized_username, hashed, cleaned_name, "ADMIN", 1),
                )
                user_id = int(cur.lastrowid)

                if company_settings is not None and company_settings.company_name.strip():
                    table_cols = {
                        str(col_row["name"])
                        for col_row in cur.execute("PRAGMA table_info(company_settings);").fetchall()
                    }
                    owner_name_val = (
                        company_settings.owner_name.strip()
                        if company_settings.owner_name and company_settings.owner_name.strip()
                        else cleaned_name
                    )
                    field_values = {
                        "company_name": company_settings.company_name.strip(),
                        "owner_name": owner_name_val,
                        "address": company_settings.address,
                        "city": company_settings.city,
                        "state": company_settings.state or "Maharashtra",
                        "pincode": company_settings.pincode,
                        "mobile": company_settings.mobile,
                        "email": company_settings.email,
                        "gstin": company_settings.gstin,
                        "dl_fertilizer": company_settings.dl_fertilizer,
                        "dl_pesticide": company_settings.dl_pesticide,
                        "dl_seed": company_settings.dl_seed,
                        "upi_id": company_settings.upi_id,
                        "bank_name": company_settings.bank_name,
                        "account_no": company_settings.account_no,
                        "ifsc_code": company_settings.ifsc_code,
                        "invoice_terms": company_settings.invoice_terms,
                        "is_thermal_print": company_settings.is_thermal_print,
                        "default_invoice_size": company_settings.default_invoice_size or "A4",
                    }
                    writable_cols = [c for c in field_values.keys() if c in table_cols]
                    cols_sql = ", ".join(["setting_id", *writable_cols, "updated_at"])
                    vals_sql = ", ".join(["1", *["?" for _ in writable_cols], "CURRENT_TIMESTAMP"])
                    upd_sql = ", ".join(
                        [f"{c} = excluded.{c}" for c in writable_cols] + ["updated_at = CURRENT_TIMESTAMP"]
                    )
                    cur.execute(
                        f"INSERT INTO company_settings ({cols_sql}) VALUES ({vals_sql}) "
                        f"ON CONFLICT(setting_id) DO UPDATE SET {upd_sql};",
                        tuple(field_values[c] for c in writable_cols),
                    )

                cur.execute(
                    "INSERT INTO audit_log (user_id, action_type, table_name, record_id, details) VALUES (?, ?, ?, ?, ?);",
                    (
                        user_id,
                        "SETUP_ADMIN",
                        "users",
                        user_id,
                        f"Initial shop owner administrator '{normalized_username}' created.",
                    ),
                )

                user_row = cur.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,)).fetchone()
                cur.close()
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        self.db._save_cloud_snapshot()
        if user_row is None:
            raise RuntimeError("Failed to retrieve newly created administrator account.")
        return User(**dict(user_row))

    def authenticate_user(self, username: str, plain_password: str) -> Optional[User]:
        """Authenticate user by username and password."""
        user = self.system_repo.get_user_by_username(username.strip().lower())
        if not user or user.is_active != 1:
            return None

        if self.verify_password(plain_password, user.password_hash or ""):
            return user
        return None

    def register_user(self, username: str, plain_password: str, full_name: str, role: str = "OPERATOR") -> int:
        """Create new system user with hashed password."""
        existing = self.system_repo.get_user_by_username(username.strip().lower())
        if existing:
            raise ValueError(f"User with username '{username}' already exists.")

        hashed = self.hash_password(plain_password)
        user = User(
            username=username.strip().lower(),
            password_hash=hashed,
            full_name=full_name.strip(),
            role=role.upper(),
            is_active=1,
        )
        return self.system_repo.create_user(user)

    def change_password(self, user_id: int, old_password: str, new_password: str) -> bool:
        """Change password with verification of old password."""
        user = self.system_repo.get_user_by_id(user_id)
        if not user:
            return False

        if not self.verify_password(old_password, user.password_hash or ""):
            return False

        new_hash = self.hash_password(new_password)
        self.system_repo.update_user_password(user_id, new_hash)
        return True

    def reset_password_by_admin(self, target_user_id: int, new_password: str) -> bool:
        """Admin override reset password."""
        user = self.system_repo.get_user_by_id(target_user_id)
        if not user:
            return False

        new_hash = self.hash_password(new_password)
        self.system_repo.update_user_password(target_user_id, new_hash)
        return True

    def list_users(self) -> List[User]:
        """List all active and inactive users."""
        return self.system_repo.list_users()

    def set_user_active_status(self, user_id: int, is_active: bool) -> None:
        """Activate or deactivate user account."""
        self.system_repo.update_user_status(user_id, 1 if is_active else 0)
