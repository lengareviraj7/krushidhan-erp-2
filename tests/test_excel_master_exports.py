"""
Stage C3-B3B Verification Suite: Customer & Supplier Master Excel Exports
Covers the two master-data Excel export endpoints and services:
1. Customer Master (/api/excel/exports/customers)  [ADMIN + OPERATOR]
2. Supplier Master (/api/excel/exports/suppliers)  [ADMIN + OPERATOR]

Verifies:
- RBAC enforcement (401 unauthenticated; 200 for both ADMIN & OPERATOR without confidential fields)
- Exact column parity with existing SQLite `customers`/`customer_groups` and `suppliers` tables
- Proper inactive handling (`include_inactive=True` default vs `False`, Active-first sorting)
- Search (`query`, `%`/`_` wildcard safety), `village`, and `group_id` filters + empty result sheets
- HTTP 422 validation errors for invalid `group_id`, boolean, or overly long filters
- Strict exclusion of Aadhaar, PAN, bank account/IFSC, UPI IDs, balances, credit limits, and auth secrets
- OOXML validity, zero formulas, formula-injection neutralization, '@' text formatting
- Snapshot consistency & closed DB connection BEFORE workbook serialization
- Read-only DB immutability, zero temp `.xlsx`/`.tmp` disk files, HTTP 503 during DB maintenance
- Frontend English-only controls inside Customer Status and non-admin Supplier Khata filter bars
"""
from __future__ import annotations

import hashlib
import io
import re
import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import openpyxl
from fastapi.testclient import TestClient

import src.services.excel_export_service as excel_export_mod
from src.api.routes_excel import XLSX_MEDIA_TYPE
from src.app import app
from src.db.connection import get_db_manager, reset_db_manager
from src.models.master_data import Customer, Supplier
from src.models.system import CompanySettings
from src.repositories.master_data_repository import MasterDataRepository
from src.services.auth_service import AuthService
from src.services.excel_export_service import (
    CUSTOMER_MASTER_NOTE,
    SUPPLIER_MASTER_NOTE,
    ExcelExportService,
)
from src.services.excel_security import validate_xlsx_bytes


FORBIDDEN_SECRET_CANARIES = (
    # Company secrets
    "secret.shop.upi@okicici",
    "Secret Bank Of Maharashtra",
    "60012349876",
    "MAHB0009999",
    # Customer confidential & balance canaries
    "999988887777",
    "444455556666",
    "1234.56",
    "5678.9",
    "98765.43",
    # Supplier confidential & balance canaries (distinct from GSTIN substring)
    "ZPQRS5544M",
    "LMNOP8877K",
    "HDFC Pune",
    "ICICI Nashik",
    "502000123456",
    "602000654321",
    "HDFC0001234",
    "ICIC0004321",
    "2222.33",
    "4444.55",
    # Auth / schema secrets
    "password_hash",
    "jwt_secret",
)

FORBIDDEN_HEADER_LABELS = (
    "Aadhaar",
    "PAN",
    "Opening Balance",
    "Current Balance",
    "Credit Limit",
    "Bank Account",
    "Bank Name",
    "IFSC",
    "UPI",
)


def _iter_workbook_cells(wb: openpyxl.Workbook) -> Iterable[Tuple[str, Any]]:
    for sname in wb.sheetnames:
        ws = wb[sname]
        for row in ws.iter_rows():
            for cell in row:
                yield sname, cell


def _find_table_header_row(ws, first_col_label: str) -> int:
    for r in range(1, ws.max_row + 1):
        if ws.cell(row=r, column=1).value == first_col_label:
            return r
    raise AssertionError(f"Header '{first_col_label}' not found in sheet '{ws.title}'")


