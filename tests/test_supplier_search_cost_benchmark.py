"""신규 협력사 탐색의 호출 비용을 전·후로 비교한다 (LangSmith 기록용).

왜 필요한가: 공급사 탐색을 만들면서 두 가지를 바꿨는데, 둘 다 "줄었다"고
말만 하고 숫자가 없었다. 발표에 넣을 수 있게 같은 입력으로 전·후를 모두
실제로 돌려 측정한다.

  A. 홈페이지 찾기: Tavily 검색 -> 네이버 검색 API(무료)
     회사 이름은 Tavily가 잘 찾지만, 찾아낸 회사마다 홈페이지를 또 Tavily로
     검색하면 호출이 회사 수만큼 늘어난다. 그 단계만 네이버로 옮겼다.

  B. 연락처 추출: 회사 1건씩 호출 -> batch_size개씩 묶어 호출
     묶으면 호출 수는 1/batch_size로 줄지만 프롬프트가 길어진다. 토큰이
     늘어난 만큼 요금이 올랐는지를 같이 본다.

⚠️ 공정성: 페이지 텍스트는 **한 번만** 가져와서 두 방식에 똑같이 넣는다.
그래야 측정되는 차이가 호출 방식 때문인지 페이지 내용 때문인지 섞이지 않는다.
LLM도 같은 함수(_extract_contacts_batch)를 batch_size만 바꿔 부른다 -
프롬프트 모양이 달라지면 토큰 비교가 의미를 잃는다.

실행 (실제 과금 호출이 나간다 - 기본은 건너뜀):
    set SUPPLIER_BENCHMARK=1
    set LANGSMITH_TRACING=true
    set LANGSMITH_PROJECT=supplier_gathering_test
    pytest tests/test_supplier_search_cost_benchmark.py -s

단가를 넣으면 요금 칸이 채워진다(안 넣으면 토큰만 표시):
    set BENCH_IN_PER_1M=...      1M 입력 토큰당 단가
    set BENCH_OUT_PER_1M=...     1M 출력 토큰당 단가
    set BENCH_TAVILY_PER_CALL=...  Tavily 1회 단가
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager

import pytest

RUN = os.getenv("SUPPLIER_BENCHMARK", "").strip().lower() in {"1", "true", "on", "yes"}
pytestmark = pytest.mark.skipif(
    not RUN,
    reason="실제 과금 API를 호출한다. SUPPLIER_BENCHMARK=1 일 때만 실행.",
)

# LangSmith에서 이 비교만 따로 보려고 프로젝트를 고정한다.
os.environ.setdefault("LANGSMITH_PROJECT", "supplier_gathering_test")

ITEM_NAME = os.getenv("BENCH_ITEM", "화학보호복")
COMPANIES = [
    name.strip()
    for name in os.getenv(
        "BENCH_COMPANIES",
        "삼공물산,오토스윙,한국3M,유한킴벌리,대한안전",
    ).split(",")
    if name.strip()
]
BATCH_SIZE = int(os.getenv("BENCH_BATCH_SIZE", "5"))


def _price(name: str) -> float | None:
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
    usage_metadata에 오기도 해서 양쪽을 다 본다. 어느 쪽도 없으면 호출 수만
    세고 토큰은 0으로 둔다 - 없는 숫자를 추정해서 채우지 않는다.
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
                            message = getattr(gen, "message", None)
                            meta = getattr(message, "usage_metadata", None) or {}
                            prompt = meta.get("input_tokens", prompt)
                            completion = meta.get("output_tokens", completion)
                if prompt or completion:
                    meter.usage_seen = True
                meter.input_tokens += int(prompt or 0)
                meter.output_tokens += int(completion or 0)

        return _Handler()

    def cost(self) -> float | None:
        if IN_PER_1M is None or OUT_PER_1M is None or not self.usage_seen:
            return None
        return (self.input_tokens / 1_000_000) * IN_PER_1M + (
            self.output_tokens / 1_000_000
        ) * OUT_PER_1M


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


def _run_with_meter(label: str, fn):
    """한 방식을 돌리고 호출 수·토큰·시간을 돌려준다. LangSmith에는 label로 남는다."""
    from langchain_core.tracers.context import tracing_v2_enabled

    meter = TokenMeter()
    started = time.perf_counter()
    with tracing_v2_enabled(project_name=os.environ["LANGSMITH_PROJECT"]):
        result = fn(meter)
    elapsed = time.perf_counter() - started
    return {"label": label, "meter": meter, "seconds": elapsed, "result": result}


def _fetch_pages_once(companies, item_name):
    """두 방식에 똑같이 넣을 페이지 텍스트를 한 번만 확보한다.

    이걸 방식마다 따로 받으면 그때그때 페이지가 달라져 토큰 비교가 깨진다.
    """
    from backend_logic2.nodes.supplier.tools import naver_contact_enrichment as nce

    prepared = []
    for name in companies:
        site = nce._find_official_site_simple(name, item_name=item_name)
        text = ""
        if site:
            text = nce._fetch_page_text(site["url"]) or site.get("content", "")
        prepared.append({"name": name, "page_text": text, "site_url": site["url"] if site else None})
    return [row for row in prepared if row["page_text"]]


def _extract_in_chunks(prepared, size, item_name, meter):
    """같은 함수를 묶음 크기만 바꿔 부른다. size=1이 예전 방식(1건씩)."""
    from backend_logic2.nodes.supplier.tools import naver_contact_enrichment as nce
    from langchain_core.callbacks.manager import CallbackManager

    handler = meter.handler()
    results = {}
    for start in range(0, len(prepared), size):
        chunk = prepared[start:start + size]
        with _with_callback(nce, handler):
            results.update(nce._extract_contacts_batch(chunk, item_name))
    return results


@contextmanager
def _with_callback(module, handler):
    """ChatOpenAI 생성 시 콜백을 끼워 넣는다 (함수 안에서 만들어져 주입 지점이 없다)."""
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


def _find_sites_with_tavily(companies, item_name, meter):
    """예전 방식 재현: 회사마다 Tavily로 홈페이지를 검색한다."""
    from tavily import TavilyClient

    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    hits = 0
    for name in companies:
        query = f"{name} {item_name} 공식 홈페이지" if item_name else f"{name} 공식 홈페이지"
        try:
            response = client.search(query=query, max_results=5)
            hits += 1 if (response or {}).get("results") else 0
        except Exception as exc:  # noqa: BLE001 - 측정이 목적이라 한 건 실패로 멈추지 않는다
            print(f"  [Tavily 실패] {name}: {exc}")
    return {"calls": len(companies), "hits": hits}


def _find_sites_with_naver(companies, item_name, meter):
    """현재 방식: 네이버 검색 API(무료)로 홈페이지를 찾는다."""
    from backend_logic2.nodes.supplier.tools import naver_contact_enrichment as nce

    with count_calls(nce, "_search_naver_web") as counter:
        hits = 0
        for name in companies:
            if nce._find_official_site_simple(name, item_name=item_name):
                hits += 1
    return {"calls": counter["count"], "hits": hits}


def _fmt_money(value):
    return "단가 미설정" if value is None else f"{value:,.4f}"


def _table(title, header, rows):
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    line = "| " + " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(header)) + " |"
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    body = [
        "| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)) + " |"
        for row in rows
    ]
    return "\n".join([f"\n### {title}", line, sep, *body])


def test_supplier_search_call_cost_before_and_after(capsys):
    """같은 회사 목록으로 전·후를 모두 돌리고 표를 찍는다."""
    assert COMPANIES, "BENCH_COMPANIES가 비어 있습니다."
    n = len(COMPANIES)
    print(f"\n품목: {ITEM_NAME} · 회사 {n}곳 · 묶음 크기 {BATCH_SIZE}")
    print(f"LangSmith 프로젝트: {os.environ['LANGSMITH_PROJECT']}")

    # ── A. 홈페이지 찾기: Tavily vs 네이버 ───────────────────────────────
    before_search = _run_with_meter(
        "A-before: 회사별 Tavily 검색",
        lambda meter: _find_sites_with_tavily(COMPANIES, ITEM_NAME, meter),
    )
    after_search = _run_with_meter(
        "A-after: 회사별 네이버 검색",
        lambda meter: _find_sites_with_naver(COMPANIES, ITEM_NAME, meter),
    )

    tavily_calls = before_search["result"]["calls"]
    tavily_cost = None if TAVILY_PER_CALL is None else tavily_calls * TAVILY_PER_CALL
    search_rows = [
        ["변경 전 (Tavily)", tavily_calls, 0, _fmt_money(tavily_cost),
         f"{before_search['seconds']:.1f}s", before_search["result"]["hits"]],
        ["변경 후 (네이버)", 0, after_search["result"]["calls"], _fmt_money(0.0 if TAVILY_PER_CALL is not None else None),
         f"{after_search['seconds']:.1f}s", after_search["result"]["hits"]],
        ["차이", f"-{tavily_calls}", f"+{after_search['result']['calls']}",
         "-100%" if TAVILY_PER_CALL is not None else "-", "-", "-"],
    ]
    table_a = _table(
        f"A. 홈페이지 찾기 — 회사 {n}곳",
        ["방식", "Tavily 호출", "네이버 호출(무료)", "검색 요금", "소요", "홈페이지 확보"],
        search_rows,
    )

    # ── B. 연락처 추출: 1건씩 vs 묶음 ────────────────────────────────────
    prepared = _fetch_pages_once(COMPANIES, ITEM_NAME)
    assert prepared, "페이지 텍스트를 한 건도 확보하지 못해 비교할 수 없습니다."
    print(f"페이지 확보: {len(prepared)}곳 (두 방식에 같은 텍스트를 넣습니다)")

    before_extract = _run_with_meter(
        "B-before: 1건씩 추출",
        lambda meter: _extract_in_chunks(prepared, 1, ITEM_NAME, meter),
    )
    after_extract = _run_with_meter(
        f"B-after: {BATCH_SIZE}건씩 묶어 추출",
        lambda meter: _extract_in_chunks(prepared, BATCH_SIZE, ITEM_NAME, meter),
    )

    def row(label, run):
        meter = run["meter"]
        return [
            label, meter.calls, f"{meter.input_tokens:,}", f"{meter.output_tokens:,}",
            _fmt_money(meter.cost()), f"{run['seconds']:.1f}s",
        ]

    b_before, b_after = before_extract["meter"], after_extract["meter"]
    diff_calls = f"{b_after.calls - b_before.calls:+d}"
    diff_in = f"{b_after.input_tokens - b_before.input_tokens:+,}"
    diff_out = f"{b_after.output_tokens - b_before.output_tokens:+,}"
    before_cost, after_cost = b_before.cost(), b_after.cost()
    diff_cost = "단가 미설정" if before_cost is None or after_cost is None else f"{after_cost - before_cost:+,.4f}"
    table_b = _table(
        f"B. 연락처 추출 — 회사 {len(prepared)}곳 (같은 페이지 텍스트)",
        ["방식", "LLM 호출", "입력 토큰", "출력 토큰", "요금", "소요"],
        [
            row("변경 전 (1건씩)", before_extract),
            row(f"변경 후 ({BATCH_SIZE}건씩)", after_extract),
            ["차이", diff_calls, diff_in, diff_out, diff_cost,
             f"{after_extract['seconds'] - before_extract['seconds']:+.1f}s"],
        ],
    )

    print(table_a)
    print(table_b)
    print(
        "\n읽는 법: A는 유료 검색을 무료 검색으로 옮긴 것이라 호출 요금이 사라집니다. "
        f"B는 호출 수가 {b_before.calls}회에서 {b_after.calls}회로 줄지만 프롬프트가 길어져 "
        "입력 토큰은 늘 수 있습니다. 요금 칸이 '단가 미설정'이면 "
        "BENCH_IN_PER_1M / BENCH_OUT_PER_1M / BENCH_TAVILY_PER_CALL을 넣고 다시 실행하세요."
    )
    if not (b_before.usage_seen and b_after.usage_seen):
        print("⚠️ 토큰 사용량이 응답에 없어 호출 수만 비교됩니다(토큰 0으로 표시).")

    # 측정이 목적이지만, 묶음이 호출 수를 줄인다는 전제는 깨지면 안 된다.
    assert b_after.calls <= b_before.calls
    assert before_search["result"]["calls"] > 0
