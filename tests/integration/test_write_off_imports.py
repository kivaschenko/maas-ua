from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import BytesIO
from threading import Barrier
from uuid import uuid4

import pytest
from import_fixtures import csv_file as receipt_csv
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from write_off_import_fixtures import csv_file, xlsx_file

from maas_ua.adapters.inventory import SqlAlchemyInventoryReader
from maas_ua.adapters.orm import WriteOffDocumentRecord, WriteOffLineRecord
from maas_ua.adapters.receipt_import_files import parse_receipt_upload
from maas_ua.adapters.receipt_import_memory import MemoryPreviewStore
from maas_ua.adapters.receipt_import_reader import SqlAlchemyReceiptImportReader
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.adapters.write_off_import_files import parse_write_off_upload
from maas_ua.adapters.write_off_import_memory import MemoryWriteOffPreviewStore
from maas_ua.adapters.write_off_import_reader import SqlAlchemyWriteOffImportReader
from maas_ua.domain.receipts import Nomenclature, TrackingPolicy
from maas_ua.entrypoints.flask_app import create_app
from maas_ua.service_layer.inventory import (
    GetAssetHistory,
    GetInventoryValuation,
    InventoryFilter,
)
from maas_ua.service_layer.movements import TransferAssets, transfer_assets
from maas_ua.service_layer.receipt_imports import ReceiptImportCoordinator
from maas_ua.service_layer.write_off_imports import WriteOffImportCoordinator

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
    write_offs = WriteOffImportCoordinator(
        SqlAlchemyWriteOffImportReader(session_factory),
        lambda: SqlAlchemyUnitOfWork(session_factory),
        MemoryWriteOffPreviewStore(start_timers=False),
    )
    yield receipts, write_offs, original
    receipts.previews.close()
    write_offs.previews.close()


def stage(write_offs, owner="a", **kwargs):
    return write_offs.preview(parse_write_off_upload(csv_file(**kwargs), "csv"), owner)


def counts(factory):
    with factory() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (WriteOffDocumentRecord, WriteOffLineRecord)
        )


def test_serial_lookup_is_exact_batched_and_read_only(setup, session_factory, engine):
    _, write_offs, _ = setup
    candidate = parse_write_off_upload(csv_file(), "csv").candidate
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        facts = write_offs.reader.inspect(candidate, uuid4())
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(facts.assets) == 8 and len(statements) <= 5
    assert sum("serial_namespace" in sql and " IN " in sql for sql in statements) == 1
    for pair in [("example-issuer", "EXAMPLE-0000"), ("EXAMPLE-ISSUER", "example-0000"), ("EXAMPLE-ISSUER", "0000")]:
        wrong = parse_write_off_upload(csv_file(serials=[pair]), "csv").candidate
        assert not write_offs.reader.inspect(wrong, uuid4()).assets
    with write_offs.reader._snapshot() as session:
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        with pytest.raises(DBAPIError):
            session.execute(text("UPDATE asset_units SET serial_value=serial_value"))


def test_flushed_failure_rolls_back_every_unit_then_retries_same_uuid(setup, session_factory):
    _, write_offs, original = setup

    class Failure(SqlAlchemyUnitOfWork):
        def commit(self):
            self.session.flush()
            raise RuntimeError("synthetic private flush failure")

    write_offs.uow_factory = lambda: Failure(session_factory)
    preview = stage(write_offs)
    assert write_offs.confirm(preview.id, "a").status == "retryable"
    assert counts(session_factory) == (0, 0)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert tuple(uow.asset_units.get(u.id) for u in original.units) == original.units
    write_offs.uow_factory = lambda: SqlAlchemyUnitOfWork(session_factory)
    assert write_offs.confirm(preview.id, "a").write_off_id == preview.registration_id
    assert counts(session_factory) == (1, 8)


