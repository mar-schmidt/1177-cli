"""Journalen API wrappers."""

from __future__ import annotations

import io
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urljoin
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from PIL import Image
from PIL import UnidentifiedImageError

from cli1177 import exit_codes
from cli1177.client.http import HttpClient
from cli1177.errors import CliError

BASE_URL = "https://journalen.1177.se"

AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded",
}

AJAX_JSON_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
}


def _debug_log(
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, object],
) -> None:
    return


@dataclass(slots=True)
class JournalBootstrap:
    """Bootstrap context for AJAX requests."""

    verification_token: str | None
    page_url: str


def _extract_saml_form(html: str, base_url: str) -> tuple[str, dict[str, str]] | None:
    """Extract SAML form action and fields from an HTML page."""
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
        for textarea_node in form.select("textarea"):
            name = textarea_node.get("name")
            if not name:
                continue
            fields[str(name)] = str(textarea_node.get_text())
        if "SAMLResponse" in fields:
            return (urljoin(base_url, str(action)), fields)
    return None


def _extract_next_idp_url(html: str, base_url: str) -> str | None:
    """Extract a likely next IdP step URL from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for meta in soup.select("meta[http-equiv]"):
        if str(meta.get("http-equiv", "")).lower() != "refresh":
            continue
        content = str(meta.get("content", ""))
        match = re.search(r"url=(.+)$", content, re.IGNORECASE)
        if match:
            next_url = urljoin(base_url, match.group(1).strip())
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J10",
                location="journal.py:_extract_next_idp_url",
                message="next_url_from_meta_refresh",
                data={
                    "base_host": urlparse(base_url).netloc,
                    "next_host": urlparse(next_url).netloc,
                    "next_path": urlparse(next_url).path,
                },
            )
            # endregion
            return next_url

    script_match = re.search(
        r"""(?:location\.href|window\.location)\s*=\s*['"]([^'"]+)['"]""",
        html,
        re.IGNORECASE,
    )
    if script_match:
        next_url = urljoin(base_url, script_match.group(1))
        # region agent log
        _debug_log(
            run_id="journal-session",
            hypothesis_id="J10",
            location="journal.py:_extract_next_idp_url",
            message="next_url_from_script_redirect",
            data={
                "base_host": urlparse(base_url).netloc,
                "next_host": urlparse(next_url).netloc,
                "next_path": urlparse(next_url).path,
            },
        )
        # endregion
        return next_url

    candidates = (
        "/samlv2/idp/sign_in/",
        "/mg-local/login?type=ccp19",
        "/mg-local/auth/ccp19/grp",
    )
    anchor_candidates: list[str] = []
    matching_hrefs: list[str] = []
    for link in soup.select("a[href]"):
        href = str(link.get("href", ""))
        if any(part in href for part in candidates):
            anchor_candidates.append(href[:180])
            matching_hrefs.append(href)
    base_parsed = urlparse(base_url)
    if "/samlv2/idp/sign_in/" in base_parsed.path:
        # region agent log
        _debug_log(
            run_id="journal-session",
            hypothesis_id="J21",
            location="journal.py:_extract_next_idp_url",
            message="sign_in_anchor_candidates",
            data={
                "base_host": base_parsed.netloc,
                "base_path": base_parsed.path,
                "candidate_count": len(anchor_candidates),
                "anchor_candidates": anchor_candidates[:10],
            },
        )
        # endregion
    if matching_hrefs:
        selected_href = matching_hrefs[0]
        if "/samlv2/idp/sign_in/" in base_parsed.path:
            for href in matching_hrefs:
                if "type=ccp19" in href.lower():
                    selected_href = href
                    break
        next_url = urljoin(base_url, selected_href)
        # region agent log
        _debug_log(
            run_id="journal-session",
            hypothesis_id="J23",
            location="journal.py:_extract_next_idp_url",
            message="next_url_anchor_selection",
            data={
                "base_host": base_parsed.netloc,
                "base_path": base_parsed.path,
                "selected_href": selected_href[:180],
                "selected_host": urlparse(next_url).netloc,
                "selected_path": urlparse(next_url).path,
                "selected_query": urlparse(next_url).query,
                "matching_count": len(matching_hrefs),
                "bankid_only": True,
            },
        )
        # endregion
        return next_url
    return None


def _extract_generic_form(
    html: str,
    base_url: str,
) -> tuple[str, str, dict[str, str]] | None:
    """Extract a generic HTML form as a best-effort next step."""
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.select("form"):
        action = str(form.get("action", "")).strip()
        if not action:
            continue
        method = str(form.get("method", "post")).lower()
        fields: dict[str, str] = {}
        for input_node in form.select("input"):
            name = input_node.get("name")
            if not name:
                continue
            fields[str(name)] = str(input_node.get("value", ""))
        for textarea_node in form.select("textarea"):
            name = textarea_node.get("name")
            if not name:
                continue
            fields[str(name)] = textarea_node.get_text("", strip=False)
        for select_node in form.select("select"):
            name = select_node.get("name")
            if not name:
                continue
            selected = select_node.select_one("option[selected]")
            if selected is None:
                selected = select_node.select_one("option")
            value = ""
            if selected is not None:
                value = str(selected.get("value", selected.get_text("", strip=True)))
            fields[str(name)] = value
        for button_node in form.select("button[name]"):
            name = button_node.get("name")
            if not name or str(name) in fields:
                continue
            fields[str(name)] = str(button_node.get("value", ""))
        if fields:
            return (urljoin(base_url, action), method, fields)
    return None


def _has_journal_launch_link(html: str) -> bool:
    """Return True if page contains an explicit journalen launch link."""
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.select("a[href]"):
        href = str(link.get("href", ""))
        if "journalen.1177.se" in href.lower():
            return True
    return False


def _extract_journal_launch_url(html: str, base_url: str) -> str | None:
    """Extract first concrete journal launch URL from page links."""
    base_parsed = urlparse(base_url)
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    selected: str | None = None
    for link in soup.select("a[href]"):
        href = str(link.get("href", "")).strip()
        if not href:
            continue
        full = urljoin(base_url, href)
        if "journalen.1177.se" not in full.lower():
            continue
        candidates.append(full)
        parsed = urlparse(full)
        path = parsed.path.lower()
        if path.startswith("/loggedout/"):
            continue
        if (
            parsed.netloc == base_parsed.netloc
            and parsed.path == base_parsed.path
            and parsed.query == base_parsed.query
        ):
            continue
        if "journalen.1177.se" in full.lower():
            if selected is None:
                selected = full
    # region agent log
    _debug_log(
        run_id="journal-session",
        hypothesis_id="J17",
        location="journal.py:_extract_journal_launch_url",
        message="journal_launch_candidates",
        data={
            "base_host": base_parsed.netloc,
            "base_path": base_parsed.path,
            "candidate_count": len(candidates),
            "candidates": candidates[:8],
            "selected": selected or "",
        },
    )
    # endregion
    return selected


def _extract_journal_link_markup(html: str, base_url: str) -> dict[str, str]:
    """Return key attributes for first journal link candidate."""
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.select("a[href]"):
        href = str(link.get("href", "")).strip()
        if not href:
            continue
        full = urljoin(base_url, href)
        if "journalen.1177.se" not in full.lower():
            continue
        outer = str(link)[:260]
        return {
            "href": href[:160],
            "full": full[:180],
            "target": str(link.get("target", ""))[:40],
            "rel": str(link.get("rel", ""))[:60],
            "role": str(link.get("role", ""))[:40],
            "id": str(link.get("id", ""))[:80],
            "class": str(link.get("class", ""))[:120],
            "outer": outer,
        }
    return {}


def _strip_html(value: str) -> str:
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return unescape(text)


def _is_step_up_terminal_failure(rfa_code: str, status_text: str) -> bool:
    """Return True only for clear terminal failures."""
    text = status_text.lower()
    if rfa_code == "code-grp-rfa8":
        return True
    if rfa_code == "code-grp-rfa9":
        progress_words = (
            "säkerhetskod",
            "identifiera",
            "skriv under",
            "starta bankid",
            "öppna bankid",
        )
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
        if any(word in text for word in progress_words):
            return False
        if any(word in text for word in cancel_words):
            return True
        return False
    return False


def _is_authenticated_journal_url(url: str) -> bool:
    """True when URL points to an authenticated Journal page."""
    parsed = urlparse(url)
    if "journalen.1177.se" not in parsed.netloc:
        return False
    path = parsed.path.lower()
    if path.startswith("/loggedout/"):
        return False
    return True


def _cookie_names_for_host(client: HttpClient, host_substring: str) -> list[str]:
    names: set[str] = set()
    needle = host_substring.lower()
    for item in client.cookies:
        domain = str(item.get("domain", "")).lower()
        if needle in domain:
            names.add(str(item.get("name", "")))
    return sorted(name for name in names if name)


def _cookie_profile_for_host(
    client: HttpClient,
    host_substring: str,
) -> dict[str, object]:
    """Return a non-sensitive cookie profile for a host."""
    needle = host_substring.lower()
    by_name: dict[str, set[str]] = {}
    total = 0
    for item in client.cookies:
        domain = str(item.get("domain", "")).lower()
        if needle not in domain:
            continue
        name = str(item.get("name", ""))
        path = str(item.get("path", ""))
        if not name:
            continue
        total += 1
        by_name.setdefault(name, set()).add(path)
    duplicates = sorted(
        name for name, paths in by_name.items() if len(paths) > 1
    )
    saml2_names = sorted(name for name in by_name if name.startswith("Saml2."))
    return {
        "cookie_total": total,
        "cookie_name_total": len(by_name),
        "duplicate_path_names": duplicates[:10],
        "saml2_count": len(saml2_names),
        "saml2_names": saml2_names[:10],
    }


def _cookie_scope_rows_for_host(
    client: HttpClient,
    host_substring: str,
) -> list[dict[str, str]]:
    """Return non-sensitive cookie scope rows for debugging."""
    rows: list[dict[str, str]] = []
    needle = host_substring.lower()
    for item in client.cookies:
        domain = str(item.get("domain", ""))
        if needle not in domain.lower():
            continue
        name = str(item.get("name", ""))
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "domain": domain,
                "path": str(item.get("path", "")),
            },
        )
    rows.sort(key=lambda row: (row["name"], row["domain"], row["path"]))
    return rows


def _cookie_names_from_header(value: str) -> list[str]:
    """Extract cookie names from a Cookie header value."""
    names: list[str] = []
    for chunk in value.split(";"):
        part = chunk.strip()
        if "=" not in part:
            continue
        name = part.split("=", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return names


def _set_cookie_names(response: object) -> list[str]:
    """Extract Set-Cookie names from response headers."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return []
    cookie_values: list[str] = []
    multi = getattr(headers, "get_list", None)
    if callable(multi):
        cookie_values = [str(item) for item in multi("set-cookie")]
    if not cookie_values:
        single = str(headers.get("Set-Cookie", "")).strip()
        if single:
            cookie_values = [single]
    names: list[str] = []
    for item in cookie_values:
        first = item.split(";", 1)[0]
        if "=" not in first:
            continue
        name = first.split("=", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return names


def _probe_grp_pollstatus(
    client: HttpClient,
    current_url: str,
) -> dict[str, Any] | None:
    """Probe pollstatus once for runtime evidence on grp/this pages."""
    parsed = urlparse(current_url)
    if "/mg-local/auth/ccp19/grp/this" not in parsed.path:
        return None
    poll_url = (
        f"{parsed.scheme}://{parsed.netloc}"
        "/mg-local/auth/ccp19/grp/pollstatus"
    )
    probe_id = str(int(time.time() * 1000))
    response = client.request(
        "GET",
        poll_url,
        params={"AjaxRequestUniqueId": probe_id},
        allow_status={200, 302, 401, 403},
    )
    payload: dict[str, Any] | None = None
    try:
        parsed_json = json.loads(response.text)
        if isinstance(parsed_json, dict):
            payload = parsed_json
    except ValueError:
        payload = None
    # region agent log
    _debug_log(
        run_id="journal-session",
        hypothesis_id="J5",
        location="journal.py:_probe_grp_pollstatus",
        message="grp_poll_probe",
        data={
            "url_host": urlparse(str(response.url)).netloc,
            "url_path": urlparse(str(response.url)).path,
            "status_code": response.status_code,
            "is_json": payload is not None,
            "rfacode": payload.get("rfacode", "") if payload else "",
            "has_location": bool(payload.get("location", "")) if payload else False,
        },
    )
    # endregion
    return payload


def _render_grp_qr(png_bytes: bytes, prompt_text: str) -> None:
    """Render a compact QR for journal step-up auth."""
    try:
        image = Image.open(io.BytesIO(png_bytes)).convert("L")
    except UnidentifiedImageError:
        return
    threshold = 200
    lines: list[str] = []
    width = min(image.width, 64)
    if width < image.width:
        image = image.resize((width, width), Image.Resampling.NEAREST)
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
    print("\033[2J\033[H", end="")
    print("\n".join(lines))
    print(f"\n{prompt_text}")


def _run_grp_step_up(
    client: HttpClient,
    grp_this_url: str,
    prompt_text: str,
) -> tuple[bool, str, str]:
    """Run a Journalen step-up BankID flow from grp/this."""
    parsed = urlparse(grp_this_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    poll_url = f"{base}/mg-local/auth/ccp19/grp/pollstatus"
    qr_url = f"{base}/mg-local/auth/ccp19/grp/qr"
    start = time.time()
    counter = 0
    while time.time() - start < 120:
        now_ms = str(int(time.time() * 1000))
        poll_resp = client.request(
            "GET",
            poll_url,
            params={"AjaxRequestUniqueId": f"{now_ms}{counter}"},
            allow_status={200, 302, 401, 403},
        )
        counter += 1
        payload: dict[str, Any] | None = None
        try:
            parsed_json = json.loads(poll_resp.text)
            if isinstance(parsed_json, dict):
                payload = parsed_json
        except ValueError:
            payload = None

        # region agent log
        _debug_log(
            run_id="journal-session",
            hypothesis_id="J6",
            location="journal.py:_run_grp_step_up",
            message="step_up_poll",
            data={
                "status_code": poll_resp.status_code,
                "response_host": urlparse(str(poll_resp.url)).netloc,
                "is_json": payload is not None,
                "rfacode": payload.get("rfacode", "") if payload else "",
                "has_location": bool(payload.get("location", "")) if payload else False,
            },
        )
        # endregion

        if payload:
            location = str(payload.get("location", "")).strip()
            if location:
                done = client.request(
                    "GET",
                    location,
                    allow_status={200, 302, 401, 403},
                )
                done_path = urlparse(str(done.url)).path
                if "/mg-local/auth/ccp19/grp/error" in done_path:
                    # region agent log
                    _debug_log(
                        run_id="journal-session",
                        hypothesis_id="J14",
                        location="journal.py:_run_grp_step_up",
                        message="step_up_location_error_page",
                        data={
                            "done_host": urlparse(str(done.url)).netloc,
                            "done_path": done_path,
                            "status_code": done.status_code,
                        },
                    )
                    # endregion
                    return False, str(done.url), done.text
                return True, str(done.url), done.text
            rfa = str(payload.get("rfacode", ""))
            info_text = _strip_html(str(payload.get("infotext", "")))
            if _is_step_up_terminal_failure(rfa, info_text):
                # region agent log
                _debug_log(
                    run_id="journal-session",
                    hypothesis_id="J9",
                    location="journal.py:_run_grp_step_up",
                    message="step_up_terminal_rfa",
                    data={
                        "rfacode": rfa,
                        "infotext": info_text[:120],
                        "has_location": bool(location),
                    },
                )
                # endregion
                return False, str(poll_resp.url), poll_resp.text
        qr_resp = client.request(
            "GET",
            qr_url,
            params={"id": now_ms},
            allow_status={200, 302, 401, 403},
        )
        # region agent log
        _debug_log(
            run_id="journal-session",
            hypothesis_id="J9",
            location="journal.py:_run_grp_step_up",
            message="step_up_qr_fetch",
            data={
                "status_code": qr_resp.status_code,
                "response_host": urlparse(str(qr_resp.url)).netloc,
                "content_type": qr_resp.headers.get("Content-Type", ""),
                "content_len": len(qr_resp.content),
            },
        )
        # endregion
        _render_grp_qr(qr_resp.content, prompt_text)
        time.sleep(1.0)
    return False, grp_this_url, ""


def _req_to_sign_in_url(current_url: str) -> str | None:
    """Convert /samlv2/idp/req/... URL to /samlv2/idp/sign_in/... URL."""
    parsed = urlparse(current_url)
    if "/samlv2/idp/req/" not in parsed.path:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    session_id = parts[-1]
    if not session_id:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/samlv2/idp/sign_in/{session_id}"


def _append_trace(
    debug_trace: list[dict[str, object]] | None,
    *,
    action: str,
    url: str,
    status_code: int | None = None,
) -> None:
    """Append one auth trace event when debug is enabled."""
    if debug_trace is None:
        return
    item: dict[str, object] = {
        "action": action,
        "url": url,
        "host": urlparse(url).netloc,
    }
    if status_code is not None:
        item["status_code"] = status_code
    debug_trace.append(item)


def _follow_idp_chain(
    client: HttpClient,
    response_url: str,
    html: str,
    *,
    allow_interactive_step_up: bool,
    debug_trace: list[dict[str, object]] | None = None,
) -> bool:
    """Traverse intermediate IdP pages until Journalen or failure."""
    current_url = response_url
    current_html = html
    last_url = ""
    same_url_count = 0
    step_up_attempts = 0
    acs_recovery_attempts = 0
    cookie_bootstrap_done = False
    _append_trace(
        debug_trace,
        action="idp_chain_start",
        url=current_url,
    )
    for _ in range(10):
        host = urlparse(current_url).netloc
        if current_url == last_url:
            same_url_count += 1
        else:
            last_url = current_url
            same_url_count = 1
        # region agent log
        _debug_log(
            run_id="journal-session",
            hypothesis_id="J1",
            location="journal.py:_follow_idp_chain",
            message="chain_iteration",
            data={
                "url_host": host,
                "url_path": urlparse(current_url).path,
                "same_url_count": same_url_count,
                "has_journal_launch_link": _has_journal_launch_link(current_html),
                "has_saml_response": "SAMLResponse" in current_html,
            },
        )
        # endregion
        grp_probe = _probe_grp_pollstatus(client, current_url)
        if grp_probe and allow_interactive_step_up:
            rfa = str(grp_probe.get("rfacode", ""))
            has_location = bool(str(grp_probe.get("location", "")).strip())
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J26",
                location="journal.py:_follow_idp_chain",
                message="grp_probe_decision",
                data={
                    "url_host": host,
                    "url_path": urlparse(current_url).path,
                    "allow_interactive_step_up": allow_interactive_step_up,
                    "rfa": rfa,
                    "has_location": has_location,
                    "step_up_attempts": step_up_attempts,
                    "will_run_step_up": (
                        rfa in {"code-grp-rfa1-qr", "code-grp-rfa17-qr"}
                        and not has_location
                    ),
                },
            )
            # endregion
            if rfa in {"code-grp-rfa1-qr", "code-grp-rfa17-qr"} and not has_location:
                if step_up_attempts >= 1:
                    # region agent log
                    _debug_log(
                        run_id="journal-session",
                        hypothesis_id="J15",
                        location="journal.py:_follow_idp_chain",
                        message="step_up_retry_blocked",
                        data={
                            "url_host": host,
                            "url_path": urlparse(current_url).path,
                            "step_up_attempts": step_up_attempts,
                        },
                    )
                    # endregion
                    return False
                step_up_attempts += 1
                prompt_text = "Skanna BankID-koden för Journalen."
                if acs_recovery_attempts > 0:
                    prompt_text = (
                        "Skanna BankID-koden igen för att slutföra "
                        "Journalen-inloggningen."
                    )
                # region agent log
                _debug_log(
                    run_id="journal-session",
                    hypothesis_id="J41",
                    location="journal.py:_follow_idp_chain",
                    message="step_up_prompt_context",
                    data={
                        "step_up_attempts": step_up_attempts,
                        "acs_recovery_attempts": acs_recovery_attempts,
                        "secondary_prompt": acs_recovery_attempts > 0,
                    },
                )
                # endregion
                ok, new_url, new_html = _run_grp_step_up(
                    client,
                    current_url,
                    prompt_text,
                )
                next_form = _extract_saml_form(new_html, new_url)
                # region agent log
                _debug_log(
                    run_id="journal-session",
                    hypothesis_id="J6",
                    location="journal.py:_follow_idp_chain",
                    message="step_up_result",
                    data={
                        "ok": ok,
                        "url_host": urlparse(new_url).netloc,
                        "url_path": urlparse(new_url).path,
                        "has_saml_form": next_form is not None,
                        "saml_action_host": (
                            urlparse(next_form[0]).netloc
                            if next_form is not None
                            else ""
                        ),
                    },
                )
                # endregion
                current_url = new_url
                current_html = new_html
                if ok:
                    continue
        if _is_authenticated_journal_url(current_url):
            _append_trace(
                debug_trace,
                action="idp_chain_reached_journal",
                url=current_url,
            )
            return True
        if "journalen.1177.se" in host:
            if urlparse(current_url).path.lower().startswith("/loggedout/"):
                # region agent log
                _debug_log(
                    run_id="journal-session",
                    hypothesis_id="J15",
                    location="journal.py:_follow_idp_chain",
                    message="journal_loggedout_terminal",
                    data={
                        "url_host": host,
                        "url_path": urlparse(current_url).path,
                        "step_up_attempts": step_up_attempts,
                    },
                )
                # endregion
                _append_trace(
                    debug_trace,
                    action="idp_chain_loggedout",
                    url=current_url,
                )
                return False
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J11",
                location="journal.py:_follow_idp_chain",
                message="journal_host_not_authenticated",
                data={
                    "url_host": host,
                    "url_path": urlparse(current_url).path,
                },
            )
            # endregion
        if host == "e-tjanster.1177.se" and urlparse(current_url).path.endswith(
            "/mvk/settings.xhtml",
        ):
            soup = BeautifulSoup(current_html, "html.parser")
            forms = soup.select("form")
            form_summaries: list[dict[str, object]] = []
            extracted_form = _extract_generic_form(current_html, current_url)
            extracted_field_names: list[str] = []
            if extracted_form:
                _, _, extracted_fields = extracted_form
                extracted_field_names = sorted(list(extracted_fields.keys()))
            for form in forms[:4]:
                action = str(form.get("action", "")).strip()
                method = str(form.get("method", "post")).lower()
                field_names: list[str] = []
                button_names: list[str] = []
                for node in form.select(
                    "input[name], textarea[name], button[name], select[name]",
                ):
                    name = str(node.get("name", "")).strip()
                    if name:
                        field_names.append(name)
                    if node.name == "button" and name:
                        button_names.append(name)
                    if len(field_names) >= 12:
                        break
                form_summaries.append(
                    {
                        "action": action,
                        "method": method,
                        "has_viewstate": any(
                            name.lower() == "javax.faces.viewstate"
                            for name in field_names
                        ),
                        "field_names": field_names,
                        "button_names": button_names,
                    },
                )
            onclick_hits: list[str] = []
            for node in soup.select("[onclick]"):
                onclick = str(node.get("onclick", ""))
                if "journal" in onclick.lower():
                    onclick_hits.append(onclick[:180])
                if len(onclick_hits) >= 6:
                    break
            journal_links: list[dict[str, str]] = []
            for link in soup.select("a[href]"):
                href = str(link.get("href", "")).strip()
                if "journalen.1177.se" not in href.lower():
                    continue
                journal_links.append(
                    {
                        "href": href,
                        "id": str(link.get("id", "")),
                        "class": str(link.get("class", "")),
                        "data_href": str(link.get("data-href", "")),
                        "onclick": str(link.get("onclick", ""))[:180],
                        "text": link.get_text(" ", strip=True)[:80],
                    },
                )
                if len(journal_links) >= 6:
                    break
            submit_controls: list[dict[str, str]] = []
            for control in soup.select(
                "button[name], input[type='submit'][name], "
                "input[type='image'][name]",
            ):
                name = str(control.get("name", "")).strip()
                if not name:
                    continue
                submit_controls.append(
                    {
                        "tag": control.name or "",
                        "name": name,
                        "value": str(control.get("value", ""))[:80],
                        "id": str(control.get("id", "")),
                    },
                )
                if len(submit_controls) >= 10:
                    break
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J18",
                location="journal.py:_follow_idp_chain",
                message="settings_page_launch_primitives",
                data={
                    "form_count": len(forms),
                    "form_summaries": form_summaries,
                    "onclick_hits": onclick_hits,
                    "extracted_field_count": len(extracted_field_names),
                    "extracted_field_names": extracted_field_names[:16],
                    "journal_links": journal_links,
                    "submit_controls": submit_controls,
                },
            )
            # endregion
        saml_form = _extract_saml_form(current_html, current_url)
        if "sign_in" in urlparse(current_url).path:
            soup = BeautifulSoup(current_html, "html.parser")
            form_count = len(soup.select("form"))
            first_form = soup.select_one("form")
            first_keys: list[str] = []
            if first_form:
                for node in first_form.select("input[name], textarea[name]"):
                    key = str(node.get("name", "")).strip()
                    if key:
                        first_keys.append(key)
                    if len(first_keys) >= 8:
                        break
            sign_in_nav_hrefs: list[str] = []
            for link in soup.select("a[href]"):
                href = str(link.get("href", "")).strip()
                lower = href.lower()
                if any(
                    marker in lower
                    for marker in (
                        "/samlv2/idp/",
                        "/mg-local/",
                        "journalen.1177.se",
                    )
                ):
                    sign_in_nav_hrefs.append(href[:180])
                if len(sign_in_nav_hrefs) >= 8:
                    break
            has_script_location = bool(
                re.search(
                    r"(?:location\.href|window\.location)\s*=",
                    current_html,
                    re.IGNORECASE,
                ),
            )
            cookie_bootstrap_url = ""
            for node in soup.select("[src]"):
                src = str(node.get("src", "")).strip()
                if "/mg-local/cookie.html" in src.lower():
                    cookie_bootstrap_url = urljoin(current_url, src)
                    break
            if not cookie_bootstrap_url:
                match = re.search(
                    r"""['"]([^'"]*/mg-local/cookie\.html[^'"]*)['"]""",
                    current_html,
                    re.IGNORECASE,
                )
                if match:
                    cookie_bootstrap_url = urljoin(current_url, match.group(1))
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J7",
                location="journal.py:_follow_idp_chain",
                message="sign_in_page_parse",
                data={
                    "url_host": host,
                    "url_path": urlparse(current_url).path,
                    "form_count": form_count,
                    "saml_form_found": saml_form is not None,
                    "first_form_keys": first_keys,
                    "cookie_bootstrap_url": cookie_bootstrap_url[:180],
                    "cookie_bootstrap_done": cookie_bootstrap_done,
                },
            )
            # endregion
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J20",
                location="journal.py:_follow_idp_chain",
                message="sign_in_navigation_primitives",
                data={
                    "url_host": host,
                    "url_path": urlparse(current_url).path,
                    "nav_href_count": len(sign_in_nav_hrefs),
                    "nav_hrefs": sign_in_nav_hrefs,
                    "has_script_location": has_script_location,
                },
            )
            # endregion
            if cookie_bootstrap_url and not cookie_bootstrap_done:
                bootstrap_resp = client.request(
                    "GET",
                    cookie_bootstrap_url,
                    allow_status={200, 302, 401, 403},
                )
                cookie_bootstrap_done = True
                # region agent log
                _debug_log(
                    run_id="journal-session",
                    hypothesis_id="J33",
                    location="journal.py:_follow_idp_chain",
                    message="cookie_bootstrap_fetch",
                    data={
                        "url_host": urlparse(cookie_bootstrap_url).netloc,
                        "url_path": urlparse(cookie_bootstrap_url).path,
                        "status_code": bootstrap_resp.status_code,
                        "response_host": urlparse(
                            str(bootstrap_resp.url),
                        ).netloc,
                        "response_path": urlparse(
                            str(bootstrap_resp.url),
                        ).path,
                        "set_cookie_present": bool(
                            bootstrap_resp.headers.get("Set-Cookie"),
                        ),
                    },
                )
                # endregion
        if saml_form:
            action, fields = saml_form
            action_host = urlparse(action).netloc
            relay_state = str(fields.get("RelayState", ""))
            pre_cookie_names: list[str] = []
            idp_cookie_names = _cookie_names_for_host(
                client,
                "funktionstjanster.se",
            )
            idp_cookie_profile = _cookie_profile_for_host(
                client,
                "funktionstjanster.se",
            )
            idp_cookie_scopes = _cookie_scope_rows_for_host(
                client,
                "funktionstjanster.se",
            )
            if "journalen.1177.se" in action_host:
                pre_cookie_names = _cookie_names_for_host(
                    client,
                    "journalen.1177.se",
                )
            journal_cookie_profile_before = _cookie_profile_for_host(
                client,
                "journalen.1177.se",
            )
            journal_cookie_scopes_before = _cookie_scope_rows_for_host(
                client,
                "journalen.1177.se",
            )
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J12",
                location="journal.py:_follow_idp_chain",
                message="post_saml_form_attempt",
                data={
                    "current_host": host,
                    "current_path": urlparse(current_url).path,
                    "action_host": action_host,
                    "action_path": urlparse(action).path,
                    "field_keys": sorted(list(fields.keys()))[:8],
                    "relaystate_has_timedout": "timedout"
                    in relay_state.lower(),
                    "relaystate_has_dashboard": "dashboard"
                    in relay_state.lower(),
                    "relaystate_has_logout": "logout" in relay_state.lower(),
                    "relaystate_len": len(relay_state),
                    "idp_cookie_count_before_post": len(idp_cookie_names),
                    "idp_cookie_names_before_post": idp_cookie_names[:10],
                    "journal_cookie_count_before_post": len(pre_cookie_names),
                    "journal_cookie_names_before_post": pre_cookie_names[:10],
                    "idp_cookie_profile_before": idp_cookie_profile,
                    "journal_cookie_profile_before": journal_cookie_profile_before,
                    "idp_cookie_scopes_before": idp_cookie_scopes[:12],
                    "journal_cookie_scopes_before": (
                        journal_cookie_scopes_before[:12]
                    ),
                },
            )
            # endregion
            pre_post_cookies = client.cookies
            try:
                resp = client.request(
                    "POST",
                    action,
                    data=fields,
                    allow_status={200, 302, 401, 403},
                )
            except CliError as exc:
                if (
                    exc.code == "upstream_error"
                    and urlparse(action).netloc == "journalen.1177.se"
                    and urlparse(action).path == "/AuthServices/Acs"
                ):
                    # region agent log
                    _debug_log(
                        run_id="journal-session",
                        hypothesis_id="J36",
                        location="journal.py:_follow_idp_chain",
                        message="acs_post_upstream_error",
                        data={
                            "status_code": int(exc.details.get("status_code", 0)),
                            "host": str(exc.details.get("host", "")),
                            "url": str(exc.details.get("url", "")),
                            "current_host": host,
                            "current_path": urlparse(current_url).path,
                        },
                    )
                    # endregion
                    dashboard_probe = client.request(
                        "GET",
                        f"{BASE_URL}/Dashboard",
                        allow_status={200, 302, 401, 403},
                    )
                    # region agent log
                    _debug_log(
                        run_id="journal-session",
                        hypothesis_id="J36",
                        location="journal.py:_follow_idp_chain",
                        message="acs_error_followup_probe",
                        data={
                            "dashboard_host": urlparse(
                                str(dashboard_probe.url),
                            ).netloc,
                            "dashboard_path": urlparse(
                                str(dashboard_probe.url),
                            ).path,
                            "dashboard_status": dashboard_probe.status_code,
                        },
                    )
                    # endregion
                raise
            if "journalen.1177.se" in action_host:
                post_cookie_names = _cookie_names_for_host(
                    client,
                    "journalen.1177.se",
                )
                journal_cookie_profile_after = _cookie_profile_for_host(
                    client,
                    "journalen.1177.se",
                )
                # region agent log
                _debug_log(
                    run_id="journal-session",
                    hypothesis_id="J16",
                    location="journal.py:_follow_idp_chain",
                    message="post_saml_form_cookie_state",
                    data={
                        "request_cookie_names": _cookie_names_from_header(
                            resp.request.headers.get("Cookie", ""),
                        ),
                        "response_host": urlparse(str(resp.url)).netloc,
                        "response_path": urlparse(str(resp.url)).path,
                        "status_code": resp.status_code,
                        "set_cookie_present": bool(resp.headers.get("Set-Cookie")),
                        "journal_cookie_count_after_post": len(post_cookie_names),
                        "journal_cookie_names_after_post": post_cookie_names[:10],
                        "response_has_timedout_word": "timedout"
                        in resp.text.lower(),
                        "response_has_session_word": "session"
                        in resp.text.lower(),
                        "history_len": len(resp.history),
                        "history_paths": [
                            urlparse(str(item.url)).path
                            for item in resp.history[:6]
                        ],
                        "history_hosts": [
                            urlparse(str(item.url)).netloc
                            for item in resp.history[:6]
                        ],
                        "sloreq_ids": [
                            path.split("/")[-1]
                            for path in [
                                urlparse(str(item.url)).path
                                for item in resp.history[:6]
                            ]
                            if "/samlv2/idp/sloreq/" in path
                        ],
                        "request_has_referer": bool(
                            resp.request.headers.get("Referer"),
                        ),
                        "request_has_origin": bool(
                            resp.request.headers.get("Origin"),
                        ),
                        "request_content_type": resp.request.headers.get(
                            "Content-Type",
                            "",
                        ),
                        "journal_cookie_scopes_after": (
                            _cookie_scope_rows_for_host(
                                client,
                                "journalen.1177.se",
                            )[:12]
                        ),
                        "journal_cookie_profile_after": (
                            journal_cookie_profile_after
                        ),
                    },
                )
                # endregion
                if urlparse(str(resp.url)).path.lower().startswith(
                    "/loggedout/timedoutresult",
                ):
                    if acs_recovery_attempts < 1:
                        acs_recovery_attempts += 1
                        recovery_cookies = [
                            item
                            for item in pre_post_cookies
                            if "journalen.1177.se"
                            not in str(item.get("domain", "")).lower()
                        ]
                        recovery_client = HttpClient(
                            cookies=recovery_cookies,
                            max_retries=client._max_retries,
                        )
                        try:
                            recovery_resp = recovery_client.request(
                                "POST",
                                action,
                                data=fields,
                                allow_status={200, 302, 401, 403},
                            )
                            # region agent log
                            _debug_log(
                                run_id="journal-session",
                                hypothesis_id="J39",
                                location="journal.py:_follow_idp_chain",
                                message="acs_recovery_retry_without_journal_cookies",
                                data={
                                    "response_host": urlparse(
                                        str(recovery_resp.url),
                                    ).netloc,
                                    "response_path": urlparse(
                                        str(recovery_resp.url),
                                    ).path,
                                    "status_code": recovery_resp.status_code,
                                    "history_paths": [
                                        urlparse(str(item.url)).path
                                        for item in recovery_resp.history[:6]
                                    ],
                                    "cookie_count_before": len(
                                        recovery_cookies,
                                    ),
                                    "step_up_attempts_before_reset": (
                                        step_up_attempts
                                    ),
                                },
                            )
                            # endregion
                            client.set_cookies(recovery_client.cookies)
                            step_up_attempts = 0
                            # region agent log
                            _debug_log(
                                run_id="journal-session",
                                hypothesis_id="J40",
                                location="journal.py:_follow_idp_chain",
                                message="reset_step_up_after_acs_recovery",
                                data={
                                    "step_up_attempts": step_up_attempts,
                                    "current_host": urlparse(
                                        str(recovery_resp.url),
                                    ).netloc,
                                    "current_path": urlparse(
                                        str(recovery_resp.url),
                                    ).path,
                                },
                            )
                            # endregion
                            current_url = str(recovery_resp.url)
                            current_html = recovery_resp.text
                            _append_trace(
                                debug_trace,
                                action=(
                                    "acs_recovery_retry_without_journal_cookies"
                                ),
                                url=current_url,
                                status_code=recovery_resp.status_code,
                            )
                            continue
                        except CliError as retry_exc:
                            # region agent log
                            _debug_log(
                                run_id="journal-session",
                                hypothesis_id="J39",
                                location="journal.py:_follow_idp_chain",
                                message=(
                                    "acs_recovery_retry_without_journal_cookies_error"
                                ),
                                data={
                                    "error_code": retry_exc.code,
                                    "status_code": int(
                                        retry_exc.details.get("status_code", 0),
                                    ),
                                    "host": str(retry_exc.details.get("host", "")),
                                    "url": str(retry_exc.details.get("url", "")),
                                },
                            )
                            # endregion
            current_url = str(resp.url)
            current_html = resp.text
            _append_trace(
                debug_trace,
                action="post_saml_form",
                url=current_url,
                status_code=resp.status_code,
            )
            continue

        launch_url = _extract_journal_launch_url(current_html, current_url)
        if launch_url:
            launch_markup = _extract_journal_link_markup(current_html, current_url)
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J29",
                location="journal.py:_follow_idp_chain",
                message="journal_launch_markup",
                data=launch_markup,
            )
            # endregion
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J4",
                location="journal.py:_follow_idp_chain",
                message="follow_journal_launch_link",
                data={
                    "launch_host": urlparse(launch_url).netloc,
                    "launch_path": urlparse(launch_url).path,
                },
            )
            # endregion
            resp = client.request(
                "GET",
                launch_url,
                allow_status={200, 302, 401, 403},
            )
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J8",
                location="journal.py:_follow_idp_chain",
                message="journal_launch_response",
                data={
                    "response_host": urlparse(str(resp.url)).netloc,
                    "response_path": urlparse(str(resp.url)).path,
                    "status_code": resp.status_code,
                    "history_len": len(resp.history),
                    "history_paths": [
                        urlparse(str(item.url)).path
                        for item in resp.history[:6]
                    ],
                    "history_hosts": [
                        urlparse(str(item.url)).netloc
                        for item in resp.history[:6]
                    ],
                    "req_ids": [
                        path.split("/")[-1]
                        for path in [
                            urlparse(str(item.url)).path
                            for item in resp.history[:6]
                        ]
                        if "/samlv2/idp/req/" in path
                    ],
                    "response_content_type": resp.headers.get("Content-Type", ""),
                    "response_cache_control": resp.headers.get("Cache-Control", ""),
                    "response_x_frame_options": resp.headers.get(
                        "X-Frame-Options",
                        "",
                    ),
                },
            )
            # endregion
            current_url = str(resp.url)
            current_html = resp.text
            _append_trace(
                debug_trace,
                action="follow_journal_launch_link",
                url=current_url,
                status_code=resp.status_code,
            )
            continue

        generic_form = _extract_generic_form(current_html, current_url)
        if generic_form:
            action, method, fields = generic_form
            has_jsf_view_state = any(
                key.lower() in {
                    "javax.faces.viewstate",
                    "jakarta.faces.viewstate",
                }
                for key in fields
            )
            if method.lower() == "post" and not has_jsf_view_state:
                # region agent log
                _debug_log(
                    run_id="journal-session",
                    hypothesis_id="J2",
                    location="journal.py:_follow_idp_chain",
                    message="skip_generic_post_without_viewstate",
                    data={
                        "action_host": urlparse(action).netloc,
                        "action_path": urlparse(action).path,
                        "field_count": len(fields),
                    },
                )
                # endregion
                generic_form = None
        if generic_form:
            action, method, fields = generic_form
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J2",
                location="journal.py:_follow_idp_chain",
                message="submit_generic_form",
                data={
                    "method": method,
                    "action_host": urlparse(action).netloc,
                    "action_path": urlparse(action).path,
                    "field_count": len(fields),
                    "has_jsf_view_state": any(
                        key.lower() in {
                            "javax.faces.viewstate",
                            "jakarta.faces.viewstate",
                        }
                        for key in fields
                    ),
                },
            )
            # endregion
            resp = client.request(
                method.upper(),
                action,
                data=fields,
                allow_status={200, 302, 401, 403},
            )
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J19",
                location="journal.py:_follow_idp_chain",
                message="generic_form_response",
                data={
                    "request_method": method.upper(),
                    "request_host": urlparse(action).netloc,
                    "request_path": urlparse(action).path,
                    "response_host": urlparse(str(resp.url)).netloc,
                    "response_path": urlparse(str(resp.url)).path,
                    "status_code": resp.status_code,
                },
            )
            # endregion
            current_url = str(resp.url)
            current_html = resp.text
            _append_trace(
                debug_trace,
                action=f"submit_{method}_form",
                url=current_url,
                status_code=resp.status_code,
            )
            continue

        sign_in_url = _req_to_sign_in_url(current_url)
        if sign_in_url:
            resp = client.request(
                "GET",
                sign_in_url,
                allow_status={200, 302, 401, 403},
            )
            current_url = str(resp.url)
            current_html = resp.text
            _append_trace(
                debug_trace,
                action="req_to_sign_in_get",
                url=current_url,
                status_code=resp.status_code,
            )
            continue

        next_url = _extract_next_idp_url(current_html, current_url)
        if not next_url:
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J3",
                location="journal.py:_follow_idp_chain",
                message="chain_no_next_url",
                data={
                    "url_host": host,
                    "url_path": urlparse(current_url).path,
                    "html_len": len(current_html),
                    "form_count": len(BeautifulSoup(current_html, "html.parser").select("form")),
                },
            )
            # endregion
            _append_trace(
                debug_trace,
                action="idp_chain_stuck",
                url=current_url,
            )
            return False
        resp = client.request(
            "GET",
            next_url,
            allow_status={200, 302, 401, 403},
        )
        current_url = str(resp.url)
        current_html = resp.text
        _append_trace(
            debug_trace,
            action="follow_next_idp_url",
            url=current_url,
            status_code=resp.status_code,
        )
    _append_trace(
        debug_trace,
        action="idp_chain_iteration_limit",
        url=current_url,
    )
    return False


