"""Authenticated procurement API plus secret-authenticated ERP webhooks."""

from __future__ import annotations

import os
import asyncio
import json
import secrets
from contextlib import suppress
from datetime import datetime
from typing import Any, Literal, Optional
from urllib.parse import quote

import psycopg
from fastapi import APIRouter, BackgroundTasks, Body, Header, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from auth_service.dependencies import CurrentUser
from backend_logic2.integrations.erp_client import ERPNextAPIError, erp_download_file, erp_get
from backend_logic2.repositories import cases as case_repository
from backend_logic2.repositories import tasks as task_repository
from backend_logic2.repositories import deliveries as delivery_repository
from backend_logic2.repositories import notifications as notification_repository
from backend_logic2.services import workflow_service
from backend_logic2.services import receipt_service
from backend_logic2.services import item_service
from backend_logic2.nodes.item.item_spec_validation import (
    ItemSpecificationPolicyError,
    get_or_create_group_requirements,
)
from backend_logic2.services import quotation_service
from procurement_db.config import require_database_url
from backend_logic2.integrations.assignment_config import (
    is_super_admin,
    is_same_user,
)


router = APIRouter(prefix="/api/procurement", tags=["Procurement Workflow"])
webhook_router = APIRouter(prefix="/api/webhooks/erpnext", tags=["ERPNext Webhooks"])


class RejectCaseRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class ResumeTaskRequest(BaseModel):
    answer: dict[str, Any]
    version: int | None = None


class ExtendQuotationDeadlineRequest(BaseModel):
    deadline_at: datetime


def _user_id(current_user: dict[str, Any]) -> str:
    return str(
        current_user.get("erp_user_id")
        or current_user.get("email")
        or current_user.get("id")
        or "unknown"
    ).strip()

def _can_access_case(
    case: dict[str, Any],
    current_user: dict[str, Any],
) -> bool:
    actor = _user_id(current_user)

    # 관리자
    if is_super_admin(actor):
        return True

    # 해당 Case 담당자
    return is_same_user(
        case.get("assigned_user_id"),
        actor,
    )


def _require_case_access(
    case_id: str,
    current_user: dict[str, Any],
) -> dict[str, Any]:
    case = case_repository.get_case(case_id)

    if case is None:
        raise HTTPException(
            status_code=404,
            detail="구매 작업을 찾을 수 없습니다.",
        )

    if not _can_access_case(case, current_user):
        raise HTTPException(
            status_code=403,
            detail="이 구매 요청의 담당자가 아닙니다.",
        )

    return case

@router.post("/cases/sync-drafts")
def sync_draft_cases(
    current_user: CurrentUser,
    reconcile_missing: bool = Query(default=True),
):
    try:
        rows = workflow_service.sync_draft_material_requests(
            reconcile_existing=reconcile_missing
        )
        purchase_documents = receipt_service.reconcile_purchase_documents()
    except (ERPNextAPIError, psycopg.Error) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "items": rows,
        "count": len(rows),
        "purchase_documents": purchase_documents,
        "requested_by": _user_id(current_user),
    }


@router.post("/cases/reconcile-purchase-documents")
def reconcile_purchase_documents(
    current_user: CurrentUser,
    po_name: str | None = Query(default=None),
):
    """Recover ERP receipt/invoice/payment changes missed while offline."""

    try:
        counts = receipt_service.reconcile_purchase_documents(po_name=po_name)
    except (ERPNextAPIError, psycopg.Error) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"items": counts, "requested_by": _user_id(current_user)}


