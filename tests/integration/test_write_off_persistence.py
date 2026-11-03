from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from maas_ua.adapters.orm import WriteOffDocumentRecord, WriteOffLineRecord
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.domain.custody import LifecycleState
from maas_ua.domain.write_offs import AlreadyWrittenOff, DuplicateWriteOff, InvalidWriteOff
from maas_ua.service_layer.write_offs import write_off_assets

pytestmark = pytest.mark.integration


@pytest.fixture
def stored(session_factory, write_off_receipt):
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.nomenclatures.add(write_off_receipt.receipt.lines[0].nomenclature)
        writer.receipts.add(write_off_receipt.receipt)
        for unit in write_off_receipt.units:
            writer.asset_units.add(unit)
        writer.commit()
    return write_off_receipt


def test_document_lines_classification_lifecycle_and_distinct_dates_reload(stored, session_factory, write_off_command):
    now = datetime(2026, 3, 1, tzinfo=UTC)
    result = write_off_assets(write_off_command, SqlAlchemyUnitOfWork(session_factory), clock=lambda: now)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.write_offs.get(result.document.id) == result.document
        assert reader.write_offs.get(uuid4()) is None
        assert reader.receipts.get(stored.receipt.id) == stored.receipt
        assert tuple(reader.asset_units.get(unit.id) for unit in result.units) == result.units
        assert reader.asset_units.get(stored.units[8].id).lifecycle_state is LifecycleState.ACTIVE
    assert (
        result.document.document_date,
        result.document.authorization_date,
        result.document.effective_date,
        result.document.created_at,
    ) == (date(2026, 2, 1), date(2026, 2, 3), date(2026, 2, 10), now)


def test_flushed_failure_rolls_back_entire_write_off(stored, session_factory, write_off_command):
    class FailedCommit(SqlAlchemyUnitOfWork):
        def commit(self):
            self.session.flush()
            raise RuntimeError("synthetic failure after flush")

    with pytest.raises(RuntimeError, match="after flush"):
        write_off_assets(write_off_command, FailedCommit(session_factory))
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.write_offs.get(write_off_command.write_off_id) is None
        assert tuple(reader.asset_units.get(unit.id) for unit in stored.units) == stored.units
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WriteOffLineRecord)) == 0


def test_invalid_last_unit_and_duplicate_document_do_not_change_existing_data(
    stored, session_factory, write_off_command
):
    invalid = replace(write_off_command, asset_ids=(*write_off_command.asset_ids[:-1], uuid4()))
    with pytest.raises(InvalidWriteOff):
        write_off_assets(invalid, SqlAlchemyUnitOfWork(session_factory))
    result = write_off_assets(write_off_command, SqlAlchemyUnitOfWork(session_factory))
    with pytest.raises(DuplicateWriteOff):
        write_off_assets(write_off_command, SqlAlchemyUnitOfWork(session_factory))
    with pytest.raises(AlreadyWrittenOff):
        write_off_assets(replace(write_off_command, write_off_id=uuid4()), SqlAlchemyUnitOfWork(session_factory))
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WriteOffDocumentRecord)) == 1
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.write_offs.get(result.document.id) == result.document


@pytest.mark.parametrize("case", ["asset_fk", "document_fk", "position", "duplicate_asset", "duplicate_position"])
def test_write_off_line_constraints(stored, session_factory, write_off_command, case):
    result = write_off_assets(write_off_command, SqlAlchemyUnitOfWork(session_factory))
    values = dict(write_off_id=result.document.id, asset_unit_id=stored.units[8].id, position=8)
    if case == "asset_fk":
        values["asset_unit_id"] = uuid4()
    elif case == "document_fk":
        values["write_off_id"] = uuid4()
    elif case == "position":
        values["position"] = -1
    elif case == "duplicate_asset":
        values["asset_unit_id"] = stored.units[0].id
    else:
        values["position"] = 0
    with session_factory() as session:
        session.add(WriteOffLineRecord(**values))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE receipt_lines SET accounting_classification='UNSUPPORTED'",
        "UPDATE asset_units SET lifecycle_state='UNSUPPORTED'",
        "UPDATE write_off_documents SET authorization_reference=''",
        "UPDATE write_off_documents SET effective_date=authorization_date - 1",
    ],
)
def test_database_classification_lifecycle_and_document_checks(stored, session_factory, write_off_command, sql):
    write_off_assets(write_off_command, SqlAlchemyUnitOfWork(session_factory))
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(text(sql))


