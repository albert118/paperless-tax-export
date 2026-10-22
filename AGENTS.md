# AGENTS.md

Operating notes for coding agents working in `paperless-tax-export`.

This is a personal, private tool. Conventions from other repositories do not apply here.

## What this is

A read-only Python helper that reads documents from Paperless-ngx and writes tax-ready
CSV output for one financial year (FY25-26: 2025-07-01 to 2026-06-30). It reads Paperless
custom fields only; there is no OCR text parsing in the current scope.

Outputs, written into `output/`:

- `review.csv`: documents that need a human decision, with reason codes. This is the
  priority output.
- `deductions.csv`: documents that pass every check.
- `investments.csv`: index of the investing portfolio statements (one configured correspondent).
- `bills.csv`: utilities and bills (Paperless documents of type `bill`), kind from a tag.
- `estimate.csv`: an estimate of total deductions in AUD (the sum of `deductions.csv`), a
  separate pending figure for `review.csv` rows, and an optional tax saving. Foreign amounts
  are converted with the owner's rates file (`run --rates FILE`). It also shows the bills and
  two illustrative WFH scenarios that never change those figures. Not tax advice.
- `manifest.json`: parameters, versions, counts and file hashes.

## Current state

The repository builds, lints, type-checks and tests. The CLI has a working `audit`
subcommand (plus `--version`) that reads the live Paperless instance read-only and
reports how it lines up with the export settings (`output/audit.json`). The audit is a
diagnostic, not a gate: a missing tag, zero matching documents, missing fields and zero
coverage are printed findings and the exit code stays 0. The `run` subcommand
classifies the FY documents and writes `deductions.csv` and `review.csv`, plus
`investments.csv` (the configured investing correspondent's FY documents), `bills.csv`
(the FY documents of type `bill`), `estimate.csv` and `manifest.json` (no timestamp, written
last), in that order. `run --expected-count N` compares N with
the FY-scoped selected count and `run --dry-run` runs every guard and writes nothing.
`verify [--output DIR]` re-checks a finished output directory offline: SHA-256 and data-row
count of every file in `manifest.json`, plus the manifest's own counts (exit 0, 4 on a
mismatch or missing listed file, 2 on an unusable manifest, which includes a manifest
written by 0.2.x (it lists `ibkr-documents.csv` and `counts.ibkr_count`) or one that does
not list `bills.csv` or `estimate.csv`).

The estimate's setting: `TAX_MARGINAL_RATE` (a decimal fraction from 0 to 1) is resolved like
`PAPERLESS_TOKEN` through `EnvSources.lookup` (real env var, then dotenv; no command-line flag)
and is optional; without it there is no `tax_saving` row. Both it and the SHA-256 of the
`--rates` file are recorded in the manifest parameters. Amounts are `Decimal` and each row's
AUD amount is rounded to cents (`ROUND_HALF_UP`) before summing.

Bills: `Settings.bill_document_type` (default `bill`, exact name, resolved from the already
fetched document types; a missing type is a warning and a header-only file, two types with
the name exit 4) and `Settings.bill_kind_tags` (kind to tag name, defaults `electricity`,
`internet`, `phone`, `gas`). A bill with the deduction tag stays in the deduction outputs,
is listed in `bills.csv` with a note and warned about, and is excluded from the bill figures
in `estimate.csv`. The WFH scenarios (fixed rate and actual cost, side by side, never added
to the base total) read `WFH_HOURS`, `WFH_RATE_PER_HOUR`, `WFH_INTERNET_PCT` (0 to 100),
`WFH_PHONE_PCT` (0 to 100) and `WFH_ENERGY_AMOUNT` through `EnvSources.lookup` like
`TAX_MARGINAL_RATE` (no flags; an empty or invalid value exits 2). They are recorded as typed
in the manifest parameters. The method assumptions behind them are the owner's, not verified.

In place: `pyproject.toml` and `uv.lock` (uv, Python 3.14), `src/paperless_tax_export/`,
`tests/`, `Dockerfile` and `.dockerignore`, the dotfiles, `.vscode/`, `.github/` (CI,
image build, labeler, dependabot) and the docs.

