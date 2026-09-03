"""Synchronize ERPNext Supplier Quotations with the procurement read model.

LangGraph owns workflow execution state.  This module deliberately stores live
quotation responses in ``procurement_case.quotation_snapshot`` instead of
rewriting a graph checkpoint.  Both polling and webhooks therefore feed the
same projection without moving a human approval step automatically.
"""

from __future__ import annotations

from typing import Any

from backend_logic2.integrations.erp_client import ERPNextAPIError, erp_get_one
from backend_logic2.nodes.quotation.sq_evaluation import get_quotations_for_rfq
from backend_logic2.repositories import cases as case_repository
from backend_logic2.repositories import events as event_repository
from backend_logic2.repositories import notifications as notification_repository
from backend_logic2.workflow.process_commands import to_checkpoint_data


def _document(payload: dict[str, Any]) -> dict[str, Any]:
    document = payload.get("doc") or payload.get("document") or payload.get("data") or payload
    if not isinstance(document, dict):
        raise ValueError("ERPNext Supplier Quotation webhook document가 필요합니다.")
    return document


def _rfq_names(document: dict[str, Any]) -> list[str]:
    names: set[str] = set()
    for key in ("request_for_quotation", "rfq_name"):
        value = str(document.get(key) or "").strip()
        if value:
            names.add(value)
    for item in document.get("items") or []:
        if not isinstance(item, dict):
            continue
        value = str(item.get("request_for_quotation") or "").strip()
        if value:
            names.add(value)
    return sorted(names)


def _workflow_values(case: dict[str, Any]) -> dict[str, Any]:
    snapshot = case.get("workflow_snapshot") or {}
    values = snapshot.get("values") if isinstance(snapshot, dict) else {}
    return values if isinstance(values, dict) else {}


def _supplier_names(rows: list[dict[str, Any]]) -> set[str]:
    return {
        str(row.get("supplier") or row.get("supplier_name") or "").strip()
        for row in rows
        if str(row.get("supplier") or row.get("supplier_name") or "").strip()
    }


def build_quotation_snapshot(case: dict[str, Any], rfq_name: str) -> dict[str, Any]:
    """Read active SQ documents and calculate response rate from actual recipients."""

    quotations = [
        to_checkpoint_data(row)
        for row in get_quotations_for_rfq(rfq_name)
    ]
    values = _workflow_values(case)
    recipients = {
        str(value).strip()
        for value in values.get("selected_suppliers") or []
        if str(value).strip()
    }
    responders = _supplier_names(quotations)
    counted_responders = responders & recipients if recipients else responders
    recipient_count = len(recipients)
    responded_count = len(counted_responders)
    response_rate = (
        min(100, round(responded_count / recipient_count * 100))
        if recipient_count
        else 0
    )
    return {
        "rfq_name": rfq_name,
        "recipient_suppliers": sorted(recipients),
        "responded_suppliers": sorted(counted_responders),
        "recipient_count": recipient_count,
        "responded_count": responded_count,
        "response_rate": response_rate,
        "quotations": quotations,
    }


