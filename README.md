# MAAS-UA

**Military Asset Accounting System — Ukraine**

MAAS-UA — UAV Asset & Lifecycle Management

An open-source-oriented Python project for integrated property accounting, financial traceability, and future technical lifecycle management of unmanned aircraft systems.

Built with a reusable accounting core and a Drone-first product strategy.

> **Status (2026-11-17):** M1–M5A, M6.1–M6.3, M7.1 receipt import, M7.2 read-only Inventory Explorer, M7.3 bulk transfer import, and M7.4 bulk inventory write-off import are implemented. The local Flask interface supports exact serial search, historical custody, document history, quantity and acquisition valuation. Registered nomenclature units replace the literal `pcs` restriction. Property operations, financial posting, and memorial register assignment remain separate. Production access is not implemented.
>
> **Data handling:** Use synthetic records in code, fixtures, prompts, CI, documentation, logs, and public discussions. Do not publish real military inventory, serial numbers, unit identities, custody/location data, accounting documents, or operational details. Follow applicable authorization and data-handling requirements.

## Purpose

The key question MAAS-UA must eventually answer is: **What property makes up an accounting balance as of a specified date; where is each item, who is responsible for it, what was its acquisition source and value, and which documents explain its history?**

The system distinguishes property/custody accounting from financial accounting. A physical transfer need not change total carrying value; depreciation need not change physical location. Do not assume that a transfer is a write-off or that property movements automatically generate accounting postings.

## Current scope and capabilities

| Capability | Status |
| --- | --- |
| Register receipt with a homogeneous receipt line and individually tracked units | Implemented (M1) |
| Unit and service-layer tests with fake infrastructure | Implemented (M1–M4) |
| PostgreSQL persistence, SQLAlchemy repository/UoW, Alembic integration | Implemented (M2) |
| Persistent `Nomenclature` and receipt references | Implemented (M2.1) |
| Document-backed transfer/movement of serial-tracked units | Implemented (M3) |
| Exact serial lookup, provenance/history, historical custody, quantity and acquisition valuation | Implemented (M4); incomplete history is explicit |
| Documented write-off of serialized inventory | Implemented (M5A); excludes fixed assets |
| Reversal/corrections and legacy classification onboarding | Planned; business rules to confirm |
| Approved chart versions, official/local accounts, typed dimensions, audited supersession | Implemented (M6.1) |
| Manually authorized immutable balanced financial journals | Implemented (M6.2) |
| Primary memorial registers, membership, finalization and canonical turnover queries | Implemented (M6.3) |
| Fixed-asset amortization, automated postings, reconciliation | Planned |
| Local Flask XLSX/CSV serialized-inventory receipt import | Implemented (M7.1); preview, confirmation, original-fact duplicate recognition |
| Local Ukrainian Inventory Explorer with history, historical balances and acquisition valuation | Implemented (M7.2); explicit incomplete-data diagnostics |
| Local Ukrainian XLSX/CSV bulk serialized-asset transfer import | Implemented (M7.3); exact serial resolution, preview, atomic confirmation and safe retries |
| Local Ukrainian XLSX/CSV bulk serialized-inventory write-off import | Implemented (M7.4); M5A eligibility, atomic confirmation and safe retries |
| Exports and financial reporting UI | Planned (M7+) |
| UAV technical lifecycle: flight hours, repair, maintenance, component replacement and alerts | Future work; separate from property lifecycle |

Implemented milestones do **not** imply production authorization, legal accounting compliance, or a complete financial ledger.

## Stack

- Python, managed by **uv** (`pyproject.toml`, `uv.lock`)
- Flask and Jinja2 for local-development receipt import
- SQLAlchemy 2.x, PostgreSQL, and Alembic for persistence
- pytest: unit, service-layer, and PostgreSQL integration tests
- Ruff for lint/format checks
- XLSX via openpyxl + defusedxml; CSV via the Python standard library
- VS Code with the Codex extension for local development

Development typically runs Python locally and PostgreSQL in Docker. Refer to the repository's existing Compose/env setup rather than creating a second one.

The optional Hetzner deployment workflow and its loopback-only access and
migration requirements are documented in
[the deployment guide](docs/development/deploy-hetzner.md).

## Architecture

A **pragmatic DDD / Ports & Adapters modular monolith**, inspired by *Architecture Patterns with Python* (Percival & Gregory):

```text
  Flask / CLI / Excel entry points
                |
          Application services
       Commands / Queries / UoW
                |
       Pure Python domain model
                |
    Ports (repository and UoW contracts)
                |
     SQLAlchemy / PostgreSQL adapters

  Read-side reporting queries -> DTOs -> caller
```

### Design rules

