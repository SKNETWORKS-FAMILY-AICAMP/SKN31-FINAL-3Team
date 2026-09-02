import unittest
from unittest.mock import patch

from backend_logic2.services.item_service import register_item_event


class ItemWebhookServiceTests(unittest.TestCase):
    @patch("backend_logic2.services.item_service.create_notification")
    @patch("backend_logic2.services.item_service.validate_new_item")
    @patch("backend_logic2.services.item_service.erp_get_one")
    @patch("backend_logic2.services.item_service.event_repository")
    def test_disabled_item_is_validated_once(self, events, get_one, validate, notify):
        events.begin_event.return_value = ({"event_id": "evt-1"}, True)
        get_one.return_value = {"item_code": "ITEM-001", "disabled": 1}
        validate.return_value = {"item_code": "ITEM-001", "approved": True, "missing": []}

        result, created = register_item_event(
            {"doc": {"name": "ITEM-001", "modified": "2026-09-02 10:00:00"}}
        )

        self.assertTrue(created)
        self.assertTrue(result["approved"])
        validate.assert_called_once_with("ITEM-001")
        events.complete_event.assert_called_once_with("evt-1")
        notify.assert_called_once()

    @patch("backend_logic2.services.item_service.create_notification")
    @patch("backend_logic2.services.item_service.validate_new_item")
    @patch("backend_logic2.services.item_service.erp_get_one")
    @patch("backend_logic2.services.item_service.event_repository")
    def test_activation_webhook_is_skipped_to_prevent_a_loop(
        self, events, get_one, validate, notify
    ):
        events.begin_event.return_value = ({"event_id": "evt-2"}, True)
        get_one.return_value = {"item_code": "ITEM-001", "disabled": 0}

        result, _ = register_item_event({"doc": {"name": "ITEM-001", "modified": "next"}})

        self.assertEqual(result["skipped"], "already_active")
        validate.assert_not_called()
        notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
