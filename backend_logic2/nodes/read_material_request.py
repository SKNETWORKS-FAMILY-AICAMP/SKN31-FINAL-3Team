"""
material_request.py — 1번 모듈: Pending 상태인 Material Request 목록 조회

Pending MR을 조회하고, 선택한 요청을 phaseA 처리 흐름으로 넘길 수 있음.

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


def process_material_request(mr_name: str):
    """조회한 MR을 이상치 판단부터 시작하는 phaseA 흐름으로 전달한다."""
    # phaseA가 이 모듈을 다시 import할 가능성을 막고 조회 전용 사용에서는 무거운
    # 의존성을 로드하지 않도록 함수 안에서 import한다.
    from phaseA import run_intake_pipeline

    return run_intake_pipeline(mr_name)


if __name__ == "__main__":
    mrs = get_pending_material_requests()

    print(f"승인 대기 Draft Material Request: {len(mrs or [])}건\n")

    for mr in mrs or []:
        print(f"  - {mr['name']} | 유형: {mr.get('material_request_type')} | "
              f"요청일: {mr.get('transaction_date')} | 희망납기: {mr.get('schedule_date')}")

    if mrs:
        mr_name = input("\n처리할 Material Request ID (Enter=종료): ").strip()
        if mr_name:
            pending_names = {mr["name"] for mr in mrs}
            if mr_name not in pending_names:
                print("승인 대기 Draft 목록에 없는 Material Request입니다.")
            else:
                process_material_request(mr_name)
