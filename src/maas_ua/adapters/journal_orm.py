"""Declarative financial records with explicit conversion to immutable snapshots."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from maas_ua.adapters.orm import Base
from maas_ua.domain.accounting import (
    AccountingDimensions,
    BudgetProgramCode,
    CounterpartyRef,
    FundingSourceRef,
    FundRef,
    KEKVCode,
    ReportingUnitRef,
)
from maas_ua.domain.journal import AccountingPeriod, JournalEntry, LedgerSourceReference, PostingLine, PostingSide
from maas_ua.domain.receipts import Money


def _text_checks(*columns):
    return tuple(CheckConstraint(f"{c} <> '' AND {c} = btrim({c})", name=f"{c}_text") for c in columns)


class AccountingLedgerRecord(Base):
    __tablename__ = "accounting_ledgers"
    __table_args__ = (UniqueConstraint("id", "chart_id", name="uq_accounting_ledgers_chart"),)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    chart_id: Mapped[UUID] = mapped_column(ForeignKey("accounting_charts.id"))


class AccountingPeriodRecord(Base):
    __tablename__ = "accounting_periods"
    __table_args__ = (
        UniqueConstraint("ledger_id", "id", name="uq_accounting_periods_ledger"),
        CheckConstraint("isfinite(starts_on) AND isfinite(ends_before) AND starts_on < ends_before", name="interval"),
        ExcludeConstraint(
            ("ledger_id", "="),
            (text("daterange(starts_on, ends_before, '[)')"), "&&"),
            name="ex_accounting_periods_interval",
            using="gist",
        ),
        *_text_checks("actor_ref", "authorization_reference"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    ledger_id: Mapped[UUID] = mapped_column(ForeignKey("accounting_ledgers.id"))
    starts_on: Mapped[date]
    ends_before: Mapped[date]
    posting_allowed: Mapped[bool]
    actor_ref: Mapped[str] = mapped_column(Text)
    authorization_reference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LedgerSourceRecord(Base):
    __tablename__ = "accounting_ledger_sources"
    __table_args__ = (
        UniqueConstraint("ledger_id", "id", name="uq_accounting_ledger_sources_ledger"),
        # Exclusive attribution in M6.2; journal membership itself is many-to-many.
        UniqueConstraint("receipt_id", name="uq_accounting_ledger_sources_receipt"),
        UniqueConstraint("write_off_id", name="uq_accounting_ledger_sources_write_off"),
        CheckConstraint("(receipt_id IS NULL) <> (write_off_id IS NULL)", name="one_document"),
        *_text_checks("actor_ref", "authorization_reference"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    ledger_id: Mapped[UUID] = mapped_column(ForeignKey("accounting_ledgers.id"))
    receipt_id: Mapped[UUID | None] = mapped_column(ForeignKey("receipt_documents.id"))
    write_off_id: Mapped[UUID | None] = mapped_column(ForeignKey("write_off_documents.id"))
    actor_ref: Mapped[str] = mapped_column(Text)
    authorization_reference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class JournalEntryRecord(Base):
    __tablename__ = "accounting_journal_entries"
    __table_args__ = (
        UniqueConstraint("ledger_id", "posting_instruction_key", name="uq_accounting_journals_instruction"),
        UniqueConstraint("id", "version_id", name="uq_accounting_journals_version"),
        UniqueConstraint("id", "ledger_id", name="uq_accounting_journals_ledger"),
        ForeignKeyConstraint(
            ("ledger_id", "chart_id"),
            ("accounting_ledgers.id", "accounting_ledgers.chart_id"),
            name="fk_accounting_journals_ledger_chart",
        ),
        ForeignKeyConstraint(
            ("chart_id", "version_id"),
            ("accounting_chart_versions.chart_id", "accounting_chart_versions.id"),
            name="fk_accounting_journals_chart_version",
        ),
        ForeignKeyConstraint(
            ("ledger_id", "period_id"),
            ("accounting_periods.ledger_id", "accounting_periods.id"),
            name="fk_accounting_journals_ledger_period",
        ),
        CheckConstraint("isfinite(effective_date)", name="effective_date_finite"),
        CheckConstraint("line_count >= 2 AND source_count >= 1", name="child_counts"),
        *_text_checks("posting_instruction_key", "currency", "purpose", "actor_ref", "authorization_reference"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    ledger_id: Mapped[UUID]
    chart_id: Mapped[UUID]
    version_id: Mapped[UUID] = mapped_column(index=True)
    period_id: Mapped[UUID]
    posting_instruction_key: Mapped[str] = mapped_column(Text(collation="C"))
    effective_date: Mapped[date]
    currency: Mapped[str] = mapped_column(Text(collation="C"))
    purpose: Mapped[str] = mapped_column(Text)
    actor_ref: Mapped[str] = mapped_column(Text)
    authorization_reference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    line_count: Mapped[int]
    source_count: Mapped[int]
    lines: Mapped[list[PostingLineRecord]] = relationship(order_by="PostingLineRecord.position")
    sources: Mapped[list[JournalSourceRecord]] = relationship(order_by="JournalSourceRecord.position")


class PostingLineRecord(Base):
    __tablename__ = "accounting_posting_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ("entry_id", "version_id"),
            ("accounting_journal_entries.id", "accounting_journal_entries.version_id"),
            name="fk_accounting_lines_entry_version",
        ),
        ForeignKeyConstraint(
            ("version_id", "account_id"),
            ("accounting_account_definitions.version_id", "accounting_account_definitions.id"),
            name="fk_accounting_lines_account_version",
        ),
        ForeignKeyConstraint(
            ("version_id", "local_accounting_analytics"),
            ("accounting_account_definitions.version_id", "accounting_account_definitions.id"),
            name="fk_accounting_lines_local_version",
        ),
        CheckConstraint("position >= 0", name="position_nonnegative"),
        CheckConstraint("side IN ('DEBIT', 'CREDIT')", name="side"),
        CheckConstraint("amount > 0 AND amount < 'Infinity'::numeric", name="amount_positive_finite"),
        *(
            CheckConstraint(f"{c} IS NULL OR ({c} <> '' AND {c} = btrim({c}))", name=f"{c}_text")
            for c in ("fund", "budget_program_code", "kekv", "funding_source", "reporting_unit", "counterparty")
        ),
    )
    entry_id: Mapped[UUID] = mapped_column(primary_key=True)
    position: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[UUID]
    account_id: Mapped[UUID]
    side: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric())
    fund: Mapped[str | None] = mapped_column(Text)
    budget_program_code: Mapped[str | None] = mapped_column(Text)
    kekv: Mapped[str | None] = mapped_column(Text)
    funding_source: Mapped[str | None] = mapped_column(Text)
    reporting_unit: Mapped[str | None] = mapped_column(Text)
    counterparty: Mapped[str | None] = mapped_column(Text)
    local_accounting_analytics: Mapped[UUID | None]


class JournalSourceRecord(Base):
    __tablename__ = "accounting_journal_sources"
    __table_args__ = (
        UniqueConstraint("entry_id", "source_id", name="uq_accounting_journal_sources_source"),
        ForeignKeyConstraint(
            ("entry_id", "ledger_id"),
            ("accounting_journal_entries.id", "accounting_journal_entries.ledger_id"),
            name="fk_accounting_sources_entry_ledger",
        ),
        ForeignKeyConstraint(
            ("ledger_id", "source_id"),
            ("accounting_ledger_sources.ledger_id", "accounting_ledger_sources.id"),
            name="fk_accounting_sources_attribution_ledger",
        ),
        CheckConstraint("position >= 0", name="position_nonnegative"),
    )
    entry_id: Mapped[UUID] = mapped_column(primary_key=True)
    position: Mapped[int] = mapped_column(primary_key=True)
    ledger_id: Mapped[UUID]
    source_id: Mapped[UUID]


def period_from_record(record):
    return AccountingPeriod(**{key: getattr(record, key) for key in AccountingPeriod.__dataclass_fields__})


def source_from_record(record):
    return LedgerSourceReference(**{key: getattr(record, key) for key in LedgerSourceReference.__dataclass_fields__})


def journal_to_record(entry: JournalEntry) -> JournalEntryRecord:
    fields = {key: value for key, value in vars(entry).items() if key not in {"lines", "source_reference_ids"}}
    lines = []
    for position, item in enumerate(entry.lines):
        dimensions = {
            key: value.value if value is not None and key != "local_accounting_analytics" else value
            for key, value in vars(item.dimensions).items()
        }
        lines.append(
            PostingLineRecord(
                entry_id=entry.id,
                version_id=entry.version_id,
                position=position,
                account_id=item.account_id,
                side=item.side.value,
                amount=item.amount.amount,
                **dimensions,
            )
        )
    return JournalEntryRecord(
        **fields,
        line_count=len(lines),
        source_count=len(entry.source_reference_ids),
        lines=lines,
        sources=[
            JournalSourceRecord(entry_id=entry.id, ledger_id=entry.ledger_id, position=i, source_id=key)
            for i, key in enumerate(entry.source_reference_ids)
        ],
    )


def journal_from_record(record: JournalEntryRecord) -> JournalEntry:
    dimension_types = dict(
        fund=FundRef,
        budget_program_code=BudgetProgramCode,
        kekv=KEKVCode,
        funding_source=FundingSourceRef,
        reporting_unit=ReportingUnitRef,
        counterparty=CounterpartyRef,
    )
    return JournalEntry(
        **{
            key: getattr(record, key)
            for key in JournalEntry.__dataclass_fields__
            if key not in {"lines", "source_reference_ids"}
        },
        lines=tuple(
            PostingLine(
                account_id=item.account_id,
                side=PostingSide(item.side),
                amount=Money(item.amount, record.currency),
                dimensions=AccountingDimensions(
                    **{
                        key: factory(getattr(item, key)) if getattr(item, key) is not None else None
                        for key, factory in dimension_types.items()
                    },
                    local_accounting_analytics=item.local_accounting_analytics,
                ),
            )
            for item in record.lines
        ),
        source_reference_ids=tuple(item.source_id for item in record.sources),
    )
