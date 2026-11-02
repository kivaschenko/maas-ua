from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from maas_ua.adapters.orm import (
    AssetUnitRecord,
    ReceiptDocumentRecord,
    ReceiptLineRecord,
)
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.domain.receipts import (
    AssetUnit,
    DuplicateSerialNumber,
    InvalidReceipt,
    Money,
    Nomenclature,
    Quantity,
    ReceiptDocument,
    SerialNumber,
    TrackingPolicy,
    register_serialized_receipt,
)
from maas_ua.ports import RepositoryConflict
from maas_ua.service_layer.receipts import (
    RegisterInventoryReceipt,
    register_inventory_receipt,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def command(session_factory):
    command = RegisterInventoryReceipt(
        document_number="EXAMPLE-125",
        document_date=date(2026, 1, 15),
        effective_date=date(2026, 1, 16),
        source="EXAMPLE-SOURCE",
        nomenclature=Nomenclature(
            "EXAMPLE-FPV", TrackingPolicy.SERIALIZED, code="EXAMPLE-FPV", name="Example drone", unit_of_measure="pcs"
        ),
        quantity=Quantity(27, "pcs"),
        unit_price=Money(Decimal("14500.00"), "UAH"),
        line_total=Money(Decimal("391500.00"), "UAH"),
        serial_numbers=tuple(SerialNumber("EXAMPLE-ISSUER", f"EXAMPLE-{i:04}") for i in range(27)),
        analytics=(("z-example", "EXAMPLE-Z"), ("a-example", "EXAMPLE-A")),
    )
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        uow.nomenclatures.add(command.nomenclature)
        uow.commit()
    return command


def build(command):
    return register_serialized_receipt(**vars(command), created_at=datetime(2026, 1, 17, tzinfo=UTC))


def stage(uow, result):
    uow.receipts.add(result.receipt)
    for unit in result.units:
        uow.asset_units.add(unit)


def counts(session_factory):
    with session_factory() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (ReceiptDocumentRecord, ReceiptLineRecord, AssetUnitRecord)
        )


def test_registers_and_reloads_entire_receipt_in_new_session(command, session_factory):
    created_at = datetime(2026, 1, 17, tzinfo=UTC)
    result = register_inventory_receipt(command, SqlAlchemyUnitOfWork(session_factory), clock=lambda: created_at)
    assert counts(session_factory) == (1, 1, 27)

    with SqlAlchemyUnitOfWork(session_factory) as reader:
        receipt = reader.receipts.get(result.receipt.id)
        units = tuple(reader.asset_units.get(unit.id) for unit in result.units)
        assert reader.receipts.get(uuid4()) is None
        assert reader.asset_units.get(uuid4()) is None
        assert reader.asset_units.contains_serial(command.serial_numbers[0])
        assert not reader.asset_units.contains_serial(SerialNumber("EXAMPLE", "ABSENT"))

    # Domain snapshots remain usable and immutable after the session closes.
    assert receipt == result.receipt
    assert units == result.units
    assert receipt.lines[0].total == command.line_total
    assert receipt.created_at == created_at
    assert {unit.receipt_line_id for unit in units} == {receipt.lines[0].id}
    with pytest.raises(FrozenInstanceError):
        receipt.source = "CHANGED"
    with pytest.raises(FrozenInstanceError):
        units[0].receipt_line_id = uuid4()


def test_receipt_relationship_preserves_multiple_line_order(command, session_factory):
    first = build(command)
    second = build(command)
    # Deliberately reverse UUID order: persistence order is the domain tuple order.
    line_a = replace(first.receipt.lines[0], id=UUID(int=200))
    line_b = replace(second.receipt.lines[0], id=UUID(int=100))
    receipt = replace(first.receipt, lines=(line_a, line_b))
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.receipts.add(receipt)
        writer.commit()
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        loaded = reader.receipts.get(receipt.id)
    assert loaded == receipt


@pytest.mark.parametrize("price", ["0", "0.12345", "12345678901234567890.123456789012345"])
def test_money_round_trip_never_quantizes(command, session_factory, price):
    unit_price = Money(Decimal(price), "UAH")
    result = register_inventory_receipt(
        replace(command, unit_price=unit_price, line_total=unit_price.multiply(27)),
        SqlAlchemyUnitOfWork(session_factory),
    )
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.receipts.get(result.receipt.id) == result.receipt