@router.get("/cases")
def get_cases(
    current_user: CurrentUser,
    case_status: str | None = Query(default=None, alias="status"),
    stage: str | None = None,

    # 기본값 True:
    # 로그인하면 자기 담당 MR만 반환
    assigned_to_me: bool = Query(default=True),

    include_closed: bool = False,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    actor = _user_id(current_user)

    # 관리자 또는 "내 담당" 필터 해제 시 전체 조회
    if is_super_admin(actor) or not assigned_to_me:
        assigned_user_id = None
    else:
        assigned_user_id = actor

    try:
        rows = case_repository.list_cases(
            status=case_status,
            stage=stage,
            assigned_user_id=assigned_user_id,
            include_closed=include_closed,
            limit=limit,
            offset=offset,
        )

    except psycopg.Error as exc:
        raise HTTPException(
            status_code=503,
            detail="구매 작업 저장소에 연결할 수 없습니다.",
        ) from exc

    from backend_logic2.services.scorecard_service import automatic_scores
    from backend_logic2.services.supplier_recommendations import attach_supplier_recommendations
    attach_supplier_recommendations(rows)
    for row in rows:
        if row.get("delivery"):
            row["delivery"]["automatic_scorecard"] = automatic_scores(row, row["delivery"])
    return {
        "items": rows,
        "count": len(rows),
        "limit": limit,
        "offset": offset,
        "assigned_to_me": assigned_to_me,
    }

@router.get("/cases/{case_id}")
def get_case(case_id: str, current_user: CurrentUser):
    row = case_repository.get_case(case_id)

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="구매 작업을 찾을 수 없습니다.",
        )

    row["tasks"] = task_repository.list_tasks(case_id=case_id)
    row["delivery"] = delivery_repository.get_delivery_by_case(case_id)
    from backend_logic2.services.supplier_recommendations import attach_supplier_recommendations
    attach_supplier_recommendations([row])
    if row["delivery"]:
        from backend_logic2.services.scorecard_service import automatic_scores
        row["delivery"]["automatic_scorecard"] = automatic_scores(row, row["delivery"])

    return row


@router.get("/cases/{case_id}/rfq-rounds/{rfq_name}/quotations")
def get_rfq_round_quotations(case_id: str, rfq_name: str, current_user: CurrentUser):
    """지난 재비딩 라운드(또는 현재 라운드)의 RFQ 하나에 대해 실제로 받은
    Supplier Quotation을 다시 조회한다.

    ⚠️ 재비딩할 때 지난 RFQ를 더 이상 취소·폐기하지 않고 그대로 두기
    때문에(rfq_rounds 이력), 협력사 선정 화면의 "차수" 배지를 눌렀을 때
    이 엔드포인트로 그 라운드의 RFQ 이름을 넘겨 단가·납기일 등 견적
    내용을 그때그때 ERPNext에서 직접 다시 읽어온다 - quotation_snapshot
    컬럼은 항상 "현재 진행 중인 라운드"만 담고 있어서 지난 라운드 조회에는
    쓸 수 없다."""
    case = _require_case_access(case_id, current_user)

    values = (case.get("workflow_snapshot") or {}).get("values") or {}
    known_rfqs = {str(values.get("rfq_name") or "").strip()}
    for round_entry in values.get("rfq_rounds") or []:
        if isinstance(round_entry, dict):
            known_rfqs.add(str(round_entry.get("rfq_name") or "").strip())
    known_rfqs.discard("")
    if rfq_name not in known_rfqs:
        raise HTTPException(status_code=404, detail="이 구매 작업에 속한 RFQ가 아닙니다.")

    try:
        return quotation_service.build_quotation_snapshot(case, rfq_name)
    except ERPNextAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/attachments/download")
