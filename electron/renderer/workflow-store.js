'use strict';

const STORAGE_PREFIX = 'wordtts.workflow.last-event.';
const SEQ_SUFFIX = '.seq';

// Keep the renderer-facing projection deliberately scalar and bounded.  The
// durable snapshot may grow new server fields over time; the desktop store
// should not accidentally retain configuration blobs, credentials, paths, or
// an unbounded event payload as part of its reactive UI state.
const WORKFLOW_PROJECTION_FIELDS = Object.freeze([
    'workflow_id',
    'workflow_group_id',
    'parent_workflow_id',
    'status',
    'result_status',
    'execution_state',
    'control_state',
    'cleanup_state',
    'current_step_id',
    'state_version',
    'group_state_version',
    'draft_revision',
    'source_artifact_id',
    'item_count',
    'artifact_count',
    'latest_event_id',
    'latest_seq',
    'updated_at',
]);

function projectWorkflowSnapshot(workflow) {
    if (!workflow || typeof workflow !== 'object') return null;
    const projection = {};
    WORKFLOW_PROJECTION_FIELDS.forEach((field) => {
        if (workflow[field] !== undefined) projection[field] = workflow[field];
    });
    const latest = workflow.latest_event;
    if (latest && typeof latest === 'object') {
        projection.latest_event_type = String(latest.event_type || '').slice(0, 128) || null;
        projection.latest_event_seq = Number.isInteger(latest.seq) ? latest.seq : null;
        const payload = latest.payload;
        if (payload && typeof payload === 'object') {
            const runtime = {};
            ['phase', 'status', 'message', 'item_id', 'segment_id', 'stage', 'error'].forEach((field) => {
                if (payload[field] !== undefined && payload[field] !== null) {
                    runtime[field] = String(payload[field]).slice(0, 500);
                }
            });
            ['completed_segments', 'total_segments'].forEach((field) => {
                const value = Number(payload[field]);
                if (Number.isInteger(value) && value >= 0) runtime[field] = value;
            });
            if (Object.keys(runtime).length > 0) projection.runtime = runtime;
        }
    }
    return projection;
}

