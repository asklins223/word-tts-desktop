'use strict';

const crypto = require('crypto');

const ALLOWED_METHODS = new Set(['GET', 'POST', 'PATCH', 'PUT', 'DELETE']);

function createAbortError(message = 'workflow transfer was cancelled') {
    const error = new Error(message);
    error.name = 'AbortError';
    error.code = 'USER_CANCELLED';
    return error;
}

function validateWorkflowPath(pathname) {
    if (typeof pathname !== 'string' || !pathname.startsWith('/api/v1/')) {
        throw new Error('workflow proxy only permits /api/v1 paths');
    }
    const queryStart = pathname.indexOf('?');
    const rawQuery = queryStart >= 0 ? pathname.slice(queryStart + 1).split('#', 1)[0] : '';
    const hasQueryToken = rawQuery.split('&').some((part) => {
        const rawName = part.split('=', 1)[0];
        try {
            return decodeURIComponent(rawName).toLowerCase() === 'token';
        } catch (_) {
            // Leave malformed query encoding to the URL parser.  It must not
            // turn an encoded token name into an accepted proxy path.
            return rawName.toLowerCase() === 'token';
        }
    });
    if (pathname.includes('://') || hasQueryToken) {
        throw new Error('workflow proxy rejects absolute URLs and query tokens');
    }
    return pathname;
}

function localBackendUrl(baseUrl, pathname) {
    validateWorkflowPath(pathname);
    const base = new URL(baseUrl);
    const url = new URL(pathname, base);
    if (
        base.protocol !== 'http:'
        || url.protocol !== 'http:'
        || base.hostname !== '127.0.0.1'
        || url.hostname !== '127.0.0.1'
        || url.origin !== base.origin
        || base.username
        || base.password
        || url.username
        || url.password
    ) {
        throw new Error('workflow proxy only permits the local backend');
    }
    return url;
}

function normalizeBody(body) {
    if (body == null) return { payload: null, contentType: null };
    if (typeof body === 'string') return { payload: Buffer.from(body, 'utf8'), contentType: 'text/plain; charset=utf-8' };
    if (Buffer.isBuffer(body)) return { payload: body, contentType: 'application/octet-stream' };
    if (body instanceof Uint8Array) return { payload: Buffer.from(body), contentType: 'application/octet-stream' };
    if (body instanceof ArrayBuffer) return { payload: Buffer.from(new Uint8Array(body)), contentType: 'application/octet-stream' };
    return { payload: Buffer.from(JSON.stringify(body), 'utf8'), contentType: 'application/json' };
}

function collectResponse(res, maxBytes = 16 * 1024 * 1024) {
    return new Promise((resolve, reject) => {
        const chunks = [];
        let size = 0;
        res.on('data', (chunk) => {
            size += chunk.length;
            if (size > maxBytes) {
                res.destroy(new Error('workflow response exceeds the local limit'));
                return;
            }
            chunks.push(Buffer.from(chunk));
        });
        res.on('end', () => resolve(Buffer.concat(chunks)));
        res.on('error', reject);
    });
}

function requestWorkflow({ http, baseUrl, capability, method, pathname, body, headers = {}, timeoutMs = 30000, signal = null }) {
    const upperMethod = String(method || 'GET').toUpperCase();
    if (!ALLOWED_METHODS.has(upperMethod)) throw new Error(`workflow method is not allowed: ${upperMethod}`);
    const url = localBackendUrl(baseUrl, pathname);
    const normalized = normalizeBody(body);
    const requestHeaders = {
        ...headers,
        Accept: 'application/json, application/octet-stream',
        'X-Desktop-Capability': capability,
    };
    if (normalized.contentType && !requestHeaders['Content-Type'] && !requestHeaders['content-type']) {
        requestHeaders['Content-Type'] = normalized.contentType;
    }
    if (normalized.payload) requestHeaders['Content-Length'] = normalized.payload.length;
    return new Promise((resolve, reject) => {
        if (signal?.aborted) {
            reject(createAbortError());
            return;
        }
        let settled = false;
        const cleanup = () => signal?.removeEventListener?.('abort', onAbort);
        const fail = (error) => {
            if (settled) return;
            settled = true;
            cleanup();
            reject(error);
        };
        const onAbort = () => {
            const error = createAbortError();
            req?.destroy(error);
            fail(error);
        };
        const req = http.request(url, { method: upperMethod, headers: requestHeaders }, async (res) => {
            try {
                const buffer = await collectResponse(res);
                const contentType = String(res.headers['content-type'] || '');
                let responseBody = buffer;
                if (contentType.includes('application/json')) {
                    try { responseBody = JSON.parse(buffer.toString('utf8')); } catch (_) { /* return raw body */ }
                }
                if (settled) return;
                settled = true;
                cleanup();
                resolve({ status: res.statusCode || 0, headers: res.headers, body: responseBody });
            } catch (error) {
                fail(error);
            }
        });
        signal?.addEventListener?.('abort', onAbort, { once: true });
        req.once('error', fail);
        req.setTimeout(timeoutMs, () => req.destroy(new Error('workflow backend request timed out')));
        if (normalized.payload) req.write(normalized.payload);
        req.end();
    });
}

