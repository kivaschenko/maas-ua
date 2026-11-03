"""Transactional fakes for local use and service tests, not durable storage.

Repositories write to private transaction copies. Commit publishes all copies
in one state replacement; rollback never changes previously committed data.
Overlapping transactions are rejected on a stale snapshot. This is not a
replacement for PostgreSQL constraints or database concurrency tests.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType, TracebackType
from typing import Self
from uuid import UUID

from maas_ua.domain.movements import DuplicateMovement, MovementDocument
from maas_ua.domain.receipts import (
    AssetUnit,
    DuplicateSerialNumber,
    Nomenclature,
    ReceiptDocument,
    SerialNumber,
)
from maas_ua.domain.write_offs import AlreadyWrittenOff, DuplicateWriteOff, WriteOffAsset, WriteOffDocument
from maas_ua.ports import DuplicateNomenclatureCode, RepositoryConflict


@dataclass(frozen=True)
class _State:
    nomenclatures: Mapping[str, Nomenclature] = field(default_factory=dict)
    receipts: Mapping[UUID, ReceiptDocument] = field(default_factory=dict)
    asset_units: Mapping[UUID, AssetUnit] = field(default_factory=dict)
    movements: Mapping[UUID, MovementDocument] = field(default_factory=dict)
    write_offs: Mapping[UUID, WriteOffDocument] = field(default_factory=dict)


class FakeStore:
    """Share this store between UoWs to retain committed in-memory records."""

    def __init__(self, *, nomenclatures: tuple[Nomenclature, ...] = ()) -> None:
        repository = FakeNomenclatureRepository({})
        for item in nomenclatures:
            repository.add(item)
        self._state = _State(nomenclatures=repository._items)

    @property
    def nomenclatures(self) -> Mapping[str, Nomenclature]:
        return MappingProxyType(self._state.nomenclatures)

    @property
    def movements(self) -> Mapping[UUID, MovementDocument]:
        return MappingProxyType(self._state.movements)

    @property
    def write_offs(self) -> Mapping[UUID, WriteOffDocument]:
        return MappingProxyType(self._state.write_offs)

    @property
    def receipts(self) -> Mapping[UUID, ReceiptDocument]:
        return MappingProxyType(self._state.receipts)

    @property
    def asset_units(self) -> Mapping[UUID, AssetUnit]:
        return MappingProxyType(self._state.asset_units)


class FakeNomenclatureRepository:
    def __init__(self, items: Mapping[str, Nomenclature]) -> None:
        self._items = dict(items)

    def add(self, item: Nomenclature) -> None:
        if item.id in self._items:
            raise RepositoryConflict("nomenclature identity already registered")
        if self.get_by_code(item.code) is not None:
            raise DuplicateNomenclatureCode("nomenclature code already registered")
        self._items[item.id] = item

    def get(self, nomenclature_id: str) -> Nomenclature | None:
        return self._items.get(nomenclature_id)

    def get_by_code(self, code: str) -> Nomenclature | None:
        return next((item for item in self._items.values() if item.code == code), None)


class FakeReceiptRepository:
    def __init__(self, receipts: Mapping[UUID, ReceiptDocument]) -> None:
        self._items = dict(receipts)

    def add(self, receipt: ReceiptDocument) -> None:
        if receipt.id in self._items:
            raise RepositoryConflict("receipt identity already registered")
        self._items[receipt.id] = receipt

    def get(self, receipt_id: UUID) -> ReceiptDocument | None:
        return self._items.get(receipt_id)


class FakeAssetUnitRepository:
    def __init__(self, units: Mapping[UUID, AssetUnit], *, receipts=None, movements=None, write_offs=None) -> None:
        self._items = dict(units)
        self._receipts = receipts if receipts is not None else {}
        self._movements = movements if movements is not None else {}
        self._write_offs = write_offs if write_offs is not None else {}

    def add(self, unit: AssetUnit) -> None:
        if unit.id in self._items:
            raise RepositoryConflict("unit identity already registered")
        if self.contains_serial(unit.serial_number):
            raise DuplicateSerialNumber("serial identifier already registered in namespace")
        self._items[unit.id] = unit

    def get(self, unit_id: UUID) -> AssetUnit | None:
        return self._items.get(unit_id)

    def get_for_update(self, unit_ids: tuple[UUID, ...]) -> tuple[AssetUnit, ...]:
        return tuple(self._items[key] for key in sorted(unit_ids) if key in self._items)

    def save(self, unit: AssetUnit) -> None:
        previous = self._items.get(unit.id)
        if previous is None or (previous.receipt_line_id, previous.serial_number, previous.initial_custody) != (
            unit.receipt_line_id,
            unit.serial_number,
            unit.initial_custody,
        ):
            raise RepositoryConflict("unit must exist and origin must remain unchanged")
        self._items[unit.id] = unit

    def write_off_facts(self, units: tuple[AssetUnit, ...]) -> tuple[WriteOffAsset, ...]:
        origins = {line.id: (receipt, line) for receipt in self._receipts.values() for line in receipt.lines}
        result = []
        for unit in units:
            origin = origins.get(unit.receipt_line_id)
            receipt, line = origin if origin else (None, None)
            dates = [
                doc.effective_date
                for doc in self._movements.values()
                if any(item.asset_unit_id == unit.id for item in doc.lines)
            ]
            previous = next(
                (
                    doc.id
                    for doc in self._write_offs.values()
                    if any(item.asset_unit_id == unit.id for item in doc.lines)
                ),
                None,
            )
            result.append(
                WriteOffAsset(
                    unit,
                    line.accounting_classification if line else None,
                    line.nomenclature.tracking_policy if line else None,
                    receipt.effective_date if receipt else None,
                    max(dates) if dates else None,
                    previous,
                )
            )
        return tuple(result)

    def contains_serial(self, serial: SerialNumber) -> bool:
        return any(unit.serial_number == serial for unit in self._items.values())


class FakeMovementRepository:
    def __init__(self, items: Mapping[UUID, MovementDocument]) -> None:
        self._items = dict(items)

    def add(self, movement: MovementDocument) -> None:
        if movement.id in self._items:
            raise DuplicateMovement("movement identity already processed")
        self._items[movement.id] = movement

    def get(self, movement_id: UUID) -> MovementDocument | None:
        return self._items.get(movement_id)


class FakeWriteOffRepository:
    def __init__(self, items: Mapping[UUID, WriteOffDocument]) -> None:
        self._items = dict(items)

    def add(self, document: WriteOffDocument) -> None:
        if document.id in self._items:
            raise DuplicateWriteOff("write-off identity already processed")
        existing = {line.asset_unit_id for doc in self._items.values() for line in doc.lines}
        if any(line.asset_unit_id in existing for line in document.lines):
            raise AlreadyWrittenOff("unit already written off")
        self._items[document.id] = document

    def get(self, document_id: UUID) -> WriteOffDocument | None:
        return self._items.get(document_id)


class FakeUnitOfWork:
    def __init__(self, store: FakeStore | None = None) -> None:
        self.store = store if store is not None else FakeStore()
        self.commit_count = 0
        self.rollback_count = 0
        self._active = False

    def __enter__(self) -> Self:
        if self._active:
            raise RuntimeError("unit of work is already active")
        self._snapshot = self.store._state
        self.nomenclatures = FakeNomenclatureRepository(self._snapshot.nomenclatures)
        self.receipts = FakeReceiptRepository(self._snapshot.receipts)
        self.movements = FakeMovementRepository(self._snapshot.movements)
        self.write_offs = FakeWriteOffRepository(self._snapshot.write_offs)
        self.asset_units = FakeAssetUnitRepository(
            self._snapshot.asset_units,
            receipts=self.receipts._items,
            movements=self.movements._items,
            write_offs=self.write_offs._items,
        )
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
            self._active = False

    def commit(self) -> None:
        if not self._active or self._committed:
            raise RuntimeError("commit requires an active uncommitted unit of work")
        if self.store._state is not self._snapshot:
            raise RepositoryConflict("store changed during transaction")
        state = _State(
            MappingProxyType(dict(self.nomenclatures._items)),
            MappingProxyType(dict(self.receipts._items)),
            MappingProxyType(dict(self.asset_units._items)),
            MappingProxyType(dict(self.movements._items)),
            MappingProxyType(dict(self.write_offs._items)),
        )
        self.store._state = state
        self._committed = True
        self.commit_count += 1

    def rollback(self) -> None:
        if not self._active:
            raise RuntimeError("rollback requires an active unit of work")
        self.nomenclatures = FakeNomenclatureRepository(self.store.nomenclatures)
        self.receipts = FakeReceiptRepository(self.store.receipts)
        self.movements = FakeMovementRepository(self.store.movements)
        self.write_offs = FakeWriteOffRepository(self.store.write_offs)
        self.asset_units = FakeAssetUnitRepository(
            self.store.asset_units,
            receipts=self.receipts._items,
            movements=self.movements._items,
            write_offs=self.write_offs._items,
        )
        self.rollback_count += 1
