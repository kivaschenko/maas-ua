"""M7.2 acceptance using XLSX, Flask and existing property commands on disposable PostgreSQL."""

from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from import_fixtures import xlsx_file
from sqlalchemy import text

from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.domain.custody import CustodyAssignment
from maas_ua.domain.receipts import Nomenclature, TrackingPolicy
from maas_ua.entrypoints.flask_app import create_app
from maas_ua.service_layer.inventory import GetAssetHistory, GetInventoryBalance
from maas_ua.service_layer.movements import TransferAssets, transfer_assets
from maas_ua.service_layer.write_offs import WriteOffAssets, write_off_assets

pytestmark = pytest.mark.integration


@pytest.fixture
def explorer(session_factory, engine):
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        uow.nomenclatures.add(
            Nomenclature(
                "EXAMPLE-ID",
                TrackingPolicy.SERIALIZED,
                code="EXAMPLE-FPV",
                name="Example",
                unit_of_measure="EXAMPLE-SET",
            )
        )
        uow.commit()
    # Exercise the production factory assembly, including both readers on one engine.
    app = create_app({"TESTING": True, "SECRET_KEY": "synthetic", "DATABASE_URL": engine.url})
    client = app.test_client()
    client.get("/imports/new")
    with client.session_transaction() as session:
        token, owner = session["csrf_token"], session["import_session"]
    response = client.post(
        "/imports",
        data={
            "csrf_token": token,
            "import_type": "serialized_receipt_v1",
            "file": (BytesIO(xlsx_file()), "synthetic.xlsx"),
        },
    )
    assert response.status_code == 303
    preview_url = response.headers["Location"]
    result = client.post(preview_url + "/confirm", data={"csrf_token": token}, follow_redirects=True)
    assert result.status_code == 200 and "Receipt registered" in result.text
    imports = app.extensions["receipt_imports"]
    from uuid import UUID

    original = imports.receipt(UUID(preview_url.rsplit("/", 1)[1]), owner)
    assert len(original.units) == 27
    assert f"/inventory/assets/{original.units[0].id}" in result.text
    yield client, original, app.extensions["inventory_reader"]
    imports.previews.close()
    app.extensions["inventory_reader"].engine.dispose()


def test_27_import_12_transfer_8_writeoff_historical_ui(explorer, session_factory):
    client, original, reader = explorer
    unit = original.units[0]
    search = client.get(
        "/inventory/search",
        query_string={"namespace": unit.serial_number.namespace, "serial_value": unit.serial_number.value},
    )
    assert search.status_code == 303
    detail_url = search.headers["Location"]
    page = client.get(detail_url)
    for value in ("EXAMPLE-125", "14500.00", "UAH", "EXAMPLE-SET", "EXAMPLE-SOURCE"):
        assert value in page.text
    source = unit.custody
    b, c = CustodyAssignment("EXAMPLE-B", "EXAMPLE-B"), CustodyAssignment("EXAMPLE-C", "EXAMPLE-C")
    for day, old, new, number in [
        (date(2026, 2, 1), source, b, "EXAMPLE-MOVE-1"),
        (date(2026, 2, 2), b, c, "EXAMPLE-MOVE-2"),
    ]:
        transfer_assets(
            TransferAssets(
                movement_id=uuid4(),
                document_number=number,
                document_date=day,
                effective_date=day,
                source=old,
                destination=new,
                asset_ids=tuple(unit.id for unit in original.units[:12]),
            ),
            SqlAlchemyUnitOfWork(session_factory),
        )
    for day, location in [("2026-01-31", "EXAMPLE-LOCATION"), ("2026-02-01", "EXAMPLE-B"), ("2026-02-02", "EXAMPLE-C")]:
        page = client.get(detail_url, query_string={"as_of": day})
        historical = page.text.split("<h2>Історична позиція</h2>")[1].split("<h2>Історія документів</h2>")[0]
        assert location in historical and "ACTIVE" in historical
    before_receipt = client.get(detail_url + "?as_of=2026-01-15").text
    historical = before_receipt.split("<h2>Історична позиція</h2>")[1].split("<h2>Історія документів</h2>")[0]
    assert "Ні" in historical and "EXAMPLE-LOCATION" not in historical
    write_off_assets(
        WriteOffAssets(
            write_off_id=uuid4(),
            document_number="EXAMPLE-WRITE-OFF",
            document_date=date(2026, 2, 9),
            authorization_reference="EXAMPLE-AUTH",
            authorization_date=date(2026, 2, 9),
            effective_date=date(2026, 2, 10),
            asset_ids=tuple(unit.id for unit in original.units[:8]),
        ),
        SqlAlchemyUnitOfWork(session_factory),
    )
    for day, state in [("2026-02-09", "ACTIVE"), ("2026-02-10", "WRITTEN_OFF")]:
        page = client.get(detail_url, query_string={"as_of": day})
        historical = page.text.split("<h2>Історична позиція</h2>")[1].split("<h2>Історія документів</h2>")[0]
        assert state in historical
        assert ("EXAMPLE-AUTH" in historical) == (state == "WRITTEN_OFF")
    assert "EXAMPLE-MOVE-1" in page.text and "EXAMPLE-MOVE-2" in page.text
    assert "EXAMPLE-WRITE-OFF" in page.text and "EXAMPLE-AUTH" in page.text
    report = reader.inventory_balance(GetInventoryBalance(as_of=date(2026, 2, 10)))
    assert report.complete and report.count == 19
    assert report.totals[0].acquisition_value == Decimal("275500.00")
    for view in ("balance", "valuation"):
        page = client.get("/inventory/balances", query_string={"as_of": "2026-02-10", "view": view})
        assert "Серійних записів: 19" in page.text and "275500.00 UAH" in page.text
    assert len(reader.asset_history(GetAssetHistory(asset_id=unit.id)).days) == 4
    assert (
        client.get(
            "/inventory/search",
            query_string={"namespace": unit.serial_number.namespace, "serial_value": unit.serial_number.value},
        ).status_code
        == 303
    )
    page = client.get("/inventory/balances?as_of=2026-02-10&lifecycle_state=WRITTEN_OFF")
    assert "Серійних записів: 8" in page.text and "116000.00 UAH" in page.text
    # Original receipt remains exactly intact after all movements and write-offs.
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert uow.receipts.get(original.receipt.id) == original.receipt


def test_postgres_incomplete_history_never_exposes_partial_ui_totals(explorer, engine):
    client, original, _ = explorer
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE asset_units SET initial_location_ref=NULL, initial_party_ref=NULL WHERE id=:id"),
            {"id": original.units[0].id},
        )
    page = client.get("/inventory/balances?as_of=2026-03-01&location_ref=EXAMPLE-LOCATION")
    assert page.status_code == 200 and "Підтверджений залишок недоступний" in page.text
    assert "391500" not in page.text and "EXAMPLE-125" not in page.text
    detail = client.get(f"/inventory/assets/{original.units[0].id}?as_of=2026-01-16")
    historical = detail.text.split("<h2>Історична позиція</h2>")[1].split("<h2>Історія документів</h2>")[0]
    assert "Історична позиція неповна" in historical
    assert "EXAMPLE-LOCATION" not in historical
