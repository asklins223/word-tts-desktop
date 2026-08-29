'use strict';

const { randomUUID } = require('crypto');

function idempotencyKey() {
    return `renderer-${randomUUID()}`;
}

function encode(value) {
    return encodeURIComponent(String(value));
}

function toBytes(value) {
    if (value instanceof Uint8Array) return value;
    if (value instanceof ArrayBuffer) return new Uint8Array(value);
    if (typeof Blob !== 'undefined' && value instanceof Blob) return value.arrayBuffer().then((buffer) => new Uint8Array(buffer));
    if (value && typeof value.getReader === 'function') {
        return (async () => {
            const reader = value.getReader();
            const chunks = [];
            let total = 0;
            try {
                while (true) {
                    const part = await reader.read();
                    if (part.done) break;
                    const chunk = part.value instanceof Uint8Array ? part.value : new Uint8Array(part.value || []);
                    chunks.push(chunk);
                    total += chunk.byteLength;
                }
            } finally {
                reader.releaseLock?.();
            }
            const result = new Uint8Array(total);
            let offset = 0;
            chunks.forEach((chunk) => { result.set(chunk, offset); offset += chunk.byteLength; });
            return result;
        })();
    }
    throw new TypeError('source content must be a byte buffer or ReadableStream');
}

function createWorkflowApi({ request, openEvents, upload, uploadSourceFile, openArtifactStream }) {
    if (typeof request !== 'function') throw new TypeError('workflow request transport is required');

    async function call(method, pathname, body, headers = {}) {
        const response = await request({ method, pathname, body, headers });
        return unwrapResponse(response, 'workflow request');
    }

    function unwrapResponse(response, label) {
        const status = Number(response?.status || 0);
        if (status >= 400 || status < 200) {
            const payload = response?.body && typeof response.body === 'object' && !(response.body instanceof Uint8Array)
                ? response.body
                : {};
            const error = new Error(payload.message || `${label} failed: HTTP ${status}`);
            Object.assign(error, payload, { code: payload.error_code, status });
            throw error;
        }
        return response?.body;
    }

    async function mutate(method, pathname, body, fixedIdempotencyKey = null) {
        return call(method, pathname, body, {
            'X-Idempotency-Key': fixedIdempotencyKey || idempotencyKey(),
        });
    }

    return Object.freeze({
        async getWorkflow(workflowId) {
            const response = await call('GET', `/api/v1/workflows/${encode(workflowId)}`);
            return response.workflow;
        },
        async getWorkspace(workflowId) {
            const response = await call('GET', `/api/v1/workflows/${encode(workflowId)}/workspace`);
            return response?.workspace || null;
        },
        async getConfig() {
            return call('GET', '/api/v1/config');
        },
        async cacheVoiceAssets(voiceKeys) {
            return call('POST', '/api/v1/voice-assets/cache', {
                voice_keys: Array.isArray(voiceKeys) ? voiceKeys : [voiceKeys],
            });
        },
        async readVoiceAsset(voiceKey, kind) {
            if (!['avatar', 'sample'].includes(kind)) throw new Error('unsupported voice asset kind');
            const response = await request({
                method: 'GET',
                pathname: `/api/v1/voice-assets/${encode(voiceKey)}/${kind}`,
                headers: { Accept: 'image/*, audio/*, application/octet-stream' },
            });
            const status = Number(response?.status || 0);
            if (status >= 400 || status < 200) {
                const payload = response?.body && typeof response.body === 'object' && !(response.body instanceof Uint8Array)
                    ? response.body
                    : {};
                const error = new Error(payload.message || `voice asset request failed: HTTP ${status}`);
                Object.assign(error, payload, { code: payload.error_code, status });
                throw error;
            }
            const bytes = await toBytes(response?.body);
            // 头像/试听样本是有界小资源（正常 <1MB）；主进程代理已有 16MB
            // 响应上限，这里按资源类型再显式设限，防止单个异常响应占据
            // 渲染进程内存。整块文档内容必须走 source/artifact 流式通道。
            const maxVoiceAssetBytes = 8 * 1024 * 1024;
            if (bytes.byteLength > maxVoiceAssetBytes) {
                throw new Error(`voice asset exceeds the ${maxVoiceAssetBytes} byte limit`);
            }
            return {
                bytes,
                contentType: response?.headers?.['content-type'] || response?.headers?.['Content-Type'] || null,
            };
        },
        async createWorkflow(input) {
            const response = await mutate('POST', '/api/v1/workflows', input);
            return response.workflow;
        },
        async listWorkflows(limit = 100) {
            const response = await call('GET', `/api/v1/workflows?limit=${encode(limit)}`);
            return Array.isArray(response?.workflows) ? response.workflows : [];
        },
        async listActiveWorkflows(limit = 100) {
            const response = await call('GET', `/api/v1/workflows/active?limit=${encode(limit)}`);
            return Array.isArray(response?.workflows) ? response.workflows : [];
        },
        async listItems(workflowId) {
            const response = await call('GET', `/api/v1/workflows/${encode(workflowId)}/items`);
            return Array.isArray(response?.items) ? response.items : [];
        },
        async listArtifacts(workflowId, limit = 500) {
            const response = await call('GET', `/api/v1/workflows/${encode(workflowId)}/artifacts?limit=${encode(limit)}`);
            return Array.isArray(response?.artifacts) ? response.artifacts : [];
        },
        async patchDraft(workflowId, input) {
            const response = await mutate('PATCH', `/api/v1/workflows/${encode(workflowId)}`, input);
            return response.workflow;
        },
        async patchWorkspace(workflowId, input) {
            const response = await mutate('PATCH', `/api/v1/workflows/${encode(workflowId)}/workspace`, input);
            return response?.workspace || null;
        },
        async holdRetry(workflowId, input) {
            return mutate('POST', `/api/v1/workflows/${encode(workflowId)}/retry-hold`, input);
        },
        async sendCommand(workflowId, action, input, options = {}) {
            return mutate(
                'POST',
                `/api/v1/workflows/${encode(workflowId)}/${encode(action)}`,
                input,
                options?.idempotencyKey || null,
            );
        },
        async parseWorkflow(workflowId, input) {
            return mutate('POST', `/api/v1/workflows/${encode(workflowId)}/parse`, input);
        },
        async generateWorkflow(workflowId, input, options = {}) {
            return mutate(
                'POST',
                `/api/v1/workflows/${encode(workflowId)}/generate`,
                input,
                options?.idempotencyKey || null,
            );
        },
        async createExportZip(workflowId, input) {
            const response = await mutate('POST', `/api/v1/workflows/${encode(workflowId)}/export-zip`, input);
            return response?.artifact || null;
        },
        async archiveWorkflow(workflowId, input) {
            return mutate('POST', `/api/v1/workflows/${encode(workflowId)}/archive`, input);
        },
        async retry(workflowId, input) {
            return mutate('POST', `/api/v1/workflows/${encode(workflowId)}/retry`, input);
        },
        async reconcile(workflowId, input) {
            return mutate('POST', `/api/v1/workflows/${encode(workflowId)}/reconcile`, input);
        },
        async rerun(workflowId, input) {
            const response = await mutate('POST', `/api/v1/workflows/${encode(workflowId)}/reruns`, input);
            return response.workflow;
        },
        async resolve(input) {
            if (!input?.attempt_id) throw new Error('resolve requires the source attempt id');
            const { attempt_id: _attemptId, ...body } = input;
            return mutate('POST', `/api/v1/attempts/${encode(_attemptId)}/resolve`, body);
        },
        async openWorkflowEvents(workflowId, lastEventId) {
            if (typeof openEvents !== 'function') throw new Error('workflow SSE transport is unavailable');
            // The main process owns the long-lived capability and obtains the
            // one-time SSE ticket immediately before opening the stream.
            return openEvents({ workflowId, lastEventId });
        },
        async createSourceImport(workflowId, input) {
            return mutate('POST', `/api/v1/workflows/${encode(workflowId)}/source-imports`, input);
        },
        async writeSourceImport(importId, generation, content) {
            const current = await call('GET', `/api/v1/source-imports/${encode(importId)}/generations/${encode(generation)}`);
            const ticket = await call(
                'POST',
                `/api/v1/source-imports/${encode(importId)}/generations/${encode(generation)}/writer-tickets`,
                { expected_state_version: current.state_version },
                { 'X-Idempotency-Key': idempotencyKey() },
            );
            const headers = {
                'X-Idempotency-Key': idempotencyKey(),
                'X-Staging-Generation': String(generation),
                'X-Source-Write-Grant': ticket.grant,
                'X-Artifact-Format': 'bin',
                'Content-Type': 'application/octet-stream',
            };
            if (content && typeof content === 'object' && content.sourceFileId && typeof uploadSourceFile === 'function') {
                const response = await uploadSourceFile({
                    pathname: `/api/v1/source-imports/${encode(importId)}/content`,
                    headers,
                    sourceFileId: String(content.sourceFileId),
                });
                return unwrapResponse(response, 'workflow source upload');
            }
            if (typeof upload === 'function') {
                const response = await upload({
                    pathname: `/api/v1/source-imports/${encode(importId)}/content`,
                    headers,
                    content,
                });
                return unwrapResponse(response, 'workflow source upload');
            }
            return call('PUT', `/api/v1/source-imports/${encode(importId)}/content`, await toBytes(content), headers);
        },
        async getSourceImport(importId) {
            return call('GET', `/api/v1/source-imports/${encode(importId)}`);
        },
        async getSourceImportGeneration(importId, generation) {
            return call('GET', `/api/v1/source-imports/${encode(importId)}/generations/${encode(generation)}`);
        },
        async openArtifact(artifactId) {
            if (typeof openArtifactStream === 'function') {
                return openArtifactStream({ artifactId });
            }
            const ticket = await call('POST', `/api/v1/artifacts/${encode(artifactId)}/content-tickets`);
            const response = await call(
                'GET',
                `/api/v1/artifacts/${encode(artifactId)}/content`,
                undefined,
                { 'X-Artifact-Ticket': ticket.ticket },
            );
            const bytes = response instanceof Uint8Array ? response : new Uint8Array(response || []);
            return new ReadableStream({
                start(controller) {
                    controller.enqueue(bytes);
                    controller.close();
                },
            });
        },
    });
}

module.exports = { createWorkflowApi, idempotencyKey, toBytes };
