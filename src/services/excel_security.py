"""
Excel Security Guard, Safe OOXML Loader, Formula-Injection Defense & Domain Validators.
Designed for offline-first Krushidhan Agri-Input Shop ERP (Python 3.9 + openpyxl + defusedxml).
All workbook processing runs in memory; no temporary files are written to disk.
"""
from __future__ import annotations

import contextlib
import io
import posixpath
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Generator, List, Optional, Sequence, Tuple, Union

import defusedxml
import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.xml import DEFUSEDXML as OPENPYXL_DEFUSEDXML
from openpyxl.xml import functions as openpyxl_xml_functions

from src.models.system import normalize_and_validate_email

# ---------------------------------------------------------------------------
# Security & Capacity Constants
# ---------------------------------------------------------------------------
ALLOWED_XLSX_EXTENSIONS = frozenset({".xlsx"})
FORBIDDEN_SPREADSHEET_EXTENSIONS = frozenset(
    {".xlsm", ".xlsb", ".xls", ".xltm", ".xlam", ".csv", ".tsv", ".ods"}
)

ZIP_MAGIC = b"PK\x03\x04"
OLE_CFBF_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

MAX_UPLOAD_BYTES = 5 * 1024 * 1024        # 5 MB
MAX_ZIP_ENTRIES = 250
MAX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024 # 25 MB
MAX_COMPRESSION_RATIO = 50.0
MIN_RATIO_CHECK_BYTES = 1024

MAX_DATA_ROWS = 2000
MAX_COLUMNS = 30
MAX_CELL_LENGTH = 500
MAX_SHEET_SCAN_ROWS = MAX_DATA_ROWS + 50

FORMULA_PREFIX_CHARS = ("=", "+", "-", "@", "\t", "\r")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LEADING_STRIP_CHARS = " \t\r\n\v\f\x00\ufeff\u200b\u00a0"

_XXE_DECL_RE = re.compile(rb"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_EXTERNAL_REL_RE = re.compile(rb'TargetMode\s*=\s*["\']External["\']', re.IGNORECASE)
_MACRO_CONTENT_TYPE_RE = re.compile(
    rb"(?:macroEnabled|vbaProject|oleObject|activeX)", re.IGNORECASE
)

_ENCRYPTED_ENTRY_NAMES = frozenset(
    {
        "encryptioninfo",
        "encryptedpackage",
        "dataspaceinfo",
    }
)
_DANGEROUS_ARCHIVE_PATH_FRAGMENTS = (
    "vbaproject.bin",
    "vbadata.xml",
    "xl/activex/",
    "xl/embeddings/",
    "xl/externallinks/",
    "/activex/",
    "/embeddings/",
    "/externallinks/",
)
_DANGEROUS_MEMBER_EXTENSIONS = (
    ".bin",
    ".exe",
    ".dll",
    ".bat",
    ".cmd",
    ".ps1",
    ".vbs",
    ".vbe",
    ".js",
    ".jse",
    ".wsf",
    ".lnk",
    ".scr",
)

# Data-protection policy sets enforced across C3 import/export flows
SENSITIVE_NEVER_EXPORT_FIELDS = frozenset(
    {
        "password_hash",
        "jwt_secret",
        "upi_id",
        "aadhar_no",
        "account_no",
        "pan",
    }
)
FORBIDDEN_IMPORT_HEADERS = frozenset(
    {
        "password",
        "password_hash",
        "jwt_secret",
        "aadhar_no",
        "aadhaar",
        "aadhaar_no",
        "opening_balance",
        "current_balance",
        "sale_id",
        "purchase_id",
        "voucher_id",
        "setting_id",
    }
)

# Domain validation regular expressions
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GSTIN_RE = re.compile(
    r"^(0[1-9]|[12][0-9]|3[0-8]|97|99)[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
)
_HSN_RE = re.compile(r"^(?:\d{4}|\d{6}|\d{8})$")
_INDIAN_MOBILE_RE = re.compile(r"^[6-9]\d{9}$")
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


class ExcelSecurityError(ValueError):
    """Raised when an uploaded workbook fails security, archive, XML, or formula checks."""


class ExcelValidationError(ValueError):
    """Raised when input parameters, dates, identifiers, or amounts fail validation."""

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        row: Optional[int] = None,
        column: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.field = field
        self.row = row
        self.column = column


