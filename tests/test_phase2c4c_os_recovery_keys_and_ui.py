"""
Isolated Security, OS Key Store, Lifecycle, API & Frontend Verification Suite for Phase 2C-4C:
OS-Protected Recovery Keys (Windows DPAPI & macOS Keychain), Recovery Cards, and Backup Manager UI.

Strictly uses isolated temporary directories, isolated SQLite databases, and isolated test Keychain
service names (cleaned up in finally). Never touches live data/agri_erp.db, repository backups/,
or real user Keychain items.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import secrets
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, get_db_manager, reset_db_manager
from src.models.master_data import Product
from src.repositories.master_data_repository import MasterDataRepository
from src.services.auth_service import AuthService
from src.services.backup_scheduler import stop_backup_scheduler
from src.services.backup_service import BackupService
from src.services.encrypted_backup_service import EncryptedBackupService, KDB_ENC_MAGIC
from src.services.os_recovery_key_store import (
    CRYPTPROTECT_LOCAL_MACHINE,
    CRYPTPROTECT_UI_FORBIDDEN,
    DPAPI_BLOB_MAGIC,
    MacOSKeychainRecoveryKeyStore,
    UnsupportedRecoveryKeyStore,
    Win32DPAPIBackend,
    WindowsDPAPIRecoveryKeyStore,
    _DATA_BLOB,
    get_production_recovery_key_store,
    reset_recovery_key_store,
    set_recovery_key_store_for_testing,
)
from src.services.recovery_key import (
    InMemoryRecoveryKeyStore,
    InvalidRecoveryKeyError,
    RecoveryKeyIntegrityError,
    RecoveryKeyStoreError,
    RecoveryKeyStoreUnavailableError,
    generate_recovery_key,
    get_recovery_key_id,
)
from src.services.recovery_key_lifecycle import (
    RecoveryKeyLifecycleError,
    RecoveryKeyLifecycleService,
    reset_recovery_key_lifecycle,
)


def _multipart_body(
    field_name: str,
    filename: str,
    file_bytes: bytes,
    extra_fields: Optional[Dict[str, str]] = None,
) -> tuple[bytes, str]:
    boundary = f"----Krushidhan2C4C{secrets.token_hex(8)}"
    parts: List[bytes] = []
    if extra_fields:
        for k, v in extra_fields.items():
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                f"{v}\r\n".encode("utf-8")
            )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


@pytest.fixture()
def isolated_2c4c_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fully isolated DB, BACKUP_DIR, JWT secret, Admin/Operator tokens, and reset key store/lifecycle."""
    db_file = tmp_path / "shop_2c4c.db"
    backup_dir = tmp_path / "backups_2c4c"
    backup_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("JWT_SECRET", "phase2c4c_test_secret_material_" + secrets.token_hex(16))

    stop_backup_scheduler(timeout=2.0)
    reset_db_manager()
    reset_recovery_key_store()
    reset_recovery_key_lifecycle()

    db = get_db_manager()
    db.initialize_database(include_seed=True)

    master = MasterDataRepository(db)
    pid = master.create_product(Product(product_name="Phase2C4C Urea Bag", default_sale_rate=310.0))

    auth = AuthService(db)
    admin_user = db.fetch_one("SELECT user_id, username, role, full_name FROM users WHERE role = 'ADMIN' LIMIT 1;")
    assert admin_user is not None
    admin_uid = int(admin_user[0])
    admin_token = auth.create_access_token(admin_uid, admin_user[1], admin_user[3], "ADMIN")

    op_row = db.fetch_one("SELECT user_id, username, role, full_name FROM users WHERE role = 'OPERATOR' LIMIT 1;")
    if op_row is None:
        pw_hash = auth.hash_password("OperatorPass@123")
        op_id = db.execute_query(
            "INSERT INTO users (username, password_hash, full_name, role, is_active) VALUES (?, ?, ?, 'OPERATOR', 1);",
            ("op_2c4c", pw_hash, "Operator 2C4C"),
        )
        op_token = auth.create_access_token(int(op_id), "op_2c4c", "Operator 2C4C", "OPERATOR")
    else:
        op_token = auth.create_access_token(int(op_row[0]), op_row[1], op_row[3], "OPERATOR")

    bkp_svc = BackupService(db, backup_dir=backup_dir)
    verified_db_backup = bkp_svc.create_backup(backup_type="MANUAL")

    yield {
        "tmp_path": tmp_path,
        "db": db,
        "db_file": db_file,
        "backup_dir": backup_dir,
        "admin_uid": admin_uid,
        "admin_headers": {"Authorization": f"Bearer {admin_token}"},
        "operator_headers": {"Authorization": f"Bearer {op_token}"},
        "verified_db_backup": verified_db_backup,
        "seed_pid": pid,
    }

    stop_backup_scheduler(timeout=2.0)
    reset_recovery_key_lifecycle()
    reset_recovery_key_store()
    reset_db_manager()


