"""
nodes/item/item_spec_validation.py — item_group별 필수규격 정의 + 신규품목
규격완결성 검증

독립 프로세스(2026-09-01 추가): backend_logic2/workflow의 MR LangGraph
파이프라인(process_graph.py)과는 완전히 별개임. 기존 "품목 신규등록 승인"
흐름(item_validation.py — ERPNext에서 Item 만들면 자동 disabled=1로
생성되고, 구매부서가 목록보고 수동승인)에 규격 사전검증을 얹는 용도.
실제로 언제/어떻게 이 검증을 실행시킬지(웹훅이든 폴링스크립트든)는 아직
안 정해짐 - 여기는 순수 판단 로직(validate_new_item 하나 호출하면 끝)만
담당. 트리거는 나중에 붙이면 됨.

설계 결정(사용자 확인, 2026-09-01):
  - item_group 최초 정의: AI(gpt-4o-mini, temperature=0 - 일관성 우선)한테
    "이 카테고리 진짜 최소 필수규격이 뭐야" 물어보고 바로 저장. 사람이
    사전에 검토해야 하는 게이트는 안 둠 - DB 한 줄이라 나중에 틀린 게
    보이면 그때 그 카테고리만 고치면 됨(케이스로깅과 같은 "실패해도
    나중에 고치면 되지 완벽한 사전검증 필요없다" 철학).
  - 기존 item_group의 완결성 확인은 순수 정규식/substring이 아니라
    AI로 함: "라벨은 있는데 값이 비어있음"이나 "라벨 없이 자연스럽게
    본문에 녹아있음" 같은 케이스를 문자열매칭으론 못 잡음(false
    positive/negative 둘 다 큼). 대신 description이 아예 비어있거나
    너무 짧으면(EMPTY_DESCRIPTION_MIN_LENGTH 미만) 그 뻔한 케이스는
    AI 호출 없이 바로 "전부 미기재"로 처리해서 비용을 아낌 - 우리가
    naver_contact_enrichment.py/narajangteo_search_based_tool.py에서
    이미 쓰던 "싼 필터 먼저, 애매하면 AI" 패턴 그대로.
  - AI 판단 이유는 기존 case_logging.log_ai_decision() 그대로 재사용
    (case_id=None으로 - 이 프로세스는 MR 케이스가 없음).

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/item/이 파일
DB 테이블: create_item_group_spec_table.py(레포 루트)로 최초 1회 생성 필요.

실행: python -m backend_logic2.nodes.item.item_spec_validation
"""

from __future__ import annotations

import json

from backend_logic2.integrations.erp_client import (
    erp_get_one,
    erp_add_comment,
    ERPNextAPIError,
    SITE_URL,
    HEADERS,
)
from backend_logic2.nodes.supplier.tools.case_logging import log_ai_decision

# description이 이 길이(문자수) 미만이면 AI 호출 없이 바로 "전부 미기재"로
# 처리 - 판단할 것도 없는 뻔한 케이스에 API 비용 쓰지 않으려는 사전필터.
EMPTY_DESCRIPTION_MIN_LENGTH = 10


def _get_conn_or_none():
    try:
        from procurement_db import get_connection
    except ImportError:
        print("    [item_spec_validation] procurement_db 모듈을 못 찾음, DB 조회/저장 건너뜀")
        return None
    return get_connection


def get_group_requirements(item_group: str) -> dict | None:
    """DB에 이미 저장된 이 item_group의 필수규격을 조회. 없으면 None."""
    get_connection = _get_conn_or_none()
    if get_connection is None:
        return None
    try:
        with get_connection(autocommit=True) as conn:
            row = conn.execute(
                "SELECT item_group, required_specs, reason "
                "FROM procurement.item_group_spec_requirements WHERE item_group = %(item_group)s",
                {"item_group": item_group},
            ).fetchone()
    except Exception as e:
        print(f"    [item_spec_validation] item_group 조회 실패: {e}")
        return None
    if not row:
        return None
    return {
        "item_group": row["item_group"],
        "required_specs": json.loads(row["required_specs"]),
        "reason": row["reason"],
    }


