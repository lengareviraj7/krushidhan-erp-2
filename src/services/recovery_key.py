"""
Recovery Key Generation, Validation, Non-Secret Key ID Derivation,
and Injectable RecoveryKeyStore Abstraction (Phase 2C-4B).

Security properties:
- Generates 32 cryptographically random bytes (256-bit entropy) via `secrets.token_bytes`.
- Encodes 34 bytes (32-byte key + 2-byte domain-separated SHA-256 typo-detection checksum)
  into human-readable uppercase RFC 4648 Base32 grouped as `KDHN-XXXXX-...` (11 groups of 5).
- Checksum is strictly for typing-error detection, never a cryptographic security primitive.
- Derives a stable non-secret 16-char hex `key_id` via domain-separated SHA-256.
- Never logs, transmits, or persists Recovery Keys to disk or SQLite.
- Zero-knowledge design: lost Recovery Keys cannot be recovered by developers.
"""
from __future__ import annotations

import abc
import base64
import binascii
import hashlib
import hmac
import re
import secrets
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

RECOVERY_KEY_BYTES = 32
RECOVERY_KEY_CHECKSUM_BYTES = 2
RECOVERY_KEY_PAYLOAD_BYTES = RECOVERY_KEY_BYTES + RECOVERY_KEY_CHECKSUM_BYTES  # 34 bytes = 272 bits
RECOVERY_KEY_B32_LENGTH = 55  # ceil(272 / 5) = 55 Base32 characters
RECOVERY_KEY_GROUP_SIZE = 5
RECOVERY_KEY_PREFIX = "KDHN"

_CHECKSUM_DOMAIN = b"krushidhan-recovery-key-checksum-v1:"
_KEY_ID_DOMAIN = b"krushidhan-recovery-key-id-v1:"
_BASE32_BODY_RE = re.compile(r"^[A-Z2-7]{55}$")
KEY_ID_RE = re.compile(r"^[0-9A-F]{16}$")

PORTABLE_RECOVERY_WARNING = (
    "OS-protected keys are tied to this computer and operating-system user profile. "
    "If this computer fails, is stolen, or the OS user profile is reset, the locally stored copy "
    "becomes inaccessible. Always keep your printed Recovery Card(s) in a safe place separate from the shop computer."
)


class InvalidRecoveryKeyError(ValueError):
    """Raised when a Recovery Key has invalid format, invalid Base32 encoding, or checksum mismatch."""


class RecoveryKeyStoreError(RuntimeError):
    """Base error for OS-protected Recovery Key store failures (always sanitized)."""


class RecoveryKeyStoreUnavailableError(RecoveryKeyStoreError):
    """Raised when the OS secure key store is unavailable, locked, denied, or unsupported."""


class RecoveryKeyIntegrityError(RecoveryKeyStoreError):
    """Raised when a stored protected key record is damaged, malformed, or substituted."""


