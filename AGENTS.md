# AGENTS.md

Operating notes for coding agents working in `paperless-tax-export`.

This is a personal, private tool. PropertyMe / monorepo conventions do not apply here.

## What this is

A read-only Python helper that reads documents from Paperless-ngx and writes tax-ready
CSV output for one financial year (FY25-26: 2025-07-01 to 2026-06-30). It reads Paperless
custom fields only; there is no OCR text parsing in the current scope.

Outputs, written into `output/`:

- `review.csv`: documents that need a human decision, with reason codes. This is the
  priority output.
- `deductions.csv`: documents that pass every check.
- `ibkr-documents.csv`: index of IBKR statements.
- `manifest.json`: parameters, versions, counts and file hashes.

## Current state

Bootstrap only. These exist: `.gitignore`, `.gitattributes`, `.editorconfig`,
`.python-version`, `.vscode/`, `README.md`, `AGENTS.md`.

Not yet created: `pyproject.toml`, `uv.lock`, `src/paperless_tax_export/`, `tests/`,
`Dockerfile`, `.dockerignore`, `compose.yaml`. Do not document or assume commands that
depend on them as if they already work.

`.github/` is owned by another change. Do not rewrite the workflows, dependabot or
labeler configuration unless the task is explicitly about them.

## Rules that must not be broken

- **Read-only toward Paperless.** GET requests only. No write client, no PATCH, no
  tag creation, no write-back.
- **No secrets in git.** The API token comes from `secrets/` locally and from a mounted
  secret file in the container. Never commit a token, a real `.env`, or a URL that
  carries a token.
- **No receipt data in git.** `output/` and `tests/fixtures/raw/` are ignored. Fixtures
  are sanitised before they are committed: no real supplier names, amounts, account
  numbers or IBKR statement content.
- **Deterministic.** Same input gives byte-identical CSV. No LLM, no cloud OCR, no n8n,
  no guessing parsers (`dateparser`, `price-parser`). `Decimal` from strings only, never
  `float`. Dates compared as ISO strings, not datetimes.
- **Tests never touch the network.** Recorded Paperless JSON plus an injectable
  transport (`httpx.MockTransport` or equivalent).
- **Python 3.12**, `uv` with a committed `uv.lock`, src layout, stdlib-first. Ask before
  adding a dependency and say why in the commit or PR.

## Domain facts worth knowing before changing selection logic

- Selection is tag `tax-deductible` **and** `created` inside FY25-26. `created` is
  Paperless's parsed document date and the only date filter. Storage paths are derived
  from `created` and are not a signal.
- The row `date` is the custom field date, falling back to `created`. A field date
  outside the FY is a review item, not an exclusion.
- Amounts are GST-inclusive, from `Gross Amount`; GST from `GST Amount`; work use from
  `Claimable %`. Never read `actual_*` fields.
- Every document selected goes to `deductions.csv` or `review.csv`; tagged documents
  outside the FY are only counted in the manifest.
- IBKR documents are selected by tag `tax-information` plus correspondent
  "Interactive Brokers"; document types are a column, not a filter. A document in both
  sets goes to review, in neither CSV.
- The Paperless document ID is the traceability key on every row. No URLs in any CSV.

## Working style

- Prefer small commits with a clear scope. Do not reformat unrelated files.
- Add or update tests with every behaviour change.
- If a task depends on a live-instance fact that is not written down (custom field
  names, tag IDs, service name, ports), ask or inspect — do not invent a value.
