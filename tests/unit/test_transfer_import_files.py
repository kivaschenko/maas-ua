import csv
from io import StringIO

import pytest
from transfer_import_fixtures import csv_file, transfer_fields, xlsx_file

from maas_ua.adapters.transfer_import_files import parse_transfer_upload


@pytest.mark.parametrize("kind,make", [("xlsx", xlsx_file), ("csv", csv_file)])
def test_versioned_transports_have_same_candidate(kind, make):
    parsed = parse_transfer_upload(make(serials=[("Example", "00001"), ("example", "00001")]), kind)
    assert not parsed.issues
    assert parsed.candidate.source.location_ref == "EXAMPLE-LOCATION"
    assert [(row.namespace, row.value) for row in parsed.candidate.serials] == [
        ("Example", "00001"),
        ("example", "00001"),
    ]
    assert [row.cell.row for row in parsed.candidate.serials] == [2, 3]


@pytest.mark.parametrize(
    "edit,code",
    [
        (lambda w: setattr(w["SerialNumbers"]["B2"], "value", 123), "text_required"),
        (lambda w: setattr(w["SerialNumbers"]["B2"], "value", "=1+1"), "formula_unsupported"),
        (lambda w: w["SerialNumbers"].merge_cells("A2:B2"), "structure"),
        (lambda w: w.create_sheet("Other"), "worksheets"),
        (lambda w: w.remove(w["SerialNumbers"]), "worksheets"),
        (lambda w: setattr(w["SerialNumbers"]["C2"], "value", "extra"), "structure"),
    ],
)
def test_unsafe_or_unsupported_workbooks_are_rejected(edit, code):
    parsed = parse_transfer_upload(xlsx_file(edit=edit), "xlsx")
    assert parsed.candidate is None
    assert code in {issue.code for issue in parsed.issues}


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"effective_date": "01/02/2026"}, "invalid_date"),
        ({"template_version": "other"}, "unsupported_version"),
        ({"source_location_ref": ""}, "required"),
        ({"document_number": " extra "}, "invalid_text"),
    ],
)
@pytest.mark.parametrize("kind,make", [("xlsx", xlsx_file), ("csv", csv_file)])
def test_metadata_errors(changes, code, kind, make):
    parsed = parse_transfer_upload(make(fields=transfer_fields(**changes)), kind)
    assert parsed.candidate is None
    assert code in {issue.code for issue in parsed.issues}


def test_csv_inconsistent_metadata_and_missing_column():
    rows = list(csv.reader(StringIO(csv_file().decode())))
    rows[2][rows[0].index("destination_location_ref")] = "OTHER"
    output = StringIO()
    csv.writer(output).writerows(rows)
    assert "inconsistent_metadata" in {i.code for i in parse_transfer_upload(output.getvalue().encode(), "csv").issues}
    assert parse_transfer_upload(b"serial_value\n0001\n", "csv").issues


def test_limits_and_semicolon():
    assert not parse_transfer_upload(csv_file(delimiter=";"), "csv", delimiter=";").issues
    assert parse_transfer_upload(b"x" * (5 * 1024 * 1024 + 1), "csv").issues
    assert parse_transfer_upload(xlsx_file(count=1001), "xlsx").issues
    assert parse_transfer_upload(csv_file(count=1001), "csv").issues
