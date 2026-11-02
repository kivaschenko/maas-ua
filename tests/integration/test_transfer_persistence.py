from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from maas_ua.adapters.orm import AssetUnitRecord, MovementDocumentRecord, MovementLineRecord
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.domain.custody import CustodyAssignment
from maas_ua.domain.movements import DuplicateMovement, InvalidTransfer
from maas_ua.domain.receipts import Money, Nomenclature, Quantity, SerialNumber, TrackingPolicy
from maas_ua.ports import RepositoryConflict
from maas_ua.service_layer.movements import TransferAssets, transfer_assets
from maas_ua.service_layer.receipts import RegisterInventoryReceipt, register_inventory_receipt

pytestmark = pytest.mark.integration
SOURCE = CustodyAssignment("EXAMPLE-LOCATION-A", "EXAMPLE-PARTY-A")
DESTINATION = CustodyAssignment("EXAMPLE-LOCATION-B", "EXAMPLE-PARTY-B")


@pytest.fixture
def stored(session_factory):
    catalog = Nomenclature(
        "EXAMPLE", TrackingPolicy.SERIALIZED, code="EXAMPLE", name="Example drone", unit_of_measure="pcs"
    )
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.nomenclatures.add(catalog)
        writer.commit()
    receipt = register_inventory_receipt(
        RegisterInventoryReceipt(
            document_number="EXAMPLE-RECEIPT",
            document_date=date(2026, 1, 1),
            effective_date=date(2026, 1, 1),
            source="EXAMPLE",
            nomenclature=catalog,
            quantity=Quantity(27, "pcs"),
            unit_price=Money(Decimal("12.34"), "UAH"),
            line_total=Money(Decimal("333.18"), "UAH"),
            serial_numbers=tuple(SerialNumber("EXAMPLE", str(i)) for i in range(27)),
            initial_custody=SOURCE,
        ),
        SqlAlchemyUnitOfWork(session_factory),
    )
    return receipt


def command(receipt):
    return TransferAssets(
        movement_id=uuid4(),
        document_number="EXAMPLE-MOVE",
        document_date=date(2026, 2, 1),
        effective_date=date(2026, 2, 1),
        source=SOURCE,
        destination=DESTINATION,
        asset_ids=tuple(unit.id for unit in receipt.units[:12]),
    )


def counts(session_factory):
    with session_factory() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (MovementDocumentRecord, MovementLineRecord)
        )


def test_transfer_reloads_movement_units_and_unchanged_receipt(stored, session_factory):
    request = command(stored)
    result = transfer_assets(request, SqlAlchemyUnitOfWork(session_factory))
    assert counts(session_factory) == (1, 12)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.movements.get(request.movement_id) == result.movement
        assert reader.movements.get(uuid4()) is None
        assert reader.receipts.get(stored.receipt.id) == stored.receipt
        for before in stored.units:
            after = reader.asset_units.get(before.id)
            if before.id in request.asset_ids:
                assert after == next(unit for unit in result.units if unit.id == before.id)
            assert after.receipt_line_id == before.receipt_line_id
            assert after.serial_number == before.serial_number
            assert after.custody == (DESTINATION if before.id in request.asset_ids else SOURCE)
    with pytest.raises(DuplicateMovement):
        transfer_assets(request, SqlAlchemyUnitOfWork(session_factory))
    assert counts(session_factory) == (1, 12)


def test_bad_last_unit_leaves_every_asset_and_history_unchanged(stored, session_factory):
    request = command(stored)
    with pytest.raises(InvalidTransfer):
        transfer_assets(
            replace(request, asset_ids=(*request.asset_ids[:-1], uuid4())), SqlAlchemyUnitOfWork(session_factory)
        )
    assert counts(session_factory) == (0, 0)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert tuple(reader.asset_units.get(u.id) for u in stored.units) == stored.units


def test_flushed_multi_unit_update_and_document_are_rolled_back_on_failure(stored, session_factory):
    class FailingUnitOfWork(SqlAlchemyUnitOfWork):
        def commit(self):
            self.session.flush()
            raise RuntimeError("synthetic failure after flush")

    with pytest.raises(RuntimeError, match="after flush"):
        transfer_assets(command(stored), FailingUnitOfWork(session_factory))
    assert counts(session_factory) == (0, 0)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert tuple(reader.asset_units.get(u.id) for u in stored.units) == stored.units


def test_database_movement_identity_race_rolls_back_second_document(stored, session_factory):
    from maas_ua.domain.movements import transfer_assets as build

    request = command(stored)
    result = build(
        id=request.movement_id,
        document_number=request.document_number,
        document_date=request.document_date,
        effective_date=request.effective_date,
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        source=SOURCE,
        destination=DESTINATION,
        asset_ids=request.asset_ids,
        assets=stored.units[:12],
    )
    with SqlAlchemyUnitOfWork(session_factory) as first, SqlAlchemyUnitOfWork(session_factory) as second:
        assert first.movements.get(request.movement_id) is None
        assert second.movements.get(request.movement_id) is None
        first.movements.add(result.movement)
        second.movements.add(result.movement)
        first.commit()
        with pytest.raises(DuplicateMovement):
            second.commit()
    assert counts(session_factory) == (1, 12)


