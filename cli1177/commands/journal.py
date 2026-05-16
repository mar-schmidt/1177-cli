"""Journal data commands."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

import typer

from cli1177 import exit_codes
from cli1177.cli_common import run_json
from cli1177.client.auth import save_auth_state
from cli1177.client.journal import (
    bootstrap_diagnosis,
    bootstrap_care_documentation,
    bootstrap_laboratory_outcome,
    extract_diagnoses_from_partial_view,
    extract_rows_from_partial_view,
    extract_results_from_partial_view,
    fetch_diagnosis_detail,
    fetch_laboratory_outcome_detail,
    get_graphable_laboratory_analyses,
    get_laboratory_tool_data,
    keep_alive,
    poll_care_documentation_until_done,
    poll_diagnosis_until_done,
    poll_laboratory_outcome_until_done,
)
from cli1177.errors import CliError
from cli1177.runtime import Runtime

app = typer.Typer(help="Fetch Journalen entries and laboratory results.")
entries_app = typer.Typer(
    help="List care documentation entries from your journal."
)
diagnoses_app = typer.Typer(
    help="List diagnosis entries from your journal."
)
results_app = typer.Typer(
    help="List and inspect laboratory results from your journal."
)
results_graph_app = typer.Typer(
    help="Query graph-ready laboratory analysis data."
)
app.add_typer(entries_app, name="entries")
app.add_typer(diagnoses_app, name="diagnoses")
app.add_typer(results_app, name="results")
results_app.add_typer(results_graph_app, name="graph")


def _runtime(ctx: typer.Context) -> Runtime:
    runtime = ctx.obj
    if not isinstance(runtime, Runtime):
        raise RuntimeError("runtime not initialized")
    return runtime


def _parse_result_entry_date(value: object) -> float | None:
    """Parse result entry date into a UTC timestamp."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.timestamp()


def _sort_results_newest_first(
    rows: list[dict[str, str | None]],
) -> list[dict[str, str | None]]:
    """Sort result rows so latest entry_date is first."""
    indexed_rows = list(enumerate(rows))

    def sort_key(
        item: tuple[int, dict[str, str | None]],
    ) -> tuple[int, float, int]:
        index, row = item
        timestamp = _parse_result_entry_date(row.get("entry_date"))
        if timestamp is None:
            return (1, 0.0, index)
        return (0, -timestamp, index)

    return [row for _, row in sorted(indexed_rows, key=sort_key)]


def _sort_diagnoses_newest_first(
    rows: list[dict[str, str | None]],
) -> list[dict[str, str | None]]:
    """Sort diagnosis rows so latest recorded_date is first."""
    indexed_rows = list(enumerate(rows))

    def sort_key(
        item: tuple[int, dict[str, str | None]],
    ) -> tuple[int, float, int]:
        index, row = item
        timestamp = _parse_result_entry_date(row.get("recorded_date"))
        if timestamp is None:
            return (1, 0.0, index)
        return (0, -timestamp, index)

    return [row for _, row in sorted(indexed_rows, key=sort_key)]


