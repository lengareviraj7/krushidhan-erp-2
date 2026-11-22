"""
API Route Handlers for Sales, Billing POS, and PDF Invoicing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from src.db.connection import DatabaseMaintenanceError, get_db_manager
from src.idempotency import (
    DuplicateInvoiceError,
    IdempotencyConflictError,
    reject_client_request_fingerprint,
    resolve_request_idempotency_key,
)
from src.models.sales import Sale, SaleItem, SalesReturn, SalesReturnItem, SalesReturnPreviewRequest


from src.printing.invoice_printer import InvoicePrinter
from src.repositories.master_data_repository import MasterDataRepository
from src.repositories.sales_repository import SalesRepository
from src.repositories.system_repository import SystemRepository
from src.services.sales_service import SalesService

router = APIRouter(prefix="/api/sales", tags=["Sales"])



class TaxCalculationRequest(BaseModel):
    qty: float
    rate: float
    discount_percent: float = 0.0
    cgst_rate: float = 0.0
    sgst_rate: float = 0.0
    igst_rate: float = 0.0
    is_rate_tax_inclusive: bool = True


@router.get("/next-invoice-no")
def get_next_invoice_no():
    repo = SalesRepository(get_db_manager())
    return {"next_invoice_no": repo.generate_next_invoice_no()}


@router.get("/next-return-no")
def get_next_return_no():
    repo = SalesRepository(get_db_manager())
    return {"next_return_no": repo.generate_next_return_no()}


@router.post("/calculate-tax")
def calculate_tax(req: TaxCalculationRequest):
    svc = SalesService(get_db_manager())
    result = svc.calculate_item_tax(
        qty=req.qty,
        rate=req.rate,
        discount_percent=req.discount_percent,
        cgst_rate=req.cgst_rate,
        sgst_rate=req.sgst_rate,
        igst_rate=req.igst_rate,
        is_rate_tax_inclusive=req.is_rate_tax_inclusive,
    )
    return result


@router.post("/create")
def create_sales_invoice(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    idempotency_key_header: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    try:
        reject_client_request_fingerprint(payload)
        raw_headers = request.headers.getlist("idempotency-key")
        resolved_key = resolve_request_idempotency_key(
            payload.get("idempotency_key"),
            header_key=idempotency_key_header,
            header_values=raw_headers if raw_headers else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    normalized_payload = dict(payload)
    normalized_payload["idempotency_key"] = resolved_key
    try:
        sale = Sale.model_validate(normalized_payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors())

    svc = SalesService(get_db_manager())
    try:
        sale_id, is_replay = svc.process_sales_invoice_with_status(sale)
        return {
            "success": True,
            "sale_id": sale_id,
            "invoice_no": sale.invoice_no,
            "idempotent_replay": is_replay,
        }
    except IdempotencyConflictError as e:
        return JSONResponse(
            status_code=409,
            content={"detail": str(e), "error_code": "IDEMPOTENCY_KEY_CONFLICT"},
        )
    except DuplicateInvoiceError as e:
        return JSONResponse(
            status_code=409,
            content={"detail": str(e), "error_code": "DUPLICATE_INVOICE_NO"},
        )
    except DatabaseMaintenanceError:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/returns/preview")
def preview_sale_return(req: SalesReturnPreviewRequest):
    """Read-only, zero-write authoritative preview of sales return totals, GST, and Phase 2B-2 settlement eligibility."""
    svc = SalesService(get_db_manager())
    try:
        return svc.preview_sale_return(sale_id=req.sale_id, items=req.items)
    except DatabaseMaintenanceError:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/returns")
def create_sale_return(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    idempotency_key_header: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Process customer sale return invoice atomically with idempotency protection."""
    try:
        reject_client_request_fingerprint(payload)
        raw_headers = request.headers.getlist("idempotency-key")
        resolved_key = resolve_request_idempotency_key(
            payload.get("idempotency_key"),
            header_key=idempotency_key_header,
            header_values=raw_headers if raw_headers else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    normalized_payload = dict(payload)
    normalized_payload["idempotency_key"] = resolved_key
    try:
        ret = SalesReturn.model_validate(normalized_payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors())

    svc = SalesService(get_db_manager())
    try:
        return_id, is_replay = svc.process_sale_return_with_status(ret)
        return {
            "success": True,
            "return_id": return_id,
            "return_no": ret.return_no,
            "idempotent_replay": is_replay,
        }
    except IdempotencyConflictError as e:
        return JSONResponse(
            status_code=409,
            content={"detail": str(e), "error_code": "IDEMPOTENCY_KEY_CONFLICT"},
        )
    except DatabaseMaintenanceError:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/returns")
def list_sale_returns(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    customer_id: Optional[int] = None,
    sale_id: Optional[int] = None,
):
    """List past sale returns with optional filters."""
    repo = SalesRepository(get_db_manager())
    return repo.get_sale_returns_list(
        from_date=from_date,
        to_date=to_date,
        customer_id=customer_id,
        sale_id=sale_id,
    )


@router.get("/returns/{return_id}")
def get_sale_return_detail(return_id: int):
    """Get sale return detail by return_id."""
    repo = SalesRepository(get_db_manager())
    ret = repo.get_sale_return_by_id(return_id)
    if not ret:
        raise HTTPException(status_code=404, detail="Sale return record not found")
    return ret


@router.get("/list")
def list_sales(from_date: Optional[str] = None, to_date: Optional[str] = None, customer_id: Optional[int] = None):
    repo = SalesRepository(get_db_manager())
    return repo.get_sales_list(from_date=from_date, to_date=to_date, customer_id=customer_id)


@router.get("/{sale_id}/eligible-returns")
def get_eligible_returns(sale_id: int):
    """Fetch eligible return items, GST snapshots, and payment summary for a sale invoice."""
    db = get_db_manager()
    repo = SalesRepository(db)
    master_repo = MasterDataRepository(db)
    sale = repo.get_sale_by_id(sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Sale invoice not found")

    customer = master_repo.get_customer_by_id(sale.customer_id)
    returned_map = repo.get_already_returned_qty_map(sale_id)
    returned_batch_map = repo.get_already_returned_qty_by_batch_map(sale_id)
    prior_returned_amount = repo.get_prior_returned_amount(sale_id)
    prior_refunded_amount = repo.get_prior_refunded_amount(sale_id)
    prior_receivable_reduced = max(0.0, round(prior_returned_amount - prior_refunded_amount, 2))
    orig_due = max(0.0, round((sale.net_amount or 0.0) - (sale.paid_amount or 0.0), 2))
    remaining_invoice_due = max(0.0, round(orig_due - prior_receivable_reduced, 2))
    remaining_invoice_paid = max(0.0, round((sale.paid_amount or 0.0) - prior_refunded_amount, 2))

    # Count occurrences per product to accurately calculate per-line remaining qty
    prod_line_counts: Dict[int, int] = {}
    for itm in sale.items:
        prod_line_counts[itm.product_id] = prod_line_counts.get(itm.product_id, 0) + 1

    eligible_items = []
    for item in sale.items:
        if item.batch_id and (item.product_id, item.batch_id) in returned_batch_map:
            already_returned = returned_batch_map.get((item.product_id, item.batch_id), 0.0)
        elif prod_line_counts.get(item.product_id, 0) == 1:
            already_returned = returned_map.get(item.product_id, 0.0)
        else:
            already_returned = returned_batch_map.get((item.product_id, item.batch_id), 0.0) if item.batch_id else 0.0

        eligible_qty = max(0.0, round(item.qty - already_returned, 4))
        eligible_items.append({
            "sale_item_id": item.sale_item_id,
            "product_id": item.product_id,
            "product_name": item.product_name,
            "hsn_code": item.hsn_code,
            "batch_id": item.batch_id,
            "batch_no": item.batch_no,
            "exp_date": item.exp_date,
            "sold_qty": item.qty,
            "already_returned_qty": already_returned,
            "eligible_return_qty": eligible_qty,
            "sale_rate": item.sale_rate,
            "mrp": item.mrp,
            "unit_name": item.unit_name,
            "discount_amount": item.discount_amount,
            "taxable_amount": item.taxable_amount,
            "cgst_rate": item.cgst_rate,
            "cgst_amount": item.cgst_amount,
            "sgst_rate": item.sgst_rate,
            "sgst_amount": item.sgst_amount,
            "igst_rate": item.igst_rate,
            "igst_amount": item.igst_amount,
            "total_amount": item.total_amount,
        })
    return {
        "sale_id": sale_id,
        "invoice_no": sale.invoice_no,
        "sale_date": sale.sale_date,
        "customer_id": sale.customer_id,
        "customer_name": sale.customer_name or (customer.customer_name if customer else ""),
        "customer_mobile": sale.customer_mobile or (customer.mobile if customer else ""),
        "customer_current_balance": float(customer.current_balance or 0.0) if customer else 0.0,
        "payment_mode": sale.payment_mode,
        "net_amount": sale.net_amount,
        "paid_amount": sale.paid_amount,
        "due_amount": sale.due_amount,
        "remaining_invoice_due": remaining_invoice_due,
        "remaining_invoice_paid": remaining_invoice_paid,
        "items": eligible_items,
    }


@router.get("/{sale_id}")
def get_sale_detail(sale_id: int):
    repo = SalesRepository(get_db_manager())
    sale = repo.get_sale_by_id(sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Sale invoice not found")
    return sale


@router.get("/{sale_id}/pdf")
def download_invoice_pdf(sale_id: int):
    repo = SalesRepository(get_db_manager())
    sys_repo = SystemRepository(get_db_manager())
    sale = repo.get_sale_by_id(sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Sale invoice not found")

    settings = sys_repo.get_company_settings()
    printer = InvoicePrinter(company_settings=settings)
    pdf_bytes = printer.generate_invoice_pdf(sale=sale)
    repo.increment_print_count(sale_id)

    raw_inv = sale.invoice_no or str(sale_id)
    safe_inv = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw_inv).strip("_") or str(sale_id)
    filename = f"Invoice_{safe_inv}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

