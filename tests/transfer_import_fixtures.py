"""Synthetic transfer files using the established two-sheet layout."""

from import_fixtures import csv_file as receipt_csv
from import_fixtures import xlsx_file as receipt_xlsx


def transfer_fields(**changes):
    return {
        "template_version": "serialized_transfer_v1",
        "document_number": "EXAMPLE-TRANSFER",
        "document_date": "2026-02-01",
        "effective_date": "2026-02-01",
        "source_location_ref": "EXAMPLE-LOCATION",
        "source_responsible_party_ref": "EXAMPLE-PARTY",
        "destination_location_ref": "EXAMPLE-DESTINATION",
        "destination_responsible_party_ref": "EXAMPLE-DESTINATION-PARTY",
    } | changes


def xlsx_file(*, fields=None, count=12, serials=None, edit=None):
    return receipt_xlsx(fields=transfer_fields() if fields is None else fields, count=count, serials=serials, edit=edit)


def csv_file(*, fields=None, count=12, serials=None, delimiter=","):
    # Transfer CSV has no receipt line grouping marker.
    import csv
    from io import StringIO

    source = receipt_csv(
        fields=transfer_fields() if fields is None else fields, count=count, serials=serials, delimiter=delimiter
    ).decode()
    rows = list(csv.reader(StringIO(source), delimiter=delimiter))
    index = rows[0].index("line_key")
    output = StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter)
    for row in rows:
        writer.writerow(row[:index] + row[index + 1 :])
    return output.getvalue().encode()
