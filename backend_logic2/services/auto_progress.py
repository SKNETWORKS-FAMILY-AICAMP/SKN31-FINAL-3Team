"""자동 진행 조건 평가 - "사람을 부를 이유"가 있는지 판정한다.

설계 원칙은 하나다. **판단이 애매하면 사람에게 넘긴다.**
RFQ 발송도 최종 선정도 외부로 메일이 나가는, 되돌리기 어려운 행동이다.
그래서 조건을 "자동으로 갈 이유"가 아니라 "사람을 부를 이유"(blocker)로
적고, 하나라도 걸리면 멈춘다. 값을 알 수 없을 때도 통과가 아니라 정지다.

여기서 반환하는 checks는 화면(예외 결정 화면)에 그대로 목록으로 뿌린다 -
담당자가 "왜 멈췄지"가 아니라 "이것만 해결하면 되는구나"를 바로 읽을 수
있어야 해서, 걸린 항목만이 아니라 통과한 항목까지 함께 돌려준다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from backend_logic2.policies.runtime import current_policy


@dataclass
class AutoDecision:
    """조건 평가 결과.

    allowed  : 조건을 모두 통과했는가(모드와 무관한 순수 판정)
    mode     : off / shadow / on
    enabled  : 이 단계의 자동화 스위치가 켜져 있는가
    """

    allowed: bool
    mode: str
    enabled: bool
    # 어느 단계의 판정인지. 화면이 지난 단계의 판정을 지금 상태로 착각하지
    # 않으려면 반드시 필요하다(RFQ 발송 판정이 남아 있는데 최종 선정이
    # 멈춘 것처럼 보이는 문제가 실제로 있었다).
    node: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def blockers(self) -> list[dict[str, Any]]:
        return [row for row in self.checks if row.get("status") == "blocked"]

    @property
    def should_proceed(self) -> bool:
        """실제로 사람 없이 진행해도 되는가."""
        return self.allowed and self.enabled and self.mode == "on"

    @property
    def shadow_only(self) -> bool:
        """조건은 통과했지만 기록만 남기고 멈춰야 하는가."""
        return self.allowed and self.enabled and self.mode == "shadow"

    def summary(self) -> str:
        if not self.enabled or self.mode == "off":
            return "자동 진행이 꺼져 있어 담당자 확인으로 넘겼습니다."
        if self.allowed:
            passed = len([row for row in self.checks if row["status"] == "passed"])
            tail = " (기록만, 실제 진행은 안 함)" if self.mode == "shadow" else ""
            return f"조건 {passed}개를 모두 통과했습니다{tail}."
        reasons = "; ".join(row["detail"] for row in self.blockers)
        return f"자동 진행을 멈췄습니다 - {reasons}"

    def as_payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "enabled": self.enabled,
            "node": self.node,
            "checks": self.checks,
            "evidence": self.evidence,
            "summary": self.summary(),
        }


def record_decision(case_id: str | None, node: str, decision: "AutoDecision") -> None:
    """자동 진행 판정을 AI 판단 로그에 남긴다.

    자동으로 진행했든 멈췄든 "왜 그랬는지"가 남아야, 나중에 문제가 생겼을
    때 설명할 수 있고 섀도 모드의 일치율도 집계할 수 있다.
    """
    if not case_id:
        return
    try:
        from backend_logic2.nodes.supplier.tools.case_logging import log_ai_decision

        blocked = [row["code"] for row in decision.blockers]
        detail = decision.summary()
        if decision.evidence:
            facts = ", ".join(
                f"{key}={value}" for key, value in decision.evidence.items() if value is not None
            )
            if facts:
                detail = f"{detail} [{facts}]"
        if blocked:
            detail = f"{detail} (걸린 조건: {', '.join(blocked)})"
        log_ai_decision(str(case_id), decision.node or node, f"[{decision.mode}] {detail}")
    except Exception:  # noqa: BLE001 - 기록 실패가 진행을 막으면 안 된다
        pass


def _check(code: str, label: str, detail: str, *, status: str) -> dict[str, Any]:
    return {"code": code, "label": label, "detail": detail, "status": status}


def _passed(code: str, label: str, detail: str) -> dict[str, Any]:
    return _check(code, label, detail, status="passed")


def _blocked(code: str, label: str, detail: str) -> dict[str, Any]:
    return _check(code, label, detail, status="blocked")


def _unknown(code: str, label: str, detail: str) -> dict[str, Any]:
    """판단할 수 없는 항목. 통과로 치지 않는다."""
    return _check(code, label, detail, status="unknown")


def _rules():
    return current_policy().rules


def _amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def evaluate_rfq_dispatch(
    candidates: list[dict[str, Any]],
    *,
    pool_decision: dict[str, Any] | None = None,
) -> AutoDecision:
    """RFQ 대상을 사람 확인 없이 확정·발송해도 되는지.

    ⚠️ 판정의 뼈대는 이미 resolve_supplier_pool이 내려놓았다. 그 함수는
    품목마다 과거 확정 구매이력, 기존 공급사 수(최소 경쟁 기준), 공급사
    풀의 노후화(마지막 등록 시점)를 보고 "기존 풀만 쓸지 / 신규를 찾아야
    할지"를 정한다. 즉 **기존 풀로 갈라진 건은 그 자체로 신규 협력사가
    없고 경쟁 수도 충족한다는 뜻**이다.

    그래서 여기서 후보 목록을 다시 비교해 같은 결론을 내지 않는다. 그건
    중복인 데다 정보도 적다. 그 판정을 그대로 받고, 발송에 실제로 필요한
    것(연락처)만 따로 확인한다.
    """
    rules = _rules()
    mode = rules.automation_mode
    enabled = bool(rules.auto_rfq_dispatch)

    named = [
        dict(candidate)
        for candidate in candidates or []
        if isinstance(candidate, dict) and str(candidate.get("name") or "").strip()
    ]
    checks: list[dict[str, Any]] = []

    reasons = [str(row) for row in (pool_decision or {}).get("reasons") or []]
    detail = " / ".join(reasons) if reasons else ""
    if pool_decision is None:
        checks.append(_unknown(
            "SUPPLIER_POOL",
            "기존 공급사 풀만으로 진행",
            "공급사 풀 판정 기록이 없어 담당자가 대상을 정합니다.",
        ))
    elif pool_decision.get("needs_search"):
        checks.append(_blocked(
            "SUPPLIER_POOL",
            "기존 공급사 풀만으로 진행",
            detail or "신규 협력사 탐색이 필요하다고 판정된 건이라 담당자가 대상을 정합니다.",
        ))
    else:
        checks.append(_passed(
            "SUPPLIER_POOL",
            "기존 공급사 풀만으로 진행",
            detail or "거래 이력이 있는 기존 공급사만으로 경쟁 요건을 채웠습니다",
        ))

    if not named:
        checks.append(_blocked(
            "HAS_CANDIDATES",
            "발송 대상 존재",
            "RFQ를 보낼 후보가 없어 담당자가 직접 지정해야 합니다",
        ))
    else:
        checks.append(_passed("HAS_CANDIDATES", "발송 대상 존재", f"후보 {len(named)}곳"))

    missing_email = [
        str(row.get("name")).strip()
        for row in named
        if not str(row.get("email") or "").strip()
    ]
    if missing_email:
        checks.append(_blocked(
            "MISSING_EMAIL",
            "연락처 확보",
            f"이메일이 없는 후보가 {len(missing_email)}곳 있습니다 ({', '.join(missing_email[:3])})",
        ))
    else:
        checks.append(_passed("MISSING_EMAIL", "연락처 확보", "모든 후보의 이메일이 확인됐습니다"))

    allowed = all(row["status"] == "passed" for row in checks)
    return AutoDecision(
        allowed=allowed,
        mode=mode,
        enabled=enabled,
        node="auto_rfq_dispatch",
        checks=checks,
        evidence={
            "candidate_count": len(named),
            "missing_email_count": len(missing_email),
            "needs_search": (pool_decision or {}).get("needs_search"),
        },
    )


def score_gap(ranking: list[dict[str, Any]]) -> float | None:
    """1순위와 2순위의 종합점수 차이. 비교 대상이 없으면 None."""
    scores = [
        float(row.get("overall_score"))
        for row in ranking or []
        if isinstance(row, dict) and isinstance(row.get("overall_score"), (int, float))
    ]
    if len(scores) < 2:
        return None
    ordered = sorted(scores, reverse=True)
    return round(ordered[0] - ordered[1], 2)


def evaluate_final_selection(
    result: dict[str, Any],
    *,
    deadline_passed: bool,
    all_responded: bool = False,
    known_supplier_names: set[str] | None = None,
) -> AutoDecision:
    """마감된 견적을 사람 확인 없이 1순위로 확정해도 되는지.

    result는 evaluate_quotations_for_rfqs의 반환값을 그대로 받는다.
    """
    rules = _rules()
    mode = rules.automation_mode
    enabled = bool(rules.auto_final_selection)
    ranking = [row for row in (result.get("ranking") or []) if isinstance(row, dict)]
    parse_failed = result.get("parse_failed") or []
    spec = result.get("specification_evaluation") or {}
    checks: list[dict[str, Any]] = []

    if deadline_passed or all_responded:
        checks.append(_passed(
            "DEADLINE",
            "마감 경과 또는 전원 회신",
            "전원이 회신했습니다" if all_responded and not deadline_passed else "견적 마감 시각이 지났습니다",
        ))
    else:
        checks.append(_blocked("DEADLINE", "마감 경과 또는 전원 회신", "아직 마감 전이고 회신도 다 오지 않았습니다"))

    if not ranking:
        checks.append(_blocked("HAS_RANKING", "비교 가능한 견적", "순위에 오른 견적이 없습니다"))
    else:
        checks.append(_passed("HAS_RANKING", "비교 가능한 견적", f"{len(ranking)}건이 순위에 올랐습니다"))

    if parse_failed:
        checks.append(_blocked(
            "PARSE_FAILED",
            "견적서 전부 판독 성공",
            f"읽지 못한 견적서가 {len(parse_failed)}건 있어 원본 확인이 필요합니다",
        ))
    else:
        checks.append(_passed("PARSE_FAILED", "견적서 전부 판독 성공", "파싱 실패 0건"))

    competition = int(result.get("competition_count") or len(ranking))
    minimum = int(rules.auto_selection_min_quotations)
    if result.get("single_bid") or competition < minimum:
        checks.append(_blocked(
            "MIN_COMPETITION",
            f"경쟁 견적 {minimum}건 이상",
            f"유효한 견적이 {competition}건뿐이라 가격을 비교할 수 없습니다"
            if result.get("single_bid")
            else f"유효한 견적이 {competition}건으로 최소 {minimum}건에 못 미칩니다",
        ))
    else:
        checks.append(_passed(
            "MIN_COMPETITION",
            f"경쟁 견적 {minimum}건 이상",
            f"유효한 견적 {competition}건",
        ))

    spec_status = str(spec.get("status") or "")
    if spec_status == "completed":
        checks.append(_passed("SPEC_EVALUATION", "규격 평가 완료", f"{spec.get('model') or 'AI'} 평가 완료"))
    elif spec_status:
        checks.append(_blocked(
            "SPEC_EVALUATION",
            "규격 평가 완료",
            f"규격 평가가 끝나지 않았습니다 (미평가 {len(spec.get('unevaluated') or [])}건)",
        ))
    else:
        checks.append(_unknown("SPEC_EVALUATION", "규격 평가 완료", "규격 평가 상태를 확인할 수 없습니다"))

    top = ranking[0] if ranking else {}
    penalties = [row for row in (top.get("penalties") or []) if isinstance(row, dict)]
    blocking_penalties = [row for row in penalties if row.get("requires_confirmation")]
    if blocking_penalties:
        labels = ", ".join(str(row.get("label") or row.get("code")) for row in blocking_penalties)
        checks.append(_blocked("TOP_PENALTY", "1순위 감점 항목 없음", f"확인이 필요한 감점이 있습니다 ({labels})"))
    else:
        checks.append(_passed("TOP_PENALTY", "1순위 감점 항목 없음", "확인이 필요한 감점 없음"))

    # ⚠️ 유효기간이 지난 견적을 자동으로 고르면 안 된다. final_selection이
    # 뒤늦게 막아주긴 하지만, 그 전에 견적을 이미 확정(Submit)해버린 뒤라
    # 되돌리기 어렵다. 조건 단계에서 걸러야 한다.
    valid_till_raw = str(top.get("valid_till") or "").strip()
    if not valid_till_raw:
        checks.append(_passed("QUOTATION_VALIDITY", "견적 유효기간", "유효기간이 지정되지 않았습니다"))
    else:
        try:
            valid_till = date.fromisoformat(valid_till_raw[:10])
        except ValueError:
            checks.append(_unknown(
                "QUOTATION_VALIDITY", "견적 유효기간",
                f"유효기간을 읽을 수 없습니다({valid_till_raw})",
            ))
        else:
            if valid_till < date.today():
                checks.append(_blocked(
                    "QUOTATION_VALIDITY", "견적 유효기간",
                    f"1순위 견적의 유효기간({valid_till})이 지났습니다",
                ))
            else:
                checks.append(_passed(
                    "QUOTATION_VALIDITY", "견적 유효기간", f"{valid_till}까지 유효합니다",
                ))

    top_supplier_name = str(top.get("supplier") or top.get("supplier_name") or "").strip()
    if ranking and not top_supplier_name:
        checks.append(_blocked(
            "TOP_SUPPLIER_NAME", "1순위 협력사 확인",
            "1순위 견적에 협력사 이름이 없어 자동으로 고를 수 없습니다",
        ))

    gap = score_gap(ranking)
    threshold = float(rules.auto_selection_score_gap)
    if gap is None:
        checks.append(_unknown("SCORE_GAP", f"1-2위 점수차 {threshold:g}점 이상", "비교할 2순위가 없습니다"))
    elif gap < threshold:
        checks.append(_blocked(
            "SCORE_GAP",
            f"1-2위 점수차 {threshold:g}점 이상",
            f"1순위와 2순위가 {gap:g}점 차이로 박빙입니다",
        ))
    else:
        checks.append(_passed(
            "SCORE_GAP",
            f"1-2위 점수차 {threshold:g}점 이상",
            f"1순위가 2순위보다 {gap:g}점 높습니다",
        ))

    limit = Decimal(str(rules.auto_selection_max_amount))
    amount = _amount(top.get("total_amount") or top.get("grand_total"))
    if amount is None:
        checks.append(_unknown("AMOUNT_LIMIT", "금액 상한 이내", "1순위 견적 금액을 확인할 수 없습니다"))
    elif amount > limit:
        checks.append(_blocked(
            "AMOUNT_LIMIT",
            "금액 상한 이내",
            f"선정 금액 {amount:,.0f}원이 상한 {limit:,.0f}원을 넘습니다",
        ))
    else:
        checks.append(_passed("AMOUNT_LIMIT", "금액 상한 이내", f"선정 금액 {amount:,.0f}원"))

    known = {str(name or "").strip() for name in (known_supplier_names or set())}
    known.discard("")
    top_supplier = top_supplier_name
    if not known:
        checks.append(_passed("KNOWN_SUPPLIER", "거래 이력 있는 협력사", "거래 이력 확인을 생략했습니다"))
    elif top_supplier and top_supplier not in known:
        checks.append(_blocked(
            "KNOWN_SUPPLIER",
            "거래 이력 있는 협력사",
            f"{top_supplier}은(는) 거래 이력이 없는 신규 협력사입니다",
        ))
    else:
        checks.append(_passed("KNOWN_SUPPLIER", "거래 이력 있는 협력사", f"{top_supplier or '1순위'} 거래 이력 확인"))

    allowed = all(row["status"] == "passed" for row in checks)
    return AutoDecision(
        allowed=allowed,
        mode=mode,
        enabled=enabled,
        node="auto_final_selection",
        checks=checks,
        evidence={
            "competition_count": competition,
            "score_gap": gap,
            "top_supplier": top_supplier or None,
            "top_quotation_id": str(top.get("quotation_id") or top.get("name") or "") or None,
            "top_score": top.get("overall_score"),
            "top_amount": str(amount) if amount is not None else None,
            "parse_failed_count": len(parse_failed),
            "specification_status": spec_status or None,
        },
    )
