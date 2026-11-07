"""Declarative register records; financial amounts remain in journal tables."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from maas_ua.adapters.orm import Base
from maas_ua.domain.memorial_orders import (
    MemorialOrder,
    MemorialOrderMembership,
    MemorialOrderNumber,
    MemorialOrderStatus,
    MemorialOrderType,
    NumberingScope,
)


def _text_checks(*columns):
    return tuple(CheckConstraint(f"{c} <> '' AND {c} = btrim({c})", name=f"{c}_text") for c in columns)


class MemorialOrderTypeRecord(Base):
    __tablename__ = "accounting_memorial_order_types"
    __table_args__ = (
        UniqueConstraint(
            "form_identifier", "variant", "revision", "authority_reference", name="uq_memorial_type_revision"
        ),
        *_text_checks(
            "form_identifier", "variant", "revision", "authority_reference", "actor_ref", "authorization_reference"
        ),
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    form_identifier: Mapped[str] = mapped_column(Text(collation="C"))
    variant: Mapped[str] = mapped_column(Text(collation="C"))
    revision: Mapped[str] = mapped_column(Text(collation="C"))
    authority_reference: Mapped[str] = mapped_column(Text(collation="C"))
    actor_ref: Mapped[str] = mapped_column(Text)
    authorization_reference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NumberingScopeRecord(Base):
    __tablename__ = "accounting_memorial_numbering_scopes"
    __table_args__ = (
        UniqueConstraint("ledger_id", "namespace", name="uq_memorial_numbering_namespace"),
        UniqueConstraint("id", "ledger_id", name="uq_memorial_scope_ledger"),
        *_text_checks("namespace", "policy_reference", "actor_ref", "authorization_reference"),
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    ledger_id: Mapped[UUID] = mapped_column(ForeignKey("accounting_ledgers.id"))
    namespace: Mapped[str] = mapped_column(Text(collation="C"))
    policy_reference: Mapped[str] = mapped_column(Text)
    actor_ref: Mapped[str] = mapped_column(Text)
    authorization_reference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemorialOrderRecord(Base):
    __tablename__ = "accounting_memorial_orders"
    __table_args__ = (
        UniqueConstraint("scope_id", "issued_number", name="uq_memorial_order_identity"),
        UniqueConstraint("id", "ledger_id", name="uq_memorial_order_ledger"),
        ForeignKeyConstraint(
            ("ledger_id", "period_id"),
            ("accounting_periods.ledger_id", "accounting_periods.id"),
            name="fk_memorial_order_period",
        ),
        ForeignKeyConstraint(
            ("scope_id", "ledger_id"),
            ("accounting_memorial_numbering_scopes.id", "accounting_memorial_numbering_scopes.ledger_id"),
            name="fk_memorial_order_scope",
        ),
        CheckConstraint(
            "isfinite(coverage_start) AND isfinite(coverage_end) AND coverage_start < coverage_end", name="coverage"
        ),
        CheckConstraint("isfinite(created_at)", name="created_at_finite"),
        CheckConstraint(
            "(status = 'OPEN' AND finalized_by IS NULL AND finalization_reference IS NULL "
            "AND finalized_at IS NULL) OR (status = 'FINALIZED' AND finalized_by IS NOT NULL "
            "AND finalized_by <> '' AND finalized_by = btrim(finalized_by) "
            "AND finalization_reference IS NOT NULL AND finalization_reference <> '' "
            "AND finalization_reference = btrim(finalization_reference) "
            "AND finalized_at IS NOT NULL AND isfinite(finalized_at))",
            name="finalization",
        ),
        *_text_checks("issued_number", "actor_ref", "authorization_reference"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    type_id: Mapped[UUID] = mapped_column(ForeignKey("accounting_memorial_order_types.id"))
    ledger_id: Mapped[UUID]
    period_id: Mapped[UUID]
    coverage_start: Mapped[date]
    coverage_end: Mapped[date]
    scope_id: Mapped[UUID]
    issued_number: Mapped[str] = mapped_column(Text(collation="C"))
    actor_ref: Mapped[str] = mapped_column(Text)
    authorization_reference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    finalized_by: Mapped[str | None] = mapped_column(Text)
    finalization_reference: Mapped[str | None] = mapped_column(Text)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memberships: Mapped[list[MemorialOrderMembershipRecord]] = relationship(
        order_by="MemorialOrderMembershipRecord.entry_id"
    )


class MemorialOrderMembershipRecord(Base):
    __tablename__ = "accounting_memorial_order_memberships"
    __table_args__ = (
        UniqueConstraint("entry_id", name="uq_memorial_primary_membership"),
        ForeignKeyConstraint(
            ("order_id", "ledger_id"),
            ("accounting_memorial_orders.id", "accounting_memorial_orders.ledger_id"),
            name="fk_memorial_membership_order",
        ),
        ForeignKeyConstraint(
            ("entry_id", "ledger_id"),
            ("accounting_journal_entries.id", "accounting_journal_entries.ledger_id"),
            name="fk_memorial_membership_entry",
        ),
        *_text_checks("actor_ref", "authorization_reference"),
        CheckConstraint("isfinite(assigned_at)", name="assigned_at_finite"),
    )
    order_id: Mapped[UUID] = mapped_column(primary_key=True)
    entry_id: Mapped[UUID] = mapped_column(primary_key=True)
    ledger_id: Mapped[UUID]
    actor_ref: Mapped[str] = mapped_column(Text)
    authorization_reference: Mapped[str] = mapped_column(Text)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def type_from_record(record):
    return MemorialOrderType(**{key: getattr(record, key) for key in MemorialOrderType.__dataclass_fields__})


def scope_from_record(record):
    return NumberingScope(**{key: getattr(record, key) for key in NumberingScope.__dataclass_fields__})


def membership_to_record(order, membership):
    return MemorialOrderMembershipRecord(order_id=order.id, ledger_id=order.ledger_id, **vars(membership))


def order_to_record(order):
    fields = {k: v for k, v in vars(order).items() if k not in {"number", "memberships", "status"}}
    return MemorialOrderRecord(
        **fields,
        scope_id=order.number.scope_id,
        issued_number=order.number.value,
        status=order.status.value,
        memberships=[membership_to_record(order, m) for m in order.memberships],
    )


def order_from_record(record):
    fields = {
        k: getattr(record, k)
        for k in MemorialOrder.__dataclass_fields__
        if k not in {"number", "memberships", "status"}
    }
    return MemorialOrder(
        **fields,
        number=MemorialOrderNumber(record.scope_id, record.issued_number),
        status=MemorialOrderStatus(record.status),
        memberships=tuple(
            MemorialOrderMembership(**{k: getattr(m, k) for k in MemorialOrderMembership.__dataclass_fields__})
            for m in record.memberships
        ),
    )
