"""추출된 견적의 형식·산식·RFQ 규격 부합 여부를 검토한다.


``--rfq``에는 ERPNext RFQ 이름 또는 기존 RFQ 요구사항 JSON 경로를 지정한다.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

try:
    from .quotation_models import (
        IssueSeverity,
        ItemCompliance,
        Quotation,
        QuotationReview,
        RFQItemRequirement,
        RFQRequirements,
        ReviewIssue,
        ReviewStatus,
        SourceKind,
        dump_json,
        load_json,
    )
except ImportError:
    from backend_logic2.nodes.quotation.quotation_filter.quotation_models import (
        IssueSeverity,
        ItemCompliance,
        Quotation,
        QuotationReview,
        RFQItemRequirement,
        RFQRequirements,
        ReviewIssue,
        ReviewStatus,
        SourceKind,
        dump_json,
        load_json,
    )


MONEY_QUANTUM = Decimal("1")
EXCLUSION_ERROR_CODES = frozenset({
    "SPECIFICATION_MISMATCH",
    "INSUFFICIENT_QUANTITY",
    "QUOTATION_EXPIRED",
    "RFQ_MISMATCH",
})
UNIT_FACTORS: dict[str, tuple[str, Decimal]] = {
    "mm": ("length", Decimal("0.001")),
    "cm": ("length", Decimal("0.01")),
    "m": ("length", Decimal("1")),
    "g": ("mass", Decimal("0.001")),
    "kg": ("mass", Decimal("1")),
    "mg": ("mass", Decimal("0.000001")),
    "ml": ("volume", Decimal("0.001")),
    "l": ("volume", Decimal("1")),
}
NUMERIC_SPEC = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([a-zA-Z㎜㎝㎏㎎㎖ℓ]+)\s*$")
GetOne = Callable[[str, str], dict[str, Any] | None]


def _erp_get_one() -> GetOne:
    """JSON 입력만 사용할 때는 ERP 모듈을 불러오지 않도록 지연 import한다."""
    backend_root = Path(__file__).resolve().parents[4]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    from backend_logic2.integrations.erp_client import erp_get_one

    return erp_get_one


def _html_to_text(value: Any) -> str:
    raw = html.unescape(str(value or ""))
    raw = re.sub(r"(?i)<br\s*/?>|</(?:p|div|li|tr)\s*>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


SPEC_LABELS = {
    "색상": "color",
    "color": "color",
    "모델": "model",
    "model": "model",
    "재질": "material",
    "material": "material",
    "치수": "dimensions",
    "dimensions": "dimensions",
    "등급": "grade",
    "grade": "grade",
    "규격": "specification",
    "specification": "specification",
    "규격(치수)": "dimensions",
    "규격（치수）": "dimensions",
    "길이": "length",
    "length": "length",
    "전압": "voltage",
    "voltage": "voltage",
}


def extract_specifications(description: Any) -> dict[str, str]:
    """ERPNext 품목 설명의 명시적 ``키: 값``과 모델 번호를 규격화한다.

    RFQ와 Supplier Quotation 양쪽에 같은 파서를 적용해야 한쪽에서만 모델을
    찾아내는 비대칭 판정을 피할 수 있다.
    """
    text = _html_to_text(description)
    specifications: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"\s*([^:：]+)\s*[:：]\s*(.+?)\s*$", line)
        if match:
            raw_key = re.sub(r"\s+", " ", match.group(1)).strip()
            key = SPEC_LABELS.get(raw_key.casefold(), raw_key)
            specifications[key] = match.group(2).strip()

    # 외부 견적을 ERP에 등록할 때 설명이 한 줄(``M100, 색상: 흰색``)로
    # 저장되는 경우도 있으므로 줄 시작에 한정하지 않고 알려진 라벨을 찾는다.
    for label, key in SPEC_LABELS.items():
        if key in specifications:
            continue
        match = re.search(
            rf"(?i)(?:^|[\s,;/|]){re.escape(label)}\s*[:：]\s*([^,\n;/|]+)",
            text,
        )
        if match:
            specifications[key] = match.group(1).strip()

    if "model" not in specifications:
        model_match = re.search(r"\b(?=[A-Z0-9._-]*\d)[A-Z][A-Z0-9._-]{1,}\b", text)
        if model_match:
            specifications["model"] = model_match.group(0)
    return specifications


def load_rfq_requirements_from_erp(
    rfq_name: str,
    *,
    get_one: GetOne | None = None,
) -> RFQRequirements:
    """ERPNext Request for Quotation을 reviewer 공통 RFQ 모델로 변환한다."""
    get_one = get_one or _erp_get_one()
    rfq = get_one("Request for Quotation", rfq_name)
    if not rfq:
        raise ValueError(f"ERPNext에서 RFQ '{rfq_name}'를 찾을 수 없습니다.")
    if int(rfq.get("docstatus") or 0) == 2:
        raise ValueError(f"ERPNext RFQ '{rfq_name}'는 취소된 문서입니다.")

    items = []
    for row in rfq.get("items") or []:
        items.append({
            "item_code": row.get("item_code"),
            "item_name": row.get("item_name") or row.get("item_code"),
            "quantity": row.get("qty"),
            "required_delivery_date": row.get("schedule_date") or rfq.get("schedule_date"),
            "specifications": extract_specifications(row.get("description")),
            "numeric_tolerance_percent": 0,   #오차범위
        })
    return RFQRequirements.model_validate({
        "rfq_name": rfq.get("name") or rfq_name,
        "items": items,
    })


def load_rfq_requirements(
    rfq_source: str | Path,
    *,
    get_one: GetOne | None = None,
) -> RFQRequirements:
    """기존 JSON 경로와 ERPNext RFQ 이름을 모두 지원한다."""
    source = Path(rfq_source)
    if source.is_file():
        return RFQRequirements.model_validate(load_json(source))
    if source.suffix.lower() == ".json":
        raise FileNotFoundError(f"RFQ 요구사항 JSON을 찾을 수 없습니다: {source}")
    return load_rfq_requirements_from_erp(str(rfq_source), get_one=get_one)


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _issue(code: str, severity: IssueSeverity, field: str | None, message: str, evidence: str) -> ReviewIssue:
    return ReviewIssue(code=code, severity=severity, field=field, message=message, evidence=evidence)


def _normalize_text(value: Any) -> str:
    """표기 구분자와 대소문자만 정규화하고 의미를 임의로 치환하지 않는다."""
    return re.sub(r"[\s_\-/]", "", str(value).casefold())


def _parse_measurement(value: Any) -> tuple[str, Decimal] | None:
    normalized = str(value).strip().lower().replace("㎜", "mm").replace("㎝", "cm")
    normalized = normalized.replace("㎏", "kg").replace("㎎", "mg").replace("㎖", "ml").replace("ℓ", "l")
    match = NUMERIC_SPEC.match(normalized)
    if not match:
        return None
    amount, unit = Decimal(match.group(1)), match.group(2)
    if unit not in UNIT_FACTORS:
        return None
    dimension, factor = UNIT_FACTORS[unit]
    return dimension, amount * factor


def _parse_dimension_vector(value: Any) -> tuple[Decimal, ...] | None:
    normalized = str(value).strip().lower().replace("×", "x").replace("*", "x")
    match = re.fullmatch(r"\s*([0-9.]+(?:\s*x\s*[0-9.]+)+)\s*(mm|cm|m)\s*", normalized)
    if not match:
        return None
    factor = UNIT_FACTORS[match.group(2)][1]
    try:
        return tuple(Decimal(part.strip()) * factor for part in match.group(1).split("x"))
    except Exception:
        return None


def _spec_matches(actual: Any, expected: Any, tolerance_percent: Decimal) -> tuple[bool, str]:
    actual_vector = _parse_dimension_vector(actual)
    expected_vector = _parse_dimension_vector(expected)
    if actual_vector and expected_vector and len(actual_vector) == len(expected_vector):
        differences = [abs(left - right) for left, right in zip(actual_vector, expected_vector)]
        allowances = [abs(value) * tolerance_percent / Decimal("100") for value in expected_vector]
        matched = all(diff <= allowed for diff, allowed in zip(differences, allowances))
        return matched, f"치수 단위 정규화 차이={differences}, 허용={allowances}"

    actual_measurement = _parse_measurement(actual)
    expected_measurement = _parse_measurement(expected)
    if actual_measurement and expected_measurement:
        if actual_measurement[0] != expected_measurement[0]:
            return False, f"단위 차원이 다름: 견적={actual}, RFQ={expected}"
        expected_value = expected_measurement[1]
        allowed = abs(expected_value) * tolerance_percent / Decimal("100")
        difference = abs(actual_measurement[1] - expected_value)
        return difference <= allowed, f"단위 정규화 차이={difference}, 허용={allowed}"

    actual_text, expected_text = _normalize_text(actual), _normalize_text(expected)
    similarity = SequenceMatcher(None, actual_text, expected_text).ratio()
    matched = actual_text == expected_text or actual_text in expected_text or expected_text in actual_text or similarity >= 0.85
    return matched, f"문자열 유사도={similarity:.3f} (기준 0.850)"


def _valid_business_number(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if len(digits) != 10:
        return False
    nums = [int(char) for char in digits]
    multipliers = [1, 3, 7, 1, 3, 7, 1, 3, 5]
    checksum = sum(num * multiplier for num, multiplier in zip(nums[:9], multipliers))
    checksum += (nums[8] * 5) // 10
    return (10 - checksum % 10) % 10 == nums[9]


def match_requirement(quotation_item: Any, requirements: RFQRequirements) -> RFQItemRequirement | None:
    """견적 품목을 RFQ의 단일 품목과 연결한다.

    RFQ/MR은 비즈니스 규칙상 품목이 항상 1개뿐이다. 공급사는 견적서에
    자기 회사 내부 품목코드/명칭을 쓰는 경우가 흔해서(예: RFQ 품목명은
    "화학보호복"인데 견적서엔 "화학보호복4형식(분무차단형,스프레이)"),
    코드/이름 유사도로 매칭을 시도하면 실제로는 같은 품목인데도
    RFQ_ITEM_NOT_FOUND로 튕겨서 견적 전체가 순위 계산에서 빠지는 문제가
    있었다. 품목이 하나뿐이면 코드/이름이 얼마나 다르게 적혀있든 그냥 그
    품목으로 확정한다. 품목이 여러 개인 RFQ가 생기면 아래 코드/이름 기반
    매칭으로 넘어간다.
    """
    if len(requirements.items) == 1:
        return requirements.items[0]
    if quotation_item.item_code:
        for required in requirements.items:
            if required.item_code and required.item_code == quotation_item.item_code:
                return required
    item_name = _normalize_text(quotation_item.item_name)
    matches = [
        required for required in requirements.items
        if SequenceMatcher(None, item_name, _normalize_text(required.item_name)).ratio() >= 0.85
    ]
    return matches[0] if len(matches) == 1 else None


def _invalid_model_review(raw: dict[str, Any], error: ValidationError) -> QuotationReview:
    source_raw = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    try:
        source_kind = SourceKind(source_raw.get("kind"))
    except (TypeError, ValueError):
        source_kind = None
    attempt = raw.get("extraction_attempt", 1)
    attempt = attempt if isinstance(attempt, int) else 1
    status = ReviewStatus.EXCLUDED if source_kind == SourceKind.EXCEL else (
        ReviewStatus.REEXTRACT if attempt < 3 else ReviewStatus.HUMAN_REVIEW
    )
    issues = [
        _issue(
            "SCHEMA_TYPE_ERROR",
            IssueSeverity.ERROR,
            ".".join(str(part) for part in item["loc"]),
            item["msg"],
            f"입력값={item.get('input')!r}",
        )
        for item in error.errors(include_url=False)
    ]
    evidence = [issue.evidence for issue in issues]
    return QuotationReview(
        quotation_id=str(raw.get("quotation_id") or "UNKNOWN"),
        supplier_name=raw.get("supplier_name"),
        source_kind=source_kind,
        status=status,
        valid=False,
        specification_compliant=False,
        issues=issues,
        rejection_evidence=evidence,
    )


def review_quotation(
    quotation_data: Quotation | dict[str, Any],
    rfq_data: RFQRequirements | dict[str, Any],
    *,
    today: date | None = None,
    known_rfq_names: set[str] | None = None,
) -> QuotationReview:
    """한 견적을 검토한다. 모든 실패 결과에는 ``rejection_evidence``가 남는다."""
    raw = quotation_data.model_dump() if isinstance(quotation_data, Quotation) else quotation_data
    try:
        quotation = quotation_data if isinstance(quotation_data, Quotation) else Quotation.model_validate(quotation_data)
    except ValidationError as exc:
        return _invalid_model_review(raw, exc)

    try:
        rfq = rfq_data if isinstance(rfq_data, RFQRequirements) else RFQRequirements.model_validate(rfq_data)
    except ValidationError as exc:
        issues = [
            _issue("RFQ_SCHEMA_ERROR", IssueSeverity.ERROR, ".".join(map(str, row["loc"])), row["msg"], f"RFQ 입력값={row.get('input')!r}")
            for row in exc.errors(include_url=False)
        ]
        return QuotationReview(
            quotation=quotation,
            quotation_id=quotation.quotation_id,
            supplier_name=quotation.supplier_name,
            source_kind=quotation.source.kind,
            status=ReviewStatus.RFQ_REWRITE,
            valid=False,
            specification_compliant=False,
            issues=issues,
            rejection_evidence=[issue.evidence for issue in issues],
        )

    issues: list[ReviewIssue] = []
    item_results: list[ItemCompliance] = []
    today = today or date.today()

    # ⚠️ 여러 라운드(재비딩 이전 RFQ까지)를 한 번에 평가하는 호출
    # (evaluate_quotations_for_rfqs)은 견적마다 실제로 제출됐던 RFQ가
    # 서로 다르지만, RFQRequirements(rfq)는 "지금 라운드" 것 하나만 로드해서
    # 모든 견적을 그걸로 검토한다. known_rfq_names 없이 quotation.rfq_name과
    # rfq.rfq_name을 그냥 비교하면, 지난 라운드에 제출된 견적은 전부
    # "RFQ_MISMATCH"로 걸려서 순위/최종선정에서 통째로 제외돼버렸다(실제
    # 버그로 나타남 - 지난 라운드 견적은 AI 분석을 몇 번을 다시 돌려도
    # 절대 평가되지도, 선택 가능해지지도 않았다). 여러 라운드를 함께 검토
    # 하는 호출은 호출부에서 known_rfq_names로 "이번에 같이 평가 중인 RFQ
    # 전체 집합"을 넘겨주므로, 그 안에 있으면 통과시킨다.
    rfq_mismatch = (
        quotation.rfq_name not in known_rfq_names
        if known_rfq_names is not None
        else quotation.rfq_name != rfq.rfq_name
    )
    if rfq_mismatch:
        issues.append(_issue("RFQ_MISMATCH", IssueSeverity.ERROR, "rfq_name", "견적의 RFQ가 검토 대상과 다릅니다.", f"견적={quotation.rfq_name}, 대상={rfq.rfq_name}"))
    if quotation.business_registration_no and not _valid_business_number(quotation.business_registration_no):
        issues.append(_issue("INVALID_BUSINESS_NUMBER", IssueSeverity.ERROR, "business_registration_no", "사업자등록번호 형식 또는 체크섬이 올바르지 않습니다.", quotation.business_registration_no))
    if quotation.valid_until and quotation.valid_until < today:
        issues.append(_issue("QUOTATION_EXPIRED", IssueSeverity.ERROR, "valid_until", "견적 유효기간이 만료되었습니다.", f"유효기한={quotation.valid_until}, 검토일={today}"))

    item_sum = Decimal("0")
    for index, item in enumerate(quotation.items):
        calculated = _money(item.quantity * item.unit_price)
        stated = _money(item.amount)
        if calculated != stated:
            issues.append(_issue("ITEM_AMOUNT_MISMATCH", IssueSeverity.ERROR, f"items.{index}.amount", "수량 × 단가와 품목 금액이 일치하지 않습니다.", f"{item.quantity} × {item.unit_price} = {calculated}, 기재={stated}"))
        item_sum += item.amount

        required = match_requirement(item, rfq)
        evidence: list[str] = []
        spec_ok = True
        qty_ok = True
        if not required:
            spec_ok = qty_ok = False
            evidence.append("RFQ에서 품목 코드/이름이 일치하는 단일 품목을 찾지 못함")
            issues.append(_issue("RFQ_ITEM_NOT_FOUND", IssueSeverity.ERROR, f"items.{index}", "견적 품목을 RFQ 품목과 연결할 수 없습니다.", f"item_code={item.item_code}, item_name={item.item_name}"))
        else:
            qty_ok = item.quantity >= required.quantity
            evidence.append(f"수량: 견적={item.quantity}, RFQ={required.quantity}")
            if not qty_ok:
                issues.append(_issue("INSUFFICIENT_QUANTITY", IssueSeverity.ERROR, f"items.{index}.quantity", "견적 수량이 RFQ 요청 수량보다 적습니다.", evidence[-1]))

            for key, expected in required.specifications.items():
                if key not in item.specifications:
                    spec_ok = False
                    evidence.append(f"규격 '{key}' 누락 (RFQ={expected})")
                    issues.append(_issue("MISSING_SPECIFICATION", IssueSeverity.ERROR, f"items.{index}.specifications.{key}", "필수 규격이 누락되었습니다.", evidence[-1]))
                    continue
                matched, match_evidence = _spec_matches(item.specifications[key], expected, required.numeric_tolerance_percent)
                spec_ok = spec_ok and matched
                evidence.append(f"규격 '{key}': 견적={item.specifications[key]}, RFQ={expected}; {match_evidence}")
                if not matched:
                    issues.append(_issue("SPECIFICATION_MISMATCH", IssueSeverity.ERROR, f"items.{index}.specifications.{key}", "RFQ 규격과 견적 규격이 부합하지 않습니다.", evidence[-1]))

            # 배송 예정일은 품목의 ERPNext expected_delivery_date를 우선한다.
            # lead_time_days만 있는 경우에는 견적일(transaction_date)이 있어야 계산할 수 있다.
            has_calculable_delivery = bool(
                item.expected_delivery_date
                or (quotation.quotation_date and item.lead_time_days is not None)
            )
            if required.required_delivery_date and not has_calculable_delivery:
                issues.append(_issue("MISSING_DELIVERY_DATE", IssueSeverity.ERROR, f"items.{index}.expected_delivery_date", "비교에 필요한 배송 예정일이 누락되었습니다.", f"RFQ 요구 납기={required.required_delivery_date}"))

        item_results.append(ItemCompliance(
            item_code=item.item_code,
            item_name=item.item_name,
            matched_rfq_item=required.item_code or required.item_name if required else None,
            specification_compliant=spec_ok,
            quantity_compliant=qty_ok,
            evidence=evidence,
        ))

    if _money(item_sum) != _money(quotation.subtotal):
        issues.append(_issue("SUBTOTAL_MISMATCH", IssueSeverity.ERROR, "subtotal", "품목 금액 합계와 공급가액이 일치하지 않습니다.", f"품목합={_money(item_sum)}, 공급가액={_money(quotation.subtotal)}"))
    calculated_total = _money(quotation.subtotal + quotation.tax_amount)
    if calculated_total != _money(quotation.total_amount):
        issues.append(_issue("TOTAL_MISMATCH", IssueSeverity.ERROR, "total_amount", "공급가액 + 세액과 총금액이 일치하지 않습니다.", f"{_money(quotation.subtotal)} + {_money(quotation.tax_amount)} = {calculated_total}, 총금액={_money(quotation.total_amount)}"))
    expected_vat = _money(quotation.subtotal * Decimal("0.1"))
    if quotation.currency == "KRW" and quotation.tax_amount not in (Decimal("0"), expected_vat):
        issues.append(_issue("UNUSUAL_VAT", IssueSeverity.ERROR, "tax_amount", "세액이 면세(0) 또는 공급가액의 10%와 다릅니다.", f"세액={quotation.tax_amount}, 일반 부가세={expected_vat}"))

    errors = [issue for issue in issues if issue.severity == IssueSeverity.ERROR]
    spec_ok = all(item.specification_compliant and item.quantity_compliant for item in item_results)
    if not errors:
        status = ReviewStatus.ACCEPTED
    elif any(issue.code in EXCLUSION_ERROR_CODES for issue in errors):
        status = ReviewStatus.EXCLUDED
    elif quotation.source.kind == SourceKind.EXCEL:
        status = ReviewStatus.EXCLUDED
    elif quotation.extraction_attempt < 3:
        # 누락·스키마·산식 오류는 OCR/구조화 오류일 수도 있으므로 먼저 재추출한다.
        # 원문 자체의 오류인지 확정할 수 없는 상태에서 공급사를 자동 탈락시키지 않는다.
        status = ReviewStatus.REEXTRACT
    else:
        # 재추출로도 해소되지 않은 누락·산식·사업자번호 오류는 사람에게 넘긴다.
        status = ReviewStatus.HUMAN_REVIEW

    rejection_evidence = [issue.evidence for issue in errors] if status != ReviewStatus.ACCEPTED else []
    return QuotationReview(
        quotation=quotation,
        quotation_id=quotation.quotation_id,
        supplier_name=quotation.supplier_name,
        source_kind=quotation.source.kind,
        status=status,
        valid=status == ReviewStatus.ACCEPTED,
        specification_compliant=spec_ok,
        issues=issues,
        item_compliance=item_results,
        rejection_evidence=rejection_evidence,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="추출 견적 검토")
    parser.add_argument("input", help="quotation_extractor 결과 JSON")
    parser.add_argument(
        "--rfq",
        required=True,
        help="ERPNext RFQ 이름(예: PUR-RFQ-2026-00295) 또는 RFQ 요구사항 JSON 경로",
    )
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        rfq = load_rfq_requirements(args.rfq)
    except Exception as exc:
        parser.exit(1, f"RFQ 요구사항 로드 실패: {exc}\n")
    result = review_quotation(load_json(args.input), rfq)
    rendered = dump_json(result, args.output)
    if args.output:
        print(f"검토 완료: {args.output} ({result.status.value})")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
