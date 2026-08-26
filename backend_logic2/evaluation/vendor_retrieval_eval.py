"""공급사 검색기의 후보 수집, Gold 작성, 오프라인 평가 CLI.

외부 API/DB를 호출하는 것은 ``collect`` 명령뿐이다. 이후 라벨링과 평가는
저장된 retrieval snapshot을 사용하므로 같은 결과를 재현할 수 있다.
"""

from __future__ import annotations

import argparse
import html
import importlib
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from urllib.parse import urlparse


EVALUATION_DIR = Path(__file__).resolve().parent
BACKEND_LOGIC2_DIR = EVALUATION_DIR.parent
NODES_DIR = BACKEND_LOGIC2_DIR / "nodes"
DEFAULT_QUERIES = EVALUATION_DIR / "vendor_retrieval_queries.json"
DEFAULT_SNAPSHOT = EVALUATION_DIR / "vendor_retrieval_snapshot.json"
DEFAULT_LABELS = EVALUATION_DIR / "vendor_retrieval_labels.json"
DEFAULT_GOLD = EVALUATION_DIR / "vendor_retrieval_gold.json"
DEFAULT_REPORT = EVALUATION_DIR / "vendor_retrieval_report.json"
DEFAULT_K_VALUES = (1, 3, 5)
DEFAULT_COLLECTOR = "resolve_supplier:collect_vendor_candidates_for_evaluation"

