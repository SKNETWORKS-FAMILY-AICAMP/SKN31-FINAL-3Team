from __future__ import annotations


# ============================================================
# ERPNext Item Group → 구매 담당자
# ============================================================

CATEGORY_MANAGER_MAP: dict[str, str] = {
    "Products": "gyalsrla57@gmail.com",
    "사무용품": "kcmini03@naver.com",
    "원자재": "purchaser2@example.com",
    "소모품": "purchaser3@example.com",
}


# 모든 MR 조회/처리가 가능한 관리자
SUPER_ADMINS = {
    "administrator",
    "admin@example.com",
}


def normalize_user_id(value: str | None) -> str:
    """사용자 ID/이메일 비교용 정규화."""
    return str(value or "").strip().lower()


def get_category_manager(item_group: str | None) -> str | None:
    """
    Item Group 담당자를 반환.

    매핑되지 않은 카테고리는 None.
    → 일반 담당자 화면에는 뜨지 않고 관리자만 확인 가능.
    """
    group = str(item_group or "").strip()

    if not group:
        return None

    manager = CATEGORY_MANAGER_MAP.get(group)

    if not manager:
        return None

    return str(manager).strip()


def is_super_admin(user_id: str | None) -> bool:
    return normalize_user_id(user_id) in SUPER_ADMINS


def is_same_user(
    left: str | None,
    right: str | None,
) -> bool:
    if not left or not right:
        return False

    return normalize_user_id(left) == normalize_user_id(right)


def can_access_category(
    item_group: str | None,
    user_id: str | None,
) -> bool:
    """
    관리자는 모든 카테고리 접근 가능.
    일반 사용자는 자신이 담당하는 카테고리만 접근 가능.
    """
    if not user_id:
        return False

    if is_super_admin(user_id):
        return True

    manager = get_category_manager(item_group)

    return is_same_user(manager, user_id)