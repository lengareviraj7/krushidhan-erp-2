"""
Recovery Key Setup, Confirmation, Rotation, and Import Lifecycle Manager (Phase 2C-4C).

Security guarantees:
- Pending newly generated keys exist ONLY in short-lived process memory bound to the
  authenticated ADMIN user_id, with automatic expiry (default 10 minutes).
- Pending keys are NEVER persisted to SQLite, files, logs, or browser storage.
- If the process restarts before confirmation, the pending operation is discarded.
- Confirmation requires re-entering the complete Recovery Key plus explicit acknowledgement.
- Rotation activates the newly confirmed key for future `.kdb.enc` archives while preserving
  ALL previous historical keys and metadata (never silently re-encrypts or deletes old keys).
- Never returns raw Recovery Keys after confirmation or import.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from src.services.os_recovery_key_store import get_recovery_key_store
from src.services.recovery_key import (
    InvalidRecoveryKeyError,
    PORTABLE_RECOVERY_WARNING,
    RecoveryKeyIntegrityError,
    RecoveryKeyStore,
    RecoveryKeyStoreError,
    RecoveryKeyStoreUnavailableError,
    generate_recovery_key,
    get_recovery_key_id,
    normalize_recovery_key,
    utc_now_iso,
)

DEFAULT_PENDING_TTL_SECONDS = 600  # 10 minutes


class RecoveryKeyLifecycleError(ValueError):
    """Sanitized lifecycle validation error (never contains Recovery Key material)."""


@dataclass
class _PendingRecoveryKeyOp:
    operation_id: str
    mode: str  # "setup" or "rotate"
    admin_user_id: int
    recovery_key: str
    key_id: str
    created_at_utc: str
    expires_at_mono: float


class RecoveryKeyLifecycleService:
    """Thread-safe coordinator for first-time setup, confirmation, rotation, and import."""

    def __init__(
        self,
        *,
        key_store_provider: Callable[[], RecoveryKeyStore] = get_recovery_key_store,
        pending_ttl_seconds: int = DEFAULT_PENDING_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store_provider = key_store_provider
        self._ttl_seconds = max(30, int(pending_ttl_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: Dict[str, _PendingRecoveryKeyOp] = {}

    def _get_store(self) -> RecoveryKeyStore:
        return self._store_provider()

    def _purge_expired_unlocked(self) -> None:
        now = self._clock()
        expired_ids = [op_id for op_id, op in self._pending.items() if op.expires_at_mono <= now]
        for op_id in expired_ids:
            self._pending.pop(op_id, None)

    def clear_all_pending(self) -> None:
        """Discard all unconfirmed pending keys from process memory."""
        with self._lock:
            self._pending.clear()

    def get_store_status(self) -> Dict[str, Any]:
        store = self._get_store()
        return store.get_status()

    def start_generation(self, admin_user_id: int, *, mode: str) -> Dict[str, Any]:
        """
        Generate a new 256-bit Recovery Key held only in short-lived process memory,
        bound to `admin_user_id`, and returned ONCE for Recovery Card display/printing.
        """
        norm_mode = str(mode or "").strip().lower()
        if norm_mode not in ("setup", "rotate"):
            raise RecoveryKeyLifecycleError("Generation mode must be 'setup' or 'rotate'.")
        if not isinstance(admin_user_id, int) or admin_user_id <= 0:
            raise RecoveryKeyLifecycleError("Authenticated administrator identity is required.")

        store = self._get_store()
        if not store.is_available():
            raise RecoveryKeyStoreUnavailableError(
                f"OS-protected key storage ({store.backend_name}) is unavailable. "
                "Cannot store a new Recovery Key safely on this computer."
            )

        has_active = store.has_key()
        if norm_mode == "setup" and has_active:
            raise RecoveryKeyLifecycleError(
                "An active Recovery Key is already configured. Use Rotate Recovery Key to create a new key."
            )
        if norm_mode == "rotate" and not has_active:
            raise RecoveryKeyLifecycleError(
                "No active Recovery Key is configured yet. Complete first-time Recovery Key setup first."
            )

        rk = generate_recovery_key()
        key_id = get_recovery_key_id(rk)
        created_at_utc = utc_now_iso()
        op_id = secrets.token_urlsafe(24)

        with self._lock:
            self._purge_expired_unlocked()
            # Discard any previous unconfirmed pending key for this admin
            stale_ids = [k for k, v in self._pending.items() if v.admin_user_id == admin_user_id]
            for sid in stale_ids:
                self._pending.pop(sid, None)
            self._pending[op_id] = _PendingRecoveryKeyOp(
                operation_id=op_id,
                mode=norm_mode,
                admin_user_id=admin_user_id,
                recovery_key=rk,
                key_id=key_id,
                created_at_utc=created_at_utc,
                expires_at_mono=self._clock() + self._ttl_seconds,
            )

        warnings = [
            "This Recovery Key is shown only once. If you lose it, encrypted backups created with this Key ID cannot be recovered by the developer or application.",
            PORTABLE_RECOVERY_WARNING,
        ]
        if norm_mode == "rotate":
            warnings.append(
                "Rotating changes the active key for FUTURE encrypted backups only. Existing .kdb.enc archives keep their original Key ID and still require their original Recovery Cards."
            )

        return {
            "operation_id": op_id,
            "mode": norm_mode,
            "recovery_key": rk,  # Returned ONCE for Recovery Card display/printing
            "key_id": key_id,
            "created_at_utc": created_at_utc,
            "expires_in_seconds": self._ttl_seconds,
            "storage_backend": store.backend_name,
            "previous_active_key_id": store.get_active_key_id() if norm_mode == "rotate" else None,
            "warnings": warnings,
        }

    def confirm_generation(
        self,
        admin_user_id: int,
        *,
        operation_id: str,
        confirmed_recovery_key: str,
        acknowledged_card_saved: bool,
    ) -> Dict[str, Any]:
        """
        Validate the re-entered Recovery Key against the short-lived pending operation,
        store and activate it in the OS secure key store while preserving all historical keys,
        verify stored retrieval, and purge the pending key from memory.
        """
        if acknowledged_card_saved is not True:
            raise RecoveryKeyLifecycleError(
                "You must confirm that you have printed or securely retained your Recovery Card before activating the key."
            )
        op_id = str(operation_id or "").strip()
        if not op_id:
            raise RecoveryKeyLifecycleError("Pending setup operation ID is required.")

        # Validate format & checksum first (never leaks input key)
        normalized_input = normalize_recovery_key(confirmed_recovery_key)

        with self._lock:
            self._purge_expired_unlocked()
            pending = self._pending.get(op_id)
            if pending is None:
                raise RecoveryKeyLifecycleError(
                    "Pending Recovery Key setup has expired or was not found. Please generate a new Recovery Key."
                )
            if pending.admin_user_id != admin_user_id:
                raise RecoveryKeyLifecycleError(
                    "Pending Recovery Key setup belongs to a different administrator session."
                )
            if not hmac.compare_digest(normalized_input, pending.recovery_key):
                raise RecoveryKeyLifecycleError(
                    "The re-entered Recovery Key does not match the generated Recovery Key. Check your Recovery Card and try again."
                )

            # Store in OS secure key store before removing pending state so OS errors can be surfaced cleanly
            store = self._get_store()
            if not store.is_available():
                raise RecoveryKeyStoreUnavailableError(
                    f"OS-protected key storage ({store.backend_name}) is unavailable. Key could not be saved."
                )
            source = "rotated" if pending.mode == "rotate" else "generated"
            stored_key_id = store.store_key(
                pending.recovery_key,
                activate=True,
                source=source,
                created_at_utc=pending.created_at_utc,
            )
            verification = store.verify_stored_key(stored_key_id)
            if not verification.get("valid"):
                raise RecoveryKeyIntegrityError("Stored Recovery Key failed post-save verification.")

            # Purge confirmed secret from short-lived process memory immediately
            self._pending.pop(op_id, None)
            all_keys = store.list_key_metadata()
            return {
                "success": True,
                "mode": pending.mode,
                "key_id": stored_key_id,
                "active_key_id": store.get_active_key_id(),
                "storage_backend": store.backend_name,
                "key_count": len(all_keys),
                "historical_keys_preserved": True,
                "message": (
                    f"New Recovery Key ({stored_key_id}) is now active for future encrypted backups. "
                    "All previous Recovery Keys remain preserved for older archives."
                    if pending.mode == "rotate"
                    else f"Recovery Key ({stored_key_id}) has been verified and saved in OS-protected storage."
                ),
            }

    def cancel_generation(self, admin_user_id: int, *, operation_id: Optional[str] = None) -> Dict[str, Any]:
        """Cancel and immediately discard pending unconfirmed Recovery Key(s) for `admin_user_id`."""
        removed = 0
        with self._lock:
            self._purge_expired_unlocked()
            if operation_id:
                op = self._pending.get(operation_id)
                if op is not None and op.admin_user_id == admin_user_id:
                    self._pending.pop(operation_id, None)
                    removed = 1
            else:
                to_drop = [k for k, v in self._pending.items() if v.admin_user_id == admin_user_id]
                for k in to_drop:
                    self._pending.pop(k, None)
                removed = len(to_drop)
        return {"success": True, "cancelled_count": removed}

    def import_recovery_key(
        self,
        admin_user_id: int,
        *,
        recovery_key: str,
        confirm_import: bool,
        set_as_active: bool = False,
    ) -> Dict[str, Any]:
        """
        Validate and import an existing Recovery Key (e.g. from a Recovery Card on a replacement computer)
        into the platform OS secure store while preserving all existing stored keys.
        """
        if confirm_import is not True:
            raise RecoveryKeyLifecycleError(
                "Explicit confirmation (confirm_import=true) is required to import a Recovery Key."
            )
        if not isinstance(admin_user_id, int) or admin_user_id <= 0:
            raise RecoveryKeyLifecycleError("Authenticated administrator identity is required.")

        canonical = normalize_recovery_key(recovery_key)
        key_id = get_recovery_key_id(canonical)

        store = self._get_store()
        if not store.is_available():
            raise RecoveryKeyStoreUnavailableError(
                f"OS-protected key storage ({store.backend_name}) is unavailable. "
                "You can still decrypt or restore an encrypted backup by entering your Recovery Key manually."
            )

        existing_secret = store.get_key(key_id)
        if existing_secret is not None:
            if not hmac.compare_digest(normalize_recovery_key(existing_secret), canonical):
                raise RecoveryKeyIntegrityError(
                    "A conflicting key record already exists for this Key ID."
                )
            if set_as_active:
                store.set_active_key(key_id)
            active_id = store.get_active_key_id()
            return {
                "success": True,
                "key_id": key_id,
                "already_present": True,
                "is_active": active_id == key_id,
                "active_key_id": active_id,
                "storage_backend": store.backend_name,
                "key_count": len(store.list_key_metadata()),
                "message": f"Recovery Key ({key_id}) is already stored in OS-protected key storage.",
            }

        should_activate = bool(set_as_active) or (not store.has_key())
        stored_id = store.store_key(canonical, activate=should_activate, source="imported")
        verification = store.verify_stored_key(stored_id)
        if not verification.get("valid"):
            raise RecoveryKeyIntegrityError("Imported Recovery Key failed post-save verification.")
        active_id = store.get_active_key_id()
        return {
            "success": True,
            "key_id": stored_id,
            "already_present": False,
            "is_active": active_id == stored_id,
            "active_key_id": active_id,
            "storage_backend": store.backend_name,
            "key_count": len(store.list_key_metadata()),
            "message": (
                f"Recovery Key ({stored_id}) imported and activated in OS-protected storage."
                if active_id == stored_id
                else f"Historical Recovery Key ({stored_id}) imported into OS-protected storage. Existing active key ({active_id}) remains unchanged."
            ),
        }

    def verify_stored_key(self, *, key_id: Optional[str] = None) -> Dict[str, Any]:
        store = self._get_store()
        if not store.is_available():
            raise RecoveryKeyStoreUnavailableError(
                f"OS-protected key storage ({store.backend_name}) is unavailable."
            )
        return store.verify_stored_key(key_id=key_id)


_LIFECYCLE_LOCK = threading.Lock()
_LIFECYCLE_INSTANCE: Optional[RecoveryKeyLifecycleService] = None


def get_recovery_key_lifecycle() -> RecoveryKeyLifecycleService:
    global _LIFECYCLE_INSTANCE
    with _LIFECYCLE_LOCK:
        if _LIFECYCLE_INSTANCE is None:
            _LIFECYCLE_INSTANCE = RecoveryKeyLifecycleService()
        return _LIFECYCLE_INSTANCE


def reset_recovery_key_lifecycle(
    *,
    pending_ttl_seconds: Optional[int] = None,
    clock: Optional[Callable[[], float]] = None,
) -> RecoveryKeyLifecycleService:
    """Reset the process lifecycle coordinator (used by isolated tests)."""
    global _LIFECYCLE_INSTANCE
    with _LIFECYCLE_LOCK:
        if _LIFECYCLE_INSTANCE is not None:
            _LIFECYCLE_INSTANCE.clear_all_pending()
        _LIFECYCLE_INSTANCE = RecoveryKeyLifecycleService(
            pending_ttl_seconds=pending_ttl_seconds if pending_ttl_seconds is not None else DEFAULT_PENDING_TTL_SECONDS,
            clock=clock if clock is not None else time.monotonic,
        )
        return _LIFECYCLE_INSTANCE
