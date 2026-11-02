# MAAS-UA

**Military Asset Accounting System — Ukraine**

MAAS-UA is a Python application for traceable accounting of military property, initially focused on individually identified FPV drones and reusable unmanned aircraft systems (БпАК), with the longer-term goal of reconciling property registers and financial accounting.

> **Status (reported as of November 2026):** Milestones M1 (receipt domain/service tests), M2 (PostgreSQL persistence and Unit of Work), M2.1 (nomenclature), and M3 (asset movements/transfers) are reported complete. M4 (inventory queries, provenance, history, and as-of balances) is next. This README was updated from the project planning conversation, **not from an inspection of the current repository**. Verify exact class names, CLI commands, API contracts, and implementation coverage against the code before merging.
>
> **Data handling:** Use synthetic records in code, fixtures, prompts, CI, documentation, logs, and public discussions. Do not publish real military inventory, serial numbers, unit identities, custody/location data, accounting documents, or operational details. Follow applicable authorization and data-handling requirements.

## Purpose

The key question MAAS-UA must eventually answer is: **What property makes up an accounting balance as of a specified date; where is each item, who is responsible for it, what was its acquisition source and value, and which documents explain its history?**

The system distinguishes property/custody accounting from financial accounting. A physical transfer need not change total carrying value; depreciation need not change physical location. Do not assume that a transfer is a write-off or that property movements automatically generate accounting postings.

## Current scope and capabilities

| Capability | Status |
| --- | --- |
| Register receipt with a homogeneous receipt line and 27 individual serial-tracked units | Reported implemented (M1) |
| Unit and service-layer tests with fake repository | Reported implemented (M1) |
| PostgreSQL persistence, SQLAlchemy repository/UoW, Alembic integration | Reported implemented (M2) |
| Persistent `Nomenclature` and receipt references | Reported implemented (M2.1) |
| Document-backed transfer/movement of serial-tracked units | Reported implemented (M3) |
| Find unit by serial, show history/provenance/current custody and as-of stock valuation | Planned (M4) |
| Write-off and reversal/corrections | Planned; business rules to confirm (M5+) |
| Fixed-asset amortization, accounting entries, reconciliation | Planned (M6+) |
| Flask UI, Excel import/export integration and user-facing reports | Planned integration (M7+); verify any pre-existing scripts separately |

The status table is intentionally conservative. A reported milestone does **not** imply all production-readiness, authorization, error-recovery, or audit requirements have been validated.

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

- **M1 — Completed (reported):** receipt registration domain, validations, unit and fake-UoW/service tests.
- **M2 — Completed (reported):** SQLAlchemy/PostgreSQL repository and transaction persistence, Alembic, integration tests.
- **M2.1 — Completed (reported):** nomenclature entity and persistence, receipt-line linkage.
- **M3 — Completed (reported):** transfer command, movement document/history, custody changes, tests.
- **M4 — Next:** traceability/read-side queries, current and historical custody, inventory balance and acquisition valuation, filters and grouping, PostgreSQL integration tests.
- **M5 — Proposed:** document-supported write-off of individual units, preserved history, correction policy.
- **M6 — Proposed:** fixed-asset accounting, depreciation, ledger analytics, reconciliation.
- **M7 — Proposed:** Flask web workflows, Excel import/export, reports and regression tests.

The roadmap is a planning tool, not an assertion of final approved accounting functionality.

## M4 scope: Inventory, Traceability & Balances

Target query use cases (adapt names to codebase):

- `GetAssetBySerialNumber`: find a tracked unit under the agreed serial uniqueness policy.
- `GetAssetHistory`: receipt and chronologically ordered confirmed movement records, with document references.
- `GetAssetPositionAsOf`: resolve the unit's custody/location **as of a business date**, based on receipt and valid movement history.
- `GetInventoryBalance`: group units by nomenclature, source/receipt, custody/location and agreed accounting dimensions for a date.
- `GetInventoryValuation`: calculate acquisition-value totals using receipt-line prices with `Decimal`, with no depreciation or unimplemented write-offs assumed.

**Key questions before implementation:** Which timestamp is legally/business-relevant (`effective_date`, document date, registration time)? Are transfers always chronological? How are corrections, canceled documents, and backdated records handled? Is an initial custody assignment established at receipt? What does a transfer between custodians mean when the physical location is unchanged?

M4 is read-only. No new posting, write-off, reversal, or retroactive editing functionality should be added in this milestone. For an as-of query, never substitute the current `AssetUnit` state for historical state. Use confirmed operations and the agreed business date. If historical reconstruction is impossible with existing M3 data, document the gap and propose the smallest safe schema/data migration before implementing the query.

### Acceptance scenario (synthetic)

1. Receive 27 serialized units under one line at UAH 14,500 each (UAH 391,500 total).
2. Transfer 12 of those units to a second custodian, leaving 15 with the original custodian.
3. The as-of report **before** the transfer shows all 27 with their original custodian; **after** transfer, 15 and 12 respectively.
4. Combined total remains 27 units and UAH 391,500; no value changes solely because of an internal transfer.
5. Selecting one transferred serial reveals its receipt, acquisition source, unit cost, transfer document, and custody history.

These are illustrative assumptions, not real military records; dates, custody rules, and registration status must be explicit in the tests.

## Testing strategy

```text
Many fast unit tests for domain behavior
   -> service-layer tests with fakes
      -> fewer PostgreSQL integration tests for adapters, constraints and queries
         -> limited end-to-end tests when a user-facing entry point exists
```

For M4, domain calculations and query result composition can be unit-tested with fixtures/fakes. SQL aggregation, ordering, grouping, date filtering, monetary precision and reference integrity **must be covered by real PostgreSQL integration tests**. Test negative scenarios, zero results, boundary dates, and contradictory/incomplete data. Production data must never enter tests.

Run existing project commands as defined in `pyproject.toml`. Typical commands, if applicable:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run alembic upgrade head
```

Do not run Alembic against production or shared databases without an approved deployment/migration procedure.

## Working with Codex

Read [`AGENTS.md`](AGENTS.md) and [`docs/architecture/domain-model.md`](docs/architecture/domain-model.md) before making domain changes. Inspect code and tests before choosing names, schemas or commands. Work in small TDD iterations, review migrations and diffs, and document accepted/undecided business rules.

## Security and limitations

- No public or AI-service uploads of real military accounting documents, unit identities, locations, personnel data or serials without proper authorization.
- No production-ready authorization, reconciliation accuracy, or legal compliance is implied by completed coding milestones.
- Treat all financial classification, document posting, and corrections as rules that must be verified with authorized accounting stakeholders.

## License

Internal/project-specific; define a license explicitly before any public release.