The CLI supports `--help`, `--version`, `audit`, `run` and `verify`. The library has a
read-only Paperless client, typed API models, amount and date parsing, and
financial-year selection. `audit` diagnoses the live instance; `run` drives the
classification/CSV pipeline.

CI runs ruff, ruff format check, mypy and pytest on every pull request and on pushes to
`main`. `docker.yml` builds the image on pull requests and pushes to GHCR only on a
release. Do not disable or weaken those checks to get a change through.

## Rules that must not be broken

- **Read-only toward Paperless.** GET requests only. No write client, no PATCH, no
  tag creation, no write-back.
- **No secrets in git.** The API token comes from, in order, `--token-file`, the
  `PAPERLESS_TOKEN` environment variable, a `PAPERLESS_TOKEN` entry in the dotenv file named
  by `--env-file` (or `PAPERLESS_ENV_FILE`), then `secrets/paperless-token.txt`. In the
  container, compose injects a chmod 600 dotenv file kept outside the stack and outside the
  repo. There is no `--token` flag, and a `.env` in the working directory is never
  auto-loaded. The dotenv file is parsed only and never written into `os.environ`. Never
  commit a token, a real `.env`, or a URL that carries a token, and never print the token or
  any part of it (errors name the source, not the value).
- **No receipt data in git.** `output/` and `tests/fixtures/raw/` are ignored. Fixtures
  are sanitised before they are committed: no real supplier names, amounts, account
  numbers or IBKR statement content.
- **Deterministic.** Same input gives byte-identical CSV. No LLM, no cloud OCR, no n8n,
  no guessing parsers (`dateparser`, `price-parser`). `Decimal` from strings only, never
  `float`. Dates compared as ISO strings, not datetimes.
- **Tests never touch the network.** Recorded Paperless JSON plus an injectable
  transport (`httpx.MockTransport` or equivalent).
- **Python 3.14**, `uv` with a committed `uv.lock`, src layout, stdlib-first. Ask before
  adding a dependency and say why in the commit or PR.

## Domain facts worth knowing before changing selection logic

- Selection is tag `tax deductible` **and** `created` inside FY25-26. `created` is
  Paperless's parsed document date and the only date filter. Storage paths are derived
  from `created` and are not a signal.
- The row `date` is the custom field date, falling back to `created`. A field date
  outside the FY is a review item, not an exclusion.
- Amounts are GST-inclusive, from the live field `Transaction Amount`. GST and work-use
  values are not exported (the `GST Amount` and `Claimable %` settings stay, unused by
  `run`). The live row-date field is `Transaction Date`. Never read `actual_*` fields.
- Foreign-currency amounts are accepted unconverted, with a `currency` column and a note;
  `AMOUNT_OVER_300` applies to AUD only. A document carrying the reviewed tag (default
  `asset-reviewed`, `run --asset-reviewed-tag`, recorded as `parameters.asset_reviewed_tag`
  in the manifest) loses that one code and nothing else; the name must differ from the
  deduction tag.
- A zero amount in any currency gets the blocking `ZERO_AMOUNT` code and goes to
  `review.csv`, never `deductions.csv`.
- Every document selected goes to `deductions.csv` or `review.csv`; tagged documents
  outside the FY are only counted in the manifest.
- IBKR selection is correspondent-only: correspondent "Interactive Brokers". The
  `tax-information` tag is general tax metadata (190 documents, all years), not an
  IBKR marker, and is only counted as metadata by the audit's `--tax-info-tag`.
  Document types are a column, not a filter. A document in both sets appears in both
  outputs with one warning line and no review code. A missing correspondent is a
  warning and a header-only `investments.csv`, never an error.
- The Paperless document ID is the traceability key on every row. No URLs in any CSV.

## Working style

- Prefer small commits with a clear scope. Do not reformat unrelated files.
- Add or update tests with every behaviour change.
- If a task depends on a live-instance fact that is not written down (custom field
  names, tag IDs, service name, ports), ask or inspect — do not invent a value.
