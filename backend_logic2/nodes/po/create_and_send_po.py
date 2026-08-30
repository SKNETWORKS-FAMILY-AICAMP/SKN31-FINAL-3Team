"""
nodes/po/create_and_send_po.py
- 선정된 Supplier Quotation을 기반으로 Purchase Order 생성 + Submit + 이메일 발송

데이터 출처:
  backend_logic2.nodes.quotation.sq_evaluation.get_quotations_for_rfq

흐름:
  1. RFQ에 제출된 Supplier Quotation 조회
  2. 선정된 Supplier의 견적서 선택
  3. 해당 SQ의 품목/가격/납기를 이용해 PO Draft 생성
  4. PO Submit
  5. 공급사 이메일로 PO 발송
"""

import sys
import os
import argparse
from datetime import date

from backend_logic2.integrations.erp_client import (
    erp_post,
    erp_submit,
    erp_send_email,
    ERPNextAPIError,
    erp_get,
    is_test_mode,
)

from backend_logic2.nodes.quotation.sq_evaluation import (
    get_quotations_for_rfq,
)


ERP_DOMAIN = os.getenv(
    "ERP_DOMAIN",
    "http://13.209.103.102:8080",
)

ERP_PORTAL_PATH_TEMPLATE = os.getenv(
    "ERP_PORTAL_PATH_TEMPLATE",
    "/orders/{po_name}",
)


