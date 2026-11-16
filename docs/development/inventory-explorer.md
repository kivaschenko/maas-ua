# Local Inventory Explorer (M7.2)

Run the existing Flask application using the [receipt-import startup instructions](receipt-imports.md).
Use one local process, debug disabled, and synthetic data in an explicitly selected disposable
development database provisioned at head `0010`. Startup does not migrate, seed, or connect just
to assemble the application. Do not apply migrations to development/shared/Neon databases as
part of browser verification without the separate approved operator procedure.

## Unit-of-measure correction

`ReceiptLine` accepts any explicitly registered serialized nomenclature unit and requires an
exact match. CSV/XLSX templates have no unit field; the command derives it from nomenclature.
Tracking `SERIALIZED`, classification `INVENTORY`/`FIXED_ASSET`/unknown, and unit text remain
independent. Imports still require `INVENTORY`. There is no mapping between `pcs`, `PCS`, `шт`,
`штука` or set codes, no unit conversion and no rewrite of existing records.

The old restriction was in ReceiptLine validation, import validation/command construction,
the preview label, and `ck_receipt_lines_quantity_unit_pcs`. Migration `0010` replaces it with
exact catalogue-unit referential integrity and a nonblank text constraint. The migration
preserves every stored value; existing catalogue/receipt unit mismatches stop it for review.
It is forward-only because restoring the literal CHECK could reject historical receipts.

## Pages and input semantics

| Page | Inputs / behavior |
| --- | --- |
| `GET /inventory/search` | Exact `namespace` and `serial_value`; successful lookup redirects to its asset card |
| `GET /inventory/assets/<asset_id>` | Asset UUID and optional exact `as_of=YYYY-MM-DD`; history and two custody sections |
| `GET /inventory/balances` | `as_of`, `nomenclature_id`, `receipt_id`, `source`, `location_ref`, `responsible_party_ref`, `currency`, `lifecycle_state`, `view` |

Default date is the current business date in Europe/Kyiv. Explicit blank/invalid dates are
errors; timestamps, week dates and compact dates are rejected. Dates include all effective
events through the end of the day. This is effective-date history, not a record of what was
known at an earlier registration time; a late document can change a past report.

Serial text is never stripped or case-folded. Nonblank exact input without outer whitespace
is required; NUL, overlong fields and duplicate query parameters are rejected. Missing/invalid
UUIDs and unknown assets have safe errors. Optional blank report filters are omitted.
Nomenclature uses its internal catalogue ID (displayed in query/API data), not its code.
`lifecycle_state` is `ACTIVE` by default, `WRITTEN_OFF`, or `ALL`; `view` is `balance` or
`valuation`. There is no fuzzy search, alias lookup, or automatic catalogue creation.

The asset card separates original acquisition price from remaining carrying value, which is
not calculated. Latest recorded custody includes all registered effective events, even future
ones, and is separately labelled. Historical custody comes only from the selected-date query.
Written-off units retain receipt origin, last recorded custody, movement history and evidence.
Lifecycle is an accounting fact and does not certify physical availability.

Legacy same-day transfer ordering is displayed as unknown, without numbered chronology.
Incomplete positions/history show warnings and available facts without assigning missing custody.
Incomplete balance/valuation reports hide all groups, quantities and values, including diagnostic
known totals. The page explains why no authoritative result is available. Valid empty reports
show zero serial records and no currency totals. Counts within groups retain their units;
the page's serial-record count is a count of identities. Currencies remain separate.

## Queries, transactions and injection

The blueprint calls the existing `GetAssetBySerialNumber`, `GetAssetHistory`,
`GetAssetPositionAsOf`, `GetInventoryBalance`, and `GetInventoryValuation` application queries.
The existing SQLAlchemy reader and set-based SQL are unchanged. Each reader operation owns a
REPEATABLE READ, READ ONLY transaction, rolls back on close, and cannot autoflush command state.
No new aggregates, reporting architecture or N+1 loops are introduced.

