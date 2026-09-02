-- AI 구매 프로세스용 운영 보조 스키마.
-- ERPNext DocType의 원본 데이터는 복제하지 않고 ERP 문서 ID만 참조한다.

CREATE SCHEMA IF NOT EXISTS procurement;

COMMENT ON SCHEMA procurement IS
    'AI procurement workflow state, decisions, reference data, and supplier performance.';

CREATE TABLE IF NOT EXISTS procurement.procurement_case (
    id SERIAL PRIMARY KEY,
    mr_id VARCHAR(50) NOT NULL,
    mr_item_id VARCHAR(50) NOT NULL,
    item_code VARCHAR(100) NOT NULL,
    item_name VARCHAR(255),
    status VARCHAR(30) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_procurement_case_mr_item UNIQUE (mr_id, mr_item_id)
);

COMMENT ON TABLE procurement.procurement_case IS
    'Current workflow state for each item row in an ERPNext Material Request.';
COMMENT ON COLUMN procurement.procurement_case.mr_id IS
    'Logical reference to ERPNext Material Request.name.';
COMMENT ON COLUMN procurement.procurement_case.mr_item_id IS
    'Logical reference to an ERPNext Material Request Item row.';
COMMENT ON COLUMN procurement.procurement_case.status IS
    'Current pipeline state. Kept extensible while the end-to-end workflow is evolving.';

CREATE TABLE IF NOT EXISTS procurement.case_status_history (
    id SERIAL PRIMARY KEY,
    case_id INTEGER NOT NULL
        REFERENCES procurement.procurement_case(id) ON DELETE CASCADE,
    from_status VARCHAR(30),
    to_status VARCHAR(30) NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason TEXT,
    triggered_by VARCHAR(50)
);

COMMENT ON TABLE procurement.case_status_history IS
    'Append-only audit history of procurement case status transitions.';

