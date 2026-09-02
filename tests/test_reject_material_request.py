import os
import unittest
from unittest.mock import patch


os.environ.setdefault("SITE_URL", "http://erp.example.test")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("API_SECRET", "test-secret")

from backend_logic2.nodes.mr.reject_material_request import (
    MaterialRequestNotDraftError,
    add_rejection_comment,
    reject_material_request,
)


class RejectMaterialRequestTests(unittest.TestCase):
    @patch("backend_logic2.nodes.mr.reject_material_request.erp_add_comment")
    @patch("backend_logic2.nodes.mr.reject_material_request.erp_get_one")
    def test_adds_only_a_comment_to_a_draft_mr(self, get_one, add_comment):
        get_one.return_value = {
            "name": "MAT-MR-0001",
            "docstatus": 0,
            "status": "Draft",
        }
        add_comment.return_value = {"name": "comment-1"}

        result = add_rejection_comment("MAT-MR-0001", "  예산을 확인해주세요.  ")

        add_comment.assert_called_once_with(
            "Material Request", "MAT-MR-0001", "예산을 확인해주세요."
        )
        self.assertEqual(result, {"name": "comment-1"})

    @patch("backend_logic2.nodes.mr.reject_material_request.erp_add_comment")
    @patch("backend_logic2.nodes.mr.reject_material_request.erp_get_one")
    def test_rejects_non_draft_without_writing_comment(self, get_one, add_comment):
        get_one.return_value = {
            "name": "MAT-MR-0001",
            "docstatus": 1,
            "status": "Submitted",
        }

        with self.assertRaises(MaterialRequestNotDraftError):
            add_rejection_comment("MAT-MR-0001", "반려 사유")

        add_comment.assert_not_called()

    def test_rejects_blank_reason(self):
        with self.assertRaises(ValueError):
            add_rejection_comment("MAT-MR-0001", "   ")

    @patch("backend_logic2.nodes.mr.reject_material_request.erp_discard_draft")
    @patch("backend_logic2.nodes.mr.reject_material_request.erp_add_comment")
    @patch("backend_logic2.nodes.mr.reject_material_request.erp_get_one")
    def test_rejection_discards_draft_after_audited_comment(
        self, get_one, add_comment, discard_draft
    ):
        get_one.return_value = {"name": "MAT-MR-0001", "docstatus": 0, "status": "Draft"}

        result = reject_material_request("MAT-MR-0001", "예산 재검토", reason_code="BUYER_REJECTED")

        add_comment.assert_called_once_with(
            "Material Request",
            "MAT-MR-0001",
            "[AI Procurement][BUYER_REJECTED] 예산 재검토",
        )
        discard_draft.assert_called_once_with("Material Request", "MAT-MR-0001")
        self.assertEqual(result["action"], "discarded_draft")

    @patch("backend_logic2.nodes.mr.reject_material_request.erp_cancel")
    @patch("backend_logic2.nodes.mr.reject_material_request.erp_add_comment")
    @patch("backend_logic2.nodes.mr.reject_material_request.erp_get_one")
    def test_rejection_cancels_submitted_mr(self, get_one, add_comment, cancel):
        get_one.return_value = {"name": "MAT-MR-0002", "docstatus": 1, "status": "Pending"}

        result = reject_material_request(
            "MAT-MR-0002", "긴급 공급사 없음", reason_code="URGENT_NO_SUPPLIER"
        )

        cancel.assert_called_once_with("Material Request", "MAT-MR-0002")
        self.assertEqual(result["action"], "cancelled_submitted")


if __name__ == "__main__":
    unittest.main()
