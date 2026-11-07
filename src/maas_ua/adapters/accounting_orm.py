"""Declarative storage and explicit conversion for approved accounting references."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from maas_ua.adapters.orm import Base
from maas_ua.domain.accounting import AccountBasis, AccountDefinition, AccountKind, ChartSupersession, ChartVersion


class ChartRecord(Base):
    __tablename__ = "accounting_charts"
    __table_args__ = (
        CheckConstraint(
            "accounting_entity_ref <> '' AND accounting_entity_ref = btrim(accounting_entity_ref)", name="entity_text"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    accounting_entity_ref: Mapped[str] = mapped_column(Text(collation="C"))


class ChartVersionRecord(Base):
    __tablename__ = "accounting_chart_versions"
    __table_args__ = (
        UniqueConstraint("chart_id", "label", name="uq_accounting_chart_versions_label"),
        UniqueConstraint("chart_id", "id", name="uq_accounting_chart_versions_chart_id"),
        CheckConstraint(
            "isfinite(valid_from) AND (valid_to IS NULL OR (isfinite(valid_to) AND valid_to > valid_from))",
            name="validity",
        ),
        *(
            CheckConstraint(f"{column} <> '' AND {column} = btrim({column})", name=f"{column}_text")
            for column in ("label", "approval_reference")
        ),
        ExcludeConstraint(
            ("chart_id", "="),
            (text("daterange(valid_from, valid_to, '[)')"), "&&"),
            name="ex_accounting_chart_versions_validity",
            using="gist",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    chart_id: Mapped[UUID] = mapped_column(ForeignKey("accounting_charts.id"))
    chart: Mapped[ChartRecord] = relationship()
    label: Mapped[str] = mapped_column(Text(collation="C"))
    valid_from: Mapped[date]
    valid_to: Mapped[date | None]
    approval_reference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accounts: Mapped[list[AccountDefinitionRecord]] = relationship(order_by="AccountDefinitionRecord.position")


class AccountDefinitionRecord(Base):
    __tablename__ = "accounting_account_definitions"
    __table_args__ = (
        UniqueConstraint("version_id", "code", name="uq_accounting_account_definitions_code"),
        UniqueConstraint("version_id", "position", name="uq_accounting_account_definitions_position"),
        UniqueConstraint("version_id", "id", name="uq_accounting_account_definitions_version_id"),
        ForeignKeyConstraint(
            ("version_id", "parent_id"),
            ("accounting_account_definitions.version_id", "accounting_account_definitions.id"),
            name="fk_accounting_accounts_parent_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ("version_id", "official_account_id"),
            ("accounting_account_definitions.version_id", "accounting_account_definitions.id"),
            name="fk_accounting_accounts_official_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("kind IN ('OFFICIAL', 'LOCAL_ANALYTICAL')", name="kind"),
        CheckConstraint("basis IN ('BALANCE', 'OFF_BALANCE')", name="basis"),
        CheckConstraint(
            "(kind='OFFICIAL' AND official_account_id IS NULL) OR "
            "(kind='LOCAL_ANALYTICAL' AND official_account_id IS NOT NULL)",
            name="mapping_required",
        ),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="parent_not_self"),
        CheckConstraint("official_account_id IS NULL OR official_account_id <> id", name="mapping_not_self"),
        CheckConstraint("position >= 0", name="position_nonnegative"),
        *(
            CheckConstraint(f"{column} <> '' AND {column} = btrim({column})", name=f"{column}_text")
            for column in ("code", "name", "authority_reference")
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    version_id: Mapped[UUID] = mapped_column(ForeignKey("accounting_chart_versions.id"))
    position: Mapped[int]
    code: Mapped[str] = mapped_column(Text(collation="C"))
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    basis: Mapped[str] = mapped_column(Text)
    posting_allowed: Mapped[bool]
    authority_reference: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[UUID | None]
    official_account_id: Mapped[UUID | None]


class ChartSupersessionRecord(Base):
    __tablename__ = "accounting_chart_supersessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ("chart_id", "predecessor_id"),
            ("accounting_chart_versions.chart_id", "accounting_chart_versions.id"),
            name="fk_accounting_supersessions_predecessor_chart",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ("chart_id", "successor_id"),
            ("accounting_chart_versions.chart_id", "accounting_chart_versions.id"),
            name="fk_accounting_supersessions_successor_chart",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint("predecessor_id", name="uq_accounting_chart_supersessions_predecessor"),
        UniqueConstraint("successor_id", name="uq_accounting_chart_supersessions_successor"),
        CheckConstraint("predecessor_id <> successor_id", name="distinct_versions"),
        *(
            CheckConstraint(f"{column} <> '' AND {column} = btrim({column})", name=f"{column}_text")
            for column in ("actor_ref", "reason")
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    chart_id: Mapped[UUID]
    predecessor_id: Mapped[UUID]
    successor_id: Mapped[UUID]
    effective_date: Mapped[date]
    actor_ref: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def account_from_record(record: AccountDefinitionRecord) -> AccountDefinition:
    return AccountDefinition(
        id=record.id,
        code=record.code,
        name=record.name,
        kind=AccountKind(record.kind),
        basis=AccountBasis(record.basis),
        posting_allowed=record.posting_allowed,
        authority_reference=record.authority_reference,
        parent_id=record.parent_id,
        official_account_id=record.official_account_id,
    )


def version_to_record(version: ChartVersion) -> ChartVersionRecord:
    return ChartVersionRecord(
        id=version.id,
        chart_id=version.chart_id,
        label=version.label,
        valid_from=version.valid_from,
        valid_to=version.valid_to,
        approval_reference=version.approval_reference,
        created_at=version.created_at,
        accounts=[
            AccountDefinitionRecord(**vars(item), position=position) for position, item in enumerate(version.accounts)
        ],
    )


def version_from_record(record: ChartVersionRecord) -> ChartVersion:
    return ChartVersion(
        id=record.id,
        chart_id=record.chart_id,
        label=record.label,
        valid_from=record.valid_from,
        valid_to=record.valid_to,
        approval_reference=record.approval_reference,
        created_at=record.created_at,
        accounts=tuple(account_from_record(item) for item in record.accounts),
    )


def supersession_from_record(record: ChartSupersessionRecord) -> ChartSupersession:
    return ChartSupersession(
        **{
            key: getattr(record, key)
            for key in (
                "id",
                "chart_id",
                "predecessor_id",
                "successor_id",
                "effective_date",
                "actor_ref",
                "reason",
                "created_at",
            )
        }
    )
