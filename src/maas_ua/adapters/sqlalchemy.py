"""Aggregate repositories and explicit transaction ownership for PostgreSQL."""

from collections.abc import Callable
from types import TracebackType
from typing import NoReturn, Self
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, selectinload

from maas_ua.adapters.orm import (
    AssetUnitRecord,
    MovementDocumentRecord,
    MovementLineRecord,
    NomenclatureRecord,
    ReceiptDocumentRecord,
    ReceiptLineRecord,
    WriteOffDocumentRecord,
    WriteOffLineRecord,
    movement_from_record,
    movement_to_record,
    nomenclature_from_record,
    nomenclature_to_record,
    receipt_from_record,
    receipt_to_record,
    unit_from_record,
    unit_to_record,
    write_off_from_record,
    write_off_to_record,
)
from maas_ua.domain.movements import DuplicateMovement, MovementDocument
from maas_ua.domain.receipts import (
    AccountingClassification,
    AssetUnit,
    DuplicateSerialNumber,
    Nomenclature,
    ReceiptDocument,
    SerialNumber,
    TrackingPolicy,
)
from maas_ua.domain.write_offs import AlreadyWrittenOff, DuplicateWriteOff, WriteOffAsset, WriteOffDocument
from maas_ua.ports import DuplicateNomenclatureCode, RepositoryConflict


def _translate_concurrency(error: OperationalError) -> NoReturn:
    if getattr(error.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
        raise RepositoryConflict("concurrent asset transaction; retry the complete command") from None
    raise error


class SqlAlchemyNomenclatureRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, item: Nomenclature) -> None:
        self.session.add(nomenclature_to_record(item))

    def get(self, nomenclature_id: str) -> Nomenclature | None:
        for record in self.session.new:
            if isinstance(record, NomenclatureRecord) and record.id == nomenclature_id:
                return nomenclature_from_record(record)
        with self.session.no_autoflush:
            record = self.session.get(NomenclatureRecord, nomenclature_id)
            return nomenclature_from_record(record) if record is not None else None

    def get_by_code(self, code: str) -> Nomenclature | None:
        for record in self.session.new:
            if isinstance(record, NomenclatureRecord) and record.code == code:
                return nomenclature_from_record(record)
        with self.session.no_autoflush:
            record = self.session.scalar(select(NomenclatureRecord).where(NomenclatureRecord.code == code))
            return nomenclature_from_record(record) if record is not None else None


class SqlAlchemyReceiptRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, receipt: ReceiptDocument) -> None:
        record = receipt_to_record(receipt)
        for line in record.lines:
            pending = next(
                (
                    item
                    for item in self.session.new
                    if isinstance(item, NomenclatureRecord) and item.id == line.nomenclature_id
                ),
                None,
            )
            with self.session.no_autoflush:
                nomenclature = pending or self.session.get(NomenclatureRecord, line.nomenclature_id)
            if nomenclature is None:
                raise RepositoryConflict("receipt nomenclature must already exist")
            line.nomenclature = nomenclature
        self.session.add(record)

    def get(self, receipt_id: UUID) -> ReceiptDocument | None:
        for record in self.session.new:
            if isinstance(record, ReceiptDocumentRecord) and record.id == receipt_id:
                return receipt_from_record(record)
        statement = (
            select(ReceiptDocumentRecord)
            .where(ReceiptDocumentRecord.id == receipt_id)
            .options(selectinload(ReceiptDocumentRecord.lines))
        )
        with self.session.no_autoflush:
            record = self.session.scalar(statement)
            return receipt_from_record(record) if record is not None else None


class SqlAlchemyAssetUnitRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, unit: AssetUnit) -> None:
        self.session.add(unit_to_record(unit))

    def get(self, unit_id: UUID) -> AssetUnit | None:
        for record in self.session.new:
            if isinstance(record, AssetUnitRecord) and record.id == unit_id:
                return unit_from_record(record)
        with self.session.no_autoflush:
            record = self.session.get(AssetUnitRecord, unit_id)
            return unit_from_record(record) if record is not None else None

    def get_for_update(self, unit_ids: tuple[UUID, ...]) -> tuple[AssetUnit, ...]:
        # Stable lock ordering prevents overlapping multi-unit transfers deadlocking.
        # Refresh any prior reads after waiting on a competing transaction.
        statement = (
            select(AssetUnitRecord)
            .where(AssetUnitRecord.id.in_(unit_ids))
            .order_by(AssetUnitRecord.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        try:
            with self.session.no_autoflush:
                return tuple(unit_from_record(record) for record in self.session.scalars(statement))
        except OperationalError as error:
            _translate_concurrency(error)

    def write_off_facts(self, units: tuple[AssetUnit, ...]) -> tuple[WriteOffAsset, ...]:
        ids = tuple(unit.id for unit in units)
        latest = (
            select(
                MovementLineRecord.asset_unit_id,
                func.max(MovementDocumentRecord.effective_date).label("effective_date"),
            )
            .join(MovementDocumentRecord, MovementLineRecord.movement_id == MovementDocumentRecord.id)
            .where(MovementLineRecord.asset_unit_id.in_(ids))
            .group_by(MovementLineRecord.asset_unit_id)
            .subquery()
        )
        statement = (
            select(
                AssetUnitRecord.id,
                ReceiptLineRecord.accounting_classification,
                ReceiptLineRecord.tracking_policy,
                ReceiptDocumentRecord.effective_date,
                latest.c.effective_date.label("transfer_date"),
                WriteOffLineRecord.write_off_id,
            )
            .outerjoin(ReceiptLineRecord, AssetUnitRecord.receipt_line_id == ReceiptLineRecord.id)
            .outerjoin(ReceiptDocumentRecord, ReceiptLineRecord.receipt_id == ReceiptDocumentRecord.id)
            .outerjoin(latest, latest.c.asset_unit_id == AssetUnitRecord.id)
            .outerjoin(WriteOffLineRecord, WriteOffLineRecord.asset_unit_id == AssetUnitRecord.id)
            .where(AssetUnitRecord.id.in_(ids))
        )
        with self.session.no_autoflush:
            rows = {row.id: row for row in self.session.execute(statement)}
        return tuple(
            WriteOffAsset(
                unit,
                AccountingClassification(rows[unit.id].accounting_classification)
                if rows[unit.id].accounting_classification
                else None,
                TrackingPolicy(rows[unit.id].tracking_policy) if rows[unit.id].tracking_policy else None,
                rows[unit.id].effective_date,
                rows[unit.id].transfer_date,
                rows[unit.id].write_off_id,
            )
            for unit in units
        )

    def save(self, unit: AssetUnit) -> None:
        with self.session.no_autoflush:
            record = self.session.get(AssetUnitRecord, unit.id)
        if record is None or (
            record.receipt_line_id,
            record.serial_namespace,
            record.serial_value,
            record.initial_location_ref,
            record.initial_party_ref,
        ) != (
            unit.receipt_line_id,
            unit.serial_number.namespace,
            unit.serial_number.value,
            unit.initial_custody.location_ref if unit.initial_custody else None,
            unit.initial_custody.responsible_party_ref if unit.initial_custody else None,
        ):
            raise RepositoryConflict("unit must exist and origin must remain unchanged")
        record.custody_location_ref = unit.custody.location_ref if unit.custody else None
        record.custody_party_ref = unit.custody.responsible_party_ref if unit.custody else None
        record.lifecycle_state = unit.lifecycle_state.value if unit.lifecycle_state else None
        record.last_effective_date = unit.last_effective_date

    def contains_serial(self, serial: SerialNumber) -> bool:
        if any(
            isinstance(record, AssetUnitRecord)
            and record.serial_namespace == serial.namespace
            and record.serial_value == serial.value
            for record in self.session.new
        ):
            return True
        statement = select(
            select(AssetUnitRecord.id)
            .where(
                AssetUnitRecord.serial_namespace == serial.namespace,
                AssetUnitRecord.serial_value == serial.value,
            )
            .exists()
        )
        with self.session.no_autoflush:
            return bool(self.session.scalar(statement))


class SqlAlchemyMovementRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, movement: MovementDocument) -> None:
        self.session.add(movement_to_record(movement))

    def get(self, movement_id: UUID) -> MovementDocument | None:
        for record in self.session.new:
            if isinstance(record, MovementDocumentRecord) and record.id == movement_id:
                return movement_from_record(record)
        statement = (
            select(MovementDocumentRecord)
            .where(MovementDocumentRecord.id == movement_id)
            .options(selectinload(MovementDocumentRecord.lines))
        )
        with self.session.no_autoflush:
            record = self.session.scalar(statement)
            return movement_from_record(record) if record is not None else None


class SqlAlchemyWriteOffRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, document: WriteOffDocument) -> None:
        self.session.add(write_off_to_record(document))

    def get(self, document_id: UUID) -> WriteOffDocument | None:
        for record in self.session.new:
            if isinstance(record, WriteOffDocumentRecord) and record.id == document_id:
                return write_off_from_record(record)
        statement = (
            select(WriteOffDocumentRecord)
            .where(WriteOffDocumentRecord.id == document_id)
            .options(selectinload(WriteOffDocumentRecord.lines))
        )
        with self.session.no_autoflush:
            record = self.session.scalar(statement)
            return write_off_from_record(record) if record is not None else None


class SqlAlchemyUnitOfWork:
    """One fresh session per command; repositories never flush or commit.

    Returned domain objects are detached immutable snapshots. Integrity errors
    are translated here, with database values excluded from application errors.
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory
        self._active = False
        self._committed = False

    def __enter__(self) -> Self:
        if self._active:
            raise RuntimeError("unit of work is already active")
        self.session = self.session_factory()
        self.nomenclatures = SqlAlchemyNomenclatureRepository(self.session)
        self.receipts = SqlAlchemyReceiptRepository(self.session)
        self.asset_units = SqlAlchemyAssetUnitRepository(self.session)
        self.movements = SqlAlchemyMovementRepository(self.session)
        self.write_offs = SqlAlchemyWriteOffRepository(self.session)
        self._active = True
        self._committed = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if not self._committed:
                self.rollback()
        finally:
            self.session.close()
            self._active = False

    def commit(self) -> None:
        if not self._active or self._committed:
            raise RuntimeError("commit requires an active uncommitted unit of work")
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
            if constraint == "pk_write_off_documents":
                raise DuplicateWriteOff("write-off identity already processed") from None
            if constraint == "uq_write_off_lines_asset_unit_id":
                raise AlreadyWrittenOff("unit already written off") from None
            if constraint == "pk_movement_documents":
                raise DuplicateMovement("movement identity already processed") from None
            if constraint == "uq_nomenclatures_code":
                raise DuplicateNomenclatureCode("nomenclature code already registered") from None
            if constraint == "uq_asset_units_serial":
                raise DuplicateSerialNumber("serial identifier already registered in namespace") from None
            raise RepositoryConflict("an identity or reference constraint prevents registration") from None
        except OperationalError as error:
            self.session.rollback()
            _translate_concurrency(error)
        self._committed = True

    def rollback(self) -> None:
        if not self._active:
            raise RuntimeError("rollback requires an active unit of work")
        self.session.rollback()
