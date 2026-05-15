"""Authentication commands."""

from __future__ import annotations

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
) -> bool:
    """Check whether journalen session is available for data requests."""
    if not runtime.state.cookies:
        return False
    client = HttpClient(
        cookies=runtime.state.cookies,
        max_retries=runtime.max_retries,
    )
    ready = establish_journal_session(
        client,
        allow_interactive_step_up=allow_interactive_step_up,
        debug_trace=debug_trace,
    )
    if ready:
        runtime.state.cookies = client.cookies
        save_auth_state(runtime.paths, runtime.state)
    return ready


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
) -> None:
    """Log in to 1177 and store a reusable local session.

    Uses BankID QR login and verifies Journalen access before returning.
    """

    def execute() -> dict[str, object]:
        runtime = _runtime(ctx)
        auth_trace: list[dict[str, object]] | None = None
        if runtime.debug_auth:
            auth_trace = []
        session_alive = runtime.state.logged_in and _check_session_alive(runtime)
        journal_ready = runtime.state.logged_in and _check_journal_session(
            runtime,
            allow_interactive_step_up=True,
            debug_trace=auth_trace,
        )
        if session_alive and journal_ready:
            payload = {
                "ok": True,
                "already_logged_in": True,
                "auth_method": runtime.state.auth_method,
                "idp_host": runtime.state.idp_host,
                "journal_ready": True,
            }
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
        journal_ready = establish_journal_session(
            runtime.client,
            allow_interactive_step_up=True,
            debug_trace=auth_trace,
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
        }
        if auth_trace is not None:
            payload["auth_trace"] = auth_trace
        return payload

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

