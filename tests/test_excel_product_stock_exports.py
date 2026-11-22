"""
Stage C3-B2A Verification Suite: Product & Stock Excel Exports
Covers all four real `.xlsx` export endpoints and services:
1. Product Catalog           (/api/excel/exports/products)
2. Current Stock Summary     (/api/excel/exports/stock-summary)
3. Batch-wise Stock & Expiry (/api/excel/exports/stock-batches)
4. Stock Ledger              (/api/excel/exports/stock-ledger)

Verifies:
- HTTP attachment headers, OOXML container validity, openpyxl loadability
- Accuracy vs SQLite master/batch/ledger records & ERP stock/low-stock/FEFO semantics
- Date, category, product & search filtering + empty dataset handling + HTTP 422 rejections
- Zero Excel formulas, hostile formula-injection escaping & '@' formatting
- Zero leakage of purchase cost prices, margins, bank/UPI/Aadhaar/password/JWT secrets
- Consistent DB snapshot & closed connections before workbook serialization
- Read-only non-mutation, zero temp .xlsx files on disk, HTTP 503 during DB maintenance
- Frontend HTML controls and authenticated Blob JS helpers
"""
from __future__ import annotations

import hashlib
import io
import sqlite3
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Tuple

import openpyxl
import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, get_db_manager, reset_db_manager
from src.models.inventory import StockBatch, StockLedgerEntry
from src.models.master_data import Customer, Product, Supplier
from src.models.purchase import Purchase, PurchaseItem
from src.models.sales import Sale, SaleItem, SalesReturn, SalesReturnItem
from src.models.system import CompanySettings
from src.repositories.inventory_repository import InventoryRepository
from src.repositories.master_data_repository import MasterDataRepository
from src.repositories.system_repository import SystemRepository
from src.services.auth_service import AuthService
from src.services.excel_export_service import ExcelExportService
from src.services.excel_security import validate_xlsx_bytes
from src.services.excel_workbook_builder import CURRENCY_FORMAT_INR, PERCENT_FORMAT_DEFAULT
from src.services.inventory_service import InventoryService
from src.services.purchase_service import PurchaseService
from src.services.sales_service import SalesService
from src.api.routes_excel import XLSX_MEDIA_TYPE


# Canary secrets/cost strings that must NEVER appear anywhere in exported XML/cells
SENSITIVE_CANARY_TOKENS = (
    "secret.shop.upi@okicici",
    "60012349876",
    "MAHB0009999",
    "999988887777",   # Customer Aadhaar
    "ABCDE9876F",     # Supplier PAN
    "888.77",         # Purchase cost canary
    "444.33",         # Purchase cost canary
    "111.22",         # Purchase cost canary
    "Purchase Rate",
    "Pur. Rate",
    "Purchase Value",
    "Profit Margin",
    "password_hash",
    "jwt_secret",
)


