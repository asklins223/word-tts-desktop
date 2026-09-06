-- v0008 system_input
--
-- The system-input graph is deliberately separate from the existing TTS
-- execution graph.  It records the stable unit/entry/run identity, the
-- source-to-audio structure projection, and the two audio acceptance gates.
-- No external write is performed by this migration; the page executor is
-- responsible for that boundary after an input_run has been durably prepared.

CREATE TABLE audio_batches (
    audio_batch_id       TEXT PRIMARY KEY,
    workflow_id          TEXT NOT NULL UNIQUE,
    source_artifact_id   TEXT,
    audio_revision       INTEGER NOT NULL DEFAULT 0 CHECK (audio_revision >= 0),
    manifest_hash        TEXT,
    status               TEXT NOT NULL DEFAULT 'DRAFT' CHECK (
        status IN ('DRAFT', 'GENERATING', 'READY', 'ACCEPTED', 'INVALIDATED')
    ),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
);

CREATE TABLE input_units (
    unit_id                    TEXT PRIMARY KEY,
    workflow_id                TEXT NOT NULL,
    audio_batch_id             TEXT NOT NULL,
    ordinal                    INTEGER NOT NULL CHECK (ordinal >= 0),
    label                      TEXT NOT NULL CHECK (length(label) > 0),
    input_type                 TEXT NOT NULL CHECK (input_type IN ('paper', 'textbook', 'vocabulary')),
    unit_count_status          TEXT NOT NULL CHECK (
        unit_count_status IN ('single_default', 'multiple_confirmed', 'multiple_candidate')
    ),
    unit_count_override        TEXT CHECK (unit_count_override IS NULL OR unit_count_override IN ('single', 'multiple')),
    input_type_status          TEXT NOT NULL DEFAULT 'suggested' CHECK (
        input_type_status IN ('suggested', 'confirmed', 'conflict', 'user_override')
    ),
    paper_category             TEXT CHECK (paper_category IS NULL OR paper_category IN ('题型专项', '听说考试')),
    paper_category_status      TEXT NOT NULL DEFAULT 'not_applicable' CHECK (
        paper_category_status IN ('suggested', 'confirmed', 'conflict', 'user_override', 'not_applicable')
    ),
    parse_coverage_status      TEXT NOT NULL DEFAULT 'complete' CHECK (
        parse_coverage_status IN ('complete', 'partial', 'unclassified')
    ),
    source_range_json          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(source_range_json)),
    evidence_json              TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_json)),
    configuration_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(configuration_json)),
    structure_revision         INTEGER NOT NULL DEFAULT 1 CHECK (structure_revision >= 1),
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL,
    UNIQUE (workflow_id, ordinal),
    UNIQUE (workflow_id, unit_id),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE,
    FOREIGN KEY (audio_batch_id) REFERENCES audio_batches(audio_batch_id) ON DELETE CASCADE
);

CREATE INDEX idx_input_units_workflow_order
    ON input_units(workflow_id, ordinal);

CREATE TABLE structure_nodes (
    node_id          TEXT PRIMARY KEY,
    workflow_id      TEXT NOT NULL,
    unit_id          TEXT NOT NULL,
    parent_node_id   TEXT,
    ordinal          INTEGER NOT NULL CHECK (ordinal >= 0),
    node_kind        TEXT NOT NULL CHECK (length(node_kind) > 0),
    label            TEXT NOT NULL CHECK (length(label) > 0),
    path_json        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(path_json)),
    source_locator   TEXT,
    confidence       REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    evidence_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_json)),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (workflow_id, unit_id, parent_node_id, ordinal, node_kind, label),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE,
    FOREIGN KEY (unit_id) REFERENCES input_units(unit_id) ON DELETE CASCADE,
    FOREIGN KEY (parent_node_id) REFERENCES structure_nodes(node_id) ON DELETE CASCADE
);

CREATE INDEX idx_structure_nodes_unit_order
    ON structure_nodes(workflow_id, unit_id, parent_node_id, ordinal);

