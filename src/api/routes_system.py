"""
API Route Handlers for Company Profile Settings, Runtime Status, and Database Backup/Restore.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, SecretStr
from starlette.background import BackgroundTask

from src.api.auth_middleware import require_admin_user
from src.config import APP_VERSION
from src.db.connection import DatabaseMaintenanceError, get_db_manager
from src.models.system import AuditLogEntry, CompanySettingsUpdate
from src.repositories.system_repository import SystemRepository
from src.services.backup_scheduler import BackupScheduler, get_backup_scheduler
from src.services.backup_service import (
    BackupService,
    InsufficientDiskSpaceError,
    pin_backup_file,
    register_active_staging_path,
    unpin_backup_file,
)
from src.services.encrypted_backup_service import (
    CryptoDependencyError,
    EncryptedBackupAuthError,
    EncryptedBackupError,
    EncryptedBackupFormatError,
    EncryptedBackupService,
    KDB_ENC_MAGIC,
    MAX_ENCRYPTED_ARCHIVE_BYTES,
    MAX_ENCRYPTED_UPLOAD_BYTES,
    _create_restricted_temp_dir,
    _restrict_path_permissions,
)
from src.desktop_runtime import redact_sensitive_text
from src.services.offsite_backup_service import (
    OffsiteBackupError,
    OffsiteBackupManager,
    OffsiteDestinationUnavailableError,
    OffsiteStatus,
)
from src.services.os_recovery_key_store import get_recovery_key_store
from src.services.recovery_key import (
    InvalidRecoveryKeyError,
    RecoveryKeyIntegrityError,
    RecoveryKeyStoreError,
    RecoveryKeyStoreUnavailableError,
)
from src.services.recovery_key_lifecycle import (
    RecoveryKeyLifecycleError,
    get_recovery_key_lifecycle,
)

logger = logging.getLogger("agri_erp.system")

router = APIRouter(prefix="/api/system", tags=["System"])
_SAFE_DB_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class RestoreBackupRequest(BaseModel):
    backup_filename: str
    confirm_restore: Optional[bool] = None


class EncryptBackupRequest(BaseModel):
    backup_filename: str
    recovery_key: Optional[SecretStr] = None


class ConfirmRecoveryKeyRequest(BaseModel):
    operation_id: str
    recovery_key: SecretStr
    acknowledged_card_saved: bool = False


class CancelRecoveryKeyRequest(BaseModel):
    operation_id: Optional[str] = None


class ImportRecoveryKeyRequest(BaseModel):
    recovery_key: SecretStr
    confirm_import: bool = False
    set_as_active: bool = False


class VerifyRecoveryKeyRequest(BaseModel):
    key_id: Optional[str] = None


class OffsiteBackupConfigRequest(BaseModel):
    enabled: bool
    destination_folder: Optional[str] = None


class OffsiteValidateFolderRequest(BaseModel):
    folder_path: str
    include_archives: bool = False


class OffsiteRunNowRequest(BaseModel):
    backup_filename: Optional[str] = None
    allow_duplicate: bool = False


class OffsiteCopyLocalEncryptedRequest(BaseModel):
    encrypted_filename: str


class OffsiteInspectFolderArchiveRequest(BaseModel):
    filename: str
    folder_path: Optional[str] = None
    recovery_key: Optional[SecretStr] = None


class OffsiteRestoreFolderArchiveRequest(BaseModel):
    filename: str
    folder_path: Optional[str] = None
    recovery_key: Optional[SecretStr] = None
    confirm_restore: bool = False


def _resolve_admin_uid(admin: Dict[str, Any]) -> int:
    raw_uid = admin.get("user_id")
    try:
        uid = int(raw_uid)
        if uid > 0:
            return uid
    except (TypeError, ValueError):
        pass
    raise HTTPException(status_code=401, detail="Authenticated administrator identity is required.")


def _audit_recovery_key_event(admin: Dict[str, Any], *, action_type: str, details: str) -> None:
    """Record non-secret Recovery Key lifecycle audit metadata (NEVER logs key material)."""
    try:
        db = get_db_manager()
        repo = SystemRepository(db)
        valid_uid: Optional[int] = None
        raw_uid = admin.get("user_id")
        if raw_uid is not None:
            try:
                cand = int(raw_uid)
                if repo.get_user_by_id(cand) is not None:
                    valid_uid = cand
            except (TypeError, ValueError):
                valid_uid = None
        repo.log_action(
            AuditLogEntry(
                user_id=valid_uid,
                action_type=action_type,
                table_name="recovery_keys",
                record_id=None,
                details=redact_sensitive_text(details),
            )
        )
    except Exception:
        logger.debug("Non-fatal recovery key audit log warning.", exc_info=False)


def _sanitize_uploaded_encrypted_name(filename: Optional[str]) -> str:
    raw = str(filename or "").strip()
    base = Path(raw.replace("\\", "/")).name
    if not base or not base.lower().endswith(".kdb.enc"):
        raise HTTPException(
            status_code=400,
            detail="Only encrypted backup archives with a .kdb.enc extension are accepted.",
        )
    safe = _SAFE_DB_NAME_RE.sub("_", base).strip("._")
    if not safe.lower().endswith(".kdb.enc"):
        safe = f"{safe}.kdb.enc" if safe else "external_backup.kdb.enc"
    return safe


def _get_max_encrypted_upload_bytes(enc_svc: Optional[EncryptedBackupService] = None) -> int:
    """
    Return the consistent encrypted backup upload size limit derived from the
    `.kdb.enc` container format bounds so every valid generated archive can be uploaded.
    """
    module_cap = int(MAX_ENCRYPTED_UPLOAD_BYTES)
    if enc_svc is None:
        return module_cap
    return min(module_cap, int(enc_svc.max_archive_bytes))


async def _stage_uploaded_encrypted_file(
    upload: UploadFile,
    *,
    max_upload_bytes: Optional[int] = None,
) -> Tuple[Path, Path, str]:
    """
    Stream an uploaded `.kdb.enc` file into a restricted temporary directory while
    enforcing magic header validation and maximum upload size limits on received bytes
    (never trusting Content-Length alone). Returns (temp_dir, staged_file_path, safe_filename).
    """
    safe_name = _sanitize_uploaded_encrypted_name(upload.filename)
    effective_limit = (
        int(MAX_ENCRYPTED_UPLOAD_BYTES) if max_upload_bytes is None else int(max_upload_bytes)
    )
    if effective_limit < len(KDB_ENC_MAGIC) + 16:
        raise HTTPException(
            status_code=500,
            detail="Encrypted backup upload limit configuration is invalid.",
        )

    temp_dir = _create_restricted_temp_dir(prefix="kdb_enc_upload_")
    staged_path = temp_dir / safe_name
    total_read = 0
    magic_buf = bytearray()
    header_checked = False
    chunk_size = 65536
    try:
        with open(staged_path, "wb") as out_fh:
            while True:
                chunk = await upload.read(chunk_size)
                if not chunk:
                    break
                if not header_checked:
                    needed = len(KDB_ENC_MAGIC) - len(magic_buf)
                    magic_buf.extend(chunk[:needed])
                    if len(magic_buf) == len(KDB_ENC_MAGIC):
                        header_checked = True
                        if bytes(magic_buf) != KDB_ENC_MAGIC:
                            raise HTTPException(
                                status_code=400,
                                detail="Uploaded file is not a valid Krushidhan encrypted backup archive.",
                            )
                total_read += len(chunk)
                if total_read > effective_limit:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Uploaded encrypted backup exceeds maximum allowed size of "
                            f"{BackupService._format_size_display(effective_limit)} "
                            f"({effective_limit:,} bytes)."
                        ),
                    )
                out_fh.write(chunk)
        if not header_checked or total_read < len(KDB_ENC_MAGIC) + 16:
            raise HTTPException(
                status_code=400,
                detail="Uploaded encrypted backup is empty or truncated.",
            )
        _restrict_path_permissions(staged_path, is_dir=False)
        return temp_dir, staged_path, safe_name
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _sanitize_uploaded_db_name(filename: Optional[str]) -> str:
    raw = str(filename or "").strip()
    base = Path(raw.replace("\\", "/")).name
    if not base or not base.lower().endswith(".db"):
        raise HTTPException(
            status_code=400,
            detail="Only SQLite database backup files with a .db extension are accepted.",
        )
    safe = _SAFE_DB_NAME_RE.sub("_", base).strip("._")
    if not safe.lower().endswith(".db"):
        safe = f"{safe}.db" if safe else "external_backup.db"
    return safe


async def _stage_uploaded_db_file(
    upload: UploadFile,
    backup_svc: BackupService,
) -> Tuple[Path, str]:
    safe_name = _sanitize_uploaded_db_name(upload.filename)
    target_dir = backup_svc.get_backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    fd, tmp_raw = tempfile.mkstemp(prefix=".upload_inspect_", suffix=".db.tmp", dir=str(target_dir))
    tmp_path = Path(tmp_raw)
    total_read = 0
    header_checked = False
    chunk_size = 65536
    try:
        with os.fdopen(fd, "wb") as out_fh:
            while True:
                chunk = await upload.read(chunk_size)
                if not chunk:
                    break
                if not header_checked:
                    header_checked = True
                    if (
                        len(chunk) < len(BackupService.SQLITE_HEADER_MAGIC)
                        or not chunk.startswith(BackupService.SQLITE_HEADER_MAGIC)
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail="Uploaded file is not a valid SQLite database backup (invalid header).",
                        )
                total_read += len(chunk)
                if total_read > BackupService.MAX_BACKUP_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Uploaded backup exceeds maximum allowed size of "
                            f"{BackupService.MAX_BACKUP_UPLOAD_BYTES // (1024 * 1024)} MB."
                        ),
                    )
                out_fh.write(chunk)
        if total_read < 100:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty or too small to be a valid SQLite database backup.",
            )
        return tmp_path, safe_name
    except Exception:
        BackupService._unlink_with_retry(tmp_path, ignore_missing=True)
        raise


@router.get("/settings")
def get_settings():
    repo = SystemRepository(get_db_manager())
    return repo.get_company_settings()


def _apply_settings_update(settings: CompanySettingsUpdate, admin: Dict[str, Any]) -> Dict[str, Any]:
    raw_fields_set = getattr(settings, "model_fields_set", None)
    if raw_fields_set is None:
        raw_fields_set = getattr(settings, "__fields_set__", set())
    fields_set = set(raw_fields_set)
    if not fields_set:
        raise HTTPException(status_code=400, detail="No settings fields provided to update.")

    repo = SystemRepository(get_db_manager())
    try:
        updated = repo.update_company_settings(settings)
        admin_id_raw = admin.get("user_id")
        valid_uid: Optional[int] = None
        if admin_id_raw is not None:
            try:
                candidate_uid = int(admin_id_raw)
                if repo.get_user_by_id(candidate_uid) is not None:
                    valid_uid = candidate_uid
            except (TypeError, ValueError):
                valid_uid = None
        repo.log_action(
            AuditLogEntry(
                user_id=valid_uid,
                action_type="UPDATE",
                table_name="company_settings",
                record_id=1,
                details=f"Updated shop settings fields: {', '.join(sorted(fields_set))}",
            )
        )
        return {
            "success": True,
            "message": "Settings updated successfully",
            "settings": updated.model_dump(),
        }
    except DatabaseMaintenanceError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/settings")
def update_settings(settings: CompanySettingsUpdate, admin: Dict[str, Any] = Depends(require_admin_user)):
    return _apply_settings_update(settings, admin)


@router.patch("/settings")
def patch_settings(settings: CompanySettingsUpdate, admin: Dict[str, Any] = Depends(require_admin_user)):
    return _apply_settings_update(settings, admin)


@router.post("/backup")
def trigger_backup(admin: Dict[str, Any] = Depends(require_admin_user)):
    svc = BackupService(get_db_manager())
    try:
        backup_file = svc.create_backup(backup_type="MANUAL")
        st = backup_file.stat()
        meta = svc.last_backup_metadata or {}
        return {
            "success": True,
            "file_path": str(backup_file),
            "filename": backup_file.name,
            "backup_type": "MANUAL",
            "size_bytes": int(st.st_size),
            "size_display": BackupService._format_size_display(st.st_size),
            "created_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "sha256": svc.last_backup_sha256 or meta.get("sha256"),
            "schema_version": meta.get("schema_version"),
        }
    except DatabaseMaintenanceError:
        raise
    except InsufficientDiskSpaceError as e:
        raise HTTPException(status_code=507, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _get_scheduler_status_payload(
    svc: BackupService,
    *,
    backups: Optional[list] = None,
) -> Dict[str, Any]:
    sched = get_backup_scheduler(db_manager=svc.db)
    if sched is not None:
        return sched.get_status(backups=backups)
    return BackupScheduler(db_manager=svc.db).get_status(backups=backups)


@router.get("/backups")
def list_available_backups(admin: Dict[str, Any] = Depends(require_admin_user)):
    """Admin Only: List verified .db backup files in BACKUP_DIR with scheduler status."""
    svc = BackupService(get_db_manager())
    try:
        backups = svc.list_backups()
        return {
            "backup_dir": str(svc.get_backup_dir()),
            "backups": backups,
            "scheduler": _get_scheduler_status_payload(svc, backups=backups),
        }
    except DatabaseMaintenanceError:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/backup-status")
def get_backup_scheduler_status(admin: Dict[str, Any] = Depends(require_admin_user)):
    """Admin Only: Return automatic local backup scheduler status, disk health, and last auto backup."""
    svc = BackupService(get_db_manager())
    try:
        return _get_scheduler_status_payload(svc)
    except DatabaseMaintenanceError:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/backups/{filename}/inspect")
def inspect_backup_file(filename: str, admin: Dict[str, Any] = Depends(require_admin_user)):
    """Admin Only: Run PRAGMA integrity_check, FK check, and schema verification on a backup file."""
    svc = BackupService(get_db_manager())
    try:
        candidate = svc.resolve_safe_backup_path(filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Requested backup file was not found in backup directory.")

    info = svc.inspect_backup(candidate)
    return {
        "filename": candidate.name,
        "size_bytes": info["size_bytes"],
        "size_display": info["size_display"],
        "modified_at": info["modified_at"],
        "integrity_ok": info["integrity_ok"],
        "integrity_results": info["integrity_results"],
        "foreign_key_ok": info["foreign_key_ok"],
        "foreign_key_violation_count": len(info["foreign_key_violations"]),
        "table_count": info["table_count"],
        "schema_version": info["schema_version"],
        "shop_name": info["shop_name"],
        "has_required_tables": info["has_required_tables"],
        "missing_required_tables": info["missing_required_tables"],
        "valid_for_restore": info["valid_for_restore"],
    }


@router.get("/backups/{filename}/download")
def download_backup_file(filename: str, admin: Dict[str, Any] = Depends(require_admin_user)):
    """Admin Only: Export/download a verified backup .db file to user-selected folder or USB drive."""
    svc = BackupService(get_db_manager())
    try:
        candidate = svc.resolve_safe_backup_path(filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Requested backup file was not found in backup directory.")

    pinned_path = pin_backup_file(candidate)
    try:
        if not svc.verify_database_integrity(candidate):
            unpin_backup_file(pinned_path)
            raise HTTPException(
                status_code=400,
                detail="Backup file failed integrity verification and cannot be exported safely.",
            )

        return FileResponse(
            path=str(candidate),
            media_type="application/vnd.sqlite3",
            headers={
                "Content-Disposition": f'attachment; filename="{candidate.name}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
            background=BackgroundTask(unpin_backup_file, pinned_path),
        )
    except HTTPException:
        raise
    except Exception:
        unpin_backup_file(pinned_path)
        raise


@router.post("/backups/inspect-upload")
async def inspect_uploaded_backup_file(
    file: UploadFile = File(...),
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Admin Only: Safely validate an external .db backup file before user confirmation."""
    svc = BackupService(get_db_manager())
    staged_path, safe_name = await _stage_uploaded_db_file(file, svc)
    try:
        with register_active_staging_path(staged_path):
            info = svc.inspect_backup(staged_path)
            return {
                "filename": safe_name,
                "size_bytes": info["size_bytes"],
                "size_display": info["size_display"],
                "modified_at": info["modified_at"],
                "integrity_ok": info["integrity_ok"],
                "integrity_results": info["integrity_results"],
                "foreign_key_ok": info["foreign_key_ok"],
                "foreign_key_violation_count": len(info["foreign_key_violations"]),
                "table_count": info["table_count"],
                "schema_version": info["schema_version"],
                "shop_name": info["shop_name"],
                "has_required_tables": info["has_required_tables"],
                "missing_required_tables": info["missing_required_tables"],
                "valid_for_restore": info["valid_for_restore"],
            }
    finally:
        BackupService._unlink_with_retry(staged_path, ignore_missing=True)