def establish_journal_session(
    client: HttpClient,
    *,
    allow_interactive_step_up: bool = False,
    debug_trace: list[dict[str, object]] | None = None,
) -> bool:
    """Ensure a valid Journalen browser session exists."""
    dashboard_url = f"{BASE_URL}/Dashboard"
    response = client.request(
        "GET",
        dashboard_url,
        allow_status={200, 302, 401, 403},
    )
    response_url = str(response.url)
    _append_trace(
        debug_trace,
        action="journal_dashboard_get",
        url=response_url,
        status_code=response.status_code,
    )
    response_host = urlparse(response_url).netloc
    if _is_authenticated_journal_url(response_url):
        probe = client.request(
            "GET",
            f"{BASE_URL}/JournalCategories/CareDocumentation",
            allow_status={200, 302, 401, 403},
        )
        _append_trace(
            debug_trace,
            action="journal_caredoc_probe",
            url=str(probe.url),
            status_code=probe.status_code,
        )
        return "journalen.1177.se" in urlparse(str(probe.url)).netloc

    if allow_interactive_step_up and "journalen.1177.se" not in response_host:
        preflight_cookies = client.cookies
        kept_cookies = [
            item
            for item in preflight_cookies
            if "journalen.1177.se"
            not in str(item.get("domain", "")).lower()
        ]
        removed_count = len(preflight_cookies) - len(kept_cookies)
        if removed_count > 0:
            client.set_cookies(kept_cookies)
            # region agent log
            _debug_log(
                run_id="journal-session",
                hypothesis_id="J46",
                location="journal.py:establish_journal_session",
                message="preflight_clear_journal_cookies",
                data={
                    "removed_cookie_count": removed_count,
                    "cookie_count_before": len(preflight_cookies),
                    "cookie_count_after": len(kept_cookies),
                    "dashboard_host": response_host,
                },
            )
            # endregion

    if not _follow_idp_chain(
        client,
        response_url,
        response.text,
        allow_interactive_step_up=allow_interactive_step_up,
        debug_trace=debug_trace,
    ):
        return False
    probe = client.request(
        "GET",
        f"{BASE_URL}/JournalCategories/CareDocumentation",
        allow_status={200, 302, 401, 403},
    )
    _append_trace(
        debug_trace,
        action="journal_caredoc_probe",
        url=str(probe.url),
        status_code=probe.status_code,
    )
    return "journalen.1177.se" in urlparse(str(probe.url)).netloc


