import unittest
from unittest.mock import patch

from backend_logic2.pr import service


class SupplierPRServiceTests(unittest.TestCase):
    def test_rejection_requires_reason(self):
        with self.assertRaises(ValueError):
            service.respond("token", "reject", " ")

    @patch("backend_logic2.pr.service.repository.record_response")
    def test_acceptance_records_response_for_workflow_resume(self, record_response):
        record_response.return_value = {
            "pr_id": "pr-1", "status": "ACCEPTED", "purchase_mode": "quotation",
            "mr_name": "MAT-MR-1", "rfq_name": "PUR-RFQ-1", "supplier_id": "SUP-1",
        }

        result = service.respond("token", "accept")

        self.assertEqual(result["status"], "ACCEPTED")
        record_response.assert_called_once()

    @patch.dict("os.environ", {"BIDDINGFLOW_PUBLIC_URL": "https://example.test"})
    @patch("backend_logic2.pr.service.erp_send_email")
    @patch("backend_logic2.pr.service.repository.create_request")
    def test_graph_retry_does_not_resend_active_pr(self, create_request, send_email):
        create_request.return_value = {
            "pr_id": "pr-1", "status": "SENT", "_created": False,
        }
        result = service.create_and_send_pr(
            case_id="case-1", mr_name="MAT-MR-1", supplier_id="SUP-1",
            supplier_email="supplier@example.test", rfq_name="PUR-RFQ-1",
        )
        self.assertEqual(result["status"], "SENT")
        send_email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
