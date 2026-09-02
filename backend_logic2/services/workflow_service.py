"""End-to-end orchestration service used by HTTP routes and webhooks."""

from __future__ import annotations

from threading import RLock
from typing import Any
from datetime import datetime, timezone

from langgraph.types import Command

from backend_logic2.integrations.erp_client import ERPNextAPIError, erp_get_one
from backend_logic2.nodes.mr.read_material_request import get_pending_material_requests
from backend_logic2.nodes.mr.reject_material_request import reject_material_request
from backend_logic2.repositories import cases as case_repository
from backend_logic2.repositories import events as event_repository
from backend_logic2.repositories import notifications as notification_repository
from backend_logic2.repositories import tasks as task_repository
from backend_logic2.workflow.process_commands import to_checkpoint_data
from backend_logic2.workflow.process_graph import get_process_app

from .workflow_projection import (
    project_graph_status,
    task_input_schema,
    task_presentation,
)


_GRAPH_LOCK = RLock()

_TASK_STAGE = {
    "substitute_selection": "SUBSTITUTE_DECISION",
    "supplier_approval": "RFQ_TARGET_SELECTION",
    "rfq_target_selection": "RFQ_TARGET_SELECTION",
    "select_rfq_targets": "RFQ_TARGET_SELECTION",
    "quotation_check": "QUOTATION_COLLECTION",
    "check_quotations": "QUOTATION_COLLECTION",
    "final_selection": "SUPPLIER_SELECTION",
    "order_start": "ORDER_START",
    "po_approval": "PRE_PO_APPROVAL",
    "supplier_scorecard": "SCORECARD",
}

_TERMINAL_CASE_STATUSES = {"COMPLETED", "CANCELLED", "REJECTED"}


def _create_notification_safely(**kwargs: Any) -> None:
    """알림 장애가 ERP/AI 본 작업의 성공 여부를 뒤집지 않게 격리합니다."""
    try:
        notification_repository.create_notification(**kwargs)
    except Exception as exc:  # 알림은 보조 채널이며 원 업무 트랜잭션과 분리한다.
        print(f"[workflow notification] 알림 생성 실패: {exc}")


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _interrupt_payloads(snapshot: Any) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            value = getattr(item, "value", item)
            if isinstance(value, dict):
                payloads.append(to_checkpoint_data(value))
            else:
                payloads.append({"type": "workflow_input", "value": to_checkpoint_data(value)})
    return payloads


def _validate_erp_mr(
    mr_name: str, *, allow_submitted: bool = False
) -> dict[str, Any]:
    material_request = erp_get_one("Material Request", mr_name)
    if material_request is None:
        raise LookupError(f"Material Request를 찾을 수 없습니다: {mr_name}")
    docstatus = int(material_request.get("docstatus") or 0)
    is_draft = docstatus == 0 and material_request.get("status") == "Draft"
    is_retryable_submission = allow_submitted and docstatus == 1
    if not is_draft and not is_retryable_submission:
        if docstatus == 2:
            raise ValueError(f"취소된 Material Request는 재개할 수 없습니다: {mr_name}")
        raise ValueError(f"Draft 상태의 Material Request만 시작할 수 있습니다: {mr_name}")
    items = material_request.get("items") or []
    if len(items) != 1:
        raise ValueError(f"MR당 품목은 정확히 1개여야 합니다: {mr_name} (현재 {len(items)}개)")
    return material_request


def register_material_request(mr_name: str) -> dict[str, Any]:
    return case_repository.upsert_case_from_material_request(_validate_erp_mr(mr_name))


def _is_missing_erp_document(exc: ERPNextAPIError) -> bool:
    return "404" in str(exc)


def _close_case_missing_in_erp(case: dict[str, Any], *, triggered_by: str) -> dict[str, Any]:
    case_id = str(case["case_id"])
    reason = "ERPNext에서 Material Request가 삭제되어 대사 과정에서 종료했습니다."
    closed = case_repository.transition_case(
        case_id,
        status="CANCELLED",
        stage="CANCELLED",
        reason=reason,
        triggered_by=triggered_by,
    )
    task_repository.cancel_pending_tasks(case_id, reason=reason)
    return closed


