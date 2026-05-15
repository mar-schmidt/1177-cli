"""Browser parity probe for Journalen redirect/cookie telemetry."""

from __future__ import annotations

import sys
from urllib.parse import urlparse

from cli1177 import exit_codes
from cli1177.errors import CliError

def _debug_log(
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, object],
) -> None:
    return
def _cookie_names_from_header(value: str) -> list[str]:
    names: list[str] = []
    for chunk in value.split(";"):
        part = chunk.strip()
        if "=" not in part:
            continue
        name = part.split("=", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return names


def _to_playwright_cookie(item: dict[str, str]) -> dict[str, object] | None:
    name = str(item.get("name", "")).strip()
    value = str(item.get("value", ""))
    domain = str(item.get("domain", "")).strip()
    path = str(item.get("path", "/")).strip() or "/"
    if not name or not domain:
        return None
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "secure": True,
        "httpOnly": False,
    }


def _request_cookie_names(request: object) -> list[str]:
    headers = getattr(request, "headers", None)
    if headers is None:
        return []
    cookie_header = str(headers.get("cookie", ""))
    return _cookie_names_from_header(cookie_header)


def probe_with_playwright(
    cookies: list[dict[str, str]],
    *,
    url: str,
    headless: bool,
    timeout_ms: int,
) -> dict[str, object]:
    executable = sys.executable
    try:
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import Error as PlaywrightError
    except ImportError as exc:
        hint = 'Install with `pip install -e ".[playwright]"`.'
        if "/pipx/venvs/" in executable:
            hint = "Install into pipx venv: `pipx inject 1177-cli playwright`."
        raise CliError(
            error="Playwright is not installed",
            code="dependency_missing",
            exit_code=exit_codes.USAGE,
            details={
                "python_executable": executable,
                "hint": hint,
            },
        ) from exc

    playwright_cookies: list[dict[str, object]] = []
    for item in cookies:
        parsed = _to_playwright_cookie(item)
        if parsed is not None:
            playwright_cookies.append(parsed)

    events: list[dict[str, object]] = []
    run_id = "browser-parity"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(ignore_https_errors=True)
            if playwright_cookies:
                context.add_cookies(playwright_cookies)
            page = context.new_page()

            def on_response(response: object) -> None:
                response_url = str(getattr(response, "url", ""))
                parsed = urlparse(response_url)
                host = parsed.netloc
                if not (
                    "journalen.1177.se" in host
                    or "funktionstjanster.se" in host
                    or "e-tjanster.1177.se" in host
                ):
                    return
                request = getattr(response, "request", None)
                entry = {
                    "host": host,
                    "path": parsed.path,
                    "status": int(getattr(response, "status", 0)),
                    "method": str(getattr(request, "method", "")),
                    "cookie_names": _request_cookie_names(request),
                }
                events.append(entry)

            page.on("response", on_response)
            response = page.goto(url, wait_until="load", timeout=timeout_ms)
            final_url = page.url
            context.close()
            browser.close()
    except PlaywrightError as exc:
        reason = str(exc)
        if "Executable doesn't exist" in reason:
            raise CliError(
                error="Playwright browser binaries are missing",
                code="dependency_missing",
                exit_code=exit_codes.USAGE,
                details={
                    "python_executable": executable,
                    "hint": (
                        "Run browser install with this interpreter: "
                        f"`{executable} -m playwright install chromium`."
                    ),
                },
            ) from exc
        raise

    # region agent log
    _debug_log(
        run_id=run_id,
        hypothesis_id="P1",
        location="parity.py:probe_with_playwright",
        message="browser_probe_result",
        data={
            "entry_url_host": urlparse(url).netloc,
            "entry_url_path": urlparse(url).path,
            "final_url_host": urlparse(final_url).netloc,
            "final_url_path": urlparse(final_url).path,
            "status": int(getattr(response, "status", 0)) if response else 0,
            "event_count": len(events),
        },
    )
    # endregion
    return {
        "ok": True,
        "probe": "browser_parity",
        "entry_url": url,
        "final_url": final_url,
        "initial_status": int(getattr(response, "status", 0))
        if response
        else None,
        "events": events[:40],
        "cookie_count_loaded": len(playwright_cookies),
    }