def download_material_request_attachment(
    current_user: CurrentUser,
    file_id: str = Query(..., min_length=1),
):
    """JWT 인증 사용자를 대신해 ERPNext MR 첨부파일을 내려준다.

    ERPNext API 자격증명은 서버에만 두며, 다른 DocType에 붙은 파일을 File ID만
    추측해 다운로드하지 못하도록 Material Request 첨부 여부를 재검증합니다.
    """
    del current_user  # FastAPI 의존성 검증 자체가 이 엔드포인트의 접근 제어입니다.
    try:
        downloaded = erp_download_file(
            file_id,
            expected_attached_to_doctype="Material Request",
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ERPNextAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    document = downloaded["document"]
    filename = str(document.get("file_name") or file_id)
    return Response(
        content=downloaded["content"],
        media_type=downloaded["content_type"],
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/cases/{case_id}/start", status_code=status.HTTP_202_ACCEPTED)
def start_case(case_id: str, background_tasks: BackgroundTasks, current_user: CurrentUser):
    
    _require_case_access(case_id, current_user)
    actor = _user_id(current_user)
    
    try:
        queued = workflow_service.queue_case_start(case_id, triggered_by=actor)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="구매 작업을 찾을 수 없습니다.") from exc
    except (ValueError, case_repository.CaseConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ERPNextAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    background_tasks.add_task(workflow_service.run_queued_case, case_id, triggered_by=actor)
    return {"accepted": True, "case": queued}


@router.post("/cases/{case_id}/reject")
def reject_case(case_id: str, body: RejectCaseRequest, current_user: CurrentUser):
    
    _require_case_access(case_id, current_user)

    try:
        return workflow_service.reject_case(
            case_id,
            reason=body.reason.strip(),
            rejected_by=_user_id(current_user),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="구매 작업을 찾을 수 없습니다.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ERPNextAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/cases/{case_id}/quotation-deadline")
def extend_quotation_deadline(
    case_id: str,
    body: ExtendQuotationDeadlineRequest,
    current_user: CurrentUser,
):
    _require_case_access(case_id, current_user)

    try:
        return workflow_service.extend_quotation_deadline(
            case_id,
            deadline_at=body.deadline_at.isoformat(),
            changed_by=_user_id(current_user),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="구매 작업을 찾을 수 없습니다.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/tasks")
def get_tasks(
    current_user: CurrentUser,
    case_id: str | None = None,
    task_status: str = Query(default="PENDING", alias="status"),
):
    rows = task_repository.list_tasks(case_id=case_id, audience="BUYER", status=task_status)
    return {"items": rows, "count": len(rows)}


@router.post("/tasks/{task_id}/answer")
def answer_task(
    task_id: str,
    body: ResumeTaskRequest,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
    response: Response,
):
    task = task_repository.get_task(task_id)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="대기 작업을 찾을 수 없습니다.",
        )

    _require_case_access(
        str(task["case_id"]),
        current_user,
    )

    try:
        # Qwen serverless cold starts can exceed nginx's ordinary request
        # timeout. Keep the browser request short and let the existing case
        # polling surface the completed checkpoint instead of losing the
        # response at the proxy after the GPU work has already started.
        if (
            task["task_type"] in {"quotation_check", "check_quotations"}
            and str(body.answer.get("decision") or "").strip() == "check"
        ):
            queued = workflow_service.queue_quotation_analysis(
                task_id,
                answer=body.answer,
                answered_by=_user_id(current_user),
                expected_version=body.version,
                background_tasks=background_tasks,
            )
            response.status_code = status.HTTP_202_ACCEPTED
            return queued
        return workflow_service.resume_task(
            task_id,
            answer=body.answer,
            answered_by=_user_id(current_user),
            expected_version=body.version,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="대기 작업을 찾을 수 없습니다.") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=503,
            detail="구매 작업 상태를 PostgreSQL에 저장하지 못했습니다.",
        ) from exc


@router.get("/notifications")
def get_notifications(
    current_user: CurrentUser,
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
):
    rows = notification_repository.list_notifications(
        _user_id(current_user), unread_only=unread_only, limit=limit
    )
    return {"items": rows, "count": len(rows)}


@router.post("/notifications/{notification_id}/read")
def read_notification(notification_id: str, current_user: CurrentUser):
    if not notification_repository.mark_notification_read(notification_id, _user_id(current_user)):
        raise HTTPException(status_code=404, detail="알림을 찾을 수 없습니다.")
    return {"success": True}


@router.delete("/notifications/{notification_id}")
def delete_notification(notification_id: str, current_user: CurrentUser):
    if not notification_repository.delete_notification(notification_id, _user_id(current_user)):
        raise HTTPException(status_code=404, detail="알림을 찾을 수 없습니다.")
    return {"success": True}


@router.delete("/notifications")
def delete_all_notifications(current_user: CurrentUser):
    deleted_count = notification_repository.delete_all_notifications(_user_id(current_user))
    return {"success": True, "deleted_count": deleted_count}


