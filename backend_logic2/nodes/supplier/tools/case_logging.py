"""
tools/case_logging.py - procurement_case / case_status_history / ai_decision_log
3개 테이블에 기록하는 공용 헬퍼 (MR 단위 재설계, 2026-08-31).

create_procurement_tracking_tables.py(레포 루트)로 테이블을 먼저 만들어야
동작함. 여기 함수들은 전부 "로깅이 실패해도 본 파이프라인은 절대 안
끊기게" 방어적으로 짜여 있음 - procurement_db 모듈을 못 찾거나 INSERT가
실패해도 print 경고만 남기고 조용히 넘어감.

사용 흐름(전체 그림, MR 단위):
  1. process_cli.py로 `--mr <MR명>` 실행 -> process_graph.py의
     route_entrypoint_command(그래프 맨 처음 노드, MR당 딱 1번만 실행됨)가
     create_case(mr_name=...)로 케이스 1건을 만들고, state에 case_id를
     심어둠.
  2. 이후 그래프의 모든 노드(check_mr_item, decide_bidding_choice,
     resolve_suppliers_choice, search_new_suppliers, ...)는 state를 통해
     같은 case_id를 넘겨받아 재사용 - process_graph.py의 공용 wrapper가
     노드마다 자동으로 log_status_change()를 호출해서 상태전이를 남김
     (각 노드 함수 내부를 일일이 고칠 필요 없음).
  3. 그래프 안에서 실제 AI 판단이 벌어지는 지점(naver_contact_enrichment의
     사이트선택·연락처추출, web_search_based_tool의 회사명추출)마다
     log_ai_decision()으로 "어느 노드에서, 왜 그렇게 판단했는지" 기록.
     case_id는 2번에서 받은 값을 계속 관통시켜서 넘김.
  4. 단독 실행(각 파일 __main__ 블록 등, MR 컨텍스트 없음)일 땐 case_id를
     안 넘기면 됨 - 아래 모든 함수가 case_id=None을 안전하게 허용(로깅만
     스킵되고 파이프라인 자체는 그대로 동작).
"""

import uuid


def _get_conn_or_none():
    try:
        from procurement_db import get_connection
    except ImportError:
        print("    [case_logging] procurement_db 모듈을 못 찾음, 이 기록은 건너뜀")
        return None
    return get_connection


def create_case(mr_name=None, status="created"):
    """
    새 procurement_case 행을 만들고 case_id(uuid 문자열)를 반환. DB 연결이
    안 되거나 INSERT가 실패하면 None을 반환 - 호출부는 case_id가 None이어도
    계속 동작해야 함(아래 log_status_change/log_ai_decision이 다
    case_id=None을 허용하도록 짜여 있음).
    """
    get_connection = _get_conn_or_none()
    if get_connection is None:
        return None

    case_id = str(uuid.uuid4())
    try:
        with get_connection(autocommit=True) as conn:
            conn.execute(
                """
                INSERT INTO procurement.procurement_case (
                    case_id, mr_name, thread_id, status, stage
                ) VALUES (
                    %(case_id)s, %(mr_name)s, %(mr_name)s, 'RUNNING', 'ITEM_CHECK'
                )
                """,
                {"case_id": case_id, "mr_name": mr_name},
            )
    except Exception as e:
        print(f"    [case_logging] 케이스 생성 실패, 케이스 없이 진행: {e}")
        return None

    print(f"    [케이스 생성] {case_id} (mr_name={mr_name})")
    return case_id


def log_status_change(case_id, to_status, reason=None, from_status=None):
    """
    LangGraph 내부 상태전이를 case_status_history에만 기록한다.

    procurement_case.status/stage는 프론트용 안정 상태이며
    workflow_service.project_case_from_checkpoint()만 갱신한다. 내부 상태를
    같은 컬럼에 쓰면 WAITING_INPUT과 awaiting_quotation_check가 실행 순서에
    따라 서로 덮어쓰므로 여기서는 운영 read model을 절대 수정하지 않는다.
    case_id가 None이면(케이스 없이 단독 실행 등) 조용히 스킵한다.

    from_status 자동 보완(2026-08-31 추가): process_graph.py의
    _with_status_log 래퍼는 그래프 "노드" 레벨 전이(checking_mr_item ->
    resolving_suppliers 등)에서만 state["status"]로 from_status를 채워줌.
    근데 supplier_search.py/resolve_supplier_pool.py/
    register_candidate_suppliers.py처럼 노드 "안"에서 서브 파이프라인이
    자체적으로 여러 번 상태를 남기는 경우(searching -> collected ->
    search_completed 등)는 호출부가 from_status를 안 넘기면 매번 NULL로
    찍혔었음(DBeaver에서 실제로 확인됨). 호출부 하나하나에 이전 상태를
    손으로 들고 다니게 하는 대신, 여기서 from_status가 안 넘어오면
    case_status_history의 직전 to_status를 조회해서 자동으로 채운다. 따라서
    UI 상태값이 내부 그래프 상태 이력에 섞이지 않는다.
    """
    if not case_id:
        return
    get_connection = _get_conn_or_none()
    if get_connection is None:
        return

    try:
        with get_connection(autocommit=True) as conn:
            if from_status is None:
                row = conn.execute(
                    """
                    SELECT to_status
                    FROM procurement.case_status_history
                    WHERE case_id = %(case_id)s
                    ORDER BY occurred_at DESC, id DESC
                    LIMIT 1
                    """,
                    {"case_id": case_id},
                ).fetchone()
                if row:
                    from_status = row["to_status"] if isinstance(row, dict) else row[0]

            conn.execute(
                """
                INSERT INTO procurement.case_status_history (case_id, from_status, to_status, reason)
                VALUES (%(case_id)s, %(from_status)s, %(to_status)s, %(reason)s)
                """,
                {"case_id": case_id, "from_status": from_status, "to_status": to_status, "reason": reason},
            )
        print(f"    [케이스 상태] {case_id[:8]}... {from_status} -> '{to_status}'" + (f" ({reason})" if reason else ""))
    except Exception as e:
        print(f"    [case_logging] 상태기록 실패, 무시하고 진행: {e}")


def log_ai_decision(case_id, node, reason=None):
    """
    ai_decision_log에 AI 판단 1건 기록: 어느 노드/함수에서, 왜 그렇게
    판단했는지 딱 두 가지만. case_id는 None이어도 됨(FK가 nullable이라
    케이스 연결 없이도 기록되고, 단독 테스트 실행 때도 AI판단 이력
    자체는 남길 수 있음).

    node 예시(자유확장 가능, 지금 실제로 쓰는 값):
      - "site_selection": naver_contact_enrichment._pick_best_site_candidate
      - "contact_extraction": naver_contact_enrichment._extract_contacts_batch
      - "company_name_extraction": web_search_based_tool._extract_company_names_llm
    """
    get_connection = _get_conn_or_none()
    if get_connection is None:
        return

    try:
        with get_connection(autocommit=True) as conn:
            conn.execute(
                """
                INSERT INTO procurement.ai_decision_log (case_id, node, reason)
                VALUES (%(case_id)s, %(node)s, %(reason)s)
                """,
                {"case_id": case_id, "node": node, "reason": reason},
            )
    except Exception as e:
        print(f"    [case_logging] AI판단 기록 실패, 무시하고 진행: {e}")
