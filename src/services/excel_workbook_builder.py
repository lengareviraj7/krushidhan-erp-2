"""
Reusable Excel Workbook Builder & Styling Utilities for Krushidhan Agri-Input Shop ERP.
- Strictly in-memory generation (io.BytesIO; zero temporary files written to disk)
- English-only commercial reporting layout with dynamic SQLite CompanySettings branding
- Anti-formula-injection string sanitization & '@' text formatting for all text cells
- Backend-calculated numeric totals (no unverified spreadsheet formulas in financial exports)
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

import openpyxl
from openpyxl.cell.cell import Cell, MergedCell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from src.models.system import CompanySettings
from src.services.excel_security import sanitize_excel_text

# ---------------------------------------------------------------------------
# Styling Palette & Reusable OpenPyXL Styles
# ---------------------------------------------------------------------------
CURRENCY_FORMAT_INR = "₹#,##0.00"
QUANTITY_FORMAT_DEFAULT = "#,##0.###"
QUANTITY_FORMAT_2DP = "#,##0.00"
DATE_FORMAT_ISO = "yyyy-mm-dd"
PERCENT_FORMAT_DEFAULT = '0.00"%"'

_SHEET_INVALID_CHARS_RE = re.compile(r"[:\\/?*\[\]]")

_THIN_BORDER_SIDE = Side(style="thin", color="D0D7DE")
_HEADER_BORDER_SIDE = Side(style="thin", color="1B4332")
_TOTAL_TOP_SIDE = Side(style="thin", color="1B4332")
_TOTAL_BOTTOM_SIDE = Side(style="double", color="1B4332")

CELL_BORDER = Border(
    left=_THIN_BORDER_SIDE,
    right=_THIN_BORDER_SIDE,
    top=_THIN_BORDER_SIDE,
    bottom=_THIN_BORDER_SIDE,
)
HEADER_BORDER = Border(
    left=_HEADER_BORDER_SIDE,
    right=_HEADER_BORDER_SIDE,
    top=_HEADER_BORDER_SIDE,
    bottom=_HEADER_BORDER_SIDE,
)
TOTALS_BORDER = Border(
    left=_THIN_BORDER_SIDE,
    right=_THIN_BORDER_SIDE,
    top=_TOTAL_TOP_SIDE,
    bottom=_TOTAL_BOTTOM_SIDE,
)

SHOP_TITLE_FILL = PatternFill(fill_type="solid", fgColor="1B4332")
SHOP_META_FILL = PatternFill(fill_type="solid", fgColor="F1F8F4")
BANNER_FILL = PatternFill(fill_type="solid", fgColor="E8F0FE")
TABLE_HEADER_FILL = PatternFill(fill_type="solid", fgColor="2D6A4F")
ZEBRA_ALT_FILL = PatternFill(fill_type="solid", fgColor="F7FAF8")
WHITE_FILL = PatternFill(fill_type="solid", fgColor="FFFFFF")
TOTALS_FILL = PatternFill(fill_type="solid", fgColor="E9F5EE")

SHOP_TITLE_FONT = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
SHOP_SUBTITLE_FONT = Font(name="Calibri", size=10, bold=False, color="1F2937")
SHOP_LICENCE_FONT = Font(name="Calibri", size=9, bold=True, color="1B4332")
BANNER_TITLE_FONT = Font(name="Calibri", size=11, bold=True, color="0F172A")
BANNER_META_FONT = Font(name="Calibri", size=9, bold=False, color="334155")
TABLE_HEADER_FONT = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Calibri", size=10, bold=False, color="111827")
TOTALS_FONT = Font(name="Calibri", size=10, bold=True, color="0F172A")


def sanitize_sheet_title(title: str, fallback: str = "Report") -> str:
    """Clean worksheet title to comply with Excel's 31-character and forbidden-character rules."""
    cleaned = sanitize_excel_text(title or "").lstrip("'")
    cleaned = _SHEET_INVALID_CHARS_RE.sub(" ", cleaned).strip()
    if not cleaned:
        cleaned = fallback
    return cleaned[:31]


def create_workbook(initial_sheet_title: Optional[str] = None) -> openpyxl.Workbook:
    """Create a fresh openpyxl Workbook cleanly in memory with standardized page setup."""
    wb = openpyxl.Workbook()
    if initial_sheet_title and wb.active is not None:
        wb.active.title = sanitize_sheet_title(initial_sheet_title)
    return wb


