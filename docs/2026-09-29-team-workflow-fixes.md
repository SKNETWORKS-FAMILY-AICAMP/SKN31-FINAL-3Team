# 구매 자동화 팀원 요청 9건 — 구현 및 검증 기록

## 요약

긴급 직접구매/종료의 판단 로그를 추가했고, 재비딩 때 같은 견적의 같은 규격·특약을 다시 평가하지 않도록 캐시 조회 범위를 바로잡았다. 유효한 최종 선정은 PR 요청까지 자동 진행하되, 공급사 수락 이후 **PO 승인 역할 검사와 최종 승인 단계는 유지**한다.

특약은 규격 점수와 별개로 평가한다. 중요하거나 불명확한 특약은 자동 선정 조건 불충족으로 처리하고 담당자에게 넘긴다. 런타임 예외로 그래프를 실패시키지는 않는다.

이 기록은 로컬 코드와 모의 검증 결과다. 실제 운영 배포, 운영 DB 마이그레이션, 실제 메일 발송, 유료 RunPod 추론 및 로그인 브라우저 실사용 검증은 수행하지 않았다.

## 요청별 반영

| 요청 | 변경 |
|---|---|
| 1. 긴급 + 거래 이력 로그 | `direct_purchase_decision`에 선택 공급사·단가·근거 PO·이유 기록. PR 생성 처리 뒤 실제 반환 상태도 별도 기록 |
| 2. 긴급 + 거래처 없음 종료 로그 | MR 종료 처리 후 `urgent_purchase_cancelled`에 종료 이유 기록 |
| 3. 재비딩 캐시 / 추가 발주 시작 버튼 | RFQ 문서 이름을 의미 기반 해시에서 제외. SQ ID·규격·특약·모델·프롬프트가 같은 캐시를 차수 간 재사용. 새 최종 선정 때 이전 `order_started` 초기화, PR 자동 진행 |
| 4. 대시보드 RFQ 대상 선정 링크 | 팀원 최신 main에 `RFQ_TARGET_SELECTION → vendor-select` 수정이 이미 있어 유지. 모든 단계 라우팅 회귀 테스트 확인 |
| 5. 특약 검토 | `terms_review`와 `terms_reason` 구조화 출력 추가. `review_required`/`unknown`이면 자동 선정 중지 및 특약 경고 표시 |
| 6. 이메일 견적 원본 | 비교 모달에 원본 확인/다운로드 추가. SQ 직접 첨부 + 해당 RFQ 수신 Communication 원본 첨부 조회 |
| 7. 자동 선정 표시 | 체크포인트 `selection_mode=auto`를 PO 화면 `AI 자동 선정` 배지로 표시. 사람이 다시 선정하면 manual로 변경 |
| 8. 건별 자동 진행 끄기 | 견적 회신 대기 중 ⋯ 메뉴 및 행 상태에 표시. DB에 `automation_paused` 저장, 재접속에도 유지 |
| 9. 공급사 연락처 | RFQ 회신 현황·견적 비교의 공급사 이름에 이메일/전화 hover 안내. 미등록 값은 미등록 표시 |

## 흐름과 안전 경계

```text
견적 원문(규격 + SQ terms/notes)
  → 기존 Qwen/선택된 평가 어댑터
  → 규격 점수 + 독립 특약 판정
  → 기존 경쟁 수·점수 차·신규 업체·유효기간 등 조건 + 특약 조건
      ├─ 조건 충족, 정책 on, 건별 중지 아님 → 자동 최종 선정
      └─ 특약 확인 필요/불명확 → 담당자 최종 선정 대기
  → 최종 선정 → PR 생성/발송 처리 → 공급사 수락
  → 기존 역할 기반 PO 최종 승인 → PO 생성/발송
```

- 특약이 없으면 clear. 인사 문구 등은 모델이 clear와 근거를 반환해야 한다.
- 특약이 있는데 모델이 새 필드를 누락하거나 clear 근거가 없으면 unknown으로 처리한다.
- 공급사 원문은 명령이 아닌 분석 데이터라고 프롬프트에 명시했다.
- 특약 확인은 1순위 자동 선정 대상에 적용한다. 하위 견적의 특약만으로 정상 1순위를 무조건 막지는 않는다.
- 기존 선택 유효기간 검사, 메일 화이트리스트, TEST_MODE, PO 승인 권한은 변경하지 않았다.
- 자동 진행 중지와 스케줄러 실행은 동일 구매 건 잠금을 사용한다. 이미 실행이 시작됐으면 409로 알리고 중지 성공처럼 표시하지 않는다. 이미 발송된 메일을 취소하는 기능은 아니다.
- 중지해도 견적 수신/분석은 유지한다. 수동 최종 선정은 가능하다. 이번 범위에는 건별 재활성화 버튼을 추가하지 않았다.
- 첨부파일 API는 구매 건 접근 권한, SQ↔RFQ 관계, 수신 메일↔RFQ 관계, 파일 연결을 검증한다. 브라우저에 ERP 자격 증명이나 원본 private URL을 노출하지 않는다.
- 과거 이메일과 SQ 연결 정보가 남아 있지 않으면 추측해서 다른 메일을 노출하지 않고 원본 없음으로 표시한다.