def _assert_workbook_hardened(raw_bytes: bytes, filename: str = "master.xlsx") -> None:
    validate_xlsx_bytes(raw_bytes, filename=filename)

    with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
        xml_blob = "\n".join(
            zf.read(name).decode("utf-8", errors="ignore")
            for name in zf.namelist()
            if name.endswith(".xml")
        )
        sheet_xml = "\n".join(
            zf.read(name).decode("utf-8", errors="ignore")
            for name in zf.namelist()
            if name.startswith("xl/worksheets/") and name.endswith(".xml")
        )

    assert not re.search(r"<f(?:\s|>)", sheet_xml), "Unexpected formula <f> element in worksheet XML!"
    for token in FORBIDDEN_SECRET_CANARIES:
        assert token not in xml_blob, f"Sensitive canary '{token}' leaked into workbook XML!"

    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=False)
    try:
        for sname, cell in _iter_workbook_cells(wb):
            val = cell.value
            if isinstance(val, str):
                assert cell.data_type != "f", (
                    f"Unexpected formula in {sname}!{cell.coordinate}: {val}"
                )
                assert not val.startswith("="), (
                    f"Unescaped '=' string in {sname}!{cell.coordinate}: {val}"
                )
                assert cell.number_format == "@", (
                    f"Text cell {sname}!{cell.coordinate} missing '@' format: {cell.number_format}"
                )
    finally:
        wb.close()


def _snapshot_counts_and_digest(db_file: Path) -> Tuple[Dict[str, int], str]:
    conn = sqlite3.connect(str(db_file))
    try:
        counts = {
            tbl: int(conn.execute(f"SELECT COUNT(*) FROM {tbl};").fetchone()[0])
            for tbl in ("company_settings", "users", "customer_groups", "customers", "suppliers")
        }
    finally:
        conn.close()
    return counts, hashlib.sha256(db_file.read_bytes()).hexdigest()


