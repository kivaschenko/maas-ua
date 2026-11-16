"""Bounded CSV/XLSX transport parsing. Never evaluate formulas or retain files.

XLSX money uses original decimal XML tokens, never openpyxl's binary floats.
Numeric cells permit at most 15 significant decimal digits; text permits 38.
Both have bounded exponents and are interpreted exactly, without quantization.
"""

import csv
import posixpath
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, DecimalException
from io import BytesIO, StringIO
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree as XML
from defusedxml.common import DefusedXmlException
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException

from maas_ua.domain.receipts import AccountingClassification
from maas_ua.service_layer.receipt_imports import (
    IMPORT_TYPE,
    MAX_ASSETS,
    ImportCell,
    ImportIssue,
    ImportSerial,
    ParsedReceiptImport,
    ReceiptImportCandidate,
)

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_CELL_LENGTH = 4096
MAX_COLUMNS = 64
DOCUMENT_FIELDS = (
    "template_version",
    "document_number",
    "document_date",
    "effective_date",
    "source",
    "nomenclature_code",
    "line_quantity",
    "unit_price",
    "currency",
    "line_total",
    "accounting_classification",
    "initial_location_ref",
    "initial_responsible_party_ref",
)
SERIAL_FIELDS = ("serial_namespace", "serial_value")
DECIMAL_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")


@dataclass(frozen=True)
class _Numeric:
    token: str


@dataclass(frozen=True)
class _Formula:
    pass


class _StructureError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message


def _issue(field, code, message, cell=None):
    return ImportIssue(field, code, message, cell)


def _text(value, field, cell, issues):
    if isinstance(value, _Formula):
        issues.append(
            _issue(field, "formula_unsupported", "Formula cells are unsupported; enter a literal value.", cell)
        )
    elif value is None or value == "":
        issues.append(_issue(field, "required", "A value is required.", cell))
    elif not isinstance(value, str):
        issues.append(_issue(field, "text_required", "Use a text cell to preserve the exact identifier.", cell))
    elif len(value) > MAX_CELL_LENGTH or value != value.strip() or "\x00" in value:
        issues.append(_issue(field, "invalid_text", "Use bounded nonblank text without outer whitespace or NUL.", cell))
    else:
        return value
    return None


