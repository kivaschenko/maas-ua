"""Aggregate repositories and explicit transaction ownership for PostgreSQL."""

from collections.abc import Callable
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from maas_ua.adapters.orm import (
    AssetUnitRecord,
    ReceiptDocumentRecord,
    receipt_from_record,
    receipt_to_record,
    unit_from_record,
    unit_to_record,
)
from maas_ua.domain.receipts import (
    AssetUnit,
    DuplicateSerialNumber,
    ReceiptDocument,
    SerialNumber,
)
from maas_ua.ports import RepositoryConflict


class SqlAlchemyReceiptRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, receipt: ReceiptDocument) -> None:
        self.session.add(receipt_to_record(receipt))

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
        self.receipts = SqlAlchemyReceiptRepository(self.session)
        self.asset_units = SqlAlchemyAssetUnitRepository(self.session)
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
            constraint = getattr(
                getattr(error.orig, "diag", None), "constraint_name", None
            )
            if constraint == "uq_asset_units_serial":
                raise DuplicateSerialNumber(
                    "serial identifier already registered in namespace"
                ) from None
            raise RepositoryConflict(
                "an identity or reference constraint prevents registration"
            ) from None
        self._committed = True

    def rollback(self) -> None:
        if not self._active:
            raise RuntimeError("rollback requires an active unit of work")
        self.session.rollback()