1. Keep domain rules independent of Flask, SQLAlchemy, and PostgreSQL.
2. Model business workflows as commands/use cases, not arbitrary row updates.
3. Use repositories and one Unit of Work per business transaction; do not commit from repositories.
4. Keep financial values precise (`Decimal` / suitable PostgreSQL `NUMERIC`, explicit rounding rules).
5. Preserve original receipt provenance, serial identity, and historical movements; never delete an asset from the receipt because it was transferred.
6. Use PostgreSQL constraints and transaction/concurrency tests for invariants that fake repositories cannot guarantee.
7. Read-side projections may use direct SQLAlchemy Core/SQL and lightweight DTOs; **do not require loading domain aggregates to report thousands of assets**.
8. Do not add microservices, message brokers, Event Sourcing, or a separate reporting database without demonstrated need.
9. Document the difference between implemented accounting behavior and rules that still require regulatory/business validation.

### Domain vocabulary

| Concept | Role |
| --- | --- |
| `Nomenclature` | Catalog definition of a property type/model; separate from accounting funding/program dimensions |
| `ReceiptDocument` | Source document for property receipt |
| `ReceiptLine` | Homogeneous receipt line: nomenclature, quantity, unit cost, provenance/analytics as applicable |
| `AssetUnit` | One individually identified physical unit, with immutable system ID and business serial number |
| `MovementDocument` / `MovementLine` | Document-backed movement of one or more tracked units and its individual records |
| `CustodyAssignment` | Current location/responsibility reference, where supported by the implementation |
| `Money`, identifiers, dates | Domain values; concrete types follow the codebase |

A homogeneous receipt line of 27 FPV drones has 27 `AssetUnit` records, each pointing to the origin line; the unit price is shared by that origin line. A serial is a business identifier, not necessarily a globally unique database primary key. The effective uniqueness scope must be documented and enforced.

**Tracking modes:** individual serial tracking is used for FPV drones and reusable БпАК. Some spare parts or consumables may later require quantity-only accounting. Do not silently treat every nomenclature as serialized.

## Milestone roadmap

- **M1 — Implemented:** receipt registration domain, validations, unit and fake-UoW/service tests.
- **M2 — Implemented:** SQLAlchemy/PostgreSQL repository and transaction persistence, Alembic, integration tests.
- **M2.1 — Implemented:** nomenclature entity and persistence, receipt-line linkage.
- **M3 — Implemented:** transfer command, movement document/history, custody changes, tests.
- **M4 — Implemented:** immutable initial custody, transfer order, traceability queries, historical quantity/acquisition valuation and PostgreSQL tests.
- **M5A — Implemented:** documented serialized-inventory write-off and historical accounting membership.
- **M5+ — Proposed:** correction/reversal and legacy onboarding policies.
- **M6.1 — Implemented:** immutable accounting reference snapshots, typed dimensions and controlled supersession.
- **M6.2 — Implemented:** explicit ledgers, permitted periods, document attribution, and manually authorized immutable balanced journals.
- **M6.3 — Implemented:** explicitly identified memorial registers, unique whole-entry primary membership, finalization, and turnover/assignment queries.
- **M6.4+ — Proposed:** form rendering, broader financial reports, automated postings and approved closing/correction rules.
- **M7.1 — Implemented:** local single-process Flask receipt uploads, versioned two-sheet XLSX / flat CSV, typed validation, preview, explicit atomic confirmation, results and safe retries.
- **M7.2 — Implemented:** exact registered units of measure, Ukrainian serial lookup, asset history, historical custody, balances and acquisition valuation through existing readers.
- **M7.3 — Implemented:** bulk transfer XLSX/CSV, batch exact serial resolution, Ukrainian preview/confirmation, stable UUID retries and atomic existing service.
- **M7.4 — Implemented:** bulk serialized-INVENTORY write-off XLSX/CSV, M5A validation, Ukrainian preview/confirmation, immutable retry reconciliation and preserved history.
- **M7+ — Proposed:** other import types, exports, broader reports and approved production access.

The roadmap is a planning tool, not an assertion of final approved accounting functionality.

## M4 scope: Inventory, Traceability & Balances

Immutable query inputs and result DTOs live in `service_layer/inventory.py`; `adapters/inventory.py` implements `InventoryReader` with SQLAlchemy Core:

- `GetAssetBySerialNumber`: find a tracked unit under the agreed serial uniqueness policy.
- `GetAssetHistory`: receipt and chronologically ordered confirmed movement records, with document references.
- `GetAssetPositionAsOf`: resolve the unit's custody/location **as of a business date**, based on receipt and valid movement history.
- `GetInventoryBalance`: group units by nomenclature, source/receipt, custody/location and agreed accounting dimensions for a date.
- `GetInventoryValuation`: calculate acquisition-value totals using receipt-line prices with `Decimal`, excluding units from their write-off accounting effective date; no depreciation.

