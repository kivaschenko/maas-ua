from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from accounting_fixtures import NOW, chart, version
from journal_fixtures import command, line, period, register_context
from memorial_fixtures import assignment, create_command, finalization, numbering_scope, order, order_type

from maas_ua.adapters.memorial_reader import SqlAlchemyMemorialOrderReader
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.domain.accounting import AccountBasis, AccountDefinition, AccountKind
from maas_ua.domain.journal import AccountingLedger, LedgerSourceReference, PostingSide
from maas_ua.service_layer.accounting import (
    SupersedeChartVersion,
    register_chart,
    register_chart_version,
    supersede_chart_version,
)
from maas_ua.service_layer.journal import (
    attribute_ledger_source,
    post_journal_entry,
    register_accounting_ledger,
    register_accounting_period,
)
from maas_ua.service_layer.memorial_orders import (
    assign_journal_entries,
    create_memorial_order,
    finalize_memorial_order,
    register_memorial_order_type,
    register_numbering_scope,
)
from maas_ua.service_layer.memorial_reporting import (
    GetJournalRegisterEntries,
    GetMemorialOrderTotals,
    JournalRegisterState,
    MemorialOrderNotFound,
    UnsupportedCorrespondenceRendering,
)

pytestmark = pytest.mark.integration


def test_canonical_multiline_and_three_correspondence_totals_without_source_multiplication(
    session_factory, engine, write_off_receipt
):
    def make():
        return SqlAlchemyUnitOfWork(session_factory)

    with make() as writer:
        writer.nomenclatures.add(write_off_receipt.receipt.lines[0].nomenclature)
        writer.commit()
    scope_chart = chart()
    snapshot = version(scope_chart.id)
    # Four synthetic account definitions, independent of any official correspondence.
    accounts = tuple(
        AccountDefinition(
            id=uuid4(),
            code=f"EXAMPLE-{i}",
            name=f"Example account {i}",
            kind=AccountKind.OFFICIAL,
            basis=AccountBasis.BALANCE,
            posting_allowed=True,
            authority_reference="EXAMPLE",
        )
        for i in range(4)
    )
    register_chart(scope_chart, make())
    snapshot = replace(snapshot, accounts=accounts)
    register_chart_version(snapshot, make())
    ledger = AccountingLedger(uuid4(), scope_chart.id)
    calendar = period(ledger.id)
    register_accounting_ledger(ledger, make())
    register_accounting_period(calendar, make())
    # Two different synthetic receipt documents support each entry. Amounts must
    # not be multiplied by this many-to-many documentary attribution.
    sources = []
    for _ in range(2):
        receipt = replace(
            write_off_receipt.receipt,
            id=uuid4(),
            lines=tuple(replace(item, id=uuid4()) for item in write_off_receipt.receipt.lines),
        )
        with make() as writer:
            writer.receipts.add(receipt)
            writer.commit()
        source = LedgerSourceReference(
            id=uuid4(),
            ledger_id=ledger.id,
            receipt_id=receipt.id,
            actor_ref="EXAMPLE",
            authorization_reference="EXAMPLE",
            created_at=NOW,
        )
        attribute_ledger_source(source, make())
        sources.append(source)
    context = (ledger, calendar, snapshot, sources[0])
    source_ids = tuple(source.id for source in sources)
    kind, scope = order_type(), numbering_scope(ledger.id)
    register_memorial_order_type(kind, make())
    register_numbering_scope(scope, make())
    register = create_memorial_order(create_command(order(ledger, calendar, kind, scope)), make())
    # Three externally supplied two-sided components, plus a multiline component.
    entries = []
    for i, amount in enumerate(("10.0001", "20.0002", "30.0003")):
        entries.append(
            post_journal_entry(
                command(
                    context,
                    posting_instruction_key=f"EXAMPLE-{i}",
                    lines=(line(accounts[i].id, amount=amount), line(accounts[3].id, PostingSide.CREDIT, amount)),
                    source_reference_ids=source_ids,
                ),
                make(),
            )
        )
    entries.append(
        post_journal_entry(
            command(
                context,
                posting_instruction_key="EXAMPLE-MULTI",
                lines=(
                    line(accounts[0].id, amount="60"),
                    line(accounts[1].id, amount="40"),
                    line(accounts[2].id, PostingSide.CREDIT, "100"),
                ),
                source_reference_ids=source_ids,
            ),
            make(),
        )
    )
    reader = SqlAlchemyMemorialOrderReader(engine)
    query = GetJournalRegisterEntries(ledger_id=ledger.id, period_id=calendar.id, state=JournalRegisterState.UNASSIGNED)
    assert {e.entry_id for e in reader.journal_entries(query)} == {e.id for e in entries}
    assert reader.totals(GetMemorialOrderTotals(register.id)).currencies == ()
    assign_journal_entries(assignment(register, entries), make())
    with localcontext() as decimal_context:
        decimal_context.prec = 2
        totals = reader.totals(GetMemorialOrderTotals(register.id))
    assert totals.entry_count == 4
    assert totals.currencies[0].debit == totals.currencies[0].credit == Decimal("160.0006")
    assert {(t.account_id, t.side): t.amount for t in totals.accounts} == {
        (accounts[0].id, PostingSide.DEBIT): Decimal("70.0001"),
        (accounts[1].id, PostingSide.DEBIT): Decimal("60.0002"),
        (accounts[2].id, PostingSide.DEBIT): Decimal("30.0003"),
        (accounts[2].id, PostingSide.CREDIT): Decimal("100"),
        (accounts[3].id, PostingSide.CREDIT): Decimal("60.0006"),
    }
    assert all(t.version_id == snapshot.id and t.account_name.startswith("Example") for t in totals.accounts)
    assert totals.diagnostics == ("correspondence_rendering_unsupported",)
    with pytest.raises(UnsupportedCorrespondenceRendering):
        totals.correspondence_rows()
    assert len(reader.journal_entries(replace(query, state=JournalRegisterState.OPEN))) == 4
    assert reader.journal_entries(query) == ()
    finalize_memorial_order(finalization(register), make())
    late = post_journal_entry(
        command(context, posting_instruction_key="EXAMPLE-LATE", source_reference_ids=source_ids), make()
    )
    assert [e.entry_id for e in reader.journal_entries(query)] == [late.id]
    assert len(reader.journal_entries(replace(query, state=JournalRegisterState.FINALIZED))) == 4
    assert reader.journal_entries(replace(query, state=JournalRegisterState.OPEN)) == ()
    assert reader.totals(GetMemorialOrderTotals(register.id)).currencies == totals.currencies
    successor = version(scope_chart.id, label="EXAMPLE-V2", start=date(2026, 3, 1))
    supersede_chart_version(
        SupersedeChartVersion(
            transition_id=uuid4(),
            predecessor_id=snapshot.id,
            successor=successor,
            actor_ref="EXAMPLE",
            reason="EXAMPLE",
        ),
        make(),
    )
    assert reader.totals(GetMemorialOrderTotals(register.id)).accounts == totals.accounts
    with pytest.raises(MemorialOrderNotFound):
        reader.totals(GetMemorialOrderTotals(uuid4()))


