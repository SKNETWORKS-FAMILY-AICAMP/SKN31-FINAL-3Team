from __future__ import annotations

from copy import deepcopy

import pytest

from backend_logic2.nodes.quotation.quotation_filter.quotation_registrar import (
    QuotationArithmeticValidationError,
    build_supplier_quotation_payload,
    validate_quotation_arithmetic,
)


def _quotation() -> dict:
    return {
        "quotation_id": "EXT-1",
        "rfq_name": "RFQ-1",
        "supplier_id": "SUP-1",
        "supplier_name": "Supplier",
        "currency": "KRW",
        "subtotal": 3000,
        "tax_amount": 300,
        "total_amount": 3300,
        "items": [
            {
                "item_code": "ITEM-1",
                "item_name": "Item 1",
                "quantity": 2,
                "unit_price": 1000,
                "amount": 2000,
            },
            {
                "item_code": "ITEM-2",
                "item_name": "Item 2",
                "quantity": 1,
                "unit_price": 1000,
                "amount": 1000,
            },
        ],
        "source": {"kind": "image", "filename": "quote.png"},
    }


def test_consistent_quotation_passes_arithmetic_validation() -> None:
    quotation = validate_quotation_arithmetic(_quotation())

    assert quotation.total_amount == 3300


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["items"][0].update(amount=1900), r"items\[0\]"),
        (lambda value: value.update(subtotal=2900), "subtotal"),
        (lambda value: value.update(total_amount=3200), "total"),
    ],
)
def test_arithmetic_mismatch_is_rejected_without_value_replacement(
    mutation,
    message,
) -> None:
    raw = deepcopy(_quotation())
    mutation(raw)
    before = deepcopy(raw)

    with pytest.raises(QuotationArithmeticValidationError, match=message):
        validate_quotation_arithmetic(raw)

    assert raw == before


def test_arithmetic_validation_runs_before_erp_lookup() -> None:
    raw = _quotation()
    raw["items"][0]["amount"] = 1900

    def unexpected_lookup(*_args, **_kwargs):
        raise AssertionError("ERP must not be called for invalid arithmetic")

    with pytest.raises(QuotationArithmeticValidationError):
        build_supplier_quotation_payload(
            raw,
            get_one=unexpected_lookup,
            get_many=unexpected_lookup,
        )
