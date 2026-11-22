"""
Stage C3-C3 Automated Verification Suite: Secure Supplier Master Excel Import

Covers:
1. Supplier Import Template generation & download (`GET /api/excel/imports/suppliers/template`)
2. Valid workbook preview & downloaded-template round-trip (`POST /api/excel/imports/suppliers/preview`)
3. Mandatory Supplier Name, optional Contact Person, Mobile, Email, Address, City, Indian State (default Maharashtra), GSTIN & Licence No validation
4. Missing mandatory fields & duplicate detection (DB active/inactive & intra-file)
5. Security enforcement: formula cells (`data_only=False`), formula-like text, forbidden columns
   (Supplier ID, PAN, Bank Name, Bank Account, IFSC, UPI ID, Opening/Current Balance, Outstanding Amount,
   Payment History, Ledger Transactions, secrets), unsupported non-DB columns (Village, Taluka, District,
   Pincode, Supplier Category/Group), ZIP bombs, XXE, macros, encrypted ZIP, `.xlsm`/`.csv`
6. Authorization (`401` unauthenticated, `403` OPERATOR, ADMIN-only) & `503` maintenance guard
7. Preview makes ZERO DB writes
8. Preview token security: ADMIN-user binding, cross-import-kind rejection (Product vs Customer vs Supplier),
   SHA-256 hash mismatch, max 20 records, expiry (<=15m) & single-use replay
9. Atomic commit: concurrent duplicate protection, mid-batch rollback without path/SQL leakage,
   preservation of existing supplier PAN/bank/balances/purchases/payment history, zero PAN/bank/balances/stock/vouchers created, audit_log
10. Frontend Supplier Directory UI presence & ADMIN-only visibility checks
"""
from __future__ import annotations

import hashlib
import io
import sqlite3
import zipfile
from pathlib import Path
from typing import Any, Dict, List

import openpyxl
import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.api.routes_excel import XLSX_MEDIA_TYPE
from src.db.connection import get_db_manager, reset_db_manager
from src.models.master_data import Supplier
from src.models.system import CompanySettings
from src.repositories.master_data_repository import MasterDataRepository
from src.services.auth_service import AuthService
from src.services.excel_import_service import (
    CUSTOMER_IMPORT_SHEET_NAME,
    IMPORT_KIND_SUPPLIERS,
    MAX_ACTIVE_PREVIEW_RECORDS,
    PREVIEW_TOKEN_TTL_SECONDS,
    PRODUCT_IMPORT_SHEET_NAME,
    ROW_STATUS_DUP_FILE,
    ROW_STATUS_INVALID_REQUIRED,
    ROW_STATUS_INVALID_VALIDATION,
    ROW_STATUS_READY,
    ROW_STATUS_SUPPLIER_DUP_DB,
    SUPPLIER_IMPORT_SHEET_NAME,
    TEMPLATE_SAMPLE_SUPPLIER_NAME,
    PreviewTokenError,
    PreviewTokenStore,
    reset_preview_token_store,
)
from src.services.excel_security import MAX_DATA_ROWS, MAX_UPLOAD_BYTES


# ---------------------------------------------------------------------------
# Helpers & Fixtures (Strictly Isolated Temp SQLite DBs)
# ---------------------------------------------------------------------------
def _make_supplier_import_xlsx(
    rows: List[List[Any]],
    *,
    sheet_name: str = SUPPLIER_IMPORT_SHEET_NAME,
    extra_sheets: Dict[str, List[List[Any]]] = None,
) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows:
        ws.append(row)
    if extra_sheets:
        for extra_name, extra_rows in extra_sheets.items():
            ews = wb.create_sheet(title=extra_name)
            for erow in extra_rows:
                ews.append(erow)
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def _rewrite_zip_bytes(raw_xlsx: bytes, mutate_fn) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw_xlsx), "r") as zin, zipfile.ZipFile(
        out, "w", compression=zipfile.ZIP_DEFLATED
    ) as zout:
        mutate_fn(zin, zout)
    return out.getvalue()


@pytest.fixture
def isolated_supplier_import_env(tmp_path: Path, monkeypatch) -> Dict[str, Any]:
    reset_preview_token_store()
    reset_db_manager()
    db_file = tmp_path / "c3c3_supplier_import.db"
    bkp_dir = tmp_path / "c3c3_backups"
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    monkeypatch.setenv("CLEAN_INSTALL", "1")
    reset_db_manager()

    db = get_db_manager()
    db.initialize_database(include_seed=True, clean_install=True)

    auth = AuthService(db)
    admin1 = auth.setup_initial_admin(
        username="owner_admin",
        plain_password="AdminPassword#2026",
        full_name="Suresh Patil",
        company_settings=CompanySettings(
            company_name="Krushidhan Agro Center",
            owner_name="Suresh Patil",
            address="APMC Market Yard",
            city="Baramati",
            state="Maharashtra",
            pincode="413102",
            mobile="9822001122",
            gstin="27ABCDE1234F1Z5",
        ),
    )
    admin2_id = auth.register_user(
        "partner_admin", "PartnerPass#2026", "Mahesh Kulkarni", role="ADMIN"
    )
    op_id = auth.register_user(
        "counter_op", "OperatorPass#2026", "Ramesh Jadhav", role="OPERATOR"
    )

    admin_token = auth.create_access_token(
        admin1.user_id or 1, "owner_admin", "Suresh Patil", "ADMIN"
    )
    admin2_token = auth.create_access_token(
        admin2_id, "partner_admin", "Mahesh Kulkarni", "ADMIN"
    )
    op_token = auth.create_access_token(
        op_id, "counter_op", "Ramesh Jadhav", "OPERATOR"
    )

    repo = MasterDataRepository(db)
    existing_active_id = repo.create_supplier(
        Supplier(
            supplier_name="Bayer CropScience Distributor",
            contact_person="Vikram Deshmukh",
            mobile="9822334455",
            email="orders@bayerdist.example",
            address="Plot 8, MIDC",
            city="Pune",
            state="Maharashtra",
            gstin="27AABCU9603R1ZM",
            pan="ABCDE1234F",
            dl_number="MH-PEST-1001",
            bank_name="SBI Market Yard",
            account_no="123456789012",
            ifsc_code="SBIN0001234",
            opening_balance=4500.0,
            current_balance=4500.0,
            is_active=1,
        )
    )
    existing_inactive_id = repo.create_supplier(
        Supplier(
            supplier_name="Archived Agro Supplier",
            contact_person="Old Contact",
            mobile="9811223344",
            city="Satara",
            state="Maharashtra",
            opening_balance=600.0,
            current_balance=600.0,
            is_active=0,
        )
    )

    with TestClient(app) as client:
        yield {
            "client": client,
            "db": db,
            "db_file": db_file,
            "admin_headers": {"Authorization": f"Bearer {admin_token}"},
            "admin2_headers": {"Authorization": f"Bearer {admin2_token}"},
            "op_headers": {"Authorization": f"Bearer {op_token}"},
            "admin1": admin1,
            "admin2_id": admin2_id,
            "existing_active_id": existing_active_id,
            "existing_inactive_id": existing_inactive_id,
        }

    reset_preview_token_store()
    reset_db_manager()