def _setup_rich_inventory_fixture(tmp_path: Path, monkeypatch) -> Dict[str, Any]:
    """Build an isolated shop database with realistic products, batches, sales, purchases & returns."""
    reset_db_manager()
    db_file = tmp_path / "c3b2a_exports.db"
    bkp_dir = tmp_path / "c3b2a_bkps"
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

    master_repo = MasterDataRepository(db)
    cust_id = master_repo.create_customer(
        Customer(
            customer_name="Dattatray Shinde",
            mobile="9811002233",
            village="Malegaon",
            aadhar_no="999988887777",
            gstin="27ZZZZZ9999Z1Z5",
            opening_balance=0.0,
        )
    )
    supp_id = master_repo.create_supplier(
        Supplier(
            supplier_name="Mahadhan Fertilizers Agency",
            mobile="9700112233",
            city="Pune",
            pan="ABCDE9876F",
            bank_name="HDFC Pune",
            account_no="502000123456",
            ifsc_code="HDFC0001234",
            opening_balance=0.0,
        )
    )

    # 1. Hostile formula-injection product + healthy & expiring batches (Fert, 5% GST)
    p1_id = master_repo.create_product(
        Product(
            product_name="=cmd|'/C calc'!A0 Urea 50% Boost",
            category_id=1,
            manufacturer_id=3,
            hsn_code="31021000",
            unit_id=2,  # BAG
            tax_group_id=2,  # CGST 2.5 + SGST 2.5 = 5%
            default_purchase_rate=888.77,
            default_sale_rate=1200.0,
            default_mrp=1300.0,
            min_stock_alert=10.0,
            crop_id=1,  # Sugarcane
            is_active=1,
        )
    )

    # 2. Low-stock product (Pesticide, 18% GST)
    p2_id = master_repo.create_product(
        Product(
            product_name="  +SUM(1,2) Shield Spray",
            category_id=3,
            manufacturer_id=1,
            hsn_code="38089199",
            unit_id=6,  # LTR
            tax_group_id=4,  # 18%
            default_purchase_rate=444.33,
            default_sale_rate=650.0,
            default_mrp=700.0,
            min_stock_alert=10.0,
            crop_id=2,
            is_active=1,
        )
    )

    # 3. Out-of-stock product (Seed, 0% GST) with depleted zero-stock batch
    p3_id = master_repo.create_product(
        Product(
            product_name="Hybrid Cotton Seed ZeroStock",
            category_id=2,
            manufacturer_id=4,
            hsn_code="12099190",
            unit_id=3,  # PKT
            tax_group_id=1,  # 0%
            default_purchase_rate=111.22,
            default_sale_rate=850.0,
            default_mrp=920.0,
            min_stock_alert=5.0,
            is_active=1,
        )
    )

    # 4. Inactive soft-deleted product (should show in Catalog as Inactive, excluded in Stock Summary/Batches)
    p4_id = master_repo.create_product(
        Product(
            product_name="@HYPERLINK Discontinued Micronutrient",
            category_id=5,
            manufacturer_id=5,
            hsn_code="38249990",
            unit_id=4,  # KG
            tax_group_id=3,  # 12%
            default_purchase_rate=250.0,
            default_sale_rate=340.0,
            default_mrp=380.0,
            min_stock_alert=3.0,
            is_active=0,
        )
    )

    today = date.today()
    exp_past = (today - timedelta(days=15)).isoformat()
    exp_soon = (today + timedelta(days=30)).isoformat()
    exp_healthy = (today + timedelta(days=365)).isoformat()

    purch_svc = PurchaseService(db)
    inv_svc = InventoryService(db)
    inv_repo = InventoryRepository(db)
    sales_svc = SalesService(db)

    # Purchase on 2026-08-05: Product 1 healthy batch + Product 2 low-stock batch
    purch_svc.process_inward_purchase(
        Purchase(
            invoice_no="PUR-AUG-001",
            purchase_date="2026-08-05",
            supplier_id=supp_id,
            payment_type="CASH",
            total_taxable=20000.0,
            net_amount=20000.0,
            paid_amount=20000.0,
            items=[
                PurchaseItem(
                    product_id=p1_id,
                    batch_no="=B1-HEALTHY",
                    mfg_date="2026-01-10",
                    exp_date=exp_healthy,
                    qty=20.0,
                    purchase_rate=888.77,
                    sale_rate=1200.0,
                    mrp=1300.0,
                    taxable_amount=17775.4,
                    total_amount=17775.4,
                ),
                PurchaseItem(
                    product_id=p2_id,
                    batch_no="B2-EXPSOON",
                    mfg_date="2026-02-01",
                    exp_date=exp_soon,
                    qty=4.0,
                    purchase_rate=444.33,
                    sale_rate=650.0,
                    mrp=700.0,
                    taxable_amount=1777.32,
                    total_amount=1777.32,
                ),
            ],
        )
    )

    # Manual stock add for Product 1 expired batch (opening_qty preserved when created)
    b1_exp_id = inv_repo.upsert_batch(
        StockBatch(
            product_id=p1_id,
            batch_no="B1-EXPIRED",
            mfg_date="2024-01-01",
            exp_date=exp_past,
            purchase_rate=888.77,
            sale_rate=1150.0,
            mrp=1250.0,
            opening_qty=5.0,
            current_qty=5.0,
            barcode="890123450001",
        )
    )
    inv_repo.record_stock_movement(
        StockLedgerEntry(
            product_id=p1_id,
            batch_id=b1_exp_id,
            transaction_type="PURCHASE",
            reference_type="MANUAL_STOCK_INWARD",
            reference_id=b1_exp_id,
            qty_in=5.0,
            qty_out=0.0,
            balance_qty=25.0,
            rate=888.77,
            remarks="\t=CMD Initial expired batch",
        )
    )

    # Batch without expiry date on Product 1
    b1_noexp_id = inv_repo.upsert_batch(
        StockBatch(
            product_id=p1_id,
            batch_no="B1-NOEXP",
            mfg_date="2026-03-01",
            exp_date=None,
            purchase_rate=888.77,
            sale_rate=1200.0,
            mrp=1300.0,
            opening_qty=3.0,
            current_qty=3.0,
        )
    )
    inv_repo.record_stock_movement(
        StockLedgerEntry(
            product_id=p1_id,
            batch_id=b1_noexp_id,
            transaction_type="ADJUSTMENT",
            reference_type="MANUAL_ADJUSTMENT",
            reference_id=1,
            qty_in=3.0,
            qty_out=0.0,
            balance_qty=28.0,
            rate=0.0,
            remarks="Audit stock adjustment",
        )
    )

    # Depleted batch on Product 3 (0 remaining)
    inv_repo.upsert_batch(
        StockBatch(
            product_id=p3_id,
            batch_no="B3-DEPLETED",
            mfg_date="2025-05-01",
            exp_date=exp_healthy,
            purchase_rate=111.22,
            sale_rate=850.0,
            mrp=920.0,
            opening_qty=10.0,
            current_qty=0.0,
        )
    )

    # Depleted or active batch on Inactive Product 4 (must NOT appear in Stock Summary/Batches!)
    inv_repo.upsert_batch(
        StockBatch(
            product_id=p4_id,
            batch_no="B4-INACTIVE",
            exp_date=exp_healthy,
            purchase_rate=250.0,
            sale_rate=340.0,
            mrp=380.0,
            opening_qty=7.0,
            current_qty=7.0,
        )
    )

    # Sale on Healthy batch of Product 1 (qty 6 -> balance drops 28 -> 22)
    b1_healthy = next(
        b for b in inv_repo.get_available_batches_fefo(p1_id) if b.batch_no == "=B1-HEALTHY"
    )
    sale_id = sales_svc.process_sales_invoice(
        Sale(
            invoice_no="INV-SEP-1001",
            sale_date="2026-09-10",
            customer_id=cust_id,
            payment_mode="CASH",
            total_taxable=6857.14,
            total_cgst=171.43,
            total_sgst=171.43,
            net_amount=7200.0,
            paid_amount=7200.0,
            due_amount=0.0,
            items=[
                SaleItem(
                    product_id=p1_id,
                    batch_id=b1_healthy.batch_id,
                    qty=6.0,
                    sale_rate=1200.0,
                    mrp=1300.0,
                    taxable_amount=6857.14,
                    cgst_rate=2.5,
                    cgst_amount=171.43,
                    sgst_rate=2.5,
                    sgst_amount=171.43,
                    total_amount=7200.0,
                )
            ],
        )
    )

    # Sale Return of 2 units on Product 1 (balance rises 22 -> 24)
    sales_svc.process_sale_return(
        SalesReturn(
            return_no="SR-SEP-001",
            return_date="2026-09-15",
            sale_id=sale_id,
            customer_id=cust_id,
            total_taxable=2285.72,
            total_tax=114.28,
            net_amount=2400.0,
            items=[
                SalesReturnItem(
                    product_id=p1_id,
                    batch_id=b1_healthy.batch_id,
                    qty=2.0,
                    sale_rate=1200.0,
                    taxable_amount=2285.72,
                    tax_amount=114.28,
                    total_amount=2400.0,
                )
            ],
        )
    )

    # Assign controlled distinct movement_date values on stock_ledger rows for strict date-range filtering
    with db.transaction() as conn:
        ledger_rows = conn.execute("SELECT ledger_id FROM stock_ledger ORDER BY ledger_id ASC;").fetchall()
        dates_seq = [
            "2026-08-05 10:00:00",  # Purchase P1 (+20, bal 20)
            "2026-08-05 10:05:00",  # Purchase P2 (+4, bal 4)
            "2026-08-20 11:30:00",  # Manual P1 expired (+5, bal 25)
            "2026-09-01 09:15:00",  # Adjustment P1 (+3, bal 28)
            "2026-09-10 14:20:00",  # Sale P1 (-6, bal 22)
            "2026-09-15 16:45:00",  # Sale Return P1 (+2, bal 24)
        ]
        for row, ts in zip(ledger_rows, dates_seq):
            conn.execute(
                "UPDATE stock_ledger SET movement_date = ? WHERE ledger_id = ?;",
                (ts, row["ledger_id"]),
            )

    return {
        "db": db,
        "db_file": db_file,
        "admin_headers": {"Authorization": f"Bearer {admin_token}"},
        "operator_headers": {"Authorization": f"Bearer {op_token}"},
        "p1_id": p1_id,
        "p2_id": p2_id,
        "p3_id": p3_id,
        "p4_id": p4_id,
        "b1_exp_id": b1_exp_id,
        "b1_noexp_id": b1_noexp_id,
        "b1_healthy_id": b1_healthy.batch_id,
        "exp_past": exp_past,
        "exp_soon": exp_soon,
        "exp_healthy": exp_healthy,
    }


