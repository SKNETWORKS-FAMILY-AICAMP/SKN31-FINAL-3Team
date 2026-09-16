import pytest

from backend_logic2.nodes.quotation.quotation_filter.quotation_registrar import (
    SupplierQuotationRegistrationError,
    build_supplier_quotation_payload,
)


def _get_one(doctype: str, name: str):
    if doctype == "Request for Quotation":
        return {
            "name": name,
            "docstatus": 1,
            "company": "Test Company",
            "suppliers": [{"supplier": "SUP-1", "supplier_name": "공급사 A"}],
            "items": [{
                "name": "RFQI-1",
                "item_code": "ITEM-1",
                "item_name": "품목 A",
                "uom": "Nos",
                "stock_uom": "Nos",
                "conversion_factor": 1,
                "material_request": "MR-1",
                "material_request_item": "MRI-1",
            }],
        }
    if doctype == "Company":
        return {"name": name, "default_currency": "KRW"}
    raise AssertionError(f"unexpected lookup: {doctype} {name}")


def _quotation(notes):
    return {
        "quotation_id": "EXT-Q-1",
        "rfq_name": "RFQ-1",
        "supplier_id": "SUP-1",
        "supplier_name": "공급사 A",
        "currency": "KRW",
        "subtotal": 1000,
        "tax_amount": 0,
        "total_amount": 1000,
        "items": [{
            "item_code": "ITEM-1",
            "item_name": "품목 A",
            "quantity": 1,
            "unit_price": 1000,
            "amount": 1000,
        }],
        "notes": notes,
        "source": {"kind": "text", "filename": "quotation.txt"},
    }


def test_external_notes_are_registered_as_safe_erpnext_terms() -> None:
    payload = build_supplier_quotation_payload(
        _quotation('특이사항: <script>alert("x")</script>\n현장 납품'),
        get_one=_get_one,
        get_many=lambda *_args, **_kwargs: [],
    )

    assert payload["terms"] == (
        "특이사항: &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;<br>현장 납품"
    )


def test_empty_external_notes_do_not_create_terms_value() -> None:
    payload = build_supplier_quotation_payload(
        _quotation("  "),
        get_one=_get_one,
        get_many=lambda *_args, **_kwargs: [],
    )

    assert "terms" not in payload


def test_single_external_item_maps_to_the_only_rfq_item() -> None:
    quotation = _quotation(None)
    quotation["items"][0].update({
        "item_code": "VENDOR-MAT-001",
        "item_name": "공급업체 자체 품목명",
        "description": "공급업체가 제시한 상세 규격",
    })

    payload = build_supplier_quotation_payload(
        quotation,
        get_one=_get_one,
        get_many=lambda *_args, **_kwargs: [],
    )

    assert payload["items"][0]["item_code"] == "ITEM-1"
    assert payload["items"][0]["request_for_quotation_item"] == "RFQI-1"
    assert payload["items"][0]["description"] == "공급업체가 제시한 상세 규격"


def test_multiple_external_items_do_not_use_single_item_fallback() -> None:
    quotation = _quotation(None)
    quotation["items"] = [
        {
            "item_code": f"VENDOR-{index}",
            "item_name": f"공급업체 품목 {index}",
            "quantity": 1,
            "unit_price": 500,
            "amount": 500,
        }
        for index in (1, 2)
    ]

    with pytest.raises(SupplierQuotationRegistrationError):
        build_supplier_quotation_payload(
            quotation,
            get_one=_get_one,
            get_many=lambda *_args, **_kwargs: [],
        )
