"""
material_request.py — 1번 모듈: Pending 상태인 Material Request 목록 조회

이 파일 하나로 완결되는 모듈. 다른 모듈(재고조회, 대체품 등)과 안 엮여있음.

실행: python material_request.py
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
 
from erp_client import erp_get

def get_pending_material_requests():
    """
    Pending 상태(=제출됐지만 아직 아무 후속조치 없는 상태)인 Material Request
    전체를 가져옴.
    """
    return erp_get(
        "Material Request",
        filters=[
            ["status", "=", "Pending"],
            ["docstatus", "=", 1],
        ],
        fields=["name", "transaction_date", "schedule_date", "material_request_type", "status"],
    )


if __name__ == "__main__":
    mrs = get_pending_material_requests()

    print(f"Pending Material Request: {len(mrs or [])}건\n")

    for mr in mrs or []:
        print(f"  - {mr['name']} | 유형: {mr.get('material_request_type')} | "
              f"요청일: {mr.get('transaction_date')} | 희망납기: {mr.get('schedule_date')}")