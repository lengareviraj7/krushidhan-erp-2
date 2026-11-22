"""
Excel Engine API Foundation Router & Shared Utilities (C3-B1).
Provides:
- Standardized .xlsx download headers (Content-Type, Content-Disposition, nosniff, no-store)
- Filename header-injection sanitization
- Chunked upload size guard foundation
- Security (400) / Validation (422) / DatabaseMaintenanceError (503) HTTP error translation
- Role-based access dependencies using existing get_current_user & require_admin_user

Specific export report endpoints and import workflows will be built on top of this
foundation in subsequent C3-B stages.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from src.api.auth_middleware import get_current_user, require_admin_user
from src.db.connection import DatabaseMaintenanceError, get_db_manager
from src.services.excel_export_service import ExcelExportService
from src.services.excel_import_service import (
    CUSTOMER_IMPORT_TEMPLATE_FILENAME,
    PRODUCT_IMPORT_TEMPLATE_FILENAME,
    SUPPLIER_IMPORT_TEMPLATE_FILENAME,
    ExcelImportService,
    ExcelImportTransactionError,
    PreviewTokenError,
)
from src.services.excel_security import (
    MAX_CELL_LENGTH,
    MAX_COLUMNS,
    MAX_COMPRESSION_RATIO,
    MAX_DATA_ROWS,
    MAX_UNCOMPRESSED_BYTES,
    MAX_UPLOAD_BYTES,
    MAX_ZIP_ENTRIES,
    ExcelSecurityError,
    ExcelValidationError,
    ensure_defusedxml_active,
    validate_date_range,
    validate_filename_extension,
    validate_xlsx_bytes,
)

T = TypeVar("T")

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

router = APIRouter(prefix="/api/excel", tags=["Excel Foundation"])


def sanitize_download_filename(filename: str, *, fallback: str = "krushidhan_report.xlsx") -> str:
    """
    Sanitize an Excel attachment filename to eliminate CRLF, quotes, path traversal,
    and non-ASCII control characters before placing in Content-Disposition headers.
    """
    raw = str(filename or "").strip()
    raw = Path(raw.replace("\\", "/")).name
    raw = _FILENAME_SAFE_RE.sub("_", raw).strip("._")
    if not raw:
        raw = fallback
    if not raw.lower().endswith(".xlsx"):
        raw = f"{raw}.xlsx"
    return raw


def xlsx_download_response(content: bytes, filename: str) -> Response:
    """
    Build a hardened HTTP attachment Response for generated .xlsx workbooks.
    """
    safe_name = sanitize_download_filename(filename)
    return Response(
        content=bytes(content),
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


def translate_excel_exception(exc: Exception) -> HTTPException:
    """
    Translate Excel domain exceptions cleanly to HTTP errors.
    Always re-raises DatabaseMaintenanceError so the global 503 handler responds with Retry-After.
    """
    if isinstance(exc, DatabaseMaintenanceError):
        raise exc
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, PreviewTokenError):
        return HTTPException(status_code=exc.status_code, detail=exc.message)
    if isinstance(exc, ExcelImportTransactionError):
        return HTTPException(status_code=500, detail=exc.message)
    if isinstance(exc, ExcelSecurityError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, ExcelValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Unexpected Excel processing error.")


def execute_excel_operation(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """
    Invoke an Excel service operation with mandatory DatabaseMaintenanceError propagation
    and structured HTTP 400/422 error translation.
    """
    try:
        return fn(*args, **kwargs)
    except DatabaseMaintenanceError:
        raise
    except HTTPException:
        raise
    except Exception as exc:
        raise translate_excel_exception(exc) from exc


def validate_query_date_range(from_date: str, to_date: str) -> Tuple[str, str]:
    """Validate query parameters from_date and to_date cleanly or raise HTTP 422."""
    return execute_excel_operation(validate_date_range, from_date, to_date)


async def read_validated_xlsx_upload(file: UploadFile) -> Tuple[bytes, str]:
    """
    Bounded async reader for FastAPI UploadFile .xlsx uploads:
    stops reading immediately past MAX_UPLOAD_BYTES and runs full OOXML security checks.
    """
    try:
        safe_filename = validate_filename_extension(file.filename)
        chunks = []
        total_read = 0
        chunk_size = 65536
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total_read += len(chunk)
            if total_read > MAX_UPLOAD_BYTES:
                raise ExcelSecurityError(
                    f"Uploaded file exceeds maximum allowed size of {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
                )
            chunks.append(chunk)
        raw_bytes = b"".join(chunks)
        validate_xlsx_bytes(raw_bytes, filename=safe_filename)
        return raw_bytes, safe_filename
    except DatabaseMaintenanceError:
        raise
    except HTTPException:
        raise
    except Exception as exc:
        raise translate_excel_exception(exc) from exc


@router.get("/health")
def get_excel_engine_health(_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """
    Authenticated Excel engine foundation readiness check.
    Verifies defusedxml protection is active and database is not in maintenance mode.
    """
    try:
        ensure_defusedxml_active()
        db = get_db_manager()
        row = db.fetch_one("SELECT company_name FROM company_settings WHERE setting_id = 1;")
        shop_configured = bool(row is not None)
        return {
            "status": "ok",
            "defusedxml_active": True,
            "shop_configured": shop_configured,
            "limits": {
                "max_upload_bytes": MAX_UPLOAD_BYTES,
                "max_zip_entries": MAX_ZIP_ENTRIES,
                "max_uncompressed_bytes": MAX_UNCOMPRESSED_BYTES,
                "max_compression_ratio": MAX_COMPRESSION_RATIO,
                "max_data_rows": MAX_DATA_ROWS,
                "max_columns": MAX_COLUMNS,
                "max_cell_length": MAX_CELL_LENGTH,
            },
        }
    except DatabaseMaintenanceError:
        raise
    except Exception as exc:
        raise translate_excel_exception(exc) from exc


@router.get("/admin/security-policy")
def get_excel_security_policy(_admin: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    """
    Admin-only endpoint confirming Excel engine security policy and active RBAC enforcement.
    """
    try:
        ensure_defusedxml_active()
        db = get_db_manager()
        schema_version = db.get_schema_version()
        return {
            "status": "ok",
            "schema_version": schema_version,
            "defusedxml_active": True,
            "rbac_enforced": True,
            "data_only_imports": False,
            "formula_exports": False,
        }
    except DatabaseMaintenanceError:
        raise
    except Exception as exc:
        raise translate_excel_exception(exc) from exc


# ---------------------------------------------------------------------------
# Stage C3-B2A: Product & Stock Excel Exports (4 Operational Reports)
# ---------------------------------------------------------------------------
@router.get("/exports/products")
def download_product_catalog_excel(
    query: str = "",
    category_id: Optional[int] = None,
    include_inactive: bool = True,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Response:
    """Download sanitized Product Catalog workbook (.xlsx) without cost prices."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_user.get("full_name") or current_user.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_product_catalog,
        query=query,
        category_id=category_id,
        include_inactive=include_inactive,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/stock-summary")