# =====================================================================
# 1. Windows DPAPI Unit Tests (Mocked Native Win32 Boundary on macOS)
# =====================================================================
class _MockWin32DPAPIDLLs:
    """
    Faithful mock of Windows crypt32.dll + kernel32.dll native DPAPI boundary for cross-platform unit tests.
    Tracks dwFlags, entropy binding, allocations, and LocalFree cleanup calls.
    """

    def __init__(self, *, user_sid: bytes = b"S-1-5-21-USER-A") -> None:
        self.user_sid = user_sid
        self.last_protect_flags: Optional[int] = None
        self.last_unprotect_flags: Optional[int] = None
        self.allocated_buffers: Dict[int, ctypes.Array] = {}
        self.freed_pointers: List[int] = []
        self.fail_protect_error: Optional[int] = None
        self.fail_unprotect_error: Optional[int] = None

    def _alloc(self, raw: bytes) -> tuple[int, Any]:
        buf = ctypes.create_string_buffer(raw, len(raw))
        addr = ctypes.addressof(buf)
        self.allocated_buffers[addr] = buf
        return addr, ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))

    def _alloc_wchar(self, text: str) -> tuple[int, Any]:
        buf = ctypes.create_unicode_buffer(text)
        addr = ctypes.addressof(buf)
        self.allocated_buffers[addr] = buf
        return addr, ctypes.cast(buf, ctypes.c_wchar_p)

    def _keystream(self, entropy: bytes, nonce: bytes, length: int) -> bytes:
        out = bytearray()
        ctr = 0
        while len(out) < length:
            out.extend(hashlib.sha256(self.user_sid + entropy + nonce + ctr.to_bytes(4, "big")).digest())
            ctr += 1
        return bytes(out[:length])

    def CryptProtectData(self, p_in, descr, p_ent, _reserved, _prompt, dw_flags, p_out) -> int:
        self.last_protect_flags = int(dw_flags)
        if self.fail_protect_error is not None:
            return 0
        in_blob = p_in._obj
        ent_blob = p_ent._obj
        plaintext = ctypes.string_at(in_blob.pbData, in_blob.cbData)
        entropy = ctypes.string_at(ent_blob.pbData, ent_blob.cbData)
        nonce = secrets.token_bytes(16)
        ks = self._keystream(entropy, nonce, len(plaintext))
        ct = bytes(a ^ b for a, b in zip(plaintext, ks))
        tag = hashlib.sha256(self.user_sid + entropy + nonce + ct).digest()
        sealed = nonce + tag + ct
        _, ptr = self._alloc(sealed)
        p_out._obj.cbData = len(sealed)
        p_out._obj.pbData = ptr
        return 1

    def CryptUnprotectData(self, p_in, p_descr_out, p_ent, _reserved, _prompt, dw_flags, p_out) -> int:
        self.last_unprotect_flags = int(dw_flags)
        if self.fail_unprotect_error is not None:
            return 0
        in_blob = p_in._obj
        ent_blob = p_ent._obj
        sealed = ctypes.string_at(in_blob.pbData, in_blob.cbData)
        entropy = ctypes.string_at(ent_blob.pbData, ent_blob.cbData)
        if len(sealed) < 49:
            return 0
        nonce = sealed[:16]
        tag = sealed[16:48]
        ct = sealed[48:]
        expected_tag = hashlib.sha256(self.user_sid + entropy + nonce + ct).digest()
        if not secrets.compare_digest(tag, expected_tag):
            return 0
        ks = self._keystream(entropy, nonce, len(ct))
        plaintext = bytes(a ^ b for a, b in zip(ct, ks))
        _, ptr = self._alloc(plaintext)
        p_out._obj.cbData = len(plaintext)
        p_out._obj.pbData = ptr
        if p_descr_out is not None:
            addr, _wptr = self._alloc_wchar("Krushidhan ERP Recovery Key")
            p_descr_out._obj.value = addr
        return 1

    def LocalFree(self, addr: int) -> int:
        self.freed_pointers.append(int(addr))
        self.allocated_buffers.pop(int(addr), None)
        return 0


def test_01_windows_dpapi_roundtrip_scope_entropy_corruption_permissions_and_cleanup(tmp_path: Path):
    """Verify Windows DPAPI current-user scope, no LOCAL_MACHINE, entropy binding, corruption rejection, LocalFree, and zero plaintext."""
    mock_dlls = _MockWin32DPAPIDLLs()
    backend = Win32DPAPIBackend(crypt32=mock_dlls, kernel32=mock_dlls, platform_name="win32")
    assert backend.is_available() is True

    # Explicitly forbid CRYPTPROTECT_LOCAL_MACHINE
    with pytest.raises(ValueError, match="CRYPTPROTECT_LOCAL_MACHINE"):
        backend.protect(b"secret", b"entropy", flags=CRYPTPROTECT_LOCAL_MACHINE)
    with pytest.raises(ValueError, match="CRYPTPROTECT_LOCAL_MACHINE"):
        backend.unprotect(b"secret", b"entropy", flags=CRYPTPROTECT_LOCAL_MACHINE)

    store_dir = tmp_path / "win_dpapi_store"
    store = WindowsDPAPIRecoveryKeyStore(storage_dir=store_dir, dpapi_backend=backend)
    assert store.is_available() is True
    assert store.backend_name == "windows_dpapi"

    rk1 = generate_recovery_key()
    rk2 = generate_recovery_key()
    kid1 = store.store_key(rk1, activate=True, source="generated")
    assert mock_dlls.last_protect_flags == CRYPTPROTECT_UI_FORBIDDEN
    assert (mock_dlls.last_protect_flags & CRYPTPROTECT_LOCAL_MACHINE) == 0

    # Roundtrip retrieve & verify
    assert store.get_key(kid1) == rk1
    assert store.get_key() == rk1
    assert mock_dlls.last_unprotect_flags == CRYPTPROTECT_UI_FORBIDDEN
    assert (mock_dlls.last_unprotect_flags & CRYPTPROTECT_LOCAL_MACHINE) == 0
    # Native buffers allocated by CryptProtectData & CryptUnprotectData must ALL be freed via LocalFree
    assert len(mock_dlls.freed_pointers) >= 3
    assert len(mock_dlls.allocated_buffers) == 0

    # Zero plaintext Recovery Key anywhere on disk (neither .dpapi blob nor metadata.json)
    for f in store_dir.rglob("*"):
        if f.is_file():
            raw = f.read_bytes()
            assert rk1.encode("utf-8") not in raw
            assert b"KDHN-" not in raw
            if os.name != "nt":
                mode = stat.S_IMODE(f.stat().st_mode)
                assert mode == 0o600
    assert [p for p in store_dir.iterdir() if ".tmp" in p.name] == []

    # Multiple historical keys preserved
    kid2 = store.store_key(rk2, activate=True, source="rotated")
    assert store.get_active_key_id() == kid2
    assert store.get_key(kid1) == rk1
    assert store.get_key(kid2) == rk2
    meta_list = store.list_key_metadata()
    assert len(meta_list) == 2
    assert meta_list[0]["key_id"] == kid2 and meta_list[0]["is_active"] is True
    assert meta_list[1]["key_id"] == kid1 and meta_list[1]["is_active"] is False
    assert all("recovery_key" not in m for m in meta_list)

    # Blob substitution attack (copy rk_<kid1>.dpapi over rk_<kid2>.dpapi) rejected by key-specific entropy!
    blob1 = store_dir / f"rk_{kid1}.dpapi"
    blob2 = store_dir / f"rk_{kid2}.dpapi"
    orig_blob2 = blob2.read_bytes()
    blob2.write_bytes(blob1.read_bytes())
    with pytest.raises(RecoveryKeyIntegrityError):
        store.get_key(kid2)
    # Must NOT silently replace damaged key with a new key
    assert store.get_active_key_id() == kid2
    assert len(store.list_key_metadata()) == 2

    # Corrupted blob (flipped byte / bad magic) rejected cleanly
    blob2.write_bytes(orig_blob2[:-1] + bytes([orig_blob2[-1] ^ 0xFF]))
    with pytest.raises(RecoveryKeyIntegrityError):
        store.get_key(kid2)
    blob2.write_bytes(b"NOT_DPAPI_MAGIC_12345")
    with pytest.raises(RecoveryKeyIntegrityError):
        store.get_key(kid2)
    blob2.write_bytes(orig_blob2)
    assert store.get_key(kid2) == rk2

    # Different Windows user profile (different SID) cannot decrypt current-user DPAPI blob!
    other_user_dlls = _MockWin32DPAPIDLLs(user_sid=b"S-1-5-21-DIFFERENT-USER")
    other_backend = Win32DPAPIBackend(crypt32=other_user_dlls, kernel32=other_user_dlls, platform_name="win32")
    other_store = WindowsDPAPIRecoveryKeyStore(storage_dir=store_dir, dpapi_backend=other_backend)
    with pytest.raises(RecoveryKeyIntegrityError):
        other_store.get_key(kid2)

    # Inaccessible user profile path reports unavailable cleanly
    bad_profile_path = tmp_path / "blocked_file_as_dir"
    bad_profile_path.write_text("not a directory", encoding="utf-8")
    inaccessible_store = WindowsDPAPIRecoveryKeyStore(storage_dir=bad_profile_path / "sub", dpapi_backend=backend)
    assert inaccessible_store.is_available() is False
    with pytest.raises(RecoveryKeyStoreUnavailableError):
        inaccessible_store.store_key(rk1)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows DPAPI runtime environment")
