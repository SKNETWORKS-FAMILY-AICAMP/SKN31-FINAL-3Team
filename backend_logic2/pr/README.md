# Supplier PR module

내부 결재가 끝난 공급사에게 수주 의사를 묻고, 공급사가 수락하면 기존
ERPNext PO 생성 함수를 실행하는 독립 모듈이다. 기존 애플리케이션 파일은
수정하지 않은 상태이므로 아래 연결 작업 전까지 라우터와 워크플로에는
자동으로 적용되지 않는다.

## 제공 기능

- 단일 사용 토큰이 포함된 `수주 수락` / `수주 거절` 이메일 버튼
- 메일 보안 스캐너 오작동을 막는 GET 확인 화면 + POST 확정 방식
- 수주 거절 사유 필수 입력 및 BiddingFlow 조회 API 노출
- 중복 응답과 만료 응답의 원자적 차단
- 수락 후 기존 `create_and_send_po` 또는 `create_and_send_direct_po` 실행
- PO 생성 성공·실패 결과 저장

## 적용 순서

1. `migrations/011_create_supplier_purchase_response.sql`을 적용한다.
2. 외부 공급사가 접근할 HTTPS 주소를 환경 변수로 지정한다.

   `BIDDINGFLOW_PUBLIC_URL=https://구매서비스.example.com`

3. FastAPI 앱에 공개/내부 라우터를 등록한다. 공개 라우터는 공급사 링크이므로
   로그인 의존성을 걸지 않고, 내부 라우터에는 기존 인증 의존성을 적용한다.

   ```python
   from backend_logic2.pr import internal_router, public_router

   app.include_router(public_router)
   app.include_router(
       internal_router,
       dependencies=[Depends(require_authenticated_user)],
   )
   ```

4. 기존 내부 PO 승인 노드의 승인 분기에서 `create_po` 대신
   `create_and_send_pr(...)`을 호출하고 `WAITING_SUPPLIER_REPLY` 상태로 둔다.
5. BiddingFlow 화면은 `GET /api/procurement/pr?case_id=...` 결과의 `status`,
   `rejection_reason`, `po_name`, `po_error`를 표시한다.

## 상태 흐름

`DRAFT → SENT → ACCEPTED → PO_CREATED`

거절은 `SENT → REJECTED`, PO 연동 오류는 `ACCEPTED → PO_FAILED`로 기록된다.
`PO_FAILED`는 수락 사실을 보존하므로 담당자가 원인을 해결한 뒤 별도의 안전한
재시도 작업으로 처리해야 한다.
