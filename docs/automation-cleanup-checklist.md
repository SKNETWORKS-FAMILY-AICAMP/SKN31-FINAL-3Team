# 자동 진행 - 테스트용으로 만든 것 정리 목록

자동화를 검증하려고 만든 것들이다. 검증이 끝나면 지우거나 되돌린다.
운영에 필요한 코드와 섞이지 않게 여기 적어둔다.

## 1. 지울 것 (테스트 전용)

### 진단·검증 스크립트
- `scripts/diagnose_auto_progress.py` — 왜 자동 진행이 안 도는지 짚어보는 용도
- `scripts/backtest_auto_selection.py` — 과거 건으로 임계값을 정하는 용도.
  임계값이 확정되면 역할이 끝난다

### 스캔 하트비트 (사용자가 "필요 없다"고 판단)
스캔이 돌고 있는지 확인하려고 만들었는데, '자동 진행 지금 확인' 버튼이
생기면서 필요가 없어졌다.
- `migrations/022_create_automation_heartbeat.sql`
  → 테이블을 지우려면 되돌리는 마이그레이션(023)을 새로 만들어야 한다.
    마이그레이션은 앞으로만 간다
- `repositories/cases.py`의 `record_automation_scan`, `get_automation_heartbeat`
- `services/auto_progress_runner.py`에서 위 함수를 부르는 부분
- `scripts/diagnose_auto_progress.py`의 "2. 스캔이 돌고 있나" 항목

### 되돌릴 설정
- `services/auto_progress_scheduler.py`의 `_MINIMUM_INTERVAL_SECONDS`
  → 테스트하려고 5초로 낮췄다. 운영에서는 60초로 되돌린다
  (기본값 600초는 그대로 두면 된다)

## 2. 남길지 정할 것

- **`⋯ 자동 진행 지금 확인`** (프론트) + **`POST /cases/{id}/automation/scan`**
  테스트용으로 만들었지만 운영에서도 쓸모가 있다. '회신 새로 확인'과
  같은 성격이다. 남기는 쪽을 권한다
- **`deploy/systemd/biddingflow-auto-progress.{service,timer}.example`**
  스캔이 API 프로세스 안에서 도니 필요 없다. API와 분리해서 돌릴 생각이
  없으면 지워도 된다

## 3. 남길 것 (운영 코드)

- 조건 평가기 `services/auto_progress.py`
- 스캔 로직 `services/auto_progress_runner.py`, 인프로세스 루프
  `services/auto_progress_scheduler.py`, 수동 실행용 `jobs/auto_progress_job.py`
- 정책의 자동 진행 설정 (`policies/schema.py`) 및 정책 화면
- 보류/타임라인 API, 마이그레이션 021
- 예외 결정 화면, 상태 배지, PO 승인 화면의 선정 근거

## 4. 테스트 데이터

- 테스트로 만든 MR과 견적 (예: `MAT-MR-2026-00134` · 네임펜 유성마커)
- 정책 버전 이력은 감사 기록이라 지우지 않는다

## 검증 기록

- 2026-09-27 · 기록 모드에서 조건 9개 전부 통과 확인
  (`MAT-MR-2026-00134` · 경쟁 2건 · 1-2위 점수차 22.18 · 규격 평가 완료 ·
  1순위 동관컴퍼니 99.98점 · 150만원)
  → 켜짐이었다면 자동 선정되고 수주 접수 요청까지 나갔을 상황