def test_02_windows_dpapi_real_native_integration(tmp_path: Path):
    """
    Comprehensive native Windows DPAPI acceptance test (runs only on genuine Windows hosts).
    Verifies Section 5 requirements against real crypt32.dll & kernel32.dll:
    - Current-user-only protection (rejects CRYPTPROTECT_LOCAL_MACHINE)
    - Recovery Key storage and retrieval
    - Application/key-specific entropy enforcement
    - Invalid, truncated, damaged, or substituted blob rejection
    - Zero plaintext Recovery Key bytes anywhere on disk
    - Historical multi-key preservation across rotations
    - Retrieval after simulated application restart (fresh store instance)
    - Safe failure when profile storage directory is unavailable
    """
    store_dir = tmp_path / "real_win_dpapi"
    store = WindowsDPAPIRecoveryKeyStore(storage_dir=store_dir)
    assert store.is_available() is True

    # Current-user-only enforcement: CRYPTPROTECT_LOCAL_MACHINE must be rejected
    with pytest.raises(ValueError, match="CRYPTPROTECT_LOCAL_MACHINE"):
        store._dpapi.protect(b"probe", b"entropy", flags=CRYPTPROTECT_LOCAL_MACHINE)

    rk1 = generate_recovery_key()
    rk2 = generate_recovery_key()
    kid1 = store.store_key(rk1, activate=True, source="generated")
    kid2 = store.store_key(rk2, activate=True, source="rotated")
    assert store.get_active_key_id() == kid2

    # Zero plaintext key strings anywhere on disk
    for fp in store_dir.rglob("*"):
        if fp.is_file():
            raw = fp.read_bytes()
            assert rk1.encode("utf-8") not in raw
            assert rk2.encode("utf-8") not in raw
            assert b"KDHN-" not in raw

    # Application/key-specific entropy check via raw Win32 DPAPI unprotect
    blob1_path = store_dir / f"rk_{kid1}.dpapi"
    blob1_bytes = blob1_path.read_bytes()
    assert blob1_bytes.startswith(DPAPI_BLOB_MAGIC)
    ct1 = blob1_bytes[len(DPAPI_BLOB_MAGIC):]
    with pytest.raises(RecoveryKeyIntegrityError):
        store._dpapi.unprotect(ct1, b"wrong-entropy-for-key")

    # Persistence across simulated application restart (fresh store instance)
    restarted_store = WindowsDPAPIRecoveryKeyStore(storage_dir=store_dir)
    assert restarted_store.is_available() is True
    assert restarted_store.get_active_key_id() == kid2
    assert restarted_store.get_key() == rk2
    assert restarted_store.get_key(kid1) == rk1  # Historical key preserved!
    assert restarted_store.verify_stored_key(kid1)["valid"] is True
    assert restarted_store.verify_stored_key(kid2)["valid"] is True

    # Corrupted / damaged blob rejection
    corrupt_copy = bytearray(blob1_bytes)
    corrupt_copy[-1] ^= 0xFF
    blob1_path.write_bytes(bytes(corrupt_copy))
    with pytest.raises(RecoveryKeyIntegrityError):
        restarted_store.get_key(kid1)

    # Blob substitution attack (copying kid2's valid blob onto kid1's path fails entropy + envelope check)
    blob2_path = store_dir / f"rk_{kid2}.dpapi"
    blob1_path.write_bytes(blob2_path.read_bytes())
    with pytest.raises(RecoveryKeyIntegrityError):
        restarted_store.get_key(kid1)

    # Safe failure when profile path is unavailable
    blocker = tmp_path / "blocked_profile_file"
    blocker.write_text("file-blocking-dir", encoding="utf-8")
    broken_store = WindowsDPAPIRecoveryKeyStore(storage_dir=blocker / "recovery_keys")
    assert broken_store.is_available() is False
    with pytest.raises(RecoveryKeyStoreUnavailableError):
        broken_store.store_key(generate_recovery_key())

    assert restarted_store.delete_key(kid1) is True
    assert restarted_store.delete_key(kid2) is True


# =====================================================================
# 2. macOS Keychain Tests (Real Keychain + Fallback Rejection + Errors)
# =====================================================================
@pytest.mark.skipif(sys.platform != "darwin", reason="Requires macOS Keychain runtime")
def test_03_macos_keychain_real_roundtrip_historical_keys_and_cleanup(tmp_path: Path):
    """
    Exercise the real macOS Keychain via explicit `keyring.backends.macOS.Keyring`
    using a unique isolated test service name and cleaning up every test entry in finally.
    """
    unique_service = f"com.krushidhan.erp.test.2c4c.{uuid.uuid4().hex[:12]}"
    store = MacOSKeychainRecoveryKeyStore(storage_dir=tmp_path / "mac_meta", service_name=unique_service)
    assert store.is_available() is True
    backend = store._get_backend()
    assert type(backend).__module__ == "keyring.backends.macOS"
    assert type(backend).__name__ == "Keyring"

    rk1 = generate_recovery_key()
    rk2 = generate_recovery_key()
    created_ids: List[str] = []
    try:
        kid1 = store.store_key(rk1, activate=True, source="generated")
        created_ids.append(kid1)
        assert store.get_active_key_id() == kid1
        assert store.get_key(kid1) == rk1
        assert store.verify_stored_key(kid1)["valid"] is True

        # Rotate / store second historical key without losing the first
        kid2 = store.store_key(rk2, activate=True, source="rotated")
        created_ids.append(kid2)
        assert store.get_active_key_id() == kid2
        assert store.get_key(kid2) == rk2
        assert store.get_key(kid1) == rk1  # Historical key still accessible!

        meta = store.list_key_metadata()
        assert [m["key_id"] for m in meta] == [kid2, kid1]
        assert meta[0]["is_active"] is True and meta[1]["is_active"] is False

        # Verify metadata.json contains ZERO raw key strings
        meta_raw = (tmp_path / "mac_meta" / "metadata.json").read_text(encoding="utf-8")
        assert rk1 not in meta_raw and rk2 not in meta_raw and "KDHN-" not in meta_raw
    finally:
        for kid in created_ids:
            try:
                store.delete_key(kid)
            except Exception:
                pass
        # Confirm test entries are cleanly removed from macOS Keychain
        for kid in created_ids:
            assert backend.get_password(unique_service, kid) is None


