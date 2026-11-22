"""
API Route Handlers for Inward Purchases, Supplier Invoices, and Purchase Returns.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from src.api.auth_middleware import require_roles
from src.db.connection import DatabaseMaintenanceError, get_db_manager
from src.idempotency import (
    reject_client_request_fingerprint,
    resolve_request_idempotency_key,
)
from src.models.purchase import Purchase
from src.models.purchase_return import (
    PurchaseReturn,
    PurchaseReturnCreate,
    PurchaseReturnPreviewRequest,
    PurchaseReturnPreviewResponse,
)
from src.repositories.purchase_repository import PurchaseRepository
from src.services.purchase_return_service import IdempotencyConflictError, PurchaseReturnService
from src.services.purchase_service import PurchaseService

logger = logging.getLogger("agri_erp.api.purchase")

router = APIRouter(prefix="/api/purchase", tags=["Purchase"])
returns_router = APIRouter(prefix="/api/purchase-returns", tags=["Purchase Returns"])


# ----------------------------------------------------------------------------
# Inward Purchase Invoices
# ----------------------------------------------------------------------------

@router.post("/create")
def create_purchase_invoice(
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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    normalized_payload = dict(payload)
    normalized_payload["idempotency_key"] = resolved_key
    try:
        purchase = Purchase.model_validate(normalized_payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors())

    svc = PurchaseService(get_db_manager())
    try:
        purchase_id, is_replay = svc.process_inward_purchase_with_status(purchase)
        return {
            "success": True,
            "purchase_id": purchase_id,
            "invoice_no": purchase.invoice_no,
            "idempotent_replay": is_replay,
        }
    except IdempotencyConflictError as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(e), "error_code": "IDEMPOTENCY_KEY_CONFLICT"},
        )
    except DatabaseMaintenanceError:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/list")
def list_purchases(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    supplier_id: Optional[int] = None,
):
    repo = PurchaseRepository(get_db_manager())
    return repo.get_purchases_list(from_date=from_date, to_date=to_date, supplier_id=supplier_id)


# ----------------------------------------------------------------------------
# Purchase Returns Handlers & Endpoints
# Note: Defined before /{purchase_id} to prevent path segment collisions
# ----------------------------------------------------------------------------

def _handle_create_purchase_return(
    payload: PurchaseReturnCreate,
    _user: Dict[str, Any],
) -> Dict[str, Any]:
    svc = PurchaseReturnService(get_db_manager())
    try:
        result = svc.process_purchase_return(payload)
        ret_dict = result.model_dump()
        ret_dict["success"] = True
        return ret_dict
    except IdempotencyConflictError as exc:
        logger.warning("Purchase return idempotency conflict: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )
    except ValueError as exc:
        logger.warning("Purchase return validation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except HTTPException:
        raise
    except DatabaseMaintenanceError:
        raise
    except Exception as exc:
        logger.exception("Unexpected error processing purchase return: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected internal error occurred while processing the purchase return. Please contact support.",
        )


def _handle_preview_purchase_return(
    payload: PurchaseReturnPreviewRequest,
    _user: Dict[str, Any],
) -> Dict[str, Any]:
    svc = PurchaseReturnService(get_db_manager())
    try:
        return svc.preview_purchase_return(payload)
    except ValueError as exc:
        logger.warning("Purchase return preview validation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except HTTPException:
        raise
    except DatabaseMaintenanceError:
        raise
    except Exception as exc:
        logger.exception("Unexpected error calculating purchase return preview: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while calculating return preview.",
        )


def _handle_list_purchase_returns(
    from_date: Optional[str],
    to_date: Optional[str],
    supplier_id: Optional[int],
    purchase_id: Optional[int],
    status_filter: Optional[str],
    limit: Optional[int],
    offset: Optional[int],
    _user: Dict[str, Any],
) -> List[PurchaseReturn]:
    svc = PurchaseReturnService(get_db_manager())
    try:
        returns = svc.list_returns(
            from_date=from_date,
            to_date=to_date,
            supplier_id=supplier_id,
            purchase_id=purchase_id,
            status=status_filter,
        )
        if offset is not None and offset > 0:
            returns = returns[offset:]
        if limit is not None and limit > 0:
            returns = returns[:limit]
        return returns
    except HTTPException:
        raise
    except DatabaseMaintenanceError:
        raise
    except Exception as exc:
        logger.exception("Unexpected error listing purchase returns: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing purchase returns.",
        )


def _handle_get_purchase_return_detail(
    return_id: int,
    _user: Dict[str, Any],
) -> PurchaseReturn:
    svc = PurchaseReturnService(get_db_manager())
    try:
        ret = svc.get_return_by_id(return_id)
        if not ret:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Purchase return #{return_id} not found.",
            )
        return ret
    except HTTPException:
        raise
    except DatabaseMaintenanceError:
        raise
    except Exception as exc:
        logger.exception("Unexpected error retrieving purchase return #%s: %s", return_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving purchase return details.",
        )


@router.post("/returns/preview")
def preview_purchase_return(
    payload: PurchaseReturnPreviewRequest,
    user: Dict[str, Any] = Depends(require_roles(["ADMIN", "OWNER", "MANAGER", "ACCOUNTANT"])),
):
    """Authoritative calculation preview for purchase returns without persistence."""
    return _handle_preview_purchase_return(payload, user)


@router.post("/returns")
def create_purchase_return(
    payload: PurchaseReturnCreate,
    user: Dict[str, Any] = Depends(require_roles(["ADMIN", "OWNER", "MANAGER", "ACCOUNTANT"])),
):
    """Process supplier purchase return (POST /api/purchase/returns)."""
    return _handle_create_purchase_return(payload, user)


@router.get("/returns")
def list_purchase_returns(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    supplier_id: Optional[int] = None,
    purchase_id: Optional[int] = None,
    status: Optional[str] = Query(None, alias="status"),
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    user: Dict[str, Any] = Depends(require_roles(["ADMIN", "OWNER", "MANAGER", "ACCOUNTANT"])),
):
    """List purchase returns with filters (GET /api/purchase/returns)."""
    return _handle_list_purchase_returns(
        from_date, to_date, supplier_id, purchase_id, status, limit, offset, user
    )


@router.get("/returns/{return_id}")
def get_purchase_return_detail(
    return_id: int,
    user: Dict[str, Any] = Depends(require_roles(["ADMIN", "OWNER", "MANAGER", "ACCOUNTANT"])),
):
    """Get purchase return detail by return_id (GET /api/purchase/returns/{return_id})."""
    return _handle_get_purchase_return_detail(return_id, user)


@router.get("/{purchase_id}/eligible-returns")
def get_eligible_purchase_returns(
    purchase_id: int,
    user: Dict[str, Any] = Depends(require_roles(["ADMIN", "OWNER", "MANAGER", "ACCOUNTANT"])),
):
    """Fetch eligible lines and remaining returnable quantities for a purchase invoice."""
    svc = PurchaseReturnService(get_db_manager())
    try:
        return svc.get_eligible_purchase_lines(purchase_id)
    except HTTPException:
        raise
    except DatabaseMaintenanceError:
        raise
    except Exception as exc:
        logger.exception("Unexpected error fetching eligible returns for purchase #%s: %s", purchase_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching eligible return lines.",
        )


# Direct mount aliases for /api/purchase-returns
@returns_router.post("/preview")
def preview_purchase_return_direct(
    payload: PurchaseReturnPreviewRequest,
    user: Dict[str, Any] = Depends(require_roles(["ADMIN", "OWNER", "MANAGER", "ACCOUNTANT"])),
):
    """Direct alias for POST /api/purchase-returns/preview."""
    return _handle_preview_purchase_return(payload, user)


@returns_router.post("")
def create_purchase_return_direct(
    payload: PurchaseReturnCreate,
    user: Dict[str, Any] = Depends(require_roles(["ADMIN", "OWNER", "MANAGER", "ACCOUNTANT"])),
):
    """Direct alias for POST /api/purchase-returns."""
    return _handle_create_purchase_return(payload, user)


@returns_router.get("")
def list_purchase_returns_direct(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    supplier_id: Optional[int] = None,
    purchase_id: Optional[int] = None,
    status: Optional[str] = Query(None, alias="status"),
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    user: Dict[str, Any] = Depends(require_roles(["ADMIN", "OWNER", "MANAGER", "ACCOUNTANT"])),
):
    """Direct alias for GET /api/purchase-returns."""
    return _handle_list_purchase_returns(
        from_date, to_date, supplier_id, purchase_id, status, limit, offset, user
    )


@returns_router.get("/{return_id}")
def get_purchase_return_detail_direct(
    return_id: int,
    user: Dict[str, Any] = Depends(require_roles(["ADMIN", "OWNER", "MANAGER", "ACCOUNTANT"])),
):
    """Direct alias for GET /api/purchase-returns/{return_id}."""
    return _handle_get_purchase_return_detail(return_id, user)


# ----------------------------------------------------------------------------
# Parameterized Inward Purchase Invoice Detail
# ----------------------------------------------------------------------------

@router.get("/{purchase_id}")
def get_purchase_detail(purchase_id: int):
    repo = PurchaseRepository(get_db_manager())
    pur = repo.get_purchase_by_id(purchase_id)
    if not pur:
        raise HTTPException(status_code=404, detail="Purchase invoice not found")
    return pur


__all__ = ["router", "returns_router"]
