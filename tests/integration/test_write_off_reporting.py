from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from maas_ua.adapters.inventory import SqlAlchemyInventoryReader
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.domain.custody import CustodyAssignment, LifecycleState
from maas_ua.service_layer.inventory import (
    GetAssetBySerialNumber,
    GetAssetHistory,
    GetAssetPositionAsOf,
    GetInventoryBalance,
    GetInventoryValuation,
    IncompleteInventory,
    InventoryFilter,
)
from maas_ua.service_layer.movements import TransferAssets, transfer_assets
from maas_ua.service_layer.write_offs import write_off_assets

pytestmark = pytest.mark.integration


@pytest.fixture
def written_off(session_factory, write_off_receipt, write_off_command):
    from uuid import uuid4

    receipt = write_off_receipt
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.nomenclatures.add(receipt.receipt.lines[0].nomenclature)
        writer.receipts.add(receipt.receipt)
        for unit in receipt.units:
            writer.asset_units.add(unit)
        writer.commit()
    movement = transfer_assets(
        TransferAssets(
            movement_id=uuid4(),
            document_number="EXAMPLE-MOVE",
            document_date=date(2026, 1, 20),
            effective_date=date(2026, 1, 21),
            source=receipt.units[0].custody,
            destination=CustodyAssignment("EXAMPLE-B", "EXAMPLE-B"),
            asset_ids=tuple(u.id for u in receipt.units[:12]),
        ),
        SqlAlchemyUnitOfWork(session_factory),
    )
    result = write_off_assets(
        write_off_command, SqlAlchemyUnitOfWork(session_factory), clock=lambda: datetime(2026, 3, 1, tzinfo=UTC)
    )
    return receipt, movement, result


def test_27_received_12_transferred_8_written_off_on_acceptance_date(engine, written_off):
    receipt, movement, result = written_off
    reader = SqlAlchemyInventoryReader(engine)
    for day in (date(2026, 2, 1), date(2026, 2, 3), date(2026, 2, 9)):
        report = reader.inventory_valuation(GetInventoryValuation(as_of=day))
        assert report.complete and report.count == 27
        assert report.totals[0].acquisition_value == Decimal("391500.00")
    for day in (date(2026, 2, 10), date(2026, 2, 28), date(2026, 3, 1)):
        report = reader.inventory_balance(GetInventoryBalance(as_of=day))
        assert report.complete and report.count == 19
        assert report.totals[0].acquisition_value == Decimal("275500.00")
        assert {(g.location_ref, g.count) for g in report.groups} == {("EXAMPLE-A", 15), ("EXAMPLE-B", 4)}
        disposed = reader.inventory_valuation(
            GetInventoryValuation(as_of=day, filters=InventoryFilter(lifecycle_state=LifecycleState.WRITTEN_OFF))
        )
        assert disposed.complete and disposed.count == 8
        assert disposed.totals[0].acquisition_value == Decimal("116000.00")
    assert (
        reader.inventory_balance(
            GetInventoryBalance(
                as_of=date(2026, 2, 9), filters=InventoryFilter(lifecycle_state=LifecycleState.WRITTEN_OFF)
            )
        ).count
        == 0
    )
    combined = reader.inventory_balance(
        GetInventoryBalance(as_of=date(2026, 2, 10), filters=InventoryFilter(lifecycle_state=None), group_by=())
    )
    assert combined.complete and combined.count == 27
    assert {(group.lifecycle_state, group.count) for group in combined.groups} == {("ACTIVE", 19), ("WRITTEN_OFF", 8)}
    assert combined.totals[0].acquisition_value == Decimal("391500.00")
    before = reader.asset_position(GetAssetPositionAsOf(asset_id=receipt.units[0].id, as_of=date(2026, 2, 9)))
    after = reader.asset_position(GetAssetPositionAsOf(asset_id=receipt.units[0].id, as_of=date(2026, 2, 10)))
    assert before.lifecycle_state == "ACTIVE" and before.write_off is None
    assert after.lifecycle_state == "WRITTEN_OFF" and after.write_off.document_id == result.document.id
    assert before.custody == after.custody == movement.movement.destination


def test_written_off_serial_and_history_keep_provenance_and_authorization(engine, written_off):
    receipt, movement, result = written_off
    reader = SqlAlchemyInventoryReader(engine)
    detail = reader.asset_by_serial(GetAssetBySerialNumber(namespace="EXAMPLE", value="0"))
    assert detail.complete and detail.lifecycle_state == "WRITTEN_OFF"
    assert detail.origin.unit_price == Decimal("14500.00")
    assert detail.origin.line_id == receipt.receipt.lines[0].id
    assert detail.origin.accounting_classification == "INVENTORY"
    assert detail.write_off.document_id == result.document.id
    history = reader.asset_history(GetAssetHistory(asset_id=receipt.units[0].id))
    assert history.complete
    events = [event for day in history.days for event in day.events]
    assert [event.kind for event in events] == ["RECEIPT", "TRANSFER", "WRITE_OFF"]
    assert events[1].document_id == movement.movement.id
    assert events[2].document_date == date(2026, 2, 1)
    assert events[2].authorization_date == date(2026, 2, 3)
    assert events[2].effective_date == date(2026, 2, 10)
    assert events[2].created_at == datetime(2026, 3, 1, tzinfo=UTC)
    assert events[2].authorization_reference == "EXAMPLE-AUTH"