def add_sheet(
    wb: openpyxl.Workbook,
    title: str,
    *,
    replace_empty_default: bool = True,
) -> Worksheet:
    """
    Create or reuse an untouched default sheet in a workbook with deduplicated safe sheet title.
    """
    desired = sanitize_sheet_title(title)
    existing_lower = {s.lower() for s in wb.sheetnames}

    # Reuse pristine default sheet only on first creation if requested
    active = wb.active
    if (
        replace_empty_default
        and active is not None
        and len(wb.sheetnames) == 1
        and active.max_row == 1
        and active.max_column == 1
        and active["A1"].value is None
    ):
        active.title = desired
        ws = active
    else:
        unique_name = desired
        suffix = 2
        while unique_name.lower() in existing_lower:
            tail = f"_{suffix}"
            unique_name = f"{desired[: 31 - len(tail)]}{tail}"
            suffix += 1
        ws = wb.create_sheet(title=unique_name)

    ws.views.sheetView[0].showGridLines = True
    return ws


def _extract_company_dict(
    company_settings: Optional[Union[CompanySettings, Mapping[str, Any]]],
) -> Dict[str, Any]:
    if company_settings is None:
        return {"company_name": "Agri-Input Shop", "state": "Maharashtra"}
    if isinstance(company_settings, CompanySettings):
        return company_settings.model_dump()
    return dict(company_settings)


def add_company_header(
    ws: Worksheet,
    company_settings: Optional[Union[CompanySettings, Mapping[str, Any]]] = None,
    *,
    start_row: int = 1,
    col_span: int = 6,
    include_licences: bool = True,
) -> int:
    """
    Render dynamic shop profile header rows from SQLite CompanySettings (never developer defaults,
    never sensitive credentials such as upi_id or bank account numbers).
    Returns the next available row index.
    """
    span = max(1, int(col_span))
    info = _extract_company_dict(company_settings)

    shop_name = sanitize_excel_text(info.get("company_name") or "Agri-Input Shop")
    row = start_row

    # Row 1: Main Shop Title
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=span)
    title_cell = ws.cell(row=row, column=1, value=shop_name)
    title_cell.data_type = "s"
    title_cell.number_format = "@"
    title_cell.font = SHOP_TITLE_FONT
    title_cell.fill = SHOP_TITLE_FILL
    title_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[row].height = 24
    row += 1

    # Row 2: Shop Address & Contact
    addr_parts: List[str] = []
    for key in ("address", "city", "state"):
        v = str(info.get(key) or "").strip()
        if v:
            addr_parts.append(v)
    pin = str(info.get("pincode") or "").strip()
    if pin:
        addr_parts.append(f"PIN: {pin}")

    contact_parts: List[str] = []
    if info.get("owner_name"):
        contact_parts.append(f"Prop: {str(info['owner_name']).strip()}")
    if info.get("mobile"):
        contact_parts.append(f"Mob: {str(info['mobile']).strip()}")
    if info.get("email"):
        contact_parts.append(f"Email: {str(info['email']).strip()}")

    line2_segments: List[str] = []
    if addr_parts:
        line2_segments.append(", ".join(addr_parts))
    if contact_parts:
        line2_segments.append(" | ".join(contact_parts))

    if line2_segments:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=span)
        meta_cell = ws.cell(row=row, column=1, value=sanitize_excel_text(" | ".join(line2_segments)))
        meta_cell.data_type = "s"
        meta_cell.number_format = "@"
        meta_cell.font = SHOP_SUBTITLE_FONT
        meta_cell.fill = SHOP_META_FILL
        meta_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1, wrap_text=True)
        ws.row_dimensions[row].height = 18
        row += 1

    # Row 3: GSTIN & Agri-Input Licences
    stat_parts: List[str] = []
    if info.get("gstin"):
        stat_parts.append(f"GSTIN: {str(info['gstin']).strip().upper()}")
    if include_licences:
        if info.get("dl_fertilizer"):
            stat_parts.append(f"Fertilizer Lic: {str(info['dl_fertilizer']).strip()}")
        if info.get("dl_pesticide"):
            stat_parts.append(f"Pesticide Lic: {str(info['dl_pesticide']).strip()}")
        if info.get("dl_seed"):
            stat_parts.append(f"Seed Lic: {str(info['dl_seed']).strip()}")

    if stat_parts:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=span)
        lic_cell = ws.cell(row=row, column=1, value=sanitize_excel_text(" | ".join(stat_parts)))
        lic_cell.data_type = "s"
        lic_cell.number_format = "@"
        lic_cell.font = SHOP_LICENCE_FONT
        lic_cell.fill = SHOP_META_FILL
        lic_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1, wrap_text=True)
        ws.row_dimensions[row].height = 18
        row += 1

    return row


