# 공급사 검색 평가

검색 결과 자체의 품질을 평가하는 도구다. 이메일 확보나 LLM 관련성 필터를
적용하기 전의 원시 후보를 저장하므로, 검색 실패와 연락처 보강 실패를 구분할
수 있다.

## 평가 흐름

### 1. 평가 품목 확정

`vendor_retrieval_queries.json`에 품목을 추가한다. 실제 수집 대상은
`evaluation_selection.item_codes`에 적힌 pilot 품목이며, 현재 기본값은 품목군별
대표 품목을 안전용품 5개, 사무용품 5개, 시약 5개로 구성한 총 15개다. 전체 품목을
한꺼번에 라벨링하지 말고 pilot 결과를 확인한 뒤 목록을 단계적으로 넓힌다.

#### 1.1. ERPNext 사무용품·시약 평가 데이터 갱신

`vendor_retrieval_queries.json`에는 안전용품 36개와 ERPNext에서 조회한
사무용품·시약 70개가 함께 들어 있다. 사무용품·시약 70개는 이미 ERPNext에
등록된 품목이며, 이 폴더에서는 공급사 검색 평가 대상으로만 사용한다.

다음 명령은 ERPNext Item에서 아래 세 품목군을 다시 조회해
기본 `vendor_retrieval_queries.json`에 병합한다.

- `사무용품`: 40개
- `시약 - 당류`: 10개
- `시약 - 무기염류`: 20개

```powershell
python backend_logic2/evaluation/build_erpnext_evaluation_queries.py
```

병합된 파일의 `items`에는 기존 안전용품 36개와 조회된 70개가 모두 들어간다. 기본
`evaluation_selection.item_codes`는 API 호출 및 초기 라벨링 부담을 줄이기 위해
안전용품·사무용품·시약의 대표 품목을 각각 5개씩 총 15개 선택한다.

```text
안전용품: SAF-HLM-001, SAF-GLV-001, SAF-GOG-001, SAF-EXT-001, SAF-MSK-001
사무용품: OFC-BRD-001, OFC-CLP-001, OFC-ERS-011, OFC-PAP-001, OFC-PEN-001
시약: REA-GLU-001-500G, REA-GLU-002-500G, REA-GLU-010-1KG,
      REA-KCL-001-500G, REA-NACL-001-500G
```

전체 활성 품목을 평가 대상으로 생성하려면 다음처럼 실행한다. 106개 전체에 대해
활성화된 검색 source를 호출하므로 pilot 결과를 확인한 뒤 사용하는 것을 권장한다.

```powershell
python backend_logic2/evaluation/build_erpnext_evaluation_queries.py --all
```

특정 품목군만 다시 가져오려면 `--groups` 뒤에 정확한 ERP 품목군명을 지정한다.
기본 세 품목군 외의 그룹을 지정하면 기존 `evaluation_selection`은 보존된다.
새 대표 품목도 추가하려면 `--pilot-codes CODE-1 CODE-2`를 함께 지정한다.

```powershell
python backend_logic2/evaluation/build_erpnext_evaluation_queries.py `
  --groups "사무용품" "시약 - 당류" "시약 - 무기염류"
```

ERP 연결 없이 이전 추출 JSON을 다시 병합해야 하면 다음 옵션을 사용할 수 있다.

```powershell
python backend_logic2/evaluation/build_erpnext_evaluation_queries.py `
  --source-json backend_logic2/evaluation/vendor_retrieval_queries_office_reagents.json
```

평가 수집기는 JSON의 `item_group`을 검색 모듈에 전달한다. 사무용품은
사무용품 도매·납품 쿼리, 두 시약군은 실험실·연구용 시약 쿼리를 사용하므로
안전용품 기본 검색어와 섞이지 않는다.

#### 1.2. 검색 API 교체와 collector 계약

기본 collector는
`resolve_supplier:collect_vendor_candidates_for_evaluation`이며 다음 형태만
계약으로 사용한다.

```python
def collect_vendor_candidates_for_evaluation(
    item, limit_per_source=20, source_limits=None, category_limit=5
):
    return {
        "contract_version": 1,
        "sources": {
            "검색기이름": [{"name": "업체명", "retrieval_rank": 1}]
        },
        "errors": {},
        "metadata": {}
    }
```

Naver를 다른 API로 바꾸면 `resolve_supplier.py`의 collector가 반환하는
`sources`에 새 source 이름과 후보 목록을 넣는다. 평가기의 snapshot 생성,
라벨 시트, source별 지표 및 복수 source Hybrid RRF는 source 이름을 자동으로
발견하므로 수정하지 않아도 된다. 기존 schema v2 snapshot의
`existing/rag/naver` 필드도 계속 읽을 수 있다.

collector를 별도 모듈로 구현했다면 코드 수정 없이 CLI에서 지정할 수 있다.

```powershell
python backend_logic2/evaluation/vendor_retrieval_eval.py collect `
  --collector my_vendor_provider:collect_for_evaluation `
  --source-k 20
```

새 collector는 위 함수 인자와 반환 계약을 구현해야 한다. 외부 API별 인증,
요청 파라미터 및 응답 필드 매핑 자체는 API마다 달라 자동화하지 않는다.

### 2. 검색 결과 스냅샷 수집

이하 명령은 모두 프로젝트 루트에서 실행한다.

```powershell
python backend_logic2/evaluation/vendor_retrieval_eval.py collect
```

기본 경로를 사용하면 `vendor_retrieval_queries.json`의 선택 품목을 읽어
`vendor_retrieval_snapshot.json`에 저장한다. 현재 기본 collector는 RAG, Naver
Local, ERP 기존 승인 공급사를 독립적으로 수집하며 한 source가 실패해도 나머지는
계속 수집한다. Naver 후보는 웹문서 검색 제목이 아니라 업체·기관 엔터티를
반환하는 Local 검색에서 가져온다. 제품 세부명·품목군·업종 쿼리를 합치고 업체명과
주소로 중복을 제거한다.

