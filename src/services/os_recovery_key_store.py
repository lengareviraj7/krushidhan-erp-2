"""
OS-Protected Recovery Key Storage Backends for Krushidhan ERP (Phase 2C-4C).

Provides platform-native encrypted key storage with zero plaintext fallback:
- Windows: Native Win32 DPAPI via `ctypes` (`CryptProtectData` / `CryptUnprotectData`,
  `CRYPTPROTECT_UI_FORBIDDEN`, current-user scope ONLY — never `CRYPTPROTECT_LOCAL_MACHINE`),
  storing only DPAPI-protected blobs in `%LOCALAPPDATA%\\com.krushidhan.erp\\recovery_keys`.
- macOS: Explicit `keyring.backends.macOS.Keyring` (`Security.framework` C API via ctypes —
  never auto-discovered, never accepts null/plaintext/file fallback, never passes secrets on CLI).
- Other platforms: Explicit `UnsupportedRecoveryKeyStore` that fails closed cleanly.

Non-secret metadata (key_id, created_at_utc, active flag, backend, last_used_at_utc) is indexed
separately so multiple historical keys are preserved across rotations without enumerating
unrelated user secrets. Raw Recovery Keys are NEVER written to metadata, logs, SQLite, or disk.
"""
from __future__ import annotations

import ctypes
import hmac
import json
import logging
import os
import stat
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.config import APP_ID, get_desktop_app_dir
from src.services.recovery_key import (
    InvalidRecoveryKeyError,
    PORTABLE_RECOVERY_WARNING,
    RecoveryKeyIntegrityError,
    RecoveryKeyStore,
    RecoveryKeyStoreError,
    RecoveryKeyStoreUnavailableError,
    get_recovery_key_id,
    normalize_recovery_key,
    utc_now_iso,
    validate_key_id,
    validate_recovery_key,
)

logger = logging.getLogger("agri_erp.recovery_key_store")

# Windows DPAPI constants
CRYPTPROTECT_UI_FORBIDDEN = 0x01
CRYPTPROTECT_LOCAL_MACHINE = 0x04  # Explicitly FORBIDDEN for Krushidhan ERP Recovery Keys

DPAPI_BLOB_MAGIC = b"KDHNDPAPI1"
DPAPI_ENTROPY_PREFIX = b"krushidhan-erp:dpapi:recovery-key:v1:"
DPAPI_DESCRIPTION = "Krushidhan ERP Recovery Key"

MACOS_KEYCHAIN_SERVICE = f"{APP_ID}.recovery-key"
KEY_ENVELOPE_VERSION = 1
METADATA_SCHEMA_VERSION = 1
METADATA_FILENAME = "metadata.json"

_DISALLOWED_BACKEND_KEYWORDS = (
    "null",
    "fail",
    "plain",
    "plaintext",
    "file",
    "chainer",
    "memory",
    "inmemory",
)