function requestWorkflowStream({ http, baseUrl, capability, method = 'GET', pathname, body, headers = {}, timeoutMs = 120000, signal = null }) {
    const upperMethod = String(method || 'GET').toUpperCase();
    if (!ALLOWED_METHODS.has(upperMethod)) throw new Error(`workflow method is not allowed: ${upperMethod}`);
    const url = localBackendUrl(baseUrl, pathname);
    const normalized = normalizeBody(body);
    const requestHeaders = {
        ...headers,
        Accept: 'application/octet-stream, application/json',
        'X-Desktop-Capability': capability,
    };
    if (normalized.contentType && !requestHeaders['Content-Type'] && !requestHeaders['content-type']) {
        requestHeaders['Content-Type'] = normalized.contentType;
    }
    if (normalized.payload) requestHeaders['Content-Length'] = normalized.payload.length;
    return new Promise((resolve, reject) => {
        let settled = false;
        let response = null;
        let abortListenerAttached = false;
        const cleanup = () => {
            if (abortListenerAttached) signal?.removeEventListener?.('abort', onAbort);
            abortListenerAttached = false;
        };
        const fail = (error) => {
            if (settled) return;
            settled = true;
            cleanup();
            reject(error);
        };
        const onAbort = () => {
            const error = createAbortError();
            response?.destroy?.(error);
            req?.destroy(error);
            fail(error);
        };
        if (signal?.aborted) {
            fail(createAbortError());
            return;
        }
        const req = http.request(url, { method: upperMethod, headers: requestHeaders }, (res) => {
            response = res;
            const status = Number(res.statusCode || 0);
            if (status >= 200 && status < 300) {
                settled = true;
                res.setTimeout(timeoutMs, () => res.destroy(new Error('workflow artifact stream timed out')));
                res.once('close', cleanup);
                res.once('end', cleanup);
                resolve(res);
                return;
            }
            collectResponse(res, 1024 * 1024)
                .then((bodyBuffer) => fail(Object.assign(
                    new Error(`workflow stream request failed: HTTP ${status}`),
                    { status, body: bodyBuffer.toString('utf8') },
                )))
                .catch(fail);
        });
        signal?.addEventListener?.('abort', onAbort, { once: true });
        abortListenerAttached = Boolean(signal);
        req.once('error', fail);
        req.setTimeout(timeoutMs, () => req.destroy(new Error('workflow backend request timed out')));
        if (normalized.payload) req.write(normalized.payload);
        req.end();
    });
}

function requestWorkflowUpload({
    http,
    baseUrl,
    capability,
    method = 'PUT',
    pathname,
    headers = {},
    bodyStream,
    contentLength,
    timeoutMs = 900000,
    signal = null,
    onProgress = null,
}) {
    const upperMethod = String(method || 'PUT').toUpperCase();
    if (!ALLOWED_METHODS.has(upperMethod)) throw new Error(`workflow method is not allowed: ${upperMethod}`);
    if (!bodyStream || typeof bodyStream.pipe !== 'function') throw new TypeError('workflow upload stream is required');
    const length = Number(contentLength);
    if (!Number.isSafeInteger(length) || length < 0) throw new TypeError('workflow upload length is invalid');
    if (signal?.aborted) return Promise.reject(createAbortError());
    const url = localBackendUrl(baseUrl, pathname);
    const requestHeaders = {
        ...headers,
        Accept: 'application/json, application/octet-stream',
        'X-Desktop-Capability': capability,
        'Content-Length': String(length),
    };
    delete requestHeaders['Transfer-Encoding'];
    delete requestHeaders['transfer-encoding'];
    return new Promise((resolve, reject) => {
        let settled = false;
        let abortListenerAttached = false;
        let receivedBytes = 0;
        const cleanup = () => {
            bodyStream.removeListener?.('error', onBodyError);
            bodyStream.removeListener?.('data', onBodyData);
            if (abortListenerAttached) signal?.removeEventListener?.('abort', onAbort);
            abortListenerAttached = false;
        };
        const fail = (error) => {
            if (settled) return;
            settled = true;
            cleanup();
            reject(error);
        };
        const onBodyError = (error) => {
            fail(error);
            req.destroy(error);
        };
        const onBodyData = (chunk) => {
            receivedBytes += Number(chunk?.length || chunk?.byteLength || 0);
            try {
                onProgress?.({ receivedBytes, totalBytes: length });
            } catch (_) {
                // Progress listeners are presentation-only and must never
                // interrupt the upload stream.
            }
        };
        const onAbort = () => {
            const error = createAbortError();
            bodyStream.destroy?.(error);
            req.destroy(error);
            fail(error);
        };
        const req = http.request(url, { method: upperMethod, headers: requestHeaders }, (res) => {
            res.setTimeout(timeoutMs, () => res.destroy(new Error('workflow upload response timed out')));
            collectResponse(res, 1024 * 1024)
                .then((buffer) => {
                    const contentType = String(res.headers['content-type'] || '');
                    let responseBody = buffer;
                    if (contentType.includes('application/json')) {
                        try { responseBody = JSON.parse(buffer.toString('utf8')); } catch (_) { /* return raw body */ }
                    }
                    if (settled) return;
                    settled = true;
                    cleanup();
                    resolve({ status: res.statusCode || 0, headers: res.headers, body: responseBody });
                })
                .catch(fail);
        });
        signal?.addEventListener?.('abort', onAbort, { once: true });
        abortListenerAttached = Boolean(signal);
        req.once('error', fail);
        req.setTimeout(timeoutMs, () => req.destroy(new Error('workflow upload timed out')));
        bodyStream.once?.('error', onBodyError);
        bodyStream.on?.('data', onBodyData);
        try {
            bodyStream.pipe(req);
        } catch (error) {
            fail(error);
            req.destroy(error);
        }
    });
}

