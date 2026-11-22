"""
Excel Export Service for Krushidhan Agri-Input Shop ERP (Stages C3-B2A, C3-B2B, C3-B3A, C3-B3B & C3-B3C).
Implements Excel (.xlsx) exports:
1. Product Catalog
2. Current Stock Summary
3. Batch-wise Stock & Expiry
4. Stock Ledger (with optional validated date range filters)
5. Sales Register (with optional `Sales Returns` sheet when recorded returns exist)
6. Sales Item Details (historical item rates & GST; zero purchase costs/margins)
7. Purchase Register (ADMIN-only via API; historical inward bills)
8. Purchase Item Details (ADMIN-only via API; historical item cost rates & GST)
9. Customer Ledger (invoices, standalone receipt vouchers & sales returns)
10. Supplier Ledger (ADMIN-only via API; inward bills & payment vouchers)
11. Customer Outstanding (actual party ledger balances from `customers.current_balance`)
12. Supplier Outstanding (ADMIN-only via API; actual balances from `suppliers.current_balance`)
13. Payment History (ADMIN-only via API; deduplicated inflows/outflows with historical UPI labels)
14. Expenses (ADMIN-only via API; direct expense records without voucher duplication)
15. Customer Master (sanitized customer directory excluding Aadhaar and ledger balances)
16. Supplier Master (sanitized supplier directory excluding PAN, bank/IFSC, and balances)
17. GST Summary (ADMIN-only via API; outward GST summary, tax-rate breakdown, return & HSN disclosures)
18. GSTR-1 Accountant Working Paper (ADMIN-only via API; internal working paper, B2B/B2C/returns separation)
19. Statutory Registers (ADMIN-only via API; FCO fertilizer, pesticide, seed & ERP inspection registers)
20. Profit & Loss Workbook (ADMIN-only via API; ERP P&L parity with mandatory returns disclosure)

Security & Reliability Guarantees:
- Reads a single consistent SQLite snapshot (BEGIN DEFERRED -> rollback -> close)
  and closes database connections BEFORE constructing/styling/serializing Workbooks.
- Zero temporary files written to disk (strictly in-memory io.BytesIO).
- Excludes Aadhaar, PAN, bank account/IFSC, UPI IDs, passwords, and JWT/auth secrets
  across all workbooks, and excludes purchase costs/margins from Product/Stock/Sales exports.
- Zero Excel formulas (=SUM, etc.) anywhere in cells or totals; backend Decimal
  calculations are written as explicit typed numeric/date/text cells.
- All text fields sanitized against spreadsheet formula injection and formatted '@'.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from src.db.connection import DatabaseManager, get_db_manager
from src.services.excel_security import (
    ExcelValidationError,
    sanitize_excel_text,
    validate_date_range,
    validate_date_str,
)
from src.services.excel_workbook_builder import (
    BODY_FONT,
    TOTALS_BORDER,
    TOTALS_FILL,
    TOTALS_FONT,
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

MAX_EXPORT_ROWS = 50000
MAX_QUERY_FILTER_LENGTH = 200
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

_ALIGN_CENTER = Alignment(horizontal="center", vertical="center")
_ALIGN_LEFT = Alignment(horizontal="left", vertical="center")

_FONT_STATUS_OK = Font(name="Calibri", size=10, bold=True, color="166534")
_FONT_STATUS_WARN = Font(name="Calibri", size=10, bold=True, color="B45309")
_FONT_STATUS_DANGER = Font(name="Calibri", size=10, bold=True, color="B91C1C")
_FONT_STATUS_MUTED = Font(name="Calibri", size=10, bold=False, color="475569")

_NOTE_FILL = PatternFill(fill_type="solid", fgColor="FFFBEB")
_NOTE_FONT = Font(name="Calibri", size=9.5, bold=True, color="78350F")
_NOTE_SIDE = Side(style="thin", color="D97706")
_NOTE_BORDER = Border(left=_NOTE_SIDE, right=_NOTE_SIDE, top=_NOTE_SIDE, bottom=_NOTE_SIDE)

_WARN_FILL = PatternFill(fill_type="solid", fgColor="FEF2F2")
_WARN_FONT = Font(name="Calibri", size=9.5, bold=True, color="991B1B")
_WARN_SIDE = Side(style="thin", color="DC2626")
_WARN_BORDER = Border(left=_WARN_SIDE, right=_WARN_SIDE, top=_WARN_SIDE, bottom=_WARN_SIDE)

_SECTION_FILL = PatternFill(fill_type="solid", fgColor="E2E8F0")
_SECTION_FONT = Font(name="Calibri", size=10, bold=True, color="0F172A")

SALES_RECORDED_DUE_NOTE = (
    "This is the recorded invoice-level due amount. Separate customer receipt vouchers may "
    "affect the customer ledger balance without changing the historical invoice due amount. "
    "Refer to the Customer Ledger for the current party balance."
)
PURCHASE_RECORDED_DUE_NOTE = (
    "This is the recorded purchase-level due amount. Separate supplier payment vouchers may "
    "affect the supplier ledger balance without changing the historical purchase due amount. "
    "Refer to the Supplier Ledger for the current party balance."
)
CUSTOMER_LEDGER_NOTE = (
    "Transactions combine Sales Invoices (Debit = Invoice Total; Credit = Amount Paid at Billing), "
    "standalone Customer Receipt vouchers (Credit), and Sales Returns (Credit). Running balance is "
    "omitted because historical vouchers do not record opening balances or sequential balance "
    "snapshots. Refer to Customer Outstanding for actual current party balances."
)
SUPPLIER_LEDGER_NOTE = (
    "Transactions combine Inward Purchase Bills (Credit = Purchase Total; Debit = Amount Paid at "
    "Billing) and Supplier Payment vouchers (Debit). Running balance is omitted because historical "
    "vouchers do not record opening balances or sequential balance snapshots. Refer to Supplier "
    "Outstanding for actual current party balances."
)
CUSTOMER_OUTSTANDING_NOTE = (
    "Current Ledger Balance reflects customers.current_balance maintained by Billing, Sales "
    "Returns, and Customer Receipt vouchers (NOT derived from historical sales.due_amount). "
    "Sign convention: positive (+) = Debit / Receivable from customer; negative (-) = Credit / "
    "Advance balance; 0.00 = Settled."
)
SUPPLIER_OUTSTANDING_NOTE = (
    "Current Ledger Balance reflects suppliers.current_balance maintained by Inward Purchases and "
    "Supplier Payment vouchers (NOT derived from historical purchases.due_amount). Sign convention: "
    "positive (+) = Credit / Payable to supplier; negative (-) = Debit / Advance to supplier; "
    "0.00 = Settled."
)
PAYMENT_HISTORY_NOTE = (
    "Consolidated payment & receipt register across Invoice-Time Customer Receipts (sales), "
    "Customer Receipt Vouchers, Bill-Time Supplier Payments (purchases), Supplier Payment Vouchers, "
    "Shop Expense Payments (expenses), and Other Receipt/Payment/Contra Vouchers — deduplicated "
    "across linked tables. Historical 'UPI' records remain labelled 'UPI (Historical)'."
)
EXPENSES_REPORT_NOTE = (
    "Sourced directly from the expenses table (joined with expense categories, payment accounts, "
    "and linked voucher numbers where present) so expenses with linked vouchers are never "
    "double-counted. Non-cash expenses using Bank Account are labelled BANK / NON-CASH."
)
CUSTOMER_MASTER_NOTE = (
    "Read-only customer directory exported from customers and customer_groups. Aadhaar numbers, "
    "opening/current ledger balances, and credit limits are intentionally excluded (refer to "
    "Customer Outstanding for balances). Records are sorted Active-first."
)
SUPPLIER_MASTER_NOTE = (
    "Read-only supplier directory exported from suppliers. PAN numbers, bank account/IFSC "
    "details, and opening/current ledger balances are intentionally excluded (refer to "
    "Admin-only Supplier Outstanding for balances). Records are sorted Active-first."
)
GST_SUMMARY_NOTE = (
    "Summarizes gross outward taxable turnover and GST tax liability from sales and sale_items "
    "alongside the ERP Tax Rate Summary. Sales returns are disclosed separately (NOT auto-deducted). "
    "ERP HSN grouping does not split distinct GST tax slabs or distinct product names sharing an "
    "HSN/unit — see HSN Reference (Unverified) for row-level diagnostics."
)
GSTR1_WORKING_PAPER_NOTE = (
    "This workbook is an internal accountant working paper generated from Krushidhan ERP records. "
    "Please verify classifications, figures and statutory requirements with your accountant before filing."
)
HSN_LIMITATION_NOTE = (
    "UNVERIFIED ERP HSN OUTPUT — NOT ACCOUNTANT-READY: GSTService.get_hsn_summary groups solely by "
    "(products.hsn_code, units.symbol), omits GST rate differentiation, displays only one arbitrary "
    "product_name when multiple items share an HSN/unit, omits product unit fallback when "
    "sale_items.unit_id is NULL, and keeps NULL vs blank HSN separate."
)
STATUTORY_REGISTERS_NOTE = (
    "Internal compilation generated from Krushidhan ERP records for audit reference only. Does NOT "
    "constitute legal certification, statutory compliance, or guaranteed filing readiness under FCO "
    "1985, Insecticides Act 1968, Seeds Act 1966, or GST laws. Customer Aadhaar numbers are excluded. "
    "Purchase Returns (no ERP workflow), pesticide Technical Name / CIB-RC numbers (not stored in "
    "DB), and opening/closing statutory stock balances are omitted."
)
PNL_RETURN_DISCLOSURE_NOTE = (
    "This report reproduces the existing Krushidhan ERP Profit & Loss calculation. Sales returns are "
    "not fully deducted by the current P&L calculation. Review recorded sales returns separately "
    "before relying on this result for final financial reporting."
)

_SUPPLIER_ID_REF_RE = re.compile(r"Supplier\s*#(\d+)\b", re.IGNORECASE)
_CUSTOMER_ID_REF_RE = re.compile(r"Customer\s*#(\d+)\b", re.IGNORECASE)
_PAID_VIA_REF_RE = re.compile(r"Paid\s+via\s+([A-Za-z0-9_ -]+?)\s+ref:\s*(.*)$", re.IGNORECASE)
_MODE_COLON_RE = re.compile(r"\bMode:\s*([A-Za-z0-9_ -]+?)(?:\s*$|\s*[-|,])", re.IGNORECASE)
_NARRATION_MODE_TOKEN_RE = re.compile(
    r"\b(UPI|BANK[\s_]+TRANSFER|NEFT|RTGS|IMPS|CHEQUE|CARD|CASH|SPLIT)\b", re.IGNORECASE
)


def _add_explanatory_note_banner(
    ws,
    note_text: str,
    *,
    row_idx: int,
    col_span: int,
    is_warning: bool = False,
    row_height: Optional[int] = None,
) -> int:
    """Render a clearly visible highlighted explanatory notice strip across the worksheet width."""
    span = max(1, int(col_span))
    ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=span)
    safe_note = sanitize_excel_text(f"Note: {note_text}")
    cell = ws.cell(row=row_idx, column=1, value=safe_note)
    cell.data_type = "s"
    cell.number_format = "@"
    cell.font = _WARN_FONT if is_warning else _NOTE_FONT
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=1, wrap_text=True)
    fill = _WARN_FILL if is_warning else _NOTE_FILL
    border = _WARN_BORDER if is_warning else _NOTE_BORDER
    for col_i in range(1, span + 1):
        c = ws.cell(row=row_idx, column=col_i)
        c.fill = fill
        c.border = border
    if row_height is not None:
        ws.row_dimensions[row_idx].height = row_height
    else:
        approx_chars_per_line = max(55, span * 15)
        est_lines = max(1, math.ceil(len(safe_note) / approx_chars_per_line))
        ws.row_dimensions[row_idx].height = max(30, min(80, 12 + est_lines * 14))
    return row_idx + 1


def normalize_optional_date_filters(
    from_date: Optional[str],
    to_date: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """
    Validate optional YYYY-MM-DD export date filters:
    - Treats None or exact empty string '' as omitted (no bound).
    - Rejects malformed calendar dates, whitespace strings, or from_date > to_date
      with ExcelValidationError (HTTP 422).
    """
    norm_from: Optional[str] = None
    norm_to: Optional[str] = None
    if from_date is not None and from_date != "":
        norm_from = validate_date_str(from_date, field_name="from_date")
    if to_date is not None and to_date != "":
        norm_to = validate_date_str(to_date, field_name="to_date")
    if norm_from is not None and norm_to is not None:
        norm_from, norm_to = validate_date_range(norm_from, norm_to)
    return norm_from, norm_to


def _validate_positive_id(value: Optional[int], field_name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExcelValidationError(
            f"Invalid {field_name}: must be a positive integer.",
            field=field_name,
        )
    return value


def _validate_search_query(query: Optional[str], *, field_name: str = "query") -> str:
    if query is None:
        return ""
    if not isinstance(query, str):
        raise ExcelValidationError(f"Invalid {field_name} filter.", field=field_name)
    cleaned = _CONTROL_CHAR_RE.sub("", query).strip()
    if len(cleaned) > MAX_QUERY_FILTER_LENGTH:
        label = "Search query" if field_name == "query" else f"Filter '{field_name}'"
        raise ExcelValidationError(
            f"{label} exceeds maximum length of {MAX_QUERY_FILTER_LENGTH} characters.",
            field=field_name,
        )
    return cleaned


def _escape_like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _to_decimal(val: Any) -> Decimal:
    if val is None or val == "":
        return Decimal("0")
    return Decimal(str(val))


def _write_int_cell(ws, row: int, col: int, value: int, *, font: Optional[Font] = None):
    cell = write_quantity_cell(ws, row, col, int(value), decimals=0, number_format="0", font=font)
    cell.alignment = _ALIGN_CENTER
    return cell


def _configure_landscape_print(ws) -> None:
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0


def _add_section_subbanner(ws, title: str, *, row_idx: int, col_span: int) -> int:
    """Render a clean slate-grey section header bar above stacked summary tables."""
    span = max(1, int(col_span))
    ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=span)
    cell = ws.cell(row=row_idx, column=1, value=sanitize_excel_text(title))
    cell.data_type = "s"
    cell.number_format = "@"
    cell.font = _SECTION_FONT
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    for col_i in range(1, span + 1):
        c = ws.cell(row=row_idx, column=col_i)
        c.fill = _SECTION_FILL
        c.border = _NOTE_BORDER if False else Border(
            left=Side(style="thin", color="94A3B8"),
            right=Side(style="thin", color="94A3B8"),
            top=Side(style="thin", color="94A3B8"),
            bottom=Side(style="thin", color="94A3B8"),
        )
    ws.row_dimensions[row_idx].height = 20
    return row_idx + 1


def _resolve_status_font(status_label: str) -> Font:
    if status_label in (
        "Out of Stock",
        "Expired",
        "Credit / Unpaid",
        "Debit (Receivable)",
        "Credit (Payable)",
        "Outflow (Payment)",
        "Not Supported (Omitted)",
        "Unverified / Limitation",
    ):
        return _FONT_STATUS_DANGER
    if status_label in (
        "Low Stock",
        "Expiring Soon (<=90d)",
        "Partial",
        "Partial (Schema Gap)",
        "Credit (Advance)",
        "Debit (Advance)",
    ):
        return _FONT_STATUS_WARN
    if status_label in (
        "In Stock",
        "Active",
        "Paid",
        "Settled (Nil)",
        "Inflow (Receipt)",
        "Recorded",
        "Recorded (Voucher Linked)",
        "Supported",
    ):
        return _FONT_STATUS_OK
    return _FONT_STATUS_MUTED


def _validate_payment_mode_filter(
    value: Optional[str], *, field_name: str = "payment_mode"
) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ExcelValidationError(f"Invalid {field_name} filter.", field=field_name)
    cleaned = _CONTROL_CHAR_RE.sub("", value).strip()
    if not cleaned:
        return None
    if len(cleaned) > 40:
        raise ExcelValidationError(
            f"Invalid {field_name}: exceeds maximum length of 40 characters.",
            field=field_name,
        )
    return re.sub(r"\s+", "_", cleaned.upper())


def _normalize_mode_token(mode: Optional[str]) -> str:
    if not mode:
        return "CASH"
    normalized = re.sub(r"[\s-]+", "_", str(mode).strip().upper())
    if normalized in ("NEFT", "RTGS", "IMPS", "BANK"):
        return "BANK_TRANSFER"
    return normalized


def _format_payment_mode_label(mode: Optional[str]) -> str:
    if not mode:
        return "CASH"
    normalized = _normalize_mode_token(mode)
    mapping = {
        "CASH": "CASH",
        "CREDIT": "CREDIT",
        "BANK_TRANSFER": "BANK TRANSFER",
        "BANK_NON_CASH": "BANK / NON-CASH",
        "CHEQUE": "CHEQUE",
        "CARD": "CARD",
        "SPLIT": "SPLIT",
        "UPI": "UPI (Historical)",
    }
    return mapping.get(normalized, normalized.replace("_", " "))


def _safe_currency_decimal(val: Any) -> Decimal:
    """Convert raw numeric balance to Decimal without changing non-zero values; avoid -0.00 artifacts."""
    dec = _to_decimal(val)
    if dec == Decimal("0"):
        return Decimal("0.00")
    return dec


def _classify_customer_balance(bal: Decimal) -> str:
    if bal > Decimal("0.005"):
        return "Debit (Receivable)"
    if bal < Decimal("-0.005"):
        return "Credit (Advance)"
    return "Settled (Nil)"


def _classify_supplier_balance(bal: Decimal) -> str:
    if bal > Decimal("0.005"):
        return "Credit (Payable)"
    if bal < Decimal("-0.005"):
        return "Debit (Advance)"
    return "Settled (Nil)"


def _derive_expense_payment_mode(account_name: Optional[str]) -> Tuple[str, str]:
    """Return (raw_mode_token, display_label) strictly from expenses.payment_account_id ledger name."""
    acc = (account_name or "").strip().lower()
    if acc == "cash in hand" or "cash" in acc:
        return "CASH", "CASH"
    if acc:
        return "BANK_NON_CASH", "BANK / NON-CASH"
    return "CASH", "CASH"


def _group_voucher_rows(raw_rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Collapse joined (vouchers JOIN ledger_entries JOIN ledger_accounts) rows into one dict
    per voucher_id so multi-leg double-entry rows are NEVER duplicated.
    """
    grouped: Dict[int, Dict[str, Any]] = {}
    ordered_ids: List[int] = []
    for r in raw_rows:
        vid = int(r["voucher_id"])
        if vid not in grouped:
            ordered_ids.append(vid)
            grouped[vid] = {
                "voucher_id": vid,
                "voucher_no": r.get("voucher_no") or "",
                "voucher_date": r.get("voucher_date") or "",
                "voucher_type": (r.get("voucher_type") or "").upper(),
                "total_amount": r.get("total_amount"),
                "narration": r.get("narration") or "",
                "reference_type": (r.get("reference_type") or "").upper(),
                "reference_id": r.get("reference_id"),
                "account_names": [],
                "particulars_list": [],
            }
        v = grouped[vid]
        acc_name = str(r.get("account_name") or "").strip()
        if acc_name and acc_name not in v["account_names"]:
            v["account_names"].append(acc_name)
        part = str(r.get("particulars") or "").strip()
        if part:
            v["particulars_list"].append(part)
    return [grouped[vid] for vid in ordered_ids]


def _extract_voucher_mode_and_ref(voucher: Dict[str, Any]) -> Tuple[str, str]:
    """
    Extract (raw_mode_token, reference_no) honestly from ledger_entries particulars,
    voucher narration, and ledger account names without inventing unstored modes.
    """
    narration = str(voucher.get("narration") or "")
    particulars_list = voucher.get("particulars_list") or []
    account_names = [str(a).strip().lower() for a in (voucher.get("account_names") or [])]

    extracted_mode: Optional[str] = None
    extracted_ref: str = ""

    for part in particulars_list:
        m = _PAID_VIA_REF_RE.search(part)
        if m:
            extracted_mode = _normalize_mode_token(m.group(1))
            raw_ref = m.group(2).strip()
            if raw_ref.lower() not in ("", "direct", "-", "none", "n/a"):
                extracted_ref = raw_ref
            break

    if not extracted_mode and narration:
        m_mode = _MODE_COLON_RE.search(narration)
        if m_mode:
            extracted_mode = _normalize_mode_token(m_mode.group(1))

    if not extracted_mode:
        combined_text = " ".join([narration] + list(particulars_list))
        m_tok = _NARRATION_MODE_TOKEN_RE.search(combined_text)
        if m_tok:
            extracted_mode = _normalize_mode_token(m_tok.group(1))

    if not extracted_mode:
        if any(a == "cash in hand" for a in account_names):
            extracted_mode = "CASH"
        elif any(a == "bank account" for a in account_names):
            extracted_mode = "BANK_NON_CASH"
        else:
            extracted_mode = "CASH"

    return extracted_mode, extracted_ref


def _resolve_voucher_customer_id(
    voucher: Dict[str, Any], customer_map: Mapping[int, Dict[str, Any]]
) -> Optional[int]:
    """Resolve customer_id cleanly for customer RECEIPT vouchers without colliding with SALE IDs."""
    ref_type = (voucher.get("reference_type") or "").upper()
    if ref_type == "SALE":
        return None
    texts = list(voucher.get("particulars_list") or []) + [str(voucher.get("narration") or "")]
    for txt in texts:
        m = _CUSTOMER_ID_REF_RE.search(txt)
        if m:
            return int(m.group(1))
    ref_id = voucher.get("reference_id")
    if ref_id is not None and ref_type in ("MANUAL", "CUSTOMER", ""):
        cid = int(ref_id)
        if cid in customer_map:
            return cid
    return None


def _resolve_voucher_supplier_id(
    voucher: Dict[str, Any], supplier_map: Mapping[int, Dict[str, Any]]
) -> Optional[int]:
    """Resolve supplier_id cleanly for supplier PAYMENT vouchers (active & legacy signatures)."""
    ref_type = (voucher.get("reference_type") or "").upper()
    if ref_type == "EXPENSE":
        return None
    texts = list(voucher.get("particulars_list") or []) + [str(voucher.get("narration") or "")]
    for txt in texts:
        m = _SUPPLIER_ID_REF_RE.search(txt)
        if m:
            return int(m.group(1))
    ref_id = voucher.get("reference_id")
    if ref_id is not None and ref_type in ("PURCHASE", "MANUAL", "SUPPLIER", ""):
        sid = int(ref_id)
        if sid in supplier_map:
            return sid
    return None


def _derive_outstanding_and_status(
    net_dec: Decimal,
    paid_dec: Decimal,
    raw_due_dec: Decimal,
) -> Tuple[Decimal, str]:
    calc_due = max(Decimal("0.00"), net_dec - paid_dec)
    outstanding = raw_due_dec if raw_due_dec > Decimal("0.005") else calc_due
    if outstanding <= Decimal("0.01"):
        return outstanding, "Paid"
    if paid_dec > Decimal("0.01"):
        return outstanding, "Partial"
    return outstanding, "Credit / Unpaid"


def _build_period_banner_and_filename(
    file_prefix: str,
    norm_from: Optional[str],
    norm_to: Optional[str],
    extra_meta: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str], str]:
    today_compact = datetime.now().strftime("%Y%m%d")
    if norm_from and norm_to:
        return norm_from, norm_to, f"{file_prefix}_{norm_from}_to_{norm_to}.xlsx"
    if norm_from:
        extra_meta["Period"] = f"From {norm_from}"
        return None, None, f"{file_prefix}_from_{norm_from}.xlsx"
    if norm_to:
        extra_meta["Period"] = f"Up to {norm_to}"
        return None, None, f"{file_prefix}_upto_{norm_to}.xlsx"
    extra_meta["Period"] = "All Dates"
    return None, None, f"{file_prefix}_{today_compact}.xlsx"


