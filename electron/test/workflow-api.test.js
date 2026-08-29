'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createWorkflowApi } = require('../workflow-api');

function createTransport() {
    const calls = [];
    const request = async (input) => {
        calls.push(input);
        if (input.pathname.includes('/event-tickets')) return { status: 201, body: { ticket: 'event-ticket' } };
        if (input.pathname.includes('/writer-tickets')) return { status: 201, body: { grant: 'writer-grant' } };
        if (input.pathname.includes('/content-tickets')) return { status: 201, body: { ticket: 'artifact-ticket' } };
        if (input.pathname.includes('/artifacts/') && input.pathname.endsWith('/content')) return { status: 200, body: new Uint8Array([1, 2, 3]) };
        if (input.pathname.includes('/voice-assets/')) return { status: 200, headers: { 'content-type': 'image/jpeg' }, body: new Uint8Array([4, 5]) };
        if (input.pathname.includes('/generations/')) return { status: 200, body: { state_version: 2 } };
        if (input.pathname === '/api/v1/workflows') return { status: 201, body: { workflow: { workflow_id: 'workflow-1' } } };
        if (input.pathname.includes('/events')) return { status: 200, body: {} };
        return { status: 200, body: { workflow: { workflow_id: 'workflow-1' } } };
    };
    return { calls, request };
}

test('工作流 API 客户端只通过代理发起版本化请求并自动携带幂等键', async () => {
    const transport = createTransport();
    const api = createWorkflowApi({ request: transport.request });
    const workflow = await api.createWorkflow({ workflow_type: 'tts', configuration: {} });

    assert.deepEqual(workflow, { workflow_id: 'workflow-1' });
    assert.equal(transport.calls[0].pathname, '/api/v1/workflows');
    assert.equal(transport.calls[0].headers['X-Idempotency-Key'].startsWith('renderer-'), true);
    assert.equal(Object.prototype.hasOwnProperty.call(api, 'token'), false);
});

test('生成命令允许在明确的乐观锁冲突重试中复用同一个幂等键', async () => {
    const transport = createTransport();
    const api = createWorkflowApi({ request: transport.request });
    await api.generateWorkflow('workflow-1', { expected_state_version: 2 }, { idempotencyKey: 'renderer-generate-fixed-key' });
    const generateCall = transport.calls.find(call => call.pathname.endsWith('/generate'));
    assert.equal(generateCall.headers['X-Idempotency-Key'], 'renderer-generate-fixed-key');
    assert.equal('idempotencyKey' in (generateCall.body || {}), false);
});

test('取消命令也支持固定幂等键，重试不会制造第二次控制命令', async () => {
    const transport = createTransport();
    const api = createWorkflowApi({ request: transport.request });
    await api.sendCommand(
        'workflow-1',
        'cancel',
        { expected_state_version: 3, reason: 'test-cancel' },
        { idempotencyKey: 'renderer-cancel-fixed-key' },
    );
    const cancelCall = transport.calls.find(call => call.pathname.endsWith('/cancel'));
    assert.equal(cancelCall.headers['X-Idempotency-Key'], 'renderer-cancel-fixed-key');
});

test('事件连接委托主进程申请 ticket，源文件写入在 API 客户端内部申请 grant', async () => {
    const transport = createTransport();
    let opened = null;
    const api = createWorkflowApi({
        request: transport.request,
        openEvents: async (input) => {
            opened = input;
            return { onFrame() { return () => {}; }, onError() { return () => {}; }, async close() {} };
        },
        upload: async (input) => {
            assert.equal(input.headers['X-Source-Write-Grant'], 'writer-grant');
            assert.equal(input.headers['X-Staging-Generation'], '1');
            return { status: 201, body: { status: 'READY' } };
        },
    });

    await api.openWorkflowEvents('workflow-1', null);
    assert.deepEqual(opened, { workflowId: 'workflow-1', lastEventId: null });
    assert.equal(transport.calls.some((call) => call.pathname.includes('/event-tickets')), false);
    await api.writeSourceImport('import-1', 1, new Uint8Array([4, 5]));
    const writerTicketCall = transport.calls.find((call) => call.pathname.includes('/writer-tickets'));
    assert.ok(writerTicketCall);
    assert.match(writerTicketCall.headers['X-Idempotency-Key'], /^renderer-/);
    assert.deepEqual(writerTicketCall.body, { expected_state_version: 2 });
});

