"""
material_request.py — Pending 상태인 Material Request 목록 조회

실행: python material_request.py
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
 
from erp_client import erp_get

def get_pending_material_requests():
    """
    승인/반려를 기다리는 Draft 상태의 Material Request 전체를 가져옴.
    """
    return erp_get(
        "Material Request",
        filters=[
            ["status", "=", "Draft"],
            ["docstatus", "=", 0],
        ],
        fields=["name", "transaction_date", "schedule_date", "material_request_type", "status"],
    )


if __name__ == "__main__":
    mrs = get_pending_material_requests()

    print(f"승인 대기 Draft Material Request: {len(mrs or [])}건\n")

    for mr in mrs or []:
        print(f"  - {mr['name']} | 유형: {mr.get('material_request_type')} | "
              f"요청일: {mr.get('transaction_date')} | 희망납기: {mr.get('schedule_date')}")

    if mrs:
        print("\n처리는 process_cli start --mr <MR ID> 명령으로 시작하세요.")