def sync_draft_material_requests(*, reconcile_existing: bool = True) -> list[dict[str, Any]]:
    """Synchronize Draft MRs and optionally reconcile every open cached case.

    ``reconcile_existing=False`` is the lightweight development polling path.
    It discovers/upserts current Draft MRs without issuing one ERP lookup per
    already-open case. Login/startup keeps the full reconciliation enabled.
    """

    cases_by_mr: dict[str, dict[str, Any]] = {}
    for summary in get_pending_material_requests() or []:
        mr_name = summary.get("name")
        if not mr_name:
            continue
        try:
            existing_case = case_repository.get_case_by_mr(str(mr_name))
            case = register_material_request(str(mr_name))
            cases_by_mr[str(mr_name)] = case
            # ERPNext가 삭제된 문서 번호를 재사용하면 repository가 이전 종료
            # 케이스를 보관용 이름으로 옮기고 새 case_id를 발급합니다.
            is_new_case = (
                existing_case is None
                or str(existing_case.get("case_id")) != str(case.get("case_id"))
            )
            if is_new_case:
                case_summary = case.get("summary") or {}
                _create_notification_safely(
                    case_id=str(case["case_id"]),
                    recipient_id=case.get("assigned_user_id"),
                    notification_type="MATERIAL_REQUEST_CREATED",
                    title="미처리 신규 MR을 불러왔습니다",
                    message=(
                        f"{mr_name} · "
                        f"{case.get('item_name') or case_summary.get('item_name') or '품목명 미지정'}"
                    ),
                    payload={
                        "mr_name": str(mr_name),
                        "item_code": case.get("item_code"),
                        "stage": case.get("stage"),
                    },
                )
        except (ERPNextAPIError, LookupError, ValueError):
            # One malformed document must not prevent other pending MRs from
            # appearing.  The caller receives all successfully reconciled rows.
            continue

    if not reconcile_existing:
        return list(cases_by_mr.values())

    # PostgreSQL is a read model, not the MR source of truth. Reconcile every
    # open case, not only migration-era rows with an empty item projection.
    # This keeps a deleted ERP document from surviving forever in the UI.
    for persisted_case in case_repository.list_open_case_references():
        mr_name = str(persisted_case.get("mr_name") or "").strip()
        if not mr_name or mr_name in cases_by_mr:
            continue
        try:
            material_request = erp_get_one("Material Request", mr_name)
        except ERPNextAPIError as exc:
            # Authentication/network failures must never be mistaken for a
            # deletion. Only an explicit ERP 404 closes the cached case.
            if _is_missing_erp_document(exc) and persisted_case.get("case_id"):
                _close_case_missing_in_erp(persisted_case, triggered_by="reconciliation")
            continue

        if material_request is None:
            _close_case_missing_in_erp(persisted_case, triggered_by="reconciliation")
            continue
        if int(material_request.get("docstatus") or 0) == 2:
            reason = "ERPNext에서 Material Request가 취소되어 대사 과정에서 종료했습니다."
            closed = case_repository.transition_case(
                str(persisted_case["case_id"]),
                status="CANCELLED",
                stage="CANCELLED",
                reason=reason,
                triggered_by="reconciliation",
            )
            task_repository.cancel_pending_tasks(str(persisted_case["case_id"]), reason=reason)
            cases_by_mr[mr_name] = closed
            continue
        if len(material_request.get("items") or []) == 1:
            # ON CONFLICT refreshes the ERP projection while preserving the
            # workflow status of already-running/submitted cases.
            cases_by_mr[mr_name] = case_repository.upsert_case_from_material_request(
                material_request
            )
    return list(cases_by_mr.values())


