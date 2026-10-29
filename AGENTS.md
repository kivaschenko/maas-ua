# MAAS-UA — Codex Working Instructions

> Scope: instructions for coding agents working in this repository. Read `docs/architecture/domain-model.md` before implementing domain behavior. These instructions describe the target architecture, **not** a claim that all modules already exist.

## 1. Project purpose

MAAS-UA is an internal application for accounting and reconciliation of military property, initially focused on unmanned aerial systems, FPV drones, spare parts, and accessories. It must explain **which items make up an accounting balance**, their quantity and valuation, acquisition origin, responsible custodian, location, status, and supporting documents **as of a specified date**.

This is an accounting and auditability system, not a mission planning, targeting, or operational tracking system. Keep operationally sensitive data out of prompts, test fixtures, logs, and committed artifacts.

## 2. Existing developer environment

- Local repository already exists; dependencies and `uv` are already configured.
- Development is done locally in VS Code with the Codex extension.
- Python application: Flask; persistence: PostgreSQL + SQLAlchemy 2.x; migrations: Alembic; tooling: `uv`, `pytest`, `ruff` (verify actual installed versions and commands before changing config).
- PostgreSQL may run in Docker while Flask runs locally through `uv`.
- **Do not reinitialize the repository, replace `pyproject.toml`, install a parallel package manager, or rewrite existing working Excel workflows unless explicitly requested.**
- Inspect actual files, modules, conventions and tests before proposing edits. Adapt examples to the real repository structure.

## 3. Architecture and dependency rule

Use pragmatic Domain-Driven Design / Ports & Adapters inspired by *Architecture Patterns with Python* (Percival & Gregory), with a small number of meaningful layers:

```text
entrypoints (Flask routes, CLI, Excel import triggers)
    -> service_layer (use cases / commands, transaction coordination)
        -> domain (entities, value objects, invariants, domain exceptions)
service_layer -> ports (repository + unit-of-work contracts)
adapters (SQLAlchemy persistence, Excel readers/writers) implement ports
```

- **Domain must not import Flask, SQLAlchemy, Alembic, pandas, openpyxl, or web-specific objects.** Use plain Python classes/dataclasses and `Decimal`-based value objects as appropriate.
- Domain objects enforce invariants and valid transitions; services coordinate repositories, transaction boundaries, and domain operations.
- Use repository interfaces around **aggregate access**, not one repository per database table.
- Use one Unit of Work per application command / atomic transaction. The production UoW coordinates SQLAlchemy `Session`, commit, and rollback. Prefer explicit commit on successful commands; no hidden commits in repositories.
- SQLAlchemy 2.x may use **imperative mapping** to keep domain objects persistence-agnostic. Evaluate mapping of the first real aggregate before committing to this technique globally. Explicit domain↔ORM mapping is an acceptable alternative when simpler; do not invent duplicate models without a reason.
- `DeclarativeBase` is suitable for persistence models if using separate domain objects; it **must not force** the domain to inherit ORM base classes.
- Avoid event sourcing, generic CRUD frameworks, broad abstractions, microservices, CQRS infrastructure, or separate bounded contexts until justified by a concrete scenario.
- Keep master-data CRUD simple. Model receipts, transfers, write-offs, depreciation, and posting as explicit business operations.

## 4. Working domain rules — current decisions

The detailed model, constraints, and open questions are in `docs/architecture/domain-model.md`. In particular:

1. Support **serial-tracked units** for FPV drones **and** reusable unmanned aircraft complexes (БпАК), regardless of their different accounting classifications.
2. Serial numbers are business identifiers, **not primary keys**. Give each individually tracked unit an immutable internal ID. Supplier-assigned numbers may be absent; authorized personnel may assign internal serial numbers.
3. Do not assume supplier serial numbers are globally unique. Propose and test a uniqueness namespace/policy before implementing its database constraint.
4. A receipt document contains lines. For the confirmed current scenario, all units in one homogeneous receipt line have the **same unit acquisition cost**. Keep that cost on `ReceiptLine`; link each serial-tracked `AssetUnit` to its originating line.
5. Original receipt lines and links remain immutable in history after posting; **never delete a serial number from a list/array upon transfer or write-off**. Record movements and documents; written-off units remain searchable.
6. Non-serialized accessories and consumables may require quantity-tracked stock rather than one `AssetUnit` per piece. Confirm the tracking policy per nomenclature.
7. Distinguish dimensions: accounting classification, recognition (on/off balance), condition, operational/lifecycle state, location, custody, funding origin, and accounting analytics. Do not collapse all dimensions into one `status`.
8. Physical movement is not equivalent to an accounting entry. Depreciation can change carrying value without physical movement; warehouse transfer need not change the unit's total carrying value.
9. Preserve historical, date-effective traceability: what was held, where, by whom, on what basis, at which recorded value, **as of a date**.
10. Accounting rules, allowed transitions, posting mappings, valuation, depreciation, document requirements, and codes must be confirmed from approved business rules. **Do not invent regulations or accounting entries.**

