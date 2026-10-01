"""홈페이지 찾기의 Tavily 폴백 동작 검증.

네이버(무료)로 못 찾은 회사만 Tavily(유료)로 다시 찾는 구조라, "네이버가
성공했는데도 Tavily가 돌았다"가 곧 요금이다. 폴백이 도는 조건과 돌지 않는
조건을 양쪽 다 고정한다.
"""

from contextlib import nullcontext as _nullcontext
from unittest.mock import patch

from backend_logic2.nodes.supplier.tools import naver_contact_enrichment as nce

NAVER_HIT = [{"link": "https://ganada.co.kr", "title": "가나다전기", "description": "멀티탭 제조"}]
NAVER_WIKI = [{"link": "https://ko.wikipedia.org/wiki/GS글로벌", "title": "GS글로벌", "description": "백과사전"}]
TAVILY_HIT = [{"url": "https://tavily-found.co.kr", "title": "가나다전기", "content": "제조사"}]


def _run(naver_items, tavily_items, ai_accepts=True, env=None):
    """_find_official_site_simple을 검색·AI 없이 돌리고 (결과, Tavily호출수)를 돌려준다."""
    def pick(company_name, candidates, item_name=None, case_id=None):
        return candidates[0] if (ai_accepts and candidates) else None

    env_patch = {"SUPPLIER_SITE_TAVILY_FALLBACK": env} if env is not None else {}
    with (
        patch.object(nce, "_search_naver_web", return_value=list(naver_items)),
        patch.object(nce, "_tavily_search_site", return_value=list(tavily_items)) as tavily,
        patch.object(nce, "_pick_best_site_candidate", side_effect=pick),
        patch.dict("os.environ", env_patch, clear=False),
    ):
        return nce._find_official_site_simple("가나다전기", item_name="멀티탭"), tavily.call_count


def test_naver_success_does_not_spend_a_tavily_call():
    site, tavily_calls = _run(NAVER_HIT, TAVILY_HIT)

    assert site["url"] == "https://ganada.co.kr"
    assert tavily_calls == 0


def test_empty_naver_result_falls_back_to_tavily():
    site, tavily_calls = _run([], TAVILY_HIT)

    assert site["url"] == "https://tavily-found.co.kr"
    assert tavily_calls == 1


def test_ai_rejecting_every_naver_candidate_also_falls_back():
    """네이버가 결과를 주더라도 AI 사이트판단이 전부 반려하면 확보 실패다.

    실측 20곳에서 네이버 실패의 상당수가 '검색 0건'이 아니라 이 경우였다.
    여기서 폴백이 안 돌면 폴백을 넣은 의미의 절반이 사라진다.
    """
    site, tavily_calls = _run(NAVER_HIT, TAVILY_HIT, ai_accepts=False)

    assert site is None  # 폴백 후보도 같은 AI 판단에서 반려됨
    assert tavily_calls == 1


def test_encyclopedia_domains_are_dropped_before_the_ai_sees_them():
    """AI 프롬프트가 백과사전을 제외하라고 해도 실제로는 통과한 사례가 있었다
    (GS글로벌 -> ko.wikipedia.org). 도메인 규칙으로 결정적으로 막는다."""
    assert nce._site_candidates(NAVER_WIKI, "link", "title", "description") == []

    site, tavily_calls = _run(NAVER_WIKI, TAVILY_HIT)

    assert site["url"] == "https://tavily-found.co.kr"
    assert tavily_calls == 1


def test_kill_switch_disables_the_paid_fallback():
    for value in ("0", "false", "False", "no", "off"):
        site, tavily_calls = _run([], TAVILY_HIT, env=value)

        assert site is None, f"SUPPLIER_SITE_TAVILY_FALLBACK={value}에서 폴백이 돌았다"
        assert tavily_calls == 0


def test_tavily_failure_is_swallowed_instead_of_breaking_the_search():
    """폴백은 이미 실패한 회사를 한 번 더 시도하는 단계다. 여기서 예외가
    올라가면 회사 하나 때문에 공급사 탐색 전체가 멈춘다."""
    breakages = {
        "_tavily_search_site": RuntimeError("Tavily 429"),
        "_pick_best_site_candidate": RuntimeError("LLM timeout"),
    }
    for attr, error in breakages.items():
        patches = {"_search_naver_web": [], attr: None}
        with (
            patch.object(nce, "_search_naver_web", return_value=[]),
            patch.object(nce, attr, side_effect=error),
        ):
            if attr != "_tavily_search_site":
                patches = None  # Tavily는 기본 동작(빈 결과)이면 AI까지 못 간다
            with patch.object(nce, "_tavily_search_site", return_value=list(TAVILY_HIT)) if patches is None else _nullcontext():
                try:
                    site = nce._find_official_site_simple("가나다전기", item_name="멀티탭")
                except Exception:  # noqa: BLE001
                    raise AssertionError(f"{attr} 실패가 호출자에게 전파되면 안 된다") from None
        assert site is None
