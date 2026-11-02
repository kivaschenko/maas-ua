from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from maas_ua.adapters.memory import FakeStore, FakeUnitOfWork
from maas_ua.domain.custody import CustodyAssignment
from maas_ua.domain.movements import DuplicateMovement, InvalidTransfer
from maas_ua.domain.receipts import (
    Money,
    Nomenclature,
    Quantity,
    SerialNumber,
    TrackingPolicy,
    register_serialized_receipt,
)
from maas_ua.ports import RepositoryConflict
from maas_ua.service_layer.movements import TransferAssets, transfer_assets

SOURCE = CustodyAssignment("EXAMPLE-LOCATION-A", "EXAMPLE-PARTY-A")
DESTINATION = CustodyAssignment("EXAMPLE-LOCATION-B", "EXAMPLE-PARTY-B")


def receipt():
    return register_serialized_receipt(
        document_number="EXAMPLE-RECEIPT",
        document_date=date(2026, 1, 1),
        effective_date=date(2026, 1, 1),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        source="EXAMPLE",
        nomenclature=Nomenclature(
            "EXAMPLE", TrackingPolicy.SERIALIZED, code="EXAMPLE", name="Example drone", unit_of_measure="pcs"
        ),
        quantity=Quantity(27, "pcs"),
        unit_price=Money(Decimal("12.34"), "UAH"),
        line_total=Money(Decimal("333.18"), "UAH"),
        serial_numbers=tuple(SerialNumber("EXAMPLE", str(i)) for i in range(27)),
        initial_custody=SOURCE,
    )


def movement(units):
    return TransferAssets(
        movement_id=uuid4(),
        document_number="EXAMPLE-MOVE",
        document_date=date(2026, 2, 1),
        effective_date=date(2026, 2, 1),
        source=SOURCE,
        destination=DESTINATION,
        asset_ids=tuple(u.id for u in units[:12]),
    )


@pytest.fixture
def stored():
    result = receipt()
    store = FakeStore(nomenclatures=(result.receipt.lines[0].nomenclature,))
    with FakeUnitOfWork(store) as uow:
        uow.receipts.add(result.receipt)
        for unit in result.units:
            uow.asset_units.add(unit)
        uow.commit()
    return store, result


def test_loads_all_selected_units_and_commits_once_preserving_original_receipt(stored):
    store, original = stored
    uow = FakeUnitOfWork(store)
    command = movement(original.units)
    now = datetime(2026, 2, 2, tzinfo=UTC)
    result = transfer_assets(command, uow, clock=lambda: now)
    assert uow.commit_count == 1
    assert result.movement.created_at == now
    assert store.receipts[original.receipt.id] == original.receipt
    assert len(store.movements) == 1
    with FakeUnitOfWork(store) as reader:
        assert reader.movements.get(command.movement_id) == result.movement
        assert reader.movements.get(uuid4()) is None
        for unit in original.units:
            expected = DESTINATION if unit.id in command.asset_ids else SOURCE
            assert reader.asset_units.get(unit.id).custody == expected


@pytest.mark.parametrize(
    "failure", ["missing", "source", "duplicate_ids", "equal", "date", "unknown_state", "unassigned"]
)
def test_failed_multi_unit_validation_never_changes_or_commits_any_unit(stored, failure):
    store, original = stored
    command = movement(original.units)
    if failure == "missing":
        command = replace(command, asset_ids=(*command.asset_ids[:-1], uuid4()))
    elif failure == "source":
        command = replace(command, source=CustodyAssignment("OTHER", "OTHER"))
    elif failure == "duplicate_ids":
        command = replace(command, asset_ids=(*command.asset_ids, command.asset_ids[0]))
    elif failure == "equal":
        command = replace(command, destination=SOURCE)
    elif failure == "date":
        command = replace(command, effective_date=date(2025, 12, 31))
    else:
        with FakeUnitOfWork(store) as writer:
            unit = original.units[11]
            updated = replace(unit, **({"lifecycle_state": None} if failure == "unknown_state" else {"custody": None}))
            writer.asset_units.save(updated)
            writer.commit()
    before = dict(store.asset_units)
    uow = FakeUnitOfWork(store)
    with pytest.raises(InvalidTransfer):
        transfer_assets(command, uow)
    assert uow.commit_count == 0
    assert store.asset_units == before
    assert not store.movements


def test_duplicate_document_is_rejected_even_after_source_custody_changes(stored):
    store, original = stored
    command = movement(original.units)
    result = transfer_assets(command, FakeUnitOfWork(store))
    failed = FakeUnitOfWork(store)
    with pytest.raises(DuplicateMovement):
        transfer_assets(command, failed)
    assert failed.commit_count == 0
    assert store.movements == {command.movement_id: result.movement}


def test_failure_after_first_staged_update_rolls_back_everything(stored):
    store, original = stored

    class FailingUnitOfWork(FakeUnitOfWork):
        def __enter__(self):
            super().__enter__()
            save = self.asset_units.save
            calls = 0

            def fail(unit):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("synthetic failure")
                save(unit)

            self.asset_units.save = fail
            return self

    failed = FailingUnitOfWork(store)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        transfer_assets(movement(original.units), failed)
    assert failed.commit_count == 0
    assert not store.movements
    assert store.asset_units == {u.id: u for u in original.units}


def test_fake_concurrent_snapshot_cannot_overwrite_a_transfer(stored):
    store, original = stored
    with FakeUnitOfWork(store) as stale:
        unit = stale.asset_units.get(original.units[0].id)
        transfer_assets(movement(original.units), FakeUnitOfWork(store))
        stale.asset_units.save(replace(unit, custody=CustodyAssignment("OTHER", "OTHER")))
        with pytest.raises(RepositoryConflict, match="store changed"):
            stale.commit()