def register_material_request_event(
    payload: dict[str, Any], *, event_id: str | None = None
) -> tuple[dict[str, Any], bool]:
    document = payload.get("doc") or payload.get("document") or payload.get("data") or payload
    if not isinstance(document, dict):
        raise ValueError("ERPNext webhook document payload가 필요합니다.")
    mr_name = str(document.get("name") or payload.get("name") or "").strip()
    if not mr_name:
        raise ValueError("Material Request name이 필요합니다.")
    modified = document.get("modified") or payload.get("modified") or "unknown"
    dedupe_key = event_id or f"erpnext:material_request:{mr_name}:{modified}"
    event, created = event_repository.begin_event(
        source="ERPNEXT",
        event_type="MATERIAL_REQUEST_CHANGED",
        external_id=mr_name,
        dedupe_key=dedupe_key,
        payload=payload,
    )
    if not created:
        existing = case_repository.get_case_by_mr(mr_name)
        return existing or {"mr_name": mr_name}, False

    try:
        existing_case = case_repository.get_case_by_mr(mr_name)
        try:
            current = erp_get_one("Material Request", mr_name)
        except ERPNextAPIError as exc:
            # An optional ERP on_trash webhook reaches the same endpoint after
            # the document has disappeared. Close its read model instead of
            # turning a valid deletion event into a permanent 502 error.
            if _is_missing_erp_document(exc) and existing_case is not None:
                case = _close_case_missing_in_erp(
                    existing_case, triggered_by="erpnext_webhook"
                )
                event_repository.complete_event(str(event["event_id"]))
                return case, True
            raise
        if current is None:
            if existing_case is not None:
                case = _close_case_missing_in_erp(
                    existing_case, triggered_by="erpnext_webhook"
                )
                event_repository.complete_event(str(event["event_id"]))
                return case, True
            raise LookupError(f"Material Request를 찾을 수 없습니다: {mr_name}")
        docstatus = int(current.get("docstatus") or 0)
        if docstatus == 0 and current.get("status") == "Draft":
            case = case_repository.upsert_case_from_material_request(current)
            # 신규 MR은 DB inbox에 영구 보관하고 PostgreSQL NOTIFY를 통해
            # 접속 중인 프론트에도 즉시 전달합니다. 수정 웹훅은 신규 작업
            # 알림으로 오인하지 않도록 최초 케이스 생성일 때만 발행합니다.
            if existing_case is None:
                summary = case.get("summary") or {}
                _create_notification_safely(
                    case_id=str(case["case_id"]),
                    recipient_id=case.get("assigned_user_id"),
                    notification_type="MATERIAL_REQUEST_CREATED",
                    title="신규 MR이 도착했습니다",
                    message=(
                        f"{mr_name} · "
                        f"{case.get('item_name') or summary.get('item_name') or '품목명 미지정'}"
                    ),
                    payload={
                        "mr_name": mr_name,
                        "item_code": case.get("item_code"),
                        "stage": case.get("stage"),
                    },
                )
        else:
            # Webhook은 Insert뿐 아니라 Submit/Cancel 갱신에도 올 수 있다.
            # 이미 실행 중인 케이스를 Draft 초기 상태로 되돌리지 않는다.
            case = case_repository.get_case_by_mr(mr_name) or {"mr_name": mr_name}
            if docstatus == 2 and case.get("case_id") and case.get("status") not in {
                "CANCELLED", "REJECTED", "COMPLETED"
            }:
                cancel_reason = "ERPNext에서 Material Request가 취소되었습니다."
                case = case_repository.transition_case(
                    str(case["case_id"]),
                    status="CANCELLED",
                    stage="CANCELLED",
                    reason=cancel_reason,
                    triggered_by="erpnext_webhook",
                )
                task_repository.cancel_pending_tasks(
                    str(case["case_id"]), reason=cancel_reason
                )
    except Exception as exc:
        event_repository.fail_event(str(event["event_id"]), str(exc))
        raise
    event_repository.complete_event(str(event["event_id"]))
    return case, True


