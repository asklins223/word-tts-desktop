-- v0011 durable system-input unit boundary decisions
--
-- A parser's automatic grouping is evidence, not a user decision.  Keep the
-- final boundary choice by stable work-item IDs so a later reparse cannot
-- silently replace an explicit single/multiple-unit confirmation.

CREATE TABLE input_unit_boundary_decisions (
    decision_id              TEXT PRIMARY KEY,
    workflow_id              TEXT NOT NULL UNIQUE,
    decision_type            TEXT NOT NULL CHECK (decision_type IN ('single', 'multiple')),
    boundaries_json          TEXT NOT NULL CHECK (json_valid(boundaries_json)),
    automatic_status         TEXT NOT NULL CHECK (
        automatic_status IN ('single_default', 'multiple_confirmed', 'multiple_candidate')
    ),
    automatic_evidence_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(automatic_evidence_json)),
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
);

CREATE INDEX idx_input_unit_boundary_decisions_workflow
    ON input_unit_boundary_decisions(workflow_id, updated_at DESC);
