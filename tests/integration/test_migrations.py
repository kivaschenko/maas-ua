from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, create_engine, inspect, text

pytestmark = pytest.mark.integration


def test_upgrade_downgrade_and_metadata_agree(database_url, migration_config):
    engine = create_engine(database_url, hide_parameters=True)
    try:
        # Historical migrations retain their original reversible behavior.
        command.upgrade(migration_config, "0004")
        command.downgrade(migration_config, "base")
        command.upgrade(migration_config, "head")
        with engine.connect() as connection:
            assert set(inspect(connection).get_table_names()) == {
                "alembic_version",
                "receipt_documents",
                "receipt_lines",
                "asset_units",
                "nomenclatures",
                "movement_documents",
                "movement_lines",
                "write_off_documents",
                "write_off_lines",
                "accounting_charts",
                "accounting_chart_versions",
                "accounting_account_definitions",
                "accounting_chart_supersessions",
            }
            assert connection.scalar(text("SELECT version_num FROM alembic_version"))
        command.check(migration_config)
        with pytest.raises(RuntimeError, match="forward-only"):
            command.downgrade(migration_config, "0006")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
    finally:
        engine.dispose()


def test_inventory_migration_preserves_legacy_rows_without_guessing_facts(database_url, migration_config):
    command.upgrade(migration_config, "0004")
    engine = create_engine(database_url, hide_parameters=True)
    ids = {name: uuid4() for name in ("receipt", "line", "asset", "movement")}
    try:
        metadata = MetaData()
        metadata.reflect(engine)
        tables = metadata.tables
        with engine.begin() as connection:
            connection.execute(
                tables["nomenclatures"].insert(),
                {
                    "id": "EXAMPLE",
                    "code": "EXAMPLE",
                    "name": "Example drone",
                    "tracking_policy": "SERIALIZED",
                    "unit_of_measure": "pcs",
                },
            )
            connection.execute(
                text("""INSERT INTO receipt_documents
                (id, document_number, document_date, effective_date, created_at, source)
                VALUES (:id, 'EXAMPLE', '2026-01-01', '2026-01-01', '2026-01-02T00:00:00Z', 'EXAMPLE')"""),
                {"id": ids["receipt"]},
            )
            connection.execute(
                tables["receipt_lines"].insert(),
                {
                    "id": ids["line"],
                    "receipt_id": ids["receipt"],
                    "position": 0,
                    "nomenclature_id": "EXAMPLE",
                    "tracking_policy": "SERIALIZED",
                    "quantity_count": 1,
                    "quantity_unit": "pcs",
                    "unit_price": 14500,
                    "currency": "UAH",
                    "analytics": [],
                },
            )
            connection.execute(
                text("""INSERT INTO asset_units
                (id, receipt_line_id, serial_namespace, serial_value, custody_location_ref, custody_party_ref,
                 lifecycle_state, last_effective_date)
                VALUES (:id, :line, 'EXAMPLE', 'EXAMPLE-1', 'EXAMPLE-B', 'EXAMPLE-B', 'ACTIVE', '2026-02-01')"""),
                {"id": ids["asset"], "line": ids["line"]},
            )
            connection.execute(
                text("""INSERT INTO movement_documents
                (id, document_number, document_date, effective_date, created_at,
                 source_location_ref, source_party_ref, destination_location_ref, destination_party_ref)
                VALUES (:id, 'EXAMPLE', '2026-02-01', '2026-02-01', '2026-02-02T00:00:00Z',
                        'EXAMPLE-A', 'EXAMPLE-A', 'EXAMPLE-B', 'EXAMPLE-B')"""),
                {"id": ids["movement"]},
            )
            connection.execute(
                tables["movement_lines"].insert(),
                {
                    "movement_id": ids["movement"],
                    "asset_unit_id": ids["asset"],
                    "position": 0,
                },
            )
            before = {
                name: dict(connection.execute(table.select()).mappings().one())
                for name, table in tables.items()
                if name != "alembic_version"
            }
        command.upgrade(migration_config, "head")
        with engine.connect() as connection:
            for name, previous in before.items():
                after = dict(connection.execute(text(f"SELECT * FROM {name}")).mappings().one())
                assert {key: after[key] for key in previous} == previous
                if name == "asset_units":
                    assert after["initial_location_ref"] is None and after["initial_party_ref"] is None
                if name == "receipt_lines":
                    assert after["accounting_classification"] is None
                if name == "movement_documents":
                    assert after["registration_order"] is None
    finally:
        engine.dispose()


def test_historical_inventory_migration_remains_forward_only(database_url, migration_config):
    command.upgrade(migration_config, "0005")
    with pytest.raises(RuntimeError, match="0005 is forward-only"):
        command.downgrade(migration_config, "0004")


def test_historical_write_off_migration_remains_forward_only(database_url, migration_config):
    command.upgrade(migration_config, "0006")
    with pytest.raises(RuntimeError, match="0006 is forward-only"):
        command.downgrade(migration_config, "0005")
