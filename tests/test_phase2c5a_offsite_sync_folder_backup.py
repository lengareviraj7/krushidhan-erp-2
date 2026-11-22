"""
Isolated Security, Provider, Scheduler, API & Replacement-Computer Recovery Verification Suite
for Phase 2C-5A: Encrypted Off-Site Backup Through Cloud-Synchronized Folders.

Strictly uses isolated temporary directories, isolated SQLite databases, and isolated simulated
synchronization folders. Never touches live data/agri_erp.db or repository backups/.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, get_db_manager, reset_db_manager
from src.models.accounting import Expense
from src.models.master_data import Customer, Product, Supplier
from src.models.purchase import Purchase, PurchaseItem
from src.models.sales import Sale, SaleItem
from src.repositories.master_data_repository import MasterDataRepository
from src.services.accounting_service import AccountingService
from src.services.auth_service import AuthService
from src.services.backup_scheduler import BackupScheduler, stop_backup_scheduler
from src.services.backup_service import BackupService
from src.services.offsite_backup_service import (
    CLOUD_SYNC_DISCLAIMER,
    OffsiteBackupManager,
    OffsiteDestinationUnavailableError,
    OffsiteStatus,
    SyncFolderOffsiteBackupProvider,
)
from src.services.os_recovery_key_store import (
    UnsupportedRecoveryKeyStore,
    reset_recovery_key_store,
    set_recovery_key_store_for_testing,
)
from src.services.purchase_service import PurchaseService
from src.services.recovery_key import (
    InMemoryRecoveryKeyStore,
    generate_recovery_key,
    get_recovery_key_id,
)
from src.services.recovery_key_lifecycle import reset_recovery_key_lifecycle
from src.services.sales_service import SalesService


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(65536)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


@pytest.fixture()
def isolated_2c5a_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "shop_2c5a.db"
    backup_dir = tmp_path / "backups_2c5a"
    sync_dir = tmp_path / "sim_cloud_sync_folder"
    backup_dir.mkdir(parents=True, exist_ok=True)
    sync_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("JWT_SECRET", "phase2c5a_test_secret_material_" + secrets.token_hex(16))

    stop_backup_scheduler(timeout=2.0)
    reset_db_manager()
    reset_recovery_key_store()
    reset_recovery_key_lifecycle()

    db = get_db_manager()
    db.initialize_database(include_seed=True)

    auth = AuthService(db)
    admin_row = db.fetch_one("SELECT user_id, username, role, full_name FROM users WHERE role = 'ADMIN' LIMIT 1;")
    assert admin_row is not None
    admin_uid = int(admin_row[0])
    admin_token = auth.create_access_token(admin_uid, admin_row[1], admin_row[3], "ADMIN")

    op_row = db.fetch_one("SELECT user_id, username, role, full_name FROM users WHERE role = 'OPERATOR' LIMIT 1;")
    if op_row is None:
        pw_hash = auth.hash_password("OperatorPass@123")
        op_id = db.execute_query(
            "INSERT INTO users (username, password_hash, full_name, role, is_active) VALUES (?, ?, ?, 'OPERATOR', 1);",
            ("op_2c5a", pw_hash, "Operator 2C5A"),
        )
        op_token = auth.create_access_token(int(op_id), "op_2c5a", "Operator 2C5A", "OPERATOR")
    else:
        op_token = auth.create_access_token(int(op_row[0]), op_row[1], op_row[3], "OPERATOR")

    key_store = InMemoryRecoveryKeyStore(available=True)
    rk = generate_recovery_key()
    kid = key_store.store_key(rk, activate=True, source="generated")
    set_recovery_key_store_for_testing(key_store)

    bkp_svc = BackupService(db, backup_dir=backup_dir)
    verified_bkp = bkp_svc.create_backup(backup_type="MANUAL")

    yield {
        "tmp_path": tmp_path,
        "db": db,
        "db_file": db_file,
        "backup_dir": backup_dir,
        "sync_dir": sync_dir,
        "admin_headers": {"Authorization": f"Bearer {admin_token}"},
        "operator_headers": {"Authorization": f"Bearer {op_token}"},
        "key_store": key_store,
        "rk": rk,
        "kid": kid,
        "verified_bkp": verified_bkp,
    }

    stop_backup_scheduler(timeout=2.0)
    reset_recovery_key_lifecycle()
    reset_recovery_key_store()
    reset_db_manager()


# =====================================================================
# 1. Folder Validation, Missing/Read-Only Rejection, Plaintext Block,
#    Exact SHA-256, No Overwrite & Interrupted Copy Cleanup
# =====================================================================
def test_01_folder_validation_no_plaintext_exact_sha256_no_overwrite_and_cleanup(
    isolated_2c5a_env: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    db = isolated_2c5a_env["db"]
    backup_dir = isolated_2c5a_env["backup_dir"]
    sync_dir = isolated_2c5a_env["sync_dir"]
    tmp_path = isolated_2c5a_env["tmp_path"]
    verified_bkp = isolated_2c5a_env["verified_bkp"]
    rk = isolated_2c5a_env["rk"]

    mgr = OffsiteBackupManager(db, backup_dir=backup_dir)

    # 1. Validation checks: valid dir passes; missing, relative, file, and live DB dir rejected
    ok_val = mgr.validate_folder(str(sync_dir))
    assert ok_val["available"] is True and ok_val["writable"] is True

    assert mgr.validate_folder(str(tmp_path / "does_not_exist"))["available"] is False
    assert mgr.validate_folder("relative/folder/path")["available"] is False

    plain_file = tmp_path / "not_a_dir.txt"
    plain_file.write_text("hi", encoding="utf-8")
    assert mgr.validate_folder(str(plain_file))["available"] is False
    assert mgr.validate_folder(str(isolated_2c5a_env["db_file"].parent))["available"] is False

    # Read-only folder rejected safely on POSIX
    if os.name != "nt" and os.geteuid() != 0:
        ro_dir = tmp_path / "readonly_sync_dir"
        ro_dir.mkdir()
        os.chmod(ro_dir, stat.S_IRUSR | stat.S_IXUSR)
        try:
            ro_val = mgr.validate_folder(str(ro_dir))
            assert ro_val["available"] is False and ro_val["writable"] is False
        finally:
            os.chmod(ro_dir, stat.S_IRWXU)

    # 2. Provider explicitly refuses plaintext .db files
    provider = SyncFolderOffsiteBackupProvider(sync_dir)
    with pytest.raises(ValueError, match="Plaintext SQLite .db files are never copied"):
        provider.publish_encrypted_archive(verified_bkp)
    assert list(sync_dir.iterdir()) == []

    # 3. Enable off-site backup and publish verified backup
    cfg = mgr.update_config(enabled=True, destination_folder=str(sync_dir))
    assert cfg["enabled"] is True and cfg["destination_available"] is True

    pub1 = mgr.publish_verified_backup(verified_bkp)
    assert pub1["action"] == "published"
    assert pub1["status"] == OffsiteStatus.CLOUD_SYNC_NOT_CONFIRMED
    assert pub1["cloud_sync_confirmed"] is False
    assert pub1["cloud_sync_disclaimer"] == CLOUD_SYNC_DISCLAIMER
    assert pub1["local_backup_verified"] is True
    assert pub1["encryption_completed"] is True
    assert pub1["copied_to_sync_folder"] is True
    assert pub1["destination_checksum_verified"] is True
    assert pub1["milestones"] == [
        OffsiteStatus.LOCAL_BACKUP_VERIFIED,
        OffsiteStatus.ENCRYPTION_COMPLETED,
        OffsiteStatus.COPIED_TO_SYNC_FOLDER,
        OffsiteStatus.DESTINATION_CHECKSUM_VERIFIED,
        OffsiteStatus.CLOUD_SYNC_NOT_CONFIRMED,
    ]

    # Confirm ONLY .kdb.enc exists in sync folder (ZERO .db, .wal, .shm, .part, .tmp!)
    sync_files = list(sync_dir.iterdir())
    assert len(sync_files) == 1
    published_file = sync_files[0]
    assert published_file.name.endswith(".kdb.enc")
    assert _sha256_file(published_file) == pub1["sha256"]
    assert published_file.stat().st_size == pub1["size_bytes"]
    # Source local .db backup preserved untouched
    assert verified_bkp.is_file()

    # 4. Duplicate publication of same verified source backup is skipped automatically
    dup_skip = mgr.publish_verified_backup(verified_bkp, allow_duplicate=False)
    assert dup_skip["action"] == "skipped"
    assert dup_skip["reason"] == "already_published"
    assert len(list(sync_dir.iterdir())) == 1

    # 5. Explicit re-publish (allow_duplicate=True) NEVER overwrites existing archive
    orig_sha = _sha256_file(published_file)
    pub2 = mgr.publish_verified_backup(verified_bkp, allow_duplicate=True)
    assert pub2["action"] == "published"
    assert pub2["filename"] != published_file.name
    assert _sha256_file(published_file) == orig_sha  # First archive untouched!
    assert len(list(sync_dir.iterdir())) == 2

    # 6. Interrupted copy cleans up incomplete .part file completely
    real_fdopen = os.fdopen

    class _FailingOutStream:
        def __init__(self, inner):
            self._inner = inner
            self._writes = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

        def write(self, data):
            self._writes += 1
            self._inner.write(data[: min(32, len(data))])
            raise OSError("Simulated disconnected USB/cloud drive mid-copy")

        def flush(self):
            self._inner.flush()

        def fileno(self):
            return self._inner.fileno()

    with monkeypatch.context() as mp:
        mp.setattr(os, "fdopen", lambda fd, mode="r", *a, **kw: _FailingOutStream(real_fdopen(fd, mode, *a, **kw)) if "wb" in mode else real_fdopen(fd, mode, *a, **kw))
        with pytest.raises(OSError, match="Simulated disconnected"):
            provider.publish_encrypted_archive(published_file)
    # ZERO .part or .tmp files left behind in sync folder!
    assert [p for p in sync_dir.iterdir() if p.name.endswith(".part") or p.name.endswith(".tmp")] == []

    # 7. Disabling off-site backup & running local retention NEVER deletes synchronized-folder archives!
    mgr.update_config(enabled=False)
    BackupService(db, backup_dir=backup_dir).apply_retention_policy(backup_dir=backup_dir)
    assert len([p for p in sync_dir.iterdir() if p.name.endswith(".kdb.enc")]) == 2

    # Zero secret leakage into config file
    cfg_text = mgr.get_config_path().read_text(encoding="utf-8")
    assert rk not in cfg_text and "KDHN-" not in cfg_text


# =====================================================================
# 2. Scheduler Integration, Bounded Retry After Temporary Unavailability,
#    OS Recovery Key Unavailable & Zero ERP Transaction Disruption
# =====================================================================
def test_02_scheduler_integration_retry_on_recovery_and_erp_isolation(
    isolated_2c5a_env: Dict[str, Any],
):
    db = isolated_2c5a_env["db"]
    backup_dir = isolated_2c5a_env["backup_dir"]
    sync_dir = isolated_2c5a_env["sync_dir"]
    tmp_path = isolated_2c5a_env["tmp_path"]

    now_box = [time.time() + 120.0]
    mgr = OffsiteBackupManager(db, backup_dir=backup_dir, clock=lambda: now_box[0])
    mgr.update_config(enabled=True, destination_folder=str(sync_dir))

    scheduler = BackupScheduler(
        db_manager=db,
        backup_dir=backup_dir,
        min_backup_interval_seconds=60.0,
        clock=lambda: now_box[0],
    )
    scheduler._offsite_manager = mgr

    # 1. Disconnect sync folder temporarily (rename it away)
    offline_dir = tmp_path / "sync_folder_temporarily_disconnected"
    sync_dir.rename(offline_dir)

    # Make a DB write so scheduler creates a new local backup
    master = MasterDataRepository(db)
    p1_id = master.create_product(Product(product_name="Offsite Offline Test Seed", default_sale_rate=420.0))
    now_box[0] += 3600.0

    res1 = scheduler.run_check(is_startup=False, sync_offsite=True)
    assert res1["action"] == "created"
    assert res1["offsite"]["status"] == OffsiteStatus.DESTINATION_UNAVAILABLE
    st1 = mgr.get_status()
    assert st1["status"] == OffsiteStatus.DESTINATION_UNAVAILABLE
    assert st1["pending_retry"] is not None
    assert st1["pending_retry"]["attempts"] == 1

    # ERP transactions continue completely unaffected while off-site destination is unavailable!
    p2_id = master.create_product(Product(product_name="ERP Works While Sync Offline", default_sale_rate=510.0))
    assert db.fetch_one("SELECT product_name FROM products WHERE product_id = ?;", (p1_id,))[0] == "Offsite Offline Test Seed"
    assert db.fetch_one("SELECT product_name FROM products WHERE product_id = ?;", (p2_id,))[0] == "ERP Works While Sync Offline"

    # 2. Bring sync folder back online; before backoff elapses, retry does not spam
    offline_dir.rename(sync_dir)
    assert mgr.process_pending_retry_if_due() is None
    assert list(sync_dir.iterdir()) == []

    # Advance clock past 5-minute backoff -> scheduler tick retries & publishes pending verified backup!
    now_box[0] += 305.0
    # Note: min_backup_interval_seconds=60 -> either creates new backup OR retries pending; both publish cleanly!
    res2 = scheduler.run_check(is_startup=False, sync_offsite=True)
    assert res2["action"] in ("created", "skipped")
    published = [p for p in sync_dir.iterdir() if p.name.endswith(".kdb.enc")]
    assert len(published) == 1
    st2 = mgr.get_status()
    assert st2["status"] == OffsiteStatus.CLOUD_SYNC_NOT_CONFIRMED
    assert st2["pending_retry"] is None
    assert st2["last_checksum_verified"] is True

    # 3. Simulate OS Recovery Key store becoming unavailable -> fails closed cleanly, ZERO plaintext copied
    set_recovery_key_store_for_testing(UnsupportedRecoveryKeyStore(platform_name="linux"))
    master.create_product(Product(product_name="Post Key Unavailable Product", default_sale_rate=600.0))
    now_box[0] += 3600.0
    res3 = scheduler.run_check(is_startup=False, sync_offsite=True)
    assert res3["action"] == "created"  # Local backup still succeeds!
    assert res3["offsite"]["status"] == OffsiteStatus.RECOVERY_KEY_UNAVAILABLE
    assert [p for p in sync_dir.iterdir() if p.name.endswith(".db")] == []
    assert len([p for p in sync_dir.iterdir() if p.name.endswith(".kdb.enc")]) == 1


# =====================================================================
# 3. ADMIN-Only API RBAC, Honest Status & UI Contract Verification
# =====================================================================
def test_03_api_rbac_honest_status_and_ui_contract(
    isolated_2c5a_env: Dict[str, Any],
    caplog: pytest.LogCaptureFixture,
):
    admin_hdr = isolated_2c5a_env["admin_headers"]
    op_hdr = isolated_2c5a_env["operator_headers"]
    sync_dir = isolated_2c5a_env["sync_dir"]
    rk = isolated_2c5a_env["rk"]
    caplog.set_level(logging.DEBUG)

    with TestClient(app) as client:
        # Unauthenticated -> 401; OPERATOR -> 403 on all off-site endpoints
        for method, path, body in [
            ("GET", "/api/system/offsite-backup/status", None),
            ("POST", "/api/system/offsite-backup/config", {"enabled": True, "destination_folder": str(sync_dir)}),
            ("POST", "/api/system/offsite-backup/validate-folder", {"folder_path": str(sync_dir)}),
            ("POST", "/api/system/offsite-backup/run-now", {}),
            ("POST", "/api/system/offsite-backup/copy-local-encrypted", {"encrypted_filename": "x.kdb.enc"}),
            ("POST", "/api/system/offsite-backup/inspect-folder-archive", {"filename": "x.kdb.enc"}),
            ("POST", "/api/system/offsite-backup/restore-folder-archive", {"filename": "x.kdb.enc", "confirm_restore": True}),
        ]:
            if method == "GET":
                assert client.get(path).status_code == 401
                assert client.get(path, headers=op_hdr).status_code == 403
            else:
                assert client.post(path, json=body).status_code == 401
                assert client.post(path, headers=op_hdr, json=body).status_code == 403

        # Admin configures, validates, runs now, and checks honest status
        v_res = client.post(
            "/api/system/offsite-backup/validate-folder",
            headers=admin_hdr,
            json={"folder_path": str(sync_dir), "include_archives": True},
        )
        assert v_res.status_code == 200 and v_res.json()["available"] is True

        c_res = client.post(
            "/api/system/offsite-backup/config",
            headers=admin_hdr,
            json={"enabled": True, "destination_folder": str(sync_dir)},
        )
        assert c_res.status_code == 200 and c_res.json()["enabled"] is True

        r_res = client.post("/api/system/offsite-backup/run-now", headers=admin_hdr, json={})
        assert r_res.status_code == 200, r_res.text
        r_data = r_res.json()
        assert r_data["status"] == OffsiteStatus.CLOUD_SYNC_NOT_CONFIRMED
        assert r_data["cloud_sync_confirmed"] is False
        assert r_data["cloud_sync_disclaimer"] == CLOUD_SYNC_DISCLAIMER
        assert "cloud backup successful" not in r_res.text.lower()
        assert rk not in r_res.text

        st_res = client.get("/api/system/offsite-backup/status", headers=admin_hdr)
        assert st_res.status_code == 200
        st = st_res.json()
        assert st["cloud_sync_confirmed"] is False
        assert st["cloud_sync_disclaimer"] == CLOUD_SYNC_DISCLAIMER
        assert len(st["published_archives"]) == 1
        assert rk not in st_res.text

    for rec in caplog.records:
        assert rk not in rec.getMessage()

    # Verify UI template contains exact honest disclaimer & required controls (English-only)
    repo_root = Path(__file__).resolve().parent.parent
    html_text = (repo_root / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    assert CLOUD_SYNC_DISCLAIMER in html_text
    for elem_id in (
        "offsite-backup-section",
        "offsite-enabled-toggle",
        "offsite-folder-path-input",
        "btn-offsite-pick-folder",
        "btn-offsite-validate-folder",
        "btn-offsite-save-config",
        "btn-offsite-run-now",
        "offsite-dest-badge",
        "offsite-status-badge",
        "offsite-last-copy-info",
        "offsite-archives-tbody",
        "offsite-recovery-panel",
        "btn-offsite-open-restore-confirm",
    ):
        assert f'id="{elem_id}"' in html_text


# =====================================================================
# 4. Full End-to-End Replacement-Computer Recovery Acceptance Test
# =====================================================================
def test_04_replacement_computer_full_recovery_and_rollback_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Realistic disaster-recovery acceptance test:
    1. Source Computer A: populate realistic customers, suppliers, purchases, invoices, balances, expenses, receipts.
    2. Encrypt & publish verified backup under Key #1 to simulated cloud-synchronized folder.
    3. Rotate to Key #2 on Source Computer A (confirming historical archive keeps Key #1).
    4. Replacement Computer B: brand-new empty DB & fresh empty key store (NO original DB, NO stored keys).
    5. Import Recovery Card Key #1 -> locate archive in sync folder -> inspect (resolves historical key_id!)
       -> reject unconfirmed restore -> confirm restore -> verify ALL business records & balances match exactly
       and original archive in sync folder is untouched!
    6. Verify forced post-swap error triggers automatic rollback preserving current DB.
    """
    sync_dir = tmp_path / "shared_cloud_sync_drive"
    sync_dir.mkdir(parents=True, exist_ok=True)

    # ---- SOURCE COMPUTER A ----
    src_db_path = tmp_path / "computer_a" / "source_shop.db"
    src_bkp_dir = tmp_path / "computer_a" / "backups"
    src_db_path.parent.mkdir(parents=True, exist_ok=True)
    src_bkp_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(src_db_path))
    monkeypatch.setenv("BACKUP_DIR", str(src_bkp_dir))
    monkeypatch.setenv("JWT_SECRET", "computer_a_jwt_secret_" + secrets.token_hex(16))

    stop_backup_scheduler(timeout=2.0)
    reset_db_manager()
    reset_recovery_key_store()
    reset_recovery_key_lifecycle()

    db_a = get_db_manager()
    db_a.initialize_database(include_seed=True)

    master_a = MasterDataRepository(db_a)
    purchase_a = PurchaseService(db_a)
    sales_a = SalesService(db_a)
    acct_a = AccountingService(db_a)

    prod_id = master_a.create_product(
        Product(
            product_name="IFFCO DAP 18:46 50kg",
            category_id=1,
            unit_id=2,
            tax_group_id=2,
            default_purchase_rate=1100.0,
            default_sale_rate=1350.0,
            default_mrp=1400.0,
        )
    )
    cust_id = master_a.create_customer(
        Customer(customer_name="Ramesh Patil", mobile="9822001122", village="Baramati")
    )
    supp_id = master_a.create_supplier(
        Supplier(supplier_name="Mahyco Agro Distributors", mobile="9811223344", gstin="27AABCM1234A1Z5")
    )

    purchase_id = purchase_a.process_inward_purchase(
        Purchase(
            supplier_id=supp_id,
            invoice_no="SUP-2C5A-101",
            purchase_date="2026-11-18",
            payment_mode="CREDIT",
            total_taxable=22000.0,
            total_cgst=550.0,
            total_sgst=550.0,
            net_amount=23100.0,
            paid_amount=0.0,
            due_amount=23100.0,
            items=[
                PurchaseItem(
                    product_id=prod_id,
                    batch_no="DAP-2026-A",
                    qty=20.0,
                    unit_id=2,
                    purchase_rate=1100.0,
                    mrp=1400.0,
                    sale_rate=1350.0,
                    taxable_amount=22000.0,
                    cgst_rate=2.5,
                    cgst_amount=550.0,
                    sgst_rate=2.5,
                    sgst_amount=550.0,
                    total_amount=23100.0,
                )
            ],
        )
    )
    batch_id = int(db_a.fetch_one("SELECT batch_id FROM stock_batches WHERE product_id = ? LIMIT 1;", (prod_id,))[0])

    sale_id = sales_a.process_sales_invoice(
        Sale(
            invoice_no="INV-2C5A-101",
            sale_date="2026-11-18",
            customer_id=cust_id,
            payment_mode="CREDIT",
            total_taxable=5142.86,
            total_cgst=128.57,
            total_sgst=128.57,
            net_amount=5400.0,
            paid_amount=0.0,
            due_amount=5400.0,
            items=[
                SaleItem(
                    product_id=prod_id,
                    batch_id=batch_id,
                    qty=4.0,
                    unit_id=2,
                    sale_rate=1350.0,
                    mrp=1400.0,
                    taxable_amount=5142.86,
                    cgst_rate=2.5,
                    cgst_amount=128.57,
                    sgst_rate=2.5,
                    sgst_amount=128.57,
                    total_amount=5400.0,
                )
            ],
        )
    )

    exp_id = acct_a.record_shop_expense(
        Expense(
            expense_date="2026-11-18",
            category_id=1,
            amount=450.0,
            payment_mode="CASH",
            description="Freight for DAP lot",
        )
    )
    rcpt_id = acct_a.record_customer_receipt(
        customer_id=cust_id,
        receipt_date="2026-11-18",
        amount=500.0,
        payment_mode="CASH",
        narration="Partial cash collection",
    )

    expected_sale_net = float(db_a.fetch_one("SELECT net_amount FROM sales WHERE sale_id = ?;", (sale_id,))[0])
    expected_cust_bal = float(db_a.fetch_one("SELECT current_balance FROM customers WHERE customer_id = ?;", (cust_id,))[0])
    expected_supp_bal = float(db_a.fetch_one("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (supp_id,))[0])
    expected_stock = float(db_a.fetch_one("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch_id,))[0])
    assert expected_stock == pytest.approx(16.0)
    assert expected_cust_bal > 0

    # Key #1 active on Computer A -> publish verified encrypted archive to sync_dir
    store_a = InMemoryRecoveryKeyStore(available=True)
    rk1 = generate_recovery_key()
    kid1 = store_a.store_key(rk1, activate=True, source="generated")
    set_recovery_key_store_for_testing(store_a)

    bkp_a = BackupService(db_a, backup_dir=src_bkp_dir).create_backup(backup_type="MANUAL")
    mgr_a = OffsiteBackupManager(db_a, backup_dir=src_bkp_dir)
    mgr_a.update_config(enabled=True, destination_folder=str(sync_dir))
    pub_a = mgr_a.publish_verified_backup(bkp_a)
    assert pub_a["action"] == "published"
    archive_name = pub_a["filename"]
    archive_path = sync_dir / archive_name
    archive_sha_before = _sha256_file(archive_path)

    # Rotate to Key #2 on Computer A (historical archive retains Key #1)
    rk2 = generate_recovery_key()
    kid2 = store_a.store_key(rk2, activate=True, source="rotated")
    assert store_a.get_active_key_id() == kid2

    # ---- REPLACEMENT COMPUTER B (BRAND NEW ENVIRONMENT — ZERO SOURCE DB, ZERO STORED KEYS) ----
    repl_db_path = tmp_path / "computer_b" / "fresh_shop.db"
    repl_bkp_dir = tmp_path / "computer_b" / "backups"
    repl_db_path.parent.mkdir(parents=True, exist_ok=True)
    repl_bkp_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DB_PATH", str(repl_db_path))
    monkeypatch.setenv("BACKUP_DIR", str(repl_bkp_dir))
    monkeypatch.setenv("JWT_SECRET", "computer_b_jwt_secret_" + secrets.token_hex(16))

    stop_backup_scheduler(timeout=2.0)
    reset_db_manager()
    reset_recovery_key_store()
    reset_recovery_key_lifecycle()

    db_b = get_db_manager()
    db_b.initialize_database(include_seed=True)
    # Confirm fresh Computer B does NOT have source customer or purchase yet
    assert db_b.fetch_one("SELECT 1 FROM customers WHERE customer_name = 'Ramesh Patil';") is None
    assert db_b.fetch_one("SELECT 1 FROM purchases WHERE invoice_no = 'SUP-2C5A-101';") is None

    # Brand new empty OS key store on Computer B
    store_b = InMemoryRecoveryKeyStore(available=True)
    set_recovery_key_store_for_testing(store_b)

    auth_b = AuthService(db_b)
    admin_b = db_b.fetch_one("SELECT user_id, username, role, full_name FROM users WHERE role = 'ADMIN' LIMIT 1;")
    token_b = auth_b.create_access_token(int(admin_b[0]), admin_b[1], admin_b[3], "ADMIN")
    hdr_b = {"Authorization": f"Bearer {token_b}"}

    with TestClient(app) as client_b:
        # Step 1: Select/validate synchronized folder on Replacement Computer B & locate .kdb.enc archives
        val_b = client_b.post(
            "/api/system/offsite-backup/validate-folder",
            headers=hdr_b,
            json={"folder_path": str(sync_dir), "include_archives": True},
        )
        assert val_b.status_code == 200
        archives_found = val_b.json()["archives"]
        assert any(a["filename"] == archive_name and a["key_id"] == kid1 for a in archives_found)

        # Step 2: Before importing Recovery Card, inspection returns unauthenticated header only
        insp_before = client_b.post(
            "/api/system/offsite-backup/inspect-folder-archive",
            headers=hdr_b,
            json={"filename": archive_name, "folder_path": str(sync_dir)},
        )
        assert insp_before.status_code == 200
        assert insp_before.json()["authenticated"] is False
        assert insp_before.json()["requires_manual_recovery_key"] is True
        assert insp_before.json()["key_id"] == kid1

        # Step 3: Import Recovery Card Key #1 into Computer B's OS secure store
        imp_res = client_b.post(
            "/api/system/recovery-keys/import",
            headers=hdr_b,
            json={"recovery_key": rk1, "confirm_import": True, "set_as_active": True},
        )
        assert imp_res.status_code == 200 and imp_res.json()["key_id"] == kid1

        # Step 4: Inspect again -> resolves exact historical kid1 automatically & verifies SQLite!
        insp_after = client_b.post(
            "/api/system/offsite-backup/inspect-folder-archive",
            headers=hdr_b,
            json={"filename": archive_name, "folder_path": str(sync_dir)},
        )
        assert insp_after.status_code == 200, insp_after.text
        ia = insp_after.json()
        assert ia["authenticated"] is True
        assert ia["metadata_authenticated"] is True
        assert ia["key_source"] == "os_secure_store"
        assert ia["valid_for_restore"] is True
        assert ia["integrity_ok"] is True and ia["foreign_key_ok"] is True

        # Step 5: Unconfirmed restore is rejected (HTTP 400)
        unconf = client_b.post(
            "/api/system/offsite-backup/restore-folder-archive",
            headers=hdr_b,
            json={"filename": archive_name, "folder_path": str(sync_dir), "confirm_restore": False},
        )
        assert unconf.status_code == 400
        assert db_b.fetch_one("SELECT 1 FROM customers WHERE customer_name = 'Ramesh Patil';") is None

        # Step 6: Confirmed restore succeeds with BEFORE_RESTORE snapshot
        rest_res = client_b.post(
            "/api/system/offsite-backup/restore-folder-archive",
            headers=hdr_b,
            json={"filename": archive_name, "folder_path": str(sync_dir), "confirm_restore": True},
        )
        assert rest_res.status_code == 200, rest_res.text
        rd = rest_res.json()
        assert rd["success"] is True
        assert rd["key_id"] == kid1
        assert rd["pre_restore_filename"]
        assert (repl_bkp_dir / rd["pre_restore_filename"]).is_file()

        # Verify restored business data matches Source Computer A identically!
        assert tuple(db_b.fetch_one("SELECT customer_name, current_balance FROM customers WHERE customer_id = ?;", (cust_id,))) == (
            "Ramesh Patil",
            pytest.approx(expected_cust_bal),
        )
        assert tuple(db_b.fetch_one("SELECT supplier_name, current_balance FROM suppliers WHERE supplier_id = ?;", (supp_id,))) == (
            "Mahyco Agro Distributors",
            pytest.approx(expected_supp_bal),
        )
        assert db_b.fetch_one("SELECT invoice_no FROM purchases WHERE purchase_id = ?;", (purchase_id,))[0] == "SUP-2C5A-101"
        assert float(db_b.fetch_one("SELECT net_amount FROM sales WHERE sale_id = ?;", (sale_id,))[0]) == pytest.approx(expected_sale_net)
        assert float(db_b.fetch_one("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (batch_id,))[0]) == pytest.approx(expected_stock)
        assert float(db_b.fetch_one("SELECT amount FROM expenses WHERE expense_id = ?;", (exp_id,))[0]) == pytest.approx(450.0)
        assert float(db_b.fetch_one("SELECT total_amount FROM vouchers WHERE voucher_id = ?;", (rcpt_id,))[0]) == pytest.approx(500.0)

        # Original archive in sync_dir was NEVER modified or deleted during recovery!
        assert archive_path.is_file()
        assert _sha256_file(archive_path) == archive_sha_before

        # Step 7: Forced post-swap failure during off-site restore triggers automatic rollback!
        master_b = MasterDataRepository(db_b)
        surv_pid = master_b.create_product(Product(product_name="Must Survive Offsite Rollback", default_sale_rate=888.0))
        orig_verify = BackupService.verify_database_integrity

        def _fail_live_verify(self_bkp, db_file_path=None, check_foreign_keys=False):
            if db_file_path is not None and Path(db_file_path).resolve() == repl_db_path.resolve():
                return False
            return orig_verify(self_bkp, db_file_path=db_file_path, check_foreign_keys=check_foreign_keys)

        monkeypatch.setattr(BackupService, "verify_database_integrity", _fail_live_verify)
        rb_res = client_b.post(
            "/api/system/offsite-backup/restore-folder-archive",
            headers=hdr_b,
            json={"filename": archive_name, "folder_path": str(sync_dir), "confirm_restore": True},
        )
        assert rb_res.status_code == 500
        assert db_b.fetch_one("SELECT product_name FROM products WHERE product_id = ?;", (surv_pid,))[0] == "Must Survive Offsite Rollback"

    stop_backup_scheduler(timeout=2.0)
    reset_recovery_key_lifecycle()
    reset_recovery_key_store()
    reset_db_manager()
