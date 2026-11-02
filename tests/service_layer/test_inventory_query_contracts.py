from dataclasses import FrozenInstanceError
from datetime import date, datetime
from uuid import uuid4

import pytest

from maas_ua.service_layer.inventory import (
    GetAssetBySerialNumber,
    GetAssetHistory,
    GetAssetPositionAsOf,
    GetInventoryBalance,
    GetInventoryValuation,
    InvalidInventoryQuery,
    InventoryFilter,
    get_asset_by_serial_number,
    get_asset_history,
    get_asset_position_as_of,
    get_inventory_balance,
    get_inventory_valuation,
)


@pytest.mark.parametrize("value", [None, "2026-01-01", datetime(2026, 1, 1)])
def test_as_of_requires_a_business_date(value):
    with pytest.raises(InvalidInventoryQuery):
        GetInventoryBalance(as_of=value)


def test_serial_key_and_grouping_are_explicit_and_inputs_are_immutable():
    with pytest.raises(InvalidInventoryQuery):
        GetAssetBySerialNumber(namespace="", value="EXAMPLE")
    with pytest.raises(InvalidInventoryQuery):
        GetInventoryBalance(as_of=date(2026, 1, 1), group_by=("invented",))
    with pytest.raises(InvalidInventoryQuery):
        GetAssetHistory(asset_id="bad")
    filters = InventoryFilter(analytics=(("fund", "EXAMPLE"),))
    with pytest.raises(FrozenInstanceError):
        filters.source = "other"


@pytest.mark.parametrize(
    "command,handler,method",
    [
        (GetAssetBySerialNumber(namespace="EXAMPLE", value="1"), get_asset_by_serial_number, "asset_by_serial"),
        (GetAssetHistory(asset_id=uuid4()), get_asset_history, "asset_history"),
        (GetAssetPositionAsOf(asset_id=uuid4(), as_of=date(2026, 1, 1)), get_asset_position_as_of, "asset_position"),
        (GetInventoryBalance(as_of=date(2026, 1, 1)), get_inventory_balance, "inventory_balance"),
        (GetInventoryValuation(as_of=date(2026, 1, 1)), get_inventory_valuation, "inventory_valuation"),
    ],
)
def test_handlers_use_the_read_port_without_a_command_uow(command, handler, method):
    calls = []
    result = object()

    class Reader:
        pass

    reader = Reader()

    def read(query):
        calls.append(query)
        return result

    setattr(reader, method, read)
    assert handler(command, reader) is result
    assert calls == [command]
