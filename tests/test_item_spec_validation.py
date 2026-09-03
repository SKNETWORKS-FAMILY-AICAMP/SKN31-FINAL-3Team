import unittest
from unittest.mock import patch

from backend_logic2.nodes.item import item_spec_validation as validation


class ItemSpecificationValidationTests(unittest.TestCase):
    @patch.object(validation, "log_ai_decision")
    @patch.object(validation, "_save_group_requirements")
    @patch.object(validation, "_ai_define_required_specs")
    @patch.object(validation, "get_group_requirements", return_value=None)
    def test_new_group_policy_is_persisted_before_use(
        self, _get_existing, define, save, _log
    ):
        define.return_value = {
            "required_specs": ["재질", "규격"],
            "reason": "발주 식별에 필요",
        }
        save.return_value = {
            "item_group": "안전용품",
            "required_specs": ["재질", "규격"],
            "reason": "발주 식별에 필요",
        }

        result = validation.get_or_create_group_requirements("안전용품")

        save.assert_called_once_with(
            "안전용품", ["재질", "규격"], "발주 식별에 필요"
        )
        self.assertEqual(result["required_specs"], ["재질", "규격"])

    @patch.object(validation, "log_ai_decision")
    @patch.object(validation, "_ai_check_completeness")
    @patch.object(validation, "get_or_create_group_requirements")
    def test_omitted_ai_result_is_treated_as_missing(
        self, get_requirements, check, _log
    ):
        get_requirements.return_value = {
            "item_group": "안전용품",
            "required_specs": ["재질", "색상"],
            "reason": "테스트",
        }
        check.return_value = [
            {"spec": "재질", "present": True, "reason": "ABS 기재"}
        ]

        result = validation.check_item_spec_completeness(
            "안전용품", "재질은 ABS이고 상세 설명이 충분합니다."
        )

        self.assertFalse(result["complete"])
        self.assertEqual(result["missing"], ["색상"])

    @patch.object(validation, "_activate_item")
    @patch.object(validation, "erp_add_comment")
    @patch.object(validation, "check_item_spec_completeness")
    @patch.object(validation, "erp_get_one")
    def test_missing_specs_leave_item_disabled_and_add_comment(
        self, get_one, check, add_comment, activate
    ):
        get_one.return_value = {
            "item_code": "ITEM-001",
            "item_group": "안전용품",
            "description": "색상만 황색으로 요청합니다.",
        }
        check.return_value = {
            "complete": False,
            "missing": ["재질", "치수"],
        }

        result = validation.validate_new_item("ITEM-001")

        self.assertFalse(result["approved"])
        activate.assert_not_called()
        add_comment.assert_called_once()
        self.assertIn("재질, 치수", add_comment.call_args.args[2])
        self.assertIn("다시 요청", add_comment.call_args.args[2])

    @patch.object(validation, "_activate_item")
    @patch.object(validation, "erp_add_comment")
    @patch.object(validation, "check_item_spec_completeness")
    @patch.object(validation, "erp_get_one")
    def test_complete_specs_activate_item(
        self, get_one, check, add_comment, activate
    ):
        get_one.return_value = {
            "item_code": "ITEM-001",
            "item_group": "안전용품",
            "description": "재질 ABS, 프리사이즈 54~63cm, 색상 황색",
        }
        check.return_value = {"complete": True, "missing": []}

        result = validation.validate_new_item("ITEM-001")

        self.assertTrue(result["approved"])
        activate.assert_called_once_with("ITEM-001")
        add_comment.assert_not_called()

    def test_empty_policy_can_never_approve_an_item(self):
        with self.assertRaises(validation.ItemSpecificationPolicyError):
            validation._normalize_required_spec_names([])


if __name__ == "__main__":
    unittest.main()
