"""Independent expected counts/values; exclusively synthetic disposable data."""

from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import InternalError

from maas_ua.adapters.inventory import SqlAlchemyInventoryReader
from maas_ua.adapters.orm import AssetUnitRecord
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.domain.custody import CustodyAssignment
from maas_ua.domain.receipts import Money, Nomenclature, Quantity, SerialNumber, TrackingPolicy
from maas_ua.service_layer.inventory import (
    AssetNotFound,
    GetAssetBySerialNumber,
    GetAssetHistory,
    GetAssetPositionAsOf,
    GetInventoryBalance,
    GetInventoryValuation,
    IncompleteInventory,
    InventoryFilter,
)
from maas_ua.service_layer.movements import TransferAssets, transfer_assets
from maas_ua.service_layer.receipts import RegisterInventoryReceipt, register_inventory_receipt

pytestmark = pytest.mark.integration
A = CustodyAssignment("EXAMPLE-LOCATION-A", "EXAMPLE-PARTY-A")
B = CustodyAssignment("EXAMPLE-LOCATION-B", "EXAMPLE-PARTY-B")
C = CustodyAssignment("EXAMPLE-LOCATION-C", "EXAMPLE-PARTY-C")
RECEIPT = date(2026, 1, 10)
MOVE = date(2026, 2, 10)


@pytest.fixture
def inventory(engine, session_factory):
    item = Nomenclature(
        "EXAMPLE", TrackingPolicy.SERIALIZED, code="EXAMPLE", name="Example drone", unit_of_measure="pcs"
    )
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.nomenclatures.add(item)
        writer.commit()

    def receive(*, count=27, price="14500.00", currency="UAH", source="EXAMPLE-FUND-A", custody=A, namespace=None):
        namespace = namespace or f"EXAMPLE-{uuid4()}"
        money = Money(Decimal(price), currency)
        return register_inventory_receipt(
            RegisterInventoryReceipt(
                document_number="EXAMPLE-RECEIPT",
                document_date=date(2026, 1, 9),
                effective_date=RECEIPT,
                source=source,
                nomenclature=item,
                quantity=Quantity(count, "pcs"),
                unit_price=money,
                line_total=money.multiply(count),
                serial_numbers=tuple(SerialNumber(namespace, str(i)) for i in range(count)),
                initial_custody=custody,
                analytics=(("fund", source),),
            ),
            SqlAlchemyUnitOfWork(session_factory),
            clock=lambda: datetime(2026, 1, 20, tzinfo=UTC),
        )

    def move(units, source=A, destination=B, day=MOVE):
        return transfer_assets(
            TransferAssets(
                movement_id=uuid4(),
                document_number="EXAMPLE-MOVE",
                document_date=day,
                effective_date=day,
                source=source,
                destination=destination,
                asset_ids=tuple(unit.id for unit in units),
            ),
            SqlAlchemyUnitOfWork(session_factory),
            clock=lambda: datetime(2026, 3, 1, tzinfo=UTC),
        )

    return SqlAlchemyInventoryReader(engine), receive, move


def amounts(report):
    return {total.currency: total.acquisition_value for total in report.totals}


def test_27_units_12_transferred_reconcile_on_inclusive_business_day(inventory):
    reader, receive, move = inventory
    receipt = receive()
    move(receipt.units[:12])
    before = reader.inventory_balance(GetInventoryBalance(as_of=date(2026, 2, 9)))
    after = reader.inventory_valuation(GetInventoryValuation(as_of=MOVE))
    assert before.count == after.count == 27
    assert amounts(before) == amounts(after) == {"UAH": Decimal("391500.00")}
    assert [(g.location_ref, g.count, g.acquisition_value) for g in before.groups] == [
        (A.location_ref, 27, Decimal("391500.00"))
    ]
    assert {(g.location_ref, g.count, g.acquisition_value) for g in after.groups} == {
        (A.location_ref, 15, Decimal("217500.00")),
        (B.location_ref, 12, Decimal("174000.00")),
    }
    for as_of, expected in [(RECEIPT, A), (date(2026, 2, 9), A), (MOVE, B), (date(2027, 1, 1), B)]:
        position = reader.asset_position(GetAssetPositionAsOf(asset_id=receipt.units[0].id, as_of=as_of))
        assert position.complete and position.registered and position.custody == expected
    grouped = reader.inventory_balance(GetInventoryBalance(as_of=MOVE, group_by=()))
    assert len(grouped.groups) == 1 and grouped.groups[0].count == 27


