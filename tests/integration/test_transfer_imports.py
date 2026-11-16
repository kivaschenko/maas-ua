from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import BytesIO
from threading import Barrier
from uuid import uuid4

import pytest
from import_fixtures import csv_file as receipt_csv
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from transfer_import_fixtures import csv_file, xlsx_file

from maas_ua.adapters.inventory import SqlAlchemyInventoryReader
from maas_ua.adapters.orm import MovementDocumentRecord, MovementLineRecord
from maas_ua.adapters.receipt_import_files import parse_receipt_upload
from maas_ua.adapters.receipt_import_memory import MemoryPreviewStore
from maas_ua.adapters.receipt_import_reader import SqlAlchemyReceiptImportReader
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.adapters.transfer_import_files import parse_transfer_upload
from maas_ua.adapters.transfer_import_memory import MemoryTransferPreviewStore
from maas_ua.adapters.transfer_import_reader import SqlAlchemyTransferImportReader
from maas_ua.domain.receipts import Nomenclature, TrackingPolicy
from maas_ua.domain.write_offs import InvalidWriteOff
from maas_ua.entrypoints.flask_app import create_app
from maas_ua.service_layer.inventory import (
    GetAssetHistory,
    GetAssetPositionAsOf,
    GetInventoryValuation,
    InventoryFilter,
)
from maas_ua.service_layer.movements import TransferAssets, transfer_assets
from maas_ua.service_layer.receipt_imports import ReceiptImportCoordinator
from maas_ua.service_layer.transfer_imports import TransferImportCoordinator
from maas_ua.service_layer.write_offs import WriteOffAssets, write_off_assets

pytestmark = pytest.mark.integration


@pytest.fixture
def setup(session_factory):
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        uow.nomenclatures.add(
            Nomenclature(
                "EXAMPLE-ID", TrackingPolicy.SERIALIZED, code="EXAMPLE-FPV", name="Example", unit_of_measure="pcs"
            )
        )
        uow.commit()
    receipts = ReceiptImportCoordinator(
        SqlAlchemyReceiptImportReader(session_factory),
        lambda: SqlAlchemyUnitOfWork(session_factory),
        MemoryPreviewStore(start_timers=False),
    )
    preview = receipts.preview(parse_receipt_upload(receipt_csv(), "csv"), "seed")
    assert receipts.confirm(preview.id, "seed").status == "registered"
    original = receipts.receipt(preview.id, "seed")
    transfers = TransferImportCoordinator(
        SqlAlchemyTransferImportReader(session_factory),
        lambda: SqlAlchemyUnitOfWork(session_factory),
        MemoryTransferPreviewStore(start_timers=False),
    )
    yield receipts, transfers, original
    receipts.previews.close()
    transfers.previews.close()


def stage(transfers, owner="a", **kwargs):
    return transfers.preview(parse_transfer_upload(csv_file(**kwargs), "csv"), owner)


def counts(factory):
    with factory() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (MovementDocumentRecord, MovementLineRecord)
        )


