# paperless-tax-export

Read-only tax export for [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx).

It reads one financial year of documents from Paperless and produces CSVs an accountant
can work from, plus a review list of the documents a human still has to decide on.

Both `audit` and `run` talk to Paperless: `audit` reports how the live instance lines up
with the settings, and `run` classifies the FY documents and writes `deductions.csv`,
`review.csv` and `investments.csv`.
`verify` talks to nothing: it re-checks a finished output directory against its manifest.

To run it in Docker next to Paperless-ngx, see [examples/README.md](examples/README.md).

## What it produces

| File | Contents |
| --- | --- |
| `review.csv` | Documents that need a human decision, with reason codes. The priority output. |
| `deductions.csv` | Documents that pass every check. |
| `investments.csv` | Index of the investing portfolio statements for the year: the documents of the one configured correspondent (`--ibkr-correspondent`, default `Interactive Brokers`). Header only, plus a warning, when the correspondent is missing. A document that also carries the deduction tag appears here and in the deduction outputs, with a warning. |
| `manifest.json` | Run parameters, tool and Paperless versions, counts, expected count, and SHA-256 and row count of each other output. No timestamp. |

All files go into `output/`, which is never committed.

`deductions.csv` has these columns, in order: `paperless_document_id`, `date`,
`supplier_name`, `description`, `amount_inc_gst`, `currency`, `document_type`,
`notes_for_agent`.

`review.csv` has its own columns, in order: `paperless_document_id`, `reason_codes`,
`date`, `correspondent`, `title`, `amount_inc_gst`, `currency`, `document_type`,
`created`, `notes_for_agent`.

`currency` is the three-letter ISO code of the parsed amount: a prefix on the stored value,
else the amount field's default currency, else `AUD`. It is blank when the amount is
missing or unparseable. `amount_inc_gst` is a plain number with two decimals for every
currency (there is no minor-unit table, so JPY is also written with two decimals).

A foreign-currency amount is accepted into `deductions.csv` as it is: it is never converted
and has no review code. `notes_for_agent` says `foreign currency <CODE>; amount not
converted to AUD` and `over-300 check not applied to <CODE>`. The `AMOUNT_OVER_300` review
code applies to AUD amounts only; `NEGATIVE_AMOUNT` applies to every currency. A zero
amount (`0`, `0.00`, `-0.00`, `AUD0.00`, `USD0`) is blocking in every currency: it gets
`ZERO_AMOUNT` and goes to `review.csv`, because a zero receipt is almost certainly a
missing value or a placeholder. Earlier
versions sent foreign-currency amounts to `review.csv` under a `FOREIGN_CURRENCY` code,
which no longer exists.

`investments.csv` has these columns, in order: `paperless_document_id`, `title`,
`created`, `document_type`, `correspondent`.

Earlier versions wrote `ibkr-documents.csv` and more columns: `gst_amount`,
`work_use_pct` and `document_modified` in both deduction files, and `tags`, `raw_amount`
and `raw_date` in `review.csv`, so column positions differ from the current output.
`category_label`, `claim_type`, `apportionment_basis`, `reimbursed`, `wfh_method`,
`asset_cost_inc_gst` and `asset_set_id` are deferred to a later revision.

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

- tags matching `--tag` (default `tax deductible`) and their document counts;
- every custom field in the instance, with type, options and document count;
- per-field value coverage over the FY deduction documents (present / null / absent);
- storage paths (informational — selection is `created`-only);
- `archived_file_name` path segments under the FY deduction documents (console only), with
  how many documents have and lack an archived version (those two counts also go to
  `audit.json`);
- IBKR identifiers: whether `--ibkr-correspondent` (`Interactive Brokers`) exists, which
  document types match `trade confirmation` / `realized gains summary`, and near-misses;
  IBKR selection is correspondent-only;
- the configured `--tax-info-tag` (default `tax-information`): exists, document count and
  near-misses, reported with a caveat that it is general tax metadata, not IBKR-specific;
- whether the expected field names exist (`Transaction Amount` is required; `GST Amount`
  and `Claimable %` are optional), and the Paperless API/server versions.

The audit is a diagnostic, not a gate: missing tags, missing fields and zero coverage
are findings and the exit code stays 0. Endpoints the token cannot read are recorded in
`unavailable` and listed as warnings. Exit codes are 2 for bad flags or an unreadable or
empty token (from any source) or unreadable `--env-file`, 3 for an auth/network/server error on the document fetch (only made
when exactly one tag matches) or when every optional read failed and no tags,
correspondents, document types, storage paths or custom fields came back at all, and 4
for a pagination guard failure.
`output/audit.json` carries counts and metadata only — never document titles,
dates, field values or file names — so it is safe to copy into `tests/fixtures/` as a
recorded fixture or share with someone else.

The audit also reports how many tags, correspondents, document types and storage paths
the token can see, lists any endpoint it could not read (console `Warnings` and the JSON
`unavailable` list), and states the UTC timestamp and base URL the audit ran against.

### Run the export

```sh
uv run paperless-tax-export run   --base-url https://paperless.example.com   --token-file secrets/paperless-token.txt   --expected-count 14   --dry-run
```

