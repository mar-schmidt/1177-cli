"""Configuration and path helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AppPaths:
    """Filesystem paths used by this CLI."""

    state_file: Path
    audit_log: Path


def _default_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "1177-cli"
    return Path.home() / ".local" / "state" / "1177-cli"


def get_app_paths() -> AppPaths:
    """Return normalized app paths."""
    override = os.environ.get("CLI1177_AUTH_STATE_PATH")
    if override:
        state_file = Path(override).expanduser()
    else:
        state_file = _default_state_dir() / "auth-state.json"
    audit_log = state_file.parent / "audit.log"
    return AppPaths(state_file=state_file, audit_log=audit_log)

