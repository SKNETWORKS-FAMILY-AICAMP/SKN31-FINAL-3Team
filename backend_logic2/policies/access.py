"""Read the authenticated user's current ERP roles; never trust browser roles."""

from urllib.parse import quote
import requests
from backend_logic2.integrations.erp_client import SITE_URL, HEADERS

# Ordinary purchasing users can process documents but cannot rewrite policy.
# These are role names to recognize, NOT roles to create/assign automatically.
POLICY_MANAGER_ROLES = frozenset({"System Manager", "Purchase Master Manager"})


class PolicyAccessUnavailable(Exception):
    pass


def read_policy_access(erp_user_id: str) -> dict:
    if not erp_user_id or erp_user_id.strip().casefold() == 'guest':
        return {"can_manage": False, "roles": [], "source": "erpnext", "enabled": False}
    try:
        response = requests.get(
            f"{SITE_URL.rstrip('/')}/api/resource/User/{quote(erp_user_id, safe='')}",
            headers=HEADERS, timeout=(5, 10),
        )
        if response.status_code == 404:
            return {"can_manage": False, "roles": [], "source": "erpnext", "enabled": False}
        response.raise_for_status()
        document = response.json().get("data")
        if not isinstance(document, dict) or str(document.get("name", "")).casefold() != erp_user_id.casefold():
            raise ValueError("ERP user identity mismatch")
        enabled = document.get("enabled") in (1, "1", True)
        roles = sorted({row['role'] for row in document.get('roles', [])
                        if isinstance(row, dict) and isinstance(row.get('role'), str)})
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        # Do not include ERP response bodies or credentials in API errors/logs.
        raise PolicyAccessUnavailable("ERPNext 사용자 권한을 확인할 수 없습니다.") from exc
    return {
        "can_manage": enabled and (
            erp_user_id.casefold() == 'administrator' or bool(POLICY_MANAGER_ROLES.intersection(roles))
        ),
        "roles": roles,
        "source": "erpnext",
        "enabled": enabled,
    }
