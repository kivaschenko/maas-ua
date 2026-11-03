# PostgreSQL receipt persistence

Milestone 2 connects `RegisterInventoryReceipt` to PostgreSQL. Milestone 2.1 adds
persistent nomenclature and Milestone 3 adds custody transfers through the same UoW.
M5A adds documented serialized-inventory write-off through the same UoW. There is no
Flask entrypoint, financial posting, depreciation, or Excel workflow here.

## Mapping choice

The receipt entities and their value objects are frozen dataclasses. Evaluating
SQLAlchemy imperative mapping against an equivalent frozen class raised
`FrozenInstanceError: cannot assign to field '_sa_instance_state'` on construction.
SQLAlchemy instrumentation and mutable relationship collections would require
special handling or changes to the existing immutability guarantees.

Declarative persistence records in `adapters/orm.py` therefore map explicitly
to and from domain objects. This introduces conversion code, but keeps ORM state
out of the domain and returns fully materialized, immutable snapshots that work
after the session closes. The receipt repository eagerly loads its line collection;
asset units are separate aggregates accessed by their own repository.

## Schema

| Table | Stored facts and constraints |
| --- | --- |
| `nomenclatures` | Existing text ID, unique stable code, name, unit, explicit tracking, optional default classification |
| `receipt_documents` | UUID PK, document number, document date, effective date, aware creation timestamp, source |
| `receipt_lines` | UUID PK, receipt FK, tuple position, nomenclature ID and tracking policy snapshot, quantity and unit, unit price and currency, ordered analytics pairs, immutable nullable accounting classification snapshot |
| `asset_units` | UUID PK, origin line FK, serial namespace/value, immutable initial custody, current custody, lifecycle, latest effective date; unique exact namespace/value pair |
| `movement_documents` | UUID PK, document/effective dates, creation timestamp, historical source/destination, new-record registration order |
| `movement_lines` | Movement/unit FK pair, ordered position; each unit occurs once per document |
| `write_off_documents` | UUID PK, act date, authorization reference/date, explicit financial-economic acceptance date, aware registration timestamp |
| `write_off_lines` | Ordered document/unit references; each unit can belong to only one completed write-off |

`position` preserves the existing ordered line tuple; it is storage metadata, with
a unique `(receipt_id, position)` constraint. Receipt nomenclature IDs reference
the persistent catalog; tracking policy remains a receipt snapshot. Analytics use a JSONB array
of pairs to preserve their existing order. Line totals are derived and not stored
twice. There is no document-number uniqueness constraint.

Serial columns use PostgreSQL `C` collation so comparison remains exact and case
sensitive independently of the database default. Unit prices use unconstrained
`NUMERIC`, avoiding silent rounding to a fixed currency scale. Check constraints
reject nonfinite/negative prices, nonpositive quantities, non-`pcs` units, and
nonserialized lines. PostgreSQL's technical storage/index limits still apply;
these are not additional approved business maxima or currency rules.

No cascading deletes or ORM merges are used. The asset repository updates current
state only; receipts and movement history have no update/delete methods.
The domain and registration service validate the quantity/serial count and totals
before writes. Database FKs and uniqueness constraints defend references and
identity/serial races; there are no triggers implementing a second receipt workflow.

## Transactions and conflicts

`SqlAlchemyUnitOfWork` receives a session factory and opens one fresh session per
command. Repositories stage inserts and return snapshots, including staged
additions, without flushing or committing. The existing service calls `commit()`
once after staging the receipt and all units. Transfers lock selected units in UUID
order, validate the complete command, then stage a movement and current custody updates. A failed commit, exception, explicit
rollback, or exit without commit discards uncommitted changes and closes the session.

The serial precheck provides an early error, but does not prevent races. The unique
database constraint is authoritative: competing registrations can both pass the
precheck, and the loser receives `DuplicateSerialNumber` at commit. Other integrity
failures become `RepositoryConflict`. Application errors omit identifiers and
database parameters, and application engines use `hide_parameters=True`.

