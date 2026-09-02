"""Reject a Material Request and close the ERPNext document safely."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from backend_logic2.integrations.erp_client import (
    ERPNextAPIError,
    erp_add_comment,
    erp_cancel,
    erp_discard_draft,
    erp_get_one,
)


router = APIRouter(
    prefix="/purchase/material-requests",
    tags=["Material Request"],
)


class MaterialRequestNotFoundError(Exception):
    """Raised when the requested Material Request does not exist."""


class MaterialRequestNotDraftError(Exception):
    """Raised when a rejection comment targets a non-Draft document."""


class RejectionCommentRequest(BaseModel):
    reason: str = Field(..., max_length=2000)


REJECTION_COMMENT_TEMPLATE = "[AI Procurement][{reason_code}] {reason}"


def add_rejection_comment(mr_name: str, reason: str) -> dict:
    """Add a rejection comment to a Draft MR without updating the MR itself.

    This function never submits, cancels, discards, or updates the Material
    Request. Its only write is an ERPNext Comment linked to the document.
    """
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError("반려 사유를 입력해주세요.")

    material_request = erp_get_one("Material Request", mr_name)
    if material_request is None:
        raise MaterialRequestNotFoundError(mr_name)

    if (
        material_request.get("docstatus") != 0
        or material_request.get("status") != "Draft"
    ):
        raise MaterialRequestNotDraftError(mr_name)

    return erp_add_comment("Material Request", mr_name, clean_reason)


def reject_material_request(
    mr_name: str,
    reason: str,
    *,
    reason_code: str = "BUYER_REJECTED",
) -> dict:
    """Comment and close an MR without leaving it in Pending/Draft.

    ERPNext cannot *Cancel* a Draft transaction directly. Drafts therefore use
    the standard Discard operation, while already submitted MRs use Cancel.
    Both operations are exposed to BiddingFlow as ``CANCELLED`` and the
    PostgreSQL workflow history retains the reason after a discarded ERP draft
    disappears from the active MR list.
    """
    clean_reason = reason.strip()
    if not clean_reason:
        raise ValueError("반려 사유를 입력해주세요.")

    material_request = erp_get_one("Material Request", mr_name)
    if material_request is None:
        raise MaterialRequestNotFoundError(mr_name)

    docstatus = int(material_request.get("docstatus") or 0)
    if docstatus == 2:
        return {"name": mr_name, "status": "CANCELLED", "action": "already_cancelled"}
    if docstatus not in {0, 1}:
        raise MaterialRequestNotDraftError(mr_name)

    comment = REJECTION_COMMENT_TEMPLATE.format(
        reason_code=reason_code.strip() or "REJECTED",
        reason=clean_reason,
    )
    erp_add_comment("Material Request", mr_name, comment)
    if docstatus == 0:
        erp_discard_draft("Material Request", mr_name)
        action = "discarded_draft"
    else:
        erp_cancel("Material Request", mr_name)
        action = "cancelled_submitted"
    return {"name": mr_name, "status": "CANCELLED", "action": action, "reason": clean_reason}


@router.post("/{mr_name}/rejection-comment", status_code=status.HTTP_201_CREATED)
def create_rejection_comment(mr_name: str, request: RejectionCommentRequest):
    """Backward-compatible route that now also closes the rejected MR."""
    try:
        result = reject_material_request(mr_name, request.reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MaterialRequestNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Material Request를 찾을 수 없습니다: {exc}",
        ) from exc
    except MaterialRequestNotDraftError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Draft 상태의 Material Request만 반려 사유를 등록할 수 있습니다: {exc}",
        ) from exc
    except ERPNextAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "success": True,
        "mr_name": mr_name,
        "status": "CANCELLED",
        "result": result,
    }
