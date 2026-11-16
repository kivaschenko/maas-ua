from datetime import date
from decimal import Decimal, localcontext

import pytest

from maas_ua.entrypoints.inventory import InvalidInput, decimal_text, exact_text, parse_date


@pytest.mark.parametrize("value", ["EXAMPLE-001", "Приклад-001", "aBc", "001"])
def test_serial_text_is_preserved_exactly(value):
    assert exact_text(value) == value


@pytest.mark.parametrize("value", [None, "", " ", " EXAMPLE", "EXAMPLE ", "EXAMPLE\x00", "x" * 4097])
def test_invalid_serial_text_is_rejected_without_echoing_input(value):
    with pytest.raises(InvalidInput) as error:
        exact_text(value)
    assert "EXAMPLE" not in str(error.value)


@pytest.mark.parametrize("value", ["2026-02-30", "20260101", "2026-W01-1", "01/02/2026", "2026-01-01 ", None])
def test_date_requires_exact_iso_business_date(value):
    with pytest.raises(InvalidInput):
        parse_date(value)


def test_valid_leap_day_and_exact_money_display_ignore_ambient_precision():
    assert parse_date("2028-02-29") == date(2028, 2, 29)
    with localcontext() as context:
        context.prec = 3
        assert decimal_text(Decimal("123456.789012")) == "123456.789012"
        assert decimal_text(Decimal("0.00")) == "0.00"
