import os
import unittest
from unittest.mock import patch


os.environ.setdefault("SITE_URL", "http://erp.example.test")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("API_SECRET", "test-secret")

from backend_logic2.nodes.mr.reject_material_request import (
    MaterialRequestNotDraftError,
    add_rejection_comment,
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


if __name__ == "__main__":
    unittest.main()
