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
        const matches = (message = {}) => (
            message.requestId === requestId
            || (streamId && message.streamId === streamId)
        );
        const cleanup = () => {
            ipcRenderer.removeListener?.('workflow-artifact-data', onData);
            ipcRenderer.removeListener?.('workflow-artifact-end', onEnd);
            ipcRenderer.removeListener?.('workflow-artifact-error', onError);
        };
        const finishError = (error) => {
            if (closed) return;
            closed = true;
            cleanup();
            controller?.error(error instanceof Error ? error : new Error(String(error || 'workflow artifact stream failed')));
        };
        const onData = (_event, message = {}) => {
            if (!matches(message) || closed) return;
            try {
                controller?.enqueue(asBytes(message.data));
            } catch (error) {
                finishError(error);
            }
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
        ipcRenderer.on?.('workflow-artifact-end', onEnd);
        ipcRenderer.on?.('workflow-artifact-error', onError);
        const stream = new ReadableStream({
            start(nextController) {
                controller = nextController;
            },
            cancel() {
                if (closed) return;
                closed = true;
                cleanup();
                if (streamId) void ipcRenderer.invoke('workflow-artifact-close', { streamId });
            },
        });
        try {
            streamId = await ipcRenderer.invoke('workflow-artifact-open', { artifactId, requestId });
            if (!streamId) throw new Error('workflow artifact stream was not opened');
            return stream;
        } catch (error) {
            finishError(error);
            throw error;
        }
    };
}

module.exports = { createWorkflowArtifactTransport, asBytes };
