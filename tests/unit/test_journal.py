from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from accounting_fixtures import NOW, chart, version
from journal_fixtures import entry, line, period

from maas_ua.domain.accounting import AccountBasis, AccountingDimensions
from maas_ua.domain.journal import (
    AccountingLedger,
    InvalidJournalEntry,
    LedgerSourceReference,
    PostingLine,
    PostingSide,
    validate_journal_references,
)
from maas_ua.domain.receipts import Money


def test_two_and_multi_line_entries_and_immutable_input_snapshots():
    snapshot = version(chart().id)
    account = snapshot.accounts[-1].id
    lines = [line(account, amount="1.0001"), line(account, amount="2"), line(account, PostingSide.CREDIT, "3.0001")]
    sources = [uuid4()]
    result = entry(snapshot, lines=lines, source_reference_ids=sources)
    lines.clear()
    sources.clear()
    assert len(result.lines) == 3 and len(result.source_reference_ids) == 1
    assert entry(snapshot).lines[0].amount == Money(Decimal("100.0001"), "UAH")
    for obj, attribute, value in ((result, "currency", "OTHER"), (result.lines[0], "side", PostingSide.CREDIT)):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, attribute, value)


@pytest.mark.parametrize("failure", ["empty", "debit_only", "credit_only", "imbalance", "currency", "sources"])
def test_invalid_aggregate(failure):
    snapshot = version(chart().id)
    account = snapshot.accounts[-1].id
    changes = {
        "empty": {"lines": ()},
        "debit_only": {"lines": (line(account),)},
        "credit_only": {"lines": (line(account, PostingSide.CREDIT),)},
        "imbalance": {"lines": (line(account), line(account, PostingSide.CREDIT, "99"))},
        "currency": {"lines": (line(account), line(account, PostingSide.CREDIT, currency="OTHER"))},
        "sources": {"source_reference_ids": ()},
    }
    with pytest.raises(InvalidJournalEntry):
        entry(snapshot, **changes[failure])


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), 1.0])
def test_invalid_amounts(amount):
    with pytest.raises(ValueError):
        PostingLine(account_id=uuid4(), side=PostingSide.DEBIT, amount=Money(amount, "UAH"))


def test_exact_sum_does_not_depend_on_ambient_decimal_context():
    snapshot = version(chart().id)
    account = snapshot.accounts[-1].id
    with localcontext() as context:
        context.prec = 2
        entry(
            snapshot,
            lines=(
                line(account, amount="123456789.123456789"),
                line(account, amount="0.000000001"),
                line(account, PostingSide.CREDIT, "123456789.123456790"),
            ),
        )
        with pytest.raises(InvalidJournalEntry):
            entry(
                snapshot,
                lines=(
                    line(account, amount="123456789.123456789"),
                    line(account, PostingSide.CREDIT, "123456789.123456790"),
                ),
            )


@pytest.mark.parametrize(
    "failure",
    [
        "ledger",
        "chart",
        "version",
        "expired",
        "period",
        "forbidden",
        "before",
        "end",
        "account",
        "nonposting",
        "off_balance",
        "analytics",
        "source_ledger",
    ],
)
def test_contextual_references_are_validated(failure):
    snapshot = version(chart().id)
    ledger = AccountingLedger(uuid4(), snapshot.chart_id)
    calendar = period(ledger.id)
    source = LedgerSourceReference(
        id=uuid4(),
        ledger_id=ledger.id,
        receipt_id=uuid4(),
        actor_ref="EXAMPLE",
        authorization_reference="EXAMPLE",
        created_at=NOW,
    )
    result = entry(snapshot, ledger, calendar, source.id)
    if failure == "ledger":
        ledger = replace(ledger, id=uuid4())
    elif failure == "chart":
        ledger = replace(ledger, chart_id=uuid4())
    elif failure == "version":
        snapshot = replace(snapshot, id=uuid4())
    elif failure == "expired":
        snapshot = replace(snapshot, valid_to=result.effective_date)
    elif failure == "period":
        calendar = replace(calendar, ledger_id=uuid4())
    elif failure == "forbidden":
        calendar = replace(calendar, posting_allowed=False)
    elif failure in {"before", "end"}:
        result = replace(result, effective_date=date(2025, 12, 31) if failure == "before" else calendar.ends_before)
    elif failure == "account":
        result = replace(result, lines=tuple(replace(item, account_id=uuid4()) for item in result.lines))
    elif failure in {"nonposting", "off_balance"}:
        snapshot = replace(
            snapshot,
            accounts=tuple(
                replace(a, posting_allowed=False)
                if failure == "nonposting"
                else replace(a, basis=AccountBasis.OFF_BALANCE)
                for a in snapshot.accounts
            ),
        )
    elif failure == "analytics":
        result = replace(
            result,
            lines=tuple(
                replace(item, dimensions=AccountingDimensions(local_accounting_analytics=uuid4()))
                for item in result.lines
            ),
        )
    else:
        source = replace(source, ledger_id=uuid4())
    with pytest.raises(InvalidJournalEntry):
        validate_journal_references(result, ledger, calendar, snapshot, (source,))


def test_valid_context_and_half_open_period_boundaries():
    snapshot = version(chart().id)
    ledger = AccountingLedger(uuid4(), snapshot.chart_id)
    calendar = period(ledger.id)
    source = LedgerSourceReference(
        id=uuid4(),
        ledger_id=ledger.id,
        write_off_id=uuid4(),
        actor_ref="EXAMPLE",
        authorization_reference="EXAMPLE",
        created_at=NOW,
    )
    result = entry(snapshot, ledger, calendar, source.id, effective_date=calendar.starts_on)
    validate_journal_references(result, ledger, calendar, snapshot, (source,))


@pytest.mark.parametrize("changes", [{}, {"receipt_id": uuid4(), "write_off_id": uuid4()}, {"receipt_id": "bad"}])
def test_document_attribution_requires_one_typed_document_id(changes):
    with pytest.raises(InvalidJournalEntry):
        LedgerSourceReference(
            id=uuid4(),
            ledger_id=uuid4(),
            actor_ref="EXAMPLE",
            authorization_reference="EXAMPLE",
            created_at=NOW,
            **changes,
        )
