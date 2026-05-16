"""BankID QR authentication orchestration."""

from __future__ import annotations

import json
import io
import re
import shutil
import time
from dataclasses import dataclass
from html import unescape
from typing import Callable
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup
from PIL import Image
from PIL import UnidentifiedImageError

from cli1177 import exit_codes
from cli1177.client.journal import establish_journal_session
from cli1177.errors import CliError
from cli1177.client.http import HttpClient

MVK_URL = "https://e-tjanster.1177.se/mvk"
JOURNAL_DASHBOARD_URL = "https://journalen.1177.se/Dashboard"


def _build_login_url(target_url: str) -> str:
    """Build Shibboleth login URL for a specific target service."""
    encoded_target = quote(target_url, safe="")
    return (
        "https://e-tjanster.1177.se/Shibboleth.sso/Login"
        f"?target={encoded_target}"
        "&authnContextClassRef=urn%3Alocal.methodid.ccp19"
    )


def _debug_log(
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, object],
) -> None:
    return


def _should_fallback_to_mvk(exc: CliError, target_url: str) -> bool:
    """Return True when target auth should retry through MVK."""
    if target_url == MVK_URL:
        return False
    if exc.code != "upstream_error":
        return False
    status_code = exc.details.get("status_code")
    host = str(exc.details.get("host", ""))
    return status_code in {500, 502, 503, 504} and host == "e-tjanster.1177.se"


@dataclass(slots=True)
class BankIdLoginResult:
    """Result from login orchestration."""

    idp_host: str
    poll_count: int
    last_rfa: str
    target_url: str


def _extract_idp_host(response_url: str) -> str | None:
    parsed = urlparse(response_url)
    host = parsed.netloc
    if host.endswith(".idp.funktionstjanster.se"):
        return host
    return None


def _find_idp_host(initial: str, history_urls: list[str]) -> str:
    host = _extract_idp_host(initial)
    if host:
        return host
    for url in history_urls:
        host = _extract_idp_host(url)
        if host:
            return host
    raise CliError(
        error="Could not determine IdP host",
        code="idp_host_missing",
        exit_code=exit_codes.AUTH,
        details={},
    )


def _strip_html(value: str) -> str:
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return unescape(text)


def _is_terminal_failure(rfa_code: str, status_text: str) -> bool:
    """Return True only for clear terminal auth failures."""
    text = status_text.lower()
    if rfa_code == "code-grp-rfa8":
        return True
    if rfa_code == "code-grp-rfa9":
        cancel_words = (
            "avbr",
            "avbrut",
            "cancel",
            "avvis",
            "nekad",
            "misslyck",
            "fel",
            "timeout",
            "tidsgr",
        )
        progress_words = (
            "säkerhetskod",
            "identifiera",
            "skriv under",
            "starta bankid",
            "öppna bankid",
        )
        if any(word in text for word in progress_words):
            return False
        if any(word in text for word in cancel_words):
            return True
        return False
    return False


def render_png_to_terminal(
    png_bytes: bytes,
    *,
    clear_screen: bool = True,
) -> None:
    """Render PNG QR using unicode blocks in terminal."""
    try:
        image = Image.open(io.BytesIO(png_bytes)).convert("L")
    except UnidentifiedImageError as exc:
        raise CliError(
            error="BankID QR image was not returned by upstream",
            code="bankid_qr_unavailable",
            exit_code=exit_codes.UPSTREAM,
            details={},
        ) from exc
    term_size = shutil.get_terminal_size((80, 24))
    max_by_width = max(16, term_size.columns - 4)
    # Two image rows are rendered into one terminal row via block chars.
    max_by_height = max(16, (term_size.lines - 6) * 2)
    max_target = min(image.width, max_by_width, max_by_height)
    preferred_width = max(16, int(max_by_width * 0.65))
    target_width = min(max_target, preferred_width)
    if target_width < image.width:
        image = image.resize(
            (target_width, target_width),
            Image.Resampling.NEAREST,
        )
    threshold = 200
    lines: list[str] = []
    for y in range(0, image.height, 2):
        row: list[str] = []
        for x in range(image.width):
            top = image.getpixel((x, y)) < threshold
            bottom = False
            if y + 1 < image.height:
                bottom = image.getpixel((x, y + 1)) < threshold
            if top and bottom:
                row.append("█")
            elif top and not bottom:
                row.append("▀")
            elif not top and bottom:
                row.append("▄")
            else:
                row.append(" ")
        lines.append("".join(row))
    if clear_screen:
        print("\033[2J\033[H", end="")
    print("\n".join(lines))
    print("\nOpen BankID app and scan the QR code.")


