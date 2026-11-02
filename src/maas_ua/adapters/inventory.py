"""Set-based historical positions and acquisition-value reports via Core selects."""

from collections import defaultdict
from contextlib import contextmanager
from datetime import date

from sqlalchemy import Text, and_, case, cast, func, literal, not_, or_, select
from sqlalchemy.engine import Engine

from maas_ua.adapters.orm import (
    AssetUnitRecord,
    MovementDocumentRecord,
    MovementLineRecord,
    NomenclatureRecord,
    ReceiptDocumentRecord,
    ReceiptLineRecord,
)
from maas_ua.domain.custody import CustodyAssignment
from maas_ua.domain.receipts import SerialNumber
from maas_ua.service_layer.inventory import (
    AssetDetails,
    AssetEvent,
    AssetHistory,
    AssetNotFound,
    AssetPosition,
    CurrencyTotal,
    GetAssetBySerialNumber,
    GetAssetHistory,
    GetAssetPositionAsOf,
    GetInventoryBalance,
    GetInventoryValuation,
    HistoryDay,
    InvalidInventoryData,
    InventoryDiagnostic,
    InventoryFilter,
    InventoryGroup,
    InventoryReport,
    NomenclatureInfo,
    ReceiptOrigin,
)

assets = AssetUnitRecord.__table__
receipt_lines = ReceiptLineRecord.__table__
receipts = ReceiptDocumentRecord.__table__
nomenclatures = NomenclatureRecord.__table__
movements = MovementDocumentRecord.__table__
movement_lines = MovementLineRecord.__table__

ISSUES = (
    "missing_provenance",
    "missing_initial_custody",
    "unknown_lifecycle",
    "ambiguous_movement_order",
    "broken_custody_chain",
    "movement_before_receipt",
)
ORIGIN_COLUMNS = (
    "receipt_id",
    "line_id",
    "document_number",
    "document_date",
    "effective_date",
    "created_at",
    "source",
    "unit_price",
    "currency",
    "unit_of_measure",
    "analytics",
)
CATALOG_COLUMNS = ("nomenclature_id", "nomenclature_code", "nomenclature_name")


def _base(filters=InventoryFilter(), *, asset_id=None, serial=None):
    statement = select(
        assets.c.id.label("asset_id"),
        assets.c.serial_namespace,
        assets.c.serial_value,
        assets.c.initial_location_ref,
        assets.c.initial_party_ref,
        assets.c.lifecycle_state,
        receipt_lines.c.id.label("line_id"),
        receipts.c.id.label("receipt_id"),
        receipt_lines.c.nomenclature_id,
        nomenclatures.c.code.label("nomenclature_code"),
        nomenclatures.c.name.label("nomenclature_name"),
        receipts.c.document_number,
        receipts.c.document_date,
        receipts.c.effective_date,
        receipts.c.created_at,
        receipts.c.source,
        receipt_lines.c.unit_price,
        receipt_lines.c.currency,
        receipt_lines.c.quantity_unit.label("unit_of_measure"),
        receipt_lines.c.analytics,
    ).select_from(
        assets.outerjoin(receipt_lines, assets.c.receipt_line_id == receipt_lines.c.id)
        .outerjoin(receipts, receipt_lines.c.receipt_id == receipts.c.id)
        .outerjoin(nomenclatures, receipt_lines.c.nomenclature_id == nomenclatures.c.id)
    )
    for column, value in (
        (receipt_lines.c.nomenclature_id, filters.nomenclature_id),
        (receipts.c.id, filters.receipt_id),
        (receipts.c.source, filters.source),
        (receipt_lines.c.currency, filters.currency),
        (assets.c.id, asset_id),
    ):
        if value is not None:
            statement = statement.where(column == value)
    for pair in filters.analytics:
        statement = statement.where(receipt_lines.c.analytics.contains([list(pair)]))
    if serial is not None:
        statement = statement.where(
            assets.c.serial_namespace == serial.namespace, assets.c.serial_value == serial.value
        )
    return statement