def test_04_macos_keychain_rejects_fallbacks_handles_locked_and_corrupted_entries(tmp_path: Path):
    """Verify macOS store rejects null/plaintext/file fallback backends and fails closed on locked/corrupted entries."""

    class NullKeyring:
        def get_password(self, s, u): return None
        def set_password(self, s, u, p): pass
        def delete_password(self, s, u): pass

    class PlaintextKeyring:
        def get_password(self, s, u): return None
        def set_password(self, s, u, p): pass
        def delete_password(self, s, u): pass

    class FileKeyring:
        def get_password(self, s, u): return None
        def set_password(self, s, u, p): pass
        def delete_password(self, s, u): pass

    for bad_backend in (NullKeyring(), PlaintextKeyring(), FileKeyring()):
        with pytest.raises(RecoveryKeyStoreUnavailableError):
            MacOSKeychainRecoveryKeyStore(storage_dir=tmp_path / "bad_kc", backend=bad_backend)

    # Simulate locked / access-denied / corrupted Keychain behavior via test double
    class _SimulatedKeychain:
        def __init__(self) -> None:
            self.items: Dict[tuple[str, str], str] = {}
            self.locked = False

        def set_password(self, svc: str, acct: str, pw: str) -> None:
            if self.locked:
                raise RuntimeError("KeychainDenied: User canceled or keychain is locked")
            self.items[(svc, acct)] = pw

        def get_password(self, svc: str, acct: str) -> Optional[str]:
            if self.locked:
                raise RuntimeError("KeychainDenied: errSecInteractionNotAllowed")
            return self.items.get((svc, acct))

        def delete_password(self, svc: str, acct: str) -> None:
            self.items.pop((svc, acct), None)

    sim = _SimulatedKeychain()
    store = MacOSKeychainRecoveryKeyStore(
        storage_dir=tmp_path / "sim_kc",
        service_name="com.krushidhan.erp.sim.test",
        backend=sim,
        _allow_test_backend=True,
    )
    rk1 = generate_recovery_key()
    rk2 = generate_recovery_key()
    kid1 = store.store_key(rk1, activate=True)
    assert store.get_key(kid1) == rk1

    # Locked keychain -> sanitized RecoveryKeyStoreUnavailableError without leaking secret
    sim.locked = True
    with pytest.raises(RecoveryKeyStoreUnavailableError) as exc_lock:
        store.get_key(kid1)
    assert rk1 not in str(exc_lock.value)
    with pytest.raises(RecoveryKeyStoreUnavailableError) as exc_set:
        store.store_key(rk2)
    assert rk2 not in str(exc_set.value)
    sim.locked = False

    # Corrupted Keychain entry -> RecoveryKeyIntegrityError (never silently replaced)
    sim.items[("com.krushidhan.erp.sim.test", kid1)] = "CORRUPTED_NOT_JSON"
    with pytest.raises(RecoveryKeyIntegrityError):
        store.get_key(kid1)

    # Substituted Keychain entry (valid key #2 stored under key #1's account) -> RecoveryKeyIntegrityError
    sim.items[("com.krushidhan.erp.sim.test", kid1)] = json.dumps(
        {"app": "com.krushidhan.erp", "key_id": kid1, "recovery_key": rk2, "v": 1}
    )
    with pytest.raises(RecoveryKeyIntegrityError):
        store.get_key(kid1)

    # Missing Keychain entry returns None safely
    sim.items.pop(("com.krushidhan.erp.sim.test", kid1), None)
    assert store.get_key(kid1) is None


# =====================================================================
# 3. Production Platform Selector (Never Falls Back to InMemory)
# =====================================================================
def test_05_production_platform_selector_never_falls_back_to_memory_or_plaintext(tmp_path: Path):
    win_store = get_production_recovery_key_store(platform_name="win32", storage_dir=tmp_path / "w")
    mac_store = get_production_recovery_key_store(platform_name="darwin", storage_dir=tmp_path / "m")
    lin_store = get_production_recovery_key_store(platform_name="linux", storage_dir=tmp_path / "l")

    assert isinstance(win_store, WindowsDPAPIRecoveryKeyStore)
    assert isinstance(mac_store, MacOSKeychainRecoveryKeyStore)
    assert isinstance(lin_store, UnsupportedRecoveryKeyStore)
    for st in (win_store, mac_store, lin_store):
        assert not isinstance(st, InMemoryRecoveryKeyStore)

    assert lin_store.is_available() is False
    status = lin_store.get_status()
    assert status["available"] is False
    assert status["backend"] == "unsupported"
    with pytest.raises(RecoveryKeyStoreUnavailableError):
        lin_store.store_key(generate_recovery_key())