@pytest.mark.parametrize("field", ["asset_unit_id", "movement_id"])
def test_movement_line_foreign_keys(stored, session_factory, field):
    result = transfer_assets(command(stored), SqlAlchemyUnitOfWork(session_factory))
    values = dict(movement_id=result.movement.id, asset_unit_id=stored.units[20].id, position=20)
    values[field] = uuid4()
    with session_factory() as session:
        session.add(MovementLineRecord(**values))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_duplicate_unit_line_is_rejected_by_database(stored, session_factory):
    result = transfer_assets(command(stored), SqlAlchemyUnitOfWork(session_factory))
    with session_factory() as session:
        session.add(MovementLineRecord(movement_id=result.movement.id, asset_unit_id=result.units[0].id, position=99))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.parametrize("case", ["stale_source", "follow_on", "duplicate_document"])
def test_concurrent_transfer_waits_for_row_lock_and_revalidates_latest_custody(stored, session_factory, case):
    from maas_ua.domain.movements import transfer_assets as build

    request = command(stored)
    ready = Event()
    backend = []

    class ObservedUnitOfWork(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            backend.append(self.session.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            return self

    follow_source = DESTINATION if case == "follow_on" else SOURCE
    follow = replace(
        request,
        movement_id=request.movement_id if case == "duplicate_document" else uuid4(),
        source=follow_source,
        destination=CustodyAssignment("EXAMPLE-C", "EXAMPLE-C"),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        with SqlAlchemyUnitOfWork(session_factory) as first:
            selected = first.asset_units.get_for_update(request.asset_ids)
            result = build(
                id=request.movement_id,
                document_number=request.document_number,
                document_date=request.document_date,
                effective_date=request.effective_date,
                created_at=datetime(2026, 2, 1, tzinfo=UTC),
                source=SOURCE,
                destination=DESTINATION,
                asset_ids=request.asset_ids,
                assets=selected,
            )
            first.movements.add(result.movement)
            for unit in result.units:
                first.asset_units.save(unit)
            with session_factory() as observer:
                sequence_before = observer.execute(
                    text("SELECT last_value, is_called FROM movement_registration_order")
                ).one()
            future = pool.submit(transfer_assets, follow, ObservedUnitOfWork(session_factory))
            try:
                assert ready.wait(5)
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    with session_factory() as observer:
                        blocked = observer.scalar(
                            text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": backend[0]}
                        )
                    if blocked:
                        break
                    Event().wait(0.01)
                assert blocked, "competing transfer did not wait on the held PostgreSQL row locks"
                assert not future.done()
                with session_factory() as observer:
                    assert (
                        observer.execute(text("SELECT last_value, is_called FROM movement_registration_order")).one()
                        == sequence_before
                    )
            finally:
                first.commit()
        if case == "duplicate_document":
            with pytest.raises(DuplicateMovement):
                future.result(timeout=5)
        elif follow_source == SOURCE:
            with pytest.raises(InvalidTransfer, match="source"):
                future.result(timeout=5)
        else:
            assert future.result(timeout=5).units[0].custody == follow.destination
    assert counts(session_factory) == ((1, 12) if follow_source == SOURCE else (2, 24))
    if case == "follow_on":
        with session_factory() as reader:
            assert (
                reader.get(MovementDocumentRecord, request.movement_id).registration_order
                < reader.get(MovementDocumentRecord, follow.movement_id).registration_order
            )


def test_lock_timeout_is_reported_as_a_retryable_repository_conflict(stored, session_factory):
    with SqlAlchemyUnitOfWork(session_factory) as first:
        first.asset_units.get_for_update(command(stored).asset_ids)
        with SqlAlchemyUnitOfWork(session_factory) as second:
            second.session.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(RepositoryConflict):
                second.asset_units.get_for_update(command(stored).asset_ids)


def test_unit_custody_requires_both_references_in_database(stored, session_factory):
    with session_factory() as session:
        unit = session.get(AssetUnitRecord, stored.units[0].id)
        unit.custody_party_ref = None
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_backdated_followup_is_rejected_after_reload(stored, session_factory):
    request = command(stored)
    transfer_assets(request, SqlAlchemyUnitOfWork(session_factory))
    with pytest.raises(InvalidTransfer, match="backdated"):
        transfer_assets(
            replace(
                request, movement_id=uuid4(), source=DESTINATION, destination=SOURCE, effective_date=date(2026, 1, 31)
            ),
            SqlAlchemyUnitOfWork(session_factory),
        )
    assert counts(session_factory) == (1, 12)


def test_same_day_order_is_persisted_independently_of_clocks_and_uuid(stored, session_factory):
    request = command(stored)
    transfer_assets(request, SqlAlchemyUnitOfWork(session_factory), clock=lambda: datetime(2026, 3, 1, tzinfo=UTC))
    follow = replace(request, movement_id=uuid4(), source=DESTINATION, destination=SOURCE)
    transfer_assets(follow, SqlAlchemyUnitOfWork(session_factory), clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    with session_factory() as session:
        first = session.get(MovementDocumentRecord, request.movement_id)
        second = session.get(MovementDocumentRecord, follow.movement_id)
        assert first.registration_order < second.registration_order
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        for unit in stored.units:
            assert reader.asset_units.get(unit.id).initial_custody == SOURCE


def test_initial_custody_cannot_be_changed_by_save(stored, session_factory):
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        unit = writer.asset_units.get(stored.units[0].id)
        with pytest.raises(RepositoryConflict):
            writer.asset_units.save(replace(unit, initial_custody=DESTINATION))


def test_initial_custody_pair_is_a_database_invariant(stored, session_factory):
    with session_factory() as session:
        session.get(AssetUnitRecord, stored.units[0].id).initial_party_ref = None
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_selected_asset_locks_are_acquired_in_uuid_order(stored, session_factory):
    selected = tuple(unit.id for unit in stored.units[:12])
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        locked = writer.asset_units.get_for_update(tuple(reversed(selected)))
        assert tuple(unit.id for unit in locked) == tuple(sorted(selected))