수집 개수 변경 예시:

```powershell
python backend_logic2/evaluation/vendor_retrieval_eval.py collect --source-k 30 --category-k 8
```

`--source-k`는 API 종류와 관계없이 source별 후보 수를 지정한다. 기존 실행 명령과의
호환을 위해 `--rag-k`, `--naver-k`도 계속 지원한다.

RAG 결과가 0건이면 실행 옵션보다 pgvector의 해당 품목 카테고리와
`vendor_item_category` 매핑 여부를 먼저 확인한다.

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

중요: 자동 검색 결과만 라벨링하면 검색기가 놓친 실제 업체가 Gold에 없어 Recall이
과대평가된다. 구매 담당자가 알고 있는 기존 업체, 계약 이력, 협회·인증 목록 또는
별도 수동 조사에서 확인한 적합 업체를 `add-reference`로 추가한다.

```powershell
python backend_logic2/evaluation/vendor_retrieval_eval.py add-reference `
  --item-code OFC-PAP-001 `
  --vendor-name "사람이 별도로 확인한 업체명" `
  --relevance 3 `
  --evidence "제품 카탈로그와 납품 가능 지역을 담당자가 확인" `
  --reviewer "구매담당자 이름" `
  --domain example.co.kr `
  --alias "검색 결과에 나타날 수 있는 별칭" `
  --source-url "https://example.co.kr/catalog"
```

`--business-no`도 사용할 수 있으며 `--domain`과 `--alias`는 여러 번 지정할 수
있다. `relevance`는 정답 후보이므로 `1~3`만 허용하고, 독립 확인 근거인
`--evidence`는 필수다. 같은 사업자번호·도메인·업체명의 후보가 이미 있으면 새 행을
만들지 않고 기존 후보에 `human_reference`를 병합한다. 이후 snapshot과 라벨 시트를
다시 생성해도 `human_reference` 후보와 판정은 유지된다.

품목별 명령을 반복하지 않으려면 담당 카테고리의 reference 파일에서 업체명,
적합도, 근거와 `reviewer`를 작성하고 카테고리별로 가져온다.

- `references_safety.json`: 안전용품 5개, 10행
- `references_office.json`: 사무용품 5개, 10행
- `references_reagents.json`: 시약 5개, 10행

각 품목에는 `relevance: 3`과 `relevance: 2` 행이 하나씩 있다. 두 행에 서로 다른
정답 업체명과 근거를 작성한다.

```powershell
python backend_logic2/evaluation/vendor_retrieval_eval.py import-references `
  --file backend_logic2/evaluation/references_safety.json`
  --reviewer "홍길동"
python backend_logic2/evaluation/vendor_retrieval_eval.py import-references `
  --file backend_logic2/evaluation/references_office.json`
  --reviewer "홍길동"
python backend_logic2/evaluation/vendor_retrieval_eval.py import-references `
  --file backend_logic2/evaluation/references_reagents.json `
  --reviewer "홍길동"
```

`name`이 빈 행은 아직 작성하지 않은 템플릿으로 보고 건너뛴다. `name`을 입력한
행은 `item_code`, `relevance(1~3)`, `evidence`를 검증하며 하나라도 잘못되면 파일
전체를 반영하지 않는다. 행별로 `business_no`, `source_url`을 추가할 수 있고,
행의 `reviewer`가 명령행의 `--reviewer` 기본값보다 우선한다.

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

snapshot에서 발견한 각 검색 source와, 검색 source가 둘 이상이면 이들을 RRF로
결합한 Hybrid를 기본 `K=1,3,5`에서 비교한다. Hybrid에서는 PDF·HWP·XLS 같은
다운로드 문서 후보를 결합 전에 제외한다. 상세 결과는
`vendor_retrieval_report.json`에 저장된다.

## 지표 해석

- `Precision@K`: 상위 K개 중 적합 공급사의 비율
- `Pool Recall@K`: 검색 후보와 사람이 독립 확인한 Gold 업체 중 상위 K개에서 찾은
  비율. 독립 조사가 시장 전체를 포괄하지 않는 한 전체 시장 Recall은 아니다.
- `nDCG@K`: 관련도 3·2·1의 좋은 업체가 위에 배치된 정도
- `MAP@K`: 적합 공급사가 상위 순위에 반복적으로 배치되는 정도
- `Qualified Precision/Recall@K`: 관련도 2 이상인 실제 RFQ 검토 가능 후보 기준
- `MRR@K`: 첫 적합 업체가 얼마나 빨리 등장하는지
- `Coverage@K`: 적합 업체를 하나 이상 찾은 품목의 비율. 보고서의 `hit` 매크로 평균
- `Judged/Unjudged Rate@K`: 상위 K개 중 사람이 판정한 후보와 미판정 후보의 비율
- `Vendor Validity@K`: 상위 K개 중 기업 엔터티 형식을 만족하는 후보의 비율
- `Document Rate@K`: PDF·HWP·XLS 등 문서 다운로드 결과의 비율
- `Duplicate Rate@K`: 동일 업체가 중복 노출된 비율

보고서의 `gold_diagnostics.pool_bias_warning`과
`items_without_independent_references`도 반드시 확인한다. 품목별로 독립적인
`human_reference` 또는 ERP `existing` 정답이 없으면 같은 검색기가 찾아온 후보로
그 검색기를 평가하는 순환 평가이므로 Precision과 nDCG가 과대평가될 수 있다.

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
