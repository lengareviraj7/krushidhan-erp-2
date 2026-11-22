"""
Verification Suite for Stage C: Billing, Shop Customization, and Offline Operations.

Sub-stage C1: Shop Profile Customization
- Dynamic SQLite persistence for shop_name, owner_name, address, mobile, email,
  GSTIN, and fertilizer/pesticide/seed licences.
- Non-destructive partial updates via Repository, POST /api/system/settings, and
  PATCH /api/system/settings (saving one field never overwrites unrelated settings).
- Multi-shop isolation across separate shop databases.
- First-run onboarding persistence of owner_name and shop metadata.
- RBAC enforcement on settings read/write endpoints.

Sub-stage C2-A: Remove UPI & Online Payment Features
- Remove Scan QR button, Counter UPI QR modal, QR JS helpers, external qrserver URLs,
  and Shop Settings / onboarding UPI ID inputs from frontend UI.
- Replace UPI options in manual forms with BANK_TRANSFER alongside CASH, CHEQUE, CARD
  (and CREDIT on POS billing).
- Preserve cash/credit billing, manual receipts/payments, double-entry accounting,
  company_settings.upi_id DB compatibility, and historical UPI record readability.
"""
from __future__ import annotations

from datetime import date
from html.parser import HTMLParser
from typing import Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.connection import DatabaseManager, get_db_manager, reset_db_manager
from src.models.system import CompanySettings, CompanySettingsUpdate
from src.repositories.system_repository import SystemRepository
from src.services.auth_service import AuthService


def _setup_isolated_clean_shop(
    monkeypatch,
    shop_dir,
    *,
    username: str,
    password: str,
    full_name: str,
    shop_name: str,
    mobile: str,
    address: str,
    city: str,
    gstin: str,
    upi_id: str,
    email: str,
):
    """Initialize a clean isolated desktop shop DB and onboard its owner via API."""
    reset_db_manager()
    local_app = shop_dir / "LocalAppData"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))

    with TestClient(app) as client:
        setup_res = client.post(
            "/api/auth/setup-admin",
            json={
                "username": username,
                "password": password,
                "full_name": full_name,
                "shop_name": shop_name,
                "mobile": mobile,
                "address": address,
                "city": city,
                "gstin": gstin,
                "upi_id": upi_id,
                "email": email,
            },
        )
        assert setup_res.status_code == 200, setup_res.text
        return setup_res.json(), local_app


def test_c1_shop_profile_fields_roundtrip_and_repository_partial_merge(tmp_path):
    """Verify full field persistence and non-overwriting partial merges in SystemRepository."""
    db_file = tmp_path / "c1_repo.db"
    bkp_dir = tmp_path / "c1_repo_bkps"
    db = DatabaseManager(db_path=db_file, app_mode="local", backup_dir=bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)
    repo = SystemRepository(db)

    # 1. Save full profile including bank details & invoice preferences
    full_profile = CompanySettings(
        company_name="Mauli Krishi Seva Kendra",
        owner_name="Rajendra Deshmukh",
        address="Plot 12, Market Yard Road",
        city="Baramati",
        state="Maharashtra",
        pincode="413102",
        mobile="9822112233",
        email="maulikendra@example.com",
        gstin="27abcde1234f1z5",
        dl_fertilizer="FERT-PUNE-2026-01",
        dl_pesticide="PEST-PUNE-2026-02",
        dl_seed="SEED-PUNE-2026-03",
        upi_id="mauliagro@okaxis",
        bank_name="Bank of Maharashtra",
        account_no="60012345678",
        ifsc_code="mahb0000123",
        invoice_terms="Goods once sold will not be taken back.",
        is_thermal_print=0,
        default_invoice_size="A4",
    )
    saved = repo.update_company_settings(full_profile)
    assert saved.company_name == "Mauli Krishi Seva Kendra"
    assert saved.owner_name == "Rajendra Deshmukh"
    assert saved.gstin == "27ABCDE1234F1Z5"
    assert saved.ifsc_code == "MAHB0000123"
    assert saved.upi_id == "mauliagro@okaxis"
    assert saved.dl_fertilizer == "FERT-PUNE-2026-01"
    assert saved.dl_pesticide == "PEST-PUNE-2026-02"
    assert saved.dl_seed == "SEED-PUNE-2026-03"

    # 2. Update ONLY UPI ID using CompanySettingsUpdate -> preserves everything else
    after_upi = repo.update_company_settings(CompanySettingsUpdate(upi_id="mauli.new@ybl"))
    assert after_upi.upi_id == "mauli.new@ybl"
    assert after_upi.company_name == "Mauli Krishi Seva Kendra"
    assert after_upi.owner_name == "Rajendra Deshmukh"
    assert after_upi.bank_name == "Bank of Maharashtra"
    assert after_upi.account_no == "60012345678"
    assert after_upi.dl_seed == "SEED-PUNE-2026-03"
    assert after_upi.email == "maulikendra@example.com"

    # 3. Update via fresh CompanySettings instance with only company_name & city set ->
    # omitted fields (upi_id, owner_name, bank details, licences, state) must NOT be wiped!
    after_partial_model = repo.update_company_settings(
        CompanySettings(company_name="Mauli Agri Mall", city="Indapur")
    )
    assert after_partial_model.company_name == "Mauli Agri Mall"
    assert after_partial_model.city == "Indapur"
    assert after_partial_model.owner_name == "Rajendra Deshmukh"
    assert after_partial_model.upi_id == "mauli.new@ybl"
    assert after_partial_model.dl_fertilizer == "FERT-PUNE-2026-01"
    assert after_partial_model.bank_name == "Bank of Maharashtra"

    # 4. Reload directly from fresh repository instance to verify SQLite persistence
    fresh_read = SystemRepository(db).get_company_settings()
    assert fresh_read.company_name == "Mauli Agri Mall"
    assert fresh_read.city == "Indapur"
    assert fresh_read.upi_id == "mauli.new@ybl"
    assert fresh_read.bank_name == "Bank of Maharashtra"


def test_c1_api_settings_partial_update_validation_and_rbac(tmp_path, monkeypatch):
    """Verify POST & PATCH /api/system/settings preserve untouched fields, validate inputs, and enforce RBAC."""
    reset_db_manager()
    db_file = tmp_path / "c1_api.db"
    bkp_dir = tmp_path / "c1_api_bkps"
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("BACKUP_DIR", str(bkp_dir))
    monkeypatch.setenv("CLEAN_INSTALL", "1")
    reset_db_manager()

    db = DatabaseManager(db_path=db_file, app_mode="local", backup_dir=bkp_dir, clean_install=True)
    db.initialize_database(include_seed=True)
    auth = AuthService(db)
    admin_user = auth.setup_initial_admin(
        username="shop_owner",
        plain_password="OwnerPass#2026",
        full_name="Santosh Patil",
        company_settings=CompanySettings(
            company_name="Kisan Krishi Bhandar",
            owner_name="Santosh Patil",
            address="Station Road",
            city="Satara",
            state="Maharashtra",
            pincode="415001",
            mobile="9811223344",
            email="kisan@sataraagro.in",
            gstin="27ZZZZZ1111Z1Z5",
            dl_fertilizer="F-STR-001",
            dl_pesticide="P-STR-002",
            dl_seed="S-STR-003",
            upi_id="kisanbhandar@sbi",
            bank_name="SBI Satara",
            account_no="3344556677",
            ifsc_code="SBIN0000555",
            default_invoice_size="A4",
        ),
    )
    op_id = auth.register_user("billing_op", "BillingOp#2026", "Counter Clerk", role="OPERATOR")
    assert op_id > 0

    admin_token = auth.create_access_token(
        admin_user.user_id or 1, admin_user.username, admin_user.full_name or "Owner", "ADMIN"
    )
    op_token = auth.create_access_token(op_id, "billing_op", "Counter Clerk", "OPERATOR")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    op_headers = {"Authorization": f"Bearer {op_token}"}

    with TestClient(app) as client:
        # Unauthenticated blocked
        assert client.get("/api/system/settings").status_code == 401
        assert client.post("/api/system/settings", json={"upi_id": "x@upi"}).status_code == 401

        # Operator can read shop profile (for billing & invoices), cannot mutate
        op_get = client.get("/api/system/settings", headers=op_headers)
        assert op_get.status_code == 200
        assert op_get.json()["owner_name"] == "Santosh Patil"
        assert op_get.json()["upi_id"] == "kisanbhandar@sbi"

        assert client.post("/api/system/settings", json={"upi_id": "hack@upi"}, headers=op_headers).status_code == 403
        assert client.patch("/api/system/settings", json={"upi_id": "hack@upi"}, headers=op_headers).status_code == 403

        # Admin UI save omitting bank_name / account_no / ifsc_code / default_invoice_size preserves them
        ui_save_payload = {
            "company_name": "Kisan Krishi Bhandar Pvt",
            "owner_name": "Santosh B. Patil",
            "address": "New Market Yard, Gate 2",
            "city": "Satara",
            "state": "Maharashtra",
            "pincode": "415002",
            "mobile": "9811223399",
            "email": "contact@sataraagro.in",
            "gstin": "27zzzzz1111z1z9",
            "dl_fertilizer": "F-STR-101",
            "dl_pesticide": "P-STR-102",
            "dl_seed": "S-STR-103",
            "upi_id": "kisan.satara@okicici",
            "invoice_terms": "Subject to Satara Jurisdiction.",
        }
        post_res = client.post("/api/system/settings", json=ui_save_payload, headers=admin_headers)
        assert post_res.status_code == 200, post_res.text
        post_data = post_res.json()
        assert post_data["success"] is True
        assert post_data["settings"]["bank_name"] == "SBI Satara"
        assert post_data["settings"]["account_no"] == "3344556677"
        assert post_data["settings"]["ifsc_code"] == "SBIN0000555"
        assert post_data["settings"]["gstin"] == "27ZZZZZ1111Z1Z9"
        assert post_data["settings"]["upi_id"] == "kisan.satara@okicici"

        # Single-field PATCH updates only mobile while preserving everything else
        patch_res = client.patch(
            "/api/system/settings",
            json={"mobile": "9900112233"},
            headers=admin_headers,
        )
        assert patch_res.status_code == 200, patch_res.text
        patched = patch_res.json()["settings"]
        assert patched["mobile"] == "9900112233"
        assert patched["upi_id"] == "kisan.satara@okicici"
        assert patched["owner_name"] == "Santosh B. Patil"
        assert patched["dl_seed"] == "S-STR-103"
        assert patched["bank_name"] == "SBI Satara"

        # Single-field POST with only email="" explicitly clears email without touching anything else
        clear_res = client.post("/api/system/settings", json={"email": ""}, headers=admin_headers)
        assert clear_res.status_code == 200
        assert clear_res.json()["settings"]["email"] is None
        assert clear_res.json()["settings"]["company_name"] == "Kisan Krishi Bhandar Pvt"
        assert clear_res.json()["settings"]["upi_id"] == "kisan.satara@okicici"

        # Invalid payloads fail with 400 and do not alter DB
        assert client.post("/api/system/settings", json={}, headers=admin_headers).status_code == 400
        assert client.post("/api/system/settings", json={"company_name": "   "}, headers=admin_headers).status_code == 400
        assert client.post("/api/system/settings", json={"upi_id": "invalid_upi"}, headers=admin_headers).status_code == 400
        assert client.post("/api/system/settings", json={"email": "not-an-email"}, headers=admin_headers).status_code == 400

        # Confirm DB values intact after failed requests
        final_get = client.get("/api/system/settings", headers=admin_headers)
        assert final_get.status_code == 200
        assert final_get.json()["upi_id"] == "kisan.satara@okicici"
        assert final_get.json()["company_name"] == "Kisan Krishi Bhandar Pvt"

    reset_db_manager()


