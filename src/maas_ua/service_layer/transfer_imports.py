"""Serial-based transfer orchestration; the existing transfer service owns writes."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol
from uuid import UUID

from maas_ua.domain.custody import CustodyAssignment
from maas_ua.domain.movements import InvalidTransfer, MovementDocument
from maas_ua.domain.movements import transfer_assets as build_transfer
from maas_ua.domain.receipts import AssetUnit
from maas_ua.service_layer.movements import TransferAssets, transfer_assets
from maas_ua.service_layer.receipt_imports import (
    MAX_ASSETS,
    ImportCell,
    ImportIssue,
    ImportPreview,
    ImportSerial,
    PreviewBusy,
)

IMPORT_TYPE = "serialized_transfer_v1"


@dataclass(frozen=True, repr=False)
class TransferImportCandidate:
    document_number: str
    document_date: date
    effective_date: date
    source: CustodyAssignment
    destination: CustodyAssignment
    serials: tuple[ImportSerial, ...]
    cells: tuple[tuple[str, ImportCell], ...]


@dataclass(frozen=True)
class ParsedTransferImport:
    candidate: TransferImportCandidate | None
    issues: tuple[ImportIssue, ...] = ()


@dataclass(frozen=True, repr=False)
class ResolvedTransferAsset:
    unit: AssetUnit
    nomenclature_code: str
    nomenclature_name: str


@dataclass(frozen=True)
class TransferImportFacts:
    assets: tuple[ResolvedTransferAsset, ...]
    original: MovementDocument | None


class TransferImportReader(Protocol):
    def inspect(self, candidate: TransferImportCandidate, movement_id: UUID) -> TransferImportFacts: ...


@dataclass(frozen=True)
class TransferImportResult:
    status: str
    movement_id: UUID | None = None
    asset_ids: tuple[UUID, ...] = ()
    issues: tuple[ImportIssue, ...] = ()


@dataclass(frozen=True)
class TransferImportPreview(ImportPreview):
    candidate: TransferImportCandidate | None
    resolved: tuple[ResolvedTransferAsset, ...] = ()


def original_matches(command: TransferAssets, document: MovementDocument) -> bool:
    """Only original immutable facts decide retry success, never present custody."""
    return (
        (
            document.document_number,
            document.document_date,
            document.effective_date,
            document.source,
            document.destination,
        )
        == (command.document_number, command.document_date, command.effective_date, command.source, command.destination)
        and len(document.lines) == len(command.asset_ids)
        and {line.asset_unit_id for line in document.lines} == set(command.asset_ids)
    )


def evaluate(candidate, facts, movement_id):
    issues, resolved = [], []
    counts = Counter((row.namespace, row.value) for row in candidate.serials)
    by_serial = {}
    for asset in facts.assets:
        serial = asset.unit.serial_number
        by_serial.setdefault((serial.namespace, serial.value), []).append(asset)
    for row in candidate.serials:
        key = (row.namespace, row.value)
        if counts[key] > 1:
            issues.append(
                ImportIssue(
                    "serial_value", "duplicate_serial", "Цей точний серійний ідентифікатор повторено у файлі.", row.cell
                )
            )
        matches = by_serial.get(key, [])
        if len(matches) != 1:
            issues.append(
                ImportIssue(
                    "serial_value",
                    "missing_serial" if not matches else "ambiguous_serial",
                    "Майно не знайдено за точним ідентифікатором."
                    if not matches
                    else "Серійний ідентифікатор неоднозначний.",
                    row.cell,
                )
            )
        else:
            resolved.append(matches[0])
    if not 1 <= len(candidate.serials) <= MAX_ASSETS:
        issues.append(ImportIssue("serials", "asset_count", "Вкажіть від 1 до 1000 одиниць майна."))
    command = TransferAssets(
        movement_id=movement_id,
        document_number=candidate.document_number,
        document_date=candidate.document_date,
        effective_date=candidate.effective_date,
        source=candidate.source,
        destination=candidate.destination,
        asset_ids=tuple(asset.unit.id for asset in resolved),
    )
    if issues:
        return command, tuple(issues), False, tuple(resolved)
    if facts.original is not None:
        if original_matches(command, facts.original):
            return command, (), True, tuple(resolved)
        return (
            command,
            (ImportIssue("document", "movement_conflict", "Технічний ідентифікатор уже належить іншому переміщенню."),),
            False,
            tuple(resolved),
        )
    # Validate each row through the same domain operation as the final write.
    # Pure immutable construction does not alter custody or create history.
    for row, asset in zip(candidate.serials, resolved, strict=True):
        try:
            build_transfer(
                id=movement_id,
                document_number=command.document_number,
                document_date=command.document_date,
                effective_date=command.effective_date,
                source=command.source,
                destination=command.destination,
                asset_ids=(asset.unit.id,),
                assets=(asset.unit,),
                created_at=datetime.now(UTC),
            )
        except InvalidTransfer as error:
            # Translate fixed domain diagnostics, never database exception text.
            messages = {
                "only explicitly active units are eligible": "Майно має бути активним. Списане майно переміщувати не можна.",
                "source must match current assigned custody for every unit": "Поточний утримувач невідомий або не відповідає зазначеному джерелу.",
                "backdated transfers or units without an effective date are unsupported": "Дата переміщення передує останньому руху або попередня дата невідома.",
                "source and destination must differ": "Джерело та призначення мають відрізнятися.",
            }
            issues.append(
                ImportIssue(
                    "asset",
                    "ineligible_asset",
                    messages.get(str(error), "Документ не відповідає вимогам переміщення."),
                    row.cell,
                )
            )
    return command, tuple(issues), False, tuple(resolved)


class TransferImportCoordinator:
    def __init__(self, reader, uow_factory, previews):
        self.reader, self.uow_factory, self.previews = reader, uow_factory, previews

    def preview(self, parsed, owner):
        preview = self.previews.create(parsed, owner)
        issues, resolved = parsed.issues, ()
        if parsed.candidate is not None and not issues:
            try:
                _, issues, _, resolved = evaluate(
                    parsed.candidate,
                    self.reader.inspect(parsed.candidate, preview.registration_id),
                    preview.registration_id,
                )
            except Exception:
                issues = (ImportIssue("database", "unavailable", "Перевірка недоступна. Завантажте файл пізніше."),)
        try:
            return self.previews.set_validation(preview.id, owner, issues, resolved)
        except PreviewBusy:
            self.previews.discard(preview.id, owner)
            raise

    def confirm(self, preview_id, owner):
        preview = self.previews.claim(preview_id, owner)
        result = TransferImportResult("rejected", issues=preview.issues) if preview.issues else self._confirm(preview)
        self.previews.finish(preview_id, owner, result)
        return result

    def _confirm(self, preview):
        candidate = preview.candidate
        if candidate is None:
            return TransferImportResult("rejected")
        try:
            command, issues, matched, _ = evaluate(
                candidate, self.reader.inspect(candidate, preview.registration_id), preview.registration_id
            )
            if issues:
                return TransferImportResult("rejected", issues=issues)
            if matched:
                return TransferImportResult("already_registered", preview.registration_id, command.asset_ids)
            result = transfer_assets(command, self.uow_factory())
            return TransferImportResult("registered", result.movement.id, command.asset_ids)
        except Exception:
            # A commit response can be lost. Reconcile only the retained UUID and
            # complete immutable membership, including after subsequent movements.
            try:
                command, issues, matched, _ = evaluate(
                    candidate, self.reader.inspect(candidate, preview.registration_id), preview.registration_id
                )
                if matched:
                    return TransferImportResult("already_registered", preview.registration_id, command.asset_ids)
                if issues:
                    return TransferImportResult("rejected", issues=issues)
            except Exception:
                pass
            return TransferImportResult(
                "retryable",
                issues=(
                    ImportIssue(
                        "database",
                        "unconfirmed",
                        "Результат не підтверджено. Повторіть підтвердження цього перегляду зі збереженим ідентифікатором.",
                    ),
                ),
            )
