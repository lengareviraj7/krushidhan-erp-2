"""
Isolated Security, Cryptographic, Recovery & API Verification Suite for Phase 2C-4B:
Encrypted Backup Container (.kdb.enc v1) and Offline Export/Restore.

Strictly uses isolated temporary files, temporary SQLite databases, and temporary backup directories.
Never touches live data/agri_erp.db or repository backups/.
"""
from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
import logging
import math
import os
import secrets
import shutil
import sqlite3
import struct
import zlib
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, reset_db_manager
from src.desktop_runtime import redact_sensitive_text
from src.models.accounting import Expense
from src.models.master_data import Customer, Product, Supplier
from src.models.purchase import Purchase, PurchaseItem
from src.models.sales import Sale, SaleItem
from src.repositories.master_data_repository import MasterDataRepository
from src.services.accounting_service import AccountingService
from src.services.auth_service import AuthService
from src.services.backup_scheduler import stop_backup_scheduler
from src.services.backup_service import BackupService
from src.services.encrypted_backup_service import (
    CHUNK_AAD_STRUCT_FMT,
    CHUNK_RECORD_OVERHEAD_BYTES,
    CHUNK_RECORD_PREFIX_BYTES,
    CHUNK_RECORD_PREFIX_STRUCT_FMT,
    DEFAULT_MAX_COMPRESSED_BYTES,
    DEFAULT_MAX_PLAINTEXT_BYTES,
    EXPECTED_WRAP_BOX_BYTES,
    GCM_TAG_BYTES,
    HEADER_LEN_PREFIX_BYTES,
    HEADER_LEN_STRUCT_FMT,
    EncryptedBackupAuthError,
    EncryptedBackupError,
    EncryptedBackupFormatError,
    EncryptedBackupService,
    KDB_ENC_MAGIC,
    MAX_CHUNK_INDEX,
    MAX_ENCRYPTED_ARCHIVE_BYTES,
    MAX_ENCRYPTED_UPLOAD_BYTES,
    MAX_HEADER_BYTES,
    MIN_CHUNK_SIZE,
    WRAP_LEN_PREFIX_BYTES,
    WRAP_LEN_STRUCT_FMT,
    WRAP_NONCE_BYTES,
    _chunk_aad,
    _chunk_nonce,
    _create_restricted_temp_dir,
    _derive_kek,
    _parse_and_validate_header,
    calculate_max_encrypted_archive_bytes,
)
from src.services.purchase_service import PurchaseService
from src.services.recovery_key import (
    InMemoryRecoveryKeyStore,
    InvalidRecoveryKeyError,
    RECOVERY_KEY_BYTES,
    encode_recovery_key,
    generate_recovery_key,
    get_recovery_key_id,
    normalize_recovery_key,
    validate_recovery_key,
)
from src.services.sales_service import SalesService


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_raw_kdb_chunks(enc_path: Path) -> Tuple[bytes, List[Tuple[int, int, bytes]]]:
    """Read raw prefix (header+wrap) and list of (record_len, is_final, ciphertext) records for tamper tests."""
    raw = enc_path.read_bytes()
    with open(enc_path, "rb") as fh:
        _parse_and_validate_header(fh)
        chunks_offset = fh.tell()
    prefix = raw[:chunks_offset]
    pos = chunks_offset
    chunks: List[Tuple[int, int, bytes]] = []
    while pos < len(raw):
        rec_len, is_final = struct.unpack(">IB", raw[pos : pos + 5])
        pos += 5
        ct = raw[pos : pos + rec_len]
        pos += rec_len
        chunks.append((rec_len, is_final, ct))
    return prefix, chunks


