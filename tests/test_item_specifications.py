import unittest

from procurement_db.item_specifications import (
    build_item_specification_response,
    normalize_required_specs,
)


class ItemSpecificationTests(unittest.TestCase):
    def test_normalize_required_specs_supports_mapping_and_metadata(self):
        definitions = normalize_required_specs(
            {
                "custom_pressure": {
                    "label": "정격 압력",
                    "unit": "bar",
                    "section": "성능 조건",
                    "required": True,
                }
            }
        )

        self.assertEqual(
            definitions,
            [
                {
                    "fieldname": "custom_pressure",
                    "label": "정격 압력",
                    "fieldtype": "text",
                    "unit": "bar",
                    "section": "성능 조건",
                    "display_order": 10,
                    "required": True,
                }
            ],
        )

    def test_build_response_keeps_missing_required_columns(self):
        item = {
            "name": "ITEM-001",
            "item_code": "ITEM-001",
            "item_name": "유압 실린더",
            "item_group": "Hydraulics",
            "custom_pressure": None,
            "is_stock_item": 1,
        }
        response = build_item_specification_response(
            item,
            [{"fieldname": "custom_pressure", "label": "정격 압력", "required": True}],
        )

        self.assertEqual(response["schema_source"], "item_group_spec")
        self.assertIsNone(response["specification_fields"][0]["value"])
        self.assertEqual(response["missing_required_fields"], ["custom_pressure"])

    def test_build_response_falls_back_to_custom_fields_and_item_attributes(self):
        item = {
            "item_code": "ITEM-002",
            "item_name": "볼 밸브",
            "custom_pressure_class": "JIS 10K",
            "attributes": [{"attribute": "Body Material", "attribute_value": "SUS316L"}],
        }
        response = build_item_specification_response(item, None)
        values = {
            field["fieldname"]: field["value"]
            for field in response["specification_fields"]
        }

        self.assertEqual(response["schema_source"], "erp_custom_fields_fallback")
        self.assertEqual(values["custom_pressure_class"], "JIS 10K")
        self.assertEqual(values["attribute_body_material"], "SUS316L")

    def test_build_response_uses_live_erp_metadata_and_sections(self):
        item = {
            "item_code": "SF-001",
            "item_name": "안전모",
            "item_group": "안전용품",
            "is_stock_item": 1,
            "lead_time_days": 7,
            "description": "<p>백색 안전모</p>",
        }
        metadata = [
            {"fieldtype": "Section Break", "label": "Details"},
            {"fieldname": "item_code", "label": "Item Code", "fieldtype": "Data"},
            {"fieldname": "is_stock_item", "label": "Maintain Stock", "fieldtype": "Check"},
            {"fieldtype": "Section Break", "label": "Purchase Details"},
            {"fieldname": "lead_time_days", "label": "Lead Time Days", "fieldtype": "Int"},
            {"fieldname": "description", "label": "Description", "fieldtype": "Text Editor"},
        ]

        response = build_item_specification_response(
            item, None, metadata_fields=metadata
        )
        fields = {field["fieldname"]: field for field in response["specification_fields"]}

        self.assertEqual(response["schema_source"], "erp_doctype_metadata")
        self.assertNotIn("item_code", fields)
        self.assertNotIn("is_stock_item", fields)
        self.assertNotIn("lead_time_days", fields)
        self.assertEqual(fields["description"]["label"], "규격")
        self.assertEqual(fields["description"]["section"], "기본 규격")
        self.assertEqual(fields["description"]["value"], "백색 안전모")

    def test_product_description_is_not_mixed_with_erp_control_fields(self):
        expected_spec = (
            "규격: 프리사이즈 54~63cm, 충격흡수 내피, 후면 래칫 조절, "
            "4점식 턱끈, 색상 황색"
        )
        item = {
            "item_code": "SAF-HLM-002",
            "item_name": "고소작업용 턱끈 안전모 (황색, 래칫형)",
            "description": expected_spec,
            "is_stock_item": 1,
            "is_fixed_asset": 0,
            "valuation_rate": 12000,
            "allow_negative_stock": 0,
        }
        metadata = [
            {"fieldtype": "Section Break", "label": "Details"},
            {"fieldname": "item_code", "label": "Item Code", "fieldtype": "Data"},
            {"fieldname": "description", "label": "Description", "fieldtype": "Text Editor"},
            {"fieldname": "is_stock_item", "label": "Maintain Stock", "fieldtype": "Check"},
            {"fieldname": "valuation_rate", "label": "Valuation Rate", "fieldtype": "Currency"},
            {"fieldname": "allow_negative_stock", "label": "Allow Negative Stock", "fieldtype": "Check"},
        ]

        response = build_item_specification_response(item, None, metadata_fields=metadata)

        self.assertEqual(
            [(field["fieldname"], field["value"]) for field in response["specification_fields"]],
            [("description", expected_spec)],
        )

    def test_label_only_requirement_remains_visible_when_erp_has_no_field(self):
        response = build_item_specification_response(
            {"item_code": "BRG-001", "item_name": "베어링"},
            ["재질"],
            metadata_fields=[
                {"fieldname": "item_code", "label": "Item Code", "fieldtype": "Data"},
            ],
        )

        required = next(
            field for field in response["specification_fields"] if field["label"] == "재질"
        )
        self.assertTrue(required["required"])
        self.assertIsNone(required["value"])


if __name__ == "__main__":
    unittest.main()