@pytest.mark.parametrize("corruption", ["missing", "current", "classification", "chronology", "unknown"])
def test_lifecycle_filter_cannot_hide_incomplete_write_off_history(engine, written_off, corruption):
    receipt, _, result = written_off
    with engine.begin() as connection:
        if corruption == "missing":
            connection.execute(text("DELETE FROM write_off_lines WHERE asset_unit_id=:id"), {"id": receipt.units[0].id})
        elif corruption == "current":
            connection.execute(
                text("UPDATE asset_units SET lifecycle_state='ACTIVE' WHERE id=:id"), {"id": receipt.units[0].id}
            )
        elif corruption == "classification":
            connection.execute(text("UPDATE receipt_lines SET accounting_classification=NULL"))
        elif corruption == "chronology":
            connection.execute(text("UPDATE receipt_documents SET effective_date='2026-02-11'"))
        else:
            connection.execute(
                text("UPDATE asset_units SET lifecycle_state=NULL WHERE id=:id"), {"id": receipt.units[0].id}
            )
    reader = SqlAlchemyInventoryReader(engine)
    for state in (LifecycleState.ACTIVE, LifecycleState.WRITTEN_OFF):
        report = reader.inventory_balance(
            GetInventoryBalance(as_of=date(2026, 3, 1), filters=InventoryFilter(lifecycle_state=state))
        )
        assert not report.complete and report.groups == ()
        with pytest.raises(IncompleteInventory):
            _ = report.count


def test_same_day_write_off_follows_transfers(engine, session_factory, write_off_receipt, write_off_command):
    from uuid import uuid4

    receipt = write_off_receipt
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.nomenclatures.add(receipt.receipt.lines[0].nomenclature)
        writer.receipts.add(receipt.receipt)
        for unit in receipt.units:
            writer.asset_units.add(unit)
        writer.commit()
    transfer_assets(
        TransferAssets(
            movement_id=uuid4(),
            document_number="EXAMPLE-MOVE",
            document_date=date(2026, 2, 10),
            effective_date=date(2026, 2, 10),
            source=receipt.units[0].custody,
            destination=CustodyAssignment("EXAMPLE-B", "EXAMPLE-B"),
            asset_ids=write_off_command.asset_ids,
        ),
        SqlAlchemyUnitOfWork(session_factory),
    )
    write_off_assets(write_off_command, SqlAlchemyUnitOfWork(session_factory))
    history = SqlAlchemyInventoryReader(engine).asset_history(GetAssetHistory(asset_id=receipt.units[0].id))
    assert history.complete and history.days[-1].ordered
    assert [event.kind for event in history.days[-1].events] == ["TRANSFER", "WRITE_OFF"]


def test_write_off_preserves_separate_exact_currency_totals(engine, session_factory, written_off):
    from uuid import uuid4

    from maas_ua.domain.receipts import AccountingClassification, Money, Quantity, SerialNumber
    from maas_ua.service_layer.receipts import RegisterInventoryReceipt, register_inventory_receipt
    from maas_ua.service_layer.write_offs import WriteOffAssets

    receipt, _, _ = written_off
    extra = register_inventory_receipt(
        RegisterInventoryReceipt(
            document_number="EXAMPLE-USD",
            document_date=date(2026, 1, 1),
            effective_date=date(2026, 1, 2),
            source="EXAMPLE",
            nomenclature=receipt.receipt.lines[0].nomenclature,
            quantity=Quantity(3, "pcs"),
            unit_price=Money(Decimal("0.12345"), "USD"),
            line_total=Money(Decimal("0.37035"), "USD"),
            serial_numbers=tuple(SerialNumber("EXAMPLE-USD", str(i)) for i in range(3)),
            initial_custody=receipt.units[0].custody,
            accounting_classification=AccountingClassification.INVENTORY,
        ),
        SqlAlchemyUnitOfWork(session_factory),
    )
    write_off_assets(
        WriteOffAssets(
            write_off_id=uuid4(),
            document_number="EXAMPLE-USD-OFF",
            document_date=date(2026, 2, 1),
            authorization_reference="EXAMPLE-AUTH",
            authorization_date=date(2026, 2, 3),
            effective_date=date(2026, 2, 10),
            asset_ids=(extra.units[0].id,),
        ),
        SqlAlchemyUnitOfWork(session_factory),
    )
    report = SqlAlchemyInventoryReader(engine).inventory_valuation(GetInventoryValuation(as_of=date(2026, 2, 10)))
    assert report.complete and report.count == 21
    assert {t.currency: t.acquisition_value for t in report.totals} == {
        "UAH": Decimal("275500.00"),
        "USD": Decimal("0.24690"),
    }
