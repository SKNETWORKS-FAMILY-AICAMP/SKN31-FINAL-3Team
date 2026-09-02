"""ERPNext Item webhook orchestration for automatic AI specification checks."""

from __future__ import annotations

from typing import Any

from backend_logic2.integrations.erp_client import erp_get_one
from backend_logic2.nodes.item.item_spec_validation import validate_new_item
from backend_logic2.repositories import events as event_repository
from backend_logic2.repositories.notifications import create_notification


def register_item_event(
    payload: dict[str, Any], *, event_id: str | None = None
) -> tuple[dict[str, Any], bool]:
    document = payload.get("doc") or payload.get("document") or payload.get("data") or payload
    if not isinstance(document, dict):
        raise ValueError("ERPNext Item webhook document payload가 필요합니다.")
    item_code = str(
        document.get("item_code") or document.get("name") or payload.get("name") or ""
    ).strip()
    if not item_code:
        raise ValueError("Item code가 필요합니다.")
    modified = document.get("modified") or payload.get("modified") or "unknown"
    dedupe_key = event_id or f"erpnext:item:{item_code}:{modified}"
    event, created = event_repository.begin_event(
        source="ERPNEXT",
        event_type="ITEM_CHANGED",
        external_id=item_code,
        dedupe_key=dedupe_key,
        payload=payload,
    )
    if not created:
        return {"item_code": item_code, "status": event.get("status")}, False

    try:
        current = erp_get_one("Item", item_code)
        if not current:
            raise LookupError(f"Item을 찾을 수 없습니다: {item_code}")

        # validate_new_item이 disabled=0으로 바꾸면 Item webhook이 한 번 더
        # 발생할 수 있다. 활성 품목은 skip해 재귀 갱신 루프를 차단한다.
        if int(current.get("disabled") or 0) != 1:
            result = {"item_code": item_code, "approved": True, "skipped": "already_active"}
        else:
            result = validate_new_item(item_code)

        create_notification(
            case_id=None,
            recipient_id=None,
            notification_type=(
                "ITEM_VALIDATION_APPROVED" if result.get("approved") else "ITEM_VALIDATION_REVIEW"
            ),
            title=(
                "신규 아이템 자동 등록이 완료되었습니다"
                if result.get("approved")
                else "신규 아이템 규격 확인이 필요합니다"
            ),
            message=(
                f"{item_code} · AI 규격 검증 완료"
                if result.get("approved")
                else f"{item_code} · 누락 규격: {', '.join(result.get('missing') or []) or '확인 필요'}"
            ),
            payload={"item_code": item_code, "validation": result},
        )
    except Exception as exc:
        event_repository.fail_event(str(event["event_id"]), str(exc))
        raise

    event_repository.complete_event(str(event["event_id"]))
    return result, True