def add_report_banner(
    ws: Worksheet,
    report_title: str,
    *,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    generated_by: Optional[str] = None,
    extra_meta: Optional[Mapping[str, Any]] = None,
    start_row: int = 1,
    col_span: int = 6,
) -> int:
    """
    Render report title, date-range metadata, and generation metadata banner.
    Returns the next available row index.
    """
    span = max(1, int(col_span))
    row = start_row

    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=span)
    banner_cell = ws.cell(row=row, column=1, value=sanitize_excel_text(report_title))
    banner_cell.data_type = "s"
    banner_cell.number_format = "@"
    banner_cell.font = BANNER_TITLE_FONT
    banner_cell.fill = BANNER_FILL
    banner_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[row].height = 20
    row += 1

    meta_parts: List[str] = []
    if from_date and to_date:
        meta_parts.append(f"Period: {from_date} to {to_date}")
    elif from_date:
        meta_parts.append(f"Date: {from_date}")
    meta_parts.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if generated_by:
        meta_parts.append(f"By: {str(generated_by).strip()}")
    if extra_meta:
        for k, v in extra_meta.items():
            if v is not None and str(v).strip():
                meta_parts.append(f"{k}: {str(v).strip()}")

    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=span)
    meta_cell = ws.cell(row=row, column=1, value=sanitize_excel_text(" | ".join(meta_parts)))
    meta_cell.data_type = "s"
    meta_cell.number_format = "@"
    meta_cell.font = BANNER_META_FONT
    meta_cell.fill = BANNER_FILL
    meta_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[row].height = 17
    row += 1

    return row


def write_table_header(
    ws: Worksheet,
    headers: Sequence[str],
    *,
    row_idx: int = 1,
    start_col: int = 1,
) -> int:
    """Write formatted table column header row and return the next row index."""
    for offset, header in enumerate(headers):
        col_idx = start_col + offset
        cell = ws.cell(row=row_idx, column=col_idx, value=sanitize_excel_text(header))
        cell.data_type = "s"
        cell.number_format = "@"
        cell.font = TABLE_HEADER_FONT
        cell.fill = TABLE_HEADER_FILL
        cell.border = HEADER_BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[row_idx].height = 22
    return row_idx + 1


def write_text_cell(
    ws: Worksheet,
    row: int,
    col: int,
    value: Any,
    *,
    alignment: Optional[Alignment] = None,
    max_length: Optional[int] = None,
    font: Optional[Font] = None,
) -> Cell:
    """Write sanitized string text explicitly as text ('@' format) to block Excel formula execution."""
    safe_val = sanitize_excel_text(value, max_length=max_length)
    cell = ws.cell(row=row, column=col, value=safe_val)
    cell.data_type = "s"
    cell.number_format = "@"
    cell.font = font or BODY_FONT
    cell.border = CELL_BORDER
    cell.alignment = alignment or Alignment(horizontal="left", vertical="center")
    return cell


def _to_rounded_decimal(value: Any, decimals: int) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        dec = Decimal(str(value).strip()) if isinstance(value, str) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Cannot convert '{value}' to numeric cell value.") from exc
    if not dec.is_finite():
        raise ValueError(f"Non-finite numeric value '{value}' cannot be exported.")
    quant = Decimal("1") if decimals <= 0 else Decimal(f"1e-{decimals}")
    return dec.quantize(quant, rounding=ROUND_HALF_UP)


