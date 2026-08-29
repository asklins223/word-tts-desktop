CREATE TABLE provider_submissions (
    provider_submission_id      TEXT PRIMARY KEY,
    workflow_group_id           TEXT NOT NULL,
    provider                    TEXT NOT NULL,
    provider_account_scope      TEXT NOT NULL,
    unit_type                   TEXT NOT NULL CHECK (unit_type IN ('single', 'composite', 'upload')),
    tts_submission_key          TEXT NOT NULL CHECK (length(tts_submission_key) > 0),
    ordered_plan_json           TEXT NOT NULL CHECK (json_valid(ordered_plan_json)),
    plan_hash                   TEXT NOT NULL,
    input_hash                  TEXT NOT NULL,
    submission_profile_hash     TEXT NOT NULL,
    submission_contract_version TEXT NOT NULL,
    capability_snapshot_hash    TEXT NOT NULL,
    capability_snapshot_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(capability_snapshot_json)),
    side_effect_state           TEXT NOT NULL CHECK (
        side_effect_state IN ('NOT_STARTED', 'INTENT_RECORDED', 'IN_FLIGHT',
                              'SUBMITTED', 'CONFIRMED', 'AMBIGUOUS', 'REJECTED')
    ),
    state_version               INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    created_at                  TEXT NOT NULL,
    submitted_at                TEXT,
    confirmed_at                TEXT,
    UNIQUE (workflow_group_id, provider_submission_id),
    UNIQUE (provider, provider_account_scope, tts_submission_key),
    UNIQUE (workflow_group_id, provider_submission_id, provider, provider_account_scope),
    FOREIGN KEY (workflow_group_id) REFERENCES workflow_groups(workflow_group_id) ON DELETE RESTRICT
);

CREATE TABLE provider_sessions (
    provider_session_id     TEXT PRIMARY KEY,
    workflow_group_id       TEXT NOT NULL,
    provider                TEXT NOT NULL,
    provider_account_scope  TEXT NOT NULL,
    profile_key             TEXT NOT NULL,
    state                   TEXT NOT NULL CHECK (state IN ('CREATED', 'READY', 'EXPIRED', 'FAILED', 'CLOSED')),
    session_reference       TEXT,
    fencing_token           INTEGER NOT NULL CHECK (fencing_token >= 1),
    last_checked_at         TEXT,
    expires_at              TEXT,
    error_code              TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE (workflow_group_id, provider, provider_account_scope, profile_key),
    FOREIGN KEY (workflow_group_id) REFERENCES workflow_groups(workflow_group_id) ON DELETE RESTRICT
);

CREATE TABLE step_attempts (
    attempt_id          TEXT PRIMARY KEY,
    workflow_id         TEXT NOT NULL,
    step_id             TEXT NOT NULL,
    attempt_kind        TEXT NOT NULL CHECK (attempt_kind IN ('EXECUTE', 'RECONCILE', 'VERIFY', 'CLEANUP')),
    attempt_seq         INTEGER NOT NULL CHECK (attempt_seq >= 1),
    execute_attempt_no  INTEGER CHECK (execute_attempt_no IS NULL OR execute_attempt_no >= 1),
    status              TEXT NOT NULL CHECK (
        status IN ('CREATED', 'PREPARING', 'RUNNING', 'VERIFYING', 'SUCCEEDED',
                   'WAITING_RETRY', 'WAITING_USER', 'RECOVERING', 'BLOCKED',
                   'FAILED', 'AMBIGUOUS', 'CANCELLED')
    ),
    result_status       TEXT NOT NULL CHECK (result_status IN ('IN_PROGRESS', 'SUCCEEDED', 'FAILED', 'MIXED', 'CANCELLED')),
    error_code          TEXT,
    error_details_json  TEXT CHECK (error_details_json IS NULL OR json_valid(error_details_json)),
    lease_fencing_token INTEGER CHECK (lease_fencing_token IS NULL OR lease_fencing_token >= 1),
    state_version       INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    started_at          TEXT NOT NULL,
    heartbeat_at        TEXT,
    finished_at         TEXT,
    UNIQUE (workflow_id, step_id, attempt_id),
    UNIQUE (workflow_id, attempt_id),
    UNIQUE (workflow_id, step_id, attempt_seq),
    FOREIGN KEY (workflow_id, step_id)
        REFERENCES workflow_steps(workflow_id, step_id) ON DELETE RESTRICT
);

