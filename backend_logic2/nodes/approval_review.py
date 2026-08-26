"""Material Request 승인/반려 Human-in-the-loop 노드.

승인하면 Draft MR을 Submit하고, 반려하면 사유를 Comments에 남긴 뒤
Frappe Desk의 Discard와 동일한 서버 메서드로 문서를 폐기한다.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from erp_client import erp_add_comment, erp_discard_draft, erp_get_one, erp_submit


def _substitute_count(substitute_results: dict | None) -> int:
    return sum(
        len(info.get("substitutes", []))
        for info in (substitute_results or {}).values()
    )


def _print_review(anomaly_result: dict | None, substitute_results: dict | None):
    anomalies = (anomaly_result or {}).get("anomalies", [])
    if anomalies:
        print("\n[검토 필요] 이상 수치가 발견되었습니다.")
        for anomaly in anomalies:
            print(f"  - [{anomaly.get('item_code', 'MR')}] {anomaly['message']}")

    count = _substitute_count(substitute_results)
    if count:
        print(f"\n[검토 필요] 재고로 사용할 수 있는 대체품 {count}건이 있습니다.")
        for item_code, info in substitute_results.items():
            for substitute in sorted(
                info.get("substitutes", []), key=lambda row: row.get("rank") or 999
            ):
                print(
                    f"  - [{item_code}] {substitute['item_name']} "
                    f"({substitute['item_code']}) / 재고 {substitute['total_qty']:g} / "
                    f"{substitute.get('reason') or '대체 가능'}"
                )


def review_material_request(
    mr_name: str,
    anomaly_result: dict | None = None,
    substitute_results: dict | None = None,
    decision: str | None = None,
    rejection_reason: str | None = None,
) -> dict:
    """승인/반려 결정을 받아 파이프라인 분기에 사용할 결과를 반환한다.

    ``decision``은 API/UI 또는 테스트에서 ``approve``/``reject``로 주입할 수
    있다. 값이 없으면 현재 CLI 실행 방식에 맞춰 터미널에서 입력받는다.
    """
    _print_review(anomaly_result, substitute_results)

    has_anomaly = bool((anomaly_result or {}).get("has_anomaly"))
    has_substitute = _substitute_count(substitute_results) > 0
    reasons = []
    if has_anomaly:
        reasons.append("이상 수치 발견")
    if has_substitute:
        reasons.append("사용 가능한 대체품 존재")

    recommendation = "reject" if reasons else "approve"
    reason_text = f" ({', '.join(reasons)})" if reasons else ""
    print(f"\n권고: {'반려' if recommendation == 'reject' else '승인'}{reason_text}")

    normalized = (decision or "").strip().lower()
    while normalized not in {"approve", "reject", "a", "r", "y", "n", "승인", "반려"}:
        normalized = input(f"Material Request '{mr_name}' 승인(a) / 반려(r): ").strip().lower()

    approved = normalized in {"approve", "a", "y", "승인"}
    erp_action = None
    comment = None

    if approved:
        mr = erp_get_one("Material Request", mr_name)
        if not mr:
            raise ValueError(f"Material Request를 찾을 수 없습니다: {mr_name}")
        if mr.get("docstatus") == 0:
            erp_submit("Material Request", mr_name)
            erp_action = "submitted"
        elif mr.get("docstatus") == 1:
            # 이미 제출된 MR을 다시 승인하는 경우에는 멱등적으로 처리한다.
            erp_action = "already_submitted"
        else:
            raise ValueError(f"이미 폐기된 Material Request입니다: {mr_name}")
    else:
        reason = (rejection_reason or "").strip()
        while not reason:
            reason = input("반려 사유를 입력하세요: ").strip()

        comment_text = f"[구매 요청 반려 사유] {reason}"
        comment = erp_add_comment("Material Request", mr_name, comment_text)
        erp_discard_draft("Material Request", mr_name)
        erp_action = "discarded"

    result = {
        "mr_name": mr_name,
        "decision": "approved" if approved else "rejected",
        "recommendation": recommendation,
        "reasons": reasons,
        "has_anomaly": has_anomaly,
        "has_substitute": has_substitute,
        "rejection_reason": None if approved else reason,
        "comment": comment,
        "erp_action": erp_action,
    }
    print(
        f"→ '{mr_name}' "
        f"{'승인 및 Submit' if approved else '반려 사유 등록 및 Discard'} 완료"
    )
    return result