def test_browser_acceptance_27_receipt_12_transfer_valuation_and_history(setup, session_factory, engine):
    receipts, transfers, original = setup
    reader = SqlAlchemyInventoryReader(engine)
    before = reader.inventory_valuation(GetInventoryValuation(as_of=date(2026, 1, 31)))
    assert before.count == 27 and before.totals[0].acquisition_value == 391500
    app = create_app(
        {"TESTING": True, "SECRET_KEY": "synthetic-secret"}, imports=receipts, transfers=transfers, inventory=reader
    )
    client = app.test_client()
    assert client.get("/transfers/import/new").status_code == 200
    with client.session_transaction() as session:
        token = session["csrf_token"]
    response = client.post(
        "/transfers/import",
        data={
            "csrf_token": token,
            "import_type": "serialized_transfer_v1",
            "file": (BytesIO(xlsx_file()), "synthetic.xlsx"),
        },
    )
    url = response.headers["Location"]
    assert "EXAMPLE-0011" in client.get(url).text
    assert counts(session_factory) == (0, 0)
    response = client.post(url + "/confirm", data={"csrf_token": token}, follow_redirects=True)
    assert "Переміщення зареєстровано" in response.text
    assert counts(session_factory) == (1, 12)
    after = reader.inventory_valuation(GetInventoryValuation(as_of=date(2026, 2, 1)))
    assert after.count == 27 and after.totals == before.totals
    assert {g.location_ref: g.count for g in after.groups} == {"EXAMPLE-LOCATION": 15, "EXAMPLE-DESTINATION": 12}
    for location, count in [("EXAMPLE-LOCATION", 15), ("EXAMPLE-DESTINATION", 12)]:
        report = reader.inventory_valuation(
            GetInventoryValuation(as_of=date(2026, 2, 1), filters=InventoryFilter(location_ref=location))
        )
        assert report.count == count
    unit = next(u for u in original.units if u.serial_number.value == "EXAMPLE-0000")
    assert (
        reader.asset_position(GetAssetPositionAsOf(asset_id=unit.id, as_of=date(2026, 1, 31))).custody
        == unit.initial_custody
    )
    assert (
        reader.asset_position(GetAssetPositionAsOf(asset_id=unit.id, as_of=date(2026, 2, 1))).custody.location_ref
        == "EXAMPLE-DESTINATION"
    )
    history = reader.asset_history(GetAssetHistory(asset_id=unit.id))
    assert history.complete and history.asset.origin.unit_price == 14500
    assert [event.kind for day in history.days for event in day.events] == ["RECEIPT", "TRANSFER"]
    assert client.get(f"/inventory/assets/{unit.id}?as_of=2026-02-01").status_code == 200
    assert not stage(transfers).ready
    assert counts(session_factory) == (1, 12)
    # A mixed batch containing a written-off unit leaves the other source asset untouched.
    remaining = sorted(
        (u for u in original.units if u.serial_number.value >= "EXAMPLE-0012"), key=lambda u: u.serial_number.value
    )
    write_off_assets(
        WriteOffAssets(
            write_off_id=uuid4(),
            document_number="EXAMPLE-WO",
            document_date=date(2026, 2, 2),
            authorization_reference="EXAMPLE-AUTH",
            authorization_date=date(2026, 2, 2),
            effective_date=date(2026, 2, 2),
            asset_ids=(remaining[0].id,),
        ),
        SqlAlchemyUnitOfWork(session_factory),
    )
    invalid = stage(
        transfers, owner="invalid", serials=[(u.serial_number.namespace, u.serial_number.value) for u in remaining[:2]]
    )
    assert transfers.confirm(invalid.id, "invalid").status == "rejected"
    assert counts(session_factory) == (1, 12)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert uow.asset_units.get(remaining[1].id).custody == remaining[1].custody
        assert uow.receipts.get(original.receipt.id) == original.receipt


def test_serial_lookup_is_exact_batched_and_read_only(setup, session_factory, engine):
    _, transfers, _ = setup
    candidate = parse_transfer_upload(csv_file(), "csv").candidate
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        facts = transfers.reader.inspect(candidate, uuid4())
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(facts.assets) == 12 and len(statements) <= 4
    assert sum("asset_units" in sql and " IN " in sql for sql in statements) == 1
    for pair in [("example-issuer", "EXAMPLE-0000"), ("EXAMPLE-ISSUER", "example-0000"), ("EXAMPLE-ISSUER", "0000")]:
        wrong = parse_transfer_upload(csv_file(serials=[pair]), "csv").candidate
        assert not transfers.reader.inspect(wrong, uuid4()).assets
    with transfers.reader._snapshot() as session:
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        with pytest.raises(DBAPIError):
            session.execute(text("UPDATE asset_units SET serial_value=serial_value"))


def test_flushed_failure_rolls_back_every_unit_then_retries_same_uuid(setup, session_factory):
    _, transfers, original = setup

    class Failure(SqlAlchemyUnitOfWork):
        def commit(self):
            self.session.flush()
            raise RuntimeError("synthetic private flush failure")

    transfers.uow_factory = lambda: Failure(session_factory)
    preview = stage(transfers)
    assert transfers.confirm(preview.id, "a").status == "retryable"
    assert counts(session_factory) == (0, 0)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert tuple(uow.asset_units.get(u.id) for u in original.units) == original.units
    transfers.uow_factory = lambda: SqlAlchemyUnitOfWork(session_factory)
    assert transfers.confirm(preview.id, "a").movement_id == preview.registration_id
    assert counts(session_factory) == (1, 12)