def historical_positions(as_of, filters=InventoryFilter(), *, asset_id=None):
    """One row per candidate asset, with explicit integrity/completeness flags.

    Window ordering with legacy NULL ties is deliberately *not* asserted to be
    chronological: ambiguity invalidates the result and hides its destination.
    No current-custody column participates in this query.
    """
    base = _base(filters, asset_id=asset_id).subquery()
    b = select(base).where(or_(base.c.effective_date <= as_of, base.c.effective_date.is_(None))).cte("candidates")
    order = (movements.c.effective_date, movements.c.registration_order.asc().nulls_first())
    movement_events = select(
        movement_lines.c.asset_unit_id.label("asset_id"),
        movements.c.effective_date.label("movement_date"),
        movements.c.source_location_ref,
        movements.c.source_party_ref,
        movements.c.destination_location_ref,
        movements.c.destination_party_ref,
        func.row_number().over(partition_by=movement_lines.c.asset_unit_id, order_by=order).label("ordinal"),
        func.row_number()
        .over(
            partition_by=movement_lines.c.asset_unit_id,
            order_by=(movements.c.effective_date.desc(), movements.c.registration_order.desc().nulls_last()),
        )
        .label("latest"),
        func.lag(movements.c.destination_location_ref)
        .over(partition_by=movement_lines.c.asset_unit_id, order_by=order)
        .label("previous_location"),
        func.lag(movements.c.destination_party_ref)
        .over(partition_by=movement_lines.c.asset_unit_id, order_by=order)
        .label("previous_party"),
        func.count().over(partition_by=(movement_lines.c.asset_unit_id, movements.c.effective_date)).label("day_count"),
        func.count(movements.c.registration_order)
        .over(partition_by=(movement_lines.c.asset_unit_id, movements.c.effective_date))
        .label("ordered_count"),
    ).select_from(
        movement_lines.join(movements, movement_lines.c.movement_id == movements.c.id).join(
            b, movement_lines.c.asset_unit_id == b.c.asset_id
        )
    )
    e = movement_events.where(movements.c.effective_date <= as_of).cte("effective_movements")
    source_mismatch = or_(
        and_(
            e.c.ordinal == 1,
            b.c.initial_location_ref.is_not(None),
            or_(
                e.c.source_location_ref.is_distinct_from(b.c.initial_location_ref),
                e.c.source_party_ref.is_distinct_from(b.c.initial_party_ref),
            ),
        ),
        and_(
            e.c.ordinal > 1,
            or_(
                e.c.source_location_ref.is_distinct_from(e.c.previous_location),
                e.c.source_party_ref.is_distinct_from(e.c.previous_party),
            ),
        ),
    )
    checks = (
        select(
            e.c.asset_id,
            func.bool_or(and_(e.c.day_count > 1, e.c.ordered_count < e.c.day_count)).label("ambiguous"),
            func.bool_or(source_mismatch).label("broken"),
            func.bool_or(e.c.movement_date < b.c.effective_date).label("before_receipt"),
        )
        .select_from(e.join(b, e.c.asset_id == b.c.asset_id))
        .group_by(e.c.asset_id)
        .cte("history_checks")
    )
    last = select(e).where(e.c.latest == 1).subquery("last_movement")
    ambiguous = func.coalesce(checks.c.ambiguous, False)
    location = case((last.c.asset_id.is_not(None), last.c.destination_location_ref), else_=b.c.initial_location_ref)
    party = case((last.c.asset_id.is_not(None), last.c.destination_party_ref), else_=b.c.initial_party_ref)
    known = and_(not_(ambiguous), location.is_not(None), party.is_not(None))
    positions = select(
        *b.c,
        case((known, location), else_=None).label("location_ref"),
        case((known, party), else_=None).label("responsible_party_ref"),
        known.label("position_known"),
        or_(
            b.c.line_id.is_(None),
            b.c.receipt_id.is_(None),
            b.c.nomenclature_code.is_(None),
            b.c.unit_price.is_(None),
            b.c.currency.is_(None),
            b.c.source.is_(None),
            func.btrim(b.c.source) == "",
            func.btrim(b.c.document_number) == "",
            func.btrim(b.c.currency) == "",
        ).label("missing_provenance"),
        or_(b.c.initial_location_ref.is_(None), b.c.initial_party_ref.is_(None)).label("missing_initial_custody"),
        b.c.lifecycle_state.is_distinct_from("ACTIVE").label("unknown_lifecycle"),
        ambiguous.label("ambiguous_movement_order"),
        and_(not_(ambiguous), func.coalesce(checks.c.broken, False)).label("broken_custody_chain"),
        func.coalesce(checks.c.before_receipt, False).label("movement_before_receipt"),
    ).select_from(
        b.outerjoin(checks, checks.c.asset_id == b.c.asset_id).outerjoin(last, last.c.asset_id == b.c.asset_id)
    )
    p = positions.cte("historical_positions")
    scoped = select(p)
    # Unknown positions might match a custody filter. Never silently drop them.
    # Known positions outside it do not contaminate an otherwise valid report.
    for column, value in (
        (p.c.location_ref, filters.location_ref),
        (p.c.responsible_party_ref, filters.responsible_party_ref),
    ):
        if value is not None:
            scoped = scoped.where(or_(not_(p.c.position_known), column == value))
    return scoped.cte("scoped_positions")


