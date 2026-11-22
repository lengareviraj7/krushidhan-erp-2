"""
Encrypted Backup Container (.kdb.enc v1) Service for Krushidhan ERP (Phase 2C-4B).

Provides streaming compression (stdlib zlib) + authenticated chunked encryption
(PyCA `cryptography` AES-256-GCM + HKDF-SHA256) so verified SQLite backups can be
exported offline to USB/external disks/cloud and restored on a replacement Windows
or macOS computer using only the customer's Recovery Key.

Strict safety guarantees:
- Never reads or modifies the live SQLite database directly for encryption.
- Never overwrites the live database during decryption.
- Never logs, stores, or transmits Recovery Keys or Data Encryption Keys.
- Uses fresh per-file DEK, salt, wrap nonce, and chunk nonce prefix; never reuses nonces.
- Verifies every newly created archive via full round-trip decryption + SHA-256 + SQLite
  integrity/FK checks before atomic promotion to `.kdb.enc`.
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
import shutil
import stat
import struct
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Tuple, Union

from src.db.connection import DatabaseManager, get_db_manager
from src.services.backup_service import BackupService, pinned_backup_file
from src.services.recovery_key import (
    InvalidRecoveryKeyError,
    RecoveryKeyIntegrityError,
    RecoveryKeyStore,
    RecoveryKeyStoreError,
    RecoveryKeyStoreUnavailableError,
    generate_recovery_key,
    get_recovery_key_id,
    validate_recovery_key,
)

logger = logging.getLogger("agri_erp.encrypted_backup")

KDB_ENC_MAGIC = b"KDHNENC1"
KDB_ENC_FORMAT_VERSION = 1
KDB_ENC_CIPHER = "AES-256-GCM"
KDB_ENC_KDF = "HKDF-SHA256"
KDB_ENC_COMPRESSION = "zlib"
KDB_ENC_HKDF_INFO = b"krushidhan-kdb-enc-v1-kek"

MAX_HEADER_BYTES = 64 * 1024  # 64 KiB strict upper bound
DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB authenticated chunks
MIN_CHUNK_SIZE = 4096
MAX_CHUNK_SIZE = 16 * 1024 * 1024  # 16 MiB
DEFAULT_MAX_PLAINTEXT_BYTES = 512 * 1024 * 1024  # 512 MiB decompression cap
DEFAULT_MAX_COMPRESSED_BYTES = 512 * 1024 * 1024  # 512 MiB compressed stream cap
MAX_CHUNK_INDEX = 0xFFFFFFFF

DEK_BYTES = 32
FILE_SALT_BYTES = 16
NONCE_PREFIX_BYTES = 8
WRAP_NONCE_BYTES = 12
GCM_TAG_BYTES = 16

# Single source of truth for `.kdb.enc` v1 wire framing (shared by pack, unpack, and size derivation).
# Note: On-disk chunk records store `record_len (uint32_be) || is_final (uint8)` (`>IB`, 5 bytes)
# followed by `ciphertext_and_tag` (`record_len` bytes). `chunk_index` is implicit sequential
# (0, 1, 2, ...) and cryptographically bound in both the 12-byte GCM nonce (`nonce_prefix || >I`)
# and the 41-byte GCM AAD (`header_sha256 || >IBI(chunk_index, is_final, record_len)`).
HEADER_LEN_STRUCT_FMT = ">I"  # uint32_be canonical JSON header byte length (4 bytes)
WRAP_LEN_STRUCT_FMT = ">H"  # uint16_be wrapped key-box byte length (2 bytes)
WRAP_SIZE_STRUCT_FMT = ">Q"  # uint64_be plaintext size inside wrapped key box (8 bytes)
CHUNK_RECORD_PREFIX_STRUCT_FMT = ">IB"  # uint32_be record_len + uint8 is_final (5 bytes on disk)
CHUNK_AAD_STRUCT_FMT = ">IBI"  # chunk_index + is_final + record_len bound in AEAD AAD (not written on disk)

HEADER_LEN_PREFIX_BYTES = struct.calcsize(HEADER_LEN_STRUCT_FMT)  # 4 bytes
WRAP_LEN_PREFIX_BYTES = struct.calcsize(WRAP_LEN_STRUCT_FMT)  # 2 bytes
CHUNK_RECORD_PREFIX_BYTES = struct.calcsize(CHUNK_RECORD_PREFIX_STRUCT_FMT)  # 5 bytes
CHUNK_RECORD_OVERHEAD_BYTES = CHUNK_RECORD_PREFIX_BYTES + GCM_TAG_BYTES  # 21 bytes/chunk
WRAP_PLAINTEXT_BYTES = DEK_BYTES + 32 + struct.calcsize(WRAP_SIZE_STRUCT_FMT)  # DEK(32) + SHA256(32) + size(8) = 72 bytes
EXPECTED_WRAP_BOX_BYTES = WRAP_PLAINTEXT_BYTES + GCM_TAG_BYTES  # 88 bytes


def calculate_max_encrypted_archive_bytes(
    *,
    max_compressed_bytes: int = DEFAULT_MAX_COMPRESSED_BYTES,
    min_chunk_size: int = MIN_CHUNK_SIZE,
    max_header_bytes: int = MAX_HEADER_BYTES,
) -> int:
    """
    Derive the exact maximum valid `.kdb.enc` v1 archive size (in bytes) directly from the
    actual wire struct formats and configured bounds:

    - Fixed prefix & maximum header envelope:
      magic (8) + calcsize(">I") (4) + max_header_bytes + wrap_nonce (12)
      + calcsize(">H") (2) + wrapped_box (88)
    - Maximum compressed payload:
      max_compressed_bytes
    - Worst-case authenticated chunk overhead at `min_chunk_size`:
      ceil(max_compressed_bytes / min_chunk_size) data-carrying chunks plus
      1 permitted final empty chunk (e.g. 0-byte payload or trailing empty is_final=1 record),
      each adding calcsize(">IB") (5) + GCM_TAG_BYTES (16) = 21 bytes.
    """
    if isinstance(max_compressed_bytes, bool) or not isinstance(max_compressed_bytes, int) or max_compressed_bytes < 0:
        raise ValueError("max_compressed_bytes must be a non-negative integer.")
    if (
        isinstance(min_chunk_size, bool)
        or not isinstance(min_chunk_size, int)
        or min_chunk_size < MIN_CHUNK_SIZE
        or min_chunk_size > MAX_CHUNK_SIZE
    ):
        raise ValueError(f"min_chunk_size must be between {MIN_CHUNK_SIZE} and {MAX_CHUNK_SIZE} bytes.")
    if (
        isinstance(max_header_bytes, bool)
        or not isinstance(max_header_bytes, int)
        or max_header_bytes < 2
        or max_header_bytes > MAX_HEADER_BYTES
    ):
        raise ValueError(f"max_header_bytes must be between 2 and {MAX_HEADER_BYTES} bytes.")

    fixed_envelope_bytes = (
        len(KDB_ENC_MAGIC)
        + HEADER_LEN_PREFIX_BYTES
        + max_header_bytes
        + WRAP_NONCE_BYTES
        + WRAP_LEN_PREFIX_BYTES
        + EXPECTED_WRAP_BOX_BYTES
    )
    max_data_chunks = (
        (max_compressed_bytes + min_chunk_size - 1) // min_chunk_size
        if max_compressed_bytes > 0
        else 0
    )
    max_total_chunks = max_data_chunks + 1  # +1 covers permitted final empty chunk
    if max_total_chunks - 1 > MAX_CHUNK_INDEX:
        raise ValueError("Configured archive limits exceed 32-bit chunk index capacity.")

    return fixed_envelope_bytes + max_compressed_bytes + (max_total_chunks * CHUNK_RECORD_OVERHEAD_BYTES)


MAX_ENCRYPTED_ARCHIVE_BYTES = calculate_max_encrypted_archive_bytes()
MAX_ENCRYPTED_UPLOAD_BYTES = MAX_ENCRYPTED_ARCHIVE_BYTES

_ALLOWED_HEADER_KEYS = frozenset(
    {
        "format_version",
        "cipher",
        "kdf",
        "compression",
        "key_id",
        "file_salt_b64",
        "nonce_prefix_b64",
        "chunk_size",
        "schema_version",
        "created_at_utc",
    }
)
_KEY_ID_RE = re.compile(r"^[0-9A-F]{16}$")
_UTC_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class EncryptedBackupError(RuntimeError):
    """Base error for encrypted backup processing failures (always sanitized)."""


class CryptoDependencyError(EncryptedBackupError):
    """Raised when the PyCA `cryptography` package is not available in the runtime."""


class EncryptedBackupFormatError(EncryptedBackupError):
    """Raised when a `.kdb.enc` file has invalid magic, header, framing, truncation, or trailing data."""


class EncryptedBackupAuthError(EncryptedBackupError):
    """Raised when Recovery Key unwrapping or AEAD chunk authentication fails."""


def _require_cryptography() -> Tuple[Any, Any, Any, Any]:
    """Import PyCA `cryptography` primitives lazily so unrelated modules remain import-safe."""
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        return AESGCM, HKDF, hashes, InvalidTag
    except ImportError as exc:
        raise CryptoDependencyError(
            "Encrypted backup support requires the 'cryptography' library."
        ) from exc


def _strict_json_object_pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """Reject duplicate JSON keys during header parsing."""
    obj: Dict[str, Any] = {}
    for k, v in pairs:
        if k in obj:
            raise EncryptedBackupFormatError("Encrypted backup header contains duplicate keys.")
        obj[k] = v
    return obj


def _strict_b64decode(raw_str: Any, expected_len: int, field_name: str) -> bytes:
    if not isinstance(raw_str, str) or not raw_str:
        raise EncryptedBackupFormatError(f"Invalid header field '{field_name}'.")
    try:
        decoded = base64.b64decode(raw_str.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise EncryptedBackupFormatError(f"Malformed Base64 in header field '{field_name}'.") from None
    if len(decoded) != expected_len:
        raise EncryptedBackupFormatError(f"Unexpected length for header field '{field_name}'.")
    return decoded


def _is_strict_int(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def _read_exact(fh: BinaryIO, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = fh.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _restrict_path_permissions(path: Path, *, is_dir: bool = False) -> None:
    """Apply owner-only POSIX permissions (0o700 dir / 0o600 file); safe no-op on Windows."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, stat.S_IRWXU if is_dir else (stat.S_IRUSR | stat.S_IWUSR))
    except OSError:
        pass


