"""Runtime object shared across commands."""

from __future__ import annotations

from dataclasses import dataclass

from cli1177.client.auth import AuthState
from cli1177.client.http import HttpClient
from cli1177.config import AppPaths


@dataclass(slots=True)
class Runtime:
    """Dependencies for one command execution."""

    paths: AppPaths
    state: AuthState
    client: HttpClient
    output_format: str
    no_input: bool
    max_retries: int
    debug_auth: bool

