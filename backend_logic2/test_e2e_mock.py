import unittest
from unittest.mock import patch
from datetime import datetime, timedelta

import erp_client
from nodes.confirm_trade import confirm_trade_node


class TestNexterpE2E(unittest.TestCase):
    
    @patch('erp_client.erp_submit')
    @patch('erp_client.erp_post')
    @patch('erp_client.erp_get_one')
    @patch('erp_client.erp_get')
    def test_full_procurement_pipeline(self, mock_get, mock_get_one, mock_post, mock_submit):
        print("\n=== [1단계] 재고 감지 및 구매 요청(MR) 생성 ===")
        # Mock Data: 재고는 5개 남았고, 재주문 기준은 10개, 발주 수량은 50개로 가정
        mock_get.return_value = [{"item_code": "SF-001", "warehouse": "Stores - SKN31", "actual_qty": 5, "projected_qty": 5}]
        mock_get_one.return_value = {
            "item_code": "SF-001", 
            "item_name": "Safety Helmet",
            "standard_rate": 15000,
            "reorder_levels": [{"warehouse": "Stores - SKN31", "warehouse_reorder_level": 10, "warehouse_reorder_qty": 50}],
            "supplier_items": [{"supplier": "안전제일(주)"}, {"supplier": "튼튼보호구"}]
        }
        
        needs_reorder, reorder_qty = erp_client.check_reorder_needed("SF-001", "Stores - SKN31")
        self.assertTrue(needs_reorder)
        print(f"재고 부족 감지! 필요 수량: {reorder_qty}")

        mock_post.return_value = {"name": "MR-2026-0001", "transaction_date": "2026-09-01"}
        mr = erp_client.create_material_request("SF-001", reorder_qty, "Stores - SKN31", "2026-09-10")
        print(f"구매 요청서(MR) 생성 완료: {mr['name']}")

        print("\n=== [2단계] 구매 경로 판별 (카탈로그 vs 비딩) ===")
        mock_get.return_value = [] 
        route, reasons = erp_client.classify_procurement_route("SF-001", reorder_qty)
        print(f"판별 결과: {route} / 사유: {reasons[0]}")

        print("\n=== [3단계] 공급사 선정 및 견적 요청(RFQ) 발송 ===")
        selected_suppliers, _ = erp_client.select_suppliers_for_rfq("SF-001", max_suppliers=2)
        print(f"선정된 공급사: {selected_suppliers}")

        mock_get_one.return_value = {
            "name": "MR-2026-0001", 
            "transaction_date": "2026-09-01",
            "items": [{"item_code": "SF-001", "qty": 50, "schedule_date": "2026-09-10", "warehouse": "Stores - SKN31", "name": "MR-ITEM-01"}]
        }
        mock_post.return_value = {"name": "RFQ-2026-0001"}
        rfq = erp_client.create_rfq_from_material_request(mr["name"], selected_suppliers)
        print(f"견적 요청서(RFQ) 생성 및 발송 완료: {rfq['name']}")

        print("\n=== [4단계] 견적(Quotation) 수취 및 비교 ===")
        mock_get.return_value = [
            {"name": "SQ-001", "supplier": "안전제일(주)", "grand_total": 750000, "status": "Submitted"},
            {"name": "SQ-002", "supplier": "튼튼보호구", "grand_total": 700000, "status": "Submitted"} 
        ]
        
        # supplier 필드와 warehouse 필드를 목 데이터에 추가
        def mock_get_one_side_effect(doctype, name):
            if doctype == "Supplier Quotation":
                return {
                    "name": name, 
                    "supplier": "안전제일(주)" if name == "SQ-001" else "튼튼보호구",
                    "items": [{"item_code": "SF-001", "qty": 50, "rate": 15000 if name=="SQ-001" else 14000, "request_for_quotation": "RFQ-2026-0001", "warehouse": "Stores - SKN31"}]
                }
            return {}
            
        mock_get_one.side_effect = mock_get_one_side_effect
        
        lowest_quote = erp_client.get_lowest_quotation("RFQ-2026-0001")
        print(f"최저가 견적 선정: {lowest_quote['supplier']} (총액: {lowest_quote['grand_total']}원)")

        print("\n=== [5단계] 발주서(PO) 생성 ===")
        mock_get_one.side_effect = None 
        mock_get_one.return_value = {
            "name": lowest_quote["name"],
            "supplier": lowest_quote["supplier"],
            "items": [{"item_code": "SF-001", "qty": 50, "rate": 14000, "warehouse": "Stores - SKN31"}]
        }
        mock_post.return_value = {"name": "PO-2026-0001"}
        po = erp_client.create_po_from_quotation(lowest_quote["name"], "2026-09-10")
        print(f"발주서(PO) 생성 및 Submit 완료: {po['name']}")

        print("\n=== [6단계] 물품 입고(PR) 및 매입송장(PI) 생성 ===")
        mock_get_one.return_value = {
            "name": "PO-2026-0001", "supplier": lowest_quote["supplier"],
            "items": [{"name": "PO-ITEM-01", "item_code": "SF-001", "qty": 50, "rate": 14000, "warehouse": "Stores - SKN31"}]
        }
        mock_post.return_value = {"name": "PR-2026-0001"}
        pr = erp_client.create_purchase_receipt("PO-2026-0001", [{"item_code": "SF-001", "qty": 50}])
        print(f"입고 처리(PR) 완료: {pr['name']}")

        mock_get_one.return_value = {
            "name": "PR-2026-0001", "supplier": lowest_quote["supplier"],
            "items": [{"item_code": "SF-001", "qty": 50, "rate": 14000, "purchase_order": "PO-2026-0001", "purchase_order_item": "PO-ITEM-01"}]
        }
        mock_post.return_value = {"name": "PI-2026-0001"}
        pi = erp_client.create_purchase_invoice_draft("PR-2026-0001")
        print(f"매입송장(PI) 초안 생성 완료: {pi['name']}")
        print("\n🎉 End-to-End 최소 시나리오 테스트 성공!")

    def test_8_3_trade_confirmation_scenarios(self):
        """[8-3 단계] 거래 확정 확인, 재촉 메일 발송, 거절 시 차순위 재진행 E2E 검증"""
        base_time = datetime.now()

        print("\n=== [8-3 단계 시나리오 1] 일정 기간 미응답 시 자동 재확인 메일 발송 ===")
        state_reminder = {
            "selected_suppliers": [
                {
                    "name": "안전제일(주)",
                    "status": "PENDING",
                    "pr_sent_at": base_time - timedelta(days=3),
                    "last_reminded_at": base_time - timedelta(days=3),
                    "reminder_count": 0
                }
            ],
            "current_supplier_index": 0,
            "max_reminder_days": 2,
            "max_no_response_days": 5
        }
        res_reminder = confirm_trade_node(state_reminder)
        print(f"결과: {res_reminder['process_status']} | 로그: {res_reminder['log_message']}")
        self.assertEqual(res_reminder["process_status"], "REMINDER_SENT")
        self.assertEqual(res_reminder["selected_suppliers"][0]["reminder_count"], 1)

        print("\n=== [8-3 단계 시나리오 2] 1순위 거래 거절 -> 차순위(2순위) 강제 재진행 ===")
        state_fallback = {
            "selected_suppliers": [
                {
                    "name": "안전제일(주)",
                    "status": "REJECTED",
                    "pr_sent_at": base_time - timedelta(days=1),
                    "last_reminded_at": base_time - timedelta(days=1)
                },
                {
                    "name": "튼튼보호구",
                    "status": "PENDING",
                    "pr_sent_at": None,
                    "last_reminded_at": None
                }
            ],
            "current_supplier_index": 0,
            "max_reminder_days": 2,
            "max_no_response_days": 5
        }
        res_fallback = confirm_trade_node(state_fallback)
        print(f"결과: {res_fallback['process_status']} | 로그: {res_fallback['log_message']}")
        self.assertEqual(res_fallback["process_status"], "FALLBACK_NEXT_SUPPLIER")
        self.assertEqual(res_fallback["current_supplier_index"], 1)

        print("\n=== [8-3 단계 시나리오 3] 공급사 거래 확정 메일 수신 -> 8-4 PO 발송 준비 ===")
        state_confirmed = {
            "selected_suppliers": [
                {
                    "name": "튼튼보호구",
                    "status": "CONFIRMED",
                    "pr_sent_at": base_time,
                    "last_reminded_at": base_time
                }
            ],
            "current_supplier_index": 0,
            "max_reminder_days": 2,
            "max_no_response_days": 5
        }
        res_confirmed = confirm_trade_node(state_confirmed)
        print(f"결과: {res_confirmed['process_status']} | 확정 공급사: {res_confirmed.get('confirmed_supplier', {}).get('name')}")
        self.assertEqual(res_confirmed["process_status"], "TRADE_CONFIRMED")
        self.assertEqual(res_confirmed["confirmed_supplier"]["name"], "튼튼보호구")
        print("\n🎉 8-3 거래 확정 및 재진행 시나리오 검증 완료!")


if __name__ == '__main__':
    unittest.main()