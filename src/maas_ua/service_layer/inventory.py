"""Immutable read contracts. No sessions, aggregate loading, or command UoW."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from maas_ua.domain.custody import CustodyAssignment, LifecycleState
from maas_ua.domain.receipts import InvalidReceipt, SerialNumber


class InvalidInventoryQuery(ValueError):
    pass


class AssetNotFound(LookupError):
    pass


class InvalidInventoryData(RuntimeError):
    pass


class IncompleteInventory(RuntimeError):
    """A report with diagnostics cannot expose verified totals."""


def _text(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise InvalidInventoryQuery("filters require nonblank exact text")


def _id(value):
    if not isinstance(value, UUID):
        raise InvalidInventoryQuery("asset/document identity must be a UUID")


def _date(value):
    if type(value) is not date:
        raise InvalidInventoryQuery("as_of must be a business date, not a timestamp")


@dataclass(frozen=True, kw_only=True)
class InventoryFilter:
    nomenclature_id: str | None = None
    receipt_id: UUID | None = None
    source: str | None = None
    currency: str | None = None
    location_ref: str | None = None
    responsible_party_ref: str | None = None
    analytics: tuple[tuple[str, str], ...] = ()
    lifecycle_state: LifecycleState | None = LifecycleState.ACTIVE

    def __post_init__(self):
        if self.lifecycle_state is not None and not isinstance(self.lifecycle_state, LifecycleState):
            raise InvalidInventoryQuery("lifecycle filter must be an explicit state or None")
        for value in (self.nomenclature_id, self.source, self.currency, self.location_ref, self.responsible_party_ref):
            if value is not None:
                _text(value)
        if self.receipt_id is not None:
            _id(self.receipt_id)
        try:
            pairs = tuple(tuple(pair) for pair in self.analytics)
            for key, value in pairs:
                _text(key)
                _text(value)
        except (TypeError, ValueError) as error:
            raise InvalidInventoryQuery("analytics require name/value pairs") from error
        if len({key for key, _ in pairs}) != len(pairs):
            raise InvalidInventoryQuery("duplicate analytic filter")
        object.__setattr__(self, "analytics", pairs)


@dataclass(frozen=True, kw_only=True)
class GetAssetBySerialNumber:
    namespace: str
    value: str

    def __post_init__(self):
        try:
            SerialNumber(self.namespace, self.value)
        except InvalidReceipt as error:
            raise InvalidInventoryQuery("a complete exact serial key is required") from error


@dataclass(frozen=True, kw_only=True)
class GetAssetHistory:
    asset_id: UUID

    def __post_init__(self):
        _id(self.asset_id)


@dataclass(frozen=True, kw_only=True)
class GetAssetPositionAsOf(GetAssetHistory):
    as_of: date

    def __post_init__(self):
        super().__post_init__()
        _date(self.as_of)


@dataclass(frozen=True, kw_only=True)
class GetInventoryBalance:
    as_of: date
    filters: InventoryFilter = InventoryFilter()
    # Receipt-line origin, price, currency, nomenclature and analytics are always
    # retained. Callers can choose whether to split that origin by each custody axis.
    group_by: tuple[str, ...] = ("location", "custodian")

    def __post_init__(self):
        _date(self.as_of)
        if not isinstance(self.filters, InventoryFilter):
            raise InvalidInventoryQuery("filters must be InventoryFilter")
        if not isinstance(self.group_by, (list, tuple)):
            raise InvalidInventoryQuery("group_by must be a collection")
        groups = tuple(self.group_by)
        if any(value not in {"location", "custodian"} for value in groups) or len(set(groups)) != len(groups):
            raise InvalidInventoryQuery("group_by supports distinct location/custodian dimensions")
        object.__setattr__(self, "group_by", groups)


@dataclass(frozen=True, kw_only=True)
class GetInventoryValuation(GetInventoryBalance):
    pass


@dataclass(frozen=True)
class NomenclatureInfo:
    id: str
    code: str
    name: str


@dataclass(frozen=True)
class ReceiptOrigin:
    receipt_id: UUID
    line_id: UUID
    document_number: str
    document_date: date
    effective_date: date
    created_at: datetime
    source: str
    unit_price: Decimal
    currency: str
    unit_of_measure: str
    analytics: tuple[tuple[str, str], ...]
    accounting_classification: str | None = None


@dataclass(frozen=True)
class WriteOffInfo:
    document_id: UUID
    document_number: str
    document_date: date
    authorization_reference: str
    authorization_date: date
    effective_date: date
    created_at: datetime


@dataclass(frozen=True)
class AssetDetails:
    asset_id: UUID
    serial_number: SerialNumber
    nomenclature: NomenclatureInfo | None
    origin: ReceiptOrigin | None
    initial_custody: CustodyAssignment | None
    lifecycle_state: str | None
    issues: tuple[str, ...]
    write_off: WriteOffInfo | None = None

    @property
    def complete(self):
        return not self.issues


@dataclass(frozen=True)
class AssetEvent:
    kind: str
    document_id: UUID
    document_number: str
    document_date: date
    effective_date: date
    created_at: datetime
    source: CustodyAssignment | None
    destination: CustodyAssignment | None
    registration_order: int | None
    authorization_reference: str | None = None
    authorization_date: date | None = None


@dataclass(frozen=True)
class HistoryDay:
    effective_date: date
    events: tuple[AssetEvent, ...]
    ordered: bool


@dataclass(frozen=True)
class AssetHistory:
    asset: AssetDetails
    days: tuple[HistoryDay, ...]
    issues: tuple[str, ...]

    @property
    def complete(self):
        return not self.issues


@dataclass(frozen=True)
class AssetPosition:
    asset_id: UUID
    as_of: date
    registered: bool | None
    custody: CustodyAssignment | None
    issues: tuple[str, ...]
    lifecycle_state: str | None = None
    write_off: WriteOffInfo | None = None

    @property
    def complete(self):
        return not self.issues


@dataclass(frozen=True)
class CurrencyTotal:
    currency: str
    acquisition_value: Decimal


@dataclass(frozen=True)
class InventoryGroup:
    nomenclature: NomenclatureInfo
    origin: ReceiptOrigin
    location_ref: str | None
    responsible_party_ref: str | None
    count: int
    acquisition_value: Decimal
    lifecycle_state: str | None = None


@dataclass(frozen=True)
class InventoryDiagnostic:
    issues: tuple[str, ...]
    count: int
    currency: str | None
    known_acquisition_value: Decimal | None


@dataclass(frozen=True)
class InventoryReport:
    as_of: date
    group_by: tuple[str, ...]
    groups: tuple[InventoryGroup, ...]
    diagnostics: tuple[InventoryDiagnostic, ...]
    candidate_count: int
    known_acquisition_totals: tuple[CurrencyTotal, ...]

    @property
    def complete(self):
        return not self.diagnostics

    @property
    def count(self):
        if not self.complete:
            raise IncompleteInventory("candidate count is diagnostic, not a verified balance")
        return self.candidate_count

    @property
    def totals(self):
        if not self.complete:
            raise IncompleteInventory("known acquisition values are diagnostic, not verified totals")
        return self.known_acquisition_totals


class InventoryReader(Protocol):
    def asset_by_serial(self, query: GetAssetBySerialNumber) -> AssetDetails: ...
    def asset_history(self, query: GetAssetHistory) -> AssetHistory: ...
    def asset_position(self, query: GetAssetPositionAsOf) -> AssetPosition: ...
    def inventory_balance(self, query: GetInventoryBalance) -> InventoryReport: ...
    def inventory_valuation(self, query: GetInventoryValuation) -> InventoryReport: ...


def get_asset_by_serial_number(query: GetAssetBySerialNumber, reader: InventoryReader) -> AssetDetails:
    return reader.asset_by_serial(query)


def get_asset_history(query: GetAssetHistory, reader: InventoryReader) -> AssetHistory:
    return reader.asset_history(query)


def get_asset_position_as_of(query: GetAssetPositionAsOf, reader: InventoryReader) -> AssetPosition:
    return reader.asset_position(query)


def get_inventory_balance(query: GetInventoryBalance, reader: InventoryReader) -> InventoryReport:
    return reader.inventory_balance(query)


def get_inventory_valuation(query: GetInventoryValuation, reader: InventoryReader) -> InventoryReport:
    return reader.inventory_valuation(query)
