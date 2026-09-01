"""Record a Material Request rejection reason without changing Draft state."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from backend_logic2.integrations.erp_client import (
    ERPNextAPIError,
    erp_add_comment,
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


@router.post("/{mr_name}/rejection-comment", status_code=status.HTTP_201_CREATED)
def create_rejection_comment(mr_name: str, request: RejectionCommentRequest):
    """Record a rejection reason while preserving the MR's Draft state."""
    try:
        comment = add_rejection_comment(mr_name, request.reason)
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
        "status": "Draft",
        "comment": comment,
    }
