from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from uuid import UUID

import pytest

from maas_ua.domain.custody import CustodyAssignment
from maas_ua.domain.receipts import (
    InvalidReceipt,
    Money,
    Nomenclature,
    Quantity,
    SerialNumber,
    TrackingPolicy,
    register_serialized_receipt,
)


def receipt_input(**changes):
    values = {
        "document_number": "EXAMPLE-125",
        "document_date": date(2026, 1, 15),
        "effective_date": date(2026, 1, 16),
        "created_at": datetime(2026, 1, 17, tzinfo=UTC),
        "source": "EXAMPLE-SOURCE",
        "nomenclature": Nomenclature(
            "EXAMPLE-FPV", TrackingPolicy.SERIALIZED, code="EXAMPLE-FPV", name="Example drone", unit_of_measure="pcs"
        ),
        "quantity": Quantity(27, "pcs"),
        "unit_price": Money(Decimal("14500.00"), "UAH"),
        "line_total": Money(Decimal("391500.00"), "UAH"),
        "serial_numbers": tuple(SerialNumber("EXAMPLE-ISSUER", f"EXAMPLE-FPV-{i:04}") for i in range(1, 28)),
        "analytics": (("example-dimension", "EXAMPLE-CODE"),),
    }
    return values | changes


def test_initial_custody_is_recorded_separately_and_unknown_stays_unknown():
    assignment = CustodyAssignment("EXAMPLE-A", "EXAMPLE-A")
    assigned = register_serialized_receipt(**receipt_input(initial_custody=assignment))
    assert all(unit.initial_custody == assignment and unit.custody == assignment for unit in assigned.units)
    assert all(unit.initial_custody is None for unit in register_serialized_receipt(**receipt_input()).units)


def test_registers_homogeneous_receipt_with_independent_unit_identities():
    ids = iter(UUID(int=i) for i in range(1, 30))
    result = register_serialized_receipt(**receipt_input(), new_id=lambda: next(ids))
    receipt, units = result.receipt, result.units
    (line,) = receipt.lines

    assert receipt.id == UUID(int=1)
    assert line.id == UUID(int=2)
    assert len(units) == 27
    assert len({unit.id for unit in units}) == 27
    assert {unit.id for unit in units}.isdisjoint({receipt.id, line.id})
    assert {unit.receipt_line_id for unit in units} == {line.id}
    assert tuple(unit.serial_number for unit in units) == receipt_input()["serial_numbers"]
    assert line.quantity == Quantity(27, "pcs")
    assert line.unit_price == Money(Decimal("14500.00"), "UAH")
    assert line.total == Money(Decimal("391500.00"), "UAH")
    assert line.nomenclature.id == "EXAMPLE-FPV"
    assert line.analytics == (("example-dimension", "EXAMPLE-CODE"),)
    assert receipt.source == "EXAMPLE-SOURCE"
    assert receipt.document_date == date(2026, 1, 15)
    assert receipt.effective_date == date(2026, 1, 16)
    assert receipt.created_at == datetime(2026, 1, 17, tzinfo=UTC)


@pytest.mark.parametrize("count", [0, -1, 1.5, True, Decimal(2)])
def test_quantity_requires_positive_integer(count):
    with pytest.raises(InvalidReceipt):
        Quantity(count, "pcs")


@pytest.mark.parametrize("count", [26, 28])
def test_requires_one_serial_per_unit(count):
    serials = tuple(SerialNumber("EXAMPLE", f"EXAMPLE-{i}") for i in range(count))
    with pytest.raises(InvalidReceipt, match="serial count"):
        register_serialized_receipt(**receipt_input(serial_numbers=serials))


def test_rejects_duplicate_serial_in_namespace():
    serials = receipt_input()["serial_numbers"]
    with pytest.raises(InvalidReceipt, match="duplicate serial"):
        register_serialized_receipt(**receipt_input(serial_numbers=serials[:-1] + (serials[0],)))


def test_same_serial_text_in_different_explicit_namespaces_is_distinct():
    serials = receipt_input()["serial_numbers"]
    result = register_serialized_receipt(
        **receipt_input(serial_numbers=serials[:-1] + (SerialNumber("OTHER-EXAMPLE", serials[0].value),))
    )
    assert len({unit.serial_number for unit in result.units}) == 27


def test_serial_comparison_is_exact_and_case_sensitive():
    # This is a sanity check that the SerialNumber class does not silently normalize or ignore case.
    assert SerialNumber("EXAMPLE", "abc") == SerialNumber("EXAMPLE", "abc")
    assert SerialNumber("EXAMPLE", "abc") != SerialNumber("EXAMPLE", "ABC")


@pytest.mark.parametrize("value", ["", " ", " EXAMPLE", "EXAMPLE "])
def test_rejects_blank_or_padded_serial_without_silent_normalization(value):
    with pytest.raises(InvalidReceipt):
        SerialNumber("EXAMPLE", value)
    with pytest.raises(InvalidReceipt):
        SerialNumber(value, "EXAMPLE")