## 5. Money, quantity, and dates

- Never use binary floating point for money. Use `decimal.Decimal`, explicit precision and rounding policies; store fixed-decimal monetary amounts in PostgreSQL `NUMERIC` where appropriate.
- Monetary allocation and rounding across individual units must be specified and tested. A homogeneous line may have `total = quantity × unit_price`, with exceptions handled as explicit business rules, not hidden adjustments.
- Quantities have units of measure and must be validated. Do not aggregate incompatible units.
- Distinguish document date, effective accounting date, and record creation time where relevant. Avoid silently overwriting historical facts.

## 6. TDD and test pyramid

Default development loop: **Red → Green → Refactor**.

1. Write a failing **domain unit test** for a confirmed business rule, independent of DB/network/Flask.
2. Implement the minimum domain behavior.
3. Test service orchestration with **FakeRepository** and **FakeUnitOfWork**.
4. Add focused PostgreSQL integration tests for SQLAlchemy mappings, constraints, repositories, UoW, rollback, and migrations.
5. Add a small number of Flask/Excel E2E or characterization tests for critical workflows.

- Do not mock plain domain objects; use actual domain entities. Fake infrastructure boundaries rather than SQLAlchemy internals.
- Integration tests must use an **isolated test database**, never development/production data. Do not require PostgreSQL for ordinary unit tests.
- Critical concurrency and uniqueness constraints require database integration tests in addition to unit tests.
- Keep tests deterministic, small, readable, and free of real-world military identifiers and records.
- For existing Excel generation, preserve externally observable output via characterization tests using **synthetic** workbooks. Validate meaningful sheets/cells/formulas/totals rather than binary equality of `.xlsx` files.
- Do not target arbitrary coverage percentages in place of meaningful behavioral tests.

## 7. First vertical slice (unless user changes priority)

**Register a receipt of serial-tracked FPV drones**:

- One receipt document with one homogeneous line (example: 27 units of a named nomenclature at an identical unit cost).
- Validate quantity, list of serial identifiers, duplicates according to the approved namespace, and monetary total.
- Generate individual `AssetUnit` identities and preserve each link to its `ReceiptLine`.
- Record receipt provenance and applicable accounting analytics at the correct level.
- Enforce atomic persistence via UoW; handle uniqueness races in PostgreSQL.
- Cover domain behavior and service orchestration with fast tests **before** database integration and Flask UI.

Do not assume an unverified chart of accounts, mandatory document list, or specific posting rules in this slice.

## 8. Suggested organization (adapt to existing repository)

```text
src/maas_ua/
  domain/                # plain Python entities and value objects
  service_layer/         # commands, services, UoW protocol
  adapters/              # SQLAlchemy repositories/mapping, Excel adapters
  entrypoints/           # Flask blueprints and optional CLI
  bootstrap.py           # dependency assembly
migrations/              # Alembic
tests/
  unit/
  service_layer/
  integration/
  e2e/
docs/architecture/
```

Treat this as a guideline, not permission to move all existing files at once.

## 9. Codex task discipline

For each change:

1. Inspect the repository and relevant documentation first.
2. State a brief implementation plan and which assumptions need confirmation.
3. Work in one small vertical slice; avoid speculative scaffolding.
4. Write/adjust tests first when changing business logic.
5. Run the appropriate existing commands (for example `uv run pytest ...`, `uv run ruff check .`) after checking the actual project configuration.
6. Report changed files, observed test results, unresolved questions, and any behavior or schema migration impacts.
7. Update `docs/architecture/domain-model.md` or an ADR **only after** a domain decision is agreed; explicitly label proposals as proposals.

Do not commit, push, migrate shared databases, overwrite fixtures, or expose data without explicit instruction. Do not change audit history by destructive updates.

## 10. Data handling and security

Use synthetic data only in AI prompts, generated examples, automated tests, committed fixtures, and public documentation. Never upload actual military-unit identifiers, serial numbers, warehouse locations, responsible-person details, financial records, or operational information to cloud AI services without explicit authorization and approved handling procedures. Keep `.env`, database dumps, imported spreadsheets, exports, and logs containing real data out of Git. Use least-privilege credentials and separate dev/test/production environments.

## 11. References and precedence

- Domain model: `docs/architecture/domain-model.md`.
- Existing repository code and tests describe *current behavior*, not necessarily approved business rules.
- Approved user decisions and verified accounting requirements override illustrative examples.
- When requirements are missing, write a focused testable proposal and ask before implementing irreversible accounting behavior.