def queue_case_start(case_id: str, *, triggered_by: str) -> dict[str, Any]:
    case = case_repository.get_case(case_id)
    if case is None:
        raise LookupError(case_id)
    if case["status"] not in {"AWAITING_MR_REVIEW", "FAILED"}:
        raise ValueError(f"현재 상태에서는 시작할 수 없습니다: {case['status']}")
    retry_from_checkpoint = False
    restart_from_bidding = False
    if case["status"] == "FAILED":
        app = get_process_app()
        snapshot = app.get_state(_config(case["thread_id"] or case["mr_name"]))
        retry_from_checkpoint = bool(snapshot.values and snapshot.next)
        restart_from_bidding = bool(
            snapshot.values
            and not snapshot.next
            and snapshot.values.get("status") == "catalog_purchase_required"
        )
        if not retry_from_checkpoint and not restart_from_bidding:
            raise ValueError(
                "이 작업은 이미 종료된 체크포인트에 있어 자동 재시도할 수 없습니다. "
                "오류 내용을 확인한 뒤 반려하거나 별도 처리해 주세요."
            )

    _validate_erp_mr(
        case["mr_name"],
        allow_submitted=retry_from_checkpoint or restart_from_bidding,
    )
    workflow_snapshot = dict(case.get("workflow_snapshot") or {})
    workflow_snapshot["retry_from_checkpoint"] = retry_from_checkpoint
    workflow_snapshot["restart_from_bidding"] = restart_from_bidding
    return case_repository.transition_case(
        case_id,
        status="QUEUED",
        stage=(
            "BIDDING_DECISION"
            if restart_from_bidding
            else case.get("stage") if retry_from_checkpoint else "MR_REVIEW"
        ),
        reason=(
            "비딩 불필요 건의 직접구매 경로를 다시 판정합니다."
            if restart_from_bidding
            else "실패 지점의 체크포인트부터 구매 처리를 재개합니다."
            if retry_from_checkpoint
            else "구매 담당자가 MR 처리를 시작했습니다."
        ),
        triggered_by=triggered_by,
        workflow_snapshot=workflow_snapshot,
        expected_version=int(case["version"]),
    )


def run_queued_case(case_id: str, *, triggered_by: str) -> None:
    """Background-safe graph runner.  Failures are persisted for recovery."""

    case = case_repository.get_case(case_id)
    if case is None:
        return
    if case["status"] != "QUEUED":
        # 취소/반려된 뒤 늦게 시작된 BackgroundTask는 실행하지 않는다.
        return
    try:
        retry_from_checkpoint = bool(
            (case.get("workflow_snapshot") or {}).get("retry_from_checkpoint")
        )
        restart_from_bidding = bool(
            (case.get("workflow_snapshot") or {}).get("restart_from_bidding")
        )
        material_request = _validate_erp_mr(
            case["mr_name"],
            allow_submitted=retry_from_checkpoint or restart_from_bidding,
        )
        refreshed_case = case_repository.upsert_case_from_material_request(material_request)
        if refreshed_case:
            case = refreshed_case
        try:
            case_repository.transition_case(
                case_id,
                status="RUNNING",
                stage=(
                    "BIDDING_DECISION"
                    if restart_from_bidding
                    else case.get("stage") if retry_from_checkpoint else "ITEM_CHECK"
                ),
                reason=(
                    "최근 거래 기준으로 직접구매 경로를 다시 판정합니다."
                    if restart_from_bidding
                    else "저장된 체크포인트부터 AI 구매 워크플로를 재개합니다."
                    if retry_from_checkpoint
                    else "AI 구매 워크플로를 실행합니다."
                ),
                triggered_by=triggered_by,
                expected_version=int(case["version"]),
            )
        except case_repository.CaseConflictError:
            current = case_repository.get_case(case_id)
            if current and current["status"] in _TERMINAL_CASE_STATUSES:
                return
            raise
        app = get_process_app()
        with _GRAPH_LOCK:
            config = _config(case["thread_id"] or case["mr_name"])
            if retry_from_checkpoint:
                snapshot = app.get_state(config)
                # A node failure leaves ``next`` populated. If the graph had
                # already reached a terminal checkpoint, projection alone is
                # enough to repair the PostgreSQL read model.
                if snapshot.next:
                    app.invoke(None, config=config)
            elif restart_from_bidding:
                app.invoke(
                    {
                        "entrypoint": "bidding_recheck",
                        "mr_name": case["mr_name"],
                        "case_id": case_id,
                        "status": "checking_bidding",
                        "direct_purchase": False,
                        "direct_purchase_items": {},
                        "error": "",
                    },
                    config=config,
                )
            else:
                app.invoke(
                    {"mr_name": case["mr_name"], "case_id": case_id, "status": "started"},
                    config=config,
                )
            project_case_from_checkpoint(case_id)
    except Exception as exc:
        failure_snapshot: dict[str, Any] | None = None
        try:
            failed_state = get_process_app().get_state(
                _config(case["thread_id"] or case["mr_name"])
            )
            failure_snapshot = {
                "values": to_checkpoint_data(failed_state.values or {}),
                "next": list(failed_state.next or ()),
                "interrupts": _interrupt_payloads(failed_state),
                "can_retry": bool(failed_state.next),
            }
        except Exception:
            # The original workflow error is more important than a secondary
            # checkpoint-inspection failure.
            failure_snapshot = None
        current = case_repository.get_case(case_id)
        if current and current["status"] in _TERMINAL_CASE_STATUSES:
            return
        case_repository.transition_case(
            case_id,
            status="FAILED",
            stage="HUMAN_REVIEW",
            reason="워크플로 실행 중 오류가 발생했습니다.",
            triggered_by=triggered_by,
            workflow_snapshot=failure_snapshot,
            last_error=str(exc),
        )
        _create_notification_safely(
            case_id=case_id,
            recipient_id=case.get("assigned_user_id"),
            notification_type="WORKFLOW_FAILED",
            title="AI 구매 처리를 확인해주세요",
            message=f"{case['mr_name']} · 자동 처리 중 오류가 발생했습니다.",
            payload={"mr_name": case["mr_name"], "stage": "HUMAN_REVIEW"},
        )


