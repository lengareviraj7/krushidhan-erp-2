"""
Stage C3-C1 Automated Verification Suite: Secure Excel Import Foundation & Product Import

Covers:
1. Product Import Template generation & download (`GET /api/excel/imports/products/template`)
2. Valid workbook preview & downloaded-template round-trip (`POST /api/excel/imports/products/preview`)
3. Tax Group, Category, Manufacturer, Unit, HSN, Prices, Min Stock Alert & Target Crop validation
4. Missing mandatory fields & unknown lookup errors with exact row/column messages
5. Duplicate detection (DB active/inactive & intra-file) — zero overwriting
6. Security enforcement: formula cells (`data_only=False`), formula-like text, forbidden columns
   (stock, batch, balance, purchase rate, secrets), ZIP bombs, XXE, macros, encrypted ZIP, `.xlsm`/`.csv`
7. Authorization (`401` unauthenticated, `403` OPERATOR, ADMIN-only) & `503` maintenance guard
8. Preview makes ZERO DB writes
9. Preview token security: ADMIN-user binding, SHA-256 hash mismatch, expiry (<=15m),
   single-use replay, max-20 eviction & thread safety
10. Atomic commit: concurrent duplicate insertion protection, mid-batch rollback on DB error,
    zero leakage of internal paths/raw SQL errors, exact field parity, zero stock/batch creation
11. Frontend Registered Products UI presence checks
"""
from __future__ import annotations

import hashlib
import io
import sqlite3
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List

import openpyxl
import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.api.routes_excel import XLSX_MEDIA_TYPE
from src.db.connection import DatabaseMaintenanceError, get_db_manager, reset_db_manager
from src.models.master_data import Product
from src.models.system import CompanySettings
from src.repositories.master_data_repository import MasterDataRepository
from src.services.auth_service import AuthService
from src.services.excel_import_service import (
    MAX_ACTIVE_PREVIEW_RECORDS,
    PREVIEW_TOKEN_TTL_SECONDS,
    PRODUCT_IMPORT_SHEET_NAME,
    ROW_STATUS_DUP_DB,
    ROW_STATUS_DUP_FILE,
    ROW_STATUS_INVALID_REQUIRED,
    ROW_STATUS_INVALID_VALIDATION,
    ROW_STATUS_READY,
    TEMPLATE_SAMPLE_PRODUCT_NAME,
    ExcelImportService,
    PreviewTokenError,
    PreviewTokenStore,
    get_preview_token_store,
    reset_preview_token_store,
)
from src.services.excel_security import MAX_DATA_ROWS, MAX_UPLOAD_BYTES


