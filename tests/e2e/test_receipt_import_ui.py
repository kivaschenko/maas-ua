import logging
from io import BytesIO

import pytest
from import_fixtures import csv_file, document_fields, xlsx_file
from openpyxl import load_workbook

from maas_ua.adapters.memory import FakeStore, FakeUnitOfWork
from maas_ua.adapters.receipt_import_memory import FakeReceiptImportReader, MemoryPreviewStore
from maas_ua.domain.receipts import Nomenclature, TrackingPolicy
from maas_ua.entrypoints.flask_app import create_app
from maas_ua.service_layer.receipt_imports import ReceiptImportCoordinator


@pytest.fixture
def app():
    catalog = Nomenclature(
        "EXAMPLE-ID", TrackingPolicy.SERIALIZED, code="EXAMPLE-FPV", name="Example", unit_of_measure="pcs"
    )
    store = FakeStore(nomenclatures=(catalog,))
    coordinator = ReceiptImportCoordinator(
        FakeReceiptImportReader(store), lambda: FakeUnitOfWork(store), MemoryPreviewStore(start_timers=False)
    )
    app = create_app({"TESTING": True, "SECRET_KEY": "synthetic-test-secret"}, imports=coordinator)
    app.extensions["test_store"] = store
    yield app
    coordinator.previews.close()


def csrf(client):
    client.get("/imports/new")
    with client.session_transaction() as session:
        return session["csrf_token"]


def upload(client, data, kind="xlsx", **extra):
    return client.post(
        "/imports",
        data={
            "csrf_token": csrf(client),
            "import_type": "serialized_receipt_v1",
            "delimiter": ",",
            "file": (BytesIO(data), f"synthetic.{kind}"),
            **extra,
        },
    )


@pytest.mark.parametrize("kind,make_file", [("xlsx", xlsx_file), ("csv", csv_file)])
def test_upload_preview_confirm_and_retrieve_27_units(app, kind, make_file):
    client = app.test_client()
    response = upload(client, make_file(), kind)
    assert response.status_code == 303
    preview_url = response.headers["Location"]
    preview = client.get(preview_url)
    assert b"Confirm registration" in preview.data and b"391500" in preview.data
    assert not app.extensions["test_store"].asset_units
    result = client.post(preview_url + "/confirm", data={"csrf_token": csrf(client)})
    assert result.status_code == 303
    page = client.get(result.headers["Location"])
    assert b"Receipt registered" in page.data and b"EXAMPLE-0026" in page.data
    assert len(app.extensions["test_store"].asset_units) == 27
    assert client.post(preview_url + "/confirm", data={"csrf_token": csrf(client)}).status_code == 409
    assert len(app.extensions["test_store"].asset_units) == 27
    assert page.headers["Cache-Control"] == "no-store"
    with client.session_transaction() as session:
        assert set(session) == {"csrf_token", "import_session"}


def test_home_dashboard_links_to_existing_workflows_and_loads_local_styles(app):
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert "Робоче місце обліковця" in response.text
    assert "Military Asset Accounting System — Ukraine" in response.text
    assert 'href="/imports/new"' in response.text
    assert 'href="/transfers/import/new"' in response.text
    assert 'href="/inventory/search"' in response.text
    assert 'href="/inventory/balances"' in response.text
    assert "<details" in response.text and "Розділи обліку" in response.text
    stylesheet = client.get("/static/main.css")
    assert stylesheet.status_code == 200
    assert "text/css" in stylesheet.content_type


def test_upload_accepts_valid_csv_larger_than_form_memory_limit(app):
    client = app.test_client()
    fields = document_fields(line_quantity="60", line_total="870000.00")
    data = csv_file(fields=fields, count=60)
    assert len(data) > 8 * 1024

    response = upload(client, data, "csv")

    assert response.status_code == 303


def test_preview_shows_all_row_errors_and_cannot_partially_register(app):
    client = app.test_client()
    data = xlsx_file(serials=[("EXAMPLE", "EXAMPLE-DUPLICATE")] * 27)
    uploaded = upload(client, data)
    page = client.get(uploaded.headers["Location"])
    assert b"duplicate_serial" in page.data and b"SerialNumbers" in page.data
    assert b"Confirm registration" not in page.data
    client.post(uploaded.headers["Location"] + "/confirm", data={"csrf_token": csrf(client)})
    assert not app.extensions["test_store"].receipts


