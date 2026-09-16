"""Admin-only policy editor. Authentication/authorization is enforced server-side."""

import logging
import psycopg
from fastapi import APIRouter, HTTPException
from auth_service.dependencies import CurrentUser
from backend_logic2.policies.access import read_policy_access, PolicyAccessUnavailable
from backend_logic2.policies import repository
from backend_logic2.policies.schema import PublishPolicy
from procurement_db.config import ProcurementDatabaseConfigurationError
from backend_logic2.policies import allowlist

router = APIRouter(prefix="/api/company-policy", tags=["Company policy"])
logger = logging.getLogger(__name__)


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
        return {"active": repository.get_active(), "history": repository.list_versions()}
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
