"""자동 진행 조건 평가 - "사람을 부를 이유"가 있는지 판정한다.

설계 원칙은 하나다. **판단이 애매하면 사람에게 넘긴다.**
RFQ 발송도 최종 선정도 외부로 메일이 나가는, 되돌리기 어려운 행동이다.
그래서 조건을 "자동으로 갈 이유"가 아니라 "사람을 부를 이유"(blocker)로
적고, 하나라도 걸리면 멈춘다. 값을 알 수 없을 때도 통과가 아니라 정지다.

여기서 반환하는 checks는 사람이 그대로 읽을 수 있어야 한다. 서버 로그를 볼
수 없는 환경이라, "왜 멈췄지"의 답이 전부 화면(AI 판단 기록)에 남아야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from backend_logic2.policies.runtime import current_policy

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
            return "자동 진행이 꺼져 있어 사람이 확인합니다."
        if self.allowed:
            passed = len([row for row in self.checks if row["status"] == "passed"])
            if self.mode == "shadow":
                return f"조건 {passed}개를 모두 통과했습니다 (기록만, 실제 진행은 안 함)."
            return f"조건 {passed}개를 모두 통과해 자동으로 진행합니다."
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
