"""Admin-only policy editor. Authentication/authorization is enforced server-side."""

import logging
import psycopg
from fastapi import APIRouter, HTTPException
from auth_service.dependencies import CurrentUser
from backend_logic2.integrations.assignment_config import is_super_admin
from backend_logic2.policies import repository
from backend_logic2.policies.schema import PublishPolicy
from procurement_db.config import ProcurementDatabaseConfigurationError

router = APIRouter(prefix="/api/company-policy", tags=["Company policy"])
logger = logging.getLogger(__name__)


def _actor(user):
    return str(user.get("erp_user_id") or user.get("email") or "")


def _require_admin(user):
    if not is_super_admin(_actor(user)):
        raise HTTPException(403, "회사 정책은 관리자만 변경할 수 있습니다.")
    return _actor(user)


@router.get("/capabilities")
def capabilities(user: CurrentUser):
    return {"can_manage": is_super_admin(_actor(user))}


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