def _parse_json_response(
    *,
    response_text: str,
    endpoint: str,
    request_url: str,
    response_url: str,
    content_type: str,
) -> dict[str, Any]:
    """Parse JSON response and raise stable errors on mismatch."""
    try:
        payload = json.loads(response_text)
    except ValueError as exc:
        lower = response_text.lower()
        is_html = "<html" in lower or "<!doctype html" in lower
        host = urlparse(response_url).netloc
        if is_html or "shibboleth" in lower or "logga in" in lower:
            raise CliError(
                error="Journal session is not authenticated",
                code="auth_required",
                exit_code=exit_codes.AUTH,
                details={
                    "endpoint": endpoint,
                    "response_url": response_url,
                    "host": host,
                },
            ) from exc
        preview = response_text[:160].strip()
        raise CliError(
            error="Journal endpoint returned non-JSON response",
            code="upstream_invalid_json",
            exit_code=exit_codes.UPSTREAM,
            details={
                "endpoint": endpoint,
                "request_url": request_url,
                "response_url": response_url,
                "content_type": content_type,
                "preview": preview,
            },
        ) from exc
    if not isinstance(payload, dict):
        raise CliError(
            error="Unexpected payload from journal endpoint",
            code="unexpected_payload",
            exit_code=exit_codes.UPSTREAM,
            details={"endpoint": endpoint, "type": type(payload).__name__},
        )
    return payload