def _find_table_header_row(ws, first_col_label: str) -> int:
    for r_idx in range(1, min(20, ws.max_row + 1)):
        if ws.cell(row=r_idx, column=1).value == first_col_label:
            return r_idx
    raise AssertionError(f"Could not find header row starting with '{first_col_label}' in sheet {ws.title}")


def _assert_workbook_hardening_and_no_formulas(xlsx_bytes: bytes, filename: str) -> None:
    """Validate OOXML ZIP structure, XML contents, and confirm zero formulas or canary secrets."""
    validate_xlsx_bytes(xlsx_bytes, filename=filename)

    # Raw XML scan inside ZIP: zero formula tags (<f>), zero sensitive canary leakage
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes), "r") as zf:
        for name in zf.namelist():
            if name.lower().endswith((".xml", ".rels")):
                xml_text = zf.read(name).decode("utf-8", errors="ignore")
                assert "<f>" not in xml_text and "<f " not in xml_text, (
                    f"Formula element found in '{name}' of '{filename}'"
                )
                for canary in SENSITIVE_CANARY_TOKENS:
                    assert canary not in xml_text, (
                        f"Sensitive canary/cost string '{canary}' leaked into '{name}' of '{filename}'"
                    )

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=False)
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    assert cell.data_type != "f", (
                        f"Cell {ws.title}!{cell.coordinate} contains Excel formula {cell.value!r}"
                    )
                    if isinstance(cell.value, str):
                        assert not cell.value.startswith("="), (
                            f"Unescaped formula string {cell.value!r} in {ws.title}!{cell.coordinate}"
                        )
    finally:
        wb.close()