@router.post("/backup/restore")
def restore_from_backup(payload: RestoreBackupRequest, admin: Dict[str, Any] = Depends(require_admin_user)):
    """
    Admin Only: Restore a verified backup from the configured BACKUP_DIR.
    Safely creates a recoverable BEFORE_RESTORE backup of current data first.
    Requires explicit confirm_restore=true.
    """
    if payload.confirm_restore is not True:
        raise HTTPException(
            status_code=400,
            detail="Explicit confirmation (confirm_restore=true) is required before restoring database.",
        )

    svc = BackupService(get_db_manager())
    try:
        candidate = svc.resolve_safe_backup_path(payload.backup_filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Requested backup file was not found in backup directory.")

    try:
        svc.restore_backup(candidate)
        pre_restore_path = svc.last_pre_restore_backup
        logger.info("Database restored from backup file '%s' by admin user_id=%s", candidate.name, admin.get("user_id"))
        return {
            "success": True,
            "message": "Database restored safely from verified backup.",
            "restored_from": candidate.name,
            "pre_restore_backup": str(pre_restore_path) if pre_restore_path else None,
            "pre_restore_filename": pre_restore_path.name if pre_restore_path else None,
        }
    except DatabaseMaintenanceError as e:
        raise HTTPException(status_code=503, detail=str(e), headers={"Retry-After": "2"})
    except InsufficientDiskSpaceError as e:
        raise HTTPException(status_code=507, detail=str(e))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/backup/restore-upload")
async def restore_from_uploaded_backup(
    file: UploadFile = File(...),
    confirm_restore: bool = Form(False),
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Admin Only: Validate and restore a user-selected external `.db` backup copy.
    Preserves a recoverable BEFORE_RESTORE snapshot of current live data first.
    """
    if confirm_restore is not True:
        raise HTTPException(status_code=400, detail="Explicit restore confirmation is required before restoring database.")

    svc = BackupService(get_db_manager())
    staged_path, safe_name = await _stage_uploaded_db_file(file, svc)
    try:
        with register_active_staging_path(staged_path):
            svc.restore_backup(staged_path)
            pre_restore_path = svc.last_pre_restore_backup
            logger.info("Database restored from uploaded external backup '%s' by admin user_id=%s", safe_name, admin.get("user_id"))
            return {
                "success": True,
                "message": "Database restored safely from external backup file.",
                "restored_from": safe_name,
                "pre_restore_backup": str(pre_restore_path) if pre_restore_path else None,
                "pre_restore_filename": pre_restore_path.name if pre_restore_path else None,
            }
    except DatabaseMaintenanceError as e:
        raise HTTPException(status_code=503, detail=str(e), headers={"Retry-After": "2"})
    except InsufficientDiskSpaceError as e:
        raise HTTPException(status_code=507, detail=str(e))
    except HTTPException:
        raise
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        BackupService._unlink_with_retry(staged_path, ignore_missing=True)


# =========================================================================
# OS-Protected Recovery Key Management Endpoints (Admin Only — Phase 2C-4C)
# =========================================================================

@router.get("/recovery-keys/status")
def get_recovery_key_store_status(admin: Dict[str, Any] = Depends(require_admin_user)):
    """Admin Only: Return non-secret OS secure key store status, active key ID, and historical metadata."""
    try:
        lifecycle = get_recovery_key_lifecycle()
        return lifecycle.get_store_status()
    except RecoveryKeyStoreError as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)))
    except Exception:
        logger.error("Unexpected failure checking Recovery Key store status.", exc_info=False)
        raise HTTPException(status_code=500, detail="Unable to check OS-protected key storage status.")


@router.post("/recovery-keys/setup/start")
def start_recovery_key_setup(admin: Dict[str, Any] = Depends(require_admin_user)):
    """
    Admin Only: Generate a first-time 256-bit Recovery Key held only in short-lived process memory
    bound to the authenticated Admin, returned ONCE for Recovery Card display and printing.
    """
    uid = _resolve_admin_uid(admin)
    try:
        result = get_recovery_key_lifecycle().start_generation(uid, mode="setup")
        _audit_recovery_key_event(
            admin,
            action_type="RECOVERY_KEY_SETUP_STARTED",
            details=f"Started first-time Recovery Key setup (key_id={result['key_id']}, backend={result['storage_backend']})",
        )
        return result
    except RecoveryKeyStoreUnavailableError as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)))
    except (RecoveryKeyLifecycleError, RecoveryKeyStoreError, ValueError) as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
    except Exception:
        logger.error("Unexpected error starting Recovery Key setup.", exc_info=False)
        raise HTTPException(status_code=500, detail="Failed to start Recovery Key setup.")


@router.post("/recovery-keys/rotate/start")
def start_recovery_key_rotation(admin: Dict[str, Any] = Depends(require_admin_user)):
    """
    Admin Only: Generate a new Recovery Key for rotation (returned ONCE for new Recovery Card).
    Activating it updates future encrypted backups only; all previous keys remain preserved.
    """
    uid = _resolve_admin_uid(admin)
    try:
        result = get_recovery_key_lifecycle().start_generation(uid, mode="rotate")
        _audit_recovery_key_event(
            admin,
            action_type="RECOVERY_KEY_ROTATION_STARTED",
            details=(
                f"Started Recovery Key rotation (new_key_id={result['key_id']}, "
                f"previous_active={result.get('previous_active_key_id')}, backend={result['storage_backend']})"
            ),
        )
        return result
    except RecoveryKeyStoreUnavailableError as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)))
    except (RecoveryKeyLifecycleError, RecoveryKeyStoreError, ValueError) as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
    except Exception:
        logger.error("Unexpected error starting Recovery Key rotation.", exc_info=False)
        raise HTTPException(status_code=500, detail="Failed to start Recovery Key rotation.")


@router.post("/recovery-keys/confirm")
def confirm_recovery_key_setup_or_rotation(
    payload: ConfirmRecoveryKeyRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Admin Only: Confirm a pending Recovery Key by re-entering the complete key and acknowledging
    Recovery Card retention. Stores and verifies in OS-protected storage; never echoes the raw key.
    """
    uid = _resolve_admin_uid(admin)
    try:
        result = get_recovery_key_lifecycle().confirm_generation(
            uid,
            operation_id=payload.operation_id,
            confirmed_recovery_key=payload.recovery_key.get_secret_value(),
            acknowledged_card_saved=payload.acknowledged_card_saved,
        )
        _audit_recovery_key_event(
            admin,
            action_type="RECOVERY_KEY_ACTIVATED",
            details=(
                f"Confirmed & activated Recovery Key (mode={result['mode']}, "
                f"key_id={result['key_id']}, backend={result['storage_backend']}, total_keys={result['key_count']})"
            ),
        )
        return result
    except RecoveryKeyStoreUnavailableError as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)))
    except (InvalidRecoveryKeyError, RecoveryKeyLifecycleError, RecoveryKeyIntegrityError, RecoveryKeyStoreError, ValueError) as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
    except Exception:
        logger.error("Unexpected error confirming Recovery Key.", exc_info=False)
        raise HTTPException(status_code=500, detail="Failed to confirm Recovery Key.")


