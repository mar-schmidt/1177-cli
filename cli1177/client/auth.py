"""Persisted auth state helpers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from cli1177.config import AppPaths

CookieRecord = dict[str, str]


@dataclass(slots=True)
class AuthState:
    """Authentication state persisted between invocations."""

    cookies: list[CookieRecord] = field(default_factory=list)
    idp_host: str | None = None
    logged_in: bool = False
    auth_method: str | None = None
    last_error: str | None = None


def _normalize_cookie_records(raw_cookies: object) -> list[CookieRecord]:
    """Normalize persisted cookies from older and current formats."""
    if isinstance(raw_cookies, list):
        records: list[CookieRecord] = []
        for item in raw_cookies:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            value = str(item.get("value", ""))
            domain = str(item.get("domain", ""))
            path = str(item.get("path", "/")) or "/"
            if not name:
                continue
            records.append(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                }
            )
        return records
    if isinstance(raw_cookies, dict):
        records = []
        for name, value in raw_cookies.items():
            key = str(name).strip()
            if not key:
                continue
            records.append(
                {
                    "name": key,
                    "value": str(value),
                    "domain": "",
                    "path": "/",
                }
            )
        return records
    return []


def load_auth_state(paths: AppPaths) -> AuthState:
    """Load state from disk. Return defaults when missing."""
    if not paths.state_file.exists():
        return AuthState()
    raw = json.loads(paths.state_file.read_text(encoding="utf-8"))
    return AuthState(
        cookies=_normalize_cookie_records(raw.get("cookies", [])),
        idp_host=raw.get("idp_host"),
        logged_in=bool(raw.get("logged_in")),
        auth_method=raw.get("auth_method"),
        last_error=raw.get("last_error"),
    )


def save_auth_state(paths: AppPaths, state: AuthState) -> None:
    """Persist state with strict local permissions."""
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookies": state.cookies,
        "idp_host": state.idp_host,
        "logged_in": state.logged_in,
        "auth_method": state.auth_method,
        "last_error": state.last_error,
    }
    paths.state_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(paths.state_file, 0o600)


def clear_auth_state(paths: AppPaths) -> None:
    """Delete persisted state file."""
    if paths.state_file.exists():
        paths.state_file.unlink()

