# ERPNext 기준 통합 견적 처리 모듈

ERPNext 공급사 포털에서 작성된 견적은 그대로 사용한다. 이메일·Excel·PDF·
이미지·텍스트로 받은 외부 견적만 공통 스키마로 추출해 ERPNext Draft로
등록한다. 이후 review와 rank 단계는 출처를 구분하지 않고 RFQ에 연결된 모든
ERPNext `Supplier Quotation`을 다시 조회한 결과만 사용한다.

```text
포털 입력 ───────────────────────────┐
외부 파일 → extractor → registrar ──┴→ ERPNext → reviewer → ranker
```

## 모듈 구성

| 파일                       | 책임                                            | 단독 입력                | 단독 출력                |
| -------------------------- | ----------------------------------------------- | ------------------------ | ------------------------ |
| `quotation_extractor.py` | 형식 분류, 텍스트/비전 추출, description 세분화 | 견적 파일                | `Quotation` JSON       |
| `get_supplier_quotations.py` | ERP 견적 전체 조회 및 `Quotation` 정규화   | RFQ 이름                 | `Quotation` 목록        |
| `quotation_reviewer.py`  | ERP 정규화 견적의 타입, 산식, 규격 검토         | `Quotation` + RFQ      | `QuotationReview` JSON |
| `quotation_ranker.py`    | 규격 → 총금액 → 납기 정렬                     | 검토 결과 배열           | `RankingResult` JSON   |
| `quotation_registrar.py` | 추출 결과를 RFQ에 연결된 ERPNext Draft로 등록   | `Quotation` JSON/객체    | 등록 상태                 |
| `quotation_pipeline.py`  | 외부 추출·등록 후 ERP 전체 재조회·검토·정렬     | manifest + RFQ 이름/JSON | 단계별 결과               |

공통 Pydantic 모델은 `quotation_models.py`에만 정의되어 있다. 따라서 한
모듈의 출력 JSON을 저장한 뒤 다음 모듈만 반복 실행할 수 있다.

## 보안 및 Hugging Face 모델

견적 원문·이메일·이미지는 외부 API로 보내지 않는다. 추출기는
`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`,
`local_files_only=True`, `trust_remote_code=False`를 강제한다.

현재 테스트용 텍스트 모델은 로컬 캐시의 `Qwen/Qwen3-0.6B`, 비전 모델은
`Qwen/Qwen2-VL-2B-Instruct`다. 운영 환경에서는 정확도 검증을 마친 사내
모델 디렉터리를 지정하는 것을 권장한다.

```dotenv
HF_QUOTATION_TEXT_MODEL=Qwen/Qwen3-0.6B
HF_QUOTATION_VISION_MODEL=Qwen/Qwen2-VL-2B-Instruct
HF_QUOTATION_MAX_NEW_TOKENS=768
```

모델이 로컬에 없으면 Hugging Face Hub에서 자동 다운로드하지 않고 오류로
중단한다. 필요한 모델은 인터넷 접근이 허용된 별도 환경에서 보안 검토 후
사내 모델 저장소로 반입해야 한다.

## RFQ 입력 예시

```json
{
  "rfq_name": "PUR-RFQ-2026-00270",
  "currency": "KRW",
  "items": [
    {
      "item_code": "ITEM-001",
      "item_name": "스테인리스 볼트",
      "quantity": 100,
      "required_delivery_date": "2026-09-10",
      "specifications": {
        "material": "SUS304",
        "length": "10 mm"
      },
      "numeric_tolerance_percent": 2
    }
  ]
}
```

`description`에 한 문장으로 들어온 재질·치수 등은 추출 단계에서
`items[].specifications`의 세부 키로 분해된다.

## 단계별 실행

```powershell
python backend_logic2/nodes/quotation_filter/quotation_extractor.py quote.xlsx `
  --rfq PUR-RFQ-2026-00270 --supplier-name "샘플상사" `
  --rfq-context rfq_requirements.json `
  --output extracted.json

python backend_logic2/nodes/quotation_filter/quotation_reviewer.py extracted.json `
  --rfq rfq_requirements.json --output reviewed.json

python backend_logic2/nodes/quotation_filter/quotation_ranker.py reviewed.json `
  --rfq rfq_requirements.json --top-k 3 --output ranked.json
