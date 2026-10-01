"""신규 협력사 탐색의 호출 비용을 전·후로 비교한다 (LangSmith 기록용).

왜 만들었나: 공급사 탐색에서 두 가지를 바꿨는데 "줄었다"는 말만 있고 숫자가
없었다. 같은 품목·같은 회사 목록으로 전·후를 모두 실제로 돌려 측정한다.

  A. 홈페이지 찾기 : 회사별 Tavily 검색 -> 네이버 검색 API(무료)
     회사 이름은 Tavily가 잘 찾지만, 찾아낸 회사마다 홈페이지를 또 Tavily로
     검색하면 호출이 회사 수만큼 늘어난다. 그 단계만 네이버로 옮겼다.

  B. 연락처 추출 : 회사 1건씩 호출 -> batch_size개씩 묶어 호출
     묶으면 호출 수는 1/batch_size로 줄지만 프롬프트가 길어진다. 토큰이
     늘어난 만큼 요금이 올랐는지를 같이 본다.

⚠️ 공정성: 페이지 텍스트는 **한 번만** 가져와 두 방식에 똑같이 넣는다. 또 LLM도
같은 _extract_contacts_batch를 묶음 크기만 바꿔 부른다. 그래야 측정된 차이가
호출 방식 때문인지 페이지 내용·프롬프트 모양 때문인지 섞이지 않는다.

이 파일은 테스트가 아니라 측정 도구다(pytest가 수집하지 않도록 이름에 test_를
쓰지 않았다). supplier_search.py와 같은 방식으로 터미널에서 직접 돌린다.

실행 (PowerShell):
    $env:LANGSMITH_TRACING = "true"
    python tests/supplier_search_cost_benchmark.py

단가를 넣으면 요금 칸이 채워진다(안 넣으면 토큰 수만 표시):
    $env:BENCH_IN_PER_1M = "..."        1M 입력 토큰당 단가
    $env:BENCH_OUT_PER_1M = "..."       1M 출력 토큰당 단가
    $env:BENCH_TAVILY_PER_CALL = "..."  Tavily 1회 단가

.env 필요: TAVILY_API_KEY, OPENAI_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET
LangSmith 프로젝트: supplier_gathering_test (아래에서 고정)
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()

# LangSmith에서 이 비교만 따로 보려고 프로젝트를 고정한다.
os.environ.setdefault("LANGSMITH_PROJECT", "supplier_gathering_test")
os.environ.setdefault("LANGSMITH_TRACING", "true")

REQUIRED_KEYS = ("TAVILY_API_KEY", "OPENAI_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")


def _price(name: str):
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


IN_PER_1M = _price("BENCH_IN_PER_1M")
OUT_PER_1M = _price("BENCH_OUT_PER_1M")
TAVILY_PER_CALL = _price("BENCH_TAVILY_PER_CALL")


class TokenMeter:
    """LLM 호출 수와 토큰을 센다.

    langchain 버전에 따라 사용량이 llm_output에 오기도 하고 메시지의
    usage_metadata에 오기도 해서 양쪽을 본다. 둘 다 없으면 호출 수만 세고
    토큰은 0으로 둔다 - 없는 숫자를 추정해 채우지 않는다.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.usage_seen = False

    def handler(self):
        from langchain_core.callbacks.base import BaseCallbackHandler

        meter = self

        class _Handler(BaseCallbackHandler):
            def on_llm_end(self, response, **kwargs):  # noqa: ANN001
                meter.calls += 1
                usage = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
                prompt = usage.get("prompt_tokens")
                completion = usage.get("completion_tokens")
                if prompt is None and completion is None:
                    for batch in getattr(response, "generations", []) or []:
                        for gen in batch:
                            meta = getattr(getattr(gen, "message", None), "usage_metadata", None) or {}
                            prompt = meta.get("input_tokens", prompt)
                            completion = meta.get("output_tokens", completion)
                if prompt or completion:
                    meter.usage_seen = True
                meter.input_tokens += int(prompt or 0)
                meter.output_tokens += int(completion or 0)

        return _Handler()

    def cost(self):
        if IN_PER_1M is None or OUT_PER_1M is None or not self.usage_seen:
            return None
        return (self.input_tokens / 1_000_000) * IN_PER_1M + (self.output_tokens / 1_000_000) * OUT_PER_1M


