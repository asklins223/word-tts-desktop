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
});
