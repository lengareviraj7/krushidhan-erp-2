"""
Automated Security, Validation, Workbook Builder & RBAC Foundation Tests for Excel Engine (C3-B1).
Covers all 19 mandatory scenarios using isolated temporary/in-memory workbooks and test DBs.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.routes_excel import (
    XLSX_MEDIA_TYPE,
    execute_excel_operation,
    read_validated_xlsx_upload,
    sanitize_download_filename,
    translate_excel_exception,
    xlsx_download_response,
)
from src.app import app
from src.db.connection import DatabaseMaintenanceError, get_db_manager, reset_db_manager
from src.models.system import CompanySettings
from src.services.auth_service import AuthService
from src.services.excel_security import (
    MAX_CELL_LENGTH,
    MAX_COLUMNS,
    MAX_DATA_ROWS,
    MAX_UPLOAD_BYTES,
    ExcelSecurityError,
    ExcelValidationError,
    assert_headers_allowed_for_import,
    ensure_defusedxml_active,
    is_formula_like_string,
    mask_sensitive_aadhaar,
    read_xlsx_sheet_safely,
    sanitize_excel_text,
    validate_date_range,
    validate_date_str,
    validate_decimal_amount,
    validate_email,
    validate_gstin,
    validate_hsn,
    validate_ifsc,
    validate_indian_mobile,
    validate_pan,
    validate_quantity,
    validate_xlsx_bytes,
)
from src.services.excel_workbook_builder import (
    CURRENCY_FORMAT_INR,
    add_company_header,
    add_report_banner,
    add_sheet,
    apply_alternating_row_styles,
    apply_auto_filter,
    apply_freeze_panes,
    auto_fit_columns,
    create_workbook,
    save_workbook_to_bytes,
    write_currency_cell,
    write_date_cell,
    write_percentage_cell,
    write_quantity_cell,
    write_table_header,
    write_text_cell,
    write_totals_row,
)


# ---------------------------------------------------------------------------
# Helpers for constructing synthetic valid & malicious OOXML test artifacts
# ---------------------------------------------------------------------------
def _build_simple_xlsx_bytes(rows: list, sheet_name: str = "Data") -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows:
        ws.append(row)
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


# ---------------------------------------------------------------------------
# Fixtures (isolated DB so live DB is never touched)
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_api_client(tmp_path, monkeypatch):
    db_file = tmp_path / "test_c3b1_excel_rbac.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    reset_db_manager()
    db = get_db_manager()
    db.initialize_database(include_seed=True)
    with TestClient(app) as client:
        yield client, db
    reset_db_manager()


def _auth_header(role: str, user_id: int = 10, username: str = "test_user") -> dict:
    token = AuthService.create_access_token(
        user_id=user_id,
        username=username,
        full_name=f"{role} User",
        role=role,
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Valid XLSX & defusedxml activation
# ---------------------------------------------------------------------------
def test_1_valid_xlsx_and_defusedxml_active():
    ensure_defusedxml_active()
    raw = _build_simple_xlsx_bytes(
        [
            ["Product Name", "HSN", "Rate", "MRP"],
            ["Urea 45Kg Bag", "31021000", 266.50, 266.50],
            ["DAP 50Kg Bag", "31053000", 1350.00, 1350.00],
        ],
        sheet_name="Products",
    )
    validate_xlsx_bytes(raw, filename="products_import.xlsx")
    parsed = read_xlsx_sheet_safely(raw, filename="products_import.xlsx")
    assert parsed["sheet_name"] == "Products"
    assert parsed["headers"] == ["Product Name", "HSN", "Rate", "MRP"]
    assert parsed["row_count"] == 2
    assert parsed["rows"][0] == ["Urea 45Kg Bag", "31021000", 266.5, 266.5]


# ---------------------------------------------------------------------------
# 2. Invalid file types & corrupted signatures
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_filename",
    [
        "products.xlsm",
        "products.xlsb",
        "products.xls",
        "products.csv",
        "products.txt",
        "../products.xlsx",
        "subdir/products.xlsx",
        "products",
    ],
)
def test_2_invalid_file_extensions_rejected(bad_filename):
    raw = _build_simple_xlsx_bytes([["A"], ["B"]])
    with pytest.raises(ExcelSecurityError):
        validate_xlsx_bytes(raw, filename=bad_filename)


def test_2_non_zip_or_legacy_ole_bytes_rejected():
    with pytest.raises(ExcelSecurityError):
        validate_xlsx_bytes(b"col1,col2\n1,2\n", filename="fake.xlsx")

    with pytest.raises(ExcelSecurityError):
        validate_xlsx_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, filename="legacy.xlsx")

    with pytest.raises(ExcelSecurityError):
        validate_xlsx_bytes(b"", filename="empty.xlsx")


# ---------------------------------------------------------------------------
# 3. Encrypted ZIP / encrypted OOXML rejected
# ---------------------------------------------------------------------------
def test_3_encrypted_zip_and_ooxml_rejected():
    base = _build_simple_xlsx_bytes([["Name"], ["Item1"]])

    # Case A: ZIP general purpose encryption flag (bit 0) set in local & central ZIP headers
    raw_flags = bytearray(base)
    idx = 0
    while True:
        pos = raw_flags.find(b"PK\x03\x04", idx)
        if pos == -1:
            break
        raw_flags[pos + 6] |= 0x01
        idx = pos + 4
    idx = 0
    while True:
        pos = raw_flags.find(b"PK\x01\x02", idx)
        if pos == -1:
            break
        raw_flags[pos + 8] |= 0x01
        idx = pos + 4
    with pytest.raises(ExcelSecurityError, match="Encrypted"):
        validate_xlsx_bytes(bytes(raw_flags), filename="enc_flag.xlsx")

    # Case B: OOXML EncryptionInfo / EncryptedPackage entry inside ZIP
    def _add_encrypted_entries(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            zout.writestr(info.filename, zin.read(info.filename))
        zout.writestr("EncryptionInfo", b"fake-encryption-header")
        zout.writestr("EncryptedPackage", b"fake-encrypted-payload")

    enc_ooxml = _rewrite_zip_bytes(base, _add_encrypted_entries)
    with pytest.raises(ExcelSecurityError, match="encrypted|Password"):
        validate_xlsx_bytes(enc_ooxml, filename="encrypted_ooxml.xlsx")


# ---------------------------------------------------------------------------
# 4. Malicious ZIP member paths rejected
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "evil_path",
    [
        "../evil.xml",
        "xl/../../etc/passwd",
        "/etc/passwd",
        "..\\evil.xml",
        "C:\\temp\\payload.xml",
    ],
)
def test_4_malicious_zip_paths_rejected(evil_path):
    base = _build_simple_xlsx_bytes([["Name"], ["Item1"]])

    def _inject_path(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            zout.writestr(info.filename, zin.read(info.filename))
        zout.writestr(evil_path, b"<evil/>")

    tampered = _rewrite_zip_bytes(base, _inject_path)
    with pytest.raises(ExcelSecurityError, match="Unsafe ZIP member path"):
        validate_xlsx_bytes(tampered, filename="traversal.xlsx")


# ---------------------------------------------------------------------------
# 5. ZIP bomb defenses (>250 entries, >25MB uncompressed, >50:1 ratio)
# ---------------------------------------------------------------------------
def test_5_zip_bomb_entry_count_size_and_ratio_rejected():
    base = _build_simple_xlsx_bytes([["Name"], ["Item1"]])

    # Case A: Too many entries (> 250)
    def _many_entries(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            zout.writestr(info.filename, zin.read(info.filename))
        for i in range(255):
            zout.writestr(f"xl/extra_{i}.xml", b"<x/>")

    with pytest.raises(ExcelSecurityError, match="too many entries"):
        validate_xlsx_bytes(_rewrite_zip_bytes(base, _many_entries), filename="many.xlsx")

    # Case B: High compression ratio (> 50:1)
    def _high_ratio(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            zout.writestr(info.filename, zin.read(info.filename))
        zout.writestr("xl/zeros.xml", b"A" * (512 * 1024))

    with pytest.raises(ExcelSecurityError, match="compression ratio|ratio"):
        validate_xlsx_bytes(_rewrite_zip_bytes(base, _high_ratio), filename="ratio_bomb.xlsx")

    # Case C: Total uncompressed size > 25 MB while keeping compression ratio ~7:1 and ZIP < 5 MB
    import os

    part_1mb = os.urandom(140 * 1024) + b"Z" * (884 * 1024)

    def _over_25mb_low_ratio(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            zout.writestr(info.filename, zin.read(info.filename))
        for i in range(26):
            zout.writestr(f"xl/pad_{i}.txt", part_1mb)

    bomb_26mb = _rewrite_zip_bytes(base, _over_25mb_low_ratio)
    assert len(bomb_26mb) < MAX_UPLOAD_BYTES
    with pytest.raises(ExcelSecurityError, match="25 MB"):
        validate_xlsx_bytes(bomb_26mb, filename="over25mb.xlsx")


# ---------------------------------------------------------------------------
# 6. XML entity / XXE attacks rejected
# ---------------------------------------------------------------------------
def test_6_xxe_doctype_entity_attack_rejected():
    base = _build_simple_xlsx_bytes([["Name"], ["Item1"]])

    def _inject_xxe(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "xl/workbook.xml":
                data = (
                    b'<?xml version="1.0" encoding="UTF-8"?>\n'
                    b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
                    + data.split(b"?>", 1)[-1]
                )
            zout.writestr(info.filename, data)

    xxe_bytes = _rewrite_zip_bytes(base, _inject_xxe)
    with pytest.raises(ExcelSecurityError, match="DOCTYPE|ENTITY"):
        validate_xlsx_bytes(xxe_bytes, filename="xxe.xlsx")


# ---------------------------------------------------------------------------
# 7. VBA, macros, ActiveX, OLE embeddings & external links blocked
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_member,bad_payload",
    [
        ("xl/vbaProject.bin", b"VBA_BINARY_DATA"),
        ("xl/embeddings/oleObject1.bin", b"OLE_DATA"),
        ("xl/activeX/activeX1.xml", b"<ocx/>"),
        ("xl/externalLinks/externalLink1.xml", b"<externalLink/>"),
        ("xl/customBinary.bin", b"\x00\x01"),
    ],
)
def test_7_macros_ole_activex_external_links_blocked(bad_member, bad_payload):
    base = _build_simple_xlsx_bytes([["Name"], ["Item1"]])

    def _inject_member(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            zout.writestr(info.filename, zin.read(info.filename))
        zout.writestr(bad_member, bad_payload)

    with pytest.raises(ExcelSecurityError):
        validate_xlsx_bytes(_rewrite_zip_bytes(base, _inject_member), filename="macro_ole.xlsx")


def test_7_external_rel_and_macro_content_type_blocked():
    base = _build_simple_xlsx_bytes([["Name"], ["Item1"]])

    def _inject_external_rel(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "xl/_rels/workbook.xml.rels":
                data = data.replace(
                    b"</Relationships>",
                    b'<Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLinkPath" Target="https://evil.example/leak.xlsx" TargetMode="External"/></Relationships>',
                )
            zout.writestr(info.filename, data)

    with pytest.raises(ExcelSecurityError, match="External"):
        validate_xlsx_bytes(_rewrite_zip_bytes(base, _inject_external_rel), filename="ext_rel.xlsx")

    def _inject_macro_ct(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "[Content_Types].xml":
                data = data.replace(
                    b"spreadsheetml.sheet.main+xml",
                    b"ms-excel.sheet.macroEnabled.main+xml",
                )
            zout.writestr(info.filename, data)

    with pytest.raises(ExcelSecurityError, match="Macro-enabled"):
        validate_xlsx_bytes(_rewrite_zip_bytes(base, _inject_macro_ct), filename="macro_ct.xlsx")


# ---------------------------------------------------------------------------
# 8. Formula cells detected with data_only=False (even with cached results)
# ---------------------------------------------------------------------------
def test_8_formula_cells_rejected_even_with_cached_values():
    raw = _build_simple_xlsx_bytes([["A", "B"], [10, "=A2*2"]])

    # Direct formula without cached value
    with pytest.raises(ExcelSecurityError, match="formula"):
        read_xlsx_sheet_safely(raw, filename="formula.xlsx")

    # Formula WITH cached numeric value <v>20</v> (must NOT bypass via cached value)
    def _add_cached_val(zin: zipfile.ZipFile, zout: zipfile.ZipFile) -> None:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                data = data.replace(b"<f>A2*2</f>", b"<f>A2*2</f><v>20</v>")
            zout.writestr(info.filename, data)

    cached_formula_xlsx = _rewrite_zip_bytes(raw, _add_cached_val)
    with pytest.raises(ExcelSecurityError, match="formula"):
        read_xlsx_sheet_safely(cached_formula_xlsx, filename="cached_formula.xlsx")

    # Multi-sheet workbook: clean Sheet1, malicious formula on Sheet2 must still be rejected
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Sheet1"
    ws1.append(["Product", "Rate"])
    ws1.append(["Urea", 266.5])
    ws2 = wb.create_sheet("HiddenCalc")
    ws2.append(["=1+1"])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    with pytest.raises(ExcelSecurityError, match="formula"):
        read_xlsx_sheet_safely(buf.getvalue(), filename="hidden_formula.xlsx", sheet_name="Sheet1")


# ---------------------------------------------------------------------------
# 9. Formula-like text payloads rejected on import & escaped on export
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        "=cmd|' /C calc'!A0",
        "+1+1",
        "-2+3",
        "@SUM(A1:A5)",
        "\t=1+1",
        "\r+1+1",
        "   =1+1",
        "  \n=SUM(A1:A2)",
        "\x00=1+1",
    ],
)
def test_9_formula_like_text_rejected_on_import_and_escaped_on_export(payload):
    assert is_formula_like_string(payload) is True

    # Note: in XML, literal ASCII control chars like \x00 are stripped before or fail validate_import_cell_value;
    # skip raw \x00 when serializing openpyxl XML cell if openpyxl rejects raw NUL, otherwise test in workbook:
    if "\x00" not in payload:
        raw = _build_simple_xlsx_bytes([["Name"], [payload]])
        with pytest.raises(ExcelSecurityError):
            read_xlsx_sheet_safely(raw, filename="csv_inj.xlsx")

    # Export escaping must prefix literal apostrophe cleanly and strip control chars
    escaped = sanitize_excel_text(payload)
    assert "\x00" not in escaped
    if not escaped.startswith("'"):
        pytest.fail(f"Expected apostrophe escape prefix for export payload {payload!r}, got {escaped!r}")


# ---------------------------------------------------------------------------
# 10-14. Domain Validators (Dates, GSTIN, HSN, Mobile, Decimal, PAN/IFSC/Email)
# ---------------------------------------------------------------------------
def test_10_date_and_range_validation():
    assert validate_date_range("2026-04-01", "2026-11-09") == ("2026-04-01", "2026-11-09")
    assert validate_date_range("2026-11-09", "2026-11-09") == ("2026-11-09", "2026-11-09")

    for bad_date in ("2026-02-30", "2026-13-01", "09-11-2026", "2026/11/09", "", "today"):
        with pytest.raises(ExcelValidationError):
            validate_date_str(bad_date)

    with pytest.raises(ExcelValidationError, match="cannot be after"):
        validate_date_range("2026-11-10", "2026-11-01")


def test_11_gstin_validation():
    assert validate_gstin("27aabcu9603r1zm") == "27AABCU9603R1ZM"
    assert validate_gstin(None, required=False) is None
    assert validate_gstin("   ", required=False) is None

    for bad_gstin in (
        "00AABCU9603R1ZM",  # Invalid state code 00
        "98AABCU9603R1ZM",  # Invalid state code 98
        "27AABCU9603R1A5",  # 14th char must be Z
        "27AABCU9603R0Z5",  # Entity number cannot be 0
        "27SHORT",
        "27AABCU9603R1ZMXX",
        "=27AABCU9603R1Z",
    ):
        with pytest.raises(ExcelValidationError):
            validate_gstin(bad_gstin, required=True)


def test_12_hsn_validation():
    assert validate_hsn("3102") == "3102"
    assert validate_hsn("310210") == "310210"
    assert validate_hsn("38089910") == "38089910"
    assert validate_hsn(31021000) == "31021000"
    assert validate_hsn(None, required=False) is None

    for bad_hsn in ("31", "310", "31021", "3102100", "380899100", "3808ABCD", 3102.5, -3102):
        with pytest.raises(ExcelValidationError):
            validate_hsn(bad_hsn, required=True)


def test_13_indian_mobile_validation():
    assert validate_indian_mobile("9876543210") == "9876543210"
    assert validate_indian_mobile(6123456789) == "6123456789"
    assert validate_indian_mobile(None, required=False) is None

    for bad_mob in (
        "5876543210",
        "1234567890",
        "987654321",
        "98765432101",
        "98765abc10",
        "+919876543210",
        9876543210.5,
    ):
        with pytest.raises(ExcelValidationError):
            validate_indian_mobile(bad_mob, required=True)


def test_14_decimal_quantity_pan_ifsc_email_and_sensitive_guards():
    assert validate_decimal_amount("1234.56") == Decimal("1234.56")
    assert validate_decimal_amount(0, allow_zero=True) == Decimal("0")
    assert validate_quantity("2.500") == Decimal("2.500")

    for bad_dec in (Decimal("NaN"), Decimal("Infinity"), "NaN", "-Infinity", "-1.00", "12.345", True, None, ""):
        with pytest.raises(ExcelValidationError):
            validate_decimal_amount(bad_dec)

    with pytest.raises(ExcelValidationError):
        validate_decimal_amount("0.00", allow_zero=False)
    with pytest.raises(ExcelValidationError):
        validate_quantity("0", allow_zero=False)
    with pytest.raises(ExcelValidationError):
        validate_quantity("1.2345", max_decimals=3)

    assert validate_pan("abcde1234f") == "ABCDE1234F"
    with pytest.raises(ExcelValidationError):
        validate_pan("INVALIDPAN")

    assert validate_ifsc("sbin0001234") == "SBIN0001234"
    with pytest.raises(ExcelValidationError):
        validate_ifsc("SBIN1001234")  # 5th char must be 0

    assert validate_email("owner@shop.in") == "owner@shop.in"
    with pytest.raises(ExcelValidationError):
        validate_email("bad-email")

    assert mask_sensitive_aadhaar("123456789012") == "XXXX-XXXX-9012"
    with pytest.raises(ExcelSecurityError):
        assert_headers_allowed_for_import(["customer_name", "mobile", "opening_balance"])
    with pytest.raises(ExcelSecurityError):
        assert_headers_allowed_for_import(["username", "password_hash"])
    with pytest.raises(ExcelSecurityError):
        assert_headers_allowed_for_import(["customer_name", "aadhar_no"])


# ---------------------------------------------------------------------------
# 15. Oversized uploads (>5 MB) & row/column/cell-length limits
# ---------------------------------------------------------------------------
def test_15_oversized_upload_and_dimension_limits():
    # Upload > 5 MB
    oversized = b"PK\x03\x04" + b"\x00" * MAX_UPLOAD_BYTES
    assert len(oversized) > MAX_UPLOAD_BYTES
    with pytest.raises(ExcelSecurityError, match="maximum allowed size"):
        validate_xlsx_bytes(oversized, filename="huge.xlsx")

    # Rows > 2000 data rows (1 header + 2001 data rows)
    rows_too_many = [["Name", "Qty"]] + [[f"Item {i}", i] for i in range(MAX_DATA_ROWS + 1)]
    raw_many_rows = _build_simple_xlsx_bytes(rows_too_many)
    with pytest.raises(ExcelValidationError, match=str(MAX_DATA_ROWS)):
        read_xlsx_sheet_safely(raw_many_rows, filename="many_rows.xlsx")

    # Columns > 30
    wide_row = [f"Col{i}" for i in range(1, MAX_COLUMNS + 2)]
    raw_wide = _build_simple_xlsx_bytes([wide_row])
    with pytest.raises(ExcelValidationError, match=str(MAX_COLUMNS)):
        read_xlsx_sheet_safely(raw_wide, filename="wide.xlsx")

    # Cell length > 500 chars
    raw_long_cell = _build_simple_xlsx_bytes([["Desc"], ["X" * (MAX_CELL_LENGTH + 1)]])
    with pytest.raises(ExcelValidationError, match=str(MAX_CELL_LENGTH)):
        read_xlsx_sheet_safely(raw_long_cell, filename="long_cell.xlsx")


# ---------------------------------------------------------------------------
# 16 & 17. Workbook Formatting, Dynamic Shop Branding & Numeric Cell Types
# ---------------------------------------------------------------------------
def test_16_and_17_workbook_builder_formatting_and_numeric_types():
    settings = CompanySettings(
        company_name="Sahyadri Agro Kendra",
        owner_name="Suresh Patil",
        address="Plot 12, Market Yard",
        city="Satara",
        state="Maharashtra",
        pincode="415001",
        mobile="9876543210",
        email="shop@sahyadriagro.in",
        gstin="27AABCU9603R1ZM",
        dl_fertilizer="FERT-SAT-001",
        dl_pesticide="PEST-SAT-002",
        dl_seed="SEED-SAT-003",
        upi_id="secret@ybl",  # Must NEVER appear in generated workbook
        account_no="998877665544",  # Must NEVER appear in generated workbook
    )

    wb = create_workbook()
    ws = add_sheet(wb, "[Sales: 2026/Q1?]* Long Title Here Exceeding31")
    assert len(ws.title) <= 31
    assert not any(ch in ws.title for ch in r":\/?*[]")

    next_row = add_company_header(ws, settings, start_row=1, col_span=6)
    next_row = add_report_banner(
        ws,
        "Sales Register",
        from_date="2026-04-01",
        to_date="2026-04-30",
        generated_by="admin",
        start_row=next_row,
        col_span=6,
    )
    header_row = next_row
    data_start = write_table_header(
        ws,
        ["Invoice No", "Sale Date", "Customer", "Qty", "GST %", "Net Amount"],
        row_idx=header_row,
    )

    r = data_start
    write_text_cell(ws, r, 1, "INV-2026-001")
    write_date_cell(ws, r, 2, "2026-04-15")
    write_text_cell(ws, r, 3, "=cmd|'/C calc'!A0")  # Hostile string must be sanitized
    write_quantity_cell(ws, r, 4, Decimal("5.5"))
    write_percentage_cell(ws, r, 5, Decimal("18"))
    write_currency_cell(ws, r, 6, Decimal("3500.50"))
    r += 1

    write_text_cell(ws, r, 1, "INV-2026-002")
    write_date_cell(ws, r, 2, date(2026, 4, 16))
    write_text_cell(ws, r, 3, "  +123456")
    write_quantity_cell(ws, r, 4, Decimal("2"))
    write_percentage_cell(ws, r, 5, Decimal("5"))
    write_currency_cell(ws, r, 6, Decimal("1000.00"))
    data_end = r

    apply_alternating_row_styles(ws, start_row=data_start, end_row=data_end, end_col=6)
    after_totals = write_totals_row(
        ws,
        row_idx=data_end + 1,
        col_totals={4: Decimal("7.5"), 6: Decimal("4500.50")},
        num_cols=6,
        currency_cols=(6,),
        quantity_cols=(4,),
    )
    apply_freeze_panes(ws, header_row=header_row)
    apply_auto_filter(ws, header_row=header_row, last_row=data_end, end_col=6)
    auto_fit_columns(ws)

    # Multi-sheet support
    ws2 = add_sheet(wb, "Summary")
    write_text_cell(ws2, 1, 1, "Total Sales")
    write_currency_cell(ws2, 1, 2, Decimal("4500.50"))

    xlsx_bytes = save_workbook_to_bytes(wb)
    validate_xlsx_bytes(xlsx_bytes, filename="sales_report.xlsx")

    # Inspect serialized workbook using data_only=False
    loaded = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=False)
    try:
        lws = loaded[ws.title]
        assert lws.freeze_panes == f"A{header_row + 1}"
        assert lws.auto_filter.ref == f"A{header_row}:F{data_end}"

        # Dynamic shop header present, NO sensitive UPI/bank account leaked
        banner_dump = "\n".join(
            str(lws.cell(row=row_i, column=1).value or "") for row_i in range(1, header_row)
        )
        assert "Sahyadri Agro Kendra" in banner_dump
        assert "27AABCU9603R1ZM" in banner_dump
        assert "secret@ybl" not in banner_dump
        assert "998877665544" not in banner_dump

        # Check sanitized hostile strings are forced to text ('@') with literal apostrophe
        inj1 = lws.cell(row=data_start, column=3)
        assert inj1.data_type == "s"
        assert inj1.number_format == "@"
        assert inj1.value == "'=cmd|'/C calc'!A0"

        inj2 = lws.cell(row=data_start + 1, column=3)
        assert inj2.data_type == "s"
        assert inj2.number_format == "@"
        assert inj2.value == "'  +123456"

        # Check numeric money/qty/percentage & backend-calculated totals (NO formulas!)
        qty_cell = lws.cell(row=data_start, column=4)
        assert qty_cell.data_type == "n"
        assert qty_cell.value == 5.5

        money_cell = lws.cell(row=data_start, column=6)
        assert money_cell.data_type == "n"
        assert money_cell.value == 3500.50
        assert money_cell.number_format == CURRENCY_FORMAT_INR

        tot_qty = lws.cell(row=after_totals - 1, column=4)
        tot_money = lws.cell(row=after_totals - 1, column=6)
        assert tot_qty.data_type == "n"
        assert tot_qty.value == 7.5
        assert tot_money.data_type == "n"
        assert tot_money.value == 4500.50
        assert not (isinstance(tot_money.value, str) and tot_money.value.startswith("="))
    finally:
        loaded.close()


# ---------------------------------------------------------------------------
# 18. P&L Admin-only RBAC + routes_excel Foundation & Auth
# ---------------------------------------------------------------------------
def test_18_pnl_admin_only_and_excel_foundation_rbac(isolated_api_client):
    client, _ = isolated_api_client
    op_headers = _auth_header("OPERATOR", user_id=202, username="counter_op")
    admin_headers = _auth_header("ADMIN", user_id=1, username="admin")

    # Unauthenticated blocked
    assert client.get("/api/accounting/profit-and-loss?from_date=2026-01-01&to_date=2026-12-31").status_code == 401
    assert client.get("/api/accounting/profit-and-loss/products?from_date=2026-01-01&to_date=2026-12-31").status_code == 401
    assert client.get("/api/excel/health").status_code == 401
    assert client.get("/api/excel/admin/security-policy").status_code == 401

    # OPERATOR rejected with 403 on P&L endpoints & admin excel policy
    assert client.get(
        "/api/accounting/profit-and-loss?from_date=2026-01-01&to_date=2026-12-31",
        headers=op_headers,
    ).status_code == 403
    assert client.get(
        "/api/accounting/profit-and-loss/products?from_date=2026-01-01&to_date=2026-12-31",
        headers=op_headers,
    ).status_code == 403
    assert client.get("/api/excel/admin/security-policy", headers=op_headers).status_code == 403

    # OPERATOR allowed on standard staff /api/excel/health
    health_op = client.get("/api/excel/health", headers=op_headers)
    assert health_op.status_code == 200
    assert health_op.json()["defusedxml_active"] is True

    # ADMIN allowed on P&L & admin Excel endpoints
    assert client.get(
        "/api/accounting/profit-and-loss?from_date=2026-01-01&to_date=2026-12-31",
        headers=admin_headers,
    ).status_code == 200
    assert client.get(
        "/api/accounting/profit-and-loss/products?from_date=2026-01-01&to_date=2026-12-31",
        headers=admin_headers,
    ).status_code == 200
    assert client.get("/api/excel/admin/security-policy", headers=admin_headers).status_code == 200


def test_18_excel_response_headers_and_exception_translation():
    safe_name = sanitize_download_filename('../../bad"\r\nreport')
    assert safe_name.endswith(".xlsx")
    assert "\r" not in safe_name and "\n" not in safe_name and "/" not in safe_name

    resp = xlsx_download_response(b"PK\x03\x04dummy", 'my/evil"\r\nreport.xlsx')
    assert resp.media_type == XLSX_MEDIA_TYPE
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Content-Disposition"] == 'attachment; filename="evil_report.xlsx"'

    assert translate_excel_exception(ExcelSecurityError("bad zip")).status_code == 400
    assert translate_excel_exception(ExcelValidationError("bad date")).status_code == 422

    # UploadFile bounded reader helper check
    import asyncio
    from starlette.datastructures import UploadFile as StarletteUploadFile

    valid_bytes = _build_simple_xlsx_bytes([["Product"], ["Urea"]])
    up_valid = StarletteUploadFile(filename="upload.xlsx", file=io.BytesIO(valid_bytes))
    got_bytes, got_name = asyncio.run(read_validated_xlsx_upload(up_valid))
    assert got_name == "upload.xlsx"
    assert got_bytes == valid_bytes

    up_bad_ext = StarletteUploadFile(filename="macro.xlsm", file=io.BytesIO(valid_bytes))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(read_validated_xlsx_upload(up_bad_ext))
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# 19. Database maintenance mode returns HTTP 503 with Retry-After header
# ---------------------------------------------------------------------------
def test_19_database_maintenance_returns_http_503(isolated_api_client):
    client, db = isolated_api_client
    admin_headers = _auth_header("ADMIN", user_id=1, username="admin")

    with db.exclusive_maintenance(timeout=2.0, reason="excel_test_restore"):
        r_health = client.get("/api/excel/health", headers=admin_headers)
        assert r_health.status_code == 503
        assert r_health.headers.get("Retry-After") == "2"

        r_admin = client.get("/api/excel/admin/security-policy", headers=admin_headers)
        assert r_admin.status_code == 503
        assert r_admin.headers.get("Retry-After") == "2"

    # Also verify execute_excel_operation re-raises DatabaseMaintenanceError directly (never swallowed as 400/500)
    def _raises_maint() -> Any:
        raise DatabaseMaintenanceError("DB restore running")

    with pytest.raises(DatabaseMaintenanceError):
        execute_excel_operation(_raises_maint)