def _create_restricted_temp_dir(prefix: str = "kdb_enc_") -> Path:
    tmp_dir = Path(tempfile.mkdtemp(prefix=prefix))
    _restrict_path_permissions(tmp_dir, is_dir=True)
    return tmp_dir


def _derive_kek(raw_recovery_key: bytes, file_salt: bytes) -> bytes:
    _, HKDF, hashes, _ = _require_cryptography()
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=DEK_BYTES,
        salt=file_salt,
        info=KDB_ENC_HKDF_INFO,
    )
    return hkdf.derive(raw_recovery_key)


def _chunk_nonce(nonce_prefix: bytes, chunk_index: int) -> bytes:
    if len(nonce_prefix) != NONCE_PREFIX_BYTES:
        raise EncryptedBackupFormatError("Invalid chunk nonce prefix length.")
    if chunk_index < 0 or chunk_index > MAX_CHUNK_INDEX:
        raise EncryptedBackupFormatError("Encrypted backup exceeded 32-bit chunk index limit.")
    return nonce_prefix + struct.pack(">I", chunk_index)


def _chunk_aad(header_sha256: bytes, chunk_index: int, is_final: int, record_len: int) -> bytes:
    return header_sha256 + struct.pack(CHUNK_AAD_STRUCT_FMT, chunk_index, is_final, record_len)


