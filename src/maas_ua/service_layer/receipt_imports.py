"""Typed receipt-import inputs; independent of files, HTTP and ORM sessions."""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from maas_ua.domain.custody import CustodyAssignment
from maas_ua.domain.receipts import (
    AccountingClassification,
    InvalidReceipt,
    Money,
    Nomenclature,
    Quantity,
    RegisteredReceipt,
    SerialNumber,
    TrackingPolicy,
    register_serialized_receipt,
)
from maas_ua.ports import UnitOfWork
from maas_ua.service_layer.receipts import RegisterInventoryReceipt, register_inventory_receipt

IMPORT_TYPE = "serialized_receipt_v1"
MAX_ASSETS = 1000


@dataclass(frozen=True)
class ImportCell:
    sheet: str
    row: int
    column: str


@dataclass(frozen=True)
class ImportIssue:
    field: str
    code: str
    message: str
    cell: ImportCell | None = None


@dataclass(frozen=True, repr=False)
class ImportSerial:
    namespace: str
    value: str
    cell: ImportCell


@dataclass(frozen=True, repr=False)
class ReceiptImportCandidate:
    document_number: str
    document_date: date
    effective_date: date
    source: str
    nomenclature_code: str
    quantity: int
    unit_price: Decimal
    currency: str
    line_total: Decimal
    classification: AccountingClassification
    location_ref: str
    responsible_party_ref: str
    analytics: tuple[tuple[str, str], ...]
    serials: tuple[ImportSerial, ...]
    cells: tuple[tuple[str, ImportCell], ...]

    def cell(self, field: str) -> ImportCell | None:
        return dict(self.cells).get(field)


@dataclass(frozen=True)
class ParsedReceiptImport:
    candidate: ReceiptImportCandidate | None
    issues: tuple[ImportIssue, ...] = ()


@dataclass(frozen=True)
class ValidatedReceiptImport:
    command: RegisterInventoryReceipt | None
    issues: tuple[ImportIssue, ...]


@dataclass(frozen=True)
class SerialMatch:
    serial: SerialNumber
    receipt_id: UUID


@dataclass(frozen=True)
class ReceiptImportFacts:
    nomenclature: Nomenclature | None
    serial_matches: tuple[SerialMatch, ...]
    originals: tuple[RegisteredReceipt, ...]


class ReceiptImportReader(Protocol):
    def inspect(self, candidate: ReceiptImportCandidate, registration_id: UUID) -> ReceiptImportFacts: ...

    def receipt(self, receipt_id: UUID) -> RegisteredReceipt | None: ...


@dataclass(frozen=True)
class ImportResult:
    status: str
    receipt_id: UUID | None = None
    asset_count: int = 0
    issues: tuple[ImportIssue, ...] = ()


@dataclass(frozen=True)
class ImportPreview:
    id: UUID
    registration_id: UUID
    candidate: ReceiptImportCandidate | None
    issues: tuple[ImportIssue, ...]
    expires_at: float
    state: str = "ready"
    result: ImportResult | None = None

    @property
    def ready(self):
        return self.state == "ready" and self.candidate is not None and not self.issues


class PreviewUnavailable(LookupError):
    """Absent, expired, or owned by another session; never disclose which."""


class PreviewBusy(RuntimeError):
    """A confirmation is active, or staging capacity is exhausted."""


class PreviewConsumed(RuntimeError):
    """A terminal confirmation already consumed this preview."""


class PreviewStore(Protocol):
    def create(self, parsed: ParsedReceiptImport, owner: str) -> ImportPreview: ...

    def get(self, preview_id: UUID, owner: str) -> ImportPreview: ...

    def set_issues(self, preview_id: UUID, owner: str, issues: tuple[ImportIssue, ...]) -> ImportPreview: ...

    def claim(self, preview_id: UUID, owner: str) -> ImportPreview: ...

    def finish(self, preview_id: UUID, owner: str, result: ImportResult) -> None: ...

    def discard(self, preview_id: UUID, owner: str) -> None: ...