def create_and_send_po(
    rfq_name: str,
    supplier_id: str,
    *,
    send_email: bool = True,
):
    print("\n=== PO(Purchase Order) 생성 및 발송 ===")
    print(f"RFQ: {rfq_name}")
    print(f"선정 공급사: {supplier_id}")

    test_mode = is_test_mode()

    print(
        f"현재 환경: "
        f"{'테스트 모드 (실제 메일 발송 차단됨)' if test_mode else '운영 모드'}"
    )
    print(f"PO 이메일: {'발송 요청' if send_email else '발송 안 함'}\n")

    # ---------------------------------------------------------
    # 1. RFQ에 제출된 Supplier Quotation 조회
    # ---------------------------------------------------------

    try:
        quotations = get_quotations_for_rfq(rfq_name)

    except ERPNextAPIError as e:
        print(f"[에러] Supplier Quotation 조회 실패: {e}")
        sys.exit(1)

    if not quotations:
        print(f"[오류] RFQ '{rfq_name}'에 제출된 Supplier Quotation이 없습니다.")
        sys.exit(1)

    # ---------------------------------------------------------
    # 2. 선정된 Supplier의 견적서 찾기
    # ---------------------------------------------------------

    supplier_quotations = [
        q
        for q in quotations
        if q.get("supplier") == supplier_id
    ]

    if not supplier_quotations:
        print(
            f"[오류] 공급사 '{supplier_id}'가 "
            f"RFQ '{rfq_name}'에 제출한 견적이 없습니다."
        )
        sys.exit(1)

    # 같은 공급사가 동일 RFQ에 여러 견적을 제출한 경우
    # 임의로 하나를 선택하면 안 됨.
    if len(supplier_quotations) > 1:
        print(
            f"[오류] 공급사 '{supplier_id}'의 Supplier Quotation이 "
            f"{len(supplier_quotations)}건 존재합니다."
        )

        for q in supplier_quotations:
            print(
                f"  - {q.get('name')} "
                f"(견적일: {q.get('transaction_date')}, "
                f"총액: {q.get('grand_total')} {q.get('currency')})"
            )

        print("어떤 견적서를 기준으로 PO를 만들지 명확히 선택해야 합니다.")
        sys.exit(1)

    quotation = supplier_quotations[0]

    quotation_name = quotation.get("name")
    supplier_items = quotation.get("items") or []

    print(f"사용 Supplier Quotation: {quotation_name}")

    if not supplier_items:
        print(
            f"[오류] Supplier Quotation '{quotation_name}'에 "
            f"품목이 존재하지 않습니다."
        )
        sys.exit(1)

    # ---------------------------------------------------------
    # 3. 기존 PO 중복 생성 방지
    # ---------------------------------------------------------

    try:
        existing_pos = erp_get(
            "Purchase Order",
            filters=[
                [
                    "Purchase Order Item",
                    "supplier_quotation",
                    "=",
                    quotation_name,
                ],
                ["docstatus", "!=", 2],
            ],
            fields=["name"],
            limit=100,
        )

    except Exception as e:
        print(f"[경고] 기존 PO 중복 여부 확인 실패: {e}")
        existing_pos = []

    if existing_pos:
        po_names = sorted(
            {
                row["name"]
                for row in existing_pos
                if row.get("name")
            }
        )

        print(
            f"[오류] Supplier Quotation '{quotation_name}'에 대해 "
            f"이미 PO가 존재합니다: {po_names}"
        )

        print("중복 발주 방지를 위해 PO 생성을 중단합니다.")
        sys.exit(1)

    # ---------------------------------------------------------
    # 4. 납기일 확인
    #
    # sq_evaluation에서는 Supplier Quotation Item의
    # expected_delivery_date를 사용함.
    # ---------------------------------------------------------

    missing_delivery_items = [
        item.get("item_code")
        for item in supplier_items
        if not item.get("expected_delivery_date")
    ]

    if missing_delivery_items:
        print(
            "[오류] 아래 견적 품목에 "
            "expected_delivery_date가 없습니다."
        )

        for item_code in missing_delivery_items:
            print(f"  - {item_code}")

        print("Supplier Quotation의 납기정보를 먼저 확인해주세요.")
        sys.exit(1)

    # ---------------------------------------------------------
    # 5. 과거 납기일 확인
    # ---------------------------------------------------------

    today = date.today().isoformat()

    past_date_items = [
        item
        for item in supplier_items
        if str(item.get("expected_delivery_date")) < today
    ]

    if past_date_items:
        print(
            f"\n[확인 필요] 아래 품목의 견적 납기일이 "
            f"오늘({today})보다 과거입니다."
        )

        for item in past_date_items:
            print(
                f"  - {item.get('item_code')}: "
                f"{item.get('expected_delivery_date')}"
            )

        answer = input(
            "그래도 진행하시겠습니까? "
            "진행 시 해당 품목의 납기일을 오늘로 조정합니다. (y/n): "
        ).strip().lower()

        if answer != "y":
            print("PO 생성을 중단합니다.")
            sys.exit(1)

        for item in past_date_items:
            old_date = item.get("expected_delivery_date")

            print(
                f"  -> {item.get('item_code')} "
                f"{old_date} -> {today}"
            )

            item["expected_delivery_date"] = today

    # ---------------------------------------------------------
    # 6. Purchase Order Item 구성
    # ---------------------------------------------------------

    po_items = []

    for item in supplier_items:

        po_item = {
            "item_code": item["item_code"],
            "qty": item["qty"],
            "rate": item["rate"],
            "schedule_date": item["expected_delivery_date"],
            "supplier_quotation": quotation_name,
        }

        # SQ에서 UOM이 존재할 때만 전달
        if item.get("uom"):
            po_item["uom"] = item["uom"]

        # Supplier Quotation Item의 child row name이 있다면
        # supplier_quotation_item까지 연결
        if item.get("name"):
            po_item["supplier_quotation_item"] = item["name"]

        po_items.append(po_item)

    # ---------------------------------------------------------
    # 7. PO Payload
    # ---------------------------------------------------------

    po_payload = {
        "supplier": supplier_id,
        "transaction_date": today,
        "items": po_items,

        # PO 대표 Required By 날짜
        "schedule_date": min(
            item["schedule_date"]
            for item in po_items
        ),
    }

    # ---------------------------------------------------------
    # 8. PO Draft 생성
    # ---------------------------------------------------------

    try:
        print("\n1. ERP에 Purchase Order Draft 생성...")

        new_po = erp_post(
            "Purchase Order",
            po_payload,
        )

        po_name = new_po.get("name")

        print(f"   -> 생성 완료: {po_name}")

    except ERPNextAPIError as e:
        print(f"[에러] PO Draft 생성 실패: {e}")
        sys.exit(1)

    # ---------------------------------------------------------
    # 9. PO Submit
    # ---------------------------------------------------------

    try:
        print("2. PO Submit 처리...")

        erp_submit(
            "Purchase Order",
            po_name,
        )

        print("   -> Submit 완료")

    except ERPNextAPIError as e:

        print("=============================================")
        print(
            f"⚠️ PO '{po_name}'는 Draft로 생성되었지만 "
            f"Submit에 실패했습니다."
        )
        print(f"원인: {e}")
        print(
            "ERP에서 해당 PO를 직접 확인해주세요. "
            "스크립트를 재실행하면 중복 Draft가 생길 수 있습니다."
        )
        print("=============================================")

        sys.exit(1)

    # ---------------------------------------------------------
    # 10. 이메일 발송 여부
    # ---------------------------------------------------------

    if not send_email:
        print("\n3. PO 이메일 발송 생략")

        return {
            "name": po_name,
            "supplier_quotation": quotation_name,
            "status": "submitted",
            "email_sent": False,
        }

    # ---------------------------------------------------------
    # 11. Supplier 이메일 조회
    # ---------------------------------------------------------

    print(f"\n3. 공급사({supplier_id}) 이메일 조회")

    recipient_email = None

    try:
        supplier_info_list = erp_get(
            "Supplier",
            filters=[
                ["name", "=", supplier_id],
            ],
            fields=[
                "email_id",
            ],
        )

        if (
            supplier_info_list
            and supplier_info_list[0].get("email_id")
        ):
            recipient_email = supplier_info_list[0]["email_id"]

    except Exception as e:
        print(f"[오류] 공급사 이메일 조회 실패: {e}")

    if not recipient_email:

        print("=============================================")
        print(
            f"⚠️ PO({po_name})는 정상 생성/확정되었지만 "
            f"공급사 이메일을 확인할 수 없습니다."
        )
        print(
            f"ERP에서 '{supplier_id}' 공급사의 이메일을 확인한 뒤 "
            f"수동 발송해주세요."
        )
        print("=============================================\n")

        return {
            "name": po_name,
            "supplier_quotation": quotation_name,
            "status": "submitted",
            "email_sent": False,
        }

    # ---------------------------------------------------------
    # 12. 이메일 발송
    # ---------------------------------------------------------

    portal_link = (
        ERP_DOMAIN
        + ERP_PORTAL_PATH_TEMPLATE.format(
            po_name=po_name
        )
    )

    subject = f"발주서(PO) 안내 - {po_name}"

    content = f"""
    <p>안녕하세요.</p>

    <p>
        귀사의 견적서
        <b>{quotation_name}</b>를 바탕으로
        발주서(<b>{po_name}</b>)를 송부합니다.
    </p>

    <p>
        아래 링크에서 발주 내역을 확인해 주세요.
    </p>

    <p>
        <a href="{portal_link}" target="_blank">
            발주서 상세 확인하기
        </a>
    </p>
    """

    try:
        erp_send_email(
            "Purchase Order",
            po_name,
            recipient_email,
            subject,
            content,
        )

        print(
            f"   -> 이메일 발송 완료 "
            f"(수신: {recipient_email})"
        )

    except ERPNextAPIError as e:

        print(
            f"[오류] PO({po_name})는 생성/확정되었으나 "
            f"이메일 발송에 실패했습니다: {e}"
        )

        return {
            "name": po_name,
            "supplier_quotation": quotation_name,
            "status": "submitted",
            "email_sent": False,
            "email_error": str(e),
        }

    print("\n=============================================")
    print(f"✅ PO 생성 및 처리 완료: {po_name}")
    print(f"   기준 견적: {quotation_name}")
    print("=============================================\n")

    return {
        "name": po_name,
        "supplier_quotation": quotation_name,
        "status": "submitted",
        "email_sent": not test_mode,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Supplier Quotation 기반 PO 생성 및 공급사 발송"
    )

    parser.add_argument(
        "rfq_name",
        help="RFQ 이름 (예: PUR-RFQ-2026-00270)",
    )

    parser.add_argument(
        "supplier_id",
        help="선정된 공급사 ID",
    )

    parser.add_argument(
        "--no-email",
        action="store_true",
        help="PO 생성/Submit만 하고 이메일은 발송하지 않음",
    )

    args = parser.parse_args()

    create_and_send_po(
        args.rfq_name,
        args.supplier_id,
        send_email=not args.no_email,
    )