def ensure_defusedxml_active() -> None:
    """Verify openpyxl is operating with defusedxml XML bomb/XXE protection active."""
    if not getattr(defusedxml, "__version__", None):
        raise ExcelSecurityError("defusedxml library is not available; unsafe XML parsing blocked.")
    if not OPENPYXL_DEFUSEDXML:
        raise ExcelSecurityError(
            "openpyxl XML security protection (DEFUSEDXML) is disabled; refusing workbook parse."
        )
    fromstring_fn = getattr(openpyxl_xml_functions, "fromstring", None)
    fn_module = getattr(fromstring_fn, "__module__", "") if fromstring_fn is not None else ""
    if not fn_module.startswith("defusedxml"):
        raise ExcelSecurityError(
            f"openpyxl XML parser is not routed through defusedxml (got module '{fn_module}')."
        )


def validate_filename_extension(filename: Optional[str]) -> str:
    """Validate uploaded filename ends cleanly with .xlsx (blocks .xlsm, .xlsb, .csv, paths)."""
    if not filename or not isinstance(filename, str):
        raise ExcelSecurityError("Workbook filename is required and must use the .xlsx extension.")

    cleaned = filename.strip()
    if not cleaned or "\x00" in cleaned:
        raise ExcelSecurityError("Invalid workbook filename.")
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise ExcelSecurityError("Workbook filename must not contain path separators or traversal segments.")

    dot_idx = cleaned.rfind(".")
    ext = cleaned[dot_idx:].lower() if dot_idx != -1 else ""
    if ext in FORBIDDEN_SPREADSHEET_EXTENSIONS:
        raise ExcelSecurityError(
            f"Unsupported spreadsheet format '{ext}'. Only strict .xlsx workbooks are allowed."
        )
    if ext not in ALLOWED_XLSX_EXTENSIONS:
        raise ExcelSecurityError("Invalid file type. Only .xlsx files are permitted.")
    return cleaned


def _is_unsafe_zip_member_path(member_name: str) -> bool:
    """Return True if a ZIP entry name attempts directory traversal or absolute/UNC paths."""
    if not member_name or "\x00" in member_name:
        return True
    if "\\" in member_name or member_name.startswith("/"):
        return True
    if re.match(r"^[A-Za-z]:", member_name):
        return True
    parts = member_name.split("/")
    if any(part in ("..", "") and idx == 0 for idx, part in enumerate(parts)):
        # Leading slash caught above, catch leading '..' too
        if parts[0] == "..":
            return True
    if ".." in parts:
        return True
    normalized = posixpath.normpath(member_name.rstrip("/"))
    if normalized in ("..", ".") or normalized.startswith("../") or normalized.startswith("/"):
        return True
    return False