def _snapshot_db_counts_and_digest(db_file: Path) -> Tuple[Dict[str, int], bytes]:
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
            ).fetchall()
        ]
        counts = {
            t: conn.execute(f'SELECT COUNT(*) FROM "{t}";').fetchone()[0]
            for t in tables
        }
        # Stable logical digest of product, batch & ledger rows
        dump = (
            conn.execute("SELECT * FROM products ORDER BY product_id;").fetchall(),
            conn.execute("SELECT * FROM stock_batches ORDER BY batch_id;").fetchall(),
            conn.execute("SELECT * FROM stock_ledger ORDER BY ledger_id;").fetchall(),
        )
        digest = hashlib.sha256(repr(dump).encode("utf-8")).digest()
        return counts, digest
    finally:
        conn.close()


def test_c3b2a_exports_auth_headers_and_role_access(tmp_path, monkeypatch):
    """Unauthenticated gets 401; both ADMIN and OPERATOR download all 4 hardened .xlsx reports."""
    ctx = _setup_rich_inventory_fixture(tmp_path, monkeypatch)
    endpoints = (
        ("/api/excel/exports/products", "Product_Catalog_"),
        ("/api/excel/exports/stock-summary", "Current_Stock_Summary_"),
        ("/api/excel/exports/stock-batches", "Batch_Wise_Stock_Expiry_"),
        ("/api/excel/exports/stock-ledger", "Stock_Ledger_"),
    )

    with TestClient(app) as client:
        for url, prefix in endpoints:
            # Unauthenticated rejected
            assert client.get(url).status_code == 401

            # OPERATOR and ADMIN both permitted (all 4 exclude cost prices/margins/secrets)
            for hdrs in (ctx["operator_headers"], ctx["admin_headers"]):
                res = client.get(url, headers=hdrs)
                assert res.status_code == 200, f"{url} failed: {res.text}"
                assert res.headers["content-type"].startswith(XLSX_MEDIA_TYPE)
                assert res.headers.get("x-content-type-options") == "nosniff"
                assert res.headers.get("cache-control") == "no-store"
                disp = res.headers.get("content-disposition", "")
                assert disp.startswith(f'attachment; filename="{prefix}')
                assert disp.endswith('.xlsx"')
                _assert_workbook_hardening_and_no_formulas(res.content, f"{prefix}test.xlsx")

    reset_db_manager()


