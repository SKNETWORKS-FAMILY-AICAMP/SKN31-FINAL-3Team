"""
inspect_email_records.py — 특정 이메일과 관련된 User/Contact/Supplier를
전부 찾아서 보여줌 (뭐가 중복됐는지, 몇 개 있는지 눈으로 확인하는 용도)

실행: python inspect_email_records.py
"""

from erp_client import erp_get

EMAIL = input("확인할 이메일 입력: ").strip()

print(f"\n{'='*60}")
print(f"'{EMAIL}' 관련 레코드 전체 조회")
print("=" * 60)

print("\n--- User ---")
users = erp_get("User", filters=[["email", "=", EMAIL]], fields=["name", "user_type", "enabled"])
for u in users or []:
    print(f"  {u}")
print(f"총 {len(users or [])}건")

print("\n--- Contact ---")
contacts = erp_get("Contact", filters=[["email_id", "=", EMAIL]], fields=["name", "first_name"])
for c in contacts or []:
    print(f"  {c}")
print(f"총 {len(contacts or [])}건")

print("\n--- Supplier (이메일 필드로) ---")
suppliers = erp_get("Supplier", filters=[["email_id", "=", EMAIL]], fields=["name", "supplier_name"])
for s in suppliers or []:
    print(f"  {s}")
print(f"총 {len(suppliers or [])}건")

print(f"\n{'='*60}")
print("2개 이상 나오는 항목이 있으면, 그게 중복이라 충돌나는 원인입니다.")
print("ERPNext 화면에서 위 name들 하나씩 열어보고, 중복된 것 중 하나만 남기고 지워주세요.")