# ---------------------------------------------------------------------------
# Helpers & Fixtures (Strictly Isolated Temp SQLite DBs)
# ---------------------------------------------------------------------------
def _make_product_import_xlsx(
    rows: List[List[Any]],
    *,
    sheet_name: str = PRODUCT_IMPORT_SHEET_NAME,
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
def isolated_import_env(tmp_path: Path, monkeypatch) -> Dict[str, Any]:
    reset_preview_token_store()
    reset_db_manager()
    db_file = tmp_path / "c3c1_product_import.db"
    bkp_dir = tmp_path / "c3c1_backups"
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
    existing_active_id = repo.create_product(
        Product(
            product_name="Existing Bayer Confidor 100ml",
            category_id=3,
            manufacturer_id=1,
            hsn_code="38089199",
            unit_id=1,
            tax_group_id=4,
            default_purchase_rate=310.0,
            default_sale_rate=420.0,
            default_mrp=450.0,
            min_stock_alert=8.0,
            crop_id=2,
            is_active=1,
        )
    )
    existing_inactive_id = repo.create_product(
        Product(
            product_name="Archived Old Herbicide 500ml",
            category_id=4,
            unit_id=7,
            tax_group_id=4,
            default_purchase_rate=200.0,
            default_sale_rate=290.0,
            default_mrp=320.0,
            min_stock_alert=5.0,
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
# 1. Valid Template Generation, Download & Round-Trip Editing
# ---------------------------------------------------------------------------
def test_valid_template_generation_and_download(isolated_import_env):
    client = isolated_import_env["client"]
    resp = client.get(
        "/api/excel/imports/products/template",
        headers=isolated_import_env["admin_headers"],
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(XLSX_MEDIA_TYPE)
    assert "Product_Import_Template.xlsx" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "no-store"

    wb = openpyxl.load_workbook(io.BytesIO(resp.content), data_only=False)
    try:
        assert wb.sheetnames == [PRODUCT_IMPORT_SHEET_NAME]
        ws = wb[PRODUCT_IMPORT_SHEET_NAME]

        # Extract all text content to confirm instructions, sample row & security rules
        all_texts: List[str] = []
        sample_found = False
        header_row_values: List[str] = []
        for row in ws.iter_rows(values_only=False):
            row_vals = [c.value for c in row]
            for c in row:
                assert c.data_type != "f"
                if c.value is not None:
                    all_texts.append(str(c.value))
            if row_vals and row_vals[0] == "Product Name *":
                header_row_values = [str(v) for v in row_vals if v is not None]
            if row_vals and row_vals[0] == TEMPLATE_SAMPLE_PRODUCT_NAME:
                sample_found = True

        assert header_row_values == [
            "Product Name *",
            "Category",
            "Manufacturer / Brand",
            "Unit",
            "HSN Code",
            "Tax Group *",
            "Sale Rate (INR)",
            "MRP (INR)",
            "Min Stock Alert",
            "Target Crop",
        ]
        assert sample_found is True
        joined = "\n".join(all_texts)
        assert "Exempted (0%)" in joined
        assert "GST 5% (Fertilizers & Bulk Inputs)" in joined
        assert "GST 18% (Pesticides, Insecticides & Sprayers)" in joined
        assert "Opening Stock" in joined  # instruction explicitly forbidding stock import
        assert "Purchase Rate" not in header_row_values
        assert "Product Code" not in header_row_values
    finally:
        wb.close()

    # Uploading untouched template should skip sample row and return 0 ready & 0 invalid rows
    up = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={
            "file": (
                "Product_Import_Template.xlsx",
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


def test_edited_downloaded_template_roundtrip_skips_sample_and_imports_edited_rows(isolated_import_env):
    client = isolated_import_env["client"]
    tmpl_resp = client.get(
        "/api/excel/imports/products/template",
        headers=isolated_import_env["admin_headers"],
    )
    assert tmpl_resp.status_code == 200

    # Edit downloaded template: keep instructions & sample row intact, append 2 real products
    wb = openpyxl.load_workbook(io.BytesIO(tmpl_resp.content))
    ws = wb[PRODUCT_IMPORT_SHEET_NAME]
    ws.append(
        [
            "Coragen 60ml Pack",
            "Insecticides & Pesticides",
            "Corteva Agriscience",
            "Bottle",
            "38089199",
            "GST 18% (Pesticides, Insecticides & Sprayers)",
            840.50,
            920.00,
            6,
            "Sugarcane",
        ]
    )
    ws.append(
        [
            "Mahadhan 19:19:19 WSF 1Kg",
            "Chemical Fertilizers",
            "Mahadhan (Smartchem Technologies)",
            "KG",
            31059090,
            2,  # By valid tax_group_id
            165.00,
            190.00,
            15,
            "Tomato",
        ]
    )
    out = io.BytesIO()
    wb.save(out)
    wb.close()
    edited_bytes = out.getvalue()

    prev = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("edited_template.xlsx", edited_bytes, XLSX_MEDIA_TYPE)},
    )
    assert prev.status_code == 200
    pdata = prev.json()
    assert pdata["sample_rows_skipped"] == 1
    assert pdata["summary"]["total_rows"] == 2
    assert pdata["summary"]["ready_count"] == 2
    assert pdata["summary"]["invalid_count"] == 0

    commit = client.post(
        "/api/excel/imports/products/commit",
        headers=isolated_import_env["admin_headers"],
        json={
            "preview_token": pdata["preview_token"],
            "file_sha256": pdata["file_sha256"],
        },
    )
    assert commit.status_code == 200
    cdata = commit.json()
    assert cdata["imported_count"] == 2
    assert cdata["skipped_duplicate_count"] == 0
    assert cdata["invalid_count"] == 0

    # Confirm the untouched sample row was NEVER imported
    db = isolated_import_env["db"]
    sample_in_db = db.fetch_one(
        "SELECT product_id FROM products WHERE product_name LIKE '%SAMPLE%';"
    )
    assert sample_in_db is None


def test_editing_sample_row_imports_real_product_cleanly(isolated_import_env):
    client = isolated_import_env["client"]
    tmpl_resp = client.get(
        "/api/excel/imports/products/template",
        headers=isolated_import_env["admin_headers"],
    )
    assert tmpl_resp.status_code == 200

    wb = openpyxl.load_workbook(io.BytesIO(tmpl_resp.content))
    ws = wb[PRODUCT_IMPORT_SHEET_NAME]
    edited_sample = False
    for row in ws.iter_rows():
        if row and row[0].value == TEMPLATE_SAMPLE_PRODUCT_NAME:
            row[0].value = "Urea 45 Kg Bag"
            edited_sample = True
            break
    assert edited_sample is True

    out = io.BytesIO()
    wb.save(out)
    wb.close()

    prev = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("edited_sample.xlsx", out.getvalue(), XLSX_MEDIA_TYPE)},
    )
    assert prev.status_code == 200
    pdata = prev.json()
    assert pdata["sample_rows_skipped"] == 0
    assert pdata["summary"]["total_rows"] == 1
    assert pdata["summary"]["ready_count"] == 1

    commit = client.post(
        "/api/excel/imports/products/commit",
        headers=isolated_import_env["admin_headers"],
        json={
            "preview_token": pdata["preview_token"],
            "file_sha256": pdata["file_sha256"],
        },
    )
    assert commit.status_code == 200
    assert commit.json()["imported_count"] == 1
    assert (
        isolated_import_env["db"].fetch_one(
            "SELECT product_id FROM products WHERE product_name = ?;",
            ("Urea 45 Kg Bag",),
        )
        is not None
    )


# ---------------------------------------------------------------------------
# 2. Valid Preview, Row Statuses, Lookups & Validation Errors
# ---------------------------------------------------------------------------
def test_valid_preview_and_all_validation_rules(isolated_import_env):
    client = isolated_import_env["client"]
    xlsx = _make_product_import_xlsx(
        [
            list(
                [
                    "Product Name",
                    "Category",
                    "Manufacturer / Brand",
                    "Unit",
                    "HSN Code",
                    "Tax Group",
                    "Sale Rate (INR)",
                    "MRP (INR)",
                    "Min Stock Alert",
                    "Target Crop",
                ]
            ),
            # Row 2: Valid with prefix Tax Group ("GST 5%") & symbol Unit ("BAG")
            [
                "  RCF   Ujjwala   Urea  ",
                "Chemical Fertilizers",
                "Rashtriya Chemicals & Fertilizers (RCF)",
                "BAG",
                "31021000",
                "GST 5%",
                266.50,
                270.00,
                20,
                "Sugarcane",
            ],
            # Row 3: Missing Tax Group -> Invalid — Missing Required Field
            [
                "No Tax Product",
                "Chemical Fertilizers",
                "",
                "Bag",
                "31021000",
                "",
                100.0,
                110.0,
                5,
                "",
            ],
            # Row 4: Missing Product Name -> Invalid — Missing Required Field
            [
                "",
                "Chemical Fertilizers",
                "",
                "Bag",
                "31021000",
                "Exempted (0%)",
                100.0,
                110.0,
                5,
                "",
            ],
            # Row 5: Unknown Category -> Invalid — Validation Error
            [
                "Bad Category Prod",
                "Alien Category",
                "",
                "Bag",
                "31021000",
                "Exempted (0%)",
                100.0,
                110.0,
                5,
                "",
            ],
            # Row 6: Unknown Unit -> Invalid — Validation Error
            [
                "Bad Unit Prod",
                "Chemical Fertilizers",
                "",
                "BUCKET",
                "31021000",
                "Exempted (0%)",
                100.0,
                110.0,
                5,
                "",
            ],
            # Row 7: Unknown Tax Group -> Invalid — Validation Error
            [
                "Bad Tax Prod",
                "Chemical Fertilizers",
                "",
                "Bag",
                "31021000",
                "GST 15%",
                100.0,
                110.0,
                5,
                "",
            ],
            # Row 8: Unknown Manufacturer + Unknown Crop + Bad HSN + Negative Rate -> Invalid — Validation Error
            [
                "Multi Error Prod",
                "Chemical Fertilizers",
                "Fake Brand XYZ",
                "Bag",
                "123",
                "Exempted (0%)",
                -10.0,
                12.345,
                -2,
                "Dragonfruit",
            ],
            # Row 9: Duplicate existing DB product (case + extra whitespace) -> Duplicate — Existing Product
            [
                "  existing   bayer confidor 100ml  ",
                "Insecticides & Pesticides",
                "Bayer CropScience Limited",
                "Bottle",
                "38089199",
                "GST 18%",
                999.0,
                1099.0,
                10,
                "Cotton",
            ],
            # Row 10: Duplicate existing inactive DB product -> Duplicate — Existing Product
            [
                "Archived Old Herbicide 500ml",
                "Fungicides & Herbicides",
                "",
                "ML",
                "38089390",
                "GST 18%",
                100.0,
                120.0,
                1,
                "",
            ],
            # Row 11: Intra-file duplicate matching Row 2 ("RCF Ujjwala Urea") -> Duplicate — Repeated in File
            [
                "rcf ujjwala urea",
                "Chemical Fertilizers",
                "",
                "Bag",
                "31021000",
                "GST 5%",
                268.00,
                275.00,
                10,
                "",
            ],
        ]
    )

    resp = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("mixed_validation.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert resp.status_code == 200
    data = resp.json()
    summary = data["summary"]
    assert summary["total_rows"] == 10
    assert summary["ready_count"] == 1
    assert summary["duplicate_existing_count"] == 2
    assert summary["duplicate_file_count"] == 1
    assert summary["duplicate_count"] == 3
    assert summary["invalid_missing_count"] == 2
    assert summary["invalid_validation_count"] == 4
    assert summary["invalid_count"] == 6

    rows_by_no = {r["row_number"]: r for r in data["rows"]}
    assert rows_by_no[2]["status"] == ROW_STATUS_READY
    assert rows_by_no[2]["product_name"] == "RCF Ujjwala Urea"
    assert rows_by_no[3]["status"] == ROW_STATUS_INVALID_REQUIRED
    assert any(e["column"] == "Tax Group" for e in rows_by_no[3]["errors"])
    assert rows_by_no[4]["status"] == ROW_STATUS_INVALID_REQUIRED
    assert any(e["column"] == "Product Name" for e in rows_by_no[4]["errors"])
    assert rows_by_no[5]["status"] == ROW_STATUS_INVALID_VALIDATION
    assert any(e["column"] == "Category" for e in rows_by_no[5]["errors"])
    assert rows_by_no[6]["status"] == ROW_STATUS_INVALID_VALIDATION
    assert any(e["column"] == "Unit" for e in rows_by_no[6]["errors"])
    assert rows_by_no[7]["status"] == ROW_STATUS_INVALID_VALIDATION
    assert any(e["column"] == "Tax Group" for e in rows_by_no[7]["errors"])
    assert rows_by_no[8]["status"] == ROW_STATUS_INVALID_VALIDATION
    err_cols = {e["column"] for e in rows_by_no[8]["errors"]}
    assert {"Manufacturer / Brand", "HSN Code", "Sale Rate (INR)", "MRP (INR)", "Min Stock Alert", "Target Crop"} <= err_cols
    assert rows_by_no[9]["status"] == ROW_STATUS_DUP_DB
    assert rows_by_no[10]["status"] == ROW_STATUS_DUP_DB
    assert rows_by_no[11]["status"] == ROW_STATUS_DUP_FILE
    assert "Row 2" in rows_by_no[11]["message"]


def test_inactive_tax_group_rejected(isolated_import_env):
    db = isolated_import_env["db"]
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tax_groups (tax_group_name, cgst_rate, sgst_rate, igst_rate, is_active) VALUES (?, 10, 10, 20, 0);",
            ("Legacy 20% Tax",),
        )

    xlsx = _make_product_import_xlsx(
        [
            ["Product Name", "Tax Group"],
            ["Test Item With Inactive Tax", "Legacy 20% Tax"],
        ]
    )
    resp = isolated_import_env["client"].post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("inactive_tax.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["ready_count"] == 0
    assert data["summary"]["invalid_validation_count"] == 1
    assert data["rows"][0]["status"] == ROW_STATUS_INVALID_VALIDATION


# ---------------------------------------------------------------------------
# 3. Upload Security, Formula Injection, Forbidden Columns & ZIP Attacks
# ---------------------------------------------------------------------------
def test_formula_cells_and_formula_like_text_rejected(isolated_import_env):
    client = isolated_import_env["client"]

    # Case A: Actual spreadsheet formula (data_type == 'f')
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = PRODUCT_IMPORT_SHEET_NAME
    ws.append(["Product Name", "Tax Group"])
    ws.append(["=1+1", "Exempted (0%)"])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    r1 = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("formula_cell.xlsx", buf.getvalue(), XLSX_MEDIA_TYPE)},
    )
    assert r1.status_code == 400
    assert "formula" in r1.json()["detail"].lower()

    # Case B: Formula-like text with leading whitespace or prefix characters
    for payload in [" =SUM(A1:A2)", "@cmd|'/C calc'!A0", "+1234", "\t=1+1"]:
        wb2 = openpyxl.Workbook()
        ws2 = wb2.active
        ws2.title = PRODUCT_IMPORT_SHEET_NAME
        ws2.append(["Product Name", "Tax Group"])
        cell = ws2.cell(row=2, column=1, value=payload)
        cell.data_type = "s"
        ws2.cell(row=2, column=2, value="Exempted (0%)")
        b2 = io.BytesIO()
        wb2.save(b2)
        wb2.close()
        r2 = client.post(
            "/api/excel/imports/products/preview",
            headers=isolated_import_env["admin_headers"],
            files={"file": ("formula_text.xlsx", b2.getvalue(), XLSX_MEDIA_TYPE)},
        )
        assert r2.status_code == 400

    # Case C: Formula hidden inside secondary worksheet
    hidden_formula_wb = openpyxl.Workbook()
    ws_main = hidden_formula_wb.active
    ws_main.title = PRODUCT_IMPORT_SHEET_NAME
    ws_main.append(["Product Name", "Tax Group"])
    ws_main.append(["Clean Product", "Exempted (0%)"])
    ws_other = hidden_formula_wb.create_sheet("HiddenCalc")
    ws_other["A1"] = "=HYPERLINK(\"http://evil.example\")"
    b3 = io.BytesIO()
    hidden_formula_wb.save(b3)
    hidden_formula_wb.close()
    r3 = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("second_sheet_formula.xlsx", b3.getvalue(), XLSX_MEDIA_TYPE)},
    )
    assert r3.status_code == 400


