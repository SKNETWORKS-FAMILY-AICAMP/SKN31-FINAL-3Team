CREATE TABLE procurement.item_group_assignment (
    item_group TEXT PRIMARY KEY,
    manager_user_id TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE procurement.item_group_assignment_head (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    revision INTEGER NOT NULL DEFAULT 1
);
INSERT INTO procurement.item_group_assignment_head(singleton, revision)
VALUES (true, 1);

-- Preserve the current effective assignments as editable database data.
INSERT INTO procurement.item_group_assignment(item_group, manager_user_id, updated_by)
SELECT DISTINCT ON (summary->>'item_group') summary->>'item_group', assigned_user_id, 'migration'
FROM procurement.procurement_case
WHERE NULLIF(summary->>'item_group', '') IS NOT NULL
  AND NULLIF(assigned_user_id, '') IS NOT NULL
ORDER BY summary->>'item_group', updated_at DESC
ON CONFLICT (item_group) DO NOTHING;