def test_lookup_history_and_before_receipt_are_distinct_from_absence(inventory):
    reader, receive, move = inventory
    receipt = receive(namespace="EXAMPLE-ISSUER")
    first = move(receipt.units[:1])
    move(first.units, source=B, destination=C, day=date(2026, 2, 11))
    detail = reader.asset_by_serial(GetAssetBySerialNumber(namespace="EXAMPLE-ISSUER", value="0"))
    assert detail.complete and detail.asset_id == receipt.units[0].id
    assert detail.nomenclature.id == "EXAMPLE" and detail.origin.source == "EXAMPLE-FUND-A"
    assert detail.origin.line_id == receipt.receipt.lines[0].id
    assert detail.origin.unit_price == Decimal("14500.00") and detail.initial_custody == A
    history = reader.asset_history(GetAssetHistory(asset_id=detail.asset_id))
    assert history.complete and all(day.ordered for day in history.days)
    assert [(e.kind, e.source, e.destination) for day in history.days for e in day.events] == [
        ("RECEIPT", None, A),
        ("TRANSFER", A, B),
        ("TRANSFER", B, C),
    ]
    assert reader.asset_position(GetAssetPositionAsOf(asset_id=detail.asset_id, as_of=MOVE)).custody == B
    assert reader.asset_position(GetAssetPositionAsOf(asset_id=detail.asset_id, as_of=date(2026, 2, 11))).custody == C
    before = reader.asset_position(GetAssetPositionAsOf(asset_id=detail.asset_id, as_of=date(2026, 1, 9)))
    assert before.complete and before.registered is False and before.custody is None
    assert reader.inventory_balance(GetInventoryBalance(as_of=date(2026, 1, 9))).count == 0
    for query in [
        GetAssetBySerialNumber(namespace="example-issuer", value="0"),
        GetAssetBySerialNumber(namespace="EXAMPLE-ISSUER", value="missing"),
    ]:
        with pytest.raises(AssetNotFound):
            reader.asset_by_serial(query)
    with pytest.raises(AssetNotFound):
        reader.asset_position(GetAssetPositionAsOf(asset_id=uuid4(), as_of=MOVE))
    other = receive(count=1, namespace="EXAMPLE-OTHER")
    assert (
        reader.asset_by_serial(GetAssetBySerialNumber(namespace="EXAMPLE-OTHER", value="0")).asset_id
        == other.units[0].id
    )


def test_same_day_transfers_use_proven_order_after_session_reload(inventory):
    reader, receive, move = inventory
    receipt = receive()
    first = move(receipt.units[:1])
    move(first.units, source=B, destination=C)
    position = reader.asset_position(GetAssetPositionAsOf(asset_id=receipt.units[0].id, as_of=MOVE))
    assert position.complete and position.custody == C
    history = reader.asset_history(GetAssetHistory(asset_id=receipt.units[0].id))
    assert history.days[-1].ordered
    assert [e.destination for e in history.days[-1].events] == [B, C]
    assert history.days[-1].events[0].created_at == history.days[-1].events[1].created_at


def test_prices_sources_analytics_and_currencies_stay_separate(inventory):
    reader, receive, _ = inventory
    receive()
    receive(count=2, price="0.12345", source="EXAMPLE-FUND-B")
    receive(count=3, price="7.001", currency="USD", source="EXAMPLE-FUND-C")
    with localcontext() as context:
        context.prec = 3
        report = reader.inventory_valuation(GetInventoryValuation(as_of=RECEIPT))
    assert report.count == 32
    assert amounts(report) == {"UAH": Decimal("391500.24690"), "USD": Decimal("21.003")}
    assert len(report.groups) == 3
    filtered = reader.inventory_balance(
        GetInventoryBalance(as_of=RECEIPT, filters=InventoryFilter(analytics=(("fund", "EXAMPLE-FUND-B"),)))
    )
    assert filtered.count == 2 and amounts(filtered) == {"UAH": Decimal("0.24690")}
    assert filtered.groups[0].origin.analytics == (("fund", "EXAMPLE-FUND-B"),)