@pytest.mark.parametrize(
    "forbidden_col",
    [
        "Opening Stock",
        "Current Stock",
        "Batch No",
        "Batch Qty",
        "Opening Balance",
        "Current Balance",
        "Purchase Rate",
        "Default Purchase Rate",
        "Profit Margin",
        "Password",
        "JWT Secret",
        "Product ID",
    ],
)
def test_stock_batch_balance_purchase_cost_and_secret_columns_rejected(
    isolated_import_env, forbidden_col
):
    xlsx = _make_product_import_xlsx(
        [
            ["Product Name", "Tax Group", forbidden_col],
            ["Safe Name", "Exempted (0%)", "100"],
        ]
    )
    resp = isolated_import_env["client"].post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("forbidden_header.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert resp.status_code == 400


def test_zip_bombs_macros_xxe_encrypted_and_malformed_workbooks_rejected(isolated_import_env):
    client = isolated_import_env["client"]
    base = _make_product_import_xlsx([["Product Name", "Tax Group"], ["Urea", "GST 5%"]])

    # 1. Wrong extensions
    for bad_name in ["products.xlsm", "products.xls", "products.xlsb", "products.csv", "../evil.xlsx"]:
        r = client.post(
            "/api/excel/imports/products/preview",
            headers=isolated_import_env["admin_headers"],
            files={"file": (bad_name, base, XLSX_MEDIA_TYPE)},
        )
        assert r.status_code == 400

    # 2. CSV/plain text masquerading as .xlsx
    r_csv = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("fake.xlsx", b"Product Name,Tax Group\nUrea,GST 5%\n", XLSX_MEDIA_TYPE)},
    )
    assert r_csv.status_code == 400

    # 3. Corrupted / truncated bytes
    r_corrupt = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("corrupt.xlsx", b"PK\x03\x04broken-zip-stream", XLSX_MEDIA_TYPE)},
    )
    assert r_corrupt.status_code == 400

    # 4. XXE DOCTYPE / ENTITY injection inside workbook XML
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

    r_xxe = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("xxe.xlsx", _rewrite_zip_bytes(base, _xxe), XLSX_MEDIA_TYPE)},
    )
    assert r_xxe.status_code == 400

    # 5. Macro VBA binary inside .xlsx
    def _macro(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            zout.writestr(info.filename, zin.read(info.filename))
        zout.writestr("xl/vbaProject.bin", b"FAKEVBA")

    r_macro = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("macro.xlsx", _rewrite_zip_bytes(base, _macro), XLSX_MEDIA_TYPE)},
    )
    assert r_macro.status_code == 400

    # 6. High-compression ZIP bomb (> 50:1 ratio)
    def _bomb(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            zout.writestr(info.filename, zin.read(info.filename))
        zout.writestr("xl/bomb.xml", b"Z" * (512 * 1024))

    r_bomb = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("bomb.xlsx", _rewrite_zip_bytes(base, _bomb), XLSX_MEDIA_TYPE)},
    )
    assert r_bomb.status_code == 400

    # 7. Encrypted OOXML container
    def _enc(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            zout.writestr(info.filename, zin.read(info.filename))
        zout.writestr("EncryptionInfo", b"encrypted")

    r_enc = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("encrypted.xlsx", _rewrite_zip_bytes(base, _enc), XLSX_MEDIA_TYPE)},
    )
    assert r_enc.status_code == 400

    # 8. Wrong sheet name
    wrong_sheet = _make_product_import_xlsx(
        [["Product Name", "Tax Group"], ["Urea", "GST 5%"]],
        sheet_name="WrongSheet",
    )
    r_sheet = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("wrong_sheet.xlsx", wrong_sheet, XLSX_MEDIA_TYPE)},
    )
    assert r_sheet.status_code == 422

    # 9. Exceeds 2,000 data row cap
    rows_2001 = [["Product Name", "Tax Group"]] + [
        [f"Bulk Product {i}", "Exempted (0%)"] for i in range(1, MAX_DATA_ROWS + 2)
    ]
    too_many_rows = _make_product_import_xlsx(rows_2001)
    r_rows = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("too_many_rows.xlsx", too_many_rows, XLSX_MEDIA_TYPE)},
    )
    assert r_rows.status_code == 422

    # 10. Unsupported Product Code column rejected cleanly (does not exist in products table)
    prod_code_xlsx = _make_product_import_xlsx(
        [
            ["Product Code", "Product Name", "Tax Group"],
            ["PRD-001", "Seed Packet", "Exempted (0%)"],
        ]
    )
    r_code = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("product_code_unsupported.xlsx", prod_code_xlsx, XLSX_MEDIA_TYPE)},
    )
    assert r_code.status_code == 422
    assert "Product Code" in r_code.json()["detail"]

    # 11. Exceeds MAX_UPLOAD_BYTES (5 MB) rejected immediately
    oversized_bytes = base + (b"\x00" * (MAX_UPLOAD_BYTES + 1024 - len(base)))
    r_big = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("oversized.xlsx", oversized_bytes, XLSX_MEDIA_TYPE)},
    )
    assert r_big.status_code == 400


