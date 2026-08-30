/*
 * 小猪wordTTS · workflow view reducer
 *
 * This file contains the renderer's pure interpretation of a server-owned
 * workspace.  It deliberately treats status as a state machine projection:
 * buttons are selected from available_actions and completion is only terminal
 * when execution, control and result facts agree.
 */
(function attachWorkflowReducer(root) {
    'use strict';

    const viewUtils = root?.WORDTTS_WORKFLOW_VIEW_UTILS
        || (typeof module !== 'undefined' && module.exports
            ? require('./workflow-view-utils')
            : {});
    const TERMINAL_RESULTS = new Set(['SUCCEEDED', 'PARTIAL_SUCCESS', 'FAILED', 'CANCELLED']);
    const ITEM_STATUSES = new Set(['PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'AMBIGUOUS', 'CANCELLED', 'SKIPPED', 'UNRESOLVED']);

    function object(value) {
        return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    }

    function integer(value, fallback = 0) {
        const number = Number(value);
        return Number.isFinite(number) ? Math.max(0, Math.round(number)) : fallback;
    }

    function stateSnapshot(workspace, snapshot = null) {
        return object(workspace?.snapshot || snapshot);
    }

    function isReadyAudioArtifact(artifact) {
        const format = String(artifact?.format || '').trim().toLowerCase().replace(/^\./, '');
        const filename = String(artifact?.filename || '').trim();
        const mimeType = String(artifact?.mime_type || '').trim().toLowerCase();
        const sha256 = String(artifact?.sha256 || '').trim().toLowerCase();
        return Boolean(
            artifact
            && artifact.item_id
            && artifact.artifact_type === 'tts-segment'
            && artifact.lifecycle_state === 'READY'
            && artifact.verified === true
            && format === 'mp3'
            && !filename.includes('/')
            && !filename.includes('\\')
            && !/[\x00-\x1f\x7f]/.test(filename)
            && filename.toLowerCase().endsWith('.mp3')
            && mimeType === 'audio/mpeg'
            && Number.isSafeInteger(Number(artifact.size_bytes))
            && Number(artifact.size_bytes) > 0
            && /^[0-9a-f]{64}$/.test(sha256),
        );
    }

    function latestTtsArtifacts(artifacts = []) {
        const latestByItem = new Map();
        (Array.isArray(artifacts) ? artifacts : [])
            .filter(artifact => artifact && artifact.item_id && artifact.artifact_type === 'tts-segment')
            .forEach(artifact => {
                const itemId = String(artifact.item_id);
                const current = latestByItem.get(itemId);
                if (!current) {
                    latestByItem.set(itemId, artifact);
                    return;
                }
                const candidateCreatedAt = String(artifact.created_at || '');
                const currentCreatedAt = String(current.created_at || '');
                if (
                    candidateCreatedAt.localeCompare(currentCreatedAt) > 0
                    || (
                        candidateCreatedAt === currentCreatedAt
                        && String(artifact.artifact_id || '').localeCompare(String(current.artifact_id || '')) > 0
                    )
                ) {
                    latestByItem.set(itemId, artifact);
                }
            });
        return [...latestByItem.values()];
    }

    function normalizeProgress(progress = {}, items = [], artifacts = []) {
        const source = object(progress);
        const rows = Array.isArray(items) ? items : [];
        const readyItemIds = new Set(
            latestTtsArtifacts(artifacts)
                .filter(isReadyAudioArtifact)
                .map(artifact => String(artifact.item_id)),
        );
        // A workspace is keyed by item_id.  Defensive de-duplication keeps a
        // stale duplicate row from inflating totals or making the progress
        // buckets add up to more than the source task actually contains.
        const knownRows = [...new Map(
            rows
                .filter(row => row && row.item_id)
                .map(row => [String(row.item_id), row]),
        ).values()];
        const count = (status) => knownRows.filter(row => String(row.status || '') === status).length;
        const hasSourceCount = key => Object.prototype.hasOwnProperty.call(source, key);
        const sourceCount = (key, fallback) => hasSourceCount(key)
            ? integer(source[key], fallback)
            : fallback;
        const total = sourceCount('total', knownRows.length);
        const derivedCompleted = knownRows.filter(row => (
            String(row.status || '') === 'SUCCEEDED' && readyItemIds.has(String(row.item_id))
        )).length;
        const derivedFailed = count('FAILED');
        const derivedCancelled = count('CANCELLED');
        const derivedSkipped = count('SKIPPED');
        // Workspace progress is the server-owned aggregate.  Use row/artifact
        // facts only for older projections that omit the aggregate fields;
        // this also preserves totals when Store has deliberately capped the
        // number of projected items for a very large document.
        const completed = sourceCount('completed', derivedCompleted);
        const failed = sourceCount('failed', derivedFailed);
        const cancelled = sourceCount('cancelled', derivedCancelled);
        const skipped = sourceCount('skipped', derivedSkipped);
        const pending = sourceCount(
            'pending',
            Math.max(0, total - completed - failed - cancelled - skipped),
        );
        const deliverable = sourceCount('deliverable', derivedCompleted);
        const accounted = completed + failed + cancelled + skipped;
        const percent = total > 0 ? Math.min(100, Math.floor((100 * accounted) / total)) : 0;
        const deliverablePercent = total > 0 ? Math.min(100, Math.floor((100 * deliverable) / total)) : 0;
        return {
            total,
            completed,
            failed,
            cancelled,
            skipped,
            pending,
            deliverable,
            percent,
            deliverable_percent: deliverablePercent,
        };
    }

    function normalizeWorkspace(workspace, snapshot = null) {
        const source = object(workspace);
        const resolvedSnapshot = stateSnapshot(source, snapshot);
        const items = Array.isArray(source.items) ? source.items : [];
        const artifacts = Array.isArray(source.artifacts) ? source.artifacts : [];
        return {
            ...source,
            snapshot: resolvedSnapshot,
            items,
            artifacts,
            blockers: Array.isArray(source.blockers) ? source.blockers : [],
            available_actions: Array.isArray(source.available_actions) ? source.available_actions : [],
            progress: normalizeProgress(source.progress, items, artifacts),
            delivery: object(source.delivery),
            configuration: object(source.configuration),
            sync: object(source.sync),
        };
    }

    function isTerminalSnapshot(snapshot) {
        const source = object(snapshot);
        return source.execution_state === 'TERMINAL'
            && source.control_state === 'TERMINATED'
            && TERMINAL_RESULTS.has(String(source.result_status || ''));
    }

    function actionFor(workspace, actionType, { enabledOnly = false } = {}) {
        const actions = Array.isArray(workspace?.available_actions) ? workspace.available_actions : [];
        const matches = actions.filter(action => String(action?.type || '') === String(actionType));
        if (enabledOnly) return matches.find(action => action.enabled === true) || null;
        return matches.find(action => action.enabled === true) || matches[0] || null;
    }

    function firstBlocker(workspace) {
        return typeof viewUtils.firstBlocker === 'function'
            ? viewUtils.firstBlocker(workspace)
            : null;
    }

    function deriveWorkflowUserState(snapshot = {}, workspace = {}) {
        const source = object(snapshot);
        const normalized = normalizeWorkspace(workspace, source);
        const execution = String(source.execution_state || normalized.snapshot.execution_state || 'CREATED');
        const control = String(source.control_state || normalized.snapshot.control_state || 'RUNNING');
        const result = String(source.result_status || normalized.snapshot.result_status || 'IN_PROGRESS');
        const status = String(source.status || normalized.snapshot.status || 'ACTIVE');
        const blocker = firstBlocker(normalized);
        const progress = normalized.progress;
        const terminal = isTerminalSnapshot({
            execution_state: execution,
            control_state: control,
            result_status: result,
        });

        if (status === 'CLOSED') {
            return { key: 'CLOSED', label: '已归档', description: '任务已从工作区归档，历史事实仍然保留。', tone: 'muted', view: 'history', terminal: true, primaryAction: null, secondaryActions: [] };
        }
        if (terminal) {
            const terminalMap = {
                SUCCEEDED: ['已完成', '音频已核验，可以试听或进入交付。', 'success'],
                PARTIAL_SUCCESS: ['部分完成', `已完成 ${progress.completed} 条，仍有内容需要处理。`, 'warning'],
                FAILED: ['生成失败', blocker?.message || '任务未能完成，可根据可用动作处理。', 'danger'],
                CANCELLED: ['已取消', `任务已停止，已完成 ${progress.completed} 条，${progress.cancelled} 条已取消。`, 'muted'],
            };
            const [label, description, tone] = terminalMap[result] || terminalMap.FAILED;
            return {
                key: result,
                label,
                description,
                tone,
                view: result === 'SUCCEEDED' || result === 'PARTIAL_SUCCESS' ? 'delivery' : 'issues',
                terminal: true,
                primaryAction: (blocker?.requires_reconcile
                    ? actionFor(normalized, 'RECONCILE', { enabledOnly: true })
                    : null)
                    || actionFor(normalized, 'DOWNLOAD_ZIP', { enabledOnly: true })
                    || actionFor(normalized, 'EXPORT_ZIP', { enabledOnly: true })
                    || actionFor(normalized, 'OPEN_VIEW', { enabledOnly: true }),
                secondaryActions: ['RETRY', 'RERUN', 'ARCHIVE'].map(type => actionFor(normalized, type, { enabledOnly: true })).filter(Boolean),
            };
        }
        if (control === 'TERMINATING') {
            return { key: 'TERMINATING', label: '正在停止', description: '停止请求已提交，正在等待任务安全收尾。', tone: 'warning', view: 'generation', terminal: false, primaryAction: null, secondaryActions: [actionFor(normalized, 'CANCEL', { enabledOnly: true })].filter(Boolean) };
        }
        if (control === 'PAUSE_REQUESTED') {
            return { key: 'PAUSE_REQUESTED', label: '正在暂停', description: '暂停请求已提交，等待当前外部操作结束。', tone: 'warning', view: 'generation', terminal: false, primaryAction: null, secondaryActions: [] };
        }
        if (control === 'PAUSED') {
            return { key: 'PAUSED', label: '已暂停', description: '任务停留在可恢复状态，不会自动重复外部提交。', tone: 'info', view: 'generation', terminal: false, primaryAction: actionFor(normalized, 'RESUME', { enabledOnly: true }), secondaryActions: [actionFor(normalized, 'CANCEL', { enabledOnly: true })].filter(Boolean) };
        }
        const stateMap = {
            CREATED: ['待生成', '内容与配置已准备好，可以开始生成。', 'info', 'voice'],
            PREPARING: ['准备中', '正在准备任务与声音配置。', 'info', 'voice'],
            RUNNING: ['生成中', '正在处理文档内容与音频产物。', 'info', 'generation'],
            RECOVERING: ['恢复中', '服务正在接管任务，等待状态同步。', 'warning', 'generation'],
            BLOCKED: ['需要处理', blocker?.message || '任务遇到阻塞，需要人工处理后才能继续。', 'danger', 'issues'],
            WAITING_RETRY: ['等待重试', blocker?.message || '任务已暂停在安全重试点。', 'warning', 'issues'],
            WAITING_USER: ['等待处理', blocker?.message || '任务需要核验或人工决策，不会自动重复提交。', 'danger', 'issues'],
        };
        const [label, description, tone, view] = stateMap[execution] || ['同步中', '正在读取任务状态。', 'info', 'generation'];
        const primaryType = execution === 'WAITING_RETRY'
            ? 'RETRY'
            : ['WAITING_USER', 'BLOCKED'].includes(execution)
                ? 'RECONCILE'
                : ['CREATED', 'PREPARING'].includes(execution)
                    ? 'GENERATE'
                    : 'OPEN_VIEW';
        return {
            key: execution,
            label,
            description,
            tone,
            view,
            terminal: false,
            primaryAction: actionFor(normalized, primaryType, { enabledOnly: true }),
            secondaryActions: [actionFor(normalized, 'CANCEL', { enabledOnly: true }), actionFor(normalized, 'RECONCILE', { enabledOnly: true })].filter(Boolean),
        };
    }

    function deriveActiveView(snapshot, workspace) {
        return deriveWorkflowUserState(snapshot, workspace).view;
    }

    const exports = {
        ITEM_STATUSES,
        TERMINAL_RESULTS,
        actionFor,
        deriveActiveView,
        deriveWorkflowUserState,
        firstBlocker,
        isReadyAudioArtifact,
        isTerminalSnapshot,
        latestTtsArtifacts,
        normalizeProgress,
        normalizeWorkspace,
    };
    if (typeof module !== 'undefined' && module.exports) module.exports = exports;
    if (root) root.WORDTTS_WORKFLOW_REDUCER = exports;
})(typeof globalThis !== 'undefined' ? globalThis : null);
