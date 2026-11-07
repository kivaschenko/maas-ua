from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest
from accounting_fixtures import NOW, version
from journal_fixtures import command, entry, period, register_context
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from maas_ua.adapters.journal_orm import JournalEntryRecord, journal_to_record
from maas_ua.adapters.sqlalchemy import SqlAlchemyUnitOfWork
from maas_ua.domain.accounting import InvalidAccountingReference, supersede_version
from maas_ua.domain.journal import AccountingLedger, DuplicateJournalEntry, InvalidJournalEntry
from maas_ua.ports import RepositoryConflict
from maas_ua.service_layer.accounting import SupersedeChartVersion, supersede_chart_version
from maas_ua.service_layer.journal import (
    attribute_ledger_source,
    post_journal_entry,
    register_accounting_ledger,
    register_accounting_period,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def registered(session_factory, write_off_receipt):
    return register_context(lambda: SqlAlchemyUnitOfWork(session_factory), write_off_receipt)


def test_round_trip_and_distinct_components_retain_document_provenance(registered, session_factory):
    first = post_journal_entry(command(registered), SqlAlchemyUnitOfWork(session_factory), clock=lambda: NOW)
    second = post_journal_entry(
        command(registered, posting_instruction_key="EXAMPLE-COMPONENT-2"),
        SqlAlchemyUnitOfWork(session_factory),
        clock=lambda: NOW,
    )
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.journals.get(first.id) == first
        assert reader.journals.get_by_instruction(first.ledger_id, first.posting_instruction_key) == first
        assert reader.journals.get(second.id) == second
        assert reader.ledgers.get_source(first.source_reference_ids[0]) == registered[3]
        assert reader.charts.protected_through(first.version_id) == first.effective_date


def write_off_source(context, session_factory, write_off_command):
    from maas_ua.service_layer.write_offs import write_off_assets

    write_off_assets(write_off_command, SqlAlchemyUnitOfWork(session_factory), clock=lambda: NOW)
    source = replace(context[3], id=uuid4(), receipt_id=None, write_off_id=write_off_command.write_off_id)
    attribute_ledger_source(source, SqlAlchemyUnitOfWork(session_factory))
    return source


def test_multi_line_dimensions_and_multiple_typed_sources_round_trip(registered, session_factory, write_off_command):
    from journal_fixtures import line

    from maas_ua.domain.accounting import (
        AccountingDimensions,
        BudgetProgramCode,
        CounterpartyRef,
        FundingSourceRef,
        FundRef,
        KEKVCode,
        ReportingUnitRef,
    )
    from maas_ua.domain.journal import PostingSide

    source = write_off_source(registered, session_factory, write_off_command)
    dimensions = AccountingDimensions(
        fund=FundRef("EXAMPLE-FUND"),
        budget_program_code=BudgetProgramCode("EXAMPLE-PROGRAM"),
        kekv=KEKVCode("EXAMPLE-KEKV"),
        funding_source=FundingSourceRef("EXAMPLE-FUNDING"),
        reporting_unit=ReportingUnitRef("EXAMPLE-REPORTING"),
        counterparty=CounterpartyRef("EXAMPLE-COUNTERPARTY"),
        local_accounting_analytics=registered[2].accounts[0].id,
    )
    account = registered[2].accounts[-1].id
    request = command(
        registered,
        source_reference_ids=(registered[3].id, source.id),
        lines=(
            line(account, amount="123456789.123456789", dimensions=dimensions),
            line(account, amount="0.000000001"),
            line(account, PostingSide.CREDIT, "123456789.123456790"),
        ),
    )
    result = post_journal_entry(request, SqlAlchemyUnitOfWork(session_factory), clock=lambda: NOW)
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.journals.get(result.id) == result
        assert reader.ledgers.get_source(source.id).write_off_id == write_off_command.write_off_id
        assert reader.receipts.get(registered[3].receipt_id).id == registered[3].receipt_id


def test_staged_aggregates_can_be_read_without_flushing(registered, session_factory):
    ledger, calendar, snapshot, source = registered
    result = entry(snapshot, ledger, calendar, source.id)
    with SqlAlchemyUnitOfWork(session_factory) as writer:
        writer.journals.add(result)
        assert writer.journals.get(result.id) == result
        assert writer.journals.get_by_instruction(ledger.id, result.posting_instruction_key) == result
        assert len(writer.session.new) == 4
    with SqlAlchemyUnitOfWork(session_factory) as reader:
        assert reader.journals.get(result.id) is None


@pytest.mark.parametrize(
    "failure",
    [
        "balance",
        "missing_side",
        "zero",
        "negative",
        "nan",
        "infinity",
        "missing_lines",
        "missing_sources",
        "account_version",
        "period_ledger",
        "source_ledger",
        "chart",
        "expired",
        "nonposting",
        "local_kind",
        "period_date",
        "period_permission",
        "source_missing",
        "off_balance",
    ],
)
def test_direct_inserts_cannot_bypass_financial_integrity(registered, session_factory, failure):
    ledger, calendar, snapshot, source = registered
    record = journal_to_record(entry(snapshot, ledger, calendar, source.id))
    if failure == "balance":
        record.lines[0].amount = 99
    elif failure == "missing_side":
        record.lines[0].side = "CREDIT"
    elif failure in {"zero", "negative", "nan", "infinity"}:
        from decimal import Decimal

        record.lines[0].amount = {
            "zero": Decimal(0),
            "negative": Decimal(-1),
            "nan": Decimal("NaN"),
            "infinity": Decimal("Infinity"),
        }[failure]
    elif failure == "missing_lines":
        record.lines = []
    elif failure == "missing_sources":
        record.sources = []
    elif failure == "account_version":
        record.lines[0].version_id = uuid4()
    elif failure == "chart":
        record.chart_id = uuid4()
    elif failure == "expired":
        record.effective_date = date(2025, 1, 1)
    elif failure == "nonposting":
        record.lines[0].account_id = snapshot.accounts[0].id
    elif failure == "local_kind":
        record.lines[0].local_accounting_analytics = snapshot.accounts[-1].id
    elif failure == "period_date":
        record.effective_date = calendar.ends_before
    elif failure == "period_permission":
        forbidden = period(
            ledger.id, starts_on=calendar.ends_before, ends_before=date(2028, 1, 1), posting_allowed=False
        )
        register_accounting_period(forbidden, SqlAlchemyUnitOfWork(session_factory))
        record.period_id, record.effective_date = forbidden.id, forbidden.starts_on
    elif failure == "source_missing":
        record.sources[0].source_id = uuid4()
    elif failure == "off_balance":
        with session_factory.begin() as session:
            session.execute(
                text("UPDATE accounting_account_definitions SET basis='OFF_BALANCE' WHERE version_id=:id"),
                {"id": snapshot.id},
            )
    else:
        other = AccountingLedger(uuid4(), ledger.chart_id)
        register_accounting_ledger(other, SqlAlchemyUnitOfWork(session_factory))
        if failure == "period_ledger":
            other_period = period(other.id)
            register_accounting_period(other_period, SqlAlchemyUnitOfWork(session_factory))
            record.period_id = other_period.id
        else:
            record.sources[0].ledger_id = other.id
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            # Core INSERTs preserve deliberately bad composite keys; ORM
            # relationship synchronization would repair them before INSERT.
            session.execute(record.__table__.insert(), record_values(record))
            for child in (*record.lines, *record.sources):
                session.execute(child.__table__.insert(), record_values(child))
            session.commit()
    with session_factory() as session:
        assert session.get(JournalEntryRecord, record.id) is None


def record_values(record):
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}


