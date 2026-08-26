"""Material Request 카테고리(Item Group) 기반 업무 분담 및 담당자 지정/이관 노드 (Step 3: MR 업무 분담).

주요 기능:
  - ERPNext Material Request의 품목 `Item Group`(예: 사무용품, 시약 등)과
    ERPNext 사용자에게 부여된 `Role Profile`(역할 프로필)을 1:1 매칭하여 담당자를 자동 배정합니다.
  - 예외 상황 및 다중 품목 예외 처리 포함.
  - ⚠️ [수정] 배정/이관 시 erp_add_comment(타임라인 댓글)만으로는 아무에게도
    알림이 가지 않아서, 실제 알림 채널 2개를 추가로 호출하도록 변경함:
      1) erp_assign_to  → ERPNext 표준 'Assign To' (종모양 알림 + Assigned To 위젯)
      2) erp_notify_assignee_email → 확실하게 즉시 가는 이메일 알림
    두 채널 중 하나가 권한/설정 문제로 실패해도 나머지 흐름은 계속되도록
    각각 try/except로 감쌈 (배정 자체가 실패 처리되면 안 되므로).
"""

from collections import Counter
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(CURRENT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.append(PARENT_DIR)

from erp_client import (
    erp_add_comment,
    erp_assign_to,
    erp_notify_assignee_email,
    erp_get,
    erp_get_one,
    ERPNextAPIError,
)

# ── ERPNext Item Group <-> Role Profile (담당자) 매칭 규칙 ──
DEFAULT_ASSIGNMENT_RULES = {
    # 1. Item Group(카테고리) <-> 담당자 & Role Profile 매칭
    "item_group_rules": {
        "시약": {
            "role_profile": "시약",
            "user_id": "ksh_hee@naver.com",
            "name": "김시약",
            "dept": "시약 담당",
        },
        "사무용품": {
            "role_profile": "사무용품",
            "user_id": "dataireview@naver.com",
            "name": "김사무용품",
            "dept": "사무용품 담당",
        },
        # "안전용품": {
        #     "role_profile": "안전용품",
        #     "user_id": "",
        #     "name": "이안전용품",
        #     "dept": "안전용품 담당",
        # },
    },
    # 2. 기본 담당자 (Fallback)
    "default_buyer": {
        "role_profile": "Purchase",
        "user_id": "parkdongkwan0814@gmail.com",
        "name": "박동관 (기본 구매담당자)",
        "dept": "구매팀",
    },
}


def _find_erp_user_by_role(role_name: str) -> dict | None:
    """ERPNext API를 통해 특정 Role(예: '시약', '사무용품')을 가진 유저를 동적으로 조회합니다."""
    try:
        roles = erp_get("Has Role", filters=[["role", "=", role_name]], fields=["parent"])
        if not roles or not roles[0].get("parent"):
            return None

        user_email = roles[0].get("parent")
        user_doc = erp_get_one("User", user_email)
        if not user_doc:
            print(f"[알림] ERPNext Role '{role_name}' 보유 유저({user_email}) 문서 정보 조회 불가")
            return None

        user_name = user_doc.get("full_name") or user_doc.get("first_name") or user_email
        return {
            "role_profile": role_name,
            "user_id": user_email,
            "name": user_name,
            "dept": f"{role_name} 담당",
        }
    except Exception as e:
        print(f"[알림] ERPNext Role '{role_name}' 동적 조회 중 예외 발생: {e}")
        return None


def evaluate_mr_assignment_rule(mr: dict, rules: dict | None = None) -> dict:
    """MR(Material Request)의 Item Group들과 ERPNext Role Profile을 매칭하여 담당자를 판별한다."""
    rules = rules or DEFAULT_ASSIGNMENT_RULES
    items = mr.get("items", [])

    if not items:
        fallback = rules["default_buyer"]
        return {
            "assignee": fallback,
            "rule_type": "default",
            "matched_group": None,
            "reason": f"MR 내 품목 정보가 없어 기본 구매 담당자 [{fallback['name']}] 배정",
        }

    # 모든 품목의 카테고리 수집
    item_groups = [item.get("item_group") for item in items if item.get("item_group")]
    if not item_groups:
        fallback = rules["default_buyer"]
        return {
            "assignee": fallback,
            "rule_type": "default",
            "matched_group": None,
            "reason": f"품목 카테고리 정보가 존재하지 않아 기본 구매 담당자 [{fallback['name']}] 배정",
        }

    # 가장 빈도수가 높은 대표 카테고리 선정 (다중 품목 대응)
    group_counts = Counter(item_groups)
    primary_group, count = group_counts.most_common(1)[0]
    total_count = len(item_groups)
    multi_note = f" (총 {total_count}개 품목 중 대표 카테고리 '{primary_group}' {count}개 포함)" if total_count > 1 else ""

    # 1. 사전 정의된 Item Group 규칙 매칭 (시약, 사무용품 등)
    if primary_group in rules.get("item_group_rules", {}):
        matched = rules["item_group_rules"][primary_group]
        return {
            "assignee": matched,
            "rule_type": "item_group_role",
            "matched_group": primary_group,
            "reason": f"품목 카테고리(Item Group) '{primary_group}'과 일치하는 Role Profile({matched.get('role_profile')}) 담당자 [{matched['name']}] 배정{multi_note}",
        }

    # 2. ERPNext API 동적 조회 (ERPNext에 등록된 Role/Role Profile명과 일치하는 유저)
    dynamic_user = _find_erp_user_by_role(primary_group)
    if dynamic_user:
        return {
            "assignee": dynamic_user,
            "rule_type": "dynamic_erp_role",
            "matched_group": primary_group,
            "reason": f"ERPNext 내 '{primary_group}' Role 보유 유저 [{dynamic_user['name']}] 동적 배정{multi_note}",
        }

    # 3. 매칭되는 카테고리가 없을 경우 Fallback
    fallback = rules["default_buyer"]
    return {
        "assignee": fallback,
        "rule_type": "default",
        "matched_group": primary_group,
        "reason": f"일치하는 카테고리 매칭 규칙이 없어 기본 구매 담당자 [{fallback['name']}] 배정{multi_note}",
    }


def _notify_assignee(mr_name: str, assignee: dict, reason: str, matched_group: str) -> None:
    """
    담당자에게 실제 알림 2채널 발송.
    - erp_assign_to: 실패해도(권한 문제 등) 전체 흐름은 계속되게 함
    - erp_notify_assignee_email: assign_to가 막혀도 최소한 이메일은 가도록 별도 시도
    """
    description = (
        f"[MR 업무 분담] {mr_name} 배정 완료\n"
        f"품목 카테고리: {matched_group}\n"
        f"배정 사유: {reason}"
    )

    try:
        erp_assign_to("Material Request", mr_name, assignee["user_id"], description=description)
        print(f"[알림] ERPNext Assign To 완료 → {assignee['user_id']}")
    except ERPNextAPIError as e:
        print(f"[경고] Assign To 실패 (권한 부족 가능성) — {e}")

    try:
        erp_notify_assignee_email(
            "Material Request",
            mr_name,
            assignee["user_id"],
            subject=f"[구매요청 배정] {mr_name} 담당자로 지정되었습니다",
            content=f"<p>{description.replace(chr(10), '<br>')}</p>",
        )
        print(f"[알림] 이메일 발송 완료 → {assignee['user_id']}")
    except ERPNextAPIError as e:
        print(f"[경고] 알림 이메일 발송 실패 — {e}")


def assign_material_request(
    mr_name: str,
    override_assignee: str | None = None,
    rules: dict | None = None,
) -> dict:
    """MR ID를 입력받아 Item Group 기반으로 담당자를 자동 분류/배정하고, 폼 데이터를 반환한다."""
    mr = erp_get_one("Material Request", mr_name)
    if not mr:
        raise ValueError(f"Material Request를 찾을 수 없습니다: {mr_name}")

    if override_assignee:
        assignment_result = {
            "assignee": {
                "role_profile": "Manual",
                "user_id": override_assignee,
                "name": override_assignee,
                "dept": "수동 지정 부서",
            },
            "rule_type": "manual_override",
            "matched_group": "수동지정",
            "reason": f"사용자 수동 지정 담당자: {override_assignee}",
        }
    else:
        assignment_result = evaluate_mr_assignment_rule(mr, rules)

    assignee = assignment_result["assignee"]
    reason = assignment_result["reason"]
    matched_group = assignment_result.get("matched_group") or "미지정"

    # ERPNext 타임라인에 댓글 기록 (감사/이력용 — 알림은 아님)
    comment_text = (
        f"[3. MR 업무 분담 카테고리 매칭 완료]\n"
        f"• 품목 카테고리 (Item Group): {matched_group}\n"
        f"• ERPNext Role Profile: {assignee.get('role_profile')}\n"
        f"• 배정 담당자: {assignee['name']} ({assignee['user_id']})\n"
        f"• 배정 사유: {reason}"
    )
    try:
        erp_add_comment("Material Request", mr_name, comment_text)
    except Exception as e:
        print(f"[경고] ERPNext 댓글 작성 실패 (로컬/테스트 환경 가능성): {e}")

    # ⭐ 실제 알림 발송 (여기가 이번에 추가된 핵심)
    _notify_assignee(mr_name, assignee, reason, matched_group)

    # 작성 폼 제공용 데이터 구조체
    form_data = {
        "mr_name": mr_name,
        "title": f"MR 업무 분담 정보 - {mr_name}",
        "transaction_date": mr.get("transaction_date"),
        "schedule_date": mr.get("schedule_date"),
        "department": mr.get("department"),
        "item_group": matched_group,
        "role_profile": assignee.get("role_profile"),
        "assignee_id": assignee["user_id"],
        "assignee_name": assignee["name"],
        "assignee_dept": assignee["dept"],
        "rule_type": assignment_result["rule_type"],
        "assignment_reason": reason,
        "items_summary": [
            {
                "item_code": item.get("item_code"),
                "item_name": item.get("item_name"),
                "item_group": item.get("item_group"),
                "qty": item.get("qty"),
                "uom": item.get("stock_uom"),
            }
            for item in mr.get("items", [])
        ],
    }

    print(f"\n[3. MR 카테고리별 업무 분담 완료]")
    print(f"  - MR ID: {mr_name}")
    print(f"  - 품목 카테고리(Item Group): {matched_group}")
    print(f"  - ERPNext Role Profile: {assignee.get('role_profile')}")
    print(f"  - 담당자: {assignee['name']} ({assignee['user_id']})")
    print(f"  - 배정 사유: {reason}")

    return form_data


def reassign_material_request(
    mr_name: str,
    new_assignee_id: str,
    new_assignee_name: str | None = None,
    transfer_reason: str | None = None,
) -> dict:
    """담당 업무를 다른 담당자에게 이관(Reassign/Transfer)한다."""
    reason = transfer_reason or "담당자 업무 조율 및 이관"
    assignee_name = new_assignee_name or new_assignee_id

    comment_text = (
        f"[3. MR 업무 이관 처리]\n"
        f"• 신규 담당자: {assignee_name} ({new_assignee_id})\n"
        f"• 이관 사유: {reason}"
    )
    try:
        erp_add_comment("Material Request", mr_name, comment_text)
    except Exception as e:
        print(f"[경고] ERPNext 이관 댓글 작성 실패: {e}")

    # ⭐ 이관도 마찬가지로 실제 알림 발송
    _notify_assignee(
        mr_name,
        {"user_id": new_assignee_id, "name": assignee_name},
        reason,
        matched_group="이관",
    )

    print(f"\n[3. MR 업무 이관 완료]")
    print(f"  - MR ID: {mr_name}")
    print(f"  - 신규 담당자: {assignee_name} ({new_assignee_id})")
    print(f"  - 이관 사유: {reason}")

    return {
        "mr_name": mr_name,
        "status": "reassigned",
        "new_assignee_id": new_assignee_id,
        "new_assignee_name": assignee_name,
        "transfer_reason": reason,
    }


if __name__ == "__main__":
    # 테스트용 대화형 스크립트
    mr_name = input("업무 분담을 진행할 Material Request ID (예: MAT-MR-2026-00001): ").strip()
    if mr_name:
        result = assign_material_request(mr_name)

        reassign_choice = input("\n다른 담당자에게 이관하시겠습니까? (y/n): ").strip().lower()
        if reassign_choice == "y":
            new_id = input("신규 담당자 ID/이메일: ").strip()
            new_name = input("신규 담당자 이름: ").strip()
            reason = input("이관 사유: ").strip()
            reassign_material_request(mr_name, new_id, new_name, reason)