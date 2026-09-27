"""지금 최종 선정을 판정해도 되는가 - 자동화 v2의 5단계 c.

**마감 시각은 "판정 시점"이 아니라 "판정을 시작해도 되는 조건"이다.**

  마감이 지났고 ＋ 마감 전에 제출된 견적이 전부 처리 완료(파싱 + 규격 평가)
  됐을 때 판정한다.

마감만 보고 판정하면, 마감 1분 전에 들어온 견적의 규격 평가가 아직 도는 중인데
그 견적을 빼고 순위를 매긴다. 반대로 처리 완료만 보면, 아직 낼 시간이 남은
협력사가 있는데 먼저 닫아버린다.

⚠️ 여기서는 RunPod을 돌리지 않는다. 캐시에 결과가 있는지만 본다. 이 판단이
그래프 잠금을 오래 잡으면 v1처럼 서버 전체가 그 앞에서 밀린다. 실제 평가는
판정 가능이 된 뒤에 그래프 안에서 돌고, 그때는 캐시가 이미 차 있어서 빠르다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any

from backend_logic2.repositories import quotation_submissions as submission_repository
from backend_logic2.services.auto_progress import KST

LOGGER = logging.getLogger(__name__)

# 사람이 날짜만 고른 마감(시각 없음)은 그날 18:00으로 본다. 자동 발송이
# 정하는 마감과 같은 규칙이다.
DEFAULT_DEADLINE_TIME = time(18, 0)


@dataclass
class Readiness:
    """판정을 시작해도 되는가, 아니면 무엇을 기다리는가."""

    ready: bool
    reason: str
    deadline: datetime | None = None
    # 마감 후에 제출된 견적. 순위에서 빼야 한다.
    late: list[dict[str, Any]] = field(default_factory=list)
    # 마감 전에 제출됐는데 아직 처리가 안 끝난 견적. 이것 때문에 기다린다.
    pending: list[dict[str, Any]] = field(default_factory=list)
    # 제출 시각을 모르는 견적. 버리지 않고 마감 전 제출로 본다.
    unknown: list[dict[str, Any]] = field(default_factory=list)
    responded: int = 0
    recipients: int = 0

    @property
    def waiting_for_processing(self) -> bool:
        """사람이 할 일은 없고 시간이 해결하는 대기인가."""
        return not self.ready and bool(self.pending)

    def as_payload(self) -> dict[str, Any]:
        """화면이 "무엇을 기다리는지" 그대로 보여줄 수 있게."""
        return {
            "ready": self.ready,
            "reason": self.reason,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "late": self.late,
            "pending": self.pending,
            "unknown_submission_time": self.unknown,
            "responded": self.responded,
            "recipients": self.recipients,
        }


def parse_deadline(value: Any) -> datetime | None:
    """견적 마감 시각을 KST aware datetime으로 읽는다.

    화면은 `2026-10-02T18:00:00+09:00` 형태로 보낸다. 날짜만 있는 옛 값은
    그날 18:00으로 본다 - 자정으로 읽으면 마감이 하루 앞당겨진다.
    """
    parsed = submission_repository.parse_erp_datetime(value)
    if parsed is None:
        return None
    text = str(value).strip() if not isinstance(value, datetime) else ""
    if len(text) == 10:
        return parsed.replace(
            hour=DEFAULT_DEADLINE_TIME.hour, minute=DEFAULT_DEADLINE_TIME.minute
        )
    return parsed


def _is_processed(item: dict[str, Any]) -> bool:
    """이 견적의 처리가 끝났는가(더 기다려도 바뀌지 않는가).

    - 읽기 실패(parse_failed)는 끝난 것이다. 기다려도 읽히지 않는다.
      순위에서 빠지는 건 판정기가 사람을 부를 이유로 따로 본다.
    - 다른 라운드 견적(rfq_mismatch)은 이 판정과 무관하다.
    - 순위 대상은 규격 평가가 캐시에 있어야 끝난 것이다.
    - ⚠️ spec_evaluated가 None이면 평가기 자체를 만들지 못한 것이다. 기다려도
      영원히 안 끝나므로 끝난 것으로 보고 넘긴다. 그다음은 판정기가
      규격 평가 상태를 보고 사람을 부른다(조용히 멈추지 않게).
    """
    kind = str(item.get("kind") or "")
    if kind != "candidate":
        return True
    return item.get("spec_evaluated") is not False


def judge(
    case: dict[str, Any],
    *,
    now: datetime | None = None,
    validation: dict[str, Any] | None = None,
) -> Readiness:
    """이 케이스를 지금 판정해도 되는지 본다."""
    from backend_logic2.services import quotation_service

    now = now or datetime.now(KST)
    snapshot = case.get("workflow_snapshot") or {}
    values = snapshot.get("values") if isinstance(snapshot, dict) else {}
    values = values if isinstance(values, dict) else {}
    deadline = parse_deadline(values.get("quotation_deadline"))

    quotation_snapshot = case.get("quotation_snapshot") or {}
    quotation_snapshot = quotation_snapshot if isinstance(quotation_snapshot, dict) else {}
    recipients = int(quotation_snapshot.get("recipient_count") or 0)
    responded = int(quotation_snapshot.get("responded_count") or 0)
    all_responded = recipients > 0 and responded >= recipients

    if deadline is None:
        return Readiness(
            ready=False,
            reason="견적 마감 시각이 정해지지 않았습니다",
            responded=responded,
            recipients=recipients,
        )

    after_deadline = now >= deadline
    stamp = deadline.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    if not after_deadline and not all_responded:
        # ⚠️ 여기서 끝낸다. 아직 낼 시간이 남은 협력사가 있는데 ERPNext를
        # 뒤질 이유가 없다(스캔이 60초마다 도는데 그때마다 견적을 전부
        # 조회하면 ERPNext가 먼저 죽는다).
        return Readiness(
            ready=False,
            reason=f"마감 전입니다 (마감 {stamp}, 회신 {responded}/{recipients}건)",
            deadline=deadline,
            responded=responded,
            recipients=recipients,
        )

    if validation is None:
        validation = quotation_service.validate_case_quotations(case)
    submitted = submission_repository.submitted_at_by_quotation(_rfq_names(values))

    late: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for item in validation.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "") == "rfq_mismatch":
            continue
        quotation_id = str(item.get("quotation_id") or "").strip()
        row = {
            "quotation_id": quotation_id,
            "supplier_name": item.get("supplier_name"),
        }
        submitted_at = submitted.get(quotation_id)
        if submitted_at is None:
            # 모르는 건 버리지 않는다. 마감 전 제출로 보고 처리 여부만 따진다.
            unknown.append(row)
        elif submitted_at > deadline:
            late.append({**row, "submitted_at": submitted_at.isoformat()})
            continue
        if not _is_processed(item):
            pending.append(row)

    if pending:
        names = ", ".join(
            str(row.get("supplier_name") or row.get("quotation_id")) for row in pending[:3]
        )
        return Readiness(
            ready=False,
            reason=(
                f"마감 전 제출 견적 {len(pending)}건의 규격 평가가 끝나지 않았습니다"
                f" ({names}) - 끝나면 자동으로 다시 확인합니다"
            ),
            deadline=deadline,
            late=late,
            pending=pending,
            unknown=unknown,
            responded=responded,
            recipients=recipients,
        )

    if after_deadline:
        reason = f"마감({stamp})이 지났고 마감 전 제출 견적의 처리가 끝났습니다"
    else:
        reason = f"협력사 {recipients}곳이 전부 회신하고 처리가 끝났습니다"
    if late:
        reason = f"{reason}. 마감 후 제출 {len(late)}건은 제외합니다"
    return Readiness(
        ready=True,
        reason=reason,
        deadline=deadline,
        late=late,
        pending=pending,
        unknown=unknown,
        responded=responded,
        recipients=recipients,
    )


def _rfq_names(values: dict[str, Any]) -> list[str]:
    """현재 라운드와 지난 라운드 RFQ 이름 전부."""
    names: list[str] = []
    for entry in values.get("rfq_rounds") or []:
        if isinstance(entry, dict):
            name = str(entry.get("rfq_name") or "").strip()
            if name:
                names.append(name)
    current = str(values.get("rfq_name") or "").strip()
    if current:
        names.append(current)
    return list(dict.fromkeys(names))


__all__ = ["DEFAULT_DEADLINE_TIME", "Readiness", "judge", "parse_deadline"]
