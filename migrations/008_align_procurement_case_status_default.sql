-- Keep the column default inside the stable status vocabulary enforced by 006.
--
-- Older installations first created procurement_case.status with DEFAULT
-- 'created'. Migration 005 used ADD COLUMN IF NOT EXISTS, so that legacy
-- default survived even though migration 006 later rejected 'created' through
-- ck_procurement_case_stable_status. Inserts that omit status could therefore
-- fail at runtime. The application starts every new MR at MR review.

ALTER TABLE procurement.procurement_case
    ALTER COLUMN status SET DEFAULT 'AWAITING_MR_REVIEW';

COMMENT ON COLUMN procurement.procurement_case.status IS
    'Stable BiddingFlow case status; defaults to AWAITING_MR_REVIEW for a newly discovered MR.';
