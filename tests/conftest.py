"""Synthetic M5A fixtures shared by fake and PostgreSQL transaction tests."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from maas_ua.domain.custody import CustodyAssignment
from maas_ua.domain.receipts import (
    AccountingClassification,
    Money,
    Nomenclature,
    Quantity,
    SerialNumber,
    TrackingPolicy,
    register_serialized_receipt,
)


@pytest.fixture
def write_off_receipt():
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


@pytest.fixture
def write_off_command(write_off_receipt):
    from maas_ua.service_layer.write_offs import WriteOffAssets

    return WriteOffAssets(
        write_off_id=uuid4(),
        document_number="EXAMPLE-WRITE-OFF",
        document_date=date(2026, 2, 1),
        authorization_reference="EXAMPLE-AUTH",
        authorization_date=date(2026, 2, 3),
        effective_date=date(2026, 2, 10),
        asset_ids=tuple(u.id for u in write_off_receipt.units[:8]),
    )
