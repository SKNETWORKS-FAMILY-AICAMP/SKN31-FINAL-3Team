# RAG · Naver 공급사 검색 평가

검색 결과 자체의 품질을 평가하는 도구다. 이메일 확보나 LLM 관련성 필터를
적용하기 전의 원시 후보를 저장하므로, 검색 실패와 연락처 보강 실패를 구분할
수 있다.

## 평가 흐름

### 0. ERPNext에 평가용 Item 등록

`vendor_retrieval_queries.json`이 품목의 단일 원본이고, `item_code`가 CSV와
검색 평가 결과를 연결하는 키다. JSON을 수정한 뒤 다음 명령으로 ERPNext
Data Import CSV를 다시 만든다.

```powershell
python backend_logic2/evaluation/build_erpnext_item_import.py
```

ERPNext에서 `Data Import`를 열어 다음과 같이 선택한다.

- Document Type: `Item`
- Import Type: `Insert New Records`
- 파일: `erpnext_item_import_safety.csv`

CSV에는 현재 ERPNext Item DocType에서 확인한 필드 라벨을 사용했다. 기존
환경과 Custom Field 구성이 달라 열 매핑 경고가 나오면 Data Import 화면에서
동일한 Item 필드로 매핑한다. `ID`는 신규 등록이므로 비워 둔다.

모든 Item 등록이 끝난 뒤 대체품 관계를 두 번째로 Import한다.

- Document Type: `Item Alternative`
- Import Type: `Insert New Records`
- 파일: `erpnext_item_alternative_import.csv`

관계는 `Item Code → Alternative Item Code`이며 `Two-way=1`로 작성되어 양방향
대체가 가능하다. 대체품 CSV를 먼저 Import하면 Item Link 검증에 실패하므로
반드시 Item CSV를 먼저 등록한다.

마지막으로 품목별 재고를 `Stock Reconciliation` 새 문서에서 업로드한다.
이 DocType은 일반 `Data Import` 목록에 나오지 않으므로 해당 화면에서 찾지
않는다.

1. `Stock > Tools > Stock Reconciliation`에서 `New`를 누른다.
2. Company를 `SKN31 FINAL`, Purpose를 `Stock Reconciliation`로 설정한다.
3. Posting Date/Time을 JSON 설정값과 맞춘다.
4. Items 표의 `Upload`에서 `erpnext_stock_reconciliation_safety.csv`를 선택한다.
5. 36개 행을 확인한 뒤 저장하고 Submit한다.

36개 품목의 수량은 모두 다르게 설정되어 있다. 이 CSV의 `Quantity (Items)`는
입고 증감량이 아니라 `2026-08-25 09:00:00` 시점의 최종 장부재고다. Import만
하고 문서를 제출하지 않으면 실제 재고가 반영되지 않는다. `Valuation Rate (Items)`는 테스트용 가상 기준값이므로 실제 운영 전에는 매입단가에 맞게
교체한다. `build_erpnext_item_import.py`는 CSV만 생성하며 ERPNext에 문서를
등록하거나 제출하지 않는다.

### 1. 평가 품목 확정

`vendor_retrieval_queries.json`에 품목을 추가한다. 초기 pilot은
`evaluation_selection.item_codes`의 대표 품목 4개만 수집한다. 전체 품목을
한꺼번에 라벨링하지 말고 pilot 결과를 확인한 뒤 목록을 단계적으로 넓힌다.

### 2. 검색 결과 스냅샷 수집

프로젝트 루트에서 실행한다.

```powershell
python backend_logic2/evaluation/vendor_retrieval_eval.py collect
```

결과는 기본적으로 `vendor_retrieval_snapshot.json`에 저장된다. RAG, Naver,
ERP 기존 승인 공급사를 독립적으로 수집하며 한 소스가 실패해도 나머지는 계속
수집한다. Naver 후보는 웹문서 검색 제목이 아니라 업체·기관 엔터티를 반환하는
Local 검색에서 수집한다. 제품 세부명·품목군·업종 쿼리를 합치고 업체명과 주소로
중복을 제거한다.

수집 개수 변경 예시:

```powershell
python backend_logic2/evaluation/vendor_retrieval_eval.py collect --rag-k 30 --naver-k 30 --category-k 8
```

### 3. 사람 검토용 라벨 시트 생성

```powershell
python backend_logic2/evaluation/vendor_retrieval_eval.py make-label-sheet
```

기본값은 각 검색 소스의 Top 5까지만 포함한다. 다른 깊이가 필요하면 다음처럼
지정한다.

```powershell
python backend_logic2/evaluation/vendor_retrieval_eval.py make-label-sheet --depth 10
```

생성된 `vendor_retrieval_labels.json`에서 각 후보의 `relevance`를 입력한다.
ERP `Item.supplier_items`에 등록된 기존 승인 공급사는 관련도 `3`과 시스템
근거가 자동 입력되지만, 정보가 오래됐거나 현재 공급이 불가능하면 담당자가
수정한다.