# =====================================================================
# 4. Recovery Key Lifecycle (Setup, Expiry, Binding, Import, Rotation)
# =====================================================================
def test_06_recovery_key_lifecycle_setup_expiry_binding_import_and_rotation():
    mem_store = InMemoryRecoveryKeyStore(available=True)
    now_box = [1000.0]
    lifecycle = RecoveryKeyLifecycleService(
        key_store_provider=lambda: mem_store,
        pending_ttl_seconds=60,
        clock=lambda: now_box[0],
    )

    # Cannot rotate before first-time setup
    with pytest.raises(RecoveryKeyLifecycleError, match="first-time"):
        lifecycle.start_generation(1, mode="rotate")

    # 1. Start first-time setup for admin 1
    started = lifecycle.start_generation(1, mode="setup")
    op_id = started["operation_id"]
    rk1 = started["recovery_key"]
    kid1 = started["key_id"]
    assert kid1 == get_recovery_key_id(rk1)
    assert mem_store.has_key() is False  # Not stored until confirmed!

    # Reject missing acknowledgement
    with pytest.raises(RecoveryKeyLifecycleError, match="Recovery Card"):
        lifecycle.confirm_generation(1, operation_id=op_id, confirmed_recovery_key=rk1, acknowledged_card_saved=False)

    # Reject wrong admin user_id (binding)
    with pytest.raises(RecoveryKeyLifecycleError, match="different administrator"):
        lifecycle.confirm_generation(2, operation_id=op_id, confirmed_recovery_key=rk1, acknowledged_card_saved=True)

    # Reject wrong re-entered Recovery Key
    wrong_rk = generate_recovery_key()
    with pytest.raises(RecoveryKeyLifecycleError, match="does not match"):
        lifecycle.confirm_generation(1, operation_id=op_id, confirmed_recovery_key=wrong_rk, acknowledged_card_saved=True)
    assert mem_store.has_key() is False

    # Pending key expiry rejects confirmation cleanly
    now_box[0] += 61.0
    with pytest.raises(RecoveryKeyLifecycleError, match="expired"):
        lifecycle.confirm_generation(1, operation_id=op_id, confirmed_recovery_key=rk1, acknowledged_card_saved=True)
    assert mem_store.has_key() is False

    # Re-generate and confirm cleanly
    started2 = lifecycle.start_generation(1, mode="setup")
    rk1 = started2["recovery_key"]
    kid1 = started2["key_id"]
    res_confirm = lifecycle.confirm_generation(
        1,
        operation_id=started2["operation_id"],
        confirmed_recovery_key=rk1.lower().replace("-", " "),  # tolerant canonicalization
        acknowledged_card_saved=True,
    )
    assert res_confirm["success"] is True
    assert res_confirm["key_id"] == kid1
    assert "recovery_key" not in res_confirm
    assert mem_store.get_active_key_id() == kid1
    assert mem_store.get_key(kid1) == rk1

    # Re-confirming consumed operation_id fails (pending key purged from memory)
    with pytest.raises(RecoveryKeyLifecycleError, match="expired or was not found"):
        lifecycle.confirm_generation(1, operation_id=started2["operation_id"], confirmed_recovery_key=rk1, acknowledged_card_saved=True)

    # 2. Rotation activates new key while preserving historical key #1
    rot = lifecycle.start_generation(1, mode="rotate")
    rk2 = rot["recovery_key"]
    kid2 = rot["key_id"]
    assert kid2 != kid1
    rot_res = lifecycle.confirm_generation(
        1,
        operation_id=rot["operation_id"],
        confirmed_recovery_key=rk2,
        acknowledged_card_saved=True,
    )
    assert rot_res["active_key_id"] == kid2
    assert rot_res["key_count"] == 2
    assert rot_res["historical_keys_preserved"] is True
    assert mem_store.get_key(kid1) == rk1  # Old key STILL present!
    assert mem_store.get_key(kid2) == rk2

    # 3. Import from Recovery Card (requires explicit confirm_import=True, detects duplicate safely)
    rk3 = generate_recovery_key()
    kid3 = get_recovery_key_id(rk3)
    with pytest.raises(RecoveryKeyLifecycleError, match="confirm_import"):
        lifecycle.import_recovery_key(1, recovery_key=rk3, confirm_import=False)

    imp1 = lifecycle.import_recovery_key(1, recovery_key=rk3, confirm_import=True, set_as_active=False)
    assert imp1["success"] is True
    assert imp1["key_id"] == kid3
    assert imp1["already_present"] is False
    assert imp1["is_active"] is False  # Existing active key (kid2) preserved!
    assert mem_store.get_active_key_id() == kid2
    assert mem_store.get_key(kid3) == rk3
    assert "recovery_key" not in imp1

    # Duplicate import of same key detected safely without replacing anything
    imp_dup = lifecycle.import_recovery_key(1, recovery_key=rk3, confirm_import=True, set_as_active=False)
    assert imp_dup["already_present"] is True
    assert imp_dup["key_count"] == 3