function createWorkflowStore({ storage = null, keyPrefix = STORAGE_PREFIX } = {}) {
    let state = {
        workflow: null,
        workflowId: null,
        lastEventId: null,
        lastSeq: 0,
        needsCatchup: false,
        connected: false,
        error: null,
    };
    const listeners = new Set();
    let stream = null;
    let detachFrame = null;
    let detachError = null;

    const notify = () => {
        const snapshot = getState();
        listeners.forEach((listener) => listener(snapshot));
    };
    const persist = () => {
        if (!storage || !state.workflowId) return;
        try {
            const cursorKey = `${keyPrefix}${state.workflowId}`;
            const seqKey = `${cursorKey}${SEQ_SUFFIX}`;
            if (state.lastEventId) storage.setItem(cursorKey, state.lastEventId);
            else storage.removeItem(cursorKey);
            if (state.lastEventId && Number.isInteger(state.lastSeq) && state.lastSeq > 0) {
                storage.setItem(seqKey, String(state.lastSeq));
            } else {
                storage.removeItem(seqKey);
            }
        } catch (_) {
            // A full localStorage must not turn an already-reduced event into
            // a false failure; the server cursor remains authoritative.
        }
    };
    const storedCursor = (workflowId) => {
        if (!storage) return null;
        try { return storage.getItem(`${keyPrefix}${workflowId}`); } catch (_) { return null; }
    };
    const storedSeq = (workflowId) => {
        if (!storage) return 0;
        try {
            const value = Number.parseInt(storage.getItem(`${keyPrefix}${workflowId}${SEQ_SUFFIX}`) || '0', 10);
            return Number.isInteger(value) && value > 0 ? value : 0;
        } catch (_) {
            return 0;
        }
    };

    function getState() {
        return {
            ...state,
            workflow: state.workflow ? { ...state.workflow } : null,
            workflowProjection: projectWorkflowSnapshot(state.workflow),
        };
    }

    function subscribe(listener) {
        if (typeof listener !== 'function') return () => {};
        listeners.add(listener);
        return () => listeners.delete(listener);
    }

    function prepare(workflowId, initial = null) {
        const persistedCursor = storedCursor(workflowId);
        const persistedSeq = storedSeq(workflowId);
        const initialCursor = initial?.lastEventId || initial?.workflow?.latest_event_id || null;
        const initialSeq = Number(initial?.lastSeq ?? initial?.workflow?.latest_seq ?? 0);
        const useInitial = !persistedCursor && initialCursor && Number.isInteger(initialSeq) && initialSeq > 0;
        state = {
            ...state,
            workflow: useInitial && initial?.workflow ? { ...initial.workflow } : null,
            workflowId,
            lastEventId: persistedCursor || (useInitial ? initialCursor : null),
            lastSeq: persistedSeq || (useInitial ? initialSeq : 0),
            needsCatchup: false,
            connected: false,
            error: null,
        };
        notify();
        return getState();
    }

    function lastEventIdFor(workflowId) {
        if (state.workflowId === workflowId && state.lastEventId) return state.lastEventId;
        return storedCursor(workflowId);
    }

    function resetCursor(workflowId) {
        const normalizedWorkflowId = String(workflowId || '');
        if (!normalizedWorkflowId) return getState();
        if (storage) {
            try {
                const cursorKey = `${keyPrefix}${normalizedWorkflowId}`;
                storage.removeItem(cursorKey);
                storage.removeItem(`${cursorKey}${SEQ_SUFFIX}`);
            } catch (_) {
                // A storage failure must not prevent the server snapshot from
                // becoming the recovery source on the next connection.
            }
        }
        if (state.workflowId !== normalizedWorkflowId) return getState();
        state = {
            ...state,
            lastEventId: null,
            lastSeq: 0,
            needsCatchup: false,
            error: null,
        };
        notify();
        return getState();
    }

    function consume(frame) {
        if (!frame || (frame.kind !== 'event' && frame.kind !== 'snapshot')) {
            return { accepted: false, reason: 'invalid-frame' };
        }
        if (frame.kind === 'snapshot') {
            const snapshot = frame.snapshot;
            if (!snapshot || !snapshot.state || !snapshot.workflow_id) {
                return { accepted: false, reason: 'invalid-snapshot' };
            }
            const snapshotSeq = Number(snapshot.snapshot_seq ?? snapshot.state.latest_seq ?? 0);
            if (!Number.isInteger(snapshotSeq) || snapshotSeq < 0) {
                return { accepted: false, reason: 'invalid-snapshot' };
            }
            if (state.workflowId && snapshot.workflow_id !== state.workflowId) {
                return { accepted: false, reason: 'workflow-mismatch' };
            }
            if (snapshotSeq < state.lastSeq) {
                return { accepted: false, reason: 'stale-snapshot' };
            }
            state = {
                ...state,
                workflow: snapshot.state,
                workflowId: snapshot.workflow_id,
                lastEventId: snapshot.snapshot_event_id || snapshot.state.latest_event_id || null,
                lastSeq: snapshotSeq,
                needsCatchup: false,
                error: null,
            };
            persist();
            notify();
            return { accepted: true, kind: 'snapshot' };
        }

        const event = frame.event;
        if (!event || !event.workflow_id || !Number.isInteger(event.seq) || event.seq < 1 || !event.event_id) {
            return { accepted: false, reason: 'invalid-event' };
        }
        if (state.workflowId && event.workflow_id !== state.workflowId) {
            return { accepted: false, reason: 'workflow-mismatch' };
        }
        if (event.seq <= state.lastSeq) {
            return { accepted: false, reason: 'duplicate' };
        }
        if (state.lastSeq > 0 && event.seq !== state.lastSeq + 1) {
            state = { ...state, needsCatchup: true, error: `event gap: expected ${state.lastSeq + 1}, got ${event.seq}` };
            notify();
            return { accepted: false, reason: 'gap', expectedSeq: state.lastSeq + 1, actualSeq: event.seq };
        }
        state = {
            ...state,
            workflowId: event.workflow_id,
            lastEventId: event.event_id,
            lastSeq: event.seq,
            workflow: state.workflow ? { ...state.workflow, latest_event_id: event.event_id, latest_seq: event.seq, latest_event: event } : state.workflow,
            error: null,
        };
        persist();
        notify();
        return { accepted: true, kind: 'event' };
    }

    async function connect(workflowId, api) {
        await close();
        prepare(workflowId);
        stream = await api.openWorkflowEvents(workflowId, state.lastEventId);
        detachFrame = stream.onFrame((frame) => consume(frame));
        detachError = stream.onError((error) => {
            state = { ...state, connected: false, error: error?.message || 'workflow stream failed' };
            notify();
        });
        state = { ...state, connected: true };
        notify();
        return getState();
    }

    async function close() {
        detachFrame?.();
        detachError?.();
        detachFrame = null;
        detachError = null;
        if (stream) await stream.close();
        stream = null;
        if (state.connected) {
            state = { ...state, connected: false };
            notify();
        }
    }

    return Object.freeze({ getState, subscribe, consume, prepare, lastEventIdFor, resetCursor, connect, close });
}

const workflowStoreExports = { createWorkflowStore, projectWorkflowSnapshot, STORAGE_PREFIX };
if (typeof module !== 'undefined' && module.exports) {
    module.exports = workflowStoreExports;
} else if (typeof globalThis !== 'undefined') {
    globalThis.createWorkflowStore = createWorkflowStore;
    globalThis.WORDTTS_WORKFLOW_STORE_PREFIX = STORAGE_PREFIX;
}
