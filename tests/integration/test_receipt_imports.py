from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import BytesIO
from threading import Barrier
from uuid import uuid4

import pytest
from import_fixtures import csv_file, document_fields, xlsx_file
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from maas_ua.adapters.orm import AssetUnitRecord, ReceiptDocumentRecord, ReceiptLineRecord
from maas_ua.adapters.receipt_import_files import parse_receipt_upload
from maas_ua.adapters.receipt_import_memory import MemoryPreviewStore
from maas_ua.adapters.receipt_import_reader import SqlAlchemyReceiptImportReader
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.domain.receipts import Nomenclature, TrackingPolicy
from maas_ua.entrypoints.flask_app import create_app
from maas_ua.service_layer.receipt_imports import ReceiptImportCoordinator
from maas_ua.service_layer.write_offs import WriteOffAssets, write_off_assets

pytestmark = pytest.mark.integration


@pytest.fixture
def imports(session_factory):
    catalog = Nomenclature(
        "EXAMPLE-ID", TrackingPolicy.SERIALIZED, code="EXAMPLE-FPV", name="Example", unit_of_measure="pcs"
    )
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        uow.nomenclatures.add(catalog)
        uow.commit()
    return ReceiptImportCoordinator(
        SqlAlchemyReceiptImportReader(session_factory),
        lambda: SqlAlchemyUnitOfWork(session_factory),
        MemoryPreviewStore(start_timers=False),
    )


def counts(session_factory):
    with session_factory() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(record))
            for record in (ReceiptDocumentRecord, ReceiptLineRecord, AssetUnitRecord)
        )


def test_preview_no_property_writes_then_27_asset_receipt_reloads(imports, session_factory):
    preview = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    assert counts(session_factory) == (0, 0, 0)
    assert preview.ready
    result = imports.confirm(preview.id, "a")
    assert result.status == "registered"
    original = imports.receipt(preview.id, "a")
    assert len(original.units) == 27
    assert {unit.receipt_line_id for unit in original.units} == {original.receipt.lines[0].id}
    assert original.receipt.lines[0].total.amount == 391500
    assert counts(session_factory) == (1, 1, 27)


def test_invalid_quantity_and_existing_serial_conflicts_never_partially_register(imports, session_factory):
    bad = imports.preview(parse_receipt_upload(csv_file(fields=document_fields(line_quantity="26")), "csv"), "a")
    assert imports.confirm(bad.id, "a").status == "rejected"
    good = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    imports.confirm(good.id, "a")
    conflict = imports.preview(
        parse_receipt_upload(csv_file(fields=document_fields(unit_price="1", line_total="27")), "csv"), "a"
    )
    assert imports.confirm(conflict.id, "a").status == "rejected"
    assert counts(session_factory) == (1, 1, 27)


def test_two_fresh_coordinators_reconcile_same_original_receipt(imports, session_factory):
    parsed = parse_receipt_upload(csv_file(), "csv")
    preview = imports.preview(parsed, "a")
    result = imports.confirm(preview.id, "a")
    fresh = ReceiptImportCoordinator(
        SqlAlchemyReceiptImportReader(session_factory),
        lambda: SqlAlchemyUnitOfWork(session_factory),
        MemoryPreviewStore(start_timers=False),
    )
    second = fresh.preview(parsed, "b")
    assert fresh.confirm(second.id, "b").status == "already_registered"
    assert fresh.receipt(second.id, "b").receipt.id == result.receipt_id
    assert counts(session_factory) == (1, 1, 27)


def test_concurrent_uploads_passing_prechecks_cannot_duplicate_assets(imports, session_factory):
    parsed = parse_receipt_upload(csv_file(), "csv")
    previews = [imports.preview(parsed, owner) for owner in ("a", "b")]
    barrier = Barrier(2)

    class RendezvousUow(SqlAlchemyUnitOfWork):
        def commit(self):
            barrier.wait(timeout=10)
            super().commit()

    imports.uow_factory = lambda: RendezvousUow(session_factory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda pair: imports.confirm(pair[0].id, pair[1]), zip(previews, ("a", "b"), strict=True))
        )
    assert sorted(item.status for item in results) == ["already_registered", "registered"]
    assert results[0].receipt_id == results[1].receipt_id
    assert counts(session_factory) == (1, 1, 27)