def _multipart_body(
    file_field: str,
    filename: str,
    payload: bytes,
    extra_fields: Dict[str, str] | None = None,
) -> Tuple[bytes, str]:
    boundary = f"----KrushidhanPhase2C4B{secrets.token_hex(8)}"
    parts: List[bytes] = []
    for k, v in (extra_fields or {}).items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"),
                f"{v}\r\n".encode("utf-8"),
            ]
        )
    parts.extend(
        [
            f"--{boundary}\r\n".encode("ascii"),
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8"),
            b"Content-Type: application/octet-stream\r\n\r\n",
            payload,
            f"\r\n--{boundary}--\r\n".encode("ascii"),
        ]
    )
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Create a rich, realistic isolated shop database + isolated backup directory."""
    stop_backup_scheduler(timeout=2.0)
    reset_db_manager()

    db_file = tmp_path / "data" / "shop_2c4b.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("JWT_SECRET", "phase2c4b-test-secret-0123456789abcdef0123456789abcdef")

    db = DatabaseManager(db_path=db_file, app_mode="local", backup_dir=backup_dir, clean_install=True)
    db.initialize_database(include_seed=True)
    for p in list(backup_dir.iterdir()):
        if p.is_file():
            p.unlink()

    # Populate realistic business data: masters, purchase, sale, receipt, supplier payment, expense
    master = MasterDataRepository(db)
    purchase_svc = PurchaseService(db)
    sales_svc = SalesService(db)
    acct_svc = AccountingService(db)

    cust_id = master.create_customer(Customer(customer_name="Suresh Patil", mobile="9822334455", village="Baramati"))
    supp_id = master.create_supplier(Supplier(supplier_name="Mahadhan Distributors", mobile="9822998877"))
    prod_id = master.create_product(
        Product(
            product_name="Encrypted Roundtrip Urea 45kg",
            category_id=1,
            unit_id=2,
            tax_group_id=2,
            default_purchase_rate=220.0,
            default_sale_rate=266.0,
            default_mrp=266.0,
        )
    )
    pur_id = purchase_svc.process_inward_purchase(
        Purchase(
            supplier_id=supp_id,
            invoice_no="PINV-2C4B-101",
            purchase_date="2026-11-18",
            payment_mode="CREDIT",
            total_taxable=4400.0,
            total_cgst=110.0,
            total_sgst=110.0,
            net_amount=4620.0,
            paid_amount=0.0,
            due_amount=4620.0,
            items=[
                PurchaseItem(
                    product_id=prod_id,
                    batch_no="B-2C4B-101",
                    qty=20.0,
                    unit_id=2,
                    purchase_rate=220.0,
                    mrp=266.0,
                    sale_rate=266.0,
                    taxable_amount=4400.0,
                    cgst_rate=2.5,
                    cgst_amount=110.0,
                    sgst_rate=2.5,
                    sgst_amount=110.0,
                    total_amount=4620.0,
                )
            ],
        )
    )
    batch_id = int(db.fetch_one("SELECT batch_id FROM stock_batches WHERE product_id = ? LIMIT 1;", (prod_id,))["batch_id"])
    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-2C4B-101",
            sale_date="2026-11-18",
            customer_id=cust_id,
            payment_mode="CREDIT",
            total_taxable=1266.67,
            total_cgst=31.67,
            total_sgst=31.66,
            net_amount=1330.0,
            paid_amount=0.0,
            due_amount=1330.0,
            items=[
                SaleItem(
                    product_id=prod_id,
                    batch_id=batch_id,
                    qty=5.0,
                    unit_id=2,
                    sale_rate=266.0,
                    mrp=266.0,
                    taxable_amount=1266.67,
                    cgst_rate=2.5,
                    cgst_amount=31.67,
                    sgst_rate=2.5,
                    sgst_amount=31.66,
                    total_amount=1330.0,
                )
            ],
        )
    )
    rcpt_id = acct_svc.record_customer_receipt(
        customer_id=cust_id,
        receipt_date="2026-11-18",
        amount=500.0,
        payment_mode="CASH",
        narration="Part recovery cash receipt",
    )
    spay_id = acct_svc.record_supplier_payment(
        supplier_id=supp_id,
        payment_date="2026-11-18",
        amount=1620.0,
        payment_mode="CASH",
        narration="Supplier part payment",
    )
    exp_id = acct_svc.record_shop_expense(
        Expense(
            expense_date="2026-11-18",
            category_id=1,
            amount=350.0,
            payment_mode="CASH",
            description="Shop electricity bill",
        )
    )

    # Create a verified baseline .db backup
    bkp_svc = BackupService(db, backup_dir=backup_dir)
    verified_db_backup = bkp_svc.create_backup(backup_type="MANUAL")

    admin_token = AuthService.create_access_token(1, "enc_admin", "Admin", "ADMIN")
    op_token = AuthService.create_access_token(2, "enc_op", "Operator", "OPERATOR")

    yield {
        "db": db,
        "db_file": db_file,
        "backup_dir": backup_dir,
        "tmp_path": tmp_path,
        "verified_db_backup": verified_db_backup,
        "records": {
            "cust_id": cust_id,
            "supp_id": supp_id,
            "prod_id": prod_id,
            "batch_id": batch_id,
            "pur_id": pur_id,
            "sale_id": sale_id,
            "rcpt_id": rcpt_id,
            "spay_id": spay_id,
            "exp_id": exp_id,
        },
        "admin_headers": {"Authorization": f"Bearer {admin_token}"},
        "operator_headers": {"Authorization": f"Bearer {op_token}"},
    }
    stop_backup_scheduler(timeout=2.0)
    reset_db_manager()


# =====================================================================
# 1–4. Recovery Key Entropy, Round-Trip, Checksum, Key ID & KeyStore
# =====================================================================
def test_01_02_03_04_recovery_key_entropy_roundtrip_checksum_key_id_and_store():
    """Tests 1, 2, 3, 4 + InMemoryRecoveryKeyStore + log redaction."""
    rk1 = generate_recovery_key()
    rk2 = generate_recovery_key()
    assert rk1 != rk2
    assert rk1.startswith("KDHN-")

    # Test 1: Exact 256-bit (32-byte) entropy
    raw1 = validate_recovery_key(rk1)
    raw2 = validate_recovery_key(rk2)
    assert len(raw1) == RECOVERY_KEY_BYTES == 32
    assert len(raw2) == 32
    assert raw1 != raw2

    # Test 2: Encode/decode round-trip + normalization (lowercase, spaces, underscores, hyphens)
    assert encode_recovery_key(raw1) == rk1
    messy = "  " + rk1.lower().replace("-", " ") + "  "
    assert validate_recovery_key(messy) == raw1
    assert normalize_recovery_key(messy) == rk1
    assert validate_recovery_key(rk1.replace("-", "_")) == raw1

    # Test 3: Checksum rejects mistyped keys (never leaks input key in error text)
    chars = list(rk1)
    # Flip a character inside the body (index 6 is inside first group)
    chars[6] = "A" if chars[6] != "A" else "B"
    mistyped_body = "".join(chars)
    with pytest.raises(InvalidRecoveryKeyError) as exc_info:
        validate_recovery_key(mistyped_body)
    assert mistyped_body not in str(exc_info.value)

    # Flip last character (checksum group)
    chars_end = list(rk1)
    chars_end[-1] = "A" if chars_end[-1] != "A" else "Q"
    with pytest.raises(InvalidRecoveryKeyError):
        validate_recovery_key("".join(chars_end))

    # Missing prefix, blank, wrong length
    for bad in ("", "   ", rk1[5:], "KDHN-SHORT", "XXXX-" + rk1[5:]):
        with pytest.raises(InvalidRecoveryKeyError):
            validate_recovery_key(bad)

    # Test 4: Key ID is stable, 16-char uppercase hex, non-secret
    kid1 = get_recovery_key_id(rk1)
    assert len(kid1) == 16
    assert kid1 == get_recovery_key_id(messy)
    assert kid1 != get_recovery_key_id(rk2)
    assert kid1 not in rk1

    # Log redaction scrubs Recovery Keys automatically
    scrubbed = redact_sensitive_text(f"attempted recovery_key={rk1} with token {rk2}")
    assert rk1 not in scrubbed
    assert rk2 not in scrubbed

    # InMemoryRecoveryKeyStore interface check
    store = InMemoryRecoveryKeyStore()
    assert store.has_key() is False
    stored_id = store.store_key(messy)
    assert stored_id == kid1
    assert store.has_key() is True
    assert store.get_active_key_id() == kid1
    assert store.get_key() == rk1
    assert store.get_key(kid1) == rk1
    assert store.delete_key(kid1) is True
    assert store.has_key() is False


# =====================================================================
# 5, 19, 20, 21, 22, 23. Round-Trip, Business Data, Cross-Platform & Live DB Safety
# =====================================================================
def test_05_19_20_21_22_23_encrypt_decrypt_roundtrip_business_records_and_determinism(
    isolated_env: Dict[str, Any],
):
    """Tests 5, 19, 20, 21, 22, 23: Full round-trip, multi-chunk streaming, business rows, cross-platform format."""
    db: DatabaseManager = isolated_env["db"]
    db_file: Path = isolated_env["db_file"]
    src_bkp: Path = isolated_env["verified_db_backup"]
    tmp_path: Path = isolated_env["tmp_path"]
    rec = isolated_env["records"]

    live_hash_before = _sha256_file(db_file)
    src_bkp_hash_before = _sha256_file(src_bkp)

    rk = generate_recovery_key()
    store = InMemoryRecoveryKeyStore()
    store.store_key(rk)

    # Use 4096-byte chunks so a ~440 KB SQLite DB spans multiple authenticated chunks
    enc_svc = EncryptedBackupService(db, backup_dir=isolated_env["backup_dir"], key_store=store, chunk_size=4096)

    # Test 23a: Refuses to encrypt the live SQLite database directly
    with pytest.raises(ValueError, match="live database"):
        enc_svc.encrypt_backup(db_file, recovery_key=rk)

    enc_meta = enc_svc.encrypt_backup(src_bkp, recovery_key=rk)
    enc_path = Path(enc_meta["file_path"])
    assert enc_path.is_file()
    assert enc_path.name.endswith(".kdb.enc")
    assert enc_meta["chunk_count"] >= 2, "Expected multi-chunk archive for streaming verification"
    assert enc_meta["plaintext_sha256"] == src_bkp_hash_before

    # Test 23b: Original live DB and original .db backup remain byte-for-byte unchanged
    assert _sha256_file(db_file) == live_hash_before
    assert _sha256_file(src_bkp) == src_bkp_hash_before

    # Test 22: Cross-platform determinism — endian-fixed binary framing, canonical JSON, zero PII
    with open(enc_path, "rb") as fh:
        parsed = _parse_and_validate_header(fh)
    hdr = parsed["header"]
    assert hdr["format_version"] == 1
    assert hdr["cipher"] == "AES-256-GCM"
    assert hdr["kdf"] == "HKDF-SHA256"
    assert hdr["compression"] == "zlib"
    assert hdr["key_id"] == get_recovery_key_id(rk)
    hdr_raw = json.dumps(hdr, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    # Zero PII in unencrypted header
    for forbidden_pii in ("Suresh Patil", "9822334455", "Mahadhan", "Baramati", str(tmp_path)):
        assert forbidden_pii not in hdr_raw

    # Test 5, 19, 20: Decrypt on a simulated replacement machine (separate temp dir, explicit RK only)
    replacement_dir = tmp_path / "replacement_pc"
    replacement_dir.mkdir()
    decrypted_db = replacement_dir / "restored_shop.db"
    dec_meta = EncryptedBackupService(db, backup_dir=replacement_dir).decrypt_backup(
        enc_path,
        decrypted_db,
        recovery_key=rk.lower().replace("-", " "),  # normalized lowercase + spaces
        verify_sqlite=True,
    )
    assert dec_meta["plaintext_sha256"] == src_bkp_hash_before
    assert _sha256_file(decrypted_db) == src_bkp_hash_before
    assert dec_meta["integrity_ok"] is True
    assert dec_meta["foreign_key_ok"] is True
    assert dec_meta["valid_for_restore"] is True

    # Test 21: Verify actual business records survived round-trip (sales, purchases, receipts, expenses, balances, stock)
    conn = sqlite3.connect(f"{decrypted_db.resolve().as_uri()}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("PRAGMA integrity_check;").fetchall()[0][0] == "ok"
        conn.execute("PRAGMA foreign_keys = ON;")
        assert conn.execute("PRAGMA foreign_key_check;").fetchall() == []

        assert conn.execute("SELECT net_amount FROM purchases WHERE purchase_id = ?;", (rec["pur_id"],)).fetchone()[0] == pytest.approx(4620.0)
        assert conn.execute("SELECT net_amount FROM sales WHERE sale_id = ?;", (rec["sale_id"],)).fetchone()[0] == pytest.approx(1330.0)
        assert conn.execute("SELECT current_qty FROM stock_batches WHERE batch_id = ?;", (rec["batch_id"],)).fetchone()[0] == pytest.approx(15.0)
        assert conn.execute("SELECT current_balance FROM customers WHERE customer_id = ?;", (rec["cust_id"],)).fetchone()[0] == pytest.approx(830.0)
        assert conn.execute("SELECT current_balance FROM suppliers WHERE supplier_id = ?;", (rec["supp_id"],)).fetchone()[0] == pytest.approx(3000.0)
        assert conn.execute("SELECT amount FROM expenses WHERE expense_id = ?;", (rec["exp_id"],)).fetchone()[0] == pytest.approx(350.0)
        assert conn.execute("SELECT total_amount FROM vouchers WHERE voucher_id = ?;", (rec["rcpt_id"],)).fetchone()[0] == pytest.approx(500.0)
        assert conn.execute("SELECT total_amount FROM vouchers WHERE voucher_id = ?;", (rec["spay_id"],)).fetchone()[0] == pytest.approx(1620.0)
    finally:
        conn.close()


# =====================================================================
# 6–14. Cryptographic Tamper, Truncation, Reorder, Trailing & Bomb Tests
# =====================================================================
def test_06_through_14_security_tamper_truncation_reorder_and_decompression_limits(
    isolated_env: Dict[str, Any],
):
    """Tests 6, 7, 8, 9, 10, 11, 12, 13, 14: Comprehensive adversarial archive rejection."""
    db: DatabaseManager = isolated_env["db"]
    src_bkp: Path = isolated_env["verified_db_backup"]
    tmp_path: Path = isolated_env["tmp_path"]
    out_dir = tmp_path / "tamper_work"
    out_dir.mkdir()

    rk = generate_recovery_key()
    wrong_rk = generate_recovery_key()
    enc_svc = EncryptedBackupService(db, backup_dir=isolated_env["backup_dir"], chunk_size=4096)

    enc_meta = enc_svc.encrypt_backup(src_bkp, out_dir / "valid.kdb.enc", recovery_key=rk)
    valid_path = Path(enc_meta["file_path"])
    assert enc_meta["chunk_count"] >= 2
    valid_bytes = valid_path.read_bytes()
    prefix, chunks = _parse_raw_kdb_chunks(valid_path)

    # Test 6: Wrong Recovery Key rejected
    with pytest.raises(EncryptedBackupAuthError):
        enc_svc.decrypt_backup(valid_path, out_dir / "out6.db", recovery_key=wrong_rk)
    assert not (out_dir / "out6.db").exists()

    # Test 7: Modified ciphertext rejected (flip 1 bit in chunk 0 ciphertext)
    tampered_ct_path = out_dir / "tampered_ct.kdb.enc"
    rec0_len, rec0_fin, rec0_ct = chunks[0]
    flipped_ct0 = bytes([rec0_ct[0] ^ 0x01]) + rec0_ct[1:]
    tampered_ct_path.write_bytes(
        prefix
        + struct.pack(">IB", rec0_len, rec0_fin)
        + flipped_ct0
        + b"".join(struct.pack(">IB", l, f) + c for l, f, c in chunks[1:])
    )
    with pytest.raises(EncryptedBackupAuthError):
        enc_svc.decrypt_backup(tampered_ct_path, out_dir / "out7.db", recovery_key=rk)

    # Test 8: Modified authenticated header rejected (flip schema_version byte in JSON while keeping canonical)
    hdr_len = struct.unpack(">I", valid_bytes[8:12])[0]
    orig_hdr = json.loads(valid_bytes[12 : 12 + hdr_len].decode("utf-8"))
    alt_hdr = dict(orig_hdr)
    alt_hdr["schema_version"] = orig_hdr["schema_version"] + 1
    alt_hdr_bytes = json.dumps(alt_hdr, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    tampered_hdr_path = out_dir / "tampered_hdr.kdb.enc"
    tampered_hdr_path.write_bytes(
        KDB_ENC_MAGIC + struct.pack(">I", len(alt_hdr_bytes)) + alt_hdr_bytes + valid_bytes[12 + hdr_len :]
    )
    with pytest.raises(EncryptedBackupAuthError):
        enc_svc.decrypt_backup(tampered_hdr_path, out_dir / "out8.db", recovery_key=rk)

    # Test 9: Reordered chunks rejected (swap chunk 0 and chunk 1)
    reordered_path = out_dir / "reordered.kdb.enc"
    swapped = [chunks[1], chunks[0], *chunks[2:]]
    reordered_path.write_bytes(
        prefix + b"".join(struct.pack(">IB", l, f) + c for l, f, c in swapped)
    )
    with pytest.raises(EncryptedBackupAuthError):
        enc_svc.decrypt_backup(reordered_path, out_dir / "out9.db", recovery_key=rk)

    # Test 10: Truncated archive rejected (cut mid-way through last chunk)
    trunc_path = out_dir / "truncated.kdb.enc"
    trunc_path.write_bytes(valid_bytes[:-25])
    with pytest.raises(EncryptedBackupFormatError):
        enc_svc.decrypt_backup(trunc_path, out_dir / "out10.db", recovery_key=rk)

    # Test 11: Missing final chunk rejected (keep only non-final chunks)
    no_final_path = out_dir / "missing_final.kdb.enc"
    no_final_path.write_bytes(
        prefix + b"".join(struct.pack(">IB", l, f) + c for l, f, c in chunks[:-1])
    )
    with pytest.raises(EncryptedBackupFormatError, match="final"):
        enc_svc.decrypt_backup(no_final_path, out_dir / "out11.db", recovery_key=rk)

    # Test 12: Trailing data after final chunk rejected
    trailing_path = out_dir / "trailing.kdb.enc"
    trailing_path.write_bytes(valid_bytes + b"\x00extra_garbage")
    with pytest.raises(EncryptedBackupFormatError, match="trailing"):
        enc_svc.decrypt_backup(trailing_path, out_dir / "out12.db", recovery_key=rk)

    # Test 13: Oversized header, bad magic, duplicate JSON keys, unexpected fields rejected
    oversized_path = out_dir / "oversized_hdr.kdb.enc"
    oversized_path.write_bytes(KDB_ENC_MAGIC + struct.pack(">I", MAX_HEADER_BYTES + 1) + b"{}")
    with pytest.raises(EncryptedBackupFormatError, match="bounds"):
        enc_svc.inspect_encrypted_backup(oversized_path)

    bad_magic_path = out_dir / "bad_magic.kdb.enc"
    bad_magic_path.write_bytes(b"NOTKDHN1" + valid_bytes[8:])
    with pytest.raises(EncryptedBackupFormatError, match="magic"):
        enc_svc.inspect_encrypted_backup(bad_magic_path)

    dup_json = b'{"format_version":1,"format_version":1}'
    dup_path = out_dir / "dup_keys.kdb.enc"
    dup_path.write_bytes(KDB_ENC_MAGIC + struct.pack(">I", len(dup_json)) + dup_json)
    with pytest.raises(EncryptedBackupFormatError, match="duplicate"):
        enc_svc.inspect_encrypted_backup(dup_path)

    extra_hdr = dict(orig_hdr, customer_name="Unexpected PII")
    extra_bytes = json.dumps(extra_hdr, sort_keys=True, separators=(",", ":")).encode("utf-8")
    extra_path = out_dir / "extra_field.kdb.enc"
    extra_path.write_bytes(KDB_ENC_MAGIC + struct.pack(">I", len(extra_bytes)) + extra_bytes)
    with pytest.raises(EncryptedBackupFormatError, match="unexpected"):
        enc_svc.inspect_encrypted_backup(extra_path)

    # Test 14: Decompression-limit protection rejects archive exceeding max_plaintext_bytes
    with pytest.raises(EncryptedBackupError, match="limit"):
        enc_svc.decrypt_backup(valid_path, out_dir / "out14.db", recovery_key=rk, max_plaintext_bytes=2048)
    assert not (out_dir / "out14.db").exists()


# =====================================================================
# 15, 16, 17, 18. Nonce Uniqueness, Interrupted Encryption & Safety
# =====================================================================
def test_15_16_17_18_fresh_key_material_interrupted_encryption_and_live_db_safety(
    isolated_env: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """Tests 15, 16, 17, 18: Unique DEK/salt/nonce per run, clean abort on interruption, original & live DB safe."""
    db: DatabaseManager = isolated_env["db"]
    db_file: Path = isolated_env["db_file"]
    src_bkp: Path = isolated_env["verified_db_backup"]
    tmp_path: Path = isolated_env["tmp_path"]
    out_dir = tmp_path / "uniqueness"
    out_dir.mkdir()

    rk = generate_recovery_key()
    enc_svc = EncryptedBackupService(db, backup_dir=isolated_env["backup_dir"], chunk_size=4096)

    # Test 15: Two encryptions of the exact same DB use different salts, nonce prefixes, wrap nonces & ciphertext
    m1 = enc_svc.encrypt_backup(src_bkp, out_dir / "run1.kdb.enc", recovery_key=rk)
    m2 = enc_svc.encrypt_backup(src_bkp, out_dir / "run2.kdb.enc", recovery_key=rk)
    with open(m1["file_path"], "rb") as f1, open(m2["file_path"], "rb") as f2:
        p1 = _parse_and_validate_header(f1)
        p2 = _parse_and_validate_header(f2)
    assert p1["file_salt"] != p2["file_salt"]
    assert p1["nonce_prefix"] != p2["nonce_prefix"]
    assert p1["wrap_nonce"] != p2["wrap_nonce"]
    assert p1["wrapped_box"] != p2["wrapped_box"]
    assert m1["encrypted_sha256"] != m2["encrypted_sha256"]

    # Tests 16 & 17: Interrupted encryption cleans up incomplete .tmp and preserves source .db backup untouched
    src_hash_before = _sha256_file(src_bkp)
    orig_compressobj = zlib.compressobj

    class _FailingCompressor:
        def __init__(self, *args, **kwargs):
            self._inner = orig_compressobj(*args, **kwargs)
            self._calls = 0

        def compress(self, data):
            self._calls += 1
            if self._calls >= 2:
                raise OSError("Simulated power loss mid-encryption")
            return self._inner.compress(data)

        def flush(self):
            return self._inner.flush()

    monkeypatch.setattr(zlib, "compressobj", lambda *a, **kw: _FailingCompressor(*a, **kw))
    aborted_target = out_dir / "aborted.kdb.enc"
    with pytest.raises(OSError, match="Simulated power loss"):
        enc_svc.encrypt_backup(src_bkp, aborted_target, recovery_key=rk)

    assert not aborted_target.exists()
    assert [p for p in out_dir.iterdir() if ".tmp" in p.name] == []
    assert _sha256_file(src_bkp) == src_hash_before

    # Test 18: Failed decryption never damages live DB and decrypt refuses live DB path directly
    live_hash_before = _sha256_file(db_file)
    with pytest.raises(ValueError, match="live database"):
        enc_svc.decrypt_backup(Path(m1["file_path"]), db_file, recovery_key=rk)
    assert _sha256_file(db_file) == live_hash_before


# =====================================================================
# 24 & 25. Offline Encrypted Export, Inspect, Download & Restore APIs
# =====================================================================
def test_24_and_25_encrypted_backup_api_auth_validation_restore_and_rollback(
    isolated_env: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """Tests 24, 25 + API RBAC, upload validation, confirmation, secret non-logging & rollback."""
    db: DatabaseManager = isolated_env["db"]
    db_file: Path = isolated_env["db_file"]
    backup_dir: Path = isolated_env["backup_dir"]
    src_name = isolated_env["verified_db_backup"].name
    admin_hdr = isolated_env["admin_headers"]
    op_hdr = isolated_env["operator_headers"]

    rk = generate_recovery_key()
    wrong_rk = generate_recovery_key()
    caplog.set_level(logging.DEBUG)

    with TestClient(app) as client:
        # 1. RBAC: Unauthenticated -> 401; Operator -> 403
        assert client.post("/api/system/backups/encrypt", json={"backup_filename": src_name, "recovery_key": rk}).status_code == 401
        assert client.post("/api/system/backups/encrypt", headers=op_hdr, json={"backup_filename": src_name, "recovery_key": rk}).status_code == 403

        # 2. Encrypt verified backup via Admin POST body (never query string)
        res_enc = client.post(
            "/api/system/backups/encrypt",
            headers=admin_hdr,
            json={"backup_filename": src_name, "recovery_key": rk},
        )
        assert res_enc.status_code == 200, res_enc.text
        enc_data = res_enc.json()
        assert enc_data["success"] is True
        enc_name = enc_data["filename"]
        assert enc_name.endswith(".kdb.enc")
        assert rk not in res_enc.text  # Recovery Key NEVER echoed back

        # 3. Download .kdb.enc archive
        res_dl = client.get(f"/api/system/backups/encrypted/{enc_name}/download", headers=admin_hdr)
        assert res_dl.status_code == 200
        assert res_dl.content.startswith(KDB_ENC_MAGIC)
        assert res_dl.headers.get("x-content-type-options") == "nosniff"
        assert res_dl.headers.get("cache-control") == "no-store"
        enc_bytes = res_dl.content

        # Operator cannot download encrypted archive
        assert client.get(f"/api/system/backups/encrypted/{enc_name}/download", headers=op_hdr).status_code == 403

        # 4. Inspect uploaded .kdb.enc (header-only & authenticated with Recovery Key)
        mp_hdr, ct_hdr = _multipart_body("file", enc_name, enc_bytes)
        res_insp_hdr = client.post(
            "/api/system/backups/encrypted/inspect-upload",
            headers={**admin_hdr, "Content-Type": ct_hdr},
            content=mp_hdr,
        )
        assert res_insp_hdr.status_code == 200
        assert res_insp_hdr.json()["header_valid"] is True
        assert res_insp_hdr.json()["authenticated"] is False
        assert res_insp_hdr.json()["key_id"] == get_recovery_key_id(rk)

        mp_auth, ct_auth = _multipart_body("file", enc_name, enc_bytes, {"recovery_key": rk})
        res_insp_auth = client.post(
            "/api/system/backups/encrypted/inspect-upload",
            headers={**admin_hdr, "Content-Type": ct_auth},
            content=mp_auth,
        )
        assert res_insp_auth.status_code == 200
        auth_info = res_insp_auth.json()
        assert auth_info["authenticated"] is True
        assert auth_info["valid_for_restore"] is True
        assert auth_info["integrity_ok"] is True
        assert auth_info["foreign_key_ok"] is True

        # Reject bad extension, non-KDHNENC1 magic & oversized upload
        bad_mp, bad_ct = _multipart_body("file", "evil.db", enc_bytes)
        assert client.post("/api/system/backups/encrypted/inspect-upload", headers={**admin_hdr, "Content-Type": bad_ct}, content=bad_mp).status_code == 400
        bad_magic_mp, bad_magic_ct = _multipart_body("file", "fake.kdb.enc", b"NOT_ENCRYPTED_ARCHIVE" * 10)
        assert client.post("/api/system/backups/encrypted/inspect-upload", headers={**admin_hdr, "Content-Type": bad_magic_ct}, content=bad_magic_mp).status_code == 400

        with monkeypatch.context() as mp:
            mp.setattr("src.api.routes_system.MAX_ENCRYPTED_UPLOAD_BYTES", 512)
            over_mp, over_ct = _multipart_body("file", enc_name, enc_bytes)
            res_over = client.post("/api/system/backups/encrypted/inspect-upload", headers={**admin_hdr, "Content-Type": over_ct}, content=over_mp)
            assert res_over.status_code == 400
            assert "exceeds maximum allowed size" in res_over.json()["detail"]

        # Mutate live DB after encrypted archive was created
        master = MasterDataRepository(db)
        post_pid = master.create_product(Product(product_name="Post-Encrypt Ephemeral Product", default_sale_rate=777.0))
        assert db.fetch_one("SELECT 1 FROM products WHERE product_id = ?;", (post_pid,)) is not None

        # 5. Restore without confirm_restore=true MUST be rejected (HTTP 400)
        mp_unconf, ct_unconf = _multipart_body("file", enc_name, enc_bytes, {"recovery_key": rk, "confirm_restore": "false"})
        assert client.post(
            "/api/system/backups/encrypted/restore-upload",
            headers={**admin_hdr, "Content-Type": ct_unconf},
            content=mp_unconf,
        ).status_code == 400
        assert db.fetch_one("SELECT 1 FROM products WHERE product_id = ?;", (post_pid,)) is not None

        # Test 24: Restore with WRONG Recovery Key rejected without modifying live DB
        mp_wrong, ct_wrong = _multipart_body("file", enc_name, enc_bytes, {"recovery_key": wrong_rk, "confirm_restore": "true"})
        res_wrong = client.post(
            "/api/system/backups/encrypted/restore-upload",
            headers={**admin_hdr, "Content-Type": ct_wrong},
            content=mp_wrong,
        )
        assert res_wrong.status_code == 400
        assert wrong_rk not in res_wrong.text
        assert db.fetch_one("SELECT 1 FROM products WHERE product_id = ?;", (post_pid,)) is not None

        # Test 25a: Confirmed restore with CORRECT Recovery Key succeeds, reverts post_pid & creates BEFORE_RESTORE
        mp_ok, ct_ok = _multipart_body("file", enc_name, enc_bytes, {"recovery_key": rk, "confirm_restore": "true"})
        res_ok = client.post(
            "/api/system/backups/encrypted/restore-upload",
            headers={**admin_hdr, "Content-Type": ct_ok},
            content=mp_ok,
        )
        assert res_ok.status_code == 200, res_ok.text
        ok_data = res_ok.json()
        assert ok_data["success"] is True
        assert ok_data["pre_restore_filename"]
        pre_restore_path = backup_dir / ok_data["pre_restore_filename"]
        assert pre_restore_path.is_file()
        assert db.fetch_one("SELECT 1 FROM products WHERE product_id = ?;", (post_pid,)) is None
        assert db.fetch_one("SELECT net_amount FROM sales WHERE sale_id = ?;", (isolated_env["records"]["sale_id"],))[0] == pytest.approx(1330.0)

        # Test 25b: Forced post-swap failure during encrypted restore triggers automatic rollback preserving live DB
        rollback_pid = master.create_product(Product(product_name="Must Survive Failed Encrypted Restore", default_sale_rate=999.0))
        orig_verify = BackupService.verify_database_integrity

        def _fail_live_verify(self_bkp, db_file_path=None, check_foreign_keys=False):
            if db_file_path is not None and Path(db_file_path).resolve() == db_file.resolve():
                return False
            return orig_verify(self_bkp, db_file_path=db_file_path, check_foreign_keys=check_foreign_keys)

        monkeypatch.setattr(BackupService, "verify_database_integrity", _fail_live_verify)
        mp_rb, ct_rb = _multipart_body("file", enc_name, enc_bytes, {"recovery_key": rk, "confirm_restore": "true"})
        res_rb = client.post(
            "/api/system/backups/encrypted/restore-upload",
            headers={**admin_hdr, "Content-Type": ct_rb},
            content=mp_rb,
        )
        assert res_rb.status_code == 500
        assert db.fetch_one("SELECT product_name FROM products WHERE product_id = ?;", (rollback_pid,))[0] == (
            "Must Survive Failed Encrypted Restore"
        )

    # Confirm ZERO Recovery Keys leaked into logs, SQLite DB, or backup manifest!
    for record in caplog.records:
        assert rk not in record.getMessage()
        assert wrong_rk not in record.getMessage()
    manifest_path = backup_dir / BackupService.MANIFEST_FILENAME
    if manifest_path.exists():
        m_text = manifest_path.read_text(encoding="utf-8")
        assert rk not in m_text
        assert wrong_rk not in m_text
    raw_db_bytes = db_file.read_bytes()
    assert rk.encode("utf-8") not in raw_db_bytes
    assert wrong_rk.encode("utf-8") not in raw_db_bytes


def test_26_additional_adversarial_vectors_and_422_secret_redaction(
    isolated_env: Dict[str, Any],
):
    """
    Explicitly verify:
    - Modified wrapped-key data & wrap_nonce rejected.
    - Duplicate chunks rejected.
    - Multiple final chunks rejected.
    - Invalid chunk length & 32-bit chunk-index overflow rejected.
    - True high-ratio zlib compression bomb rejected during streaming decompression + max_compressed_bytes cap.
    - Corrupted SQLite payload inside validly authenticated AEAD archive rejected by verify_sqlite=True.
    - Never silently overwrites an existing .kdb.enc archive.
    - Unauthenticated inspect_encrypted_backup() explicitly sets metadata_authenticated=False.
    - FastAPI HTTP 422 RequestValidationError NEVER echoes Recovery Key in response body.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    db: DatabaseManager = isolated_env["db"]
    src_bkp: Path = isolated_env["verified_db_backup"]
    tmp_path: Path = isolated_env["tmp_path"]
    out_dir = tmp_path / "adv_extra"
    out_dir.mkdir()

    rk = generate_recovery_key()
    raw_rk = validate_recovery_key(rk)
    enc_svc = EncryptedBackupService(db, backup_dir=isolated_env["backup_dir"], chunk_size=4096)

    valid_path = out_dir / "base.kdb.enc"
    enc_meta = enc_svc.encrypt_backup(src_bkp, valid_path, recovery_key=rk)
    assert enc_meta["chunk_count"] >= 2
    valid_bytes = valid_path.read_bytes()
    prefix, chunks = _parse_raw_kdb_chunks(valid_path)

    # 1. Never silently overwrite existing .kdb.enc (explicit output_path raises FileExistsError)
    base_hash = _sha256_file(valid_path)
    with pytest.raises(FileExistsError):
        enc_svc.encrypt_backup(src_bkp, valid_path, recovery_key=rk)
    assert _sha256_file(valid_path) == base_hash

    # Default output_path=None creates unique non-colliding .kdb.enc files
    auto1 = Path(enc_svc.encrypt_backup(src_bkp, recovery_key=rk)["file_path"])
    auto2 = Path(enc_svc.encrypt_backup(src_bkp, recovery_key=rk)["file_path"])
    assert auto1 != auto2 and auto1.is_file() and auto2.is_file()

    # 2. Unauthenticated inspect reports metadata_authenticated=False explicitly
    unauth_info = enc_svc.inspect_encrypted_backup(valid_path)
    assert unauth_info["authenticated"] is False
    assert unauth_info["metadata_authenticated"] is False

    # 3. Modified wrapped-key box & modified wrap_nonce rejected
    tampered_wrap = out_dir / "tampered_wrap.kdb.enc"
    tampered_prefix = prefix[:-1] + bytes([prefix[-1] ^ 0xFF])
    tampered_wrap.write_bytes(tampered_prefix + valid_bytes[len(prefix) :])
    with pytest.raises(EncryptedBackupAuthError):
        enc_svc.decrypt_backup(tampered_wrap, out_dir / "o_wrap.db", recovery_key=rk)

    # 4. Duplicate chunks rejected ([chunk0, chunk0, ...])
    dup_chunk_path = out_dir / "dup_chunk.kdb.enc"
    dup_chunks = [chunks[0], chunks[0], *chunks[1:]]
    dup_chunk_path.write_bytes(prefix + b"".join(struct.pack(">IB", l, f) + c for l, f, c in dup_chunks))
    with pytest.raises(EncryptedBackupAuthError):
        enc_svc.decrypt_backup(dup_chunk_path, out_dir / "o_dup.db", recovery_key=rk)

    # 5. Multiple final chunks rejected (append another chunk record after the valid final chunk)
    multi_final_path = out_dir / "multi_final.kdb.enc"
    multi_final_path.write_bytes(valid_bytes + struct.pack(">IB", chunks[-1][0], 1) + chunks[-1][2])
    with pytest.raises(EncryptedBackupFormatError, match="trailing"):
        enc_svc.decrypt_backup(multi_final_path, out_dir / "o_mf.db", recovery_key=rk)

    # 6. Invalid chunk lengths (< 16 and > chunk_size + 16) & 32-bit chunk-index overflow rejected
    short_len_path = out_dir / "short_len.kdb.enc"
    short_len_path.write_bytes(prefix + struct.pack(">IB", 8, 1) + b"\x00" * 8)
    with pytest.raises(EncryptedBackupFormatError, match="bounds"):
        enc_svc.decrypt_backup(short_len_path, out_dir / "o_sl.db", recovery_key=rk)

    huge_len_path = out_dir / "huge_len.kdb.enc"
    huge_len_path.write_bytes(prefix + struct.pack(">IB", 4096 + 100, 1) + b"\x00" * 32)
    with pytest.raises(EncryptedBackupFormatError, match="bounds"):
        enc_svc.decrypt_backup(huge_len_path, out_dir / "o_hl.db", recovery_key=rk)

    with pytest.raises(EncryptedBackupFormatError, match="32-bit"):
        _chunk_nonce(b"\x01" * 8, MAX_CHUNK_INDEX + 1)

    # 7. Max compressed size cap enforced on decrypt
    with pytest.raises(EncryptedBackupError, match="compressed size"):
        enc_svc.decrypt_backup(valid_path, out_dir / "o_comp.db", recovery_key=rk, max_compressed_bytes=1024)

    # 8. True high-ratio zlib compression bomb (2 MiB zeros -> ~2 KiB compressed) AND corrupt SQLite payload
    def _craft_archive(raw_payload: bytes, declared_size: int | None = None) -> Path:
        comp = zlib.compress(raw_payload, level=9)
        dek = os.urandom(32)
        salt = os.urandom(16)
        nprefix = os.urandom(8)
        wnonce = os.urandom(12)
        hdr_obj = {
            "chunk_size": 1024 * 1024,
            "cipher": "AES-256-GCM",
            "compression": "zlib",
            "created_at_utc": "2026-11-18T10:00:00Z",
            "file_salt_b64": __import__("base64").b64encode(salt).decode("ascii"),
            "format_version": 1,
            "kdf": "HKDF-SHA256",
            "key_id": get_recovery_key_id(rk),
            "nonce_prefix_b64": __import__("base64").b64encode(nprefix).decode("ascii"),
            "schema_version": 6,
        }
        hdr_bytes = json.dumps(hdr_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        env = KDB_ENC_MAGIC + struct.pack(">I", len(hdr_bytes)) + hdr_bytes
        hsha = hashlib.sha256(env).digest()
        kek = _derive_kek(raw_rk, salt)
        decl = len(raw_payload) if declared_size is None else declared_size
        wbox = AESGCM(kek).encrypt(wnonce, dek + hashlib.sha256(raw_payload).digest() + struct.pack(">Q", decl), env)
        rec_len = len(comp) + 16
        ct = AESGCM(dek).encrypt(_chunk_nonce(nprefix, 0), comp, _chunk_aad(hsha, 0, 1, rec_len))
        p = out_dir / f"crafted_{secrets.token_hex(4)}.kdb.enc"
        p.write_bytes(env + wnonce + struct.pack(">H", len(wbox)) + wbox + struct.pack(">IB", len(ct), 1) + ct)
        return p

    # Bomb claiming small declared size (4096) while zlib stream expands to 2 MiB -> aborted DURING decompression!
    bomb_path = _craft_archive(b"\x00" * (2 * 1024 * 1024), declared_size=4096)
    with pytest.raises(EncryptedBackupError, match="decompressed size"):
        enc_svc.decrypt_backup(bomb_path, out_dir / "o_bomb.db", recovery_key=rk, verify_sqlite=False, max_plaintext_bytes=65536)
    assert not (out_dir / "o_bomb.db").exists()

    # Corrupt non-SQLite plaintext inside validly authenticated AEAD archive -> rejected by verify_sqlite=True!
    corrupt_sql_path = _craft_archive(b"SQLite format 3\x00" + b"\xff" * 4096)
    with pytest.raises(EncryptedBackupError, match="integrity_check"):
        enc_svc.decrypt_backup(corrupt_sql_path, out_dir / "o_corrupt_sql.db", recovery_key=rk, verify_sqlite=True)
    assert not (out_dir / "o_corrupt_sql.db").exists()

    # 9. FastAPI HTTP 422 RequestValidationError MUST NOT echo Recovery Key in response body
    with TestClient(app) as client:
        res_422 = client.post(
            "/api/system/backups/encrypt",
            headers=isolated_env["admin_headers"],
            json={"recovery_key": rk},  # intentionally omit required backup_filename to trigger 422
        )
        assert res_422.status_code == 422
        assert rk not in res_422.text


# =====================================================================
# 27. Encrypted Archive & Upload Size-Limit Consistency + Streaming Bounds
# =====================================================================
def test_27_encrypted_archive_size_limit_consistency_and_streaming_bounds(
    isolated_env: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Verify Phase 2C-4B size-limit consistency correction:
    - Derived MAX_ENCRYPTED_ARCHIVE_BYTES == MAX_ENCRYPTED_UPLOAD_BYTES accounts for
      fixed envelope, max header (64 KiB), key wrap, max compressed payload (512 MiB),
      worst-case chunk count at MIN_CHUNK_SIZE (4096), per-record framing (5B) + GCM tag (16B),
      plus 1 permitted final empty chunk.
    - Archives > 250 MiB (former cap) within the real derived limit are accepted while streaming.
    - Valid archive at exact configured maximum succeeds across service + inspect-upload + restore-upload.
    - Archive 1 byte over configured maximum is rejected identically across service + inspect-upload + restore-upload.
    - Uploads remain streamed in bounded chunks, never trust spoofed Content-Length, and always clean up staging files.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from src.api import routes_system

    db: DatabaseManager = isolated_env["db"]
    src_bkp: Path = isolated_env["verified_db_backup"]
    tmp_path: Path = isolated_env["tmp_path"]
    admin_hdr = isolated_env["admin_headers"]
    out_dir = tmp_path / "size_limit_checks"
    out_dir.mkdir()

    # 1. Exact formula derivation directly from actual wire struct formats
    assert HEADER_LEN_STRUCT_FMT == ">I"
    assert WRAP_LEN_STRUCT_FMT == ">H"
    assert CHUNK_RECORD_PREFIX_STRUCT_FMT == ">IB"  # actual on-disk prefix: record_len(4) + is_final(1)
    assert CHUNK_AAD_STRUCT_FMT == ">IBI"  # bound in AEAD AAD (chunk_index + is_final + record_len), not written on disk
    assert HEADER_LEN_PREFIX_BYTES == struct.calcsize(HEADER_LEN_STRUCT_FMT) == 4
    assert WRAP_LEN_PREFIX_BYTES == struct.calcsize(WRAP_LEN_STRUCT_FMT) == 2
    assert CHUNK_RECORD_PREFIX_BYTES == struct.calcsize(CHUNK_RECORD_PREFIX_STRUCT_FMT) == 5
    assert GCM_TAG_BYTES == 16
    assert CHUNK_RECORD_OVERHEAD_BYTES == CHUNK_RECORD_PREFIX_BYTES + GCM_TAG_BYTES == 21

    fixed_envelope = (
        len(KDB_ENC_MAGIC)
        + HEADER_LEN_PREFIX_BYTES
        + MAX_HEADER_BYTES
        + WRAP_NONCE_BYTES
        + WRAP_LEN_PREFIX_BYTES
        + EXPECTED_WRAP_BOX_BYTES
    )
    assert fixed_envelope == 65_650

    expected_chunks = math.ceil(DEFAULT_MAX_COMPRESSED_BYTES / MIN_CHUNK_SIZE) + 1
    assert expected_chunks == 131_073
    expected_default_max = (
        fixed_envelope
        + DEFAULT_MAX_COMPRESSED_BYTES
        + (expected_chunks * CHUNK_RECORD_OVERHEAD_BYTES)
    )
    assert expected_default_max == 539_689_095

    assert MAX_ENCRYPTED_ARCHIVE_BYTES == expected_default_max
    assert MAX_ENCRYPTED_UPLOAD_BYTES == MAX_ENCRYPTED_ARCHIVE_BYTES
    assert routes_system.MAX_ENCRYPTED_UPLOAD_BYTES == MAX_ENCRYPTED_ARCHIVE_BYTES
    assert MAX_ENCRYPTED_UPLOAD_BYTES > 250 * 1024 * 1024  # strictly exceeds former 250 MiB mismatch
    assert MAX_ENCRYPTED_UPLOAD_BYTES > DEFAULT_MAX_COMPRESSED_BYTES

    # Smaller chunk size yields strictly larger max due to more per-chunk framing+tag records
    max_at_4k = calculate_max_encrypted_archive_bytes(max_compressed_bytes=16_384, min_chunk_size=MIN_CHUNK_SIZE)
    max_at_1m = calculate_max_encrypted_archive_bytes(max_compressed_bytes=16_384, min_chunk_size=1024 * 1024)
    assert max_at_4k > max_at_1m
    assert max_at_4k - max_at_1m == (4 - 1) * CHUNK_RECORD_OVERHEAD_BYTES  # 3 extra chunks * 21B

    # Zero compressed payload still reserves 1 final empty chunk record
    assert calculate_max_encrypted_archive_bytes(max_compressed_bytes=0) == fixed_envelope + CHUNK_RECORD_OVERHEAD_BYTES

    # Exact worst-case valid archive at MIN_CHUNK_SIZE with full chunks + 1 trailing empty is_final=1 chunk,
    # verified to BOTH match calculate_max_encrypted_archive_bytes() AND actually decrypt cleanly!
    rk = generate_recovery_key()
    raw_rk = validate_recovery_key(rk)
    salt = os.urandom(16)
    nprefix = os.urandom(8)
    wnonce = os.urandom(12)
    dek = os.urandom(32)
    # Build a valid zlib stream whose compressed length is >= 2 * MIN_CHUNK_SIZE (use random data)
    synth_plain = os.urandom(9000)
    synth_comp = zlib.compress(synth_plain, level=6)
    assert len(synth_comp) > 2 * MIN_CHUNK_SIZE
    hdr_obj = {
        "chunk_size": MIN_CHUNK_SIZE,
        "cipher": "AES-256-GCM",
        "compression": "zlib",
        "created_at_utc": "2026-11-18T11:00:00Z",
        "file_salt_b64": __import__("base64").b64encode(salt).decode("ascii"),
        "format_version": 1,
        "kdf": "HKDF-SHA256",
        "key_id": get_recovery_key_id(rk),
        "nonce_prefix_b64": __import__("base64").b64encode(nprefix).decode("ascii"),
        "schema_version": 6,
    }
    hdr_bytes = json.dumps(hdr_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    env = KDB_ENC_MAGIC + struct.pack(HEADER_LEN_STRUCT_FMT, len(hdr_bytes)) + hdr_bytes
    hsha = hashlib.sha256(env).digest()
    kek = _derive_kek(raw_rk, salt)
    wbox = AESGCM(kek).encrypt(wnonce, dek + hashlib.sha256(synth_plain).digest() + struct.pack(">Q", len(synth_plain)), env)
    aead = AESGCM(dek)
    # Split compressed stream into MIN_CHUNK_SIZE chunks + append a valid empty is_final=1 chunk
    chunk_records = bytearray()
    idx = 0
    for pos in range(0, len(synth_comp), MIN_CHUNK_SIZE):
        piece = synth_comp[pos : pos + MIN_CHUNK_SIZE]
        ct_piece = aead.encrypt(_chunk_nonce(nprefix, idx), piece, _chunk_aad(hsha, idx, 0, len(piece) + GCM_TAG_BYTES))
        chunk_records.extend(struct.pack(CHUNK_RECORD_PREFIX_STRUCT_FMT, len(ct_piece), 0) + ct_piece)
        idx += 1
    ct_empty_final = aead.encrypt(_chunk_nonce(nprefix, idx), b"", _chunk_aad(hsha, idx, 1, GCM_TAG_BYTES))
    chunk_records.extend(struct.pack(CHUNK_RECORD_PREFIX_STRUCT_FMT, len(ct_empty_final), 1) + ct_empty_final)
    idx += 1  # total chunks == ceil(len(synth_comp) / MIN_CHUNK_SIZE) + 1

    exact_worst_case_archive = (
        env
        + wnonce
        + struct.pack(WRAP_LEN_STRUCT_FMT, len(wbox))
        + wbox
        + bytes(chunk_records)
    )
    exact_formula_size = calculate_max_encrypted_archive_bytes(
        max_compressed_bytes=len(synth_comp),
        min_chunk_size=MIN_CHUNK_SIZE,
        max_header_bytes=len(hdr_bytes),
    )
    assert len(exact_worst_case_archive) == exact_formula_size

    # Prove decrypt_backup genuinely accepts the trailing empty final chunk at exact_formula_size!
    empty_final_path = out_dir / "with_trailing_empty_final.kdb.enc"
    empty_final_path.write_bytes(exact_worst_case_archive)
    svc_default = EncryptedBackupService(db, backup_dir=isolated_env["backup_dir"])
    dec_empty_final = svc_default.decrypt_backup(
        empty_final_path,
        out_dir / "dec_empty_final.bin",
        recovery_key=rk,
        verify_sqlite=False,
        max_compressed_bytes=len(synth_comp),
        max_archive_bytes=exact_formula_size,
    )
    assert dec_empty_final["chunk_count"] == idx
    assert (out_dir / "dec_empty_final.bin").read_bytes() == synth_plain

    # 2. Prove archives > former 250 MiB cap (260 MiB) are accepted via real default streaming upload cap,
    #    while MAX_ENCRYPTED_ARCHIVE_BYTES + 1 is rejected immediately without buffering into memory.
    class _StreamingSyntheticUpload:
        def __init__(self, total_bytes: int, *, filename: str = "large_backup.kdb.enc", fail_after: int | None = None):
            self.filename = filename
            self.size = 128  # Intentionally spoofed tiny Content-Length / size to prove it is ignored!
            self._remaining = total_bytes
            self._sent = 0
            self._fail_after = fail_after
            self.read_sizes: List[int] = []

        async def read(self, size: int = -1) -> bytes:
            assert 0 < size <= 65536, f"Expected bounded streaming read, got size={size}"
            self.read_sizes.append(size)
            if self._fail_after is not None and self._sent >= self._fail_after:
                raise OSError("Simulated mid-upload connection reset")
            if self._remaining <= 0:
                return b""
            n = min(size, self._remaining)
            if self._sent == 0:
                # Start with valid KDHNENC1 magic
                chunk = (KDB_ENC_MAGIC + b"\x00" * max(0, n - len(KDB_ENC_MAGIC)))[:n]
            else:
                chunk = b"\x00" * n
            self._sent += n
            self._remaining -= n
            return chunk

    class _NullSink:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def write(self, data: bytes) -> int:
            return len(data)

    real_open = builtins.open
    created_stage_dirs: List[Path] = []
    real_mktemp_dir = routes_system._create_restricted_temp_dir

    def _tracking_temp_dir(prefix: str = "kdb_enc_"):
        d = real_mktemp_dir(prefix=prefix)
        created_stage_dirs.append(d)
        return d

    def _null_for_large_stage(file, mode="r", *args, **kwargs):
        if "w" in mode and str(file).endswith("large_260mb.kdb.enc"):
            Path(file).touch()
            return _NullSink()
        return real_open(file, mode, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(routes_system, "_create_restricted_temp_dir", _tracking_temp_dir)
        mp.setattr(builtins, "open", _null_for_large_stage)

        # 2a. 260 MiB (> former 250 MiB limit, < derived 539,689,095 byte limit) ACCEPTED!
        upload_260m = _StreamingSyntheticUpload(260 * 1024 * 1024, filename="large_260mb.kdb.enc")
        tdir, spath, sname = asyncio.run(routes_system._stage_uploaded_encrypted_file(upload_260m))
        assert sname == "large_260mb.kdb.enc"
        assert upload_260m._sent == 260 * 1024 * 1024
        # 4160 full 64 KiB reads + 1 final EOF read
        assert len(upload_260m.read_sizes) == ((260 * 1024 * 1024) // 65536) + 1
        assert max(upload_260m.read_sizes) == 65536
        shutil.rmtree(tdir, ignore_errors=True)

        # 2b. Exact derived maximum (MAX_ENCRYPTED_ARCHIVE_BYTES == 539,689,095 bytes) ACCEPTED!
        upload_exact_max = _StreamingSyntheticUpload(MAX_ENCRYPTED_ARCHIVE_BYTES, filename="large_260mb.kdb.enc")
        tdir_max, _, _ = asyncio.run(routes_system._stage_uploaded_encrypted_file(upload_exact_max))
        assert upload_exact_max._sent == MAX_ENCRYPTED_ARCHIVE_BYTES
        shutil.rmtree(tdir_max, ignore_errors=True)

        # 2c. Exact MAX_ENCRYPTED_ARCHIVE_BYTES + 1 REJECTED immediately while streaming & staging cleaned up!
        created_stage_dirs.clear()
        upload_over_one = _StreamingSyntheticUpload(
            MAX_ENCRYPTED_ARCHIVE_BYTES + 1,
            filename="large_260mb.kdb.enc",
        )
        with pytest.raises(HTTPException) as exc_over_one:
            asyncio.run(routes_system._stage_uploaded_encrypted_file(upload_over_one))
        assert exc_over_one.value.status_code == 400
        assert "exceeds maximum allowed size" in exc_over_one.value.detail
        assert created_stage_dirs and all(not d.exists() for d in created_stage_dirs)

        # 2d. Oversized stream (MAX + 1 MiB) stops immediately on first crossing chunk & cleans up staging!
        created_stage_dirs.clear()
        upload_over_real = _StreamingSyntheticUpload(
            MAX_ENCRYPTED_ARCHIVE_BYTES + 1024 * 1024,
            filename="large_260mb.kdb.enc",
        )
        with pytest.raises(HTTPException) as exc_over:
            asyncio.run(routes_system._stage_uploaded_encrypted_file(upload_over_real))
        assert exc_over.value.status_code == 400
        assert "exceeds maximum allowed size" in exc_over.value.detail
        # Stopped immediately upon crossing limit (did NOT consume the remaining ~1 MiB)
        assert upload_over_real._remaining > 0
        assert created_stage_dirs and all(not d.exists() for d in created_stage_dirs)

        # 2e. Mid-upload read failure cleans up incomplete staging directory!
        created_stage_dirs.clear()
        upload_err = _StreamingSyntheticUpload(200_000, filename="mid_fail.kdb.enc", fail_after=65536)
        with pytest.raises(OSError, match="connection reset"):
            asyncio.run(routes_system._stage_uploaded_encrypted_file(upload_err))
        assert created_stage_dirs and all(not d.exists() for d in created_stage_dirs)

    # 3. Real encrypted SQLite backup (at MIN_CHUNK_SIZE=4096):
    #    prove actual on-disk bytes match exact wire framing formula byte-for-byte, AND
    #    prove exact boundary vs +1 byte over enforced identically across service, inspect-upload, and restore-upload!
    enc_svc = EncryptedBackupService(db, backup_dir=isolated_env["backup_dir"], chunk_size=MIN_CHUNK_SIZE)
    enc_path = out_dir / "min_chunk_real.kdb.enc"
    enc_meta = enc_svc.encrypt_backup(src_bkp, enc_path, recovery_key=rk)
    enc_bytes = enc_path.read_bytes()
    exact_size = len(enc_bytes)
    assert enc_meta["chunk_count"] >= 2
    assert exact_size == enc_meta["size_bytes"]

    # Measure actual on-disk envelope + chunk payload + chunk overhead of encrypt_backup() output
    actual_prefix, actual_chunks = _parse_raw_kdb_chunks(enc_path)
    actual_compressed_total = sum(rec_len - GCM_TAG_BYTES for rec_len, _, _ in actual_chunks)
    assert len(actual_chunks) == enc_meta["chunk_count"]
    assert exact_size == len(actual_prefix) + actual_compressed_total + (len(actual_chunks) * CHUNK_RECORD_OVERHEAD_BYTES)

    # 3a. Service inspect & decrypt at exact_size SUCCEED, and at exact_size - 1 FAIL
    insp_exact = enc_svc.inspect_encrypted_backup(enc_path, recovery_key=rk, max_archive_bytes=exact_size)
    assert insp_exact["valid_for_restore"] is True
    dec_exact = enc_svc.decrypt_backup(enc_path, out_dir / "dec_exact.db", recovery_key=rk, max_archive_bytes=exact_size)
    assert dec_exact["valid_for_restore"] is True

    with pytest.raises(EncryptedBackupError, match="exceeds maximum allowed size"):
        enc_svc.inspect_encrypted_backup(enc_path, max_archive_bytes=exact_size - 1)
    with pytest.raises(EncryptedBackupError, match="exceeds maximum allowed size"):
        enc_svc.decrypt_backup(enc_path, out_dir / "dec_over.db", recovery_key=rk, max_archive_bytes=exact_size - 1)
    assert not (out_dir / "dec_over.db").exists()

    # 3b. API inspect-upload and restore-upload apply the exact same boundary & clean up staging on failure
    with TestClient(app) as client:
        # At exact limit -> BOTH inspect-upload and restore-upload SUCCEED
        with monkeypatch.context() as mp:
            mp.setattr(routes_system, "MAX_ENCRYPTED_UPLOAD_BYTES", exact_size)
            assert routes_system._get_max_encrypted_upload_bytes(enc_svc) == exact_size

            mp_insp, ct_insp = _multipart_body("file", enc_path.name, enc_bytes, {"recovery_key": rk})
            res_insp_ok = client.post(
                "/api/system/backups/encrypted/inspect-upload",
                headers={**admin_hdr, "Content-Type": ct_insp},
                content=mp_insp,
            )
            assert res_insp_ok.status_code == 200, res_insp_ok.text
            assert res_insp_ok.json()["valid_for_restore"] is True

            mp_rest, ct_rest = _multipart_body(
                "file",
                enc_path.name,
                enc_bytes,
                {"recovery_key": rk, "confirm_restore": "true"},
            )
            res_rest_ok = client.post(
                "/api/system/backups/encrypted/restore-upload",
                headers={**admin_hdr, "Content-Type": ct_rest},
                content=mp_rest,
            )
            assert res_rest_ok.status_code == 200, res_rest_ok.text
            assert res_rest_ok.json()["success"] is True

        # 1 byte over limit -> BOTH inspect-upload and restore-upload REJECT cleanly and wipe staging dirs!
        created_stage_dirs.clear()
        with monkeypatch.context() as mp:
            mp.setattr(routes_system, "MAX_ENCRYPTED_UPLOAD_BYTES", exact_size - 1)
            mp.setattr(routes_system, "_create_restricted_temp_dir", _tracking_temp_dir)
            assert routes_system._get_max_encrypted_upload_bytes(enc_svc) == exact_size - 1

            mp_insp_over, ct_insp_over = _multipart_body("file", enc_path.name, enc_bytes, {"recovery_key": rk})
            res_insp_over = client.post(
                "/api/system/backups/encrypted/inspect-upload",
                headers={**admin_hdr, "Content-Type": ct_insp_over},
                content=mp_insp_over,
            )
            assert res_insp_over.status_code == 400
            assert "exceeds maximum allowed size" in res_insp_over.json()["detail"]

            mp_rest_over, ct_rest_over = _multipart_body(
                "file",
                enc_path.name,
                enc_bytes,
                {"recovery_key": rk, "confirm_restore": "true"},
            )
            res_rest_over = client.post(
                "/api/system/backups/encrypted/restore-upload",
                headers={**admin_hdr, "Content-Type": ct_rest_over},
                content=mp_rest_over,
            )
            assert res_rest_over.status_code == 400
            assert "exceeds maximum allowed size" in res_rest_over.json()["detail"]
            assert len(created_stage_dirs) == 2
            assert all(not d.exists() for d in created_stage_dirs)