@router.post("/recovery-keys/cancel")
def cancel_pending_recovery_key(
    payload: Optional[CancelRecoveryKeyRequest] = None,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Admin Only: Cancel and purge any unconfirmed pending Recovery Key from process memory."""
    uid = _resolve_admin_uid(admin)
    op_id = payload.operation_id if payload is not None else None
    result = get_recovery_key_lifecycle().cancel_generation(uid, operation_id=op_id)
    if result.get("cancelled_count"):
        _audit_recovery_key_event(
            admin,
            action_type="RECOVERY_KEY_PENDING_CANCELLED",
            details=f"Cancelled {result['cancelled_count']} unconfirmed pending Recovery Key operation(s).",
        )
    return result


@router.post("/recovery-keys/import")
def import_existing_recovery_key(
    payload: ImportRecoveryKeyRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Admin Only: Import an existing Recovery Key (e.g. from a printed Recovery Card on a
    replacement computer) into the platform OS secure store. Preserves all existing stored keys.
    """
    uid = _resolve_admin_uid(admin)
    try:
        result = get_recovery_key_lifecycle().import_recovery_key(
            uid,
            recovery_key=payload.recovery_key.get_secret_value(),
            confirm_import=payload.confirm_import,
            set_as_active=payload.set_as_active,
        )
        _audit_recovery_key_event(
            admin,
            action_type="RECOVERY_KEY_IMPORTED",
            details=(
                f"Imported Recovery Key (key_id={result['key_id']}, already_present={result['already_present']}, "
                f"is_active={result['is_active']}, backend={result['storage_backend']})"
            ),
        )
        return result
    except RecoveryKeyStoreUnavailableError as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)))
    except (InvalidRecoveryKeyError, RecoveryKeyLifecycleError, RecoveryKeyIntegrityError, RecoveryKeyStoreError, ValueError) as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
    except Exception:
        logger.error("Unexpected error importing Recovery Key.", exc_info=False)
        raise HTTPException(status_code=500, detail="Failed to import Recovery Key.")


