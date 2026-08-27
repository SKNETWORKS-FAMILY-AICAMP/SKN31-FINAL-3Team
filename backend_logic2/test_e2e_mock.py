import unittest
from unittest.mock import patch
import erp_client

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
        
        # 에러 수정: supplier 필드와 warehouse 필드를 목 데이터에 추가
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
        # 핵심 수정: side_effect를 끄고 단일 리턴값으로 돌려줌
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

if __name__ == '__main__':
    unittest.main()