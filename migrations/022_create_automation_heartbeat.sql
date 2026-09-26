-- 자동 진행 스캔이 마지막으로 언제 돌았는지.
--
-- 스캔은 API 프로세스 안에서 조용히 도는데, 서버 로그를 볼 수 없는 사람은
-- "지금 돌고 있는 게 맞나"를 확인할 방법이 없었다. 한 줄짜리 상태를 남겨
-- 화면이나 진단 스크립트에서 바로 볼 수 있게 한다.
CREATE TABLE IF NOT EXISTS procurement.automation_heartbeat (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    last_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    interval_seconds INTEGER,
    ran_by VARCHAR(140),
    result JSONB NOT NULL DEFAULT '{}'::jsonb
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biddingflow_team') THEN
        GRANT SELECT, INSERT, UPDATE ON procurement.automation_heartbeat TO biddingflow_team;
    END IF;
END
$$;
