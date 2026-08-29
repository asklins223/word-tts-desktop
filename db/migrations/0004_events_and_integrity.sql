CREATE TABLE workflow_events (
    event_id        TEXT PRIMARY KEY,
    workflow_id     TEXT NOT NULL,
    seq             INTEGER NOT NULL CHECK (seq >= 1),
    mutation_id     TEXT NOT NULL UNIQUE,
    schema_version  TEXT NOT NULL,
    step_id         TEXT,
    item_id         TEXT,
    attempt_id      TEXT,
    request_id      TEXT,
    correlation_id  TEXT,
    causation_id    TEXT,
    actor_type      TEXT NOT NULL CHECK (actor_type IN ('USER', 'WORKER', 'RECOVERY', 'SCHEDULER', 'SYSTEM')),
    actor_id        TEXT,
    event_type      TEXT NOT NULL,
    phase           TEXT,
    payload_json    TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at      TEXT NOT NULL,
    UNIQUE (workflow_id, seq),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, step_id) REFERENCES workflow_steps(workflow_id, step_id),
    FOREIGN KEY (workflow_id, item_id) REFERENCES work_items(workflow_id, item_id),
    FOREIGN KEY (workflow_id, attempt_id) REFERENCES step_attempts(workflow_id, attempt_id)
);

CREATE TABLE workflow_event_streams (
    workflow_id         TEXT PRIMARY KEY,
    latest_seq          INTEGER NOT NULL DEFAULT 0 CHECK (latest_seq >= 0),
    min_available_seq   INTEGER NOT NULL DEFAULT 1 CHECK (min_available_seq >= 1),
    latest_snapshot_seq INTEGER CHECK (latest_snapshot_seq IS NULL OR latest_snapshot_seq >= 0),
    updated_at          TEXT NOT NULL,
    CHECK (min_available_seq <= latest_seq + 1),
    CHECK (latest_snapshot_seq IS NULL OR latest_snapshot_seq <= latest_seq),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT
);

CREATE TABLE workflow_snapshots (
    snapshot_id          TEXT PRIMARY KEY,
    workflow_id          TEXT NOT NULL,
    snapshot_seq         INTEGER NOT NULL CHECK (snapshot_seq >= 0),
    snapshot_event_id    TEXT,
    schema_version       TEXT NOT NULL,
    state_json            TEXT NOT NULL CHECK (json_valid(state_json)),
    size_bytes            INTEGER NOT NULL CHECK (size_bytes >= 0),
    created_at            TEXT NOT NULL,
    UNIQUE (workflow_id, snapshot_seq),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT
);

CREATE TABLE snapshot_anchors (
    workflow_id          TEXT NOT NULL,
    snapshot_event_id    TEXT NOT NULL,
    snapshot_seq         INTEGER NOT NULL CHECK (snapshot_seq >= 1),
    snapshot_id          TEXT NOT NULL,
    retained_until       TEXT,
    PRIMARY KEY (workflow_id, snapshot_event_id),
    UNIQUE (snapshot_event_id),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
    FOREIGN KEY (snapshot_id) REFERENCES workflow_snapshots(snapshot_id) ON DELETE RESTRICT
);

-- A definition is immutable once published; runs may only point at a
-- published definition with the same type/family as their group.
CREATE TRIGGER workflow_definition_immutable
BEFORE UPDATE OF workflow_type, definition_family, version, definition_hash,
                definition_json, published_at ON workflow_definitions
WHEN OLD.published_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'WORKFLOW_DEFINITION_IMMUTABLE');
END;

CREATE TRIGGER workflow_group_definition_guard_insert
BEFORE INSERT ON workflow_groups
WHEN NOT EXISTS (
    SELECT 1 FROM workflow_definitions d
    WHERE d.workflow_definition_id = NEW.workflow_definition_id
      AND d.definition_family = NEW.definition_family
      AND d.workflow_type = NEW.workflow_type
)
BEGIN
    SELECT RAISE(ABORT, 'WORKFLOW_DEFINITION_SCOPE_MISMATCH');
END;

CREATE TRIGGER workflow_group_definition_guard_update
BEFORE UPDATE OF workflow_type, definition_family, workflow_definition_id ON workflow_groups
WHEN NOT EXISTS (
    SELECT 1 FROM workflow_definitions d
    WHERE d.workflow_definition_id = NEW.workflow_definition_id
      AND d.definition_family = NEW.definition_family
      AND d.workflow_type = NEW.workflow_type
)
BEGIN
    SELECT RAISE(ABORT, 'WORKFLOW_DEFINITION_SCOPE_MISMATCH');
