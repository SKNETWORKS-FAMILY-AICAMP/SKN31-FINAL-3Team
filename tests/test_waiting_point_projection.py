"""멈춰 선 지점에서 단계를 도출한다 - 어느 경로로 왔든 같게.

⚠️ 왜 이 파일이 있나: status는 직전 노드가 남긴 값이다. 사람을 기다리는
노드는 interrupt()로 멈추는데 LangGraph는 노드가 Command를 반환할 때만
state를 갱신하므로, 그 노드는 자기 상태를 남길 방법이 없다. 그래서 같은
대기 지점인데도 "어떤 경로로 왔는지"에 따라 단계가 달라졌다.

실제로 겪은 것: 기존 협력사 풀로 충분해서 select_rfq_targets로 바로 오면
단계가 '협력사 탐색'에 머문 채 사람을 기다렸고, 화면은 그 단계에서 버튼을
막아서 아무것도 누를 수 없었다. 신규 탐색 경로는 멀쩡했다.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from backend_logic2.services.workflow_projection import (
    INTERRUPT_TASK_STAGE,
    project_waiting_point,
)


_COMMANDS = Path("backend_logic2/workflow/process_commands.py")


def _interrupt_types_in_source() -> set[str]:
    """그래프가 실제로 사람을 기다릴 때 쓰는 payload type을 소스에서 뽑는다."""
    source = io.open(_COMMANDS, encoding="utf-8").read()
    types: set[str] = set()
    for match in re.finditer(r"interrupt\(\s*\{", source):
        # 이 interrupt 호출의 payload 안에서 "type": "..." 을 찾는다.
        depth = 0
        start = source.index("{", match.start())
        for i in range(start, len(source)):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    break
        payload = source[start:i]
        found = re.search(r'"type"\s*:\s*"([^"]+)"', payload)
        if found:
            types.add(found.group(1))
    return types


def test_every_waiting_point_in_the_graph_has_a_stage() -> None:
    """새 대기 지점을 추가하고 표에 안 넣으면 조용히 옛 동작으로 돌아간다."""
    missing = sorted(_interrupt_types_in_source() - set(INTERRUPT_TASK_STAGE))

    assert missing == [], (
        "이 대기 지점들이 INTERRUPT_TASK_STAGE에 없습니다. 표에 추가하세요: "
        f"{missing}"
    )


def test_the_source_actually_yielded_waiting_points() -> None:
    """추출이 망가지면 위 테스트가 빈 집합을 비교하며 항상 통과한다."""
    found = _interrupt_types_in_source()

    assert len(found) >= 8, f"대기 지점을 제대로 못 찾았습니다: {found}"
    assert "select_rfq_targets" in found


def test_waiting_at_rfq_targets_reports_that_stage_whatever_the_status_was() -> None:
    """기존 풀 경로로 와서 status가 뒤처져 있어도 단계는 대기 지점 기준이다."""
    result = project_waiting_point([{"type": "select_rfq_targets"}])

    assert result == ("WAITING_INPUT", "RFQ_TARGET_SELECTION")


@pytest.mark.parametrize(
    ("task_type", "stage"),
    [
        ("substitute_selection", "SUBSTITUTE_DECISION"),
        ("check_quotations", "QUOTATION_COLLECTION"),
        ("final_selection", "SUPPLIER_SELECTION"),
        ("order_start", "ORDER_START"),
        ("po_approval", "PRE_PO_APPROVAL"),
        ("pr_request", "PR_REQUEST"),
        ("supplier_pr_response", "PR_RESPONSE_WAITING"),
        ("pr_rejection_review", "PR_REJECTED"),
        ("po_creation_failed", "PO_CREATION_FAILED"),
    ],
)
def test_each_waiting_point_maps_to_its_own_stage(task_type: str, stage: str) -> None:
    assert project_waiting_point([{"type": task_type}]) == ("WAITING_INPUT", stage)


def test_nothing_waiting_means_no_override() -> None:
    """돌고 있는 중에는 status 기반 판정을 그대로 쓴다."""
    assert project_waiting_point([]) is None


def test_an_unknown_waiting_point_does_not_invent_a_stage() -> None:
    assert project_waiting_point([{"type": "something_new"}]) is None


def test_a_malformed_payload_is_ignored() -> None:
    assert project_waiting_point(["not a dict", {"no": "type"}]) is None