def test_c1_multi_shop_isolation_and_onboarding_owner_persistence(tmp_path, monkeypatch):
    """Each independent shop installation stores and displays strictly its own settings from SQLite."""
    shop_a_resp, shop_a_app = _setup_isolated_clean_shop(
        monkeypatch,
        tmp_path / "shop_a",
        username="owner_a",
        password="ShopAPassword#1",
        full_name="Vikram Pawar",
        shop_name="Sahyadri Agro Center",
        mobile="9822000111",
        address="Main Road, Phaltan",
        city="Phaltan",
        gstin="27AAAAA1111A1Z1",
        upi_id="sahyadri.pawar@ybl",
        email="sahyadri@example.com",
    )
    assert shop_a_resp["shop"]["name"] == "Sahyadri Agro Center"
    assert shop_a_resp["shop"]["owner"] == "Vikram Pawar"

    with TestClient(app) as client_a:
        res_a = client_a.get(
            "/api/system/settings",
            headers={"Authorization": f"Bearer {shop_a_resp['token']}"},
        )
        assert res_a.status_code == 200
        data_a = res_a.json()
        assert data_a["company_name"] == "Sahyadri Agro Center"
        assert data_a["owner_name"] == "Vikram Pawar"
        assert data_a["upi_id"] == "sahyadri.pawar@ybl"
        assert data_a["email"] == "sahyadri@example.com"

        # Save licences specific to Shop A
        upd_a = client_a.post(
            "/api/system/settings",
            json={
                "dl_fertilizer": "A-FERT-111",
                "dl_pesticide": "A-PEST-222",
                "dl_seed": "A-SEED-333",
            },
            headers={"Authorization": f"Bearer {shop_a_resp['token']}"},
        )
        assert upd_a.status_code == 200

    # Onboard Shop B in a completely separate desktop data directory
    shop_b_resp, shop_b_app = _setup_isolated_clean_shop(
        monkeypatch,
        tmp_path / "shop_b",
        username="owner_b",
        password="ShopBPassword#2",
        full_name="Nitin Kulkarni",
        shop_name="Baliraja Krishi Kendra",
        mobile="9766000222",
        address="Bus Stand Chowk, Sangola",
        city="Sangola",
        gstin="27BBBBB2222B1Z2",
        upi_id="baliraja.shop@okhdfcbank",
        email="baliraja@example.com",
    )
    assert shop_b_resp["shop"]["name"] == "Baliraja Krishi Kendra"
    assert shop_b_resp["shop"]["owner"] == "Nitin Kulkarni"

    with TestClient(app) as client_b:
        res_b = client_b.get(
            "/api/system/settings",
            headers={"Authorization": f"Bearer {shop_b_resp['token']}"},
        )
        assert res_b.status_code == 200
        data_b = res_b.json()
        assert data_b["company_name"] == "Baliraja Krishi Kendra"
        assert data_b["owner_name"] == "Nitin Kulkarni"
        assert data_b["upi_id"] == "baliraja.shop@okhdfcbank"
        assert data_b["dl_fertilizer"] is None  # Shop A licences must never bleed into Shop B

    # Re-point back to Shop A and confirm Shop A's custom licences & profile still load cleanly
    reset_db_manager()
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(shop_a_app))
    with TestClient(app) as client_a_again:
        login_a = client_a_again.post(
            "/api/auth/login",
            json={"username": "owner_a", "password": "ShopAPassword#1"},
        )
        assert login_a.status_code == 200
        assert login_a.json()["shop"]["name"] == "Sahyadri Agro Center"
        assert login_a.json()["shop"]["owner"] == "Vikram Pawar"

        res_a2 = client_a_again.get(
            "/api/system/settings",
            headers={"Authorization": f"Bearer {login_a.json()['token']}"},
        )
        assert res_a2.status_code == 200
        assert res_a2.json()["dl_fertilizer"] == "A-FERT-111"
        assert res_a2.json()["upi_id"] == "sahyadri.pawar@ybl"

    reset_db_manager()


def test_c1_settings_ui_contains_all_shop_profile_inputs(tmp_path, monkeypatch):
    """Verify UI HTML template serves all required C1 Shop Profile inputs (without removed UPI ID)."""
    reset_db_manager()
    local_app = tmp_path / "ui_check"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))

    with TestClient(app) as client:
        html = client.get("/").text
        required_ids = (
            'id="set-shop-name"',
            'id="set-owner-name"',
            'id="set-gstin"',
            'id="set-address"',
            'id="set-city"',
            'id="set-state"',
            'id="set-pincode"',
            'id="set-mobile"',
            'id="set-email"',
            'id="set-lic-fert"',
            'id="set-lic-pest"',
            'id="set-lic-seed"',
            'id="set-terms"',
            'id="btn-save-shop-profile"',
        )
        for dom_id in required_ids:
            assert dom_id in html, f"Missing expected Shop Profile input {dom_id}"
        assert 'id="set-upi-id"' not in html

    reset_db_manager()


class _SelectOptionsParser(HTMLParser):
    """Lightweight HTML parser extracting <option value=...> items grouped by <select id=...>."""

    def __init__(self) -> None:
        super().__init__()
        self.selects: Dict[str, List[Tuple[str, str]]] = {}
        self._current_select_id: str | None = None
        self._current_option_val: str | None = None
        self._current_option_text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag == "select":
            self._current_select_id = attr_map.get("id")
            if self._current_select_id and self._current_select_id not in self.selects:
                self.selects[self._current_select_id] = []
        elif tag == "option" and self._current_select_id:
            self._current_option_val = attr_map.get("value") or ""
            self._current_option_text = []

    def handle_data(self, data: str) -> None:
        if self._current_option_val is not None:
            self._current_option_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._current_select_id and self._current_option_val is not None:
            label = "".join(self._current_option_text).strip()
            self.selects[self._current_select_id].append((self._current_option_val, label))
            self._current_option_val = None
            self._current_option_text = []
        elif tag == "select":
            self._current_select_id = None


def test_c2a_frontend_removes_upi_qr_and_online_payment_features(tmp_path, monkeypatch):
    """
    C2-A: Verify UPI QR modal, Scan QR button, UPI inputs, external QR requests,
    and UPI dropdown options are completely removed from frontend UI and JS.
    """
    reset_db_manager()
    local_app = tmp_path / "c2a_ui_check"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))

    with TestClient(app) as client:
        html_res = client.get("/")
        assert html_res.status_code == 200
        html = html_res.text

        js_res = client.get("/static/js/app.js")
        assert js_res.status_code == 200
        js = js_res.text

        # Forbidden UPI QR & online payment markers across HTML & JS
        forbidden_html = (
            "modal-counter-upi-qr",
            "openCounterUpiQrModal",
            "closeCounterUpiQrModal",
            "onPosPaymentModeChanged",
            "api.qrserver.com",
            "upi://pay",
            "set-upi-id",
            "Scan QR",
            "upi-qr-image",
            "upi-qr-amount",
            "9503673620@upi",
            "9503573620@upi",
            "Google Pay",
            "PhonePe",
            "Paytm",
            "BHIM UPI",
            "UPI / Online",
            "UPI / QR Code",
        )
        for marker in forbidden_html:
            assert marker not in html, f"Removed UPI/QR marker still found in index.html: {marker}"

        forbidden_js = (
            "modal-counter-upi-qr",
            "openCounterUpiQrModal",
            "closeCounterUpiQrModal",
            "onPosPaymentModeChanged",
            "api.qrserver.com",
            "upi://pay",
            "set-upi-id",
            "upi-qr-image",
            "upi-qr-amount",
            "9503573620@upi",
            "9503673620@upi",
        )
        for marker in forbidden_js:
            assert marker not in js, f"Removed UPI/QR marker still found in app.js: {marker}"

        # Settings payload save/load in JS must not reference set-upi-id or submit upi_id
        assert "upi_id:" not in js
        # Offline browser connectivity sync must remain intact
        assert 'window.addEventListener("online", syncPendingOfflineBills)' in js
        assert "function formatPaymentModeLabel(" in js

        # First-run setup form must not ask for UPI ID
        setup_start = html.index('id="erp-first-run-setup-form"')
        setup_end = html.index("</form>", setup_start)
        setup_form_html = html[setup_start:setup_end]
        assert "upi" not in setup_form_html.lower()

        # Verify payment mode dropdowns in new-entry forms exclude UPI and include BANK_TRANSFER
        parser = _SelectOptionsParser()
        parser.feed(html)

        expected_selects = {
            "pos-pay-mode": {"CASH", "CREDIT", "BANK_TRANSFER", "CHEQUE", "CARD"},
            "acc-receipt-mode": {"CASH", "BANK_TRANSFER", "CHEQUE", "CARD"},
            "acc-exp-mode": {"CASH", "BANK_TRANSFER", "CHEQUE", "CARD"},
            "fd-pay-mode": {"CASH", "BANK_TRANSFER", "CHEQUE", "CARD"},
            "sp-pay-mode": {"CASH", "BANK_TRANSFER", "CHEQUE", "CARD"},
        }
        for select_id, required_values in expected_selects.items():
            assert select_id in parser.selects, f"Expected payment mode <select id='{select_id}'>"
            options = parser.selects[select_id]
            values = {val for val, _ in options}
            labels_joined = " | ".join(label.upper() for _, label in options)
            assert "UPI" not in values, f"{select_id} must not offer UPI option"
            assert "UPI" not in labels_joined, f"{select_id} labels must not mention UPI"
            assert "ONLINE" not in labels_joined, f"{select_id} labels must not mention Online"
            assert required_values.issubset(values), (
                f"{select_id} missing expected payment modes: {required_values - values}"
            )

    reset_db_manager()