def bootstrap_care_documentation(
    client: HttpClient,
    *,
    debug_trace: list[dict[str, object]] | None = None,
) -> JournalBootstrap:
    """Open care documentation page and extract anti-forgery token."""
    url = f"{BASE_URL}/JournalCategories/CareDocumentation"
    response = client.request("GET", url)
    _append_trace(
        debug_trace,
        action="bootstrap_caredoc_get",
        url=str(response.url),
        status_code=response.status_code,
    )
    response_host = urlparse(str(response.url)).netloc
    if "journalen.1177.se" not in response_host:
        if not establish_journal_session(
            client,
            allow_interactive_step_up=True,
            debug_trace=debug_trace,
        ):
            raise CliError(
                error="Journal session is not authenticated",
                code="auth_required",
                exit_code=exit_codes.AUTH,
                details={
                    "response_url": str(response.url),
                    "auth_trace": debug_trace,
                },
            )
        response = client.request("GET", url)
        _append_trace(
            debug_trace,
            action="bootstrap_caredoc_get_retry",
            url=str(response.url),
            status_code=response.status_code,
        )
    html = response.text
    soup = BeautifulSoup(html, "html.parser")
    token_input = soup.select_one("input[name='__RequestVerificationToken']")
    token = None
    if token_input and token_input.get("value"):
        token = str(token_input.get("value"))
    return JournalBootstrap(
        verification_token=token,
        page_url=str(response.url),
    )


