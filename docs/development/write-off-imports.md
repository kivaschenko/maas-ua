# Bulk serialized-inventory write-off import (M7.4)

Run the existing local Flask application with synthetic data and migration head `0010`,
as described in [receipt imports](receipt-imports.md). Open
<http://127.0.0.1:5000/write-offs/import/new> or select “Списання” in navigation.
The application does not migrate or provision a database at startup.

## File contract: serialized_inventory_write_off_v1

Download the blank XLSX or CSV template from the upload page. One file represents one
write-off act and one explicit accounting acceptance date. Different receipts,
nomenclatures and custodians may participate. The importer never splits documents.
No UUID, quantity, price, account or journal reference is entered in the file.

XLSX contains exactly two worksheets:

- `Document`: columns `field,value`, then each required metadata field once.
- `SerialNumbers`: columns `serial_namespace,serial_value`, one exact text identity per row.

| Document field | Synthetic example |
| --- | --- |
| `template_version` | `serialized_inventory_write_off_v1` |
| `document_number` | `EXAMPLE-WRITE-OFF` |
| `document_date` | `2026-02-01` |
| `authorization_reference` | `EXAMPLE-AUTH` |
| `authorization_date` | `2026-02-03` |
| `effective_date` | `2026-02-10` |

The effective date means acceptance of the verified and authorized package by the
financial-economic service. It must be entered explicitly; neither act date, physical
loss/use, authorization, initial document delivery nor registration time supplies it.
Dates accept exact ISO `YYYY-MM-DD` or Excel dates at midnight. No additional act-date
ordering, financial-period restriction or future-date rule is introduced.

CSV is UTF-8 (optional BOM), explicitly comma- or semicolon-delimited. Its header contains
all six metadata fields followed by the two serial columns. Repeat the same document
metadata on every row; inconsistent metadata, extra columns and multiple documents fail.
A populated [synthetic CSV](../examples/serialized_inventory_write_off_v1.csv) selects
`EXAMPLE-ISSUER / EXAMPLE-0000` through `EXAMPLE-0007`. To make an XLSX sample, copy the
common metadata into Document and these eight text pairs into SerialNumbers.

Case, Unicode and leading zeros remain exact. Whitespace/NUL are rejected rather than
trimmed. Numeric serial cells, formulas, merged input cells, extra worksheets, macros,
external content and malformed ZIP/XML are rejected. Diagnostics include field and
available sheet, row and column coordinates. CSV expressions remain literal strings.

The existing bounded transports enforce 5 MiB uploads, 1000 assets, 4096-character cells,
64 CSV columns, 200 ZIP members, 50 MiB expanded XML/ZIP content and 200000 XML elements.
Raw files are not retained. Existing parser unit tests also cover shared transport limits.

## Eligibility and transactional boundary

A frozen candidate and exact set-based serial lookup supply preview facts. The adapter
uses an isolated READ ONLY, REPEATABLE READ snapshot and reads persisted receipt-line
classification/tracking, receipt date, latest transfer date and previous write-off.
Catalog defaults and names do not establish receipt-time classification.

Every unit must have explicit ACTIVE lifecycle, SERIALIZED receipt tracking and INVENTORY
receipt classification. FIXED_ASSET, unknown classification/lifecycle, previous write-off,
missing/ambiguous/duplicate identities and contradictory origin/history fail the batch.
Acceptance cannot precede authorization, receipt or latest transfer. Same-day operations
remain supported; mixed custody requires no artificial common source/destination.

Preview invokes the existing pure M5A domain operation on immutable snapshots and writes
nothing. Confirmation resolves again, compares technical identity, and invokes
`write_off_assets(WriteOffAssets(...), fresh_uow)` as the sole property write path.
That unchanged service locks UUIDs in order, refreshes after lock waits, rechecks the
original document and eligibility, stages all changes and commits once. A failing unit
rejects everything. Existing document PK, FK and unique unit membership constraints
remain authoritative. No migration, integration event or reporting SQL change is added.

Receipt links, serial identities, stable asset IDs, initial/current custody and movement
history survive write-off. Inventory Explorer reconstructs ACTIVE before acceptance and
WRITTEN_OFF on/after acceptance. Last recorded custody is historical evidence, not proof
of present physical possession. Result values sum verified receipt acquisition prices
separately per currency with Decimal precision; they are neither a journal balance nor
an automatically calculated expense. No financial postings are generated.

## Identity, retries and staging

The private preview UUID differs from the application-generated write-off UUID and the
paper act number. The write-off UUID remains stable for every retry of that preview.

- One process claims concurrent confirmation of a preview under the established memory
  lock. Consumed previews return a conflict with a result link; no second command runs.