def test_c2a_cash_credit_and_manual_bank_payment_accounting_workflows(tmp_path, monkeypatch):
    """
    C2-A: Verify preserved cash billing, credit/udhar billing, manual BANK_TRANSFER / CHEQUE / CARD
    customer receipts, supplier payments, daily expenses, Cash Drawer reconciliation, and balanced vouchers.
    """
    shop_resp, _ = _setup_isolated_clean_shop(
        monkeypatch,
        tmp_path / "c2a_ops",
        username="owner_ops",
        password="OpsPassword#1",
        full_name="Mahesh Shinde",
        shop_name="Shinde Krishi Kendra",
        mobile="9822334455",
        address="Market Yard, Karad",
        city="Karad",
        gstin="27CCCCC3333C1Z3",
        upi_id="",
        email="shindekrishi@example.com",
    )
    headers = {"Authorization": f"Bearer {shop_resp['token']}"}
    today_str = date.today().isoformat()

    with TestClient(app) as client:
        # 1. Create farmer, product, batch stock, and supplier
        cust_res = client.post(
            "/api/masters/customers",
            json={
                "customer_name": "Prakash Jadhav",
                "mobile": "9811001100",
                "village": "Ogalewadi",
                "opening_balance": 0.0,
            },
            headers=headers,
        )
        assert cust_res.status_code == 200, cust_res.text
        cust_id = cust_res.json()["customer_id"]

        supp_res = client.post(
            "/api/masters/suppliers",
            json={
                "supplier_name": "Sahyadri Fertilizers Pvt Ltd",
                "contact_person": "Anil More",
                "mobile": "9700112233",
                "city": "Pune",
                "opening_balance": 5000.0,
            },
            headers=headers,
        )
        assert supp_res.status_code == 200, supp_res.text
        supp_id = supp_res.json()["supplier_id"]

        prod_res = client.post(
            "/api/masters/products",
            json={
                "product_name": "Test NPK 19:19:19",
                "category_id": 1,
                "manufacturer_id": 1,
                "hsn_code": "31051000",
                "unit_id": 1,
                "tax_group_id": 2,
                "default_purchase_rate": 400.0,
                "default_sale_rate": 500.0,
                "default_mrp": 550.0,
                "min_stock_alert": 5.0,
                "is_active": 1,
            },
            headers=headers,
        )
        assert prod_res.status_code == 200, prod_res.text
        prod_id = prod_res.json()["product_id"]

        stk_res = client.post(
            "/api/inventory/add-stock",
            json={
                "product_id": prod_id,
                "qty": 30.0,
                "batch_no": "NPK-C2A-01",
                "purchase_rate": 400.0,
                "sale_rate": 500.0,
                "mrp": 550.0,
                "mfg_date": "2026-01-01",
                "exp_date": "2028-01-01",
                "remarks": "Opening C2-A stock",
            },
            headers=headers,
        )
        assert stk_res.status_code == 200, stk_res.text
        batch_id = stk_res.json()["batch_id"]

        # 2. Cash sale (₹1000)
        cash_sale_res = client.post(
            "/api/sales/create",
            json={
                "invoice_no": "INV-C2A-CASH-1",
                "sale_date": today_str,
                "customer_id": cust_id,
                "payment_mode": "CASH",
                "total_taxable": 952.38,
                "total_cgst": 23.81,
                "total_sgst": 23.81,
                "net_amount": 1000.0,
                "paid_amount": 1000.0,
                "due_amount": 0.0,
                "items": [
                    {
                        "product_id": prod_id,
                        "batch_id": batch_id,
                        "qty": 2.0,
                        "sale_rate": 500.0,
                        "mrp": 550.0,
                        "taxable_amount": 952.38,
                        "cgst_rate": 2.5,
                        "cgst_amount": 23.81,
                        "sgst_rate": 2.5,
                        "sgst_amount": 23.81,
                        "total_amount": 1000.0,
                    }
                ],
            },
            headers=headers,
        )
        assert cash_sale_res.status_code == 200, cash_sale_res.text

        # 3. Credit (Udhar) sale (₹1500 unpaid)
        credit_sale_res = client.post(
            "/api/sales/create",
            json={
                "invoice_no": "INV-C2A-CR-2",
                "sale_date": today_str,
                "customer_id": cust_id,
                "payment_mode": "CREDIT",
                "total_taxable": 1428.57,
                "total_cgst": 35.72,
                "total_sgst": 35.71,
                "net_amount": 1500.0,
                "paid_amount": 0.0,
                "due_amount": 1500.0,
                "items": [
                    {
                        "product_id": prod_id,
                        "batch_id": batch_id,
                        "qty": 3.0,
                        "sale_rate": 500.0,
                        "mrp": 550.0,
                        "taxable_amount": 1428.57,
                        "cgst_rate": 2.5,
                        "cgst_amount": 35.72,
                        "sgst_rate": 2.5,
                        "sgst_amount": 35.71,
                        "total_amount": 1500.0,
                    }
                ],
            },
            headers=headers,
        )
        assert credit_sale_res.status_code == 200, credit_sale_res.text

        # 4. Bank Transfer sale (₹500 paid directly via bank)
        bank_sale_res = client.post(
            "/api/sales/create",
            json={
                "invoice_no": "INV-C2A-BANK-3",
                "sale_date": today_str,
                "customer_id": cust_id,
                "payment_mode": "BANK_TRANSFER",
                "total_taxable": 476.19,
                "total_cgst": 11.91,
                "total_sgst": 11.90,
                "net_amount": 500.0,
                "paid_amount": 500.0,
                "due_amount": 0.0,
                "items": [
                    {
                        "product_id": prod_id,
                        "batch_id": batch_id,
                        "qty": 1.0,
                        "sale_rate": 500.0,
                        "mrp": 550.0,
                        "taxable_amount": 476.19,
                        "cgst_rate": 2.5,
                        "cgst_amount": 11.91,
                        "sgst_rate": 2.5,
                        "sgst_amount": 11.90,
                        "total_amount": 500.0,
                    }
                ],
            },
            headers=headers,
        )
        assert bank_sale_res.status_code == 200, bank_sale_res.text
        bank_sale_id = bank_sale_res.json()["sale_id"]

        # PDF for BANK_TRANSFER sale generates cleanly
        pdf_res = client.get(f"/api/sales/{bank_sale_id}/pdf", headers=headers)
        assert pdf_res.status_code == 200
        assert pdf_res.content.startswith(b"%PDF-")

        # 5. Customer receipts: ₹500 BANK_TRANSFER + ₹300 CHEQUE + ₹200 CASH (leaves ₹500 due)
        for mode, amt in (("BANK_TRANSFER", 500.0), ("CHEQUE", 300.0), ("CASH", 200.0)):
            r_res = client.post(
                "/api/accounting/customer-receipt",
                json={
                    "customer_id": cust_id,
                    "receipt_date": today_str,
                    "amount": amt,
                    "payment_mode": mode,
                },
                headers=headers,
            )
            assert r_res.status_code == 200, f"Receipt failed for {mode}: {r_res.text}"

        # 6. Manual supplier payments across non-UPI modes (BANK_TRANSFER, CHEQUE, CASH, CARD)
        for mode, amt, ref in (
            ("BANK_TRANSFER", 1200.0, "NEFT-001"),
            ("CHEQUE", 800.0, "CHQ-4411"),
            ("CASH", 500.0, "COUNTER-CASH"),
            ("CARD", 300.0, "CARD-9911"),
        ):
            sp_res = client.post(
                "/api/accounting/supplier-payment",
                json={
                    "supplier_id": supp_id,
                    "amount": amt,
                    "payment_date": today_str,
                    "payment_mode": mode,
                    "reference_no": ref,
                },
                headers=headers,
            )
            assert sp_res.status_code == 200, f"Supplier payment failed for {mode}: {sp_res.text}"

        # 7. Operational expenses across CASH & BANK_TRANSFER
        for mode, amt, note in (("CASH", 150.0, "Tea & snacks"), ("BANK_TRANSFER", 600.0, "Transport")):
            exp_res = client.post(
                "/api/accounting/expenses",
                json={
                    "category_id": 1,
                    "amount": amt,
                    "expense_date": today_str,
                    "payment_mode": mode,
                    "remarks": note,
                },
                headers=headers,
            )
            assert exp_res.status_code == 200, exp_res.text

        # Verify Farmer statement & remaining credit balance (1500 - 500 - 300 - 200 = 500)
        f_stmt = client.get(f"/api/masters/farmers/{cust_id}/statement", headers=headers)
        assert f_stmt.status_code == 200
        f_data = f_stmt.json()
        assert f_data["customer"]["current_balance"] == pytest.approx(500.0)
        assert {inv["payment_mode"] for inv in f_data["invoices"]} == {"CASH", "CREDIT", "BANK_TRANSFER"}
        manual_receipts = [
            r for r in f_data["receipts"]
            if "Payment received from customer" in (r.get("narration") or "")
        ]
        assert len(manual_receipts) == 3

        # Verify Supplier statement & remaining payable (5000 - 1200 - 800 - 500 - 300 = 2200)
        s_stmt = client.get(f"/api/accounting/suppliers/{supp_id}/statement", headers=headers)
        assert s_stmt.status_code == 200
        s_data = s_stmt.json()
        assert s_data["supplier"]["current_balance"] == pytest.approx(2200.0)
        assert len(s_data["payments"]) == 4

        # Verify Day Book & Cash Drawer Reconciliation (only CASH items affect drawer)
        db_res = client.get(f"/api/accounting/day-book?date={today_str}", headers=headers)
        assert db_res.status_code == 200
        assert db_res.json()["total_sales"] == pytest.approx(3000.0)
        assert db_res.json()["total_expenses"] == pytest.approx(750.0)

        cash_res = client.get(f"/api/accounting/cash-closing-summary?date={today_str}", headers=headers)
        assert cash_res.status_code == 200
        cash_summary = cash_res.json()
        assert cash_summary["cash_sales"] == pytest.approx(1000.0)
        # CASH receipt vouchers include cash sale voucher (1000.0) + manual cash receipt (200.0),
        # while BANK_TRANSFER / CHEQUE / CARD receipts & sales go to Bank Account instead of Cash drawer.
        assert cash_summary["farmer_cash_receipts"] == pytest.approx(1200.0)
        assert cash_summary["cash_expenses"] == pytest.approx(150.0)
        assert cash_summary["cash_supplier_payments"] == pytest.approx(500.0)
        assert cash_summary["expected_drawer_cash"] == pytest.approx(5000.0 + 1000.0 + 1200.0 - 150.0 - 500.0)

        # Verify remaining stock (30 - 2 - 3 - 1 = 24) and double-entry vouchers balance
        batches = client.get(f"/api/inventory/batches/{prod_id}", headers=headers).json()
        assert batches[0]["current_qty"] == pytest.approx(24.0)

        db_mgr = get_db_manager()
        unbalanced = db_mgr.fetch_all(
            """
            SELECT v.voucher_no,
                   ROUND(SUM(le.debit_amount), 2) AS debits,
                   ROUND(SUM(le.credit_amount), 2) AS credits
            FROM vouchers v
            JOIN ledger_entries le ON v.voucher_id = le.voucher_id
            GROUP BY v.voucher_id
            HAVING ABS(debits - credits) > 0.05;
            """
        )
        assert unbalanced == []

    reset_db_manager()


