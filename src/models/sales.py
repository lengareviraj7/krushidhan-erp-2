"""
Domain Data Models for Sales Billing, Invoicing, and Sales Returns.
"""
from __future__ import annotations
from typing import Any, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

from src.idempotency import (
    clean_str,
    decimal_to_fixed4,
    decimal_to_paise,
    reject_client_request_fingerprint,
    sha256_canonical_json,
    validate_optional_idempotency_key,
)


class SaleItem(BaseModel):
    sale_item_id: Optional[int] = None
    sale_id: Optional[int] = None
    product_id: int
    batch_id: Optional[int] = 0

    product_name: Optional[str] = None
    batch_no: Optional[str] = None
    exp_date: Optional[str] = None
    hsn_code: Optional[str] = None
    qty: float
    unit_id: Optional[int] = None
    unit_name: Optional[str] = None
    sale_rate: float
    mrp: float
    discount_percent: float = 0.0
    discount_amount: float = 0.0
    taxable_amount: float
    cgst_rate: float = 0.0
    cgst_amount: float = 0.0
    sgst_rate: float = 0.0
    sgst_amount: float = 0.0
    igst_rate: float = 0.0
    igst_amount: float = 0.0
    total_amount: float
    created_at: Optional[str] = None


class Sale(BaseModel):
    sale_id: Optional[int] = None
    invoice_no: Optional[str] = ""
    sale_date: Optional[str] = None
    customer_id: int
    customer_name: Optional[str] = None
    customer_mobile: Optional[str] = None
    customer_village: Optional[str] = None
    customer_taluka: Optional[str] = None
    customer_district: Optional[str] = None
    customer_state: Optional[str] = None
    customer_gstin: Optional[str] = None
    doctor_or_officer: Optional[str] = None
    payment_mode: str = "CASH"  # 'CASH', 'CREDIT', 'UPI', 'CARD', 'SPLIT'
    total_taxable: float = 0.0
    total_cgst: float = 0.0
    total_sgst: float = 0.0
    total_igst: float = 0.0
    total_discount: float = 0.0
    extra_charges: float = 0.0
    round_off: float = 0.0
    net_amount: float
    paid_amount: float = 0.0
    due_amount: float = 0.0
    print_count: int = 0
    remarks: Optional[str] = None
    idempotency_key: Optional[str] = None
    items: List[SaleItem] = Field(default_factory=list)
    created_at: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_untrusted_fingerprint(cls, data: Any) -> Any:
        return reject_client_request_fingerprint(data)

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def _validate_idempotency_key(cls, v: Any) -> Optional[str]:
        return validate_optional_idempotency_key(v)

    def compute_request_fingerprint(self) -> str:
        """
        Compute deterministic SHA-256 fingerprint strictly from client-supplied business fields
        BEFORE any server defaults (such as generated invoice_no, defaulted sale_date, or
        server-resolved batch_id/batch_no/exp_date) are populated.
        Excludes idempotency_key itself. Item order as submitted is preserved.
        """
        canonical_items = [
            {
                "product_id": int(itm.product_id),
                "batch_id": int(itm.batch_id) if itm.batch_id and itm.batch_id > 0 else None,
                "batch_no": clean_str(itm.batch_no),
                "exp_date": clean_str(itm.exp_date),
                "qty": decimal_to_fixed4(itm.qty),
                "unit_id": itm.unit_id,
                "sale_rate_paise": decimal_to_paise(itm.sale_rate),
                "mrp_paise": decimal_to_paise(itm.mrp),
                "discount_percent": decimal_to_fixed4(itm.discount_percent),
                "discount_amount_paise": decimal_to_paise(itm.discount_amount),
                "taxable_amount_paise": decimal_to_paise(itm.taxable_amount),
                "cgst_rate": decimal_to_fixed4(itm.cgst_rate),
                "cgst_amount_paise": decimal_to_paise(itm.cgst_amount),
                "sgst_rate": decimal_to_fixed4(itm.sgst_rate),
                "sgst_amount_paise": decimal_to_paise(itm.sgst_amount),
                "igst_rate": decimal_to_fixed4(itm.igst_rate),
                "igst_amount_paise": decimal_to_paise(itm.igst_amount),
                "total_amount_paise": decimal_to_paise(itm.total_amount),
            }
            for itm in self.items
        ]
        payload = {
            "invoice_no": clean_str(self.invoice_no),
            "sale_date": clean_str(self.sale_date),
            "customer_id": int(self.customer_id),
            "doctor_or_officer": clean_str(self.doctor_or_officer),
            "payment_mode": (clean_str(self.payment_mode) or "CASH").upper(),
            "total_taxable_paise": decimal_to_paise(self.total_taxable),
            "total_cgst_paise": decimal_to_paise(self.total_cgst),
            "total_sgst_paise": decimal_to_paise(self.total_sgst),
            "total_igst_paise": decimal_to_paise(self.total_igst),
            "total_discount_paise": decimal_to_paise(self.total_discount),
            "extra_charges_paise": decimal_to_paise(self.extra_charges),
            "round_off_paise": decimal_to_paise(self.round_off),
            "net_amount_paise": decimal_to_paise(self.net_amount),
            "paid_amount_paise": decimal_to_paise(self.paid_amount),
            "due_amount_paise": decimal_to_paise(self.due_amount),
            "remarks": clean_str(self.remarks),
            "items": canonical_items,
        }
        return sha256_canonical_json(payload)


