CREATE TABLE artifact_blobs (
    blob_id          TEXT PRIMARY KEY,
    sha256           TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
    size_bytes       INTEGER NOT NULL CHECK (size_bytes >= 0),
    format           TEXT NOT NULL,
    storage_key      TEXT NOT NULL UNIQUE
        CHECK (storage_key NOT LIKE '/%'
           AND storage_key NOT LIKE '%..%'
           AND storage_key NOT LIKE '%\\%'),
    lifecycle_state  TEXT NOT NULL CHECK (lifecycle_state IN ('READY', 'INVALID', 'DELETED')),
    verified_at      TEXT,
    created_at       TEXT NOT NULL,
    deleted_at       TEXT
);

-- A source_import is the logical session.  Per-generation mutable facts are
-- stored in source_import_generations; these columns are projections only.
CREATE TABLE source_imports (
    source_import_id       TEXT PRIMARY KEY,
    workflow_id            TEXT NOT NULL,
    request_key            TEXT NOT NULL,
    metadata_hash          TEXT NOT NULL,
    current_generation     INTEGER NOT NULL CHECK (current_generation >= 1),
    current_status         TEXT NOT NULL CHECK (
        current_status IN ('CREATED', 'RECEIVING', 'READY', 'FAILED', 'EXPIRED', 'ABORTED')
    ),
    current_artifact_id    TEXT,
    expires_at             TEXT NOT NULL,
    error_code             TEXT,
    error_details_json     TEXT CHECK (error_details_json IS NULL OR json_valid(error_details_json)),
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    completed_at           TEXT,
    UNIQUE (workflow_id, request_key),
    UNIQUE (workflow_id, source_import_id),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT
);

CREATE TABLE source_import_generations (
    source_import_generation_id TEXT PRIMARY KEY,
    source_import_id            TEXT NOT NULL,
    workflow_id                 TEXT NOT NULL,
    generation                  INTEGER NOT NULL CHECK (generation >= 1),
    staging_key                 TEXT NOT NULL UNIQUE
        CHECK (staging_key NOT LIKE '/%'
           AND staging_key NOT LIKE '%..%'
           AND staging_key NOT LIKE '%\\%'),
    expected_size_bytes         INTEGER CHECK (expected_size_bytes IS NULL OR expected_size_bytes >= 0),
    expected_sha256             TEXT CHECK (expected_sha256 IS NULL OR length(expected_sha256) = 64),
    received_size_bytes         INTEGER NOT NULL DEFAULT 0 CHECK (received_size_bytes >= 0),
    actual_size_bytes           INTEGER CHECK (actual_size_bytes IS NULL OR actual_size_bytes >= 0),
    actual_sha256               TEXT CHECK (actual_sha256 IS NULL OR length(actual_sha256) = 64),
    writer_lease_id             TEXT,
    writer_fencing_token        INTEGER CHECK (writer_fencing_token IS NULL OR writer_fencing_token >= 1),
    state_version               INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    source_artifact_id          TEXT,
    status                      TEXT NOT NULL CHECK (
        status IN ('CREATED', 'RECEIVING', 'READY', 'FAILED', 'EXPIRED', 'ABORTED')
    ),
    expires_at                  TEXT NOT NULL,
    error_code                  TEXT,
    error_details_json          TEXT CHECK (error_details_json IS NULL OR json_valid(error_details_json)),
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    completed_at                TEXT,
    UNIQUE (source_import_id, generation),
    UNIQUE (workflow_id, source_import_generation_id),
    UNIQUE (workflow_id, source_import_id, generation),
    FOREIGN KEY (source_import_id) REFERENCES source_imports(source_import_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, source_import_id)
        REFERENCES source_imports(workflow_id, source_import_id) ON DELETE RESTRICT
);