def bootstrap_laboratory_outcome(
    client: HttpClient,
    *,
    debug_trace: list[dict[str, object]] | None = None,
) -> JournalBootstrap:
    """Open laboratory outcome page and extract anti-forgery token."""
    url = f"{BASE_URL}/JournalCategories/LaboratoryOutcome"
    response = client.request("GET", url)
    _append_trace(
        debug_trace,
        action="bootstrap_laboratoryoutcome_get",
        url=str(response.url),
        status_code=response.status_code,
    )
    response_host = urlparse(str(response.url)).netloc
    if "journalen.1177.se" not in response_host:
        if not establish_journal_session(
            client,
            allow_interactive_step_up=True,
            debug_trace=debug_trace,
        ):
            raise CliError(
                error="Journal session is not authenticated",
                code="auth_required",
                exit_code=exit_codes.AUTH,
                details={
                    "response_url": str(response.url),
                    "auth_trace": debug_trace,
                },
            )
        response = client.request("GET", url)
        _append_trace(
            debug_trace,
            action="bootstrap_laboratoryoutcome_get_retry",
            url=str(response.url),
            status_code=response.status_code,
        )
    soup = BeautifulSoup(response.text, "html.parser")
    token_input = soup.select_one("input[name='__RequestVerificationToken']")
    token = None
    if token_input and token_input.get("value"):
        token = str(token_input.get("value"))
    return JournalBootstrap(
        verification_token=token,
        page_url=str(response.url),
    )