function createSseParser(onFrame) {
    let buffer = '';
    let eventId = null;
    let eventName = 'message';
    let dataLines = [];
    const dispatch = () => {
        if (!dataLines.length) {
            eventId = null;
            eventName = 'message';
            return;
        }
        const data = dataLines.join('\n');
        let parsed = data;
        try { parsed = JSON.parse(data); } catch (_) { /* non-JSON SSE data remains a string */ }
        onFrame({ id: eventId, event: eventName, data: parsed });
        eventId = null;
        eventName = 'message';
        dataLines = [];
    };
    const consumeLine = (line) => {
        if (line === '') return dispatch();
        if (line.startsWith(':')) return;
        const separator = line.indexOf(':');
        const field = separator >= 0 ? line.slice(0, separator) : line;
        const value = separator >= 0 ? line.slice(separator + 1).replace(/^ /, '') : '';
        if (field === 'id') eventId = value;
        else if (field === 'event') eventName = value || 'message';
        else if (field === 'data') dataLines.push(value);
    };
    return {
        push(chunk) {
            buffer += Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk);
            const lines = buffer.split(/\r?\n/);
            buffer = lines.pop() || '';
            lines.forEach(consumeLine);
        },
        end() {
            if (buffer) consumeLine(buffer);
            dispatch();
        },
    };
}

function openWorkflowSse({ http, baseUrl, capability, pathname, headers = {}, onFrame, onError, timeoutMs = 0 }) {
    const url = localBackendUrl(baseUrl, pathname);
    const parser = createSseParser(onFrame);
    const timeout = Number(timeoutMs);
    const hasTimeout = Number.isFinite(timeout) && timeout > 0;
    let errorReported = false;
    const reportError = (error) => {
        if (errorReported) return;
        errorReported = true;
        onError(error);
    };
    const req = http.get(url, {
        headers: {
            ...headers,
            Accept: 'text/event-stream',
            'X-Desktop-Capability': capability,
        },
    }, (res) => {
        if (res.statusCode !== 200) {
            collectResponse(res, 1024 * 1024)
                .then((body) => reportError(Object.assign(new Error(`workflow SSE returned HTTP ${res.statusCode}`), {
                    status: res.statusCode,
                    body: body.toString('utf8'),
                })))
                .catch(reportError);
            return;
        }
        res.setEncoding('utf8');
        const resetResponseTimeout = () => {
            if (hasTimeout) {
                const failForTimeout = () => res.destroy(new Error('workflow SSE timed out'));
                // Node's request timeout remains armed after headers arrive;
                // reset both handles so an opted-in inactivity timeout is
                // measured from the most recent SSE data frame.
                req.setTimeout(timeout, failForTimeout);
                res.setTimeout(timeout, failForTimeout);
            }
        };
        resetResponseTimeout();
        res.on('data', (chunk) => {
            // A caller may opt into an inactivity timeout, but every received
            // SSE frame/heartbeat starts a fresh interval.  The default is
            // unlimited because the backend intentionally keeps long-running
            // workflow streams open between heartbeats.
            resetResponseTimeout();
            parser.push(chunk);
        });
        res.on('end', () => { parser.end(); reportError(Object.assign(new Error('workflow SSE closed'), { closed: true })); });
        res.on('error', reportError);
    });
    req.once('error', reportError);
    if (hasTimeout) req.setTimeout(timeout, () => req.destroy(new Error('workflow SSE timed out')));
    return {
        close: () => {
            errorReported = true;
            req.destroy();
        },
    };
}

function newStreamId() {
    return `stream_${crypto.randomBytes(12).toString('hex')}`;
}

module.exports = {
    ALLOWED_METHODS,
    createSseParser,
    newStreamId,
    normalizeBody,
    localBackendUrl,
    openWorkflowSse,
    requestWorkflow,
    requestWorkflowStream,
    requestWorkflowUpload,
    createAbortError,
    validateWorkflowPath,
};
