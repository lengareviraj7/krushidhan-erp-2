"""
Provider-Neutral Encrypted Off-Site Backup Service via Cloud-Synchronized Folders (Phase 2C-5A).

Architecture:
- `OffsiteBackupProvider` ABC defines a provider-neutral publishing and discovery interface.
- `SyncFolderOffsiteBackupProvider` publishes verified `.kdb.enc` archives to an administrator-selected
  local directory managed by any desktop cloud sync client (OneDrive, Google Drive, Dropbox, iCloud Drive, etc.)
  without direct cloud APIs, OAuth, or provider credentials.

Strict security & data-integrity guarantees:
- NEVER copies plaintext `.db` files, WAL/SHM files, or live SQLite databases.
- NEVER encrypts inside the synchronized folder; encrypts and round-trip verifies in an isolated
  restricted staging directory outside the sync folder first.
- NEVER overwrites existing archives or provider conflict files (always picks a unique `.kdb.enc` name).
- Copies via hidden `.part` temp file + fsync + byte-count & SHA-256 verification before atomic promotion,
  then re-verifies the final destination file's exact byte count and SHA-256.
- NEVER silently redirects to another folder or guesses user cloud folder paths.
- NEVER automatically deletes archives in the synchronized folder (even when disabled or during local retention).
- NEVER claims cloud upload succeeded: explicitly reports `CLOUD_SYNC_NOT_CONFIRMED` with honest disclaimer.
- Fails closed when OS Recovery Key store is unavailable (never falls back to plaintext or in-memory production key).
"""
from __future__ import annotations

import abc
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from src.db.connection import DatabaseManager, get_db_manager
from src.desktop_runtime import redact_sensitive_text
from src.services.backup_service import BackupService, pinned_backup_file
from src.services.encrypted_backup_service import (
    EncryptedBackupError,
    EncryptedBackupService,
    KDB_ENC_MAGIC,
    MAX_ENCRYPTED_ARCHIVE_BYTES,
    _create_restricted_temp_dir,
)
from src.services.os_recovery_key_store import get_recovery_key_store
from src.services.recovery_key import (
    InvalidRecoveryKeyError,
    RecoveryKeyStore,
    RecoveryKeyStoreError,
    RecoveryKeyStoreUnavailableError,
    utc_now_iso,
)

logger = logging.getLogger("agri_erp.offsite_backup")


class OffsiteStatus:
    """Explicit, honest status vocabulary for off-site folder synchronization."""

    DISABLED = "DISABLED"
    IDLE = "IDLE"
    LOCAL_BACKUP_VERIFIED = "LOCAL_BACKUP_VERIFIED"
    ENCRYPTION_COMPLETED = "ENCRYPTION_COMPLETED"
    COPIED_TO_SYNC_FOLDER = "COPIED_TO_SYNC_FOLDER"
    DESTINATION_CHECKSUM_VERIFIED = "DESTINATION_CHECKSUM_VERIFIED"
    CLOUD_SYNC_NOT_CONFIRMED = "CLOUD_SYNC_NOT_CONFIRMED"
    DESTINATION_UNAVAILABLE = "DESTINATION_UNAVAILABLE"
    RECOVERY_KEY_UNAVAILABLE = "RECOVERY_KEY_UNAVAILABLE"
    OPERATION_FAILED = "OPERATION_FAILED"


CLOUD_SYNC_DISCLAIMER = (
    "Encrypted archive copied to your selected synchronization folder. "
    "Actual cloud upload completion depends on your synchronization provider "
    "and has not been independently confirmed."
)

OFFSITE_CONFIG_FILENAME = ".offsite_backup_config.json"
OFFSITE_CONFIG_VERSION = 1
MAX_RETRY_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = (300, 900, 1800, 3600, 7200)  # 5m, 15m, 30m, 1h, 2h
COPY_BUFFER_BYTES = 256 * 1024  # 256 KiB streaming copy buffer
_SAFE_ARCHIVE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.kdb\.enc$", re.IGNORECASE)


class OffsiteBackupError(RuntimeError):
    """Base error for off-site backup operations (always sanitized)."""


class OffsiteDestinationUnavailableError(OffsiteBackupError):
    """Raised when the configured synchronized folder is missing, disconnected, or unwritable."""


def _compute_sha256_and_size(path: Path) -> Tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(COPY_BUFFER_BYTES)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_write_json(target: Path, payload: Dict[str, Any]) -> None:
    text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True)
    # Defense-in-depth: never persist Recovery Keys into off-site config/metadata
    if "KDHN-" in text.upper() or '"recovery_key"' in text.lower():
        raise OffsiteBackupError("Refused to persist secret material into off-site backup configuration.")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_raw = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=f".{os.getpid()}.tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_raw)
    try:
        if os.name != "nt":
            try:
                os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp_path), str(target))
        _fsync_dir(target.parent)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _is_absolute_cross_platform_path(raw: str) -> bool:
    """Validate absolute paths on both POSIX and Windows (drive letter or UNC)."""
    p = Path(raw)
    if p.is_absolute():
        return True
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith("\\\\"):
        return True
    return False