def poll_care_documentation(
    client: HttpClient,
    *,
    page: int,
    page_size: int,
    sort_by: str = "Date",
    sort_order: str = "desc",
    date_from: str = "",
    date_to: str = "",
    verification_token: str | None = None,
) -> dict[str, Any]:
    """Fetch entries for care documentation section."""
    if page < 1:
        raise CliError(
            error="Invalid page value",
            code="invalid_argument",
            exit_code=exit_codes.USAGE,
            details={"field": "page"},
        )
    if page_size < 1 or page_size > 100:
        raise CliError(
            error="Invalid page size",
            code="invalid_argument",
            exit_code=exit_codes.USAGE,
            details={"field": "page_size", "max": 100},
        )
    sort_key_map = {
        "date": "DocumentTime",
        "documenttime": "DocumentTime",
        "type": "Type",
        "author": "AuthorName",
        "authorname": "AuthorName",
        "careunit": "CareUnit",
    }
    order_key_map = {
        "asc": "Ascending",
        "ascending": "Ascending",
        "desc": "Descending",
        "descending": "Descending",
    }
    normalized_sort = sort_key_map.get(sort_by.strip().lower(), sort_by)
    normalized_order = order_key_map.get(
        sort_order.strip().lower(),
        "Descending",
    )
    skip = (page - 1) * page_size
    filter_state: dict[str, Any] = {
        "Skip": skip,
        "Take": page_size,
        "AuthorName": [],
        "Type": [],
        "InformationType": [],
        "CareUnit": [],
        "VaccineName": [],
        "VaccineDisease": [],
        "MedicationName": [],
        "OngoingTreatment": [],
        "LoggedPersonName": [],
        "LoggedPersonRole": [],
        "LoggedPersonCareProvider": [],
        "OrderDirection": normalized_order,
        "OrderByEnum": normalized_sort,
        "FilterArrays": {},
        "GetFiltersView": True,
    }
    if date_from:
        filter_state["From"] = date_from
    if date_to:
        filter_state["To"] = date_to
    headers = dict(AJAX_JSON_HEADERS)
    if verification_token:
        headers["__RequestVerificationToken"] = verification_token
    url = f"{BASE_URL}/journalcategories/caredocumentation/poll"
    response = client.request(
        "POST",
        url,
        headers=headers,
        json_body={"fs": filter_state},
    )
    return _parse_json_response(
        response_text=response.text,
        endpoint="caredocumentation/poll",
        request_url=url,
        response_url=str(response.url),
        content_type=response.headers.get("Content-Type", ""),
    )


def poll_care_documentation_until_done(
    client: HttpClient,
    *,
    page: int,
    page_size: int,
    sort_by: str = "Date",
    sort_order: str = "desc",
    date_from: str = "",
    date_to: str = "",
    verification_token: str | None = None,
    timeout_seconds: float = 8.0,
    poll_interval_seconds: float = 0.35,
) -> dict[str, Any]:
    """Poll care documentation until loading is done or timeout."""
    started_at = time.monotonic()
    attempts = 0
    timed_out = False
    payload: dict[str, Any] = {}
    partial_fragments: list[str] = []
    while True:
        attempts += 1
        payload = poll_care_documentation(
            client,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            date_from=date_from,
            date_to=date_to,
            verification_token=verification_token,
        )
        partial_html = str(payload.get("PartialView", ""))
        if partial_html:
            partial_fragments.append(partial_html)
        done = bool(payload.get("DataFetchingForAllBatchesIsDone"))
        loading = bool(payload.get("DataIsLoading"))
        error_occurred = bool(payload.get("ErrorOccurred"))
        fetch_timed_out = bool(payload.get("DataFetchingTimedOut"))
        if done or not loading or error_occurred or fetch_timed_out:
            break
        elapsed = time.monotonic() - started_at
        if elapsed >= timeout_seconds:
            timed_out = True
            break
        time.sleep(max(0.0, poll_interval_seconds))
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    combined_partial = "".join(partial_fragments)
    return {
        "payload": payload,
        "attempts": attempts,
        "timed_out": timed_out,
        "elapsed_ms": elapsed_ms,
        "combined_partial_view": combined_partial,
    }


def poll_laboratory_outcome(
    client: HttpClient,
    *,
    date_from: str = "",
    date_to: str = "",
    answer_type_filter: str = "",
    ordered_by_filter: str = "",
    care_unit_filter: str = "",
    verification_token: str | None = None,
) -> dict[str, Any]:
    """Fetch laboratory outcome list payload."""
    headers = dict(AJAX_HEADERS)
    if verification_token:
        headers["__RequestVerificationToken"] = verification_token
    form_data = {
        "DateFrom": date_from,
        "DateTo": date_to,
        "AnswerTypeFilter": answer_type_filter,
        "OrderedByFilter": ordered_by_filter,
        "CareUnitFilter": care_unit_filter,
    }
    url = f"{BASE_URL}/journalcategories/laboratoryoutcome/poll"
    response = client.request(
        "POST",
        url,
        headers=headers,
        data=form_data,
    )
    return _parse_json_response(
        response_text=response.text,
        endpoint="laboratoryoutcome/poll",
        request_url=url,
        response_url=str(response.url),
        content_type=response.headers.get("Content-Type", ""),
    )


def poll_laboratory_outcome_until_done(
    client: HttpClient,
    *,
    date_from: str = "",
    date_to: str = "",
    answer_type_filter: str = "",
    ordered_by_filter: str = "",
    care_unit_filter: str = "",
    verification_token: str | None = None,
    timeout_seconds: float = 8.0,
    poll_interval_seconds: float = 0.35,
) -> dict[str, Any]:
    """Poll laboratory outcome until loading is done or timeout."""
    started_at = time.monotonic()
    attempts = 0
    timed_out = False
    payload: dict[str, Any] = {}
    partial_fragments: list[str] = []
    while True:
        attempts += 1
        payload = poll_laboratory_outcome(
            client,
            date_from=date_from,
            date_to=date_to,
            answer_type_filter=answer_type_filter,
            ordered_by_filter=ordered_by_filter,
            care_unit_filter=care_unit_filter,
            verification_token=verification_token,
        )
        partial_html = str(payload.get("PartialView", ""))
        if partial_html:
            partial_fragments.append(partial_html)
        done = bool(payload.get("DataFetchingForAllBatchesIsDone"))
        loading = bool(payload.get("DataIsLoading"))
        error_occurred = bool(payload.get("ErrorOccurred"))
        fetch_timed_out = bool(payload.get("DataFetchingTimedOut"))
        if done or not loading or error_occurred or fetch_timed_out:
            break
        elapsed = time.monotonic() - started_at
        if elapsed >= timeout_seconds:
            timed_out = True
            break
        time.sleep(max(0.0, poll_interval_seconds))
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    return {
        "payload": payload,
        "attempts": attempts,
        "timed_out": timed_out,
        "elapsed_ms": elapsed_ms,
        "combined_partial_view": "".join(partial_fragments),
    }


