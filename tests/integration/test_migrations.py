import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.integration


def test_upgrade_downgrade_and_metadata_agree(database_url, migration_config):
    engine = create_engine(database_url, hide_parameters=True)
    try:
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
            }
            assert connection.scalar(text("SELECT version_num FROM alembic_version"))
        command.check(migration_config)
        command.downgrade(migration_config, "base")
        with engine.connect() as connection:
            assert set(inspect(connection).get_table_names()) == {"alembic_version"}
        command.upgrade(migration_config, "head")
        command.check(migration_config)
    finally:
        engine.dispose()