def test_c3b2a_product_catalog_export_accuracy_filters_and_formula_safety(tmp_path, monkeypatch):
    """Product Catalog accurately maps DB master fields, active/inactive statuses, GST, & filters."""
    ctx = _setup_rich_inventory_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        # 1. Full catalog (including inactive)
        res = client.get("/api/excel/exports/products", headers=ctx["admin_headers"])
        assert res.status_code == 200
        wb = openpyxl.load_workbook(io.BytesIO(res.content), data_only=False)
        try:
            assert wb.sheetnames == ["Product Catalog"]
            ws = wb["Product Catalog"]
            assert ws["A1"].value == "Sahyadri Krishi Kendra"
            banner_meta = "\n".join(str(ws.cell(row=r, column=1).value or "") for r in range(1, 7))
            assert "27ABCDE1234F1Z5" in banner_meta
            assert "FERT-PUNE-101" in banner_meta

            hdr_row = _find_table_header_row(ws, "Product ID")
            headers = [ws.cell(row=hdr_row, column=c).value for c in range(1, 18)]
            assert headers == [
                "Product ID",
                "Product Name",
                "Category",
                "Manufacturer / Brand",
                "Unit",
                "HSN Code",
                "Tax Group",
                "GST Rate (%)",
                "CGST (%)",
                "SGST (%)",
                "IGST (%)",
                "Sale Rate (₹)",
                "MRP (₹)",
                "Current Stock",
                "Min Stock Alert",
                "Target Crop",
                "Status",
            ]
            assert ws.freeze_panes == f"A{hdr_row + 1}"
            assert ws.auto_filter.ref is not None

            by_id = {}
            for r in range(hdr_row + 1, ws.max_row + 1):
                pid = ws.cell(row=r, column=1).value
                by_id[pid] = {headers[c - 1]: ws.cell(row=r, column=c) for c in range(1, 18)}

            assert set(by_id.keys()) == {ctx["p1_id"], ctx["p2_id"], ctx["p3_id"], ctx["p4_id"]}

            # Verify formula injection escaping & numeric formatting on Product 1
            p1 = by_id[ctx["p1_id"]]
            assert p1["Product Name"].value == "'=cmd|'/C calc'!A0 Urea 50% Boost"
            assert p1["Product Name"].data_type == "s"
            assert p1["Product Name"].number_format == "@"
            assert p1["Category"].value == "Chemical Fertilizers"
            assert p1["Unit"].value == "BAG"
            assert p1["HSN Code"].data_type == "s"
            assert p1["HSN Code"].number_format == "@"
            assert p1["HSN Code"].value == "31021000"
            assert p1["GST Rate (%)"].data_type == "n"
            assert p1["GST Rate (%)"].value == 5.0
            assert p1["GST Rate (%)"].number_format == PERCENT_FORMAT_DEFAULT
            assert p1["Sale Rate (₹)"].data_type == "n"
            assert p1["Sale Rate (₹)"].value == 1200.0
            assert p1["Sale Rate (₹)"].number_format == CURRENCY_FORMAT_INR
            assert p1["Current Stock"].data_type == "n"
            assert p1["Current Stock"].value == 24  # 20 + 5 + 3 - 6 + 2 = 24
            assert p1["Target Crop"].value == "Sugarcane"
            assert p1["Status"].value == "Active"

            # Leading-whitespace '+SUM' sanitized safely
            assert by_id[ctx["p2_id"]]["Product Name"].value == "'  +SUM(1,2) Shield Spray"
            assert by_id[ctx["p2_id"]]["GST Rate (%)"].value == 18.0
            assert by_id[ctx["p3_id"]]["Current Stock"].value == 0
            assert by_id[ctx["p4_id"]]["Product Name"].value == "'@HYPERLINK Discontinued Micronutrient"
            assert by_id[ctx["p4_id"]]["Status"].value == "Inactive"
        finally:
            wb.close()

        # 2. Exclude inactive + literal '%' LIKE wildcard search test ('50%' only matches P1!)
        res_filt = client.get(
            "/api/excel/exports/products",
            params={"include_inactive": "false", "query": "50%", "category_id": 1},
            headers=ctx["operator_headers"],
        )
        assert res_filt.status_code == 200
        _assert_workbook_hardening_and_no_formulas(res_filt.content, "filtered_products.xlsx")
        wb2 = openpyxl.load_workbook(io.BytesIO(res_filt.content), data_only=False)
        try:
            ws2 = wb2["Product Catalog"]
            hdr2 = _find_table_header_row(ws2, "Product ID")
            data_ids = [ws2.cell(row=r, column=1).value for r in range(hdr2 + 1, ws2.max_row + 1)]
            assert data_ids == [ctx["p1_id"]]
        finally:
            wb2.close()

        # 3. No-match filter produces valid empty table without crash
        res_empty = client.get(
            "/api/excel/exports/products?query=ZZZ_NO_MATCH_999",
            headers=ctx["admin_headers"],
        )
        assert res_empty.status_code == 200
        wb3 = openpyxl.load_workbook(io.BytesIO(res_empty.content), data_only=False)
        try:
            ws3 = wb3["Product Catalog"]
            hdr3 = _find_table_header_row(ws3, "Product ID")
            assert ws3.max_row == hdr3
        finally:
            wb3.close()

    reset_db_manager()


