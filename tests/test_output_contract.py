"""Tests for output and parsing contracts."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import click
from typer.testing import CliRunner

from cli1177 import exit_codes
import cli1177.cli_common as cli_common
import cli1177.client.journal as journal_client
import cli1177.commands.auth as auth_commands
import cli1177.commands.journal as journal_commands
from cli1177.commands.journal import _sort_diagnoses_newest_first
from cli1177.commands.journal import _sort_results_newest_first
from cli1177.client.journal import extract_diagnoses_from_partial_view
from cli1177.client.journal import extract_rows_from_partial_view
from cli1177.client.http import HttpClient
from cli1177.main import app
from cli1177.output import print_error
from cli1177.redact import redact_payload
from cli1177.client.bankid import _is_terminal_failure
from cli1177.client.bankid import _extract_saml_form
from cli1177.client.bankid import _parse_poll_payload
from cli1177.client.journal import _parse_json_response
from cli1177.client.auth import AuthState as PersistedAuthState
from cli1177.client.auth import load_auth_state, save_auth_state
from cli1177.config import get_app_paths
from cli1177.client.journal import (
    _extract_saml_form as _journal_extract_saml_form,
)
from cli1177.client.journal import _extract_next_idp_url
from cli1177.client.journal import _req_to_sign_in_url
from cli1177.client.bankid import _is_retryable_location
from cli1177.client.journal import poll_care_documentation
from cli1177.client.journal import fetch_diagnosis_detail
from cli1177.client.journal import fetch_laboratory_outcome_detail
from cli1177.client.journal import get_graphable_laboratory_analyses
from cli1177.client.journal import get_laboratory_tool_data
from cli1177.client.journal import poll_laboratory_outcome
from cli1177.client.journal import poll_diagnosis
from cli1177.client.journal import extract_results_from_partial_view
from cli1177.client.journal import JournalBootstrap
from cli1177.errors import CliError

try:
    runner = CliRunner(mix_stderr=False)
except TypeError:
    runner = CliRunner()

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _normalized_help_output(result: object) -> str:
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    combined = f"{stdout}\n{stderr}"
    no_ansi = ANSI_ESCAPE_RE.sub("", combined)
    normalized_dash = (
        no_ansi.replace("—", "-")
        .replace("–", "-")
        .replace("‑", "-")
    )
    return " ".join(normalized_dash.split())


class _FakeStream:
    def __init__(self, *, is_tty: bool) -> None:
        self._is_tty = is_tty
        self.writes: list[str] = []

    def isatty(self) -> bool:
        return self._is_tty

    def write(self, value: str) -> int:
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        return


def test_print_error_payload_shape(capsys: object) -> None:
    """Raw print_error should emit stable JSON shape."""
    print_error("Auth failed", "auth_required", {"token": "secret"})
    captured = capsys.readouterr()
    payload = json.loads(captured.err.strip())
    assert payload["error"] == "Auth failed"
    assert payload["code"] == "auth_required"
    assert payload["details"]["token"] == "***"


def test_redaction_scrubs_nested_values() -> None:
    """Sensitive fields should be removed recursively."""
    payload = {
        "ok": True,
        "token": "abc",
        "nested": {
            "cookies": "raw-cookie",
            "name": "hello",
        },
    }
    safe = redact_payload(payload)
    assert safe["token"] == "***"
    assert safe["nested"]["cookies"] == "***"
    assert safe["nested"]["name"] == "hello"


def test_extract_rows_from_partial_view() -> None:
    """Basic row extraction should preserve coarse summary."""
    html = """
    <table>
      <tr><td>2026-05-01</td><td>Anteckning</td><td>Vardenhet A</td></tr>
      <tr><td>2026-05-02</td><td>Anteckning</td><td>Vardenhet B</td></tr>
    </table>
    """
    rows = extract_rows_from_partial_view(html)
    assert len(rows) == 2
    assert "2026-05-01" in rows[0]["summary"]


def test_extract_rows_from_partial_view_list_posts_markup() -> None:
    """List-post markup should produce entry summaries and ids."""
    html = """
    <ul>
      <li class="nc-list-post">
        <button class="nc-list-post-expander"
          data-id="abc123"
          data-date="2026-05-08T08:49:00"
          aria-label="Datum 2026-05-08, anteckningstyp Åtgärd."></button>
      </li>
    </ul>
    """
    rows = extract_rows_from_partial_view(html)
    assert len(rows) == 1
    assert rows[0]["entry_id"] == "abc123"
    assert "2026-05-08" in rows[0]["summary"]


def test_main_help_runs() -> None:
    """CLI should expose command groups."""
    result = runner.invoke(app, ["--help"])
    help_text = _normalized_help_output(result)
    assert result.exit_code == 0
    assert "Access 1177 Journalen data" in help_text
    assert "auth" in help_text
    assert "journal" in help_text
    assert "Set output format for" in help_text


def test_delayed_spinner_skips_non_tty_stream() -> None:
    """Delayed spinner should stay silent on non-interactive streams."""
    stream = _FakeStream(is_tty=False)
    with cli_common._DelayedSpinner(
        stream=stream,
        delay_seconds=0.01,
        interval_seconds=0.01,
    ):
        time.sleep(0.04)
    assert stream.writes == []


def test_delayed_spinner_renders_on_tty_for_slow_calls() -> None:
    """Delayed spinner should render when stream is interactive."""
    stream = _FakeStream(is_tty=True)
    with cli_common._DelayedSpinner(
        stream=stream,
        delay_seconds=0.01,
        interval_seconds=0.01,
    ):
        time.sleep(0.05)
    rendered = "".join(stream.writes)
    assert "Loading..." in rendered


def test_spinner_disabled_for_auth_login_command_path() -> None:
    """Auth login command should skip spinner to keep QR view clean."""
    assert cli_common._is_auth_login_command("1177 auth login")
    assert cli_common._is_auth_login_command("cli auth login")
    assert cli_common._is_auth_login_command("AUTH LOGIN")
    assert not cli_common._is_auth_login_command("1177 auth status")
    assert not cli_common._is_auth_login_command("1177 journal results list")


def test_run_json_propagates_cli_error_exit_code(capsys: object) -> None:
    """run_json should exit with CliError's configured non-zero code."""

    def execute() -> dict[str, object]:
        raise CliError(
            error="Access denied by upstream",
            code="forbidden",
            exit_code=exit_codes.FORBIDDEN,
            details={"url": "https://journalen.1177.se/Dashboard"},
        )

    try:
        cli_common.run_json(execute)
    except click.exceptions.Exit as exc:
        assert exc.exit_code == exit_codes.FORBIDDEN
    else:
        assert False, "expected click Exit from run_json"

    captured = capsys.readouterr()
    payload = json.loads(captured.err.strip())
    assert payload["code"] == "forbidden"


def test_run_json_unexpected_error_exits_nonzero(capsys: object) -> None:
    """run_json should map unexpected failures to a non-zero code."""

    def execute() -> dict[str, object]:
        raise RuntimeError("boom")

    try:
        cli_common.run_json(execute)
    except click.exceptions.Exit as exc:
        assert exc.exit_code == exit_codes.UPSTREAM
    else:
        assert False, "expected click Exit from run_json"

    captured = capsys.readouterr()
    payload = json.loads(captured.err.strip())
    assert payload["code"] == "internal_error"


def test_journal_help_contains_results_group() -> None:
    """Journal command should include results command group."""
    result = runner.invoke(app, ["journal", "--help"])
    help_text = _normalized_help_output(result)
    assert result.exit_code == 0
    assert "entries" in help_text
    assert "results" in help_text
    assert "Fetch Journalen entries" in help_text


def test_journal_help_contains_diagnoses_group() -> None:
    """Journal command should include diagnoses command group."""
    result = runner.invoke(app, ["journal", "--help"])
    help_text = _normalized_help_output(result)
    assert result.exit_code == 0
    assert "diagnoses" in help_text


def test_auth_login_help_describes_user_action() -> None:
    """Auth login help should explain what the command does."""
    result = runner.invoke(app, ["auth", "login", "--help"])
    help_text = _normalized_help_output(result)
    assert result.exit_code == 0
    assert "Log in to 1177" in help_text
    assert "store a reusable local session" in help_text
    assert "method" in help_text
    assert "bankid-qr" in help_text


def test_results_list_help_describes_limit_constraint() -> None:
    """Results list help should explain return behavior and limit rule."""
    result = runner.invoke(app, ["journal", "results", "list", "--help"])
    help_text = _normalized_help_output(result)
    assert result.exit_code == 0
    assert "List laboratory results" in help_text
    assert "sorted newest first" in help_text
    assert "limit" in help_text
    assert "Maximum number of results to return" in help_text
    assert "at least 1)." in help_text