@entries_app.command("list")
def list_entries(
    ctx: typer.Context,
    page: int = typer.Option(
        1,
        "--page",
        help="Result page number to request from the entries list.",
    ),
    page_size: int = typer.Option(
        10,
        "--page-size",
        help="Number of entries to request per page.",
    ),
    sort_by: str = typer.Option(
        "Date",
        "--sort-by",
        help='Entry field used for sorting, for example "Date".',
    ),
    sort_order: str = typer.Option(
        "desc",
        "--sort-order",
        help='Sort direction: "asc" or "desc".',
    ),
    date_from: str = typer.Option(
        "",
        "--date-from",
        help="Include entries on or after this date.",
    ),
    date_to: str = typer.Option(
        "",
        "--date-to",
        help="Include entries on or before this date.",
    ),
) -> None:
    """List care documentation entries from your journal.

    Use filters and paging options to narrow the returned entry set.
    """

    def execute() -> dict[str, object]:
        runtime = _runtime(ctx)
        if not runtime.state.logged_in or not runtime.state.cookies:
            raise CliError(
                error="Authentication required",
                code="auth_required",
                exit_code=exit_codes.AUTH,
                details={"hint": "run 1177 auth login"},
            )
        auth_trace: list[dict[str, object]] | None = None
        if runtime.debug_auth:
            auth_trace = []
        bootstrap = bootstrap_care_documentation(
            runtime.client,
            debug_trace=auth_trace,
        )
        keepalive: dict[str, object] = {}
        keepalive_error: dict[str, object] | None = None
        try:
            keepalive = keep_alive(runtime.client)
        except CliError as exc:
            keepalive_error = {
                "error": exc.error,
                "code": exc.code,
            }
        poll_result = poll_care_documentation_until_done(
            runtime.client,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            date_from=date_from,
            date_to=date_to,
            verification_token=bootstrap.verification_token,
        )
        poll_payload = poll_result["payload"]
        partial = str(poll_result.get("combined_partial_view", ""))
        if not partial:
            partial = str(poll_payload.get("PartialView", ""))
        rows = extract_rows_from_partial_view(partial)
        runtime.state.cookies = runtime.client.cookies
        runtime.state.logged_in = True
        runtime.state.last_error = None
        save_auth_state(runtime.paths, runtime.state)
        payload = {
            "ok": True,
            "page": page,
            "page_size": page_size,
            "total_rows": poll_payload.get("TotalNumberOfRows"),
            "data_loading": poll_payload.get("DataIsLoading"),
            "done": poll_payload.get("DataFetchingForAllBatchesIsDone"),
            "entry_count": len(rows),
            "entries": rows,
            "polling": {
                "attempts": poll_result["attempts"],
                "elapsed_ms": poll_result["elapsed_ms"],
                "timed_out": poll_result["timed_out"],
            },
            "poll_status": {
                "error_occurred": poll_payload.get("ErrorOccurred"),
                "fetch_timed_out": poll_payload.get("DataFetchingTimedOut"),
                "should_fetch_more": poll_payload.get("ShouldFetchMore"),
            },
            "bootstrap": {
                "page_url": bootstrap.page_url,
                "token_found": bool(bootstrap.verification_token),
            },
            "keepalive": keepalive,
            "keepalive_error": keepalive_error,
        }
        if auth_trace is not None:
            payload["auth_trace"] = auth_trace
        return payload

    run_json(execute)


@diagnoses_app.command("list")
def list_diagnoses(
    ctx: typer.Context,
    limit: int = typer.Option(
        20,
        "--limit",
        help="Maximum number of diagnoses to return (must be at least 1).",
    ),
) -> None:
    """List diagnosis entries from your journal.

    Diagnoses are sorted newest first and truncated to the chosen limit.
    """

    def execute() -> dict[str, object]:
        runtime = _runtime(ctx)
        if not runtime.state.logged_in or not runtime.state.cookies:
            raise CliError(
                error="Authentication required",
                code="auth_required",
                exit_code=exit_codes.AUTH,
                details={"hint": "run 1177 auth login"},
            )
        if limit < 1:
            raise CliError(
                error="Invalid limit value",
                code="invalid_argument",
                exit_code=exit_codes.USAGE,
                details={"field": "limit", "min": 1},
            )
        auth_trace: list[dict[str, object]] | None = None
        if runtime.debug_auth:
            auth_trace = []
        bootstrap = bootstrap_diagnosis(
            runtime.client,
            debug_trace=auth_trace,
        )
        keepalive: dict[str, object] = {}
        keepalive_error: dict[str, object] | None = None
        try:
            keepalive = keep_alive(runtime.client)
        except CliError as exc:
            keepalive_error = {"error": exc.error, "code": exc.code}
        poll_result = poll_diagnosis_until_done(
            runtime.client,
            verification_token=bootstrap.verification_token,
        )
        poll_payload = poll_result["payload"]
        partial = str(poll_result.get("combined_partial_view", ""))
        if not partial:
            partial = str(poll_payload.get("PartialView", ""))
        rows = extract_diagnoses_from_partial_view(partial)
        rows = _sort_diagnoses_newest_first(rows)
        limited_rows = rows[:limit]
        total_available = len(rows)
        runtime.state.cookies = runtime.client.cookies
        runtime.state.logged_in = True
        runtime.state.last_error = None
        save_auth_state(runtime.paths, runtime.state)
        payload: dict[str, object] = {
            "ok": True,
            "total_rows": poll_payload.get("TotalNumberOfRows"),
            "data_loading": poll_payload.get("DataIsLoading"),
            "done": poll_payload.get("DataFetchingForAllBatchesIsDone"),
            "diagnosis_count": len(limited_rows),
            "diagnoses_returned": len(limited_rows),
            "diagnoses_available": total_available,
            "limit": limit,
            "has_more": total_available > limit,
            "diagnoses": limited_rows,
            "polling": {
                "attempts": poll_result["attempts"],
                "elapsed_ms": poll_result["elapsed_ms"],
                "timed_out": poll_result["timed_out"],
            },
            "poll_status": {
                "error_occurred": poll_payload.get("ErrorOccurred"),
                "fetch_timed_out": poll_payload.get("DataFetchingTimedOut"),
                "should_fetch_more": poll_payload.get("ShouldFetchMore"),
            },
            "bootstrap": {
                "page_url": bootstrap.page_url,
                "token_found": bool(bootstrap.verification_token),
            },
            "keepalive": keepalive,
            "keepalive_error": keepalive_error,
        }
        if auth_trace is not None:
            payload["auth_trace"] = auth_trace
        return payload

    run_json(execute)


