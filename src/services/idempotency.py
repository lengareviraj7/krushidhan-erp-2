"""
Compatibility re-export of shared idempotency utilities from `src.idempotency`.

The canonical dependency-light definitions live in `src/idempotency.py` so model and
repository modules do not trigger import cycles through `src.services.__init__`.
"""
from __future__ import annotations

from src.idempotency import (
    DuplicateInvoiceError,
    IdempotencyConflictError,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    clean_str,
    decimal_to_fixed4,
    decimal_to_paise,
    reject_client_request_fingerprint,
    resolve_request_idempotency_key,
    sha256_canonical_json,
    validate_optional_idempotency_key,
)

__all__ = [
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "IdempotencyConflictError",
    "DuplicateInvoiceError",
    "validate_optional_idempotency_key",
    "resolve_request_idempotency_key",
    "reject_client_request_fingerprint",
    "clean_str",
    "decimal_to_paise",
    "decimal_to_fixed4",
    "sha256_canonical_json",
]
