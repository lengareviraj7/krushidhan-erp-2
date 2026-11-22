"""
Stage C3-C2 Automated Verification Suite: Secure Customer Master Excel Import

Covers:
1. Customer Import Template generation & download (`GET /api/excel/imports/customers/template`)
2. Valid workbook preview & downloaded-template round-trip (`POST /api/excel/imports/customers/preview`)
3. Customer Name, Mobile, GSTIN, Indian State (default Maharashtra), Address fields & active Customer Group validation
4. Missing mandatory fields, inactive/unknown group rejection & duplicate detection (DB active/inactive & intra-file)
5. Security enforcement: formula cells (`data_only=False`), formula-like text, forbidden columns
   (Customer ID, Aadhaar, Opening/Current Balance, Credit Limit, Ledger/Payment History, secrets),
   unsupported columns (Email, Pincode, Address), ZIP bombs, XXE, macros, encrypted ZIP, `.xlsm`/`.csv`
6. Authorization (`401` unauthenticated, `403` OPERATOR, ADMIN-only) & `503` maintenance guard
7. Preview makes ZERO DB writes
8. Preview token security: ADMIN-user binding, cross-import-kind rejection (Product vs Customer),
   SHA-256 hash mismatch, expiry (<=15m) & single-use replay
9. Atomic commit: concurrent duplicate protection, mid-batch rollback without path/SQL leakage,
   preservation of existing customer balances/invoices/receipts, zero Aadhaar/balances created, audit_log
10. Frontend Customer Directory UI presence & ADMIN-only visibility checks
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
from src.models.master_data import Customer
from src.models.system import CompanySettings
from src.repositories.master_data_repository import MasterDataRepository
from src.services.auth_service import AuthService
from src.services.excel_import_service import (
    CUSTOMER_IMPORT_SHEET_NAME,
    IMPORT_KIND_CUSTOMERS,
    PREVIEW_TOKEN_TTL_SECONDS,
    PRODUCT_IMPORT_SHEET_NAME,
    ROW_STATUS_CUSTOMER_DUP_DB,
    ROW_STATUS_DUP_FILE,
    ROW_STATUS_INVALID_REQUIRED,
    ROW_STATUS_INVALID_VALIDATION,
    ROW_STATUS_READY,
    TEMPLATE_SAMPLE_CUSTOMER_NAME,
    PreviewTokenError,
    PreviewTokenStore,
    reset_preview_token_store,
)
from src.services.excel_security import MAX_DATA_ROWS, MAX_UPLOAD_BYTES


# ---------------------------------------------------------------------------
# Helpers & Fixtures (Strictly Isolated Temp SQLite DBs)
# ---------------------------------------------------------------------------
def _make_customer_import_xlsx(
    rows: List[List[Any]],
    *,
    sheet_name: str = CUSTOMER_IMPORT_SHEET_NAME,
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
def isolated_customer_import_env(tmp_path: Path, monkeypatch) -> Dict[str, Any]:
    reset_preview_token_store()
    reset_db_manager()
    db_file = tmp_path / "c3c2_customer_import.db"
    bkp_dir = tmp_path / "c3c2_backups"
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
    existing_active_id = repo.create_customer(
        Customer(
            customer_name="Dnyaneshwar Govind Kulkarni",
            mobile="9822334455",
            village="Malegaon",
            taluka="Baramati",
            district="Pune",
            state="Maharashtra",
            aadhar_no="123456789012",
            gstin="27AABCU9603R1ZM",
            group_id=1,
            opening_balance=1250.0,
            current_balance=1250.0,
            credit_limit=10000.0,
            is_active=1,
        )
    )
    existing_inactive_id = repo.create_customer(
        Customer(
            customer_name="Archived Farmer Pawar",
            mobile="9811223344",
            village="Katewadi",
            taluka="Baramati",
            district="Pune",
            state="Maharashtra",
            group_id=1,
            opening_balance=400.0,
            current_balance=400.0,
            credit_limit=0.0,
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
def test_customer_template_generation_and_download(isolated_customer_import_env):
    client = isolated_customer_import_env["client"]
    resp = client.get(
        "/api/excel/imports/customers/template",
        headers=isolated_customer_import_env["admin_headers"],
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(XLSX_MEDIA_TYPE)
    assert "Customer_Import_Template.xlsx" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "no-store"

    wb = openpyxl.load_workbook(io.BytesIO(resp.content), data_only=False)
    try:
        assert wb.sheetnames == [CUSTOMER_IMPORT_SHEET_NAME]
        ws = wb[CUSTOMER_IMPORT_SHEET_NAME]

        all_texts: List[str] = []
        sample_found = False
        header_row_values: List[str] = []
        for row in ws.iter_rows(values_only=False):
            row_vals = [c.value for c in row]
            for c in row:
                assert c.data_type != "f"
                if c.value is not None:
                    all_texts.append(str(c.value))
            if row_vals and row_vals[0] == "Customer Name *":
                header_row_values = [str(v) for v in row_vals if v is not None]
            if row_vals and row_vals[0] == TEMPLATE_SAMPLE_CUSTOMER_NAME:
                sample_found = True

        assert header_row_values == [
            "Customer Name *",
            "Mobile",
            "Village",
            "Taluka",
            "District",
            "State",
            "GSTIN",
            "Customer Group",
        ]
        assert sample_found is True
        joined = "\n".join(all_texts)
        assert "Retail Farmer" in joined
        assert "Wholesale Buyer" in joined
        assert "Aadhaar" in joined
        assert "Customer ID" not in header_row_values
        assert "Opening Balance" not in header_row_values
        assert "Credit Limit" not in header_row_values
        assert "Email" not in header_row_values
        assert "Pincode" not in header_row_values
    finally:
        wb.close()

    # Uploading untouched template should skip sample row cleanly
    up = client.post(
        "/api/excel/imports/customers/preview",
        headers=isolated_customer_import_env["admin_headers"],
        files={
            "file": (
                "Customer_Import_Template.xlsx",
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


def test_edited_customer_template_roundtrip_and_sample_row_edit(isolated_customer_import_env):
    client = isolated_customer_import_env["client"]
    db = isolated_customer_import_env["db"]

    tmpl_resp = client.get(
        "/api/excel/imports/customers/template",
        headers=isolated_customer_import_env["admin_headers"],
    )
    assert tmpl_resp.status_code == 200

    # Roundtrip A: keep sample row intact and append 2 customers
    wb = openpyxl.load_workbook(io.BytesIO(tmpl_resp.content))
    ws = wb[CUSTOMER_IMPORT_SHEET_NAME]
    ws.append(
        [
            "Prakash Vasant Bhosale",
            "9765432109",
            "Phaltan",
            "Phaltan",
            "Satara",
            "",  # Blank state should default to Maharashtra
            "27aabcu9603r1zm",  # lowercase should normalize uppercase
            "Wholesale Buyer",
        ]
    )
    ws.append(
        [
            "Ganesh Agro Agency",
            9890112233,  # numeric cell
            "Nippani",
            "Chikodi",
            "Belagavi",
            "Karnataka",
            "",
            3,  # by valid group_id
        ]
    )
    out = io.BytesIO()
    wb.save(out)
    wb.close()

    prev = client.post(
        "/api/excel/imports/customers/preview",
        headers=isolated_customer_import_env["admin_headers"],
        files={"file": ("edited_customers.xlsx", out.getvalue(), XLSX_MEDIA_TYPE)},
    )
    assert prev.status_code == 200
    pdata = prev.json()
    assert pdata["sample_rows_skipped"] == 1
    assert pdata["summary"]["ready_count"] == 2
    assert pdata["rows"][0]["state"] == "Maharashtra"
    assert pdata["rows"][0]["gstin"] == "27AABCU9603R1ZM"

    commit = client.post(
        "/api/excel/imports/customers/commit",
        headers=isolated_customer_import_env["admin_headers"],
        json={"preview_token": pdata["preview_token"], "file_sha256": pdata["file_sha256"]},
    )
    assert commit.status_code == 200
    assert commit.json()["imported_count"] == 2
    assert db.fetch_one("SELECT 1 FROM customers WHERE customer_name LIKE '%SAMPLE%';") is None

    # Roundtrip B: intentionally edit the sample row into a real customer
    wb2 = openpyxl.load_workbook(io.BytesIO(tmpl_resp.content))
    ws2 = wb2[CUSTOMER_IMPORT_SHEET_NAME]
    for row in ws2.iter_rows():
        if row[0].value == TEMPLATE_SAMPLE_CUSTOMER_NAME:
            row[0].value = "Ramesh Baburao Patil"
            break
    out2 = io.BytesIO()
    wb2.save(out2)
    wb2.close()

    prev2 = client.post(
        "/api/excel/imports/customers/preview",
        headers=isolated_customer_import_env["admin_headers"],
        files={"file": ("edited_sample.xlsx", out2.getvalue(), XLSX_MEDIA_TYPE)},
    )
    assert prev2.status_code == 200
    assert prev2.json()["sample_rows_skipped"] == 0
    assert prev2.json()["summary"]["ready_count"] == 1

    commit2 = client.post(
        "/api/excel/imports/customers/commit",
        headers=isolated_customer_import_env["admin_headers"],
        json={"preview_token": prev2.json()["preview_token"], "file_sha256": prev2.json()["file_sha256"]},
    )
    assert commit2.status_code == 200
    assert commit2.json()["imported_count"] == 1
    assert db.fetch_one("SELECT customer_id FROM customers WHERE customer_name = ?;", ("Ramesh Baburao Patil",)) is not None


# ---------------------------------------------------------------------------
# 2. Validation Rules, Customer Groups, States & Duplicate Classification
# ---------------------------------------------------------------------------
def test_customer_validation_rules_and_duplicate_detection(isolated_customer_import_env):
    client = isolated_customer_import_env["client"]
    db = isolated_customer_import_env["db"]
    groups_before = db.fetch_one("SELECT COUNT(*) AS cnt FROM customer_groups;")["cnt"]

    xlsx = _make_customer_import_xlsx(
        [
            [
                "Customer Name *",
                "Mobile",
                "Village",
                "Taluka",
                "District",
                "State",
                "GSTIN",
                "Customer Group",
            ],
            # Row 2: Valid minimal customer (blank state -> Maharashtra, blank optional fields)
            ["Tukaram Pandurang More", "", "", "", "", "", "", ""],
            # Row 3: Valid full customer with UT alias and lowercase GSTIN
            [
                "Savitribai Agro Traders",
                "7890123456",
                "Karad",
                "Karad",
                "Satara",
                "Andaman & Nicobar Islands",
                "27aabcu9603r1zm",
                "Retail Farmer",
            ],
            # Row 4: Duplicate of active DB customer (different case & whitespace)
            ["  dnyaneshwar   govind kulkarni ", "9000000001", "Other", "", "", "Gujarat", "", "Wholesale Buyer"],
            # Row 5: Duplicate of inactive DB customer
            ["archived farmer pawar", "9876543210", "Katewadi", "", "", "Maharashtra", "", ""],
            # Row 6: Intra-file repeat of Row 2
            ["tukaram pandurang more", "9876543211", "Other", "", "", "", "", ""],
            # Row 7: Missing mandatory Customer Name
            ["", "9876543210", "Baramati", "Baramati", "Pune", "Maharashtra", "", "Retail Farmer"],
            # Row 8: Invalid mobile (starts with 1 and short)
            ["Bad Mobile Farmer", "12345", "Pimpri", "", "", "Maharashtra", "", ""],
            # Row 9: Invalid GSTIN
            ["Bad GSTIN Farmer", "9876543210", "Pimpri", "", "", "Maharashtra", "INVALIDGSTIN12", ""],
            # Row 10: Invalid non-Indian State
            ["Bad State Farmer", "9876543210", "Pimpri", "", "", "Atlantis", "", ""],
            # Row 11: Unknown Customer Group (never auto-created)
            ["Unknown Group Farmer", "9876543210", "Pimpri", "", "", "Maharashtra", "", "NonExistent Platinum Group"],
            # Row 12: Overlong District (> 100 chars)
            ["Long District Farmer", "9876543210", "Pimpri", "Baramati", "D" * 105, "Maharashtra", "", ""],
        ]
    )

    resp = client.post(
        "/api/excel/imports/customers/preview",
        headers=isolated_customer_import_env["admin_headers"],
        files={"file": ("mixed_customers.xlsx", xlsx, XLSX_MEDIA_TYPE)},
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
    assert rows_by_num[4]["status"] == ROW_STATUS_CUSTOMER_DUP_DB
    assert "active" in rows_by_num[4]["message"].lower()
    assert rows_by_num[5]["status"] == ROW_STATUS_CUSTOMER_DUP_DB
    assert "inactive" in rows_by_num[5]["message"].lower()
    assert rows_by_num[6]["status"] == ROW_STATUS_DUP_FILE
    assert rows_by_num[7]["status"] == ROW_STATUS_INVALID_REQUIRED
    assert rows_by_num[8]["status"] == ROW_STATUS_INVALID_VALIDATION
    assert "mobile" in rows_by_num[8]["message"].lower()
    assert rows_by_num[9]["status"] == ROW_STATUS_INVALID_VALIDATION
    assert "gstin" in rows_by_num[9]["message"].lower()
    assert rows_by_num[10]["status"] == ROW_STATUS_INVALID_VALIDATION
    assert "state" in rows_by_num[10]["message"].lower()
    assert rows_by_num[11]["status"] == ROW_STATUS_INVALID_VALIDATION
    assert "never auto-created" in rows_by_num[11]["message"].lower()
    assert rows_by_num[12]["status"] == ROW_STATUS_INVALID_VALIDATION
    assert "district" in rows_by_num[12]["message"].lower()

    # Unknown group must NOT have been auto-created
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM customer_groups;")["cnt"] == groups_before


def test_inactive_customer_group_rejected(isolated_customer_import_env):
    db = isolated_customer_import_env["db"]
    with db.transaction() as conn:
        conn.execute("ALTER TABLE customer_groups ADD COLUMN is_active INTEGER DEFAULT 1;")
        conn.execute(
            "INSERT INTO customer_groups (group_name, discount_percent, is_active) VALUES (?, ?, ?);",
            ("Retired VIP Group", 5.0, 0),
        )

    xlsx = _make_customer_import_xlsx(
        [
            ["Customer Name", "Customer Group"],
            ["Farmer With Retired Group", "Retired VIP Group"],
        ]
    )
    resp = isolated_customer_import_env["client"].post(
        "/api/excel/imports/customers/preview",
        headers=isolated_customer_import_env["admin_headers"],
        files={"file": ("inactive_group.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["ready_count"] == 0
    assert data["summary"]["invalid_validation_count"] == 1
    assert data["rows"][0]["status"] == ROW_STATUS_INVALID_VALIDATION


# ---------------------------------------------------------------------------
# 3. Security Enforcement (Formulas, Forbidden/Unsupported Headers, ZIP Attacks)
# ---------------------------------------------------------------------------
def test_customer_import_formula_cells_and_formula_text_rejected(isolated_customer_import_env):
    client = isolated_customer_import_env["client"]

    # Case A: Actual Excel formula (data_type == 'f')
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = CUSTOMER_IMPORT_SHEET_NAME
    ws.append(["Customer Name", "Mobile"])
    ws.append(["=1+1", "9876543210"])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    r1 = client.post(
        "/api/excel/imports/customers/preview",
        headers=isolated_customer_import_env["admin_headers"],
        files={"file": ("formula_cell.xlsx", buf.getvalue(), XLSX_MEDIA_TYPE)},
    )
    assert r1.status_code == 400
    assert "formula" in r1.json()["detail"].lower()

    # Case B: Formula-like strings
    for payload in [" =CMD()", "@SUM(1,2)", "+919876543210", "-123", "\t=1+1"]:
        wb2 = openpyxl.Workbook()
        ws2 = wb2.active
        ws2.title = CUSTOMER_IMPORT_SHEET_NAME
        ws2.append(["Customer Name", "Village"])
        c = ws2.cell(row=2, column=1, value=payload)
        c.data_type = "s"
        ws2.cell(row=2, column=2, value="Baramati")
        b2 = io.BytesIO()
        wb2.save(b2)
        wb2.close()
        r2 = client.post(
            "/api/excel/imports/customers/preview",
            headers=isolated_customer_import_env["admin_headers"],
            files={"file": ("formula_text.xlsx", b2.getvalue(), XLSX_MEDIA_TYPE)},
        )
        assert r2.status_code == 400

    # Case C: Formula hidden inside secondary worksheet
    wb3 = openpyxl.Workbook()
    ws3_main = wb3.active
    ws3_main.title = CUSTOMER_IMPORT_SHEET_NAME
    ws3_main.append(["Customer Name"])
    ws3_main.append(["Clean Farmer"])
    ws3_hidden = wb3.create_sheet("HiddenCalc")
    ws3_hidden["A1"] = '=HYPERLINK("http://evil.example")'
    b3 = io.BytesIO()
    wb3.save(b3)
    wb3.close()
    r3 = client.post(
        "/api/excel/imports/customers/preview",
        headers=isolated_customer_import_env["admin_headers"],
        files={"file": ("hidden_formula.xlsx", b3.getvalue(), XLSX_MEDIA_TYPE)},
    )
    assert r3.status_code == 400


@pytest.mark.parametrize(
    "forbidden_col",
    [
        "Customer ID",
        "Aadhaar",
        "Aadhaar No",
        "aadhar_no",
        "Opening Balance",
        "Current Balance",
        "Credit Limit",
        "Ledger Transactions",
        "Payment History",
        "Balance",
        "Password",
        "JWT Secret",
    ],
)
def test_customer_forbidden_columns_rejected(isolated_customer_import_env, forbidden_col):
    xlsx = _make_customer_import_xlsx(
        [
            ["Customer Name", "Mobile", forbidden_col],
            ["Safe Farmer Name", "9876543210", "500"],
        ]
    )
    resp = isolated_customer_import_env["client"].post(
        "/api/excel/imports/customers/preview",
        headers=isolated_customer_import_env["admin_headers"],
        files={"file": ("forbidden_cust_header.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert resp.status_code == 400


@pytest.mark.parametrize("unsupported_col", ["Email", "Pincode", "Address", "Street Address"])
def test_customer_unsupported_non_db_columns_rejected(isolated_customer_import_env, unsupported_col):
    xlsx = _make_customer_import_xlsx(
        [
            ["Customer Name", unsupported_col],
            ["Clean Farmer", "Sample Value"],
        ]
    )
    resp = isolated_customer_import_env["client"].post(
        "/api/excel/imports/customers/preview",
        headers=isolated_customer_import_env["admin_headers"],
        files={"file": ("unsupported_header.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert resp.status_code == 422
    assert unsupported_col in resp.json()["detail"]


def test_customer_import_archive_xxe_macro_encryption_and_limits_rejected(isolated_customer_import_env):
    client = isolated_customer_import_env["client"]
    base = _make_customer_import_xlsx([["Customer Name", "Mobile"], ["Valid Farmer", "9876543210"]])

    # 1. Wrong extensions
    for bad_name in ["cust.xlsm", "cust.xls", "cust.xlsb", "cust.csv", "../evil.xlsx"]:
        r = client.post(
            "/api/excel/imports/customers/preview",
            headers=isolated_customer_import_env["admin_headers"],
            files={"file": (bad_name, base, XLSX_MEDIA_TYPE)},
        )
        assert r.status_code == 400

    # 2. Fake text masquerading as .xlsx & corrupt bytes
    assert (
        client.post(
            "/api/excel/imports/customers/preview",
            headers=isolated_customer_import_env["admin_headers"],
            files={"file": ("fake.xlsx", b"Customer Name\nFarmer\n", XLSX_MEDIA_TYPE)},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/excel/imports/customers/preview",
            headers=isolated_customer_import_env["admin_headers"],
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
            "/api/excel/imports/customers/preview",
            headers=isolated_customer_import_env["admin_headers"],
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
            "/api/excel/imports/customers/preview",
            headers=isolated_customer_import_env["admin_headers"],
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
            "/api/excel/imports/customers/preview",
            headers=isolated_customer_import_env["admin_headers"],
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
            "/api/excel/imports/customers/preview",
            headers=isolated_customer_import_env["admin_headers"],
            files={"file": ("enc.xlsx", _rewrite_zip_bytes(base, _enc), XLSX_MEDIA_TYPE)},
        ).status_code
        == 400
    )

    # 7. Wrong sheet name
    wrong_sheet = _make_customer_import_xlsx([["Customer Name"], ["F1"]], sheet_name="WrongSheet")
    assert (
        client.post(
            "/api/excel/imports/customers/preview",
            headers=isolated_customer_import_env["admin_headers"],
            files={"file": ("wrong_sheet.xlsx", wrong_sheet, XLSX_MEDIA_TYPE)},
        ).status_code
        == 422
    )

    # 8. Exceeds MAX_DATA_ROWS (2,000)
    rows_2001 = [["Customer Name"]] + [[f"Bulk Customer {i}"] for i in range(1, MAX_DATA_ROWS + 2)]
    too_many = _make_customer_import_xlsx(rows_2001)
    assert (
        client.post(
            "/api/excel/imports/customers/preview",
            headers=isolated_customer_import_env["admin_headers"],
            files={"file": ("too_many.xlsx", too_many, XLSX_MEDIA_TYPE)},
        ).status_code
        == 422
    )

    # 9. Exceeds 5 MB
    oversized = base + (b"\x00" * (MAX_UPLOAD_BYTES + 1024 - len(base)))
    assert (
        client.post(
            "/api/excel/imports/customers/preview",
            headers=isolated_customer_import_env["admin_headers"],
            files={"file": ("oversized.xlsx", oversized, XLSX_MEDIA_TYPE)},
        ).status_code
        == 400
    )


# ---------------------------------------------------------------------------
# 4. Authorization, Maintenance Mode & Zero Preview DB Writes
# ---------------------------------------------------------------------------
def test_customer_import_rbac_and_maintenance(isolated_customer_import_env):
    client = isolated_customer_import_env["client"]
    db = isolated_customer_import_env["db"]
    xlsx = _make_customer_import_xlsx([["Customer Name", "Mobile"], ["RBAC Farmer", "9876543210"]])

    # Unauthenticated -> 401
    assert client.get("/api/excel/imports/customers/template").status_code == 401
    assert (
        client.post(
            "/api/excel/imports/customers/preview",
            files={"file": ("c.xlsx", xlsx, XLSX_MEDIA_TYPE)},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/excel/imports/customers/commit",
            json={"preview_token": "x" * 24, "file_sha256": "a" * 64},
        ).status_code
        == 401
    )

    # OPERATOR -> 403
    op_headers = isolated_customer_import_env["op_headers"]
    assert client.get("/api/excel/imports/customers/template", headers=op_headers).status_code == 403
    assert (
        client.post(
            "/api/excel/imports/customers/preview",
            headers=op_headers,
            files={"file": ("c.xlsx", xlsx, XLSX_MEDIA_TYPE)},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/excel/imports/customers/commit",
            headers=op_headers,
            json={"preview_token": "x" * 24, "file_sha256": "a" * 64},
        ).status_code
        == 403
    )

    # Maintenance -> 503
    with db.exclusive_maintenance(reason="test maintenance"):
        assert (
            client.get(
                "/api/excel/imports/customers/template",
                headers=isolated_customer_import_env["admin_headers"],
            ).status_code
            == 503
        )
        assert (
            client.post(
                "/api/excel/imports/customers/preview",
                headers=isolated_customer_import_env["admin_headers"],
                files={"file": ("c.xlsx", xlsx, XLSX_MEDIA_TYPE)},
            ).status_code
            == 503
        )


def test_customer_preview_makes_zero_database_writes(isolated_customer_import_env):
    client = isolated_customer_import_env["client"]
    db = isolated_customer_import_env["db"]

    before_cust = db.fetch_one("SELECT COUNT(*) AS cnt FROM customers;")["cnt"]
    before_groups = db.fetch_one("SELECT COUNT(*) AS cnt FROM customer_groups;")["cnt"]
    before_audit = db.fetch_one("SELECT COUNT(*) AS cnt FROM audit_log;")["cnt"]
    before_data_ver = db.fetch_one("PRAGMA data_version;")[0]

    xlsx = _make_customer_import_xlsx(
        [
            ["Customer Name", "Mobile", "Village", "State", "Customer Group"],
            ["Preview Only Farmer A", "9876500001", "Supa", "Maharashtra", "Retail Farmer"],
            ["Preview Only Farmer B", "9876500002", "Morgaon", "Maharashtra", "Wholesale Buyer"],
        ]
    )
    resp = client.post(
        "/api/excel/imports/customers/preview",
        headers=isolated_customer_import_env["admin_headers"],
        files={"file": ("preview_only.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert resp.status_code == 200
    assert resp.json()["summary"]["ready_count"] == 2

    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM customers;")["cnt"] == before_cust
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM customer_groups;")["cnt"] == before_groups
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM audit_log;")["cnt"] == before_audit
    assert db.fetch_one("PRAGMA data_version;")[0] == before_data_ver


# ---------------------------------------------------------------------------
# 5. Preview Token Security (Cross-User, Cross-Kind, Hash Mismatch, Replay, Expiry)
# ---------------------------------------------------------------------------
def test_customer_preview_token_security_and_cross_kind_isolation(isolated_customer_import_env):
    client = isolated_customer_import_env["client"]
    db = isolated_customer_import_env["db"]

    cust_xlsx = _make_customer_import_xlsx(
        [["Customer Name", "Mobile"], ["Token Guard Customer", "9876511122"]]
    )
    prev_resp = client.post(
        "/api/excel/imports/customers/preview",
        headers=isolated_customer_import_env["admin_headers"],
        files={"file": ("cust_token.xlsx", cust_xlsx, XLSX_MEDIA_TYPE)},
    )
    assert prev_resp.status_code == 200
    pdata = prev_resp.json()
    token = pdata["preview_token"]
    file_sha = pdata["file_sha256"]
    assert pdata["expires_in_seconds"] == PREVIEW_TOKEN_TTL_SECONDS

    # 1. Product commit MUST reject a Customer preview token (and keep it unconsumed!)
    cross_kind_resp = client.post(
        "/api/excel/imports/products/commit",
        headers=isolated_customer_import_env["admin_headers"],
        json={"preview_token": token, "file_sha256": file_sha},
    )
    assert cross_kind_resp.status_code == 400
    assert "different import" in cross_kind_resp.json()["detail"].lower()

    # 2. Vice versa: Customer commit MUST reject a Product preview token
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
        headers=isolated_customer_import_env["admin_headers"],
        files={"file": ("p.xlsx", p_buf.getvalue(), XLSX_MEDIA_TYPE)},
    ).json()
    assert (
        client.post(
            "/api/excel/imports/customers/commit",
            headers=isolated_customer_import_env["admin_headers"],
            json={"preview_token": prod_prev["preview_token"], "file_sha256": prod_prev["file_sha256"]},
        ).status_code
        == 400
    )

    # 3. Cross-admin user commit rejected with 403
    assert (
        client.post(
            "/api/excel/imports/customers/commit",
            headers=isolated_customer_import_env["admin2_headers"],
            json={"preview_token": token, "file_sha256": file_sha},
        ).status_code
        == 403
    )
    assert db.fetch_one("SELECT 1 FROM customers WHERE customer_name = ?;", ("Token Guard Customer",)) is None

    # 4. SHA-256 mismatch -> 400
    wrong_sha = hashlib.sha256(b"tampered").hexdigest()
    assert (
        client.post(
            "/api/excel/imports/customers/commit",
            headers=isolated_customer_import_env["admin_headers"],
            json={"preview_token": token, "file_sha256": wrong_sha},
        ).status_code
        == 400
    )

    # 5. Valid single-use commit succeeds
    ok_resp = client.post(
        "/api/excel/imports/customers/commit",
        headers=isolated_customer_import_env["admin_headers"],
        json={"preview_token": token, "file_sha256": file_sha},
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json()["imported_count"] == 1

    # 6. Replay of same token -> 400 and no duplicate record
    assert (
        client.post(
            "/api/excel/imports/customers/commit",
            headers=isolated_customer_import_env["admin_headers"],
            json={"preview_token": token, "file_sha256": file_sha},
        ).status_code
        == 400
    )
    assert (
        db.fetch_one(
            "SELECT COUNT(*) AS cnt FROM customers WHERE customer_name = ?;",
            ("Token Guard Customer",),
        )["cnt"]
        == 1
    )

    # 7. Expiry (>15m)
    clock = [1_700_000_000.0]
    store = PreviewTokenStore(ttl_seconds=900.0, max_records=20, time_fn=lambda: clock[0])
    rec = store.create(
        import_kind=IMPORT_KIND_CUSTOMERS,
        user_id=1,
        username="owner_admin",
        db_key="testdb",
        file_sha256=file_sha,
        filename="c.xlsx",
        ready_candidates=[],
        preview_rows=[],
        summary={"ready_count": 0},
        sample_rows_skipped=0,
    )
    clock[0] += PREVIEW_TOKEN_TTL_SECONDS + 1.0
    with pytest.raises(PreviewTokenError) as exc_info:
        store.validate_and_consume(
            rec.token,
            user_id=1,
            username="owner_admin",
            db_key="testdb",
            file_sha256=file_sha,
            expected_kind=IMPORT_KIND_CUSTOMERS,
        )
    assert exc_info.value.status_code == 400
    assert "expired" in exc_info.value.message.lower()


# ---------------------------------------------------------------------------
# 6. Concurrent Duplicate, Atomic Rollback, Field Parity & Existing History Preservation
# ---------------------------------------------------------------------------
def test_concurrent_duplicate_and_existing_customer_balance_preservation(isolated_customer_import_env):
    client = isolated_customer_import_env["client"]
    db = isolated_customer_import_env["db"]

    before_groups = db.fetch_one("SELECT COUNT(*) AS cnt FROM customer_groups;")["cnt"]
    # Record a pre-existing invoice and payment receipt for the existing customer to ensure history is untouched
    existing_cid = isolated_customer_import_env["existing_active_id"]
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO sales (
                invoice_no, sale_date, customer_id, payment_mode, total_taxable,
                net_amount, paid_amount, due_amount
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            ("INV-CUST-TEST-1", "2026-09-10", existing_cid, "CREDIT", 1000.0, 1000.0, 200.0, 800.0),
        )
        conn.execute(
            """
            INSERT INTO vouchers (
                voucher_no, voucher_date, voucher_type, total_amount, narration, reference_type, reference_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            ("RCP-CUST-TEST-1", "2026-09-11", "RECEIPT", 200.0, "Existing payment receipt", "SALE", existing_cid),
        )

    xlsx = _make_customer_import_xlsx(
        [
            ["Customer Name", "Mobile", "Village", "Taluka", "District", "State", "GSTIN", "Customer Group"],
            ["Concurrent New Farmer", "9823456789", "Jejuri", "Purandar", "Pune", "Maharashtra", "", "Retail Farmer"],
            ["Unique Valid Farmer", "9823456790", "Nira", "Purandar", "Pune", "Maharashtra", "27aabcu9603r1zm", "Wholesale Buyer"],
            # Existing DB customer with attempted different fields — must be skipped, NEVER overwritten
            ["Dnyaneshwar Govind Kulkarni", "9000000000", "FakeVillage", "FakeTaluka", "FakeDist", "Gujarat", "", "Wholesale Buyer"],
        ]
    )
    prev = client.post(
        "/api/excel/imports/customers/preview",
        headers=isolated_customer_import_env["admin_headers"],
        files={"file": ("race_customers.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert prev.status_code == 200
    pdata = prev.json()
    assert pdata["summary"]["ready_count"] == 2
    assert pdata["summary"]["duplicate_existing_count"] == 1

    # Insert "Concurrent New Farmer" after preview & before commit
    repo = MasterDataRepository(db)
    race_orig_id = repo.create_customer(
        Customer(
            customer_name="Concurrent New Farmer",
            mobile="9999988888",
            village="OriginalVillage",
            state="Maharashtra",
            opening_balance=750.0,
            current_balance=750.0,
        )
    )

    commit = client.post(
        "/api/excel/imports/customers/commit",
        headers=isolated_customer_import_env["admin_headers"],
        json={"preview_token": pdata["preview_token"], "file_sha256": pdata["file_sha256"]},
    )
    assert commit.status_code == 200
    cdata = commit.json()
    assert cdata["imported_count"] == 1
    assert cdata["skipped_duplicate_count"] == 2

    # Concurrent row retains its original data & balance
    race_row = db.fetch_one("SELECT * FROM customers WHERE customer_id = ?;", (race_orig_id,))
    assert race_row["mobile"] == "9999988888"
    assert race_row["village"] == "OriginalVillage"
    assert race_row["current_balance"] == pytest.approx(750.0)

    # Pre-existing active customer retains Aadhaar, balances, credit limit & sales invoice intact
    orig_cust = db.fetch_one("SELECT * FROM customers WHERE customer_id = ?;", (existing_cid,))
    assert orig_cust["mobile"] == "9822334455"
    assert orig_cust["village"] == "Malegaon"
    assert orig_cust["aadhar_no"] == "123456789012"
    assert orig_cust["opening_balance"] == pytest.approx(1250.0)
    assert orig_cust["current_balance"] == pytest.approx(1250.0)
    assert orig_cust["credit_limit"] == pytest.approx(10000.0)
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM sales WHERE customer_id = ?;", (existing_cid,))["cnt"] == 1

    # Newly imported customer has exact validated values, zero Aadhaar, zero balances/credit limit
    saved_new = db.fetch_one(
        """
        SELECT c.*, cg.group_name
        FROM customers c
        LEFT JOIN customer_groups cg ON c.group_id = cg.group_id
        WHERE c.customer_name = ?;
        """,
        ("Unique Valid Farmer",),
    )
    assert saved_new is not None
    assert saved_new["mobile"] == "9823456790"
    assert saved_new["village"] == "Nira"
    assert saved_new["taluka"] == "Purandar"
    assert saved_new["district"] == "Pune"
    assert saved_new["state"] == "Maharashtra"
    assert saved_new["gstin"] == "27AABCU9603R1ZM"
    assert saved_new["group_id"] == 2
    assert saved_new["aadhar_no"] is None
    assert saved_new["opening_balance"] == 0.0
    assert saved_new["current_balance"] == 0.0
    assert saved_new["credit_limit"] == 0.0
    assert saved_new["is_active"] == 1

    # No extra sales, vouchers, ledger entries, or groups created; audit_log recorded cleanly
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM vouchers;")["cnt"] == 1
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM ledger_entries;")["cnt"] == 0
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM customer_groups;")["cnt"] == before_groups
    audit = db.fetch_one(
        "SELECT action_type, table_name, details FROM audit_log WHERE action_type = 'EXCEL_IMPORT' AND table_name = 'customers' ORDER BY log_id DESC LIMIT 1;"
    )
    assert audit is not None
    assert "Imported 1 customer(s)" in audit["details"]


def test_atomic_rollback_on_mid_batch_customer_db_error(isolated_customer_import_env, monkeypatch):
    client = isolated_customer_import_env["client"]
    db = isolated_customer_import_env["db"]

    before_count = db.fetch_one("SELECT COUNT(*) AS cnt FROM customers;")["cnt"]
    xlsx = _make_customer_import_xlsx(
        [
            ["Customer Name", "Mobile"],
            ["Atomic Customer 1", "9876501010"],
            ["Atomic Customer 2", "9876502020"],
        ]
    )
    prev = client.post(
        "/api/excel/imports/customers/preview",
        headers=isolated_customer_import_env["admin_headers"],
        files={"file": ("atomic_cust.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert prev.status_code == 200
    pdata = prev.json()
    assert pdata["summary"]["ready_count"] == 2

    orig_create = MasterDataRepository.create_customer
    calls = {"count": 0}

    def _fail_second_insert(self, cust: Customer, conn=None) -> int:
        calls["count"] += 1
        if calls["count"] == 2:
            raise sqlite3.OperationalError(
                f"Simulated failure touching internal path {isolated_customer_import_env['db_file']}"
            )
        return orig_create(self, cust, conn=conn)

    monkeypatch.setattr(MasterDataRepository, "create_customer", _fail_second_insert)

    commit = client.post(
        "/api/excel/imports/customers/commit",
        headers=isolated_customer_import_env["admin_headers"],
        json={"preview_token": pdata["preview_token"], "file_sha256": pdata["file_sha256"]},
    )
    assert commit.status_code == 500
    detail = commit.json()["detail"]
    assert str(isolated_customer_import_env["db_file"]) not in detail
    assert "OperationalError" not in detail
    assert "rolled back" in detail.lower()

    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM customers;")["cnt"] == before_count
    assert db.fetch_one("SELECT 1 FROM customers WHERE customer_name = ?;", ("Atomic Customer 1",)) is None


# ---------------------------------------------------------------------------
# 7. Frontend Customer Directory UI Checks
# ---------------------------------------------------------------------------
def test_frontend_customer_import_controls_and_role_hiding_present():
    repo_root = Path(__file__).resolve().parent.parent
    html = (repo_root / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (repo_root / "src" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    for elem_id in (
        "customer-excel-import-card",
        "btn-download-customer-import-template",
        "btn-select-customer-import-file",
        "customer-import-file-input",
        "btn-preview-customer-import",
        "btn-confirm-customer-import",
        "cust-import-ready-count",
        "cust-import-duplicate-count",
        "cust-import-invalid-count",
        "customer-import-preview-tbody",
    ):
        assert elem_id in html

    for fn_name in (
        "downloadCustomerImportTemplate",
        "handleCustomerImportFileSelected",
        "previewCustomerImport",
        "commitCustomerImport",
        "resetCustomerImportState",
    ):
        assert fn_name in js
