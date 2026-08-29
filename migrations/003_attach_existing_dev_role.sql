-- 기존 팀 공유 개발 계정에 구매 운영 스키마 권한 그룹을 연결한다.
-- nexterp_dev가 없는 신규 환경에서도 마이그레이션이 실패하지 않게 처리한다.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexterp_dev') THEN
        GRANT biddingflow_team TO nexterp_dev;
        ALTER ROLE nexterp_dev SET search_path = procurement, public;
    END IF;
END
$$;
