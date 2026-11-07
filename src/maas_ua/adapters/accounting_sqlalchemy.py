"""Accounting reference repository: insert-only snapshots and one narrow closure."""

from datetime import date
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from maas_ua.adapters.accounting_orm import (
    AccountDefinitionRecord,
    ChartRecord,
    ChartSupersessionRecord,
    ChartVersionRecord,
    account_from_record,
    supersession_from_record,
    version_from_record,
    version_to_record,
)
from maas_ua.domain.accounting import (
    AccountDefinition,
    ChartOfAccounts,
    ChartSupersession,
    ChartVersion,
    ChartVersionConflict,
    InvalidAccountingReference,
    SupersededChartVersion,
)
from maas_ua.ports import RepositoryConflict


class SqlAlchemyChartRepository:
    def __init__(self, session: Session):
        self.session = session

    def add_chart(self, chart: ChartOfAccounts) -> None:
        self.session.add(ChartRecord(**vars(chart)))

    def get_chart(self, chart_id: UUID) -> ChartOfAccounts | None:
        for record in self.session.new:
            if isinstance(record, ChartRecord) and record.id == chart_id:
                return ChartOfAccounts(record.id, record.accounting_entity_ref)
        with self.session.no_autoflush:
            record = self.session.get(ChartRecord, chart_id)
        return ChartOfAccounts(record.id, record.accounting_entity_ref) if record is not None else None

    def get_chart_for_update(self, chart_id: UUID) -> ChartOfAccounts | None:
        try:
            with self.session.no_autoflush:
                record = self.session.scalar(
                    select(ChartRecord)
                    .where(ChartRecord.id == chart_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
        except OperationalError as error:
            if getattr(error.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
                raise RepositoryConflict("concurrent chart transaction; retry the complete command") from None
            raise
        return ChartOfAccounts(record.id, record.accounting_entity_ref) if record is not None else None

    def add_version(self, version: ChartVersion) -> None:
        record = version_to_record(version)
        pending = next(
            (item for item in self.session.new if isinstance(item, ChartRecord) and item.id == version.chart_id), None
        )
        with self.session.no_autoflush:
            record.chart = pending or self.session.get(ChartRecord, version.chart_id)
        self.session.add(record)

    def get_version(self, version_id: UUID) -> ChartVersion | None:
        for record in self.session.new:
            if isinstance(record, ChartVersionRecord) and record.id == version_id:
                return version_from_record(record)
        with self.session.no_autoflush:
            record = self.session.scalar(
                select(ChartVersionRecord)
                .where(ChartVersionRecord.id == version_id)
                .options(selectinload(ChartVersionRecord.accounts))
                .execution_options(populate_existing=True)
            )
        return version_from_record(record) if record is not None else None

    def versions_for_chart(self, chart_id: UUID) -> tuple[ChartVersion, ...]:
        with self.session.no_autoflush:
            records = list(
                self.session.scalars(
                    select(ChartVersionRecord)
                    .where(ChartVersionRecord.chart_id == chart_id)
                    .options(selectinload(ChartVersionRecord.accounts))
                    .execution_options(populate_existing=True)
                )
            )
        records += [
            item for item in self.session.new if isinstance(item, ChartVersionRecord) and item.chart_id == chart_id
        ]
        return tuple(version_from_record(item) for item in sorted(records, key=lambda item: item.valid_from))

    def version_as_of(self, chart_id: UUID, as_of: date) -> ChartVersion | None:
        if type(as_of) is not date:
            raise InvalidAccountingReference("account resolution requires a business date")
        with self.session.no_autoflush:
            ids = list(
                self.session.scalars(
                    select(ChartVersionRecord.id).where(
                        ChartVersionRecord.chart_id == chart_id,
                        ChartVersionRecord.valid_from <= as_of,
                        or_(ChartVersionRecord.valid_to.is_(None), ChartVersionRecord.valid_to > as_of),
                    )
                )
            )
        ids += [
            item.id
            for item in self.session.new
            if isinstance(item, ChartVersionRecord)
            and item.chart_id == chart_id
            and item.valid_from <= as_of
            and (item.valid_to is None or as_of < item.valid_to)
        ]
        if len(ids) > 1:
            raise ChartVersionConflict("ambiguous applicable chart versions")
        return self.get_version(ids[0]) if ids else None

    def get_account(self, account_id: UUID) -> AccountDefinition | None:
        for record in self.session.new:
            if isinstance(record, AccountDefinitionRecord) and record.id == account_id:
                return account_from_record(record)
        with self.session.no_autoflush:
            record = self.session.get(AccountDefinitionRecord, account_id)
        return account_from_record(record) if record is not None else None

    def get_supersession(self, transition_id: UUID) -> ChartSupersession | None:
        for record in self.session.new:
            if isinstance(record, ChartSupersessionRecord) and record.id == transition_id:
                return supersession_from_record(record)
        with self.session.no_autoflush:
            record = self.session.get(ChartSupersessionRecord, transition_id)
        return supersession_from_record(record) if record is not None else None

    def protected_through(self, version_id: UUID) -> date | None:
        from maas_ua.adapters.journal_orm import JournalEntryRecord

        with self.session.no_autoflush:
            recorded = self.session.scalar(
                select(func.max(JournalEntryRecord.effective_date)).where(JournalEntryRecord.version_id == version_id)
            )
        pending = [
            item.effective_date
            for item in self.session.new
            if isinstance(item, JournalEntryRecord) and item.version_id == version_id
        ]
        return max(([recorded] if recorded is not None else []) + pending, default=None)

    def apply_supersession(self, result: SupersededChartVersion) -> None:
        if self.get_version(result.predecessor.id) != result.predecessor:
            raise RepositoryConflict("predecessor changed during supersession")
        # Execute only the approved applicability change before staging the
        # successor, avoiding a temporary overlap at INSERT. No flush or commit.
        with self.session.no_autoflush:
            changed = self.session.execute(
                update(ChartVersionRecord)
                .where(ChartVersionRecord.id == result.predecessor.id, ChartVersionRecord.valid_to.is_(None))
                .values(valid_to=result.closed_version.valid_to)
            )
        if changed.rowcount != 1:
            raise RepositoryConflict("predecessor is no longer open-ended")
        self.add_version(result.successor)
        self.session.add(ChartSupersessionRecord(**vars(result.audit)))