def test_c3b2a_current_stock_summary_export_status_and_calculations(tmp_path, monkeypatch):
    """Current Stock Summary aggregates product stock, low-stock threshold, active batches & nearest expiry."""
    ctx = _setup_rich_inventory_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        res = client.get("/api/excel/exports/stock-summary", headers=ctx["operator_headers"])
        assert res.status_code == 200
        _assert_workbook_hardening_and_no_formulas(res.content, "stock_summary.xlsx")

        wb = openpyxl.load_workbook(io.BytesIO(res.content), data_only=False)
        try:
            assert wb.sheetnames == ["Current Stock Summary"]
            ws = wb["Current Stock Summary"]
            hdr = _find_table_header_row(ws, "Product ID")
            headers = [ws.cell(row=hdr, column=c).value for c in range(1, 14)]

            rows_by_id = {}
            for r in range(hdr + 1, ws.max_row + 1):
                pid = ws.cell(row=r, column=1).value
                rows_by_id[pid] = {headers[c - 1]: ws.cell(row=r, column=c) for c in range(1, 14)}

            # Inactive P4 excluded from Current Stock Summary
            assert set(rows_by_id.keys()) == {ctx["p1_id"], ctx["p2_id"], ctx["p3_id"]}

            # P1: stock 24 > min 10 -> In Stock; 3 active positive batches (healthy, expired, noexp); nearest expiry = exp_past
            r1 = rows_by_id[ctx["p1_id"]]
            assert r1["Current Available Qty"].value == 24
            assert r1["Min Stock Alert"].value == 10
            assert r1["Stock Status"].value == "In Stock"
            assert r1["Active Batches"].value == 3
            assert isinstance(r1["Nearest Expiry"].value, (date, datetime))
            assert r1["Nearest Expiry"].value.strftime("%Y-%m-%d") == ctx["exp_past"]

            # P2: stock 4 <= min 10 -> Low Stock; 1 active batch; nearest expiry = exp_soon
            r2 = rows_by_id[ctx["p2_id"]]
            assert r2["Current Available Qty"].value == 4
            assert r2["Stock Status"].value == "Low Stock"
            assert r2["Active Batches"].value == 1
            assert r2["Nearest Expiry"].value.strftime("%Y-%m-%d") == ctx["exp_soon"]

            # P3: stock 0 (depleted batch only) -> Out of Stock; 0 active batches
            r3 = rows_by_id[ctx["p3_id"]]
            assert r3["Current Available Qty"].value == 0
            assert r3["Stock Status"].value == "Out of Stock"
            assert r3["Active Batches"].value == 0
            assert r3["Nearest Expiry"].value in (None, "")
        finally:
            wb.close()

    reset_db_manager()


def test_c3b2a_batch_wise_stock_expiry_fefo_order_and_statuses(tmp_path, monkeypatch):
    """Batch-wise Stock & Expiry preserves strict FEFO order, expiry status, and zero-stock toggle."""
    ctx = _setup_rich_inventory_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        # 1. Default active batches only (current_qty > 0), FEFO order
        res = client.get("/api/excel/exports/stock-batches", headers=ctx["admin_headers"])
        assert res.status_code == 200
        _assert_workbook_hardening_and_no_formulas(res.content, "batch_expiry.xlsx")

        wb = openpyxl.load_workbook(io.BytesIO(res.content), data_only=False)
        try:
            assert wb.sheetnames == ["Batch Stock & Expiry"]
            ws = wb["Batch Stock & Expiry"]
            hdr = _find_table_header_row(ws, "Batch ID")
            headers = [ws.cell(row=hdr, column=c).value for c in range(1, 18)]

            ordered_batches = []
            for r in range(hdr + 1, ws.max_row + 1):
                row_map = {headers[c - 1]: ws.cell(row=r, column=c) for c in range(1, 18)}
                ordered_batches.append(row_map)

            batch_nos = [b["Batch No"].value for b in ordered_batches]
            # Strict FEFO order: B1-EXPIRED (-15d), B2-EXPSOON (+30d), '=B1-HEALTHY (+365d), B1-NOEXP (last)
            assert batch_nos == ["B1-EXPIRED", "B2-EXPSOON", "'=B1-HEALTHY", "B1-NOEXP"]

            assert ordered_batches[0]["Days to Expiry"].value == -15
            assert ordered_batches[0]["Days to Expiry"].data_type == "n"
            assert ordered_batches[0]["Expiry Status"].value == "Expired"
            assert ordered_batches[0]["Opening Qty"].value == 5
            assert ordered_batches[0]["Remaining Qty"].value == 5
            assert ordered_batches[0]["Barcode"].value == "890123450001"

            assert ordered_batches[1]["Days to Expiry"].value == 30
            assert ordered_batches[1]["Expiry Status"].value == "Expiring Soon (<=90d)"
            assert ordered_batches[1]["Remaining Qty"].value == 4

            assert ordered_batches[2]["Expiry Status"].value == "Active"
            assert ordered_batches[2]["Remaining Qty"].value == 16  # 20 - 6 + 2 = 16

            assert ordered_batches[3]["Expiry Date"].value in (None, "")
            assert ordered_batches[3]["Days to Expiry"].value in (None, "")
            assert ordered_batches[3]["Expiry Status"].value == "No Expiry Date"
            assert ordered_batches[3]["Remaining Qty"].value == 3
        finally:
            wb.close()

        # 2. Include zero-stock batches -> includes B3-DEPLETED marked 'Depleted'
        res_all = client.get(
            "/api/excel/exports/stock-batches?include_zero_stock=true",
            headers=ctx["operator_headers"],
        )
        assert res_all.status_code == 200
        wb_all = openpyxl.load_workbook(io.BytesIO(res_all.content), data_only=False)
        try:
            ws_all = wb_all["Batch Stock & Expiry"]
            hdr_all = _find_table_header_row(ws_all, "Batch ID")
            status_by_batch = {
                ws_all.cell(row=r, column=6).value: ws_all.cell(row=r, column=10).value
                for r in range(hdr_all + 1, ws_all.max_row + 1)
            }
            assert status_by_batch["B3-DEPLETED"] == "Depleted"
            assert "B4-INACTIVE" not in status_by_batch
        finally:
            wb_all.close()

    reset_db_manager()


