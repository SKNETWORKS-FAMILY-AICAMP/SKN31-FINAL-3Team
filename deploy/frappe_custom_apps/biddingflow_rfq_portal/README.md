# BiddingFlow RFQ Portal

ERPNext의 코어 파일을 수정하지 않고 공급사 RFQ 포털을 확장하는 Frappe 앱입니다.

- Supplier Quotation 헤더의 `valid_till` 입력
- Supplier Quotation Item별 `expected_delivery_date` 입력
- 포털 입력값의 서버측 필수값/날짜 검증
- ERPNext 기본 포털 사용자 권한 검사 유지
- 동일 RFQ + 동일 공급사의 활성 견적(초안 포함)이 있으면 기존 견적으로 이동
- RFQ 행 잠금과 견적 잠금 조회로 포털의 동시 제출 중복 생성 방지
- 제출 후 재접속 시 작성 버튼을 숨기고 제출 완료 및 기존 견적 목록 표시

중복 제출 정책은 포털 생성 API에 적용됩니다. 기존 중복 견적은 삭제하거나
취소하지 않습니다. 취소된 견적은 활성 견적으로 세지 않으며, 새 RFQ로
재비딩하면 다시 제출할 수 있습니다. ERP Desk나 별도 수입 API는 이 포털
전용 정책의 적용 대상이 아닙니다.

이 폴더의 변경은 BiddingFlow FastAPI 자동 배포만으로 운영 ERP에 반영되지
않습니다. 운영 ERPNext Compose 프로젝트의 커스텀 앱 빌드 경로에 소스를
반영하고, Dockerfile.rfq-portal로 이미지를 재빌드한 뒤 해당 이미지를 사용하는
서비스를 갱신해야 합니다. 갱신 후 사이트 캐시를 비우고 포털에서 최초 제출,
재접속, 같은 RFQ의 두 탭 제출, 새 RFQ 제출을 확인하세요. 기존 Supplier
Quotation을 변경하는 데이터 마이그레이션은 없습니다.

ERPNext/Frappe v16용으로 작성되었습니다.
