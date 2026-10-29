"""Transactional fakes for local use and service tests, not durable storage.

Repositories write to private transaction copies. Commit publishes both copies
in one state replacement; rollback never changes previously committed data.
Overlapping transactions are rejected on a stale snapshot. This is not a
replacement for PostgreSQL constraints or database concurrency tests.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType, TracebackType
from typing import Self
from uuid import UUID

from maas_ua.domain.receipts import (
    AssetUnit,
    DuplicateSerialNumber,
    ReceiptDocument,
    SerialNumber,
)
from maas_ua.ports import RepositoryConflict


@dataclass(frozen=True)
class _State:
    receipts: Mapping[UUID, ReceiptDocument] = field(default_factory=dict)
    asset_units: Mapping[UUID, AssetUnit] = field(default_factory=dict)


class FakeStore:
    """Share this store between UoWs to retain committed in-memory records."""

    def __init__(self) -> None:
        self._state = _State()

    @property
    def receipts(self) -> Mapping[UUID, ReceiptDocument]:
        return MappingProxyType(self._state.receipts)

    @property
    def asset_units(self) -> Mapping[UUID, AssetUnit]:
        return MappingProxyType(self._state.asset_units)


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
    def __init__(self, units: Mapping[UUID, AssetUnit]) -> None:
        self._items = dict(units)

    def add(self, unit: AssetUnit) -> None:
        if unit.id in self._items:
            raise RepositoryConflict("unit identity already registered")
        if self.contains_serial(unit.serial_number):
            raise DuplicateSerialNumber(
                "serial identifier already registered in namespace"
            )
        self._items[unit.id] = unit

    def get(self, unit_id: UUID) -> AssetUnit | None:
        return self._items.get(unit_id)

    def contains_serial(self, serial: SerialNumber) -> bool:
        return any(unit.serial_number == serial for unit in self._items.values())


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
        self.receipts = FakeReceiptRepository(self._snapshot.receipts)
        self.asset_units = FakeAssetUnitRepository(self._snapshot.asset_units)
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
            MappingProxyType(dict(self.receipts._items)),
            MappingProxyType(dict(self.asset_units._items)),
        )
        self.store._state = state
        self._committed = True
        self.commit_count += 1

    def rollback(self) -> None:
        if not self._active:
            raise RuntimeError("rollback requires an active unit of work")
        self.receipts = FakeReceiptRepository(self.store.receipts)
        self.asset_units = FakeAssetUnitRepository(self.store.asset_units)
        self.rollback_count += 1