- `0`: 무관
- `1`: 검토 가능
- `2`: 적합
- `3`: 매우 적합

`evidence`, `verified_by`, `verified_at`도 함께 기록하고 품목의
`labeling_status`를 `complete`로 바꾼다.

JSON을 직접 수정하지 않으려면 대화형 명령을 사용한다. 입력할 때마다 파일에
저장되므로 중간에 `q`로 종료하고 나중에 이어서 할 수 있다.

```powershell
python backend_logic2/evaluation/vendor_retrieval_eval.py label --reviewer "홍길동"
```

특정 품목만 처리할 수도 있다.

```powershell
python backend_logic2/evaluation/vendor_retrieval_eval.py label --item-code SAF-HLM-001 --reviewer "홍길동"
```

중요: 자동 검색 결과만 라벨링하면 Recall을 올바르게 측정할 수 없다. 구매
담당자가 알고 있는 기존 업체, 협회·인증 목록 또는 별도 수동 조사에서 발견한
적합 업체도 `candidates`에 직접 추가해야 한다. 이때 `observed_sources`는 다음처럼
기록할 수 있다.

```json
{
  "candidate_name": "사람이 별도로 확인한 업체명",
  "vendor_id": null,
  "business_no": null,
  "domains": ["example.co.kr"],
  "aliases": ["검색 결과에 나타날 수 있는 별칭"],
  "observed_sources": [{"source": "human_reference", "rank": null, "url": "https://example.co.kr"}],
  "relevance": 3,
  "evidence": "제품 카탈로그와 납품 가능 지역을 담당자가 확인",
  "verified_by": "구매담당자 이름",
  "verified_at": "2026-08-25"
}
```

### 4. 완료 라벨을 Gold 데이터로 변환

```powershell
python backend_logic2/evaluation/vendor_retrieval_eval.py build-gold
```

미입력 라벨이나 `complete`가 아닌 품목이 있으면 Gold 생성을 중단한다. 검색
결과를 임의로 정답으로 확정하는 것을 막기 위한 장치다.

### 5. 오프라인 평가

```powershell
python backend_logic2/evaluation/vendor_retrieval_eval.py evaluate
```

기본적으로 RAG, Naver, 두 결과의 RRF 결합인 Hybrid를 `K=1,3,5`에서
비교한다. Hybrid에서는 PDF·HWP·XLS 같은 다운로드 문서 후보를 결합 전에
제외한다. 상세 결과는 `vendor_retrieval_report.json`에 저장된다.

## 지표 해석

- `Precision@K`: 상위 K개 중 적합 공급사의 비율
- `Pool Recall@K`: 라벨된 후보 풀의 적합 공급사 중 상위 K개에서 찾은 비율.
  전체 시장의 공급사를 얼마나 찾았는지는 의미하지 않는다.
- `nDCG@K`: 관련도 3·2·1의 좋은 업체가 위에 배치된 정도
- `MAP@K`: 적합 공급사가 상위 순위에 반복적으로 배치되는 정도
- `Qualified Precision/Recall@K`: 관련도 2 이상인 실제 RFQ 검토 가능 후보 기준
- `MRR@K`: 첫 적합 업체가 얼마나 빨리 등장하는지
- `Coverage@K`: 적합 업체를 하나 이상 찾은 품목의 비율. 보고서의 `hit` 매크로 평균
- `Judged/Unjudged Rate@K`: 상위 K개 중 사람이 판정한 후보와 미판정 후보의 비율
- `Vendor Validity@K`: 상위 K개 중 기업 엔터티 형식을 만족하는 후보의 비율
- `Document Rate@K`: PDF·HWP·XLS 등 문서 다운로드 결과의 비율
- `Duplicate Rate@K`: 동일 업체가 중복 노출된 비율

보고서의 `gold_diagnostics.pool_bias_warning`도 반드시 확인한다. 독립적인
`human_reference` 또는 ERP `existing` 정답이 0건이면 같은 검색기가 찾아온
후보로 그 검색기를 평가하는 순환 평가이므로 Precision과 nDCG가 과대평가될 수
있다.

초기에는 `Qualified Precision`, `Vendor Validity`, `Judged Rate`, `nDCG`를
함께 본다. `Pool Recall`만 높아도 미판정 후보가 많으면 신뢰할 수 없다.

## Gold 데이터 운영 규칙

- 동일 업체의 표기 차이는 `aliases`에 기록한다.
- 공식 도메인은 `domains`에 기록해 Naver 제목이 달라도 일치시킬 수 있게 한다.
- 사업자번호가 있으면 가장 강한 일치 키로 사용한다.
- 근거 없는 관련도 라벨은 만들지 않는다.
- 품목, 검토자, 검토일, 근거를 함께 보존한다.
- 검색 로직이나 임베딩 모델을 바꾸면 새 후보가 생기므로 snapshot을 다시 만들고,
  기존 라벨을 보존한 상태에서 새 후보를 추가 판정한 뒤 Gold를 재생성한다.
