"""Serial-based write-off orchestration; the existing write-off service owns writes."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from typing import Protocol
from uuid import UUID

from maas_ua.domain.receipts import Money
from maas_ua.domain.write_offs import InvalidWriteOff, WriteOffAsset, WriteOffDocument
from maas_ua.domain.write_offs import write_off_assets as build_write_off
from maas_ua.service_layer.receipt_imports import (
    MAX_ASSETS,
    ImportCell,
    ImportIssue,
    ImportPreview,
    ImportSerial,
    PreviewBusy,
)
from maas_ua.service_layer.write_offs import WriteOffAssets, write_off_assets

IMPORT_TYPE = "serialized_inventory_write_off_v1"


@dataclass(frozen=True, repr=False)
class WriteOffImportCandidate:
    document_number: str
    document_date: date
    effective_date: date
    authorization_reference: str
    authorization_date: date
    serials: tuple[ImportSerial, ...]
    cells: tuple[tuple[str, ImportCell], ...]


@dataclass(frozen=True)
class ParsedWriteOffImport:
    candidate: WriteOffImportCandidate | None
    issues: tuple[ImportIssue, ...] = ()


@dataclass(frozen=True, repr=False)
class ResolvedWriteOffAsset:
    facts: WriteOffAsset
    nomenclature_code: str
    nomenclature_name: str
    acquisition_price: Money | None = None

    @property
    def unit(self):
        return self.facts.unit


@dataclass(frozen=True)
class WriteOffImportFacts:
    assets: tuple[ResolvedWriteOffAsset, ...]
    original: WriteOffDocument | None


class WriteOffImportReader(Protocol):
    def inspect(self, candidate: WriteOffImportCandidate, write_off_id: UUID) -> WriteOffImportFacts: ...


@dataclass(frozen=True)
class WriteOffImportResult:
    status: str
    write_off_id: UUID | None = None
    asset_ids: tuple[UUID, ...] = ()
    issues: tuple[ImportIssue, ...] = ()
    document_number: str | None = None
    effective_date: date | None = None
    acquisition_values: tuple[Money, ...] = ()


@dataclass(frozen=True)
class WriteOffImportPreview(ImportPreview):
    candidate: WriteOffImportCandidate | None
    resolved: tuple[ResolvedWriteOffAsset, ...] = ()


def original_matches(command: WriteOffAssets, document: WriteOffDocument) -> bool:
    return (
        (
            document.document_number,
            document.document_date,
            document.authorization_reference,
            document.authorization_date,
            document.effective_date,
        )
        == (
            command.document_number,
            command.document_date,
            command.authorization_reference,
            command.authorization_date,
            command.effective_date,
        )
        and len(document.lines) == len(command.asset_ids)
        and {line.asset_unit_id for line in document.lines} == set(command.asset_ids)
    )


def evaluate(candidate, facts, write_off_id):
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
    command = WriteOffAssets(
        write_off_id=write_off_id,
        document_number=candidate.document_number,
        document_date=candidate.document_date,
        effective_date=candidate.effective_date,
        authorization_reference=candidate.authorization_reference,
        authorization_date=candidate.authorization_date,
        asset_ids=tuple(asset.unit.id for asset in resolved),
    )
    if len({asset.unit.id for asset in resolved}) != len(resolved) and not any(count > 1 for count in counts.values()):
        issues.extend(
            ImportIssue("asset", "duplicate_asset", "Повторна або суперечлива ідентичність майна.", row.cell)
            for row in candidate.serials
        )
    if issues:
        return command, tuple(issues), False, tuple(resolved)
    if facts.original is not None:
        if original_matches(command, facts.original):
            return command, (), True, tuple(resolved)
        return (
            command,
            (ImportIssue("document", "write_off_conflict", "Технічний ідентифікатор уже належить іншому списанню."),),
            False,
            tuple(resolved),
        )
    # Validate each row through the same domain operation as the final write.
    # Pure immutable construction does not alter custody or create history.
    for row, asset in zip(candidate.serials, resolved, strict=True):
        try:
            build_write_off(
                id=write_off_id,
                document_number=command.document_number,
                document_date=command.document_date,
                effective_date=command.effective_date,
                authorization_reference=command.authorization_reference,
                authorization_date=command.authorization_date,
                asset_ids=(asset.unit.id,),
                assets=(asset.facts,),
                created_at=datetime.now(UTC),
            )
        except InvalidWriteOff as error:
            # Translate fixed domain diagnostics, never database exception text.
            messages = {
                "unit already written off": "Майно вже списано.",
                "only explicitly active units can be written off": "Майно має бути явно активним.",
                "write-off requires a serialized inventory receipt classification": "Потрібна класифікація INVENTORY та облік SERIALIZED у первісному надходженні.",
                "accounting acceptance cannot precede authorization": "Дата прийняття до обліку передує даті дозволу.",
                "accounting acceptance cannot precede receipt or latest transfer": "Дата прийняття до обліку передує надходженню або останньому переміщенню.",
            }
            issues.append(
                ImportIssue(
                    "asset",
                    "ineligible_asset",
                    messages.get(str(error), "Походження або хронологія майна не відповідає вимогам списання."),
                    row.cell,
                )
            )
    return command, tuple(issues), False, tuple(resolved)


def _registered_result(status, candidate, command, resolved):
    # Sum original prices independently per currency, without ambient Decimal rounding.
    values = {}
    for asset in resolved:
        if asset.acquisition_price is None:
            return WriteOffImportResult(
                status,
                command.write_off_id,
                command.asset_ids,
                document_number=candidate.document_number,
                effective_date=candidate.effective_date,
            )
        price = asset.acquisition_price
        values.setdefault(price.currency, []).append(price.amount)
    totals = []
    for currency, amounts in sorted(values.items()):
        with localcontext() as context:
            context.prec = (
                max(a.adjusted() for a in amounts)
                - min(a.as_tuple().exponent for a in amounts)
                + len(str(len(amounts)))
                + 2
            )
            totals.append(Money(sum(amounts, Decimal(0)), currency))
    return WriteOffImportResult(
        status,
        command.write_off_id,
        command.asset_ids,
        document_number=candidate.document_number,
        effective_date=candidate.effective_date,
        acquisition_values=tuple(totals),
    )


class WriteOffImportCoordinator:
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
        result = WriteOffImportResult("rejected", issues=preview.issues) if preview.issues else self._confirm(preview)
        self.previews.finish(preview_id, owner, result)
        return result

    def _confirm(self, preview):
        candidate = preview.candidate
        if candidate is None:
            return WriteOffImportResult("rejected")
        try:
            command, issues, matched, resolved = evaluate(
                candidate, self.reader.inspect(candidate, preview.registration_id), preview.registration_id
            )
            if issues:
                return WriteOffImportResult("rejected", issues=issues)
            if matched:
                return _registered_result("already_registered", candidate, command, resolved)
            write_off_assets(command, self.uow_factory())
            return _registered_result("registered", candidate, command, resolved)
        except Exception:
            # A commit response can be lost. Reconcile only the retained UUID and
            # complete immutable membership, including after subsequent movements.
            try:
                command, issues, matched, resolved = evaluate(
                    candidate, self.reader.inspect(candidate, preview.registration_id), preview.registration_id
                )
                if matched:
                    return _registered_result("already_registered", candidate, command, resolved)
                if issues:
                    return WriteOffImportResult("rejected", issues=issues)
            except Exception:
                pass
            return WriteOffImportResult(
                "retryable",
                issues=(
                    ImportIssue(
                        "database",
                        "unconfirmed",
                        "Результат не підтверджено. Повторіть підтвердження цього перегляду зі збереженим ідентифікатором.",
                    ),
                ),
            )