@contextmanager
def with_callback(handler):
    """ChatOpenAI가 함수 안에서 생성돼 주입 지점이 없다. 모듈 속성을 잠시 바꿔 끼운다."""
    import langchain_openai

    original = langchain_openai.ChatOpenAI

    class _Traced(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            callbacks = list(kwargs.pop("callbacks", None) or [])
            callbacks.append(handler)
            super().__init__(*args, callbacks=callbacks, **kwargs)

    langchain_openai.ChatOpenAI = _Traced
    try:
        yield
    finally:
        langchain_openai.ChatOpenAI = original


@contextmanager
def count_calls(module, attr: str):
    """모듈 속성을 감싸 호출 횟수만 센다. 원본 동작은 그대로 통과시킨다."""
    original = getattr(module, attr)
    box = {"count": 0}

    def wrapped(*args, **kwargs):
        box["count"] += 1
        return original(*args, **kwargs)

    setattr(module, attr, wrapped)
    try:
        yield box
    finally:
        setattr(module, attr, original)


def phase(name: str, fn, *args, **kwargs):
    """한 단계를 LangSmith에 이름 있는 상위 run으로 남기고 시간을 잰다."""
    from langsmith import traceable

    traced = traceable(name=name, run_type="chain")(fn)
    started = time.perf_counter()
    result = traced(*args, **kwargs)
    return result, time.perf_counter() - started


# ── 측정 대상 ───────────────────────────────────────────────────────────────

def normalize(item_name):
    """운영과 같은 정규화. 이걸 빼면 비교 자체가 성립하지 않는다.

    supplier_search()는 맨 앞에서 한 번 정규화하고, 이후 모든 도구에
    normalized를 넘긴다(enrich 단계도 item_name=normalized). 원본 ERP
    품목명("삼상 유도전동기 2.2kW 4극")을 그대로 쓰면 네이버 검색어가
    "회사명 + 삼상 유도전동기 2.2kW 4극 + 공식 홈페이지"가 되는데,
    네이버 웹검색은 Tavily와 달리 문자 그대로 찾아서 결과가 0건이 된다.
    """
    from backend_logic2.nodes.supplier.tools.web_search_based_tool import normalize_item_name

    return normalize_item_name(item_name)


def collect_company_names(item_name, count):
    """실제 파이프라인과 같은 방식으로 후보 회사명을 모은다."""
    from backend_logic2.nodes.supplier.tools.web_search_based_tool import (
        tavily_collect_candidate_names,
    )

    rows = tavily_collect_candidate_names(item_name, target_count=count)
    return [row["name"] for row in rows][:count]


def find_sites_with_tavily(companies, item_name):
    """변경 전: 회사마다 Tavily로 홈페이지를 검색한다."""
    from tavily import TavilyClient

    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    hits = 0
    for name in companies:
        query = f"{name} {item_name} 공식 홈페이지" if item_name else f"{name} 공식 홈페이지"
        try:
            if (client.search(query=query, max_results=5) or {}).get("results"):
                hits += 1
        except Exception as exc:  # noqa: BLE001 - 측정이 목적이라 한 건 실패로 멈추지 않는다
            print(f"    [Tavily 실패] {name}: {exc}")
    return {"calls": len(companies), "hits": hits}


def find_sites_with_naver(companies, item_name, meter):
    """변경 후: 네이버 검색 API(무료)로 홈페이지를 찾는다(사이트 선택은 AI 1회)."""
    from backend_logic2.nodes.supplier.tools import naver_contact_enrichment as nce

    with count_calls(nce, "_search_naver_web") as counter, with_callback(meter.handler()):
        hits = sum(1 for name in companies if nce._find_official_site_simple(name, item_name=item_name))
    return {"calls": counter["count"], "hits": hits}


def fetch_pages_once(companies, item_name):
    """두 방식에 똑같이 넣을 페이지 텍스트를 한 번만 확보한다.

    방식마다 따로 받으면 그때그때 페이지가 달라져 토큰 비교가 깨진다.
    """
    from backend_logic2.nodes.supplier.tools import naver_contact_enrichment as nce

    prepared = []
    for name in companies:
        site = nce._find_official_site_simple(name, item_name=item_name)
        text = nce._fetch_page_text(site["url"]) or site.get("content", "") if site else ""
        if text:
            prepared.append({"name": name, "page_text": text, "site_url": site["url"]})
        else:
            print(f"    [페이지 없음] {name} - 비교에서 제외")
    return prepared


def extract_in_chunks(prepared, size, item_name, meter):
    """같은 함수를 묶음 크기만 바꿔 부른다. size=1이 변경 전(1건씩)."""
    from backend_logic2.nodes.supplier.tools import naver_contact_enrichment as nce

    results = {}
    with with_callback(meter.handler()):
        for start in range(0, len(prepared), size):
            results.update(nce._extract_contacts_batch(prepared[start:start + size], item_name))
    return results


# ── 표 출력 ─────────────────────────────────────────────────────────────────

def money(value):
    return "단가 미설정" if value is None else f"{value:,.4f}"


def table(title, header, rows):
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    head = "| " + " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(header)) + " |"
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    body = ["| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)) + " |" for r in rows]
    return "\n".join([f"\n### {title}", head, sep, *body])


def main():
    missing = [key for key in REQUIRED_KEYS if not os.getenv(key, "").strip()]
    if missing:
        print(f"[중단] .env에서 다음 키를 찾지 못했습니다: {', '.join(missing)}")
        return 1

    item_name = input("품목명 입력: ").strip()
    if not item_name:
        print("[중단] 품목명이 비었습니다.")
        return 1
    count_input = input("비교할 회사 개수 (그냥 엔터시 5개): ").strip()
    count = int(count_input) if count_input else 5
    batch_input = input("묶음 크기 (그냥 엔터시 5): ").strip()
    batch_size = int(batch_input) if batch_input else 5

    print(f"\n{'=' * 60}")
    print(f"LangSmith 프로젝트: {os.environ['LANGSMITH_PROJECT']}")
    print(f"품목: {item_name} · 회사 {count}곳 · 묶음 {batch_size}")
    print(f"{'=' * 60}")

    # 운영과 똑같이 맨 앞에서 한 번만 정규화하고, 이후 모든 단계에 이걸 넘긴다.
    print("\n[0/4] 품목명 정규화 중...")
    item, _ = phase("0. 품목명 정규화", normalize, item_name)
    print(f"  -> '{item_name}' -> '{item}'")

    print("\n[1/4] 후보 회사명 수집 중 (Tavily)...")
    companies, _ = phase("1. 회사명 수집", collect_company_names, item, count)
    if not companies:
        print("[중단] 후보 회사를 한 곳도 찾지 못했습니다.")
        return 1
    print(f"  -> {len(companies)}곳: {', '.join(companies)}")

    print("\n[2/4] 홈페이지 찾기 — 변경 전 (회사별 Tavily)...")
    before_search, before_secs = phase("A-before: 회사별 Tavily 검색", find_sites_with_tavily, companies, item)

    print("[3/4] 홈페이지 찾기 — 변경 후 (네이버 무료 API)...")
    naver_meter = TokenMeter()
    after_search, after_secs = phase("A-after: 회사별 네이버 검색", find_sites_with_naver, companies, item, naver_meter)

    tavily_calls = before_search["calls"]
    tavily_cost = None if TAVILY_PER_CALL is None else tavily_calls * TAVILY_PER_CALL
    table_a = table(
        f"A. 홈페이지 찾기 — 회사 {len(companies)}곳",
        ["방식", "Tavily 호출(유료)", "네이버 호출(무료)", "검색 요금", "소요", "홈페이지 확보"],
        [
            ["변경 전 (Tavily)", tavily_calls, 0, money(tavily_cost), f"{before_secs:.1f}s", before_search["hits"]],
            ["변경 후 (네이버)", 0, after_search["calls"], money(0.0 if TAVILY_PER_CALL is not None else None),
             f"{after_secs:.1f}s", after_search["hits"]],
            ["차이", f"-{tavily_calls}", f"+{after_search['calls']}",
             "-100%" if TAVILY_PER_CALL is not None else "-", "-", "-"],
        ],
    )

    if before_search["hits"] and not after_search["hits"]:
        print("\n⚠️ Tavily는 찾았는데 네이버는 0건입니다. 검색어가 너무 구체적이면"
              " 네이버는 문자 그대로 찾아 결과가 비어 나옵니다. 위의 정규화 결과를 확인하세요.")

    print("\n[4/4] 연락처 추출 — 페이지 확보 후 1건씩 vs 묶음...")
    prepared, _ = phase("B-prep: 페이지 텍스트 확보(1회)", fetch_pages_once, companies, item)
    if not prepared:
        print("[중단] 페이지 텍스트를 한 건도 확보하지 못해 B를 비교할 수 없습니다.")
        print(table_a)
        return 1
    print(f"  -> {len(prepared)}곳 확보 (두 방식에 같은 텍스트를 넣습니다)")

    before_meter, after_meter = TokenMeter(), TokenMeter()
    _, b_before_secs = phase("B-before: 1건씩 추출", extract_in_chunks, prepared, 1, item, before_meter)
    _, b_after_secs = phase(f"B-after: {batch_size}건씩 묶어 추출", extract_in_chunks, prepared, batch_size, item, after_meter)

    def row(label, meter, secs):
        return [label, meter.calls, f"{meter.input_tokens:,}", f"{meter.output_tokens:,}",
                money(meter.cost()), f"{secs:.1f}s"]

    before_cost, after_cost = before_meter.cost(), after_meter.cost()
    diff_cost = ("단가 미설정" if before_cost is None or after_cost is None
                 else f"{after_cost - before_cost:+,.4f}")
    table_b = table(
        f"B. 연락처 추출 — 회사 {len(prepared)}곳 (같은 페이지 텍스트)",
        ["방식", "LLM 호출", "입력 토큰", "출력 토큰", "요금", "소요"],
        [
            row("변경 전 (1건씩)", before_meter, b_before_secs),
            row(f"변경 후 ({batch_size}건씩)", after_meter, b_after_secs),
            ["차이", f"{after_meter.calls - before_meter.calls:+d}",
             f"{after_meter.input_tokens - before_meter.input_tokens:+,}",
             f"{after_meter.output_tokens - before_meter.output_tokens:+,}",
             diff_cost, f"{b_after_secs - b_before_secs:+.1f}s"],
        ],
    )

    print(table_a)
    print(table_b)
    print(
        f"\n읽는 법: A는 유료 검색을 무료 검색으로 옮긴 것이라 호출 요금이 사라집니다"
        f"(네이버 호출이 회사 수보다 많으면 홈페이지를 못 찾아 '연락처'로 재검색한 것입니다). "
        f"B는 호출이 {before_meter.calls}회에서 {after_meter.calls}회로 줄지만 프롬프트가 길어져 "
        f"입력 토큰은 늘 수 있습니다."
    )
    if not (before_meter.usage_seen and after_meter.usage_seen):
        print("⚠️ 응답에 토큰 사용량이 없어 호출 수만 비교됩니다(토큰 0으로 표시).")
    if IN_PER_1M is None or OUT_PER_1M is None:
        print("※ 요금 칸을 채우려면 BENCH_IN_PER_1M / BENCH_OUT_PER_1M / BENCH_TAVILY_PER_CALL을 넣고 다시 실행하세요.")
    print(f"\nLangSmith → {os.environ['LANGSMITH_PROJECT']} 프로젝트에서 "
          f"'A-before', 'A-after', 'B-before', 'B-after' run으로 확인하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