def write_currency_cell(
    ws: Worksheet,
    row: int,
    col: int,
    value: Any,
    *,
    number_format: str = CURRENCY_FORMAT_INR,
    font: Optional[Font] = None,
) -> Cell:
    """Write a true numeric INR monetary value (2 decimals) with ₹#,##0.00 display formatting."""
    dec_val = _to_rounded_decimal(value, 2)
    cell = ws.cell(row=row, column=col, value=float(dec_val))
    cell.data_type = "n"
    cell.number_format = number_format
    cell.font = font or BODY_FONT
    cell.border = CELL_BORDER
    cell.alignment = Alignment(horizontal="right", vertical="center")
    return cell


def write_quantity_cell(
    ws: Worksheet,
    row: int,
    col: int,
    value: Any,
    *,
    decimals: int = 3,
    number_format: Optional[str] = None,
    font: Optional[Font] = None,
) -> Cell:
    """Write a true numeric inventory/billing quantity cell."""
    dec_val = _to_rounded_decimal(value, decimals)
    if dec_val == dec_val.to_integral_value():
        num_val: Union[int, float] = int(dec_val)
    else:
        num_val = float(dec_val)
    cell = ws.cell(row=row, column=col, value=num_val)
    cell.data_type = "n"
    cell.number_format = number_format or (
        QUANTITY_FORMAT_2DP if decimals == 2 else QUANTITY_FORMAT_DEFAULT
    )
    cell.font = font or BODY_FONT
    cell.border = CELL_BORDER
    cell.alignment = Alignment(horizontal="right", vertical="center")
    return cell


def write_percentage_cell(
    ws: Worksheet,
    row: int,
    col: int,
    value: Any,
    *,
    decimals: int = 2,
    font: Optional[Font] = None,
) -> Cell:
    """Write a numeric percentage cell (e.g., 18.0 -> '18.00%')."""
    dec_val = _to_rounded_decimal(value, decimals)
    cell = ws.cell(row=row, column=col, value=float(dec_val))
    cell.data_type = "n"
    cell.number_format = PERCENT_FORMAT_DEFAULT
    cell.font = font or BODY_FONT
    cell.border = CELL_BORDER
    cell.alignment = Alignment(horizontal="right", vertical="center")
    return cell


def write_date_cell(
    ws: Worksheet,
    row: int,
    col: int,
    value: Any,
    *,
    date_format: str = DATE_FORMAT_ISO,
    font: Optional[Font] = None,
) -> Cell:
    """Write an Excel native date value when valid ISO/date input is provided, or safe text fallback."""
    parsed_date: Optional[date] = None
    if isinstance(value, datetime):
        parsed_date = value.date()
    elif isinstance(value, date):
        parsed_date = value
    elif isinstance(value, str) and value.strip():
        candidate = value.strip()[:10]
        try:
            parsed_date = date.fromisoformat(candidate)
        except ValueError:
            parsed_date = None

    if parsed_date is not None:
        cell = ws.cell(row=row, column=col, value=parsed_date)
        cell.number_format = date_format
        cell.font = font or BODY_FONT
        cell.border = CELL_BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center")
        return cell

    return write_text_cell(
        ws,
        row,
        col,
        value or "",
        alignment=Alignment(horizontal="center", vertical="center"),
        font=font,
    )


def write_totals_row(
    ws: Worksheet,
    *,
    row_idx: int,
    col_totals: Mapping[int, Any],
    num_cols: int,
    label: str = "TOTAL",
    label_col: int = 1,
    start_col: int = 1,
    currency_cols: Optional[Sequence[int]] = None,
    quantity_cols: Optional[Sequence[int]] = None,
) -> int:
    """
    Write a formatted summary totals row using backend-calculated numeric values ONLY
    (zero Excel formula strings like =SUM(...) to eliminate formula injection/recalculation issues).
    Returns next row index.
    """
    currency_set = set(currency_cols or ())
    qty_set = set(quantity_cols or ())
    end_col = start_col + max(1, int(num_cols)) - 1

    for col_idx in range(start_col, end_col + 1):
        if col_idx == label_col:
            cell = write_text_cell(ws, row_idx, col_idx, label, font=TOTALS_FONT)
        elif col_idx in col_totals:
            raw_total = col_totals[col_idx]
            if col_idx in qty_set and col_idx not in currency_set:
                cell = write_quantity_cell(ws, row_idx, col_idx, raw_total, font=TOTALS_FONT)
            else:
                cell = write_currency_cell(ws, row_idx, col_idx, raw_total, font=TOTALS_FONT)
        else:
            cell = ws.cell(row=row_idx, column=col_idx, value="")
            cell.data_type = "s"
            cell.number_format = "@"
            cell.font = TOTALS_FONT
        cell.fill = TOTALS_FILL
        cell.border = TOTALS_BORDER

    ws.row_dimensions[row_idx].height = 21
    return row_idx + 1