# ---------------------------------------------------------------------------
# 1. Template Generation, Download & Round-Trip Editing
# ---------------------------------------------------------------------------
def test_supplier_template_generation_and_download(isolated_supplier_import_env):
    client = isolated_supplier_import_env["client"]
    resp = client.get(
        "/api/excel/imports/suppliers/template",
        headers=isolated_supplier_import_env["admin_headers"],
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(XLSX_MEDIA_TYPE)
    assert "Supplier_Import_Template.xlsx" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "no-store"

    wb = openpyxl.load_workbook(io.BytesIO(resp.content), data_only=False)
    try:
        assert wb.sheetnames == [SUPPLIER_IMPORT_SHEET_NAME]
        ws = wb[SUPPLIER_IMPORT_SHEET_NAME]

        all_texts: List[str] = []
        sample_found = False
        header_row_values: List[str] = []
        for row in ws.iter_rows(values_only=False):
            row_vals = [c.value for c in row]
            for c in row:
                assert c.data_type != "f"
                if c.value is not None:
                    all_texts.append(str(c.value))
            if row_vals and row_vals[0] == "Supplier Name *":
                header_row_values = [str(v) for v in row_vals if v is not None]
            if row_vals and row_vals[0] == TEMPLATE_SAMPLE_SUPPLIER_NAME:
                sample_found = True

        assert header_row_values == [
            "Supplier Name *",
            "Contact Person",
            "Mobile",
            "Email",
            "Address",
            "City",
            "State",
            "GSTIN",
            "Licence No",
        ]
        assert sample_found is True
        joined = "\n".join(all_texts)
        assert "PAN" in joined
        assert "IFSC" in joined
        for excluded_col in (
            "Supplier ID",
            "PAN",
            "Bank Name",
            "Bank Account Number",
            "IFSC",
            "UPI ID",
            "Opening Balance",
            "Current Balance",
            "Outstanding Amount",
            "Payment History",
            "Village",
            "District",
            "Pincode",
        ):
            assert excluded_col not in header_row_values
    finally:
        wb.close()

    # Uploading untouched template skips sample row cleanly
    up = client.post(
        "/api/excel/imports/suppliers/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={
            "file": (
                "Supplier_Import_Template.xlsx",
                resp.content,
                XLSX_MEDIA_TYPE,
            )
        },
    )
    assert up.status_code == 200
    pdata = up.json()
    assert pdata["sample_rows_skipped"] == 1
    assert pdata["summary"]["total_rows"] == 0
    assert pdata["summary"]["ready_count"] == 0
    assert pdata["summary"]["invalid_count"] == 0


def test_edited_supplier_template_roundtrip_and_sample_row_edit(isolated_supplier_import_env):
    client = isolated_supplier_import_env["client"]
    db = isolated_supplier_import_env["db"]

    tmpl_resp = client.get(
        "/api/excel/imports/suppliers/template",
        headers=isolated_supplier_import_env["admin_headers"],
    )
    assert tmpl_resp.status_code == 200

    # Roundtrip A: keep sample row intact and append 2 real suppliers
    wb = openpyxl.load_workbook(io.BytesIO(tmpl_resp.content))
    ws = wb[SUPPLIER_IMPORT_SHEET_NAME]
    ws.append(
        [
            "Mahyco Seeds Depot",
            "Sandeep More",
            "9765432109",
            "dispatch@mahyco.example",
            "Gat 44, MIDC Jalna",
            "Jalna",
            "",  # Blank state should default to Maharashtra
            "27aabcu9603r1zm",  # lowercase should normalize uppercase
            "MH-SEED-2026-88",
        ]
    )
    ws.append(
        [
            "Nuziveedu Agro Agency",
            "Kiran Rao",
            9890112233,  # numeric cell
            "",
            "",
            "Belagavi",
            "Karnataka",
            "",
            123456,  # numeric licence no
        ]
    )
    out = io.BytesIO()
    wb.save(out)
    wb.close()

    prev = client.post(
        "/api/excel/imports/suppliers/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={"file": ("edited_suppliers.xlsx", out.getvalue(), XLSX_MEDIA_TYPE)},
    )
    assert prev.status_code == 200
    pdata = prev.json()
    assert pdata["sample_rows_skipped"] == 1
    assert pdata["summary"]["ready_count"] == 2
    assert pdata["rows"][0]["state"] == "Maharashtra"
    assert pdata["rows"][0]["gstin"] == "27AABCU9603R1ZM"
    assert pdata["rows"][1]["dl_number"] == "123456"

    commit = client.post(
        "/api/excel/imports/suppliers/commit",
        headers=isolated_supplier_import_env["admin_headers"],
        json={"preview_token": pdata["preview_token"], "file_sha256": pdata["file_sha256"]},
    )
    assert commit.status_code == 200
    assert commit.json()["imported_count"] == 2
    assert db.fetch_one("SELECT 1 FROM suppliers WHERE supplier_name LIKE '%SAMPLE%';") is None

    # Roundtrip B: intentionally edit the sample row into a real supplier
    wb2 = openpyxl.load_workbook(io.BytesIO(tmpl_resp.content))
    ws2 = wb2[SUPPLIER_IMPORT_SHEET_NAME]
    for row in ws2.iter_rows():
        if row[0].value == TEMPLATE_SAMPLE_SUPPLIER_NAME:
            row[0].value = "Sahyadri Agro Distributors Pvt Ltd"
            break
    out2 = io.BytesIO()
    wb2.save(out2)
    wb2.close()

    prev2 = client.post(
        "/api/excel/imports/suppliers/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={"file": ("edited_sample.xlsx", out2.getvalue(), XLSX_MEDIA_TYPE)},
    )
    assert prev2.status_code == 200
    assert prev2.json()["sample_rows_skipped"] == 0
    assert prev2.json()["summary"]["ready_count"] == 1

    commit2 = client.post(
        "/api/excel/imports/suppliers/commit",
        headers=isolated_supplier_import_env["admin_headers"],
        json={"preview_token": prev2.json()["preview_token"], "file_sha256": prev2.json()["file_sha256"]},
    )
    assert commit2.status_code == 200
    assert commit2.json()["imported_count"] == 1
    assert db.fetch_one("SELECT supplier_id FROM suppliers WHERE supplier_name = ?;", ("Sahyadri Agro Distributors Pvt Ltd",)) is not None