@pytest.mark.parametrize("same_identity", [False, True])
def test_concurrent_imports_lock_and_refresh_overlapping_assets(setup, session_factory, same_identity):
    _, write_offs, _ = setup
    previews = [stage(write_offs, owner) for owner in ("a", "b")]
    if same_identity:
        with write_offs.previews._lock:
            write_offs.previews._replace(previews[1].id, "b", registration_id=previews[0].registration_id)
    barrier = Barrier(2)

    class Rendezvous(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            barrier.wait(timeout=10)
            return self

    write_offs.uow_factory = lambda: Rendezvous(session_factory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write_offs.confirm, p.id, owner) for p, owner in zip(previews, ("a", "b"), strict=True)]
        results = [f.result(timeout=15) for f in futures]
    assert sorted(r.status for r in results) == sorted(
        ["registered", "already_registered" if same_identity else "rejected"]
    )
    assert counts(session_factory) == (1, 8)


def test_acceptance_receipt_transfer_writeoff_history_and_retry(setup, session_factory, engine):
    from decimal import Decimal

    from transfer_import_fixtures import csv_file as transfer_csv

    from maas_ua.adapters.transfer_import_files import parse_transfer_upload
    from maas_ua.adapters.transfer_import_memory import MemoryTransferPreviewStore
    from maas_ua.adapters.transfer_import_reader import SqlAlchemyTransferImportReader
    from maas_ua.domain.custody import LifecycleState
    from maas_ua.service_layer.transfer_imports import TransferImportCoordinator

    receipts, imports, original = setup
    transfers = TransferImportCoordinator(
        SqlAlchemyTransferImportReader(session_factory),
        lambda: SqlAlchemyUnitOfWork(session_factory),
        MemoryTransferPreviewStore(start_timers=False),
    )
    preview = transfers.preview(parse_transfer_upload(transfer_csv(), "csv"), "seed")
    assert transfers.confirm(preview.id, "seed").status == "registered"
    reader = SqlAlchemyInventoryReader(engine)
    before = reader.inventory_valuation(GetInventoryValuation(as_of=date(2026, 2, 1)))
    assert before.count == 27 and before.totals[0].acquisition_value == Decimal("391500")
    app = create_app(
        {"TESTING": True, "SECRET_KEY": "synthetic-secret"},
        imports=receipts,
        transfers=transfers,
        write_offs=imports,
        inventory=reader,
    )
    client = app.test_client()
    client.get("/write-offs/import/new")
    with client.session_transaction() as session:
        token = session["csrf_token"]
    response = client.post(
        "/write-offs/import",
        data={
            "csrf_token": token,
            "import_type": "serialized_inventory_write_off_v1",
            "file": (BytesIO(xlsx_file()), "synthetic.xlsx"),
        },
    )
    url = response.headers["Location"]
    assert "INVENTORY" in client.get(url).text and counts(session_factory) == (0, 0)
    result = client.post(url + "/confirm", data={"csrf_token": token}, follow_redirects=True)
    assert "Списання зареєстровано" in result.text and counts(session_factory) == (1, 8)
    active = reader.inventory_valuation(GetInventoryValuation(as_of=date(2026, 2, 1)))
    assert active.count == 19 and active.totals[0].acquisition_value == Decimal("275500")
    disposed = reader.inventory_valuation(
        GetInventoryValuation(
            as_of=date(2026, 2, 1), filters=InventoryFilter(lifecycle_state=LifecycleState.WRITTEN_OFF)
        )
    )
    assert disposed.count == 8 and disposed.totals[0].acquisition_value == Decimal("116000")
    historical = reader.inventory_valuation(GetInventoryValuation(as_of=date(2026, 1, 31)))
    assert historical.count == 27 and historical.totals == before.totals
    unit = next(u for u in original.units if u.serial_number.value == "EXAMPLE-0000")
    history = reader.asset_history(GetAssetHistory(asset_id=unit.id))
    assert history.complete and history.asset.origin.unit_price == Decimal("14500")
    assert [e.kind for day in history.days for e in day.events] == ["RECEIPT", "TRANSFER", "WRITE_OFF"]
    assert client.get(f"/inventory/assets/{unit.id}?as_of=2026-02-01").status_code == 200
    assert client.post(url + "/confirm", data={"csrf_token": token}).status_code == 409
    assert not stage(imports, owner="new").ready
    invalid = stage(
        imports, owner="mixed", serials=[("EXAMPLE-ISSUER", "EXAMPLE-0000"), ("EXAMPLE-ISSUER", "EXAMPLE-0026")]
    )
    assert imports.confirm(invalid.id, "mixed").status == "rejected"
    assert counts(session_factory) == (1, 8)
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert uow.receipts.get(original.receipt.id) == original.receipt
    transfers.previews.close()