def test_late_unique_conflict_rolls_back_other_units_and_receipt(imports, session_factory):
    parsed = parse_receipt_upload(csv_file(), "csv")
    preview = imports.preview(parsed, "a")

    class InjectConflict(SqlAlchemyUnitOfWork):
        def commit(self):
            other = ReceiptImportCoordinator(
                SqlAlchemyReceiptImportReader(session_factory),
                lambda: SqlAlchemyUnitOfWork(session_factory),
                MemoryPreviewStore(start_timers=False),
            )
            single = parse_receipt_upload(
                csv_file(fields=document_fields(line_quantity="1", line_total="14500", source="OTHER"), count=1), "csv"
            )
            p = other.preview(single, "b")
            assert other.confirm(p.id, "b").status == "registered"
            super().commit()

    imports.uow_factory = lambda: InjectConflict(session_factory)
    result = imports.confirm(preview.id, "a")
    assert result.status == "rejected"
    assert any(issue.cell.row == 2 for issue in result.issues)
    assert counts(session_factory) == (1, 1, 1)


def test_uncertain_commit_recovery_uses_persisted_receipt(imports, session_factory):
    class LostResponse(SqlAlchemyUnitOfWork):
        def commit(self):
            super().commit()
            raise RuntimeError("synthetic lost response")

    imports.uow_factory = lambda: LostResponse(session_factory)
    preview = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    assert imports.confirm(preview.id, "a").status == "already_registered"
    assert imports.receipt(preview.id, "a").receipt.id == preview.registration_id
    assert counts(session_factory) == (1, 1, 27)


def test_reader_retains_all_units_after_writeoff(imports, session_factory):
    preview = imports.preview(parse_receipt_upload(csv_file(), "csv"), "a")
    imports.confirm(preview.id, "a")
    original = imports.receipt(preview.id, "a")
    write_off_assets(
        WriteOffAssets(
            write_off_id=uuid4(),
            document_number="EXAMPLE-WRITE-OFF",
            document_date=date(2026, 2, 1),
            authorization_reference="EXAMPLE-AUTH",
            authorization_date=date(2026, 2, 1),
            effective_date=date(2026, 2, 1),
            asset_ids=(original.units[0].id,),
        ),
        SqlAlchemyUnitOfWork(session_factory),
    )
    again = imports.preview(parse_receipt_upload(csv_file(), "csv"), "b")
    assert imports.confirm(again.id, "b").status == "already_registered"
    assert len(imports.receipt(again.id, "b").units) == 27


def test_reader_uses_repeatable_read_read_only_transaction(imports):
    with imports.reader._snapshot() as session:
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        with pytest.raises(DBAPIError):
            session.execute(text("UPDATE receipt_documents SET source=source"))


def test_full_flask_xlsx_receipt_flow_persists_27_units(imports, session_factory):
    app = create_app({"TESTING": True, "SECRET_KEY": "synthetic-test-secret"}, imports=imports)
    client = app.test_client()
    client.get("/imports/new")
    with client.session_transaction() as session:
        token = session["csrf_token"]
    response = client.post(
        "/imports",
        data={
            "csrf_token": token,
            "import_type": "serialized_receipt_v1",
            "file": (BytesIO(xlsx_file()), "synthetic.xlsx"),
        },
    )
    assert response.status_code == 303
    assert counts(session_factory) == (0, 0, 0)
    assert b"Confirm registration" in client.get(response.headers["Location"]).data
    result = client.post(response.headers["Location"] + "/confirm", data={"csrf_token": token}, follow_redirects=True)
    assert result.status_code == 200 and b"Receipt registered" in result.data
    assert b"EXAMPLE-0026" in result.data
    assert counts(session_factory) == (1, 1, 27)