DOCUMENT_RESULT_PATTERN = re.compile(
    r"(?:\.(?:pdf|hwp|hwpx|xls|xlsx|csv|doc|docx|ppt|pptx|zip)(?:$|[\s?#])"
    r"|filedown|file_download|download\.do|downloaddirect|/download(?:/|\?|$)|attachment)",
    re.IGNORECASE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")


def normalize_business_no(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalize_vendor_name(value: object) -> str:
    value = html.unescape(str(value or "")).casefold()
    return re.sub(r"[^0-9a-z가-힣]", "", value)


def normalize_domain(value: object) -> str:
    raw = str(value or "").strip().casefold()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    domain = (parsed.hostname or "").removeprefix("www.")
    return domain.rstrip(".")


def candidate_domain(candidate: dict) -> str:
    return normalize_domain(
        candidate.get("official_site_url") or candidate.get("source_url")
    )


def candidate_identity(candidate: dict) -> str:
    business_no = normalize_business_no(candidate.get("business_no"))
    if business_no:
        return f"business:{business_no}"
    domain = candidate_domain(candidate)
    if domain:
        return f"domain:{domain}"
    return f"name:{normalize_vendor_name(candidate.get('name'))}"


def label_candidate_identity(candidate: dict) -> str:
    """라벨 시트 행을 검색 후보와 같은 규칙으로 식별."""
    business_no = normalize_business_no(candidate.get("business_no"))
    if business_no:
        return f"business:{business_no}"
    domains = candidate.get("domains") or []
    if domains:
        return f"domain:{normalize_domain(domains[0])}"
    return f"name:{normalize_vendor_name(candidate.get('candidate_name'))}"


def candidate_matches_gold(candidate: dict, gold_vendor: dict) -> bool:
    candidate_id = str(candidate.get("vendor_id") or "").strip()
    gold_id = str(gold_vendor.get("vendor_id") or "").strip()
    if candidate_id and gold_id and candidate_id == gold_id:
        return True

    candidate_business_no = normalize_business_no(candidate.get("business_no"))
    gold_business_no = normalize_business_no(gold_vendor.get("business_no"))
    if candidate_business_no and gold_business_no == candidate_business_no:
        return True

    domain = candidate_domain(candidate)
    for gold_domain in gold_vendor.get("domains") or []:
        normalized_gold_domain = normalize_domain(gold_domain)
        if domain and normalized_gold_domain and (
            domain == normalized_gold_domain
            or domain.endswith(f".{normalized_gold_domain}")
        ):
            return True

    candidate_name = normalize_vendor_name(candidate.get("name"))
    vendor_name = normalize_vendor_name(gold_vendor.get("vendor_name"))
    if candidate_name and vendor_name and candidate_name == vendor_name:
        return True
    # 부분 일치는 사람이 명시한 alias에만 허용한다. 짧은 상호를 일반 웹 제목과
    # 부분 일치시키면 전혀 다른 회사를 정답으로 세는 오탐이 생긴다.
    for alias in gold_vendor.get("aliases") or []:
        normalized_alias = normalize_vendor_name(alias)
        if candidate_name and len(normalized_alias) >= 4 and normalized_alias in candidate_name:
            return True
    return False


def is_document_candidate(candidate: dict) -> bool:
    """첨부파일/다운로드 페이지를 공급사로 잘못 수집했는지 판별."""
    if candidate.get("candidate_type") == "vendor":
        return False
    target = " ".join(
        str(candidate.get(field) or "")
        for field in ("name", "source_url", "official_site_url", "description")
    )
    return bool(DOCUMENT_RESULT_PATTERN.search(target))


def is_valid_vendor_candidate(candidate: dict) -> bool:
    if candidate.get("candidate_type"):
        return candidate.get("candidate_type") == "vendor"
    return not is_document_candidate(candidate)


def _gold_match(candidate: dict, gold_vendors: list[dict]) -> tuple[int | None, int]:
    for index, gold_vendor in enumerate(gold_vendors):
        if candidate_matches_gold(candidate, gold_vendor):
            return index, int(gold_vendor["relevance"])
    return None, 0


def metrics_at_k(
    candidates: list[dict],
    gold_vendors: list[dict],
    k: int,
    judgments: list[dict] | None = None,
) -> dict:
    top_k = candidates[:k]
    matched_gold_indexes: set[int] = set()
    gains = []
    first_relevant_rank = None
    average_precision_sum = 0.0
    qualified_hits = 0
    judged_positions = 0

    for rank, candidate in enumerate(top_k, start=1):
        gold_index, relevance = _gold_match(candidate, gold_vendors)
        if gold_index is None or gold_index in matched_gold_indexes:
            gains.append(0)
            continue
        matched_gold_indexes.add(gold_index)
        gains.append(relevance)
        average_precision_sum += len(matched_gold_indexes) / rank
        if relevance >= 2:
            qualified_hits += 1
        if first_relevant_rank is None:
            first_relevant_rank = rank

    if judgments is not None:
        for candidate in top_k:
            if any(candidate_matches_gold(candidate, judgment) for judgment in judgments):
                judged_positions += 1

    relevant_count = len(gold_vendors)
    hits = len(matched_gold_indexes)
    qualified_count = sum(int(vendor.get("relevance", 0)) >= 2 for vendor in gold_vendors)
    dcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(gains, start=1))
    ideal_gains = sorted(
        (int(vendor["relevance"]) for vendor in gold_vendors), reverse=True
    )[:k]
    idcg = sum(
        (2**gain - 1) / math.log2(rank + 1)
        for rank, gain in enumerate(ideal_gains, start=1)
    )

    return {
        "precision": hits / k,
        "recall": hits / relevant_count if relevant_count else None,
        "pool_recall": hits / relevant_count if relevant_count else None,
        "ndcg": dcg / idcg if idcg else None,
        "average_precision": (
            average_precision_sum / min(relevant_count, k) if relevant_count else None
        ),
        "qualified_precision": qualified_hits / k,
        "qualified_recall": (
            qualified_hits / qualified_count if qualified_count else None
        ),
        "reciprocal_rank": 1 / first_relevant_rank if first_relevant_rank else 0.0,
        "hit": hits > 0,
        "judged_rate": judged_positions / k if judgments is not None else None,
        "unjudged_rate": 1 - (judged_positions / k) if judgments is not None else None,
        "vendor_validity_rate": sum(is_valid_vendor_candidate(c) for c in top_k) / k,
        "document_rate": sum(is_document_candidate(c) for c in top_k) / k,
        "result_fill_rate": len(top_k) / k,
        "duplicate_rate": (
            (len(top_k) - len({candidate_identity(c) for c in top_k})) / len(top_k)
            if top_k else 0.0
        ),
        "hits": hits,
        "retrieved": len(top_k),
        "gold_relevant": relevant_count,
    }


def reciprocal_rank_fusion(result_lists: list[list[dict]], rrf_k: int = 60) -> list[dict]:
    fused: dict[str, dict] = {}
    for candidates in result_lists:
        for fallback_rank, candidate in enumerate(candidates, start=1):
            if not is_valid_vendor_candidate(candidate):
                continue
            rank = int(candidate.get("retrieval_rank") or fallback_rank)
            identity = candidate_identity(candidate)
            if identity not in fused:
                fused[identity] = {
                    **candidate,
                    "source": "hybrid",
                    "source_members": [],
                    "rrf_score": 0.0,
                }
            fused[identity]["source_members"].append(candidate.get("source"))
            fused[identity]["rrf_score"] += 1.0 / (rrf_k + rank)

    ranked = sorted(
        fused.values(),
        key=lambda candidate: (
            -candidate["rrf_score"],
            normalize_vendor_name(candidate.get("name")),
        ),
    )
    for rank, candidate in enumerate(ranked, start=1):
        candidate["retrieval_rank"] = rank
    return ranked


def _load_collector(spec: str):
    """``module:function`` 형식의 평가 후보 수집기를 동적으로 로드한다."""
    if str(NODES_DIR) not in sys.path:
        sys.path.insert(0, str(NODES_DIR))
    try:
        module_name, function_name = spec.split(":", 1)
    except ValueError as error:
        raise ValueError("collector는 module:function 형식이어야 합니다.") from error
    collector = getattr(importlib.import_module(module_name), function_name, None)
    if not callable(collector):
        raise ValueError(f"호출 가능한 collector를 찾을 수 없습니다: {spec}")
    return collector


def _item_sources(item: dict) -> dict[str, list]:
    """schema v3 sources와 기존 v2 rag/naver/existing 구조를 모두 읽는다."""
    if isinstance(item.get("sources"), dict):
        return {
            str(name): list(candidates or [])
            for name, candidates in item["sources"].items()
        }
    return {
        name: list(item.get(name) or [])
        for name in ("existing", "rag", "naver")
        if name in item
    }


def collect_candidates(
    queries_path: Path,
    output_path: Path,
    rag_k: int,
    naver_k: int,
    category_k: int,
    collector_spec: str = DEFAULT_COLLECTOR,
    source_k: int | None = None,
) -> dict:
    collector = _load_collector(collector_spec)

    query_data = _read_json(queries_path)
    selected_item_codes = set(
        (query_data.get("evaluation_selection") or {}).get("item_codes") or []
    )
    snapshot = {
        "schema_version": 3,
        "created_at": _now_iso(),
        "settings": {
            "rag_k": rag_k,
            "naver_k": naver_k,
            "source_k": source_k or max(rag_k, naver_k),
            "category_k": category_k,
            "selected_item_codes": sorted(selected_item_codes),
            "collector": collector_spec,
            "collector_contract_version": 1,
            "source_names": [],
        },
        "items": [],
    }

    for item in query_data.get("items") or []:
        if not item.get("enabled", True):
            continue
        item_code = item.get("item_code") or ""
        if selected_item_codes and item_code not in selected_item_codes:
            continue
        item_name = item["item_name"]
        print(f"[collect] {item_code or '-'} | {item_name}")
        result = {
            "item_code": item_code,
            "item_name": item_name,
            "item_group": item.get("item_group"),
            "sources": {},
            "errors": {},
            "metadata": {},
        }

        try:
            collected = collector(
                item=item,
                limit_per_source=source_k or max(rag_k, naver_k),
                source_limits={"rag": rag_k, "naver": naver_k},
                category_limit=category_k,
            )
            if not isinstance(collected, dict) or not isinstance(collected.get("sources"), dict):
                raise ValueError("collector는 sources 객체를 포함한 dict를 반환해야 합니다.")
            normalized_sources = {}
            for source, candidates in collected["sources"].items():
                source_name = str(source)
                normalized_candidates = []
                for rank, candidate in enumerate(candidates or [], start=1):
                    if not isinstance(candidate, dict):
                        raise ValueError(f"{source_name} 후보는 dict여야 합니다.")
                    normalized = dict(candidate)
                    normalized.setdefault("source", source_name)
                    normalized.setdefault("retrieval_rank", rank)
                    normalized_candidates.append(normalized)
                normalized_sources[source_name] = normalized_candidates
            result["sources"] = normalized_sources
            result["errors"] = dict(collected.get("errors") or {})
            result["metadata"] = dict(collected.get("metadata") or {})
        except Exception as error:
            result["errors"]["collector"] = str(error)

        snapshot["items"].append(result)

    snapshot["settings"]["source_names"] = sorted({
        source
        for item in snapshot["items"]
        for source in _item_sources(item)
    })
    _write_json(output_path, snapshot)
    return snapshot


def build_label_sheet(
    snapshot_path: Path,
    output_path: Path,
    depth: int = 10,
    selected_item_codes: set[str] | None = None,
) -> dict:
    snapshot = _read_json(snapshot_path)
    previous_by_item = {}
    if output_path.exists():
        previous = _read_json(output_path)
        previous_by_item = {
            item.get("item_code"): {
                label_candidate_identity(candidate): candidate
                for candidate in item.get("candidates") or []
            }
            for item in previous.get("items") or []
        }
    label_items = []

    for item in snapshot.get("items") or []:
        if selected_item_codes and item.get("item_code") not in selected_item_codes:
            continue
        grouped: dict[str, dict] = {}
        sources = _item_sources(item)
        for source in sorted(sources, key=lambda name: (name != "existing", name)):
            for candidate in sources[source]:
                rank = int(candidate.get("retrieval_rank") or 0)
                if source != "existing" and rank > depth:
                    continue
                identity = candidate_identity(candidate)
                if identity not in grouped:
                    is_existing = source == "existing"
                    grouped[identity] = {
                        "candidate_name": candidate.get("name"),
                        "vendor_id": candidate.get("vendor_id"),
                        "business_no": candidate.get("business_no"),
                        "domains": [candidate_domain(candidate)] if candidate_domain(candidate) else [],
                        "aliases": [],
                        "observed_sources": [],
                        "relevance": 3 if is_existing else None,
                        "evidence": "ERP Item.supplier_items 승인 공급사" if is_existing else "",
                        "verified_by": "ERPNext" if is_existing else "",
                        "verified_at": snapshot.get("created_at", "") if is_existing else "",
                    }
                grouped[identity]["observed_sources"].append({
                    "source": source,
                    "rank": candidate.get("retrieval_rank"),
                    "url": candidate.get("source_url") or candidate.get("official_site_url"),
                    "category_similarity": candidate.get("category_similarity"),
                })

        previous_candidates = previous_by_item.get(item.get("item_code"), {})
        # 검색 snapshot을 다시 만들어도 사람이 독립적으로 추가한 정답 업체는
        # 검색 결과에 없다는 이유로 라벨 시트에서 제거하면 안 된다.
        for identity, previous_candidate in previous_candidates.items():
            human_sources = [
                source
                for source in previous_candidate.get("observed_sources") or []
                if source.get("source") == "human_reference"
            ]
            if not human_sources:
                continue
            if identity not in grouped:
                grouped[identity] = dict(previous_candidate)
                grouped[identity]["observed_sources"] = list(
                    previous_candidate.get("observed_sources") or []
                )
                continue
            known_sources = grouped[identity]["observed_sources"]
            for source in human_sources:
                if source not in known_sources:
                    known_sources.append(source)

        for identity, candidate in grouped.items():
            previous_candidate = previous_candidates.get(identity)
            if not previous_candidate:
                continue
            for field in (
                "relevance", "evidence", "verified_by", "verified_at", "aliases",
                "domains", "business_no", "vendor_id",
            ):
                if field in previous_candidate:
                    candidate[field] = previous_candidate[field]

        candidates = list(grouped.values())
        is_complete = bool(candidates) and all(
            isinstance(candidate.get("relevance"), int) for candidate in candidates
        )

        label_items.append({
            "item_code": item.get("item_code"),
            "item_name": item.get("item_name"),
            "labeling_status": "complete" if is_complete else "pending",
            "label_depth": depth,
            "labeling_guide": "relevance를 0(무관)~3(매우 적합)으로 입력하고 근거를 작성하세요.",
            "candidates": candidates,
        })

    labels = {
        "schema_version": 2,
        "created_at": _now_iso(),
        "source_snapshot": str(snapshot_path),
        "source_snapshot_created_at": snapshot.get("created_at"),
        "source_snapshot_schema_version": snapshot.get("schema_version"),
        "label_depth": depth,
        "items": label_items,
    }
    _write_json(output_path, labels)
    return labels


def add_human_reference(
    labels_path: Path,
    item_code: str,
    vendor_name: str,
    relevance: int,
    evidence: str,
    reviewer: str,
    business_no: str | None = None,
    domains: list[str] | None = None,
    aliases: list[str] | None = None,
    source_url: str | None = None,
) -> dict:
    """검색 결과와 독립적으로 확인한 정답 업체를 라벨 시트에 추가한다."""
    labels = _read_json(labels_path)
    _add_human_reference_to_labels(
        labels,
        item_code,
        vendor_name,
        relevance,
        evidence,
        reviewer,
        business_no=business_no,
        domains=domains,
        aliases=aliases,
        source_url=source_url,
    )
    _write_json(labels_path, labels)
    return labels


def _add_human_reference_to_labels(
    labels: dict,
    item_code: str,
    vendor_name: str,
    relevance: int,
    evidence: str,
    reviewer: str,
    business_no: str | None = None,
    domains: list[str] | None = None,
    aliases: list[str] | None = None,
    source_url: str | None = None,
) -> None:
    if relevance not in {1, 2, 3}:
        raise ValueError("사람이 추가하는 정답 업체의 relevance는 1, 2, 3 중 하나여야 합니다.")
    if not vendor_name.strip():
        raise ValueError("업체명은 비워 둘 수 없습니다.")
    if not evidence.strip():
        raise ValueError("독립 정답 업체에는 확인 근거가 필요합니다.")

    item = next(
        (
            entry
            for entry in labels.get("items") or []
            if entry.get("item_code") == item_code
        ),
        None,
    )
    if item is None:
        raise ValueError(f"라벨 시트에 item_code가 없습니다: {item_code}")

    normalized_domains = list(dict.fromkeys(
        normalized
        for value in domains or []
        if (normalized := normalize_domain(value))
    ))
    normalized_aliases = list(dict.fromkeys(
        value.strip() for value in aliases or [] if value.strip()
    ))
    reference = {
        "candidate_name": vendor_name.strip(),
        "vendor_id": None,
        "business_no": str(business_no).strip() if business_no else None,
        "domains": normalized_domains,
        "aliases": normalized_aliases,
        "observed_sources": [{
            "source": "human_reference",
            "rank": None,
            "url": source_url or (f"https://{normalized_domains[0]}" if normalized_domains else None),
        }],
        "relevance": relevance,
        "evidence": evidence.strip(),
        "verified_by": reviewer.strip() or "human",
        "verified_at": _now_iso(),
    }
    identity = label_candidate_identity(reference)
    candidates = item.setdefault("candidates", [])
    existing = next(
        (candidate for candidate in candidates if label_candidate_identity(candidate) == identity),
        None,
    )
    if existing is None:
        candidates.append(reference)
    else:
        existing.update({
            "relevance": relevance,
            "evidence": reference["evidence"],
            "verified_by": reference["verified_by"],
            "verified_at": reference["verified_at"],
        })
        if business_no and not existing.get("business_no"):
            existing["business_no"] = reference["business_no"]
        existing["domains"] = list(dict.fromkeys(
            [*(existing.get("domains") or []), *normalized_domains]
        ))
        existing["aliases"] = list(dict.fromkeys(
            [*(existing.get("aliases") or []), *normalized_aliases]
        ))
        observed_sources = existing.setdefault("observed_sources", [])
        if not any(source.get("source") == "human_reference" for source in observed_sources):
            observed_sources.extend(reference["observed_sources"])

    item["labeling_status"] = (
        "complete"
        if candidates and all(isinstance(candidate.get("relevance"), int) for candidate in candidates)
        else "pending"
    )


def import_human_references(
    labels_path: Path,
    references_path: Path,
    reviewer: str = "human",
) -> tuple[dict, int, int]:
    """JSON 목록의 독립 정답을 검증한 뒤 라벨 시트에 일괄 반영한다."""
    raw = _read_json(references_path)
    records = raw.get("references") if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        raise ValueError("references JSON은 배열 또는 references 배열을 가진 객체여야 합니다.")

    labels = _read_json(labels_path)
    imported = 0
    skipped = 0
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"references[{index}]는 객체여야 합니다.")
        vendor_name = str(record.get("name") or record.get("vendor_name") or "").strip()
        # 배포된 작업용 템플릿의 빈 행은 아직 사람이 작성하지 않은 항목이다.
        if not vendor_name:
            skipped += 1
            continue
        try:
            relevance = int(record.get("relevance"))
        except (TypeError, ValueError) as error:
            raise ValueError(f"references[{index}].relevance는 1~3 정수여야 합니다.") from error
        domains = record.get("domains") or []
        aliases = record.get("aliases") or []
        if not isinstance(domains, list) or not isinstance(aliases, list):
            raise ValueError(f"references[{index}].domains와 aliases는 배열이어야 합니다.")
        try:
            _add_human_reference_to_labels(
                labels,
                str(record.get("item_code") or "").strip(),
                vendor_name,
                relevance,
                str(record.get("evidence") or ""),
                str(record.get("reviewer") or reviewer),
                business_no=record.get("business_no"),
                domains=domains,
                aliases=aliases,
                source_url=record.get("source_url"),
            )
        except ValueError as error:
            raise ValueError(f"references[{index}] 오류: {error}") from error
        imported += 1

    _write_json(labels_path, labels)
    return labels, imported, skipped


