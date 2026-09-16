"""Live RFQ recommendation scores based on completed BiddingFlow evaluations."""

from backend_logic2.repositories.deliveries import SCORECARD_FIELDS
from procurement_db import get_connection


def aggregate_evaluations(rows):
    grouped = {}
    for row in rows:
        card = row.get("scorecard")
        supplier = row.get("supplier")
        if not supplier or not isinstance(card, dict):
            continue
        scores = {key: float(card[key]) for key in SCORECARD_FIELDS
                  if type(card.get(key)) in (int, float) and 1 <= card[key] <= 5}
        if not all(key in scores for key in ("leadTime", "quality", "service", "communication")):
            continue
        grouped.setdefault(supplier, []).append(scores)
    result = {}
    for supplier, cards in grouped.items():
        averages = {}
        for key in SCORECARD_FIELDS:
            values = [card[key] for card in cards if key in card]
            if values:
                averages[key] = sum(values) / len(values)
        result[supplier] = {
            "scores": averages,
            # Each PO has equal weight; excluded price is not treated as zero.
            "average_score": sum(sum(card.values()) / len(card) for card in cards) / len(cards),
            "evaluation_count": len(cards),
        }
    return result


def get_supplier_recommendations(names):
    names = sorted({str(name).strip() for name in names if str(name).strip()})
    if not names:
        return {}
    with get_connection() as connection:
        rows = connection.execute(
            """SELECT supplier, scorecard FROM procurement.purchase_order_delivery
               WHERE supplier = ANY(%(names)s::varchar[])
                 AND scorecard_status = 'COMPLETED' AND delivery_status = 'FULL'
                 AND scorecard IS NOT NULL""", {"names": names}
        ).fetchall()
    return aggregate_evaluations(rows)


def attach_supplier_recommendations(cases):
    names = set()
    case_names = []
    for case in cases:
        values = (case.get("workflow_snapshot") or {}).get("values") or {}
        rows = [*(values.get("supplier_candidates") or values.get("existing_supplier_candidates") or []),
                *(values.get("quotation_ranking") or []),
                *((case.get("quotation_snapshot") or {}).get("quotations") or [])]
        suppliers = {str(row.get("supplier") or row.get("supplier_name") or row.get("name") or "").strip()
                     for row in rows if isinstance(row, dict)} - {""}
        case_names.append(suppliers)
        names.update(suppliers)
    aggregates = get_supplier_recommendations(names)
    for case, suppliers in zip(cases, case_names):
        case["supplier_recommendations"] = {name: aggregates[name] for name in suppliers if name in aggregates}