@pytest.mark.parametrize(
    "table", ["accounting_journal_entries", "accounting_posting_lines", "accounting_journal_sources"]
)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_posted_history_rejects_direct_mutation(registered, session_factory, table, operation):
    post_journal_entry(command(registered), SqlAlchemyUnitOfWork(session_factory))
    sql = {
        "UPDATE": f"UPDATE {table} SET {'id=id' if table.endswith('entries') else 'position=position'}",
        "DELETE": f"DELETE FROM {table}",
        "TRUNCATE": f"TRUNCATE {table} CASCADE",
    }[operation]
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(text(sql))
            session.commit()


@pytest.mark.parametrize("child", ["line", "balanced_lines", "source"])
def test_late_insert_cannot_extend_completed_entry(registered, session_factory, write_off_command, child):
    result = post_journal_entry(command(registered), SqlAlchemyUnitOfWork(session_factory))
    extra_source = write_off_source(registered, session_factory, write_off_command)
    if child in {"line", "balanced_lines"}:
        sql = """INSERT INTO accounting_posting_lines
        (entry_id, position, version_id, account_id, side, amount)
        VALUES (:entry, 99, :version, :account, 'DEBIT', 1)"""
        if child == "balanced_lines":
            sql += ", (:entry, 100, :version, :account, 'CREDIT', 1)"
    else:
        sql = """INSERT INTO accounting_journal_sources (entry_id, position, ledger_id, source_id)
        VALUES (:entry, 99, :ledger, :source)"""
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text(sql),
                dict(
                    entry=result.id,
                    version=result.version_id,
                    account=result.lines[0].account_id,
                    ledger=result.ledger_id,
                    source=extra_source.id,
                ),
            )
            session.commit()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE accounting_journal_entries SET line_count=line_count+2",
        "UPDATE accounting_chart_versions SET valid_to='2026-02-10'",
        "UPDATE accounting_account_definitions SET posting_allowed=false",
        "DELETE FROM accounting_ledger_sources",
        "UPDATE accounting_ledger_sources SET receipt_id=NULL",
        "UPDATE accounting_periods SET posting_allowed=false",
        "UPDATE accounting_ledgers SET chart_id=chart_id",
        "INSERT INTO accounting_posting_lines SELECT * FROM accounting_posting_lines "
        "ON CONFLICT (entry_id, position) DO UPDATE SET amount=EXCLUDED.amount",
    ],
)
def test_other_attempts_to_rewrite_posted_basis_are_rejected(registered, session_factory, sql):
    post_journal_entry(command(registered), SqlAlchemyUnitOfWork(session_factory))
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(text(sql))
            session.commit()