- After an uncertain commit, a fresh read reconciles the same UUID against act number,
  act date, authorization reference/date, effective date and complete unordered asset set.
  Exact matches are `already_registered`; any mismatch is a conflict. Current lifecycle
  and custody do not determine successful retry recognition.
- If reconciliation cannot establish an outcome, retry the retained preview. It preserves
  its candidate and technical UUID. Atomic failures roll back all changes.
- An identical fresh upload gets a new UUID and fails because its assets are already
  written off. No file fingerprint or durable cross-restart deduplication is introduced.
- Paper numbers are not globally unique. A new UUID with repeated paper facts and disjoint
  assets cannot reliably be identified as duplicate without an approved identity policy.

`MemoryWriteOffPreviewStore` reuses the existing bounded staging lifecycle: 30-minute TTL,
32 previews, four per session, 32 MiB estimated memory including resolved snapshots.
Completed previews release candidate/snapshots; uncertain outcomes retain candidate.
Receipt, transfer and write-off stores have independent process-local budgets. Expiry,
restart or process loss removes staging; inspect history before re-uploading uncertain work.

The existing local-only, single-process, debug-off security contract remains: CSRF on
writes, session ownership, safe Jinja escaping, no-store responses, CSP and redacted
access logs. Cookies hold random session/CSRF tokens, never documents or serials. No
production authentication or RBAC is supplied. `create_app(..., write_offs=...)` accepts
an independent coordinator; default assembly shares the existing session factory.

## Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/write-offs/import/new` | Upload form |
| POST | `/write-offs/import` | Parse, resolve and preview |
| GET | `/write-offs/import/<preview_id>` | Validation and resolved units |
| POST | `/write-offs/import/<preview_id>/confirm` | Revalidate and register atomically |
| GET | `/write-offs/import/<preview_id>/result` | Result and asset histories |
| POST | `/write-offs/import/<preview_id>/discard` | Release preview |
| GET | `/write-offs/import/template/<xlsx-or-csv>` | Blank versioned template |

## Manual browser acceptance

1. In an explicitly selected disposable synthetic database, follow the receipt guide:
   import 27 units at UAH 14500, namespace EXAMPLE-ISSUER, values EXAMPLE-0000 to EXAMPLE-0026.
2. Follow the transfer guide to transfer the first 12. Verify 27 ACTIVE / UAH 391500.
3. Download the write-off template, fill it as above, or upload the synthetic CSV.
   Inspect all eight serials, nomenclature, receipt classification, authorization and dates.
   No write-off exists before confirmation.
4. Confirm. Inspect the technical UUID, act number/date of accounting acceptance, count,
   original acquisition value and linked histories. Verify receipt, transfer and write-off
   events and stable identities. No financial journal is created.
5. At 2026-02-10 verify 19 ACTIVE / UAH 275500 and 8 WRITTEN_OFF / UAH 116000. At
   2026-02-09 all 27 remain ACTIVE / UAH 391500. Serials remain searchable after write-off.
6. Reconfirm the consumed preview and re-upload the identical file: no duplicate records.
7. Mix an already written-off unit with an eligible unit; reject the whole batch. Repeat
   with a separately registered synthetic fixed-asset receipt. No fixed asset is disposed.
8. Exercise missing/duplicate serials, numeric/formula cells, malformed dates, stale previews,
   different sessions, CSRF, discard and expiry. Errors must remain safe and Ukrainian.

Automated Flask clients exercise the complete flow; visual browser acceptance remains
manual. PostgreSQL tests cover overlapping UUIDs, transfer/write-off waits in both orders,
rollback after flush, exact set-based read-only lookup, history and valuation. Existing M5A
constraint and service tests remain part of regression coverage.

## Scope and handoff

Head remains `0010`; old migrations and accounting entities are unchanged. Tests create
and drop only random databases on the explicit isolated local test service. Never apply
migrations to shared databases as part of this workflow. See the M7.3 guide for the existing
0010 receipt/catalog unit precondition and separately authorized operator review.

The Drone-first strategy uses a reusable property/financial accounting core. Property
ACTIVE/WRITTEN_OFF lifecycle is separate from future UAV technical status. No flight hours,
repair events, maintenance schedules, component replacement or servicing alerts are added.
Fixed-asset disposal, financial correspondence, legal supporting-document verification,
reversal/corrections, closing periods and paper-document identity need separate approved
rules. Document references record an accepted package; they do not verify signatures.


## Verification commands

```bash
docker compose -f compose.test.yaml up -d --wait
TEST_DATABASE_URL=postgresql+psycopg://maas_test:test_password@127.0.0.1:55432/maas_ua_test uv run pytest -q --tb=short
uv run ruff check .
uv run ruff format --check .
git diff --check
```

The full suite includes Alembic head/metadata consistency and existing PostgreSQL
write-off membership constraints. Browser visual acceptance has not been performed.
