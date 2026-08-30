#!/usr/bin/env python3
"""Structural and negative checks for the workflow SQLite schema.

The check command is read-only for ``--db``.  A clean temporary database is
used when no path is supplied, so a developer's ignored runtime database can
never be modified or accidentally become the schema-check fixture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

# When executed as ``python db/schema_checks.py`` Python puts ``db/`` rather
# than the repository root on sys.path.  Add the root explicitly so the
# runner can also be imported as a normal module in tests.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from db.migration_runner import (
    Migration,
    MigrationError,
    apply_migrations,
    load_migrations,
    resolve_target,
    verify_recorded_checksums,
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

EXPECTED_TABLES_2A = {
    "workflow_definitions", "workflow_groups", "workflows", "work_items",
    "workflow_steps", "workflow_step_dependencies", "work_item_assignments",
    "provider_submissions", "provider_sessions", "step_attempts", "work_units",
    "work_unit_items", "work_unit_segments", "work_unit_attempts", "retry_budgets",
    "workflow_leases", "user_interventions", "workflow_idempotency_keys",
    "side_effect_intents", "artifact_blobs", "source_imports",
    "source_import_generations", "artifacts", "artifact_derivations",
    "provider_receipts", "provider_receipt_identifiers", "provider_receipt_bindings",
    "reconcile_evidence", "reconcile_targets", "workflow_events",
    "workflow_event_streams", "workflow_snapshots", "snapshot_anchors",
}
EXPECTED_TABLES_EXTERNAL = {
    "external_records", "external_operations", "external_record_bindings",
    "external_record_leases",
}
EXPECTED_TABLES_ATOMIC = {
    "question_sub_types", "source_documents", "document_revisions",
    "document_revision_members", "major_sections", "question_groups",
    "question_items", "question_revisions", "question_parts", "stimuli",
    "stimulus_revisions", "question_stimuli", "content_units",
    "content_unit_revisions", "operation_plans", "operation_scopes",
    "operation_scope_members", "operation_tasks", "operation_task_targets",
    "operation_task_dependencies", "revision_match_decisions", "legacy_aliases",
    "legacy_execution_sessions",
}
EXPECTED_TABLES_EXTERNAL_TARGETS = {
    "external_record_targets", "external_operation_targets",
}

EXPECTED_INDEXES = {
    "ux_workflow_scope_step_key", "ux_item_scope_step_key", "ux_active_assignment",
    "ux_execute_attempt_no", "ux_active_step_attempt", "ux_active_generation_writer",
    "ux_source_import_ready_artifact", "ix_events_workflow_seq",
    "ix_events_workflow_event_id", "ix_source_import_status",
    "ix_source_generation_status", "ix_artifacts_workflow_state", "ix_leases_expiry",
    "ix_retry_budgets_next_action", "ix_idempotency_expiry",
}
EXPECTED_INDEXES_ATOMIC = {
    "idx_document_revision_members_entity", "idx_document_revisions_source",
    "idx_major_sections_revision", "idx_question_items_source",
    "idx_question_revisions_question", "idx_question_revisions_revision_doc",
    "idx_stimuli_source", "idx_stimulus_revisions_doc",
    "idx_question_stimuli_stimulus", "idx_content_units_source",
    "idx_content_unit_revisions_doc", "idx_operation_scopes_plan",
    "idx_operation_scope_members_scope", "idx_operation_tasks_plan",
    "idx_operation_task_targets_operation", "idx_operation_task_deps_upstream",
    "idx_legacy_aliases_value", "idx_question_items_sub_type",
    "idx_stimuli_sub_type", "idx_content_units_sub_type",
}
EXPECTED_INDEXES_EXTERNAL_TARGETS = {
    "idx_external_operations_step", "idx_external_operations_attempt",
    "idx_external_record_targets_target", "ux_external_record_targets_dedupe",
    "idx_external_operation_targets_target", "ux_external_operation_targets_dedupe",
}

EXPECTED_TRIGGERS = {
    "workflow_definition_immutable", "workflow_group_definition_guard_insert",
    "workflow_group_definition_guard_update", "workflow_definition_publication_guard",
    "workflow_definition_publication_guard_update", "workflow_group_root_guard_insert",
    "workflow_group_root_guard_update", "workflow_group_root_guard_workflow_insert",
    "workflow_source_must_be_ready", "workflow_source_must_be_ready_insert",
    "source_import_generation_guard", "source_import_current_projection_guard",
    "source_import_generation_scope_guard_update",
    "source_import_generation_artifact_guard", "artifacts_ready_guard_insert",
    "artifacts_ready_guard_update", "artifact_blob_content_immutable",
    "artifact_source_binding_guard", "artifact_source_binding_guard_update",
    "work_unit_item_scope_guard", "work_unit_item_scope_guard_update",
    "work_unit_attempt_scope_guard", "work_unit_attempt_scope_guard_update",
    "provider_receipt_binding_scope_guard", "provider_receipt_binding_scope_guard_update",
    "snapshot_anchor_scope_guard", "snapshot_anchor_scope_guard_update",
}
EXPECTED_TRIGGERS_EXTERNAL = {
    "external_record_current_scope_guard", "external_operation_scope_guard",
    "external_record_binding_scope_guard",
    "external_record_current_scope_guard_update", "external_operation_scope_guard_update",
    "external_record_binding_scope_guard_update",
}
EXPECTED_TRIGGERS_EXTERNAL_TARGETS = {
    "trg_ext_record_target_question", "trg_ext_record_target_stimulus",
    "trg_ext_record_target_content_unit", "trg_ext_record_target_group",
    "trg_ext_record_target_major_section", "trg_ext_record_target_scope",
    "trg_ext_operation_target_question", "trg_ext_operation_target_stimulus",
    "trg_ext_operation_target_content_unit", "trg_ext_operation_target_group",
    "trg_ext_operation_target_major_section", "trg_ext_operation_target_scope",
}

EXPECTED_COLUMNS = {
    "source_imports": {"current_generation", "current_status", "current_artifact_id"},
    "source_import_generations": {
        "generation", "staging_key", "state_version", "writer_fencing_token",
        "source_artifact_id",
    },
    "artifacts": {"blob_id", "source_import_generation_id", "lifecycle_state"},
    "workflow_events": {"seq", "mutation_id", "correlation_id", "causation_id", "actor_type"},
    "reconcile_targets": {"target_type", "expected_state_version"},
    "provider_receipt_bindings": {"binding_key"},
    "provider_receipts": {"state_version"},
    "provider_submissions": {"state_version"},
    "external_operations": {"state_version"},
}
EXPECTED_COLUMNS_BY_TARGET = {
    7: {"external_operations": {"workflow_step_id", "attempt_id"}},
}


def _ro_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA query_only=ON")
    return con


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')}


def _check_integrity(con: sqlite3.Connection) -> None:
    fk_errors = con.execute("PRAGMA foreign_key_check").fetchall()
    if fk_errors:
        raise MigrationError(f"foreign_key_check failed: {fk_errors}")
    result = con.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise MigrationError(f"integrity_check failed: {result}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_graph(con: sqlite3.Connection, suffix: str = "1") -> dict[str, str]:
    """Create one valid graph used by cross-scope negative fixtures."""

    ids = {
        "definition": f"def-{suffix}", "group": f"group-{suffix}",
        "workflow": f"workflow-{suffix}", "item": f"item-{suffix}",
        "step": f"step-{suffix}", "assignment": f"assignment-{suffix}",
        "attempt": f"attempt-{suffix}", "submission": f"submission-{suffix}",
        "unit": f"unit-{suffix}", "unit_item": f"unit-item-{suffix}",
        "receipt": f"receipt-{suffix}", "source_import": f"import-{suffix}",
        "generation": f"generation-{suffix}",
    }
    now = _now()
    con.execute(
        "INSERT INTO workflow_definitions VALUES (?,?,?,?,?,?,?,?)",
        (ids["definition"], "tts", "default", f"1-{suffix}", "hash-def", "{}", now, now),
    )
    con.execute(
        "INSERT INTO workflow_groups VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ids["group"], "tts", "default", ids["definition"], None, "DRAFT", None,
         0, "policy-1", "retention-1", None, now, now, None, None),
    )
    con.execute(
        """INSERT INTO workflows (
            workflow_id, workflow_group_id, parent_workflow_id, workflow_type,
            workflow_definition_id, schema_version, workflow_definition_version,
            step_graph_hash, workflow_business_key, source_id, source_fingerprint,
            source_artifact_id, configuration_version, configuration_hash,
            configuration_snapshot, result_status, execution_state, control_state,
            cleanup_state, status, current_step_id, state_version, draft_revision,
            draft_expires_at, last_error_code, last_error_message, created_at,
            updated_at, accepted_at, finished_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ids["workflow"], ids["group"], None, "tts", ids["definition"], "1", f"1-{suffix}",
         "hash-graph", None, None, None, None, "config-1", "hash-config", "{}",
         "IN_PROGRESS", "CREATED", "RUNNING", "NONE", "CREATED", None, 0, 0,
         None, None, None, now, now, None, None),
    )
    con.execute(
        "UPDATE workflow_groups SET root_workflow_id=? WHERE workflow_group_id=?",
        (ids["workflow"], ids["group"]),
    )
    con.execute(
        """INSERT INTO work_items (
            item_id, workflow_id, item_identity_key, item_type, sequence,
            identity_version, source_locator, normalized_content, content_hash,
            role, voice_key, metadata_json, status, state_version, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ids["item"], ids["workflow"], f"identity-{suffix}", "sentence", 0, "1",
         "doc:0", "hello", "a" * 64, "default", "voice", "{}", "PENDING", 0, now, now),
    )
    con.execute(
        """INSERT INTO workflow_steps (
            step_id, workflow_id, scope, item_id, step_key, step_type,
            step_definition_version, dependency_keys_json, status, current_attempt_id,
            attempt_count, state_version, aggregate_operation_key, operation_key_type,
            input_hash, output_reference_json, retry_after, error_code,
            error_details_json, started_at, finished_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ids["step"], ids["workflow"], "item", ids["item"], "tts", "TTS", "1", "[]",
         "PENDING", None, 0, 0, None, None, None, None, None, None, None, None, None),
    )
    con.execute(
        """INSERT INTO work_item_assignments (
            assignment_id, workflow_id, step_id, item_id, delivery_unit_key,
            assignment_revision, state, supersedes_assignment_id, plan_hash,
            state_version, created_at, superseded_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ids["assignment"], ids["workflow"], ids["step"], ids["item"], "unit-0", 0,
         "ACTIVE", None, "plan", 0, now, None),
    )
    con.execute(
        """INSERT INTO step_attempts (
            attempt_id, workflow_id, step_id, attempt_kind, attempt_seq,
            execute_attempt_no, status, result_status, error_code, error_details_json,
            lease_fencing_token, state_version, started_at, heartbeat_at, finished_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ids["attempt"], ids["workflow"], ids["step"], "EXECUTE", 1, 1, "CREATED",
         "IN_PROGRESS", None, None, 1, 0, now, None, None),
    )
    con.execute(
        """INSERT INTO provider_submissions (
            provider_submission_id, workflow_group_id, provider, provider_account_scope,
            unit_type, tts_submission_key, ordered_plan_json, plan_hash, input_hash,
            submission_profile_hash, submission_contract_version, capability_snapshot_hash,
            capability_snapshot_json, side_effect_state, created_at, submitted_at, confirmed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ids["submission"], ids["group"], "fake", "account-a", "composite", f"tts-key-{suffix}",
         "[]", "plan", "input", "profile", "1", "cap", "{}", "NOT_STARTED", now, None, None),
    )
    con.execute(
        """INSERT INTO work_units (
            work_unit_id, workflow_id, workflow_group_id, step_id, provider_submission_id,
            created_by_attempt_id, unit_type, tts_submission_key, input_hash,
            provider_receipt_ref, side_effect_state, status, state_version, created_at, finished_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ids["unit"], ids["workflow"], ids["group"], ids["step"], ids["submission"],
         ids["attempt"], "composite", f"tts-key-{suffix}", "input", None, "NOT_STARTED", "READY", 0, now, None),
    )
    con.execute(
        """INSERT INTO work_unit_items (
            work_unit_item_id, workflow_id, work_unit_id, assignment_id, item_id,
            ordinal, result_status, result_metadata_json, state_version
        ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (ids["unit_item"], ids["workflow"], ids["unit"], ids["assignment"], ids["item"],
         0, "PENDING", "{}", 0),
    )
    con.execute(
        """INSERT INTO provider_receipts (
            receipt_id, workflow_group_id, provider_submission_id, provider,
            provider_account_scope, canonical_key, query_status, receipt_summary_json,
            created_at, confirmed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (ids["receipt"], ids["group"], ids["submission"], "fake", "account-a", f"canonical-{suffix}",
         "FOUND", "{}", now, now),
    )
    con.execute(
        """INSERT INTO source_imports (
            source_import_id, workflow_id, request_key, metadata_hash,
            current_generation, current_status, current_artifact_id, expires_at,
            error_code, error_details_json, created_at, updated_at, completed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ids["source_import"], ids["workflow"], "request", "metadata", 1, "CREATED", None,
         now, None, None, now, now, None),
    )
    con.execute(
        """INSERT INTO source_import_generations (
            source_import_generation_id, source_import_id, workflow_id, generation,
            staging_key, expected_size_bytes, expected_sha256, received_size_bytes,
            actual_size_bytes, actual_sha256, writer_lease_id, writer_fencing_token,
            state_version, source_artifact_id, status, expires_at, error_code,
            error_details_json, created_at, updated_at, completed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ids["generation"], ids["source_import"], ids["workflow"], 1, f"staging/key-{suffix}",
         5, "b" * 64, 0, None, None, "writer", 1, 0, None, "CREATED", now, None,
         None, now, now, None),
    )
    con.commit()
    return ids


def _expect_integrity_error(fn: Callable[[], None], label: str) -> None:
    try:
        fn()
    except sqlite3.IntegrityError:
        return
    raise AssertionError(f"negative fixture unexpectedly succeeded: {label}")


def run_negative_fixtures(source: sqlite3.Connection, target: int) -> None:
    """Exercise constraints that PRAGMA foreign_key_check cannot prove alone."""

    if target < 4:
        return
    with tempfile.TemporaryDirectory(prefix="wordtts-schema-fixtures-") as tmp:
        fixture_path = Path(tmp) / "fixture.db"
        fixture = sqlite3.connect(str(fixture_path), isolation_level=None)
        source.backup(fixture)
        fixture.execute("PRAGMA foreign_keys=ON")
        ids = _seed_graph(fixture, "fixture")

        _expect_integrity_error(
            lambda: fixture.execute(
                "INSERT INTO work_items (item_id, workflow_id, item_identity_key, item_type, sequence, identity_version, normalized_content, content_hash, metadata_json, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("bad-item", ids["workflow"], "bad", "sentence", 1, "1", "bad", "c" * 64, "{}", "UNKNOWN", _now(), _now()),
            ),
            "unknown item status",
        )
        _expect_integrity_error(
            lambda: fixture.execute(
                "INSERT INTO workflow_events (event_id, workflow_id, seq, mutation_id, schema_version, actor_type, event_type, payload_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("bad-event", ids["workflow"], 1, "bad-mut", "1", "ALIEN", "x", "{}", _now()),
            ),
            "unknown event actor",
        )

        ids2 = _seed_graph(fixture, "fixture-2")
        _expect_integrity_error(
            lambda: fixture.execute(
                "INSERT INTO work_unit_items (work_unit_item_id, workflow_id, work_unit_id, assignment_id, item_id, ordinal, result_status, result_metadata_json, state_version) VALUES (?,?,?,?,?,?,?,?,?)",
                ("cross-unit-item", ids["workflow"], ids["unit"], ids2["assignment"], ids["item"], 9, "PENDING", "{}", 0),
            ),
            "cross-workflow assignment",
        )
        _expect_integrity_error(
            lambda: fixture.execute(
                "INSERT INTO provider_receipts (receipt_id, workflow_group_id, provider_submission_id, provider, provider_account_scope, canonical_key, query_status, receipt_summary_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("bad-receipt", ids["group"], ids["submission"], "fake", "wrong-account", "bad", "UNKNOWN", "{}", _now()),
            ),
            "provider/account mismatch",
        )
        _expect_integrity_error(
            lambda: fixture.execute(
                "INSERT INTO artifacts (artifact_id, workflow_id, source_import_id, source_import_generation, source_import_generation_id, artifact_type, producer, lifecycle_state, schema_version, staging_ref, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("cross-artifact", ids2["workflow"], ids["source_import"], 1, ids["generation"], "source", "test", "TEMP", "1", "staging/cross", _now(), _now()),
            ),
            "cross-workflow source generation",
        )

        fixture.execute(
            "INSERT INTO provider_receipt_bindings (binding_id, binding_key, receipt_id, workflow_id, work_unit_id, relation_type, first_observed_at, last_observed_at) VALUES (?,?,?,?,?,?,?,?)",
            ("binding-1", "binding-key", ids["receipt"], ids["workflow"], ids["unit"], "OBSERVED", _now(), _now()),
        )
        _expect_integrity_error(
            lambda: fixture.execute(
                "INSERT INTO provider_receipt_bindings (binding_id, binding_key, receipt_id, workflow_id, work_unit_id, relation_type, first_observed_at, last_observed_at) VALUES (?,?,?,?,?,?,?,?)",
                ("binding-2", "binding-key", ids["receipt"], ids["workflow"], ids["unit"], "OBSERVED", _now(), _now()),
            ),
            "duplicate non-null binding key",
        )
        fixture.close()


def check_db(path: Path, *, target: int, migrations: Sequence[Migration], run_fixtures: bool = True) -> int:
    if not path.exists():
        print(f"[schema] database does not exist: {path}", file=sys.stderr)
        return 1
    con = _ro_connection(path)
    try:
        current = verify_recorded_checksums(con, migrations, target)
        if current != target:
            print(f"[schema] expected schema version {target}, found {current}", file=sys.stderr)
            return 2
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = set(EXPECTED_TABLES_2A)
        if target >= 5:
            expected |= EXPECTED_TABLES_EXTERNAL
        if target >= 6:
            expected |= EXPECTED_TABLES_ATOMIC
        if target >= 7:
            expected |= EXPECTED_TABLES_EXTERNAL_TARGETS
        missing = sorted(expected - tables)
        if missing:
            print(f"[schema] missing tables: {missing}", file=sys.stderr)
            return 3
        indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        expected_indexes = set(EXPECTED_INDEXES)
        if target >= 5:
            expected_indexes.add("ux_external_active_operation")
        if target >= 6:
            expected_indexes |= EXPECTED_INDEXES_ATOMIC
        if target >= 7:
            expected_indexes |= EXPECTED_INDEXES_EXTERNAL_TARGETS
        missing_indexes = sorted(expected_indexes - indexes)
        if missing_indexes:
            print(f"[schema] missing indexes: {missing_indexes}", file=sys.stderr)
            return 4
        triggers = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        missing_triggers = sorted(EXPECTED_TRIGGERS - triggers)
        if target >= 5:
            missing_triggers += sorted(EXPECTED_TRIGGERS_EXTERNAL - triggers)
        if target >= 7:
            missing_triggers += sorted(EXPECTED_TRIGGERS_EXTERNAL_TARGETS - triggers)
        if missing_triggers:
            print(f"[schema] missing triggers: {missing_triggers}", file=sys.stderr)
            return 5
        expected_columns = {table: set(columns) for table, columns in EXPECTED_COLUMNS.items()}
        for minimum_target, columns_by_table in EXPECTED_COLUMNS_BY_TARGET.items():
            if target >= minimum_target:
                for table, columns in columns_by_table.items():
                    expected_columns.setdefault(table, set()).update(columns)
        for table, columns in expected_columns.items():
            if table in EXPECTED_TABLES_EXTERNAL and target < 5:
                continue
            missing_columns = sorted(columns - _table_columns(con, table))
            if missing_columns:
                print(f"[schema] {table} missing columns: {missing_columns}", file=sys.stderr)
                return 6
        _check_integrity(con)
        if run_fixtures:
            run_negative_fixtures(con, target)
        print(f"[schema] version 000{target}, tables={len(expected)}, checks passed")
        return 0
    except (MigrationError, AssertionError, sqlite3.Error) as exc:
        print(f"[schema] failed: {exc}", file=sys.stderr)
        return 7
    finally:
        con.close()


def _new_temp_db(target: int, migrations: Sequence[Migration]) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    holder = tempfile.TemporaryDirectory(prefix="wordtts-schema-check-")
    path = Path(holder.name) / "check.db"
    con = sqlite3.connect(str(path), isolation_level=None)
    try:
        result = apply_migrations(con, target=target, migrations=migrations)
        if result:
            raise RuntimeError(f"migration runner returned {result}")
    finally:
        con.close()
    return holder, path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wordTTS workflow schema checks")
    parser.add_argument("--db", type=str, default=None, help="existing database to inspect read-only")
    parser.add_argument("--up-to", type=int, default=None, help="expected schema version")
    parser.add_argument("--profile", choices=("2a", "full"), default="2a")
    parser.add_argument("--no-fixtures", action="store_true", help="skip negative fixtures")
    args = parser.parse_args(argv)
    try:
        migrations = load_migrations()
        target = resolve_target(migrations, up_to=args.up_to, profile=args.profile)
        if args.db:
            return check_db(Path(args.db), target=target, migrations=migrations, run_fixtures=not args.no_fixtures)
        holder, path = _new_temp_db(target, migrations)
        try:
            return check_db(path, target=target, migrations=migrations, run_fixtures=not args.no_fixtures)
        finally:
            holder.cleanup()
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"[schema] failed: {exc}", file=sys.stderr)
        return 8


if __name__ == "__main__":
    sys.exit(main())
