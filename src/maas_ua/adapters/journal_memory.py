"""Financial fakes integrated with the shared atomic snapshot UoW."""

from maas_ua.domain.journal import DuplicateJournalEntry
from maas_ua.ports import RepositoryConflict


class FakeAccountingLedgerRepository:
    def __init__(self, ledgers, periods, sources):
        self._ledgers, self._periods, self._sources = dict(ledgers), dict(periods), dict(sources)

    def add(self, ledger):
        if ledger.id in self._ledgers:
            raise RepositoryConflict("ledger identity already registered")
        self._ledgers[ledger.id] = ledger

    def get(self, ledger_id):
        return self._ledgers.get(ledger_id)

    def add_period(self, period):
        if period.id in self._periods or any(
            p.ledger_id == period.ledger_id and p.starts_on < period.ends_before and period.starts_on < p.ends_before
            for p in self._periods.values()
        ):
            raise RepositoryConflict("accounting period identity or interval conflict")
        self._periods[period.id] = period

    def get_period_for_update(self, period_id):
        return self._periods.get(period_id)

    def add_source(self, source):
        if source.id in self._sources or any(
            (source.receipt_id is not None and source.receipt_id == old.receipt_id)
            or (source.write_off_id is not None and source.write_off_id == old.write_off_id)
            for old in self._sources.values()
        ):
            raise RepositoryConflict("source identity or document attribution already registered")
        self._sources[source.id] = source

    def get_source(self, source_id):
        return self._sources.get(source_id)


class FakeJournalEntryRepository:
    def __init__(self, journals):
        self._items = dict(journals)

    def add(self, entry):
        if (
            entry.id in self._items
            or self.get_by_instruction(entry.ledger_id, entry.posting_instruction_key) is not None
        ):
            raise DuplicateJournalEntry("journal identity or business posting instruction already registered")
        self._items[entry.id] = entry

    def get(self, entry_id):
        return self._items.get(entry_id)

    def get_by_instruction(self, ledger_id, key):
        return next(
            (e for e in self._items.values() if (e.ledger_id, e.posting_instruction_key) == (ledger_id, key)), None
        )
