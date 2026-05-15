"""Output rendering utilities."""

from __future__ import annotations

import json
import sys
from typing import Any

from cli1177.redact import redact_payload


def print_success(payload: dict[str, Any]) -> None:
    """Print success JSON payload to stdout."""
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def print_success_formatted(
    payload: dict[str, Any],
    output_format: str,
) -> None:
    """Print success payload with configurable output format."""
    safe_payload = redact_payload(payload)
    if output_format == "text":
        json.dump(safe_payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    print_success(safe_payload)


def print_error(error: str, code: str, details: dict[str, Any]) -> None:
    """Print stable error JSON payload to stderr."""
    safe_details = redact_payload(details)
    payload = {
        "error": error,
        "code": code,
        "details": safe_details,
    }
    json.dump(payload, sys.stderr, ensure_ascii=False)
    sys.stderr.write("\n")

