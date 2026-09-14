from decimal import Decimal

import pytest

from backend_logic2.nodes.quotation.quotation_filter.quotation_registrar import (
    SupplierQuotationRegistrationError,
    _default_tax_row,
)
from backend_logic2.nodes.quotation.quotation_filter.quotation_reviewer import (
    review_quotation,
)


def _get_many(*_args, **_kwargs):
    return [{"name": "기본 매입세"}]


def _get_one(*_args, **_kwargs):
    return {
        "taxes": [{
            "account_head": "매입부가세 - T",
            "charge_type": "On Net Total",
            "rate": 10,
        }],
    }


def test_tax_row_uses_official_template_rate_without_float_conversion() -> None:
    rows = _default_tax_row(
        "Test Company",
        Decimal("100"),
        Decimal("1000"),
        get_many=_get_many,
        get_one=_get_one,
    )

    assert rows[0]["rate"] == "10"
    assert isinstance(rows[0]["rate"], str)


def test_tax_row_rejects_reverse_calculated_unusual_rate() -> None:
    with pytest.raises(SupplierQuotationRegistrationError, match="공식 세율=10%"):
        _default_tax_row(
            "Test Company",
            Decimal("98"),
            Decimal("1000"),
            get_many=_get_many,
            get_one=_get_one,
        )


def test_unusual_krw_vat_does_not_pass_review() -> None:
    quotation = {
        "quotation_id": "SQ-1",
        "rfq_name": "RFQ-1",
        "supplier_name": "공급사 A",
        "currency": "KRW",
        "subtotal": 1000,
        "tax_amount": 98,
        "total_amount": 1098,
        "items": [{
            "item_code": "ITEM-1",
            "item_name": "품목 A",
            "quantity": 1,
            "unit_price": 1000,
            "amount": 1000,
        }],
        "source": {"kind": "portal", "filename": "SQ-1"},
        "extraction_attempt": 3,
    }
    rfq = {
        "rfq_name": "RFQ-1",
        "items": [{"item_code": "ITEM-1", "item_name": "품목 A", "quantity": 1}],
    }

    review = review_quotation(quotation, rfq)

    assert review.status.value == "human_review"
    assert review.valid is False
    assert any(
        issue.code == "UNUSUAL_VAT" and issue.severity.value == "error"
        for issue in review.issues
    )