def _setup_master_fixture(tmp_path: Path, monkeypatch) -> Dict[str, Any]:
    reset_db_manager()
    db_file = tmp_path / "c3b3b_masters.db"
    bkp_dir = tmp_path / "c3b3b_bkps"
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    monkeypatch.setenv("CLEAN_INSTALL", "1")
    reset_db_manager()

    db = get_db_manager()
    db.initialize_database(include_seed=True, clean_install=True)

    auth = AuthService(db)
    admin_user = auth.setup_initial_admin(
        username="shop_owner",
        plain_password="OwnerPassword#2026",
        full_name="Rajeshwar Deshmukh",
        company_settings=CompanySettings(
            company_name="Sahyadri Krishi Kendra",
            owner_name="Rajeshwar Deshmukh",
            address="Plot 18, APMC Market Yard",
            city="Baramati",
            state="Maharashtra",
            pincode="413102",
            mobile="9822112233",
            email="contact@sahyadriagro.in",
            gstin="27ABCDE1234F1Z5",
            dl_fertilizer="FERT-PUNE-101",
            dl_pesticide="PEST-PUNE-202",
            dl_seed="SEED-PUNE-303",
            upi_id="secret.shop.upi@okicici",
            bank_name="Secret Bank Of Maharashtra",
            account_no="60012349876",
            ifsc_code="MAHB0009999",
        ),
    )
    op_id = auth.register_user("counter_staff", "StaffPassword#2026", "Anil Kale", role="OPERATOR")
    admin_token = auth.create_access_token(
        admin_user.user_id or 1, "shop_owner", "Rajeshwar Deshmukh", "ADMIN"
    )
    op_token = auth.create_access_token(op_id, "counter_staff", "Anil Kale", "OPERATOR")

    repo = MasterDataRepository(db)

    # Customers across groups, villages, active/inactive states, and hostile strings
    c_active_retail = repo.create_customer(
        Customer(
            customer_name="Anand Kulkarni",
            mobile="9811002233",
            village="Malegaon",
            taluka="Baramati",
            district="Pune",
            state="Maharashtra",
            aadhar_no="999988887777",
            gstin="27aaaaa1111a1z5",  # lowercase -> exported uppercase
            group_id=1,  # Retail Farmer
            opening_balance=1234.56,
            current_balance=5678.90,
            credit_limit=98765.43,
            is_active=1,
        )
    )
    c_active_formula = repo.create_customer(
        Customer(
            customer_name="=CMD|'/C calc'!A0 Patwardhan",
            mobile="+9822001122",
            village="  @Katewadi 100%",
            taluka="-Baramati",
            district="Pune",
            state="Maharashtra",
            aadhar_no="444455556666",
            gstin=None,
            group_id=2,  # Wholesale Buyer
            opening_balance=0.0,
            current_balance=0.0,
            credit_limit=0.0,
            is_active=1,
        )
    )
    c_inactive = repo.create_customer(
        Customer(
            customer_name="Zebra Retired Co-op",
            mobile="9833004455",
            village="Supe",
            taluka="Baramati",
            district="Pune",
            state="Maharashtra",
            aadhar_no="444455556666",
            gstin="27bbbbb2222b1z9",
            group_id=3,  # Farmer Cooperative / Institution
            opening_balance=0.0,
            current_balance=0.0,
            credit_limit=0.0,
            is_active=0,
        )
    )

    # Set deterministic created_at timestamps and ensure current_balance is updated explicitly
    with db.transaction() as cur:
        cur.execute(
            "UPDATE customers SET created_at = '2026-04-15 10:25:00', current_balance = 5678.90 WHERE customer_id = ?;",
            (c_active_retail,),
        )
        cur.execute(
            "UPDATE customers SET created_at = '2026-05-01 09:00:00' WHERE customer_id = ?;",
            (c_active_formula,),
        )
        cur.execute(
            "UPDATE customers SET created_at = '2025-12-20 17:30:00' WHERE customer_id = ?;",
            (c_inactive,),
        )

    # Suppliers across active/inactive states, full profile, and hostile strings
    s_active = repo.create_supplier(
        Supplier(
            supplier_name="Bayer Agro Distributors",
            contact_person="Vikram Joshi",
            mobile="9700112233",
            email="sales@bayerdist.in",
            address="Plot 42, Gultekdi Market Yard",
            city="Pune",
            state="Maharashtra",
            gstin="27abcde9876f1z2",
            pan="ZPQRS5544M",
            dl_number="LIC-PEST-909",
            bank_name="HDFC Pune",
            account_no="502000123456",
            ifsc_code="HDFC0001234",
            opening_balance=2222.33,
            current_balance=4444.55,
            is_active=1,
        )
    )
    s_formula = repo.create_supplier(
        Supplier(
            supplier_name="=1+1 Wildcard 100% Agency",
            contact_person=" +Suresh",
            mobile="-9850123456",
            email="@info@agency.in",
            address="=Shop 5, Nashik Road",
            city="Nashik_West",
            state="Maharashtra",
            gstin="",
            pan="LMNOP8877K",
            dl_number="DL_SEED_50%",
            bank_name="ICICI Nashik",
            account_no="602000654321",
            ifsc_code="ICIC0004321",
            opening_balance=0.0,
            current_balance=0.0,
            is_active=1,
        )
    )
    s_inactive = repo.create_supplier(
        Supplier(
            supplier_name="Old Dormant Chem",
            contact_person="Ramesh Kulkarni",
            mobile="9766554433",
            email="old@dormant.in",
            address="Midc Plot 9",
            city="Satara",
            state="Maharashtra",
            gstin="27ccccc3333c1z1",
            pan="LMNOP8877K",
            dl_number="LIC-OLD-11",
            bank_name="ICICI Nashik",
            account_no="602000654321",
            ifsc_code="ICIC0004321",
            opening_balance=0.0,
            current_balance=0.0,
            is_active=0,
        )
    )

    with db.transaction() as cur:
        cur.execute(
            "UPDATE suppliers SET created_at = '2026-03-10 14:15:00', current_balance = 4444.55 WHERE supplier_id = ?;",
            (s_active,),
        )
        cur.execute(
            "UPDATE suppliers SET created_at = '2026-06-05 11:00:00' WHERE supplier_id = ?;",
            (s_formula,),
        )
        cur.execute(
            "UPDATE suppliers SET created_at = '2025-11-01 08:45:00' WHERE supplier_id = ?;",
            (s_inactive,),
        )

    return {
        "db": db,
        "db_file": db_file,
        "admin_token": admin_token,
        "op_token": op_token,
        "customers": {
            "active_retail": c_active_retail,
            "active_formula": c_active_formula,
            "inactive": c_inactive,
        },
        "suppliers": {
            "active": s_active,
            "formula": s_formula,
            "inactive": s_inactive,
        },
    }