def project_case_from_checkpoint(case_id: str) -> dict[str, Any]:
    case = case_repository.get_case(case_id)
    if case is None:
        raise LookupError(case_id)
    if case["status"] in _TERMINAL_CASE_STATUSES:
        # 체크포인트는 감사용으로 남겨도 PostgreSQL의 종료 상태가 우선이다.
        # 취소 전 인터럽트를 재투영해 케이스를 되살리지 않는다.
        task_repository.cancel_pending_tasks(
            case_id, reason="종료된 구매 작업의 남은 인터럽트를 정리했습니다."
        )
        return case
    thread_id = case["thread_id"] or case["mr_name"]
    app = get_process_app()
    snapshot = app.get_state(_config(thread_id))
    values = to_checkpoint_data(snapshot.values or {})
    graph_status = values.get("status")
    case_status, stage = project_graph_status(graph_status)
    projection_error = values.get("error") or None
    if graph_status == "catalog_purchase_required" and not projection_error:
        projection_error = (
            "이 작업은 직접구매 경로 연결 전 생성된 체크포인트입니다. "
            "다시 시도하면 최근 거래 협력사·확정단가를 재확인한 뒤 "
            "PO 발송 전 승인 단계로 이어집니다."
        )
    snapshot_data = {
        "values": values,
        "next": list(snapshot.next or ()),
        "interrupts": _interrupt_payloads(snapshot),
        "can_retry": bool(snapshot.next) or graph_status == "catalog_purchase_required",
    }
    updated = case_repository.transition_case(
        case_id,
        status=case_status,
        stage=stage,
        reason=(
            values.get("cancellation_reason")
            or f"LangGraph 상태 동기화: {graph_status or 'unknown'}"
        ),
        workflow_snapshot=snapshot_data,
        last_error=projection_error,
    )

    if values.get("quotation_deadline"):
        updated = case_repository.update_quotation_deadline(
            case_id, str(values["quotation_deadline"])
        )

    if graph_status == "po_sent" and values.get("po_name"):
        from backend_logic2.services.receipt_service import ensure_delivery_for_po

        ensure_delivery_for_po(case_id, str(values["po_name"]))

    active_task_types = {
        task_presentation(payload)["task_type"]
        for payload in snapshot_data["interrupts"]
    }
    task_repository.supersede_inactive_tasks(case_id, active_task_types)
    biddingflow_tasks: list[dict[str, Any]] = []
    for payload in snapshot_data["interrupts"]:
        presentation = task_presentation(payload)
        task_repository.replace_pending_task(
            case_id=case_id,
            task_type=presentation["task_type"],
            audience=presentation["audience"],
            channel=presentation["channel"],
            title=presentation["title"],
            description=presentation["description"],
            input_schema=task_input_schema(payload),
            payload=payload,
        )
        if presentation["channel"] == "BIDDINGFLOW":
            biddingflow_tasks.append(presentation)

    # 각 그래프 실행/재개 결과로 새 구매 담당자 입력이 생겼을 때 inbox와
    # SSE에 함께 알립니다. 요청자용 ERP_NEXT 인터럽트는 BiddingFlow에서
    # 잘못 응답하지 않도록 이 알림 대상에서 제외합니다.
    if biddingflow_tasks:
        presentation = biddingflow_tasks[-1]
        _create_notification_safely(
            case_id=case_id,
            recipient_id=updated.get("assigned_user_id"),
            notification_type="WORKFLOW_INPUT_REQUIRED",
            title=presentation["title"],
            message=f"{case['mr_name']} · {presentation['description']}",
            payload={"mr_name": case["mr_name"], "stage": stage},
        )
    return updated


