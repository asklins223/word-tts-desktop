'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { Readable } = require('node:stream');
const {
    createSseParser,
    normalizeBody,
    requestWorkflow,
    requestWorkflowUpload,
    validateWorkflowPath,
} = require('../workflow-proxy');

test('workflow proxy rejects non-versioned paths and query tokens', () => {
    assert.throws(() => validateWorkflowPath('/api/generate'));
    assert.throws(() => validateWorkflowPath('/api/v1/workflows?token=secret'));
    assert.deepEqual(normalizeBody({ hello: 'world' }).contentType, 'application/json');
});

test('SSE parser handles standard event ids, multi-line JSON and heartbeats', () => {
    const frames = [];
    const parser = createSseParser((frame) => frames.push(frame));
    parser.push(': heartbeat\n\n');
    parser.push('id: event-1\nevent: workflow_event\ndata: {"seq":\n');
    parser.push('data:2}\n\n');
    parser.end();
    assert.deepEqual(frames, [{ id: 'event-1', event: 'workflow_event', data: { seq: 2 } }]);
});

test('主进程代理附加 capability，并把 JSON 响应结构化返回', async () => {
    const server = http.createServer((req, res) => {
        assert.equal(req.url, '/api/v1/workflows');
        assert.equal(req.headers['x-desktop-capability'], 'capability');
        res.setHeader('Content-Type', 'application/json');
        res.end(JSON.stringify({ ok: true }));
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const address = server.address();
    try {
        const response = await requestWorkflow({
            http,
            baseUrl: `http://127.0.0.1:${address.port}`,
            capability: 'capability',
            method: 'POST',
            pathname: '/api/v1/workflows',
            body: { hello: 'world' },
        });
        assert.equal(response.status, 200);
        assert.deepEqual(response.body, { ok: true });
    } finally {
        await new Promise((resolve) => server.close(resolve));
    }
});

test('源文档上传由主进程按长度流式转发，不把内容拼成一个请求缓冲', async () => {
    const received = [];
    const server = http.createServer((req, res) => {
        assert.equal(req.url, '/api/v1/source-imports/import-1/content');
        assert.equal(req.headers['x-desktop-capability'], 'capability');
        assert.equal(req.headers['content-length'], '5');
        req.on('data', (chunk) => received.push(Buffer.from(chunk)));
        req.on('end', () => {
            res.setHeader('Content-Type', 'application/json');
            res.end(JSON.stringify({ status: 'READY' }));
        });
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const address = server.address();
    try {
        const response = await requestWorkflowUpload({
            http,
            baseUrl: `http://127.0.0.1:${address.port}`,
            capability: 'capability',
            pathname: '/api/v1/source-imports/import-1/content',
            headers: { 'X-Source-Write-Grant': 'grant' },
            bodyStream: Readable.from([Buffer.from([1, 2]), Buffer.from([3, 4, 5])]),
            contentLength: 5,
        });
        assert.equal(response.status, 200);
        assert.deepEqual(response.body, { status: 'READY' });
        assert.deepEqual(Buffer.concat(received), Buffer.from([1, 2, 3, 4, 5]));
    } finally {
        await new Promise((resolve) => server.close(resolve));
    }
});
