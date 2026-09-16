# 회사 구매 정책 관리자 설정

## 사용 방법

관리자 로그인 → 사이드바 **회사 구매 정책** → 기준/지침 수정 → 변경 사유 입력 →
**변경 내용 검토** → **정책 게시**.

접근 권한은 **현재 로그인 계정의 ERPNext User 문서**를 서버가 직접 조회해 확인합니다.
활성 `Administrator` 계정, `System Manager` 또는 `Purchase Master Manager` 역할이 있는 활성 계정만
정책을 조회/게시할 수 있습니다. 일반 `Purchase Manager`/`Purchase User`는 허용하지 않습니다.
역할을 자동 생성하거나 사용자에게 부여하지 않습니다. 실제 ERP에 역할이 없으면 해당 이름으로 접근할 계정도 없습니다.
2026-09-16 서버 내부 읽기 전용 확인에서 `Purchase Master Manager` 역할의 존재(HTTP 200)와
Administrator의 역할 조회 가능 여부를 확인했습니다. 이 확인은 역할을 부여/변경하지 않았습니다.
화면에서 내 ERP 역할 목록을 확인할 수 있고, 창에 다시 포커스하면 메뉴 권한을 갱신합니다.
게시할 때도 ERP 역할을 재조회하므로 로그인 이후 권한이 회수돼도 저장할 수 없습니다.
ERP 권한 조회가 실패하면 503으로 차단하며, 브라우저 localStorage/이전 토큰의 역할 정보로 우회하지 않습니다.
이 설정의 접근 제어는 ERP의 역할 기반입니다. ERPNext의 모든 문서별 권한/User Permission 규칙을 복제하는 기능은 아닙니다.

- JSONB로 저장된 게시본만 실행에 사용합니다. 입력 중에는 실제 업무 기준이 바뀌지 않습니다.
- 게시 후 서비스 재시작 없이 새 MR 작업에 적용합니다. 이미 시작한 MR은 기존 버전을 유지합니다.
- 탭을 이동해도 같은 로그인 세션 내의 미게시 초안은 유지합니다. 새로고침하면 사라지며, 수정 중 새로고침 시 브라우저 경고를 요청합니다.
- 기존 버전의 **초안으로 불러오기** → 검토 → 게시로 복원합니다. 과거 버전은 덮어쓰지 않습니다.
- 두 관리자가 동시에 수정하면 먼저 게시한 사람만 성공합니다. 나머지는 409 안내 후 최신 내용을 다시 불러옵니다.
- **게시본 JSON**은 현재 적용 중인 버전을 내려받습니다. 미게시 초안을 내려받는 버튼은 아닙니다.
- 현재는 단일 회사 전체 설정입니다. 회사별 tenant 분리 기능은 아닙니다.

## 이번에 실제 연결한 항목

| 설정 | 기존 기본값 | 적용 위치 |
|---|---:|---|
| 긴급구매 기준 | 납기까지 7일 이하 | `nodes/mr/decide_bidding.py` |
| 경쟁입찰 금액 | 20,000,000원 이상 | 최근 확정 단가 × 수량 |
| 구매주기 분석 최소 이력 | 3건 | 구매 간격 통계 산정 |
| 불규칙 구매주기 CV | 0.5 이상 | 표준편차 / 평균 |
| 구매주기 초과 허용 배수 | 1.5배 | 평균 주기 대비 경과일 |
| 장기 미거래 기간 | 12개월 | 구매 패턴 분석 이력 부족 시 |
| 최소 경쟁 협력사 | 3곳 | `nodes/supplier/resolve_supplier_pool.py` |
| 협력사 재탐색 주기 | 3년 | 최근 Supplier 등록일 기준, 365일/년 |
| 견적 비교 우선순위 | 금액 → 납기 | `quotation_filter/quotation_ranker.py` |
| 품목 규격 보조 지침 | 추가 지침 없음 | 신규 그룹 필수규격 생성 및 설명 완전성 검토 |
| 대체품 보조 지침 | 추가 지침 없음 | 용도 적합성·추천 사유 판단 |

견적 순위는 **금액 우선** 또는 **납기 우선**을 선택할 수 있습니다. 규격/수량/산식 검토를
통과한 후보만 비교하며, 같은 가격·납기 안에서 기존 공급사 평가를 보조 기준으로 사용하는
원칙은 유지합니다. AI가 임의로 순위를 결정하는 방식이 아닙니다.

### 팀원 제안 변수와의 대응

| 기존 코드 변수 | 정책 JSON 필드 |
|---|---|
| `URGENT_LEAD_TIME_DAYS` | `rules.urgent_lead_days` |
| `AMOUNT_THRESHOLD` | `rules.bidding_amount` |
| `MIN_ORDERS_FOR_PATTERN` | `rules.pattern_min_orders` |
| `IRREGULAR_CV_THRESHOLD` | `rules.irregular_cv` |
| `CYCLE_OVERDUE_MULTIPLIER` | `rules.cycle_overdue_multiplier` |
| `INACTIVE_MONTHS` | `rules.inactive_months` |
| `MIN_COMPETING_SUPPLIERS` | `rules.min_competing_suppliers` |