@diagnoses_app.command("detail")
def diagnosis_detail(
    ctx: typer.Context,
    diagnosis_id: str = typer.Option(
        ...,
        "--diagnosis-id",
        help="Required diagnosis identifier from `journal diagnoses list`.",
    ),
    include_raw: bool = typer.Option(
        False,
        "--include-raw/--no-include-raw",
        help="Include raw HTML and payload fields in the response.",
    ),
) -> None:
    """Fetch detailed data for one diagnosis entry.

    Use `--diagnosis-id` from a previously listed diagnosis row.
    """

    def execute() -> dict[str, object]:
        runtime = _runtime(ctx)
        if not runtime.state.logged_in or not runtime.state.cookies:
            raise CliError(
                error="Authentication required",
                code="auth_required",
                exit_code=exit_codes.AUTH,
                details={"hint": "run 1177 auth login"},
            )
        auth_trace: list[dict[str, object]] | None = None
        if runtime.debug_auth:
            auth_trace = []
        bootstrap = bootstrap_diagnosis(
            runtime.client,
            debug_trace=auth_trace,
        )
        detail = fetch_diagnosis_detail(
            runtime.client,
            diagnosis_id=diagnosis_id,
            verification_token=bootstrap.verification_token,
        )
        if not include_raw:
            detail.pop("detail_html", None)
            detail.pop("detail_payload", None)
        runtime.state.cookies = runtime.client.cookies
        runtime.state.logged_in = True
        runtime.state.last_error = None
        save_auth_state(runtime.paths, runtime.state)
        payload: dict[str, object] = {
            "ok": True,
            "diagnosis_id": diagnosis_id,
            "include_raw": include_raw,
        }
        payload.update(detail)
        if auth_trace is not None:
            payload["auth_trace"] = auth_trace
        return payload

    run_json(execute)