def validate_xlsx_bytes(data: bytes, filename: Optional[str] = None) -> None:
    """
    Independently inspect a candidate .xlsx payload via ZipFile BEFORE openpyxl touches it:
    - Enforces extension (.xlsx) if filename provided
    - Enforces max upload size (5 MB) and ZIP magic bytes (PK\\x03\\x04)
    - Blocks encrypted OOXML containers and encrypted ZIP flags
    - Blocks directory traversal / absolute ZIP member paths
    - Blocks ZIP bombs (>250 entries, >25 MB uncompressed declared or streamed, >50:1 ratio)
    - Blocks VBA macros (.bin / vbaProject), OLE embeddings, ActiveX, and external links
    - Blocks <!DOCTYPE / <!ENTITY XXE declarations in XML and .rels members
    - Verifies required OOXML core files ([Content_Types].xml and xl/workbook.xml)
    """
    ensure_defusedxml_active()

    if filename is not None:
        validate_filename_extension(filename)

    if not isinstance(data, (bytes, bytearray)):
        raise ExcelSecurityError("Workbook payload must be raw bytes.")
    raw = bytes(data)
    if not raw:
        raise ExcelSecurityError("Uploaded .xlsx file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ExcelSecurityError(
            f"Uploaded file exceeds maximum allowed size of {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
    if raw[: len(OLE_CFBF_MAGIC)] == OLE_CFBF_MAGIC:
        raise ExcelSecurityError(
            "Encrypted OOXML or legacy binary .xls (OLE) container rejected; upload an unencrypted .xlsx file."
        )
    if not raw.startswith(ZIP_MAGIC):
        raise ExcelSecurityError("Invalid .xlsx file signature (expected standard OOXML ZIP header).")

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw), mode="r")
    except zipfile.BadZipFile as exc:
        raise ExcelSecurityError("Corrupted or invalid .xlsx ZIP archive.") from exc

    with zf:
        infolist = zf.infolist()
        if not infolist:
            raise ExcelSecurityError("Empty .xlsx ZIP archive.")
        if len(infolist) > MAX_ZIP_ENTRIES:
            raise ExcelSecurityError(
                f"Archive contains too many entries ({len(infolist)} > {MAX_ZIP_ENTRIES}); potential ZIP bomb."
            )

        seen_names_lower = set()
        total_declared_uncompressed = 0
        total_actual_uncompressed = 0
        total_compressed = 0

        for info in infolist:
            name = info.filename
            if _is_unsafe_zip_member_path(name):
                raise ExcelSecurityError(f"Unsafe ZIP member path detected: '{name}'.")

            if info.flag_bits & 0x1:
                raise ExcelSecurityError("Encrypted ZIP workbooks are not permitted.")

            lower_name = name.lower()
            seen_names_lower.add(lower_name)

            base_lower = posixpath.basename(lower_name.rstrip("/"))
            if base_lower in _ENCRYPTED_ENTRY_NAMES or "encryption" in base_lower:
                raise ExcelSecurityError("Password-protected / encrypted Excel workbooks are not permitted.")

            if any(frag in lower_name for frag in _DANGEROUS_ARCHIVE_PATH_FRAGMENTS):
                raise ExcelSecurityError(
                    f"Blocked unsafe workbook component '{name}' (macros, ActiveX, OLE, or external links)."
                )

            if not info.is_dir() and lower_name.endswith(_DANGEROUS_MEMBER_EXTENSIONS):
                raise ExcelSecurityError(f"Blocked potentially dangerous binary archive member '{name}'.")

            if info.is_dir():
                continue

            total_declared_uncompressed += max(info.file_size, 0)
            if total_declared_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise ExcelSecurityError(
                    f"Archive uncompressed size exceeds {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB limit."
                )

            total_compressed += max(info.compress_size, 0)
            if info.file_size > 0 and info.compress_size == 0:
                raise ExcelSecurityError(
                    f"Malformed compressed size for ZIP entry '{name}'; potential ZIP bomb."
                )
            if info.file_size > MIN_RATIO_CHECK_BYTES and info.compress_size > 0:
                declared_ratio = info.file_size / float(info.compress_size)
                if declared_ratio > MAX_COMPRESSION_RATIO:
                    raise ExcelSecurityError(
                        f"Excessive ZIP compression ratio ({declared_ratio:.1f}:1) in '{name}'; potential ZIP bomb."
                    )

            is_xml_like = lower_name.endswith((".xml", ".rels"))
            prev_tail = b""
            entry_actual_size = 0
            xml_scan_buffer = bytearray()

            try:
                with zf.open(info, mode="r") as member_stream:
                    while True:
                        chunk = member_stream.read(65536)
                        if not chunk:
                            break
                        entry_actual_size += len(chunk)
                        total_actual_uncompressed += len(chunk)

                        if total_actual_uncompressed > MAX_UNCOMPRESSED_BYTES:
                            raise ExcelSecurityError(
                                f"Decompressed workbook content exceeds {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB limit."
                            )
                        if entry_actual_size > MIN_RATIO_CHECK_BYTES and info.compress_size > 0:
                            actual_ratio = entry_actual_size / float(info.compress_size)
                            if actual_ratio > MAX_COMPRESSION_RATIO:
                                raise ExcelSecurityError(
                                    f"Decompressed compression ratio ({actual_ratio:.1f}:1) exceeded limit in '{name}'."
                                )

                        if is_xml_like:
                            window = prev_tail + chunk
                            if _XXE_DECL_RE.search(window):
                                raise ExcelSecurityError(
                                    f"Disallowed XML DOCTYPE/ENTITY declaration detected in '{name}'."
                                )
                            if lower_name.endswith(".rels") and _EXTERNAL_REL_RE.search(window):
                                raise ExcelSecurityError(
                                    f"External OOXML relationship link blocked in '{name}'."
                                )
                            if lower_name == "[content_types].xml" and len(xml_scan_buffer) < 512 * 1024:
                                xml_scan_buffer.extend(chunk)
                            prev_tail = chunk[-64:]
            except RuntimeError as exc:
                raise ExcelSecurityError("Encrypted or unreadable .xlsx ZIP entry rejected.") from exc
            except zipfile.BadZipFile as exc:
                raise ExcelSecurityError(f"Corrupted ZIP stream in member '{name}'.") from exc

            if lower_name == "[content_types].xml" and _MACRO_CONTENT_TYPE_RE.search(bytes(xml_scan_buffer)):
                raise ExcelSecurityError(
                    "Macro-enabled, ActiveX, or embedded OLE content type blocked in [Content_Types].xml."
                )

        if total_actual_uncompressed > 4096 and total_compressed > 0:
            overall_ratio = total_actual_uncompressed / float(total_compressed)
            if overall_ratio > MAX_COMPRESSION_RATIO:
                raise ExcelSecurityError(
                    f"Archive overall compression ratio ({overall_ratio:.1f}:1) exceeds safe limit."
                )

        if "[content_types].xml" not in seen_names_lower or "xl/workbook.xml" not in seen_names_lower:
            raise ExcelSecurityError("Missing required OOXML workbook structure ([Content_Types].xml or xl/workbook.xml).")


