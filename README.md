# 1177 CLI

LLM-friendly CLI for 1177/journalen data access.

## Goals

- JSON-first output for deterministic automation
- Stable error schema and exit codes
- BankID QR login flow for terminal use
- Session-safe handling for sensitive healthcare data

## Install

```bash
python3 -m pip install -e .
```

## Commands

```bash
1177 auth login
1177 auth login --qr-output web
1177 auth status
1177 auth logout
1177 journal entries list --page 1 --page-size 10
1177 journal diagnoses list
1177 journal diagnoses detail --diagnosis-id dx-123
1177 journal results list
```

## Output Contract

Success payloads are printed to stdout as JSON.

Errors are printed to stderr as JSON with:

- `error`: human readable message
- `code`: stable machine code
- `details`: machine-readable context

For auth QR flows, stderr can also include machine-readable events:

- `bankid_qr_frame`: base64 QR frame payloads (`--qr-output base64|both`)
- `bankid_qr_web_url`: localhost page URL (`--qr-output web`)

## Development

```bash
python3 -m pip install -e ".[dev]"
pytest
```

Run only the contract test module:

```bash
pytest tests/test_output_contract.py
```

Run only live tests by marker (default behavior is skip unless enabled):

```bash
pytest -m live_bankid tests/test_live_api_contract.py
```

Run live contract tests against real endpoints (manual pre-login reuse):

```bash
1177 auth login
export CLI1177_AUTH_STATE_PATH="$HOME/.local/state/1177-cli/auth-state.json"
export CLI1177_LIVE_BANKID=1
pytest -m live_bankid tests/test_live_api_contract.py
```

Live tests are opt-in and skipped unless both environment variables are set.
The auth state file must already exist and contain a journal-ready session.

## Agent Skill

This repo includes a packaged Agent Skill in `.agent/`.

Local build:

```bash
python scripts/generate-skill-manifest.py
python scripts/package-skill.py
```

The output archive is written to:

```bash
dist/1177-cli.skill
```

GitHub CI also builds this `.skill` file for each `push` and `pull_request`
and uploads it as the `1177-cli-skill` workflow artifact.