def _save_group_requirements(item_group: str, required_specs: list, reason: str) -> None:
    get_connection = _get_conn_or_none()
    if get_connection is None:
        return
    try:
        with get_connection(autocommit=True) as conn:
            conn.execute(
                """
                INSERT INTO procurement.item_group_spec_requirements (item_group, required_specs, reason)
                VALUES (%(item_group)s, %(required_specs)s, %(reason)s)
                ON CONFLICT (item_group) DO UPDATE
                    SET required_specs = EXCLUDED.required_specs,
                        reason = EXCLUDED.reason,
                        updated_at = now()
                """,
                {
                    "item_group": item_group,
                    "required_specs": json.dumps(required_specs, ensure_ascii=False),
                    "reason": reason,
                },
            )
    except Exception as e:
        print(f"    [item_spec_validation] item_group 저장 실패, 이번 판단만 메모리로 사용: {e}")


def _parse_ai_json(raw: str) -> dict:
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(cleaned)


def _ai_define_required_specs(item_group: str) -> dict:
    """AI한테 이 item_group의 진짜 최소 필수규격이 뭔지 물어봄 (temperature=0)."""
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = PromptTemplate.from_template(
        "당신은 기업 구매팀의 품목등록 검수 담당자입니다. "
        "다음 품목분류(item_group)에 대해, 구매요청서에 '이것만은 반드시' "
        "적혀 있어야 하는 최소 필수 규격 항목을 정하세요.\n\n"
        "품목분류: {item_group}\n\n"
        "규칙:\n"
        "- 정말 이게 없으면 발주 자체가 불가능한 항목만 최소로 고르세요 "
        "(과하게 많이 요구하지 마세요, 통상 2~5개 사이).\n"
        "- 항목명은 짧은 한국어 명사로 (예: '재질', '규격(치수)', '전압', '용량').\n"
        "- 왜 이 항목들을 필수로 골랐는지 한두 문장으로 이유를 남기세요.\n\n"
        '반드시 이 JSON 형식으로만 답하세요: '
        '{{"required_specs": ["항목1", "항목2"], "reason": "짧은 이유"}}'
    )
    result = (prompt | llm).invoke({"item_group": item_group}).content
    parsed = _parse_ai_json(result)
    return {
        "required_specs": [str(s).strip() for s in parsed.get("required_specs", []) if str(s).strip()],
        "reason": parsed.get("reason") or "",
    }


def get_or_create_group_requirements(item_group: str) -> dict:
    """
    이 item_group의 필수규격을 반환. DB에 없으면(처음 보는 카테고리) AI한테
    새로 물어보고 바로 저장까지 함 - 사전 사람검토 게이트는 없음. 나중에
    특정 카테고리 판정이 이상하면 DB에서 그 한 줄만 직접 고치면 됨.
    """
    existing = get_group_requirements(item_group)
    if existing:
        return existing

    print(f"    [신규 item_group] '{item_group}' 처음 보는 카테고리, AI로 필수규격 정의 중...")
    defined = _ai_define_required_specs(item_group)
    _save_group_requirements(item_group, defined["required_specs"], defined["reason"])
    log_ai_decision(
        case_id=None,
        node="item_group_spec_definition",
        reason=f"[{item_group}] 필수규격 {defined['required_specs']} 정의 - {defined['reason']}",
    )
    print(f"    -> 필수규격: {defined['required_specs']} ({defined['reason']})")
    return {"item_group": item_group, **defined}


def _ai_check_completeness(item_group: str, description: str, required_specs: list) -> list:
    """
    설명(description)에 필수규격 각각이 실제 값까지 채워져 기재됐는지 AI로
    판단. 라벨 존재여부가 아니라 의미상 채워졌는지를 봐야 해서(정규식/
    substring으론 "재질:"만 있고 값이 없는 경우나, 라벨 없이 자연스럽게
    녹아있는 경우를 둘 다 놓침) AI로 함.
    """
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = PromptTemplate.from_template(
        "다음은 품목분류 '{item_group}'에 대한 구매요청 설명입니다.\n\n"
        "[설명]\n{description}\n\n"
        "[확인해야 할 필수규격 목록]\n{required_specs}\n\n"
        "각 필수규격 항목이 이 설명에 실제 값까지 구체적으로 채워져서 "
        "기재됐는지 판단하세요. 항목 이름(라벨)만 있고 값이 비어있거나, "
        "아예 언급이 없으면 '미기재'입니다. 라벨이 명시적으로 없어도 "
        "설명 안에 그 값이 자연스럽게 들어있으면 '기재됨'으로 인정하세요.\n\n"
        '반드시 이 JSON 형식으로만 답하세요: '
        '{{"results": [{{"spec": "항목명", "present": true, "reason": "짧은 이유"}}]}}'
    )
    result = (prompt | llm).invoke({
        "item_group": item_group,
        "description": description,
        "required_specs": ", ".join(required_specs),
    }).content
    parsed = _parse_ai_json(result)
    return parsed.get("results", [])


