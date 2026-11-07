"""Insert-only journal aggregates and explicit financial reference persistence."""

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import selectinload

from maas_ua.adapters.journal_orm import (
    AccountingLedgerRecord,
    AccountingPeriodRecord,
    JournalEntryRecord,
    LedgerSourceRecord,
    journal_from_record,
    journal_to_record,
    period_from_record,
    source_from_record,
)
from maas_ua.domain.journal import AccountingLedger
from maas_ua.ports import RepositoryConflict


class SqlAlchemyAccountingLedgerRepository:
    def __init__(self, session):
        self.session = session

    def _get(self, model, key):
        for record in self.session.new:
            if isinstance(record, model) and record.id == key:
                return record
        with self.session.no_autoflush:
            return self.session.get(model, key)

    def add(self, ledger):
        self.session.add(AccountingLedgerRecord(**vars(ledger)))

    def get(self, ledger_id):
        record = self._get(AccountingLedgerRecord, ledger_id)
        return AccountingLedger(record.id, record.chart_id) if record is not None else None

    def add_period(self, period):
        self.session.add(AccountingPeriodRecord(**vars(period)))

    def get_period(self, period_id):
        record = self._get(AccountingPeriodRecord, period_id)
        return period_from_record(record) if record is not None else None

    def get_period_for_update(self, period_id):
        try:
            with self.session.no_autoflush:
                record = self.session.scalar(
                    select(AccountingPeriodRecord)
                    .where(AccountingPeriodRecord.id == period_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
        except OperationalError as error:
            if getattr(error.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
                raise RepositoryConflict("concurrent period transaction; retry the complete command") from None
            raise
        return period_from_record(record) if record is not None else None

    def add_source(self, source):
        self.session.add(LedgerSourceRecord(**vars(source)))

    def get_source(self, source_id):
        record = self._get(LedgerSourceRecord, source_id)
        return source_from_record(record) if record is not None else None


class SqlAlchemyJournalEntryRepository:
    def __init__(self, session):
        self.session = session

    def add(self, entry):
        self.session.add(journal_to_record(entry))

    def _find(self, predicate):
        with self.session.no_autoflush:
            record = self.session.scalar(
                select(JournalEntryRecord)
                .where(predicate)
                .options(selectinload(JournalEntryRecord.lines), selectinload(JournalEntryRecord.sources))
            )
        return journal_from_record(record) if record is not None else None

    def get(self, entry_id):
        for record in self.session.new:
            if isinstance(record, JournalEntryRecord) and record.id == entry_id:
                return journal_from_record(record)
        return self._find(JournalEntryRecord.id == entry_id)

    def get_by_instruction(self, ledger_id, key):
        for record in self.session.new:
            if isinstance(record, JournalEntryRecord) and (record.ledger_id, record.posting_instruction_key) == (
                ledger_id,
                key,
            ):
                return journal_from_record(record)
        return self._find(
            (JournalEntryRecord.ledger_id == ledger_id) & (JournalEntryRecord.posting_instruction_key == key)
        )
