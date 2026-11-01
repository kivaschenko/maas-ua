"""Coordinate one atomic transfer across a document and selected asset aggregates."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from maas_ua.domain.custody import CustodyAssignment
from maas_ua.domain.movements import DuplicateMovement, InvalidTransfer, TransferredAssets
from maas_ua.domain.movements import transfer_assets as build_transfer
from maas_ua.ports import UnitOfWork


@dataclass(frozen=True, kw_only=True)
class TransferAssets:
    movement_id: UUID
    document_number: str
    document_date: date
    effective_date: date
    source: CustodyAssignment
    destination: CustodyAssignment
    asset_ids: tuple[UUID, ...]

    def __post_init__(self):
        object.__setattr__(self, "asset_ids", tuple(self.asset_ids))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def transfer_assets(
    command: TransferAssets, uow: UnitOfWork, *, clock: Callable[[], datetime] = _utc_now
) -> TransferredAssets:
    # Reject malformed/duplicate IDs before requesting locks or database queries.
    if (
        not isinstance(command.movement_id, UUID)
        or not command.asset_ids
        or any(not isinstance(key, UUID) for key in command.asset_ids)
        or len(set(command.asset_ids)) != len(command.asset_ids)
    ):
        raise InvalidTransfer("transfer requires distinct asset UUIDs and a movement UUID")
    with uow:
        if uow.movements.get(command.movement_id) is not None:
            raise DuplicateMovement("movement identity already processed")
        assets = uow.asset_units.get_for_update(command.asset_ids)
        # A duplicate command can have committed while this transaction waited
        # for its asset locks. Report the document conflict before source checks.
        if uow.movements.get(command.movement_id) is not None:
            raise DuplicateMovement("movement identity already processed")
        result = build_transfer(
            id=command.movement_id,
            document_number=command.document_number,
            document_date=command.document_date,
            effective_date=command.effective_date,
            created_at=clock(),
            source=command.source,
            destination=command.destination,
            asset_ids=command.asset_ids,
            assets=assets,
        )
        uow.movements.add(result.movement)
        for unit in result.units:
            uow.asset_units.save(unit)
        uow.commit()
    return result