@pytest.mark.parametrize("first_write_off", [True, False])
def test_import_transfer_race_revalidates_after_lock_wait(setup, session_factory, first_write_off):
    from threading import Event
    from time import monotonic

    from maas_ua.domain.custody import CustodyAssignment
    from maas_ua.domain.movements import InvalidTransfer

    _, imports, original = setup
    selected = sorted(original.units, key=lambda u: u.serial_number.value)[:8]
    preview = stage(imports)
    held, release, started = Event(), Event(), Event()
    backend = []

    class Holding(SqlAlchemyUnitOfWork):
        def commit(self):
            held.set()
            assert release.wait(10)
            super().commit()

    class Observed(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            backend.append(self.session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            return self

    move = TransferAssets(
        movement_id=uuid4(),
        document_number="EXAMPLE-LATER",
        document_date=date(2026, 2, 2),
        effective_date=date(2026, 2, 2),
        source=selected[0].custody,
        destination=CustodyAssignment("EXAMPLE-OTHER", "EXAMPLE-OTHER"),
        asset_ids=tuple(u.id for u in selected),
    )
    imports.uow_factory = lambda: Holding(session_factory) if first_write_off else Observed(session_factory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        if first_write_off:
            first = pool.submit(imports.confirm, preview.id, "a")
        else:
            first = pool.submit(transfer_assets, move, Holding(session_factory))
        try:
            assert held.wait(5)
            second = (
                pool.submit(transfer_assets, move, Observed(session_factory))
                if first_write_off
                else pool.submit(imports.confirm, preview.id, "a")
            )
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
            assert blocked and not second.done()
        finally:
            release.set()
        first.result(timeout=5)
        if first_write_off:
            with pytest.raises(InvalidTransfer):
                second.result(timeout=5)
            assert counts(session_factory) == (1, 8)
        else:
            assert second.result(timeout=5).status == "rejected"
            assert counts(session_factory) == (0, 0)


def test_uncertain_commit_reconciles_written_off_state_without_second_commit(setup, session_factory):
    _, imports, _ = setup
    inspect = imports.reader.inspect
    unavailable, commits = [False], []

    def read(*args):
        if unavailable[0]:
            raise RuntimeError("synthetic private unavailable")
        return inspect(*args)

    class LostResponse(SqlAlchemyUnitOfWork):
        def commit(self):
            super().commit()
            commits.append(True)
            unavailable[0] = True
            raise RuntimeError("synthetic lost response")

    imports.reader.inspect = read
    imports.uow_factory = lambda: LostResponse(session_factory)
    preview = stage(imports)
    assert imports.confirm(preview.id, "a").status == "retryable"
    assert counts(session_factory) == (1, 8)
    unavailable[0] = False
    result = imports.confirm(preview.id, "a")
    assert result.status == "already_registered" and result.write_off_id == preview.registration_id
    assert result.acquisition_values[0].amount == 116000
    assert len(commits) == 1 and counts(session_factory) == (1, 8)


@pytest.mark.parametrize("eligible", [False, True])
def test_mixed_receipts_and_catalogs_use_each_origin_classification(
    setup, session_factory, write_off_receipt, eligible
):
    from dataclasses import replace

    from maas_ua.domain.receipts import AccountingClassification

    _, imports, _ = setup
    receipt = write_off_receipt.receipt
    line = replace(
        receipt.lines[0],
        accounting_classification=AccountingClassification.INVENTORY
        if eligible
        else AccountingClassification.FIXED_ASSET,
    )
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        # New synthetic receipt and catalog; no rewrite of posted history.
        uow.nomenclatures.add(line.nomenclature)
        uow.receipts.add(replace(receipt, lines=(line,)))
        for unit in write_off_receipt.units:
            uow.asset_units.add(unit)
        uow.commit()
    fixed = write_off_receipt.units[0]
    preview = stage(
        imports,
        serials=[("EXAMPLE-ISSUER", "EXAMPLE-0000"), (fixed.serial_number.namespace, fixed.serial_number.value)],
    )
    assert preview.ready is eligible
    assert imports.confirm(preview.id, "a").status == ("registered" if eligible else "rejected")
    assert counts(session_factory) == ((1, 2) if eligible else (0, 0))
