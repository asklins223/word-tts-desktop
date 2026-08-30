'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createWorkflowStore } = require('../renderer/workflow-store');

function memoryStorage() {
    const values = new Map();
    return {
        getItem: (key) => values.get(key) || null,
        setItem: (key, value) => values.set(key, value),
        removeItem: (key) => values.delete(key),
    };
}

function event(seq, id = `event-${seq}`) {
    return { kind: 'event', event: {
        event_id: id, seq, workflow_id: 'workflow-1', mutation_id: `mutation-${seq}`,
        schema_version: '1', correlation_id: `correlation-${seq}`, causation_id: null,
        actor_type: 'SYSTEM', actor_id: null, event_type: 'TEST', phase: null,
        payload: {}, created_at: '2026-01-01T00:00:00Z', step_id: null, item_id: null, attempt_id: null,
    } };
}

test('Store 只在 reducer 成功后推进并持久化 SSE 游标，重复事件幂等', () => {
    const storage = memoryStorage();
    const store = createWorkflowStore({ storage });
    store.consume({ kind: 'snapshot', snapshot: {
        workflow_id: 'workflow-1', snapshot_seq: 1, snapshot_event_id: 'event-1',
        state: { workflow_id: 'workflow-1', latest_seq: 1, latest_event_id: 'event-1' },
    } });
    assert.equal(store.getState().lastEventId, 'event-1');
    assert.equal(store.consume(event(2)).accepted, true);
    assert.equal(store.consume(event(2)).reason, 'duplicate');
    assert.equal(store.getState().lastEventId, 'event-2');
    assert.equal(store.getState().lastSeq, 2);
    assert.equal(storage.getItem('wordtts.workflow.last-event.workflow-1'), 'event-2');
    assert.equal(storage.getItem('wordtts.workflow.last-event.workflow-1.seq'), '2');
});

test('Store 检测到 seq 缺口后停止推进游标，等待 catch-up/snapshot', () => {
    const store = createWorkflowStore();
    store.consume({ kind: 'snapshot', snapshot: {
        workflow_id: 'workflow-1', snapshot_seq: 3, snapshot_event_id: 'event-3', state: { workflow_id: 'workflow-1' },
    } });
    const result = store.consume(event(5));
    assert.deepEqual(result, { accepted: false, reason: 'gap', expectedSeq: 4, actualSeq: 5 });
    assert.equal(store.getState().lastEventId, 'event-3');
    assert.equal(store.getState().needsCatchup, true);
});

test('Store 拒绝无效的快照或事件序号，不污染恢复游标', () => {
    const store = createWorkflowStore();
    assert.deepEqual(store.consume({ kind: 'snapshot', snapshot: {
        workflow_id: 'workflow-1', snapshot_seq: 'not-a-number', state: { workflow_id: 'workflow-1' },
    } }), { accepted: false, reason: 'invalid-snapshot' });
    assert.deepEqual(store.consume({ kind: 'snapshot', snapshot: {
        workflow_id: 'workflow-1', snapshot_seq: -1, state: { workflow_id: 'workflow-1' },
    } }), { accepted: false, reason: 'invalid-snapshot' });
    assert.deepEqual(store.consume(event(0)), { accepted: false, reason: 'invalid-event' });
    assert.equal(store.getState().workflowId, null);
    assert.equal(store.getState().lastSeq, 0);
});

test('Store 连接时从持久化游标申请新的一次性事件连接并能关闭', async () => {
    const storage = memoryStorage();
    storage.setItem('wordtts.workflow.last-event.workflow-1', 'event-4');
    storage.setItem('wordtts.workflow.last-event.workflow-1.seq', '4');
    let openedWith = null;
    let closed = false;
    const api = {
        async openWorkflowEvents(workflowId, lastEventId) {
            openedWith = { workflowId, lastEventId };
            return {
                onFrame() { return () => {}; },
                onError() { return () => {}; },
                async close() { closed = true; },
            };
        },
    };
    const store = createWorkflowStore({ storage });
    await store.connect('workflow-1', api);
    assert.deepEqual(openedWith, { workflowId: 'workflow-1', lastEventId: 'event-4' });
    await store.close();
    assert.equal(closed, true);
});

