"""
Domain Data Models and Value Validators for Purchase Returns, Debit Notes,
Multi-Settlement Allocations, and Supplier Credit Notes.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


# Canonical domain constants
CANONICAL_SETTLEMENT_TYPES = (
    "PAYABLE_REDUCTION",
    "REFUND_CASH",
    "REFUND_BANK",
    "SUPPLIER_CREDIT",
)

INITIAL_ITC_STATUSES = (
    "CLAIMED_REVERSED",
    "NOT_CLAIMED",
    "PENDING_CONFIRMATION",
)

ALL_ITC_STATUSES = (
    "CLAIMED_REVERSED",
    "NOT_CLAIMED",
    "PENDING_CONFIRMATION",
    "CONFIRMED_REVERSED",
)

RETURN_STATUSES = ("POSTED", "CANCELLED")

SUPPLIER_CREDIT_STATUSES = (
    "AVAILABLE",
    "PARTIALLY_USED",
    "FULLY_USED",
    "CANCELLED",
)


def validate_currency_paise(
    v: Any,
    field_name: str = "amount",
    allow_none: bool = False,
    min_value: Optional[Decimal] = None,
) -> Optional[Decimal]:
    """
    Validates currency amounts:
    - Must be representable in exact paise (at most 2 decimal places).
    - Never silently rounds input; rejects excess decimal places.
    """
    if v is None:
        if allow_none:
            return None
        raise ValueError(f"{field_name} cannot be None")

    try:
        dec = Decimal(str(v))
    except Exception as exc:
        raise ValueError(f"Invalid monetary value for {field_name}: {v}") from exc

    if dec.as_tuple().exponent < -2:
        raise ValueError(
            f"{field_name} cannot have more than 2 decimal places (paise precision). Got: {v}"
        )
    if min_value is not None and dec < min_value:
        raise ValueError(f"{field_name} must be >= {min_value}. Got: {dec}")

    return dec.quantize(Decimal("0.01"))


def validate_positive_currency_paise(
    v: Any,
    field_name: str = "amount",
) -> Decimal:
    """Validates strictly positive currency amount (> 0.00)."""
    dec = validate_currency_paise(v, field_name=field_name, allow_none=False)
    if dec is None or dec <= Decimal("0.00"):
        raise ValueError(f"{field_name} must be strictly positive (> 0.00). Got: {dec}")
    return dec


def validate_quantity_precision(
    v: Any,
    field_name: str = "qty",
) -> Decimal:
    """
    Validates inventory quantity:
    - Must be strictly positive (> 0).
    - Respects established four-decimal precision.
    - Never silently rounds input.
    """
    if v is None:
        raise ValueError(f"{field_name} cannot be None")

    try:
        dec = Decimal(str(v))
    except Exception as exc:
        raise ValueError(f"Invalid quantity value for {field_name}: {v}") from exc

    if dec <= Decimal("0"):
        raise ValueError(f"{field_name} must be strictly positive (> 0). Got: {v}")
    if dec.as_tuple().exponent < -4:
        raise ValueError(
            f"{field_name} cannot have more than 4 decimal places. Got: {v}"
        )

    return dec.quantize(Decimal("0.0001"))


def validate_tax_rate_precision(
    v: Any,
    field_name: str = "rate",
    allow_none: bool = True,
) -> Optional[Decimal]:
    """Validates percentage tax rate (>= 0)."""
    if v is None:
        if allow_none:
            return None
        return Decimal("0.00")

    try:
        dec = Decimal(str(v))
    except Exception as exc:
        raise ValueError(f"Invalid tax rate for {field_name}: {v}") from exc

    if dec < Decimal("0"):
        raise ValueError(f"{field_name} cannot be negative. Got: {dec}")
    if dec.as_tuple().exponent < -4:
        raise ValueError(f"{field_name} cannot have more than 4 decimal places. Got: {v}")

    return dec


# ============================================================================
# 1. Input / Creation Models (Client-facing or Service Inward Requests)
# ============================================================================

class PurchaseReturnItemCreate(BaseModel):
    """
    Line item creation payload.
    Does not allow calculated amounts from client input; the repository / domain
    derives taxable amount, tax rates, and totals directly from original purchase_items.
    """
    purchase_item_id: int
    product_id: int
    batch_id: Optional[int] = None
    qty: Decimal
    reason: Optional[str] = None

    @field_validator("qty", mode="before")
    @classmethod
    def check_qty(cls, v: Any) -> Decimal:
        return validate_quantity_precision(v, field_name="qty")


class PurchaseReturnSettlementCreate(BaseModel):
    """
    Settlement allocation creation payload (client-facing).
    Does not accept credit_id from client input; supplier credits are generated
    and linked internally by the repository / domain service after ID generation.
    """
    settlement_type: str
    amount: Decimal
    account_id: Optional[int] = None
    reference_no: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("settlement_type", mode="before")
    @classmethod
    def check_settlement_type(cls, v: Any) -> str:
        if not v or not isinstance(v, str):
            raise ValueError(f"Invalid settlement_type: {v!r}")
        st = v.strip().upper()
        if st not in CANONICAL_SETTLEMENT_TYPES:
            raise ValueError(
                f"Unsupported settlement_type: '{v}'. Must be one of {CANONICAL_SETTLEMENT_TYPES}."
            )
        return st

    @field_validator("amount", mode="before")
    @classmethod
    def check_amount(cls, v: Any) -> Decimal:
        return validate_positive_currency_paise(v, field_name="amount")

    @model_validator(mode="after")
    def validate_account_requirements(self) -> PurchaseReturnSettlementCreate:
        if self.settlement_type in ("REFUND_CASH", "REFUND_BANK") and not self.account_id:
            raise ValueError(
                f"account_id is required for settlement type {self.settlement_type}"
            )
        return self


class PurchaseReturnCreate(BaseModel):
    """
    Purchase return creation payload.
    - Requires non-empty idempotency_key for idempotency safety.
    - Does not accept client-provided request_fingerprint (computed deterministically).
    - Enforces at most one SUPPLIER_CREDIT settlement allocation per return.
    """
    purchase_id: int
    supplier_id: int
    return_date: str
    idempotency_key: str
    return_no: Optional[str] = None  # Strictly system-generated; client-supplied values rejected
    supplier_credit_note_no: Optional[str] = None
    supplier_credit_note_date: Optional[str] = None
    remarks: Optional[str] = None
    itc_status: Optional[str] = "CLAIMED_REVERSED"
    items: List[PurchaseReturnItemCreate] = Field(default_factory=list)
    settlements: List[PurchaseReturnSettlementCreate] = Field(default_factory=list)

    @field_validator("return_no", mode="before")
    @classmethod
    def reject_client_return_no(cls, v: Any) -> Optional[str]:
        if v is not None and str(v).strip():
            raise ValueError(
                "Client-supplied return_no is not permitted; return numbers are strictly system-generated."
            )
        return None

    @field_validator("idempotency_key")
    @classmethod
    def check_idempotency_key(cls, v: Any) -> str:
        if not v or not isinstance(v, str) or not v.strip():
            raise ValueError("idempotency_key is required and cannot be empty for new purchase returns.")
        return v.strip()

    @field_validator("itc_status", mode="before")
    @classmethod
    def check_initial_itc_status(cls, v: Any) -> str:
        if v is None:
            return "CLAIMED_REVERSED"
        if not str(v).strip():
            raise ValueError(
                f"itc_status cannot be empty. Supported creation statuses are: {INITIAL_ITC_STATUSES}."
            )
        val = str(v).strip().upper()
        if val == "CONFIRMED_REVERSED":
            raise ValueError(
                "CONFIRMED_REVERSED is a post-audit status and cannot be accepted as an initial creation status."
            )
        if val not in INITIAL_ITC_STATUSES:
            raise ValueError(
                f"Invalid initial ITC status: '{v}'. Supported creation statuses are: {INITIAL_ITC_STATUSES}."
            )
        return val

    @model_validator(mode="before")
    @classmethod
    def reject_untrusted_fingerprint(cls, data: Any) -> Any:
        if isinstance(data, dict) and "request_fingerprint" in data and data["request_fingerprint"]:
            raise ValueError(
                "Client-provided request_fingerprint is not accepted as a trusted value; "
                "fingerprints are computed internally from canonical payload fields."
            )
        return data

    @model_validator(mode="after")
    def validate_return_integrity(self) -> PurchaseReturnCreate:
        if not self.items:
            raise ValueError("Purchase return must contain at least one return item.")

        # Enforce one-supplier-credit-allocation-per-return
        sc_count = sum(1 for s in self.settlements if s.settlement_type == "SUPPLIER_CREDIT")
        if sc_count > 1:
            raise ValueError(
                f"A purchase return cannot have more than one SUPPLIER_CREDIT allocation. Found {sc_count}."
            )
        return self

    def compute_request_fingerprint(self) -> str:
        """
        Computes a deterministic SHA-256 fingerprint of the return request payload.
        Includes all business-critical header fields, items, settlements, and tax handling.
        Does NOT include the idempotency_key itself (which identifies the request, not content).
        Uses integer paise for currency amounts and fixed 4-decimal strings for quantities.
        """
        def _clean_str(v: Optional[str]) -> Optional[str]:
            if v is None:
                return None
            s = str(v).strip()
            return s if s else None

        canonical_items = [
            {
                "purchase_item_id": itm.purchase_item_id,
                "product_id": itm.product_id,
                "batch_id": itm.batch_id,
                "quantity": f"{Decimal(str(itm.qty)):.4f}",
                "reason": _clean_str(itm.reason),
            }
            for itm in self.items
        ]

        canonical_settlements = [
            {
                "settlement_type": s.settlement_type,
                "amount_paise": int((Decimal(str(s.amount)) * Decimal("100")).quantize(Decimal("1"))),
                "account_id": s.account_id,
                "reference_no": _clean_str(s.reference_no),
                "notes": _clean_str(s.notes),
            }
            for s in self.settlements
        ]

        payload = {
            "purchase_id": self.purchase_id,
            "supplier_id": self.supplier_id,
            "return_date": self.return_date,
            "supplier_credit_note_no": _clean_str(self.supplier_credit_note_no),
            "supplier_credit_note_date": _clean_str(self.supplier_credit_note_date),
            "remarks": _clean_str(self.remarks),
            "itc_status": self.itc_status,
            "items": canonical_items,
            "settlements": canonical_settlements,
        }
        data_str = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(data_str.encode("utf-8")).hexdigest()


class PurchaseReturnItemPreview(BaseModel):
    """Single purchase return line item request for calculation preview."""
    purchase_item_id: int
    qty: Decimal = Field(..., gt=0)
    product_id: Optional[int] = None
    batch_id: Optional[int] = None
    reason: Optional[str] = None

    @field_validator("qty", mode="before")
    @classmethod
    def check_qty(cls, v: Any) -> Decimal:
        return validate_quantity_precision(v, field_name="qty")


class PurchaseReturnPreviewRequest(BaseModel):
    """Request payload for authoritative backend return preview calculation."""
    purchase_id: int
    supplier_id: Optional[int] = None
    items: List[PurchaseReturnItemPreview] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_items_present(self) -> PurchaseReturnPreviewRequest:
        if not self.items:
            raise ValueError("At least one purchase return item is required for preview.")
        item_ids = [itm.purchase_item_id for itm in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Duplicate purchase_item_id entries found in preview items list.")
        return self


class PurchaseReturnItemPreviewResult(BaseModel):
    """Authoritative calculated line item in return preview."""
    purchase_item_id: int
    product_id: int
    product_name: Optional[str] = None
    batch_id: int
    batch_no: str
    qty: Decimal
    purchase_rate: Decimal
    discount_amount: Decimal
    net_rate: Decimal
    taxable_amount: Decimal
    cgst_rate: Decimal
    cgst_amount: Decimal
    sgst_rate: Decimal
    sgst_amount: Decimal
    igst_rate: Decimal
    igst_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    reason: Optional[str] = None


class PurchaseReturnPreviewResponse(BaseModel):
    """Authoritative response containing exact calculation breakdown, rounding, and supplier balance."""
    success: bool = True
    purchase_id: int
    supplier_id: int
    supplier_name: Optional[str] = None
    supplier_balance: Decimal
    total_taxable: Decimal
    total_cgst: Decimal
    total_sgst: Decimal
    total_igst: Decimal
    total_tax: Decimal
    gross_total: Decimal
    round_off: Decimal
    net_amount: Decimal
    items: List[PurchaseReturnItemPreviewResult] = Field(default_factory=list)


class SupplierCreditCreate(BaseModel):
    """Supplier credit note creation payload."""
    credit_no: str
    supplier_id: int
    return_id: int
    credit_date: str
    original_amount: Decimal
    balance_amount: Optional[Decimal] = None
    notes: Optional[str] = None

    @field_validator("original_amount", mode="before")
    @classmethod
    def check_original_amount(cls, v: Any) -> Decimal:
        return validate_positive_currency_paise(v, field_name="original_amount")

    @field_validator("balance_amount", mode="before")
    @classmethod
    def check_balance_amount(cls, v: Any) -> Optional[Decimal]:
        if v is None:
            return None
        return validate_currency_paise(v, field_name="balance_amount", min_value=Decimal("0.00"))

    @model_validator(mode="after")
    def default_balance_amount(self) -> SupplierCreditCreate:
        if self.balance_amount is None:
            self.balance_amount = self.original_amount
        elif self.balance_amount > self.original_amount:
            raise ValueError(
                f"balance_amount ({self.balance_amount}) cannot exceed original_amount ({self.original_amount})"
            )
        return self


# ============================================================================
# 2. Database-Read / Persisted Domain Models
# ============================================================================

class PurchaseReturnItem(BaseModel):
    """
    Persisted Purchase Return Line Item.
    Preserves NULL for historical snapshot fields when loading pre-v4 rows.
    """
    return_item_id: Optional[int] = None
    return_id: Optional[int] = None
    purchase_item_id: Optional[int] = None
    product_id: int
    batch_id: Optional[int] = None
    qty: Decimal
    purchase_rate: Decimal
    discount_amount: Optional[Decimal] = None
    net_rate: Optional[Decimal] = None
    taxable_amount: Decimal
    cgst_rate: Optional[Decimal] = None
    cgst_amount: Optional[Decimal] = None
    sgst_rate: Optional[Decimal] = None
    sgst_amount: Optional[Decimal] = None
    igst_rate: Optional[Decimal] = None
    igst_amount: Optional[Decimal] = None
    tax_amount: Decimal = Decimal("0.00")
    total_amount: Decimal
    reason: Optional[str] = None

    # Auxiliary display metadata (optional joins)
    product_name: Optional[str] = None
    batch_no: Optional[str] = None

    @field_validator("qty", mode="before")
    @classmethod
    def check_qty(cls, v: Any) -> Decimal:
        return validate_quantity_precision(v, field_name="qty")

    @field_validator("purchase_rate", "taxable_amount", "total_amount", mode="before")
    @classmethod
    def check_mandatory_money(cls, v: Any, info) -> Decimal:
        return validate_currency_paise(v, field_name=info.field_name, min_value=Decimal("0.00"))

    @field_validator("tax_amount", mode="before")
    @classmethod
    def check_tax_amount(cls, v: Any) -> Decimal:
        if v is None:
            return Decimal("0.00")
        return validate_currency_paise(v, field_name="tax_amount", min_value=Decimal("0.00"))

    @field_validator(
        "discount_amount",
        "net_rate",
        "cgst_amount",
        "sgst_amount",
        "igst_amount",
        mode="before",
    )
    @classmethod
    def check_nullable_money_snapshots(cls, v: Any, info) -> Optional[Decimal]:
        return validate_currency_paise(
            v, field_name=info.field_name, allow_none=True, min_value=Decimal("0.00")
        )

    @field_validator("cgst_rate", "sgst_rate", "igst_rate", mode="before")
    @classmethod
    def check_nullable_rate_snapshots(cls, v: Any, info) -> Optional[Decimal]:
        return validate_tax_rate_precision(v, field_name=info.field_name, allow_none=True)


class PurchaseReturnSettlement(BaseModel):
    """Persisted Purchase Return Multi-Settlement Allocation."""
    settlement_id: Optional[int] = None
    return_id: Optional[int] = None
    settlement_type: str
    amount: Decimal
    account_id: Optional[int] = None
    credit_id: Optional[int] = None
    reference_no: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None

    # Auxiliary display metadata
    account_name: Optional[str] = None
    credit_no: Optional[str] = None

    @field_validator("settlement_type", mode="before")
    @classmethod
    def check_type(cls, v: Any) -> str:
        st = str(v).strip().upper()
        if st not in CANONICAL_SETTLEMENT_TYPES:
            raise ValueError(f"Unknown settlement_type: {v}")
        return st

    @field_validator("amount", mode="before")
    @classmethod
    def check_amount(cls, v: Any) -> Decimal:
        return validate_positive_currency_paise(v, field_name="amount")


class PurchaseReturn(BaseModel):
    """
    Persisted Purchase Return Header.
    Preserves NULL for historical snapshot fields when loading pre-v4 rows.
    """
    return_id: Optional[int] = None
    return_no: str
    return_date: str
    purchase_id: int
    supplier_id: int
    total_taxable: Decimal = Decimal("0.00")
    total_cgst: Optional[Decimal] = None
    total_sgst: Optional[Decimal] = None
    total_igst: Optional[Decimal] = None
    total_tax: Decimal = Decimal("0.00")
    round_off: Decimal = Decimal("0.00")
    net_amount: Decimal
    supplier_credit_note_no: Optional[str] = None
    supplier_credit_note_date: Optional[str] = None
    remarks: Optional[str] = None
    status: Optional[str] = "POSTED"
    itc_status: Optional[str] = "CLAIMED_REVERSED"
    voucher_id: Optional[int] = None
    itc_adjustment_voucher_id: Optional[int] = None
    reversal_voucher_id: Optional[int] = None
    itc_adjustment_reversal_voucher_id: Optional[int] = None
    cancelled_at: Optional[str] = None
    cancel_reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    request_fingerprint: Optional[str] = None
    created_at: Optional[str] = None

    # Associated nested records
    items: List[PurchaseReturnItem] = Field(default_factory=list)
    settlements: List[PurchaseReturnSettlement] = Field(default_factory=list)

    # Auxiliary display metadata (optional joins)
    supplier_name: Optional[str] = None
    supplier_gstin: Optional[str] = None
    purchase_invoice_no: Optional[str] = None

    @field_validator("total_taxable", "total_tax", "net_amount", mode="before")
    @classmethod
    def check_mandatory_money(cls, v: Any, info) -> Decimal:
        return validate_currency_paise(v, field_name=info.field_name, min_value=Decimal("0.00"))

    @field_validator("round_off", mode="before")
    @classmethod
    def check_round_off(cls, v: Any) -> Decimal:
        if v is None:
            return Decimal("0.00")
        return validate_currency_paise(v, field_name="round_off")

    @field_validator("total_cgst", "total_sgst", "total_igst", mode="before")
    @classmethod
    def check_nullable_tax_totals(cls, v: Any, info) -> Optional[Decimal]:
        return validate_currency_paise(
            v, field_name=info.field_name, allow_none=True, min_value=Decimal("0.00")
        )

    @field_validator("status", mode="before")
    @classmethod
    def check_status(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        val = str(v).strip().upper()
        if val not in RETURN_STATUSES:
            raise ValueError(f"Invalid return status: {v}. Must be one of {RETURN_STATUSES}.")
        return val

    @field_validator("itc_status", mode="before")
    @classmethod
    def check_itc_status(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        val = str(v).strip().upper()
        if val not in ALL_ITC_STATUSES:
            raise ValueError(f"Invalid itc_status: {v}. Must be one of {ALL_ITC_STATUSES}.")
        return val


class SupplierCredit(BaseModel):
    """
    Persisted Supplier Credit Note Record.
    """
    credit_id: Optional[int] = None
    credit_no: str
    supplier_id: int
    return_id: int
    credit_date: str
    original_amount: Decimal
    balance_amount: Decimal
    status: str = "AVAILABLE"
    notes: Optional[str] = None
    created_at: Optional[str] = None

    # Auxiliary display metadata
    supplier_name: Optional[str] = None
    return_no: Optional[str] = None

    @field_validator("original_amount", mode="before")
    @classmethod
    def check_orig(cls, v: Any) -> Decimal:
        return validate_positive_currency_paise(v, field_name="original_amount")

    @field_validator("balance_amount", mode="before")
    @classmethod
    def check_bal(cls, v: Any) -> Decimal:
        return validate_currency_paise(v, field_name="balance_amount", min_value=Decimal("0.00"))

    @field_validator("status", mode="before")
    @classmethod
    def check_credit_status(cls, v: Any) -> str:
        val = str(v).strip().upper()
        if val not in SUPPLIER_CREDIT_STATUSES:
            raise ValueError(
                f"Invalid supplier credit status: '{v}'. Must be one of {SUPPLIER_CREDIT_STATUSES}."
            )
        return val

    @model_validator(mode="after")
    def validate_balance_bounds(self) -> SupplierCredit:
        if self.balance_amount > self.original_amount:
            raise ValueError(
                f"balance_amount ({self.balance_amount}) cannot exceed original_amount ({self.original_amount})"
            )
        return self
