import unittest
from unittest.mock import patch

from backend_logic2.integrations import erp_client
from backend_logic2.services import workflow_service


class MaterialRequestChangeSyncTests(unittest.TestCase):
    @patch.object(erp_client, "erp_get")
    @patch.object(erp_client, "erp_get_one")
    def test_material_request_detail_merges_file_rows(self, get_one, get_many):
        get_one.return_value = {
            "name": "MAT-MR-0001",
            "status": "Draft",
            "items": [{"item_code": "ITEM-001"}],
        }
        get_many.return_value = [
            {"name": "FILE-1", "file_name": "drawing.pdf", "file_url": "/files/drawing.pdf"}
        ]

        result = erp_client.get_material_request_detail("MAT-MR-0001")

        self.assertEqual(result["_attachments"][0]["file_name"], "drawing.pdf")
        get_many.assert_called_once()
        self.assertEqual(get_many.call_args.args[0], "File")

    @patch.object(workflow_service, "_create_notification_safely")
    @patch.object(workflow_service, "register_material_request")
    @patch.object(workflow_service.case_repository, "get_case_by_mr")
    @patch.object(workflow_service, "get_pending_material_requests")
    def test_lightweight_poll_notifies_only_changed_existing_mr(
        self,
        pending,
        get_case,
        register,
        create_notification,
    ):
        pending.return_value = [{"name": "MAT-MR-0001"}]
        get_case.return_value = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "summary": {"attachments": []},
        }
        register.return_value = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "item_code": "ITEM-001",
            "stage": "MR_REVIEW",
            "summary": {"attachments": [{"name": "FILE-1"}]},
        }

        workflow_service.sync_draft_material_requests(reconcile_existing=False)

        self.assertEqual(
            create_notification.call_args.kwargs["notification_type"],
            "MATERIAL_REQUEST_UPDATED",
        )
        self.assertEqual(
            create_notification.call_args.kwargs["payload"]["changed_fields"],
            ["attachments"],
        )

    @patch.object(workflow_service, "_create_notification_safely")
    @patch.object(workflow_service.case_repository, "upsert_case_from_material_request")
    @patch.object(workflow_service.case_repository, "list_open_case_references")
    @patch.object(workflow_service, "get_material_request_detail")
    @patch.object(workflow_service, "get_pending_material_requests")
    def test_full_reconcile_refreshes_attachments_after_mr_was_submitted(
        self,
        pending,
        get_detail,
        open_cases,
        upsert,
        create_notification,
    ):
        """File changes remain discoverable after an MR leaves Draft state."""

        previous = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "status": "RUNNING",
            "stage": "SUPPLIER_RECOMMENDATION",
            "item_code": "ITEM-001",
            "item_name": "Test item",
            "requester_id": "requester@example.com",
            "assigned_user_id": "buyer@example.com",
            "summary": {"attachments": []},
            "erp_modified_at": "2026-09-03 12:00:00",
        }
        current_document = {
            "name": "MAT-MR-0001",
            "status": "Pending",
            "docstatus": 1,
            "items": [{"item_code": "ITEM-001"}],
            "_attachments": [{"name": "FILE-1", "file_name": "drawing.pdf"}],
        }
        refreshed = {
            **previous,
            "summary": {"attachments": current_document["_attachments"]},
        }
        pending.return_value = []
        open_cases.return_value = [previous]
        get_detail.return_value = current_document
        upsert.return_value = refreshed

        result = workflow_service.sync_draft_material_requests(
            reconcile_existing=True
        )

        self.assertEqual(result, [refreshed])
        upsert.assert_called_once_with(current_document)
        self.assertEqual(
            create_notification.call_args.kwargs["notification_type"],
            "MATERIAL_REQUEST_UPDATED",
        )
        self.assertEqual(
            create_notification.call_args.kwargs["payload"]["changed_fields"],
            ["attachments"],
        )

    @patch.object(workflow_service.event_repository, "complete_event")
    @patch.object(workflow_service.event_repository, "begin_event")
    @patch.object(workflow_service, "_create_notification_safely")
    @patch.object(workflow_service.case_repository, "upsert_case_from_material_request")
    @patch.object(workflow_service.case_repository, "get_case_by_mr")
    @patch.object(workflow_service, "get_material_request_detail")
    def test_file_webhook_refreshes_existing_mr_and_publishes_update(
        self,
        get_detail,
        get_case,
        upsert,
        create_notification,
        begin_event,
        complete_event,
    ):
        event = {"event_id": "event-1"}
        previous = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "summary": {"attachments": []},
        }
        current_document = {
            "name": "MAT-MR-0001",
            "status": "Draft",
            "docstatus": 0,
            "items": [{"item_code": "ITEM-001"}],
            "_attachments": [{"name": "FILE-1", "file_name": "drawing.pdf"}],
        }
        refreshed = {
            **previous,
            "item_code": "ITEM-001",
            "stage": "MR_REVIEW",
            "summary": {"attachments": current_document["_attachments"]},
        }
        begin_event.return_value = (event, True)
        get_case.return_value = previous
        get_detail.return_value = current_document
        upsert.return_value = refreshed

        result, created = workflow_service.register_material_request_attachment_event({
            "event": "after_insert",
            "doc": {
                "name": "FILE-1",
                "file_name": "drawing.pdf",
                "attached_to_doctype": "Material Request",
                "attached_to_name": "MAT-MR-0001",
                "modified": "2026-09-03 12:00:00",
            },
        })

        self.assertTrue(created)
        self.assertEqual(result["case_id"], "case-1")
        self.assertEqual(
            create_notification.call_args.kwargs["notification_type"],
            "MATERIAL_REQUEST_UPDATED",
        )
        complete_event.assert_called_once_with("event-1")

    @patch.object(workflow_service.event_repository, "complete_event")
    @patch.object(workflow_service.event_repository, "begin_event")
    @patch.object(workflow_service, "_create_notification_safely")
    @patch.object(workflow_service.case_repository, "upsert_case_from_material_request")
    @patch.object(workflow_service.case_repository, "get_case_by_mr")
    @patch.object(workflow_service, "get_material_request_detail")
    def test_file_on_trash_excludes_row_before_frappe_deletes_it(
        self,
        get_detail,
        get_case,
        upsert,
        create_notification,
        begin_event,
        complete_event,
    ):
        previous = {
            "case_id": "case-1",
            "mr_name": "MAT-MR-0001",
            "summary": {"attachments": [{"name": "FILE-1"}]},
        }
        begin_event.return_value = ({"event_id": "event-delete"}, True)
        get_case.return_value = previous
        get_detail.return_value = {
            "name": "MAT-MR-0001",
            "status": "Draft",
            "docstatus": 0,
            "items": [{"item_code": "ITEM-001"}],
            # on_trash runs before the File row disappears from REST results.
            "_attachments": [{"name": "FILE-1", "file_name": "drawing.pdf"}],
        }
        upsert.return_value = {
            **previous,
            "item_code": "ITEM-001",
            "stage": "MR_REVIEW",
            "summary": {"attachments": []},
        }

        workflow_service.register_material_request_attachment_event({
            "event": "on_trash",
            "doc": {
                "name": "FILE-1",
                "file_name": "drawing.pdf",
                "attached_to_doctype": "Material Request",
                "attached_to_name": "MAT-MR-0001",
                "modified": "2026-09-03 12:00:00",
            },
        })

        submitted_document = upsert.call_args.args[0]
        self.assertEqual(submitted_document["_attachments"], [])
        self.assertEqual(
            create_notification.call_args.kwargs["payload"]["attachment_count"],
            0,
        )
        complete_event.assert_called_once_with("event-delete")


if __name__ == "__main__":
    unittest.main()
