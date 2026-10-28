# MAAS-UA

**Military Asset Accounting System — Ukraine**

MAAS-UA is a Python web application for automating military property accounting workflows, maintaining registers of fixed assets and inventories, reconciling accounting records, and generating Excel-based accounting documents.

> **Project status:** Initial architecture and repository setup. The capabilities below describe the intended scope; they are not all implemented yet.
>
> **Data protection:** Use synthetic or properly sanitized datasets for development, testing, CI, and public issue reports. Never commit real military accounting documents, personal data, operational details, or credentials.

## Goals

- Maintain structured registers of fixed assets, inventories, and related reference data.
- Record auditable accounting operations and supporting documents.
- Automate depreciation calculations and preparation of memorial orders.
- Import, validate, reconcile, and export Excel workbooks.
- Support controlled correction of posted accounting entries rather than unrestricted CRUD updates.
- Keep the application straightforward to develop, test, deploy, and maintain.

## Technology stack

| Component | Technology |
| --- | --- |
| Web | Flask 3, Jinja2 templates, HTML forms |
| ORM | SQLAlchemy 2.x, typed Declarative Mapping |
| Database | PostgreSQL |
| Migrations | Alembic |
| Excel | openpyxl, pandas (as needed) |
| Tests | pytest, pytest-cov |
| Tooling | uv, Ruff |
| CI | GitHub Actions (planned) |

## Architecture

The application follows a **pragmatic three-layer architecture**:

```text
HTTP / HTML / Forms
        |
        v
Presentation Layer (Flask routes / blueprints)
        |
        v
Service Layer (accounting workflows and business rules)
        |
        v
Data Access Layer (SQLAlchemy Session / ORM / PostgreSQL)
```

Excel readers and writers are separate integration components invoked by services. Calculations that do not require persistence should remain pure Python functions so they can be tested without Flask or a database.

### Responsibilities

**Presentation:** HTTP requests and responses, form validation, authentication/authorization integration, and template rendering. Routes should not implement accounting calculations or complex SQL queries.

**Services:** Explicit business operations such as registering receipts, calculating depreciation, generating memorial orders, reconciling registers, and writing off assets. Services own workflow coordination, validation, and transaction boundaries.

**Data access:** SQLAlchemy ORM models, queries, sessions, and PostgreSQL persistence. SQLAlchemy's `Session` already provides identity-map and unit-of-work behavior; a custom Repository abstraction is **optional**, not mandatory.

**Excel:** Parse and validate incoming workbooks, then render outgoing workbooks. Keep worksheet layouts and formatting outside core accounting calculations.

### Design decisions

1. Use **SQLAlchemy 2.x Annotated Declarative Mapping**: `DeclarativeBase`, `Mapped[T]`, and `mapped_column()`.
2. Define one shared declarative base and `MetaData` with deterministic constraint naming conventions for Alembic.
3. Use Alembic for schema changes; do not use `Base.metadata.create_all()` as the production migration mechanism.
4. Use normalized PostgreSQL tables for transactional accounting (**OLTP**), not a Star Schema as the primary database model.
5. Keep SQLAlchemy ORM models as persistence/application objects where practical; do not duplicate every model as a separate DDD entity.
6. Add repositories only when they simplify complex queries, isolate important aggregate loading, or solve a concrete testing/maintenance problem.
7. Use `Decimal` and explicit rounding rules for money; use PostgreSQL `NUMERIC` for monetary columns. Define and verify financial rules before implementing them.
8. Treat posted accounting records as controlled, auditable operations. Corrections should follow approved accounting procedures.
9. Prefer a modular monolith. Do not add microservices, queues, or additional databases without demonstrated need.

## Proposed repository structure

```text
maas-ua/
├── src/
│   └── maas_ua/
│       ├── __init__.py
│       ├── app.py                 # Flask application factory
│       ├── config.py
│       ├── db.py                  # Engine / session management
│       ├── models/
│       │   ├── __init__.py
│       │   ├── base.py            # DeclarativeBase and MetaData
│       │   ├── assets.py
│       │   ├── inventory.py
│       │   ├── accounting.py
│       │   └── documents.py
│       ├── services/
│       │   ├── accounting.py
│       │   ├── amortization.py
│       │   ├── reconciliation.py
│       │   └── documents.py
│       ├── repositories/         # Optional; add only if useful
│       ├── excel/
│       │   ├── readers/
│       │   └── writers/
│       └── web/
│           ├── routes/
│           ├── forms/
│           └── templates/
├── migrations/                   # Alembic revisions
├── tests/
│   ├── conftest.py
│   ├── unit/
│   ├── integration/
│   └── fixtures/                 # Synthetic Excel data only
├── .github/workflows/            # CI, when configured
├── pyproject.toml
├── uv.lock
├── alembic.ini
└── README.md
```

> This tree is a **target structure**, not a claim that all files or modules already exist. Start with the minimum viable modules and expand as workflows are implemented.

## Data model: initial candidates

The first version of the database should be driven by actual use cases, not by a fully speculative schema. Candidate entities include:

| Model | Purpose |
| --- | --- |
| `Nomenclature` | Reference catalog of asset/inventory types |
| `Asset` | Individually tracked fixed asset |
| `InventoryItem` / `StockPosition` | Inventory accounting position |
| `OrganizationalUnit` | Organizational reference data |
| `FundingSource` | Funding classification |
| `AccountingAccount` | Chart of accounts and subaccounts |
| `AccountingDocument` | Source and generated accounting documents |
| `AccountingOperation` | Business/accounting operation |
| `JournalEntry` / `JournalEntryLine` | Posting and debit/credit lines |
| `AssetMovement` | Asset lifecycle/movement history |
| `DepreciationRecord` | Depreciation results by period |
| `ImportBatch` | Provenance of imported files and processing runs |