def check_item_spec_completeness(item_group: str, description: str) -> dict:
    """
    반환: {"complete": bool, "missing": [...], "checked": [...], "requirements_reason": ...}
    """
    requirements = get_or_create_group_requirements(item_group)
    required_specs = requirements["required_specs"]

    if not required_specs:
        return {"complete": True, "missing": [], "checked": [], "requirements_reason": requirements.get("reason")}

    description = (description or "").strip()
    if len(description) < EMPTY_DESCRIPTION_MIN_LENGTH:
        print(f"    [규격확인] 설명이 너무 짧음({len(description)}자) - AI 호출 없이 전부 미기재 처리")
        return {
            "complete": False,
            "missing": required_specs,
            "checked": [],
            "requirements_reason": requirements.get("reason"),
        }

    results = _ai_check_completeness(item_group, description, required_specs)
    missing = [r["spec"] for r in results if not r.get("present")]
    reasons = "; ".join(
        f"{r.get('spec')}: {'기재됨' if r.get('present') else '미기재'}({r.get('reason', '')})"
        for r in results
    )

    log_ai_decision(
        case_id=None,
        node="item_spec_completeness_check",
        reason=f"[{item_group}] {reasons}",
    )

    return {
        "complete": len(missing) == 0,
        "missing": missing,
        "checked": results,
        "requirements_reason": requirements.get("reason"),
    }


def _activate_item(item_code: str) -> None:
    """item_validation.py의 approve_item_request와 동일한 방식 - disabled=0으로 활성화."""
    import requests

    res = requests.put(
        f"{SITE_URL}/api/resource/Item/{item_code}",
        headers=HEADERS,
        json={"disabled": 0},
    )
    if res.status_code != 200:
        raise ERPNextAPIError(f"승인(활성화) 실패: {res.status_code} - {res.text[:300]}")


def validate_new_item(item_code: str) -> dict:
    """
    메인 진입점. item_code 하나를 검증해서:
      - 필수규격 전부 기재됐으면 자동승인(disabled=0으로 활성화)
      - 아니면 ERPNext 코멘트로 누락항목 안내, disabled=1(보류) 그대로 둠
    반환: {"item_code":..., "approved": bool, "missing": [...]}
    """
    item = erp_get_one("Item", item_code)
    if not item:
        print(f"[item_spec_validation] Item을 찾을 수 없습니다: {item_code}")
        return {"item_code": item_code, "approved": False, "missing": [], "error": "not_found"}

    item_group = item.get("item_group")
    description = item.get("description") or ""

    if not item_group:
        print(f"[item_spec_validation] '{item_code}'에 item_group이 없어 검증을 건너뜁니다.")
        return {"item_code": item_code, "approved": False, "missing": [], "error": "no_item_group"}

    print(f"\n[규격검증] {item_code} (분류: {item_group})")
    result = check_item_spec_completeness(item_group, description)

    if result["complete"]:
        _activate_item(item_code)
        print(f"  -> 필수규격 전부 기재됨, 자동승인(활성화) 완료")
        return {"item_code": item_code, "approved": True, "missing": []}

    missing_text = ", ".join(result["missing"])
    comment = f"필수 규격사항({missing_text})이 누락되었습니다. 채워서 다시 요청해주세요."
    try:
        erp_add_comment("Item", item_code, comment)
    except ERPNextAPIError as e:
        print(f"  -> 코멘트 등록 실패: {e}")
    print(f"  -> 미기재 항목: {result['missing']} -> 코멘트 등록, 보류 상태 유지")
    return {"item_code": item_code, "approved": False, "missing": result["missing"]}


if __name__ == "__main__":
    _item_code = input("검증할 Item code 입력: ").strip()
    _result = validate_new_item(_item_code)
    print(f"\n결과: {_result}")