class OffsiteBackupProvider(abc.ABC):
    """Provider-neutral interface for encrypted off-site backup publication and discovery."""

    @property
    @abc.abstractmethod
    def provider_type(self) -> str:
        """Stable non-secret identifier for this off-site provider."""

    @abc.abstractmethod
    def validate_destination(self, destination: Optional[str] = None) -> Dict[str, Any]:
        """Revalidate destination existence, directory type, and writability without redirecting."""

    @abc.abstractmethod
    def publish_encrypted_archive(
        self,
        local_encrypted_archive_path: Union[str, Path],
        *,
        expected_sha256: Optional[str] = None,
        preferred_filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Publish a verified `.kdb.enc` archive safely without overwriting existing files."""

    @abc.abstractmethod
    def list_published_archives(self) -> List[Dict[str, Any]]:
        """List `.kdb.enc` archives currently present in the destination (read-only, never deletes)."""

    @abc.abstractmethod
    def resolve_archive_path(self, filename: str) -> Path:
        """Safely resolve a `.kdb.enc` filename inside the destination, blocking path traversal."""


class SyncFolderOffsiteBackupProvider(OffsiteBackupProvider):
    """
    Provider-neutral local synchronized-folder provider (compatible with OneDrive, Google Drive,
    Dropbox, iCloud Drive, or any external/network synchronized folder).
    """

    def __init__(
        self,
        destination_folder: Optional[Union[str, Path]],
        *,
        forbidden_dirs: Optional[List[Path]] = None,
    ) -> None:
        self._raw_folder = str(destination_folder).strip() if destination_folder is not None else ""
        self._forbidden_dirs = [Path(d).resolve() for d in (forbidden_dirs or []) if d]
        self._publish_lock = threading.Lock()

    @property
    def provider_type(self) -> str:
        return "sync_folder"

    def _validate_path_obj(self, raw_folder: str) -> Path:
        cleaned = str(raw_folder or "").strip()
        if not cleaned or "\x00" in cleaned:
            raise OffsiteDestinationUnavailableError("No synchronized backup folder is configured.")
        if not _is_absolute_cross_platform_path(cleaned):
            raise OffsiteDestinationUnavailableError(
                "Synchronized backup folder must be an absolute directory path."
            )
        dest = Path(cleaned)
        if not dest.exists():
            raise OffsiteDestinationUnavailableError(
                "Selected synchronized folder does not exist or its drive is disconnected."
            )
        if not dest.is_dir():
            raise OffsiteDestinationUnavailableError(
                "Selected synchronized backup path is not a directory."
            )
        try:
            resolved = dest.resolve()
        except OSError:
            raise OffsiteDestinationUnavailableError(
                "Selected synchronized folder could not be resolved safely."
            ) from None

        for forb in self._forbidden_dirs:
            if resolved == forb:
                raise OffsiteDestinationUnavailableError(
                    "Selected synchronized folder cannot be the live database directory itself. Choose a dedicated backup sync folder."
                )

        # Verify genuine write + delete permission via tiny hidden probe file
        probe_path: Optional[Path] = None
        try:
            fd, probe_raw = tempfile.mkstemp(prefix=".kdb_probe_", suffix=".tmp", dir=str(resolved))
            probe_path = Path(probe_raw)
            os.close(fd)
        except OSError:
            raise OffsiteDestinationUnavailableError(
                "Selected synchronized folder is not writable. Check folder permissions or drive availability."
            ) from None
        finally:
            if probe_path is not None:
                try:
                    probe_path.unlink()
                except OSError:
                    pass
        return resolved

    def validate_destination(self, destination: Optional[str] = None) -> Dict[str, Any]:
        target_raw = self._raw_folder if destination is None else str(destination or "").strip()
        try:
            resolved = self._validate_path_obj(target_raw)
            free_bytes: Optional[int] = None
            try:
                free_bytes = int(shutil.disk_usage(str(resolved)).free)
            except OSError:
                free_bytes = None
            return {
                "available": True,
                "writable": True,
                "provider_type": self.provider_type,
                "destination_folder": str(resolved),
                "free_bytes": free_bytes,
                "free_display": BackupService._format_size_display(free_bytes) if free_bytes is not None else None,
                "message": "Synchronized folder is available and writable.",
            }
        except OffsiteDestinationUnavailableError as exc:
            return {
                "available": False,
                "writable": False,
                "provider_type": self.provider_type,
                "destination_folder": target_raw or None,
                "free_bytes": None,
                "free_display": None,
                "message": str(exc),
            }

    @staticmethod
    def _sanitize_archive_filename(filename: str) -> str:
        raw = str(filename or "").strip()
        base = Path(raw.replace("\\", "/")).name
        if not base or not base.lower().endswith(".kdb.enc"):
            raise ValueError("Only completed .kdb.enc encrypted backup archives may be published.")
        if any(sep in base for sep in ("/", "\\", "\x00")) or ".." in base or base.startswith("."):
            raise ValueError("Invalid encrypted backup archive filename.")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
        if not safe.lower().endswith(".kdb.enc"):
            raise ValueError("Invalid encrypted backup archive filename.")
        return safe

    @staticmethod
    def _unique_destination_path(dest_dir: Path, safe_filename: str) -> Path:
        """Select a unique non-colliding `.kdb.enc` path so existing archives are NEVER overwritten."""
        stem = safe_filename[: -len(".kdb.enc")]
        candidate = dest_dir / f"{stem}.kdb.enc"
        counter = 1
        while candidate.exists():
            candidate = dest_dir / f"{stem}_{counter}.kdb.enc"
            counter += 1
        return candidate

    def resolve_archive_path(self, filename: str) -> Path:
        resolved_dir = self._validate_path_obj(self._raw_folder)
        raw = str(filename or "").strip()
        if not raw or any(sep in raw for sep in ("/", "\\", "\x00")) or ".." in raw or raw.startswith("."):
            raise ValueError("Invalid encrypted backup filename.")
        if Path(raw).name != raw or not raw.lower().endswith(".kdb.enc"):
            raise ValueError("Only .kdb.enc encrypted backup files can be inspected or restored from the sync folder.")
        candidate = (resolved_dir / raw).resolve()
        if candidate.parent != resolved_dir:
            raise ValueError("Invalid archive path: escapes synchronized folder.")
        return candidate

    def publish_encrypted_archive(
        self,
        local_encrypted_archive_path: Union[str, Path],
        *,
        expected_sha256: Optional[str] = None,
        preferred_filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Safely copy a completed, verified `.kdb.enc` archive into the synchronized folder:
        1. Revalidates destination folder (no silent redirect).
        2. Validates source is a completed `.kdb.enc` file with `KDHNENC1` magic.
        3. Streams bytes into a hidden `.part` file + fsync + pre-promotion SHA-256 verification.
        4. Promotes atomically to a unique non-overwriting `.kdb.enc` name (never overwrites existing files).
        5. Re-verifies final destination file's exact byte count and SHA-256.
        6. Cleans up any `.part` file on error or interruption.
        """
        src_path = Path(local_encrypted_archive_path)
        if not src_path.exists() or not src_path.is_file():
            raise FileNotFoundError("Source encrypted archive does not exist.")
        if not src_path.name.lower().endswith(".kdb.enc"):
            raise ValueError(
                "Refused to publish non-.kdb.enc file. Plaintext SQLite .db files are never copied to synchronized folders."
            )

        with open(src_path, "rb") as magic_fh:
            magic = magic_fh.read(len(KDB_ENC_MAGIC))
        if magic != KDB_ENC_MAGIC:
            raise ValueError("Source archive does not have a valid KDHNENC1 encrypted container header.")

        src_sha256, src_size = _compute_sha256_and_size(src_path)
        if src_size <= 0 or src_size > MAX_ENCRYPTED_ARCHIVE_BYTES:
            raise OffsiteBackupError("Source encrypted archive size is outside permitted bounds.")
        if expected_sha256 and not hmac.compare_digest(src_sha256.lower(), expected_sha256.lower()):
            raise OffsiteBackupError("Source encrypted archive checksum mismatch before off-site copy.")

        dest_dir = self._validate_path_obj(self._raw_folder)
        safe_name = self._sanitize_archive_filename(preferred_filename or src_path.name)

        with self._publish_lock:
            part_path: Optional[Path] = None
            try:
                fd, part_raw = tempfile.mkstemp(
                    prefix=".kdb_publish_",
                    suffix=f".{os.getpid()}.part",
                    dir=str(dest_dir),
                )
                part_path = Path(part_raw)
                copy_hasher = hashlib.sha256()
                copied_bytes = 0
                with open(src_path, "rb") as in_fh, os.fdopen(fd, "wb") as out_fh:
                    while True:
                        buf = in_fh.read(COPY_BUFFER_BYTES)
                        if not buf:
                            break
                        out_fh.write(buf)
                        copy_hasher.update(buf)
                        copied_bytes += len(buf)
                    out_fh.flush()
                    os.fsync(out_fh.fileno())

                if copied_bytes != src_size or not hmac.compare_digest(copy_hasher.hexdigest(), src_sha256):
                    raise OffsiteBackupError("Destination copy verification failed before publish.")

                final_dest = self._unique_destination_path(dest_dir, safe_name)
                # Atomic non-overwriting publish: prefer hard-link + unlink on POSIX (fails with FileExistsError if race)
                promoted = False
                if os.name != "nt":
                    try:
                        os.link(str(part_path), str(final_dest))
                        part_path.unlink()
                        part_path = None
                        promoted = True
                    except FileExistsError:
                        final_dest = self._unique_destination_path(dest_dir, safe_name)
                        os.link(str(part_path), str(final_dest))
                        part_path.unlink()
                        part_path = None
                        promoted = True
                    except OSError:
                        # Filesystems without hard-link support (e.g. FAT32/exFAT/network mounts) fall back safely
                        promoted = False

                if not promoted:
                    if final_dest.exists():
                        final_dest = self._unique_destination_path(dest_dir, safe_name)
                    os.replace(str(part_path), str(final_dest))
                    part_path = None

                _fsync_dir(dest_dir)

                # Independent post-publish verification of destination file's exact byte count and SHA-256
                dest_sha256, dest_size = _compute_sha256_and_size(final_dest)
                if dest_size != src_size or not hmac.compare_digest(dest_sha256, src_sha256):
                    raise OffsiteBackupError(
                        "Post-publish checksum verification failed for copied encrypted archive."
                    )

                return {
                    "filename": final_dest.name,
                    "file_path": str(final_dest),
                    "destination_folder": str(dest_dir),
                    "size_bytes": dest_size,
                    "size_display": BackupService._format_size_display(dest_size),
                    "sha256": dest_sha256,
                    "checksum_verified": True,
                    "cloud_sync_confirmed": False,
                    "cloud_sync_disclaimer": CLOUD_SYNC_DISCLAIMER,
                }
            except Exception:
                if part_path is not None:
                    try:
                        if part_path.exists():
                            part_path.unlink()
                    except OSError:
                        pass
                raise

    def list_published_archives(self) -> List[Dict[str, Any]]:
        resolved_dir = self._validate_path_obj(self._raw_folder)
        enc_svc = EncryptedBackupService(get_db_manager())
        archives: List[Dict[str, Any]] = []
        try:
            entries = list(resolved_dir.iterdir())
        except OSError as exc:
            raise OffsiteDestinationUnavailableError(
                f"Unable to read synchronized backup folder: {exc.__class__.__name__}."
            ) from None

        for p in entries:
            if p.name.startswith(".") or not p.name.lower().endswith(".kdb.enc"):
                continue
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            item: Dict[str, Any] = {
                "filename": p.name,
                "size_bytes": int(st.st_size),
                "size_display": BackupService._format_size_display(st.st_size),
                "modified_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "mtime_epoch": float(st.st_mtime),
                "header_valid": False,
                "metadata_authenticated": False,
                "key_id": None,
                "created_at_utc": None,
                "schema_version": None,
            }
            try:
                hdr_info = enc_svc.inspect_encrypted_backup(p)
                item.update(
                    {
                        "header_valid": True,
                        "key_id": hdr_info.get("key_id"),
                        "created_at_utc": hdr_info.get("created_at_utc"),
                        "schema_version": hdr_info.get("schema_version"),
                        "cipher": hdr_info.get("cipher"),
                    }
                )
            except Exception:
                item["header_valid"] = False
            archives.append(item)

        archives.sort(key=lambda x: (x["mtime_epoch"], x["filename"]), reverse=True)
        return archives


# ---------------------------------------------------------------------------
# Off-Site Backup Manager (Config, Dedup, Bounded Retries, Async Queue, Recovery)
# ---------------------------------------------------------------------------
class OffsiteBackupManager:
    """
    Coordinates administrator-controlled encrypted off-site backups via a synchronized folder,
    integrating safely with BackupService, EncryptedBackupService, BackupScheduler, and OS RecoveryKeyStore.
    """

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        backup_dir: Optional[Union[str, Path]] = None,
        *,
        config_path: Optional[Union[str, Path]] = None,
        key_store_provider: Callable[[], RecoveryKeyStore] = get_recovery_key_store,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db_manager or get_db_manager()
        self.backup_service = BackupService(self.db, backup_dir=backup_dir)
        self._explicit_config_path = Path(config_path) if config_path is not None else None
        self._key_store_provider = key_store_provider
        self._clock = clock

        self._lock = threading.RLock()
        self._publish_lock = threading.Lock()
        self._in_flight_hashes: set[str] = set()
        self._in_flight_thread: Optional[threading.Thread] = None

    def get_config_path(self) -> Path:
        if self._explicit_config_path is not None:
            return self._explicit_config_path
        return self.backup_service.get_backup_dir() / OFFSITE_CONFIG_FILENAME

    def _forbidden_sync_dirs(self) -> List[Path]:
        dirs: List[Path] = []
        if self.db.db_path != ":memory:":
            try:
                dirs.append(Path(self.db.db_path).resolve().parent)
            except OSError:
                pass
        return dirs

    def _default_state(self) -> Dict[str, Any]:
        return {
            "config_version": OFFSITE_CONFIG_VERSION,
            "enabled": False,
            "provider_type": "sync_folder",
            "destination_folder": None,
            "last_status": OffsiteStatus.DISABLED,
            "last_status_message": "Encrypted off-site backup is currently disabled.",
            "last_milestones": [],
            "last_published_at_utc": None,
            "last_published_filename": None,
            "last_published_sha256": None,
            "last_published_size_bytes": None,
            "last_published_size_display": None,
            "last_published_key_id": None,
            "last_source_backup_filename": None,
            "last_source_backup_sha256": None,
            "last_checksum_verified": False,
            "last_error": None,
            "last_error_at_utc": None,
            "published_source_hashes": {},
            "pending_retry": None,
        }

    def load_state(self) -> Dict[str, Any]:
        cfg_path = self.get_config_path()
        with self._lock:
            base = self._default_state()
            if not cfg_path.exists():
                return base
            try:
                loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    base.update(loaded)
                    if not isinstance(base.get("published_source_hashes"), dict):
                        base["published_source_hashes"] = {}
            except (OSError, ValueError):
                logger.warning("Off-site backup config file unreadable; using safe disabled defaults.")
            return base

    def _save_state(self, state: Dict[str, Any]) -> None:
        cfg_path = self.get_config_path()
        with self._lock:
            _atomic_write_json(cfg_path, state)

    def _build_provider(self, destination_folder: Optional[str]) -> SyncFolderOffsiteBackupProvider:
        return SyncFolderOffsiteBackupProvider(
            destination_folder,
            forbidden_dirs=self._forbidden_sync_dirs(),
        )

    def validate_folder(self, folder_path: str, *, include_archives: bool = False) -> Dict[str, Any]:
        provider = self._build_provider(folder_path)
        result = provider.validate_destination(folder_path)
        if include_archives and result.get("available"):
            try:
                result["archives"] = provider.list_published_archives()
            except OffsiteBackupError:
                result["archives"] = []
        return result

    def update_config(
        self,
        *,
        enabled: bool,
        destination_folder: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update non-secret off-site backup configuration.
        - Validates selected directory when enabling or updating destination_folder.
        - Disabling NEVER deletes any archives already present in the synchronized folder.
        """
        with self._lock:
            state = self.load_state()
            new_folder = state.get("destination_folder")
            if destination_folder is not None:
                cleaned = str(destination_folder).strip()
                new_folder = cleaned if cleaned else None

            if enabled:
                if not new_folder:
                    raise OffsiteDestinationUnavailableError(
                        "Please select a valid synchronized folder before enabling off-site backup."
                    )
                val = self.validate_folder(new_folder)
                if not val["available"]:
                    raise OffsiteDestinationUnavailableError(val["message"])
                new_folder = val["destination_folder"]
            elif new_folder:
                # Validate explicitly provided folder even while disabled
                if destination_folder is not None:
                    val = self.validate_folder(new_folder)
                    if not val["available"]:
                        raise OffsiteDestinationUnavailableError(val["message"])
                    new_folder = val["destination_folder"]

            prev_folder = state.get("destination_folder")
            if new_folder != prev_folder:
                # Destination changed: clear per-destination dedup map so archives publish cleanly to new folder
                state["published_source_hashes"] = {}
                state["pending_retry"] = None

            state["enabled"] = bool(enabled)
            state["destination_folder"] = new_folder
            if not state["enabled"]:
                state["pending_retry"] = None
                state["last_status"] = OffsiteStatus.DISABLED
                state["last_status_message"] = (
                    "Encrypted off-site backup is disabled. Existing archives in your synchronized folder are preserved untouched."
                )
            elif state.get("last_status") == OffsiteStatus.DISABLED:
                state["last_status"] = OffsiteStatus.IDLE
                state["last_status_message"] = (
                    "Encrypted off-site backup is enabled and ready for the next verified local backup."
                )
            self._save_state(state)
        return self.get_status()

    def _record_failure(
        self,
        state: Dict[str, Any],
        *,
        status: str,
        message: str,
        milestones: List[str],
        source_backup_path: Optional[Path] = None,
        source_sha256: Optional[str] = None,
        schedule_retry: bool = False,
    ) -> None:
        safe_msg = redact_sensitive_text(message)
        state["last_status"] = status
        state["last_status_message"] = safe_msg
        state["last_milestones"] = list(milestones)
        state["last_error"] = safe_msg
        state["last_error_at_utc"] = utc_now_iso()
        if schedule_retry and source_backup_path is not None and source_sha256:
            prev_retry = state.get("pending_retry") or {}
            prev_attempts = (
                int(prev_retry.get("attempts", 0))
                if prev_retry.get("source_sha256") == source_sha256
                else 0
            )
            attempts = prev_attempts + 1
            if attempts <= MAX_RETRY_ATTEMPTS:
                delay = RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
                state["pending_retry"] = {
                    "source_backup_path": str(source_backup_path),
                    "source_backup_filename": source_backup_path.name,
                    "source_sha256": source_sha256,
                    "attempts": attempts,
                    "max_attempts": MAX_RETRY_ATTEMPTS,
                    "next_retry_epoch": float(self._clock()) + float(delay),
                    "last_error": safe_msg,
                }
            else:
                state["pending_retry"] = None
        self._save_state(state)

    def publish_verified_backup(
        self,
        source_backup_path: Optional[Union[str, Path]] = None,
        *,
        allow_duplicate: bool = False,
        force_when_disabled: bool = False,
    ) -> Dict[str, Any]:
        """
        Encrypt a verified local `.db` backup using the active OS-stored Recovery Key and
        publish ONLY the verified `.kdb.enc` archive to the selected synchronized folder.

        Never copies plaintext `.db`, never overwrites existing archives, never blocks ERP transactions.
        """
        state = self.load_state()
        if not state.get("enabled") and not force_when_disabled:
            return {
                "action": "skipped",
                "status": OffsiteStatus.DISABLED,
                "message": "Off-site backup is disabled.",
                "cloud_sync_confirmed": False,
            }

        dest_folder = state.get("destination_folder")
        if not dest_folder:
            msg = "No synchronized backup folder is configured."
            self._record_failure(state, status=OffsiteStatus.DESTINATION_UNAVAILABLE, message=msg, milestones=[])
            return {
                "action": "failed",
                "status": OffsiteStatus.DESTINATION_UNAVAILABLE,
                "message": msg,
                "cloud_sync_confirmed": False,
            }

        # Resolve source verified `.db` backup
        src_path: Optional[Path] = Path(source_backup_path) if source_backup_path is not None else None
        if src_path is None:
            backups = self.backup_service.list_backups()
            verified = [b for b in backups if b.get("integrity_ok") and b.get("valid_for_restore")]
            if not verified:
                # Create a fresh verified local backup first
                src_path = self.backup_service.create_backup(backup_type="MANUAL")
            else:
                src_path = self.backup_service.resolve_safe_backup_path(verified[0]["filename"])

        if not src_path.exists() or not src_path.is_file() or not src_path.name.lower().endswith(".db"):
            msg = "Verified local SQLite .db backup was not found for off-site publishing."
            self._record_failure(state, status=OffsiteStatus.OPERATION_FAILED, message=msg, milestones=[])
            return {
                "action": "failed",
                "status": OffsiteStatus.OPERATION_FAILED,
                "message": msg,
                "cloud_sync_confirmed": False,
            }

        milestones: List[str] = []

        # Step 1: Verify local `.db` backup integrity, FK, and schema
        with pinned_backup_file(src_path):
            insp = self.backup_service.inspect_backup(src_path)
            if not (insp.get("integrity_ok") and insp.get("foreign_key_ok") and insp.get("has_required_tables")):
                msg = "Local backup failed integrity/schema verification and cannot be published off-site."
                self._record_failure(state, status=OffsiteStatus.OPERATION_FAILED, message=msg, milestones=[])
                return {
                    "action": "failed",
                    "status": OffsiteStatus.OPERATION_FAILED,
                    "message": msg,
                    "cloud_sync_confirmed": False,
                }
            source_sha256 = BackupService.compute_file_sha256(src_path)
            milestones.append(OffsiteStatus.LOCAL_BACKUP_VERIFIED)

            # Prevent simultaneous publication of the same verified source backup
            with self._lock:
                if source_sha256 in self._in_flight_hashes:
                    return {
                        "action": "skipped",
                        "reason": "publication_in_progress",
                        "status": state.get("last_status") or OffsiteStatus.IDLE,
                        "message": "Off-site publication for this backup is already in progress.",
                        "cloud_sync_confirmed": False,
                    }
                self._in_flight_hashes.add(source_sha256)

            try:
                with self._publish_lock:
                    state = self.load_state()
                    # Deduplication: skip if this exact verified backup SHA-256 was already published
                    published_map = state.get("published_source_hashes") or {}
                    prev_pub = published_map.get(source_sha256)
                    if not allow_duplicate and isinstance(prev_pub, dict):
                        return {
                            "action": "skipped",
                            "reason": "already_published",
                            "status": OffsiteStatus.CLOUD_SYNC_NOT_CONFIRMED,
                            "filename": prev_pub.get("archive_filename"),
                            "sha256": prev_pub.get("archive_sha256"),
                            "key_id": prev_pub.get("key_id"),
                            "cloud_sync_confirmed": False,
                            "cloud_sync_disclaimer": CLOUD_SYNC_DISCLAIMER,
                            "message": (
                                f"Unchanged verified backup already published as '{prev_pub.get('archive_filename')}'. "
                                "Skipped redundant duplicate encryption."
                            ),
                        }

                    # Step 2: Revalidate synchronized destination BEFORE encrypting
                    provider = self._build_provider(dest_folder)
                    dest_check = provider.validate_destination(dest_folder)
                    if not dest_check["available"]:
                        self._record_failure(
                            state,
                            status=OffsiteStatus.DESTINATION_UNAVAILABLE,
                            message=dest_check["message"],
                            milestones=milestones,
                            source_backup_path=src_path,
                            source_sha256=source_sha256,
                            schedule_retry=True,
                        )
                        return {
                            "action": "failed",
                            "status": OffsiteStatus.DESTINATION_UNAVAILABLE,
                            "milestones": milestones,
                            "message": dest_check["message"],
                            "cloud_sync_confirmed": False,
                        }

                    # Step 3: Verify OS-protected Recovery Key availability (FAIL CLOSED — never plaintext or memory fallback)
                    key_store = self._key_store_provider()
                    if not key_store.is_available() or not key_store.has_key():
                        msg = (
                            f"OS-protected Recovery Key storage ({key_store.backend_name}) is unavailable or has no active Recovery Key. "
                            "Off-site encrypted backup stopped safely (plaintext copies are never written to synchronized folders)."
                        )
                        self._record_failure(
                            state,
                            status=OffsiteStatus.RECOVERY_KEY_UNAVAILABLE,
                            message=msg,
                            milestones=milestones,
                            source_backup_path=src_path,
                            source_sha256=source_sha256,
                            schedule_retry=False,
                        )
                        return {
                            "action": "failed",
                            "status": OffsiteStatus.RECOVERY_KEY_UNAVAILABLE,
                            "milestones": milestones,
                            "message": msg,
                            "cloud_sync_confirmed": False,
                        }

                    # Step 4: Encrypt & round-trip verify inside an isolated staging dir OUTSIDE the sync folder
                    stage_dir = _create_restricted_temp_dir(prefix="kdb_offsite_stage_")
                    try:
                        enc_svc = EncryptedBackupService(
                            self.db,
                            backup_dir=self.backup_service.get_backup_dir(),
                            key_store=key_store,
                        )
                        stem = src_path.name[:-3] if src_path.name.lower().endswith(".db") else src_path.name
                        staged_enc_path = stage_dir / f"{stem}.kdb.enc"
                        enc_meta = enc_svc.encrypt_backup(
                            src_path,
                            staged_enc_path,
                            recovery_key=None,  # Active OS-stored key resolved strictly inside backend
                            verify_roundtrip=True,
                        )
                        milestones.append(OffsiteStatus.ENCRYPTION_COMPLETED)

                        # Step 5 & 6: Publish completed `.kdb.enc` atomically & verify destination byte count + SHA-256
                        pub_meta = provider.publish_encrypted_archive(
                            staged_enc_path,
                            expected_sha256=enc_meta["encrypted_sha256"],
                            preferred_filename=staged_enc_path.name,
                        )
                        milestones.append(OffsiteStatus.COPIED_TO_SYNC_FOLDER)
                        milestones.append(OffsiteStatus.DESTINATION_CHECKSUM_VERIFIED)
                        milestones.append(OffsiteStatus.CLOUD_SYNC_NOT_CONFIRMED)
                    except (RecoveryKeyStoreUnavailableError, RecoveryKeyStoreError, InvalidRecoveryKeyError) as exc:
                        self._record_failure(
                            state,
                            status=OffsiteStatus.RECOVERY_KEY_UNAVAILABLE,
                            message=str(exc),
                            milestones=milestones,
                            source_backup_path=src_path,
                            source_sha256=source_sha256,
                            schedule_retry=False,
                        )
                        return {
                            "action": "failed",
                            "status": OffsiteStatus.RECOVERY_KEY_UNAVAILABLE,
                            "milestones": milestones,
                            "message": redact_sensitive_text(str(exc)),
                            "cloud_sync_confirmed": False,
                        }
                    except OffsiteDestinationUnavailableError as exc:
                        self._record_failure(
                            state,
                            status=OffsiteStatus.DESTINATION_UNAVAILABLE,
                            message=str(exc),
                            milestones=milestones,
                            source_backup_path=src_path,
                            source_sha256=source_sha256,
                            schedule_retry=True,
                        )
                        return {
                            "action": "failed",
                            "status": OffsiteStatus.DESTINATION_UNAVAILABLE,
                            "milestones": milestones,
                            "message": redact_sensitive_text(str(exc)),
                            "cloud_sync_confirmed": False,
                        }
                    except Exception as exc:
                        self._record_failure(
                            state,
                            status=OffsiteStatus.OPERATION_FAILED,
                            message=str(exc),
                            milestones=milestones,
                            source_backup_path=src_path,
                            source_sha256=source_sha256,
                            schedule_retry=True,
                        )
                        return {
                            "action": "failed",
                            "status": OffsiteStatus.OPERATION_FAILED,
                            "milestones": milestones,
                            "message": redact_sensitive_text(str(exc)),
                            "cloud_sync_confirmed": False,
                        }
                    finally:
                        shutil.rmtree(stage_dir, ignore_errors=True)

                    # Step 7: Record non-secret success state (honestly reporting cloud_sync_confirmed=False)
                    now_utc = utc_now_iso()
                    state["last_status"] = OffsiteStatus.CLOUD_SYNC_NOT_CONFIRMED
                    state["last_status_message"] = CLOUD_SYNC_DISCLAIMER
                    state["last_milestones"] = list(milestones)
                    state["last_published_at_utc"] = now_utc
                    state["last_published_filename"] = pub_meta["filename"]
                    state["last_published_sha256"] = pub_meta["sha256"]
                    state["last_published_size_bytes"] = pub_meta["size_bytes"]
                    state["last_published_size_display"] = pub_meta["size_display"]
                    state["last_published_key_id"] = enc_meta["key_id"]
                    state["last_source_backup_filename"] = src_path.name
                    state["last_source_backup_sha256"] = source_sha256
                    state["last_checksum_verified"] = True
                    state["last_error"] = None
                    state["last_error_at_utc"] = None
                    state["pending_retry"] = None

                    pub_map = dict(state.get("published_source_hashes") or {})
                    pub_map[source_sha256] = {
                        "archive_filename": pub_meta["filename"],
                        "archive_sha256": pub_meta["sha256"],
                        "key_id": enc_meta["key_id"],
                        "source_filename": src_path.name,
                        "published_at_utc": now_utc,
                    }
                    # Keep bounded history map (latest 200 hashes)
                    if len(pub_map) > 200:
                        keys_sorted = sorted(
                            pub_map.keys(),
                            key=lambda k: pub_map[k].get("published_at_utc") or "",
                            reverse=True,
                        )[:200]
                        pub_map = {k: pub_map[k] for k in keys_sorted}
                    state["published_source_hashes"] = pub_map
                    self._save_state(state)

                    logger.info(
                        "Encrypted off-site archive '%s' copied & checksum-verified in sync folder (key_id=%s, sha256=%s...). Cloud upload completion depends on sync provider.",
                        pub_meta["filename"],
                        enc_meta["key_id"],
                        pub_meta["sha256"][:12],
                    )
                    return {
                        "action": "published",
                        "status": OffsiteStatus.CLOUD_SYNC_NOT_CONFIRMED,
                        "milestones": milestones,
                        "filename": pub_meta["filename"],
                        "file_path": pub_meta["file_path"],
                        "destination_folder": pub_meta["destination_folder"],
                        "size_bytes": pub_meta["size_bytes"],
                        "size_display": pub_meta["size_display"],
                        "sha256": pub_meta["sha256"],
                        "key_id": enc_meta["key_id"],
                        "source_backup_filename": src_path.name,
                        "source_backup_sha256": source_sha256,
                        "local_backup_verified": True,
                        "encryption_completed": True,
                        "copied_to_sync_folder": True,
                        "destination_checksum_verified": True,
                        "cloud_sync_confirmed": False,
                        "cloud_sync_disclaimer": CLOUD_SYNC_DISCLAIMER,
                        "message": CLOUD_SYNC_DISCLAIMER,
                    }
            finally:
                with self._lock:
                    self._in_flight_hashes.discard(source_sha256)

    def copy_existing_local_encrypted_archive(self, local_encrypted_filename: str) -> Dict[str, Any]:
        """
        Copy an existing verified local `.kdb.enc` archive from BACKUP_DIR into the synchronized folder
        without overwriting any existing archive.
        """
        state = self.load_state()
        dest_folder = state.get("destination_folder")
        if not dest_folder:
            raise OffsiteDestinationUnavailableError("No synchronized backup folder is configured.")

        enc_svc = EncryptedBackupService(self.db, backup_dir=self.backup_service.get_backup_dir())
        local_enc_path = enc_svc.resolve_safe_encrypted_backup_path(local_encrypted_filename)
        if not local_enc_path.exists() or not local_enc_path.is_file():
            raise FileNotFoundError("Requested local encrypted archive was not found.")

        hdr_info = enc_svc.inspect_encrypted_backup(local_enc_path)
        local_sha, _ = _compute_sha256_and_size(local_enc_path)
        provider = self._build_provider(dest_folder)
        pub_meta = provider.publish_encrypted_archive(
            local_enc_path,
            expected_sha256=local_sha,
            preferred_filename=local_enc_path.name,
        )

        milestones = [
            OffsiteStatus.COPIED_TO_SYNC_FOLDER,
            OffsiteStatus.DESTINATION_CHECKSUM_VERIFIED,
            OffsiteStatus.CLOUD_SYNC_NOT_CONFIRMED,
        ]
        state["last_status"] = OffsiteStatus.CLOUD_SYNC_NOT_CONFIRMED
        state["last_status_message"] = CLOUD_SYNC_DISCLAIMER
        state["last_milestones"] = milestones
        state["last_published_at_utc"] = utc_now_iso()
        state["last_published_filename"] = pub_meta["filename"]
        state["last_published_sha256"] = pub_meta["sha256"]
        state["last_published_size_bytes"] = pub_meta["size_bytes"]
        state["last_published_size_display"] = pub_meta["size_display"]
        state["last_published_key_id"] = hdr_info.get("key_id")
        state["last_checksum_verified"] = True
        state["last_error"] = None
        state["last_error_at_utc"] = None
        self._save_state(state)

        return {
            "action": "published",
            "status": OffsiteStatus.CLOUD_SYNC_NOT_CONFIRMED,
            "milestones": milestones,
            "filename": pub_meta["filename"],
            "destination_folder": pub_meta["destination_folder"],
            "size_bytes": pub_meta["size_bytes"],
            "size_display": pub_meta["size_display"],
            "sha256": pub_meta["sha256"],
            "key_id": hdr_info.get("key_id"),
            "destination_checksum_verified": True,
            "cloud_sync_confirmed": False,
            "cloud_sync_disclaimer": CLOUD_SYNC_DISCLAIMER,
        }

    def enqueue_async_publish(self, source_backup_path: Union[str, Path]) -> bool:
        """
        Enqueue encrypted off-site publishing on a background daemon thread so BackupScheduler
        and normal shop transactions are never blocked by slow synced folders.
        """
        state = self.load_state()
        if not state.get("enabled") or not state.get("destination_folder"):
            return False
        src = Path(source_backup_path)

        def _worker() -> None:
            try:
                self.publish_verified_backup(src, allow_duplicate=False)
            except Exception as exc:
                logger.warning("Background off-site backup worker error: %s", redact_sensitive_text(str(exc)))

        with self._lock:
            if self._in_flight_thread is not None and self._in_flight_thread.is_alive():
                return False
            t = threading.Thread(
                target=_worker,
                name="krushidhan-offsite-publish",
                daemon=True,
            )
            self._in_flight_thread = t
            t.start()
            return True

    def process_pending_retry_if_due(self) -> Optional[Dict[str, Any]]:
        """Retry a previously deferred off-site publish once backoff has elapsed and destination returns."""
        state = self.load_state()
        if not state.get("enabled"):
            return None
        pending = state.get("pending_retry")
        if not isinstance(pending, dict):
            return None
        next_epoch = float(pending.get("next_retry_epoch") or 0.0)
        if float(self._clock()) < next_epoch:
            return None
        src_raw = pending.get("source_backup_path")
        if not src_raw or not Path(src_raw).is_file():
            state["pending_retry"] = None
            self._save_state(state)
            return None
        return self.publish_verified_backup(Path(src_raw), allow_duplicate=False)

    def inspect_folder_archive(
        self,
        *,
        filename: str,
        folder_path: Optional[str] = None,
        recovery_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Inspect a `.kdb.enc` archive located in the configured or administrator-selected synchronized folder
        (supports replacement-computer recovery). Never modifies or deletes the archive.
        """
        state = self.load_state()
        target_folder = folder_path if (folder_path is not None and str(folder_path).strip()) else state.get("destination_folder")
        provider = self._build_provider(target_folder)
        archive_path = provider.resolve_archive_path(filename)
        if not archive_path.exists() or not archive_path.is_file():
            raise FileNotFoundError("Requested encrypted archive was not found in the synchronized folder.")

        enc_svc = EncryptedBackupService(
            self.db,
            backup_dir=self.backup_service.get_backup_dir(),
            key_store=self._key_store_provider(),
        )
        rk = recovery_key if (recovery_key is not None and recovery_key.strip()) else None
        info = enc_svc.inspect_encrypted_backup(
            archive_path,
            recovery_key=rk,
            use_key_store=True,
        )
        info["destination_folder"] = str(archive_path.parent)
        info["archive_sha256"] = BackupService.compute_file_sha256(archive_path)
        return info

    def restore_folder_archive(
        self,
        *,
        filename: str,
        confirm_restore: bool,
        folder_path: Optional[str] = None,
        recovery_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Authenticate, decrypt, verify, and safely restore a `.kdb.enc` archive from a synchronized folder
        (replacement-computer or disaster recovery).
        - Requires explicit `confirm_restore is True`.
        - Uses Phase 2C-4B decrypt + SQLite verification + existing `BackupService.restore_backup()`
          (`BEFORE_RESTORE` snapshot & automatic rollback).
        - Never modifies or deletes the original `.kdb.enc` archive in the synchronized folder.
        """
        if confirm_restore is not True:
            raise ValueError("Explicit confirmation (confirm_restore=true) is required before restoring database.")

        state = self.load_state()
        target_folder = folder_path if (folder_path is not None and str(folder_path).strip()) else state.get("destination_folder")
        provider = self._build_provider(target_folder)
        archive_path = provider.resolve_archive_path(filename)
        if not archive_path.exists() or not archive_path.is_file():
            raise FileNotFoundError("Requested encrypted archive was not found in the synchronized folder.")

        enc_svc = EncryptedBackupService(
            self.db,
            backup_dir=self.backup_service.get_backup_dir(),
            key_store=self._key_store_provider(),
        )
        rk = recovery_key if (recovery_key is not None and recovery_key.strip()) else None

        temp_dir = _create_restricted_temp_dir(prefix="kdb_offsite_restore_")
        try:
            decrypted_db = temp_dir / "restored_verified.db"
            dec_meta = enc_svc.decrypt_backup(
                archive_path,
                decrypted_db,
                recovery_key=rk,
                verify_sqlite=True,
            )
            self.backup_service.restore_backup(decrypted_db)
            pre_restore = self.backup_service.last_pre_restore_backup
            return {
                "success": True,
                "message": "Database restored safely from verified off-site encrypted archive.",
                "restored_from": archive_path.name,
                "destination_folder": str(archive_path.parent),
                "key_id": dec_meta["key_id"],
                "plaintext_sha256": dec_meta["plaintext_sha256"],
                "pre_restore_filename": pre_restore.name if pre_restore else None,
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def list_local_encrypted_archives(self) -> List[Dict[str, Any]]:
        """List existing verified local `.kdb.enc` archives in BACKUP_DIR (for optional re-copy to sync folder)."""
        bdir = self.backup_service.get_backup_dir()
        if not bdir.exists():
            return []
        out: List[Dict[str, Any]] = []
        for p in bdir.iterdir():
            if p.name.startswith(".") or not p.name.lower().endswith(".kdb.enc") or not p.is_file():
                continue
            try:
                st = p.stat()
                out.append(
                    {
                        "filename": p.name,
                        "size_bytes": int(st.st_size),
                        "size_display": BackupService._format_size_display(st.st_size),
                        "modified_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        "mtime_epoch": float(st.st_mtime),
                    }
                )
            except OSError:
                continue
        out.sort(key=lambda x: (x["mtime_epoch"], x["filename"]), reverse=True)
        return out

    def get_status(self) -> Dict[str, Any]:
        """Return complete, honest, non-secret status for the Backup Manager Off-Site Backup section."""
        state = self.load_state()
        dest_folder = state.get("destination_folder")
        dest_validation = (
            self.validate_folder(dest_folder)
            if dest_folder
            else {
                "available": False,
                "writable": False,
                "provider_type": "sync_folder",
                "destination_folder": None,
                "free_bytes": None,
                "free_display": None,
                "message": "No synchronized folder selected yet.",
            }
        )

        archives: List[Dict[str, Any]] = []
        if dest_folder and dest_validation.get("available"):
            try:
                archives = self._build_provider(dest_folder).list_published_archives()
            except OffsiteBackupError:
                archives = []

        key_store = self._key_store_provider()
        key_store_available = key_store.is_available()
        active_key_id = key_store.get_active_key_id() if key_store_available else None

        effective_status = state.get("last_status") or (OffsiteStatus.IDLE if state.get("enabled") else OffsiteStatus.DISABLED)
        if state.get("enabled") and not dest_validation.get("available"):
            effective_status = OffsiteStatus.DESTINATION_UNAVAILABLE
        elif state.get("enabled") and (not key_store_available or not active_key_id):
            if effective_status not in (OffsiteStatus.DESTINATION_UNAVAILABLE,):
                effective_status = OffsiteStatus.RECOVERY_KEY_UNAVAILABLE

        return {
            "enabled": bool(state.get("enabled")),
            "provider_type": state.get("provider_type") or "sync_folder",
            "destination_folder": dest_folder,
            "destination_available": bool(dest_validation.get("available")),
            "destination_writable": bool(dest_validation.get("writable")),
            "destination_message": dest_validation.get("message"),
            "destination_free_display": dest_validation.get("free_display"),
            "recovery_key_available": bool(key_store_available and active_key_id),
            "active_key_id": active_key_id,
            "status": effective_status,
            "status_message": state.get("last_status_message"),
            "milestones": list(state.get("last_milestones") or []),
            "last_published_at_utc": state.get("last_published_at_utc"),
            "last_published_filename": state.get("last_published_filename"),
            "last_published_sha256": state.get("last_published_sha256"),
            "last_published_size_bytes": state.get("last_published_size_bytes"),
            "last_published_size_display": state.get("last_published_size_display"),
            "last_published_key_id": state.get("last_published_key_id"),
            "last_source_backup_filename": state.get("last_source_backup_filename"),
            "last_checksum_verified": bool(state.get("last_checksum_verified")),
            "last_error": state.get("last_error"),
            "last_error_at_utc": state.get("last_error_at_utc"),
            "pending_retry": state.get("pending_retry"),
            "cloud_sync_confirmed": False,
            "cloud_sync_disclaimer": CLOUD_SYNC_DISCLAIMER,
            "local_backups_preserved_notice": (
                "Local SQLite .db backups remain stored on this computer and are never deleted after encrypted copies are published. "
                "Synchronized-folder archives are never deleted automatically."
            ),
            "published_archives": archives,
            "local_encrypted_archives": self.list_local_encrypted_archives(),
        }