def validate_import_candidate(
    candidate: ReceiptImportCandidate, catalog: Nomenclature | None
) -> ValidatedReceiptImport:
    """Shared business validation for both transports, without persistence effects."""
    issues = []

    def problem(field, code, message, cell=None):
        issues.append(ImportIssue(field, code, message, cell or candidate.cell(field)))

    if catalog is None or catalog.code != candidate.nomenclature_code:
        problem("nomenclature_code", "missing_nomenclature", "Select an existing registered nomenclature code.")
    elif catalog.tracking_policy is not TrackingPolicy.SERIALIZED or catalog.unit_of_measure != "pcs":
        problem("nomenclature_code", "unsupported_tracking", "The nomenclature must use SERIALIZED tracking and pcs.")
    if candidate.classification is not AccountingClassification.INVENTORY:
        problem(
            "accounting_classification", "inventory_required", "This import requires explicit INVENTORY classification."
        )
    if type(candidate.quantity) is not int or not 1 <= candidate.quantity <= MAX_ASSETS:
        problem("line_quantity", "invalid_quantity", "Use a positive integer quantity of at most 1000.")
    elif candidate.quantity != len(candidate.serials):
        problem("line_quantity", "quantity_mismatch", "Expected quantity must equal the number of serialized rows.")
    for field, value in (
        ("initial_location_ref", candidate.location_ref),
        ("initial_responsible_party_ref", candidate.responsible_party_ref),
    ):
        if not isinstance(value, str) or not value or value != value.strip():
            problem(field, "custody_required", "Complete exact initial custody references are required.")
    keys = Counter((row.namespace, row.value) for row in candidate.serials)
    for row in candidate.serials:
        try:
            SerialNumber(row.namespace, row.value)
        except InvalidReceipt:
            problem("serial_value", "invalid_serial", "Supply an exact nonblank namespace and serial value.", row.cell)
        if keys[(row.namespace, row.value)] > 1:
            problem(
                "serial_value",
                "duplicate_serial",
                "This exact namespace/value pair occurs more than once in the file.",
                row.cell,
            )
    try:
        price, total = Money(candidate.unit_price, candidate.currency), Money(candidate.line_total, candidate.currency)
        if type(candidate.quantity) is int and candidate.quantity > 0 and price.multiply(candidate.quantity) != total:
            problem("line_total", "total_mismatch", "Line total must exactly equal quantity multiplied by unit price.")
    except InvalidReceipt:
        problem("unit_price", "invalid_money", "Supply finite nonnegative decimal amounts and an exact currency.")
    if issues:
        return ValidatedReceiptImport(None, tuple(issues))
    command = RegisterInventoryReceipt(
        document_number=candidate.document_number,
        document_date=candidate.document_date,
        effective_date=candidate.effective_date,
        source=candidate.source,
        nomenclature=catalog,
        quantity=Quantity(candidate.quantity, "pcs"),
        unit_price=price,
        line_total=total,
        serial_numbers=tuple(SerialNumber(row.namespace, row.value) for row in candidate.serials),
        analytics=candidate.analytics,
        initial_custody=CustodyAssignment(candidate.location_ref, candidate.responsible_party_ref),
        accounting_classification=candidate.classification,
    )
    # Reuse existing domain invariants. Building immutable objects performs no posting.
    try:
        register_serialized_receipt(**vars(command), created_at=datetime.now(UTC))
    except InvalidReceipt, ValueError:
        return ValidatedReceiptImport(
            None,
            (ImportIssue("receipt", "invalid_receipt", "The document violates receipt registration requirements."),),
        )
    return ValidatedReceiptImport(command, ())


def original_matches(command: RegisterInventoryReceipt, original: RegisteredReceipt) -> bool:
    """Compare immutable input facts, ignoring mutable custody/lifecycle and UUIDs."""
    document = original.receipt
    if len(document.lines) != 1:
        return False
    line = document.lines[0]
    return (
        (document.document_number, document.document_date, document.effective_date, document.source)
        == (command.document_number, command.document_date, command.effective_date, command.source)
        and (line.nomenclature, line.quantity, line.unit_price, line.accounting_classification)
        == (command.nomenclature, command.quantity, command.unit_price, command.accounting_classification)
        and sorted(line.analytics) == sorted(command.analytics)
        and len(original.units) == command.quantity.count
        and {unit.serial_number for unit in original.units} == set(command.serial_numbers)
        and all(
            unit.receipt_line_id == line.id and unit.initial_custody == command.initial_custody
            for unit in original.units
        )
    )


