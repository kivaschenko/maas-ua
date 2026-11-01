from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.domain.receipts import (
    InvalidReceipt,
    Money,
    Nomenclature,
    Quantity,
    SerialNumber,
    TrackingPolicy,
)
from maas_ua.ports import DuplicateNomenclatureCode, RepositoryConflict
from maas_ua.service_layer.receipts import RegisterInventoryReceipt, register_inventory_receipt

pytestmark = pytest.mark.integration


def catalog():
    return Nomenclature(
        "EXAMPLE-ID", TrackingPolicy.SERIALIZED, code="EXAMPLE-CODE", name="Example drone", unit_of_measure="pcs"
    )


def test_catalog_persists_and_retrieves_pending_and_committed_records(session_factory):
    item = catalog()
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.nomenclatures.add(item)
        assert writer.nomenclatures.get(item.id) == item
        assert writer.nomenclatures.get_by_code(item.code) == item
        writer.commit()
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.nomenclatures.get(item.id) == item
        assert reader.nomenclatures.get_by_code(item.code) == item
        assert reader.nomenclatures.get("MISSING") is None
        assert reader.nomenclatures.get_by_code("MISSING") is None


def test_interleaved_catalog_writes_enforce_code_uniqueness(session_factory):
    with SqlAlchemyUnitOfWork(session_factory) as first, SqlAlchemyUnitOfWork(session_factory) as second:
        assert first.nomenclatures.get_by_code(catalog().code) is None
        assert second.nomenclatures.get_by_code(catalog().code) is None
        first.nomenclatures.add(catalog())
        second.nomenclatures.add(replace(catalog(), id="OTHER-ID"))
        first.commit()
        with pytest.raises(DuplicateNomenclatureCode):
            second.commit()


def test_receipt_registration_rejects_absent_catalog_record(session_factory):
    from decimal import Decimal

    receipt = RegisterInventoryReceipt(
        document_number="EXAMPLE",
        document_date=date(2026, 1, 1),
        effective_date=date(2026, 1, 1),
        source="EXAMPLE",
        nomenclature=catalog(),
        quantity=Quantity(1, "pcs"),
        unit_price=Money(Decimal(1), "UAH"),
        line_total=Money(Decimal(1), "UAH"),
        serial_numbers=(SerialNumber("EXAMPLE", "1"),),
    )
    with pytest.raises(InvalidReceipt, match="nomenclature"):
        register_inventory_receipt(receipt, SqlAlchemyUnitOfWork(session_factory))


def test_database_rejects_missing_catalog_reference_without_service(session_factory):
    from decimal import Decimal

    from maas_ua.domain.receipts import register_serialized_receipt

    result = register_serialized_receipt(
        document_number="EXAMPLE",
        document_date=date(2026, 1, 1),
        effective_date=date(2026, 1, 1),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        source="EXAMPLE",
        nomenclature=catalog(),
        quantity=Quantity(1, "pcs"),
        unit_price=Money(Decimal(1), "UAH"),
        line_total=Money(Decimal(1), "UAH"),
        serial_numbers=(SerialNumber("EXAMPLE", "1"),),
    )
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.receipts.add(result.receipt)
        with pytest.raises(RepositoryConflict):
            writer.commit()


def test_existing_receipts_require_reviewed_catalog_without_rewriting_ids(database_url, migration_config):
    command.upgrade(migration_config, "0002")
    engine = create_engine(database_url)
    receipt_id, line_id, unit_id = uuid4(), uuid4(), uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO receipt_documents
                VALUES (:id, 'EXAMPLE', '2026-01-01', '2026-01-01', '2026-01-01T00:00:00Z', 'EXAMPLE')"""),
                {"id": receipt_id},
            )
            connection.execute(
                text("""INSERT INTO receipt_lines
                VALUES (:id, :receipt, 0, 'EXAMPLE-ID', 'SERIALIZED', 1, 'pcs', 1, 'UAH', '[]')"""),
                {"id": line_id, "receipt": receipt_id},
            )
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO asset_units VALUES (:id, :line, 'EXAMPLE', 'EXAMPLE-1')"),
                {"id": unit_id, "line": line_id},
            )
        with pytest.raises(RuntimeError, match="reviewed metadata"):
            command.upgrade(migration_config, "head")
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT nomenclature_id FROM receipt_lines")) == "EXAMPLE-ID"
            connection.execute(
                text("""INSERT INTO nomenclatures
                VALUES ('EXAMPLE-ID', 'EXAMPLE-CODE', 'Example drone', 'pcs', 'SERIALIZED', NULL)""")
            )
        command.upgrade(migration_config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT nomenclature_id FROM receipt_lines")) == "EXAMPLE-ID"
            legacy = connection.execute(
                text(
                    "SELECT receipt_line_id, custody_location_ref, custody_party_ref, "
                    "lifecycle_state, last_effective_date FROM asset_units"
                )
            ).one()
            assert legacy == (line_id, None, None, None, date(2026, 1, 1))
    finally:
        engine.dispose()


def test_catalog_codes_compare_exactly_and_quantity_policy_round_trips(session_factory):
    first = catalog()
    other = replace(
        first,
        id="OTHER",
        code=first.code.lower(),
        tracking_policy=TrackingPolicy.QUANTITY,
        unit_of_measure="m",
        default_accounting_classification="EXAMPLE-CLASSIFICATION",
    )
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.nomenclatures.add(first)
        writer.nomenclatures.add(other)
        writer.commit()
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.nomenclatures.get_by_code(first.code) == first
        assert reader.nomenclatures.get_by_code(other.code) == other
