from dataclasses import replace
from uuid import uuid4

import pytest
from accounting_fixtures import NOW
from journal_fixtures import command, register_context
from memorial_fixtures import assignment, create_command, finalization, numbering_scope, order, order_type

from maas_ua.adapters.memory import FakeStore, FakeUnitOfWork
from maas_ua.domain.memorial_orders import (
    DuplicateMemorialOrder,
    DuplicatePrimaryMembership,
    InvalidMemorialOrder,
    MemorialOrderNumber,
    MemorialOrderStatus,
)
from maas_ua.ports import RepositoryConflict
from maas_ua.service_layer.journal import post_journal_entry
from maas_ua.service_layer.memorial_orders import (
    assign_journal_entries,
    create_memorial_order,
    finalize_memorial_order,
    register_memorial_order_type,
    register_numbering_scope,
)


@pytest.fixture
def registered(write_off_receipt):
    store = FakeStore()

    def make():
        return FakeUnitOfWork(store)

    context = register_context(make, write_off_receipt)
    kind, scope = order_type(), numbering_scope(context[0].id)
    register_memorial_order_type(kind, make())
    register_numbering_scope(scope, make())
    register = create_memorial_order(create_command(order(*context[:2], kind, scope)), make(), clock=lambda: NOW)
    return store, context, register


def test_creation_assignment_and_finalization_commit_once(registered):
    store, context, register = registered

    def make():
        return FakeUnitOfWork(store)

    journals = tuple(
        post_journal_entry(command(context, posting_instruction_key=f"EXAMPLE-{n}"), make()) for n in range(3)
    )
    writer = make()
    assigned = assign_journal_entries(assignment(register, journals), writer, clock=lambda: NOW)
    assert writer.commit_count == 1 and len(assigned.memberships) == 3
    writer = make()
    final = finalize_memorial_order(finalization(register), writer, clock=lambda: NOW)
    assert writer.commit_count == 1 and final.status is MemorialOrderStatus.FINALIZED
    with make() as reader:
        assert reader.memorial_orders.get(register.id) == final
        assert reader.ledgers.get_period(context[1].id).posting_allowed
        assert reader.journals.get(journals[0].id) == journals[0]


@pytest.mark.parametrize("failure", ["missing", "duplicate", "other_order", "finalized"])
def test_assignment_failures_publish_no_partial_batch(registered, failure):
    store, context, register = registered

    def make():
        return FakeUnitOfWork(store)

    journal = post_journal_entry(command(context), make())
    request = assignment(register, (journal,))
    if failure == "missing":
        request = replace(request, entry_ids=(journal.id, uuid4()))
    if failure == "duplicate":
        request = replace(request, entry_ids=(journal.id, journal.id))
    if failure in {"other_order", "finalized"}:
        assign_journal_entries(request, make())
        if failure == "other_order":
            other = create_memorial_order(
                replace(
                    create_command(register),
                    id=uuid4(),
                    number=MemorialOrderNumber(register.number.scope_id, "EXAMPLE-002"),
                ),
                make(),
            )
            request = replace(request, order_id=other.id)
        else:
            finalize_memorial_order(finalization(register), make())
            second = post_journal_entry(command(context, posting_instruction_key="EXAMPLE-NEW"), make())
            request = assignment(register, (second,))
    writer = make()
    with pytest.raises(InvalidMemorialOrder):
        assign_journal_entries(request, writer)
    assert writer.commit_count == 0
    with make() as reader:
        target = reader.memorial_orders.get(request.order_id)
        assert len(target.memberships) == (1 if failure == "finalized" else 0)


def test_duplicate_number_namespace_and_type_identities(registered):
    store, _, register = registered

    def make():
        return FakeUnitOfWork(store)

    with pytest.raises(DuplicateMemorialOrder):
        create_memorial_order(replace(create_command(register), id=uuid4()), make())
    with make() as reader:
        scope = reader.memorial_orders.get_scope(register.number.scope_id)
        kind = reader.memorial_orders.get_type(register.type_id)
    with pytest.raises(DuplicateMemorialOrder):
        register_numbering_scope(replace(scope, id=uuid4()), make())
    with pytest.raises(DuplicateMemorialOrder):
        register_memorial_order_type(replace(kind, id=uuid4()), make())


@pytest.mark.parametrize("field", ["type_id", "ledger_id", "period_id", "number"])
def test_creation_rejects_unregistered_references(registered, field):
    store, _, register = registered
    value = MemorialOrderNumber(uuid4(), "EXAMPLE") if field == "number" else uuid4()
    with pytest.raises(InvalidMemorialOrder):
        create_memorial_order(replace(create_command(register), id=uuid4(), **{field: value}), FakeUnitOfWork(store))


@pytest.mark.parametrize("failure", ["staging", "commit"])
def test_assignment_failure_rolls_back_and_fake_stale_commit_is_rejected(registered, failure):
    store, context, register = registered
    journal = post_journal_entry(command(context), FakeUnitOfWork(store))

    class FailingUow(FakeUnitOfWork):
        def __enter__(self):
            super().__enter__()
            if failure == "staging":
                original = self.memorial_orders.assign

                def fail(*args):
                    original(*args)
                    raise RuntimeError("injected staging failure")

                self.memorial_orders.assign = fail
            return self

        def commit(self):
            if failure == "commit":
                raise RuntimeError("injected commit failure")
            super().commit()

    with pytest.raises(RuntimeError):
        assign_journal_entries(assignment(register, (journal,)), FailingUow(store))
    with FakeUnitOfWork(store) as reader:
        assert reader.memorial_orders.get(register.id).memberships == ()
    with FakeUnitOfWork(store) as stale:
        assign_journal_entries(assignment(register, (journal,)), FakeUnitOfWork(store))
        with pytest.raises(RepositoryConflict):
            stale.commit()


def test_nonempty_finalization_and_duplicate_primary_membership(registered):
    store, context, register = registered
    with pytest.raises(InvalidMemorialOrder):
        finalize_memorial_order(finalization(register), FakeUnitOfWork(store))
    journal = post_journal_entry(command(context), FakeUnitOfWork(store))
    request = assignment(register, (journal,))
    assign_journal_entries(request, FakeUnitOfWork(store))
    with pytest.raises(DuplicatePrimaryMembership):
        assign_journal_entries(request, FakeUnitOfWork(store))