def label_interactively(labels_path: Path, item_code: str | None, reviewer: str) -> dict:
    """미라벨 후보만 한 건씩 보여주고 입력 즉시 저장한다."""
    labels = _read_json(labels_path)
    selected_items = [
        item for item in labels.get("items") or []
        if not item_code or item.get("item_code") == item_code
    ]
    if item_code and not selected_items:
        raise ValueError(f"라벨 시트에 item_code가 없습니다: {item_code}")

    for item in selected_items:
        print(f"\n=== {item.get('item_code')} | {item.get('item_name')} ===")
        candidates = item.get("candidates") or []
        for index, candidate in enumerate(candidates, start=1):
            if isinstance(candidate.get("relevance"), int):
                continue
            sources = ", ".join(
                f"{source.get('source')}@{source.get('rank')}"
                for source in candidate.get("observed_sources") or []
            )
            urls = [
                source.get("url") for source in candidate.get("observed_sources") or []
                if source.get("url")
            ]
            print(f"\n[{index}/{len(candidates)}] {candidate.get('candidate_name')}")
            print(f"출처: {sources or '-'}")
            if urls:
                print(f"URL: {urls[0]}")

            while True:
                answer = input("관련도 0=무관, 1=검토, 2=적합, 3=매우적합, s=건너뜀, q=종료: ").strip().lower()
                if answer == "q":
                    _write_json(labels_path, labels)
                    return labels
                if answer == "s":
                    break
                if answer in {"0", "1", "2", "3"}:
                    candidate["relevance"] = int(answer)
                    candidate["verified_by"] = reviewer
                    candidate["verified_at"] = _now_iso()
                    candidate["evidence"] = input("근거(Enter면 빈 값): ").strip()
                    _write_json(labels_path, labels)
                    break
                print("0, 1, 2, 3, s, q 중 하나를 입력하세요.")

        item["labeling_status"] = (
            "complete"
            if candidates and all(isinstance(candidate.get("relevance"), int) for candidate in candidates)
            else "pending"
        )
        _write_json(labels_path, labels)

    return labels


