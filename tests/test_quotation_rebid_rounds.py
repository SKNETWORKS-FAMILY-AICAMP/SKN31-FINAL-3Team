import unittest
from unittest.mock import patch

from backend_logic2.services.workflow_projection import task_input_schema
from backend_logic2.workflow.process_commands import (
    check_quotations_command,
    final_selection_command,
)


class QuotationRebidRoundTests(unittest.TestCase):
    def test_quotation_check_schema_exposes_rebid(self):
        schema = task_input_schema({"type": "check_quotations"})

        self.assertEqual(
            {option["value"] for option in schema["options"]},
            {"check", "later", "finalize", "rebid"},
        )


    @patch(
        "backend_logic2.workflow.process_commands.interrupt",
        return_value={"decision": "rebid"},
    )
    def test_rebid_before_deadline_archives_round_and_preserves_ranking(self, _interrupt):
        ranking = [{
            "quotation_id": "SUP-QTN-0001",
            "supplier": "공급사 A",
            "rfq_name": "PUR-RFQ-0001",
            "rfq_round": 1,
        }]

        command = check_quotations_command({
            "rfq_name": "PUR-RFQ-0001",
            "quotation_deadline": "2099-09-30T18:00:00+09:00",
            "rfq_rounds": [],
            "quotation_ranking": ranking,
            "supplier_candidates": [{"name": "공급사 A"}],
        })

        self.assertEqual(command.goto, "select_rfq_targets")
        self.assertEqual(command.update["rfq_rounds"][0]["rfq_name"], "PUR-RFQ-0001")
        # 0차부터 시작: 한 번도 재비딩하지 않은 첫 라운드는 0차.
        self.assertEqual(command.update["rfq_rounds"][0]["round"], 0)
        self.assertNotIn("quotation_ranking", command.update)
        self.assertNotIn("supplier_candidates", command.update)


    @patch("backend_logic2.nodes.quotation.quotation_filter.quotation_ranker.print_evaluation")
    @patch("backend_logic2.nodes.quotation.quotation_filter.quotation_ranker.evaluate_quotations_for_rfqs")
    @patch("backend_logic2.workflow.process_commands.interrupt", return_value={"decision": "check"})
    def test_check_evaluates_archived_and_current_rounds(
        self, _interrupt, evaluate, _print_evaluation
    ):
        evaluate.return_value = {
            "ranking": [{
                "quotation_id": "SUP-QTN-0001",
                "supplier": "공급사 A",
                "rfq_name": "PUR-RFQ-0001",
                "rfq_round": 1,
            }],
        }

        command = check_quotations_command({
            "rfq_name": "PUR-RFQ-0002",
            "rfq_rounds": [{"round": 0, "rfq_name": "PUR-RFQ-0001"}],
        })

        evaluate.assert_called_once_with(
            ["PUR-RFQ-0001", "PUR-RFQ-0002"],
            current_rfq_name="PUR-RFQ-0002",
            round_by_rfq={"PUR-RFQ-0001": 0, "PUR-RFQ-0002": 1},
        )
        self.assertEqual(command.goto, "check_quotations")


    @patch("backend_logic2.nodes.quotation.quotation_filter.quotation_ranker.print_evaluation")
    @patch("backend_logic2.nodes.quotation.quotation_filter.quotation_ranker.evaluate_quotations_for_rfqs")
    @patch("backend_logic2.workflow.process_commands.interrupt", return_value={"decision": "check"})
    def test_failed_new_round_analysis_keeps_previous_ranking(
        self, _interrupt, evaluate, _print_evaluation
    ):
        previous = [{"quotation_id": "SUP-QTN-OLD", "supplier": "공급사 A"}]
        evaluate.return_value = {"ranking": [], "message": "제출된 견적이 아직 없습니다."}

        command = check_quotations_command({
            "rfq_name": "PUR-RFQ-0002",
            "rfq_rounds": [{"round": 0, "rfq_name": "PUR-RFQ-0001"}],
            "quotation_ranking": previous,
        })

        self.assertEqual(command.update["quotation_ranking"], previous)

    @patch(
        "backend_logic2.workflow.process_commands.interrupt",
        return_value={
            "supplier": "공급사 A",
            "quotation_id": "SUP-QTN-ROUND-1",
        },
    )
    def test_final_selection_remembers_the_selected_quotation_round(self, _interrupt):
        command = final_selection_command({
            "rfq_name": "PUR-RFQ-0002",
            "quotation_ranking": [
                {
                    "quotation_id": "SUP-QTN-ROUND-1",
                    "supplier": "공급사 A",
                    "rfq_name": "PUR-RFQ-0001",
                    "rfq_round": 1,
                },
                {
                    "quotation_id": "SUP-QTN-ROUND-2",
                    "supplier": "공급사 A",
                    "rfq_name": "PUR-RFQ-0002",
                    "rfq_round": 2,
                },
            ],
        })

        self.assertEqual(command.update["selected_quotation"], "SUP-QTN-ROUND-1")
        self.assertEqual(command.update["selected_rfq_name"], "PUR-RFQ-0001")

    @patch(
        "backend_logic2.workflow.process_commands.interrupt",
        return_value={
            "supplier": "공급사 A",
            "quotation_id": "SUP-QTN-EXPIRED",
        },
    )
    def test_final_selection_rejects_expired_quotation(self, _interrupt):
        command = final_selection_command({
            "rfq_name": "PUR-RFQ-0001",
            "quotation_ranking": [
                {
                    "quotation_id": "SUP-QTN-EXPIRED",
                    "supplier": "공급사 A",
                    "rfq_name": "PUR-RFQ-0001",
                    "rfq_round": 0,
                    "valid_till": "2000-01-01",
                },
            ],
        })

        self.assertEqual(command.goto, "final_selection")
        self.assertIn("유효기간", command.update["error"])
        self.assertNotIn("selected_supplier", command.update)

    @patch(
        "backend_logic2.workflow.process_commands.interrupt",
        return_value={
            "supplier": "공급사 A",
            "quotation_id": "SUP-QTN-VALID",
        },
    )
    def test_final_selection_allows_quotation_within_valid_till(self, _interrupt):
        command = final_selection_command({
            "rfq_name": "PUR-RFQ-0001",
            "quotation_ranking": [
                {
                    "quotation_id": "SUP-QTN-VALID",
                    "supplier": "공급사 A",
                    "rfq_name": "PUR-RFQ-0001",
                    "rfq_round": 0,
                    "valid_till": "2999-12-31",
                },
            ],
        })

        self.assertEqual(command.goto, "await_order_start")
        self.assertEqual(command.update["selected_supplier"], "공급사 A")
        self.assertEqual(command.update["selected_quotation"], "SUP-QTN-VALID")


if __name__ == "__main__":
    unittest.main()
