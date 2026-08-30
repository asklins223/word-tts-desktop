'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createWorkflowCommandCoordinator } = require('../renderer/workflow-command-coordinator');

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

test('同一动作连续点击只发送一次，并在成功后刷新工作区', async () => {
    const request = deferred();
    const calls = [];
    const pending = [];
    const store = { markCommandPending: (key, value) => pending.push([key, value]) };
    const coordinator = createWorkflowCommandCoordinator({
        api: { sendCommand: async (...args) => { calls.push(args); return request.promise; } },
        store,
        getWorkflowId: () => 'workflow-1',
        getWorkspace: () => ({ snapshot: { state_version: 4 } }),
        refresh: async () => ({ snapshot: { state_version: 4 }, available_actions: [{ type: 'PAUSE', enabled: true }] }),
        resolveAction: type => ({ type, enabled: true }),
        timeoutMs: 1000,
    });
    const action = { type: 'PAUSE', enabled: true };
    const first = coordinator.run(action);
    const second = coordinator.run(action);
    assert.equal(first, second);
    request.resolve({ current_snapshot: { state_version: 5 } });
    const result = await first;
    assert.equal(result.ok, true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0][1], 'pause');
    assert.equal(calls[0][2].expected_state_version, 4);
    assert.match(calls[0][3].idempotencyKey, /^renderer-pause-workflow-1-/);
    assert.deepEqual(pending, [['workflow-1:PAUSE', true], ['workflow-1:PAUSE', false]]);
});

test('刷新后的完整动作列表不允许旧点击回退提交', async () => {
    const calls = [];
    const coordinator = createWorkflowCommandCoordinator({
        api: { sendCommand: async (...args) => { calls.push(args); return {}; } },
        getWorkflowId: () => 'workflow-stale-action',
        getWorkspace: () => ({ snapshot: { state_version: 2 } }),
        refresh: async () => ({
            snapshot: { state_version: 3 },
            available_actions: [{ type: 'PAUSE', enabled: false }],
        }),
        resolveAction: (type, workspace) => workspace.available_actions.find(item => item.type === type) || null,
        timeoutMs: 1000,
    });

    const result = await coordinator.run({ type: 'PAUSE', enabled: true });
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'action-disabled-after-refresh');
    assert.equal(calls.length, 0);
});

test('工作区刷新失败时不使用旧版本发送命令', async () => {
    const calls = [];
    const coordinator = createWorkflowCommandCoordinator({
        api: { sendCommand: async (...args) => { calls.push(args); return {}; } },
        getWorkflowId: () => 'workflow-refresh-failed',
        getWorkspace: () => ({ snapshot: { state_version: 2 } }),
        refresh: async () => null,
        resolveAction: () => ({ type: 'PAUSE', enabled: true }),
        timeoutMs: 1000,
    });

    const result = await coordinator.run({ type: 'PAUSE', enabled: true });
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'workspace-refresh-failed');
    assert.equal(calls.length, 0);
});

test('状态冲突只刷新一次并用同一幂等键重发', async () => {
    const calls = [];
    let refreshCount = 0;
    const coordinator = createWorkflowCommandCoordinator({
        api: {
            sendCommand: async (...args) => {
                calls.push(args);
                if (calls.length === 1) {
                    const error = new Error('stale');
                    error.code = 'STATE_CONFLICT';
                    throw error;
                }
                return { current_snapshot: { state_version: 8 } };
            },
        },
        getWorkflowId: () => 'workflow-2',
        getWorkspace: () => ({ snapshot: { state_version: 6 } }),
        refresh: async () => {
            refreshCount += 1;
            return { snapshot: { state_version: refreshCount > 1 ? 8 : 7 }, available_actions: [{ type: 'RESUME', enabled: true }] };
        },
        resolveAction: type => ({ type, enabled: true }),
        timeoutMs: 1000,
    });
    const result = await coordinator.run({ type: 'RESUME', enabled: true });
    assert.equal(result.ok, true);
    assert.equal(calls.length, 2);
    assert.equal(calls[0][3].idempotencyKey, calls[1][3].idempotencyKey);
    assert.equal(calls[1][2].expected_state_version, 8);
    assert.equal(refreshCount, 3);
});