def test_period_overlap_source_attribution_and_missing_document_constraints(registered, session_factory):
    ledger, calendar, _, source = registered
    with pytest.raises(RepositoryConflict):
        register_accounting_period(replace(calendar, id=uuid4()), SqlAlchemyUnitOfWork(session_factory))
    register_accounting_period(
        period(ledger.id, starts_on=calendar.ends_before, ends_before=date(2028, 1, 1)),
        SqlAlchemyUnitOfWork(session_factory),
    )
    other = AccountingLedger(uuid4(), ledger.chart_id)
    register_accounting_ledger(other, SqlAlchemyUnitOfWork(session_factory))
    with pytest.raises(RepositoryConflict):
        attribute_ledger_source(replace(source, id=uuid4(), ledger_id=other.id), SqlAlchemyUnitOfWork(session_factory))
    with pytest.raises(InvalidJournalEntry):
        attribute_ledger_source(replace(source, id=uuid4(), receipt_id=uuid4()), SqlAlchemyUnitOfWork(session_factory))


def test_failure_after_flush_rolls_back_header_lines_and_sources(registered, session_factory):
    request = command(registered)

    class FailingUow(SqlAlchemyUnitOfWork):
        def commit(self):
            self.session.flush()
            raise RuntimeError("synthetic failure after flush")

    with pytest.raises(RuntimeError):
        post_journal_entry(request, FailingUow(session_factory))
    with session_factory() as session:
        for table in ("accounting_journal_entries", "accounting_posting_lines", "accounting_journal_sources"):
            assert session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


