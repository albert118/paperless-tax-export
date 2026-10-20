# Recorded fixtures

Sanitised recordings of real runs, committed so the tests can keep the audit's
JSON contract honest without a network or a live instance.

Every file here is produced by `paperless-tax-export audit --output <dir>` and
then copied in unchanged. The audit's `dump_json` is the only sanctioned
committed data path: it carries counts and metadata only, never document
titles, ids, dates, custom-field values, file names or amounts.

`tests/test_audit_fixtures.py` loads every `*.json` here and fails if a file is
not canonical JSON or no longer matches the nested schema `dump_json` emits,
and checks that the recording carries no document-derived data. Recordings are
schema-checked, never replayed against a live instance: adding a recording
costs one file and no test code.

## audit-live-2026-10-20-pre-permissions.json

- **Recorded:** 2026-10-20, from `https://paperless.daedalus-project.dev/`
  (Paperless-ngx 3.3.2, API v10).
- **Why it is kept:** it is the only recorded evidence of the lenient failure
  path — a half-configured instance where the selection tag matched nothing,
  so the deduction set was empty. It shows the audit reports findings and exits
  0 instead of treating a misconfiguration as fatal.
- **It is not a baseline.** It was recorded *before* the tag, field-name and
  permissions corrections: it says `tax-deductible` (the real tag is
  `tax deductible`), and it expects `Gross Amount` (the instance has
  `Transaction Amount`). It predates the `tax_metadata_tag` and `notes` keys,
  the recursive schema check tolerates the omission, and its `ibkr` block was
  pruned of the retired tag keys so the recording stays schema-shaped.

## audit-live-2026-10-20T1527Z.json

- **Recorded:** 2026-10-20 15:27 UTC, from `https://paperless.daedalus-project.dev/`
  (Paperless-ngx 3.3.2, API v10).
- **Why it is kept:** it records the audit's robustness against a server whose
  absolute link builder is misconfigured. The correspondents fetch followed a
  page-2 `next` that the proxy 301-redirected (the link was absolute
  `http://`, the proxy answers `https://`), the follow failed and the
  correspondents were captured in `unavailable`, exactly as the optional-read
  resilience is meant to behave. Root cause: the proxy does not set
  `X-Forwarded-Proto`, so the server builds `http://` links. The recording is
  expected to disappear once the proxy fix lands and re-recording shows the
  correspondents coming back.
- **Translated to the current schema.** The recording predates the IBKR-block
  change: its `ibkr.tag*` facts were moved to the top-level
  `tax_metadata_tag`, and the retired `correspondent_without_tag` /
  `tag_with_other_correspondent` counters removed. No counts were altered.

## audit-live-2026-10-20T1554Z.json

- **Recorded:** 2026-10-20 15:54 UTC, from `https://paperless.daedalus-project.dev/`
  (Paperless-ngx 3.3.2, API v10) — the first run after the client re-anchors
  server-advertised `next` links to the configured origin.
- **What it shows:** `unavailable` is empty, correspondents are visible (184),
  the IBKR correspondent resolves with 24 documents, and the `notes` list keeps
  the rewrite visible: the server still advertises `http://` `next` links, so
  the proxy fix is outstanding. This is the recording that proves the fix in
  production rather than by construction.
- **Facts it pins:** deduction tag `tax deductible` (48 documents, 14 in the
  FY), `Transaction Amount` present on 3 of 14, the two IBKR document types,
  and the FY storage path with 19 documents.
- The partial-failure facts it pins down (non-empty `unavailable` naming
  `correspondents`, `correspondents_visible == 0`, the deduction tag among
  `tag_matches`, `deduction_document_count == 13`, `tax_metadata_tag.exists`)
  are asserted by `test_audit_fixtures.py`.