@router.post("/recovery-keys/verify")
def verify_os_stored_recovery_key(
    payload: Optional[VerifyRecoveryKeyRequest] = None,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Admin Only: Verify that a stored Recovery Key can be retrieved from OS storage and passes validation."""
    target_id = payload.key_id if payload is not None else None
    try:
        result = get_recovery_key_lifecycle().verify_stored_key(key_id=target_id)
        _audit_recovery_key_event(
            admin,
            action_type="RECOVERY_KEY_VERIFIED",
            details=f"Verified OS-stored Recovery Key (key_id={result['key_id']}, backend={result['storage_backend']})",
        )
        return result
    except RecoveryKeyStoreUnavailableError as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)))
    except (RecoveryKeyIntegrityError, RecoveryKeyStoreError, ValueError) as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
    except Exception:
        logger.error("Unexpected error verifying stored Recovery Key.", exc_info=False)
        raise HTTPException(status_code=500, detail="Failed to verify stored Recovery Key.")


@router.post("/backups/encrypt")
def create_encrypted_backup(
    payload: EncryptBackupRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Admin Only: Encrypt an existing verified local `.db` backup into a `.kdb.enc` v1 archive.
    Uses the active key from OS-protected storage when `recovery_key` is omitted, or the
    explicitly supplied Recovery Key (POST body only; never logged or persisted).
    """
    db = get_db_manager()
    bkp_svc = BackupService(db)
    key_store = get_recovery_key_store()
    enc_svc = EncryptedBackupService(db, key_store=key_store)
    try:
        candidate = bkp_svc.resolve_safe_backup_path(payload.backup_filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Requested backup file was not found in backup directory.")

    explicit_rk = payload.recovery_key.get_secret_value() if payload.recovery_key is not None else None
    if explicit_rk is not None and not explicit_rk.strip():
        explicit_rk = None

    try:
        meta = enc_svc.encrypt_backup(
            candidate,
            recovery_key=explicit_rk,
        )
        logger.info(
            "Encrypted backup archive '%s' created (key_id=%s) by admin user_id=%s",
            meta["filename"],
            meta["key_id"],
            admin.get("user_id"),
        )
        _audit_recovery_key_event(
            admin,
            action_type="ENCRYPTED_BACKUP_CREATED",
            details=(
                f"Encrypted backup '{meta['filename']}' created (key_id={meta['key_id']}, "
                f"key_source={'manual' if explicit_rk else key_store.backend_name})"
            ),
        )
        return {
            "success": True,
            "filename": meta["filename"],
            "size_bytes": meta["size_bytes"],
            "size_display": meta["size_display"],
            "encrypted_sha256": meta["encrypted_sha256"],
            "plaintext_sha256": meta["plaintext_sha256"],
            "plaintext_size_bytes": meta["plaintext_size_bytes"],
            "key_id": meta["key_id"],
            "key_source": "manual" if explicit_rk else "os_secure_store",
            "format_version": meta["format_version"],
            "cipher": meta["cipher"],
            "compression": meta["compression"],
            "chunk_count": meta["chunk_count"],
            "schema_version": meta["schema_version"],
            "created_at_utc": meta["created_at_utc"],
        }
    except DatabaseMaintenanceError as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)), headers={"Retry-After": "2"})
    except (CryptoDependencyError, RecoveryKeyStoreUnavailableError) as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)))
    except (
        InvalidRecoveryKeyError,
        RecoveryKeyIntegrityError,
        RecoveryKeyStoreError,
        EncryptedBackupFormatError,
        EncryptedBackupAuthError,
        EncryptedBackupError,
        ValueError,
    ) as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
    except Exception:
        logger.error("Unexpected encrypted backup creation failure.", exc_info=False)
        raise HTTPException(status_code=500, detail="Failed to create encrypted backup archive.")