제안한 7개 항목은 모두 실제 판단 분기에 연결했습니다. 관리자 화면에는 한국어 설명과 기존 변수명을 함께 표시합니다.
`Purchase Master Manager`는 구매 정책을 편집할 수 있는 역할명으로 인식합니다. ERPNext에서 실제 부여된 역할만 사용합니다.

긴급 분기 → 금액 → 구매패턴이라는 기존 실행 순서는 변경하지 않았습니다.
따라서 긴급이면 고액 기준보다 긴급 분기가 먼저 적용됩니다. 이는 예외 없는 고액 승인 한도 설정이 아닙니다.
기존 품목군 `required_specs`는 캐시/DB를 재사용하므로 지침을 변경해도 이미 저장된 규격을 자동 생성·교체하지 않습니다.

메일 TEST_MODE, 계정 권한, 사람 승인 게이트, 출력 JSON 스키마, 모델/외부 검색 구성,
업체 평가 점수 가중치, RFQ 개별 견적 마감일은 이번 관리자 설정에 포함하지 않습니다.
기능별 고정 안전 규칙과 API 호출 방식은 유지했으며, 전체 프롬프트를 관리자에게 자유 편집시키지 않습니다.

## 메일 화이트리스트 편집

같은 관리자 화면에서 **메일 수신 화이트리스트**를 편집할 수 있습니다.
한 줄에 정확한 주소 한 개를 입력하고 변경 사유 → 추가/삭제 내역 검토 → 저장으로 반영합니다.

- `custom_only` 모드와 활성 파일에서만 저장 허용. TEST_MODE나 파일 경로는 UI에서 바꿀 수 없습니다.
- 최대 500개, 소문자/중복 정리, 와일드카드·표시명·헤더 주입 거부. 빈 목록은 전체 발송 차단.
- 기존 동적 JSON 파일을 원자적으로 교체하므로 다음 발송 판단부터 반영. 메일을 직접 보내는 기능은 아님.
- 정책 버전과 달리 **전역 안전 제한**이므로 진행 중인 MR에도 다음 발송부터 적용.
- 파일 내용 SHA-256으로 동시 수정 충돌을 감지하고 PostgreSQL advisory lock으로 API 간 저장 직렬화.
- 교체 전에 같은 디렉터리의 `allowlist-history/`에 이전 원본 JSON을 저장. 백업 실패 시 저장 중단.
- 이력에는 원래 목록을 유지하고, 현재 파일에는 `updated_by`, `updated_at`, `change_reason` 기록.
- 외부에서 파일을 수동 수정할 때는 관리자 화면 편집과 동시에 하지 않습니다.

API: `GET/POST /api/company-policy/email-allowlist`. POST는 `expected_revision`, `recipients`, `reason`을 받으며,
다른 정책 API와 동일하게 현재 ERPNext 역할을 재확인합니다.

운영 설정: `EMAIL_RECIPIENT_ALLOWLIST_PATH=/var/lib/biddingflow/email-policy/allowlist.json`.
이 디렉터리만 서비스 사용자 `ubuntu` 소유 0700, 파일 0600으로 지정합니다.
`/etc/biddingflow` 전체 쓰기 권한을 부여하거나 서비스를 root로 실행하지 않습니다.
기존 `/etc/biddingflow/email-recipient-allowlist.json`은 전환 당시 원본으로 보관합니다.
전환 뒤에는 새 경로가 실제 발송 기준입니다. 이전 경로를 수정해도 적용되지 않습니다.
파일/경로 설정 자체를 처음 바꿀 때는 서비스 재시작이 필요하지만, 이후 UI에서 주소를 수정할 때는 필요 없습니다.

구현: `policies/allowlist.py`, `views/EmailAllowlistEditor.tsx`.

## 백엔드 구조

```
관리자 UI → 인증된 정책 API → 검증된 CompanyPolicy → PostgreSQL 게시 버전
                                                   ↓
업무 시작 → case_policy에 버전 고정 → 노드 실행 컨텍스트 → 숫자 판단 / 템플릿 변수
```

- `backend_logic2/policies/schema.py`: 허용 키·타입·범위·기존 기본값. 알 수 없는 필드, 문자열 숫자, NaN 등 거부.
- `policies/repository.py`: 게시 시 head 행 잠금 + expected_version 검사. 이력 INSERT와 head 갱신은 단일 트랜잭션.
- `policies/runtime.py`: `ContextVar`로 실행별 정책 격리. 병렬 품목 처리에는 값을 명시적으로 전달.
- `policies/access.py`: 기존 인증 세션의 `erp_user_id`/email로 ERPNext User 역할을 조회. 정책 API마다 재검증하며 타임아웃/장애 시 차단.
- `api/policy_routes.py`: 인증된 사용자 역할에 따른 접근 제한. 프론트 표시 여부만으로 권한을 허용하지 않음.
- `workflow/process_graph.py`: case_id로 정책을 조회하고 각 노드에 동일 버전을 주입. 체크포인트에도 `policy_version` 기록.
- `services/item_service.py`: 품목 웹훅 처리 시작 시 현재 버전을 한 번 읽어 전체 검증에 사용. 알림 검증 payload에 버전 포함.

