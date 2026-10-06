"""Authenticated HTTP interface for the read-only BiddingFlow assistant."""

from __future__ import annotations

import os
import logging
from uuid import UUID
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Response, status

from auth_service.dependencies import CurrentUser

from .models import AssistantCapabilities, AssistantMessageRequest, AssistantMessageResponse, AssistantSessionCreate, ConversationMessage
from .service import assistant_enabled, get_assistant_service, actor_id
from .session_store import AssistantSessionStore, SessionNotFound, SessionConflict


router = APIRouter(prefix="/api/assistant", tags=["BiddingFlow Assistant"])
session_store = AssistantSessionStore()


@contextmanager
def session_errors():
    try:
        yield
    except SessionNotFound:
        raise HTTPException(404, "대화를 찾을 수 없습니다. 삭제되었거나 접근 권한이 없습니다.") from None
    except SessionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except HTTPException:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Assistant session storage unavailable")
        raise HTTPException(503, "대화 저장소에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.") from None


@router.get("/sessions")
def list_sessions(current_user: CurrentUser):
    with session_errors():
        return {"sessions": session_store.list(actor_id(current_user))}


@router.post("/sessions", status_code=201)
def create_session(body: AssistantSessionCreate, current_user: CurrentUser):
    with session_errors():
        return session_store.create(actor_id(current_user), body.id)


@router.get("/sessions/{session_id}")
def get_session(session_id: UUID, current_user: CurrentUser):
    with session_errors():
        return session_store.get(actor_id(current_user), session_id)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: UUID, current_user: CurrentUser):
    with session_errors():
        session_store.delete(actor_id(current_user), session_id)
    return Response(status_code=204)


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
            "같은 대화의 조건 좁히기와 목록 이어보기",
            "조회 목록의 순서로 MR 선택",
            "담당 업무와 직접 결정할 업무 구분",
            "모호한 요청 확인 질문과 선택지",
            "최근 8턴 기억과 이전 대화 의도 요약",
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
    if body.session_id is None:
        # Older frontends remain compatible during staggered deployment.
        return get_assistant_service().answer(body, current_user=current_user)
    owner = actor_id(current_user)
    with session_errors():
        session = session_store.get(owner, body.session_id)
        if body.session_version is not None and body.session_version != session["version"]:
            raise SessionConflict("다른 탭에서 대화가 변경되었습니다. 세션 목록에서 다시 열어 주세요.")
        # Ignore browser-provided conversation/context once a persisted session is selected.
        from .memory import restore_memory
        request = body.model_copy(update={
            "dialogue": restore_memory(session),
            "conversation": [ConversationMessage(role="user", content=m["text"][:3000])
                             for m in session["messages"] if m["sender"] == "user"][-8:],
        })
        response = get_assistant_service().answer(request, current_user=current_user)
        version = session["version"]
        if not response.meta.get("busy"):
            version = session_store.append_turn(owner, session, body.message, response)
        response.meta["session_version"] = version
        response.meta["session_persisted"] = not response.meta.get("busy", False)
        return response