def is_formula_like_string(text: str) -> bool:
    """
    Detect spreadsheet formula or DDE/macro-like text payloads on import,
    including hidden leading whitespace or control characters (e.g., ' =1+1', '\\t=cmd|...').
    """
    if not text:
        return False
    if text[0] in FORMULA_PREFIX_CHARS:
        return True
    stripped = text.lstrip(_LEADING_STRIP_CHARS)
    if not stripped:
        return False
    if stripped[0] in FORMULA_PREFIX_CHARS:
        return True
    return False


def validate_import_cell_value(
    cell_value: Any,
    *,
    data_type: Optional[str] = None,
    row: Optional[int] = None,
    col_index: Optional[int] = None,
    max_length: int = MAX_CELL_LENGTH,
) -> Any:
    """
    Validate an individual cell read from an imported workbook with data_only=False:
    - Rejects formula cells (data_type == 'f') and formula errors ('e')
    - Rejects control characters and cell text > max_length (default 500)
    - Rejects formula-like strings ('=', '+', '-', '@', '\\t', '\\r', or leading whitespace before them)
    """
    coord = f"{get_column_letter(col_index)}{row}" if (row and col_index) else None
    loc_label = f" at cell {coord}" if coord else ""

    if data_type == "f":
        raise ExcelSecurityError(f"Spreadsheet formulas are not allowed in import files{loc_label}.")
    if data_type == "e":
        raise ExcelValidationError(
            f"Spreadsheet formula error value '{cell_value}' is not allowed{loc_label}.",
            row=row,
            column=get_column_letter(col_index) if col_index else None,
        )

    if isinstance(cell_value, str):
        if len(cell_value) > max_length:
            raise ExcelValidationError(
                f"Cell text length ({len(cell_value)}) exceeds {max_length}-character limit{loc_label}.",
                row=row,
                column=get_column_letter(col_index) if col_index else None,
            )
        if _CONTROL_CHAR_RE.search(cell_value):
            raise ExcelSecurityError(f"Control characters are not allowed in import cell{loc_label}.")
        if is_formula_like_string(cell_value):
            raise ExcelSecurityError(f"Potentially dangerous formula-like text rejected{loc_label}.")

    return cell_value


def sanitize_excel_text(value: Any, *, max_length: Optional[int] = None) -> str:
    """
    Sanitize user-supplied text for safe Excel export:
    - Strips ASCII C0 control characters (keeps standard newline '\\n' cleanly)
    - Escapes formula-injection triggers ('=', '+', '-', '@', '\\t', '\\r', including
      leading-whitespace variants such as '  =SUM(A1)') by prefixing a literal apostrophe (').
    """
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\r\n", "\n")
    text = _CONTROL_CHAR_RE.sub("", text)

    if text and (
        text[0] in ("=", "+", "-", "@", "\t", "\r")
        or text.lstrip(_LEADING_STRIP_CHARS).startswith(("=", "+", "-", "@"))
    ):
        text = "'" + text

    if max_length is not None and len(text) > max_length:
        text = text[:max_length]
    return text


def assert_headers_allowed_for_import(headers: Sequence[str]) -> None:
    """Reject forbidden sensitive/audit/historical-balance columns in import templates."""
    for idx, raw_header in enumerate(headers, start=1):
        norm = str(raw_header or "").strip().lower().replace(" ", "_")
        if norm in FORBIDDEN_IMPORT_HEADERS:
            raise ExcelSecurityError(
                f"Column '{raw_header}' (column {idx}) is protected and cannot be imported via Excel."
            )


