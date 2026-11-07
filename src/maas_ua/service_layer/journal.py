"""Manually authorized journals and explicit financial reference registration."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from maas_ua.domain.journal import (
    AccountingLedger,
    AccountingPeriod,
    DuplicateJournalEntry,
    InvalidJournalEntry,
    JournalEntry,
    LedgerSourceReference,
    PostingLine,
    validate_journal_references,
)
from maas_ua.ports import UnitOfWork


def register_accounting_ledger(ledger: AccountingLedger, uow: UnitOfWork) -> AccountingLedger:
    with uow:
        if uow.charts.get_chart(ledger.chart_id) is None:
            raise InvalidJournalEntry("ledger requires a registered chart and its explicit accounting entity")
        uow.ledgers.add(ledger)
        uow.commit()
    return ledger


def register_accounting_period(period: AccountingPeriod, uow: UnitOfWork) -> AccountingPeriod:
    with uow:
        ledger = uow.ledgers.get(period.ledger_id)
        if ledger is None:
            raise InvalidJournalEntry("accounting period requires an existing ledger")
        uow.charts.get_chart_for_update(ledger.chart_id)
        uow.ledgers.add_period(period)
        uow.commit()
    return period


def attribute_ledger_source(source: LedgerSourceReference, uow: UnitOfWork) -> LedgerSourceReference:
    with uow:
        if uow.ledgers.get(source.ledger_id) is None:
            raise InvalidJournalEntry("source attribution requires an existing ledger")
        document = (
            uow.receipts.get(source.receipt_id)
            if source.receipt_id is not None
            else uow.write_offs.get(source.write_off_id)
        )
        if document is None:
            raise InvalidJournalEntry("source attribution requires an existing typed property document")
        uow.ledgers.add_source(source)
        uow.commit()
    return source


@dataclass(frozen=True, kw_only=True)
class PostJournalEntry:
    id: UUID
    ledger_id: UUID
    version_id: UUID
    period_id: UUID
    posting_instruction_key: str
    effective_date: date
    currency: str
    purpose: str
    actor_ref: str
    authorization_reference: str
    lines: tuple[PostingLine, ...]
    source_reference_ids: tuple[UUID, ...]

    def __post_init__(self):
        object.__setattr__(self, "lines", tuple(self.lines))
        object.__setattr__(self, "source_reference_ids", tuple(self.source_reference_ids))


def _utc_now():
    return datetime.now(UTC)


def post_journal_entry(
    command: PostJournalEntry,
    uow: UnitOfWork,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> JournalEntry:
    with uow:
        ledger = uow.ledgers.get(command.ledger_id)
        if ledger is None:
            raise InvalidJournalEntry("accounting ledger must exist")
        # Global protocol: chart first, then period, then fresh reference reads.
        # Future closing must use this order and retain locks through commit.
        uow.charts.get_chart_for_update(ledger.chart_id)
        period = uow.ledgers.get_period_for_update(command.period_id)
        if (
            uow.journals.get(command.id) is not None
            or uow.journals.get_by_instruction(ledger.id, command.posting_instruction_key) is not None
        ):
            raise DuplicateJournalEntry("journal identity or business posting instruction already registered")
        version = uow.charts.get_version(command.version_id)
        sources = tuple(uow.ledgers.get_source(key) for key in command.source_reference_ids)
        if period is None or version is None or any(source is None for source in sources):
            raise InvalidJournalEntry("period, approved version and explicit source attributions must exist")
        result = JournalEntry(chart_id=ledger.chart_id, created_at=clock(), **vars(command))
        validate_journal_references(result, ledger, period, version, sources)
        uow.journals.add(result)
        uow.commit()
    return result