# ---------------------------------------------------------------------------
# 4. Authorization, Role Separation & Zero Preview DB Writes
# ---------------------------------------------------------------------------
def test_unauthorized_and_unauthenticated_requests_rejected(isolated_import_env):
    client = isolated_import_env["client"]
    xlsx = _make_product_import_xlsx(
        [["Product Name", "Tax Group"], ["RBAC Seed", "Exempted (0%)"]]
    )

    # Unauthenticated (401)
    assert client.get("/api/excel/imports/products/template").status_code == 401
    assert (
        client.post(
            "/api/excel/imports/products/preview",
            files={"file": ("p.xlsx", xlsx, XLSX_MEDIA_TYPE)},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/excel/imports/products/commit",
            json={"preview_token": "x" * 24, "file_sha256": "a" * 64},
        ).status_code
        == 401
    )

    # OPERATOR user (403)
    op_headers = isolated_import_env["op_headers"]
    assert (
        client.get("/api/excel/imports/products/template", headers=op_headers).status_code
        == 403
    )
    assert (
        client.post(
            "/api/excel/imports/products/preview",
            headers=op_headers,
            files={"file": ("p.xlsx", xlsx, XLSX_MEDIA_TYPE)},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/excel/imports/products/commit",
            headers=op_headers,
            json={"preview_token": "x" * 24, "file_sha256": "a" * 64},
        ).status_code
        == 403
    )