def test_incomplete_records_have_known_values_but_no_verified_totals_and_static_filter_scope(inventory, engine):
    reader, receive, _ = inventory
    receive(count=2, custody=None, source="EXAMPLE-INCOMPLETE")
    valid = receive(count=3, source="EXAMPLE-COMPLETE")
    with engine.begin() as connection:
        connection.execute(text("UPDATE asset_units SET lifecycle_state = NULL WHERE initial_location_ref IS NULL"))
    incomplete = reader.inventory_valuation(GetInventoryValuation(as_of=RECEIPT))
    assert not incomplete.complete and incomplete.groups == () and incomplete.candidate_count == 5
    assert incomplete.known_acquisition_totals[0].acquisition_value == Decimal("72500.00")
    assert incomplete.diagnostics[0].count == 2
    assert set(incomplete.diagnostics[0].issues) == {"missing_initial_custody", "unknown_lifecycle"}
    for attribute in ("count", "totals"):
        with pytest.raises(IncompleteInventory):
            getattr(incomplete, attribute)
    for filters in [InventoryFilter(source="EXAMPLE-COMPLETE"), InventoryFilter(receipt_id=valid.receipt.id)]:
        scoped = reader.inventory_balance(GetInventoryBalance(as_of=RECEIPT, filters=filters))
        assert scoped.complete and scoped.count == 3
    empty = reader.inventory_balance(
        GetInventoryBalance(as_of=RECEIPT, filters=InventoryFilter(source="EXAMPLE-ABSENT"))
    )
    assert empty.complete and empty.count == 0 and empty.totals == ()


def test_unknown_custody_is_not_absence_and_is_not_filled_from_current_state(inventory, engine):
    reader, receive, _ = inventory
    receipt = receive(count=1)
    with engine.begin() as connection:
        connection.execute(text("UPDATE asset_units SET initial_location_ref=NULL, initial_party_ref=NULL"))
    position = reader.asset_position(GetAssetPositionAsOf(asset_id=receipt.units[0].id, as_of=RECEIPT))
    assert position.registered and not position.complete and position.custody is None
    assert position.issues == ("missing_initial_custody",)
    detail = reader.asset_by_serial(
        GetAssetBySerialNumber(namespace=receipt.units[0].serial_number.namespace, value="0")
    )
    assert detail.origin.unit_price == Decimal("14500.00") and not detail.complete


def test_ambiguous_legacy_movements_and_broken_chains_are_explicit(inventory, engine):
    reader, receive, move = inventory
    receipt = receive(count=1)
    first = move(receipt.units)
    second = move(first.units, source=B, destination=C)
    with engine.begin() as connection:
        connection.execute(text("UPDATE movement_documents SET registration_order=NULL"))
    history = reader.asset_history(GetAssetHistory(asset_id=receipt.units[0].id))
    assert not history.complete and not history.days[-1].ordered
    assert "ambiguous_movement_order" in history.issues
    report = reader.inventory_balance(GetInventoryBalance(as_of=MOVE))
    assert not report.complete and "ambiguous_movement_order" in report.diagnostics[0].issues
    # Do not choose a destination from the ambiguous ordering, even with a custody filter.
    filtered = reader.inventory_balance(
        GetInventoryBalance(as_of=MOVE, filters=InventoryFilter(location_ref=A.location_ref))
    )
    assert not filtered.complete
    with engine.begin() as connection:
        for index, movement in enumerate((first.movement, second.movement), start=100):
            connection.execute(
                text("UPDATE movement_documents SET registration_order=:i WHERE id=:id"),
                {"i": index, "id": movement.id},
            )
        connection.execute(
            text("UPDATE movement_documents SET source_party_ref='EXAMPLE-BAD' WHERE id=:id"),
            {"id": second.movement.id},
        )
    broken = reader.asset_position(GetAssetPositionAsOf(asset_id=receipt.units[0].id, as_of=MOVE))
    assert "broken_custody_chain" in broken.issues


def test_custody_filters_do_not_fail_for_known_incomplete_assets_elsewhere(inventory, engine):
    reader, receive, _ = inventory
    receive(count=2, custody=B)
    receive(count=3)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE asset_units SET lifecycle_state=NULL WHERE initial_location_ref=:location"),
            {"location": B.location_ref},
        )
    report = reader.inventory_balance(
        GetInventoryBalance(as_of=RECEIPT, filters=InventoryFilter(location_ref=A.location_ref))
    )
    assert report.complete and report.count == 3


