from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest
from accounting_fixtures import NOW, version
from journal_fixtures import command, period, register_context

from maas_ua.adapters.memory import FakeStore, FakeUnitOfWork
from maas_ua.domain.journal import AccountingLedger, DuplicateJournalEntry, InvalidJournalEntry
from maas_ua.ports import RepositoryConflict
from maas_ua.service_layer.accounting import SupersedeChartVersion, supersede_chart_version
from maas_ua.service_layer.journal import (
    attribute_ledger_source,
    post_journal_entry,
    register_accounting_ledger,
    register_accounting_period,
)


@pytest.fixture
def registered(write_off_receipt):
    store = FakeStore()
    context = register_context(lambda: FakeUnitOfWork(store), write_off_receipt)
    return store, context


def test_post_commits_once_and_source_can_support_distinct_components(registered):
    store, context = registered
    uow = FakeUnitOfWork(store)
    first = post_journal_entry(command(context), uow, clock=lambda: NOW)
    assert uow.commit_count == 1
    second = post_journal_entry(command(context, posting_instruction_key="EXAMPLE-COMPONENT-2"), FakeUnitOfWork(store))
    with FakeUnitOfWork(store) as reader:
        assert reader.journals.get(first.id) == first
        assert reader.journals.get_by_instruction(first.ledger_id, first.posting_instruction_key) == first
        assert reader.journals.get(second.id).source_reference_ids == first.source_reference_ids
        assert reader.charts.protected_through(first.version_id) == first.effective_date


@pytest.mark.parametrize("failure", ["ledger", "period", "date", "version", "account", "source", "source_ledger"])
def test_invalid_references_prevent_commit(registered, failure):
    store, context = registered
    request = command(context)
    if failure in {"ledger", "period", "version"}:
        request = replace(request, **{failure + "_id": uuid4()})
    elif failure == "date":
        request = replace(request, effective_date=date(2027, 1, 1))
    elif failure == "account":
        request = replace(
            request, lines=tuple(replace(item, account_id=context[2].accounts[0].id) for item in request.lines)
        )
    elif failure == "source":
        request = replace(request, source_reference_ids=(uuid4(),))
    else:
        other = AccountingLedger(uuid4(), context[0].chart_id)
        register_accounting_ledger(other, FakeUnitOfWork(store))
        other_period = period(other.id)
        register_accounting_period(other_period, FakeUnitOfWork(store))
        request = replace(request, ledger_id=other.id, period_id=other_period.id)
    uow = FakeUnitOfWork(store)
    with pytest.raises(InvalidJournalEntry):
        post_journal_entry(request, uow)
    assert uow.commit_count == 0
    with FakeUnitOfWork(store) as reader:
        assert reader.journals.get(request.id) is None


@pytest.mark.parametrize("identity", ["uuid", "instruction"])
def test_duplicate_identity_is_rejected(registered, identity):
    store, context = registered
    request = command(context)
    post_journal_entry(request, FakeUnitOfWork(store))
    duplicate = (
        replace(request, posting_instruction_key="EXAMPLE-OTHER")
        if identity == "uuid"
        else replace(request, id=uuid4())
    )
    with pytest.raises(DuplicateJournalEntry):
        post_journal_entry(duplicate, FakeUnitOfWork(store))


@pytest.mark.parametrize("failure", ["staging", "commit"])
def test_failures_roll_back_everything(registered, failure):
    store, context = registered
    request = command(context)

    class FailingUow(FakeUnitOfWork):
        def __enter__(self):
            super().__enter__()
            if failure == "staging":
                add = self.journals.add

                def fail(result):
                    add(result)
                    raise RuntimeError("synthetic staging failure")

                self.journals.add = fail
            return self

        def commit(self):
            raise RuntimeError("synthetic commit failure")

    uow = FailingUow(store)
    with pytest.raises(RuntimeError):
        post_journal_entry(request, uow)
    assert uow.commit_count == 0
    with FakeUnitOfWork(store) as reader:
        assert reader.journals.get(request.id) is None
        assert reader.charts.protected_through(context[2].id) is None


def test_period_overlap_and_unauthorized_cross_ledger_attribution(registered):
    store, context = registered
    ledger, calendar, _, source = context
    with pytest.raises(RepositoryConflict):
        register_accounting_period(replace(calendar, id=uuid4()), FakeUnitOfWork(store))
    other = AccountingLedger(uuid4(), ledger.chart_id)
    register_accounting_ledger(other, FakeUnitOfWork(store))
    with pytest.raises(RepositoryConflict):
        attribute_ledger_source(replace(source, id=uuid4(), ledger_id=other.id), FakeUnitOfWork(store))
    with pytest.raises(InvalidJournalEntry):
        attribute_ledger_source(replace(source, id=uuid4(), receipt_id=uuid4()), FakeUnitOfWork(store))


def test_supersession_reads_real_posted_protection(registered):
    store, context = registered
    result = post_journal_entry(command(context), FakeUnitOfWork(store))
    request = SupersedeChartVersion(
        transition_id=uuid4(),
        predecessor_id=context[2].id,
        successor=version(context[2].chart_id, label="EXAMPLE-V2", start=result.effective_date),
        actor_ref="EXAMPLE",
        reason="EXAMPLE",
    )
    with pytest.raises(ValueError):
        supersede_chart_version(request, FakeUnitOfWork(store))


def test_fake_stale_journal_transaction_cannot_erase_another_commit(registered):
    from journal_fixtures import entry

    store, context = registered
    ledger, calendar, snapshot, source = context
    with FakeUnitOfWork(store) as stale:
        stale.journals.add(entry(snapshot, ledger, calendar, source.id))
        winner = post_journal_entry(command(context), FakeUnitOfWork(store))
        with pytest.raises(RepositoryConflict):
            stale.commit()
    with FakeUnitOfWork(store) as reader:
        assert reader.journals.get(winner.id) == winner


def test_chart_then_period_lock_precedes_refreshed_validation(registered):
    store, context = registered
    events = []

    class ObservedUow(FakeUnitOfWork):
        def __enter__(self):
            super().__enter__()
            for repository, name, label in (
                (self.charts, "get_chart_for_update", "chart"),
                (self.ledgers, "get_period_for_update", "period"),
                (self.charts, "get_version", "version"),
            ):
                original = getattr(repository, name)

                def observed(key, original=original, label=label):
                    events.append(label)
                    return original(key)

                setattr(repository, name, observed)
            return self

    post_journal_entry(command(context), ObservedUow(store))
    assert events == ["chart", "period", "version"]