END;

CREATE TRIGGER workflow_definition_publication_guard
BEFORE INSERT ON workflows
WHEN NOT EXISTS (
    SELECT 1
    FROM workflow_groups g
    JOIN workflow_definitions d
      ON d.workflow_definition_id = g.workflow_definition_id
     AND d.definition_family = g.definition_family
    WHERE g.workflow_group_id = NEW.workflow_group_id
      AND g.workflow_type = NEW.workflow_type
      AND g.workflow_definition_id = NEW.workflow_definition_id
      AND d.version = NEW.workflow_definition_version
      AND d.published_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'WORKFLOW_DEFINITION_NOT_PUBLISHED');
END;

CREATE TRIGGER workflow_definition_publication_guard_update
BEFORE UPDATE OF workflow_group_id, workflow_type, workflow_definition_id,
                workflow_definition_version ON workflows
WHEN NOT EXISTS (
    SELECT 1
    FROM workflow_groups g
    JOIN workflow_definitions d
      ON d.workflow_definition_id = g.workflow_definition_id
     AND d.definition_family = g.definition_family
    WHERE g.workflow_group_id = NEW.workflow_group_id
      AND g.workflow_type = NEW.workflow_type
      AND g.workflow_definition_id = NEW.workflow_definition_id
      AND d.version = NEW.workflow_definition_version
      AND d.published_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'WORKFLOW_DEFINITION_NOT_PUBLISHED');
END;

CREATE TRIGGER workflow_group_root_guard_insert
BEFORE INSERT ON workflow_groups
WHEN NEW.root_workflow_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM workflows w
    WHERE w.workflow_id = NEW.root_workflow_id
      AND w.workflow_group_id = NEW.workflow_group_id
      AND w.parent_workflow_id IS NULL
      AND w.workflow_type = NEW.workflow_type
      AND w.workflow_definition_id = NEW.workflow_definition_id
 )
BEGIN
    SELECT RAISE(ABORT, 'ROOT_WORKFLOW_SCOPE_MISMATCH');
END;

CREATE TRIGGER workflow_group_root_guard_update
BEFORE UPDATE OF root_workflow_id, workflow_type, workflow_definition_id ON workflow_groups
WHEN NEW.root_workflow_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM workflows w
    WHERE w.workflow_id = NEW.root_workflow_id
      AND w.workflow_group_id = NEW.workflow_group_id
      AND w.parent_workflow_id IS NULL
      AND w.workflow_type = NEW.workflow_type
      AND w.workflow_definition_id = NEW.workflow_definition_id
 )
BEGIN
    SELECT RAISE(ABORT, 'ROOT_WORKFLOW_SCOPE_MISMATCH');
END;

CREATE TRIGGER workflow_group_root_guard_workflow_insert
AFTER INSERT ON workflows
WHEN EXISTS (SELECT 1 FROM workflow_groups g WHERE g.root_workflow_id = NEW.workflow_id)
 AND (NEW.parent_workflow_id IS NOT NULL
      OR NOT EXISTS (
          SELECT 1 FROM workflow_groups g
          WHERE g.workflow_group_id = NEW.workflow_group_id
            AND g.workflow_type = NEW.workflow_type
            AND g.workflow_definition_id = NEW.workflow_definition_id
      ))
BEGIN
    SELECT RAISE(ABORT, 'ROOT_WORKFLOW_SCOPE_MISMATCH');
END;

CREATE TRIGGER workflow_source_must_be_ready
BEFORE UPDATE OF source_artifact_id ON workflows
WHEN NEW.source_artifact_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM artifacts a
     WHERE a.artifact_id = NEW.source_artifact_id
       AND a.workflow_id = NEW.workflow_id
       AND a.lifecycle_state = 'READY'
 )
BEGIN
    SELECT RAISE(ABORT, 'SOURCE_ARTIFACT_NOT_READY');
END;

CREATE TRIGGER workflow_source_must_be_ready_insert
BEFORE INSERT ON workflows
WHEN NEW.source_artifact_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM artifacts a
     WHERE a.artifact_id = NEW.source_artifact_id
       AND a.workflow_id = NEW.workflow_id
       AND a.lifecycle_state = 'READY'
 )
BEGIN
    SELECT RAISE(ABORT, 'SOURCE_ARTIFACT_NOT_READY');
END;