class ExcelExportService:
    """Service layer generating Excel workbooks from consistent read-only SQLite snapshots."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or get_db_manager()

    def _read_snapshot_bundle(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        category_id: Optional[int] = None,
        product_id: Optional[int] = None,
        customer_id: Optional[int] = None,
        customer_group_id: Optional[int] = None,
        supplier_id: Optional[int] = None,
        secondary_sql: Optional[str] = None,
        secondary_params: Sequence[Any] = (),
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
        """
        Execute all read queries on a single SQLite BEGIN DEFERRED snapshot and close the
        connection completely before returning any data to workbook builders.
        """
        conn = self.db.get_connection()
        cursor = None
        secondary_rows: List[Dict[str, Any]] = []
        try:
            conn.execute("BEGIN DEFERRED;")
            cursor = conn.cursor()

            # Read ONLY safe business branding fields (never credentials/UPI/bank details)
            cursor.execute(
                """
                SELECT
                    company_name, owner_name, address, city, state, pincode,
                    mobile, email, gstin, dl_fertilizer, dl_pesticide, dl_seed
                FROM company_settings
                WHERE setting_id = 1;
                """
            )
            comp_row = cursor.fetchone()
            company_settings: Dict[str, Any] = (
                dict(comp_row)
                if comp_row
                else {"company_name": "Agri-Input Shop", "state": "Maharashtra"}
            )

            filter_meta: Dict[str, Any] = {}
            if category_id is not None:
                cursor.execute(
                    "SELECT category_name FROM categories WHERE category_id = ?;",
                    (category_id,),
                )
                cat_row = cursor.fetchone()
                filter_meta["Category"] = (
                    cat_row["category_name"] if cat_row else f"Category #{category_id}"
                )

            if product_id is not None:
                cursor.execute(
                    "SELECT product_name FROM products WHERE product_id = ?;",
                    (product_id,),
                )
                prod_row = cursor.fetchone()
                filter_meta["Product"] = (
                    prod_row["product_name"] if prod_row else f"Product #{product_id}"
                )

            if customer_id is not None:
                cursor.execute(
                    "SELECT customer_name FROM customers WHERE customer_id = ?;",
                    (customer_id,),
                )
                cust_row = cursor.fetchone()
                filter_meta["Customer"] = (
                    cust_row["customer_name"] if cust_row else f"Customer #{customer_id}"
                )

            if customer_group_id is not None:
                cursor.execute(
                    "SELECT group_name FROM customer_groups WHERE group_id = ?;",
                    (customer_group_id,),
                )
                grp_row = cursor.fetchone()
                filter_meta["Customer Group"] = (
                    grp_row["group_name"] if grp_row else f"Group #{customer_group_id}"
                )

            if supplier_id is not None:
                cursor.execute(
                    "SELECT supplier_name FROM suppliers WHERE supplier_id = ?;",
                    (supplier_id,),
                )
                supp_row = cursor.fetchone()
                filter_meta["Supplier"] = (
                    supp_row["supplier_name"] if supp_row else f"Supplier #{supplier_id}"
                )

            cursor.execute(sql, tuple(params))
            rows = [dict(r) for r in cursor.fetchall()]

            if secondary_sql:
                cursor.execute(secondary_sql, tuple(secondary_params))
                secondary_rows = [dict(r) for r in cursor.fetchall()]

            conn.rollback()
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            conn.close()

        if len(rows) > MAX_EXPORT_ROWS or len(secondary_rows) > MAX_EXPORT_ROWS:
            raise ExcelValidationError(
                f"Export exceeds maximum limit of {MAX_EXPORT_ROWS:,} rows. "
                "Please narrow your party, product, category, or date range filter."
            )

        return company_settings, rows, filter_meta, secondary_rows

    def _read_snapshot(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        category_id: Optional[int] = None,
        product_id: Optional[int] = None,
        customer_id: Optional[int] = None,
        customer_group_id: Optional[int] = None,
        supplier_id: Optional[int] = None,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
        company_settings, rows, filter_meta, _ = self._read_snapshot_bundle(
            sql,
            params,
            category_id=category_id,
            product_id=product_id,
            customer_id=customer_id,
            customer_group_id=customer_group_id,
            supplier_id=supplier_id,
        )
        return company_settings, rows, filter_meta

    def _run_multi_snapshot_queries(
        self,
        queries: Mapping[str, Tuple[str, Sequence[Any]]],
        *,
        customer_id: Optional[int] = None,
        supplier_id: Optional[int] = None,
        expense_category_id: Optional[int] = None,
        enforce_row_limit_keys: Optional[Sequence[str]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]], Dict[str, Any], Dict[str, Any]]:
        """
        Execute multiple read-only SQL queries inside a single SQLite BEGIN DEFERRED
        snapshot and close the connection completely BEFORE workbook construction.
        Never reads Aadhaar, PAN, bank account/IFSC, UPI IDs, passwords, or JWT secrets.
        """
        conn = self.db.get_connection()
        cursor = None
        results: Dict[str, List[Dict[str, Any]]] = {}
        entity_info: Dict[str, Any] = {}
        try:
            conn.execute("BEGIN DEFERRED;")
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    company_name, owner_name, address, city, state, pincode,
                    mobile, email, gstin, dl_fertilizer, dl_pesticide, dl_seed
                FROM company_settings
                WHERE setting_id = 1;
                """
            )
            comp_row = cursor.fetchone()
            company_settings: Dict[str, Any] = (
                dict(comp_row)
                if comp_row
                else {"company_name": "Agri-Input Shop", "state": "Maharashtra"}
            )

            filter_meta: Dict[str, Any] = {}
            if customer_id is not None:
                cursor.execute(
                    """
                    SELECT
                        customer_id, customer_name, mobile, village, taluka, district, state,
                        gstin, COALESCE(opening_balance, 0.0) AS opening_balance,
                        COALESCE(current_balance, 0.0) AS current_balance
                    FROM customers
                    WHERE customer_id = ?;
                    """,
                    (customer_id,),
                )
                cust_row = cursor.fetchone()
                if cust_row:
                    cust_dict = dict(cust_row)
                    entity_info["customer"] = cust_dict
                    filter_meta["Customer"] = cust_dict["customer_name"]
                else:
                    filter_meta["Customer"] = f"Customer #{customer_id}"

            if supplier_id is not None:
                cursor.execute(
                    """
                    SELECT
                        supplier_id, supplier_name, contact_person, mobile, city, state,
                        gstin, dl_number, COALESCE(opening_balance, 0.0) AS opening_balance,
                        COALESCE(current_balance, 0.0) AS current_balance
                    FROM suppliers
                    WHERE supplier_id = ?;
                    """,
                    (supplier_id,),
                )
                supp_row = cursor.fetchone()
                if supp_row:
                    supp_dict = dict(supp_row)
                    entity_info["supplier"] = supp_dict
                    filter_meta["Supplier"] = supp_dict["supplier_name"]
                else:
                    filter_meta["Supplier"] = f"Supplier #{supplier_id}"

            if expense_category_id is not None:
                cursor.execute(
                    "SELECT category_id, category_name FROM expense_categories WHERE category_id = ?;",
                    (expense_category_id,),
                )
                cat_row = cursor.fetchone()
                if cat_row:
                    cat_dict = dict(cat_row)
                    entity_info["expense_category"] = cat_dict
                    filter_meta["Category"] = cat_dict["category_name"]
                else:
                    filter_meta["Category"] = f"Category #{expense_category_id}"

            for key, (sql_stmt, params) in queries.items():
                cursor.execute(sql_stmt, tuple(params))
                results[key] = [dict(r) for r in cursor.fetchall()]

            conn.rollback()
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            conn.close()

        limit_keys = list(enforce_row_limit_keys) if enforce_row_limit_keys is not None else list(queries.keys())
        for key in limit_keys:
            if len(results.get(key, [])) > MAX_EXPORT_ROWS:
                raise ExcelValidationError(
                    f"Export exceeds maximum limit of {MAX_EXPORT_ROWS:,} rows. "
                    "Please narrow your party, category, or date range filter."
                )

        return company_settings, results, filter_meta, entity_info

    # ------------------------------------------------------------------
    # 1. Product Catalog Export
    # ------------------------------------------------------------------
    def export_product_catalog(
        self,
        *,
        query: str = "",
        category_id: Optional[int] = None,
        include_inactive: bool = True,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the Product Catalog workbook (.xlsx) using actual DB product fields
        without exposing default purchase cost rates or sensitive credentials.
        """
        clean_query = _validate_search_query(query)
        valid_cat_id = _validate_positive_id(category_id, "category_id")
        like_pat = _escape_like_pattern(clean_query) if clean_query else ""
        include_flag = 1 if include_inactive else 0

        sql = """
            SELECT
                p.product_id,
                p.product_name,
                COALESCE(c.category_name, '') AS category_name,
                COALESCE(m.manufacturer_name, '') AS manufacturer_name,
                COALESCE(NULLIF(TRIM(u.symbol), ''), u.unit_name, '') AS unit_label,
                COALESCE(p.hsn_code, '') AS hsn_code,
                COALESCE(tg.tax_group_name, '') AS tax_group_name,
                COALESCE(tg.cgst_rate, 0.0) AS cgst_rate,
                COALESCE(tg.sgst_rate, 0.0) AS sgst_rate,
                COALESCE(tg.igst_rate, 0.0) AS igst_rate,
                COALESCE(p.default_sale_rate, 0.0) AS default_sale_rate,
                COALESCE(p.default_mrp, 0.0) AS default_mrp,
                COALESCE(SUM(sb.current_qty), 0.0) AS total_stock,
                COALESCE(p.min_stock_alert, 0.0) AS min_stock_alert,
                COALESCE(cr.crop_name, '') AS crop_name,
                COALESCE(p.is_active, 1) AS is_active
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.category_id
            LEFT JOIN manufacturers m ON p.manufacturer_id = m.manufacturer_id
            LEFT JOIN units u ON p.unit_id = u.unit_id
            LEFT JOIN tax_groups tg ON p.tax_group_id = tg.tax_group_id
            LEFT JOIN crops cr ON p.crop_id = cr.crop_id
            LEFT JOIN stock_batches sb ON p.product_id = sb.product_id
            WHERE (? = 1 OR p.is_active = 1)
              AND (? IS NULL OR p.category_id = ?)
              AND (? = '' OR (
                  p.product_name LIKE ? ESCAPE '\\'
                  OR p.hsn_code LIKE ? ESCAPE '\\'
                  OR m.manufacturer_name LIKE ? ESCAPE '\\'
                  OR c.category_name LIKE ? ESCAPE '\\'
              ))
            GROUP BY
                p.product_id, p.product_name, c.category_name, m.manufacturer_name,
                u.symbol, u.unit_name, p.hsn_code, tg.tax_group_name,
                tg.cgst_rate, tg.sgst_rate, tg.igst_rate,
                p.default_sale_rate, p.default_mrp, p.min_stock_alert,
                cr.crop_name, p.is_active
            ORDER BY p.is_active DESC, p.product_name ASC, p.product_id ASC
            LIMIT ?;
        """
        params = (
            include_flag,
            valid_cat_id,
            valid_cat_id,
            clean_query,
            like_pat,
            like_pat,
            like_pat,
            like_pat,
            MAX_EXPORT_ROWS + 1,
        )
        company_settings, rows, extra_meta = self._read_snapshot(
            sql, params, category_id=valid_cat_id
        )

        active_count = sum(1 for r in rows if int(r.get("is_active") or 0) == 1)
        inactive_count = len(rows) - active_count
        if clean_query:
            extra_meta["Search"] = clean_query
        extra_meta["Products"] = len(rows)
        extra_meta["Active"] = active_count
        if include_inactive:
            extra_meta["Inactive"] = inactive_count

        headers = [
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
        num_cols = len(headers)

        wb = create_workbook("Product Catalog")
        ws = add_sheet(wb, "Product Catalog")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Product Catalog Master",
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1  # Spacer row
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        cur_row = data_start_row
        for item in rows:
            cgst = _to_decimal(item.get("cgst_rate"))
            sgst = _to_decimal(item.get("sgst_rate"))
            igst = _to_decimal(item.get("igst_rate"))
            total_gst = (cgst + sgst) if (cgst + sgst) > Decimal("0") else igst
            status_label = "Active" if int(item.get("is_active") or 0) == 1 else "Inactive"

            _write_int_cell(ws, cur_row, 1, int(item["product_id"]))
            write_text_cell(ws, cur_row, 2, item.get("product_name") or "")
            write_text_cell(ws, cur_row, 3, item.get("category_name") or "")
            write_text_cell(ws, cur_row, 4, item.get("manufacturer_name") or "")
            write_text_cell(ws, cur_row, 5, item.get("unit_label") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 6, item.get("hsn_code") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 7, item.get("tax_group_name") or "")
            write_percentage_cell(ws, cur_row, 8, total_gst)
            write_percentage_cell(ws, cur_row, 9, cgst)
            write_percentage_cell(ws, cur_row, 10, sgst)
            write_percentage_cell(ws, cur_row, 11, igst)
            write_currency_cell(ws, cur_row, 12, _to_decimal(item.get("default_sale_rate")))
            write_currency_cell(ws, cur_row, 13, _to_decimal(item.get("default_mrp")))
            write_quantity_cell(ws, cur_row, 14, _to_decimal(item.get("total_stock")), decimals=3)
            write_quantity_cell(ws, cur_row, 15, _to_decimal(item.get("min_stock_alert")), decimals=3)
            write_text_cell(ws, cur_row, 16, item.get("crop_name") or "")
            write_text_cell(
                ws,
                cur_row,
                17,
                status_label,
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(status_label),
            )
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        today_compact = datetime.now().strftime("%Y%m%d")
        return save_workbook_to_bytes(wb), f"Product_Catalog_{today_compact}.xlsx"

    # ------------------------------------------------------------------
    # 2. Current Stock Summary Export
    # ------------------------------------------------------------------
    def export_current_stock_summary(
        self,
        *,
        category_id: Optional[int] = None,
        query: str = "",
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the product-level Current Stock Summary workbook (.xlsx).
        Reuses existing ERP total stock (SUM(stock_batches.current_qty)) and
        low-stock alert definitions (min_stock_alert > 0 and qty <= min_stock_alert).
        Exposes zero purchase cost prices or profit margins.
        """
        clean_query = _validate_search_query(query)
        valid_cat_id = _validate_positive_id(category_id, "category_id")
        like_pat = _escape_like_pattern(clean_query) if clean_query else ""

        sql = """
            SELECT
                p.product_id,
                p.product_name,
                COALESCE(c.category_name, '') AS category_name,
                COALESCE(m.manufacturer_name, '') AS manufacturer_name,
                COALESCE(p.hsn_code, '') AS hsn_code,
                COALESCE(NULLIF(TRIM(u.symbol), ''), u.unit_name, '') AS unit_label,
                COALESCE(SUM(sb.current_qty), 0.0) AS current_qty,
                COALESCE(p.min_stock_alert, 0.0) AS min_stock_alert,
                COUNT(CASE WHEN sb.current_qty > 0 THEN 1 END) AS active_batch_count,
                MIN(CASE
                    WHEN sb.current_qty > 0 AND sb.exp_date IS NOT NULL AND TRIM(sb.exp_date) != ''
                    THEN TRIM(sb.exp_date)
                END) AS nearest_exp_date,
                COALESCE(p.default_sale_rate, 0.0) AS default_sale_rate,
                COALESCE(p.default_mrp, 0.0) AS default_mrp
            FROM products p
            LEFT JOIN categories c ON p.category_id = c.category_id
            LEFT JOIN manufacturers m ON p.manufacturer_id = m.manufacturer_id
            LEFT JOIN units u ON p.unit_id = u.unit_id
            LEFT JOIN stock_batches sb ON p.product_id = sb.product_id
            WHERE p.is_active = 1
              AND (? IS NULL OR p.category_id = ?)
              AND (? = '' OR (
                  p.product_name LIKE ? ESCAPE '\\'
                  OR p.hsn_code LIKE ? ESCAPE '\\'
                  OR m.manufacturer_name LIKE ? ESCAPE '\\'
                  OR c.category_name LIKE ? ESCAPE '\\'
              ))
            GROUP BY
                p.product_id, p.product_name, c.category_name, m.manufacturer_name,
                p.hsn_code, u.symbol, u.unit_name, p.min_stock_alert,
                p.default_sale_rate, p.default_mrp
            ORDER BY p.product_name ASC, p.product_id ASC
            LIMIT ?;
        """
        params = (
            valid_cat_id,
            valid_cat_id,
            clean_query,
            like_pat,
            like_pat,
            like_pat,
            like_pat,
            MAX_EXPORT_ROWS + 1,
        )
        company_settings, rows, extra_meta = self._read_snapshot(
            sql, params, category_id=valid_cat_id
        )

        out_of_stock_count = 0
        low_stock_count = 0
        in_stock_count = 0
        total_active_batches = 0

        enriched_rows: List[Tuple[Dict[str, Any], Decimal, Decimal, str]] = []
        for item in rows:
            qty_dec = _to_decimal(item.get("current_qty"))
            alert_dec = _to_decimal(item.get("min_stock_alert"))
            total_active_batches += int(item.get("active_batch_count") or 0)
            if qty_dec <= Decimal("0"):
                stock_status = "Out of Stock"
                out_of_stock_count += 1
            elif alert_dec > Decimal("0") and qty_dec <= alert_dec:
                stock_status = "Low Stock"
                low_stock_count += 1
            else:
                stock_status = "In Stock"
                in_stock_count += 1
            enriched_rows.append((item, qty_dec, alert_dec, stock_status))

        if clean_query:
            extra_meta["Search"] = clean_query
        extra_meta["Products"] = len(rows)
        extra_meta["In Stock"] = in_stock_count
        extra_meta["Low Stock"] = low_stock_count
        extra_meta["Out of Stock"] = out_of_stock_count
        extra_meta["Active Batches"] = total_active_batches

        headers = [
            "Product ID",
            "Product Name",
            "Category",
            "Manufacturer / Brand",
            "HSN Code",
            "Unit",
            "Current Available Qty",
            "Min Stock Alert",
            "Stock Status",
            "Active Batches",
            "Nearest Expiry",
            "Default Sale Rate (₹)",
            "Default MRP (₹)",
        ]
        num_cols = len(headers)

        wb = create_workbook("Current Stock Summary")
        ws = add_sheet(wb, "Current Stock Summary")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Current Stock Summary",
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        cur_row = data_start_row
        for item, qty_dec, alert_dec, stock_status in enriched_rows:
            _write_int_cell(ws, cur_row, 1, int(item["product_id"]))
            write_text_cell(ws, cur_row, 2, item.get("product_name") or "")
            write_text_cell(ws, cur_row, 3, item.get("category_name") or "")
            write_text_cell(ws, cur_row, 4, item.get("manufacturer_name") or "")
            write_text_cell(ws, cur_row, 5, item.get("hsn_code") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 6, item.get("unit_label") or "", alignment=_ALIGN_CENTER)
            write_quantity_cell(ws, cur_row, 7, qty_dec, decimals=3)
            write_quantity_cell(ws, cur_row, 8, alert_dec, decimals=3)
            write_text_cell(
                ws,
                cur_row,
                9,
                stock_status,
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(stock_status),
            )
            _write_int_cell(ws, cur_row, 10, int(item.get("active_batch_count") or 0))
            write_date_cell(ws, cur_row, 11, item.get("nearest_exp_date") or "")
            write_currency_cell(ws, cur_row, 12, _to_decimal(item.get("default_sale_rate")))
            write_currency_cell(ws, cur_row, 13, _to_decimal(item.get("default_mrp")))
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        today_compact = datetime.now().strftime("%Y%m%d")
        return save_workbook_to_bytes(wb), f"Current_Stock_Summary_{today_compact}.xlsx"

    # ------------------------------------------------------------------
    # 3. Batch-wise Stock & Expiry Export
    # ------------------------------------------------------------------
    def export_batch_wise_stock_expiry(
        self,
        *,
        category_id: Optional[int] = None,
        query: str = "",
        include_zero_stock: bool = False,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the FEFO-ordered Batch-wise Stock & Expiry workbook (.xlsx).
        Places earliest-expiring / expired batches first and excludes purchase cost rates.
        """
        clean_query = _validate_search_query(query)
        valid_cat_id = _validate_positive_id(category_id, "category_id")
        like_pat = _escape_like_pattern(clean_query) if clean_query else ""
        include_zero_flag = 1 if include_zero_stock else 0

        sql = """
            SELECT
                sb.batch_id,
                sb.product_id,
                p.product_name,
                COALESCE(c.category_name, '') AS category_name,
                COALESCE(m.manufacturer_name, '') AS manufacturer_name,
                sb.batch_no,
                COALESCE(sb.mfg_date, '') AS mfg_date,
                COALESCE(sb.exp_date, '') AS exp_date,
                COALESCE(sb.opening_qty, 0.0) AS opening_qty,
                COALESCE(sb.current_qty, 0.0) AS current_qty,
                COALESCE(NULLIF(TRIM(u.symbol), ''), u.unit_name, '') AS unit_label,
                COALESCE(sb.sale_rate, p.default_sale_rate, 0.0) AS sale_rate,
                COALESCE(sb.mrp, p.default_mrp, 0.0) AS mrp,
                COALESCE(sb.barcode, '') AS barcode,
                COALESCE(sb.created_at, '') AS batch_created_at
            FROM stock_batches sb
            JOIN products p ON sb.product_id = p.product_id
            LEFT JOIN categories c ON p.category_id = c.category_id
            LEFT JOIN manufacturers m ON p.manufacturer_id = m.manufacturer_id
            LEFT JOIN units u ON p.unit_id = u.unit_id
            WHERE p.is_active = 1
              AND (? = 1 OR sb.current_qty > 0)
              AND (? IS NULL OR p.category_id = ?)
              AND (? = '' OR (
                  p.product_name LIKE ? ESCAPE '\\'
                  OR sb.batch_no LIKE ? ESCAPE '\\'
                  OR m.manufacturer_name LIKE ? ESCAPE '\\'
                  OR c.category_name LIKE ? ESCAPE '\\'
              ))
            ORDER BY
                CASE WHEN sb.exp_date IS NULL OR TRIM(sb.exp_date) = '' THEN 1 ELSE 0 END ASC,
                sb.exp_date ASC,
                p.product_name ASC,
                sb.batch_id ASC
            LIMIT ?;
        """
        params = (
            include_zero_flag,
            valid_cat_id,
            valid_cat_id,
            clean_query,
            like_pat,
            like_pat,
            like_pat,
            like_pat,
            MAX_EXPORT_ROWS + 1,
        )
        company_settings, rows, extra_meta = self._read_snapshot(
            sql, params, category_id=valid_cat_id
        )

        today = date.today()
        expired_count = 0
        expiring_soon_count = 0

        enriched_rows: List[Tuple[Dict[str, Any], Optional[int], str]] = []
        for item in rows:
            raw_exp = str(item.get("exp_date") or "").strip()
            days_to_exp: Optional[int] = None
            if raw_exp:
                try:
                    exp_d = date.fromisoformat(raw_exp[:10])
                    days_to_exp = (exp_d - today).days
                except ValueError:
                    days_to_exp = None

            rem_qty = _to_decimal(item.get("current_qty"))
            if rem_qty <= Decimal("0"):
                expiry_status = "Depleted"
            elif days_to_exp is not None:
                if days_to_exp < 0:
                    expiry_status = "Expired"
                    expired_count += 1
                elif days_to_exp <= 90:
                    expiry_status = "Expiring Soon (<=90d)"
                    expiring_soon_count += 1
                else:
                    expiry_status = "Active"
            else:
                expiry_status = "No Expiry Date"

            enriched_rows.append((item, days_to_exp, expiry_status))

        if clean_query:
            extra_meta["Search"] = clean_query
        extra_meta["Batches"] = len(rows)
        extra_meta["Expired"] = expired_count
        extra_meta["Expiring <=90d"] = expiring_soon_count
        extra_meta["Order"] = "FEFO (Earliest Expiry First)"

        headers = [
            "Batch ID",
            "Product ID",
            "Product Name",
            "Category",
            "Manufacturer / Brand",
            "Batch No",
            "Mfg Date",
            "Expiry Date",
            "Days to Expiry",
            "Expiry Status",
            "Opening Qty",
            "Remaining Qty",
            "Unit",
            "Sale Rate (₹)",
            "MRP (₹)",
            "Barcode",
            "Batch Added Date",
        ]
        num_cols = len(headers)

        wb = create_workbook("Batch Stock & Expiry")
        ws = add_sheet(wb, "Batch Stock & Expiry")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Batch-Wise Stock & Expiry Report (FEFO)",
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        cur_row = data_start_row
        for item, days_to_exp, expiry_status in enriched_rows:
            _write_int_cell(ws, cur_row, 1, int(item["batch_id"]))
            _write_int_cell(ws, cur_row, 2, int(item["product_id"]))
            write_text_cell(ws, cur_row, 3, item.get("product_name") or "")
            write_text_cell(ws, cur_row, 4, item.get("category_name") or "")
            write_text_cell(ws, cur_row, 5, item.get("manufacturer_name") or "")
            write_text_cell(ws, cur_row, 6, item.get("batch_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws, cur_row, 7, item.get("mfg_date") or "")
            write_date_cell(ws, cur_row, 8, item.get("exp_date") or "")
            if days_to_exp is not None:
                _write_int_cell(
                    ws,
                    cur_row,
                    9,
                    days_to_exp,
                    font=_resolve_status_font(expiry_status),
                )
            else:
                write_text_cell(ws, cur_row, 9, "", alignment=_ALIGN_CENTER)
            write_text_cell(
                ws,
                cur_row,
                10,
                expiry_status,
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(expiry_status),
            )
            write_quantity_cell(ws, cur_row, 11, _to_decimal(item.get("opening_qty")), decimals=3)
            write_quantity_cell(ws, cur_row, 12, _to_decimal(item.get("current_qty")), decimals=3)
            write_text_cell(ws, cur_row, 13, item.get("unit_label") or "", alignment=_ALIGN_CENTER)
            write_currency_cell(ws, cur_row, 14, _to_decimal(item.get("sale_rate")))
            write_currency_cell(ws, cur_row, 15, _to_decimal(item.get("mrp")))
            write_text_cell(ws, cur_row, 16, item.get("barcode") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws, cur_row, 17, item.get("batch_created_at") or "")
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        today_compact = datetime.now().strftime("%Y%m%d")
        return save_workbook_to_bytes(wb), f"Batch_Wise_Stock_Expiry_{today_compact}.xlsx"

    # ------------------------------------------------------------------
    # 4. Stock Ledger Export
    # ------------------------------------------------------------------
    def export_stock_ledger(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        product_id: Optional[int] = None,
        category_id: Optional[int] = None,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the chronological Stock Ledger movement workbook (.xlsx) from `stock_ledger`.
        Supports optional validated YYYY-MM-DD date filters (`from_date`, `to_date`),
        resolves reliable invoice references cleanly, excludes `rate` (cost price on inward rows),
        uses recorded `balance_qty` verbatim, and computes movement sums with Decimal.
        """
        norm_from, norm_to = normalize_optional_date_filters(from_date, to_date)
        valid_prod_id = _validate_positive_id(product_id, "product_id")
        valid_cat_id = _validate_positive_id(category_id, "category_id")

        sql = """
            SELECT
                sl.ledger_id,
                COALESCE(NULLIF(TRIM(sl.movement_date), ''), sl.created_at, '') AS movement_ts,
                sl.product_id,
                COALESCE(p.product_name, 'Product #' || sl.product_id) AS product_name,
                COALESCE(c.category_name, '') AS category_name,
                COALESCE(sb.batch_no, '') AS batch_no,
                sl.transaction_type,
                sl.reference_type,
                sl.reference_id,
                CASE
                    WHEN sl.reference_type = 'INVOICE' AND sl.transaction_type = 'SALE'
                        THEN COALESCE(s.invoice_no, '')
                    WHEN sl.reference_type = 'INVOICE' AND sl.transaction_type = 'PURCHASE'
                        THEN COALESCE(pur.invoice_no, '')
                    WHEN sl.reference_type = 'RETURN' AND sl.transaction_type = 'SALE_RETURN'
                        THEN COALESCE(s_ret.invoice_no, '')
                    ELSE ''
                END AS reference_no,
                COALESCE(sl.qty_in, 0.0) AS qty_in,
                COALESCE(sl.qty_out, 0.0) AS qty_out,
                sl.balance_qty,
                COALESCE(NULLIF(TRIM(u.symbol), ''), u.unit_name, '') AS unit_label,
                COALESCE(sl.remarks, '') AS remarks
            FROM stock_ledger sl
            LEFT JOIN products p ON sl.product_id = p.product_id
            LEFT JOIN categories c ON p.category_id = c.category_id
            LEFT JOIN units u ON p.unit_id = u.unit_id
            LEFT JOIN stock_batches sb ON sl.batch_id = sb.batch_id
            LEFT JOIN sales s
                ON sl.reference_type = 'INVOICE'
               AND sl.transaction_type = 'SALE'
               AND sl.reference_id = s.sale_id
            LEFT JOIN purchases pur
                ON sl.reference_type = 'INVOICE'
               AND sl.transaction_type = 'PURCHASE'
               AND sl.reference_id = pur.purchase_id
            LEFT JOIN sales s_ret
                ON sl.reference_type = 'RETURN'
               AND sl.transaction_type = 'SALE_RETURN'
               AND sl.reference_id = s_ret.sale_id
            WHERE (? IS NULL OR substr(COALESCE(NULLIF(TRIM(sl.movement_date), ''), sl.created_at), 1, 10) >= ?)
              AND (? IS NULL OR substr(COALESCE(NULLIF(TRIM(sl.movement_date), ''), sl.created_at), 1, 10) <= ?)
              AND (? IS NULL OR sl.product_id = ?)
              AND (? IS NULL OR p.category_id = ?)
            ORDER BY
                substr(COALESCE(NULLIF(TRIM(sl.movement_date), ''), sl.created_at), 1, 10) ASC,
                COALESCE(NULLIF(TRIM(sl.movement_date), ''), sl.created_at) ASC,
                sl.ledger_id ASC
            LIMIT ?;
        """
        params = (
            norm_from,
            norm_from,
            norm_to,
            norm_to,
            valid_prod_id,
            valid_prod_id,
            valid_cat_id,
            valid_cat_id,
            MAX_EXPORT_ROWS + 1,
        )
        company_settings, rows, extra_meta = self._read_snapshot(
            sql, params, category_id=valid_cat_id, product_id=valid_prod_id
        )

        if norm_from and norm_to:
            banner_from, banner_to = norm_from, norm_to
        elif norm_from:
            banner_from, banner_to = None, None
            extra_meta["Period"] = f"From {norm_from}"
        elif norm_to:
            banner_from, banner_to = None, None
            extra_meta["Period"] = f"Up to {norm_to}"
        else:
            banner_from, banner_to = None, None
            extra_meta["Period"] = "All Dates"
        extra_meta["Movements"] = len(rows)

        headers = [
            "Ledger ID",
            "Movement Date",
            "Product ID",
            "Product Name",
            "Category",
            "Batch No",
            "Transaction Type",
            "Reference Type",
            "Reference No",
            "Reference ID",
            "Inward Qty",
            "Outward Qty",
            "Balance Qty",
            "Unit",
            "Remarks",
        ]
        num_cols = len(headers)

        wb = create_workbook("Stock Ledger")
        ws = add_sheet(wb, "Stock Ledger")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Stock Movement Ledger",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        total_in = Decimal("0")
        total_out = Decimal("0")
        cur_row = data_start_row

        for entry in rows:
            qty_in_dec = _to_decimal(entry.get("qty_in"))
            qty_out_dec = _to_decimal(entry.get("qty_out"))
            total_in += qty_in_dec
            total_out += qty_out_dec

            _write_int_cell(ws, cur_row, 1, int(entry["ledger_id"]))
            write_date_cell(ws, cur_row, 2, entry.get("movement_ts") or "")
            _write_int_cell(ws, cur_row, 3, int(entry["product_id"]))
            write_text_cell(ws, cur_row, 4, entry.get("product_name") or "")
            write_text_cell(ws, cur_row, 5, entry.get("category_name") or "")
            write_text_cell(ws, cur_row, 6, entry.get("batch_no") or "", alignment=_ALIGN_CENTER)
            write_text_cell(
                ws, cur_row, 7, entry.get("transaction_type") or "", alignment=_ALIGN_CENTER
            )
            write_text_cell(
                ws, cur_row, 8, entry.get("reference_type") or "", alignment=_ALIGN_CENTER
            )
            write_text_cell(ws, cur_row, 9, entry.get("reference_no") or "", alignment=_ALIGN_CENTER)
            ref_id = entry.get("reference_id")
            if ref_id is not None:
                _write_int_cell(ws, cur_row, 10, int(ref_id))
            else:
                write_text_cell(ws, cur_row, 10, "", alignment=_ALIGN_CENTER)
            write_quantity_cell(ws, cur_row, 11, qty_in_dec, decimals=3)
            write_quantity_cell(ws, cur_row, 12, qty_out_dec, decimals=3)
            bal_val = entry.get("balance_qty")
            if bal_val is not None:
                write_quantity_cell(ws, cur_row, 13, _to_decimal(bal_val), decimals=3)
            else:
                write_text_cell(ws, cur_row, 13, "")
            write_text_cell(ws, cur_row, 14, entry.get("unit_label") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 15, entry.get("remarks") or "", alignment=_ALIGN_LEFT)
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )
            write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals={11: total_in, 12: total_out},
                num_cols=num_cols,
                label="TOTAL MOVEMENTS",
                label_col=1,
                quantity_cols=[11, 12],
            )

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        today_compact = datetime.now().strftime("%Y%m%d")
        if norm_from and norm_to:
            filename = f"Stock_Ledger_{norm_from}_to_{norm_to}.xlsx"
        elif norm_from:
            filename = f"Stock_Ledger_from_{norm_from}.xlsx"
        elif norm_to:
            filename = f"Stock_Ledger_upto_{norm_to}.xlsx"
        else:
            filename = f"Stock_Ledger_{today_compact}.xlsx"

        return save_workbook_to_bytes(wb), filename

    # ------------------------------------------------------------------
    # 5. Sales Register Export (Stage C3-B2B)
    # ------------------------------------------------------------------
    def export_sales_register(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        customer_id: Optional[int] = None,
        payment_mode: Optional[str] = None,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the invoice-level Sales Register workbook (.xlsx) from `sales` and `customers`.
        - Exposes zero purchase cost, margin, profit, Aadhaar, bank, UPI, or credential fields.
        - Does NOT silently net `sales_returns` into Sales Register totals; if recorded sales
          returns exist in the filter scope, appends them on a clearly labelled `Sales Returns` sheet.
        """
        norm_from, norm_to = normalize_optional_date_filters(from_date, to_date)
        valid_cust_id = _validate_positive_id(customer_id, "customer_id")
        valid_pay_mode = _validate_payment_mode_filter(payment_mode, field_name="payment_mode")

        sql = """
            SELECT
                s.sale_id,
                s.invoice_no,
                s.sale_date,
                s.customer_id,
                COALESCE(c.customer_name, 'Customer #' || s.customer_id) AS customer_name,
                COALESCE(c.gstin, '') AS customer_gstin,
                COALESCE(c.state, '') AS customer_state,
                COALESCE(s.payment_mode, 'CASH') AS payment_mode,
                COALESCE(s.total_taxable, 0.0) AS total_taxable,
                COALESCE(s.total_cgst, 0.0) AS total_cgst,
                COALESCE(s.total_sgst, 0.0) AS total_sgst,
                COALESCE(s.total_igst, 0.0) AS total_igst,
                COALESCE(s.total_discount, 0.0) AS total_discount,
                COALESCE(s.extra_charges, 0.0) AS extra_charges,
                COALESCE(s.round_off, 0.0) AS round_off,
                COALESCE(s.net_amount, 0.0) AS net_amount,
                COALESCE(s.paid_amount, 0.0) AS paid_amount,
                COALESCE(s.due_amount, 0.0) AS due_amount,
                COALESCE(s.remarks, '') AS remarks
            FROM sales s
            LEFT JOIN customers c ON s.customer_id = c.customer_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
              AND (? IS NULL OR s.customer_id = ?)
              AND (? IS NULL OR UPPER(REPLACE(TRIM(COALESCE(s.payment_mode, '')), ' ', '_')) = ?)
            ORDER BY
                substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) ASC,
                s.sale_id ASC
            LIMIT ?;
        """
        params = (
            norm_from,
            norm_from,
            norm_to,
            norm_to,
            valid_cust_id,
            valid_cust_id,
            valid_pay_mode,
            valid_pay_mode,
            MAX_EXPORT_ROWS + 1,
        )

        returns_sql = """
            SELECT
                sr.return_id,
                sr.return_no,
                sr.return_date,
                COALESCE(s.invoice_no, '') AS original_invoice_no,
                sr.customer_id,
                COALESCE(c.customer_name, 'Customer #' || sr.customer_id) AS customer_name,
                COALESCE(sr.total_taxable, 0.0) AS total_taxable,
                COALESCE(sr.total_tax, 0.0) AS total_tax,
                COALESCE(sr.round_off, 0.0) AS round_off,
                COALESCE(sr.net_amount, 0.0) AS net_amount,
                COALESCE(sr.remarks, '') AS remarks
            FROM sales_returns sr
            LEFT JOIN customers c ON sr.customer_id = c.customer_id
            LEFT JOIN sales s ON sr.sale_id = s.sale_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) <= ?)
              AND (? IS NULL OR sr.customer_id = ?)
              AND (? IS NULL OR UPPER(REPLACE(TRIM(COALESCE(s.payment_mode, '')), ' ', '_')) = ?)
            ORDER BY
                substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) ASC,
                sr.return_id ASC
            LIMIT ?;
        """

        company_settings, rows, extra_meta, return_rows = self._read_snapshot_bundle(
            sql,
            params,
            customer_id=valid_cust_id,
            secondary_sql=returns_sql,
            secondary_params=params,
        )

        banner_from, banner_to, filename = _build_period_banner_and_filename(
            "Sales_Register", norm_from, norm_to, extra_meta
        )
        if valid_pay_mode:
            extra_meta["Payment Mode"] = _format_payment_mode_label(valid_pay_mode)
        extra_meta["Invoices"] = len(rows)
        if return_rows:
            ret_sum = sum(_to_decimal(r.get("net_amount")) for r in return_rows)
            extra_meta["Sales Returns Sheet"] = (
                f"{len(return_rows)} (₹{ret_sum:,.2f}; not deducted from Sales Register)"
            )

        headers = [
            "Invoice No",
            "Invoice Date",
            "Customer ID",
            "Customer Name",
            "Customer GSTIN",
            "Place of Supply",
            "Taxable Value (₹)",
            "CGST (₹)",
            "SGST (₹)",
            "IGST (₹)",
            "Discount (₹)",
            "Extra Charges (₹)",
            "Round Off (₹)",
            "Invoice Total (₹)",
            "Amount Paid (₹)",
            "Recorded Invoice Due (₹)",
            "Payment Mode",
            "Invoice Status",
            "Remarks",
        ]
        num_cols = len(headers)
        shop_state = str(company_settings.get("state") or "").strip()

        wb = create_workbook("Sales Register")
        ws = add_sheet(wb, "Sales Register")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Sales Register (Invoice Summary)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws,
            SALES_RECORDED_DUE_NOTE,
            row_idx=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        totals = {idx: Decimal("0") for idx in range(7, 17)}
        cur_row = data_start_row

        for inv in rows:
            taxable = _to_decimal(inv.get("total_taxable"))
            cgst = _to_decimal(inv.get("total_cgst"))
            sgst = _to_decimal(inv.get("total_sgst"))
            igst = _to_decimal(inv.get("total_igst"))
            discount = _to_decimal(inv.get("total_discount"))
            extra_chg = _to_decimal(inv.get("extra_charges"))
            round_off = _to_decimal(inv.get("round_off"))
            net_amt = _to_decimal(inv.get("net_amount"))
            paid_amt = _to_decimal(inv.get("paid_amount"))
            raw_due = _to_decimal(inv.get("due_amount"))
            outstanding_amt, inv_status = _derive_outstanding_and_status(net_amt, paid_amt, raw_due)

            totals[7] += taxable
            totals[8] += cgst
            totals[9] += sgst
            totals[10] += igst
            totals[11] += discount
            totals[12] += extra_chg
            totals[13] += round_off
            totals[14] += net_amt
            totals[15] += paid_amt
            totals[16] += outstanding_amt

            pos_state = str(inv.get("customer_state") or "").strip() or shop_state

            write_text_cell(ws, cur_row, 1, inv.get("invoice_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws, cur_row, 2, inv.get("sale_date") or "")
            _write_int_cell(ws, cur_row, 3, int(inv["customer_id"]))
            write_text_cell(ws, cur_row, 4, inv.get("customer_name") or "")
            write_text_cell(
                ws, cur_row, 5, str(inv.get("customer_gstin") or "").upper(), alignment=_ALIGN_CENTER
            )
            write_text_cell(ws, cur_row, 6, pos_state, alignment=_ALIGN_CENTER)
            write_currency_cell(ws, cur_row, 7, taxable)
            write_currency_cell(ws, cur_row, 8, cgst)
            write_currency_cell(ws, cur_row, 9, sgst)
            write_currency_cell(ws, cur_row, 10, igst)
            write_currency_cell(ws, cur_row, 11, discount)
            write_currency_cell(ws, cur_row, 12, extra_chg)
            write_currency_cell(ws, cur_row, 13, round_off)
            write_currency_cell(ws, cur_row, 14, net_amt)
            write_currency_cell(ws, cur_row, 15, paid_amt)
            write_currency_cell(
                ws,
                cur_row,
                16,
                outstanding_amt,
                font=_FONT_STATUS_DANGER if outstanding_amt > Decimal("0.01") else None,
            )
            write_text_cell(
                ws,
                cur_row,
                17,
                _format_payment_mode_label(inv.get("payment_mode")),
                alignment=_ALIGN_CENTER,
            )
            write_text_cell(
                ws,
                cur_row,
                18,
                inv_status,
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(inv_status),
            )
            write_text_cell(ws, cur_row, 19, inv.get("remarks") or "")
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )
            write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals=totals,
                num_cols=num_cols,
                label="TOTAL SALES & RECORDED INVOICE DUE",
                label_col=1,
                currency_cols=list(range(7, 17)),
            )
            ws.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=6)

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        # Optional clearly labelled Sales Returns sheet ONLY when recorded returns exist
        if return_rows:
            ws_ret = add_sheet(wb, "Sales Returns", replace_empty_default=False)
            ret_headers = [
                "Return No",
                "Return Date",
                "Original Invoice No",
                "Customer ID",
                "Customer Name",
                "Taxable Value (₹)",
                "Total Tax (₹)",
                "Round Off (₹)",
                "Return Net Total (₹)",
                "Remarks",
            ]
            ret_num_cols = len(ret_headers)
            ret_meta: Dict[str, Any] = {
                "Returns Count": len(return_rows),
                "Scope Note": "Separate credit-note returns (NOT deducted from Sales Register totals)",
            }
            if extra_meta.get("Period"):
                ret_meta["Period"] = extra_meta["Period"]

            ret_ptr = add_company_header(
                ws_ret, company_settings, start_row=1, col_span=ret_num_cols, include_licences=True
            )
            ret_ptr = add_report_banner(
                ws_ret,
                "Sales Returns Register (Credit Notes)",
                from_date=banner_from,
                to_date=banner_to,
                generated_by=generated_by,
                extra_meta=ret_meta,
                start_row=ret_ptr,
                col_span=ret_num_cols,
            )
            ret_ptr += 1
            ret_hdr_row = ret_ptr
            ret_data_start = write_table_header(ws_ret, ret_headers, row_idx=ret_hdr_row)

            ret_totals = {6: Decimal("0"), 7: Decimal("0"), 8: Decimal("0"), 9: Decimal("0")}
            r_cur = ret_data_start
            for ret in return_rows:
                r_taxable = _to_decimal(ret.get("total_taxable"))
                r_tax = _to_decimal(ret.get("total_tax"))
                r_round = _to_decimal(ret.get("round_off"))
                r_net = _to_decimal(ret.get("net_amount"))
                ret_totals[6] += r_taxable
                ret_totals[7] += r_tax
                ret_totals[8] += r_round
                ret_totals[9] += r_net

                write_text_cell(ws_ret, r_cur, 1, ret.get("return_no") or "", alignment=_ALIGN_CENTER)
                write_date_cell(ws_ret, r_cur, 2, ret.get("return_date") or "")
                write_text_cell(
                    ws_ret, r_cur, 3, ret.get("original_invoice_no") or "", alignment=_ALIGN_CENTER
                )
                _write_int_cell(ws_ret, r_cur, 4, int(ret["customer_id"]))
                write_text_cell(ws_ret, r_cur, 5, ret.get("customer_name") or "")
                write_currency_cell(ws_ret, r_cur, 6, r_taxable)
                write_currency_cell(ws_ret, r_cur, 7, r_tax)
                write_currency_cell(ws_ret, r_cur, 8, r_round)
                write_currency_cell(ws_ret, r_cur, 9, r_net)
                write_text_cell(ws_ret, r_cur, 10, ret.get("remarks") or "")
                r_cur += 1

            ret_last_data = r_cur - 1
            if ret_last_data >= ret_data_start:
                apply_alternating_row_styles(
                    ws_ret,
                    start_row=ret_data_start,
                    end_row=ret_last_data,
                    start_col=1,
                    end_col=ret_num_cols,
                )
                write_totals_row(
                    ws_ret,
                    row_idx=r_cur,
                    col_totals=ret_totals,
                    num_cols=ret_num_cols,
                    label="TOTAL RETURNS",
                    label_col=1,
                    currency_cols=[6, 7, 8, 9],
                )

            apply_auto_filter(
                ws_ret,
                header_row=ret_hdr_row,
                last_row=max(ret_hdr_row, ret_last_data),
                start_col=1,
                end_col=ret_num_cols,
            )
            apply_freeze_panes(ws_ret, header_row=ret_hdr_row, freeze_col=1)
            auto_fit_columns(ws_ret, min_width=10, max_width=45, padding=3, exclude_merged=True)
            _configure_landscape_print(ws_ret)

        return save_workbook_to_bytes(wb), filename

    # ------------------------------------------------------------------
    # 6. Sales Item Details Export (Stage C3-B2B)
    # ------------------------------------------------------------------
    def export_sales_item_details(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        customer_id: Optional[int] = None,
        product_id: Optional[int] = None,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the line-item Sales Item Details workbook (.xlsx) from `sale_items` & `sales`.
        - Uses historical sale line-item rates/taxes saved on `sale_items` (never today's master rates).
        - Exposes zero purchase cost prices, margins, profits, Aadhaar, bank, or UPI fields.
        - Does NOT silently net returned items into sales totals; appends `Sales Return Items` only
          when recorded return items exist in the matching filter scope.
        """
        norm_from, norm_to = normalize_optional_date_filters(from_date, to_date)
        valid_cust_id = _validate_positive_id(customer_id, "customer_id")
        valid_prod_id = _validate_positive_id(product_id, "product_id")

        sql = """
            SELECT
                si.sale_item_id,
                s.sale_id,
                s.invoice_no,
                s.sale_date,
                s.customer_id,
                COALESCE(c.customer_name, 'Customer #' || s.customer_id) AS customer_name,
                si.product_id,
                COALESCE(p.product_name, 'Product #' || si.product_id) AS product_name,
                COALESCE(p.hsn_code, '') AS hsn_code,
                COALESCE(sb.batch_no, '') AS batch_no,
                COALESCE(sb.exp_date, '') AS exp_date,
                COALESCE(si.qty, 0.0) AS qty,
                COALESCE(
                    NULLIF(TRIM(u_si.symbol), ''),
                    u_si.unit_name,
                    NULLIF(TRIM(u_p.symbol), ''),
                    u_p.unit_name,
                    ''
                ) AS unit_label,
                COALESCE(si.sale_rate, 0.0) AS sale_rate,
                COALESCE(si.mrp, 0.0) AS mrp,
                COALESCE(si.discount_percent, 0.0) AS discount_percent,
                COALESCE(si.discount_amount, 0.0) AS discount_amount,
                COALESCE(si.taxable_amount, 0.0) AS taxable_amount,
                COALESCE(si.cgst_rate, 0.0) AS cgst_rate,
                COALESCE(si.cgst_amount, 0.0) AS cgst_amount,
                COALESCE(si.sgst_rate, 0.0) AS sgst_rate,
                COALESCE(si.sgst_amount, 0.0) AS sgst_amount,
                COALESCE(si.igst_rate, 0.0) AS igst_rate,
                COALESCE(si.igst_amount, 0.0) AS igst_amount,
                COALESCE(si.total_amount, 0.0) AS total_amount
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            LEFT JOIN customers c ON s.customer_id = c.customer_id
            LEFT JOIN products p ON si.product_id = p.product_id
            LEFT JOIN stock_batches sb ON si.batch_id = sb.batch_id
            LEFT JOIN units u_si ON si.unit_id = u_si.unit_id
            LEFT JOIN units u_p ON p.unit_id = u_p.unit_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
              AND (? IS NULL OR s.customer_id = ?)
              AND (? IS NULL OR si.product_id = ?)
            ORDER BY
                substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) ASC,
                s.sale_id ASC,
                si.sale_item_id ASC
            LIMIT ?;
        """
        params = (
            norm_from,
            norm_from,
            norm_to,
            norm_to,
            valid_cust_id,
            valid_cust_id,
            valid_prod_id,
            valid_prod_id,
            MAX_EXPORT_ROWS + 1,
        )

        return_items_sql = """
            SELECT
                sri.return_item_id,
                sr.return_no,
                sr.return_date,
                COALESCE(s.invoice_no, '') AS original_invoice_no,
                sr.customer_id,
                COALESCE(c.customer_name, 'Customer #' || sr.customer_id) AS customer_name,
                sri.product_id,
                COALESCE(p.product_name, 'Product #' || sri.product_id) AS product_name,
                COALESCE(sb.batch_no, '') AS batch_no,
                COALESCE(sri.qty, 0.0) AS qty,
                COALESCE(NULLIF(TRIM(u_p.symbol), ''), u_p.unit_name, '') AS unit_label,
                COALESCE(sri.sale_rate, 0.0) AS sale_rate,
                COALESCE(sri.taxable_amount, 0.0) AS taxable_amount,
                COALESCE(sri.tax_amount, 0.0) AS tax_amount,
                COALESCE(sri.total_amount, 0.0) AS total_amount,
                COALESCE(sri.reason, '') AS reason
            FROM sales_return_items sri
            JOIN sales_returns sr ON sri.return_id = sr.return_id
            LEFT JOIN customers c ON sr.customer_id = c.customer_id
            LEFT JOIN sales s ON sr.sale_id = s.sale_id
            LEFT JOIN products p ON sri.product_id = p.product_id
            LEFT JOIN units u_p ON p.unit_id = u_p.unit_id
            LEFT JOIN stock_batches sb ON sri.batch_id = sb.batch_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) <= ?)
              AND (? IS NULL OR sr.customer_id = ?)
              AND (? IS NULL OR sri.product_id = ?)
            ORDER BY
                substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) ASC,
                sr.return_id ASC,
                sri.return_item_id ASC
            LIMIT ?;
        """

        company_settings, rows, extra_meta, return_item_rows = self._read_snapshot_bundle(
            sql,
            params,
            product_id=valid_prod_id,
            customer_id=valid_cust_id,
            secondary_sql=return_items_sql,
            secondary_params=params,
        )

        banner_from, banner_to, filename = _build_period_banner_and_filename(
            "Sales_Item_Details", norm_from, norm_to, extra_meta
        )
        extra_meta["Line Items"] = len(rows)
        if return_item_rows:
            extra_meta["Return Items Sheet"] = (
                f"{len(return_item_rows)} (Not deducted from Sales Item totals)"
            )

        headers = [
            "Invoice No",
            "Invoice Date",
            "Customer ID",
            "Customer Name",
            "Product ID",
            "Product Name",
            "HSN Code",
            "Batch No",
            "Expiry Date",
            "Qty",
            "Unit",
            "Sale Rate (₹)",
            "MRP (₹)",
            "Discount (%)",
            "Discount (₹)",
            "Taxable Amount (₹)",
            "GST Rate (%)",
            "CGST (₹)",
            "SGST (₹)",
            "IGST (₹)",
            "Line Total (₹)",
        ]
        num_cols = len(headers)

        wb = create_workbook("Sales Item Details")
        ws = add_sheet(wb, "Sales Item Details")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Sales Line-Item Details Report",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        totals = {idx: Decimal("0") for idx in (15, 16, 18, 19, 20, 21)}
        cur_row = data_start_row

        for item in rows:
            disc_amt = _to_decimal(item.get("discount_amount"))
            taxable_amt = _to_decimal(item.get("taxable_amount"))
            cgst_rate = _to_decimal(item.get("cgst_rate"))
            sgst_rate = _to_decimal(item.get("sgst_rate"))
            igst_rate = _to_decimal(item.get("igst_rate"))
            gst_rate = cgst_rate + sgst_rate + igst_rate
            cgst_amt = _to_decimal(item.get("cgst_amount"))
            sgst_amt = _to_decimal(item.get("sgst_amount"))
            igst_amt = _to_decimal(item.get("igst_amount"))
            line_total = _to_decimal(item.get("total_amount"))

            totals[15] += disc_amt
            totals[16] += taxable_amt
            totals[18] += cgst_amt
            totals[19] += sgst_amt
            totals[20] += igst_amt
            totals[21] += line_total

            write_text_cell(ws, cur_row, 1, item.get("invoice_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws, cur_row, 2, item.get("sale_date") or "")
            _write_int_cell(ws, cur_row, 3, int(item["customer_id"]))
            write_text_cell(ws, cur_row, 4, item.get("customer_name") or "")
            _write_int_cell(ws, cur_row, 5, int(item["product_id"]))
            write_text_cell(ws, cur_row, 6, item.get("product_name") or "")
            write_text_cell(ws, cur_row, 7, item.get("hsn_code") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 8, item.get("batch_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws, cur_row, 9, item.get("exp_date") or "")
            write_quantity_cell(ws, cur_row, 10, _to_decimal(item.get("qty")), decimals=3)
            write_text_cell(ws, cur_row, 11, item.get("unit_label") or "", alignment=_ALIGN_CENTER)
            write_currency_cell(ws, cur_row, 12, _to_decimal(item.get("sale_rate")))
            write_currency_cell(ws, cur_row, 13, _to_decimal(item.get("mrp")))
            write_percentage_cell(ws, cur_row, 14, _to_decimal(item.get("discount_percent")))
            write_currency_cell(ws, cur_row, 15, disc_amt)
            write_currency_cell(ws, cur_row, 16, taxable_amt)
            write_percentage_cell(ws, cur_row, 17, gst_rate)
            write_currency_cell(ws, cur_row, 18, cgst_amt)
            write_currency_cell(ws, cur_row, 19, sgst_amt)
            write_currency_cell(ws, cur_row, 20, igst_amt)
            write_currency_cell(ws, cur_row, 21, line_total)
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )
            write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals=totals,
                num_cols=num_cols,
                label="TOTAL SALES ITEMS",
                label_col=1,
                currency_cols=[15, 16, 18, 19, 20, 21],
            )

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        if return_item_rows:
            ws_ri = add_sheet(wb, "Sales Return Items", replace_empty_default=False)
            ri_headers = [
                "Return No",
                "Return Date",
                "Original Invoice No",
                "Customer ID",
                "Customer Name",
                "Product ID",
                "Product Name",
                "Batch No",
                "Returned Qty",
                "Unit",
                "Sale Rate (₹)",
                "Taxable Amount (₹)",
                "Tax Amount (₹)",
                "Line Total (₹)",
                "Reason",
            ]
            ri_num_cols = len(ri_headers)
            ri_meta: Dict[str, Any] = {
                "Return Items": len(return_item_rows),
                "Scope Note": "Separate return line items (NOT deducted from Sales Item Details totals)",
            }
            if extra_meta.get("Period"):
                ri_meta["Period"] = extra_meta["Period"]

            ri_ptr = add_company_header(
                ws_ri, company_settings, start_row=1, col_span=ri_num_cols, include_licences=True
            )
            ri_ptr = add_report_banner(
                ws_ri,
                "Sales Return Line Items (Credit Notes)",
                from_date=banner_from,
                to_date=banner_to,
                generated_by=generated_by,
                extra_meta=ri_meta,
                start_row=ri_ptr,
                col_span=ri_num_cols,
            )
            ri_ptr += 1
            ri_hdr_row = ri_ptr
            ri_data_start = write_table_header(ws_ri, ri_headers, row_idx=ri_hdr_row)

            ri_totals = {12: Decimal("0"), 13: Decimal("0"), 14: Decimal("0")}
            rc = ri_data_start
            for ri in return_item_rows:
                ri_taxable = _to_decimal(ri.get("taxable_amount"))
                ri_tax = _to_decimal(ri.get("tax_amount"))
                ri_total = _to_decimal(ri.get("total_amount"))
                ri_totals[12] += ri_taxable
                ri_totals[13] += ri_tax
                ri_totals[14] += ri_total

                write_text_cell(ws_ri, rc, 1, ri.get("return_no") or "", alignment=_ALIGN_CENTER)
                write_date_cell(ws_ri, rc, 2, ri.get("return_date") or "")
                write_text_cell(
                    ws_ri, rc, 3, ri.get("original_invoice_no") or "", alignment=_ALIGN_CENTER
                )
                _write_int_cell(ws_ri, rc, 4, int(ri["customer_id"]))
                write_text_cell(ws_ri, rc, 5, ri.get("customer_name") or "")
                _write_int_cell(ws_ri, rc, 6, int(ri["product_id"]))
                write_text_cell(ws_ri, rc, 7, ri.get("product_name") or "")
                write_text_cell(ws_ri, rc, 8, ri.get("batch_no") or "", alignment=_ALIGN_CENTER)
                write_quantity_cell(ws_ri, rc, 9, _to_decimal(ri.get("qty")), decimals=3)
                write_text_cell(ws_ri, rc, 10, ri.get("unit_label") or "", alignment=_ALIGN_CENTER)
                write_currency_cell(ws_ri, rc, 11, _to_decimal(ri.get("sale_rate")))
                write_currency_cell(ws_ri, rc, 12, ri_taxable)
                write_currency_cell(ws_ri, rc, 13, ri_tax)
                write_currency_cell(ws_ri, rc, 14, ri_total)
                write_text_cell(ws_ri, rc, 15, ri.get("reason") or "")
                rc += 1

            ri_last_data = rc - 1
            if ri_last_data >= ri_data_start:
                apply_alternating_row_styles(
                    ws_ri,
                    start_row=ri_data_start,
                    end_row=ri_last_data,
                    start_col=1,
                    end_col=ri_num_cols,
                )
                write_totals_row(
                    ws_ri,
                    row_idx=rc,
                    col_totals=ri_totals,
                    num_cols=ri_num_cols,
                    label="TOTAL RETURN ITEMS",
                    label_col=1,
                    currency_cols=[12, 13, 14],
                )

            apply_auto_filter(
                ws_ri,
                header_row=ri_hdr_row,
                last_row=max(ri_hdr_row, ri_last_data),
                start_col=1,
                end_col=ri_num_cols,
            )
            apply_freeze_panes(ws_ri, header_row=ri_hdr_row, freeze_col=1)
            auto_fit_columns(ws_ri, min_width=10, max_width=45, padding=3, exclude_merged=True)
            _configure_landscape_print(ws_ri)

        return save_workbook_to_bytes(wb), filename

    # ------------------------------------------------------------------
    # 7. Purchase Register Export (Stage C3-B2B — ADMIN ONLY)
    # ------------------------------------------------------------------
    def export_purchase_register(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        supplier_id: Optional[int] = None,
        payment_type: Optional[str] = None,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the ADMIN-only Purchase Register workbook (.xlsx) from `purchases` & `suppliers`.
        - Exposes zero supplier PAN, bank account, IFSC, UPI ID, or auth credential fields.
        - Reports gross inward purchases clearly (purchase returns workflow is not implemented).
        """
        norm_from, norm_to = normalize_optional_date_filters(from_date, to_date)
        valid_supp_id = _validate_positive_id(supplier_id, "supplier_id")
        valid_pay_type = _validate_payment_mode_filter(payment_type, field_name="payment_type")

        sql = """
            SELECT
                p.purchase_id,
                COALESCE(p.invoice_no, '') AS supplier_invoice_no,
                p.purchase_date,
                COALESCE(p.challan_no, '') AS challan_no,
                p.supplier_id,
                COALESCE(s.supplier_name, 'Supplier #' || p.supplier_id) AS supplier_name,
                COALESCE(s.gstin, '') AS supplier_gstin,
                COALESCE(p.payment_type, 'CREDIT') AS payment_type,
                COALESCE(p.total_taxable, 0.0) AS total_taxable,
                COALESCE(p.total_cgst, 0.0) AS total_cgst,
                COALESCE(p.total_sgst, 0.0) AS total_sgst,
                COALESCE(p.total_igst, 0.0) AS total_igst,
                COALESCE(pid.item_discount_total, 0.0) AS item_discount_total,
                COALESCE(p.extra_charges, 0.0) AS extra_charges,
                COALESCE(p.round_off, 0.0) AS round_off,
                COALESCE(p.net_amount, 0.0) AS net_amount,
                COALESCE(p.paid_amount, 0.0) AS paid_amount,
                COALESCE(p.due_amount, 0.0) AS due_amount,
                COALESCE(p.remarks, '') AS remarks
            FROM purchases p
            LEFT JOIN suppliers s ON p.supplier_id = s.supplier_id
            LEFT JOIN (
                SELECT
                    purchase_id,
                    COALESCE(SUM(discount_amount), 0.0) AS item_discount_total
                FROM purchase_items
                GROUP BY purchase_id
            ) pid ON p.purchase_id = pid.purchase_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) <= ?)
              AND (? IS NULL OR p.supplier_id = ?)
              AND (? IS NULL OR UPPER(REPLACE(TRIM(COALESCE(p.payment_type, '')), ' ', '_')) = ?)
            ORDER BY
                substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) ASC,
                p.purchase_id ASC
            LIMIT ?;
        """
        params = (
            norm_from,
            norm_from,
            norm_to,
            norm_to,
            valid_supp_id,
            valid_supp_id,
            valid_pay_type,
            valid_pay_type,
            MAX_EXPORT_ROWS + 1,
        )
        company_settings, rows, extra_meta = self._read_snapshot(
            sql, params, supplier_id=valid_supp_id
        )

        banner_from, banner_to, filename = _build_period_banner_and_filename(
            "Purchase_Register", norm_from, norm_to, extra_meta
        )
        if valid_pay_type:
            extra_meta["Payment Mode"] = _format_payment_mode_label(valid_pay_type)
        extra_meta["Bills"] = len(rows)
        extra_meta["Returns Note"] = "Gross inward purchases (purchase returns not implemented)"

        headers = [
            "Purchase ID",
            "Purchase Date",
            "Supplier Invoice No",
            "Challan No",
            "Supplier ID",
            "Supplier Name",
            "Supplier GSTIN",
            "Taxable Value (₹)",
            "CGST (₹)",
            "SGST (₹)",
            "IGST (₹)",
            "Item Discount (₹)",
            "Extra Charges (₹)",
            "Round Off (₹)",
            "Purchase Total (₹)",
            "Amount Paid (₹)",
            "Recorded Purchase Due (₹)",
            "Payment Mode",
            "Purchase Status",
            "Remarks",
        ]
        num_cols = len(headers)

        wb = create_workbook("Purchase Register")
        ws = add_sheet(wb, "Purchase Register")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Purchase Register (Inward Bills Summary)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws,
            PURCHASE_RECORDED_DUE_NOTE,
            row_idx=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        totals = {idx: Decimal("0") for idx in range(8, 18)}
        cur_row = data_start_row

        for pur in rows:
            taxable = _to_decimal(pur.get("total_taxable"))
            cgst = _to_decimal(pur.get("total_cgst"))
            sgst = _to_decimal(pur.get("total_sgst"))
            igst = _to_decimal(pur.get("total_igst"))
            item_disc = _to_decimal(pur.get("item_discount_total"))
            extra_chg = _to_decimal(pur.get("extra_charges"))
            round_off = _to_decimal(pur.get("round_off"))
            net_amt = _to_decimal(pur.get("net_amount"))
            paid_amt = _to_decimal(pur.get("paid_amount"))
            raw_due = _to_decimal(pur.get("due_amount"))
            outstanding_amt, pur_status = _derive_outstanding_and_status(net_amt, paid_amt, raw_due)

            totals[8] += taxable
            totals[9] += cgst
            totals[10] += sgst
            totals[11] += igst
            totals[12] += item_disc
            totals[13] += extra_chg
            totals[14] += round_off
            totals[15] += net_amt
            totals[16] += paid_amt
            totals[17] += outstanding_amt

            _write_int_cell(ws, cur_row, 1, int(pur["purchase_id"]))
            write_date_cell(ws, cur_row, 2, pur.get("purchase_date") or "")
            write_text_cell(
                ws, cur_row, 3, pur.get("supplier_invoice_no") or "", alignment=_ALIGN_CENTER
            )
            write_text_cell(ws, cur_row, 4, pur.get("challan_no") or "", alignment=_ALIGN_CENTER)
            _write_int_cell(ws, cur_row, 5, int(pur["supplier_id"]))
            write_text_cell(ws, cur_row, 6, pur.get("supplier_name") or "")
            write_text_cell(
                ws, cur_row, 7, str(pur.get("supplier_gstin") or "").upper(), alignment=_ALIGN_CENTER
            )
            write_currency_cell(ws, cur_row, 8, taxable)
            write_currency_cell(ws, cur_row, 9, cgst)
            write_currency_cell(ws, cur_row, 10, sgst)
            write_currency_cell(ws, cur_row, 11, igst)
            write_currency_cell(ws, cur_row, 12, item_disc)
            write_currency_cell(ws, cur_row, 13, extra_chg)
            write_currency_cell(ws, cur_row, 14, round_off)
            write_currency_cell(ws, cur_row, 15, net_amt)
            write_currency_cell(ws, cur_row, 16, paid_amt)
            write_currency_cell(
                ws,
                cur_row,
                17,
                outstanding_amt,
                font=_FONT_STATUS_DANGER if outstanding_amt > Decimal("0.01") else None,
            )
            write_text_cell(
                ws,
                cur_row,
                18,
                _format_payment_mode_label(pur.get("payment_type")),
                alignment=_ALIGN_CENTER,
            )
            write_text_cell(
                ws,
                cur_row,
                19,
                pur_status,
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(pur_status),
            )
            write_text_cell(ws, cur_row, 20, pur.get("remarks") or "")
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )
            write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals=totals,
                num_cols=num_cols,
                label="TOTAL PURCHASES & RECORDED PURCHASE DUE",
                label_col=1,
                currency_cols=list(range(8, 18)),
            )
            ws.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=7)

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        return save_workbook_to_bytes(wb), filename

    # ------------------------------------------------------------------
    # 8. Purchase Item Details Export (Stage C3-B2B — ADMIN ONLY)
    # ------------------------------------------------------------------
    def export_purchase_item_details(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        supplier_id: Optional[int] = None,
        product_id: Optional[int] = None,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the ADMIN-only Purchase Item Details workbook (.xlsx) from `purchase_items` & `purchases`.
        - Uses historical purchase line-item values stored on `purchase_items` (never `stock_batches` or
          today's `products.default_purchase_rate` master rates).
        - Exposes zero supplier PAN, bank account, IFSC, UPI ID, or auth credentials.
        """
        norm_from, norm_to = normalize_optional_date_filters(from_date, to_date)
        valid_supp_id = _validate_positive_id(supplier_id, "supplier_id")
        valid_prod_id = _validate_positive_id(product_id, "product_id")

        sql = """
            SELECT
                pi.item_id,
                p.purchase_id,
                COALESCE(p.invoice_no, '') AS supplier_invoice_no,
                p.purchase_date,
                p.supplier_id,
                COALESCE(s.supplier_name, 'Supplier #' || p.supplier_id) AS supplier_name,
                pi.product_id,
                COALESCE(pr.product_name, 'Product #' || pi.product_id) AS product_name,
                COALESCE(pr.hsn_code, '') AS hsn_code,
                COALESCE(pi.batch_no, '') AS batch_no,
                COALESCE(pi.mfg_date, '') AS mfg_date,
                COALESCE(pi.exp_date, '') AS exp_date,
                COALESCE(pi.qty, 0.0) AS qty,
                COALESCE(pi.free_qty, 0.0) AS free_qty,
                COALESCE(
                    NULLIF(TRIM(u_pi.symbol), ''),
                    u_pi.unit_name,
                    NULLIF(TRIM(u_pr.symbol), ''),
                    u_pr.unit_name,
                    ''
                ) AS unit_label,
                COALESCE(pi.purchase_rate, 0.0) AS purchase_rate,
                COALESCE(pi.discount_percent, 0.0) AS discount_percent,
                COALESCE(pi.discount_amount, 0.0) AS discount_amount,
                COALESCE(pi.taxable_amount, 0.0) AS taxable_amount,
                COALESCE(pi.cgst_rate, 0.0) AS cgst_rate,
                COALESCE(pi.cgst_amount, 0.0) AS cgst_amount,
                COALESCE(pi.sgst_rate, 0.0) AS sgst_rate,
                COALESCE(pi.sgst_amount, 0.0) AS sgst_amount,
                COALESCE(pi.igst_rate, 0.0) AS igst_rate,
                COALESCE(pi.igst_amount, 0.0) AS igst_amount,
                COALESCE(pi.total_amount, 0.0) AS total_amount
            FROM purchase_items pi
            JOIN purchases p ON pi.purchase_id = p.purchase_id
            LEFT JOIN suppliers s ON p.supplier_id = s.supplier_id
            LEFT JOIN products pr ON pi.product_id = pr.product_id
            LEFT JOIN units u_pi ON pi.unit_id = u_pi.unit_id
            LEFT JOIN units u_pr ON pr.unit_id = u_pr.unit_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) <= ?)
              AND (? IS NULL OR p.supplier_id = ?)
              AND (? IS NULL OR pi.product_id = ?)
            ORDER BY
                substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) ASC,
                p.purchase_id ASC,
                pi.item_id ASC
            LIMIT ?;
        """
        params = (
            norm_from,
            norm_from,
            norm_to,
            norm_to,
            valid_supp_id,
            valid_supp_id,
            valid_prod_id,
            valid_prod_id,
            MAX_EXPORT_ROWS + 1,
        )
        company_settings, rows, extra_meta = self._read_snapshot(
            sql, params, product_id=valid_prod_id, supplier_id=valid_supp_id
        )

        banner_from, banner_to, filename = _build_period_banner_and_filename(
            "Purchase_Item_Details", norm_from, norm_to, extra_meta
        )
        extra_meta["Line Items"] = len(rows)
        extra_meta["Returns Note"] = "Gross inward purchase items (purchase returns not implemented)"

        headers = [
            "Purchase ID",
            "Supplier Invoice No",
            "Purchase Date",
            "Supplier ID",
            "Supplier Name",
            "Product ID",
            "Product Name",
            "HSN Code",
            "Batch No",
            "Mfg Date",
            "Expiry Date",
            "Qty",
            "Free Qty",
            "Unit",
            "Purchase Rate (₹)",
            "Discount (%)",
            "Discount (₹)",
            "Taxable Amount (₹)",
            "GST Rate (%)",
            "CGST (₹)",
            "SGST (₹)",
            "IGST (₹)",
            "Line Total (₹)",
        ]
        num_cols = len(headers)

        wb = create_workbook("Purchase Item Details")
        ws = add_sheet(wb, "Purchase Item Details")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Purchase Line-Item Details Report",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        totals = {idx: Decimal("0") for idx in (17, 18, 20, 21, 22, 23)}
        cur_row = data_start_row

        for item in rows:
            disc_amt = _to_decimal(item.get("discount_amount"))
            taxable_amt = _to_decimal(item.get("taxable_amount"))
            cgst_rate = _to_decimal(item.get("cgst_rate"))
            sgst_rate = _to_decimal(item.get("sgst_rate"))
            igst_rate = _to_decimal(item.get("igst_rate"))
            gst_rate = cgst_rate + sgst_rate + igst_rate
            cgst_amt = _to_decimal(item.get("cgst_amount"))
            sgst_amt = _to_decimal(item.get("sgst_amount"))
            igst_amt = _to_decimal(item.get("igst_amount"))
            line_total = _to_decimal(item.get("total_amount"))

            totals[17] += disc_amt
            totals[18] += taxable_amt
            totals[20] += cgst_amt
            totals[21] += sgst_amt
            totals[22] += igst_amt
            totals[23] += line_total

            _write_int_cell(ws, cur_row, 1, int(item["purchase_id"]))
            write_text_cell(
                ws, cur_row, 2, item.get("supplier_invoice_no") or "", alignment=_ALIGN_CENTER
            )
            write_date_cell(ws, cur_row, 3, item.get("purchase_date") or "")
            _write_int_cell(ws, cur_row, 4, int(item["supplier_id"]))
            write_text_cell(ws, cur_row, 5, item.get("supplier_name") or "")
            _write_int_cell(ws, cur_row, 6, int(item["product_id"]))
            write_text_cell(ws, cur_row, 7, item.get("product_name") or "")
            write_text_cell(ws, cur_row, 8, item.get("hsn_code") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 9, item.get("batch_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws, cur_row, 10, item.get("mfg_date") or "")
            write_date_cell(ws, cur_row, 11, item.get("exp_date") or "")
            write_quantity_cell(ws, cur_row, 12, _to_decimal(item.get("qty")), decimals=3)
            write_quantity_cell(ws, cur_row, 13, _to_decimal(item.get("free_qty")), decimals=3)
            write_text_cell(ws, cur_row, 14, item.get("unit_label") or "", alignment=_ALIGN_CENTER)
            write_currency_cell(ws, cur_row, 15, _to_decimal(item.get("purchase_rate")))
            write_percentage_cell(ws, cur_row, 16, _to_decimal(item.get("discount_percent")))
            write_currency_cell(ws, cur_row, 17, disc_amt)
            write_currency_cell(ws, cur_row, 18, taxable_amt)
            write_percentage_cell(ws, cur_row, 19, gst_rate)
            write_currency_cell(ws, cur_row, 20, cgst_amt)
            write_currency_cell(ws, cur_row, 21, sgst_amt)
            write_currency_cell(ws, cur_row, 22, igst_amt)
            write_currency_cell(ws, cur_row, 23, line_total)
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )
            write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals=totals,
                num_cols=num_cols,
                label="TOTAL PURCHASE ITEMS",
                label_col=1,
                currency_cols=[17, 18, 20, 21, 22, 23],
            )

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        return save_workbook_to_bytes(wb), filename

    # ------------------------------------------------------------------
    # 9. Customer Ledger Export (Stage C3-B3A — ADMIN + OPERATOR)
    # ------------------------------------------------------------------
    def export_customer_ledger(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        customer_id: Optional[int] = None,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the Customer Ledger workbook (.xlsx) combining:
        - Sales invoices (`sales`)
        - Standalone Customer Receipt vouchers (`vouchers` WHERE voucher_type='RECEIPT' AND reference_type!='SALE')
        - Sales Returns (`sales_returns`)
        Omits fabricated sequential running balances and surfaces stored opening/current ledger balances cleanly.
        Excludes Aadhaar and internal credentials completely.
        """
        norm_from, norm_to = normalize_optional_date_filters(from_date, to_date)
        valid_cust_id = _validate_positive_id(customer_id, "customer_id")

        sql_customers = """
            SELECT
                c.customer_id,
                c.customer_name,
                COALESCE(c.village, '') AS village,
                COALESCE(c.mobile, '') AS mobile,
                COALESCE(c.opening_balance, 0.0) AS opening_balance,
                COALESCE(c.current_balance, 0.0) AS current_balance
            FROM customers c
            WHERE (? IS NULL OR c.customer_id = ?)
            ORDER BY c.customer_name ASC, c.customer_id ASC;
        """
        sql_sales = """
            SELECT
                s.sale_id,
                COALESCE(s.invoice_no, '') AS invoice_no,
                substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) AS tx_date,
                s.customer_id,
                COALESCE(c.customer_name, 'Customer #' || s.customer_id) AS customer_name,
                COALESCE(c.village, '') AS village,
                COALESCE(c.mobile, '') AS mobile,
                COALESCE(s.payment_mode, 'CASH') AS payment_mode,
                COALESCE(s.net_amount, 0.0) AS net_amount,
                COALESCE(s.paid_amount, 0.0) AS paid_amount,
                COALESCE(s.due_amount, 0.0) AS due_amount,
                COALESCE(s.remarks, '') AS remarks,
                COALESCE(vlink.voucher_no, '') AS linked_voucher_no
            FROM sales s
            LEFT JOIN customers c ON s.customer_id = c.customer_id
            LEFT JOIN (
                SELECT reference_id AS sale_id, MAX(voucher_no) AS voucher_no
                FROM vouchers
                WHERE UPPER(COALESCE(reference_type, '')) = 'SALE'
                  AND reference_id IS NOT NULL
                GROUP BY reference_id
            ) vlink ON s.sale_id = vlink.sale_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
              AND (? IS NULL OR s.customer_id = ?)
            ORDER BY tx_date ASC, s.sale_id ASC
            LIMIT ?;
        """
        sql_receipts = """
            SELECT
                v.voucher_id,
                COALESCE(v.voucher_no, '') AS voucher_no,
                substr(TRIM(COALESCE(v.voucher_date, '')), 1, 10) AS voucher_date,
                COALESCE(v.voucher_type, '') AS voucher_type,
                COALESCE(v.total_amount, 0.0) AS total_amount,
                COALESCE(v.narration, '') AS narration,
                COALESCE(v.reference_type, '') AS reference_type,
                v.reference_id,
                COALESCE(la.account_name, '') AS account_name,
                COALESCE(le.particulars, '') AS particulars
            FROM vouchers v
            LEFT JOIN ledger_entries le ON v.voucher_id = le.voucher_id
            LEFT JOIN ledger_accounts la ON le.account_id = la.account_id
            WHERE UPPER(COALESCE(v.voucher_type, '')) = 'RECEIPT'
              AND UPPER(COALESCE(v.reference_type, '')) != 'SALE'
              AND (? IS NULL OR substr(TRIM(COALESCE(v.voucher_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(v.voucher_date, '')), 1, 10) <= ?)
            ORDER BY voucher_date ASC, v.voucher_id ASC, le.entry_id ASC;
        """
        sql_returns = """
            SELECT
                sr.return_id,
                COALESCE(sr.return_no, '') AS return_no,
                substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) AS tx_date,
                sr.sale_id,
                COALESCE(s.invoice_no, '') AS original_invoice_no,
                sr.customer_id,
                COALESCE(c.customer_name, 'Customer #' || sr.customer_id) AS customer_name,
                COALESCE(c.village, '') AS village,
                COALESCE(c.mobile, '') AS mobile,
                COALESCE(sr.net_amount, 0.0) AS net_amount,
                COALESCE(sr.remarks, '') AS remarks
            FROM sales_returns sr
            LEFT JOIN sales s ON sr.sale_id = s.sale_id
            LEFT JOIN customers c ON sr.customer_id = c.customer_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) <= ?)
              AND (? IS NULL OR sr.customer_id = ?)
            ORDER BY tx_date ASC, sr.return_id ASC
            LIMIT ?;
        """

        queries = {
            "customers": (sql_customers, (valid_cust_id, valid_cust_id)),
            "sales": (
                sql_sales,
                (
                    norm_from,
                    norm_from,
                    norm_to,
                    norm_to,
                    valid_cust_id,
                    valid_cust_id,
                    MAX_EXPORT_ROWS + 1,
                ),
            ),
            "receipts": (sql_receipts, (norm_from, norm_from, norm_to, norm_to)),
            "returns": (
                sql_returns,
                (
                    norm_from,
                    norm_from,
                    norm_to,
                    norm_to,
                    valid_cust_id,
                    valid_cust_id,
                    MAX_EXPORT_ROWS + 1,
                ),
            ),
        }
        company_settings, data, extra_meta, entity_info = self._run_multi_snapshot_queries(
            queries,
            customer_id=valid_cust_id,
            enforce_row_limit_keys=("sales", "returns"),
        )

        customer_map: Dict[int, Dict[str, Any]] = {
            int(c["customer_id"]): c for c in data["customers"]
        }
        grouped_receipts = _group_voucher_rows(data["receipts"])

        entries: List[Dict[str, Any]] = []

        for s in data["sales"]:
            net_dec = _to_decimal(s.get("net_amount"))
            paid_dec = _to_decimal(s.get("paid_amount"))
            due_dec = _to_decimal(s.get("due_amount"))
            _, status_label = _derive_outstanding_and_status(net_dec, paid_dec, due_dec)
            mode_label = _format_payment_mode_label(s.get("payment_mode"))
            linked_vch = str(s.get("linked_voucher_no") or "").strip()
            inv_no = str(s.get("invoice_no") or "").strip() or f"SALE-{s['sale_id']}"
            desc = str(s.get("remarks") or "").strip()
            if not desc:
                desc = f"Invoice {inv_no} ({mode_label}) - Paid ₹{paid_dec:,.2f} of ₹{net_dec:,.2f}"
            entries.append(
                {
                    "tx_date": str(s.get("tx_date") or ""),
                    "source_order": 1,
                    "doc_id": int(s["sale_id"]),
                    "doc_no": inv_no,
                    "tx_type": "Sales Invoice",
                    "customer_id": int(s["customer_id"]),
                    "customer_name": s.get("customer_name") or f"Customer #{s['customer_id']}",
                    "village": s.get("village") or "",
                    "mobile": s.get("mobile") or "",
                    "ref_no": linked_vch or "",
                    "payment_mode": mode_label,
                    "debit": net_dec,
                    "credit": paid_dec,
                    "status": status_label,
                    "description": desc,
                }
            )

        for v in grouped_receipts:
            cid = _resolve_voucher_customer_id(v, customer_map)
            if cid is None:
                continue
            if valid_cust_id is not None and cid != valid_cust_id:
                continue
            cust = customer_map.get(cid, {})
            raw_mode, extracted_ref = _extract_voucher_mode_and_ref(v)
            amt_dec = _to_decimal(v.get("total_amount"))
            vch_no = str(v.get("voucher_no") or "").strip() or f"RCP-{v['voucher_id']}"
            entries.append(
                {
                    "tx_date": str(v.get("voucher_date") or ""),
                    "source_order": 2,
                    "doc_id": int(v["voucher_id"]),
                    "doc_no": vch_no,
                    "tx_type": "Receipt Voucher",
                    "customer_id": cid,
                    "customer_name": cust.get("customer_name") or f"Customer #{cid}",
                    "village": cust.get("village") or "",
                    "mobile": cust.get("mobile") or "",
                    "ref_no": extracted_ref or "",
                    "payment_mode": _format_payment_mode_label(raw_mode),
                    "debit": Decimal("0.00"),
                    "credit": amt_dec,
                    "status": "Inflow (Receipt)",
                    "description": str(v.get("narration") or "").strip()
                    or "Customer collection receipt",
                }
            )

        for sr in data["returns"]:
            amt_dec = _to_decimal(sr.get("net_amount"))
            ret_no = str(sr.get("return_no") or "").strip() or f"RET-{sr['return_id']}"
            orig_inv = str(sr.get("original_invoice_no") or "").strip()
            ref_no = orig_inv or (f"Sale #{sr['sale_id']}" if sr.get("sale_id") else "")
            desc = str(sr.get("remarks") or "").strip()
            if not desc:
                desc = (
                    f"Sales return against {orig_inv}"
                    if orig_inv
                    else "Sales return credit adjustment"
                )
            entries.append(
                {
                    "tx_date": str(sr.get("tx_date") or ""),
                    "source_order": 3,
                    "doc_id": int(sr["return_id"]),
                    "doc_no": ret_no,
                    "tx_type": "Sales Return",
                    "customer_id": int(sr["customer_id"]),
                    "customer_name": sr.get("customer_name") or f"Customer #{sr['customer_id']}",
                    "village": sr.get("village") or "",
                    "mobile": sr.get("mobile") or "",
                    "ref_no": ref_no,
                    "payment_mode": "Adjustment",
                    "debit": Decimal("0.00"),
                    "credit": amt_dec,
                    "status": "Credit Adjustment",
                    "description": desc,
                }
            )

        if len(entries) > MAX_EXPORT_ROWS:
            raise ExcelValidationError(
                f"Export exceeds maximum limit of {MAX_EXPORT_ROWS:,} rows. "
                "Please narrow your customer or date range filter."
            )

        entries.sort(key=lambda e: (e["tx_date"], e["source_order"], e["doc_id"]))

        banner_from, banner_to, filename = _build_period_banner_and_filename(
            "Customer_Ledger", norm_from, norm_to, extra_meta
        )
        total_debit = sum((e["debit"] for e in entries), Decimal("0.00"))
        total_credit = sum((e["credit"] for e in entries), Decimal("0.00"))
        extra_meta["Entries"] = len(entries)

        if valid_cust_id is not None and "customer" in entity_info:
            cust_info = entity_info["customer"]
            open_bal = _safe_currency_decimal(cust_info.get("opening_balance"))
            curr_bal = _safe_currency_decimal(cust_info.get("current_balance"))
            extra_meta["Opening Balance"] = (
                f"₹{open_bal:,.2f} ({_classify_customer_balance(open_bal)})"
            )
            extra_meta["Current Ledger Balance"] = (
                f"₹{curr_bal:,.2f} ({_classify_customer_balance(curr_bal)})"
            )
        else:
            sum_open = sum(
                (_safe_currency_decimal(c.get("opening_balance")) for c in data["customers"]),
                Decimal("0.00"),
            )
            sum_curr = sum(
                (_safe_currency_decimal(c.get("current_balance")) for c in data["customers"]),
                Decimal("0.00"),
            )
            extra_meta["Total Opening Balance"] = f"₹{sum_open:,.2f}"
            extra_meta["Total Current Ledger Balance"] = f"₹{sum_curr:,.2f}"

        extra_meta["Period Net (Dr - Cr)"] = f"₹{(total_debit - total_credit):,.2f}"

        headers = [
            "Date",
            "Voucher / Invoice No",
            "Transaction Type",
            "Customer ID",
            "Customer Name",
            "Village",
            "Mobile",
            "Reference No",
            "Payment Mode",
            "Debit / Billed (₹)",
            "Credit / Received (₹)",
            "Status",
            "Narration / Remarks",
        ]
        num_cols = len(headers)

        wb = create_workbook("Customer Ledger")
        ws = add_sheet(wb, "Customer Ledger")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Customer Ledger (Invoices, Receipts & Returns)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws,
            CUSTOMER_LEDGER_NOTE,
            row_idx=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        cur_row = data_start_row
        for item in entries:
            write_date_cell(ws, cur_row, 1, item["tx_date"])
            write_text_cell(ws, cur_row, 2, item["doc_no"], alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 3, item["tx_type"], alignment=_ALIGN_CENTER)
            _write_int_cell(ws, cur_row, 4, item["customer_id"])
            write_text_cell(ws, cur_row, 5, item["customer_name"])
            write_text_cell(ws, cur_row, 6, item["village"])
            write_text_cell(ws, cur_row, 7, item["mobile"], alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 8, item["ref_no"], alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 9, item["payment_mode"], alignment=_ALIGN_CENTER)
            write_currency_cell(ws, cur_row, 10, item["debit"])
            write_currency_cell(ws, cur_row, 11, item["credit"])
            write_text_cell(
                ws,
                cur_row,
                12,
                item["status"],
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(item["status"]),
            )
            write_text_cell(ws, cur_row, 13, item["description"])
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )
            write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals={10: total_debit, 11: total_credit},
                num_cols=num_cols,
                label="TOTAL CUSTOMER LEDGER TRANSACTIONS",
                label_col=1,
                currency_cols=[10, 11],
            )
            ws.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=9)

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        return save_workbook_to_bytes(wb), filename

    # ------------------------------------------------------------------
    # 10. Supplier Ledger Export (Stage C3-B3A — ADMIN ONLY)
    # ------------------------------------------------------------------
    def export_supplier_ledger(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        supplier_id: Optional[int] = None,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the ADMIN-only Supplier Ledger workbook (.xlsx) combining:
        - Inward purchase bills (`purchases`)
        - Supplier payment vouchers (`vouchers` WHERE voucher_type='PAYMENT' AND reference_type!='EXPENSE')
        Omits fabricated sequential running balances and surfaces stored opening/current payable balances cleanly.
        Excludes PAN, bank account, IFSC, UPI ID, and auth credentials completely.
        """
        norm_from, norm_to = normalize_optional_date_filters(from_date, to_date)
        valid_supp_id = _validate_positive_id(supplier_id, "supplier_id")

        sql_suppliers = """
            SELECT
                s.supplier_id,
                s.supplier_name,
                COALESCE(s.city, '') AS city,
                COALESCE(s.mobile, '') AS mobile,
                COALESCE(s.opening_balance, 0.0) AS opening_balance,
                COALESCE(s.current_balance, 0.0) AS current_balance
            FROM suppliers s
            WHERE (? IS NULL OR s.supplier_id = ?)
            ORDER BY s.supplier_name ASC, s.supplier_id ASC;
        """
        sql_purchases = """
            SELECT
                p.purchase_id,
                COALESCE(p.invoice_no, '') AS invoice_no,
                substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) AS tx_date,
                p.supplier_id,
                COALESCE(s.supplier_name, 'Supplier #' || p.supplier_id) AS supplier_name,
                COALESCE(s.city, '') AS city,
                COALESCE(s.mobile, '') AS mobile,
                COALESCE(p.challan_no, '') AS challan_no,
                COALESCE(p.payment_type, 'CREDIT') AS payment_type,
                COALESCE(p.net_amount, 0.0) AS net_amount,
                COALESCE(p.paid_amount, 0.0) AS paid_amount,
                COALESCE(p.due_amount, 0.0) AS due_amount,
                COALESCE(p.remarks, '') AS remarks
            FROM purchases p
            LEFT JOIN suppliers s ON p.supplier_id = s.supplier_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) <= ?)
              AND (? IS NULL OR p.supplier_id = ?)
            ORDER BY tx_date ASC, p.purchase_id ASC
            LIMIT ?;
        """
        sql_payments = """
            SELECT
                v.voucher_id,
                COALESCE(v.voucher_no, '') AS voucher_no,
                substr(TRIM(COALESCE(v.voucher_date, '')), 1, 10) AS voucher_date,
                COALESCE(v.voucher_type, '') AS voucher_type,
                COALESCE(v.total_amount, 0.0) AS total_amount,
                COALESCE(v.narration, '') AS narration,
                COALESCE(v.reference_type, '') AS reference_type,
                v.reference_id,
                COALESCE(la.account_name, '') AS account_name,
                COALESCE(le.particulars, '') AS particulars
            FROM vouchers v
            LEFT JOIN ledger_entries le ON v.voucher_id = le.voucher_id
            LEFT JOIN ledger_accounts la ON le.account_id = la.account_id
            WHERE UPPER(COALESCE(v.voucher_type, '')) = 'PAYMENT'
              AND UPPER(COALESCE(v.reference_type, '')) != 'EXPENSE'
              AND (? IS NULL OR substr(TRIM(COALESCE(v.voucher_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(v.voucher_date, '')), 1, 10) <= ?)
            ORDER BY voucher_date ASC, v.voucher_id ASC, le.entry_id ASC;
        """

        queries = {
            "suppliers": (sql_suppliers, (valid_supp_id, valid_supp_id)),
            "purchases": (
                sql_purchases,
                (
                    norm_from,
                    norm_from,
                    norm_to,
                    norm_to,
                    valid_supp_id,
                    valid_supp_id,
                    MAX_EXPORT_ROWS + 1,
                ),
            ),
            "payments": (sql_payments, (norm_from, norm_from, norm_to, norm_to)),
        }
        company_settings, data, extra_meta, entity_info = self._run_multi_snapshot_queries(
            queries,
            supplier_id=valid_supp_id,
            enforce_row_limit_keys=("purchases",),
        )

        supplier_map: Dict[int, Dict[str, Any]] = {
            int(s["supplier_id"]): s for s in data["suppliers"]
        }
        grouped_payments = _group_voucher_rows(data["payments"])

        entries: List[Dict[str, Any]] = []

        for p in data["purchases"]:
            net_dec = _to_decimal(p.get("net_amount"))
            paid_dec = _to_decimal(p.get("paid_amount"))
            due_dec = _to_decimal(p.get("due_amount"))
            _, status_label = _derive_outstanding_and_status(net_dec, paid_dec, due_dec)
            mode_label = _format_payment_mode_label(p.get("payment_type"))
            inv_no = str(p.get("invoice_no") or "").strip() or f"PUR-{p['purchase_id']}"
            challan = str(p.get("challan_no") or "").strip()
            desc = str(p.get("remarks") or "").strip()
            if not desc:
                desc = (
                    f"Purchase bill {inv_no} ({mode_label}) - "
                    f"Paid ₹{paid_dec:,.2f} of ₹{net_dec:,.2f}"
                )
            entries.append(
                {
                    "tx_date": str(p.get("tx_date") or ""),
                    "source_order": 1,
                    "doc_id": int(p["purchase_id"]),
                    "doc_no": inv_no,
                    "tx_type": "Purchase Bill",
                    "supplier_id": int(p["supplier_id"]),
                    "supplier_name": p.get("supplier_name") or f"Supplier #{p['supplier_id']}",
                    "city": p.get("city") or "",
                    "mobile": p.get("mobile") or "",
                    "ref_no": challan or "",
                    "payment_mode": mode_label,
                    "debit": paid_dec,
                    "credit": net_dec,
                    "status": status_label,
                    "description": desc,
                }
            )

        for v in grouped_payments:
            sid = _resolve_voucher_supplier_id(v, supplier_map)
            if sid is None:
                continue
            if valid_supp_id is not None and sid != valid_supp_id:
                continue
            supp = supplier_map.get(sid, {})
            raw_mode, extracted_ref = _extract_voucher_mode_and_ref(v)
            amt_dec = _to_decimal(v.get("total_amount"))
            vch_no = str(v.get("voucher_no") or "").strip() or f"PAY-{v['voucher_id']}"
            entries.append(
                {
                    "tx_date": str(v.get("voucher_date") or ""),
                    "source_order": 2,
                    "doc_id": int(v["voucher_id"]),
                    "doc_no": vch_no,
                    "tx_type": "Supplier Payment Voucher",
                    "supplier_id": sid,
                    "supplier_name": supp.get("supplier_name") or f"Supplier #{sid}",
                    "city": supp.get("city") or "",
                    "mobile": supp.get("mobile") or "",
                    "ref_no": extracted_ref or "",
                    "payment_mode": _format_payment_mode_label(raw_mode),
                    "debit": amt_dec,
                    "credit": Decimal("0.00"),
                    "status": "Outflow (Payment)",
                    "description": str(v.get("narration") or "").strip()
                    or "Payment to supplier",
                }
            )

        if len(entries) > MAX_EXPORT_ROWS:
            raise ExcelValidationError(
                f"Export exceeds maximum limit of {MAX_EXPORT_ROWS:,} rows. "
                "Please narrow your supplier or date range filter."
            )

        entries.sort(key=lambda e: (e["tx_date"], e["source_order"], e["doc_id"]))

        banner_from, banner_to, filename = _build_period_banner_and_filename(
            "Supplier_Ledger", norm_from, norm_to, extra_meta
        )
        total_debit = sum((e["debit"] for e in entries), Decimal("0.00"))
        total_credit = sum((e["credit"] for e in entries), Decimal("0.00"))
        extra_meta["Entries"] = len(entries)

        if valid_supp_id is not None and "supplier" in entity_info:
            supp_info = entity_info["supplier"]
            open_bal = _safe_currency_decimal(supp_info.get("opening_balance"))
            curr_bal = _safe_currency_decimal(supp_info.get("current_balance"))
            extra_meta["Opening Balance"] = (
                f"₹{open_bal:,.2f} ({_classify_supplier_balance(open_bal)})"
            )
            extra_meta["Current Ledger Balance"] = (
                f"₹{curr_bal:,.2f} ({_classify_supplier_balance(curr_bal)})"
            )
        else:
            sum_open = sum(
                (_safe_currency_decimal(s.get("opening_balance")) for s in data["suppliers"]),
                Decimal("0.00"),
            )
            sum_curr = sum(
                (_safe_currency_decimal(s.get("current_balance")) for s in data["suppliers"]),
                Decimal("0.00"),
            )
            extra_meta["Total Opening Balance"] = f"₹{sum_open:,.2f}"
            extra_meta["Total Current Ledger Balance"] = f"₹{sum_curr:,.2f}"

        extra_meta["Period Payable Net (Cr - Dr)"] = f"₹{(total_credit - total_debit):,.2f}"

        headers = [
            "Date",
            "Voucher / Bill No",
            "Transaction Type",
            "Supplier ID",
            "Supplier Name",
            "City",
            "Mobile",
            "Reference / Challan No",
            "Payment Mode",
            "Debit / Paid (₹)",
            "Credit / Billed (₹)",
            "Status",
            "Narration / Remarks",
        ]
        num_cols = len(headers)

        wb = create_workbook("Supplier Ledger")
        ws = add_sheet(wb, "Supplier Ledger")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Supplier Ledger (Purchase Bills & Payment Vouchers)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws,
            SUPPLIER_LEDGER_NOTE,
            row_idx=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        cur_row = data_start_row
        for item in entries:
            write_date_cell(ws, cur_row, 1, item["tx_date"])
            write_text_cell(ws, cur_row, 2, item["doc_no"], alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 3, item["tx_type"], alignment=_ALIGN_CENTER)
            _write_int_cell(ws, cur_row, 4, item["supplier_id"])
            write_text_cell(ws, cur_row, 5, item["supplier_name"])
            write_text_cell(ws, cur_row, 6, item["city"])
            write_text_cell(ws, cur_row, 7, item["mobile"], alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 8, item["ref_no"], alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 9, item["payment_mode"], alignment=_ALIGN_CENTER)
            write_currency_cell(ws, cur_row, 10, item["debit"])
            write_currency_cell(ws, cur_row, 11, item["credit"])
            write_text_cell(
                ws,
                cur_row,
                12,
                item["status"],
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(item["status"]),
            )
            write_text_cell(ws, cur_row, 13, item["description"])
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )
            write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals={10: total_debit, 11: total_credit},
                num_cols=num_cols,
                label="TOTAL SUPPLIER LEDGER TRANSACTIONS",
                label_col=1,
                currency_cols=[10, 11],
            )
            ws.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=9)

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        return save_workbook_to_bytes(wb), filename

    # ------------------------------------------------------------------
    # 11. Customer Outstanding Export (Stage C3-B3A — ADMIN + OPERATOR)
    # ------------------------------------------------------------------
    def export_customer_outstanding(
        self,
        *,
        query: str = "",
        village: str = "",
        only_outstanding: bool = False,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate Customer Outstanding Summary workbook (.xlsx) from `customers.current_balance`
        with Cartesian-safe pre-aggregated sales, returns, and standalone receipt vouchers.
        Never derives balance from historical `sales.due_amount` and never exposes Aadhaar.
        """
        clean_query = _validate_search_query(query)
        clean_village = _validate_search_query(village)
        like_query = _escape_like_pattern(clean_query) if clean_query else ""
        like_village = _escape_like_pattern(clean_village) if clean_village else ""
        only_flag = 1 if only_outstanding else 0

        sql_customers = """
            SELECT
                c.customer_id,
                c.customer_name,
                COALESCE(c.village, '') AS village,
                COALESCE(c.mobile, '') AS mobile,
                COALESCE(c.gstin, '') AS gstin,
                COALESCE(c.opening_balance, 0.0) AS opening_balance,
                COALESCE(c.current_balance, 0.0) AS current_balance,
                COALESCE(c.is_active, 1) AS is_active,
                COALESCE(sa.bills_count, 0) AS bills_count,
                COALESCE(sa.total_billed, 0.0) AS total_billed,
                COALESCE(sa.total_bill_paid, 0.0) AS total_bill_paid,
                COALESCE(sa.last_sale_date, '') AS last_sale_date,
                COALESCE(ra.total_returns, 0.0) AS total_returns,
                COALESCE(ra.last_return_date, '') AS last_return_date
            FROM customers c
            LEFT JOIN (
                SELECT
                    customer_id,
                    COUNT(*) AS bills_count,
                    COALESCE(SUM(net_amount), 0.0) AS total_billed,
                    COALESCE(SUM(paid_amount), 0.0) AS total_bill_paid,
                    MAX(substr(TRIM(COALESCE(sale_date, '')), 1, 10)) AS last_sale_date
                FROM sales
                GROUP BY customer_id
            ) sa ON c.customer_id = sa.customer_id
            LEFT JOIN (
                SELECT
                    customer_id,
                    COALESCE(SUM(net_amount), 0.0) AS total_returns,
                    MAX(substr(TRIM(COALESCE(return_date, '')), 1, 10)) AS last_return_date
                FROM sales_returns
                GROUP BY customer_id
            ) ra ON c.customer_id = ra.customer_id
            WHERE (c.is_active = 1 OR ABS(COALESCE(c.current_balance, 0.0)) > 0.005)
              AND (? = 0 OR ABS(COALESCE(c.current_balance, 0.0)) > 0.005)
              AND (
                  ? = ''
                  OR COALESCE(c.customer_name, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(c.mobile, '') LIKE ? ESCAPE '\\'
              )
              AND (? = '' OR COALESCE(c.village, '') LIKE ? ESCAPE '\\')
            ORDER BY
                COALESCE(c.current_balance, 0.0) DESC,
                c.customer_name ASC,
                c.customer_id ASC
            LIMIT ?;
        """
        sql_all_customer_ids = "SELECT customer_id FROM customers;"
        sql_receipts = """
            SELECT
                v.voucher_id,
                substr(TRIM(COALESCE(v.voucher_date, '')), 1, 10) AS voucher_date,
                COALESCE(v.voucher_type, '') AS voucher_type,
                COALESCE(v.total_amount, 0.0) AS total_amount,
                COALESCE(v.narration, '') AS narration,
                COALESCE(v.reference_type, '') AS reference_type,
                v.reference_id,
                COALESCE(la.account_name, '') AS account_name,
                COALESCE(le.particulars, '') AS particulars
            FROM vouchers v
            LEFT JOIN ledger_entries le ON v.voucher_id = le.voucher_id
            LEFT JOIN ledger_accounts la ON le.account_id = la.account_id
            WHERE UPPER(COALESCE(v.voucher_type, '')) = 'RECEIPT'
              AND UPPER(COALESCE(v.reference_type, '')) != 'SALE'
            ORDER BY v.voucher_id ASC, le.entry_id ASC;
        """

        queries = {
            "customers": (
                sql_customers,
                (
                    only_flag,
                    clean_query,
                    like_query,
                    like_query,
                    clean_village,
                    like_village,
                    MAX_EXPORT_ROWS + 1,
                ),
            ),
            "all_customer_ids": (sql_all_customer_ids, ()),
            "receipts": (sql_receipts, ()),
        }
        company_settings, data, extra_meta, _ = self._run_multi_snapshot_queries(
            queries,
            enforce_row_limit_keys=("customers",),
        )

        all_cust_map: Dict[int, Dict[str, Any]] = {
            int(r["customer_id"]): r for r in data["all_customer_ids"]
        }
        receipt_agg: Dict[int, Dict[str, Any]] = {}
        for v in _group_voucher_rows(data["receipts"]):
            cid = _resolve_voucher_customer_id(v, all_cust_map)
            if cid is None:
                continue
            bucket = receipt_agg.setdefault(
                cid, {"total": Decimal("0.00"), "last_date": ""}
            )
            bucket["total"] += _to_decimal(v.get("total_amount"))
            vdate = str(v.get("voucher_date") or "")
            if vdate > bucket["last_date"]:
                bucket["last_date"] = vdate

        rows = data["customers"]
        if clean_query:
            extra_meta["Search"] = clean_query
        if clean_village:
            extra_meta["Village"] = clean_village
        if only_outstanding:
            extra_meta["Filter"] = "Only Non-Zero Balances"
        extra_meta["Customers"] = len(rows)

        total_receivable = Decimal("0.00")
        total_advances = Decimal("0.00")
        net_balance = Decimal("0.00")
        for r in rows:
            curr = _safe_currency_decimal(r.get("current_balance"))
            net_balance += curr
            if curr > Decimal("0.005"):
                total_receivable += curr
            elif curr < Decimal("-0.005"):
                total_advances += abs(curr)

        extra_meta["Total Receivables (Dr)"] = f"₹{total_receivable:,.2f}"
        extra_meta["Total Advances (Cr)"] = f"₹{total_advances:,.2f}"
        extra_meta["Net Balance"] = f"₹{net_balance:,.2f}"

        headers = [
            "Customer ID",
            "Customer Name",
            "Village",
            "Mobile",
            "GSTIN",
            "Opening Balance (₹)",
            "Bills Count",
            "Total Billed (₹)",
            "Bill-Time Paid (₹)",
            "Receipt Vouchers (₹)",
            "Sales Returns (₹)",
            "Current Ledger Balance (₹)",
            "Balance Position",
            "Last Activity Date",
        ]
        num_cols = len(headers)

        wb = create_workbook("Customer Outstanding")
        ws = add_sheet(wb, "Customer Outstanding")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Customer Outstanding & Party Ledger Balances",
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws,
            CUSTOMER_OUTSTANDING_NOTE,
            row_idx=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        totals = {
            6: Decimal("0.00"),
            7: 0,
            8: Decimal("0.00"),
            9: Decimal("0.00"),
            10: Decimal("0.00"),
            11: Decimal("0.00"),
            12: Decimal("0.00"),
        }
        cur_row = data_start_row

        for cust in rows:
            cid = int(cust["customer_id"])
            rec_info = receipt_agg.get(cid, {"total": Decimal("0.00"), "last_date": ""})

            open_bal = _safe_currency_decimal(cust.get("opening_balance"))
            bills_cnt = int(cust.get("bills_count") or 0)
            tot_billed = _to_decimal(cust.get("total_billed"))
            bill_paid = _to_decimal(cust.get("total_bill_paid"))
            rec_vouchers = rec_info["total"]
            tot_returns = _to_decimal(cust.get("total_returns"))
            curr_bal = _safe_currency_decimal(cust.get("current_balance"))
            pos_label = _classify_customer_balance(curr_bal)

            last_candidates = [
                d
                for d in (
                    str(cust.get("last_sale_date") or "").strip(),
                    str(cust.get("last_return_date") or "").strip(),
                    str(rec_info["last_date"] or "").strip(),
                )
                if d
            ]
            last_activity = max(last_candidates) if last_candidates else ""

            totals[6] += open_bal
            totals[7] += bills_cnt
            totals[8] += tot_billed
            totals[9] += bill_paid
            totals[10] += rec_vouchers
            totals[11] += tot_returns
            totals[12] += curr_bal

            _write_int_cell(ws, cur_row, 1, cid)
            write_text_cell(ws, cur_row, 2, cust.get("customer_name") or f"Customer #{cid}")
            write_text_cell(ws, cur_row, 3, cust.get("village") or "")
            write_text_cell(ws, cur_row, 4, cust.get("mobile") or "", alignment=_ALIGN_CENTER)
            write_text_cell(
                ws,
                cur_row,
                5,
                str(cust.get("gstin") or "").upper(),
                alignment=_ALIGN_CENTER,
            )
            write_currency_cell(ws, cur_row, 6, open_bal)
            _write_int_cell(ws, cur_row, 7, bills_cnt)
            write_currency_cell(ws, cur_row, 8, tot_billed)
            write_currency_cell(ws, cur_row, 9, bill_paid)
            write_currency_cell(ws, cur_row, 10, rec_vouchers)
            write_currency_cell(ws, cur_row, 11, tot_returns)
            write_currency_cell(
                ws,
                cur_row,
                12,
                curr_bal,
                font=_resolve_status_font(pos_label) if pos_label != "Settled (Nil)" else None,
            )
            write_text_cell(
                ws,
                cur_row,
                13,
                pos_label,
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(pos_label),
            )
            write_date_cell(ws, cur_row, 14, last_activity or "")
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )
            write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals=totals,
                num_cols=num_cols,
                label="TOTAL CUSTOMER OUTSTANDING SUMMARY",
                label_col=1,
                currency_cols=[6, 8, 9, 10, 11, 12],
                quantity_cols=[7],
            )
            ws.cell(row=cur_row, column=7).alignment = _ALIGN_CENTER
            ws.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=5)

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        today_compact = datetime.now().strftime("%Y%m%d")
        return save_workbook_to_bytes(wb), f"Customer_Outstanding_{today_compact}.xlsx"

    # ------------------------------------------------------------------
    # 12. Supplier Outstanding Export (Stage C3-B3A — ADMIN ONLY)
    # ------------------------------------------------------------------
    def export_supplier_outstanding(
        self,
        *,
        query: str = "",
        only_outstanding: bool = False,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the ADMIN-only Supplier Outstanding Summary workbook (.xlsx) using
        `suppliers.current_balance` with Cartesian-safe purchase & voucher payment totals.
        Excludes PAN, bank account, IFSC, UPI ID, and auth secrets completely.
        """
        clean_query = _validate_search_query(query)
        like_query = _escape_like_pattern(clean_query) if clean_query else ""
        only_flag = 1 if only_outstanding else 0

        sql_suppliers = """
            SELECT
                s.supplier_id,
                s.supplier_name,
                COALESCE(s.contact_person, '') AS contact_person,
                COALESCE(s.city, '') AS city,
                COALESCE(s.mobile, '') AS mobile,
                COALESCE(s.gstin, '') AS gstin,
                COALESCE(s.opening_balance, 0.0) AS opening_balance,
                COALESCE(s.current_balance, 0.0) AS current_balance,
                COALESCE(s.is_active, 1) AS is_active,
                COALESCE(pa.bills_count, 0) AS bills_count,
                COALESCE(pa.total_purchases, 0.0) AS total_purchases,
                COALESCE(pa.total_bill_paid, 0.0) AS total_bill_paid,
                COALESCE(pa.last_purchase_date, '') AS last_purchase_date
            FROM suppliers s
            LEFT JOIN (
                SELECT
                    supplier_id,
                    COUNT(*) AS bills_count,
                    COALESCE(SUM(net_amount), 0.0) AS total_purchases,
                    COALESCE(SUM(paid_amount), 0.0) AS total_bill_paid,
                    MAX(substr(TRIM(COALESCE(purchase_date, '')), 1, 10)) AS last_purchase_date
                FROM purchases
                GROUP BY supplier_id
            ) pa ON s.supplier_id = pa.supplier_id
            WHERE (s.is_active = 1 OR ABS(COALESCE(s.current_balance, 0.0)) > 0.005)
              AND (? = 0 OR ABS(COALESCE(s.current_balance, 0.0)) > 0.005)
              AND (
                  ? = ''
                  OR COALESCE(s.supplier_name, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(s.contact_person, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(s.mobile, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(s.city, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(s.gstin, '') LIKE ? ESCAPE '\\'
              )
            ORDER BY
                COALESCE(s.current_balance, 0.0) DESC,
                s.supplier_name ASC,
                s.supplier_id ASC
            LIMIT ?;
        """
        sql_all_supplier_ids = "SELECT supplier_id FROM suppliers;"
        sql_payments = """
            SELECT
                v.voucher_id,
                substr(TRIM(COALESCE(v.voucher_date, '')), 1, 10) AS voucher_date,
                COALESCE(v.voucher_type, '') AS voucher_type,
                COALESCE(v.total_amount, 0.0) AS total_amount,
                COALESCE(v.narration, '') AS narration,
                COALESCE(v.reference_type, '') AS reference_type,
                v.reference_id,
                COALESCE(la.account_name, '') AS account_name,
                COALESCE(le.particulars, '') AS particulars
            FROM vouchers v
            LEFT JOIN ledger_entries le ON v.voucher_id = le.voucher_id
            LEFT JOIN ledger_accounts la ON le.account_id = la.account_id
            WHERE UPPER(COALESCE(v.voucher_type, '')) = 'PAYMENT'
              AND UPPER(COALESCE(v.reference_type, '')) != 'EXPENSE'
            ORDER BY v.voucher_id ASC, le.entry_id ASC;
        """

        queries = {
            "suppliers": (
                sql_suppliers,
                (
                    only_flag,
                    clean_query,
                    like_query,
                    like_query,
                    like_query,
                    like_query,
                    like_query,
                    MAX_EXPORT_ROWS + 1,
                ),
            ),
            "all_supplier_ids": (sql_all_supplier_ids, ()),
            "payments": (sql_payments, ()),
        }
        company_settings, data, extra_meta, _ = self._run_multi_snapshot_queries(
            queries,
            enforce_row_limit_keys=("suppliers",),
        )

        all_supp_map: Dict[int, Dict[str, Any]] = {
            int(s["supplier_id"]): s for s in data["all_supplier_ids"]
        }
        payment_agg: Dict[int, Dict[str, Any]] = {}
        for v in _group_voucher_rows(data["payments"]):
            sid = _resolve_voucher_supplier_id(v, all_supp_map)
            if sid is None:
                continue
            bucket = payment_agg.setdefault(
                sid, {"total": Decimal("0.00"), "last_date": ""}
            )
            bucket["total"] += _to_decimal(v.get("total_amount"))
            vdate = str(v.get("voucher_date") or "")
            if vdate > bucket["last_date"]:
                bucket["last_date"] = vdate

        rows = data["suppliers"]
        if clean_query:
            extra_meta["Search"] = clean_query
        if only_outstanding:
            extra_meta["Filter"] = "Only Non-Zero Balances"
        extra_meta["Suppliers"] = len(rows)

        total_payable = Decimal("0.00")
        total_advances = Decimal("0.00")
        net_balance = Decimal("0.00")
        for r in rows:
            curr = _safe_currency_decimal(r.get("current_balance"))
            net_balance += curr
            if curr > Decimal("0.005"):
                total_payable += curr
            elif curr < Decimal("-0.005"):
                total_advances += abs(curr)

        extra_meta["Total Payables (Cr)"] = f"₹{total_payable:,.2f}"
        extra_meta["Total Supplier Advances (Dr)"] = f"₹{total_advances:,.2f}"
        extra_meta["Net Payable Balance"] = f"₹{net_balance:,.2f}"

        headers = [
            "Supplier ID",
            "Supplier Name",
            "Contact Person",
            "City",
            "Mobile",
            "GSTIN",
            "Opening Balance (₹)",
            "Bills Count",
            "Total Purchases (₹)",
            "Bill-Time Paid (₹)",
            "Payment Vouchers (₹)",
            "Current Ledger Balance (₹)",
            "Balance Position",
            "Last Activity Date",
        ]
        num_cols = len(headers)

        wb = create_workbook("Supplier Outstanding")
        ws = add_sheet(wb, "Supplier Outstanding")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Supplier Outstanding & Payable Ledger Balances",
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws,
            SUPPLIER_OUTSTANDING_NOTE,
            row_idx=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        totals = {
            7: Decimal("0.00"),
            8: 0,
            9: Decimal("0.00"),
            10: Decimal("0.00"),
            11: Decimal("0.00"),
            12: Decimal("0.00"),
        }
        cur_row = data_start_row

        for supp in rows:
            sid = int(supp["supplier_id"])
            pay_info = payment_agg.get(sid, {"total": Decimal("0.00"), "last_date": ""})

            open_bal = _safe_currency_decimal(supp.get("opening_balance"))
            bills_cnt = int(supp.get("bills_count") or 0)
            tot_pur = _to_decimal(supp.get("total_purchases"))
            bill_paid = _to_decimal(supp.get("total_bill_paid"))
            pay_vouchers = pay_info["total"]
            curr_bal = _safe_currency_decimal(supp.get("current_balance"))
            pos_label = _classify_supplier_balance(curr_bal)

            last_candidates = [
                d
                for d in (
                    str(supp.get("last_purchase_date") or "").strip(),
                    str(pay_info["last_date"] or "").strip(),
                )
                if d
            ]
            last_activity = max(last_candidates) if last_candidates else ""

            totals[7] += open_bal
            totals[8] += bills_cnt
            totals[9] += tot_pur
            totals[10] += bill_paid
            totals[11] += pay_vouchers
            totals[12] += curr_bal

            _write_int_cell(ws, cur_row, 1, sid)
            write_text_cell(ws, cur_row, 2, supp.get("supplier_name") or f"Supplier #{sid}")
            write_text_cell(ws, cur_row, 3, supp.get("contact_person") or "")
            write_text_cell(ws, cur_row, 4, supp.get("city") or "")
            write_text_cell(ws, cur_row, 5, supp.get("mobile") or "", alignment=_ALIGN_CENTER)
            write_text_cell(
                ws,
                cur_row,
                6,
                str(supp.get("gstin") or "").upper(),
                alignment=_ALIGN_CENTER,
            )
            write_currency_cell(ws, cur_row, 7, open_bal)
            _write_int_cell(ws, cur_row, 8, bills_cnt)
            write_currency_cell(ws, cur_row, 9, tot_pur)
            write_currency_cell(ws, cur_row, 10, bill_paid)
            write_currency_cell(ws, cur_row, 11, pay_vouchers)
            write_currency_cell(
                ws,
                cur_row,
                12,
                curr_bal,
                font=_resolve_status_font(pos_label) if pos_label != "Settled (Nil)" else None,
            )
            write_text_cell(
                ws,
                cur_row,
                13,
                pos_label,
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(pos_label),
            )
            write_date_cell(ws, cur_row, 14, last_activity or "")
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )
            write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals=totals,
                num_cols=num_cols,
                label="TOTAL SUPPLIER OUTSTANDING SUMMARY",
                label_col=1,
                currency_cols=[7, 9, 10, 11, 12],
                quantity_cols=[8],
            )
            ws.cell(row=cur_row, column=8).alignment = _ALIGN_CENTER
            ws.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=6)

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        today_compact = datetime.now().strftime("%Y%m%d")
        return save_workbook_to_bytes(wb), f"Supplier_Outstanding_{today_compact}.xlsx"

    # ------------------------------------------------------------------
    # 13. Payment History Export (Stage C3-B3A — ADMIN ONLY)
    # ------------------------------------------------------------------
    def export_payment_history(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        payment_mode: Optional[str] = None,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the ADMIN-only Consolidated Payment History workbook (.xlsx) without duplicate rows across:
        1. Sales invoice bill-time payments (`sales.paid_amount > 0`)
        2. Purchase bill-time payments (`purchases.paid_amount > 0`)
        3. Standalone RECEIPT / PAYMENT / CONTRA vouchers (`reference_type NOT IN ('SALE', 'EXPENSE')`)
        4. Shop expenses (`expenses`, joined with linked voucher numbers where present)
        Totals row sums inflows and outflows separately; historical 'UPI' rows stay 'UPI (Historical)'.
        """
        norm_from, norm_to = normalize_optional_date_filters(from_date, to_date)
        valid_pay_mode = _validate_payment_mode_filter(payment_mode, field_name="payment_mode")

        sql_sale_payments = """
            SELECT
                s.sale_id,
                COALESCE(s.invoice_no, '') AS invoice_no,
                substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) AS tx_date,
                s.customer_id,
                COALESCE(c.customer_name, 'Customer #' || s.customer_id) AS customer_name,
                COALESCE(s.payment_mode, 'CASH') AS payment_mode,
                COALESCE(s.paid_amount, 0.0) AS paid_amount,
                COALESCE(s.remarks, '') AS remarks,
                COALESCE(vlink.voucher_no, '') AS linked_voucher_no
            FROM sales s
            LEFT JOIN customers c ON s.customer_id = c.customer_id
            LEFT JOIN (
                SELECT reference_id AS sale_id, MAX(voucher_no) AS voucher_no
                FROM vouchers
                WHERE UPPER(COALESCE(reference_type, '')) = 'SALE'
                  AND reference_id IS NOT NULL
                GROUP BY reference_id
            ) vlink ON s.sale_id = vlink.sale_id
            WHERE COALESCE(s.paid_amount, 0.0) > 0.005
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
            ORDER BY tx_date ASC, s.sale_id ASC
            LIMIT ?;
        """
        sql_purchase_payments = """
            SELECT
                p.purchase_id,
                COALESCE(p.invoice_no, '') AS invoice_no,
                substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) AS tx_date,
                p.supplier_id,
                COALESCE(s.supplier_name, 'Supplier #' || p.supplier_id) AS supplier_name,
                COALESCE(p.challan_no, '') AS challan_no,
                COALESCE(p.payment_type, 'CREDIT') AS payment_type,
                COALESCE(p.paid_amount, 0.0) AS paid_amount,
                COALESCE(p.remarks, '') AS remarks,
                COALESCE(vlink.voucher_no, '') AS linked_voucher_no
            FROM purchases p
            LEFT JOIN suppliers s ON p.supplier_id = s.supplier_id
            LEFT JOIN (
                SELECT reference_id AS purchase_id, MAX(voucher_no) AS voucher_no
                FROM vouchers
                WHERE UPPER(COALESCE(reference_type, '')) = 'PURCHASE'
                  AND UPPER(COALESCE(voucher_type, '')) = 'JOURNAL'
                  AND reference_id IS NOT NULL
                GROUP BY reference_id
            ) vlink ON p.purchase_id = vlink.purchase_id
            WHERE COALESCE(p.paid_amount, 0.0) > 0.005
              AND (? IS NULL OR substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) <= ?)
            ORDER BY tx_date ASC, p.purchase_id ASC
            LIMIT ?;
        """
        sql_standalone_vouchers = """
            SELECT
                v.voucher_id,
                COALESCE(v.voucher_no, '') AS voucher_no,
                substr(TRIM(COALESCE(v.voucher_date, '')), 1, 10) AS voucher_date,
                COALESCE(v.voucher_type, '') AS voucher_type,
                COALESCE(v.total_amount, 0.0) AS total_amount,
                COALESCE(v.narration, '') AS narration,
                COALESCE(v.reference_type, '') AS reference_type,
                v.reference_id,
                COALESCE(la.account_name, '') AS account_name,
                COALESCE(le.particulars, '') AS particulars
            FROM vouchers v
            LEFT JOIN ledger_entries le ON v.voucher_id = le.voucher_id
            LEFT JOIN ledger_accounts la ON le.account_id = la.account_id
            WHERE UPPER(COALESCE(v.voucher_type, '')) IN ('RECEIPT', 'PAYMENT', 'CONTRA')
              AND UPPER(COALESCE(v.reference_type, '')) NOT IN ('SALE', 'EXPENSE')
              AND (? IS NULL OR substr(TRIM(COALESCE(v.voucher_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(v.voucher_date, '')), 1, 10) <= ?)
            ORDER BY voucher_date ASC, v.voucher_id ASC, le.entry_id ASC;
        """
        sql_expenses = """
            SELECT
                e.expense_id,
                substr(TRIM(COALESCE(e.expense_date, '')), 1, 10) AS tx_date,
                e.category_id,
                COALESCE(ec.category_name, 'General Expense') AS category_name,
                COALESCE(e.amount, 0.0) AS amount,
                COALESCE(la.account_name, 'Cash in Hand') AS payment_account_name,
                COALESCE(e.reference_no, '') AS reference_no,
                COALESCE(e.remarks, '') AS remarks,
                COALESCE(vlink.voucher_no, '') AS linked_voucher_no
            FROM expenses e
            LEFT JOIN expense_categories ec ON e.category_id = ec.category_id
            LEFT JOIN ledger_accounts la ON e.payment_account_id = la.account_id
            LEFT JOIN (
                SELECT reference_id AS expense_id, MAX(voucher_no) AS voucher_no
                FROM vouchers
                WHERE UPPER(COALESCE(reference_type, '')) = 'EXPENSE'
                  AND reference_id IS NOT NULL
                GROUP BY reference_id
            ) vlink ON e.expense_id = vlink.expense_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(e.expense_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(e.expense_date, '')), 1, 10) <= ?)
            ORDER BY tx_date ASC, e.expense_id ASC
            LIMIT ?;
        """
        sql_cust_names = "SELECT customer_id, customer_name FROM customers;"
        sql_supp_names = "SELECT supplier_id, supplier_name FROM suppliers;"

        queries = {
            "sale_payments": (
                sql_sale_payments,
                (norm_from, norm_from, norm_to, norm_to, MAX_EXPORT_ROWS + 1),
            ),
            "purchase_payments": (
                sql_purchase_payments,
                (norm_from, norm_from, norm_to, norm_to, MAX_EXPORT_ROWS + 1),
            ),
            "standalone_vouchers": (
                sql_standalone_vouchers,
                (norm_from, norm_from, norm_to, norm_to),
            ),
            "expenses": (
                sql_expenses,
                (norm_from, norm_from, norm_to, norm_to, MAX_EXPORT_ROWS + 1),
            ),
            "cust_names": (sql_cust_names, ()),
            "supp_names": (sql_supp_names, ()),
        }
        company_settings, data, extra_meta, _ = self._run_multi_snapshot_queries(
            queries,
            enforce_row_limit_keys=("sale_payments", "purchase_payments", "expenses"),
        )

        cust_map: Dict[int, Dict[str, Any]] = {
            int(r["customer_id"]): r for r in data["cust_names"]
        }
        supp_map: Dict[int, Dict[str, Any]] = {
            int(r["supplier_id"]): r for r in data["supp_names"]
        }

        def _matches_mode(raw_tok: str) -> bool:
            if valid_pay_mode is None:
                return True
            norm_tok = _normalize_mode_token(raw_tok)
            if valid_pay_mode == "BANK_NON_CASH":
                return norm_tok in ("BANK_NON_CASH", "BANK_TRANSFER", "CHEQUE", "CARD")
            return norm_tok == valid_pay_mode

        entries: List[Dict[str, Any]] = []

        for s in data["sale_payments"]:
            raw_mode = _normalize_mode_token(s.get("payment_mode"))
            if not _matches_mode(raw_mode):
                continue
            amt = _to_decimal(s.get("paid_amount"))
            inv_no = str(s.get("invoice_no") or "").strip() or f"SALE-{s['sale_id']}"
            linked_vch = str(s.get("linked_voucher_no") or "").strip()
            entries.append(
                {
                    "tx_date": str(s.get("tx_date") or ""),
                    "source_order": 1,
                    "doc_id": int(s["sale_id"]),
                    "doc_no": linked_vch or inv_no,
                    "tx_type": "Customer Receipt (Sale Bill)",
                    "flow_dir": "Inflow (Receipt)",
                    "party": s.get("customer_name") or f"Customer #{s['customer_id']}",
                    "ref_no": inv_no,
                    "payment_mode": _format_payment_mode_label(raw_mode),
                    "amount": amt,
                    "inflow": amt,
                    "outflow": Decimal("0.00"),
                    "description": str(s.get("remarks") or "").strip()
                    or f"Bill-time payment for {inv_no}",
                    "status": "Recorded (Voucher Linked)" if linked_vch else "Recorded",
                }
            )

        for p in data["purchase_payments"]:
            raw_mode = _normalize_mode_token(p.get("payment_type"))
            if not _matches_mode(raw_mode):
                continue
            amt = _to_decimal(p.get("paid_amount"))
            inv_no = str(p.get("invoice_no") or "").strip() or f"PUR-{p['purchase_id']}"
            challan = str(p.get("challan_no") or "").strip()
            linked_vch = str(p.get("linked_voucher_no") or "").strip()
            entries.append(
                {
                    "tx_date": str(p.get("tx_date") or ""),
                    "source_order": 2,
                    "doc_id": int(p["purchase_id"]),
                    "doc_no": linked_vch or inv_no,
                    "tx_type": "Supplier Payment (Purchase Bill)",
                    "flow_dir": "Outflow (Payment)",
                    "party": p.get("supplier_name") or f"Supplier #{p['supplier_id']}",
                    "ref_no": challan or inv_no,
                    "payment_mode": _format_payment_mode_label(raw_mode),
                    "amount": amt,
                    "inflow": Decimal("0.00"),
                    "outflow": amt,
                    "description": str(p.get("remarks") or "").strip()
                    or f"Bill-time payment for {inv_no}",
                    "status": "Recorded (Voucher Linked)" if linked_vch else "Recorded",
                }
            )

        for v in _group_voucher_rows(data["standalone_vouchers"]):
            raw_mode, extracted_ref = _extract_voucher_mode_and_ref(v)
            if not _matches_mode(raw_mode):
                continue
            vtype = v["voucher_type"]
            amt = _to_decimal(v.get("total_amount"))
            vch_no = str(v.get("voucher_no") or "").strip() or f"VCH-{v['voucher_id']}"
            narration = str(v.get("narration") or "").strip()

            if vtype == "RECEIPT":
                cid = _resolve_voucher_customer_id(v, cust_map)
                if cid is not None:
                    party = cust_map.get(cid, {}).get("customer_name") or f"Customer #{cid}"
                    tx_type = "Customer Receipt Voucher"
                else:
                    party = "General / Voucher Receipt"
                    tx_type = "Receipt Voucher"
                flow_dir = "Inflow (Receipt)"
                inflow = amt
                outflow = Decimal("0.00")
            elif vtype == "PAYMENT":
                sid = _resolve_voucher_supplier_id(v, supp_map)
                if sid is not None:
                    party = supp_map.get(sid, {}).get("supplier_name") or f"Supplier #{sid}"
                    tx_type = "Supplier Payment Voucher"
                else:
                    party = "General / Voucher Payment"
                    tx_type = "Payment Voucher"
                flow_dir = "Outflow (Payment)"
                inflow = Decimal("0.00")
                outflow = amt
            else:
                party = "Internal Cash / Bank Transfer"
                tx_type = "Contra Entry"
                flow_dir = "Transfer (Contra)"
                inflow = Decimal("0.00")
                outflow = Decimal("0.00")

            entries.append(
                {
                    "tx_date": str(v.get("voucher_date") or ""),
                    "source_order": 3,
                    "doc_id": int(v["voucher_id"]),
                    "doc_no": vch_no,
                    "tx_type": tx_type,
                    "flow_dir": flow_dir,
                    "party": party,
                    "ref_no": extracted_ref or "",
                    "payment_mode": _format_payment_mode_label(raw_mode),
                    "amount": amt,
                    "inflow": inflow,
                    "outflow": outflow,
                    "description": narration or tx_type,
                    "status": "Recorded",
                }
            )

        for e in data["expenses"]:
            raw_mode, mode_label = _derive_expense_payment_mode(e.get("payment_account_name"))
            if not _matches_mode(raw_mode):
                continue
            amt = _to_decimal(e.get("amount"))
            linked_vch = str(e.get("linked_voucher_no") or "").strip()
            exp_doc = linked_vch or f"EXP-{int(e['expense_id']):05d}"
            ref_no = str(e.get("reference_no") or "").strip()
            cat_name = str(e.get("category_name") or "General Expense").strip()
            entries.append(
                {
                    "tx_date": str(e.get("tx_date") or ""),
                    "source_order": 4,
                    "doc_id": int(e["expense_id"]),
                    "doc_no": exp_doc,
                    "tx_type": "Shop Expense Payment",
                    "flow_dir": "Outflow (Payment)",
                    "party": cat_name,
                    "ref_no": ref_no,
                    "payment_mode": mode_label,
                    "amount": amt,
                    "inflow": Decimal("0.00"),
                    "outflow": amt,
                    "description": str(e.get("remarks") or "").strip() or cat_name,
                    "status": "Recorded (Voucher Linked)" if linked_vch else "Recorded",
                }
            )

        if len(entries) > MAX_EXPORT_ROWS:
            raise ExcelValidationError(
                f"Export exceeds maximum limit of {MAX_EXPORT_ROWS:,} rows. "
                "Please narrow your date range or payment mode filter."
            )

        entries.sort(key=lambda r: (r["tx_date"], r["source_order"], r["doc_id"]))

        banner_from, banner_to, filename = _build_period_banner_and_filename(
            "Payment_History", norm_from, norm_to, extra_meta
        )
        if valid_pay_mode:
            extra_meta["Payment Mode"] = _format_payment_mode_label(valid_pay_mode)

        total_inflow = sum((r["inflow"] for r in entries), Decimal("0.00"))
        total_outflow = sum((r["outflow"] for r in entries), Decimal("0.00"))
        extra_meta["Entries"] = len(entries)
        extra_meta["Total Receipts (Inflow)"] = f"₹{total_inflow:,.2f}"
        extra_meta["Total Payments (Outflow)"] = f"₹{total_outflow:,.2f}"
        extra_meta["Net Cash/Bank Flow"] = f"₹{(total_inflow - total_outflow):,.2f}"

        headers = [
            "Date",
            "Voucher / Doc No",
            "Transaction Type",
            "Flow Direction",
            "Party / Category",
            "Reference No",
            "Payment Mode",
            "Amount (₹)",
            "Receipt Inflow (₹)",
            "Payment Outflow (₹)",
            "Description / Narration",
            "Recorded Status",
        ]
        num_cols = len(headers)

        wb = create_workbook("Payment History")
        ws = add_sheet(wb, "Payment History")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Consolidated Payment & Receipt History",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws,
            PAYMENT_HISTORY_NOTE,
            row_idx=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        cur_row = data_start_row
        for item in entries:
            write_date_cell(ws, cur_row, 1, item["tx_date"])
            write_text_cell(ws, cur_row, 2, item["doc_no"], alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 3, item["tx_type"], alignment=_ALIGN_CENTER)
            write_text_cell(
                ws,
                cur_row,
                4,
                item["flow_dir"],
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(item["flow_dir"]),
            )
            write_text_cell(ws, cur_row, 5, item["party"])
            write_text_cell(ws, cur_row, 6, item["ref_no"], alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 7, item["payment_mode"], alignment=_ALIGN_CENTER)
            write_currency_cell(ws, cur_row, 8, item["amount"])
            write_currency_cell(
                ws,
                cur_row,
                9,
                item["inflow"],
                font=_FONT_STATUS_OK if item["inflow"] > Decimal("0") else None,
            )
            write_currency_cell(
                ws,
                cur_row,
                10,
                item["outflow"],
                font=_FONT_STATUS_DANGER if item["outflow"] > Decimal("0") else None,
            )
            write_text_cell(ws, cur_row, 11, item["description"])
            write_text_cell(
                ws,
                cur_row,
                12,
                item["status"],
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(item["status"]),
            )
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )
            write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals={9: total_inflow, 10: total_outflow},
                num_cols=num_cols,
                label="TOTAL RECEIPTS (INFLOW) & PAYMENTS (OUTFLOW)",
                label_col=1,
                currency_cols=[9, 10],
            )
            ws.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=7)

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        return save_workbook_to_bytes(wb), filename

    # ------------------------------------------------------------------
    # 14. Expenses Export (Stage C3-B3A — ADMIN ONLY)
    # ------------------------------------------------------------------
    def export_expenses(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        category_id: Optional[int] = None,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the ADMIN-only Expenses workbook (.xlsx) sourced once from `expenses`
        joined with `expense_categories`, `ledger_accounts`, and optional linked `EXPENSE`
        voucher numbers so expenses are never duplicated across `vouchers`.
        """
        norm_from, norm_to = normalize_optional_date_filters(from_date, to_date)
        valid_cat_id = _validate_positive_id(category_id, "category_id")

        sql_expenses = """
            SELECT
                e.expense_id,
                substr(TRIM(COALESCE(e.expense_date, '')), 1, 10) AS expense_date,
                e.category_id,
                COALESCE(ec.category_name, 'Category #' || e.category_id) AS category_name,
                COALESCE(e.amount, 0.0) AS amount,
                COALESCE(la.account_name, 'Cash in Hand') AS payment_account_name,
                COALESCE(e.reference_no, '') AS reference_no,
                COALESCE(e.remarks, '') AS remarks,
                COALESCE(vlink.voucher_no, '') AS linked_voucher_no
            FROM expenses e
            LEFT JOIN expense_categories ec ON e.category_id = ec.category_id
            LEFT JOIN ledger_accounts la ON e.payment_account_id = la.account_id
            LEFT JOIN (
                SELECT reference_id AS expense_id, MAX(voucher_no) AS voucher_no
                FROM vouchers
                WHERE UPPER(COALESCE(reference_type, '')) = 'EXPENSE'
                  AND reference_id IS NOT NULL
                GROUP BY reference_id
            ) vlink ON e.expense_id = vlink.expense_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(e.expense_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(e.expense_date, '')), 1, 10) <= ?)
              AND (? IS NULL OR e.category_id = ?)
            ORDER BY expense_date ASC, e.expense_id ASC
            LIMIT ?;
        """

        queries = {
            "expenses": (
                sql_expenses,
                (
                    norm_from,
                    norm_from,
                    norm_to,
                    norm_to,
                    valid_cat_id,
                    valid_cat_id,
                    MAX_EXPORT_ROWS + 1,
                ),
            )
        }
        company_settings, data, extra_meta, _ = self._run_multi_snapshot_queries(
            queries,
            expense_category_id=valid_cat_id,
            enforce_row_limit_keys=("expenses",),
        )
        rows = data["expenses"]

        banner_from, banner_to, filename = _build_period_banner_and_filename(
            "Expenses", norm_from, norm_to, extra_meta
        )

        cash_total = Decimal("0.00")
        bank_total = Decimal("0.00")
        grand_total = Decimal("0.00")
        for r in rows:
            amt = _to_decimal(r.get("amount"))
            raw_mode, _ = _derive_expense_payment_mode(r.get("payment_account_name"))
            grand_total += amt
            if raw_mode == "CASH":
                cash_total += amt
            else:
                bank_total += amt

        extra_meta["Expenses"] = len(rows)
        extra_meta["Cash Expenses"] = f"₹{cash_total:,.2f}"
        extra_meta["Bank / Non-Cash Expenses"] = f"₹{bank_total:,.2f}"
        extra_meta["Total Amount"] = f"₹{grand_total:,.2f}"

        headers = [
            "Expense ID",
            "Expense Date",
            "Linked Voucher No",
            "Expense Category",
            "Description / Remarks",
            "Payment Account",
            "Payment Mode",
            "Reference No",
            "Amount (₹)",
            "Recorded Status",
        ]
        num_cols = len(headers)

        wb = create_workbook("Expenses")
        ws = add_sheet(wb, "Expenses")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Operational Expenses Register",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws,
            EXPENSES_REPORT_NOTE,
            row_idx=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        cur_row = data_start_row
        for exp in rows:
            amt = _to_decimal(exp.get("amount"))
            linked_vch = str(exp.get("linked_voucher_no") or "").strip()
            acc_name = str(exp.get("payment_account_name") or "Cash in Hand").strip()
            _, mode_label = _derive_expense_payment_mode(acc_name)
            status_label = "Recorded (Voucher Linked)" if linked_vch else "Recorded"

            _write_int_cell(ws, cur_row, 1, int(exp["expense_id"]))
            write_date_cell(ws, cur_row, 2, exp.get("expense_date") or "")
            write_text_cell(ws, cur_row, 3, linked_vch or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 4, exp.get("category_name") or "")
            write_text_cell(ws, cur_row, 5, exp.get("remarks") or "")
            write_text_cell(ws, cur_row, 6, acc_name, alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 7, mode_label, alignment=_ALIGN_CENTER)
            write_text_cell(
                ws, cur_row, 8, exp.get("reference_no") or "", alignment=_ALIGN_CENTER
            )
            write_currency_cell(ws, cur_row, 9, amt)
            write_text_cell(
                ws,
                cur_row,
                10,
                status_label,
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(status_label),
            )
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )
            write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals={9: grand_total},
                num_cols=num_cols,
                label="TOTAL SHOP EXPENSES",
                label_col=1,
                currency_cols=[9],
            )
            ws.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=8)

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        return save_workbook_to_bytes(wb), filename

    # ------------------------------------------------------------------
    # 15. Customer Master Export (Stage C3-B3B)
    # ------------------------------------------------------------------
    def export_customer_master(
        self,
        *,
        query: str = "",
        village: str = "",
        group_id: Optional[int] = None,
        include_inactive: bool = True,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate a sanitized read-only Customer Master Directory workbook (.xlsx).
        Strictly uses existing DB columns from `customers` and `customer_groups`, and
        intentionally excludes Aadhaar, opening/current ledger balances, and credit limits.
        """
        clean_query = _validate_search_query(query, field_name="query")
        clean_village = _validate_search_query(village, field_name="village")
        valid_group_id = _validate_positive_id(group_id, "group_id")
        like_query = _escape_like_pattern(clean_query) if clean_query else ""
        like_village = _escape_like_pattern(clean_village) if clean_village else ""
        include_flag = 1 if include_inactive else 0

        sql = """
            SELECT
                c.customer_id,
                c.customer_name,
                COALESCE(cg.group_name, '') AS group_name,
                COALESCE(c.mobile, '') AS mobile,
                COALESCE(c.village, '') AS village,
                COALESCE(c.taluka, '') AS taluka,
                COALESCE(c.district, '') AS district,
                COALESCE(c.state, '') AS state,
                COALESCE(c.gstin, '') AS gstin,
                COALESCE(c.is_active, 1) AS is_active,
                substr(TRIM(COALESCE(c.created_at, '')), 1, 10) AS created_date
            FROM customers c
            LEFT JOIN customer_groups cg ON c.group_id = cg.group_id
            WHERE (? = 1 OR COALESCE(c.is_active, 1) = 1)
              AND (? IS NULL OR c.group_id = ?)
              AND (? = '' OR COALESCE(c.village, '') LIKE ? ESCAPE '\\')
              AND (
                  ? = ''
                  OR COALESCE(c.customer_name, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(c.mobile, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(c.village, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(c.taluka, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(c.district, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(c.gstin, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(cg.group_name, '') LIKE ? ESCAPE '\\'
              )
            ORDER BY COALESCE(c.is_active, 1) DESC, c.customer_name ASC, c.customer_id ASC
            LIMIT ?;
        """
        params = (
            include_flag,
            valid_group_id,
            valid_group_id,
            clean_village,
            like_village,
            clean_query,
            like_query,
            like_query,
            like_query,
            like_query,
            like_query,
            like_query,
            like_query,
            MAX_EXPORT_ROWS + 1,
        )
        company_settings, rows, extra_meta = self._read_snapshot(
            sql, params, customer_group_id=valid_group_id
        )

        active_count = sum(1 for r in rows if int(r.get("is_active") or 0) == 1)
        inactive_count = len(rows) - active_count
        if clean_query:
            extra_meta["Search"] = clean_query
        if clean_village:
            extra_meta["Village"] = clean_village
        extra_meta["Scope"] = "Active & Inactive" if include_inactive else "Active Only"
        extra_meta["Customers"] = len(rows)
        extra_meta["Active"] = active_count
        if include_inactive:
            extra_meta["Inactive"] = inactive_count

        headers = [
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
        num_cols = len(headers)

        wb = create_workbook("Customer Master")
        ws = add_sheet(wb, "Customer Master")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Customer Master Directory",
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws,
            CUSTOMER_MASTER_NOTE,
            row_idx=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1  # Spacer row
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        cur_row = data_start_row
        for cust in rows:
            cid = int(cust["customer_id"])
            status_label = "Active" if int(cust.get("is_active") or 0) == 1 else "Inactive"

            _write_int_cell(ws, cur_row, 1, cid)
            write_text_cell(ws, cur_row, 2, cust.get("customer_name") or f"Customer #{cid}")
            write_text_cell(ws, cur_row, 3, cust.get("group_name") or "")
            write_text_cell(ws, cur_row, 4, cust.get("mobile") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 5, cust.get("village") or "")
            write_text_cell(ws, cur_row, 6, cust.get("taluka") or "")
            write_text_cell(ws, cur_row, 7, cust.get("district") or "")
            write_text_cell(ws, cur_row, 8, cust.get("state") or "")
            write_text_cell(
                ws,
                cur_row,
                9,
                str(cust.get("gstin") or "").upper(),
                alignment=_ALIGN_CENTER,
            )
            write_text_cell(
                ws,
                cur_row,
                10,
                status_label,
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(status_label),
            )
            write_date_cell(ws, cur_row, 11, cust.get("created_date") or "")
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        today_compact = datetime.now().strftime("%Y%m%d")
        return save_workbook_to_bytes(wb), f"Customer_Master_{today_compact}.xlsx"

    # ------------------------------------------------------------------
    # 16. Supplier Master Export (Stage C3-B3B)
    # ------------------------------------------------------------------
    def export_supplier_master(
        self,
        *,
        query: str = "",
        include_inactive: bool = True,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate a sanitized read-only Supplier Master Directory workbook (.xlsx).
        Strictly uses existing DB columns from `suppliers` and intentionally excludes
        PAN, bank account/IFSC details, and opening/current ledger balances.
        """
        clean_query = _validate_search_query(query, field_name="query")
        like_query = _escape_like_pattern(clean_query) if clean_query else ""
        include_flag = 1 if include_inactive else 0

        sql = """
            SELECT
                s.supplier_id,
                s.supplier_name,
                COALESCE(s.contact_person, '') AS contact_person,
                COALESCE(s.mobile, '') AS mobile,
                COALESCE(s.email, '') AS email,
                COALESCE(s.address, '') AS address,
                COALESCE(s.city, '') AS city,
                COALESCE(s.state, '') AS state,
                COALESCE(s.gstin, '') AS gstin,
                COALESCE(s.dl_number, '') AS dl_number,
                COALESCE(s.is_active, 1) AS is_active,
                substr(TRIM(COALESCE(s.created_at, '')), 1, 10) AS created_date
            FROM suppliers s
            WHERE (? = 1 OR COALESCE(s.is_active, 1) = 1)
              AND (
                  ? = ''
                  OR COALESCE(s.supplier_name, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(s.contact_person, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(s.mobile, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(s.email, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(s.city, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(s.state, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(s.gstin, '') LIKE ? ESCAPE '\\'
                  OR COALESCE(s.dl_number, '') LIKE ? ESCAPE '\\'
              )
            ORDER BY COALESCE(s.is_active, 1) DESC, s.supplier_name ASC, s.supplier_id ASC
            LIMIT ?;
        """
        params = (
            include_flag,
            clean_query,
            like_query,
            like_query,
            like_query,
            like_query,
            like_query,
            like_query,
            like_query,
            like_query,
            MAX_EXPORT_ROWS + 1,
        )
        company_settings, rows, extra_meta = self._read_snapshot(sql, params)

        active_count = sum(1 for r in rows if int(r.get("is_active") or 0) == 1)
        inactive_count = len(rows) - active_count
        if clean_query:
            extra_meta["Search"] = clean_query
        extra_meta["Scope"] = "Active & Inactive" if include_inactive else "Active Only"
        extra_meta["Suppliers"] = len(rows)
        extra_meta["Active"] = active_count
        if include_inactive:
            extra_meta["Inactive"] = inactive_count

        headers = [
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
        num_cols = len(headers)

        wb = create_workbook("Supplier Master")
        ws = add_sheet(wb, "Supplier Master")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "Supplier Master Directory",
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws,
            SUPPLIER_MASTER_NOTE,
            row_idx=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1  # Spacer row
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        cur_row = data_start_row
        for supp in rows:
            sid = int(supp["supplier_id"])
            status_label = "Active" if int(supp.get("is_active") or 0) == 1 else "Inactive"

            _write_int_cell(ws, cur_row, 1, sid)
            write_text_cell(ws, cur_row, 2, supp.get("supplier_name") or f"Supplier #{sid}")
            write_text_cell(ws, cur_row, 3, supp.get("contact_person") or "")
            write_text_cell(ws, cur_row, 4, supp.get("mobile") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 5, supp.get("email") or "")
            write_text_cell(ws, cur_row, 6, supp.get("address") or "")
            write_text_cell(ws, cur_row, 7, supp.get("city") or "")
            write_text_cell(ws, cur_row, 8, supp.get("state") or "")
            write_text_cell(
                ws,
                cur_row,
                9,
                str(supp.get("gstin") or "").upper(),
                alignment=_ALIGN_CENTER,
            )
            write_text_cell(
                ws, cur_row, 10, supp.get("dl_number") or "", alignment=_ALIGN_CENTER
            )
            write_text_cell(
                ws,
                cur_row,
                11,
                status_label,
                alignment=_ALIGN_CENTER,
                font=_resolve_status_font(status_label),
            )
            write_date_cell(ws, cur_row, 12, supp.get("created_date") or "")
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        today_compact = datetime.now().strftime("%Y%m%d")
        return save_workbook_to_bytes(wb), f"Supplier_Master_{today_compact}.xlsx"

    # ------------------------------------------------------------------
    # Shared Helpers for Stage C3-B3C GST, Statutory & Financial Reports
    # ------------------------------------------------------------------
    @staticmethod
    def _quantize_2dp(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _calc_margin_pct(
        profit_dec: Decimal,
        revenue_dec: Decimal,
        *,
        decimals: int = 2,
    ) -> Decimal:
        if revenue_dec <= Decimal("0"):
            return Decimal("0.00") if decimals == 2 else Decimal("0.0")
        quant = Decimal("0.01") if decimals == 2 else Decimal("0.1")
        return (profit_dec / revenue_dec * Decimal("100")).quantize(quant, rounding=ROUND_HALF_UP)

    @staticmethod
    def _classify_supply_scope(cgst: Decimal, sgst: Decimal, igst: Decimal) -> str:
        if igst > Decimal("0.005"):
            return "Inter-State (IGST)"
        if (cgst + sgst) > Decimal("0.005"):
            return "Intra-State (CGST+SGST)"
        return "Exempt / Zero Tax"

    @staticmethod
    def _build_hsn_diagnostic_warning(row: Mapping[str, Any]) -> Tuple[str, Font]:
        flags: List[str] = []
        distinct_rates = int(row.get("distinct_rates_count") or 0)
        distinct_prods = int(row.get("distinct_products_count") or 0)
        missing_unit = int(row.get("missing_item_unit_count") or 0)
        missing_hsn = int(row.get("missing_hsn_count") or 0)
        uqc_val = row.get("uqc")

        if distinct_rates > 1:
            flags.append(f"Mixed GST rates collapsed ({row.get('merged_rates') or ''})")
        if distinct_prods > 1:
            flags.append(f"{distinct_prods} distinct products merged under 1 product_name")
        if missing_unit > 0 or uqc_val is None or str(uqc_val).strip() == "":
            flags.append("Missing/uncoalesced UQC")
        if missing_hsn > 0:
            flags.append("NULL or blank HSN code")

        if flags:
            return "LIMITATION: " + "; ".join(flags), _FONT_STATUS_DANGER
        return "Unverified ERP query (single product/rate in group)", _FONT_STATUS_WARN

    def _populate_unverified_hsn_sheet(
        self,
        wb,
        *,
        sheet_title: str,
        company_settings: Mapping[str, Any],
        hsn_rows: Sequence[Dict[str, Any]],
        banner_from: Optional[str],
        banner_to: Optional[str],
        generated_by: Optional[str],
        period_label: str,
    ) -> None:
        ws = add_sheet(wb, sheet_title, replace_empty_default=False)
        headers = [
            "HSN Code (ERP Raw)",
            "ERP Product Name (First/Arbitrary)",
            "UQC (ERP Raw)",
            "Total Qty",
            "Taxable Value (₹)",
            "CGST (₹)",
            "SGST (₹)",
            "IGST (₹)",
            "Total Amount (₹)",
            "Products Merged",
            "GST Rates Merged",
            "Grouping Limitation Warning",
        ]
        num_cols = len(headers)
        meta = {
            "Period": period_label,
            "HSN Groups": len(hsn_rows),
            "Verification Status": "UNVERIFIED — Requires Manual Accountant Review",
        }
        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "HSN Reference (Unverified ERP Output — Do Not File Without Verification)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws,
            HSN_LIMITATION_NOTE,
            row_idx=row_ptr,
            col_span=num_cols,
            is_warning=True,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        totals = {idx: Decimal("0") for idx in range(4, 10)}
        cur_row = data_start_row
        for r in hsn_rows:
            qty = _to_decimal(r.get("total_qty"))
            taxable = _to_decimal(r.get("total_taxable"))
            cgst = _to_decimal(r.get("total_cgst"))
            sgst = _to_decimal(r.get("total_sgst"))
            igst = _to_decimal(r.get("total_igst"))
            total_amt = _to_decimal(r.get("total_amount"))

            totals[4] += qty
            totals[5] += taxable
            totals[6] += cgst
            totals[7] += sgst
            totals[8] += igst
            totals[9] += total_amt

            warn_text, warn_font = self._build_hsn_diagnostic_warning(r)
            write_text_cell(ws, cur_row, 1, r.get("hsn_code") if r.get("hsn_code") is not None else "", alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 2, r.get("product_name") or "")
            write_text_cell(ws, cur_row, 3, r.get("uqc") or "", alignment=_ALIGN_CENTER)
            write_quantity_cell(ws, cur_row, 4, qty, decimals=3)
            write_currency_cell(ws, cur_row, 5, taxable)
            write_currency_cell(ws, cur_row, 6, cgst)
            write_currency_cell(ws, cur_row, 7, sgst)
            write_currency_cell(ws, cur_row, 8, igst)
            write_currency_cell(ws, cur_row, 9, total_amt)
            _write_int_cell(ws, cur_row, 10, int(r.get("distinct_products_count") or 0))
            write_text_cell(ws, cur_row, 11, r.get("merged_rates") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws, cur_row, 12, warn_text, font=warn_font)
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )
            write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals=totals,
                num_cols=num_cols,
                label="TOTAL (UNVERIFIED ERP HSN REFERENCE)",
                label_col=1,
                currency_cols=[5, 6, 7, 8, 9],
                quantity_cols=[4],
            )
            ws.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=3)

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=48, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

    def _populate_sales_returns_reference_sheet(
        self,
        wb,
        *,
        sheet_title: str,
        report_title: str,
        note_text: str,
        company_settings: Mapping[str, Any],
        return_rows: Sequence[Dict[str, Any]],
        banner_from: Optional[str],
        banner_to: Optional[str],
        generated_by: Optional[str],
        period_label: str,
        include_gst_classification: bool = False,
    ) -> None:
        ws = add_sheet(wb, sheet_title, replace_empty_default=False)
        headers = [
            "Return No",
            "Return Date",
            "Original Invoice No",
            "Original Invoice Date",
            "Customer ID",
            "Customer Name",
        ]
        if include_gst_classification:
            headers.extend(["Customer GSTIN", "GST Classification"])
        headers.extend(
            [
                "Return Taxable (₹)",
                "Return Total Tax (₹)",
                "Round Off (₹)",
                "Return Net Amount (₹)",
                "Remarks",
            ]
        )
        num_cols = len(headers)
        meta = {
            "Period": period_label,
            "Sales Returns": len(return_rows),
            "Accounting Handling": "Separate Reference Only (NOT Auto-Deducted)",
        }
        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            report_title,
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws,
            note_text,
            row_idx=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1
        header_row = row_ptr
        data_start_row = write_table_header(ws, headers, row_idx=header_row)

        taxable_col = 9 if include_gst_classification else 7
        tax_col = taxable_col + 1
        round_col = taxable_col + 2
        net_col = taxable_col + 3
        remarks_col = taxable_col + 4

        totals = {
            taxable_col: Decimal("0"),
            tax_col: Decimal("0"),
            round_col: Decimal("0"),
            net_col: Decimal("0"),
        }
        cur_row = data_start_row
        for sr in return_rows:
            r_taxable = _to_decimal(sr.get("total_taxable"))
            r_tax = _to_decimal(sr.get("total_tax"))
            r_round = _to_decimal(sr.get("round_off"))
            r_net = _to_decimal(sr.get("net_amount"))

            totals[taxable_col] += r_taxable
            totals[tax_col] += r_tax
            totals[round_col] += r_round
            totals[net_col] += r_net

            cust_gstin = str(sr.get("customer_gstin") or "").strip().upper()
            write_text_cell(ws, cur_row, 1, sr.get("return_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws, cur_row, 2, sr.get("return_date") or "")
            write_text_cell(ws, cur_row, 3, sr.get("original_invoice_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws, cur_row, 4, sr.get("original_invoice_date") or "")
            _write_int_cell(ws, cur_row, 5, int(sr["customer_id"]))
            write_text_cell(ws, cur_row, 6, sr.get("customer_name") or "")
            if include_gst_classification:
                cls_label = "B2B Credit Note (Registered)" if cust_gstin else "B2C Credit Note (Unregistered)"
                write_text_cell(ws, cur_row, 7, cust_gstin, alignment=_ALIGN_CENTER)
                write_text_cell(ws, cur_row, 8, cls_label, alignment=_ALIGN_CENTER)
            write_currency_cell(ws, cur_row, taxable_col, r_taxable)
            write_currency_cell(ws, cur_row, tax_col, r_tax)
            write_currency_cell(ws, cur_row, round_col, r_round)
            write_currency_cell(ws, cur_row, net_col, r_net)
            write_text_cell(ws, cur_row, remarks_col, sr.get("remarks") or "")
            cur_row += 1

        last_data_row = cur_row - 1
        if last_data_row >= data_start_row:
            apply_alternating_row_styles(
                ws,
                start_row=data_start_row,
                end_row=last_data_row,
                start_col=1,
                end_col=num_cols,
            )
            write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals=totals,
                num_cols=num_cols,
                label="TOTAL SEPARATELY RECORDED SALES RETURNS",
                label_col=1,
                currency_cols=[taxable_col, tax_col, round_col, net_col],
            )
            ws.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=6)

        apply_auto_filter(
            ws,
            header_row=header_row,
            last_row=max(header_row, last_data_row),
            start_col=1,
            end_col=num_cols,
        )
        apply_freeze_panes(ws, header_row=header_row, freeze_col=1)
        auto_fit_columns(ws, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

    # ------------------------------------------------------------------
    # 17. GST Summary Export (Stage C3-B3C — ADMIN ONLY)
    # ------------------------------------------------------------------
    def export_gst_summary(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate ADMIN-only GST Summary workbook (.xlsx):
        - Outward GST KPI totals & B2B/B2C split matching existing `sales` & `GSTService`
        - Verified Tax Rate-Wise Summary (`GSTService.get_gst_rate_wise_summary`)
        - Explicit disclosure & diagnostic sheet for unverified ERP HSN grouping
        - Separately reported sales returns reference (never silently netted)
        """
        norm_from, norm_to = normalize_optional_date_filters(from_date, to_date)
        date_params = (norm_from, norm_from, norm_to, norm_to)

        sql_sales_hdr = """
            SELECT
                s.sale_id,
                COALESCE(s.total_taxable, 0.0) AS total_taxable,
                COALESCE(s.total_cgst, 0.0) AS total_cgst,
                COALESCE(s.total_sgst, 0.0) AS total_sgst,
                COALESCE(s.total_igst, 0.0) AS total_igst,
                COALESCE(s.extra_charges, 0.0) AS extra_charges,
                COALESCE(s.round_off, 0.0) AS round_off,
                COALESCE(s.net_amount, 0.0) AS net_amount,
                CASE WHEN c.gstin IS NOT NULL AND c.gstin != '' THEN 1 ELSE 0 END AS is_b2b
            FROM sales s
            LEFT JOIN customers c ON s.customer_id = c.customer_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
            ORDER BY s.sale_id ASC
            LIMIT ?;
        """
        sql_rate_summary = """
            SELECT
                COALESCE(
                    tg.tax_group_name,
                    'GST ' || CAST(ROUND(COALESCE(si.cgst_rate, 0.0) + COALESCE(si.sgst_rate, 0.0) + COALESCE(si.igst_rate, 0.0), 2) AS TEXT) || '%'
                ) AS tax_group_name,
                COALESCE(si.cgst_rate, 0.0) + COALESCE(si.sgst_rate, 0.0) + COALESCE(si.igst_rate, 0.0) AS applied_rate,
                COUNT(DISTINCT s.sale_id) AS total_invoices,
                COALESCE(SUM(si.taxable_amount), 0.0) AS total_taxable,
                COALESCE(SUM(si.cgst_amount), 0.0) AS total_cgst,
                COALESCE(SUM(si.sgst_amount), 0.0) AS total_sgst,
                COALESCE(SUM(si.igst_amount), 0.0) AS total_igst,
                COALESCE(SUM(si.total_amount), 0.0) AS total_gross
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN products p ON si.product_id = p.product_id
            LEFT JOIN tax_groups tg ON p.tax_group_id = tg.tax_group_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
            GROUP BY tg.tax_group_id, applied_rate
            ORDER BY applied_rate ASC
            LIMIT ?;
        """
        sql_hsn_diag = """
            SELECT
                COALESCE(p.hsn_code, 'NA') AS hsn_code,
                p.product_name,
                u.symbol AS uqc,
                COALESCE(SUM(si.qty), 0.0) AS total_qty,
                COALESCE(SUM(si.taxable_amount), 0.0) AS total_taxable,
                COALESCE(SUM(si.cgst_amount), 0.0) AS total_cgst,
                COALESCE(SUM(si.sgst_amount), 0.0) AS total_sgst,
                COALESCE(SUM(si.igst_amount), 0.0) AS total_igst,
                COALESCE(SUM(si.total_amount), 0.0) AS total_amount,
                COUNT(DISTINCT si.product_id) AS distinct_products_count,
                COUNT(DISTINCT ROUND(COALESCE(si.cgst_rate, 0.0) + COALESCE(si.sgst_rate, 0.0) + COALESCE(si.igst_rate, 0.0), 2)) AS distinct_rates_count,
                GROUP_CONCAT(DISTINCT CAST(ROUND(COALESCE(si.cgst_rate, 0.0) + COALESCE(si.sgst_rate, 0.0) + COALESCE(si.igst_rate, 0.0), 2) AS TEXT) || '%') AS merged_rates,
                SUM(CASE WHEN si.unit_id IS NULL AND p.unit_id IS NOT NULL THEN 1 ELSE 0 END) AS missing_item_unit_count,
                SUM(CASE WHEN TRIM(COALESCE(p.hsn_code, '')) = '' THEN 1 ELSE 0 END) AS missing_hsn_count
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN products p ON si.product_id = p.product_id
            LEFT JOIN units u ON si.unit_id = u.unit_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
            GROUP BY p.hsn_code, u.symbol
            ORDER BY p.hsn_code ASC
            LIMIT ?;
        """
        sql_returns = """
            SELECT
                sr.return_id,
                COALESCE(sr.total_taxable, 0.0) AS total_taxable,
                COALESCE(sr.total_tax, 0.0) AS total_tax,
                COALESCE(sr.round_off, 0.0) AS round_off,
                COALESCE(sr.net_amount, 0.0) AS net_amount
            FROM sales_returns sr
            WHERE (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) <= ?)
            ORDER BY sr.return_id ASC
            LIMIT ?;
        """
        queries = {
            "sales": (sql_sales_hdr, date_params + (MAX_EXPORT_ROWS + 1,)),
            "rates": (sql_rate_summary, date_params + (MAX_EXPORT_ROWS + 1,)),
            "hsn": (sql_hsn_diag, date_params + (MAX_EXPORT_ROWS + 1,)),
            "returns": (sql_returns, date_params + (MAX_EXPORT_ROWS + 1,)),
        }
        company_settings, results, extra_meta, _ = self._run_multi_snapshot_queries(queries)

        sales_rows = results["sales"]
        rate_rows = results["rates"]
        hsn_rows = results["hsn"]
        return_rows = results["returns"]

        banner_from, banner_to, filename = _build_period_banner_and_filename(
            "GST_Summary", norm_from, norm_to, extra_meta
        )
        period_label = (
            f"{norm_from} to {norm_to}" if (norm_from and norm_to) else extra_meta.get("Period", "All Dates")
        )

        b2b_count = sum(1 for r in sales_rows if int(r.get("is_b2b") or 0) == 1)
        b2c_count = len(sales_rows) - b2b_count
        b2b_taxable = sum((_to_decimal(r["total_taxable"]) for r in sales_rows if r["is_b2b"]), Decimal("0"))
        b2b_cgst = sum((_to_decimal(r["total_cgst"]) for r in sales_rows if r["is_b2b"]), Decimal("0"))
        b2b_sgst = sum((_to_decimal(r["total_sgst"]) for r in sales_rows if r["is_b2b"]), Decimal("0"))
        b2b_igst = sum((_to_decimal(r["total_igst"]) for r in sales_rows if r["is_b2b"]), Decimal("0"))
        b2b_net = sum((_to_decimal(r["net_amount"]) for r in sales_rows if r["is_b2b"]), Decimal("0"))

        b2c_taxable = sum((_to_decimal(r["total_taxable"]) for r in sales_rows if not r["is_b2b"]), Decimal("0"))
        b2c_cgst = sum((_to_decimal(r["total_cgst"]) for r in sales_rows if not r["is_b2b"]), Decimal("0"))
        b2c_sgst = sum((_to_decimal(r["total_sgst"]) for r in sales_rows if not r["is_b2b"]), Decimal("0"))
        b2c_igst = sum((_to_decimal(r["total_igst"]) for r in sales_rows if not r["is_b2b"]), Decimal("0"))
        b2c_net = sum((_to_decimal(r["net_amount"]) for r in sales_rows if not r["is_b2b"]), Decimal("0"))

        total_taxable = b2b_taxable + b2c_taxable
        total_cgst = b2b_cgst + b2c_cgst
        total_sgst = b2b_sgst + b2c_sgst
        total_igst = b2b_igst + b2c_igst
        total_tax = total_cgst + total_sgst + total_igst
        total_extra = sum((_to_decimal(r["extra_charges"]) for r in sales_rows), Decimal("0"))
        total_round = sum((_to_decimal(r["round_off"]) for r in sales_rows), Decimal("0"))
        total_invoice_net = b2b_net + b2c_net

        ret_count = len(return_rows)
        ret_taxable = sum((_to_decimal(r["total_taxable"]) for r in return_rows), Decimal("0"))
        ret_tax = sum((_to_decimal(r["total_tax"]) for r in return_rows), Decimal("0"))
        ret_net = sum((_to_decimal(r["net_amount"]) for r in return_rows), Decimal("0"))

        extra_meta["Invoices"] = len(sales_rows)
        extra_meta["B2B / B2C"] = f"{b2b_count} / {b2c_count}"
        extra_meta["Returns (Separate)"] = ret_count

        num_cols = 9
        wb = create_workbook("GST Summary")
        ws = add_sheet(wb, "GST Summary")

        row_ptr = add_company_header(
            ws, company_settings, start_row=1, col_span=num_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws,
            "GST Summary (Outward Supplies & Tax Rate Breakdown)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=num_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws,
            GST_SUMMARY_NOTE,
            row_idx=row_ptr,
            col_span=num_cols,
        )
        row_ptr += 1

        # Section 1: Outward Supplies Executive Summary (matches sales invoice headers)
        row_ptr = _add_section_subbanner(
            ws,
            "1. Outward Supplies Summary (Gross Sales Invoices — Excl. Sales Returns)",
            row_idx=row_ptr,
            col_span=num_cols,
        )
        kpi_headers = [
            "Summary Component",
            "Invoices / Count",
            "Taxable Value (₹)",
            "CGST (₹)",
            "SGST (₹)",
            "IGST (₹)",
            "Total Tax (₹)",
            "Invoice Net Value (₹)",
            "Accounting & Scope Notes",
        ]
        row_ptr = write_table_header(ws, kpi_headers, row_idx=row_ptr)
        kpi_start = row_ptr
        summary_rows_data = [
            (
                "Registered Buyers (B2B — Valid GSTIN)",
                b2b_count,
                b2b_taxable,
                b2b_cgst,
                b2b_sgst,
                b2b_igst,
                b2b_cgst + b2b_sgst + b2b_igst,
                b2b_net,
                "Matches GSTService.get_gstr1_b2b",
            ),
            (
                "Unregistered Buyers (B2C — Consumers / Farmers)",
                b2c_count,
                b2c_taxable,
                b2c_cgst,
                b2c_sgst,
                b2c_igst,
                b2c_cgst + b2c_sgst + b2c_igst,
                b2c_net,
                "All invoices where customer GSTIN is blank/NULL",
            ),
            (
                "Total Outward Supplies (Gross Sales Headers)",
                len(sales_rows),
                total_taxable,
                total_cgst,
                total_sgst,
                total_igst,
                total_tax,
                total_invoice_net,
                f"Includes Extra Charges ₹{self._quantize_2dp(total_extra):.2f} & Round-Off ₹{self._quantize_2dp(total_round):.2f}",
            ),
            (
                "Separately Recorded Sales Returns (Reference Only)",
                ret_count,
                ret_taxable,
                Decimal("0.00"),
                Decimal("0.00"),
                Decimal("0.00"),
                ret_tax,
                ret_net,
                "NOT deducted from Outward GST above; sales_returns stores aggregate total_tax only",
            ),
        ]
        for label, cnt, txbl, cg, sg, ig, tx, net_v, note in summary_rows_data:
            is_ret_row = label.startswith("Separately Recorded")
            is_tot_row = label.startswith("Total Outward")
            fnt = TOTALS_FONT if is_tot_row else (_FONT_STATUS_WARN if is_ret_row else BODY_FONT)
            write_text_cell(ws, row_ptr, 1, label, font=fnt)
            _write_int_cell(ws, row_ptr, 2, cnt, font=fnt)
            write_currency_cell(ws, row_ptr, 3, txbl, font=fnt)
            write_currency_cell(ws, row_ptr, 4, cg, font=fnt)
            write_currency_cell(ws, row_ptr, 5, sg, font=fnt)
            write_currency_cell(ws, row_ptr, 6, ig, font=fnt)
            write_currency_cell(ws, row_ptr, 7, tx, font=fnt)
            write_currency_cell(ws, row_ptr, 8, net_v, font=fnt)
            write_text_cell(ws, row_ptr, 9, note, font=_FONT_STATUS_MUTED if not is_ret_row else _FONT_STATUS_WARN)
            row_ptr += 1

        apply_alternating_row_styles(
            ws, start_row=kpi_start, end_row=row_ptr - 1, start_col=1, end_col=num_cols
        )
        # Highlight Total Outward Supplies row
        for col_i in range(1, num_cols + 1):
            ws.cell(row=kpi_start + 2, column=col_i).fill = TOTALS_FILL
            ws.cell(row=kpi_start + 2, column=col_i).border = TOTALS_BORDER

        row_ptr += 1

        # Section 2: Tax Rate-Wise Breakdown (matches GSTService.get_gst_rate_wise_summary)
        row_ptr = _add_section_subbanner(
            ws,
            "2. Tax Rate-Wise Outward Supply Breakdown (Matches ERP GSTService.get_gst_rate_wise_summary)",
            row_idx=row_ptr,
            col_span=num_cols,
        )
        rate_headers = [
            "Tax Group",
            "Applied GST Rate (%)",
            "Distinct Invoices",
            "Taxable Value (₹)",
            "CGST (₹)",
            "SGST (₹)",
            "IGST (₹)",
            "Total GST (₹)",
            "Total Gross (₹)",
        ]
        rate_hdr_row = row_ptr
        rate_data_start = write_table_header(ws, rate_headers, row_idx=rate_hdr_row)
        cur_row = rate_data_start

        rate_totals = {4: Decimal("0"), 5: Decimal("0"), 6: Decimal("0"), 7: Decimal("0"), 8: Decimal("0"), 9: Decimal("0")}
        for rr in rate_rows:
            txbl = _to_decimal(rr.get("total_taxable"))
            cg = _to_decimal(rr.get("total_cgst"))
            sg = _to_decimal(rr.get("total_sgst"))
            ig = _to_decimal(rr.get("total_igst"))
            tot_tax = cg + sg + ig
            gross = _to_decimal(rr.get("total_gross"))

            rate_totals[4] += txbl
            rate_totals[5] += cg
            rate_totals[6] += sg
            rate_totals[7] += ig
            rate_totals[8] += tot_tax
            rate_totals[9] += gross

            write_text_cell(ws, cur_row, 1, rr.get("tax_group_name") or "")
            write_percentage_cell(ws, cur_row, 2, _to_decimal(rr.get("applied_rate")), decimals=2)
            _write_int_cell(ws, cur_row, 3, int(rr.get("total_invoices") or 0))
            write_currency_cell(ws, cur_row, 4, txbl)
            write_currency_cell(ws, cur_row, 5, cg)
            write_currency_cell(ws, cur_row, 6, sg)
            write_currency_cell(ws, cur_row, 7, ig)
            write_currency_cell(ws, cur_row, 8, tot_tax)
            write_currency_cell(ws, cur_row, 9, gross)
            cur_row += 1

        last_rate_row = cur_row - 1
        if last_rate_row >= rate_data_start:
            apply_alternating_row_styles(
                ws, start_row=rate_data_start, end_row=last_rate_row, start_col=1, end_col=num_cols
            )
            cur_row = write_totals_row(
                ws,
                row_idx=cur_row,
                col_totals=rate_totals,
                num_cols=num_cols,
                label="TOTAL TAX SLABS (LINE ITEMS)",
                label_col=1,
                currency_cols=[4, 5, 6, 7, 8, 9],
            )
            ws.merge_cells(start_row=cur_row - 1, start_column=1, end_row=cur_row - 1, end_column=3)
        else:
            write_text_cell(ws, cur_row, 1, "No taxable outward sale items in selected period.", font=_FONT_STATUS_MUTED)
            cur_row += 1

        cur_row += 1
        hsn_warning_count = sum(
            1
            for hr in hsn_rows
            if int(hr.get("distinct_rates_count") or 0) > 1
            or int(hr.get("distinct_products_count") or 0) > 1
            or int(hr.get("missing_item_unit_count") or 0) > 0
            or int(hr.get("missing_hsn_count") or 0) > 0
            or hr.get("uqc") is None
        )
        cur_row = _add_section_subbanner(
            ws,
            "3. HSN Grouping Verification & Limitation Status",
            row_idx=cur_row,
            col_span=num_cols,
        )
        _add_explanatory_note_banner(
            ws,
            (
                f"Unverified ERP HSN Query: {len(hsn_rows)} HSN group(s) found, including "
                f"{hsn_warning_count} group(s) with detected product/rate/UQC ambiguities. "
                "See 'HSN Reference (Unverified)' sheet for raw ERP output and diagnostics."
            ),
            row_idx=cur_row,
            col_span=num_cols,
            is_warning=True,
        )

        apply_freeze_panes(ws, header_row=kpi_start - 1, freeze_col=1)
        auto_fit_columns(ws, min_width=12, max_width=48, padding=3, exclude_merged=True)
        _configure_landscape_print(ws)

        # Sheet 2: Unverified ERP HSN Reference with empirical diagnostic warnings
        self._populate_unverified_hsn_sheet(
            wb,
            sheet_title="HSN Reference (Unverified)",
            company_settings=company_settings,
            hsn_rows=hsn_rows,
            banner_from=banner_from,
            banner_to=banner_to,
            generated_by=generated_by,
            period_label=period_label,
        )

        return save_workbook_to_bytes(wb), filename

    # ------------------------------------------------------------------
    # 18. GSTR-1 Accountant Working Paper (Stage C3-B3C — ADMIN ONLY)
    # ------------------------------------------------------------------
    def export_gstr1_working_paper(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the ADMIN-only GSTR-1 Accountant Working Paper workbook (.xlsx):
        - Strictly labelled as an internal accountant working paper (never portal-ready or certified)
        - Reconciles Summary, B2B Invoices, B2C Small Summary, Credit Notes - Sales Returns,
          Tax Rate Summary, and an explicitly unverified HSN Reference diagnostic sheet
        """
        norm_from, norm_to = normalize_optional_date_filters(from_date, to_date)
        date_params = (norm_from, norm_from, norm_to, norm_to)

        sql_b2b = """
            SELECT
                s.sale_id,
                c.gstin AS receiver_gstin,
                c.customer_name AS receiver_name,
                s.invoice_no,
                substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) AS invoice_date,
                COALESCE(c.state, '') AS place_of_supply,
                COALESCE(s.total_taxable, 0.0) AS total_taxable,
                COALESCE(s.total_cgst, 0.0) AS total_cgst,
                COALESCE(s.total_sgst, 0.0) AS total_sgst,
                COALESCE(s.total_igst, 0.0) AS total_igst,
                COALESCE(s.extra_charges, 0.0) AS extra_charges,
                COALESCE(s.round_off, 0.0) AS round_off,
                COALESCE(s.net_amount, 0.0) AS invoice_value
            FROM sales s
            JOIN customers c ON s.customer_id = c.customer_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
              AND c.gstin IS NOT NULL
              AND c.gstin != ''
            ORDER BY substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) ASC, s.invoice_no ASC, s.sale_id ASC
            LIMIT ?;
        """
        sql_b2c_rates = """
            SELECT
                COALESCE(si.cgst_rate, 0.0) + COALESCE(si.sgst_rate, 0.0) + COALESCE(si.igst_rate, 0.0) AS gst_rate,
                COUNT(DISTINCT s.sale_id) AS distinct_invoices,
                COALESCE(SUM(si.taxable_amount), 0.0) AS total_taxable,
                COALESCE(SUM(si.cgst_amount), 0.0) AS total_cgst,
                COALESCE(SUM(si.sgst_amount), 0.0) AS total_sgst,
                COALESCE(SUM(si.igst_amount), 0.0) AS total_igst,
                COALESCE(SUM(si.total_amount), 0.0) AS total_invoice_value
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN customers c ON s.customer_id = c.customer_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
              AND (c.gstin IS NULL OR c.gstin = '')
            GROUP BY gst_rate
            ORDER BY gst_rate ASC
            LIMIT ?;
        """
        sql_b2c_hdr_agg = """
            SELECT
                COUNT(s.sale_id) AS b2c_invoice_count,
                COALESCE(SUM(s.total_taxable), 0.0) AS b2c_hdr_taxable,
                COALESCE(SUM(s.total_cgst), 0.0) AS b2c_hdr_cgst,
                COALESCE(SUM(s.total_sgst), 0.0) AS b2c_hdr_sgst,
                COALESCE(SUM(s.total_igst), 0.0) AS b2c_hdr_igst,
                COALESCE(SUM(s.extra_charges), 0.0) AS b2c_hdr_extra,
                COALESCE(SUM(s.round_off), 0.0) AS b2c_hdr_round_off,
                COALESCE(SUM(s.net_amount), 0.0) AS b2c_hdr_net
            FROM sales s
            LEFT JOIN customers c ON s.customer_id = c.customer_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
              AND (c.gstin IS NULL OR c.gstin = '');
        """
        sql_rate_summary = """
            SELECT
                COALESCE(
                    tg.tax_group_name,
                    'GST ' || CAST(ROUND(COALESCE(si.cgst_rate, 0.0) + COALESCE(si.sgst_rate, 0.0) + COALESCE(si.igst_rate, 0.0), 2) AS TEXT) || '%'
                ) AS tax_group_name,
                COALESCE(si.cgst_rate, 0.0) + COALESCE(si.sgst_rate, 0.0) + COALESCE(si.igst_rate, 0.0) AS applied_rate,
                COUNT(DISTINCT s.sale_id) AS total_invoices,
                COALESCE(SUM(si.taxable_amount), 0.0) AS total_taxable,
                COALESCE(SUM(si.cgst_amount), 0.0) AS total_cgst,
                COALESCE(SUM(si.sgst_amount), 0.0) AS total_sgst,
                COALESCE(SUM(si.igst_amount), 0.0) AS total_igst,
                COALESCE(SUM(si.total_amount), 0.0) AS total_gross
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN products p ON si.product_id = p.product_id
            LEFT JOIN tax_groups tg ON p.tax_group_id = tg.tax_group_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
            GROUP BY tg.tax_group_id, applied_rate
            ORDER BY applied_rate ASC
            LIMIT ?;
        """
        sql_returns = """
            SELECT
                sr.return_id,
                sr.return_no,
                substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) AS return_date,
                sr.sale_id,
                COALESCE(s.invoice_no, '') AS original_invoice_no,
                substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) AS original_invoice_date,
                sr.customer_id,
                COALESCE(c.customer_name, 'Customer #' || sr.customer_id) AS customer_name,
                COALESCE(c.gstin, '') AS customer_gstin,
                COALESCE(sr.total_taxable, 0.0) AS total_taxable,
                COALESCE(sr.total_tax, 0.0) AS total_tax,
                COALESCE(sr.round_off, 0.0) AS round_off,
                COALESCE(sr.net_amount, 0.0) AS net_amount,
                COALESCE(sr.remarks, '') AS remarks
            FROM sales_returns sr
            LEFT JOIN sales s ON sr.sale_id = s.sale_id
            LEFT JOIN customers c ON sr.customer_id = c.customer_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) <= ?)
            ORDER BY substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) ASC, sr.return_id ASC
            LIMIT ?;
        """
        sql_hsn_diag = """
            SELECT
                COALESCE(p.hsn_code, 'NA') AS hsn_code,
                p.product_name,
                u.symbol AS uqc,
                COALESCE(SUM(si.qty), 0.0) AS total_qty,
                COALESCE(SUM(si.taxable_amount), 0.0) AS total_taxable,
                COALESCE(SUM(si.cgst_amount), 0.0) AS total_cgst,
                COALESCE(SUM(si.sgst_amount), 0.0) AS total_sgst,
                COALESCE(SUM(si.igst_amount), 0.0) AS total_igst,
                COALESCE(SUM(si.total_amount), 0.0) AS total_amount,
                COUNT(DISTINCT si.product_id) AS distinct_products_count,
                COUNT(DISTINCT ROUND(COALESCE(si.cgst_rate, 0.0) + COALESCE(si.sgst_rate, 0.0) + COALESCE(si.igst_rate, 0.0), 2)) AS distinct_rates_count,
                GROUP_CONCAT(DISTINCT CAST(ROUND(COALESCE(si.cgst_rate, 0.0) + COALESCE(si.sgst_rate, 0.0) + COALESCE(si.igst_rate, 0.0), 2) AS TEXT) || '%') AS merged_rates,
                SUM(CASE WHEN si.unit_id IS NULL AND p.unit_id IS NOT NULL THEN 1 ELSE 0 END) AS missing_item_unit_count,
                SUM(CASE WHEN TRIM(COALESCE(p.hsn_code, '')) = '' THEN 1 ELSE 0 END) AS missing_hsn_count
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN products p ON si.product_id = p.product_id
            LEFT JOIN units u ON si.unit_id = u.unit_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
            GROUP BY p.hsn_code, u.symbol
            ORDER BY p.hsn_code ASC
            LIMIT ?;
        """
        queries = {
            "b2b": (sql_b2b, date_params + (MAX_EXPORT_ROWS + 1,)),
            "b2c": (sql_b2c_rates, date_params + (MAX_EXPORT_ROWS + 1,)),
            "b2c_hdr": (sql_b2c_hdr_agg, date_params),
            "rates": (sql_rate_summary, date_params + (MAX_EXPORT_ROWS + 1,)),
            "returns": (sql_returns, date_params + (MAX_EXPORT_ROWS + 1,)),
            "hsn": (sql_hsn_diag, date_params + (MAX_EXPORT_ROWS + 1,)),
        }
        company_settings, results, extra_meta, _ = self._run_multi_snapshot_queries(queries)

        b2b_rows = results["b2b"]
        b2c_rows = results["b2c"]
        b2c_hdr = results["b2c_hdr"][0] if results["b2c_hdr"] else {}
        rate_rows = results["rates"]
        return_rows = results["returns"]
        hsn_rows = results["hsn"]

        banner_from, banner_to, filename = _build_period_banner_and_filename(
            "GSTR1_Accountant_Working_Paper", norm_from, norm_to, extra_meta
        )
        period_label = (
            f"{norm_from} to {norm_to}" if (norm_from and norm_to) else extra_meta.get("Period", "All Dates")
        )

        # Totals per section (exact Decimal reconciliation)
        b2b_taxable = sum((_to_decimal(r["total_taxable"]) for r in b2b_rows), Decimal("0"))
        b2b_cgst = sum((_to_decimal(r["total_cgst"]) for r in b2b_rows), Decimal("0"))
        b2b_sgst = sum((_to_decimal(r["total_sgst"]) for r in b2b_rows), Decimal("0"))
        b2b_igst = sum((_to_decimal(r["total_igst"]) for r in b2b_rows), Decimal("0"))
        b2b_tax = b2b_cgst + b2b_sgst + b2b_igst
        b2b_val = sum((_to_decimal(r["invoice_value"]) for r in b2b_rows), Decimal("0"))

        b2c_taxable = sum((_to_decimal(r["total_taxable"]) for r in b2c_rows), Decimal("0"))
        b2c_cgst = sum((_to_decimal(r["total_cgst"]) for r in b2c_rows), Decimal("0"))
        b2c_sgst = sum((_to_decimal(r["total_sgst"]) for r in b2c_rows), Decimal("0"))
        b2c_igst = sum((_to_decimal(r["total_igst"]) for r in b2c_rows), Decimal("0"))
        b2c_tax = b2c_cgst + b2c_sgst + b2c_igst
        b2c_val = sum((_to_decimal(r["total_invoice_value"]) for r in b2c_rows), Decimal("0"))

        b2c_hdr_cnt = int(b2c_hdr.get("b2c_invoice_count") or 0)
        b2c_hdr_extra = _to_decimal(b2c_hdr.get("b2c_hdr_extra"))
        b2c_hdr_round = _to_decimal(b2c_hdr.get("b2c_hdr_round_off"))
        b2c_hdr_net = _to_decimal(b2c_hdr.get("b2c_hdr_net"))

        ret_taxable = sum((_to_decimal(r["total_taxable"]) for r in return_rows), Decimal("0"))
        ret_tax = sum((_to_decimal(r["total_tax"]) for r in return_rows), Decimal("0"))
        ret_net = sum((_to_decimal(r["net_amount"]) for r in return_rows), Decimal("0"))

        rate_taxable = sum((_to_decimal(r["total_taxable"]) for r in rate_rows), Decimal("0"))
        rate_cgst = sum((_to_decimal(r["total_cgst"]) for r in rate_rows), Decimal("0"))
        rate_sgst = sum((_to_decimal(r["total_sgst"]) for r in rate_rows), Decimal("0"))
        rate_igst = sum((_to_decimal(r["total_igst"]) for r in rate_rows), Decimal("0"))
        rate_gross = sum((_to_decimal(r["total_gross"]) for r in rate_rows), Decimal("0"))

        extra_meta["B2B Invoices"] = len(b2b_rows)
        extra_meta["B2C Invoices"] = b2c_hdr_cnt
        extra_meta["Credit Notes (Returns)"] = len(return_rows)

        wb = create_workbook("Summary")

        # --- Sheet 1: Summary ---
        ws_sum = add_sheet(wb, "Summary")
        sum_cols = 9
        row_ptr = add_company_header(
            ws_sum, company_settings, start_row=1, col_span=sum_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws_sum,
            "GSTR-1 Accountant Working Paper — Summary & Section Reconciliation",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=sum_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws_sum,
            GSTR1_WORKING_PAPER_NOTE,
            row_idx=row_ptr,
            col_span=sum_cols,
        )
        row_ptr += 1

        sum_headers = [
            "GSTR-1 Working Section",
            "Target Sheet",
            "Records / Count",
            "Taxable Value (₹)",
            "CGST (₹)",
            "SGST (₹)",
            "IGST (₹)",
            "Total Tax (₹)",
            "Total Value / Invoice Total (₹)",
        ]
        sum_hdr_row = row_ptr
        sum_data_start = write_table_header(ws_sum, sum_headers, row_idx=sum_hdr_row)
        cur_row = sum_data_start

        summary_entries = [
            (
                "Table 4 — B2B Invoices (Registered GSTIN Buyers)",
                "B2B Invoices",
                len(b2b_rows),
                b2b_taxable,
                b2b_cgst,
                b2b_sgst,
                b2b_igst,
                b2b_tax,
                b2b_val,
            ),
            (
                "Table 7 — B2C Small Summary (Rate-Wise Line Items; B2CL not split)",
                "B2C Small Summary",
                len(b2c_rows),
                b2c_taxable,
                b2c_cgst,
                b2c_sgst,
                b2c_igst,
                b2c_tax,
                b2c_val,
            ),
            (
                "Total Outward Supplies (B2B Invoices + B2C Line Items — Excl. Returns)",
                "Summary Check",
                len(b2b_rows) + b2c_hdr_cnt,
                b2b_taxable + b2c_taxable,
                b2b_cgst + b2c_cgst,
                b2b_sgst + b2c_sgst,
                b2b_igst + b2c_igst,
                b2b_tax + b2c_tax,
                b2b_val + b2c_val,
            ),
            (
                "Tax Rate-Wise Line Item Cross-Check (All Outward Supplies)",
                "Tax Rate Summary",
                len(rate_rows),
                rate_taxable,
                rate_cgst,
                rate_sgst,
                rate_igst,
                rate_cgst + rate_sgst + rate_igst,
                rate_gross,
            ),
            (
                "Table 9B (Ref) — Sales Returns / Credit Notes (NOT Auto-Deducted)",
                "Credit Notes - Sales Returns",
                len(return_rows),
                ret_taxable,
                Decimal("0.00"),
                Decimal("0.00"),
                Decimal("0.00"),
                ret_tax,
                ret_net,
            ),
        ]
        for sec, sh, cnt, txbl, cg, sg, ig, tx, val in summary_entries:
            is_tot = sec.startswith("Total Outward")
            is_ret = "Sales Returns" in sec
            fnt = TOTALS_FONT if is_tot else (_FONT_STATUS_WARN if is_ret else BODY_FONT)
            write_text_cell(ws_sum, cur_row, 1, sec, font=fnt)
            write_text_cell(ws_sum, cur_row, 2, sh, alignment=_ALIGN_CENTER, font=fnt)
            _write_int_cell(ws_sum, cur_row, 3, cnt, font=fnt)
            write_currency_cell(ws_sum, cur_row, 4, txbl, font=fnt)
            write_currency_cell(ws_sum, cur_row, 5, cg, font=fnt)
            write_currency_cell(ws_sum, cur_row, 6, sg, font=fnt)
            write_currency_cell(ws_sum, cur_row, 7, ig, font=fnt)
            write_currency_cell(ws_sum, cur_row, 8, tx, font=fnt)
            write_currency_cell(ws_sum, cur_row, 9, val, font=fnt)
            cur_row += 1

        last_sum_row = cur_row - 1
        apply_alternating_row_styles(
            ws_sum, start_row=sum_data_start, end_row=last_sum_row, start_col=1, end_col=sum_cols
        )
        for col_i in range(1, sum_cols + 1):
            ws_sum.cell(row=sum_data_start + 2, column=col_i).fill = TOTALS_FILL
            ws_sum.cell(row=sum_data_start + 2, column=col_i).border = TOTALS_BORDER

        cur_row += 1
        cur_row = _add_section_subbanner(
            ws_sum,
            "Working Paper Disclosures & Known ERP Limitations",
            row_idx=cur_row,
            col_span=sum_cols,
        )
        disclosures = [
            (
                "1. B2C Classification Scope",
                f"ERP get_gstr1_b2c aggregates {b2c_hdr_cnt} B2C invoice(s) by GST slab without B2C Large (> Rs 2.5L inter-state) or state Place-of-Supply separation. B2C Invoice Extra Charges: ₹{self._quantize_2dp(b2c_hdr_extra):.2f}, Round-Off: ₹{self._quantize_2dp(b2c_hdr_round):.2f}, Invoice Header Net Total: ₹{self._quantize_2dp(b2c_hdr_net):.2f}.",
            ),
            (
                "2. Sales Returns / Credit Notes",
                "Recorded sales returns appear separately on 'Credit Notes - Sales Returns' and are NEVER auto-netted against outward supplies. Note that sales_returns stores aggregate total_tax without CGST/SGST/IGST columns.",
            ),
            (
                "3. Table 12 HSN Summary Status",
                "NOT ACCOUNTANT-READY: ERP GSTService.get_hsn_summary collapses mixed GST rates and multiple products under an arbitrary product_name. Raw output with row-level diagnostic warnings is isolated on 'HSN Reference (Unverified)'.",
            ),
        ]
        for topic, detail in disclosures:
            write_text_cell(ws_sum, cur_row, 1, topic, font=TOTALS_FONT)
            ws_sum.merge_cells(start_row=cur_row, start_column=2, end_row=cur_row, end_column=sum_cols)
            write_text_cell(ws_sum, cur_row, 2, detail)
            ws_sum.cell(row=cur_row, column=2).alignment = Alignment(
                horizontal="left", vertical="center", wrap_text=True
            )
            ws_sum.row_dimensions[cur_row].height = 28
            cur_row += 1

        apply_freeze_panes(ws_sum, header_row=sum_hdr_row, freeze_col=1)
        auto_fit_columns(ws_sum, min_width=11, max_width=46, padding=3, exclude_merged=True)
        _configure_landscape_print(ws_sum)

        # --- Sheet 2: B2B Invoices ---
        ws_b2b = add_sheet(wb, "B2B Invoices", replace_empty_default=False)
        b2b_headers = [
            "Invoice No",
            "Invoice Date",
            "Receiver GSTIN",
            "Receiver Name",
            "Place of Supply (State)",
            "Supply Type",
            "Taxable Value (₹)",
            "CGST (₹)",
            "SGST (₹)",
            "IGST (₹)",
            "Total GST (₹)",
            "Extra Charges (₹)",
            "Round Off (₹)",
            "Invoice Value (₹)",
        ]
        b2b_cols = len(b2b_headers)
        r_b2b = add_company_header(
            ws_b2b, company_settings, start_row=1, col_span=b2b_cols, include_licences=True
        )
        r_b2b = add_report_banner(
            ws_b2b,
            "GSTR-1 Working Paper — B2B Outward Supplies (Registered Buyers)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta={"Period": period_label, "B2B Invoices": len(b2b_rows)},
            start_row=r_b2b,
            col_span=b2b_cols,
        )
        r_b2b = _add_explanatory_note_banner(
            ws_b2b,
            GSTR1_WORKING_PAPER_NOTE,
            row_idx=r_b2b,
            col_span=b2b_cols,
        )
        r_b2b += 1
        b2b_hdr_row = r_b2b
        b2b_start = write_table_header(ws_b2b, b2b_headers, row_idx=b2b_hdr_row)
        cur_row = b2b_start
        b2b_totals = {idx: Decimal("0") for idx in range(7, 15)}
        for inv in b2b_rows:
            txbl = _to_decimal(inv.get("total_taxable"))
            cg = _to_decimal(inv.get("total_cgst"))
            sg = _to_decimal(inv.get("total_sgst"))
            ig = _to_decimal(inv.get("total_igst"))
            tx = cg + sg + ig
            ext = _to_decimal(inv.get("extra_charges"))
            rnd = _to_decimal(inv.get("round_off"))
            val = _to_decimal(inv.get("invoice_value"))

            b2b_totals[7] += txbl
            b2b_totals[8] += cg
            b2b_totals[9] += sg
            b2b_totals[10] += ig
            b2b_totals[11] += tx
            b2b_totals[12] += ext
            b2b_totals[13] += rnd
            b2b_totals[14] += val

            write_text_cell(ws_b2b, cur_row, 1, inv.get("invoice_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws_b2b, cur_row, 2, inv.get("invoice_date") or "")
            write_text_cell(ws_b2b, cur_row, 3, str(inv.get("receiver_gstin") or "").upper(), alignment=_ALIGN_CENTER)
            write_text_cell(ws_b2b, cur_row, 4, inv.get("receiver_name") or "")
            write_text_cell(ws_b2b, cur_row, 5, inv.get("place_of_supply") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws_b2b, cur_row, 6, self._classify_supply_scope(cg, sg, ig), alignment=_ALIGN_CENTER)
            write_currency_cell(ws_b2b, cur_row, 7, txbl)
            write_currency_cell(ws_b2b, cur_row, 8, cg)
            write_currency_cell(ws_b2b, cur_row, 9, sg)
            write_currency_cell(ws_b2b, cur_row, 10, ig)
            write_currency_cell(ws_b2b, cur_row, 11, tx)
            write_currency_cell(ws_b2b, cur_row, 12, ext)
            write_currency_cell(ws_b2b, cur_row, 13, rnd)
            write_currency_cell(ws_b2b, cur_row, 14, val)
            cur_row += 1

        b2b_last = cur_row - 1
        if b2b_last >= b2b_start:
            apply_alternating_row_styles(
                ws_b2b, start_row=b2b_start, end_row=b2b_last, start_col=1, end_col=b2b_cols
            )
            write_totals_row(
                ws_b2b,
                row_idx=cur_row,
                col_totals=b2b_totals,
                num_cols=b2b_cols,
                label="TOTAL B2B OUTWARD SUPPLIES",
                label_col=1,
                currency_cols=list(range(7, 15)),
            )
            ws_b2b.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=6)

        apply_auto_filter(
            ws_b2b, header_row=b2b_hdr_row, last_row=max(b2b_hdr_row, b2b_last), start_col=1, end_col=b2b_cols
        )
        apply_freeze_panes(ws_b2b, header_row=b2b_hdr_row, freeze_col=1)
        auto_fit_columns(ws_b2b, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws_b2b)

        # --- Sheet 3: B2C Small Summary ---
        ws_b2c = add_sheet(wb, "B2C Small Summary", replace_empty_default=False)
        b2c_headers = [
            "Supply Classification",
            "GST Rate (%)",
            "Distinct Invoices",
            "Taxable Value (₹)",
            "CGST (₹)",
            "SGST (₹)",
            "IGST (₹)",
            "Total GST (₹)",
            "Line-Item Total Value (₹)",
        ]
        b2c_cols = len(b2c_headers)
        r_b2c = add_company_header(
            ws_b2c, company_settings, start_row=1, col_span=b2c_cols, include_licences=True
        )
        r_b2c = add_report_banner(
            ws_b2c,
            "GSTR-1 Working Paper — B2C Summary by Rate (Unregistered Buyers)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta={
                "Period": period_label,
                "Rate Slabs": len(b2c_rows),
                "B2C Invoices": b2c_hdr_cnt,
            },
            start_row=r_b2c,
            col_span=b2c_cols,
        )
        r_b2c = _add_explanatory_note_banner(
            ws_b2c,
            (
                "Aggregated by GST rate from sale_items for customers without GSTIN (matches ERP "
                "GSTService.get_gstr1_b2c). B2C Large (> Rs 2.5L inter-state) & Place-of-Supply splits "
                "are not separately isolated in ERP; invoice extra charges/round-off appear on Summary."
            ),
            row_idx=r_b2c,
            col_span=b2c_cols,
        )
        r_b2c += 1
        b2c_hdr_row = r_b2c
        b2c_start = write_table_header(ws_b2c, b2c_headers, row_idx=b2c_hdr_row)
        cur_row = b2c_start
        b2c_totals = {idx: Decimal("0") for idx in range(4, 10)}
        for row in b2c_rows:
            txbl = _to_decimal(row.get("total_taxable"))
            cg = _to_decimal(row.get("total_cgst"))
            sg = _to_decimal(row.get("total_sgst"))
            ig = _to_decimal(row.get("total_igst"))
            tx = cg + sg + ig
            val = _to_decimal(row.get("total_invoice_value"))

            b2c_totals[4] += txbl
            b2c_totals[5] += cg
            b2c_totals[6] += sg
            b2c_totals[7] += ig
            b2c_totals[8] += tx
            b2c_totals[9] += val

            write_text_cell(ws_b2c, cur_row, 1, "B2C Unregistered (Rate-Wise ERP Aggregate)")
            write_percentage_cell(ws_b2c, cur_row, 2, _to_decimal(row.get("gst_rate")), decimals=2)
            _write_int_cell(ws_b2c, cur_row, 3, int(row.get("distinct_invoices") or 0))
            write_currency_cell(ws_b2c, cur_row, 4, txbl)
            write_currency_cell(ws_b2c, cur_row, 5, cg)
            write_currency_cell(ws_b2c, cur_row, 6, sg)
            write_currency_cell(ws_b2c, cur_row, 7, ig)
            write_currency_cell(ws_b2c, cur_row, 8, tx)
            write_currency_cell(ws_b2c, cur_row, 9, val)
            cur_row += 1

        b2c_last = cur_row - 1
        if b2c_last >= b2c_start:
            apply_alternating_row_styles(
                ws_b2c, start_row=b2c_start, end_row=b2c_last, start_col=1, end_col=b2c_cols
            )
            write_totals_row(
                ws_b2c,
                row_idx=cur_row,
                col_totals=b2c_totals,
                num_cols=b2c_cols,
                label="TOTAL B2C SUPPLIES (LINE ITEMS)",
                label_col=1,
                currency_cols=[4, 5, 6, 7, 8, 9],
            )
            ws_b2c.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=3)

        apply_auto_filter(
            ws_b2c, header_row=b2c_hdr_row, last_row=max(b2c_hdr_row, b2c_last), start_col=1, end_col=b2c_cols
        )
        apply_freeze_panes(ws_b2c, header_row=b2c_hdr_row, freeze_col=1)
        auto_fit_columns(ws_b2c, min_width=11, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws_b2c)

        # --- Sheet 4: Credit Notes - Sales Returns (strictly separate!) ---
        self._populate_sales_returns_reference_sheet(
            wb,
            sheet_title="Credit Notes - Sales Returns",
            report_title="GSTR-1 Working Paper — Credit Notes / Sales Returns (Separate Reference)",
            note_text=(
                "Sales returns recorded in sales_returns are reported separately here and NEVER "
                "auto-netted against B2B or B2C outward supplies. Note that sales_returns records "
                "aggregate total_tax without CGST/SGST/IGST breakdown."
            ),
            company_settings=company_settings,
            return_rows=return_rows,
            banner_from=banner_from,
            banner_to=banner_to,
            generated_by=generated_by,
            period_label=period_label,
            include_gst_classification=True,
        )

        # --- Sheet 5: Tax Rate Summary ---
        ws_rates = add_sheet(wb, "Tax Rate Summary", replace_empty_default=False)
        rate_headers = [
            "Tax Group Name",
            "Applied GST Rate (%)",
            "Distinct Invoices",
            "Taxable Value (₹)",
            "CGST (₹)",
            "SGST (₹)",
            "IGST (₹)",
            "Total GST (₹)",
            "Total Gross (₹)",
        ]
        rate_cols = len(rate_headers)
        r_rate = add_company_header(
            ws_rates, company_settings, start_row=1, col_span=rate_cols, include_licences=True
        )
        r_rate = add_report_banner(
            ws_rates,
            "GSTR-1 Working Paper — Tax Rate-Wise Summary",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta={"Period": period_label, "Tax Slabs": len(rate_rows)},
            start_row=r_rate,
            col_span=rate_cols,
        )
        r_rate = _add_explanatory_note_banner(
            ws_rates,
            GSTR1_WORKING_PAPER_NOTE,
            row_idx=r_rate,
            col_span=rate_cols,
        )
        r_rate += 1
        rate_hdr_row = r_rate
        rate_start = write_table_header(ws_rates, rate_headers, row_idx=rate_hdr_row)
        cur_row = rate_start
        rt_totals = {idx: Decimal("0") for idx in range(4, 10)}
        for rr in rate_rows:
            txbl = _to_decimal(rr.get("total_taxable"))
            cg = _to_decimal(rr.get("total_cgst"))
            sg = _to_decimal(rr.get("total_sgst"))
            ig = _to_decimal(rr.get("total_igst"))
            tx = cg + sg + ig
            gross = _to_decimal(rr.get("total_gross"))

            rt_totals[4] += txbl
            rt_totals[5] += cg
            rt_totals[6] += sg
            rt_totals[7] += ig
            rt_totals[8] += tx
            rt_totals[9] += gross

            write_text_cell(ws_rates, cur_row, 1, rr.get("tax_group_name") or "")
            write_percentage_cell(ws_rates, cur_row, 2, _to_decimal(rr.get("applied_rate")), decimals=2)
            _write_int_cell(ws_rates, cur_row, 3, int(rr.get("total_invoices") or 0))
            write_currency_cell(ws_rates, cur_row, 4, txbl)
            write_currency_cell(ws_rates, cur_row, 5, cg)
            write_currency_cell(ws_rates, cur_row, 6, sg)
            write_currency_cell(ws_rates, cur_row, 7, ig)
            write_currency_cell(ws_rates, cur_row, 8, tx)
            write_currency_cell(ws_rates, cur_row, 9, gross)
            cur_row += 1

        rate_last = cur_row - 1
        if rate_last >= rate_start:
            apply_alternating_row_styles(
                ws_rates, start_row=rate_start, end_row=rate_last, start_col=1, end_col=rate_cols
            )
            write_totals_row(
                ws_rates,
                row_idx=cur_row,
                col_totals=rt_totals,
                num_cols=rate_cols,
                label="TOTAL BY TAX RATE",
                label_col=1,
                currency_cols=[4, 5, 6, 7, 8, 9],
            )
            ws_rates.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=3)

        apply_auto_filter(
            ws_rates, header_row=rate_hdr_row, last_row=max(rate_hdr_row, rate_last), start_col=1, end_col=rate_cols
        )
        apply_freeze_panes(ws_rates, header_row=rate_hdr_row, freeze_col=1)
        auto_fit_columns(ws_rates, min_width=11, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws_rates)

        # --- Sheet 6: HSN Reference (Unverified) ---
        self._populate_unverified_hsn_sheet(
            wb,
            sheet_title="HSN Reference (Unverified)",
            company_settings=company_settings,
            hsn_rows=hsn_rows,
            banner_from=banner_from,
            banner_to=banner_to,
            generated_by=generated_by,
            period_label=period_label,
        )

        return save_workbook_to_bytes(wb), filename

    # ------------------------------------------------------------------
    # 19. Statutory Registers Export (Stage C3-B3C — ADMIN ONLY)
    # ------------------------------------------------------------------
    def export_statutory_registers(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate ADMIN-only Statutory & Audit Registers workbook (.xlsx):
        - Summary & Scope (register support status & limitations)
        - Fertilizer Register (FCO) [strictly excludes customer Aadhaar]
        - Pesticide Register [real DB columns only; omits non-existent Technical Name/CIB-RC]
        - Seed Register
        - Sales Register, Purchase Register (ADMIN-only, no PAN/bank secrets),
          Stock Register (active current batches), and Sales Returns (separate)
        - Intentionally omits Purchase Returns (no backend/UI workflow in ERP)
        """
        norm_from, norm_to = normalize_optional_date_filters(from_date, to_date)
        date_params = (norm_from, norm_from, norm_to, norm_to)

        sql_fert = """
            SELECT
                s.sale_id,
                s.invoice_no,
                substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) AS sale_date,
                COALESCE(c.customer_name, 'Customer #' || s.customer_id) AS farmer_name,
                COALESCE(c.mobile, '') AS farmer_mobile,
                COALESCE(c.village, '') AS farmer_village,
                COALESCE(p.product_name, 'Product #' || si.product_id) AS fertilizer_name,
                COALESCE(m.manufacturer_name, '') AS company_name,
                COALESCE(sb.batch_no, '') AS batch_no,
                COALESCE(sb.exp_date, '') AS exp_date,
                COALESCE(si.qty, 0.0) AS qty,
                COALESCE(NULLIF(TRIM(u_si.symbol), ''), NULLIF(TRIM(u_p.symbol), ''), '') AS unit_symbol,
                COALESCE(si.sale_rate, 0.0) AS sale_rate,
                COALESCE(si.taxable_amount, 0.0) AS taxable_amount,
                COALESCE(si.cgst_amount, 0.0) + COALESCE(si.sgst_amount, 0.0) + COALESCE(si.igst_amount, 0.0) AS tax_amount,
                COALESCE(si.total_amount, 0.0) AS total_amount
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN products p ON si.product_id = p.product_id
            JOIN categories cat ON p.category_id = cat.category_id
            JOIN customers c ON s.customer_id = c.customer_id
            LEFT JOIN manufacturers m ON p.manufacturer_id = m.manufacturer_id
            LEFT JOIN stock_batches sb ON si.batch_id = sb.batch_id
            LEFT JOIN units u_si ON si.unit_id = u_si.unit_id
            LEFT JOIN units u_p ON p.unit_id = u_p.unit_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
              AND (cat.category_name LIKE '%Fertilizer%' OR cat.short_name = 'FERT' OR cat.category_id = 1)
            ORDER BY substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) ASC, s.invoice_no ASC, si.sale_item_id ASC
            LIMIT ?;
        """
        sql_pest = """
            SELECT
                s.sale_id,
                s.invoice_no,
                substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) AS sale_date,
                COALESCE(c.customer_name, 'Customer #' || s.customer_id) AS farmer_name,
                COALESCE(c.mobile, '') AS farmer_mobile,
                COALESCE(c.village, '') AS farmer_village,
                COALESCE(p.product_name, 'Product #' || si.product_id) AS pesticide_name,
                COALESCE(p.hsn_code, '') AS hsn_code,
                COALESCE(m.manufacturer_name, '') AS company_name,
                COALESCE(sb.batch_no, '') AS batch_no,
                COALESCE(sb.mfg_date, '') AS mfg_date,
                COALESCE(sb.exp_date, '') AS exp_date,
                COALESCE(si.qty, 0.0) AS qty,
                COALESCE(NULLIF(TRIM(u_si.symbol), ''), NULLIF(TRIM(u_p.symbol), ''), '') AS unit_symbol,
                COALESCE(si.sale_rate, 0.0) AS sale_rate,
                COALESCE(si.total_amount, 0.0) AS total_amount
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN products p ON si.product_id = p.product_id
            JOIN categories cat ON p.category_id = cat.category_id
            JOIN customers c ON s.customer_id = c.customer_id
            LEFT JOIN manufacturers m ON p.manufacturer_id = m.manufacturer_id
            LEFT JOIN stock_batches sb ON si.batch_id = sb.batch_id
            LEFT JOIN units u_si ON si.unit_id = u_si.unit_id
            LEFT JOIN units u_p ON p.unit_id = u_p.unit_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
              AND (
                  cat.category_name LIKE '%Pesticide%'
                  OR cat.category_name LIKE '%Insecticide%'
                  OR cat.category_name LIKE '%Fungicide%'
                  OR cat.short_name IN ('PEST', 'FUNG')
                  OR cat.category_id IN (3, 4, 5)
              )
            ORDER BY substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) ASC, s.invoice_no ASC, si.sale_item_id ASC
            LIMIT ?;
        """
        sql_seed = """
            SELECT
                s.sale_id,
                s.invoice_no,
                substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) AS sale_date,
                COALESCE(c.customer_name, 'Customer #' || s.customer_id) AS farmer_name,
                COALESCE(c.mobile, '') AS farmer_mobile,
                COALESCE(c.village, '') AS farmer_village,
                COALESCE(p.product_name, 'Product #' || si.product_id) AS seed_variety,
                COALESCE(m.manufacturer_name, '') AS seed_company,
                COALESCE(sb.batch_no, '') AS lot_no,
                COALESCE(sb.exp_date, '') AS valid_upto,
                COALESCE(si.qty, 0.0) AS qty,
                COALESCE(NULLIF(TRIM(u_si.symbol), ''), NULLIF(TRIM(u_p.symbol), ''), '') AS unit_symbol,
                COALESCE(si.sale_rate, 0.0) AS sale_rate,
                COALESCE(si.total_amount, 0.0) AS total_amount
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN products p ON si.product_id = p.product_id
            JOIN categories cat ON p.category_id = cat.category_id
            JOIN customers c ON s.customer_id = c.customer_id
            LEFT JOIN manufacturers m ON p.manufacturer_id = m.manufacturer_id
            LEFT JOIN stock_batches sb ON si.batch_id = sb.batch_id
            LEFT JOIN units u_si ON si.unit_id = u_si.unit_id
            LEFT JOIN units u_p ON p.unit_id = u_p.unit_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
              AND (cat.category_name LIKE '%Seed%' OR cat.short_name = 'SEED' OR cat.category_id = 2)
            ORDER BY substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) ASC, s.invoice_no ASC, si.sale_item_id ASC
            LIMIT ?;
        """
        sql_sales = """
            SELECT
                s.sale_id,
                s.invoice_no,
                substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) AS sale_date,
                s.customer_id,
                COALESCE(c.customer_name, 'Customer #' || s.customer_id) AS customer_name,
                COALESCE(c.village, '') AS village,
                COALESCE(c.gstin, '') AS customer_gstin,
                COALESCE(s.payment_mode, 'CASH') AS payment_mode,
                COALESCE(s.total_taxable, 0.0) AS total_taxable,
                COALESCE(s.total_cgst, 0.0) AS total_cgst,
                COALESCE(s.total_sgst, 0.0) AS total_sgst,
                COALESCE(s.total_igst, 0.0) AS total_igst,
                COALESCE(s.total_discount, 0.0) AS total_discount,
                COALESCE(s.net_amount, 0.0) AS net_amount
            FROM sales s
            LEFT JOIN customers c ON s.customer_id = c.customer_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
            ORDER BY substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) ASC, s.invoice_no ASC, s.sale_id ASC
            LIMIT ?;
        """
        sql_purchases = """
            SELECT
                p.purchase_id,
                COALESCE(p.invoice_no, '') AS supplier_invoice_no,
                substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) AS purchase_date,
                COALESCE(p.challan_no, '') AS challan_no,
                p.supplier_id,
                COALESCE(s.supplier_name, 'Supplier #' || p.supplier_id) AS supplier_name,
                COALESCE(s.gstin, '') AS supplier_gstin,
                COALESCE(p.payment_type, 'CREDIT') AS payment_type,
                COALESCE(p.total_taxable, 0.0) AS total_taxable,
                COALESCE(p.total_cgst, 0.0) AS total_cgst,
                COALESCE(p.total_sgst, 0.0) AS total_sgst,
                COALESCE(p.total_igst, 0.0) AS total_igst,
                COALESCE(p.net_amount, 0.0) AS net_amount
            FROM purchases p
            LEFT JOIN suppliers s ON p.supplier_id = s.supplier_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) <= ?)
            ORDER BY substr(TRIM(COALESCE(p.purchase_date, '')), 1, 10) ASC, p.purchase_id ASC
            LIMIT ?;
        """
        sql_stock = """
            SELECT
                p.product_id,
                p.product_name,
                COALESCE(cat.category_name, '') AS category_name,
                COALESCE(m.manufacturer_name, '') AS company_name,
                COALESCE(p.hsn_code, '') AS hsn_code,
                COALESCE(sb.batch_no, '') AS batch_no,
                COALESCE(sb.mfg_date, '') AS mfg_date,
                COALESCE(sb.exp_date, '') AS exp_date,
                COALESCE(sb.opening_qty, 0.0) AS opening_qty,
                COALESCE(sb.current_qty, 0.0) AS current_qty,
                COALESCE(NULLIF(TRIM(u.symbol), ''), u.unit_name, '') AS unit_symbol,
                COALESCE(sb.purchase_rate, 0.0) AS purchase_rate,
                COALESCE(sb.sale_rate, 0.0) AS sale_rate,
                COALESCE(sb.mrp, 0.0) AS mrp
            FROM stock_batches sb
            JOIN products p ON sb.product_id = p.product_id
            LEFT JOIN categories cat ON p.category_id = cat.category_id
            LEFT JOIN manufacturers m ON p.manufacturer_id = m.manufacturer_id
            LEFT JOIN units u ON p.unit_id = u.unit_id
            WHERE COALESCE(sb.current_qty, 0.0) > 0
            ORDER BY
                CASE WHEN TRIM(COALESCE(sb.exp_date, '')) = '' THEN 1 ELSE 0 END ASC,
                substr(TRIM(COALESCE(sb.exp_date, '')), 1, 10) ASC,
                p.product_name ASC,
                sb.batch_id ASC
            LIMIT ?;
        """
        sql_returns = """
            SELECT
                sr.return_id,
                sr.return_no,
                substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) AS return_date,
                sr.sale_id,
                COALESCE(s.invoice_no, '') AS original_invoice_no,
                substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) AS original_invoice_date,
                sr.customer_id,
                COALESCE(c.customer_name, 'Customer #' || sr.customer_id) AS customer_name,
                COALESCE(c.gstin, '') AS customer_gstin,
                COALESCE(sr.total_taxable, 0.0) AS total_taxable,
                COALESCE(sr.total_tax, 0.0) AS total_tax,
                COALESCE(sr.round_off, 0.0) AS round_off,
                COALESCE(sr.net_amount, 0.0) AS net_amount,
                COALESCE(sr.remarks, '') AS remarks
            FROM sales_returns sr
            LEFT JOIN sales s ON sr.sale_id = s.sale_id
            LEFT JOIN customers c ON sr.customer_id = c.customer_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) <= ?)
            ORDER BY substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) ASC, sr.return_id ASC
            LIMIT ?;
        """
        queries = {
            "fert": (sql_fert, date_params + (MAX_EXPORT_ROWS + 1,)),
            "pest": (sql_pest, date_params + (MAX_EXPORT_ROWS + 1,)),
            "seed": (sql_seed, date_params + (MAX_EXPORT_ROWS + 1,)),
            "sales": (sql_sales, date_params + (MAX_EXPORT_ROWS + 1,)),
            "purchases": (sql_purchases, date_params + (MAX_EXPORT_ROWS + 1,)),
            "stock": (sql_stock, (MAX_EXPORT_ROWS + 1,)),
            "returns": (sql_returns, date_params + (MAX_EXPORT_ROWS + 1,)),
        }
        company_settings, results, extra_meta, _ = self._run_multi_snapshot_queries(queries)

        fert_rows = results["fert"]
        pest_rows = results["pest"]
        seed_rows = results["seed"]
        sales_rows = results["sales"]
        purchase_rows = results["purchases"]
        stock_rows = results["stock"]
        return_rows = results["returns"]

        banner_from, banner_to, filename = _build_period_banner_and_filename(
            "Statutory_Registers", norm_from, norm_to, extra_meta
        )
        period_label = (
            f"{norm_from} to {norm_to}" if (norm_from and norm_to) else extra_meta.get("Period", "All Dates")
        )
        extra_meta["Fertilizer Rows"] = len(fert_rows)
        extra_meta["Pesticide Rows"] = len(pest_rows)
        extra_meta["Seed Rows"] = len(seed_rows)

        wb = create_workbook("Summary & Scope")

        # --- Sheet 1: Summary & Scope ---
        ws_scope = add_sheet(wb, "Summary & Scope")
        scope_headers = [
            "Register / Sheet Name",
            "Statutory / ERP Source",
            "Support Status",
            "Records",
            "Total Qty",
            "Total Value (₹)",
            "Scope & Known ERP Limitations",
        ]
        scope_cols = len(scope_headers)
        row_ptr = add_company_header(
            ws_scope, company_settings, start_row=1, col_span=scope_cols, include_licences=True
        )
        row_ptr = add_report_banner(
            ws_scope,
            "Statutory & Inspection Registers — Coverage Summary (Internal Reference Only)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=row_ptr,
            col_span=scope_cols,
        )
        row_ptr = _add_explanatory_note_banner(
            ws_scope,
            STATUTORY_REGISTERS_NOTE,
            row_idx=row_ptr,
            col_span=scope_cols,
        )
        row_ptr += 1
        scope_hdr_row = row_ptr
        scope_start = write_table_header(ws_scope, scope_headers, row_idx=scope_hdr_row)
        cur_row = scope_start

        fert_qty = sum((_to_decimal(r["qty"]) for r in fert_rows), Decimal("0"))
        fert_amt = sum((_to_decimal(r["total_amount"]) for r in fert_rows), Decimal("0"))
        pest_qty = sum((_to_decimal(r["qty"]) for r in pest_rows), Decimal("0"))
        pest_amt = sum((_to_decimal(r["total_amount"]) for r in pest_rows), Decimal("0"))
        seed_qty = sum((_to_decimal(r["qty"]) for r in seed_rows), Decimal("0"))
        seed_amt = sum((_to_decimal(r["total_amount"]) for r in seed_rows), Decimal("0"))
        sales_amt = sum((_to_decimal(r["net_amount"]) for r in sales_rows), Decimal("0"))
        pur_amt = sum((_to_decimal(r["net_amount"]) for r in purchase_rows), Decimal("0"))
        stk_qty = sum((_to_decimal(r["current_qty"]) for r in stock_rows), Decimal("0"))
        stk_val = sum((_to_decimal(r["current_qty"]) * _to_decimal(r["purchase_rate"]) for r in stock_rows), Decimal("0"))
        ret_amt = sum((_to_decimal(r["net_amount"]) for r in return_rows), Decimal("0"))

        scope_matrix = [
            (
                "Fertilizer Register (FCO)",
                "StatutoryService.get_fertilizer_register",
                "Supported",
                len(fert_rows),
                fert_qty,
                fert_amt,
                "Customer Aadhaar intentionally excluded. Opening/closing statutory stock balances not computed per invoice line.",
            ),
            (
                "Pesticide Register",
                "StatutoryService.get_pesticide_register",
                "Partial (Schema Gap)",
                len(pest_rows),
                pest_qty,
                pest_amt,
                "ERP database lacks technical_name & cib_rc_reg_no columns; omitted rather than fabricated.",
            ),
            (
                "Seed Register",
                "StatutoryService.get_seed_register",
                "Supported",
                len(seed_rows),
                seed_qty,
                seed_amt,
                "Lot/Batch No & Valid Upto mapped from stock_batches; germination % not stored in ERP schema.",
            ),
            (
                "Sales Register",
                "sales & customers",
                "Supported",
                len(sales_rows),
                Decimal("0"),
                sales_amt,
                "Gross outward invoices for period (excludes sales returns).",
            ),
            (
                "Purchase Register",
                "purchases & suppliers",
                "Supported",
                len(purchase_rows),
                Decimal("0"),
                pur_amt,
                "Admin-only inward purchase bills (excludes PAN, bank account & IFSC).",
            ),
            (
                "Stock Register",
                "stock_batches & products",
                "Supported",
                len(stock_rows),
                stk_qty,
                stk_val,
                "Current live active stock snapshot (current_qty > 0); not historical period-end balance.",
            ),
            (
                "Sales Returns",
                "sales_returns",
                "Supported",
                len(return_rows),
                Decimal("0"),
                ret_amt,
                "Recorded customer returns kept separately (not deducted from agriculture registers).",
            ),
            (
                "Purchase Returns",
                "purchase_returns (Schema Only)",
                "Not Supported (Omitted)",
                0,
                Decimal("0"),
                Decimal("0.00"),
                "Tables exist in schema.sql, but no repository, service, API route, or UI workflow exists in ERP.",
            ),
        ]
        for reg_name, src_name, st_label, cnt, qty_tot, val_tot, lim in scope_matrix:
            write_text_cell(ws_scope, cur_row, 1, reg_name, font=TOTALS_FONT if st_label != "Not Supported (Omitted)" else _FONT_STATUS_DANGER)
            write_text_cell(ws_scope, cur_row, 2, src_name)
            write_text_cell(
                ws_scope, cur_row, 3, st_label, alignment=_ALIGN_CENTER, font=_resolve_status_font(st_label)
            )
            _write_int_cell(ws_scope, cur_row, 4, cnt)
            if qty_tot > Decimal("0") or reg_name in ("Fertilizer Register (FCO)", "Pesticide Register", "Seed Register", "Stock Register"):
                write_quantity_cell(ws_scope, cur_row, 5, qty_tot, decimals=3)
            else:
                write_text_cell(ws_scope, cur_row, 5, "-", alignment=_ALIGN_CENTER, font=_FONT_STATUS_MUTED)
            write_currency_cell(ws_scope, cur_row, 6, val_tot)
            write_text_cell(ws_scope, cur_row, 7, lim)
            ws_scope.cell(row=cur_row, column=7).alignment = Alignment(
                horizontal="left", vertical="center", wrap_text=True
            )
            ws_scope.row_dimensions[cur_row].height = 24
            cur_row += 1

        apply_alternating_row_styles(
            ws_scope, start_row=scope_start, end_row=cur_row - 1, start_col=1, end_col=scope_cols
        )
        apply_freeze_panes(ws_scope, header_row=scope_hdr_row, freeze_col=1)
        auto_fit_columns(ws_scope, min_width=11, max_width=46, padding=3, exclude_merged=True)
        ws_scope.column_dimensions["G"].width = 48
        _configure_landscape_print(ws_scope)

        # --- Sheet 2: Fertilizer Register (FCO) — strictly NO Aadhaar ---
        ws_fert = add_sheet(wb, "Fertilizer Register (FCO)", replace_empty_default=False)
        fert_headers = [
            "Invoice No",
            "Sale Date",
            "Farmer Name",
            "Mobile",
            "Village",
            "Fertilizer Name",
            "Company",
            "Batch No",
            "Expiry Date",
            "Qty Sold",
            "Unit",
            "Sale Rate (₹)",
            "Taxable Amount (₹)",
            "GST Tax (₹)",
            "Total Amount (₹)",
        ]
        f_cols = len(fert_headers)
        rf = add_company_header(ws_fert, company_settings, start_row=1, col_span=f_cols, include_licences=True)
        rf = add_report_banner(
            ws_fert,
            "Fertilizer Sale Register (FCO Reference — Customer Aadhaar Excluded)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta={"Period": period_label, "Records": len(fert_rows), "Licence": company_settings.get("dl_fertilizer") or ""},
            start_row=rf,
            col_span=f_cols,
        )
        rf = _add_explanatory_note_banner(
            ws_fert,
            "Generated from Fertilizer sale items (FCO category filter). Customer Aadhaar numbers are intentionally excluded for privacy.",
            row_idx=rf,
            col_span=f_cols,
        )
        rf += 1
        f_hdr = rf
        f_start = write_table_header(ws_fert, fert_headers, row_idx=f_hdr)
        cur_row = f_start
        f_totals = {10: Decimal("0"), 13: Decimal("0"), 14: Decimal("0"), 15: Decimal("0")}
        for fr in fert_rows:
            q = _to_decimal(fr.get("qty"))
            txbl = _to_decimal(fr.get("taxable_amount"))
            tx = _to_decimal(fr.get("tax_amount"))
            tot = _to_decimal(fr.get("total_amount"))
            f_totals[10] += q
            f_totals[13] += txbl
            f_totals[14] += tx
            f_totals[15] += tot

            write_text_cell(ws_fert, cur_row, 1, fr.get("invoice_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws_fert, cur_row, 2, fr.get("sale_date") or "")
            write_text_cell(ws_fert, cur_row, 3, fr.get("farmer_name") or "")
            write_text_cell(ws_fert, cur_row, 4, fr.get("farmer_mobile") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws_fert, cur_row, 5, fr.get("farmer_village") or "")
            write_text_cell(ws_fert, cur_row, 6, fr.get("fertilizer_name") or "")
            write_text_cell(ws_fert, cur_row, 7, fr.get("company_name") or "")
            write_text_cell(ws_fert, cur_row, 8, fr.get("batch_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws_fert, cur_row, 9, fr.get("exp_date") or "")
            write_quantity_cell(ws_fert, cur_row, 10, q, decimals=3)
            write_text_cell(ws_fert, cur_row, 11, fr.get("unit_symbol") or "", alignment=_ALIGN_CENTER)
            write_currency_cell(ws_fert, cur_row, 12, _to_decimal(fr.get("sale_rate")))
            write_currency_cell(ws_fert, cur_row, 13, txbl)
            write_currency_cell(ws_fert, cur_row, 14, tx)
            write_currency_cell(ws_fert, cur_row, 15, tot)
            cur_row += 1
        f_last = cur_row - 1
        if f_last >= f_start:
            apply_alternating_row_styles(ws_fert, start_row=f_start, end_row=f_last, start_col=1, end_col=f_cols)
            write_totals_row(
                ws_fert,
                row_idx=cur_row,
                col_totals=f_totals,
                num_cols=f_cols,
                label="TOTAL FERTILIZER SALES",
                label_col=1,
                currency_cols=[13, 14, 15],
                quantity_cols=[10],
            )
            ws_fert.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=9)
        apply_auto_filter(ws_fert, header_row=f_hdr, last_row=max(f_hdr, f_last), start_col=1, end_col=f_cols)
        apply_freeze_panes(ws_fert, header_row=f_hdr, freeze_col=1)
        auto_fit_columns(ws_fert, min_width=10, max_width=42, padding=3, exclude_merged=True)
        _configure_landscape_print(ws_fert)

        # --- Sheet 3: Pesticide Register ---
        ws_pest = add_sheet(wb, "Pesticide Register", replace_empty_default=False)
        pest_headers = [
            "Invoice No",
            "Sale Date",
            "Farmer Name",
            "Mobile",
            "Village",
            "Pesticide / Product Name",
            "HSN Code",
            "Company",
            "Batch No",
            "Mfg Date",
            "Expiry Date",
            "Qty Sold",
            "Unit",
            "Sale Rate (₹)",
            "Total Amount (₹)",
        ]
        p_cols = len(pest_headers)
        rp = add_company_header(ws_pest, company_settings, start_row=1, col_span=p_cols, include_licences=True)
        rp = add_report_banner(
            ws_pest,
            "Pesticide & Fungicide Sale Register (Insecticides Act Reference)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta={"Period": period_label, "Records": len(pest_rows), "Licence": company_settings.get("dl_pesticide") or ""},
            start_row=rp,
            col_span=p_cols,
        )
        rp = _add_explanatory_note_banner(
            ws_pest,
            "Technical Name and CIB/RC Registration No are omitted because those attributes are not stored in the Krushidhan ERP products schema.",
            row_idx=rp,
            col_span=p_cols,
        )
        rp += 1
        p_hdr = rp
        p_start = write_table_header(ws_pest, pest_headers, row_idx=p_hdr)
        cur_row = p_start
        p_totals = {12: Decimal("0"), 15: Decimal("0")}
        for pr in pest_rows:
            q = _to_decimal(pr.get("qty"))
            tot = _to_decimal(pr.get("total_amount"))
            p_totals[12] += q
            p_totals[15] += tot

            write_text_cell(ws_pest, cur_row, 1, pr.get("invoice_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws_pest, cur_row, 2, pr.get("sale_date") or "")
            write_text_cell(ws_pest, cur_row, 3, pr.get("farmer_name") or "")
            write_text_cell(ws_pest, cur_row, 4, pr.get("farmer_mobile") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws_pest, cur_row, 5, pr.get("farmer_village") or "")
            write_text_cell(ws_pest, cur_row, 6, pr.get("pesticide_name") or "")
            write_text_cell(ws_pest, cur_row, 7, pr.get("hsn_code") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws_pest, cur_row, 8, pr.get("company_name") or "")
            write_text_cell(ws_pest, cur_row, 9, pr.get("batch_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws_pest, cur_row, 10, pr.get("mfg_date") or "")
            write_date_cell(ws_pest, cur_row, 11, pr.get("exp_date") or "")
            write_quantity_cell(ws_pest, cur_row, 12, q, decimals=3)
            write_text_cell(ws_pest, cur_row, 13, pr.get("unit_symbol") or "", alignment=_ALIGN_CENTER)
            write_currency_cell(ws_pest, cur_row, 14, _to_decimal(pr.get("sale_rate")))
            write_currency_cell(ws_pest, cur_row, 15, tot)
            cur_row += 1
        p_last = cur_row - 1
        if p_last >= p_start:
            apply_alternating_row_styles(ws_pest, start_row=p_start, end_row=p_last, start_col=1, end_col=p_cols)
            write_totals_row(
                ws_pest,
                row_idx=cur_row,
                col_totals=p_totals,
                num_cols=p_cols,
                label="TOTAL PESTICIDE SALES",
                label_col=1,
                currency_cols=[15],
                quantity_cols=[12],
            )
            ws_pest.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=11)
        apply_auto_filter(ws_pest, header_row=p_hdr, last_row=max(p_hdr, p_last), start_col=1, end_col=p_cols)
        apply_freeze_panes(ws_pest, header_row=p_hdr, freeze_col=1)
        auto_fit_columns(ws_pest, min_width=10, max_width=42, padding=3, exclude_merged=True)
        _configure_landscape_print(ws_pest)

        # --- Sheet 4: Seed Register ---
        ws_seed = add_sheet(wb, "Seed Register", replace_empty_default=False)
        seed_headers = [
            "Invoice No",
            "Sale Date",
            "Farmer Name",
            "Mobile",
            "Village",
            "Seed Variety",
            "Seed Company",
            "Lot / Batch No",
            "Valid Upto",
            "Qty Sold",
            "Unit",
            "Sale Rate (₹)",
            "Total Amount (₹)",
        ]
        s_cols = len(seed_headers)
        rs = add_company_header(ws_seed, company_settings, start_row=1, col_span=s_cols, include_licences=True)
        rs = add_report_banner(
            ws_seed,
            "Seed Sale Register (Seeds Act Reference)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta={"Period": period_label, "Records": len(seed_rows), "Licence": company_settings.get("dl_seed") or ""},
            start_row=rs,
            col_span=s_cols,
        )
        rs = _add_explanatory_note_banner(
            ws_seed,
            "Generated from Seed sale items with batch lot and expiry/validity mapped from stock_batches.",
            row_idx=rs,
            col_span=s_cols,
        )
        rs += 1
        s_hdr = rs
        s_start = write_table_header(ws_seed, seed_headers, row_idx=s_hdr)
        cur_row = s_start
        sd_totals = {10: Decimal("0"), 13: Decimal("0")}
        for sr in seed_rows:
            q = _to_decimal(sr.get("qty"))
            tot = _to_decimal(sr.get("total_amount"))
            sd_totals[10] += q
            sd_totals[13] += tot

            write_text_cell(ws_seed, cur_row, 1, sr.get("invoice_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws_seed, cur_row, 2, sr.get("sale_date") or "")
            write_text_cell(ws_seed, cur_row, 3, sr.get("farmer_name") or "")
            write_text_cell(ws_seed, cur_row, 4, sr.get("farmer_mobile") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws_seed, cur_row, 5, sr.get("farmer_village") or "")
            write_text_cell(ws_seed, cur_row, 6, sr.get("seed_variety") or "")
            write_text_cell(ws_seed, cur_row, 7, sr.get("seed_company") or "")
            write_text_cell(ws_seed, cur_row, 8, sr.get("lot_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws_seed, cur_row, 9, sr.get("valid_upto") or "")
            write_quantity_cell(ws_seed, cur_row, 10, q, decimals=3)
            write_text_cell(ws_seed, cur_row, 11, sr.get("unit_symbol") or "", alignment=_ALIGN_CENTER)
            write_currency_cell(ws_seed, cur_row, 12, _to_decimal(sr.get("sale_rate")))
            write_currency_cell(ws_seed, cur_row, 13, tot)
            cur_row += 1
        s_last = cur_row - 1
        if s_last >= s_start:
            apply_alternating_row_styles(ws_seed, start_row=s_start, end_row=s_last, start_col=1, end_col=s_cols)
            write_totals_row(
                ws_seed,
                row_idx=cur_row,
                col_totals=sd_totals,
                num_cols=s_cols,
                label="TOTAL SEED SALES",
                label_col=1,
                currency_cols=[13],
                quantity_cols=[10],
            )
            ws_seed.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=9)
        apply_auto_filter(ws_seed, header_row=s_hdr, last_row=max(s_hdr, s_last), start_col=1, end_col=s_cols)
        apply_freeze_panes(ws_seed, header_row=s_hdr, freeze_col=1)
        auto_fit_columns(ws_seed, min_width=10, max_width=42, padding=3, exclude_merged=True)
        _configure_landscape_print(ws_seed)

        # --- Sheet 5: Sales Register ---
        ws_sales = add_sheet(wb, "Sales Register", replace_empty_default=False)
        sales_headers = [
            "Invoice No",
            "Sale Date",
            "Customer Name",
            "Village",
            "Customer GSTIN",
            "Payment Mode",
            "Taxable Amount (₹)",
            "CGST (₹)",
            "SGST (₹)",
            "IGST (₹)",
            "Discount (₹)",
            "Net Amount (₹)",
        ]
        sl_cols = len(sales_headers)
        rsl = add_company_header(ws_sales, company_settings, start_row=1, col_span=sl_cols, include_licences=True)
        rsl = add_report_banner(
            ws_sales,
            "Sales Register (Invoice Summary — Excl. Sales Returns)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta={"Period": period_label, "Invoices": len(sales_rows)},
            start_row=rsl,
            col_span=sl_cols,
        )
        rsl += 1
        sl_hdr = rsl
        sl_start = write_table_header(ws_sales, sales_headers, row_idx=sl_hdr)
        cur_row = sl_start
        sl_totals = {idx: Decimal("0") for idx in range(7, 13)}
        for inv in sales_rows:
            txbl = _to_decimal(inv.get("total_taxable"))
            cg = _to_decimal(inv.get("total_cgst"))
            sg = _to_decimal(inv.get("total_sgst"))
            ig = _to_decimal(inv.get("total_igst"))
            dsc = _to_decimal(inv.get("total_discount"))
            net = _to_decimal(inv.get("net_amount"))
            sl_totals[7] += txbl
            sl_totals[8] += cg
            sl_totals[9] += sg
            sl_totals[10] += ig
            sl_totals[11] += dsc
            sl_totals[12] += net

            write_text_cell(ws_sales, cur_row, 1, inv.get("invoice_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws_sales, cur_row, 2, inv.get("sale_date") or "")
            write_text_cell(ws_sales, cur_row, 3, inv.get("customer_name") or "")
            write_text_cell(ws_sales, cur_row, 4, inv.get("village") or "")
            write_text_cell(ws_sales, cur_row, 5, str(inv.get("customer_gstin") or "").upper(), alignment=_ALIGN_CENTER)
            write_text_cell(ws_sales, cur_row, 6, _format_payment_mode_label(inv.get("payment_mode")), alignment=_ALIGN_CENTER)
            write_currency_cell(ws_sales, cur_row, 7, txbl)
            write_currency_cell(ws_sales, cur_row, 8, cg)
            write_currency_cell(ws_sales, cur_row, 9, sg)
            write_currency_cell(ws_sales, cur_row, 10, ig)
            write_currency_cell(ws_sales, cur_row, 11, dsc)
            write_currency_cell(ws_sales, cur_row, 12, net)
            cur_row += 1
        sl_last = cur_row - 1
        if sl_last >= sl_start:
            apply_alternating_row_styles(ws_sales, start_row=sl_start, end_row=sl_last, start_col=1, end_col=sl_cols)
            write_totals_row(
                ws_sales,
                row_idx=cur_row,
                col_totals=sl_totals,
                num_cols=sl_cols,
                label="TOTAL SALES INVOICES",
                label_col=1,
                currency_cols=list(range(7, 13)),
            )
            ws_sales.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=6)
        apply_auto_filter(ws_sales, header_row=sl_hdr, last_row=max(sl_hdr, sl_last), start_col=1, end_col=sl_cols)
        apply_freeze_panes(ws_sales, header_row=sl_hdr, freeze_col=1)
        auto_fit_columns(ws_sales, min_width=10, max_width=42, padding=3, exclude_merged=True)
        _configure_landscape_print(ws_sales)

        # --- Sheet 6: Purchase Register (ADMIN-only, no PAN/bank/IFSC) ---
        ws_pur = add_sheet(wb, "Purchase Register", replace_empty_default=False)
        pur_headers = [
            "Purchase ID",
            "Supplier Invoice No",
            "Purchase Date",
            "Challan No",
            "Supplier Name",
            "Supplier GSTIN",
            "Payment Mode",
            "Taxable Amount (₹)",
            "CGST (₹)",
            "SGST (₹)",
            "IGST (₹)",
            "Net Amount (₹)",
        ]
        pr_cols = len(pur_headers)
        rpr = add_company_header(ws_pur, company_settings, start_row=1, col_span=pr_cols, include_licences=True)
        rpr = add_report_banner(
            ws_pur,
            "Purchase Register (Inward Bills — Purchase Returns Unsupported in ERP)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta={"Period": period_label, "Purchases": len(purchase_rows)},
            start_row=rpr,
            col_span=pr_cols,
        )
        rpr += 1
        pr_hdr = rpr
        pr_start = write_table_header(ws_pur, pur_headers, row_idx=pr_hdr)
        cur_row = pr_start
        pr_totals = {idx: Decimal("0") for idx in range(8, 13)}
        for pur in purchase_rows:
            txbl = _to_decimal(pur.get("total_taxable"))
            cg = _to_decimal(pur.get("total_cgst"))
            sg = _to_decimal(pur.get("total_sgst"))
            ig = _to_decimal(pur.get("total_igst"))
            net = _to_decimal(pur.get("net_amount"))
            pr_totals[8] += txbl
            pr_totals[9] += cg
            pr_totals[10] += sg
            pr_totals[11] += ig
            pr_totals[12] += net

            _write_int_cell(ws_pur, cur_row, 1, int(pur["purchase_id"]))
            write_text_cell(ws_pur, cur_row, 2, pur.get("supplier_invoice_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws_pur, cur_row, 3, pur.get("purchase_date") or "")
            write_text_cell(ws_pur, cur_row, 4, pur.get("challan_no") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws_pur, cur_row, 5, pur.get("supplier_name") or "")
            write_text_cell(ws_pur, cur_row, 6, str(pur.get("supplier_gstin") or "").upper(), alignment=_ALIGN_CENTER)
            write_text_cell(ws_pur, cur_row, 7, _format_payment_mode_label(pur.get("payment_type")), alignment=_ALIGN_CENTER)
            write_currency_cell(ws_pur, cur_row, 8, txbl)
            write_currency_cell(ws_pur, cur_row, 9, cg)
            write_currency_cell(ws_pur, cur_row, 10, sg)
            write_currency_cell(ws_pur, cur_row, 11, ig)
            write_currency_cell(ws_pur, cur_row, 12, net)
            cur_row += 1
        pr_last = cur_row - 1
        if pr_last >= pr_start:
            apply_alternating_row_styles(ws_pur, start_row=pr_start, end_row=pr_last, start_col=1, end_col=pr_cols)
            write_totals_row(
                ws_pur,
                row_idx=cur_row,
                col_totals=pr_totals,
                num_cols=pr_cols,
                label="TOTAL INWARD PURCHASES",
                label_col=1,
                currency_cols=list(range(8, 13)),
            )
            ws_pur.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=7)
        apply_auto_filter(ws_pur, header_row=pr_hdr, last_row=max(pr_hdr, pr_last), start_col=1, end_col=pr_cols)
        apply_freeze_panes(ws_pur, header_row=pr_hdr, freeze_col=1)
        auto_fit_columns(ws_pur, min_width=10, max_width=42, padding=3, exclude_merged=True)
        _configure_landscape_print(ws_pur)

        # --- Sheet 7: Stock Register (Live Active Batches Snapshot) ---
        ws_stk = add_sheet(wb, "Stock Register", replace_empty_default=False)
        stk_headers = [
            "Product ID",
            "Product Name",
            "Category",
            "Company",
            "HSN Code",
            "Batch No",
            "Mfg Date",
            "Expiry Date",
            "Opening Qty",
            "Current Qty",
            "Unit",
            "Purchase Rate (₹)",
            "Sale Rate (₹)",
            "MRP (₹)",
            "Stock Cost Value (₹)",
        ]
        st_cols = len(stk_headers)
        rst = add_company_header(ws_stk, company_settings, start_row=1, col_span=st_cols, include_licences=True)
        rst = add_report_banner(
            ws_stk,
            "Stock Register (Live Active Stock Batches Snapshot — current_qty > 0)",
            generated_by=generated_by,
            extra_meta={"Active Batches": len(stock_rows), "Snapshot Scope": "Live Inventory State (Not Period-End Rollback)"},
            start_row=rst,
            col_span=st_cols,
        )
        rst += 1
        st_hdr = rst
        st_start = write_table_header(ws_stk, stk_headers, row_idx=st_hdr)
        cur_row = st_start
        st_totals = {9: Decimal("0"), 10: Decimal("0"), 15: Decimal("0")}
        for sb in stock_rows:
            op_q = _to_decimal(sb.get("opening_qty"))
            cur_q = _to_decimal(sb.get("current_qty"))
            p_rate = _to_decimal(sb.get("purchase_rate"))
            cost_val = self._quantize_2dp(cur_q * p_rate)
            st_totals[9] += op_q
            st_totals[10] += cur_q
            st_totals[15] += cost_val

            _write_int_cell(ws_stk, cur_row, 1, int(sb["product_id"]))
            write_text_cell(ws_stk, cur_row, 2, sb.get("product_name") or "")
            write_text_cell(ws_stk, cur_row, 3, sb.get("category_name") or "")
            write_text_cell(ws_stk, cur_row, 4, sb.get("company_name") or "")
            write_text_cell(ws_stk, cur_row, 5, sb.get("hsn_code") or "", alignment=_ALIGN_CENTER)
            write_text_cell(ws_stk, cur_row, 6, sb.get("batch_no") or "", alignment=_ALIGN_CENTER)
            write_date_cell(ws_stk, cur_row, 7, sb.get("mfg_date") or "")
            write_date_cell(ws_stk, cur_row, 8, sb.get("exp_date") or "")
            write_quantity_cell(ws_stk, cur_row, 9, op_q, decimals=3)
            write_quantity_cell(ws_stk, cur_row, 10, cur_q, decimals=3)
            write_text_cell(ws_stk, cur_row, 11, sb.get("unit_symbol") or "", alignment=_ALIGN_CENTER)
            write_currency_cell(ws_stk, cur_row, 12, p_rate)
            write_currency_cell(ws_stk, cur_row, 13, _to_decimal(sb.get("sale_rate")))
            write_currency_cell(ws_stk, cur_row, 14, _to_decimal(sb.get("mrp")))
            write_currency_cell(ws_stk, cur_row, 15, cost_val)
            cur_row += 1
        st_last = cur_row - 1
        if st_last >= st_start:
            apply_alternating_row_styles(ws_stk, start_row=st_start, end_row=st_last, start_col=1, end_col=st_cols)
            write_totals_row(
                ws_stk,
                row_idx=cur_row,
                col_totals=st_totals,
                num_cols=st_cols,
                label="TOTAL ACTIVE STOCK & COST VALUE",
                label_col=1,
                currency_cols=[15],
                quantity_cols=[9, 10],
            )
            ws_stk.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=8)
        apply_auto_filter(ws_stk, header_row=st_hdr, last_row=max(st_hdr, st_last), start_col=1, end_col=st_cols)
        apply_freeze_panes(ws_stk, header_row=st_hdr, freeze_col=1)
        auto_fit_columns(ws_stk, min_width=10, max_width=42, padding=3, exclude_merged=True)
        _configure_landscape_print(ws_stk)

        # --- Sheet 8: Sales Returns (separate) ---
        self._populate_sales_returns_reference_sheet(
            wb,
            sheet_title="Sales Returns",
            report_title="Sales Returns Register (Separately Reported Customer Returns)",
            note_text=(
                "Sales returns recorded in sales_returns are listed separately and NOT deducted from "
                "the Fertilizer, Pesticide, Seed, or Sales registers. Purchase Returns are omitted "
                "because Krushidhan ERP has no purchase-return workflow."
            ),
            company_settings=company_settings,
            return_rows=return_rows,
            banner_from=banner_from,
            banner_to=banner_to,
            generated_by=generated_by,
            period_label=period_label,
            include_gst_classification=False,
        )

        return save_workbook_to_bytes(wb), filename

    # ------------------------------------------------------------------
    # 20. Profit & Loss Workbook Export (Stage C3-B3C — ADMIN ONLY)
    # ------------------------------------------------------------------
    def export_profit_and_loss(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        generated_by: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """
        Generate the ADMIN-only Profit & Loss workbook (.xlsx):
        - Reproduces AccountingService.get_profit_and_loss & get_product_profitability exactly
        - Includes the mandatory verbatim sales-return disclosure note
        - Never labels Net Profit as return-adjusted or final net profit
        - Includes a dedicated `Sales Returns Reference` worksheet without netting into P&L
        """
        norm_from, norm_to = normalize_optional_date_filters(from_date, to_date)
        date_params = (norm_from, norm_from, norm_to, norm_to)

        sql_sales = """
            SELECT
                COALESCE(SUM(s.total_taxable), 0.0) AS total_taxable,
                COALESCE(SUM(s.net_amount), 0.0) AS gross_sales,
                COALESCE(SUM(s.total_discount), 0.0) AS total_discount,
                COUNT(s.sale_id) AS total_invoices
            FROM sales s
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?);
        """
        sql_cogs = """
            SELECT
                COALESCE(SUM(si.qty * COALESCE(sb.purchase_rate, p.default_purchase_rate)), 0.0) AS cogs
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN products p ON si.product_id = p.product_id
            LEFT JOIN stock_batches sb ON si.batch_id = sb.batch_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?);
        """
        sql_expenses = """
            SELECT
                COALESCE(ec.category_name, 'General Expenses') AS category_name,
                COALESCE(SUM(e.amount), 0.0) AS total_amount
            FROM expenses e
            LEFT JOIN expense_categories ec ON e.category_id = ec.category_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(e.expense_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(e.expense_date, '')), 1, 10) <= ?)
            GROUP BY ec.category_name
            ORDER BY total_amount DESC, category_name ASC
            LIMIT ?;
        """
        sql_cat_breakdown = """
            SELECT
                COALESCE(c.category_name, 'General') AS category_name,
                COALESCE(SUM(si.taxable_amount), 0.0) AS sales_revenue,
                COALESCE(SUM(si.qty * COALESCE(sb.purchase_rate, p.default_purchase_rate)), 0.0) AS cogs,
                COALESCE(
                    SUM(si.taxable_amount) - SUM(si.qty * COALESCE(sb.purchase_rate, p.default_purchase_rate)),
                    0.0
                ) AS gross_profit
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN products p ON si.product_id = p.product_id
            LEFT JOIN categories c ON p.category_id = c.category_id
            LEFT JOIN stock_batches sb ON si.batch_id = sb.batch_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
            GROUP BY c.category_name
            ORDER BY gross_profit DESC, category_name ASC
            LIMIT ?;
        """
        sql_prod_prof = """
            SELECT
                p.product_id,
                p.product_name,
                COALESCE(c.category_name, 'General') AS category_name,
                COALESCE(m.manufacturer_name, '-') AS manufacturer_name,
                COALESCE(SUM(si.qty), 0.0) AS qty_sold,
                COALESCE(u.symbol, 'Nos') AS unit_symbol,
                COALESCE(SUM(si.taxable_amount), 0.0) AS sales_revenue,
                COALESCE(SUM(si.qty * COALESCE(sb.purchase_rate, p.default_purchase_rate)), 0.0) AS cogs,
                COALESCE(
                    SUM(si.taxable_amount) - SUM(si.qty * COALESCE(sb.purchase_rate, p.default_purchase_rate)),
                    0.0
                ) AS gross_profit
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN products p ON si.product_id = p.product_id
            LEFT JOIN categories c ON p.category_id = c.category_id
            LEFT JOIN manufacturers m ON p.manufacturer_id = m.manufacturer_id
            LEFT JOIN units u ON p.unit_id = u.unit_id
            LEFT JOIN stock_batches sb ON si.batch_id = sb.batch_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) <= ?)
            GROUP BY p.product_id
            ORDER BY gross_profit DESC, p.product_name ASC
            LIMIT ?;
        """
        sql_closing_stock = """
            SELECT
                COALESCE(SUM(current_qty * purchase_rate), 0.0) AS total_valuation,
                COALESCE(SUM(current_qty), 0.0) AS total_units
            FROM stock_batches
            WHERE current_qty > 0;
        """
        sql_returns = """
            SELECT
                sr.return_id,
                sr.return_no,
                substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) AS return_date,
                sr.sale_id,
                COALESCE(s.invoice_no, '') AS original_invoice_no,
                substr(TRIM(COALESCE(s.sale_date, '')), 1, 10) AS original_invoice_date,
                sr.customer_id,
                COALESCE(c.customer_name, 'Customer #' || sr.customer_id) AS customer_name,
                COALESCE(c.gstin, '') AS customer_gstin,
                COALESCE(sr.total_taxable, 0.0) AS total_taxable,
                COALESCE(sr.total_tax, 0.0) AS total_tax,
                COALESCE(sr.round_off, 0.0) AS round_off,
                COALESCE(sr.net_amount, 0.0) AS net_amount,
                COALESCE(sr.remarks, '') AS remarks
            FROM sales_returns sr
            LEFT JOIN sales s ON sr.sale_id = s.sale_id
            LEFT JOIN customers c ON sr.customer_id = c.customer_id
            WHERE (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) >= ?)
              AND (? IS NULL OR substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) <= ?)
            ORDER BY substr(TRIM(COALESCE(sr.return_date, '')), 1, 10) ASC, sr.return_id ASC
            LIMIT ?;
        """
        queries = {
            "sales": (sql_sales, date_params),
            "cogs": (sql_cogs, date_params),
            "expenses": (sql_expenses, date_params + (MAX_EXPORT_ROWS + 1,)),
            "categories": (sql_cat_breakdown, date_params + (MAX_EXPORT_ROWS + 1,)),
            "products": (sql_prod_prof, date_params + (MAX_EXPORT_ROWS + 1,)),
            "closing_stock": (sql_closing_stock, ()),
            "returns": (sql_returns, date_params + (MAX_EXPORT_ROWS + 1,)),
        }
        company_settings, results, extra_meta, _ = self._run_multi_snapshot_queries(queries)

        sales_row = results["sales"][0] if results["sales"] else {}
        cogs_row = results["cogs"][0] if results["cogs"] else {}
        exp_rows = results["expenses"]
        cat_rows = results["categories"]
        prod_rows = results["products"]
        stk_row = results["closing_stock"][0] if results["closing_stock"] else {}
        return_rows = results["returns"]

        sales_rev = _to_decimal(sales_row.get("total_taxable"))
        gross_sales = _to_decimal(sales_row.get("gross_sales"))
        total_discount = _to_decimal(sales_row.get("total_discount"))
        total_invoices = int(sales_row.get("total_invoices") or 0)
        cogs = _to_decimal(cogs_row.get("cogs"))
        total_expenses = sum((_to_decimal(e.get("total_amount")) for e in exp_rows), Decimal("0"))

        gross_profit = self._quantize_2dp(sales_rev - cogs)
        gross_margin_pct = self._calc_margin_pct(gross_profit, sales_rev, decimals=2)
        net_profit = self._quantize_2dp(gross_profit - total_expenses)
        net_margin_pct = self._calc_margin_pct(net_profit, sales_rev, decimals=2)
        closing_stock_val = _to_decimal(stk_row.get("total_valuation"))

        ret_taxable = sum((_to_decimal(r.get("total_taxable")) for r in return_rows), Decimal("0"))
        ret_tax = sum((_to_decimal(r.get("total_tax")) for r in return_rows), Decimal("0"))
        ret_net = sum((_to_decimal(r.get("net_amount")) for r in return_rows), Decimal("0"))

        banner_from, banner_to, filename = _build_period_banner_and_filename(
            "Profit_And_Loss", norm_from, norm_to, extra_meta
        )
        period_label = (
            f"{norm_from} to {norm_to}" if (norm_from and norm_to) else extra_meta.get("Period", "All Dates")
        )
        extra_meta["Invoices"] = total_invoices
        extra_meta["Returns (Separate Ref)"] = len(return_rows)

        # --- Sheet 1: Profit & Loss ---
        wb = create_workbook("Profit & Loss")
        ws_pnl = add_sheet(wb, "Profit & Loss")
        pnl_cols = 4
        rp = add_company_header(
            ws_pnl, company_settings, start_row=1, col_span=pnl_cols, include_licences=True
        )
        rp = add_report_banner(
            ws_pnl,
            "Profit & Loss Statement (ERP Calculation — Gross Before Returns)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta=extra_meta,
            start_row=rp,
            col_span=pnl_cols,
        )
        rp = _add_explanatory_note_banner(
            ws_pnl,
            PNL_RETURN_DISCLOSURE_NOTE,
            row_idx=rp,
            col_span=pnl_cols,
            row_height=54,
        )
        rp += 1

        rp = _add_section_subbanner(
            ws_pnl,
            "Trading & Profit / Loss Statement (Matches ERP AccountingService.get_profit_and_loss)",
            row_idx=rp,
            col_span=pnl_cols,
        )
        stmt_headers = ["Particulars", "Amount (₹)", "Margin % / Count", "ERP Basis & Notes"]
        stmt_hdr = rp
        stmt_start = write_table_header(ws_pnl, stmt_headers, row_idx=stmt_hdr)
        cur_row = stmt_start

        # Revenue
        write_text_cell(ws_pnl, cur_row, 1, "A. Revenue from Operations (Taxable Sales Turnover)", font=TOTALS_FONT)
        write_currency_cell(ws_pnl, cur_row, 2, sales_rev, font=TOTALS_FONT)
        _write_int_cell(ws_pnl, cur_row, 3, total_invoices)
        write_text_cell(ws_pnl, cur_row, 4, "Total invoices count; unadjusted for sales returns")
        cur_row += 1

        # COGS
        write_text_cell(ws_pnl, cur_row, 1, "Less: Cost of Goods Sold (COGS - Batch Purchase Rate)")
        write_currency_cell(ws_pnl, cur_row, 2, cogs, font=_FONT_STATUS_DANGER)
        write_text_cell(ws_pnl, cur_row, 3, "-", alignment=_ALIGN_CENTER)
        write_text_cell(ws_pnl, cur_row, 4, "COGS from sale_items x batch purchase_rate (unadjusted for returns)")
        cur_row += 1

        # Gross Profit
        gp_font = TOTALS_FONT if gross_profit >= Decimal("0") else _FONT_STATUS_DANGER
        write_text_cell(ws_pnl, cur_row, 1, "Reported Gross Profit (A - COGS — Unadjusted for Returns)", font=gp_font)
        write_currency_cell(ws_pnl, cur_row, 2, gross_profit, font=gp_font)
        write_percentage_cell(ws_pnl, cur_row, 3, gross_margin_pct, decimals=2, font=gp_font)
        write_text_cell(ws_pnl, cur_row, 4, "Matches ERP P&L Gross Profit & Gross Margin %")
        gp_row_idx = cur_row
        cur_row += 1

        # Expenses header
        write_text_cell(ws_pnl, cur_row, 1, "B. Operating / Shop Expenses", font=TOTALS_FONT)
        write_currency_cell(ws_pnl, cur_row, 2, total_expenses, font=TOTALS_FONT)
        _write_int_cell(ws_pnl, cur_row, 3, len(exp_rows))
        write_text_cell(ws_pnl, cur_row, 4, "Category count; from expenses table")
        cur_row += 1

        if exp_rows:
            for er in exp_rows:
                write_text_cell(ws_pnl, cur_row, 1, f"   - {er.get('category_name') or 'General Expenses'}")
                write_currency_cell(ws_pnl, cur_row, 2, _to_decimal(er.get("total_amount")), font=_FONT_STATUS_DANGER)
                write_text_cell(ws_pnl, cur_row, 3, "-", alignment=_ALIGN_CENTER)
                write_text_cell(ws_pnl, cur_row, 4, "Operating expense category")
                cur_row += 1
        else:
            write_text_cell(ws_pnl, cur_row, 1, "   - No shop expenses recorded in period", font=_FONT_STATUS_MUTED)
            write_currency_cell(ws_pnl, cur_row, 2, Decimal("0.00"), font=_FONT_STATUS_MUTED)
            write_text_cell(ws_pnl, cur_row, 3, "-", alignment=_ALIGN_CENTER, font=_FONT_STATUS_MUTED)
            write_text_cell(ws_pnl, cur_row, 4, "Zero operating expenses recorded", font=_FONT_STATUS_MUTED)
            cur_row += 1

        # Net Operating Profit / Loss (never labelled "return-adjusted" or "final net profit")
        np_font = _FONT_STATUS_OK if net_profit >= Decimal("0") else _FONT_STATUS_DANGER
        np_label = (
            "Reported Net Operating Profit (Unadjusted for Returns)"
            if net_profit >= Decimal("0")
            else "Reported Net Operating Loss (Unadjusted for Returns)"
        )
        write_text_cell(ws_pnl, cur_row, 1, np_label, font=TOTALS_FONT)
        write_currency_cell(ws_pnl, cur_row, 2, net_profit, font=np_font)
        write_percentage_cell(ws_pnl, cur_row, 3, net_margin_pct, decimals=2, font=np_font)
        write_text_cell(ws_pnl, cur_row, 4, "Matches ERP P&L Net Profit (sales returns NOT deducted)")
        np_row_idx = cur_row
        cur_row += 1

        stmt_last = cur_row - 1
        apply_alternating_row_styles(
            ws_pnl, start_row=stmt_start, end_row=stmt_last, start_col=1, end_col=pnl_cols
        )
        for col_i in range(1, pnl_cols + 1):
            ws_pnl.cell(row=gp_row_idx, column=col_i).fill = TOTALS_FILL
            ws_pnl.cell(row=gp_row_idx, column=col_i).border = TOTALS_BORDER
            ws_pnl.cell(row=np_row_idx, column=col_i).fill = TOTALS_FILL
            ws_pnl.cell(row=np_row_idx, column=col_i).border = TOTALS_BORDER

        cur_row += 1
        cur_row = _add_section_subbanner(
            ws_pnl,
            "Memo & Reference Figures (Separate From Reported Profit Above)",
            row_idx=cur_row,
            col_span=pnl_cols,
        )
        memo_hdr_row = write_table_header(
            ws_pnl,
            ["Reference Metric", "Amount (₹)", "Count / Note", "Disclosure"],
            row_idx=cur_row,
        )
        memo_start = memo_hdr_row
        memo_rows = [
            (
                "Gross Sales Value (Incl. GST, Extra Charges & Round-Off)",
                gross_sales,
                total_invoices,
                "From sales.net_amount (includes indirect taxes & round-off)",
            ),
            (
                "Total Line Discounts Allowed",
                total_discount,
                total_invoices,
                "From sales.total_discount",
            ),
            (
                "Separately Recorded Sales Returns — Taxable Value (NOT Deducted)",
                ret_taxable,
                len(return_rows),
                "Review 'Sales Returns Reference' sheet before final financial reporting",
            ),
            (
                "Separately Recorded Sales Returns — Net Refund/Credit Value (NOT Deducted)",
                ret_net,
                len(return_rows),
                f"Includes Return Tax ₹{self._quantize_2dp(ret_tax):.2f} (not netted above)",
            ),
            (
                "Current Inventory Valuation (Live Stock Snapshot — Not Historical Period-End)",
                closing_stock_val,
                int(round(float(stk_row.get("total_units") or 0))),
                "Live stock_batches where current_qty > 0 multiplied by purchase_rate",
            ),
        ]
        cur_row = memo_start
        for m_lbl, m_amt, m_cnt, m_disc in memo_rows:
            is_ret_m = "Sales Returns" in m_lbl
            fnt = _FONT_STATUS_WARN if is_ret_m else BODY_FONT
            write_text_cell(ws_pnl, cur_row, 1, m_lbl, font=fnt)
            write_currency_cell(ws_pnl, cur_row, 2, m_amt, font=fnt)
            _write_int_cell(ws_pnl, cur_row, 3, m_cnt, font=fnt)
            write_text_cell(ws_pnl, cur_row, 4, m_disc, font=fnt)
            cur_row += 1
        apply_alternating_row_styles(
            ws_pnl, start_row=memo_start, end_row=cur_row - 1, start_col=1, end_col=pnl_cols
        )

        apply_freeze_panes(ws_pnl, header_row=stmt_hdr, freeze_col=1)
        auto_fit_columns(ws_pnl, min_width=14, max_width=52, padding=3, exclude_merged=True)
        _configure_landscape_print(ws_pnl)

        # --- Sheet 2: Category & Product Margins ---
        ws_margins = add_sheet(wb, "Category & Product Margins", replace_empty_default=False)
        m_cols = 10
        rm = add_company_header(ws_margins, company_settings, start_row=1, col_span=m_cols, include_licences=True)
        rm = add_report_banner(
            ws_margins,
            "Profit & Loss — Category & Product Profitability (Before Sales Returns)",
            from_date=banner_from,
            to_date=banner_to,
            generated_by=generated_by,
            extra_meta={"Period": period_label, "Categories": len(cat_rows), "Products": len(prod_rows)},
            start_row=rm,
            col_span=m_cols,
        )
        rm = _add_explanatory_note_banner(
            ws_margins,
            PNL_RETURN_DISCLOSURE_NOTE,
            row_idx=rm,
            col_span=m_cols,
            row_height=46,
        )
        rm += 1

        rm = _add_section_subbanner(
            ws_margins,
            "1. Category Margin Breakdown",
            row_idx=rm,
            col_span=m_cols,
        )
        cat_headers = [
            "Category Name",
            "Taxable Sales Revenue (₹)",
            "Cost of Goods Sold (₹)",
            "Gross Profit (₹)",
            "Margin %",
        ]
        cat_hdr_row = rm
        cat_start = write_table_header(ws_margins, cat_headers, row_idx=cat_hdr_row)
        cur_row = cat_start
        cat_totals = {2: Decimal("0"), 3: Decimal("0"), 4: Decimal("0")}
        for cr in cat_rows:
            c_rev = _to_decimal(cr.get("sales_revenue"))
            c_cogs = _to_decimal(cr.get("cogs"))
            c_gp = _to_decimal(cr.get("gross_profit"))
            c_margin = self._calc_margin_pct(c_gp, c_rev, decimals=1)
            cat_totals[2] += c_rev
            cat_totals[3] += c_cogs
            cat_totals[4] += c_gp

            write_text_cell(ws_margins, cur_row, 1, cr.get("category_name") or "General")
            write_currency_cell(ws_margins, cur_row, 2, c_rev)
            write_currency_cell(ws_margins, cur_row, 3, c_cogs)
            write_currency_cell(
                ws_margins, cur_row, 4, c_gp, font=_FONT_STATUS_OK if c_gp >= 0 else _FONT_STATUS_DANGER
            )
            write_percentage_cell(ws_margins, cur_row, 5, c_margin, decimals=1)
            cur_row += 1

        cat_last = cur_row - 1
        if cat_last >= cat_start:
            apply_alternating_row_styles(
                ws_margins, start_row=cat_start, end_row=cat_last, start_col=1, end_col=5
            )
            cur_row = write_totals_row(
                ws_margins,
                row_idx=cur_row,
                col_totals=cat_totals,
                num_cols=5,
                label="TOTAL CATEGORY MARGINS",
                label_col=1,
                currency_cols=[2, 3, 4],
            )
        else:
            write_text_cell(ws_margins, cur_row, 1, "No category sales in selected period.", font=_FONT_STATUS_MUTED)
            cur_row += 1

        cur_row += 1
        cur_row = _add_section_subbanner(
            ws_margins,
            "2. Product-Wise Profitability & Margins",
            row_idx=cur_row,
            col_span=m_cols,
        )
        prod_headers_10 = [
            "Product ID",
            "Product Name",
            "Category",
            "Manufacturer",
            "Qty Sold",
            "Unit",
            "Taxable Sales (₹)",
            "COGS Cost (₹)",
            "Gross Profit (₹)",
            "Margin %",
        ]
        prod_hdr_row = cur_row
        prod_start = write_table_header(ws_margins, prod_headers_10, row_idx=prod_hdr_row)
        cur_row = prod_start
        prod_totals = {5: Decimal("0"), 7: Decimal("0"), 8: Decimal("0"), 9: Decimal("0")}
        for pr in prod_rows:
            q_sold = _to_decimal(pr.get("qty_sold"))
            p_rev = _to_decimal(pr.get("sales_revenue"))
            p_cogs = _to_decimal(pr.get("cogs"))
            p_gp = _to_decimal(pr.get("gross_profit"))
            p_margin = self._calc_margin_pct(p_gp, p_rev, decimals=1)

            prod_totals[5] += q_sold
            prod_totals[7] += p_rev
            prod_totals[8] += p_cogs
            prod_totals[9] += p_gp

            _write_int_cell(ws_margins, cur_row, 1, int(pr["product_id"]))
            write_text_cell(ws_margins, cur_row, 2, pr.get("product_name") or "")
            write_text_cell(ws_margins, cur_row, 3, pr.get("category_name") or "General")
            write_text_cell(ws_margins, cur_row, 4, pr.get("manufacturer_name") or "-")
            write_quantity_cell(ws_margins, cur_row, 5, q_sold, decimals=3)
            write_text_cell(ws_margins, cur_row, 6, pr.get("unit_symbol") or "Nos", alignment=_ALIGN_CENTER)
            write_currency_cell(ws_margins, cur_row, 7, p_rev)
            write_currency_cell(ws_margins, cur_row, 8, p_cogs)
            write_currency_cell(
                ws_margins, cur_row, 9, p_gp, font=_FONT_STATUS_OK if p_gp >= 0 else _FONT_STATUS_DANGER
            )
            write_percentage_cell(ws_margins, cur_row, 10, p_margin, decimals=1)
            cur_row += 1

        prod_last = cur_row - 1
        if prod_last >= prod_start:
            apply_alternating_row_styles(
                ws_margins, start_row=prod_start, end_row=prod_last, start_col=1, end_col=10
            )
            write_totals_row(
                ws_margins,
                row_idx=cur_row,
                col_totals=prod_totals,
                num_cols=10,
                label="TOTAL PRODUCT PROFITABILITY",
                label_col=1,
                currency_cols=[7, 8, 9],
                quantity_cols=[5],
            )
            ws_margins.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=4)

        apply_auto_filter(
            ws_margins, header_row=prod_hdr_row, last_row=max(prod_hdr_row, prod_last), start_col=1, end_col=10
        )
        apply_freeze_panes(ws_margins, header_row=cat_hdr_row, freeze_col=1)
        auto_fit_columns(ws_margins, min_width=10, max_width=45, padding=3, exclude_merged=True)
        _configure_landscape_print(ws_margins)

        # --- Sheet 3: Sales Returns Reference (never netted into P&L) ---
        self._populate_sales_returns_reference_sheet(
            wb,
            sheet_title="Sales Returns Reference",
            report_title="Profit & Loss — Separately Recorded Sales Returns Reference",
            note_text=(
                "Sales returns listed below are NOT deducted from Revenue, COGS, Gross Profit, or "
                "Net Profit on the Profit & Loss sheet. Review these returns separately."
            ),
            company_settings=company_settings,
            return_rows=return_rows,
            banner_from=banner_from,
            banner_to=banner_to,
            generated_by=generated_by,
            period_label=period_label,
            include_gst_classification=False,
        )

        return save_workbook_to_bytes(wb), filename


__all__ = [
    "CUSTOMER_LEDGER_NOTE",
    "CUSTOMER_MASTER_NOTE",
    "CUSTOMER_OUTSTANDING_NOTE",
    "EXPENSES_REPORT_NOTE",
    "ExcelExportService",
    "GSTR1_WORKING_PAPER_NOTE",
    "GST_SUMMARY_NOTE",
    "HSN_LIMITATION_NOTE",
    "MAX_EXPORT_ROWS",
    "PAYMENT_HISTORY_NOTE",
    "PNL_RETURN_DISCLOSURE_NOTE",
    "PURCHASE_RECORDED_DUE_NOTE",
    "SALES_RECORDED_DUE_NOTE",
    "STATUTORY_REGISTERS_NOTE",
    "SUPPLIER_LEDGER_NOTE",
    "SUPPLIER_MASTER_NOTE",
    "SUPPLIER_OUTSTANDING_NOTE",
    "normalize_optional_date_filters",
]