def _restrict_fs_permissions(path: Path, *, is_dir: bool) -> None:
    """Apply restrictive owner-only POSIX permissions (best-effort on Windows)."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, stat.S_IRWXU if is_dir else (stat.S_IRUSR | stat.S_IWUSR))
    except OSError:
        pass


def _fsync_file(path: Path) -> None:
    try:
        with open(path, "rb") as fh:
            os.fsync(fh.fileno())
    except OSError:
        pass


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _atomic_write_bytes(target_path: Path, data: bytes) -> None:
    """Atomically write bytes with restrictive permissions, fsync, and replace."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _restrict_fs_permissions(target_path.parent, is_dir=True)
    fd, tmp_raw = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=f".{os.getpid()}.tmp",
        dir=str(target_path.parent),
    )
    tmp_path = Path(tmp_raw)
    try:
        _restrict_fs_permissions(tmp_path, is_dir=False)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp_path), str(target_path))
        _restrict_fs_permissions(target_path, is_dir=False)
        _fsync_dir(target_path.parent)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _pack_secret_envelope(key_id: str, canonical_recovery_key: str) -> bytes:
    """Serialize a Recovery Key with its bound key_id into canonical UTF-8 JSON bytes."""
    clean_id = validate_key_id(key_id)
    derived = get_recovery_key_id(canonical_recovery_key)
    if not hmac.compare_digest(derived, clean_id):
        raise RecoveryKeyIntegrityError("Recovery Key does not match expected Key ID.")
    payload = {
        "app": APP_ID,
        "key_id": clean_id,
        "recovery_key": canonical_recovery_key,
        "v": KEY_ENVELOPE_VERSION,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _unpack_and_verify_secret_envelope(raw_bytes: bytes, expected_key_id: str) -> str:
    """Parse and strictly authenticate a decrypted/retrieved Recovery Key envelope."""
    clean_expected = validate_key_id(expected_key_id)
    try:
        obj = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RecoveryKeyIntegrityError(
            "Stored Recovery Key record is malformed or corrupted. Use your printed Recovery Card."
        ) from None

    if not isinstance(obj, dict):
        raise RecoveryKeyIntegrityError("Stored Recovery Key record has invalid structure.")
    if obj.get("v") != KEY_ENVELOPE_VERSION or obj.get("app") != APP_ID:
        raise RecoveryKeyIntegrityError("Stored Recovery Key record belongs to an incompatible format or application.")
    stored_id = obj.get("key_id")
    if not isinstance(stored_id, str) or not hmac.compare_digest(stored_id.upper(), clean_expected):
        raise RecoveryKeyIntegrityError(
            "Stored Recovery Key record Key ID mismatch (possible blob substitution)."
        )
    rk_val = obj.get("recovery_key")
    if not isinstance(rk_val, str):
        raise RecoveryKeyIntegrityError("Stored Recovery Key record is missing key payload.")
    try:
        validate_recovery_key(rk_val)
        canonical = normalize_recovery_key(rk_val)
        derived = get_recovery_key_id(canonical)
    except InvalidRecoveryKeyError:
        raise RecoveryKeyIntegrityError(
            "Stored Recovery Key failed checksum validation and cannot be trusted."
        ) from None
    if not hmac.compare_digest(derived, clean_expected):
        raise RecoveryKeyIntegrityError(
            "Stored Recovery Key does not match the expected Key ID."
        )
    return canonical


class _NonSecretKeyMetadataStore:
    """
    Atomic non-secret metadata index for historical and active Recovery Key IDs.
    Strictly forbids persisting any secret key material.
    """

    def __init__(self, storage_dir: Path, backend_name: str) -> None:
        self.storage_dir = Path(storage_dir)
        self.backend_name = backend_name
        self.meta_path = self.storage_dir / METADATA_FILENAME
        self._lock = threading.RLock()

    def ensure_accessible(self) -> None:
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            _restrict_fs_permissions(self.storage_dir, is_dir=True)
            # Probe writability without creating plaintext secrets
            if not os.access(str(self.storage_dir), os.R_OK | os.W_OK):
                raise RecoveryKeyStoreUnavailableError(
                    "Per-user Recovery Key metadata directory is not readable/writable."
                )
        except RecoveryKeyStoreUnavailableError:
            raise
        except OSError as exc:
            raise RecoveryKeyStoreUnavailableError(
                f"Per-user Recovery Key directory is unavailable: {exc.__class__.__name__}."
            ) from None

    def _read_unlocked(self) -> Dict[str, Any]:
        if not self.meta_path.exists():
            return {
                "schema_version": METADATA_SCHEMA_VERSION,
                "active_key_id": None,
                "keys": {},
            }
        try:
            raw = self.meta_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            raise RecoveryKeyIntegrityError(
                "Recovery Key metadata index is unreadable or corrupted."
            ) from None
        if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
            raise RecoveryKeyIntegrityError("Recovery Key metadata index has invalid structure.")
        return {
            "schema_version": int(data.get("schema_version") or METADATA_SCHEMA_VERSION),
            "active_key_id": data.get("active_key_id"),
            "keys": dict(data["keys"]),
        }

    def _write_unlocked(self, data: Dict[str, Any]) -> None:
        serialized = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True)
        # Defense-in-depth: never allow raw recovery key strings in metadata file
        if "KDHN-" in serialized.upper() or "recovery_key" in serialized.lower():
            raise RecoveryKeyStoreError("Refused to persist secret material into key metadata index.")
        _atomic_write_bytes(self.meta_path, serialized.encode("utf-8"))

    def record_key(
        self,
        key_id: str,
        *,
        activate: bool,
        source: str,
        created_at_utc: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_id = validate_key_id(key_id)
        ts = created_at_utc or utc_now_iso()
        with self._lock:
            self.ensure_accessible()
            data = self._read_unlocked()
            existing = data["keys"].get(clean_id)
            if existing is None:
                entry = {
                    "key_id": clean_id,
                    "created_at_utc": ts,
                    "storage_backend": self.backend_name,
                    "last_used_at_utc": None,
                    "source": source,
                }
                data["keys"][clean_id] = entry
            else:
                entry = dict(existing)
                entry["storage_backend"] = self.backend_name
                data["keys"][clean_id] = entry
            if activate or not data.get("active_key_id"):
                data["active_key_id"] = clean_id
            self._write_unlocked(data)
            out = dict(entry)
            out["is_active"] = data["active_key_id"] == clean_id
            return out

    def touch_last_used(self, key_id: str) -> None:
        clean_id = validate_key_id(key_id)
        with self._lock:
            if not self.meta_path.exists():
                return
            try:
                data = self._read_unlocked()
                if clean_id in data["keys"]:
                    data["keys"][clean_id]["last_used_at_utc"] = utc_now_iso()
                    self._write_unlocked(data)
            except RecoveryKeyStoreError:
                pass

    def get_active_key_id(self) -> Optional[str]:
        with self._lock:
            if not self.meta_path.exists():
                return None
            data = self._read_unlocked()
            active = data.get("active_key_id")
            if isinstance(active, str) and active in data["keys"]:
                return active
            return None

    def set_active_key_id(self, key_id: str) -> Dict[str, Any]:
        clean_id = validate_key_id(key_id)
        with self._lock:
            self.ensure_accessible()
            data = self._read_unlocked()
            if clean_id not in data["keys"]:
                raise KeyError(f"Recovery Key ID '{clean_id}' is not registered in local key metadata.")
            data["active_key_id"] = clean_id
            self._write_unlocked(data)
            out = dict(data["keys"][clean_id])
            out["is_active"] = True
            return out

    def list_entries(self) -> List[Dict[str, Any]]:
        with self._lock:
            if not self.meta_path.exists():
                return []
            data = self._read_unlocked()
            active_id = data.get("active_key_id")
            rows: List[Dict[str, Any]] = []
            for kid, entry in data["keys"].items():
                if not isinstance(entry, dict):
                    continue
                row = {
                    "key_id": str(entry.get("key_id") or kid),
                    "created_at_utc": entry.get("created_at_utc"),
                    "storage_backend": entry.get("storage_backend") or self.backend_name,
                    "last_used_at_utc": entry.get("last_used_at_utc"),
                    "source": entry.get("source") or "generated",
                    "is_active": kid == active_id,
                }
                rows.append(row)
            active_rows = [r for r in rows if r["is_active"]]
            hist_rows = sorted(
                [r for r in rows if not r["is_active"]],
                key=lambda r: r.get("created_at_utc") or "",
                reverse=True,
            )
            return active_rows + hist_rows

    def remove_key(self, key_id: str) -> bool:
        clean_id = validate_key_id(key_id)
        with self._lock:
            if not self.meta_path.exists():
                return False
            data = self._read_unlocked()
            if clean_id not in data["keys"]:
                return False
            data["keys"].pop(clean_id, None)
            if data.get("active_key_id") == clean_id:
                data["active_key_id"] = next(iter(data["keys"]), None)
            self._write_unlocked(data)
            return True


# ---------------------------------------------------------------------------
# Windows DPAPI Native Implementation (ctypes -> crypt32.dll / kernel32.dll)
# ---------------------------------------------------------------------------
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


class Win32DPAPIBackend:
    """
    Thin wrapper around Windows native DPAPI (`CryptProtectData` / `CryptUnprotectData`).
    Strictly enforces current-user scope (`CRYPTPROTECT_UI_FORBIDDEN`, NEVER `CRYPTPROTECT_LOCAL_MACHINE`)
    and frees all native output buffers via `kernel32.LocalFree`.
    """

    def __init__(self, *, crypt32: Any = None, kernel32: Any = None, platform_name: Optional[str] = None) -> None:
        self._platform = (platform_name or sys.platform).lower()
        self._crypt32 = crypt32
        self._kernel32 = kernel32
        if self._crypt32 is None and self._platform == "win32":
            self._load_win32_dlls()

    def _load_win32_dlls(self) -> None:
        try:
            c32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

            c32.CryptProtectData.argtypes = [
                ctypes.POINTER(_DATA_BLOB),  # pDataIn
                ctypes.c_wchar_p,  # szDataDescr
                ctypes.POINTER(_DATA_BLOB),  # pOptionalEntropy
                ctypes.c_void_p,  # pvReserved
                ctypes.c_void_p,  # pPromptStruct
                ctypes.c_uint32,  # dwFlags
                ctypes.POINTER(_DATA_BLOB),  # pDataOut
            ]
            c32.CryptProtectData.restype = ctypes.c_int

            c32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(_DATA_BLOB),  # pDataIn
                ctypes.POINTER(ctypes.c_void_p),  # ppszDataDescr (raw LPWSTR heap ptr for LocalFree)
                ctypes.POINTER(_DATA_BLOB),  # pOptionalEntropy
                ctypes.c_void_p,  # pvReserved
                ctypes.c_void_p,  # pPromptStruct
                ctypes.c_uint32,  # dwFlags
                ctypes.POINTER(_DATA_BLOB),  # pDataOut
            ]
            c32.CryptUnprotectData.restype = ctypes.c_int

            k32.LocalFree.argtypes = [ctypes.c_void_p]
            k32.LocalFree.restype = ctypes.c_void_p

            self._crypt32 = c32
            self._kernel32 = k32
        except (AttributeError, OSError):
            self._crypt32 = None
            self._kernel32 = None

    def is_available(self) -> bool:
        return (
            self._platform == "win32"
            and self._crypt32 is not None
            and self._kernel32 is not None
            and hasattr(self._crypt32, "CryptProtectData")
            and hasattr(self._crypt32, "CryptUnprotectData")
            and hasattr(self._kernel32, "LocalFree")
        )

    @staticmethod
    def _make_blob(raw: bytes) -> tuple[_DATA_BLOB, Any]:
        buf = ctypes.create_string_buffer(raw, len(raw))
        blob = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        return blob, buf

    def _free_native_ptr(self, ptr: Any) -> None:
        if ptr and self._kernel32 is not None:
            try:
                addr = ctypes.cast(ptr, ctypes.c_void_p).value
                if addr:
                    self._kernel32.LocalFree(addr)
            except Exception:
                pass

    def protect(self, plaintext: bytes, entropy: bytes, *, flags: int = CRYPTPROTECT_UI_FORBIDDEN) -> bytes:
        if not self.is_available():
            raise RecoveryKeyStoreUnavailableError("Windows DPAPI is unavailable on this system.")
        if flags & CRYPTPROTECT_LOCAL_MACHINE:
            raise ValueError("CRYPTPROTECT_LOCAL_MACHINE is strictly forbidden for Recovery Key protection.")
        effective_flags = (int(flags) | CRYPTPROTECT_UI_FORBIDDEN) & ~CRYPTPROTECT_LOCAL_MACHINE

        in_blob, _in_buf = self._make_blob(plaintext)
        ent_blob, _ent_buf = self._make_blob(entropy)
        out_blob = _DATA_BLOB()

        ok = self._crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            DPAPI_DESCRIPTION,
            ctypes.byref(ent_blob),
            None,
            None,
            effective_flags,
            ctypes.byref(out_blob),
        )
        if not ok:
            err_code = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
            raise RecoveryKeyStoreUnavailableError(
                f"Windows DPAPI protection failed (Win32 error {err_code})."
            )
        try:
            if not out_blob.pbData or out_blob.cbData <= 0:
                raise RecoveryKeyStoreError("Windows DPAPI returned an empty protected blob.")
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            self._free_native_ptr(out_blob.pbData)

    def unprotect(self, ciphertext: bytes, entropy: bytes, *, flags: int = CRYPTPROTECT_UI_FORBIDDEN) -> bytes:
        if not self.is_available():
            raise RecoveryKeyStoreUnavailableError("Windows DPAPI is unavailable on this system.")
        if flags & CRYPTPROTECT_LOCAL_MACHINE:
            raise ValueError("CRYPTPROTECT_LOCAL_MACHINE is strictly forbidden for Recovery Key unprotection.")
        effective_flags = (int(flags) | CRYPTPROTECT_UI_FORBIDDEN) & ~CRYPTPROTECT_LOCAL_MACHINE

        in_blob, _in_buf = self._make_blob(ciphertext)
        ent_blob, _ent_buf = self._make_blob(entropy)
        out_blob = _DATA_BLOB()
        descr_ptr = ctypes.c_void_p()

        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            ctypes.byref(descr_ptr),
            ctypes.byref(ent_blob),
            None,
            None,
            effective_flags,
            ctypes.byref(out_blob),
        )
        if not ok:
            err_code = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
            # Never reveal secret material; signal corruption / wrong profile / wrong entropy cleanly
            raise RecoveryKeyIntegrityError(
                f"Windows DPAPI could not decrypt the stored Recovery Key (Win32 error {err_code}). "
                "The protected key may be corrupted or belong to a different Windows user profile. "
                "Use your printed Recovery Card instead."
            )
        try:
            if not out_blob.pbData or out_blob.cbData <= 0:
                raise RecoveryKeyIntegrityError("Windows DPAPI returned empty plaintext.")
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            self._free_native_ptr(out_blob.pbData)
            if descr_ptr.value:
                self._kernel32.LocalFree(descr_ptr.value)