def _evaluate(candidate, facts, registration_id):
    validated = validate_import_candidate(candidate, facts.nomenclature)
    if validated.issues:
        return validated.command, validated.issues, None
    originals = {item.receipt.id: item for item in facts.originals}
    if registration_id in originals:
        if original_matches(validated.command, originals[registration_id]):
            return validated.command, (), registration_id
        return (
            None,
            (
                ImportIssue(
                    "receipt",
                    "receipt_conflict",
                    "The registration identity already refers to different original receipt facts.",
                ),
            ),
            None,
        )
    origins = {match.receipt_id for match in facts.serial_matches}
    if len(origins) == 1:
        original = originals.get(next(iter(origins)))
        if original is not None and original_matches(validated.command, original):
            return validated.command, (), original.receipt.id
    existing = {match.serial for match in facts.serial_matches}
    issues = tuple(
        ImportIssue(
            "serial_value",
            "serial_conflict",
            "This exact serial identity is already registered with different receipt facts.",
            row.cell,
        )
        for row in candidate.serials
        if SerialNumber(row.namespace, row.value) in existing
    )
    return validated.command, issues, None


class ReceiptImportCoordinator:
    """UI orchestration; the existing receipt service owns the sole write transaction."""

    def __init__(self, reader: ReceiptImportReader, uow_factory: Callable[[], UnitOfWork], previews: PreviewStore):
        self.reader, self.uow_factory, self.previews = reader, uow_factory, previews

    def preview(self, parsed: ParsedReceiptImport, owner: str) -> ImportPreview:
        preview = self.previews.create(parsed, owner)
        issues = parsed.issues
        if parsed.candidate is not None and not issues:
            try:
                _, issues, _ = _evaluate(
                    parsed.candidate,
                    self.reader.inspect(parsed.candidate, preview.registration_id),
                    preview.registration_id,
                )
            except Exception:
                issues = (
                    ImportIssue(
                        "database",
                        "unavailable",
                        "Database validation is unavailable; upload again when it is available.",
                    ),
                )
        return self.previews.set_issues(preview.id, owner, issues)

    def confirm(self, preview_id: UUID, owner: str) -> ImportResult:
        preview = self.previews.claim(preview_id, owner)
        if preview.candidate is None or preview.issues:
            result = ImportResult("rejected", issues=preview.issues)
        else:
            result = self._confirm(preview)
        self.previews.finish(preview_id, owner, result)
        return result

    def _confirm(self, preview):
        candidate = preview.candidate
        try:
            command, issues, match = _evaluate(
                candidate, self.reader.inspect(candidate, preview.registration_id), preview.registration_id
            )
            if issues:
                return ImportResult("rejected", issues=issues)
            if match is not None:
                return ImportResult("already_registered", match, candidate.quantity)
            # First allocation is the receipt UUID. Remaining aggregate UUIDs need
            # not persist across rolled-back attempts; no partial receipt can commit.
            first = True

            def new_id():
                nonlocal first
                if first:
                    first = False
                    return preview.registration_id
                return uuid4()

            registered = register_inventory_receipt(command, self.uow_factory(), new_id=new_id)
            return ImportResult("registered", registered.receipt.id, len(registered.units))
        except Exception:
            # Includes lost responses after a successful commit. Always inspect
            # fresh persisted facts before deciding whether another attempt is safe.
            try:
                _, issues, match = _evaluate(
                    candidate, self.reader.inspect(candidate, preview.registration_id), preview.registration_id
                )
                if match is not None:
                    return ImportResult("already_registered", match, candidate.quantity)
                if issues:
                    return ImportResult("rejected", issues=issues)
            except Exception:
                pass
            return ImportResult(
                "retryable",
                issues=(
                    ImportIssue(
                        "database",
                        "unconfirmed",
                        "Registration could not be verified. Retry this preview to reconcile its stable registration identity.",
                    ),
                ),
            )

    def receipt(self, preview_id: UUID, owner: str) -> RegisteredReceipt | None:
        preview = self.previews.get(preview_id, owner)
        if preview.result is None or preview.result.receipt_id is None:
            return None
        return self.reader.receipt(preview.result.receipt_id)
