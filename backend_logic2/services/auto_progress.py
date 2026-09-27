"""자동 진행 조건 평가 - "사람을 부를 이유"가 있는지 판정한다.

설계 원칙은 하나다. **판단이 애매하면 사람에게 넘긴다.**
RFQ 발송도 최종 선정도 외부로 메일이 나가는, 되돌리기 어려운 행동이다.
그래서 조건을 "자동으로 갈 이유"가 아니라 "사람을 부를 이유"(blocker)로
적고, 하나라도 걸리면 멈춘다. 값을 알 수 없을 때도 통과가 아니라 정지다.

여기서 반환하는 checks는 사람이 그대로 읽을 수 있어야 한다. 서버 로그를 볼
수 없는 환경이라, "왜 멈췄지"의 답이 전부 화면(AI 판단 기록)에 남아야 한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from backend_logic2.policies.runtime import current_policy

LOGGER = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


@dataclass
class AutoDecision:
    """조건 평가 결과.

    allowed : 조건을 모두 통과했는가(모드와 무관한 순수 판정)
    mode    : off / shadow / on
    enabled : 이 단계의 자동화 스위치가 켜져 있는가
    node    : 어느 단계의 판정인지. 화면이 지난 단계의 판정을 지금 상태로
              착각하지 않으려면 반드시 필요하다.
    """

    allowed: bool
    mode: str
    enabled: bool
    node: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def blockers(self) -> list[dict[str, Any]]:
        """사람을 불러야 하는 이유."""
        return [row for row in self.checks if row.get("status") == "blocked"]

    @property
    def waiting(self) -> list[dict[str, Any]]:
        """시간이 해결하는 이유(규격 평가가 아직 도는 중 등).

        ⚠️ 이걸 blocked와 섞으면 안 된다. 아직 안 끝난 것뿐인데 사람을 부르면
        담당자는 할 일도 없이 불려 나오고, 진짜 멈춘 건과 구분이 안 된다.
        """
        return [row for row in self.checks if row.get("status") == "waiting"]

    @property
    def needs_person(self) -> bool:
        """지금 사람을 불러야 하는가. 기다리는 중이면 아직 아니다."""
        return bool(self.blockers)

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
            return "자동 진행이 꺼져 있어 사람이 확인합니다."
        if self.allowed:
            passed = len([row for row in self.checks if row["status"] == "passed"])
            if self.mode == "shadow":
                return f"조건 {passed}개를 모두 통과했습니다 (기록만, 실제 진행은 안 함)."
            return f"조건 {passed}개를 모두 통과해 자동으로 진행합니다."
        if not self.blockers and self.waiting:
            reasons = "; ".join(row["detail"] for row in self.waiting)
            return f"아직 판정할 때가 아닙니다 - {reasons}"
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
            "needs_person": self.needs_person,
            "summary": self.summary(),
        }


def _check(code: str, label: str, detail: str, *, status: str) -> dict[str, Any]:
    return {"code": code, "label": label, "detail": detail, "status": status}


def _passed(code: str, label: str, detail: str) -> dict[str, Any]:
    return _check(code, label, detail, status="passed")


def _blocked(code: str, label: str, detail: str) -> dict[str, Any]:
    return _check(code, label, detail, status="blocked")


def _rules():
    return current_policy().rules


def auto_rfq_deadline(schedule_date: date | None, *, now: datetime | None = None) -> str | None:
    """자동 발송용 견적 마감 시각. 발송 시각 + N일의 18:00(KST).

    납기요청일이 있으면 min(N, 납기까지 남은 일수)를 쓴다. 납기가 없으면
    제약이 없으므로 N을 그대로 쓴다.

    > 비딩에 들어온 건은 납기가 최소 8일 남아 있다(납기 ≤ 7일이면 긴급으로
    > 분류돼 비딩에 오지 않는다). 납기일이 아예 없는 MR만 긴급 판정을
    > 건너뛰어 비딩에 올 수 있다 - decide_bidding의 remaining_days is None.
    """
    moment = (now or datetime.now(KST)).astimezone(KST)
    days = int(_rules().auto_rfq_deadline_days)
    if schedule_date is not None:
        remaining = (schedule_date - moment.date()).days
        days = min(days, remaining)
    if days < 1:
        return None
    target = datetime.combine(moment.date() + timedelta(days=days), time(hour=18), tzinfo=KST)
    if target <= moment:
        return None
    return target.isoformat()


def evaluate_rfq_dispatch(
    candidates: list[dict[str, Any]],
    *,
    pool_decision: dict[str, Any] | None,
    is_rebid: bool,
    deadline: str | None,
) -> AutoDecision:
    """RFQ를 사람 확인 없이 보내도 되는가.

    ⚠️ 신규 협력사를 찾아야 하는지는 여기서 다시 판정하지 않는다.
    resolve_supplier_pool이 이미 내린 결론(needs_search)을 그대로 쓴다.
    같은 질문을 두 곳에서 따로 판단하면 언젠가 갈라진다.
    """
    rules = _rules()
    mode = str(rules.automation_mode)
    enabled = bool(rules.auto_rfq_dispatch)
    checks: list[dict[str, Any]] = []

    # 1) 재비딩은 언제나 사람이 정한다. 사람이 재비딩을 고른 건 1차에서 뭔가
    #    잘못됐기 때문인데, 같은 후보에 같은 조건으로 다시 보내면 그 잘못을
    #    반복한다.
    if is_rebid:
        checks.append(_blocked(
            "REBID_ROUND", "재비딩 차수",
            "재비딩은 협력사와 마감일을 담당자가 직접 정합니다",
        ))
    else:
        checks.append(_passed("REBID_ROUND", "재비딩 차수", "1차 발송입니다"))

    # 2) 공급사 풀 판정. 신규 탐색이 필요했다면 거래한 적 없는 곳이 섞인다는
    #    뜻이므로 사람이 본다.
    if pool_decision is None:
        checks.append(_blocked(
            "SUPPLIER_POOL", "공급사 풀 판정",
            "공급사 풀 판정 결과를 확인할 수 없습니다",
        ))
    elif pool_decision.get("needs_search"):
        reasons = [str(row) for row in (pool_decision.get("reasons") or []) if str(row).strip()]
        detail = "; ".join(reasons[:3]) if reasons else "신규 협력사를 찾아야 합니다"
        checks.append(_blocked("SUPPLIER_POOL", "공급사 풀 판정", detail))
    else:
        checks.append(_passed(
            "SUPPLIER_POOL", "공급사 풀 판정", "기존 거래 협력사만으로 충분합니다",
        ))

    # 3) 사람 확인을 건너뛸 만큼 후보가 있는가.
    minimum = int(rules.auto_rfq_min_existing_suppliers)
    if len(candidates) < minimum:
        checks.append(_blocked(
            "ENOUGH_SUPPLIERS", "자동 발송 최소 협력사",
            f"협력사가 {len(candidates)}곳뿐입니다 (기준 {minimum}곳)",
        ))
    else:
        checks.append(_passed(
            "ENOUGH_SUPPLIERS", "자동 발송 최소 협력사",
            f"{len(candidates)}곳 (기준 {minimum}곳)",
        ))

    # 4) 이메일이 없으면 보낼 수가 없다.
    missing = [
        str(row.get("name") or "").strip()
        for row in candidates
        if not str(row.get("email") or "").strip()
    ]
    if missing:
        checks.append(_blocked(
            "SUPPLIER_EMAIL", "협력사 이메일",
            f"이메일이 없는 협력사가 있습니다 ({', '.join(missing[:3])})",
        ))
    else:
        checks.append(_passed("SUPPLIER_EMAIL", "협력사 이메일", "전원 이메일 확인됨"))

    # 5) 마감일을 정할 수 있는가.
    if not deadline:
        checks.append(_blocked(
            "DEADLINE", "견적 마감일",
            "납기요청일이 없거나 너무 가까워 마감일을 정할 수 없습니다",
        ))
    else:
        checks.append(_passed("DEADLINE", "견적 마감일", f"{deadline[:16].replace('T', ' ')}까지"))

    allowed = not any(row["status"] != "passed" for row in checks)
    return AutoDecision(
        allowed=allowed,
        mode=mode,
        enabled=enabled,
        node="auto_rfq_dispatch",
        checks=checks,
        evidence={
            "candidate_count": len(candidates),
            "minimum_suppliers": minimum,
            "deadline": deadline,
            "is_rebid": is_rebid,
        },
    )


def _amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _score(row: dict[str, Any]) -> float | None:
    value = row.get("overall_score")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def score_gap(ranking: list[dict[str, Any]]) -> float | None:
    """1순위와 2순위의 종합점수 차이. 비교 대상이 없으면 None."""
    if len(ranking) < 2:
        return None
    first, second = _score(ranking[0]), _score(ranking[1])
    if first is None or second is None:
        return None
    return round(first - second, 2)


def evaluate_final_selection(
    result: dict[str, Any],
    *,
    ready: bool,
    not_ready_reason: str = "",
    known_supplier_names: set[str] | None = None,
    today: date | None = None,
) -> AutoDecision:
    """1순위를 사람 확인 없이 선정해도 되는가.

    ready는 "판정을 시작해도 되는가"다 - 마감이 지났고 마감 전 제출분이 전부
    처리(파싱 + 규격 평가)됐는지. 그 판단은 호출하는 쪽이 하고, 여기서는
    "그래서 자동으로 골라도 되는가"만 본다.

    ⚠️ 여기서 걸리면 견적을 확정(Submit)하면 안 된다. 확정은 되돌리기 어렵다.
    """
    rules = _rules()
    mode = str(rules.automation_mode)
    enabled = bool(rules.auto_final_selection)
    checks: list[dict[str, Any]] = []

    ranking = [row for row in (result.get("ranking") or []) if isinstance(row, dict)]
    parse_failed = list(result.get("parse_failed") or [])
    spec = result.get("specification_evaluation") or {}
    now_date = today or datetime.now(KST).date()

    # 0) 판정할 때가 됐는가. 아직이면 사람을 부르는 게 아니라 그냥 기다린다.
    if not ready:
        checks.append(_check(
            "READY", "판정 시점",
            not_ready_reason or "아직 판정할 때가 아닙니다",
            status="waiting",
        ))
    else:
        checks.append(_passed("READY", "판정 시점", "마감이 지났고 견적 처리가 끝났습니다"))

    # 1) 회신이 있는가.
    if not ranking:
        checks.append(_blocked(
            "HAS_RANKING", "순위 산정",
            "순위를 매길 수 있는 견적이 없습니다" if parse_failed or result.get("excluded")
            else "회신한 협력사가 없습니다",
        ))
    else:
        checks.append(_passed("HAS_RANKING", "순위 산정", f"{len(ranking)}건의 순위가 매겨졌습니다"))

    # 2) 읽지 못한 견적이 있으면 비교가 공정하지 않다.
    if parse_failed:
        checks.append(_blocked(
            "PARSE_FAILED", "견적 읽기",
            f"읽지 못한 견적이 {len(parse_failed)}건 있습니다",
        ))
    else:
        checks.append(_passed("PARSE_FAILED", "견적 읽기", "모든 견적을 읽었습니다"))

    # 3) 경쟁이 성립하는가. 하한이 2라 단독 응찰은 어떤 설정으로도 못 지나간다.
    minimum = int(rules.auto_selection_min_quotations)
    competition = int(result.get("competition_count") or len(ranking))
    if result.get("single_bid") or competition < minimum:
        checks.append(_blocked(
            "MIN_COMPETITION", "경쟁 견적",
            f"비교 가능한 견적이 {competition}건입니다 (기준 {minimum}건)",
        ))
    else:
        checks.append(_passed(
            "MIN_COMPETITION", "경쟁 견적", f"{competition}건 (기준 {minimum}건)",
        ))

    # 4) 규격 평가가 끝났는가. 안 끝났으면 기다린다(사람을 부르지 않는다).
    spec_status = str(spec.get("status") or "")
    unevaluated = len(spec.get("unevaluated") or [])
    if spec_status == "completed":
        checks.append(_passed("SPEC_EVALUATION", "규격 평가", f"{spec.get('model') or 'AI'} 평가 완료"))
    elif spec_status == "failed":
        # ⚠️ failed는 "한 건도 평가되지 않았다"는 뜻이다. 평가기가 없거나
        # 못 돌았다는 것이고, 기다려도 영원히 안 끝난다. 이걸 대기로 두면
        # 아무 표시 없이 멈춰 있는다 - v1에서 제일 나빴던 증상이다.
        detail = spec.get("error") or "규격 평가가 한 건도 되지 않았습니다"
        checks.append(_blocked("SPEC_EVALUATION", "규격 평가", str(detail)))
    elif spec_status:
        checks.append(_check(
            "SPEC_EVALUATION", "규격 평가",
            f"규격 평가가 끝나지 않았습니다 (미평가 {unevaluated}건)",
            status="waiting",
        ))
    else:
        checks.append(_blocked("SPEC_EVALUATION", "규격 평가", "규격 평가 상태를 확인할 수 없습니다"))

    top = ranking[0] if ranking else {}
    top_supplier = str(top.get("supplier") or top.get("supplier_name") or "").strip()

    # 5) 1순위에 확인이 필요한 감점이 있는가.
    blocking = [
        row for row in (top.get("penalties") or [])
        if isinstance(row, dict) and row.get("requires_confirmation")
    ]
    if blocking:
        labels = ", ".join(str(row.get("label") or row.get("code")) for row in blocking)
        checks.append(_blocked("TOP_PENALTY", "1순위 감점", f"확인이 필요한 감점이 있습니다 ({labels})"))
    else:
        checks.append(_passed("TOP_PENALTY", "1순위 감점", "확인이 필요한 감점 없음"))

    # 6) 유효기간이 지난 견적을 자동으로 고르면 안 된다.
    valid_till_raw = str(top.get("valid_till") or "").strip()
    if not valid_till_raw:
        checks.append(_passed("QUOTATION_VALIDITY", "견적 유효기간", "유효기간이 지정되지 않았습니다"))
    else:
        try:
            valid_till = date.fromisoformat(valid_till_raw[:10])
        except ValueError:
            checks.append(_blocked(
                "QUOTATION_VALIDITY", "견적 유효기간",
                f"유효기간을 읽을 수 없습니다({valid_till_raw})",
            ))
        else:
            if valid_till < now_date:
                checks.append(_blocked(
                    "QUOTATION_VALIDITY", "견적 유효기간",
                    f"1순위 견적의 유효기간({valid_till})이 지났습니다",
                ))
            else:
                checks.append(_passed("QUOTATION_VALIDITY", "견적 유효기간", f"{valid_till}까지 유효합니다"))

    # 7) 1순위와 2순위가 붙어 있으면 사람이 본다.
    gap = score_gap(ranking)
    threshold = float(rules.auto_selection_score_gap)
    if gap is None:
        checks.append(_blocked("SCORE_GAP", "1·2순위 점수차", "점수차를 계산할 수 없습니다"))
    elif gap < threshold:
        checks.append(_blocked(
            "SCORE_GAP", "1·2순위 점수차",
            f"1·2순위 점수차가 {gap}점으로 기준({threshold}점)보다 작습니다",
        ))
    else:
        checks.append(_passed("SCORE_GAP", "1·2순위 점수차", f"{gap}점 (기준 {threshold}점)"))

    # 8) 금액 상한.
    amount = _amount(top.get("total_amount") or top.get("grand_total"))
    limit = Decimal(str(rules.auto_selection_max_amount))
    if amount is None:
        checks.append(_blocked("AMOUNT_LIMIT", "자동 선정 금액", "1순위 견적 금액을 확인할 수 없습니다"))
    elif amount > limit:
        checks.append(_blocked(
            "AMOUNT_LIMIT", "자동 선정 금액",
            f"선정 금액 {amount:,.0f}원이 상한({limit:,.0f}원)을 넘습니다",
        ))
    else:
        checks.append(_passed("AMOUNT_LIMIT", "자동 선정 금액", f"{amount:,.0f}원 (상한 {limit:,.0f}원)"))

    # 9) 처음 거래하는 협력사인가.
    if not top_supplier:
        checks.append(_blocked("KNOWN_SUPPLIER", "거래 이력", "1순위 협력사 이름을 확인할 수 없습니다"))
    elif known_supplier_names is None:
        checks.append(_blocked("KNOWN_SUPPLIER", "거래 이력", "거래 이력을 확인할 수 없습니다"))
    elif top_supplier not in known_supplier_names:
        years = int(rules.auto_known_supplier_years)
        checks.append(_blocked(
            "KNOWN_SUPPLIER", "거래 이력",
            f"{top_supplier}은(는) 최근 {years}년 내 확정 발주 이력이 없는 신규 협력사입니다",
        ))
    else:
        checks.append(_passed("KNOWN_SUPPLIER", "거래 이력", f"{top_supplier} 거래 이력 확인됨"))

    allowed = not any(row["status"] != "passed" for row in checks)
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
            "top_amount": float(amount) if amount is not None else None,
            "parse_failed_count": len(parse_failed),
            "specification_status": spec_status or None,
        },
    )


# 확정 발주 이력은 분 단위로 바뀌지 않는다. 판정은 그래프 잠금 안에서 도는데
# 그 안에서 ERPNext를 매번 부르면 그만큼 다른 요청이 뒤에서 기다린다.
_KNOWN_SUPPLIER_TTL_SECONDS = 300.0
_known_suppliers_cache: tuple[float, int, set[str]] | None = None


def known_supplier_names(*, today: date | None = None) -> set[str] | None:
    """최근 N년 안에 **확정 발주(Submit된 Purchase Order)** 이력이 있는 협력사.

    N은 회사 정책값(auto_known_supplier_years). 확정 발주를 기준으로 삼는 건
    실제로 거래가 성립한 곳만 "아는 협력사"로 보기 위해서다 - 견적만 받아본
    곳이나 취소된 발주는 이력이 아니다.

    ⚠️ 조회에 실패하면 빈 set이 아니라 **None**을 돌려준다. 빈 set은 "아는 곳이
    하나도 없다"는 사실이지만, None은 "모른다"다. 판정기는 모르는 걸 통과로
    보지 않는다. 이걸 빈 set으로 뭉개면 조회가 한 번 실패한 날 모든 건이
    신규 협력사로 보여 자동 진행이 전부 멈춘다.
    """
    from backend_logic2.integrations.erp_client import erp_get

    global _known_suppliers_cache

    years = int(_rules().auto_known_supplier_years)
    now = monotonic()
    cached = _known_suppliers_cache
    if (
        cached is not None
        and cached[1] == years
        and now - cached[0] < _KNOWN_SUPPLIER_TTL_SECONDS
    ):
        return set(cached[2])
    base = today or datetime.now(KST).date()
    try:
        since = base.replace(year=base.year - years)
    except ValueError:  # 2월 29일
        since = base.replace(year=base.year - years, day=28)
    try:
        rows = erp_get(
            "Purchase Order",
            filters=[
                ["docstatus", "=", 1],
                ["transaction_date", ">=", since.isoformat()],
            ],
            fields=["supplier", "supplier_name"],
            limit=2000,
        ) or []
    except Exception:  # noqa: BLE001 - 모르는 건 모른다고 한다
        LOGGER.warning("확정 발주 이력 조회 실패", exc_info=True)
        return None
    names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("supplier", "supplier_name"):
            value = str(row.get(key) or "").strip()
            if value:
                names.add(value)
    _known_suppliers_cache = (now, years, set(names))
    return names


def record_decision(case_id: str | None, decision: AutoDecision) -> None:
    """판정을 AI 판단 기록에 남긴다.

    자동으로 갔든 멈췄든 "왜 그랬는지"가 화면에 남아야 한다. 서버 로그를 볼
    수 없는 환경이라 이게 유일한 설명 수단이다.
    """
    if not case_id:
        return
    try:
        from backend_logic2.nodes.supplier.tools.case_logging import log_ai_decision

        detail = decision.summary()
        facts = ", ".join(
            f"{key}={value}" for key, value in decision.evidence.items() if value is not None
        )
        if facts:
            detail = f"{detail} [{facts}]"
        blocked = [row["code"] for row in decision.blockers]
        if blocked:
            detail = f"{detail} (걸린 조건: {', '.join(blocked)})"
        log_ai_decision(str(case_id), decision.node, f"[{decision.mode}] {detail}")
    except Exception:  # noqa: BLE001 - 기록 실패가 진행을 막으면 안 된다
        pass
