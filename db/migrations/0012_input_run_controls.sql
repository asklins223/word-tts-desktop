-- v0012 system input run controls
--
-- A page-input run cannot be interrupted in the middle of an external write.
-- Pause and stop are therefore durable flags the worker honours at the next
-- entry boundary: pause parks the worker between units, stop finalizes the
-- run without touching entries that were never attempted. Keeping the flags
-- in their own table avoids widening the input_runs status CHECK and lets a
-- stop request survive a backend restart before the worker observes it.

CREATE TABLE input_run_controls (
    input_run_id        TEXT PRIMARY KEY,
    pause_requested     INTEGER NOT NULL DEFAULT 0 CHECK (pause_requested IN (0, 1)),
    stop_requested      INTEGER NOT NULL DEFAULT 0 CHECK (stop_requested IN (0, 1)),
    reason              TEXT,
    requested_by        TEXT NOT NULL DEFAULT 'user' CHECK (length(requested_by) > 0),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (input_run_id) REFERENCES input_runs(input_run_id) ON DELETE CASCADE
);

CREATE INDEX idx_input_run_controls_updated
    ON input_run_controls(updated_at);
