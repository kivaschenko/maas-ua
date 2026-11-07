from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest
from accounting_fixtures import NOW, chart, version

from maas_ua.adapters.memory import FakeStore, FakeUnitOfWork
from maas_ua.domain.accounting import AccountingDimensions, ChartVersionConflict, InvalidAccountingReference
from maas_ua.service_layer.accounting import (
    SupersedeChartVersion,
    register_chart,
    register_chart_version,
    supersede_chart_version,
    validate_accounting_dimensions,
)


@pytest.fixture
def registered():
    store = FakeStore()
    scope = chart()
    first = version(scope.id)
    register_chart(scope, FakeUnitOfWork(store))
    register_chart_version(first, FakeUnitOfWork(store))
    return store, scope, first


def command(scope, first):
    return SupersedeChartVersion(
        transition_id=uuid4(),
        predecessor_id=first.id,
        successor=version(scope.id, label="EXAMPLE-V2", start=date(2026, 7, 1)),
        actor_ref="EXAMPLE-ACTOR",
        reason="EXAMPLE approved replacement",
    )


def test_registration_and_historical_lookup_commit_once(registered):
    store, scope, first = registered
    second = version(scope.id, label="EXAMPLE-V2", start=date(2027, 1, 1))
    with pytest.raises(ChartVersionConflict):
        register_chart_version(second, FakeUnitOfWork(store))
    with FakeUnitOfWork(store) as reader:
        assert reader.charts.get_chart(scope.id) == scope
        assert reader.charts.get_version(first.id) == first
        assert reader.charts.version_as_of(scope.id, date(2026, 1, 1)) == first
        assert reader.charts.version_as_of(scope.id, date(2025, 1, 1)) is None
        assert reader.charts.get_account(first.accounts[0].id) == first.accounts[0]
        assert reader.charts.get_version(uuid4()) is None


def test_supersession_is_one_atomic_commit_and_retains_old_definitions(registered):
    store, scope, first = registered
    request = command(scope, first)
    uow = FakeUnitOfWork(store)
    result = supersede_chart_version(request, uow, clock=lambda: NOW)
    assert uow.commit_count == 1
    with FakeUnitOfWork(store) as reader:
        closed = reader.charts.get_version(first.id)
        assert closed == replace(first, valid_to=request.successor.valid_from)
        assert closed.accounts == first.accounts
        assert reader.charts.get_version(request.successor.id) == request.successor
        assert reader.charts.get_supersession(request.transition_id) == result.audit
        assert reader.charts.version_as_of(scope.id, date(2026, 6, 30)) == closed
        assert reader.charts.version_as_of(scope.id, date(2026, 7, 1)) == request.successor
        assert reader.charts.get_account(first.accounts[0].id) == first.accounts[0]
    with pytest.raises(ChartVersionConflict):
        supersede_chart_version(request, FakeUnitOfWork(store))


@pytest.mark.parametrize(
    "failure", ["same_start", "missing_predecessor", "other_chart", "protected", "staging", "commit"]
)
def test_failed_supersession_keeps_everything_unchanged(registered, failure):
    store, scope, first = registered
    request = command(scope, first)
    if failure == "same_start":
        request = replace(request, successor=replace(request.successor, valid_from=first.valid_from))
    elif failure == "missing_predecessor":
        request = replace(request, predecessor_id=uuid4())
    elif failure == "other_chart":
        request = replace(request, successor=replace(request.successor, chart_id=uuid4()))

    class TestedUow(FakeUnitOfWork):
        def __enter__(self):
            super().__enter__()
            if failure == "protected":
                self.charts.protected_through = lambda key: date(2026, 7, 1)
            elif failure == "staging":
                apply = self.charts.apply_supersession

                def fail(result):
                    apply(result)
                    raise RuntimeError("synthetic staging failure")

                self.charts.apply_supersession = fail
            return self

        def commit(self):
            if failure == "commit":
                raise RuntimeError("synthetic commit failure")
            super().commit()

    uow = TestedUow(store)
    with pytest.raises((InvalidAccountingReference, RuntimeError)):
        supersede_chart_version(request, uow)
    assert uow.commit_count == 0
    with FakeUnitOfWork(store) as reader:
        assert reader.charts.get_version(first.id) == first
        assert reader.charts.get_version(request.successor.id) is None
        assert reader.charts.get_supersession(request.transition_id) is None


def test_missing_chart_and_duplicate_codes_cannot_be_registered(registered):
    store, scope, first = registered
    with pytest.raises(InvalidAccountingReference):
        register_chart_version(version(uuid4()), FakeUnitOfWork(store))
    with pytest.raises(ChartVersionConflict):
        register_chart_version(first, FakeUnitOfWork(store))


def test_code_reuse_in_disjoint_version_is_allowed():
    store = FakeStore()
    scope = chart()
    first = version(scope.id, end=date(2027, 1, 1))
    second = version(scope.id, label="EXAMPLE-V2", start=date(2027, 1, 1))
    register_chart(scope, FakeUnitOfWork(store))
    for snapshot in (first, second):
        uow = FakeUnitOfWork(store)
        register_chart_version(snapshot, uow)
        assert uow.commit_count == 1
    with FakeUnitOfWork(store) as reader:
        assert reader.charts.version_as_of(scope.id, date(2026, 12, 31)) == first
        assert reader.charts.version_as_of(scope.id, date(2027, 1, 1)) == second


def test_local_dimension_must_resolve_to_a_registered_definition(registered):
    store, scope, first = registered
    dimensions = AccountingDimensions(local_accounting_analytics=first.accounts[0].id)
    assert validate_accounting_dimensions(dimensions, first.id, FakeUnitOfWork(store)) == dimensions
    for version_id, local_id in (
        (uuid4(), dimensions.local_accounting_analytics),
        (first.id, uuid4()),
        (first.id, first.accounts[1].id),
    ):
        with pytest.raises(InvalidAccountingReference):
            validate_accounting_dimensions(
                replace(dimensions, local_accounting_analytics=local_id), version_id, FakeUnitOfWork(store)
            )