test('定向动作会携带服务端下发的目标版本和目标标识', async () => {
    const calls = [];
    const action = {
        type: 'RECONCILE',
        enabled: true,
        target: { target_type: 'WORK_UNIT', work_unit_id: 'unit-1' },
        expected_state_version: 12,
        expected_target_state_version: 7,
        expected_attempt_id: 'attempt-1',
    };
    const coordinator = createWorkflowCommandCoordinator({
        api: { sendCommand: async (...args) => { calls.push(args); return { current_snapshot: { state_version: 13 } }; } },
        getWorkflowId: () => 'workflow-target',
        getWorkspace: () => ({ snapshot: { state_version: 12 } }),
        refresh: async () => ({ snapshot: { state_version: 12 } }),
        resolveAction: () => action,
        timeoutMs: 1000,
    });

    const result = await coordinator.run(action);
    assert.equal(result.ok, true);
    assert.deepEqual(calls[0][2], {
        expected_state_version: 12,
        expected_target_state_version: 7,
        expected_attempt_id: 'attempt-1',
        reason: 'desktop-workspace-action',
        target: { target_type: 'WORK_UNIT', work_unit_id: 'unit-1' },
    });
});

test('resolve 与 retry/reconcile 一样按 work_unit_id 隔离并携带目标字段', async () => {
    const calls = [];
    const action = {
        type: 'RESOLVE',
        enabled: true,
        target: { target_type: 'WORK_UNIT', work_unit_id: 'unit-resolve' },
        expected_state_version: 14,
        expected_target_state_version: 9,
    };
    const coordinator = createWorkflowCommandCoordinator({
        api: { sendCommand: async (...args) => { calls.push(args); return {}; } },
        getWorkflowId: () => 'workflow-resolve',
        getWorkspace: () => ({ snapshot: { state_version: 14 } }),
        refresh: async () => ({ snapshot: { state_version: 14 } }),
        resolveAction: () => action,
        timeoutMs: 1000,
    });

    const result = await coordinator.run(action);
    assert.equal(result.ok, true);
    assert.equal(calls[0][1], 'resolve');
    assert.deepEqual(calls[0][2], {
        expected_state_version: 14,
        expected_target_state_version: 9,
        reason: 'desktop-workspace-action',
        target: { target_type: 'WORK_UNIT', work_unit_id: 'unit-resolve' },
    });
});

test('同一工作流不同目标的定向动作不会互相吞并', async () => {
    const firstRequest = deferred();
    const secondRequest = deferred();
    const calls = [];
    const coordinator = createWorkflowCommandCoordinator({
        api: {
            sendCommand: async (...args) => {
                calls.push(args);
                return calls.length === 1 ? firstRequest.promise : secondRequest.promise;
            },
        },
        getWorkflowId: () => 'workflow-targets',
        getWorkspace: () => ({ snapshot: { state_version: 20 } }),
        refresh: async () => ({ snapshot: { state_version: 20 } }),
        resolveAction: (_type, workspace) => workspace?.action || null,
        timeoutMs: 1000,
    });
    const first = coordinator.run({
        type: 'RETRY',
        enabled: true,
        target: { target_type: 'ITEM', item_id: 'item-1' },
        expected_state_version: 20,
    });
    const second = coordinator.run({
        type: 'RETRY',
        enabled: true,
        target: { target_type: 'ITEM', item_id: 'item-2' },
        expected_state_version: 20,
    });

    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls.length, 2);
    assert.notEqual(calls[0][3].idempotencyKey, calls[1][3].idempotencyKey);
    assert.deepEqual(calls.map(call => call[2].target), [
        { target_type: 'ITEM', item_id: 'item-1' },
        { target_type: 'ITEM', item_id: 'item-2' },
    ]);
    firstRequest.resolve({ current_snapshot: { state_version: 21 } });
    secondRequest.resolve({ current_snapshot: { state_version: 21 } });
    assert.equal((await first).ok, true);
    assert.equal((await second).ok, true);
    assert.equal(coordinator.inFlightCount(), 0);
});

