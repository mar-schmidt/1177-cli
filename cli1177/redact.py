"""Redaction helpers for machine-readable output."""

from __future__ import annotations

from collections.abc import Mapping

SENSITIVE_KEYS = {
    "cookie",
    "cookies",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "personnummer",
    "pnr",
}


def _redact_value(value: object) -> object:
    if isinstance(value, Mapping):
        return redact_payload(dict(value))
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def redact_payload(payload: dict[str, object]) -> dict[str, object]:
    """Remove sensitive values from payloads before print/logging."""
    output: dict[str, object] = {}
    for key, value in payload.items():
        if key.lower() in SENSITIVE_KEYS:
            output[key] = "***"
            continue
        output[key] = _redact_value(value)
    return output