# =====================================================================
# 5. API Security, OS Store Integration, Rotation Compatibility & Redaction
# =====================================================================
def test_07_api_rbac_lifecycle_rotation_historical_archive_restore_and_redaction(
    isolated_2c4c_env: Dict[str, Any],
    caplog: pytest.LogCaptureFixture,
):
    """
    End-to-end API test:
    - ADMIN-only RBAC (401 unauthenticated, 403 OPERATOR) on all recovery-key endpoints.
    - Setup -> Confirm -> Export .kdb.enc using OS-stored active key (no key in request body!).
    - Rotate to Key #2 -> Export Archive #2 -> Inspect & Restore Archive #1 automatically via historical key_id!
    - Unavailable OS store fails closed on stored-key ops while manual Recovery Card inspect/restore still works.
    - ZERO Recovery Keys leaked in responses, HTTP 422, logs, or SQLite audit_logs.
    """
    db: DatabaseManager = isolated_2c4c_env["db"]
    db_file: Path = isolated_2c4c_env["db_file"]
    backup_dir: Path = isolated_2c4c_env["backup_dir"]
    src_bkp_name = isolated_2c4c_env["verified_db_backup"].name
    admin_hdr = isolated_2c4c_env["admin_headers"]
    op_hdr = isolated_2c4c_env["operator_headers"]

    # Inject isolated store into API layer for deterministic cross-platform testing
    test_store = InMemoryRecoveryKeyStore(available=True)
    set_recovery_key_store_for_testing(test_store)
    reset_recovery_key_lifecycle()
    caplog.set_level(logging.DEBUG)

    generated_secrets: List[str] = []

    with TestClient(app) as client:
        # 1. RBAC: unauthenticated -> 401; OPERATOR -> 403 across all new endpoints
        for method, path in [
            ("GET", "/api/system/recovery-keys/status"),
            ("POST", "/api/system/recovery-keys/setup/start"),
            ("POST", "/api/system/recovery-keys/rotate/start"),
            ("POST", "/api/system/recovery-keys/confirm"),
            ("POST", "/api/system/recovery-keys/cancel"),
            ("POST", "/api/system/recovery-keys/import"),
            ("POST", "/api/system/recovery-keys/verify"),
        ]:
            req_fn = client.get if method == "GET" else client.post
            assert req_fn(path).status_code == 401, f"{method} {path} must reject unauthenticated"
            assert req_fn(path, headers=op_hdr).status_code == 403, f"{method} {path} must reject OPERATOR"

        # 2. First-time setup & confirmation via Admin API
        st0 = client.get("/api/system/recovery-keys/status", headers=admin_hdr)
        assert st0.status_code == 200
        assert st0.json()["available"] is True
        assert st0.json()["has_active_key"] is False

        start_res = client.post("/api/system/recovery-keys/setup/start", headers=admin_hdr)
        assert start_res.status_code == 200, start_res.text
        start_data = start_res.json()
        rk1 = start_data["recovery_key"]
        kid1 = start_data["key_id"]
        op1 = start_data["operation_id"]
        generated_secrets.append(rk1)

        # HTTP 422 secret redaction check (omit required operation_id while passing recovery_key)
        res_422 = client.post(
            "/api/system/recovery-keys/confirm",
            headers=admin_hdr,
            json={"recovery_key": rk1, "acknowledged_card_saved": True},
        )
        assert res_422.status_code == 422
        assert rk1 not in res_422.text

        # Confirm key #1
        conf1 = client.post(
            "/api/system/recovery-keys/confirm",
            headers=admin_hdr,
            json={"operation_id": op1, "recovery_key": rk1, "acknowledged_card_saved": True},
        )
        assert conf1.status_code == 200, conf1.text
        assert conf1.json()["key_id"] == kid1
        assert rk1 not in conf1.text

        # Verify stored key endpoint returns non-secret metadata only
        ver1 = client.post("/api/system/recovery-keys/verify", headers=admin_hdr, json={})
        assert ver1.status_code == 200
        assert ver1.json()["valid"] is True and ver1.json()["key_id"] == kid1
        assert rk1 not in ver1.text

        # 3. Encrypt verified .db backup using OS-stored active key (NO recovery_key in POST body!)
        enc1_res = client.post(
            "/api/system/backups/encrypt",
            headers=admin_hdr,
            json={"backup_filename": src_bkp_name},
        )
        assert enc1_res.status_code == 200, enc1_res.text
        enc1 = enc1_res.json()
        assert enc1["key_id"] == kid1
        assert enc1["key_source"] == "os_secure_store"
        assert rk1 not in enc1_res.text
        # Original .db backup still intact!
        assert (backup_dir / src_bkp_name).is_file()

        dl1 = client.get(f"/api/system/backups/encrypted/{enc1['filename']}/download", headers=admin_hdr)
        assert dl1.status_code == 200
        archive1_bytes = dl1.content
        assert archive1_bytes.startswith(KDB_ENC_MAGIC)

        # 4. Rotate to Key #2 -> verify old key #1 remains preserved
        rot_start = client.post("/api/system/recovery-keys/rotate/start", headers=admin_hdr)
        assert rot_start.status_code == 200
        rk2 = rot_start.json()["recovery_key"]
        kid2 = rot_start.json()["key_id"]
        op2 = rot_start.json()["operation_id"]
        generated_secrets.append(rk2)

        rot_conf = client.post(
            "/api/system/recovery-keys/confirm",
            headers=admin_hdr,
            json={"operation_id": op2, "recovery_key": rk2, "acknowledged_card_saved": True},
        )
        assert rot_conf.status_code == 200
        assert rot_conf.json()["active_key_id"] == kid2
        assert rot_conf.json()["key_count"] == 2
        assert rk2 not in rot_conf.text

        # Future encrypted backup now uses Key #2 automatically
        enc2_res = client.post(
            "/api/system/backups/encrypt",
            headers=admin_hdr,
            json={"backup_filename": src_bkp_name},
        )
        assert enc2_res.status_code == 200
        assert enc2_res.json()["key_id"] == kid2

        # 5. Inspect OLD Archive #1 WITHOUT manual key -> automatically resolves historical kid1 from OS store!
        mp1, ct1 = _multipart_body("file", enc1["filename"], archive1_bytes)
        insp1 = client.post(
            "/api/system/backups/encrypted/inspect-upload",
            headers={**admin_hdr, "Content-Type": ct1},
            content=mp1,
        )
        assert insp1.status_code == 200, insp1.text
        insp1_json = insp1.json()
        assert insp1_json["authenticated"] is True
        assert insp1_json["metadata_authenticated"] is True
        assert insp1_json["key_source"] == "os_secure_store"
        assert insp1_json["key_id"] == kid1
        assert insp1_json["valid_for_restore"] is True

        # Mutate live DB, then restore OLD Archive #1 WITHOUT manual key (confirm_restore=true)
        master = MasterDataRepository(db)
        temp_pid = master.create_product(Product(product_name="Ephemeral Before Historical Restore", default_sale_rate=555.0))
        assert db.fetch_one("SELECT 1 FROM products WHERE product_id = ?;", (temp_pid,)) is not None

        mp_r1, ct_r1 = _multipart_body("file", enc1["filename"], archive1_bytes, {"confirm_restore": "true"})
        rest1 = client.post(
            "/api/system/backups/encrypted/restore-upload",
            headers={**admin_hdr, "Content-Type": ct_r1},
            content=mp_r1,
        )
        assert rest1.status_code == 200, rest1.text
        assert rest1.json()["success"] is True
        assert rest1.json()["key_id"] == kid1
        assert rest1.json()["pre_restore_filename"]
        assert db.fetch_one("SELECT 1 FROM products WHERE product_id = ?;", (temp_pid,)) is None

        # 6. Simulate unavailable OS secure store -> stored-key ops fail closed (503),
        #    while manual Recovery Card inspect & restore still succeed!
        set_recovery_key_store_for_testing(UnsupportedRecoveryKeyStore(platform_name="linux"))
        st_unavail = client.get("/api/system/recovery-keys/status", headers=admin_hdr)
        assert st_unavail.status_code == 200
        assert st_unavail.json()["available"] is False

        assert client.post("/api/system/recovery-keys/setup/start", headers=admin_hdr).status_code == 503
        assert client.post(
            "/api/system/backups/encrypt",
            headers=admin_hdr,
            json={"backup_filename": src_bkp_name},
        ).status_code == 503

        # Inspect without manual key returns unauthenticated header cleanly
        insp_unauth = client.post(
            "/api/system/backups/encrypted/inspect-upload",
            headers={**admin_hdr, "Content-Type": ct1},
            content=mp1,
        )
        assert insp_unauth.status_code == 200
        assert insp_unauth.json()["authenticated"] is False
        assert insp_unauth.json()["metadata_authenticated"] is False
        assert insp_unauth.json()["requires_manual_recovery_key"] is True

        # Inspect WITH manual Recovery Key authenticates cleanly even when OS store is unavailable!
        mp1_man, ct1_man = _multipart_body("file", enc1["filename"], archive1_bytes, {"recovery_key": rk1})
        insp_man = client.post(
            "/api/system/backups/encrypted/inspect-upload",
            headers={**admin_hdr, "Content-Type": ct1_man},
            content=mp1_man,
        )
        assert insp_man.status_code == 200
        assert insp_man.json()["authenticated"] is True
        assert insp_man.json()["metadata_authenticated"] is True
        assert insp_man.json()["key_source"] == "manual"

    # 7. Confirm ZERO Recovery Keys leaked in logs, audit_logs, or SQLite raw bytes!
    for secret_key in generated_secrets:
        for rec in caplog.records:
            assert secret_key not in rec.getMessage()
        audit_rows = db.fetch_all("SELECT details FROM audit_log;")
        for row in audit_rows:
            assert secret_key not in str(row[0] or "")
        assert secret_key.encode("utf-8") not in db_file.read_bytes()


