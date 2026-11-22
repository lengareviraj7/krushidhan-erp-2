"""
Excel Import Service for Krushidhan Agri-Input Shop ERP (Stages C3-C1, C3-C2 & C3-C3).

Implements the secure Product Master, Customer Master, and Supplier Master Excel import workflows
reusing C3-B1 security and workbook builders (`excel_security.py`, `excel_workbook_builder.py`):
1. Import Templates (.xlsx) — `Product Import` (C3-C1), `Customer Import` (C3-C2), and `Supplier Import` (C3-C3)
2. Secure Preview Endpoint Logic — zero DB writes, row-by-row validation & duplicate checks
3. Thread-Safe Short-Lived Single-Use Preview Token Store (<= 15 min, max 20 active records)
4. Confirmed Atomic Import Commit — revalidates duplicates & master FKs inside `BEGIN IMMEDIATE`

Never imports stock quantities, batch quantities, opening/current balances, purchase cost rates,
profit margins, Aadhaar numbers, PAN, bank account/IFSC, UPI IDs, credit limits, ledger/payment history,
passwords, or secrets.
Never overwrites existing master records automatically.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from src.db.connection import DatabaseManager, DatabaseMaintenanceError, get_db_manager
from src.models.master_data import Customer, Product, Supplier
from src.repositories.master_data_repository import MasterDataRepository
from src.services.excel_security import (
    MAX_CELL_LENGTH,
    MAX_COLUMNS,
    MAX_DATA_ROWS,
    ExcelSecurityError,
    ExcelValidationError,
    assert_headers_allowed_for_import,
    read_xlsx_sheet_safely,
    safe_xlsx_workbook,
    sanitize_excel_text,
    validate_decimal_amount,
    validate_email,
    validate_gstin,
    validate_hsn,
    validate_indian_mobile,
    validate_quantity,
)
from src.services.excel_workbook_builder import (
    BODY_FONT,
    CELL_BORDER,
    ZEBRA_ALT_FILL,
    add_company_header,
    add_report_banner,
    add_sheet,
    apply_freeze_panes,
    auto_fit_columns,
    create_workbook,
    save_workbook_to_bytes,
    write_currency_cell,
    write_quantity_cell,
    write_table_header,
    write_text_cell,
)

IMPORT_KIND_PRODUCTS = "products"
IMPORT_KIND_CUSTOMERS = "customers"
IMPORT_KIND_SUPPLIERS = "suppliers"

PRODUCT_IMPORT_SHEET_NAME = "Product Import"
PRODUCT_IMPORT_TEMPLATE_FILENAME = "Product_Import_Template.xlsx"
TEMPLATE_SAMPLE_PRODUCT_NAME = "[SAMPLE EXAMPLE - DO NOT IMPORT] Urea 45 Kg Bag"
TEMPLATE_SAMPLE_PREFIX = "[sample example - do not import]"

CUSTOMER_IMPORT_SHEET_NAME = "Customer Import"
CUSTOMER_IMPORT_TEMPLATE_FILENAME = "Customer_Import_Template.xlsx"
TEMPLATE_SAMPLE_CUSTOMER_NAME = "[SAMPLE EXAMPLE - DO NOT IMPORT] Ramesh Baburao Patil"

SUPPLIER_IMPORT_SHEET_NAME = "Supplier Import"
SUPPLIER_IMPORT_TEMPLATE_FILENAME = "Supplier_Import_Template.xlsx"
TEMPLATE_SAMPLE_SUPPLIER_NAME = "[SAMPLE EXAMPLE - DO NOT IMPORT] Sahyadri Agro Distributors Pvt Ltd"

PREVIEW_TOKEN_TTL_SECONDS = 15 * 60  # Max 15 minutes
MAX_ACTIVE_PREVIEW_RECORDS = 20

ROW_STATUS_READY = "Ready to Import"
ROW_STATUS_DUP_DB = "Duplicate — Existing Product"
ROW_STATUS_CUSTOMER_DUP_DB = "Duplicate — Existing Customer"
ROW_STATUS_SUPPLIER_DUP_DB = "Duplicate — Existing Supplier"
ROW_STATUS_DUP_FILE = "Duplicate — Repeated in File"
ROW_STATUS_INVALID_REQUIRED = "Invalid — Missing Required Field"
ROW_STATUS_INVALID_VALIDATION = "Invalid — Validation Error"

MAX_PRODUCT_NAME_LENGTH = 120
MAX_CUSTOMER_NAME_LENGTH = 120
MAX_CUSTOMER_ADDRESS_PART_LENGTH = 100
MAX_SUPPLIER_NAME_LENGTH = 120
MAX_SUPPLIER_CONTACT_LENGTH = 120
MAX_SUPPLIER_EMAIL_LENGTH = 150
MAX_SUPPLIER_ADDRESS_LENGTH = 300
MAX_SUPPLIER_CITY_LENGTH = 100
MAX_SUPPLIER_LICENCE_LENGTH = 100

_TEMPLATE_HEADERS: Tuple[str, ...] = (
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
)

_CANONICAL_HEADER_ALIASES: Dict[str, str] = {
    "product_name": "product_name",
    "name": "product_name",
    "item_name": "product_name",
    "sku_name": "product_name",
    "category": "category",
    "category_name": "category",
    "manufacturer": "manufacturer",
    "manufacturer_brand": "manufacturer",
    "brand": "manufacturer",
    "company": "manufacturer",
    "manufacturer_name": "manufacturer",
    "unit": "unit",
    "unit_name": "unit",
    "unit_packing": "unit",
    "uom": "unit",
    "hsn_code": "hsn_code",
    "hsn": "hsn_code",
    "tax_group": "tax_group",
    "tax_group_name": "tax_group",
    "tax_group_id": "tax_group",
    "gst_tax_group": "tax_group",
    "gst_group": "tax_group",
    "sale_rate": "sale_rate",
    "sale_rate_inr": "sale_rate",
    "default_sale_rate": "sale_rate",
    "selling_rate": "sale_rate",
    "rate": "sale_rate",
    "mrp": "mrp",
    "mrp_inr": "mrp",
    "default_mrp": "mrp",
    "min_stock_alert": "min_stock_alert",
    "minimum_stock_alert": "min_stock_alert",
    "min_alert": "min_stock_alert",
    "reorder_level": "min_stock_alert",
    "target_crop": "target_crop",
    "crop": "target_crop",
    "crop_name": "target_crop",
}

PRODUCT_FORBIDDEN_HEADERS = frozenset(
    {
        "opening_stock",
        "current_stock",
        "total_stock",
        "stock_qty",
        "stock",
        "qty",
        "quantity",
        "qty_on_hand",
        "batch_no",
        "batch_id",
        "batch_qty",
        "batch_quantities",
        "batch_quantity",
        "mfg_date",
        "exp_date",
        "expiry_date",
        "default_purchase_rate",
        "purchase_rate",
        "purchase_cost",
        "purchase_price",
        "cost_price",
        "cost",
        "profit_margin",
        "margin",
        "margin_percent",
        "opening_balance",
        "current_balance",
        "credit_limit",
        "password",
        "password_hash",
        "jwt_secret",
        "upi_id",
        "aadhar_no",
        "aadhaar",
        "aadhaar_no",
        "account_no",
        "ifsc_code",
        "pan",
        "product_id",
        "sale_id",
        "purchase_id",
        "voucher_id",
        "setting_id",
    }
)

_CUSTOMER_TEMPLATE_HEADERS: Tuple[str, ...] = (
    "Customer Name *",
    "Mobile",
    "Village",
    "Taluka",
    "District",
    "State",
    "GSTIN",
    "Customer Group",
)

_CUSTOMER_CANONICAL_HEADER_ALIASES: Dict[str, str] = {
    "customer_name": "customer_name",
    "farmer_name": "customer_name",
    "party_name": "customer_name",
    "name": "customer_name",
    "mobile": "mobile",
    "mobile_no": "mobile",
    "phone": "mobile",
    "contact_no": "mobile",
    "village": "village",
    "village_town": "village",
    "city_village": "village",
    "taluka": "taluka",
    "tehsil": "taluka",
    "block": "taluka",
    "district": "district",
    "state": "state",
    "gstin": "gstin",
    "gst_no": "gstin",
    "gstin_no": "gstin",
    "customer_group": "customer_group",
    "group": "customer_group",
    "group_name": "customer_group",
    "group_id": "customer_group",
}

CUSTOMER_FORBIDDEN_HEADERS = frozenset(
    {
        "customer_id",
        "id",
        "aadhar_no",
        "aadhaar",
        "aadhaar_no",
        "aadhar",
        "masked_aadhaar",
        "opening_balance",
        "current_balance",
        "balance",
        "pending_credit",
        "credit_balance",
        "due_amount",
        "total_due",
        "outstanding",
        "outstanding_balance",
        "credit_limit",
        "ledger_transactions",
        "ledger",
        "transactions",
        "payment_history",
        "payments",
        "receipts",
        "password",
        "password_hash",
        "jwt_secret",
        "token",
        "secret",
        "upi_id",
        "pan",
        "account_no",
        "bank_name",
        "ifsc_code",
        "sale_id",
        "purchase_id",
        "voucher_id",
        "setting_id",
    }
)

_SUPPLIER_TEMPLATE_HEADERS: Tuple[str, ...] = (
    "Supplier Name *",
    "Contact Person",
    "Mobile",
    "Email",
    "Address",
    "City",
    "State",
    "GSTIN",
    "Licence No",
)

_SUPPLIER_CANONICAL_HEADER_ALIASES: Dict[str, str] = {
    "supplier_name": "supplier_name",
    "company_name": "supplier_name",
    "company_supplier_name": "supplier_name",
    "distributor_name": "supplier_name",
    "vendor_name": "supplier_name",
    "party_name": "supplier_name",
    "name": "supplier_name",
    "contact_person": "contact_person",
    "contact_name": "contact_person",
    "representative": "contact_person",
    "mobile": "mobile",
    "mobile_no": "mobile",
    "phone": "mobile",
    "phone_no": "mobile",
    "contact_no": "mobile",
    "email": "email",
    "email_id": "email",
    "email_address": "email",
    "address": "address",
    "street_address": "address",
    "office_address": "address",
    "city": "city",
    "city_location": "city",
    "town": "city",
    "state": "state",
    "gstin": "gstin",
    "gst_no": "gstin",
    "gstin_no": "gstin",
    "licence_no": "dl_number",
    "license_no": "dl_number",
    "dl_number": "dl_number",
    "dl_no": "dl_number",
    "licence_number": "dl_number",
    "license_number": "dl_number",
    "drug_licence_no": "dl_number",
    "drug_license_no": "dl_number",
    "agri_licence_no": "dl_number",
    "agri_license_no": "dl_number",
}

SUPPLIER_FORBIDDEN_HEADERS = frozenset(
    {
        "supplier_id",
        "vendor_id",
        "distributor_id",
        "id",
        "pan",
        "pan_no",
        "pan_number",
        "pan_card",
        "bank_name",
        "bank",
        "bank_details",
        "bank_account",
        "bank_account_no",
        "bank_account_number",
        "account_no",
        "account_number",
        "ifsc",
        "ifsc_code",
        "bank_ifsc",
        "upi_id",
        "upi",
        "opening_balance",
        "current_balance",
        "balance",
        "outstanding",
        "outstanding_amount",
        "outstanding_payable",
        "outstanding_balance",
        "due_amount",
        "total_due",
        "payable_balance",
        "payment_history",
        "payments",
        "ledger",
        "ledger_transactions",
        "transactions",
        "bills_count",
        "total_purchases",
        "credit_limit",
        "password",
        "password_hash",
        "jwt_secret",
        "token",
        "secret",
        "aadhar_no",
        "aadhaar",
        "aadhaar_no",
        "sale_id",
        "purchase_id",
        "voucher_id",
        "setting_id",
        "is_active",
        "created_at",
    }
)

_CANONICAL_INDIAN_STATES: Tuple[str, ...] = (
    "Andhra Pradesh",
    "Arunachal Pradesh",
    "Assam",
    "Bihar",
    "Chhattisgarh",
    "Goa",
    "Gujarat",
    "Haryana",
    "Himachal Pradesh",
    "Jharkhand",
    "Karnataka",
    "Kerala",
    "Madhya Pradesh",
    "Maharashtra",
    "Manipur",
    "Meghalaya",
    "Mizoram",
    "Nagaland",
    "Odisha",
    "Punjab",
    "Rajasthan",
    "Sikkim",
    "Tamil Nadu",
    "Telangana",
    "Tripura",
    "Uttar Pradesh",
    "Uttarakhand",
    "West Bengal",
    "Andaman and Nicobar Islands",
    "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi",
    "Jammu and Kashmir",
    "Ladakh",
    "Lakshadweep",
    "Puducherry",
)

_INDIAN_STATE_LOOKUP: Dict[str, str] = {
    s.lower(): s for s in _CANONICAL_INDIAN_STATES
}
_INDIAN_STATE_LOOKUP.update(
    {
        "orissa": "Odisha",
        "uttaranchal": "Uttarakhand",
        "pondicherry": "Puducherry",
        "nct of delhi": "Delhi",
        "national capital territory of delhi": "Delhi",
        "new delhi": "Delhi",
        "andaman & nicobar islands": "Andaman and Nicobar Islands",
        "andaman and nicobar": "Andaman and Nicobar Islands",
        "andaman & nicobar": "Andaman and Nicobar Islands",
        "jammu & kashmir": "Jammu and Kashmir",
        "dadra & nagar haveli and daman & diu": "Dadra and Nagar Haveli and Daman and Diu",
        "dadra and nagar haveli": "Dadra and Nagar Haveli and Daman and Diu",
        "dadra & nagar haveli": "Dadra and Nagar Haveli and Daman and Diu",
        "daman and diu": "Dadra and Nagar Haveli and Daman and Diu",
        "daman & diu": "Dadra and Nagar Haveli and Daman and Diu",
    }
)

_NOTE_FILL = PatternFill(fill_type="solid", fgColor="FFFBEB")
_NOTE_FONT = Font(name="Calibri", size=9.5, bold=False, color="78350F")
_NOTE_SIDE = Side(style="thin", color="D97706")
_NOTE_BORDER = Border(left=_NOTE_SIDE, right=_NOTE_SIDE, top=_NOTE_SIDE, bottom=_NOTE_SIDE)
_SAMPLE_FILL = PatternFill(fill_type="solid", fgColor="FEF3C7")
_SAMPLE_FONT = Font(name="Calibri", size=10, italic=True, color="92400E")
_ALIGN_CENTER = Alignment(horizontal="center", vertical="center")
_ALIGN_LEFT = Alignment(horizontal="left", vertical="center")
_HEX_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_NON_ALNUM_SEP_RE = re.compile(r"[^a-z0-9]+")
_PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")
_PREFIX_BEFORE_PAREN_RE = re.compile(r"^(.+?)\s*\([^)]*\)\s*$")


class PreviewTokenError(ValueError):
    """Raised when a preview token is missing, invalid, expired, reused, mismatched, or wrong-user."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = int(status_code)