CREATE TABLE work_units (
    work_unit_id          TEXT PRIMARY KEY,
    workflow_id           TEXT NOT NULL,
    workflow_group_id     TEXT NOT NULL,
    step_id               TEXT NOT NULL,
    provider_submission_id TEXT,
    created_by_attempt_id  TEXT,
    unit_type              TEXT NOT NULL CHECK (unit_type IN ('single', 'composite', 'upload')),
    tts_submission_key     TEXT,
    input_hash             TEXT NOT NULL,
    provider_receipt_ref   TEXT,
    side_effect_state      TEXT NOT NULL CHECK (
        side_effect_state IN ('NOT_STARTED', 'INTENT_RECORDED', 'IN_FLIGHT',
                              'SUBMITTED', 'CONFIRMED', 'AMBIGUOUS', 'REJECTED')
    ),
    status                 TEXT NOT NULL CHECK (
        status IN ('PENDING', 'READY', 'RUNNING', 'VERIFYING', 'SUCCEEDED',
                   'WAITING_RETRY', 'WAITING_USER', 'RECOVERING', 'BLOCKED',
                   'FAILED', 'AMBIGUOUS', 'CANCELLED')
    ),
    state_version          INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    created_at             TEXT NOT NULL,
    finished_at            TEXT,
    UNIQUE (workflow_id, work_unit_id),
    FOREIGN KEY (workflow_id, workflow_group_id)
        REFERENCES workflows(workflow_id, workflow_group_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, step_id)
        REFERENCES workflow_steps(workflow_id, step_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_group_id, provider_submission_id)
        REFERENCES provider_submissions(workflow_group_id, provider_submission_id),
    FOREIGN KEY (workflow_id, created_by_attempt_id)
        REFERENCES step_attempts(workflow_id, attempt_id)
);

CREATE TABLE work_unit_items (
    work_unit_item_id     TEXT PRIMARY KEY,
    workflow_id           TEXT NOT NULL,
    work_unit_id          TEXT NOT NULL,
    assignment_id         TEXT NOT NULL,
    item_id               TEXT NOT NULL,
    ordinal               INTEGER NOT NULL CHECK (ordinal >= 0),
    result_status         TEXT NOT NULL CHECK (result_status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'AMBIGUOUS', 'CANCELLED', 'SKIPPED')),
    result_metadata_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(result_metadata_json)),
    state_version         INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    UNIQUE (work_unit_id, item_id),
    UNIQUE (work_unit_id, ordinal),
    UNIQUE (workflow_id, work_unit_id, item_id),
    FOREIGN KEY (workflow_id, work_unit_id)
        REFERENCES work_units(workflow_id, work_unit_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, assignment_id, item_id)
        REFERENCES work_item_assignments(workflow_id, assignment_id, item_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, item_id)
        REFERENCES work_items(workflow_id, item_id) ON DELETE RESTRICT
);

CREATE TABLE work_unit_segments (
    work_unit_segment_id TEXT PRIMARY KEY,
    work_unit_id         TEXT NOT NULL,
    item_id              TEXT NOT NULL,
    segment_index        INTEGER NOT NULL CHECK (segment_index >= 0),
    segment_key          TEXT,
    ordered_position     INTEGER NOT NULL CHECK (ordered_position >= 0),
    input_hash           TEXT NOT NULL,
    result_status        TEXT NOT NULL CHECK (result_status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'AMBIGUOUS', 'CANCELLED', 'SKIPPED')),
    UNIQUE (work_unit_id, work_unit_segment_id),
    UNIQUE (work_unit_id, item_id, segment_index),
    UNIQUE (work_unit_id, item_id, ordered_position),
    FOREIGN KEY (work_unit_id, item_id)
        REFERENCES work_unit_items(work_unit_id, item_id) ON DELETE RESTRICT
);

CREATE TABLE work_unit_attempts (
    work_unit_attempt_id TEXT PRIMARY KEY,
    workflow_id          TEXT NOT NULL,
    step_id              TEXT NOT NULL,
    work_unit_id         TEXT NOT NULL,
    attempt_id           TEXT NOT NULL,
    attempt_kind         TEXT NOT NULL CHECK (attempt_kind IN ('EXECUTE', 'RECONCILE', 'VERIFY', 'CLEANUP')),
    status               TEXT NOT NULL CHECK (
        status IN ('CREATED', 'PREPARING', 'RUNNING', 'VERIFYING', 'SUCCEEDED',
                   'WAITING_RETRY', 'WAITING_USER', 'RECOVERING', 'BLOCKED',
                   'FAILED', 'AMBIGUOUS', 'CANCELLED')
    ),
    side_effect_state    TEXT NOT NULL CHECK (
        side_effect_state IN ('NOT_STARTED', 'INTENT_RECORDED', 'IN_FLIGHT',
                              'SUBMITTED', 'CONFIRMED', 'AMBIGUOUS', 'REJECTED')
    ),
    fencing_token        INTEGER CHECK (fencing_token IS NULL OR fencing_token >= 1),
    state_version        INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    started_at           TEXT NOT NULL,
    heartbeat_at         TEXT,
    finished_at          TEXT,
    UNIQUE (work_unit_id, attempt_id),
    UNIQUE (workflow_id, work_unit_attempt_id),
    FOREIGN KEY (workflow_id, work_unit_id)
        REFERENCES work_units(workflow_id, work_unit_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, step_id, attempt_id)
        REFERENCES step_attempts(workflow_id, step_id, attempt_id) ON DELETE RESTRICT
);

