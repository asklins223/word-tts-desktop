/*
 * 小猪wordTTS · workflow command coordinator
 *
 * UI buttons are intentionally thin.  This boundary owns the small amount of
 * command protocol that every service action needs: one in-flight request per
 * logical action, a stable idempotency key, one optimistic-lock refresh, and a
 * read-after-timeout so the renderer never guesses whether a command landed.
 */
(function attachWorkflowCommandCoordinator(root) {
    'use strict';

    const COMMANDS = Object.freeze({
        PAUSE: 'pause',
        RESUME: 'resume',
        CANCEL: 'cancel',
        RETRY: 'retry',
        RECONCILE: 'reconcile',
        RESOLVE: 'resolve',
        ARCHIVE: 'archive',
        EXPORT_ZIP: 'export-zip',
        RERUN: 'rerun',
    });
    const TARGETED_COMMANDS = new Set(['retry', 'reconcile', 'resolve']);

    function stableTargetKey(value) {
        if (value === null || value === undefined) return '';
        if (Array.isArray(value)) return `[${value.map(stableTargetKey).join(',')}]`;
        if (typeof value !== 'object') return JSON.stringify(value);
        return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stableTargetKey(value[key])}`).join(',')}}`;
    }

    function timeoutError(timeoutMs) {
        const error = new Error(`workflow command timed out after ${timeoutMs}ms`);
        error.code = 'COMMAND_TIMEOUT';
        error.side_effect_occurred = true;
        error.retryable = false;
        return error;
    }

    function createWorkflowCommandCoordinator({
        api,
        store = null,
        getWorkflowId,
        getWorkspace,
        refresh,
        resolveAction = null,
        onStateChanged = null,
        timeoutMs = 30000,
    } = {}) {
        if (!api || typeof api.sendCommand !== 'function') {
            throw new TypeError('workflow command API is required');
        }
        if (typeof getWorkflowId !== 'function') throw new TypeError('getWorkflowId is required');
        if (typeof refresh !== 'function') throw new TypeError('workflow workspace refresh is required');

        const inFlight = new Map();
        let operationSequence = 0;

        const markPending = (key, pending) => {
            if (typeof store?.markCommandPending === 'function') {
                store.markCommandPending(key, pending);
            }
        };

        const refreshWorkspace = async (workflowId, reason) => {
            try {
                return await refresh(workflowId, { reason });
            } catch (error) {
                // A timeout must remain visible even when the follow-up read is
                // also unavailable.  Do not turn it into a safe-to-retry hint.
                if (reason === 'after-timeout') return null;
                throw error;
            }
        };

        const withTimeout = (promise, limit) => {
            const duration = Math.max(1, Number(limit) || 30000);
            let timer = null;
            const timeout = new Promise((_, reject) => {
                timer = setTimeout(() => reject(timeoutError(duration)), duration);
            });
            return Promise.race([promise, timeout]).finally(() => {
                if (timer) clearTimeout(timer);
            });
        };

        const run = (action, { reason = 'desktop-workspace-action', input = {} } = {}) => {
            const actionType = String(action?.type || '');
            const workflowId = String(getWorkflowId() || '');
            const command = COMMANDS[actionType];
            if (!workflowId || !command || action?.enabled !== true
                || (command === 'rerun' && typeof api.rerun !== 'function')) {
                return Promise.resolve({ ok: false, reason: 'action-disabled' });
            }

            // Targeted commands may legitimately run in parallel for two
            // different items/units.  Their in-flight lock must not collapse
            // those requests into one promise, while workflow-level commands
            // retain the compact legacy key used by Store diagnostics.
            const targetKey = TARGETED_COMMANDS.has(command)
                ? stableTargetKey(action?.target ?? input?.target)
                : '';
            const key = targetKey
                ? `${workflowId}:${actionType}:${targetKey}`
                : `${workflowId}:${actionType}`;
            const existing = inFlight.get(key);
            if (existing) return existing;

            const operationId = `${workflowId}-${actionType.toLowerCase()}-${++operationSequence}`;
            const idempotencyKey = `renderer-${command}-${operationId}`;
            let retainInFlight = false;
            let commandRequest = null;
            const removeInFlight = () => {
                if (inFlight.get(key) === promise) inFlight.delete(key);
            };
            const release = () => {
                markPending(key, false);
                removeInFlight();
            };
            const promise = (async () => {
                markPending(key, true);
                let workspace = typeof getWorkspace === 'function' ? getWorkspace() : null;
                let conflictRetried = false;
                try {
                    workspace = await refreshWorkspace(workflowId, 'before-command') || workspace;
                    while (true) {
                        const currentAction = typeof resolveAction === 'function'
                            ? resolveAction(actionType, workspace) || action
                            : action;
                        if (currentAction.enabled !== true) {
                            return { ok: false, reason: 'action-disabled-after-refresh', workspace };
                        }
                        const snapshot = workspace?.snapshot || workspace?.workflow || {};
                        const expectedStateVersion = Number(
                            currentAction.expected_state_version
                            ?? snapshot.state_version
                            ?? workspace?.sync?.state_version
                            ?? 0,
                        );
                        const body = command === 'rerun'
                            ? { ...input }
                            : {
                                ...input,
                                expected_state_version: Number.isInteger(expectedStateVersion) && expectedStateVersion >= 0
                                    ? expectedStateVersion
                                    : 0,
                            };
                        if (command !== 'export-zip' && command !== 'rerun') body.reason = reason;
                        if (command === 'rerun') {
                            const expectedGroupStateVersion = Number(
                                currentAction.expected_group_state_version
                                ?? snapshot.group_state_version
                                ?? 0,
                            );
                            body.expected_group_state_version = Number.isInteger(expectedGroupStateVersion)
                                && expectedGroupStateVersion >= 0
                                ? expectedGroupStateVersion
                                : 0;
                            body.source_workflow_id = body.source_workflow_id || workflowId;
                            body.reason = body.reason || reason;
                        } else if (TARGETED_COMMANDS.has(command)) {
                            if (currentAction.target) body.target = currentAction.target;
                            const expectedTargetStateVersion = Number(currentAction.expected_target_state_version);
                            if (Number.isInteger(expectedTargetStateVersion) && expectedTargetStateVersion >= 0) {
                                body.expected_target_state_version = expectedTargetStateVersion;
                            }
                            if (currentAction.expected_attempt_id || input.expected_attempt_id) {
                                body.expected_attempt_id = String(
                                    currentAction.expected_attempt_id || input.expected_attempt_id,
                                );
                            }
                        } else {
                            // Archive and export have their own strict request
                            // schemas; a workflow target is already implied by
                            // the URL and must not be copied into their body.
                            delete body.target;
                            delete body.expected_target_state_version;
                            delete body.expected_attempt_id;
                            if (command === 'export-zip') delete body.reason;
                        }
                        try {
                            // Keep a handle to the underlying request. A timeout
                            // only means the client stopped waiting; it does not
                            // cancel the server-side command.
                            commandRequest = Promise.resolve().then(() => command === 'rerun'
                                ? api.rerun(workflowId, body, { idempotencyKey })
                                : api.sendCommand(workflowId, command, body, { idempotencyKey }));
                            const response = await withTimeout(commandRequest, timeoutMs);
                            workspace = await refreshWorkspace(workflowId, 'after-command') || workspace;
                            if (typeof onStateChanged === 'function') onStateChanged(workspace, response);
                            return { ok: true, response, workspace };
                        } catch (error) {
                            if (error?.code === 'STATE_CONFLICT' && !conflictRetried) {
                                conflictRetried = true;
                                workspace = await refreshWorkspace(workflowId, 'state-conflict') || workspace;
                                continue;
                            }
                            if (error?.code === 'COMMAND_TIMEOUT') {
                                // Do not let a second click create a new
                                // idempotency key while the first request may
                                // still reach the service. The lock is released
                                // only after the original request settles.
                                retainInFlight = true;
                                commandRequest?.finally(release).catch(() => {});
                                workspace = await refreshWorkspace(workflowId, 'after-timeout');
                                if (typeof onStateChanged === 'function') onStateChanged(workspace, null);
                                error.workspace = workspace;
                                throw error;
                            }
                            throw error;
                        }
                    }
                } finally {
                    if (!retainInFlight) markPending(key, false);
                }
            })();
            inFlight.set(key, promise);
            promise.finally(() => {
                if (!retainInFlight) removeInFlight();
            }).catch(() => {});
            return promise;
        };

        return Object.freeze({
            inFlightCount: () => inFlight.size,
            isPending: (workflowId, actionType, target = undefined) => {
                const prefix = `${workflowId}:${actionType}`;
                if (target !== undefined) {
                    const targetKey = stableTargetKey(target);
                    return inFlight.has(targetKey ? `${prefix}:${targetKey}` : prefix);
                }
                return [...inFlight.keys()].some(key => key === prefix || key.startsWith(`${prefix}:`));
            },
            run,
        });
    }

    const exports = { COMMANDS, createWorkflowCommandCoordinator };
    if (typeof module !== 'undefined' && module.exports) module.exports = exports;
    if (root) root.WORDTTS_WORKFLOW_COMMAND_COORDINATOR = exports;
})(typeof globalThis !== 'undefined' ? globalThis : null);
