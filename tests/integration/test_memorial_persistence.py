from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest
from accounting_fixtures import NOW
from journal_fixtures import command, period, register_context
from memorial_fixtures import assignment, create_command, finalization, numbering_scope, order, order_type
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from maas_ua.adapters.journal_orm import JournalEntryRecord
from maas_ua.adapters.memorial_orm import (
    MemorialOrderMembershipRecord,
    membership_to_record,
    order_to_record,
)
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.domain.journal import AccountingLedger
from maas_ua.domain.memorial_orders import (
    DuplicateMemorialOrder,
    DuplicatePrimaryMembership,
    InvalidMemorialOrder,
    MemorialOrderNumber,
    MemorialOrderStatus,
)
from maas_ua.ports import RepositoryConflict
from maas_ua.service_layer.journal import post_journal_entry, register_accounting_ledger, register_accounting_period
from maas_ua.service_layer.memorial_orders import (
    assign_journal_entries,
    create_memorial_order,
    finalize_memorial_order,
    register_memorial_order_type,
    register_numbering_scope,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def registered(session_factory, write_off_receipt):
    def make():
        return SqlAlchemyUnitOfWork(session_factory)

    context = register_context(make, write_off_receipt)
    kind, scope = order_type(), numbering_scope(context[0].id)
    register_memorial_order_type(kind, make())
    register_numbering_scope(scope, make())
    register = create_memorial_order(create_command(order(*context[:2], kind, scope)), make(), clock=lambda: NOW)
    journals = tuple(
        post_journal_entry(command(context, posting_instruction_key=f"EXAMPLE-{n}"), make()) for n in range(3)
    )
    return context, register, journals


def test_round_trip_assignment_finalization_and_posting_remains_independent(registered, session_factory):
    context, register, journals = registered

    def make():
        return SqlAlchemyUnitOfWork(session_factory)

    assigned = assign_journal_entries(assignment(register, journals), make(), clock=lambda: NOW)
    final = finalize_memorial_order(finalization(register), make(), clock=lambda: NOW)
    with make() as reader:
        restored = reader.memorial_orders.get(register.id)
        assert restored.status is MemorialOrderStatus.FINALIZED
        assert set(restored.memberships) == set(assigned.memberships)
        assert restored.number == register.number and restored.finalized_at == final.finalized_at
        assert reader.journals.get(journals[0].id) == journals[0]
        assert reader.ledgers.get_period(context[1].id).posting_allowed
    late = post_journal_entry(command(context, posting_instruction_key="EXAMPLE-LATER"), make())
    with make() as reader:
        assert reader.memorial_orders.order_for_entry(late.id) is None


@pytest.mark.parametrize("failure", ["missing", "coverage", "period", "ledger"])
def test_direct_membership_inserts_require_existing_matching_postings(registered, session_factory, failure):
    context, register, journals = registered

    def make():
        return SqlAlchemyUnitOfWork(session_factory)

    target = journals[0]
    if failure == "coverage":
        target = post_journal_entry(
            command(context, posting_instruction_key="EXAMPLE-JAN", effective_date=date(2026, 1, 31)), make()
        )
    elif failure == "period":
        other_period = period(context[0].id, starts_on=date(2027, 1, 1), ends_before=date(2028, 1, 1))
        register_accounting_period(other_period, make())
        target = post_journal_entry(
            command(
                context,
                period_id=other_period.id,
                effective_date=date(2027, 2, 1),
                posting_instruction_key="EXAMPLE-OTHER-PERIOD",
            ),
            make(),
        )
    elif failure == "ledger":
        other = AccountingLedger(uuid4(), context[0].chart_id)
        register_accounting_ledger(other, make())
        scope = numbering_scope(other.id)
        register_numbering_scope(scope, make())
        calendar = period(other.id)
        register_accounting_period(calendar, make())
        register = create_memorial_order(
            create_command(order(other, calendar, scope=scope, kind=order_type(id=register.type_id))), make()
        )
    member = (
        register.assign(
            (journals[0],), actor_ref="EXAMPLE", authorization_reference="EXAMPLE", assigned_at=NOW
        ).memberships[0]
        if failure != "ledger"
        else None
    )
    from maas_ua.domain.memorial_orders import MemorialOrderMembership

    member = (
        replace(member, entry_id=uuid4() if failure == "missing" else target.id)
        if member
        else MemorialOrderMembership(
            entry_id=target.id, actor_ref="EXAMPLE", authorization_reference="EXAMPLE", assigned_at=NOW
        )
    )
    with session_factory() as session:
        session.add(membership_to_record(register, member))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize(
    "table",
    [
        "accounting_memorial_order_types",
        "accounting_memorial_numbering_scopes",
        "accounting_memorial_orders",
        "accounting_memorial_order_memberships",
    ],
)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_ordinary_mutation_cannot_rewrite_register_history(registered, session_factory, table, operation):
    _, register, journals = registered
    assign_journal_entries(assignment(register, journals), SqlAlchemyUnitOfWork(session_factory))
    finalize_memorial_order(finalization(register), SqlAlchemyUnitOfWork(session_factory))
    sql = f"{operation} {table}"
    if operation == "UPDATE":
        sql += " SET actor_ref = 'EXAMPLE-CHANGED'"
    if operation == "DELETE":
        sql = f"DELETE FROM {table}"
    if operation == "TRUNCATE":
        sql += " CASCADE"  # Reach our guard rather than PostgreSQL's FK precheck.
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(text(sql))


@pytest.mark.parametrize(
    "failure", ["identity", "coverage", "empty_final", "insert_final", "missing_evidence", "reopen", "append_final"]
)
def test_database_guards_register_identity_lifecycle_and_finalized_membership(registered, session_factory, failure):
    _, register, journals = registered

    def make():
        return SqlAlchemyUnitOfWork(session_factory)

    if failure in {"reopen", "append_final"}:
        assign_journal_entries(assignment(register, journals[:1]), make())
        finalize_memorial_order(finalization(register), make())
    with session_factory() as session:
        if failure == "identity":
            record = order_to_record(replace(register, id=uuid4()))
            session.add(record)
            with pytest.raises(IntegrityError):
                session.commit()
            return
        if failure in {"coverage", "insert_final"}:
            other = replace(register, id=uuid4(), number=MemorialOrderNumber(register.number.scope_id, "EXAMPLE-OTHER"))
            record = order_to_record(other)
            if failure == "coverage":
                record.coverage_end = date(2028, 1, 1)
            else:
                record.status, record.finalized_by, record.finalization_reference, record.finalized_at = (
                    "FINALIZED",
                    "EXAMPLE",
                    "EXAMPLE",
                    NOW,
                )
            session.add(record)
            with pytest.raises(IntegrityError):
                session.commit()
            return
        if failure == "append_final":
            member = register.assign(
                journals[1:], actor_ref="EXAMPLE", authorization_reference="EXAMPLE", assigned_at=NOW
            ).memberships[0]
            session.add(membership_to_record(register, member))
            with pytest.raises(IntegrityError):
                session.commit()
            return
        sql = "UPDATE accounting_memorial_orders SET status = 'FINALIZED', finalized_by = 'EXAMPLE', finalization_reference = 'EXAMPLE', finalized_at = :now WHERE id = :id"
        if failure == "missing_evidence":
            sql = "UPDATE accounting_memorial_orders SET status = 'FINALIZED' WHERE id = :id"
        if failure == "reopen":
            sql = "UPDATE accounting_memorial_orders SET status = 'OPEN', finalized_by = NULL, finalization_reference = NULL, finalized_at = NULL WHERE id = :id"
        with pytest.raises(IntegrityError):
            session.execute(text(sql), {"id": register.id, "now": NOW})


def test_assignment_failure_after_flush_rolls_back_entire_batch(registered, session_factory):
    _, register, journals = registered

    class FailingUow(SqlAlchemyUnitOfWork):
        def commit(self):
            self.session.flush()
            raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError):
        assign_journal_entries(assignment(register, journals), FailingUow(session_factory))
    with session_factory() as session:
        assert session.scalar(select(MemorialOrderMembershipRecord.entry_id)) is None
        assert len(session.scalars(select(JournalEntryRecord.id)).all()) == 3


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_register_writes_require_read_committed(registered, session_factory, isolation):
    _, register, journals = registered
    with session_factory() as session:
        session.connection(execution_options={"isolation_level": isolation})
        member = register.assign(
            journals, actor_ref="EXAMPLE", authorization_reference="EXAMPLE", assigned_at=NOW
        ).memberships[0]
        session.add(membership_to_record(register, member))
        with pytest.raises(IntegrityError):
            session.commit()


