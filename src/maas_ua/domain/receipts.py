"""Complete serialized receipts; no drafts or financial postings.

Provisional in-memory rules: serials compare exactly within a caller-supplied
namespace. Money uses exact Decimal arithmetic without currency quantization;
zero amounts are representable. These are not approved accounting policies.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from uuid import UUID, uuid4

from maas_ua.domain.custody import CustodyAssignment, LifecycleState


class InvalidReceipt(ValueError):
    """The supplied receipt violates a registration invariant."""


class DuplicateSerialNumber(InvalidReceipt):
    """A serial identifier is already used in its namespace."""


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InvalidReceipt(f"{field} must be nonblank text without outer whitespace")


def _require_id(value: UUID) -> None:
    if not isinstance(value, UUID):
        raise InvalidReceipt("internal identities must be UUIDs")


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal) or not self.amount.is_finite() or self.amount < 0:
            raise InvalidReceipt("money must be a finite nonnegative Decimal")
        _require_text(self.currency, "currency")

    def multiply(self, count: int) -> Money:
        # Multiplication by an integer needs no monetary rounding. Increase
        # precision so the caller's Decimal context cannot silently round it.
        with localcontext() as context:
            context.prec = len(self.amount.as_tuple().digits) + len(str(count)) + 1
            return Money(self.amount * count, self.currency)


@dataclass(frozen=True)
class Quantity:
    count: int
    unit: str

    def __post_init__(self) -> None:
        if type(self.count) is not int or self.count <= 0:
            raise InvalidReceipt("serialized quantity must be a positive integer")
        _require_text(self.unit, "unit of measure")


class AccountingClassification(StrEnum):
    INVENTORY = "INVENTORY"
    FIXED_ASSET = "FIXED_ASSET"


class TrackingPolicy(StrEnum):
    SERIALIZED = "SERIALIZED"
    QUANTITY = "QUANTITY"


@dataclass(frozen=True)
class Nomenclature:
    id: str
    tracking_policy: TrackingPolicy
    code: str = field(kw_only=True)
    name: str = field(kw_only=True)
    unit_of_measure: str = field(kw_only=True)
    default_accounting_classification: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        _require_text(self.id, "nomenclature ID")
        _require_text(self.code, "nomenclature code")
        _require_text(self.name, "nomenclature name")
        _require_text(self.unit_of_measure, "unit of measure")
        if self.default_accounting_classification is not None:
            _require_text(self.default_accounting_classification, "default accounting classification")
        if not isinstance(self.tracking_policy, TrackingPolicy):
            raise InvalidReceipt("nomenclature needs an explicit tracking policy")


@dataclass(frozen=True)
class SerialNumber:
    namespace: str
    value: str

    def __post_init__(self) -> None:
        _require_text(self.namespace, "serial namespace")
        _require_text(self.value, "serial number")


@dataclass(frozen=True)
class ReceiptLine:
    id: UUID
    nomenclature: Nomenclature
    quantity: Quantity
    unit_price: Money
    analytics: tuple[tuple[str, str], ...] = ()
    accounting_classification: AccountingClassification | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        _require_id(self.id)
        if self.accounting_classification is not None and not isinstance(
            self.accounting_classification, AccountingClassification
        ):
            raise InvalidReceipt("receipt classification must be explicit or unknown")
        if not isinstance(self.nomenclature, Nomenclature) or (
            self.nomenclature.tracking_policy is not TrackingPolicy.SERIALIZED
        ):
            raise InvalidReceipt("this receipt requires serialized nomenclature")
        if not isinstance(self.quantity, Quantity) or (
            self.quantity.unit != "pcs" or self.quantity.unit != self.nomenclature.unit_of_measure
        ):
            raise InvalidReceipt("serialized receipt quantity must use pcs")
        if not isinstance(self.unit_price, Money):
            raise InvalidReceipt("unit price must be Money")
        if not isinstance(self.analytics, (tuple, list)):
            raise InvalidReceipt("analytics must be a collection of name/value pairs")
        dimensions = []
        for dimension in self.analytics:
            if not isinstance(dimension, (tuple, list)) or len(dimension) != 2:
                raise InvalidReceipt("analytics must be name/value pairs")
            key, value = dimension
            _require_text(key, "analytic name")
            _require_text(value, "analytic value")
            dimensions.append((key, value))
        if len({key for key, _ in dimensions}) != len(dimensions):
            raise InvalidReceipt("duplicate analytic name")
        object.__setattr__(self, "analytics", tuple(dimensions))

    @property
    def total(self) -> Money:
        return self.unit_price.multiply(self.quantity.count)


@dataclass(frozen=True)
class ReceiptDocument:
    id: UUID
    document_number: str
    document_date: date
    effective_date: date
    created_at: datetime
    source: str
    lines: tuple[ReceiptLine, ...]

    def __post_init__(self) -> None:
        _require_id(self.id)
        _require_text(self.document_number, "document number")
        _require_text(self.source, "receipt source")
        if type(self.document_date) is not date or type(self.effective_date) is not date:
            raise InvalidReceipt("document and effective dates must be dates")
        if not isinstance(self.created_at, datetime) or self.created_at.utcoffset() is None:
            raise InvalidReceipt("creation timestamp must be timezone-aware")
        lines = tuple(self.lines)
        if not lines or any(not isinstance(line, ReceiptLine) for line in lines):
            raise InvalidReceipt("receipt requires valid lines")
        if len({line.id for line in lines}) != len(lines):
            raise InvalidReceipt("duplicate receipt line identity")
        object.__setattr__(self, "lines", lines)


@dataclass(frozen=True)
class AssetUnit:
    id: UUID
    serial_number: SerialNumber
    receipt_line_id: UUID
    custody: CustodyAssignment | None = None
    lifecycle_state: LifecycleState | None = LifecycleState.ACTIVE
    last_effective_date: date | None = None
    initial_custody: CustodyAssignment | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        _require_id(self.id)
        _require_id(self.receipt_line_id)
        if self.custody is not None and not isinstance(self.custody, CustodyAssignment):
            raise InvalidReceipt("custody must be an assignment or unknown")
        if self.initial_custody is not None and not isinstance(self.initial_custody, CustodyAssignment):
            raise InvalidReceipt("initial custody must be an assignment or unknown")
        if self.lifecycle_state is not None and not isinstance(self.lifecycle_state, LifecycleState):
            raise InvalidReceipt("lifecycle state must be explicit or unknown")
        if self.last_effective_date is not None and type(self.last_effective_date) is not date:
            raise InvalidReceipt("last effective date must be a date or unknown")
        if not isinstance(self.serial_number, SerialNumber):
            raise InvalidReceipt("unit requires a serial identifier")


@dataclass(frozen=True)
class RegisteredReceipt:
    receipt: ReceiptDocument
    units: tuple[AssetUnit, ...]


def register_serialized_receipt(
    *,
    document_number: str,
    document_date: date,
    effective_date: date,
    created_at: datetime,
    source: str,
    nomenclature: Nomenclature,
    quantity: Quantity,
    unit_price: Money,
    line_total: Money,
    serial_numbers: Iterable[SerialNumber],
    analytics: tuple[tuple[str, str], ...] = (),
    initial_custody: CustodyAssignment | None = None,
    accounting_classification: AccountingClassification | None = None,
    new_id: Callable[[], UUID] = uuid4,
) -> RegisteredReceipt:
    """Build a complete receipt and its units, with no persistence side effects."""
    receipt_id = new_id()
    line = ReceiptLine(
        new_id(), nomenclature, quantity, unit_price, analytics, accounting_classification=accounting_classification
    )
    receipt = ReceiptDocument(
        receipt_id,
        document_number,
        document_date,
        effective_date,
        created_at,
        source,
        (line,),
    )
    if not isinstance(line_total, Money) or line.total != line_total:
        raise InvalidReceipt("line total must equal quantity times unit price in one currency")
    if not isinstance(serial_numbers, Iterable):
        raise InvalidReceipt("serial identifiers must be an iterable collection")
    serials = tuple(serial_numbers)
    if len(serials) != quantity.count:
        raise InvalidReceipt("serial count must equal receipt quantity")
    if any(not isinstance(serial, SerialNumber) for serial in serials):
        raise InvalidReceipt("all units require serial identifiers")
    if len(set(serials)) != len(serials):
        raise DuplicateSerialNumber("duplicate serial in namespace")
    units = tuple(
        AssetUnit(
            new_id(),
            serial,
            line.id,
            custody=initial_custody,
            initial_custody=initial_custody,
            last_effective_date=effective_date,
        )
        for serial in serials
    )
    identities = {receipt.id, line.id, *(unit.id for unit in units)}
    if len(identities) != len(units) + 2:
        raise InvalidReceipt("generated internal identities must be distinct")
    return RegisteredReceipt(receipt, units)