def test_c2a_preserves_historical_upi_records_and_column_compat(tmp_path, monkeypatch):
    """
    C2-A: Existing databases with historical payment_mode='UPI' records and company_settings.upi_id
    remain readable and intact when shop settings are saved from the updated UI (without upi_id).
    """
    shop_resp, _ = _setup_isolated_clean_shop(
        monkeypatch,
        tmp_path / "c2a_historical",
        username="owner_hist",
        password="HistPassword#1",
        full_name="Vijay Deshpande",
        shop_name="Deshpande Agro Center",
        mobile="9822998877",
        address="Shivaji Chowk, Sangli",
        city="Sangli",
        gstin="27DDDDD4444D1Z4",
        upi_id="legacy.deshpande@sbi",
        email="deshpande@example.com",
    )
    headers = {"Authorization": f"Bearer {shop_resp['token']}"}
    db_mgr = get_db_manager()

    # Seed historical UPI records directly (simulating pre-C2-A production data)
    with db_mgr.transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO customers (customer_name, mobile, village, current_balance)
            VALUES ('Historical Farmer', '9911223344', 'Tasgaon', 0.0);
            """
        )
        cust_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO suppliers (supplier_name, contact_person, mobile, city, current_balance)
            VALUES ('Historical Supplier', 'Ramesh', '9800112233', 'Pune', 1000.0);
            """
        )
        supp_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO products (
                product_name, category_id, manufacturer_id, hsn_code, unit_id,
                tax_group_id, default_purchase_rate, default_sale_rate, default_mrp, is_active
            ) VALUES ('Legacy Herbicide', 1, 1, '38089300', 1, 2, 200.0, 250.0, 280.0, 1);
            """
        )
        prod_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO stock_batches (
                product_id, batch_no, mfg_date, exp_date, purchase_rate,
                sale_rate, mrp, opening_qty, current_qty
            ) VALUES (?, 'LEG-UPI-01', '2025-01-01', '2027-01-01', 200.0, 250.0, 280.0, 10.0, 8.0);
            """,
            (prod_id,),
        )
        batch_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO sales (
                invoice_no, sale_date, customer_id, payment_mode,
                total_taxable, total_cgst, total_sgst, total_igst, total_discount,
                extra_charges, round_off, net_amount, paid_amount, due_amount, remarks
            ) VALUES (
                'INV-HIST-UPI-1', '2026-08-15', ?, 'UPI',
                476.19, 11.91, 11.90, 0.0, 0.0,
                0.0, 0.0, 500.0, 500.0, 0.0, 'Historical UPI sale'
            );
            """,
            (cust_id,),
        )
        hist_sale_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO sale_items (
                sale_id, product_id, batch_id, qty, unit_id,
                sale_rate, mrp, discount_percent, discount_amount, taxable_amount,
                cgst_rate, cgst_amount, sgst_rate, sgst_amount, igst_rate, igst_amount, total_amount
            ) VALUES (?, ?, ?, 2.0, 1, 250.0, 280.0, 0.0, 0.0, 476.19, 2.5, 11.91, 2.5, 11.90, 0.0, 0.0, 500.0);
            """,
            (hist_sale_id, prod_id, batch_id),
        )

        cur.execute(
            """
            INSERT INTO purchases (
                invoice_no, purchase_date, supplier_id, payment_type,
                total_taxable, total_cgst, total_sgst, net_amount, paid_amount, due_amount
            ) VALUES ('PUR-HIST-UPI-1', '2026-08-10', ?, 'UPI', 2000.0, 0.0, 0.0, 2000.0, 1000.0, 1000.0);
            """,
            (supp_id,),
        )

    with TestClient(app) as client:
        # 1. Saving Shop Profile without upi_id (new UI behavior) preserves SQLite upi_id column value
        save_res = client.post(
            "/api/system/settings",
            json={
                "company_name": "Deshpande Agro & Seeds",
                "owner_name": "Vijay Deshpande",
                "mobile": "9822998800",
                "email": "deshpande@example.com",
            },
            headers=headers,
        )
        assert save_res.status_code == 200, save_res.text
        assert save_res.json()["settings"]["company_name"] == "Deshpande Agro & Seeds"
        assert save_res.json()["settings"]["upi_id"] == "legacy.deshpande@sbi"

        # 2. Historical UPI sale detail, farmer statement, and invoice PDF remain readable
        sale_res = client.get(f"/api/sales/{hist_sale_id}", headers=headers)
        assert sale_res.status_code == 200
        assert sale_res.json()["payment_mode"] == "UPI"

        pdf_res = client.get(f"/api/sales/{hist_sale_id}/pdf", headers=headers)
        assert pdf_res.status_code == 200
        assert pdf_res.content.startswith(b"%PDF-")

        f_stmt = client.get(f"/api/masters/farmers/{cust_id}/statement", headers=headers)
        assert f_stmt.status_code == 200
        assert f_stmt.json()["invoices"][0]["payment_mode"] == "UPI"

        # 3. Historical UPI purchase & payment remain readable in supplier statement
        s_stmt = client.get(f"/api/accounting/suppliers/{supp_id}/statement", headers=headers)
        assert s_stmt.status_code == 200
        assert s_stmt.json()["purchases"][0]["payment_mode"] == "UPI"

        # 4. Day Book & Cash Reconciliation read historical date cleanly without counting UPI as cash drawer
        db_res = client.get("/api/accounting/day-book?date=2026-08-15", headers=headers)
        assert db_res.status_code == 200
        assert db_res.json()["total_sales"] == pytest.approx(500.0)

        cash_res = client.get("/api/accounting/cash-closing-summary?date=2026-08-15", headers=headers)
        assert cash_res.status_code == 200
        assert cash_res.json()["cash_sales"] == pytest.approx(0.0)

        # 5. Historical UPI record in DB was never rewritten or deleted
        row = db_mgr.fetch_one("SELECT payment_mode FROM sales WHERE sale_id = ?;", (hist_sale_id,))
        assert row is not None and row["payment_mode"] == "UPI"

    reset_db_manager()


# =====================================================================
# C2-B: Professional GST Invoice Design & Dynamic Shop Branding
# =====================================================================
def _extract_pdf_text_and_fonts(pdf_bytes: bytes) -> Tuple[List[str], str]:
    """
    Pure-Python ReportLab PDF inspector:
    Decompresses Flate / ASCII85+Flate streams, parses Font ToUnicode CMaps,
    and decodes rendered Tj text runs + embedded BaseFont names.
    """
    import base64
    import re
    import zlib

    objs: Dict[int, bytes] = {}
    for m in re.finditer(rb"(\d+)\s+0\s+obj\b(.*?)\bendobj", pdf_bytes, flags=re.S):
        objs[int(m.group(1))] = m.group(2)

    def decode_stream(obj_body: bytes) -> bytes:
        sm = re.search(rb"stream[\r\n]+(.*?)endstream", obj_body, flags=re.S)
        if not sm:
            return b""
        raw = sm.group(1).strip()
        if b"/ASCII85Decode" in obj_body or raw.endswith(b"~>"):
            return zlib.decompress(base64.a85decode(raw, adobe=True))
        if b"/FlateDecode" in obj_body:
            return zlib.decompress(raw)
        return raw

    cmaps: Dict[int, Dict[int, str]] = {}
    for oid, body in objs.items():
        if b"stream" not in body:
            continue
        try:
            dec = decode_stream(body)
        except Exception:
            continue
        if b"beginbfchar" in dec:
            txt = dec.decode("latin1", errors="replace")
            mp: Dict[int, str] = {}
            for cm in re.finditer(r"<([0-9A-Fa-f]+)>\s+<([0-9A-Fa-f]+)>", txt):
                src = int(cm.group(1), 16)
                dst = bytes.fromhex(cm.group(2)).decode("utf-16-be", errors="replace")
                mp[src] = dst
            cmaps[oid] = mp

    basefonts: set[str] = set()
    font_ref_to_cmap: Dict[int, Dict[int, str] | None] = {}
    for oid, body in objs.items():
        bf = re.search(rb"/BaseFont\s+/([^\s/>]+)", body)
        if bf:
            basefonts.add(bf.group(1).decode("latin1"))
            tu = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", body)
            if tu and int(tu.group(1)) in cmaps:
                font_ref_to_cmap[oid] = cmaps[int(tu.group(1))]
            else:
                font_ref_to_cmap[oid] = None

    res_map: Dict[str, Dict[int, str] | None] = {}
    for _oid, body in objs.items():
        for fm in re.finditer(rb"/([A-Za-z0-9_+]+)\s+(\d+)\s+0\s+R", body):
            key = fm.group(1).decode("latin1")
            target = int(fm.group(2))
            if target in font_ref_to_cmap:
                res_map[key] = font_ref_to_cmap[target]

    def unescape_pdf_str(raw_bytes: bytes) -> bytes:
        out = bytearray()
        i = 0
        n = len(raw_bytes)
        while i < n:
            b = raw_bytes[i]
            if b == 92 and i + 1 < n:
                nxt = raw_bytes[i + 1]
                if 48 <= nxt <= 55:
                    oct_digits = bytes([nxt])
                    j = i + 2
                    while j < n and len(oct_digits) < 3 and 48 <= raw_bytes[j] <= 55:
                        oct_digits += bytes([raw_bytes[j]])
                        j += 1
                    out.append(int(oct_digits, 8))
                    i = j
                    continue
                esc_map = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12, 40: 40, 41: 41, 92: 92}
                if nxt in esc_map:
                    out.append(esc_map[nxt])
                    i += 2
                    continue
                i += 2
                continue
            out.append(b)
            i += 1
        return bytes(out)

    decoded_chunks: List[str] = []
    pattern = re.compile(rb"/([A-Za-z0-9_+]+)\s+[\d.]+\s+Tf|\(((?:\\.|[^\\)])*)\)\s*Tj")
    for _oid, body in objs.items():
        if b"/Contents" in body or b"stream" not in body:
            continue
        try:
            dec = decode_stream(body)
        except Exception:
            continue
        if b"BT" not in dec:
            continue
        cur_cmap: Dict[int, str] | None = None
        for m in pattern.finditer(dec):
            if m.group(1):
                cur_cmap = res_map.get(m.group(1).decode("latin1"))
            elif m.group(2) is not None:
                raw_s = unescape_pdf_str(m.group(2))
                if cur_cmap:
                    decoded_chunks.append("".join(cur_cmap.get(b, "") for b in raw_s))
                else:
                    decoded_chunks.append(raw_s.decode("cp1252", errors="replace"))

    return sorted(basefonts), "".join(decoded_chunks)


