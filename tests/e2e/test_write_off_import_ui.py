from io import BytesIO

import pytest
from import_fixtures import csv_file as receipt_csv
from openpyxl import load_workbook
from write_off_import_fixtures import csv_file, write_off_fields, xlsx_file

from maas_ua.adapters.memory import FakeStore, FakeUnitOfWork
from maas_ua.adapters.receipt_import_files import parse_receipt_upload
from maas_ua.adapters.receipt_import_memory import FakeReceiptImportReader, MemoryPreviewStore
from maas_ua.adapters.write_off_import_memory import FakeWriteOffImportReader, MemoryWriteOffPreviewStore
from maas_ua.domain.receipts import Nomenclature, TrackingPolicy
from maas_ua.entrypoints.flask_app import create_app
from maas_ua.service_layer.receipt_imports import ReceiptImportCoordinator
from maas_ua.service_layer.write_off_imports import WriteOffImportCoordinator


@pytest.fixture
def app():
    catalog = Nomenclature(
        "EXAMPLE-ID", TrackingPolicy.SERIALIZED, code="EXAMPLE-FPV", name="Example", unit_of_measure="pcs"
    )
    store = FakeStore(nomenclatures=(catalog,))
    receipts = ReceiptImportCoordinator(
        FakeReceiptImportReader(store), lambda: FakeUnitOfWork(store), MemoryPreviewStore(start_timers=False)
    )
    preview = receipts.preview(parse_receipt_upload(receipt_csv(), "csv"), "seed")
    receipts.confirm(preview.id, "seed")
    write_offs = WriteOffImportCoordinator(
        FakeWriteOffImportReader(store), lambda: FakeUnitOfWork(store), MemoryWriteOffPreviewStore(start_timers=False)
    )
    app = create_app({"TESTING": True, "SECRET_KEY": "synthetic-test-secret"}, imports=receipts, write_offs=write_offs)
    app.extensions["test_store"] = store
    yield app
    receipts.previews.close()
    write_offs.previews.close()


def csrf(client):
    client.get("/write-offs/import/new")
    with client.session_transaction() as session:
        return session["csrf_token"]


def upload(client, data, kind="xlsx"):
    return client.post(
        "/write-offs/import",
        data={
            "csrf_token": csrf(client),
            "import_type": "serialized_inventory_write_off_v1",
            "file": (BytesIO(data), f"synthetic.{kind}"),
        },
    )


@pytest.mark.parametrize("kind,make", [("xlsx", xlsx_file), ("csv", csv_file)])
def test_upload_preview_confirm_result_links_and_duplicate(app, kind, make):
    client = app.test_client()
    response = upload(client, make(), kind)
    assert response.status_code == 303
    url = response.headers["Location"]
    page = client.get(url)
    assert "Підтвердити списання" in page.text
    assert "EXAMPLE-AUTH" in page.text and "EXAMPLE-0007" in page.text
    assert not app.extensions["test_store"].write_offs
    result = client.post(url + "/confirm", data={"csrf_token": csrf(client)}, follow_redirects=True)
    assert result.status_code == 200 and "Списання зареєстровано" in result.text
    assert result.text.count("/inventory/assets/") == 8
    assert client.post(url + "/confirm", data={"csrf_token": csrf(client)}).status_code == 409
    assert len(app.extensions["test_store"].write_offs) == 1
    assert result.headers["Cache-Control"] == "no-store"
    with client.session_transaction() as session:
        assert set(session) == {"csrf_token", "import_session"}
    again = upload(client, make(), kind)
    assert "Підтвердити списання" not in client.get(again.headers["Location"]).text


def test_csrf_ownership_expiry_discard_and_size(app):
    client = app.test_client()
    assert client.post("/write-offs/import", data={}).status_code == 400
    assert upload(client, b"x", "xlsm").status_code == 415
    assert upload(client, b"x" * (5 * 1024 * 1024 + 1), "csv").status_code == 413
    url = upload(client, xlsx_file()).headers["Location"]
    other = app.test_client()
    assert other.get(url).status_code == 410
    assert other.post(url + "/confirm", data={"csrf_token": csrf(other)}).status_code == 410
    assert client.post(url + "/confirm", data={"csrf_token": "invalid"}).status_code == 400
    assert client.post(url + "/discard", data={"csrf_token": csrf(client)}).status_code == 303
    assert client.get(url).status_code == 410
    now = [0]
    app.extensions["write_off_imports"].previews = MemoryWriteOffPreviewStore(
        ttl_seconds=10, clock=lambda: now[0], start_timers=False
    )
    url = upload(client, xlsx_file()).headers["Location"]
    now[0] = 11
    assert client.get(url).status_code == 410


def test_invalid_preview_and_database_failure_render_safe_ukrainian_errors(app, caplog):
    client = app.test_client()
    response = upload(client, xlsx_file(serials=[("MISSING", "0000")]))
    page = client.get(response.headers["Location"])
    assert "Майно не знайдено" in page.text and "SerialNumbers" in page.text
    assert "Підтвердити списання" not in page.text

    def fail(*args):
        raise RuntimeError("synthetic-private-sql-error")

    app.extensions["write_off_imports"].reader.inspect = fail
    response = upload(client, xlsx_file())
    page = client.get(response.headers["Location"])
    assert "Перевірка недоступна" in page.text
    assert "synthetic-private" not in page.text + caplog.text


def test_escaping_and_download_templates(app):
    client = app.test_client()
    response = upload(client, xlsx_file(fields=write_off_fields(document_number="<script>synthetic</script>")))
    assert "&lt;script&gt;synthetic" in client.get(response.headers["Location"]).text
    response = client.get("/write-offs/import/template/xlsx")
    workbook = load_workbook(BytesIO(response.data))
    assert workbook.sheetnames == ["Document", "SerialNumbers"]
    fields = dict(workbook["Document"].values)
    assert fields["template_version"] == "serialized_inventory_write_off_v1"
    assert not any("uuid" in key for key in fields)
    workbook.close()
    assert (
        b"serialized_inventory_write_off_v1" not in client.get("/write-offs/import/template/csv").data
    )  # header-only blank template