def build_gold(labels_path: Path, output_path: Path) -> dict:
    labels = _read_json(labels_path)
    incomplete = []
    gold_items = []

    for item in labels.get("items") or []:
        candidates = item.get("candidates") or []
        invalid = [
            candidate.get("candidate_name")
            for candidate in candidates
            if not isinstance(candidate.get("relevance"), int)
            or not 0 <= candidate["relevance"] <= 3
        ]
        if item.get("labeling_status") != "complete" or invalid:
            incomplete.append({
                "item_code": item.get("item_code"),
                "invalid_or_unlabeled": invalid,
            })
            continue

        relevant_vendors = []
        judgments = []
        for candidate in candidates:
            judgment = {
                "vendor_name": candidate.get("candidate_name"),
                "vendor_id": candidate.get("vendor_id"),
                "business_no": candidate.get("business_no"),
                "domains": candidate.get("domains") or [],
                "aliases": candidate.get("aliases") or [],
                "relevance": candidate["relevance"],
                "evidence": candidate.get("evidence") or "",
                "verified_by": candidate.get("verified_by") or "",
                "verified_at": candidate.get("verified_at") or "",
                "observed_sources": candidate.get("observed_sources") or [],
            }
            judgments.append(judgment)
            if candidate["relevance"] <= 0:
                continue
            relevant_vendors.append(judgment)

        gold_items.append({
            "item_code": item.get("item_code"),
            "item_name": item.get("item_name"),
            "judgment_status": "complete",
            "judged_candidate_count": len(candidates),
            "judgments": judgments,
            "relevant_vendors": relevant_vendors,
        })

    if incomplete:
        details = ", ".join(
            f"{entry['item_code'] or '-'}({len(entry['invalid_or_unlabeled'])} unlabeled)"
            for entry in incomplete
        )
        raise ValueError(f"라벨링이 완료되지 않은 품목이 있습니다: {details}")

    source_snapshot_created_at = labels.get("source_snapshot_created_at")
    if not source_snapshot_created_at and labels.get("source_snapshot"):
        source_snapshot_path = Path(labels["source_snapshot"])
        if source_snapshot_path.exists():
            source_snapshot_created_at = _read_json(source_snapshot_path).get("created_at")

    gold = {
        "schema_version": 2,
        "created_at": _now_iso(),
        "source_labels": str(labels_path),
        "source_snapshot_created_at": source_snapshot_created_at,
        "gold_scope": "검색 후보 pooled judgments와 사람이 독립 확인한 human_reference 정답",
        "relevance_scale": {"0": "무관", "1": "검토 가능", "2": "적합", "3": "매우 적합"},
        "items": gold_items,
    }
    _write_json(output_path, gold)
    return gold


