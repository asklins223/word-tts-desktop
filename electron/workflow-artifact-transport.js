'use strict';

const { randomUUID } = require('crypto');

function asBytes(value) {
    if (value instanceof Uint8Array) return value;
    if (value instanceof ArrayBuffer) return new Uint8Array(value);
    if (value && value.buffer instanceof ArrayBuffer) {
        return new Uint8Array(value.buffer, value.byteOffset || 0, value.byteLength);
    }
    return new Uint8Array(value || []);
}

/**
 * Bridge a main-process artifact response into a renderer ReadableStream.
 * The request id is installed before invoking the main process so the first
 * chunk cannot be lost during the IPC handshake.
 */
function createWorkflowArtifactTransport(ipcRenderer) {
    if (!ipcRenderer || typeof ipcRenderer.invoke !== 'function') {
        throw new TypeError('ipcRenderer is required');
    }

    return async ({ artifactId }) => {
        const requestId = randomUUID();
        let streamId = null;
        let controller = null;
        let closed = false;
        let pendingAck = false;
        let ackInFlight = false;
        const metadata = {};
        const matches = (message = {}) => (
            message.requestId === requestId
            || (streamId && message.streamId === streamId)
        );
        const cleanup = () => {
            ipcRenderer.removeListener?.('workflow-artifact-data', onData);
            ipcRenderer.removeListener?.('workflow-artifact-meta', onMetadata);
            ipcRenderer.removeListener?.('workflow-artifact-end', onEnd);
            ipcRenderer.removeListener?.('workflow-artifact-error', onError);
        };
        const finishError = (error) => {
            if (closed) return;
            closed = true;
            cleanup();
            if (streamId) {
                void ipcRenderer.invoke('workflow-artifact-close', { streamId }).catch(() => {});
            }
            controller?.error(error instanceof Error ? error : new Error(String(error || 'workflow artifact stream failed')));
        };
        const requestAckIfReadable = () => {
            if (!pendingAck || ackInFlight || closed || !streamId) return;
            if (controller?.desiredSize !== null && controller?.desiredSize <= 0) return;
            pendingAck = false;
            ackInFlight = true;
            void ipcRenderer.invoke('workflow-artifact-ack', { streamId })
                .catch(finishError)
                .finally(() => { ackInFlight = false; });
        };
        const onData = (_event, message = {}) => {
            if (!matches(message) || closed) return;
            try {
                controller?.enqueue(asBytes(message.data));
                pendingAck = true;
                requestAckIfReadable();
            } catch (error) {
                finishError(error);
            }
        };
        const onMetadata = (_event, message = {}) => {
            if (!matches(message) || closed) return;
            const value = message.metadata && typeof message.metadata === 'object'
                ? message.metadata
                : message;
            ['content_type', 'content_length', 'sha256', 'filename'].forEach((field) => {
                if (value[field] !== undefined && value[field] !== null) metadata[field] = value[field];
            });
        };
        const onEnd = (_event, message = {}) => {
            if (!matches(message) || closed) return;
            closed = true;
            cleanup();
            controller?.close();
        };
        const onError = (_event, message = {}) => {
            if (!matches(message) || closed) return;
            finishError(new Error(message.error?.message || 'workflow artifact stream failed'));
        };

        ipcRenderer.on?.('workflow-artifact-data', onData);
        ipcRenderer.on?.('workflow-artifact-meta', onMetadata);
        ipcRenderer.on?.('workflow-artifact-end', onEnd);
        ipcRenderer.on?.('workflow-artifact-error', onError);
        const stream = new ReadableStream({
            start(nextController) {
                controller = nextController;
            },
            pull() {
                requestAckIfReadable();
            },
            cancel() {
                if (closed) return;
                closed = true;
                pendingAck = false;
                cleanup();
                if (streamId) void ipcRenderer.invoke('workflow-artifact-close', { streamId });
            },
        });
        stream.metadata = metadata;
        try {
            streamId = await ipcRenderer.invoke('workflow-artifact-open', { artifactId, requestId });
            if (!streamId) throw new Error('workflow artifact stream was not opened');
            requestAckIfReadable();
            return stream;
        } catch (error) {
            finishError(error);
            throw error;
        }
    };
}

/**
 * ContextBridge-safe Artifact transport.
 *
 * A native ReadableStream cannot be returned from a preload API and still be
 * expected to retain its internal slots in the renderer. The event transport
 * keeps the IPC stream in preload and exposes only plain data plus functions;
 * the renderer rebuilds its own ReadableStream around these callbacks.
 */
