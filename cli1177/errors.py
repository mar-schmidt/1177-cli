"""Custom CLI error type."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class CliError(Exception):
    """Known CLI failure with stable machine metadata."""

    error: str
    code: str
    exit_code: int
    details: dict = field(default_factory=dict)