def project_substitute_decision(
    mr_name: str,
    *,
    new_purchase: bool = False,
    selected_item_code: str | None = None,
) -> dict[str, Any] | None:
    """Project a requester decision and publish the corresponding UI event.

    Both the direct Client Script API and the local comment poller resume the
    same LangGraph interrupt. Keeping projection/notification here prevents
    one transport from updating PostgreSQL without waking the frontend.
    """

    case = case_repository.get_case_by_mr(mr_name)
    if case is None:
        return None

    projected = project_case_from_checkpoint(str(case["case_id"]))
    if new_purchase:
        _create_notification_safely(
            case_id=str(case["case_id"]),
            recipient_id=case.get("assigned_user_id"),
            notification_type="SUBSTITUTE_NEW_PURCHASE_REQUESTED",
            title="신규구매가 요청되었습니다",
            message=f"{mr_name} · 요청자가 대체품 대신 신규구매 진행을 선택했습니다.",
            payload={"mr_name": mr_name, "stage": projected.get("stage")},
        )
    elif selected_item_code:
        _create_notification_safely(
            case_id=str(case["case_id"]),
            recipient_id=case.get("assigned_user_id"),
            notification_type="SUBSTITUTE_SELECTED",
            title="대체품 사용이 확정되었습니다",
            message=(
                f"{mr_name} · 요청자가 {selected_item_code} 대체품을 선택하여 "
                "원 MR이 취소되었습니다."
            ),
            payload={
                "mr_name": mr_name,
                "item_code": selected_item_code,
                "stage": projected.get("stage"),
            },
        )
    return projected


