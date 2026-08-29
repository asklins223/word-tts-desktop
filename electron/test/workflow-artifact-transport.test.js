'use strict';

const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const test = require('node:test');
const { createWorkflowArtifactTransport } = require('../workflow-artifact-transport');

test('Artifact transport 不丢失 IPC 握手期间到达的首个数据块', async () => {
    const ipc = new EventEmitter();
    ipc.invoke = async (channel, input = {}) => {
        if (channel === 'workflow-artifact-open') {
            ipc.emit('workflow-artifact-data', {}, {
                requestId: input.requestId,
                streamId: 'stream-1',
                data: new Uint8Array([1, 2]),
            });
            ipc.emit('workflow-artifact-end', {}, {
                requestId: input.requestId,
                streamId: 'stream-1',
            });
            return 'stream-1';
        }
        if (channel === 'workflow-artifact-close') return true;
        throw new Error(`unexpected IPC channel: ${channel}`);
    };

    const stream = await createWorkflowArtifactTransport(ipc)({ artifactId: 'artifact-1' });
    const reader = stream.getReader();
    const first = await reader.read();
    assert.deepEqual(Array.from(first.value), [1, 2]);
    assert.equal((await reader.read()).done, true);
});

test('Artifact transport 取消读取时会关闭主进程流', async () => {
    const ipc = new EventEmitter();
    let opened;
    let closed;
    ipc.invoke = async (channel, input = {}) => {
        if (channel === 'workflow-artifact-open') {
            opened = input;
            return 'stream-2';
        }
        if (channel === 'workflow-artifact-close') {
            closed = input.streamId;
            return true;
        }
        throw new Error(`unexpected IPC channel: ${channel}`);
    };

    const stream = await createWorkflowArtifactTransport(ipc)({ artifactId: 'artifact-2' });
    await stream.cancel();
    assert.ok(opened.requestId);
    assert.equal(closed, 'stream-2');
});