def test_duplicate_primary_membership_and_identity_are_translated(registered, session_factory):
    _, register, journals = registered
    with pytest.raises(DuplicateMemorialOrder):
        create_memorial_order(replace(create_command(register), id=uuid4()), SqlAlchemyUnitOfWork(session_factory))
    assign_journal_entries(assignment(register, journals), SqlAlchemyUnitOfWork(session_factory))
    other = create_memorial_order(
        replace(
            create_command(register), id=uuid4(), number=MemorialOrderNumber(register.number.scope_id, "EXAMPLE-OTHER")
        ),
        SqlAlchemyUnitOfWork(session_factory),
    )
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.memorial_orders.assign(
            other, other.assign(journals, actor_ref="EXAMPLE", authorization_reference="EXAMPLE", assigned_at=NOW)
        )
        with pytest.raises(DuplicatePrimaryMembership):
            writer.commit()


@pytest.mark.parametrize("finalize_first", [False, True])
def test_concurrent_assignment_and_finalization_refresh_state(registered, session_factory, finalize_first):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_accounting_reference_persistence import _wait_until_blocked

    _, register, journals = registered
    assign_journal_entries(assignment(register, journals[:1]), SqlAlchemyUnitOfWork(session_factory))
    started, backend = Event(), []

    class ObservedUow(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            self.memorial_orders.get(register.id)  # Prime stale ORM header and children deliberately.
            backend.append(self.session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            return self

    with ThreadPoolExecutor(max_workers=1) as pool:
        with SqlAlchemyUnitOfWork(session_factory) as first:
            previous = first.memorial_orders.get_for_update(register.id)
            if finalize_first:
                first.memorial_orders.finalize(
                    previous,
                    previous.finalize(actor_ref="EXAMPLE", authorization_reference="EXAMPLE", finalized_at=NOW),
                )
                future = pool.submit(
                    assign_journal_entries, assignment(register, journals[1:]), ObservedUow(session_factory)
                )
            else:
                first.memorial_orders.assign(
                    previous,
                    previous.assign(
                        journals[1:], actor_ref="EXAMPLE", authorization_reference="EXAMPLE", assigned_at=NOW
                    ),
                )
                future = pool.submit(finalize_memorial_order, finalization(register), ObservedUow(session_factory))
            try:
                _wait_until_blocked(session_factory, backend, started, future)
            finally:
                first.commit()
        if finalize_first:
            with pytest.raises(InvalidMemorialOrder):
                future.result(timeout=5)
        else:
            assert len(future.result(timeout=5).memberships) == 3
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        final = reader.memorial_orders.get(register.id)
        assert final.status is MemorialOrderStatus.FINALIZED
        assert len(final.memberships) == (1 if finalize_first else 3)


def test_register_lock_timeout_requires_complete_command_retry(registered, session_factory):
    _, register, journals = registered
    with SqlAlchemyUnitOfWork(session_factory) as first:
        first.memorial_orders.get_for_update(register.id)

        class TimeoutUow(SqlAlchemyUnitOfWork):
            def __enter__(self):
                super().__enter__()
                self.session.execute(text("SET LOCAL lock_timeout = '100ms'"))
                return self

        with pytest.raises(RepositoryConflict):
            assign_journal_entries(assignment(register, journals), TimeoutUow(session_factory))


def test_staged_references_memberships_and_finalization_are_readable_without_flush(registered, session_factory):
    _, register, journals = registered
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        previous = writer.memorial_orders.get_for_update(register.id)
        assigned = previous.assign(journals, actor_ref="EXAMPLE", authorization_reference="EXAMPLE", assigned_at=NOW)
        writer.memorial_orders.assign(previous, assigned)
        assert set(writer.memorial_orders.get(register.id).memberships) == set(assigned.memberships)
        assert writer.memorial_orders.order_for_entry(journals[0].id) == register.id
        assert not writer.session.dirty
    assign_journal_entries(assignment(register, journals), SqlAlchemyUnitOfWork(session_factory))
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        previous = writer.memorial_orders.get_for_update(register.id)
        final = previous.finalize(actor_ref="EXAMPLE", authorization_reference="EXAMPLE", finalized_at=NOW)
        writer.memorial_orders.finalize(previous, final)
        assert writer.memorial_orders.get(register.id).status is MemorialOrderStatus.FINALIZED
        assert writer.session.dirty  # Reading must not flush or discard the staged transition.


@pytest.mark.parametrize("same_uuid", [False, True])
def test_concurrent_register_identity_without_prechecks(registered, session_factory, same_uuid):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_accounting_reference_persistence import _wait_until_blocked

    _, register, _ = registered
    first_order = replace(register, id=uuid4(), number=MemorialOrderNumber(register.number.scope_id, "EXAMPLE-RACE"))
    second_order = first_order if same_uuid else replace(first_order, id=uuid4())
    started, backend = Event(), []

    def competing():
        with SqlAlchemyUnitOfWork(session_factory) as writer:
            backend.append(writer.session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            writer.memorial_orders.add(second_order)
            writer.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with SqlAlchemyUnitOfWork(session_factory) as first:
            first.memorial_orders.add(first_order)
            first.session.flush()
            future = pool.submit(competing)
            try:
                _wait_until_blocked(session_factory, backend, started, future)
            finally:
                first.commit()
        with pytest.raises(DuplicateMemorialOrder):
            future.result(timeout=5)


def test_concurrent_primary_assignment_across_two_orders_uses_database_uniqueness(registered, session_factory):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_accounting_reference_persistence import _wait_until_blocked

    _, register, journals = registered
    other = create_memorial_order(
        replace(
            create_command(register), id=uuid4(), number=MemorialOrderNumber(register.number.scope_id, "EXAMPLE-OTHER")
        ),
        SqlAlchemyUnitOfWork(session_factory),
    )
    started, backend = Event(), []

    class ObservedUow(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            backend.append(self.session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            return self

    with ThreadPoolExecutor(max_workers=1) as pool:
        with SqlAlchemyUnitOfWork(session_factory) as first:
            previous = first.memorial_orders.get_for_update(register.id)
            assigned = previous.assign(
                journals, actor_ref="EXAMPLE", authorization_reference="EXAMPLE", assigned_at=NOW
            )
            first.memorial_orders.assign(previous, assigned)
            first.session.flush()
            future = pool.submit(assign_journal_entries, assignment(other, journals), ObservedUow(session_factory))
            try:
                _wait_until_blocked(session_factory, backend, started, future)
            finally:
                first.commit()
        with pytest.raises(DuplicatePrimaryMembership):
            future.result(timeout=5)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.memorial_orders.get(other.id).memberships == ()


@pytest.mark.parametrize("finalize_first", [False, True])
def test_direct_sql_assignment_and_finalization_share_parent_lock(registered, session_factory, finalize_first):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_accounting_reference_persistence import _wait_until_blocked

    _, register, journals = registered
    assign_journal_entries(assignment(register, journals[:1]), SqlAlchemyUnitOfWork(session_factory))
    started, backend = Event(), []
    member = register.assign(
        journals[1:], actor_ref="EXAMPLE", authorization_reference="EXAMPLE", assigned_at=NOW
    ).memberships[0]
    update = text("""UPDATE accounting_memorial_orders SET status = 'FINALIZED', finalized_by = 'EXAMPLE',
        finalization_reference = 'EXAMPLE', finalized_at = :now WHERE id = :id""")

    def competing():
        with session_factory() as session:
            backend.append(session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            if finalize_first:
                session.add(membership_to_record(register, member))
            else:
                session.execute(update, {"id": register.id, "now": NOW})
            session.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with session_factory() as first:
            if finalize_first:
                first.execute(update, {"id": register.id, "now": NOW})
            else:
                first.add(membership_to_record(register, member))
                first.flush()
            future = pool.submit(competing)
            try:
                _wait_until_blocked(session_factory, backend, started, future)
            finally:
                first.commit()
        if finalize_first:
            with pytest.raises(IntegrityError):
                future.result(timeout=5)
        else:
            future.result(timeout=5)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        final = reader.memorial_orders.get(register.id)
        assert final.status is MemorialOrderStatus.FINALIZED
        assert len(final.memberships) == (1 if finalize_first else 2)
