# Local serialized-inventory receipt import (M7.1)

This interface is for one local application process and synthetic development data.
It does not authenticate production users or verify accounting authorization.
PostgreSQL remains authoritative; the existing `RegisterInventoryReceipt` service is
the only property write path. No migration, staging table, UPSERT, financial posting,
memorial generation, or editing of registered receipts is introduced.

## Start and use

Configure `DATABASE_URL` explicitly for an already provisioned development database
at migration head. See [the existing setup](postgresql-receipts.md). Schema upgrades
remain an explicit operator action; startup never migrates or creates catalog data.

```bash
uv run flask --app maas_ua.entrypoints.flask_app:create_app --no-debug run --host 127.0.0.1 --no-reload --no-debugger
```

Open <http://127.0.0.1:5000/imports/new>. Select the import type, download its XLSX or
CSV template, enter synthetic data, upload, inspect the diagnostics and every unit,
then explicitly confirm. The result retrieves the document and units from PostgreSQL.
Close the result to release its preview slot. An error in any row rejects the whole
receipt. Correct the file and upload again; individual rows cannot be accepted.

`create_app(config=None, *, imports=None, inventory=None, transfers=None)` supports dependency injection for test
clients and otherwise reuses `make_session_factory()` and `SqlAlchemyUnitOfWork`.
The app owns its factory/engine for its process lifetime; UoWs close their own
sessions. No connection is needed just to construct the application or view uploads.

## Versioned XLSX layout

Only these two standard worksheets are accepted, including when sheets are hidden:

- `Document`: `A1=field`, `B1=value`; subsequent rows contain unique field names in A
  and literal values in B. Field order is flexible. At most 64 metadata rows.
- `SerialNumbers`: `A1=serial_namespace`, `B1=serial_value`; subsequent rows each
  represent one physical unit. Both columns must contain text. Hidden/filtered rows
  participate. Completely empty rows are ignored.

The download preformats identifier fields/columns as text. Keep identifiers as text
when populating the template in other software as well. Formulas, merged input cells,
additional columns/sheets, unknown/duplicate fields, repeated or unordered XML cell
coordinates, and missing required values are rejected. Worksheet dimension claims
do not determine which rows are inspected.

Required Document fields:

| Field | Contract |
| --- | --- |
| `template_version` | Exact `serialized_receipt_v1` |
| `document_number` | Exact nonblank text; not a unique identifier |
| `document_date` | Document date |
| `effective_date` | Explicit receipt-effective date; not derived from document/creation time |
| `source` | Exact nonblank receipt-source reference |
| `nomenclature_code` | Exact code of an existing `SERIALIZED` catalog record; its registered unit is authoritative |
| `line_quantity` | Positive integer, at most 1,000, equal to serialized unit count |
| `unit_price` | Finite nonnegative decimal, including zero |
| `currency` | Exact nonblank currency text; no inferred currency or FX |
| `line_total` | Exact `line_quantity × unit_price`, in the stated currency |
| `accounting_classification` | Exact explicit `INVENTORY` |
| `initial_location_ref` | Exact nonblank initial location reference |
| `initial_responsible_party_ref` | Exact nonblank initial responsible-party reference |

Optional `analytic.<key>` fields contain exact nonblank receipt analytics. Empty
values omit an optional analytic; empty analytic names are invalid. Keys are unique,
with canonical ordering by exact key. These are the existing receipt name/value pairs,
not M6 financial dimensions or an inferred chart of accounts. No authoritative source,
custody, or analytics catalog is claimed; required future code lists need approval.

## CSV layout

CSV is UTF-8 with optional BOM. Explicitly choose comma or semicolon; encoding and
delimiter are never guessed. The header contains every required Document field above,
`line_key`, `serial_namespace`, `serial_value`, and optional `analytic.<key>` columns.
Repeat all document/line facts on each asset row. `line_key` is a text grouping marker,
not a technical UUID or a new domain identifier. Exactly one distinct line key and
one homogeneous group are accepted; multiple lines are rejected, never split or merged.
No fill-down is performed. Row widths must match the header. Quoted CSV values are
supported; diagnostics use physical CSV line numbers, including quoted multiline fields.

