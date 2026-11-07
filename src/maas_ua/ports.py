"""Aggregate persistence contracts used by receipt and transfer commands."""

from datetime import date
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from maas_ua.domain.accounting import (
    AccountDefinition,
    ChartOfAccounts,
    ChartSupersession,
    ChartVersion,
    SupersededChartVersion,
)
from maas_ua.domain.journal import AccountingLedger, AccountingPeriod, JournalEntry, LedgerSourceReference
from maas_ua.domain.movements import MovementDocument
from maas_ua.domain.receipts import AssetUnit, Nomenclature, ReceiptDocument, SerialNumber
from maas_ua.domain.write_offs import WriteOffAsset, WriteOffDocument


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

    def write_off_facts(self, units: tuple[AssetUnit, ...]) -> tuple[WriteOffAsset, ...]: ...


class MovementRepository(Protocol):
    def add(self, movement: MovementDocument) -> None: ...

    def get(self, movement_id: UUID) -> MovementDocument | None: ...


class WriteOffRepository(Protocol):
    def add(self, document: WriteOffDocument) -> None: ...

    def get(self, document_id: UUID) -> WriteOffDocument | None: ...


class ChartRepository(Protocol):
    def add_chart(self, chart: ChartOfAccounts) -> None: ...

    def get_chart(self, chart_id: UUID) -> ChartOfAccounts | None: ...

    def get_chart_for_update(self, chart_id: UUID) -> ChartOfAccounts | None: ...

    def add_version(self, version: ChartVersion) -> None: ...

    def get_version(self, version_id: UUID) -> ChartVersion | None: ...

    def versions_for_chart(self, chart_id: UUID) -> tuple[ChartVersion, ...]: ...

    def version_as_of(self, chart_id: UUID, as_of: date) -> ChartVersion | None: ...

    def get_account(self, account_id: UUID) -> AccountDefinition | None: ...

    def get_supersession(self, transition_id: UUID) -> ChartSupersession | None: ...

    def protected_through(self, version_id: UUID) -> date | None: ...

    def apply_supersession(self, result: SupersededChartVersion) -> None: ...


class AccountingLedgerRepository(Protocol):
    def add(self, ledger: AccountingLedger) -> None: ...

    def get(self, ledger_id: UUID) -> AccountingLedger | None: ...

    def add_period(self, period: AccountingPeriod) -> None: ...

    def get_period_for_update(self, period_id: UUID) -> AccountingPeriod | None: ...

    def add_source(self, source: LedgerSourceReference) -> None: ...

    def get_source(self, source_id: UUID) -> LedgerSourceReference | None: ...


class JournalEntryRepository(Protocol):
    def add(self, entry: JournalEntry) -> None: ...

    def get(self, entry_id: UUID) -> JournalEntry | None: ...

    def get_by_instruction(self, ledger_id: UUID, key: str) -> JournalEntry | None: ...


class UnitOfWork(Protocol):
    ledgers: AccountingLedgerRepository
    journals: JournalEntryRepository
    charts: ChartRepository
    write_offs: WriteOffRepository
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