These names and relationships are **proposals**. Confirm identity, cardinality, constraints, and audit requirements against real workflows and applicable accounting rules before creating migrations.

### ORM mapping convention

```python
# src/maas_ua/models/base.py
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

Example mapping (illustrative, **not** a finalized accounting schema):

```python
from decimal import Decimal
from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from maas_ua.models.base import Base


class Nomenclature(Base):
    __tablename__ = "nomenclature"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_number: Mapped[str] = mapped_column(String(100))
    nomenclature_id: Mapped[int] = mapped_column(ForeignKey("nomenclature.id"))
    initial_cost: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    nomenclature: Mapped[Nomenclature] = relationship()
```

Uniqueness of inventory numbers and other constraints must be defined according to the actual accounting scope, not assumed globally.

## Database and migrations

Use **PostgreSQL** as the source of truth for structured records and metadata. Store original and generated XLSX files in protected file/object storage; save their metadata, paths/identifiers, hashes, and processing status in PostgreSQL when document persistence is introduced.

Alembic will manage schema evolution:

```bash
uv run alembic revision --autogenerate -m "initial schema"
uv run alembic upgrade head
```

These commands require Alembic configuration, a reachable database, and imported ORM models in `migrations/env.py` so that `target_metadata = Base.metadata` includes every mapped table. Review autogenerated migrations before applying them, especially destructive changes and renames.

## Local development (once bootstrap is implemented)

**Prerequisites:** Python version specified in `pyproject.toml`, [uv](https://docs.astral.sh/uv/), and PostgreSQL.

```bash
# Install locked dependencies
uv sync

# Configure local environment (use a non-committed .env or environment variables)
export DATABASE_URL="postgresql+psycopg://maas:local_password@localhost:5432/maas_dev"

# Apply migrations
uv run alembic upgrade head

# Run Flask (application factory to be implemented)
uv run flask --app maas_ua.app:create_app run --debug
```

The database name, credentials, and URL above are **examples**, not provisioned resources. Never use production credentials for development. Exact startup commands must be updated to match the implemented application factory and configuration.

## Testing strategy

Use **pytest** with two complementary test suites:

- **Unit tests:** pure accounting calculations, validation, reconciliation, and formatting rules. No Flask or database required.
- **Integration tests:** Flask routes, SQLAlchemy queries/transactions, PostgreSQL constraints, Alembic migrations, and Excel reader/writer behavior.

Prefer a dedicated **test PostgreSQL database**, rather than assuming SQLite behaves identically. Fixtures should create isolated test data and roll back transactions when appropriate. Use `tmp_path` for file outputs and synthetic XLSX workbooks for regression tests.

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

These commands become executable after the development dependencies and test suite are added. Tests must never connect to the production database.

## Excel workflows

The intended document workflows include:

- MO-09 generation from amortization records.
- MO-17 generation for amortization and receipts.
- MO-16 generation.
- Reconciliation of shortage registers against MO-16.
- Reconciliation of material/technical supply registers.

When migrating existing Excel scripts, first capture their behavior with deterministic **synthetic regression fixtures**. Separate reading/parsing, accounting calculations, and workbook generation without silently changing worksheet structure, amounts, formatting, or established input/output contracts.

## Implementation roadmap

### Phase 1 — Foundation

- Initialize the repository with `uv`, Flask, SQLAlchemy 2.x, PostgreSQL driver, Alembic, pytest, and Ruff.
- Add application factory, configuration, engine/session lifecycle, and health endpoint.
- Configure GitHub Actions for linting and tests.
- Establish synthetic fixtures and safe development data practices.

### Phase 2 — First persistent workflow

- Choose one concrete accounting workflow (for example, asset receipt registration).
- Implement only the necessary ORM models, constraints, and Alembic migration.
- Add a service function and a thin Flask route/form.
- Test validation, transactions, and persistence against test PostgreSQL.

### Phase 3 — Excel integration

- Integrate existing Excel scripts incrementally as readers, writers, and calculation services.
- Preserve legacy output behavior with characterization tests.
- Add import provenance, validation, and isolated per-run files.

### Phase 4 — Accounting workflows

- Implement controlled accounting operations, asset/inventory movements, depreciation, and document lifecycle.
- Add reconciliation between property and financial records.
- Introduce repositories only when concrete complexity justifies them.

### Phase 5 — Hardening

- Implement role-based access, audit logging, backup/restore procedures, secure file handling, and operational monitoring.
- Optimize expensive queries based on measured performance.
- Add reporting views; introduce an analytical Star Schema only if reporting requirements warrant it.

## Engineering principles

- **Simple first:** do not add DDD layers, custom repositories, or event-driven infrastructure without demonstrated need.
- **Explicit operations:** CRUD is appropriate for reference catalogs; posted accounting operations require controlled workflows.
- **Transactional consistency:** service-level operations that update related records must share a clear SQLAlchemy transaction boundary.
- **Financial correctness:** use `Decimal`, `NUMERIC`, validated posting invariants, and documented rounding policies.
- **Auditability:** preserve source-document references, record provenance, and correction history.
- **Testability:** prefer real PostgreSQL integration tests and pure unit tests over unnecessary mocking.
- **Security:** keep sensitive documents, database dumps, credentials, and personal data outside the repository and CI artifacts.

## License

License to be determined. Do not assume the repository is open-source until a license and data-publication policy are explicitly selected.