@router.get("/item-groups/{item_group}/required-specs")
def get_item_group_required_specs_for_frontend(
    item_group: str,
    current_user: CurrentUser,
):
    """프론트엔드 규격 모달이 '요청 규격' 항목 중 실제 필수 항목에만
    빨간색 필수 태그를 붙이기 위해 호출하는 인증 전용 엔드포인트.

    ERPNext Item 폼의 Client Script가 쓰는 웹훅 엔드포인트와
    동일한 get_or_create_group_requirements를 재사용하므로, 두 화면이 같은
    필수 규격 목록을 기준으로 동작한다.
    """
    del current_user  # 인증만 필요, 값 자체는 응답에 사용하지 않는다.
    try:
        requirements = get_or_create_group_requirements(item_group)
    except ItemSpecificationPolicyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "item_group": requirements["item_group"],
        "required_specs": requirements["required_specs"],
    }


class SupplierEvaluationRequest(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=100)


@router.post("/suppliers/evaluations")
def get_supplier_evaluations(request: SupplierEvaluationRequest, current_user: CurrentUser):
    from backend_logic2.services.supplier_recommendations import get_supplier_recommendations
    return {"items": get_supplier_recommendations(request.names)}


@router.get("/suppliers/search")
def search_suppliers_for_frontend(
    current_user: CurrentUser,
    q: str = Query(default="", max_length=200),
    field: Literal["name", "email", "all"] = Query(default="all"),
):
    """협력사명 또는 이메일로 기존 Supplier를 검색한다.

    field="name"이면 협력사명(supplier_name)만, field="email"이면
    이메일(email_id)만 대조한다. 프런트엔드의 협력사명 입력란과 이메일
    입력란이 각자의 드롭다운에 서로 다른 후보를 보여줘야 해서(이름란에
    이메일 검색 결과가 섞여 나오면 안 됨) 필드별로 분리했다. field를
    안 넘기거나 "all"이면 기존처럼 이름+이메일을 합쳐서 반환한다(하위
    호환용).
    """

    del current_user

    query = q.strip()

    if not query:
        return {"items": []}

    fields = [
        "name",
        "supplier_name",
        "email_id",
        "mobile_no",
    ]

    try:
        name_rows: list[dict] = []
        email_rows: list[dict] = []

        if field in ("name", "all"):
            # 협력사명 substring 검색
            name_rows = erp_get(
                "Supplier",
                filters=[
                    ["supplier_name", "like", f"%{query}%"],
                ],
                fields=fields,
                order_by="supplier_name asc",
                limit=10,
            ) or []

        if field in ("email", "all"):
            # 이메일 substring 검색
            email_rows = erp_get(
                "Supplier",
                filters=[
                    ["email_id", "like", f"%{query}%"],
                ],
                fields=fields,
                order_by="supplier_name asc",
                limit=10,
            ) or []

        # 이름/이메일 검색 결과 합치기 + 중복 제거 (field="all"일 때만
        # 둘 다 채워지므로, name/email 단독 조회일 때는 그대로 통과됨)
        merged = {}

        for row in [*name_rows, *email_rows]:
            supplier_id = row.get("name")

            if supplier_id:
                merged[supplier_id] = row

        rows = list(merged.values())[:10]

    except ERPNextAPIError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"ERPNext Supplier 검색 실패: {exc}",
        ) from exc

    from backend_logic2.services.supplier_recommendations import get_supplier_recommendations
    recommendations = get_supplier_recommendations([row["name"] for row in rows])
    return {
        "items": [
            {
                "recommendation": recommendations.get(row.get("name")),
                "name": row.get("name"),
                "supplier_name": row.get("supplier_name") or row.get("name"),
                "email": row.get("email_id"),
                "phone": row.get("mobile_no"),
            }
            for row in rows
        ]
    }