CREATE TABLE artifacts (
    artifact_id                  TEXT PRIMARY KEY,
    workflow_id                  TEXT NOT NULL,
    item_id                      TEXT,
    step_id                      TEXT,
    attempt_id                   TEXT,
    work_unit_id                 TEXT,
    work_unit_segment_id         TEXT,
    source_import_id             TEXT,
    source_import_generation     INTEGER,
    source_import_generation_id  TEXT,
    blob_id                      TEXT,
    staging_ref                  TEXT,
    artifact_type                TEXT NOT NULL,
    sha256                       TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
    size_bytes                   INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    format                      TEXT,
    producer                    TEXT NOT NULL,
    producer_version            TEXT,
    verified                    INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    verified_at                TEXT,
    lifecycle_state             TEXT NOT NULL CHECK (lifecycle_state IN ('TEMP', 'READY', 'INVALID', 'DELETED')),
    schema_version              TEXT NOT NULL,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    UNIQUE (workflow_id, artifact_id),
    UNIQUE (workflow_id, source_import_generation_id),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, item_id) REFERENCES work_items(workflow_id, item_id),
    FOREIGN KEY (workflow_id, step_id) REFERENCES workflow_steps(workflow_id, step_id),
    FOREIGN KEY (workflow_id, attempt_id) REFERENCES step_attempts(workflow_id, attempt_id),
    FOREIGN KEY (workflow_id, work_unit_id) REFERENCES work_units(workflow_id, work_unit_id),
    FOREIGN KEY (work_unit_id, work_unit_segment_id)
        REFERENCES work_unit_segments(work_unit_id, work_unit_segment_id),
    FOREIGN KEY (workflow_id, source_import_id, source_import_generation)
        REFERENCES source_import_generations(workflow_id, source_import_id, generation),
    FOREIGN KEY (workflow_id, source_import_generation_id)
        REFERENCES source_import_generations(workflow_id, source_import_generation_id),
    FOREIGN KEY (blob_id) REFERENCES artifact_blobs(blob_id) ON DELETE RESTRICT,
    CHECK ((lifecycle_state = 'TEMP' AND staging_ref IS NOT NULL)
        OR (lifecycle_state <> 'TEMP' AND staging_ref IS NULL)),
    CHECK ((source_import_generation IS NULL AND source_import_generation_id IS NULL)
        OR (source_import_generation IS NOT NULL AND source_import_generation_id IS NOT NULL))
);