@results_app.command("list")
def list_results(
    ctx: typer.Context,
    date_from: str = typer.Option(
        "",
        "--date-from",
        help="Include results on or after this date.",
    ),
    date_to: str = typer.Option(
        "",
        "--date-to",
        help="Include results on or before this date.",
    ),
    answer_type: str = typer.Option(
        "",
        "--answer-type",
        help="Filter by result answer type shown in Journalen.",
    ),
    ordered_by: str = typer.Option(
        "",
        "--ordered-by",
        help="Filter by ordering provider shown in Journalen.",
    ),
    care_unit: str = typer.Option(
        "",
        "--care-unit",
        help="Filter by care unit shown in Journalen.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        help="Maximum number of results to return (must be at least 1).",
    ),
) -> None:
    """List laboratory results from your journal.

    Results are sorted newest first and truncated to the chosen limit.
    """

    def execute() -> dict[str, object]:
        runtime = _runtime(ctx)
        if not runtime.state.logged_in or not runtime.state.cookies:
            raise CliError(
                error="Authentication required",
                code="auth_required",
                exit_code=exit_codes.AUTH,
                details={"hint": "run 1177 auth login"},
            )
        if limit < 1:
            raise CliError(
                error="Invalid limit value",
                code="invalid_argument",
                exit_code=exit_codes.USAGE,
                details={"field": "limit", "min": 1},
            )
        auth_trace: list[dict[str, object]] | None = None
        if runtime.debug_auth:
            auth_trace = []
        bootstrap = bootstrap_laboratory_outcome(
            runtime.client,
            debug_trace=auth_trace,
        )
        keepalive: dict[str, object] = {}
        keepalive_error: dict[str, object] | None = None
        try:
            keepalive = keep_alive(runtime.client)
        except CliError as exc:
            keepalive_error = {"error": exc.error, "code": exc.code}
        poll_result = poll_laboratory_outcome_until_done(
            runtime.client,
            date_from=date_from,
            date_to=date_to,
            answer_type_filter=answer_type,
            ordered_by_filter=ordered_by,
            care_unit_filter=care_unit,
            verification_token=bootstrap.verification_token,
        )
        poll_payload = poll_result["payload"]
        partial = str(poll_result.get("combined_partial_view", ""))
        if not partial:
            partial = str(poll_payload.get("PartialView", ""))
        rows = extract_results_from_partial_view(partial)
        rows = _sort_results_newest_first(rows)
        limited_rows = rows[:limit]
        total_available = len(rows)
        runtime.state.cookies = runtime.client.cookies
        runtime.state.logged_in = True
        runtime.state.last_error = None
        save_auth_state(runtime.paths, runtime.state)
        payload: dict[str, object] = {
            "ok": True,
            "date_from": date_from,
            "date_to": date_to,
            "answer_type_filter": answer_type,
            "ordered_by_filter": ordered_by,
            "care_unit_filter": care_unit,
            "total_rows": poll_payload.get("TotalNumberOfRows"),
            "data_loading": poll_payload.get("DataIsLoading"),
            "done": poll_payload.get("DataFetchingForAllBatchesIsDone"),
            "result_count": len(limited_rows),
            "results_returned": len(limited_rows),
            "results_available": total_available,
            "limit": limit,
            "has_more": total_available > limit,
            "results": limited_rows,
            "polling": {
                "attempts": poll_result["attempts"],
                "elapsed_ms": poll_result["elapsed_ms"],
                "timed_out": poll_result["timed_out"],
            },
            "poll_status": {
                "error_occurred": poll_payload.get("ErrorOccurred"),
                "fetch_timed_out": poll_payload.get("DataFetchingTimedOut"),
                "should_fetch_more": poll_payload.get("ShouldFetchMore"),
            },
            "bootstrap": {
                "page_url": bootstrap.page_url,
                "token_found": bool(bootstrap.verification_token),
            },
            "keepalive": keepalive,
            "keepalive_error": keepalive_error,
        }
        if auth_trace is not None:
            payload["auth_trace"] = auth_trace
        return payload

    run_json(execute)


@results_app.command("detail")
def result_detail(
    ctx: typer.Context,
    result_id: str = typer.Option(
        ...,
        "--result-id",
        help="Required result identifier from `journal results list`.",
    ),
    include_raw: bool = typer.Option(
        False,
        "--include-raw/--no-include-raw",
        help="Include raw HTML and payload fields in the response.",
    ),
) -> None:
    """Fetch detailed data for one laboratory result.

    Use `--result-id` from a previously listed result row.
    """

    def execute() -> dict[str, object]:
        runtime = _runtime(ctx)
        if not runtime.state.logged_in or not runtime.state.cookies:
            raise CliError(
                error="Authentication required",
                code="auth_required",
                exit_code=exit_codes.AUTH,
                details={"hint": "run 1177 auth login"},
            )
        auth_trace: list[dict[str, object]] | None = None
        if runtime.debug_auth:
            auth_trace = []
        bootstrap = bootstrap_laboratory_outcome(
            runtime.client,
            debug_trace=auth_trace,
        )
        detail = fetch_laboratory_outcome_detail(
            runtime.client,
            result_id=result_id,
            verification_token=bootstrap.verification_token,
        )
        if not include_raw:
            detail.pop("detail_html", None)
            detail.pop("detail_payload", None)
        runtime.state.cookies = runtime.client.cookies
        runtime.state.logged_in = True
        runtime.state.last_error = None
        save_auth_state(runtime.paths, runtime.state)
        payload: dict[str, object] = {
            "ok": True,
            "result_id": result_id,
            "include_raw": include_raw,
        }
        payload.update(detail)
        if auth_trace is not None:
            payload["auth_trace"] = auth_trace
        return payload

    run_json(execute)