def test_c2b_frontend_invoice_preview_has_no_developer_pii_and_complete_columns(tmp_path, monkeypatch):
    """
    C2-B Task 1, 3, 4, 5, 7:
    - HTML invoice preview modal and JS helpers contain zero hardcoded developer PII or fake placeholders.
    - Product preview table contains all 13 commercial GST columns.
    - Totals section exposes Subtotal, Discount, Taxable Value, CGST, SGST, IGST, Round Off, Grand Total, Paid, Balance Due.
    - submitSalesBill fetches authoritative backend sale details and downloadInvoicePdf uses authenticated fetch.
    """
    reset_db_manager()
    local_app = tmp_path / "c2b_ui_check"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))

    with TestClient(app) as client:
        html = client.get("/").text
        js = client.get("/static/js/app.js").text
        css = client.get("/static/css/app.css").text

        forbidden_developer_markers = (
            "कृषीधन कृषी उद्योग समूह",
            "कृषीधन कृषी सेवा केंद्र",
            "KRUSHIDHAN AGRI UDYOG SAMUH",
            "आकाश लेंगारे",
            "9503573620",
            "7218409780",
            "akashlengare",
            "27BSXPL3414R1Z5",
            "LCFRD0920250506SNG",
            "LCID0920250497SNG",
            "LCSD0920250506SNG",
            "विसापूर",
            "तासगाव",
            "नागोबा कट्ट्याशेजारी",
            "TASGAON",
            "SANGLI Jurisdiction",
            "S/2026-27",
            "11:30 AM",
            "BAYER CROP",
        )
        for marker in forbidden_developer_markers:
            assert marker not in html, f"Hardcoded developer info still present in index.html: {marker}"
            assert marker not in js, f"Hardcoded developer info still present in app.js: {marker}"

        # Required invoice preview modal element IDs & totals breakdown
        required_html_ids = (
            'id="inv-prev-shop-name"',
            'id="inv-prev-shop-owner"',
            'id="inv-prev-shop-address"',
            'id="inv-prev-shop-contact"',
            'id="inv-prev-gstin"',
            'id="inv-prev-lic-fert"',
            'id="inv-prev-lic-pest"',
            'id="inv-prev-lic-seed"',
            'id="inv-prev-cust-name"',
            'id="inv-prev-cust-addr"',
            'id="inv-prev-cust-mobile"',
            'id="inv-prev-cust-gstin"',
            'id="inv-prev-inv-no"',
            'id="inv-prev-date"',
            'id="inv-prev-paymode"',
            'id="inv-prev-pay-status"',
            'id="inv-prev-subtotal"',
            'id="inv-prev-disc"',
            'id="inv-prev-taxable"',
            'id="inv-prev-cgst"',
            'id="inv-prev-sgst"',
            'id="inv-prev-igst"',
            'id="inv-prev-round"',
            'id="inv-prev-net"',
            'id="inv-prev-paid"',
            'id="inv-prev-balance"',
            'id="inv-prev-words"',
            'id="inv-prev-terms"',
            'id="inv-prev-auth-company"',
            'id="inv-prev-jurisdiction"',
        )
        for dom_id in required_html_ids:
            assert dom_id in html, f"Missing expected C2-B DOM element {dom_id} in index.html"

        # 13 required product columns in invoice preview table
        inv_modal_start = html.index('id="modal-invoice-preview"')
        inv_modal_end = html.index("<!-- Add New Manufacturer Modal -->", inv_modal_start)
        inv_modal_html = html[inv_modal_start:inv_modal_end]
        for col_label in ("Product Description", "HSN", "Batch No", "Exp Date", "Qty", "Unit", "Rate", "Disc", "Taxable", "GST %", "Tax", "Total"):
            assert col_label in inv_modal_html, f"Missing invoice product column '{col_label}'"

        # JS uses escapeHtml, backend sale detail fetch, authenticated PDF fetch, and no KRUSHIDHAN product fallback
        assert "function escapeHtml(" in js
        assert "await fetch(`/api/sales/${result.sale_id}`)" in js
        assert "const res = await fetch(`/api/sales/${id}/pdf`)" in js
        assert "item.company_name || 'KRUSHIDHAN'" not in js
        assert "@media print" in css and "size: A4 portrait" in css

    reset_db_manager()


def test_c2b_multi_shop_dynamic_invoice_pdf_english_special_chars_and_taxes(tmp_path, monkeypatch):
    """
    C2-B & C2-C End-to-End Integration Verification across Shop A & Shop B:
    - Shop A: English-only + special chars (&, <, >) + all 3 licences + GSTIN + bank details + payment terms
    - Line items: discount, CGST + SGST, HSN, batch, expiry, unit resolved via product
    - Partial payment: verifies Paid Amount, Balance Due, and PARTIAL status without altering calculations
    - Shop B: Minimal/different profile (zero licences, different owner/GSTIN, zero Shop A bleedover)
    - Inspects decoded PDF content streams (Helvetica English text + Rupee font for ₹)
    """
    from src.printing.invoice_printer import InvoicePrinter
    from src.repositories.system_repository import SystemRepository

    # ------------------------------------------------------------------
    # SHOP A SETUP (English-only, special chars, full profile & licences)
    # ------------------------------------------------------------------
    shop_a_resp, _shop_a_dir = _setup_isolated_clean_shop(
        monkeypatch,
        tmp_path / "c2b_shop_a",
        username="owner_mauli",
        password="MauliPassword#1",
        full_name="Rajendra Deshmukh",
        shop_name="Mauli & Sons <Agro> - Baramati Kendra",
        mobile="9822112233",
        address="Market Yard Road",
        city="Baramati",
        gstin="27AAAAA1111A1Z1",
        upi_id="",
        email="mauli@agroshop.in",
    )
    headers_a = {"Authorization": f"Bearer {shop_a_resp['token']}"}
    today_str = date.today().isoformat()

    with TestClient(app) as client_a:
        # Update licences, bank details, pincode & custom invoice terms for Shop A
        upd_a = client_a.post(
            "/api/system/settings",
            json={
                "pincode": "413102",
                "dl_fertilizer": "FERT-BRM-2026-101",
                "dl_pesticide": "PEST-BRM-2026-202",
                "dl_seed": "SEED-BRM-2026-303",
                "bank_name": "Bank of Maharashtra",
                "account_no": "60099887766",
                "ifsc_code": "MAHB0000123",
                "invoice_terms": "Payment due within 15 days. Check batch & expiry on receipt.",
            },
            headers=headers_a,
        )
        assert upd_a.status_code == 200, upd_a.text

        # Create Customer with village, taluka, district, GSTIN
        cust_a = client_a.post(
            "/api/masters/customers",
            json={
                "customer_name": "Ramesh Patil (Ramesh & Co)",
                "mobile": "9811223344",
                "village": "Karhati",
                "taluka": "Baramati",
                "district": "Pune",
                "gstin": "27PQRSX9999A1Z2",
                "opening_balance": 0.0,
            },
            headers=headers_a,
        )
        assert cust_a.status_code == 200, cust_a.text
        cust_a_id = cust_a.json()["customer_id"]

        # Create English product & batch
        prod_a = client_a.post(
            "/api/masters/products",
            json={
                "product_name": "Urea Gold Fertilizer (IFFCO 50kg)",
                "category_id": 1,
                "manufacturer_id": 1,
                "hsn_code": "31052000",
                "unit_id": 1,
                "tax_group_id": 2,
                "default_purchase_rate": 420.0,
                "default_sale_rate": 500.0,
                "default_mrp": 550.0,
                "min_stock_alert": 5.0,
                "is_active": 1,
            },
            headers=headers_a,
        )
        assert prod_a.status_code == 200, prod_a.text
        prod_a_id = prod_a.json()["product_id"]

        stk_a = client_a.post(
            "/api/inventory/add-stock",
            json={
                "product_id": prod_a_id,
                "qty": 20.0,
                "batch_no": "BATCH-A99",
                "purchase_rate": 420.0,
                "sale_rate": 500.0,
                "mrp": 550.0,
                "mfg_date": "2026-01-10",
                "exp_date": "2028-06-30",
            },
            headers=headers_a,
        )
        assert stk_a.status_code == 200, stk_a.text
        batch_a_id = stk_a.json()["batch_id"]

        # Create partial-payment invoice with discount, round_off & GST (omitting item.unit_id to verify fallback resolution)
        sale_a = client_a.post(
            "/api/sales/create",
            json={
                "invoice_no": "INV-BRM-0042",
                "sale_date": today_str,
                "customer_id": cust_a_id,
                "doctor_or_officer": "Sugarcane Crop",
                "payment_mode": "BANK_TRANSFER",
                "total_taxable": 1359.05,
                "total_cgst": 33.98,
                "total_sgst": 33.97,
                "total_igst": 0.0,
                "total_discount": 73.0,
                "round_off": 0.0,
                "net_amount": 1427.0,
                "paid_amount": 1000.0,
                "due_amount": 427.0,
                "remarks": "Loaded on tractor",
                "items": [
                    {
                        "product_id": prod_a_id,
                        "batch_id": batch_a_id,
                        "qty": 3.0,
                        "sale_rate": 500.0,
                        "mrp": 550.0,
                        "discount_percent": 4.87,
                        "discount_amount": 73.0,
                        "taxable_amount": 1359.05,
                        "cgst_rate": 2.5,
                        "cgst_amount": 33.98,
                        "sgst_rate": 2.5,
                        "sgst_amount": 33.97,
                        "igst_rate": 0.0,
                        "igst_amount": 0.0,
                        "total_amount": 1427.0,
                    }
                ],
            },
            headers=headers_a,
        )
        assert sale_a.status_code == 200, sale_a.text
        sale_a_id = sale_a.json()["sale_id"]

        # Verify authoritative GET /api/sales/{sale_id} preserves calculations & resolves customer + unit fields
        detail_a = client_a.get(f"/api/sales/{sale_a_id}", headers=headers_a)
        assert detail_a.status_code == 200
        d_json = detail_a.json()
        assert d_json["invoice_no"] == "INV-BRM-0042"
        assert d_json["customer_taluka"] == "Baramati"
        assert d_json["customer_district"] == "Pune"
        assert d_json["customer_gstin"] == "27PQRSX9999A1Z2"
        assert d_json["total_discount"] == pytest.approx(73.0)
        assert d_json["total_taxable"] == pytest.approx(1359.05)
        assert d_json["total_cgst"] == pytest.approx(33.98)
        assert d_json["total_sgst"] == pytest.approx(33.97)
        assert d_json["net_amount"] == pytest.approx(1427.0)
        assert d_json["paid_amount"] == pytest.approx(1000.0)
        assert d_json["due_amount"] == pytest.approx(427.0)
        assert d_json["items"][0]["unit_name"]  # resolved cleanly from product unit_id fallback

        # Unauthenticated PDF request must fail closed
        assert client_a.get(f"/api/sales/{sale_a_id}/pdf").status_code == 401

        # Fetch PDF bytes for Shop A
        pdf_a_res = client_a.get(f"/api/sales/{sale_a_id}/pdf", headers=headers_a)
        assert pdf_a_res.status_code == 200
        assert pdf_a_res.content.startswith(b"%PDF-")

        fonts_a, text_a = _extract_pdf_text_and_fonts(pdf_a_res.content)
        # C2-C: English-only Helvetica + offline Rupee glyph font subset ONLY
        assert "Helvetica" in fonts_a
        assert "Helvetica-Bold" in fonts_a
        assert "\x7f" not in text_a
        assert "₹" in text_a

        # Verify all Shop A dynamic English fields & special chars rendered cleanly in PDF
        expected_shop_a_tokens = (
            "TAX INVOICE",
            "Mauli & Sons <Agro> - Baramati Kendra",
            "Rajendra Deshmukh",
            "Market Yard Road",
            "Baramati",
            "413102",
            "9822112233",
            "mauli@agroshop.in",
            "27AAAAA1111A1Z1",
            "FERT-BRM-2026-101",
            "PEST-BRM-2026-202",
            "SEED-BRM-2026-303",
            "INV-BRM-0042",
            today_str,
            "Ramesh Patil (Ramesh & Co)",
            "Karhati, Baramati, Pune",
            "9811223344",
            "27PQRSX9999A1Z2",
            "Sugarcane Crop",
            "BANK TRANSFER",
            "PARTIAL",
            "Urea Gold Fertilizer",
            "IFFCO",
            "50kg",
            "31052000",
            "BATCH-A99",
            "2028-06-30",
            "500.00",
            "73.00",
            "1359.05",
            "5%",
            "67.95",
            "1427.00",
            "1,500.00",   # Subtotal (Gross) = 1427 + 73
            "1,359.05",   # Taxable Value
            "33.98",      # CGST
            "33.97",      # SGST
            "1,427.00",   # Grand Total
            "1,000.00",   # Paid Amount
            "427.00",     # Balance Due
            "RUPEES ONE THOUSAND FOUR HUNDRED TWENTY SEVEN ONLY",
            "Payment due within 15 days. Check batch & expiry on receipt.",
            "Bank of Maharashtra",
            "60099887766",
            "MAHB0000123",
            "Customer Signature",
            "Authorized Signatory",
            "Subject to Baramati Jurisdiction",
        )
        for token in expected_shop_a_tokens:
            assert token in text_a, f"Missing expected token '{token}' in Shop A PDF text:\n{text_a}"

        # Ensure zero Devanagari, bilingual labels, or developer PII in Shop A PDF
        assert not any("\u0900" <= ch <= "\u097f" for ch in text_a), f"Devanagari leaked into English-only PDF:\n{text_a}"
        for forbidden in ("ग्राहकाची सही", "कर बीजक", "कृषीधन", "आकाश लेंगारे", "9503573620", "7218409780", "27BSXPL3414R1Z5", "LCFRD", "S/2026-27", "11:30 AM", "BAYER CROP", "TASGAON", "SANGLI"):
            assert forbidden not in text_a, f"Forbidden string '{forbidden}' leaked into Shop A PDF"

    # ------------------------------------------------------------------
    # SHOP B SETUP (Completely isolated shop with minimal details + IGST invoice)
    # ------------------------------------------------------------------
    shop_b_resp, _shop_b_dir = _setup_isolated_clean_shop(
        monkeypatch,
        tmp_path / "c2b_shop_b",
        username="owner_sahyadri",
        password="SahyadriPassword#2",
        full_name="Nitin Kulkarni",
        shop_name="Sahyadri Farm Inputs",
        mobile="9766000222",
        address="",
        city="",
        gstin="27BBBBB2222B1Z2",
        upi_id="",
        email="",
    )
    headers_b = {"Authorization": f"Bearer {shop_b_resp['token']}"}

    with TestClient(app) as client_b:
        cust_b = client_b.post(
            "/api/masters/customers",
            json={"customer_name": "Sunil Shinde", "mobile": "9700112233", "village": "Sangola", "opening_balance": 0.0},
            headers=headers_b,
        ).json()
        prod_b = client_b.post(
            "/api/masters/products",
            json={
                "product_name": "Hybrid Cotton Seed",
                "category_id": 1,
                "manufacturer_id": 1,
                "hsn_code": "12099190",
                "unit_id": 1,
                "tax_group_id": 1,
                "default_purchase_rate": 700.0,
                "default_sale_rate": 850.0,
                "default_mrp": 900.0,
                "is_active": 1,
            },
            headers=headers_b,
        ).json()
        stk_b = client_b.post(
            "/api/inventory/add-stock",
            json={"product_id": prod_b["product_id"], "qty": 10.0, "batch_no": "SEED-B1", "purchase_rate": 700.0, "sale_rate": 850.0, "mrp": 900.0},
            headers=headers_b,
        ).json()

        # Credit unpaid IGST invoice
        sale_b = client_b.post(
            "/api/sales/create",
            json={
                "invoice_no": "INV-SGL-0009",
                "sale_date": today_str,
                "customer_id": cust_b["customer_id"],
                "payment_mode": "CREDIT",
                "total_taxable": 800.0,
                "total_cgst": 0.0,
                "total_sgst": 0.0,
                "total_igst": 144.0,
                "net_amount": 944.0,
                "paid_amount": 0.0,
                "due_amount": 944.0,
                "items": [
                    {
                        "product_id": prod_b["product_id"],
                        "batch_id": stk_b["batch_id"],
                        "qty": 1.0,
                        "sale_rate": 944.0,
                        "mrp": 950.0,
                        "taxable_amount": 800.0,
                        "igst_rate": 18.0,
                        "igst_amount": 144.0,
                        "total_amount": 944.0,
                    }
                ],
            },
            headers=headers_b,
        )
        assert sale_b.status_code == 200, sale_b.text
        sale_b_id = sale_b.json()["sale_id"]

        pdf_b_res = client_b.get(f"/api/sales/{sale_b_id}/pdf", headers=headers_b)
        assert pdf_b_res.status_code == 200
        _fonts_b, text_b = _extract_pdf_text_and_fonts(pdf_b_res.content)

        # Shop B contains Shop B identity, IGST, CREDIT/UNPAID status, hides unconfigured licences
        for expected_b in ("Sahyadri Farm Inputs", "Nitin Kulkarni", "9766000222", "27BBBBB2222B1Z2", "INV-SGL-0009", "Sunil Shinde", "Sangola", "IGST", "144.00", "944.00", "CREDIT / UNPAID"):
            assert expected_b in text_b, f"Missing expected Shop B token '{expected_b}'"
        for omitted_b in ("Fertilizer Lic.:", "Pesticide Lic.:", "Seed Lic.:", "Email:"):
            assert omitted_b not in text_b, f"Blank setting label '{omitted_b}' should be omitted cleanly"

        # Strict multi-shop isolation: zero Shop A data or developer PII in Shop B PDF
        for leak in ("Mauli", "माऊली", "Baramati", "27AAAAA1111A1Z1", "FERT-BRM", "INV-BRM-0042", "कृषीधन", "आकाश लेंगारे", "9503573620", "TASGAON"):
            assert leak not in text_b, f"Cross-shop / developer leak '{leak}' detected in Shop B PDF"

    reset_db_manager()