test('原生源文件引用走主进程流式上传，不把文件句柄暴露给渲染器', async () => {
    const transport = createTransport();
    let uploaded = null;
    const api = createWorkflowApi({
        request: transport.request,
        uploadSourceFile: async (input) => {
            uploaded = input;
            return { status: 201, body: { status: 'READY' } };
        },
    });

    await api.writeSourceImport('import-1', 1, { sourceFileId: 'source_1' });
    assert.equal(uploaded.sourceFileId, 'source_1');
    assert.equal(uploaded.pathname, '/api/v1/source-imports/import-1/content');
    assert.equal(uploaded.headers['X-Source-Write-Grant'], 'writer-grant');
});

test('源文件上传 transport 的非 2xx 响应会传播稳定错误', async () => {
    for (const options of [
        { upload: async () => ({ status: 409, body: { error_code: 'STATE_CONFLICT', message: 'stale generation' } }) },
        { uploadSourceFile: async () => ({ status: 409, body: { error_code: 'STATE_CONFLICT', message: 'stale generation' } }) },
    ]) {
        const transport = createTransport();
        const api = createWorkflowApi({ request: transport.request, ...options });
        const content = options.uploadSourceFile ? { sourceFileId: 'source_1' } : new Uint8Array([1]);
        await assert.rejects(
            () => api.writeSourceImport('import-1', 1, content),
            (error) => error.code === 'STATE_CONFLICT' && error.status === 409,
        );
    }
});

test('非 2xx 响应会保留稳定错误码，Artifact 内容返回 ReadableStream', async () => {
    const transport = createTransport();
    const api = createWorkflowApi({
        request: async (input) => {
            if (input.pathname.includes('/workflows/')) return { status: 409, body: { error_code: 'STATE_CONFLICT', message: 'stale' } };
            return transport.request(input);
        },
    });
    await assert.rejects(() => api.getWorkflow('workflow-1'), (error) => error.code === 'STATE_CONFLICT' && error.status === 409);

    const artifactApi = createWorkflowApi({ request: transport.request });
    const stream = await artifactApi.openArtifact('artifact-1');
    const reader = stream.getReader();
    const chunk = await reader.read();
    assert.deepEqual(Array.from(chunk.value), [1, 2, 3]);
    assert.equal((await reader.read()).done, true);
});

test('生产 Artifact 读取优先委托主进程流式 transport', async () => {
    const calls = [];
    const api = createWorkflowApi({
        request: async (input) => {
            calls.push(input);
            return { status: 500, body: { message: 'buffered transport should not be used' } };
        },
        openArtifactStream: async ({ artifactId }) => {
            assert.equal(artifactId, 'artifact-large');
            return new ReadableStream({
                start(controller) {
                    controller.enqueue(new Uint8Array([9]));
                    controller.close();
                },
            });
        },
    });

    const stream = await api.openArtifact('artifact-large');
    assert.deepEqual(Array.from((await stream.getReader().read()).value), [9]);
    assert.equal(calls.length, 0);
});

test('语音资源通过带能力的 Preload transport 读取字节，不向渲染器暴露后端 URL', async () => {
    const transport = createTransport();
    const api = createWorkflowApi({ request: transport.request });
    const asset = await api.readVoiceAsset('voice/key', 'avatar');
    assert.deepEqual(Array.from(asset.bytes), [4, 5]);
    assert.equal(asset.contentType, 'image/jpeg');
    assert.equal(transport.calls.at(-1).pathname, '/api/v1/voice-assets/voice%2Fkey/avatar');
});
