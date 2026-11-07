"""Set-based read-only projections, without joins to documentary memberships."""

from contextlib import contextmanager

from sqlalchemy import func, select

from maas_ua.adapters.accounting_orm import AccountDefinitionRecord
from maas_ua.adapters.journal_orm import JournalEntryRecord, PostingLineRecord
from maas_ua.adapters.memorial_orm import MemorialOrderMembershipRecord, MemorialOrderRecord
from maas_ua.domain.journal import PostingSide
from maas_ua.domain.memorial_orders import MemorialOrderStatus
from maas_ua.service_layer.memorial_reporting import (
    AccountTurnover,
    CurrencyTurnover,
    JournalRegisterEntry,
    JournalRegisterState,
    MemorialOrderNotFound,
    MemorialOrderTotals,
)

orders = MemorialOrderRecord.__table__
members = MemorialOrderMembershipRecord.__table__
entries = JournalEntryRecord.__table__
lines = PostingLineRecord.__table__
accounts = AccountDefinitionRecord.__table__


class SqlAlchemyMemorialOrderReader:
    def __init__(self, engine):
        self.engine = engine

    @contextmanager
    def _snapshot(self):
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            yield connection

    def totals(self, query):
        with self._snapshot() as connection:
            header = connection.execute(
                select(
                    orders.c.status,
                    select(func.count())
                    .select_from(members)
                    .where(members.c.order_id == orders.c.id)
                    .correlate(orders)
                    .scalar_subquery()
                    .label("entry_count"),
                ).where(orders.c.id == query.order_id)
            ).one_or_none()
            if header is None:
                raise MemorialOrderNotFound("register not found")
            base = members.join(entries, members.c.entry_id == entries.c.id).join(
                lines, lines.c.entry_id == entries.c.id
            )
            rows = connection.execute(
                select(
                    entries.c.currency,
                    func.sum(lines.c.amount).filter(lines.c.side == "DEBIT").label("debit"),
                    func.sum(lines.c.amount).filter(lines.c.side == "CREDIT").label("credit"),
                )
                .select_from(base)
                .where(members.c.order_id == query.order_id)
                .group_by(entries.c.currency)
                .order_by(entries.c.currency)
            ).mappings()
            currencies = tuple(
                CurrencyTurnover(currency=r["currency"], debit=r["debit"], credit=r["credit"]) for r in rows
            )
            # Join the exact historical account UUID/version stored on each line.
            account_columns = (
                lines.c.version_id,
                lines.c.account_id,
                accounts.c.code,
                accounts.c.name,
                entries.c.currency,
                lines.c.side,
            )
            rows = connection.execute(
                select(*account_columns, func.sum(lines.c.amount).label("amount"))
                .select_from(
                    base.join(
                        accounts, (accounts.c.id == lines.c.account_id) & (accounts.c.version_id == lines.c.version_id)
                    )
                )
                .where(members.c.order_id == query.order_id)
                .group_by(*account_columns)
                .order_by(entries.c.currency, lines.c.version_id, lines.c.account_id, lines.c.side)
            ).mappings()
            totals = tuple(
                AccountTurnover(
                    version_id=r["version_id"],
                    account_id=r["account_id"],
                    account_code=r["code"],
                    account_name=r["name"],
                    currency=r["currency"],
                    side=PostingSide(r["side"]),
                    amount=r["amount"],
                )
                for r in rows
            )
            return MemorialOrderTotals(
                order_id=query.order_id,
                status=MemorialOrderStatus(header.status),
                entry_count=header.entry_count,
                currencies=currencies,
                accounts=totals,
            )

    def journal_entries(self, query):
        base = entries.outerjoin(members, members.c.entry_id == entries.c.id).outerjoin(
            orders, orders.c.id == members.c.order_id
        )
        statement = select(
            entries.c.id,
            entries.c.ledger_id,
            entries.c.period_id,
            entries.c.effective_date,
            entries.c.posting_instruction_key,
            entries.c.currency,
            members.c.order_id,
        )
        statement = statement.select_from(base).where(entries.c.ledger_id == query.ledger_id)
        if query.period_id is not None:
            statement = statement.where(entries.c.period_id == query.period_id)
        statement = statement.where(
            members.c.entry_id.is_(None)
            if query.state is JournalRegisterState.UNASSIGNED
            else orders.c.status == query.state.value
        )
        statement = statement.order_by(entries.c.effective_date, entries.c.id)
        with self._snapshot() as connection:
            return tuple(
                JournalRegisterEntry(
                    entry_id=r["id"],
                    ledger_id=r["ledger_id"],
                    period_id=r["period_id"],
                    effective_date=r["effective_date"],
                    posting_instruction_key=r["posting_instruction_key"],
                    currency=r["currency"],
                    state=query.state,
                    order_id=r["order_id"],
                )
                for r in connection.execute(statement).mappings()
            )