# =====================================================================
# C2-C: English-Only Invoice & PDF System
# =====================================================================
def _inspect_pdf_font_usage(pdf_bytes: bytes) -> Tuple[List[str], str, List[str]]:
    """
    Inspect PDF objects and return:
    - sorted embedded BaseFont names
    - full decoded text
    - list of strings emitted specifically using TrueType /ToUnicode fonts (should only be '₹')
    """
    import base64
    import re
    import zlib

    objs: Dict[int, bytes] = {}
    for m in re.finditer(rb"(\d+)\s+0\s+obj\b(.*?)\bendobj", pdf_bytes, flags=re.S):
        objs[int(m.group(1))] = m.group(2)

    def decode_stream(obj_body: bytes) -> bytes:
        sm = re.search(rb"stream[\r\n]+(.*?)endstream", obj_body, flags=re.S)
        if not sm:
            return b""
        raw = sm.group(1).strip()
        if b"/ASCII85Decode" in obj_body or raw.endswith(b"~>"):
            return zlib.decompress(base64.a85decode(raw, adobe=True))
        if b"/FlateDecode" in obj_body:
            return zlib.decompress(raw)
        return raw

    cmaps: Dict[int, Dict[int, str]] = {}
    for oid, body in objs.items():
        if b"stream" not in body:
            continue
        try:
            dec = decode_stream(body)
        except Exception:
            continue
        if b"beginbfchar" in dec:
            txt = dec.decode("latin1", errors="replace")
            mp: Dict[int, str] = {}
            for cm in re.finditer(r"<([0-9A-Fa-f]+)>\s+<([0-9A-Fa-f]+)>", txt):
                mp[int(cm.group(1), 16)] = bytes.fromhex(cm.group(2)).decode("utf-16-be", errors="replace")
            cmaps[oid] = mp

    basefonts: set[str] = set()
    font_ref_to_cmap: Dict[int, Dict[int, str] | None] = {}
    for oid, body in objs.items():
        bf = re.search(rb"/BaseFont\s+/([^\s/>]+)", body)
        if bf:
            basefonts.add(bf.group(1).decode("latin1"))
            tu = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", body)
            font_ref_to_cmap[oid] = cmaps[int(tu.group(1))] if (tu and int(tu.group(1)) in cmaps) else None

    res_map: Dict[str, Dict[int, str] | None] = {}
    for _oid, body in objs.items():
        for fm in re.finditer(rb"/([A-Za-z0-9_+]+)\s+(\d+)\s+0\s+R", body):
            target = int(fm.group(2))
            if target in font_ref_to_cmap:
                res_map[fm.group(1).decode("latin1")] = font_ref_to_cmap[target]

    def unescape_pdf_str(raw_bytes: bytes) -> bytes:
        out = bytearray()
        i = 0
        n = len(raw_bytes)
        while i < n:
            b = raw_bytes[i]
            if b == 92 and i + 1 < n:
                nxt = raw_bytes[i + 1]
                if 48 <= nxt <= 55:
                    oct_digits = bytes([nxt])
                    j = i + 2
                    while j < n and len(oct_digits) < 3 and 48 <= raw_bytes[j] <= 55:
                        oct_digits += bytes([raw_bytes[j]])
                        j += 1
                    out.append(int(oct_digits, 8))
                    i = j
                    continue
                esc_map = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12, 40: 40, 41: 41, 92: 92}
                if nxt in esc_map:
                    out.append(esc_map[nxt])
                    i += 2
                    continue
                i += 2
                continue
            out.append(b)
            i += 1
        return bytes(out)

    decoded_chunks: List[str] = []
    tounicode_runs: List[str] = []
    pattern = re.compile(rb"/([A-Za-z0-9_+]+)\s+[\d.]+\s+Tf|\(((?:\\.|[^\\)])*)\)\s*Tj")
    for _oid, body in objs.items():
        if b"/Contents" in body or b"stream" not in body:
            continue
        try:
            dec = decode_stream(body)
        except Exception:
            continue
        if b"BT" not in dec:
            continue
        cur_cmap: Dict[int, str] | None = None
        for m in pattern.finditer(dec):
            if m.group(1):
                cur_cmap = res_map.get(m.group(1).decode("latin1"))
            elif m.group(2) is not None:
                raw_s = unescape_pdf_str(m.group(2))
                if cur_cmap:
                    run_txt = "".join(cur_cmap.get(b, "") for b in raw_s)
                    tounicode_runs.append(run_txt)
                    decoded_chunks.append(run_txt)
                else:
                    decoded_chunks.append(raw_s.decode("cp1252", errors="replace"))

    return sorted(basefonts), "".join(decoded_chunks), tounicode_runs