CREATE TABLE content_segments (
    segment_id          TEXT PRIMARY KEY,
    workflow_id         TEXT NOT NULL,
    unit_id             TEXT NOT NULL,
    node_id             TEXT,
    item_id             TEXT NOT NULL,
    content_item_id     TEXT NOT NULL,
    ordinal             INTEGER NOT NULL CHECK (ordinal >= 0),
    raw_text            TEXT NOT NULL DEFAULT '',
    tts_text            TEXT NOT NULL DEFAULT '',
    source_locator      TEXT,
    score               REAL,
    answer_json         TEXT CHECK (answer_json IS NULL OR json_valid(answer_json)),
    audio_artifact_id   TEXT,
    audio_revision      INTEGER NOT NULL DEFAULT 0 CHECK (audio_revision >= 0),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (workflow_id, item_id),
    UNIQUE (workflow_id, unit_id, ordinal, segment_id),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE,
    FOREIGN KEY (unit_id) REFERENCES input_units(unit_id) ON DELETE CASCADE,
    FOREIGN KEY (node_id) REFERENCES structure_nodes(node_id) ON DELETE CASCADE,
    FOREIGN KEY (workflow_id, item_id) REFERENCES work_items(workflow_id, item_id) ON DELETE CASCADE,
    FOREIGN KEY (audio_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE SET NULL
);

CREATE INDEX idx_content_segments_unit_order
    ON content_segments(workflow_id, unit_id, ordinal);

CREATE TABLE input_entries (
    entry_id                       TEXT PRIMARY KEY,
    workflow_id                    TEXT NOT NULL,
    unit_id                        TEXT NOT NULL,
    audio_batch_id                 TEXT NOT NULL,
    input_type                     TEXT NOT NULL CHECK (input_type IN ('paper', 'textbook', 'vocabulary')),
    document_name                 TEXT,
    unit_label                    TEXT NOT NULL CHECK (length(unit_label) > 0),
    configuration_json             TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(configuration_json)),
    configuration_revision         INTEGER NOT NULL DEFAULT 1 CHECK (configuration_revision >= 1),
    structure_revision             INTEGER NOT NULL DEFAULT 1 CHECK (structure_revision >= 1),
    external_record_mapping_id     TEXT,
    external_record_id             TEXT,
    input_status                   TEXT NOT NULL DEFAULT 'pending_config' CHECK (
        input_status IN ('not_enabled', 'pending_config', 'pending_execute', 'running',
                         'succeeded', 'partial_failed', 'failed_retryable', 'failed', 'needs_reconcile')
    ),
    external_status                TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (
        external_status IN ('UNKNOWN', 'PENDING', 'SUCCEEDED', 'FAILED', 'AMBIGUOUS')
    ),
    requires_reconcile             INTEGER NOT NULL DEFAULT 0 CHECK (requires_reconcile IN (0, 1)),
    review_url                     TEXT,
    review_url_status              TEXT NOT NULL DEFAULT 'unknown' CHECK (
        review_url_status IN ('unknown', 'available', 'unavailable', 'stale')
    ),
    review_url_source              TEXT,
    run_review_url                 TEXT,
    created_at                     TEXT NOT NULL,
    updated_at                     TEXT NOT NULL,
    UNIQUE (workflow_id, unit_id),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE,
    FOREIGN KEY (unit_id) REFERENCES input_units(unit_id) ON DELETE CASCADE,
    FOREIGN KEY (audio_batch_id) REFERENCES audio_batches(audio_batch_id) ON DELETE CASCADE
);

CREATE INDEX idx_input_entries_workflow_status
    ON input_entries(workflow_id, input_status, updated_at);