@contextlib.contextmanager
def safe_xlsx_workbook(
    data: bytes,
    filename: Optional[str] = None,
    *,
    read_only: bool = True,
) -> Generator[openpyxl.Workbook, None, None]:
    """
    Validate and safely open an uploaded .xlsx byte stream in memory with:
    - Full ZIP/XXE/macro/encryption/ratio pre-checks
    - data_only=False (so formulas remain detectable as data_type == 'f')
    - Deterministic wb.close() on exit
    """
    validate_xlsx_bytes(data, filename=filename)
    try:
        wb = openpyxl.load_workbook(
            io.BytesIO(bytes(data)),
            read_only=read_only,
            data_only=False,
        )
    except ExcelSecurityError:
        raise
    except Exception as exc:
        raise ExcelSecurityError(f"Unable to parse .xlsx workbook safely: {exc}") from exc

    try:
        yield wb
    finally:
        try:
            wb.close()
        except Exception:
            pass


def read_xlsx_sheet_safely(
    data: bytes,
    filename: Optional[str] = None,
    *,
    sheet_name: Optional[str] = None,
    header_rows: int = 1,
    max_data_rows: int = MAX_DATA_ROWS,
    max_columns: int = MAX_COLUMNS,
    max_cell_length: int = MAX_CELL_LENGTH,
    validate_all_sheets: bool = True,
) -> Dict[str, Any]:
    """
    Validate workbook container security, formulas, row/column limits, and cell lengths,
    and extract normalized header + data rows from the target worksheet using data_only=False.
    """
    if header_rows < 0:
        raise ValueError("header_rows must be non-negative.")

    with safe_xlsx_workbook(data, filename=filename, read_only=True) as wb:
        sheetnames = list(wb.sheetnames or [])
        if not sheetnames:
            raise ExcelValidationError("Workbook contains no worksheets.")

        target_name = sheet_name or sheetnames[0]
        if target_name not in sheetnames:
            raise ExcelValidationError(f"Requested worksheet '{target_name}' was not found in workbook.")

        sheets_to_validate = sheetnames if validate_all_sheets else [target_name]
        extracted_target: Optional[Dict[str, Any]] = None

        for s_name in sheets_to_validate:
            ws = wb[s_name]
            headers: List[str] = []
            data_rows: List[List[Any]] = []
            non_empty_data_count = 0
            max_populated_col = 0
            sheet_raw_rows: List[Tuple[int, List[Any]]] = []

            for phys_row_idx, row_cells in enumerate(ws.iter_rows(values_only=False), start=1):
                if phys_row_idx > header_rows + MAX_SHEET_SCAN_ROWS:
                    # Peek whether remaining rows are empty; any non-empty row beyond cap fails below
                    has_any = any(
                        c.value is not None or c.data_type in ("f", "e") for c in row_cells
                    )
                    if has_any:
                        raise ExcelValidationError(
                            f"Worksheet '{s_name}' exceeds maximum supported {max_data_rows} data rows.",
                            row=phys_row_idx,
                        )
                    continue

                row_values: List[Any] = []
                row_has_content = False
                row_max_non_empty_col = 0

                for col_idx, cell in enumerate(row_cells, start=1):
                    cell_dtype = getattr(cell, "data_type", "n")
                    cell_val = getattr(cell, "value", None)
                    is_populated = (cell_val is not None) or (cell_dtype in ("f", "e"))

                    if is_populated:
                        if col_idx > max_columns:
                            raise ExcelValidationError(
                                f"Worksheet '{s_name}' exceeds maximum allowed {max_columns} columns "
                                f"(found content at column {col_idx}).",
                                row=phys_row_idx,
                                column=get_column_letter(col_idx),
                            )
                        validate_import_cell_value(
                            cell_val,
                            data_type=cell_dtype,
                            row=phys_row_idx,
                            col_index=col_idx,
                            max_length=max_cell_length,
                        )
                        row_has_content = True
                        row_max_non_empty_col = col_idx

                    if col_idx <= max_columns:
                        row_values.append(cell_val)

                if not row_has_content:
                    continue

                actual_row_num = next(
                    (
                        getattr(c, "row", None)
                        for c in row_cells
                        if getattr(c, "row", None) is not None
                    ),
                    phys_row_idx,
                )

                max_populated_col = max(max_populated_col, row_max_non_empty_col)
                if actual_row_num > header_rows:
                    non_empty_data_count += 1
                    if (
                        non_empty_data_count > max_data_rows
                        or (actual_row_num - header_rows) > max_data_rows
                    ):
                        raise ExcelValidationError(
                            f"Worksheet '{s_name}' exceeds maximum allowed {max_data_rows} data rows.",
                            row=actual_row_num,
                        )

                sheet_raw_rows.append((actual_row_num, row_values))

            width = max(max_populated_col, 0)
            trimmed_rows: List[Tuple[int, List[Any]]] = [
                (r_no, vals[:width]) for r_no, vals in sheet_raw_rows
            ]

            if s_name == target_name:
                if header_rows > 0:
                    header_entries = [r for r in trimmed_rows if r[0] == header_rows]
                    if header_entries:
                        headers = [
                            str(v).strip() if v is not None else ""
                            for v in header_entries[0][1]
                        ]
                    data_entries = [r for r in trimmed_rows if r[0] > header_rows]
                    data_rows = [vals for _, vals in data_entries]
                    data_row_numbers = [r_no for r_no, _ in data_entries]
                else:
                    data_rows = [vals for _, vals in trimmed_rows]
                    data_row_numbers = [r_no for r_no, _ in trimmed_rows]

                extracted_target = {
                    "sheet_name": s_name,
                    "headers": headers,
                    "rows": data_rows,
                    "row_numbers": data_row_numbers,
                    "row_count": len(data_rows),
                    "column_count": width,
                }

        assert extracted_target is not None
        return extracted_target