test('Store prepare 为渲染器重连恢复持久化 seq，事件只在 Store 接受后推进', () => {
    const storage = memoryStorage();
    storage.setItem('wordtts.workflow.last-event.workflow-1', 'event-7');
    storage.setItem('wordtts.workflow.last-event.workflow-1.seq', '7');
    const store = createWorkflowStore({ storage });
    const prepared = store.prepare('workflow-1');
    assert.equal(prepared.lastEventId, 'event-7');
    assert.equal(prepared.lastSeq, 7);
    assert.equal(store.consume(event(9)).reason, 'gap');
    assert.equal(storage.getItem('wordtts.workflow.last-event.workflow-1'), 'event-7');
});

test('Store prepare 可用当前会话快照作为事件游标，首次连接不会把后续事件误判为缺口', () => {
    const store = createWorkflowStore();
    const prepared = store.prepare('workflow-1', {
        workflow: { workflow_id: 'workflow-1', latest_seq: 4, latest_event_id: 'event-4' },
        lastEventId: 'event-4',
        lastSeq: 4,
    });
    assert.equal(prepared.lastEventId, 'event-4');
    assert.equal(prepared.lastSeq, 4);
    assert.equal(store.consume(event(5)).accepted, true);
    assert.equal(store.getState().lastSeq, 5);
});

test('Store 可清除过期游标，下一次连接回到服务端 snapshot', () => {
    const storage = memoryStorage();
    storage.setItem('wordtts.workflow.last-event.workflow-1', 'expired-event');
    storage.setItem('wordtts.workflow.last-event.workflow-1.seq', '9');
    const store = createWorkflowStore({ storage });
    store.prepare('workflow-1');

    const reset = store.resetCursor('workflow-1');

    assert.equal(reset.lastEventId, null);
    assert.equal(reset.lastSeq, 0);
    assert.equal(storage.getItem('wordtts.workflow.last-event.workflow-1'), null);
    assert.equal(storage.getItem('wordtts.workflow.last-event.workflow-1.seq'), null);
    assert.equal(store.consume({ kind: 'snapshot', snapshot: {
        workflow_id: 'workflow-1', snapshot_seq: 12, snapshot_event_id: 'event-12', state: { workflow_id: 'workflow-1' },
    } }).accepted, true);
});

test('Store 同时提供受限的工作流快照投影，不保留配置和完整事件载荷', () => {
    const store = createWorkflowStore();
    store.consume({ kind: 'snapshot', snapshot: {
        workflow_id: 'workflow-1', snapshot_seq: 2, snapshot_event_id: 'event-2',
        state: {
            workflow_id: 'workflow-1', status: 'ACTIVE', result_status: 'IN_PROGRESS',
            execution_state: 'RUNNING', control_state: 'RUNNING', cleanup_state: 'NONE',
            state_version: 8, item_count: 3, configuration_snapshot: { secret: 'nope' },
            latest_event: {
                event_type: 'TTS_RUNTIME_PROGRESS', seq: 2,
                payload: {
                    phase: 'provider', message: '处理中', item_id: 'item-1',
                    completed_segments: 1, total_segments: 3,
                    credentials: 'must-not-project',
                },
            },
        },
    } });

    const projection = store.getState().workflowProjection;
    assert.equal(projection.workflow_id, 'workflow-1');
    assert.equal(projection.execution_state, 'RUNNING');
    assert.equal(projection.runtime.item_id, 'item-1');
    assert.equal(projection.runtime.completed_segments, 1);
    assert.equal(projection.configuration_snapshot, undefined);
    assert.equal(projection.runtime.credentials, undefined);
    const storedWorkflow = store.getState().workflow;
    assert.equal(storedWorkflow.configuration_snapshot, undefined);
    assert.equal(storedWorkflow.latest_event, undefined);
    assert.equal(JSON.stringify(storedWorkflow).includes('credentials'), false);
});

