"""외부 견적 등록 후 ERPNext 기준으로 검토·정렬하는 오케스트레이터.

포털 견적은 이미 ERPNext에 있으므로 추출하지 않는다. 외부 견적만 추출해
ERPNext Draft로 등록하고, 이후에는 출처를 나누지 않고 RFQ에 연결된 모든
Supplier Quotation을 다시 조회하여 동일한 reviewer와 ranker를 적용한다.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

try:
    from .get_supplier_quotations import get_reviewable_quotations
    from .quotation_extractor import QuotationParser, classify_source, extract_quotation
    from .quotation_models import (
        IssueSeverity,
        Quotation,
        QuotationReview,
        RFQRequirements,
        ReviewIssue,
        ReviewStatus,
        SourceKind,
        dump_json,
        load_json,
    )
    from .quotation_ranker import rank_quotations
    from .quotation_registrar import register_supplier_quotation
    from .quotation_reviewer import load_rfq_requirements, review_quotation
except ImportError:
    from get_supplier_quotations import get_reviewable_quotations
    from quotation_extractor import QuotationParser, classify_source, extract_quotation
    from quotation_models import (
        IssueSeverity,
        Quotation,
        QuotationReview,
        RFQRequirements,
        ReviewIssue,
        ReviewStatus,
        SourceKind,
        dump_json,
        load_json,
    )
    from quotation_ranker import rank_quotations
    from quotation_registrar import register_supplier_quotation
    from quotation_reviewer import load_rfq_requirements, review_quotation


RegistrationFn = Callable[[Quotation], dict[str, Any]]
QuotationFetchFn = Callable[[str], list[Quotation]]


def _failure_review(source: dict[str, Any], status: ReviewStatus, evidence: str) -> QuotationReview:
    source_kind = None
    if source.get("path"):
        try:
            source_kind = classify_source(source["path"])
        except ValueError:
            pass
    issue = ReviewIssue(
        code="QUOTATION_PROCESSING_FAILED",
        severity=IssueSeverity.ERROR,
        message="외부 견적을 추출하거나 ERPNext에 등록하지 못했습니다.",
        evidence=evidence,
    )
    return QuotationReview(
        quotation_id=str(source.get("quotation_id") or Path(source.get("path", "UNKNOWN")).stem),
        supplier_name=source.get("supplier_name"),
        source_kind=source_kind,
        status=status,
        valid=False,
        specification_compliant=False,
        issues=[issue],
        rejection_evidence=[evidence],
    )


def extract_external_source(
    source: dict[str, Any],
    rfq: RFQRequirements,
    *,
    model_parser: QuotationParser | None = None,
    max_attempts: int = 3,
) -> tuple[Quotation | None, QuotationReview | None]:
    """외부 소스만 최대 3회 추출한다. 품질 검토는 ERP 등록 후 수행한다."""
    if source.get("channel") == "portal":
        return None, None

    if not source.get("path"):
        return None, _failure_review(source, ReviewStatus.HUMAN_REVIEW, "견적 source.path가 누락됨")

    try:
        source_kind = classify_source(source["path"])
    except (KeyError, ValueError) as exc:
        return None, _failure_review(source, ReviewStatus.EXCLUDED, str(exc))

    reflections: list[str] = []
    last_quotation: Quotation | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            last_quotation = extract_quotation(
                source["path"],
                rfq.rfq_name,
                supplier_name=source.get("supplier_name"),
                supplier_id=source.get("supplier_id"),
                quotation_id=source.get("quotation_id"),
                attempt=attempt,
                reflection_errors=reflections,
                rfq_requirements=rfq.model_dump(mode="json"),
                model_parser=model_parser,
            )
        except Exception as exc:
            reflections.append(f"{type(exc).__name__}: {exc}")
            if source_kind == SourceKind.EXCEL:
                return None, _failure_review(
                    source,
                    ReviewStatus.EXCLUDED,
                    "Excel 견적은 필수값 추출 실패 시 비교 대상에서 제외: " + reflections[-1],
                )
            continue

        return last_quotation, None

    evidence = " | ".join(reflections) or "추출 결과 없음"
    return None, _failure_review(source, ReviewStatus.HUMAN_REVIEW, evidence)


# 기존 호출부 호환용 이름. 의미는 이제 "외부 소스 추출"이며 review는 ERP
# 재조회 이후 run_pipeline에서만 수행한다.
process_source = extract_external_source


def run_pipeline(
    manifest: dict[str, Any],
    rfq_data: dict[str, Any] | RFQRequirements,
    *,
    top_k: int = 3,
    model_parser: QuotationParser | None = None,
    register_erp: bool = True,
    registration_fn: RegistrationFn | None = None,
    quotation_fetch_fn: QuotationFetchFn | None = None,
) -> dict[str, Any]:
    """외부 파일을 등록한 뒤 ERPNext의 전체 견적을 일괄 검토·정렬한다."""
    rfq = rfq_data if isinstance(rfq_data, RFQRequirements) else RFQRequirements.model_validate(rfq_data)
    extracted_quotations: list[Quotation] = []
    processing_failures: list[QuotationReview] = []
    registrations: list[dict[str, Any]] = []
    registration_fn = registration_fn or register_supplier_quotation
    quotation_fetch_fn = quotation_fetch_fn or get_reviewable_quotations
    for source in manifest.get("quotations", []):
        quotation, failure = extract_external_source(source, rfq, model_parser=model_parser)
        if failure:
            processing_failures.append(failure)
        if not quotation:
            continue
        extracted_quotations.append(quotation)
        if not register_erp:
            registrations.append({
                "status": "skipped",
                "quotation_number": quotation.quotation_id,
                "rfq_name": quotation.rfq_name,
                "supplier": quotation.supplier_id or quotation.supplier_name,
            })
            continue
        try:
            registrations.append(registration_fn(quotation))
        except Exception as exc:
            evidence = f"ERPNext Supplier Quotation 등록 실패: {type(exc).__name__}: {exc}"
            registrations.append({
                "status": "failed",
                "quotation_number": quotation.quotation_id,
                "rfq_name": quotation.rfq_name,
                "supplier": quotation.supplier_id or quotation.supplier_name,
                "error": evidence,
            })
            processing_failures.append(
                _failure_review(source, ReviewStatus.HUMAN_REVIEW, evidence)
            )

    # 이 지점부터 포털/외부 견적을 구분하지 않는다. ERPNext가 유일한 검토 원본이다.
    quotations = quotation_fetch_fn(rfq.rfq_name)
    reviews = [review_quotation(quotation, rfq) for quotation in quotations]
    reviews.extend(processing_failures)
    ranking = rank_quotations(reviews, rfq, top_k=top_k)
    return {
        "extracted_quotations": extracted_quotations,
        "quotations": quotations,
        "reviews": reviews,
        "registrations": registrations,
        "ranking": ranking,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="외부 견적 ERP 등록 후 전체 견적 검토·정렬")
    parser.add_argument(
        "manifest",
        nargs="?",
        help="선택: 외부 견적 소스 목록 JSON. 생략하면 ERPNext에 등록된 견적만 처리",
    )
    parser.add_argument("--rfq", required=True, help="ERPNext RFQ 이름 또는 RFQ 요구사항 JSON")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument(
        "--skip-register-erp",
        action="store_true",
        help="외부 견적 등록을 생략하고 ERPNext에 이미 존재하는 견적만 검토",
    )
    parser.add_argument("--output-dir", default="quotation_results")
    args = parser.parse_args()

    try:
        manifest = load_json(args.manifest) if args.manifest else {"quotations": []}
        rfq = load_rfq_requirements(args.rfq)
    except (FileNotFoundError, ValueError) as exc:
        parser.exit(2, f"입력 로드 실패: {exc}\n")

    result = run_pipeline(
        manifest,
        rfq,
        top_k=args.top_k,
        register_erp=not args.skip_register_erp,
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dump_json(result["extracted_quotations"], output_dir / "01_extracted_external.json")
    dump_json(result["registrations"], output_dir / "02_registered.json")
    dump_json(result["quotations"], output_dir / "03_erp_quotations.json")
    dump_json(result["reviews"], output_dir / "04_reviewed.json")
    dump_json(result["ranking"], output_dir / "05_ranked.json")
    print(f"완료: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
