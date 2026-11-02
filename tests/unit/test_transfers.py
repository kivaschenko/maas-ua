from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from maas_ua.domain.custody import CustodyAssignment, LifecycleState
from maas_ua.domain.movements import InvalidTransfer, transfer_assets
from maas_ua.domain.receipts import AssetUnit, SerialNumber

SOURCE = CustodyAssignment("EXAMPLE-LOCATION-A", "EXAMPLE-PARTY-A")
DESTINATION = CustodyAssignment("EXAMPLE-LOCATION-B", "EXAMPLE-PARTY-B")
DAY = date(2026, 2, 1)


def units():
    line_id = UUID(int=100)
    return tuple(
        AssetUnit(
            UUID(int=i), SerialNumber("EXAMPLE", str(i)), line_id, custody=SOURCE, last_effective_date=date(2026, 1, 1)
        )
        for i in range(1, 28)
    )


def inputs(selected=None):
    selected = units()[:1] if selected is None else selected
    return dict(
        id=uuid4(),
        document_number="EXAMPLE-MOVE",
        document_date=DAY,
        effective_date=DAY,
        created_at=datetime(2026, 2, 2, tzinfo=UTC),
        asset_ids=tuple(unit.id for unit in selected),
        assets=selected,
        source=SOURCE,
        destination=DESTINATION,
    )


@pytest.mark.parametrize("count", [1, 12])
def test_transfer_creates_history_and_new_snapshots_without_touching_origin(count):
    original = units()
    result = transfer_assets(**inputs(original[:count]))
    assert len(result.movement.lines) == count
    assert tuple(line.asset_unit_id for line in result.movement.lines) == tuple(u.id for u in original[:count])
    assert result.movement.source == SOURCE
    assert result.movement.destination == DESTINATION
    for before, after in zip(original, result.units):
        assert after == replace(before, custody=DESTINATION, last_effective_date=DAY)
        assert before.custody == SOURCE
    assert all(u.custody == SOURCE for u in original[count:])


@pytest.mark.parametrize(
    "change", ["duplicates", "missing", "source", "equal", "unassigned", "inactive", "backdated", "undated"]
)
def test_complete_validation_precedes_changes(change):
    values = inputs(units()[:12])
    if change == "duplicates":
        values["asset_ids"] += (values["asset_ids"][0],)
    elif change == "missing":
        values["assets"] = values["assets"][:-1]
    elif change == "source":
        values["assets"] = (*values["assets"][:-1], replace(values["assets"][-1], custody=DESTINATION))
    elif change == "equal":
        values["destination"] = SOURCE
    elif change == "unassigned":
        values["assets"] = (replace(values["assets"][0], custody=None), *values["assets"][1:])
    elif change == "inactive":
        values["assets"] = (replace(values["assets"][0], lifecycle_state=None), *values["assets"][1:])
    elif change == "backdated":
        values["effective_date"] = date(2025, 12, 31)
    elif change == "undated":
        values["assets"] = (replace(values["assets"][0], last_effective_date=None), *values["assets"][1:])
    with pytest.raises(InvalidTransfer):
        transfer_assets(**values)
    assert all(u.custody == SOURCE for u in units())


def test_same_day_followup_is_allowed_but_earlier_posting_is_rejected():
    first = transfer_assets(**inputs())
    values = inputs(first.units) | {"source": DESTINATION, "destination": SOURCE}
    assert transfer_assets(**values).units[0].custody == SOURCE
    with pytest.raises(InvalidTransfer):
        transfer_assets(**(values | {"effective_date": date(2026, 1, 31)}))


@pytest.mark.parametrize(
    "changes",
    [
        {"asset_ids": (), "assets": ()},
        {"id": "bad"},
        {"document_number": ""},
        {"document_date": "2026-02-01"},
        {"effective_date": datetime(2026, 2, 1)},
        {"created_at": datetime(2026, 2, 1)},
        {"source": None},
        {"destination": None},
        {"asset_ids": ("bad",)},
    ],
)
def test_transfer_metadata(changes):
    with pytest.raises(InvalidTransfer):
        transfer_assets(**(inputs() | changes))


@pytest.mark.parametrize("field", ["location_ref", "responsible_party_ref"])
@pytest.mark.parametrize("value", ["", " ", " padded", None])
def test_custody_requires_explicit_references(field, value):
    with pytest.raises(ValueError):
        replace(SOURCE, **{field: value})


def test_active_is_an_explicit_lifecycle_state():
    assert units()[0].lifecycle_state is LifecycleState.ACTIVE


@pytest.mark.parametrize(
    "destination",
    [
        CustodyAssignment(SOURCE.location_ref, "EXAMPLE-OTHER-PARTY"),
        CustodyAssignment("EXAMPLE-OTHER-LOCATION", SOURCE.responsible_party_ref),
    ],
)
def test_changing_either_custody_dimension_is_a_transfer(destination):
    result = transfer_assets(**(inputs() | {"destination": destination}))
    assert result.units[0].custody == destination
