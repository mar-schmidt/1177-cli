"""Opt-in live API contract tests using a persisted auth session."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _live_enabled() -> bool:
    return os.environ.get("CLI1177_LIVE_BANKID", "").strip() == "1"


def _auth_state_path() -> Path | None:
    raw = os.environ.get("CLI1177_AUTH_STATE_PATH", "").strip()
    if not raw:
        return None
    return Path(raw)


@pytest.fixture(scope="module")
def live_env() -> dict[str, str]:
    """Prepare environment for live runs or skip with clear reason."""
    if not _live_enabled():
        pytest.skip("Set CLI1177_LIVE_BANKID=1 to enable live tests.")
    state_path = _auth_state_path()
    if state_path is None:
        pytest.skip("Set CLI1177_AUTH_STATE_PATH to an auth state file.")
    if not state_path.exists():
        pytest.skip(
            "Auth state file is missing. Run `1177 auth login` first.",
        )
    env = dict(os.environ)
    env["CLI1177_AUTH_STATE_PATH"] = str(state_path)
    return env


def _run_cli_json(args: list[str], env: dict[str, str]) -> dict[str, object]:
    """Run the real CLI and parse JSON stdout payload."""
    process = subprocess.run(
        [sys.executable, "-m", "cli1177.main", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if process.returncode != 0:
        pytest.fail(
            "CLI command failed: "
            f"{' '.join(args)}\n"
            f"stderr:\n{process.stderr}",
        )
    stdout = process.stdout.strip()
    if not stdout:
        pytest.fail(f"CLI command returned empty stdout: {' '.join(args)}")
    return json.loads(stdout)


@pytest.fixture(scope="module")
def authenticated_env(live_env: dict[str, str]) -> dict[str, str]:
    """Require a currently valid persisted login before contract checks."""
    payload = _run_cli_json(["auth", "status"], live_env)
    required_keys = {
        "ok",
        "logged_in",
        "session_alive",
        "journal_ready",
        "auth_method",
        "idp_host",
        "primary_state_path",
        "global_state_path",
        "state_path_source",
    }
    assert required_keys.issubset(set(payload.keys()))
    if not payload.get("logged_in"):
        pytest.skip("Run `1177 auth login` first to populate auth state.")
    if not payload.get("journal_ready"):
        pytest.skip(
            "Saved auth state is not journal-ready. "
            "Re-run `1177 auth login`.",
        )
    return live_env


@pytest.mark.live_bankid
def test_live_auth_status_contract(authenticated_env: dict[str, str]) -> None:
    """Auth status should return stable contract keys in live mode."""
    payload = _run_cli_json(["auth", "status"], authenticated_env)
    assert payload["ok"] is True
    assert payload["logged_in"] is True
    assert isinstance(payload["session_alive"], bool)
    assert isinstance(payload["journal_ready"], bool)
    assert "primary_state_path" in payload
    assert "global_state_path" in payload
    assert "state_path_source" in payload


@pytest.mark.live_bankid
def test_live_entries_contract(authenticated_env: dict[str, str]) -> None:
    """Entries list should map to the expected output contract."""
    payload = _run_cli_json(
        ["journal", "entries", "list", "--page", "1", "--page-size", "5"],
        authenticated_env,
    )
    assert payload["ok"] is True
    assert isinstance(payload["entries"], list)
    assert isinstance(payload["entry_count"], int)
    assert isinstance(payload["polling"], dict)
    assert isinstance(payload["poll_status"], dict)
    assert isinstance(payload["bootstrap"], dict)
    assert "keepalive" in payload
    assert "keepalive_error" in payload
    if payload["entries"]:
        first = payload["entries"][0]
        assert isinstance(first, dict)
        assert "summary" in first


@pytest.mark.live_bankid
def test_live_diagnoses_contract(authenticated_env: dict[str, str]) -> None:
    """Diagnoses list should map to expected output contract keys."""
    payload = _run_cli_json(
        ["journal", "diagnoses", "list", "--limit", "5"],
        authenticated_env,
    )
    assert payload["ok"] is True
    assert isinstance(payload["diagnoses"], list)
    assert isinstance(payload["diagnosis_count"], int)
    assert isinstance(payload["diagnoses_returned"], int)
    assert isinstance(payload["diagnoses_available"], int)
    assert isinstance(payload["has_more"], bool)
    assert isinstance(payload["polling"], dict)
    assert isinstance(payload["poll_status"], dict)
    assert isinstance(payload["bootstrap"], dict)


@pytest.mark.live_bankid
def test_live_results_and_graph_contracts(
    authenticated_env: dict[str, str],
) -> None:
    """Results list and graph endpoints should keep stable contracts."""
    results = _run_cli_json(
        ["journal", "results", "list", "--limit", "5"],
        authenticated_env,
    )
    assert results["ok"] is True
    assert isinstance(results["results"], list)
    assert isinstance(results["result_count"], int)
    assert isinstance(results["results_returned"], int)
    assert isinstance(results["results_available"], int)
    assert isinstance(results["has_more"], bool)

    analyses = _run_cli_json(
        ["journal", "results", "graph", "analyses"],
        authenticated_env,
    )
    assert analyses["ok"] is True
    assert isinstance(analyses["analysis_count"], int)
    assert isinstance(analyses["analyses"], list)

    if not analyses["analyses"]:
        pytest.skip("No graphable analyses available for this account.")
    first = analyses["analyses"][0]
    if not isinstance(first, dict):
        pytest.skip("Unexpected analyses payload item type.")
    analysis_id = str(first.get("analysis_id", "")).strip()
    if not analysis_id:
        pytest.skip("First analysis item has no analysis_id.")
    graph_data = _run_cli_json(
        [
            "journal",
            "results",
            "graph",
            "data",
            "--analysis-id",
            analysis_id,
        ],
        authenticated_env,
    )
    assert graph_data["ok"] is True
    assert isinstance(graph_data["analysis_ids"], list)
    assert isinstance(graph_data["point_count"], int)
    assert isinstance(graph_data["series"], list)
