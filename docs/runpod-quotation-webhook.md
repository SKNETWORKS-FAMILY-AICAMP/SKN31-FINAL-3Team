# RunPod 견적 추출 완료 웹훅 (2026-09-16)

## 설정과 적용 범위

```dotenv
QUOTATION_EXTRACTOR_PROVIDER=runpod
RUNPOD_QUOTATION_DELIVERY_MODE=webhook
RUNPOD_QUOTATION_WEBHOOK_URL=https://biddingflow.13.209.103.102.nip.io/api/webhooks/runpod/quotation
```

`RUNPOD_API_KEY`, `RUNPOD_QUOTATION_ENDPOINT_ID`는 기존 값을 사용한다.
서버 설정 파일은 `/etc/biddingflow/backend.env`, 서비스는 `biddingflow-api.service`다.
환경변수 변경 후 `sudo systemctl restart biddingflow-api.service`로 적용한다.
공개 콜백 URL이 없는 로컬 개발에서는 DELIVERY_MODE=polling을 사용한다.

대상은 ERPNext RFQ 회신 Communication/File의 견적 첨부 추출이다.
동기 CLI 및 명시적으로 호출하는 파서의 `__call__`은 기존 폴링 방식을 유지한다.
기존 일반 텍스트 모델의 제공자나 메일 발송 설정은 이 기능에 관여하지 않는다.

## 요청과 완료 처리

1. DOCX/Excel/CSV/TXT/EML은 Python으로 본문·표를 추출하고, 이미지/PDF는
   원본 문서 입력을 유지해 프롬프트와 함께 text/vision/hybrid 요청을 구성한다.
2. `procurement.quotation_extraction_job`에 SUBMITTING 작업과 신뢰할 수 있는
   RFQ/공급사/출처 문맥을 기록한다. API 키와 이미지 Base64는 저장하지 않는다.
3. `POST /v2/{endpoint}/run`에 `input`과 같은 위계의 `webhook` URL을 전달한다.
4. 반환된 RunPod job ID를 기록하고 SUBMITTED로 전환한다. 이메일 처리 함수는
   모델 완료를 기다리지 않고 `queued`, `extraction_jobs`를 반환한다.
5. Worker는 기존대로 `handler()`에서 딕셔너리를 반환한다. RunPod가 콜백을 보낸다.
6. 콜백 수신 API는 알려진 job ID의 깨우기 신호를 DB에 기록한 후 HTTP 200을 반환한다.
7. 백엔드는 API 키로 원래 endpoint의 `/status/{job_id}`를 조회한다.
   콜백에 포함된 status/output은 사용하지 않는다.
8. job ID, request ID, 프롬프트 해시/버전을 검증하고 결과를 DB에 READY로 보존한다.
9. 기존 견적 정규화·RFQ/공급사 매핑·중복 검사·Supplier Quotation 등록을 수행한다.
10. REGISTERED를 기록한 후 회신율·알림을 갱신하고 COMPLETED로 종료한다.

## 중복·장애 복구

- 동일 문서/프롬프트 요청은 request_id UNIQUE로 중복 GPU 제출을 방지한다.
- RunPod job ID에도 UNIQUE 제약이 있다.
- 작업별 및 RFQ/공급사별 PostgreSQL advisory lock으로 여러 프로세스의 동시 등록을 막는다.
- ERP 저장 직후 프로세스가 죽어도 재시도 시 기존 등록기의 견적번호·금액 지문 검사로
  생성된 SQ를 다시 찾아 사용한다. DB와 ERP 사이에 분산 트랜잭션이 있는 것은 아니다.
- 화면 갱신만 실패하면 REGISTERED부터 재개해 SQ를 재등록하지 않는다.
- 매 60초 PostgreSQL의 대기 작업만 확인하고, 정상 대기 작업의 RunPod 상태는
  120초 간격으로 복구 조회한다. 대기 작업이 없으면 RunPod를 호출하지 않는다.
- 콜백이 오면 즉시 조회 가능하게 하되, 반복 콜백으로 10초 이내에 상태 조회를
  반복할 수 없게 한다. 타이밍상 처리하지 못한 신호는 다음 복구 주기가 처리한다.
- 콜백이 제출 응답 저장보다 빨리 도착하면 503을 반환해 재전송을 유도한다.
- 실패/취소/시간 초과는 FAILED, 복구 오류는 최대 10회 기록 후 FAILED로 남긴다.
- POST 응답 유실 또는 제출 중 프로세스 종료는 SUBMISSION_UNKNOWN으로 남긴다.
  원격에서 이미 실행됐을 수 있으므로 자동 재제출하지 않는다.
  운영자가 RunPod 요청 이력을 확인해야 한다.
- API가 결과 보존 시간 이상 꺼져 있으면 복구를 보장하지 않는다. RunPod 공식 문서상
  `/run` 완료 결과는 30분 보존되며, 본 구조는 회수 즉시 DB에 저장한다.

## 배포

백엔드 main push → 기존 CI/CD → migration 013 → 서비스 재시작 순서다.
전달 모드와 공개 URL은 서버 환경변수로 설정한다.
RunPod Worker 저장소/모델/LoRA의 재빌드는 필요 없다. 새 작업마다 전달되는 webhook
인자가 완료 통지를 활성화한다. 이미 제출된 작업에 소급 적용되지는 않는다.

## 운영 확인

```bash
sudo systemctl status biddingflow-api.service
sudo journalctl -u biddingflow-api.service --since '10 minutes ago'
```

```sql
SELECT job_id, runpod_job_id, status, retry_count,
       created_at, callback_at, processed_at, last_error
FROM procurement.quotation_extraction_job
ORDER BY created_at DESC LIMIT 30;
```

상태 흐름: SUBMITTING → SUBMITTED → READY → REGISTERED → COMPLETED.
테이블의 결과에는 견적 데이터가 포함되므로 일반 공개 API로 노출하지 않는다.

## 관련 코드

- `backend_logic2/integrations/quotation_extraction/runpod.py`: 요청 구성·제출·인증 조회
- `backend_logic2/services/quotation_service.py`: 기존 이메일 추출의 비동기 제출 분기
- `backend_logic2/services/runpod_quotation_jobs.py`: 제출·검증·등록·복구
- `backend_logic2/api/runpod_routes.py`: 완료 신호 수신
- `backend_logic2/repositories/quotation_jobs.py`: 영속 상태·동시성 제어
- `migrations/013_create_runpod_quotation_jobs.sql`: 추가 테이블
- `main.py`: 콜백 라우터와 복구 루프 연결

공식 문서: https://docs.runpod.io/serverless/endpoints/send-requests