def extract_rows_from_partial_view(partial_html: str) -> list[dict[str, str]]:
    """Extract coarse rows from poll PartialView HTML."""
    soup = BeautifulSoup(partial_html, "html.parser")
    rows: list[dict[str, str]] = []
    post_rows = soup.select("li.nc-list-post button.nc-list-post-expander")
    for row in post_rows:
        summary = str(row.get("aria-label", "")).strip()
        if not summary:
            summary = row.get_text(" ", strip=True)
        if not summary:
            continue
        row_id = str(row.get("data-id", "")).strip()
        row_date = str(row.get("data-date", "")).strip()
        rows.append(
            {
                "summary": unescape(summary),
                "column_count": "0",
                "entry_id": row_id,
                "entry_date": row_date,
            },
        )
    if rows:
        return rows
    table_rows = soup.select("tr")
    for row in table_rows:
        cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
        if not cells:
            continue
        rows.append(
            {
                "summary": " | ".join(cells),
                "column_count": str(len(cells)),
            }
        )
    if rows:
        return rows
    text = soup.get_text("\n", strip=True)
    if text:
        return [{"summary": text, "column_count": "0"}]
    return []


def _normalize_result_field_name(label: str) -> str:
    base = unicodedata.normalize("NFKD", label).encode(
        "ascii",
        "ignore",
    ).decode("ascii")
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", base.strip().lower())
    return normalized.strip("_")


def _canonical_result_detail_key(normalized_label: str) -> str | None:
    """Map normalized detail labels to stable JSON keys."""
    canonical_map = {
        "analys": "analysis_name",
        "analysnamn": "analysis_name",
        "namn_pa_analys": "analysis_name",
        "provtagningstid": "sample_time",
        "provtagningstidpunkt": "sample_time",
        "provtagningstid_punkt": "sample_time",
        "svarstid": "reported_time",
        "svarstyp": "answer_type",
        "bestalld_av": "ordering_provider",
        "bestallare": "ordering_provider",
        "vardenhet": "care_unit",
        "enhet": "unit",
        "varde": "value",
        "referensintervall": "reference_interval",
        "vidimeringsstatus": "review_status",
        "ovidimerad": "review_status",
    }
    return canonical_map.get(normalized_label)


