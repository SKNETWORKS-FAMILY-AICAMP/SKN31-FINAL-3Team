"""ERPNext Item webhook orchestration for automatic AI specification checks."""

from __future__ import annotations

import logging
from typing import Any

from backend_logic2.integrations.erp_client import erp_get, erp_get_one
from backend_logic2.nodes.item.item_spec_validation import validate_new_item
from backend_logic2.repositories import events as event_repository
from backend_logic2.repositories.notifications import create_notification


LOGGER = logging.getLogger(__name__)


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

        # 자동 활성화가 다시 발생시킨 on_update 웹훅에서는 같은 성공 알림을
        # 중복 생성하지 않는다.
        if not result.get("skipped"):
            create_notification(
                case_id=None,
                recipient_id=None,
                notification_type=(
                    "ITEM_VALIDATION_APPROVED"
                    if result.get("approved")
                    else "ITEM_VALIDATION_REVIEW"
                ),
                title=(
                    "신규 아이템 자동 등록이 완료되었습니다"
                    if result.get("approved")
                    else "신규 아이템 규격 확인이 필요합니다"
                ),
                message=(
                    f"{item_code} · AI 규격 검증 완료"
                    if result.get("approved")
                    else (
                        f"{item_code} · 누락 규격: "
                        f"{', '.join(result.get('missing') or []) or '확인 필요'}"
                    )
                ),
                payload={"item_code": item_code, "validation": result},
            )
    except Exception as exc:
        event_repository.fail_event(str(event["event_id"]), str(exc))
        raise

    event_repository.complete_event(str(event["event_id"]))
    return result, True


def reconcile_disabled_items(*, page_size: int = 500) -> dict[str, int]:
    """웹훅 누락 또는 polling 개발 환경의 비활성 Item을 다시 검증한다.

    Item의 ``modified``를 dedupe key로 쓰므로 이미 처리한 동일 버전은 AI를
    다시 호출하지 않는다. 사용자가 description을 수정해 저장하면 modified가
    바뀌어 새 검증으로 인식된다.
    """

    inspected = 0
    processed = 0
    failed = 0
    offset = 0
    pending_items: list[dict[str, Any]] = []
    while True:
        items = erp_get(
            "Item",
            filters=[["disabled", "=", 1]],
            fields=["name", "item_code", "modified", "disabled"],
            order_by="modified asc",
            limit=page_size,
            start=offset,
        ) or []
        pending_items.extend(items)
        if len(items) < page_size:
            break
        offset += page_size

    # 조회 도중 Item을 활성화하면 offset 기반 페이지가 당겨져 일부 행을
    # 건너뛸 수 있으므로, 전체 식별자 스냅숏을 확보한 다음 처리한다.
    for item in pending_items:
        inspected += 1
        try:
            _result, created = register_item_event(
                {"event": "reconcile", "doc": item}
            )
            if created:
                processed += 1
        except Exception:
            # 한 Item의 잘못된 데이터나 일시적인 AI/ERP 실패 때문에 뒤의
            # 모든 신규 Item이 굶지 않게 개별 실패로 기록하고 계속 진행한다.
            failed += 1
            LOGGER.exception(
                "Disabled Item reconciliation failed for %s",
                item.get("item_code") or item.get("name"),
            )
    return {"inspected": inspected, "processed": processed, "failed": failed}
