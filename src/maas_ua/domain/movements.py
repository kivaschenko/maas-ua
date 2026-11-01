"""Immutable documented transfers and complete-command validation."""

from dataclasses import dataclass, replace
from datetime import date, datetime
from uuid import UUID

from maas_ua.domain.custody import CustodyAssignment, LifecycleState
from maas_ua.domain.receipts import AssetUnit


class InvalidTransfer(ValueError):
    """A transfer violates a confirmed movement invariant."""


class DuplicateMovement(InvalidTransfer):
    """The movement UUID has already been processed."""


@dataclass(frozen=True)
class MovementLine:
    asset_unit_id: UUID

    def __post_init__(self):
        if not isinstance(self.asset_unit_id, UUID):
            raise InvalidTransfer("movement units require internal UUID identities")


@dataclass(frozen=True)
class MovementDocument:
    id: UUID
    document_number: str
    document_date: date
    effective_date: date
    created_at: datetime
    source: CustodyAssignment
    destination: CustodyAssignment
    lines: tuple[MovementLine, ...]

    def __post_init__(self):
        if not isinstance(self.id, UUID):
            raise InvalidTransfer("movement identity must be a UUID")
        if (
            not isinstance(self.document_number, str)
            or not self.document_number
            or self.document_number != self.document_number.strip()
        ):
            raise InvalidTransfer("movement requires a nonblank document number without outer whitespace")
        if type(self.document_date) is not date or type(self.effective_date) is not date:
            raise InvalidTransfer("movement document and effective dates must be dates")
        if not isinstance(self.created_at, datetime) or self.created_at.utcoffset() is None:
            raise InvalidTransfer("movement creation timestamp must be timezone-aware")
        if not isinstance(self.source, CustodyAssignment) or not isinstance(self.destination, CustodyAssignment):
            raise InvalidTransfer("movement requires source and destination custody")
        if self.source == self.destination:
            raise InvalidTransfer("source and destination must differ")
        lines = tuple(self.lines)
        if not lines or any(not isinstance(line, MovementLine) for line in lines):
            raise InvalidTransfer("movement requires valid lines")
        if len({line.asset_unit_id for line in lines}) != len(lines):
            raise InvalidTransfer("duplicate asset identity")
        object.__setattr__(self, "lines", lines)


@dataclass(frozen=True)
class TransferredAssets:
    movement: MovementDocument
    units: tuple[AssetUnit, ...]


def transfer_assets(
    *,
    id: UUID,
    document_number: str,
    document_date: date,
    effective_date: date,
    created_at: datetime,
    source: CustodyAssignment,
    destination: CustodyAssignment,
    asset_ids: tuple[UUID, ...],
    assets: tuple[AssetUnit, ...],
) -> TransferredAssets:
    movement = MovementDocument(
        id,
        document_number,
        document_date,
        effective_date,
        created_at,
        source,
        destination,
        tuple(MovementLine(key) for key in asset_ids),
    )
    by_id = {unit.id: unit for unit in assets}
    if len(by_id) != len(assets) or set(by_id) != set(asset_ids):
        raise InvalidTransfer("all requested assets must exist exactly once")
    ordered = tuple(by_id[key] for key in asset_ids)
    for unit in ordered:
        if unit.lifecycle_state is not LifecycleState.ACTIVE:
            raise InvalidTransfer("only explicitly active units are eligible")
        if unit.custody != source:
            raise InvalidTransfer("source must match current assigned custody for every unit")
        if unit.last_effective_date is None or effective_date < unit.last_effective_date:
            raise InvalidTransfer("backdated transfers or units without an effective date are unsupported")
    return TransferredAssets(
        movement, tuple(replace(unit, custody=destination, last_effective_date=effective_date) for unit in ordered)
    )