def evaluate(snapshot_path: Path, gold_path: Path, output_path: Path, k_values: tuple[int, ...]) -> dict:
    snapshot = _read_json(snapshot_path)
    gold = _read_json(gold_path)
    gold_by_code = {
        item.get("item_code"): item
        for item in gold.get("items") or []
        if item.get("judgment_status") == "complete"
    }
    if not gold_by_code:
        raise ValueError("평가할 complete Gold 품목이 없습니다. 먼저 라벨링과 build-gold를 완료하세요.")

    gold_snapshot_created_at = gold.get("source_snapshot_created_at")
    if (
        gold_snapshot_created_at
        and snapshot.get("created_at")
        and gold_snapshot_created_at != snapshot.get("created_at")
    ):
        raise ValueError(
            "Gold가 현재 snapshot에서 만들어진 것이 아닙니다. "
            "make-label-sheet → 라벨링 → build-gold를 다시 실행하세요."
        )

    per_item = []
    aggregate_values: dict[tuple[str, int, str], list[float]] = defaultdict(list)
    total_judged = 0
    total_relevant = 0
    total_qualified = 0
    total_independent_references = 0
    items_without_independent_references = []
    evaluated_source_names = set()

    for item in snapshot.get("items") or []:
        gold_item = gold_by_code.get(item.get("item_code"))
        if not gold_item:
            continue
        gold_vendors = gold_item.get("relevant_vendors") or []
        judgments = gold_item.get("judgments")
        judgments = judgments if judgments is not None else gold_vendors
        total_judged += len(judgments)
        total_relevant += len(gold_vendors)
        total_qualified += sum(
            int(vendor.get("relevance", 0)) >= 2 for vendor in gold_vendors
        )
        independent_references = sum(
            any(
                source.get("source") in {"human_reference", "existing"}
                for source in vendor.get("observed_sources") or []
            )
            for vendor in gold_vendors
        )
        total_independent_references += independent_references
        if independent_references == 0:
            items_without_independent_references.append(item.get("item_code"))
        result_lists = {
            source: candidates
            for source, candidates in _item_sources(item).items()
            if source != "existing"
        }
        if len(result_lists) >= 2:
            result_lists["hybrid"] = reciprocal_rank_fusion(list(result_lists.values()))
        evaluated_source_names.update(result_lists)
        item_result = {
            "item_code": item.get("item_code"),
            "item_name": item.get("item_name"),
            "gold_relevant": len(gold_vendors),
            "gold_diagnostics": {
                "judged_count": len(judgments),
                "qualified_count": sum(
                    int(vendor.get("relevance", 0)) >= 2 for vendor in gold_vendors
                ),
                "independent_reference_count": independent_references,
            },
            "sources": {},
        }

        for source, candidates in result_lists.items():
            source_result = {
                "candidate_count": len(candidates),
                "unique_candidate_count": len({candidate_identity(c) for c in candidates}),
                "metrics": {},
            }
            for k in k_values:
                metric = metrics_at_k(candidates, gold_vendors, k, judgments=judgments)
                source_result["metrics"][str(k)] = metric
                for metric_name in (
                    "precision", "pool_recall", "ndcg", "average_precision",
                    "qualified_precision", "qualified_recall", "reciprocal_rank",
                    "hit", "judged_rate", "unjudged_rate", "vendor_validity_rate",
                    "document_rate", "result_fill_rate", "duplicate_rate",
                ):
                    value = metric[metric_name]
                    if value is not None:
                        aggregate_values[(source, k, metric_name)].append(float(value))
            item_result["sources"][source] = source_result
        per_item.append(item_result)

    if not per_item:
        raise ValueError("snapshot과 Gold 사이에 item_code가 일치하는 품목이 없습니다.")

    aggregate = {}
    for source in sorted(evaluated_source_names):
        aggregate[source] = {}
        for k in k_values:
            aggregate[source][str(k)] = {
                metric_name: (
                    mean(aggregate_values[(source, k, metric_name)])
                    if aggregate_values[(source, k, metric_name)]
                    else None
                )
                for metric_name in (
                    "precision", "pool_recall", "ndcg", "average_precision",
                    "qualified_precision", "qualified_recall", "reciprocal_rank",
                    "hit", "judged_rate", "unjudged_rate", "vendor_validity_rate",
                    "document_rate", "result_fill_rate", "duplicate_rate",
                )
            }

    report = {
        "schema_version": 2,
        "created_at": _now_iso(),
        "evaluated_item_count": len(per_item),
        "k_values": list(k_values),
        "aggregate_macro_average": aggregate,
        "gold_diagnostics": {
            "judged_candidate_count": total_judged,
            "relevant_vendor_count": total_relevant,
            "qualified_vendor_count": total_qualified,
            "independent_reference_count": total_independent_references,
            "items_without_independent_references": items_without_independent_references,
            "pool_bias_warning": (
                "일부 평가 품목에 독립적인 human_reference/existing 정답이 없습니다. "
                "해당 품목은 같은 검색 결과를 라벨링한 Gold이므로 검색기 성능이 "
                "과대평가될 수 있습니다."
                if items_without_independent_references else None
            ),
        },
        "methodology": {
            "gold_scope": gold.get("gold_scope", "pooled judgments"),
            "recall_interpretation": (
                "전체 시장 recall이 아니라 검색 후보와 독립 확인 업체로 구성한 "
                "Gold 집합 내부의 pool_recall"
            ),
            "qualified_threshold": "relevance >= 2",
            "unjudged_policy": "precision/nDCG에서는 비정답으로 계산하고 judged_rate를 함께 보고",
            "hybrid_policy": "문서 다운로드 후보를 제외한 뒤 RRF 결합",
        },
        "per_item": per_item,
    }
    _write_json(output_path, report)
    return report


