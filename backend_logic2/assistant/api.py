"""Authenticated HTTP interface for the read-only BiddingFlow assistant."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, status

from auth_service.dependencies import CurrentUser

from .models import AssistantCapabilities, AssistantMessageRequest, AssistantMessageResponse
from .service import assistant_enabled, get_assistant_service


router = APIRouter(prefix="/api/assistant", tags=["BiddingFlow Assistant"])


@router.get("/capabilities", response_model=AssistantCapabilities)
def capabilities(current_user: CurrentUser) -> AssistantCapabilities:
    del current_user
    service = get_assistant_service()
    return AssistantCapabilities(
        enabled=assistant_enabled(),
        model=service.model.model_name,
        reasoning_effort=os.getenv("ASSISTANT_REASONING_EFFORT", "medium"),
        supports=[
            "기능 위치 안내",
            "사용법 검색",
            "담당 MR 조건 조회",
            "MR 현재 단계와 다음 행동 안내",
        ],
    )


@router.post("/messages", response_model=AssistantMessageResponse)
def create_message(
    body: AssistantMessageRequest,
    current_user: CurrentUser,
) -> AssistantMessageResponse:
    # This endpoint deliberately exposes no workflow mutation. The response
    # may contain navigation hints, but starting/approving/sending work remains
    # on the existing procurement endpoints and UI confirmation controls.
    if not assistant_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="기능 안내 챗봇이 현재 비활성화되어 있습니다.",
        )
    return get_assistant_service().answer(body, current_user=current_user)
