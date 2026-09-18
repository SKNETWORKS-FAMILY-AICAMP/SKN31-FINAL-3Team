"""Admin-only policy editor. Authentication/authorization is enforced server-side."""

import logging
from uuid import UUID
import psycopg
from fastapi import APIRouter, HTTPException
from auth_service.dependencies import CurrentUser
from backend_logic2.policies.access import read_policy_access, PolicyAccessUnavailable
from backend_logic2.policies import repository
from backend_logic2.policies.schema import PublishPolicy, CompanyPolicy
from procurement_db.config import ProcurementDatabaseConfigurationError
from backend_logic2.policies import allowlist
from backend_logic2.services import runpod_worker_control as worker_control
from backend_logic2.repositories import ai_decisions

router = APIRouter(prefix="/api/company-policy", tags=["Company policy"])
logger = logging.getLogger(__name__)


@router.get('/ai-decisions')
def read_ai_decisions(
    user: CurrentUser,
    node: str | None = None,
    case_id: UUID | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Return the administrator-visible AI reasoning audit trail."""

    _require_admin(user)
    if limit < 1 or limit > 200 or offset < 0:
        raise HTTPException(422, "limit은 1~200, offset은 0 이상이어야 합니다.")
    try:
        items, count = ai_decisions.list_decisions(
            node=node,
            case_id=str(case_id) if case_id else None,
            limit=limit,
            offset=offset,
        )
        return {
            "items": items,
            "count": count,
            "nodes": ai_decisions.list_nodes(),
            "limit": limit,
            "offset": offset,
        }
    except (psycopg.Error, ProcurementDatabaseConfigurationError) as exc:
        logger.exception("AI decision audit lookup failed")
        raise HTTPException(503, "AI 판단 로그 저장소를 조회할 수 없습니다.") from exc


@router.get('/runpod-worker')
def read_runpod_worker(user: CurrentUser):
    _require_admin(user)
    try:
        return worker_control.status()
    except worker_control.ControlUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except (psycopg.Error, ProcurementDatabaseConfigurationError) as exc:
        raise HTTPException(503, '워커 설정 저장소를 확인할 수 없습니다.') from exc


@router.post('/runpod-worker')
def change_runpod_worker(body: worker_control.WorkerCommand, user: CurrentUser):
    actor = _require_admin(user)
    try:
        return worker_control.command(body, actor)
    except worker_control.ControlConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except worker_control.ControlUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except (psycopg.Error, ProcurementDatabaseConfigurationError) as exc:
        raise HTTPException(503, '워커 설정 저장 결과를 확인할 수 없습니다. 새로고침 후 확인하세요.') from exc


def _actor(user):
    return str(user.get("erp_user_id") or user.get("email") or "")


def _access(user):
    try:
        return read_policy_access(_actor(user))
    except PolicyAccessUnavailable as exc:
        raise HTTPException(503, "ERPNext 권한을 확인할 수 없습니다. 권한 조회 연결을 확인해주세요.") from exc


def _require_admin(user):
    # Re-read on EVERY read/publish, so an open tab cannot retain a revoked role.
    if not _access(user)["can_manage"]:
        raise HTTPException(403, "ERPNext 관리자 또는 구매 정책 관리자 권한이 필요합니다.")
    return _actor(user)


@router.get("/capabilities")
def capabilities(user: CurrentUser):
    return _access(user)


@router.get("")
def read_policy(user: CurrentUser):
    _require_admin(user)
    try:
        # Expand defaults at the read boundary; immutable old DB versions stay
        # untouched and can still be restored through the current editor.
        def expanded(row):
            return {**row, 'policy': CompanyPolicy.model_validate(row['policy']).model_dump()}
        return {"active": expanded(repository.get_active()),
                "history": [expanded(row) for row in repository.list_versions()]}
    except (psycopg.Error, ProcurementDatabaseConfigurationError) as exc:
        logger.exception("Policy lookup failed")
        raise HTTPException(503, "정책 저장소에 연결할 수 없습니다. 관리자에게 마이그레이션 상태를 확인해주세요.") from exc


@router.post("/publish")
def publish_policy(body: PublishPolicy, user: CurrentUser):
    actor = _require_admin(user)
    if len(body.reason.strip()) < 3:
        raise HTTPException(422, "변경 사유를 3자 이상 입력하세요.")
    try:
        return repository.publish(body.policy, expected_version=body.expected_version,
                                  actor=actor, reason=body.reason)
    except repository.PolicyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (psycopg.Error, ProcurementDatabaseConfigurationError) as exc:
        logger.exception("Policy publication failed")
        raise HTTPException(503, "정책 저장소에 연결할 수 없습니다. 저장 결과를 다시 확인하세요.") from exc


@router.get('/email-allowlist')
def read_email_allowlist(user: CurrentUser):
    _require_admin(user)
    try:
        return allowlist.get_allowlist()
    except allowlist.AllowlistUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post('/email-allowlist')
def update_email_allowlist(body: allowlist.SaveAllowlist, user: CurrentUser):
    actor = _require_admin(user)
    try:
        return allowlist.save_allowlist(body, actor)
    except allowlist.AllowlistConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except allowlist.AllowlistUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (psycopg.Error, ProcurementDatabaseConfigurationError) as exc:
        raise HTTPException(503, '화이트리스트 저장 잠금을 확보할 수 없습니다.') from exc