@pytest.mark.parametrize("change", ["namespace", "serial_case", "namespace_case"])
def test_serial_uniqueness_uses_exact_explicit_namespace(command, session_factory, change):
    register_inventory_receipt(command, SqlAlchemyUnitOfWork(session_factory))
    serials = tuple(
        SerialNumber(
            "OTHER-EXAMPLE"
            if change == "namespace"
            else (s.namespace.lower() if change == "namespace_case" else s.namespace),
            s.value.lower() if change == "serial_case" else s.value,
        )
        for s in command.serial_numbers
    )
    register_inventory_receipt(replace(command, serial_numbers=serials), SqlAlchemyUnitOfWork(session_factory))
    assert counts(session_factory) == (2, 2, 54)


def test_duplicate_registration_keeps_existing_receipt(command, session_factory):
    original = register_inventory_receipt(command, SqlAlchemyUnitOfWork(session_factory))
    with pytest.raises(DuplicateSerialNumber):
        register_inventory_receipt(command, SqlAlchemyUnitOfWork(session_factory))
    assert counts(session_factory) == (1, 1, 27)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.receipts.get(original.receipt.id) == original.receipt


def test_document_number_is_not_an_invented_uniqueness_rule(command, session_factory):
    register_inventory_receipt(command, SqlAlchemyUnitOfWork(session_factory))
    other = replace(
        command,
        serial_numbers=tuple(SerialNumber("OTHER-EXAMPLE", s.value) for s in command.serial_numbers),
    )
    register_inventory_receipt(other, SqlAlchemyUnitOfWork(session_factory))
    assert counts(session_factory) == (2, 2, 54)


def test_interleaved_prechecks_cannot_bypass_database_serial_uniqueness(command, session_factory):
    first = build(command)
    second = build(replace(command, document_number="EXAMPLE-126"))
    with (
        SqlAlchemyUnitOfWork(session_factory) as winner,
        SqlAlchemyUnitOfWork(session_factory) as loser,
    ):
        assert not winner.asset_units.contains_serial(first.units[-1].serial_number)
        assert not loser.asset_units.contains_serial(second.units[-1].serial_number)
        stage(winner, first)
        stage(loser, second)
        winner.commit()
        with pytest.raises(DuplicateSerialNumber):
            loser.commit()
    assert counts(session_factory) == (1, 1, 27)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.receipts.get(second.receipt.id) is None


@pytest.mark.parametrize("identity", ["receipt", "line", "unit"])
def test_database_identity_conflict_cannot_overwrite_history(command, session_factory, identity):
    first = register_inventory_receipt(command, SqlAlchemyUnitOfWork(session_factory))
    second = build(
        replace(
            command,
            serial_numbers=tuple(SerialNumber("OTHER-EXAMPLE", s.value) for s in command.serial_numbers),
        )
    )
    if identity == "receipt":
        second = replace(second, receipt=replace(second.receipt, id=first.receipt.id))
    elif identity == "line":
        line = replace(second.receipt.lines[0], id=first.receipt.lines[0].id)
        second = replace(
            second,
            receipt=replace(second.receipt, lines=(line,)),
            units=tuple(replace(unit, receipt_line_id=line.id) for unit in second.units),
        )
    else:
        second = replace(
            second,
            units=(*second.units[:-1], replace(second.units[-1], id=first.units[0].id)),
        )
    with (
        pytest.raises(RepositoryConflict),
        SqlAlchemyUnitOfWork(session_factory) as writer,
    ):
        stage(writer, second)
        writer.commit()
    assert counts(session_factory) == (1, 1, 27)


def test_uncommitted_exit_rolls_back_even_flushed_rows(command, session_factory):
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        stage(writer, build(command))
        writer.session.flush()
        assert counts(session_factory) == (0, 0, 0)
    assert counts(session_factory) == (0, 0, 0)


def test_unexpected_failure_after_flush_rolls_back(command, session_factory):
    with (
        pytest.raises(RuntimeError, match="synthetic failure"),
        SqlAlchemyUnitOfWork(session_factory) as writer,
    ):
        stage(writer, build(command))
        writer.session.flush()
        raise RuntimeError("synthetic failure")
    assert counts(session_factory) == (0, 0, 0)


def test_explicit_rollback_discards_staged_aggregates(command, session_factory):
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        stage(writer, build(command))
        writer.session.flush()
        writer.rollback()
        writer.commit()
    assert counts(session_factory) == (0, 0, 0)


