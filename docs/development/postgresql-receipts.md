# PostgreSQL receipt persistence

Milestone 2 connects `RegisterInventoryReceipt` to PostgreSQL. The domain, service,
ports, and transactional fakes retain their existing behavior. There is no Flask
entrypoint, financial posting, movement, depreciation, or Excel workflow here.

## Mapping choice

The receipt entities and their value objects are frozen dataclasses. Evaluating
SQLAlchemy imperative mapping against an equivalent frozen class raised
`FrozenInstanceError: cannot assign to field '_sa_instance_state'` on construction.
SQLAlchemy instrumentation and mutable relationship collections would require
special handling or changes to the existing immutability guarantees.

Three declarative persistence records in `adapters/orm.py` therefore map explicitly
to and from domain objects. This introduces conversion code, but keeps ORM state
out of the domain and returns fully materialized, immutable snapshots that work
after the session closes. The receipt repository eagerly loads its line collection;
asset units are separate aggregates accessed by their own repository.

## Schema

| Table | Stored facts and constraints |
| --- | --- |
| `receipt_documents` | UUID PK, document number, document date, effective date, aware creation timestamp, source |
| `receipt_lines` | UUID PK, receipt FK, tuple position, nomenclature ID and tracking policy snapshot, quantity and unit, unit price and currency, ordered analytics pairs |
| `asset_units` | UUID PK, origin line FK, serial namespace and serial value; unique exact namespace/value pair |

`position` preserves the existing ordered line tuple; it is storage metadata, with
a unique `(receipt_id, position)` constraint. Nomenclature remains the existing
line value object, without an invented catalog table. Analytics use a JSONB array
of pairs to preserve their existing order. Line totals are derived and not stored
twice. There is no document-number uniqueness constraint.

Serial columns use PostgreSQL `C` collation so comparison remains exact and case
sensitive independently of the database default. Unit prices use unconstrained
`NUMERIC`, avoiding silent rounding to a fixed currency scale. Check constraints
reject nonfinite/negative prices, nonpositive quantities, non-`pcs` units, and
nonserialized lines. PostgreSQL's technical storage/index limits still apply;
these are not additional approved business maxima or currency rules.

No cascading deletes, repository update/delete methods, or ORM merges are used.
The domain and registration service validate the quantity/serial count and totals
before writes. Database FKs and uniqueness constraints defend references and
identity/serial races; there are no triggers implementing a second receipt workflow.

## Transactions and conflicts

`SqlAlchemyUnitOfWork` receives a session factory and opens one fresh session per
command. Repositories stage inserts and return snapshots, including staged
additions, without flushing or committing. The existing service calls `commit()`
once after staging the receipt and all units. A failed commit, exception, explicit
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
result = register_inventory_receipt(command, SqlAlchemyUnitOfWork(session_factory))

with SqlAlchemyUnitOfWork(session_factory) as reader:
    assert reader.receipts.get(result.receipt.id) == result.receipt
```

The application owns the factory/engine lifetime. Each UoW closes its session.
Schema changes use Alembic; there is no `metadata.create_all()` startup path.

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
commits preserving previous records, and UoW reuse. The migration test upgrades a
fresh database, checks ORM/schema agreement, downgrades it, and upgrades it again.