The asset card composes three independent snapshots: history/details, latest recorded position,
and selected-date position. These can differ under concurrent registration; the page says so.
A balance page chooses one balance or valuation operation, whose groups and totals share one
snapshot. Search and its redirect are also separate reads. No cross-request or cross-operation
snapshot guarantee is claimed.

`create_app(config=None, *, imports=None, inventory=None, transfers=None)` assembles both production readers on
the same existing session-factory engine when imports are not injected. Tests may inject both
dependencies independently. Injecting only imports leaves inventory unconfigured; inventory
requests then fail safely without creating an implicit second factory or database connection.

New pages use Ukrainian labels; Python/contracts/documentation and persisted identifiers remain
unchanged. Templates separate labels from query data for future localization. Existing escaping,
no-store, no-referrer, CSP, local/trusted-host limits, session ownership and write-CSRF remain.
Debug defaults off and debug/multiprocess requests are rejected. No production access, RBAC,
depreciation, currency conversion, write-off forms, pagination or exports are supplied.
M7.3 adds [bulk transfer import](transfer-imports.md) using the existing transfer command.
Large numbers of distinct report groups can produce large HTML tables; SQL remains set-based.
Werkzeug development access logs retain status/size while replacing request lines with fixed
text, so exact search identifiers and filters do not enter those logs.

## Manual browser acceptance with synthetic data

1. Follow the import guide's synthetic catalogue and 27-unit XLSX walkthrough, at UAH 14,500
   per unit. To exercise the fix, explicitly register that synthetic catalogue with
   `unit_of_measure="EXAMPLE-SET"` instead of `pcs` before importing. Use a fresh namespace
   and database for each walkthrough. Do not alter an existing catalogue to reinterpret receipts.
2. Upload XLSX, inspect preview, explicitly confirm, and follow a result asset link. Verify
   receipt `EXAMPLE-125`, effective date `2026-01-16`, source, unit and UAH 14,500 original price.
3. Use “Пошук майна” for exact namespace `EXAMPLE-ISSUER` and value `EXAMPLE-0000`. Verify that
   changing case or adding outer spaces does not silently match the identifier.
4. Prepare movements using existing commands in a local Python session with the same explicitly
   selected disposable synthetic database. This is a one-time fixture step, not a Flask form:

   ```python
   from datetime import date
   from uuid import uuid4
   from maas_ua.adapters.inventory import SqlAlchemyInventoryReader
   from maas_ua.adapters.receipt_import_reader import SqlAlchemyReceiptImportReader
   from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
   from maas_ua.bootstrap import make_session_factory
   from maas_ua.domain.custody import CustodyAssignment
   from maas_ua.service_layer.inventory import GetAssetBySerialNumber
   from maas_ua.service_layer.movements import TransferAssets, transfer_assets
   from maas_ua.service_layer.write_offs import WriteOffAssets, write_off_assets

   factory = make_session_factory()  # Explicit disposable synthetic DATABASE_URL.
   asset = SqlAlchemyInventoryReader(factory.kw["bind"]).asset_by_serial(
       GetAssetBySerialNumber(namespace="EXAMPLE-ISSUER", value="EXAMPLE-0000")
   )
   original = SqlAlchemyReceiptImportReader(factory).receipt(asset.origin.receipt_id)
   units = tuple(sorted(original.units, key=lambda unit: unit.serial_number.value))
   assert len(units) == 27
   transfer_assets(
       TransferAssets(
           movement_id=uuid4(),
           document_number="EXAMPLE-MOVE",
           document_date=date(2026, 2, 1),
           effective_date=date(2026, 2, 1),
           source=units[0].custody,
           destination=CustodyAssignment("EXAMPLE-B", "EXAMPLE-B"),
           asset_ids=tuple(unit.id for unit in units[:12]),
       ),
       SqlAlchemyUnitOfWork(factory),
   )
   write_off_assets(
       WriteOffAssets(
           write_off_id=uuid4(),
           document_number="EXAMPLE-WRITE-OFF",
           document_date=date(2026, 2, 9),
           authorization_reference="EXAMPLE-AUTH",
           authorization_date=date(2026, 2, 9),
           effective_date=date(2026, 2, 10),
           asset_ids=tuple(unit.id for unit in units[:8]),
       ),
       SqlAlchemyUnitOfWork(factory),
   )
   factory.kw["bind"].dispose()
   ```

