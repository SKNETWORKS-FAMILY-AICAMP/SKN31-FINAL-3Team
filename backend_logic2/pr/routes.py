"""Public supplier pages and authenticated BiddingFlow PR read API."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Query, Request
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
    # The page sends application/x-www-form-urlencoded. Parsing it directly
    # avoids adding the optional python-multipart package for a two-field form.
    fields = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    decision = (fields.get("decision") or [""])[0]
    reason = (fields.get("reason") or [None])[0]
    try:
        pr = service.respond(token, decision, reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        return HTMLResponse(render_result("처리할 수 없음", str(exc)), status_code=409)
    from backend_logic2.services.workflow_service import resume_supplier_pr_response

    try:
        projected = resume_supplier_pr_response(
            case_id=str(pr["case_id"]),
            pr_id=str(pr["pr_id"]),
            decision=decision,
            reason=reason,
        )
    except (ValueError, LookupError) as exc:
        repository.record_processing_error(
            str(pr["pr_id"]), stage="validation", error=str(exc)
        )
        return HTMLResponse(
            render_result(
                "응답 확인 필요",
                "응답은 접수되었지만 현재 구매 상태와 일치하지 않아 담당자가 확인할 예정입니다.",
            ),
            status_code=409,
        )
    except RuntimeError:
        # The workflow service records whether invoke or projection failed.
        return HTMLResponse(
            render_result(
                "응답 접수 완료",
                "응답이 접수되었습니다. 처리 결과는 담당자가 확인 후 반영합니다.",
            ),
            status_code=202,
        )

    if pr["status"] == "REJECTED":
        return HTMLResponse(render_result("수주 거절 완료", "거절 사유가 BiddingFlow 담당자에게 전달되었습니다."))
    po_name = (
        projected.get("workflow_snapshot", {}).get("values", {}).get("po_name")
    )
    if po_name:
        return HTMLResponse(render_result("수주 접수 완료", f"응답이 반영되었으며 발주서 {po_name}가 생성되었습니다."))
    return HTMLResponse(render_result("수주 접수 완료", "응답은 반영되었으나 PO 생성 확인이 필요합니다."), status_code=202)


@internal_router.get("")
def get_pr_requests(case_id: str | None = None):
    items = repository.list_requests(case_id=case_id)
    return {"items": items, "count": len(items)}
