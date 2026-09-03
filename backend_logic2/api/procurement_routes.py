"""Authenticated procurement API plus secret-authenticated ERP webhooks."""

from __future__ import annotations

import os
import asyncio
import json
from contextlib import suppress
from datetime import datetime
from typing import Any, Optional

import psycopg
from fastapi import APIRouter, BackgroundTasks, Body, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auth_service.dependencies import CurrentUser
from backend_logic2.integrations.erp_client import ERPNextAPIError
from backend_logic2.repositories import cases as case_repository
from backend_logic2.repositories import tasks as task_repository
from backend_logic2.repositories import deliveries as delivery_repository
from backend_logic2.repositories import notifications as notification_repository
from backend_logic2.services import workflow_service
from backend_logic2.services import receipt_service
from backend_logic2.services import item_service
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
    if value != expected:
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


@webhook_router.post("/item")
def item_webhook(
    payload: dict[str, Any] = Body(...),
    x_erpnext_webhook_secret: Optional[str] = Header(default=None),
    x_erpnext_event_id: Optional[str] = Header(default=None),
):
    """Validate newly-created disabled Items without a CLI/manual trigger."""
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
