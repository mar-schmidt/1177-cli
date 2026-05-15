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
1177 auth login --method bankid-qr
1177 auth status
1177 auth logout
1177 auth probe-browser-parity --headless
1177 journal entries list --page 1 --page-size 10
```

`probe-browser-parity` requires optional Playwright dependency:

```bash
python3 -m pip install -e ".[playwright]"
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
