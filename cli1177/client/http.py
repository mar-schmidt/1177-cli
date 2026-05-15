"""Thin HTTP wrapper with stable error mapping."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

from cli1177 import exit_codes
from cli1177.errors import CliError

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
}


class HttpClient:
    """HTTP client with cookie persistence and retries."""

    def __init__(
        self,
        cookies: list[dict[str, str]] | dict[str, str] | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 1,
    ) -> None:
        self._max_retries = max_retries
        cookie_jar = httpx.Cookies()
        if isinstance(cookies, dict):
            for name, value in cookies.items():
                cookie_jar.set(str(name), str(value), path="/")
        elif isinstance(cookies, list):
            for item in cookies:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                value = str(item.get("value", ""))
                domain = str(item.get("domain", "")).strip() or None
                path = str(item.get("path", "/")) or "/"
                cookie_jar.set(name, value, domain=domain, path=path)
        self._client = httpx.Client(
            headers=DEFAULT_HEADERS,
            cookies=cookie_jar,
            timeout=timeout_s,
            follow_redirects=True,
        )

    @property
    def cookies(self) -> list[dict[str, str]]:
        """Expose cookie jar as serializable list preserving domain/path."""
        records: list[dict[str, str]] = []
        for cookie in self._client.cookies.jar:
            records.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain or "",
                    "path": cookie.path or "/",
                }
            )
        return records

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        json_body: Any | None = None,
        allow_status: set[int] | None = None,
    ) -> httpx.Response:
        """Perform request with retry/backoff and stable errors."""
        last_exc: Exception | None = None
        max_attempts = self._max_retries + 1
        for attempt in range(max_attempts):
            try:
                response = self._client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    data=data,
                    json=json_body,
                )
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt < max_attempts - 1:
                    time.sleep((attempt + 1) * 0.5)
                    continue
                raise CliError(
                    error="Network request failed",
                    code="network_error",
                    exit_code=exit_codes.NETWORK,
                    details={
                        "url": url,
                        "reason": str(exc),
                    },
                ) from exc
            if allow_status and response.status_code in allow_status:
                return response
            if response.status_code < 400:
                return response
            if response.status_code == 401:
                raise CliError(
                    error="Authentication required",
                    code="auth_required",
                    exit_code=exit_codes.AUTH,
                    details={"url": url},
                )
            if response.status_code == 403:
                raise CliError(
                    error="Access denied by upstream",
                    code="forbidden",
                    exit_code=exit_codes.FORBIDDEN,
                    details={"url": url},
                )
            if response.status_code == 429:
                raise CliError(
                    error="Rate limited by upstream",
                    code="rate_limited",
                    exit_code=exit_codes.RATE_LIMIT,
                    details={
                        "url": url,
                        "retry_after": response.headers.get("Retry-After"),
                    },
                )
            raise CliError(
                error="Upstream returned an error",
                code="upstream_error",
                exit_code=exit_codes.UPSTREAM,
                details={
                    "url": url,
                    "status_code": response.status_code,
                    "host": urlparse(url).netloc,
                },
            )
        raise CliError(
            error="Network request failed",
            code="network_error",
            exit_code=exit_codes.NETWORK,
            details={"url": url, "reason": str(last_exc) if last_exc else "none"},
        )

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Get JSON payload."""
        response = self.request(
            "GET",
            url,
            params=params,
            headers=headers,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CliError(
                error="Invalid JSON from upstream",
                code="invalid_json",
                exit_code=exit_codes.UPSTREAM,
                details={"url": url},
            ) from exc
        if not isinstance(payload, dict):
            raise CliError(
                error="Unexpected JSON shape",
                code="unexpected_payload",
                exit_code=exit_codes.UPSTREAM,
                details={"url": url, "type": type(payload).__name__},
            )
        return payload

    def set_cookies(self, cookies: list[dict[str, str]]) -> None:
        """Replace cookie jar with serialized cookie records."""
        self._client.cookies.clear()
        for item in cookies:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            value = str(item.get("value", ""))
            domain = str(item.get("domain", "")).strip() or None
            path = str(item.get("path", "/")) or "/"
            self._client.cookies.set(
                name,
                value,
                domain=domain,
                path=path,
            )


