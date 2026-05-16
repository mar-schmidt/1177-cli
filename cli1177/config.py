"""Configuration and path helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AppPaths:
    """Filesystem paths used by this CLI."""

    primary_state_file: Path
    global_state_file: Path
    audit_log: Path

    @property
    def state_file(self) -> Path:
        """Backwards-compatible primary auth state path."""
        return self.primary_state_file


def _default_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "1177-cli"
    return Path.home() / ".local" / "state" / "1177-cli"


def get_app_paths() -> AppPaths:
    """Return normalized app paths."""
    global_state_file = _default_state_dir() / "auth-state.json"
    override = os.environ.get("CLI1177_AUTH_STATE_PATH")
    if override:
        primary_state_file = Path(override).expanduser()
    else:
        primary_state_file = global_state_file
    audit_log = primary_state_file.parent / "audit.log"
    return AppPaths(
        primary_state_file=primary_state_file,
        global_state_file=global_state_file,
        audit_log=audit_log,
    )

