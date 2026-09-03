import unittest
from unittest.mock import patch

from backend_logic2.integrations.erp_client import list_registered_items


class ItemListingTests(unittest.TestCase):
    @patch("backend_logic2.integrations.erp_client.erp_get")
    def test_default_listing_includes_disabled_items(self, erp_get):
        erp_get.return_value = [
            {"item_code": "ACTIVE", "disabled": 0},
            {"item_code": "WAITING", "disabled": 1},
        ]

        response = list_registered_items(
            limit=500, offset=0, include_disabled=True
        )

        self.assertEqual(response["count"], 2)
        self.assertEqual(response["disabled_count"], 1)
        self.assertTrue(response["include_disabled"])
        self.assertIsNone(erp_get.call_args.kwargs["filters"])

    @patch("backend_logic2.integrations.erp_client.erp_get")
    def test_listing_can_explicitly_exclude_disabled_items(self, erp_get):
        erp_get.return_value = [{"item_code": "ACTIVE", "disabled": 0}]

        list_registered_items(limit=500, offset=0, include_disabled=False)

        self.assertEqual(
            erp_get.call_args.kwargs["filters"], [["disabled", "=", 0]]
        )


if __name__ == "__main__":
    unittest.main()