CREATE TABLE retry_budgets (
    retry_budget_id       TEXT PRIMARY KEY,
    workflow_group_id     TEXT NOT NULL,
    budget_kind           TEXT NOT NULL CHECK (budget_kind IN ('pure', 'tts', 'external')),
    budget_key            TEXT NOT NULL CHECK (length(budget_key) > 0),
    policy_version        TEXT NOT NULL,
    max_attempts          INTEGER CHECK (max_attempts IS NULL OR max_attempts >= 0),
    max_elapsed_ms        INTEGER CHECK (max_elapsed_ms IS NULL OR max_elapsed_ms >= 0),
    deadline_at           TEXT,
    used_attempts         INTEGER NOT NULL DEFAULT 0 CHECK (used_attempts >= 0),
    reserved_attempts     INTEGER NOT NULL DEFAULT 0 CHECK (reserved_attempts >= 0),
    next_action_at        TEXT,
    last_decision         TEXT,
    updated_at            TEXT NOT NULL,
    UNIQUE (workflow_group_id, budget_key),
    FOREIGN KEY (workflow_group_id) REFERENCES workflow_groups(workflow_group_id) ON DELETE RESTRICT
);

CREATE TABLE workflow_leases (
    lease_id              TEXT PRIMARY KEY,
    workflow_id           TEXT NOT NULL,
    resource_type         TEXT NOT NULL,
    resource_id           TEXT NOT NULL,
    owner_id              TEXT NOT NULL,
    fencing_token         INTEGER NOT NULL CHECK (fencing_token >= 1),
    lease_until            TEXT NOT NULL,
    heartbeat_at          TEXT NOT NULL,
    state                 TEXT NOT NULL CHECK (state IN ('ACTIVE', 'EXPIRED', 'RELEASED')),
    UNIQUE (resource_type, resource_id),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT
);

CREATE TABLE user_interventions (
    intervention_id       TEXT PRIMARY KEY,
    workflow_id           TEXT NOT NULL,
    step_id               TEXT,
    attempt_id            TEXT,
    work_unit_id          TEXT,
    intervention_type     TEXT NOT NULL,
    reason                TEXT NOT NULL,
    owner_id              TEXT,
    state                 TEXT NOT NULL CHECK (state IN ('OPEN', 'CLAIMED', 'RESOLVED', 'EXPIRED', 'CANCELLED')),
    evidence_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_json)),
    expires_at            TEXT,
    resolved_by           TEXT,
    resolved_at           TEXT,
    state_version         INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, step_id) REFERENCES workflow_steps(workflow_id, step_id),
    FOREIGN KEY (workflow_id, attempt_id) REFERENCES step_attempts(workflow_id, attempt_id),
    FOREIGN KEY (workflow_id, work_unit_id) REFERENCES work_units(workflow_id, work_unit_id)
);

CREATE TABLE workflow_idempotency_keys (
    idempotency_id        TEXT PRIMARY KEY,
    scope_hash            TEXT NOT NULL,
    client_key            TEXT NOT NULL,
    command_name          TEXT NOT NULL,
    method                TEXT NOT NULL,
    resource_id           TEXT,
    target_json           TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(target_json)),
    request_hash          TEXT NOT NULL,
    workflow_id           TEXT,
    response_status       INTEGER,
    response_json         TEXT CHECK (response_json IS NULL OR json_valid(response_json)),
    expires_at            TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    UNIQUE (scope_hash, client_key),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT
);

CREATE TABLE side_effect_intents (
    intent_id              TEXT PRIMARY KEY,
    workflow_id            TEXT NOT NULL,
    step_id                TEXT,
    attempt_id             TEXT,
    work_unit_id           TEXT,
    work_unit_attempt_id   TEXT,
    operation_namespace    TEXT NOT NULL,
    operation_key          TEXT NOT NULL,
    payload_hash           TEXT NOT NULL,
    provider_account_scope TEXT,
    state                  TEXT NOT NULL CHECK (state IN ('RECORDED', 'COMMITTED', 'NEEDS_RECONCILE', 'ARCHIVED')),
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    UNIQUE (operation_namespace, operation_key),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, step_id) REFERENCES workflow_steps(workflow_id, step_id),
    FOREIGN KEY (workflow_id, attempt_id) REFERENCES step_attempts(workflow_id, attempt_id),
    FOREIGN KEY (workflow_id, work_unit_id) REFERENCES work_units(workflow_id, work_unit_id),
    FOREIGN KEY (workflow_id, work_unit_attempt_id) REFERENCES work_unit_attempts(workflow_id, work_unit_attempt_id)
);