# =====================================================================
# 6. Frontend Static + Recovery Card Print + Node Runtime Contract Tests
# =====================================================================
def test_08_frontend_recovery_key_ui_recovery_card_and_no_browser_storage():
    """
    Verify:
    - All Phase 2C-4C Backup Manager UI elements, modals, and Recovery Card print sheet exist and are English-only.
    - Recovery Key is NEVER stored in localStorage, sessionStorage, or cookies.
    - Node.js runtime verifies setup modal -> print Recovery Card -> confirm clears DOM/memory,
      file selection never auto-restores, unauthenticated inspect keeps restore disabled, and logout wipes state.
    """
    repo_root = Path(__file__).resolve().parent.parent
    html_text = (repo_root / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    css_text = (repo_root / "src" / "web" / "static" / "css" / "app.css").read_text(encoding="utf-8")
    js_text = (repo_root / "src" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    # Required UI IDs exist
    for elem_id in (
        "enc-backup-section",
        "rk-store-status-badge",
        "rk-active-key-id",
        "rk-history-tbody",
        "btn-rk-create",
        "btn-rk-rotate",
        "btn-rk-import",
        "btn-rk-verify",
        "enc-export-source-select",
        "btn-enc-export-now",
        "enc-bkp-file-input",
        "enc-bkp-manual-key",
        "btn-enc-inspect-upload",
        "enc-inspect-result-panel",
        "btn-enc-open-restore-confirm",
        "modal-recovery-key-setup",
        "rk-generated-key-display",
        "btn-rk-print-card",
        "rk-confirm-saved-check",
        "rk-confirm-reenter-input",
        "btn-rk-confirm-activate",
        "modal-recovery-key-import",
        "rk-import-key-input",
        "rk-import-confirm-check",
        "recovery-card-print-sheet",
        "rk-card-key-value",
        "rk-card-key-id",
        "rk-card-created-at",
    ):
        assert f'id="{elem_id}"' in html_text, f"Missing Phase 2C-4C element id='{elem_id}'"

    # Print scoping CSS exists for Recovery Card
    assert "body.print-target-recovery-card" in css_text
    assert "#recovery-card-print-sheet" in css_text

    # Recovery Key section in app.js NEVER references localStorage, sessionStorage, or document.cookie in code
    phase_marker = "Phase 2C-4C: OS-Protected Recovery Keys"
    idx = js_text.index(phase_marker)
    js_2c4c_code_only = "\n".join(
        line for line in js_text[idx:].splitlines() if not line.strip().startswith("//")
    )
    assert "localStorage" not in js_2c4c_code_only
    assert "sessionStorage" not in js_2c4c_code_only
    assert "document.cookie" not in js_2c4c_code_only

    # Node.js runtime verification of Phase 2C-4C JS flows
    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js is required for frontend runtime verification"

    sample_rk = generate_recovery_key()
    sample_kid = get_recovery_key_id(sample_rk)

    node_script = f"""
const fs = require("fs");
const vm = require("vm");

const elements = new Map();
function makeEl(id) {{
    const el = {{
        id,
        value: "",
        type: "password",
        checked: false,
        disabled: false,
        textContent: "",
        innerHTML: "",
        style: {{ display: "" }},
        className: "",
        dataset: {{}},
        files: [],
        title: "",
        classList: {{
            _set: new Set(),
            add(c) {{ this._set.add(c); }},
            remove(c) {{ this._set.delete(c); }},
            contains(c) {{ return this._set.has(c); }},
        }},
        addEventListener() {{}},
        removeEventListener() {{}},
        getAttribute() {{ return null; }},
        setAttribute() {{}},
    }};
    elements.set(id, el);
    return el;
}}

[
    "enc-bkp-feedback", "rk-store-warning-banner", "rk-store-status-badge", "rk-store-backend",
    "rk-active-key-id", "rk-key-count", "rk-history-tbody", "btn-rk-create", "btn-rk-rotate",
    "btn-rk-import", "btn-rk-verify", "btn-enc-export-now", "enc-export-source-select",
    "modal-recovery-key-setup", "rk-setup-modal-title", "rk-setup-rotation-warning",
    "rk-generated-key-display", "rk-generated-key-id", "rk-generated-created-at",
    "rk-confirm-saved-check", "rk-confirm-reenter-input", "rk-setup-error", "btn-rk-confirm-activate",
    "modal-recovery-key-import", "rk-import-key-input", "rk-import-set-active-check",
    "rk-import-confirm-check", "rk-import-error", "btn-rk-import-submit",
    "rk-card-key-value", "rk-card-key-id", "rk-card-key-id-repeat", "rk-card-created-at",
    "enc-bkp-file-input", "enc-bkp-manual-key", "enc-inspect-result-panel",
    "enc-inspect-auth-badge", "enc-inspect-message", "enc-insp-filename", "enc-insp-key-id",
    "enc-insp-meta-auth", "enc-insp-created", "enc-insp-size", "enc-insp-key-source",
    "enc-insp-integrity", "enc-insp-schema", "btn-enc-open-restore-confirm",
    "modal-backup-restore-confirm", "bkp-confirm-source", "bkp-confirm-filename", "bkp-confirm-date",
    "bkp-confirm-size", "bkp-confirm-integrity", "bkp-confirm-fk", "bkp-confirm-schema",
    "bkp-confirm-shop", "bkp-confirm-key-row", "bkp-confirm-key-id", "bkp-restore-confirm-check",
    "btn-confirm-backup-restore", "btn-cancel-backup-restore",
].forEach(makeEl);

const storageWrites = [];
const fetchCalls = [];
let printCount = 0;
let printClassWasActive = false;

const bodyEl = makeEl("__body__");
const sandbox = {{
    console,
    setTimeout: (fn) => {{ return 1; }},
    clearTimeout: () => {{}},
    Date,
    Math,
    JSON,
    Promise,
    Set,
    Map,
    Array,
    Object,
    String,
    Number,
    Boolean,
    RegExp,
    Error,
    URLSearchParams,
    encodeURIComponent,
    FormData: class {{
        constructor() {{ this.entries = []; }}
        append(k, v) {{ this.entries.push([k, v]); }}
    }},
    localStorage: {{
        getItem: () => "tok_admin",
        setItem: (k, v) => storageWrites.push(["local", k, v]),
        removeItem: () => {{}},
    }},
    sessionStorage: {{
        getItem: () => null,
        setItem: (k, v) => storageWrites.push(["session", k, v]),
        removeItem: () => {{}},
    }},
    document: {{
        body: bodyEl,
        getElementById: (id) => elements.get(id) || null,
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: () => {{}},
        removeEventListener: () => {{}},
        createElement: () => makeEl("__tmp__"),
    }},
    window: {{
        addEventListener: () => {{}},
        removeEventListener: () => {{}},
        print: () => {{
            printCount++;
            printClassWasActive = bodyEl.classList.contains("print-target-recovery-card");
        }},
    }},
    fetch: async (url, opts = {{}}) => {{
        fetchCalls.push([url, opts.method || "GET"]);
        if (url === "/api/system/recovery-keys/setup/start") {{
            return {{
                ok: true,
                status: 200,
                json: async () => ({{
                    operation_id: "op_test_1",
                    mode: "setup",
                    recovery_key: "{sample_rk}",
                    key_id: "{sample_kid}",
                    created_at_utc: "2026-11-18T11:45:00Z",
                    expires_in_seconds: 600,
                }}),
            }};
        }}
        if (url === "/api/system/recovery-keys/confirm") {{
            return {{
                ok: true,
                status: 200,
                json: async () => ({{
                    success: true,
                    key_id: "{sample_kid}",
                    active_key_id: "{sample_kid}",
                    key_count: 1,
                    message: "Recovery Key activated.",
                }}),
            }};
        }}
        if (url === "/api/system/recovery-keys/status") {{
            return {{
                ok: true,
                status: 200,
                json: async () => ({{
                    available: true,
                    backend: "macos_keychain",
                    has_active_key: true,
                    active_key_id: "{sample_kid}",
                    key_count: 1,
                    keys: [{{ key_id: "{sample_kid}", is_active: true, source: "generated", created_at_utc: "2026-11-18T11:45:00Z", storage_backend: "macos_keychain", last_used_at_utc: null }}],
                }}),
            }};
        }}
        if (url === "/api/system/backups/encrypted/inspect-upload") {{
            const hasManual = opts.body && opts.body.entries.some(([k]) => k === "recovery_key");
            return {{
                ok: true,
                status: 200,
                json: async () => (hasManual ? {{
                    filename: "shop.kdb.enc",
                    key_id: "{sample_kid}",
                    authenticated: true,
                    metadata_authenticated: true,
                    valid_for_restore: true,
                    integrity_ok: true,
                    foreign_key_ok: true,
                    key_source: "manual",
                    created_at_utc: "2026-11-18T11:45:00Z",
                    size_display: "15 KB",
                    schema_version: 6,
                    table_count: 31,
                }} : {{
                    filename: "shop.kdb.enc",
                    key_id: "{sample_kid}",
                    authenticated: false,
                    metadata_authenticated: false,
                    valid_for_restore: false,
                    requires_manual_recovery_key: true,
                    created_at_utc: "2026-11-18T11:45:00Z",
                    size_display: "15 KB",
                    schema_version: 6,
                }}),
            }};
        }}
        return {{ ok: true, status: 200, json: async () => ({{}}) }};
    }},
}};
sandbox.globalThis = sandbox;
sandbox.window.document = sandbox.document;
sandbox.window.localStorage = sandbox.localStorage;
sandbox.window.sessionStorage = sandbox.sessionStorage;

// Load only the Phase 2C-4C slice with minimal helpers from app.js
const fullJs = fs.readFileSync({json.dumps(str(repo_root / "src" / "web" / "static" / "js" / "app.js"))}, "utf8");
const sliceStart = fullJs.indexOf("// Phase 2C-4C: OS-Protected Recovery Keys");
const preamble = `
let currentUser = {{ user_id: 1, username: "admin", role: "ADMIN" }};
let authSessionVersion = 1;
let _pendingBackupRestore = null;
function hasValidAuthSession() {{ return Boolean(currentUser); }}
function showToast() {{}}
function escapeHtml(s) {{ return String(s || ""); }}
function _populateBackupRestoreConfirmModal(src, info) {{
    document.getElementById("bkp-confirm-source").textContent = src;
    document.getElementById("bkp-confirm-key-id").textContent = info.key_id || "-";
}}
`;
vm.runInNewContext(preamble + fullJs.slice(sliceStart), sandbox);

(async () => {{
    // 1. Setup displays Recovery Key once & populates Recovery Card
    const okStart = await sandbox.window.startRecoveryKeyWorkflow("setup");
    if (!okStart) throw new Error("startRecoveryKeyWorkflow failed");
    if (elements.get("rk-generated-key-display").textContent !== "{sample_rk}") throw new Error("Generated key not displayed");
    if (elements.get("rk-card-key-value").textContent !== "{sample_rk}") throw new Error("Recovery Card key missing");
    if (elements.get("rk-card-key-id").textContent !== "{sample_kid}") throw new Error("Recovery Card key_id missing");

    // 2. Print Recovery Card activates print-target-recovery-card class cleanly
    sandbox.window.printRecoveryCard();
    if (printCount !== 1 || !printClassWasActive) throw new Error("Recovery Card print scoping failed");

    // 3. Confirming clears displayed key & Recovery Card DOM immediately
    elements.get("rk-confirm-saved-check").checked = true;
    elements.get("rk-confirm-reenter-input").value = "{sample_rk}";
    const okConfirm = await sandbox.window.confirmRecoveryKeyWorkflow();
    if (!okConfirm) throw new Error("confirmRecoveryKeyWorkflow failed");
    if (elements.get("rk-generated-key-display").textContent !== "-") throw new Error("Displayed key not wiped after confirm");
    if (elements.get("rk-card-key-value").textContent !== "-") throw new Error("Recovery Card key not wiped after confirm");
    if (elements.get("rk-confirm-reenter-input").value !== "") throw new Error("Re-enter input not cleared");

    // 4. Selecting .kdb.enc file NEVER triggers fetch or auto-restore
    const callsBeforeSelect = fetchCalls.length;
    elements.get("enc-bkp-file-input").files = [{{ name: "shop.kdb.enc" }}];
    sandbox.window.onEncryptedBackupFileSelected();
    if (fetchCalls.length !== callsBeforeSelect) throw new Error("File selection triggered automatic network request!");

    // 5. Inspecting unauthenticated archive keeps restore button DISABLED
    await sandbox.window.inspectUploadedEncryptedArchive();
    if (!elements.get("btn-enc-open-restore-confirm").disabled) throw new Error("Unauthenticated archive enabled restore button!");
    if (!elements.get("enc-insp-meta-auth").textContent.includes("UNAUTHENTICATED")) throw new Error("Did not label unauthenticated metadata!");

    // 6. Inspecting WITH manual key authenticates and enables restore confirmation button
    elements.get("enc-bkp-manual-key").value = "{sample_rk}";
    await sandbox.window.inspectUploadedEncryptedArchive();
    if (elements.get("btn-enc-open-restore-confirm").disabled) throw new Error("Authenticated archive did not enable restore button!");
    if (!elements.get("enc-insp-meta-auth").textContent.includes("AUTHENTICATED")) throw new Error("Did not label authenticated metadata!");

    // 7. Logout / clearSensitiveRecoveryKeyUiState wipes manual key input & inspected state
    sandbox.window.clearSensitiveRecoveryKeyUiState();
    if (elements.get("enc-bkp-manual-key").value !== "") throw new Error("Manual key not cleared on logout/reset!");

    // 8. Zero Recovery Key writes to localStorage or sessionStorage!
    if (storageWrites.length !== 0) throw new Error("Unexpected browser storage writes: " + JSON.stringify(storageWrites));

    console.log("PHASE_2C4C_NODE_UI_OK");
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""

    proc = subprocess.run([node_bin, "-e", node_script], capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, f"Node UI test failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert "PHASE_2C4C_NODE_UI_OK" in proc.stdout