@router.get("/events")
async def stream_procurement_events(current_user: CurrentUser):
    """Authenticated SSE bridge backed by PostgreSQL LISTEN/NOTIFY."""

    recipient_id = _user_id(current_user)

    async def event_stream():
        connection = await psycopg.AsyncConnection.connect(
            require_database_url(), autocommit=True, connect_timeout=10
        )
        notifications = None
        pending_notice: asyncio.Task | None = None
        try:
            await connection.execute("LISTEN biddingflow_notifications")
            notifications = connection.notifies()
            yield "retry: 3000\n\n"
            while True:
                # Shield the pending LISTEN read from the heartbeat timeout.
                # Cancelling anext() closes psycopg's async iterator, which made
                # the next loop raise StopAsyncIteration inside StreamingResponse.
                if pending_notice is None:
                    pending_notice = asyncio.create_task(anext(notifications))
                try:
                    notice = await asyncio.wait_for(
                        asyncio.shield(pending_notice), timeout=20
                    )
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                except StopAsyncIteration:
                    # A closed DB connection ends the SSE response normally;
                    # the browser reconnects using the retry directive above.
                    return
                pending_notice = None
                try:
                    payload = json.loads(notice.payload)
                except (TypeError, json.JSONDecodeError):
                    continue
                target = payload.get("recipient_id")
                if target and target != recipient_id:
                    continue
                yield "event: notification\n" + (
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
        except asyncio.CancelledError:
            # Uvicorn 종료나 브라우저 이탈로 장기 SSE가 끊기는 것은 정상이다.
            # 취소 예외를 ASGI 오류로 전파하지 않고 아래 정리 단계로 이동한다.
            return
        finally:
            if pending_notice is not None and not pending_notice.done():
                pending_notice.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await asyncio.wait_for(pending_notice, timeout=1)
            if notifications is not None:
                with suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(notifications.aclose(), timeout=1)
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(connection.close(), timeout=1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _require_webhook_secret(value: Optional[str]) -> None:
    expected = os.environ.get("ERPNEXT_WEBHOOK_SECRET", "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="ERPNEXT_WEBHOOK_SECRET이 설정되지 않았습니다.")
    if not value or not secrets.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="웹훅 인증에 실패했습니다.")


@webhook_router.post("/material-request")
def material_request_webhook(
    payload: dict[str, Any] = Body(...),
    x_erpnext_webhook_secret: Optional[str] = Header(default=None),
    x_erpnext_event_id: Optional[str] = Header(default=None),
):
    _require_webhook_secret(x_erpnext_webhook_secret)
    try:
        case, created = workflow_service.register_material_request_event(
            payload,
            event_id=x_erpnext_event_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ERPNextAPIError, psycopg.Error) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"accepted": True, "duplicate": not created, "case": case}


@webhook_router.post("/material-request-file")
def material_request_file_webhook(
    payload: dict[str, Any] = Body(...),
    x_erpnext_webhook_secret: Optional[str] = Header(default=None),
    x_erpnext_event_id: Optional[str] = Header(default=None),
):
    """Refresh an MR projection after an attached ERPNext File changes."""

    _require_webhook_secret(x_erpnext_webhook_secret)
    try:
        case, created = workflow_service.register_material_request_attachment_event(
            payload,
            event_id=x_erpnext_event_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ERPNextAPIError, psycopg.Error) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"accepted": True, "duplicate": not created, "case": case}


@webhook_router.post("/quotation-email")
def quotation_email_webhook(
    background_tasks: BackgroundTasks,
    payload: dict[str, Any] = Body(...),
    x_erpnext_webhook_secret: Optional[str] = Header(default=None),
    x_erpnext_event_id: Optional[str] = Header(default=None),
):
    """Queue an inbound RFQ Communication for local quotation extraction."""
    _require_webhook_secret(x_erpnext_webhook_secret)
    background_tasks.add_task(
        quotation_service.register_quotation_email_event,
        payload,
        event_id=x_erpnext_event_id,
    )
    return {"accepted": True, "queued": True}


@webhook_router.post("/quotation-email-file")
def quotation_email_file_webhook(
    background_tasks: BackgroundTasks,
    payload: dict[str, Any] = Body(...),
    x_erpnext_webhook_secret: Optional[str] = Header(default=None),
    x_erpnext_event_id: Optional[str] = Header(default=None),
):
    """Queue a File attached to an RFQ reply after ERPNext finishes saving it."""
    _require_webhook_secret(x_erpnext_webhook_secret)
    background_tasks.add_task(
        quotation_service.register_quotation_email_event,
        payload,
        event_id=x_erpnext_event_id,
    )
    return {"accepted": True, "queued": True}


@webhook_router.post("/purchase-receipt")
def purchase_receipt_webhook(
    payload: dict[str, Any] = Body(...),
    x_erpnext_webhook_secret: Optional[str] = Header(default=None),
    x_erpnext_event_id: Optional[str] = Header(default=None),
):
    _require_webhook_secret(x_erpnext_webhook_secret)
    try:
        projections, created = receipt_service.register_purchase_receipt_event(
            payload,
            event_id=x_erpnext_event_id,
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ERPNextAPIError, psycopg.Error) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"accepted": True, "duplicate": not created, "items": projections}