CREATE TABLE input_runs (
    input_run_id             TEXT PRIMARY KEY,
    workflow_id              TEXT NOT NULL,
    input_type               TEXT NOT NULL CHECK (input_type IN ('paper', 'textbook', 'vocabulary')),
    status                   TEXT NOT NULL CHECK (
        status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'PARTIAL_SUCCESS', 'FAILED', 'AMBIGUOUS')
    ),
    audio_revision           INTEGER NOT NULL CHECK (audio_revision >= 0),
    artifact_manifest_hash   TEXT NOT NULL CHECK (length(artifact_manifest_hash) > 0),
    configuration_revision   INTEGER NOT NULL CHECK (configuration_revision >= 1),
    target_snapshot_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(target_snapshot_json)),
    payload_hash             TEXT NOT NULL CHECK (length(payload_hash) = 64),
    idempotency_key          TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) > 0),
    error_code               TEXT,
    error_message            TEXT,
    started_at               TEXT,
    finished_at              TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
);

CREATE INDEX idx_input_runs_workflow_created
    ON input_runs(workflow_id, created_at DESC);

CREATE TABLE input_attempts (
    attempt_id               TEXT PRIMARY KEY,
    input_run_id             TEXT NOT NULL,
    entry_id                 TEXT NOT NULL,
    external_operation_id    TEXT,
    status                   TEXT NOT NULL CHECK (
        status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'AMBIGUOUS', 'NEEDS_RECONCILE')
    ),
    side_effect_state        TEXT NOT NULL DEFAULT 'NOT_STARTED' CHECK (
        side_effect_state IN ('NOT_STARTED', 'INTENT_RECORDED', 'IN_FLIGHT', 'SUBMITTED',
                              'CONFIRMED', 'AMBIGUOUS', 'REJECTED')
    ),
    idempotency_key          TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) > 0),
    evidence_json            TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_json)),
    error_code               TEXT,
    error_message            TEXT,
    started_at               TEXT,
    finished_at              TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    UNIQUE (input_run_id, entry_id),
    FOREIGN KEY (input_run_id) REFERENCES input_runs(input_run_id) ON DELETE CASCADE,
    FOREIGN KEY (entry_id) REFERENCES input_entries(entry_id) ON DELETE CASCADE,
    FOREIGN KEY (external_operation_id) REFERENCES external_operations(external_operation_id) ON DELETE SET NULL
);

CREATE INDEX idx_input_attempts_run_status
    ON input_attempts(input_run_id, status, created_at);

CREATE TABLE audio_acceptances (
    acceptance_id             TEXT PRIMARY KEY,
    workflow_id               TEXT NOT NULL,
    audio_batch_id            TEXT NOT NULL,
    audio_revision            INTEGER NOT NULL CHECK (audio_revision >= 0),
    artifact_manifest_hash    TEXT NOT NULL CHECK (length(artifact_manifest_hash) > 0),
    required_artifact_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(required_artifact_ids_json)),
    technical_status           TEXT NOT NULL CHECK (technical_status IN ('pending', 'passed', 'failed')),
    user_status                TEXT NOT NULL CHECK (user_status IN ('pending', 'accepted', 'invalidated')),
    accepted_at                TEXT,
    invalidated_at             TEXT,
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL,
    UNIQUE (workflow_id, audio_revision, artifact_manifest_hash),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE,
    FOREIGN KEY (audio_batch_id) REFERENCES audio_batches(audio_batch_id) ON DELETE CASCADE
);

CREATE INDEX idx_audio_acceptances_workflow_latest
    ON audio_acceptances(workflow_id, audio_revision DESC, updated_at DESC);

CREATE TABLE input_config_templates (
    app_template_id          TEXT PRIMARY KEY,
    input_type               TEXT NOT NULL CHECK (input_type IN ('paper', 'textbook', 'vocabulary')),
    name                     TEXT NOT NULL CHECK (length(name) > 0),
    schema_version           TEXT NOT NULL CHECK (length(schema_version) > 0),
    configuration_json       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(configuration_json)),
    platform_template_id     TEXT,
    platform_template_name   TEXT,
    platform_template_version TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    archived_at              TEXT
);

CREATE INDEX idx_input_config_templates_type_active
    ON input_config_templates(input_type, archived_at, updated_at DESC);
