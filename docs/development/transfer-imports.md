# Local serialized-asset transfer import (M7.3)

Use synthetic data in one local application process at migration head `0010`.
Start the existing app as described in [receipt imports](receipt-imports.md), then
open <http://127.0.0.1:5000/transfers/import/new>. Navigation also links this page.
No startup connection, migration, catalog creation or shared-database change occurs.

## File contract: serialized_transfer_v1

Download blank XLSX/CSV templates from the upload page. One file describes exactly
one transfer document, with one common source, destination and effective date.
Mixed-source documents and extra worksheets/columns/metadata are rejected; the
importer never splits a file. Custody references remain opaque exact text, not
new location or personnel catalogs. Transfer import supports all serialized assets
eligible under the existing transfer command; it adds no classification rule.

XLSX has exactly two worksheets:

- `Document`: `A1=field`, `B1=value`, followed by these unique required field/value rows.
- `SerialNumbers`: `A1=serial_namespace`, `B1=serial_value`, one exact text pair per
  physical unit. Hidden rows participate; empty rows are ignored. No row-number field.

| Document field | Synthetic example |
| --- | --- |
| `template_version` | `serialized_transfer_v1` |
| `document_number` | `EXAMPLE-TRANSFER` |
| `document_date` | `2026-02-01` |
| `effective_date` | `2026-02-01` |
| `source_location_ref` | `EXAMPLE-LOCATION` |
| `source_responsible_party_ref` | `EXAMPLE-PARTY` |
| `destination_location_ref` | `EXAMPLE-DESTINATION` |
| `destination_responsible_party_ref` | `EXAMPLE-DESTINATION-PARTY` |

Use serial namespace `EXAMPLE-ISSUER` and text values `EXAMPLE-0000` through
`EXAMPLE-0011` for the 12-unit walkthrough. The upload page templates contain no UUID
fields. Dates accept exact ISO `YYYY-MM-DD` or native Excel dates at midnight.
Document and effective dates are separate; registration gets its own aware timestamp.

CSV is UTF-8, optionally with BOM, and explicitly comma- or semicolon-delimited.
Its header has all eight Document fields plus `serial_namespace,serial_value`.
Repeat every metadata value on each asset row; no fill-down, receipt `line_key`,
quantity, price, nomenclature override or analytics columns. Metadata must agree
on every row. Quoted CSV is supported; diagnostics use physical line coordinates.
A populated [synthetic CSV example](../examples/serialized_transfer_v1.csv) matches
this walkthrough. To create XLSX, copy its eight common metadata values into the
blank Document template and its serial columns into SerialNumbers as text.

Both transports share receipt-import ZIP/XML/row/cell safeguards: 5 MiB upload,
1000 assets, 4096-character cells, 64 CSV columns, 200 ZIP members, 50 MiB expanded
ZIP and 200000 XML elements. XLSX permits two columns and 64 Document metadata rows.
Macro packages, external content, formulas, merged cells, extra sheets, malformed
coordinates and numeric serial/reference cells are rejected. CSV cells are literal
text; they are never evaluated as spreadsheet expressions. Case, leading zeros,
Unicode and namespace are preserved; outer whitespace/NUL are rejected, never trimmed.
Raw uploads are not retained after parsing.

## Validation and write boundary

The parser creates a frozen `TransferImportCandidate`. The read adapter resolves all
exact serial pairs with one set-based asset/catalog query, alongside retrieval of the
retained movement UUID's original document and full membership. Its transaction is
REPEATABLE READ, READ ONLY, independent of command sessions. It never updates custody.

Preview reports every duplicated/missing/ambiguous serial row and resolved nomenclature.
Eligibility uses the existing pure transfer domain operation on immutable snapshots:
ACTIVE lifecycle, known current custody matching the explicit source, distinct source
and destination, and a date no earlier than each unit's latest effective date. A single
invalid unit blocks the complete document. Written-off and legacy unknown units remain
ineligible. No future-date, financial-period or paper-number uniqueness rule is added.

Confirmation reloads staged input, resolves again, checks original technical identity,
and calls `transfer_assets(TransferAssets(...), fresh_uow)`. That unchanged service locks
selected UUIDs in sorted order, refreshes their state after waits, rechecks movement
identity, validates the complete batch, saves custody and documentary movement together,
and explicitly commits once. Failures roll back everything; database PK/FKs and serial
uniqueness remain authoritative. There is no alternative property write path or financial
journal generation. Acquisition values and receipt links remain unchanged. M4 reporting
SQL is unchanged, with historical custody and values covered by acceptance tests.

## Identity, retries and re-uploads

Three values are different: private temporary preview UUID, application-generated
movement UUID (stored as the staging `registration_id`), and paper document number.
The movement UUID stays fixed across every retry of that preview. Paper numbers are
not unique and do not prove that two transfers are the same business event.

- Concurrent confirmations of one preview are claimed under the existing memory lock.
  One may proceed; others receive conflict. Consumed previews expose their result and
  cannot issue another command.
