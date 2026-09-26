"""이미 끝난 건에 자동 선정 조건을 돌려보고 담당자 선택과 얼마나 맞는지 본다.

자동화를 켜기 전에 임계값을 정하는 근거를 만드는 용도다. 저장된 순위만
읽고 ERPNext도 RunPod도 부르지 않으며, 아무것도 바꾸지 않는다.

실행:
    python -m scripts.backtest_auto_selection
    python -m scripts.backtest_auto_selection --score-gap 15 --limit 200
"""

from __future__ import annotations

import argparse
from collections import Counter
from typing import Any

from procurement_db import get_connection

from backend_logic2.policies.runtime import policy_scope
from backend_logic2.policies.schema import CompanyPolicy
from backend_logic2.services.auto_progress import evaluate_final_selection


_CASES_SQL = """
    SELECT case_id,
           mr_name,
           workflow_snapshot #>> '{values,selected_supplier}' AS selected_supplier,
           workflow_snapshot #> '{values,quotation_ranking}' AS ranking,
           workflow_snapshot #> '{values,quotation_excluded}' AS excluded,
           workflow_snapshot #> '{values,quotation_ranking_meta}' AS meta,
           workflow_snapshot #> '{values,existing_supplier_candidates}' AS existing_candidates,
           workflow_snapshot #> '{values,selected_suppliers}' AS rfq_recipients,
           quotation_snapshot AS quotation_snapshot
    FROM procurement.procurement_case
    WHERE workflow_snapshot #>> '{values,selected_supplier}' IS NOT NULL
      AND workflow_snapshot #>> '{values,selected_supplier}' <> ''
      AND jsonb_array_length(
            COALESCE(workflow_snapshot #> '{values,quotation_ranking}', '[]'::jsonb)
          ) > 0
    ORDER BY updated_at DESC
    LIMIT %(limit)s
"""


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in (value or []) if isinstance(row, dict)]