# ---------------------------------------------------------------------------
# 2. Validation Rules, Mobile/Email/GSTIN/State & Duplicate Classification
# ---------------------------------------------------------------------------
def test_supplier_validation_rules_and_duplicate_detection(isolated_supplier_import_env):
    client = isolated_supplier_import_env["client"]

    xlsx = _make_supplier_import_xlsx(
        [
            [
                "Supplier Name *",
                "Contact Person",
                "Mobile",
                "Email",
                "Address",
                "City",
                "State",
                "GSTIN",
                "Licence No",
            ],
            # Row 2: Valid minimal supplier (blank state -> Maharashtra, blank optional fields)
            ["Coromandel Farm Supply", "", "", "", "", "", "", "", ""],
            # Row 3: Valid full supplier with UT alias and lowercase GSTIN
            [
                "UPL Crop Protection Hub",
                "Amol Jadhav",
                "7890123456",
                "sales@uplhub.example",
                "Plot 22, APMC",
                "Solapur",
                "Andaman & Nicobar Islands",
                "27aabcu9603r1zm",
                "MH-PEST-9090",
            ],
            # Row 4: Duplicate of active DB supplier (different case & whitespace)
            ["  bayer   cropscience distributor ", "Other", "9000000001", "", "", "Mumbai", "Gujarat", "", ""],
            # Row 5: Duplicate of inactive DB supplier
            ["archived agro supplier", "", "9876543210", "", "", "Satara", "Maharashtra", "", ""],
            # Row 6: Intra-file repeat of Row 2
            ["coromandel farm supply", "Dup", "9876543211", "", "", "", "", "", ""],
            # Row 7: Missing mandatory Supplier Name
            ["", "Contact", "9876543210", "a@b.com", "Market Yard", "Pune", "Maharashtra", "", ""],
            # Row 8: Invalid mobile (starts with 1 / short)
            ["Bad Mobile Supplier", "", "12345", "", "", "", "Maharashtra", "", ""],
            # Row 9: Invalid email format
            ["Bad Email Supplier", "", "9876543210", "invalid-email-no-at", "", "", "Maharashtra", "", ""],
            # Row 10: Invalid GSTIN
            ["Bad GSTIN Supplier", "", "9876543210", "", "", "", "Maharashtra", "INVALIDGSTIN12", ""],
            # Row 11: Invalid non-Indian State
            ["Bad State Supplier", "", "9876543210", "", "", "", "Atlantis", "", ""],
            # Row 12: Overlong Address (> 300 chars) & invalid decimal Licence No
            ["Bad Address Supplier", "", "9876543210", "", "A" * 305, "Pune", "Maharashtra", "", 12.75],
        ]
    )

    resp = client.post(
        "/api/excel/imports/suppliers/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={"file": ("mixed_suppliers.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert resp.status_code == 200
    data = resp.json()
    summary = data["summary"]
    assert summary["total_rows"] == 11
    assert summary["ready_count"] == 2
    assert summary["duplicate_existing_count"] == 2
    assert summary["duplicate_file_count"] == 1
    assert summary["duplicate_count"] == 3
    assert summary["invalid_missing_count"] == 1
    assert summary["invalid_validation_count"] == 5
    assert summary["invalid_count"] == 6

    rows_by_num = {r["row_number"]: r for r in data["rows"]}
    assert rows_by_num[2]["status"] == ROW_STATUS_READY
    assert rows_by_num[2]["state"] == "Maharashtra"
    assert rows_by_num[3]["status"] == ROW_STATUS_READY
    assert rows_by_num[3]["state"] == "Andaman and Nicobar Islands"
    assert rows_by_num[3]["gstin"] == "27AABCU9603R1ZM"
    assert rows_by_num[3]["email"] == "sales@uplhub.example"
    assert rows_by_num[4]["status"] == ROW_STATUS_SUPPLIER_DUP_DB
    assert "active" in rows_by_num[4]["message"].lower()
    assert rows_by_num[5]["status"] == ROW_STATUS_SUPPLIER_DUP_DB
    assert "inactive" in rows_by_num[5]["message"].lower()
    assert rows_by_num[6]["status"] == ROW_STATUS_DUP_FILE
    assert rows_by_num[7]["status"] == ROW_STATUS_INVALID_REQUIRED
    assert rows_by_num[8]["status"] == ROW_STATUS_INVALID_VALIDATION
    assert "mobile" in rows_by_num[8]["message"].lower()
    assert rows_by_num[9]["status"] == ROW_STATUS_INVALID_VALIDATION
    assert "email" in rows_by_num[9]["message"].lower()
    assert rows_by_num[10]["status"] == ROW_STATUS_INVALID_VALIDATION
    assert "gstin" in rows_by_num[10]["message"].lower()
    assert rows_by_num[11]["status"] == ROW_STATUS_INVALID_VALIDATION
    assert "state" in rows_by_num[11]["message"].lower()
    assert rows_by_num[12]["status"] == ROW_STATUS_INVALID_VALIDATION
    assert "address" in rows_by_num[12]["message"].lower()
    assert "licence" in rows_by_num[12]["message"].lower()


# ---------------------------------------------------------------------------
# 3. Security Enforcement (Formulas, Forbidden/Unsupported Headers, ZIP Attacks)
# ---------------------------------------------------------------------------
def test_supplier_import_formula_cells_and_formula_text_rejected(isolated_supplier_import_env):
    client = isolated_supplier_import_env["client"]

    # Case A: Actual Excel formula (data_type == 'f')
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = SUPPLIER_IMPORT_SHEET_NAME
    ws.append(["Supplier Name", "Mobile"])
    ws.append(["=1+1", "9876543210"])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    r1 = client.post(
        "/api/excel/imports/suppliers/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={"file": ("formula_cell.xlsx", buf.getvalue(), XLSX_MEDIA_TYPE)},
    )
    assert r1.status_code == 400
    assert "formula" in r1.json()["detail"].lower()

    # Case B: Formula-like text strings
    for payload in [" =CMD()", "@SUM(1,2)", "+919876543210", "-123", "\t=1+1"]:
        wb2 = openpyxl.Workbook()
        ws2 = wb2.active
        ws2.title = SUPPLIER_IMPORT_SHEET_NAME
        ws2.append(["Supplier Name", "City"])
        c = ws2.cell(row=2, column=1, value=payload)
        c.data_type = "s"
        ws2.cell(row=2, column=2, value="Pune")
        b2 = io.BytesIO()
        wb2.save(b2)
        wb2.close()
        r2 = client.post(
            "/api/excel/imports/suppliers/preview",
            headers=isolated_supplier_import_env["admin_headers"],
            files={"file": ("formula_text.xlsx", b2.getvalue(), XLSX_MEDIA_TYPE)},
        )
        assert r2.status_code == 400

    # Case C: Formula hidden inside secondary worksheet
    wb3 = openpyxl.Workbook()
    ws3_main = wb3.active
    ws3_main.title = SUPPLIER_IMPORT_SHEET_NAME
    ws3_main.append(["Supplier Name"])
    ws3_main.append(["Clean Supplier"])
    ws3_hidden = wb3.create_sheet("HiddenCalc")
    ws3_hidden["A1"] = '=HYPERLINK("http://evil.example")'
    b3 = io.BytesIO()
    wb3.save(b3)
    wb3.close()
    r3 = client.post(
        "/api/excel/imports/suppliers/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={"file": ("hidden_formula.xlsx", b3.getvalue(), XLSX_MEDIA_TYPE)},
    )
    assert r3.status_code == 400


@pytest.mark.parametrize(
    "forbidden_col",
    [
        "Supplier ID",
        "PAN",
        "PAN No",
        "Bank Name",
        "Bank Account Number",
        "Account No",
        "IFSC",
        "IFSC Code",
        "UPI ID",
        "Opening Balance",
        "Current Balance",
        "Outstanding Amount",
        "Payment History",
        "Ledger Transactions",
        "Balance",
        "Password",
        "JWT Secret",
    ],
)
def test_supplier_forbidden_columns_rejected(isolated_supplier_import_env, forbidden_col):
    xlsx = _make_supplier_import_xlsx(
        [
            ["Supplier Name", "Mobile", forbidden_col],
            ["Safe Agro Distributor", "9876543210", "SENSITIVE123"],
        ]
    )
    resp = isolated_supplier_import_env["client"].post(
        "/api/excel/imports/suppliers/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={"file": ("forbidden_supp_header.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "unsupported_col",
    [
        "Village",
        "Taluka",
        "District",
        "Pincode",
        "Supplier Category",
        "Supplier Group",
    ],
)
def test_supplier_unsupported_non_db_columns_rejected(isolated_supplier_import_env, unsupported_col):
    xlsx = _make_supplier_import_xlsx(
        [
            ["Supplier Name", unsupported_col],
            ["Clean Distributor", "Sample Value"],
        ]
    )
    resp = isolated_supplier_import_env["client"].post(
        "/api/excel/imports/suppliers/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={"file": ("unsupported_supp_header.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert resp.status_code == 422
    assert unsupported_col in resp.json()["detail"]


def test_supplier_import_archive_xxe_macro_encryption_and_limits_rejected(isolated_supplier_import_env):
    client = isolated_supplier_import_env["client"]
    base = _make_supplier_import_xlsx([["Supplier Name", "Mobile"], ["Valid Distributor", "9876543210"]])

    # 1. Wrong extensions & traversal
    for bad_name in ["supp.xlsm", "supp.xls", "supp.xlsb", "supp.csv", "../evil.xlsx"]:
        r = client.post(
            "/api/excel/imports/suppliers/preview",
            headers=isolated_supplier_import_env["admin_headers"],
            files={"file": (bad_name, base, XLSX_MEDIA_TYPE)},
        )
        assert r.status_code == 400

    # 2. Fake text masquerading as .xlsx & corrupt bytes
    assert (
        client.post(
            "/api/excel/imports/suppliers/preview",
            headers=isolated_supplier_import_env["admin_headers"],
            files={"file": ("fake.xlsx", b"Supplier Name\nDistributor\n", XLSX_MEDIA_TYPE)},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/excel/imports/suppliers/preview",
            headers=isolated_supplier_import_env["admin_headers"],
            files={"file": ("corrupt.xlsx", b"PK\x03\x04broken", XLSX_MEDIA_TYPE)},
        ).status_code
        == 400
    )

    # 3. XXE DOCTYPE/ENTITY
    def _xxe(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "xl/workbook.xml":
                data = (
                    b'<?xml version="1.0" encoding="UTF-8"?>\n'
                    b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
                    + data.split(b"?>", 1)[-1]
                )
            zout.writestr(info.filename, data)

    assert (
        client.post(
            "/api/excel/imports/suppliers/preview",
            headers=isolated_supplier_import_env["admin_headers"],
            files={"file": ("xxe.xlsx", _rewrite_zip_bytes(base, _xxe), XLSX_MEDIA_TYPE)},
        ).status_code
        == 400
    )

    # 4. Macro VBA binary
    def _macro(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            zout.writestr(info.filename, zin.read(info.filename))
        zout.writestr("xl/vbaProject.bin", b"FAKEVBA")

    assert (
        client.post(
            "/api/excel/imports/suppliers/preview",
            headers=isolated_supplier_import_env["admin_headers"],
            files={"file": ("macro.xlsx", _rewrite_zip_bytes(base, _macro), XLSX_MEDIA_TYPE)},
        ).status_code
        == 400
    )

    # 5. ZIP bomb
    def _bomb(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            zout.writestr(info.filename, zin.read(info.filename))
        zout.writestr("xl/bomb.xml", b"Z" * (512 * 1024))

    assert (
        client.post(
            "/api/excel/imports/suppliers/preview",
            headers=isolated_supplier_import_env["admin_headers"],
            files={"file": ("bomb.xlsx", _rewrite_zip_bytes(base, _bomb), XLSX_MEDIA_TYPE)},
        ).status_code
        == 400
    )

    # 6. Encrypted OOXML container
    def _enc(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            zout.writestr(info.filename, zin.read(info.filename))
        zout.writestr("EncryptionInfo", b"encrypted")

    assert (
        client.post(
            "/api/excel/imports/suppliers/preview",
            headers=isolated_supplier_import_env["admin_headers"],
            files={"file": ("enc.xlsx", _rewrite_zip_bytes(base, _enc), XLSX_MEDIA_TYPE)},
        ).status_code
        == 400
    )

    # 7. Wrong sheet name
    wrong_sheet = _make_supplier_import_xlsx([["Supplier Name"], ["S1"]], sheet_name="WrongSheet")
    assert (
        client.post(
            "/api/excel/imports/suppliers/preview",
            headers=isolated_supplier_import_env["admin_headers"],
            files={"file": ("wrong_sheet.xlsx", wrong_sheet, XLSX_MEDIA_TYPE)},
        ).status_code
        == 422
    )

    # 8. Exceeds MAX_DATA_ROWS (2,000)
    rows_2001 = [["Supplier Name"]] + [[f"Bulk Supplier {i}"] for i in range(1, MAX_DATA_ROWS + 2)]
    too_many = _make_supplier_import_xlsx(rows_2001)
    assert (
        client.post(
            "/api/excel/imports/suppliers/preview",
            headers=isolated_supplier_import_env["admin_headers"],
            files={"file": ("too_many.xlsx", too_many, XLSX_MEDIA_TYPE)},
        ).status_code
        == 422
    )

    # 9. Exceeds 5 MB
    oversized = base + (b"\x00" * (MAX_UPLOAD_BYTES + 1024 - len(base)))
    assert (
        client.post(
            "/api/excel/imports/suppliers/preview",
            headers=isolated_supplier_import_env["admin_headers"],
            files={"file": ("oversized.xlsx", oversized, XLSX_MEDIA_TYPE)},
        ).status_code
        == 400
    )


# ---------------------------------------------------------------------------
# 4. Authorization, Maintenance Mode & Zero Preview DB Writes
# ---------------------------------------------------------------------------
def test_supplier_import_rbac_and_maintenance(isolated_supplier_import_env):
    client = isolated_supplier_import_env["client"]
    db = isolated_supplier_import_env["db"]
    xlsx = _make_supplier_import_xlsx([["Supplier Name", "Mobile"], ["RBAC Distributor", "9876543210"]])

    # Unauthenticated -> 401
    assert client.get("/api/excel/imports/suppliers/template").status_code == 401
    assert (
        client.post(
            "/api/excel/imports/suppliers/preview",
            files={"file": ("s.xlsx", xlsx, XLSX_MEDIA_TYPE)},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/excel/imports/suppliers/commit",
            json={"preview_token": "x" * 24, "file_sha256": "a" * 64},
        ).status_code
        == 401
    )

    # OPERATOR -> 403
    op_headers = isolated_supplier_import_env["op_headers"]
    assert client.get("/api/excel/imports/suppliers/template", headers=op_headers).status_code == 403
    assert (
        client.post(
            "/api/excel/imports/suppliers/preview",
            headers=op_headers,
            files={"file": ("s.xlsx", xlsx, XLSX_MEDIA_TYPE)},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/excel/imports/suppliers/commit",
            headers=op_headers,
            json={"preview_token": "x" * 24, "file_sha256": "a" * 64},
        ).status_code
        == 403
    )

    # Maintenance -> 503
    with db.exclusive_maintenance(reason="test maintenance"):
        assert (
            client.get(
                "/api/excel/imports/suppliers/template",
                headers=isolated_supplier_import_env["admin_headers"],
            ).status_code
            == 503
        )
        assert (
            client.post(
                "/api/excel/imports/suppliers/preview",
                headers=isolated_supplier_import_env["admin_headers"],
                files={"file": ("s.xlsx", xlsx, XLSX_MEDIA_TYPE)},
            ).status_code
            == 503
        )


def test_supplier_preview_makes_zero_database_writes(isolated_supplier_import_env):
    client = isolated_supplier_import_env["client"]
    db = isolated_supplier_import_env["db"]

    before_supp = db.fetch_one("SELECT COUNT(*) AS cnt FROM suppliers;")["cnt"]
    before_audit = db.fetch_one("SELECT COUNT(*) AS cnt FROM audit_log;")["cnt"]
    before_vouchers = db.fetch_one("SELECT COUNT(*) AS cnt FROM vouchers;")["cnt"]
    before_ledger = db.fetch_one("SELECT COUNT(*) AS cnt FROM ledger_entries;")["cnt"]
    before_data_ver = db.fetch_one("PRAGMA data_version;")[0]

    xlsx = _make_supplier_import_xlsx(
        [
            ["Supplier Name", "Mobile", "Email", "City", "State", "GSTIN", "Licence No"],
            ["Preview Only Supplier A", "9876500001", "a@supp.example", "Pune", "Maharashtra", "27AABCU9603R1ZM", "L-1"],
            ["Preview Only Supplier B", "9876500002", "b@supp.example", "Nashik", "Maharashtra", "", "L-2"],
        ]
    )
    resp = client.post(
        "/api/excel/imports/suppliers/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={"file": ("preview_only.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert resp.status_code == 200
    assert resp.json()["summary"]["ready_count"] == 2

    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM suppliers;")["cnt"] == before_supp
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM audit_log;")["cnt"] == before_audit
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM vouchers;")["cnt"] == before_vouchers
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM ledger_entries;")["cnt"] == before_ledger
    assert db.fetch_one("PRAGMA data_version;")[0] == before_data_ver


# ---------------------------------------------------------------------------
# 5. Preview Token Security (Cross-User, Cross-Kind, Hash Mismatch, Replay, Expiry)
# ---------------------------------------------------------------------------
def test_supplier_preview_token_security_and_cross_kind_isolation(isolated_supplier_import_env):
    client = isolated_supplier_import_env["client"]
    db = isolated_supplier_import_env["db"]

    supp_xlsx = _make_supplier_import_xlsx(
        [["Supplier Name", "Mobile"], ["Token Guard Supplier", "9876511122"]]
    )
    prev_resp = client.post(
        "/api/excel/imports/suppliers/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={"file": ("supp_token.xlsx", supp_xlsx, XLSX_MEDIA_TYPE)},
    )
    assert prev_resp.status_code == 200
    pdata = prev_resp.json()
    token = pdata["preview_token"]
    file_sha = pdata["file_sha256"]
    assert pdata["expires_in_seconds"] == PREVIEW_TOKEN_TTL_SECONDS

    # 1. Product and Customer commit MUST reject a Supplier preview token (and leave it unconsumed!)
    for other_commit_path in (
        "/api/excel/imports/products/commit",
        "/api/excel/imports/customers/commit",
    ):
        cross_kind_resp = client.post(
            other_commit_path,
            headers=isolated_supplier_import_env["admin_headers"],
            json={"preview_token": token, "file_sha256": file_sha},
        )
        assert cross_kind_resp.status_code == 400
        assert "different import" in cross_kind_resp.json()["detail"].lower()

    # 2. Vice versa: Supplier commit MUST reject Product AND Customer preview tokens
    wb_prod = openpyxl.Workbook()
    ws_p = wb_prod.active
    ws_p.title = PRODUCT_IMPORT_SHEET_NAME
    ws_p.append(["Product Name", "Tax Group"])
    ws_p.append(["Cross Token Prod", "Exempted (0%)"])
    p_buf = io.BytesIO()
    wb_prod.save(p_buf)
    wb_prod.close()
    prod_prev = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={"file": ("p.xlsx", p_buf.getvalue(), XLSX_MEDIA_TYPE)},
    ).json()
    assert (
        client.post(
            "/api/excel/imports/suppliers/commit",
            headers=isolated_supplier_import_env["admin_headers"],
            json={"preview_token": prod_prev["preview_token"], "file_sha256": prod_prev["file_sha256"]},
        ).status_code
        == 400
    )

    wb_cust = openpyxl.Workbook()
    ws_c = wb_cust.active
    ws_c.title = CUSTOMER_IMPORT_SHEET_NAME
    ws_c.append(["Customer Name", "Mobile"])
    ws_c.append(["Cross Token Cust", "9876543210"])
    c_buf = io.BytesIO()
    wb_cust.save(c_buf)
    wb_cust.close()
    cust_prev = client.post(
        "/api/excel/imports/customers/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={"file": ("c.xlsx", c_buf.getvalue(), XLSX_MEDIA_TYPE)},
    ).json()
    assert (
        client.post(
            "/api/excel/imports/suppliers/commit",
            headers=isolated_supplier_import_env["admin_headers"],
            json={"preview_token": cust_prev["preview_token"], "file_sha256": cust_prev["file_sha256"]},
        ).status_code
        == 400
    )

    # 3. Cross-admin user commit rejected with 403
    assert (
        client.post(
            "/api/excel/imports/suppliers/commit",
            headers=isolated_supplier_import_env["admin2_headers"],
            json={"preview_token": token, "file_sha256": file_sha},
        ).status_code
        == 403
    )
    assert db.fetch_one("SELECT 1 FROM suppliers WHERE supplier_name = ?;", ("Token Guard Supplier",)) is None

    # 4. SHA-256 mismatch -> 400
    wrong_sha = hashlib.sha256(b"tampered").hexdigest()
    assert (
        client.post(
            "/api/excel/imports/suppliers/commit",
            headers=isolated_supplier_import_env["admin_headers"],
            json={"preview_token": token, "file_sha256": wrong_sha},
        ).status_code
        == 400
    )

    # 5. Valid single-use commit succeeds
    ok_resp = client.post(
        "/api/excel/imports/suppliers/commit",
        headers=isolated_supplier_import_env["admin_headers"],
        json={"preview_token": token, "file_sha256": file_sha},
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json()["imported_count"] == 1

    # 6. Replay of same token -> 400 and no duplicate record
    assert (
        client.post(
            "/api/excel/imports/suppliers/commit",
            headers=isolated_supplier_import_env["admin_headers"],
            json={"preview_token": token, "file_sha256": file_sha},
        ).status_code
        == 400
    )
    assert (
        db.fetch_one(
            "SELECT COUNT(*) AS cnt FROM suppliers WHERE supplier_name = ?;",
            ("Token Guard Supplier",),
        )["cnt"]
        == 1
    )

    # 7. Expiry (>15m) and capacity cap (max 20 active records)
    clock = [1_700_000_000.0]
    store = PreviewTokenStore(ttl_seconds=900.0, max_records=MAX_ACTIVE_PREVIEW_RECORDS, time_fn=lambda: clock[0])
    recs = []
    for idx in range(MAX_ACTIVE_PREVIEW_RECORDS + 3):
        recs.append(
            store.create(
                import_kind=IMPORT_KIND_SUPPLIERS,
                user_id=1,
                username="owner_admin",
                db_key="testdb",
                file_sha256=file_sha,
                filename=f"s_{idx}.xlsx",
                ready_candidates=[],
                preview_rows=[],
                summary={"ready_count": 0},
                sample_rows_skipped=0,
            )
        )
    assert store.active_count() == MAX_ACTIVE_PREVIEW_RECORDS

    clock[0] += PREVIEW_TOKEN_TTL_SECONDS + 1.0
    with pytest.raises(PreviewTokenError) as exc_info:
        store.validate_and_consume(
            recs[-1].token,
            user_id=1,
            username="owner_admin",
            db_key="testdb",
            file_sha256=file_sha,
            expected_kind=IMPORT_KIND_SUPPLIERS,
        )
    assert exc_info.value.status_code == 400
    assert "expired" in exc_info.value.message.lower()


# ---------------------------------------------------------------------------
# 6. Concurrent Duplicate, Atomic Rollback, Field Parity & Existing History Preservation
# ---------------------------------------------------------------------------
def test_concurrent_duplicate_and_existing_supplier_balance_preservation(isolated_supplier_import_env):
    client = isolated_supplier_import_env["client"]
    db = isolated_supplier_import_env["db"]

    existing_sid = isolated_supplier_import_env["existing_active_id"]
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO purchases (
                invoice_no, purchase_date, supplier_id, payment_type, total_taxable,
                net_amount, paid_amount, due_amount
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            ("PUR-SUPP-TEST-1", "2026-09-10", existing_sid, "CREDIT", 4500.0, 4500.0, 0.0, 4500.0),
        )
        conn.execute(
            """
            INSERT INTO vouchers (
                voucher_no, voucher_date, voucher_type, total_amount, narration, reference_type, reference_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            ("PMT-SUPP-TEST-1", "2026-09-11", "PAYMENT", 1000.0, "Existing supplier payment", "PURCHASE", existing_sid),
        )

    before_batches = db.fetch_one("SELECT COUNT(*) AS cnt FROM stock_batches;")["cnt"]
    before_ledger_stock = db.fetch_one("SELECT COUNT(*) AS cnt FROM stock_ledger;")["cnt"]

    xlsx = _make_supplier_import_xlsx(
        [
            ["Supplier Name", "Contact Person", "Mobile", "Email", "Address", "City", "State", "GSTIN", "Licence No"],
            ["Concurrent New Distributor", "Raj", "9823456789", "raj@conc.example", "Plot 1", "Pune", "Maharashtra", "", "MH-1"],
            ["Unique Valid Distributor", "Sunil Shinde", "9823456790", "sales@uniqueagro.example", "Lane 4, APMC", "Sangli", "Maharashtra", "27aabcu9603r1zm", "MH-FCO-7788"],
            # Existing DB supplier with attempted different fields — must be skipped, NEVER overwritten
            ["Bayer CropScience Distributor", "FakeName", "9000000000", "fake@evil.example", "FakeAddr", "Surat", "Gujarat", "", "FAKE-0"],
        ]
    )
    prev = client.post(
        "/api/excel/imports/suppliers/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={"file": ("race_suppliers.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert prev.status_code == 200
    pdata = prev.json()
    assert pdata["summary"]["ready_count"] == 2
    assert pdata["summary"]["duplicate_existing_count"] == 1

    # Insert "Concurrent New Distributor" after preview & before commit
    repo = MasterDataRepository(db)
    race_orig_id = repo.create_supplier(
        Supplier(
            supplier_name="Concurrent New Distributor",
            contact_person="OriginalContact",
            mobile="9999988888",
            city="OriginalCity",
            state="Maharashtra",
            pan="ZZZZZ9999Z",
            opening_balance=1750.0,
            current_balance=1750.0,
        )
    )

    commit = client.post(
        "/api/excel/imports/suppliers/commit",
        headers=isolated_supplier_import_env["admin_headers"],
        json={"preview_token": pdata["preview_token"], "file_sha256": pdata["file_sha256"]},
    )
    assert commit.status_code == 200
    cdata = commit.json()
    assert cdata["imported_count"] == 1
    assert cdata["skipped_duplicate_count"] == 2

    # Concurrent row retains its original data, PAN & balance
    race_row = db.fetch_one("SELECT * FROM suppliers WHERE supplier_id = ?;", (race_orig_id,))
    assert race_row["mobile"] == "9999988888"
    assert race_row["city"] == "OriginalCity"
    assert race_row["pan"] == "ZZZZZ9999Z"
    assert race_row["current_balance"] == pytest.approx(1750.0)

    # Pre-existing active supplier retains PAN, bank, IFSC, balances, purchases & vouchers intact
    orig_supp = db.fetch_one("SELECT * FROM suppliers WHERE supplier_id = ?;", (existing_sid,))
    assert orig_supp["mobile"] == "9822334455"
    assert orig_supp["city"] == "Pune"
    assert orig_supp["pan"] == "ABCDE1234F"
    assert orig_supp["bank_name"] == "SBI Market Yard"
    assert orig_supp["account_no"] == "123456789012"
    assert orig_supp["ifsc_code"] == "SBIN0001234"
    assert orig_supp["opening_balance"] == pytest.approx(4500.0)
    assert orig_supp["current_balance"] == pytest.approx(4500.0)
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM purchases WHERE supplier_id = ?;", (existing_sid,))["cnt"] == 1

    # Newly imported supplier has exact validated values, NULL PAN/bank/IFSC, and zero balances
    saved_new = db.fetch_one(
        "SELECT * FROM suppliers WHERE supplier_name = ?;",
        ("Unique Valid Distributor",),
    )
    assert saved_new is not None
    assert saved_new["contact_person"] == "Sunil Shinde"
    assert saved_new["mobile"] == "9823456790"
    assert saved_new["email"] == "sales@uniqueagro.example"
    assert saved_new["address"] == "Lane 4, APMC"
    assert saved_new["city"] == "Sangli"
    assert saved_new["state"] == "Maharashtra"
    assert saved_new["gstin"] == "27AABCU9603R1ZM"
    assert saved_new["dl_number"] == "MH-FCO-7788"
    assert saved_new["pan"] is None
    assert saved_new["bank_name"] is None
    assert saved_new["account_no"] is None
    assert saved_new["ifsc_code"] is None
    assert saved_new["opening_balance"] == 0.0
    assert saved_new["current_balance"] == 0.0
    assert saved_new["is_active"] == 1

    # Zero extra purchases, vouchers, ledger entries, or inventory records created; audit_log recorded cleanly
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM vouchers;")["cnt"] == 1
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM ledger_entries;")["cnt"] == 0
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM stock_batches;")["cnt"] == before_batches
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM stock_ledger;")["cnt"] == before_ledger_stock
    audit = db.fetch_one(
        "SELECT action_type, table_name, details FROM audit_log WHERE action_type = 'EXCEL_IMPORT' AND table_name = 'suppliers' ORDER BY log_id DESC LIMIT 1;"
    )
    assert audit is not None
    assert "Imported 1 supplier(s)" in audit["details"]


def test_atomic_rollback_on_mid_batch_supplier_db_error(isolated_supplier_import_env, monkeypatch):
    client = isolated_supplier_import_env["client"]
    db = isolated_supplier_import_env["db"]

    before_count = db.fetch_one("SELECT COUNT(*) AS cnt FROM suppliers;")["cnt"]
    xlsx = _make_supplier_import_xlsx(
        [
            ["Supplier Name", "Mobile"],
            ["Atomic Supplier 1", "9876501010"],
            ["Atomic Supplier 2", "9876502020"],
        ]
    )
    prev = client.post(
        "/api/excel/imports/suppliers/preview",
        headers=isolated_supplier_import_env["admin_headers"],
        files={"file": ("atomic_supp.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert prev.status_code == 200
    pdata = prev.json()
    assert pdata["summary"]["ready_count"] == 2

    orig_create = MasterDataRepository.create_supplier
    calls = {"count": 0}

    def _fail_second_insert(self, supp: Supplier, conn=None) -> int:
        calls["count"] += 1
        if calls["count"] == 2:
            raise sqlite3.OperationalError(
                f"Simulated failure touching internal path {isolated_supplier_import_env['db_file']}"
            )
        return orig_create(self, supp, conn=conn)

    monkeypatch.setattr(MasterDataRepository, "create_supplier", _fail_second_insert)

    commit = client.post(
        "/api/excel/imports/suppliers/commit",
        headers=isolated_supplier_import_env["admin_headers"],
        json={"preview_token": pdata["preview_token"], "file_sha256": pdata["file_sha256"]},
    )
    assert commit.status_code == 500
    detail = commit.json()["detail"]
    assert str(isolated_supplier_import_env["db_file"]) not in detail
    assert "OperationalError" not in detail
    assert "rolled back" in detail.lower()

    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM suppliers;")["cnt"] == before_count
    assert db.fetch_one("SELECT 1 FROM suppliers WHERE supplier_name = ?;", ("Atomic Supplier 1",)) is None


# ---------------------------------------------------------------------------
# 7. Frontend Supplier Directory UI Checks
# ---------------------------------------------------------------------------
def test_frontend_supplier_import_controls_and_role_hiding_present():
    repo_root = Path(__file__).resolve().parent.parent
    html = (repo_root / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (repo_root / "src" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    for elem_id in (
        "supplier-excel-import-card",
        "btn-download-supplier-import-template",
        "btn-select-supplier-import-file",
        "supplier-import-file-input",
        "btn-preview-supplier-import",
        "btn-confirm-supplier-import",
        "supp-import-ready-count",
        "supp-import-duplicate-count",
        "supp-import-invalid-count",
        "supplier-import-preview-tbody",
    ):
        assert elem_id in html

    for fn_name in (
        "downloadSupplierImportTemplate",
        "handleSupplierImportFileSelected",
        "previewSupplierImport",
        "commitSupplierImport",
        "resetSupplierImportState",
    ):
        assert fn_name in js

    assert 'supplierImportCard.style.display = "none"' in js
    assert 'supplierImportCard.style.display = "block"' in js
    assert "Confirm Supplier Import?" in js