def test_c2c_english_only_src_frontend_and_clean_masters(tmp_path, monkeypatch):
    """
    C2-C Task 1, 4 & 6:
    - Zero Devanagari characters (\\u0900-\\u097F) anywhere in src/ (.py, .html, .js, .css, .sql).
    - UI HTML/JS/CSS & clean seeded master lookups use crisp English-only terminology.
    - Preserves KRUSHIDHAN app title, Cash & Credit billing modes, and Ledger tabs.
    """
    import re
    from pathlib import Path

    src_root = Path(__file__).resolve().parent.parent / "src"
    deva_re = re.compile(r"[\u0900-\u097F]")
    devanagari_hits: List[str] = []
    for fpath in sorted(src_root.rglob("*")):
        if fpath.is_file() and fpath.suffix in {".py", ".html", ".js", ".css", ".sql"}:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
            if deva_re.search(content):
                devanagari_hits.append(str(fpath.relative_to(src_root.parent)))
    assert devanagari_hits == [], f"Unexpected Devanagari text remaining in src/: {devanagari_hits}"

    shop_resp, _shop_dir = _setup_isolated_clean_shop(
        monkeypatch,
        tmp_path / "c2c_ui",
        username="admin_c2c",
        password="EnglishOnly#123",
        full_name="Sandeep Deshmukh",
        shop_name="Green Valley Agri Inputs",
        mobile="9822334455",
        address="Plot 8, APMC Road",
        city="Satara",
        gstin="27AAAAA5555A1Z5",
        upi_id="",
        email="sales@greenvalley.in",
    )
    headers = {"Authorization": f"Bearer {shop_resp['token']}"}

    with TestClient(app) as client:
        html = client.get("/").text
        js = client.get("/static/js/app.js").text
        css = client.get("/static/css/app.css").text

        for asset_name, text_body in (("index.html", html), ("app.js", js), ("app.css", css)):
            assert not deva_re.search(text_body), f"Devanagari found in {asset_name}"

        assert "KRUSHIDHAN" in html
        assert "Customer Signature" in html
        assert "Authorized Signatory" in html
        assert "Tax Invoice" in html
        assert "Farmer Status &amp; Ledger" in html
        assert "Supplier Ledger" in html
        assert "Zero Rupees Only" in js
        assert "Noto Sans Devanagari" not in css

        for forbidden_label in ("(कर बीजक)", "(ग्राहकाची सही)", "शून्य रुपये", "मो.:", "Udhar", "Farmer Khata", "Supplier Khata"):
            assert forbidden_label not in html, f"Forbidden label '{forbidden_label}' in index.html"
            assert forbidden_label not in js, f"Forbidden label '{forbidden_label}' in app.js"

        # Clean seeded master categories, crops, customer groups & expense categories are English-only
        masters = client.get("/api/masters/all", headers=headers).json()
        exp_cats = client.get("/api/accounting/expense-categories", headers=headers).json()
        cust_groups = get_db_manager().fetch_all("SELECT group_name FROM customer_groups;")
        all_names = (
            [c["category_name"] for c in masters["categories"]]
            + [c["crop_name"] for c in masters["crops"]]
            + [g["group_name"] for g in cust_groups]
            + [e["category_name"] for e in exp_cats]
        )
        assert any("Chemical Fertilizers" in n for n in all_names)
        assert any("Retail Farmer" in n for n in all_names)
        assert any("Shop Rent" in n for n in all_names)
        assert all(not deva_re.search(n) for n in all_names)

    reset_db_manager()


def test_c2c_pdf_font_routing_rupee_glyph_long_names_multipage_and_calculations(tmp_path, monkeypatch):
    """
    C2-C Task 2, 3, 5 & 6:
    - Generates an authenticated 42-item multi-page GST invoice with very long shop/customer names,
      special XML chars (&, <, >, ", '), discounts, CGST, SGST, extra charges, and round-off.
    - Verifies English text uses Helvetica/Helvetica-Bold ONLY and ToUnicode subset font renders ONLY '₹'.
    - Verifies multi-page table breaks cleanly with repeating headers and zero unchanged GST/ledger math drift.
    """
    import re

    shop_resp, _shop_dir = _setup_isolated_clean_shop(
        monkeypatch,
        tmp_path / "c2c_pdf",
        username="admin_c2c_pdf",
        password="PdfTestPassword#9",
        full_name="Pandurang Kulkarni-Deshpande",
        shop_name='Green Valley & Sons <Agri Inputs> Cooperative Supply Kendra "Unit #1"',
        mobile="9822001122",
        address="Gate 412/B, Opposite Agricultural Produce Market Committee Yard",
        city="Ahmednagar",
        gstin="27CCCCC3333C1Z3",
        upi_id="",
        email="accounts@greenvalleyagro.in",
    )
    headers = {"Authorization": f"Bearer {shop_resp['token']}"}
    today_str = date.today().isoformat()

    with TestClient(app) as client:
        cust = client.post(
            "/api/masters/customers",
            json={
                "customer_name": 'Kisanrao Jagtap & Family Orchard Group <East Wing> "Plot 4"',
                "mobile": "9765432100",
                "village": "Rahata",
                "taluka": "Shrirampur",
                "district": "Ahilyanagar",
                "gstin": "27XYZAB1234C1Z9",
                "opening_balance": 0.0,
            },
            headers=headers,
        ).json()

        prod = client.post(
            "/api/masters/products",
            json={
                "product_name": 'Chelated Nutrient Mix <Zn+B> "Super Booster"',
                "category_id": 1,
                "manufacturer_id": 1,
                "hsn_code": "38249990",
                "unit_id": 1,
                "tax_group_id": 4,
                "default_purchase_rate": 90.0,
                "default_sale_rate": 118.0,
                "default_mrp": 125.0,
                "is_active": 1,
            },
            headers=headers,
        ).json()

        stk = client.post(
            "/api/inventory/add-stock",
            json={
                "product_id": prod["product_id"],
                "qty": 100.0,
                "batch_no": "BATCH-C2C-42",
                "purchase_rate": 90.0,
                "sale_rate": 118.0,
                "mrp": 125.0,
                "exp_date": "2028-12-31",
            },
            headers=headers,
        ).json()

        # Verify backend GST calculation logic remains completely unchanged
        calc_res = client.post(
            "/api/sales/calculate-tax",
            json={
                "qty": 1.0,
                "rate": 118.0,
                "discount_percent": 0.0,
                "cgst_rate": 9.0,
                "sgst_rate": 9.0,
                "igst_rate": 0.0,
                "is_rate_tax_inclusive": True,
            },
            headers=headers,
        )
        assert calc_res.status_code == 200
        calc = calc_res.json()
        assert calc["total_amount"] == pytest.approx(118.0)
        assert calc["cgst_amount"] == pytest.approx(9.0)
        assert calc["sgst_amount"] == pytest.approx(9.0)

        # Create 42-item invoice to force multi-page A4 output with repeating table headers
        items_payload = [
            {
                "product_id": prod["product_id"],
                "batch_id": stk["batch_id"],
                "qty": 1.0,
                "sale_rate": 118.0,
                "mrp": 125.0,
                "discount_percent": 0.0,
                "discount_amount": 0.0,
                "taxable_amount": 100.0,
                "cgst_rate": 9.0,
                "cgst_amount": 9.0,
                "sgst_rate": 9.0,
                "sgst_amount": 9.0,
                "igst_rate": 0.0,
                "igst_amount": 0.0,
                "total_amount": 118.0,
            }
            for _ in range(42)
        ]

        sale_res = client.post(
            "/api/sales/create",
            json={
                "invoice_no": "INV-C2C-0042",
                "sale_date": today_str,
                "customer_id": cust["customer_id"],
                "payment_mode": "CASH",
                "total_taxable": 4200.0,
                "total_cgst": 378.0,
                "total_sgst": 378.0,
                "total_igst": 0.0,
                "total_discount": 0.0,
                "extra_charges": 44.0,
                "round_off": 0.0,
                "net_amount": 5000.0,
                "paid_amount": 3500.0,
                "due_amount": 1500.0,
                "items": items_payload,
            },
            headers=headers,
        )
        assert sale_res.status_code == 200, sale_res.text
        sale_id = sale_res.json()["sale_id"]

        assert client.get(f"/api/sales/{sale_id}/pdf").status_code == 401
        pdf_res = client.get(f"/api/sales/{sale_id}/pdf", headers=headers)
        assert pdf_res.status_code == 200
        pdf_bytes = pdf_res.content
        assert pdf_bytes.startswith(b"%PDF-")

        page_count = len(re.findall(rb"/Type\s*/Page\b", pdf_bytes))
        assert page_count >= 2, f"Expected multi-page PDF (>=2 pages), got {page_count}"

        fonts, full_text, tounicode_runs = _inspect_pdf_font_usage(pdf_bytes)
        assert "Helvetica" in fonts
        assert "Helvetica-Bold" in fonts
        assert len(tounicode_runs) >= 5
        # Every run emitted via the bundled TrueType font must be strictly ₹ glyph(s)
        assert all(set(run) == {"₹"} for run in tounicode_runs if run), (
            f"TrueType Rupee font was unexpectedly used for non-rupee text: {tounicode_runs}"
        )

        # Repeating table column header on page 2+
        assert full_text.count("Product Description") >= 2
        assert "\x7f" not in full_text
        assert not any("\u0900" <= ch <= "\u097f" for ch in full_text)

        for tok in (
            "TAX INVOICE",
            "Green Valley & Sons <Agri Inputs>",
            'Cooperative Supply Kendra "Unit #1"',
            "Pandurang Kulkarni-Deshpande",
            'Kisanrao Jagtap & Family Orchard Group <East Wing> "Plot 4"',
            "Rahata, Shrirampur, Ahilyanagar",
            "27XYZAB1234C1Z9",
            "PARTIAL",
            "₹ 4,956.00",   # Subtotal
            "₹ 4,200.00",   # Taxable
            "₹ 378.00",     # CGST / SGST
            "₹ 44.00",      # Extra Charges
            "₹ 5,000.00",   # Grand Total
            "₹ 3,500.00",   # Paid Amount
            "₹ 1,500.00",   # Balance Due
            "RUPEES FIVE THOUSAND ONLY",
            "Customer Signature",
            "Authorized Signatory",
            "Subject to Ahmednagar Jurisdiction",
        ):
            assert tok in full_text, f"Missing '{tok}' in C2-C multi-page PDF"

    reset_db_manager()


