"""Aggregate persistence contracts used by receipt registration."""

from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from maas_ua.domain.receipts import AssetUnit, ReceiptDocument, SerialNumber


class RepositoryConflict(RuntimeError):
    """An identity or concurrent write prevents persistence."""


class ReceiptRepository(Protocol):
    def add(self, receipt: ReceiptDocument) -> None: ...

    def get(self, receipt_id: UUID) -> ReceiptDocument | None: ...


class AssetUnitRepository(Protocol):
    def add(self, unit: AssetUnit) -> None: ...

    def get(self, unit_id: UUID) -> AssetUnit | None: ...

    def contains_serial(self, serial: SerialNumber) -> bool: ...


class UnitOfWork(Protocol):
    receipts: ReceiptRepository
    asset_units: AssetUnitRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
