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
| `bills.csv` | The utilities and bills of the year: every Paperless document of the configured document type (default `bill`), with its kind (electricity, internet, phone, gas) from a tag. Header only, plus a warning, when the type is missing. See [Bills and the WFH scenarios](#bills-and-the-wfh-scenarios). |
| `estimate.csv` | An estimate of total deductions in AUD, with a separate pending figure, an optional tax saving and, from the bills, illustrative work-from-home scenarios. An estimate, not tax advice. See [The estimate](#the-estimate). |
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

### Review codes

A document goes to `review.csv` when it gets at least one code. `reason_codes` lists every
code that applies, `;` separated, in the order below. Fix the document in Paperless and
re-run; a document with no code is written to `deductions.csv`.

| Code | When it applies |
| --- | --- |
| `MISSING_DATE` | The date field (`Transaction Date`) has an entry that is empty, null or not an ISO date. A document with no entry at all gets no code: the row date is `created` and `notes_for_agent` says `date from created`. |
| `MISSING_AMOUNT` | The document has no entry for the amount field (`Transaction Amount`), or the amount field is not configured. |
| `MISSING_SUPPLIER` | The document has no correspondent. If an optional supplier custom field is configured (a library-only setting), the code also applies when the document has an entry in that field that is blank or null, even if it has a correspondent. |
| `AMOUNT_UNPARSEABLE` | The amount entry is empty, null or does not match an optional uppercase three-letter currency prefix directly followed by a number with optional `,` thousands grouping and decimals. `AUD12.34`, `12.34` and `AUD1,234.50` parse; `$12.34`, `aud12.34` and `AUD 12.34` do not. |
| `NEGATIVE_AMOUNT` | The amount is below zero (a credit note or refund), in any currency. |
| `ZERO_AMOUNT` | The amount is zero, in any currency: almost certainly a missing value or a placeholder. |
| `DATE_OUT_OF_FY` | The date field holds a valid date outside the financial year. Selection uses `created`, so this fires only when that field date is outside the FY; a field date that differs from `created` but is inside the FY gets no code. |
| `AMOUNT_OVER_300` | An AUD amount above 300, unless the document carries the reviewed tag (`asset-reviewed` by default). It prompts you to decide whether the purchase needs asset treatment. Not applied to other currencies. See [Clearing `AMOUNT_OVER_300`](#clearing-amount_over_300). |
| `DUPLICATE_SUSPECT` | Another document selected in this run has the same `supplier_name` (the correspondent by default), date, amount and currency. Documents with no parsed amount match each other. Every matching document gets the code. |
| `INCOME_TAGGED` | The document also carries an income tag (`income` or `dividend` by default). |

### Notes printed by `run`

`run` prints settings-level lines once per run, as `note:` on stdout, after a successful
run. They are information and do not change the exit code.

- `supplier_name is the document correspondent; no separate supplier custom field is
  configured (the default)`: the `supplier_name` column in `deductions.csv` is the
  document's correspondent. The command line cannot set a supplier field, so this line
  always appears; `review.csv` shows the same value in `correspondent`.
- `date field not configured; row date falls back to the document created date` and
  `amount field not configured; every row gets MISSING_AMOUNT` appear only when the tool
  is used as a library with those settings set to `None`. The command line never prints
  them.

Findings about your data or Paperless setup are printed separately as `warning:` lines on
stderr. They do not change the exit code either. Examples: the investing correspondent
is missing, a document is in both the deduction and investing sets, and an
`asset-reviewed` tag that does not exist while an `AMOUNT_OVER_300` row remains (see
[Clearing `AMOUNT_OVER_300`](#clearing-amount_over_300)).

Per-row information goes in `notes_for_agent`, joined with `; `: `amount missing` (the
amount field is configured and no amount was read), `date from created` (the date field
has no usable entry), the foreign-currency notes described above, and
`over-300 reviewed (tag <name>)` when the reviewed tag cleared `AMOUNT_OVER_300`.

CSV format: UTF-8 with BOM, `\r\n` line endings, ISO dates, two-decimal amounts without
currency symbols or thousands separators, rows sorted by date then document ID, and text
cells guarded against formula injection. The Paperless document ID is on every row as
the traceability key; no CSV contains URLs.

### Clearing `AMOUNT_OVER_300`

`AMOUNT_OVER_300` flags an AUD amount above 300 so you decide whether the purchase needs
asset treatment (depreciation) instead of an immediate deduction. The amount never
changes, so the flag cannot clear itself: you clear it in Paperless.

- To claim the item in full, tag the document with `asset-reviewed` (the default name; set
  another with `run --asset-reviewed-tag NAME`) and re-run. The row loses the code, goes
  to `deductions.csv` (when no other code applies), and `notes_for_agent` says
  `over-300 reviewed (tag asset-reviewed)`.
- The tag name is matched exactly and case-sensitively against the Paperless tag name, so
  `Asset-Reviewed` does not match. If several Paperless tags share the name, all of them
  count. The name must differ from the deduction tag. The command line cannot disable the
  check; only `Settings(asset_reviewed_tag=None)` does.
- To depreciate it, or if you are unsure, leave the tag off. The row stays in `review.csv`
  for you and the accountant to settle.
- The tag clears only this code. A zero or negative amount, a duplicate suspect, an income
  tag and every other code still send the row to review. It has no effect on a foreign
  amount (the check applies to AUD only) or on an amount of 300 or less.
- If the tag does not exist in Paperless and a row is still flagged, `run` warns once:
  `asset-reviewed tag 'asset-reviewed' not found in Paperless; AMOUNT_OVER_300 rows cannot
  be cleared until it exists`. `run` never creates the tag.
- The tag name is recorded in `manifest.json` as `parameters.asset_reviewed_tag`.

The tag records that you reviewed the item, not why. A total built from `deductions.csv`
counts a tagged item at its full amount.

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
  of `investments.csv`, `counts.bills_count` the rows of `bills.csv`, and `accepted + review`
  equals `selected_in_fy`.
- It prints one `OK` or `MISMATCH` line per file, one line per failed check and a summary.
  Files in the directory that the manifest does not list (`audit.json`, `manifest.json`)
  produce one `note:` line, never a failure. It never prints file contents. A listed file
  that is a symbolic link, a directory or a device is reported as `UNREADABLE` (exit 4);
  `verify` does not follow links.
- Exit 0 when everything matches. Exit 4 for any hash, row or count mismatch, or a listed
  file that is missing. Exit 2 when `manifest.json` is missing, unreadable, not valid JSON
  or lacks the expected keys or types, or lists a name that is not a plain file name (a path
  separator, a colon, leading or trailing whitespace, a trailing dot).

What it proves: the files are the bytes the run wrote, so it catches an accidental edit and
a corrupted or truncated copy. What it does not prove: that the files match Paperless, or
that nobody changed them on purpose. It is not tamper-proof, because whoever can edit a CSV
can regenerate the manifest too. After you hand-edit `review.csv`, `verify` fails on that
file, as it should, so run it straight after a run or against a copy.

A manifest written by 0.2.x lists `ibkr-documents.csv` and `counts.ibkr_count`, so `verify`
rejects it with exit 2. Re-run `run` to produce a current manifest.

`estimate.csv` is a required output: a manifest that does not list it also exits 2, naming it.

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

## The estimate

`run` also writes `estimate.csv`: an estimate of total deductions, built only from the
document amounts of that run. It is an estimate, not tax advice, and it ignores the rest
of your return. `manifest.json` records it, so `verify` checks it.

`estimate.csv` has these columns, in order: `section`, `label`, `count`, `currency`,
`amount`, `rate_to_aud`, `amount_aud`, `notes`. It follows the same byte contract as the
other CSVs (UTF-8 with BOM, `\r\n`, text cells guarded against formula injection,
two-decimal amounts; `rate_to_aud` is written as supplied). The rows are in this fixed
order:

| `section` | `label` | What it holds |
| --- | --- | --- |
| `estimate` | `ESTIMATE only; not tax advice` | The first data row. `notes` says the figures come from this run's document amounts, the rates file and the marginal rate you supplied, and ignore the rest of your return. |
| `deductions` | `total` | `count` is the number of `deductions.csv` rows, `amount_aud` the AUD total of what is claimable now. |
| `deductions_currency` | currency code | One row per currency in `deductions.csv`, sorted by code: `count`, the native total in `amount`, the rate used in `rate_to_aud` (blank for AUD or when there is none) and the converted total in `amount_aud` (blank when there is no rate; `notes` says `no rate; excluded from the AUD total`). |
| `deductions_by_type` | document type, `(none)` when blank | `count` and the AUD total of converted amounts, sorted by type name. |
| `deductions_by_month` | `YYYY-MM` of the row date | `count` and the AUD total of converted amounts, sorted ascending. |
| `bills`, `wfh_evidence`, `wfh_fixed_rate`, `wfh_actual_cost`, `wfh_scenarios`, `wfh_note` | see below | Bill totals and the illustrative WFH scenarios, between `deductions_by_month` and `pending`. They never change the deductions, pending or tax saving figures. See [Bills and the WFH scenarios](#bills-and-the-wfh-scenarios). |
| `pending` | `review rows with an amount` | The `review.csv` rows that have an amount: `count` and their AUD total, negatives included. These are not claimable yet. It is an upper bound: it includes suspected duplicates and items that may never be deductions (the `notes` cell says so). |
| `pending_no_amount` | `review rows without an amount` | `count` of the `review.csv` rows with no amount. |
| `tax_saving` | `estimated saving at marginal rate <rate>` | Only when `TAX_MARGINAL_RATE` is set: the deductions AUD total times the rate, in `amount_aud`. |

How the figures are worked out:

- Each row's AUD amount is `amount x rate`, rounded to cents with round-half-up
  (`ROUND_HALF_UP`, so a half cent goes up and a negative half cent goes down). AUD rows use
  rate 1. The totals are the sums of those per-row amounts, so rounding happens per row
  before adding, not once on the sum. Two rows of 0.01 at rate 0.5 give 0.02, not 0.01.
- The tax saving is the deductions AUD total times the marginal rate, rounded to cents
  with round-half-up. It uses the marginal rate only. It ignores the Medicare levy,
  offsets, thresholds and everything else in your return, which is why the row says
  `rough`.
- An item you cleared with the `asset-reviewed` tag is in `deductions.csv` and counts at its
  full amount.
- `pending` is not in the deductions total. It is an upper bound: `review.csv` also holds
  suspected duplicates, zero or negative amounts and income-tagged items, so it includes
  items that may never be deductions.
- A row whose currency has no rate is excluded from every AUD figure and counted in
  `notes` (for example `excluded, no rate: JPY 2`). Nothing is dropped silently. The
  `count` on the `deductions`, `deductions_by_type` and `deductions_by_month` rows still
  includes those excluded rows; only the AUD amounts leave them out.
- Limits: a rate has at most 6 integer digits and 12 fraction digits and no leading zeros
  (`0.5`, not `00.5`); an amount too large for the arithmetic exits 4 and writes nothing.

### Exchange rates: `run --rates FILE`

Foreign amounts are converted with rates you supply. The tool fetches no rates. The file
is a CSV with the header `currency,rate_to_aud` (UTF-8, with or without a BOM). Placeholder
example:

```csv
currency,rate_to_aud
USD,1.50
EUR,1.65
```

- `currency` is an uppercase three-letter code. `AUD` is not allowed (it is always 1).
- `rate_to_aud` is a positive decimal number: AUD for one unit of the currency. No
  exponent, sign or thousands separator.
- A duplicate currency, an unknown or missing column, a missing header, a bad number or a
  rate of zero or less exits 2 with a message that names the file and line, and at most the
  column name. Nothing is written.
- A currency in your data but not in the file is unconverted. `run` prints one warning on
  stderr per run listing the codes (`no exchange rate for JPY, USD; ...`). With no `--rates`
  flag every foreign amount is unconverted and the same warning appears.
- The file's SHA-256 is recorded in `manifest.json` as `parameters.rates_file_sha256`
  (`null` when there is no file), so you can tell which rates an estimate used. The rates
  themselves are not copied into the manifest.

### Marginal tax rate: `TAX_MARGINAL_RATE`

Set `TAX_MARGINAL_RATE` to your marginal rate as a decimal fraction, for example `0.32`,
to get the `tax_saving` row. It is optional: when it is not set there is no `tax_saving`
row. There is no command-line flag for it. It is read like `PAPERLESS_TOKEN`: a real
environment variable wins over an entry in the dotenv file named by `--env-file`.

```dotenv
TAX_MARGINAL_RATE=0.32
```

The value must be a plain decimal from 0 to 1 (`0` and `1` are accepted), with no leading
zeros (`0.5`, not `00.5`) and at most 12 fraction digits, so the manifest records it exactly
as typed. Anything else,
including an empty value, exits 2 with a message that names the variable and where it came
from, never the value. The rate is recorded in `manifest.json` as `parameters.marginal_rate`
(a string, or `null`).

### Console summary and `verify`

`run` prints one extra line, `estimate: deductions AUD 115.00, pending AUD -5.00`, with
`, estimated saving AUD 36.80` appended when a marginal rate is set. `--dry-run` prints the
same line and writes nothing.

`verify` requires `estimate.csv` in the manifest and checks its SHA-256 and data-row count
like the other files. A manifest that does not list it exits 2 and names `estimate.csv`;
re-run `run`.

## Bills and the WFH scenarios

`run` also writes `bills.csv` and adds work-from-home (WFH) scenarios to `estimate.csv`.
The scenarios are illustrative. They rest on your own assumptions about how the two WFH
methods work (below), which this tool does not check: confirm the method and every figure
with your accountant. Nothing here is tax advice.

### `bills.csv`

Bills are the Paperless documents of one document type, named `bill` by default
(`Settings.bill_document_type`; matched by exact name; a name used by two types exits 4). The
selection is the same financial-year window as the other outputs, on `created`. A missing
type is a warning (`document type 'bill' not found in Paperless; bills.csv has no rows`) and a
header-only file, not an error. As with the other filters, a returned document of another
type or created outside the FY exits 4 and writes nothing.

`bills.csv` has these columns, in order: `paperless_document_id`, `date`, `correspondent`,
`title`, `kind`, `amount_inc_gst`, `currency`, `notes_for_agent`. It follows the byte
contract of the other CSVs. Every selected bill is listed, sorted by `(date, id)`: it is a
data-gathering file with no review split.

- `date` is the `Transaction Date` field when it parses, else `created` (note `date from
  created`), the same rule as `deductions.csv`.
- `amount_inc_gst` and `currency` use the deduction rules (a prefix on the stored value,
  else the amount field's default currency, else `AUD`). A missing or unparseable amount
  leaves both blank with the note `amount missing`. A negative or zero amount is listed with
  `negative amount` or `zero amount`.
- `kind` comes from a tag, one tag per kind. The defaults are the tags `electricity`,
  `internet`, `phone` and `gas` (`Settings.bill_kind_tags` maps each kind to its tag name;
  each kind needs its own tag). A bill with none of them has a blank kind and the note
  `no kind tag`. A bill with several has a blank kind and the note
  `several kind tags: a; b` (the tag names, sorted). Other tags are ignored. Tag names match
  case-sensitively, so `Gas` is not `gas`. A kind tag may not have the name of the deduction
  tag or the `asset-reviewed` tag (exit 2). When bills are listed and a configured kind tag
  name does not exist in Paperless, `run` warns once on stderr naming the missing tag names.

Notes are joined with `; ` in this order: amount, date, kind, deduction overlap.

### The overlap rule

A bill that also carries the deduction tag (`tax deductible`) stays in `deductions.csv` or
`review.csv` like any other document. In `bills.csv` it is still listed, with the note
`also in deductions.csv; excluded from WFH totals`, and `run` warns on stderr with its id. It
is left out of every bill figure in `estimate.csv` (totals, kinds, evidence counts) so
nothing counts twice. The note names `deductions.csv`, but a document with a review code is
in `review.csv` instead; the warning says `deductions.csv or review.csv`. Negative and zero bills are listed but not counted in
those figures; a bill with no amount is counted but adds nothing to a total.

### The WFH variables

Five optional variables supply the scenario inputs. They are read like `TAX_MARGINAL_RATE`:
a real environment variable wins over an entry in the dotenv file named by `--env-file`;
there are no command-line flags. An empty or invalid value exits 2 with a message that names
the variable and where it came from, never the value. Each value is recorded as typed in
`manifest.json` under `parameters` (`wfh_hours`, `wfh_rate_per_hour`, `wfh_internet_pct`,
`wfh_phone_pct`, `wfh_energy_amount`; `null` when unset), so `verify` covers them.

| Variable | Meaning | Valid values |
| --- | --- | --- |
| `WFH_HOURS` | Hours worked from home in the year, for the fixed-rate scenario. | Non-negative decimal |
| `WFH_RATE_PER_HOUR` | The fixed rate in AUD per hour. There is no default: you supply it. | Non-negative decimal |
| `WFH_INTERNET_PCT` | Work-use share of the internet bills, as a percentage (`50` is half, not `0.5`). | Decimal from 0 to 100 |
| `WFH_PHONE_PCT` | Work-use share of the phone bills, as a percentage. | Decimal from 0 to 100 |
| `WFH_ENERGY_AMOUNT` | Your own appliance-based energy figure in AUD. The tool does not compute energy from bills. | Non-negative decimal |

```dotenv
WFH_HOURS=100
WFH_RATE_PER_HOUR=0.70
WFH_INTERNET_PCT=50
WFH_PHONE_PCT=25
WFH_ENERGY_AMOUNT=30.00
```

### The scenario rows

The rows follow `deductions_by_month` in `estimate.csv`, in this fixed order. Amounts are
`amount_aud`, converted with the rates file like the deductions and rounded to cents with
round-half-up.

| `section` | `label` | What it holds |
| --- | --- | --- |
| `bills` | `total` | `count` and AUD total of the bills counted (see the overlap rule). `notes` lists currencies left out for lack of a rate. |
| `bills` | `electricity`, `internet`, `phone`, `gas`, `(no kind)` | `count` and AUD total per kind, always all five. |
| `wfh_evidence` | `internet`, `phone`, `energy` | The number of counted bills for each covered expense (`energy` is electricity plus gas); `notes` says `no bill found` when it is zero. |
| `wfh_evidence` | `stationery` | A note only: the tool does not track stationery. |
| `wfh_fixed_rate` | `hours x rate` | `WFH_HOURS` x `WFH_RATE_PER_HOUR`. Blank with `not computed: set WFH_HOURS and WFH_RATE_PER_HOUR` when either is missing. |
| `wfh_actual_cost` | `internet`, `phone` | The counted bills' AUD total x the percentage. Blank with `not computed: set <VARIABLE>` when it is missing. With the percentage set but no counted bill of that kind, the row is `0.00` and `notes` says `no internet bills found` (or `no phone bills found`). |
| `wfh_actual_cost` | `energy` | The value of `WFH_ENERGY_AMOUNT`. |
| `wfh_actual_cost` | `total` | The sum of the rows that were computed; `notes` names the variables still missing and any kind with no bills, and starts with `partial` when a computed total has such gaps. Blank when none was computed. |
| `wfh_scenarios` | `base deductions + fixed rate`, `base deductions + actual cost` | The deductions total plus the method's figure, for each method computed fully or partly. `notes`: `illustrative; one method per year; not added to the base total`, followed by the total's `partial` note when it has one. |
| `wfh_note` | `illustrative only` | Starts with `per your own notes:`. Your own assumption, not a verified rule: the fixed rate covers internet, phone and energy, so those bills would not also be claimed in `deductions.csv`. |

The WFH figures are never added to `deductions`, `pending` or `tax_saving`. The method is
undecided and one method applies per year, so both are shown side by side.

### What the scenarios assume

These are the owner's own working assumptions, from personal research that is partly
unverified. They are not facts this tool establishes, and the ATO and your accountant decide:

- Fixed rate: hours x a per-hour rate (the owner's notes say $0.70 for 2024-25 and 2025-26)
  covers internet, phone, energy for heating, cooling and lighting, and stationery, with one
  bill or receipt per covered type as evidence.
- Actual cost: internet and phone by annual cost x work-use percentage; energy from the
  appliances used, not as a share of the bill.
- One method per year. The owner and the accountant choose it.

Check each point, and the rate you put in `WFH_RATE_PER_HOUR`, before relying on a figure.

### Console and `verify`

When any bills are listed, `run` prints `bills: N listed, M counted, AUD <total>` after the
estimate line. `N` counts every row of `bills.csv`; `M` counts only the bills that feed the
estimate (not overlapping with the deduction tag, not negative or zero); `<total>` is their
AUD total. `--dry-run` prints the same line
and writes nothing. The outputs are written in this order: `deductions.csv`, `review.csv`,
`investments.csv`, `bills.csv`, `estimate.csv`, `manifest.json`. `bills.csv` is listed in
the manifest with `counts.bills_count` and a file entry, and `verify` requires it: a manifest
that does not list it exits 2 and names `bills.csv`; re-run `run`. The manifest also records
`parameters.bill_document_type` and `parameters.bill_kind_tags`.

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