Both transports produce the same frozen typed candidate and shared business validator.
M7.2 derives the receipt quantity unit from this existing catalog record. The file has
no unit override: aliases and implicit conversions are not supported. `pcs`, `PCS`,
`шт` and approved set codes remain distinct; serialized tracking does not pick a label.
Duplicate serial pairs identify every implicated row. Case, leading zeros, Unicode,
namespaces, nomenclature codes and other text references are preserved. Outer whitespace
and NUL are rejected rather than stripped. No catalog records or serials are invented.

## Exact monetary and date import policy

The XLSX adapter reads numeric money from the original XML `<v>` decimal token. It
does not construct Decimal from openpyxl's binary floating-point cell value. Numeric
money permits at most **15 significant decimal digits**; trailing coefficient zeros
do not add precision. Decimal text in either format permits **38 significant digits**.
Both the stored Decimal exponent and adjusted exponent must be within `[-100, 100]`.
Money tokens are limited to 128 characters, use a dot separator and may use decimal
scientific notation. Grouping separators, currency symbols, NaN, Infinity and negatives
are rejected. Zero is valid. No rounding, scale quantization or balancing adjustment
occurs. These are transport/resource limits, not new currency/accounting rules.

For example, numeric `0.1` imports as exact Decimal `0.1`; numeric
`0.30000000000000004` is rejected. Use text for precision beyond 15 significant digits.
The importer cannot recover digits Excel already discarded before saving a workbook.
Serial and reference identifiers always require text, regardless of monetary support.

Dates accept exact `YYYY-MM-DD` text or native Excel dates/midnight date cells.
Ambiguous locale strings and non-midnight timestamps are rejected. Formula cells in
imported fields are rejected even when a cached result exists. Nothing calculates
formulas or accesses external content. No new date-order, closed-period or future-date
business policy is imposed on the existing receipt command.

## Transactions, duplicate recognition and uncertainty

Preview parses/validates and reads PostgreSQL in a separate REPEATABLE READ, READ ONLY
snapshot. It performs no property/financial writes. Confirmation reads fresh facts,
reuses domain validation, and calls `register_inventory_receipt()` with a fresh UoW.
The command retains its existing catalog check, exact serial prechecks, authoritative
database uniqueness, one explicit commit, and whole-document rollback.

Two UUIDs are distinct: a temporary preview UUID locates private UI state, while an
application-issued registration UUID becomes the receipt UUID for every attempt of
that preview. Remaining line/unit UUIDs may change between rolled-back attempts;
atomic persistence ensures that no successful receipt contains a partial allocation.
No UUID is requested from an accountant. Random preview UUIDs are not business identity.

Before a write, matching compares document number, both dates, source, the sole line's
catalog/quantity/price/currency/classification/analytics, the complete exact serial set,
and every original initial-custody assignment. Mutable current custody, lifecycle,
last movement dates, registration timestamp and generated UUIDs do not decide matching.
Later transfers or write-offs therefore do not turn an exact original into a conflict.

- Matching original facts return **already registered** with the existing receipt UUID.
- The retained registration UUID with different original facts is a conflict.
- Any existing serial with different original receipt facts rejects the whole upload.
- Same-preview concurrent confirmation is claimed atomically in memory; another caller
  receives conflict/in-progress. Consumed previews cannot run another command.
- Concurrent independent uploads still rely on PostgreSQL receipt/serial uniqueness.
  A losing transaction rolls back and then rereads original facts to report a matching
  registration or a real conflict.
- An exception/lost response after commit triggers a fresh reconciliation read. If the
  database cannot establish the outcome, the preview remains retryable with the same
  registration UUID. Never assume success or allocate a fresh identity blindly.