CREATE TABLE artifact_derivations (
    derivation_id           TEXT PRIMARY KEY,
    parent_artifact_id      TEXT NOT NULL,
    child_artifact_id       TEXT NOT NULL,
    relation_type           TEXT NOT NULL CHECK (
        relation_type IN ('PARSE_OUTPUT', 'TTS_OUTPUT', 'CUT_SEGMENT', 'COMPOSITE', 'EXPORT', 'CACHE_REUSE')
    ),
    derivation_version      TEXT NOT NULL,
    derivation_context_hash TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    UNIQUE (parent_artifact_id, child_artifact_id, relation_type, derivation_version),
    CHECK (parent_artifact_id <> child_artifact_id),
    FOREIGN KEY (parent_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    FOREIGN KEY (child_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE RESTRICT
);

CREATE TABLE provider_receipts (
    receipt_id             TEXT PRIMARY KEY,
    workflow_group_id      TEXT NOT NULL,
    provider_submission_id TEXT NOT NULL,
    provider               TEXT NOT NULL,
    provider_account_scope TEXT NOT NULL,
    canonical_key          TEXT NOT NULL CHECK (length(canonical_key) > 0),
    query_status           TEXT NOT NULL CHECK (query_status IN ('UNKNOWN', 'PENDING', 'FOUND', 'NOT_FOUND', 'CONFLICT')),
    receipt_summary_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(receipt_summary_json)),
    state_version          INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    created_at             TEXT NOT NULL,
    confirmed_at           TEXT,
    UNIQUE (provider, provider_account_scope, canonical_key),
    UNIQUE (workflow_group_id, receipt_id),
    UNIQUE (workflow_group_id, provider_submission_id, provider, provider_account_scope),
    UNIQUE (receipt_id, provider, provider_account_scope),
    FOREIGN KEY (workflow_group_id, provider_submission_id, provider, provider_account_scope)
        REFERENCES provider_submissions(workflow_group_id, provider_submission_id, provider, provider_account_scope)
        ON DELETE RESTRICT
);

CREATE TABLE provider_receipt_identifiers (
    identifier_id          TEXT PRIMARY KEY,
    receipt_id             TEXT NOT NULL,
    provider               TEXT NOT NULL,
    provider_account_scope TEXT NOT NULL,
    identifier_type        TEXT NOT NULL,
    identifier_value       TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    UNIQUE (provider, provider_account_scope, identifier_type, identifier_value),
    FOREIGN KEY (receipt_id, provider, provider_account_scope)
        REFERENCES provider_receipts(receipt_id, provider, provider_account_scope) ON DELETE RESTRICT
);

CREATE TABLE provider_receipt_bindings (
    binding_id             TEXT PRIMARY KEY,
    binding_key             TEXT NOT NULL CHECK (length(binding_key) > 0),
    receipt_id             TEXT NOT NULL,
    workflow_id             TEXT NOT NULL,
    work_unit_id            TEXT NOT NULL,
    work_unit_attempt_id    TEXT,
    observed_by_attempt_id  TEXT,
    relation_type           TEXT NOT NULL CHECK (relation_type IN ('SUBMITTED', 'OBSERVED', 'REUSED')),
    first_observed_at       TEXT NOT NULL,
    last_observed_at        TEXT NOT NULL,
    UNIQUE (binding_key),
    UNIQUE (receipt_id, workflow_id, work_unit_id, relation_type, binding_key),
    FOREIGN KEY (receipt_id) REFERENCES provider_receipts(receipt_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, work_unit_id)
        REFERENCES work_units(workflow_id, work_unit_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, work_unit_attempt_id)
        REFERENCES work_unit_attempts(workflow_id, work_unit_attempt_id),
    FOREIGN KEY (workflow_id, observed_by_attempt_id)
        REFERENCES step_attempts(workflow_id, attempt_id)
);

CREATE TABLE reconcile_evidence (
    evidence_id             TEXT PRIMARY KEY,
    workflow_id             TEXT NOT NULL,
    source_attempt_id       TEXT,
    target_type             TEXT NOT NULL CHECK (
        target_type IN ('WORK_UNIT', 'WORK_UNIT_ATTEMPT', 'PROVIDER_RECEIPT', 'EXTERNAL_OPERATION')
    ),
    target_id               TEXT NOT NULL,
    evidence_source         TEXT NOT NULL,
    evidence_hash           TEXT NOT NULL,
    evidence_json           TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_json)),
    created_at              TEXT NOT NULL,
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, source_attempt_id)
        REFERENCES step_attempts(workflow_id, attempt_id)
);

CREATE TABLE reconcile_targets (
    reconcile_target_id    TEXT PRIMARY KEY,
    workflow_id            TEXT NOT NULL,
    reconcile_attempt_id   TEXT NOT NULL,
    target_type            TEXT NOT NULL CHECK (
        target_type IN ('WORK_UNIT', 'WORK_UNIT_ATTEMPT', 'PROVIDER_RECEIPT', 'EXTERNAL_OPERATION')
    ),
    target_id              TEXT NOT NULL,
    source_attempt_id      TEXT,
    expected_state_version INTEGER NOT NULL CHECK (expected_state_version >= 0),
    created_at             TEXT NOT NULL,
    UNIQUE (reconcile_attempt_id, target_type, target_id),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, reconcile_attempt_id)
        REFERENCES step_attempts(workflow_id, attempt_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id, source_attempt_id)
        REFERENCES step_attempts(workflow_id, attempt_id)
);