def resume_task(
    task_id: str,
    *,
    answer: dict[str, Any],
    answered_by: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    if expected_version is None:
        raise ValueError("작업 버전이 필요합니다. 목록을 새로고침한 뒤 다시 시도해 주세요.")
    task = task_repository.get_task(task_id)
    if task is None:
        raise LookupError(task_id)
    if task["status"] != "PENDING":
        raise ValueError("이미 처리된 작업입니다.")
    case = case_repository.get_case(str(task["case_id"]))
    if case is None:
        raise LookupError(str(task["case_id"]))
    if case["status"] in _TERMINAL_CASE_STATUSES:
        raise ValueError("이미 종료된 구매 작업에는 응답할 수 없습니다.")

    expected_stage = _TASK_STAGE.get(str(task["task_type"]))
    if expected_stage and case["stage"] != expected_stage:
        raise ValueError(
            f"현재 단계({case['stage']})와 작업 종류({task['task_type']})가 일치하지 않습니다. "
            "목록을 새로고침해 주세요."
        )

    if task["task_type"] == "supplier_scorecard":
        from backend_logic2.repositories.deliveries import complete_scorecard

        required = {"leadTime", "quality", "price", "service", "communication"}
        if set(answer) != required or any(
            not isinstance(answer[key], (int, float)) or not 1 <= answer[key] <= 5
            for key in required
        ):
            raise ValueError("Scorecard 5개 항목을 각각 1~5점으로 입력해주세요.")
        claimed = task_repository.claim_task(
            task_id,
            answer=answer,
            answered_by=answered_by,
            expected_version=expected_version,
        )
        try:
            complete_scorecard(str(case["case_id"]), answer)
        except Exception:
            task_repository.release_claimed_task(
                task_id, claimed_version=int(claimed["version"])
            )
            raise
        task_repository.complete_claimed_task(
            task_id, claimed_version=int(claimed["version"])
        )
        return case_repository.transition_case(
            str(case["case_id"]),
            status="COMPLETED",
            stage="COMPLETED",
            reason="Supplier Scorecard 평가가 완료되었습니다.",
            triggered_by=answered_by,
        )

    app = get_process_app()
    with _GRAPH_LOCK:
        snapshot = app.get_state(_config(case["thread_id"] or case["mr_name"]))
        active_task_types = {
            task_presentation(payload)["task_type"]
            for payload in _interrupt_payloads(snapshot)
        }
        if task["task_type"] not in active_task_types:
            raise ValueError(
                "현재 LangGraph 인터럽트와 대기 작업이 일치하지 않습니다. "
                "서버 상태를 다시 동기화한 뒤 시도해 주세요."
            )

        claimed = task_repository.claim_task(
            task_id,
            answer=answer,
            answered_by=answered_by,
            expected_version=expected_version,
        )
        try:
            app.invoke(
                Command(resume=answer),
                config=_config(case["thread_id"] or case["mr_name"]),
            )
        except Exception:
            task_repository.release_claimed_task(
                task_id, claimed_version=int(claimed["version"])
            )
            raise
        task_repository.complete_claimed_task(
            task_id, claimed_version=int(claimed["version"])
        )
        projected = project_case_from_checkpoint(str(case["case_id"]))

        # The frontend quotation modal intentionally offers one human action:
        # "이 견적을 최종 선택".  The graph internally has two adjacent
        # interrupts (quotation finalize -> final supplier), so when the same
        # answer also contains supplier we safely consume the newly-created
        # final_selection task in the same serialized graph lock.
        supplier = answer.get("supplier")
        if task["task_type"] in {"quotation_check", "check_quotations"} and supplier:
            next_tasks = task_repository.list_tasks(
                case_id=str(case["case_id"]), audience="BUYER", status="PENDING"
            )
            final_task = next(
                (item for item in next_tasks if item["task_type"] == "final_selection"),
                None,
            )
            if final_task:
                final_claim = task_repository.claim_task(
                    str(final_task["task_id"]),
                    answer={"supplier": supplier},
                    answered_by=answered_by,
                    expected_version=int(final_task["version"]),
                )
                try:
                    app.invoke(
                        Command(resume={"supplier": supplier}),
                        config=_config(case["thread_id"] or case["mr_name"]),
                    )
                except Exception:
                    task_repository.release_claimed_task(
                        str(final_task["task_id"]),
                        claimed_version=int(final_claim["version"]),
                    )
                    raise
                task_repository.complete_claimed_task(
                    str(final_task["task_id"]),
                    claimed_version=int(final_claim["version"]),
                )
                projected = project_case_from_checkpoint(str(case["case_id"]))
        return projected


def reject_case(case_id: str, *, reason: str, rejected_by: str) -> dict[str, Any]:
    case = case_repository.get_case(case_id)
    if case is None:
        raise LookupError(case_id)
    reject_material_request(case["mr_name"], reason, reason_code="BUYER_REJECTED")
    task_repository.cancel_pending_tasks(case_id, reason=reason)
    return case_repository.transition_case(
        case_id,
        status="CANCELLED",
        stage="CANCELLED",
        reason=reason,
        triggered_by=rejected_by,
    )


def extend_quotation_deadline(
    case_id: str, *, deadline_at: str, changed_by: str
) -> dict[str, Any]:
    case = case_repository.get_case(case_id)
    if case is None:
        raise LookupError(case_id)
    if case["stage"] not in {"QUOTATION_COLLECTION", "SUPPLIER_SELECTION"}:
        raise ValueError("RFQ 발송 후 견적 수집 단계에서만 마감일을 연장할 수 있습니다.")
    parsed = datetime.fromisoformat(deadline_at)
    if parsed.tzinfo is None:
        raise ValueError("견적 마감일에는 시간대가 포함되어야 합니다.")
    if parsed <= datetime.now(timezone.utc):
        raise ValueError("견적 마감일은 현재 시각 이후여야 합니다.")
    current_deadline = case.get("quotation_deadline_at")
    if current_deadline and parsed <= current_deadline:
        raise ValueError("새 견적 마감일은 기존 마감일보다 늦어야 합니다.")
    updated = case_repository.update_quotation_deadline(case_id, deadline_at)
    from backend_logic2.repositories.notifications import create_notification

    create_notification(
        case_id=case_id,
        recipient_id=case.get("assigned_user_id"),
        notification_type="QUOTATION_DEADLINE_EXTENDED",
        title="견적 마감일이 연장되었습니다",
        message=f"{case['mr_name']} · 새 마감일 {deadline_at} · 이메일 재발송 없음",
        payload={"mr_name": case["mr_name"], "deadline_at": deadline_at, "changed_by": changed_by},
    )
    return updated
