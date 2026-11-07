"""Register aggregate fakes use the shared atomic snapshot publication."""

from maas_ua.domain.memorial_orders import DuplicateMemorialOrder, DuplicatePrimaryMembership


class FakeMemorialOrderRepository:
    def __init__(self, types, scopes, orders):
        self._types, self._scopes, self._orders = dict(types), dict(scopes), dict(orders)

    def add_type(self, kind):
        def identity(k):
            return (k.form_identifier, k.variant, k.revision, k.authority_reference)

        if kind.id in self._types or any(identity(k) == identity(kind) for k in self._types.values()):
            raise DuplicateMemorialOrder("form revision already registered")
        self._types[kind.id] = kind

    def get_type(self, key):
        return self._types.get(key)

    def add_scope(self, scope):
        if scope.id in self._scopes or any(
            (s.ledger_id, s.namespace) == (scope.ledger_id, scope.namespace) for s in self._scopes.values()
        ):
            raise DuplicateMemorialOrder("numbering namespace already registered")
        self._scopes[scope.id] = scope

    def get_scope(self, key):
        return self._scopes.get(key)

    def add(self, order):
        if order.id in self._orders or any(o.number == order.number for o in self._orders.values()):
            raise DuplicateMemorialOrder("register identity already registered")
        self._orders[order.id] = order

    def get(self, key):
        return self._orders.get(key)

    def get_for_update(self, key):
        return self.get(key)

    def order_for_entry(self, entry_id):
        return next((o.id for o in self._orders.values() if any(m.entry_id == entry_id for m in o.memberships)), None)

    def assign(self, previous, result):
        for membership in result.memberships[len(previous.memberships) :]:
            if self.order_for_entry(membership.entry_id) is not None:
                raise DuplicatePrimaryMembership("entry already assigned to a primary register")
        self._orders[result.id] = result

    def finalize(self, previous, result):
        self._orders[result.id] = result
