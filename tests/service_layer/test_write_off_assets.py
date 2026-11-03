from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from maas_ua.adapters.memory import FakeStore, FakeUnitOfWork
from maas_ua.domain.custody import LifecycleState
from maas_ua.domain.receipts import AccountingClassification
from maas_ua.domain.write_offs import DuplicateWriteOff, InvalidWriteOff
from maas_ua.service_layer.write_offs import write_off_assets


@pytest.fixture
def stored(write_off_receipt):
    store = FakeStore(nomenclatures=(write_off_receipt.receipt.lines[0].nomenclature,))
    with FakeUnitOfWork(store) as uow:
        uow.receipts.add(write_off_receipt.receipt)
        for unit in write_off_receipt.units:
            uow.asset_units.add(unit)
        uow.commit()
    return store


def test_write_off_commits_document_and_units_once(stored, write_off_receipt, write_off_command):
    uow = FakeUnitOfWork(stored)
    now = datetime(2026, 3, 1, tzinfo=UTC)
    result = write_off_assets(write_off_command, uow, clock=lambda: now)
    assert uow.commit_count == 1
    assert stored.write_offs == {result.document.id: result.document}
    assert result.document.created_at == now
    assert stored.receipts[write_off_receipt.receipt.id] == write_off_receipt.receipt
    for unit in write_off_receipt.units:
        expected = (
            replace(unit, lifecycle_state=LifecycleState.WRITTEN_OFF, last_effective_date=date(2026, 2, 10))
            if unit.id in write_off_command.asset_ids
            else unit
        )
        assert stored.asset_units[unit.id] == expected
    with FakeUnitOfWork(stored) as reader:
        assert reader.write_offs.get(result.document.id) == result.document


@pytest.mark.parametrize(
    "failure", ["missing", "duplicates", "already_written_off", "authorization", "unknown", "fixed"]
)
def test_one_invalid_unit_prevents_all_changes(stored, write_off_receipt, write_off_command, failure):
    command = write_off_command
    if failure == "missing":
        command = replace(command, asset_ids=(*command.asset_ids[:-1], uuid4()))
    elif failure == "duplicates":
        command = replace(command, asset_ids=(*command.asset_ids, command.asset_ids[0]))
    elif failure == "already_written_off":
        write_off_assets(
            replace(command, write_off_id=uuid4(), asset_ids=(command.asset_ids[-1],)), FakeUnitOfWork(stored)
        )
    elif failure == "authorization":
        command = replace(command, effective_date=date(2026, 2, 2))
    else:
        # Construct legacy/fixed receipt facts before storing; never mutate posted history.
        receipt = write_off_receipt.receipt
        line = replace(
            receipt.lines[0],
            accounting_classification=None if failure == "unknown" else AccountingClassification.FIXED_ASSET,
        )
        stored = FakeStore(nomenclatures=(line.nomenclature,))
        with FakeUnitOfWork(stored) as writer:
            writer.receipts.add(replace(receipt, lines=(line,)))
            for unit in write_off_receipt.units:
                writer.asset_units.add(unit)
            writer.commit()
    before_units, before_docs = dict(stored.asset_units), dict(stored.write_offs)
    uow = FakeUnitOfWork(stored)
    with pytest.raises(InvalidWriteOff):
        write_off_assets(command, uow)
    assert uow.commit_count == 0
    assert stored.asset_units == before_units and stored.write_offs == before_docs


def test_duplicate_document_is_rejected(stored, write_off_command):
    write_off_assets(write_off_command, FakeUnitOfWork(stored))
    uow = FakeUnitOfWork(stored)
    with pytest.raises(DuplicateWriteOff):
        write_off_assets(write_off_command, uow)
    assert uow.commit_count == 0
    assert len(stored.write_offs) == 1


def test_failure_after_staging_rolls_back_document_and_all_units(stored, write_off_command):
    class FailingUow(FakeUnitOfWork):
        def __enter__(self):
            super().__enter__()
            save = self.asset_units.save
            calls = 0

            def fail(unit):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("synthetic write failure")
                save(unit)

            self.asset_units.save = fail
            return self

    before = dict(stored.asset_units)
    uow = FailingUow(stored)
    with pytest.raises(RuntimeError, match="synthetic"):
        write_off_assets(write_off_command, uow)
    assert uow.commit_count == 0 and not stored.write_offs
    assert stored.asset_units == before
