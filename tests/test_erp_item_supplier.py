from unittest.mock import Mock, patch

from backend_logic2.integrations import erp_client


def test_existing_item_supplier_is_not_written_again():
    with (
        patch.object(
            erp_client,
            "erp_get_one",
            return_value={"supplier_items": [{"supplier": "SUP-001"}]},
        ),
        patch.object(erp_client.requests, "put") as put,
    ):
        result = erp_client.ensure_item_supplier("ITEM-001", "SUP-001")

    assert result["created"] is False
    put.assert_not_called()


def test_completed_supplier_is_appended_without_losing_existing_rows():
    existing = {"supplier": "SUP-001", "supplier_part_no": "PART-A"}
    response = Mock(status_code=200)
    with (
        patch.object(
            erp_client,
            "erp_get_one",
            return_value={"supplier_items": [existing]},
        ),
        patch.object(erp_client.requests, "put", return_value=response) as put,
    ):
        result = erp_client.ensure_item_supplier("ITEM/001", "SUP-002")

    assert result["created"] is True
    assert put.call_args.args[0].endswith("/api/resource/Item/ITEM%2F001")
    assert put.call_args.kwargs["json"] == {
        "supplier_items": [existing, {"supplier": "SUP-002"}]
    }