CREATE TRIGGER source_import_generation_guard
BEFORE INSERT ON source_import_generations
WHEN NOT EXISTS (
    SELECT 1 FROM source_imports s
    WHERE s.source_import_id = NEW.source_import_id
      AND s.workflow_id = NEW.workflow_id
)
BEGIN
    SELECT RAISE(ABORT, 'SOURCE_IMPORT_SCOPE_MISMATCH');
END;

CREATE TRIGGER source_import_current_projection_guard
BEFORE UPDATE OF current_generation, current_status, current_artifact_id ON source_imports
WHEN NOT EXISTS (
    SELECT 1 FROM source_import_generations g
    WHERE g.source_import_id = NEW.source_import_id
      AND g.workflow_id = NEW.workflow_id
      AND g.generation = NEW.current_generation
      AND g.status = NEW.current_status
      AND (NEW.current_artifact_id IS NULL OR g.source_artifact_id = NEW.current_artifact_id)
)
BEGIN
    SELECT RAISE(ABORT, 'SOURCE_IMPORT_PROJECTION_MISMATCH');
END;

CREATE TRIGGER source_import_generation_artifact_guard
BEFORE UPDATE OF source_artifact_id ON source_import_generations
WHEN NEW.source_artifact_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM artifacts a
    WHERE a.artifact_id = NEW.source_artifact_id
      AND a.workflow_id = NEW.workflow_id
      AND a.source_import_generation_id = NEW.source_import_generation_id
      AND a.lifecycle_state = 'READY'
)
BEGIN
    SELECT RAISE(ABORT, 'SOURCE_ARTIFACT_BINDING_MISMATCH');
END;

CREATE TRIGGER artifacts_ready_guard_insert
BEFORE INSERT ON artifacts
WHEN NEW.lifecycle_state = 'READY'
 AND (
    NEW.blob_id IS NULL OR NEW.staging_ref IS NOT NULL OR NEW.verified <> 1
    OR NOT EXISTS (
        SELECT 1 FROM artifact_blobs b
        WHERE b.blob_id = NEW.blob_id
          AND b.lifecycle_state = 'READY'
          AND b.sha256 IS NEW.sha256
          AND b.size_bytes IS NEW.size_bytes
          AND b.format IS NEW.format
    )
 )
BEGIN
    SELECT RAISE(ABORT, 'ARTIFACT_NOT_VERIFIED');
END;

CREATE TRIGGER artifacts_ready_guard_update
BEFORE UPDATE OF lifecycle_state, blob_id, staging_ref, verified, sha256, size_bytes, format ON artifacts
WHEN NEW.lifecycle_state = 'READY'
 AND (
    NEW.blob_id IS NULL OR NEW.staging_ref IS NOT NULL OR NEW.verified <> 1
    OR NOT EXISTS (
        SELECT 1 FROM artifact_blobs b
        WHERE b.blob_id = NEW.blob_id
          AND b.lifecycle_state = 'READY'
          AND b.sha256 IS NEW.sha256
          AND b.size_bytes IS NEW.size_bytes
          AND b.format IS NEW.format
    )
 )
BEGIN
    SELECT RAISE(ABORT, 'ARTIFACT_NOT_VERIFIED');
END;

CREATE TRIGGER artifact_blob_content_immutable
BEFORE UPDATE ON artifact_blobs
WHEN OLD.sha256 IS NOT NEW.sha256
  OR OLD.size_bytes <> NEW.size_bytes
  OR OLD.format IS NOT NEW.format
  OR OLD.storage_key IS NOT NEW.storage_key
BEGIN
    SELECT RAISE(ABORT, 'ARTIFACT_BLOB_IMMUTABLE');
END;

CREATE TRIGGER artifact_source_binding_guard
BEFORE INSERT ON artifacts
WHEN NEW.source_import_generation_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM source_import_generations g
    WHERE g.source_import_generation_id = NEW.source_import_generation_id
      AND g.workflow_id = NEW.workflow_id
      AND g.source_import_id = NEW.source_import_id
      AND g.generation = NEW.source_import_generation
)
BEGIN
    SELECT RAISE(ABORT, 'SOURCE_IMPORT_ARTIFACT_SCOPE_MISMATCH');
END;

CREATE TRIGGER work_unit_item_scope_guard
BEFORE INSERT ON work_unit_items
WHEN NOT EXISTS (
    SELECT 1
    FROM work_units u
    JOIN work_item_assignments a
      ON a.workflow_id = NEW.workflow_id
     AND a.assignment_id = NEW.assignment_id
     AND a.item_id = NEW.item_id
    WHERE u.workflow_id = NEW.workflow_id
      AND u.work_unit_id = NEW.work_unit_id
      AND u.step_id = a.step_id
      AND a.state = 'ACTIVE'
)
BEGIN
    SELECT RAISE(ABORT, 'WORK_UNIT_ITEM_SCOPE_MISMATCH');
