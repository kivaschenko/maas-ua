"""Synthetic registers and independently authorized posting components."""

from datetime import date
from uuid import uuid4

from accounting_fixtures import NOW

from maas_ua.domain.memorial_orders import MemorialOrder, MemorialOrderNumber, MemorialOrderType, NumberingScope


def order_type(**changes):
    return MemorialOrderType(
        **(
            dict(
                id=uuid4(),
                form_identifier="EXAMPLE-17",
                variant="EXAMPLE-AUT",
                revision="EXAMPLE-REVISION",
                authority_reference="EXAMPLE-AUTHORITY",
                actor_ref="EXAMPLE",
                authorization_reference="EXAMPLE-APPROVAL",
                created_at=NOW,
            )
            | changes
        )
    )


def numbering_scope(ledger_id, **changes):
    return NumberingScope(
        **(
            dict(
                id=uuid4(),
                ledger_id=ledger_id,
                namespace="EXAMPLE-SERIES/2026",
                policy_reference="EXAMPLE-REVIEWED-NUMBERING-POLICY",
                actor_ref="EXAMPLE",
                authorization_reference="EXAMPLE-APPROVAL",
                created_at=NOW,
            )
            | changes
        )
    )


def order(ledger, calendar, kind=None, scope=None, **changes):
    kind, scope = kind or order_type(), scope or numbering_scope(ledger.id)
    return MemorialOrder(
        **(
            dict(
                id=uuid4(),
                type_id=kind.id,
                ledger_id=ledger.id,
                period_id=calendar.id,
                coverage_start=date(2026, 2, 1),
                coverage_end=date(2026, 3, 1),
                number=MemorialOrderNumber(scope.id, "EXAMPLE-001"),
                actor_ref="EXAMPLE",
                authorization_reference="EXAMPLE-APPROVAL",
                created_at=NOW,
            )
            | changes
        )
    )


def create_command(register):
    from maas_ua.service_layer.memorial_orders import CreateMemorialOrder

    return CreateMemorialOrder(**{key: getattr(register, key) for key in CreateMemorialOrder.__dataclass_fields__})


def assignment(register, entries):
    from maas_ua.service_layer.memorial_orders import AssignJournalEntries

    return AssignJournalEntries(
        order_id=register.id,
        entry_ids=tuple(e.id for e in entries),
        actor_ref="EXAMPLE",
        authorization_reference="EXAMPLE-ASSIGN",
    )


def finalization(register):
    from maas_ua.service_layer.memorial_orders import FinalizeMemorialOrder

    return FinalizeMemorialOrder(order_id=register.id, actor_ref="EXAMPLE", authorization_reference="EXAMPLE-FINALIZE")
