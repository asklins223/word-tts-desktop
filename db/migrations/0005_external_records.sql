CREATE TABLE external_records (
    external_record_mapping_id TEXT PRIMARY KEY,
    external_system            TEXT NOT NULL CHECK (length(external_system) > 0),
    external_account_scope     TEXT NOT NULL CHECK (length(external_account_scope) > 0),
    business_record_key        TEXT NOT NULL CHECK (length(business_record_key) > 0),
    external_record_id         TEXT,
    current_workflow_group_id  TEXT,
    local_workflow_id          TEXT,
    local_item_id              TEXT,
    current_operation_key      TEXT,
    mapping_version            TEXT NOT NULL,
    external_status            TEXT NOT NULL CHECK (
        external_status IN ('UNKNOWN', 'PENDING', 'EXISTS', 'CREATED', 'UPDATED',
                            'VERIFIED', 'NOT_FOUND', 'AMBIGUOUS', 'BLOCKED')
    ),
    last_verified_at           TEXT,
    last_error                 TEXT,
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL,
    UNIQUE (external_system, external_account_scope, business_record_key),
    FOREIGN KEY (current_workflow_group_id) REFERENCES workflow_groups(workflow_group_id),
    FOREIGN KEY (local_workflow_id) REFERENCES workflows(workflow_id),
    FOREIGN KEY (local_item_id) REFERENCES work_items(item_id)
);

CREATE TABLE external_operations (
    external_operation_id      TEXT PRIMARY KEY,
    external_record_mapping_id TEXT NOT NULL,
    workflow_id                TEXT NOT NULL,
    item_id                    TEXT,
    external_operation_key     TEXT NOT NULL CHECK (length(external_operation_key) > 0),
    target_payload_hash        TEXT NOT NULL,
    mapping_version            TEXT NOT NULL,
    state_version              INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    side_effect_state          TEXT NOT NULL CHECK (
        side_effect_state IN ('NOT_STARTED', 'INTENT_RECORDED', 'IN_FLIGHT',
                              'SUBMITTED', 'CONFIRMED', 'AMBIGUOUS', 'REJECTED')
    ),
    receipt_json               TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(receipt_json)),
    created_at                 TEXT NOT NULL,
    confirmed_at               TEXT,
    UNIQUE (external_record_mapping_id, external_operation_key),
    UNIQUE (external_operation_id, workflow_id),
    FOREIGN KEY (external_record_mapping_id)
        REFERENCES external_records(external_record_mapping_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, item_id) REFERENCES work_items(workflow_id, item_id)
);

CREATE TABLE external_record_bindings (
    binding_id                 TEXT PRIMARY KEY,
    binding_key                TEXT NOT NULL CHECK (length(binding_key) > 0),
    external_record_mapping_id TEXT NOT NULL,
    workflow_id                TEXT NOT NULL,
    item_id                    TEXT,
    external_operation_id      TEXT,
    relation_type              TEXT NOT NULL CHECK (relation_type IN ('TOUCHED', 'CREATED', 'UPDATED', 'VERIFIED')),
    first_touched_at           TEXT NOT NULL,
    last_touched_at            TEXT NOT NULL,
    UNIQUE (binding_key),
    UNIQUE (external_record_mapping_id, workflow_id, relation_type, binding_key),
    FOREIGN KEY (external_record_mapping_id) REFERENCES external_records(external_record_mapping_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, item_id) REFERENCES work_items(workflow_id, item_id),
    FOREIGN KEY (external_operation_id, workflow_id)
        REFERENCES external_operations(external_operation_id, workflow_id)
);

CREATE TABLE external_record_leases (
    lease_id                   TEXT PRIMARY KEY,
    external_record_mapping_id TEXT NOT NULL,
    owner_id                   TEXT NOT NULL,
    fencing_token              INTEGER NOT NULL CHECK (fencing_token >= 1),
    lease_until                TEXT NOT NULL,
    heartbeat_at               TEXT NOT NULL,
    state                      TEXT NOT NULL CHECK (state IN ('ACTIVE', 'EXPIRED', 'RELEASED')),
    UNIQUE (external_record_mapping_id),
    FOREIGN KEY (external_record_mapping_id)
        REFERENCES external_records(external_record_mapping_id) ON DELETE RESTRICT
);

-- A record-level lease serializes ownership, while this partial index makes
-- the no-parallel-operation invariant database-enforced as well.  Resolved
-- operations remain historical facts and therefore do not block a later
-- independent update.
CREATE UNIQUE INDEX ux_external_active_operation
    ON external_operations(external_record_mapping_id)
    WHERE side_effect_state IN ('INTENT_RECORDED', 'IN_FLIGHT', 'SUBMITTED', 'AMBIGUOUS');

CREATE TRIGGER external_record_current_scope_guard
BEFORE INSERT ON external_records
WHEN (NEW.local_workflow_id IS NOT NULL OR NEW.local_item_id IS NOT NULL)
 AND NOT EXISTS (
    SELECT 1 FROM workflows w
    LEFT JOIN work_items i
      ON i.workflow_id = w.workflow_id AND i.item_id = NEW.local_item_id
    WHERE w.workflow_id = NEW.local_workflow_id
      AND (NEW.local_item_id IS NULL OR i.item_id IS NOT NULL)
      AND (NEW.current_workflow_group_id IS NULL OR w.workflow_group_id = NEW.current_workflow_group_id)
 )