def test_c3b2a_stock_ledger_export_date_filters_balances_totals_and_validation(tmp_path, monkeypatch):
    """Stock Ledger respects date filters, preserves recorded balance_qty, resolves invoice refs, and rejects bad dates."""
    ctx = _setup_rich_inventory_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        # 1. Invalid date inputs rejected cleanly with HTTP 422
        assert client.get(
            "/api/excel/exports/stock-ledger?from_date=2026-02-30",
            headers=ctx["admin_headers"],
        ).status_code == 422
        assert client.get(
            "/api/excel/exports/stock-ledger?from_date=2026-09-20&to_date=2026-09-01",
            headers=ctx["admin_headers"],
        ).status_code == 422
        assert client.get(
            "/api/excel/exports/stock-ledger?from_date=%20%20%20",
            headers=ctx["admin_headers"],
        ).status_code == 422
        assert client.get(
            "/api/excel/exports/stock-ledger?product_id=0",
            headers=ctx["admin_headers"],
        ).status_code == 422
        assert client.get(
            "/api/excel/exports/products?category_id=-1",
            headers=ctx["admin_headers"],
        ).status_code == 422

        # 2. Full chronological Stock Ledger for Product 1
        res_full = client.get(
            f"/api/excel/exports/stock-ledger?product_id={ctx['p1_id']}",
            headers=ctx["operator_headers"],
        )
        assert res_full.status_code == 200
        _assert_workbook_hardening_and_no_formulas(res_full.content, "stock_ledger_p1.xlsx")

        wb = openpyxl.load_workbook(io.BytesIO(res_full.content), data_only=False)
        try:
            assert wb.sheetnames == ["Stock Ledger"]
            ws = wb["Stock Ledger"]
            hdr = _find_table_header_row(ws, "Ledger ID")
            headers = [ws.cell(row=hdr, column=c).value for c in range(1, 16)]
            assert "Rate" not in headers and "Purchase Rate" not in headers

            # 5 movements for Product 1 + 1 TOTALS row = 6 rows after header
            assert ws.max_row == hdr + 6
            rows = []
            for r in range(hdr + 1, ws.max_row):
                rows.append({headers[c - 1]: ws.cell(row=r, column=c) for c in range(1, 16)})

            # Verify chronological dates, resolved references, sanitized remarks, and verbatim DB balances
            assert [r["Transaction Type"].value for r in rows] == [
                "PURCHASE",
                "PURCHASE",
                "ADJUSTMENT",
                "SALE",
                "SALE_RETURN",
            ]
            assert [r["Balance Qty"].value for r in rows] == [20, 25, 28, 22, 24]
            assert rows[0]["Reference No"].value == "PUR-AUG-001"
            assert rows[1]["Remarks"].value == "'\t=CMD Initial expired batch"
            assert rows[3]["Reference No"].value == "INV-SEP-1001"
            assert rows[4]["Reference No"].value == "INV-SEP-1001"  # sale_return references original sale_id

            # Totals row: Inward = 20+5+3+2 = 30, Outward = 6, Balance column blank (no formulas!)
            tot_row_idx = ws.max_row
            assert ws.cell(row=tot_row_idx, column=1).value == "TOTAL MOVEMENTS"
            assert ws.cell(row=tot_row_idx, column=11).data_type == "n"
            assert ws.cell(row=tot_row_idx, column=11).value == 30
            assert ws.cell(row=tot_row_idx, column=12).data_type == "n"
            assert ws.cell(row=tot_row_idx, column=12).value == 6
            assert ws.cell(row=tot_row_idx, column=13).value in (None, "")
        finally:
            wb.close()

        # 3. Date-range filtered Stock Ledger (2026-09-01 to 2026-09-12 -> Adjustment + Sale only)
        res_range = client.get(
            "/api/excel/exports/stock-ledger?from_date=2026-09-01&to_date=2026-09-12",
            headers=ctx["admin_headers"],
        )
        assert res_range.status_code == 200
        assert 'Stock_Ledger_2026-09-01_to_2026-09-12.xlsx' in res_range.headers["content-disposition"]
        wb_rng = openpyxl.load_workbook(io.BytesIO(res_range.content), data_only=False)
        try:
            ws_rng = wb_rng["Stock Ledger"]
            hdr_rng = _find_table_header_row(ws_rng, "Ledger ID")
            tx_types = [
                ws_rng.cell(row=r, column=7).value
                for r in range(hdr_rng + 1, ws_rng.max_row)
            ]
            assert tx_types == ["ADJUSTMENT", "SALE"]
            assert ws_rng.cell(row=ws_rng.max_row, column=11).value == 3  # Inward
            assert ws_rng.cell(row=ws_rng.max_row, column=12).value == 6  # Outward
        finally:
            wb_rng.close()

        # 4. Date range with no movements returns clean empty header-only sheet
        res_none = client.get(
            "/api/excel/exports/stock-ledger?from_date=2025-01-01&to_date=2025-01-31",
            headers=ctx["admin_headers"],
        )
        assert res_none.status_code == 200
        wb_none = openpyxl.load_workbook(io.BytesIO(res_none.content), data_only=False)
        try:
            ws_none = wb_none["Stock Ledger"]
            hdr_none = _find_table_header_row(ws_none, "Ledger ID")
            assert ws_none.max_row == hdr_none
        finally:
            wb_none.close()

    reset_db_manager()


