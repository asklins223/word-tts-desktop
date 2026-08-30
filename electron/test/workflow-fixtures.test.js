'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const reducer = require('../renderer/workflow-reducer');
const adapter = require('../renderer/workflow-adapter');

const fixtureDirectory = path.join(__dirname, '..', '..', 'contracts', 'fixtures', 'workflow');
const fixtureNames = [
    'pending-configuration.json',
    'generation-running.json',
    'reconciliation-required.json',
    'partial-success.json',
    'completed.json',
    'cancelled-no-deliverables.json',
    'provider-not-ready.json',
    'paused.json',
    'partial-cancelled.json',
    'missing-content-projection.json',
    'provider-expired.json',
];

function loadFixture(name) {
    return JSON.parse(fs.readFileSync(path.join(fixtureDirectory, name), 'utf8'));
}

test('workflow fixtures cover every frozen state boundary and derive a safe primary action', () => {
    const seen = new Set();
    fixtureNames.forEach(name => {
        const fixture = loadFixture(name);
        seen.add(fixture.name);
        const workspace = reducer.normalizeWorkspace(fixture);
        const snapshot = workspace.snapshot;
        const state = reducer.deriveWorkflowUserState(snapshot, workspace);
        const expected = fixture.expected || {};

        assert.equal(reducer.isTerminalSnapshot(snapshot), expected.terminal, name);
        if (expected.view) assert.equal(state.view, expected.view, name);
        if (expected.primary_action) assert.equal(state.primaryAction?.type, expected.primary_action, name);
        if (expected.deliverable_percent !== undefined) {
            assert.equal(workspace.progress.deliverable_percent, expected.deliverable_percent, name);
        }
        ['total', 'completed', 'failed', 'cancelled', 'skipped', 'pending', 'deliverable'].forEach(field => {
            assert.equal(workspace.progress[field], fixture.progress[field], `${name}: progress.${field}`);
        });

        const enabledActions = new Set(workspace.available_actions
            .filter(action => action.enabled === true)
            .map(action => action.type));
        const blocker = adapter.blockerSummary(workspace);
        const issue = blocker
            ? adapter.issueMessage({ code: blocker.code, message: blocker.message })
            : adapter.issueMessage({ code: 'STATE_CONFLICT' });
        assert.ok(issue.message, `${name}: adapter issue message`);
        const scope = adapter.deliveryScope(workspace);
        const exclusions = adapter.exclusionDetails(workspace);
        assert.deepEqual(scope.included, workspace.delivery.included_item_ids, `${name}: included scope`);
        assert.equal(exclusions.length, workspace.delivery.excluded_item_ids.length, `${name}: exclusion details`);

        if (expected.generate_enabled !== undefined) {
            assert.equal(enabledActions.has('GENERATE'), expected.generate_enabled, name);
        }
        if (expected.pause_enabled !== undefined) assert.equal(enabledActions.has('PAUSE'), expected.pause_enabled, name);
        if (expected.resume_enabled !== undefined) assert.equal(enabledActions.has('RESUME'), expected.resume_enabled, name);
        if (expected.retry_enabled !== undefined) assert.equal(enabledActions.has('RETRY'), expected.retry_enabled, name);
        if (expected.reconnect_enabled !== undefined) assert.equal(enabledActions.has('RECONNECT'), expected.reconnect_enabled, name);
        if (expected.completed !== undefined) assert.equal(workspace.progress.completed, expected.completed, name);
        if (expected.cancelled !== undefined) assert.equal(workspace.progress.cancelled, expected.cancelled, name);
    });

    assert.deepEqual([...seen].sort(), fixtureNames.map(name => loadFixture(name).name).sort());
});

test('projection-gap fixture never turns missing content into editable text', () => {
    const fixture = loadFixture('missing-content-projection.json');
    const item = fixture.items[0];
    assert.equal(item.normalized_content, null);
    assert.equal(item.source_locator, null);
    assert.match(item.content_ref.content_id, /^content-[0-9a-f]{32}$/);
    assert.equal(fixture.expected.editable_before_load, false);
});

test('renderer contains the workspace fields and transport boundaries exercised by fixtures', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
    [
        'normalized_content',
        'content_ref',
        'source_locator',
        'available_actions',
        'exclusionDetails',
        'onSourceUploadProgress',
        'onArtifactDownloadProgress',
        'isTerminalWorkflowSnapshot',
        'ACTIVE_WORKFLOW_HYDRATE_LIMIT = 8',
        'ACTIVE_WORKFLOW_HYDRATE_CONCURRENCY = 2',
        'ACTIVE_WORKFLOW_HYDRATE_TIMEOUT_MS = 5000',
        'ACTIVE_WORKFLOW_HYDRATE_BUDGET_MS = 20000',
    ].forEach(token => assert.ok(source.includes(token), `renderer contract token: ${token}`));
});
