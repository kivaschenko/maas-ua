"""Explicit register commands, each coordinated by one atomic UoW."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from maas_ua.domain.memorial_orders import (
    DuplicatePrimaryMembership,
    InvalidMemorialOrder,
    MemorialOrder,
    MemorialOrderNumber,
    MemorialOrderType,
    NumberingScope,
    validate_order_references,
)
from maas_ua.ports import UnitOfWork


def _now():
    return datetime.now(UTC)


def register_memorial_order_type(kind: MemorialOrderType, uow: UnitOfWork) -> MemorialOrderType:
    with uow:
        uow.memorial_orders.add_type(kind)
        uow.commit()
    return kind


def register_numbering_scope(scope: NumberingScope, uow: UnitOfWork) -> NumberingScope:
    with uow:
        if uow.ledgers.get(scope.ledger_id) is None:
            raise InvalidMemorialOrder("numbering namespace requires a registered ledger")
        uow.memorial_orders.add_scope(scope)
        uow.commit()
    return scope


@dataclass(frozen=True, kw_only=True)
class CreateMemorialOrder:
    id: UUID
    type_id: UUID
    ledger_id: UUID
    period_id: UUID
    coverage_start: date
    coverage_end: date
    number: MemorialOrderNumber
    actor_ref: str
    authorization_reference: str


@dataclass(frozen=True, kw_only=True)
class AssignJournalEntries:
    order_id: UUID
    entry_ids: tuple[UUID, ...]
    actor_ref: str
    authorization_reference: str

    def __post_init__(self):
        object.__setattr__(self, "entry_ids", tuple(self.entry_ids))


@dataclass(frozen=True, kw_only=True)
class FinalizeMemorialOrder:
    order_id: UUID
    actor_ref: str
    authorization_reference: str


def create_memorial_order(
    command: CreateMemorialOrder, uow: UnitOfWork, *, clock: Callable[[], datetime] = _now
) -> MemorialOrder:
    result = MemorialOrder(**vars(command), created_at=clock())
    with uow:
        period = uow.ledgers.get_period(result.period_id)
        kind = uow.memorial_orders.get_type(result.type_id)
        scope = uow.memorial_orders.get_scope(result.number.scope_id)
        if period is None or kind is None or scope is None or uow.ledgers.get(result.ledger_id) is None:
            raise InvalidMemorialOrder("register references must already be registered")
        validate_order_references(result, period, kind, scope)
        uow.memorial_orders.add(result)
        uow.commit()
    return result


def _locked_order(order_id, uow):
    if not isinstance(order_id, UUID):
        raise InvalidMemorialOrder("register identity must be a UUID")
    order = uow.memorial_orders.get_for_update(order_id)
    if order is None:
        raise InvalidMemorialOrder("register must exist")
    return order


def assign_journal_entries(
    command: AssignJournalEntries, uow: UnitOfWork, *, clock: Callable[[], datetime] = _now
) -> MemorialOrder:
    if not command.entry_ids or any(not isinstance(key, UUID) for key in command.entry_ids):
        raise InvalidMemorialOrder("assignment requires posted entry UUIDs")
    if len(set(command.entry_ids)) != len(command.entry_ids):
        raise DuplicatePrimaryMembership("duplicate primary membership")
    with uow:
        previous = _locked_order(command.order_id, uow)
        entries = tuple(uow.journals.get(key) for key in command.entry_ids)
        result = previous.assign(
            entries,
            actor_ref=command.actor_ref,
            authorization_reference=command.authorization_reference,
            assigned_at=clock(),
        )
        if any(uow.memorial_orders.order_for_entry(key) is not None for key in command.entry_ids):
            raise DuplicatePrimaryMembership("entry already assigned to a primary register")
        # Validate the complete batch before staging even its first membership.
        uow.memorial_orders.assign(previous, result)
        uow.commit()
    return result


def finalize_memorial_order(
    command: FinalizeMemorialOrder, uow: UnitOfWork, *, clock: Callable[[], datetime] = _now
) -> MemorialOrder:
    with uow:
        previous = _locked_order(command.order_id, uow)
        result = previous.finalize(
            actor_ref=command.actor_ref, authorization_reference=command.authorization_reference, finalized_at=clock()
        )
        uow.memorial_orders.finalize(previous, result)
        uow.commit()
    return result