Across restart there is **no persisted preview, upload identity or import history**.
Re-upload can recognize an existing receipt from a complete matching original serial
set and facts; otherwise database serial conflicts prevent duplicate units. This is
not permanent upload idempotency or paper-document identification. Document numbers
remain nonunique. Disjoint serial identities and a repeated paper number can register
a distinct receipt under the existing policy. Approved paper-document identity,
namespace issuance, corrections, and production authority remain separate decisions.

## Staging and security limits

Previews are private to a signed browser session and remain only in server memory.
Cookies contain random session/CSRF tokens, never worksheet data. CSRF is checked on
every POST, values are HTML-escaped, and responses prohibit caching and referrer leakage.
The local app rejects nonloopback clients, untrusted hosts, debug mode, `WEB_CONCURRENCY`
other than 1, and multiprocess WSGI requests. Do not deploy with multiple workers.
Approved production authentication/authorization and data handling are still required.

Previews/results expire after 30 minutes, with daemon expiry timers and access-time
checks. In-flight confirmation retains its state until it finishes; after expiry its
result may require re-upload to recover. The process allows 32 retained previews,
at most 4 per browser session, with a 32 MiB estimated staging budget. Raw files are
not retained after parsing; temporary multipart streams are closed. Completed outcomes
release their candidate data immediately. Discard/close and expiry release their slots.

Limits: 5 MiB file, 1,000 asset rows, 64 CSV columns, 128-character field names,
4,096-character text cells, 200 ZIP members, 50 MiB expanded XLSX, and 200,000 XML
elements. Compressed-package sizes are checked before openpyxl; XML entity/DTD attacks,
macro-enabled packages, embedded object paths, external links, encrypted/invalid files
and unsupported formats are rejected. No workbook content executes. Errors and logs
never interpolate worksheet values, filenames, serials or underlying database errors.

## Synthetic 27-unit walkthrough

Using only an isolated development database with synthetic records, register the
synthetic catalog once through the existing UoW:

```python
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.bootstrap import make_session_factory
from maas_ua.domain.receipts import Nomenclature, TrackingPolicy

factory = make_session_factory()
with SqlAlchemyUnitOfWork(factory) as uow:
    if uow.nomenclatures.get_by_code("EXAMPLE-FPV") is None:
        uow.nomenclatures.add(
            Nomenclature(
                "EXAMPLE-ID",
                TrackingPolicy.SERIALIZED,
                code="EXAMPLE-FPV",
                name="Example",
                unit_of_measure="pcs",
            )
        )
        uow.commit()
```

Fill Document with `EXAMPLE-125`, dates `2026-01-15` / `2026-01-16`, source
`EXAMPLE-SOURCE`, nomenclature `EXAMPLE-FPV`, quantity `27`, price `14500`,
currency `UAH`, total `391500`, classification `INVENTORY`, location
`EXAMPLE-LOCATION`, and responsible party `EXAMPLE-PARTY`. Keep the version field.
In SerialNumbers enter namespace `EXAMPLE-ISSUER` and values `EXAMPLE-0000` through
`EXAMPLE-0026`. All identifiers must be text. Preview should show 27 units and
UAH 391500; confirmation persists one document, one line and 27 linked AssetUnits.
Re-uploading returns the same original receipt. Changing its price/total or source
while retaining those serials produces conflicts with no new property records.

Automated tests generate synthetic files in memory. Parser, fake-UoW, Flask-client
and real PostgreSQL tests cover this flow, invalid documents, exact identities,
precision, cross-session isolation, concurrency, rollback and uncertain commits.

After confirmation, each asset UUID links to its read-only inventory card. Navigation
also opens exact serial search and historical balances. See the
[Inventory Explorer guide](inventory-explorer.md) for the 27/12/8 acceptance scenario.
