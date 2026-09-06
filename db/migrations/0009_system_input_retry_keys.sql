-- v0009 system_input retry identity
--
-- A safe retry belongs to the original input_run and adds a new attempt for
-- the affected entry.  Keep every client idempotency key durable so a retry
-- request can be replayed after a backend restart without creating another
-- run or submitting the page twice.

DROP INDEX IF EXISTS idx_input_attempts_run_status;
ALTER TABLE input_attempts RENAME TO input_attempts_v8;

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
    evidence_json             TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_json)),
    error_code                TEXT,
    error_message             TEXT,
    started_at                TEXT,
    finished_at               TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    FOREIGN KEY (input_run_id) REFERENCES input_runs(input_run_id) ON DELETE CASCADE,
    FOREIGN KEY (entry_id) REFERENCES input_entries(entry_id) ON DELETE CASCADE,
    FOREIGN KEY (external_operation_id) REFERENCES external_operations(external_operation_id) ON DELETE SET NULL
);

INSERT INTO input_attempts(
    attempt_id, input_run_id, entry_id, external_operation_id, status,
    side_effect_state, idempotency_key, evidence_json, error_code, error_message,
    started_at, finished_at, created_at, updated_at)
SELECT
    attempt_id, input_run_id, entry_id, external_operation_id, status,
    side_effect_state, idempotency_key, evidence_json, error_code, error_message,
    started_at, finished_at, created_at, updated_at
FROM input_attempts_v8;

DROP TABLE input_attempts_v8;

CREATE INDEX idx_input_attempts_run_status
    ON input_attempts(input_run_id, status, created_at);

CREATE TABLE input_run_idempotency_keys (
    idempotency_key TEXT PRIMARY KEY CHECK (length(idempotency_key) > 0),
    input_run_id    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (input_run_id) REFERENCES input_runs(input_run_id) ON DELETE CASCADE
);

INSERT INTO input_run_idempotency_keys(idempotency_key, input_run_id, created_at)
SELECT idempotency_key, input_run_id, created_at
FROM input_runs;

CREATE INDEX idx_input_run_idempotency_keys_run
    ON input_run_idempotency_keys(input_run_id, created_at);
