'use strict';

const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const test = require('node:test');
const { createWorkflowEventTransport } = require('../workflow-event-transport');

test('事件 transport 不丢失 ready 握手期间主进程刷出的首帧', async () => {
    const ipc = new EventEmitter();
    let readyResolve;
    const ready = new Promise((resolve) => { readyResolve = resolve; });
    ipc.invoke = async (channel) => {
        if (channel === 'workflow-events-open') return 'stream-1';
        if (channel === 'workflow-events-ready') {
            ipc.emit('workflow-event', {}, {
                streamId: 'stream-1',
                frame: { event: 'snapshot', data: { snapshot_seq: 3 } },
            });
            readyResolve();
            return true;
        }
        if (channel === 'workflow-events-close') return true;
        throw new Error(`unexpected IPC channel: ${channel}`);
    };
    ipc.removeListener = ipc.removeListener.bind(ipc);

    const open = createWorkflowEventTransport(ipc);
    const streamPromise = open({ workflowId: 'workflow-1', lastEventId: null });
    await ready;
    const stream = await streamPromise;
    const frames = [];
    stream.onFrame((frame) => frames.push(frame));

    assert.deepEqual(frames, [{ kind: 'snapshot', snapshot: { snapshot_seq: 3 } }]);
    await stream.close();
});

test('事件 transport 会把 listener 注册前收到的错误交付给后续 listener', async () => {
    const ipc = new EventEmitter();
    ipc.invoke = async (channel) => {
        if (channel === 'workflow-events-open') return 'stream-2';
        if (channel === 'workflow-events-ready') {
            ipc.emit('workflow-event-error', {}, {
                streamId: 'stream-2',
                error: { message: 'closed', code: 'EVENT_GAP', status: 410, closed: true },
            });
            return true;
        }
        if (channel === 'workflow-events-close') return true;
        throw new Error(`unexpected IPC channel: ${channel}`);
    };

    const stream = await createWorkflowEventTransport(ipc)({ workflowId: 'workflow-1' });
    const errors = [];
    stream.onError((error) => errors.push(error));

    assert.equal(errors.length, 1);
    assert.equal(errors[0].message, 'closed');
    assert.equal(errors[0].code, 'EVENT_GAP');
    assert.equal(errors[0].status, 410);
    assert.equal(errors[0].closed, true);
    await stream.close();
});
