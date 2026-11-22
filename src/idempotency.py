"""
Shared Dependency-Light Idempotency Utilities and Domain Exceptions for Krushidhan ERP.

Kept outside `src.services` (stdlib-only, zero internal imports) so domain models,
repositories, routes, and services can share these primitives without triggering
import cycles through `src.services.__init__`.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional, Sequence

MAX_IDEMPOTENCY_KEY_LENGTH = 128


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency_key is reused with a different request payload (HTTP 409 Conflict)."""


class DuplicateInvoiceError(ValueError):
    """Raised when an explicit invoice_no already exists and is not an idempotent replay (HTTP 409 Conflict)."""


def validate_optional_idempotency_key(value: Any) -> Optional[str]:
    """
    Validate an optional idempotency_key (from JSON body or HTTP Idempotency-Key header).
    When provided (not None):
    - must be a string
    - trimmed non-empty
    - at most 128 characters
    - printable ASCII only (0x20..0x7E)
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("idempotency_key must be a string.")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("idempotency_key cannot be empty or whitespace.")
    if len(trimmed) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(
            f"idempotency_key exceeds maximum length of {MAX_IDEMPOTENCY_KEY_LENGTH} characters."
        )
    if any(ord(ch) < 32 or ord(ch) > 126 for ch in trimmed):
        raise ValueError("idempotency_key must contain only printable ASCII characters.")
    return trimmed


def resolve_request_idempotency_key(
    body_key: Any,
    header_key: Optional[str] = None,
    header_values: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """
    Validate and resolve optional idempotency key from JSON body and HTTP Idempotency-Key header.

    Rules:
    - Both missing -> returns None (legacy compatibility preserved)
    - Blank or invalid in either body or header -> raises ValueError (HTTP 400)
    - Body Key A, Header missing -> returns Key A
    - Body missing, Header Key A -> returns Key A
    - Body Key A, Header Key A -> returns Key A
    - Body Key A, Header Key B (or multiple mismatched headers) -> raises ValueError (HTTP 400)
    """
    validated_body = validate_optional_idempotency_key(body_key)

    validated_header: Optional[str] = None
    if header_values is not None and len(header_values) > 0:
        for raw_hv in header_values:
            hv = validate_optional_idempotency_key(raw_hv)
            if validated_header is not None and hv != validated_header:
                raise ValueError(
                    "Conflicting Idempotency-Key headers: multiple differing header values were supplied."
                )
            validated_header = hv
    elif header_key is not None:
        validated_header = validate_optional_idempotency_key(header_key)

    if validated_body is not None and validated_header is not None:
        if validated_body != validated_header:
            raise ValueError(
                "Conflicting idempotency keys: JSON body idempotency_key does not match HTTP Idempotency-Key header."
            )
        return validated_body

    return validated_body if validated_body is not None else validated_header


def reject_client_request_fingerprint(data: Any) -> Any:
    """Reject any client-supplied request_fingerprint field in incoming payloads."""
    if isinstance(data, dict) and "request_fingerprint" in data and data["request_fingerprint"] is not None:
        raise ValueError(
            "Client-provided request_fingerprint is not accepted as a trusted value; "
            "fingerprints are computed internally from canonical payload fields."
        )
    return data


def clean_str(value: Any) -> Optional[str]:
    """Normalize optional string by stripping whitespace; empty strings normalize to None."""
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned if cleaned else None


def decimal_to_paise(value: Any) -> Optional[int]:
    """Convert numeric currency amount to exact integer paise using Decimal ROUND_HALF_UP."""
    if value is None:
        return None
    dec = Decimal(str(value))
    return int((dec * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def decimal_to_fixed4(value: Any) -> Optional[str]:
    """Convert numeric quantity/rate to deterministic 4-decimal string using Decimal ROUND_HALF_UP."""
    if value is None:
        return None
    dec = Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return f"{dec:.4f}"


def sha256_canonical_json(payload: Dict[str, Any]) -> str:
    """Compute SHA-256 hex digest over sorted-key canonical JSON with fixed compact separators."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
