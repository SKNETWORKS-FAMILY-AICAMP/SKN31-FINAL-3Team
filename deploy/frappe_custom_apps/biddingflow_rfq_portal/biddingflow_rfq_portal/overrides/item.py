"""Item 폼(Client Script)이 item_group의 필수 규격 placeholder를 받아가는
서버 대 서버 프록시.

절차: Item 폼에서 item_group을 고르면 Client Script가 이 whitelisted
메서드를 frappe.call()로 호출 -> 여기서 backend_logic2 FastAPI의
GET /api/webhooks/erpnext/item-groups/{item_group}/required-specs를
서버 대 서버로 호출해서 결과만 브라우저에 돌려준다. 공유 비밀키
(ERPNEXT_WEBHOOK_SECRET)를 브라우저 네트워크 탭에 노출시키지 않기
위해 이 프록시를 거친다.

배포 시 이 사이트의 site_config.json에 아래 두 키를 반드시 추가해야
한다 (bench --site <site> set-config <key> <value>):
  - procurement_api_base_url : backend_logic2 FastAPI의 base URL
                                (예: http://backend:8000 — 도커 컴포즈
                                내부 네트워크에서 백엔드 서비스명으로
                                접근 가능한 주소)
  - erpnext_webhook_secret   : backend_logic2/.env의 ERPNEXT_WEBHOOK_SECRET과
                                반드시 동일한 값

site_config.json에 넣기 애매하면 이 프론트(ERPNext) 컨테이너 환경변수로
PROCUREMENT_API_BASE_URL / ERPNEXT_WEBHOOK_SECRET을 넣어도 동작한다
(아래 _config()가 둘 다 확인함).
"""

import os

import frappe

_AUTO_MARKER = "[자동생성 - 품목분류 필수 규격, 값을 채워주세요]"


def _config(site_config_key: str, env_var_name: str) -> str:
    value = frappe.conf.get(site_config_key) or os.environ.get(env_var_name)
    if not value:
        frappe.throw(
            f"'{site_config_key}' 설정이 없습니다. site_config.json에 "
            f"'{site_config_key}'를 추가하거나 환경변수 {env_var_name}를 "
            f"설정해주세요."
        )
    return value


@frappe.whitelist()
def get_required_specs_placeholder(item_group: str):
    """item_group의 필수 규격을 백엔드에서 조회해 description용 placeholder
    텍스트까지 만들어 돌려준다.

    get_or_create_group_requirements가 처음 보는 item_group이라도 AI로
    즉시 필수 규격을 정의해서 DB에 저장하고 반환하므로(자가치유), 이
    호출 한 번으로 바로 최신 필수 규격을 받는다 - 별도로 "일단 실패시켜서
    AI가 정의하게 한 뒤 재조회" 하는 단계가 필요 없다.
    """
    if not item_group:
        frappe.throw("item_group이 필요합니다.")

    import requests

    base_url = _config("procurement_api_base_url", "PROCUREMENT_API_BASE_URL").rstrip("/")
    secret = _config("erpnext_webhook_secret", "ERPNEXT_WEBHOOK_SECRET")

    try:
        response = requests.get(
            f"{base_url}/api/webhooks/erpnext/item-groups/{item_group}/required-specs",
            headers={"X-ERPNext-Webhook-Secret": secret},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        frappe.log_error(title="get_required_specs_placeholder 실패", message=str(exc))
        frappe.throw(f"필수 규격 조회에 실패했습니다: {exc}")

    data = response.json()
    required_specs = data.get("required_specs") or []
    placeholder = "\n".join([_AUTO_MARKER, *[f"{spec}: " for spec in required_specs]])

    return {
        "item_group": data.get("item_group") or item_group,
        "required_specs": required_specs,
        "reason": data.get("reason"),
        "placeholder": placeholder,
        "marker": _AUTO_MARKER,
    }