# ---------------------------------------------------------------------------
# Task 4: Date, Indian Identifier & Financial Validators
# ---------------------------------------------------------------------------
def validate_date_str(value: Any, *, field_name: str = "date") -> str:
    """Validate strict YYYY-MM-DD calendar date string."""
    if not isinstance(value, str):
        raise ExcelValidationError(
            f"Invalid {field_name}: expected YYYY-MM-DD string.",
            field=field_name,
        )
    cleaned = value.strip()
    if not _ISO_DATE_RE.match(cleaned):
        raise ExcelValidationError(
            f"Invalid {field_name} '{value}'. Expected YYYY-MM-DD format.",
            field=field_name,
        )
    try:
        date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ExcelValidationError(
            f"Invalid calendar {field_name} '{value}': {exc}",
            field=field_name,
        ) from exc
    return cleaned


def validate_date_range(from_date: str, to_date: str) -> Tuple[str, str]:
    """Validate YYYY-MM-DD from_date and to_date and ensure from_date <= to_date."""
    norm_from = validate_date_str(from_date, field_name="from_date")
    norm_to = validate_date_str(to_date, field_name="to_date")
    if norm_from > norm_to:
        raise ExcelValidationError(
            f"Invalid date range: from_date ({norm_from}) cannot be after to_date ({norm_to}).",
            field="from_date",
        )
    return norm_from, norm_to


def validate_gstin(
    value: Any, *, required: bool = False, field_name: str = "gstin"
) -> Optional[str]:
    """Validate 15-character uppercase Indian GSTIN format with valid GST state code."""
    if value is None:
        if required:
            raise ExcelValidationError(f"{field_name} is required.", field=field_name)
        return None
    if isinstance(value, (bool, int, float)):
        raise ExcelValidationError(f"Invalid {field_name} format.", field=field_name)

    cleaned = str(value).strip().upper()
    if not cleaned:
        if required:
            raise ExcelValidationError(f"{field_name} cannot be empty.", field=field_name)
        return None

    if not _GSTIN_RE.match(cleaned):
        raise ExcelValidationError(
            f"Invalid {field_name} '{value}'. Expected 15-character Indian GSTIN (e.g. 27AABCU9603R1ZM).",
            field=field_name,
        )
    return cleaned


def validate_hsn(
    value: Any, *, required: bool = False, field_name: str = "hsn_code"
) -> Optional[str]:
    """Validate Indian GST HSN code (strictly 4, 6, or 8 digits)."""
    if value is None:
        if required:
            raise ExcelValidationError(f"{field_name} is required.", field=field_name)
        return None
    if isinstance(value, bool):
        raise ExcelValidationError(f"Invalid {field_name} value.", field=field_name)
    if isinstance(value, float):
        if not value.is_integer() or value < 0:
            raise ExcelValidationError(
                f"Invalid {field_name} '{value}'. HSN must be a 4, 6, or 8 digit code.",
                field=field_name,
            )
        cleaned = str(int(value))
    elif isinstance(value, int):
        if value < 0:
            raise ExcelValidationError(
                f"Invalid {field_name} '{value}'. HSN must be a 4, 6, or 8 digit code.",
                field=field_name,
            )
        cleaned = str(value)
    else:
        cleaned = str(value).strip()

    if not cleaned:
        if required:
            raise ExcelValidationError(f"{field_name} cannot be empty.", field=field_name)
        return None

    if not _HSN_RE.match(cleaned):
        raise ExcelValidationError(
            f"Invalid {field_name} '{value}'. HSN code must be 4, 6, or 8 numeric digits.",
            field=field_name,
        )
    return cleaned