test('Store workspace 投影保留 xlsx 来源、条目修订字段与显式 Provider 状态', () => {
    const store = createWorkflowStore();
    store.setWorkspace({
        source_filename: 'U6-词汇模板.xlsx',
        snapshot: {
            workflow_id: 'workflow-1',
            state_version: 11,
            execution_state: 'CREATED',
            control_state: 'RUNNING',
            result_status: 'NONE',
        },
        progress: { total: 1, pending: 1, completed: 0, failed: 0, skipped: 0, cancelled: 0 },
        provider: {
            provider: 'xunfei',
            status: 'EXPIRED',
            ready: true,
            can_generate: true,
            reason: '登录已过期',
        },
        configuration: {
            effective: {
                rate: -0.2,
                pitch: -1.5,
                volume: 0.75,
                role_configs: { narrator: { pitch: -2.5 } },
            },
        },
        items: [{
            item_id: 'item-1',
            item_identity_key: 'sheet:Sheet1:2',
            sequence: 1,
            item_type: 'vocabulary',
            normalized_content: 'abandon',
            content_ref: null,
            source_locator: 'Sheet1!A2:B2',
            metadata: { word: 'abandon', example: 'Do not abandon the plan.' },
            skip_reason: null,
            content_hash: 'a'.repeat(64),
            status: 'PENDING',
            role: null,
            voice_key: null,
            attempt_count: 0,
            error_code: null,
            user_message: null,
            retry_scope: 'ITEM',
            requires_reconcile: false,
            artifact_ids: [],
            updated_at: '2026-08-30T00:00:00Z',
        }],
        available_actions: [],
        blockers: [],
        artifacts: [],
        delivery: { zip_available: false, included_item_ids: [], excluded_item_ids: [], exclusion_reasons: {} },
    });

    const workspace = store.getState().workspaceData;
    assert.equal(workspace.source_filename, 'U6-词汇模板.xlsx');
    assert.equal(workspace.provider.status, 'EXPIRED');
    assert.equal(workspace.provider.ready, false);
    assert.equal(workspace.provider.can_generate, false);
    assert.equal(workspace.provider.reason, '登录已过期');
    assert.equal(workspace.configuration.effective.rate, -0.2);
    assert.equal(workspace.configuration.effective.pitch, -1.5);
    assert.equal(workspace.configuration.effective.volume, 0.75);
    assert.equal(workspace.configuration.effective.role_configs.narrator.pitch, -2.5);
    assert.equal(workspace.items[0].item_type, 'vocabulary');
    assert.equal(workspace.items[0].source_locator, 'Sheet1!A2:B2');
    assert.equal(workspace.items[0].normalized_content, 'abandon');
    assert.equal(workspace.items[0].metadata.example, 'Do not abandon the plan.');
});

test('Store 的 workspace 快照缓存保持有界并保留当前工作流', () => {
    const store = createWorkflowStore();
    store.prepare('workflow-1');
    for (let index = 1; index <= 10; index += 1) {
        store.setWorkspace({
            snapshot: {
                workflow_id: `workflow-${index}`,
                state_version: index,
                execution_state: 'CREATED',
                control_state: 'RUNNING',
                result_status: 'IN_PROGRESS',
            },
            items: [],
            artifacts: [],
            blockers: [],
            available_actions: [],
        });
    }
    const cached = store.getState().workspaceByWorkflow;
    assert.equal(Object.keys(cached).length, 8);
    assert.ok(cached['workflow-1']);
    assert.equal(cached['workflow-2'], undefined);
    assert.ok(cached['workflow-10']);
});

// ============================================================================
// T8：workspace 进度投影
// ============================================================================

function typedEvent(seq, eventType, payload = {}) {
    const frame = event(seq, `event-${seq}`);
    frame.event.event_type = eventType;
    frame.event.payload = payload;
    frame.event.created_at = '2026-01-01T00:00:01Z';
    return frame;
}

function seededStore() {
    const store = createWorkflowStore();
    store.consume({ kind: 'snapshot', snapshot: {
        workflow_id: 'workflow-1', snapshot_seq: 1, snapshot_event_id: 'event-1',
        state: { workflow_id: 'workflow-1', latest_seq: 1, item_count: 12, execution_state: 'RUNNING' },
    } });
    return store;
}