- An uncertain commit triggers a fresh read. Only the same movement UUID with equal
  original number, dates, source, destination, and complete exact asset membership is
  reported already registered. Order of file rows is not business identity.
- Later transfers/write-offs do not invalidate a successful retry: original facts,
  rather than current custody, establish the match. A conflicting UUID is rejected.
- If the result remains unknown, the preview becomes retryable with the same UUID.
  Retry that preview rather than allocating a replacement identity.
- A fresh upload receives a new movement UUID. Immediately re-uploading a successful
  file is rejected because the source no longer matches, without duplicate history.
  If the assets have subsequently returned to the source and chronology permits it,
  the same file can represent another transfer. There is **no durable file fingerprint,
  paper-document deduplication, or cross-restart upload recognition**. The system does
  not guess that repeated paper facts identify the same event. Restart/expiry loses
  preview identity; inspect Inventory Explorer before re-uploading after uncertainty.

## Staging, security and dependency injection

`MemoryTransferPreviewStore` extends the existing `MemoryPreviewStore` lifecycle,
adding advisory resolved-asset snapshots and accounting for them in its memory budget.
Each import store permits 32 previews, four per session, 32 MiB estimated memory and
30-minute expiry, with timer and access checks. Completed outcomes release input and
resolved snapshots; retries retain parsed input. In-flight confirmations are retained
until completion; results may expire immediately if confirmation outlives the TTL.
Receipt and transfer stores have separate capacity budgets in the same process.

Session ownership and CSRF apply to every write. Cookies hold random tokens only.
Jinja escapes text; responses are no-store, no-referrer and CSP-protected; request
logs redact URLs. Debug, nonloopback clients, untrusted hosts and multiprocess WSGI
requests are rejected. This milestone restores the documented debug-off default and
debug-request guard, which were missing from the actual factory despite existing tests.
There is no production authentication, RBAC or multiworker guarantee.

`create_app(config=None, *, imports=None, inventory=None, transfers=None)` accepts
independent test dependencies. Default production adapters share the existing factory.
When only receipt imports are injected, transfer writes fail safely as unconfigured;
they do not create an implicit database factory.

## Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/transfers/import/new` | Upload form |
| POST | `/transfers/import` | Parse and preview |
| GET | `/transfers/import/<preview_id>` | Validation and resolved assets |
| POST | `/transfers/import/<preview_id>/confirm` | Fresh validation and atomic service call |
| GET | `/transfers/import/<preview_id>/result` | Result and Inventory Explorer links |
| POST | `/transfers/import/<preview_id>/discard` | Release staging |
| GET | `/transfers/import/template/<xlsx-or-csv>` | Blank versioned template |

## Manual browser acceptance

1. In an explicitly provisioned disposable synthetic development database, follow the
   receipt guide to import 27 assets at UAH 14500 each with the source references above.
2. In Inventory Explorer at `2026-01-31`, verify 27 units and UAH 391500 acquisition value.
3. Upload the synthetic 12-unit CSV or fill the downloaded XLSX as above. Inspect all
   serials, nomenclature, source, destination and dates; preview must write nothing.
4. Confirm once. Follow asset links; verify the receipt, original price, transfer history
   and custody at `2026-01-31` versus `2026-02-01`.
5. At `2026-02-01`, source grouping is 15 and destination grouping is 12. Combined count
   remains 27 and acquisition value remains UAH 391500. This is not carrying value.
6. Reconfirm the consumed preview: conflict, with its result available. Re-upload the
   same file: ineligible-source errors, no second movement.
7. Using the existing write-off service in the synthetic database, write off one of the
   remaining source units. Upload a batch containing it and one eligible source unit:
   reject the whole batch and leave the eligible unit unchanged.
8. Exercise wrong source, same destination, duplicate/missing serial, numeric XLSX serial,
   formula, incorrect date, stale preview, cross-session access, discard and expiry.

Automated tests cover both transports, fakes, Flask, PostgreSQL atomic rollback after
flush, overlapping imports, identical technical IDs, transfer/write-off races, uncertain
commits, original membership conflicts, read-only batch query count and historical totals.
Tests create/drop only randomly named databases on the explicit isolated test server.

## Migration handoff

Head remains `0010`; M7.3 changes no table, old migration, reporting SQL or historical fact.
The `0010` precondition refuses receipt/catalog unit mismatches. Before an operator's
separately authorized upgrade on a populated database, review locally with:

```sql
SELECT r.id AS receipt_line_id, r.nomenclature_id,
       r.quantity_unit, n.unit_of_measure
FROM receipt_lines r JOIN nomenclatures n ON n.id = r.nomenclature_id
WHERE r.quantity_unit <> n.unit_of_measure;
```

This implementation does not query shared/development records or claim they match.
If this returns rows, stop and obtain an approved operator decision for those records;
do not rewrite receipt units or reinterpret catalogue units to force the migration.
