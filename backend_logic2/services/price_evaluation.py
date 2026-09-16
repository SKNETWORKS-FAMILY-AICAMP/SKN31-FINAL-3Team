"""Persistable supplier price scores from the RFQ's actual item quotations."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def build_price_evaluations(quotations, item_code, rfq_name=None):
    candidates = {}
    for document in quotations:
        if str(document.get("docstatus")) == "2":
            continue
        supplier = document.get("supplier")
        if not supplier:
            continue
        # ERP projections contain header + items; legacy snapshots are flat.
        for item in document.get("items") or [document]:
            if not item_code or item.get("item_code") != item_code:
                continue
            if rfq_name and item.get("request_for_quotation") and item["request_for_quotation"] != rfq_name:
                continue
            try:
                rate = Decimal(str(item.get("rate") or item.get("net_rate")))
                if not rate.is_finite() or rate <= 0:
                    continue
            except (InvalidOperation, ValueError, TypeError):
                continue
            basis = {
                "quotation": document.get("quotation_name") or document.get("name"),
                "rate": float(rate),
                "currency": item.get("currency") or document.get("currency"),
                "uom": item.get("uom") or document.get("uom"),
            }
            if basis not in candidates.setdefault(supplier, []):
                candidates[supplier].append(basis)
    comparable = [row for supplier_rows in candidates.values() for row in supplier_rows]
    if not comparable:
        return {}
    units = {(row["currency"], row["uom"]) for row in comparable}
    highest = max(Decimal(str(row["rate"])) for row in comparable)
    result = {}
    for supplier, rows in candidates.items():
        if len(units) > 1:
            result[supplier] = {"reason": "견적의 통화와 단위가 같아야 단가를 비교할 수 있습니다."}
            continue
        rates = {row["rate"] for row in rows}
        if len(rates) != 1:
            result[supplier] = {"reason": "동일 협력사의 견적 단가가 여러 개이므로 적용 견적을 확인해주세요."}
            continue
        rate = Decimal(str(rows[0]["rate"]))
        score = float(max(Decimal(1), rate / highest * 5).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
        result[supplier] = {
            "score": score, "item_code": item_code, "rfq_name": rfq_name,
            "rate": float(rate), "highest_rate": float(highest),
            "supplier_count": len(candidates), "quotations": rows,
            "reason": f"견적 단가 {rate:,.2f} ÷ 최고 단가 {highest:,.2f} × 5 (비교 {len(candidates)}개사)",
        }
    return result
