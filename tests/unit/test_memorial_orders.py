from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
from uuid import uuid4

import pytest
from accounting_fixtures import NOW, chart, version
from journal_fixtures import entry, period
from memorial_fixtures import numbering_scope, order, order_type

from maas_ua.domain.journal import AccountingLedger
from maas_ua.domain.memorial_orders import (
    InvalidMemorialOrder,
    MemorialOrderNumber,
    MemorialOrderStatus,
    validate_order_references,
)


@pytest.fixture
def facts():
    snapshot = version(chart().id)
    ledger = AccountingLedger(uuid4(), snapshot.chart_id)
    calendar = period(ledger.id)
    kind, scope = order_type(), numbering_scope(ledger.id)
    register = order(ledger, calendar, kind, scope)
    return register, calendar, kind, scope, entry(snapshot, ledger, calendar)


def test_open_register_and_whole_entry_membership_snapshots(facts):
    register, calendar, kind, scope, journal = facts
    validate_order_references(register, calendar, kind, scope)
    assert register.status is MemorialOrderStatus.OPEN and register.memberships == ()
    entries = [journal, replace(journal, id=uuid4())]
    assigned = register.assign(entries, actor_ref="EXAMPLE", authorization_reference="EXAMPLE", assigned_at=NOW)
    entries.clear()
    assert register.memberships == () and len(assigned.memberships) == 2
    assert assigned.memberships[0].entry_id == journal.id
    with pytest.raises(FrozenInstanceError):
        assigned.memberships[0].entry_id = uuid4()


@pytest.mark.parametrize(
    "changes",
    [
        {"ledger_id": uuid4()},
        {"period_id": uuid4()},
        {"effective_date": date(2026, 1, 31)},
        {"effective_date": date(2026, 3, 1)},
    ],
)
def test_assignment_requires_same_ledger_period_and_half_open_coverage(facts, changes):
    register, *_, journal = facts
    with pytest.raises(InvalidMemorialOrder):
        register.assign(
            (replace(journal, **changes),), actor_ref="EXAMPLE", authorization_reference="EXAMPLE", assigned_at=NOW
        )


@pytest.mark.parametrize("failure", ["empty", "duplicate", "already_member", "bad_evidence", "naive_time"])
def test_invalid_assignment(facts, failure):
    register, *_, journal = facts
    entries, actor, timestamp = (journal,), "EXAMPLE", NOW
    if failure == "empty":
        entries = ()
    if failure == "duplicate":
        entries = (journal, journal)
    if failure == "already_member":
        register = register.assign(entries, actor_ref=actor, authorization_reference=actor, assigned_at=timestamp)
    if failure == "bad_evidence":
        actor = " "
    if failure == "naive_time":
        timestamp = datetime(2026, 1, 1)
    with pytest.raises(InvalidMemorialOrder):
        register.assign(entries, actor_ref=actor, authorization_reference="EXAMPLE", assigned_at=timestamp)


def test_only_nonempty_order_can_finalize_and_no_further_assignment(facts):
    register, *_, journal = facts
    with pytest.raises(InvalidMemorialOrder):
        register.finalize(actor_ref="EXAMPLE", authorization_reference="EXAMPLE", finalized_at=NOW)
    assigned = register.assign((journal,), actor_ref="EXAMPLE", authorization_reference="EXAMPLE", assigned_at=NOW)
    final = assigned.finalize(actor_ref="EXAMPLE", authorization_reference="EXAMPLE", finalized_at=NOW)
    assert final.status is MemorialOrderStatus.FINALIZED and final.finalized_at == NOW
    assert final.memberships == assigned.memberships
    with pytest.raises(InvalidMemorialOrder):
        final.assign(
            (replace(journal, id=uuid4()),), actor_ref="EXAMPLE", authorization_reference="EXAMPLE", assigned_at=NOW
        )
    with pytest.raises(InvalidMemorialOrder):
        final.finalize(actor_ref="EXAMPLE", authorization_reference="EXAMPLE", finalized_at=NOW)


@pytest.mark.parametrize("failure", ["period_ledger", "period_id", "coverage", "type", "scope", "scope_ledger"])
def test_registration_references(facts, failure):
    register, calendar, kind, scope, _ = facts
    if failure == "period_ledger":
        calendar = replace(calendar, ledger_id=uuid4())
    if failure == "period_id":
        calendar = replace(calendar, id=uuid4())
    if failure == "coverage":
        calendar = replace(calendar, starts_on=date(2026, 2, 2))
    if failure == "type":
        kind = replace(kind, id=uuid4())
    if failure == "scope":
        scope = replace(scope, id=uuid4())
    if failure == "scope_ledger":
        scope = replace(scope, ledger_id=uuid4())
    with pytest.raises(InvalidMemorialOrder):
        validate_order_references(register, calendar, kind, scope)


@pytest.mark.parametrize(
    "changes",
    [
        {"coverage_end": date(2026, 2, 1)},
        {"coverage_start": datetime(2026, 2, 1)},
        {"actor_ref": ""},
        {"created_at": datetime(2026, 1, 1)},
        {"status": "OPEN"},
        {"finalized_at": NOW},
        {"status": MemorialOrderStatus.FINALIZED},
    ],
)
def test_invalid_order_snapshot(facts, changes):
    with pytest.raises(InvalidMemorialOrder):
        replace(facts[0], **changes)


def test_number_scope_and_form_revision_are_explicit():
    with pytest.raises(InvalidMemorialOrder):
        MemorialOrderNumber(uuid4(), " 17")
    with pytest.raises(InvalidMemorialOrder):
        order_type(revision="")
    with pytest.raises(InvalidMemorialOrder):
        numbering_scope(uuid4(), policy_reference="")