def _extract_saml_form(html: str, base_url: str) -> tuple[str, dict[str, str]] | None:
    """Extract SAML POST form action and fields from IdP response HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.select("form"):
        action = form.get("action")
        if not action:
            continue
        fields: dict[str, str] = {}
        for input_node in form.select("input"):
            name = input_node.get("name")
            if not name:
                continue
            fields[str(name)] = str(input_node.get("value", ""))
        for textarea in form.select("textarea"):
            name = textarea.get("name")
            if not name:
                continue
            fields[str(name)] = textarea.get_text("", strip=False)
        normalized = {key.lower(): key for key in fields}
        if "samlresponse" in normalized:
            return (urljoin(base_url, str(action)), fields)
    return None


def _extract_next_url(html: str, base_url: str) -> str | None:
    """Extract likely next auth URL from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for meta in soup.select("meta[http-equiv]"):
        if str(meta.get("http-equiv", "")).lower() != "refresh":
            continue
        content = str(meta.get("content", ""))
        # Avoid brittle string slicing, handle variants like URL = '/path'.
        match = re.search(
            r"""url\s*=\s*['"]?([^'";]+)""",
            content,
            re.IGNORECASE,
        )
        if match:
            return urljoin(base_url, match.group(1).strip())
    for link in soup.select("a[href]"):
        href = str(link.get("href", ""))
        if any(
            part in href
            for part in (
                "/samlv2/idp/sign_in/",
            )
        ):
            return urljoin(base_url, href)
    return None


def _req_to_sign_in_url(url: str) -> str | None:
    """Convert req URL to sign_in URL when needed."""
    parsed = urlparse(url)
    if "/samlv2/idp/req/" not in parsed.path:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    session_id = parts[-1]
    if not session_id:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/samlv2/idp/sign_in/{session_id}"


def _follow_until_mvk(client: HttpClient, start_url: str) -> str:
    """Follow IdP/SP chain until e-tjanster host is reached."""
    current_url = start_url
    current_html = ""
    for _ in range(12):
        response = client.request(
            "GET",
            current_url,
            allow_status={200, 302, 401, 403},
        )
        current_url = str(response.url)
        current_html = response.text
        host = urlparse(current_url).netloc
        # region agent log
        _debug_log(
            run_id="auth-login",
            hypothesis_id="H2",
            location="bankid.py:_follow_until_mvk",
            message="chain_step",
            data={
                "url_host": host,
                "url_path": urlparse(current_url).path,
                "status_code": response.status_code,
                "has_saml_response": "SAMLResponse" in current_html,
            },
        )
        # endregion
        if "e-tjanster.1177.se" in host:
            return current_url

        saml_form = _extract_saml_form(current_html, current_url)
        if saml_form:
            action, fields = saml_form
            # region agent log
            _debug_log(
                run_id="auth-login",
                hypothesis_id="H2",
                location="bankid.py:_follow_until_mvk",
                message="submit_saml_form_detected",
                data={
                    "action_host": urlparse(action).netloc,
                    "action_path": urlparse(action).path,
                    "field_count": len(fields),
                    "has_relay_state": any(
                        key.lower() == "relaystate" for key in fields
                    ),
                },
            )
            # endregion
            post = client.request(
                "POST",
                action,
                data=fields,
                allow_status={200, 302, 401, 403},
            )
            current_url = str(post.url)
            if "e-tjanster.1177.se" in urlparse(current_url).netloc:
                return current_url
            current_html = post.text
            continue

        sign_in_url = _req_to_sign_in_url(current_url)
        if sign_in_url:
            current_url = sign_in_url
            continue

        next_url = _extract_next_url(current_html, current_url)
        if not next_url:
            soup = BeautifulSoup(current_html, "html.parser")
            has_meta_refresh = bool(soup.select("meta[http-equiv]"))
            has_signin_link = bool(
                soup.select("a[href*='/samlv2/idp/sign_in/']")
            )
            has_req_link = bool(soup.select("a[href*='/samlv2/idp/req/']"))
            form_count = len(soup.select("form"))
            # region agent log
            _debug_log(
                run_id="auth-login",
                hypothesis_id="H2",
                location="bankid.py:_follow_until_mvk",
                message="chain_stuck_no_next_url",
                data={
                    "url_host": host,
                    "url_path": urlparse(current_url).path,
                    "html_len": len(current_html),
                    "form_count": form_count,
                    "has_meta_refresh": has_meta_refresh,
                    "has_signin_link": has_signin_link,
                    "has_req_link": has_req_link,
                    "has_saml_response": "SAMLResponse" in current_html,
                },
            )
            # endregion
            return current_url
        current_url = next_url
    return current_url