`as_of` means **end of the business date**, including receipts/transfers with `effective_date <= as_of`. Document date and `created_at` are displayed evidence, not reporting cutoffs. Late registration can change earlier reports; reporting does not reproduce what the system knew at an earlier time. Before receipt, position returns `registered=False`; a missing asset raises `AssetNotFound`.

New receipts preserve `AssetUnit.initial_custody` separately from current custody. Transfers use their stored destinations and a persisted `registration_order`, allocated at INSERT after selected assets have been locked in UUID order. Sequence gaps are allowed; its order is meaningful for overlapping assets, not a global business-date or commit-time ordering. UUID and creation time never decide same-day chronology. Each history day exposes `ordered`; legacy same-day ties are explicitly unordered.

`GetInventoryBalance` and `GetInventoryValuation` share the position query. Filters support nomenclature ID, receipt UUID, source, currency, receipt analytics, historical location and responsible party. Every group retains original receipt/line, source, nomenclature, exact price, currency, unit and receipt analytics. `group_by` selects custody splits: `("location", "custodian")` by default, either dimension, or `()` to combine custody within each origin. Catalog classification defaults are not historical accounting dimensions.

Reports expose `complete`, `groups`, `diagnostics`, `candidate_count` and `known_acquisition_totals`. Incomplete reports suppress groups; accessing verified `count` or `totals` raises `IncompleteInventory`. Known diagnostic values are not verified balances. Unknown initial custody/lifecycle, ambiguous movement order, broken custody chains and missing provenance cannot disappear from accepted totals. Static filters narrow the scope first; known historical positions outside custody filters are excluded. An unresolved position might match a custody filter and remains diagnostic. Empty valid reports have zero count and no currency totals. Asset lookup/history retain known information with completeness indicators.

Amounts use each asset's original receipt-line price, PostgreSQL `NUMERIC` and `Decimal`, with no new rounding. Currency totals remain separate. These are **acquisition values**, not depreciation-adjusted carrying values or financial ledger balances.

Each read operation owns a PostgreSQL **REPEATABLE READ, READ ONLY** transaction. Multiple SELECTs share one snapshot; closing the connection rolls back without commit. There is no ORM autoflush, aggregate loading, per-asset reporting loop, or command-UoW change. Normal reporting uses three aggregate SELECTs independent of asset count.

Example using the existing configured engine:

```python
from datetime import date

from maas_ua.adapters.inventory import SqlAlchemyInventoryReader
from maas_ua.bootstrap import make_session_factory
from maas_ua.service_layer.inventory import GetInventoryValuation, get_inventory_valuation

factory = make_session_factory()  # Explicit DATABASE_URL; does not migrate.
engine = factory.kw["bind"]
try:
    report = get_inventory_valuation(GetInventoryValuation(as_of=date(2026, 2, 10)), SqlAlchemyInventoryReader(engine))
    if report.complete:
        verified_count, verified_currency_totals = report.count, report.totals
    else:
        issues = report.diagnostics  # No verified balance is available.
finally:
    engine.dispose()
```

Migration `0005` follows `0004` and is **forward-only**: downgrade refuses to erase audit facts. It adds initial custody and order without filling legacy records from current state, timestamps, UUIDs or the first movement. Old custody/order remain unknown; no onboarding/repair workflow is supplied. Apply it to a reviewed deployment target through the existing Alembic procedure; tests migrate only disposable databases. M4 itself added no lifecycle transition; M5A supplies the documented write-off extension below. Draft/cancel/reversal, depreciation and posting workflows remain outside scope. Read guarantees assume records are created through the supported commands, not arbitrary administrative SQL.

### Acceptance scenario (synthetic)

1. Receive 27 serialized units under one line at UAH 14,500 each (UAH 391,500 total).
2. Transfer 12 of those units to a second custodian, leaving 15 with the original custodian.
3. The as-of report **before** the transfer shows all 27 with their original custodian; **after** transfer, 15 and 12 respectively.
4. Combined total remains 27 units and UAH 391,500; no value changes solely because of an internal transfer.
5. Selecting one transferred serial reveals its receipt, acquisition source, unit cost, transfer document, and custody history.

These are illustrative assumptions, not real military records; dates, custody rules, and registration status must be explicit in the tests.

## M5A: Documented inventory write-off

`WriteOffAssets` in `service_layer/write_offs.py` registers one immutable
`WriteOffDocument` and its per-unit `WriteOffLine` references. It transitions only
explicitly active, serialized **inventory** to `WRITTEN_OFF`. Receipt lines now
have an explicit `AccountingClassification` snapshot (`INVENTORY`, `FIXED_ASSET`,
or unknown). Pass it through `RegisterInventoryReceipt.accounting_classification`;
it is never inferred from catalog defaults, names, codes or an allowlist. Unknown
and fixed-asset classifications are ineligible for M5A write-off. Existing receipt
callers remain compatible; omitting classification records unknown.

