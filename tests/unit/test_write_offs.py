from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from maas_ua.domain.custody import CustodyAssignment, LifecycleState
from maas_ua.domain.movements import transfer_assets
from maas_ua.domain.receipts import (
    AccountingClassification,
    Money,
    Nomenclature,
    Quantity,
    SerialNumber,
    TrackingPolicy,
    register_serialized_receipt,
)
from maas_ua.domain.write_offs import InvalidWriteOff, WriteOffAsset, write_off_assets


def receipt():
    return register_serialized_receipt(
        document_number="EXAMPLE-RECEIPT",
        document_date=date(2026, 1, 1),
        effective_date=date(2026, 1, 2),
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
        source="EXAMPLE",
        nomenclature=Nomenclature(
            "EXAMPLE", TrackingPolicy.SERIALIZED, code="EXAMPLE", name="Example", unit_of_measure="pcs"
        ),
        quantity=Quantity(27, "pcs"),
        unit_price=Money(Decimal("14500.00"), "UAH"),
        line_total=Money(Decimal("391500.00"), "UAH"),
        serial_numbers=tuple(SerialNumber("EXAMPLE", str(i)) for i in range(27)),
        initial_custody=CustodyAssignment("EXAMPLE-A", "EXAMPLE-A"),
        accounting_classification=AccountingClassification.INVENTORY,
    )


def inputs(count=8):
    original = receipt()
    return dict(
        id=uuid4(),
        document_number="EXAMPLE-WRITE-OFF",
        document_date=date(2026, 2, 1),
        authorization_reference="EXAMPLE-AUTH",
        authorization_date=date(2026, 2, 3),
        effective_date=date(2026, 2, 10),
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
        asset_ids=tuple(u.id for u in original.units[:count]),
        assets=tuple(
            WriteOffAsset(
                u,
                AccountingClassification.INVENTORY,
                TrackingPolicy.SERIALIZED,
                original.receipt.effective_date,
                None,
                None,
            )
            for u in original.units[:count]
        ),
    )


@pytest.mark.parametrize("count", [1, 8])
def test_write_off_changes_only_lifecycle_and_effective_date(count):
    values = inputs(count)
    result = write_off_assets(**values)
    assert len(result.document.lines) == count
    assert tuple(line.asset_unit_id for line in result.document.lines) == values["asset_ids"]
    for before, after in zip(values["assets"], result.units):
        assert after == replace(
            before.unit, lifecycle_state=LifecycleState.WRITTEN_OFF, last_effective_date=values["effective_date"]
        )
        assert before.unit.lifecycle_state is LifecycleState.ACTIVE
    assert result.document.document_date == date(2026, 2, 1)
    assert result.document.authorization_date == date(2026, 2, 3)
    assert result.document.effective_date == date(2026, 2, 10)
    assert result.document.created_at == datetime(2026, 3, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "failure",
    [
        "duplicate",
        "missing",
        "written_off",
        "prior_record",
        "unknown_lifecycle",
        "unknown_classification",
        "fixed_asset",
        "quantity",
        "authorization",
        "receipt",
        "transfer",
        "unknown_date",
        "stale_date",
    ],
)
def test_invalid_selection_or_chronology_rejects_entire_write_off(failure):
    values = inputs()
    facts = values["assets"][-1]
    if failure == "duplicate":
        values["asset_ids"] += (values["asset_ids"][0],)
    elif failure == "missing":
        values["assets"] = values["assets"][:-1]
    elif failure == "written_off":
        facts = replace(facts, unit=replace(facts.unit, lifecycle_state=LifecycleState.WRITTEN_OFF))
    elif failure == "prior_record":
        facts = replace(facts, previous_write_off_id=uuid4())
    elif failure == "unknown_lifecycle":
        facts = replace(facts, unit=replace(facts.unit, lifecycle_state=None))
    elif failure == "unknown_classification":
        facts = replace(facts, accounting_classification=None)
    elif failure == "fixed_asset":
        facts = replace(facts, accounting_classification=AccountingClassification.FIXED_ASSET)
    elif failure == "quantity":
        facts = replace(facts, tracking_policy=TrackingPolicy.QUANTITY)
    elif failure == "authorization":
        values["effective_date"] = date(2026, 2, 2)
    elif failure == "receipt":
        facts = replace(facts, receipt_effective_date=date(2026, 2, 11))
    elif failure == "transfer":
        facts = replace(
            facts,
            latest_transfer_effective_date=date(2026, 2, 11),
            unit=replace(facts.unit, last_effective_date=date(2026, 2, 11)),
        )
    elif failure == "unknown_date":
        facts = replace(facts, unit=replace(facts.unit, last_effective_date=None))
    else:
        facts = replace(facts, latest_transfer_effective_date=date(2026, 2, 5))
    if failure not in {"duplicate", "missing"}:
        values["assets"] = (*values["assets"][:-1], facts)
    with pytest.raises(InvalidWriteOff):
        write_off_assets(**values)


def test_same_day_transfer_then_write_off_preserves_movements_and_custody():
    values = inputs(1)
    facts = values["assets"][0]
    moved = transfer_assets(
        id=uuid4(),
        document_number="EXAMPLE-MOVE",
        document_date=values["effective_date"],
        effective_date=values["effective_date"],
        created_at=values["created_at"],
        source=facts.unit.custody,
        destination=CustodyAssignment("EXAMPLE-B", "EXAMPLE-B"),
        asset_ids=values["asset_ids"],
        assets=(facts.unit,),
    )
    values["assets"] = (replace(facts, unit=moved.units[0], latest_transfer_effective_date=values["effective_date"]),)
    result = write_off_assets(**values)
    assert result.units[0].custody == moved.movement.destination
    assert moved.units[0].lifecycle_state is LifecycleState.ACTIVE
    assert len(moved.movement.lines) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "bad"},
        {"asset_ids": ()},
        {"asset_ids": ("bad",)},
        {"document_number": " "},
        {"authorization_reference": ""},
        {"authorization_date": None},
        {"document_date": datetime(2026, 2, 1)},
        {"effective_date": None},
        {"created_at": datetime(2026, 3, 1)},
    ],
)
def test_write_off_requires_explicit_valid_document_metadata(changes):
    with pytest.raises(InvalidWriteOff):
        write_off_assets(**(inputs(1) | changes))
