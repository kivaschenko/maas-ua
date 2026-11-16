"""HTTP behavior with immutable query DTOs; PostgreSQL scenarios live in integration tests."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from maas_ua.adapters.memory import FakeStore, FakeUnitOfWork
from maas_ua.adapters.receipt_import_memory import FakeReceiptImportReader, MemoryPreviewStore
from maas_ua.entrypoints.flask_app import create_app
from maas_ua.service_layer.inventory import (
    AssetDetails,
    AssetEvent,
    AssetHistory,
    AssetNotFound,
    AssetPosition,
    CurrencyTotal,
    HistoryDay,
    InventoryDiagnostic,
    InventoryGroup,
    InventoryReport,
    NomenclatureInfo,
    ReceiptOrigin,
)
from maas_ua.service_layer.receipt_imports import ReceiptImportCoordinator


class Reader:
    def __init__(self, receipt):
        doc, line, unit = receipt.receipt, receipt.receipt.lines[0], receipt.units[0]
        origin = ReceiptOrigin(
            doc.id,
            line.id,
            doc.document_number,
            doc.document_date,
            doc.effective_date,
            doc.created_at,
            doc.source,
            line.unit_price.amount,
            line.unit_price.currency,
            line.quantity.unit,
            line.analytics,
            line.accounting_classification,
        )
        self.asset = AssetDetails(
            unit.id,
            unit.serial_number,
            NomenclatureInfo(line.nomenclature.id, line.nomenclature.code, line.nomenclature.name),
            origin,
            unit.initial_custody,
            "ACTIVE",
            (),
        )
        event = AssetEvent(
            "RECEIPT",
            doc.id,
            doc.document_number,
            doc.document_date,
            doc.effective_date,
            doc.created_at,
            None,
            unit.initial_custody,
            None,
        )
        self.history = AssetHistory(self.asset, (HistoryDay(doc.effective_date, (event,), True),), ())
        self.position = AssetPosition(unit.id, date(2026, 3, 1), True, unit.initial_custody, (), "ACTIVE")
        group = InventoryGroup(
            self.asset.nomenclature, origin, "EXAMPLE-A", "EXAMPLE-A", 27, Decimal("391500"), "ACTIVE"
        )
        self.report = InventoryReport(
            date(2026, 3, 1), ("location", "custodian"), (group,), (), 27, (CurrencyTotal("UAH", Decimal("391500")),)
        )
        self.calls = []

    def asset_by_serial(self, query):
        self.calls.append(query)
        if (query.namespace, query.value) != (self.asset.serial_number.namespace, self.asset.serial_number.value):
            raise AssetNotFound()
        return self.asset

    def asset_history(self, query):
        self.calls.append(query)
        if query.asset_id != self.asset.asset_id:
            raise AssetNotFound()
        return self.history

    def asset_position(self, query):
        self.calls.append(query)
        return replace(self.position, as_of=query.as_of)

    def inventory_balance(self, query):
        self.calls.append(query)
        return self.report

    inventory_valuation = inventory_balance


@pytest.fixture
def ui(write_off_receipt):
    reader = Reader(write_off_receipt)
    store = FakeStore()
    previews = MemoryPreviewStore(start_timers=False)
    imports = ReceiptImportCoordinator(FakeReceiptImportReader(store), lambda: FakeUnitOfWork(store), previews)
    app = create_app({"TESTING": True, "SECRET_KEY": "synthetic"}, imports=imports, inventory=reader)
    yield app.test_client(), reader
    previews.close()


def test_exact_search_redirects_to_asset_and_unknown_is_safe(ui):
    client, reader = ui
    assert client.get("/inventory/search").status_code == 200
    serial = reader.asset.serial_number
    response = client.get(
        "/inventory/search", query_string={"namespace": serial.namespace, "serial_value": serial.value}
    )
    assert response.status_code == 303
    assert response.headers["Location"].endswith(str(reader.asset.asset_id))
    assert reader.calls[-1].value == serial.value
    response = client.get("/inventory/search", query_string={"namespace": serial.namespace, "serial_value": "missing"})
    assert response.status_code == 404
    assert "не знайдено" in response.text
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "parameters",
    [
        {"namespace": " EXAMPLE", "serial_value": "0"},
        {"namespace": "EXAMPLE", "serial_value": "0 "},
        {"namespace": "EXAMPLE"},
        {"namespace": "", "serial_value": "0"},
        {"namespace": "EXAMPLE", "serial_value": "x\x00"},
        [("namespace", "EXAMPLE"), ("namespace", "OTHER"), ("serial_value", "0")],
    ],
)
def test_invalid_search_never_calls_reader(ui, parameters):
    client, reader = ui
    assert client.get("/inventory/search", query_string=parameters).status_code == 400
    assert not reader.calls


def test_details_origin_history_position_and_escaping(ui):
    client, reader = ui
    origin = replace(reader.asset.origin, source="<script>synthetic</script>")
    reader.history = replace(reader.history, asset=replace(reader.asset, origin=origin))
    page = client.get(f"/inventory/assets/{reader.asset.asset_id}?as_of=2026-03-01")
    assert page.status_code == 200
    for value in ["EXAMPLE-RECEIPT", "14500.00", "UAH", "EXAMPLE-A", "2026-03-01", "Первісна ціна придбання"]:
        assert value in page.text
    assert "&lt;script&gt;synthetic" in page.text
    assert "<script>synthetic" not in page.text
    assert "окремі знімки" in page.text


def test_missing_uuid_unknown_uuid_and_invalid_dates(ui):
    client, reader = ui
    assert client.get("/inventory/assets/").status_code == 400
    assert client.get("/inventory/assets/not-a-uuid").status_code == 400
    assert client.get(f"/inventory/assets/{uuid4()}").status_code == 404
    reader.calls.clear()
    for value in ("2026-02-30", "20260101", "2026-W01-1", "", "2026-01-01T12:00:00"):
        assert (
            client.get(f"/inventory/assets/{reader.asset.asset_id}", query_string={"as_of": value}).status_code == 400
        )
    assert not reader.calls


def test_incomplete_history_does_not_infer_custody_or_same_day_order(ui):
    client, reader = ui
    event = reader.history.days[0].events[0]
    day = HistoryDay(date(2026, 2, 1), (replace(event, kind="TRANSFER"), replace(event, kind="TRANSFER")), False)
    reader.history = replace(reader.history, days=(day,), issues=("ambiguous_movement_order",))
    reader.position = replace(reader.position, custody=None, issues=("ambiguous_movement_order",))
    page = client.get(f"/inventory/assets/{reader.asset.asset_id}")
    assert page.status_code == 200
    assert "Порядок подій цього дня невідомий" in page.text
    assert "Історія неповна" in page.text
    assert "Не встановлено" in page.text


@pytest.mark.parametrize(
    "view,query_type", [("balance", "GetInventoryBalance"), ("valuation", "GetInventoryValuation")]
)
def test_balance_filters_and_verified_totals(ui, view, query_type):
    client, reader = ui
    params = dict(
        as_of="2026-03-01",
        nomenclature_id="EXAMPLE",
        source="EXAMPLE",
        location_ref="EXAMPLE-A",
        responsible_party_ref="EXAMPLE-A",
        currency="UAH",
        lifecycle_state="WRITTEN_OFF",
        view=view,
    )
    response = client.get("/inventory/balances", query_string=params)
    assert response.status_code == 200
    query = reader.calls[-1]
    assert type(query).__name__ == query_type
    assert query.as_of == date(2026, 3, 1)
    for field in ("nomenclature_id", "source", "location_ref", "responsible_party_ref", "currency", "lifecycle_state"):
        assert getattr(query.filters, field) == params[field]
    assert "391500" in response.text and "pcs" in response.text and "EXAMPLE-RECEIPT" in response.text


def test_empty_report_and_separate_currency_totals(ui):
    client, reader = ui
    reader.report = replace(reader.report, groups=(), candidate_count=0, known_acquisition_totals=())
    page = client.get("/inventory/balances")
    assert "Немає записів" in page.text and "Серійних записів: 0" in page.text
    reader.report = replace(
        reader.report,
        known_acquisition_totals=(CurrencyTotal("UAH", Decimal("10.12345")), CurrencyTotal("USD", Decimal("2.50"))),
    )
    page = client.get("/inventory/balances")
    assert "10.12345 UAH" in page.text and "2.50 USD" in page.text


def test_incomplete_report_hides_all_partial_quantities_values_and_groups(ui):
    client, reader = ui
    reader.report = replace(
        reader.report, diagnostics=(InventoryDiagnostic(("missing_initial_custody",), 27, "UAH", Decimal("391500")),)
    )
    page = client.get("/inventory/balances")
    assert page.status_code == 200
    assert "Підтверджений залишок недоступний" in page.text
    assert "391500" not in page.text and "EXAMPLE-RECEIPT" not in page.text
    assert "Серійних записів: 27" not in page.text


@pytest.mark.parametrize(
    "parameters",
    [
        {"as_of": "yesterday"},
        {"currency": " UAH"},
        {"lifecycle_state": "INVALID"},
        {"view": "INVALID"},
        {"receipt_id": "not-a-uuid"},
    ],
)
def test_invalid_balance_input_never_calls_reader(ui, parameters):
    client, reader = ui
    assert client.get("/inventory/balances", query_string=parameters).status_code == 400
    assert not reader.calls


def test_reader_failure_is_safe_and_never_logged(ui, caplog):
    client, reader = ui

    def fail(query):
        raise RuntimeError("synthetic SQL private marker")

    reader.asset_history = fail
    response = client.get(f"/inventory/assets/{reader.asset.asset_id}")
    assert response.status_code == 503
    assert "private marker" not in response.text + caplog.text