@pytest.mark.parametrize(
    "case",
    [
        "write_off_twice",
        "duplicate_document",
        "transfer_after_write_off",
        "write_off_after_transfer",
        "backdated_after_transfer",
    ],
)
def test_concurrent_commands_wait_and_revalidate(stored, session_factory, write_off_command, case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic

    from maas_ua.domain.custody import CustodyAssignment
    from maas_ua.domain.movements import InvalidTransfer
    from maas_ua.service_layer.movements import TransferAssets, transfer_assets

    held, release, started = Event(), Event(), Event()
    backend = []

    class HoldingUow(SqlAlchemyUnitOfWork):
        def commit(self):
            held.set()
            assert release.wait(10), "test failed to release held asset locks"
            super().commit()

    class ObservedUow(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            backend.append(self.session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            return self

    transfer = TransferAssets(
        movement_id=uuid4(),
        document_number="EXAMPLE-MOVE",
        document_date=date(2026, 2, 1),
        effective_date=date(2026, 2, 10),
        source=stored.units[0].custody,
        destination=CustodyAssignment("EXAMPLE-B", "EXAMPLE-B"),
        asset_ids=write_off_command.asset_ids,
    )
    if case in {"write_off_after_transfer", "backdated_after_transfer"}:
        first_handler, first_command = transfer_assets, transfer
        second_handler = write_off_assets
        second_command = (
            replace(write_off_command, effective_date=date(2026, 2, 9))
            if case == "backdated_after_transfer"
            else write_off_command
        )
        expected = InvalidWriteOff if case == "backdated_after_transfer" else None
    else:
        first_handler, first_command = write_off_assets, write_off_command
        second_handler = transfer_assets if case == "transfer_after_write_off" else write_off_assets
        second_command = (
            transfer
            if case == "transfer_after_write_off"
            else replace(
                write_off_command,
                write_off_id=write_off_command.write_off_id if case == "duplicate_document" else uuid4(),
            )
        )
        expected = (
            InvalidTransfer
            if case == "transfer_after_write_off"
            else (DuplicateWriteOff if case == "duplicate_document" else AlreadyWrittenOff)
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_handler, first_command, HoldingUow(session_factory))
        try:
            assert held.wait(5)
            second = pool.submit(second_handler, second_command, ObservedUow(session_factory))
            assert started.wait(5)
            deadline = monotonic() + 5
            blocked = False
            while monotonic() < deadline:
                with session_factory() as observer:
                    blocked = observer.scalar(
                        text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": backend[0]}
                    )
                if blocked:
                    break
                Event().wait(0.01)
            assert blocked and not second.done(), "competing command did not wait on asset locks"
        finally:
            release.set()
        first.result(timeout=5)
        if expected is not None:
            with pytest.raises(expected):
                second.result(timeout=5)
        else:
            second.result(timeout=5)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        expected_state = LifecycleState.ACTIVE if case == "backdated_after_transfer" else LifecycleState.WRITTEN_OFF
        for key in write_off_command.asset_ids:
            unit = reader.asset_units.get(key)
            assert unit.lifecycle_state is expected_state
            if case == "write_off_after_transfer":
                assert unit.custody == transfer.destination
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WriteOffDocumentRecord)) == (
            0 if case == "backdated_after_transfer" else 1
        )


def test_document_uuid_race_on_disjoint_assets_rolls_back_loser(stored, session_factory, write_off_command):
    from maas_ua.domain.write_offs import write_off_assets as build

    def stage(uow, keys):
        units = uow.asset_units.get_for_update(keys)
        facts = uow.asset_units.write_off_facts(units)
        result = build(
            id=write_off_command.write_off_id,
            document_number=write_off_command.document_number,
            document_date=write_off_command.document_date,
            authorization_reference=write_off_command.authorization_reference,
            authorization_date=write_off_command.authorization_date,
            effective_date=write_off_command.effective_date,
            created_at=datetime(2026, 3, 1, tzinfo=UTC),
            asset_ids=keys,
            assets=facts,
        )
        uow.write_offs.add(result.document)
        for unit in result.units:
            uow.asset_units.save(unit)
        return result

    with SqlAlchemyUnitOfWork(session_factory) as first, SqlAlchemyUnitOfWork(session_factory) as second:
        assert first.write_offs.get(write_off_command.write_off_id) is None
        assert second.write_offs.get(write_off_command.write_off_id) is None
        stage(first, write_off_command.asset_ids)
        stage(second, (stored.units[8].id,))
        first.commit()
        with pytest.raises(DuplicateWriteOff):
            second.commit()
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.asset_units.get(stored.units[8].id) == stored.units[8]


def test_database_prevents_second_document_for_same_unit(stored, session_factory, write_off_command):
    result = write_off_assets(write_off_command, SqlAlchemyUnitOfWork(session_factory))
    duplicate = replace(result.document, id=uuid4())
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.write_offs.add(duplicate)
        with pytest.raises(AlreadyWrittenOff):
            writer.commit()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WriteOffDocumentRecord)) == 1


@pytest.mark.parametrize("classification", [None, "FIXED_ASSET"])
def test_persisted_classification_is_required_for_write_off(
    session_factory, write_off_receipt, write_off_command, classification
):
    from maas_ua.domain.receipts import AccountingClassification

    receipt = write_off_receipt
    line = replace(
        receipt.receipt.lines[0],
        accounting_classification=(AccountingClassification(classification) if classification else None),
    )
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.nomenclatures.add(line.nomenclature)
        writer.receipts.add(replace(receipt.receipt, lines=(line,)))
        for unit in receipt.units:
            writer.asset_units.add(unit)
        writer.commit()
    with pytest.raises(InvalidWriteOff, match="classification"):
        write_off_assets(write_off_command, SqlAlchemyUnitOfWork(session_factory))
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.write_offs.get(write_off_command.write_off_id) is None
        assert reader.asset_units.get(receipt.units[0].id) == receipt.units[0]
