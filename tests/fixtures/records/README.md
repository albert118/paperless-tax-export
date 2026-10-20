# Recorded fixtures

Sanitised recordings of real runs, committed so tests can replay them without a
network or a live instance.

Every file here is produced by `paperless-tax-export audit --output <dir>` and
then copied in unchanged. The audit's `dump_json` is the only sanctioned
committed data path: it carries counts and metadata only, never document
titles, ids, dates, custom-field values, file names or amounts.

`tests/test_audit_fixtures.py` loads every `*.json` here and fails if a file is
not canonical JSON or no longer matches the schema `dump_json` emits. Adding a
recording therefore costs one file and no test code.

## audit-live-2026-10-20.json

- **Recorded:** 2026-10-20, from `https://paperless.daedalus-project.dev/`
  (Paperless-ngx 3.3.2, API v10).
- **Why it is kept:** it is the only recorded evidence of the lenient failure
  path — a half-configured instance where the selection tag matched nothing, so
  the deduction set was empty. It shows the audit reports findings and exits 0
  instead of treating a misconfiguration as fatal.
- **It is not a baseline.** It was recorded *before* the tag and field-name
  corrections: it says `tax-deductible` (the real tag is `tax deductible`), and
  it expects `Gross Amount` (the instance has `Transaction Amount`). The
  coverage counts are zero because no documents were selected, not because the
  fields are unused.
