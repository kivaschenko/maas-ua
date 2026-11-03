"""Completed, document-backed accounting write-offs; no physical-use workflow."""

from dataclasses import dataclass, replace
from datetime import date, datetime
from uuid import UUID

from maas_ua.domain.custody import LifecycleState
from maas_ua.domain.receipts import AccountingClassification, AssetUnit, TrackingPolicy


class InvalidWriteOff(ValueError):
    """A document or selected unit violates the write-off invariants."""


class DuplicateWriteOff(InvalidWriteOff):
    """The caller-supplied document UUID has already been registered."""


class AlreadyWrittenOff(InvalidWriteOff):
    """A selected unit already has a completed write-off."""


@dataclass(frozen=True)
class WriteOffLine:
    asset_unit_id: UUID

    def __post_init__(self):
        if not isinstance(self.asset_unit_id, UUID):
            raise InvalidWriteOff("write-off units require internal UUID identities")


@dataclass(frozen=True)
class WriteOffDocument:
    id: UUID
    document_number: str
    document_date: date
    authorization_reference: str
    authorization_date: date
    effective_date: date
    created_at: datetime
    lines: tuple[WriteOffLine, ...]

    def __post_init__(self):
        if not isinstance(self.id, UUID):
            raise InvalidWriteOff("write-off identity must be a UUID")
        for value in (self.document_number, self.authorization_reference):
            if not isinstance(value, str) or not value or value != value.strip():
                raise InvalidWriteOff("document and authorization references require nonblank unpadded text")
        if any(type(value) is not date for value in (self.document_date, self.authorization_date, self.effective_date)):
            raise InvalidWriteOff("document, authorization and accounting acceptance dates must be explicit dates")
        if self.effective_date < self.authorization_date:
            raise InvalidWriteOff("accounting acceptance cannot precede authorization")
        if not isinstance(self.created_at, datetime) or self.created_at.utcoffset() is None:
            raise InvalidWriteOff("registration timestamp must be timezone-aware")
        lines = tuple(self.lines)
        if not lines or any(not isinstance(line, WriteOffLine) for line in lines):
            raise InvalidWriteOff("write-off requires valid nonempty lines")
        if len({line.asset_unit_id for line in lines}) != len(lines):
            raise InvalidWriteOff("duplicate asset identity")
        object.__setattr__(self, "lines", lines)


@dataclass(frozen=True)
class WriteOffAsset:
    """Origin and chronology facts read under the selected unit's transaction lock."""

    unit: AssetUnit
    accounting_classification: AccountingClassification | None
    tracking_policy: TrackingPolicy | None
    receipt_effective_date: date | None
    latest_transfer_effective_date: date | None
    previous_write_off_id: UUID | None


@dataclass(frozen=True)
class WrittenOffAssets:
    document: WriteOffDocument
    units: tuple[AssetUnit, ...]


def write_off_assets(
    *,
    id: UUID,
    document_number: str,
    document_date: date,
    authorization_reference: str,
    authorization_date: date,
    effective_date: date,
    created_at: datetime,
    asset_ids: tuple[UUID, ...],
    assets: tuple[WriteOffAsset, ...],
) -> WrittenOffAssets:
    document = WriteOffDocument(
        id,
        document_number,
        document_date,
        authorization_reference,
        authorization_date,
        effective_date,
        created_at,
        tuple(WriteOffLine(key) for key in asset_ids),
    )
    by_id = {facts.unit.id: facts for facts in assets}
    if len(by_id) != len(assets) or set(by_id) != set(asset_ids):
        raise InvalidWriteOff("all requested assets and their origin facts must exist exactly once")
    selected = tuple(by_id[key] for key in asset_ids)
    for facts in selected:
        unit = facts.unit
        if facts.previous_write_off_id is not None or unit.lifecycle_state is LifecycleState.WRITTEN_OFF:
            raise AlreadyWrittenOff("unit already written off")
        if unit.lifecycle_state is not LifecycleState.ACTIVE:
            raise InvalidWriteOff("only explicitly active units can be written off")
        if (
            facts.accounting_classification is not AccountingClassification.INVENTORY
            or facts.tracking_policy is not TrackingPolicy.SERIALIZED
        ):
            raise InvalidWriteOff("write-off requires a serialized inventory receipt classification")
        receipt_date = facts.receipt_effective_date
        transfer_date = facts.latest_transfer_effective_date
        if type(receipt_date) is not date or unit.last_effective_date is None:
            raise InvalidWriteOff("known receipt and latest effective dates are required")
        if transfer_date is not None and (type(transfer_date) is not date or transfer_date < receipt_date):
            raise InvalidWriteOff("transfer chronology contradicts receipt")
        latest = max(receipt_date, transfer_date) if transfer_date is not None else receipt_date
        if unit.last_effective_date != latest:
            raise InvalidWriteOff("current effective date contradicts recorded history")
        if effective_date < latest:
            raise InvalidWriteOff("accounting acceptance cannot precede receipt or latest transfer")
    return WrittenOffAssets(
        document,
        tuple(
            replace(facts.unit, lifecycle_state=LifecycleState.WRITTEN_OFF, last_effective_date=effective_date)
            for facts in selected
        ),
    )