def _money(value, field, cell, issues):
    if isinstance(value, _Formula):
        _text(value, field, cell, issues)
        return None
    numeric = isinstance(value, _Numeric)
    token = value.token if numeric else value
    if not isinstance(token, str) or len(token) > 128 or not DECIMAL_PATTERN.fullmatch(token):
        issues.append(
            _issue(field, "invalid_money", "Use a finite nonnegative decimal with a dot decimal separator.", cell)
        )
        return None
    try:
        amount = Decimal(token)
    except DecimalException:
        issues.append(_issue(field, "money_precision", "The decimal exponent exceeds supported precision.", cell))
        return None
    if not amount.is_finite() or amount < 0:
        issues.append(_issue(field, "invalid_money", "Use a finite nonnegative decimal.", cell))
        return None
    digits = list(amount.as_tuple().digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
    if len(digits) > (15 if numeric else 38) or abs(amount.as_tuple().exponent) > 100 or abs(amount.adjusted()) > 100:
        issues.append(
            _issue(
                field,
                "money_precision",
                "Numeric money permits 15 significant digits; text permits 38, with exponents bounded to 100. No rounding is applied.",
                cell,
            )
        )
        return None
    return amount


def _date(value, field, cell, issues):
    if isinstance(value, _Formula):
        _text(value, field, cell, issues)
        return None
    if type(value) is datetime and value.time() == datetime.min.time() and value.tzinfo is None:
        return value.date()
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    issues.append(_issue(field, "invalid_date", "Use an ISO YYYY-MM-DD date or an Excel date without a time.", cell))
    return None


def _header(values, cells, issues):
    converted = {}
    for field in DOCUMENT_FIELDS:
        value, cell = values.get(field), cells.get(field, ImportCell("Document", 1, "A"))
        if field in {"unit_price", "line_total"}:
            converted[field] = _money(value, field, cell, issues)
        elif field in {"document_date", "effective_date"}:
            converted[field] = _date(value, field, cell, issues)
        elif field == "line_quantity":
            token = value.token if isinstance(value, _Numeric) else value
            if isinstance(value, _Formula):
                _text(value, field, cell, issues)
            elif isinstance(value, _Numeric) and len(token) <= 32 and DECIMAL_PATTERN.fullmatch(token):
                try:
                    number = Decimal(token)
                    if number.is_finite() and abs(number) <= MAX_ASSETS and number == number.to_integral_value():
                        converted[field] = int(number)
                    else:
                        raise ValueError
                except DecimalException, ValueError:
                    issues.append(
                        _issue(field, "invalid_quantity", "Use an integer quantity within the row limits.", cell)
                    )
            elif isinstance(token, str) and re.fullmatch(r"[0-9]{1,7}", token):
                converted[field] = int(token)
            else:
                issues.append(_issue(field, "invalid_quantity", "Use an integer quantity.", cell))
        else:
            converted[field] = _text(value, field, cell, issues)
    if converted.get("template_version") not in {None, IMPORT_TYPE}:
        issues.append(
            _issue(
                "template_version",
                "unsupported_version",
                "Use the serialized_receipt_v1 template.",
                cells["template_version"],
            )
        )
    kind = converted.get("accounting_classification")
    if kind is not None:
        try:
            converted["accounting_classification"] = AccountingClassification(kind)
        except ValueError:
            issues.append(
                _issue(
                    "accounting_classification",
                    "invalid_classification",
                    "Use an explicit supported classification.",
                    cells["accounting_classification"],
                )
            )
    analytics = []
    for field, value in values.items():
        if field.startswith("analytic."):
            key = _text(field[len("analytic.") :], field, cells[field], issues)
            if value not in {None, ""}:
                text = _text(value, field, cells[field], issues)
                if key and text:
                    analytics.append((key, text))
    converted["analytics"] = tuple(sorted(analytics))
    return converted


def _candidate(header, serials, cells, issues):
    if issues:
        return ParsedReceiptImport(None, tuple(issues))
    return ParsedReceiptImport(
        ReceiptImportCandidate(
            document_number=header["document_number"],
            document_date=header["document_date"],
            effective_date=header["effective_date"],
            source=header["source"],
            nomenclature_code=header["nomenclature_code"],
            quantity=header["line_quantity"],
            unit_price=header["unit_price"],
            currency=header["currency"],
            line_total=header["line_total"],
            classification=header["accounting_classification"],
            location_ref=header["initial_location_ref"],
            responsible_party_ref=header["initial_responsible_party_ref"],
            analytics=header["analytics"],
            serials=tuple(serials),
            cells=tuple(cells.items()),
        )
    )


def _serial(values, cells, issues):
    namespace = _text(values[0], "serial_namespace", cells[0], issues)
    value = _text(values[1], "serial_value", cells[1], issues)
    if namespace is not None and value is not None:
        return ImportSerial(namespace, value, cells[1])
    return None


def _csv(data, delimiter, *, fields=DOCUMENT_FIELDS, header_parser=_header, candidate_builder=_candidate, receipt=True):
    if delimiter not in {",", ";"}:
        raise _StructureError("dialect", "Select a comma or semicolon CSV delimiter.")
    reader = csv.reader(StringIO(data.decode("utf-8-sig"), newline=""), delimiter=delimiter, strict=True)
    columns = next(reader, [])
    required = {*fields, *SERIAL_FIELDS, *(["line_key"] if receipt else [])}
    if (
        not required <= set(columns)
        or len(set(columns)) != len(columns)
        or len(columns) > MAX_COLUMNS
        or any(
            len(key) > 128 or (key not in required and not (receipt and key.startswith("analytic."))) for key in columns
        )
    ):
        raise _StructureError("headers", "Use the exact required CSV headers and optional analytic.<key> columns.")
    issues, serials, first_header, first_cells, first_line = [], [], None, {}, None
    count = 0
    for row in reader:
        if not any(value != "" for value in row):
            continue
        count += 1
        if count > MAX_ASSETS:
            raise _StructureError("row_limit", "At most 1000 serialized asset rows are supported.")
        row_number = reader.line_num
        if len(row) != len(columns):
            issues.append(
                _issue(
                    "row",
                    "row_width",
                    "The row must have exactly one cell per header.",
                    ImportCell("CSV", row_number, "A"),
                )
            )
            continue
        values = dict(zip(columns, row, strict=True))
        cells = {key: ImportCell("CSV", row_number, get_column_letter(i)) for i, key in enumerate(columns, 1)}
        header = header_parser(values, cells, issues)
        line = _text(values["line_key"], "line_key", cells["line_key"], issues) if receipt else None
        if first_header is None:
            first_header, first_cells, first_line = header, cells, line
        elif header != first_header:
            for key in fields:
                if header.get(key) != first_header.get(key):
                    issues.append(
                        _issue(
                            key,
                            "inconsistent_metadata",
                            "All rows must describe the same document and homogeneous line.",
                            cells[key],
                        )
                    )
            if receipt and header["analytics"] != first_header["analytics"]:
                issues.append(
                    _issue(
                        "analytics",
                        "inconsistent_metadata",
                        "All rows must have identical receipt analytics.",
                        cells["line_key"],
                    )
                )
        if line != first_line:
            issues.append(
                _issue(
                    "line_key",
                    "multiple_lines",
                    "Only one receipt line is supported; the complete file is rejected.",
                    cells["line_key"],
                )
            )
        serial = _serial([values[key] for key in SERIAL_FIELDS], [cells[key] for key in SERIAL_FIELDS], issues)
        if serial:
            serials.append(serial)
    if first_header is None:
        raise _StructureError("empty_file", "Include at least one serialized asset row.")
    return candidate_builder(first_header, serials, first_cells, issues)


def _workbook_parts(archive):
    infos = archive.infolist()
    names = [item.filename for item in infos]
    if (
        len(infos) > 200
        or len(names) != len(set(names))
        or sum(item.file_size for item in infos) > MAX_EXPANDED_BYTES
        or any(item.flag_bits & 1 for item in infos)
        or any(name.startswith("/") or ".." in name.split("/") or "\\" in name for name in names)
        or any(
            part in name.lower()
            for name in names
            for part in ("vbaproject", "embeddings/", "externallinks/", "activex/")
        )
    ):
        raise _StructureError(
            "unsafe_workbook", "Workbook package exceeds limits or contains unsupported embedded content."
        )
    roots, numeric, bounds = {}, {}, {}
    total_elements = 0
    for name in names:
        if name.endswith((".xml", ".rels")):
            root = XML.fromstring(archive.read(name), forbid_dtd=True)
            roots[name] = root if name in {"xl/workbook.xml", "xl/_rels/workbook.xml.rels"} else None
            previous_cell = (0, "")
            for element in root.iter():
                total_elements += 1
                if total_elements > 200000 or (element.text and len(element.text) > MAX_CELL_LENGTH):
                    raise _StructureError("unsafe_workbook", "Workbook XML exceeds parsing limits.")
                if element.get("TargetMode") == "External" or "macroEnabled" in element.get("ContentType", ""):
                    raise _StructureError(
                        "unsafe_workbook", "External relationships and macro-enabled workbooks are unsupported."
                    )
                tag = element.tag.rsplit("}", 1)[-1]
                if tag == "mergeCell":
                    raise _StructureError("structure", "Merged input cells are unsupported.")
                if tag == "row" and (
                    not re.fullmatch(r"[1-9][0-9]{0,6}", element.get("r", "")) or int(element.get("r")) > MAX_ASSETS + 1
                ):
                    raise _StructureError("row_limit", "Workbook rows exceed the template limits.")
                if tag == "c":
                    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", element.get("r", ""))
                    if not match or int(match[2]) > MAX_ASSETS + 1 or len(match[1]) > 1 or match[1] > "B":
                        raise _StructureError("structure", "Use the two-column template within the row limits.")
                    position = (int(match[2]), match[1])
                    if position <= previous_cell:
                        raise _StructureError(
                            "structure", "Worksheet cells must have distinct coordinates in row order."
                        )
                    previous_cell = position
                    bounds[name] = max(bounds.get(name, 1), int(match[2]))
                    if element.get("t", "n") == "n":
                        value = element.find("{*}v")
                        if value is not None:
                            numeric[(name, element.get("r"))] = _Numeric(value.text or "")
    workbook, relations = roots.get("xl/workbook.xml"), roots.get("xl/_rels/workbook.xml.rels")
    if workbook is None or relations is None:
        raise _StructureError("structure", "Use a standard XLSX workbook package.")
    targets = {
        item.get("Id"): posixpath.normpath("xl/" + item.get("Target", ""))
        if not item.get("Target", "").startswith("/")
        else item.get("Target").lstrip("/")
        for item in relations
    }
    sheets = workbook.find("{*}sheets")
    if sheets is None:
        raise _StructureError("structure", "The workbook requires Document and SerialNumbers worksheets.")
    paths = {
        sheet.get("name"): targets[sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")]
        for sheet in sheets
    }
    if set(paths) != {"Document", "SerialNumbers"} or len(sheets) != 2:
        raise _StructureError("worksheets", "Only Document and SerialNumbers worksheets are supported.")
    if len(set(paths.values())) != 2 or any(
        path not in names or not path.startswith("xl/worksheets/") for path in paths.values()
    ):
        raise _StructureError("worksheets", "Use two distinct standard worksheets.")
    if bounds.get(paths["Document"], 1) > 65:
        raise _StructureError("structure", "Document supports at most 64 metadata fields.")
    return paths, numeric, bounds


def _xlsx(data, *, fields=DOCUMENT_FIELDS, header_parser=_header, candidate_builder=_candidate, receipt=True):
    with ZipFile(BytesIO(data)) as archive:
        paths, numeric, bounds = _workbook_parts(archive)
    workbook = load_workbook(BytesIO(data), read_only=True, data_only=False, keep_links=False)
    try:
        issues, values, cells, serials = [], {}, {}, []

        def rows(name):
            for row in workbook[name].iter_rows(min_row=1, max_row=bounds.get(paths[name], 1), max_col=2):
                converted = []
                for cell in row:
                    value = cell.value
                    if cell.data_type == "f":
                        value = _Formula()
                    elif cell.data_type == "e":
                        value = False  # Not a valid imported value; never trust Excel errors.
                    elif cell.data_type == "n" and value is not None:
                        value = numeric.get((paths[name], cell.coordinate), False)
                    converted.append(value)
                yield converted

        document_rows = rows("Document")
        if next(document_rows, None) != ["field", "value"]:
            raise _StructureError("headers", "Document row 1 must contain field,value.")
        for number, (key, value) in enumerate(document_rows, 2):
            if key is None and value is None:
                continue
            cell = ImportCell("Document", number, "B")
            if (
                not isinstance(key, str)
                or len(key) > 128
                or key in values
                or (key not in fields and not (receipt and key.startswith("analytic.")))
            ):
                issues.append(
                    _issue(
                        "field",
                        "headers",
                        "Use unique supported document field names.",
                        ImportCell("Document", number, "A"),
                    )
                )
                continue
            values[key], cells[key] = value, cell
        header = header_parser(values, cells, issues)
        serial_rows = rows("SerialNumbers")
        if next(serial_rows, None) != list(SERIAL_FIELDS):
            raise _StructureError("headers", "SerialNumbers row 1 must contain serial_namespace,serial_value.")
        for number, row in enumerate(serial_rows, 2):
            if row == [None, None]:
                continue
            serial = _serial(row, [ImportCell("SerialNumbers", number, col) for col in ("A", "B")], issues)
            if serial:
                serials.append(serial)
        return candidate_builder(header, serials, cells, issues)
    finally:
        workbook.close()


def parse_receipt_upload(data: bytes, file_type: str, *, delimiter: str = ",") -> ParsedReceiptImport:
    """Return safe diagnostics; never interpolate a cell or underlying exception."""
    return _parse_upload(data, file_type, delimiter=delimiter)


def _parse_upload(data, file_type, *, delimiter=",", **layout):
    if len(data) > MAX_FILE_BYTES:
        return ParsedReceiptImport(None, (_issue("file", "file_limit", "Upload at most 5 MiB."),))
    try:
        if file_type == "csv":
            return _csv(data, delimiter, **layout)
        if file_type == "xlsx":
            return _xlsx(data, **layout)
        raise _StructureError("file_type", "Only CSV and XLSX files are supported.")
    except _StructureError as error:
        return ParsedReceiptImport(None, (_issue("file", error.code, error.message),))
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        UnicodeError,
        csv.Error,
        BadZipFile,
        InvalidFileException,
        XML.ParseError,
        DefusedXmlException,
    ):
        return ParsedReceiptImport(
            None, (_issue("file", "malformed_file", "The file is malformed or contains unsupported values."),)
        )
