"""Transfer layout on the existing bounded, formula-free file transports."""

from dataclasses import replace

from maas_ua.adapters.receipt_import_files import _date, _issue, _parse_upload, _text
from maas_ua.domain.custody import CustodyAssignment
from maas_ua.service_layer.receipt_imports import ImportCell
from maas_ua.service_layer.transfer_imports import IMPORT_TYPE, ParsedTransferImport, TransferImportCandidate

DOCUMENT_FIELDS = (
    "template_version",
    "document_number",
    "document_date",
    "effective_date",
    "source_location_ref",
    "source_responsible_party_ref",
    "destination_location_ref",
    "destination_responsible_party_ref",
)


def _header(values, cells, issues):
    converted = {}
    for field in DOCUMENT_FIELDS:
        cell = cells.get(field, ImportCell("Document", 1, "A"))
        parser = _date if field in {"document_date", "effective_date"} else _text
        converted[field] = parser(values.get(field), field, cell, issues)
    if converted.get("template_version") not in {None, IMPORT_TYPE}:
        issues.append(
            _issue(
                "template_version",
                "unsupported_version",
                "Use the serialized_transfer_v1 template.",
                cells["template_version"],
            )
        )
    return converted


def _candidate(header, serials, cells, issues):
    if issues:
        return ParsedTransferImport(None, tuple(issues))
    return ParsedTransferImport(
        TransferImportCandidate(
            document_number=header["document_number"],
            document_date=header["document_date"],
            effective_date=header["effective_date"],
            source=CustodyAssignment(header["source_location_ref"], header["source_responsible_party_ref"]),
            destination=CustodyAssignment(
                header["destination_location_ref"], header["destination_responsible_party_ref"]
            ),
            serials=tuple(serials),
            cells=tuple(cells.items()),
        )
    )


def parse_transfer_upload(data: bytes, file_type: str, *, delimiter=",") -> ParsedTransferImport:
    parsed = _parse_upload(
        data,
        file_type,
        delimiter=delimiter,
        fields=DOCUMENT_FIELDS,
        header_parser=_header,
        candidate_builder=_candidate,
        receipt=False,
    )
    # The common transport returns the receipt envelope for package-level errors.
    messages = {
        "formula_unsupported": "Формули не підтримуються. Введіть буквальне значення.",
        "required": "Заповніть обов’язкове поле.",
        "text_required": "Використовуйте текстову клітинку для точного ідентифікатора.",
        "invalid_text": "Вкажіть текст без зовнішніх пробілів і NUL, до 4096 символів.",
        "invalid_date": "Вкажіть дату YYYY-MM-DD або дату Excel без часу.",
        "unsupported_version": "Використовуйте шаблон serialized_transfer_v1.",
        "inconsistent_metadata": "Усі рядки мають описувати один документ, джерело, призначення та дату.",
        "row_width": "Кількість клітинок має відповідати заголовку.",
        "headers": "Перевірте обов’язкові назви полів і колонок шаблону.",
        "empty_file": "Додайте хоча б одну одиницю майна.",
        "row_limit": "Дозволено щонайбільше 1000 серійних одиниць.",
        "worksheets": "Потрібні лише аркуші Document та SerialNumbers.",
        "structure": "Структура не підтримується. Використовуйте двоколонковий шаблон без об’єднаних клітинок.",
        "file_limit": "Максимальний розмір файлу — 5 МіБ.",
        "file_type": "Підтримуються лише CSV та XLSX.",
        "dialect": "Оберіть кому або крапку з комою як роздільник CSV.",
        "unsafe_workbook": "Книга перевищує обмеження або містить непідтримувані вкладення чи зовнішні посилання.",
        "malformed_file": "Файл пошкоджений або містить непідтримувані значення.",
    }
    return ParsedTransferImport(
        parsed.candidate,
        tuple(
            replace(issue, message=messages.get(issue.code, "Перевірте структуру та значення файлу."))
            for issue in parsed.issues
        ),
    )
