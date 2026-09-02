import unittest
from unittest.mock import patch

from backend_logic2.api.mr_substitute_routes import (
    SubstituteDecisionRequest,
    submit_substitute_decision,
)
from backend_logic2.nodes.mr.substitute_reply_watcher import (
    _find_reply_after_anchor,
    _get_comments,
    _parse_reply,
)


class MRSubstituteRouteTests(unittest.TestCase):
    def test_comment_reply_parser_supports_candidate_and_new_purchase(self):
        candidates = [
            {"item_code": "ITEM-SUB-1"},
            {"item_code": "ITEM-SUB-2"},
        ]

        self.assertEqual(
            _parse_reply("[BiddingFlow 대체품 선택] 2", candidates),
            {"item_code": "ITEM-SUB-2"},
        )
        self.assertEqual(
            _parse_reply("[BiddingFlow 대체품 선택] 구매", candidates),
            {"decision": "new_purchase"},
        )

    def test_comment_reply_parser_rejects_unmarked_system_text(self):
        candidates = [{"item_code": "ITEM-SUB-1"}]

        self.assertIsNone(
            _parse_reply(
                "Administrator assigned 구매팀: 대체품 후보가 확인되었습니다.",
                candidates,
            )
        )

    def test_reply_selection_ignores_assignment_and_label_events(self):
        comments = [
            {
                "comment_type": "Comment",
                "content": "[AI Procurement] 대체품 후보가 확인되었습니다.",
            },
            {
                "comment_type": "Assigned",
                "content": "Administrator assigned 구매팀: 구매 여부를 확인해주세요.",
            },
            {"comment_type": "Label", "content": "Pending"},
            {
                "comment_type": "Comment",
                "content": "일반 문의 댓글입니다.",
            },
            {
                "comment_type": "Comment",
                "content": "[BiddingFlow 대체품 선택] 1",
            },
        ]

        self.assertEqual(
            _find_reply_after_anchor(comments),
            comments[-1],
        )

    @patch("backend_logic2.nodes.mr.substitute_reply_watcher.erp_get", return_value=[])
    def test_comment_query_excludes_non_comment_timeline_events(self, erp_get):
        _get_comments("MAT-MR-TEST")

        filters = erp_get.call_args.kwargs["filters"]
        self.assertIn(["comment_type", "=", "Comment"], filters)

    @patch("backend_logic2.repositories.cases.get_case_by_mr", return_value=None)
    @patch("backend_logic2.nodes.mr.reject_material_request.reject_material_request")
    def test_force_reject_closes_mr_instead_of_leaving_pending(self, reject, _get_case):
        result = submit_substitute_decision(
            "MAT-MR-0001",
            SubstituteDecisionRequest(decision="force_reject", reason="대체 검토 반려"),
            _auth=None,
        )

        reject.assert_called_once_with(
            "MAT-MR-0001",
            "대체 검토 반려",
            reason_code="FORCE_REJECT",
        )
        self.assertEqual(result["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