def print_report(report: dict) -> None:
    print(f"평가 품목: {report['evaluated_item_count']}개")
    print("source  K  Prec  QPrec  PoolR  MAP  nDCG  Valid  Judged")
    for source, by_k in report["aggregate_macro_average"].items():
        for k, metrics in by_k.items():
            def display(value: float | None) -> str:
                return "N/A" if value is None else f"{value:.3f}"

            print(
                f"{source:<7} {k:>2}  {display(metrics['precision']):>4}  "
                f"{display(metrics['qualified_precision']):>5}  "
                f"{display(metrics['pool_recall']):>5}  "
                f"{display(metrics['average_precision']):>4}  "
                f"{display(metrics['ndcg']):>4}  "
                f"{display(metrics['vendor_validity_rate']):>5}  "
                f"{display(metrics['judged_rate']):>6}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="RAG/Naver 후보 스냅샷 수집")
    collect_parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    collect_parser.add_argument("--output", type=Path, default=DEFAULT_SNAPSHOT)
    collect_parser.add_argument("--rag-k", type=int, default=20)
    collect_parser.add_argument("--naver-k", type=int, default=20)
    collect_parser.add_argument(
        "--source-k",
        type=int,
        help="검색 source 공통 후보 수. 미지정 시 rag-k/naver-k 중 큰 값",
    )
    collect_parser.add_argument("--category-k", type=int, default=5)
    collect_parser.add_argument(
        "--collector",
        default=DEFAULT_COLLECTOR,
        help="평가 후보 수집 함수(module:function)",
    )

    labels_parser = subparsers.add_parser("make-label-sheet", help="사람 검토용 JSON 생성")
    labels_parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    labels_parser.add_argument("--output", type=Path, default=DEFAULT_LABELS)
    labels_parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    labels_parser.add_argument("--depth", type=int, help="소스별 상위 몇 개까지 라벨링할지")

    label_parser = subparsers.add_parser("label", help="JSON 직접 편집 없이 대화형 라벨링")
    label_parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    label_parser.add_argument("--item-code", help="특정 품목만 라벨링")
    label_parser.add_argument("--reviewer", default="human", help="검토자 식별값")

    reference_parser = subparsers.add_parser(
        "add-reference",
        help="검색 결과 밖에서 사람이 확인한 정답 업체 추가",
    )
    reference_parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    reference_parser.add_argument("--item-code", required=True)
    reference_parser.add_argument("--vendor-name", "--name", dest="vendor_name", required=True)
    reference_parser.add_argument("--relevance", required=True, type=int, choices=(1, 2, 3))
    reference_parser.add_argument("--evidence", required=True, help="카탈로그·계약 이력 등 확인 근거")
    reference_parser.add_argument("--reviewer", default="human", help="검토자 식별값")
    reference_parser.add_argument("--business-no")
    reference_parser.add_argument("--domain", action="append", default=[])
    reference_parser.add_argument("--alias", action="append", default=[])
    reference_parser.add_argument("--source-url", help="근거를 확인한 URL")

    import_parser = subparsers.add_parser(
        "import-references",
        help="JSON 파일에서 human_reference 일괄 추가",
    )
    import_parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    import_parser.add_argument("--file", type=Path, required=True)
    import_parser.add_argument("--reviewer", default="human", help="기본 검토자 식별값")

    gold_parser = subparsers.add_parser("build-gold", help="완료된 라벨을 Gold JSON으로 변환")
    gold_parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    gold_parser.add_argument("--output", type=Path, default=DEFAULT_GOLD)

    evaluate_parser = subparsers.add_parser("evaluate", help="저장된 검색 결과 오프라인 평가")
    evaluate_parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    evaluate_parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    evaluate_parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    evaluate_parser.add_argument("--k", type=int, nargs="+", default=list(DEFAULT_K_VALUES))

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "collect":
        snapshot = collect_candidates(
            args.queries,
            args.output,
            args.rag_k,
            args.naver_k,
            args.category_k,
            collector_spec=args.collector,
            source_k=args.source_k,
        )
        print(f"수집 완료: {len(snapshot['items'])}개 품목 → {args.output}")
    elif args.command == "make-label-sheet":
        query_data = _read_json(args.queries)
        selection = query_data.get("evaluation_selection") or {}
        depth = args.depth or int(selection.get("label_depth") or 10)
        selected_item_codes = set(selection.get("item_codes") or [])
        labels = build_label_sheet(
            args.snapshot,
            args.output,
            depth,
            selected_item_codes=selected_item_codes,
        )
        print(f"라벨 시트 생성: {len(labels['items'])}개 품목 → {args.output}")
    elif args.command == "label":
        labels = label_interactively(args.labels, args.item_code, args.reviewer)
        complete_count = sum(
            item.get("labeling_status") == "complete" for item in labels.get("items") or []
        )
        print(f"라벨 저장 완료: complete {complete_count}/{len(labels.get('items') or [])}")
    elif args.command == "add-reference":
        labels = add_human_reference(
            args.labels,
            args.item_code,
            args.vendor_name,
            args.relevance,
            args.evidence,
            args.reviewer,
            business_no=args.business_no,
            domains=args.domain,
            aliases=args.alias,
            source_url=args.source_url,
        )
        item = next(
            entry for entry in labels.get("items") or []
            if entry.get("item_code") == args.item_code
        )
        reference_count = sum(
            any(source.get("source") == "human_reference" for source in candidate.get("observed_sources") or [])
            for candidate in item.get("candidates") or []
        )
        print(f"독립 정답 업체 저장: {args.item_code} human_reference {reference_count}개")
    elif args.command == "import-references":
        _, imported, skipped = import_human_references(
            args.labels,
            args.file,
            args.reviewer,
        )
        print(f"독립 정답 업체 일괄 저장: {imported}개, 미작성 템플릿 건너뜀: {skipped}개")
    elif args.command == "build-gold":
        gold = build_gold(args.labels, args.output)
        print(f"Gold 생성: {len(gold['items'])}개 품목 → {args.output}")
    elif args.command == "evaluate":
        k_values = tuple(sorted({k for k in args.k if k > 0}))
        report = evaluate(args.snapshot, args.gold, args.output, k_values)
        print_report(report)
        print(f"상세 보고서: {args.output}")


if __name__ == "__main__":
    main()
