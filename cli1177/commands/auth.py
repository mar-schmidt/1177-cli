"""Authentication commands."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import typer

from cli1177 import exit_codes
from cli1177.cli_common import run_json
from cli1177.client.auth import AuthState, clear_auth_state, save_auth_state
from cli1177.client.bankid import (
    JOURNAL_DASHBOARD_URL,
    login_with_playwright_fallback,
)
from cli1177.client.http import HttpClient
from cli1177.client.journal import establish_journal_session
from cli1177.client.parity import probe_with_playwright
from cli1177.errors import CliError
from cli1177.runtime import Runtime

app = typer.Typer(
    help="Manage login state before running journal data commands."
)


def _emit_event(payload: dict[str, object]) -> None:
    sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


class _QrWebServer:
    """Serve the latest QR frame on a local web page."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest_png: bytes | None = None
        self._server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            self._handler_class(),
        )
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        host, port = self._server.server_address
        self.page_url = f"http://{host}:{port}/"

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/":
                    body = parent._page_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/latest.png":
                    image = parent.latest_png()
                    if image is None:
                        self.send_response(404)
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(image)))
                    self.end_headers()
                    self.wfile.write(image)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler

    @staticmethod
    def _page_html() -> str:
        return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>1177 BankID QR</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: sans-serif; margin: 1.25rem; }
    #qr { max-width: min(80vw, 420px); width: 100%; }
    .hint { color: #555; }
  </style>
</head>
<body>
  <h1>Scan with BankID</h1>
  <p class="hint">Keep this page open while signing in.</p>
  <img id="qr" alt="BankID QR code" src="/latest.png">
  <script>
    const qr = document.getElementById("qr");
    function refresh() {
      qr.src = "/latest.png?t=" + Date.now();
    }
    setInterval(refresh, 750);
    refresh();
  </script>
</body>
</html>
""".strip()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1.0)

    def update_png(self, png_bytes: bytes) -> None:
        with self._lock:
            self._latest_png = bytes(png_bytes)

    def latest_png(self) -> bytes | None:
        with self._lock:
            if self._latest_png is None:
                return None
            return bytes(self._latest_png)


def _start_qr_web_server() -> _QrWebServer:
    server = _QrWebServer()
    server.start()
    return server


def _has_shib_cookie(cookies: list[dict[str, str]]) -> bool:
    for item in cookies:
        name = str(item.get("name", "")).lower()
        if name.startswith("shibsession"):
            return True
    return False


def _runtime(ctx: typer.Context) -> Runtime:
    runtime = ctx.obj
    if not isinstance(runtime, Runtime):
        raise RuntimeError("runtime not initialized")
    return runtime


def _check_session_alive(runtime: Runtime) -> bool:
    """Check whether current session cookies are still valid."""
    if not runtime.state.cookies:
        return False
    client = HttpClient(
        cookies=runtime.state.cookies,
        max_retries=runtime.max_retries,
    )
    check = client.request(
        "GET",
        "https://e-tjanster.1177.se/mvk",
        allow_status={200, 401, 403},
    )
    return "e-tjanster.1177.se" in str(check.url)


def _check_journal_session(
    runtime: Runtime,
    *,
    allow_interactive_step_up: bool = False,
    debug_trace: list[dict[str, object]] | None = None,
    qr_frame_callback: Callable[[bytes], None] | None = None,
    render_terminal_qr: bool = True,
    clear_terminal_qr_screen: bool = True,
) -> bool:
    """Check whether journalen session is available for data requests."""
    if not runtime.state.cookies:
        return False
    client = HttpClient(
        cookies=runtime.state.cookies,
        max_retries=runtime.max_retries,
    )
    session_kwargs: dict[str, object] = {
        "allow_interactive_step_up": allow_interactive_step_up,
        "debug_trace": debug_trace,
    }
    if qr_frame_callback is not None:
        session_kwargs["qr_frame_callback"] = qr_frame_callback
        session_kwargs["render_terminal_qr"] = render_terminal_qr
        session_kwargs["clear_terminal_qr_screen"] = clear_terminal_qr_screen
    ready = establish_journal_session(client, **session_kwargs)
    if ready:
        runtime.state.cookies = client.cookies
        save_auth_state(runtime.paths, runtime.state)
    return ready


def _state_path_metadata(runtime: Runtime) -> dict[str, str]:
    return {
        "primary_state_path": str(runtime.paths.primary_state_file),
        "global_state_path": str(runtime.paths.global_state_file),
        "state_path_source": (
            "default"
            if runtime.paths.primary_state_file == runtime.paths.global_state_file
            else "override"
        ),
    }


@app.command("login")
def login(
    ctx: typer.Context,
    method: str = typer.Option(
        "bankid-qr",
        "--method",
        help="Choose login method. Currently only bankid-qr is supported.",
    ),
    allow_playwright_fallback: bool = typer.Option(
        False,
        "--allow-playwright-fallback",
        help=(
            "Allow a browser fallback step when the standard BankID flow "
            "fails."
        ),
    ),
    qr_output: str = typer.Option(
        "terminal",
        "--qr-output",
        help="Choose QR output mode: terminal, base64, both, or web.",
    ),
) -> None:
    """Log in to 1177 and store a reusable local session.

    Uses BankID QR login and verifies Journalen access before returning.
    """

    def execute() -> dict[str, object]:
        runtime = _runtime(ctx)
        valid_qr_outputs = {"terminal", "base64", "both", "web"}
        if qr_output not in valid_qr_outputs:
            raise CliError(
                error="Unsupported QR output mode",
                code="invalid_argument",
                exit_code=1,
                details={
                    "field": "qr_output",
                    "value": qr_output,
                    "allowed": sorted(valid_qr_outputs),
                },
            )

        qr_image_path: Path | None = None
        qr_frame_count = 0
        qr_frame_sinks: list[Callable[[bytes], None]] = []
        render_terminal_qr = True
        clear_terminal_qr_screen = True
        qr_web_server: _QrWebServer | None = None

        def ensure_qr_web_server() -> _QrWebServer:
            nonlocal qr_web_server
            if qr_web_server is not None:
                return qr_web_server
            qr_web_server = _start_qr_web_server()
            _emit_event(
                {
                    "event": "bankid_qr_web_url",
                    "url": qr_web_server.page_url,
                },
            )
            return qr_web_server

        if qr_output in {"base64", "both"}:
            qr_image_path = Path(tempfile.gettempdir()) / (
                f"1177-bankid-qr-{os.getpid()}.png"
            )

            def emit_base64_qr_frame(png_bytes: bytes) -> None:
                assert qr_image_path is not None
                qr_image_path.write_bytes(png_bytes)
                _emit_event(
                    {
                        "event": "bankid_qr_frame",
                        "frame_index": qr_frame_count,
                        "encoding": "base64",
                        "image_base64": base64.b64encode(
                            png_bytes,
                        ).decode("ascii"),
                        "image_path": str(qr_image_path),
                    },
                )

            qr_frame_sinks.append(emit_base64_qr_frame)
            if qr_output == "base64":
                render_terminal_qr = False
            else:
                clear_terminal_qr_screen = False

        if qr_output == "web":
            render_terminal_qr = False

            def emit_web_qr_frame(png_bytes: bytes) -> None:
                ensure_qr_web_server().update_png(png_bytes)

            qr_frame_sinks.append(emit_web_qr_frame)
            ensure_qr_web_server()

        qr_frame_callback = None
        if qr_frame_sinks:

            def qr_frame_callback(png_bytes: bytes) -> None:
                nonlocal qr_frame_count
                qr_frame_count += 1
                for sink in qr_frame_sinks:
                    sink(png_bytes)

        try:
            auth_trace: list[dict[str, object]] | None = None
            if runtime.debug_auth:
                auth_trace = []
            session_alive = (
                runtime.state.logged_in and _check_session_alive(runtime)
            )
            journal_ready = runtime.state.logged_in and _check_journal_session(
                runtime,
                allow_interactive_step_up=True,
                debug_trace=auth_trace,
                qr_frame_callback=qr_frame_callback,
                render_terminal_qr=render_terminal_qr,
                clear_terminal_qr_screen=clear_terminal_qr_screen,
            )
            if session_alive and journal_ready:
                payload = {
                    "ok": True,
                    "already_logged_in": True,
                    "auth_method": runtime.state.auth_method,
                    "idp_host": runtime.state.idp_host,
                    "journal_ready": True,
                    "qr_output": qr_output,
                }
                payload.update(_state_path_metadata(runtime))
                if auth_trace is not None:
                    payload["auth_trace"] = auth_trace
                return payload
            if method != "bankid-qr":
                raise CliError(
                    error="Unsupported auth method",
                    code="invalid_argument",
                    exit_code=1,
                    details={"method": method},
                )
            session_kwargs: dict[str, object] = {
                "allow_interactive_step_up": True,
                "debug_trace": auth_trace,
            }
            if qr_frame_callback is not None:
                session_kwargs["qr_frame_callback"] = qr_frame_callback
                session_kwargs["render_terminal_qr"] = render_terminal_qr
                session_kwargs["clear_terminal_qr_screen"] = (
                    clear_terminal_qr_screen
                )
            journal_ready = establish_journal_session(
                runtime.client,
                **session_kwargs,
            )
            if not journal_ready:
                if allow_playwright_fallback:
                    login_with_playwright_fallback()
                raise CliError(
                    error="BankID authentication failed",
                    code="auth_required",
                    exit_code=exit_codes.AUTH,
                    details={"hint": "Retry `1177 auth login`."},
                )

            state = AuthState(
                cookies=runtime.client.cookies,
                idp_host=runtime.state.idp_host,
                logged_in=True,
                auth_method=method,
                last_error=None,
            )
            save_auth_state(runtime.paths, state)
            runtime.state = state
            payload = {
                "ok": True,
                "auth_method": method,
                "idp_host": state.idp_host,
                "target_url": JOURNAL_DASHBOARD_URL,
                "poll_count": 0,
                "last_rfa": "",
                "journal_ready": True,
                "qr_output": qr_output,
                "qr_frames_emitted": qr_frame_count,
            }
            if qr_image_path is not None:
                payload["qr_image_path"] = str(qr_image_path)
            payload.update(_state_path_metadata(runtime))
            if auth_trace is not None:
                payload["auth_trace"] = auth_trace
            return payload
        finally:
            if qr_web_server is not None:
                qr_web_server.stop()

    run_json(execute)


@app.command("status")
def status(ctx: typer.Context) -> None:
    """Show whether your saved 1177 session is still usable."""

    def execute() -> dict[str, object]:
        runtime = _runtime(ctx)
        has_state = bool(runtime.state.cookies and runtime.state.logged_in)
        has_shib_cookie = _has_shib_cookie(runtime.state.cookies)
        session_check_ok = _check_session_alive(runtime) if has_state else False
        auth_trace: list[dict[str, object]] | None = None
        if runtime.debug_auth:
            auth_trace = []
        journal_ready = False
        if has_state:
            journal_ready = _check_journal_session(
                runtime,
                allow_interactive_step_up=False,
                debug_trace=auth_trace,
            )
        payload = {
            "ok": True,
            "logged_in": has_state and (session_check_ok or has_shib_cookie),
            "session_alive": session_check_ok,
            "journal_ready": journal_ready,
            "auth_method": runtime.state.auth_method,
            "idp_host": runtime.state.idp_host,
        }
        payload.update(_state_path_metadata(runtime))
        if auth_trace is not None:
            payload["auth_trace"] = auth_trace
        return payload

    run_json(execute)


@app.command("logout")
def logout(ctx: typer.Context) -> None:
    """Sign out and clear the locally saved authentication session."""

    def execute() -> dict[str, object]:
        runtime = _runtime(ctx)
        if runtime.state.cookies:
            client = HttpClient(
                cookies=runtime.state.cookies,
                max_retries=runtime.max_retries,
            )
            client.request(
                "GET",
                "https://e-tjanster.1177.se/mvk/logout",
                allow_status={200, 302, 401, 403},
            )
        clear_auth_state(runtime.paths)
        runtime.state = AuthState()
        return {"ok": True, "logged_in": False}

    run_json(execute)


@app.command("probe-browser-parity")
def probe_browser_parity(
    ctx: typer.Context,
    url: str = typer.Option(
        "https://journalen.1177.se/",
        "--url",
        help="Start URL to open when checking browser session behavior.",
    ),
    headless: bool = typer.Option(
        True,
        "--headless/--headed",
        help="Run browser checks without or with a visible browser window.",
    ),
    timeout_ms: int = typer.Option(
        30000,
        "--timeout-ms",
        help="Maximum time to wait for page navigation and redirects.",
    ),
) -> None:
    """Collect browser-side redirect and cookie diagnostics.

    Use this when API requests fail but you need browser parity debugging.
    """

    def execute() -> dict[str, object]:
        runtime = _runtime(ctx)
        if not runtime.state.cookies:
            raise CliError(
                error="Login required before browser parity probe",
                code="auth_required",
                exit_code=exit_codes.AUTH,
                details={"hint": "Run `1177 auth login` first."},
            )
        return probe_with_playwright(
            runtime.state.cookies,
            url=url,
            headless=headless,
            timeout_ms=timeout_ms,
        )

    run_json(execute)