def _coerce_iso_date(value: object) -> str | None:
    """Return YYYY-MM-DD when present, else None."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if len(raw) < 10:
        return None
    candidate = raw[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", candidate):
        return candidate
    return None


def extract_results_from_partial_view(
    partial_html: str,
) -> list[dict[str, str | None]]:
    """Extract structured laboratory result rows from partial HTML."""
    soup = BeautifulSoup(partial_html, "html.parser")
    rows: list[dict[str, str | None]] = []
    post_rows = soup.select("li.nc-list-post button.nc-list-post-expander")
    for row in post_rows:
        summary = str(row.get("aria-label", "")).strip()
        if not summary:
            summary = row.get_text(" ", strip=True)
        if not summary:
            continue
        row_id = str(row.get("data-id", "")).strip()
        row_date = str(row.get("data-date", "")).strip()
        rows.append(
            {
                "result_id": row_id or None,
                "entry_date": row_date or None,
                "summary": unescape(summary),
                "analysis_name": None,
                "care_unit": None,
                "answer_type": None,
                "review_status": None,
                "raw_text": unescape(summary),
            },
        )
    if rows:
        return rows

    table_rows = soup.select("tr")
    for row in table_rows:
        cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
        if not cells:
            continue
        summary = " | ".join(cells)
        entry_date = cells[0] if cells else ""
        analysis_name = cells[1] if len(cells) > 1 else ""
        care_unit = cells[2] if len(cells) > 2 else ""
        answer_type = cells[3] if len(cells) > 3 else ""
        review_status = cells[4] if len(cells) > 4 else ""
        rows.append(
            {
                "result_id": None,
                "entry_date": entry_date or None,
                "summary": summary,
                "analysis_name": analysis_name or None,
                "care_unit": care_unit or None,
                "answer_type": answer_type or None,
                "review_status": review_status or None,
                "raw_text": summary,
            },
        )
    if rows:
        return rows

    text = soup.get_text("\n", strip=True)
    if text:
        return [
            {
                "result_id": None,
                "entry_date": None,
                "summary": text,
                "analysis_name": None,
                "care_unit": None,
                "answer_type": None,
                "review_status": None,
                "raw_text": text,
            },
        ]
    return []


def _parse_detail_html(detail_html: str) -> dict[str, Any]:
    """Parse detail HTML into normalized fields and key/value pairs."""
    soup = BeautifulSoup(detail_html, "html.parser")
    fields: dict[str, str] = {}
    core_fields: dict[str, str] = {}
    extra_fields: dict[str, str] = {}
    for row in soup.select("dl > dt"):
        label = row.get_text(" ", strip=True)
        value_node = row.find_next_sibling("dd")
        if not label or value_node is None:
            continue
        value = value_node.get_text(" ", strip=True)
        normalized_label = _normalize_result_field_name(label)
        fields[normalized_label] = value
        canonical_key = _canonical_result_detail_key(normalized_label)
        if canonical_key:
            core_fields[canonical_key] = value
        else:
            extra_fields[normalized_label] = value
    if not fields:
        for row in soup.select("table tr"):
            cells = row.select("th, td")
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True)
            value = cells[1].get_text(" ", strip=True)
            if not label:
                continue
            normalized_label = _normalize_result_field_name(label)
            fields[normalized_label] = value
            canonical_key = _canonical_result_detail_key(normalized_label)
            if canonical_key:
                core_fields[canonical_key] = value
            else:
                extra_fields[normalized_label] = value
    measurements: list[dict[str, str | None]] = []
    for row in soup.select("table.nc-laboratory-table tbody tr"):
        cells = row.select("td")
        if len(cells) < 2:
            continue
        analysis_cell = cells[0]
        result_cell = cells[1]
        analysis_link = analysis_cell.select_one("a[data-analysis-name]")
        analysis_name = ""
        analysis_code = ""
        analysis_unit = ""
        if analysis_link is not None:
            analysis_name = str(
                analysis_link.get("data-analysis-name", ""),
            ).strip()
            analysis_code = str(analysis_link.get("data-npu-code", "")).strip()
            analysis_unit = str(
                analysis_link.get("data-analysis-unit", ""),
            ).strip()
        if not analysis_name:
            analysis_name = analysis_cell.get_text(" ", strip=True)
        analysis_text = analysis_cell.get_text(" ", strip=True)
        result_text = result_cell.get_text(" ", strip=True)
        reference_interval = ""
        reference_match = re.search(
            r"Referensintervall:\s*([^\n]+)",
            analysis_text,
        )
        if reference_match:
            reference_interval = reference_match.group(1).strip()
        value_text = ""
        value_node = result_cell.select_one(".iu-fw-bold")
        if value_node is not None:
            value_text = value_node.get_text(" ", strip=True)
        if not value_text:
            value_text = result_text
        comment_text = ""
        comment_node = result_cell.select_one(".iu-sr-only")
        if comment_node is not None:
            comment_text = comment_node.get_text(" ", strip=True)
        review_status = ""
        review_node = result_cell.select_one(".nu-fs-italic")
        if review_node is not None:
            review_status = review_node.get_text(" ", strip=True)
        graph_url = ""
        graph_link = result_cell.select_one("a[title='Visa som graf']")
        if graph_link is not None:
            graph_url = str(graph_link.get("href", "")).strip()
        measurements.append(
            {
                "analysis_name": analysis_name or None,
                "analysis_code": analysis_code or None,
                "analysis_unit": analysis_unit or None,
                "reference_interval": reference_interval or None,
                "result_value": value_text or None,
                "result_comment": comment_text or None,
                "review_status": review_status or None,
                "graph_url": graph_url or None,
                "analysis_text": analysis_text or None,
                "result_text": result_text or None,
            },
        )
    return {
        "fields": fields,
        "core_fields": core_fields,
        "extra_fields": extra_fields,
        "measurements": measurements,
        "text": soup.get_text("\n", strip=True),
    }


def fetch_laboratory_outcome_detail(
    client: HttpClient,
    *,
    result_id: str,
    verification_token: str | None = None,
) -> dict[str, Any]:
    """Fetch detail for one laboratory outcome row."""
    headers = dict(AJAX_HEADERS)
    if verification_token:
        headers["__RequestVerificationToken"] = verification_token
    url = f"{BASE_URL}/journalcategories/laboratoryoutcome/detailview"
    candidates = [
        {"id": result_id},
        {"resultId": result_id},
        {"entryId": result_id},
        {"laboratoryOutcomeId": result_id},
        {
            "id": result_id,
            "resultId": result_id,
            "entryId": result_id,
            "laboratoryOutcomeId": result_id,
        },
    ]
    response: Any | None = None
    last_error: CliError | None = None
    for candidate in candidates:
        try:
            response = client.request(
                "POST",
                url,
                headers=headers,
                data=candidate,
            )
            break
        except CliError as exc:
            last_error = exc
            status_code = int(exc.details.get("status_code", 0))
            if exc.code == "upstream_error" and status_code == 500:
                continue
            raise
    if response is None:
        details = {
            "result_id": result_id,
            "hint": (
                "run `1177 journal results list` and retry "
                "with a fresh id"
            ),
            "attempted_payloads": [sorted(item.keys()) for item in candidates],
        }
        if last_error is not None:
            details["upstream_status_code"] = last_error.details.get(
                "status_code",
            )
            details["upstream_host"] = last_error.details.get("host")
        raise CliError(
            error="Could not load detail for result id",
            code="detail_unavailable",
            exit_code=exit_codes.UPSTREAM,
            details=details,
        )
    payload = _parse_json_response(
        response_text=response.text,
        endpoint="laboratoryoutcome/detailview",
        request_url=url,
        response_url=str(response.url),
        content_type=response.headers.get("Content-Type", ""),
    )
    detail_html = str(
        payload.get("DetailView")
        or payload.get("PartialView")
        or payload.get("Html")
        or "",
    )
    parsed = _parse_detail_html(detail_html)
    return {
        "detail_core": parsed["core_fields"],
        "detail_extra": parsed["extra_fields"],
        "detail_fields": parsed["fields"],
        "detail_measurements": parsed["measurements"],
        "detail_text": parsed["text"],
        "detail_html": detail_html,
        "detail_payload": payload,
    }


def get_graphable_laboratory_analyses(
    client: HttpClient,
    *,
    verification_token: str | None = None,
) -> dict[str, Any]:
    """Fetch graphable laboratory analyses list."""
    headers = dict(AJAX_HEADERS)
    if verification_token:
        headers["__RequestVerificationToken"] = verification_token
    url = (
        f"{BASE_URL}/journalcategories/"
        "laboratoryoutcome/getallgraphableanalyses"
    )
    response = client.request(
        "POST",
        url,
        headers=headers,
        data={},
    )
    payload = _parse_json_response(
        response_text=response.text,
        endpoint="laboratoryoutcome/getallgraphableanalyses",
        request_url=url,
        response_url=str(response.url),
        content_type=response.headers.get("Content-Type", ""),
    )
    analysis_items = payload.get("Items")
    if not isinstance(analysis_items, list):
        analysis_items = payload.get("Analyses")
    if not isinstance(analysis_items, list):
        analysis_items = payload.get("AnalysisListItems")
    if not isinstance(analysis_items, list):
        analysis_items = []
    normalized: list[dict[str, Any]] = []
    for item in analysis_items:
        if not isinstance(item, dict):
            continue
        analysis_id = (
            item.get("AnalysisId")
            or item.get("Id")
            or item.get("selectedAnalysisId")
            or item.get("NpuCode")
        )
        normalized.append(
            {
                "analysis_id": str(analysis_id or ""),
                "analysis_name": str(
                    item.get("Name") or item.get("AnalysisName") or ""
                ),
                "graphable": bool(item.get("Graphable", True)),
                "count": int(item.get("Count", 0) or 0),
            },
        )
    return {
        "ok": True,
        "analysis_count": len(normalized),
        "analyses": normalized,
        "raw_payload": payload,
    }


def get_laboratory_tool_data(
    client: HttpClient,
    *,
    analysis_ids: list[str],
    date_from: str = "",
    date_to: str = "",
    verification_token: str | None = None,
) -> dict[str, Any]:
    """Fetch trend data for selected analyses."""
    headers = dict(AJAX_HEADERS)
    if verification_token:
        headers["__RequestVerificationToken"] = verification_token
    selected_ids = [item.strip() for item in analysis_ids if item.strip()]
    if not selected_ids:
        raise CliError(
            error="At least one analysis id is required",
            code="invalid_argument",
            exit_code=exit_codes.USAGE,
            details={"field": "analysis_ids"},
        )
    form_data = {
        "selectedAnalysisIds": ",".join(selected_ids),
        "dateFrom": date_from,
        "dateTo": date_to,
    }
    url = f"{BASE_URL}/journalcategories/laboratoryoutcome/gettooldata"
    response = client.request(
        "POST",
        url,
        headers=headers,
        data=form_data,
    )
    payload = _parse_json_response(
        response_text=response.text,
        endpoint="laboratoryoutcome/gettooldata",
        request_url=url,
        response_url=str(response.url),
        content_type=response.headers.get("Content-Type", ""),
    )
    series = payload.get("Series")
    if not isinstance(series, list):
        series = payload.get("Data")
    if not isinstance(series, list):
        series = []
    normalized_series: list[dict[str, Any]] = []
    for item in series:
        if not isinstance(item, dict):
            continue
        normalized_series.append(
            {
                "analysis_id": str(
                    item.get("AnalysisId")
                    or item.get("selectedAnalysisId")
                    or "",
                ),
                "date": (
                    item.get("Date")
                    or item.get("TakenAt")
                    or item.get("x")
                ),
                "value": item.get("Value") or item.get("y"),
                "unit": item.get("Unit"),
                "reference_interval": item.get("ReferenceInterval"),
                "review_status": item.get("VidimationStatus")
                or item.get("ReviewStatus"),
            },
        )
    if not normalized_series:
        fallback_points: list[dict[str, Any]] = []
        expected_total = 0

        probe_poll = poll_laboratory_outcome_until_done(
            client,
            date_from=date_from,
            date_to=date_to,
            verification_token=verification_token,
            timeout_seconds=6.0,
            poll_interval_seconds=0.25,
        )
        probe_payload = probe_poll["payload"]
        probe_partial = str(probe_poll.get("combined_partial_view", ""))
        if not probe_partial:
            probe_partial = str(probe_payload.get("PartialView", ""))
        probe_rows = extract_results_from_partial_view(probe_partial)

        analyses_payload = get_graphable_laboratory_analyses(
            client,
            verification_token=verification_token,
        )
        analyses = analyses_payload.get("analyses", [])
        if isinstance(analyses, list):
            count_by_id: dict[str, int] = {}
            for analysis in analyses:
                if not isinstance(analysis, dict):
                    continue
                analysis_id = str(analysis.get("analysis_id") or "").strip()
                count_value = int(analysis.get("count", 0) or 0)
                if analysis_id:
                    count_by_id[analysis_id] = count_value
            expected_total = sum(
                count_by_id.get(selected_id, 0)
                for selected_id in selected_ids
            )

        requested_from = _coerce_iso_date(date_from)
        requested_to = _coerce_iso_date(date_to)
        candidate_rows = probe_rows
        if requested_from or requested_to:
            filtered_rows: list[dict[str, str | None]] = []
            for row in probe_rows:
                row_date = _coerce_iso_date(row.get("entry_date"))
                if row_date is None:
                    continue
                if requested_from and row_date < requested_from:
                    continue
                if requested_to and row_date > requested_to:
                    continue
                filtered_rows.append(row)
            candidate_rows = filtered_rows

        for row in candidate_rows:
            row_id = str(row.get("result_id") or "").strip()
            if not row_id:
                continue
            entry_date = str(row.get("entry_date") or "").strip()
            detail = fetch_laboratory_outcome_detail(
                client,
                result_id=row_id,
                verification_token=verification_token,
            )
            measurements = detail.get("detail_measurements", [])
            detail_core = detail.get("detail_core", {})
            if not isinstance(measurements, list):
                measurements = []
            if not isinstance(detail_core, dict):
                detail_core = {}
            point_date = str(
                detail_core.get("sample_time")
                or detail_core.get("reported_time")
                or entry_date,
            ).strip()
            for measurement in measurements:
                if not isinstance(measurement, dict):
                    continue
                analysis_code = str(
                    measurement.get("analysis_code") or "",
                ).strip()
                if analysis_code not in selected_ids:
                    continue
                fallback_points.append(
                    {
                        "analysis_id": analysis_code,
                        "date": point_date or None,
                        "value": measurement.get("result_value"),
                        "unit": measurement.get("analysis_unit"),
                        "reference_interval": measurement.get(
                            "reference_interval",
                        ),
                        "review_status": measurement.get("review_status"),
                    },
                )
            if expected_total > 0 and len(fallback_points) >= expected_total:
                break

        if fallback_points:
            normalized_series = fallback_points
    return {
        "ok": True,
        "analysis_ids": selected_ids,
        "date_from": date_from,
        "date_to": date_to,
        "point_count": len(normalized_series),
        "series": normalized_series,
        "raw_payload": payload,
    }


def keep_alive(client: HttpClient) -> dict[str, Any]:
    """Call keep-alive endpoint."""
    url = f"{BASE_URL}/Handlers/Poller.ashx"
    response = client.request("POST", url, headers=AJAX_HEADERS, data={})
    text = response.text.strip()
    if not text:
        return {}
    return _parse_json_response(
        response_text=text,
        endpoint="Handlers/Poller.ashx",
        request_url=url,
        response_url=str(response.url),
        content_type=response.headers.get("Content-Type", ""),
    )

