---
name: 1177-cli
version: 1.0.0
description: >
  Use this skill for the 1177 CLI that reads Journalen data with JSON-first
  outputs. Trigger when the user asks about 1177 auth, session state, journal
  entries, diagnoses, laboratory results, or graph data workflows.
author: marcus
repo: https://github.com/mar-schmidt/1177-cli
install:
  pip: pip install "git+https://github.com/mar-schmidt/1177-cli.git"
  pipx: pipx install "git+https://github.com/mar-schmidt/1177-cli.git"
requires:
  - python: ">=3.11"
compatibility:
  - claude-desktop
  - cursor
  - continue
  - generic-mcp
tags:
  - healthcare
  - journalen
  - cli
  - graph-data
---

# Skill: 1177-cli

## When to use

Use this skill when a user wants to:

- log in to 1177 and verify usable session state
- fetch journal entries, diagnosis rows, or laboratory result rows
- fetch graph-ready data points for one to three analyses
- script deterministic JSON output for automation

## What this CLI is for

`1177` provides terminal access to Journalen workflows with stable JSON output.
It is built for machine-friendly automation, not text scraping.

Primary command areas:

- `1177 auth ...` for login and session checks
- `1177 journal entries ...` for care documentation entries
- `1177 journal diagnoses ...` for diagnosis lists and detailed diagnosis data
- `1177 journal results ...` for laboratory result lists and details
- `1177 journal results graph ...` for graphable analyses and graph points

## Installation

```bash
pip install "git+https://github.com/mar-schmidt/1177-cli.git"
1177 --help
```

Optional:

```bash
pipx install "git+https://github.com/mar-schmidt/1177-cli.git"
1177 --help
```

## Runtime model

- Default output format is JSON (`--format json`).
- Text mode exists for humans (`--format text`).
- Interactive prompts are disabled by default (`--no-input`).
- Command state includes saved auth cookies for reuse across invocations.

## Output and error contract

Success payloads are printed to `stdout` as JSON.

Error payloads are printed to `stderr` as JSON with stable fields:

- `error` (human readable)
- `code` (machine code)
- `details` (structured context)

Exit codes:

- `0` success
- `1` usage or validation
- `2` auth
- `3` upstream API
- `4` network

## Core auth commands

```bash
1177 auth login
1177 auth status
1177 auth logout
```

Use `1177 auth status` before data calls in headless workflows to verify
`logged_in` and `journal_ready`.

### Agent login flow

When an auth-required error occurs:

1. Run `1177 auth status` first and inspect:
   - `logged_in`
   - `journal_ready`
   - `state_path_source`
   - `primary_state_path`
2. If re-auth is needed, run:

```bash
1177 auth login --qr-output web
```

Preferred behavior for LLM clients:

- use `--qr-output web`
- open the emitted localhost URL in an embedded browser/webview for the user
- keep that embedded page visible until login succeeds or fails

During `--qr-output web` login, parse stderr JSON lines for:

- `event: bankid_qr_web_url`
- `url`: local page with the live QR image

Base64 fallback when embedding is unavailable:

```bash
1177 auth login --qr-output both
```

For `--qr-output base64|both`, parse QR frame events from stderr:

- `event: bankid_qr_frame`
- `image_base64`: preferred for direct UI rendering
- `image_path`: fallback PNG path when base64 rendering is unavailable

Render the QR from `image_base64` only when embedded web display is not
available.

### Required domain whitelist

For agents to fully execute auth and data flows, allow outbound access to:

- `e-tjanster.1177.se` (Shibboleth login and MVK session/logout)
- `journalen.1177.se` (journal and laboratory endpoints)
- `*.idp.funktionstjanster.se` (dynamic BankID IdP host discovered at runtime)

### Sandbox fallback behavior

1177 may block traffic from hosted/sandbox agent environments. If login or
data requests fail in a way that indicates environment blocking, the agent
should switch execution to the user's local terminal session.

Use platform-specific terminal launch methods if* needed:

- macOS: `osascript` (run commands in Terminal/iTerm session)
- Windows: PowerShell + terminal launcher (for example `wt` or `cmd`)
- Linux: system terminal launcher (for example `x-terminal-emulator`,
  `gnome-terminal`, or `konsole`)

* If you are running in a sandbox environment

#### macOS `osascript` notes for graph data

`do shell script` throws when the command exits non-zero. Do not place pipes
or direct redirection in the `do shell script` command string. Write a wrapper
script to disk and execute it with `bash`.

Use command substitution inside the wrapper to capture output:

```bash
OUTPUT=$(1177 journal results graph data --analysis-id X ... 2>/tmp/err.txt)
echo "$OUTPUT" > /tmp/result.json
```

Then run the wrapper via:

```applescript
do shell script "bash /path/to/wrapper.sh"
```

Read result files via filesystem read tools, not via another `do shell script`
plus `cat`.

Set `PATH` and `HOME` explicitly in the wrapper script. `osascript` does not
inherit the user's interactive shell environment, so `1177` may not resolve
unless you use an absolute path or export `PATH` first.

After switching to the local terminal, continue with the normal auth flow:
`1177 auth status` then `1177 auth login --qr-output web` when required.

## Journal commands

```bash
1177 journal entries list --page 1 --page-size 10
1177 journal diagnoses list
1177 journal diagnoses detail --diagnosis-id <id>
1177 journal results list
1177 journal results detail --result-id <id>
```

### Diagnoses output structure

`1177 journal diagnoses list` returns structured rows under `diagnoses` with
keys such as:

- `diagnosis_id`
- `recorded_date`
- `diagnosis_code` (when available)
- `diagnosis_name`
- `recording_provider`
- `care_unit`

`1177 journal diagnoses detail` returns structured fields in `detail_core`
when available, including:

- `diagnosis_code`
- `diagnosis_name`
- `recorded_date`
- `recording_provider`
- `care_unit`
- `diagnosis_type`
- `review_status`

## Graph data: correct workflow

Always fetch graph data in two steps:

1. Discover valid analysis ids.
2. Request data points with one to three selected ids.

Step 1:

```bash
1177 journal results graph analyses
```

Step 2:

```bash
1177 journal results graph data \
  --analysis-id NPU02902 \
  --analysis-id NPU04111 \
  --date-from 2026-01-01 \
  --date-to 2026-03-01
```

Rules:

- Pass at least one `--analysis-id`.
- Pass at most three `--analysis-id`.
- Repeat the option for each id. Do not comma-pack into one argument.
- Use `analyses` output as the source of valid ids.
- `1177 journal results graph data` may return slowly from upstream 1177 APIs.
  Wait for completion before treating the call as failed.

Expected graph payload fields:

- `analysis_ids`
- `date_from`
- `date_to`
- `point_count`
- `series`

## Graph troubleshooting

- `auth_required`: run `1177 auth login` and retry.
- `invalid_argument` with `analysis_ids`: provide 1-3 repeated ids.
- `point_count` is `0`: widen date range and retry with known ids from
  `graph analyses`.

## Working guidance for agents

- Prefer JSON mode and parse fields, not prose.
- Keep command flows explicit and deterministic.
- Do not invent analysis ids; query them first.
- Keep date filters in ISO format (`YYYY-MM-DD`).

## What not to do

- Do not call `graph data` without first checking `graph analyses`.
- Do not pass more than three ids.
- Do not parse terminal text when JSON fields exist.
- Do not assume login state persists across machines.

## Additional references

- Graph workflow examples: `examples/graph-workflow.md`
