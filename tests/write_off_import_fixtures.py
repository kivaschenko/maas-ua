"""Synthetic write-off files using the established two-sheet layout."""

from import_fixtures import csv_file as receipt_csv
from import_fixtures import xlsx_file as receipt_xlsx


def write_off_fields(**changes):
    return {
        "template_version": "serialized_inventory_write_off_v1",
        "document_number": "EXAMPLE-TRANSFER",
        "document_date": "2026-02-01",
        "effective_date": "2026-02-01",
        "authorization_reference": "EXAMPLE-AUTH",
        "authorization_date": "2026-02-01",
    } | changes


def xlsx_file(*, fields=None, count=8, serials=None, edit=None):
    return receipt_xlsx(
        fields=write_off_fields() if fields is None else fields, count=count, serials=serials, edit=edit
    )


def csv_file(*, fields=None, count=8, serials=None, delimiter=","):
    # Write-off CSV has no receipt line grouping marker.
    import csv
    from io import StringIO

    source = receipt_csv(
        fields=write_off_fields() if fields is None else fields, count=count, serials=serials, delimiter=delimiter
    ).decode()
    rows = list(csv.reader(StringIO(source), delimiter=delimiter))
    index = rows[0].index("line_key")
    output = StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter)
    for row in rows:
        writer.writerow(row[:index] + row[index + 1 :])
    return output.getvalue().encode()