def _issues(row):
    return tuple(name for name in ISSUES if row[name])


def _custody(location, party):
    return CustodyAssignment(location, party) if location is not None and party is not None else None


def _origin(row):
    if row["receipt_id"] is None or row["line_id"] is None:
        return None
    try:
        pairs = tuple(tuple(pair) for pair in row["analytics"])
        if any(len(pair) != 2 or any(not isinstance(value, str) for value in pair) for pair in pairs):
            raise ValueError
    except TypeError, ValueError:
        raise InvalidInventoryData("invalid persisted receipt analytics") from None
    return ReceiptOrigin(*(row[name] if name != "analytics" else pairs for name in ORIGIN_COLUMNS))


def _catalog(row):
    if row["nomenclature_code"] is None:
        return None
    return NomenclatureInfo(*(row[name] for name in CATALOG_COLUMNS))


def _details(row):
    return AssetDetails(
        row["asset_id"],
        SerialNumber(row["serial_namespace"], row["serial_value"]),
        _catalog(row),
        _origin(row),
        _custody(row["initial_location_ref"], row["initial_party_ref"]),
        row["lifecycle_state"],
        _issues(row),
    )


class SqlAlchemyInventoryReader:
    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def _snapshot(self):
        # Isolation is selected before autobegin. Connection.close rolls back;
        # queries never commit, flush pending ORM objects, or share a command UoW.
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            yield connection

    def _asset(self, connection, *, asset_id=None, serial=None):
        rows = connection.execute(_base(asset_id=asset_id, serial=serial)).mappings().all()
        if not rows:
            raise AssetNotFound("asset not found")
        if len(rows) != 1:
            raise InvalidInventoryData("ambiguous complete asset key")
        return rows[0]

    def asset_by_serial(self, query: GetAssetBySerialNumber) -> AssetDetails:
        with self._snapshot() as connection:
            row = self._asset(connection, serial=SerialNumber(query.namespace, query.value))
            p = historical_positions(date.max, asset_id=row["asset_id"])
            return _details(connection.execute(select(p)).mappings().one())

    def asset_position(self, query: GetAssetPositionAsOf) -> AssetPosition:
        with self._snapshot() as connection:
            asset = self._asset(connection, asset_id=query.asset_id)
            if asset["effective_date"] is not None and asset["effective_date"] > query.as_of:
                return AssetPosition(query.asset_id, query.as_of, False, None, ())
            p = historical_positions(query.as_of, asset_id=query.asset_id)
            row = connection.execute(select(p)).mappings().one()
            return AssetPosition(
                query.asset_id,
                query.as_of,
                True if row["effective_date"] is not None else None,
                _custody(row["location_ref"], row["responsible_party_ref"]),
                _issues(row),
            )

    def asset_history(self, query: GetAssetHistory) -> AssetHistory:
        with self._snapshot() as connection:
            self._asset(connection, asset_id=query.asset_id)
            p = historical_positions(date.max, asset_id=query.asset_id)
            detail = _details(connection.execute(select(p)).mappings().one())
            days = defaultdict(list)
            if detail.origin is not None:
                origin = detail.origin
                days[origin.effective_date].append(
                    AssetEvent(
                        "RECEIPT",
                        origin.receipt_id,
                        origin.document_number,
                        origin.document_date,
                        origin.effective_date,
                        origin.created_at,
                        None,
                        detail.initial_custody,
                        None,
                    )
                )
            statement = (
                select(movements)
                .select_from(movements.join(movement_lines, movements.c.id == movement_lines.c.movement_id))
                .where(movement_lines.c.asset_unit_id == query.asset_id)
                .order_by(movements.c.effective_date, movements.c.registration_order)
            )
            for row in connection.execute(statement).mappings():
                days[row["effective_date"]].append(
                    AssetEvent(
                        "TRANSFER",
                        row["id"],
                        row["document_number"],
                        row["document_date"],
                        row["effective_date"],
                        row["created_at"],
                        _custody(row["source_location_ref"], row["source_party_ref"]),
                        _custody(row["destination_location_ref"], row["destination_party_ref"]),
                        row["registration_order"],
                    )
                )
            result = []
            for day, events in sorted(days.items()):
                transfers = [event for event in events if event.kind == "TRANSFER"]
                ordered = len(transfers) <= 1 or all(event.registration_order is not None for event in transfers)
                # For an ambiguous day, tuple order is presentation only. No UUID
                # or timestamp is used to manufacture a chronological sequence.
                result.append(HistoryDay(day, tuple(events), ordered))
            return AssetHistory(detail, tuple(result), detail.issues)

    def inventory_balance(self, query: GetInventoryBalance) -> InventoryReport:
        return self._inventory(query)

    def inventory_valuation(self, query: GetInventoryValuation) -> InventoryReport:
        return self._inventory(query)

    def _inventory(self, query):
        p = historical_positions(query.as_of, query.filters)
        with self._snapshot() as connection:
            issue_columns = [p.c[name] for name in ISSUES]
            diagnostics = []
            statement = (
                select(
                    *issue_columns, p.c.currency, func.count().label("count"), func.sum(p.c.unit_price).label("value")
                )
                .group_by(*issue_columns, p.c.currency)
                .order_by(*issue_columns, p.c.currency)
            )
            for row in connection.execute(statement).mappings():
                issues = _issues(row)
                if issues:
                    diagnostics.append(InventoryDiagnostic(issues, row["count"], row["currency"], row["value"]))
            totals = (
                connection.execute(
                    select(p.c.currency, func.sum(p.c.unit_price).label("value"), func.count().label("count"))
                    .group_by(p.c.currency)
                    .order_by(p.c.currency)
                )
                .mappings()
                .all()
            )
            candidate_count = sum(row["count"] for row in totals)
            known_totals = tuple(
                CurrencyTotal(row["currency"], row["value"])
                for row in totals
                if row["currency"] is not None and row["value"] is not None
            )
            groups = []
            if not diagnostics:
                columns = [p.c[name] for name in (*ORIGIN_COLUMNS, *CATALOG_COLUMNS)]
                columns += [
                    p.c.location_ref
                    if "location" in query.group_by
                    else cast(literal(None), Text).label("location_ref"),
                    p.c.responsible_party_ref
                    if "custodian" in query.group_by
                    else cast(literal(None), Text).label("responsible_party_ref"),
                ]
                statement = (
                    select(*columns, func.count().label("count"), func.sum(p.c.unit_price).label("value"))
                    .group_by(*columns)
                    .order_by(p.c.receipt_id, p.c.line_id, "location_ref", "responsible_party_ref")
                )
                for row in connection.execute(statement).mappings():
                    groups.append(
                        InventoryGroup(
                            _catalog(row),
                            _origin(row),
                            row["location_ref"],
                            row["responsible_party_ref"],
                            row["count"],
                            row["value"],
                        )
                    )
            return InventoryReport(
                query.as_of,
                query.group_by,
                tuple(groups),
                tuple(diagnostics),
                candidate_count,
                known_totals,
            )
