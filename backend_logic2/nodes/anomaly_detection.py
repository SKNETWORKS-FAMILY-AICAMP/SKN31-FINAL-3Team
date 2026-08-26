"""Material Request의 수량·일자 이상치를 판정하는 노드.

판정 규칙은 외부 AI에 맡기지 않고 재현 가능한 규칙으로 유지한다.
과거 동일 품목 요청이 3건 이상이면 중앙값과 MAD를 이용해 수량 이상치를
판정하고, 이력이 부족하면 명백한 입력 오류(0 이하 수량, 요청일보다 빠른
희망납기)만 확인한다.
"""

import os
import statistics
import sys
from datetime import date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from erp_client import erp_get, erp_get_one


MIN_HISTORY_COUNT = 3
MAD_Z_THRESHOLD = 3.0


def _as_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _historical_quantities(item_code: str, current_mr_name: str) -> list[float]:
    """과거 제출된 MR에서 동일 품목의 요청수량을 수집한다."""
    rows = erp_get(
        "Material Request",
        filters=[
            ["docstatus", "=", 1],
            ["name", "!=", current_mr_name],
            ["Material Request Item", "item_code", "=", item_code],
        ],
        fields=["name"],
        order_by="transaction_date desc",
        limit=100,
    )

    quantities = []
    for row in rows or []:
        historical_mr = erp_get_one("Material Request", row["name"])
        for item in (historical_mr or {}).get("items", []):
            if item.get("item_code") != item_code:
                continue
            qty = _as_number(item.get("qty"))
            if qty is not None and qty > 0:
                quantities.append(qty)
    return quantities


def _quantity_anomaly(qty: float, history: list[float]):
    if len(history) < MIN_HISTORY_COUNT:
        return None

    median = statistics.median(history)
    mad = statistics.median(abs(value - median) for value in history)

    if mad > 0:
        robust_z = abs(qty - median) / (1.4826 * mad)
        if robust_z > MAD_Z_THRESHOLD:
            return {
                "type": "quantity_outlier",
                "message": f"요청수량 {qty:g}은 과거 중앙값 {median:g}과 큰 차이가 있습니다.",
                "current_qty": qty,
                "history_count": len(history),
                "history_median": median,
                "robust_z_score": round(robust_z, 2),
            }
        return None

    # 과거 수량이 모두 같아 MAD가 0이면 중앙값 대비 2배/절반을 경계로 사용한다.
    if median > 0 and (qty > median * 2 or qty < median * 0.5):
        return {
            "type": "quantity_outlier",
            "message": f"요청수량 {qty:g}은 반복된 과거 수량 {median:g}의 정상 범위를 벗어납니다.",
            "current_qty": qty,
            "history_count": len(history),
            "history_median": median,
            "robust_z_score": None,
        }
    return None


def detect_material_request_anomalies(mr_name: str) -> dict:
    """MR 전체를 검사하고 승인/반려 노드가 사용할 구조화 결과를 반환한다."""
    mr = erp_get_one("Material Request", mr_name)
    if not mr:
        raise ValueError(f"Material Request를 찾을 수 없습니다: {mr_name}")

    anomalies = []
    transaction_date = _as_date(mr.get("transaction_date"))
    schedule_date = _as_date(mr.get("schedule_date"))
    if transaction_date and schedule_date and schedule_date < transaction_date:
        anomalies.append({
            "type": "invalid_schedule_date",
            "message": "희망납기가 요청일보다 빠릅니다.",
            "transaction_date": str(transaction_date),
            "schedule_date": str(schedule_date),
        })

    checked_items = []
    for line in mr.get("items", []):
        item_code = line.get("item_code")
        qty = _as_number(line.get("qty"))
        checked_items.append(item_code)

        if qty is None or qty <= 0:
            anomalies.append({
                "type": "invalid_quantity",
                "item_code": item_code,
                "message": f"요청수량이 올바르지 않습니다: {line.get('qty')}",
            })
            continue

        history = _historical_quantities(item_code, mr_name)
        anomaly = _quantity_anomaly(qty, history)
        if anomaly:
            anomalies.append({"item_code": item_code, **anomaly})

    return {
        "mr_name": mr_name,
        "has_anomaly": bool(anomalies),
        "anomalies": anomalies,
        "checked_items": checked_items,
    }


if __name__ == "__main__":
    target_mr = input("Material Request ID 입력: ").strip()
    result = detect_material_request_anomalies(target_mr)
    print(f"이상 여부: {'있음' if result['has_anomaly'] else '없음'}")
    for detected in result["anomalies"]:
        print(f"  - [{detected.get('item_code', 'MR')}] {detected['message']}")