@router.get("/backups/encrypted/{filename}/download")
def download_encrypted_backup_file(
    filename: str,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Admin Only: Export/download a `.kdb.enc` encrypted backup archive."""
    enc_svc = EncryptedBackupService(get_db_manager())
    try:
        candidate = enc_svc.resolve_safe_encrypted_backup_path(filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Requested encrypted backup file was not found.")

    try:
        enc_svc.inspect_encrypted_backup(candidate)
    except EncryptedBackupFormatError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return FileResponse(
        path=str(candidate),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{candidate.name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.post("/backups/encrypted/inspect-upload")
async def inspect_uploaded_encrypted_backup(
    file: UploadFile = File(...),
    recovery_key: Optional[str] = Form(None),
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Admin Only: Inspect an uploaded `.kdb.enc` archive.
    Validates container format and authenticates using either the supplied `recovery_key`
    or the matching historical/active `key_id` from OS-protected storage when available.
    Clearly distinguishes unauthenticated header metadata (`metadata_authenticated: false`)
    from verified authenticated metadata (`metadata_authenticated: true`).
    """
    enc_svc = EncryptedBackupService(get_db_manager(), key_store=get_recovery_key_store())
    max_upload_bytes = _get_max_encrypted_upload_bytes(enc_svc)
    temp_dir, staged_path, safe_name = await _stage_uploaded_encrypted_file(
        file,
        max_upload_bytes=max_upload_bytes,
    )
    try:
        rk = recovery_key if (recovery_key is not None and recovery_key.strip()) else None
        info = enc_svc.inspect_encrypted_backup(
            staged_path,
            recovery_key=rk,
            use_key_store=True,
            max_archive_bytes=max_upload_bytes,
        )
        info["filename"] = safe_name
        return info
    except CryptoDependencyError as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)))
    except (
        InvalidRecoveryKeyError,
        RecoveryKeyIntegrityError,
        RecoveryKeyStoreError,
        EncryptedBackupFormatError,
        EncryptedBackupAuthError,
        EncryptedBackupError,
        ValueError,
    ) as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected encrypted backup inspection failure.", exc_info=False)
        raise HTTPException(status_code=500, detail="Failed to inspect encrypted backup archive.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.post("/backups/encrypted/restore-upload")
async def restore_from_uploaded_encrypted_backup(
    file: UploadFile = File(...),
    recovery_key: Optional[str] = Form(None),
    confirm_restore: bool = Form(False),
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Admin Only: Authenticate, decrypt, verify, and atomically restore an uploaded `.kdb.enc` archive.
    Requires explicit `confirm_restore=true`. Uses the explicitly supplied `recovery_key` if provided,
    or automatically resolves the archive's exact `key_id` (active or historical) from OS-protected storage.
    Preserves BEFORE_RESTORE snapshot and automatic rollback via BackupService.restore_backup().
    """
    if confirm_restore is not True:
        raise HTTPException(
            status_code=400,
            detail="Explicit confirmation (confirm_restore=true) is required before restoring database.",
        )

    db = get_db_manager()
    bkp_svc = BackupService(db)
    key_store = get_recovery_key_store()
    enc_svc = EncryptedBackupService(db, key_store=key_store)
    max_upload_bytes = _get_max_encrypted_upload_bytes(enc_svc)
    temp_dir, staged_path, safe_name = await _stage_uploaded_encrypted_file(
        file,
        max_upload_bytes=max_upload_bytes,
    )
    try:
        rk = recovery_key if (recovery_key is not None and recovery_key.strip()) else None
        decrypted_db_path = temp_dir / "decrypted_verified.db"
        dec_meta = enc_svc.decrypt_backup(
            staged_path,
            decrypted_db_path,
            recovery_key=rk,
            verify_sqlite=True,
            max_archive_bytes=max_upload_bytes,
        )
        with register_active_staging_path(decrypted_db_path):
            bkp_svc.restore_backup(decrypted_db_path)
            pre_restore_path = bkp_svc.last_pre_restore_backup

        logger.info(
            "Database restored from encrypted backup '%s' (key_id=%s) by admin user_id=%s",
            safe_name,
            dec_meta["key_id"],
            admin.get("user_id"),
        )
        _audit_recovery_key_event(
            admin,
            action_type="ENCRYPTED_BACKUP_RESTORED",
            details=(
                f"Restored database from encrypted archive '{safe_name}' (key_id={dec_meta['key_id']}, "
                f"key_source={'manual' if rk else key_store.backend_name})"
            ),
        )
        return {
            "success": True,
            "message": "Database restored safely from verified encrypted backup archive.",
            "restored_from": safe_name,
            "key_id": dec_meta["key_id"],
            "plaintext_sha256": dec_meta["plaintext_sha256"],
            "pre_restore_filename": pre_restore_path.name if pre_restore_path else None,
        }
    except DatabaseMaintenanceError as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)), headers={"Retry-After": "2"})
    except InsufficientDiskSpaceError as e:
        raise HTTPException(status_code=507, detail=redact_sensitive_text(str(e)))
    except (CryptoDependencyError, RecoveryKeyStoreUnavailableError) as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)))
    except (
        InvalidRecoveryKeyError,
        RecoveryKeyIntegrityError,
        RecoveryKeyStoreError,
        EncryptedBackupFormatError,
        EncryptedBackupAuthError,
        EncryptedBackupError,
        ValueError,
    ) as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
    except HTTPException:
        raise
    except Exception:
        logger.error("Unexpected encrypted backup restore failure.", exc_info=False)
        raise HTTPException(status_code=500, detail="Encrypted backup restoration failed safely.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# =========================================================================
# Encrypted Off-Site Backup via Synchronized Folders (Admin Only — 2C-5A)
# =========================================================================

@router.get("/offsite-backup/status")
def get_offsite_backup_status(admin: Dict[str, Any] = Depends(require_admin_user)):
    """Admin Only: Return honest non-secret status, destination availability, and published `.kdb.enc` archives."""
    try:
        mgr = OffsiteBackupManager(get_db_manager())
        return mgr.get_status()
    except Exception:
        logger.error("Unexpected failure loading off-site backup status.", exc_info=False)
        raise HTTPException(status_code=500, detail="Unable to load off-site backup status.")


@router.post("/offsite-backup/config")
def update_offsite_backup_config(
    payload: OffsiteBackupConfigRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Admin Only: Enable/disable encrypted off-site backup and save the administrator-selected synchronized folder.
    Disabling preserves all existing archives in the synchronized folder untouched.
    """
    mgr = OffsiteBackupManager(get_db_manager())
    try:
        status = mgr.update_config(
            enabled=payload.enabled,
            destination_folder=payload.destination_folder,
        )
        _audit_recovery_key_event(
            admin,
            action_type="OFFSITE_BACKUP_CONFIG_UPDATED",
            details=(
                f"Off-site backup config updated (enabled={status['enabled']}, "
                f"destination_available={status['destination_available']})"
            ),
        )
        return status
    except OffsiteDestinationUnavailableError as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
    except (OffsiteBackupError, ValueError) as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
    except Exception:
        logger.error("Unexpected error updating off-site backup config.", exc_info=False)
        raise HTTPException(status_code=500, detail="Failed to update off-site backup configuration.")


@router.post("/offsite-backup/validate-folder")
def validate_offsite_backup_folder(
    payload: OffsiteValidateFolderRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Admin Only: Validate whether a user-selected folder exists, is writable, and optionally list `.kdb.enc` archives."""
    mgr = OffsiteBackupManager(get_db_manager())
    return mgr.validate_folder(payload.folder_path, include_archives=payload.include_archives)


@router.post("/offsite-backup/run-now")
def run_offsite_backup_now(
    payload: Optional[OffsiteRunNowRequest] = None,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Admin Only: Encrypt a verified local `.db` backup with the active OS-stored Recovery Key and
    publish ONLY the verified `.kdb.enc` archive to the selected synchronized folder.
    """
    db = get_db_manager()
    bkp_svc = BackupService(db)
    mgr = OffsiteBackupManager(db)
    source_path: Optional[Path] = None
    allow_dup = bool(payload.allow_duplicate) if payload is not None else False
    if payload is not None and payload.backup_filename:
        try:
            source_path = bkp_svc.resolve_safe_backup_path(payload.backup_filename)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
        if not source_path.exists() or not source_path.is_file():
            raise HTTPException(status_code=404, detail="Requested local backup file was not found.")

    try:
        result = mgr.publish_verified_backup(
            source_path,
            allow_duplicate=allow_dup,
            force_when_disabled=True,
        )
        if result.get("action") == "failed":
            status_code = (
                503
                if result.get("status")
                in (OffsiteStatus.DESTINATION_UNAVAILABLE, OffsiteStatus.RECOVERY_KEY_UNAVAILABLE)
                else 400
            )
            raise HTTPException(status_code=status_code, detail=result.get("message") or "Off-site backup failed.")
        _audit_recovery_key_event(
            admin,
            action_type="OFFSITE_ENCRYPTED_BACKUP_PUBLISHED",
            details=(
                f"Off-site backup action={result.get('action')} status={result.get('status')} "
                f"archive={result.get('filename')} key_id={result.get('key_id')}"
            ),
        )
        return result
    except HTTPException:
        raise
    except DatabaseMaintenanceError as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)), headers={"Retry-After": "2"})
    except InsufficientDiskSpaceError as e:
        raise HTTPException(status_code=507, detail=redact_sensitive_text(str(e)))
    except Exception:
        logger.error("Unexpected error running off-site backup.", exc_info=False)
        raise HTTPException(status_code=500, detail="Encrypted off-site backup failed safely.")


