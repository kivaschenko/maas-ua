"""Aggregate persistence contracts used by receipt and transfer commands."""

from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from maas_ua.domain.movements import MovementDocument
from maas_ua.domain.receipts import AssetUnit, Nomenclature, ReceiptDocument, SerialNumber


class RepositoryConflict(RuntimeError):
    """An identity or concurrent write prevents persistence."""


class DuplicateNomenclatureCode(RepositoryConflict):
    """A stable catalog code is already registered."""


class NomenclatureRepository(Protocol):
    def add(self, nomenclature: Nomenclature) -> None: ...

    def get(self, nomenclature_id: str) -> Nomenclature | None: ...

    def get_by_code(self, code: str) -> Nomenclature | None: ...


class ReceiptRepository(Protocol):
    def add(self, receipt: ReceiptDocument) -> None: ...

    def get(self, receipt_id: UUID) -> ReceiptDocument | None: ...


class AssetUnitRepository(Protocol):
    def add(self, unit: AssetUnit) -> None: ...

    def get(self, unit_id: UUID) -> AssetUnit | None: ...

    def contains_serial(self, serial: SerialNumber) -> bool: ...

    def get_for_update(self, unit_ids: tuple[UUID, ...]) -> tuple[AssetUnit, ...]: ...

    def save(self, unit: AssetUnit) -> None: ...


class MovementRepository(Protocol):
    def add(self, movement: MovementDocument) -> None: ...

    def get(self, movement_id: UUID) -> MovementDocument | None: ...


class UnitOfWork(Protocol):
    movements: MovementRepository
    nomenclatures: NomenclatureRepository
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