@pytest.mark.parametrize("same_uuid", [False, True])
def test_concurrent_duplicate_posting_is_atomic(registered, session_factory, same_uuid):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_accounting_reference_persistence import _wait_until_blocked

    request = command(registered)
    started, backend = Event(), []

    class ObservedUow(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            backend.append(self.session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            return self

    with ThreadPoolExecutor(max_workers=1) as pool:
        with SqlAlchemyUnitOfWork(session_factory) as first:
            first.charts.get_chart_for_update(registered[0].chart_id)
            first.ledgers.get_period_for_update(registered[1].id)
            first.journals.add(
                entry(registered[2], registered[0], registered[1], registered[3].id, **vars_for_entry(request))
            )
            other = (
                replace(request, posting_instruction_key="EXAMPLE-OTHER") if same_uuid else replace(request, id=uuid4())
            )
            future = pool.submit(post_journal_entry, other, ObservedUow(session_factory))
            try:
                _wait_until_blocked(session_factory, backend, started, future)
            finally:
                first.commit()
        with pytest.raises(DuplicateJournalEntry):
            future.result(timeout=5)
    with session_factory() as session:
        assert len(session.scalars(select(JournalEntryRecord)).all()) == 1


def vars_for_entry(request):
    return dict(
        id=request.id, posting_instruction_key=request.posting_instruction_key, effective_date=request.effective_date
    )


@pytest.mark.parametrize("same_uuid", [True, False])
def test_database_duplicate_races_without_service_prechecks(registered, session_factory, same_uuid):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_accounting_reference_persistence import _wait_until_blocked

    ledger, calendar, snapshot, source = registered
    result = entry(snapshot, ledger, calendar, source.id)
    other = replace(result, posting_instruction_key="EXAMPLE-OTHER") if same_uuid else replace(result, id=uuid4())
    started, backend = Event(), []

    def persist_without_prechecks():
        with SqlAlchemyUnitOfWork(session_factory) as writer:
            backend.append(writer.session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            writer.journals.add(other)
            writer.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with SqlAlchemyUnitOfWork(session_factory) as writer:
            writer.journals.add(result)
            writer.session.flush()
            future = pool.submit(persist_without_prechecks)
            try:
                _wait_until_blocked(session_factory, backend, started, future)
            finally:
                writer.commit()
        with pytest.raises(DuplicateJournalEntry):
            future.result(timeout=5)
    with session_factory() as session:
        assert len(session.scalars(select(JournalEntryRecord)).all()) == 1


def test_lock_timeout_retains_whole_command_retry_contract(registered, session_factory):
    class TimeoutUow(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            self.session.execute(text("SET LOCAL lock_timeout='50ms'"))
            return self

    with SqlAlchemyUnitOfWork(session_factory) as holder:
        holder.charts.get_chart_for_update(registered[0].chart_id)
        with pytest.raises(RepositoryConflict, match="retry the complete command"):
            post_journal_entry(command(registered), TimeoutUow(session_factory))


def test_forcing_deferred_checks_early_does_not_allow_later_extension(registered, session_factory):
    ledger, calendar, snapshot, source = registered
    result = entry(snapshot, ledger, calendar, source.id)
    with session_factory() as session:
        session.add(journal_to_record(result))
        session.flush()
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        with pytest.raises(IntegrityError):
            session.execute(
                text("""INSERT INTO accounting_posting_lines
                (entry_id, position, version_id, account_id, side, amount)
                VALUES (:entry, 50, :version, :account, 'DEBIT', 1),
                       (:entry, 51, :version, :account, 'CREDIT', 1)"""),
                dict(entry=result.id, version=snapshot.id, account=snapshot.accounts[-1].id),
            )
    with session_factory() as session:
        assert session.get(JournalEntryRecord, result.id) is None


def test_temporary_tables_cannot_shadow_integrity_validation(registered, session_factory):
    ledger, calendar, snapshot, source = registered
    record = journal_to_record(entry(snapshot, ledger, calendar, source.id))
    with session_factory() as session:
        # A decoy balance must not make an unbalanced real journal acceptable.
        session.execute(
            text(
                "CREATE TEMP TABLE accounting_posting_lines "
                "(LIKE public.accounting_posting_lines INCLUDING ALL) ON COMMIT DROP"
            )
        )
        session.execute(
            text("""INSERT INTO pg_temp.accounting_posting_lines
            (entry_id, position, version_id, account_id, side, amount)
            VALUES (:entry, 0, :version, :account, 'DEBIT', 1), (:entry, 1, :version, :account, 'CREDIT', 1)"""),
            dict(entry=record.id, version=snapshot.id, account=snapshot.accounts[-1].id),
        )
        with pytest.raises(IntegrityError):
            for obj in (record, *record.lines, *record.sources):
                fields = record_values(obj)
                if obj is record.lines[0]:
                    fields["amount"] = 99
                columns = ", ".join(fields)
                parameters = ", ".join(":" + key for key in fields)
                session.execute(
                    text(f"INSERT INTO public.{obj.__tablename__} ({columns}) VALUES ({parameters})"), fields
                )
            session.commit()


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_financial_writes_reject_nondefault_snapshot_isolation(registered, session_factory, isolation):
    class SnapshotUow(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            self.session.connection(execution_options={"isolation_level": isolation})
            return self

    with pytest.raises(InvalidJournalEntry):
        post_journal_entry(command(registered), SnapshotUow(session_factory))


def test_application_factory_sets_required_isolation_even_if_database_default_differs(engine):
    from maas_ua.bootstrap import make_session_factory

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(
            text(f"ALTER DATABASE \"{engine.url.database}\" SET default_transaction_isolation TO 'repeatable read'")
        )
    factory = make_session_factory(engine.url)
    try:
        with factory() as session:
            assert session.scalar(text("SHOW transaction_isolation")) == "read committed"
    finally:
        factory.kw["bind"].dispose()


@pytest.mark.parametrize("posting_first", [True, False])
def test_posting_and_supersession_coordinate_in_both_orders(registered, session_factory, posting_first):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_accounting_reference_persistence import _wait_until_blocked

    ledger, calendar, snapshot, source = registered
    request = command(registered)
    successor = version(ledger.chart_id, label="EXAMPLE-V2", start=request.effective_date)
    transition = SupersedeChartVersion(
        transition_id=uuid4(), predecessor_id=snapshot.id, successor=successor, actor_ref="EXAMPLE", reason="EXAMPLE"
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
            first.charts.get_chart_for_update(ledger.chart_id)
            if posting_first:
                first.ledgers.get_period_for_update(calendar.id)
                first.journals.add(entry(snapshot, ledger, calendar, source.id, **vars_for_entry(request)))
                future = pool.submit(supersede_chart_version, transition, ObservedUow(session_factory))
            else:
                first.charts.apply_supersession(
                    supersede_version(
                        snapshot,
                        successor,
                        id=transition.transition_id,
                        actor_ref="EXAMPLE",
                        reason="EXAMPLE",
                        created_at=NOW,
                        protected_through=None,
                    )
                )
                future = pool.submit(post_journal_entry, request, ObservedUow(session_factory))
            try:
                _wait_until_blocked(session_factory, backend, started, future)
            finally:
                first.commit()
        with pytest.raises((InvalidAccountingReference, InvalidJournalEntry)):
            future.result(timeout=5)
    if posting_first:
        later = replace(transition, successor=version(ledger.chart_id, label="EXAMPLE-LATER", start=date(2026, 7, 1)))
        supersede_chart_version(later, SqlAlchemyUnitOfWork(session_factory))
        with SqlAlchemyUnitOfWork(session_factory) as reader:
            assert reader.journals.get(request.id).version_id == snapshot.id
            assert reader.charts.get_account(snapshot.accounts[-1].id) == snapshot.accounts[-1]