class ExcelImportTransactionError(RuntimeError):
    """Raised when an atomic import commit transaction fails and rolls back."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _normalize_whitespace(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_lookup_key(value: str) -> str:
    return _normalize_whitespace(value).lower()


def _normalize_product_key(value: str) -> str:
    return _normalize_whitespace(value).lower()


def _normalize_customer_key(value: str) -> str:
    return _normalize_whitespace(value).lower()


def _normalize_supplier_key(value: str) -> str:
    return _normalize_whitespace(value).lower()


def _normalize_indian_state(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (bool, int, float)):
        return None
    cleaned = _normalize_whitespace(str(value)).lower()
    if not cleaned:
        return None
    normalized_amp = _normalize_whitespace(cleaned.replace("&", " and "))
    return _INDIAN_STATE_LOOKUP.get(cleaned) or _INDIAN_STATE_LOOKUP.get(normalized_amp)


def _normalize_header_token(raw_header: Any) -> str:
    text = str(raw_header or "").replace("*", "").replace("₹", "inr").strip().lower()
    cleaned = _NON_ALNUM_SEP_RE.sub("_", text).strip("_")
    return cleaned


def _display_english_label(name: str) -> str:
    """
    Strip trailing non-ASCII parenthetical annotations (e.g. legacy seed labels)
    while preserving standard ASCII labels like 'Exempted (0%)'.
    """
    raw = _normalize_whitespace(name)
    m = _PREFIX_BEFORE_PAREN_RE.match(raw)
    if m:
        inner = raw[len(m.group(1)):].strip()
        if any(ord(ch) > 127 for ch in inner):
            return _normalize_whitespace(m.group(1))
    return raw


def _db_identity_key(db: DatabaseManager) -> str:
    if str(db.db_path) == ":memory:":
        return ":memory:"
    return str(Path(db.db_path).resolve())


@dataclass(frozen=True)
class ValidatedProductCandidate:
    row_number: int
    product_name: str
    normalized_name: str
    category_id: Optional[int]
    category_label: str
    manufacturer_id: Optional[int]
    manufacturer_label: str
    unit_id: Optional[int]
    unit_label: str
    hsn_code: Optional[str]
    tax_group_id: int
    tax_group_label: str
    default_sale_rate: float
    default_mrp: float
    min_stock_alert: float
    crop_id: Optional[int]
    crop_label: str


@dataclass(frozen=True)
class ValidatedCustomerCandidate:
    row_number: int
    customer_name: str
    normalized_name: str
    mobile: Optional[str]
    village: Optional[str]
    taluka: Optional[str]
    district: Optional[str]
    state: str
    gstin: Optional[str]
    group_id: Optional[int]
    group_label: str


@dataclass(frozen=True)
class ValidatedSupplierCandidate:
    row_number: int
    supplier_name: str
    normalized_name: str
    contact_person: Optional[str]
    mobile: Optional[str]
    email: Optional[str]
    address: Optional[str]
    city: Optional[str]
    state: str
    gstin: Optional[str]
    dl_number: Optional[str]


@dataclass
class PreviewTokenRecord:
    token: str
    user_id: int
    username: str
    db_key: str
    file_sha256: str
    filename: str
    created_at: float
    expires_at: float
    ready_candidates: Tuple[Any, ...]
    preview_rows: Tuple[Dict[str, Any], ...]
    summary: Dict[str, int]
    sample_rows_skipped: int
    import_kind: str = IMPORT_KIND_PRODUCTS
    used: bool = False


class PreviewTokenStore:
    """
    Thread-safe bounded in-memory store for short-lived single-use import preview tokens.
    Enforces max 15-minute TTL, max 20 active records, stale eviction, user, kind & SHA-256 binding.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = PREVIEW_TOKEN_TTL_SECONDS,
        max_records: int = MAX_ACTIVE_PREVIEW_RECORDS,
        time_fn: Callable[[], float] = time.time,
    ):
        self._ttl_seconds = min(float(ttl_seconds), float(PREVIEW_TOKEN_TTL_SECONDS))
        self._max_records = max(1, int(max_records))
        self._time_fn = time_fn
        self._lock = threading.Lock()
        self._records: "OrderedDict[str, PreviewTokenRecord]" = OrderedDict()

    def _purge_expired_locked(self, now: float) -> int:
        expired_keys = [
            tok for tok, rec in self._records.items() if now >= rec.expires_at
        ]
        for tok in expired_keys:
            self._records.pop(tok, None)
        return len(expired_keys)

    def _enforce_capacity_locked(self) -> None:
        while len(self._records) >= self._max_records:
            used_keys = [tok for tok, rec in self._records.items() if rec.used]
            if used_keys:
                self._records.pop(used_keys[0], None)
            else:
                self._records.popitem(last=False)

    def purge_stale(self) -> int:
        now = self._time_fn()
        with self._lock:
            return self._purge_expired_locked(now)

    def active_count(self) -> int:
        now = self._time_fn()
        with self._lock:
            self._purge_expired_locked(now)
            return sum(1 for rec in self._records.values() if not rec.used)

    def total_cached_count(self) -> int:
        now = self._time_fn()
        with self._lock:
            self._purge_expired_locked(now)
            return len(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def create(
        self,
        *,
        user_id: int,
        username: str,
        db_key: str,
        file_sha256: str,
        filename: str,
        ready_candidates: Sequence[Any],
        preview_rows: Sequence[Mapping[str, Any]],
        summary: Mapping[str, int],
        sample_rows_skipped: int,
        import_kind: str = IMPORT_KIND_PRODUCTS,
    ) -> PreviewTokenRecord:
        now = self._time_fn()
        expires_at = now + self._ttl_seconds
        token = secrets.token_urlsafe(32)
        record = PreviewTokenRecord(
            token=token,
            user_id=int(user_id),
            username=str(username or "").strip().lower(),
            db_key=db_key,
            file_sha256=file_sha256.lower(),
            filename=str(filename or "upload.xlsx"),
            created_at=now,
            expires_at=expires_at,
            ready_candidates=tuple(ready_candidates),
            preview_rows=tuple(dict(r) for r in preview_rows),
            summary={k: int(v) for k, v in summary.items()},
            sample_rows_skipped=int(sample_rows_skipped),
            import_kind=str(import_kind or IMPORT_KIND_PRODUCTS),
            used=False,
        )
        with self._lock:
            self._purge_expired_locked(now)
            self._enforce_capacity_locked()
            self._records[token] = record
        return record

    def validate_and_consume(
        self,
        token: str,
        *,
        user_id: int,
        username: str,
        db_key: str,
        file_sha256: str,
        expected_kind: Optional[str] = None,
    ) -> PreviewTokenRecord:
        clean_token = str(token or "").strip()
        clean_sha = str(file_sha256 or "").strip().lower()
        if not clean_token:
            raise PreviewTokenError("Preview token is required.", status_code=400)
        if not _HEX_SHA256_RE.match(clean_sha):
            raise PreviewTokenError("Valid SHA-256 workbook digest is required.", status_code=400)

        now = self._time_fn()
        clean_username = str(username or "").strip().lower()

        with self._lock:
            self._purge_expired_locked(now)
            record = self._records.get(clean_token)
            if record is None or now >= record.expires_at:
                if clean_token in self._records:
                    self._records.pop(clean_token, None)
                raise PreviewTokenError(
                    "Preview token is invalid or has expired. Please preview the workbook again.",
                    status_code=400,
                )

            if expected_kind is not None and record.import_kind != expected_kind:
                raise PreviewTokenError(
                    "Preview token belongs to a different import workflow. Please run preview again on this screen.",
                    status_code=400,
                )

            if record.db_key != db_key:
                raise PreviewTokenError(
                    "Preview token is not valid for the current database context.",
                    status_code=400,
                )

            if record.user_id != int(user_id) or record.username != clean_username:
                raise PreviewTokenError(
                    "Access forbidden: this preview token belongs to a different administrator.",
                    status_code=403,
                )

            if record.used:
                raise PreviewTokenError(
                    "This preview token has already been used. Please run preview again before committing.",
                    status_code=400,
                )

            if not hmac.compare_digest(clean_sha, record.file_sha256):
                raise PreviewTokenError(
                    "Uploaded workbook SHA-256 verification failed. Please preview the workbook again.",
                    status_code=400,
                )

            # Atomically consume token before releasing lock to block replay / concurrent commit
            record.used = True
            return record


_DEFAULT_PREVIEW_TOKEN_STORE = PreviewTokenStore()


def get_preview_token_store() -> PreviewTokenStore:
    return _DEFAULT_PREVIEW_TOKEN_STORE


def reset_preview_token_store() -> None:
    _DEFAULT_PREVIEW_TOKEN_STORE.clear()


# ---------------------------------------------------------------------------
# Lookup Helpers (Pure Snapshot Resolution)
# ---------------------------------------------------------------------------
class _MasterLookups:
    """In-memory lookup index built from a single read-only SQLite snapshot."""

    def __init__(
        self,
        *,
        tax_groups: Sequence[Dict[str, Any]],
        categories: Sequence[Dict[str, Any]],
        manufacturers: Sequence[Dict[str, Any]],
        units: Sequence[Dict[str, Any]],
        crops: Sequence[Dict[str, Any]],
        existing_products: Sequence[Dict[str, Any]],
    ):
        self.tax_groups = list(tax_groups)
        self.categories = list(categories)
        self.manufacturers = list(manufacturers)
        self.units = list(units)
        self.crops = list(crops)

        # Active tax groups indexed by exact ID string, full name, and unique prefix before '('
        self._tax_by_id: Dict[int, Dict[str, Any]] = {}
        self._tax_by_name: Dict[str, Dict[str, Any]] = {}
        prefix_buckets: Dict[str, List[Dict[str, Any]]] = {}
        for tg in self.tax_groups:
            if int(tg.get("is_active") or 0) != 1:
                continue
            tid = int(tg["tax_group_id"])
            tname = _normalize_whitespace(tg["tax_group_name"])
            self._tax_by_id[tid] = tg
            self._tax_by_name[tname.lower()] = tg
            m = _PREFIX_BEFORE_PAREN_RE.match(tname)
            if m:
                prefix_key = _normalize_lookup_key(m.group(1))
                prefix_buckets.setdefault(prefix_key, []).append(tg)
        self._tax_unique_prefix: Dict[str, Dict[str, Any]] = {
            k: bucket[0] for k, bucket in prefix_buckets.items() if len(bucket) == 1
        }

        # Categories indexed by exact name, short_name, and clean English prefix
        self._cat_exact: Dict[str, Dict[str, Any]] = {}
        cat_eng_buckets: Dict[str, List[Dict[str, Any]]] = {}
        for c in self.categories:
            cname = _normalize_whitespace(c["category_name"])
            self._cat_exact[cname.lower()] = c
            short_n = _normalize_whitespace(c.get("short_name") or "")
            if short_n:
                self._cat_exact.setdefault(short_n.lower(), c)
            eng = _display_english_label(cname).lower()
            cat_eng_buckets.setdefault(eng, []).append(c)
        self._cat_eng: Dict[str, Dict[str, Any]] = {
            k: b[0] for k, b in cat_eng_buckets.items() if len(b) == 1
        }

        # Manufacturers indexed case-insensitively
        self._mfg_exact: Dict[str, Dict[str, Any]] = {}
        for m in self.manufacturers:
            mname = _normalize_whitespace(m["manufacturer_name"])
            self._mfg_exact[mname.lower()] = m

        # Units indexed by unit_name, symbol, and "unit_name (symbol)"
        self._unit_exact: Dict[str, Dict[str, Any]] = {}
        for u in self.units:
            uname = _normalize_whitespace(u["unit_name"])
            usym = _normalize_whitespace(u.get("symbol") or "")
            self._unit_exact[uname.lower()] = u
            if usym:
                self._unit_exact.setdefault(usym.lower(), u)
                self._unit_exact.setdefault(f"{uname} ({usym})".lower(), u)

        # Crops indexed by exact name and clean English label
        self._crop_exact: Dict[str, Dict[str, Any]] = {}
        crop_eng_buckets: Dict[str, List[Dict[str, Any]]] = {}
        for cr in self.crops:
            cr_name = _normalize_whitespace(cr["crop_name"])
            self._crop_exact[cr_name.lower()] = cr
            eng = _display_english_label(cr_name).lower()
            crop_eng_buckets.setdefault(eng, []).append(cr)
        self._crop_eng: Dict[str, Dict[str, Any]] = {
            k: b[0] for k, b in crop_eng_buckets.items() if len(b) == 1
        }

        # Existing products indexed by normalized product_name (both active & inactive)
        self.existing_products_by_key: Dict[str, Dict[str, Any]] = {}
        for p in existing_products:
            pkey = _normalize_product_key(p["product_name"])
            if pkey and pkey not in self.existing_products_by_key:
                self.existing_products_by_key[pkey] = p

    def resolve_tax_group(self, raw_val: Any) -> Optional[Dict[str, Any]]:
        if raw_val is None or isinstance(raw_val, bool):
            return None
        if isinstance(raw_val, int):
            return self._tax_by_id.get(raw_val)
        if isinstance(raw_val, float):
            if raw_val.is_integer():
                return self._tax_by_id.get(int(raw_val))
            return None
        text = _normalize_whitespace(str(raw_val))
        if not text:
            return None
        if text.isdigit():
            found_by_id = self._tax_by_id.get(int(text))
            if found_by_id:
                return found_by_id
        lowered = text.lower()
        return self._tax_by_name.get(lowered) or self._tax_unique_prefix.get(lowered)

    def resolve_category(self, raw_val: Any) -> Optional[Dict[str, Any]]:
        if raw_val is None or isinstance(raw_val, bool):
            return None
        text = _normalize_whitespace(str(raw_val))
        if not text:
            return None
        lowered = text.lower()
        return self._cat_exact.get(lowered) or self._cat_eng.get(lowered)

    def resolve_manufacturer(self, raw_val: Any) -> Optional[Dict[str, Any]]:
        if raw_val is None or isinstance(raw_val, bool):
            return None
        text = _normalize_whitespace(str(raw_val))
        if not text:
            return None
        return self._mfg_exact.get(text.lower())

    def resolve_unit(self, raw_val: Any) -> Optional[Dict[str, Any]]:
        if raw_val is None or isinstance(raw_val, bool):
            return None
        text = _normalize_whitespace(str(raw_val))
        if not text:
            return None
        return self._unit_exact.get(text.lower())

    def resolve_crop(self, raw_val: Any) -> Optional[Dict[str, Any]]:
        if raw_val is None or isinstance(raw_val, bool):
            return None
        text = _normalize_whitespace(str(raw_val))
        if not text:
            return None
        lowered = text.lower()
        return self._crop_exact.get(lowered) or self._crop_eng.get(lowered)


class _CustomerLookups:
    """In-memory lookup index for Customer Master import built from one read-only snapshot."""

    def __init__(
        self,
        *,
        customer_groups: Sequence[Dict[str, Any]],
        existing_customers: Sequence[Dict[str, Any]],
    ):
        self.customer_groups = list(customer_groups)
        self._group_by_id: Dict[int, Dict[str, Any]] = {}
        self._group_exact: Dict[str, Dict[str, Any]] = {}
        group_eng_buckets: Dict[str, List[Dict[str, Any]]] = {}

        for cg in self.customer_groups:
            if int(cg.get("is_active") if cg.get("is_active") is not None else 1) != 1:
                continue
            gid = int(cg["group_id"])
            gname = _normalize_whitespace(cg["group_name"])
            self._group_by_id[gid] = cg
            self._group_exact[gname.lower()] = cg
            eng = _display_english_label(gname).lower()
            group_eng_buckets.setdefault(eng, []).append(cg)

        self._group_eng: Dict[str, Dict[str, Any]] = {
            k: b[0] for k, b in group_eng_buckets.items() if len(b) == 1
        }

        self.existing_customers_by_key: Dict[str, Dict[str, Any]] = {}
        for cust in existing_customers:
            ckey = _normalize_customer_key(cust["customer_name"])
            if ckey and ckey not in self.existing_customers_by_key:
                self.existing_customers_by_key[ckey] = cust

    def resolve_customer_group(self, raw_val: Any) -> Optional[Dict[str, Any]]:
        if raw_val is None or isinstance(raw_val, bool):
            return None
        if isinstance(raw_val, int):
            return self._group_by_id.get(raw_val)
        if isinstance(raw_val, float):
            if raw_val.is_integer():
                return self._group_by_id.get(int(raw_val))
            return None
        text = _normalize_whitespace(str(raw_val))
        if not text:
            return None
        if text.isdigit():
            by_id = self._group_by_id.get(int(text))
            if by_id:
                return by_id
        lowered = text.lower()
        return self._group_exact.get(lowered) or self._group_eng.get(lowered)


class _SupplierLookups:
    """In-memory lookup index for Supplier Master import built from one read-only snapshot."""

    def __init__(
        self,
        *,
        existing_suppliers: Sequence[Dict[str, Any]],
    ):
        self.existing_suppliers_by_key: Dict[str, Dict[str, Any]] = {}
        for s in existing_suppliers:
            skey = _normalize_supplier_key(s["supplier_name"])
            if skey and skey not in self.existing_suppliers_by_key:
                self.existing_suppliers_by_key[skey] = s


def _is_cell_blank(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str) and not val.strip():
        return True
    return False


def _is_unmodified_sample_row(raw_product_name: Any) -> bool:
    if not isinstance(raw_product_name, str):
        return False
    norm = _normalize_product_key(raw_product_name)
    return (
        norm == _normalize_product_key(TEMPLATE_SAMPLE_PRODUCT_NAME)
        or norm.startswith(TEMPLATE_SAMPLE_PREFIX)
    )


def _is_unmodified_customer_sample_row(raw_customer_name: Any) -> bool:
    if not isinstance(raw_customer_name, str):
        return False
    norm = _normalize_customer_key(raw_customer_name)
    return (
        norm == _normalize_customer_key(TEMPLATE_SAMPLE_CUSTOMER_NAME)
        or norm.startswith(TEMPLATE_SAMPLE_PREFIX)
    )


def _is_unmodified_supplier_sample_row(raw_supplier_name: Any) -> bool:
    if not isinstance(raw_supplier_name, str):
        return False
    norm = _normalize_supplier_key(raw_supplier_name)
    return (
        norm == _normalize_supplier_key(TEMPLATE_SAMPLE_SUPPLIER_NAME)
        or norm.startswith(TEMPLATE_SAMPLE_PREFIX)
    )


def _write_instruction_row(ws, row_idx: int, text: str, *, col_span: int, bold: bool = False) -> int:
    span = max(1, int(col_span))
    ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=span)
    safe = sanitize_excel_text(text)
    cell = ws.cell(row=row_idx, column=1, value=safe)
    cell.data_type = "s"
    cell.number_format = "@"
    cell.font = Font(name="Calibri", size=9.5, bold=bold, color="78350F") if bold else _NOTE_FONT
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=1, wrap_text=True)
    for col_i in range(1, span + 1):
        c = ws.cell(row=row_idx, column=col_i)
        c.fill = _NOTE_FILL
        c.border = _NOTE_BORDER
    ws.row_dimensions[row_idx].height = 24 if len(safe) > 110 else 19
    return row_idx + 1