test('归档和 ZIP 动作不会把工作流目标误塞进各自的严格请求体', async () => {
    const calls = [];
    const archive = {
        type: 'ARCHIVE', enabled: true,
        target: { target_type: 'WORKFLOW', workflow_id: 'workflow-strict' },
        expected_state_version: 4,
    };
    const exportZip = {
        type: 'EXPORT_ZIP', enabled: true,
        target: { target_type: 'WORKFLOW', workflow_id: 'workflow-strict' },
        expected_state_version: 4,
    };
    // The coordinator resolves against the refresh result. Run each action
    // with its own coordinator so the test also mirrors separate UI clicks.
    const run = async (action, input = {}) => {
        const local = createWorkflowCommandCoordinator({
            api: { sendCommand: async (...args) => { calls.push(args); return {}; } },
            getWorkflowId: () => 'workflow-strict',
            getWorkspace: () => ({ snapshot: { state_version: 4 } }),
            refresh: async () => ({ snapshot: { state_version: 4 } }),
            resolveAction: () => action,
            timeoutMs: 1000,
        });
        return local.run(action, { input });
    };
    await run(archive);
    await run(exportZip, { include_item_ids: ['item-1'] });
    assert.deepEqual(calls[0][2], {
        expected_state_version: 4,
        reason: 'desktop-workspace-action',
    });
    assert.deepEqual(calls[1][2], {
        expected_state_version: 4,
        include_item_ids: ['item-1'],
    });
});

test('重新运行使用独立的任务组版本和固定幂等键', async () => {
    const calls = [];
    const action = {
        type: 'RERUN',
        enabled: true,
        expected_group_state_version: 11,
        expected_state_version: null,
    };
    const coordinator = createWorkflowCommandCoordinator({
        api: {
            sendCommand: async () => { throw new Error('rerun must not use sendCommand'); },
            rerun: async (...args) => {
                calls.push(args);
                return { workflow: { workflow_id: 'workflow-2' } };
            },
        },
        getWorkflowId: () => 'workflow-1',
        getWorkspace: () => ({ snapshot: { workflow_id: 'workflow-1', group_state_version: 11 } }),
        refresh: async () => ({
            snapshot: { workflow_id: 'workflow-1', group_state_version: 11 },
            available_actions: [action],
        }),
        resolveAction: () => action,
        timeoutMs: 1000,
    });

    const result = await coordinator.run(action, {
        input: { source_workflow_id: 'workflow-1' },
        reason: 'desktop-result-rerun',
    });

    assert.equal(result.ok, true);
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0][1], {
        source_workflow_id: 'workflow-1',
        expected_group_state_version: 11,
        reason: 'desktop-result-rerun',
    });
    assert.match(calls[0][2].idempotencyKey, /^renderer-rerun-workflow-1-/);
});

test('超时后读取一次最新工作区并明确标记结果不确定', async () => {
    const request = deferred();
    let callCount = 0;
    let refreshCount = 0;
    const coordinator = createWorkflowCommandCoordinator({
        api: { sendCommand: () => { callCount += 1; return request.promise; } },
        getWorkflowId: () => 'workflow-3',
        getWorkspace: () => ({ snapshot: { state_version: 1 } }),
        refresh: async () => {
            refreshCount += 1;
            return { snapshot: { state_version: refreshCount }, available_actions: [{ type: 'CANCEL', enabled: true }] };
        },
        resolveAction: type => ({ type, enabled: true }),
        timeoutMs: 10,
    });
    const first = coordinator.run({ type: 'CANCEL', enabled: true });
    await assert.rejects(
        first,
        error => error.code === 'COMMAND_TIMEOUT' && error.side_effect_occurred === true,
    );
    assert.equal(refreshCount, 2);
    assert.equal(coordinator.inFlightCount(), 1);
    assert.equal(coordinator.run({ type: 'CANCEL', enabled: true }), first);
    assert.equal(callCount, 1);
    request.resolve({ current_snapshot: { state_version: 3 } });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(coordinator.inFlightCount(), 0);
});
