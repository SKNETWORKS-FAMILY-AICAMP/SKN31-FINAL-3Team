"""자동 진행이 왜 안 도는지 짚어본다. 아무것도 바꾸지 않는다.

실행:
    python -m scripts.diagnose_auto_progress                  # 최근 건 5개
    python -m scripts.diagnose_auto_progress MAT-MR-2026-00134
"""

from __future__ import annotations

import sys
from typing import Any

from procurement_db import get_connection


def _tables(connection) -> set[str]:
    return {
        dict(row)["table_name"]
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='procurement'"
        ).fetchall()
    }


def _scalar(connection, sql: str, params: dict[str, Any] | None = None):
    row = connection.execute(sql, params or {}).fetchone()
    return None if row is None else list(row.values())[0] if hasattr(row, "values") else row[0]


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else None

    with get_connection() as connection:
        print("=== 1. 마이그레이션 021 적용 여부 ===")
        columns = [
            dict(row)["column_name"]
            for row in connection.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='procurement' AND table_name='procurement_case'
                  AND column_name IN ('automation_hold','auto_progress_signature',
                                      'auto_deadline_extended_at','live_quotation_ranking')
                """
            ).fetchall()
        ]
        for name in ("live_quotation_ranking", "automation_hold", "auto_progress_signature", "auto_deadline_extended_at"):
            print(f"  {name}: {'있음' if name in columns else '❌ 없음 - 마이그레이션 미적용'}")

        print("\n=== 2. 스캔이 돌고 있나 ===")
        beat = connection.execute(
            "SELECT * FROM procurement.automation_heartbeat WHERE singleton = true"
        ).fetchone() if "automation_heartbeat" in _tables(connection) else None
        if beat is None:
            print("  ❌ 아직 한 번도 안 돌았습니다.")
            print("     - 서버에 최신 코드가 배포됐는지 확인하세요(스캔 루프가 그 안에 있습니다).")
            print("     - 배포 직후라면 첫 실행까지 최대 10분 걸립니다.")
        else:
            beat = dict(beat)
            from datetime import datetime, timezone

            last = beat["last_run_at"]
            interval = int(beat.get("interval_seconds") or 600)
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            remaining = max(0, interval - elapsed)
            print(f"  마지막 실행: {last:%Y-%m-%d %H:%M:%S} UTC ({elapsed / 60:.1f}분 전, {beat.get('ran_by')})")
            print(f"  주기: {interval // 60}분 · 다음 실행까지 약 {remaining / 60:.1f}분")
            if elapsed > interval * 2.5:
                print("  ⚠️ 주기보다 한참 지났습니다. API가 재시작됐거나 스캔이 꺼져 있을 수 있습니다.")
            print(f"  마지막 결과: {beat.get('result')}")

        print("\n=== 3. 회사 정책 자동 진행 설정 ===")
        policy = connection.execute(
            """
            SELECT v.policy FROM procurement.company_policy_head h
            JOIN procurement.company_policy_version v USING (version)
            WHERE h.singleton = true
            """
        ).fetchone()
        rules = (dict(policy)["policy"] if policy else {}).get("rules", {}) if policy else {}
        for key in ("automation_mode", "auto_rfq_dispatch", "auto_final_selection",
                    "auto_selection_min_quotations", "auto_selection_score_gap"):
            print(f"  {key}: {rules.get(key, '❌ 키 없음 - 백엔드 미배포 또는 옛 정책')}")

        where = "pc.mr_name = %(mr)s" if target else "pc.stage = 'QUOTATION_COLLECTION'"
        cases = connection.execute(
            f"""
            SELECT pc.case_id, pc.mr_name, pc.stage, pc.status, pc.thread_id,
                   pc.quotation_deadline_at, pc.automation_hold, pc.auto_progress_signature,
                   pc.auto_deadline_extended_at,
                   pc.quotation_snapshot, pc.workflow_snapshot #> '{{values,auto_progress}}' AS verdict,
                   cp.version AS pinned_policy_version
            FROM procurement.procurement_case pc
            LEFT JOIN procurement.case_policy cp ON cp.case_id = pc.case_id
            WHERE {where}
            ORDER BY pc.updated_at DESC
            LIMIT 5
            """,
            {"mr": target} if target else {},
        ).fetchall()

        if not cases:
            print("\n해당하는 케이스가 없습니다.")
            return 0

        for row in cases:
            case = dict(row)
            case_id = str(case["case_id"])
            snapshot = case.get("quotation_snapshot") or {}
            print(f"\n=== 4. {case['mr_name']} ===")
            print(f"  단계: {case['stage']} / 상태: {case['status']}")
            print(f"  마감: {case['quotation_deadline_at']}")
            print(f"  회신: {snapshot.get('responded_count')}/{snapshot.get('recipient_count')}"
                  f"  → 전원 회신 트리거 조건: "
                  f"{'충족' if (snapshot.get('recipient_count') or 0) > 0 and (snapshot.get('responded_count') or 0) >= (snapshot.get('recipient_count') or 0) else '미충족'}")
            print(f"  보류: {case['automation_hold']}")
            print(f"  고정 정책 버전: {case['pinned_policy_version']}")

            tasks = connection.execute(
                """
                SELECT task_type, status, version, audience, answered_by, answered_at
                FROM procurement.human_task
                WHERE case_id = %(case_id)s
                ORDER BY created_at DESC LIMIT 4
                """,
                {"case_id": case_id},
            ).fetchall()
            print("  대기 작업:")
            if not tasks:
                print("    ❌ 없음 - 깨울 대상이 없습니다")
            for task in tasks:
                t = dict(task)
                print(f"    {t['task_type']} / {t['status']} / v{t['version']}"
                      f"{' / 답변: ' + str(t['answered_by']) if t['answered_by'] else ''}")

            print(f"  마지막 판정: {case['verdict'] or '❌ 없음 - 판정이 한 번도 안 돌았습니다'}")

            decisions = connection.execute(
                """
                SELECT node, reason, created_at FROM procurement.ai_decision_log
                WHERE case_id = %(case_id)s AND node LIKE 'auto_%%'
                ORDER BY created_at DESC LIMIT 3
                """,
                {"case_id": case_id},
            ).fetchall()
            print("  자동 진행 로그:")
            if not decisions:
                print("    ❌ 없음")
            for decision in decisions:
                d = dict(decision)
                print(f"    [{d['created_at']}] {d['node']}: {str(d['reason'])[:110]}")

            print(f"  thread_id: {case['thread_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
