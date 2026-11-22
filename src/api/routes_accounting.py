"""
API Route Handlers for Double-Entry Accounts, Vouchers, Day Book, Supplier Ledger, Expenses & Cash Drawer.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from src.api.auth_middleware import require_admin_user
from src.db.connection import DatabaseMaintenanceError, get_db_manager
from src.idempotency import (
    IdempotencyConflictError,
    reject_client_request_fingerprint,
    resolve_request_idempotency_key,
)
from src.services.accounting_service import AccountingService

router = APIRouter(prefix="/api/accounting", tags=["Accounting"])


class CustomerReceiptRequest(BaseModel):
    customer_id: int
    receipt_date: Optional[str] = None
    amount: float
    payment_mode: str = "CASH"
    narration: Optional[str] = None
    idempotency_key: Optional[str] = None


class SupplierPaymentRequest(BaseModel):
    supplier_id: int
    amount: float
    payment_date: Optional[str] = None
    payment_mode: str = "BANK_TRANSFER"
    reference_no: Optional[str] = None
    narration: Optional[str] = None
    idempotency_key: Optional[str] = None


class RecordExpenseRequest(BaseModel):
    category_id: int
    amount: float
    expense_date: Optional[str] = None
    payment_mode: str = "CASH"
    reference_no: Optional[str] = None
    remarks: Optional[str] = None
    description: Optional[str] = None
    idempotency_key: Optional[str] = None


def _extract_idempotency_key_or_400(
    request: Request,
    payload: Dict[str, Any],
    idempotency_key_header: Optional[str],
) -> Optional[str]:
    try:
        reject_client_request_fingerprint(payload)
        raw_headers = request.headers.getlist("idempotency-key")
        return resolve_request_idempotency_key(
            payload.get("idempotency_key"),
            header_key=idempotency_key_header,
            header_values=raw_headers if raw_headers else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/customer-receipt")
def record_receipt(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    idempotency_key_header: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    resolved_key = _extract_idempotency_key_or_400(request, payload, idempotency_key_header)
    normalized_payload = dict(payload)
    normalized_payload["idempotency_key"] = resolved_key
    try:
        req = CustomerReceiptRequest.model_validate(normalized_payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors())

    svc = AccountingService(get_db_manager())
    try:
        vch_id, vch_no, is_replay = svc.record_customer_receipt_with_status(
            customer_id=req.customer_id,
            receipt_date=req.receipt_date,
            amount=req.amount,
            payment_mode=req.payment_mode,
            narration=req.narration,
            idempotency_key=resolved_key,
        )
        return {
            "success": True,
            "voucher_id": vch_id,
            "voucher_no": vch_no,
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


@router.get("/suppliers")
def get_suppliers_balances():
    """Fetch all suppliers with outstanding payables and inward purchase totals."""
    svc = AccountingService(get_db_manager())
    return svc.get_suppliers_with_balances()


@router.get("/suppliers/{supplier_id}/statement")
def get_supplier_statement(supplier_id: int):
    """Fetch complete ledger statement for a supplier."""
    svc = AccountingService(get_db_manager())
    try:
        return svc.get_supplier_statement(supplier_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/supplier-payment")
def record_payment(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    idempotency_key_header: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Record payment made to a supplier/distributor."""
    resolved_key = _extract_idempotency_key_or_400(request, payload, idempotency_key_header)
    normalized_payload = dict(payload)
    normalized_payload["idempotency_key"] = resolved_key
    try:
        req = SupplierPaymentRequest.model_validate(normalized_payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors())

    svc = AccountingService(get_db_manager())
    try:
        vch_id, vch_no, is_replay = svc.record_supplier_payment_with_status(
            supplier_id=req.supplier_id,
            amount=req.amount,
            payment_mode=req.payment_mode,
            payment_date=req.payment_date,
            reference_no=req.reference_no,
            narration=req.narration,
            idempotency_key=resolved_key,
        )
        return {
            "success": True,
            "voucher_id": vch_id,
            "voucher_no": vch_no,
            "idempotent_replay": is_replay,
            "message": "Payment recorded successfully",
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


@router.get("/expense-categories")
def get_expense_categories():
    """Fetch standard operational expense categories."""
    svc = AccountingService(get_db_manager())
    return svc.get_expense_categories()


@router.post("/expenses")
def record_expense(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    idempotency_key_header: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Record an operational shop expense (Rent, Labor / Loading, Electricity, Freight)."""
    resolved_key = _extract_idempotency_key_or_400(request, payload, idempotency_key_header)
    normalized_payload = dict(payload)
    normalized_payload["idempotency_key"] = resolved_key
    try:
        req = RecordExpenseRequest.model_validate(normalized_payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors())

    svc = AccountingService(get_db_manager())
    try:
        exp_id, vch_id, vch_no, is_replay = svc.record_daily_expense_with_status(
            category_id=req.category_id,
            amount=req.amount,
            expense_date=req.expense_date,
            payment_mode=req.payment_mode,
            reference_no=req.reference_no,
            remarks=req.remarks if req.remarks is not None else req.description,
            idempotency_key=resolved_key,
        )
        return {
            "success": True,
            "expense_id": exp_id,
            "voucher_id": vch_id,
            "voucher_no": vch_no,
            "idempotent_replay": is_replay,
            "message": "Expense recorded successfully",
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


@router.get("/cash-closing-summary")
def get_cash_closing_summary(target_date: Optional[str] = None, date: Optional[str] = None):
    """Reconcile daily cash drawer: Sales + Receipts - Expenses - Payments = Expected Drawer Cash."""
    from datetime import date as dt_date
    actual_date = target_date or date or dt_date.today().isoformat()
    svc = AccountingService(get_db_manager())
    return svc.get_cash_drawer_reconciliation(actual_date)


@router.get("/day-book")
def get_day_book(date: str):
    svc = AccountingService(get_db_manager())
    return svc.get_day_book(date)


@router.get("/profit-and-loss")
def get_profit_and_loss(
    from_date: str,
    to_date: str,
    _admin: Dict[str, Any] = Depends(require_admin_user),
):
    svc = AccountingService(get_db_manager())
    return svc.get_profit_and_loss(from_date, to_date)


@router.get("/profit-and-loss/products")
def get_product_profitability(
    from_date: str,
    to_date: str,
    query: str = "",
    _admin: Dict[str, Any] = Depends(require_admin_user),
):
    svc = AccountingService(get_db_manager())
    return svc.get_product_profitability(from_date, to_date, query=query)
