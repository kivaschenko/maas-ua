from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest
from accounting_fixtures import NOW, chart, version
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from maas_ua.adapters.accounting_orm import AccountDefinitionRecord, ChartSupersessionRecord, ChartVersionRecord
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.domain.accounting import AccountingDimensions, ChartVersionConflict, InvalidAccountingReference
from maas_ua.service_layer.accounting import (
    SupersedeChartVersion,
    register_chart,
    register_chart_version,
    supersede_chart_version,
    validate_accounting_dimensions,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def registered(session_factory):
    scope = chart()
    first = version(scope.id)
    register_chart(scope, SqlAlchemyUnitOfWork(session_factory))
    register_chart_version(first, SqlAlchemyUnitOfWork(session_factory))
    return scope, first


def command(scope, first):
    return SupersedeChartVersion(
        transition_id=uuid4(),
        predecessor_id=first.id,
        successor=version(scope.id, label="EXAMPLE-V2", start=date(2026, 7, 1)),
        actor_ref="EXAMPLE-ACTOR",
        reason="EXAMPLE approved replacement",
    )


def test_persist_reload_reference_snapshot_and_explicit_local_mapping(registered, session_factory):
    scope, first = registered
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.charts.get_chart(scope.id) == scope
        assert reader.charts.get_version(first.id) == first
        assert reader.charts.versions_for_chart(scope.id) == (first,)
        assert reader.charts.get_account(first.accounts[0].id) == first.accounts[0]
        assert reader.charts.get_account(uuid4()) is None
        assert reader.charts.version_as_of(scope.id, date.max) == first
        assert reader.charts.version_as_of(scope.id, date(2025, 12, 31)) is None
    dimensions = AccountingDimensions(local_accounting_analytics=first.accounts[0].id)
    assert validate_accounting_dimensions(dimensions, first.id, SqlAlchemyUnitOfWork(session_factory)) == dimensions


def test_supersession_preserves_historical_definitions_and_audit(registered, session_factory):
    scope, first = registered
    request = command(scope, first)
    result = supersede_chart_version(request, SqlAlchemyUnitOfWork(session_factory), clock=lambda: NOW)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.charts.get_version(first.id) == result.closed_version
        assert reader.charts.get_version(request.successor.id) == request.successor
        assert reader.charts.get_supersession(request.transition_id) == result.audit
        assert reader.charts.get_account(first.accounts[0].id) == first.accounts[0]
        assert reader.charts.version_as_of(scope.id, date(2026, 6, 30)) == result.closed_version
        assert reader.charts.version_as_of(scope.id, date(2026, 7, 1)) == request.successor
    with pytest.raises(ChartVersionConflict):
        supersede_chart_version(request, SqlAlchemyUnitOfWork(session_factory))


@pytest.mark.parametrize("failure", ["before_commit", "after_flush", "identity_conflict"])
def test_supersession_failure_rolls_back_closure_successor_and_audit(registered, session_factory, failure):
    scope, first = registered
    request = command(scope, first)
    if failure == "identity_conflict":
        # Reusing any historical definition UUID, even under another code, is forbidden.
        request = replace(
            request,
            successor=replace(
                request.successor,
                accounts=(
                    replace(request.successor.accounts[0], id=first.accounts[0].id),
                    request.successor.accounts[1],
                ),
            ),
        )

    class FailingUow(SqlAlchemyUnitOfWork):
        def commit(self):
            if failure == "after_flush":
                self.session.flush()
            if failure != "identity_conflict":
                raise RuntimeError("synthetic commit failure")
            super().commit()

    with pytest.raises((InvalidAccountingReference, RuntimeError)):
        supersede_chart_version(request, FailingUow(session_factory))
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.charts.get_version(first.id) == first
        assert reader.charts.get_version(request.successor.id) is None
        assert reader.charts.get_supersession(request.transition_id) is None


@pytest.mark.parametrize(
    "failure",
    [
        "chart_fk",
        "parent_fk",
        "mapping_fk",
        "duplicate_code",
        "classification",
        "mapping_kind",
        "validity",
        "label",
        "overlap",
    ],
)
def test_database_reference_constraints(registered, session_factory, failure):
    scope, first = registered
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            if failure == "chart_fk":
                session.execute(text("UPDATE accounting_chart_versions SET chart_id=:id"), {"id": uuid4()})
            elif failure in {"parent_fk", "mapping_fk"}:
                column = "parent_id" if failure == "parent_fk" else "official_account_id"
                session.execute(
                    text(f"UPDATE accounting_account_definitions SET {column}=:id WHERE kind='LOCAL_ANALYTICAL'"),
                    {"id": uuid4()},
                )
            elif failure == "duplicate_code":
                session.execute(
                    text("UPDATE accounting_account_definitions SET code='1518' WHERE kind='LOCAL_ANALYTICAL'")
                )
            elif failure == "classification":
                session.execute(text("UPDATE accounting_account_definitions SET basis='UNKNOWN'"))
            elif failure == "mapping_kind":
                session.execute(
                    text(
                        "UPDATE accounting_account_definitions SET official_account_id=NULL WHERE kind='LOCAL_ANALYTICAL'"
                    )
                )
            elif failure == "validity":
                session.execute(text("UPDATE accounting_chart_versions SET valid_to=valid_from"))
            else:
                candidate = version(scope.id, label=first.label if failure == "label" else "EXAMPLE-OVERLAP")
                record = ChartVersionRecord(
                    id=candidate.id,
                    chart_id=scope.id,
                    label=candidate.label,
                    valid_from=candidate.valid_from,
                    valid_to=candidate.valid_to,
                    approval_reference=candidate.approval_reference,
                    created_at=NOW,
                )
                session.add(record)
                session.flush()
            session.commit()  # Deferred same-version FKs are checked at commit.


def test_same_version_foreign_keys_reject_cross_version_parent_and_mapping(registered, session_factory):
    scope, first = registered
    successor = command(scope, first)
    supersede_chart_version(successor, SqlAlchemyUnitOfWork(session_factory))
    for column in ("parent_id", "official_account_id"):
        with session_factory() as session:
            session.execute(
                text(f"UPDATE accounting_account_definitions SET {column}=:old WHERE id=:new"),
                {"old": first.accounts[1].id, "new": successor.successor.accounts[0].id},
            )
            with pytest.raises(IntegrityError):
                session.commit()


def test_exact_case_sensitive_codes_and_code_reuse_in_disjoint_versions(session_factory):
    scope = chart()
    first = version(scope.id, end=date(2027, 1, 1))
    extra = replace(first.accounts[1], id=uuid4(), code="1518a")
    first = replace(first, accounts=(*first.accounts, extra, replace(extra, id=uuid4(), code="1518A")))
    second = version(scope.id, label="EXAMPLE-V2", start=date(2027, 1, 1))
    register_chart(scope, SqlAlchemyUnitOfWork(session_factory))
    register_chart_version(first, SqlAlchemyUnitOfWork(session_factory))
    register_chart_version(second, SqlAlchemyUnitOfWork(session_factory))
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.charts.get_version(first.id) == first
        assert reader.charts.get_version(second.id) == second
    with session_factory() as session:
        for code in ("1518a", "1518A"):
            assert (
                session.scalars(
                    select(AccountDefinitionRecord).where(
                        AccountDefinitionRecord.version_id == first.id, AccountDefinitionRecord.code == code
                    )
                )
                .one()
                .code
                == code
            )


def test_repositories_can_read_staged_snapshots_without_flushing(session_factory):
    scope = chart()
    first = version(scope.id)
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.charts.add_chart(scope)
        writer.charts.add_version(first)
        assert writer.charts.get_chart(scope.id) == scope
        assert writer.charts.get_version(first.id) == first
        assert writer.charts.get_account(first.accounts[0].id) == first.accounts[0]
        assert len(writer.session.new) == 4
        writer.rollback()


def _wait_until_blocked(session_factory, backend, started, future):
    from threading import Event
    from time import monotonic

    assert started.wait(5)
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with session_factory() as observer:
            blocked = observer.scalar(text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": backend[0]})
        if blocked:
            assert not future.done()
            return
        Event().wait(0.01)
    pytest.fail("competing accounting transaction did not wait in PostgreSQL")


@pytest.mark.parametrize("via_service", [True, False])
def test_concurrent_registration_cannot_create_overlapping_versions(session_factory, via_service):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    scope = chart()
    register_chart(scope, SqlAlchemyUnitOfWork(session_factory))
    first_version = version(scope.id)
    second_version = version(scope.id, label="EXAMPLE-COMPETING")
    started = Event()
    backend = []

    class ObservedUow(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            backend.append(self.session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            return self

    with ThreadPoolExecutor(max_workers=1) as pool:
        with SqlAlchemyUnitOfWork(session_factory) as first:
            if via_service:
                first.charts.get_chart_for_update(scope.id)
            first.charts.add_version(first_version)
            if not via_service:
                first.session.flush()  # Exclusion must also defend callers bypassing chart locks/prechecks.
            second = pool.submit(register_chart_version, second_version, ObservedUow(session_factory))
            try:
                _wait_until_blocked(session_factory, backend, started, second)
            finally:
                first.commit()
        with pytest.raises(ChartVersionConflict):
            second.result(timeout=5)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.charts.versions_for_chart(scope.id) == (first_version,)


@pytest.mark.parametrize("competing", ["supersede", "duplicate_transition", "register"])
def test_concurrent_supersession_waits_and_revalidates_history(registered, session_factory, competing):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    scope, first_version = registered
    request = command(scope, first_version)
    started = Event()
    backend = []

    class ObservedUow(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            backend.append(self.session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            return self

    from maas_ua.domain.accounting import supersede_version

    with ThreadPoolExecutor(max_workers=1) as pool:
        with SqlAlchemyUnitOfWork(session_factory) as first:
            first.charts.get_chart_for_update(scope.id)
            result = supersede_version(
                first_version,
                request.successor,
                id=request.transition_id,
                actor_ref=request.actor_ref,
                reason=request.reason,
                created_at=NOW,
                protected_through=None,
            )
            first.charts.apply_supersession(result)
            if competing == "register":
                future = pool.submit(
                    register_chart_version,
                    version(scope.id, label="EXAMPLE-COMPETING", start=date(2026, 8, 1)),
                    ObservedUow(session_factory),
                )
            else:
                other = (
                    request
                    if competing == "duplicate_transition"
                    else replace(
                        command(scope, first_version),
                        successor=version(scope.id, label="EXAMPLE-COMPETING", start=date(2026, 8, 1)),
                    )
                )
                future = pool.submit(supersede_chart_version, other, ObservedUow(session_factory))
            try:
                _wait_until_blocked(session_factory, backend, started, future)
            finally:
                first.commit()
        with pytest.raises(InvalidAccountingReference):
            future.result(timeout=5)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.charts.versions_for_chart(scope.id) == (result.closed_version, request.successor)
        assert reader.charts.get_supersession(request.transition_id) == result.audit
        assert reader.charts.get_account(first_version.accounts[0].id) == first_version.accounts[0]
    with session_factory() as session:
        assert len(session.scalars(select(ChartSupersessionRecord)).all()) == 1


def test_database_identity_conflict_after_closure_rolls_back_everything(registered, session_factory):
    scope, first = registered
    other_chart = chart()
    other = version(other_chart.id)
    register_chart(other_chart, SqlAlchemyUnitOfWork(session_factory))
    register_chart_version(other, SqlAlchemyUnitOfWork(session_factory))
    request = command(scope, first)
    accounts = request.successor.accounts
    request = replace(
        request,
        successor=replace(request.successor, accounts=(replace(accounts[0], id=other.accounts[0].id), accounts[1])),
    )
    with pytest.raises(ChartVersionConflict):
        supersede_chart_version(request, SqlAlchemyUnitOfWork(session_factory))
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.charts.get_version(first.id) == first
        assert reader.charts.get_version(other.id) == other
        assert reader.charts.get_version(request.successor.id) is None
        assert reader.charts.get_supersession(request.transition_id) is None


def test_account_references_and_audit_cannot_cross_chart_boundaries(registered, session_factory):
    scope, first = registered
    other_chart = chart()
    other = version(other_chart.id)
    register_chart(other_chart, SqlAlchemyUnitOfWork(session_factory))
    register_chart_version(other, SqlAlchemyUnitOfWork(session_factory))
    for values in (
        dict(chart_id=scope.id, predecessor_id=first.id, successor_id=other.id),
        dict(chart_id=scope.id, predecessor_id=uuid4(), successor_id=first.id),
    ):
        with session_factory() as session:
            session.add(
                ChartSupersessionRecord(
                    id=uuid4(),
                    effective_date=date(2026, 7, 1),
                    actor_ref="EXAMPLE-ACTOR",
                    reason="EXAMPLE",
                    created_at=NOW,
                    **values,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()


def test_off_balance_metadata_and_posting_permission_round_trip(session_factory):
    from maas_ua.domain.accounting import AccountBasis

    scope = chart()
    snapshot = version(scope.id)
    snapshot = replace(
        snapshot,
        accounts=tuple(
            replace(item, basis=AccountBasis.OFF_BALANCE, posting_allowed=not item.posting_allowed)
            for item in snapshot.accounts
        ),
    )
    register_chart(scope, SqlAlchemyUnitOfWork(session_factory))
    register_chart_version(snapshot, SqlAlchemyUnitOfWork(session_factory))
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.charts.get_version(snapshot.id) == snapshot
        assert all(item.basis is AccountBasis.OFF_BALANCE for item in reader.charts.get_version(snapshot.id).accounts)


def test_supersession_can_be_followed_by_another_replacement(registered, session_factory):
    scope, original = registered
    second = supersede_chart_version(command(scope, original), SqlAlchemyUnitOfWork(session_factory))
    third = replace(
        command(scope, second.successor), successor=version(scope.id, label="EXAMPLE-V3", start=date(2027, 1, 1))
    )
    result = supersede_chart_version(third, SqlAlchemyUnitOfWork(session_factory))
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.charts.get_version(original.id) == second.closed_version
        assert reader.charts.get_version(second.successor.id) == result.closed_version
        assert reader.charts.version_as_of(scope.id, date(2027, 1, 1)) == third.successor
        assert reader.charts.get_account(original.accounts[0].id) == original.accounts[0]
        assert reader.charts.get_account(second.successor.accounts[0].id) == second.successor.accounts[0]