5. In the asset card choose `2026-01-15` (not registered), `2026-01-31` (initial custody),
   `2026-02-01` (EXAMPLE-B), `2026-02-09` (ACTIVE), and `2026-02-10` (WRITTEN_OFF with evidence).
   Verify receipt, movement and write-off documents remain visible in history.
6. Open balances at `2026-02-10`, active state. Verify 19 records, UAH 275,500, grouped as
   15 at the original assignment and 4 at EXAMPLE-B. Before write-off: 27 and UAH 391,500.
   Written-off state shows 8 and UAH 116,000; ALL retains 27 and UAH 391,500.
7. Repeat with the valuation view, receipt/source/currency/custody filters, and a source
   that has no records. Verify an empty report rather than an error or invented currency.

Incomplete and ambiguous legacy facts are exercised by automated disposable-database tests;
do not damage registered audit history to manufacture them in a shared database. The tests
verify explicit warnings and absence of diagnostic partial totals in HTML. Browser acceptance
requires running the steps above; automated Flask clients do not constitute visual browser QA.

## Verification

Fast tests need no database. Full tests use the existing isolated local test service;
the fixture creates/drops only randomly named disposable databases, never the supplied database:

```bash
uv run pytest tests/unit tests/service_layer tests/e2e -q
docker compose -f compose.test.yaml up -d --wait
export TEST_DATABASE_URL='postgresql+psycopg://maas_test:test_password@127.0.0.1:55432/maas_ua_test'
uv run pytest -q --tb=short
uv run ruff check .
uv run ruff format --check .
```

Migration tests verify head/ORM consistency, 0009 row preservation, safe refusal on legacy
unit mismatches and forward-only guards. Existing set-based query, repeatable-read/read-only,
scale, race and M1–M7.1 regressions remain part of the full suite. The new acceptance test uses
the normal factory, real XLSX import, two transfers, write-off and browser-style GETs.

The completed regression run passed **694 tests**, including PostgreSQL integration and
Alembic/ORM consistency checks. `ruff check .`, `ruff format --check .` and
`git diff --check` pass. Browser visual acceptance remains manual.

## Changed files

Implementation and schema:

- `src/maas_ua/domain/receipts.py`
- `src/maas_ua/service_layer/receipt_imports.py`
- `src/maas_ua/adapters/orm.py`
- `migrations/versions/0010_receipt_unit_of_measure.py`
- `src/maas_ua/entrypoints/flask_app.py`
- `src/maas_ua/entrypoints/inventory.py`
- `src/maas_ua/entrypoints/templates/base.html`
- `src/maas_ua/entrypoints/templates/imports/preview.html`
- `src/maas_ua/entrypoints/templates/imports/result.html`
- `src/maas_ua/entrypoints/templates/inventory/search.html`
- `src/maas_ua/entrypoints/templates/inventory/asset.html`
- `src/maas_ua/entrypoints/templates/inventory/balances.html`
- `src/maas_ua/entrypoints/templates/inventory/error.html`
- `src/maas_ua/entrypoints/templates/inventory/macros.html`

Behavioral verification:

- `tests/unit/test_receipts.py`
- `tests/unit/test_inventory_http_inputs.py`
- `tests/service_layer/test_confirm_receipt_import.py`
- `tests/e2e/test_receipt_import_ui.py`
- `tests/e2e/test_inventory_ui.py`
- `tests/integration/test_receipt_imports.py`
- `tests/integration/test_receipt_persistence.py`
- `tests/integration/test_inventory_explorer.py`
- `tests/integration/test_migrations.py`

Documentation:

- `README.md`
- `docs/architecture/domain-model.md`
- `docs/development/receipt-imports.md`
- `docs/development/inventory-explorer.md`

Formatting-only cleanup to satisfy the repository-wide check:

- `docs/development/in-memory-receipts.md`
- `migrations/versions/0001_receipt_persistence.py`
- `tests/integration/conftest.py`
