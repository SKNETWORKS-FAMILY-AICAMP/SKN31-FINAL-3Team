# Command 기반 구매 프로세스

process_graph.py는 backend_logic2/nodes의 기존 업무 함수를 LangGraph
Command(update=..., goto=...)로 연결한다. 사람 승인과 며칠간의 견적 수집
대기는 interrupt()에서 멈추며, 같은 thread_id로 재개한다.

## 흐름

```text
MR 검사
  → 신규/비활성 품목 확인
  → [신규 품목 승인/반려]
  → 이상치 판단
  → 대체품 확인
  → [MR 승인/반려]
  → 비딩 판단
  → 기존 공급사 조회 / 신규 공급사 검색·등록
  → [RFQ 발송 공급사 승인]
  → RFQ 생성·제출
  → [견적 접수 마감까지 대기]
  → 외부 견적 추출·ERP 등록
  → ERPNext 전체 Supplier Quotation 재조회·검토·정렬
  → [최종 공급사 선택]
  → PO 생성·제출·발송
```

## 시작

```powershell
python -m backend_logic2.nodes.process_cli start `
  --mr MAT-MR-2026-00252 `
  --quotation-deadline 2026-09-02
```

기본 thread_id는 MR 이름이다. 실행은 첫 번째 사람 승인 지점에서 멈춘다.

## MR 승인/반려

```powershell
python -m backend_logic2.nodes.process_cli resume `
  --thread MAT-MR-2026-00252 `
  --decision approve
```

```powershell
python -m backend_logic2.nodes.process_cli resume `
  --thread MAT-MR-2026-00001 `
  --decision reject `
  --reason "요청 수량 재확인 필요"
```

## RFQ 발송 공급사 승인

후보 전체 승인:

```powershell
python -m backend_logic2.nodes.process_cli resume `
  --thread MAT-MR-2026-00001 `
  --action approve_all
```

RFQ를 Submit하되 공급사 메일은 보내지 않음:

```powershell
python -m backend_logic2.nodes.process_cli resume `
  --thread MAT-MR-2026-00001 `
  --action approve_all `
  --no-email
```

RFQ Draft 생성까지만 수행:

```powershell
python -m backend_logic2.nodes.process_cli resume `
  --thread MAT-MR-2026-00001 `
  --action approve_all `
  --draft-only
```

일부 업체만 선택:

```powershell
python -m backend_logic2.nodes.process_cli resume `
  --thread MAT-MR-2026-00001 `
  --suppliers "대영산업" "화진에스텍"
```

승인하면 실제 RFQ 생성·제출 단계로 이동한다. 운영 환경의 ERPNext 설정에
따라 공급사 메일이 발송될 수 있다.

## 견적 마감일 일괄 처리

ERPNext 포털 견적만 처리:

```powershell
python -m backend_logic2.nodes.process_cli resume `
  --thread MAT-MR-2026-00001 `
  --action process `
  --top-k 3
```

이메일 첨부 견적도 함께 처리:

```powershell
python -m backend_logic2.nodes.process_cli resume `
  --thread MAT-MR-2026-00001 `
  --action process `
  --manifest-path "C:/Users/Playdata/Desktop/1.png" `
  --top-k 3
```

manifest의 외부 파일은 로컬 Hugging Face 추출 후 ERPNext Draft로 등록된다.
manifest가 비어 있으면 모델을 실행하지 않고 ERPNext 견적만 처리한다.

## 최종 공급사 선택

```powershell
python -m backend_logic2.nodes.process_cli resume `
  --thread MAT-MR-2026-00252 `
  --supplier "대영산업"
```

이 재개는 실제 PO 생성·제출·메일 발송 단계로 이어질 수 있다.

## 상태 조회

```powershell
python -m backend_logic2.nodes.process_cli status `
  --thread MAT-MR-2026-00001
```

경로 오류나 일시적인 외부 API 오류로 노드 실행이 실패했지만 체크포인트의
next 작업이 남아 있으면 승인값을 다시 보내지 않고 해당 작업만 재시도한다.

```powershell
python -m backend_logic2.nodes.process_cli retry `
  --thread MAT-MR-2026-00001
```

체크포인트는 backend_logic2/process_checkpoints.sqlite에 로컬 저장된다.
LangSmith tracing 활성화 여부는 실행 환경의 LangChain/LangSmith 환경변수
설정을 그대로 따른다.
