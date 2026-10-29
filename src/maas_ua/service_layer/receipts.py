"""Register a complete receipt without HTTP or persistence dependencies."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from maas_ua.domain.receipts import (
    DuplicateSerialNumber,
    Money,
    Nomenclature,
    Quantity,
    RegisteredReceipt,
    SerialNumber,
    register_serialized_receipt,
)
from maas_ua.ports import UnitOfWork


@dataclass(frozen=True, kw_only=True)
class RegisterInventoryReceipt:
    """Input for an already confirmed document, not a financial posting command."""

    document_number: str
    document_date: date
    effective_date: date
    source: str
    nomenclature: Nomenclature
    quantity: Quantity
    unit_price: Money
    line_total: Money
    serial_numbers: tuple[SerialNumber, ...]
    analytics: tuple[tuple[str, str], ...] = ()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def register_inventory_receipt(
    command: RegisterInventoryReceipt,
    uow: UnitOfWork,
    *,
    clock: Callable[[], datetime] = _utc_now,
    new_id: Callable[[], UUID] = uuid4,
) -> RegisteredReceipt:
    """Validate everything, stage all aggregates, then explicitly commit once."""
    result = register_serialized_receipt(
        document_number=command.document_number,
        document_date=command.document_date,
        effective_date=command.effective_date,
        created_at=clock(),
        source=command.source,
        nomenclature=command.nomenclature,
        quantity=command.quantity,
        unit_price=command.unit_price,
        line_total=command.line_total,
        serial_numbers=command.serial_numbers,
        analytics=command.analytics,
        new_id=new_id,
    )
    with uow:
        for unit in result.units:
            if uow.asset_units.contains_serial(unit.serial_number):
                # Avoid including identifiers in error messages or logs.
                raise DuplicateSerialNumber(
                    "serial identifier already registered in namespace"
                )
        uow.receipts.add(result.receipt)
        for unit in result.units:
            uow.asset_units.add(unit)
        uow.commit()
    return result