AI 지침은 `{company_guidance}`의 **값**으로 전달합니다. 관리자 문자열을 새 템플릿으로 다시 해석하지 않으므로
`{...}`가 포함되어도 포맷 오류가 나지 않습니다. AI 보조 지침 자체의 품질은 관리자가 검토해야 하며,
텍스트 지침은 검증 코드를 대체하지 않습니다.

업무 실행 중 DB 정책 조회가 실패하면 임의로 기본값을 사용해 진행하지 않고 오류를 전파합니다.
DB 컨텍스트를 바인딩하지 않는 순수 함수/개발용 CLI 직접 호출만 기존 기본값을 사용합니다.
실제 운영은 워크플로우/품목 서비스 진입점을 사용해야 합니다.

## API

- `GET /api/company-policy/capabilities`: 로그인 사용자의 `can_manage`, `roles`, `source`, `enabled`.
- `GET /api/company-policy`: 관리자 전용 현재 게시본 + 최근 100개 이력. DB에는 전체 이력이 남음.
- `POST /api/company-policy/publish`: 관리자 전용. body = `expected_version`, `policy`, `reason`.
- 응답: 미로그인 401, 관리자 아님 403, 동시 수정 충돌 409, 잘못된 설정 422, 저장소 장애 503.

## DB와 배포

`migrations/014_create_company_policy.sql`이 다음 테이블을 추가합니다.

- `procurement.company_policy_version`: 버전, 정책 JSONB, 변경 사유, 작성자, 시각.
- `procurement.company_policy_head`: 현재 게시 버전 1개.
- `procurement.case_policy`: MR case_id와 고정 정책 버전 연결.

마이그레이션 당시 존재하는 MR은 모두 기존 기본값 v1에 고정합니다.
그 후 새로 만들어진 case는 첫 워크플로우 실행 시 최신 버전을 고정합니다.
ERP 문서, 발송 설정, 비밀키에는 변경이 없습니다.

배포 순서:

1. 백엔드 코드 반영.
2. `python -m procurement_db.migrate` 실행.
3. 백엔드 재시작(새 기능 최초 설치 때만).
4. 프론트 `npm ci` → `npm run build` → 배포.
5. 관리자 로그인 후 회사 구매 정책 메뉴 확인. 일반 계정에는 메뉴가 없어야 함.

백엔드의 ERP API 계정에 User 및 역할 하위 테이블을 읽을 권한이 필요합니다.
조회 권한이 없으면 설정을 허용하지 않고 오류를 반환합니다. 이 코드가 ERP 계정 권한을 자동 확대하지는 않습니다.

기존 서버 배포 스크립트는 재시작 전에 마이그레이션을 실행합니다. 이 기능 설치 이후 정책 수정에는 재시작이 필요하지 않습니다.
코드를 이전 버전으로 되돌려도 이력 테이블을 삭제할 필요는 없습니다.

## 프론트 구현

- `src/procurement/views/CompanyPolicyView.tsx`, `.css`: 모노크롬 관리자 폼, 변경 검토, 게시, 복원, JSON 내려받기.
- `src/procurement/api/companyPolicy.ts`: 인증된 API 계약.
- `ProcurementWorkspace.tsx`, `Sidebar.tsx`, `Header.tsx`, `types/index.ts`: 권한 기반 메뉴 및 탭 연결.

## 검증

- `pytest -q`: 기존 회귀 + 정책 인증/검증/분기/병렬 컨텍스트/견적 우선순위 테스트.
- `POLICY_REPOSITORY_DB_TEST=1 pytest tests/test_company_policy_db.py -q`: UUID 임시 스키마에서 게시·복원·동시성·버전 고정 검증 후 임시 스키마 제거.
- `npm run build`, `npm run lint`: 타입/빌드/정적 검사.
- 로컬 브라우저 모의 API로 관리자 메뉴, 입력 초안 유지, 게시, 복원, 409 안내, 일반 사용자 메뉴 숨김, 좁은 화면 확인.
- 테스트 과정에서 실제 ERP 문서와 메일은 생성하지 않습니다.
# 공급사 탐색 소스 (2026-09-16)

관리자 페이지의 체크박스로 Tavily / 나라장터 / DB를 복수 선택합니다.
최소 1개가 필요하며 기본은 기존 동작인 Tavily 단독입니다. DB는
`procurement.narajangteo_company_info`의 저장된 업체 데이터이며 ERP의
기존 협력사 조회를 끄는 옵션이 아닙니다. DB만 선택해도 기존 정규화·검증·
연락처 보완에서 외부 LLM/정부 API/네이버 등이 사용될 수 있습니다.
새 정책 게시 후 시작한 MR에 적용되며 진행 중 MR은 고정된 기존 정책을 유지합니다.
이전 버전은 원본 JSON을 수정하지 않고 읽을 때 기본 소스를 보완합니다.
캐시는 선택한 소스 조합별로 분리하며 해제한 소스를 후보 수집 목적으로 호출하지 않습니다.
