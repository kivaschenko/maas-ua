"""Narrow aggregate operations; never flush or commit from repositories."""

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import selectinload

from maas_ua.adapters.memorial_orm import (
    MemorialOrderMembershipRecord,
    MemorialOrderRecord,
    MemorialOrderTypeRecord,
    NumberingScopeRecord,
    membership_to_record,
    order_from_record,
    order_to_record,
    scope_from_record,
    type_from_record,
)
from maas_ua.ports import RepositoryConflict


class SqlAlchemyMemorialOrderRepository:
    def __init__(self, session):
        self.session = session

    def _get(self, model, key):
        for record in self.session.new:
            if isinstance(record, model) and record.id == key:
                return record
        with self.session.no_autoflush:
            return self.session.get(model, key)

    def add_type(self, kind):
        self.session.add(MemorialOrderTypeRecord(**vars(kind)))

    def get_type(self, key):
        record = self._get(MemorialOrderTypeRecord, key)
        return type_from_record(record) if record is not None else None

    def add_scope(self, scope):
        self.session.add(NumberingScopeRecord(**vars(scope)))

    def get_scope(self, key):
        record = self._get(NumberingScopeRecord, key)
        return scope_from_record(record) if record is not None else None

    def add(self, order):
        self.session.add(order_to_record(order))

    def get(self, key, *, refresh=False):
        for record in self.session.new:
            if isinstance(record, MemorialOrderRecord) and record.id == key:
                return order_from_record(record)
        with self.session.no_autoflush:
            record = self.session.scalar(
                select(MemorialOrderRecord)
                .where(MemorialOrderRecord.id == key)
                .options(selectinload(MemorialOrderRecord.memberships))
                .execution_options(populate_existing=refresh)
            )
        if record is None:
            return None
        # Include transaction-local staged children without forcing an early flush.
        result = order_from_record(record)
        staged = tuple(
            m for m in self.session.new if isinstance(m, MemorialOrderMembershipRecord) and m.order_id == key
        )
        if staged:
            from dataclasses import replace

            from maas_ua.domain.memorial_orders import MemorialOrderMembership

            result = replace(
                result,
                memberships=result.memberships
                + tuple(
                    MemorialOrderMembership(**{k: getattr(m, k) for k in MemorialOrderMembership.__dataclass_fields__})
                    for m in staged
                ),
            )
        return result

    def get_for_update(self, key):
        try:
            with self.session.no_autoflush:
                self.session.execute(
                    select(MemorialOrderRecord.id).where(MemorialOrderRecord.id == key).with_for_update()
                )
            # Separate statement snapshots after any wait; refresh header and children.
            return self.get(key, refresh=True)
        except OperationalError as error:
            if getattr(error.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
                raise RepositoryConflict("concurrent register transaction; retry the complete command") from None
            raise

    def order_for_entry(self, key):
        for record in self.session.new:
            if isinstance(record, MemorialOrderMembershipRecord) and record.entry_id == key:
                return record.order_id
            if isinstance(record, MemorialOrderRecord) and any(m.entry_id == key for m in record.memberships):
                return record.id
        with self.session.no_autoflush:
            return self.session.scalar(
                select(MemorialOrderMembershipRecord.order_id).where(MemorialOrderMembershipRecord.entry_id == key)
            )

    def assign(self, previous, result):
        self.session.add_all([membership_to_record(result, m) for m in result.memberships[len(previous.memberships) :]])

    def finalize(self, previous, result):
        record = self._get(MemorialOrderRecord, previous.id)
        record.status = result.status.value
        record.finalized_by = result.finalized_by
        record.finalization_reference = result.finalization_reference
        record.finalized_at = result.finalized_at