def test_reports_are_read_only_and_use_repeatable_read_snapshot(inventory, engine):
    reader, receive, _ = inventory
    receive(count=1)
    commits = []

    def committed(connection):
        commits.append(True)

    event.listen(engine, "commit", committed)
    try:
        assert reader.inventory_balance(GetInventoryBalance(as_of=RECEIPT)).count == 1
        assert commits == []
    finally:
        event.remove(engine, "commit", committed)
    with reader._snapshot() as connection:
        assert connection.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert connection.scalar(text("SHOW transaction_read_only")) == "on"
        assert connection.scalar(select(func.count()).select_from(AssetUnitRecord.__table__)) == 1
        receive(count=2)
        assert connection.scalar(select(func.count()).select_from(AssetUnitRecord.__table__)) == 1
        with pytest.raises(InternalError):
            connection.execute(text("DELETE FROM asset_units"))
    assert reader.inventory_balance(GetInventoryBalance(as_of=RECEIPT)).count == 3


def test_missing_source_cannot_be_presented_as_complete_provenance(inventory, engine):
    reader, receive, _ = inventory
    receive(count=1)
    with engine.begin() as connection:
        connection.execute(text("UPDATE receipt_documents SET source=''"))
    report = reader.inventory_balance(GetInventoryBalance(as_of=RECEIPT))
    assert not report.complete and report.diagnostics[0].issues == ("missing_provenance",)


def test_one_report_does_not_mix_snapshots_when_a_late_receipt_commits(inventory, engine):
    reader, receive, _ = inventory
    receive(count=1)
    inserted = []

    def insert_after_first_select(connection, cursor, statement, parameters, context, executemany):
        if "history_checks" in statement and not inserted:
            inserted.append(True)
            receive(count=2)

    event.listen(engine, "after_cursor_execute", insert_after_first_select)
    try:
        report = reader.inventory_balance(GetInventoryBalance(as_of=RECEIPT))
    finally:
        event.remove(engine, "after_cursor_execute", insert_after_first_select)
    assert inserted and report.complete and report.count == 1
    assert amounts(report) == {"UAH": Decimal("14500.00")}
    later = reader.inventory_balance(GetInventoryBalance(as_of=RECEIPT))
    assert later.count == 3 and amounts(later) == {"UAH": Decimal("43500.00")}


def test_receipt_precedes_same_day_transfers_and_original_analytics_remain(inventory):
    reader, receive, move = inventory
    receipt = receive(count=1)
    move(receipt.units, day=RECEIPT)
    history = reader.asset_history(GetAssetHistory(asset_id=receipt.units[0].id))
    assert history.complete and len(history.days) == 1 and history.days[0].ordered
    assert [event.kind for event in history.days[0].events] == ["RECEIPT", "TRANSFER"]
    position = reader.asset_position(GetAssetPositionAsOf(asset_id=receipt.units[0].id, as_of=RECEIPT))
    assert position.complete and position.custody == B


def test_synthetic_scale_uses_fixed_query_count_and_a_measured_sql_plan(inventory, engine):
    reader, receive, move = inventory
    receipt = receive(count=5000)
    first = move(receipt.units[:2000])
    move(first.units[:750], source=B, destination=C)
    statements = []

    def executed(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", executed)
    try:
        report = reader.inventory_valuation(GetInventoryValuation(as_of=MOVE))
    finally:
        event.remove(engine, "before_cursor_execute", executed)
    assert report.count == 5000 and amounts(report) == {"UAH": Decimal("72500000.00")}
    assert {(g.location_ref, g.count) for g in report.groups} == {
        (A.location_ref, 3000),
        (B.location_ref, 1250),
        (C.location_ref, 750),
    }
    assert len(statements) == 4  # SET TRANSACTION and three aggregate SELECTs, independent of asset count.
    assert all("custody_location_ref" not in statement for statement in statements)
    from maas_ua.adapters.inventory import historical_positions

    positions = historical_positions(MOVE)
    statement = select(positions.c.location_ref, func.count(), func.sum(positions.c.unit_price)).group_by(
        positions.c.location_ref
    )
    compiled = statement.compile(engine, compile_kwargs={"literal_binds": True})
    with engine.begin() as connection:
        connection.exec_driver_sql("ANALYZE")
    with reader._snapshot() as connection:
        plan = connection.exec_driver_sql(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {compiled}").scalar_one()[0]
    assert plan["Plan"]["Actual Rows"] == 3
    print(f"Synthetic plan: 5000 assets, 2750 movement lines, execution_ms={plan['Execution Time']}")