@pytest.mark.parametrize("amount", [0.1, 1, Decimal("NaN"), Decimal("Infinity"), Decimal(-1)])
def test_money_requires_finite_nonnegative_decimal(amount):
    with pytest.raises(InvalidReceipt):
        Money(amount, "UAH")


def test_money_multiplication_is_exact_even_with_low_ambient_precision():
    with localcontext() as context:
        context.prec = 3
        result = register_serialized_receipt(**receipt_input())
    assert result.receipt.lines[0].total.amount == Decimal("391500.00")


def test_fractional_prices_are_not_silently_rounded():
    result = register_serialized_receipt(
        **receipt_input(
            unit_price=Money(Decimal("0.12345"), "UAH"),
            line_total=Money(Decimal("3.33315"), "UAH"),
        )
    )
    assert result.receipt.lines[0].total.amount == Decimal("3.33315")


@pytest.mark.parametrize("total", [Money(Decimal("391499.99"), "UAH"), Money(Decimal(391500), "USD")])
def test_rejects_incorrect_total_or_currency(total):
    with pytest.raises(InvalidReceipt, match="total"):
        register_serialized_receipt(**receipt_input(line_total=total))


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": Quantity(27, "kg")},
        {
            "nomenclature": Nomenclature(
                "EXAMPLE-CABLE",
                TrackingPolicy.QUANTITY,
                code="EXAMPLE-CABLE",
                name="Example cable",
                unit_of_measure="pcs",
            )
        },
        {"source": ""},
        {"document_number": ""},
        {"document_date": "2026-01-15"},
        {"effective_date": datetime(2026, 1, 16)},
        {"created_at": datetime(2026, 1, 17)},
        {"analytics": (("example", ""),)},
        {"analytics": (("example", "A"), ("example", "B"))},
        {"analytics": None},
        {"serial_numbers": None},
        {"serial_numbers": ("EXAMPLE",) * 27},
    ],
)
def test_rejects_invalid_receipt_metadata_or_tracking(changes):
    with pytest.raises(InvalidReceipt):
        register_serialized_receipt(**receipt_input(**changes))


def test_history_is_immutable_and_input_collections_are_snapshotted():
    serials = list(receipt_input()["serial_numbers"])
    analytics = [["example", "EXAMPLE-CODE"]]
    result = register_serialized_receipt(**receipt_input(serial_numbers=serials, analytics=analytics))
    serials.clear()
    analytics[0][1] = "CHANGED"
    assert len(result.units) == 27
    assert result.receipt.lines[0].analytics == (("example", "EXAMPLE-CODE"),)
    for obj, field, value in [
        (result.receipt, "source", "CHANGED"),
        (result.receipt.lines[0], "unit_price", Money(Decimal(1), "UAH")),
        (result.units[0], "receipt_line_id", UUID(int=100)),
        (result.units[0], "id", UUID(int=101)),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, value)


def test_line_itself_enforces_serialized_quantity_invariants():
    line = register_serialized_receipt(**receipt_input()).receipt.lines[0]
    with pytest.raises(InvalidReceipt):
        replace(line, quantity=Quantity(1, "kg"))


@pytest.mark.parametrize("unit", ["pcs", "PCS", "EXAMPLE-SET", "шт"])
def test_serialized_receipt_uses_exact_registered_unit_without_changing_original(unit):
    original = register_serialized_receipt(**receipt_input())
    catalog = replace(receipt_input()["nomenclature"], unit_of_measure=unit)
    result = register_serialized_receipt(**receipt_input(nomenclature=catalog, quantity=Quantity(27, unit)))
    assert result.receipt.lines[0].quantity == Quantity(27, unit)
    assert result.receipt.lines[0].total == original.receipt.lines[0].total
    assert original.receipt.lines[0].quantity == Quantity(27, "pcs")
    assert original.receipt.lines[0].unit_price == Money(Decimal("14500.00"), "UAH")
    if unit != "pcs":
        with pytest.raises(InvalidReceipt):
            register_serialized_receipt(**receipt_input(nomenclature=catalog))


def test_identity_factory_cannot_reuse_an_identity():
    with pytest.raises(InvalidReceipt, match="identities must be distinct"):
        register_serialized_receipt(**receipt_input(), new_id=lambda: UUID(int=1))


def test_receipt_classification_is_an_explicit_immutable_snapshot():
    from maas_ua.domain.receipts import AccountingClassification

    result = register_serialized_receipt(**receipt_input(accounting_classification=AccountingClassification.INVENTORY))
    line = result.receipt.lines[0]
    assert line.accounting_classification is AccountingClassification.INVENTORY
    assert register_serialized_receipt(**receipt_input()).receipt.lines[0].accounting_classification is None
    with pytest.raises(FrozenInstanceError):
        line.accounting_classification = AccountingClassification.FIXED_ASSET
    with pytest.raises(InvalidReceipt, match="classification"):
        register_serialized_receipt(**receipt_input(accounting_classification="INVENTORY"))
