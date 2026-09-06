/** Renderer module: workflow.viewModel */
(function attachRendererFeature_workflow_viewModel(root) {
    'use strict';

function setProgressBarPercent(percent) {
    const bar = $('progress-bar');
    if (!bar) return;
    const normalized = Math.min(100, Math.max(0, Number(percent) || 0));
    bar.style.setProperty('--progress-scale', String(normalized / 100));
}

const STEP_TITLES = {
    1: '01 / 导入文档',
    2: '02 / 内容核对',
    3: '03 / 配置中心',
    4: '04 / 音频交付中心',
};

const WORKSPACE_ORDER = ['import', 'review', 'voice', 'generation', 'delivery'];
const WORKSPACE_TITLES = {
    import: '导入文档',
    review: '内容核对',
    voice: '配置中心',
    generation: '生成任务',
    delivery: '音频交付中心',
};

function authoritativeWorkspace(workflowId = currentSession?.session_id, fallback = currentWorkspace) {
    const targetId = String(workflowId || '');
    const stored = workflowStore?.getState?.().workspaceData;
    const storedId = String(stored?.snapshot?.workflow_id || '');
    if (stored && targetId && storedId === targetId) return stored;
    const fallbackId = String(fallback?.snapshot?.workflow_id || fallback?.workflow_id || '');
    return fallback && (!targetId || !fallbackId || fallbackId === targetId) ? fallback : null;
}

function normalizedWorkspace(workspace = null, snapshot = currentSession) {
    const source = workspace || authoritativeWorkspace(snapshot?.session_id || snapshot?.workflow_id, currentWorkspace);
    if (!source) return null;
    return typeof workflowAdapter.normalizeWorkspace === 'function'
        ? workflowAdapter.normalizeWorkspace(source, snapshot)
        : source;
}

function workspaceUserState(workspace = null, snapshot = currentSession) {
    const sourceWorkspace = workspace || authoritativeWorkspace(snapshot?.session_id || snapshot?.workflow_id, currentWorkspace);
    const reducer = workflowReducer;
    if (typeof reducer.deriveWorkflowUserState === 'function') {
        return reducer.deriveWorkflowUserState(snapshot || sourceWorkspace?.snapshot || {}, sourceWorkspace || {});
    }
    const execution = String(snapshot?.execution_state || sourceWorkspace?.snapshot?.execution_state || 'CREATED');
    return { key: execution, label: execution, description: '', tone: 'info', view: 'generation', terminal: false, primaryAction: null, secondaryActions: [] };
}

const GENERATION_RUNTIME_FROZEN_CONTROL_STATES = new Set([
    'PAUSE_REQUESTED',
    'PAUSED',
    'TERMINATING',
]);

function workspaceControlState(workspace = currentWorkspace, snapshot = currentSession) {
    return String(
        snapshot?.control_state
        || workspace?.snapshot?.control_state
        || workspace?.control_state
        || currentSession?.control_state
        || '',
    ).toUpperCase();
}

function pendingWorkspaceCommand(type, workflowId = currentSession?.session_id) {
    const commandType = String(type || '').toUpperCase();
    const sessionId = String(workflowId || '');
    if (!commandType || !sessionId) return false;
    return workflowStore?.getState?.().pendingCommands?.[`${sessionId}:${commandType}`] === true;
}

function generationWorkflowOwnsRuntimeView(workspace = currentWorkspace, snapshot = currentSession) {
    const control = workspaceControlState(workspace, snapshot);
    const execution = String(snapshot?.execution_state || workspace?.snapshot?.execution_state || '').toUpperCase();
    const result = String(snapshot?.result_status || workspace?.snapshot?.result_status || '').toUpperCase();
    return GENERATION_RUNTIME_FROZEN_CONTROL_STATES.has(control)
        || (control === 'TERMINATED' && (
            (result !== 'SUCCEEDED' && result !== 'PARTIAL_SUCCESS')
            || isHardStoppedWorkflowSnapshot(workspace?.snapshot || snapshot)
        ))
        || ['BLOCKED', 'WAITING_RETRY', 'WAITING_USER'].includes(execution)
        || pendingWorkspaceCommand('PAUSE')
        || pendingWorkspaceCommand('RESUME')
        || generationCancelRequested
        || Boolean(cancelWorkflowPromise);
}

// This is deliberately pure: the server/reducer owns the state, while this
// table owns the words and visual treatment shown on the generation page.
// Keeping the mapping in one place prevents the toolbar and the main console
// from drifting apart when a command or a late runtime event arrives.
function generationStatePresentation(state = {}, {
    runtimeMessage = '',
    pendingPause = false,
    pendingResume = false,
    pendingCancel = false,
    generationResultState = generationResult,
} = {}) {
    const sourceKey = String(state?.key || 'RUNNING').trim().toUpperCase() || 'RUNNING';
    let key = sourceKey;
    const resultState = String(generationResultState || '').trim().toLowerCase();
    if (pendingCancel && !state?.terminal) key = 'TERMINATING';
    else if (pendingPause && ['PREPARING', 'RUNNING', 'RECOVERING'].includes(sourceKey)) key = 'PAUSE_REQUESTED';
    else if (pendingResume && ['PAUSED', 'PAUSE_REQUESTED'].includes(sourceKey)) key = 'RESUME_REQUESTED';
    else if (resultState === 'error' && ['CREATED', 'PREPARING', 'RUNNING', 'RECOVERING'].includes(sourceKey)) key = 'FAILED';
    else if (resultState === 'cancelled' && !state?.terminal) key = 'CANCELLED';

    const defaults = {
        CREATED: {
            visualState: 'running', title: '等待生成', badge: '待生成', liveLabel: '等待任务',
            liveStatus: '内容与配置已准备好，可以开始生成。', note: '确认声音配置后即可开始生成。',
            progressStatus: '等待开始', indeterminate: false, freezeProgress: true, terminal: false,
        },
        PREPARING: {
            visualState: 'running', title: '正在准备生成', badge: '准备中', liveLabel: '准备任务',
            liveStatus: '正在准备生成计划…', note: '正在检查设置并连接讯飞浏览器，请保持应用开启。',
            progressStatus: '正在准备生成', indeterminate: true, freezeProgress: false, terminal: false,
            useRuntimeMessage: true,
        },
        RUNNING: {
            visualState: 'running', title: '正在生成音频', badge: '任务进行中', liveLabel: '当前阶段',
            liveStatus: '讯飞浏览器正在处理', note: '请保持应用开启，讯飞浏览器会在后台完成当前批次。',
            progressStatus: '正在生成', indeterminate: true, freezeProgress: false, terminal: false,
            useRuntimeMessage: true,
        },
        RECOVERING: {
            visualState: 'running', title: '正在恢复任务', badge: '恢复中', liveLabel: '任务恢复',
            liveStatus: '生成服务正在接管任务…', note: '正在从已记录的位置恢复任务，请保持应用开启。',
            progressStatus: '正在恢复任务', indeterminate: true, freezeProgress: false, terminal: false,
            useRuntimeMessage: true,
        },
        PAUSE_REQUESTED: {
            visualState: 'paused', title: '正在暂停生成', badge: '正在暂停', liveLabel: '任务控制',
            liveStatus: '正在暂停，等待当前处理点结束…', note: '暂停请求已收到；当前处理完成后会停在安全点，不会继续提交新的内容。',
            progressStatus: '正在暂停', indeterminate: false, freezeProgress: true, terminal: false,
        },
        PAUSED: {
            visualState: 'paused', title: '任务已暂停', badge: '已暂停', liveLabel: '任务已暂停',
            liveStatus: '任务已暂停，可恢复执行', note: '任务停在安全点，不会继续生成；点击“恢复任务”后继续。',
            progressStatus: '任务已暂停', indeterminate: false, freezeProgress: true, terminal: false,
        },
        RESUME_REQUESTED: {
            visualState: 'running', title: '正在恢复任务', badge: '正在恢复', liveLabel: '任务控制',
            liveStatus: '正在恢复任务…', note: '恢复请求已提交，任务状态同步后会继续处理。',
            progressStatus: '正在恢复', indeterminate: false, freezeProgress: true, terminal: false,
        },
        TERMINATING: {
            visualState: 'stopped', title: '正在停止生成', badge: '正在停止', liveLabel: '任务控制',
            liveStatus: '正在停止生成任务…', note: '正在结束当前任务并保存已完成内容，请稍候。',
            progressStatus: '正在停止', indeterminate: false, freezeProgress: true, terminal: false,
        },
        SUCCEEDED: {
            visualState: 'done', title: '生成完成', badge: '处理完成', liveLabel: '任务完成',
            liveStatus: '音频已完成核验，可以试听或进入交付。', note: '已保存并核验的音频可以试听和下载。',
            progressStatus: '已完成', indeterminate: false, freezeProgress: true, terminal: true,
        },
        PARTIAL_SUCCESS: {
            visualState: 'warning', title: '部分完成', badge: '部分完成', liveLabel: '部分完成',
            liveStatus: '任务已完成，部分内容需要处理。', note: '已完成的音频仍可交付；请查看记录处理剩余内容。',
            progressStatus: '部分完成', indeterminate: false, freezeProgress: true, terminal: true,
        },
        FAILED: {
            visualState: 'error', title: '生成失败', badge: '生成失败', liveLabel: '生成异常',
            liveStatus: '生成任务未能完成。', note: '请查看任务记录，根据提示重试或返回配置。',
            progressStatus: '生成失败', indeterminate: false, freezeProgress: true, terminal: true,
        },
        CANCELLED: {
            visualState: 'stopped', title: '任务已取消', badge: '任务已取消', liveLabel: '任务停止',
            liveStatus: '任务已取消，未完成内容不会继续生成。', note: '已完成的内容会保留；可以从历史记录重新生成。',
            progressStatus: '任务已取消', indeterminate: false, freezeProgress: true, terminal: true,
        },
        BLOCKED: {
            visualState: 'error', title: '任务需要处理', badge: '需要处理', liveLabel: '需要处理',
            liveStatus: '任务遇到阻塞，需要人工处理。', note: '请查看任务记录中的阻塞原因和可用操作。',
            progressStatus: '需要处理', indeterminate: false, freezeProgress: true, terminal: false,
        },
        WAITING_RETRY: {
            visualState: 'warning', title: '等待重试', badge: '等待重试', liveLabel: '等待操作',
            liveStatus: '任务已停在安全重试点。', note: '确认后可以重试未完成的内容。',
            progressStatus: '等待重试', indeterminate: false, freezeProgress: true, terminal: false,
        },
        WAITING_USER: {
            visualState: 'error', title: '等待处理', badge: '等待处理', liveLabel: '等待操作',
            liveStatus: '任务需要人工核验后才能继续。', note: '请查看任务记录并完成必要的处理。',
            progressStatus: '等待处理', indeterminate: false, freezeProgress: true, terminal: false,
        },
        CLOSED: {
            visualState: 'stopped', title: '任务已归档', badge: '已归档', liveLabel: '任务归档',
            liveStatus: '任务已从工作区归档。', note: '历史事实仍然保留，可以从历史记录查看。',
            progressStatus: '已归档', indeterminate: false, freezeProgress: true, terminal: true,
        },
    };
    const selected = defaults[key] || defaults.RUNNING;
    const description = String(state?.description || '').trim();
    const runtime = String(runtimeMessage || '').trim();
    const liveStatus = selected.useRuntimeMessage && runtime
        ? runtime
        : (['BLOCKED', 'WAITING_RETRY', 'WAITING_USER'].includes(key) && description
            ? description
            : selected.liveStatus);
    return {
        sourceKey,
        key,
        ...selected,
        liveStatus,
        terminal: Boolean(selected.terminal || state?.terminal),
    };
}

function workspaceAction(actionType, workspace = null) {
    const sourceWorkspace = workspace || authoritativeWorkspace();
    if (typeof workflowAdapter.action === 'function') return workflowAdapter.action(sourceWorkspace, actionType);
    return (Array.isArray(sourceWorkspace?.available_actions) ? sourceWorkspace.available_actions : [])
        .find(action => String(action?.type || '') === String(actionType)) || null;
}

function workspaceActionEnabled(actionType, workspace = currentWorkspace) {
    return workspaceAction(actionType, workspace)?.enabled === true;
}


registerRendererModule("workflow.viewModel", {
    setProgressBarPercent,
    STEP_TITLES,
    WORKSPACE_ORDER,
    WORKSPACE_TITLES,
    authoritativeWorkspace,
    normalizedWorkspace,
    workspaceUserState,
    GENERATION_RUNTIME_FROZEN_CONTROL_STATES,
    workspaceControlState,
    pendingWorkspaceCommand,
    generationWorkflowOwnsRuntimeView,
    generationStatePresentation,
    workspaceAction,
    workspaceActionEnabled,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