def validate_indian_mobile(
    value: Any, *, required: bool = False, field_name: str = "mobile"
) -> Optional[str]:
    """Validate 10-digit Indian mobile number starting with digits 6-9."""
    if value is None:
        if required:
            raise ExcelValidationError(f"{field_name} is required.", field=field_name)
        return None
    if isinstance(value, bool):
        raise ExcelValidationError(f"Invalid {field_name} value.", field=field_name)
    if isinstance(value, float):
        if not value.is_integer() or value < 0:
            raise ExcelValidationError(
                f"Invalid {field_name} '{value}'. Expected 10-digit Indian mobile number.",
                field=field_name,
            )
        cleaned = str(int(value))
    elif isinstance(value, int):
        if value < 0:
            raise ExcelValidationError(
                f"Invalid {field_name} '{value}'. Expected 10-digit Indian mobile number.",
                field=field_name,
            )
        cleaned = str(value)
    else:
        cleaned = str(value).strip()

    if not cleaned:
        if required:
            raise ExcelValidationError(f"{field_name} cannot be empty.", field=field_name)
        return None

    if not _INDIAN_MOBILE_RE.match(cleaned):
        raise ExcelValidationError(
            f"Invalid {field_name} '{value}'. Expected 10-digit Indian mobile starting with 6, 7, 8, or 9.",
            field=field_name,
        )
    return cleaned


def validate_email(
    value: Any, *, required: bool = False, field_name: str = "email"
) -> Optional[str]:
    """Validate email format consistently with System models."""
    if value is None:
        if required:
            raise ExcelValidationError(f"{field_name} is required.", field=field_name)
        return None
    if not isinstance(value, str):
        raise ExcelValidationError(f"Invalid {field_name} value.", field=field_name)
    cleaned = value.strip()
    if not cleaned:
        if required:
            raise ExcelValidationError(f"{field_name} cannot be empty.", field=field_name)
        return None
    try:
        return normalize_and_validate_email(cleaned)
    except ValueError as exc:
        raise ExcelValidationError(f"Invalid {field_name} '{value}'.", field=field_name) from exc


def validate_pan(
    value: Any, *, required: bool = False, field_name: str = "pan"
) -> Optional[str]:
    """Validate 10-character uppercase Indian PAN format (AAAAA9999A)."""
    if value is None:
        if required:
            raise ExcelValidationError(f"{field_name} is required.", field=field_name)
        return None
    if isinstance(value, (bool, int, float)):
        raise ExcelValidationError(f"Invalid {field_name} format.", field=field_name)

    cleaned = str(value).strip().upper()
    if not cleaned:
        if required:
            raise ExcelValidationError(f"{field_name} cannot be empty.", field=field_name)
        return None
    if not _PAN_RE.match(cleaned):
        raise ExcelValidationError(
            f"Invalid {field_name} '{value}'. Expected 10-character PAN format (e.g. ABCDE1234F).",
            field=field_name,
        )
    return cleaned


def validate_ifsc(
    value: Any, *, required: bool = False, field_name: str = "ifsc_code"
) -> Optional[str]:
    """Validate 11-character uppercase Indian bank IFSC code (AAAA0XXXXXX)."""
    if value is None:
        if required:
            raise ExcelValidationError(f"{field_name} is required.", field=field_name)
        return None
    if isinstance(value, (bool, int, float)):
        raise ExcelValidationError(f"Invalid {field_name} format.", field=field_name)

    cleaned = str(value).strip().upper()
    if not cleaned:
        if required:
            raise ExcelValidationError(f"{field_name} cannot be empty.", field=field_name)
        return None
    if not _IFSC_RE.match(cleaned):
        raise ExcelValidationError(
            f"Invalid {field_name} '{value}'. Expected 11-character IFSC code (e.g. SBIN0001234).",
            field=field_name,
        )
    return cleaned