CREATE TABLE IF NOT EXISTS procurement.ai_decision_log (
    id SERIAL PRIMARY KEY,
    case_id INTEGER
        REFERENCES procurement.procurement_case(id) ON DELETE SET NULL,
    node_name VARCHAR(50) NOT NULL,
    decision VARCHAR(50),
    reasoning TEXT,
    input_data JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE procurement.ai_decision_log IS
    'Reproducible AI decision records shared by procurement pipeline nodes.';

CREATE TABLE IF NOT EXISTS procurement.item_group_spec (
    item_group VARCHAR(140) PRIMARY KEY,
    required_specs JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE procurement.item_group_spec IS
    'Cached required specification fields for each ERPNext Item Group.';

CREATE TABLE IF NOT EXISTS procurement.narajangteo_company_info (
    bizno VARCHAR(140) NOT NULL,
    company_name VARCHAR(140) NOT NULL,
    sigungu VARCHAR(140),
    head_branch_cls VARCHAR(140),
    country VARCHAR(140),
    company_type VARCHAR(140),
    main_biz_type VARCHAR(140) NOT NULL,
    is_manufacturer CHAR(1),
    main_item_no VARCHAR(140) NOT NULL,
    main_item_name VARCHAR(140),
    reg_date DATE,
    biz_status_code VARCHAR(140),
    CONSTRAINT pk_narajangteo_company_info
        PRIMARY KEY (bizno, main_item_no, main_biz_type)
);

COMMENT ON TABLE procurement.narajangteo_company_info IS
    'Preprocessed local reference of registered companies from Narajangteo.';

CREATE TABLE IF NOT EXISTS procurement.substitute_item_mapping (
    id SERIAL PRIMARY KEY,
    original_item_code VARCHAR(100) NOT NULL,
    substitute_item_code VARCHAR(100) NOT NULL,
    is_bidirectional BOOLEAN NOT NULL DEFAULT false,
    ai_reasoning TEXT,
    confidence VARCHAR(10),
    approved_by VARCHAR(50),
    approved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_substitute_item_mapping_pair
        UNIQUE (original_item_code, substitute_item_code),
    CONSTRAINT ck_substitute_item_mapping_not_self
        CHECK (original_item_code <> substitute_item_code)
);

COMMENT ON TABLE procurement.substitute_item_mapping IS
    'One-to-many substitute relationships between ERPNext item codes.';

CREATE TABLE IF NOT EXISTS procurement.supplier_performance (
    id SERIAL PRIMARY KEY,
    supplier_id VARCHAR(50) NOT NULL,
    item_group VARCHAR(140) NOT NULL,
    total_orders INTEGER NOT NULL DEFAULT 0,
    on_time_count INTEGER NOT NULL DEFAULT 0,
    late_count INTEGER NOT NULL DEFAULT 0,
    avg_delay_days DECIMAL(5, 1),
    last_transaction_date DATE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_supplier_performance_supplier_group
        UNIQUE (supplier_id, item_group),
    CONSTRAINT ck_supplier_performance_counts_nonnegative
        CHECK (total_orders >= 0 AND on_time_count >= 0 AND late_count >= 0)
);

COMMENT ON TABLE procurement.supplier_performance IS
    'Aggregated supplier performance for each ERPNext item group.';

-- Pipeline recovery, audit, search, and ranking paths used by the application.
CREATE INDEX IF NOT EXISTS idx_procurement_case_status
    ON procurement.procurement_case (status);
CREATE INDEX IF NOT EXISTS idx_procurement_case_mr_id
    ON procurement.procurement_case (mr_id);
CREATE INDEX IF NOT EXISTS idx_procurement_case_item_code
    ON procurement.procurement_case (item_code);
CREATE INDEX IF NOT EXISTS idx_case_status_history_case_changed
    ON procurement.case_status_history (case_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_decision_log_case_created
    ON procurement.ai_decision_log (case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_decision_log_node_created
    ON procurement.ai_decision_log (node_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_narajangteo_company_name
    ON procurement.narajangteo_company_info (company_name);
CREATE INDEX IF NOT EXISTS idx_narajangteo_main_item_name
    ON procurement.narajangteo_company_info (main_item_name);
CREATE INDEX IF NOT EXISTS idx_narajangteo_main_item_no
    ON procurement.narajangteo_company_info (main_item_no);
CREATE INDEX IF NOT EXISTS idx_substitute_item_mapping_original
    ON procurement.substitute_item_mapping (original_item_code);
CREATE INDEX IF NOT EXISTS idx_substitute_item_mapping_substitute
    ON procurement.substitute_item_mapping (substitute_item_code);
CREATE INDEX IF NOT EXISTS idx_supplier_performance_item_group
    ON procurement.supplier_performance (item_group);

-- updated_at을 애플리케이션마다 빠뜨리지 않도록 DB에서 일관되게 갱신한다.
CREATE OR REPLACE FUNCTION procurement.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_procurement_case_updated_at
    ON procurement.procurement_case;
CREATE TRIGGER trg_procurement_case_updated_at
BEFORE UPDATE ON procurement.procurement_case
FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();

DROP TRIGGER IF EXISTS trg_item_group_spec_updated_at
    ON procurement.item_group_spec;
CREATE TRIGGER trg_item_group_spec_updated_at
BEFORE UPDATE ON procurement.item_group_spec
FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();

DROP TRIGGER IF EXISTS trg_supplier_performance_updated_at
    ON procurement.supplier_performance;
CREATE TRIGGER trg_supplier_performance_updated_at
BEFORE UPDATE ON procurement.supplier_performance
FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();

-- 로그인 계정과 권한 묶음을 분리한다. 실제 팀원 계정은 이 NOLOGIN 그룹에
-- 소속시키며, PostgreSQL superuser 비밀번호를 공유하지 않는다.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biddingflow_team') THEN
        CREATE ROLE biddingflow_team NOLOGIN;
    END IF;
END
$$;

DO $$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO biddingflow_team',
        current_database()
    );
END
$$;

GRANT USAGE ON SCHEMA procurement TO biddingflow_team;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA procurement TO biddingflow_team;
GRANT USAGE, SELECT, UPDATE
    ON ALL SEQUENCES IN SCHEMA procurement TO biddingflow_team;

ALTER DEFAULT PRIVILEGES IN SCHEMA procurement
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO biddingflow_team;
ALTER DEFAULT PRIVILEGES IN SCHEMA procurement
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO biddingflow_team;