- `--expected-count N` is the number of documents the Paperless admin view shows for the
  tag with the FY `created` filter applied. A different count exits 4, names both
  numbers and writes nothing. It is a check against documents the token cannot see; it
  does not cover documents outside the FY window.
- `--dry-run` runs every guard and prints the counts and planned files without creating
  the output directory or any file. Drop it for the real run.
- Every guard (paging integrity, expected count, `accepted + review == selected`) runs
  before any file is written. `manifest.json` is written last; its hashes are of the
  exact bytes of the other files, and the same data gives a byte-identical manifest.
- Paperless silently ignores filters it does not recognise, so the selection also checks
  every returned document: a deduction document without the tag or created outside the FY
  exits 4 (as does an IBKR document of another correspondent), and so does a tagged count
  below the FY-selected count (Paperless changed during the run; re-run). Nothing is written.

### Verify an output directory

```sh
uv run paperless-tax-export verify --output output
```

`verify` checks a finished output directory against its `manifest.json`. It is offline: no
token, no network, and it reads no Paperless settings or environment variables. `--output`
defaults to the directory `run` writes into.

- For each file the manifest lists, it recomputes the SHA-256 of the bytes on disk and the
  number of data rows (header excluded, counted with a CSV parser, so a quoted title with a
  line break is one row) and compares both with the manifest.
- It also checks the manifest against the files: `counts.accepted` equals the rows of
  `deductions.csv`, `counts.review` the rows of `review.csv`, `counts.investments_count` the rows
  of `investments.csv`, and `accepted + review` equals `selected_in_fy`.
- It prints one `OK` or `MISMATCH` line per file, one line per failed check and a summary.
  Files in the directory that the manifest does not list (`audit.json`, `manifest.json`)
  produce one `note:` line, never a failure. It never prints file contents. A listed file
  that is a symbolic link, a directory or a device is reported as `UNREADABLE` (exit 4);
  `verify` does not follow links.
- Exit 0 when everything matches. Exit 4 for any hash, row or count mismatch, or a listed
  file that is missing. Exit 2 when `manifest.json` is missing, unreadable, not valid JSON
  or lacks the expected keys or types.

What it proves: the files are the bytes the run wrote, so it catches an accidental edit and
a corrupted or truncated copy. What it does not prove: that the files match Paperless, or
that nobody changed them on purpose. It is not tamper-proof, because whoever can edit a CSV
can regenerate the manifest too. After you hand-edit `review.csv`, `verify` fails on that
file, as it should, so run it straight after a run or against a copy.

A manifest written by 0.2.x lists `ibkr-documents.csv` and `counts.ibkr_count`, so `verify`
rejects it with exit 2. Re-run `run` to produce a current manifest.

## The Paperless token

The token is never committed and never pasted into a file that is tracked by git.

1. Create a Paperless user with view permission only — no staff, no superuser.
2. Make sure that user can see the target documents. A document the user cannot see is
   silently missing from the export, with no error.
3. Create the API token for that user and store it in a password manager.
4. Supply it through one of the sources below. Never commit it.

### Configuration sources

`audit` and `run` read the token and base URL from the first of these that is set:

1. the command-line flag: `--token-file PATH` or `--base-url URL`;
2. a real environment variable: `PAPERLESS_TOKEN` (the token itself) or
   `PAPERLESS_BASE_URL`;
3. an entry of the same name in the dotenv file named by `--env-file PATH` (or the
   `PAPERLESS_ENV_FILE` environment variable);
4. the default: `secrets/paperless-token.txt` and `http://localhost:8000`.

There is no `--token` flag, so the token never shows up in the process list. A `.env`
in the working directory is never loaded; the dotenv file is read only when you name it.
Its entries are parsed, not exported, so a real environment variable always wins. An
empty or whitespace-only token, or an unreadable `--env-file`, exits 2. Error messages
name the source, never the value.

The first source that sets a value wins even when that value is empty: a blank
`PAPERLESS_TOKEN=` (for example from `${PAPERLESS_TOKEN:-}` in compose) is an error and
does not fall through to the env file or the default token file. If the env file has no
usable `PAPERLESS_TOKEN` line, check it for a missing or malformed line. The dotenv file
is parsed by python-dotenv, not by compose, so write the token without `$`, quotes or
`#`; a Paperless token is plain hex.

Example dotenv file (placeholder values):

```dotenv
PAPERLESS_TOKEN=replace-with-the-view-only-api-token
PAPERLESS_BASE_URL=https://paperless.example.com
```

Keep this file outside the repository and make it readable only by you
(`chmod 600 /path/to/paperless.env`). In the container, docker compose injects it with
`env_file:`, so the tool sees ordinary environment variables. On another machine, pass
the same file:

```sh
uv run paperless-tax-export audit --env-file /path/to/paperless.env
```

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

CI runs the same checks on every pull request and on pushes to `main`. The container
image is built on pull requests and pushed to `ghcr.io/albert118/paperless-tax-export`
only on a release.

Fixtures recorded from the live instance are sanitised before they are committed. Real
receipts and IBKR statements stay out of git, under `tests/fixtures/raw/` or `scratch/`.
