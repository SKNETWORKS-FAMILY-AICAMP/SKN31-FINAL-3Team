"""
nodes/item/item_spec_validation.py — item_group별 필수규격 정의 + 신규품목
규격완결성 검증

독립 프로세스(2026-09-01 추가): backend_logic2/workflow의 MR LangGraph
파이프라인(process_graph.py)과는 완전히 별개임. 기존 "품목 신규등록 승인"
흐름(item_validation.py — ERPNext에서 Item 만들면 자동 disabled=1로
생성되고, 구매부서가 목록보고 수동승인)에 규격 사전검증을 얹는 용도.
실행 트리거는 Item 생성·수정 웹훅이며, 로컬 polling 모드와 서버 재시작
복구에서는 item_service.reconcile_disabled_items가 같은 진입점을 호출함.

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

연동 코드: backend_logic2/integrations/erp_client.py,
backend_logic2/services/item_service.py
DB 테이블: migrations/010_create_item_group_spec_requirements.sql로 관리.

실행: python -m backend_logic2.nodes.item.item_spec_validation
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from urllib.parse import quote

from backend_logic2.integrations.erp_client import (
    erp_get_one,
    erp_add_comment,
    ERPNextAPIError,
    SITE_URL,
    HEADERS,
)
from backend_logic2.nodes.supplier.tools.case_logging import log_ai_decision
from procurement_db import get_connection

# description이 이 길이(문자수) 미만이면 AI 호출 없이 바로 "전부 미기재"로
# 처리 - 판단할 것도 없는 뻔한 케이스에 API 비용 쓰지 않으려는 사전필터.
EMPTY_DESCRIPTION_MIN_LENGTH = 10


class ItemSpecificationPolicyError(RuntimeError):
    """품목군 규격 정책을 만들거나 영속화할 수 없을 때 발생한다.

    정책을 저장하지 못한 채 일회성 AI 결과만으로 Item을 활성화하면 다음
    요청에서 판정 기준이 달라질 수 있다. 따라서 이 오류는 웹훅을 실패로
    남기고 Item은 disabled 상태로 유지하도록 호출부까지 전파한다.
    """


def _normalize_required_spec_names(value: object) -> list[str]:
    """과거 문자열 목록과 구조화된 규격 정의를 짧은 항목명 목록으로 통일한다."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ItemSpecificationPolicyError(
                "품목군 필수 규격 데이터가 올바른 JSON이 아닙니다."
            ) from exc

    if isinstance(value, Mapping):
        value = value.get("fields", value)
        if isinstance(value, Mapping):
            value = [
                (
                    {"fieldname": fieldname, **metadata}
                    if isinstance(metadata, Mapping)
                    else {"fieldname": fieldname, "label": metadata}
                )
                for fieldname, metadata in value.items()
            ]

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ItemSpecificationPolicyError("품목군 필수 규격은 목록 형식이어야 합니다.")

    names: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if isinstance(entry, Mapping):
            raw_name = entry.get("label") or entry.get("fieldname") or entry.get("name")
        else:
            raw_name = entry
        name = str(raw_name or "").strip()
        key = name.casefold()
        if name and key not in seen:
            names.append(name)
            seen.add(key)
    if not names:
        raise ItemSpecificationPolicyError("품목군 필수 규격이 비어 있습니다.")
    return names


def get_group_requirements(item_group: str) -> dict | None:
    """DB에 이미 저장된 이 item_group의 필수규격을 조회. 없으면 None."""
    with get_connection(autocommit=True) as conn:
        row = conn.execute(
            "SELECT item_group, required_specs, reason "
            "FROM procurement.item_group_spec_requirements WHERE item_group = %(item_group)s",
            {"item_group": item_group},
        ).fetchone()
    if not row:
        return None
    return {
        "item_group": row["item_group"],
        "required_specs": _normalize_required_spec_names(row["required_specs"]),
        "reason": row["reason"],
    }


