"""Synthetic financial instructions; no regulatory correspondence is implied."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from accounting_fixtures import NOW

from maas_ua.domain.journal import (
    AccountingLedger,
    AccountingPeriod,
    JournalEntry,
    LedgerSourceReference,
    PostingLine,
    PostingSide,
)
from maas_ua.domain.receipts import Money


def period(ledger_id, **changes):
    return AccountingPeriod(
        **(
            dict(
                id=uuid4(),
                ledger_id=ledger_id,
                starts_on=date(2026, 1, 1),
                ends_before=date(2027, 1, 1),
                posting_allowed=True,
                actor_ref="EXAMPLE-ACTOR",
                authorization_reference="EXAMPLE-PERIOD",
                created_at=NOW,
            )
            | changes
        )
    )


def line(account_id, side=PostingSide.DEBIT, amount="100.0001", currency="UAH", **changes):
    return PostingLine(account_id=account_id, side=side, amount=Money(Decimal(amount), currency), **changes)


def entry(snapshot, ledger=None, accounting_period=None, source_id=None, **changes):
    ledger = ledger or AccountingLedger(uuid4(), snapshot.chart_id)
    accounting_period = accounting_period or period(ledger.id)
    account_id = snapshot.accounts[-1].id
    return JournalEntry(
        **(
            dict(
                id=uuid4(),
                ledger_id=ledger.id,
                chart_id=ledger.chart_id,
                version_id=snapshot.id,
                period_id=accounting_period.id,
                posting_instruction_key="EXAMPLE-INSTRUCTION/COMPONENT-1",
                effective_date=date(2026, 2, 10),
                currency="UAH",
                purpose="EXAMPLE component",
                actor_ref="EXAMPLE-ACTOR",
                authorization_reference="EXAMPLE-APPROVAL",
                created_at=NOW,
                lines=(line(account_id), line(account_id, PostingSide.CREDIT)),
                source_reference_ids=(source_id or uuid4(),),
            )
            | changes
        )
    )


def register_context(make_uow, receipt):
    from accounting_fixtures import chart, version

    from maas_ua.service_layer.accounting import register_chart, register_chart_version
    from maas_ua.service_layer.journal import (
        attribute_ledger_source,
        register_accounting_ledger,
        register_accounting_period,
    )

    with make_uow() as uow:
        uow.nomenclatures.add(receipt.receipt.lines[0].nomenclature)
        uow.receipts.add(receipt.receipt)
        for unit in receipt.units:
            uow.asset_units.add(unit)
        uow.commit()
    chart_record = chart()
    snapshot = version(chart_record.id)
    register_chart(chart_record, make_uow())
    register_chart_version(snapshot, make_uow())
    ledger = AccountingLedger(uuid4(), chart_record.id)
    calendar = period(ledger.id)
    source = LedgerSourceReference(
        id=uuid4(),
        ledger_id=ledger.id,
        receipt_id=receipt.receipt.id,
        actor_ref="EXAMPLE-ACTOR",
        authorization_reference="EXAMPLE-ATTRIBUTION",
        created_at=NOW,
    )
    register_accounting_ledger(ledger, make_uow())
    register_accounting_period(calendar, make_uow())
    attribute_ledger_source(source, make_uow())
    return ledger, calendar, snapshot, source


def command(context, **changes):
    from maas_ua.service_layer.journal import PostJournalEntry

    ledger, calendar, snapshot, source = context
    values = vars(entry(snapshot, ledger, calendar, source.id)).copy()
    values.pop("chart_id")
    values.pop("created_at")
    return PostJournalEntry(**(values | changes))