@router.post("/offsite-backup/copy-local-encrypted")
def copy_local_encrypted_archive_to_offsite(
    payload: OffsiteCopyLocalEncryptedRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """Admin Only: Copy an existing verified local `.kdb.enc` archive to the synchronized folder without overwriting."""
    mgr = OffsiteBackupManager(get_db_manager())
    try:
        result = mgr.copy_existing_local_encrypted_archive(payload.encrypted_filename)
        _audit_recovery_key_event(
            admin,
            action_type="OFFSITE_LOCAL_ARCHIVE_COPIED",
            details=f"Copied local encrypted archive to sync folder as '{result['filename']}' (key_id={result.get('key_id')})",
        )
        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=redact_sensitive_text(str(e)))
    except OffsiteDestinationUnavailableError as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)))
    except (OffsiteBackupError, EncryptedBackupError, ValueError) as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
    except Exception:
        logger.error("Unexpected error copying local encrypted archive off-site.", exc_info=False)
        raise HTTPException(status_code=500, detail="Failed to copy local encrypted archive to synchronized folder.")


@router.post("/offsite-backup/inspect-folder-archive")
def inspect_offsite_folder_archive(
    payload: OffsiteInspectFolderArchiveRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Admin Only: Inspect a `.kdb.enc` archive from a synchronized folder (replacement-computer recovery).
    Uses exact historical `key_id` from OS secure store when available, or optional manual Recovery Key.
    Never modifies or deletes the archive.
    """
    mgr = OffsiteBackupManager(get_db_manager())
    rk = payload.recovery_key.get_secret_value() if payload.recovery_key is not None else None
    try:
        return mgr.inspect_folder_archive(
            filename=payload.filename,
            folder_path=payload.folder_path,
            recovery_key=rk,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=redact_sensitive_text(str(e)))
    except (OffsiteDestinationUnavailableError, CryptoDependencyError) as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)))
    except (
        InvalidRecoveryKeyError,
        RecoveryKeyIntegrityError,
        RecoveryKeyStoreError,
        EncryptedBackupFormatError,
        EncryptedBackupAuthError,
        EncryptedBackupError,
        OffsiteBackupError,
        ValueError,
    ) as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
    except Exception:
        logger.error("Unexpected error inspecting synchronized-folder archive.", exc_info=False)
        raise HTTPException(status_code=500, detail="Failed to inspect synchronized-folder encrypted archive.")


@router.post("/offsite-backup/restore-folder-archive")
def restore_offsite_folder_archive(
    payload: OffsiteRestoreFolderArchiveRequest,
    admin: Dict[str, Any] = Depends(require_admin_user),
):
    """
    Admin Only: Authenticate, decrypt, verify, and atomically restore a `.kdb.enc` archive from a
    synchronized folder. Requires explicit `confirm_restore=true`, creates BEFORE_RESTORE snapshot,
    and preserves automatic rollback. Never modifies or deletes the original archive.
    """
    if payload.confirm_restore is not True:
        raise HTTPException(
            status_code=400,
            detail="Explicit confirmation (confirm_restore=true) is required before restoring database.",
        )

    mgr = OffsiteBackupManager(get_db_manager())
    rk = payload.recovery_key.get_secret_value() if payload.recovery_key is not None else None
    try:
        result = mgr.restore_folder_archive(
            filename=payload.filename,
            confirm_restore=True,
            folder_path=payload.folder_path,
            recovery_key=rk,
        )
        _audit_recovery_key_event(
            admin,
            action_type="OFFSITE_ENCRYPTED_ARCHIVE_RESTORED",
            details=f"Restored database from synchronized-folder archive '{result['restored_from']}' (key_id={result['key_id']})",
        )
        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=redact_sensitive_text(str(e)))
    except DatabaseMaintenanceError as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)), headers={"Retry-After": "2"})
    except InsufficientDiskSpaceError as e:
        raise HTTPException(status_code=507, detail=redact_sensitive_text(str(e)))
    except (OffsiteDestinationUnavailableError, CryptoDependencyError, RecoveryKeyStoreUnavailableError) as e:
        raise HTTPException(status_code=503, detail=redact_sensitive_text(str(e)))
    except (
        InvalidRecoveryKeyError,
        RecoveryKeyIntegrityError,
        RecoveryKeyStoreError,
        EncryptedBackupFormatError,
        EncryptedBackupAuthError,
        EncryptedBackupError,
        OffsiteBackupError,
        ValueError,
    ) as e:
        raise HTTPException(status_code=400, detail=redact_sensitive_text(str(e)))
    except Exception:
        logger.error("Unexpected error restoring synchronized-folder archive.", exc_info=False)
        raise HTTPException(status_code=500, detail="Off-site encrypted archive restoration failed safely.")


@router.get("/backup-history")
def get_backup_history(admin: Dict[str, Any] = Depends(require_admin_user)):
    svc = BackupService(get_db_manager())
    return svc.get_backup_history()


@router.get("/runtime-info")
def get_runtime_info(admin: Dict[str, Any] = Depends(require_admin_user)):
    """Admin Only: Return active APP_MODE, storage directories, cloud sync state, and schema version."""
    db = get_db_manager()
    backup_svc = BackupService(db)
    return {
        "app_mode": db.app_mode,
        "app_version": APP_VERSION,
        "db_path": str(db.db_path),
        "backup_dir": str(backup_svc.get_backup_dir()),
        "cloud_sync_enabled": bool(db.is_vercel and db._has_mongo and not db.is_desktop),
        "schema_version": db.get_schema_version(),
    }
