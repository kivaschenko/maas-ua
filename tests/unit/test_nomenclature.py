from dataclasses import replace

import pytest

from maas_ua.domain.receipts import InvalidReceipt, Nomenclature, TrackingPolicy


def item():
    return Nomenclature(
        "EXAMPLE-ID",
        TrackingPolicy.SERIALIZED,
        code="EXAMPLE-CODE",
        name="Example drone",
        unit_of_measure="pcs",
    )


def test_catalog_identity_is_separate_from_code_and_analytics():
    catalog = item()
    assert catalog.id != catalog.code
    assert catalog.default_accounting_classification is None
    assert replace(catalog, name="Example revised name").id == catalog.id


@pytest.mark.parametrize("field", ["id", "code", "name", "unit_of_measure"])
@pytest.mark.parametrize("value", ["", " ", " padded", None, 12])
def test_required_catalog_text(field, value):
    with pytest.raises(InvalidReceipt):
        replace(item(), **{field: value})


def test_catalog_requires_explicit_tracking_and_valid_optional_classification():
    with pytest.raises(InvalidReceipt):
        replace(item(), tracking_policy="SERIALIZED")
    with pytest.raises(InvalidReceipt):
        replace(item(), default_accounting_classification=" ")