## 주요 파일

### 백엔드

- `backend_logic2/workflow/process_commands.py`: 긴급 로그, 재선정 상태 초기화, PR 자동 전환 및 PR 결과 로그
- `backend_logic2/nodes/quotation/quotation_filter/quotation_spec_evaluator.py`: 특약 출력 계약/프롬프트, 의미 기반 해시
- 같은 폴더 `quotation_models.py`, `quotation_ranker.py`: 특약 결과·경고를 점수와 분리해 전달
- `backend_logic2/repositories/quotation_specification_cache.py`: 차수 간 동일 입력 캐시 검색
- `backend_logic2/services/quotation_service.py`: 재비딩 캐시 검색을 차수별 반복에서 1회로 축소
- `backend_logic2/services/auto_progress.py`: 특약 사람 확인 게이트
- `backend_logic2/services/quotation_attachments.py`: 견적/메일 원본 연결 확인
- `backend_logic2/api/procurement_routes.py`: 원본 목록·다운로드 및 건별 자동 진행 중지 API
- `backend_logic2/repositories/cases.py`, `policies/repository.py`, `services/deadline_scheduler.py`: 건별 중지 영속화 및 실행 직전 재검사
- `migrations/027_case_automation_pause.sql`: 중지 컬럼 및 캐시 조회 인덱스 (비파괴 추가)

### 프론트

- `src/procurement/api/cases.ts`, `types/index.ts`: API와 서버 상태 매핑
- `components/QuotationOriginals.tsx`: 인증된 원본 다운로드
- `components/QuotationScoreBreakdown.tsx`: 특약 경고는 축약 화면에서도 숨기지 않음
- `views/VendorSelectionView.tsx`: ⋯ 자동 진행 중지, 연락처, 첨부파일
- `views/POManagementView.tsx`: AI 자동 선정 배지
- `views/AiDecisionLogView.tsx`: 새 로그 유형 한글 표시
- `ProcurementWorkspace.tsx`: 최종 선정 PR 자동 진행 및 건별 중지 연결

## 검증

- 백엔드 `pytest tests`: **693 passed, 27 skipped**, 4 subtests passed.
- 프론트 Node 테스트: **50 passed**.
- TypeScript 검사 통과. Vite production build 통과 (기존 단일 번들 500 kB 크기 경고 있음).
- 검증 내용: 긴급 분기 로그, PR 반환 상태 로그, 재비딩 입력 동일/변경 해시, 특약 clear/review_required/unknown, 수동 재선정, 만료 견적 거부, 실행 직전 중지 확인, 다른 구매 건 파일 차단, 원본 바이트 응답, 권한 없는 중지 차단, 새 탭 상태 매핑, PO 배지, 대시보드 단계별 이동.
- 외부 연동은 mock과 테스트용 주소를 사용했다. 스킵된 실제 PostgreSQL 등 opt-in 테스트와 실제 Qwen 특약 판단 품질은 별도 운영 전 확인이 필요하다.

## 배포 및 롤백 주의

1. 백엔드 마이그레이션 후 백엔드 재시작, 이후 프론트 배포 순서로 적용한다. 레포 배포 스크립트에는 `python -m procurement_db.migrate`가 재시작 전에 이미 포함돼 있다. 운영 설치본이 같은 스크립트인지 확인해야 한다.
2. 마이그레이션 전 새 백엔드를 실행하면 정책 조회가 새 컬럼을 요구하므로 오류가 날 수 있다.
3. RunPod 호출용 프롬프트/JSON 계약은 백엔드에서 구성하므로 이번 변경 자체에 모델 가중치나 워커 이미지 변경은 없다. 실제 엔드포인트가 새 특약 필드를 반환하는지는 추론 검증이 필요하다. 미반환 시 자동 선정은 안전하게 중지된다.
4. 새 특약 프롬프트를 적용하므로 기존 캐시는 한 번 무효화된다. 이후 동일 모델·입력·프롬프트의 동일 SQ는 차수가 바뀌어도 재사용한다. 규격이나 특약 변경 시 재평가는 의도된 동작이다.
5. 이전 코드로 롤백해도 추가 컬럼/인덱스는 남겨둬도 된다. 단, 구버전은 건별 중지 플래그를 읽지 못하므로 중지 약속을 지키려면 스케줄러/회사 자동 진행을 먼저 중지해야 한다. 구버전은 새 특약 게이트도 지원하지 않는다.
6. 이미 대기 중인 과거 체크포인트를 일괄 재개하거나 이미 발송한 PR을 재발송하지 않았다. 이번 수정은 이후 최종 선정 전이에 적용된다.