# =====================================================================
# C2-D: Browser Workflows, A4 Print Layout & PDF Download Regression Tests
# =====================================================================
def test_c2d_frontend_print_scoping_table_fit_and_pos_races(tmp_path, monkeypatch):
    """
    C2-D UI/CSS/JS Regression Verification:
    - Responsive mobile media queries are scoped to '@media screen' so A4 portrait (~733px)
      never triggers mobile rules during printing.
    - Explicit print target classes ('print-target-invoice', 'print-target-farmer',
      'print-target-pnl', 'print-target-statutory') and '.print-only-header' banners exist.
    - Farmer statement, P&L, Statutory Register (11 cols), and GST Invoice (13 cols) define
      A4 portrait table fit and no-mid-number wrap rules.
    - Inline SVG favicon prevents 404 console errors; Farmer Statement Print button exists.
    - POS batch fetch race guard ('posBatchFetchSeq' + 'data-productid'), Credit payment default
      zeroing ('handlePosPayModeChange'), resetPosCart reset, and auth session race guards
      ('authSessionVersion', 'stopAutoSyncPolling') remain intact.
    """
    import re

    reset_db_manager()
    local_app = tmp_path / "c2d_ui"
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    monkeypatch.setenv("APP_MODE", "desktop")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))

    with TestClient(app) as client:
        html = client.get("/").text
        js = client.get("/static/js/app.js").text
        css = client.get("/static/css/app.css").text

        # HTML controls, print banners, favicon & event bindings
        required_html = (
            'rel="icon" href="data:image/svg+xml',
            'id="pos-pay-mode" onchange="handlePosPayModeChange()"',
            'id="pnl-print-shop-header" class="print-only-header"',
            'onclick="printPnLReport()"',
            'id="stat-print-shop-header" class="print-only-header"',
            'onclick="printStatutoryRegister()"',
            'id="fd-print-shop-header" class="print-only-header"',
            'id="btn-print-farmer-statement"',
            'onclick="printFarmerStatement()"',
            '<th class="text-center no-print">Invoice PDF</th>',
        )
        for token in required_html:
            assert token in html, f"Missing expected C2-D HTML marker: {token}"

        # CSS media scoping & A4 print layout rules
        assert "@media screen and (max-width: 900px)" in css
        assert "@media screen and (max-width: 768px)" in css
        assert not re.search(r"@media\s*\(max-width:\s*(?:900|768)px\)", css), (
            "Bare @media (max-width: 900px/768px) must stay scoped to '@media screen'"
        )
        required_css = (
            "size: A4 portrait;",
            "body.print-target-invoice #modal-invoice-preview",
            "body.print-target-farmer #modal-farmer-detail",
            "body.print-target-pnl #tab-pnl",
            "body.print-target-statutory #tab-statutory",
            ".print-only-header {",
            "#modal-farmer-detail #fd-invoices-tbody tr td:nth-child(1)",
            "#tab-pnl td.text-right",
            "#tab-statutory table {",
            "#tab-statutory th:nth-child(10) { width: 8%; }",
            "#tab-statutory th:nth-child(11) { width: 8%; }",
            ".inv-table {\n    width: 100% !important;\n    table-layout: fixed;",
            ".inv-table th:nth-child(13)",
        )
        for token in required_css:
            assert token in css, f"Missing expected C2-D CSS print rule: {token}"

        # JS POS calculation, batch race protection, auth race hygiene, print & PDF download logic
        required_js = (
            "function calculatePosLineTax(",
            "function handlePosPayModeChange(",
            "let posBatchFetchSeq = 0;",
            'data-productid="${prodId}"',
            "batchProdId && batchProdId !== prodId",
            'payModeEl.value = "CASH";',
            "let authSessionVersion = 0;",
            "function hasValidAuthSession()",
            "function stopAutoSyncPolling()",
            "function printSection(",
            "function printInvoicePreview()",
            "function printFarmerStatement()",
            "function printPnLReport()",
            "function printStatutoryRegister()",
            'window.addEventListener("beforeprint"',
            'window.addEventListener("afterprint", clearPrintTargetClasses)',
            'link.download = filename;',
        )
        for token in required_js:
            assert token in js, f"Missing expected C2-D JS construct: {token}"

        # Ensure downloadInvoicePdf never calls window.open (uses Blob + <a download>)
        dl_start = js.index("async function downloadInvoicePdf(")
        dl_end = js.index("/* ==========================================", dl_start)
        dl_body = js[dl_start:dl_end]
        assert "window.open" not in dl_body
        assert "URL.createObjectURL(blob)" in dl_body
        assert "Content-Disposition" in dl_body

    reset_db_manager()


def test_c2d_billing_pdf_download_headers_unauth_and_credit_balances(tmp_path, monkeypatch):
    """
    C2-D End-to-End Billing, Stock, Farmer Ledger, IGST/CGST Math & PDF Download Verification:
    - Creates Seed (0% GST), Fertilizer (5% CGST+SGST), and Pesticide (18% CGST+SGST / IGST) products.
    - Executes multi-item discounted Cash invoice + Credit invoice and verifies exact stock deduction,
      farmer outstanding balance, Farmer Statement particulars, P&L & FCO register totals.
    - Verifies GET /api/sales/{id}/pdf rejects unauthenticated requests with 401 and serves
      authenticated PDFs with 'Content-Disposition: attachment; filename="Invoice_..."'.
    """
    from src.services.sales_service import SalesService

    shop_resp, _shop_dir = _setup_isolated_clean_shop(
        monkeypatch,
        tmp_path / "c2d_e2e",
        username="admin_c2d",
        password="BrowserPass#2026",
        full_name="Suresh Patil",
        shop_name="Nirmal Krishi Seva Kendra",
        mobile="9822445566",
        address="Shop 14, Market Yard",
        city="Baramati",
        gstin="27AABCN1234K1Z8",
        upi_id="",
        email="nirmalagro@example.com",
    )
    headers = {"Authorization": f"Bearer {shop_resp['token']}"}
    today_str = date.today().isoformat()

    with TestClient(app) as client:
        cust = client.post(
            "/api/masters/customers",
            json={
                "customer_name": "Rameshwar Deshmukh",
                "mobile": "9823456789",
                "village": "Nimgaon Ketki",
                "opening_balance": 0.0,
            },
            headers=headers,
        ).json()

        # Seed (0%), Fertilizer (5%), Pesticide (18%)
        def _mk_prod(name, cat_id, hsn, tax_id, pur_rate, sale_rate, mrp, batch_no, qty, exp_date):
            p = client.post(
                "/api/masters/products",
                json={
                    "product_name": name,
                    "category_id": cat_id,
                    "manufacturer_id": 1,
                    "hsn_code": hsn,
                    "unit_id": 1,
                    "tax_group_id": tax_id,
                    "default_purchase_rate": pur_rate,
                    "default_sale_rate": sale_rate,
                    "default_mrp": mrp,
                    "is_active": 1,
                },
                headers=headers,
            ).json()
            s = client.post(
                "/api/inventory/add-stock",
                json={
                    "product_id": p["product_id"],
                    "qty": qty,
                    "batch_no": batch_no,
                    "purchase_rate": pur_rate,
                    "sale_rate": sale_rate,
                    "mrp": mrp,
                    "exp_date": exp_date,
                },
                headers=headers,
            ).json()
            return p["product_id"], s["batch_id"]

        seed_id, seed_batch = _mk_prod("Maize Seed MH-44", 3, "10051000", 1, 700.0, 850.0, 950.0, "SEED-26", 25.0, "2028-11-30")
        fert_id, fert_batch = _mk_prod("Nano Urea NPK 50kg", 1, "31051000", 2, 980.0, 1200.0, 1300.0, "NPK-26", 20.0, "2028-02-01")
        pest_id, pest_batch = _mk_prod("Ampligo 500ml", 2, "38089199", 4, 450.0, 590.0, 650.0, "AMP-26", 15.0, "2027-09-30")

        svc = SalesService(get_db_manager())
        # Verify tax math for discounted lines & IGST inter-state calculation
        t_seed = svc.calculate_item_tax(2.0, 850.0, 10.0, 0.0, 0.0, 0.0, True)
        t_fert = svc.calculate_item_tax(2.0, 1200.0, 0.0, 2.5, 2.5, 0.0, True)
        t_pest = svc.calculate_item_tax(1.0, 590.0, 5.0, 9.0, 9.0, 0.0, True)
        t_igst = svc.calculate_item_tax(2.0, 590.0, 0.0, 0.0, 0.0, 18.0, True)
        assert t_seed["total_amount"] == pytest.approx(1530.0)
        assert t_fert["total_amount"] == pytest.approx(2399.99)
        assert t_pest["total_amount"] == pytest.approx(560.50)
        assert t_igst["igst_amount"] == pytest.approx(180.0)
        assert t_igst["cgst_amount"] == 0.0 and t_igst["sgst_amount"] == 0.0

        gross_cash = round(t_seed["total_amount"] + t_fert["total_amount"] + t_pest["total_amount"], 2)
        net_cash = float(round(gross_cash))
        round_off = round(net_cash - gross_cash, 2)
        assert net_cash == pytest.approx(4490.0)

        # 1. Multi-item discounted Cash invoice (special char '/' tests filename sanitization)
        cash_res = client.post(
            "/api/sales/create",
            json={
                "invoice_no": "INV/2026/001",
                "sale_date": today_str,
                "customer_id": cust["customer_id"],
                "payment_mode": "CASH",
                "total_taxable": round(t_seed["taxable_amount"] + t_fert["taxable_amount"] + t_pest["taxable_amount"], 2),
                "total_cgst": round(t_seed["cgst_amount"] + t_fert["cgst_amount"] + t_pest["cgst_amount"], 2),
                "total_sgst": round(t_seed["sgst_amount"] + t_fert["sgst_amount"] + t_pest["sgst_amount"], 2),
                "total_discount": round(t_seed["discount_amount"] + t_fert["discount_amount"] + t_pest["discount_amount"], 2),
                "round_off": round_off,
                "net_amount": net_cash,
                "paid_amount": net_cash,
                "due_amount": 0.0,
                "items": [
                    {"product_id": seed_id, "batch_id": seed_batch, "qty": 2.0, "sale_rate": 850.0, "mrp": 950.0, **t_seed},
                    {"product_id": fert_id, "batch_id": fert_batch, "qty": 2.0, "sale_rate": 1200.0, "mrp": 1300.0, **t_fert},
                    {"product_id": pest_id, "batch_id": pest_batch, "qty": 1.0, "sale_rate": 590.0, "mrp": 650.0, **t_pest},
                ],
            },
            headers=headers,
        )
        assert cash_res.status_code == 200, cash_res.text
        cash_sale_id = cash_res.json()["sale_id"]

        # 2. Credit invoice (4 PKT Maize Seed -> ₹3400.00 unpaid)
        t_cred = svc.calculate_item_tax(4.0, 850.0, 0.0, 0.0, 0.0, 0.0, True)
        cred_res = client.post(
            "/api/sales/create",
            json={
                "invoice_no": "INV-2026-002",
                "sale_date": today_str,
                "customer_id": cust["customer_id"],
                "payment_mode": "CREDIT",
                "total_taxable": t_cred["taxable_amount"],
                "net_amount": 3400.0,
                "paid_amount": 0.0,
                "due_amount": 3400.0,
                "items": [{"product_id": seed_id, "batch_id": seed_batch, "qty": 4.0, "sale_rate": 850.0, "mrp": 950.0, **t_cred}],
            },
            headers=headers,
        )
        assert cred_res.status_code == 200, cred_res.text

        # Verify exact stock deduction: Seed 25->19, Fert 20->18, Pest 15->14
        stock = {row["product_id"]: row["total_stock"] for row in client.get("/api/masters/products", headers=headers).json()}
        assert stock[seed_id] == pytest.approx(19.0)
        assert stock[fert_id] == pytest.approx(18.0)
        assert stock[pest_id] == pytest.approx(14.0)

        # Verify Farmer Statement matches total purchases (7890) and credit due (3400)
        stmt = client.get(f"/api/masters/farmers/{cust['customer_id']}/statement", headers=headers)
        assert stmt.status_code == 200
        stmt_data = stmt.json()
        assert stmt_data["customer"]["current_balance"] == pytest.approx(3400.0)
        assert sum(inv["net_amount"] for inv in stmt_data["invoices"]) == pytest.approx(7890.0)

        # Verify Statutory FCO Fertilizer Register lists INV/2026/001 fertilizer row
        fco = client.get(f"/api/statutory/fertilizer-register?from_date={today_str}&to_date={today_str}", headers=headers)
        assert fco.status_code == 200
        assert any(r["invoice_no"] == "INV/2026/001" and r["qty"] == pytest.approx(2.0) for r in fco.json())

        # Verify unauthenticated PDF request blocked (401) & authenticated download sets clean attachment filename
        assert client.get(f"/api/sales/{cash_sale_id}/pdf").status_code == 401
        pdf_res = client.get(f"/api/sales/{cash_sale_id}/pdf", headers=headers)
        assert pdf_res.status_code == 200
        assert pdf_res.content.startswith(b"%PDF-")
        assert pdf_res.headers.get("content-disposition") == 'attachment; filename="Invoice_INV_2026_001.pdf"'

    reset_db_manager()