def test_foreign_key_failure_rolls_back_all_rows(command, session_factory):
    result = build(command)
    with (
        pytest.raises(RepositoryConflict),
        SqlAlchemyUnitOfWork(session_factory) as writer,
    ):
        stage(writer, result)
        writer.asset_units.add(AssetUnit(uuid4(), SerialNumber("EXAMPLE", "ORPHAN"), uuid4()))
        writer.commit()
    assert counts(session_factory) == (0, 0, 0)


def test_validation_failure_does_not_open_a_transaction(command, session_factory):
    writer = SqlAlchemyUnitOfWork(session_factory)
    with pytest.raises(InvalidReceipt):
        register_inventory_receipt(replace(command, serial_numbers=()), writer)
    assert counts(session_factory) == (0, 0, 0)


def test_uow_rejects_nested_entry_and_commit_outside_context(session_factory):
    writer = SqlAlchemyUnitOfWork(session_factory)
    with pytest.raises(RuntimeError):
        writer.commit()
    with writer:
        with pytest.raises(RuntimeError):
            writer.__enter__()
        writer.commit()
        with pytest.raises(RuntimeError):
            writer.commit()
    with pytest.raises(RuntimeError):
        writer.commit()


def test_database_constraint_rejects_serial_duplicate_without_service_precheck(command, session_factory):
    result = register_inventory_receipt(command, SqlAlchemyUnitOfWork(session_factory))
    with session_factory() as session:
        serial = result.units[0].serial_number
        session.add(
            AssetUnitRecord(
                id=uuid4(),
                receipt_line_id=result.receipt.lines[0].id,
                serial_namespace=serial.namespace,
                serial_value=serial.value,
            )
        )
        with pytest.raises(IntegrityError) as error:
            session.commit()
        assert error.value.orig.diag.constraint_name == "uq_asset_units_serial"
        session.rollback()
    assert counts(session_factory) == (1, 1, 27)


def test_domain_classes_have_no_orm_instrumentation():
    assert not hasattr(ReceiptDocument, "_sa_class_manager")
    assert not hasattr(AssetUnit, "_sa_class_manager")


def test_repositories_read_staged_additions_without_flushing(command, session_factory):
    result = build(command)
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        stage(writer, result)
        assert writer.receipts.get(result.receipt.id) == result.receipt
        assert writer.asset_units.get(result.units[0].id) == result.units[0]
        assert writer.asset_units.contains_serial(result.units[0].serial_number)
        assert len(writer.session.new) == 29
        assert counts(session_factory) == (0, 0, 0)
    assert counts(session_factory) == (0, 0, 0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity_count", 0),
        ("quantity_count", -1),
        ("quantity_unit", "kg"),
        ("tracking_policy", "QUANTITY"),
        ("unit_price", Decimal(-1)),
        ("unit_price", Decimal("NaN")),
        ("unit_price", Decimal("Infinity")),
        ("unit_price", Decimal("-Infinity")),
    ],
)
def test_database_rejects_invalid_line_values(command, session_factory, field, value):
    from maas_ua.adapters.orm import receipt_to_record

    record = receipt_to_record(build(command).receipt)
    setattr(record.lines[0], field, value)
    with session_factory() as session:
        session.add(record)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    assert counts(session_factory) == (0, 0, 0)


def test_uow_can_be_reused_after_failure_and_success(command, session_factory):
    writer = SqlAlchemyUnitOfWork(session_factory)
    first = register_inventory_receipt(command, writer)
    with pytest.raises(DuplicateSerialNumber):
        register_inventory_receipt(command, writer)
    with writer:
        assert writer.receipts.get(first.receipt.id) == first.receipt
        writer.commit()
    assert counts(session_factory) == (1, 1, 27)


def test_service_commit_failure_preserves_previous_data(command, session_factory):
    original = register_inventory_receipt(command, SqlAlchemyUnitOfWork(session_factory))
    other = replace(
        command,
        serial_numbers=tuple(SerialNumber("OTHER-EXAMPLE", serial.value) for serial in command.serial_numbers),
    )
    ids = iter([*(uuid4() for _ in range(28)), original.units[-1].id])
    with pytest.raises(RepositoryConflict):
        register_inventory_receipt(other, SqlAlchemyUnitOfWork(session_factory), new_id=lambda: next(ids))
    assert counts(session_factory) == (1, 1, 27)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.receipts.get(original.receipt.id) == original.receipt