END;

CREATE TRIGGER work_unit_attempt_scope_guard
BEFORE INSERT ON work_unit_attempts
WHEN NOT EXISTS (
    SELECT 1
    FROM work_units u
    JOIN step_attempts a
      ON a.workflow_id = NEW.workflow_id
     AND a.attempt_id = NEW.attempt_id
     AND a.step_id = NEW.step_id
    WHERE u.workflow_id = NEW.workflow_id
      AND u.work_unit_id = NEW.work_unit_id
      AND u.step_id = NEW.step_id
)
BEGIN
    SELECT RAISE(ABORT, 'WORK_UNIT_ATTEMPT_SCOPE_MISMATCH');
END;

CREATE TRIGGER provider_receipt_binding_scope_guard
BEFORE INSERT ON provider_receipt_bindings
WHEN NOT EXISTS (
    SELECT 1
    FROM provider_receipts r
    JOIN work_units u
      ON u.workflow_group_id = r.workflow_group_id
     AND u.work_unit_id = NEW.work_unit_id
     AND u.workflow_id = NEW.workflow_id
    JOIN provider_submissions p
      ON p.workflow_group_id = r.workflow_group_id
     AND p.provider_submission_id = r.provider_submission_id
     AND p.provider = r.provider
     AND p.provider_account_scope = r.provider_account_scope
    WHERE r.receipt_id = NEW.receipt_id
)
BEGIN
    SELECT RAISE(ABORT, 'PROVIDER_RECEIPT_SCOPE_MISMATCH');
END;

CREATE TRIGGER snapshot_anchor_scope_guard
BEFORE INSERT ON snapshot_anchors
WHEN NOT EXISTS (
    SELECT 1
    FROM workflow_snapshots s
    JOIN workflow_events e
      ON e.workflow_id = NEW.workflow_id
     AND e.event_id = NEW.snapshot_event_id
     AND e.seq = NEW.snapshot_seq
    WHERE s.snapshot_id = NEW.snapshot_id
      AND s.workflow_id = NEW.workflow_id
      AND s.snapshot_seq = NEW.snapshot_seq
      AND (s.snapshot_event_id IS NULL OR s.snapshot_event_id = NEW.snapshot_event_id)
)
BEGIN
    SELECT RAISE(ABORT, 'SNAPSHOT_ANCHOR_MISMATCH');
END;

CREATE TRIGGER source_import_generation_scope_guard_update
BEFORE UPDATE OF source_import_id, workflow_id, generation ON source_import_generations
WHEN NOT EXISTS (
    SELECT 1 FROM source_imports s
    WHERE s.source_import_id = NEW.source_import_id
      AND s.workflow_id = NEW.workflow_id
)
BEGIN
    SELECT RAISE(ABORT, 'SOURCE_IMPORT_SCOPE_MISMATCH');
END;

CREATE TRIGGER artifact_source_binding_guard_update
BEFORE UPDATE OF workflow_id, source_import_id, source_import_generation,
                source_import_generation_id ON artifacts
WHEN NEW.source_import_generation_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM source_import_generations g
    WHERE g.source_import_generation_id = NEW.source_import_generation_id
      AND g.workflow_id = NEW.workflow_id
      AND g.source_import_id = NEW.source_import_id
      AND g.generation = NEW.source_import_generation
)
BEGIN
    SELECT RAISE(ABORT, 'SOURCE_IMPORT_ARTIFACT_SCOPE_MISMATCH');
END;

CREATE TRIGGER work_unit_item_scope_guard_update
BEFORE UPDATE OF workflow_id, work_unit_id, assignment_id, item_id ON work_unit_items
WHEN NOT EXISTS (
    SELECT 1
    FROM work_units u
    JOIN work_item_assignments a
      ON a.workflow_id = NEW.workflow_id
     AND a.assignment_id = NEW.assignment_id
     AND a.item_id = NEW.item_id
    WHERE u.workflow_id = NEW.workflow_id
      AND u.work_unit_id = NEW.work_unit_id
      AND u.step_id = a.step_id
      AND a.state = 'ACTIVE'
)
BEGIN
    SELECT RAISE(ABORT, 'WORK_UNIT_ITEM_SCOPE_MISMATCH');