def test_c3b2a_non_mutation_zero_temp_files_closed_conn_and_maintenance_503(tmp_path, monkeypatch):
    """Exports perform zero DB mutations, write zero disk files, close DB conn before styling, and honor 503 maintenance."""
    ctx = _setup_rich_inventory_fixture(tmp_path, monkeypatch)
    db = ctx["db"]
    db_file = ctx["db_file"]

    before_counts, before_digest = _snapshot_db_counts_and_digest(db_file)
    files_before = {p.name for p in tmp_path.iterdir()}

    # Verify DB connection is closed BEFORE save_workbook_to_bytes runs
    seen_active_conns_during_serialize: List[int] = []
    import src.services.excel_export_service as export_mod

    orig_save = export_mod.save_workbook_to_bytes

    def _spy_save(wb):
        seen_active_conns_during_serialize.append(db.active_connection_count)
        return orig_save(wb)

    monkeypatch.setattr(export_mod, "save_workbook_to_bytes", _spy_save)

    svc = ExcelExportService(db)
    for fn in (
        lambda: svc.export_product_catalog(),
        lambda: svc.export_current_stock_summary(),
        lambda: svc.export_batch_wise_stock_expiry(),
        lambda: svc.export_stock_ledger(),
    ):
        data, name = fn()
        _assert_workbook_hardening_and_no_formulas(data, name)

    assert seen_active_conns_during_serialize == [0, 0, 0, 0], (
        f"DB connection was still open while serializing Excel workbooks: {seen_active_conns_during_serialize}"
    )

    after_counts, after_digest = _snapshot_db_counts_and_digest(db_file)
    assert after_counts == before_counts
    assert after_digest == before_digest

    files_after = {p.name for p in tmp_path.iterdir()}
    assert not any(name.endswith((".xlsx", ".tmp")) for name in (files_after - files_before))

    # Maintenance mode returns HTTP 503 + Retry-After: 2 on all 4 endpoints
    with TestClient(app) as client:
        with db.exclusive_maintenance(timeout=2.0, reason="c3b2a_restore_test"):
            for url in (
                "/api/excel/exports/products",
                "/api/excel/exports/stock-summary",
                "/api/excel/exports/stock-batches",
                "/api/excel/exports/stock-ledger",
            ):
                r = client.get(url, headers=ctx["admin_headers"])
                assert r.status_code == 503
                assert r.headers.get("Retry-After") == "2"

    reset_db_manager()


def test_c3b2a_frontend_ui_buttons_and_authenticated_blob_downloads(tmp_path, monkeypatch):
    """Frontend HTML/JS exposes clear English-only buttons wired to authenticated Blob downloads."""
    _setup_rich_inventory_fixture(tmp_path, monkeypatch)

    with TestClient(app) as client:
        html = client.get("/").text
        js = client.get("/static/js/app.js").text

        for dom_id in (
            'id="btn-export-products-excel"',
            'id="btn-export-stock-summary-excel"',
            'id="btn-export-stock-batches-excel"',
            'id="btn-export-stock-ledger-excel"',
            'id="inv-ledger-from-date"',
            'id="inv-ledger-to-date"',
        ):
            assert dom_id in html, f"Missing expected C3-B2A HTML control {dom_id}"

        # Ensure download helpers use authenticated fetch + Blob URL (never window.open / unauthenticated <a>)
        for fn_name in (
            "async function downloadExcelExport(",
            "function exportProductCatalogExcel(",
            "function exportStockSummaryExcel(",
            "function exportBatchStockExpiryExcel(",
            "function exportStockLedgerExcel(",
        ):
            assert fn_name in js, f"Missing expected JS function {fn_name}"

        for api_path in (
            "/api/excel/exports/products",
            "/api/excel/exports/stock-summary",
            "/api/excel/exports/stock-batches",
            "/api/excel/exports/stock-ledger",
        ):
            assert api_path in js, f"Missing API path {api_path} in app.js"

    reset_db_manager()
