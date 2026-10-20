# paperless-tax-export

Read-only tax export for [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx).

It reads one financial year of documents from Paperless and produces CSVs an accountant
can work from, plus a review list of the documents a human still has to decide on.

## Status

Bootstrap complete. What is in place:

| Path | Purpose |
| --- | --- |
| `pyproject.toml`, `uv.lock` | uv project, Python 3.14, pinned dependencies |
| `src/paperless_tax_export/` | Package. The CLI supports `--help`, `--version` and the `audit` subcommand |
| `tests/` | pytest suite |
| `Dockerfile`, `.dockerignore` | Non-root image built from a locked uv install |
| `.gitignore`, `.gitattributes`, `.editorconfig`, `.python-version` | Environment |
| `.vscode/` | Ruff formatter on save, pytest discovery, file nesting |
| `.github/` | CI, image build, pull request labeler, dependabot, release notes |
| `AGENTS.md` | Rules and context for coding agents |

The `audit` subcommand reads the live Paperless instance read-only and reports how it
lines up with the export settings (see below); it is the only thing that touches
Paperless today. There is no CSV writer and no `run` subcommand yet. Year selection and field resolution exist
in the library but are not exposed on the command line, so nothing is exported.

CI runs on every pull request and on pushes to `main`: ruff, ruff format check, mypy and
pytest. The container image is built on pull requests and pushed to GHCR only on a
release.

## What it produces

| File | Contents |
| --- | --- |
| `review.csv` | Documents that need a human decision, with reason codes. The priority output. |
| `deductions.csv` | Documents that pass every check. |
| `ibkr-documents.csv` | Index of Interactive Brokers statements for the year. |
| `manifest.json` | Run parameters, Paperless version, counts and file hashes. |

All files go into `output/`, which is never committed.

CSV format: UTF-8 with BOM, `\r\n` line endings, ISO dates, two-decimal amounts without
currency symbols or thousands separators, rows sorted by date then document ID, and text
cells guarded against formula injection. The Paperless document ID is on every row as
the traceability key; no CSV contains URLs.

## How it reads Paperless

- Read-only. The API token belongs to a view-only user, and the client makes GET
  requests only.
- Values come from Paperless custom fields. There is no OCR text parsing.
- Scope is FY25-26 only (2025-07-01 to 2026-06-30), selected by the document's `created`
  date, filtered by tag.
- Amounts are GST-inclusive, which is the whole claim for an individual who is not
  registered for GST.

### Audit the live instance

Run the audit before the first export to see how the live instance lines up with the
settings:

```sh
uv run paperless-tax-export audit \
  --base-url https://paperless.example.com \
  --token-file secrets/paperless-token.txt
```

The audit prints a console report and writes `output/audit.json`. It reports, without
failing:

- tags matching `--tag` (default `tax-deductible`) and their document counts;
- every custom field in the instance, with type, options and document count;
- per-field value coverage over the FY deduction documents (present / null / absent);
- storage paths (informational — selection is `created`-only);
- `archived_file_name` path segments under the FY deduction documents (console only);
- IBKR identifiers: whether `--ibkr-tag` and `--ibkr-correspondent` exist, which
  document types match `trade confirmation` / `realized gains summary`, and near-misses
  inside the FY deduction set;
- whether the expected field names exist (`Gross Amount` is required; `GST Amount` and
  `Claimable %` are optional), and the Paperless API/server versions.

The audit is a diagnostic, not a gate: missing tags, missing fields and zero coverage
are findings and the exit code stays 0. Exit codes are 2 for bad flags or an unreadable
or empty token file, 3 for auth/network/server errors, and 4 for pagination guard
failures. `output/audit.json` carries counts and metadata only — never document titles,
dates, field values or file names — so it is safe to copy into `tests/fixtures/` as a
recorded fixture or share with someone else.

## The Paperless token

The token is never committed and never pasted into a file that is tracked by git.

1. Create a Paperless user with view permission only — no staff, no superuser.
2. Make sure that user can see the target documents. A document the user cannot see is
   silently missing from the export, with no error.
3. Create the API token for that user and store it in a password manager.
4. Locally, write it to `secrets/paperless-token.txt` (ignored by git). In the container
   it is a mounted secret file.

## Development

The loop:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run paperless-tax-export --help
docker build -t paperless-tax-export .
```

`uv` is required. Python 3.14 is pinned in `.python-version`.

Fixtures recorded from the live instance are sanitised before they are committed. Real
receipts and IBKR statements stay out of git, under `tests/fixtures/raw/` or `scratch/`.

## Design notes

The plan, the decisions behind it and the output contract live outside this repository
in the owner's notes (`tax-helper-mvp-plan`, `tax-helper-requirements`).
