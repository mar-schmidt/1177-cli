---
name: 1177-cli
version: 1.0.0
description: >
  Use this skill for the 1177 CLI that reads Journalen data with JSON-first
  outputs. Trigger when the user asks about 1177 auth, session state, journal
  entries, laboratory results, or how to fetch graph data correctly.
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
- fetch journal entries or laboratory result rows
- fetch graph-ready data points for one to three analyses
- script deterministic JSON output for automation

## What this CLI is for

`1177` provides terminal access to Journalen workflows with stable JSON output.
It is built for machine-friendly automation, not text scraping.

Primary command areas:

- `1177 auth ...` for login and session checks
- `1177 journal entries ...` for care documentation entries
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
1177 auth login --qr-output both
```

During login, parse QR frame events written to stderr as JSON lines:

- `event: bankid_qr_frame`
- `image_base64`: preferred for direct UI rendering
- `image_path`: fallback PNG path when base64 rendering is unavailable

Render the QR in the UI from `image_base64` so users can scan it with their
phone.

## Journal commands

```bash
1177 journal entries list --page 1 --page-size 10
1177 journal results list
1177 journal results detail --result-id <id>
```

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