def _is_retryable_location(location: str) -> bool:
    """Return True if poll location looks like pre-final IdP step."""
    return (
        "/samlv2/idp/sign_in/" in location
        or "/samlv2/idp/req/" in location
    )


def _parse_poll_payload(
    response_text: str,
    response_url: str,
) -> dict[str, object] | None:
    """Parse poll response when JSON is present."""
    try:
        payload = json.loads(response_text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    payload["__response_url"] = response_url
    return payload


def _status_text(info_html: str, rfa_code: str) -> str:
    """Build user-facing status text with sensible fallback."""
    text = _strip_html(info_html).strip()
    if text:
        return text
    if rfa_code in {"code-grp-rfa17-qr", "code-grp-rfa1-qr"}:
        return "Väntar på bekräftelse i BankID..."
    return "Väntar på slutlig inloggningsbekräftelse..."


def _complete_saml_handshake(
    client: HttpClient,
    location: str,
    target_url: str,
) -> bool:
    """Follow IdP callback and submit SAML form to Shibboleth."""
    _follow_until_mvk(client, location)
    target_response = client.request(
        "GET",
        target_url,
        allow_status={200, 302, 401, 403},
    )
    target_host = urlparse(str(target_response.url)).netloc
    # region agent log
    _debug_log(
        run_id="auth-login",
        hypothesis_id="H3",
        location="bankid.py:_complete_saml_handshake",
        message="target_probe",
        data={
            "target_host": target_host,
            "target_path": urlparse(str(target_response.url)).path,
            "status_code": target_response.status_code,
        },
    )
    # endregion
    if "e-tjanster.1177.se" not in target_host:
        return False
    establish_journal_session(client)
    return True


def login_with_bankid_qr(
    client: HttpClient,
    *,
    target_url: str = JOURNAL_DASHBOARD_URL,
    timeout_s: int = 180,
    poll_limit: int = 240,
    qr_frame_callback: Callable[[bytes], None] | None = None,
    render_terminal_qr: bool = True,
    clear_terminal_qr_screen: bool = True,
) -> BankIdLoginResult:
    """Authenticate through BankID QR flow."""
    effective_target = target_url
    try:
        login_response = client.request("GET", _build_login_url(effective_target))
    except CliError as exc:
        if not _should_fallback_to_mvk(exc, effective_target):
            raise
        effective_target = MVK_URL
        login_response = client.request("GET", _build_login_url(effective_target))
    history = [str(item.url) for item in login_response.history]
    idp_host = _find_idp_host(str(login_response.url), history)
    # region agent log
    _debug_log(
        run_id="auth-login",
        hypothesis_id="H1",
        location="bankid.py:login_with_bankid_qr",
        message="login_initialized",
        data={
            "effective_target": effective_target,
            "idp_host": idp_host,
            "history_len": len(history),
        },
    )
    # endregion

    start = time.time()
    counter = 0
    poll_count = 0
    last_rfa = "unknown"
    finalizing_url = ""
    finalizing_count = 0

    def track_finalizing(url: str) -> None:
        nonlocal finalizing_url, finalizing_count
        if url == finalizing_url:
            finalizing_count += 1
        else:
            finalizing_url = url
            finalizing_count = 1
        if finalizing_count >= 45:
            raise CliError(
                error="BankID callback appears stalled",
                code="auth_stalled",
                exit_code=exit_codes.AUTH,
                details={
                    "response_url": url,
                    "finalizing_attempts": finalizing_count,
                },
            )

    def reset_finalizing() -> None:
        nonlocal finalizing_url, finalizing_count
        finalizing_url = ""
        finalizing_count = 0

    while True:
        now_ms = int(time.time() * 1000)
        poll_url = f"https://{idp_host}/mg-local/auth/ccp19/grp/pollstatus"
        poll_response = client.request(
            "GET",
            poll_url,
            params={"AjaxRequestUniqueId": f"{now_ms}{counter}"},
        )
        poll = _parse_poll_payload(poll_response.text, str(poll_response.url))
        counter += 1
        poll_count += 1
        # region agent log
        _debug_log(
            run_id="auth-login",
            hypothesis_id="H1",
            location="bankid.py:login_with_bankid_qr",
            message="poll_result",
            data={
                "poll_count": poll_count,
                "response_host": urlparse(str(poll_response.url)).netloc,
                "response_path": urlparse(str(poll_response.url)).path,
                "status_code": poll_response.status_code,
                "is_json": poll is not None,
            },
        )
        # endregion
        if poll_count >= poll_limit:
            raise CliError(
                error="BankID authentication timed out",
                code="bankid_timeout",
                exit_code=exit_codes.AUTH,
                details={"poll_count": poll_count},
            )
        if time.time() - start > timeout_s:
            raise CliError(
                error="BankID authentication timed out",
                code="bankid_timeout",
                exit_code=exit_codes.AUTH,
                details={"seconds": timeout_s},
            )

        if poll is None:
            fallback_url = str(poll_response.url)
            print("Status: Väntar på slutlig inloggningsbekräftelse...")
            track_finalizing(fallback_url)
            # region agent log
            _debug_log(
                run_id="auth-login",
                hypothesis_id="H4",
                location="bankid.py:login_with_bankid_qr",
                message="poll_non_json_branch",
                data={
                    "fallback_host": urlparse(fallback_url).netloc,
                    "fallback_path": urlparse(fallback_url).path,
                    "content_type": poll_response.headers.get(
                        "Content-Type",
                        "",
                    ),
                    "history_len": len(poll_response.history),
                    "has_saml_response": "SAMLResponse" in poll_response.text,
                    "has_meta_refresh": "http-equiv=\"refresh\""
                    in poll_response.text.lower(),
                },
            )
            # endregion
            if _is_retryable_location(fallback_url):
                try:
                    completed = _complete_saml_handshake(
                        client,
                        fallback_url,
                        effective_target,
                    )
                    # region agent log
                    _debug_log(
                        run_id="auth-login",
                        hypothesis_id="H4",
                        location="bankid.py:login_with_bankid_qr",
                        message="non_json_handshake_attempt",
                        data={
                            "fallback_host": urlparse(fallback_url).netloc,
                            "fallback_path": urlparse(fallback_url).path,
                            "completed": completed,
                        },
                    )
                    # endregion
                    if not completed:
                        time.sleep(1.0)
                        continue
                    return BankIdLoginResult(
                        idp_host=idp_host,
                        poll_count=poll_count,
                        last_rfa=last_rfa,
                        target_url=effective_target,
                    )
                except CliError:
                    time.sleep(1.0)
                    continue
            time.sleep(1.0)
            continue

        info_html = str(poll.get("infotext", ""))
        last_rfa = str(poll.get("rfacode", ""))
        info_text = _status_text(info_html, last_rfa)
        print(f"Status: {info_text}")

        if _is_terminal_failure(last_rfa, info_text):
            raise CliError(
                error="BankID authentication failed",
                code="bankid_rejected",
                exit_code=exit_codes.AUTH,
                details={"rfacode": last_rfa, "status": info_text},
            )

        location = str(poll.get("location", "")).strip()
        if location:
            track_finalizing(location)
            try:
                completed = _complete_saml_handshake(
                    client,
                    location,
                    effective_target,
                )
                # region agent log
                _debug_log(
                    run_id="auth-login",
                    hypothesis_id="H3",
                    location="bankid.py:login_with_bankid_qr",
                    message="location_handshake_attempt",
                    data={
                        "location_host": urlparse(location).netloc,
                        "location_path": urlparse(location).path,
                        "completed": completed,
                    },
                )
                # endregion
                if not completed:
                    print("Status: Väntar på slutlig inloggningsbekräftelse...")
                    time.sleep(1.0)
                    continue
                return BankIdLoginResult(
                    idp_host=idp_host,
                    poll_count=poll_count,
                    last_rfa=last_rfa,
                    target_url=effective_target,
                )
            except CliError as exc:
                response_url = str(exc.details.get("response_url", ""))
                if (
                    _is_retryable_location(location)
                    or _is_retryable_location(response_url)
                ):
                    time.sleep(1.0)
                    continue
                raise
        else:
            reset_finalizing()

        qr_url = f"https://{idp_host}/mg-local/auth/ccp19/grp/qr"
        try:
            qr_response = client.request(
                "GET",
                qr_url,
                params={"id": str(now_ms)},
            )
            if qr_frame_callback is not None:
                qr_frame_callback(qr_response.content)
            if render_terminal_qr:
                render_png_to_terminal(
                    qr_response.content,
                    clear_screen=clear_terminal_qr_screen,
                )
        except CliError as exc:
            # After the user confirms in BankID, QR can disappear before the
            # final callback is emitted. Keep polling instead of failing.
            if exc.code != "bankid_qr_unavailable":
                raise
        poll_interval_ms = int(str(poll.get("pollinterval", "1000")))
        time.sleep(max(0.4, poll_interval_ms / 1000))

