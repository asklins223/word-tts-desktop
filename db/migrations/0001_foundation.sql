-- Foundation model for an immutable workflow run.
-- All timestamps are UTC ISO-8601 strings and all JSON columns are validated
-- by SQLite.  A workflow_id identifies one immutable run; a group owns the
-- cross-run business boundary.

CREATE TABLE workflow_definitions (
    workflow_definition_id TEXT PRIMARY KEY,
    workflow_type          TEXT NOT NULL CHECK (length(workflow_type) > 0),
    definition_family      TEXT NOT NULL CHECK (length(definition_family) > 0),
    version                TEXT NOT NULL CHECK (length(version) > 0),
    definition_hash        TEXT NOT NULL CHECK (length(definition_hash) > 0),
    definition_json        TEXT NOT NULL CHECK (json_valid(definition_json)),
    published_at           TEXT,
    created_at             TEXT NOT NULL,
    UNIQUE (workflow_type, definition_family, version),
    UNIQUE (workflow_definition_id, definition_family)
);

CREATE TABLE workflow_groups (
    workflow_group_id       TEXT PRIMARY KEY,
    workflow_type           TEXT NOT NULL CHECK (length(workflow_type) > 0),
    definition_family       TEXT NOT NULL CHECK (length(definition_family) > 0),
    workflow_definition_id  TEXT NOT NULL,
    business_key            TEXT,
    lifecycle_state         TEXT NOT NULL CHECK (
        lifecycle_state IN ('DRAFT', 'ACTIVE', 'ABANDONED', 'CLOSED')
    ),
    root_workflow_id        TEXT,
    state_version            INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    policy_version           TEXT NOT NULL,
    retention_policy_version TEXT NOT NULL,
    accepted_at              TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    abandoned_at             TEXT,
    closed_at                TEXT,
    UNIQUE (workflow_group_id, workflow_definition_id),
    UNIQUE (workflow_type, business_key),
    FOREIGN KEY (workflow_definition_id, definition_family)
        REFERENCES workflow_definitions(workflow_definition_id, definition_family),
    FOREIGN KEY (root_workflow_id, workflow_group_id)
        REFERENCES workflows(workflow_id, workflow_group_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE workflows (
    workflow_id                 TEXT PRIMARY KEY,
    workflow_group_id           TEXT NOT NULL,
    parent_workflow_id          TEXT,
    workflow_type               TEXT NOT NULL CHECK (length(workflow_type) > 0),
    workflow_definition_id      TEXT NOT NULL,
    schema_version              TEXT NOT NULL,
    workflow_definition_version TEXT NOT NULL,
    step_graph_hash             TEXT NOT NULL,
    workflow_business_key       TEXT,
    source_id                   TEXT,
    source_fingerprint          TEXT,
    source_artifact_id          TEXT,
    configuration_version       TEXT NOT NULL,
    configuration_hash          TEXT NOT NULL,
    configuration_snapshot      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(configuration_snapshot)),
    result_status               TEXT NOT NULL CHECK (
        result_status IN ('IN_PROGRESS', 'SUCCEEDED', 'PARTIAL_SUCCESS', 'FAILED', 'CANCELLED')
    ),
    execution_state             TEXT NOT NULL CHECK (
        execution_state IN ('CREATED', 'PREPARING', 'RUNNING', 'WAITING_RETRY',
                            'WAITING_USER', 'RECOVERING', 'BLOCKED', 'TERMINAL')
    ),
    control_state               TEXT NOT NULL CHECK (
        control_state IN ('RUNNING', 'PAUSE_REQUESTED', 'PAUSED', 'TERMINATING', 'TERMINATED')
    ),
    cleanup_state               TEXT NOT NULL CHECK (
        cleanup_state IN ('NONE', 'PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'DEFERRED')
    ),
    -- status is a display projection; the state columns above are facts.
    status                      TEXT NOT NULL CHECK (length(status) > 0),
    current_step_id             TEXT,
    state_version               INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    draft_revision              INTEGER NOT NULL DEFAULT 0 CHECK (draft_revision >= 0),
    draft_expires_at            TEXT,
    last_error_code              TEXT,
    last_error_message           TEXT,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    accepted_at                 TEXT,
    finished_at                 TEXT,
    UNIQUE (workflow_id, workflow_group_id),
    FOREIGN KEY (workflow_group_id, workflow_definition_id)
        REFERENCES workflow_groups(workflow_group_id, workflow_definition_id),
    FOREIGN KEY (parent_workflow_id, workflow_group_id)
        REFERENCES workflows(workflow_id, workflow_group_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE work_items (
    item_id             TEXT PRIMARY KEY,
    workflow_id         TEXT NOT NULL,
    item_identity_key   TEXT NOT NULL CHECK (length(item_identity_key) > 0),
    item_type           TEXT NOT NULL,
    sequence            INTEGER NOT NULL CHECK (sequence >= 0),
    identity_version    TEXT NOT NULL,
    source_locator      TEXT,
    normalized_content  TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    role                TEXT,
    voice_key           TEXT,
    metadata_json       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    status              TEXT NOT NULL CHECK (
        status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'AMBIGUOUS',
                   'CANCELLED', 'SKIPPED', 'UNRESOLVED')
    ),
    state_version       INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (workflow_id, item_id),
    UNIQUE (workflow_id, item_identity_key),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT
);

CREATE TABLE workflow_steps (
    step_id                    TEXT PRIMARY KEY,
    workflow_id                TEXT NOT NULL,
    scope                      TEXT NOT NULL CHECK (scope IN ('workflow', 'item')),
    item_id                    TEXT,
    step_key                   TEXT NOT NULL CHECK (length(step_key) > 0),
    step_type                  TEXT NOT NULL,
    step_definition_version    TEXT NOT NULL,
    dependency_keys_json       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(dependency_keys_json)),
    status                     TEXT NOT NULL CHECK (
        status IN ('PENDING', 'READY', 'PREPARING', 'RUNNING', 'VERIFYING',
                   'SUCCEEDED', 'WAITING_RETRY', 'RETRYABLE_FAILED',
                   'PERMANENT_FAILED', 'AMBIGUOUS', 'WAITING_USER',
                   'BLOCKED', 'CANCELLED')
    ),
    current_attempt_id         TEXT,
    attempt_count              INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    state_version              INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    aggregate_operation_key    TEXT,
    operation_key_type         TEXT,
    input_hash                 TEXT,
    output_reference_json      TEXT CHECK (output_reference_json IS NULL OR json_valid(output_reference_json)),
    retry_after                TEXT,
    error_code                 TEXT,
    error_details_json         TEXT CHECK (error_details_json IS NULL OR json_valid(error_details_json)),
    started_at                 TEXT,
    finished_at                TEXT,
    UNIQUE (workflow_id, step_id),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, item_id) REFERENCES work_items(workflow_id, item_id),
    CHECK ((scope = 'workflow' AND item_id IS NULL)
        OR (scope = 'item' AND item_id IS NOT NULL))
);

CREATE TABLE workflow_step_dependencies (
    dependency_id              TEXT PRIMARY KEY,
    workflow_id                TEXT NOT NULL,
    step_id                    TEXT NOT NULL,
    depends_on_step_id         TEXT NOT NULL,
    binding_rule               TEXT NOT NULL CHECK (
        binding_rule IN ('SAME_ITEM', 'ALL_ITEMS', 'ANY_ITEM', 'EXPLICIT_MAP')
    ),
    definition_version          TEXT NOT NULL,
    UNIQUE (workflow_id, step_id, depends_on_step_id, binding_rule),
    FOREIGN KEY (workflow_id, step_id)
        REFERENCES workflow_steps(workflow_id, step_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, depends_on_step_id)
        REFERENCES workflow_steps(workflow_id, step_id) ON DELETE RESTRICT,
    CHECK (step_id <> depends_on_step_id)
);

CREATE TABLE work_item_assignments (
    assignment_id             TEXT PRIMARY KEY,
    workflow_id               TEXT NOT NULL,
    step_id                   TEXT NOT NULL,
    item_id                   TEXT NOT NULL,
    delivery_unit_key         TEXT NOT NULL CHECK (length(delivery_unit_key) > 0),
    assignment_revision       INTEGER NOT NULL CHECK (assignment_revision >= 0),
    state                     TEXT NOT NULL CHECK (state IN ('ACTIVE', 'SUPERSEDED', 'REJECTED')),
    supersedes_assignment_id  TEXT,
    plan_hash                 TEXT NOT NULL,
    state_version             INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    created_at                TEXT NOT NULL,
    superseded_at             TEXT,
    UNIQUE (workflow_id, assignment_id),
    UNIQUE (workflow_id, assignment_id, item_id),
    FOREIGN KEY (workflow_id, step_id)
        REFERENCES workflow_steps(workflow_id, step_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, item_id)
        REFERENCES work_items(workflow_id, item_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, supersedes_assignment_id)
        REFERENCES work_item_assignments(workflow_id, assignment_id)
);