@pytest.mark.parametrize("same_identity", [False, True])
def test_concurrent_imports_lock_and_refresh_overlapping_assets(setup, session_factory, same_identity):
    _, transfers, _ = setup
    previews = [stage(transfers, owner) for owner in ("a", "b")]
    if same_identity:
        with transfers.previews._lock:
            transfers.previews._replace(previews[1].id, "b", registration_id=previews[0].registration_id)
    barrier = Barrier(2)

    class Rendezvous(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            barrier.wait(timeout=10)
            return self

    transfers.uow_factory = lambda: Rendezvous(session_factory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(transfers.confirm, p.id, owner) for p, owner in zip(previews, ("a", "b"), strict=True)]
        results = [f.result(timeout=15) for f in futures]
    assert sorted(r.status for r in results) == sorted(
        ["registered", "already_registered" if same_identity else "rejected"]
    )
    assert counts(session_factory) == (1, 12)


def test_import_versus_writeoff_race_has_one_complete_winner(setup, session_factory):
    _, transfers, original = setup
    preview = stage(transfers)
    wanted = {f"EXAMPLE-{i:04}" for i in range(12)}
    ids = tuple(u.id for u in original.units if u.serial_number.value in wanted)
    barrier = Barrier(2)

    class Rendezvous(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            barrier.wait(timeout=10)
            return self

    transfers.uow_factory = lambda: Rendezvous(session_factory)
    command = WriteOffAssets(
        write_off_id=uuid4(),
        document_number="EXAMPLE-WO",
        document_date=date(2026, 1, 20),
        authorization_reference="EXAMPLE-AUTH",
        authorization_date=date(2026, 1, 20),
        effective_date=date(2026, 1, 20),
        asset_ids=ids,
    )

    def writeoff():
        try:
            write_off_assets(command, Rendezvous(session_factory))
            return "written_off"
        except InvalidWriteOff:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        transfer = pool.submit(transfers.confirm, preview.id, "a")
        off = pool.submit(writeoff)
        result, written = transfer.result(timeout=15), off.result(timeout=15)
    assert (result.status, written) in {("registered", "rejected"), ("rejected", "written_off")}
    assert counts(session_factory) == ((1, 12) if result.status == "registered" else (0, 0))


def test_lost_commit_response_and_later_custody_reconcile_immutable_original(setup, session_factory):
    _, transfers, _ = setup

    class LostResponse(SqlAlchemyUnitOfWork):
        def commit(self):
            super().commit()
            original = transfers.reader.inspect(preview.candidate, preview.registration_id).original
            transfer_assets(
                TransferAssets(
                    movement_id=uuid4(),
                    document_number="EXAMPLE-NEXT",
                    document_date=date(2026, 3, 1),
                    effective_date=date(2026, 3, 1),
                    source=original.destination,
                    destination=original.source,
                    asset_ids=tuple(line.asset_unit_id for line in original.lines),
                ),
                SqlAlchemyUnitOfWork(session_factory),
            )
            raise RuntimeError("synthetic lost response")

    transfers.uow_factory = lambda: LostResponse(session_factory)
    preview = stage(transfers)
    result = transfers.confirm(preview.id, "a")
    assert result.status == "already_registered" and result.movement_id == preview.registration_id
    assert counts(session_factory) == (2, 24)


def test_same_technical_uuid_with_different_facts_rejects_all(setup, session_factory):
    _, transfers, original = setup
    preview = stage(transfers)
    c = preview.candidate
    # Another document claims that UUID with disjoint assets and identical header.
    selected = tuple(u.id for u in original.units if u.serial_number.value >= "EXAMPLE-0012")
    transfer_assets(
        TransferAssets(
            movement_id=preview.registration_id,
            document_number=c.document_number,
            document_date=c.document_date,
            effective_date=c.effective_date,
            source=c.source,
            destination=c.destination,
            asset_ids=selected,
        ),
        SqlAlchemyUnitOfWork(session_factory),
    )
    result = transfers.confirm(preview.id, "a")
    assert result.status == "rejected" and result.issues[0].code == "movement_conflict"
    assert counts(session_factory) == (1, 15)