test('workspace 投影按事件顺序推进阶段、分段计数与运行时消息', () => {
    const store = seededStore();
    const seen = [];
    store.subscribe((state) => seen.push(state.workspace.phase));

    assert.equal(store.consume(typedEvent(2, 'TTS_PLAN_PREPARED', { item_count: 12 })).accepted, true);
    assert.equal(store.getState().workspace.phase, 'preparing');
    assert.equal(store.getState().workspace.items.total, 12);

    store.consume(typedEvent(3, 'TTS_RUNTIME_STATUS', { status: 'starting', message: '正在启动讯飞浏览器会话' }));
    store.consume(typedEvent(4, 'TTS_RUNTIME_PROGRESS', {
        status: 'processing', message: '正在处理条目 item-3',
        completed_segments: 5, total_segments: 12, item_id: 'item-3',
    }));

    const workspace = store.getState().workspace;
    assert.equal(workspace.phase, 'running');
    assert.deepEqual(workspace.segments, { completed: 5, total: 12 });
    assert.equal(workspace.runtime.message, '正在处理条目 item-3');
    assert.equal(workspace.runtime.itemId, 'item-3');
    assert.equal(workspace.executionState, 'RUNNING');
    assert.deepEqual(seen, ['preparing', 'running', 'running']);
});

test('workspace 投影是有界的：超长消息被截断，诊断字段不进入状态', () => {
    const store = seededStore();
    store.consume(typedEvent(2, 'TTS_RUNTIME_PROGRESS', {
        message: 'x'.repeat(5000),
        hugeDiag: { nested: 'blob' },
        completed_segments: 1, total_segments: 2,
    }));
    const workspace = store.getState().workspace;
    assert.equal(workspace.runtime.message.length, 500);
    assert.equal(JSON.stringify(workspace).includes('hugeDiag'), false);
});

test('prepare 切换 run 时重置 workspace，旧进度不会泄漏成新任务的初值', () => {
    const store = seededStore();
    store.consume(typedEvent(2, 'TTS_RUNTIME_PROGRESS', { completed_segments: 9, total_segments: 12 }));
    assert.equal(store.getState().workspace.segments.completed, 9);

    store.prepare('workflow-2');
    const workspace = store.getState().workspace;
    assert.deepEqual(workspace.segments, { completed: 0, total: 0 });
    assert.equal(workspace.phase, null);
    assert.equal(workspace.items.total, null);
});

test('陈旧快照不会回退 workspace 的条目总数', () => {
    const store = createWorkflowStore();
    store.consume({ kind: 'snapshot', snapshot: {
        workflow_id: 'workflow-1', snapshot_seq: 5, snapshot_event_id: 'event-5',
        state: { workflow_id: 'workflow-1', item_count: 37, execution_state: 'RUNNING' },
    } });
    const rejected = store.consume({ kind: 'snapshot', snapshot: {
        workflow_id: 'workflow-1', snapshot_seq: 3, snapshot_event_id: 'event-3',
        state: { workflow_id: 'workflow-1', item_count: 1, execution_state: 'CREATED' },
    } });
    assert.deepEqual(rejected, { accepted: false, reason: 'stale-snapshot' });
    assert.equal(store.getState().workspace.items.total, 37);
    assert.equal(store.getState().workspace.executionState, 'RUNNING');
});

test('陈旧 workspace 响应不会让对应同步状态永久停在 loading', async () => {
    const store = createWorkflowStore({
        workspaceLoader: async () => ({
            snapshot: { workflow_id: 'workflow-stale', state_version: 4 },
            items: [],
            artifacts: [],
        }),
    });
    store.setWorkspace({
        snapshot: { workflow_id: 'workflow-stale', state_version: 5 },
        items: [],
        artifacts: [],
    });

    await store.hydrate('workflow-stale');
    const state = store.getState();
    assert.equal(state.workspaceSyncByWorkflow['workflow-stale'].state, 'ready');
    assert.equal(state.workspaceData.snapshot.state_version, 5);
});