def validate_decimal_amount(
    value: Any,
    *,
    field_name: str = "amount",
    min_value: Optional[Decimal] = Decimal("0"),
    max_value: Optional[Decimal] = None,
    max_decimals: int = 2,
    allow_zero: bool = True,
    allow_negative: bool = False,
) -> Decimal:
    """
    Parse and validate monetary Decimal amounts strictly:
    - Rejects bool, None, NaN, Infinity, non-finite values
    - Rejects negatives when allow_negative=False
    - Rejects zero when allow_zero=False
    - Rejects excess decimal places (> max_decimals)
    """
    if value is None or isinstance(value, bool):
        raise ExcelValidationError(f"{field_name} must be a valid numeric amount.", field=field_name)

    try:
        raw_str = str(value).strip() if isinstance(value, (str, int)) else repr(value) if isinstance(value, float) else str(value)
        if not raw_str:
            raise ExcelValidationError(f"{field_name} cannot be empty.", field=field_name)
        dec = Decimal(str(value).strip()) if isinstance(value, str) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExcelValidationError(f"Invalid numeric {field_name} '{value}'.", field=field_name) from exc

    if not dec.is_finite():
        raise ExcelValidationError(f"Invalid {field_name}: non-finite number '{value}'.", field=field_name)

    if not allow_negative and dec < Decimal("0"):
        raise ExcelValidationError(f"{field_name} cannot be negative (got {dec}).", field=field_name)

    if not allow_zero and dec == Decimal("0"):
        raise ExcelValidationError(f"{field_name} must be greater than zero.", field=field_name)

    if min_value is not None and dec < min_value:
        raise ExcelValidationError(
            f"{field_name} ({dec}) is below minimum allowed value ({min_value}).",
            field=field_name,
        )

    if max_value is not None and dec > max_value:
        raise ExcelValidationError(
            f"{field_name} ({dec}) exceeds maximum allowed value ({max_value}).",
            field=field_name,
        )

    exp = dec.normalize().as_tuple().exponent
    if isinstance(exp, int) and exp < -max_decimals:
        raise ExcelValidationError(
            f"{field_name} ({value}) has more than {max_decimals} decimal places.",
            field=field_name,
        )

    return dec


def validate_quantity(
    value: Any,
    *,
    field_name: str = "qty",
    min_value: Decimal = Decimal("0"),
    max_value: Decimal = Decimal("1000000"),
    max_decimals: int = 3,
    allow_zero: bool = False,
) -> Decimal:
    """Validate inventory/billing numeric quantities within precision & range limits."""
    return validate_decimal_amount(
        value,
        field_name=field_name,
        min_value=min_value,
        max_value=max_value,
        max_decimals=max_decimals,
        allow_zero=allow_zero,
        allow_negative=False,
    )


def mask_sensitive_aadhaar(aadhaar_no: Optional[str]) -> str:
    """Mask Aadhaar numbers before any display or export (never export full 12 digits)."""
    if not aadhaar_no:
        return ""
    digits = re.sub(r"\D", "", str(aadhaar_no))
    if len(digits) < 4:
        return "****"
    return f"XXXX-XXXX-{digits[-4:]}"


__all__ = [
    "ALLOWED_XLSX_EXTENSIONS",
    "FORBIDDEN_IMPORT_HEADERS",
    "FORBIDDEN_SPREADSHEET_EXTENSIONS",
    "FORMULA_PREFIX_CHARS",
    "MAX_CELL_LENGTH",
    "MAX_COLUMNS",
    "MAX_COMPRESSION_RATIO",
    "MAX_DATA_ROWS",
    "MAX_UNCOMPRESSED_BYTES",
    "MAX_UPLOAD_BYTES",
    "MAX_ZIP_ENTRIES",
    "SENSITIVE_NEVER_EXPORT_FIELDS",
    "ZIP_MAGIC",
    "ExcelSecurityError",
    "ExcelValidationError",
    "assert_headers_allowed_for_import",
    "ensure_defusedxml_active",
    "is_formula_like_string",
    "mask_sensitive_aadhaar",
    "read_xlsx_sheet_safely",
    "safe_xlsx_workbook",
    "sanitize_excel_text",
    "validate_date_range",
    "validate_date_str",
    "validate_decimal_amount",
    "validate_email",
    "validate_filename_extension",
    "validate_gstin",
    "validate_hsn",
    "validate_ifsc",
    "validate_import_cell_value",
    "validate_indian_mobile",
    "validate_pan",
    "validate_quantity",
    "validate_xlsx_bytes",
]
