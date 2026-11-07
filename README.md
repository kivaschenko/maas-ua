# MAAS-UA

**Military Asset Accounting System — Ukraine**

MAAS-UA is a Python application for traceable accounting of military property, initially focused on individually identified FPV drones and reusable unmanned aircraft systems (БпАК), with the longer-term goal of reconciling property registers and financial accounting.

> **Status (2026-11-06):** M1–M5A and the M6.1 accounting reference foundation are implemented. M5A adds receipt-time accounting classification and documented write-off of serialized inventory, with financial-economic acceptance dates, atomic persistence, and historical lifecycle reporting. These are Python use cases; no Flask reporting endpoints or operational UI are implemented.
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
| Fixed-asset amortization, accounting entries, reconciliation | Planned (M6.2+) |
| Flask UI, Excel import/export integration and user-facing reports | Planned integration (M7+); verify any pre-existing scripts separately |

Implemented milestones do **not** imply production authorization, legal accounting compliance, or a complete financial ledger.

## Stack

- Python, managed by **uv** (`pyproject.toml`, `uv.lock`)
- Flask for future HTTP/web entry points
- SQLAlchemy 2.x, PostgreSQL, and Alembic for persistence
- pytest: unit, service-layer, and PostgreSQL integration tests
- Ruff for lint/format checks
- Excel processing via openpyxl / pandas when integration work starts
- VS Code with the Codex extension for local development

Development typically runs Python locally and PostgreSQL in Docker. Refer to the repository's existing Compose/env setup rather than creating a second one.

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
- **M6.2+ — Proposed:** journal entries, memorial orders, financial reports and approved period/correction rules.
- **M7 — Proposed:** Flask web workflows, Excel import/export, reports and regression tests.

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
there are no journal entries, postings, memorial orders, financial reports or
period-closing commands. Receipt/transfer/write-off commands retain M1–M5A behavior.

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

`ChartRepository.protected_through(version_id)` is the narrow integration point for
future posting/closed-period protection. It returns `None` in M6.1 because neither kind
of financial record exists. The domain rejects a cutover on or before a protected date.
Before future financial records are introduced, their maximum protected date must be
queried here, and posting/period commands must coordinate using the same chart lock.
M6.1 does not pretend to inspect nonexistent postings or implement period controls.

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

Balance-account double-entry equality and applicable off-balance single-entry treatment
remain distinct future behaviors. M6.1 implements neither. The selected entity, approved
codes/names, local suffix meanings, analytical requirements and future posting policies
remain business-owner inputs. See [the M6.1 domain contract](docs/architecture/domain-model.md#22-milestone-61--accounting-reference-data-and-version-management).

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
uv run ruff check .
```

Full verification on the existing isolated test service:

```bash
docker compose -f compose.test.yaml up -d --wait
export TEST_DATABASE_URL='postgresql+psycopg://maas_test:test_password@127.0.0.1:55432/maas_ua_test'
uv run pytest -q --tb=short
uv run ruff check .
```

The migration tests check Alembic/ORM agreement, historical revision reversibility, `0005`/`0006` downgrade refusal and preservation of populated legacy rows. The scale test checks fixed reporting query count and runs `EXPLAIN (ANALYZE, BUFFERS)` on 5,000 synthetic assets and 2,750 movement lines. An earlier M4 position aggregation measured approximately 13 ms; this is diagnostic, not a production performance guarantee. No extra reporting indexes were justified by that plan. Formatting checks for M4 use changed Python files; unrelated historical formatting differences remain untouched.

Do not run Alembic against production or shared databases without an approved deployment/migration procedure.

## Working with Codex

Read [`AGENTS.md`](AGENTS.md) and [`docs/architecture/domain-model.md`](docs/architecture/domain-model.md) before making domain changes. Inspect code and tests before choosing names, schemas or commands. Work in small TDD iterations, review migrations and diffs, and document accepted/undecided business rules.

## Security and limitations

- No public or AI-service uploads of real military accounting documents, unit identities, locations, personnel data or serials without proper authorization.
- No production-ready authorization, reconciliation accuracy, or legal compliance is implied by completed coding milestones.
- Treat all financial classification, document posting, and corrections as rules that must be verified with authorized accounting stakeholders.

## License

Internal/project-specific; define a license explicitly before any public release.
