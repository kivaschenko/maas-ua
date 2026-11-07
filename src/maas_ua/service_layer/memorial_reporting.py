"""Read contracts for canonical register turnover and assignment diagnostics."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from maas_ua.domain.journal import PostingSide
from maas_ua.domain.memorial_orders import InvalidMemorialOrder, MemorialOrderStatus


class MemorialOrderNotFound(LookupError):
    """No registered order has this technical identity."""


class UnsupportedCorrespondenceRendering(RuntimeError):
    """Approved presentation rules are required; balance does not establish pairing."""


class JournalRegisterState(StrEnum):
    UNASSIGNED = "UNASSIGNED"
    OPEN = "OPEN"
    FINALIZED = "FINALIZED"


@dataclass(frozen=True)
class GetMemorialOrderTotals:
    order_id: UUID

    def __post_init__(self):
        if not isinstance(self.order_id, UUID):
            raise InvalidMemorialOrder("register query requires UUID identity")


@dataclass(frozen=True, kw_only=True)
class GetJournalRegisterEntries:
    ledger_id: UUID
    state: JournalRegisterState
    period_id: UUID | None = None

    def __post_init__(self):
        if (
            not isinstance(self.ledger_id, UUID)
            or not isinstance(self.state, JournalRegisterState)
            or (self.period_id is not None and not isinstance(self.period_id, UUID))
        ):
            raise InvalidMemorialOrder("assignment query requires ledger UUID, explicit state and optional period UUID")


@dataclass(frozen=True, kw_only=True)
class JournalRegisterEntry:
    entry_id: UUID
    ledger_id: UUID
    period_id: UUID
    effective_date: date
    posting_instruction_key: str
    currency: str
    state: JournalRegisterState
    order_id: UUID | None


@dataclass(frozen=True, kw_only=True)
class CurrencyTurnover:
    currency: str
    debit: Decimal
    credit: Decimal


@dataclass(frozen=True, kw_only=True)
class AccountTurnover:
    version_id: UUID
    account_id: UUID
    account_code: str
    account_name: str
    currency: str
    side: PostingSide
    amount: Decimal


@dataclass(frozen=True, kw_only=True)
class MemorialOrderTotals:
    order_id: UUID
    status: MemorialOrderStatus
    entry_count: int
    currencies: tuple[CurrencyTurnover, ...]
    accounts: tuple[AccountTurnover, ...]
    diagnostics: tuple[str, ...] = ("correspondence_rendering_unsupported",)

    def correspondence_rows(self):
        raise UnsupportedCorrespondenceRendering(
            "whole-entry membership does not establish debit-to-credit correspondence; approved presentation rules required"
        )


class MemorialOrderReader(Protocol):
    def totals(self, query: GetMemorialOrderTotals) -> MemorialOrderTotals: ...

    def journal_entries(self, query: GetJournalRegisterEntries) -> tuple[JournalRegisterEntry, ...]: ...


def get_memorial_order_totals(query: GetMemorialOrderTotals, reader: MemorialOrderReader) -> MemorialOrderTotals:
    return reader.totals(query)


def get_journal_register_entries(
    query: GetJournalRegisterEntries, reader: MemorialOrderReader
) -> tuple[JournalRegisterEntry, ...]:
    return reader.journal_entries(query)
