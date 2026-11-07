from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from maas_ua.domain.accounting import (
    AccountBasis,
    AccountDefinition,
    AccountingDimensions,
    AccountKind,
    BudgetProgramCode,
    ChartOfAccounts,
    ChartVersion,
    CounterpartyRef,
    FundingSourceRef,
    FundRef,
    InvalidAccountingReference,
    KEKVCode,
    ReportingUnitRef,
    supersede_version,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def account(**changes):
    return AccountDefinition(
        **(
            dict(
                id=uuid4(),
                code="EXAMPLE-OFFICIAL",
                name="Example official account",
                kind=AccountKind.OFFICIAL,
                basis=AccountBasis.BALANCE,
                posting_allowed=True,
                authority_reference="EXAMPLE-REGULATION",
                parent_id=None,
                official_account_id=None,
            )
            | changes
        )
    )


def version(**changes):
    return ChartVersion(
        **(
            dict(
                id=uuid4(),
                chart_id=uuid4(),
                label="EXAMPLE-V1",
                valid_from=date(2026, 1, 1),
                valid_to=None,
                approval_reference="EXAMPLE-APPROVAL",
                created_at=NOW,
                accounts=(account(),),
            )
            | changes
        )
    )


def test_exact_codes_identity_and_immutable_snapshot():
    first = account(code="1518/03.01")
    second = account(code="1518/03.01a")
    third = account(code="1518/03.01A")
    snapshot = version(accounts=[first, second, third])
    assert snapshot.account_by_code(first.code) == first
    assert snapshot.account_by_code("1518/03.01A") == third
    assert snapshot.account_by_code("1518/03.01a") == second
    assert snapshot.account_by_code("missing") is None
    assert snapshot.account_by_id(first.id) == first
    with pytest.raises(FrozenInstanceError):
        first.code = "CHANGED"
    with pytest.raises(FrozenInstanceError):
        snapshot.label = "CHANGED"


def test_explicit_local_mapping_does_not_parse_code_or_infer_posting_permissions():
    official = account(code="1518", posting_allowed=True)
    local = account(
        code="1116/03.02",
        kind=AccountKind.LOCAL_ANALYTICAL,
        parent_id=official.id,
        official_account_id=official.id,
        posting_allowed=False,
    )
    snapshot = version(accounts=(local, official))
    assert snapshot.account_by_id(local.id).official_account_id == official.id
    assert official.posting_allowed and not local.posting_allowed


@pytest.mark.parametrize(
    "failure",
    [
        "duplicate_code",
        "duplicate_id",
        "missing_parent",
        "self_parent",
        "cycle",
        "official_to_local",
        "missing_mapping",
        "mapping_to_local",
        "different_mapping",
        "basis_mismatch",
    ],
)
def test_invalid_hierarchy_or_mapping_is_rejected(failure):
    official = account()
    other = account()
    local = account(
        code="EXAMPLE/03.01", kind=AccountKind.LOCAL_ANALYTICAL, parent_id=official.id, official_account_id=official.id
    )
    accounts = (official, local)
    if failure == "duplicate_code":
        accounts = (official, replace(other, code=official.code))
    elif failure == "duplicate_id":
        accounts = (official, replace(other, id=official.id, code="OTHER"))
    elif failure == "missing_parent":
        accounts = (replace(official, parent_id=uuid4()),)
    elif failure == "self_parent":
        accounts = (replace(official, parent_id=official.id),)
    elif failure == "cycle":
        accounts = (replace(official, parent_id=other.id), replace(other, code="OTHER", parent_id=official.id))
    elif failure == "official_to_local":
        accounts = (replace(official, parent_id=local.id), local)
    elif failure == "missing_mapping":
        with pytest.raises(InvalidAccountingReference):
            replace(local, official_account_id=None)
        return
    elif failure == "mapping_to_local":
        accounts = (official, replace(local, official_account_id=local.id))
    elif failure == "different_mapping":
        accounts = (official, replace(other, code="OTHER"), replace(local, official_account_id=other.id))
    else:
        accounts = (official, replace(local, basis=AccountBasis.OFF_BALANCE))
    with pytest.raises(InvalidAccountingReference):
        version(accounts=accounts)


@pytest.mark.parametrize("basis", list(AccountBasis))
def test_balance_basis_and_posting_eligibility_are_independent(basis):
    assert account(basis=basis, posting_allowed=False).basis is basis
    assert account(basis=basis, posting_allowed=True).posting_allowed


def test_half_open_validity_and_open_ended_versions():
    current = version(valid_to=date(2027, 1, 1))
    assert current.applies_on(date(2026, 1, 1))
    assert current.applies_on(date(2026, 12, 31))
    assert not current.applies_on(date(2027, 1, 1))
    assert not current.applies_on(date(2025, 12, 31))
    assert version().applies_on(date.max)


@pytest.mark.parametrize(
    "changes",
    [
        {"valid_to": date(2026, 1, 1)},
        {"valid_to": date(2025, 1, 1)},
        {"valid_from": NOW},
        {"created_at": datetime(2026, 1, 1)},
        {"accounts": ()},
        {"approval_reference": ""},
        {"label": " padded"},
    ],
)
def test_invalid_version_metadata(changes):
    with pytest.raises(InvalidAccountingReference):
        version(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"code": ""},
        {"code": " padded"},
        {"name": ""},
        {"id": "bad"},
        {"basis": "BALANCE"},
        {"kind": "OFFICIAL"},
        {"posting_allowed": 1},
        {"authority_reference": ""},
        {"official_account_id": uuid4()},
    ],
)
def test_account_metadata(changes):
    with pytest.raises(InvalidAccountingReference):
        account(**changes)