```

로컬 JSON을 남기지 않고 추출 결과를 ERPNext Supplier Quotation Draft로 바로
등록할 수 있다. 외부 견적번호는 `quotation_number`, RFQ 연결은 각 품목의
`request_for_quotation`과 `request_for_quotation_item`에 저장된다.

```powershell
python -m backend_logic2.nodes.quotation_filter.quotation_extractor `
  "C:/quotes/vendor-a.png" `
  --rfq PUR-RFQ-2026-00295 `
  --supplier-name "유비에스상사" `
  --register-erp
```

등록 전에 실제 ERPNext 매핑과 중복 여부만 확인하려면 다음처럼 실행한다.

```powershell
python -m backend_logic2.nodes.quotation_filter.quotation_registrar `
  extracted.json --dry-run
```

같은 공급사·RFQ·외부 견적번호와 값이 이미 있으면 `already_exists`를 반환하고
새 문서를 만들지 않는다. 같은 외부 견적번호인데 금액이 다르면 충돌로 중단한다.
등록된 외부 견적은 포털 입력 견적과 함께 같은 API 조회 모듈에서 즉시 조회된다.

```powershell
python -m backend_logic2.nodes.quotation_filter.get_supplier_quotations PUR-RFQ-2026-00295 --json
```

승인된 로컬 모델 경로는 CLI에서도 직접 지정할 수 있다.

```powershell
python backend_logic2/nodes/quotation_filter/quotation_extractor.py quote.png `
  --rfq PUR-RFQ-2026-00270 `
  --text-model D:/approved-models/Qwen3-4B-Instruct `
  --vision-model D:/approved-models/Qwen2-VL-2B-Instruct `
  --output extracted.json
```

추출 재시험에는 이전 오류를 reflection으로 직접 넣을 수 있다.

```powershell
python backend_logic2/nodes/quotation_filter/quotation_extractor.py quote.pdf `
  --rfq PUR-RFQ-2026-00270 --attempt 2 `
  --reflection "ITEM_AMOUNT_MISMATCH: 수량×단가 재확인" `
  --output extracted_retry.json
```

## 전체 실행 manifest

```json
{
  "quotations": [
    {
      "path": "C:/quotes/vendor-a.xlsx",
      "supplier_id": "SUP-001",
      "supplier_name": "A상사",
      "quotation_id": "A-2026-08"
    },
    {
      "path": "C:/quotes/vendor-b.eml",
      "supplier_name": "B상사"
    },
    {"channel": "portal"}
  ]
}
```

```powershell
python -m backend_logic2.nodes.quotation_filter.quotation_pipeline manifest.json `
  --rfq PUR-RFQ-2026-00295 `
  --top-k 3 --output-dir quotation_results
```

이미 ERPNext에 등록된 포털/외부 견적만 review·rank하는 경우에는 manifest를
생략한다.

```powershell
python -m backend_logic2.nodes.quotation_filter.quotation_pipeline `
  --rfq PUR-RFQ-2026-00295 `
  --top-k 3 --output-dir quotation_results
```

결과는 `01_extracted_external.json`, `02_registered.json`,
`03_erp_quotations.json`, `04_reviewed.json`, `05_ranked.json`으로 분리된다.
manifest의 `channel=portal` 항목은 별도 추출하지 않는다. 포털 견적을 포함한
ERPNext의 전체 연결 견적은 등록 단계가 끝난 뒤 한 번에 다시 조회된다.
외부 파일의 구조화 자체가 실패하면 최대 3회 추출하며, 등록 또는 추출 실패는
`human_review` 근거로 남긴다.

## 정렬 및 검토 규칙

- Pydantic 타입 검증과 사업자등록번호 체크섬 검증
- `수량 × 단가 = 품목 금액`, 품목합 = 공급가액, 공급가액 + 세액 = 총금액
- RFQ와 견적의 `item_code`가 정확히 일치하면 설명에서 생략된 품목 규격은
  해당 품목 규격으로 인정하되, 명시된 규격이 충돌하면 부적합 처리
- `mm/cm/m`, `mg/g/kg`, `ml/l` 단위 정규화와 허용오차
- 편집거리 기반 문자열 유사도와 재질 동의어 정규화
- 규격 통과 견적만 총금액 오름차순, 동일 금액이면 납기 오름차순
- 상위 k 경계에서 동점이면 동점 업체를 모두 출력
- 모든 제외/재추출/사람 검토 결과에 `rejection_evidence` 기록

## 오프라인 테스트

```powershell
python -m unittest discover -s backend_logic2/nodes/quotation_filter/tests -v
```