BEGIN
    SELECT RAISE(ABORT, 'EXTERNAL_RECORD_SCOPE_MISMATCH');
END;

CREATE TRIGGER external_operation_scope_guard
BEFORE INSERT ON external_operations
WHEN NOT EXISTS (
    SELECT 1 FROM external_records r
    JOIN workflows w ON w.workflow_id = NEW.workflow_id
    LEFT JOIN work_items i ON i.workflow_id = NEW.workflow_id AND i.item_id = NEW.item_id
    WHERE r.external_record_mapping_id = NEW.external_record_mapping_id
      AND (NEW.item_id IS NULL OR i.item_id IS NOT NULL)
      AND (r.current_workflow_group_id IS NULL OR r.current_workflow_group_id = w.workflow_group_id)
)
BEGIN
    SELECT RAISE(ABORT, 'EXTERNAL_OPERATION_SCOPE_MISMATCH');
END;

CREATE TRIGGER external_record_binding_scope_guard
BEFORE INSERT ON external_record_bindings
WHEN NOT EXISTS (
    SELECT 1
    FROM external_records r
    JOIN workflows w ON w.workflow_id = NEW.workflow_id
    LEFT JOIN work_items i ON i.workflow_id = NEW.workflow_id AND i.item_id = NEW.item_id
    LEFT JOIN external_operations o
      ON o.external_operation_id = NEW.external_operation_id
     AND o.workflow_id = NEW.workflow_id
    WHERE r.external_record_mapping_id = NEW.external_record_mapping_id
      AND (NEW.item_id IS NULL OR i.item_id IS NOT NULL)
      AND (NEW.external_operation_id IS NULL OR o.external_operation_id IS NOT NULL)
      AND (r.current_workflow_group_id IS NULL OR r.current_workflow_group_id = w.workflow_group_id)
)
BEGIN
    SELECT RAISE(ABORT, 'EXTERNAL_BINDING_SCOPE_MISMATCH');
END;

CREATE TRIGGER external_record_current_scope_guard_update
BEFORE UPDATE OF current_workflow_group_id, local_workflow_id, local_item_id ON external_records
WHEN (NEW.local_workflow_id IS NOT NULL OR NEW.local_item_id IS NOT NULL)
 AND NOT EXISTS (
    SELECT 1 FROM workflows w
    LEFT JOIN work_items i
      ON i.workflow_id = w.workflow_id AND i.item_id = NEW.local_item_id
    WHERE w.workflow_id = NEW.local_workflow_id
      AND (NEW.local_item_id IS NULL OR i.item_id IS NOT NULL)
      AND (NEW.current_workflow_group_id IS NULL OR w.workflow_group_id = NEW.current_workflow_group_id)
 )
BEGIN
    SELECT RAISE(ABORT, 'EXTERNAL_RECORD_SCOPE_MISMATCH');
END;

CREATE TRIGGER external_operation_scope_guard_update
BEFORE UPDATE OF external_record_mapping_id, workflow_id, item_id ON external_operations
WHEN NOT EXISTS (
    SELECT 1 FROM external_records r
    JOIN workflows w ON w.workflow_id = NEW.workflow_id
    LEFT JOIN work_items i ON i.workflow_id = NEW.workflow_id AND i.item_id = NEW.item_id
    WHERE r.external_record_mapping_id = NEW.external_record_mapping_id
      AND (NEW.item_id IS NULL OR i.item_id IS NOT NULL)
      AND (r.current_workflow_group_id IS NULL OR r.current_workflow_group_id = w.workflow_group_id)
)
BEGIN
    SELECT RAISE(ABORT, 'EXTERNAL_OPERATION_SCOPE_MISMATCH');
END;

CREATE TRIGGER external_record_binding_scope_guard_update
BEFORE UPDATE OF external_record_mapping_id, workflow_id, item_id, external_operation_id ON external_record_bindings
WHEN NOT EXISTS (
    SELECT 1
    FROM external_records r
    JOIN workflows w ON w.workflow_id = NEW.workflow_id
    LEFT JOIN work_items i ON i.workflow_id = NEW.workflow_id AND i.item_id = NEW.item_id
    LEFT JOIN external_operations o
      ON o.external_operation_id = NEW.external_operation_id
     AND o.workflow_id = NEW.workflow_id
    WHERE r.external_record_mapping_id = NEW.external_record_mapping_id
      AND (NEW.item_id IS NULL OR i.item_id IS NOT NULL)
      AND (NEW.external_operation_id IS NULL OR o.external_operation_id IS NOT NULL)
      AND (r.current_workflow_group_id IS NULL OR r.current_workflow_group_id = w.workflow_group_id)
)
BEGIN
    SELECT RAISE(ABORT, 'EXTERNAL_BINDING_SCOPE_MISMATCH');
END;
