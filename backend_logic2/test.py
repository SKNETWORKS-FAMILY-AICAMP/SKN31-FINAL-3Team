"""
nodes/register_test_supplier.py — 본인 이메일로 테스트용 Supplier 하나 등록.

이렇게 만들어두면, create_and_send_rfq.py 돌릴 때 TEST_RECIPIENT_OVERRIDE
같은 우회 로직 없이, 그냥 이 공급사 이름을 그대로 넣으면 본인 메일로 옴.

실행: python nodes/register_test_supplier.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from erp_client import erp_post, erp_get_one, ERPNextAPIError

SUPPLIER_NAME = "테스트공급사(본인)"
MY_EMAIL = "pdk0814@naver.com"

if __name__ == "__main__":
    try:
        existing = erp_get_one("Supplier", SUPPLIER_NAME)
        print(f"이미 존재함: {existing['name']} ({existing.get('email_id')})")
    except ERPNextAPIError:
        created = erp_post("Supplier", {
            "supplier_name": SUPPLIER_NAME,
            "email_id": MY_EMAIL,
            "supplier_group": "All Supplier Groups",
            "country": "Korea, Republic of",
            "supplier_type": "Company",
        })
        print(f"생성 완료: {created['name']} ({MY_EMAIL})")