@webhook_router.post("/supplier-quotation")
def supplier_quotation_webhook(
    payload: dict[str, Any] = Body(...),
    x_erpnext_webhook_secret: Optional[str] = Header(default=None),
    x_erpnext_event_id: Optional[str] = Header(default=None),
):
    """Refresh quote response rate and wake the frontend without auto-finalizing."""

    _require_webhook_secret(x_erpnext_webhook_secret)
    try:
        projections, created = quotation_service.register_supplier_quotation_event(
            payload,
            event_id=x_erpnext_event_id,
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ERPNextAPIError, psycopg.Error) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"accepted": True, "duplicate": not created, "items": projections}


@webhook_router.post("/purchase-order")
def purchase_order_webhook(
    payload: dict[str, Any] = Body(...),
    x_erpnext_webhook_secret: Optional[str] = Header(default=None),
    x_erpnext_event_id: Optional[str] = Header(default=None),
):
    _require_webhook_secret(x_erpnext_webhook_secret)
    try:
        projection, created = receipt_service.register_purchase_order_event(
            payload, event_id=x_erpnext_event_id
        )
        return {"accepted": True, "created": created, "projection": projection}
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ERPNextAPIError, psycopg.Error) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@webhook_router.post("/purchase-invoice")
def purchase_invoice_webhook(
    payload: dict[str, Any] = Body(...),
    x_erpnext_webhook_secret: Optional[str] = Header(default=None),
    x_erpnext_event_id: Optional[str] = Header(default=None),
):
    _require_webhook_secret(x_erpnext_webhook_secret)
    try:
        projections, created = receipt_service.register_purchase_invoice_event(
            payload, event_id=x_erpnext_event_id
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ERPNextAPIError, psycopg.Error) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"accepted": True, "duplicate": not created, "items": projections}


@webhook_router.post("/payment-entry")
def payment_entry_webhook(
    payload: dict[str, Any] = Body(...),
    x_erpnext_webhook_secret: Optional[str] = Header(default=None),
    x_erpnext_event_id: Optional[str] = Header(default=None),
):
    _require_webhook_secret(x_erpnext_webhook_secret)
    try:
        projections, created = receipt_service.register_payment_entry_event(
            payload, event_id=x_erpnext_event_id
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ERPNextAPIError, psycopg.Error) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"accepted": True, "duplicate": not created, "items": projections}


@webhook_router.get("/item-groups/{item_group}/required-specs")
def get_item_group_required_specs(
    item_group: str,
    x_erpnext_webhook_secret: Optional[str] = Header(default=None),
):
    """ERPNext Item 폼(Client Script)이 item_group 선택 시 description
    placeholder를 채우기 위해 호출하는 조회 전용 엔드포인트.

    get_or_create_group_requirements는 처음 보는 item_group이라도 AI로
    즉시 필수 규격을 정의해 DB에 저장하고 반환한다(자가치유) - 그래서
    "한 번 실패해야 AI가 규격을 정의한다"는 별도 단계 없이, 이 호출
    한 번으로 바로 최신 필수 규격을 돌려줄 수 있다.
    """
    _require_webhook_secret(x_erpnext_webhook_secret)
    try:
        requirements = get_or_create_group_requirements(item_group)
    except ItemSpecificationPolicyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "item_group": requirements["item_group"],
        "required_specs": requirements["required_specs"],
        "reason": requirements.get("reason"),
    }


@webhook_router.post("/item")
def item_webhook(
    payload: dict[str, Any] = Body(...),
    x_erpnext_webhook_secret: Optional[str] = Header(default=None),
    x_erpnext_event_id: Optional[str] = Header(default=None),
):
    """Validate a newly-created or specification-updated disabled Item."""
    _require_webhook_secret(x_erpnext_webhook_secret)
    try:
        result, created = item_service.register_item_event(
            payload,
            event_id=x_erpnext_event_id,
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ERPNextAPIError, psycopg.Error) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"accepted": True, "duplicate": not created, "result": result}