function createWorkflowArtifactEventTransport(ipcRenderer) {
    if (!ipcRenderer || typeof ipcRenderer.invoke !== 'function') {
        throw new TypeError('ipcRenderer is required');
    }

    return async ({ artifactId }) => {
        const requestId = randomUUID();
        let streamId = null;
        let closed = false;
        let ended = false;
        let metadataReceived = false;
        let pendingEnd = false;
        const metadata = {};
        const pendingData = [];
        const pendingMetadata = [];
        const pendingErrors = [];
        const dataListeners = new Set();
        const metadataListeners = new Set();
        const endListeners = new Set();
        const errorListeners = new Set();

        const matches = (message = {}) => (
            message.requestId === requestId
            || (streamId && message.streamId === streamId)
        );
        const cleanup = () => {
            ipcRenderer.removeListener?.('workflow-artifact-data', onData);
            ipcRenderer.removeListener?.('workflow-artifact-meta', onMetadata);
            ipcRenderer.removeListener?.('workflow-artifact-end', onEnd);
            ipcRenderer.removeListener?.('workflow-artifact-error', onError);
        };
        const dispatch = (listeners, pending, value) => {
            if (listeners.size === 0) {
                pending.push(value);
                return;
            }
            listeners.forEach((listener) => {
                try { listener(value); } catch (error) { finishError(error); }
            });
        };
        const finishError = (error) => {
            if (closed) return;
            closed = true;
            cleanup();
            const normalized = {
                code: error?.code || null,
                status: error?.status || null,
                message: error?.message || String(error || 'workflow artifact stream failed'),
            };
            if (streamId) {
                void ipcRenderer.invoke('workflow-artifact-close', { streamId }).catch(() => {});
            }
            dispatch(errorListeners, pendingErrors, normalized);
        };
        const onData = (_event, message = {}) => {
            if (!matches(message) || closed) return;
            dispatch(dataListeners, pendingData, asBytes(message.data));
        };
        const onMetadata = (_event, message = {}) => {
            if (!matches(message) || closed) return;
            const value = message.metadata && typeof message.metadata === 'object'
                ? message.metadata
                : message;
            ['content_type', 'content_length', 'sha256', 'filename'].forEach((field) => {
                if (value[field] !== undefined && value[field] !== null) metadata[field] = value[field];
            });
            metadataReceived = true;
            dispatch(metadataListeners, pendingMetadata, { ...metadata });
        };
        const onEnd = (_event, message = {}) => {
            if (!matches(message) || closed) return;
            ended = true;
            closed = true;
            cleanup();
            if (endListeners.size === 0) pendingEnd = true;
            else endListeners.forEach((listener) => {
                try { listener(); } catch (_) { /* listener cleanup is best effort */ }
            });
        };
        const onError = (_event, message = {}) => {
            if (!matches(message) || closed) return;
            const error = new Error(message.error?.message || 'workflow artifact stream failed');
            error.code = message.error?.code || null;
            error.status = message.error?.status || null;
            finishError(error);
        };

        ipcRenderer.on?.('workflow-artifact-data', onData);
        ipcRenderer.on?.('workflow-artifact-meta', onMetadata);
        ipcRenderer.on?.('workflow-artifact-end', onEnd);
        ipcRenderer.on?.('workflow-artifact-error', onError);
        try {
            streamId = await ipcRenderer.invoke('workflow-artifact-open', { artifactId, requestId });
            if (!streamId) throw new Error('workflow artifact stream was not opened');
        } catch (error) {
            finishError(error);
            throw error;
        }

        let transportClosed = false;
        const close = async () => {
            if (transportClosed) return;
            transportClosed = true;
            closed = true;
            cleanup();
            pendingData.length = 0;
            pendingMetadata.length = 0;
            pendingErrors.length = 0;
            dataListeners.clear();
            metadataListeners.clear();
            endListeners.clear();
            errorListeners.clear();
            await ipcRenderer.invoke('workflow-artifact-close', { streamId });
        };

        return {
            getMetadata() { return { ...metadata }; },
            onData(listener) {
                if (typeof listener !== 'function') return () => {};
                dataListeners.add(listener);
                pendingData.splice(0).forEach((value) => listener(value));
                return () => dataListeners.delete(listener);
            },
            onMetadata(listener) {
                if (typeof listener !== 'function') return () => {};
                metadataListeners.add(listener);
                const hadPendingMetadata = pendingMetadata.length > 0;
                pendingMetadata.splice(0).forEach((value) => listener(value));
                if (!hadPendingMetadata && metadataReceived) listener({ ...metadata });
                return () => metadataListeners.delete(listener);
            },
            onEnd(listener) {
                if (typeof listener !== 'function') return () => {};
                endListeners.add(listener);
                if (pendingEnd || ended) {
                    pendingEnd = false;
                    listener();
                }
                return () => endListeners.delete(listener);
            },
            onError(listener) {
                if (typeof listener !== 'function') return () => {};
                errorListeners.add(listener);
                pendingErrors.splice(0).forEach((value) => listener(value));
                return () => errorListeners.delete(listener);
            },
            ack() {
                if (transportClosed || !streamId) return Promise.resolve(false);
                return ipcRenderer.invoke('workflow-artifact-ack', { streamId });
            },
            close,
        };
    };
}

module.exports = { createWorkflowArtifactTransport, createWorkflowArtifactEventTransport, asBytes };
