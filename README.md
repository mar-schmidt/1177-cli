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
1177 auth status
1177 auth logout
1177 journal entries list --page 1 --page-size 10
1177 journal results list
```

## Output Contract

Success payloads are printed to stdout as JSON.

Errors are printed to stderr as JSON with:

- `error`: human readable message
- `code`: stable machine code
- `details`: machine-readable context

## Development

```bash
python3 -m pip install -e ".[dev]"
pytest
```

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
