"""Shared command execution helpers."""

from __future__ import annotations

import itertools
import sys
import threading
from collections.abc import Callable
from typing import Any

import click
import typer

from cli1177 import exit_codes
from cli1177.errors import CliError
from cli1177.output import print_error, print_success_formatted
from cli1177.runtime import Runtime

_SPINNER_DELAY_SECONDS = 0.5
_SPINNER_INTERVAL_SECONDS = 0.1
_SPINNER_FRAMES = ("|", "/", "-", "\\")
_SPINNER_MESSAGE = "Loading..."


def _is_auth_login_command(command_path: str) -> bool:
    """Return True when current command is auth login."""
    parts = [part.strip().lower() for part in command_path.split() if part]
    return len(parts) >= 2 and parts[-2:] == ["auth", "login"]


class _DelayedSpinner:
    """Render a lightweight delayed spinner on stderr."""

    def __init__(
        self,
        *,
        stream: Any = None,
        delay_seconds: float = _SPINNER_DELAY_SECONDS,
        interval_seconds: float = _SPINNER_INTERVAL_SECONDS,
        message: str = _SPINNER_MESSAGE,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._delay_seconds = delay_seconds
        self._interval_seconds = interval_seconds
        self._message = message
        self._frames = itertools.cycle(_SPINNER_FRAMES)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._is_rendering = False

    def __enter__(self) -> _DelayedSpinner:
        self._thread = threading.Thread(
            target=self._run,
            name="cli1177-spinner",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._clear_line()

    def _can_render(self) -> bool:
        stream = self._stream
        isatty = getattr(stream, "isatty", None)
        if not callable(isatty):
            return False
        return bool(isatty())

    def _run(self) -> None:
        if self._stop_event.wait(self._delay_seconds):
            return
        if not self._can_render():
            return
        self._is_rendering = True
        while not self._stop_event.wait(self._interval_seconds):
            frame = next(self._frames)
            self._stream.write(f"\r{frame} {self._message}")
            self._stream.flush()

    def _clear_line(self) -> None:
        if not self._is_rendering:
            return
        clear_width = len(self._message) + 4
        self._stream.write("\r" + (" " * clear_width) + "\r")
        self._stream.flush()
        self._is_rendering = False


def run_json(callable_fn: Callable[[], dict[str, Any]]) -> None:
    """Run a command and print stable success/error payloads."""
    try:
        ctx = click.get_current_context(silent=True)
        command_path = ctx.command_path if ctx else ""
        use_spinner = not _is_auth_login_command(command_path)
        if use_spinner:
            with _DelayedSpinner():
                payload = callable_fn()
        else:
            payload = callable_fn()
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