def test_diagnoses_list_help_describes_limit_constraint() -> None:
    """Diagnoses list help should explain return behavior and limit rule."""
    result = runner.invoke(app, ["journal", "diagnoses", "list", "--help"])
    help_text = _normalized_help_output(result)
    assert result.exit_code == 0
    assert "List diagnosis entries" in help_text
    assert "sorted newest first" in help_text
    assert "limit" in help_text
    assert "Maximum number of diagnoses to return" in help_text


def test_results_graph_data_help_describes_analysis_count() -> None:
    """Graph data help should explain analysis id requirements."""
    result = runner.invoke(
        app,
        ["journal", "results", "graph", "data", "--help"],
    )
    help_text = _normalized_help_output(result)
    assert result.exit_code == 0
    assert "Fetch graph data for one to three" in help_text
    assert "analysis-id" in help_text or "analysis id" in help_text
    assert "Repeat option one to" in help_text
    assert "three times, for example" in help_text


def test_auth_login_requests_interactive_journal_step_up(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """Auth login should run journal check with interactive step-up."""
    calls: list[bool] = []

    def fake_establish_journal_session(
        client: object,
        *,
        allow_interactive_step_up: bool = False,
        debug_trace: list[dict[str, object]] | None = None,
    ) -> bool:
        assert client
        assert debug_trace is None
        calls.append(allow_interactive_step_up)
        return True

    monkeypatch.setattr(
        auth_commands,
        "_check_session_alive",
        lambda runtime: False,
    )
    monkeypatch.setattr(
        auth_commands,
        "establish_journal_session",
        fake_establish_journal_session,
    )
    state_file = tmp_path / "auth-state.json"
    xdg_state_home = tmp_path / "xdg-state"
    result = runner.invoke(
        app,
        ["auth", "login"],
        env={
            "CLI1177_AUTH_STATE_PATH": str(state_file),
            "XDG_STATE_HOME": str(xdg_state_home),
        },
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["journal_ready"] is True
    assert calls == [True]


def test_auth_login_persists_step_up_cookies(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """Auth login should persist cookies refreshed during journal step-up."""

    def fake_establish_journal_session(
        client: object,
        *,
        allow_interactive_step_up: bool = False,
        debug_trace: list[dict[str, object]] | None = None,
    ) -> bool:
        assert allow_interactive_step_up is True
        assert debug_trace is None
        client.set_cookies(
            [
                {
                    "name": "journal_session",
                    "value": "cookie-2",
                    "domain": "journalen.1177.se",
                    "path": "/",
                }
            ]
        )
        return True

    monkeypatch.setattr(
        auth_commands,
        "_check_session_alive",
        lambda runtime: False,
    )
    monkeypatch.setattr(
        auth_commands,
        "establish_journal_session",
        fake_establish_journal_session,
    )
    state_file = tmp_path / "auth-state.json"
    xdg_state_home = tmp_path / "xdg-state"
    result = runner.invoke(
        app,
        ["auth", "login"],
        env={
            "CLI1177_AUTH_STATE_PATH": str(state_file),
            "XDG_STATE_HOME": str(xdg_state_home),
        },
    )
    assert result.exit_code == 0
    state_payload = json.loads(state_file.read_text(encoding="utf-8"))
    cookie_names = [item["name"] for item in state_payload["cookies"]]
    assert cookie_names == ["journal_session"]


def test_load_auth_state_falls_back_to_global_path(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """Primary override path should fallback to valid global state."""
    xdg_state_home = tmp_path / "xdg-state"
    primary_file = tmp_path / "override" / "auth-state.json"
    global_file = xdg_state_home / "1177-cli" / "auth-state.json"
    global_file.parent.mkdir(parents=True, exist_ok=True)
    global_payload = {
        "cookies": [
            {
                "name": "session",
                "value": "cookie-1",
                "domain": "journalen.1177.se",
                "path": "/",
            }
        ],
        "idp_host": "idp.example.test",
        "logged_in": True,
        "auth_method": "bankid-qr",
        "last_error": None,
    }
    global_file.write_text(
        json.dumps(global_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLI1177_AUTH_STATE_PATH", str(primary_file))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state_home))
    state = load_auth_state(get_app_paths())
    assert state.logged_in is True
    assert state.cookies[0]["name"] == "session"
    assert state.idp_host == "idp.example.test"


def test_save_auth_state_mirrors_primary_to_global(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """Saving auth state should update both override and global paths."""
    xdg_state_home = tmp_path / "xdg-state"
    primary_file = tmp_path / "override" / "auth-state.json"
    global_file = xdg_state_home / "1177-cli" / "auth-state.json"
    monkeypatch.setenv("CLI1177_AUTH_STATE_PATH", str(primary_file))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state_home))
    paths = get_app_paths()
    state = PersistedAuthState(
        cookies=[
            {
                "name": "session",
                "value": "cookie-2",
                "domain": "journalen.1177.se",
                "path": "/",
            }
        ],
        idp_host="idp.example.test",
        logged_in=True,
        auth_method="bankid-qr",
        last_error=None,
    )
    save_auth_state(paths, state)
    primary_payload = json.loads(primary_file.read_text(encoding="utf-8"))
    global_payload = json.loads(global_file.read_text(encoding="utf-8"))
    assert primary_payload["logged_in"] is True
    assert primary_payload == global_payload


def test_auth_login_qr_output_both_emits_base64_and_qr_flags(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """Both mode should emit QR frame event and set render flags."""
    observed: dict[str, object] = {}

    def fake_establish_journal_session(
        client: object,
        *,
        allow_interactive_step_up: bool = False,
        debug_trace: list[dict[str, object]] | None = None,
        qr_frame_callback: object = None,
        render_terminal_qr: bool = True,
        clear_terminal_qr_screen: bool = True,
    ) -> bool:
        assert client
        assert allow_interactive_step_up is True
        assert debug_trace is None
        observed["render_terminal_qr"] = render_terminal_qr
        observed["clear_terminal_qr_screen"] = clear_terminal_qr_screen
        assert callable(qr_frame_callback)
        qr_frame_callback(b"png-bytes")
        return True

    monkeypatch.setattr(
        auth_commands,
        "_check_session_alive",
        lambda runtime: False,
    )
    monkeypatch.setattr(
        auth_commands,
        "establish_journal_session",
        fake_establish_journal_session,
    )
    state_file = tmp_path / "auth-state.json"
    xdg_state_home = tmp_path / "xdg-state"
    result = runner.invoke(
        app,
        ["auth", "login", "--qr-output", "both"],
        env={
            "CLI1177_AUTH_STATE_PATH": str(state_file),
            "XDG_STATE_HOME": str(xdg_state_home),
        },
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["qr_output"] == "both"
    assert payload["qr_frames_emitted"] == 1
    assert payload["state_path_source"] == "override"
    assert observed["render_terminal_qr"] is True
    assert observed["clear_terminal_qr_screen"] is False
    event_lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert event_lines
    event = json.loads(event_lines[0])
    assert event["event"] == "bankid_qr_frame"
    assert event["frame_index"] == 1
    assert event["image_base64"] == "cG5nLWJ5dGVz"
    assert Path(event["image_path"]).exists()


def test_auth_login_qr_output_base64_disables_terminal_render(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """Base64 mode should disable terminal QR rendering."""
    observed: dict[str, object] = {}

    def fake_establish_journal_session(
        client: object,
        *,
        allow_interactive_step_up: bool = False,
        debug_trace: list[dict[str, object]] | None = None,
        qr_frame_callback: object = None,
        render_terminal_qr: bool = True,
        clear_terminal_qr_screen: bool = True,
    ) -> bool:
        assert client
        assert allow_interactive_step_up is True
        assert debug_trace is None
        observed["render_terminal_qr"] = render_terminal_qr
        observed["clear_terminal_qr_screen"] = clear_terminal_qr_screen
        assert callable(qr_frame_callback)
        qr_frame_callback(b"png-bytes")
        return True

    monkeypatch.setattr(
        auth_commands,
        "_check_session_alive",
        lambda runtime: False,
    )
    monkeypatch.setattr(
        auth_commands,
        "establish_journal_session",
        fake_establish_journal_session,
    )
    state_file = tmp_path / "auth-state.json"
    xdg_state_home = tmp_path / "xdg-state"
    result = runner.invoke(
        app,
        ["auth", "login", "--qr-output", "base64"],
        env={
            "CLI1177_AUTH_STATE_PATH": str(state_file),
            "XDG_STATE_HOME": str(xdg_state_home),
        },
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["qr_output"] == "base64"
    assert observed["render_terminal_qr"] is False
    assert observed["clear_terminal_qr_screen"] is True


def test_auth_login_qr_output_web_emits_url_and_disables_terminal_render(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """Web mode should expose local page URL and keep QR off terminal."""
    observed: dict[str, object] = {}
    server_state = {
        "started": 0,
        "stopped": 0,
        "last_png": b"",
    }

    class _FakeQrWebServer:
        page_url = "http://127.0.0.1:41234/"

        def update_png(self, png_bytes: bytes) -> None:
            server_state["last_png"] = png_bytes

        def stop(self) -> None:
            server_state["stopped"] = int(server_state["stopped"]) + 1

    def fake_start_qr_web_server() -> _FakeQrWebServer:
        server_state["started"] = int(server_state["started"]) + 1
        return _FakeQrWebServer()

    def fake_establish_journal_session(
        client: object,
        *,
        allow_interactive_step_up: bool = False,
        debug_trace: list[dict[str, object]] | None = None,
        qr_frame_callback: object = None,
        render_terminal_qr: bool = True,
        clear_terminal_qr_screen: bool = True,
    ) -> bool:
        assert client
        assert allow_interactive_step_up is True
        assert debug_trace is None
        observed["render_terminal_qr"] = render_terminal_qr
        observed["clear_terminal_qr_screen"] = clear_terminal_qr_screen
        assert callable(qr_frame_callback)
        qr_frame_callback(b"web-png-bytes")
        return True

    monkeypatch.setattr(
        auth_commands,
        "_check_session_alive",
        lambda runtime: False,
    )
    monkeypatch.setattr(
        auth_commands,
        "_start_qr_web_server",
        fake_start_qr_web_server,
    )
    monkeypatch.setattr(
        auth_commands,
        "establish_journal_session",
        fake_establish_journal_session,
    )
    state_file = tmp_path / "auth-state.json"
    xdg_state_home = tmp_path / "xdg-state"
    result = runner.invoke(
        app,
        ["auth", "login", "--qr-output", "web"],
        env={
            "CLI1177_AUTH_STATE_PATH": str(state_file),
            "XDG_STATE_HOME": str(xdg_state_home),
        },
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["qr_output"] == "web"
    assert payload["qr_frames_emitted"] == 1
    assert observed["render_terminal_qr"] is False
    assert observed["clear_terminal_qr_screen"] is True
    assert server_state["started"] == 1
    assert server_state["stopped"] == 1
    assert server_state["last_png"] == b"web-png-bytes"
    event_lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert event_lines
    event = json.loads(event_lines[0])
    assert event["event"] == "bankid_qr_web_url"
    assert event["url"] == "http://127.0.0.1:41234/"


def test_journal_results_list_reuses_session_after_successful_auth_login(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """A successful login should be enough for results list command."""

    def fake_establish_journal_session(
        client: object,
        *,
        allow_interactive_step_up: bool = False,
        debug_trace: list[dict[str, object]] | None = None,
    ) -> bool:
        assert allow_interactive_step_up is True
        assert debug_trace is None
        client.set_cookies(
            [
                {
                    "name": "journal_session",
                    "value": "cookie-2",
                    "domain": "journalen.1177.se",
                    "path": "/",
                }
            ]
        )
        return True

    def fake_bootstrap_laboratory_outcome(
        client: object,
        *,
        debug_trace: list[dict[str, object]] | None = None,
    ) -> JournalBootstrap:
        assert client
        assert debug_trace is None
        return JournalBootstrap(
            verification_token="token-1",
            page_url="https://journalen.1177.se/JournalCategories/LaboratoryOutcome",
        )

    def fake_poll_laboratory_outcome_until_done(
        client: object,
        **kwargs: object,
    ) -> dict[str, object]:
        assert client
        assert kwargs["verification_token"] == "token-1"
        payload = {
            "TotalNumberOfRows": 1,
            "DataIsLoading": False,
            "DataFetchingForAllBatchesIsDone": True,
            "ErrorOccurred": False,
            "DataFetchingTimedOut": False,
            "ShouldFetchMore": False,
            "PartialView": "<ul></ul>",
        }
        return {
            "payload": payload,
            "combined_partial_view": "<ul></ul>",
            "attempts": 1,
            "elapsed_ms": 0,
            "timed_out": False,
        }

    monkeypatch.setattr(
        auth_commands,
        "_check_session_alive",
        lambda runtime: False,
    )
    monkeypatch.setattr(
        auth_commands,
        "establish_journal_session",
        fake_establish_journal_session,
    )
    monkeypatch.setattr(
        journal_commands,
        "bootstrap_laboratory_outcome",
        fake_bootstrap_laboratory_outcome,
    )
    monkeypatch.setattr(
        journal_commands,
        "keep_alive",
        lambda _client: {},
    )
    monkeypatch.setattr(
        journal_commands,
        "poll_laboratory_outcome_until_done",
        fake_poll_laboratory_outcome_until_done,
    )
    monkeypatch.setattr(
        journal_commands,
        "extract_results_from_partial_view",
        lambda _partial: [
            {
                "result_id": "res-1",
                "entry_date": "2026-03-12T10:00:00",
                "summary": "HbA1c",
            }
        ],
    )

    state_file = tmp_path / "auth-state.json"
    xdg_state_home = tmp_path / "xdg-state"
    env = {
        "CLI1177_AUTH_STATE_PATH": str(state_file),
        "XDG_STATE_HOME": str(xdg_state_home),
    }
    login_result = runner.invoke(app, ["auth", "login"], env=env)
    assert login_result.exit_code == 0

    result = runner.invoke(
        app,
        ["journal", "results", "list", "--limit", "1"],
        env=env,
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["result_count"] == 1


def test_journal_diagnoses_list_reuses_session_after_auth_login(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """A successful login should be enough for diagnoses list command."""

    def fake_establish_journal_session(
        client: object,
        *,
        allow_interactive_step_up: bool = False,
        debug_trace: list[dict[str, object]] | None = None,
    ) -> bool:
        assert allow_interactive_step_up is True
        assert debug_trace is None
        client.set_cookies(
            [
                {
                    "name": "journal_session",
                    "value": "cookie-2",
                    "domain": "journalen.1177.se",
                    "path": "/",
                }
            ]
        )
        return True

    def fake_bootstrap_diagnosis(
        client: object,
        *,
        debug_trace: list[dict[str, object]] | None = None,
    ) -> JournalBootstrap:
        assert client
        assert debug_trace is None
        return JournalBootstrap(
            verification_token="token-dx",
            page_url="https://journalen.1177.se/JournalCategories/Diagnosis",
        )

    def fake_poll_diagnosis_until_done(
        client: object,
        **kwargs: object,
    ) -> dict[str, object]:
        assert client
        assert kwargs["verification_token"] == "token-dx"
        payload = {
            "TotalNumberOfRows": 1,
            "DataIsLoading": False,
            "DataFetchingForAllBatchesIsDone": True,
            "ErrorOccurred": False,
            "DataFetchingTimedOut": False,
            "ShouldFetchMore": False,
            "PartialView": "<ul></ul>",
        }
        return {
            "payload": payload,
            "combined_partial_view": "<ul></ul>",
            "attempts": 1,
            "elapsed_ms": 0,
            "timed_out": False,
        }

    monkeypatch.setattr(
        auth_commands,
        "_check_session_alive",
        lambda runtime: False,
    )
    monkeypatch.setattr(
        auth_commands,
        "establish_journal_session",
        fake_establish_journal_session,
    )
    monkeypatch.setattr(
        journal_commands,
        "bootstrap_diagnosis",
        fake_bootstrap_diagnosis,
    )
    monkeypatch.setattr(
        journal_commands,
        "keep_alive",
        lambda _client: {},
    )
    monkeypatch.setattr(
        journal_commands,
        "poll_diagnosis_until_done",
        fake_poll_diagnosis_until_done,
    )
    monkeypatch.setattr(
        journal_commands,
        "extract_diagnoses_from_partial_view",
        lambda _partial: [
            {
                "diagnosis_id": "dx-1",
                "recorded_date": "2026-03-12T10:00:00",
                "diagnosis_code": "E11.9",
                "diagnosis_name": "Diabetes",
            }
        ],
    )

    state_file = tmp_path / "auth-state.json"
    xdg_state_home = tmp_path / "xdg-state"
    env = {
        "CLI1177_AUTH_STATE_PATH": str(state_file),
        "XDG_STATE_HOME": str(xdg_state_home),
    }
    login_result = runner.invoke(app, ["auth", "login"], env=env)
    assert login_result.exit_code == 0

    result = runner.invoke(
        app,
        ["journal", "diagnoses", "list", "--limit", "1"],
        env=env,
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["diagnosis_count"] == 1
    assert payload["diagnoses"][0]["diagnosis_code"] == "E11.9"


def test_journal_entries_list_reuses_session_after_auth_login(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """A successful login should be enough for entries list command."""

    def fake_establish_journal_session(
        client: object,
        *,
        allow_interactive_step_up: bool = False,
        debug_trace: list[dict[str, object]] | None = None,
    ) -> bool:
        assert allow_interactive_step_up is True
        assert debug_trace is None
        client.set_cookies(
            [
                {
                    "name": "journal_session",
                    "value": "cookie-entries",
                    "domain": "journalen.1177.se",
                    "path": "/",
                }
            ]
        )
        return True

    def fake_bootstrap_care_documentation(
        client: object,
        *,
        debug_trace: list[dict[str, object]] | None = None,
    ) -> JournalBootstrap:
        assert client
        assert debug_trace is None
        return JournalBootstrap(
            verification_token="token-entries",
            page_url=(
                "https://journalen.1177.se/"
                "JournalCategories/CareDocumentation"
            ),
        )

    def fake_poll_care_documentation_until_done(
        client: object,
        **kwargs: object,
    ) -> dict[str, object]:
        assert client
        assert kwargs["verification_token"] == "token-entries"
        payload = {
            "TotalNumberOfRows": 2,
            "DataIsLoading": False,
            "DataFetchingForAllBatchesIsDone": True,
            "ErrorOccurred": False,
            "DataFetchingTimedOut": False,
            "ShouldFetchMore": False,
            "PartialView": "<ul></ul>",
        }
        return {
            "payload": payload,
            "combined_partial_view": "<ul></ul>",
            "attempts": 2,
            "elapsed_ms": 35,
            "timed_out": False,
        }

    monkeypatch.setattr(
        auth_commands,
        "_check_session_alive",
        lambda runtime: False,
    )
    monkeypatch.setattr(
        auth_commands,
        "establish_journal_session",
        fake_establish_journal_session,
    )
    monkeypatch.setattr(
        journal_commands,
        "bootstrap_care_documentation",
        fake_bootstrap_care_documentation,
    )
    monkeypatch.setattr(
        journal_commands,
        "keep_alive",
        lambda _client: {"heartbeat": "ok"},
    )
    monkeypatch.setattr(
        journal_commands,
        "poll_care_documentation_until_done",
        fake_poll_care_documentation_until_done,
    )
    monkeypatch.setattr(
        journal_commands,
        "extract_rows_from_partial_view",
        lambda _partial: [
            {
                "entry_id": "entry-1",
                "entry_date": "2026-03-12T10:00:00",
                "summary": "Anteckning 1",
            },
            {
                "entry_id": "entry-2",
                "entry_date": "2026-03-11T09:00:00",
                "summary": "Anteckning 2",
            },
        ],
    )

    state_file = tmp_path / "auth-state.json"
    xdg_state_home = tmp_path / "xdg-state"
    env = {
        "CLI1177_AUTH_STATE_PATH": str(state_file),
        "XDG_STATE_HOME": str(xdg_state_home),
    }
    login_result = runner.invoke(app, ["auth", "login"], env=env)
    assert login_result.exit_code == 0

    result = runner.invoke(
        app,
        [
            "journal",
            "entries",
            "list",
            "--page",
            "2",
            "--page-size",
            "10",
            "--sort-by",
            "Date",
            "--sort-order",
            "desc",
        ],
        env=env,
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["page"] == 2
    assert payload["page_size"] == 10
    assert payload["entry_count"] == 2
    assert payload["total_rows"] == 2
    assert payload["entries"][0]["entry_id"] == "entry-1"
    assert payload["polling"]["attempts"] == 2
    assert payload["poll_status"]["should_fetch_more"] is False
    assert payload["bootstrap"]["token_found"] is True
    assert payload["keepalive"]["heartbeat"] == "ok"
    assert payload["keepalive_error"] is None


def test_journal_diagnoses_detail_uses_structured_payload(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """Diagnoses detail command should return stable structured fields."""

    def fake_establish_journal_session(
        client: object,
        *,
        allow_interactive_step_up: bool = False,
        debug_trace: list[dict[str, object]] | None = None,
    ) -> bool:
        assert allow_interactive_step_up is True
        assert debug_trace is None
        client.set_cookies(
            [
                {
                    "name": "journal_session",
                    "value": "cookie-3",
                    "domain": "journalen.1177.se",
                    "path": "/",
                }
            ]
        )
        return True

    monkeypatch.setattr(
        auth_commands,
        "_check_session_alive",
        lambda runtime: False,
    )
    monkeypatch.setattr(
        auth_commands,
        "establish_journal_session",
        fake_establish_journal_session,
    )
    monkeypatch.setattr(
        journal_commands,
        "bootstrap_diagnosis",
        lambda _client, **_kwargs: JournalBootstrap(
            verification_token="token-dx",
            page_url=(
                "https://journalen.1177.se/JournalCategories/Diagnosis"
            ),
        ),
    )
    monkeypatch.setattr(
        journal_commands,
        "fetch_diagnosis_detail",
        lambda _client, **_kwargs: {
            "detail_core": {
                "diagnosis_code": "E11.9",
                "diagnosis_name": "Diabetes",
                "recorded_date": "2026-03-12",
                "recording_provider": "Dr X",
                "care_unit": "Vardenhet A",
            },
            "detail_extra": {"okand_etikett": "Extra"},
            "detail_fields": {"diagnoskod": "E11.9"},
            "detail_text": "Diagnoskod E11.9",
            "detail_html": "<div>raw</div>",
            "detail_payload": {"DetailView": "<div>raw</div>"},
        },
    )

    state_file = tmp_path / "auth-state.json"
    xdg_state_home = tmp_path / "xdg-state"
    env = {
        "CLI1177_AUTH_STATE_PATH": str(state_file),
        "XDG_STATE_HOME": str(xdg_state_home),
    }
    login_result = runner.invoke(app, ["auth", "login"], env=env)
    assert login_result.exit_code == 0

    result = runner.invoke(
        app,
        ["journal", "diagnoses", "detail", "--diagnosis-id", "dx-1"],
        env=env,
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["diagnosis_id"] == "dx-1"
    assert payload["detail_core"]["diagnosis_code"] == "E11.9"
    assert "detail_html" not in payload
    assert "detail_payload" not in payload


def test_auth_login_failed_step_up_triggers_playwright_fallback(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """Failed journal step-up should trigger optional browser fallback."""
    fallback_called = {"value": False}

    monkeypatch.setattr(
        auth_commands,
        "_check_session_alive",
        lambda runtime: False,
    )
    monkeypatch.setattr(
        auth_commands,
        "establish_journal_session",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        auth_commands,
        "login_with_playwright_fallback",
        lambda: fallback_called.update(value=True),
    )

    state_file = tmp_path / "auth-state.json"
    xdg_state_home = tmp_path / "xdg-state"
    result = runner.invoke(
        app,
        ["auth", "login", "--allow-playwright-fallback"],
        env={
            "CLI1177_AUTH_STATE_PATH": str(state_file),
            "XDG_STATE_HOME": str(xdg_state_home),
        },
    )
    assert result.exit_code == 2
    assert fallback_called["value"] is True


def test_auth_login_blocked_journal_returns_auth_exit_code(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """Blocked journal session must fail auth login with non-zero exit."""
    monkeypatch.setattr(
        auth_commands,
        "_check_session_alive",
        lambda runtime: False,
    )
    monkeypatch.setattr(
        auth_commands,
        "establish_journal_session",
        lambda *_args, **_kwargs: False,
    )

    state_file = tmp_path / "auth-state.json"
    xdg_state_home = tmp_path / "xdg-state"
    result = runner.invoke(
        app,
        ["auth", "login"],
        env={
            "CLI1177_AUTH_STATE_PATH": str(state_file),
            "XDG_STATE_HOME": str(xdg_state_home),
        },
    )

    assert result.exit_code == exit_codes.AUTH
    assert not result.stdout.strip()


def test_establish_session_forbidden_dashboard_returns_false() -> None:
    """Dashboard 403 on Journalen should never be treated as ready."""
    calls: list[tuple[str, str]] = []

    class Response:
        def __init__(self, url: str, status_code: int) -> None:
            self.url = url
            self.status_code = status_code
            self.text = "<html>forbidden</html>"
            self.headers: dict[str, str] = {}
            self.history: list[object] = []

    class FakeClient:
        cookies: list[dict[str, str]] = []

        def request(self, method: str, url: str, **kwargs: object) -> Response:
            calls.append((method, url))
            assert kwargs["allow_status"] == {200, 302, 401, 403}
            if url.endswith("/Dashboard"):
                return Response(url, 403)
            raise AssertionError(f"unexpected URL: {url}")

        def set_cookies(self, cookies: list[dict[str, str]]) -> None:
            self.cookies = cookies

    assert journal_client.establish_journal_session(FakeClient()) is False
    assert calls == [("GET", "https://journalen.1177.se/Dashboard")]


def test_establish_session_forbidden_probe_returns_false() -> None:
    """Probe 403 must not be interpreted as authenticated session."""

    class Response:
        def __init__(self, url: str, status_code: int) -> None:
            self.url = url
            self.status_code = status_code
            self.text = "<html></html>"
            self.headers: dict[str, str] = {}
            self.history: list[object] = []

    class FakeClient:
        cookies: list[dict[str, str]] = []

        def request(self, method: str, url: str, **kwargs: object) -> Response:
            assert kwargs["allow_status"] == {200, 302, 401, 403}
            if url.endswith("/Dashboard"):
                return Response(url, 200)
            if "CareDocumentation" in url:
                return Response(url, 403)
            raise AssertionError(f"unexpected URL: {url}")

    assert journal_client.establish_journal_session(FakeClient()) is False


def test_establish_session_unauthorized_probe_returns_false() -> None:
    """Probe 401 must not be interpreted as authenticated session."""

    class Response:
        def __init__(self, url: str, status_code: int) -> None:
            self.url = url
            self.status_code = status_code
            self.text = "<html></html>"
            self.headers: dict[str, str] = {}
            self.history: list[object] = []

    class FakeClient:
        cookies: list[dict[str, str]] = []

        def request(self, method: str, url: str, **kwargs: object) -> Response:
            assert kwargs["allow_status"] == {200, 302, 401, 403}
            if url.endswith("/Dashboard"):
                return Response(url, 200)
            if "CareDocumentation" in url:
                return Response(url, 401)
            raise AssertionError(f"unexpected URL: {url}")

    assert journal_client.establish_journal_session(FakeClient()) is False


def test_establish_session_successful_dashboard_and_probe() -> None:
    """Ready session should still return True on successful responses."""

    class Response:
        def __init__(self, url: str, status_code: int) -> None:
            self.url = url
            self.status_code = status_code
            self.text = "<html></html>"
            self.headers: dict[str, str] = {}
            self.history: list[object] = []

    class FakeClient:
        cookies: list[dict[str, str]] = []

        def request(self, method: str, url: str, **kwargs: object) -> Response:
            assert kwargs["allow_status"] == {200, 302, 401, 403}
            return Response(url, 200)

    assert journal_client.establish_journal_session(FakeClient()) is True


def test_bankid_rfa9_progress_state_not_terminal() -> None:
    """RFA9 with security-code text should continue polling."""
    status = (
        "Skriv in din säkerhetskod i BankID-appen och välj "
        "Identifiera eller Skriv under."
    )
    assert _is_terminal_failure("code-grp-rfa9", status) is False


def test_bankid_rfa8_is_terminal() -> None:
    """RFA8 should be treated as a terminal failure."""
    assert _is_terminal_failure("code-grp-rfa8", "Timeout") is True


def test_http_client_preserves_duplicate_cookie_names() -> None:
    """Same cookie name across domains should not crash serialization."""
    client = HttpClient(
        cookies=[
            {
                "name": "TS01caf0a5",
                "value": "value-a",
                "domain": "a.example.test",
                "path": "/",
            },
            {
                "name": "TS01caf0a5",
                "value": "value-b",
                "domain": "b.example.test",
                "path": "/",
            },
        ]
    )
    records = client.cookies
    assert len(records) == 2


def test_extract_saml_form_from_html() -> None:
    """SAML auto-submit form should be detected and extracted."""
    html = """
    <html>
      <body>
        <form action="/Shibboleth.sso/SAML2/POST" method="post">
          <input type="hidden" name="SAMLResponse" value="abc123" />
          <input type="hidden" name="RelayState" value="relay" />
        </form>
      </body>
    </html>
    """
    extracted = _extract_saml_form(html, "https://e-tjanster.1177.se/callback")
    assert extracted is not None
    action, fields = extracted
    assert action == "https://e-tjanster.1177.se/Shibboleth.sso/SAML2/POST"
    assert fields["SAMLResponse"] == "abc123"


def test_render_qr_non_image_raises_cli_error() -> None:
    """Non-image payload should become stable CliError."""
    from cli1177.client.bankid import render_png_to_terminal

    try:
        render_png_to_terminal(b"<html>not-an-image</html>")
    except CliError as exc:
        assert exc.code == "bankid_qr_unavailable"
        return
    assert False, "expected CliError for invalid QR payload"


def test_parse_json_response_html_requires_auth() -> None:
    """HTML login response should map to auth_required."""
    try:
        _parse_json_response(
            response_text="<html><title>Logga in</title></html>",
            endpoint="caredocumentation/poll",
            request_url="https://journalen.1177.se/x",
            response_url="https://e-tjanster.1177.se/mvk",
            content_type="text/html",
        )
    except CliError as exc:
        assert exc.code == "auth_required"
        return
    assert False, "expected auth_required for HTML login response"


def test_bootstrap_care_documentation_extracts_token() -> None:
    """Care documentation bootstrap should return token and page URL."""
    requested: dict[str, object] = {}

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            requested["method"] = method
            requested["url"] = url
            requested["kwargs"] = kwargs

            class Response:
                def __init__(self) -> None:
                    self.text = (
                        '<input name="__RequestVerificationToken" '
                        'value="token-caredoc">'
                    )
                    self.url = (
                        "https://journalen.1177.se/"
                        "JournalCategories/CareDocumentation"
                    )
                    self.status_code = 200

            return Response()

    result = journal_client.bootstrap_care_documentation(FakeClient())
    assert requested["method"] == "GET"
    assert str(requested["url"]).endswith(
        "/JournalCategories/CareDocumentation",
    )
    assert result.verification_token == "token-caredoc"
    assert str(result.page_url).endswith("/JournalCategories/CareDocumentation")


def test_bootstrap_laboratory_outcome_extracts_token() -> None:
    """Laboratory bootstrap should return token and page URL."""

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            assert method == "GET"
            assert kwargs == {}

            class Response:
                def __init__(self) -> None:
                    self.text = (
                        '<input name="__RequestVerificationToken" '
                        'value="token-lab">'
                    )
                    self.url = (
                        "https://journalen.1177.se/"
                        "JournalCategories/LaboratoryOutcome"
                    )
                    self.status_code = 200

            assert str(url).endswith("/JournalCategories/LaboratoryOutcome")
            return Response()

    result = journal_client.bootstrap_laboratory_outcome(FakeClient())
    assert result.verification_token == "token-lab"
    assert str(result.page_url).endswith("/JournalCategories/LaboratoryOutcome")


def test_bootstrap_diagnosis_extracts_token() -> None:
    """Diagnosis bootstrap should return token and page URL."""

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            assert method == "GET"
            assert kwargs == {}

            class Response:
                def __init__(self) -> None:
                    self.text = (
                        '<input name="__RequestVerificationToken" '
                        'value="token-dx">'
                    )
                    self.url = (
                        "https://journalen.1177.se/"
                        "JournalCategories/Diagnosis"
                    )
                    self.status_code = 200

            assert str(url).endswith("/JournalCategories/Diagnosis")
            return Response()

    result = journal_client.bootstrap_diagnosis(FakeClient())
    assert result.verification_token == "token-dx"
    assert str(result.page_url).endswith("/JournalCategories/Diagnosis")


def test_bankid_poll_payload_parsing_contract() -> None:
    """BankID poll parser should accept dict payloads only."""
    parsed = _parse_poll_payload(
        '{"rfacode":"code-grp-rfa9","location":"/next"}',
        "https://idp/mg-local/auth/ccp19/grp/pollstatus",
    )
    assert isinstance(parsed, dict)
    assert parsed["rfacode"] == "code-grp-rfa9"
    assert parsed["location"] == "/next"
    assert parsed["__response_url"] == (
        "https://idp/mg-local/auth/ccp19/grp/pollstatus"
    )
    assert _parse_poll_payload("[]", "https://idp/x") is None
    assert _parse_poll_payload("<html></html>", "https://idp/x") is None


def test_journal_extract_saml_form() -> None:
    """Journal helper should detect and parse SAML form."""
    html = """
    <html><body>
      <form action="/sp/consume" method="post">
        <input type="hidden" name="SAMLResponse" value="xyz" />
      </form>
    </body></html>
    """
    result = _journal_extract_saml_form(html, "https://idp.example.test/sso")
    assert result is not None
    action, fields = result
    assert action == "https://idp.example.test/sp/consume"
    assert fields["SAMLResponse"] == "xyz"


def test_extract_next_idp_url_from_anchor() -> None:
    """IdP helper should pick sign-in URL from links."""
    html = """
    <html><body>
      <a href="/samlv2/idp/sign_in/abc123">Continue</a>
    </body></html>
    """
    url = _extract_next_idp_url(html, "https://m00.idp.example.test/req")
    assert url == "https://m00.idp.example.test/samlv2/idp/sign_in/abc123"


def test_req_to_sign_in_url_conversion() -> None:
    """Req endpoint should convert to sign-in endpoint."""
    req_url = "https://m00.idp.example.test/samlv2/idp/req/0/30?x=1"
    sign_in = _req_to_sign_in_url(req_url)
    assert sign_in == "https://m00.idp.example.test/samlv2/idp/sign_in/30"


def test_retryable_poll_location_detection() -> None:
    """IdP sign-in and req locations should be retryable."""
    assert _is_retryable_location("https://idp/samlv2/idp/sign_in/123")
    assert _is_retryable_location("https://idp/samlv2/idp/req/0/30")
    assert _is_retryable_location("https://idp/path") is False


def test_retryable_poll_location_detection_with_query() -> None:
    """Retryable URL detection should work with query strings."""
    url = "https://idp/samlv2/idp/req/0/30?SAMLRequest=abc"
    assert _is_retryable_location(url)


def test_caredoc_poll_waits_until_done(monkeypatch: object) -> None:
    """Care documentation polling should continue until done."""
    responses = [
        {
            "DataIsLoading": True,
            "DataFetchingForAllBatchesIsDone": False,
            "TotalNumberOfRows": 0,
            "PartialView": "",
        },
        {
            "DataIsLoading": False,
            "DataFetchingForAllBatchesIsDone": True,
            "TotalNumberOfRows": 2,
            "PartialView": "<table><tr><td>A</td></tr></table>",
        },
    ]
    call_count = {"value": 0}

    def fake_poll(*args: object, **kwargs: object) -> dict[str, object]:
        call_count["value"] += 1
        return responses.pop(0)

    monkeypatch.setattr(journal_client, "poll_care_documentation", fake_poll)
    monkeypatch.setattr(journal_client.time, "sleep", lambda *_: None)
    result = journal_client.poll_care_documentation_until_done(
        object(),
        page=1,
        page_size=10,
    )
    payload = result["payload"]
    assert call_count["value"] == 2
    assert result["attempts"] == 2
    assert result["timed_out"] is False
    assert payload["DataFetchingForAllBatchesIsDone"] is True
    assert payload["TotalNumberOfRows"] == 2
    assert "A" in str(result["combined_partial_view"])


def test_caredoc_poll_reports_timeout(monkeypatch: object) -> None:
    """Care documentation polling should report timeout."""
    responses = [
        {
            "DataIsLoading": True,
            "DataFetchingForAllBatchesIsDone": False,
            "TotalNumberOfRows": 0,
            "PartialView": "",
        },
    ]

    def fake_poll(*args: object, **kwargs: object) -> dict[str, object]:
        return responses[0]

    class FakeClock:
        def __init__(self) -> None:
            self.current = -0.01

        def __call__(self) -> float:
            self.current += 0.01
            return self.current

    monkeypatch.setattr(journal_client, "poll_care_documentation", fake_poll)
    monkeypatch.setattr(journal_client.time, "sleep", lambda *_: None)
    monkeypatch.setattr(journal_client.time, "monotonic", FakeClock())
    result = journal_client.poll_care_documentation_until_done(
        object(),
        page=1,
        page_size=10,
        timeout_seconds=0.0,
        poll_interval_seconds=0.0,
    )
    assert result["attempts"] == 1
    assert result["timed_out"] is True
    assert result["elapsed_ms"] >= 0


def test_caredoc_poll_preserves_pagination_args(monkeypatch: object) -> None:
    """Polling retries should keep page and page size stable."""
    responses = [
        {
            "DataIsLoading": True,
            "DataFetchingForAllBatchesIsDone": False,
            "PartialView": "",
        },
        {
            "DataIsLoading": False,
            "DataFetchingForAllBatchesIsDone": True,
            "PartialView": "",
        },
    ]
    calls: list[tuple[object, object, object, object, object, object]] = []

    def fake_poll(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append(
            (
                kwargs.get("page"),
                kwargs.get("page_size"),
                kwargs.get("sort_by"),
                kwargs.get("sort_order"),
                kwargs.get("date_from"),
                kwargs.get("date_to"),
            ),
        )
        return responses.pop(0)

    monkeypatch.setattr(journal_client, "poll_care_documentation", fake_poll)
    monkeypatch.setattr(journal_client.time, "sleep", lambda *_: None)
    journal_client.poll_care_documentation_until_done(
        object(),
        page=3,
        page_size=25,
        sort_by="Date",
        sort_order="desc",
        date_from="2026-01-01",
        date_to="2026-02-01",
        poll_interval_seconds=0.0,
    )
    assert calls == [
        (3, 25, "Date", "desc", "2026-01-01", "2026-02-01"),
        (3, 25, "Date", "desc", "2026-01-01", "2026-02-01"),
    ]


def test_caredoc_poll_accumulates_partial_fragments(
    monkeypatch: object,
) -> None:
    """Polling should return merged partial HTML across retries."""
    responses = [
        {
            "DataIsLoading": True,
            "DataFetchingForAllBatchesIsDone": False,
            "PartialView": "<table><tr><td>row-one</td></tr></table>",
        },
        {
            "DataIsLoading": False,
            "DataFetchingForAllBatchesIsDone": True,
            "PartialView": "<table><tr><td>row-two</td></tr></table>",
        },
    ]

    def fake_poll(*args: object, **kwargs: object) -> dict[str, object]:
        return responses.pop(0)

    monkeypatch.setattr(journal_client, "poll_care_documentation", fake_poll)
    monkeypatch.setattr(journal_client.time, "sleep", lambda *_: None)
    result = journal_client.poll_care_documentation_until_done(
        object(),
        page=1,
        page_size=10,
        poll_interval_seconds=0.0,
    )
    merged = str(result["combined_partial_view"])
    assert "row-one" in merged
    assert "row-two" in merged


def test_poll_caredoc_uses_json_filter_state() -> None:
    """Care documentation poll should use JSON body and fs contract."""
    captured: dict[str, object] = {}

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs

            class Response:
                def __init__(self, response_url: str) -> None:
                    self.text = "{}"
                    self.headers = {"Content-Type": "application/json"}
                    self.url = response_url

            return Response(url)

    poll_care_documentation(
        FakeClient(),
        page=2,
        page_size=15,
        sort_by="Date",
        sort_order="desc",
        date_from="2026-01-01",
        date_to="2026-02-01",
        verification_token="token-1",
    )
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert "json_body" in kwargs
    body = kwargs["json_body"]
    assert isinstance(body, dict)
    fs = body["fs"]
    assert fs["Skip"] == 15
    assert fs["Take"] == 15
    assert fs["OrderByEnum"] == "DocumentTime"
    assert fs["OrderDirection"] == "Descending"
    assert fs["From"] == "2026-01-01"
    assert fs["To"] == "2026-02-01"
    headers = kwargs["headers"]
    assert isinstance(headers, dict)
    assert headers["__RequestVerificationToken"] == "token-1"


def test_extract_results_from_partial_view_list_posts_markup() -> None:
    """List-post result markup should expose structured fields."""
    html = """
    <ul>
      <li class="nc-list-post">
        <button class="nc-list-post-expander"
          data-id="res-123"
          data-date="2026-03-12T10:00:00"
          aria-label="2026-03-12 HbA1c Slutsvar Vardenhet A"></button>
      </li>
    </ul>
    """
    rows = extract_results_from_partial_view(html)
    assert len(rows) == 1
    assert rows[0]["result_id"] == "res-123"
    assert rows[0]["entry_date"] == "2026-03-12T10:00:00"
    assert "HbA1c" in str(rows[0]["summary"])


def test_extract_diagnoses_from_partial_view_list_posts_markup() -> None:
    """List-post diagnosis markup should expose structured fields."""
    html = """
    <ul>
      <li class="nc-list-post">
        <button class="nc-list-post-expander"
          data-id="dx-123"
          data-date="2026-03-11T09:00:00"
          aria-label="E11.9 Diabetes typ 2"></button>
      </li>
    </ul>
    """
    rows = extract_diagnoses_from_partial_view(html)
    assert len(rows) == 1
    assert rows[0]["diagnosis_id"] == "dx-123"
    assert rows[0]["recorded_date"] == "2026-03-11T09:00:00"
    assert rows[0]["diagnosis_code"] == "E11.9"
    assert "Diabetes" in str(rows[0]["summary"])


def test_extract_diagnoses_from_partial_view_maps_summary_fields() -> None:
    """Diagnosis summary text should map key structured fields."""
    html = """
    <ul>
      <li class="nc-list-post">
        <button class="nc-list-post-expander"
          data-id="dx-999"
          data-date="2026-04-23"
          aria-label="Datum 2026-04-23, diagnos Hårcellsleukemi, antecknad av
          Benson, Peter, (Läk), på Hematologimottagning Sahlgrenska,
          Göteborg. Klicka för att visa detaljer."></button>
      </li>
    </ul>
    """
    rows = extract_diagnoses_from_partial_view(html)
    assert len(rows) == 1
    row = rows[0]
    assert row["diagnosis_name"] == "Hårcellsleukemi"
    assert row["recording_provider"] == "Benson, Peter, (Läk)"
    assert row["care_unit"] == (
        "Hematologimottagning Sahlgrenska, Göteborg"
    )


def test_sort_results_newest_first_keeps_undated_rows_last() -> None:
    """Newest entry_date should appear first and invalid dates last."""
    rows = [
        {"entry_date": "2026-03-10T10:00:00", "summary": "older"},
        {"entry_date": None, "summary": "missing-date"},
        {"entry_date": "2026-03-12T08:00:00Z", "summary": "latest"},
        {"entry_date": "not-a-date", "summary": "invalid"},
        {"entry_date": "2026-03-11T10:00:00", "summary": "middle"},
    ]
    sorted_rows = _sort_results_newest_first(rows)
    summaries = [str(item["summary"]) for item in sorted_rows]
    assert summaries == [
        "latest",
        "middle",
        "older",
        "missing-date",
        "invalid",
    ]


def test_sort_results_newest_first_limit_returns_latest_entries() -> None:
    """Limit should keep the latest rows after sorting."""
    rows = [
        {"entry_date": "2026-03-10T10:00:00", "summary": "r1"},
        {"entry_date": "2026-03-12T10:00:00", "summary": "r3"},
        {"entry_date": "2026-03-11T10:00:00", "summary": "r2"},
    ]
    limited = _sort_results_newest_first(rows)[:2]
    summaries = [str(item["summary"]) for item in limited]
    assert summaries == ["r3", "r2"]


def test_sort_diagnoses_newest_first_keeps_undated_rows_last() -> None:
    """Newest diagnosis date should appear first, invalid dates last."""
    rows = [
        {"recorded_date": "2026-03-10T10:00:00", "summary": "older"},
        {"recorded_date": None, "summary": "missing-date"},
        {"recorded_date": "2026-03-12T08:00:00Z", "summary": "latest"},
        {"recorded_date": "not-a-date", "summary": "invalid"},
        {"recorded_date": "2026-03-11T10:00:00", "summary": "middle"},
    ]
    sorted_rows = _sort_diagnoses_newest_first(rows)
    summaries = [str(item["summary"]) for item in sorted_rows]
    assert summaries == [
        "latest",
        "middle",
        "older",
        "missing-date",
        "invalid",
    ]


def test_poll_diagnosis_uses_json_filter_state() -> None:
    """Diagnosis poll should use JSON fs contract and token header."""
    captured: dict[str, object] = {}

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs

            class Response:
                def __init__(self, response_url: str) -> None:
                    self.text = "{}"
                    self.headers = {"Content-Type": "application/json"}
                    self.url = response_url

            return Response(url)

    poll_diagnosis(FakeClient(), verification_token="token-diagnosis")
    assert captured["method"] == "POST"
    assert str(captured["url"]).endswith("/journalcategories/diagnosis/poll")
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    body = kwargs["json_body"]
    assert isinstance(body, dict)
    fs = body["fs"]
    assert fs["Skip"] == 0
    assert fs["Take"] == 20
    assert fs["OrderDirection"] == "Descending"
    assert fs["OrderByEnum"] == "DiagnosisDate"
    assert fs["GetFiltersView"] is True
    headers = kwargs["headers"]
    assert isinstance(headers, dict)
    assert headers["__RequestVerificationToken"] == "token-diagnosis"


def test_diagnosis_poll_waits_until_done(monkeypatch: object) -> None:
    """Diagnosis polling should continue until done."""
    responses = [
        {
            "DataIsLoading": True,
            "DataFetchingForAllBatchesIsDone": False,
            "PartialView": "<ul><li>first</li></ul>",
        },
        {
            "DataIsLoading": False,
            "DataFetchingForAllBatchesIsDone": True,
            "TotalNumberOfRows": 1,
            "PartialView": "<ul><li>second</li></ul>",
        },
    ]

    def fake_poll(*args: object, **kwargs: object) -> dict[str, object]:
        return responses.pop(0)

    monkeypatch.setattr(journal_client, "poll_diagnosis", fake_poll)
    monkeypatch.setattr(journal_client.time, "sleep", lambda *_: None)
    result = journal_client.poll_diagnosis_until_done(
        object(),
        poll_interval_seconds=0.0,
    )
    assert result["attempts"] == 2
    assert result["timed_out"] is False
    payload = result["payload"]
    assert payload["DataFetchingForAllBatchesIsDone"] is True
    assert "first" in str(result["combined_partial_view"])
    assert "second" in str(result["combined_partial_view"])


def test_poll_laboratory_outcome_uses_form_filters() -> None:
    """Laboratory outcome poll should use form filter contract."""
    captured: dict[str, object] = {}

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs

            class Response:
                def __init__(self, response_url: str) -> None:
                    self.text = "{}"
                    self.headers = {"Content-Type": "application/json"}
                    self.url = response_url

            return Response(url)

    poll_laboratory_outcome(
        FakeClient(),
        date_from="2026-01-01",
        date_to="2026-02-01",
        answer_type_filter="Slutsvar",
        ordered_by_filter="Dr X",
        care_unit_filter="Lab A",
        verification_token="token-2",
    )
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert "data" in kwargs
    body = kwargs["data"]
    assert isinstance(body, dict)
    assert body["DateFrom"] == "2026-01-01"
    assert body["DateTo"] == "2026-02-01"
    assert body["AnswerTypeFilter"] == "Slutsvar"
    assert body["OrderedByFilter"] == "Dr X"
    assert body["CareUnitFilter"] == "Lab A"
    headers = kwargs["headers"]
    assert isinstance(headers, dict)
    assert headers["__RequestVerificationToken"] == "token-2"


def test_results_poll_waits_until_done(monkeypatch: object) -> None:
    """Laboratory outcome polling should continue until done."""
    responses = [
        {
            "DataIsLoading": True,
            "DataFetchingForAllBatchesIsDone": False,
            "PartialView": "<ul><li>first</li></ul>",
        },
        {
            "DataIsLoading": False,
            "DataFetchingForAllBatchesIsDone": True,
            "TotalNumberOfRows": 1,
            "PartialView": "<ul><li>second</li></ul>",
        },
    ]

    def fake_poll(*args: object, **kwargs: object) -> dict[str, object]:
        return responses.pop(0)

    monkeypatch.setattr(journal_client, "poll_laboratory_outcome", fake_poll)
    monkeypatch.setattr(journal_client.time, "sleep", lambda *_: None)
    result = journal_client.poll_laboratory_outcome_until_done(
        object(),
        poll_interval_seconds=0.0,
    )
    assert result["attempts"] == 2
    assert result["timed_out"] is False
    payload = result["payload"]
    assert payload["DataFetchingForAllBatchesIsDone"] is True
    assert "first" in str(result["combined_partial_view"])
    assert "second" in str(result["combined_partial_view"])


def test_fetch_diagnosis_detail_uses_post_contract() -> None:
    """Diagnosis detail endpoint should post compatible id keys."""
    captured: dict[str, object] = {}

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs

            class Response:
                def __init__(self, response_url: str) -> None:
                    self.text = (
                        '{"DetailView":"<dl><dt>Diagnoskod</dt>'
                        "<dd>E11.9</dd>"
                        "<dt>Diagnos</dt>"
                        "<dd>Diabetes</dd>"
                        "<dt>Vårdenhet</dt>"
                        "<dd>Vardenhet A</dd>"
                        "<dt>Okand etikett</dt>"
                        '<dd>Extra</dd></dl>"}'
                    )
                    self.headers = {"Content-Type": "application/json"}
                    self.url = response_url

            return Response(url)

    payload = fetch_diagnosis_detail(
        FakeClient(),
        diagnosis_id="dx-1",
        verification_token="token-dx",
    )
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    body = kwargs["data"]
    assert isinstance(body, dict)
    assert body["id"] == "dx-1"
    headers = kwargs["headers"]
    assert isinstance(headers, dict)
    assert headers["__RequestVerificationToken"] == "token-dx"
    core = payload["detail_core"]
    assert isinstance(core, dict)
    assert core["diagnosis_code"] == "E11.9"
    assert core["diagnosis_name"] == "Diabetes"
    assert core["care_unit"] == "Vardenhet A"
    extra = payload["detail_extra"]
    assert isinstance(extra, dict)
    assert extra["okand_etikett"] == "Extra"


def test_fetch_diagnosis_detail_retries_payload_shapes() -> None:
    """Diagnosis detail should retry alternate keys on HTTP 500."""
    calls: list[dict[str, object]] = []

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            calls.append(dict(kwargs))
            if len(calls) == 1:
                raise CliError(
                    error="Upstream returned an error",
                    code="upstream_error",
                    exit_code=1,
                    details={"status_code": 500, "host": "journalen.1177.se"},
                )

            class Response:
                def __init__(self, response_url: str) -> None:
                    self.text = '{"DetailView":"<dl></dl>"}'
                    self.headers = {"Content-Type": "application/json"}
                    self.url = response_url

            return Response(url)

    fetch_diagnosis_detail(
        FakeClient(),
        diagnosis_id="dx-2",
        verification_token=None,
    )
    assert len(calls) == 2
    first_data = calls[0]["data"]
    second_data = calls[1]["data"]
    assert isinstance(first_data, dict)
    assert isinstance(second_data, dict)
    assert sorted(first_data.keys()) == ["id"]
    assert sorted(second_data.keys()) == ["diagnosisId"]


def test_fetch_diagnosis_detail_extracts_text_fallback_fields() -> None:
    """Diagnosis detail should parse structured fields from plain text."""

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            class Response:
                def __init__(self, response_url: str) -> None:
                    self.text = (
                        '{"PartialView":"'
                        "<div>Osignerad diagnos</div>"
                        "<div>Diagnos: Lateral traumatisk meniskruptur i "
                        "knaled</div>"
                        "<div>2025-01-14 15:15</div>"
                        "<div>Antecknad av</div>"
                        "<div>Gudmundsson, Stefan (Lak)</div>"
                        "<div>Operation 3 Molndal, Goteborg</div>"
                        "<div>Huvuddiagnos</div>"
                        '"}'
                    )
                    self.headers = {"Content-Type": "application/json"}
                    self.url = response_url

            return Response(url)

    payload = fetch_diagnosis_detail(
        FakeClient(),
        diagnosis_id="dx-3",
        verification_token=None,
    )
    core = payload["detail_core"]
    assert isinstance(core, dict)
    assert core["diagnosis_name"] == "Lateral traumatisk meniskruptur i knaled"
    assert core["recorded_date"] == "2025-01-14 15:15"
    assert core["recording_provider"] == "Gudmundsson, Stefan (Lak)"
    assert core["care_unit"] == "Operation 3 Molndal, Goteborg"
    assert core["diagnosis_type"] == "Huvuddiagnos"
    assert core["review_status"] == "Osignerad diagnos"


def test_fetch_laboratory_outcome_detail_uses_post_contract() -> None:
    """Detail endpoint should post multiple compatible id keys."""
    captured: dict[str, object] = {}

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs

            class Response:
                def __init__(self, response_url: str) -> None:
                    self.text = (
                        '{"DetailView":"<dl><dt>Analys</dt>'
                        "<dd>HbA1c</dd>"
                        "<dt>Vårdenhet</dt>"
                        "<dd>Lab A</dd>"
                        "<dt>Okand etikett</dt>"
                        '<dd>Extra</dd></dl>"}'
                    )
                    self.headers = {"Content-Type": "application/json"}
                    self.url = response_url

            return Response(url)

    payload = fetch_laboratory_outcome_detail(
        FakeClient(),
        result_id="row-1",
        verification_token="token-3",
    )
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    body = kwargs["data"]
    assert isinstance(body, dict)
    assert body["id"] == "row-1"
    headers = kwargs["headers"]
    assert isinstance(headers, dict)
    assert headers["__RequestVerificationToken"] == "token-3"
    fields = payload["detail_fields"]
    assert isinstance(fields, dict)
    assert fields["analys"] == "HbA1c"
    assert fields["vardenhet"] == "Lab A"
    core = payload["detail_core"]
    assert isinstance(core, dict)
    assert core["analysis_name"] == "HbA1c"
    assert core["care_unit"] == "Lab A"
    extra = payload["detail_extra"]
    assert isinstance(extra, dict)
    assert extra["okand_etikett"] == "Extra"


def test_fetch_laboratory_outcome_detail_extracts_measurement_rows() -> None:
    """Detail parser should pair analysis and result columns per row."""

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            class Response:
                def __init__(self, response_url: str) -> None:
                    self.text = (
                        '{"PartialView":"'
                        '<table class=\\"nc-laboratory-table\\">'
                        "<tbody><tr><td>"
                        '<a data-analysis-name=\\"B-Hemoglobin (Hb)\\" '
                        'data-npu-code=\\"NPU28309\\" '
                        'data-analysis-unit=\\"g/L\\">B-Hemoglobin (Hb)</a>'
                        "<div>Referensintervall: 134 - 170</div>"
                        "</td><td>"
                        '<span class=\\"iu-fw-bold\\">96 g/L</span>'
                        '<span class=\\"iu-sr-only\\">'
                        "Värdet ligger utanför referensintervall"
                        "</span>"
                        '<span class=\\"nu-fs-italic\\">Svar ej vidimerat'
                        "</span>"
                        '<a title=\\"Visa som graf\\" href=\\"/tool\\">Graf</a>'
                        "</td></tr></tbody></table>\"}"
                    )
                    self.headers = {"Content-Type": "application/json"}
                    self.url = response_url

            return Response(url)

    payload = fetch_laboratory_outcome_detail(
        FakeClient(),
        result_id="row-2",
        verification_token=None,
    )
    measurements = payload["detail_measurements"]
    assert isinstance(measurements, list)
    assert len(measurements) == 1
    row = measurements[0]
    assert row["analysis_name"] == "B-Hemoglobin (Hb)"
    assert row["analysis_code"] == "NPU28309"
    assert row["analysis_unit"] == "g/L"
    assert row["reference_interval"] == "134 - 170"
    assert row["result_value"] == "96 g/L"
    assert row["result_comment"] == "Värdet ligger utanför referensintervall"
    assert row["review_status"] == "Svar ej vidimerat"
    assert row["graph_url"] == "/tool"


def test_fetch_laboratory_outcome_detail_retries_payload_shapes() -> None:
    """Detail fetch should retry alternate form payload keys on 500."""
    calls: list[dict[str, object]] = []

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            calls.append(dict(kwargs))
            if len(calls) == 1:
                raise CliError(
                    error="Upstream returned an error",
                    code="upstream_error",
                    exit_code=1,
                    details={"status_code": 500, "host": "journalen.1177.se"},
                )

            class Response:
                def __init__(self, response_url: str) -> None:
                    self.text = '{"DetailView":"<dl></dl>"}'
                    self.headers = {"Content-Type": "application/json"}
                    self.url = response_url

            return Response(url)

    fetch_laboratory_outcome_detail(
        FakeClient(),
        result_id="row-3",
        verification_token=None,
    )
    assert len(calls) == 2
    first_data = calls[0]["data"]
    second_data = calls[1]["data"]
    assert isinstance(first_data, dict)
    assert isinstance(second_data, dict)
    assert sorted(first_data.keys()) == ["id"]
    assert sorted(second_data.keys()) == ["resultId"]


def test_get_graphable_laboratory_analyses_contract() -> None:
    """Graphable analyses endpoint should normalize analysis rows."""
    captured: dict[str, object] = {}

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs

            class Response:
                def __init__(self, response_url: str) -> None:
                    self.text = (
                        '{"Items":[{"AnalysisId":"NPU02902",'
                        '"Name":"Neutrofiler","Graphable":true}]}'
                    )
                    self.headers = {"Content-Type": "application/json"}
                    self.url = response_url

            return Response(url)

    payload = get_graphable_laboratory_analyses(
        FakeClient(),
        verification_token="token-4",
    )
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["data"] == {}
    headers = kwargs["headers"]
    assert isinstance(headers, dict)
    assert headers["__RequestVerificationToken"] == "token-4"
    assert payload["analysis_count"] == 1
    analyses = payload["analyses"]
    assert isinstance(analyses, list)
    assert analyses[0]["analysis_id"] == "NPU02902"
    assert analyses[0]["analysis_name"] == "Neutrofiler"


def test_get_laboratory_tool_data_contract() -> None:
    """Graph data endpoint should post selected ids and date filters."""
    captured: dict[str, object] = {}

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs

            class Response:
                def __init__(self, response_url: str) -> None:
                    self.text = (
                        '{"Data":[{"AnalysisId":"NPU02902",'
                        '"Date":"2026-02-02","Value":"1.2"}]}'
                    )
                    self.headers = {"Content-Type": "application/json"}
                    self.url = response_url

            return Response(url)

    payload = get_laboratory_tool_data(
        FakeClient(),
        analysis_ids=["NPU02902", "NPU04111"],
        date_from="2026-01-01",
        date_to="2026-03-01",
        verification_token="token-5",
    )
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    body = kwargs["data"]
    assert isinstance(body, dict)
    assert body["selectedAnalysisIds"] == "NPU02902,NPU04111"
    assert body["dateFrom"] == "2026-01-01"
    assert body["dateTo"] == "2026-03-01"
    headers = kwargs["headers"]
    assert isinstance(headers, dict)
    assert headers["__RequestVerificationToken"] == "token-5"
    assert payload["point_count"] == 1
    series = payload["series"]
    assert isinstance(series, list)
    assert series[0]["analysis_id"] == "NPU02902"


def test_keep_alive_contract() -> None:
    """Keep alive should post Poller endpoint and parse JSON payload."""
    captured: dict[str, object] = {}

    class FakeClient:
        def request(self, method: str, url: str, **kwargs: object) -> object:
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs

            class Response:
                def __init__(self) -> None:
                    self.text = (
                        '{"Ok":true,'
                        '"ServerTime":"2026-05-16T13:00:00Z"}'
                    )
                    self.headers = {"Content-Type": "application/json"}
                    self.url = url

            return Response()

    payload = journal_client.keep_alive(FakeClient())
    assert captured["method"] == "POST"
    assert str(captured["url"]).endswith("/Handlers/Poller.ashx")
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    headers = kwargs["headers"]
    assert isinstance(headers, dict)
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    body = kwargs["data"]
    assert isinstance(body, dict)
    assert payload["Ok"] is True
    assert payload["ServerTime"] == "2026-05-16T13:00:00Z"

