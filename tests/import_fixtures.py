"""Synthetic import files; never use operational spreadsheets as fixtures."""

import csv
from io import BytesIO, StringIO

from openpyxl import Workbook


def document_fields(**changes):
    return {
        "template_version": "serialized_receipt_v1",
        "document_number": "EXAMPLE-125",
        "document_date": "2026-01-15",
        "effective_date": "2026-01-16",
        "source": "EXAMPLE-SOURCE",
        "nomenclature_code": "EXAMPLE-FPV",
        "line_quantity": "27",
        "unit_price": "14500.00",
        "currency": "UAH",
        "line_total": "391500.00",
        "accounting_classification": "INVENTORY",
        "initial_location_ref": "EXAMPLE-LOCATION",
        "initial_responsible_party_ref": "EXAMPLE-PARTY",
        "analytic.example": "EXAMPLE-ANALYTIC",
    } | changes


def csv_file(*, fields=None, count=27, delimiter=",", serials=None):
    fields = document_fields() if fields is None else fields
    stream = StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter)
    writer.writerow([*fields, "line_key", "serial_namespace", "serial_value"])
    for namespace, value in serials or [("EXAMPLE-ISSUER", f"EXAMPLE-{n:04}") for n in range(count)]:
        writer.writerow([*fields.values(), "1", namespace, value])
    return stream.getvalue().encode("utf-8")


def xlsx_file(*, fields=None, count=27, serials=None, edit=None):
    workbook = Workbook()
    document = workbook.active
    document.title = "Document"
    document.append(["field", "value"])
    for key, value in (document_fields() if fields is None else fields).items():
        document.append([key, value])
    sheet = workbook.create_sheet("SerialNumbers")
    sheet.append(["serial_namespace", "serial_value"])
    for pair in serials or [("EXAMPLE-ISSUER", f"EXAMPLE-{n:04}") for n in range(count)]:
        sheet.append(pair)
    if edit:
        edit(workbook)
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()