class ExcelImportService:
    """
    Service layer handling secure Excel template generation, workbook preview validation,
    and atomic SQLite import commits.
    """

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        token_store: Optional[PreviewTokenStore] = None,
    ):
        self.db = db_manager or get_db_manager()
        self.token_store = token_store or get_preview_token_store()

    # ------------------------------------------------------------------
    # Read-Only Snapshot Helpers
    # ------------------------------------------------------------------
    def _load_snapshot_lookups(self) -> Tuple[Dict[str, Any], _MasterLookups]:
        """
        Read company settings, master lookup tables, and existing product names inside one
        consistent BEGIN DEFERRED snapshot and close the connection immediately.
        """
        conn = self.db.get_connection()
        cursor = None
        try:
            conn.execute("BEGIN DEFERRED;")
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    company_name, owner_name, address, city, state, pincode,
                    mobile, email, gstin
                FROM company_settings
                WHERE setting_id = 1;
                """
            )
            comp_row = cursor.fetchone()
            company_settings: Dict[str, Any] = (
                dict(comp_row)
                if comp_row
                else {"company_name": "Krushidhan Agri-Input Shop ERP", "state": "Maharashtra"}
            )

            cursor.execute(
                """
                SELECT tax_group_id, tax_group_name, cgst_rate, sgst_rate, igst_rate, is_active
                FROM tax_groups
                ORDER BY tax_group_id ASC;
                """
            )
            tax_groups = [dict(r) for r in cursor.fetchall()]

            cursor.execute(
                """
                SELECT category_id, category_name, short_name
                FROM categories
                ORDER BY category_name ASC;
                """
            )
            categories = [dict(r) for r in cursor.fetchall()]

            cursor.execute(
                """
                SELECT manufacturer_id, manufacturer_name
                FROM manufacturers
                ORDER BY manufacturer_name ASC;
                """
            )
            manufacturers = [dict(r) for r in cursor.fetchall()]

            cursor.execute(
                """
                SELECT unit_id, unit_name, symbol
                FROM units
                ORDER BY unit_id ASC;
                """
            )
            units = [dict(r) for r in cursor.fetchall()]

            cursor.execute(
                """
                SELECT crop_id, crop_name
                FROM crops
                ORDER BY crop_name ASC;
                """
            )
            crops = [dict(r) for r in cursor.fetchall()]

            cursor.execute(
                """
                SELECT product_id, product_name, is_active
                FROM products;
                """
            )
            existing_products = [dict(r) for r in cursor.fetchall()]

            conn.rollback()
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            conn.close()

        lookups = _MasterLookups(
            tax_groups=tax_groups,
            categories=categories,
            manufacturers=manufacturers,
            units=units,
            crops=crops,
            existing_products=existing_products,
        )
        return company_settings, lookups

    # ------------------------------------------------------------------
    # 1. Product Import Template (.xlsx)
    # ------------------------------------------------------------------
    def generate_product_import_template(
        self,
        *,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the ADMIN-only downloadable `.xlsx` Product Import Template
        with a single worksheet named `Product Import`, clear mandatory/optional
        instructions referencing active Tax Groups, and one marked sample row.
        Zero temporary files written to disk.
        """
        company_settings, lookups = self._load_snapshot_lookups()
        num_cols = len(_TEMPLATE_HEADERS)

        wb = create_workbook(PRODUCT_IMPORT_SHEET_NAME)
        ws = add_sheet(wb, PRODUCT_IMPORT_SHEET_NAME)

        row_ptr = add_company_header(
            ws,
            company_settings,
            start_row=1,
            col_span=num_cols,
            include_licences=False,
        )
        row_ptr = add_report_banner(
            ws,
            "Product Master Import Template (Admin Only)",
            generated_by=generated_by,
            extra_meta={
                "Sheet": PRODUCT_IMPORT_SHEET_NAME,
                "Format": "Strict .xlsx Only (Zero Formulas)",
            },
            start_row=row_ptr,
            col_span=num_cols,
        )

        active_tax_names = [
            f"{tg['tax_group_name']} [ID {tg['tax_group_id']}]"
            for tg in lookups.tax_groups
            if int(tg.get("is_active") or 0) == 1
        ]
        unit_hints = [
            f"{u['unit_name']} ({u['symbol']})" if u.get("symbol") else str(u["unit_name"])
            for u in lookups.units
        ]
        cat_hints = [_display_english_label(c["category_name"]) for c in lookups.categories]

        row_ptr = _write_instruction_row(
            ws,
            row_ptr,
            "Mandatory Fields: Product Name * and Tax Group *. Optional Fields: Category, Manufacturer / Brand, Unit, HSN Code (4, 6, or 8 digits), Sale Rate (INR), MRP (INR), Min Stock Alert, Target Crop.",
            col_span=num_cols,
            bold=True,
        )
        row_ptr = _write_instruction_row(
            ws,
            row_ptr,
            f"Valid Tax Groups: {'; '.join(active_tax_names) or 'Configure active Tax Groups in ERP Master Data'}.",
            col_span=num_cols,
        )
        row_ptr = _write_instruction_row(
            ws,
            row_ptr,
            f"Reference Master Values — Categories: {', '.join(cat_hints[:8]) or 'N/A'} | Units: {', '.join(unit_hints[:9]) or 'N/A'}. Product ID is auto-assigned (no Product Code column exists).",
            col_span=num_cols,
        )
        row_ptr = _write_instruction_row(
            ws,
            row_ptr,
            "Security & Scope Rules: Never add Opening Stock, Current Stock, Batch Qty, Balances, Purchase Rates, Profit Margins, Passwords, or Excel formulas. Sample row below is skipped automatically unless edited into a real product.",
            col_span=num_cols,
        )

        header_row_idx = row_ptr
        sample_row_idx = write_table_header(ws, _TEMPLATE_HEADERS, row_idx=header_row_idx)

        # Pick authentic master defaults for the illustrative sample row
        sample_cat = (
            _display_english_label(lookups.categories[0]["category_name"])
            if lookups.categories
            else ""
        )
        sample_mfg = (
            str(lookups.manufacturers[0]["manufacturer_name"])
            if lookups.manufacturers
            else ""
        )
        sample_unit = "Bag"
        if lookups.units:
            bag_unit = lookups.resolve_unit("Bag")
            sample_unit = (
                str(bag_unit["unit_name"])
                if bag_unit
                else str(lookups.units[0]["unit_name"])
            )
        active_taxes = [tg for tg in lookups.tax_groups if int(tg.get("is_active") or 0) == 1]
        sample_tax = (
            str(lookups.resolve_tax_group("GST 5%")["tax_group_name"])
            if lookups.resolve_tax_group("GST 5%")
            else (str(active_taxes[0]["tax_group_name"]) if active_taxes else "")
        )
        sample_crop = (
            _display_english_label(lookups.crops[0]["crop_name"])
            if lookups.crops
            else ""
        )

        write_text_cell(ws, sample_row_idx, 1, TEMPLATE_SAMPLE_PRODUCT_NAME, font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 2, sample_cat, font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 3, sample_mfg, font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 4, sample_unit, alignment=_ALIGN_CENTER, font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 5, "31021000", alignment=_ALIGN_CENTER, font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 6, sample_tax, font=_SAMPLE_FONT)
        write_currency_cell(ws, sample_row_idx, 7, Decimal("266.50"), font=_SAMPLE_FONT)
        write_currency_cell(ws, sample_row_idx, 8, Decimal("270.00"), font=_SAMPLE_FONT)
        write_quantity_cell(ws, sample_row_idx, 9, Decimal("10"), decimals=3, font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 10, sample_crop, font=_SAMPLE_FONT)
        for col_i in range(1, num_cols + 1):
            ws.cell(row=sample_row_idx, column=col_i).fill = _SAMPLE_FILL
        ws.row_dimensions[sample_row_idx].height = 20

        # Pre-format empty HSN column cells below as '@' text so Excel retains leading zeroes cleanly
        for entry_row in range(sample_row_idx + 1, sample_row_idx + 101):
            ws.cell(row=entry_row, column=5).number_format = "@"

        apply_freeze_panes(ws, header_row=header_row_idx, freeze_col=1)
        auto_fit_columns(
            ws,
            min_width=14,
            max_width=44,
        )
        ws.column_dimensions["A"].width = max(ws.column_dimensions["A"].width or 0, 38)
        ws.column_dimensions["F"].width = max(ws.column_dimensions["F"].width or 0, 34)

        return save_workbook_to_bytes(wb), PRODUCT_IMPORT_TEMPLATE_FILENAME

    # ------------------------------------------------------------------
    # 2. Header Detection & Validation
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_header_row_index(raw_bytes: bytes, *, filename: str) -> int:
        with safe_xlsx_workbook(raw_bytes, filename=filename, read_only=True) as wb:
            sheetnames = list(wb.sheetnames or [])
            if not sheetnames:
                raise ExcelValidationError("Workbook contains no worksheets.")
            if PRODUCT_IMPORT_SHEET_NAME not in sheetnames:
                raise ExcelValidationError(
                    f"Requested worksheet '{PRODUCT_IMPORT_SHEET_NAME}' was not found in workbook. "
                    f"Please use the official Product Import Template.",
                )

            ws = wb[PRODUCT_IMPORT_SHEET_NAME]
            first_non_empty_row: Optional[int] = None
            for phys_row_idx, row_cells in enumerate(
                ws.iter_rows(max_row=25, values_only=False), start=1
            ):
                row_vals = [getattr(c, "value", None) for c in row_cells]
                populated = [v for v in row_vals if not _is_cell_blank(v)]
                if not populated:
                    continue
                if first_non_empty_row is None:
                    first_non_empty_row = phys_row_idx

                if len(populated) >= 2:
                    tokens = {_normalize_header_token(v) for v in populated if isinstance(v, str)}
                    if "product_name" in tokens or "tax_group" in tokens or "tax_group_name" in tokens:
                        return phys_row_idx

            if first_non_empty_row is not None:
                return first_non_empty_row
            raise ExcelValidationError(
                f"Worksheet '{PRODUCT_IMPORT_SHEET_NAME}' is empty or missing column headers."
            )

    @staticmethod
    def _parse_and_validate_headers(headers: Sequence[str]) -> Dict[str, int]:
        if not headers or all(not str(h or "").strip() for h in headers):
            raise ExcelValidationError(
                f"Worksheet '{PRODUCT_IMPORT_SHEET_NAME}' is missing column headers."
            )

        # C3-B1 shared header blocklist check
        assert_headers_allowed_for_import(headers)

        col_map: Dict[str, int] = {}
        unknown_headers: List[str] = []

        for idx, raw_header in enumerate(headers):
            display_h = _normalize_whitespace(str(raw_header or ""))
            if not display_h:
                raise ExcelValidationError(
                    f"Blank column header found at column {idx + 1}. Remove empty columns inside the header row."
                )

            norm_token = _normalize_header_token(display_h)
            if norm_token in PRODUCT_FORBIDDEN_HEADERS:
                raise ExcelSecurityError(
                    f"Column '{display_h}' (column {idx + 1}) is forbidden in Product Import "
                    "(stock, batch quantities, balances, purchase costs, margins, IDs, and security fields cannot be imported)."
                )

            canonical = _CANONICAL_HEADER_ALIASES.get(norm_token)
            if canonical is None:
                unknown_headers.append(display_h)
                continue

            if canonical in col_map:
                raise ExcelValidationError(
                    f"Duplicate column header '{display_h}' maps to '{canonical}'."
                )
            col_map[canonical] = idx

        if unknown_headers:
            raise ExcelValidationError(
                f"Unsupported column header(s) in '{PRODUCT_IMPORT_SHEET_NAME}': {', '.join(unknown_headers)}. "
                "Download and use the official Product Import Template."
            )

        missing_required_cols = []
        if "product_name" not in col_map:
            missing_required_cols.append("Product Name")
        if "tax_group" not in col_map:
            missing_required_cols.append("Tax Group")
        if missing_required_cols:
            raise ExcelValidationError(
                f"Missing mandatory column header(s) in '{PRODUCT_IMPORT_SHEET_NAME}': {', '.join(missing_required_cols)}."
            )

        return col_map

    # ------------------------------------------------------------------
    # 3. Preview Before Import (Zero DB Writes)
    # ------------------------------------------------------------------
    def preview_product_import(
        self,
        raw_bytes: bytes,
        *,
        filename: str,
        admin_user: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """
        Validate uploaded `.xlsx` workbook completely in memory, validate every row
        against SQLite master lookups and existing product names (zero DB writes),
        and issue a bound short-lived preview token.
        """
        file_sha256 = hashlib.sha256(bytes(raw_bytes)).hexdigest()
        header_row_idx = self._detect_header_row_index(raw_bytes, filename=filename)

        parsed = read_xlsx_sheet_safely(
            raw_bytes,
            filename=filename,
            sheet_name=PRODUCT_IMPORT_SHEET_NAME,
            header_rows=header_row_idx,
            max_data_rows=MAX_DATA_ROWS,
            max_columns=MAX_COLUMNS,
            max_cell_length=MAX_CELL_LENGTH,
            validate_all_sheets=True,
        )
        headers = parsed.get("headers") or []
        col_map = self._parse_and_validate_headers(headers)

        _, lookups = self._load_snapshot_lookups()

        data_rows: List[List[Any]] = parsed.get("rows") or []
        row_numbers: List[int] = parsed.get("row_numbers") or [
            header_row_idx + idx + 1 for idx in range(len(data_rows))
        ]

        def _cell(row_vals: Sequence[Any], field_key: str) -> Any:
            col_i = col_map.get(field_key)
            if col_i is None or col_i >= len(row_vals):
                return None
            return row_vals[col_i]

        seen_in_file: Dict[str, int] = {}
        preview_rows: List[Dict[str, Any]] = []
        ready_candidates: List[ValidatedProductCandidate] = []
        sample_rows_skipped = 0

        counts = {
            "total_rows": 0,
            "ready_count": 0,
            "duplicate_existing_count": 0,
            "duplicate_file_count": 0,
            "duplicate_count": 0,
            "invalid_missing_count": 0,
            "invalid_validation_count": 0,
            "invalid_count": 0,
        }

        for row_vals, phys_row_no in zip(data_rows, row_numbers):
            if all(_is_cell_blank(v) for v in row_vals):
                continue

            raw_name_val = _cell(row_vals, "product_name")
            if _is_unmodified_sample_row(raw_name_val):
                sample_rows_skipped += 1
                continue

            counts["total_rows"] += 1
            missing_errors: List[Dict[str, str]] = []
            validation_errors: List[Dict[str, str]] = []

            # ---- Product Name (Mandatory) ----
            clean_name = ""
            if _is_cell_blank(raw_name_val):
                missing_errors.append(
                    {
                        "column": "Product Name",
                        "field": "product_name",
                        "message": "Product Name is mandatory.",
                    }
                )
            elif isinstance(raw_name_val, bool):
                validation_errors.append(
                    {
                        "column": "Product Name",
                        "field": "product_name",
                        "message": "Product Name cannot be a boolean value.",
                    }
                )
            else:
                clean_name = _normalize_whitespace(str(raw_name_val))
                if len(clean_name) > MAX_PRODUCT_NAME_LENGTH:
                    validation_errors.append(
                        {
                            "column": "Product Name",
                            "field": "product_name",
                            "message": f"Product Name exceeds maximum length of {MAX_PRODUCT_NAME_LENGTH} characters.",
                        }
                    )

            # ---- Tax Group (Mandatory, DB Master Lookup) ----
            raw_tax_val = _cell(row_vals, "tax_group")
            tax_record: Optional[Dict[str, Any]] = None
            tax_display = ""
            if _is_cell_blank(raw_tax_val):
                missing_errors.append(
                    {
                        "column": "Tax Group",
                        "field": "tax_group",
                        "message": "Tax Group is mandatory.",
                    }
                )
            else:
                tax_display = _normalize_whitespace(str(raw_tax_val))
                tax_record = lookups.resolve_tax_group(raw_tax_val)
                if tax_record is None:
                    validation_errors.append(
                        {
                            "column": "Tax Group",
                            "field": "tax_group",
                            "message": f"Unknown or inactive Tax Group '{tax_display}'. Must match an active master Tax Group.",
                        }
                    )
                else:
                    tax_display = str(tax_record["tax_group_name"])

            # ---- Category (Optional, DB Master Lookup) ----
            raw_cat_val = _cell(row_vals, "category")
            cat_record: Optional[Dict[str, Any]] = None
            cat_display = ""
            if not _is_cell_blank(raw_cat_val):
                cat_display = _normalize_whitespace(str(raw_cat_val))
                if isinstance(raw_cat_val, bool):
                    validation_errors.append(
                        {
                            "column": "Category",
                            "field": "category",
                            "message": "Invalid Category value.",
                        }
                    )
                else:
                    cat_record = lookups.resolve_category(raw_cat_val)
                    if cat_record is None:
                        validation_errors.append(
                            {
                                "column": "Category",
                                "field": "category",
                                "message": f"Unknown Category '{cat_display}'. Must match an existing product category.",
                            }
                        )
                    else:
                        cat_display = _display_english_label(cat_record["category_name"])

            # ---- Manufacturer / Brand (Optional, DB Master Lookup) ----
            raw_mfg_val = _cell(row_vals, "manufacturer")
            mfg_record: Optional[Dict[str, Any]] = None
            mfg_display = ""
            if not _is_cell_blank(raw_mfg_val):
                mfg_display = _normalize_whitespace(str(raw_mfg_val))
                if isinstance(raw_mfg_val, bool):
                    validation_errors.append(
                        {
                            "column": "Manufacturer / Brand",
                            "field": "manufacturer",
                            "message": "Invalid Manufacturer value.",
                        }
                    )
                else:
                    mfg_record = lookups.resolve_manufacturer(raw_mfg_val)
                    if mfg_record is None:
                        validation_errors.append(
                            {
                                "column": "Manufacturer / Brand",
                                "field": "manufacturer",
                                "message": f"Unknown Manufacturer / Brand '{mfg_display}'. Must match an existing master manufacturer.",
                            }
                        )
                    else:
                        mfg_display = str(mfg_record["manufacturer_name"])

            # ---- Unit (Optional, DB Master Lookup) ----
            raw_unit_val = _cell(row_vals, "unit")
            unit_record: Optional[Dict[str, Any]] = None
            unit_display = ""
            if not _is_cell_blank(raw_unit_val):
                unit_display = _normalize_whitespace(str(raw_unit_val))
                if isinstance(raw_unit_val, bool):
                    validation_errors.append(
                        {
                            "column": "Unit",
                            "field": "unit",
                            "message": "Invalid Unit value.",
                        }
                    )
                else:
                    unit_record = lookups.resolve_unit(raw_unit_val)
                    if unit_record is None:
                        validation_errors.append(
                            {
                                "column": "Unit",
                                "field": "unit",
                                "message": f"Unknown Unit '{unit_display}'. Must match an existing unit name or symbol.",
                            }
                        )
                    else:
                        unit_display = (
                            f"{unit_record['unit_name']} ({unit_record['symbol']})"
                            if unit_record.get("symbol")
                            else str(unit_record["unit_name"])
                        )

            # ---- HSN Code (Optional, 4/6/8 digits) ----
            raw_hsn_val = _cell(row_vals, "hsn_code")
            clean_hsn: Optional[str] = None
            if not _is_cell_blank(raw_hsn_val):
                try:
                    clean_hsn = validate_hsn(raw_hsn_val, required=False, field_name="HSN Code")
                except ExcelValidationError as exc:
                    validation_errors.append(
                        {
                            "column": "HSN Code",
                            "field": "hsn_code",
                            "message": exc.message,
                        }
                    )

            # ---- Sale Rate (Optional >= 0, 2dp) ----
            raw_sale_rate = _cell(row_vals, "sale_rate")
            sale_rate_val = 0.0
            if not _is_cell_blank(raw_sale_rate):
                try:
                    dec_sale = validate_decimal_amount(
                        raw_sale_rate,
                        field_name="Sale Rate",
                        min_value=Decimal("0"),
                        max_value=Decimal("10000000"),
                        max_decimals=2,
                        allow_zero=True,
                        allow_negative=False,
                    )
                    sale_rate_val = float(dec_sale)
                except ExcelValidationError as exc:
                    validation_errors.append(
                        {
                            "column": "Sale Rate (INR)",
                            "field": "sale_rate",
                            "message": exc.message,
                        }
                    )

            # ---- MRP (Optional >= 0, 2dp) ----
            raw_mrp = _cell(row_vals, "mrp")
            mrp_val = 0.0
            if not _is_cell_blank(raw_mrp):
                try:
                    dec_mrp = validate_decimal_amount(
                        raw_mrp,
                        field_name="MRP",
                        min_value=Decimal("0"),
                        max_value=Decimal("10000000"),
                        max_decimals=2,
                        allow_zero=True,
                        allow_negative=False,
                    )
                    mrp_val = float(dec_mrp)
                except ExcelValidationError as exc:
                    validation_errors.append(
                        {
                            "column": "MRP (INR)",
                            "field": "mrp",
                            "message": exc.message,
                        }
                    )

            # ---- Min Stock Alert (Optional >= 0, 3dp) ----
            raw_alert = _cell(row_vals, "min_stock_alert")
            min_alert_val = 0.0
            if not _is_cell_blank(raw_alert):
                try:
                    dec_alert = validate_quantity(
                        raw_alert,
                        field_name="Min Stock Alert",
                        min_value=Decimal("0"),
                        max_value=Decimal("1000000"),
                        max_decimals=3,
                        allow_zero=True,
                    )
                    min_alert_val = float(dec_alert)
                except ExcelValidationError as exc:
                    validation_errors.append(
                        {
                            "column": "Min Stock Alert",
                            "field": "min_stock_alert",
                            "message": exc.message,
                        }
                    )

            # ---- Target Crop (Optional, DB Master Lookup) ----
            raw_crop_val = _cell(row_vals, "target_crop")
            crop_record: Optional[Dict[str, Any]] = None
            crop_display = ""
            if not _is_cell_blank(raw_crop_val):
                crop_display = _normalize_whitespace(str(raw_crop_val))
                if isinstance(raw_crop_val, bool):
                    validation_errors.append(
                        {
                            "column": "Target Crop",
                            "field": "target_crop",
                            "message": "Invalid Target Crop value.",
                        }
                    )
                else:
                    crop_record = lookups.resolve_crop(raw_crop_val)
                    if crop_record is None:
                        validation_errors.append(
                            {
                                "column": "Target Crop",
                                "field": "target_crop",
                                "message": f"Unknown Target Crop '{crop_display}'. Must match an existing crop master.",
                            }
                        )
                    else:
                        crop_display = _display_english_label(crop_record["crop_name"])

            # ---- Determine Row Status & Duplicate Classification ----
            all_errors = missing_errors + validation_errors
            if missing_errors:
                status = ROW_STATUS_INVALID_REQUIRED
                counts["invalid_missing_count"] += 1
                counts["invalid_count"] += 1
                message = "; ".join(e["message"] for e in all_errors)
            elif validation_errors:
                status = ROW_STATUS_INVALID_VALIDATION
                counts["invalid_validation_count"] += 1
                counts["invalid_count"] += 1
                message = "; ".join(e["message"] for e in all_errors)
            else:
                norm_key = _normalize_product_key(clean_name)
                if norm_key in seen_in_file:
                    first_row = seen_in_file[norm_key]
                    status = ROW_STATUS_DUP_FILE
                    counts["duplicate_file_count"] += 1
                    counts["duplicate_count"] += 1
                    message = f"Repeated product name in workbook (first appeared at Row {first_row})."
                    all_errors.append(
                        {
                            "column": "Product Name",
                            "field": "product_name",
                            "message": message,
                        }
                    )
                elif norm_key in lookups.existing_products_by_key:
                    seen_in_file[norm_key] = phys_row_no
                    existing_rec = lookups.existing_products_by_key[norm_key]
                    active_hint = (
                        "active"
                        if int(existing_rec.get("is_active") or 0) == 1
                        else "inactive"
                    )
                    status = ROW_STATUS_DUP_DB
                    counts["duplicate_existing_count"] += 1
                    counts["duplicate_count"] += 1
                    message = (
                        f"Already exists in database (Product ID #{existing_rec['product_id']}, {active_hint}). "
                        "Existing products are never overwritten."
                    )
                    all_errors.append(
                        {
                            "column": "Product Name",
                            "field": "product_name",
                            "message": message,
                        }
                    )
                else:
                    assert tax_record is not None
                    seen_in_file[norm_key] = phys_row_no
                    status = ROW_STATUS_READY
                    counts["ready_count"] += 1
                    message = "Ready to Import"
                    ready_candidates.append(
                        ValidatedProductCandidate(
                            row_number=phys_row_no,
                            product_name=clean_name,
                            normalized_name=norm_key,
                            category_id=int(cat_record["category_id"]) if cat_record else None,
                            category_label=cat_display,
                            manufacturer_id=int(mfg_record["manufacturer_id"]) if mfg_record else None,
                            manufacturer_label=mfg_display,
                            unit_id=int(unit_record["unit_id"]) if unit_record else None,
                            unit_label=unit_display,
                            hsn_code=clean_hsn,
                            tax_group_id=int(tax_record["tax_group_id"]),
                            tax_group_label=tax_display,
                            default_sale_rate=sale_rate_val,
                            default_mrp=mrp_val,
                            min_stock_alert=min_alert_val,
                            crop_id=int(crop_record["crop_id"]) if crop_record else None,
                            crop_label=crop_display,
                        )
                    )

            preview_rows.append(
                {
                    "row_number": phys_row_no,
                    "status": status,
                    "product_name": clean_name or ("" if _is_cell_blank(raw_name_val) else str(raw_name_val)),
                    "category": cat_display,
                    "manufacturer": mfg_display,
                    "unit": unit_display,
                    "hsn_code": clean_hsn or ("" if _is_cell_blank(raw_hsn_val) else str(raw_hsn_val)),
                    "tax_group": tax_display,
                    "sale_rate": sale_rate_val if _is_cell_blank(raw_sale_rate) or not any(e["field"] == "sale_rate" for e in validation_errors) else str(raw_sale_rate),
                    "mrp": mrp_val if _is_cell_blank(raw_mrp) or not any(e["field"] == "mrp" for e in validation_errors) else str(raw_mrp),
                    "min_stock_alert": min_alert_val if _is_cell_blank(raw_alert) or not any(e["field"] == "min_stock_alert" for e in validation_errors) else str(raw_alert),
                    "target_crop": crop_display,
                    "errors": all_errors,
                    "message": message,
                }
            )

        token_rec = self.token_store.create(
            import_kind=IMPORT_KIND_PRODUCTS,
            user_id=int(admin_user["user_id"]),
            username=str(admin_user["username"]),
            db_key=_db_identity_key(self.db),
            file_sha256=file_sha256,
            filename=filename,
            ready_candidates=ready_candidates,
            preview_rows=preview_rows,
            summary=counts,
            sample_rows_skipped=sample_rows_skipped,
        )

        expires_iso = datetime.fromtimestamp(
            token_rec.expires_at, tz=timezone.utc
        ).isoformat()

        return {
            "success": True,
            "sheet_name": PRODUCT_IMPORT_SHEET_NAME,
            "filename": token_rec.filename,
            "file_sha256": token_rec.file_sha256,
            "preview_token": token_rec.token,
            "expires_in_seconds": int(max(0, round(token_rec.expires_at - token_rec.created_at))),
            "expires_at": expires_iso,
            "sample_rows_skipped": sample_rows_skipped,
            "summary": counts,
            "rows": preview_rows,
        }

    # ------------------------------------------------------------------
    # 4. Confirmed Atomic Import Commit
    # ------------------------------------------------------------------
    def commit_product_import(
        self,
        *,
        preview_token: str,
        file_sha256: str,
        admin_user: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """
        Validate the single-use preview token against user & uploaded SHA-256,
        recheck duplicates and master references inside an atomic SQLite
        `BEGIN IMMEDIATE` transaction, insert ONLY `Ready to Import` products
        using existing `Product` + `MasterDataRepository.create_product`,
        and roll back completely on any DB consistency failure.
        """
        admin_uid = int(admin_user["user_id"])
        admin_username = str(admin_user["username"])
        db_key = _db_identity_key(self.db)

        record = self.token_store.validate_and_consume(
            preview_token,
            user_id=admin_uid,
            username=admin_username,
            db_key=db_key,
            file_sha256=file_sha256,
            expected_kind=IMPORT_KIND_PRODUCTS,
        )

        # Carry over skipped duplicates / invalids recorded during preview
        pre_skipped_dup = int(record.summary.get("duplicate_count", 0))
        pre_invalid = int(record.summary.get("invalid_count", 0))
        row_messages: List[Dict[str, Any]] = []
        for prev_r in record.preview_rows:
            if prev_r.get("status") != ROW_STATUS_READY:
                row_messages.append(
                    {
                        "row_number": prev_r["row_number"],
                        "product_name": prev_r.get("product_name", ""),
                        "status": prev_r["status"],
                        "message": prev_r.get("message", ""),
                    }
                )

        imported_products: List[Dict[str, Any]] = []
        concurrent_dup_skips = 0
        repo = MasterDataRepository(self.db)

        try:
            with self.db.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE;")
                cur = conn.cursor()
                try:
                    # Re-read existing products & master FKs inside the exclusive write lock
                    cur.execute("SELECT product_id, product_name, is_active FROM products;")
                    existing_names: Dict[str, Tuple[int, int]] = {}
                    for r in cur.fetchall():
                        key = _normalize_product_key(r["product_name"])
                        if key and key not in existing_names:
                            existing_names[key] = (int(r["product_id"]), int(r["is_active"] or 0))

                    cur.execute("SELECT tax_group_id FROM tax_groups WHERE is_active = 1;")
                    active_tax_ids = {int(r["tax_group_id"]) for r in cur.fetchall()}

                    cur.execute("SELECT category_id FROM categories;")
                    valid_cat_ids = {int(r["category_id"]) for r in cur.fetchall()}

                    cur.execute("SELECT manufacturer_id FROM manufacturers;")
                    valid_mfg_ids = {int(r["manufacturer_id"]) for r in cur.fetchall()}

                    cur.execute("SELECT unit_id FROM units;")
                    valid_unit_ids = {int(r["unit_id"]) for r in cur.fetchall()}

                    cur.execute("SELECT crop_id FROM crops;")
                    valid_crop_ids = {int(r["crop_id"]) for r in cur.fetchall()}

                    for cand in record.ready_candidates:
                        if cand.normalized_name in existing_names:
                            existing_pid, _ = existing_names[cand.normalized_name]
                            concurrent_dup_skips += 1
                            row_messages.append(
                                {
                                    "row_number": cand.row_number,
                                    "product_name": cand.product_name,
                                    "status": ROW_STATUS_DUP_DB,
                                    "message": (
                                        f"Skipped during commit — already exists in database "
                                        f"(Product ID #{existing_pid})."
                                    ),
                                }
                            )
                            continue

                        # Verify referenced master records still exist cleanly before insertion
                        if cand.tax_group_id not in active_tax_ids:
                            raise ExcelValidationError(
                                f"Row {cand.row_number}: referenced Tax Group '{cand.tax_group_label}' "
                                "is no longer active. Transaction rolled back cleanly.",
                                row=cand.row_number,
                                field="tax_group",
                            )
                        if cand.category_id is not None and cand.category_id not in valid_cat_ids:
                            raise ExcelValidationError(
                                f"Row {cand.row_number}: referenced Category '{cand.category_label}' "
                                "no longer exists. Transaction rolled back cleanly.",
                                row=cand.row_number,
                                field="category",
                            )
                        if cand.manufacturer_id is not None and cand.manufacturer_id not in valid_mfg_ids:
                            raise ExcelValidationError(
                                f"Row {cand.row_number}: referenced Manufacturer '{cand.manufacturer_label}' "
                                "no longer exists. Transaction rolled back cleanly.",
                                row=cand.row_number,
                                field="manufacturer",
                            )
                        if cand.unit_id is not None and cand.unit_id not in valid_unit_ids:
                            raise ExcelValidationError(
                                f"Row {cand.row_number}: referenced Unit '{cand.unit_label}' "
                                "no longer exists. Transaction rolled back cleanly.",
                                row=cand.row_number,
                                field="unit",
                            )
                        if cand.crop_id is not None and cand.crop_id not in valid_crop_ids:
                            raise ExcelValidationError(
                                f"Row {cand.row_number}: referenced Target Crop '{cand.crop_label}' "
                                "no longer exists. Transaction rolled back cleanly.",
                                row=cand.row_number,
                                field="target_crop",
                            )

                        prod_model = Product(
                            product_name=cand.product_name,
                            category_id=cand.category_id,
                            manufacturer_id=cand.manufacturer_id,
                            hsn_code=cand.hsn_code,
                            unit_id=cand.unit_id,
                            tax_group_id=cand.tax_group_id,
                            default_purchase_rate=0.0,
                            default_sale_rate=cand.default_sale_rate,
                            default_mrp=cand.default_mrp,
                            min_stock_alert=cand.min_stock_alert,
                            crop_id=cand.crop_id,
                            is_active=1,
                        )
                        new_prod_id = repo.create_product(prod_model, conn=conn)
                        existing_names[cand.normalized_name] = (int(new_prod_id), 1)
                        imported_products.append(
                            {
                                "row_number": cand.row_number,
                                "product_id": int(new_prod_id),
                                "product_name": cand.product_name,
                            }
                        )
                        row_messages.append(
                            {
                                "row_number": cand.row_number,
                                "product_name": cand.product_name,
                                "status": "Imported",
                                "product_id": int(new_prod_id),
                                "message": f"Imported successfully (Product ID #{new_prod_id}).",
                            }
                        )

                    if imported_products:
                        cur.execute("SELECT 1 FROM users WHERE user_id = ? LIMIT 1;", (admin_uid,))
                        audit_uid = admin_uid if cur.fetchone() is not None else None
                        cur.execute(
                            "INSERT INTO audit_log (user_id, action_type, table_name, record_id, details) VALUES (?, ?, ?, ?, ?);",
                            (
                                audit_uid,
                                "EXCEL_IMPORT",
                                "products",
                                imported_products[0]["product_id"],
                                (
                                    f"Imported {len(imported_products)} product(s) from Excel "
                                    f"(sha256:{record.file_sha256[:12]}), skipped "
                                    f"{pre_skipped_dup + concurrent_dup_skips} duplicate(s), "
                                    f"{pre_invalid} invalid row(s)."
                                ),
                            ),
                        )
                finally:
                    cur.close()
        except DatabaseMaintenanceError:
            raise
        except ExcelValidationError:
            raise
        except (sqlite3.Error, ValueError, RuntimeError) as exc:
            # Never leak internal DB file paths, schema traces, or raw SQL errors
            raise ExcelImportTransactionError(
                "Product import transaction failed and was rolled back completely. No products were added or modified."
            ) from exc

        row_messages.sort(key=lambda item: int(item.get("row_number") or 0))
        total_skipped_dups = pre_skipped_dup + concurrent_dup_skips
        imported_count = len(imported_products)

        return {
            "success": True,
            "imported_count": imported_count,
            "skipped_duplicate_count": total_skipped_dups,
            "invalid_count": pre_invalid,
            "failed_count": 0,
            "sample_rows_skipped": record.sample_rows_skipped,
            "imported_products": imported_products,
            "rows": row_messages,
            "message": (
                f"Imported {imported_count} product(s); skipped {total_skipped_dups} duplicate(s) "
                f"and {pre_invalid} invalid row(s)."
            ),
        }

    # ------------------------------------------------------------------
    # Stage C3-C2: Customer Master Import
    # ------------------------------------------------------------------
    def _load_customer_snapshot_lookups(self) -> Tuple[Dict[str, Any], _CustomerLookups]:
        """
        Read company settings, customer groups, and existing customer names inside
        one consistent BEGIN DEFERRED snapshot and close the connection immediately.
        """
        conn = self.db.get_connection()
        cursor = None
        try:
            conn.execute("BEGIN DEFERRED;")
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    company_name, owner_name, address, city, state, pincode,
                    mobile, email, gstin
                FROM company_settings
                WHERE setting_id = 1;
                """
            )
            comp_row = cursor.fetchone()
            company_settings: Dict[str, Any] = (
                dict(comp_row)
                if comp_row
                else {"company_name": "Krushidhan Agri-Input Shop ERP", "state": "Maharashtra"}
            )

            cursor.execute("PRAGMA table_info(customer_groups);")
            cg_cols = {
                str(r["name"] if isinstance(r, sqlite3.Row) else r[1]).lower()
                for r in cursor.fetchall()
            }
            if "is_active" in cg_cols:
                cursor.execute(
                    """
                    SELECT group_id, group_name, discount_percent, COALESCE(is_active, 1) AS is_active
                    FROM customer_groups
                    ORDER BY group_id ASC;
                    """
                )
            else:
                cursor.execute(
                    """
                    SELECT group_id, group_name, discount_percent, 1 AS is_active
                    FROM customer_groups
                    ORDER BY group_id ASC;
                    """
                )
            customer_groups = [dict(r) for r in cursor.fetchall()]

            cursor.execute(
                """
                SELECT customer_id, customer_name, is_active
                FROM customers;
                """
            )
            existing_customers = [dict(r) for r in cursor.fetchall()]

            conn.rollback()
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            conn.close()

        lookups = _CustomerLookups(
            customer_groups=customer_groups,
            existing_customers=existing_customers,
        )
        return company_settings, lookups

    def generate_customer_import_template(
        self,
        *,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the ADMIN-only downloadable `.xlsx` Customer Import Template
        with a single worksheet named `Customer Import`, clear instructions
        listing active Customer Groups, and one marked sample row.
        """
        company_settings, lookups = self._load_customer_snapshot_lookups()
        num_cols = len(_CUSTOMER_TEMPLATE_HEADERS)

        wb = create_workbook(CUSTOMER_IMPORT_SHEET_NAME)
        ws = add_sheet(wb, CUSTOMER_IMPORT_SHEET_NAME)

        row_ptr = add_company_header(
            ws,
            company_settings,
            start_row=1,
            col_span=num_cols,
            include_licences=False,
        )
        row_ptr = add_report_banner(
            ws,
            "Customer Master Import Template (Admin Only)",
            generated_by=generated_by,
            extra_meta={
                "Sheet": CUSTOMER_IMPORT_SHEET_NAME,
                "Format": "Strict .xlsx Only (Zero Formulas)",
            },
            start_row=row_ptr,
            col_span=num_cols,
        )

        active_group_hints = [
            f"{_display_english_label(cg['group_name'])} [ID {cg['group_id']}]"
            for cg in lookups.customer_groups
            if int(cg.get("is_active") if cg.get("is_active") is not None else 1) == 1
        ]

        row_ptr = _write_instruction_row(
            ws,
            row_ptr,
            "Mandatory Field: Customer Name *. Optional Fields: Mobile (10-digit Indian mobile), Village, Taluka, District, State (defaults to Maharashtra if blank), GSTIN (15-character uppercase), Customer Group.",
            col_span=num_cols,
            bold=True,
        )
        row_ptr = _write_instruction_row(
            ws,
            row_ptr,
            f"Valid Customer Groups: {'; '.join(active_group_hints) or 'Configure Customer Groups in ERP Master Data'}. Unknown groups are never auto-created.",
            col_span=num_cols,
        )
        row_ptr = _write_instruction_row(
            ws,
            row_ptr,
            "Formatting Rules: State defaults to Maharashtra when blank (or enter a valid Indian State / Union Territory). GSTIN must be 15 characters (e.g. 27AABCU9603R1ZM). Customer ID is auto-assigned.",
            col_span=num_cols,
        )
        row_ptr = _write_instruction_row(
            ws,
            row_ptr,
            "Security & Scope Rules: Never add Customer ID, Aadhaar, Opening Balance, Current Balance, Credit Limit, Ledger Transactions, Payment History, Email, Pincode, Address, Passwords, or Excel formulas. Sample row below is skipped automatically unless edited into a real customer.",
            col_span=num_cols,
        )

        header_row_idx = row_ptr
        sample_row_idx = write_table_header(ws, _CUSTOMER_TEMPLATE_HEADERS, row_idx=header_row_idx)

        active_groups = [
            cg
            for cg in lookups.customer_groups
            if int(cg.get("is_active") if cg.get("is_active") is not None else 1) == 1
        ]
        sample_group = (
            _display_english_label(active_groups[0]["group_name"])
            if active_groups
            else ""
        )

        write_text_cell(ws, sample_row_idx, 1, TEMPLATE_SAMPLE_CUSTOMER_NAME, font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 2, "9876543210", alignment=_ALIGN_CENTER, font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 3, "Pimpri", font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 4, "Baramati", font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 5, "Pune", font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 6, "Maharashtra", font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 7, "", alignment=_ALIGN_CENTER, font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 8, sample_group, font=_SAMPLE_FONT)
        for col_i in range(1, num_cols + 1):
            ws.cell(row=sample_row_idx, column=col_i).fill = _SAMPLE_FILL
        ws.row_dimensions[sample_row_idx].height = 20

        # Pre-format Mobile and GSTIN entry cells below as '@' text so Excel retains formatting
        for entry_row in range(sample_row_idx + 1, sample_row_idx + 101):
            ws.cell(row=entry_row, column=2).number_format = "@"
            ws.cell(row=entry_row, column=7).number_format = "@"

        apply_freeze_panes(ws, header_row=header_row_idx, freeze_col=1)
        auto_fit_columns(
            ws,
            min_width=14,
            max_width=40,
        )
        ws.column_dimensions["A"].width = max(ws.column_dimensions["A"].width or 0, 36)
        ws.column_dimensions["H"].width = max(ws.column_dimensions["H"].width or 0, 28)

        return save_workbook_to_bytes(wb), CUSTOMER_IMPORT_TEMPLATE_FILENAME

    @staticmethod
    def _detect_customer_header_row_index(raw_bytes: bytes, *, filename: str) -> int:
        with safe_xlsx_workbook(raw_bytes, filename=filename, read_only=True) as wb:
            sheetnames = list(wb.sheetnames or [])
            if not sheetnames:
                raise ExcelValidationError("Workbook contains no worksheets.")
            if CUSTOMER_IMPORT_SHEET_NAME not in sheetnames:
                raise ExcelValidationError(
                    f"Requested worksheet '{CUSTOMER_IMPORT_SHEET_NAME}' was not found in workbook. "
                    "Please use the official Customer Import Template.",
                )

            ws = wb[CUSTOMER_IMPORT_SHEET_NAME]
            first_non_empty_row: Optional[int] = None
            for phys_row_idx, row_cells in enumerate(
                ws.iter_rows(max_row=25, values_only=False), start=1
            ):
                row_vals = [getattr(c, "value", None) for c in row_cells]
                populated = [v for v in row_vals if not _is_cell_blank(v)]
                if not populated:
                    continue
                if first_non_empty_row is None:
                    first_non_empty_row = phys_row_idx

                tokens = {_normalize_header_token(v) for v in populated if isinstance(v, str)}
                if "customer_name" in tokens or "farmer_name" in tokens or "party_name" in tokens:
                    return phys_row_idx
                if len(populated) >= 2 and any(
                    tok in tokens
                    for tok in ("mobile", "gstin", "customer_group", "village", "taluka", "district", "state")
                ):
                    return phys_row_idx

            if first_non_empty_row is not None:
                return first_non_empty_row
            raise ExcelValidationError(
                f"Worksheet '{CUSTOMER_IMPORT_SHEET_NAME}' is empty or missing column headers."
            )

    @staticmethod
    def _parse_and_validate_customer_headers(headers: Sequence[str]) -> Dict[str, int]:
        if not headers or all(not str(h or "").strip() for h in headers):
            raise ExcelValidationError(
                f"Worksheet '{CUSTOMER_IMPORT_SHEET_NAME}' is missing column headers."
            )

        assert_headers_allowed_for_import(headers)

        col_map: Dict[str, int] = {}
        unknown_headers: List[str] = []

        for idx, raw_header in enumerate(headers):
            display_h = _normalize_whitespace(str(raw_header or ""))
            if not display_h:
                raise ExcelValidationError(
                    f"Blank column header found at column {idx + 1}. Remove empty columns inside the header row."
                )

            norm_token = _normalize_header_token(display_h)
            if norm_token in CUSTOMER_FORBIDDEN_HEADERS:
                raise ExcelSecurityError(
                    f"Column '{display_h}' (column {idx + 1}) is forbidden in Customer Import "
                    "(customer IDs, Aadhaar numbers, opening/current balances, credit limits, "
                    "ledger/payment history, and security fields cannot be imported)."
                )

            canonical = _CUSTOMER_CANONICAL_HEADER_ALIASES.get(norm_token)
            if canonical is None:
                unknown_headers.append(display_h)
                continue

            if canonical in col_map:
                raise ExcelValidationError(
                    f"Duplicate column header '{display_h}' maps to '{canonical}'."
                )
            col_map[canonical] = idx

        if unknown_headers:
            raise ExcelValidationError(
                f"Unsupported column header(s) in '{CUSTOMER_IMPORT_SHEET_NAME}': {', '.join(unknown_headers)}. "
                "Download and use the official Customer Import Template."
            )

        if "customer_name" not in col_map:
            raise ExcelValidationError(
                f"Missing mandatory column header(s) in '{CUSTOMER_IMPORT_SHEET_NAME}': Customer Name."
            )

        return col_map

    def preview_customer_import(
        self,
        raw_bytes: bytes,
        *,
        filename: str,
        admin_user: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """
        Validate uploaded `.xlsx` customer workbook completely in memory, check every row
        against active Customer Groups and existing customer names (zero DB writes),
        and issue a bound short-lived preview token for `import_kind="customers"`.
        """
        file_sha256 = hashlib.sha256(bytes(raw_bytes)).hexdigest()
        header_row_idx = self._detect_customer_header_row_index(raw_bytes, filename=filename)

        parsed = read_xlsx_sheet_safely(
            raw_bytes,
            filename=filename,
            sheet_name=CUSTOMER_IMPORT_SHEET_NAME,
            header_rows=header_row_idx,
            max_data_rows=MAX_DATA_ROWS,
            max_columns=MAX_COLUMNS,
            max_cell_length=MAX_CELL_LENGTH,
            validate_all_sheets=True,
        )
        headers = parsed.get("headers") or []
        col_map = self._parse_and_validate_customer_headers(headers)

        _, lookups = self._load_customer_snapshot_lookups()

        data_rows: List[List[Any]] = parsed.get("rows") or []
        row_numbers: List[int] = parsed.get("row_numbers") or [
            header_row_idx + idx + 1 for idx in range(len(data_rows))
        ]

        def _cell(row_vals: Sequence[Any], field_key: str) -> Any:
            col_i = col_map.get(field_key)
            if col_i is None or col_i >= len(row_vals):
                return None
            return row_vals[col_i]

        seen_in_file: Dict[str, int] = {}
        preview_rows: List[Dict[str, Any]] = []
        ready_candidates: List[ValidatedCustomerCandidate] = []
        sample_rows_skipped = 0

        counts = {
            "total_rows": 0,
            "ready_count": 0,
            "duplicate_existing_count": 0,
            "duplicate_file_count": 0,
            "duplicate_count": 0,
            "invalid_missing_count": 0,
            "invalid_validation_count": 0,
            "invalid_count": 0,
        }

        for row_vals, phys_row_no in zip(data_rows, row_numbers):
            if all(_is_cell_blank(v) for v in row_vals):
                continue

            raw_name_val = _cell(row_vals, "customer_name")
            if _is_unmodified_customer_sample_row(raw_name_val):
                sample_rows_skipped += 1
                continue

            counts["total_rows"] += 1
            missing_errors: List[Dict[str, str]] = []
            validation_errors: List[Dict[str, str]] = []

            # ---- Customer Name (Mandatory) ----
            clean_name = ""
            if _is_cell_blank(raw_name_val):
                missing_errors.append(
                    {
                        "column": "Customer Name",
                        "field": "customer_name",
                        "message": "Customer Name is mandatory.",
                    }
                )
            elif isinstance(raw_name_val, bool):
                validation_errors.append(
                    {
                        "column": "Customer Name",
                        "field": "customer_name",
                        "message": "Customer Name cannot be a boolean value.",
                    }
                )
            else:
                clean_name = _normalize_whitespace(str(raw_name_val))
                if len(clean_name) > MAX_CUSTOMER_NAME_LENGTH:
                    validation_errors.append(
                        {
                            "column": "Customer Name",
                            "field": "customer_name",
                            "message": f"Customer Name exceeds maximum length of {MAX_CUSTOMER_NAME_LENGTH} characters.",
                        }
                    )

            # ---- Mobile (Optional, 10-digit Indian Mobile) ----
            raw_mobile_val = _cell(row_vals, "mobile")
            clean_mobile: Optional[str] = None
            mobile_valid = True
            if not _is_cell_blank(raw_mobile_val):
                try:
                    clean_mobile = validate_indian_mobile(
                        raw_mobile_val,
                        required=False,
                        field_name="Mobile",
                    )
                except ExcelValidationError as exc:
                    mobile_valid = False
                    validation_errors.append(
                        {
                            "column": "Mobile",
                            "field": "mobile",
                            "message": exc.message,
                        }
                    )

            # ---- Village, Taluka, District (Optional bounded text) ----
            clean_address_parts: Dict[str, Optional[str]] = {}
            address_display_parts: Dict[str, str] = {}
            for field_key, col_label in (
                ("village", "Village"),
                ("taluka", "Taluka"),
                ("district", "District"),
            ):
                raw_part = _cell(row_vals, field_key)
                if _is_cell_blank(raw_part):
                    clean_address_parts[field_key] = None
                    address_display_parts[field_key] = ""
                elif isinstance(raw_part, bool):
                    clean_address_parts[field_key] = None
                    address_display_parts[field_key] = str(raw_part)
                    validation_errors.append(
                        {
                            "column": col_label,
                            "field": field_key,
                            "message": f"{col_label} cannot be a boolean value.",
                        }
                    )
                else:
                    norm_part = _normalize_whitespace(str(raw_part))
                    address_display_parts[field_key] = norm_part
                    if len(norm_part) > MAX_CUSTOMER_ADDRESS_PART_LENGTH:
                        clean_address_parts[field_key] = None
                        validation_errors.append(
                            {
                                "column": col_label,
                                "field": field_key,
                                "message": f"{col_label} exceeds maximum length of {MAX_CUSTOMER_ADDRESS_PART_LENGTH} characters.",
                            }
                        )
                    else:
                        clean_address_parts[field_key] = norm_part

            # ---- State (Optional, defaults to Maharashtra; validated against Indian States/UTs) ----
            raw_state_val = _cell(row_vals, "state")
            clean_state = "Maharashtra"
            state_display = "Maharashtra"
            if not _is_cell_blank(raw_state_val):
                normalized_state = _normalize_indian_state(raw_state_val)
                if normalized_state is None:
                    state_display = _normalize_whitespace(str(raw_state_val))
                    validation_errors.append(
                        {
                            "column": "State",
                            "field": "state",
                            "message": (
                                f"Invalid State '{state_display}'. Must be a valid Indian State "
                                "or Union Territory (or leave blank to default to Maharashtra)."
                            ),
                        }
                    )
                else:
                    clean_state = normalized_state
                    state_display = normalized_state

            # ---- GSTIN (Optional, 15-char uppercase Indian GSTIN) ----
            raw_gstin_val = _cell(row_vals, "gstin")
            clean_gstin: Optional[str] = None
            gstin_valid = True
            if not _is_cell_blank(raw_gstin_val):
                try:
                    clean_gstin = validate_gstin(
                        raw_gstin_val,
                        required=False,
                        field_name="GSTIN",
                    )
                except ExcelValidationError as exc:
                    gstin_valid = False
                    validation_errors.append(
                        {
                            "column": "GSTIN",
                            "field": "gstin",
                            "message": exc.message,
                        }
                    )

            # ---- Customer Group (Optional, strict DB master lookup, never auto-created) ----
            raw_group_val = _cell(row_vals, "customer_group")
            group_record: Optional[Dict[str, Any]] = None
            group_display = ""
            if not _is_cell_blank(raw_group_val):
                group_display = _normalize_whitespace(str(raw_group_val))
                if isinstance(raw_group_val, bool):
                    validation_errors.append(
                        {
                            "column": "Customer Group",
                            "field": "customer_group",
                            "message": "Invalid Customer Group value.",
                        }
                    )
                else:
                    group_record = lookups.resolve_customer_group(raw_group_val)
                    if group_record is None:
                        validation_errors.append(
                            {
                                "column": "Customer Group",
                                "field": "customer_group",
                                "message": (
                                    f"Unknown or inactive Customer Group '{group_display}'. "
                                    "Must match an active Customer Group (groups are never auto-created)."
                                ),
                            }
                        )
                    else:
                        group_display = _display_english_label(group_record["group_name"])

            # ---- Row Status & Duplicate Classification ----
            all_errors = missing_errors + validation_errors
            if missing_errors:
                status = ROW_STATUS_INVALID_REQUIRED
                counts["invalid_missing_count"] += 1
                counts["invalid_count"] += 1
                message = "; ".join(e["message"] for e in all_errors)
            elif validation_errors:
                status = ROW_STATUS_INVALID_VALIDATION
                counts["invalid_validation_count"] += 1
                counts["invalid_count"] += 1
                message = "; ".join(e["message"] for e in all_errors)
            else:
                norm_key = _normalize_customer_key(clean_name)
                if norm_key in seen_in_file:
                    first_row = seen_in_file[norm_key]
                    status = ROW_STATUS_DUP_FILE
                    counts["duplicate_file_count"] += 1
                    counts["duplicate_count"] += 1
                    message = f"Repeated customer name in workbook (first appeared at Row {first_row})."
                    all_errors.append(
                        {
                            "column": "Customer Name",
                            "field": "customer_name",
                            "message": message,
                        }
                    )
                elif norm_key in lookups.existing_customers_by_key:
                    seen_in_file[norm_key] = phys_row_no
                    existing_rec = lookups.existing_customers_by_key[norm_key]
                    active_hint = (
                        "active"
                        if int(existing_rec.get("is_active") or 0) == 1
                        else "inactive"
                    )
                    status = ROW_STATUS_CUSTOMER_DUP_DB
                    counts["duplicate_existing_count"] += 1
                    counts["duplicate_count"] += 1
                    message = (
                        f"Already exists in database (Customer ID #{existing_rec['customer_id']}, {active_hint}). "
                        "Existing customers and balances are never overwritten."
                    )
                    all_errors.append(
                        {
                            "column": "Customer Name",
                            "field": "customer_name",
                            "message": message,
                        }
                    )
                else:
                    seen_in_file[norm_key] = phys_row_no
                    status = ROW_STATUS_READY
                    counts["ready_count"] += 1
                    message = "Ready to Import"
                    ready_candidates.append(
                        ValidatedCustomerCandidate(
                            row_number=phys_row_no,
                            customer_name=clean_name,
                            normalized_name=norm_key,
                            mobile=clean_mobile,
                            village=clean_address_parts["village"],
                            taluka=clean_address_parts["taluka"],
                            district=clean_address_parts["district"],
                            state=clean_state,
                            gstin=clean_gstin,
                            group_id=int(group_record["group_id"]) if group_record else None,
                            group_label=group_display,
                        )
                    )

            preview_rows.append(
                {
                    "row_number": phys_row_no,
                    "status": status,
                    "customer_name": clean_name or ("" if _is_cell_blank(raw_name_val) else str(raw_name_val)),
                    "mobile": (
                        (clean_mobile or "")
                        if mobile_valid
                        else ("" if _is_cell_blank(raw_mobile_val) else str(raw_mobile_val))
                    ),
                    "village": address_display_parts["village"],
                    "taluka": address_display_parts["taluka"],
                    "district": address_display_parts["district"],
                    "state": state_display,
                    "gstin": (
                        (clean_gstin or "")
                        if gstin_valid
                        else ("" if _is_cell_blank(raw_gstin_val) else str(raw_gstin_val))
                    ),
                    "customer_group": group_display,
                    "errors": all_errors,
                    "message": message,
                }
            )

        token_rec = self.token_store.create(
            import_kind=IMPORT_KIND_CUSTOMERS,
            user_id=int(admin_user["user_id"]),
            username=str(admin_user["username"]),
            db_key=_db_identity_key(self.db),
            file_sha256=file_sha256,
            filename=filename,
            ready_candidates=ready_candidates,
            preview_rows=preview_rows,
            summary=counts,
            sample_rows_skipped=sample_rows_skipped,
        )

        expires_iso = datetime.fromtimestamp(
            token_rec.expires_at, tz=timezone.utc
        ).isoformat()

        return {
            "success": True,
            "sheet_name": CUSTOMER_IMPORT_SHEET_NAME,
            "filename": token_rec.filename,
            "file_sha256": token_rec.file_sha256,
            "preview_token": token_rec.token,
            "expires_in_seconds": int(max(0, round(token_rec.expires_at - token_rec.created_at))),
            "expires_at": expires_iso,
            "sample_rows_skipped": sample_rows_skipped,
            "summary": counts,
            "rows": preview_rows,
        }

    def commit_customer_import(
        self,
        *,
        preview_token: str,
        file_sha256: str,
        admin_user: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """
        Validate the single-use customer preview token against user & uploaded SHA-256,
        recheck duplicates and active customer groups inside an atomic SQLite
        `BEGIN IMMEDIATE` transaction, insert ONLY `Ready to Import` customers using
        existing `Customer` + `MasterDataRepository.create_customer` with zero opening/current
        balances, zero credit limit, and `aadhar_no=None`, and roll back completely on any DB failure.
        """
        admin_uid = int(admin_user["user_id"])
        admin_username = str(admin_user["username"])
        db_key = _db_identity_key(self.db)

        record = self.token_store.validate_and_consume(
            preview_token,
            user_id=admin_uid,
            username=admin_username,
            db_key=db_key,
            file_sha256=file_sha256,
            expected_kind=IMPORT_KIND_CUSTOMERS,
        )

        pre_skipped_dup = int(record.summary.get("duplicate_count", 0))
        pre_invalid = int(record.summary.get("invalid_count", 0))
        row_messages: List[Dict[str, Any]] = []
        for prev_r in record.preview_rows:
            if prev_r.get("status") != ROW_STATUS_READY:
                row_messages.append(
                    {
                        "row_number": prev_r["row_number"],
                        "customer_name": prev_r.get("customer_name", ""),
                        "status": prev_r["status"],
                        "message": prev_r.get("message", ""),
                    }
                )

        imported_customers: List[Dict[str, Any]] = []
        concurrent_dup_skips = 0
        repo = MasterDataRepository(self.db)

        try:
            with self.db.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE;")
                cur = conn.cursor()
                try:
                    cur.execute("SELECT customer_id, customer_name, is_active FROM customers;")
                    existing_names: Dict[str, Tuple[int, int]] = {}
                    for r in cur.fetchall():
                        key = _normalize_customer_key(r["customer_name"])
                        if key and key not in existing_names:
                            existing_names[key] = (int(r["customer_id"]), int(r["is_active"] or 0))

                    cur.execute("PRAGMA table_info(customer_groups);")
                    cg_cols = {
                        str(r["name"] if isinstance(r, sqlite3.Row) else r[1]).lower()
                        for r in cur.fetchall()
                    }
                    if "is_active" in cg_cols:
                        cur.execute(
                            "SELECT group_id FROM customer_groups WHERE COALESCE(is_active, 1) = 1;"
                        )
                    else:
                        cur.execute("SELECT group_id FROM customer_groups;")
                    active_group_ids = {int(r["group_id"]) for r in cur.fetchall()}

                    for cand in record.ready_candidates:
                        if cand.normalized_name in existing_names:
                            existing_cid, _ = existing_names[cand.normalized_name]
                            concurrent_dup_skips += 1
                            row_messages.append(
                                {
                                    "row_number": cand.row_number,
                                    "customer_name": cand.customer_name,
                                    "status": ROW_STATUS_CUSTOMER_DUP_DB,
                                    "message": (
                                        f"Skipped during commit — already exists in database "
                                        f"(Customer ID #{existing_cid})."
                                    ),
                                }
                            )
                            continue

                        if cand.group_id is not None and cand.group_id not in active_group_ids:
                            raise ExcelValidationError(
                                f"Row {cand.row_number}: referenced Customer Group '{cand.group_label}' "
                                "is no longer active. Transaction rolled back cleanly.",
                                row=cand.row_number,
                                field="customer_group",
                            )

                        cust_model = Customer(
                            customer_name=cand.customer_name,
                            mobile=cand.mobile,
                            village=cand.village,
                            taluka=cand.taluka,
                            district=cand.district,
                            state=cand.state,
                            aadhar_no=None,
                            gstin=cand.gstin,
                            group_id=cand.group_id,
                            opening_balance=0.0,
                            current_balance=0.0,
                            credit_limit=0.0,
                            is_active=1,
                        )
                        new_cust_id = repo.create_customer(cust_model, conn=conn)
                        existing_names[cand.normalized_name] = (int(new_cust_id), 1)
                        imported_customers.append(
                            {
                                "row_number": cand.row_number,
                                "customer_id": int(new_cust_id),
                                "customer_name": cand.customer_name,
                            }
                        )
                        row_messages.append(
                            {
                                "row_number": cand.row_number,
                                "customer_name": cand.customer_name,
                                "status": "Imported",
                                "customer_id": int(new_cust_id),
                                "message": f"Imported successfully (Customer ID #{new_cust_id}).",
                            }
                        )

                    if imported_customers:
                        cur.execute("SELECT 1 FROM users WHERE user_id = ? LIMIT 1;", (admin_uid,))
                        audit_uid = admin_uid if cur.fetchone() is not None else None
                        cur.execute(
                            "INSERT INTO audit_log (user_id, action_type, table_name, record_id, details) VALUES (?, ?, ?, ?, ?);",
                            (
                                audit_uid,
                                "EXCEL_IMPORT",
                                "customers",
                                imported_customers[0]["customer_id"],
                                (
                                    f"Imported {len(imported_customers)} customer(s) from Excel "
                                    f"(sha256:{record.file_sha256[:12]}), skipped "
                                    f"{pre_skipped_dup + concurrent_dup_skips} duplicate(s), "
                                    f"{pre_invalid} invalid row(s)."
                                ),
                            ),
                        )
                finally:
                    cur.close()
        except DatabaseMaintenanceError:
            raise
        except ExcelValidationError:
            raise
        except (sqlite3.Error, ValueError, RuntimeError) as exc:
            # Never leak internal DB file paths, schema traces, or raw SQL errors
            raise ExcelImportTransactionError(
                "Customer import transaction failed and was rolled back completely. No customers were added or modified."
            ) from exc

        row_messages.sort(key=lambda item: int(item.get("row_number") or 0))
        total_skipped_dups = pre_skipped_dup + concurrent_dup_skips
        imported_count = len(imported_customers)

        return {
            "success": True,
            "imported_count": imported_count,
            "skipped_duplicate_count": total_skipped_dups,
            "invalid_count": pre_invalid,
            "failed_count": 0,
            "sample_rows_skipped": record.sample_rows_skipped,
            "imported_customers": imported_customers,
            "rows": row_messages,
            "message": (
                f"Imported {imported_count} customer(s); skipped {total_skipped_dups} duplicate(s) "
                f"and {pre_invalid} invalid row(s)."
            ),
        }

    # ------------------------------------------------------------------
    # Stage C3-C3: Supplier Master Import
    # ------------------------------------------------------------------
    def _load_supplier_snapshot_lookups(self) -> Tuple[Dict[str, Any], _SupplierLookups]:
        """
        Read company settings and existing supplier names inside one consistent
        BEGIN DEFERRED snapshot and close the connection immediately.
        """
        conn = self.db.get_connection()
        cursor = None
        try:
            conn.execute("BEGIN DEFERRED;")
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    company_name, owner_name, address, city, state, pincode,
                    mobile, email, gstin
                FROM company_settings
                WHERE setting_id = 1;
                """
            )
            comp_row = cursor.fetchone()
            company_settings: Dict[str, Any] = (
                dict(comp_row)
                if comp_row
                else {"company_name": "Krushidhan Agri-Input Shop ERP", "state": "Maharashtra"}
            )

            cursor.execute(
                """
                SELECT supplier_id, supplier_name, is_active
                FROM suppliers;
                """
            )
            existing_suppliers = [dict(r) for r in cursor.fetchall()]

            conn.rollback()
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            conn.close()

        return company_settings, _SupplierLookups(existing_suppliers=existing_suppliers)

    def generate_supplier_import_template(
        self,
        *,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the ADMIN-only downloadable `.xlsx` Supplier Import Template
        with a single worksheet named `Supplier Import`, mandatory/optional
        instructions, zero formulas, and one marked sample row.
        Zero temporary files written to disk.
        """
        company_settings, _ = self._load_supplier_snapshot_lookups()
        num_cols = len(_SUPPLIER_TEMPLATE_HEADERS)

        wb = create_workbook(SUPPLIER_IMPORT_SHEET_NAME)
        ws = add_sheet(wb, SUPPLIER_IMPORT_SHEET_NAME)

        row_ptr = add_company_header(
            ws,
            company_settings,
            start_row=1,
            col_span=num_cols,
            include_licences=False,
        )
        row_ptr = add_report_banner(
            ws,
            "Supplier Master Import Template",
            generated_by=generated_by,
            extra_meta={
                "Sheet": SUPPLIER_IMPORT_SHEET_NAME,
                "Mode": "Validated Master Data Import",
            },
            start_row=row_ptr,
            col_span=num_cols,
        )

        row_ptr = _write_instruction_row(
            ws,
            row_ptr,
            "Mandatory Field: Supplier Name *. Optional Fields: Contact Person, Mobile (10-digit Indian mobile), Email, Address, City, State (defaults to Maharashtra if blank), GSTIN (15-character uppercase), Licence No.",
            col_span=num_cols,
            bold=True,
        )
        row_ptr = _write_instruction_row(
            ws,
            row_ptr,
            "Formatting Rules: State defaults to Maharashtra when blank (or enter a valid Indian State / Union Territory). Mobile must be 10 digits starting with 6-9. GSTIN must be 15 characters (e.g. 27AABCS1429B1ZS). Supplier ID is auto-assigned.",
            col_span=num_cols,
        )
        row_ptr = _write_instruction_row(
            ws,
            row_ptr,
            "Security & Scope Rules: Never add Supplier ID, PAN, Bank Name, Bank Account Number, IFSC, UPI ID, Opening/Current Balance, Outstanding Amount, Payment History, Pincode, Village/Taluka/District, Supplier Category, Passwords, or Excel formulas. Sample row below is skipped automatically unless edited into a real supplier.",
            col_span=num_cols,
        )

        header_row_idx = row_ptr
        sample_row_idx = write_table_header(ws, _SUPPLIER_TEMPLATE_HEADERS, row_idx=header_row_idx)

        write_text_cell(ws, sample_row_idx, 1, TEMPLATE_SAMPLE_SUPPLIER_NAME, font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 2, "Rajesh Kulkarni", font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 3, "9822112233", alignment=_ALIGN_CENTER, font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 4, "orders@sahyadriagro.example", font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 5, "Plot 14, APMC Market Yard", font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 6, "Pune", font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 7, "Maharashtra", font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 8, "27AABCS1429B1ZS", alignment=_ALIGN_CENTER, font=_SAMPLE_FONT)
        write_text_cell(ws, sample_row_idx, 9, "MH-FCO-2026-001", alignment=_ALIGN_CENTER, font=_SAMPLE_FONT)
        for col_i in range(1, num_cols + 1):
            ws.cell(row=sample_row_idx, column=col_i).fill = _SAMPLE_FILL
        ws.row_dimensions[sample_row_idx].height = 20

        # Pre-format Mobile, GSTIN, and Licence No entry cells below as '@' text so Excel retains formatting
        for entry_row in range(sample_row_idx + 1, sample_row_idx + 101):
            ws.cell(row=entry_row, column=3).number_format = "@"
            ws.cell(row=entry_row, column=8).number_format = "@"
            ws.cell(row=entry_row, column=9).number_format = "@"

        apply_freeze_panes(ws, header_row=header_row_idx, freeze_col=1)
        auto_fit_columns(
            ws,
            min_width=14,
            max_width=40,
        )
        ws.column_dimensions["A"].width = max(ws.column_dimensions["A"].width or 0, 38)
        ws.column_dimensions["D"].width = max(ws.column_dimensions["D"].width or 0, 26)
        ws.column_dimensions["E"].width = max(ws.column_dimensions["E"].width or 0, 28)

        return save_workbook_to_bytes(wb), SUPPLIER_IMPORT_TEMPLATE_FILENAME

    @staticmethod
    def _detect_supplier_header_row_index(raw_bytes: bytes, *, filename: str) -> int:
        with safe_xlsx_workbook(raw_bytes, filename=filename, read_only=True) as wb:
            sheetnames = list(wb.sheetnames or [])
            if not sheetnames:
                raise ExcelValidationError("Workbook contains no worksheets.")
            if SUPPLIER_IMPORT_SHEET_NAME not in sheetnames:
                raise ExcelValidationError(
                    f"Requested worksheet '{SUPPLIER_IMPORT_SHEET_NAME}' was not found in workbook. "
                    "Please use the official Supplier Import Template.",
                )

            ws = wb[SUPPLIER_IMPORT_SHEET_NAME]
            first_non_empty_row: Optional[int] = None
            for phys_row_idx, row_cells in enumerate(
                ws.iter_rows(max_row=25, values_only=False), start=1
            ):
                row_vals = [getattr(c, "value", None) for c in row_cells]
                populated = [v for v in row_vals if not _is_cell_blank(v)]
                if not populated:
                    continue
                if first_non_empty_row is None:
                    first_non_empty_row = phys_row_idx

                tokens = {_normalize_header_token(v) for v in populated if isinstance(v, str)}
                if tokens & {
                    "supplier_name",
                    "company_name",
                    "company_supplier_name",
                    "distributor_name",
                    "vendor_name",
                }:
                    return phys_row_idx
                if len(populated) >= 2 and any(
                    tok in tokens
                    for tok in ("contact_person", "mobile", "email", "address", "city", "state", "gstin", "licence_no", "license_no")
                ):
                    return phys_row_idx

            if first_non_empty_row is not None:
                return first_non_empty_row
            raise ExcelValidationError(
                f"Worksheet '{SUPPLIER_IMPORT_SHEET_NAME}' is empty or missing column headers."
            )

    @staticmethod
    def _parse_and_validate_supplier_headers(headers: Sequence[str]) -> Dict[str, int]:
        if not headers or all(not str(h or "").strip() for h in headers):
            raise ExcelValidationError(
                f"Worksheet '{SUPPLIER_IMPORT_SHEET_NAME}' is missing column headers."
            )

        assert_headers_allowed_for_import(headers)

        col_map: Dict[str, int] = {}
        unknown_headers: List[str] = []

        for idx, raw_header in enumerate(headers):
            display_h = _normalize_whitespace(str(raw_header or ""))
            if not display_h:
                raise ExcelValidationError(
                    f"Blank column header found at column {idx + 1}. Remove empty columns inside the header row."
                )

            norm_token = _normalize_header_token(display_h)
            if norm_token in SUPPLIER_FORBIDDEN_HEADERS:
                raise ExcelSecurityError(
                    f"Column '{display_h}' (column {idx + 1}) is forbidden in Supplier Import "
                    "(supplier IDs, PAN, bank account/IFSC, UPI IDs, opening/current balances, "
                    "outstanding amounts, payment history, and security fields cannot be imported)."
                )

            canonical = _SUPPLIER_CANONICAL_HEADER_ALIASES.get(norm_token)
            if canonical is None:
                unknown_headers.append(display_h)
                continue

            if canonical in col_map:
                raise ExcelValidationError(
                    f"Duplicate column header '{display_h}' maps to '{canonical}'."
                )
            col_map[canonical] = idx

        if unknown_headers:
            raise ExcelValidationError(
                f"Unsupported column header(s) in '{SUPPLIER_IMPORT_SHEET_NAME}': {', '.join(unknown_headers)}. "
                "Download and use the official Supplier Import Template."
            )

        if "supplier_name" not in col_map:
            raise ExcelValidationError(
                f"Missing mandatory column header(s) in '{SUPPLIER_IMPORT_SHEET_NAME}': Supplier Name."
            )

        return col_map

    def preview_supplier_import(
        self,
        raw_bytes: bytes,
        *,
        filename: str,
        admin_user: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """
        Validate uploaded `.xlsx` supplier workbook completely in memory, check every row
        against existing supplier names (zero DB writes), and issue a bound short-lived
        preview token for `import_kind="suppliers"`.
        """
        file_sha256 = hashlib.sha256(bytes(raw_bytes)).hexdigest()
        header_row_idx = self._detect_supplier_header_row_index(raw_bytes, filename=filename)

        parsed = read_xlsx_sheet_safely(
            raw_bytes,
            filename=filename,
            sheet_name=SUPPLIER_IMPORT_SHEET_NAME,
            header_rows=header_row_idx,
            max_data_rows=MAX_DATA_ROWS,
            max_columns=MAX_COLUMNS,
            max_cell_length=MAX_CELL_LENGTH,
            validate_all_sheets=True,
        )
        headers = parsed.get("headers") or []
        col_map = self._parse_and_validate_supplier_headers(headers)

        _, lookups = self._load_supplier_snapshot_lookups()

        data_rows: List[List[Any]] = parsed.get("rows") or []
        row_numbers: List[int] = parsed.get("row_numbers") or [
            header_row_idx + idx + 1 for idx in range(len(data_rows))
        ]

        def _cell(row_vals: Sequence[Any], field_key: str) -> Any:
            col_i = col_map.get(field_key)
            if col_i is None or col_i >= len(row_vals):
                return None
            return row_vals[col_i]

        seen_in_file: Dict[str, int] = {}
        preview_rows: List[Dict[str, Any]] = []
        ready_candidates: List[ValidatedSupplierCandidate] = []
        sample_rows_skipped = 0

        counts = {
            "total_rows": 0,
            "ready_count": 0,
            "duplicate_existing_count": 0,
            "duplicate_file_count": 0,
            "duplicate_count": 0,
            "invalid_missing_count": 0,
            "invalid_validation_count": 0,
            "invalid_count": 0,
        }

        for row_vals, phys_row_no in zip(data_rows, row_numbers):
            if all(_is_cell_blank(v) for v in row_vals):
                continue

            raw_name_val = _cell(row_vals, "supplier_name")
            if _is_unmodified_supplier_sample_row(raw_name_val):
                sample_rows_skipped += 1
                continue

            counts["total_rows"] += 1
            missing_errors: List[Dict[str, str]] = []
            validation_errors: List[Dict[str, str]] = []

            # ---- Supplier Name (Mandatory) ----
            clean_name = ""
            if _is_cell_blank(raw_name_val):
                missing_errors.append(
                    {
                        "column": "Supplier Name",
                        "field": "supplier_name",
                        "message": "Supplier Name is mandatory.",
                    }
                )
            elif isinstance(raw_name_val, bool):
                validation_errors.append(
                    {
                        "column": "Supplier Name",
                        "field": "supplier_name",
                        "message": "Supplier Name cannot be a boolean value.",
                    }
                )
            else:
                clean_name = _normalize_whitespace(str(raw_name_val))
                if len(clean_name) > MAX_SUPPLIER_NAME_LENGTH:
                    validation_errors.append(
                        {
                            "column": "Supplier Name",
                            "field": "supplier_name",
                            "message": f"Supplier Name exceeds maximum length of {MAX_SUPPLIER_NAME_LENGTH} characters.",
                        }
                    )

            # ---- Contact Person (Optional bounded text) ----
            raw_contact_val = _cell(row_vals, "contact_person")
            clean_contact: Optional[str] = None
            contact_display = ""
            if not _is_cell_blank(raw_contact_val):
                if isinstance(raw_contact_val, bool):
                    contact_display = str(raw_contact_val)
                    validation_errors.append(
                        {
                            "column": "Contact Person",
                            "field": "contact_person",
                            "message": "Contact Person cannot be a boolean value.",
                        }
                    )
                else:
                    clean_contact = _normalize_whitespace(str(raw_contact_val))
                    contact_display = clean_contact
                    if len(clean_contact) > MAX_SUPPLIER_CONTACT_LENGTH:
                        clean_contact = None
                        validation_errors.append(
                            {
                                "column": "Contact Person",
                                "field": "contact_person",
                                "message": f"Contact Person exceeds maximum length of {MAX_SUPPLIER_CONTACT_LENGTH} characters.",
                            }
                        )

            # ---- Mobile (Optional, 10-digit Indian Mobile) ----
            raw_mobile_val = _cell(row_vals, "mobile")
            clean_mobile: Optional[str] = None
            mobile_valid = True
            if not _is_cell_blank(raw_mobile_val):
                try:
                    clean_mobile = validate_indian_mobile(
                        raw_mobile_val,
                        required=False,
                        field_name="Mobile",
                    )
                except ExcelValidationError as exc:
                    mobile_valid = False
                    validation_errors.append(
                        {
                            "column": "Mobile",
                            "field": "mobile",
                            "message": exc.message,
                        }
                    )

            # ---- Email (Optional, standard email format) ----
            raw_email_val = _cell(row_vals, "email")
            clean_email: Optional[str] = None
            email_valid = True
            if not _is_cell_blank(raw_email_val):
                try:
                    clean_email = validate_email(
                        raw_email_val,
                        required=False,
                        field_name="Email",
                    )
                    if clean_email and len(clean_email) > MAX_SUPPLIER_EMAIL_LENGTH:
                        email_valid = False
                        validation_errors.append(
                            {
                                "column": "Email",
                                "field": "email",
                                "message": f"Email exceeds maximum length of {MAX_SUPPLIER_EMAIL_LENGTH} characters.",
                            }
                        )
                        clean_email = None
                except ExcelValidationError as exc:
                    email_valid = False
                    validation_errors.append(
                        {
                            "column": "Email",
                            "field": "email",
                            "message": exc.message,
                        }
                    )

            # ---- Address & City (Optional bounded text) ----
            clean_addr_city: Dict[str, Optional[str]] = {"address": None, "city": None}
            display_addr_city: Dict[str, str] = {"address": "", "city": ""}
            for field_key, col_label, max_len in (
                ("address", "Address", MAX_SUPPLIER_ADDRESS_LENGTH),
                ("city", "City", MAX_SUPPLIER_CITY_LENGTH),
            ):
                raw_part = _cell(row_vals, field_key)
                if not _is_cell_blank(raw_part):
                    if isinstance(raw_part, bool):
                        display_addr_city[field_key] = str(raw_part)
                        validation_errors.append(
                            {
                                "column": col_label,
                                "field": field_key,
                                "message": f"{col_label} cannot be a boolean value.",
                            }
                        )
                    else:
                        norm_part = _normalize_whitespace(str(raw_part))
                        display_addr_city[field_key] = norm_part
                        if len(norm_part) > max_len:
                            validation_errors.append(
                                {
                                    "column": col_label,
                                    "field": field_key,
                                    "message": f"{col_label} exceeds maximum length of {max_len} characters.",
                                }
                            )
                        else:
                            clean_addr_city[field_key] = norm_part

            # ---- State (Optional, defaults to Maharashtra; validated against Indian States/UTs) ----
            raw_state_val = _cell(row_vals, "state")
            clean_state = "Maharashtra"
            state_display = "Maharashtra"
            if not _is_cell_blank(raw_state_val):
                normalized_state = _normalize_indian_state(raw_state_val)
                if normalized_state is None:
                    state_display = _normalize_whitespace(str(raw_state_val))
                    validation_errors.append(
                        {
                            "column": "State",
                            "field": "state",
                            "message": (
                                f"Invalid State '{state_display}'. Must be a valid Indian State "
                                "or Union Territory (or leave blank to default to Maharashtra)."
                            ),
                        }
                    )
                else:
                    clean_state = normalized_state
                    state_display = normalized_state

            # ---- GSTIN (Optional, 15-char uppercase Indian GSTIN) ----
            raw_gstin_val = _cell(row_vals, "gstin")
            clean_gstin: Optional[str] = None
            gstin_valid = True
            if not _is_cell_blank(raw_gstin_val):
                try:
                    clean_gstin = validate_gstin(
                        raw_gstin_val,
                        required=False,
                        field_name="GSTIN",
                    )
                except ExcelValidationError as exc:
                    gstin_valid = False
                    validation_errors.append(
                        {
                            "column": "GSTIN",
                            "field": "gstin",
                            "message": exc.message,
                        }
                    )

            # ---- Licence No / dl_number (Optional bounded text or non-negative integer) ----
            raw_dl_val = _cell(row_vals, "dl_number")
            clean_dl: Optional[str] = None
            dl_display = ""
            if not _is_cell_blank(raw_dl_val):
                if isinstance(raw_dl_val, bool):
                    dl_display = str(raw_dl_val)
                    validation_errors.append(
                        {
                            "column": "Licence No",
                            "field": "dl_number",
                            "message": "Licence No cannot be a boolean value.",
                        }
                    )
                elif isinstance(raw_dl_val, float):
                    dl_display = str(raw_dl_val)
                    if not raw_dl_val.is_integer() or raw_dl_val < 0:
                        validation_errors.append(
                            {
                                "column": "Licence No",
                                "field": "dl_number",
                                "message": "Invalid Licence No numeric value.",
                            }
                        )
                    else:
                        clean_dl = str(int(raw_dl_val))
                        dl_display = clean_dl
                elif isinstance(raw_dl_val, int):
                    if raw_dl_val < 0:
                        dl_display = str(raw_dl_val)
                        validation_errors.append(
                            {
                                "column": "Licence No",
                                "field": "dl_number",
                                "message": "Licence No cannot be negative.",
                            }
                        )
                    else:
                        clean_dl = str(raw_dl_val)
                        dl_display = clean_dl
                else:
                    norm_dl = _normalize_whitespace(str(raw_dl_val))
                    dl_display = norm_dl
                    if len(norm_dl) > MAX_SUPPLIER_LICENCE_LENGTH:
                        validation_errors.append(
                            {
                                "column": "Licence No",
                                "field": "dl_number",
                                "message": f"Licence No exceeds maximum length of {MAX_SUPPLIER_LICENCE_LENGTH} characters.",
                            }
                        )
                    else:
                        clean_dl = norm_dl

            # ---- Row Status & Duplicate Classification ----
            all_errors = missing_errors + validation_errors
            if missing_errors:
                status = ROW_STATUS_INVALID_REQUIRED
                counts["invalid_missing_count"] += 1
                counts["invalid_count"] += 1
                message = "; ".join(e["message"] for e in all_errors)
            elif validation_errors:
                status = ROW_STATUS_INVALID_VALIDATION
                counts["invalid_validation_count"] += 1
                counts["invalid_count"] += 1
                message = "; ".join(e["message"] for e in all_errors)
            else:
                norm_key = _normalize_supplier_key(clean_name)
                if norm_key in seen_in_file:
                    first_row = seen_in_file[norm_key]
                    status = ROW_STATUS_DUP_FILE
                    counts["duplicate_file_count"] += 1
                    counts["duplicate_count"] += 1
                    message = f"Repeated supplier name in workbook (first appeared at Row {first_row})."
                    all_errors.append(
                        {
                            "column": "Supplier Name",
                            "field": "supplier_name",
                            "message": message,
                        }
                    )
                elif norm_key in lookups.existing_suppliers_by_key:
                    seen_in_file[norm_key] = phys_row_no
                    existing_rec = lookups.existing_suppliers_by_key[norm_key]
                    active_hint = (
                        "active"
                        if int(existing_rec.get("is_active") or 0) == 1
                        else "inactive"
                    )
                    status = ROW_STATUS_SUPPLIER_DUP_DB
                    counts["duplicate_existing_count"] += 1
                    counts["duplicate_count"] += 1
                    message = (
                        f"Already exists in database (Supplier ID #{existing_rec['supplier_id']}, {active_hint}). "
                        "Existing suppliers and balances are never overwritten."
                    )
                    all_errors.append(
                        {
                            "column": "Supplier Name",
                            "field": "supplier_name",
                            "message": message,
                        }
                    )
                else:
                    seen_in_file[norm_key] = phys_row_no
                    status = ROW_STATUS_READY
                    counts["ready_count"] += 1
                    message = "Ready to Import"
                    ready_candidates.append(
                        ValidatedSupplierCandidate(
                            row_number=phys_row_no,
                            supplier_name=clean_name,
                            normalized_name=norm_key,
                            contact_person=clean_contact,
                            mobile=clean_mobile,
                            email=clean_email,
                            address=clean_addr_city["address"],
                            city=clean_addr_city["city"],
                            state=clean_state,
                            gstin=clean_gstin,
                            dl_number=clean_dl,
                        )
                    )

            preview_rows.append(
                {
                    "row_number": phys_row_no,
                    "status": status,
                    "supplier_name": clean_name or ("" if _is_cell_blank(raw_name_val) else str(raw_name_val)),
                    "contact_person": contact_display,
                    "mobile": (
                        (clean_mobile or "")
                        if mobile_valid
                        else ("" if _is_cell_blank(raw_mobile_val) else str(raw_mobile_val))
                    ),
                    "email": (
                        (clean_email or "")
                        if email_valid
                        else ("" if _is_cell_blank(raw_email_val) else str(raw_email_val))
                    ),
                    "address": display_addr_city["address"],
                    "city": display_addr_city["city"],
                    "state": state_display,
                    "gstin": (
                        (clean_gstin or "")
                        if gstin_valid
                        else ("" if _is_cell_blank(raw_gstin_val) else str(raw_gstin_val))
                    ),
                    "dl_number": dl_display,
                    "errors": all_errors,
                    "message": message,
                }
            )

        token_rec = self.token_store.create(
            import_kind=IMPORT_KIND_SUPPLIERS,
            user_id=int(admin_user["user_id"]),
            username=str(admin_user["username"]),
            db_key=_db_identity_key(self.db),
            file_sha256=file_sha256,
            filename=filename,
            ready_candidates=ready_candidates,
            preview_rows=preview_rows,
            summary=counts,
            sample_rows_skipped=sample_rows_skipped,
        )

        expires_iso = datetime.fromtimestamp(
            token_rec.expires_at, tz=timezone.utc
        ).isoformat()

        return {
            "success": True,
            "sheet_name": SUPPLIER_IMPORT_SHEET_NAME,
            "filename": token_rec.filename,
            "file_sha256": token_rec.file_sha256,
            "preview_token": token_rec.token,
            "expires_in_seconds": int(max(0, round(token_rec.expires_at - token_rec.created_at))),
            "expires_at": expires_iso,
            "sample_rows_skipped": sample_rows_skipped,
            "summary": counts,
            "rows": preview_rows,
        }

    def commit_supplier_import(
        self,
        *,
        preview_token: str,
        file_sha256: str,
        admin_user: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """
        Validate the single-use supplier preview token against user & uploaded SHA-256,
        recheck duplicate supplier names inside an atomic SQLite `BEGIN IMMEDIATE` transaction,
        insert ONLY `Ready to Import` suppliers using existing `Supplier` +
        `MasterDataRepository.create_supplier` with zero opening/current balances and
        null PAN/bank fields, and roll back completely on any DB failure.
        """
        admin_uid = int(admin_user["user_id"])
        admin_username = str(admin_user["username"])
        db_key = _db_identity_key(self.db)

        record = self.token_store.validate_and_consume(
            preview_token,
            user_id=admin_uid,
            username=admin_username,
            db_key=db_key,
            file_sha256=file_sha256,
            expected_kind=IMPORT_KIND_SUPPLIERS,
        )

        pre_skipped_dup = int(record.summary.get("duplicate_count", 0))
        pre_invalid = int(record.summary.get("invalid_count", 0))
        row_messages: List[Dict[str, Any]] = []
        for prev_r in record.preview_rows:
            if prev_r.get("status") != ROW_STATUS_READY:
                row_messages.append(
                    {
                        "row_number": prev_r["row_number"],
                        "supplier_name": prev_r.get("supplier_name", ""),
                        "status": prev_r["status"],
                        "message": prev_r.get("message", ""),
                    }
                )

        imported_suppliers: List[Dict[str, Any]] = []
        concurrent_dup_skips = 0
        repo = MasterDataRepository(self.db)

        try:
            with self.db.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE;")
                cur = conn.cursor()
                try:
                    cur.execute("SELECT supplier_id, supplier_name, is_active FROM suppliers;")
                    existing_names: Dict[str, Tuple[int, int]] = {}
                    for r in cur.fetchall():
                        key = _normalize_supplier_key(r["supplier_name"])
                        if key and key not in existing_names:
                            existing_names[key] = (int(r["supplier_id"]), int(r["is_active"] or 0))

                    for cand in record.ready_candidates:
                        if cand.normalized_name in existing_names:
                            existing_sid, _ = existing_names[cand.normalized_name]
                            concurrent_dup_skips += 1
                            row_messages.append(
                                {
                                    "row_number": cand.row_number,
                                    "supplier_name": cand.supplier_name,
                                    "status": ROW_STATUS_SUPPLIER_DUP_DB,
                                    "message": (
                                        f"Skipped during commit — already exists in database "
                                        f"(Supplier ID #{existing_sid})."
                                    ),
                                }
                            )
                            continue

                        supp_model = Supplier(
                            supplier_name=cand.supplier_name,
                            contact_person=cand.contact_person,
                            mobile=cand.mobile,
                            email=cand.email,
                            address=cand.address,
                            city=cand.city,
                            state=cand.state,
                            gstin=cand.gstin,
                            pan=None,
                            dl_number=cand.dl_number,
                            bank_name=None,
                            account_no=None,
                            ifsc_code=None,
                            opening_balance=0.0,
                            current_balance=0.0,
                            is_active=1,
                        )
                        new_supp_id = repo.create_supplier(supp_model, conn=conn)
                        existing_names[cand.normalized_name] = (int(new_supp_id), 1)
                        imported_suppliers.append(
                            {
                                "row_number": cand.row_number,
                                "supplier_id": int(new_supp_id),
                                "supplier_name": cand.supplier_name,
                            }
                        )
                        row_messages.append(
                            {
                                "row_number": cand.row_number,
                                "supplier_name": cand.supplier_name,
                                "status": "Imported",
                                "supplier_id": int(new_supp_id),
                                "message": f"Imported successfully (Supplier ID #{new_supp_id}).",
                            }
                        )

                    if imported_suppliers:
                        cur.execute("SELECT 1 FROM users WHERE user_id = ? LIMIT 1;", (admin_uid,))
                        audit_uid = admin_uid if cur.fetchone() is not None else None
                        cur.execute(
                            "INSERT INTO audit_log (user_id, action_type, table_name, record_id, details) VALUES (?, ?, ?, ?, ?);",
                            (
                                audit_uid,
                                "EXCEL_IMPORT",
                                "suppliers",
                                imported_suppliers[0]["supplier_id"],
                                (
                                    f"Imported {len(imported_suppliers)} supplier(s) from Excel "
                                    f"(sha256:{record.file_sha256[:12]}), skipped "
                                    f"{pre_skipped_dup + concurrent_dup_skips} duplicate(s), "
                                    f"{pre_invalid} invalid row(s)."
                                ),
                            ),
                        )
                finally:
                    cur.close()
        except DatabaseMaintenanceError:
            raise
        except ExcelValidationError:
            raise
        except (sqlite3.Error, ValueError, RuntimeError) as exc:
            # Never leak internal DB file paths, schema traces, or raw SQL errors
            raise ExcelImportTransactionError(
                "Supplier import transaction failed and was rolled back completely. No suppliers were added or modified."
            ) from exc

        row_messages.sort(key=lambda item: int(item.get("row_number") or 0))
        total_skipped_dups = pre_skipped_dup + concurrent_dup_skips
        imported_count = len(imported_suppliers)

        return {
            "success": True,
            "imported_count": imported_count,
            "skipped_duplicate_count": total_skipped_dups,
            "invalid_count": pre_invalid,
            "failed_count": 0,
            "sample_rows_skipped": record.sample_rows_skipped,
            "imported_suppliers": imported_suppliers,
            "rows": row_messages,
            "message": (
                f"Imported {imported_count} supplier(s); skipped {total_skipped_dups} duplicate(s) "
                f"and {pre_invalid} invalid row(s)."
            ),
        }