The caller supplies a document UUID reused on retries, act number/date,
`authorization_reference`, `authorization_date`, explicit `effective_date`, and
selected internal asset UUIDs. Duplicate UUIDs are rejected, not silently accepted;
document numbers are not unique. A unit can occur in only one completed write-off.

**The accounting effective date is when the financial-economic service accepts
the verified document package for accounting.** It must be on or after authorization,
receipt and each unit's latest transfer. The act date and aware system `created_at`
remain separate evidence. The date is never copied from physical use/loss, first
unverified receipt of documents, authorization, transfer, or registration time.
The caller supplies already verified evidence; no multi-step approval workflow or
mandatory launch act is introduced.

The command locks all selected units in UUID order, rechecks document identity
after waiting, validates the entire selection, and commits the document and unit
snapshots once. Any failure rolls back all changes. Transfers share these locks
and reject written-off units. Same-day receipt/transfer followed by write-off is
allowed. Custody is preserved as the last recorded assignment, not an assertion
of physical presence; receipt origins, serials, original values and movements remain.

Historical reports default to `InventoryFilter(lifecycle_state=LifecycleState.ACTIVE)`:
units remain included before accounting acceptance and are excluded on that date,
inclusive. Request `LifecycleState.WRITTEN_OFF` for written-off units, or `None` for
both states; groups retain lifecycle separately. Position results expose historical
lifecycle and effective write-off evidence. Serial lookup and history preserve the
write-off document and its authorization fields. Missing/contradictory lifecycle
history remains diagnostic rather than disappearing through a lifecycle filter.
These reports describe **accounting availability, not physical availability**.

The 27-received, 12-transferred, 8-written-off scenario leaves 19 active units:
15 at the original assignment and 4 at the receiving assignment. At the synthetic
UAH 14,500 unit price, active acquisition value becomes UAH 275,500; the eight
retain UAH 116,000 of original acquisition value in historical queries. Currency
totals remain separate, with no new rounding or postings.