def utc_now_iso() -> str:
    """Return canonical UTC ISO-8601 timestamp (YYYY-MM-DDTHH:MM:SSZ)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_key_id(key_id: str) -> str:
    """Validate and normalize a 16-character uppercase hex Recovery Key ID."""
    if not isinstance(key_id, str):
        raise ValueError("Recovery key ID must be a string.")
    cleaned = key_id.strip().upper()
    if not KEY_ID_RE.match(cleaned):
        raise ValueError("Invalid Recovery Key ID format.")
    return cleaned


def _compute_checksum(raw_key: bytes) -> bytes:
    if len(raw_key) != RECOVERY_KEY_BYTES:
        raise InvalidRecoveryKeyError("Recovery key raw entropy must be exactly 32 bytes.")
    return hashlib.sha256(_CHECKSUM_DOMAIN + raw_key).digest()[:RECOVERY_KEY_CHECKSUM_BYTES]


def encode_recovery_key(raw_key: bytes) -> str:
    """
    Encode 32 raw entropy bytes into grouped human-readable Base32 with `KDHN-` prefix
    and a 2-byte domain-separated SHA-256 typo-detection checksum.
    """
    if not isinstance(raw_key, (bytes, bytearray)) or len(raw_key) != RECOVERY_KEY_BYTES:
        raise InvalidRecoveryKeyError("Recovery key raw entropy must be exactly 32 bytes.")
    raw = bytes(raw_key)
    payload = raw + _compute_checksum(raw)
    b32_body = base64.b32encode(payload).decode("ascii").rstrip("=")
    if len(b32_body) != RECOVERY_KEY_B32_LENGTH:
        raise InvalidRecoveryKeyError("Unexpected Base32 recovery key length.")
    groups = [
        b32_body[i : i + RECOVERY_KEY_GROUP_SIZE]
        for i in range(0, RECOVERY_KEY_B32_LENGTH, RECOVERY_KEY_GROUP_SIZE)
    ]
    return f"{RECOVERY_KEY_PREFIX}-" + "-".join(groups)


def generate_recovery_key() -> str:
    """Generate a new 256-bit cryptographically random Recovery Key in grouped `KDHN-...` format."""
    raw = secrets.token_bytes(RECOVERY_KEY_BYTES)
    return encode_recovery_key(raw)


def normalize_recovery_key(key_str: str) -> str:
    """Validate and return the canonical grouped uppercase `KDHN-...` form of a Recovery Key."""
    raw = validate_recovery_key(key_str)
    return encode_recovery_key(raw)


def validate_recovery_key(key_str: str) -> bytes:
    """
    Normalize and validate a user-entered Recovery Key string, returning its exact 32 raw bytes.

    Accepts lowercase letters and grouping separators (hyphens, spaces, underscores).
    Requires the `KDHN` prefix, canonical Base32 encoding (zero unused trailing bits),
    and exact 2-byte domain-separated SHA-256 checksum match.
    Never includes the input key string in raised error messages.
    """
    if not isinstance(key_str, str):
        raise InvalidRecoveryKeyError("Recovery key must be provided as text.")

    cleaned = re.sub(r"[\s\-_]+", "", key_str.strip()).upper()
    if not cleaned:
        raise InvalidRecoveryKeyError("Recovery key is required.")

    if not cleaned.startswith(RECOVERY_KEY_PREFIX):
        raise InvalidRecoveryKeyError("Recovery key must start with the 'KDHN-' prefix.")

    body = cleaned[len(RECOVERY_KEY_PREFIX) :]
    if not _BASE32_BODY_RE.match(body):
        raise InvalidRecoveryKeyError(
            "Recovery key format is invalid. Check all character groups and try again."
        )

    padded = body + "="  # 55 + 1 = 56 (multiple of 8)
    try:
        decoded = base64.b32decode(padded, casefold=False)
    except (binascii.Error, ValueError):
        raise InvalidRecoveryKeyError("Recovery key contains invalid Base32 characters.") from None

    if len(decoded) != RECOVERY_KEY_PAYLOAD_BYTES:
        raise InvalidRecoveryKeyError("Recovery key length is invalid.")

    # Reject non-canonical Base32 encodings where unused trailing bits are non-zero
    reencoded_body = base64.b32encode(decoded).decode("ascii").rstrip("=")
    if not hmac.compare_digest(reencoded_body, body):
        raise InvalidRecoveryKeyError("Recovery key encoding is non-canonical or mistyped.")

    raw_key = decoded[:RECOVERY_KEY_BYTES]
    provided_checksum = decoded[RECOVERY_KEY_BYTES:]
    expected_checksum = _compute_checksum(raw_key)
    if not hmac.compare_digest(provided_checksum, expected_checksum):
        raise InvalidRecoveryKeyError(
            "Recovery key checksum failed. One or more characters were mistyped."
        )

    return raw_key


def get_recovery_key_id(recovery_key: str) -> str:
    """
    Derive a stable, non-secret 16-character uppercase hex key identifier from a Recovery Key
    using domain-separated SHA-256. Safe to store in `.kdb.enc` headers and display on Recovery Cards.
    """
    raw_key = validate_recovery_key(recovery_key)
    return hashlib.sha256(_KEY_ID_DOMAIN + raw_key).hexdigest()[:16].upper()


class RecoveryKeyStore(abc.ABC):
    """
    Injectable interface for OS-native Recovery Key storage (Windows DPAPI and macOS Keychain).
    Stores multiple historical keys keyed by non-secret `key_id` so rotation never orphans older archives.
    Never implemented with plaintext disk files, and never falls back to InMemoryRecoveryKeyStore in production.
    """

    @property
    @abc.abstractmethod
    def backend_name(self) -> str:
        """Stable non-secret identifier for the storage backend."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return True only when the underlying OS secure key store is operational."""

    @abc.abstractmethod
    def store_key(
        self,
        recovery_key: str,
        *,
        activate: bool = True,
        source: str = "generated",
        created_at_utc: Optional[str] = None,
    ) -> str:
        """
        Validate and store a Recovery Key associated with its derived `key_id`.
        Preserves all previously stored historical keys. Returns non-secret `key_id`.
        """

    @abc.abstractmethod
    def get_key(self, key_id: Optional[str] = None) -> Optional[str]:
        """
        Retrieve and validate the active Recovery Key (when key_id is None) or a specific
        historical Recovery Key by `key_id`. Returns None when not stored.
        Raises RecoveryKeyIntegrityError if stored key material is corrupted/substituted.
        """

    @abc.abstractmethod
    def get_active_key_id(self) -> Optional[str]:
        """Return the non-secret key_id of the active stored Recovery Key, or None."""

    @abc.abstractmethod
    def set_active_key(self, key_id: str) -> Dict[str, Any]:
        """Verify and mark an existing stored key_id as active for future encrypted backups."""

    @abc.abstractmethod
    def list_key_metadata(self) -> List[Dict[str, Any]]:
        """
        Return non-secret metadata for all stored keys (active and historical).
        NEVER includes raw Recovery Key strings or bytes.
        """

    @abc.abstractmethod
    def verify_stored_key(self, key_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Retrieve and cryptographically validate a stored Recovery Key (format, checksum, key_id match)
        without returning raw key material.
        """

    @abc.abstractmethod
    def delete_key(self, key_id: Optional[str] = None) -> bool:
        """
        Delete a stored key (intended strictly for isolated test cleanup).
        Production key-management workflows must NEVER delete historical keys automatically.
        """

    @abc.abstractmethod
    def has_key(self) -> bool:
        """Return True when an active Recovery Key is available in the store."""

    def get_status(self) -> Dict[str, Any]:
        """Return a non-secret status summary safe for Admin UI and API responses."""
        avail = self.is_available()
        keys: List[Dict[str, Any]] = []
        active_id: Optional[str] = None
        if avail:
            try:
                keys = self.list_key_metadata()
                active_id = self.get_active_key_id()
            except RecoveryKeyStoreError:
                avail = False
        return {
            "available": bool(avail),
            "backend": self.backend_name,
            "platform": sys.platform,
            "has_active_key": bool(avail and active_id),
            "active_key_id": active_id if avail else None,
            "key_count": len(keys) if avail else 0,
            "keys": keys if avail else [],
            "message": (
                f"OS-protected key storage ({self.backend_name}) is operational."
                if avail
                else f"OS-protected key storage ({self.backend_name}) is unavailable. "
                "Automatic stored-key operations are disabled; enter your Recovery Key manually from your Recovery Card."
            ),
            "portable_recovery_warning": PORTABLE_RECOVERY_WARNING,
        }


class InMemoryRecoveryKeyStore(RecoveryKeyStore):
    """
    Process-memory-only RecoveryKeyStore strictly for isolated automated testing.
    NEVER selected as a production fallback. Never writes key material to disk, SQLite,
    environment variables, or logs.
    """

    def __init__(self, *, available: bool = True) -> None:
        self._lock = threading.RLock()
        self._available = bool(available)
        self._keys: Dict[str, str] = {}
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._active_key_id: Optional[str] = None

    @property
    def backend_name(self) -> str:
        return "in_memory_test_only"

    def is_available(self) -> bool:
        return self._available

    def _require_available(self) -> None:
        if not self._available:
            raise RecoveryKeyStoreUnavailableError("In-memory test key store is marked unavailable.")

    def store_key(
        self,
        recovery_key: str,
        *,
        activate: bool = True,
        source: str = "generated",
        created_at_utc: Optional[str] = None,
    ) -> str:
        self._require_available()
        canonical = normalize_recovery_key(recovery_key)
        key_id = get_recovery_key_id(canonical)
        ts = created_at_utc or utc_now_iso()
        with self._lock:
            self._keys[key_id] = canonical
            if key_id not in self._meta:
                self._meta[key_id] = {
                    "key_id": key_id,
                    "created_at_utc": ts,
                    "storage_backend": self.backend_name,
                    "last_used_at_utc": None,
                    "source": source,
                }
            if activate or self._active_key_id is None:
                self._active_key_id = key_id
        return key_id

    def get_key(self, key_id: Optional[str] = None) -> Optional[str]:
        self._require_available()
        with self._lock:
            target_id = validate_key_id(key_id) if key_id is not None else self._active_key_id
            if not target_id:
                return None
            val = self._keys.get(target_id)
            if val is None:
                return None
            try:
                derived = get_recovery_key_id(val)
            except InvalidRecoveryKeyError:
                raise RecoveryKeyIntegrityError("Stored Recovery Key failed validation.") from None
            if not hmac.compare_digest(derived, target_id):
                raise RecoveryKeyIntegrityError("Stored Recovery Key does not match expected Key ID.")
            if target_id in self._meta:
                self._meta[target_id]["last_used_at_utc"] = utc_now_iso()
            return val

    def get_active_key_id(self) -> Optional[str]:
        if not self._available:
            return None
        with self._lock:
            if self._active_key_id and self._active_key_id in self._keys:
                return self._active_key_id
            return None

    def set_active_key(self, key_id: str) -> Dict[str, Any]:
        self._require_available()
        clean_id = validate_key_id(key_id)
        with self._lock:
            if clean_id not in self._keys:
                raise KeyError(f"Recovery Key ID '{clean_id}' is not present in key store.")
            # Verify key before activation
            self.verify_stored_key(clean_id)
            self._active_key_id = clean_id
            meta = dict(self._meta[clean_id])
            meta["is_active"] = True
            return meta

    def list_key_metadata(self) -> List[Dict[str, Any]]:
        self._require_available()
        with self._lock:
            rows: List[Dict[str, Any]] = []
            for kid, info in self._meta.items():
                if kid not in self._keys:
                    continue
                item = dict(info)
                item["is_active"] = kid == self._active_key_id
                rows.append(item)
            rows.sort(key=lambda r: (not r["is_active"], r.get("created_at_utc") or ""), reverse=False)
            # Active first, then newest created_at_utc first
            active_rows = [r for r in rows if r["is_active"]]
            hist_rows = sorted(
                [r for r in rows if not r["is_active"]],
                key=lambda r: r.get("created_at_utc") or "",
                reverse=True,
            )
            return active_rows + hist_rows

    def verify_stored_key(self, key_id: Optional[str] = None) -> Dict[str, Any]:
        self._require_available()
        target_id = validate_key_id(key_id) if key_id is not None else self.get_active_key_id()
        if not target_id:
            raise RecoveryKeyStoreError("No active Recovery Key is stored to verify.")
        secret = self.get_key(target_id)
        if secret is None:
            raise RecoveryKeyStoreError("Requested Recovery Key ID was not found in the key store.")
        derived = get_recovery_key_id(secret)
        if not hmac.compare_digest(derived, target_id):
            raise RecoveryKeyIntegrityError("Stored Recovery Key failed key ID integrity verification.")
        return {
            "valid": True,
            "key_id": target_id,
            "is_active": target_id == self.get_active_key_id(),
            "storage_backend": self.backend_name,
        }

    def delete_key(self, key_id: Optional[str] = None) -> bool:
        self._require_available()
        with self._lock:
            target_id = validate_key_id(key_id) if key_id is not None else self._active_key_id
            if not target_id or target_id not in self._keys:
                return False
            self._keys.pop(target_id, None)
            self._meta.pop(target_id, None)
            if self._active_key_id == target_id:
                self._active_key_id = next(iter(self._keys), None)
            return True

    def has_key(self) -> bool:
        if not self._available:
            return False
        with self._lock:
            return bool(self._active_key_id and self._active_key_id in self._keys)