def test_master_exports_rbac_headers_and_content(tmp_path: Path, monkeypatch):
    ctx = _setup_master_fixture(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            for endpoint in ("/api/excel/exports/customers", "/api/excel/exports/suppliers"):
                assert client.get(endpoint).status_code == 401

            # Both ADMIN and OPERATOR may download non-financial directory exports
            for role_headers in (
                {"Authorization": f"Bearer {ctx['op_token']}"},
                {"Authorization": f"Bearer {ctx['admin_token']}"},
            ):
                cust_res = client.get("/api/excel/exports/customers", headers=role_headers)
                assert cust_res.status_code == 200
                assert cust_res.headers["content-type"] == XLSX_MEDIA_TYPE
                assert cust_res.headers["x-content-type-options"] == "nosniff"
                assert cust_res.headers["cache-control"] == "no-store"
                assert 'filename="Customer_Master_' in cust_res.headers["content-disposition"]
                _assert_workbook_hardened(cust_res.content, "Customer_Master.xlsx")

                supp_res = client.get("/api/excel/exports/suppliers", headers=role_headers)
                assert supp_res.status_code == 200
                assert supp_res.headers["content-type"] == XLSX_MEDIA_TYPE
                assert supp_res.headers["x-content-type-options"] == "nosniff"
                assert supp_res.headers["cache-control"] == "no-store"
                assert 'filename="Supplier_Master_' in supp_res.headers["content-disposition"]
                _assert_workbook_hardened(supp_res.content, "Supplier_Master.xlsx")
    finally:
        reset_db_manager()


def test_customer_master_workbook_columns_parity_and_filtering(tmp_path: Path, monkeypatch):
    ctx = _setup_master_fixture(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {ctx['op_token']}"}
            res = client.get("/api/excel/exports/customers", headers=headers)
            assert res.status_code == 200
            _assert_workbook_hardened(res.content, "Customer_Master.xlsx")

            wb = openpyxl.load_workbook(io.BytesIO(res.content), data_only=False)
            assert wb.sheetnames == ["Customer Master"]
            ws = wb["Customer Master"]

            banner_texts = [
                str(ws.cell(row=r, column=1).value or "")
                for r in range(1, 15)
            ]
            assert any("Sahyadri Krishi Kendra" in txt for txt in banner_texts)
            assert any("Customer Master Directory" in txt for txt in banner_texts)
            assert any(CUSTOMER_MASTER_NOTE in txt for txt in banner_texts)

            header_row = _find_table_header_row(ws, "Customer ID")
            actual_headers = [ws.cell(row=header_row, column=c).value for c in range(1, 12)]
            for bad_hdr in FORBIDDEN_HEADER_LABELS:
                assert all(bad_hdr.lower() not in str(h).lower() for h in actual_headers)
            assert actual_headers == [
                "Customer ID",
                "Customer Name",
                "Customer Group",
                "Mobile",
                "Village",
                "Taluka",
                "District",
                "State",
                "GSTIN",
                "Status",
                "Created Date",
            ]
            assert ws.freeze_panes == f"A{header_row + 1}"
            assert ws.auto_filter.ref is not None

            data_rows: List[List[Any]] = []
            for r in range(header_row + 1, ws.max_row + 1):
                cid = ws.cell(row=r, column=1).value
                if cid is not None:
                    data_rows.append([ws.cell(row=r, column=c).value for c in range(1, 12)])

            # Default include_inactive=True returns all 3 customers, Active first then Inactive
            assert len(data_rows) == 3
            assert [row[9] for row in data_rows] == ["Active", "Active", "Inactive"]

            # Row mapping by Customer ID
            by_id = {int(row[0]): row for row in data_rows}
            r_retail = by_id[ctx["customers"]["active_retail"]]
            assert r_retail[1] == "Anand Kulkarni"
            assert r_retail[2] == "Retail Farmer"
            assert r_retail[3] == "9811002233"
            assert r_retail[4] == "Malegaon"
            assert r_retail[5] == "Baramati"
            assert r_retail[6] == "Pune"
            assert r_retail[7] == "Maharashtra"
            assert r_retail[8] == "27AAAAA1111A1Z5"  # uppercased GSTIN
            assert r_retail[9] == "Active"
            assert isinstance(r_retail[10], datetime) and r_retail[10].date() == date(2026, 4, 15)

            # Formula injection neutralized & blank optional fields are "" not "-"
            r_formula = by_id[ctx["customers"]["active_formula"]]
            assert r_formula[1].startswith("'=CMD|")
            assert r_formula[2] == "Wholesale Buyer"
            assert r_formula[3].startswith("'+9822001122")
            assert r_formula[4] in ("'  @Katewadi 100%", "'@Katewadi 100%")
            assert r_formula[5].startswith("'-Baramati")
            assert (r_formula[8] or "") == ""  # Never '-' placeholder

            r_inact = by_id[ctx["customers"]["inactive"]]
            assert r_inact[2] == "Farmer Cooperative / Institution"
            assert r_inact[8] == "27BBBBB2222B1Z9"
            assert r_inact[9] == "Inactive"
            assert isinstance(r_inact[10], datetime) and r_inact[10].date() == date(2025, 12, 20)
            wb.close()

            # include_inactive=false excludes inactive customer
            active_only_res = client.get(
                "/api/excel/exports/customers?include_inactive=false", headers=headers
            )
            assert active_only_res.status_code == 200
            wb2 = openpyxl.load_workbook(io.BytesIO(active_only_res.content), data_only=False)
            ws2 = wb2["Customer Master"]
            hr2 = _find_table_header_row(ws2, "Customer ID")
            ids2 = [
                ws2.cell(row=r, column=1).value
                for r in range(hr2 + 1, ws2.max_row + 1)
                if ws2.cell(row=r, column=1).value is not None
            ]
            assert ids2 == [
                ctx["customers"]["active_formula"],
                ctx["customers"]["active_retail"],
            ] or set(ids2) == {
                ctx["customers"]["active_formula"],
                ctx["customers"]["active_retail"],
            }
            assert ctx["customers"]["inactive"] not in ids2
            wb2.close()

            # Filter by village & wildcard safety ('100%' must match only Katewadi 100%, not Malegaon/Supe)
            wild_res = client.get(
                "/api/excel/exports/customers?village=100%25", headers=headers
            )
            assert wild_res.status_code == 200
            wb3 = openpyxl.load_workbook(io.BytesIO(wild_res.content), data_only=False)
            ws3 = wb3["Customer Master"]
            hr3 = _find_table_header_row(ws3, "Customer ID")
            ids3 = [
                ws3.cell(row=r, column=1).value
                for r in range(hr3 + 1, ws3.max_row + 1)
                if ws3.cell(row=r, column=1).value is not None
            ]
            assert ids3 == [ctx["customers"]["active_formula"]]
            wb3.close()

            # Filter by group_id=3 (Farmer Cooperative / Institution)
            grp_res = client.get(
                "/api/excel/exports/customers?group_id=3", headers=headers
            )
            assert grp_res.status_code == 200
            wb4 = openpyxl.load_workbook(io.BytesIO(grp_res.content), data_only=False)
            ws4 = wb4["Customer Master"]
            meta_row_vals = [str(ws4.cell(row=r, column=1).value or "") for r in range(1, 12)]
            assert any("Customer Group: Farmer Cooperative / Institution" in v for v in meta_row_vals)
            hr4 = _find_table_header_row(ws4, "Customer ID")
            ids4 = [
                ws4.cell(row=r, column=1).value
                for r in range(hr4 + 1, ws4.max_row + 1)
                if ws4.cell(row=r, column=1).value is not None
            ]
            assert ids4 == [ctx["customers"]["inactive"]]
            wb4.close()

            # No-match filter returns clean valid empty header-only sheet
            empty_res = client.get(
                "/api/excel/exports/customers?query=NonExistentFarmerXYZ", headers=headers
            )
            assert empty_res.status_code == 200
            _assert_workbook_hardened(empty_res.content, "Customer_Master_empty.xlsx")
            wb5 = openpyxl.load_workbook(io.BytesIO(empty_res.content), data_only=False)
            ws5 = wb5["Customer Master"]
            hr5 = _find_table_header_row(ws5, "Customer ID")
            assert ws5.max_row == hr5
            wb5.close()
    finally:
        reset_db_manager()


def test_supplier_master_workbook_columns_parity_and_filtering(tmp_path: Path, monkeypatch):
    ctx = _setup_master_fixture(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {ctx['op_token']}"}
            res = client.get("/api/excel/exports/suppliers", headers=headers)
            assert res.status_code == 200
            _assert_workbook_hardened(res.content, "Supplier_Master.xlsx")

            wb = openpyxl.load_workbook(io.BytesIO(res.content), data_only=False)
            assert wb.sheetnames == ["Supplier Master"]
            ws = wb["Supplier Master"]

            banner_texts = [str(ws.cell(row=r, column=1).value or "") for r in range(1, 15)]
            assert any("Sahyadri Krishi Kendra" in txt for txt in banner_texts)
            assert any("Supplier Master Directory" in txt for txt in banner_texts)
            assert any(SUPPLIER_MASTER_NOTE in txt for txt in banner_texts)

            header_row = _find_table_header_row(ws, "Supplier ID")
            actual_headers = [ws.cell(row=header_row, column=c).value for c in range(1, 13)]
            for bad_hdr in FORBIDDEN_HEADER_LABELS:
                assert all(bad_hdr.lower() not in str(h).lower() for h in actual_headers)
            assert actual_headers == [
                "Supplier ID",
                "Supplier Name",
                "Contact Person",
                "Mobile",
                "Email",
                "Address",
                "City",
                "State",
                "GSTIN",
                "Licence No",
                "Status",
                "Created Date",
            ]
            assert ws.freeze_panes == f"A{header_row + 1}"
            assert ws.auto_filter.ref is not None

            data_rows: List[List[Any]] = []
            for r in range(header_row + 1, ws.max_row + 1):
                sid = ws.cell(row=r, column=1).value
                if sid is not None:
                    data_rows.append([ws.cell(row=r, column=c).value for c in range(1, 13)])

            # Default include_inactive=True returns all 3 suppliers, Active first
            assert len(data_rows) == 3
            assert [row[10] for row in data_rows] == ["Active", "Active", "Inactive"]

            by_id = {int(row[0]): row for row in data_rows}
            s_act = by_id[ctx["suppliers"]["active"]]
            assert s_act[1] == "Bayer Agro Distributors"
            assert s_act[2] == "Vikram Joshi"
            assert s_act[3] == "9700112233"
            assert s_act[4] == "sales@bayerdist.in"
            assert s_act[5] == "Plot 42, Gultekdi Market Yard"
            assert s_act[6] == "Pune"
            assert s_act[7] == "Maharashtra"
            assert s_act[8] == "27ABCDE9876F1Z2"  # uppercased GSTIN
            assert s_act[9] == "LIC-PEST-909"
            assert s_act[10] == "Active"
            assert isinstance(s_act[11], datetime) and s_act[11].date() == date(2026, 3, 10)

            # Formula neutralization on supplier name, contact_person, mobile, email, address
            s_form = by_id[ctx["suppliers"]["formula"]]
            assert s_form[1].startswith("'=1+1 Wildcard 100%")
            assert s_form[2] in ("' +Suresh", "'+Suresh")
            assert s_form[3].startswith("'-9850123456")
            assert s_form[4].startswith("'@info@agency.in")
            assert s_form[5].startswith("'=Shop 5, Nashik Road")
            assert (s_form[8] or "") == ""  # Never '-'
            assert s_form[9] == "DL_SEED_50%"

            s_inact = by_id[ctx["suppliers"]["inactive"]]
            assert s_inact[8] == "27CCCCC3333C1Z1"
            assert s_inact[10] == "Inactive"
            assert isinstance(s_inact[11], datetime) and s_inact[11].date() == date(2025, 11, 1)
            wb.close()

            # include_inactive=false excludes dormant supplier
            active_res = client.get(
                "/api/excel/exports/suppliers?include_inactive=false", headers=headers
            )
            assert active_res.status_code == 200
            wb2 = openpyxl.load_workbook(io.BytesIO(active_res.content), data_only=False)
            ws2 = wb2["Supplier Master"]
            hr2 = _find_table_header_row(ws2, "Supplier ID")
            ids2 = {
                int(ws2.cell(row=r, column=1).value)
                for r in range(hr2 + 1, ws2.max_row + 1)
                if ws2.cell(row=r, column=1).value is not None
            }
            assert ids2 == {ctx["suppliers"]["active"], ctx["suppliers"]["formula"]}
            wb2.close()

            # Wildcard safety: searching '50%' matches only s_formula (DL_SEED_50%)
            wild_res = client.get(
                "/api/excel/exports/suppliers?query=50%25", headers=headers
            )
            assert wild_res.status_code == 200
            wb3 = openpyxl.load_workbook(io.BytesIO(wild_res.content), data_only=False)
            ws3 = wb3["Supplier Master"]
            hr3 = _find_table_header_row(ws3, "Supplier ID")
            ids3 = [
                int(ws3.cell(row=r, column=1).value)
                for r in range(hr3 + 1, ws3.max_row + 1)
                if ws3.cell(row=r, column=1).value is not None
            ]
            assert ids3 == [ctx["suppliers"]["formula"]]
            wb3.close()

            # Wildcard '_' safety: 'Nashik_West' matches only s_formula
            und_res = client.get(
                "/api/excel/exports/suppliers?query=Nashik_West", headers=headers
            )
            assert und_res.status_code == 200
            wb4 = openpyxl.load_workbook(io.BytesIO(und_res.content), data_only=False)
            ws4 = wb4["Supplier Master"]
            hr4 = _find_table_header_row(ws4, "Supplier ID")
            ids4 = [
                int(ws4.cell(row=r, column=1).value)
                for r in range(hr4 + 1, ws4.max_row + 1)
                if ws4.cell(row=r, column=1).value is not None
            ]
            assert ids4 == [ctx["suppliers"]["formula"]]
            wb4.close()
    finally:
        reset_db_manager()


def test_master_exports_validation_snapshot_immutability_and_maintenance(tmp_path: Path, monkeypatch):
    ctx = _setup_master_fixture(tmp_path, monkeypatch)
    db = ctx["db"]
    db_file = ctx["db_file"]
    repo_root = Path(__file__).resolve().parent.parent

    try:
        before_counts, before_digest = _snapshot_counts_and_digest(db_file)
        before_tmp = set(tmp_path.rglob("*.xlsx")) | set(tmp_path.rglob("*.tmp"))

        seen_active_conns: List[int] = []
        orig_save = excel_export_mod.save_workbook_to_bytes

        def _spy_save(wb):
            seen_active_conns.append(db.active_connection_count)
            return orig_save(wb)

        monkeypatch.setattr(excel_export_mod, "save_workbook_to_bytes", _spy_save)

        svc = ExcelExportService(db)
        c_bytes, c_name = svc.export_customer_master(generated_by="test")
        s_bytes, s_name = svc.export_supplier_master(generated_by="test")
        assert c_name.startswith("Customer_Master_") and c_bytes
        assert s_name.startswith("Supplier_Master_") and s_bytes
        assert seen_active_conns == [0, 0]

        after_counts, after_digest = _snapshot_counts_and_digest(db_file)
        assert after_counts == before_counts
        assert after_digest == before_digest

        after_tmp = set(tmp_path.rglob("*.xlsx")) | set(tmp_path.rglob("*.tmp"))
        assert after_tmp == before_tmp == set()
        assert list(repo_root.glob("*.xlsx")) == []

        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {ctx['admin_token']}"}

            # Validation 422 checks
            assert client.get("/api/excel/exports/customers?group_id=0", headers=headers).status_code == 422
            assert client.get("/api/excel/exports/customers?group_id=-1", headers=headers).status_code == 422
            assert (
                client.get("/api/excel/exports/customers?include_inactive=notbool", headers=headers).status_code
                == 422
            )
            assert (
                client.get(f"/api/excel/exports/customers?query={'x' * 205}", headers=headers).status_code
                == 422
            )
            assert (
                client.get(f"/api/excel/exports/customers?village={'v' * 205}", headers=headers).status_code
                == 422
            )
            assert (
                client.get(f"/api/excel/exports/suppliers?query={'s' * 205}", headers=headers).status_code
                == 422
            )
            assert (
                client.get("/api/excel/exports/suppliers?include_inactive=notbool", headers=headers).status_code
                == 422
            )

            # Maintenance mode 503 + Retry-After
            with db.exclusive_maintenance():
                c_maint = client.get("/api/excel/exports/customers", headers=headers)
                assert c_maint.status_code == 503
                assert "retry-after" in c_maint.headers

                s_maint = client.get("/api/excel/exports/suppliers", headers=headers)
                assert s_maint.status_code == 503
                assert "retry-after" in s_maint.headers
    finally:
        reset_db_manager()


def test_master_exports_ui_controls_wiring_and_english_only():
    repo_root = Path(__file__).resolve().parent.parent
    html_text = (repo_root / "src" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    js_text = (repo_root / "src" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    # 1. Customer Master UI control inside Farmer Status / Directory tab
    assert 'id="customer-master-export-bar"' in html_text
    assert 'id="cust-master-include-inactive"' in html_text
    assert 'id="btn-export-customer-master-excel"' in html_text
    assert 'onclick="exportCustomerMasterExcel(this)"' in html_text

    # 2. Supplier Master UI control inside non-admin Supplier Filter Bar (NOT inside #supplier-excel-export-card)
    assert 'id="supplier-master-export-bar"' in html_text
    assert 'id="supp-master-include-inactive"' in html_text
    assert 'id="btn-export-supplier-master-excel"' in html_text
    assert 'onclick="exportSupplierMasterExcel(this)"' in html_text

    supp_master_pos = html_text.index('id="supplier-master-export-bar"')
    supp_admin_card_pos = html_text.index('id="supplier-excel-export-card"')
    assert supp_master_pos < supp_admin_card_pos

    # 3. JS helper functions reuse authenticated Blob download pipeline
    assert "function exportCustomerMasterExcel(" in js_text
    assert '"/api/excel/exports/customers"' in js_text
    assert "function exportSupplierMasterExcel(" in js_text
    assert '"/api/excel/exports/suppliers"' in js_text

    # 4. Strict English-only policy across entire `src/`
    for path in (repo_root / "src").rglob("*"):
        if path.is_file() and path.suffix in (".py", ".html", ".js", ".css", ".sql"):
            text = path.read_text(encoding="utf-8")
            for ch in text:
                assert not ("\u0900" <= ch <= "\u097f"), (
                    f"Unexpected Devanagari character '{ch}' found in {path}"
                )
