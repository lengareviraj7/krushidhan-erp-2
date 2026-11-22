"""
Domain Data Models for Inward Purchases and Purchase Returns.
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


class PurchaseItem(BaseModel):
    item_id: Optional[int] = None
    purchase_id: Optional[int] = None
    product_id: int
    batch_no: str
    mfg_date: Optional[str] = None
    exp_date: Optional[str] = None
    qty: float
    free_qty: float = 0.0
    unit_id: Optional[int] = None
    purchase_rate: float
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


class Purchase(BaseModel):
    purchase_id: Optional[int] = None
    invoice_no: str
    purchase_date: Optional[str] = None
    supplier_id: int
    challan_no: Optional[str] = None
    payment_type: str = "CREDIT"  # 'CASH', 'CREDIT', 'BANK_TRANSFER'
    total_taxable: float = 0.0
    total_cgst: float = 0.0
    total_sgst: float = 0.0
    total_igst: float = 0.0
    extra_charges: float = 0.0
    round_off: float = 0.0
    net_amount: float
    paid_amount: float = 0.0
    due_amount: float = 0.0
    remarks: Optional[str] = None
    idempotency_key: Optional[str] = None
    items: List[PurchaseItem] = Field(default_factory=list)
    created_at: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_untrusted_fingerprint(cls, data: Any) -> Any:
        return reject_client_request_fingerprint(data)

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def _validate_idempotency_key(cls, v: Any) -> Optional[str]:
        return validate_optional_idempotency_key(v)

    @field_validator("invoice_no", mode="before")
    @classmethod
    def _normalize_invoice_no(cls, v: Any) -> str:
        if v is None or not isinstance(v, str) or not v.strip():
            raise ValueError("Purchase invoice_no is required and cannot be blank.")
        return v.strip()

    def compute_request_fingerprint(self) -> str:
        """
        Compute deterministic SHA-256 fingerprint strictly from client-supplied business fields
        BEFORE any server defaults (such as defaulted purchase_date) are populated.
        Excludes idempotency_key itself. Item order as submitted is preserved.
        """
        canonical_items = [
            {
                "product_id": int(itm.product_id),
                "batch_no": clean_str(itm.batch_no),
                "mfg_date": clean_str(itm.mfg_date),
                "exp_date": clean_str(itm.exp_date),
                "qty": decimal_to_fixed4(itm.qty),
                "free_qty": decimal_to_fixed4(itm.free_qty),
                "unit_id": itm.unit_id,
                "purchase_rate_paise": decimal_to_paise(itm.purchase_rate),
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
            "purchase_date": clean_str(self.purchase_date),
            "supplier_id": int(self.supplier_id),
            "challan_no": clean_str(self.challan_no),
            "payment_type": (clean_str(self.payment_type) or "CREDIT").upper(),
            "total_taxable_paise": decimal_to_paise(self.total_taxable),
            "total_cgst_paise": decimal_to_paise(self.total_cgst),
            "total_sgst_paise": decimal_to_paise(self.total_sgst),
            "total_igst_paise": decimal_to_paise(self.total_igst),
            "extra_charges_paise": decimal_to_paise(self.extra_charges),
            "round_off_paise": decimal_to_paise(self.round_off),
            "net_amount_paise": decimal_to_paise(self.net_amount),
            "paid_amount_paise": decimal_to_paise(self.paid_amount),
            "due_amount_paise": decimal_to_paise(self.due_amount),
            "remarks": clean_str(self.remarks),
            "items": canonical_items,
        }
        return sha256_canonical_json(payload)


# Re-export rich purchase return domain models from dedicated module
from src.models.purchase_return import (
    PurchaseReturn,
    PurchaseReturnCreate,
    PurchaseReturnItem,
    PurchaseReturnItemCreate,
    PurchaseReturnSettlement,
    PurchaseReturnSettlementCreate,
    SupplierCredit,
    SupplierCreditCreate,
)