def refresh_case_quotations(
    case: dict[str, Any],
    *,
    rfq_name: str | None = None,
    notify: bool = True,
) -> tuple[dict[str, Any], bool]:
    """Refresh one case and publish an SSE-backed notification when it changed."""

    values = _workflow_values(case)
    resolved_rfq = str(rfq_name or values.get("rfq_name") or "").strip()
    if not resolved_rfq:
        raise ValueError("구매 작업에 연결된 Request for Quotation이 없습니다.")

    previous = case.get("quotation_snapshot") or {}
    previous = previous if isinstance(previous, dict) else {}
    current = build_quotation_snapshot(case, resolved_rfq)
    updated, changed = case_repository.update_quotation_snapshot(
        str(case["case_id"]), current
    )
    if not changed or not notify:
        return updated, changed

    previous_names = _supplier_names(previous.get("quotations") or [])
    current_names = _supplier_names(current["quotations"])
    new_names = sorted(current_names - previous_names)
    removed_names = sorted(previous_names - current_names)
    if new_names:
        title = "신규 견적 회신이 도착했습니다"
        detail = f"신규 회신 {', '.join(new_names)}"
        notification_type = "SUPPLIER_QUOTATION_RECEIVED"
    elif removed_names:
        title = "견적 회신 상태가 변경되었습니다"
        detail = f"취소·삭제 {', '.join(removed_names)}"
        notification_type = "SUPPLIER_QUOTATION_REMOVED"
    elif previous:
        title = "협력사 견적 내용이 변경되었습니다"
        detail = "금액·납기 등 최신 내용을 반영했습니다"
        notification_type = "SUPPLIER_QUOTATION_UPDATED"
    else:
        # Empty initial baseline is useful for future comparisons but is not a
        # user-visible event.
        return updated, changed

    notification_repository.create_notification(
        case_id=str(case["case_id"]),
        recipient_id=case.get("assigned_user_id"),
        notification_type=notification_type,
        title=title,
        message=(
            f"{case.get('mr_name') or resolved_rfq} · {detail} · "
            f"회신 {current['responded_count']}/{current['recipient_count']}건"
        ),
        payload={
            "mr_name": case.get("mr_name"),
            "rfq_name": resolved_rfq,
            "stage": case.get("stage"),
            "responded_count": current["responded_count"],
            "recipient_count": current["recipient_count"],
            "response_rate": current["response_rate"],
            "new_suppliers": new_names,
            "removed_suppliers": removed_names,
        },
    )
    return updated, changed


def register_supplier_quotation_event(
    payload: dict[str, Any], *, event_id: str | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """Process one idempotent SQ create/update/submit/cancel/delete event."""

    document = _document(payload)
    quotation_name = str(document.get("name") or "").strip()
    if not quotation_name:
        raise ValueError("Supplier Quotation name이 필요합니다.")
    modified = document.get("modified") or document.get("creation") or "unknown"
    event_kind = str(payload.get("event") or payload.get("method") or "changed")
    event, created = event_repository.begin_event(
        source="ERPNEXT",
        event_type="SUPPLIER_QUOTATION_CHANGED",
        external_id=quotation_name,
        dedupe_key=(
            event_id
            or f"erpnext:supplier_quotation:{quotation_name}:{modified}:{event_kind}"
        ),
        payload=payload,
    )
    if not created:
        return [], False

    try:
        rfq_names = _rfq_names(document)
        if not rfq_names and event_kind != "on_trash":
            current = erp_get_one("Supplier Quotation", quotation_name) or {}
            rfq_names = _rfq_names(current)
        if not rfq_names and event_kind == "on_trash":
            previous_case = case_repository.get_case_by_supplier_quotation(
                quotation_name
            )
            if previous_case is not None:
                previous_rfq = str(
                    _workflow_values(previous_case).get("rfq_name") or ""
                ).strip()
                if previous_rfq:
                    rfq_names = [previous_rfq]
        if not rfq_names:
            raise ValueError(
                f"Supplier Quotation {quotation_name}에서 RFQ 연결 정보를 찾지 못했습니다."
            )

        projections: list[dict[str, Any]] = []
        for rfq_name in rfq_names:
            case = case_repository.get_case_by_rfq(rfq_name)
            if case is None:
                projections.append({"rfq_name": rfq_name, "matched": False})
                continue
            updated, changed = refresh_case_quotations(
                case,
                rfq_name=rfq_name,
                notify=True,
            )
            projections.append(
                {
                    "rfq_name": rfq_name,
                    "matched": True,
                    "changed": changed,
                    "case_id": str(updated["case_id"]),
                    "quotation_snapshot": updated.get("quotation_snapshot") or {},
                }
            )
    except Exception as exc:
        event_repository.fail_event(str(event["event_id"]), str(exc))
        raise
    event_repository.complete_event(str(event["event_id"]))
    return projections, True


def reconcile_supplier_quotations(*, notify: bool = True) -> dict[str, int]:
    """Recover Supplier Quotation events missed while the API was offline."""

    counts = {"cases": 0, "changed": 0, "failed": 0}
    for case in case_repository.list_cases_for_quotation_reconciliation():
        counts["cases"] += 1
        try:
            _, changed = refresh_case_quotations(case, notify=notify)
            if changed:
                counts["changed"] += 1
        except (ERPNextAPIError, LookupError, ValueError):
            counts["failed"] += 1
    return counts