def _save_group_requirements(
    item_group: str, required_specs: list[str], reason: str
) -> dict:
    """최초 생성자가 만든 정책을 저장하고 DB에 확정된 값을 반환한다.

    같은 품목군의 웹훅이 동시에 도착할 수 있으므로 기존 행은 덮어쓰지
    않는다. 경합 시 먼저 저장된 정책을 다시 읽어 모든 요청이 같은 기준을
    사용하게 한다.
    """

    normalized_specs = _normalize_required_spec_names(required_specs)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO procurement.item_group_spec_requirements (
                item_group, required_specs, reason
            ) VALUES (
                %(item_group)s, %(required_specs)s, %(reason)s
            )
            ON CONFLICT (item_group) DO NOTHING
            """,
            {
                "item_group": item_group,
                "required_specs": json.dumps(normalized_specs, ensure_ascii=False),
                "reason": reason,
            },
        )
        row = conn.execute(
            """
            SELECT item_group, required_specs, reason
            FROM procurement.item_group_spec_requirements
            WHERE item_group = %(item_group)s
            """,
            {"item_group": item_group},
        ).fetchone()
    if not row:
        raise ItemSpecificationPolicyError(
            f"'{item_group}' 품목군 필수 규격을 DB에 저장하지 못했습니다."
        )
    return {
        "item_group": row["item_group"],
        "required_specs": _normalize_required_spec_names(row["required_specs"]),
        "reason": row["reason"],
    }


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
    required_specs = _normalize_required_spec_names(defined.get("required_specs"))
    persisted = _save_group_requirements(item_group, required_specs, defined["reason"])
    log_ai_decision(
        case_id=None,
        node="item_group_spec_definition",
        reason=(
            f"[{item_group}] 필수규격 {persisted['required_specs']} 정의 - "
            f"{persisted.get('reason') or ''}"
        ),
    )
    print(
        f"    -> 필수규격: {persisted['required_specs']} "
        f"({persisted.get('reason') or ''})"
    )
    return persisted


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
    results = parsed.get("results", [])
    return results if isinstance(results, list) else []


def check_item_spec_completeness(item_group: str, description: str) -> dict:
    """
    반환: {"complete": bool, "missing": [...], "checked": [...], "requirements_reason": ...}
    """
    requirements = get_or_create_group_requirements(item_group)
    required_specs = requirements["required_specs"]

    description = (description or "").strip()
    if len(description) < EMPTY_DESCRIPTION_MIN_LENGTH:
        print(f"    [규격확인] 설명이 너무 짧음({len(description)}자) - AI 호출 없이 전부 미기재 처리")
        return {
            "complete": False,
            "missing": required_specs,
            "checked": [],
            "requirements_reason": requirements.get("reason"),
        }

    ai_results = _ai_check_completeness(item_group, description, required_specs)
    by_spec = {
        str(result.get("spec") or "").strip().casefold(): result
        for result in ai_results
        if isinstance(result, Mapping) and result.get("spec")
    }
    # AI가 필수 항목 하나를 응답에서 빼먹은 경우 이를 기재 완료로 오인하지
    # 않는다. 명시적으로 present=true인 항목만 통과시킨다.
    results = []
    for spec in required_specs:
        ai_result = by_spec.get(spec.casefold())
        results.append(
            {
                "spec": spec,
                "present": bool(ai_result and ai_result.get("present") is True),
                "reason": (
                    str(ai_result.get("reason") or "")
                    if ai_result
                    else "AI 응답에 해당 필수 항목 판정이 없습니다."
                ),
            }
        )
    missing = [result["spec"] for result in results if not result["present"]]
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
        f"{SITE_URL}/api/resource/Item/{quote(item_code, safe='')}",
        headers=HEADERS,
        json={"disabled": 0},
        timeout=30,
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
        raise LookupError(f"Item을 찾을 수 없습니다: {item_code}")

    item_group = item.get("item_group")
    description = item.get("description") or ""

    if not item_group:
        comment = (
            "필수 정보(품목 그룹)가 누락되었습니다. 품목 그룹을 지정한 뒤 "
            "다시 요청해주세요."
        )
        erp_add_comment("Item", item_code, comment)
        return {
            "item_code": item_code,
            "approved": False,
            "missing": ["품목 그룹"],
            "error": "no_item_group",
        }

    print(f"\n[규격검증] {item_code} (분류: {item_group})")
    result = check_item_spec_completeness(item_group, description)

    if result["complete"]:
        _activate_item(item_code)
        print(f"  -> 필수규격 전부 기재됨, 자동승인(활성화) 완료")
        return {"item_code": item_code, "approved": True, "missing": []}

    missing_text = ", ".join(result["missing"])
    comment = f"필수 규격사항({missing_text})이 누락되었습니다. 채워서 다시 요청해주세요."
    # 댓글 등록도 업무 결과의 일부다. 실패를 숨기면 사용자는 왜 Item이
    # 비활성인지 알 수 없으므로 예외를 전파해 integration_event를 FAILED로
    # 남기고 같은 웹훅/대사에서 재시도할 수 있게 한다.
    erp_add_comment("Item", item_code, comment)
    print(f"  -> 미기재 항목: {result['missing']} -> 코멘트 등록, 보류 상태 유지")
    return {"item_code": item_code, "approved": False, "missing": result["missing"]}


if __name__ == "__main__":
    _item_code = input("검증할 Item code 입력: ").strip()
    _result = validate_new_item(_item_code)
    print(f"\n결과: {_result}")
