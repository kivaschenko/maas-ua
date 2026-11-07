"""Synthetic reference snapshots; codes are examples, never regulatory seed data."""

from datetime import UTC, date, datetime
from uuid import uuid4

from maas_ua.domain.accounting import AccountBasis, AccountDefinition, AccountKind, ChartOfAccounts, ChartVersion

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def chart():
    return ChartOfAccounts(uuid4(), "EXAMPLE-ACCOUNTING-ENTITY")


def version(chart_id, *, label="EXAMPLE-V1", start=date(2026, 1, 1), end=None):
    official = AccountDefinition(
        id=uuid4(),
        code="1518",
        name="Example official account",
        kind=AccountKind.OFFICIAL,
        basis=AccountBasis.BALANCE,
        posting_allowed=True,
        authority_reference="EXAMPLE-REGULATORY-REFERENCE",
    )
    local = AccountDefinition(
        id=uuid4(),
        code="1518/03.01",
        name="Example local analytical account",
        kind=AccountKind.LOCAL_ANALYTICAL,
        basis=AccountBasis.BALANCE,
        posting_allowed=False,
        authority_reference="EXAMPLE-WORKING-CHART",
        parent_id=official.id,
        official_account_id=official.id,
    )
    return ChartVersion(
        id=uuid4(),
        chart_id=chart_id,
        label=label,
        valid_from=start,
        valid_to=end,
        approval_reference="EXAMPLE-APPROVAL",
        created_at=NOW,
        accounts=(local, official),
    )
