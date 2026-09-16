-- Published versions are append-only in the application. ERP data is untouched.
CREATE TABLE procurement.company_policy_version (
    version INTEGER PRIMARY KEY,
    policy JSONB NOT NULL,
    reason TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO procurement.company_policy_version(version, policy, reason, published_by)
VALUES (1, '{"rules":{"urgent_lead_days":7,"bidding_amount":20000000,"pattern_min_orders":3,"irregular_cv":0.5,"cycle_overdue_multiplier":1.5,"inactive_months":12,"min_competing_suppliers":3,"supplier_refresh_years":3,"quotation_priority":"price_then_delivery"},"guidance":{"item_specification":"","substitute_selection":""}}',
        '기존 코드의 구매 기준을 그대로 등록', 'migration');
CREATE TABLE procurement.company_policy_head (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    version INTEGER NOT NULL REFERENCES procurement.company_policy_version(version)
);
INSERT INTO procurement.company_policy_head VALUES (true, 1);
CREATE TABLE procurement.case_policy (
    case_id UUID PRIMARY KEY REFERENCES procurement.procurement_case(case_id) ON DELETE CASCADE,
    version INTEGER NOT NULL REFERENCES procurement.company_policy_version(version),
    pinned_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Existing requests must NOT silently inherit a future administrator edit.
INSERT INTO procurement.case_policy(case_id, version)
SELECT case_id, 1 FROM procurement.procurement_case;
