"""판정 시점 판단 - 자동화 v2의 5단계 c.

**마감 시각은 "판정 시점"이 아니라 "판정을 시작해도 되는 조건"이다.**

  마감이 지났고 ＋ 마감 전에 제출된 견적이 전부 처리 완료됐을 때 판정한다.

마감만 보면 마감 1분 전에 온 견적의 규격 평가가 도는 중인데 그걸 빼고 순위를
매긴다. 처리 완료만 보면 아직 낼 시간이 남은 협력사를 먼저 닫아버린다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend_logic2.services import deadline_readiness

KST = ZoneInfo("Asia/Seoul")
DEADLINE = "2026-10-02T18:00:00+09:00"
BEFORE = datetime(2026, 10, 2, 17, 0, tzinfo=KST)
AFTER = datetime(2026, 10, 2, 18, 5, tzinfo=KST)


def _case(*, deadline=DEADLINE, recipients=3, responded=2, rounds=None):
    return {
        "case_id": "CASE-1",
        "mr_name": "MAT-MR-2026-00300",
        "workflow_snapshot": {
            "values": {
                "rfq_name": "PUR-RFQ-0002",
                "quotation_deadline": deadline,
                "rfq_rounds": rounds or [],
            }
        },
        "quotation_snapshot": {
            "recipient_count": recipients,
            "responded_count": responded,
        },
    }


def _item(quotation_id="SQ-1", *, kind="candidate", spec=True, supplier="동관컴퍼니"):
    return {
        "quotation_id": quotation_id,
        "supplier_name": supplier,
        "kind": kind,
        "spec_evaluated": spec,
    }


@pytest.fixture
def submitted(monkeypatch):
    """견적 문서명 -> 제출 시각. 5b가 기록한 값을 대신한다."""
    state: dict[str, datetime] = {}
    monkeypatch.setattr(
        deadline_readiness.submission_repository,
        "submitted_at_by_quotation",
        lambda names: dict(state),
    )
    return state


def _judge(case, *, now, items, submitted=None):
    return deadline_readiness.judge(
        case, now=now, validation={"items": items}
    )


# ---------------------------------------------------------------------------
# 마감 시각 읽기
# ---------------------------------------------------------------------------


def test_the_deadline_is_read_with_its_offset() -> None:
    assert deadline_readiness.parse_deadline(DEADLINE) == datetime(
        2026, 10, 2, 18, 0, tzinfo=KST
    )


def test_a_date_only_deadline_means_six_pm_not_midnight() -> None:
    """⚠️ 자정으로 읽으면 마감이 하루 앞당겨져서 멀쩡한 견적이 전부 늦은 게 된다."""
    assert deadline_readiness.parse_deadline("2026-10-02") == datetime(
        2026, 10, 2, 18, 0, tzinfo=KST
    )


@pytest.mark.parametrize("value", [None, "", "언젠가"])
def test_an_unreadable_deadline_is_unknown(value) -> None:
    assert deadline_readiness.parse_deadline(value) is None


def test_no_deadline_means_never_ready(submitted) -> None:
    """마감을 모르면 자동으로 닫지 않는다."""
    readiness = _judge(_case(deadline=None), now=AFTER, items=[_item()])

    assert readiness.ready is False
    assert "마감 시각이 정해지지 않았" in readiness.reason


# ---------------------------------------------------------------------------
# 마감 전
# ---------------------------------------------------------------------------


def test_before_the_deadline_it_waits(submitted) -> None:
    readiness = _judge(_case(), now=BEFORE, items=[_item()])

    assert readiness.ready is False
    assert "마감 전입니다" in readiness.reason
    assert "회신 2/3건" in readiness.reason


def test_before_the_deadline_it_does_not_touch_erpnext(monkeypatch, submitted) -> None:
    """⚠️ 스캔은 60초마다 돈다. 대기 중인 건마다 견적을 전부 조회하면
    ERPNext가 먼저 죽는다."""
    from backend_logic2.services import quotation_service

    monkeypatch.setattr(
        quotation_service, "validate_case_quotations",
        lambda case: pytest.fail("마감 전인데 견적을 조회했다"),
    )

    assert deadline_readiness.judge(_case(), now=BEFORE).ready is False


def test_everyone_answering_opens_the_judgment_early(submitted) -> None:
    readiness = _judge(
        _case(recipients=2, responded=2), now=BEFORE,
        items=[_item("SQ-1"), _item("SQ-2")],
    )

    assert readiness.ready is True
    assert "전부 회신" in readiness.reason


def test_everyone_answering_still_waits_for_processing(submitted) -> None:
    readiness = _judge(
        _case(recipients=2, responded=2), now=BEFORE,
        items=[_item("SQ-1"), _item("SQ-2", spec=False)],
    )

    assert readiness.ready is False
    assert readiness.waiting_for_processing is True


# ---------------------------------------------------------------------------
# 마감 후 - 처리 완료를 기다린다
# ---------------------------------------------------------------------------


def test_after_the_deadline_with_everything_processed_it_is_ready(submitted) -> None:
    readiness = _judge(_case(), now=AFTER, items=[_item()])

    assert readiness.ready is True
    assert "마감" in readiness.reason


def test_a_running_spec_evaluation_holds_the_judgment(submitted) -> None:
    """마감 1분 전에 온 견적의 평가가 도는 중이면 그 견적을 빼고 순위를
    매기면 안 된다."""
    readiness = _judge(_case(), now=AFTER, items=[_item(spec=False)])

    assert readiness.ready is False
    assert readiness.waiting_for_processing is True
    assert "규격 평가가 끝나지 않았" in readiness.reason
    assert "자동으로 다시 확인" in readiness.reason


def test_an_unreadable_quotation_does_not_hold_the_judgment(submitted) -> None:
    """읽기 실패는 기다려도 읽히지 않는다. 판정기가 사람을 부를 이유로 본다.

    ⚠️ 읽지 못한 견적에는 규격 평가 결과가 있을 수 없다(spec_evaluated=False).
    그래서 kind를 보지 않고 평가 여부만 보면 이 건에서 영원히 기다린다.
    """
    readiness = _judge(
        _case(), now=AFTER, items=[_item(kind="parse_failed", spec=False)]
    )

    assert readiness.ready is True
    assert readiness.pending == []


def test_a_missing_evaluator_does_not_hold_the_judgment_forever(submitted) -> None:
    """⚠️ spec_evaluated=None은 평가기를 아예 못 만든 것이다. 기다려도 영원히
    안 끝난다. 여기서 막으면 아무 표시 없이 멈춰 있는다."""
    readiness = _judge(_case(), now=AFTER, items=[_item(spec=None)])

    assert readiness.ready is True


def test_another_rounds_quotation_is_not_counted(submitted) -> None:
    readiness = _judge(
        _case(), now=AFTER,
        items=[_item(), _item("SQ-9", kind="rfq_mismatch", spec=False)],
    )

    assert readiness.ready is True


def test_no_response_at_all_is_ready_so_a_person_gets_called(submitted) -> None:
    """회신 0건은 자동 연장하지 않는다. 판정을 열어서 판정기가 사람을 부른다."""
    readiness = _judge(_case(responded=0), now=AFTER, items=[])

    assert readiness.ready is True


# ---------------------------------------------------------------------------
# 마감 후 제출 - 5b가 기록한 시각으로 가른다
# ---------------------------------------------------------------------------


def test_a_submission_after_the_deadline_is_marked_late(submitted) -> None:
    submitted["SQ-2"] = datetime(2026, 10, 2, 18, 1, tzinfo=KST)
    submitted["SQ-1"] = datetime(2026, 10, 2, 17, 59, tzinfo=KST)

    readiness = _judge(_case(), now=AFTER, items=[_item("SQ-1"), _item("SQ-2")])

    assert [row["quotation_id"] for row in readiness.late] == ["SQ-2"]
    assert readiness.ready is True
    assert "마감 후 제출 1건은 제외" in readiness.reason


def test_a_reply_sent_in_time_but_processed_late_is_kept(submitted) -> None:
    """이 단계가 존재하는 이유. 17:59에 낸 견적을 18:04에 등록했어도 받는다."""
    submitted["SQ-1"] = datetime(2026, 10, 2, 17, 59, tzinfo=KST)

    readiness = _judge(_case(), now=AFTER, items=[_item("SQ-1")])

    assert readiness.late == []
    assert readiness.ready is True


def test_a_late_submission_does_not_hold_the_judgment(submitted) -> None:
    """⚠️ 버릴 견적의 규격 평가를 기다리면 영원히 판정이 시작되지 않는다."""
    submitted["SQ-2"] = datetime(2026, 10, 2, 18, 1, tzinfo=KST)

    readiness = _judge(
        _case(), now=AFTER, items=[_item("SQ-1"), _item("SQ-2", spec=False)],
    )

    assert readiness.ready is True
    assert readiness.pending == []


def test_an_unknown_submission_time_is_not_thrown_away(submitted) -> None:
    """모르는 건 버리지 않는다. 5b 이전에 들어온 견적이 그렇다."""
    readiness = _judge(_case(), now=AFTER, items=[_item("SQ-1")])

    assert readiness.late == []
    assert [row["quotation_id"] for row in readiness.unknown] == ["SQ-1"]
    assert readiness.ready is True


def test_an_exactly_on_time_submission_is_kept(submitted) -> None:
    submitted["SQ-1"] = datetime(2026, 10, 2, 18, 0, tzinfo=KST)

    readiness = _judge(_case(), now=AFTER, items=[_item("SQ-1")])

    assert readiness.late == []


def test_the_verdict_carries_what_the_screen_needs(submitted) -> None:
    submitted["SQ-2"] = datetime(2026, 10, 2, 18, 1, tzinfo=KST)
    payload = _judge(
        _case(), now=AFTER, items=[_item("SQ-1"), _item("SQ-2")]
    ).as_payload()

    assert payload["ready"] is True
    assert payload["deadline"] == "2026-10-02T18:00:00+09:00"
    assert payload["late"][0]["quotation_id"] == "SQ-2"
    assert payload["responded"] == 2 and payload["recipients"] == 3


def test_past_rounds_submission_times_are_looked_up_too(monkeypatch) -> None:
    """재비딩한 건은 지난 라운드 견적도 후보로 남는다."""
    asked: list[list[str]] = []
    monkeypatch.setattr(
        deadline_readiness.submission_repository, "submitted_at_by_quotation",
        lambda names: asked.append(names) or {},
    )

    _judge(
        _case(rounds=[{"round": 0, "rfq_name": "PUR-RFQ-0001"}]),
        now=AFTER, items=[_item()],
    )

    assert asked[0] == ["PUR-RFQ-0001", "PUR-RFQ-0002"]


def test_the_deadline_boundary_uses_the_recorded_time_not_now(submitted) -> None:
    """지금 시각으로 가르면, 스캔이 늦게 돈 만큼 멀쩡한 견적이 늦은 게 된다."""
    submitted["SQ-1"] = datetime(2026, 10, 2, 17, 59, tzinfo=KST)

    readiness = _judge(
        _case(), now=AFTER + timedelta(hours=3), items=[_item("SQ-1")]
    )

    assert readiness.late == []
