"""Primary registers reference whole posted entries, never independent amounts."""

from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from maas_ua.domain.journal import AccountingPeriod, JournalEntry


class InvalidMemorialOrder(ValueError):
    """Invalid register, membership, lifecycle or supplied evidence."""


class DuplicateMemorialOrder(InvalidMemorialOrder):
    """Register/reference identity or issued number already registered."""


class DuplicatePrimaryMembership(InvalidMemorialOrder):
    """A posted entry already belongs to a primary register."""


def _ids(*values):
    if any(not isinstance(v, UUID) for v in values):
        raise InvalidMemorialOrder("register identities must be UUIDs")


def _text(*values):
    if any(not isinstance(v, str) or not v or v != v.strip() for v in values):
        raise InvalidMemorialOrder("register references require nonblank exact text without outer whitespace")


def _evidence(actor, authorization, timestamp):
    _text(actor, authorization)
    if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
        raise InvalidMemorialOrder("register evidence requires an aware timestamp")


@dataclass(frozen=True, kw_only=True)
class MemorialOrderType:
    id: UUID
    form_identifier: str
    variant: str
    revision: str
    authority_reference: str
    actor_ref: str
    authorization_reference: str
    created_at: datetime

    def __post_init__(self):
        _ids(self.id)
        _text(self.form_identifier, self.variant, self.revision, self.authority_reference)
        _evidence(self.actor_ref, self.authorization_reference, self.created_at)


@dataclass(frozen=True, kw_only=True)
class NumberingScope:
    """Explicit registered namespace; its approved policy is a required input."""

    id: UUID
    ledger_id: UUID
    namespace: str
    policy_reference: str
    actor_ref: str
    authorization_reference: str
    created_at: datetime

    def __post_init__(self):
        _ids(self.id, self.ledger_id)
        _text(self.namespace, self.policy_reference)
        _evidence(self.actor_ref, self.authorization_reference, self.created_at)


@dataclass(frozen=True)
class MemorialOrderNumber:
    scope_id: UUID
    value: str

    def __post_init__(self):
        _ids(self.scope_id)
        _text(self.value)


class MemorialOrderStatus(StrEnum):
    OPEN = "OPEN"
    FINALIZED = "FINALIZED"


@dataclass(frozen=True, kw_only=True)
class MemorialOrderMembership:
    entry_id: UUID
    actor_ref: str
    authorization_reference: str
    assigned_at: datetime

    def __post_init__(self):
        _ids(self.entry_id)
        _evidence(self.actor_ref, self.authorization_reference, self.assigned_at)


@dataclass(frozen=True, kw_only=True)
class MemorialOrder:
    id: UUID
    type_id: UUID
    ledger_id: UUID
    period_id: UUID
    coverage_start: date
    coverage_end: date
    number: MemorialOrderNumber
    actor_ref: str
    authorization_reference: str
    created_at: datetime
    status: MemorialOrderStatus = MemorialOrderStatus.OPEN
    memberships: tuple[MemorialOrderMembership, ...] = ()
    finalized_by: str | None = None
    finalization_reference: str | None = None
    finalized_at: datetime | None = None

    def __post_init__(self):
        _ids(self.id, self.type_id, self.ledger_id, self.period_id)
        if (
            type(self.coverage_start) is not date
            or type(self.coverage_end) is not date
            or self.coverage_start >= self.coverage_end
        ):
            raise InvalidMemorialOrder("register requires positive half-open business-date coverage")
        if not isinstance(self.number, MemorialOrderNumber) or not isinstance(self.status, MemorialOrderStatus):
            raise InvalidMemorialOrder("register number and lifecycle must be explicit")
        _evidence(self.actor_ref, self.authorization_reference, self.created_at)
        members = tuple(self.memberships)
        if any(not isinstance(m, MemorialOrderMembership) for m in members):
            raise InvalidMemorialOrder("register requires validated memberships")
        if len({m.entry_id for m in members}) != len(members):
            raise DuplicatePrimaryMembership("duplicate primary membership")
        object.__setattr__(self, "memberships", members)
        if self.status is MemorialOrderStatus.FINALIZED:
            if not members:
                raise InvalidMemorialOrder("finalization requires at least one assigned entry")
            _evidence(self.finalized_by, self.finalization_reference, self.finalized_at)
        elif any(v is not None for v in (self.finalized_by, self.finalization_reference, self.finalized_at)):
            raise InvalidMemorialOrder("open register cannot contain finalization evidence")

    def _require_open(self):
        if self.status is not MemorialOrderStatus.OPEN:
            raise InvalidMemorialOrder("finalized register cannot be changed")

    def assign(self, entries, *, actor_ref, authorization_reference, assigned_at):
        self._require_open()
        _evidence(actor_ref, authorization_reference, assigned_at)
        entries = tuple(entries)
        if not entries or any(not isinstance(e, JournalEntry) for e in entries):
            raise InvalidMemorialOrder("assignment requires existing posted journal entries")
        ids = tuple(e.id for e in entries)
        if len(set(ids)) != len(ids) or set(ids) & {m.entry_id for m in self.memberships}:
            raise DuplicatePrimaryMembership("duplicate primary membership")
        if any(
            (e.ledger_id, e.period_id) != (self.ledger_id, self.period_id)
            or not self.coverage_start <= e.effective_date < self.coverage_end
            for e in entries
        ):
            raise InvalidMemorialOrder("entry ledger, period and effective date must match register coverage")
        return replace(
            self,
            memberships=self.memberships
            + tuple(
                MemorialOrderMembership(
                    entry_id=e.id,
                    actor_ref=actor_ref,
                    authorization_reference=authorization_reference,
                    assigned_at=assigned_at,
                )
                for e in entries
            ),
        )

    def finalize(self, *, actor_ref, authorization_reference, finalized_at):
        self._require_open()
        return replace(
            self,
            status=MemorialOrderStatus.FINALIZED,
            finalized_by=actor_ref,
            finalization_reference=authorization_reference,
            finalized_at=finalized_at,
        )


def validate_order_references(
    order: MemorialOrder, period: AccountingPeriod, kind: MemorialOrderType, scope: NumberingScope
) -> None:
    if (
        period.id != order.period_id
        or period.ledger_id != order.ledger_id
        or not period.starts_on <= order.coverage_start < order.coverage_end <= period.ends_before
        or kind.id != order.type_id
        or scope.id != order.number.scope_id
        or scope.ledger_id != order.ledger_id
    ):
        raise InvalidMemorialOrder("register requires matching registered type, numbering scope, ledger and period")