def test_preview_makes_zero_database_writes(isolated_import_env):
    client = isolated_import_env["client"]
    db = isolated_import_env["db"]

    before_prod_count = db.fetch_one("SELECT COUNT(*) AS cnt FROM products;")["cnt"]
    before_audit_count = db.fetch_one("SELECT COUNT(*) AS cnt FROM audit_log;")["cnt"]
    before_data_version = db.fetch_one("PRAGMA data_version;")[0]

    xlsx = _make_product_import_xlsx(
        [
            ["Product Name", "Category", "Unit", "HSN Code", "Tax Group", "Sale Rate (INR)"],
            ["Preview Only Hybrid Seed 1", "Hybrid & Certified Seeds", "PKT", "12099990", "Exempted (0%)", 450.0],
            ["Preview Only Hybrid Seed 2", "Hybrid & Certified Seeds", "PKT", "12099990", "Exempted (0%)", 550.0],
        ]
    )
    resp = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("preview_only.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert resp.status_code == 200
    assert resp.json()["summary"]["ready_count"] == 2

    after_prod_count = db.fetch_one("SELECT COUNT(*) AS cnt FROM products;")["cnt"]
    after_audit_count = db.fetch_one("SELECT COUNT(*) AS cnt FROM audit_log;")["cnt"]
    after_data_version = db.fetch_one("PRAGMA data_version;")[0]

    assert after_prod_count == before_prod_count
    assert after_audit_count == before_audit_count
    assert after_data_version == before_data_version
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM stock_batches;")["cnt"] == 0
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM stock_ledger;")["cnt"] == 0


# ---------------------------------------------------------------------------
# 5. Preview Token Security (Expiry, Cross-User, Replay, Hash Mismatch, Cap)
# ---------------------------------------------------------------------------
def test_expired_cross_user_reused_and_hash_mismatch_preview_tokens(
    isolated_import_env, monkeypatch
):
    client = isolated_import_env["client"]
    db = isolated_import_env["db"]
    xlsx = _make_product_import_xlsx(
        [
            ["Product Name", "Tax Group", "Sale Rate (INR)"],
            ["Token Guard Product", "GST 5%", 300.0],
        ]
    )

    # Preview as admin1
    prev_resp = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("token_test.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert prev_resp.status_code == 200
    pdata = prev_resp.json()
    token = pdata["preview_token"]
    file_sha = pdata["file_sha256"]
    assert pdata["expires_in_seconds"] == PREVIEW_TOKEN_TTL_SECONDS

    # 1. Cross-user attempt by admin2 -> 403; token stays intact and 0 rows inserted
    cross_resp = client.post(
        "/api/excel/imports/products/commit",
        headers=isolated_import_env["admin2_headers"],
        json={"preview_token": token, "file_sha256": file_sha},
    )
    assert cross_resp.status_code == 403
    assert db.fetch_one("SELECT 1 FROM products WHERE product_name = ?;", ("Token Guard Product",)) is None

    # 2. Hash mismatch -> 400; 0 rows inserted
    wrong_sha = hashlib.sha256(b"tampered-workbook-bytes").hexdigest()
    hash_resp = client.post(
        "/api/excel/imports/products/commit",
        headers=isolated_import_env["admin_headers"],
        json={"preview_token": token, "file_sha256": wrong_sha},
    )
    assert hash_resp.status_code == 400
    assert db.fetch_one("SELECT 1 FROM products WHERE product_name = ?;", ("Token Guard Product",)) is None

    # 3. Valid single-use commit succeeds
    ok_resp = client.post(
        "/api/excel/imports/products/commit",
        headers=isolated_import_env["admin_headers"],
        json={"preview_token": token, "file_sha256": file_sha},
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json()["imported_count"] == 1

    # 4. Replay of same preview token -> 400 and no duplicate insert
    replay_resp = client.post(
        "/api/excel/imports/products/commit",
        headers=isolated_import_env["admin_headers"],
        json={"preview_token": token, "file_sha256": file_sha},
    )
    assert replay_resp.status_code == 400
    assert (
        db.fetch_one(
            "SELECT COUNT(*) AS cnt FROM products WHERE product_name = ?;",
            ("Token Guard Product",),
        )["cnt"]
        == 1
    )

    # 5. Expired token rejected & stale records purged
    clock = [1_700_000_000.0]
    store = PreviewTokenStore(ttl_seconds=900.0, max_records=20, time_fn=lambda: clock[0])
    rec = store.create(
        user_id=1,
        username="owner_admin",
        db_key="testdb",
        file_sha256=file_sha,
        filename="t.xlsx",
        ready_candidates=[],
        preview_rows=[],
        summary={"ready_count": 0},
        sample_rows_skipped=0,
    )
    assert store.active_count() == 1
    clock[0] += PREVIEW_TOKEN_TTL_SECONDS + 1.0  # > 15 minutes
    with pytest.raises(PreviewTokenError) as exc_info:
        store.validate_and_consume(
            rec.token,
            user_id=1,
            username="owner_admin",
            db_key="testdb",
            file_sha256=file_sha,
        )
    assert exc_info.value.status_code == 400
    assert "expired" in exc_info.value.message.lower()
    assert store.total_cached_count() == 0


def test_preview_token_store_capacity_and_thread_safety():
    now_val = [10_000.0]
    store = PreviewTokenStore(
        ttl_seconds=900.0,
        max_records=MAX_ACTIVE_PREVIEW_RECORDS,
        time_fn=lambda: now_val[0],
    )
    tokens: List[str] = []
    sha = hashlib.sha256(b"payload").hexdigest()
    for i in range(MAX_ACTIVE_PREVIEW_RECORDS + 5):
        rec = store.create(
            user_id=1,
            username="admin",
            db_key="db",
            file_sha256=sha,
            filename=f"f{i}.xlsx",
            ready_candidates=[],
            preview_rows=[],
            summary={"ready_count": 0},
            sample_rows_skipped=0,
        )
        tokens.append(rec.token)

    assert store.total_cached_count() <= MAX_ACTIVE_PREVIEW_RECORDS
    # Oldest evicted token fails cleanly
    with pytest.raises(PreviewTokenError):
        store.validate_and_consume(
            tokens[0], user_id=1, username="admin", db_key="db", file_sha256=sha
        )

    # Concurrent double-consume race on the newest token allows EXACTLY ONE winner
    target = tokens[-1]
    barrier = threading.Barrier(6)
    winners = []
    lock = threading.Lock()

    def _try_consume():
        barrier.wait()
        try:
            store.validate_and_consume(
                target, user_id=1, username="admin", db_key="db", file_sha256=sha
            )
            with lock:
                winners.append(True)
        except PreviewTokenError:
            pass

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(_try_consume) for _ in range(6)]
        for f in futs:
            f.result()

    assert len(winners) == 1


# ---------------------------------------------------------------------------
# 6. Concurrent Duplicate, Atomic Commit/Rollback & Field Parity
# ---------------------------------------------------------------------------
def test_concurrent_duplicate_insertion_skipped_and_existing_never_overwritten(isolated_import_env):
    client = isolated_import_env["client"]
    db = isolated_import_env["db"]

    xlsx = _make_product_import_xlsx(
        [
            ["Product Name", "Category", "Unit", "HSN Code", "Tax Group", "Sale Rate (INR)", "MRP (INR)"],
            ["Concurrent Race Herbicide", "Fungicides & Herbicides", "Litre", "38089390", "GST 18%", 910.0, 990.0],
            ["Unique New Insecticide", "Insecticides & Pesticides", "Bottle", "38089199", "GST 18%", 540.0, 600.0],
            # Duplicate already in DB before preview, with attempted different Sale Rate
            ["Existing Bayer Confidor 100ml", "Insecticides & Pesticides", "Bottle", "99999999", "Exempted (0%)", 1.0, 1.0],
        ]
    )
    prev = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("race.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert prev.status_code == 200
    pdata = prev.json()
    assert pdata["summary"]["ready_count"] == 2
    assert pdata["summary"]["duplicate_existing_count"] == 1

    # Simulate another session inserting "Concurrent Race Herbicide" AFTER preview & BEFORE commit
    repo = MasterDataRepository(db)
    race_orig_id = repo.create_product(
        Product(
            product_name="Concurrent Race Herbicide",
            category_id=4,
            unit_id=6,
            hsn_code="38089390",
            tax_group_id=4,
            default_purchase_rate=700.0,
            default_sale_rate=850.0,
            default_mrp=900.0,
            min_stock_alert=3.0,
        )
    )

    commit = client.post(
        "/api/excel/imports/products/commit",
        headers=isolated_import_env["admin_headers"],
        json={
            "preview_token": pdata["preview_token"],
            "file_sha256": pdata["file_sha256"],
        },
    )
    assert commit.status_code == 200
    cdata = commit.json()
    assert cdata["imported_count"] == 1
    assert cdata["skipped_duplicate_count"] == 2
    assert cdata["invalid_count"] == 0

    # Verify concurrent record keeps its original rate (not overwritten by import!)
    race_row = db.fetch_one(
        "SELECT * FROM products WHERE product_id = ?;", (race_orig_id,)
    )
    assert race_row["default_sale_rate"] == pytest.approx(850.0)
    assert (
        db.fetch_one(
            "SELECT COUNT(*) AS cnt FROM products WHERE LOWER(product_name) = LOWER(?);",
            ("Concurrent Race Herbicide",),
        )["cnt"]
        == 1
    )

    # Verify pre-existing Bayer Confidor was NEVER overwritten
    orig_bayer = db.fetch_one(
        "SELECT * FROM products WHERE product_id = ?;",
        (isolated_import_env["existing_active_id"],),
    )
    assert orig_bayer["default_sale_rate"] == pytest.approx(420.0)
    assert orig_bayer["hsn_code"] == "38089199"
    assert orig_bayer["tax_group_id"] == 4


def test_atomic_rollback_on_mid_batch_database_error_and_safe_error_message(
    isolated_import_env, monkeypatch
):
    client = isolated_import_env["client"]
    db = isolated_import_env["db"]

    before_count = db.fetch_one("SELECT COUNT(*) AS cnt FROM products;")["cnt"]
    xlsx = _make_product_import_xlsx(
        [
            ["Product Name", "Tax Group", "Sale Rate (INR)"],
            ["Atomic Batch Row 1", "GST 5%", 110.0],
            ["Atomic Batch Row 2", "GST 5%", 220.0],
        ]
    )
    prev = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("atomic.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert prev.status_code == 200
    pdata = prev.json()
    assert pdata["summary"]["ready_count"] == 2

    orig_create = MasterDataRepository.create_product
    calls = {"count": 0}

    def _fail_second_insert(self, prod: Product, conn=None) -> int:
        calls["count"] += 1
        if calls["count"] == 2:
            raise sqlite3.OperationalError(
                f"Simulated failure touching internal path {isolated_import_env['db_file']}"
            )
        return orig_create(self, prod, conn=conn)

    monkeypatch.setattr(MasterDataRepository, "create_product", _fail_second_insert)

    commit = client.post(
        "/api/excel/imports/products/commit",
        headers=isolated_import_env["admin_headers"],
        json={
            "preview_token": pdata["preview_token"],
            "file_sha256": pdata["file_sha256"],
        },
    )
    assert commit.status_code == 500
    detail = commit.json()["detail"]
    # Internal filesystem path and raw SQLite message must NEVER leak
    assert str(isolated_import_env["db_file"]) not in detail
    assert "OperationalError" not in detail
    assert "rolled back" in detail.lower()

    # Row 1 MUST have rolled back cleanly!
    after_count = db.fetch_one("SELECT COUNT(*) AS cnt FROM products;")["cnt"]
    assert after_count == before_count
    assert db.fetch_one("SELECT 1 FROM products WHERE product_name = ?;", ("Atomic Batch Row 1",)) is None


def test_imported_product_fields_match_and_no_stock_created(isolated_import_env):
    client = isolated_import_env["client"]
    db = isolated_import_env["db"]

    xlsx = _make_product_import_xlsx(
        [
            [
                "Product Name *",
                "Category",
                "Manufacturer / Brand",
                "Unit",
                "HSN Code",
                "Tax Group *",
                "Sale Rate (INR)",
                "MRP (INR)",
                "Min Stock Alert",
                "Target Crop",
            ],
            [
                "Syngenta Amistar Top 100ml",
                "Fungicides & Herbicides",
                "Syngenta India Limited",
                "Bottle",
                "38089290",
                "GST 18% (Pesticides, Insecticides & Sprayers)",
                580.75,
                640.00,
                12.5,
                "Grapes",
            ],
        ]
    )
    prev = client.post(
        "/api/excel/imports/products/preview",
        headers=isolated_import_env["admin_headers"],
        files={"file": ("exact_match.xlsx", xlsx, XLSX_MEDIA_TYPE)},
    )
    assert prev.status_code == 200
    pdata = prev.json()

    commit = client.post(
        "/api/excel/imports/products/commit",
        headers=isolated_import_env["admin_headers"],
        json={
            "preview_token": pdata["preview_token"],
            "file_sha256": pdata["file_sha256"],
        },
    )
    assert commit.status_code == 200
    assert commit.json()["imported_count"] == 1

    saved = db.fetch_one(
        """
        SELECT p.*, c.category_name, m.manufacturer_name, u.symbol AS unit_symbol,
               tg.tax_group_name, cr.crop_name
        FROM products p
        LEFT JOIN categories c ON p.category_id = c.category_id
        LEFT JOIN manufacturers m ON p.manufacturer_id = m.manufacturer_id
        LEFT JOIN units u ON p.unit_id = u.unit_id
        LEFT JOIN tax_groups tg ON p.tax_group_id = tg.tax_group_id
        LEFT JOIN crops cr ON p.crop_id = cr.crop_id
        WHERE p.product_name = ?;
        """,
        ("Syngenta Amistar Top 100ml",),
    )
    assert saved is not None
    assert saved["category_name"] == "Fungicides & Herbicides"
    assert saved["manufacturer_name"] == "Syngenta India Limited"
    assert saved["unit_symbol"] == "BTL"
    assert saved["hsn_code"] == "38089290"
    assert saved["tax_group_id"] == 4
    assert saved["default_purchase_rate"] == 0.0
    assert saved["default_sale_rate"] == pytest.approx(580.75)
    assert saved["default_mrp"] == pytest.approx(640.00)
    assert saved["min_stock_alert"] == pytest.approx(12.5)
    assert saved["crop_name"] == "Grapes"
    assert saved["is_active"] == 1

    # Stock batches and stock ledger must remain untouched (zero initialization)
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM stock_batches;")["cnt"] == 0
    assert db.fetch_one("SELECT COUNT(*) AS cnt FROM stock_ledger;")["cnt"] == 0

    # Audit log recorded cleanly
    audit = db.fetch_one(
        "SELECT action_type, table_name, details FROM audit_log WHERE action_type = 'EXCEL_IMPORT' ORDER BY log_id DESC LIMIT 1;"
    )
    assert audit is not None
    assert audit["table_name"] == "products"
    assert "Imported 1 product(s)" in audit["details"]


def test_maintenance_mode_returns_503_on_import_endpoints(isolated_import_env):
    client = isolated_import_env["client"]
    db = isolated_import_env["db"]
    xlsx = _make_product_import_xlsx(
        [["Product Name", "Tax Group"], ["Maint Prod", "Exempted (0%)"]]
    )

    with db.exclusive_maintenance(reason="test maintenance"):
        r1 = client.get(
            "/api/excel/imports/products/template",
            headers=isolated_import_env["admin_headers"],
        )
        r2 = client.post(
            "/api/excel/imports/products/preview",
            headers=isolated_import_env["admin_headers"],
            files={"file": ("maint.xlsx", xlsx, XLSX_MEDIA_TYPE)},
        )
        assert r1.status_code == 503
        assert r2.status_code == 503


# ---------------------------------------------------------------------------
# 7. Frontend Registered Products UI Checks
# ---------------------------------------------------------------------------
def test_frontend_product_import_controls_and_role_hiding_present():
    repo_root = Path(__file__).resolve().parent.parent
    html = (repo_root / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (repo_root / "src" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    for elem_id in (
        "product-excel-import-card",
        "btn-download-product-import-template",
        "btn-select-product-import-file",
        "product-import-file-input",
        "btn-preview-product-import",
        "btn-confirm-product-import",
        "prod-import-ready-count",
        "prod-import-duplicate-count",
        "prod-import-invalid-count",
        "product-import-preview-tbody",
    ):
        assert elem_id in html

    for fn_name in (
        "downloadProductImportTemplate",
        "handleProductImportFileSelected",
        "previewProductImport",
        "commitProductImport",
        "resetProductImportState",
    ):
        assert fn_name in js
