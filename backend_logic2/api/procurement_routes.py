"""Authenticated procurement API plus secret-authenticated ERP webhooks."""

from __future__ import annotations

import os
import asyncio
import json
import secrets
from contextlib import suppress
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote

import psycopg
from fastapi import APIRouter, BackgroundTasks, Body, Header, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from auth_service.dependencies import CurrentUser
from backend_logic2.integrations.erp_client import ERPNextAPIError, erp_download_file
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
    return str(current_user.get("erp_user_id") or current_user.get("id") or "unknown")


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
    include_closed: bool = False,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    try:
        rows = case_repository.list_cases(
            status=case_status,
            stage=stage,
            include_closed=include_closed,
            limit=limit,
            offset=offset,
        )
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="구매 작업 저장소에 연결할 수 없습니다.") from exc
    return {"items": rows, "count": len(rows), "limit": limit, "offset": offset}


@router.get("/cases/{case_id}")
def get_case(case_id: str, current_user: CurrentUser):
    row = case_repository.get_case(case_id)
    if row is None:
        raise HTTPException(status_code=404, detail="구매 작업을 찾을 수 없습니다.")
    row["tasks"] = task_repository.list_tasks(case_id=case_id)
    row["delivery"] = delivery_repository.get_delivery_by_case(case_id)
    return row


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
def answer_task(task_id: str, body: ResumeTaskRequest, current_user: CurrentUser):
    try:
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