def apply_alternating_row_styles(
    ws: Worksheet,
    *,
    start_row: int,
    end_row: int,
    start_col: int = 1,
    end_col: int,
) -> None:
    """Apply crisp alternating zebra shading and borders cleanly over body rows."""
    if end_row < start_row:
        return
    for r_idx in range(start_row, end_row + 1):
        fill = ZEBRA_ALT_FILL if ((r_idx - start_row) % 2 == 1) else WHITE_FILL
        ws.row_dimensions[r_idx].height = ws.row_dimensions[r_idx].height or 19
        for c_idx in range(start_col, end_col + 1):
            cell = ws.cell(row=r_idx, column=c_idx)
            cell.fill = fill
            cell.border = CELL_BORDER


def apply_freeze_panes(ws: Worksheet, *, header_row: int, freeze_col: int = 1) -> None:
    """Freeze header rows (and optional leading columns) below header_row."""
    ws.freeze_panes = f"{get_column_letter(max(1, freeze_col))}{max(1, header_row) + 1}"


def apply_auto_filter(
    ws: Worksheet,
    *,
    header_row: int,
    last_row: int,
    start_col: int = 1,
    end_col: int,
) -> None:
    """Apply Excel AutoFilter dropdowns covering table headers down through last_row."""
    safe_last = max(header_row, last_row)
    safe_end_col = max(start_col, end_col)
    ws.auto_filter.ref = (
        f"{get_column_letter(start_col)}{header_row}:"
        f"{get_column_letter(safe_end_col)}{safe_last}"
    )


def auto_fit_columns(
    ws: Worksheet,
    *,
    min_width: int = 10,
    max_width: int = 45,
    padding: int = 3,
    exclude_merged: bool = True,
) -> None:
    """Compute column widths safely across populated cells (skipping wide merged banner rows)."""
    widths: Dict[int, int] = {}
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell, MergedCell):
                continue
            if exclude_merged:
                is_merged_anchor = any(
                    cell.coordinate == f"{get_column_letter(rng.min_col)}{rng.min_row}"
                    for rng in ws.merged_cells.ranges
                )
                if is_merged_anchor:
                    continue

            val = cell.value
            if val is None:
                length = 0
            elif isinstance(val, (date, datetime)):
                length = 12
            elif isinstance(val, (int, float, Decimal)):
                length = len(f"{val:,.2f}") + 2
            else:
                lines = str(val).splitlines() or [""]
                length = max(len(line) for line in lines)

            col_idx = cell.column
            widths[col_idx] = max(widths.get(col_idx, 0), length + padding)

    for col_idx, raw_w in widths.items():
        bounded = max(min_width, min(max_width, raw_w))
        ws.column_dimensions[get_column_letter(col_idx)].width = bounded


def save_workbook_to_bytes(wb: openpyxl.Workbook) -> bytes:
    """Serialize an openpyxl Workbook strictly in memory via BytesIO and close it safely."""
    buf = io.BytesIO()
    try:
        wb.save(buf)
        return buf.getvalue()
    finally:
        try:
            wb.close()
        except Exception:
            pass


__all__ = [
    "BANNER_FILL",
    "CURRENCY_FORMAT_INR",
    "DATE_FORMAT_ISO",
    "PERCENT_FORMAT_DEFAULT",
    "QUANTITY_FORMAT_2DP",
    "QUANTITY_FORMAT_DEFAULT",
    "SHOP_TITLE_FILL",
    "TABLE_HEADER_FILL",
    "TOTALS_FILL",
    "ZEBRA_ALT_FILL",
    "add_company_header",
    "add_report_banner",
    "add_sheet",
    "apply_alternating_row_styles",
    "apply_auto_filter",
    "apply_freeze_panes",
    "auto_fit_columns",
    "create_workbook",
    "sanitize_sheet_title",
    "save_workbook_to_bytes",
    "write_currency_cell",
    "write_date_cell",
    "write_percentage_cell",
    "write_quantity_cell",
    "write_table_header",
    "write_text_cell",
    "write_totals_row",
]
