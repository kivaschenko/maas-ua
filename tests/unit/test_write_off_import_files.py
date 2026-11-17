import csv
from io import StringIO

import pytest
from write_off_import_fixtures import csv_file, write_off_fields, xlsx_file

from maas_ua.adapters.write_off_import_files import parse_write_off_upload


@pytest.mark.parametrize("kind,make", [("xlsx", xlsx_file), ("csv", csv_file)])
def test_versioned_transports_have_same_candidate(kind, make):
    parsed = parse_write_off_upload(make(serials=[("Example", "00001"), ("example", "00001")]), kind)
    assert not parsed.issues
    assert parsed.candidate.authorization_reference == "EXAMPLE-AUTH"
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
    parsed = parse_write_off_upload(xlsx_file(edit=edit), "xlsx")
    assert parsed.candidate is None
    assert code in {issue.code for issue in parsed.issues}


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"effective_date": "01/02/2026"}, "invalid_date"),
        ({"template_version": "other"}, "unsupported_version"),
        ({"authorization_reference": ""}, "required"),
        ({"document_number": " extra "}, "invalid_text"),
    ],
)
@pytest.mark.parametrize("kind,make", [("xlsx", xlsx_file), ("csv", csv_file)])
def test_metadata_errors(changes, code, kind, make):
    parsed = parse_write_off_upload(make(fields=write_off_fields(**changes)), kind)
    assert parsed.candidate is None
    assert code in {issue.code for issue in parsed.issues}


def test_csv_inconsistent_metadata_and_missing_column():
    rows = list(csv.reader(StringIO(csv_file().decode())))
    rows[2][rows[0].index("authorization_reference")] = "OTHER"
    output = StringIO()
    csv.writer(output).writerows(rows)
    assert "inconsistent_metadata" in {i.code for i in parse_write_off_upload(output.getvalue().encode(), "csv").issues}
    assert parse_write_off_upload(b"serial_value\n0001\n", "csv").issues


def test_limits_and_semicolon():
    assert not parse_write_off_upload(csv_file(delimiter=";"), "csv", delimiter=";").issues
    assert parse_write_off_upload(b"x" * (5 * 1024 * 1024 + 1), "csv").issues
    assert parse_write_off_upload(xlsx_file(count=1001), "xlsx").issues
    assert parse_write_off_upload(csv_file(count=1001), "csv").issues


@pytest.mark.parametrize(
    "field", ["document_number", "document_date", "authorization_reference", "authorization_date", "effective_date"]
)
@pytest.mark.parametrize("kind,make", [("xlsx", xlsx_file), ("csv", csv_file)])
def test_each_required_document_field_reports_its_coordinate(field, kind, make):
    parsed = parse_write_off_upload(make(fields=write_off_fields(**{field: ""})), kind)
    assert parsed.candidate is None
    issues = [i for i in parsed.issues if i.field == field and i.code in {"required", "invalid_date"}]
    assert issues and all(i.cell.row and i.cell.column for i in issues)


@pytest.mark.parametrize("kind,make", [("xlsx", xlsx_file), ("csv", csv_file)])
def test_empty_selection_is_rejected(kind, make):
    assert parse_write_off_upload(make(count=0), kind).issues