class SalesReturnItem(BaseModel):
    return_item_id: Optional[int] = None
    return_id: Optional[int] = None
    product_id: int
    batch_id: Optional[int] = None
    qty: float
    sale_rate: float
    taxable_amount: float
    tax_amount: float = 0.0
    total_amount: float
    reason: Optional[str] = None


class SalesReturnPreviewItem(BaseModel):
    product_id: int
    batch_id: Optional[int] = None
    qty: float
    reason: Optional[str] = None


class SalesReturnPreviewRequest(BaseModel):
    sale_id: int
    items: List[SalesReturnPreviewItem] = Field(default_factory=list)


class SalesReturn(BaseModel):
    return_id: Optional[int] = None
    return_no: Optional[str] = ""
    return_date: Optional[str] = None
    sale_id: Optional[int] = None
    customer_id: int
    total_taxable: float = 0.0
    total_tax: float = 0.0
    round_off: float = 0.0
    net_amount: float
    remarks: Optional[str] = None
    idempotency_key: Optional[str] = None
    items: List[SalesReturnItem] = Field(default_factory=list)
    created_at: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_untrusted_fingerprint(cls, data: Any) -> Any:
        return reject_client_request_fingerprint(data)

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def _validate_idempotency_key(cls, v: Any) -> Optional[str]:
        return validate_optional_idempotency_key(v)

    def compute_request_fingerprint(self) -> str:
        """
        Compute deterministic SHA-256 fingerprint strictly from client-supplied business fields
        BEFORE any server defaults (generated return_no, defaulted return_date, resolved batch_id)
        are applied. Excludes idempotency_key itself.
        """
        canonical_items = [
            {
                "product_id": int(itm.product_id),
                "batch_id": int(itm.batch_id) if itm.batch_id and itm.batch_id > 0 else None,
                "qty": decimal_to_fixed4(itm.qty),
                "sale_rate_paise": decimal_to_paise(itm.sale_rate),
                "taxable_amount_paise": decimal_to_paise(itm.taxable_amount),
                "tax_amount_paise": decimal_to_paise(itm.tax_amount),
                "total_amount_paise": decimal_to_paise(itm.total_amount),
                "reason": clean_str(itm.reason),
            }
            for itm in self.items
        ]
        payload = {
            "operation": "SALE_RETURN",
            "return_no": clean_str(self.return_no),
            "return_date": clean_str(self.return_date),
            "sale_id": int(self.sale_id) if self.sale_id is not None else None,
            "customer_id": int(self.customer_id),
            "total_taxable_paise": decimal_to_paise(self.total_taxable),
            "total_tax_paise": decimal_to_paise(self.total_tax),
            "round_off_paise": decimal_to_paise(self.round_off),
            "net_amount_paise": decimal_to_paise(self.net_amount),
            "remarks": clean_str(self.remarks),
            "items": canonical_items,
        }
        return sha256_canonical_json(payload)
