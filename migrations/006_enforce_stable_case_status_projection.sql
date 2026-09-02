-- Keep procurement_case as a stable frontend/API read model.
--
-- LangGraph's detailed node status belongs in workflow_snapshot and
-- case_status_history.  Only the projection service may store one of the
-- stable lifecycle values below in procurement_case.status.

WITH mapped AS MATERIALIZED (
    SELECT
        pc.case_id,
        pc.status AS old_status,
        pc.stage AS old_stage,
        pc.workflow_snapshot #>> '{values,status}' AS graph_status,
        CASE pc.workflow_snapshot #>> '{values,status}'
            WHEN 'awaiting_substitute_selection' THEN 'WAITING_INPUT'
            WHEN 'awaiting_supplier_approval' THEN 'WAITING_INPUT'
            WHEN 'awaiting_quotation_check' THEN 'WAITING_INPUT'
            WHEN 'awaiting_final_selection' THEN 'WAITING_INPUT'
            WHEN 'supplier_selected' THEN 'WAITING_INPUT'
            WHEN 'awaiting_po_approval' THEN 'WAITING_INPUT'
            WHEN 'substitute_selected' THEN 'CANCELLED'
            WHEN 'urgent_no_supplier_cancelled' THEN 'CANCELLED'
            WHEN 'human_review' THEN 'FAILED'
            WHEN 'catalog_purchase_required' THEN 'FAILED'
            ELSE 'RUNNING'
        END AS new_status,
        CASE pc.workflow_snapshot #>> '{values,status}'
            WHEN 'started' THEN 'MR_REVIEW'
            WHEN 'checking_mr_item' THEN 'ITEM_CHECK'
            WHEN 'awaiting_substitute_selection' THEN 'SUBSTITUTE_DECISION'
            WHEN 'substitute_selected' THEN 'SUBSTITUTE_SELECTED'
            WHEN 'urgent_no_supplier_cancelled' THEN 'CANCELLED'
            WHEN 'checking_bidding' THEN 'BIDDING_DECISION'
            WHEN 'catalog_purchase_required' THEN 'HUMAN_REVIEW'
            WHEN 'resolving_suppliers' THEN 'SUPPLIER_RECOMMENDATION'
            WHEN 'resolving_supplier_pool' THEN 'SUPPLIER_RECOMMENDATION'
            WHEN 'searching_suppliers' THEN 'SUPPLIER_RECOMMENDATION'
            WHEN 'supplier_search_completed' THEN 'SUPPLIER_RECOMMENDATION'
            WHEN 'awaiting_supplier_approval' THEN 'RFQ_TARGET_SELECTION'
            WHEN 'creating_rfq' THEN 'RFQ_SENDING'
            WHEN 'awaiting_quotation_check' THEN 'QUOTATION_COLLECTION'
            WHEN 'awaiting_final_selection' THEN 'SUPPLIER_SELECTION'
            WHEN 'supplier_selected' THEN 'ORDER_START'
            WHEN 'awaiting_po_approval' THEN 'PRE_PO_APPROVAL'
            WHEN 'creating_po' THEN 'PO_CREATION'
            WHEN 'po_sent' THEN 'DELIVERY'
            WHEN 'human_review' THEN 'HUMAN_REVIEW'
            ELSE pc.stage
        END AS new_stage
    FROM procurement.procurement_case pc
    WHERE pc.status NOT IN ('COMPLETED', 'CANCELLED', 'REJECTED')
      AND pc.workflow_snapshot #>> '{values,status}' IN (
          'started', 'checking_mr_item', 'awaiting_substitute_selection',
          'substitute_selected', 'urgent_no_supplier_cancelled',
          'checking_bidding', 'catalog_purchase_required',
          'resolving_suppliers', 'resolving_supplier_pool',
          'searching_suppliers', 'supplier_search_completed',
          'awaiting_supplier_approval', 'creating_rfq',
          'awaiting_quotation_check', 'awaiting_final_selection',
          'supplier_selected', 'awaiting_po_approval', 'creating_po',
          'po_sent', 'human_review'
      )
), changed AS MATERIALIZED (
    SELECT *
    FROM mapped
    WHERE old_status IS DISTINCT FROM new_status
       OR old_stage IS DISTINCT FROM new_stage
), audit_rows AS (
    INSERT INTO procurement.workflow_status_history (
        case_id, from_status, to_status, stage, reason, triggered_by
    )
    SELECT
        case_id,
        old_status,
        new_status,
        new_stage,
        'Migration 006: LangGraph internal status repaired to stable API projection',
        'schema_migration'
    FROM changed
    RETURNING history_id
)
UPDATE procurement.procurement_case pc
SET status = changed.new_status,
    stage = changed.new_stage,
    last_error = CASE
        WHEN changed.graph_status = 'catalog_purchase_required'
            AND NULLIF(BTRIM(pc.last_error), '') IS NULL
        THEN '비딩 불필요(카탈로그/직접구매) 경로는 아직 자동 PO 생성 대상으로 연결되지 않았습니다.'
        ELSE pc.last_error
    END,
    updated_at = now(),
    version = pc.version + 1
FROM changed
WHERE pc.case_id = changed.case_id;

-- Fail fast if a graph node ever attempts to write its private status into
-- the frontend read model again.
ALTER TABLE procurement.procurement_case
    DROP CONSTRAINT IF EXISTS ck_procurement_case_stable_status;

ALTER TABLE procurement.procurement_case
    ADD CONSTRAINT ck_procurement_case_stable_status CHECK (
        status IN (
            'AWAITING_MR_REVIEW', 'QUEUED', 'RUNNING', 'WAITING_INPUT',
            'FAILED', 'COMPLETED', 'CANCELLED', 'REJECTED'
        )
    );
