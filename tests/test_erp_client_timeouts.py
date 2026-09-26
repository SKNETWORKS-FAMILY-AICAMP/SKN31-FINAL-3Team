"""ERPNext 호출은 반드시 timeout을 갖는다.

왜 테스트로 못 박는가: timeout 없는 requests 호출은 무한정 기다린다. 그런
호출 하나가 스레드를 영구히 묶고, 몇 개 쌓이면 API 스레드풀이 고갈돼서
관계없는 화면까지 전부 응답을 못 한다. 실제로 이 사고를 겪었고, 원인은
호출 20곳 중 18곳에 timeout이 빠져 있던 것이었다. 사람이 매번 기억하는
방식으로는 또 빠뜨리므로 여기서 막는다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import requests

from backend_logic2.integrations import erp_client


_SOURCE = Path(erp_client.__file__).read_text(encoding="utf-8")


def test_no_call_bypasses_the_timeout_enforcing_session() -> None:
    """requests.get/post/... 을 직접 쓰면 timeout이 빠질 수 있다."""
    direct = re.findall(r"\brequests\.(get|post|put|delete|patch)\(", _SOURCE)

    assert direct == [], (
        "erp_client는 requests를 직접 호출하지 않고 _SESSION을 써야 합니다. "
        f"직접 호출: {direct}"
    )


_ERP_CALLERS = [
    "backend_logic2/nodes/item/item_validation.py",
    "backend_logic2/nodes/rfq/check_rfq_connection.py",
    "backend_logic2/nodes/rfq/send_rfq.py",
    "backend_logic2/nodes/supplier/onboarding.py",
    "backend_logic2/nodes/supplier/register_candidate_suppliers.py",
]


@pytest.mark.parametrize("module_path", _ERP_CALLERS)
def test_nodes_calling_erpnext_directly_also_use_the_session(module_path: str) -> None:
    """노드에서 ERPNext를 직접 부르는 곳도 같은 세션을 써야 한다."""
    source = Path(module_path).read_text(encoding="utf-8")
    direct = re.findall(r"\brequests\.(get|post|put|delete|patch)\(", source)

    assert direct == [], f"{module_path}가 requests를 직접 호출합니다: {direct}"


def test_a_call_without_a_timeout_gets_the_default() -> None:
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": []}

    def fake_request(self, method, url, **kwargs):
        captured.update(kwargs)
        return FakeResponse()

    original = requests.Session.request
    requests.Session.request = fake_request
    try:
        erp_client.erp_get("Material Request")
    finally:
        requests.Session.request = original

    assert captured["timeout"] == erp_client._DEFAULT_TIMEOUT


def test_a_caller_supplied_timeout_is_respected() -> None:
    """첨부파일 다운로드처럼 오래 걸리는 호출은 자기 값을 쓴다."""
    captured = {}

    def fake_request(self, method, url, **kwargs):
        captured.update(kwargs)
        raise AssertionError("여기까지 오면 충분하다")

    original = requests.Session.request
    requests.Session.request = fake_request
    try:
        with pytest.raises(AssertionError):
            erp_client._SESSION.get("http://example.invalid", timeout=60)
    finally:
        requests.Session.request = original

    assert captured["timeout"] == 60


def test_a_timeout_becomes_a_readable_erpnext_error() -> None:
    """requests.Timeout이 그대로 올라가면 화면에 정체불명의 500으로 보인다."""

    def fake_request(self, method, url, **kwargs):
        raise requests.Timeout("timed out")

    original = requests.Session.request
    requests.Session.request = fake_request
    try:
        with pytest.raises(erp_client.ERPNextAPIError) as caught:
            erp_client.erp_get_one("Material Request", "MAT-MR-2026-00001")
    finally:
        requests.Session.request = original

    assert "응답이 없습니다" in str(caught.value)


def test_a_connection_failure_becomes_a_readable_erpnext_error() -> None:
    def fake_request(self, method, url, **kwargs):
        raise requests.ConnectionError("refused")

    original = requests.Session.request
    requests.Session.request = fake_request
    try:
        with pytest.raises(erp_client.ERPNextAPIError) as caught:
            erp_client.erp_get("Item")
    finally:
        requests.Session.request = original

    assert "연결할 수 없습니다" in str(caught.value)
