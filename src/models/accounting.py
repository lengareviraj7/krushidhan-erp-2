"""
Domain Data Models for Chart of Accounts, Vouchers, Ledger Entries, and Expenses.
"""
from __future__ import annotations
from typing import Any, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

from src.idempotency import (
    reject_client_request_fingerprint,
    validate_optional_idempotency_key,
)


class LedgerAccount(BaseModel):
    account_id: Optional[int] = None
    account_name: str
    account_group: str  # 'ASSETS', 'LIABILITIES', 'INCOME', 'EXPENSE', 'BANK_ACCOUNTS', 'CASH'
    opening_balance: float = 0.0
    current_balance: float = 0.0
    is_system: int = 0
    created_at: Optional[str] = None


class LedgerEntry(BaseModel):
    entry_id: Optional[int] = None
    voucher_id: Optional[int] = None
    account_id: int
    account_name: Optional[str] = None
    debit_amount: float = 0.0
    credit_amount: float = 0.0
    particulars: Optional[str] = None
    created_at: Optional[str] = None


class Voucher(BaseModel):
    voucher_id: Optional[int] = None
    voucher_no: str
    voucher_date: str
    voucher_type: str  # 'RECEIPT', 'PAYMENT', 'CONTRA', 'JOURNAL', 'DEBIT_NOTE', 'CREDIT_NOTE'
    total_amount: float
    narration: Optional[str] = None
    reference_type: Optional[str] = None  # 'SALE', 'PURCHASE', 'EXPENSE', 'MANUAL'
    reference_id: Optional[int] = None
    idempotency_key: Optional[str] = None
    entries: List[LedgerEntry] = Field(default_factory=list)
    created_at: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_untrusted_fingerprint(cls, data: Any) -> Any:
        return reject_client_request_fingerprint(data)

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def _validate_idempotency_key(cls, v: Any) -> Optional[str]:
        return validate_optional_idempotency_key(v)


class ExpenseCategory(BaseModel):
    category_id: Optional[int] = None
    category_name: str
    description: Optional[str] = None
    created_at: Optional[str] = None


class Expense(BaseModel):
    expense_id: Optional[int] = None
    category_id: int
    category_name: Optional[str] = None
    expense_date: Optional[str] = None
    amount: float
    payment_account_id: Optional[int] = None
    reference_no: Optional[str] = None
    remarks: Optional[str] = None
    idempotency_key: Optional[str] = None
    created_at: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_untrusted_fingerprint(cls, data: Any) -> Any:
        return reject_client_request_fingerprint(data)

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def _validate_idempotency_key(cls, v: Any) -> Optional[str]:
        return validate_optional_idempotency_key(v)
