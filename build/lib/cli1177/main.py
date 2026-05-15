"""CLI entrypoint."""

from __future__ import annotations

import typer

from cli1177.client.auth import load_auth_state
from cli1177.client.http import HttpClient
from cli1177.commands.auth import app as auth_app
from cli1177.commands.journal import app as journal_app
from cli1177.config import get_app_paths
from cli1177.runtime import Runtime

app = typer.Typer(
    help=(
        "Access 1177 Journalen data and authentication commands from the CLI."
    )
)
app.pretty_exceptions_enable = False
app.add_typer(auth_app, name="auth")
app.add_typer(journal_app, name="journal")


@app.callback()
def main(
    ctx: typer.Context,
    output_format: str = typer.Option(
        "json",
        "--format",
        help="Set output format for command results: json or text.",
    ),
    no_input: bool = typer.Option(
        True,
        "--no-input/--allow-input",
        help="Disable or allow interactive prompts during command execution.",
    ),
    max_retries: int = typer.Option(
        1,
        "--max-retries",
        help="Retry failed HTTP requests this many extra times.",
    ),
    debug_auth: bool = typer.Option(
        False,
        "--debug-auth",
        help="Include authentication trace details in command output.",
    ),
) -> None:
    """Set up runtime settings shared by every command invocation."""
    if output_format not in {"json", "text"}:
        raise typer.BadParameter("format must be one of: json, text")
    paths = get_app_paths()
    state = load_auth_state(paths)
    client = HttpClient(cookies=state.cookies, max_retries=max_retries)
    ctx.obj = Runtime(
        paths=paths,
        state=state,
        client=client,
        output_format=output_format,
        no_input=no_input,
        max_retries=max_retries,
        debug_auth=debug_auth,
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()

