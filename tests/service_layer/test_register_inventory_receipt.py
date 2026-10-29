from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from maas_ua.adapters.memory import FakeStore, FakeUnitOfWork
from maas_ua.domain.receipts import (
    DuplicateSerialNumber,
    InvalidReceipt,
    Money,
    Nomenclature,
    Quantity,
    SerialNumber,
    TrackingPolicy,
)
from maas_ua.ports import RepositoryConflict
from maas_ua.service_layer.receipts import (
    RegisterInventoryReceipt,
    register_inventory_receipt,
)


@pytest.fixture
def command():
    return RegisterInventoryReceipt(
        document_number="EXAMPLE-125",
        document_date=date(2026, 1, 15),
        effective_date=date(2026, 1, 16),
        source="EXAMPLE-SOURCE",
        nomenclature=Nomenclature("EXAMPLE-FPV", TrackingPolicy.SERIALIZED),
        quantity=Quantity(27, "pcs"),
        unit_price=Money(Decimal("14500.00"), "UAH"),
        line_total=Money(Decimal("391500.00"), "UAH"),
        serial_numbers=tuple(
            SerialNumber("EXAMPLE", f"EXAMPLE-{i}") for i in range(27)
        ),
        analytics=(("example-dimension", "EXAMPLE-CODE"),),
    )


def test_persists_document_and_all_units_and_commits_once(command):
    store = FakeStore()
    uow = FakeUnitOfWork(store)
    created_at = datetime(2026, 1, 17, tzinfo=UTC)

    result = register_inventory_receipt(command, uow, clock=lambda: created_at)

    assert uow.commit_count == 1
    assert store.receipts == {result.receipt.id: result.receipt}
    assert store.asset_units == {unit.id: unit for unit in result.units}
    assert result.receipt.created_at == created_at
    assert len(store.asset_units) == 27
    with FakeUnitOfWork(store) as reader:
        assert reader.receipts.get(result.receipt.id) == result.receipt
        for unit in result.units:
            assert reader.asset_units.get(unit.id) == unit
            assert reader.asset_units.contains_serial(unit.serial_number)
            assert unit.receipt_line_id == result.receipt.lines[0].id


@pytest.mark.parametrize(
    "changes",
    [
        {"serial_numbers": ()},
        {"line_total": Money(Decimal(1), "UAH")},
        {"source": ""},
        {"serial_numbers": (SerialNumber("EXAMPLE", "REPEATED"),) * 27},
    ],
)
def test_validation_failure_never_commits_or_leaves_partial_data(command, changes):
    store = FakeStore()
    uow = FakeUnitOfWork(store)
    with pytest.raises(InvalidReceipt):
        register_inventory_receipt(replace(command, **changes), uow)
    assert uow.commit_count == 0
    assert not store.receipts
    assert not store.asset_units


def test_existing_serial_conflict_preserves_previous_registration(command):
    store = FakeStore()
    original = register_inventory_receipt(command, FakeUnitOfWork(store))
    uow = FakeUnitOfWork(store)
    conflicting = replace(command, document_number="EXAMPLE-126")

    with pytest.raises(DuplicateSerialNumber):
        register_inventory_receipt(conflicting, uow)

    assert uow.commit_count == 0
    assert store.receipts == {original.receipt.id: original.receipt}
    assert store.asset_units == {unit.id: unit for unit in original.units}


def test_same_serial_text_in_another_namespace_can_be_registered(command):
    store = FakeStore()
    register_inventory_receipt(command, FakeUnitOfWork(store))
    other = replace(
        command,
        serial_numbers=tuple(
            SerialNumber("OTHER-EXAMPLE", s.value) for s in command.serial_numbers
        ),
    )
    register_inventory_receipt(other, FakeUnitOfWork(store))
    assert len(store.receipts) == 2
    assert len(store.asset_units) == 54


def test_failure_while_adding_units_rolls_back_every_staged_aggregate(command):
    class FailingUnitOfWork(FakeUnitOfWork):
        def __enter__(self):
            super().__enter__()
            original_add = self.asset_units.add
            calls = 0

            def fail_after_one_unit(unit):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("synthetic write failure")
                original_add(unit)

            self.asset_units.add = fail_after_one_unit
            return self

    store = FakeStore()
    uow = FailingUnitOfWork(store)
    with pytest.raises(RuntimeError, match="synthetic write failure"):
        register_inventory_receipt(command, uow)
    assert uow.commit_count == 0
    assert uow.rollback_count == 1
    assert not store.receipts
    assert not store.asset_units


def test_commit_failure_leaves_previous_committed_data_untouched(command):
    class FailingCommit(FakeUnitOfWork):
        def commit(self):
            raise RuntimeError("synthetic commit failure")

    store = FakeStore()
    original = register_inventory_receipt(command, FakeUnitOfWork(store))
    other = replace(
        command,
        serial_numbers=tuple(
            SerialNumber("OTHER-EXAMPLE", s.value) for s in command.serial_numbers
        ),
    )
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        register_inventory_receipt(other, FailingCommit(store))
    assert store.receipts == {original.receipt.id: original.receipt}
    assert store.asset_units == {unit.id: unit for unit in original.units}


def test_leaving_uow_without_explicit_commit_discards_changes(command):
    source_store = FakeStore()
    result = register_inventory_receipt(command, FakeUnitOfWork(source_store))
    store = FakeStore()
    with FakeUnitOfWork(store) as uow:
        uow.receipts.add(result.receipt)
        for unit in result.units:
            uow.asset_units.add(unit)
        assert not store.receipts
        assert not store.asset_units
    assert uow.commit_count == 0
    assert not store.receipts
    assert not store.asset_units


def test_unit_identity_collision_cannot_overwrite_history(command):
    store = FakeStore()
    original = register_inventory_receipt(command, FakeUnitOfWork(store))
    other = replace(
        command,
        serial_numbers=tuple(
            SerialNumber("OTHER-EXAMPLE", s.value) for s in command.serial_numbers
        ),
    )
    ids = iter(
        [UUID(int=1), UUID(int=2), original.units[0].id]
        + [UUID(int=i) for i in range(3, 29)]
    )
    with pytest.raises(RepositoryConflict, match="unit identity"):
        register_inventory_receipt(
            other, FakeUnitOfWork(store), new_id=lambda: next(ids)
        )
    assert store.receipts == {original.receipt.id: original.receipt}
    assert store.asset_units == {unit.id: unit for unit in original.units}


def test_stale_uow_cannot_erase_another_committed_receipt(command):
    store = FakeStore()
    with FakeUnitOfWork(store) as stale:
        result = register_inventory_receipt(command, FakeUnitOfWork(store))
        with pytest.raises(RepositoryConflict, match="store changed"):
            stale.commit()
    assert store.receipts == {result.receipt.id: result.receipt}
    assert store.asset_units == {unit.id: unit for unit in result.units}
