"""
nodes/register_candidate_suppliers.py — 5번 모듈:
resolve_suppliers.py로 찾은 신규 후보들을 실제 ERPNext Supplier로 등록

⚠️ Contact/User/User Permission은 여기서 안 만듦 — 그건 포털 로그인이
필요한 "RFQ 발송" 단계(아직 구현 안 함)에서, ERPNext 내장기능
(send_supplier_emails)이 알아서 처리하는 부분. 여기는 순수하게
"Supplier 문서로 존재하게만" 만드는 역할.

이미 존재하는 Supplier는 건너뜀 (중복 생성 방지).

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/register_candidate_suppliers.py
"""


import html
import requests
from backend_logic2.integrations.erp_client import erp_get_one, erp_post, ERPNextAPIError, SITE_URL, HEADERS
from backend_logic2.nodes.supplier.tools.case_logging import log_status_change


def _sanitize_name(name: str, max_length: int = 140) -> str:
    """
    HTML 인코딩(&quot; 등) 풀고 길이 제한 — 검색결과 제목을 그대로 이름으로
    쓰다가 깨진 텍스트가 Supplier 이름으로 들어가서 포털 페이지가 500 에러로
    깨진 적이 실제로 있어서, 등록 직전에 마지막 안전장치로 정리함.
    """
    cleaned = html.unescape(name).strip()
    return cleaned[:max_length].strip()


def get_existing_supplier(name: str):
    """이 이름의 기존 Supplier를 반환하고, 없으면 None을 반환한다."""
    try:
        return erp_get_one("Supplier", name)
    except ERPNextAPIError:
        return None


def supplier_exists(name: str) -> bool:
    return get_existing_supplier(name) is not None


def register_candidate_suppliers(candidates: list, case_id: str = None) -> list:
    """
    candidates: [{"name": ..., "email": ...(선택), "phone": ...(선택)}, ...]
    이미 존재하는 이름은 건너뛰고, 없는 것만 새로 생성.

    case_id를 넘기면(예: supplier_search()가 쓰던 케이스를 그대로 이어서)
    등록 결과 요약을 case_status_history에 'suppliers_registered' 상태로
    남김 - 탐색(searching/collected/search_completed) 다음 단계로 케이스
    이력이 이어짐. 안 넘기면(단독 실행 등) 그냥 등록만 하고 이력은 안 남김.

    반환: [{"name": 등록된이름, "status": "created" 또는 "already_exists" 또는 "failed", ...}]
    """
    results = []

    for c in candidates:
        raw_name = c.get("name")
        if not raw_name:
            results.append({"name": None, "status": "failed", "reason": "이름 없음"})
            continue

        name = _sanitize_name(raw_name)

        email = str(c.get("email") or "").strip()
        if not email:
            results.append({"name": name, "status": "failed", "reason": "이메일 없음"})
            continue

        existing = get_existing_supplier(name)
        if existing:
            if not existing.get("email_id"):
                response = requests.put(
                    f"{SITE_URL}/api/resource/Supplier/{name}",
                    headers=HEADERS,
                    json={"email_id": email},
                )
                if response.status_code != 200:
                    results.append({
                        "name": name,
                        "status": "failed",
                        "reason": f"기존 Supplier 이메일 갱신 실패: {response.text[:300]}",
                    })
                    continue
                results.append({"name": name, "status": "updated"})
            else:
                results.append({"name": name, "status": "already_exists"})
            continue

        payload = {
            "supplier_name": name,
            "supplier_group": "All Supplier Groups",
            "country": "Korea, Republic of",
            "supplier_type": "Company",
        }
        payload["email_id"] = email

        try:
            created = erp_post("Supplier", payload)
            results.append({"name": created["name"], "status": "created"})
        except ERPNextAPIError as e:
            results.append({"name": name, "status": "failed", "reason": str(e)})

    if case_id:
        counts = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        counts_text = ", ".join(f"{status} {n}건" for status, n in counts.items())
        log_status_change(
            case_id, "suppliers_registered",
            reason=f"공급사 등록 총 {len(results)}건 처리 ({counts_text})",
        )

    return results

