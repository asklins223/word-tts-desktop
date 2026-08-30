'use strict';

function normalizeFrame(streamId, message = {}) {
    if (message.streamId !== streamId || !message.frame) return null;
    const frame = message.frame;
    if (frame.event === 'workflow_event' && frame.data && typeof frame.data === 'object') {
        return { kind: 'event', event: frame.data };
    }
    if (frame.event === 'snapshot' && frame.data && typeof frame.data === 'object') {
        return { kind: 'snapshot', snapshot: frame.data };
    }
    return null;
}

/**
 * Bridge one main-process SSE stream to the renderer without a startup race.
 * The main process flushes its own buffer while `workflow-events-ready` is
 * awaited; the renderer cannot register listeners until that promise returns.
 * Frames received in that interval therefore stay queued here and are
 * delivered when the first listener is attached.
 */
function createWorkflowEventTransport(ipcRenderer) {
    if (!ipcRenderer || typeof ipcRenderer.invoke !== 'function') {
        throw new TypeError('ipcRenderer is required');
    }

    return async ({ workflowId, lastEventId }) => {
        const streamId = await ipcRenderer.invoke('workflow-events-open', { workflowId, lastEventId });
        const frameListeners = new Set();
        const errorListeners = new Set();
        const pendingFrames = [];
        const pendingErrors = [];

        const onFrame = (_event, message = {}) => {
            const frame = normalizeFrame(streamId, message);
            if (!frame) return;
            if (frameListeners.size === 0) {
                pendingFrames.push(frame);
                return;
            }
            frameListeners.forEach((listener) => listener(frame));
        };
        const onError = (_event, message = {}) => {
            if (message.streamId !== streamId) return;
            const error = new Error(message.error?.message || 'workflow SSE failed');
            if (message.error?.code) error.code = message.error.code;
            if (message.error?.status) error.status = message.error.status;
            if (message.error?.closed) error.closed = true;
            if (errorListeners.size === 0) {
                pendingErrors.push(error);
                return;
            }
            errorListeners.forEach((listener) => listener(error));
        };

        ipcRenderer.on('workflow-event', onFrame);
        ipcRenderer.on('workflow-event-error', onError);
        try {
            const ready = await ipcRenderer.invoke('workflow-events-ready', { streamId });
            if (ready === false) throw new Error('workflow event stream is no longer available');
        } catch (error) {
            ipcRenderer.removeListener('workflow-event', onFrame);
            ipcRenderer.removeListener('workflow-event-error', onError);
            try { await ipcRenderer.invoke('workflow-events-close', { streamId }); } catch (_) { /* best effort */ }
            throw error;
        }

        let closed = false;
        return {
            onFrame(listener) {
                if (typeof listener !== 'function') return () => {};
                frameListeners.add(listener);
                pendingFrames.splice(0).forEach((frame) => listener(frame));
                return () => frameListeners.delete(listener);
            },
            onError(listener) {
                if (typeof listener !== 'function') return () => {};
                errorListeners.add(listener);
                pendingErrors.splice(0).forEach((error) => listener(error));
                return () => errorListeners.delete(listener);
            },
            async close() {
                if (closed) return;
                closed = true;
                ipcRenderer.removeListener('workflow-event', onFrame);
                ipcRenderer.removeListener('workflow-event-error', onError);
                pendingFrames.length = 0;
                pendingErrors.length = 0;
                await ipcRenderer.invoke('workflow-events-close', { streamId });
                frameListeners.clear();
                errorListeners.clear();
            },
        };
    };
}

module.exports = { createWorkflowEventTransport, normalizeFrame };
