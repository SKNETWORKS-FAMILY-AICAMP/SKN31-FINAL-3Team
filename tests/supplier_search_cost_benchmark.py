"""신규 협력사 탐색의 호출 비용을 전·후로 비교한다 (LangSmith 기록용).

왜 만들었나: 공급사 탐색에서 두 가지를 바꿨는데 "줄었다"는 말만 있고 숫자가
없었다. 같은 품목·같은 회사 목록으로 전·후를 모두 실제로 돌려 측정한다.

  A. 홈페이지 찾기 : 네이버 단독 -> 네이버 + Tavily 폴백
     역할 분담은 원래부터 이렇다 - 회사명 수집은 Tavily, 그 회사의 홈페이지
     찾기는 네이버(무료). 홈페이지까지 회사마다 Tavily로 돌리면 호출이 회사
     수만큼 늘어 비싸기 때문이다. 문제는 네이버 적중률이라, 네이버가 못 찾은
     회사만 Tavily로 보완하는 폴백의 효과를 잰다.
     "Tavily만" 줄도 같이 찍지만 그건 과거 상태가 아니라 "홈페이지까지 전부
     Tavily로 돌렸다면"의 참고선이다.

  B. 연락처 추출 : 회사 1건씩 호출 -> batch_size개씩 묶어 호출
     호출 수가 1/batch_size로 줄고, 지시문 블록이 회사마다 반복되지 않아
     토큰까지 함께 줄어드는지를 실제로 측정한다.

⚠️ 공정성 1 — 홈페이지 찾기: 두 검색엔진 결과를 **같은 제외 규칙과 같은
AI 사이트 판단(_pick_best_site_candidate)** 에 넣는다. "검색 결과가 하나라도
나왔다"를 확보로 세면 Tavily가 부당하게 유리해진다(운영에서는 제외 규칙과
AI 판단을 통과한 URL만 쓰이므로, 그 기준으로 세야 운영 적중률과 같다).

⚠️ 공정성 2 — 연락처 추출: 페이지 텍스트는 **한 번만** 가져와 두 방식에
똑같이 넣고, LLM도 같은 _extract_contacts_batch를 묶음 크기만 바꿔 부른다.
그래야 차이가 호출 방식 때문인지 페이지 내용 때문인지 섞이지 않는다.

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


# ── 홈페이지 찾기: 두 검색엔진을 같은 판단기준에 넣는다 ──────────────────────

def site_query(name, item_name):
    """운영(_find_official_site_simple)과 같은 검색어를 쓴다."""
    return f"{name} {item_name} 공식 홈페이지" if item_name else f"{name} 공식 홈페이지"


def usable_candidates(items, link_key, title_key, desc_key):
    """검색 결과를 운영과 같은 제외 규칙에 통과시켜 같은 모양의 후보로 만든다.

    네이버 결과는 link/title/description, Tavily 결과는 url/title/content로
    키가 달라서 키 이름만 받아 맞춘다. 제외 규칙(채용·제3자 플랫폼 도메인,
    디렉토리 URL 패턴)은 운영 모듈의 것을 그대로 가져다 쓴다.
    """
    from backend_logic2.nodes.supplier.tools import naver_contact_enrichment as nce

    candidates = []
    for item in items or []:
        link = item.get(link_key, "") or ""
        if any(domain in link for domain in nce._EXCLUDED_CONTACT_DOMAINS):
            continue
        if nce._looks_like_directory_url(link):
            continue
        candidates.append({
            "title": item.get(title_key),
            "link": link,
            "description": item.get(desc_key, "") or "",
        })
    return candidates


def _decide(name, item_name, raw, candidates):
    """후보를 운영과 같은 AI 판단에 넣고, 못 찾았으면 어느 단계에서 떨어졌는지 남긴다.

    "검색 0건 / 후보 전부 제외 / AI 전부 반려"를 구분하는 게 중요하다. 원인이
    다르면 고치는 곳도 다르다 - 검색 0건이면 검색어 문제, AI 반려면 사이트
    판단 기준이 너무 엄격한 것이다.
    """
    from backend_logic2.nodes.supplier.tools import naver_contact_enrichment as nce

    base = {"raw": len(raw), "candidates": len(candidates), "url": None, "content": ""}
    if not raw:
        return {**base, "stage": "검색 0건"}
    if not candidates:
        return {**base, "stage": "후보 전부 제외(도메인·URL규칙)"}
    best = nce._pick_best_site_candidate(name, candidates, item_name=item_name)
    if not best:
        return {**base, "stage": "AI가 전부 반려"}
    return {**base, "url": best["link"], "content": best.get("description", ""), "stage": "확보"}


def resolve_site_naver(name, item_name, budget):
    """네이버 웹검색으로 홈페이지를 찾는다(변경 후)."""
    from backend_logic2.nodes.supplier.tools import naver_contact_enrichment as nce

    budget["naver"] += 1
    raw = nce._search_naver_web(site_query(name, item_name))
    return _decide(name, item_name, raw, usable_candidates(raw, "link", "title", "description"))


def resolve_site_tavily(name, item_name, budget):
    """Tavily 검색으로 홈페이지를 찾는다(변경 전, 그리고 폴백).

    운영의 네이버 경로와 같은 제외 규칙·같은 AI 판단을 거친다. 예전 버전은
    "results가 비어있지 않으면 확보"로 셌는데, 그러면 Tavily만 필터와 AI
    판단을 건너뛰어 적중률이 부풀려진다(실측에서 Tavily 20/20 vs 네이버
    7/20이 나온 원인 중 하나).
    """
    from tavily import TavilyClient

    budget["tavily"] += 1
    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    try:
        raw = (client.search(query=site_query(name, item_name), max_results=5) or {}).get("results") or []
    except Exception as exc:  # noqa: BLE001 - 측정이 목적이라 한 건 실패로 멈추지 않는다
        return {"url": None, "content": "", "raw": 0, "candidates": 0, "stage": f"Tavily 실패({exc})"}
    return _decide(name, item_name, raw, usable_candidates(raw, "url", "title", "content"))


def find_sites(companies, item_name, resolver, source, meter, budget):
    """회사별로 홈페이지를 찾고 한 줄씩 찍는다. resolver만 바꿔 전·후를 비교한다."""
    records = []
    with with_callback(meter.handler()):
        for name in companies:
            info = resolver(name, item_name, budget)
            rec = {"name": name, "source": source if info["url"] else None, **info}
            records.append(rec)
            if info["url"]:
                print(f"    [O] {name} -> {info['url']}")
            else:
                print(f"    [X] {name} - {info['stage']} (검색 {info['raw']}건 / 후보 {info['candidates']}건)")
    return records


def fallback_with_tavily(records, item_name, meter, budget):
    """네이버가 못 찾은 회사만 Tavily로 보완한다.

    records를 제자리에서 고쳐 url·source를 채운다. 운영에 넣기 전에 "유료
    호출을 몇 번만 더 써서 적중률이 얼마나 복구되는지"를 보려는 단계다.
    """
    misses = [r for r in records if not r["url"]]
    recovered = []
    with with_callback(meter.handler()):
        for rec in misses:
            info = resolve_site_tavily(rec["name"], item_name, budget)
            rec["fallback_stage"] = info["stage"]
            if info["url"]:
                rec.update(url=info["url"], content=info["content"], source="tavily(폴백)")
                recovered.append(rec["name"])
                print(f"    [O] {rec['name']} -> {info['url']}  (네이버 실패 -> Tavily 복구)")
            else:
                print(f"    [X] {rec['name']} - Tavily도 실패: {info['stage']}")
    return {"tried": len(misses), "recovered": len(recovered), "names": recovered}


def fetch_pages_once(records):
    """이미 찾아둔 홈페이지에서 페이지 텍스트를 한 번만 가져온다.

    두 방식에 같은 텍스트를 넣어야 토큰 비교가 깨지지 않는다. A단계에서
    확보한 URL을 그대로 재사용한다(예전 버전은 여기서 사이트 찾기를 처음부터
    다시 해서 검색·AI 호출이 두 배로 나갔다).
    """
    from backend_logic2.nodes.supplier.tools import naver_contact_enrichment as nce

    prepared = []
    for rec in records:
        if not rec.get("url"):
            rec["page_chars"] = 0
            continue
        text = nce._fetch_page_text(rec["url"]) or rec.get("content", "")
        rec["page_chars"] = len(text or "")
        if text:
            prepared.append({"name": rec["name"], "page_text": text, "site_url": rec["url"]})
        else:
            print(f"    [페이지 비어있음] {rec['name']} - B 비교에서 제외")
    return prepared


def extract_in_chunks(prepared, size, item_name, meter, sink=None):
    """같은 함수를 묶음 크기만 바꿔 부른다. size=1이 변경 전(1건씩).

    돌려주는 값은 추출 결과가 아니라 요약이다. LangSmith 목록에서 이 단계의
    Output 칸만 보고도 호출이 몇 번 나갔는지 바로 알 수 있게 하려는 것이다.
    추출 결과 자체는 sink에 담아 터미널 표에 쓴다.
    """
    from backend_logic2.nodes.supplier.tools import naver_contact_enrichment as nce

    results = {}
    with with_callback(meter.handler()):
        for start in range(0, len(prepared), size):
            results.update(nce._extract_contacts_batch(prepared[start:start + size], item_name))
    if sink is not None:
        sink.update(results)
    found = sum(1 for row in results.values() if row.get("email") or row.get("phone"))
    return {
        "batch_size": size,
        "companies": len(prepared),
        "llm_calls": meter.calls,
        "input_tokens": meter.input_tokens,
        "output_tokens": meter.output_tokens,
        "contacts_found": found,
    }


# ── 표 출력 ─────────────────────────────────────────────────────────────────

def money(value):
    return "단가 미설정" if value is None else f"{value:,.4f}"


def width(text):
    """한글은 터미널에서 두 칸을 먹어서 글자 수로 맞추면 표가 어긋난다."""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def table(title, header, rows):
    cols = len(header)
    widths = [max(width(r[i]) for r in [header, *rows]) for i in range(cols)]

    def cell(value, i):
        return str(value) + " " * (widths[i] - width(value))

    head = "| " + " | ".join(cell(h, i) for i, h in enumerate(header)) + " |"
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    body = ["| " + " | ".join(cell(c, i) for i, c in enumerate(r)) + " |" for r in rows]
    return "\n".join([f"\n### {title}", head, sep, *body])


def shorten(text, limit):
    text = str(text or "")
    return text if width(text) <= limit else text[: max(1, limit // 2)] + "…"


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
    print("\n[0/5] 품목명 정규화 중...")
    item, _ = phase("0. 품목명 정규화", normalize, item_name)
    print(f"  -> '{item_name}' -> '{item}'")

    print("\n[1/5] 후보 회사명 수집 중 (Tavily)...")
    companies, _ = phase("1. 회사명 수집", collect_company_names, item, count)
    if not companies:
        print("[중단] 후보 회사를 한 곳도 찾지 못했습니다.")
        return 1
    print(f"  -> {len(companies)}곳: {', '.join(companies)}")

    budget = {"naver": 0, "tavily": 0}

    print("\n[2/5] 홈페이지 찾기 — 참고선 (회사별 Tavily, 같은 제외규칙+AI판단)...")
    before_meter = TokenMeter()
    before_budget = {"naver": 0, "tavily": 0}
    before_records, before_secs = phase(
        "A-ref: 전부 Tavily였다면", find_sites,
        companies, item, resolve_site_tavily, "tavily", before_meter, before_budget,
    )
    before_hits = sum(1 for r in before_records if r["url"])

    print("\n[3/5] 홈페이지 찾기 — 네이버 단독 (폴백 전, 지금까지의 동작)...")
    naver_meter = TokenMeter()
    from backend_logic2.nodes.supplier.tools import naver_contact_enrichment as nce
    with count_calls(nce, "_search_naver_web") as naver_counter:
        records, after_secs = phase(
            "A-naver: 네이버 단독", find_sites,
            companies, item, resolve_site_naver, "naver", naver_meter, budget,
        )
    naver_hits = sum(1 for r in records if r["url"])

    print(f"\n[4/5] 홈페이지 찾기 — 폴백 (네이버 실패 {len(records) - naver_hits}곳만 Tavily)...")
    fb_meter = TokenMeter()
    fallback, fb_secs = phase("A-fallback: 네이버 실패분만 Tavily", fallback_with_tavily, records, item, fb_meter, budget)
    final_hits = sum(1 for r in records if r["url"])
    print(f"  -> {fallback['tried']}곳 재시도, {fallback['recovered']}곳 복구"
          f" (최종 {final_hits}/{len(records)}곳)")

    def tavily_money(calls):
        return money(None if TAVILY_PER_CALL is None else calls * TAVILY_PER_CALL)

    table_a = table(
        f"A. 홈페이지 찾기 — 회사 {len(companies)}곳",
        ["방식", "Tavily 호출(유료)", "네이버 호출(무료)", "AI 호출", "검색 요금", "소요", "홈페이지 확보"],
        [
            ["(참고) Tavily만", before_budget["tavily"], 0, before_meter.calls,
             tavily_money(before_budget["tavily"]), f"{before_secs:.1f}s", f"{before_hits}/{len(companies)}"],
            ["네이버만 (폴백 전)", 0, naver_counter["count"], naver_meter.calls,
             tavily_money(0), f"{after_secs:.1f}s", f"{naver_hits}/{len(companies)}"],
            ["네이버 + Tavily 폴백", budget["tavily"], naver_counter["count"],
             naver_meter.calls + fb_meter.calls, tavily_money(budget["tavily"]),
             f"{after_secs + fb_secs:.1f}s", f"{final_hits}/{len(companies)}"],
        ],
    )

    if before_hits and not naver_hits:
        print("\n⚠️ Tavily는 찾았는데 네이버는 0건입니다. 검색어가 너무 구체적이면"
              " 네이버는 문자 그대로 찾아 결과가 비어 나옵니다. 위의 정규화 결과를 확인하세요.")

    print("\n[5/5] 연락처 추출 — 페이지 확보 후 1건씩 vs 묶음...")
    prepared, _ = phase("B-prep: 페이지 텍스트 확보(1회)", fetch_pages_once, records)
    table_b = None
    contacts = {}
    if not prepared:
        print("[중단] 페이지 텍스트를 한 건도 확보하지 못해 B를 비교할 수 없습니다.")
    else:
        print(f"  -> {len(prepared)}곳 확보 (두 방식에 같은 텍스트를 넣습니다)")
        b_before_meter, b_after_meter = TokenMeter(), TokenMeter()
        before_summary, b_before_secs = phase("B-before: 1건씩 추출", extract_in_chunks, prepared, 1, item, b_before_meter)
        after_summary, b_after_secs = phase(
            f"B-after: {batch_size}건씩 묶어 추출", extract_in_chunks,
            prepared, batch_size, item, b_after_meter, contacts,
        )
        print(f"  -> 1건씩: LLM {before_summary['llm_calls']}회 · "
              f"{batch_size}건씩: LLM {after_summary['llm_calls']}회")

        def row(label, meter, secs):
            return [label, meter.calls, f"{meter.input_tokens:,}", f"{meter.output_tokens:,}",
                    money(meter.cost()), f"{secs:.1f}s"]

        before_cost, after_cost = b_before_meter.cost(), b_after_meter.cost()
        diff_cost = ("단가 미설정" if before_cost is None or after_cost is None
                     else f"{after_cost - before_cost:+,.4f}")
        table_b = table(
            f"B. 연락처 추출 — 회사 {len(prepared)}곳 (같은 페이지 텍스트)",
            ["방식", "LLM 호출", "입력 토큰", "출력 토큰", "요금", "소요"],
            [
                row("변경 전 (1건씩)", b_before_meter, b_before_secs),
                row(f"변경 후 ({batch_size}건씩)", b_after_meter, b_after_secs),
                ["차이", f"{b_after_meter.calls - b_before_meter.calls:+d}",
                 f"{b_after_meter.input_tokens - b_before_meter.input_tokens:+,}",
                 f"{b_after_meter.output_tokens - b_before_meter.output_tokens:+,}",
                 diff_cost, f"{b_after_secs - b_before_secs:+.1f}s"],
            ],
        )

    # 회사별 결과: "잘 찾아지나"를 눈으로 확인하는 표.
    detail_rows = []
    for rec in records:
        got = contacts.get(rec["name"], {})
        detail_rows.append([
            shorten(rec["name"], 20),
            rec["source"] or "-",
            shorten(rec["url"] or f"({rec['stage']})", 44),
            shorten(got.get("email") or "-", 28),
            shorten(got.get("phone") or "-", 16),
        ])
    table_c = table(
        f"C. 회사별 결과 — 홈페이지 {final_hits}/{len(records)}곳, "
        f"연락처 {sum(1 for r in detail_rows if r[3] != '-' or r[4] != '-')}곳",
        ["회사명", "홈페이지 출처", "홈페이지 / 실패 사유", "이메일", "전화"],
        detail_rows,
    )

    print(table_a)
    if table_b:
        print(table_b)
    print(table_c)

    print(
        f"\n읽는 법:\n"
        f"  · A — 세 줄 모두 같은 제외규칙과 같은 AI 사이트판단을 거친 숫자입니다. "
        f"홈페이지 찾기는 원래 네이버 단독이고 그때 확보가 {naver_hits}곳입니다. "
        f"실패분만 Tavily로 보완하면 유료 호출 {budget['tavily']}회를 써서 확보가 {final_hits}곳이 됩니다. "
        f"첫 줄(전부 Tavily)은 유료 {before_budget['tavily']}회에 {before_hits}곳이라, "
        f"폴백 쪽이 호출은 적고 확보는 많습니다.\n"
        f"  · 못 찾은 회사는 C표의 실패 사유를 보세요. '검색 0건'은 검색어 문제, "
        f"'AI가 전부 반려'는 사이트 판단 기준이 엄격한 것이라 고칠 곳이 다릅니다.\n"
        f"  · 운영에서는 홈페이지를 못 찾으면 연락처가 비어 그 회사가 후보에서 제외되므로, "
        f"확보 칸이 곧 공급사 후보 수입니다."
    )
    if table_b:
        print(
            f"  · B — 묶어 보내면 호출뿐 아니라 토큰도 줄어듭니다. 회사마다 반복되던 "
            f"지시문 블록이 묶음당 한 번만 들어가서, 묶느라 늘어난 분량보다 반복을 "
            f"줄여서 아낀 분량이 더 큽니다."
        )
    print(
        f"\nLangSmith → {os.environ['LANGSMITH_PROJECT']} 프로젝트:\n"
        f"  · 목록의 Output 칸만 봐도 단계별 호출 수가 보입니다.\n"
        f"  · 각 run을 열면 그 안에 자식 LLM 호출이 실제로 그만큼 달려 있습니다."
    )
    if not (naver_meter.usage_seen or before_meter.usage_seen):
        print("⚠️ 응답에 토큰 사용량이 없어 호출 수만 비교됩니다(토큰 0으로 표시).")
    if IN_PER_1M is None or OUT_PER_1M is None:
        print("※ 요금 칸을 채우려면 BENCH_IN_PER_1M / BENCH_OUT_PER_1M / BENCH_TAVILY_PER_CALL을 넣고 다시 실행하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