def _as_result(case: dict[str, Any]) -> dict[str, Any]:
    ranking = _rows(case.get("ranking"))
    excluded = _rows(case.get("excluded"))
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    return {
        "ranking": ranking,
        "excluded": excluded,
        "parse_failed": [row for row in excluded if row.get("kind") == "parse_failed"],
        "competition_count": meta.get("competition_count") or len(ranking),
        "single_bid": bool(meta.get("single_bid")) or len(ranking) <= 1,
        "specification_evaluation": meta.get("specification_evaluation")
        # 옛 건은 평가 상태를 저장하지 않았다. 규격 점수가 있으면 평가된
        # 것으로 본다(없으면 unknown으로 남아 조건에 걸린다).
        or ({"status": "completed", "model": "(기록 없음)"}
            if ranking and ranking[0].get("specification_score") is not None
            else {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="자동 선정 조건 과거 검증")
    parser.add_argument("--limit", type=int, default=100, help="살펴볼 최근 건수")
    parser.add_argument("--score-gap", type=float, default=None, help="1-2위 최소 점수차 (기본: 정책값)")
    parser.add_argument("--max-amount", type=int, default=None, help="선정 금액 상한 (기본: 정책값)")
    parser.add_argument("--min-competition", type=int, default=None, help="최소 경쟁 견적 수 (기본: 정책값)")
    args = parser.parse_args()

    overrides: dict[str, Any] = {"automation_mode": "on", "auto_final_selection": True}
    if args.score_gap is not None:
        overrides["auto_selection_score_gap"] = args.score_gap
    if args.max_amount is not None:
        overrides["auto_selection_max_amount"] = args.max_amount
    if args.min_competition is not None:
        overrides["min_competing_suppliers"] = args.min_competition
    base = CompanyPolicy()
    policy = base.model_copy(update={"rules": base.rules.model_copy(update=overrides)})

    with get_connection() as connection:
        cases = [dict(row) for row in connection.execute(_CASES_SQL, {"limit": args.limit}).fetchall()]

    if not cases:
        print("검증할 과거 건이 없습니다 (선정이 끝나고 순위가 저장된 건 기준).")
        return 0

    # 진단: 순위에 몇 건이 올라 있고, 실제로는 몇 곳이 회신했는지.
    # 옛 2항목 엔진은 규격 점수가 없는 견적을 순위에서 통째로 뺐기 때문에,
    # "순위 건수"와 "실제 회신 건수"가 다르면 경쟁 부족 판정이 데이터의
    # 흔적일 수 있다.
    ranking_sizes = Counter()
    responded_sizes = Counter()
    shrunk = 0

    blockers = Counter()
    agreed: list[dict[str, Any]] = []
    disagreed: list[dict[str, Any]] = []
    stopped = 0

    print(f"기준: 점수차 {policy.rules.auto_selection_score_gap:g}점 이상 · "
          f"경쟁 {policy.rules.min_competing_suppliers}건 이상 · "
          f"금액 {policy.rules.auto_selection_max_amount:,}원 이하\n")

    with policy_scope(policy):
        for case in cases:
            known = {
                str(row.get("name") or "").strip()
                for row in _rows(case.get("existing_candidates"))
            }
            decision = evaluate_final_selection(
                _as_result(case),
                deadline_passed=True,
                known_supplier_names=known,
            )
            ranking_size = len(_rows(case.get("ranking")))
            snapshot = case.get("quotation_snapshot") if isinstance(case.get("quotation_snapshot"), dict) else {}
            responded = int(snapshot.get("responded_count") or 0)
            ranking_sizes[ranking_size] += 1
            responded_sizes[responded] += 1
            if responded > ranking_size:
                shrunk += 1

            actual = str(case.get("selected_supplier") or "").strip()
            predicted = decision.evidence.get("top_supplier")
            row = {
                "mr_name": case.get("mr_name"),
                "actual": actual,
                "predicted": predicted,
                "score_gap": decision.evidence.get("score_gap"),
            }
            if not decision.allowed:
                stopped += 1
                for blocked in decision.blockers:
                    blockers[blocked["code"]] += 1
                continue
            (agreed if predicted == actual else disagreed).append(row)

    auto_count = len(agreed) + len(disagreed)
    print(f"살펴본 건: {len(cases)}건")
    print(f"  자동 진행 가능: {auto_count}건 ({auto_count / len(cases) * 100:.0f}%)")
    print(f"  사람에게 넘어갔을 건: {stopped}건")
    if auto_count:
        print(f"\n담당자 선택과 일치: {len(agreed)}/{auto_count}건 "
              f"({len(agreed) / auto_count * 100:.0f}%)")
    if disagreed:
        print("\n불일치 (임계값을 다시 볼 지점):")
        for row in disagreed[:20]:
            gap = f"{row['score_gap']:g}점차" if row["score_gap"] is not None else "점수차 없음"
            print(f"  - {row['mr_name']}: 자동 {row['predicted']} vs 담당자 {row['actual']} ({gap})")
    if blockers:
        print("\n사람에게 넘어간 이유:")
        for code, count in blockers.most_common():
            print(f"  - {code}: {count}건")

    print("\n--- 진단 ---")
    print("순위에 오른 견적 수:  " + " · ".join(
        f"{size}건→{count}개 케이스" for size, count in sorted(ranking_sizes.items())
    ))
    print("실제 회신한 협력사 수: " + " · ".join(
        f"{size}곳→{count}개 케이스" for size, count in sorted(responded_sizes.items())
    ))
    if shrunk:
        print(
            f"\n⚠ {shrunk}개 케이스는 회신 수보다 순위 건수가 적습니다. "
            "옛 엔진이 규격 평가가 없는 견적을 순위에서 뺀 흔적일 수 있어, "
            "경쟁 부족 판정이 실제보다 과하게 잡혔을 수 있습니다."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