def _parse_and_validate_header(fh: BinaryIO) -> Dict[str, Any]:
    """Read and strictly validate magic, bounded header length, canonical header JSON, and wrap framing."""
    magic = _read_exact(fh, len(KDB_ENC_MAGIC))
    if magic != KDB_ENC_MAGIC:
        raise EncryptedBackupFormatError("Not a valid Krushidhan encrypted backup file (invalid magic header).")

    len_bytes = _read_exact(fh, HEADER_LEN_PREFIX_BYTES)
    if len(len_bytes) != HEADER_LEN_PREFIX_BYTES:
        raise EncryptedBackupFormatError("Encrypted backup file is truncated in header length.")
    (header_len,) = struct.unpack(HEADER_LEN_STRUCT_FMT, len_bytes)
    if header_len < 2 or header_len > MAX_HEADER_BYTES:
        raise EncryptedBackupFormatError("Encrypted backup header length is out of allowed bounds.")

    header_bytes = _read_exact(fh, header_len)
    if len(header_bytes) != header_len:
        raise EncryptedBackupFormatError("Encrypted backup file is truncated inside header.")

    try:
        header_text = header_bytes.decode("utf-8")
        header_obj = json.loads(header_text, object_pairs_hook=_strict_json_object_pairs)
    except EncryptedBackupFormatError:
        raise
    except (UnicodeDecodeError, ValueError):
        raise EncryptedBackupFormatError("Encrypted backup header is malformed JSON.") from None

    if not isinstance(header_obj, dict):
        raise EncryptedBackupFormatError("Encrypted backup header must be a JSON object.")

    keys = set(header_obj.keys())
    if keys != _ALLOWED_HEADER_KEYS:
        raise EncryptedBackupFormatError("Encrypted backup header contains missing or unexpected fields.")

    if not _is_strict_int(header_obj["format_version"]) or header_obj["format_version"] != KDB_ENC_FORMAT_VERSION:
        raise EncryptedBackupFormatError("Unsupported encrypted backup format version.")
    if header_obj["cipher"] != KDB_ENC_CIPHER:
        raise EncryptedBackupFormatError("Unsupported encryption cipher in backup header.")
    if header_obj["kdf"] != KDB_ENC_KDF:
        raise EncryptedBackupFormatError("Unsupported key derivation algorithm in backup header.")
    if header_obj["compression"] != KDB_ENC_COMPRESSION:
        raise EncryptedBackupFormatError("Unsupported compression algorithm in backup header.")

    key_id = header_obj["key_id"]
    if not isinstance(key_id, str) or not _KEY_ID_RE.match(key_id):
        raise EncryptedBackupFormatError("Invalid key identifier in encrypted backup header.")

    chunk_size = header_obj["chunk_size"]
    if not _is_strict_int(chunk_size) or chunk_size < MIN_CHUNK_SIZE or chunk_size > MAX_CHUNK_SIZE:
        raise EncryptedBackupFormatError("Invalid chunk size in encrypted backup header.")

    schema_version = header_obj["schema_version"]
    if not _is_strict_int(schema_version) or schema_version < 1:
        raise EncryptedBackupFormatError("Invalid schema version in encrypted backup header.")

    created_at_utc = header_obj["created_at_utc"]
    if not isinstance(created_at_utc, str) or not _UTC_ISO_RE.match(created_at_utc):
        raise EncryptedBackupFormatError("Invalid creation timestamp in encrypted backup header.")

    file_salt = _strict_b64decode(header_obj["file_salt_b64"], FILE_SALT_BYTES, "file_salt_b64")
    nonce_prefix = _strict_b64decode(header_obj["nonce_prefix_b64"], NONCE_PREFIX_BYTES, "nonce_prefix_b64")

    # Enforce canonical header serialization for cross-platform determinism
    canonical_bytes = json.dumps(header_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if canonical_bytes != header_bytes:
        raise EncryptedBackupFormatError("Encrypted backup header is not in canonical format.")

    header_envelope = magic + len_bytes + header_bytes
    header_sha256 = hashlib.sha256(header_envelope).digest()

    wrap_nonce = _read_exact(fh, WRAP_NONCE_BYTES)
    if len(wrap_nonce) != WRAP_NONCE_BYTES:
        raise EncryptedBackupFormatError("Encrypted backup file is truncated in key-wrap nonce.")

    wrap_len_bytes = _read_exact(fh, WRAP_LEN_PREFIX_BYTES)
    if len(wrap_len_bytes) != WRAP_LEN_PREFIX_BYTES:
        raise EncryptedBackupFormatError("Encrypted backup file is truncated in key-wrap length.")
    (wrap_len,) = struct.unpack(WRAP_LEN_STRUCT_FMT, wrap_len_bytes)
    if wrap_len != EXPECTED_WRAP_BOX_BYTES:
        raise EncryptedBackupFormatError("Encrypted backup key-wrap box has unexpected length.")

    wrapped_box = _read_exact(fh, wrap_len)
    if len(wrapped_box) != wrap_len:
        raise EncryptedBackupFormatError("Encrypted backup file is truncated in wrapped key box.")

    return {
        "header": header_obj,
        "header_envelope": header_envelope,
        "header_sha256": header_sha256,
        "file_salt": file_salt,
        "nonce_prefix": nonce_prefix,
        "wrap_nonce": wrap_nonce,
        "wrapped_box": wrapped_box,
    }


class EncryptedBackupService:
    """
    Service for creating, inspecting, verifying, and decrypting `.kdb.enc` v1 encrypted backups.
    Reuses Phase 2C-2 BackupService for SQLite verification, atomic replacement, and fsync.
    """

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        backup_dir: Optional[Union[str, Path]] = None,
        *,
        key_store: Optional[RecoveryKeyStore] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_plaintext_bytes: int = DEFAULT_MAX_PLAINTEXT_BYTES,
        max_compressed_bytes: int = DEFAULT_MAX_COMPRESSED_BYTES,
    ):
        self.db = db_manager or get_db_manager()
        self.backup_service = BackupService(self.db, backup_dir=backup_dir)
        self.key_store = key_store
        if not _is_strict_int(chunk_size) or chunk_size < MIN_CHUNK_SIZE or chunk_size > MAX_CHUNK_SIZE:
            raise ValueError(f"chunk_size must be between {MIN_CHUNK_SIZE} and {MAX_CHUNK_SIZE} bytes.")
        if not _is_strict_int(max_plaintext_bytes) or max_plaintext_bytes < 1024:
            raise ValueError("max_plaintext_bytes must be at least 1024 bytes.")
        if not _is_strict_int(max_compressed_bytes) or max_compressed_bytes < 1024:
            raise ValueError("max_compressed_bytes must be at least 1024 bytes.")
        self.chunk_size = int(chunk_size)
        self.max_plaintext_bytes = int(max_plaintext_bytes)
        self.max_compressed_bytes = int(max_compressed_bytes)
        self.max_archive_bytes = calculate_max_encrypted_archive_bytes(
            max_compressed_bytes=self.max_compressed_bytes,
            min_chunk_size=MIN_CHUNK_SIZE,
            max_header_bytes=MAX_HEADER_BYTES,
        )

    @staticmethod
    def _unique_encrypted_path(target_dir: Path, stem: str) -> Path:
        """Generate a unique non-colliding `.kdb.enc` path so existing encrypted archives are never overwritten."""
        candidate = target_dir / f"{stem}.kdb.enc"
        counter = 1
        while candidate.exists():
            candidate = target_dir / f"{stem}_{counter}.kdb.enc"
            counter += 1
        return candidate

    # Convenience wrappers required by Phase 2C-4B service interface
    @staticmethod
    def generate_recovery_key() -> str:
        return generate_recovery_key()

    @staticmethod
    def validate_recovery_key(recovery_key: str) -> bytes:
        return validate_recovery_key(recovery_key)

    @staticmethod
    def get_recovery_key_id(recovery_key: str) -> str:
        return get_recovery_key_id(recovery_key)

    def _resolve_recovery_key_bytes(
        self,
        recovery_key: Optional[str],
        *,
        key_id_hint: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        chosen = recovery_key
        if not chosen and self.key_store is not None:
            if not self.key_store.is_available():
                raise RecoveryKeyStoreUnavailableError(
                    f"OS-protected key storage ({self.key_store.backend_name}) is unavailable. "
                    "Enter your Recovery Key manually from your Recovery Card."
                )
            if key_id_hint is not None:
                chosen = self.key_store.get_key(key_id=key_id_hint)
                if not chosen:
                    raise InvalidRecoveryKeyError(
                        f"Recovery Key for Key ID '{key_id_hint}' is not stored in this computer's secure key store. "
                        "Enter the matching Recovery Key manually from your Recovery Card or import it first."
                    )
            else:
                chosen = self.key_store.get_key()
                if not chosen:
                    raise InvalidRecoveryKeyError(
                        "No active Recovery Key is configured in OS-protected key storage. "
                        "Create or import a Recovery Key first."
                    )
        if not chosen:
            raise InvalidRecoveryKeyError("A valid Recovery Key is required.")
        raw_key = validate_recovery_key(chosen)
        key_id = get_recovery_key_id(chosen)
        return raw_key, key_id

    def resolve_safe_encrypted_backup_path(
        self,
        filename: str,
        backup_dir: Optional[Union[str, Path]] = None,
    ) -> Path:
        """Safely resolve a `.kdb.enc` filename inside BACKUP_DIR while blocking traversal."""
        raw = str(filename or "").strip()
        if not raw:
            raise ValueError("Encrypted backup filename is required.")
        if any(sep in raw for sep in ("/", "\\", "\x00")) or ".." in raw or raw.startswith("."):
            raise ValueError("Invalid encrypted backup filename.")
        if Path(raw).name != raw or not raw.lower().endswith(".kdb.enc"):
            raise ValueError("Encrypted backup filename must be a plain .kdb.enc file.")
        target_dir = self.backup_service.get_backup_dir(backup_dir).resolve()
        candidate = (target_dir / raw).resolve()
        if candidate.parent != target_dir:
            raise ValueError("Invalid encrypted backup filename: escapes backup directory.")
        return candidate

    def inspect_encrypted_backup(
        self,
        encrypted_file_path: Union[str, Path],
        *,
        recovery_key: Optional[str] = None,
        use_key_store: bool = False,
        verify_sqlite: bool = True,
        max_plaintext_bytes: Optional[int] = None,
        max_compressed_bytes: Optional[int] = None,
        max_archive_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Inspect a `.kdb.enc` archive.
        - Without `recovery_key` (and `use_key_store=False`): validates format framing and returns
          unauthenticated non-sensitive header metadata (`metadata_authenticated=False`).
        - With `recovery_key`, OR when `use_key_store=True` and the archive's `key_id` is stored
          in `self.key_store`: authenticates DEK + every encrypted chunk, decompresses into a
          restricted temp directory, verifies plaintext SHA-256 + full SQLite integrity/FK/schema,
          and removes the temporary directory cleanly in `finally:`.
        """
        enc_path = Path(encrypted_file_path)
        if not enc_path.exists() or not enc_path.is_file():
            raise FileNotFoundError("Encrypted backup file does not exist.")

        comp_cap = self.max_compressed_bytes if max_compressed_bytes is None else int(max_compressed_bytes)
        archive_cap = (
            calculate_max_encrypted_archive_bytes(max_compressed_bytes=comp_cap)
            if max_archive_bytes is None
            else int(max_archive_bytes)
        )
        st = enc_path.stat()
        if st.st_size > archive_cap:
            raise EncryptedBackupError(
                "Encrypted backup archive exceeds maximum allowed size of "
                f"{BackupService._format_size_display(archive_cap)} ({archive_cap:,} bytes)."
            )

        with open(enc_path, "rb") as fh:
            parsed = _parse_and_validate_header(fh)

        hdr = parsed["header"]
        result: Dict[str, Any] = {
            "filename": enc_path.name,
            "size_bytes": int(st.st_size),
            "size_display": BackupService._format_size_display(st.st_size),
            "format_version": hdr["format_version"],
            "cipher": hdr["cipher"],
            "kdf": hdr["kdf"],
            "compression": hdr["compression"],
            "key_id": hdr["key_id"],
            "chunk_size": hdr["chunk_size"],
            "schema_version": hdr["schema_version"],
            "created_at_utc": hdr["created_at_utc"],
            "header_valid": True,
            "authenticated": False,
            "metadata_authenticated": False,
            "key_in_os_store": False,
            "key_source": None,
            "requires_manual_recovery_key": False,
        }

        effective_rk = recovery_key
        key_source: Optional[str] = "manual" if recovery_key is not None else None
        if effective_rk is None and use_key_store and self.key_store is not None:
            try:
                if self.key_store.is_available():
                    stored_match = self.key_store.get_key(key_id=hdr["key_id"])
                    if stored_match:
                        effective_rk = stored_match
                        key_source = "os_secure_store"
                        result["key_in_os_store"] = True
                    else:
                        result["requires_manual_recovery_key"] = True
                        result["store_status_message"] = (
                            f"No Recovery Key for Key ID '{hdr['key_id']}' is stored on this computer. "
                            "Enter the Recovery Key manually from your Recovery Card to authenticate this archive."
                        )
                else:
                    result["requires_manual_recovery_key"] = True
                    result["store_status_message"] = (
                        f"OS-protected key storage ({self.key_store.backend_name}) is unavailable. "
                        "Enter the Recovery Key manually from your Recovery Card to authenticate this archive."
                    )
            except RecoveryKeyStoreError as exc:
                result["requires_manual_recovery_key"] = True
                result["store_status_message"] = str(exc)

        if effective_rk is None:
            return result

        # Full authenticated inspection into restricted temporary directory
        temp_dir = _create_restricted_temp_dir(prefix="kdb_enc_inspect_")
        try:
            decrypted_db = temp_dir / "verified_inspect.db"
            dec_meta = self.decrypt_backup(
                enc_path,
                decrypted_db,
                recovery_key=effective_rk,
                verify_sqlite=verify_sqlite,
                max_plaintext_bytes=max_plaintext_bytes,
                max_compressed_bytes=comp_cap,
                max_archive_bytes=archive_cap,
            )
            result.update(
                {
                    "authenticated": True,
                    "metadata_authenticated": True,
                    "key_source": key_source,
                    "requires_manual_recovery_key": False,
                    "plaintext_size_bytes": dec_meta["plaintext_size_bytes"],
                    "plaintext_sha256": dec_meta["plaintext_sha256"],
                    "chunk_count": dec_meta["chunk_count"],
                    "integrity_ok": dec_meta.get("integrity_ok", False),
                    "foreign_key_ok": dec_meta.get("foreign_key_ok", False),
                    "foreign_key_violation_count": dec_meta.get("foreign_key_violation_count", 0),
                    "has_required_tables": dec_meta.get("has_required_tables", False),
                    "table_count": dec_meta.get("table_count", 0),
                    "shop_name": dec_meta.get("shop_name"),
                    "valid_for_restore": dec_meta.get("valid_for_restore", False),
                }
            )
            return result
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def encrypt_backup(
        self,
        source_backup_path: Union[str, Path],
        output_path: Optional[Union[str, Path]] = None,
        *,
        recovery_key: Optional[str] = None,
        chunk_size: Optional[int] = None,
        created_at_utc: Optional[str] = None,
        verify_roundtrip: bool = True,
    ) -> Dict[str, Any]:
        """
        Compress and encrypt an existing verified SQLite `.db` backup into a `.kdb.enc` v1 archive.

        Safety guarantees:
        - Rejects the live database directly; input must be a separate verified `.db` backup.
        - Streams compression + authenticated chunk encryption with bounded memory.
        - Generates fresh DEK, file_salt, nonce_prefix, and wrap_nonce every call.
        - Self-verifies by decrypting the temporary output into a restricted temp dir before
          flushing (`os.fsync`) and atomically promoting to `.kdb.enc`.
        - Preserves the original `.db` backup completely untouched.
        """
        AESGCM, _, _, _ = _require_cryptography()

        src_path = Path(source_backup_path)
        if not src_path.exists() or not src_path.is_file():
            raise FileNotFoundError("Source backup file does not exist.")

        # Never encrypt the live SQLite database directly
        if self.db.db_path != ":memory:":
            live_db = Path(self.db.db_path)
            try:
                if src_path.resolve() == live_db.resolve():
                    raise ValueError(
                        "Cannot encrypt the live database directly. Create and verify a local backup first."
                    )
            except OSError:
                pass

        eff_chunk_size = self.chunk_size if chunk_size is None else int(chunk_size)
        if not _is_strict_int(eff_chunk_size) or eff_chunk_size < MIN_CHUNK_SIZE or eff_chunk_size > MAX_CHUNK_SIZE:
            raise ValueError(f"chunk_size must be between {MIN_CHUNK_SIZE} and {MAX_CHUNK_SIZE} bytes.")

        raw_rk, key_id = self._resolve_recovery_key_bytes(recovery_key)

        with pinned_backup_file(src_path):
            # 1. Verify source `.db` backup (integrity + foreign keys + ERP schema)
            inspection = self.backup_service.inspect_backup(src_path)
            if not inspection["integrity_ok"]:
                raise ValueError("Source backup failed PRAGMA integrity_check and cannot be encrypted.")
            if not inspection["foreign_key_ok"]:
                raise ValueError("Source backup contains foreign key violations and cannot be encrypted.")
            if not inspection["has_required_tables"]:
                raise ValueError("Source backup is missing required ERP tables and cannot be encrypted.")
            schema_version = inspection.get("schema_version")
            if not _is_strict_int(schema_version) or schema_version < 1:
                raise ValueError("Source backup has invalid or missing schema_version.")

            src_size = int(src_path.stat().st_size)
            if src_size <= 0 or src_size > self.max_plaintext_bytes:
                raise EncryptedBackupError("Source backup size is out of allowed bounds for encryption.")

            plaintext_sha256_hex = BackupService.compute_file_sha256(src_path)
            plaintext_sha256_bytes = bytes.fromhex(plaintext_sha256_hex)

            # Determine final `.kdb.enc` destination (NEVER silently overwrite an existing archive)
            if output_path is not None:
                final_path = Path(output_path)
                if final_path.exists():
                    raise FileExistsError(
                        "Target encrypted backup archive already exists and will not be overwritten."
                    )
            else:
                stem = src_path.name[:-3] if src_path.name.lower().endswith(".db") else src_path.name
                final_path = self._unique_encrypted_path(src_path.parent, stem)
            final_path.parent.mkdir(parents=True, exist_ok=True)

            # Fresh cryptographic material per backup (NEVER reused)
            dek = os.urandom(DEK_BYTES)
            file_salt = os.urandom(FILE_SALT_BYTES)
            nonce_prefix = os.urandom(NONCE_PREFIX_BYTES)
            wrap_nonce = os.urandom(WRAP_NONCE_BYTES)
            ts_utc = (
                created_at_utc
                if created_at_utc is not None
                else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            if not _UTC_ISO_RE.match(ts_utc):
                raise ValueError("created_at_utc must follow YYYY-MM-DDTHH:MM:SSZ format.")

            # Canonical non-PII header JSON
            header_obj: Dict[str, Any] = {
                "chunk_size": eff_chunk_size,
                "cipher": KDB_ENC_CIPHER,
                "compression": KDB_ENC_COMPRESSION,
                "created_at_utc": ts_utc,
                "file_salt_b64": base64.b64encode(file_salt).decode("ascii"),
                "format_version": KDB_ENC_FORMAT_VERSION,
                "kdf": KDB_ENC_KDF,
                "key_id": key_id,
                "nonce_prefix_b64": base64.b64encode(nonce_prefix).decode("ascii"),
                "schema_version": int(schema_version),
            }
            header_bytes = json.dumps(
                header_obj,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
            if len(header_bytes) > MAX_HEADER_BYTES:
                raise EncryptedBackupFormatError("Serialized header exceeds maximum allowed size.")

            header_envelope = KDB_ENC_MAGIC + struct.pack(HEADER_LEN_STRUCT_FMT, len(header_bytes)) + header_bytes
            header_sha256 = hashlib.sha256(header_envelope).digest()

            # Derive KEK via HKDF-SHA256 & wrap DEK + authenticated plaintext metadata via AES-256-GCM
            kek = _derive_kek(raw_rk, file_salt)
            wrap_plaintext = dek + plaintext_sha256_bytes + struct.pack(WRAP_SIZE_STRUCT_FMT, src_size)
            wrapped_box = AESGCM(kek).encrypt(wrap_nonce, wrap_plaintext, header_envelope)
            if len(wrapped_box) != EXPECTED_WRAP_BOX_BYTES:
                raise EncryptedBackupError("Unexpected wrapped key box size.")

            # Stage output to restricted temp file (0o600)
            fd, tmp_raw = tempfile.mkstemp(
                prefix=f".{final_path.name}.",
                suffix=f".{os.getpid()}.tmp",
                dir=str(final_path.parent),
            )
            tmp_path = Path(tmp_raw)
            _restrict_path_permissions(tmp_path, is_dir=False)

            chunk_count = 0
            total_compressed_bytes = 0
            try:
                data_aead = AESGCM(dek)
                compressor = zlib.compressobj(level=6)

                def _write_chunk(out_fh: BinaryIO, compressed_slice: bytes, *, is_final: int, idx: int) -> None:
                    nonlocal total_compressed_bytes
                    total_compressed_bytes += len(compressed_slice)
                    if total_compressed_bytes > self.max_compressed_bytes:
                        raise EncryptedBackupError(
                            "Compressed backup stream exceeded maximum allowed compressed size."
                        )
                    nonce = _chunk_nonce(nonce_prefix, idx)
                    record_len = len(compressed_slice) + GCM_TAG_BYTES
                    aad = _chunk_aad(header_sha256, idx, is_final, record_len)
                    ct = data_aead.encrypt(nonce, compressed_slice, aad)
                    out_fh.write(struct.pack(CHUNK_RECORD_PREFIX_STRUCT_FMT, len(ct), is_final))
                    out_fh.write(ct)

                with os.fdopen(fd, "wb") as out_fh:
                    out_fh.write(header_envelope)
                    out_fh.write(wrap_nonce)
                    out_fh.write(struct.pack(WRAP_LEN_STRUCT_FMT, len(wrapped_box)))
                    out_fh.write(wrapped_box)

                    comp_buf = bytearray()
                    pending_chunk: Optional[bytes] = None

                    with open(src_path, "rb") as in_fh:
                        while True:
                            raw_block = in_fh.read(65536)
                            if not raw_block:
                                break
                            comp_piece = compressor.compress(raw_block)
                            if comp_piece:
                                comp_buf.extend(comp_piece)
                            while len(comp_buf) >= eff_chunk_size:
                                next_chunk = bytes(comp_buf[:eff_chunk_size])
                                del comp_buf[:eff_chunk_size]
                                if pending_chunk is not None:
                                    _write_chunk(out_fh, pending_chunk, is_final=0, idx=chunk_count)
                                    chunk_count += 1
                                pending_chunk = next_chunk

                    tail_comp = compressor.flush()
                    if tail_comp:
                        comp_buf.extend(tail_comp)

                    while len(comp_buf) >= eff_chunk_size:
                        next_chunk = bytes(comp_buf[:eff_chunk_size])
                        del comp_buf[:eff_chunk_size]
                        if pending_chunk is not None:
                            _write_chunk(out_fh, pending_chunk, is_final=0, idx=chunk_count)
                            chunk_count += 1
                        pending_chunk = next_chunk

                    if len(comp_buf) > 0:
                        if pending_chunk is not None:
                            _write_chunk(out_fh, pending_chunk, is_final=0, idx=chunk_count)
                            chunk_count += 1
                        _write_chunk(out_fh, bytes(comp_buf), is_final=1, idx=chunk_count)
                        chunk_count += 1
                    elif pending_chunk is not None:
                        _write_chunk(out_fh, pending_chunk, is_final=1, idx=chunk_count)
                        chunk_count += 1
                    else:
                        # Unambiguous final chunk even for empty compressed payload
                        _write_chunk(out_fh, b"", is_final=1, idx=chunk_count)
                        chunk_count += 1

                    out_fh.flush()
                    os.fsync(out_fh.fileno())

                if tmp_path.stat().st_size > self.max_archive_bytes:
                    raise EncryptedBackupError(
                        "Generated encrypted backup exceeded maximum allowed archive size."
                    )

                # Round-trip self-verification into isolated restricted temp directory before atomic promote
                if verify_roundtrip:
                    verify_dir = _create_restricted_temp_dir(prefix="kdb_enc_verify_")
                    try:
                        verified_db = verify_dir / "roundtrip_verify.db"
                        verify_rk = (
                            recovery_key
                            if recovery_key is not None
                            else (self.key_store.get_key(key_id=key_id) if self.key_store else None)
                        )
                        dec_info = self.decrypt_backup(
                            tmp_path,
                            verified_db,
                            recovery_key=verify_rk,
                            verify_sqlite=True,
                        )
                        if not hmac.compare_digest(dec_info["plaintext_sha256"], plaintext_sha256_hex):
                            raise EncryptedBackupError("Post-encryption round-trip SHA-256 verification failed.")
                        if not dec_info.get("valid_for_restore"):
                            raise EncryptedBackupError("Post-encryption round-trip SQLite verification failed.")
                    finally:
                        shutil.rmtree(verify_dir, ignore_errors=True)

                BackupService._fsync_file(tmp_path)
                if final_path.exists():
                    raise FileExistsError(
                        "Target encrypted backup archive already exists and will not be overwritten."
                    )
                BackupService._replace_with_retry(tmp_path, final_path)
                BackupService._fsync_directory(final_path.parent)
                _restrict_path_permissions(final_path, is_dir=False)

                enc_size = int(final_path.stat().st_size)
                enc_sha256 = BackupService.compute_file_sha256(final_path)
                return {
                    "file_path": str(final_path),
                    "filename": final_path.name,
                    "size_bytes": enc_size,
                    "size_display": BackupService._format_size_display(enc_size),
                    "encrypted_sha256": enc_sha256,
                    "plaintext_size_bytes": src_size,
                    "plaintext_sha256": plaintext_sha256_hex,
                    "key_id": key_id,
                    "format_version": KDB_ENC_FORMAT_VERSION,
                    "cipher": KDB_ENC_CIPHER,
                    "compression": KDB_ENC_COMPRESSION,
                    "chunk_size": eff_chunk_size,
                    "chunk_count": chunk_count,
                    "schema_version": int(schema_version),
                    "created_at_utc": ts_utc,
                }
            except Exception:
                # Remove ONLY the incomplete temporary output; source .db backup remains untouched
                try:
                    BackupService._unlink_with_retry(tmp_path, ignore_missing=True)
                except OSError:
                    pass
                raise

    def decrypt_backup(
        self,
        encrypted_file_path: Union[str, Path],
        output_db_path: Union[str, Path],
        *,
        recovery_key: Optional[str] = None,
        verify_sqlite: bool = True,
        max_plaintext_bytes: Optional[int] = None,
        max_compressed_bytes: Optional[int] = None,
        max_archive_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Authenticate, decrypt, decompress, and verify a `.kdb.enc` v1 archive into `output_db_path`.

        Safety guarantees:
        - Never overwrites the live SQLite database directly.
        - Validates Recovery Key checksum, header format, DEK wrap AEAD tag, every chunk AEAD tag,
          chunk ordering, final-chunk flag, zero trailing bytes, decompression size cap, exact
          decompressed size, plaintext SHA-256, and full SQLite integrity + foreign keys + schema.
        - Cleans up incomplete output files on any error.
        """
        AESGCM, _, _, InvalidTag = _require_cryptography()

        enc_path = Path(encrypted_file_path)
        if not enc_path.exists() or not enc_path.is_file():
            raise FileNotFoundError("Encrypted backup file does not exist.")

        out_path = Path(output_db_path)
        if self.db.db_path != ":memory:":
            live_db = Path(self.db.db_path)
            try:
                if out_path.resolve() == live_db.resolve():
                    raise ValueError("Cannot decrypt directly onto the live database path.")
            except OSError:
                pass

        out_path.parent.mkdir(parents=True, exist_ok=True)
        size_cap = self.max_plaintext_bytes if max_plaintext_bytes is None else int(max_plaintext_bytes)
        comp_cap = self.max_compressed_bytes if max_compressed_bytes is None else int(max_compressed_bytes)
        if size_cap < 1024:
            raise ValueError("max_plaintext_bytes must be at least 1024 bytes.")
        if comp_cap < 1024:
            raise ValueError("max_compressed_bytes must be at least 1024 bytes.")
        archive_cap = (
            calculate_max_encrypted_archive_bytes(max_compressed_bytes=comp_cap)
            if max_archive_bytes is None
            else int(max_archive_bytes)
        )
        if enc_path.stat().st_size > archive_cap:
            raise EncryptedBackupError(
                "Encrypted backup archive exceeds maximum allowed size of "
                f"{BackupService._format_size_display(archive_cap)} ({archive_cap:,} bytes)."
            )

        with open(enc_path, "rb") as in_fh:
            parsed = _parse_and_validate_header(in_fh)
            hdr = parsed["header"]
            header_envelope = parsed["header_envelope"]
            header_sha256 = parsed["header_sha256"]
            file_salt = parsed["file_salt"]
            nonce_prefix = parsed["nonce_prefix"]
            wrap_nonce = parsed["wrap_nonce"]
            wrapped_box = parsed["wrapped_box"]
            chunk_size = int(hdr["chunk_size"])

            raw_rk, _ = self._resolve_recovery_key_bytes(recovery_key, key_id_hint=hdr["key_id"])
            kek = _derive_kek(raw_rk, file_salt)

            try:
                wrap_plaintext = AESGCM(kek).decrypt(wrap_nonce, wrapped_box, header_envelope)
            except InvalidTag:
                raise EncryptedBackupAuthError(
                    "Authentication failed: incorrect Recovery Key or modified backup header."
                ) from None

            if len(wrap_plaintext) != WRAP_PLAINTEXT_BYTES:
                raise EncryptedBackupFormatError("Decrypted key-wrap metadata has unexpected length.")

            dek = wrap_plaintext[:DEK_BYTES]
            expected_sha256_bytes = wrap_plaintext[DEK_BYTES : DEK_BYTES + 32]
            (expected_plaintext_size,) = struct.unpack(WRAP_SIZE_STRUCT_FMT, wrap_plaintext[DEK_BYTES + 32 :])

            if expected_plaintext_size > size_cap:
                raise EncryptedBackupError("Archive plaintext size exceeds allowed decompression limit.")

            fd, tmp_raw = tempfile.mkstemp(
                prefix=f".{out_path.name}.dec.",
                suffix=f".{os.getpid()}.tmp",
                dir=str(out_path.parent),
            )
            tmp_out = Path(tmp_raw)
            _restrict_path_permissions(tmp_out, is_dir=False)

            data_aead = AESGCM(dek)
            decompressor = zlib.decompressobj()
            plaintext_hasher = hashlib.sha256()
            total_plaintext_written = 0
            total_compressed_read = 0
            chunk_index = 0
            saw_final = False
            max_record_len = chunk_size + GCM_TAG_BYTES

            try:
                with os.fdopen(fd, "wb") as out_fh:
                    while True:
                        prefix_bytes = _read_exact(in_fh, CHUNK_RECORD_PREFIX_BYTES)
                        if not prefix_bytes:
                            if not saw_final:
                                raise EncryptedBackupFormatError(
                                    "Encrypted backup is truncated or missing its final authenticated chunk."
                                )
                            break
                        if len(prefix_bytes) != CHUNK_RECORD_PREFIX_BYTES:
                            raise EncryptedBackupFormatError("Encrypted backup is truncated in chunk framing.")
                        if saw_final:
                            raise EncryptedBackupFormatError(
                                "Encrypted backup contains unexpected trailing data after final chunk."
                            )

                        record_len, is_final = struct.unpack(CHUNK_RECORD_PREFIX_STRUCT_FMT, prefix_bytes)
                        if is_final not in (0, 1):
                            raise EncryptedBackupFormatError("Encrypted backup contains invalid chunk finality flag.")
                        if record_len < GCM_TAG_BYTES or record_len > max_record_len:
                            raise EncryptedBackupFormatError("Encrypted backup chunk record length is out of bounds.")
                        if chunk_index > MAX_CHUNK_INDEX:
                            raise EncryptedBackupFormatError("Encrypted backup exceeded 32-bit chunk index limit.")

                        ciphertext = _read_exact(in_fh, record_len)
                        if len(ciphertext) != record_len:
                            raise EncryptedBackupFormatError("Encrypted backup is truncated inside chunk payload.")
                        if in_fh.tell() > archive_cap:
                            raise EncryptedBackupError(
                                "Encrypted backup archive exceeds maximum allowed size of "
                                f"{BackupService._format_size_display(archive_cap)} ({archive_cap:,} bytes)."
                            )

                        nonce = _chunk_nonce(nonce_prefix, chunk_index)
                        aad = _chunk_aad(header_sha256, chunk_index, is_final, record_len)
                        try:
                            compressed_chunk = data_aead.decrypt(nonce, ciphertext, aad)
                        except InvalidTag:
                            raise EncryptedBackupAuthError(
                                "Encrypted backup integrity check failed: corrupted, modified, or reordered chunk."
                            ) from None

                        total_compressed_read += len(compressed_chunk)
                        if total_compressed_read > comp_cap:
                            raise EncryptedBackupError(
                                "Encrypted backup exceeded maximum allowed compressed size."
                            )

                        # Stream-decompress with strict upper bound against decompression bombs
                        remaining_allowed = min(size_cap, expected_plaintext_size) - total_plaintext_written
                        try:
                            decompressed = decompressor.decompress(compressed_chunk, remaining_allowed + 1)
                        except zlib.error:
                            raise EncryptedBackupFormatError("Encrypted backup compressed stream is corrupted.") from None

                        if decompressor.unconsumed_tail or (total_plaintext_written + len(decompressed) > min(size_cap, expected_plaintext_size)):
                            raise EncryptedBackupError(
                                "Encrypted backup exceeded maximum allowed decompressed size."
                            )

                        if decompressed:
                            out_fh.write(decompressed)
                            plaintext_hasher.update(decompressed)
                            total_plaintext_written += len(decompressed)

                        chunk_index += 1
                        if is_final == 1:
                            saw_final = True
                            # Verify ZERO trailing bytes remain after the authenticated final chunk
                            if in_fh.read(1):
                                raise EncryptedBackupFormatError(
                                    "Encrypted backup contains unexpected trailing data after final chunk."
                                )
                            break

                    if not saw_final or chunk_index == 0:
                        raise EncryptedBackupFormatError("Encrypted backup is missing a final authenticated chunk.")

                    try:
                        remaining_allowed = min(size_cap, expected_plaintext_size) - total_plaintext_written
                        flushed = decompressor.flush(remaining_allowed + 1)
                    except zlib.error:
                        raise EncryptedBackupFormatError("Encrypted backup compressed stream failed final flush.") from None

                    if total_plaintext_written + len(flushed) > min(size_cap, expected_plaintext_size):
                        raise EncryptedBackupError("Encrypted backup exceeded maximum allowed decompressed size.")
                    if flushed:
                        out_fh.write(flushed)
                        plaintext_hasher.update(flushed)
                        total_plaintext_written += len(flushed)

                    if not decompressor.eof:
                        raise EncryptedBackupFormatError("Compressed backup stream ended unexpectedly.")

                    out_fh.flush()
                    os.fsync(out_fh.fileno())

                if total_plaintext_written != expected_plaintext_size:
                    raise EncryptedBackupFormatError(
                        "Decrypted backup size does not match authenticated header metadata."
                    )

                actual_sha256_bytes = plaintext_hasher.digest()
                if not hmac.compare_digest(actual_sha256_bytes, expected_sha256_bytes):
                    raise EncryptedBackupAuthError(
                        "Decrypted backup SHA-256 does not match authenticated plaintext checksum."
                    )

                sqlite_info: Dict[str, Any] = {}
                if verify_sqlite:
                    inspection = self.backup_service.inspect_backup(tmp_out)
                    if not inspection["integrity_ok"]:
                        raise EncryptedBackupError("Decrypted SQLite backup failed PRAGMA integrity_check.")
                    if not inspection["foreign_key_ok"]:
                        raise EncryptedBackupError("Decrypted SQLite backup contains foreign key violations.")
                    if not inspection["has_required_tables"]:
                        raise EncryptedBackupError("Decrypted SQLite backup is missing required ERP tables.")
                    sqlite_info = {
                        "integrity_ok": True,
                        "foreign_key_ok": True,
                        "foreign_key_violation_count": 0,
                        "has_required_tables": True,
                        "table_count": inspection["table_count"],
                        "schema_version": inspection["schema_version"],
                        "shop_name": inspection["shop_name"],
                        "valid_for_restore": True,
                    }

                BackupService._fsync_file(tmp_out)
                BackupService._replace_with_retry(tmp_out, out_path)
                BackupService._fsync_directory(out_path.parent)
                _restrict_path_permissions(out_path, is_dir=False)

                return {
                    "file_path": str(out_path),
                    "plaintext_size_bytes": total_plaintext_written,
                    "plaintext_sha256": actual_sha256_bytes.hex(),
                    "key_id": hdr["key_id"],
                    "format_version": hdr["format_version"],
                    "chunk_count": chunk_index,
                    "schema_version": hdr["schema_version"],
                    "created_at_utc": hdr["created_at_utc"],
                    **sqlite_info,
                }
            except Exception:
                try:
                    BackupService._unlink_with_retry(tmp_out, ignore_missing=True)
                except OSError:
                    pass
                raise
