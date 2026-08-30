'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const reducer = require('../renderer/workflow-reducer.js');
const adapter = require('../renderer/workflow-adapter.js');

test('workspace progress keeps cancelled items separate and gates completion on verified artifacts', () => {
    const items = [
        { item_id: 'ready', status: 'SUCCEEDED' },
        { item_id: 'missing', status: 'SUCCEEDED' },
        { item_id: 'failed', status: 'FAILED' },
        { item_id: 'cancelled', status: 'CANCELLED' },
        { item_id: 'skipped', status: 'SKIPPED' },
        { item_id: 'pending', status: 'PENDING' },
    ];
    const artifacts = [{
        artifact_type: 'tts-segment',
        item_id: 'ready',
        lifecycle_state: 'READY',
        verified: true,
        filename: '001.mp3',
        format: 'mp3',
        mime_type: 'audio/mpeg',
        size_bytes: 12,
        sha256: 'a'.repeat(64),
    }];

    assert.deepEqual(reducer.normalizeProgress({}, items, artifacts), {
        total: 6,
        completed: 1,
        failed: 1,
        cancelled: 1,
        skipped: 1,
        pending: 2,
        deliverable: 1,
        percent: 66,
        deliverable_percent: 16,
    });

    const staleReadyArtifact = {
        ...artifacts[0],
        artifact_id: 'artifact-old',
        created_at: '2026-08-30T00:00:00Z',
    };
    const latestUnverifiedArtifact = {
        ...artifacts[0],
        artifact_id: 'artifact-new',
        created_at: '2026-08-30T00:00:01Z',
        verified: false,
    };
    const staleArtifactWorkspace = {
        items: [{ item_id: 'ready', status: 'SUCCEEDED' }],
        artifacts: [staleReadyArtifact, latestUnverifiedArtifact],
    };
    assert.equal(reducer.normalizeProgress({}, staleArtifactWorkspace.items, staleArtifactWorkspace.artifacts).completed, 0);
    assert.deepEqual(adapter.deliverableItemIds(staleArtifactWorkspace), []);
    assert.deepEqual(adapter.verifiedTtsArtifacts(staleArtifactWorkspace), []);

    // Store deliberately caps projected item rows, but aggregate workspace
    // counts still describe the full document and must not collapse to the
    // number of rows that happen to be present in the projection.
    assert.deepEqual(reducer.normalizeProgress({
        total: 3000,
        completed: 2000,
        failed: 100,
        cancelled: 50,
        skipped: 25,
        pending: 825,
        deliverable: 1900,
    }, [{ item_id: 'only-projected-row', status: 'PENDING' }], []), {
        total: 3000,
        completed: 2000,
        failed: 100,
        cancelled: 50,
        skipped: 25,
        pending: 825,
        deliverable: 1900,
        percent: 72,
        deliverable_percent: 63,
    });
});

test('terminal state requires execution, control and result facts together', () => {
    assert.equal(reducer.isTerminalSnapshot({
        execution_state: 'TERMINAL',
        control_state: 'TERMINATED',
        result_status: 'PARTIAL_SUCCESS',
    }), true);
    assert.equal(reducer.isTerminalSnapshot({
        execution_state: 'TERMINAL',
        control_state: 'TERMINATING',
        result_status: 'CANCELLED',
    }), false);
});

test('state projection selects only enabled server actions and exposes delivery facts', () => {
    const workspace = {
        snapshot: {
            execution_state: 'WAITING_USER',
            control_state: 'RUNNING',
            result_status: 'IN_PROGRESS',
        },
        items: [],
        artifacts: [],
        blockers: [{ severity: 'BLOCKING', message: '需要核验' }],
        available_actions: [
            { type: 'RECONCILE', enabled: true },
            { type: 'RETRY', enabled: false, reason: '存在未决副作用' },
        ],
        delivery: {
            included_item_ids: ['item-1'],
            excluded_item_ids: ['item-2'],
            exclusion_reasons: { 'item-2': 'ITEM_CANCELLED' },
            zip_available: false,
            zip_artifact_id: null,
        },
    };
    const state = reducer.deriveWorkflowUserState(workspace.snapshot, workspace);
    assert.equal(state.key, 'WAITING_USER');
    assert.equal(state.primaryAction.type, 'RECONCILE');
    assert.equal(adapter.actionEnabled(workspace, 'RECONCILE'), true);
    assert.equal(adapter.actionEnabled(workspace, 'RETRY'), false);
    assert.deepEqual(adapter.deliveryScope(workspace), {
        included: ['item-1'],
        excluded: ['item-2'],
        reasons: { 'item-2': 'ITEM_CANCELLED' },
        zipArtifactId: null,
        zipAvailable: false,
    });
});

test('terminal projection exposes reconciliation before delivery for missing artifacts', () => {
    const workspace = {
        snapshot: {
            execution_state: 'TERMINAL',
            control_state: 'TERMINATED',
            result_status: 'SUCCEEDED',
        },
        items: [{ item_id: 'item-1', status: 'SUCCEEDED' }],
        artifacts: [],
        blockers: [{
            code: 'ARTIFACT_MISSING_OR_UNVERIFIED',
            severity: 'ERROR',
            requires_reconcile: true,
        }],
        available_actions: [
            { type: 'RECONCILE', enabled: true, target: { target_type: 'WORK_UNIT', work_unit_id: 'unit-1' } },
            { type: 'EXPORT_ZIP', enabled: false },
            { type: 'OPEN_VIEW', enabled: true },
        ],
        progress: { total: 1, completed: 0, failed: 0, cancelled: 0, skipped: 0, pending: 1, deliverable: 0 },
        delivery: { included_item_ids: [], excluded_item_ids: ['item-1'], exclusion_reasons: { 'item-1': 'ARTIFACT_MISSING_OR_UNVERIFIED' } },
    };
    const state = reducer.deriveWorkflowUserState(workspace.snapshot, workspace);
    assert.equal(state.primaryAction.type, 'RECONCILE');
});

test('reducer 与 adapter 使用同一套 blocker severity 优先级', () => {
    const workspace = {
        blockers: [
            { code: 'warning', severity: 'WARNING' },
            { code: 'error', severity: 'ERROR' },
        ],
    };
    assert.equal(reducer.firstBlocker(workspace).code, 'error');
    assert.equal(adapter.blockerSummary(workspace).code, 'error');
});
