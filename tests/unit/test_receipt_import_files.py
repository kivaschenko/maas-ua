from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from import_fixtures import csv_file, document_fields, xlsx_file

from maas_ua.adapters.receipt_import_files import parse_receipt_upload


@pytest.mark.parametrize("kind,make_file", [("csv", csv_file), ("xlsx", xlsx_file)])
def test_formats_map_to_same_typed_candidate(kind, make_file):
    parsed = parse_receipt_upload(make_file(), kind)
    assert not parsed.issues
    candidate = parsed.candidate
    assert candidate.document_number == "EXAMPLE-125"
    assert candidate.document_date == date(2026, 1, 15)
    assert candidate.effective_date == date(2026, 1, 16)
    assert candidate.unit_price == Decimal("14500")
    assert candidate.line_total == Decimal("391500")
    assert candidate.quantity == 27
    assert len(candidate.serials) == 27
    assert candidate.analytics == (("example", "EXAMPLE-ANALYTIC"),)


@pytest.mark.parametrize("kind,make_file", [("csv", csv_file), ("xlsx", xlsx_file)])
def test_identifiers_preserve_leading_zeroes_unicode_and_case(kind, make_file):
    parsed = parse_receipt_upload(make_file(serials=[("Приклад-α", "000012Аа")] * 27), kind)
    assert not parsed.issues
    assert parsed.candidate.serials[0].namespace == "Приклад-α"
    assert parsed.candidate.serials[0].value == "000012Аа"


def test_csv_bom_and_explicit_semicolon_dialect():
    parsed = parse_receipt_upload(b"\xef\xbb\xbf" + csv_file(delimiter=";"), "csv", delimiter=";")
    assert not parsed.issues
    assert parsed.candidate.quantity == 27


@pytest.mark.parametrize("amount", [0, 0.1, 14500, 0.123456789012345])
def test_xlsx_native_numeric_money_reads_exact_decimal_xml(amount):
    parsed = parse_receipt_upload(xlsx_file(fields=document_fields(unit_price=amount)), "xlsx")
    assert not parsed.issues
    assert parsed.candidate.unit_price == Decimal(str(amount))


def test_xlsx_native_dates_are_distinct_from_timestamps():
    fields = document_fields(document_date=date(2026, 1, 15), effective_date=datetime(2026, 1, 16))
    parsed = parse_receipt_upload(xlsx_file(fields=fields), "xlsx")
    assert not parsed.issues
    assert parsed.candidate.effective_date == date(2026, 1, 16)
    fields["effective_date"] = datetime(2026, 1, 16, 12)
    assert "invalid_date" in codes(parse_receipt_upload(xlsx_file(fields=fields), "xlsx"))


def rewrite_member(data, member, transform):
    stream = BytesIO()
    with ZipFile(BytesIO(data)) as source, ZipFile(stream, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            content = source.read(name)
            target.writestr(name, transform(content) if name == member else content)
    return stream.getvalue()


def codes(parsed):
    return {issue.code for issue in parsed.issues}


@pytest.mark.parametrize("token", ["0.30000000000000004", "1234567890123456", "1e101"])
def test_xlsx_excessive_numeric_precision_is_rejected_at_cell(token):
    data = xlsx_file(fields=document_fields(unit_price=14500))
    data = rewrite_member(
        data, "xl/worksheets/sheet1.xml", lambda raw: raw.replace(b"<v>14500</v>", f"<v>{token}</v>".encode())
    )
    parsed = parse_receipt_upload(data, "xlsx")
    issue = next(issue for issue in parsed.issues if issue.field == "unit_price")
    assert issue.code == "money_precision"
    assert issue.cell.sheet == "Document"
    assert issue.cell.column == "B"
    assert token not in issue.message


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("unit_price", "=100+200", "formula_unsupported"),
        ("nomenclature_code", 123, "text_required"),
        ("unit_price", "1,234.56", "invalid_money"),
        ("unit_price", "NaN", "invalid_money"),
        ("unit_price", True, "invalid_money"),
        ("document_date", "15/01/2026", "invalid_date"),
        ("source", None, "required"),
        ("source", " EXAMPLE", "invalid_text"),
        ("template_version", "other", "unsupported_version"),
    ],
)
def test_malformed_document_values_are_safe_cell_errors(field, value, code):
    parsed = parse_receipt_upload(xlsx_file(fields=document_fields(**{field: value})), "xlsx")
    assert code in codes(parsed)
    assert any(issue.field == field and issue.cell.sheet == "Document" for issue in parsed.issues)
    assert parsed.candidate is None


def test_xlsx_serial_numeric_and_formula_cells_are_rejected():
    def edit(workbook):
        workbook["SerialNumbers"]["B2"] = 123
        workbook["SerialNumbers"]["A3"] = "=1+1"

    parsed = parse_receipt_upload(xlsx_file(edit=edit), "xlsx")
    assert {"text_required", "formula_unsupported"} <= codes(parsed)
    assert {issue.cell.row for issue in parsed.issues} == {2, 3}


@pytest.mark.parametrize(
    "edit",
    [
        lambda wb: wb.create_sheet("Extra"),
        lambda wb: wb["Document"].merge_cells("A2:B2"),
        lambda wb: wb["Document"].append(["document_number", "EXAMPLE-OTHER"]),
        lambda wb: wb["Document"].append(["unknown_field", "EXAMPLE"]),
        lambda wb: wb["SerialNumbers"].append(["EXAMPLE", "EXAMPLE", "unexpected"]),
    ],
)
def test_xlsx_rejects_ambiguous_shapes(edit):
    assert parse_receipt_upload(xlsx_file(edit=edit), "xlsx").issues


