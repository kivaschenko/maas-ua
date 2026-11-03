"""Register one complete write-off and its lifecycle changes atomically."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from maas_ua.domain.write_offs import DuplicateWriteOff, InvalidWriteOff, WrittenOffAssets
from maas_ua.domain.write_offs import write_off_assets as build_write_off
from maas_ua.ports import UnitOfWork


@dataclass(frozen=True, kw_only=True)
class WriteOffAssets:
    write_off_id: UUID
    document_number: str
    document_date: date
    authorization_reference: str
    authorization_date: date
    effective_date: date
    asset_ids: tuple[UUID, ...]

    def __post_init__(self):
        object.__setattr__(self, "asset_ids", tuple(self.asset_ids))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def write_off_assets(
    command: WriteOffAssets, uow: UnitOfWork, *, clock: Callable[[], datetime] = _utc_now
) -> WrittenOffAssets:
    if (
        not isinstance(command.write_off_id, UUID)
        or not command.asset_ids
        or any(not isinstance(key, UUID) for key in command.asset_ids)
        or len(set(command.asset_ids)) != len(command.asset_ids)
    ):
        raise InvalidWriteOff("write-off requires distinct asset UUIDs and a document UUID")
    with uow:
        if uow.write_offs.get(command.write_off_id) is not None:
            raise DuplicateWriteOff("write-off identity already processed")
        units = uow.asset_units.get_for_update(command.asset_ids)
        if uow.write_offs.get(command.write_off_id) is not None:
            raise DuplicateWriteOff("write-off identity already processed")
        facts = uow.asset_units.write_off_facts(units)
        result = build_write_off(
            id=command.write_off_id,
            document_number=command.document_number,
            document_date=command.document_date,
            authorization_reference=command.authorization_reference,
            authorization_date=command.authorization_date,
            effective_date=command.effective_date,
            created_at=clock(),
            asset_ids=command.asset_ids,
            assets=facts,
        )
        uow.write_offs.add(result.document)
        for unit in result.units:
            uow.asset_units.save(unit)
        uow.commit()
    return result
