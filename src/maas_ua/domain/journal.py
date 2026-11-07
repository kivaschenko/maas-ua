"""Immutable manual balance-account journals; no correspondence or rounding policy."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import MAX_EMAX, MIN_EMIN, Decimal, localcontext
from enum import StrEnum
from uuid import UUID

from maas_ua.domain.accounting import AccountBasis, AccountingDimensions, ChartVersion, InvalidAccountingReference
from maas_ua.domain.receipts import Money


class InvalidJournalEntry(ValueError):
    """Invalid journal, financial references, period or administrative evidence."""


class DuplicateJournalEntry(InvalidJournalEntry):
    """An entry UUID or ledger's business instruction is already registered."""


def _ids(*values):
    if any(not isinstance(value, UUID) for value in values):
        raise InvalidJournalEntry("financial identities must be UUIDs")


def _text(*values):
    if any(not isinstance(value, str) or not value or value != value.strip() for value in values):
        raise InvalidJournalEntry("financial references require nonblank exact text without outer whitespace")


def _date(*values):
    if any(type(value) is not date for value in values):
        raise InvalidJournalEntry("financial effective dates and boundaries must be business dates")


def _evidence(actor, authorization, timestamp):
    _text(actor, authorization)
    if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
        raise InvalidJournalEntry("financial registration timestamps must be timezone-aware")


@dataclass(frozen=True)
class AccountingLedger:
    id: UUID
    chart_id: UUID

    def __post_init__(self):
        _ids(self.id, self.chart_id)


@dataclass(frozen=True, kw_only=True)
class AccountingPeriod:
    id: UUID
    ledger_id: UUID
    starts_on: date
    ends_before: date
    posting_allowed: bool
    actor_ref: str
    authorization_reference: str
    created_at: datetime

    def __post_init__(self):
        _ids(self.id, self.ledger_id)
        _date(self.starts_on, self.ends_before)
        if self.ends_before <= self.starts_on or type(self.posting_allowed) is not bool:
            raise InvalidJournalEntry("period requires a positive bounded interval and explicit posting permission")
        _evidence(self.actor_ref, self.authorization_reference, self.created_at)

    def contains(self, effective_date: date) -> bool:
        _date(effective_date)
        return self.starts_on <= effective_date < self.ends_before


@dataclass(frozen=True, kw_only=True)
class LedgerSourceReference:
    id: UUID
    ledger_id: UUID
    actor_ref: str
    authorization_reference: str
    created_at: datetime
    receipt_id: UUID | None = None
    write_off_id: UUID | None = None

    def __post_init__(self):
        _ids(self.id, self.ledger_id)
        if (self.receipt_id is None) == (self.write_off_id is None):
            raise InvalidJournalEntry("source attribution requires exactly one receipt or write-off")
        _ids(self.receipt_id if self.receipt_id is not None else self.write_off_id)
        _evidence(self.actor_ref, self.authorization_reference, self.created_at)


class PostingSide(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


@dataclass(frozen=True, kw_only=True)
class PostingLine:
    account_id: UUID
    side: PostingSide
    amount: Money
    dimensions: AccountingDimensions = field(default_factory=AccountingDimensions)

    def __post_init__(self):
        _ids(self.account_id)
        if not isinstance(self.side, PostingSide) or not isinstance(self.amount, Money) or self.amount.amount <= 0:
            raise InvalidJournalEntry("posting requires an explicit side and strictly positive finite Money")
        if not isinstance(self.dimensions, AccountingDimensions):
            raise InvalidJournalEntry("posting dimensions must use typed accounting references")


def _exact_sum(amounts: tuple[Decimal, ...]) -> Decimal:
    # Include every exponent and sufficient carry digits, independently of the
    # caller's precision/exponent range. No currency quantization is performed.
    precision = max(a.adjusted() for a in amounts) - min(a.as_tuple().exponent for a in amounts)
    with localcontext() as context:
        context.prec = max(1, precision + len(str(len(amounts))) + 2)
        context.Emax, context.Emin = MAX_EMAX, MIN_EMIN
        return sum(amounts, Decimal(0))


@dataclass(frozen=True, kw_only=True)
class JournalEntry:
    id: UUID
    ledger_id: UUID
    chart_id: UUID
    version_id: UUID
    period_id: UUID
    posting_instruction_key: str
    effective_date: date
    currency: str
    purpose: str
    actor_ref: str
    authorization_reference: str
    created_at: datetime
    lines: tuple[PostingLine, ...]
    source_reference_ids: tuple[UUID, ...]

    def __post_init__(self):
        _ids(self.id, self.ledger_id, self.chart_id, self.version_id, self.period_id)
        _text(self.posting_instruction_key, self.currency, self.purpose)
        _date(self.effective_date)
        _evidence(self.actor_ref, self.authorization_reference, self.created_at)
        lines, sources = tuple(self.lines), tuple(self.source_reference_ids)
        if not lines or any(not isinstance(line, PostingLine) for line in lines):
            raise InvalidJournalEntry("journal requires validated posting lines")
        if any(line.amount.currency != self.currency for line in lines):
            raise InvalidJournalEntry("all posting lines must use the entry currency")
        debit = tuple(line.amount.amount for line in lines if line.side is PostingSide.DEBIT)
        credit = tuple(line.amount.amount for line in lines if line.side is PostingSide.CREDIT)
        if not debit or not credit or _exact_sum(debit) != _exact_sum(credit):
            raise InvalidJournalEntry("journal requires both sides and exactly equal debit and credit totals")
        _ids(*sources)
        if not sources or len(set(sources)) != len(sources):
            raise InvalidJournalEntry("journal requires distinct documentary source references")
        object.__setattr__(self, "lines", lines)
        object.__setattr__(self, "source_reference_ids", sources)


def validate_journal_references(
    entry: JournalEntry,
    ledger: AccountingLedger,
    period: AccountingPeriod,
    version: ChartVersion,
    sources: tuple[LedgerSourceReference, ...],
) -> None:
    """Validate contextual facts refreshed under chart then period transaction locks."""
    if (
        entry.ledger_id != ledger.id
        or entry.chart_id != ledger.chart_id
        or version.chart_id != ledger.chart_id
        or entry.version_id != version.id
        or not version.applies_on(entry.effective_date)
    ):
        raise InvalidJournalEntry("selected ledger and applicable chart version must match the entry")
    if (
        period.id != entry.period_id
        or period.ledger_id != ledger.id
        or not period.posting_allowed
        or not period.contains(entry.effective_date)
    ):
        raise InvalidJournalEntry("accounting date must belong to a permitted period of the ledger")
    if (
        len(sources) != len(entry.source_reference_ids)
        or {s.id for s in sources} != set(entry.source_reference_ids)
        or any(s.ledger_id != ledger.id for s in sources)
    ):
        raise InvalidJournalEntry("every documentary source must be explicitly attributed to this ledger")
    for line in entry.lines:
        account = version.account_by_id(line.account_id)
        if account is None or not account.posting_allowed or account.basis is not AccountBasis.BALANCE:
            raise InvalidJournalEntry("posting requires an existing posting-enabled balance account in this version")
        try:
            line.dimensions.validate_for(version)
        except InvalidAccountingReference:
            raise InvalidJournalEntry("local analytics must resolve in the selected account version") from None
