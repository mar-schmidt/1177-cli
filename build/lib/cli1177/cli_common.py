"""Shared command execution helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click
import typer

from cli1177 import exit_codes
from cli1177.errors import CliError
from cli1177.output import print_error, print_success_formatted
from cli1177.runtime import Runtime


def run_json(callable_fn: Callable[[], dict[str, Any]]) -> None:
    """Run a command and print stable success/error payloads."""
    try:
        payload = callable_fn()
        ctx = click.get_current_context(silent=True)
        runtime = ctx.obj if ctx else None
        output_format = "json"
        if isinstance(runtime, Runtime):
            output_format = runtime.output_format
        print_success_formatted(payload, output_format)
    except CliError as exc:
        print_error(exc.error, exc.code, exc.details)
        raise typer.Exit(code=exc.exit_code) from exc
    except Exception as exc:  # pragma: no cover
        print_error(
            "Unexpected internal error",
            "internal_error",
            {"reason": str(exc)},
        )
        raise typer.Exit(code=exit_codes.UPSTREAM) from exc