The confirmed slice policies are recorded in
[the domain model](../architecture/domain-model.md#17-confirmed-milestone-2-receipt-persistence-decisions).
Actual namespace assignment/authorization, serial correction, document deduplication,
and currency-specific accounting rules remain outside this milestone.

## Development configuration and migrations

The existing development Compose service remains in `compose.yaml`. Configure
`DATABASE_URL` explicitly, for example with the synthetic credentials from that file:

```bash
export DATABASE_URL='postgresql+psycopg://maas:dev_password@127.0.0.1:5432/maas_ua_dev'
uv run alembic upgrade head
```

This command applies the schema to the database you select; it is not part of test
setup. No development/shared database migration is performed automatically.
`DATABASE_URL` is the only environment configuration used by application assembly
and normal Alembic commands. `.env` is not loaded implicitly, and neither falls
back to `TEST_DATABASE_URL`. The psycopg binary extra supplies the required libpq
runtime without a separate system installation.

For a `RegisterInventoryReceipt` command prepared as in
[the in-memory example](in-memory-receipts.md), replace the fake UoW with:

```python
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.bootstrap import make_session_factory
from maas_ua.service_layer.receipts import register_inventory_receipt

session_factory = make_session_factory()  # Reads DATABASE_URL; no connection yet.
with SqlAlchemyUnitOfWork(session_factory) as writer:
    writer.nomenclatures.add(command.nomenclature)  # Create once before the first receipt.
    writer.commit()
result = register_inventory_receipt(command, SqlAlchemyUnitOfWork(session_factory))

with SqlAlchemyUnitOfWork(session_factory) as reader:
    assert reader.receipts.get(result.receipt.id) == result.receipt
```

The application owns the factory/engine lifetime. Each UoW closes its session.
Schema changes use Alembic; there is no `metadata.create_all()` startup path.

For populated Milestone 2 databases, first upgrade to `0002` and enter reviewed
catalog records with the existing nomenclature IDs, matching tracking and units.
Then upgrade to head. `0003` refuses unresolved or incompatible references without
rewriting receipts. `0004` leaves legacy custody/lifecycle unknown and sets the
known latest effective date from each original receipt. Such units cannot transfer
until an approved onboarding workflow exists; do not invent values in a migration.

`0005` adds initial custody and new-transfer order without filling legacy values.
It is forward-only: downgrade refuses to erase those facts. New order is allocated
at movement INSERT after asset row locks are held. See the
[M4 reporting contract](../architecture/domain-model.md#20-milestone-4--historical-inventory-and-acquisition-valuation)
for completeness, inclusive business-date semantics and read-only snapshot isolation.

Create each catalog record once; subsequent receipts reuse it. The constructor now
requires `code`, `name` and `unit_of_measure` in addition to the existing ID/tracking.
Registration rejects missing or mismatched catalog snapshots. See the confirmed
[transfer rules](../architecture/domain-model.md#19-milestone-3--documented-custody-transfers).

## Isolated integration tests

```bash
docker compose -f compose.test.yaml up -d --wait
export TEST_DATABASE_URL='postgresql+psycopg://maas_test:test_password@127.0.0.1:55432/maas_ua_test'
uv run pytest
uv run ruff check .
uv run ruff format --check .
docker compose -f compose.test.yaml down
```

This separate Compose project uses a different service, port, role, and temporary
storage, with no development volume mounted. The fixture accepts only an explicit
local PostgreSQL/psycopg URL for role `maas_test` and database `maas_ua_test`.
That role needs `CREATEDB`; the isolated container provisions it. The fixture creates
a new UUID-named database for each database test, applies Alembic, and drops only
that newly created database during teardown. It never migrates, clears, or drops
the supplied base database and never reads `DATABASE_URL` as a test fallback.

Without `TEST_DATABASE_URL`, database tests skip; unit and service tests still run
without PostgreSQL. With an explicit but unavailable/invalid test target, tests
fail rather than silently skipping.

Integration coverage includes cross-session reloads and detached snapshots,
multiple-line ordering, exact monetary round trips, namespace/case comparisons,
UUID and serial uniqueness, interleaved two-session serial races, foreign keys,
database check constraints, rollback of already flushed rows, failed service
commits preserving previous records, and UoW reuse. Migration tests exercise the
original reversible revisions through `0004`, verify ORM/head agreement, confirm
`0005` downgrade refusal, and verify that populated legacy business fields remain
unchanged after the forward upgrade.


## M5A write-off persistence

`0006` follows `0005` and is forward-only. It adds receipt-line classification without
backfilling legacy rows, creates write-off tables, and permits `WRITTEN_OFF` lifecycle.
No development/shared database is migrated by tests. Existing historical migrations
remain unchanged. The metadata/migration checks and downgrade refusal are tested.

Pass `accounting_classification=AccountingClassification.INVENTORY` explicitly when
registering eligible receipts. Omission means unknown, even if a catalog default exists;
unknown and fixed-asset origins cannot be written off. Receipt snapshots have no edit API.

Use `WriteOffAssets` / `write_off_assets` from `service_layer/write_offs.py` with the
existing `SqlAlchemyUnitOfWork`. The command requires a UUID reused on retries, act
number/date, authorization reference/date, internal asset IDs and explicit effective
date. That effective date is financial-economic acceptance of the verified package,
not first receipt of documents or a physical event. The domain requires it to be on
or after authorization, receipt and the latest transfer for every selected unit.

The service reuses transfer row locks in UUID order, rechecks identity after waiting,
validates origin/history facts and commits the document and lifecycle updates once.
Duplicate document UUIDs raise `DuplicateWriteOff`; existing write-off membership raises
`AlreadyWrittenOff`. Database uniqueness remains authoritative for concurrent races;
a failed insert rolls back all lifecycle changes. Repositories never flush or commit.
Historical reporting derives membership from write-off effective dates while retaining
original custody, serials, receipts, prices and movements. Read transactions and
separate currency totals retain the M4 guarantees.

Tests cover persistence/reload, all four distinct dates, flushed rollback, UUID races
on disjoint assets, same-unit uniqueness across documents, FK/check constraints,
competing write-offs, both transfer/write-off lock orders, chronology after waits,
and the 27/12/8 historical reporting scenario. Unknown legacy classification requires
an approved future onboarding process; do not fill it from catalog defaults.