END;

CREATE TRIGGER work_unit_attempt_scope_guard_update
BEFORE UPDATE OF workflow_id, step_id, work_unit_id, attempt_id ON work_unit_attempts
WHEN NOT EXISTS (
    SELECT 1
    FROM work_units u
    JOIN step_attempts a
      ON a.workflow_id = NEW.workflow_id
     AND a.attempt_id = NEW.attempt_id
     AND a.step_id = NEW.step_id
    WHERE u.workflow_id = NEW.workflow_id
      AND u.work_unit_id = NEW.work_unit_id
      AND u.step_id = NEW.step_id
)
BEGIN
    SELECT RAISE(ABORT, 'WORK_UNIT_ATTEMPT_SCOPE_MISMATCH');
END;

CREATE TRIGGER provider_receipt_binding_scope_guard_update
BEFORE UPDATE OF receipt_id, workflow_id, work_unit_id ON provider_receipt_bindings
WHEN NOT EXISTS (
    SELECT 1
    FROM provider_receipts r
    JOIN work_units u
      ON u.workflow_group_id = r.workflow_group_id
     AND u.work_unit_id = NEW.work_unit_id
     AND u.workflow_id = NEW.workflow_id
    WHERE r.receipt_id = NEW.receipt_id
)
BEGIN
    SELECT RAISE(ABORT, 'PROVIDER_RECEIPT_SCOPE_MISMATCH');
END;

CREATE TRIGGER snapshot_anchor_scope_guard_update
BEFORE UPDATE OF workflow_id, snapshot_event_id, snapshot_seq, snapshot_id ON snapshot_anchors
WHEN NOT EXISTS (
    SELECT 1
    FROM workflow_snapshots s
    JOIN workflow_events e
      ON e.workflow_id = NEW.workflow_id
     AND e.event_id = NEW.snapshot_event_id
     AND e.seq = NEW.snapshot_seq
    WHERE s.snapshot_id = NEW.snapshot_id
      AND s.workflow_id = NEW.workflow_id
      AND s.snapshot_seq = NEW.snapshot_seq
      AND (s.snapshot_event_id IS NULL OR s.snapshot_event_id = NEW.snapshot_event_id)
)
BEGIN
    SELECT RAISE(ABORT, 'SNAPSHOT_ANCHOR_MISMATCH');
END;

CREATE UNIQUE INDEX ux_workflow_scope_step_key
    ON workflow_steps(workflow_id, step_key) WHERE scope = 'workflow';
CREATE UNIQUE INDEX ux_item_scope_step_key
    ON workflow_steps(workflow_id, step_key, item_id) WHERE scope = 'item';
CREATE UNIQUE INDEX ux_active_assignment
    ON work_item_assignments(workflow_id, step_id, item_id) WHERE state = 'ACTIVE';
CREATE UNIQUE INDEX ux_execute_attempt_no
    ON step_attempts(workflow_id, step_id, execute_attempt_no)
    WHERE attempt_kind = 'EXECUTE' AND execute_attempt_no IS NOT NULL;
CREATE UNIQUE INDEX ux_active_step_attempt
    ON step_attempts(workflow_id, step_id)
    WHERE status IN ('PREPARING', 'RUNNING', 'WAITING_RETRY', 'WAITING_USER', 'RECOVERING', 'BLOCKED');
CREATE UNIQUE INDEX ux_active_generation_writer
    ON source_import_generations(source_import_id)
    WHERE status IN ('CREATED', 'RECEIVING') AND writer_lease_id IS NOT NULL;
CREATE UNIQUE INDEX ux_source_import_ready_artifact
    ON source_import_generations(source_import_id, source_artifact_id)
    WHERE status = 'READY' AND source_artifact_id IS NOT NULL;

CREATE INDEX ix_events_workflow_seq ON workflow_events(workflow_id, seq);
CREATE INDEX ix_events_workflow_event_id ON workflow_events(workflow_id, event_id);
CREATE INDEX ix_source_import_status ON source_imports(workflow_id, current_status, expires_at);
CREATE INDEX ix_source_generation_status ON source_import_generations(workflow_id, status, expires_at);
CREATE INDEX ix_artifacts_workflow_state ON artifacts(workflow_id, lifecycle_state);
CREATE INDEX ix_leases_expiry ON workflow_leases(state, lease_until);
CREATE INDEX ix_retry_budgets_next_action ON retry_budgets(next_action_at);
CREATE INDEX ix_idempotency_expiry ON workflow_idempotency_keys(expires_at);