@results_graph_app.command("analyses")
def result_graph_analyses(ctx: typer.Context) -> None:
    """List analysis identifiers that can be used for result graphs."""

    def execute() -> dict[str, object]:
        runtime = _runtime(ctx)
        if not runtime.state.logged_in or not runtime.state.cookies:
            raise CliError(
                error="Authentication required",
                code="auth_required",
                exit_code=exit_codes.AUTH,
                details={"hint": "run 1177 auth login"},
            )
        auth_trace: list[dict[str, object]] | None = None
        if runtime.debug_auth:
            auth_trace = []
        bootstrap = bootstrap_laboratory_outcome(
            runtime.client,
            debug_trace=auth_trace,
        )
        payload = get_graphable_laboratory_analyses(
            runtime.client,
            verification_token=bootstrap.verification_token,
        )
        runtime.state.cookies = runtime.client.cookies
        runtime.state.logged_in = True
        runtime.state.last_error = None
        save_auth_state(runtime.paths, runtime.state)
        if auth_trace is not None:
            payload["auth_trace"] = auth_trace
        return payload

    run_json(execute)


@results_graph_app.command("data")
def result_graph_data(
    ctx: typer.Context,
    analysis_ids: list[str] = typer.Option(
        [],
        "--analysis-id",
        help=(
            "Analysis id to include. Repeat option one to three times, "
            "for example --analysis-id A --analysis-id B."
        ),
    ),
    date_from: str = typer.Option(
        "",
        "--date-from",
        help="Include graph points on or after this date.",
    ),
    date_to: str = typer.Option(
        "",
        "--date-to",
        help="Include graph points on or before this date.",
    ),
) -> None:
    """Fetch graph data for one to three laboratory analyses.

    Pass one to three `--analysis-id` values to choose series to return.
    """

    def execute() -> dict[str, object]:
        runtime = _runtime(ctx)
        if not runtime.state.logged_in or not runtime.state.cookies:
            raise CliError(
                error="Authentication required",
                code="auth_required",
                exit_code=exit_codes.AUTH,
                details={"hint": "run 1177 auth login"},
            )
        if not analysis_ids:
            raise CliError(
                error="At least one analysis id is required",
                code="invalid_argument",
                exit_code=exit_codes.USAGE,
                details={"field": "analysis_ids"},
            )
        if len(analysis_ids) > 3:
            raise CliError(
                error="At most three analysis ids are supported",
                code="invalid_argument",
                exit_code=exit_codes.USAGE,
                details={"field": "analysis_ids", "max": 3},
            )
        auth_trace: list[dict[str, object]] | None = None
        if runtime.debug_auth:
            auth_trace = []
        bootstrap = bootstrap_laboratory_outcome(
            runtime.client,
            debug_trace=auth_trace,
        )
        payload = get_laboratory_tool_data(
            runtime.client,
            analysis_ids=analysis_ids,
            date_from=date_from,
            date_to=date_to,
            verification_token=bootstrap.verification_token,
        )
        runtime.state.cookies = runtime.client.cookies
        runtime.state.logged_in = True
        runtime.state.last_error = None
        save_auth_state(runtime.paths, runtime.state)
        if auth_trace is not None:
            payload["auth_trace"] = auth_trace
        return payload

    run_json(execute)