def transition(previous, successor, **changes):
    return supersede_version(
        previous,
        successor,
        **(
            dict(
                id=uuid4(),
                actor_ref="EXAMPLE-ACTOR",
                reason="EXAMPLE approved replacement",
                created_at=NOW,
                protected_through=None,
            )
            | changes
        ),
    )


def test_supersession_closes_only_applicability_and_preserves_definitions():
    previous = version()
    successor = version(chart_id=previous.chart_id, label="EXAMPLE-V2", valid_from=date(2026, 7, 1))
    result = transition(previous, successor)
    assert result.closed_version == replace(previous, valid_to=successor.valid_from)
    assert result.closed_version.accounts == previous.accounts
    assert result.closed_version.approval_reference == previous.approval_reference
    assert previous.valid_to is None
    assert result.audit.predecessor_id == previous.id and result.audit.successor_id == successor.id
    assert result.audit.effective_date == successor.valid_from
    assert result.successor == successor
    assert not result.closed_version.applies_on(successor.valid_from) and successor.applies_on(successor.valid_from)


@pytest.mark.parametrize(
    "failure",
    [
        "other_chart",
        "same_start",
        "earlier_start",
        "closed_predecessor",
        "same_label",
        "same_version",
        "reused_account_id",
        "posted_usage",
        "closed_period",
    ],
)
def test_invalid_supersession(failure):
    previous = version()
    successor = version(chart_id=previous.chart_id, label="EXAMPLE-V2", valid_from=date(2026, 7, 1))
    extra = {}
    if failure == "other_chart":
        successor = replace(successor, chart_id=uuid4())
    elif failure in {"same_start", "earlier_start"}:
        successor = replace(successor, valid_from=previous.valid_from if failure == "same_start" else date(2025, 1, 1))
    elif failure == "closed_predecessor":
        previous = replace(previous, valid_to=date(2026, 7, 1))
    elif failure == "same_label":
        successor = replace(successor, label=previous.label)
    elif failure == "same_version":
        successor = replace(successor, id=previous.id)
    elif failure == "reused_account_id":
        successor = replace(successor, accounts=previous.accounts)
    else:
        extra["protected_through"] = date(2026, 7, 1) if failure == "posted_usage" else date(2026, 12, 31)
    with pytest.raises(InvalidAccountingReference):
        transition(previous, successor, **extra)


def test_cutover_after_protected_history_is_allowed():
    previous = version()
    successor = version(chart_id=previous.chart_id, label="EXAMPLE-V2", valid_from=date(2026, 7, 1))
    assert transition(previous, successor, protected_through=date(2026, 6, 30)).successor == successor


@pytest.mark.parametrize(
    "value_type", [FundRef, BudgetProgramCode, KEKVCode, FundingSourceRef, ReportingUnitRef, CounterpartyRef]
)
def test_dimension_types_preserve_exact_values_without_inventing_code_lists(value_type):
    value = value_type("EXAMPLE/03.01")
    assert value.value == "EXAMPLE/03.01"
    with pytest.raises(FrozenInstanceError):
        value.value = "CHANGED"
    for bad in ("", " ", " padded", 42, None):
        with pytest.raises(InvalidAccountingReference):
            value_type(bad)


def test_dimensions_are_optional_and_references_are_validated_against_version():
    official = account()
    local = account(code="EXAMPLE/03", kind=AccountKind.LOCAL_ANALYTICAL, official_account_id=official.id)
    snapshot = version(accounts=(official, local))
    dimensions = AccountingDimensions(fund=FundRef("EXAMPLE"), local_accounting_analytics=local.id)
    dimensions.validate_for(snapshot)
    AccountingDimensions().validate_for(snapshot)
    with pytest.raises(InvalidAccountingReference):
        AccountingDimensions(fund="EXAMPLE")
    with pytest.raises(InvalidAccountingReference):
        AccountingDimensions(fund=FundingSourceRef("EXAMPLE"))
    for key in (official.id, uuid4()):
        with pytest.raises(InvalidAccountingReference):
            replace(dimensions, local_accounting_analytics=key).validate_for(snapshot)


def test_chart_identity_requires_explicit_entity():
    chart = ChartOfAccounts(uuid4(), "EXAMPLE-ENTITY")
    assert chart.accounting_entity_ref == "EXAMPLE-ENTITY"
    with pytest.raises(InvalidAccountingReference):
        ChartOfAccounts(chart.id, "")


def test_registration_intervals_cannot_overlap_but_can_be_adjacent():
    from maas_ua.domain.accounting import ChartVersionConflict, validate_new_version

    first = version(valid_to=date(2026, 7, 1))
    second = version(chart_id=first.chart_id, label="EXAMPLE-V2", valid_from=date(2026, 7, 1))
    validate_new_version(second, (first,))
    with pytest.raises(ChartVersionConflict):
        validate_new_version(replace(second, valid_from=date(2026, 6, 30)), (first,))
    with pytest.raises(ChartVersionConflict):
        validate_new_version(second, (replace(first, valid_to=None),))


def test_supersession_result_cannot_smuggle_approved_content_changes():
    first = version()
    second = version(chart_id=first.chart_id, label="EXAMPLE-V2", valid_from=date(2026, 7, 1))
    result = transition(first, second)
    with pytest.raises(InvalidAccountingReference):
        replace(result, closed_version=replace(result.closed_version, approval_reference="CHANGED"))