def download_current_stock_summary_excel(
    category_id: Optional[int] = None,
    query: str = "",
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Response:
    """Download product-level Current Stock Summary workbook (.xlsx) without purchase costs/margins."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_user.get("full_name") or current_user.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_current_stock_summary,
        category_id=category_id,
        query=query,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/stock-batches")
def download_batch_wise_stock_expiry_excel(
    category_id: Optional[int] = None,
    query: str = "",
    include_zero_stock: bool = False,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Response:
    """Download FEFO-ordered Batch-wise Stock & Expiry workbook (.xlsx)."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_user.get("full_name") or current_user.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_batch_wise_stock_expiry,
        category_id=category_id,
        query=query,
        include_zero_stock=include_zero_stock,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/stock-ledger")
def download_stock_ledger_excel(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    product_id: Optional[int] = None,
    category_id: Optional[int] = None,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Response:
    """Download chronological Stock Ledger workbook (.xlsx) with optional validated date filters."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_user.get("full_name") or current_user.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_stock_ledger,
        from_date=from_date,
        to_date=to_date,
        product_id=product_id,
        category_id=category_id,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


# ---------------------------------------------------------------------------
# Stage C3-B2B: Sales & Purchase Excel Exports (4 Operational & Admin Reports)
# ---------------------------------------------------------------------------
@router.get("/exports/sales-register")
def download_sales_register_excel(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    customer_id: Optional[int] = None,
    payment_mode: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Response:
    """Download invoice-level Sales Register workbook (.xlsx) without cost prices or secrets."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_user.get("full_name") or current_user.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_sales_register,
        from_date=from_date,
        to_date=to_date,
        customer_id=customer_id,
        payment_mode=payment_mode,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/sales-items")
def download_sales_item_details_excel(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    customer_id: Optional[int] = None,
    product_id: Optional[int] = None,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Response:
    """Download line-item Sales Item Details workbook (.xlsx) without purchase costs or margins."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_user.get("full_name") or current_user.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_sales_item_details,
        from_date=from_date,
        to_date=to_date,
        customer_id=customer_id,
        product_id=product_id,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/purchase-register")
def download_purchase_register_excel(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    supplier_id: Optional[int] = None,
    payment_type: Optional[str] = None,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Response:
    """Download ADMIN-only Purchase Register workbook (.xlsx) without supplier bank/PAN secrets."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_admin.get("full_name") or current_admin.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_purchase_register,
        from_date=from_date,
        to_date=to_date,
        supplier_id=supplier_id,
        payment_type=payment_type,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/purchase-items")
def download_purchase_item_details_excel(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    supplier_id: Optional[int] = None,
    product_id: Optional[int] = None,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Response:
    """Download ADMIN-only Purchase Item Details workbook (.xlsx) using saved historical rates."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_admin.get("full_name") or current_admin.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_purchase_item_details,
        from_date=from_date,
        to_date=to_date,
        supplier_id=supplier_id,
        product_id=product_id,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


# ---------------------------------------------------------------------------
# Stage C3-B3A: Accounting, Ledger, Outstanding, Payment & Expense Exports
# ---------------------------------------------------------------------------
@router.get("/exports/customer-ledger")
def download_customer_ledger_excel(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    customer_id: Optional[int] = None,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Response:
    """Download Customer Ledger workbook (.xlsx) combining invoices, receipts, and sales returns."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_user.get("full_name") or current_user.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_customer_ledger,
        from_date=from_date,
        to_date=to_date,
        customer_id=customer_id,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/customer-outstanding")
def download_customer_outstanding_excel(
    query: str = "",
    village: str = "",
    only_outstanding: bool = False,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Response:
    """Download Customer Outstanding Balances workbook (.xlsx) from `customers.current_balance`."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_user.get("full_name") or current_user.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_customer_outstanding,
        query=query,
        village=village,
        only_outstanding=only_outstanding,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/supplier-ledger")
def download_supplier_ledger_excel(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    supplier_id: Optional[int] = None,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Response:
    """Download ADMIN-only Supplier Ledger workbook (.xlsx) combining purchase bills & payment vouchers."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_admin.get("full_name") or current_admin.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_supplier_ledger,
        from_date=from_date,
        to_date=to_date,
        supplier_id=supplier_id,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/supplier-outstanding")
def download_supplier_outstanding_excel(
    query: str = "",
    only_outstanding: bool = False,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Response:
    """Download ADMIN-only Supplier Outstanding Balances workbook (.xlsx) from `suppliers.current_balance`."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_admin.get("full_name") or current_admin.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_supplier_outstanding,
        query=query,
        only_outstanding=only_outstanding,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/payment-history")
def download_payment_history_excel(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    payment_mode: Optional[str] = None,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Response:
    """Download ADMIN-only Consolidated Payment History workbook (.xlsx) across inflows and outflows."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_admin.get("full_name") or current_admin.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_payment_history,
        from_date=from_date,
        to_date=to_date,
        payment_mode=payment_mode,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/expenses")
def download_expenses_excel(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    category_id: Optional[int] = None,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Response:
    """Download ADMIN-only Operational Expenses workbook (.xlsx) sourced directly from `expenses`."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_admin.get("full_name") or current_admin.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_expenses,
        from_date=from_date,
        to_date=to_date,
        category_id=category_id,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


# ---------------------------------------------------------------------------
# Stage C3-B3B: Customer & Supplier Master Directory Excel Exports
# ---------------------------------------------------------------------------
@router.get("/exports/customers")
def download_customer_master_excel(
    query: str = "",
    village: str = "",
    group_id: Optional[int] = None,
    include_inactive: bool = True,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Response:
    """Download sanitized Customer Master Directory workbook (.xlsx) without Aadhaar or balances."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_user.get("full_name") or current_user.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_customer_master,
        query=query,
        village=village,
        group_id=group_id,
        include_inactive=include_inactive,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/suppliers")
def download_supplier_master_excel(
    query: str = "",
    include_inactive: bool = True,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Response:
    """Download sanitized Supplier Master Directory workbook (.xlsx) without PAN, bank/IFSC, or balances."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_user.get("full_name") or current_user.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_supplier_master,
        query=query,
        include_inactive=include_inactive,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


# ---------------------------------------------------------------------------
# Stage C3-B3C: GST Summary, GSTR-1 Working Paper, Statutory & P&L Exports (ADMIN ONLY)
# ---------------------------------------------------------------------------
@router.get("/exports/gst-summary")
def download_gst_summary_excel(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Response:
    """Download ADMIN-only GST Summary workbook (.xlsx) with rate breakdown & HSN limitation notice."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_admin.get("full_name") or current_admin.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_gst_summary,
        from_date=from_date,
        to_date=to_date,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/gstr1-working-paper")
def download_gstr1_working_paper_excel(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Response:
    """Download ADMIN-only GSTR-1 Accountant Working Paper (.xlsx) for internal verification before filing."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_admin.get("full_name") or current_admin.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_gstr1_working_paper,
        from_date=from_date,
        to_date=to_date,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/statutory-registers")
def download_statutory_registers_excel(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Response:
    """Download ADMIN-only Statutory & Audit Registers workbook (.xlsx) without Aadhaar/PAN/bank secrets."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_admin.get("full_name") or current_admin.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_statutory_registers,
        from_date=from_date,
        to_date=to_date,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


@router.get("/exports/profit-and-loss")
def download_profit_and_loss_excel(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Response:
    """Download ADMIN-only Profit & Loss workbook (.xlsx) matching ERP P&L with returns disclosure."""
    svc = ExcelExportService(get_db_manager())
    generated_by = current_admin.get("full_name") or current_admin.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.export_profit_and_loss,
        from_date=from_date,
        to_date=to_date,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename)


# ---------------------------------------------------------------------------
# Stage C3-C1: Secure Excel Import Foundation & Product Master Import (ADMIN ONLY)
# ---------------------------------------------------------------------------
class ProductImportCommitRequest(BaseModel):
    preview_token: str = Field(..., min_length=16, max_length=256)
    file_sha256: str = Field(..., min_length=64, max_length=64)


@router.get("/imports/products/template")
def download_product_import_template_excel(
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Response:
    """Download ADMIN-only Product Import Template (.xlsx) with single sheet `Product Import`."""
    svc = ExcelImportService(get_db_manager())
    generated_by = current_admin.get("full_name") or current_admin.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.generate_product_import_template,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename or PRODUCT_IMPORT_TEMPLATE_FILENAME)


@router.post("/imports/products/preview")
async def preview_product_import_excel(
    file: UploadFile = File(...),
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    """
    Validate an uploaded `.xlsx` workbook completely in memory (zero DB writes),
    check master references & duplicates row by row, and return a bound single-use preview token.
    """
    raw_bytes, safe_filename = await read_validated_xlsx_upload(file)
    svc = ExcelImportService(get_db_manager())
    return execute_excel_operation(
        svc.preview_product_import,
        raw_bytes,
        filename=safe_filename,
        admin_user=current_admin,
    )


@router.post("/imports/products/commit")
def commit_product_import_excel(
    payload: ProductImportCommitRequest,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    """
    Validate the single-use ADMIN preview token + workbook SHA-256 and commit
    only `Ready to Import` products atomically inside a single SQLite transaction.
    """
    svc = ExcelImportService(get_db_manager())
    return execute_excel_operation(
        svc.commit_product_import,
        preview_token=payload.preview_token,
        file_sha256=payload.file_sha256,
        admin_user=current_admin,
    )


# ---------------------------------------------------------------------------
# Stage C3-C2: Secure Customer Master Excel Import (ADMIN ONLY)
# ---------------------------------------------------------------------------
class CustomerImportCommitRequest(BaseModel):
    preview_token: str = Field(..., min_length=16, max_length=256)
    file_sha256: str = Field(..., min_length=64, max_length=64)


@router.get("/imports/customers/template")
def download_customer_import_template_excel(
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Response:
    """Download ADMIN-only Customer Import Template (.xlsx) with single sheet `Customer Import`."""
    svc = ExcelImportService(get_db_manager())
    generated_by = current_admin.get("full_name") or current_admin.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.generate_customer_import_template,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename or CUSTOMER_IMPORT_TEMPLATE_FILENAME)


@router.post("/imports/customers/preview")
async def preview_customer_import_excel(
    file: UploadFile = File(...),
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    """
    Validate an uploaded `.xlsx` customer workbook completely in memory (zero DB writes),
    check customer group lookups & duplicates row by row, and return a bound single-use preview token.
    """
    raw_bytes, safe_filename = await read_validated_xlsx_upload(file)
    svc = ExcelImportService(get_db_manager())
    return execute_excel_operation(
        svc.preview_customer_import,
        raw_bytes,
        filename=safe_filename,
        admin_user=current_admin,
    )


@router.post("/imports/customers/commit")
def commit_customer_import_excel(
    payload: CustomerImportCommitRequest,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    """
    Validate the single-use ADMIN customer preview token + workbook SHA-256 and commit
    only `Ready to Import` customers atomically inside a single SQLite transaction.
    """
    svc = ExcelImportService(get_db_manager())
    return execute_excel_operation(
        svc.commit_customer_import,
        preview_token=payload.preview_token,
        file_sha256=payload.file_sha256,
        admin_user=current_admin,
    )


# ---------------------------------------------------------------------------
# Stage C3-C3: Secure Supplier Master Excel Import (ADMIN ONLY)
# ---------------------------------------------------------------------------
class SupplierImportCommitRequest(BaseModel):
    preview_token: str = Field(..., min_length=16, max_length=256)
    file_sha256: str = Field(..., min_length=64, max_length=64)


@router.get("/imports/suppliers/template")
def download_supplier_import_template_excel(
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Response:
    """Download ADMIN-only Supplier Import Template (.xlsx) with single sheet `Supplier Import`."""
    svc = ExcelImportService(get_db_manager())
    generated_by = current_admin.get("full_name") or current_admin.get("username")
    xlsx_bytes, filename = execute_excel_operation(
        svc.generate_supplier_import_template,
        generated_by=generated_by,
    )
    return xlsx_download_response(xlsx_bytes, filename or SUPPLIER_IMPORT_TEMPLATE_FILENAME)


@router.post("/imports/suppliers/preview")
async def preview_supplier_import_excel(
    file: UploadFile = File(...),
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    """
    Validate an uploaded `.xlsx` supplier workbook completely in memory (zero DB writes),
    check formatting & duplicates row by row, and return a bound single-use preview token.
    """
    raw_bytes, safe_filename = await read_validated_xlsx_upload(file)
    svc = ExcelImportService(get_db_manager())
    return execute_excel_operation(
        svc.preview_supplier_import,
        raw_bytes,
        filename=safe_filename,
        admin_user=current_admin,
    )


@router.post("/imports/suppliers/commit")
def commit_supplier_import_excel(
    payload: SupplierImportCommitRequest,
    current_admin: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    """
    Validate the single-use ADMIN supplier preview token + workbook SHA-256 and commit
    only `Ready to Import` suppliers atomically inside a single SQLite transaction.
    """
    svc = ExcelImportService(get_db_manager())
    return execute_excel_operation(
        svc.commit_supplier_import,
        preview_token=payload.preview_token,
        file_sha256=payload.file_sha256,
        admin_user=current_admin,
    )