def test_csv_rejects_different_lines_metadata_and_row_width():
    data = csv_file().decode()
    assert "inconsistent_metadata" in codes(
        parse_receipt_upload(data.replace("EXAMPLE-125", "EXAMPLE-OTHER", 1).encode(), "csv")
    )
    assert "multiple_lines" in codes(
        parse_receipt_upload(data.replace(",1,EXAMPLE-ISSUER", ",2,EXAMPLE-ISSUER", 1).encode(), "csv")
    )
    assert "row_width" in codes(parse_receipt_upload((data + "only,two\n").encode(), "csv"))


@pytest.mark.parametrize("data,kind", [(b"not a workbook", "xlsx"), (b"\xff", "csv"), (b"", "csv"), (b"x", "xlsm")])
def test_bad_files_return_safe_diagnostics(data, kind):
    assert parse_receipt_upload(data, kind).issues


def test_file_and_row_limits_and_zip_payloads():
    assert "file_limit" in codes(parse_receipt_upload(b"x" * (5 * 1024 * 1024 + 1), "csv"))
    assert "row_limit" in codes(parse_receipt_upload(csv_file(count=1001), "csv"))
    data = xlsx_file()
    stream = BytesIO(data)
    with ZipFile(stream, "a") as archive:
        archive.writestr("xl/vbaProject.bin", b"synthetic")
    assert "unsafe_workbook" in codes(parse_receipt_upload(stream.getvalue(), "xlsx"))


def test_csv_duplicate_and_missing_headers_and_blank_serials():
    data = csv_file().decode()
    assert "headers" in codes(parse_receipt_upload(data.replace("serial_value", "serial_namespace", 1).encode(), "csv"))
    assert "headers" in codes(parse_receipt_upload(data.replace("serial_value", "unknown", 1).encode(), "csv"))
    assert "required" in codes(parse_receipt_upload(data.replace("EXAMPLE-0000", "", 1).encode(), "csv"))


def test_workbook_xml_entities_and_duplicate_cell_addresses_are_rejected():
    data = xlsx_file()
    malicious = rewrite_member(
        data, "xl/worksheets/sheet1.xml", lambda raw: b'<!DOCTYPE worksheet [<!ENTITY x "synthetic">]>' + raw
    )
    assert "malformed_file" in codes(parse_receipt_upload(malicious, "xlsx"))
    duplicate = rewrite_member(data, "xl/worksheets/sheet1.xml", lambda raw: raw.replace(b'<c r="B2"', b'<c r="A2"', 1))
    assert "structure" in codes(parse_receipt_upload(duplicate, "xlsx"))


def test_workbook_dimensions_cannot_hide_serial_rows():
    data = rewrite_member(
        xlsx_file(), "xl/worksheets/sheet2.xml", lambda raw: raw.replace(b'ref="A1:B28"', b'ref="A1:A1"')
    )
    parsed = parse_receipt_upload(data, "xlsx")
    assert not parsed.issues and len(parsed.candidate.serials) == 27


def test_excessive_decimal_exponent_is_a_safe_cell_error():
    parsed = parse_receipt_upload(csv_file(fields=document_fields(unit_price="1e99999999999999999999999")), "csv")
    assert "money_precision" in codes(parsed)
    assert any(issue.field == "unit_price" and issue.cell.row == 2 for issue in parsed.issues)


def test_integral_xlsx_numeric_quantity_is_supported_without_rounding():
    data = rewrite_member(
        xlsx_file(fields=document_fields(line_quantity=27)),
        "xl/worksheets/sheet1.xml",
        lambda raw: raw.replace(b"<v>27</v>", b"<v>27.0</v>"),
    )
    parsed = parse_receipt_upload(data, "xlsx")
    assert not parsed.issues and parsed.candidate.quantity == 27
    parsed = parse_receipt_upload(xlsx_file(fields=document_fields(line_quantity=Decimal("27.5"))), "xlsx")
    assert "invalid_quantity" in codes(parsed)


@pytest.mark.parametrize("kind,make_file", [("csv", csv_file), ("xlsx", xlsx_file)])
def test_reference_identifiers_remain_exact_text(kind, make_file):
    fields = document_fields(
        nomenclature_code="000012-Приклад",
        source="000099-α",
        initial_location_ref="000001",
        initial_responsible_party_ref="000002",
    )
    parsed = parse_receipt_upload(make_file(fields=fields), kind)
    assert not parsed.issues
    assert parsed.candidate.nomenclature_code == "000012-Приклад"
    assert parsed.candidate.source == "000099-α"
    assert parsed.candidate.location_ref == "000001"
    assert parsed.candidate.responsible_party_ref == "000002"


@pytest.mark.parametrize("kind,make_file", [("csv", csv_file), ("xlsx", xlsx_file)])
def test_text_money_allows_38_significant_digits_without_quantization(kind, make_file):
    token = "0.12345678901234567890123456789012345678"
    parsed = parse_receipt_upload(make_file(fields=document_fields(unit_price=token)), kind)
    assert not parsed.issues and parsed.candidate.unit_price == Decimal(token)
    assert "money_precision" in codes(
        parse_receipt_upload(make_file(fields=document_fields(unit_price=token + "9")), kind)
    )