class WindowsDPAPIRecoveryKeyStore(RecoveryKeyStore):
    """
    Windows Current-User DPAPI Recovery Key Store.
    Stores ONLY DPAPI-encrypted blobs (`rk_<KEY_ID>.dpapi`) inside `%LOCALAPPDATA%\\com.krushidhan.erp\\recovery_keys`.
    """

    def __init__(
        self,
        storage_dir: Optional[Union[str, Path]] = None,
        *,
        dpapi_backend: Optional[Any] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._dpapi = dpapi_backend if dpapi_backend is not None else Win32DPAPIBackend()
        if storage_dir is not None:
            self.storage_dir = Path(storage_dir)
        else:
            self.storage_dir = get_desktop_app_dir(platform_name="win32") / "recovery_keys"
        self._meta = _NonSecretKeyMetadataStore(self.storage_dir, self.backend_name)

    @property
    def backend_name(self) -> str:
        return "windows_dpapi"

    @staticmethod
    def _entropy_for_key(key_id: str) -> bytes:
        return DPAPI_ENTROPY_PREFIX + validate_key_id(key_id).encode("ascii")

    def _blob_path(self, key_id: str) -> Path:
        clean_id = validate_key_id(key_id)
        return self.storage_dir / f"rk_{clean_id}.dpapi"

    def is_available(self) -> bool:
        if not self._dpapi.is_available():
            return False
        try:
            self._meta.ensure_accessible()
            return True
        except RecoveryKeyStoreError:
            return False

    def _require_available(self) -> None:
        if not self._dpapi.is_available():
            raise RecoveryKeyStoreUnavailableError(
                "Windows DPAPI key storage is unavailable on this computer."
            )
        self._meta.ensure_accessible()

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
        envelope = _pack_secret_envelope(key_id, canonical)
        entropy = self._entropy_for_key(key_id)

        with self._lock:
            protected_bytes = self._dpapi.protect(envelope, entropy, flags=CRYPTPROTECT_UI_FORBIDDEN)
            if not protected_bytes or canonical.encode("utf-8") in protected_bytes:
                raise RecoveryKeyStoreError("DPAPI protection produced invalid output.")
            blob_file = self._blob_path(key_id)
            _atomic_write_bytes(blob_file, DPAPI_BLOB_MAGIC + protected_bytes)
            self._meta.record_key(
                key_id,
                activate=activate,
                source=source,
                created_at_utc=created_at_utc,
            )
        return key_id

    def get_key(self, key_id: Optional[str] = None) -> Optional[str]:
        self._require_available()
        with self._lock:
            target_id = validate_key_id(key_id) if key_id is not None else self._meta.get_active_key_id()
            if not target_id:
                return None
            blob_file = self._blob_path(target_id)
            if not blob_file.exists():
                return None
            try:
                raw_blob = blob_file.read_bytes()
            except OSError:
                raise RecoveryKeyStoreUnavailableError(
                    "Unable to read protected Recovery Key blob from Windows user profile."
                ) from None

            if not raw_blob.startswith(DPAPI_BLOB_MAGIC) or len(raw_blob) <= len(DPAPI_BLOB_MAGIC):
                raise RecoveryKeyIntegrityError(
                    "Protected Recovery Key file is corrupted or malformed. Use your Recovery Card."
                )

            ciphertext = raw_blob[len(DPAPI_BLOB_MAGIC) :]
            entropy = self._entropy_for_key(target_id)
            plaintext = self._dpapi.unprotect(ciphertext, entropy, flags=CRYPTPROTECT_UI_FORBIDDEN)
            canonical = _unpack_and_verify_secret_envelope(plaintext, target_id)
            self._meta.touch_last_used(target_id)
            return canonical

    def get_active_key_id(self) -> Optional[str]:
        if not self.is_available():
            return None
        return self._meta.get_active_key_id()

    def set_active_key(self, key_id: str) -> Dict[str, Any]:
        self._require_available()
        clean_id = validate_key_id(key_id)
        with self._lock:
            self.verify_stored_key(clean_id)
            return self._meta.set_active_key_id(clean_id)

    def list_key_metadata(self) -> List[Dict[str, Any]]:
        self._require_available()
        return self._meta.list_entries()

    def verify_stored_key(self, key_id: Optional[str] = None) -> Dict[str, Any]:
        self._require_available()
        target_id = validate_key_id(key_id) if key_id is not None else self._meta.get_active_key_id()
        if not target_id:
            raise RecoveryKeyStoreError("No active Recovery Key is stored in Windows DPAPI.")
        secret = self.get_key(target_id)
        if secret is None:
            raise RecoveryKeyStoreError("Requested Recovery Key ID was not found in Windows DPAPI storage.")
        return {
            "valid": True,
            "key_id": target_id,
            "is_active": target_id == self._meta.get_active_key_id(),
            "storage_backend": self.backend_name,
        }

    def delete_key(self, key_id: Optional[str] = None) -> bool:
        self._require_available()
        with self._lock:
            target_id = validate_key_id(key_id) if key_id is not None else self._meta.get_active_key_id()
            if not target_id:
                return False
            blob_file = self._blob_path(target_id)
            existed = blob_file.exists()
            if existed:
                blob_file.unlink()
            meta_removed = self._meta.remove_key(target_id)
            return existed or meta_removed

    def has_key(self) -> bool:
        if not self.is_available():
            return False
        active_id = self._meta.get_active_key_id()
        return bool(active_id and self._blob_path(active_id).exists())


# ---------------------------------------------------------------------------
# macOS Keychain Implementation (Explicit keyring.backends.macOS.Keyring ONLY)
# ---------------------------------------------------------------------------
def _validate_macos_keyring_backend(backend: Any, *, allow_test_double: bool = False) -> None:
    """
    Strictly verify the selected keyring backend is the real macOS Keychain backend
    (`keyring.backends.macOS.Keyring`), never a null, plaintext, file, chainer, or fallback backend.
    """
    if backend is None:
        raise RecoveryKeyStoreUnavailableError("macOS Keychain backend is unavailable.")

    cls_name = type(backend).__name__.lower()
    mod_name = str(getattr(type(backend), "__module__", "")).lower()

    for forbidden in _DISALLOWED_BACKEND_KEYWORDS:
        if forbidden in cls_name or forbidden in mod_name:
            raise RecoveryKeyStoreUnavailableError(
                "Disallowed insecure/fallback keyring backend rejected. macOS Keychain is required."
            )

    if allow_test_double:
        for req_method in ("get_password", "set_password", "delete_password"):
            if not callable(getattr(backend, req_method, None)):
                raise RecoveryKeyStoreUnavailableError("Invalid Keychain test backend.")
        return

    if mod_name != "keyring.backends.macos" or type(backend).__name__ != "Keyring":
        raise RecoveryKeyStoreUnavailableError(
            "Only the explicit macOS Keychain backend (keyring.backends.macOS.Keyring) is permitted."
        )


def _instantiate_explicit_macos_keyring() -> Any:
    """Explicitly import and instantiate `keyring.backends.macOS.Keyring` without auto-discovery."""
    if sys.platform != "darwin":
        raise RecoveryKeyStoreUnavailableError("macOS Keychain is only available on macOS.")
    try:
        from keyring.backends.macOS import Keyring as MacOSKeyring
    except ImportError:
        raise RecoveryKeyStoreUnavailableError(
            "macOS Keychain support requires the 'keyring' package (keyring.backends.macOS)."
        ) from None

    backend = MacOSKeyring()
    _validate_macos_keyring_backend(backend, allow_test_double=False)
    try:
        # Accessing `.priority` triggers keyring's own macOS platform check
        if float(getattr(backend, "priority", 0)) <= 0:
            raise RecoveryKeyStoreUnavailableError("macOS Keychain backend reported non-operational priority.")
    except RecoveryKeyStoreUnavailableError:
        raise
    except Exception:
        raise RecoveryKeyStoreUnavailableError("macOS Keychain backend is not available on this system.") from None
    return backend


class MacOSKeychainRecoveryKeyStore(RecoveryKeyStore):
    """
    macOS Keychain-backed Recovery Key Store using explicit `keyring.backends.macOS.Keyring`
    (`Security.framework` C API via ctypes; never spawns `security` CLI subprocesses and never
    falls back to null, plaintext, file, or third-party backends).
    """

    def __init__(
        self,
        storage_dir: Optional[Union[str, Path]] = None,
        *,
        service_name: str = MACOS_KEYCHAIN_SERVICE,
        backend: Optional[Any] = None,
        _allow_test_backend: bool = False,
    ) -> None:
        self._lock = threading.RLock()
        self.service_name = str(service_name or MACOS_KEYCHAIN_SERVICE).strip()
        if not self.service_name:
            raise ValueError("macOS Keychain service_name cannot be empty.")
        self._allow_test_backend = bool(_allow_test_backend)
        self._explicit_backend = backend
        self._cached_backend: Optional[Any] = None
        if backend is not None:
            _validate_macos_keyring_backend(backend, allow_test_double=self._allow_test_backend)
            self._cached_backend = backend

        if storage_dir is not None:
            self.storage_dir = Path(storage_dir)
        else:
            self.storage_dir = get_desktop_app_dir(platform_name="darwin") / "recovery_keys"
        self._meta = _NonSecretKeyMetadataStore(self.storage_dir, self.backend_name)

    @property
    def backend_name(self) -> str:
        return "macos_keychain"

    def _get_backend(self) -> Any:
        if self._cached_backend is not None:
            _validate_macos_keyring_backend(self._cached_backend, allow_test_double=self._allow_test_backend)
            return self._cached_backend
        backend = _instantiate_explicit_macos_keyring()
        self._cached_backend = backend
        return backend

    def is_available(self) -> bool:
        try:
            self._get_backend()
            self._meta.ensure_accessible()
            return True
        except RecoveryKeyStoreError:
            return False

    def _map_keychain_error(self, exc: Exception, *, operation: str) -> RecoveryKeyStoreUnavailableError:
        name = type(exc).__name__
        detail = str(exc)
        lowered = detail.lower()
        if any(tok in lowered or tok in name.lower() for tok in ("denied", "cancel", "auth", "lock", "interaction")):
            return RecoveryKeyStoreUnavailableError(
                f"macOS Keychain access was locked or denied during {operation}. "
                "Unlock your login Keychain or enter your Recovery Key manually from your Recovery Card."
            )
        return RecoveryKeyStoreUnavailableError(
            f"macOS Keychain is unavailable during {operation} ({name}). "
            "Enter your Recovery Key manually from your Recovery Card."
        )

    def store_key(
        self,
        recovery_key: str,
        *,
        activate: bool = True,
        source: str = "generated",
        created_at_utc: Optional[str] = None,
    ) -> str:
        backend = self._get_backend()
        self._meta.ensure_accessible()
        canonical = normalize_recovery_key(recovery_key)
        key_id = get_recovery_key_id(canonical)
        envelope_str = _pack_secret_envelope(key_id, canonical).decode("utf-8")

        with self._lock:
            try:
                backend.set_password(self.service_name, key_id, envelope_str)
            except RecoveryKeyStoreError:
                raise
            except Exception as exc:
                raise self._map_keychain_error(exc, operation="storing key") from None

            self._meta.record_key(
                key_id,
                activate=activate,
                source=source,
                created_at_utc=created_at_utc,
            )
        return key_id

    def get_key(self, key_id: Optional[str] = None) -> Optional[str]:
        backend = self._get_backend()
        with self._lock:
            target_id = validate_key_id(key_id) if key_id is not None else self._meta.get_active_key_id()
            if not target_id:
                return None
            try:
                stored_val = backend.get_password(self.service_name, target_id)
            except RecoveryKeyStoreError:
                raise
            except Exception as exc:
                raise self._map_keychain_error(exc, operation="retrieving key") from None

            if stored_val is None:
                return None
            if not isinstance(stored_val, str):
                raise RecoveryKeyIntegrityError("macOS Keychain entry returned invalid data type.")

            canonical = _unpack_and_verify_secret_envelope(stored_val.encode("utf-8"), target_id)
            self._meta.touch_last_used(target_id)
            return canonical

    def get_active_key_id(self) -> Optional[str]:
        if not self.is_available():
            return None
        return self._meta.get_active_key_id()

    def set_active_key(self, key_id: str) -> Dict[str, Any]:
        self._get_backend()
        self._meta.ensure_accessible()
        clean_id = validate_key_id(key_id)
        with self._lock:
            self.verify_stored_key(clean_id)
            return self._meta.set_active_key_id(clean_id)

    def list_key_metadata(self) -> List[Dict[str, Any]]:
        self._get_backend()
        return self._meta.list_entries()

    def verify_stored_key(self, key_id: Optional[str] = None) -> Dict[str, Any]:
        self._get_backend()
        target_id = validate_key_id(key_id) if key_id is not None else self._meta.get_active_key_id()
        if not target_id:
            raise RecoveryKeyStoreError("No active Recovery Key is stored in macOS Keychain.")
        secret = self.get_key(target_id)
        if secret is None:
            raise RecoveryKeyStoreError("Requested Recovery Key ID was not found in macOS Keychain.")
        return {
            "valid": True,
            "key_id": target_id,
            "is_active": target_id == self._meta.get_active_key_id(),
            "storage_backend": self.backend_name,
        }

    def delete_key(self, key_id: Optional[str] = None) -> bool:
        backend = self._get_backend()
        with self._lock:
            target_id = validate_key_id(key_id) if key_id is not None else self._meta.get_active_key_id()
            if not target_id:
                return False
            deleted_kc = False
            try:
                backend.delete_password(self.service_name, target_id)
                deleted_kc = True
            except Exception as exc:
                # PasswordDeleteError / NotFound means already absent
                low = f"{type(exc).__name__} {exc}".lower()
                if "not found" not in low and "notfound" not in low and "passworddeleteerror" not in low:
                    raise self._map_keychain_error(exc, operation="deleting test key") from None
            deleted_meta = self._meta.remove_key(target_id)
            return deleted_kc or deleted_meta

    def has_key(self) -> bool:
        if not self.is_available():
            return False
        active_id = self._meta.get_active_key_id()
        if not active_id:
            return False
        try:
            return self.get_key(active_id) is not None
        except RecoveryKeyStoreError:
            return False


# ---------------------------------------------------------------------------
# Unsupported Platform Store & Fail-Closed Production Selector
# ---------------------------------------------------------------------------
class UnsupportedRecoveryKeyStore(RecoveryKeyStore):
    """Explicit fail-closed store for unsupported operating systems. Never falls back to plaintext or memory."""

    def __init__(self, platform_name: Optional[str] = None) -> None:
        self._platform = (platform_name or sys.platform).lower()

    @property
    def backend_name(self) -> str:
        return "unsupported"

    def is_available(self) -> bool:
        return False

    def _fail(self) -> None:
        raise RecoveryKeyStoreUnavailableError(
            f"OS-protected Recovery Key storage is not supported on platform '{self._platform}'. "
            "Automatic stored-key operations are disabled; enter your Recovery Key manually from your Recovery Card."
        )

    def store_key(self, recovery_key: str, *, activate: bool = True, source: str = "generated", created_at_utc: Optional[str] = None) -> str:
        self._fail()
        return ""

    def get_key(self, key_id: Optional[str] = None) -> Optional[str]:
        self._fail()
        return None

    def get_active_key_id(self) -> Optional[str]:
        return None

    def set_active_key(self, key_id: str) -> Dict[str, Any]:
        self._fail()
        return {}

    def list_key_metadata(self) -> List[Dict[str, Any]]:
        return []

    def verify_stored_key(self, key_id: Optional[str] = None) -> Dict[str, Any]:
        self._fail()
        return {}

    def delete_key(self, key_id: Optional[str] = None) -> bool:
        self._fail()
        return False

    def has_key(self) -> bool:
        return False

    def get_status(self) -> Dict[str, Any]:
        return {
            "available": False,
            "backend": self.backend_name,
            "platform": self._platform,
            "has_active_key": False,
            "active_key_id": None,
            "key_count": 0,
            "keys": [],
            "message": (
                f"OS-protected key storage is not supported on '{self._platform}'. "
                "Automatic stored-key operations are disabled; you can still inspect or restore encrypted archives by entering your Recovery Key manually from your Recovery Card."
            ),
            "portable_recovery_warning": PORTABLE_RECOVERY_WARNING,
        }


def get_production_recovery_key_store(
    *,
    platform_name: Optional[str] = None,
    storage_dir: Optional[Union[str, Path]] = None,
) -> RecoveryKeyStore:
    """
    Production platform selector:
    - Windows (`win32`) -> WindowsDPAPIRecoveryKeyStore
    - macOS (`darwin`)  -> MacOSKeychainRecoveryKeyStore
    - Other platforms   -> UnsupportedRecoveryKeyStore (fail closed)
    NEVER falls back to InMemoryRecoveryKeyStore or plaintext files.
    """
    plat = (platform_name or sys.platform).lower()
    if plat == "win32":
        return WindowsDPAPIRecoveryKeyStore(storage_dir=storage_dir)
    if plat == "darwin":
        return MacOSKeychainRecoveryKeyStore(storage_dir=storage_dir)
    return UnsupportedRecoveryKeyStore(platform_name=plat)


_STORE_LOCK = threading.Lock()
_TEST_OVERRIDE_STORE: Optional[RecoveryKeyStore] = None


def get_recovery_key_store() -> RecoveryKeyStore:
    """Return the active RecoveryKeyStore (production platform store unless overridden by isolated test fixture)."""
    with _STORE_LOCK:
        if _TEST_OVERRIDE_STORE is not None:
            return _TEST_OVERRIDE_STORE
    return get_production_recovery_key_store()


def set_recovery_key_store_for_testing(store: Optional[RecoveryKeyStore]) -> None:
    """Explicit test-only hook for injecting an isolated RecoveryKeyStore in automated test suites."""
    global _TEST_OVERRIDE_STORE
    with _STORE_LOCK:
        _TEST_OVERRIDE_STORE = store


def reset_recovery_key_store() -> None:
    """Clear any test-only store override so normal production platform selection applies."""
    set_recovery_key_store_for_testing(None)