Forward-only migration `0006` follows `0005`, adds the nullable receipt classification
snapshot and write-off document/line tables, and extends the lifecycle constraint.
It preserves legacy rows without assigning classifications or synthesizing history.
It has been applied only to disposable test databases during verification. Existing
unknown classifications need a separately approved onboarding/correction process.
Fixed-asset disposal, depreciation, journal entries, reversals, legal evidence
verification, signature/role enforcement, closed periods and Flask UI remain outside
this milestone. See [the confirmed domain rules](docs/architecture/domain-model.md#21-milestone-5a--documented-serialized-inventory-write-off).

## M6.1: Accounting reference data

The Property Ledger and Financial Accounting Ledger share document provenance but
have different responsibilities. M6.1 supplies reference data for the latter;
M6.2 adds the manual journal below. Memorial orders, financial reports and
period-closing commands remain absent. Receipt/transfer/write-off commands retain M1–M5A behavior.

`domain/accounting.py` defines frozen `ChartOfAccounts`, `ChartVersion`,
`AccountDefinition` and `AccountingDimensions`. A chart carries an opaque accounting
entity reference; callers select its UUID explicitly. An approved version contains a
complete immutable tuple of the selected approved definitions, an exact version label,
approval reference, aware creation timestamp, and half-open applicability
`[valid_from, valid_to)`. `None` means an open end. Accounts inherit version validity;
there are no independent account intervals or automatic expiration dates.

`AccountKind.OFFICIAL` and `LOCAL_ANALYTICAL` distinguish official definitions from
local analytical extensions. A local definition must explicitly reference an official
account in its version. Codes such as `1518/03.01` are exact opaque text: no prefix,
slash or suffix is interpreted. Names, hierarchy, mappings, authority references,
`AccountBasis.BALANCE` / `OFF_BALANCE` and posting eligibility are immutable metadata.
Posting eligibility is independent of having children and does not implement a posting
rule. No regulatory definitions or mappings are seeded by migration or startup.

Use `register_chart` and `register_chart_version` from `service_layer/accounting.py`
with the existing fake or SQL UoW. `uow.charts` supports chart/version/account lookup,
version listing and `version_as_of(chart_id, business_date)`; retrieve an exact code
through the returned version's `account_by_code`. No applicable version returns
`None`; an ambiguous result is an error. Account UUIDs remain historical references;
a successor uses new definition UUIDs even when its codes are unchanged.

`SupersedeChartVersion` / `supersede_chart_version` is the only supported applicability
change. Supply a retry-stable transition UUID, predecessor UUID, approved successor
snapshot, actor reference and reason. The service locks the chart, refreshes the
predecessor, checks its open-ended interval and successor identity/start, closes the
predecessor at the successor's exact start, inserts the successor and transition audit,
and commits once. No gap is introduced at this boundary. Failures roll back the closure,
new definitions and audit together. Repeated transition UUIDs are rejected. Closed
predecessors cannot be shortened again; version contents and original starts cannot
be edited. Ordinary registration permits disjoint intervals, including gaps; no general
policy of gap-free chart coverage has been assumed.

`ChartRepository.protected_through(version_id)` now returns the latest posted accounting
effective date for that version across all ledgers. Supersession rejects a cutover on or
before that date. Posting and supersession coordinate through the same chart lock.
Future period closing must extend this protection to closed-period coverage, including
periods without postings; closing is not implemented in M6.2.

Dimensions have optional `FundRef`, `BudgetProgramCode`, `KEKVCode`, `FundingSourceRef`,
`ReportingUnitRef`, `CounterpartyRef` and a local account-definition UUID. Reference values
preserve exact nonblank text; no regulatory code lists or mandatory combinations are
invented. `validate_accounting_dimensions(dimensions, version_id, uow)` checks that a
local reference resolves to a registered `LOCAL_ANALYTICAL` definition in the selected
version. Dimensions have no persistence table or EAV model and do not translate
`ReceiptLine.analytics`, custody, serials or provenance into financial attributes.

Forward-only migration `0007` adds exactly four tables: `accounting_charts`,
`accounting_chart_versions`, `accounting_account_definitions`, and
`accounting_chart_supersessions`. It installs PostgreSQL's `btree_gist` extension for
UUID-plus-date-range exclusion constraints. Deployment requires permission to install
that extension or prior provisioning. Exact code/label uniqueness, same-version FKs
and a GiST exclusion constraint defend concurrent registrations at the database level.
There is no data seeding or modification of property tables or migrations `0001`–`0006`.
Supported APIs provide immutability; unrestricted administrative SQL is outside that
boundary. Domain validation additionally checks cycles, mapping target kinds and hierarchy
consistency. Apply shared-database migrations only through the approved deployment process.

Balance-account double-entry equality is implemented in M6.2; off-balance single-entry
treatment remains separate future behavior. The selected entity, approved
codes/names, local suffix meanings, analytical requirements and future posting policies
remain business-owner inputs. See [the M6.1 domain contract](docs/architecture/domain-model.md#22-milestone-61--accounting-reference-data-and-version-management).

## M6.2: Manually authorized financial journal

`domain/journal.py` defines frozen `AccountingLedger`, `AccountingPeriod`,
`LedgerSourceReference`, `JournalEntry`, `PostingLine` and `PostingSide`. Ledger identity
explicitly selects one chart and its accounting entity. The first use case uses one
registered ledger; persistence supports multiple ledgers without inter-ledger posting.

Use these functions from `service_layer/journal.py`, each with its own UoW:

1. `register_accounting_ledger(ledger, uow)` selects a registered chart.
2. `register_accounting_period(period, uow)` records an explicit bounded `[starts_on,
   ends_before)` interval, posting permission, actor and authorization reference.
   Periods cannot overlap within a ledger; no calendar is generated.
3. `attribute_ledger_source(source, uow)` explicitly attributes an existing receipt or
   write-off to that ledger through typed document FKs. A document has one attribution
   in M6.2; sharing it across ledgers requires a later approved workflow.
4. `post_journal_entry(PostJournalEntry(...), uow)` validates and commits a complete
   immutable financial entry. The command supplies `id`, `ledger_id`, `version_id`,
   `period_id`, stable `posting_instruction_key`, `effective_date`, `currency`, `purpose`,
   `actor_ref`, `authorization_reference`, `lines`, and `source_reference_ids`.
   The service records an aware registration timestamp.

Every entry requires debit and credit lines with strictly positive finite Decimal
amounts and exact equality in one explicit currency. Accounts must be posting-enabled
`BALANCE` definitions from the selected version applicable on the accounting effective
date. Typed dimensions are stored per line, including same-version local analytics.
No mandatory analytics, currency list, rounding, FX or correspondence is inferred.
Receipt `Money` behavior remains unchanged, including zero acquisition prices.

UUID reuse and exact `(ledger_id, posting_instruction_key)` reuse are rejected, rather
than silently replayed. Distinct legitimate components of a source use distinct stable
business instruction keys. One entry may reference several documents, and one source
may support several entries. An instruction key is an application identity contract,
not a regulatory code; callers must reuse it on retries.

Forward-only `0008` adds six financial tables: ledgers, periods, source attributions,
journal headers, posting lines and journal-source memberships. Composite FKs enforce
ledger/chart/version/period/source relationships. Deferred constraint triggers validate
complete child counts, debit/credit equality and posting eligibility at commit. Row
CHECK constraints alone do not enforce journal balance. Immutable expected child counts
prevent late inserts, including additional balanced lines, from extending a journal.
Triggers reject UPDATE, DELETE, TRUNCATE and conflict-update changes to financial history.
They also guard used chart snapshots against changes that invalidate posted history.

Posting locks the chart row, then the period row, refreshes references, stages the
aggregate and commits once. Direct header inserts follow the same lock order. Future
closing must acquire those locks in that order, re-read permission/history and retain
locks through commit; its migration must explicitly replace the current immutable-period
guard. Financial writes and protected chart changes require PostgreSQL `READ COMMITTED`,
matching the configured application default. Snapshot-isolation writes are rejected to
avoid stale validation after lock waits. Read-only inventory reports retain their existing
`REPEATABLE READ` snapshots. Trigger functions fix their search path to prevent temporary
tables from shadowing integrity queries. Deadlocks, lock timeouts and serialization
failures retain whole-command retry semantics; failures roll back the complete journal.

No property operation automatically posts financial entries. Actor/authorization fields
record supplied administrative evidence without verifying legal authority, signatures or
RBAC. Approved required analytics, currency/rounding policy, document-package sufficiency,
instruction-key issuance and source-to-journal date relationships remain policy inputs.
No extra backdating/future-date restriction is imposed beyond the selected period and
chart applicability. Closing/reopening, corrections/reversals, memorial orders, opening
balances, off-balance operations, depreciation, reports and Flask UI remain excluded.
Migration tests use disposable databases only; no shared database is migrated automatically.

## M6.3: Primary memorial-order registers

`domain/memorial_orders.py` defines immutable `MemorialOrderType`, `NumberingScope`,
`MemorialOrderNumber`, `MemorialOrder`, `MemorialOrderMembership`, and
`MemorialOrderStatus` (`OPEN`, `FINALIZED`). Each order owns its membership, while
the referenced M6.2 journal entries and posting lines remain the canonical amounts.

Explicit Python commands in `service_layer/memorial_orders.py` each use one UoW:

1. `register_memorial_order_type` registers an exact form identifier, variant,
   revision, authority reference and supplied administrative evidence. No official
   types or regulatory revisions are seeded or inferred from a workbook header.
2. `register_numbering_scope` registers a ledger namespace and its required reviewed
   numbering-policy reference. Exact `(ledger_id, namespace)` is unique and immutable.
   The responsible accounting owner must establish which types, periods, funds or
   series share a namespace; callers must reuse its UUID. Supplied evidence does
   not independently authenticate the policy, its authority, or scope equivalence.
3. `create_memorial_order(CreateMemorialOrder(...), uow)` creates an OPEN register
   with caller UUID, selected type/ledger/period, `[coverage_start, coverage_end)`,
   and externally issued `MemorialOrderNumber(scope_id, value)`. Coverage may be
   a subset of the accounting period. `(scope_id, issued_number)` and UUID are unique.
   Multiple orders of the same type/period are permitted. No numbers or monthly
   calendars are generated, and numbering never uses `MAX + 1`.
4. `assign_journal_entries(AssignJournalEntries(...), uow)` validates the entire
   selected batch of existing posted entries before staging membership. Entries
   must share the order ledger/period and fall within its coverage. Each entry has
   at most one primary membership across OPEN and FINALIZED orders. Assignment
   records actor, authorization reference and an aware service timestamp.
5. `finalize_memorial_order(FinalizeMemorialOrder(...), uow)` requires a nonempty
   order and complete finalization evidence. It freezes membership and identity.
   It does not close the accounting period; later postings can remain unassigned.

Migration `0009` follows `0008` and adds four tables: type revisions, registered
numbering scopes, orders, and membership. Composite FKs preserve ledger/period/scope
relationships and real journal references. Database triggers additionally enforce
period/coverage matching, OPEN-only assignment, nonempty finalization, immutable
identity and append-only membership, including direct SQL and TRUNCATE CASCADE.
Assignment and finalization share the parent-order row lock and refreshed statement
snapshots under READ COMMITTED. Unique constraints resolve competing primary
assignments and register identities. Locks last through the command's single commit;
concurrency failures retain whole-command retry semantics. Existing journal triggers,
chart/period locks, historical account references and property behavior are preserved.
`0009` is forward-only and adds no inferred assignments to existing entries.

`service_layer/memorial_reporting.py` defines `GetMemorialOrderTotals` and
`GetJournalRegisterEntries`, with `JournalRegisterState.UNASSIGNED`, `OPEN`, or
`FINALIZED`. `SqlAlchemyMemorialOrderReader` performs set-based reads in its own
REPEATABLE READ, READ ONLY snapshot. Turnover remains separate by debit/credit,
currency, and original account-definition/version UUID; documentary references do
not multiply amounts. Empty registers return no currency/account groups. Assignment
queries require a ledger and optionally a period, with deterministic entry ordering.
These results reflect the query snapshot, not a new historical knowledge-time model.

Displayed correspondence rows are distinct from whole-entry membership. M6.2 does
not preserve arbitrary multiline debit-to-credit pairing. Totals carry the explicit
`correspondence_rendering_unsupported` diagnostic; requesting correspondence rows
raises `UnsupportedCorrespondenceRendering`. The supplied operational workbook is
reference evidence only, with an older regulatory header and no embedded calculation
formulas. No workbook, real records, or identifying contents are included in fixtures.
Future rendering needs an approved form revision, grouping/pairing and turnover rules,
and signature requirements. No Excel/PDF export, automatic allocation/posting,
partial/secondary membership, removal/reassignment, reopening, period closing,
corrections, trial balance, Journal-General, depreciation or Flask UI is added.

## M7.1: Local receipt imports

Use an explicitly configured, already provisioned development `DATABASE_URL` at
migration head. Startup never migrates or seeds data. Run one local process:

```bash
uv run flask --app maas_ua.entrypoints.flask_app:create_app --no-debug run --host 127.0.0.1 --no-reload --no-debugger
```

Open <http://127.0.0.1:5000/imports/new>, explicitly select the serialized receipt
import type, and download its template. XLSX uses `Document` field/value rows and
`SerialNumbers` asset rows; CSV repeats document/line metadata per asset. One upload
must describe one complete homogeneous serialized **INVENTORY** line. Catalog
nomenclature must already exist with `SERIALIZED` tracking; its exact registered unit
is used without aliases or conversion. Complete
initial custody is required. All errors block the whole document.

Money is exact Decimal: numeric XLSX cells are read from original XML decimal tokens
with at most 15 significant digits, text money allows 38, and decimal exponents are
bounded to 100. No rounding or currency-specific quantization is applied. Formulas
are rejected. Identifier cells must be text; case, Unicode and leading zeros remain
exact. Source/custody references and receipt analytics are opaque, without financial
dimension mapping or automatic catalog creation.

Preview writes no property facts. Confirmation rechecks PostgreSQL and calls
`register_inventory_receipt()` with one UoW/commit. The application creates technical
UUIDs; accountants never enter them. A retained preview has a stable registration
UUID for uncertain-response recovery. Re-upload recognition compares the entire
original receipt and serial set, including initial custody, even after transfers or
write-offs. Matching originals return their existing receipt; conflicts never
overwrite documents. Database serial uniqueness remains authoritative under races.
Document numbers remain nonunique. A new upload with disjoint serial identities can
be a new receipt even when its paper number repeats; no paper-document identity or
permanent import history is claimed.

Private in-memory previews expire after 30 minutes or application restart, with
session ownership, CSRF protection and capacity limits. Close completed results or
discard previews to release capacity. Raw uploads are not retained. The interface
rejects nonlocal clients, untrusted hosts, debug mode and multiprocess WSGI requests.
**Do not deploy it with multiple workers or use it for production data.** Production
requires approved authentication, authorization, handling and staging controls.
No staging table, migration, automatic journal, or memorial generation was added.

See [the full import contract and synthetic walkthrough](docs/development/receipt-imports.md)
and [the domain decision](docs/architecture/domain-model.md#25-milestone-71--local-serialized-inventory-receipt-import).

## M7.2: Inventory Explorer and registered units

Use the same local Flask process and navigation as receipt import:

- `/inventory/search`: exact `namespace` and `serial_value` lookup, with no case or whitespace normalization.
- `/inventory/assets/<asset_id>`: original receipt/price/source/classification, document-backed history,
  latest recorded custody, write-off evidence, and an `as_of` end-of-day position.
- `/inventory/balances`: historical quantity and acquisition valuation, filtered by date,
  nomenclature ID, receipt UUID, source, location, responsible party, currency and lifecycle.

New pages use Ukrainian labels and preserve persisted identifiers. Import results link to each
asset. Incomplete reports show diagnostics without partial quantities, currency totals or groups.
Empty complete reports show zero serial records. Currencies and units remain distinct; there is
no FX, depreciation or claim of physical presence from accounting lifecycle.

All five M4/M5A query contracts and the existing set-based SQL reader are reused unchanged.
Each operation retains REPEATABLE READ, READ ONLY isolation. The asset page composes separate
snapshots for history, latest recorded position and selected-date position; it does not promise
cross-query consistency. A balance/valuation page uses one reader operation and one snapshot.

Forward-only migration `0010` replaces the receipt-line `pcs` CHECK with exact catalogue-unit
referential integrity and a text CHECK. It preserves all historical rows and rejects existing
catalogue/receipt unit mismatches for review. It does not map `PCS`, `pcs`, `шт` or other labels,
convert quantities, seed units, or rewrite nomenclature. Shared/development/Neon databases have
not been migrated by this change; startup still performs no migrations.

See [the explorer guide and manual acceptance steps](docs/development/inventory-explorer.md)
for input semantics, dependency injection, limitations and the synthetic 27/12/8 walkthrough.

## Testing strategy

```text
Many fast unit tests for domain behavior
   -> service-layer tests with fakes
      -> fewer PostgreSQL integration tests for adapters, constraints and queries
         -> limited end-to-end tests when a user-facing entry point exists
```

For M4, domain calculations and query result composition can be unit-tested with fixtures/fakes. SQL aggregation, ordering, grouping, date filtering, monetary precision and reference integrity **must be covered by real PostgreSQL integration tests**. Test negative scenarios, zero results, boundary dates, and contradictory/incomplete data. Production data must never enter tests.

Fast tests need no PostgreSQL:

```bash
uv run pytest tests/unit tests/service_layer
uv run pytest tests/e2e
uv run ruff check .
```

Full verification on the existing isolated test service:

```bash
docker compose -f compose.test.yaml up -d --wait
export TEST_DATABASE_URL='postgresql+psycopg://maas_test:test_password@127.0.0.1:55432/maas_ua_test'
uv run pytest -q --tb=short
uv run ruff check .
```

The migration tests check Alembic/ORM agreement, historical revision reversibility, forward-only guards and preservation of populated legacy rows. The scale test checks fixed reporting query count and runs `EXPLAIN (ANALYZE, BUFFERS)` on 5,000 synthetic assets and 2,750 movement lines. An earlier M4 position aggregation measured approximately 13 ms; this is diagnostic, not a production performance guarantee. No extra reporting indexes were justified by that plan. Repository-wide lint and formatting checks pass; M7.2 includes formatting-only cleanup of three existing files without changing their behavior.

Do not run Alembic against production or shared databases without an approved deployment/migration procedure.

## Working with Codex

Read [`AGENTS.md`](AGENTS.md) and [`docs/architecture/domain-model.md`](docs/architecture/domain-model.md) before making domain changes. Inspect code and tests before choosing names, schemas or commands. Work in small TDD iterations, review migrations and diffs, and document accepted/undecided business rules.

## Security and limitations

- No public or AI-service uploads of real military accounting documents, unit identities, locations, personnel data or serials without proper authorization.
- No production-ready authorization, reconciliation accuracy, or legal compliance is implied by completed coding milestones.
- Treat all financial classification, document posting, and corrections as rules that must be verified with authorized accounting stakeholders.

## License

MAAS-UA follows an **Open Core** licensing model.

- **Community Edition** — open-source software distributed under the [MIT License](LICENSE).
- **Enterprise Edition** — proprietary software offering additional features, integrations, and professional support, available under a separate commercial license. A proprietary Enterprise Edition is planned.

The Community Edition remains free to use, modify, and distribute in accordance with the MIT License.

Enterprise features are not covered by the Community Edition's MIT License.

## Bulk transfer import (M7.3)

Open `/transfers/import/new` in the existing local Flask app. Versioned XLSX/CSV files
resolve exact serial identifiers to internal asset UUIDs and invoke the existing
`TransferAssets` service. See the [schema, synthetic template, retry limits and browser
acceptance guide](docs/development/transfer-imports.md). Preview staging is single-process
and session-bound; no new migration or financial posting is introduced. Head remains `0010`.

## Bulk inventory write-off import (M7.4)

Open `/write-offs/import/new` to upload a `serialized_inventory_write_off_v1` XLSX/CSV,
review exact serial identities, persisted receipt classification, authorization and explicit
accounting acceptance date, then confirm. The importer invokes existing `WriteOffAssets`;
only ACTIVE serialized INVENTORY is eligible. Mixed receipts/custodians are supported.
Stable asset IDs, receipt provenance and historical documents survive write-off. Retained
technical UUIDs allow immutable-fact retry reconciliation; paper act numbers are nonunique.
No financial journal is generated and no migration is introduced (head `0010`).

See [write-off import contract and acceptance guide](docs/development/write-off-imports.md)
and the [synthetic CSV](docs/examples/serialized_inventory_write_off_v1.csv).

The 2026–2027 Drone-first strategy builds on the reusable property and financial accounting
core. `AssetUnit`, `Nomenclature`, write-off, provenance and accounting lifecycle remain
domain-neutral. Property ACTIVE/WRITTEN_OFF is distinct from future UAV technical status
such as SERVICEABLE, UNSERVICEABLE or UNDER_REPAIR. UAV flight hours, repairs, maintenance
schedules, component replacement and servicing alerts remain future work. Fixed-asset UAV
disposal requires its own approved workflow; M7.4 retains the serialized-INVENTORY boundary.
