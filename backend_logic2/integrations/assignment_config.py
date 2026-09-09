# 카테고리(ERPNext Item Group)별 담당자 매핑
CATEGORY_MANAGER_MAP = {
    "Products": ["gyalsrla57@gmail.com"],
    "사무용품": ["kcmini03@naver.com"],
    "원자재": ["purchaser2@example.com"],
    "소모품": ["purchaser3@example.com"],
}

# 전체 조회가 가능한 관리자 계정 목록
SUPER_ADMINS = [
    "Administrator",
    "admin@example.com",
]


def can_access_category(item_group: str, user_email: str | None) -> bool:
    """사용자가 해당 카테고리 담당자이거나 관리자인지 검증"""
    if not user_email or user_email in SUPER_ADMINS:
        return True

    allowed_users = CATEGORY_MANAGER_MAP.get(item_group, [])
    return user_email in allowed_users