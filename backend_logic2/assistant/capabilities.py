"""Closed execution registry. Similarity can select a capability, never add one."""

CAPABILITIES = (
    {"id": "search_cases", "purpose": "구매 작업을 품목·단계·대기주체·납기·첨부 여부로 검색", "examples": ["마우스 건 좀 찾아줘", "아직 회신 안 온 건"], "result": "권한 내 MR 목록·화면 바로가기"},
    {"id": "count_cases", "purpose": "조건에 맞는 구매 작업의 전체 건수 집계", "examples": ["총 얼마나 남았어?", "몇 개야?"], "result": "조회 범위의 정확한 건수·목록"},
    {"id": "case_detail", "purpose": "특정 MR 현재 단계와 다음 행동 조회", "examples": ["그 두 번째 건은 어디서 막혔어?"], "result": "최신 저장 상태·담당 주체·바로가기"},
    {"id": "my_tasks", "purpose": "현재 사용자의 담당 건 또는 직접 결정할 작업 조회", "examples": ["내가 처리할 일 있어?", "내 담당 중 선택 대기 몇 개야?"], "result": "서버 담당 범위와 현재 역할을 적용한 MR 목록·건수"},
    {"id": "feature_guide", "purpose": "기능 위치 및 바로가기; 실행 요청도 직접 실행하지 않고 안내", "examples": ["메일 보내줘", "협력사 다시 뽑으려면 어디야?"], "result": "등록된 화면 바로가기"},
    {"id": "usage_help", "purpose": "기능 사용법·절차·오류 안내를 내부 도움말에서 찾음", "examples": ["승인 버튼이 왜 안 보여?"], "result": "근거가 있는 사용법"},
    {"id": "clarify", "purpose": "대상·의미가 갈려 서로 다른 조회 결과가 나올 때만 확인 질문", "examples": ["내 승인: 결재만인지 선택 업무도인지 불명확"], "result": "짧은 질문과 최대 3개 선택지"},
    {"id": "unsupported", "purpose": "지원하지 않는 실시간 재고·연락처·정책값 조회 또는 무관한 요청", "examples": ["실제 재고가 몇 개야?"], "result": "제한 설명과 가능한 관련 화면 안내; 조회한 척 금지"},
)

INTENTS = {"search_cases": "case_query", "count_cases": "case_query", "case_detail": "case_status", "my_tasks": "case_query", "feature_guide": "feature_guide", "usage_help": "help", "clarify": "clarification", "unsupported": "unsupported"}


def normalize_capability(plan):
    if plan.capability:
        plan.intent = INTENTS[plan.capability]
        if plan.capability == "count_cases":
            plan.filters.count_requested = True
        if plan.capability == "my_tasks" and plan.filters.task_scope == "visible":
            plan.filters.task_scope = "actionable"
    else:
        plan.capability = {"case_query": "count_cases" if plan.filters.count_requested else "search_cases", "case_status": "case_detail", "help": "usage_help", "feature_guide": "feature_guide", "clarification": "clarify"}.get(plan.intent, "unsupported")
    if plan.filters.task_scope == 'actionable' and not any((plan.filters.waiting_for, plan.filters.stage, plan.filters.status, plan.filters.exact_reference)):
        plan.filters.waiting_for = 'decision'
    return plan