def test_reupload_and_conflicting_existing_original_facts(app):
    client = app.test_client()
    first = upload(client, xlsx_file())
    client.post(first.headers["Location"] + "/confirm", data={"csrf_token": csrf(client)})
    again = upload(client, xlsx_file())
    result = client.post(
        again.headers["Location"] + "/confirm", data={"csrf_token": csrf(client)}, follow_redirects=True
    )
    assert b"Already registered" in result.data
    conflicting = upload(client, xlsx_file(fields=document_fields(source="OTHER")))
    assert b"serial_conflict" in client.get(conflicting.headers["Location"]).data
    assert len(app.extensions["test_store"].asset_units) == 27


def test_csrf_type_formats_size_and_private_session_access(app):
    client = app.test_client()
    assert client.post("/imports", data={}).status_code == 400
    assert upload(client, b"anything", "xlsm").status_code == 415
    assert upload(client, csv_file(), "csv", import_type="").status_code == 400
    assert upload(client, b"x" * (5 * 1024 * 1024 + 1), "csv").status_code == 413
    uploaded = upload(client, xlsx_file())
    other = app.test_client()
    assert other.get(uploaded.headers["Location"]).status_code == 410
    token = csrf(other)
    assert other.post(uploaded.headers["Location"] + "/confirm", data={"csrf_token": token}).status_code == 410


def test_expired_discarded_and_nonlocal_previews_fail_safely(app):
    client = app.test_client()
    uploaded = upload(client, xlsx_file())
    url = uploaded.headers["Location"]
    assert client.post(url + "/discard", data={"csrf_token": csrf(client)}).status_code == 303
    assert client.get(url).status_code == 410
    assert client.get("/imports/new", environ_overrides={"REMOTE_ADDR": "192.0.2.1"}).status_code == 403
    assert client.get("/imports/new", environ_overrides={"wsgi.multiprocess": True}).status_code == 503
    assert client.get("/imports/new", headers={"Host": "untrusted.example"}).status_code == 400


def test_worksheet_values_are_escaped_and_never_logged(app, caplog):
    client = app.test_client()
    value = "<script>synthetic-private-marker</script>"
    uploaded = upload(client, xlsx_file(fields=document_fields(source=value)))
    page = client.get(uploaded.headers["Location"])
    assert b"&lt;script&gt;" in page.data
    assert b"<script>synthetic" not in page.data
    assert value not in caplog.text


def test_downloaded_xlsx_is_versioned_two_sheet_template_without_uuid_fields(app):
    client = app.test_client()
    response = client.get("/imports/template/xlsx")
    assert response.status_code == 200
    workbook = load_workbook(BytesIO(response.data))
    assert workbook.sheetnames == ["Document", "SerialNumbers"]
    fields = dict(workbook["Document"].values)
    assert fields["template_version"] == "serialized_receipt_v1"
    assert not any("uuid" in key or "import_id" in key for key in fields)
    assert workbook["SerialNumbers"].column_dimensions["A"].number_format == "@"
    assert workbook["SerialNumbers"].column_dimensions["B"].number_format == "@"
    assert workbook["Document"]["B7"].number_format == "@"  # exact nomenclature code
    workbook.close()


def test_factory_requires_single_process_local_mode(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(ValueError, match="single"):
        create_app({"TESTING": True})


def test_default_factory_assembles_without_connecting_or_migrating(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/synthetic")
    app = create_app({"TESTING": True})
    assert app.test_client().get("/imports/new").status_code == 200
    app.extensions["receipt_imports"].previews.close()


def test_factory_uses_configured_secret_key_from_environment(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/synthetic")
    monkeypatch.setenv("SECRET_KEY", "synthetic-environment-secret")
    app = create_app({"TESTING": True})
    assert app.secret_key == "synthetic-environment-secret"
    app.extensions["receipt_imports"].previews.close()


def test_debug_requests_are_rejected_before_accessing_import_data(app):
    assert not app.debug
    app.config["DEBUG"] = True
    assert app.test_client().get("/imports/new").status_code == 503


def test_development_access_logs_omit_search_identities(app, caplog):
    from werkzeug.serving import WSGIRequestHandler

    # Exercise the installed development server's actual log formatting.
    handler = object.__new__(WSGIRequestHandler)
    handler.path = "/inventory/search?namespace=synthetic-private-namespace&serial_value=synthetic-private-serial"
    handler.command = "GET"
    handler.request_version = "HTTP/1.1"
    handler.client_address = ("127.0.0.1", 12345)
    with caplog.at_level(logging.INFO, logger="werkzeug"):
        handler.log_request(200)
    assert "200" in caplog.text
    assert "synthetic-private" not in caplog.text
