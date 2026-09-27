"""Public supplier pages and authenticated BiddingFlow PR read API."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from . import repository, service
from .email_template import render_response_form, render_result


public_router = APIRouter(prefix="/api/public/pr", tags=["Supplier PR Response"])
internal_router = APIRouter(prefix="/api/procurement/pr", tags=["Supplier PR Management"])


@public_router.get("/respond/{token}", response_class=HTMLResponse)
def response_form(token: str, decision: str = Query(..., pattern="^(accept|reject)$")):
    pr = service.inspect_token(token)
    if not pr or pr["status"] != "SENT":
        return HTMLResponse(render_result("처리할 수 없음", "이미 응답했거나 유효하지 않은 요청입니다."), status_code=409)
    expires_at = pr.get("expires_at")
    if expires_at and expires_at <= datetime.now(timezone.utc):
        return HTMLResponse(render_result("응답 기한 만료", "이 PR의 응답 기한이 지났습니다."), status_code=410)
    return HTMLResponse(render_response_form(token, decision, str(pr["supplier_id"]), str(pr["pr_id"])))


@public_router.post("/respond/{token}", response_class=HTMLResponse)
async def submit_response(token: str, request: Request):
    # ⚠️ 이 라우트는 async다. 여기서 동기 함수를 그대로 부르면 스레드 하나가
    # 아니라 이벤트 루프 전체가 멈춘다 - 로그인·목록 조회까지 서버 전체가
    # 먹통이 된다. 예전엔 여기서 그래프 잠금을 잡는 응답 반영을 직접 불러서,
    # 자동 진행이 그래프를 몇 분 돌리는 동안 협력사가 버튼을 누르면 서버가
    # 통째로 멈출 수 있었다. DB 작업은 스레드로 넘기고, 그래프 실행은 전용
    # 스레드에 예약만 한다.
    # The page sends application/x-www-form-urlencoded. Parsing it directly
    # avoids adding the optional python-multipart package for a two-field form.
    fields = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    decision = (fields.get("decision") or [""])[0]
    reason = (fields.get("reason") or [None])[0]
    try:
        pr = await run_in_threadpool(service.respond, token, decision, reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        return HTMLResponse(render_result("처리할 수 없음", str(exc)), status_code=409)
    from backend_logic2.services.workflow_service import (
        check_supplier_pr_response_ready,
        submit_graph_work,
    )

    case_id = str(pr["case_id"])
    pr_id = str(pr["pr_id"])
    try:
        await run_in_threadpool(
            check_supplier_pr_response_ready, case_id=case_id, pr_id=pr_id
        )
    except (ValueError, LookupError) as exc:
        await run_in_threadpool(
            repository.record_processing_error, pr_id, stage="validation", error=str(exc)
        )
        return HTMLResponse(
            render_result(
                "응답 확인 필요",
                "응답은 접수되었지만 현재 구매 상태와 일치하지 않아 담당자가 확인할 예정입니다.",
            ),
            status_code=409,
        )

    submit_graph_work(
        _apply_supplier_response,
        case_id=case_id,
        pr_id=pr_id,
        decision=decision,
        reason=reason,
    )
    if pr["status"] == "REJECTED":
        return HTMLResponse(render_result("수주 거절 완료", "거절 사유가 BiddingFlow 담당자에게 전달되었습니다."))
    return HTMLResponse(
        render_result(
            "수주 접수 완료",
            "응답이 전달되었습니다. 담당자 승인 후 발주서가 발행됩니다.",
        )
    )


def _apply_supplier_response(
    *, case_id: str, pr_id: str, decision: str, reason: str | None
) -> None:
    """그래프 전용 스레드에서 협력사 응답을 반영한다. 실패하면 담당자가 볼 수 있게 남긴다."""
    from backend_logic2.services.workflow_service import resume_supplier_pr_response

    try:
        resume_supplier_pr_response(
            case_id=case_id, pr_id=pr_id, decision=decision, reason=reason
        )
    except Exception as exc:  # noqa: BLE001 - 응답 자체는 이미 접수됐다
        repository.record_processing_error(pr_id, stage="background", error=str(exc))
        try:
            from backend_logic2.nodes.supplier.tools.case_logging import log_ai_decision

            log_ai_decision(
                case_id,
                "supplier_pr_response",
                f"협력사 수주 응답({decision})을 반영하지 못했습니다: {exc}",
            )
        except Exception:  # noqa: BLE001
            pass


@internal_router.get("")
def get_pr_requests(case_id: str | None = None):
    items = repository.list_requests(case_id=case_id)
    return {"items": items, "count": len(items)}