def test_currency_and_ledger_filters_are_separate(session_factory, engine, write_off_receipt):
    def make():
        return SqlAlchemyUnitOfWork(session_factory)

    context = register_context(make, write_off_receipt)
    kind, scope = order_type(), numbering_scope(context[0].id)
    register_memorial_order_type(kind, make())
    register_numbering_scope(scope, make())
    register = create_memorial_order(create_command(order(*context[:2], kind, scope)), make())
    account = context[2].accounts[-1].id
    entries = tuple(
        post_journal_entry(
            command(
                context,
                currency=currency,
                posting_instruction_key=currency,
                lines=(
                    line(account, amount="1.0001", currency=currency),
                    line(account, PostingSide.CREDIT, "1.0001", currency=currency),
                ),
            ),
            make(),
        )
        for currency in ("UAH", "EXAMPLE")
    )
    assign_journal_entries(assignment(register, entries), make())
    reader = SqlAlchemyMemorialOrderReader(engine)
    totals = reader.totals(GetMemorialOrderTotals(register.id))
    assert {t.currency for t in totals.currencies} == {"UAH", "EXAMPLE"}
    assert all(t.debit == t.credit == Decimal("1.0001") for t in totals.currencies)
    assert reader.journal_entries(GetJournalRegisterEntries(ledger_id=uuid4(), state=JournalRegisterState.OPEN)) == ()


def test_report_reads_one_read_only_snapshot_during_assignment_and_finalization(
    session_factory, engine, write_off_receipt
):
    from sqlalchemy import event

    def make():
        return SqlAlchemyUnitOfWork(session_factory)

    context = register_context(make, write_off_receipt)
    kind, scope = order_type(), numbering_scope(context[0].id)
    register_memorial_order_type(kind, make())
    register_numbering_scope(scope, make())
    register = create_memorial_order(create_command(order(*context[:2], kind, scope)), make())
    journal = post_journal_entry(command(context), make())
    changed = []

    def change_after_header(connection, cursor, statement, parameters, execution_context, executemany):
        if not changed and "SELECT accounting_memorial_orders.status," in statement:
            changed.append(True)
            assert connection.exec_driver_sql("SHOW transaction_isolation").scalar_one() == "repeatable read"
            assert connection.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "on"
            assign_journal_entries(assignment(register, (journal,)), make())
            finalize_memorial_order(finalization(register), make())

    event.listen(engine, "after_cursor_execute", change_after_header)
    try:
        reader = SqlAlchemyMemorialOrderReader(engine)
        before = reader.totals(GetMemorialOrderTotals(register.id))
    finally:
        event.remove(engine, "after_cursor_execute", change_after_header)
    assert changed and before.entry_count == 0 and before.currencies == before.accounts == ()
    assert before.status.value == "OPEN"
    after = reader.totals(GetMemorialOrderTotals(register.id))
    assert after.entry_count == 1 and after.status.value == "FINALIZED"
