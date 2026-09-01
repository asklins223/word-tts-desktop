/**
 * 小猪wordTTS — Frontend Logic v2
 * =================================
 * 五段工作台流程：导入 → 核对 → 配置 → 生成 → 交付
 */

// ============================================================================
// 常量 & 全局状态
// ============================================================================

const isElectron = typeof window.electronAPI !== 'undefined';
const platform = isElectron ? window.electronAPI.platform : 'web';
const workflowApi = isElectron ? window.electronAPI.workflow : null;
function getRendererStorage() {
    try {
        const storage = window.localStorage;
        if (storage && typeof storage.getItem === 'function' && typeof storage.setItem === 'function') {
            return storage;
        }
    } catch (_) {
        // Accessing localStorage can itself throw when persistence is disabled.
    }
    try {
        // Keep the browser/test fallback for environments that expose storage
        // as a global but do not attach it to the synthetic window object.
        if (typeof localStorage !== 'undefined'
            && typeof localStorage.getItem === 'function'
            && typeof localStorage.setItem === 'function') {
            return localStorage;
        }
    } catch (_) {
        // Storage is optional; the workflow remains usable in memory.
    }
    return null;
}
const rendererStorage = getRendererStorage();
const workflowStore = isElectron && typeof window.createWorkflowStore === 'function'
    ? window.createWorkflowStore({
        storage: rendererStorage,
        workspaceLoader: workflowId => workflowApi?.getWorkspace?.(workflowId),
    })
    : null;
const PRODUCT_NAME = '小猪wordTTS';

let currentStep = 1;
let currentView = 'workflow';    // 'workflow' | 'history' | 'history-result' | 'version'
let activeWorkspace = 'import';  // import | review | voice | generation | delivery
let historyReturnStep = 1;       // 从历史中心返回工作流时恢复原步骤
let historyRecords = [];
let historyFilters = { query: '', status: 'all', sort: 'updated' };
let historyRequestToken = 0;     // 使较早的历史列表/详情请求失效
let activeResultContext = null;  // 当前交付页对应当前任务或历史记录
let latestCurrentResultEvent = null; // 从历史详情返回时恢复当前任务的交付页
let currentSession = null;       // { session_id, source_filename, source_artifact_id, parse_results }
let currentWorkspace = null;     // server-owned workspace projection for the active task
let activeWorkflowCandidates = [];
let activeWorkflowListTruncated = false;
let workspaceRefreshTimer = null;
let workspaceRefreshInFlight = null;
let themePreference = 'light';
let currentConfig = null;        // API 返回的配置
let clientConfigInitialized = false; // 防止连接重试时用服务端默认值覆盖用户当前设置
let voiceCatalog = [
    { key: 'amanda', name: '英语-Amanda', gender: 'female', gender_label: '女声', language: ['英语'], tags: ['英语'], categories: ['女声', '英语'] },
    { key: 'george', name: '英语-George', gender: 'male', gender_label: '男声', language: ['英语'], tags: ['英语'], categories: ['男声', '英语'] },
];
let voiceAliasMap = {};
let voiceFilterOptions = [];
let activeVoiceFilter = 'all';
let voiceFiltersExpanded = false;
let activeVoiceRole = '__default_female__';
let voiceRoles = [];
let roleVoiceMap = {};
let voiceParamConfigs = {};
let selectedDefaultFemaleVoice = 'amanda';
let selectedDefaultMaleVoice = 'george';
const DEFAULT_FEMALE_ROLE_KEY = '__default_female__';
const DEFAULT_MALE_ROLE_KEY = '__default_male__';
const ROLE_CONFIG_PREFIX = 'role:';
const DEFAULT_VOICE_PARAMS = { rate: 50, volume: 50, pitch: 50 };
const DEFAULT_FEMALE_VOICE_PARAMS = { rate: 50, volume: 50, pitch: 50 };
const DEFAULT_MALE_VOICE_PARAMS = { rate: 35, volume: 50, pitch: 50 };
const GENERATION_MODE_COMPOSITE = 'composite_cut';
const GENERATION_MODE_SINGLE = 'single_segment';
const DEFAULT_GENERATION_MODE = GENERATION_MODE_COMPOSITE;
const GENERATION_MODE_LABELS = {
    [GENERATION_MODE_COMPOSITE]: '全部生成后切割',
    [GENERATION_MODE_SINGLE]: '单条单条生成',
};
let voicePreviewAudio = null;
let isVoiceDetailCollapsed = false;
let voiceAvatarObserver = null;
let voiceCardsRenderFrame = null;
const VOICE_RECENT_STORAGE_KEY = 'wordtts_recent_xunfei_voices_v1';
const voiceAssetCacheRequests = new Map();
const voiceAssetCacheReady = new Set();
const voiceAssetObjectUrls = new Map();
const VOICE_ASSET_OBJECT_URL_LIMIT = 128;
let workflowStream = null;       // 由 preload 持有一次性 SSE ticket 的连接
let sseReconnectTimer = null;    // SSE 延迟重连计时器
let sseStableTimer = null;       // 连接稳定后重置累计重试次数
let sseConnectionToken = 0;      // 使旧连接回调失效
let isGenerating = false;
let parseAbortController = null; // 当前文档解析请求
let parseAttemptId = 0;          // 使已取消的解析响应失效
const MAX_BROWSER_SOURCE_BYTES = 16 * 1024 * 1024;
const ACTIVE_WORKFLOW_HYDRATE_LIMIT = 8;
const ACTIVE_WORKFLOW_HYDRATE_CONCURRENCY = 2;
const ACTIVE_WORKFLOW_HYDRATE_TIMEOUT_MS = 5000;
const ACTIVE_WORKFLOW_HYDRATE_BUDGET_MS = 20000;
// Browser fallback playback/download is intentionally bounded. Electron's
// native save path and the MediaSource playback path remain stream-backed for
// larger artifacts; a browser that cannot append this MIME type must explain
// the limit instead of silently allocating the whole file.
const MAX_BUFFERED_ARTIFACT_BYTES = 16 * 1024 * 1024;
let sourceImportController = null;
let sourceImportInFlight = false;
let sourceFileDialogInFlight = false;
let globalFileDragActive = false;
let incomingFileDropInFlight = false;
let sourceStagingUploadId = null;
let sourceImportId = null;
let sourceTransportUploadId = null;
let sourceUploadProgressCleanup = null;
let generateAbortController = null; // 当前生成启动请求
let generationAttemptId = 0;        // 使旧生成任务回调失效
let generationStartInFlight = false; // 防止启动握手尚未结束时重复提交
let generationStartAttemptId = 0;
let generationRecoveryRetryInFlight = false; // 重试请求尚未进入新一轮生成时，隐藏旧异常卡片
let cancelWorkflowPromise = null;    // 同一任务只允许一个取消请求链
let generationCancelRequested = false;
let hardStopNavigationRequested = false;
let resultNavigationTimer = null;   // 完成后跳转结果页的计时器
let generatedFiles = [];         // 生成完成的文件列表
let logEntryCount = 0;
const logEntriesByKey = new Map(); // 稳定 key 对应一条可原地更新的时间线记录
const logSeenSeq = new Set();
let logFilter = 'all';
let logAutoFollow = true;
let logUnseenCount = 0;
let logLocalSeq = 0;
let logStageIndex = -1;
const logStageStates = new Map();
const LOG_DOM_LIMIT = 300;
let lastStats = null;             // 最近一次 stats 事件数据
let lastDownloadEvent = null;     // 最近一次 download 事件数据
let sseRetryCount = 0;            // SSE 重连次数计数
const SSE_MAX_RETRIES = 5;        // SSE 最大重连次数
let generationResult = null;      // 'done' | 'error' | null — 跟踪生成结果状态
let transientGenerationErrorMessage = ''; // 启动/传输错误在服务端快照落盘前的临时详情
let lastGenerationConfig = null;  // 最近一次实际提交的配置（用于试听后继续生成全部）
let generationStartupTimer = null; // 让首次连接/浏览器启动阶段持续给出反馈
let wavesurferInstances = [];    // 波形仅负责可视化与定位，播放由原生 Audio 优先处理
let audioElements = [];          // 结果页原生音频元素（支持无需等待波形解码即可播放）
const artifactObjectUrls = new Set();
let currentPlayingAudio = null;  // 当前播放中的原生音频元素
let audioPlayRequestToken = 0;   // 使较早的异步 play() 请求无法覆盖最后一次点击
let waveformObserver = null;     // 结果页激活后，按可见范围加载真实波形
let waveformItems = [];          // 等待渲染波形的结果条目
let waveformQueue = [];          // 限流队列，避免同时解码大量音频
let waveformLoadsActive = 0;
const WAVEFORM_MAX_CONCURRENT = 1;
const WAVEFORM_PLACEHOLDER_BARS = 32;
let waveformRenderToken = 0;     // 使离开结果页后排队中的回调失效
let audioFilterFrame = null;
let isRestarting = false;        // 防止 cleanup 等待期间重复重置或重新上传
let artifactDownloadProgressCleanup = null;
let activeArtifactTransfer = null;
let updateStateCleanup = null;
let updateActionInFlight = false;
let updateState = {
    status: isElectron ? 'idle' : 'disabled',
    currentVersion: '',
    version: null,
    latestVersion: null,
    isForced: false,
    updateMode: null,
    minimumSupportedVersion: null,
    releaseName: '',
    releaseNotes: '',
    releaseDate: null,
    updateMessage: '',
    progress: null,
    error: null,
    checkedAt: null,
    platform,
    canDownload: false,
    canInstall: false,
    releaseUrl: '',
};
const itemContentCache = new Map();
const ITEM_CONTENT_CACHE_LIMIT = 16;

function itemContentCacheKey(itemId, workflowId = currentSession?.session_id) {
    const normalizedItemId = String(itemId || '');
    const normalizedWorkflowId = String(workflowId || '');
    return normalizedWorkflowId ? `${normalizedWorkflowId}:${normalizedItemId}` : normalizedItemId;
}

function readItemContentCache(itemId, workflowId = currentSession?.session_id) {
    const key = itemContentCacheKey(itemId, workflowId);
    if (!key || !itemContentCache.has(key)) return undefined;
    const value = itemContentCache.get(key);
    // Map insertion order is the LRU order. Touch reads so repeatedly opened
    // content remains available while old completed documents are evicted.
    itemContentCache.delete(key);
    itemContentCache.set(key, value);
    return value;
}

function rememberItemContentCache(itemId, value, workflowId = currentSession?.session_id) {
    const key = itemContentCacheKey(itemId, workflowId);
    if (!key || typeof value !== 'string') return;
    itemContentCache.delete(key);
    itemContentCache.set(key, value);
    while (itemContentCache.size > ITEM_CONTENT_CACHE_LIMIT) {
        const oldest = itemContentCache.keys().next().value;
        if (oldest === undefined) break;
        itemContentCache.delete(oldest);
    }
}

const workflowReducer = (typeof globalThis !== 'undefined' && globalThis.WORDTTS_WORKFLOW_REDUCER) || {};
const workflowAdapter = (typeof globalThis !== 'undefined' && globalThis.WORDTTS_WORKFLOW_ADAPTER) || {};
const workflowCommandCoordinator = workflowApi
    && typeof globalThis !== 'undefined'
    && typeof globalThis.WORDTTS_WORKFLOW_COMMAND_COORDINATOR?.createWorkflowCommandCoordinator === 'function'
    ? globalThis.WORDTTS_WORKFLOW_COMMAND_COORDINATOR.createWorkflowCommandCoordinator({
        api: workflowApi,
        store: workflowStore,
        getWorkflowId: () => currentSession?.session_id,
        getWorkspace: () => currentWorkspace,
        refresh: (workflowId, { reason } = {}) => hydrateWorkflowWorkspace(workflowId, {
            silent: reason === 'before-command' || reason === 'after-timeout',
        }),
        resolveAction: (type, workspace) => workspaceAction(type, workspace),
        onStateChanged: (workspace) => {
            if (workspace && currentSession?.session_id === workspace.snapshot?.workflow_id) {
                currentWorkspace = workspace;
                renderWorkspaceShell(workspace, workspace.snapshot);
            }
        },
    })
    : null;

// ============================================================================
// 配置预设管理 (localStorage 持久化)
// ============================================================================

// 讯飞参数与旧版倍率/音色配置不兼容，使用新存储命名空间避免旧值被误套用。
const PRESET_STORAGE_KEY = 'wordtts_presets_xunfei_v3';
const CURRENT_CONFIG_STORAGE_KEY = 'wordtts_current_config_xunfei_v3';

/**
 * 保存尚未创建为预设的当前配置。Windows 渲染进程或页面意外重载后，
 * 仍可恢复用户刚刚选择的参数。
 */
function saveCurrentConfig(config) {
    if (!config) return false;
    try {
        rendererStorage?.setItem(CURRENT_CONFIG_STORAGE_KEY, JSON.stringify(normalizePersistedConfig(config)));
        return true;
    } catch (e) {
        console.error('保存当前配置失败:', e);
        return false;
    }
}

function loadCurrentConfig() {
    try {
        const raw = rendererStorage?.getItem(CURRENT_CONFIG_STORAGE_KEY);
        if (!raw) return null;
        const config = JSON.parse(raw);
        return config && typeof config === 'object' && !Array.isArray(config)
            ? normalizePersistedConfig(config)
            : null;
    } catch (e) {
        console.error('读取当前配置失败:', e);
        return null;
    }
}

function rememberCurrentConfig() {
    saveCurrentConfig(collectPersistedConfig());
}

/**
 * 从 localStorage 读取所有预设。
 */
function loadPresets() {
    try {
        const raw = rendererStorage?.getItem(PRESET_STORAGE_KEY);
        if (!raw) return [];
        const arr = JSON.parse(raw);
        if (!Array.isArray(arr)) return [];
        const sanitized = arr
            .filter(p => p && typeof p === 'object' && p.id && p.name)
            .map(p => ({ ...p, config: normalizePersistedConfig(p.config) }));
        // 迁移旧版本：旧预设可能带有角色映射/角色参数，读取时立即清理，
        // 保证后续任何一次保存都不会继续把文档角色写进长期配置。
        if (JSON.stringify(arr) !== JSON.stringify(sanitized)) {
            try {
                rendererStorage?.setItem(PRESET_STORAGE_KEY, JSON.stringify(sanitized));
            } catch (_) {
                // 迁移失败不影响本次使用，savePresets 仍会在下次操作时重试。
            }
        }
        return sanitized;
    } catch (e) {
        console.error('读取预设失败:', e);
        return [];
    }
}

/**
 * 保存预设列表到 localStorage。
 */
function savePresets(presets) {
    try {
        const sanitized = Array.isArray(presets)
            ? presets.map(p => ({ ...p, config: normalizePersistedConfig(p?.config) }))
            : [];
        rendererStorage?.setItem(PRESET_STORAGE_KEY, JSON.stringify(sanitized));
        return true;
    } catch (e) {
        console.error('保存预设失败:', e);
        showToast('保存失败：存储空间不足');
        return false;
    }
}

function showPromptDialog(title, message, defaultValue = '') {
    return window.WordTTSUI.prompt({
        title,
        message,
        defaultValue,
        inputLabel: '配置名称',
        confirmLabel: '保存配置',
    });
}

function showConfirmDialog(options) {
    return window.WordTTSUI.confirm(options);
}

function showAlertDialog(options) {
    return window.WordTTSUI.alert(options);
}

const UPDATE_STATUS_VALUES = new Set([
    'disabled',
    'idle',
    'checking',
    'up-to-date',
    'available',
    'downloading',
    'downloaded',
    'installing',
    'error',
]);

function normalizeUpdateState(rawState = {}) {
    const source = rawState && typeof rawState === 'object' ? rawState : {};
    const status = UPDATE_STATUS_VALUES.has(String(source.status)) ? String(source.status) : 'error';
    const progress = source.progress && typeof source.progress === 'object'
        ? {
            percent: Math.min(100, Math.max(0, Number(source.progress.percent) || 0)),
            transferred: Math.max(0, Number(source.progress.transferred) || 0),
            total: Math.max(0, Number(source.progress.total) || 0),
            bytesPerSecond: Math.max(0, Number(source.progress.bytesPerSecond) || 0),
        }
        : null;
    return {
        ...updateState,
        ...source,
        status,
        currentVersion: String(source.currentVersion || updateState.currentVersion || ''),
        version: source.version ? String(source.version).replace(/^v/i, '') : null,
        latestVersion: source.latestVersion ? String(source.latestVersion).replace(/^v/i, '') : null,
        isForced: Boolean(source.isForced),
        updateMode: source.updateMode === 'force' ? 'force' : (source.updateMode === 'optional' ? 'optional' : null),
        minimumSupportedVersion: source.minimumSupportedVersion
            ? String(source.minimumSupportedVersion).replace(/^v/i, '')
            : null,
        releaseName: String(source.releaseName || ''),
        releaseNotes: source.releaseNotes || '',
        releaseDate: source.releaseDate ? String(source.releaseDate) : null,
        updateMessage: String(source.updateMessage || ''),
        progress,
        error: source.error && typeof source.error === 'object'
            ? { code: String(source.error.code || 'UPDATE_ERROR'), message: String(source.error.message || '') }
            : null,
        checkedAt: source.checkedAt ? String(source.checkedAt) : null,
        platform: String(source.platform || platform || 'web'),
        canDownload: Boolean(source.canDownload),
        canInstall: Boolean(source.canInstall),
        releaseUrl: String(source.releaseUrl || ''),
    };
}

function hasInstallableUpdate(state = {}) {
    const lifecycleStatus = ['available', 'downloading', 'downloaded', 'installing', 'error'].includes(state.status);
    const acceptedLifecycle = state.canDownload
        || state.status === 'downloading'
        || (state.status === 'downloaded' && state.canInstall)
        || state.status === 'installing';
    return Boolean(
        state.version
        && lifecycleStatus
        && acceptedLifecycle,
    );
}

function versionDisplay(value, fallback = '—') {
    const text = String(value || '').trim();
    return text ? `v${text.replace(/^v/i, '')}` : fallback;
}

function updateStatusPresentation(state = {}) {
    const version = versionDisplay(state.version, '新版本');
    if (state.status === 'disabled') {
        return { code: 'DESKTOP ONLY', title: '桌面端支持自动更新', message: '当前环境没有桌面更新能力；请从 GitHub Releases 获取安装包。', tone: 'neutral' };
    }
    if (state.status === 'checking') {
        return { code: 'CHECKING', title: '正在检查更新', message: '正在连接 GitHub Releases，稍候会显示最新版本。', tone: 'info' };
    }
    if (state.status === 'up-to-date') {
        return { code: 'UP TO DATE', title: '已是最新版本', message: '当前安装版本已经是可用的最新版本。', tone: 'success' };
    }
    if (state.status === 'available') {
        if (!hasInstallableUpdate(state)) {
            return { code: 'VERIFYING ASSET', title: '正在确认更新包', message: '已收到版本信息，正在确认当前平台的安装包是否可用。', tone: 'info' };
        }
        return state.isForced
            ? { code: 'REQUIRED', title: `需要更新到 ${version}`, message: state.updateMessage || '当前版本已停止支持，请先完成更新。', tone: 'danger' }
            : { code: 'NEW RELEASE', title: `发现 ${version}`, message: state.updateMessage || '有新的桌面版本可用，你可以在方便时下载并安装。', tone: 'info' };
    }
    if (state.status === 'downloading') {
        return { code: state.isForced ? 'REQUIRED · DOWNLOADING' : 'DOWNLOADING', title: `正在下载 ${version}`, message: '更新包正在本机准备，请保持应用开启。', tone: 'info' };
    }
    if (state.status === 'downloaded') {
        return { code: state.isForced ? 'REQUIRED · READY' : 'READY TO INSTALL', title: `${version} 已准备好`, message: '更新包已下载完成，重启应用即可完成安装。', tone: 'success' };
    }
    if (state.status === 'installing') {
        return { code: 'INSTALLING', title: '正在启动安装', message: '应用即将重启，请稍候。', tone: 'info' };
    }
    if (state.status === 'error') {
        return { code: 'CHECK FAILED', title: '更新暂时不可用', message: state.error?.message || '请检查网络后重试，或打开 GitHub Releases 手动下载。', tone: 'danger' };
    }
    return { code: 'IDLE', title: '等待检查更新', message: '应用会在启动后自动检查，也可以随时手动检查。', tone: 'neutral' };
}

function isForcedUpdateBlocking() {
    return Boolean(
        updateState.isForced
        && updateState.version
        && (hasInstallableUpdate(updateState) || updateState.status === 'installing')
    );
}

function formatUpdateBytes(value) {
    const bytes = Math.max(0, Number(value) || 0);
    if (bytes < 1024) return `${Math.round(bytes)} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatUpdateDate(value, fallback = '尚未检查') {
    if (!value) return fallback;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return fallback;
    return date.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
    });
}

function renderVersionReleaseNotes(notes) {
    const container = $('version-release-notes');
    if (!container) return;
    container.replaceChildren();
    const text = Array.isArray(notes)
        ? notes.map(entry => typeof entry === 'string' ? entry : `${entry?.version || ''}\n\n${entry?.note || ''}`).join('\n\n')
        : String(notes || '').trim();
    if (!text) {
        const empty = document.createElement('p');
        empty.className = 'version-empty-note';
        empty.textContent = '检查到新版本后，更新说明会显示在这里。';
        container.appendChild(empty);
        return;
    }
    text.split(/\n\s*\n/).map(block => block.trim()).filter(Boolean).slice(0, 120).forEach(block => {
        const firstLine = block.split('\n')[0].trim();
        const headingMatch = firstLine.match(/^#{1,3}\s+(.+)$/);
        const element = headingMatch ? document.createElement('h3') : document.createElement('p');
        element.textContent = headingMatch ? headingMatch[1] : block;
        container.appendChild(element);
    });
}

function renderVersionCenter() {
    const state = normalizeUpdateState(updateState);
    updateState = state;
    const presentation = updateStatusPresentation(state);
    const current = versionDisplay(state.currentVersion);
    const latest = versionDisplay(state.version || state.latestVersion, '暂无');
    // A version string alone is not proof that this platform can install it.
    // The main process only sets canDownload after platform metadata and the
    // concrete release asset have both been verified.
    const hasUpdate = hasInstallableUpdate(state);
    const updateCanDownload = Boolean(
        hasUpdate
        && ['available', 'error'].includes(state.status),
    );
    const card = $('version-status-card');
    if (card) {
        card.dataset.updateStatus = state.status;
        card.classList.toggle('is-forced', Boolean(state.isForced));
        card.classList.toggle('has-update', hasUpdate);
        card.classList.remove('is-neutral', 'is-info', 'is-success', 'is-danger');
        card.classList.add(`is-${presentation.tone}`);
    }
    if ($('version-current')) $('version-current').textContent = current;
    if ($('version-fact-current')) $('version-fact-current').textContent = current;
    if ($('version-fact-latest')) $('version-fact-latest').textContent = latest;
    if ($('version-fact-mode')) $('version-fact-mode').textContent = state.updateMode === 'force' || state.isForced ? '强制更新' : (state.updateMode === 'optional' ? '可选更新' : '—');
    if ($('version-fact-minimum')) $('version-fact-minimum').textContent = state.minimumSupportedVersion ? versionDisplay(state.minimumSupportedVersion) : '不设下限';
    if ($('version-fact-platform')) $('version-fact-platform').textContent = state.platform === 'darwin' ? 'macOS' : (state.platform === 'win32' ? 'Windows' : '桌面端');
    if ($('version-status-code')) $('version-status-code').textContent = presentation.code;
    if ($('version-status-title')) $('version-status-title').textContent = presentation.title;
    if ($('version-status-message')) $('version-status-message').textContent = presentation.message;

    const checkButton = $('version-check-btn');
    if (checkButton) {
        checkButton.hidden = hasUpdate && !['error'].includes(state.status);
        checkButton.disabled = updateActionInFlight || state.status === 'checking' || state.status === 'installing' || state.status === 'disabled';
        checkButton.textContent = state.status === 'checking' ? '检查中…' : (state.status === 'error' ? '重试检查' : '检查更新');
    }
    const downloadButton = $('version-download-btn');
    if (downloadButton) {
        downloadButton.hidden = !updateCanDownload || ['downloaded', 'downloading', 'installing'].includes(state.status);
        downloadButton.disabled = updateActionInFlight || state.status === 'disabled';
        downloadButton.textContent = state.status === 'error' ? '重试下载' : '下载更新';
    }
    const installButton = $('version-install-btn');
    if (installButton) {
        installButton.hidden = !['downloaded', 'installing'].includes(state.status);
        installButton.disabled = updateActionInFlight || state.status === 'installing';
        installButton.textContent = state.status === 'installing' ? '正在安装…' : '重启并安装';
    }
    const releaseButton = $('version-open-release-btn');
    if (releaseButton) releaseButton.disabled = updateActionInFlight;

    const progress = $('version-progress');
    const progressVisible = Boolean(state.progress && ['downloading', 'downloaded'].includes(state.status));
    if (progress) progress.hidden = !progressVisible;
    if (progressVisible) {
        const percent = Math.min(100, Math.max(0, Number(state.progress.percent) || 0));
        if ($('version-progress-label')) $('version-progress-label').textContent = state.status === 'downloaded' ? '下载完成' : '正在下载更新';
        if ($('version-progress-value')) $('version-progress-value').textContent = `${Math.round(percent)}%`;
        if ($('version-progress-bar')) {
            $('version-progress-bar').value = percent;
            $('version-progress-bar').setAttribute('aria-valuetext', `${Math.round(percent)}%`);
        }
        if ($('version-progress-detail')) $('version-progress-detail').textContent = state.progress.total
            ? `${formatUpdateBytes(state.progress.transferred)} / ${formatUpdateBytes(state.progress.total)}${state.progress.bytesPerSecond ? ` · ${formatUpdateBytes(state.progress.bytesPerSecond)}/s` : ''}`
            : '正在传输更新包';
    }
    if ($('version-last-checked')) $('version-last-checked').textContent = state.status === 'checking'
        ? '正在检查…'
        : formatUpdateDate(state.checkedAt);

    const releaseTitle = $('version-release-title');
    if (releaseTitle) releaseTitle.textContent = state.releaseName || (hasUpdate ? `${latest} 更新说明` : '暂无待安装版本');
    const modeBadge = $('version-update-mode-badge');
    if (modeBadge) {
        modeBadge.hidden = !hasUpdate;
        modeBadge.textContent = state.isForced ? '强制更新' : '可选更新';
        modeBadge.classList.toggle('is-forced', Boolean(state.isForced));
    }
    const releaseSummary = $('version-release-summary');
    if (releaseSummary) releaseSummary.hidden = !hasUpdate;
    if ($('version-release-version')) $('version-release-version').textContent = latest;
    if ($('version-release-date')) $('version-release-date').textContent = formatUpdateDate(state.releaseDate, '时间未提供');
    const releaseMessage = $('version-release-message');
    if (releaseMessage) {
        releaseMessage.hidden = !state.updateMessage;
        releaseMessage.textContent = state.updateMessage;
    }
    renderVersionReleaseNotes(hasUpdate ? state.releaseNotes : '');

    const badge = $('version-nav-badge');
    const versionNav = $('version-nav-btn');
    const badgeVisible = hasUpdate && !['up-to-date', 'disabled', 'installing'].includes(state.status);
    if (badge) {
        badge.hidden = !badgeVisible;
        badge.textContent = state.isForced ? '必更' : '新';
    }
    versionNav?.classList.toggle('has-update', badgeVisible);
    const overlay = $('update-required-overlay');
    const forcedVisible = Boolean(
        state.isForced
        && (hasUpdate || (state.version && state.status === 'installing'))
        && !['disabled', 'idle', 'up-to-date'].includes(state.status),
    );
    const appRoot = $('app');
    appRoot?.toggleAttribute?.('inert', forcedVisible);
    if (overlay) {
        const wasHidden = overlay.hidden;
        overlay.hidden = !forcedVisible;
        overlay.setAttribute('aria-hidden', forcedVisible ? 'false' : 'true');
        if (forcedVisible) {
            if ($('update-required-message')) $('update-required-message').textContent = state.updateMessage || `当前版本低于最低支持版本 ${versionDisplay(state.minimumSupportedVersion, latest)}，请先完成更新。`;
            const overlayDownload = $('update-required-download');
            const overlayInstall = $('update-required-install');
            const overlayRetry = $('update-required-retry');
            if (overlayDownload) {
                overlayDownload.hidden = !updateCanDownload || ['downloaded', 'downloading', 'installing'].includes(state.status);
                overlayDownload.disabled = updateActionInFlight;
                overlayDownload.textContent = state.status === 'error' ? '重试下载' : '下载并更新';
            }
            if (overlayInstall) {
                overlayInstall.hidden = !['downloaded', 'installing'].includes(state.status);
                overlayInstall.disabled = updateActionInFlight || state.status === 'installing';
                overlayInstall.textContent = state.status === 'installing' ? '正在安装…' : '重启并安装';
            }
            if (overlayRetry) {
                overlayRetry.hidden = state.status !== 'error' || updateCanDownload;
                overlayRetry.disabled = updateActionInFlight;
            }
            const overlayProgress = $('update-required-progress');
            const overlayProgressVisible = Boolean(state.progress && ['downloading', 'downloaded'].includes(state.status));
            if (overlayProgress) overlayProgress.hidden = !overlayProgressVisible;
            if (overlayProgressVisible) {
                const percent = Math.min(100, Math.max(0, Number(state.progress.percent) || 0));
                if ($('update-required-progress-label')) $('update-required-progress-label').textContent = state.status === 'downloaded' ? '下载完成' : '正在下载更新';
                if ($('update-required-progress-value')) $('update-required-progress-value').textContent = `${Math.round(percent)}%`;
                if ($('update-required-progress-bar')) $('update-required-progress-bar').value = percent;
            }
            const overlayRelease = $('update-required-open-release');
            if (overlayRelease) overlayRelease.disabled = updateActionInFlight;
            if (wasHidden) {
                window.requestAnimationFrame?.(() => {
                    overlay.querySelector('button:not([hidden]):not(:disabled)')?.focus();
                });
            }
        }
    }
}

function applyUpdateState(rawState) {
    updateState = normalizeUpdateState(rawState);
    renderVersionCenter();
    if (updateState.isForced && hasInstallableUpdate(updateState) && ['available', 'downloaded'].includes(updateState.status) && currentView !== 'version') {
        showVersionPage({ fromUpdate: true });
    }
}

async function bindNativeAppUpdates() {
    if (!isElectron || !window.electronAPI?.update) {
        renderVersionCenter();
        return;
    }
    updateStateCleanup?.();
    updateStateCleanup = typeof window.electronAPI.update.onStateChange === 'function'
        ? window.electronAPI.update.onStateChange(state => applyUpdateState(state))
        : null;
    try {
        const initialState = await window.electronAPI.update.getStatus?.();
        if (initialState) applyUpdateState(initialState);
    } catch (error) {
        applyUpdateState({
            status: 'error',
            currentVersion: updateState.currentVersion,
            error: { code: 'UPDATE_STATUS_FAILED', message: error?.message || '无法读取更新状态' },
        });
    }
}

async function runUpdateAction(method, button) {
    const updateApi = window.electronAPI?.update;
    if (!isElectron || !updateApi || typeof updateApi[method] !== 'function') {
        showToast('请在桌面端使用自动更新，或打开 GitHub Releases 手动下载', 'warning');
        return;
    }
    if (updateActionInFlight) return;
    updateActionInFlight = true;
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
    }
    try {
        const result = await updateApi[method]();
        if (result) applyUpdateState(result);
    } catch (error) {
        applyUpdateState({
            ...updateState,
            status: 'error',
            error: { code: 'UPDATE_ACTION_FAILED', message: error?.message || '更新操作失败' },
        });
        showToast(error?.message || '更新操作失败，请稍后重试', 'error');
    } finally {
        updateActionInFlight = false;
        if (button) {
            button.removeAttribute('aria-busy');
            renderVersionCenter();
        }
    }
}

async function openUpdateReleasePage(button) {
    const updateApi = window.electronAPI?.update;
    if (!isElectron || typeof updateApi?.openReleasePage !== 'function') {
        showToast('请在桌面端打开 GitHub Releases 页面', 'warning');
        return;
    }
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
    }
    try {
        const result = await updateApi.openReleasePage();
        if (result?.success === false) showToast(result.message || '无法打开 GitHub Releases', 'error');
    } catch (error) {
        showToast(error?.message || '无法打开 GitHub Releases', 'error');
    } finally {
        if (button) {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    }
}

function bindNativeAppNotices() {
    if (!isElectron) return;
    if (typeof window.electronAPI?.onAppNotice === 'function') {
        window.electronAPI.onAppNotice((notice = {}) => {
            showAlertDialog({
                kicker: notice.kicker || '应用消息',
                title: notice.title || `${PRODUCT_NAME} 提示`,
                message: notice.message || '应用遇到一个需要处理的问题。',
                detail: notice.detail || '',
                tone: notice.tone || 'danger',
                confirmLabel: notice.confirmLabel || '知道了',
            });
        });
    }
    sourceUploadProgressCleanup?.();
    sourceUploadProgressCleanup = typeof window.electronAPI?.onSourceUploadProgress === 'function'
        ? window.electronAPI.onSourceUploadProgress((progress = {}) => {
            const expectedUploadId = sourceTransportUploadId || sourceStagingUploadId;
            if (!expectedUploadId || String(progress.uploadId || '') !== String(expectedUploadId)) return;
            const received = Number(progress.receivedBytes);
            const total = Number(progress.totalBytes);
            if (progress.state === 'transferring') {
                updateSourceImportProgress('正在上传源文档', received, total);
                setUploadFeedback('info', `正在上传源文档 · ${formatSourceBytes(received)} / ${formatSourceBytes(total)}`);
            } else if (progress.state === 'starting') {
                updateSourceImportProgress('正在连接源文档', 0, total);
            }
        })
        : null;
    artifactDownloadProgressCleanup?.();
    artifactDownloadProgressCleanup = typeof window.electronAPI?.onArtifactDownloadProgress === 'function'
        ? window.electronAPI.onArtifactDownloadProgress((progress = {}) => {
            const transfer = activeArtifactTransfer;
            if (!transfer || String(progress.transferId || '') !== String(transfer.transferId)) return;
            transfer.lastProgress = { ...transfer.lastProgress, ...progress };
            renderArtifactTransferProgress(transfer.lastProgress);
            if (['completed', 'failed', 'cancelled'].includes(String(progress.state || ''))) {
                const result = progress.state === 'completed'
                    ? { success: progress.result?.success !== false, ...progress.result }
                    : {
                        success: false,
                        reason: progress.state === 'cancelled' ? 'user-cancelled' : (progress.error?.code || 'download-error'),
                        error: progress.error?.message || progress.result?.error,
                    };
                transfer.resolve?.(result);
                if (activeArtifactTransfer === transfer) activeArtifactTransfer = null;
                window.setTimeout(() => {
                    if (!activeArtifactTransfer) hideArtifactTransferProgress();
                }, 900);
            }
        })
        : null;
}

/**
 * 生成预设描述文字。
 */
function presetSummary(config) {
    if (!config) return '配置数据缺失';
    const normalized = normalizePersistedConfig(config);
    const female = normalized.role_configs?.[DEFAULT_FEMALE_ROLE_KEY] || DEFAULT_VOICE_PARAMS;
    const male = normalized.role_configs?.[DEFAULT_MALE_ROLE_KEY] || DEFAULT_VOICE_PARAMS;
    const parts = [];
    parts.push(`女 ${female.rate}/${female.pitch}/${female.volume}`);
    parts.push(`男 ${male.rate}/${male.pitch}/${male.volume}`);
    parts.push((normalized.format || 'mp3').toUpperCase());
    return parts.join(' · ');
}

/**
 * 渲染 Step 2 的预设下拉框。
 */
function renderStep2PresetSelect() {
    const select = $('preset-select');
    if (!select) return;
    const presets = loadPresets();

    // 保留第一个 option
    select.innerHTML = '<option value="">选择已保存的配置</option>';
    presets.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = `${p.name} (${presetSummary(p.config)})`;
        select.appendChild(opt);
    });
    window.WordTTSUI?.syncSelect(select);
}

/** 讯飞参数滑块（0-100）：range 与 number 双向联动。 */
function clampParamValue(v) {
    if (v === null || v === undefined || v === '') return 50;
    const n = Math.round(Number(v));
    if (!Number.isFinite(n)) return 50;
    return Math.min(100, Math.max(0, n));
}

function normalizeVoiceKey(value, fallback = '') {
    const key = String(value ?? '').trim();
    return key ? key.slice(0, 160) : fallback;
}

function canonicalVoiceKey(value) {
    let key = normalizeVoiceKey(value);
    const legacyDefaultKeys = {
        amanda: 'amanda',
        george: 'george',
        '英语-amanda': 'amanda',
        '英语-george': 'george',
    };
    key = legacyDefaultKeys[key.toLocaleLowerCase('zh-CN')] || key;
    const seen = new Set();
    for (let index = 0; key && index < 8 && !seen.has(key); index += 1) {
        seen.add(key);
        const next = normalizeVoiceKey(voiceAliasMap[key]);
        if (!next || next === key) break;
        key = next;
    }
    return key;
}

function normalizeRoleKeyClient(value) {
    return String(value ?? '').trim().replace(/\s+/g, ' ').toLocaleLowerCase('zh-CN').slice(0, 80);
}

function createDefaultVoiceParams(roleKey = null) {
    if (roleKey === DEFAULT_MALE_ROLE_KEY) return { ...DEFAULT_MALE_VOICE_PARAMS };
    if (roleKey === DEFAULT_FEMALE_ROLE_KEY) return { ...DEFAULT_FEMALE_VOICE_PARAMS };
    return { ...DEFAULT_VOICE_PARAMS };
}

function normalizeRoleConfigKeyClient(value) {
    let raw = String(value ?? '').trim();
    if (raw === DEFAULT_FEMALE_ROLE_KEY || raw === DEFAULT_MALE_ROLE_KEY) return raw;
    if (raw.startsWith(ROLE_CONFIG_PREFIX)) raw = raw.slice(ROLE_CONFIG_PREFIX.length);
    const roleKey = normalizeRoleKeyClient(raw);
    return roleKey ? `${ROLE_CONFIG_PREFIX}${roleKey}` : '';
}

function roleConfigKeyForRole(role) {
    if (role?.kind === 'default-female' || role === DEFAULT_FEMALE_ROLE_KEY) {
        return DEFAULT_FEMALE_ROLE_KEY;
    }
    if (role?.kind === 'default-male' || role === DEFAULT_MALE_ROLE_KEY) {
        return DEFAULT_MALE_ROLE_KEY;
    }
    const raw = typeof role === 'string' ? role : (role?.key || role?.label || '');
    const normalized = normalizeRoleKeyClient(raw);
    return normalized ? `${ROLE_CONFIG_PREFIX}${normalized}` : DEFAULT_FEMALE_ROLE_KEY;
}

function normalizeVoiceParams(raw, fallback = { rate: 50, volume: 50, pitch: 50 }) {
    const values = raw && typeof raw === 'object' ? raw : {};
    return {
        rate: clampParamValue(values.rate ?? fallback.rate),
        volume: clampParamValue(values.volume ?? fallback.volume),
        pitch: clampParamValue(values.pitch ?? fallback.pitch),
    };
}

function normalizeGenerationMode(value) {
    return value === GENERATION_MODE_SINGLE
        ? GENERATION_MODE_SINGLE
        : GENERATION_MODE_COMPOSITE;
}

function generationModeLabel(value) {
    return GENERATION_MODE_LABELS[normalizeGenerationMode(value)] || GENERATION_MODE_LABELS[DEFAULT_GENERATION_MODE];
}

function generationModeDescription(value) {
    return normalizeGenerationMode(value) === GENERATION_MODE_SINGLE
        ? '保留原有单条生成流程，逐段生成后直接整理。'
        : '一次提交全部文本，按人工停顿安全切回单段音频。';
}

function selectedGenerationMode() {
    return normalizeGenerationMode(document.querySelector('input[name="generation-mode"]:checked')?.value);
}

function updateGenerationModeUI(mode) {
    const normalizedMode = normalizeGenerationMode(mode);
    const label = generationModeLabel(normalizedMode);
    const composite = normalizedMode === GENERATION_MODE_COMPOSITE;
    const summary = $('summary-mode');
    const strategy = $('generation-strategy');
    const consoleTitle = $('generation-console-title');
    const consoleDescription = $('generation-console-description');
    const progressDetail = $('progress-mode-detail');
    const stageDetail = $('generation-synthesis-stage-detail');
    if (summary) summary.textContent = label;
    if (strategy) strategy.innerHTML = `<i></i>${label}`;
    if (consoleTitle) consoleTitle.textContent = composite ? '合并生成音频' : '逐条生成音频';
    if (consoleDescription) consoleDescription.textContent = generationModeDescription(normalizedMode);
    if (progressDetail) progressDetail.textContent = composite
        ? '合并生成后切割 · 按停顿恢复单段'
        : '单条生成 · 逐段整理文件';
    if (stageDetail) stageDetail.textContent = composite ? '按停顿切割' : '逐条提交';
    document.querySelectorAll('input[name="generation-mode"]').forEach(input => {
        const selected = input.value === normalizedMode;
        input.checked = selected;
        input.closest('.generation-mode-option')?.classList.toggle('is-selected', selected);
    });
}

function normalizeClientConfig(config = {}) {
    const raw = config && typeof config === 'object' ? config : {};
    const formats = ['mp3'];
    const qualities = [
        '48 kbps（低）',
        '128 kbps（标准）',
        '192 kbps（高）',
        '320 kbps（极高）',
    ];
    const baseParams = normalizeVoiceParams(raw, DEFAULT_VOICE_PARAMS);
    const defaultFemaleVoice = canonicalVoiceKey(
        raw.default_female_voice || currentConfig?.default_female_voice,
    ) || 'amanda';
    const defaultMaleVoice = canonicalVoiceKey(
        raw.default_male_voice || currentConfig?.default_male_voice,
    ) || 'george';
    const normalizedVoiceConfigs = {};
    const legacyVoiceConfigValues = {};
    const rawVoiceConfigs = raw.voice_configs && typeof raw.voice_configs === 'object'
        ? raw.voice_configs
        : {};
    Object.entries(rawVoiceConfigs).slice(0, 512).forEach(([key, value]) => {
        const normalizedKey = canonicalVoiceKey(key);
        if (normalizedKey) {
            legacyVoiceConfigValues[normalizedKey] = value;
            let fallback = baseParams;
            if (normalizedKey === defaultMaleVoice || normalizedKey === 'george') fallback = DEFAULT_MALE_VOICE_PARAMS;
            else if (normalizedKey === defaultFemaleVoice || normalizedKey === 'amanda') fallback = DEFAULT_FEMALE_VOICE_PARAMS;
            normalizedVoiceConfigs[normalizedKey] = normalizeVoiceParams(value, fallback);
        }
    });
    if (!normalizedVoiceConfigs[defaultFemaleVoice]) normalizedVoiceConfigs[defaultFemaleVoice] = { ...DEFAULT_FEMALE_VOICE_PARAMS };
    if (!normalizedVoiceConfigs[defaultMaleVoice]) normalizedVoiceConfigs[defaultMaleVoice] = { ...DEFAULT_MALE_VOICE_PARAMS };

    const normalizedRoleConfigs = {};
    const rawRoleConfigs = raw.role_configs && typeof raw.role_configs === 'object'
        ? raw.role_configs
        : {};
    Object.entries(rawRoleConfigs).slice(0, 512).forEach(([key, value]) => {
        const normalizedKey = normalizeRoleConfigKeyClient(key);
        if (normalizedKey) {
            let fallback = baseParams;
            if (normalizedKey === DEFAULT_MALE_ROLE_KEY) fallback = DEFAULT_MALE_VOICE_PARAMS;
            else if (normalizedKey === DEFAULT_FEMALE_ROLE_KEY) fallback = DEFAULT_FEMALE_VOICE_PARAMS;
            normalizedRoleConfigs[normalizedKey] = normalizeVoiceParams(value, fallback);
        }
    });
    // 旧版配置按音色保存参数。首次升级时将旧值复制到两个默认槽位，
    // 之后槽位各自维护，不再因为选用了同一音色而互相覆盖。
    const legacyParamsForRole = (voiceKey, fallback) => normalizeVoiceParams(
        defaultFemaleVoice === defaultMaleVoice
            ? legacyVoiceConfigValues[voiceKey]
            : normalizedVoiceConfigs[voiceKey],
        fallback,
    );
    if (!normalizedRoleConfigs[DEFAULT_FEMALE_ROLE_KEY]) {
        normalizedRoleConfigs[DEFAULT_FEMALE_ROLE_KEY] = normalizeVoiceParams(
            legacyParamsForRole(defaultFemaleVoice, DEFAULT_FEMALE_VOICE_PARAMS),
            DEFAULT_FEMALE_VOICE_PARAMS,
        );
    }
    if (!normalizedRoleConfigs[DEFAULT_MALE_ROLE_KEY]) {
        normalizedRoleConfigs[DEFAULT_MALE_ROLE_KEY] = normalizeVoiceParams(
            legacyParamsForRole(defaultMaleVoice, DEFAULT_MALE_VOICE_PARAMS),
            DEFAULT_MALE_VOICE_PARAMS,
        );
    }

    const normalizedRoleVoices = {};
    const rawRoleVoices = raw.role_voices && typeof raw.role_voices === 'object'
        ? raw.role_voices
        : {};
    Object.entries(rawRoleVoices).slice(0, 128).forEach(([role, key]) => {
        const roleKey = normalizeRoleKeyClient(role);
        const voiceKey = canonicalVoiceKey(key);
        if (roleKey && voiceKey) normalizedRoleVoices[roleKey] = voiceKey;
    });

    return {
        generation_mode: normalizeGenerationMode(raw.generation_mode ?? DEFAULT_GENERATION_MODE),
        rate: baseParams.rate,
        volume: baseParams.volume,
        pitch: baseParams.pitch,
        format: formats.includes(raw.format) ? raw.format : 'mp3',
        quality: qualities.includes(raw.quality) ? raw.quality : '128 kbps（标准）',
        preview: Boolean(raw.preview),
        default_female_voice: defaultFemaleVoice,
        default_male_voice: defaultMaleVoice,
        voice_configs: normalizedVoiceConfigs,
        role_configs: normalizedRoleConfigs,
        role_voices: normalizedRoleVoices,
    };
}

/**
 * 长期配置只保存默认男女声和它们各自的三项参数。
 * 文档角色属于当前任务，不能因为预设或页面恢复而跨文档复用。
 */
function normalizePersistedConfig(config = {}) {
    const normalized = normalizeClientConfig(config);
    const fallback = normalizeVoiceParams(normalized, DEFAULT_VOICE_PARAMS);
    const femaleParams = normalizeVoiceParams(
        normalized.role_configs?.[DEFAULT_FEMALE_ROLE_KEY]
            || normalized.voice_configs?.[normalized.default_female_voice]
            || normalized,
        fallback,
    );
    const maleParams = normalizeVoiceParams(
        normalized.role_configs?.[DEFAULT_MALE_ROLE_KEY]
            || normalized.voice_configs?.[normalized.default_male_voice]
            || normalized,
        fallback,
    );
    return {
        generation_mode: normalized.generation_mode,
        rate: femaleParams.rate,
        volume: femaleParams.volume,
        pitch: femaleParams.pitch,
        format: 'mp3',
        quality: normalized.quality,
        preview: normalized.preview,
        default_female_voice: normalized.default_female_voice,
        default_male_voice: normalized.default_male_voice,
        role_configs: {
            [DEFAULT_FEMALE_ROLE_KEY]: femaleParams,
            [DEFAULT_MALE_ROLE_KEY]: maleParams,
        },
    };
}

function normalizeVoiceEntry(raw) {
    const item = raw && typeof raw === 'object' ? raw : {};
    const key = normalizeVoiceKey(item.key || item.voice_key || item.id);
    const name = String(item.name || item.speakerName || item.speaker_name || key || '未命名音色').trim();
    const gender = String(item.gender || '').toLowerCase();
    const genderLabel = item.gender_label || (gender === 'female' ? '女声' : gender === 'male' ? '男声' : '音色');
    const toList = value => Array.isArray(value)
        ? value.map(entry => String(entry || '').trim()).filter(Boolean)
        : String(value || '').split(/[、,，|/]/).map(entry => entry.trim()).filter(Boolean);
    const language = toList(item.language || item.languages);
    const tags = toList(item.tags);
    const categories = [...new Set([...toList(item.categories), ...language, ...tags, genderLabel])].slice(0, 24);
    return {
        ...item,
        key: key || `name:${name.toLocaleLowerCase('zh-CN')}`,
        name,
        gender: gender || 'unknown',
        gender_label: genderLabel,
        language,
        tags,
        categories,
        img_url: String(item.img_url || item.imgUrl || '').trim(),
        audio_url: String(item.audio_url || item.audioUrl || '').trim(),
        search_text: [name, genderLabel, ...language, ...tags, ...categories]
            .join(' ').toLocaleLowerCase('zh-CN'),
    };
}

function getVoiceEntry(key) {
    const rawKey = String(key ?? '').trim();
    const normalizedKey = canonicalVoiceKey(rawKey);
    const normalizedName = rawKey.toLocaleLowerCase('zh-CN');
    return voiceCatalog.find(voice => (
        voice.key === normalizedKey
        || String(voice.name || '').trim().toLocaleLowerCase('zh-CN') === normalizedName
    ))
        || normalizeVoiceEntry({ key: normalizedKey, name: normalizedKey || '未选择音色' });
}

function voiceAssetUrl(key, kind) {
    const normalizedKey = canonicalVoiceKey(key);
    if (!normalizedKey || !['avatar', 'sample'].includes(kind)) return '';
    const cacheKey = `${normalizedKey}:${kind}`;
    if (voiceAssetObjectUrls.has(cacheKey)) return voiceAssetObjectUrls.get(cacheKey);
    // Chromium loads file:// pages without the capability header.  In the
    // packaged app the renderer therefore receives a Blob URL created from
    // bytes fetched by the preload proxy; it must never address the backend
    // directly.  Keep the versioned relative path for the browser preview
    // and its configuration-only tests.
    if (isElectron) return '';
    // Keep the asset path versioned.  It is a presentation-only cache endpoint,
    // but it still belongs to the same capability-protected local API.
    return ['', 'api', 'v1', 'voice-assets', encodeURIComponent(normalizedKey), kind].join('/');
}

function clearVoiceAssetObjectUrls() {
    voiceAssetObjectUrls.forEach(url => {
        try { URL.revokeObjectURL(url); } catch (_) { /* ignore */ }
    });
    voiceAssetObjectUrls.clear();
}

function rememberVoiceAssetObjectUrl(key, kind, url) {
    const cacheKey = `${key}:${kind}`;
    const oldUrl = voiceAssetObjectUrls.get(cacheKey);
    if (oldUrl && oldUrl !== url) {
        try { URL.revokeObjectURL(oldUrl); } catch (_) { /* ignore */ }
    }
    while (voiceAssetObjectUrls.size >= VOICE_ASSET_OBJECT_URL_LIMIT && !voiceAssetObjectUrls.has(cacheKey)) {
        const oldest = voiceAssetObjectUrls.keys().next().value;
        if (!oldest) break;
        const oldestUrl = voiceAssetObjectUrls.get(oldest);
        try { URL.revokeObjectURL(oldestUrl); } catch (_) { /* ignore */ }
        voiceAssetObjectUrls.delete(oldest);
    }
    voiceAssetObjectUrls.set(cacheKey, url);
}

async function loadCachedVoiceAsset(key, kind) {
    const normalizedKey = canonicalVoiceKey(key);
    if (!normalizedKey || !workflowApi?.readVoiceAsset) return false;
    if (voiceAssetObjectUrls.has(`${normalizedKey}:${kind}`)) return true;
    try {
        const asset = await workflowApi.readVoiceAsset(normalizedKey, kind);
        const bytes = asset?.bytes;
        if (!(bytes instanceof Uint8Array) || bytes.byteLength === 0 || typeof URL?.createObjectURL !== 'function') return false;
        const contentType = asset.contentType || (kind === 'sample' ? 'audio/mpeg' : 'image/jpeg');
        const url = URL.createObjectURL(new Blob([bytes], { type: contentType }));
        rememberVoiceAssetObjectUrl(normalizedKey, kind, url);
        return true;
    } catch (error) {
        console.warn(`音色${kind}资源读取失败:`, error);
        return false;
    }
}

function queueVoiceAssetCache(keys) {
    const values = Array.isArray(keys) ? keys : [keys];
    const normalizedKeys = [...new Set(values.map(value => canonicalVoiceKey(value)).filter(Boolean))];
    const pendingKeys = normalizedKeys.filter(key => (
        !voiceAssetCacheReady.has(key) && !voiceAssetCacheRequests.has(key)
    ));
    const inFlightRequests = [...new Set(normalizedKeys
        .map(key => voiceAssetCacheRequests.get(key))
        .filter(Boolean))];
    let cacheRequest = null;

    if (pendingKeys.length) {
        if (workflowApi?.cacheVoiceAssets) {
            const request = workflowApi.cacheVoiceAssets(pendingKeys)
                .then(response => {
                    const cached = response?.cached;
                    return Promise.all(pendingKeys.map(async key => {
                        const result = cached?.[key] || {};
                        const kinds = ['avatar', 'sample'].filter(kind => Boolean(result[kind]));
                        if (!isElectron) return { key, ready: false };
                        const loaded = await Promise.all(kinds.map(kind => loadCachedVoiceAsset(key, kind)));
                        const ready = loaded.some(Boolean);
                        if (ready) voiceAssetCacheReady.add(key);
                        return { key, ready };
                    })).then(() => response);
                })
                .catch(error => {
                    // Voice media is an enhancement; a catalog URL remains a
                    // valid fallback when the local cache cannot be populated.
                    console.warn('音色资源缓存失败:', error);
                    return null;
                });
            pendingKeys.forEach(key => voiceAssetCacheRequests.set(key, request));
            cacheRequest = request.finally(() => {
                pendingKeys.forEach(key => {
                    if (voiceAssetCacheRequests.get(key) === request) voiceAssetCacheRequests.delete(key);
                });
            });
        } else {
            // Configuration-only renderer tests and non-Electron preview pages
            // do not have a cache transport.  Keep the remote catalog URL.
            cacheRequest = Promise.resolve(null);
        }
    }

    // 如果生成流程刚刚发起过同一批缓存请求，结果页必须等待它们完成，
    // 否则首次渲染会错过缓存完成时机，头像节点被移除后就不会再回来。
    const requests = [...new Set([...inFlightRequests, cacheRequest].filter(Boolean))];
    if (!requests.length) return Promise.resolve(null);
    return Promise.all(requests).then(results => results.find(Boolean) || null);
}

function getResultVoiceEntry(key) {
    const voice = getVoiceEntry(key);
    const normalizedKey = canonicalVoiceKey(voice.key || key);
    const useCachedAssets = voiceAssetCacheReady.has(normalizedKey);
    return {
        ...voice,
        // 缓存完成前直接使用目录中的远程资源，避免把尚未生成的本地地址
        // 当成首选地址；缓存完成后再切换到本地资源，减少结果页的网络依赖。
        img_url: voice.img_url
            ? (useCachedAssets ? voiceAssetUrl(normalizedKey, 'avatar') : voice.img_url)
            : '',
        fallback_img_url: useCachedAssets ? voice.img_url : '',
        audio_url: voice.audio_url
            ? (useCachedAssets ? voiceAssetUrl(normalizedKey, 'sample') : voice.audio_url)
            : '',
        fallback_audio_url: useCachedAssets ? voice.audio_url : '',
    };
}

function voiceDisplayName(key) {
    return getVoiceEntry(key).name;
}

function getVoiceInitials(name) {
    const value = String(name || '?').trim();
    const ascii = value.match(/[A-Za-z0-9]/g);
    if (ascii?.length) return ascii.slice(0, 2).join('').toUpperCase();
    return value.slice(0, 1) || '?';
}

function renderVoiceAvatar(container, voice, large = false, eager = false) {
    if (!container) return;
    container.replaceChildren();
    container.classList.toggle('voice-avatar-large', large);
    container.classList.remove('has-image');
    const fallback = document.createElement('span');
    fallback.textContent = getVoiceInitials(voice?.name);
    container.appendChild(fallback);
    const sources = [...new Set([voice?.img_url, voice?.fallback_img_url].filter(Boolean))];
    if (!sources.length) return;
    const image = document.createElement('img');
    image.alt = '';
    const loadImmediately = large || eager;
    image.loading = loadImmediately ? 'eager' : 'lazy';
    image.decoding = 'async';
    image.hidden = loadImmediately;
    image.addEventListener('load', () => {
        fallback.hidden = true;
        image.hidden = false;
        container.classList.add('has-image');
    }, { once: true });
    image.addEventListener('error', () => {
        const fallbackSrc = image.dataset.fallbackSrc;
        if (fallbackSrc) {
            delete image.dataset.fallbackSrc;
            image.src = fallbackSrc;
            return;
        }
        image.remove();
        container.classList.remove('has-image');
    });
    container.appendChild(image);
    if (loadImmediately) {
        image.src = sources[0];
        if (sources[1]) image.dataset.fallbackSrc = sources[1];
    } else {
        image.dataset.src = sources[0];
        if (sources[1]) image.dataset.fallbackSrc = sources[1];
    }
}

function observeVoiceAvatars() {
    if (voiceAvatarObserver) {
        voiceAvatarObserver.disconnect();
        voiceAvatarObserver = null;
    }
    const grid = $('voice-browser-grid');
    if (!grid) return;
    const lazyImages = [...grid.querySelectorAll('img[data-src]')];
    if (!lazyImages.length) return;

    const activate = image => {
        const src = image.dataset.src;
        if (!src) return;
        image.hidden = true;
        image.loading = 'eager';
        image.src = src;
        delete image.dataset.src;
    };
    if (!('IntersectionObserver' in window)) {
        lazyImages.forEach(activate);
        return;
    }
    voiceAvatarObserver = new IntersectionObserver(entries => {
        entries.forEach(entry => {
            if (!entry.isIntersecting) return;
            activate(entry.target);
            voiceAvatarObserver?.unobserve(entry.target);
        });
    }, { root: grid, rootMargin: '120px 0px' });
    lazyImages.forEach(image => voiceAvatarObserver.observe(image));
}

function getRecentVoiceKeys() {
    try {
        const raw = JSON.parse(rendererStorage?.getItem(VOICE_RECENT_STORAGE_KEY) || '[]');
        return Array.isArray(raw)
            ? [...new Set(raw.map(key => canonicalVoiceKey(key)).filter(Boolean))].slice(0, 12)
            : [];
    } catch (_) {
        return [];
    }
}

function rememberVoiceUse(key) {
    const normalizedKey = canonicalVoiceKey(key);
    if (!normalizedKey) return;
    const recent = [normalizedKey, ...getRecentVoiceKeys().filter(item => item !== normalizedKey)].slice(0, 12);
    try {
        rendererStorage?.setItem(VOICE_RECENT_STORAGE_KEY, JSON.stringify(recent));
    } catch (_) {
        // localStorage 不可用时不影响当前音色选择。
    }
}

function setVoiceCatalog(entries, filters = [], aliases = {}) {
    voiceAliasMap = Object.fromEntries(
        Object.entries(aliases && typeof aliases === 'object' ? aliases : {})
            .slice(0, 4096)
            .map(([alias, target]) => [normalizeVoiceKey(alias), normalizeVoiceKey(target)])
            .filter(([alias, target]) => alias && target && alias !== target),
    );
    const normalized = Array.isArray(entries) ? entries.map(normalizeVoiceEntry) : [];
    const byKey = new Map();
    [...normalized, ...voiceCatalog].forEach(voice => {
        if (voice?.key && !byKey.has(voice.key)) byKey.set(voice.key, voice);
    });
    voiceCatalog = [...byKey.values()];
    const filterMap = new Map([['all', { key: 'all', label: '全部音色' }]]);
    if (Array.isArray(filters)) {
        filters.forEach(filter => {
            const key = String(filter?.key || '').trim();
            const label = String(filter?.label || '').trim();
            if (key && label && key !== 'all') filterMap.set(key, { key, label, count: filter.count });
        });
    }
    if (!filterMap.has('female')) filterMap.set('female', { key: 'female', label: '女声' });
    if (!filterMap.has('male')) filterMap.set('male', { key: 'male', label: '男声' });
    filterMap.set('recent', { key: 'recent', label: '最近使用' });
    const priorityFilters = ['英语', '多语种']
        .map(label => [...filterMap.values()].find(filter => filter.label === label))
        .filter(Boolean);
    const priorityKeys = new Set(priorityFilters.map(filter => filter.key));
    voiceFilterOptions = [
        filterMap.get('all'),
        ...priorityFilters,
        filterMap.get('recent'),
        ...[...filterMap.values()].filter(filter => (
            filter.key !== 'all'
            && filter.key !== 'recent'
            && !priorityKeys.has(filter.key)
        )),
    ].filter(Boolean);
}

function getVoiceFilterOptions() {
    return voiceFilterOptions.map(filter => ({ ...filter }));
}

function migrateVoiceSelections() {
    let changed = false;
    const migrate = value => {
        const normalized = canonicalVoiceKey(value);
        if (normalized !== normalizeVoiceKey(value)) changed = true;
        return normalized;
    };
    const female = migrate(selectedDefaultFemaleVoice);
    const male = migrate(selectedDefaultMaleVoice);
    if (female) selectedDefaultFemaleVoice = female;
    if (male) selectedDefaultMaleVoice = male;

    const migratedRoles = {};
    Object.entries(roleVoiceMap || {}).forEach(([role, key]) => {
        const normalizedRole = normalizeRoleKeyClient(role);
        const migratedKey = migrate(key);
        if (normalizedRole && migratedKey) migratedRoles[normalizedRole] = migratedKey;
    });
    if (JSON.stringify(migratedRoles) !== JSON.stringify(roleVoiceMap || {})) changed = true;
    roleVoiceMap = migratedRoles;

    try {
        const raw = JSON.parse(rendererStorage?.getItem(VOICE_RECENT_STORAGE_KEY) || '[]');
        const recent = Array.isArray(raw)
            ? [...new Set(raw.map(key => canonicalVoiceKey(key)).filter(Boolean))].slice(0, 12)
            : [];
        if (JSON.stringify(recent) !== JSON.stringify(raw)) {
            rendererStorage?.setItem(VOICE_RECENT_STORAGE_KEY, JSON.stringify(recent));
            changed = true;
        }
    } catch (_) {
        // localStorage 不可用时不影响当前音色迁移。
    }
    return changed;
}

function roleLooksLikeLabel(label) {
    const value = String(label || '').trim();
    return Boolean(value)
        && value.length <= 48
        && value.split(/\s+/).length <= 4
        && !/^\d/.test(value)
        && !value.includes('://')
        && !/[\\/.,!?。！？；;，,]/.test(value);
}

function inferRoleVoice(label) {
    const value = String(label || '').trim().toLocaleLowerCase('en-US');
    if (/^(mr|mr\.|sir|男|先生)\b/.test(value)) return selectedDefaultMaleVoice;
    return selectedDefaultFemaleVoice;
}

/**
 * 从后端已经解析完成的完整文档结果中提取角色名。
 *
 * 一个文档可能包含多个题型、多个录音稿条目；不能只看第一条 item。
 * 这里只识别录音稿行首的「角色名: 内容」形式，W/M 标记交给音频解析器，
 * 避免把普通正文中的冒号误当成可配置角色。新版 7 上规则会把一个角色
 * 单独拆成一个音频，因此解析器同时提供可信的 item.role 元数据。
 */
function extractParsedRoleLabels(parseResults) {
    const labels = [];
    const seen = new Set();
    if (!Array.isArray(parseResults)) return labels;

    parseResults.forEach(result => {
        if (!Array.isArray(result?.items)) return;
        result.items.forEach(item => {
            const explicitRoles = [];
            if (typeof item?.role === 'string' && item.role.trim()) {
                explicitRoles.push(item.role.trim());
            }
            if (Array.isArray(item?.roles)) {
                item.roles.forEach(role => {
                    if (typeof role === 'string' && role.trim()) explicitRoles.push(role.trim());
                });
            }
            explicitRoles.forEach(label => {
                if (!roleLooksLikeLabel(label)) return;
                const key = normalizeRoleKeyClient(label);
                if (!key || seen.has(key)) return;
                seen.add(key);
                labels.push({ key, label });
            });

            const lines = String(item?.text || '').split(/\r?\n/);
            const itemLabels = [];
            const itemSeen = new Set();
            lines.forEach(line => {
                const value = line.trim();
                if (!value || /^[WwMm]\s*[:：]/.test(value) || /^\([WwMm]\)/.test(value)) return;
                const match = /^([^:：\n]{1,60}?)\s*[:：]\s*(.*)$/.exec(value);
                const label = match?.[1]?.trim() || '';
                if (!match || !roleLooksLikeLabel(label)) return;
                const key = normalizeRoleKeyClient(label);
                if (!key || itemSeen.has(key)) return;
                itemSeen.add(key);
                itemLabels.push({ key, label });
            });
            // 没有可信 role 元数据时，至少出现两个不同的行首角色名，
            // 才把该 item 视为对话题；单独一行的普通说明不进入角色配置。
            if (itemLabels.length < 2) return;
            itemLabels.forEach(role => {
                if (seen.has(role.key)) return;
                seen.add(role.key);
                labels.push(role);
            });
        });
    });
    return labels;
}

function discoverVoiceRoles(parseResults = currentSession?.parse_results) {
    const roles = [
        { key: DEFAULT_FEMALE_ROLE_KEY, label: '默认女声', kind: 'default-female' },
        { key: DEFAULT_MALE_ROLE_KEY, label: '默认男声', kind: 'default-male' },
    ];
    const parsedRoles = extractParsedRoleLabels(parseResults);
    const currentRoleKeys = new Set(parsedRoles.map(role => role.key));
    const hasDocumentContext = Array.isArray(parseResults);

    // 角色映射属于当前文档；切换文档后不把上一份文档的角色配置继续提交。
    // 没有导入文档时先保留本地预设中的角色配置，避免初始化渲染把它们清掉。
    if (hasDocumentContext) {
        roleVoiceMap = Object.fromEntries(
            Object.entries(roleVoiceMap).filter(([key]) => currentRoleKeys.has(key)),
        );
    }
    parsedRoles.forEach(({ key, label }) => {
        if (!roleVoiceMap[key]) roleVoiceMap[key] = inferRoleVoice(label);
        roles.push({ key, label, kind: 'role' });
    });
    voiceRoles = roles;
    const validParamKeys = new Set(roles.map(roleConfigKeyForRole));
    if (hasDocumentContext) {
        voiceParamConfigs = Object.fromEntries(
            Object.entries(voiceParamConfigs).filter(([key]) => validParamKeys.has(key)),
        );
    }
    roles.forEach(role => {
        const configKey = roleConfigKeyForRole(role);
        if (!voiceParamConfigs[configKey]) voiceParamConfigs[configKey] = createDefaultVoiceParams(configKey);
    });
    if (!roles.some(role => role.key === activeVoiceRole)) activeVoiceRole = roles[0].key;
    const roleCount = $('voice-role-count');
    if (roleCount) roleCount.textContent = parsedRoles.length > 0
        ? `${parsedRoles.length} 个角色`
        : '2 个默认角色';
    return roles;
}

function resetTaskVoiceConfiguration() {
    stopVoicePreview();
    roleVoiceMap = {};
    voiceParamConfigs = {
        [DEFAULT_FEMALE_ROLE_KEY]: normalizeVoiceParams(
            voiceParamConfigs[DEFAULT_FEMALE_ROLE_KEY],
            DEFAULT_FEMALE_VOICE_PARAMS,
        ),
        [DEFAULT_MALE_ROLE_KEY]: normalizeVoiceParams(
            voiceParamConfigs[DEFAULT_MALE_ROLE_KEY],
            DEFAULT_MALE_VOICE_PARAMS,
        ),
    };
    activeVoiceRole = DEFAULT_FEMALE_ROLE_KEY;
    voiceRoles = [];
    setVoiceDetailCollapsed(false);
}

function activeVoiceKeyForRole(role = voiceRoles.find(item => item.key === activeVoiceRole)) {
    if (role?.kind === 'default-male') return selectedDefaultMaleVoice;
    if (role?.kind === 'default-female') return selectedDefaultFemaleVoice;
    return roleVoiceMap[role?.key || activeVoiceRole] || selectedDefaultFemaleVoice;
}

function activeVoiceParams() {
    const role = voiceRoles.find(item => item.key === activeVoiceRole);
    const configKey = roleConfigKeyForRole(role || activeVoiceRole);
    if (!voiceParamConfigs[configKey]) voiceParamConfigs[configKey] = createDefaultVoiceParams(configKey);
    return voiceParamConfigs[configKey];
}

function renderRoleList() {
    const container = $('voice-role-list');
    if (!container) return;
    container.replaceChildren();
    voiceRoles.forEach(role => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `voice-role-item${role.key === activeVoiceRole ? ' is-active' : ''}${role.kind === 'default-male' ? ' is-male' : ''}`;
        button.dataset.roleKey = role.key;
        button.setAttribute('aria-pressed', role.key === activeVoiceRole ? 'true' : 'false');

        const mark = document.createElement('span');
        mark.className = 'voice-role-mark';
        mark.textContent = role.kind === 'default-female' ? '女' : role.kind === 'default-male' ? '男' : getVoiceInitials(role.label);
        const copy = document.createElement('span');
        copy.className = 'voice-role-copy';
        const name = document.createElement('strong');
        name.textContent = role.label;
        const voice = document.createElement('small');
        voice.textContent = voiceDisplayName(activeVoiceKeyForRole(role));
        copy.append(name, voice);
        button.append(mark, copy);
        container.appendChild(button);
    });
}

function voiceSearchText(voice) {
    return voice.search_text || [voice.name, voice.gender_label, ...(voice.language || []), ...(voice.tags || []), ...(voice.categories || [])]
        .join(' ').toLocaleLowerCase('zh-CN');
}

function voiceMatchesFilter(voice, filterKey) {
    if (!filterKey || filterKey === 'all') return true;
    if (filterKey === 'recent') return getRecentVoiceKeys().includes(voice.key);
    if (filterKey === 'female' || filterKey === 'male') return voice.gender === filterKey;
    const label = filterKey.startsWith('tag:') ? filterKey.slice(4) : filterKey;
    return [...(voice.categories || []), ...(voice.tags || []), ...(voice.language || [])].includes(label);
}

function voiceTags(voice, limit = 2) {
    const values = [voice.gender_label, ...(voice.tags || []), ...(voice.language || []), ...(voice.categories || [])];
    return [...new Set(values.filter(Boolean))].slice(0, limit);
}

function voiceHasPreview(voice) {
    return Boolean(String(voice?.audio_url || voice?.fallback_audio_url || '').trim());
}

function createVoicePreviewButton(voice) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'voice-entry-preview';
    button.dataset.voicePreviewKey = voice.key;
    button.dataset.audioName = `音色 ${voice.name}`;
    const hasPreview = voiceHasPreview(voice);
    button.disabled = !hasPreview;
    button.title = hasPreview ? `试听 ${voice.name}` : `${voice.name}暂无示例音频`;
    button.setAttribute('aria-label', hasPreview ? `试听 ${voice.name}` : `${voice.name}暂无示例音频`);
    button.innerHTML = '<svg class="icon-play" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg><svg class="icon-pause" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style="display:none"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
    button.setAttribute('aria-pressed', 'false');
    return button;
}

function renderVoiceFilters() {
    const container = $('voice-filter-row');
    if (!container) return;
    const overflow = $('voice-filter-overflow');
    const isPrimaryFilter = filter => filter.key === 'all'
        || filter.key === 'female'
        || filter.key === 'male'
        || filter.key === 'recent'
        || ['英语', '多语种'].includes(filter.label);
    const primaryFilters = voiceFilterOptions.filter(isPrimaryFilter);
    const secondaryFilters = voiceFilterOptions.filter(filter => !isPrimaryFilter(filter));
    const activeSecondary = secondaryFilters.some(filter => filter.key === activeVoiceFilter);
    const expanded = voiceFiltersExpanded || activeSecondary;
    const createFilterButton = filter => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `voice-filter-chip${filter.key === activeVoiceFilter ? ' is-active' : ''}`;
        button.dataset.voiceFilter = filter.key;
        button.setAttribute('aria-pressed', filter.key === activeVoiceFilter ? 'true' : 'false');
        button.textContent = filter.label;
        return button;
    };

    container.replaceChildren(...primaryFilters.map(createFilterButton));
    if (secondaryFilters.length > 0) {
        const moreButton = document.createElement('button');
        moreButton.type = 'button';
        moreButton.className = `voice-filter-chip voice-filter-more${expanded ? ' is-expanded' : ''}`;
        moreButton.dataset.voiceFilterMore = 'true';
        moreButton.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        moreButton.textContent = expanded ? '收起更多' : `更多筛选 · ${secondaryFilters.length}`;
        container.appendChild(moreButton);
    }

    if (overflow) {
        overflow.replaceChildren(...secondaryFilters.map(createFilterButton));
        overflow.hidden = !expanded || secondaryFilters.length === 0;
    }
}

function renderVoiceCards() {
    const grid = $('voice-browser-grid');
    const empty = $('voice-browser-empty');
    if (!grid || !empty) return;
    const query = String($('voice-search-input')?.value || '').trim().toLocaleLowerCase('zh-CN');
    const selectedKey = activeVoiceKeyForRole();
    const matches = voiceCatalog.filter(voice => voiceMatchesFilter(voice, activeVoiceFilter) && (!query || voiceSearchText(voice).includes(query)));
    if (voiceAvatarObserver) {
        voiceAvatarObserver.disconnect();
        voiceAvatarObserver = null;
    }
    const fragment = document.createDocumentFragment();
    matches.forEach((voice, index) => {
        const card = document.createElement('div');
        card.className = `voice-entry${voice.key === selectedKey ? ' is-selected' : ''}`;
        card.dataset.voiceKey = voice.key;
        card.setAttribute('role', 'listitem');
        card.setAttribute('aria-label', `${voice.name}，${voiceHasPreview(voice) ? '可试听' : '暂无示例音频'}`);
        const selectButton = document.createElement('button');
        selectButton.type = 'button';
        selectButton.className = 'voice-entry-select';
        selectButton.setAttribute('aria-pressed', voice.key === selectedKey ? 'true' : 'false');
        selectButton.setAttribute('aria-label', `选择音色 ${voice.name}`);
        const avatar = document.createElement('span');
        avatar.className = 'voice-avatar';
        // 首屏卡片直接加载，滚动到后续音色时再按可见区域加载，避免
        // 387 个音色同时请求头像而又保证当前列表不会只显示首字母。
        renderVoiceAvatar(avatar, voice, false, index < 20);
        const copy = document.createElement('span');
        copy.className = 'voice-entry-copy';
        const name = document.createElement('strong');
        name.textContent = voice.name;
        const tags = document.createElement('span');
        tags.className = 'voice-entry-tags';
        voiceTags(voice).forEach((tag, index) => {
            const tagEl = document.createElement('span');
            if (index === 0) tagEl.classList.add('is-gender');
            tagEl.textContent = tag;
            tags.appendChild(tagEl);
        });
        copy.append(name, tags);
        selectButton.append(avatar, copy);
        const actions = document.createElement('span');
        actions.className = 'voice-entry-actions';
        actions.appendChild(createVoicePreviewButton(voice));
        card.append(selectButton, actions);
        fragment.appendChild(card);
    });
    // 一次性挂载，避免每个音色卡片都触发布局计算。
    grid.replaceChildren(fragment);
    observeVoiceAvatars();
    empty.hidden = matches.length > 0;
}

function scheduleVoiceCardsRender() {
    if (voiceCardsRenderFrame !== null) return;
    const schedule = window.requestAnimationFrame
        ? callback => window.requestAnimationFrame(callback)
        : callback => window.setTimeout(callback, 0);
    voiceCardsRenderFrame = schedule(() => {
        voiceCardsRenderFrame = null;
        renderVoiceCards();
    });
}

function renderRecentVoiceList() {
    const list = $('voice-recent-list');
    const empty = $('voice-recent-empty');
    const count = $('voice-recent-count');
    if (!list || !empty || !count) return;
    const recent = getRecentVoiceKeys();
    count.textContent = String(recent.length);
    list.replaceChildren();
    recent.slice(0, 6).forEach(key => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'voice-recent-chip';
        button.dataset.recentVoiceKey = key;
        button.textContent = voiceDisplayName(key);
        list.appendChild(button);
    });
    empty.hidden = recent.length > 0;
}

function setVoiceParamInputs(params) {
    ['rate', 'pitch', 'volume'].forEach(param => {
        const value = clampParamValue(params?.[param]);
        const range = $(`voice-${param}`);
        const number = $(`voice-${param}-number`);
        if (range) range.value = String(value);
        if (number) number.value = String(value);
    });
}

function renderVoiceDetails() {
    const role = voiceRoles.find(item => item.key === activeVoiceRole);
    const voice = getResultVoiceEntry(activeVoiceKeyForRole(role));
    const detailRole = $('voice-detail-role');
    const detailName = $('voice-detail-name');
    const detailMeta = $('voice-detail-meta');
    if (detailRole) detailRole.textContent = role?.label || '当前角色';
    if (detailName) detailName.textContent = voice.name;
    if (detailMeta) detailMeta.textContent = [...(voice.language || []), voice.gender_label].filter(Boolean).join(' · ') || voice.gender_label;
    renderVoiceAvatar($('voice-detail-avatar'), voice, true);
    setVoiceParamInputs(activeVoiceParams());
    const previewButton = $('voice-preview-btn');
    if (previewButton) {
        const hasPreview = voiceHasPreview(voice);
        previewButton.disabled = !hasPreview;
        previewButton.title = hasPreview ? `试听 ${voice.name}` : `${voice.name}暂无示例音频`;
        previewButton.setAttribute('aria-label', hasPreview ? `试听 ${voice.name}` : `${voice.name}暂无示例音频`);
        previewButton.dataset.audioName = `音色 ${voice.name}`;
        setVoicePreviewButtonState(
            voicePreviewAudio?._previewButton?.id === 'voice-preview-btn',
        );
    }
}

function renderVoiceWorkspace() {
    discoverVoiceRoles();
    renderRoleList();
    renderVoiceFilters();
    renderVoiceCards();
    renderVoiceDetails();
    renderRecentVoiceList();
    setVoiceDetailCollapsed(isVoiceDetailCollapsed);
}

function selectVoiceForActiveRole(key) {
    const normalizedKey = canonicalVoiceKey(key);
    if (!normalizedKey) return;
    stopVoicePreview();
    const role = voiceRoles.find(item => item.key === activeVoiceRole);
    if (role?.kind === 'default-male') selectedDefaultMaleVoice = normalizedKey;
    else if (role?.kind === 'default-female') selectedDefaultFemaleVoice = normalizedKey;
    else roleVoiceMap[activeVoiceRole] = normalizedKey;
    const configKey = roleConfigKeyForRole(role || activeVoiceRole);
    if (!voiceParamConfigs[configKey]) voiceParamConfigs[configKey] = createDefaultVoiceParams(configKey);
    rememberVoiceUse(normalizedKey);
    void queueVoiceAssetCache(normalizedKey);
    renderVoiceWorkspace();
    updateConfigSummary();
    rememberCurrentConfig();
}

function updateActiveVoiceParam(param, value) {
    const params = activeVoiceParams();
    params[param] = clampParamValue(value);
    setVoiceParamInputs(params);
    updateConfigSummary();
    rememberCurrentConfig();
}

function setResultVoicePreviewButtonState(button, isPlaying) {
    if (!button) return;
    button.classList.toggle('is-playing', Boolean(isPlaying));
    button.setAttribute('aria-pressed', isPlaying ? 'true' : 'false');
    updatePlayIcon(button, Boolean(isPlaying));
}

function stopVoicePreview() {
    const audio = voicePreviewAudio;
    if (audio) {
        const triggerButton = audio._previewButton;
        audio.pause();
        try { audio.currentTime = 0; } catch (_) { /* ignore */ }
        voicePreviewAudio = null;
        if (triggerButton?.id === 'voice-preview-btn') {
            setVoicePreviewButtonState(false);
        } else {
            setResultVoicePreviewButtonState(triggerButton, false);
        }
    }
    setVoicePreviewButtonState(false);
}

function setVoicePreviewButtonState(isPlaying) {
    const button = $('voice-preview-btn');
    if (!button) return;
    button.classList.toggle('is-playing', Boolean(isPlaying));
    button.setAttribute('aria-pressed', isPlaying ? 'true' : 'false');
    updatePlayIcon(button, Boolean(isPlaying));
}

function stopGeneratedAudioPlayback() {
    audioPlayRequestToken++;
    audioElements.forEach(audio => {
        audio._playRequestToken = 0;
        audio.pause();
        if (audio._playButton) {
            audio._playButton.classList.remove('is-buffering');
            updatePlayIcon(audio._playButton, false);
        }
    });
    currentPlayingAudio = null;
}

function playVoiceSample(voice, triggerButton) {
    const localSource = String(voice?.audio_url || '').trim();
    const fallbackSource = String(voice?.fallback_audio_url || '').trim();
    const source = localSource || fallbackSource;
    if (!source || !triggerButton) return;

    if (
        voicePreviewAudio
        && voicePreviewAudio._previewButton === triggerButton
        && voicePreviewAudio._previewKey === voice?.key
    ) {
        stopVoicePreview();
        return;
    }

    stopGeneratedAudioPlayback();
    stopVoicePreview();
    const audio = new Audio(source);
    audio.preload = 'auto';
    audio._previewButton = triggerButton;
    audio._previewKey = voice?.key || source;
    audio._previewSource = source;
    audio._previewFallbackTried = !localSource || !fallbackSource || localSource === fallbackSource;
    voicePreviewAudio = audio;
    let playbackAttempt = 0;

    const setPlaying = isPlaying => {
        if (triggerButton.id === 'voice-preview-btn') setVoicePreviewButtonState(isPlaying);
        else setResultVoicePreviewButtonState(triggerButton, isPlaying);
    };
    const finish = () => {
        if (voicePreviewAudio !== audio) return;
        stopVoicePreview();
    };
    const handleFailure = (attempt, attemptedSource) => {
        if (voicePreviewAudio !== audio) return;
        if (attempt !== playbackAttempt || attemptedSource !== audio.src) return;
        if (!audio._previewFallbackTried && fallbackSource && fallbackSource !== attemptedSource) {
            audio._previewFallbackTried = true;
            audio.src = fallbackSource;
            const resolvedFallbackSource = audio.src;
            playbackAttempt += 1;
            const fallbackAttempt = playbackAttempt;
            void audio.play().catch(() => handleFailure(fallbackAttempt, resolvedFallbackSource));
            return;
        }
        finish();
        showToast('当前音色试听暂时不可用', 'warning');
    };

    setPlaying(true);
    audio.addEventListener('ended', finish, { once: true });
    audio.addEventListener('error', () => handleFailure(playbackAttempt, audio.src));
    playbackAttempt += 1;
    const initialAttempt = playbackAttempt;
    const resolvedInitialSource = audio.src;
    void audio.play().catch(() => handleFailure(initialAttempt, resolvedInitialSource));
}

function setVoiceDetailCollapsed(collapsed) {
    isVoiceDetailCollapsed = Boolean(collapsed);
    const layout = document.querySelector('.voice-browser-layout');
    const panel = $('voice-detail-panel');
    const button = $('voice-detail-toggle');
    layout?.classList.toggle('is-detail-collapsed', isVoiceDetailCollapsed);
    panel?.classList.toggle('is-collapsed', isVoiceDetailCollapsed);
    if (button) {
        button.setAttribute('aria-expanded', isVoiceDetailCollapsed ? 'false' : 'true');
        button.setAttribute('aria-label', isVoiceDetailCollapsed ? '展开详细配置' : '收起详细配置');
        button.title = isVoiceDetailCollapsed ? '展开详细配置' : '收起详细配置';
        const label = button.querySelector('.voice-detail-toggle-label');
        if (label) {
            label.textContent = isVoiceDetailCollapsed
                ? '展开详细配置'
                : '收起详细配置';
        }
    }
}

function toggleVoicePreview() {
    const voice = getResultVoiceEntry(activeVoiceKeyForRole());
    if (!voiceHasPreview(voice)) return;
    playVoiceSample(voice, $('voice-preview-btn'));
}

function bindVoiceWorkspaceEvents() {
    $('voice-search-input')?.addEventListener('input', () => {
        stopVoicePreview();
        scheduleVoiceCardsRender();
    });
    const handleVoiceFilterClick = event => {
        const moreButton = event.target.closest('[data-voice-filter-more]');
        if (moreButton) {
            voiceFiltersExpanded = moreButton.getAttribute('aria-expanded') !== 'true';
            renderVoiceFilters();
            return;
        }
        const button = event.target.closest('[data-voice-filter]');
        if (!button) return;
        stopVoicePreview();
        activeVoiceFilter = button.dataset.voiceFilter || 'all';
        renderVoiceFilters();
        renderVoiceCards();
    };
    $('voice-filter-row')?.addEventListener('click', handleVoiceFilterClick);
    $('voice-filter-overflow')?.addEventListener('click', handleVoiceFilterClick);
    $('voice-role-list')?.addEventListener('click', event => {
        const button = event.target.closest('[data-role-key]');
        if (!button) return;
        stopVoicePreview();
        activeVoiceRole = button.dataset.roleKey || '__default_female__';
        renderVoiceWorkspace();
    });
    $('voice-browser-grid')?.addEventListener('click', event => {
        const previewButton = event.target.closest('[data-voice-preview-key]');
        if (previewButton) {
            const voice = getResultVoiceEntry(previewButton.dataset.voicePreviewKey);
            if (voiceHasPreview(voice)) playVoiceSample(voice, previewButton);
            return;
        }
        const button = event.target.closest('[data-voice-key]');
        if (button) selectVoiceForActiveRole(button.dataset.voiceKey);
    });
    $('voice-recent-list')?.addEventListener('click', event => {
        const button = event.target.closest('[data-recent-voice-key]');
        if (button) selectVoiceForActiveRole(button.dataset.recentVoiceKey);
    });
    $('voice-preview-btn')?.addEventListener('click', toggleVoicePreview);
    $('voice-detail-toggle')?.addEventListener('click', () => {
        setVoiceDetailCollapsed(!isVoiceDetailCollapsed);
    });
    ['rate', 'pitch', 'volume'].forEach(param => {
        const range = $(`voice-${param}`);
        const number = $(`voice-${param}-number`);
        range?.addEventListener('input', () => updateActiveVoiceParam(param, range.value));
        number?.addEventListener('input', () => updateActiveVoiceParam(param, number.value));
        number?.addEventListener('blur', () => updateActiveVoiceParam(param, number.value));
    });
    $$('[data-voice-param-reset]').forEach(button => {
        button.addEventListener('click', () => updateActiveVoiceParam(button.dataset.voiceParamReset, 50));
    });
}

function setSelectValue(selectEl, value, defaultValue) {
    const str = String(value ?? defaultValue);
    // 精确匹配
    for (const opt of selectEl.options) {
        if (opt.value === str) {
            selectEl.value = str;
            window.WordTTSUI?.syncSelect(selectEl);
            return;
        }
    }
    // 数值匹配（处理 1.0 → "1" vs "1.0" 等情况）
    const num = parseFloat(str);
    if (!isNaN(num)) {
        for (const opt of selectEl.options) {
            if (parseFloat(opt.value) === num) {
                selectEl.value = opt.value;
                window.WordTTSUI?.syncSelect(selectEl);
                return;
            }
        }
    }
    window.WordTTSUI?.syncSelect(selectEl);
}

/**
 * 将配置应用到 Step 2 表单。
 */
function applyConfigToForm(config, { includeRoles = true } = {}) {
    if (!config) return;
    stopVoicePreview();
    clientConfigInitialized = true;
    const normalized = normalizeClientConfig(config);
    const existingTaskRoleVoiceMap = currentSession ? { ...roleVoiceMap } : {};
    const existingTaskRoleConfigs = currentSession
        ? Object.fromEntries(
            Object.entries(voiceParamConfigs).filter(([key]) => (
                key !== DEFAULT_FEMALE_ROLE_KEY && key !== DEFAULT_MALE_ROLE_KEY
            )),
        )
        : {};
    selectedDefaultFemaleVoice = normalized.default_female_voice;
    selectedDefaultMaleVoice = normalized.default_male_voice;
    updateGenerationModeUI(normalized.generation_mode);
    const defaultRoleConfigs = {
        [DEFAULT_FEMALE_ROLE_KEY]: normalizeVoiceParams(
            normalized.role_configs?.[DEFAULT_FEMALE_ROLE_KEY],
            DEFAULT_FEMALE_VOICE_PARAMS,
        ),
        [DEFAULT_MALE_ROLE_KEY]: normalizeVoiceParams(
            normalized.role_configs?.[DEFAULT_MALE_ROLE_KEY],
            DEFAULT_MALE_VOICE_PARAMS,
        ),
    };
    if (includeRoles) {
        voiceParamConfigs = Object.fromEntries(
            Object.entries(normalized.role_configs || {}).map(([key, value]) => [key, { ...value }]),
        );
        roleVoiceMap = { ...(normalized.role_voices || {}) };
    } else {
        // 预设/页面恢复只覆盖默认男女声；当前文档角色始终由用户自己维护。
        voiceParamConfigs = { ...existingTaskRoleConfigs, ...defaultRoleConfigs };
        roleVoiceMap = existingTaskRoleVoiceMap;
    }
    voiceParamConfigs[DEFAULT_FEMALE_ROLE_KEY] ||= createDefaultVoiceParams(DEFAULT_FEMALE_ROLE_KEY);
    voiceParamConfigs[DEFAULT_MALE_ROLE_KEY] ||= createDefaultVoiceParams(DEFAULT_MALE_ROLE_KEY);
    renderVoiceWorkspace();
    setSelectValue($('format'), normalized.format, 'mp3');
    setSelectValue($('quality'), normalized.quality, '128 kbps（标准）');
    $('preview').checked = normalized.preview;
    enforceOutputCompatibility();
    rememberCurrentConfig();
}

/**
 * 刷新所有预设 UI。
 */
function refreshPresetUI() {
    renderStep2PresetSelect();
}

/**
 * 保存当前表单配置为预设。
 */
async function handleSavePreset() {
    const config = collectPersistedConfig();
    const name = await showPromptDialog('保存配置', '请输入配置名称：', `配置 ${new Date().toLocaleDateString('zh-CN')}`);
    if (!name || !name.trim()) return;

    const presets = loadPresets();
    const preset = {
        id: `preset_${Date.now()}`,
        name: name.trim(),
        config: config,
        created_at: Date.now(),
    };
    presets.push(preset);
    if (!savePresets(presets)) return;
    refreshPresetUI();

    // 选中新保存的预设
    const select = $('preset-select');
    if (select) {
        select.value = preset.id;
        window.WordTTSUI?.syncSelect(select);
    }

    showToast(`已保存配置「${preset.name}」`);
}

/**
 * 应用选中的预设到表单。
 */
function handleApplyPreset() {
    const select = $('preset-select');
    const presetId = select ? select.value : '';
    if (!presetId) {
        showToast('请先选择一个配置');
        return;
    }
    const presets = loadPresets();
    const preset = presets.find(p => p.id === presetId);
    if (!preset) {
        showToast('配置不存在');
        return;
    }
    applyConfigToForm(preset.config, { includeRoles: false });
    showToast(`已应用配置「${preset.name}」`);
}

/**
 * 删除选中的预设。
 */
async function handleDeletePreset() {
    const select = $('preset-select');
    const presetId = select ? select.value : '';
    if (!presetId) {
        showToast('请先选择一个配置');
        return;
    }
    const presets = loadPresets();
    const preset = presets.find(p => p.id === presetId);
    if (!preset) {
        showToast('配置不存在，可能已被删除');
        return;
    }

    const confirmed = await showConfirmDialog({
        kicker: '配置管理',
        title: '删除这个配置？',
        message: `「${preset.name}」将从已保存配置中移除。`,
        detail: '此操作不会影响已经生成的音频，但删除后无法恢复。',
        tone: 'danger',
        confirmLabel: '删除配置',
    });
    if (!confirmed) return;

    const filtered = presets.filter(p => p.id !== presetId);
    if (!savePresets(filtered)) return;
    refreshPresetUI();
    showToast(`已删除配置「${preset.name}」`);
}

/**
 * 启动生成。useDefaults=true 使用默认配置；presetConfig 不为空时直接使用该配置。
 */
async function startProcessing(useDefaults, presetConfig, itemIds = null) {
    if (isRestarting) return;
    if (!currentSession) {
        showToast('当前文档会话已失效，请重新导入文档');
        goToStep(1);
        return;
    }
    if (isGenerating || generationStartInFlight) return;

    // A completed preview (or a completed failed run) is immutable.  When
    // the user changes its settings, this function may replace the session
    // with a durable rerun before patching the new configuration.
    let session = currentSession;
    const config = normalizeClientConfig(presetConfig || collectConfig(useDefaults));
    let requestedItemIds = Array.isArray(itemIds)
        ? [...new Set(itemIds.map((itemId) => String(itemId || '').trim()).filter(Boolean))]
        : null;
    if (requestedItemIds && requestedItemIds.length === 0) requestedItemIds = null;
    updateGenerationModeUI(config.generation_mode);
    const sourceTotal = summarizeParseResults(session.parse_results).total;
    let isPreviewScope = !requestedItemIds && Boolean(config.preview && sourceTotal > 3);
    let generationTotal = requestedItemIds
        ? requestedItemIds.length
        : (isPreviewScope ? Math.min(sourceTotal, 3) : sourceTotal);
    const attemptId = ++generationAttemptId;
    generationStartInFlight = true;
    generationStartAttemptId = attemptId;
    const controller = new AbortController();
    generateAbortController = controller;
    destroyWaveSurfers();
    clearSSEReconnectTimer();
    clearGenerationStartupTimer();
    isGenerating = true;
    activeWorkspace = 'generation';
    currentWorkspace = currentWorkspace?.workflow_id === session.session_id
        || currentWorkspace?.snapshot?.workflow_id === session.session_id
        ? currentWorkspace
        : null;
    if ($('history-nav-btn')) $('history-nav-btn').disabled = true;
    generatedFiles = [];
    logEntryCount = 0;
    lastStats = null;
    lastDownloadEvent = null;
    sseRetryCount = 0;
    generationResult = null;
    transientGenerationErrorMessage = '';
    updateSessionLabels(session.source_filename, session.parse_results, {
        preview: isPreviewScope,
        total: generationTotal,
    });
    hideGenerationRecovery();
    setGenerationVisualState('running');

    // 重置生成页面 UI
    setProgressBarPercent(0);
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', '0');
    $('progress-bar').parentElement?.setAttribute('aria-valuetext', '0% 处理中');
    setProgressReadoutMode(false);
    setProgressIndeterminate(true);
    $('progress-stats').textContent = `正在准备生成计划 · 0 / ${generationTotal || '—'}`;
    $('progress-percent').textContent = '0';
    $('progress-completed-label').textContent = '已完成';
    $('progress-completed').textContent = '0';
    $('progress-remaining').textContent = generationTotal || '—';
    $('progress-failed').textContent = '0';
    if ($('progress-cancelled')) $('progress-cancelled').textContent = '0';
    if ($('progress-skipped')) $('progress-skipped').textContent = '0';
    if ($('progress-deliverable')) $('progress-deliverable').textContent = '0 / 0';
    $('generation-live-status').textContent = '正在准备生成计划…';
    $('gen-title').textContent = '正在生成音频';
    $('gen-animation').classList.remove('done');
    resetLogTimeline('生成任务即将开始，正在等待第一条处理记录…');
    generationStartupTimer = setTimeout(() => {
        generationStartupTimer = null;
        if (!isGenerating || currentSession?.session_id !== session.session_id || lastStats) return;
        $('progress-stats').textContent = `正在连接讯飞浏览器 · 0 / ${generationTotal || '—'}`;
        $('generation-live-status').textContent = '正在连接讯飞浏览器…';
        $('status-text').textContent = '生成中：正在连接讯飞浏览器…';
        addLogEntry({
            level: 'progress', stage: 'synthesize', kind: 'stage', status: 'running',
            key: 'tts:startup', title: '正在启动音频引擎', detail: '首次启动或浏览器刚被中断后，正在重新建立讯飞会话，请保持应用开启。',
        });
    }, 700);
    $('type-stats').innerHTML = '';

    lastGenerationConfig = { ...config };
    void queueVoiceAssetCache([
        config.default_female_voice,
        config.default_male_voice,
        ...Object.values(config.role_voices || {}),
    ]);

    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        // 用户可能刚从中断/失败页面返回；先同步后端版本，避免把旧的
        // expected_state_version 重新提交而触发 STATE_CONFLICT。
        const snapshot = await refreshCurrentWorkflowSnapshot(session);
        if (controller.signal.aborted || attemptId !== generationAttemptId || currentSession?.session_id !== session.session_id) return;
        // 另一个窗口、自动重试调度器或本页面较早的请求可能已经接受了
        // 这个工作流。此时不能再 PATCH 草稿；直接接管权威任务进度，
        // 否则用户会看到“配置已冻结”，而真正的浏览器任务仍在后台运行。
        if (isAcceptedGenerationSnapshot(snapshot)) {
            adoptAcceptedGeneration(session, snapshot, {
                reason: '检测到已有生成任务，已接管当前进度',
            });
            return;
        }
        if (snapshot?.execution_state === 'TERMINAL') {
            const expectedGroupStateVersion = Number(snapshot.group_state_version);
            if (!Number.isInteger(expectedGroupStateVersion)) {
                throw new Error('任务组版本缺失，无法安全创建新的生成任务');
            }
            const rerun = await workflowApi.rerun(session.session_id, {
                expected_group_state_version: expectedGroupStateVersion,
                source_workflow_id: session.session_id,
                reason: 'desktop-renderer-rerun',
            }, {
                idempotencyKey: `renderer-rerun-${session.session_id}-${expectedGroupStateVersion}`,
            });
            if (!rerun?.workflow_id) throw new Error('服务端未返回新的生成任务');
            const nextSession = {
                ...session,
                session_id: String(rerun.workflow_id),
                source_artifact_id: rerun.source_artifact_id || session.source_artifact_id || null,
                state_version: Number(rerun.state_version || 0),
                group_state_version: Number(rerun.group_state_version || expectedGroupStateVersion + 1),
                execution_state: rerun.execution_state || 'CREATED',
                control_state: rerun.control_state || 'RUNNING',
                result_status: rerun.result_status || 'IN_PROGRESS',
                cleanup_state: rerun.cleanup_state || 'NONE',
                latest_event_id: null,
                latest_seq: 0,
                last_event_id: null,
            };
            currentSession = nextSession;
            session = nextSession;
            // A terminal run is immutable and its item IDs belong to the old
            // workflow.  Rerun the complete document; targeted retry is only
            // valid on the still-open original run.
            requestedItemIds = null;
            isPreviewScope = Boolean(config.preview && sourceTotal > 3);
            generationTotal = isPreviewScope ? Math.min(sourceTotal, 3) : sourceTotal;
            workflowStore?.resetCursor?.(session.session_id);
            updateSessionLabels(session.source_filename, session.parse_results, {
                preview: isPreviewScope,
                total: generationTotal,
            });
        }
        // 解析步骤会让 run 进入 ACTIVE，但在第一个执行 attempt 之前仍
        // 是可编辑的。把配置页当前选择写进 SQLite 后再接受 generate，
        // 确保后端不会继续使用创建草稿时的默认音色。已有 attempt 的
        // run 则由后端拒绝改变配置，避免修改已产生外部副作用的事实。
        const persistedConfiguration = buildWorkflowConfiguration(
            config,
            session.source_filename,
            currentConfig?.account_scope,
        );
        const workspaceBeforePatch = await workflowApi.getWorkspace(session.session_id);
        if (workspaceBeforePatch && currentSession?.session_id === session.session_id) {
            currentWorkspace = workspaceBeforePatch;
            workflowStore?.hydrate?.(workspaceBeforePatch, { snapshot: workspaceBeforePatch.snapshot || snapshot });
            renderWorkspaceShell(currentWorkspace, workspaceBeforePatch.snapshot || snapshot);
        }
        const generationAction = workspaceAction('GENERATE', workspaceBeforePatch);
        if (generationAction && generationAction.enabled !== true) {
            throw new Error(generationAction.reason || '当前任务状态不允许生成');
        }
        const configurationRevisionBeforePatch = Number(
            workspaceBeforePatch?.configuration?.configuration_revision,
        );
        if (!Number.isInteger(configurationRevisionBeforePatch) || configurationRevisionBeforePatch < 1) {
            throw new Error('工作区配置版本缺失，无法安全提交生成任务');
        }
        const patched = await workflowApi.patchWorkspace(session.session_id, {
            expected_state_version: Number(workspaceBeforePatch.snapshot?.state_version ?? session.state_version),
            configuration_revision: configurationRevisionBeforePatch,
            configuration: persistedConfiguration,
        }, {
            idempotencyKey: `renderer-config-${session.session_id}-${attemptId}-${configurationRevisionBeforePatch}`,
        });
        if (!patched) throw new Error('服务端未返回更新后的工作区');
        const workspaceAfterPatch = patched;
        if (workspaceAfterPatch && currentSession?.session_id === session.session_id) {
            currentWorkspace = workspaceAfterPatch;
            mergeWorkflowSnapshotIntoSession(workspaceAfterPatch.snapshot, session);
            session.parse_results = workspaceItemsToParseResults(workspaceAfterPatch);
            workflowStore?.hydrate?.(workspaceAfterPatch, { snapshot: workspaceAfterPatch.snapshot });
            renderWorkspaceShell(currentWorkspace, workspaceAfterPatch.snapshot);
            renderContentReview(session.parse_results);
        }
        const configurationRevision = Number(
            workspaceAfterPatch?.configuration?.configuration_revision,
        );
        if (!Number.isInteger(configurationRevision) || configurationRevision < 1) {
            throw new Error('工作区配置版本缺失，无法安全提交生成任务');
        }
        const response = await submitGenerationCommand(
            session,
            config,
            controller,
            attemptId,
            requestedItemIds,
            configurationRevision,
        );

        if (controller.signal.aborted || attemptId !== generationAttemptId || currentSession?.session_id !== session.session_id) return;
        mergeWorkflowSnapshotIntoSession(response?.current_snapshot, currentSession);
        const acceptedVersion = Number(response?.state_version);
        if (Number.isInteger(acceptedVersion) && acceptedVersion >= Number(currentSession.state_version || 0)) {
            currentSession.state_version = acceptedVersion;
        }
        generateAbortController = null;
        clearGenerationStartupTimer();
        connectSSE(session.session_id);
        setProgressIndeterminate(true);
        $('progress-stats').textContent = `已提交任务，正在连接讯飞浏览器 · 0 / ${generationTotal || '—'}`;
        $('generation-live-status').textContent = '正在连接讯飞浏览器并提交作品…';
        $('status-text').textContent = '生成中：正在连接讯飞浏览器…';

    } catch (err) {
        if (err.name === 'AbortError' || attemptId !== generationAttemptId) return;

        // PATCH 与后台调度之间存在竞态：PATCH 读取时仍可编辑，真正提交
        // 前自动化任务已经先被接受。CONFIG_FROZEN 在这里不是“重新报错”
        // 的终点，而是重新读取并接管那个已经在跑的任务。
        if (['CONFIG_FROZEN', 'GENERATION_ALREADY_RUNNING'].includes(err?.code) && workflowApi) {
            try {
                const authoritative = await workflowApi.getWorkflow(session.session_id);
                if (
                    currentSession?.session_id === session.session_id
                    && isAcceptedGenerationSnapshot(authoritative)
                ) {
                    adoptAcceptedGeneration(session, authoritative, {
                        reason: '任务已由后台接受，已接管当前进度',
                    });
                    showToast('任务已经在运行，已接管后台进度', 'warning');
                    return;
                }
            } catch (syncError) {
                console.warn('配置冻结后同步已接受任务失败:', syncError);
            }
        }
        clearGenerationStartupTimer();
        generateAbortController = null;
        generationResult = 'error';
        const serviceUnavailable = err instanceof TypeError || /failed to fetch/i.test(err.message || '');
        const failureMessage = serviceUnavailable
            ? '无法连接生成服务，请重试连接后再次生成。'
            : err?.code === 'CONFIG_FROZEN'
                    ? '本次任务已经开始过外部提交，不能在原任务中修改音色或参数；请点击“重新开始”新建任务后再生成。'
                    : `启动失败：${err?.message || '生成服务返回了未说明的错误'}`;
        transientGenerationErrorMessage = failureMessage;
        $('gen-title').textContent = '任务未能启动';
        $('generation-file-name').textContent = `未能启动「${session.source_filename || '当前文档'}」；设置与解析结果仍会保留。`;
        $('status-text').textContent = failureMessage;
        if (serviceUnavailable) {
            setServiceState('error', '服务连接中断');
            $('retry-service-btn').hidden = false;
        }
        setGenerationVisualState('error');
        setProgressIndeterminate(false);
        addLogEntry({
            level: 'error',
            stage: 'complete',
            kind: 'summary',
            status: 'error',
            key: 'task:summary',
            title: '生成任务未能启动',
            detail: failureMessage,
        });
        resetGenerateState();
        syncGenerationRecoveryState(
            currentWorkspace,
            workspaceUserState(currentWorkspace, currentSession),
        );
        syncTransientGenerationErrorShell(failureMessage);
        showToast(failureMessage, 'error');
    } finally {
        if (generationStartAttemptId === attemptId) {
            generationStartInFlight = false;
            generationStartAttemptId = 0;
            updateGenerationCancelUI();
        }
    }
}

// ============================================================================
// DOM 引用
// ============================================================================

const $ = (id) => document.getElementById(id);
const $$ = (sel) => document.querySelectorAll(sel);

function setProgressBarPercent(percent) {
    const bar = $('progress-bar');
    if (!bar) return;
    const normalized = Math.min(100, Math.max(0, Number(percent) || 0));
    bar.style.setProperty('--progress-scale', String(normalized / 100));
}

const STEP_TITLES = {
    1: '01 / 导入文档',
    2: '02 / 核对与设置',
    3: '03 / 生成音频',
    4: '04 / 试听与下载',
};

const WORKSPACE_ORDER = ['import', 'review', 'voice', 'generation', 'delivery'];
const WORKSPACE_TITLES = {
    import: '导入文档',
    review: '核对内容',
    voice: '配置声音',
    generation: '生成任务',
    delivery: '交付中心',
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

function workspaceProgress(workspace = null) {
    const sourceWorkspace = workspace || authoritativeWorkspace();
    if (typeof workflowReducer.normalizeProgress === 'function') {
        return workflowReducer.normalizeProgress(sourceWorkspace?.progress, sourceWorkspace?.items, sourceWorkspace?.artifacts);
    }
    return sourceWorkspace?.progress || { total: 0, completed: 0, failed: 0, cancelled: 0, skipped: 0, pending: 0, deliverable: 0, percent: 0, deliverable_percent: 0 };
}

function renderWorkspaceProgress(workspace = currentWorkspace, state = null) {
    const progress = workspaceProgress(workspace);
    const total = Math.max(0, Number(progress.total) || 0);
    const completed = Math.max(0, Number(progress.completed) || 0);
    const deliverable = Math.max(0, Number(progress.deliverable) || 0);
    const pending = Math.max(0, Number(progress.pending) || 0);
    if ($('progress-completed')) $('progress-completed').textContent = String(completed);
    if ($('progress-remaining')) $('progress-remaining').textContent = total > 0 ? String(pending) : '—';
    if ($('progress-failed')) $('progress-failed').textContent = String(Math.max(0, Number(progress.failed) || 0));
    if ($('progress-cancelled')) $('progress-cancelled').textContent = String(Math.max(0, Number(progress.cancelled) || 0));
    if ($('progress-skipped')) $('progress-skipped').textContent = String(Math.max(0, Number(progress.skipped) || 0));
    if ($('progress-deliverable')) $('progress-deliverable').textContent = total > 0
        ? `${Math.max(0, Number(progress.deliverable) || 0)} / ${total}`
        : '0 / 0';
    // Live provider segment progress is more granular during an active run;
    // only let the item projection own the bar before/after that run.
    const terminal = Boolean(state?.terminal) || isTerminalWorkflowSnapshot(workspace?.snapshot || workspace);
    if (total > 0 && (!isGenerating || terminal)) {
        const percent = terminal
            ? terminalProgressPercent(deliverable, total)
            : Math.min(99, Math.max(0, Number(progress.percent) || 0));
        setProgressBarPercent(percent);
        $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(percent));
        $('progress-bar').parentElement?.setAttribute('aria-valuetext', terminal ? `${percent}% 可交付` : `${percent}% 处理中`);
        $('progress-percent').textContent = String(percent);
        setProgressReadoutMode(terminal, terminal && (progress.failed > 0 || progress.cancelled > 0 || progress.skipped > 0));
    }
}

function setWorkspaceTheme(theme, { persist = true } = {}) {
    const next = theme === 'dark' ? 'dark' : 'light';
    themePreference = next;
    document.documentElement.dataset.theme = next;
    document.documentElement.style.colorScheme = next;
    const toggle = $('theme-toggle');
    if (toggle) {
        const dark = next === 'dark';
        toggle.setAttribute('aria-label', dark ? '切换浅色模式' : '切换深色模式');
        toggle.title = dark ? '切换浅色模式' : '切换深色模式';
        toggle.setAttribute('aria-pressed', dark ? 'true' : 'false');
    }
    if (persist) {
        try { rendererStorage?.setItem('wordtts_theme_preference', next); } catch (_) { /* ignore */ }
    }
}

function initializeTheme() {
    let stored = '';
    try { stored = rendererStorage?.getItem('wordtts_theme_preference') || ''; } catch (_) { /* ignore */ }
    const systemPrefersDark = typeof window.matchMedia === 'function'
        && window.matchMedia('(prefers-color-scheme: dark)').matches;
    const initial = stored === 'dark' || stored === 'light'
        ? stored
        : (systemPrefersDark ? 'dark' : 'light');
    // Do not persist the system-derived default.  Only an explicit user
    // toggle becomes a local UI preference, so later OS theme changes remain
    // visible until the user chooses a mode.
    setWorkspaceTheme(initial, { persist: stored === 'dark' || stored === 'light' });
}

function setServiceState(state, label) {
    const service = $('service-state');
    if (!service) return;
    service.classList.remove('is-ready', 'is-warning', 'is-error');
    if (state) service.classList.add(`is-${state}`);
    const labelEl = service.querySelector('.service-label');
    if (labelEl) labelEl.textContent = label;
    renderProviderStatus();
}

function summarizeParseResults(parseResults) {
    if (!Array.isArray(parseResults)) return { total: 0, types: [] };
    const types = [...new Set(parseResults.map(item => item?.doc_type).filter(Boolean))];
    const total = parseResults.reduce((sum, item) => {
        const count = Number(item?.item_count ?? item?.items?.length ?? 0);
        return sum + (Number.isFinite(count) ? count : 0);
    }, 0);
    return { total, types };
}

function updateSessionLabels(filename = '', parseResults = currentSession?.parse_results, generationScope = null) {
    const displayName = filename || '已导入的文档';
    const { total, types } = summarizeParseResults(parseResults);
    const generationTotal = generationScope?.total ?? total;
    const generationDescriptor = generationScope?.preview
        ? `试听模式 · 前 ${generationTotal} 条内容`
        : (generationTotal > 0 ? `共 ${generationTotal} 条内容` : '');
    const sourceName = $('source-file-name');
    const sourceMeta = $('source-file-meta');
    const generationName = $('generation-file-name');
    const summaryDocument = $('summary-document');
    const generateButtonLabel = $('generate-button-label');
    const toolbarDocument = $('toolbar-document');
    if (sourceName) sourceName.textContent = displayName;
    if (sourceMeta) sourceMeta.textContent = filename
        ? (total > 0 ? `已识别 ${total} 条 · ${types.length} 种题型` : '文档解析完成')
        : '当前文档';
    if (summaryDocument) summaryDocument.textContent = total > 0
        ? `${total} 条 · ${types.length} 种题型`
        : '等待解析';
    if (generateButtonLabel) {
        const previewEnabled = Boolean($('preview')?.checked && total > 3);
        generateButtonLabel.textContent = previewEnabled
            ? `先试听 ${Math.min(total, 3)} 条`
            : (total > 0 ? `开始生成 ${total} 条音频` : '开始生成音频');
    }
    if (toolbarDocument) {
        toolbarDocument.textContent = filename || '';
        toolbarDocument.title = filename || '';
        toolbarDocument.hidden = !filename;
    }
    if (generationName) {
        generationName.textContent = filename
            ? `正在处理「${displayName}」${generationDescriptor ? ` · ${generationDescriptor}` : ''}，请保持应用开启。`
            : `${PRODUCT_NAME} 正在准备当前文档，请保持应用开启。`;
    }
}

function setActiveWorkspaceView(workspaceName) {
    const next = WORKSPACE_ORDER.includes(workspaceName) ? workspaceName : 'import';
    activeWorkspace = next;
    const review = $('content-review-view');
    const voice = $('voice-config-view');
    const reviewVisible = next === 'review';
    const voiceVisible = next === 'voice';
    if (review) {
        review.hidden = !reviewVisible;
        review.setAttribute('aria-hidden', reviewVisible ? 'false' : 'true');
    }
    if (voice) {
        voice.hidden = !voiceVisible;
        voice.setAttribute('aria-hidden', voiceVisible ? 'false' : 'true');
    }
    document.body.dataset.activeWorkspace = next;
    return next;
}

function workspaceItemsToParseResults(workspace) {
    const items = Array.isArray(workspace?.items) ? workspace.items : [];
    const groups = new Map();
    items.forEach((item, index) => {
        const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
        const docType = String(
            metadata.doc_type || metadata.category || item?.item_type || '未分类',
        );
        const itemType = String(
            item?.item_type || metadata.category || docType || 'document',
        );
        if (!groups.has(docType)) groups.set(docType, []);
        const itemId = String(item?.item_id || `workspace-item-${index}`);
        const loadedContent = readItemContentCache(itemId);
        const normalizedContent = loadedContent !== undefined
            ? loadedContent
            : (typeof item?.normalized_content === 'string' ? item.normalized_content : null);
        groups.get(docType).push({
            item_id: itemId,
            doc_type: docType,
            // Keep the parser's leaf type.  The old renderer copied doc_type
            // into category here, which made every row look like the same
            // top-level type (for example, every reading item became
            // “课文跟读” and hid “句子/段落/语篇跟读”).
            category: itemType,
            item_type: itemType,
            sequence: Number(item?.sequence ?? index),
            text: normalizedContent,
            content: normalizedContent,
            normalized_content: normalizedContent,
            content_ref: item?.content_ref || null,
            source_locator: item?.source_locator || null,
            metadata,
            role: item?.role || null,
            voice_key: item?.voice_key || null,
            voice: item?.voice || metadata.voice || metadata.voice_gender || metadata.gender || null,
            question_type: item?.question_type || metadata.question_type || null,
            sub_type_code: item?.sub_type_code || metadata.sub_type_code || null,
            type_path: item?.type_path || metadata.type_path || metadata.type_hierarchy || null,
            status: item?.status || 'PENDING',
            skip_reason: item?.skip_reason || null,
            error_code: item?.error_code || null,
            user_message: item?.user_message || null,
        });
    });
    return [...groups.entries()].map(([docType, groupItems]) => ({
        doc_type: docType,
        category: docType,
        item_count: groupItems.length,
        items: groupItems,
    }));
}

function reviewContentForItem(item) {
    const itemId = String(item?.item_id || '');
    const cacheKey = itemContentCacheKey(itemId);
    if (itemId && itemContentCache.has(cacheKey)) return readItemContentCache(itemId);
    if (typeof item?.normalized_content === 'string') return item.normalized_content;
    if (typeof item?.text === 'string') return item.text;
    if (typeof item?.content === 'string') return item.content;
    return '';
}

function reviewItemIsEditable(item, workspace = authoritativeWorkspace()) {
    const status = String(item?.status || '').toUpperCase();
    const hasContent = readItemContentCache(item?.item_id) !== undefined
        || typeof item?.normalized_content === 'string';
    return workspaceActionEnabled('SAVE_CONFIGURATION', workspace)
        && ['PENDING', 'SKIPPED'].includes(status)
        && hasContent
        && Boolean(item?.item_id);
}

function reviewStatusLabel(status) {
    switch (String(status || '').toUpperCase()) {
        case 'SUCCEEDED': return '已完成';
        case 'SKIPPED': return '已跳过';
        case 'FAILED': return '生成失败';
        case 'CANCELLED': return '已取消';
        case 'RUNNING': return '生成中';
        case 'PENDING': return '待处理';
        default: return String(status || '待处理');
    }
}

// The parser has two useful type dimensions: the document family and the
// leaf item type.  Keep the code-to-label map here so the review surface can
// show either the parser's Chinese label or the atomic model's stable code.
const REVIEW_TYPE_LABELS = Object.freeze({
    info_acquisition: '信息获取',
    listening_info: '听选信息',
    answer_question: '回答问题',
    listening_choice: '听后选择',
    listening_response: '听后应答',
    info_retelling: '信息转述及询问',
    asking_info: '询问信息',
    listening_record_retelling: '听后记录并转述信息',
    imitation_reading: '模仿朗读',
    text_reading: '课文跟读',
    text_reading_sentence: '句子跟读',
    text_reading_paragraph: '段落跟读',
    text_reading_discourse: '语篇跟读',
    vocabulary: '词汇',
});

const REVIEW_TYPE_ALIASES = Object.freeze({
    '听选信息题目': '听选信息',
    '听选信息录音稿': '听选信息',
    '回答问题题目': '回答问题',
    '回答问题录音稿': '回答问题',
    '听后记录并转述信息录音稿': '听后记录并转述信息',
});

const REVIEW_TYPE_FAMILIES = Object.freeze({
    listening_info: '信息获取',
    answer_question: '信息获取',
    listening_choice: '听后选择',
    listening_response: '听后应答',
    info_retelling: '信息转述及询问',
    asking_info: '信息转述及询问',
    listening_record_retelling: '听后记录并转述信息',
    imitation_reading: '模仿朗读',
    text_reading_sentence: '课文跟读',
    text_reading_paragraph: '课文跟读',
    text_reading_discourse: '课文跟读',
    vocabulary: '词汇',
});

const REVIEW_GENERIC_TYPES = new Set([
    'document', 'audio', 'content', 'question', 'stimulus', 'work_item',
]);

function reviewTypeKey(value) {
    return String(value ?? '').trim().toLocaleLowerCase('zh-CN');
}

function reviewTypeLabel(value) {
    const raw = String(value ?? '').trim();
    if (!raw) return '';
    const key = reviewTypeKey(raw);
    return REVIEW_TYPE_LABELS[key] || REVIEW_TYPE_ALIASES[raw] || raw;
}

function reviewTypeParts(value) {
    if (Array.isArray(value)) {
        return value.flatMap(entry => reviewTypeParts(entry));
    }
    if (value && typeof value === 'object') {
        return reviewTypeParts(
            value.label || value.display_name || value.name || value.title || value.code,
        );
    }
    const raw = String(value ?? '').trim();
    if (!raw) return [];
    return raw
        .split(/\s*(?:\/|>|＞|→|›|»|·)\s*/)
        .map(part => reviewTypeLabel(part))
        .filter(part => part && !/^\d+$/.test(part));
}

function reviewIsGenericType(value) {
    return REVIEW_GENERIC_TYPES.has(reviewTypeKey(value));
}

function reviewPushTypePart(parts, value) {
    reviewTypeParts(value).forEach(part => {
        if (reviewIsGenericType(part)) return;
        if (!parts.some(existing => reviewTypeKey(existing) === reviewTypeKey(part))) {
            parts.push(part);
        }
    });
}

function reviewTypePathForItem(item, fallbackDocType = '') {
    const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
    const subtypeCode = item?.question_type
        || item?.sub_type_code
        || metadata.question_type
        || metadata.sub_type_code;
    const subtypeLabel = reviewTypeLabel(subtypeCode);
    const explicitPath = item?.type_path
        || item?.typePath
        || item?.type_hierarchy
        || item?.typeHierarchy
        || metadata.type_path
        || metadata.type_hierarchy
        || metadata.typeHierarchy;
    const parts = [];
    reviewPushTypePart(parts, explicitPath);

    let family = item?.major_type
        || metadata.major_type
        || metadata.doc_type
        || item?.doc_type
        || fallbackDocType;
    if (reviewIsGenericType(family) && subtypeCode) family = REVIEW_TYPE_FAMILIES[reviewTypeKey(subtypeCode)] || family;
    reviewPushTypePart(parts, family);

    // item_type/category is the parser's concrete leaf type for legacy and
    // current source imports.  Stable subtype codes cover atomic projections
    // where the item type is only “audio”.
    reviewPushTypePart(parts, item?.category);
    reviewPushTypePart(parts, item?.item_type);
    reviewPushTypePart(parts, metadata.category);
    reviewPushTypePart(parts, subtypeLabel);
    const roleCode = reviewTypeKey(item?.role);
    if (REVIEW_TYPE_LABELS[roleCode]) reviewPushTypePart(parts, roleCode);

    return parts.length ? parts : ['未分类'];
}

function reviewRoleLabel(item) {
    const raw = String(item?.role ?? item?.metadata?.role ?? '').trim();
    if (!raw) return '';
    const key = reviewTypeKey(raw);
    if (['default', 'default_female', 'default_male', '__default_female__', '__default_male__', 'forced_female', 'speaker'].includes(key)) return '';
    if (REVIEW_TYPE_LABELS[key]) return '';
    return raw.replace(/^role:/i, '').trim();
}

function reviewGenderFromValue(value) {
    const key = String(value ?? '').trim().toLocaleLowerCase('zh-CN');
    if (!key) return '';
    if (['female', '女', '女声', '女生', '女性', 'woman', 'girl', 'f', 'w', 'default_female', '__default_female__', 'forced_female', 'forced-female'].includes(key)) return 'female';
    if (['male', '男', '男声', '男生', '男性', 'man', 'boy', 'm', 'default_male', '__default_male__', 'forced_male', 'forced-male'].includes(key)) return 'male';
    return '';
}

function reviewConcreteVoiceKey(item, roleLabel = '') {
    const policyKeys = new Set(['default', 'speaker', 'forced_female', 'forced-female', 'forced_male', 'forced-male']);
    const rawItemKey = String(item?.voice_key || '').trim();
    if (rawItemKey && !policyKeys.has(reviewTypeKey(rawItemKey))) return rawItemKey;
    if (roleLabel) {
        const roleKey = normalizeRoleKeyClient(roleLabel);
        const configured = roleVoiceMap?.[roleKey]
            || currentConfig?.role_voices?.[roleKey]
            || currentConfig?.role_voices?.[roleLabel];
        if (configured) return String(configured);
    }
    return '';
}

function reviewItemGender(item) {
    const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
    const directValues = [
        item?.voice,
        item?.voice_gender,
        item?.gender,
        metadata.voice,
        metadata.voice_gender,
        metadata.gender,
    ];
    for (const value of directValues) {
        const gender = reviewGenderFromValue(value);
        if (gender) return gender;
    }
    const itemKey = reviewTypeKey(item?.voice_key);
    if (['forced_female', 'forced-female'].includes(itemKey)) return 'female';
    if (['forced_male', 'forced-male'].includes(itemKey)) return 'male';
    const roleRaw = item?.role;
    const roleGender = reviewGenderFromValue(roleRaw);
    if (roleGender) return roleGender;

    const roleLabel = reviewRoleLabel(item);
    const configuredVoiceKey = reviewConcreteVoiceKey(item, roleLabel);
    const configuredVoice = configuredVoiceKey ? getVoiceEntry(configuredVoiceKey) : null;
    if (configuredVoice?.gender === 'female' || configuredVoice?.gender === 'male') return configuredVoice.gender;

    const normalizedKey = canonicalVoiceKey(item?.voice_key || '');
    if (normalizedKey && canonicalVoiceKey(selectedDefaultMaleVoice) === normalizedKey) return 'male';
    if (normalizedKey && canonicalVoiceKey(selectedDefaultFemaleVoice) === normalizedKey) return 'female';
    // Unmarked content follows the product's documented default: female.
    return 'female';
}

function reviewVoicePresentation(item) {
    const role = reviewRoleLabel(item);
    const gender = reviewItemGender(item);
    const genderLabel = gender === 'male' ? '男声' : '女声';
    if (!role) return { role: '', voice: `默认${genderLabel}` };
    const voiceKey = reviewConcreteVoiceKey(item, role);
    const entry = voiceKey ? getVoiceEntry(voiceKey) : null;
    return {
        role: `角色：${role}`,
        voice: entry?.name ? `音色：${entry.name}` : '音色：按角色配置',
    };
}

function renderReviewInspector(item, index = 0) {
    const empty = $('review-inspector-empty');
    const content = $('review-inspector-content');
    if (!item) {
        if (empty) empty.hidden = false;
        if (content) content.hidden = true;
        return;
    }
    if (empty) empty.hidden = true;
    if (content) content.hidden = false;
    const typePath = reviewTypePathForItem(item, item.doc_type || item.category || '未分类');
    const itemTitle = typePath[0] || String(item.doc_type || item.category || '解析条目');
    const voicePresentation = reviewVoicePresentation(item);
    const locator = item.source_locator || item.sourceLocator || item.metadata?.source_locator;
    const sourceLabel = locator
        ? String(locator)
        : (item.content_ref?.content_id ? '正文按需加载' : '未提供');
    const body = reviewContentForItem(item);
    if ($('review-inspector-index')) $('review-inspector-index').textContent = String(index + 1).padStart(2, '0');
    if ($('review-inspector-item-title')) $('review-inspector-item-title').textContent = itemTitle;
    if ($('review-inspector-status')) {
        const status = $('review-inspector-status');
        status.textContent = reviewStatusLabel(item.status);
        status.className = `inspector-status is-${String(item.status || 'PENDING').toLowerCase()}`;
    }
    if ($('review-inspector-source')) $('review-inspector-source').textContent = sourceLabel;
    if ($('review-inspector-type')) $('review-inspector-type').textContent = typePath.join(' / ');
    if ($('review-inspector-voice')) $('review-inspector-voice').textContent = voicePresentation.voice;
    if ($('review-inspector-body')) $('review-inspector-body').textContent = body || (item.content_ref ? '正文较长，请点击条目中的“加载全文”。' : '（无可预览文本）');
}

function selectReviewItem(item, index, row) {
    $$('.review-item-row').forEach(entry => {
        const selected = entry === row;
        entry.classList.toggle('is-selected', selected);
        entry.setAttribute('aria-current', selected ? 'true' : 'false');
    });
    renderReviewInspector(item, index);
}

async function loadReviewItemContent(item, button) {
    const workflowId = currentSession?.session_id;
    const contentRef = item?.content_ref;
    if (!workflowId || !contentRef?.content_id || !workflowApi?.getItemContent) return;
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        button.textContent = '加载中…';
    }
    try {
        const workspace = authoritativeWorkspace(workflowId);
        const expectedStateVersion = workspace?.snapshot?.state_version;
        const maxResponseBytes = Math.max(
            1024,
            Math.min(65536, Number(contentRef.max_response_bytes) || 65536),
        );
        let offsetBytes = 0;
        let completeContent = '';
        let loaded = false;
        // Long item bodies are addressable through content_ref and read in
        // bounded UTF-8 chunks.  Never turn a partial response into editable
        // text or loop forever if a malformed server projection stops making
        // progress.
        for (let chunkIndex = 0; chunkIndex < 2048; chunkIndex += 1) {
            const response = await workflowApi.getItemContent(
                workflowId,
                item.item_id,
                contentRef.content_id,
                expectedStateVersion,
                { offsetBytes, maxResponseBytes },
            );
            if (typeof response?.content !== 'string') throw new Error('服务端未返回条目正文');
            completeContent += response.content;
            if (response.truncated !== true) {
                loaded = true;
                break;
            }
            const nextOffset = Number(response.next_offset_bytes);
            if (!Number.isSafeInteger(nextOffset) || nextOffset <= offsetBytes) {
                throw new Error('服务端条目正文分块没有向前推进');
            }
            offsetBytes = nextOffset;
        }
        if (!loaded) throw new Error('条目正文分块数量超过安全上限');
        rememberItemContentCache(item.item_id, completeContent, workflowId);
        renderContentReview();
        showToast('已加载条目全文');
    } catch (error) {
        console.error('加载条目全文失败:', error);
        if (error?.code === 'STATE_CONFLICT') await hydrateWorkflowWorkspace(workflowId, { silent: true });
        showToast(workflowAdapter.issueMessage?.(error, '条目全文暂时无法加载')?.message || '条目全文暂时无法加载', 'error');
    } finally {
        if (button) {
            button.disabled = false;
            button.removeAttribute('aria-busy');
            button.textContent = '加载全文';
        }
    }
}

async function patchCurrentWorkspaceItem(itemId, patch) {
    const workflowId = currentSession?.session_id;
    const workspace = authoritativeWorkspace(workflowId);
    if (!workflowId || !workspace || !workflowApi?.patchWorkspace) throw new Error('当前任务工作区不可编辑');
    const action = workspaceAction('SAVE_CONFIGURATION', workspace);
    if (action?.enabled !== true) {
        const error = new Error(action?.reason || '当前任务状态不允许编辑条目');
        error.code = 'CONFIG_FROZEN';
        throw error;
    }
    const stateVersion = Number(workspace.snapshot?.state_version);
    const configurationRevision = Number(workspace.configuration?.configuration_revision);
    if (!Number.isInteger(stateVersion) || !Number.isInteger(configurationRevision)) {
        throw new Error('工作区版本缺失，无法安全保存条目');
    }
    const updated = await workflowApi.patchWorkspace(workflowId, {
        expected_state_version: stateVersion,
        configuration_revision: configurationRevision,
        item_overrides: [{ item_id: String(itemId), patch }],
    }, {
        idempotencyKey: `renderer-item-edit-${workflowId}-${itemId}-${stateVersion}-${Date.now()}`,
    });
    if (!updated) throw new Error('服务端未返回更新后的工作区');
    if (typeof patch.normalized_content === 'string') {
        rememberItemContentCache(itemId, patch.normalized_content, workflowId);
    }
    currentWorkspace = updated;
    currentSession.parse_results = workspaceItemsToParseResults(updated);
    mergeWorkflowSnapshotIntoSession(updated.snapshot, currentSession);
    workflowStore?.hydrate?.(updated, { snapshot: updated.snapshot });
    renderWorkspaceShell(updated, updated.snapshot);
    updateSessionLabels(currentSession.source_filename, currentSession.parse_results);
    renderContentReview(currentSession.parse_results);
    return updated;
}

function renderContentReview(parseResults = currentSession?.parse_results) {
    const workspace = authoritativeWorkspace();
    const workspaceGroups = Array.isArray(workspace?.items)
        ? workspaceItemsToParseResults(workspace)
        : null;
    const groups = workspaceGroups || (Array.isArray(parseResults) ? parseResults : []);
    const summary = summarizeParseResults(groups);
    const items = groups.flatMap((group, groupIndex) => {
        const groupItems = Array.isArray(group?.items) ? group.items : [];
        return groupItems.map((item, index) => ({
            ...item,
            doc_type: item?.doc_type || group?.doc_type || group?.category || '未分类',
            sequence: Number(item?.sequence ?? item?.number ?? index + 1),
            groupIndex,
        }));
    }).sort((left, right) => (
        (Number.isFinite(left.sequence) ? left.sequence : Number.MAX_SAFE_INTEGER)
        - (Number.isFinite(right.sequence) ? right.sequence : Number.MAX_SAFE_INTEGER)
        || left.groupIndex - right.groupIndex
    ));
    const outline = $('review-outline');
    const reviewItems = $('review-items');
    const empty = $('review-empty');
    if ($('review-summary')) $('review-summary').textContent = `${summary.total} 条 · ${summary.types.length || groups.length} 类内容`;
    if ($('review-item-count')) $('review-item-count').textContent = `${items.length || summary.total} 条`;
    if (outline) {
        outline.replaceChildren();
        groups.forEach((group, index) => {
            const row = document.createElement('div');
            row.className = 'review-outline-row';
            const copy = document.createElement('div');
            copy.className = 'review-outline-copy';
            const name = document.createElement('strong');
            const groupName = String(group?.doc_type || group?.category || `内容组 ${index + 1}`);
            name.textContent = reviewTypeLabel(groupName);
            copy.appendChild(name);
            const childTypes = [...new Set(
                (Array.isArray(group?.items) ? group.items : [])
                    .map(item => reviewTypePathForItem({ ...item, doc_type: groupName }, groupName).slice(1).join(' / '))
                    .filter(Boolean),
            )];
            if (childTypes.length > 0) {
                const detail = document.createElement('small');
                detail.className = 'review-outline-detail';
                detail.textContent = childTypes.join(' · ');
                copy.appendChild(detail);
            }
            const count = document.createElement('span');
            count.textContent = `${Number(group?.item_count ?? group?.items?.length ?? 0)} 条`;
            row.append(copy, count);
            outline.appendChild(row);
        });
    }
    if (!reviewItems) return;
    reviewItems.replaceChildren();
    if (items.length === 0) {
        if (empty) empty.hidden = false;
        return;
    }
    if (empty) empty.hidden = true;
    const fragment = document.createDocumentFragment();
    items.slice(0, 500).forEach((item, index) => {
        const row = document.createElement('article');
        row.className = 'review-item-row';
        row.setAttribute('role', 'listitem');
        row.tabIndex = 0;
        row.setAttribute('aria-current', 'false');
        const typePath = reviewTypePathForItem(item, item.doc_type || '未分类');
        const voicePresentation = reviewVoicePresentation(item);
        row.setAttribute('aria-label', `查看第 ${index + 1} 条${typePath.length ? ` ${typePath.join('，')}` : ''}`);
        if (item.item_id) row.dataset.itemId = String(item.item_id);
        row.classList.add(`is-${String(item.status || 'PENDING').toLowerCase()}`);
        const indexEl = document.createElement('span');
        indexEl.className = 'review-item-index';
        indexEl.textContent = String(index + 1).padStart(2, '0');
        const body = document.createElement('div');
        body.className = 'review-item-body';
        const meta = document.createElement('div');
        meta.className = 'review-item-meta';
        const type = document.createElement('strong');
        type.textContent = typePath[0] || '未分类';
        meta.appendChild(type);
        if (typePath.length > 1) {
            const typeDetail = document.createElement('span');
            typeDetail.className = 'review-item-type-path';
            typeDetail.textContent = `小题 · ${typePath.slice(1).join(' / ')}`;
            meta.appendChild(typeDetail);
        }
        if (voicePresentation.role) {
            const role = document.createElement('span');
            role.className = 'review-item-role';
            role.textContent = voicePresentation.role;
            meta.appendChild(role);
        }
        const voice = document.createElement('span');
        voice.className = 'review-item-voice';
        voice.textContent = voicePresentation.voice;
        meta.appendChild(voice);
        const itemText = reviewContentForItem(item);
        const contentRef = item.content_ref;
        const editable = reviewItemIsEditable(item, workspace);
        let content;
        if (editable) {
            content = document.createElement('textarea');
            content.className = 'review-item-editor';
            content.value = itemText;
            content.rows = Math.min(8, Math.max(3, itemText.split('\n').length));
            content.placeholder = contentRef && !itemText
                ? '正文较长，请先加载全文'
                : '输入要提交给讯飞的正文';
            content.disabled = Boolean(contentRef && !itemText);
            content.dataset.reviewEditor = String(item.item_id || '');
            content.setAttribute('aria-label', `编辑第 ${index + 1} 条内容`);
        } else {
            content = document.createElement('p');
            content.className = 'review-item-content';
            content.textContent = itemText || (contentRef ? '（正文较长，点击“加载全文”查看）' : '（无可预览文本）');
        }
        const locator = item.source_locator || item.sourceLocator || item.metadata?.source_locator;
        if (locator) {
            const source = document.createElement('small');
            source.className = 'review-item-locator';
            source.textContent = `来源：${String(locator).slice(0, 180)}`;
            body.append(meta, source, content);
        } else {
            body.append(meta, content);
        }
        const details = document.createElement('div');
        details.className = 'review-item-details';
        if (item.skip_reason) {
            const skip = document.createElement('span');
            skip.className = 'review-item-skip-reason';
            skip.textContent = `跳过原因：${item.skip_reason}`;
            details.appendChild(skip);
        }
        if (contentRef && !itemText) {
            const loadButton = document.createElement('button');
            loadButton.type = 'button';
            loadButton.className = 'btn-ghost btn-sm review-item-load';
            loadButton.textContent = '加载全文';
            loadButton.addEventListener('click', () => { void loadReviewItemContent(item, loadButton); });
            details.appendChild(loadButton);
        }
        if (editable) {
            const saveButton = document.createElement('button');
            saveButton.type = 'button';
            saveButton.className = 'btn-secondary btn-sm';
            saveButton.textContent = '保存修改';
            saveButton.addEventListener('click', async () => {
                if (content.disabled) return;
                const nextText = String(content.value || '').trim();
                if (!nextText) {
                    showToast('条目正文不能为空', 'error');
                    content.focus();
                    return;
                }
                saveButton.disabled = true;
                saveButton.setAttribute('aria-busy', 'true');
                try {
                    await patchCurrentWorkspaceItem(item.item_id, { normalized_content: nextText });
                    showToast('条目修改已保存');
                } catch (error) {
                    console.error('保存条目修改失败:', error);
                    if (error?.code === 'STATE_CONFLICT' || error?.code === 'CONFIGURATION_CONFLICT') {
                        await hydrateWorkflowWorkspace(currentSession?.session_id, { silent: true });
                    }
                    showToast(workflowAdapter.issueMessage?.(error, '条目修改未保存')?.message || '条目修改未保存', 'error');
                } finally {
                    saveButton.disabled = false;
                    saveButton.removeAttribute('aria-busy');
                }
            });
            details.appendChild(saveButton);
            const skipButton = document.createElement('button');
            skipButton.type = 'button';
            skipButton.className = 'btn-ghost btn-sm';
            const isSkipped = String(item.status || '').toUpperCase() === 'SKIPPED';
            skipButton.textContent = isSkipped ? '恢复条目' : '跳过条目';
            skipButton.addEventListener('click', async () => {
                let skipReason = item.skip_reason || '';
                if (!isSkipped) {
                    skipReason = await showPromptDialog(
                        '跳过这条内容？',
                        '跳过后它不会提交给讯飞，也不会进入交付范围。之后仍可恢复；请填写原因。',
                        '用户跳过',
                    );
                    if (skipReason === null) return;
                    skipReason = String(skipReason || '用户跳过').trim().slice(0, 500) || '用户跳过';
                }
                skipButton.disabled = true;
                try {
                    await patchCurrentWorkspaceItem(item.item_id, isSkipped
                        ? { status: 'PENDING', skip_reason: null }
                        : { status: 'SKIPPED', skip_reason: skipReason });
                    showToast(isSkipped ? '条目已恢复' : '条目已跳过');
                } catch (error) {
                    console.error('更新条目状态失败:', error);
                    showToast(workflowAdapter.issueMessage?.(error, '条目状态未更新')?.message || '条目状态未更新', 'error');
                } finally {
                    skipButton.disabled = false;
                }
            });
            details.appendChild(skipButton);
        }
        if (details.childElementCount > 0) body.appendChild(details);
        row.append(indexEl, body);
        const selectRow = event => {
            if (event?.target?.closest('button, textarea, input, select')) return;
            selectReviewItem(item, index, row);
        };
        row.addEventListener('click', selectRow);
        row.addEventListener('keydown', event => {
            if (event.target !== row || !['Enter', ' '].includes(event.key)) return;
            event.preventDefault();
            selectReviewItem(item, index, row);
        });
        fragment.appendChild(row);
    });
    if (items.length > 500) {
        const more = document.createElement('p');
        more.className = 'review-truncated-note';
        more.textContent = `已展示前 500 条，剩余 ${items.length - 500} 条仍会按完整解析结果生成。`;
        fragment.appendChild(more);
    }
    reviewItems.appendChild(fragment);
    const firstRow = reviewItems.querySelector('.review-item-row');
    if (firstRow) {
        firstRow.classList.add('is-selected');
        firstRow.setAttribute('aria-current', 'true');
        renderReviewInspector(items[0], 0);
    } else {
        renderReviewInspector(null);
    }
}

function showContentReview() {
    currentView = 'workflow';
    currentStep = 2;
    setActiveWorkspaceView('review');
    renderContentReview();
    $$('.step-page').forEach(page => page.classList.remove('active'));
    $('page-2')?.classList.add('active');
    updateStepper();
    // updateStepper re-renders the workspace shell and may run after an
    // in-flight workspace refresh. Keep the visible nested view authoritative
    // for this navigation action.
    setActiveWorkspaceView('review');
    const scrollPage = $('page-2')?.querySelector('.page-scroll');
    if (scrollPage) scrollPage.scrollTop = 0;
    requestAnimationFrame(() => $('review-title')?.focus({ preventScroll: true }));
}

function renderProviderStatus() {
    const target = $('provider-status');
    const text = $('provider-status-text');
    if (!target || !text) return;
    const unavailable = currentConfig?.tts_engine === 'xunfei' && currentConfig.xunfei_available === false;
    const provider = currentWorkspace?.provider || authoritativeWorkspace()?.provider || null;
    const providerStatus = String(provider?.status || '').toUpperCase();
    const providerUnavailable = ['UNAVAILABLE', 'DISABLED'].includes(providerStatus);
    const providerCanStartGeneration = provider?.can_start_generation === true;
    const ready = $('service-state')?.classList.contains('is-ready')
        && !unavailable
        && !providerUnavailable
        && (provider ? provider.can_generate !== false && provider.ready !== false : true);
    const actionButton = $('provider-action-btn');
    target.classList.toggle('is-ready', ready);
    target.classList.toggle('is-error', unavailable || providerUnavailable);
    target.classList.toggle('is-warning', !ready && !unavailable && !providerUnavailable);
    const providerMessage = String(provider?.reason || '').trim();
    text.textContent = unavailable
        ? '依赖未就绪，请先安装浏览器运行环境'
        : providerMessage || (ready ? '已连接，可提交生成任务' : '等待生成服务连接');
    if (actionButton) {
        // For the legacy browser provider, Generate is the user-visible
        // login/reconnect entry point. Do not expose a no-op "reconnect"
        // button beside it when the provider explicitly allows that path.
        actionButton.hidden = (ready || providerCanStartGeneration) && !unavailable;
        actionButton.disabled = isRestarting || isParsing || sourceImportInFlight;
        actionButton.textContent = providerStatus === 'EXPIRED' || providerStatus === 'LOGIN_REQUIRED'
            ? '重新连接'
            : '重新检测';
    }
}

function renderWorkspaceShell(workspace = currentWorkspace, snapshot = currentSession) {
    const current = normalizedWorkspace(workspace, snapshot);
    if (!current && !currentSession) {
        const badge = $('task-status-badge');
        if (badge) {
            badge.className = 'task-status-badge is-neutral';
            badge.textContent = '待导入';
            badge.title = '';
        }
        if ($('toolbar-counts')) $('toolbar-counts').textContent = '';
        const progressBar = $('progress-bar');
        if (progressBar) {
            setProgressBarPercent(0);
            progressBar.parentElement?.setAttribute('aria-valuenow', '0');
            progressBar.parentElement?.setAttribute('aria-valuetext', '0% 处理中');
        }
        if ($('progress-percent')) $('progress-percent').textContent = '0';
        setProgressReadoutMode(false);
        if ($('progress-stats')) $('progress-stats').textContent = '准备中...';
        if ($('generation-live-status')) $('generation-live-status').textContent = '等待导入文档';
        setProgressIndeterminate(false);
        delete document.body.dataset.workflowState;
        delete document.body.dataset.generationState;
        updateGenerationControlUI({ available_actions: [] });
        renderProviderStatus();
        return;
    }
    if (!current) {
        // A new session is assigned before its first authoritative workspace
        // snapshot arrives. Clear the previous task's banner/progress during
        // that gap so stale actions cannot appear to belong to the new task.
        const badge = $('task-status-badge');
        if (badge) {
            badge.className = 'task-status-badge is-neutral';
            badge.textContent = '同步中';
            badge.title = '正在读取任务状态';
        }
        if ($('toolbar-counts')) $('toolbar-counts').textContent = '';
        const progressBar = $('progress-bar');
        if (progressBar) {
            setProgressBarPercent(0);
            progressBar.parentElement?.setAttribute('aria-valuenow', '0');
            progressBar.parentElement?.setAttribute('aria-valuetext', '0% 处理中');
        }
        if ($('progress-percent')) $('progress-percent').textContent = '0';
        setProgressReadoutMode(false);
        if ($('progress-stats')) $('progress-stats').textContent = '正在同步任务状态…';
        if ($('generation-live-status')) $('generation-live-status').textContent = '正在同步任务状态…';
        setProgressIndeterminate(false);
        document.body.dataset.workflowState = 'SYNCING';
        document.body.dataset.generationState = 'SYNCING';
        updateGenerationControlUI({ available_actions: [] });
        renderProviderStatus();
        return;
    }
    const state = workspaceUserState(current, current.snapshot || snapshot);
    const progress = workspaceProgress(current);
    renderWorkspaceProgress(current, state);
    renderGenerationViewState(current, state, progress);
    const badge = $('task-status-badge');
    if (badge) {
        badge.className = `task-status-badge is-${state.tone || 'info'}`;
        badge.textContent = state.label;
        badge.title = state.description || '';
    }
    const counts = $('toolbar-counts');
    if (counts) {
        const issueSummary = generationProgressIssueSummary(progress);
        counts.textContent = progress.total > 0
            ? `${progress.completed}/${progress.total} 已完成 · ${progress.deliverable} 可交付${issueSummary ? ` · ${issueSummary}` : ''}`
            : '';
    }
    document.body.dataset.workflowState = state.key || '';
    renderProviderStatus();
    updateConfigActionState(current);
    updateGenerationControlUI(current);
    if (generationResult === 'error' && transientGenerationErrorMessage && activeWorkspace === 'generation') {
        syncTransientGenerationErrorShell(transientGenerationErrorMessage);
    }
}

async function hydrateWorkflowWorkspace(workflowId = currentSession?.session_id, { snapshot = null, silent = true, adoptConfiguration = false } = {}) {
    if (!workflowApi || !workflowId || typeof workflowApi.getWorkspace !== 'function') return null;
    if (workspaceRefreshInFlight?.workflowId === String(workflowId)) return workspaceRefreshInFlight.promise;
    const promise = (async () => {
        try {
            const workspace = await workflowApi.getWorkspace(workflowId);
            if (!workspace || currentSession?.session_id !== workflowId) return null;
            const incomingSnapshot = workspace.snapshot || snapshot;
            if (!workflowSnapshotBelongsToSession(incomingSnapshot, currentSession)) return null;
            const stale = workflowSnapshotIsStaleForSession(incomingSnapshot, currentSession);
            const effectiveSnapshot = stale
                ? latestWorkflowSnapshotForSession(currentSession, incomingSnapshot)
                : incomingSnapshot;
            const effectiveWorkspace = stale
                ? (
                    currentWorkspace
                    && String(currentWorkspace?.snapshot?.workflow_id || '') === String(workflowId)
                        ? currentWorkspace
                        : { ...workspace, snapshot: { ...effectiveSnapshot } }
                )
                : workspace;
            currentWorkspace = effectiveWorkspace;
            mergeWorkflowSnapshotIntoSession(effectiveSnapshot, currentSession);
            if (Array.isArray(workspace.items)) {
                currentSession.parse_results = workspaceItemsToParseResults(workspace);
            }
            if (adoptConfiguration && effectiveWorkspace.configuration?.effective) {
                applyConfigToForm(effectiveWorkspace.configuration.effective, { includeRoles: true });
            }
            workflowStore?.hydrate?.(effectiveWorkspace, { snapshot: effectiveSnapshot });
            renderWorkspaceShell(effectiveWorkspace, effectiveSnapshot);
            renderContentReview(currentSession?.parse_results);
            // A workspace refresh can finish after the user has returned from
            // generation. Re-assert the nested step view here so an older
            // async response cannot leave both page-2 workspaces hidden.
            if (currentView === 'workflow' && currentStep === 2) {
                const stepView = activeWorkspace === 'review' ? 'review' : 'voice';
                setActiveWorkspaceView(stepView);
                if (stepView === 'voice') renderVoiceWorkspace();
            }
            return effectiveWorkspace;
        } catch (error) {
            if (!silent) showToast(workflowAdapter.issueMessage?.(error)?.message || '任务工作区暂时无法同步', 'error');
            return null;
        } finally {
            if (workspaceRefreshInFlight?.workflowId === String(workflowId)) workspaceRefreshInFlight = null;
        }
    })();
    workspaceRefreshInFlight = { workflowId: String(workflowId), promise };
    return promise;
}

function scheduleWorkspaceRefresh(workflowId = currentSession?.session_id) {
    if (!workflowId || !workflowApi) return;
    clearTimeout(workspaceRefreshTimer);
    workspaceRefreshTimer = setTimeout(() => {
        workspaceRefreshTimer = null;
        void hydrateWorkflowWorkspace(workflowId);
    }, 220);
}

async function performWorkspaceAction(action) {
    if (!action || action.enabled !== true || !currentSession?.session_id || !workflowApi) return false;
    const type = String(action.type || '');
    if (type === 'OPEN_VIEW') {
        const view = workspaceUserState(currentWorkspace, currentSession).view;
        if (view === 'delivery') goToStep(4);
        else if (view === 'issues') goToStep(3);
        else if (view === 'voice') goToStep(2);
        return true;
    }
    if (type === 'GENERATE') {
        goToStep(3);
        void startProcessing(false);
        return true;
    }
    if (type === 'DOWNLOAD_ZIP') {
        goToStep(4);
        return true;
    }
    const commandMap = {
        PAUSE: 'pause',
        RESUME: 'resume',
        CANCEL: 'cancel',
        RETRY: 'retry',
        ARCHIVE: 'archive',
        EXPORT_ZIP: 'export-zip',
        RERUN: 'rerun',
    };
    const command = commandMap[type];
    if (!command) return false;
    const key = `${currentSession.session_id}:${type}`;
    if (workflowStore?.getState?.().pendingCommands?.[key]) return false;
    if (workflowCommandCoordinator) {
        try {
            const outcome = await workflowCommandCoordinator.run(action, {
                reason: 'desktop-workspace-action',
            });
            if (!outcome.ok) {
                if (outcome.reason === 'action-disabled-after-refresh') {
                    showToast('任务状态已变化，已刷新最新操作。', 'warning');
                } else if (outcome.reason === 'workspace-refresh-failed') {
                    showToast('任务状态刷新失败，请稍后重试。', 'warning');
                }
                return false;
            }
            if (type === 'RERUN') {
                const rerunSnapshot = outcome.response?.workflow || outcome.response;
                const nextWorkflowId = String(rerunSnapshot?.workflow_id || '');
                if (!nextWorkflowId || typeof workflowApi.getWorkspace !== 'function') {
                    throw new Error('服务端未返回新的工作流');
                }
                const nextWorkspace = await workflowApi.getWorkspace(nextWorkflowId);
                await adoptWorkflowWorkspace(nextWorkspace, {
                    record: { workflow_id: nextWorkflowId, source_filename: nextWorkspace?.source_filename },
                    reason: '已创建新的生成任务，请确认配置后开始生成',
                });
                showToast('已创建新的生成任务，请确认配置后开始生成');
                return true;
            }
            if (type === 'RESUME' && !isGenerating) {
                adoptResumedGenerationIfNeeded(type, outcome.workspace, outcome.response);
            }
            showToast(type === 'PAUSE'
                ? '已发送暂停请求'
                : type === 'RESUME'
                    ? '已发送恢复请求'
                    : type === 'EXPORT_ZIP'
                        ? '正在整理 ZIP 交付文件'
                        : '操作已提交');
            return true;
        } catch (error) {
            showToast(workflowAdapter.issueMessage?.(error)?.message || `操作失败：${error.message || '请稍后重试'}`, 'error');
            renderWorkspaceShell(currentWorkspace, currentSession);
            return false;
        }
    }
    workflowStore?.markCommandPending?.(key, true);
    try {
        const snapshot = await refreshCurrentWorkflowSnapshot(currentSession);
        if (command === 'rerun' && typeof workflowApi.rerun === 'function') {
            const expectedGroupStateVersion = Number(
                action.expected_group_state_version ?? snapshot?.group_state_version ?? currentSession.group_state_version,
            );
            if (!Number.isInteger(expectedGroupStateVersion) || expectedGroupStateVersion < 0) {
                throw new Error('任务组版本缺失，无法安全重新运行');
            }
            const response = await workflowApi.rerun(currentSession.session_id, {
                expected_group_state_version: expectedGroupStateVersion,
                source_workflow_id: currentSession.session_id,
                reason: 'desktop-workspace-action',
            }, {
                idempotencyKey: `renderer-rerun-${currentSession.session_id}-${expectedGroupStateVersion}`,
            });
            const nextWorkflowId = String(response?.workflow_id || response?.workflow?.workflow_id || '');
            if (!nextWorkflowId) throw new Error('服务端未返回新的工作流');
            const nextWorkspace = await workflowApi.getWorkspace(nextWorkflowId);
            await adoptWorkflowWorkspace(nextWorkspace, {
                record: { workflow_id: nextWorkflowId, source_filename: nextWorkspace?.source_filename },
                reason: '已创建新的生成任务，请确认配置后开始生成',
            });
            showToast('已创建新的生成任务，请确认配置后开始生成');
            return true;
        }
        const body = {
            expected_state_version: Number(snapshot?.state_version ?? currentSession.state_version ?? 0),
        };
        if (command === 'retry') {
            if (action.target) body.target = action.target;
            const targetVersion = Number(action.expected_target_state_version);
            if (Number.isInteger(targetVersion) && targetVersion >= 0) {
                body.expected_target_state_version = targetVersion;
            }
            if (action.expected_attempt_id) body.expected_attempt_id = String(action.expected_attempt_id);
            body.reason = 'desktop-workspace-action';
        } else if (command !== 'export-zip') {
            body.reason = 'desktop-workspace-action';
        }
        const response = await workflowApi.sendCommand(currentSession.session_id, command, body, {
            idempotencyKey: `renderer-${command}-${currentSession.session_id}-${Number(snapshot?.state_version || currentSession.state_version || 0)}`,
        });
        mergeWorkflowSnapshotIntoSession(response?.current_snapshot || response, currentSession);
        const refreshedWorkspace = await hydrateWorkflowWorkspace(currentSession.session_id, { silent: false });
        if (type === 'RESUME' && !isGenerating) {
            adoptResumedGenerationIfNeeded(type, refreshedWorkspace, response);
        }
        showToast(type === 'PAUSE'
            ? '已发送暂停请求'
            : type === 'RESUME'
                ? '已发送恢复请求'
                : '操作已提交');
        return true;
    } catch (error) {
        showToast(workflowAdapter.issueMessage?.(error)?.message || `操作失败：${error.message || '请稍后重试'}`, 'error');
        return false;
    } finally {
        workflowStore?.markCommandPending?.(key, false);
        renderWorkspaceShell(currentWorkspace, currentSession);
    }
}

async function runFreshWorkspaceAction(actionType) {
    if (!workflowApi || !currentSession?.session_id) return false;
    const workspace = await hydrateWorkflowWorkspace(currentSession.session_id, {
        silent: false,
    });
    // A failed refresh must not fall back to the cached action: sending that
    // action is exactly how an old expected_state_version produced the
    // expected/current conflict shown by the UI.
    if (!workspace) return false;
    const action = workspaceAction(actionType, workspace);
    if (!action || action.enabled !== true) {
        showToast('任务状态已刷新，当前不可执行该操作。', 'warning');
        return false;
    }
    return performWorkspaceAction(action);
}

async function rerunResultContext(context = activeResultContext) {
    const workspace = context?.workspace || null;
    const workflowId = String(context?.workflowId || context?.recordId || '');
    const action = workflowAdapter.action?.(workspace, 'RERUN');
    if (!workflowId || action?.enabled !== true || typeof workflowApi?.rerun !== 'function') {
        showToast(action?.reason || '当前任务暂时不能重新运行', 'warning');
        return false;
    }
    const expectedGroupStateVersion = Number(
        action.expected_group_state_version ?? workspace?.snapshot?.group_state_version,
    );
    if (!Number.isInteger(expectedGroupStateVersion) || expectedGroupStateVersion < 0) {
        showToast('任务组版本缺失，无法安全重新运行', 'error');
        return false;
    }
    try {
        const rerun = await workflowApi.rerun(workflowId, {
            expected_group_state_version: expectedGroupStateVersion,
            source_workflow_id: workflowId,
            reason: 'desktop-result-rerun',
        }, { idempotencyKey: `renderer-rerun-${workflowId}-${expectedGroupStateVersion}` });
        const nextWorkflowId = String(rerun?.workflow_id || '');
        if (!nextWorkflowId) throw new Error('服务端未返回新的工作流');
        const nextWorkspace = await workflowApi.getWorkspace(nextWorkflowId);
        await adoptWorkflowWorkspace(nextWorkspace, {
            record: { workflow_id: nextWorkflowId, source_filename: nextWorkspace?.source_filename || context.sourceFilename },
            reason: '已创建新的生成任务，请确认配置后开始生成',
        });
        showToast('已创建新的生成任务，请确认配置后开始生成');
        return true;
    } catch (error) {
        console.error('重新运行任务失败:', error);
        showToast(workflowAdapter.issueMessage?.(error, '重新运行未完成')?.message || '重新运行未完成', 'error');
        return false;
    }
}

function syncRestartButtonState(sourceBusy = null) {
    const restartBtn = $('restart-btn');
    if (!restartBtn) return;
    const busy = sourceBusy === null
        ? Boolean(isParsing || sourceImportInFlight)
        : Boolean(sourceBusy);
    restartBtn.disabled = Boolean(isRestarting || busy);
}

function setUploadParsing(parsing) {
    const uploadZone = $('upload-zone');
    if (!uploadZone) return;
    const active = Boolean(parsing || isParsing || sourceImportInFlight);
    uploadZone.classList.toggle('is-processing', active);
    uploadZone.setAttribute('aria-busy', active ? 'true' : 'false');
    syncRestartButtonState(active);
    const historyNav = $('history-nav-btn');
    if (historyNav) historyNav.disabled = active || isGenerating || isRestarting;
    const cancelButton = $('cancel-import-btn');
    if (cancelButton) {
        cancelButton.hidden = !active || !sourceImportController;
        cancelButton.disabled = !active || !sourceImportController;
        cancelButton.setAttribute('aria-busy', active && sourceImportController ? 'true' : 'false');
        cancelButton.textContent = sourceImportId ? '停止等待' : '停止导入';
    }
}

function formatSourceBytes(value) {
    const bytes = Math.max(0, Number(value) || 0);
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function updateSourceImportProgress(stage = '', completed = null, total = null) {
    const panel = $('upload-progress');
    const label = $('upload-progress-label');
    const value = $('upload-progress-value');
    const progress = $('upload-progress-bar');
    if (!panel || !label || !value || !progress) return;
    if (!stage) {
        panel.hidden = true;
        progress.value = 0;
        value.textContent = '0%';
        return;
    }
    panel.hidden = false;
    label.textContent = stage;
    const hasTotal = Number.isFinite(Number(total)) && Number(total) > 0;
    const percent = hasTotal
        ? Math.min(100, Math.max(0, Math.floor((Number(completed) || 0) * 100 / Number(total))))
        : null;
    if (percent === null) {
        progress.removeAttribute('value');
        value.textContent = '处理中';
    } else {
        progress.value = percent;
        value.textContent = `${percent}%`;
    }
}

function createAbortError(message = 'source import was cancelled') {
    const error = new Error(message);
    error.name = 'AbortError';
    error.code = 'USER_CANCELLED';
    return error;
}

function throwIfSourceImportAborted(signal) {
    if (signal?.aborted) throw createAbortError();
}

async function abortSourceImportIfPossible(importId, reason = 'desktop-user-cancel') {
    if (!workflowApi || !importId || typeof workflowApi.abortSourceImport !== 'function') return false;
    try {
        const current = await workflowApi.getSourceImport(importId);
        const status = String(current?.current_status || current?.status || '');
        if (['READY', 'ABORTED', 'FAILED', 'EXPIRED'].includes(status)) return false;
        const stateVersion = Number(current?.state_version);
        if (!Number.isInteger(stateVersion) || stateVersion < 0) return false;
        await workflowApi.abortSourceImport(importId, {
            expected_state_version: stateVersion,
            reason,
        }, { idempotencyKey: `renderer-abort-source-${importId}` });
        return true;
    } catch (error) {
        // A write may have crossed the READY fence while the user cancelled.
        // In that case the source remains immutable and the local parser wait
        // is still stopped; never report it as a successful server abort.
        if (error?.code !== 'STATE_CONFLICT') {
            console.warn('停止源文件导入未能完成服务端收尾:', error);
        }
        return false;
    }
}

async function cancelSourceImport() {
    const controller = sourceImportController;
    if (!controller) return;
    const hadServerImport = Boolean(sourceImportId);
    const cancelButton = $('cancel-import-btn');
    if (cancelButton) {
        cancelButton.disabled = true;
        cancelButton.textContent = '正在停止…';
        cancelButton.setAttribute('aria-busy', 'true');
    }
    setUploadFeedback('info', sourceImportId ? '正在停止等待，并核对源文件状态…' : '正在停止导入…');
    updateSourceImportProgress('正在停止导入…');
    controller.abort();
    const stagingId = sourceStagingUploadId;
    sourceStagingUploadId = null;
    if (stagingId && typeof window.electronAPI?.sourceUpload?.abort === 'function') {
        await window.electronAPI.sourceUpload.abort(stagingId).catch(() => {});
    }
    if (sourceImportId) await abortSourceImportIfPossible(sourceImportId);
    sourceTransportUploadId = null;
    $('status-text').textContent = hadServerImport
        ? '已停止等待；请确认源文件状态后再重新导入'
        : '已停止导入，可重新选择文档';
}

function setUploadFeedback(state = '', message = '') {
    const feedback = $('upload-feedback');
    const uploadZone = $('upload-zone');
    if (!feedback || !uploadZone) return;
    feedback.classList.remove('is-info', 'is-success', 'is-error');
    uploadZone.classList.remove('has-error');
    if (!message) {
        feedback.hidden = true;
        feedback.textContent = '';
        feedback.setAttribute('role', 'status');
        return;
    }
    feedback.hidden = false;
    feedback.textContent = message;
    if (state) feedback.classList.add(`is-${state}`);
    if (state === 'error') {
        uploadZone.classList.add('has-error');
        feedback.setAttribute('role', 'alert');
    } else {
        feedback.setAttribute('role', 'status');
    }
}

function updateConfigSummary() {
    updateGenerationModeUI(selectedGenerationMode());
    const paramsTarget = $('summary-params');
    if (paramsTarget) {
        const params = activeVoiceParams();
        paramsTarget.textContent = `${params.rate} / ${params.pitch} / ${params.volume}`;
    }

    // 音色摘要：显示当前默认男女声，角色音色在音色工作区逐个配置。
    const voiceEl = $('summary-voice');
    if (voiceEl) {
        const roleCount = Math.max(0, voiceRoles.length - 2);
        voiceEl.textContent = `女 ${voiceDisplayName(selectedDefaultFemaleVoice)} · 男 ${voiceDisplayName(selectedDefaultMaleVoice)}${roleCount ? ` · ${roleCount} 个角色` : ''}`;
    }

    const output = $('summary-output');
    if (output) {
        const format = $('format') ? $('format').value.toUpperCase() : 'MP3';
        const quality = $('quality') ? $('quality').value : '128 kbps（标准）';
        const qualityShort = quality.match(/^(\d+\s*kbps)/)?.[1] || quality;
        output.textContent = `${format} · ${qualityShort}`;
    }

    const scope = $('summary-scope');
    if (scope) scope.textContent = $('preview')?.checked ? '试听前 3 条' : '完整文档';
    updateSessionLabels(currentSession?.source_filename || '', currentSession?.parse_results);
}

function enforceOutputCompatibility() {
    const format = $('format');
    if (!format) return;
    // 输出格式固定为 MP3；质量只代表 MP3 码率，不再驱动格式切换。
    // 不依赖旧页面是否存在 MP3 option，直接重建唯一选项，杜绝 WAV 视觉回退。
    if (format.tagName === 'SELECT') {
        const option = document.createElement('option');
        option.value = 'mp3';
        option.textContent = 'MP3 · 通用格式';
        option.selected = true;
        format.replaceChildren(option);
        format.value = 'mp3';
        format.disabled = true;
        window.WordTTSUI?.syncSelect(format);
    } else {
        format.textContent = 'MP3 · 通用格式';
        format.dataset.format = 'mp3';
    }
    updateConfigSummary();
}

const GENERATION_RECOVERY_EXECUTION_STATES = new Set([
    'BLOCKED',
    'WAITING_RETRY',
    'WAITING_USER',
    'FAILED',
]);

function generationRecoveryMessage(workspace, state, progress, { transientMessage = transientGenerationErrorMessage } = {}) {
    const current = workspace || {};
    const snapshot = current.snapshot || {};
    const blocker = typeof workflowAdapter.blockerSummary === 'function'
        ? workflowAdapter.blockerSummary(current)
        : null;
    const failedItem = (Array.isArray(current.items) ? current.items : [])
        .find(item => String(item?.status || '').toUpperCase() === 'FAILED');
    const explicitMessage = [
        snapshot.last_error_message,
        current.last_error_message,
        currentSession?.last_error_message,
        blocker?.message,
        failedItem?.user_message,
    ].map(value => String(value || '').trim()).find(Boolean);
    if (explicitMessage) {
        if (blocker?.title && explicitMessage === String(blocker.message || '').trim()) {
            return `${blocker.title}：${explicitMessage}`;
        }
        return explicitMessage;
    }

    const transient = String(transientMessage || '').trim();
    if (transient) return transient;

    const failed = Math.max(0, Math.round(Number(progress?.failed) || 0));
    const key = String(state?.key || '').toUpperCase();
    if (key === 'WAITING_RETRY') {
        return failed > 0
            ? `生成过程已中断，有 ${failed} 条内容未完成；任务已停在安全重试点，可点击“重试生成”继续。`
            : '生成过程已中断，任务已停在安全重试点，可点击“重试生成”继续。';
    }
    if (key === 'WAITING_USER') {
        return failed > 0
            ? `有 ${failed} 条内容未完成，任务正在等待处理；请先查看任务记录再决定下一步。`
            : '任务正在等待人工处理；请先查看任务记录再决定下一步。';
    }
    if (key === 'BLOCKED') return '任务被阻塞，当前自动化流程已经停止；请查看任务记录中的处理原因。';
    if (key === 'FAILED') return '生成任务未能完成；任务记录已保留，请返回声音配置后重新生成。';
    if (failed > 0) return `当前有 ${failed} 条内容未完成，任务记录已保留；请等待状态收敛后再重试。`;
    return '生成任务未能继续，请重试或返回声音配置调整设置。';
}

function generationRecoveryPresentation(
    workspace = currentWorkspace,
    state = null,
    {
        generationResultState = generationResult,
        transientMessage = transientGenerationErrorMessage,
    } = {},
) {
    const current = workspace || {};
    const resolvedState = state || workspaceUserState(current, current.snapshot || currentSession);
    const key = String(resolvedState?.key || '').toUpperCase();
    const progress = workspaceProgress(current);
    const snapshot = current.snapshot || {};
    const control = String(snapshot.control_state || '').toUpperCase();
    const terminal = Boolean(resolvedState?.terminal || isTerminalWorkflowSnapshot(snapshot));
    const blocker = typeof workflowAdapter.blockerSummary === 'function'
        ? workflowAdapter.blockerSummary(current)
        : null;
    const hasFailure = Math.max(0, Number(progress?.failed) || 0) > 0;
    const hasPersistedError = Boolean(
        String(snapshot.last_error_message || current.last_error_message || currentSession?.last_error_message || '').trim(),
    );
    const hasIssueState = GENERATION_RECOVERY_EXECUTION_STATES.has(key);
    const isTransientError = String(generationResultState || '').toLowerCase() === 'error';
    const hasTransientError = Boolean(String(transientMessage || '').trim());
    const hardStopped = isHardStoppedWorkflowSnapshot(snapshot);

    // A pause/stop control state owns the page until its command settles. Do
    // not let an old failed-item count reopen an error panel over that state.
    if (hardStopped || ['PAUSE_REQUESTED', 'PAUSED', 'RESUME_REQUESTED', 'TERMINATING'].includes(control)) {
        return null;
    }
    if (!isTransientError && !hasTransientError && !hasIssueState && !hasPersistedError && !blocker && !hasFailure) return null;

    const retryAction = workspaceAction('RETRY', current);
    // A transient renderer error is only retryable while the workflow is
    // still in the startup/accepted execution window. Once the server has
    // projected WAITING_RETRY/WAITING_USER without an enabled RETRY action,
    // showing a button would create a misleading no-op recovery panel.
    const transientRetryAllowed = isTransientError
        && ['CREATED', 'PREPARING', 'RUNNING', 'RECOVERING'].includes(key);
    // Terminal runs are immutable; their retry action belongs to the result
    // page (or a fresh rerun), not this live-generation recovery panel.
    const retryVisible = !terminal
        && (retryAction?.enabled === true || transientRetryAllowed);
    const title = key === 'WAITING_RETRY'
        ? '任务已中断，可重试'
        : key === 'WAITING_USER'
            ? '任务需要处理'
            : key === 'BLOCKED'
                ? '任务被阻塞'
                : key === 'FAILED'
                    ? '生成失败'
                    : '生成异常';
    return {
        key: key || 'ERROR',
        title,
        message: generationRecoveryMessage(current, resolvedState, progress, { transientMessage }),
        retryVisible,
        retryLabel: key === 'WAITING_RETRY' ? '重试生成' : '重试生成',
        returnVisible: true,
    };
}

function generationRecoveryIsSuppressed({
    generationActive = isGenerating,
    retryInFlight = generationRecoveryRetryInFlight,
} = {}) {
    return Boolean(generationActive || retryInFlight);
}

function syncGenerationRecoveryState(workspace = currentWorkspace, state = null, progress = null) {
    // A recovery card describes an idle, actionable failure. Once a retry has
    // started, an old persisted error must not remain above the new progress
    // view while the retry handshake is still settling.
    if (generationRecoveryIsSuppressed()) {
        hideGenerationRecovery();
        return null;
    }
    const presentation = generationRecoveryPresentation(workspace, state, {
        generationResultState: generationResult,
        transientMessage: transientGenerationErrorMessage,
    });
    if (!presentation) {
        hideGenerationRecovery();
        return null;
    }
    // An accepted RECOVERING/RUNNING task is already owned by the background
    // worker.  The recovery card may still be useful as an explanation of the
    // previous failure, but its retry button would be a no-op while the live
    // task is being adopted.  Only expose the action once the server has
    // settled into a retryable state and the renderer is idle.
    const retryVisible = presentation.retryVisible
        && !isGenerating
        && !generationStartInFlight;
    showGenerationRecovery(presentation.message, {
        title: presentation.title,
        retryVisible,
        retryLabel: presentation.retryLabel,
        returnVisible: presentation.returnVisible,
    });
    return presentation;
}

function showGenerationRecovery(message, {
    title = '生成异常',
    retryVisible = true,
    retryLabel = '重试生成',
    returnVisible = true,
    returnLabel = '返回声音配置',
} = {}) {
    const panel = $('generation-recovery');
    const titleEl = $('generation-recovery-title');
    const messageEl = $('generation-error-message');
    if (titleEl) titleEl.textContent = title;
    if (messageEl) {
        messageEl.textContent = message || '生成任务未能继续，请重试或返回声音配置。';
    }
    if (panel) panel.hidden = false;
    const retryButton = $('retry-generation-btn');
    if (retryButton) {
        retryButton.hidden = !retryVisible;
        retryButton.disabled = !retryVisible;
        retryButton.textContent = retryLabel;
    }
    const returnButton = $('return-config-btn');
    if (returnButton) {
        returnButton.hidden = !returnVisible;
        returnButton.disabled = !returnVisible;
        returnButton.textContent = returnLabel;
    }
}

function hideGenerationRecovery() {
    const panel = $('generation-recovery');
    if (panel) panel.hidden = true;
    const titleEl = $('generation-recovery-title');
    if (titleEl) titleEl.textContent = '生成异常';
    const retryButton = $('retry-generation-btn');
    if (retryButton) {
        retryButton.hidden = false;
        retryButton.disabled = false;
        retryButton.textContent = '重试生成';
    }
    const returnButton = $('return-config-btn');
    if (returnButton) {
        returnButton.hidden = false;
        returnButton.disabled = false;
        returnButton.textContent = '返回声音配置';
    }
}

function syncTransientGenerationErrorShell(message) {
    if (!message || currentView !== 'workflow' || activeWorkspace !== 'generation') return;
    const badge = $('task-status-badge');
    if (badge) {
        badge.className = 'task-status-badge is-danger';
        badge.textContent = '生成异常';
        badge.title = message;
    }
    document.body.dataset.workflowState = 'FAILED';
    document.body.dataset.generationState = 'FAILED';
}

function clearGenerationStartupTimer() {
    if (generationStartupTimer) {
        clearTimeout(generationStartupTimer);
        generationStartupTimer = null;
    }
}

function setProgressIndeterminate(enabled) {
    const bar = $('progress-bar');
    const track = bar?.parentElement;
    bar?.classList.toggle('is-indeterminate', Boolean(enabled));
    if (track) track.setAttribute('aria-busy', enabled ? 'true' : 'false');
}

function clearSSEReconnectTimer() {
    if (sseReconnectTimer) {
        clearTimeout(sseReconnectTimer);
        sseReconnectTimer = null;
    }
    if (sseStableTimer) {
        clearTimeout(sseStableTimer);
        sseStableTimer = null;
    }
}

function destroyWaveSurfers() {
    waveformRenderToken++;
    audioPlayRequestToken++;
    if (waveformObserver) {
        waveformObserver.disconnect();
        waveformObserver = null;
    }
    waveformItems.forEach(item => {
        item.cancelWaveformLoad?.(false);
        item.resetAudioSource?.();
    });
    waveformItems = [];
    waveformQueue = [];
    waveformLoadsActive = 0;
    wavesurferInstances.forEach(ws => {
        try { ws.destroy(); } catch (e) { /* ignore */ }
    });
    wavesurferInstances = [];
    audioElements.forEach(audio => {
        try {
            audio.pause();
            audio.removeAttribute('src');
            audio.load();
        } catch (e) { /* ignore */ }
    });
    audioElements = [];
    artifactObjectUrls.forEach(url => {
        try { URL.revokeObjectURL(url); } catch (_) { /* ignore */ }
    });
    artifactObjectUrls.clear();
    clearVoiceAssetObjectUrls();
    voiceAssetCacheReady.clear();
    currentPlayingAudio = null;
}

function pumpWaveformQueue() {
    while (waveformLoadsActive < WAVEFORM_MAX_CONCURRENT && waveformQueue.length > 0) {
        const item = waveformQueue.shift();
        if (!item) continue;
        item._waveformQueued = false;
        if (!item.isConnected || item._waveformInitialized || item._waveformFailed) continue;

        const token = waveformRenderToken;
        let settled = false;
        const release = () => {
            if (settled) return;
            settled = true;
            if (token !== waveformRenderToken) return;
            waveformLoadsActive = Math.max(0, waveformLoadsActive - 1);
            pumpWaveformQueue();
        };

        waveformLoadsActive++;
        const instance = item.initializeWaveform?.(release);
        if (!instance) release();
    }
}

function queueWaveformInitialization(item, prioritize = false) {
    if (!item || item._waveformInitialized || item._waveformQueued || item._waveformFailed) return;
    item._waveformQueued = true;
    if (prioritize) waveformQueue.unshift(item);
    else waveformQueue.push(item);
    pumpWaveformQueue();
}

function activateResultWaveforms() {
    if (!$('page-4')?.classList.contains('active') || waveformItems.length === 0) return;
    if (waveformObserver) waveformObserver.disconnect();

    const scrollRoot = $('page-4')?.querySelector('.page-scroll') || null;
    if (typeof IntersectionObserver === 'function') {
        waveformObserver = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (!entry.isIntersecting) return;
                waveformObserver?.unobserve(entry.target);
                queueWaveformInitialization(entry.target);
            });
        }, { root: scrollRoot, rootMargin: '420px 0px', threshold: 0.01 });
        waveformItems.forEach(item => {
            if (item.isConnected) waveformObserver.observe(item);
        });
    }

    // 页面显示、容器获得真实宽度后优先预热首条；其余条目按可见区域串行解码。
    const firstVisible = waveformItems.find(item => item.isConnected && item.getBoundingClientRect().width > 1);
    if (firstVisible) queueWaveformInitialization(firstVisible, true);
}

function setGenerationVisualState(state) {
    const animation = $('gen-animation');
    const badge = $('processing-badge');
    const badgeLabel = $('processing-badge-label');
    const logDot = document.querySelector('.log-live-dot');
    const liveStatus = $('generation-live-status');
    const liveLabelText = $('generation-live-label-text');
    [animation, badge, logDot].forEach(el => {
        if (el) el.classList.remove('is-error', 'is-stopped', 'is-done', 'is-paused');
    });
    if (animation && state !== 'done') animation.classList.remove('done');

    const labels = {
        running: '任务进行中',
        paused: '已暂停',
        done: '处理完成',
        error: '需要处理',
        warning: '部分完成',
        stopped: '任务已停止',
    };
    if (badgeLabel) badgeLabel.textContent = labels[state] || labels.running;
    const liveLabels = {
        running: '批量任务进行中',
        paused: '任务已暂停，可恢复执行',
        done: '批量任务已完成',
        warning: '任务完成，部分内容需处理',
        error: '生成遇到问题，请检查记录',
        stopped: '任务已停止',
    };
    if (liveStatus) liveStatus.textContent = liveLabels[state] || liveLabels.running;
    const liveLabelTexts = {
        running: '当前阶段',
        paused: '任务已暂停',
        done: '任务完成',
        warning: '部分完成',
        error: '生成异常',
        stopped: '任务停止',
    };
    if (liveLabelText) liveLabelText.textContent = liveLabelTexts[state] || liveLabelTexts.running;

    if (state === 'paused') {
        animation?.classList.add('is-paused');
        badge?.classList.add('is-paused');
        logDot?.classList.add('is-paused');
    } else if (state === 'done') {
        animation?.classList.add('done');
        badge?.classList.add('is-done');
        logDot?.classList.add('is-done');
    } else if (state === 'error') {
        animation?.classList.add('is-error');
        badge?.classList.add('is-error');
        logDot?.classList.add('is-error');
    } else if (state === 'stopped' || state === 'warning') {
        animation?.classList.add('is-stopped');
        badge?.classList.add('is-stopped');
        logDot?.classList.add('is-stopped');
    }
    updateGenerationCancelUI();
}

function generationProgressTotal(progress, workspace = currentWorkspace) {
    return Math.max(
        0,
        Math.round(
            Number(progress?.total)
            || Number(workspace?.progress?.total)
            || summarizeParseResults(currentSession?.parse_results).total
            || Number(lastStats?.total)
            || 0,
        ),
    );
}

function generationProgressPercentForView(presentation, progress, total) {
    if (presentation.terminal) {
        return terminalProgressPercent(progress?.deliverable, total);
    }
    // The server's aggregate percent counts failures/cancellations as
    // processed work. That is useful for throughput, but it is misleading as
    // the generation progress bar: a failed 1/1 run must not look 99% done.
    // During an active/recovering run, keep the bar aligned with the completed
    // count shown below it. Terminal states use verified deliverables above.
    if (total > 0) {
        const completed = Math.max(0, Math.round(Number(progress?.completed) || 0));
        return Math.min(99, Math.round((Math.min(completed, total) / total) * 100));
    }
    return Math.min(99, Math.max(0, Math.round(Number(progress?.percent) || 0)));
}

function generationProgressIssueSummary(progress = {}) {
    const counts = [
        ['pending', '待处理'],
        ['failed', '失败'],
        ['cancelled', '已取消'],
        ['skipped', '已跳过'],
    ];
    return counts
        .map(([key, label]) => [Math.max(0, Math.round(Number(progress?.[key]) || 0)), label])
        .filter(([count]) => count > 0)
        .map(([count, label]) => `${count} 条${label}`)
        .join(' · ');
}

function generationProgressCopy(presentation, progress, total) {
    const countTotal = total;
    const countCompleted = Math.max(0, Math.round(Number(progress?.completed) || 0));
    const count = countTotal > 0 ? `${countCompleted} / ${countTotal}` : '等待计数';
    const issueSummary = generationProgressIssueSummary(progress);
    return `${presentation.progressStatus} · ${count}${issueSummary ? ` · ${issueSummary}` : ''}`;
}

function generationProgressAriaText(presentation, percent) {
    if (presentation?.terminal) return `${percent}% 可交付`;
    const status = {
        CREATED: '等待开始',
        PAUSE_REQUESTED: '正在暂停',
        PAUSED: '已暂停',
        RESUME_REQUESTED: '正在恢复',
        TERMINATING: '正在停止',
        BLOCKED: '需要处理',
        WAITING_RETRY: '等待重试',
        WAITING_USER: '等待处理',
    }[presentation?.key] || '处理中';
    return `${percent}% ${status}`;
}

function generationFileStatusText(presentation, progress, workspace = currentWorkspace) {
    const source = workspace?.source_filename || currentSession?.source_filename || '当前文档';
    const total = generationProgressTotal(progress, workspace);
    const completed = Math.max(0, Math.round(Number(progress?.completed) || 0));
    const count = total > 0 ? `${completed}/${total}` : `${completed}`;
    const status = {
        PAUSE_REQUESTED: `已收到「${source}」的暂停请求 · 当前进度 ${count}，等待安全暂停点。`,
        PAUSED: `「${source}」已暂停 · 当前进度 ${count}，点击“恢复任务”继续。`,
        RESUME_REQUESTED: `正在恢复「${source}」· 当前进度 ${count}，请稍候。`,
        TERMINATING: `正在停止「${source}」· 当前进度 ${count}，正在保存已完成内容。`,
        CANCELLED: `已取消「${source}」的生成任务 · 已完成 ${count}。`,
        FAILED: `「${source}」生成遇到问题 · 已完成 ${count}，可查看记录处理。`,
        BLOCKED: `「${source}」需要处理 · 已完成 ${count}，请查看任务记录。`,
        WAITING_RETRY: `「${source}」正在等待重试 · 已完成 ${count}。`,
        WAITING_USER: `「${source}」正在等待处理 · 已完成 ${count}。`,
    };
    return status[presentation.key] || '';
}

function restoreGenerationFileLabel(workspace = currentWorkspace) {
    if (!currentSession?.source_filename) return;
    const parseTotal = summarizeParseResults(currentSession.parse_results).total;
    const preview = Boolean(lastGenerationConfig?.preview);
    const previewLimit = Number(lastGenerationConfig?.preview_limit) || 3;
    const total = preview ? Math.min(parseTotal, previewLimit) : parseTotal;
    updateSessionLabels(currentSession.source_filename, currentSession.parse_results, {
        preview,
        total: total || Number(workspace?.progress?.total) || parseTotal,
    });
}

function syncGenerationControlStage(presentation) {
    if (!['PAUSE_REQUESTED', 'PAUSED', 'RESUME_REQUESTED', 'TERMINATING'].includes(presentation?.key)) return;
    const currentStage = LOG_STAGE_ORDER?.[Math.max(0, logStageIndex)] || 'synthesize';
    const stage = currentStage === 'complete' ? 'synthesize' : currentStage;
    logStageStates.set(stage, 'warning');
    renderLogStageRail();
}

function renderGenerationViewState(workspace = currentWorkspace, state = null, progress = null) {
    const current = workspace || currentWorkspace || {};
    const resolvedState = state || workspaceUserState(current, current?.snapshot || currentSession);
    const runtimeMessage = current?.snapshot?.runtime?.message
        || current?.runtime?.message
        || '';
    const pendingPause = pendingWorkspaceCommand('PAUSE');
    const pendingResume = pendingWorkspaceCommand('RESUME');
    const presentation = generationStatePresentation(resolvedState, {
        runtimeMessage,
        pendingPause,
        pendingResume,
        pendingCancel: generationCancelRequested || Boolean(cancelWorkflowPromise),
    });
    const viewProgress = progress || workspaceProgress(current);
    const total = generationProgressTotal(viewProgress, current);
    const percent = generationProgressPercentForView(presentation, viewProgress, total);
    const previousKey = document.body.dataset.generationState || '';
    const staticStatusKeys = new Set([
        'PAUSE_REQUESTED', 'PAUSED', 'RESUME_REQUESTED', 'TERMINATING',
        'BLOCKED', 'WAITING_RETRY', 'WAITING_USER', 'CANCELLED', 'FAILED',
    ]);

    document.body.dataset.generationState = presentation.key;
    setGenerationVisualState(presentation.visualState);
    if ($('gen-title')) $('gen-title').textContent = presentation.title;
    if ($('processing-badge-label')) $('processing-badge-label').textContent = presentation.badge;
    if ($('generation-live-label-text')) $('generation-live-label-text').textContent = presentation.liveLabel;
    if ($('generation-live-status')) $('generation-live-status').textContent = presentation.liveStatus;
    if ($('status-text')) $('status-text').textContent = presentation.liveStatus;
    if ($('generation-active-note')) $('generation-active-note').textContent = presentation.note;

    const pigCopy = {
        CREATED: ['READY', '等待开始生成'],
        PREPARING: ['PREPARING', '正在准备音频任务'],
        RUNNING: ['ON AIR', '她正在把文字念成声音'],
        RECOVERING: ['RECOVERING', '正在恢复音频任务'],
        PAUSE_REQUESTED: ['PAUSING', '正在安全暂停任务'],
        PAUSED: ['PAUSED', '任务已暂停，等待恢复'],
        RESUME_REQUESTED: ['RESUMING', '正在恢复音频任务'],
        TERMINATING: ['STOPPING', '正在停止音频任务'],
        SUCCEEDED: ['READY', '音频已经准备好'],
        PARTIAL_SUCCESS: ['REVIEW', '部分音频需要处理'],
        FAILED: ['CHECK', '生成遇到问题'],
        CANCELLED: ['STOPPED', '任务已取消'],
        BLOCKED: ['CHECK', '任务需要处理'],
        WAITING_RETRY: ['WAITING', '等待重试'],
        WAITING_USER: ['CHECK', '等待人工处理'],
        CLOSED: ['ARCHIVED', '任务已归档'],
    }[presentation.key] || ['VOICE', '正在处理文字'];
    if ($('generation-v2-pig-status')) $('generation-v2-pig-status').textContent = pigCopy[0];
    if ($('generation-v2-pig-message')) $('generation-v2-pig-message').textContent = pigCopy[1];

    const activeDot = $('generation-active-dot');
    const activeDotLabel = $('generation-active-dot-label');
    const liveLabel = $('generation-live-label');
    const paused = ['PAUSE_REQUESTED', 'PAUSED', 'RESUME_REQUESTED'].includes(presentation.key);
    const stopped = ['TERMINATING', 'CANCELLED'].includes(presentation.key);
    activeDot?.classList.toggle('is-paused', paused);
    activeDot?.classList.toggle('is-stopped', stopped);
    liveLabel?.classList.toggle('is-paused', paused);
    liveLabel?.classList.toggle('is-stopped', stopped);
    if (activeDotLabel) activeDotLabel.textContent = presentation.key === 'RUNNING' ? '实时' : presentation.liveLabel;

    if (staticStatusKeys.has(presentation.key) || presentation.terminal) {
        const fileStatus = generationFileStatusText(presentation, viewProgress, current);
        if (fileStatus && $('generation-file-name')) $('generation-file-name').textContent = fileStatus;
    } else if (['PREPARING', 'RUNNING', 'RECOVERING'].includes(presentation.key)
        && staticStatusKeys.has(previousKey)) {
        restoreGenerationFileLabel(current);
    }

    if ($('progress-percent')) $('progress-percent').textContent = String(percent);
    if ($('progress-bar')) setProgressBarPercent(percent);
    const progressTrack = $('progress-bar')?.parentElement;
    progressTrack?.setAttribute('aria-valuenow', String(percent));
    progressTrack?.setAttribute('aria-valuetext', generationProgressAriaText(presentation, percent));
    setProgressIndeterminate(presentation.indeterminate);
    setProgressReadoutMode(
        presentation.terminal,
        presentation.key === 'PARTIAL_SUCCESS'
            || presentation.key === 'FAILED'
            || presentation.key === 'CANCELLED',
    );
    if (presentation.freezeProgress || !lastStats || ['PREPARING', 'RECOVERING'].includes(presentation.key)) {
        if ($('progress-stats')) $('progress-stats').textContent = generationProgressCopy(presentation, viewProgress, total);
    }
    syncGenerationControlStage(presentation);
    syncGenerationRecoveryState(current, resolvedState, viewProgress);
    updateGenerationControlUI(current);
    return presentation;
}

function setAppInteractive(enabled) {
    const effectiveEnabled = enabled && !isRestarting;
    const uploadZone = $('upload-zone');
    if (uploadZone) {
        uploadZone.classList.toggle('is-disabled', !effectiveEnabled);
        uploadZone.setAttribute('aria-disabled', effectiveEnabled ? 'false' : 'true');
        uploadZone.tabIndex = effectiveEnabled ? 0 : -1;
    }
    ['start-generate-btn', 'skip-config-btn'].forEach(id => {
        const button = $(id);
        if (button) button.disabled = !effectiveEnabled;
    });
    renderProviderStatus();
    updateConfigActionState(currentWorkspace);
}

/**
 * 将服务端工作流快照合并到当前会话。
 *
 * 生成期间事件流可能先送达一个较早的快照；状态版本只能向前推进，
 * 不能让旧快照把已经拿到的版本回退，否则用户返回配置后重试会把
 * stale expected_state_version 提交给后端。
 */
function workflowSnapshotIsOlder(candidate, reference) {
    if (!candidate || !reference) return false;
    const candidateWorkflowId = String(candidate.workflow_id || '');
    const referenceWorkflowId = String(reference.workflow_id || reference.session_id || '');
    if (candidateWorkflowId && referenceWorkflowId && candidateWorkflowId !== referenceWorkflowId) return false;

    const candidateSeq = Number(candidate.latest_seq);
    const referenceSeq = Number(reference.latest_seq);
    if (
        Number.isInteger(candidateSeq) && candidateSeq >= 0
        && Number.isInteger(referenceSeq) && referenceSeq >= 0
        && candidateSeq < referenceSeq
    ) return true;

    const candidateVersion = Number(candidate.state_version);
    const referenceVersion = Number(reference.state_version);
    return Number.isInteger(candidateVersion) && candidateVersion >= 0
        && Number.isInteger(referenceVersion) && referenceVersion >= 0
        && candidateVersion < referenceVersion;
}

function workflowSnapshotBelongsToSession(snapshot, session = currentSession) {
    if (!snapshot || !session) return true;
    const snapshotWorkflowId = String(snapshot.workflow_id || '');
    const sessionWorkflowId = String(session.session_id || session.workflow_id || '');
    return !snapshotWorkflowId || !sessionWorkflowId || snapshotWorkflowId === sessionWorkflowId;
}

function workflowSnapshotIsStaleForSession(candidate, session = currentSession) {
    if (!candidate || !session) return false;
    if (!workflowSnapshotBelongsToSession(candidate, session)) return true;
    if (workflowSnapshotIsOlder(candidate, session)) return true;
    const workspaceSnapshot = currentWorkspace?.snapshot;
    return Boolean(
        workspaceSnapshot
        && String(workspaceSnapshot.workflow_id || '') === String(session.session_id || '')
        && workflowSnapshotIsOlder(candidate, workspaceSnapshot)
    );
}

function latestWorkflowSnapshotForSession(session = currentSession, fallback = null) {
    const workspaceSnapshot = currentWorkspace?.snapshot;
    if (
        workspaceSnapshot
        && (!session?.session_id || String(workspaceSnapshot.workflow_id || '') === String(session.session_id))
        && !workflowSnapshotIsOlder(workspaceSnapshot, session)
    ) return workspaceSnapshot;
    if (session) {
        return {
            ...session,
            workflow_id: session.workflow_id || session.session_id || fallback?.workflow_id,
        };
    }
    return fallback;
}

function mergeWorkflowSnapshotIntoSession(snapshot, session = currentSession) {
    if (!snapshot || !session) return session;
    if (!workflowSnapshotBelongsToSession(snapshot, session)) return session;
    if (workflowSnapshotIsOlder(snapshot, session)) return session;
    const snapshotVersion = Number(snapshot.state_version);
    if (Number.isInteger(snapshotVersion) && snapshotVersion >= 0) {
        session.state_version = snapshotVersion;
    }
    ['execution_state', 'control_state', 'result_status', 'cleanup_state', 'source_artifact_id', 'latest_event_id', 'latest_seq'].forEach((key) => {
        if (snapshot[key] !== undefined && snapshot[key] !== null) session[key] = snapshot[key];
    });
    ['last_error_code', 'last_error_message'].forEach((key) => {
        if (Object.prototype.hasOwnProperty.call(snapshot, key)) {
            session[key] = snapshot[key] === null || snapshot[key] === undefined
                ? null
                : String(snapshot[key]);
        }
    });
    const snapshotGroupVersion = Number(snapshot.group_state_version);
    const sessionGroupVersion = Number(session.group_state_version);
    if (Number.isInteger(snapshotGroupVersion) && snapshotGroupVersion >= 0
        && (!Number.isInteger(sessionGroupVersion) || snapshotGroupVersion >= sessionGroupVersion)) {
        session.group_state_version = snapshotGroupVersion;
    }
    return session;
}

function renderLiveWorkflowSnapshot(snapshot, session = currentSession) {
    if (!snapshot || !session || currentSession?.session_id !== session.session_id) return;
    if (!workflowSnapshotBelongsToSession(snapshot, session)) return;
    const stale = workflowSnapshotIsStaleForSession(snapshot, session);
    mergeWorkflowSnapshotIntoSession(snapshot, session);
    const effectiveSnapshot = stale
        ? latestWorkflowSnapshotForSession(session, snapshot)
        : snapshot;
    const workspaceId = String(currentWorkspace?.snapshot?.workflow_id || '');
    if (workspaceId === String(session.session_id)) {
        currentWorkspace = { ...currentWorkspace, snapshot: { ...effectiveSnapshot } };
    }
    // Cancellation returns a terminal local snapshot. Keep the badge and
    // action buttons in sync before the next SSE refresh.
    renderWorkspaceShell(currentWorkspace, effectiveSnapshot);
}

/**
 * 在会话恢复/重新生成前读取一次权威工作流快照。
 * 这个请求不替代后端的乐观锁，只负责避免渲染器继续使用中断前缓存的
 * state_version；真正的命令仍然必须带 expected_state_version 到后端校验。
 */
async function refreshCurrentWorkflowSnapshot(session = currentSession) {
    if (!workflowApi || !session?.session_id) return null;
    const snapshot = await workflowApi.getWorkflow(session.session_id);
    if (currentSession?.session_id !== session.session_id) return null;
    if (!workflowSnapshotBelongsToSession(snapshot, session)) return null;
    const stale = workflowSnapshotIsStaleForSession(snapshot, session);
    mergeWorkflowSnapshotIntoSession(snapshot, session);
    const effectiveSnapshot = stale
        ? latestWorkflowSnapshotForSession(session, snapshot)
        : snapshot;
    if (!stale && currentWorkspace && currentWorkspace.snapshot) {
        // A server workspace snapshot is authoritative.  Replacing this
        // nested object matters when the server clears a blocker/runtime
        // field; merging would resurrect the stale value from the previous
        // snapshot.
        currentWorkspace = { ...currentWorkspace, snapshot: { ...snapshot } };
    }
    renderWorkspaceShell(currentWorkspace, effectiveSnapshot);
    return effectiveSnapshot;
}

const ACCEPTED_GENERATION_EXECUTION_STATES = new Set([
    'RUNNING',
    'RECOVERING',
]);
const TERMINAL_WORKFLOW_RESULT_STATES = new Set([
    'SUCCEEDED',
    'PARTIAL_SUCCESS',
    'FAILED',
    'CANCELLED',
]);

function isTerminalWorkflowSnapshot(snapshot) {
    return Boolean(
        snapshot
        && String(snapshot.execution_state || '') === 'TERMINAL'
        && String(snapshot.control_state || '') === 'TERMINATED'
        && TERMINAL_WORKFLOW_RESULT_STATES.has(String(snapshot.result_status || ''))
    );
}

function isHardStoppedWorkflowSnapshot(snapshot) {
    if (!isTerminalWorkflowSnapshot(snapshot)) return false;
    const latestEventType = String(
        snapshot?.latest_event?.event_type
        || snapshot?.latest_event_type
        || '',
    ).toUpperCase();
    return String(snapshot?.result_status || '') === 'CANCELLED'
        || String(snapshot?.last_error_code || '') === 'WORKFLOW_CANCELLED'
        || latestEventType === 'WORKFLOW_CANCELLED';
}

function isAcceptedGenerationSnapshot(snapshot) {
    if (!snapshot || isTerminalWorkflowSnapshot(snapshot)) return false;
    const executionState = String(snapshot.execution_state || '');
    if (ACCEPTED_GENERATION_EXECUTION_STATES.has(executionState)) return true;
    if (String(snapshot.control_state || '') === 'TERMINATING') return true;
    if (executionState !== 'PREPARING') return false;

    // Parsing also uses PREPARING, so do not cancel a normal editable parse
    // draft. A prepared TTS run has a frozen configuration, and a recovered
    // active-list candidate carries the durable generation-accepted marker.
    const workflowId = String(snapshot.workflow_id || '');
    const workspace = currentWorkspace
        && String(currentWorkspace?.snapshot?.workflow_id || '') === workflowId
        ? currentWorkspace
        : null;
    const frozenFields = workspace?.configuration?.frozen_fields;
    const candidateAccepted = currentSession?.session_id === workflowId
        && currentSession?.active_candidate?.generation_accepted === true;
    return (Array.isArray(frozenFields) && frozenFields.length > 0) || candidateAccepted;
}

function isCancellationSettledSnapshot(snapshot) {
    return isTerminalWorkflowSnapshot(snapshot);
}

/**
 * The ZIP Artifact is created on demand.  A terminal result with at least one
 * verified audio file must therefore keep the ZIP action visible even before
 * the first export has been materialized.
 */
function resultZipState(context, resultCount) {
    const delivery = context?.workspace?.delivery || context?.delivery || null;
    const hasAuthoritativeDelivery = Boolean(
        delivery && typeof delivery === 'object'
        && ('zip_available' in delivery || 'zip_artifact_id' in delivery),
    );
    const authoritativeArtifactId = hasAuthoritativeDelivery
        ? (delivery.zip_available === true ? delivery.zip_artifact_id : null)
        : (context?.zipArtifactId || null);
    const hasArtifact = Boolean(
        authoritativeArtifactId
        && (hasAuthoritativeDelivery ? delivery.zip_available === true : context?.zipAvailable === true),
    );
    const count = Number(resultCount);
    const hasDeliverableAudio = Number.isFinite(count) && count > 0;
    const scopeRequired = Boolean(context && ('delivery' in context || 'workspace' in context));
    const hasScope = Boolean(
        delivery
        && Array.isArray(delivery.included_item_ids)
        && Array.isArray(delivery.excluded_item_ids)
        && delivery.exclusion_reasons
        && typeof delivery.exclusion_reasons === 'object',
    );
    const terminal = String(context?.executionState || '') === 'TERMINAL'
        || TERMINAL_WORKFLOW_RESULT_STATES.has(String(context?.resultStatus || ''));
    return {
        // Legacy callers that only know terminal + file count keep the old
        // projection; the real result context always carries a workspace or
        // delivery object and therefore must pass the range contract.
        visible: hasArtifact || (hasDeliverableAudio && terminal && (!scopeRequired || hasScope)),
        ready: hasArtifact,
    };
}

function setGenerationControlLabel(button, text) {
    if (!button) return;
    const label = button.querySelector('.generation-control-label');
    if (label) label.textContent = text;
    else button.textContent = text;
}

function updateGenerationCancelUI() {
    const button = $('cancel-generation-btn');
    if (!button) return;
    const cancelAction = workspaceAction('CANCEL', currentWorkspace);
    const projectionAllowsCancel = cancelAction?.enabled === true;
    const sessionActive = Boolean(
        currentSession?.session_id
        && !isTerminalWorkflowSnapshot(currentSession)
        && (
            projectionAllowsCancel
            || isGenerating
            || generationStartInFlight
            || cancelWorkflowPromise
        )
    );
    button.hidden = !sessionActive;
    button.disabled = Boolean(cancelWorkflowPromise) || isRestarting
        || (!projectionAllowsCancel && !isGenerating && !generationStartInFlight);
    const cancelLabel = cancelWorkflowPromise ? '正在停止…' : '停止生成';
    setGenerationControlLabel(button, cancelLabel);
    button.setAttribute('aria-label', cancelWorkflowPromise ? '正在停止生成' : '停止生成并结束本次任务');
    if (cancelWorkflowPromise) button.setAttribute('aria-busy', 'true');
    else button.removeAttribute('aria-busy');
    if (cancelAction?.reason && !cancelAction.enabled) button.title = cancelAction.reason;
    else button.title = cancelWorkflowPromise ? '正在等待任务停止' : '停止生成并结束本次任务';
    updateGenerationControlUI(currentWorkspace);
}

function updateConfigActionState(workspace = currentWorkspace) {
    const button = $('start-generate-btn');
    if (!button) return;
    const action = workspaceAction('GENERATE', workspace);
    const hasAuthoritativeAction = Boolean(workspace && Array.isArray(workspace.available_actions));
    const serviceReady = $('service-state')?.classList.contains('is-ready') !== false;
    // A user stop intentionally leaves the old run terminal and immutable,
    // but the voice form must remain the explicit entry point for creating a
    // fresh rerun. startProcessing() performs that rerun fence before saving
    // the current voice configuration, so the button is allowed here even
    // though the old workspace's GENERATE action is disabled.
    const hardStoppedAwaitingRerun = isHardStoppedWorkflowSnapshot(workspace?.snapshot || workspace)
        && activeWorkspace === 'voice'
        && !isGenerating
        && !generationStartInFlight;
    const blockedByWorkspace = hasAuthoritativeAction
        && action?.enabled !== true
        && !hardStoppedAwaitingRerun;
    const disabled = !currentSession?.session_id
        || !workspace
        || isRestarting
        || isParsing
        || isGenerating
        || generationStartInFlight
        || !serviceReady
        || blockedByWorkspace;
    button.disabled = disabled;
    button.setAttribute('aria-disabled', disabled ? 'true' : 'false');
    if (hardStoppedAwaitingRerun) {
        button.title = '当前生成已停止；确认声音配置后可重新生成';
    } else if (blockedByWorkspace) {
        button.title = action?.reason || '当前任务状态不允许生成';
    } else if (!serviceReady) {
        button.title = '等待生成服务连接';
    } else {
        button.removeAttribute('title');
    }
}

function updateGenerationControlUI(workspace = currentWorkspace) {
    const current = workspace || {};
    const state = workspaceUserState(current, currentSession);
    const presentation = generationStatePresentation(state, {
        pendingPause: pendingWorkspaceCommand('PAUSE'),
        pendingResume: pendingWorkspaceCommand('RESUME'),
        pendingCancel: generationCancelRequested || Boolean(cancelWorkflowPromise),
    });
    const pending = workflowStore?.getState?.().pendingCommands || {};
    const sessionId = currentSession?.session_id || '';
    const setActionButton = (id, type, visibleWhen = true) => {
        const button = $(id);
        if (!button) return;
        const action = workspaceAction(type, current);
        const commandKey = `${sessionId}:${type}`;
        const visible = Boolean(sessionId && visibleWhen && action && action.enabled === true && !presentation.terminal);
        button.hidden = !visible;
        button.disabled = !visible || Boolean(pending[commandKey]) || isRestarting;
        button.setAttribute('aria-busy', pending[commandKey] ? 'true' : 'false');
        const label = type === 'PAUSE'
            ? (pending[commandKey] ? '正在暂停…' : '暂停生成')
            : (pending[commandKey] ? '正在恢复…' : '恢复生成');
        setGenerationControlLabel(button, label);
        button.setAttribute('aria-label', pending[commandKey]
            ? (type === 'PAUSE' ? '正在暂停生成' : '正在恢复生成')
            : (type === 'PAUSE' ? '暂停生成并保留当前进度' : '恢复生成并继续当前任务'));
        if (visible) button.title = pending[commandKey]
            ? (type === 'PAUSE' ? '正在等待任务进入暂停状态' : '正在等待任务恢复')
            : (action.reason || (type === 'PAUSE' ? '暂停生成并保留当前进度' : '恢复生成并继续当前任务'));
    };
    setActionButton('pause-generation-btn', 'PAUSE', presentation.key === 'PREPARING' || presentation.key === 'RUNNING' || presentation.key === 'RECOVERING');
    setActionButton('resume-generation-btn', 'RESUME', presentation.key === 'PAUSED' || presentation.key === 'PAUSE_REQUESTED');
    const controlBar = $('generation-task-controls');
    if (controlBar) {
        const cancelButton = $('cancel-generation-btn');
        const cancelAction = workspaceAction('CANCEL', current);
        const hasVisibleControl = ['pause-generation-btn', 'resume-generation-btn']
            .some(id => $(id) && !$(id).hidden)
            || Boolean(
                sessionId
                && cancelButton
                && !cancelButton.hidden
                && (cancelAction?.enabled === true || isGenerating || generationStartInFlight || cancelWorkflowPromise),
            );
        controlBar.hidden = !hasVisibleControl;
    }
    updateConfigActionState(current);
}

/**
 * 接管已经被接受的工作流，而不是把它误当成仍可编辑的草稿。
 * 这是启动竞态和自动重试抢先启动两条路径共用的收敛点。
 */
function adoptAcceptedGeneration(session, snapshot, { reason = '任务已在后台运行' } = {}) {
    if (
        !session?.session_id
        || currentSession?.session_id !== session.session_id
        || !isAcceptedGenerationSnapshot(snapshot)
    ) return false;

    mergeWorkflowSnapshotIntoSession(snapshot, session);
    clearGenerationStartupTimer();
    generateAbortController = null;
    generationResult = null;
    transientGenerationErrorMessage = '';
    isGenerating = true;
    lastWorkspaceRenderKey = '';
    const presentation = renderGenerationViewState(
        currentWorkspace,
        workspaceUserState(currentWorkspace, snapshot),
        workspaceProgress(currentWorkspace),
    );

    // Keep the adoption reason for an actively running task, but let the
    // authoritative presentation own paused/stopping/error states. This is
    // important when history opens an already-paused workflow: it must stay
    // visibly paused and must not restart an indeterminate progress animation.
    if (reason && ['PREPARING', 'RUNNING', 'RECOVERING'].includes(presentation?.key)) {
        $('generation-live-status').textContent = reason;
        $('status-text').textContent = reason;
        if (!lastStats) {
            const total = summarizeParseResults(session.parse_results).total;
            $('progress-stats').textContent = `${reason} · 0 / ${total || '—'}`;
        }
    }
    updateGenerationCancelUI();
    void connectSSE(session.session_id);
    return true;
}

function resumedGenerationSnapshot(workspace, response) {
    const snapshot = workspace?.snapshot
        || response?.current_snapshot
        || response?.workflow
        || response?.snapshot
        || response;
    return snapshot && typeof snapshot === 'object' ? snapshot : null;
}

function shouldAdoptResumedGeneration(
    type,
    workspace,
    response,
    { generationActive = isGenerating, startInFlight = generationStartInFlight } = {},
) {
    if (String(type || '').toUpperCase() !== 'RESUME' || generationActive || startInFlight) return false;
    const snapshot = resumedGenerationSnapshot(workspace, response);
    if (String(snapshot?.control_state || '').toUpperCase() !== 'RUNNING') return false;
    return isAcceptedGenerationSnapshot(snapshot);
}

function adoptResumedGenerationIfNeeded(type, workspace, response) {
    if (!shouldAdoptResumedGeneration(type, workspace, response)) return false;
    const snapshot = resumedGenerationSnapshot(workspace, response);
    if (!snapshot || !currentSession?.session_id) return false;
    const workflowId = String(snapshot.workflow_id || '');
    if (workflowId && workflowId === String(currentSession.session_id)) {
        if (workspace?.snapshot) {
            currentWorkspace = workspace;
        } else if (currentWorkspace) {
            currentWorkspace = {
                ...currentWorkspace,
                snapshot: { ...(currentWorkspace.snapshot || {}), ...snapshot },
            };
        }
    }
    return adoptAcceptedGeneration(currentSession, snapshot, {
        reason: '任务已恢复，正在接管生成进度',
    });
}

function workflowProgressCounts(snapshot, session = currentSession) {
    const progress = snapshot?.progress || session?.progress || {};
    const total = nonNegativeCount(progress.total, summarizeParseResults(session?.parse_results).total);
    const completed = nonNegativeCount(progress.completed, generatedFiles.length);
    const failed = nonNegativeCount(progress.failed);
    const cancelled = nonNegativeCount(progress.cancelled);
    return {
        total: Math.max(0, total),
        completed: Math.max(0, Math.min(completed, total || completed)),
        failed: Math.max(0, failed),
        cancelled: Math.max(0, cancelled),
    };
}

/**
 * 发出一次幂等取消命令。后端会立即返回本地终态；浏览器 worker 即使
 * 之后才退出，也不能再发布结果。
 */
async function cancelCurrentWorkflow(session = currentSession, {
    reason = 'desktop-user-cancel',
} = {}) {
    if (!workflowApi || !session?.session_id) return null;
    if (cancelWorkflowPromise) return cancelWorkflowPromise;

    const sessionId = session.session_id;
    const idempotencyKey = `renderer-cancel-${sessionId}-${generationAttemptId}`;
    generationCancelRequested = true;
    clearGenerationStartupTimer();
    if (generateAbortController) {
        generateAbortController.abort();
        generateAbortController = null;
        // 使尚未返回的 generate/patch 请求不能在取消命令之后继续接管页面。
        generationAttemptId++;
    }
    renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
    updateGenerationCancelUI();

    const operation = (async () => {
        let snapshot = await workflowApi.getWorkflow(sessionId);
        renderLiveWorkflowSnapshot(snapshot, session);
        if (isTerminalWorkflowSnapshot(snapshot)) return snapshot;

        // Cancellation is a local terminalization command, not a projected
        // workspace action. Always send it with the freshest version so a
        // stale/partial workspace cannot hide the only decisive stop path.
        let commandAttempts = 0;
        while (!isTerminalWorkflowSnapshot(snapshot) && commandAttempts < 2) {
            try {
                const response = await workflowApi.sendCommand(
                    sessionId,
                    'cancel',
                    {
                        expected_state_version: Number(snapshot.state_version || session.state_version || 0),
                        reason,
                    },
                    { idempotencyKey },
                );
                snapshot = response?.current_snapshot || response || snapshot;
                renderLiveWorkflowSnapshot(snapshot, session);
                break;
            } catch (error) {
                if (error?.code !== 'STATE_CONFLICT' || commandAttempts >= 1) throw error;
                snapshot = await workflowApi.getWorkflow(sessionId);
                renderLiveWorkflowSnapshot(snapshot, session);
            }
            commandAttempts++;
        }
        return snapshot;
    })();
    cancelWorkflowPromise = operation;
    try {
        return await operation;
    } finally {
        if (cancelWorkflowPromise === operation) cancelWorkflowPromise = null;
        generationCancelRequested = false;
        updateGenerationCancelUI();
    }
}

function resetGenerationAfterHardStop(session = currentSession, snapshot = currentWorkspace?.snapshot) {
    if (!session?.session_id || currentSession?.session_id !== session.session_id) return false;
    if (!isHardStoppedWorkflowSnapshot(snapshot)) return false;

    const alreadyAtVoiceConfig = activeWorkspace === 'voice'
        && currentStep === 2
        && !isGenerating
        && generationResult === null;
    if (!alreadyAtVoiceConfig) {
        // Invalidate every renderer-side generation callback and tear down the
        // browser-facing stream. The server terminal snapshot remains in
        // history, but no late runtime event may put this task back on the
        // generation page.
        generationAttemptId++;
        generationStartAttemptId = 0;
        generationStartInFlight = false;
        generateAbortController?.abort();
        generateAbortController = null;
        clearGenerationStartupTimer();
        clearSSEReconnectTimer();
        sseConnectionToken++;
        if (resultNavigationTimer) {
            clearTimeout(resultNavigationTimer);
            resultNavigationTimer = null;
        }
        if (workspaceRefreshTimer) {
            clearTimeout(workspaceRefreshTimer);
            workspaceRefreshTimer = null;
        }
        if (workflowStream) {
            workflowStream.close().catch(() => {});
            workflowStream = null;
        }
        destroyWaveSurfers();

        generatedFiles = [];
        activeResultContext = null;
        latestCurrentResultEvent = null;
        lastStats = null;
        lastDownloadEvent = null;
        sseRetryCount = 0;
        generationResult = null;
        transientGenerationErrorMessage = '';
        generationCancelRequested = false;
        isGenerating = false;
        resetLogTimeline('当前生成已停止；请确认声音配置后重新生成。');
        hideGenerationRecovery();

        mergeWorkflowSnapshotIntoSession(snapshot, session);
        if (currentWorkspace?.snapshot?.workflow_id === session.session_id) {
            currentWorkspace = { ...currentWorkspace, snapshot: { ...snapshot } };
        }
    }

    // A cancelled workflow is immutable. Keep its document and voice choices
    // in memory so the next click on “开始生成” can create a fresh rerun,
    // while making the voice step the only reachable generation entry point.
    goToStep(2);
    setActiveWorkspaceView('voice');
    renderVoiceWorkspace();
    updateGenerationCancelUI();
    updateConfigActionState(currentWorkspace);
    $('status-text').textContent = '当前生成已停止，请确认声音配置后重新生成。';
    if (!alreadyAtVoiceConfig) showToast('已停止生成，请确认声音配置后重新生成。', 'info');
    return true;
}

function applyCancellationOutcome(session, snapshot) {
    if (!snapshot || currentSession?.session_id !== session?.session_id) return;
    renderLiveWorkflowSnapshot(snapshot, session);
    if (hardStopNavigationRequested && isHardStoppedWorkflowSnapshot(snapshot)) {
        resetGenerationAfterHardStop(session, snapshot);
        return;
    }
    if (isTerminalWorkflowSnapshot(snapshot)) {
        // A cancellation response is only a snapshot hint. Re-read the full
        // workspace so item partitions and verified artifacts cannot lag the
        // terminal control/result facts shown on the result page.
        void refreshGeneratedArtifacts(session.session_id).then(() => {
            if (currentSession?.session_id !== session.session_id) return;
            const authoritative = currentWorkspace?.snapshot;
            if (!isTerminalWorkflowSnapshot(authoritative)) {
                $('status-text').textContent = '本地取消状态待刷新…';
                return;
            }
            const counts = workspaceProgress(currentWorkspace);
            if (authoritative.result_status === 'CANCELLED') {
                handleSSEEvent({ type: 'cancelled', ...counts });
            } else if (['SUCCEEDED', 'PARTIAL_SUCCESS'].includes(String(authoritative.result_status || ''))) {
                handleDone({
                    type: 'done',
                    ...counts,
                    file_list: generatedFiles,
                });
            }
        }).catch(() => {
            $('status-text').textContent = '任务已停止，结果刷新失败，请重新打开任务查看。';
        });
        return;
    }
    // The cancel route is expected to return a terminal local snapshot. Keep
    // a conservative fallback for a concurrent state error without implying
    // that another provider request is required.
    isGenerating = true;
    updateGenerationCancelUI();
    $('status-text').textContent = '停止请求未返回终态，请刷新任务。';
    $('generation-live-status').textContent = '正在更新本地任务状态…';
}

async function createEditableWorkflowFromTerminal(session, snapshot) {
    if (!workflowApi || typeof workflowApi.rerun !== 'function') {
        throw new Error('当前运行时不支持创建新的配置任务');
    }
    const expectedGroupStateVersion = Number(snapshot?.group_state_version);
    if (!Number.isInteger(expectedGroupStateVersion) || expectedGroupStateVersion < 0) {
        throw new Error('任务组版本缺失，无法返回可编辑配置');
    }
    const rerun = await workflowApi.rerun(session.session_id, {
        expected_group_state_version: expectedGroupStateVersion,
        source_workflow_id: session.session_id,
        reason: 'desktop-return-to-configuration',
    }, {
        idempotencyKey: `renderer-return-config-rerun-${session.session_id}-${expectedGroupStateVersion}`,
    });
    const nextWorkflowId = String(rerun?.workflow_id || '');
    if (!nextWorkflowId) throw new Error('服务端未返回新的配置任务');
    const nextWorkspace = await workflowApi.getWorkspace(nextWorkflowId);
    if (!nextWorkspace?.snapshot?.workflow_id) throw new Error('新的配置任务工作区不可用');
    await adoptWorkflowWorkspace(nextWorkspace, {
        record: {
            workflow_id: nextWorkflowId,
            source_filename: nextWorkspace.source_filename || session.source_filename,
        },
        reason: '已创建新的配置任务，请确认后重新生成',
    });
    return nextWorkspace;
}

async function returnToConfigSafely({ buttonId = 'return-config-btn' } = {}) {
    const button = $(buttonId);
    if (button?.dataset.busy === 'true') return false;
    if (button) {
        button.dataset.busy = 'true';
        button.disabled = true;
    }
    let session = currentSession;
    try {
        if (!session || !workflowApi) {
            goToStep(2);
            return true;
        }
        let snapshot = await refreshCurrentWorkflowSnapshot(session);
        const mustStop = generationStartInFlight
            || isGenerating
            || isAcceptedGenerationSnapshot(snapshot);
        if (mustStop && !isTerminalWorkflowSnapshot(snapshot)) {
            snapshot = await cancelCurrentWorkflow(session, {
                reason: 'desktop-return-to-configuration',
            });
            applyCancellationOutcome(session, snapshot);
            if (!isTerminalWorkflowSnapshot(snapshot)) {
                showToast('本地停止尚未完成，请刷新任务后重试返回配置', 'warning');
                return false;
            }
        }
        snapshot = await holdAutomaticRetry(session) || snapshot;
        // hold 与调度器之间仍可能发生一次竞态：如果调度器已经把任务
        // 推进到 RUNNING，不能继续打开配置页，而要立即走同一取消路径。
        if (isAcceptedGenerationSnapshot(snapshot)) {
            snapshot = await cancelCurrentWorkflow(session, {
                reason: 'desktop-return-to-configuration-race',
            });
            applyCancellationOutcome(session, snapshot);
            if (!isTerminalWorkflowSnapshot(snapshot)) {
                showToast('返回配置时任务状态发生变化，请刷新后重试', 'warning');
                return false;
            }
        }
        // A terminal run is immutable because it already owns attempts and
        // artifacts. Returning to configuration means starting a fresh run in
        // the same workflow group, not editing history in place.
        if (isTerminalWorkflowSnapshot(snapshot)) {
            const nextWorkspace = await createEditableWorkflowFromTerminal(session, snapshot);
            session = currentSession;
            snapshot = nextWorkspace.snapshot;
            showToast('已创建新的配置任务，请确认后重新生成');
        }
        if (currentSession?.session_id !== session.session_id) return false;
        hideGenerationRecovery();
        goToStep(2);
        // Returning from a terminal generation state must always land on the
        // editable voice workspace. The explicit re-render also covers a
        // workspace refresh that resolved between the cancellation response
        // and the navigation above.
        setActiveWorkspaceView('voice');
        renderVoiceWorkspace();
        return true;
    } catch (error) {
        console.error('返回配置前停止任务失败:', error);
        showToast(`无法停止当前任务：${error.message || '请稍后重试'}`, 'error');
        return false;
    } finally {
        if (button) {
            button.dataset.busy = 'false';
            button.disabled = isRestarting;
        }
        updateGenerationCancelUI();
    }
}

/**
 * Return control of a safe, pre-submission retry to the configuration editor.
 * The scheduler remains enabled for unattended recovery, but it must not race
 * a user who is changing the voice after closing the browser deliberately.
 */
async function holdAutomaticRetry(session = currentSession) {
    if (!workflowApi || !session?.session_id) return null;
    let snapshot = await refreshCurrentWorkflowSnapshot(session);
    for (let attempt = 0; attempt < 2; attempt++) {
        if (!snapshot || !['WAITING_RETRY', 'WAITING_USER'].includes(String(snapshot.execution_state))) {
            return snapshot;
        }
        // Use the version from the same authoritative GET that produced the
        // state predicate. session.state_version may already have been
        // overwritten by an SSE update from the scheduler.
        const expectedStateVersion = Number(snapshot.state_version);
        if (!Number.isInteger(expectedStateVersion) || expectedStateVersion < 0) return snapshot;
        try {
            const response = await workflowApi.holdRetry(session.session_id, {
                expected_state_version: expectedStateVersion,
                reason: 'desktop-return-to-configuration',
            });
            const current = response?.current_snapshot;
            if (current) mergeWorkflowSnapshotIntoSession(current, session);
            return current || snapshot;
        } catch (error) {
            if (error?.code === 'STATE_CONFLICT' && attempt === 0) {
                try {
                    snapshot = await refreshCurrentWorkflowSnapshot(session);
                    continue;
                } catch (_) {
                    // Keep the last authoritative snapshot for the caller's
                    // best-effort navigation decision.
                }
            }
            console.warn('暂停后台自动重试失败:', error);
            return snapshot;
        }
    }
    return snapshot;
}

function verifiedItemIdsFromArtifacts(artifacts) {
    return new Set((Array.isArray(artifacts) ? artifacts : [])
        .filter(artifact => (
            artifact?.item_id
            && artifact.lifecycle_state === 'READY'
            && artifact.verified === true
            && artifact.artifact_type === 'tts-segment'
        ))
        .map(artifact => String(artifact.item_id)));
}

/**
 * Retry only durable local failures from the still-open run. TTS legacy
 * AMBIGUOUS items are normalized to the same local retry path on use.
 */
async function retryFailedItems() {
    const button = $('retry-failed-btn');
    if (!workflowApi || !currentSession || !lastGenerationConfig || isGenerating || isRestarting) return;
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
    }
    const session = currentSession;
    try {
        let snapshot = await holdAutomaticRetry(session);
        if (!snapshot) throw new Error('当前任务状态不可用');
        if (snapshot.execution_state === 'TERMINAL') {
            showToast('当前任务已封存，请返回配置后重新开始生成');
            return;
        }
        if (!['WAITING_RETRY', 'WAITING_USER'].includes(String(snapshot.execution_state))) {
            showToast('旧任务仍在处理，请等待它停止后再重试');
            return;
        }

        const [items, artifacts] = await Promise.all([
            workflowApi.listItems(session.session_id),
            workflowApi.listArtifacts(session.session_id),
        ]);
        const verifiedItemIds = verifiedItemIdsFromArtifacts(artifacts);
        const failedItems = (Array.isArray(items) ? items : [])
            .filter(item => (
                item?.status === 'FAILED'
                && item.item_id
                && !verifiedItemIds.has(String(item.item_id))
            ))
            .sort((left, right) => (
                Number(left.sequence || 0) - Number(right.sequence || 0)
                || String(left.item_id).localeCompare(String(right.item_id))
            ));
        const ambiguousCount = (Array.isArray(items) ? items : [])
            .filter(item => item?.status === 'AMBIGUOUS').length;
        if (failedItems.length === 0) {
            showToast(ambiguousCount > 0
                ? '有未完成条目，请重新生成'
                : '当前没有可安全重试的失败项');
            return;
        }
        const stepId = String(snapshot.current_step_id || '');
        if (!stepId) throw new Error('当前任务缺少可重试的生成步骤');

        const retryableIds = [];
        for (const item of failedItems) {
            const response = await workflowApi.retry(session.session_id, {
                expected_state_version: Number(snapshot.state_version),
                expected_target_state_version: Number(item.state_version),
                target: {
                    target_type: 'ITEM',
                    step_id: stepId,
                    item_id: String(item.item_id),
                },
                reason: 'desktop-retry-failed-items',
            });
            const nextSnapshot = response?.current_snapshot;
            if (nextSnapshot) {
                mergeWorkflowSnapshotIntoSession(nextSnapshot, session);
                snapshot = nextSnapshot;
            }
            retryableIds.push(String(item.item_id));
        }
        if (retryableIds.length === 0) return;

        destroyWaveSurfers();
        goToStep(3);
        showToast(`正在仅重试 ${retryableIds.length} 个失败项`);
        await startProcessing(false, { ...lastGenerationConfig }, retryableIds);
    } catch (error) {
        console.error('失败项重试失败:', error);
        showToast(`失败项重试失败：${error.message || '请检查任务记录'}`, 'error');
    } finally {
        if (button) {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    }
}

async function retryGenerationFromRecovery() {
    if (isGenerating || generationStartInFlight || isRestarting || !currentSession) return;
    const recoveryButton = $('retry-generation-btn');
    if (recoveryButton?.dataset.busy === 'true') return;
    if (recoveryButton) {
        recoveryButton.dataset.busy = 'true';
        recoveryButton.disabled = true;
        recoveryButton.setAttribute('aria-busy', 'true');
    }
    generationRecoveryRetryInFlight = true;
    hideGenerationRecovery();
    const workspace = authoritativeWorkspace() || currentWorkspace;
    const state = workspaceUserState(workspace, workspace?.snapshot || currentSession);
    const retryAction = workspaceAction('RETRY', workspace);

    try {
        // WAITING_RETRY/WAITING_USER expose item-scoped safe retry actions. Use
        // the existing failed-item flow so every target carries its own state
        // version and a mixed result never causes a blind whole-workflow submit.
        if (!state.terminal && retryAction?.enabled === true) {
            await retryFailedItems();
            return;
        }

        // A renderer-side startup error can happen before the server accepts
        // the generation command, so there is no durable RETRY action yet.
        // Reuse the saved voice configuration and perform the normal guarded
        // start.
        if (String(generationResult || '').toLowerCase() === 'error'
            && ['CREATED', 'PREPARING', 'RUNNING', 'RECOVERING'].includes(state.key)) {
            startProcessing(false, lastGenerationConfig || undefined);
            return;
        }

        if (state.terminal && !isHardStoppedWorkflowSnapshot(workspace?.snapshot || currentSession)) {
            await returnToConfigSafely();
            return;
        }

        showToast('当前任务暂时没有可安全重试的内容，请查看任务记录或返回声音配置。', 'warning');
    } finally {
        generationRecoveryRetryInFlight = false;
        if (recoveryButton) {
            recoveryButton.dataset.busy = 'false';
            recoveryButton.removeAttribute('aria-busy');
            recoveryButton.disabled = Boolean(isGenerating || generationStartInFlight);
        }
        // retryFailedItems catches its own API errors. If it never reached a
        // new generation, restore the actionable recovery state after the
        // immediate hide instead of leaving the user without a next step.
        if (!isGenerating && !generationStartInFlight
            && currentView === 'workflow' && activeWorkspace === 'generation') {
            syncGenerationRecoveryState(
                currentWorkspace,
                workspaceUserState(currentWorkspace, currentSession),
            );
        }
    }
}

async function submitGenerationCommand(
    session,
    config,
    controller,
    attemptId,
    itemIds = null,
    configurationRevision = null,
) {
    let expectedConfigurationRevision = Number(configurationRevision);
    // A STATE_CONFLICT retry is still one logical generate command. Keep its
    // idempotency key stable so a response that arrived after the conflict
    // cannot result in a second server-side command.
    const commandIdempotencyKey = `renderer-generate-${session.session_id}-${attemptId}`;
    const refreshConfigurationRevision = async () => {
        const workspace = await workflowApi.getWorkspace(session.session_id);
        const revision = Number(workspace?.configuration?.configuration_revision);
        if (!Number.isInteger(revision) || revision < 1) {
            throw new Error('工作区配置版本缺失，无法安全提交生成任务');
        }
        expectedConfigurationRevision = revision;
    };
    if (!Number.isInteger(expectedConfigurationRevision) || expectedConfigurationRevision < 1) {
        await refreshConfigurationRevision();
    }
    const submit = () => workflowApi.generateWorkflow(session.session_id, {
        expected_state_version: session.state_version,
        configuration_revision: expectedConfigurationRevision,
        reason: 'desktop-renderer',
        ...(Array.isArray(itemIds) && itemIds.length > 0 ? { item_ids: itemIds } : {}),
    }, { idempotencyKey: commandIdempotencyKey });

    try {
        return await submit();
    } catch (error) {
        // GET 与 POST 之间仍可能有后台事件/其它窗口推进版本；只对明确的
        // 乐观锁冲突重新读取一次并重试，绝不对未知网络错误盲目重发。
        if (error?.code !== 'STATE_CONFLICT') throw error;
        await refreshCurrentWorkflowSnapshot(session);
        await refreshConfigurationRevision();
        if (controller.signal.aborted || attemptId !== generationAttemptId || currentSession?.session_id !== session.session_id) {
            const aborted = new Error('generation attempt was superseded');
            aborted.name = 'AbortError';
            throw aborted;
        }
        return submit();
    }
}

function setRestartingUI(restarting) {
    isRestarting = restarting;
    [
        'restart-btn',
        'change-file-btn',
        'back-to-upload-btn',
        'new-file-btn',
        'retry-failed-btn',
        'result-return-config-btn',
        'generate-full-btn',
        'download-zip-btn',
        'retry-service-btn',
        'retry-generation-btn',
        'return-config-btn',
        'pause-generation-btn',
        'resume-generation-btn',
        'cancel-generation-btn',
        'cancel-import-btn',
        'history-nav-btn',
        'history-start-btn',
        'history-back-btn',
        'back-to-history-btn',
        'version-nav-btn',
        'version-check-btn',
        'version-download-btn',
        'version-install-btn',
        'version-open-release-btn',
    ].forEach(id => {
        const button = $(id);
        if (button) button.disabled = restarting;
    });
    syncRestartButtonState();
    if (restarting) {
        setAppInteractive(false);
        $('status-text').textContent = '正在结束当前任务...';
    }
}

async function connectService(showToastOnStart = false) {
    const retryButton = $('retry-service-btn');
    if (retryButton) retryButton.hidden = true;
    setAppInteractive(false);
    setServiceState('', '正在连接服务');
    if (showToastOnStart) showToast('正在连接生成服务...');

    if (isElectron) {
        let ready = false;
        try {
            ready = await window.electronAPI.serverReady();
        } catch (error) {
            console.error('检查生成服务状态失败:', error);
        }
        if (!ready) {
            setServiceState('error', '服务连接失败');
            if (retryButton) retryButton.hidden = false;
            showToast('生成服务启动失败，请重试连接');
            return false;
        }
    }

    const configLoaded = await loadConfig();
    if (!configLoaded) {
        setServiceState('warning', '服务状态异常');
        if (retryButton) retryButton.hidden = false;
        showToast('生成服务暂不可用，请重试连接');
        return false;
    }

    if (currentConfig?.tts_engine === 'xunfei' && currentConfig.xunfei_available === false) {
        setServiceState('error', '讯飞配音依赖未就绪');
        if (retryButton) retryButton.hidden = false;
        showToast('讯飞配音依赖未就绪，请安装 Playwright 浏览器后重试');
        return false;
    }

    setServiceState('ready', '服务已连接');
    setAppInteractive(true);
    if (currentSession?.session_id) void hydrateWorkflowWorkspace(currentSession.session_id);
    return true;
}

// ============================================================================
// 初始化
// ============================================================================

function applyPerformanceMode() {
    const cores = Number(navigator.hardwareConcurrency || 0);
    const memory = Number(navigator.deviceMemory || 0);
    const lowPerformance = (cores > 0 && cores <= 4) || (memory > 0 && memory <= 4);
    document.documentElement.classList.toggle('low-performance', lowPerformance);
}

function resetActivePageScroll() {
    const activePage = document.querySelector('.step-page.active');
    const scrollRoot = activePage?.querySelector('.page-scroll, .page-center');
    if (!scrollRoot) return;
    scrollRoot.scrollTop = 0;
    scrollRoot.scrollLeft = 0;
}

function pinReviewActionsToWindow() {
    const actions = document.querySelector('.review-actions');
    const reviewPage = $('page-2');
    if (!actions || !reviewPage || actions.parentElement === reviewPage) return;
    // Keep the dock outside `.page-scroll`; the CSS fixed positioning then
    // remains tied to the application window instead of the document flow.
    reviewPage.appendChild(actions);
}

async function init() {
    initializeTheme();
    applyPerformanceMode();
    if (platform === 'darwin') {
        document.body.classList.add('platform-darwin');
    } else if (platform === 'win32') {
        document.body.classList.add('platform-win32');
    }

    bindNativeAppNotices();
    void bindNativeAppUpdates();
    pinReviewActionsToWindow();
    bindEvents();

    // 初始化预设 UI
    refreshPresetUI();
    window.WordTTSUI?.enhanceSelects(document);
    // 输出格式是产品固定约束：先清理任何旧版/异常页面残留的格式选项，
    // 再恢复当前配置，避免 MP3 选项缺失时自定义下拉框把第一项显示成 WAV。
    enforceOutputCompatibility();
    const savedConfig = loadCurrentConfig();
    if (savedConfig) {
        applyConfigToForm(savedConfig, { includeRoles: false });
    } else {
        rememberCurrentConfig();
    }

    const connected = await connectService(isElectron);
    updateStepper();
    updateConfigSummary();
    if (connected) {
        await refreshHistoryRecords({ showLoading: false });
        // Startup is intentionally passive: persisted workspaces remain
        // discoverable in history, but opening the app never changes the
        // current page or adopts a task without an explicit user action.
        renderActiveCandidateHint(activeWorkflowCandidates);
    }
    resetActivePageScroll();
}

function bindEvents() {
    $$('[data-workspace]').forEach((entry) => {
        const activate = () => {
            const workspace = entry.dataset.workspace || '';
            if (!isWorkspaceNavigationAllowed(workspace)) {
                const lockReason = workspaceNavigationLockReason(workspace);
                if (lockReason) showToast(lockReason);
                return;
            }
            if (workspace === 'import') goToStep(1);
            else if (workspace === 'review') showContentReview();
            else if (workspace === 'voice') goToStep(2);
            else if (workspace === 'generation') goToStep(3);
            else if (workspace === 'delivery') goToStep(4);
        };
        entry.addEventListener('click', activate);
        entry.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                activate();
            }
        });
    });
    $('theme-toggle')?.addEventListener('click', () => setWorkspaceTheme(themePreference === 'dark' ? 'light' : 'dark'));

    // 重新开始按钮（工具栏）
    $('restart-btn').addEventListener('click', requestRestart);
    $('retry-service-btn').addEventListener('click', async () => {
        const connected = await connectService(true);
        if (connected) await refreshHistoryRecords({ showLoading: currentView === 'history' });
    });
    $('provider-action-btn')?.addEventListener('click', async () => {
        const button = $('provider-action-btn');
        if (button) {
            button.disabled = true;
            button.setAttribute('aria-busy', 'true');
        }
        try {
            const connected = await connectService(true);
            if (connected && currentSession?.session_id) {
                await hydrateWorkflowWorkspace(currentSession.session_id, { silent: false });
            }
        } finally {
            if (button) {
                button.disabled = false;
                button.removeAttribute('aria-busy');
            }
            renderProviderStatus();
        }
    });
    $('history-nav-btn').addEventListener('click', () => showHistoryPage());
    $('version-nav-btn')?.addEventListener('click', () => showVersionPage());
    $('import-history-link')?.addEventListener('click', () => showHistoryPage());
    $('back-to-history-btn').addEventListener('click', () => showHistoryPage());
    $('history-back-btn').addEventListener('click', returnToWorkflow);
    $('history-start-btn').addEventListener('click', returnToWorkflow);
    $('version-check-btn')?.addEventListener('click', event => { void runUpdateAction('check', event.currentTarget); });
    $('version-download-btn')?.addEventListener('click', event => { void runUpdateAction('download', event.currentTarget); });
    $('version-install-btn')?.addEventListener('click', event => { void runUpdateAction('install', event.currentTarget); });
    $('version-open-release-btn')?.addEventListener('click', event => { void openUpdateReleasePage(event.currentTarget); });
    $('update-required-download')?.addEventListener('click', event => { void runUpdateAction('download', event.currentTarget); });
    $('update-required-install')?.addEventListener('click', event => { void runUpdateAction('install', event.currentTarget); });
    $('update-required-retry')?.addEventListener('click', event => { void runUpdateAction('check', event.currentTarget); });
    $('update-required-open-release')?.addEventListener('click', event => { void openUpdateReleasePage(event.currentTarget); });
    $('history-search-input')?.addEventListener('input', event => {
        historyFilters.query = String(event.target.value || '').trim().toLocaleLowerCase('zh-CN');
        renderHistoryRecords(historyRecords);
    });
    $('history-status-filter')?.addEventListener('change', event => {
        historyFilters.status = String(event.target.value || 'all');
        renderHistoryRecords(historyRecords);
    });
    $('history-sort-order')?.addEventListener('change', event => {
        historyFilters.sort = String(event.target.value || 'updated');
        renderHistoryRecords(historyRecords);
    });

    $$('[data-log-filter]').forEach(button => {
        button.addEventListener('click', () => setLogFilter(button.dataset.logFilter || 'all'));
    });
    $('log-follow-btn').addEventListener('click', () => {
        setLogAutoFollow(!logAutoFollow, { scrollToEnd: !logAutoFollow });
    });
    $('log-new-records-btn').addEventListener('click', () => {
        setLogAutoFollow(true, { scrollToEnd: true });
    });
    $('log-toggle-btn').addEventListener('click', () => {
        setLogDetailsExpanded($('log-panel').classList.contains('is-collapsed'));
    });
    $('progress-log').addEventListener('scroll', () => {
        if (!logAutoFollow) return;
        const body = $('progress-log');
        if (body.scrollHeight - body.scrollTop - body.clientHeight > 64) {
            setLogAutoFollow(false);
        }
    }, { passive: true });
    const resultScrollPage = $('page-4')?.querySelector('.page-scroll');
    resultScrollPage?.addEventListener('scroll', updateResultScrollTopButton, { passive: true });
    $('result-scroll-top')?.addEventListener('click', scrollResultToTop);

    // Step 1: 上传
    const uploadZone = $('upload-zone');
    uploadZone.addEventListener('click', selectFile);
    uploadZone.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            selectFile();
        }
    });
    uploadZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        if (isRestarting || isForcedUpdateBlocking() || uploadZone.getAttribute('aria-disabled') === 'true') return;
        uploadZone.classList.add('dragover');
    });
    uploadZone.addEventListener('dragleave', () => {
        uploadZone.classList.remove('dragover');
    });
    uploadZone.addEventListener('drop', (e) => {
        e.preventDefault();
        e.stopPropagation();
        setGlobalFileDropActive(false);
        uploadZone.classList.remove('dragover');
        if (isRestarting || isForcedUpdateBlocking() || uploadZone.getAttribute('aria-disabled') === 'true') return;
        const file = e.dataTransfer.files[0];
        void handleIncomingSourceFile(file);
    });

    // 全局文件拖拽：无论用户当前在哪个步骤，都先拦截系统默认打开行为，
    // 显示统一导入层，并把文件交给同一条“新任务”确认/导入链路。
    window.addEventListener('dragenter', event => {
        if (!isFileDragEvent(event)) return;
        event.preventDefault();
        setGlobalFileDropActive(true);
    });
    window.addEventListener('dragover', event => {
        if (!isFileDragEvent(event)) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
        setGlobalFileDropActive(true);
    });
    window.addEventListener('dragleave', event => {
        if (!isFileDragEvent(event)) return;
        if (!event.relatedTarget) setGlobalFileDropActive(false);
    });
    window.addEventListener('drop', event => {
        if (!isFileDragEvent(event)) return;
        event.preventDefault();
        setGlobalFileDropActive(false);
        const file = event.dataTransfer?.files?.[0];
        void handleIncomingSourceFile(file);
    });

    $('cancel-import-btn')?.addEventListener('click', () => { void cancelSourceImport(); });

    // 隐藏的 file input（浏览器模式）
    $('hidden-file-input').addEventListener('change', (e) => {
        const file = e.target.files[0];
        if (file) handleFileSelected(file);
        // 重置 value 以便重复选择同一文件时仍能触发 change 事件
        e.target.value = '';
    });

    // Step 2: 预设管理
    $('save-preset-btn').addEventListener('click', handleSavePreset);
    $('apply-preset-btn').addEventListener('click', handleApplyPreset);
    $('delete-preset-btn').addEventListener('click', handleDeletePreset);

    // Step 2: 配置
    bindVoiceWorkspaceEvents();
    $('format').addEventListener('change', (e) => {
        enforceOutputCompatibility();
        rememberCurrentConfig();
    });
    $('quality').addEventListener('change', (e) => {
        updateConfigSummary();
        rememberCurrentConfig();
    });
    $('preview').addEventListener('change', () => {
        updateConfigSummary();
        rememberCurrentConfig();
    });
    $$('input[name="generation-mode"]').forEach(input => {
        input.addEventListener('change', () => {
            updateGenerationModeUI(selectedGenerationMode());
            updateConfigSummary();
            rememberCurrentConfig();
        });
    });
    $('change-file-btn').addEventListener('click', requestRestart);
    $('back-to-upload-btn').addEventListener('click', requestRestart);
    $('retry-generation-btn').addEventListener('click', () => {
        void retryGenerationFromRecovery();
    });
    $('return-config-btn').addEventListener('click', () => {
        void returnToConfigSafely();
    });
    $('cancel-generation-btn')?.addEventListener('click', async () => {
        if (!currentSession || cancelWorkflowPromise) return;
        const session = currentSession;
        const button = $('cancel-generation-btn');
        if (button) button.disabled = true;
        hardStopNavigationRequested = true;
        try {
            const snapshot = await cancelCurrentWorkflow(session, {
                reason: 'desktop-user-cancel',
            });
            if (isHardStoppedWorkflowSnapshot(snapshot)) {
                resetGenerationAfterHardStop(session, snapshot);
            } else if (isTerminalWorkflowSnapshot(snapshot)) {
                // The provider won a completion race before the stop fence;
                // retain the normal terminal result instead of pretending it
                // was cancelled.
                applyCancellationOutcome(session, snapshot);
            } else {
                showToast('停止请求未立即返回终态，请刷新任务', 'warning');
            }
        } catch (error) {
            console.error('停止生成失败:', error);
            showToast(`停止生成失败：${error.message || '请稍后重试'}`, 'error');
            $('status-text').textContent = '停止请求未完成，请再次点击“停止生成”';
        } finally {
            hardStopNavigationRequested = false;
            updateGenerationCancelUI();
        }
    });
    $('pause-generation-btn')?.addEventListener('click', () => {
        void runFreshWorkspaceAction('PAUSE');
    });
    $('resume-generation-btn')?.addEventListener('click', () => {
        void runFreshWorkspaceAction('RESUME');
    });
    $('review-next-btn')?.addEventListener('click', () => {
        if (!currentSession) return;
        goToStep(2);
        showToast('内容已确认，请配置声音');
    });
    $('review-reprocess-btn')?.addEventListener('click', () => { void requestRestart(); });
    $('skip-config-btn').addEventListener('click', () => {
        applyConfigToForm(collectConfig(true), { includeRoles: true });
        showToast('已恢复推荐设置');
    });
    $('start-generate-btn').addEventListener('click', () => {
        goToStep(3);
        startProcessing(false);
    });

    $('audio-search-input').addEventListener('input', scheduleAudioFilter);
    $('audio-type-filter').addEventListener('change', scheduleAudioFilter);

    // Step 4: 下载
    $('download-zip-btn').addEventListener('click', async () => {
        const button = $('download-zip-btn');
        if (button.disabled) return;
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        try {
            await downloadZip();
        } finally {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    });
    $('cancel-artifact-transfer-btn')?.addEventListener('click', () => { void cancelArtifactTransfer(); });
    $('generate-full-btn').addEventListener('click', () => {
        if (!lastGenerationConfig) return;
        destroyWaveSurfers();
        applyConfigToForm({ ...lastGenerationConfig, preview: false }, { includeRoles: true });
        goToStep(2);
        showToast('已保留试听设置，确认后可生成完整文档');
    });
    $('result-return-config-btn').addEventListener('click', async () => {
        destroyWaveSurfers();
        if (lastGenerationConfig) applyConfigToForm(lastGenerationConfig, { includeRoles: true });
        const moved = await returnToConfigSafely({ buttonId: 'result-return-config-btn' });
        if (moved) showToast('已返回配置；修改参数后会重新生成全部内容');
    });
    $('rerun-task-btn')?.addEventListener('click', () => {
        const action = workflowAdapter.action?.(activeResultContext?.workspace || currentWorkspace, 'RERUN');
        if (!action) return;
        if (activeResultContext?.mode === 'history') void rerunResultContext(activeResultContext);
        else void performWorkspaceAction(action);
    });
    $('retry-failed-btn').addEventListener('click', () => { void retryFailedItems(); });
    $('new-file-btn').addEventListener('click', requestRestart);
}

// ============================================================================
// 配置加载
// ============================================================================

async function loadConfig() {
    const maxRetries = 3;
    for (let attempt = 1; attempt <= maxRetries; attempt++) {
        try {
            if (!workflowApi) throw new Error('工作流服务未初始化');
            currentConfig = await workflowApi.getConfig();

            setVoiceCatalog(
                currentConfig.voices,
                currentConfig.voice_filters,
                currentConfig.voice_aliases,
            );
            const migratedVoiceSelections = migrateVoiceSelections();
            if (!clientConfigInitialized) {
                const normalized = normalizeClientConfig(currentConfig);
                selectedDefaultFemaleVoice = normalized.default_female_voice;
                selectedDefaultMaleVoice = normalized.default_male_voice;
                voiceParamConfigs = Object.fromEntries(
                    Object.entries(normalized.role_configs || {}).map(([key, value]) => [key, { ...value }]),
                );
                if (!voiceParamConfigs[DEFAULT_FEMALE_ROLE_KEY]) voiceParamConfigs[DEFAULT_FEMALE_ROLE_KEY] = createDefaultVoiceParams(DEFAULT_FEMALE_ROLE_KEY);
                if (!voiceParamConfigs[DEFAULT_MALE_ROLE_KEY]) voiceParamConfigs[DEFAULT_MALE_ROLE_KEY] = createDefaultVoiceParams(DEFAULT_MALE_ROLE_KEY);
                clientConfigInitialized = true;
            }
            if (migratedVoiceSelections) rememberCurrentConfig();
            renderVoiceWorkspace();

            // 刷新摘要中的音色显示
            updateConfigSummary();

            return true;  // 成功，退出重试
        } catch (err) {
            console.error(`加载配置失败 (尝试 ${attempt}/${maxRetries}):`, err);
            if (attempt < maxRetries) {
                await new Promise(r => setTimeout(r, 1000 * attempt));
            } else {
                showToast('加载配置失败，部分功能可能不可用');
            }
        }
    }
    return false;
}

// ============================================================================
// 步骤导航
// ============================================================================

function updateResultScrollTopButton() {
    const button = $('result-scroll-top');
    const scrollPage = $('page-4')?.querySelector('.page-scroll');
    if (!button) return;
    button.hidden = !scrollPage || scrollPage.scrollTop < 240;
}

function scrollResultToTop() {
    const scrollPage = $('page-4')?.querySelector('.page-scroll');
    if (!scrollPage) return;
    scrollPage.scrollTo({ top: 0, behavior: 'smooth' });
    window.setTimeout(updateResultScrollTopButton, 280);
}

function generationWorkspaceNavigationAllowed({
    activeWorkspaceName = activeWorkspace,
    hasSession = Boolean(currentSession?.session_id),
    generationActive = isGenerating || generationStartInFlight,
    generationAccepted = isAcceptedGenerationSnapshot(currentWorkspace?.snapshot || currentSession),
    generationResultState = generationResult,
    generationSnapshot = currentWorkspace?.snapshot || currentSession,
} = {}) {
    if (!hasSession) return false;

    const activeIndex = WORKSPACE_ORDER.indexOf(activeWorkspaceName);
    const generationIndex = WORKSPACE_ORDER.indexOf('generation');
    const hasRecoveryState = ['error', 'cancelled'].includes(String(generationResultState || ''));
    // A hard stop is deliberately a one-way UI transition. The old terminal
    // run remains available in history, but its generation console cannot be
    // reopened from the step rail; the next run must start from voice config.
    if (isHardStoppedWorkflowSnapshot(generationSnapshot) && !generationActive) return false;
    return activeIndex >= generationIndex || generationActive || generationAccepted || hasRecoveryState;
}

function isWorkspaceNavigationAllowed(workspaceName) {
    const workspace = String(workspaceName || '');
    if (workspace === 'import') return true;
    if (!currentSession?.session_id) return false;
    if (workspace === 'review' || workspace === 'voice') return true;

    if (workspace === 'generation') {
        return generationWorkspaceNavigationAllowed();
    }

    if (workspace === 'delivery') {
        const snapshot = currentWorkspace?.snapshot || currentSession;
        const terminalResult = String(snapshot?.result_status || '');
        return Boolean(
            latestCurrentResultEvent
            || activeResultContext?.files?.length
            || generationResult === 'done'
            || (
                isTerminalWorkflowSnapshot(snapshot)
                && ['SUCCEEDED', 'PARTIAL_SUCCESS'].includes(terminalResult)
            )
        );
    }

    return false;
}

function workspaceNavigationLockReason(workspaceName) {
    if (!currentSession?.session_id && workspaceName !== 'import') return '请先导入文档';
    if (workspaceName === 'generation') {
        if (isHardStoppedWorkflowSnapshot(currentWorkspace?.snapshot || currentSession)) {
            return '当前生成已停止，请从声音配置重新生成';
        }
        return '开始生成后才能进入生成任务';
    }
    if (workspaceName === 'delivery') return '完成任务后才能进入交付中心';
    return '';
}

function goToStep(step) {
    currentView = 'workflow';
    currentStep = step;
    const workspaceForStep = { 1: 'import', 2: 'voice', 3: 'generation', 4: 'delivery' }[step] || 'import';
    setActiveWorkspaceView(workspaceForStep);
    if (step === 4 && currentWorkspace) renderWorkspaceShell(currentWorkspace, currentSession);

    const historyNav = $('history-nav-btn');
    historyNav?.classList.remove('active');
    historyNav?.removeAttribute('aria-current');
    const versionNav = $('version-nav-btn');
    versionNav?.classList.remove('active');
    versionNav?.removeAttribute('aria-current');
    const backToHistoryBtn = $('back-to-history-btn');
    if (backToHistoryBtn) backToHistoryBtn.hidden = true;

    // 切换页面
    $$('.step-page').forEach(p => p.classList.remove('active'));
    $(`page-${step}`)?.classList.add('active');

    updateStepper();

    // Rendering the toolbar/workspace shell can be triggered by a late
    // snapshot while this transition is in progress. Re-apply the step view
    // after that render so page 2 never ends up with two hidden workspaces.
    setActiveWorkspaceView(workspaceForStep);
    if (step === 2 && currentSession) renderVoiceWorkspace();

    // 滚动到顶部
    const scrollPage = $(`page-${step}`)?.querySelector('.page-scroll, .page-center');
    if (scrollPage) scrollPage.scrollTop = 0;
    if (step === 4) updateResultScrollTopButton();

    const heading = $(`page-${step}`)?.querySelector('h1:not([hidden])');
    if (heading) requestAnimationFrame(() => heading.focus({ preventScroll: true }));

    if (step === 4) {
        // 结果页在构建波形时仍处于 display:none；等待两帧，确保 WaveSurfer 获得正确容器宽度。
        requestAnimationFrame(() => requestAnimationFrame(activateResultWaveforms));
    }
}

function updateStepper() {
    const activeIndex = Math.max(0, WORKSPACE_ORDER.indexOf(activeWorkspace));
    $$('.step-indicator').forEach(el => {
        const workspace = el.dataset.workspace || 'import';
        const index = WORKSPACE_ORDER.indexOf(workspace);
        el.classList.remove('active', 'completed');
        el.removeAttribute('aria-current');
        const isAccessible = isWorkspaceNavigationAllowed(workspace);
        el.disabled = !isAccessible;
        el.setAttribute('aria-disabled', isAccessible ? 'false' : 'true');
        if (!isAccessible) {
            const lockReason = workspaceNavigationLockReason(workspace);
            if (lockReason) el.title = lockReason;
        } else {
            el.removeAttribute('title');
        }
        if (currentView === 'workflow' && index >= 0 && index < activeIndex && isAccessible) {
            el.classList.add('completed');
        } else if (workspace === activeWorkspace && currentView === 'workflow') {
            el.classList.add('active');
            el.setAttribute('aria-current', 'step');
        }
    });

    $$('.step-line').forEach(el => {
        const line = String(el.dataset.line || '');
        const lineIndex = { '1': 0, 'review-voice': 1, 'voice-generation': 2, 'generation-delivery': 3 }[line];
        el.classList.toggle('active', currentView === 'workflow' && Number.isInteger(lineIndex) && lineIndex < activeIndex);
    });

    // On narrow windows the workflow rail is intentionally horizontally
    // scrollable. Keep the active step fully visible when a transition moves
    // from the first steps to delivery; otherwise only the edge of the step
    // indicator is visible while the toolbar already reports the new step.
    const stepper = $('stepper');
    const activeStep = stepper?.querySelector('.step-indicator.active');
    if (stepper && activeStep && stepper.scrollWidth > stepper.clientWidth) {
        const inset = 8;
        const stepperRect = stepper.getBoundingClientRect();
        const activeRect = activeStep.getBoundingClientRect();
        // getBoundingClientRect() reflects the current scroll position. Add
        // it back so the comparison below stays in the stepper's content
        // coordinate system when moving both forwards and backwards.
        const stepLeft = activeRect.left - stepperRect.left + stepper.scrollLeft;
        const stepRight = stepLeft + activeStep.offsetWidth;
        const visibleLeft = stepper.scrollLeft + inset;
        const visibleRight = stepper.scrollLeft + stepper.clientWidth - inset;
        if (stepLeft < visibleLeft) {
            stepper.scrollLeft = Math.max(0, stepLeft - inset);
        } else if (stepRight > visibleRight) {
            stepper.scrollLeft = Math.min(
                stepper.scrollWidth - stepper.clientWidth,
                stepRight - stepper.clientWidth + inset,
            );
        }
    }

    const toolbarStep = $('toolbar-step');
    const toolbarContextLabel = $('toolbar-context-label');
    const taskBadge = $('task-status-badge');
    const toolbarCounts = $('toolbar-counts');
    const toolbarDocument = $('toolbar-document');
    const isTaskContext = currentView === 'workflow';
    if (toolbarContextLabel) toolbarContextLabel.textContent = isTaskContext ? '当前任务' : (currentView === 'version' ? '应用' : '任务中心');
    if (taskBadge) taskBadge.hidden = !isTaskContext;
    if (toolbarCounts) toolbarCounts.hidden = !isTaskContext;
    if (toolbarDocument) toolbarDocument.hidden = !isTaskContext || !toolbarDocument.textContent.trim();
    if (toolbarStep) {
        toolbarStep.textContent = currentView === 'workflow'
            ? `${String(activeIndex + 1).padStart(2, '0')} / ${WORKSPACE_TITLES[activeWorkspace] || STEP_TITLES[currentStep] || ''}`
            : (currentView === 'version' ? '版本中心' : '历史记录');
    }
    renderWorkspaceShell(currentWorkspace, currentSession);
}

function setHistoryNavActive(active) {
    const historyNav = $('history-nav-btn');
    if (!historyNav) return;
    historyNav.classList.toggle('active', active);
    if (active) historyNav.setAttribute('aria-current', 'page');
    else historyNav.removeAttribute('aria-current');
}

function setVersionNavActive(active) {
    const versionNav = $('version-nav-btn');
    if (!versionNav) return;
    versionNav.classList.toggle('active', active);
    if (active) versionNav.setAttribute('aria-current', 'page');
    else versionNav.removeAttribute('aria-current');
}

function activateStandalonePage(pageId, view) {
    currentView = view;
    $$('.step-page').forEach(page => page.classList.remove('active'));
    const page = $(pageId);
    page?.classList.add('active');
    setHistoryNavActive(view === 'history');
    setVersionNavActive(view === 'version');
    updateStepper();

    const scrollPage = page?.querySelector('.page-scroll, .page-center');
    if (scrollPage) scrollPage.scrollTop = 0;
    if (pageId === 'page-4') updateResultScrollTopButton();
    const heading = page?.querySelector('h1');
    if (heading && !isForcedUpdateBlocking()) {
        requestAnimationFrame(() => heading.focus({ preventScroll: true }));
    }
}

function showHistoryPage({ refresh = true } = {}) {
    if (isRestarting) return;
    if (currentView === 'workflow') {
        historyReturnStep = generationResult === 'done' && latestCurrentResultEvent ? 4 : currentStep;
    }
    destroyWaveSurfers();
    activateStandalonePage('page-history', 'history');
    const backToHistoryBtn = $('back-to-history-btn');
    if (backToHistoryBtn) backToHistoryBtn.hidden = true;
    const historyBackBtn = $('history-back-btn');
    if (historyBackBtn) historyBackBtn.textContent = currentSession ? '返回当前任务' : '返回导入文档';
    if (refresh) void refreshHistoryRecords();
    else renderHistoryRecords(historyRecords);
}

function showVersionPage({ fromUpdate = false } = {}) {
    if (isRestarting) return;
    destroyWaveSurfers();
    activateStandalonePage('page-version', 'version');
    renderVersionCenter();
    if (!fromUpdate && updateState.status === 'idle' && isElectron) {
        void runUpdateAction('check', $('version-check-btn'));
    }
}

function returnToWorkflow() {
    const returnStep = currentSession ? historyReturnStep : 1;
    if (returnStep === 4 && latestCurrentResultEvent && currentSession) {
        buildResultPage(latestCurrentResultEvent);
    }
    goToStep(returnStep);
}

function setHistoryCounts(count) {
    const safeCount = Math.max(0, Math.min(Number(count) || 0, 20));
    if ($('history-nav-count')) $('history-nav-count').textContent = String(safeCount);
    if ($('history-count')) $('history-count').textContent = `${safeCount} / 20`;
}

function historyDateLabel(value) {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '完成时间未知';
    return parsed.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
    });
}

function historyFormatLabel(record) {
    return String(record?.format || '未知').toUpperCase();
}

function historyGenerationModeLabel(record) {
    // 历史清单升级前没有该字段，按原有逐条流程解释，避免把旧任务误标成
    // 新的合并切割模式。
    return record?.generation_mode
        ? generationModeLabel(record.generation_mode)
        : GENERATION_MODE_LABELS[GENERATION_MODE_SINGLE];
}

function nonNegativeCount(value, fallback = 0) {
    if (value === null || value === undefined || value === '') return fallback;
    const number = Number(value);
    return Number.isFinite(number) ? Math.max(0, Math.floor(number)) : fallback;
}

function hasOwnRecordField(source, key) {
    return Boolean(
        source
        && typeof source === 'object'
        && Object.prototype.hasOwnProperty.call(source, key),
    );
}

function historyProgressCounts(progress = {}, record = {}, fileCount = 0) {
    const authoritative = progress && typeof progress === 'object' ? progress : {};
    const fallbackRecord = record && typeof record === 'object' ? record : {};
    const count = (key, fallback = 0) => {
        const fallbackValue = nonNegativeCount(fallbackRecord[key], fallback);
        return hasOwnRecordField(authoritative, key)
            ? nonNegativeCount(authoritative[key], fallbackValue)
            : fallbackValue;
    };
    return {
        completed: count('completed', fileCount),
        total: count('total', fileCount),
        failed: count('failed'),
        cancelled: count('cancelled'),
    };
}

function resultSummaryCounts(context = {}, resultCount = 0, workspaceCounts = {}, deliveryIssueCount = 0) {
    const resultContext = context && typeof context === 'object' ? context : {};
    const authoritativeCounts = workspaceCounts && typeof workspaceCounts === 'object' ? workspaceCounts : {};
    const count = key => Math.max(0, Number(resultContext[key] ?? authoritativeCounts[key]) || 0);
    const reportedCompleted = count('completed');
    const success = Math.max(0, Number(resultCount) || 0);
    const missingFiles = Math.max(0, reportedCompleted - success);
    const failed = count('failed') + missingFiles;
    const cancelled = count('cancelled');
    const deliveryIssues = Math.max(0, Number(deliveryIssueCount) || 0);
    // A missing result file is already represented by the gap between the
    // server's completed count and the files we can actually read. Only
    // additional delivery blockers should increase the unresolved total.
    const unresolved = failed + cancelled + Math.max(0, deliveryIssues - missingFiles);
    return {
        reportedCompleted,
        success,
        missingFiles,
        failed,
        cancelled,
        deliveryIssues,
        unresolved,
    };
}

function historyActiveCandidateState(candidate) {
    if (!candidate?.workspace?.snapshot?.workflow_id) return 'unavailable';
    if (candidate.can_takeover === true) return 'takeover';
    if (candidate.can_resume === true) return 'resume';
    return 'context';
}

function historyActiveActionLabel(record) {
    if (isTerminalWorkflowSnapshot(record)) return '查看交付';
    switch (historyActiveCandidateState(record?.active_candidate)) {
        case 'takeover':
            return '继续生成';
        case 'resume':
        case 'context':
            return '恢复上下文';
        default:
            return '查看状态';
    }
}

function historyActiveStatusLabel(candidate) {
    switch (historyActiveCandidateState(candidate)) {
        case 'takeover':
            return '可继续生成';
        case 'resume':
            return '可恢复上下文';
        case 'context':
            return '待处理';
        default:
            return '状态待同步';
    }
}

function activeCandidateHintText(candidates = [], truncated = false) {
    if (!Array.isArray(candidates) || candidates.length === 0) return '';
    const counts = candidates.reduce((result, candidate) => {
        const state = historyActiveCandidateState(candidate);
        result[state] = (result[state] || 0) + 1;
        return result;
    }, {});
    const parts = [];
    if (counts.takeover) parts.push(`${counts.takeover} 个任务可继续生成`);
    if (counts.resume) parts.push(`${counts.resume} 个任务可恢复上下文`);
    if (counts.context) parts.push(`${counts.context} 个任务待处理`);
    if (counts.unavailable) parts.push(`${counts.unavailable} 个任务状态待同步`);
    const suffix = truncated ? '（列表已截断）' : '';
    return `${parts.join('，') || `${candidates.length} 个任务状态待同步`}${suffix}`;
}

function historyStatusPresentation(record) {
    const executionState = String(record?.execution_state || '');
    const controlState = String(record?.control_state || '');
    const resultStatus = String(record?.result_status || '');
    const requiresReconcile = Boolean(record?.active_candidate?.requires_reconcile)
        || executionState === 'WAITING_USER'
        || resultStatus === 'AMBIGUOUS';
    if (requiresReconcile) return { label: '待处理', className: 'is-partial' };
    if (executionState === 'TERMINAL') {
        if (resultStatus === 'SUCCEEDED') {
            const completed = nonNegativeCount(record?.completed, nonNegativeCount(record?.available_files));
            const total = nonNegativeCount(record?.total);
            if (total > 0 && completed < total) return { label: '交付待同步', className: 'is-partial' };
            return { label: '已完成', className: '' };
        }
        if (resultStatus === 'PARTIAL_SUCCESS') return { label: '部分完成', className: 'is-partial' };
        if (resultStatus === 'CANCELLED') return { label: '已取消', className: 'is-partial' };
        return { label: '生成失败', className: 'is-danger' };
    }
    if (controlState === 'PAUSED' || controlState === 'PAUSE_REQUESTED') {
        return { label: '已暂停', className: 'is-active' };
    }
    if (executionState === 'WAITING_RETRY') return { label: '等待重试', className: 'is-active' };
    if (executionState === 'RECOVERING') return { label: '恢复中', className: 'is-active' };
    if (executionState === 'CREATED' || String(record?.status || '') === 'DRAFT') {
        return { label: '待配置', className: 'is-active' };
    }
    return { label: '生成中', className: 'is-active' };
}

function historyRecordMatchesFilter(record) {
    const query = String(historyFilters.query || '').trim();
    if (query) {
        const searchable = [
            record?.source_filename,
            record?.format,
            record?.result_status,
            record?.execution_state,
            historyStatusPresentation(record).label,
        ].filter(Boolean).join(' ').toLocaleLowerCase('zh-CN');
        if (!searchable.includes(query)) return false;
    }
    const presentation = historyStatusPresentation(record);
    const terminal = isTerminalWorkflowSnapshot(record);
    const attention = presentation.className.includes('is-danger')
        || presentation.className.includes('is-partial');
    switch (historyFilters.status) {
        case 'active':
            return !terminal && !attention;
        case 'attention':
            return attention;
        case 'done':
            return terminal;
        default:
            return true;
    }
}

function historyRecordTimestamp(record) {
    const key = historyFilters.sort === 'created' ? 'created_at' : 'updated_at';
    const value = Date.parse(String(record?.[key] || record?.updated_at || record?.created_at || ''));
    return Number.isFinite(value) ? value : 0;
}

function itemDisplayFacts(item) {
    const metadata = item?.metadata && typeof item.metadata === 'object' && !Array.isArray(item.metadata)
        ? item.metadata
        : {};
    const docType = String(
        metadata.doc_type
        || item?.doc_type
        || item?.item_type
        || '',
    ).trim();
    let category = String(metadata.category || item?.category || '').trim();
    // Older rows only have item_type. Use it as the document label once, not
    // as both “document type” and “category”, which produced duplicate labels
    // such as “模仿朗读-框内英文 · 模仿朗读-框内英文”.
    if (!docType && item?.item_type) category = String(item.item_type).trim();
    if (category === docType) category = '';
    return {
        docType: docType || '音频',
        category,
    };
}

function resultHasUsableAcceptedVoiceConfiguration(workspace = null, item = null) {
    const configuration = workspace?.configuration?.effective;
    if (!configuration || typeof configuration !== 'object' || Array.isArray(configuration)) return false;
    // A workspace can briefly expose an empty `effective` object while it is
    // being hydrated.  Treating that placeholder as an accepted snapshot
    // hides the real voice_keys stored on legacy artifacts and makes the
    // delivery page invent the female fallback.  The normalized server
    // projection always carries both default fields, even when an old record
    // or a hydration placeholder has both values set to null. Field presence
    // alone is therefore not evidence that a voice was accepted; require an
    // actual non-empty voice key. Role-only configs are also meaningful for
    // explicitly role-labelled items.
    const hasDefaultVoice = [
        configuration.default_female_voice,
        configuration.default_male_voice,
    ].some(value => String(value ?? '').trim());
    const roleVoices = configuration.role_voices;
    const role = normalizeRoleKeyClient(item?.role);
    const hasRoleVoiceForItem = role && roleVoices
        && typeof roleVoices === 'object'
        && !Array.isArray(roleVoices)
        && Boolean(roleVoices[role] || roleVoices[`role:${role}`]);
    return hasDefaultVoice || Boolean(hasRoleVoiceForItem);
}

function resultVoiceKeyFromAcceptedConfiguration(item, workspace = null) {
    const configuration = workspace?.configuration?.effective;
    if (!configuration || typeof configuration !== 'object' || Array.isArray(configuration)) return '';
    // An empty effective object is a hydration placeholder, not a frozen
    // generation decision.  Returning the built-in Amanda/George fallback
    // here would make old Artifact voice facts look like a new accepted
    // configuration and can overwrite the actual voice used by that record.
    if (!resultHasUsableAcceptedVoiceConfiguration(workspace, item)) return '';
    const explicit = String(item?.voice_key || '').trim();
    if (explicit) return explicit;

    const role = normalizeRoleKeyClient(item?.role);
    const roleVoices = configuration.role_voices && typeof configuration.role_voices === 'object'
        && !Array.isArray(configuration.role_voices)
        ? configuration.role_voices
        : {};
    const roleVoice = role && (
        roleVoices[role]
        || roleVoices[`role:${role}`]
    );
    if (roleVoice) return String(roleVoice).trim();

    const metadata = item?.metadata && typeof item.metadata === 'object' && !Array.isArray(item.metadata)
        ? item.metadata
        : {};
    const itemGender = [
        item?.voice,
        item?.voice_gender,
        item?.gender,
        metadata.voice,
        metadata.voice_gender,
        metadata.gender,
    ].map(reviewGenderFromValue).find(Boolean);
    if (itemGender === 'male') {
        return String(configuration.default_male_voice || 'george').trim();
    }
    if (itemGender === 'female') {
        return String(configuration.default_female_voice || 'amanda').trim();
    }

    // Keep this fallback aligned with WorkflowEngine._effective_plan_item for
    // older workspaces whose durable provider plan predates the voice
    // projection. It is only enabled when the accepted workspace contains its
    // own frozen configuration, never from the renderer's current settings.
    const roleText = String(item?.role || '').trim();
    const maleRole = /^(mr|mr\.|sir|男|先生)\b/i.test(roleText);
    return String(
        (maleRole ? configuration.default_male_voice : configuration.default_female_voice)
        || (maleRole ? 'george' : 'amanda'),
    ).trim();
}

function resultVoiceKeysFromAcceptedContent(item, workspace = null) {
    const text = String(item?.normalized_content ?? item?.text ?? item?.content ?? '').trim();
    if (!text) return [];

    const hasAcceptedConfiguration = resultHasUsableAcceptedVoiceConfiguration(workspace, item);
    const configuration = hasAcceptedConfiguration && workspace?.configuration?.effective
        && typeof workspace.configuration.effective === 'object'
        && !Array.isArray(workspace.configuration.effective)
        ? workspace.configuration.effective
        : {};
    const configuredItemVoice = String(item?.voice_key || '').trim()
        || (hasAcceptedConfiguration ? resultVoiceKeyFromAcceptedConfiguration(item, workspace) : '')
        || String(configuration.default_female_voice || '').trim();
    const femaleVoice = String(
        configuration.default_female_voice
        || configuredItemVoice
        || (hasAcceptedConfiguration ? 'amanda' : '')
    ).trim();
    const maleVoice = String(
        configuration.default_male_voice
        || (hasAcceptedConfiguration ? 'george' : '')
    ).trim();
    const defaultVoice = configuredItemVoice || femaleVoice;
    const roleVoices = configuration.role_voices
        && typeof configuration.role_voices === 'object'
        && !Array.isArray(configuration.role_voices)
        ? configuration.role_voices
        : {};
    const roleVoiceMapForResult = {};
    Object.entries(roleVoices).slice(0, 256).forEach(([role, voice]) => {
        const value = String(voice || '').trim();
        if (!value) return;
        const roleKey = normalizeRoleKeyClient(role);
        if (roleKey) roleVoiceMapForResult[roleKey] = value;
        if (roleKey.startsWith('role:')) {
            const bareRoleKey = normalizeRoleKeyClient(roleKey.slice(5));
            if (bareRoleKey) roleVoiceMapForResult[bareRoleKey] = value;
        }
    });
    const roleVoiceFor = role => {
        const roleKey = normalizeRoleKeyClient(role);
        return roleVoiceMapForResult[roleKey]
            || roleVoiceMapForResult[`role:${roleKey}`]
            || '';
    };
    const inferRoleVoiceForResult = role => /^(mr|mr\.|sir|男|先生)\b/i.test(String(role || '').trim())
        ? maleVoice
        : femaleVoice;
    const values = [];
    const append = value => {
        const key = String(value || '').trim();
        if (key && !values.includes(key)) values.push(key);
    };
    const lines = text.split(/\r?\n/);
    const candidateRoleKeys = new Set();
    lines.forEach(line => {
        const value = line.trim();
        if (!value || /^[WwMm]\s*[:：]/.test(value) || /^\([WwMm]\)/.test(value)) return;
        const match = /^([^:：\n]{1,60}?)\s*[:：]\s*(.*)$/.exec(value);
        if (match && roleLooksLikeLabel(match[1])) {
            candidateRoleKeys.add(normalizeRoleKeyClient(match[1]));
        }
    });
    const allowInferredRoles = candidateRoleKeys.size >= 2;
    let activeVoice = defaultVoice || femaleVoice;
    lines.forEach(line => {
        const value = line.trim();
        if (!value) return;

        const speakerMatch = /^([WwMm])\s*[:：]\s*(.*)$/.exec(value)
            || /^\(([WwMm])\)\s*(.*)$/.exec(value);
        if (speakerMatch) {
            activeVoice = speakerMatch[1].toUpperCase() === 'W' ? femaleVoice : maleVoice;
            if (String(speakerMatch[2] || '').trim()) append(activeVoice);
            return;
        }

        const roleMatch = /^([^:：\n]{1,60}?)\s*[:：]\s*(.*)$/.exec(value);
        if (roleMatch && roleLooksLikeLabel(roleMatch[1])) {
            const mappedVoice = roleVoiceFor(roleMatch[1]);
            if (mappedVoice || allowInferredRoles) {
                activeVoice = mappedVoice || inferRoleVoiceForResult(roleMatch[1]);
                if (String(roleMatch[2] || '').trim()) append(activeVoice);
                return;
            }
        }
        append(activeVoice);
    });
    return values;
}

function resultContentHasExplicitVoiceEvidence(item) {
    const text = String(item?.normalized_content ?? item?.text ?? item?.content ?? '').trim();
    if (!text) return false;
    const lines = text.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (lines.some(line => (
        /^[WwMm]\s*[:：]/.test(line)
        || /^\([WwMm]\)\s*/.test(line)
    ))) return true;
    const roleLabels = new Set();
    lines.forEach(line => {
        const match = /^([^:：\n]{1,60}?)\s*[:：]\s*(.*)$/.exec(line);
        if (match && roleLooksLikeLabel(match[1])) {
            roleLabels.add(normalizeRoleKeyClient(match[1]));
        }
    });
    return roleLabels.size >= 2;
}

function resultItemHasAcceptedVoiceFacts(item, workspace = null) {
    if (resultHasUsableAcceptedVoiceConfiguration(workspace, item)) return true;
    if (String(item?.voice_key || '').trim()) return true;
    if (Array.isArray(item?.segments) && item.segments.some(segment => (
        String(segment?.voice_key || '').trim()
        || (Array.isArray(segment?.voice_keys) && segment.voice_keys.length > 0)
    ))) return true;
    const metadata = item?.metadata && typeof item.metadata === 'object' && !Array.isArray(item.metadata)
        ? item.metadata
        : {};
    return [
        item?.voice,
        item?.voice_gender,
        item?.gender,
        metadata.voice,
        metadata.voice_gender,
        metadata.gender,
    ].map(reviewGenderFromValue).some(Boolean)
        || (resultHasUsableAcceptedVoiceConfiguration(workspace, item)
            && resultContentHasExplicitVoiceEvidence(item));
}

function resultVoiceKeysForItem(item, workspace = null) {
    const values = [];
    const append = value => {
        if (Array.isArray(value)) {
            value.forEach(append);
            return;
        }
        const key = String(value ?? '').trim();
        if (key && !values.includes(key)) values.push(key);
    };
    const legacyVoiceKeys = [];
    const appendLegacy = value => {
        if (Array.isArray(value)) {
            value.forEach(appendLegacy);
            return;
        }
        const key = String(value ?? '').trim();
        if (key && !legacyVoiceKeys.includes(key)) legacyVoiceKeys.push(key);
    };
    appendLegacy(item?.voice_keys);
    appendLegacy(item?.metadata?.voice_keys);
    appendLegacy(item?.voice_key);
    const segmentVoiceKeys = [];
    const appendSegment = value => {
        if (Array.isArray(value)) {
            value.forEach(appendSegment);
            return;
        }
        const key = String(value ?? '').trim();
        if (key && !segmentVoiceKeys.includes(key)) segmentVoiceKeys.push(key);
    };
    if (Array.isArray(item?.segments)) {
        item.segments.forEach(segment => {
            appendSegment(segment?.voice_keys);
            appendSegment(segment?.voice_key);
        });
    }
    const contentVoiceKeys = resultVoiceKeysFromAcceptedContent(item, workspace);
    // Explicit W/M/role markers describe the actual audio and are stronger
    // than stale WorkItem/artifact voice arrays from an older attempt.
    if (resultContentHasExplicitVoiceEvidence(item)) {
        // Without a frozen configuration those markers have no reliable
        // mapping to catalog keys. Keep any concrete item fact as a fallback;
        // otherwise resultFilesFromArtifacts will retain the Artifact facts.
        if (contentVoiceKeys.length) return contentVoiceKeys;
        if (legacyVoiceKeys.length) {
            legacyVoiceKeys.forEach(append);
            return values;
        }
        return [];
    }
    // Segment-level metadata is the precise source for callers that already
    // split the item into voice-specific pieces.
    if (segmentVoiceKeys.length) return segmentVoiceKeys;
    // For an unmarked question, the accepted snapshot (including parser
    // gender metadata) determines exactly one default slot. Do not union it
    // with legacy arrays: that was the source of the “one male voice shown as
    // two voices” symptom.
    const acceptedVoiceKey = resultHasUsableAcceptedVoiceConfiguration(workspace, item)
        ? resultVoiceKeyFromAcceptedConfiguration(item, workspace)
        : '';
    if (acceptedVoiceKey) return [acceptedVoiceKey];
    // Preserve old records only when no accepted configuration can resolve
    // the item. This keeps historical data readable without allowing it to
    // override a frozen generation decision.
    if (legacyVoiceKeys.length) {
        legacyVoiceKeys.forEach(append);
        return values;
    }
    return contentVoiceKeys;
}

function resultFilesFromArtifacts(items, artifacts, workspace = null) {
    const hasAuthoritativeWorkspaceArtifacts = Boolean(workspace && Array.isArray(workspace.artifacts));
    // A workspace response is the complete, server-owned projection.  Prefer
    // it over the bounded legacy artifact list so a large task cannot expose
    // an older page of artifacts, and an empty projection cannot be revived by
    // raw rows from a second endpoint.
    const sourceArtifacts = hasAuthoritativeWorkspaceArtifacts
        ? workspace.artifacts
        : (Array.isArray(artifacts) ? artifacts : []);
    const itemById = new Map((Array.isArray(items) ? items : []).map(item => [String(item.item_id), item]));
    const workspaceItems = new Map((Array.isArray(workspace?.items) ? workspace.items : [])
        .map(item => [String(item.item_id), item]));
    const workspaceArtifacts = new Map((Array.isArray(workspace?.artifacts) ? workspace.artifacts : [])
        .map(artifact => [String(artifact.artifact_id), artifact]));
    const rawArtifactsById = new Map((Array.isArray(artifacts) ? artifacts : [])
        .map(artifact => [String(artifact.artifact_id), artifact]));
    const latestByItem = new Map();
    const seenItemIds = new Set();

    // A READY Artifact is deliverable only when the authoritative item state
    // is SUCCEEDED and its server-owned MP3 filename/format/MIME metadata
    // passes validation. Never synthesize a filename or default a missing
    // format to MP3: doing so turns stale/conflicting facts into a false
    // download.
    sourceArtifacts
        .slice()
        .sort((left, right) => (
            // Workspace normally carries created_at. Keep the raw list as a
            // sort-only fallback for older clients/fixtures that omitted that
            // non-sensitive field; it never supplies delivery metadata.
            String(right.created_at || rawArtifactsById.get(String(right.artifact_id))?.created_at || '')
                .localeCompare(String(left.created_at || rawArtifactsById.get(String(left.artifact_id))?.created_at || ''))
            || String(right.artifact_id || '').localeCompare(String(left.artifact_id || ''))
        ))
        .forEach(artifact => {
            const itemId = String(artifact?.item_id || '');
            if (artifact.artifact_type !== 'tts-segment') return;
            if (!itemId || seenItemIds.has(itemId)) return;
            // The newest TTS artifact is authoritative for this item. If it is
            // not deliverable, do not fall back to an older attempt's audio.
            seenItemIds.add(itemId);

            const workspaceItem = workspaceItems.get(itemId);
            // Workspace fields win, while sparse legacy test/compatibility
            // rows may still fill fields that the old endpoint did not send.
            // Explicit nulls from the workspace remain authoritative.
            const item = workspaceItem
                ? { ...(itemById.get(itemId) || {}), ...workspaceItem }
                : (itemById.get(itemId) || {});
            const hasWorkspaceMetadata = hasAuthoritativeWorkspaceArtifacts
                || workspaceArtifacts.has(String(artifact.artifact_id));
            const metadata = hasWorkspaceMetadata
                ? workspaceArtifacts.get(String(artifact.artifact_id))
                : artifact;
            const itemStatus = String(workspaceItem?.status || item.status || '');
            // A present workspace record is authoritative even when it
            // intentionally redacts conflicting format/size/hash facts. Do
            // not revive those facts from the raw Artifact list and turn a
            // metadata conflict into a downloadable result.
            const lifecycleState = String(metadata?.lifecycle_state || (hasWorkspaceMetadata ? '' : artifact.lifecycle_state) || '');
            const verified = metadata?.verified === true && artifact.verified !== false;
            const format = String(metadata?.format || (hasWorkspaceMetadata ? '' : artifact.format) || '').trim().toLowerCase().replace(/^\./, '');
            const filename = String(metadata?.filename || (hasWorkspaceMetadata ? '' : artifact.filename) || '').trim();
            const mimeType = String(metadata?.mime_type || (hasWorkspaceMetadata ? '' : artifact.mime_type) || '').trim().toLowerCase();
            const extension = filename.includes('.') ? filename.split('.').pop().toLowerCase() : '';
            const sizeBytes = Number(hasWorkspaceMetadata ? metadata?.size_bytes : (metadata?.size_bytes ?? artifact.size_bytes));
            const expectedMime = artifactMime(format);
            const sha256 = String(metadata?.sha256 || (hasWorkspaceMetadata ? '' : artifact.sha256) || '').trim().toLowerCase();
            if (
                itemStatus !== 'SUCCEEDED'
                || lifecycleState !== 'READY'
                || !verified
                || !filename
                || filename.includes('/')
                || filename.includes('\\')
                || /[\x00-\x1f\x7f]/.test(filename)
                || format !== 'mp3'
                || !mimeType
                || extension !== 'mp3'
                || expectedMime !== 'audio/mpeg'
                || mimeType !== 'audio/mpeg'
                || !Number.isSafeInteger(sizeBytes)
                || sizeBytes <= 0
                || !/^[0-9a-f]{64}$/.test(sha256)
            ) return;

            const displayFacts = itemDisplayFacts(item);
            const text = String(item.normalized_content ?? '');
            const artifactVoiceKeys = Array.isArray(artifact?.voice_keys)
                ? artifact.voice_keys
                : (artifact?.voice_keys ? [artifact.voice_keys] : []);
            const acceptedVoiceKeys = resultVoiceKeysForItem(item, workspace);
            // Accepted item/content metadata is authoritative. Artifact voice
            // arrays are only a compatibility fallback for old records where
            // the item has no resolvable voice fact; unioning both revives a
            // stale default as a second voice in the delivery page.
            const useAcceptedVoiceKeys = resultItemHasAcceptedVoiceFacts(item, workspace);
            const voiceKeys = [...new Set(((useAcceptedVoiceKeys && acceptedVoiceKeys.length)
                ? acceptedVoiceKeys
                : artifactVoiceKeys
            ).map(value => String(value || '').trim()).filter(Boolean))];
            const primaryVoiceKey = (useAcceptedVoiceKeys && acceptedVoiceKeys[0])
                || artifactVoiceKeys[0]
                || resultVoiceKeyFromAcceptedConfiguration(item, workspace)
                || acceptedVoiceKeys[0]
                || '';
            const sequenceValue = Number(item.sequence);
            latestByItem.set(itemId, {
                filename,
                artifact_id: String(artifact.artifact_id),
                available: true,
                doc_type: displayFacts.docType,
                category: displayFacts.category,
                item_id: itemId,
                text,
                text_preview: text.slice(0, 160),
                role: item.role ?? null,
                voice_keys: voiceKeys,
                voice_key: primaryVoiceKey || null,
                sequence: Number.isSafeInteger(sequenceValue) && sequenceValue >= 0
                    ? sequenceValue
                    : null,
                size_bytes: sizeBytes,
                format,
                mime_type: mimeType,
                sha256,
                duration_ms: Number.isFinite(Number(metadata.duration_ms)) ? Number(metadata.duration_ms) : null,
            });
        });

    return [...latestByItem.values()].sort((left, right) => {
        const leftSequence = left.sequence ?? Number.MAX_SAFE_INTEGER;
        const rightSequence = right.sequence ?? Number.MAX_SAFE_INTEGER;
        return leftSequence - rightSequence || left.filename.localeCompare(right.filename);
    });
}

function createHistoryAction(label, className, handler) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.textContent = label;
    button.addEventListener('click', handler);
    return button;
}

function renderHistoryRecords(records) {
    const list = $('history-list');
    const empty = $('history-empty');
    if (!list || !empty) return;
    const visibleRecords = (Array.isArray(records) ? records : [])
        .filter(historyRecordMatchesFilter)
        .slice()
        .sort((left, right) => historyRecordTimestamp(right) - historyRecordTimestamp(left));
    list.replaceChildren();
    empty.hidden = visibleRecords.length > 0;
    const emptyTitle = empty.querySelector('h2');
    const emptyMessage = empty.querySelector('p');
    const hasRecords = Array.isArray(records) && records.length > 0;
    if (emptyTitle) emptyTitle.textContent = hasRecords ? '没有符合条件的任务' : '还没有生成记录';
    if (emptyMessage) emptyMessage.textContent = hasRecords
        ? '试试更换关键词或状态筛选，所有任务仍会保留在本机历史记录中。'
        : '完成一次音频生成后，结果会自动保存在这里。';
    if (visibleRecords.length === 0) return;

    visibleRecords.forEach(record => {
        const activeCandidate = record.active_candidate || null;
        const availableCount = nonNegativeCount(record.available_files);
        // Zero is an authoritative value.  Only an absent/invalid completed
        // field may fall back to the legacy available_files projection.
        const completed = nonNegativeCount(record.completed, availableCount);
        const failed = nonNegativeCount(record.failed);
        const cancelled = nonNegativeCount(record.cancelled);
        const skipped = nonNegativeCount(record.skipped);
        const total = Math.max(completed + failed + cancelled + skipped, nonNegativeCount(record.total));
        const pending = Math.max(
            0,
            record.pending !== null && record.pending !== undefined && Number.isFinite(Number(record.pending))
                ? nonNegativeCount(record.pending)
                : total - completed - failed - cancelled - skipped,
        );
        const presentation = historyStatusPresentation(record);
        const terminal = isTerminalWorkflowSnapshot(record);
        const item = document.createElement('article');
        item.className = `history-item${activeCandidate ? ' is-active-task' : ''}`;

        const icon = document.createElement('span');
        icon.className = 'history-item-icon';
        icon.setAttribute('aria-hidden', 'true');
        icon.textContent = historyFormatLabel(record);

        const main = document.createElement('div');
        main.className = 'history-item-main';
        const titleRow = document.createElement('div');
        titleRow.className = 'history-item-title-row';
        const title = document.createElement('h2');
        title.className = 'history-item-title';
        title.textContent = record.source_filename || '未命名文档.docx';
        title.title = title.textContent;
        const status = document.createElement('span');
        status.className = `history-status-badge ${presentation.className}`.trim();
        status.textContent = presentation.label;
        status.title = [record.execution_state, record.result_status, record.control_state]
            .filter(Boolean)
            .join(' · ');
        titleRow.append(title, status);

        const meta = document.createElement('div');
        meta.className = 'history-item-meta';
        const completedAt = document.createElement('span');
        completedAt.textContent = `${terminal ? '完成' : '更新'} ${historyDateLabel(terminal ? record.completed_at : record.updated_at)}`;
        const scope = document.createElement('span');
        scope.textContent = record.preview ? '试听任务' : '完整任务';
        const mode = document.createElement('span');
        mode.textContent = historyGenerationModeLabel(record);
        meta.append(completedAt, scope, mode);
        if (activeCandidate) {
            const activeLabel = document.createElement('span');
            activeLabel.className = 'history-active-label';
            activeLabel.textContent = historyActiveStatusLabel(activeCandidate);
            meta.appendChild(activeLabel);
        }

        const stats = document.createElement('div');
        stats.className = 'history-item-stats';
        const audioStat = document.createElement('span');
        audioStat.className = 'history-stat';
        const audioStrong = document.createElement('strong');
        audioStrong.textContent = `${availableCount}/${total}`;
        audioStat.append(audioStrong, document.createTextNode(' 个可交付'));
        const formatStat = document.createElement('span');
        formatStat.className = 'history-stat';
        const formatStrong = document.createElement('strong');
        formatStrong.textContent = historyFormatLabel(record);
        formatStat.append(formatStrong, document.createTextNode(' 格式'));
        stats.append(audioStat, formatStat);
        if (failed > 0) {
            const failedStat = document.createElement('span');
            failedStat.className = 'history-stat';
            const failedStrong = document.createElement('strong');
            failedStrong.textContent = String(failed);
            failedStat.append(failedStrong, document.createTextNode(' 条失败'));
            stats.appendChild(failedStat);
        }
        if (cancelled > 0) {
            const cancelledStat = document.createElement('span');
            cancelledStat.className = 'history-stat is-cancelled';
            const cancelledStrong = document.createElement('strong');
            cancelledStrong.textContent = String(cancelled);
            cancelledStat.append(cancelledStrong, document.createTextNode(' 条已取消'));
            stats.appendChild(cancelledStat);
        }
        if (skipped > 0) {
            const skippedStat = document.createElement('span');
            skippedStat.className = 'history-stat is-skipped';
            const skippedStrong = document.createElement('strong');
            skippedStrong.textContent = String(skipped);
            skippedStat.append(skippedStrong, document.createTextNode(' 条已跳过'));
            stats.appendChild(skippedStat);
        }
        if (pending > 0) {
            const pendingStat = document.createElement('span');
            pendingStat.className = 'history-stat';
            const pendingStrong = document.createElement('strong');
            pendingStrong.textContent = String(pending);
            pendingStat.append(pendingStrong, document.createTextNode(' 条待处理'));
            stats.appendChild(pendingStat);
        }

        main.append(titleRow, meta, stats);

        const actions = document.createElement('div');
        actions.className = 'history-item-actions';
        const viewBtn = createHistoryAction(
            historyActiveActionLabel(record),
            'btn-primary',
            () => viewHistoryRecord(record.workflow_id || record.id),
        );
        if (record.zip_available && record.zip_artifact_id) {
            const zipBtn = createHistoryAction('下载 ZIP', 'btn-ghost', async () => {
                zipBtn.disabled = true;
                try {
                    await downloadZip({
                        mode: 'history',
                        recordId: record.id,
                        workflowId: record.workflow_id || record.id,
                        sourceFilename: record.source_filename,
                        zipArtifactId: record.zip_artifact_id || null,
                    });
                } finally {
                    zipBtn.disabled = false;
                }
            });
            actions.appendChild(zipBtn);
        }
        const deleteBtn = createHistoryAction(
            terminal ? '归档' : '删除',
            'btn-ghost history-delete-btn',
            () => deleteHistoryRecord(record, deleteBtn),
        );
        deleteBtn.disabled = terminal ? false : record.can_delete === false;
        if (terminal) {
            deleteBtn.title = '隐藏已完成任务，保留其审计事实和文件';
        } else if (record.can_delete === false) {
            deleteBtn.title = record.delete_reason || '当前任务暂时不能删除';
        } else {
            deleteBtn.title = '删除未完成任务及其相关本地数据';
        }
        actions.prepend(viewBtn);
        actions.appendChild(deleteBtn);
        item.append(icon, main, actions);
        list.appendChild(item);
    });
}

function renderHistoryMessage(message, className = 'history-loading') {
    const list = $('history-list');
    const empty = $('history-empty');
    if (!list || !empty) return;
    empty.hidden = true;
    list.replaceChildren();
    const notice = document.createElement('div');
    notice.className = className;
    notice.textContent = message;
    list.appendChild(notice);
}

function renderActiveCandidateHint(candidates = activeWorkflowCandidates) {
    const hint = $('active-task-hint');
    const text = $('active-task-hint-text');
    if (!hint || !text) return;
    const visible = !currentSession && Array.isArray(candidates) && candidates.length > 0;
    hint.hidden = !visible;
    if (visible) {
        text.textContent = activeCandidateHintText(candidates, activeWorkflowListTruncated);
        hint.title = '打开历史记录查看未结束任务';
        hint.onclick = () => showHistoryPage();
        hint.tabIndex = 0;
        hint.onkeydown = event => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                showHistoryPage();
            }
        };
    } else {
        hint.onclick = null;
        hint.onkeydown = null;
        hint.removeAttribute('tabindex');
    }
}

function readWithTimeout(promise, timeoutMs = 6000) {
    const duration = Math.max(1, Number(timeoutMs) || 6000);
    let timer = null;
    const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => {
            const error = new Error(`workspace read timed out after ${duration}ms`);
            error.code = 'WORKSPACE_READ_TIMEOUT';
            reject(error);
        }, duration);
    });
    return Promise.race([Promise.resolve(promise), timeout]).finally(() => {
        if (timer) clearTimeout(timer);
    });
}

async function hydrateActiveWorkflowCandidates(candidates = []) {
    const source = (Array.isArray(candidates) ? candidates : [])
        .slice(0, ACTIVE_WORKFLOW_HYDRATE_LIMIT);
    const hydrated = [];
    // Startup should stay responsive even when a stale task points at a slow
    // local backend. A small bounded window also prevents hundreds of active
    // candidates from turning one refresh into a request storm. The total
    // budget is part of the recovery contract, not just a UI timeout.
    const deadline = Date.now() + ACTIVE_WORKFLOW_HYDRATE_BUDGET_MS;
    for (let index = 0; index < source.length; index += ACTIVE_WORKFLOW_HYDRATE_CONCURRENCY) {
        const batch = source.slice(index, index + ACTIVE_WORKFLOW_HYDRATE_CONCURRENCY);
        const results = await Promise.all(batch.map(async candidate => {
            const workflowId = String(candidate?.workflow?.workflow_id || '');
            if (!workflowId || typeof workflowApi?.getWorkspace !== 'function') return candidate;
            const remaining = deadline - Date.now();
            if (remaining <= 0) {
                return {
                    ...candidate,
                    workspace: null,
                    workspace_sync_error: {
                        code: 'WORKSPACE_HYDRATE_TIMEOUT',
                        message: '活动任务恢复超出时间预算',
                    },
                };
            }
            try {
                const workspace = await readWithTimeout(
                    workflowApi.getWorkspace(workflowId),
                    Math.min(ACTIVE_WORKFLOW_HYDRATE_TIMEOUT_MS, remaining),
                );
                return { ...candidate, workspace, workspace_sync_error: null };
            } catch (error) {
                console.warn('活动任务工作区读取失败:', workflowId, error);
                return {
                    ...candidate,
                    workspace: null,
                    workspace_sync_error: {
                        code: error?.code || 'WORKSPACE_SYNC_ERROR',
                        message: error?.message || '工作区暂时无法同步',
                    },
                };
            }
        }));
        hydrated.push(...results);
        if (Date.now() >= deadline && index + batch.length < source.length) {
            hydrated.push(...source.slice(index + batch.length).map(candidate => ({
                ...candidate,
                workspace: null,
                workspace_sync_error: {
                    code: 'WORKSPACE_HYDRATE_TIMEOUT',
                    message: '活动任务恢复超出时间预算',
                },
            })));
            break;
        }
    }
    return hydrated;
}

function sessionFromWorkspace(workspace, candidate = {}, record = {}) {
    const snapshot = workspace?.snapshot || candidate?.workflow || {};
    const workflowId = String(snapshot.workflow_id || candidate?.workflow?.workflow_id || record.workflow_id || record.id || '');
    if (!workflowId) return null;
    const parseResults = workspaceItemsToParseResults(workspace);
    return {
        session_id: workflowId,
        workflow_id: workflowId,
        source_filename: workspace?.source_filename || record.source_filename || '未命名文档.docx',
        source_artifact_id: snapshot.source_artifact_id || null,
        state_version: Number(snapshot.state_version || 0),
        group_state_version: Number(snapshot.group_state_version || 0),
        execution_state: snapshot.execution_state || 'CREATED',
        control_state: snapshot.control_state || 'RUNNING',
        result_status: snapshot.result_status || 'IN_PROGRESS',
        cleanup_state: snapshot.cleanup_state || 'NONE',
        status: snapshot.status || 'ACTIVE',
        latest_event_id: snapshot.latest_event_id || null,
        latest_seq: Number(snapshot.latest_seq || 0),
        last_error_code: snapshot.last_error_code || null,
        last_error_message: snapshot.last_error_message || null,
        last_event_id: snapshot.latest_event_id || null,
        parse_results: parseResults,
        active_candidate: candidate,
    };
}

async function adoptWorkflowWorkspace(workspace, { candidate = {}, record = {}, reason = '已恢复任务工作区' } = {}) {
    if (!workspace?.snapshot?.workflow_id) return false;
    const previousWorkflowId = String(currentSession?.session_id || '');
    const session = sessionFromWorkspace(workspace, candidate, record);
    if (!session) return false;
    if (workflowStream && previousWorkflowId && previousWorkflowId !== session.session_id) {
        await workflowStream.close().catch(() => {});
        workflowStream = null;
    }
    if (previousWorkflowId !== session.session_id) itemContentCache.clear();
    currentSession = session;
    currentWorkspace = workspace;
    generatedFiles = [];
    activeResultContext = null;
    latestCurrentResultEvent = null;
    lastStats = null;
    lastDownloadEvent = null;
    sseRetryCount = 0;
    resetLogTimeline('正在恢复任务记录…');
    lastGenerationConfig = workspace.configuration?.effective
        ? normalizeClientConfig(workspace.configuration.effective)
        : lastGenerationConfig;
    workflowStore?.prepare?.(session.session_id, {
        workflow: { workflow_id: session.session_id, ...workspace.snapshot },
        lastEventId: session.last_event_id,
        lastSeq: session.latest_seq,
    });
    workflowStore?.hydrate?.(workspace, { snapshot: workspace.snapshot });
    if (workspace.configuration?.effective) {
        applyConfigToForm(workspace.configuration.effective, { includeRoles: true });
    }
    updateSessionLabels(session.source_filename, session.parse_results);
    renderContentReview(session.parse_results);
    renderWorkspaceShell(workspace, workspace.snapshot);
    renderActiveCandidateHint([]);

    const state = workspaceUserState(workspace, workspace.snapshot);
    generationResult = null;
    transientGenerationErrorMessage = '';
    const recovery = generationRecoveryPresentation(workspace, state);
    if (recovery) {
        addLogEntry({
            level: 'error',
            stage: 'complete',
            kind: 'summary',
            status: 'error',
            key: 'task:recovery',
            title: recovery.title,
            detail: recovery.message,
        });
    }
    if (isAcceptedGenerationSnapshot(workspace.snapshot)) {
        // Opening an active task from history must land on the same generation
        // console as an in-session task. Otherwise the workspace is hydrated
        // successfully but the user remains on the history page and cannot
        // see the authoritative pause/resume/stop state.
        goToStep(3);
        adoptAcceptedGeneration(session, workspace.snapshot, { reason });
    } else {
        isGenerating = false;
        generationStartInFlight = false;
        clearGenerationStartupTimer();
        if (isHardStoppedWorkflowSnapshot(workspace.snapshot)) {
            // A cancelled run is an immutable history fact, not a page the
            // user can resume. Reopen it directly in the editable voice step;
            // clicking Generate there will create the next run explicitly.
            hideGenerationRecovery();
            goToStep(2);
            setActiveWorkspaceView('voice');
            renderVoiceWorkspace();
            $('status-text').textContent = '当前生成已停止，请确认声音配置后重新生成。';
        } else if (state.view === 'issues' || ['WAITING_RETRY', 'WAITING_USER'].includes(state.key)) {
            goToStep(3);
        } else if (state.key === 'CREATED' && session.parse_results.length > 0) {
            showContentReview();
        } else {
            goToStep(3);
        }
        renderWorkspaceShell(workspace, workspace.snapshot);
        updateGenerationCancelUI();
    }
    return true;
}

async function refreshHistoryRecords({ showLoading = true } = {}) {
    const requestToken = ++historyRequestToken;
    if (showLoading && currentView === 'history') renderHistoryMessage('正在读取本机历史记录…');
    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        const [data, activePage] = await Promise.all([
            workflowApi.listWorkflows(100),
            typeof workflowApi.listActiveWorkflowPage === 'function'
                ? workflowApi.listActiveWorkflowPage(ACTIVE_WORKFLOW_HYDRATE_LIMIT).catch(() => ({ workflows: [], truncated: false }))
                : typeof workflowApi.listActiveWorkflows === 'function'
                    ? workflowApi.listActiveWorkflows(ACTIVE_WORKFLOW_HYDRATE_LIMIT).then(workflows => ({ workflows, truncated: false })).catch(() => ({ workflows: [], truncated: false }))
                    : Promise.resolve({ workflows: [], truncated: false }),
        ]);
        if (requestToken !== historyRequestToken) return historyRecords;
        const activeCandidates = Array.isArray(activePage?.workflows) ? activePage.workflows : [];
        const hydratedCandidates = await hydrateActiveWorkflowCandidates(activeCandidates);
        if (requestToken !== historyRequestToken) return historyRecords;
        const activeByWorkflowId = new Map(
            hydratedCandidates
                .map(candidate => [String(candidate?.workflow?.workflow_id || ''), candidate])
                .filter(([workflowId]) => workflowId),
        );
        activeWorkflowListTruncated = activePage?.truncated === true;
        activeWorkflowCandidates = hydratedCandidates;
        workflowStore?.setActiveCandidates?.(activeWorkflowCandidates);
        renderActiveCandidateHint(activeWorkflowCandidates);
        historyRecords = (Array.isArray(data) ? data : [])
            .slice(0, 20)
            .map(record => ({
                ...record,
                active_candidate: activeByWorkflowId.get(String(record.workflow_id || record.id || '')) || null,
            }));
        setHistoryCounts(historyRecords.length);
        if (currentView === 'history') renderHistoryRecords(historyRecords);
        return historyRecords;
    } catch (error) {
        if (requestToken !== historyRequestToken) return historyRecords;
        console.error('读取历史记录失败:', error);
        if (currentView === 'history') renderHistoryMessage('历史记录暂时无法读取，请确认生成服务已连接后重试。', 'history-error');
        return historyRecords;
    }
}

async function viewHistoryRecord(historyId) {
    if (!historyId || isRestarting) return;
    const requestToken = ++historyRequestToken;
    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        const record = historyRecords.find(item => item.id === historyId || item.workflow_id === historyId) || {};
        const [workflow, items, artifacts, workspace] = await Promise.all([
            workflowApi.getWorkflow(historyId),
            workflowApi.listItems(historyId),
            workflowApi.listArtifacts(historyId),
            typeof workflowApi.getWorkspace === 'function'
                ? workflowApi.getWorkspace(historyId).catch(error => {
                    console.warn('读取历史工作区失败，保留基础历史详情:', historyId, error);
                    return null;
                })
                : Promise.resolve(null),
        ]);
        if (requestToken !== historyRequestToken) return;
        const authoritativeSnapshot = workspace?.snapshot || workflow;
        const files = resultFilesFromArtifacts(items, artifacts, workspace);
        if (!isTerminalWorkflowSnapshot(authoritativeSnapshot)) {
            const candidate = record.active_candidate || {};
            if (workspace) {
                historyReturnStep = 1;
                await adoptWorkflowWorkspace(workspace, {
                    candidate,
                    record,
                    reason: '已恢复任务工作区',
                });
                showToast('已打开未结束任务；暂停任务不会自动恢复', 'info');
                return;
            }
            const progress = workspace?.progress || {};
            const completed = nonNegativeCount(progress.completed, files.length);
            const total = nonNegativeCount(progress.total, nonNegativeCount(record.total, completed));
            await showAlertDialog({
                kicker: '活动任务',
                title: record.source_filename || '未命名文档',
                message: `当前进度：${completed} / ${total}；任务尚未结束。`,
                detail: candidate.workspace_sync_error?.message || '任务工作区暂时无法同步，请稍后重试。',
                tone: 'warning',
                confirmLabel: '知道了',
            });
            return;
        }
        const progress = workspace?.progress || {};
        const delivery = workspace?.delivery || {};
        const { completed, total, failed, cancelled } = historyProgressCounts(progress, record, files.length);
        const failedItems = (Array.isArray(record.failed_items) ? record.failed_items : [])
            .filter(item => !['CANCELLED', 'SKIPPED'].includes(String(item?.status || '')));
        const context = {
            mode: 'history',
            recordId: historyId,
            workflowId: historyId,
            sourceFilename: record.source_filename || '未命名文档.docx',
            files,
            artifacts: Array.isArray(workspace?.artifacts) ? workspace.artifacts : artifacts,
            completed,
            failed,
            cancelled,
            total: total || nonNegativeCount(authoritativeSnapshot.item_count, files.length),
            format: files[0]?.format
                || workspace?.configuration?.effective?.format
                || record.format
                || null,
            generationMode: record.generation_mode || GENERATION_MODE_SINGLE,
            preview: Boolean(record.preview),
            zipAvailable: Boolean(delivery.zip_available),
            zipArtifactId: delivery.zip_artifact_id || null,
            failedItems,
            delivery,
            workspace,
            stateVersion: Number(authoritativeSnapshot.state_version || record.state_version || 0),
            executionState: authoritativeSnapshot.execution_state || record.execution_state || null,
            resultStatus: authoritativeSnapshot.result_status || record.result_status || null,
        };
        buildResultPage({
            workflow_id: historyId,
            completed: context.completed,
            failed: context.failed,
            cancelled: context.cancelled,
            total: context.total,
            failed_items: context.failedItems,
        }, context);
        activateStandalonePage('page-4', 'history-result');
        const backToHistoryBtn = $('back-to-history-btn');
        if (backToHistoryBtn) backToHistoryBtn.hidden = false;
        requestAnimationFrame(() => requestAnimationFrame(activateResultWaveforms));
    } catch (error) {
        if (requestToken !== historyRequestToken) return;
        console.error('读取历史详情失败:', error);
        showToast('这条历史记录暂时无法打开，可能文件已被移除');
        await refreshHistoryRecords({ showLoading: false });
    }
}

async function deleteHistoryRecord(record, button) {
    const workflowId = record?.workflow_id || record?.id;
    if (!workflowId || isRestarting) return;
    const terminal = isTerminalWorkflowSnapshot(record);
    const action = terminal ? 'archive' : 'delete';
    const filename = record.source_filename || '未命名文档';
    const fileCount = Math.max(0, Number(record.available_files) || 0);
    const confirmed = await showConfirmDialog({
        kicker: '历史记录',
        title: terminal ? '归档这条生成记录？' : '删除这条未完成任务？',
        message: terminal
            ? `将从历史列表隐藏「${filename}」及其 ${fileCount} 个音频文件。`
            : `将永久删除「${filename}」及其 ${fileCount} 个本地音频文件。`,
        detail: terminal
            ? '审计事件和 Artifact 会保留，不会物理删除文件。'
            : '本地工作流、Artifact、源文件暂存数据和本地文件都会清理；若任务已经提交到外部服务，本操作不会撤销外部提交。删除后无法恢复。',
        tone: 'danger',
        confirmLabel: terminal ? '归档记录' : '删除任务',
    });
    if (!confirmed) return;
    historyRequestToken++;
    if (button) button.disabled = true;
    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        const request = {
            expected_state_version: Number(record.state_version || 0),
            reason: terminal ? 'desktop-history-archive' : 'desktop-history-delete',
        };
        if (action === 'archive') await workflowApi.archiveWorkflow(workflowId, request);
        else await workflowApi.deleteWorkflow(workflowId, request);
        const archivedCurrentResult = latestCurrentResultEvent?.workflow_id === workflowId
            || currentSession?.session_id === workflowId;
        historyRecords = historyRecords.filter(item => item.id !== workflowId && item.workflow_id !== workflowId);
        if (archivedCurrentResult) {
            currentSession = null;
            generatedFiles = [];
            activeResultContext = null;
            latestCurrentResultEvent = null;
            historyReturnStep = 1;
            const uploadZone = $('upload-zone');
            uploadZone?.classList.remove('has-file', 'is-processing', 'dragover');
            uploadZone?.setAttribute('aria-busy', 'false');
            const uploadTitle = uploadZone?.querySelector('.upload-text-large');
            const uploadHint = uploadZone?.querySelector('.upload-hint');
            if (uploadTitle) uploadTitle.textContent = '拖拽文档到这里，或点击选择';
            if (uploadHint) uploadHint.textContent = '支持 .docx / .xlsx 文件 · 选择后会自动解析';
            updateSessionLabels();
            if ($('stats-bar')) $('stats-bar').replaceChildren();
            if ($('status-text')) $('status-text').textContent = '就绪';
            if ($('history-back-btn')) $('history-back-btn').textContent = '返回导入文档';
        }
        setHistoryCounts(historyRecords.length);
        if (currentView === 'history') renderHistoryRecords(historyRecords);
        showToast(
            action === 'archive'
                ? (archivedCurrentResult ? '当前任务已归档，Artifact 仍保留' : '历史记录已归档')
                : '未完成任务及其相关本地数据已删除',
        );
    } catch (error) {
        console.error(action === 'archive' ? '归档历史记录失败:' : '删除未完成任务失败:', error);
        showToast(`${action === 'archive' ? '归档' : '删除'}失败：${error.message || '请稍后重试'}`);
        if (button) button.disabled = false;
    }
}

// ============================================================================
// Step 1: 文件上传
// ============================================================================

async function selectFile() {
    if (isParsing || sourceImportInFlight || sourceFileDialogInFlight || isRestarting || isForcedUpdateBlocking() || $('upload-zone')?.getAttribute('aria-disabled') === 'true') {
        return;
    }
    if (isElectron) {
        sourceFileDialogInFlight = true;
        try {
            const result = typeof window.electronAPI.selectFileStream === 'function'
                ? await window.electronAPI.selectFileStream()
                : await window.electronAPI.selectFile();
            if (result?.success && result.sourceFileId && result.fileName) {
                void processSourceFileReference(result.sourceFileId, result.fileName, result.sizeBytes);
            } else if (result?.success && result.bytes && result.fileName) {
                // Compatibility fallback for an older preload that does not
                // expose the opaque native file stream handle.
                void processSourceBytes(result.bytes, result.fileName);
            } else if (result != null && result?.reason !== 'user-cancelled') {
                await showNativeFileDialogError('选择文档失败', result?.reason ? result : {
                reason: result?.success === true ? 'dialog-error' : 'ipc-error',
                error: '主进程未返回有效的文档流',
                });
            }
        } catch (error) {
            console.error('打开文件选择框失败:', error);
            await showNativeFileDialogError('选择文档失败', {
                reason: 'ipc-error',
                error: error?.message,
            });
        } finally {
            sourceFileDialogInFlight = false;
        }
    } else {
        $('hidden-file-input').click();
    }
}

function handleFileSelected(file) {
    if (isParsing || sourceImportInFlight || isRestarting || isForcedUpdateBlocking() || $('upload-zone')?.getAttribute('aria-disabled') === 'true') return;
    void ingestSourceFile(file);
}

function isSupportedSourceFile(file) {
    const filename = String(file?.name || '').split(/[\\/]/).pop().toLowerCase();
    return ['.docx', '.xlsx'].some(extension => filename.endsWith(extension));
}

function setGlobalFileDropActive(active) {
    globalFileDragActive = Boolean(active) && !isForcedUpdateBlocking();
    const overlay = $('global-drop-overlay');
    if (overlay) {
        overlay.hidden = !globalFileDragActive;
        overlay.setAttribute('aria-hidden', globalFileDragActive ? 'false' : 'true');
    }
    document.body.classList.toggle('has-global-file-drop', globalFileDragActive);
}

function isFileDragEvent(event) {
    return Array.from(event?.dataTransfer?.types || []).includes('Files');
}

function hasActiveTaskForIncomingFile() {
    if (!currentSession?.session_id) return false;
    const snapshot = currentWorkspace?.snapshot || currentWorkspace || currentSession;
    return !isTerminalWorkflowSnapshot(snapshot) && generationResult !== 'done';
}

async function handleIncomingSourceFile(file) {
    if (!file || incomingFileDropInFlight || isForcedUpdateBlocking()) return;
    if (!isSupportedSourceFile(file)) {
        setUploadFeedback('error', '文件格式不支持，请重新选择 .docx 或 .xlsx 文档。');
        showToast('请选择 .docx 或 .xlsx 格式的文档', 'error');
        return;
    }
    if (isParsing || sourceImportInFlight || isRestarting) {
        showToast('当前文档仍在导入，请等待本次操作完成', 'warning');
        return;
    }
    incomingFileDropInFlight = true;
    try {
        if (hasActiveTaskForIncomingFile()) {
            const filename = String(file.name || '新文档').split(/[\\/]/).pop();
            const confirmed = await showConfirmDialog({
                kicker: '检测到新文档',
                title: '使用这个文件新建任务？',
                message: '当前任务仍在进行中。确认后将切换到「' + filename + '」，当前任务会先完成清理。',
                detail: '当前任务不会从历史记录中删除；如果任务已经产生结果，可稍后从历史记录继续查看。',
                tone: 'warning',
                confirmLabel: '使用此文件',
            });
            if (!confirmed) return;
        }

        if (currentSession?.session_id) {
            setRestartingUI(true);
            let cleanupConfirmed = false;
            try {
                cleanupConfirmed = await restart({ notify: false });
            } finally {
                setRestartingUI(false);
                setAppInteractive($('service-state')?.classList.contains('is-ready') === true);
            }
            if (!cleanupConfirmed) {
                showToast('旧任务尚未完成清理，暂未导入新文件', 'warning');
                return;
            }
        }
        handleFileSelected(file);
    } finally {
        incomingFileDropInFlight = false;
    }
}

async function readBoundedSourceFile(file, maxBytes, signal) {
    if (typeof file?.stream !== 'function') {
        const error = new Error('当前环境不支持流式读取源文档');
        error.code = 'STREAM_UNSUPPORTED';
        throw error;
    }
    const reader = file.stream().getReader();
    const chunks = [];
    let total = 0;
    try {
        while (true) {
            throwIfSourceImportAborted(signal);
            const part = await reader.read();
            if (part.done) break;
            const chunk = part.value instanceof Uint8Array
                ? part.value
                : new Uint8Array(part.value || []);
            if (chunk.byteLength === 0) continue;
            if (total + chunk.byteLength > maxBytes) {
                const error = new Error(`兼容导入仅支持不超过 ${formatSourceBytes(maxBytes)} 的文档`);
                error.code = 'SOURCE_SIZE_LIMIT';
                throw error;
            }
            chunks.push(chunk);
            total += chunk.byteLength;
            updateSourceImportProgress('正在读取兼容文档', total, Number(file.size) || total);
        }
    } catch (error) {
        await reader.cancel().catch(() => {});
        throw error;
    } finally {
        reader.releaseLock?.();
    }
    const expectedSize = Number(file.size);
    if (Number.isSafeInteger(expectedSize) && total !== expectedSize) {
        const error = new Error('文档流读取长度与文件大小不一致');
        error.code = 'SOURCE_SIZE_MISMATCH';
        throw error;
    }
    const bytes = new Uint8Array(total);
    let offset = 0;
    chunks.forEach(chunk => {
        bytes.set(chunk, offset);
        offset += chunk.byteLength;
    });
    return bytes;
}

/**
 * 导入拖拽/选择的源文档。Electron 下通过主进程分块暂存：渲染层每次只
 * 持有一个分块（约 4MB），文件内容由主进程在允许目录内落盘后按一次性
 * 句柄流式上传，300MB 级文档不再整块进入渲染进程内存。暂存不可用时
 * 只允许明确标记的 <=16MiB 兼容路径，不能悄悄把大文档聚合进内存。
 */
async function ingestSourceFile(file) {
    if (!file || typeof file.slice !== 'function' || isForcedUpdateBlocking()) return;
    const filename = String(file.name || '').split(/[\\/]/).pop() || '';
    const extension = filename.toLowerCase().slice(filename.lastIndexOf('.'));
    const size = Number(file.size);
    if (!['.docx', '.xlsx'].includes(extension)) {
        setUploadFeedback('error', '文件格式不支持，请重新选择 .docx 或 .xlsx 文档。');
        showToast('请选择 .docx 或 .xlsx 格式的文档', 'error');
        return;
    }
    if (!Number.isSafeInteger(size) || size <= 0) {
        setUploadFeedback('error', '文档大小无效，请重新选择文件。');
        showToast('文档大小无效，请重新选择', 'error');
        return;
    }
    const controller = new AbortController();
    sourceImportController = controller;
    sourceImportInFlight = true;
    const uploadZone = $('upload-zone');
    const uploadTitle = uploadZone?.querySelector('.upload-text-large');
    const uploadHint = uploadZone?.querySelector('.upload-hint');
    if (uploadTitle) uploadTitle.textContent = filename;
    if (uploadHint) uploadHint.textContent = '正在接收文档…';
    uploadZone?.classList.add('has-file');
    setUploadParsing(true);
    updateSourceImportProgress('准备读取文档', 0, size);
    setUploadFeedback('info', `正在准备导入 ${filename} · ${formatSourceBytes(size)}`);
    $('status-text').textContent = `正在导入: ${filename}`;
    const staging = isElectron ? window.electronAPI?.sourceUpload : null;
    if (staging && typeof staging.begin === 'function') {
        let uploadId = null;
        let sourceProcessingStarted = false;
        try {
            throwIfSourceImportAborted(controller.signal);
            const opened = await staging.begin({ fileName: filename, sizeBytes: size });
            uploadId = opened.uploadId;
            sourceStagingUploadId = uploadId;
            const chunkSize = Number(opened.chunkSize) || 4 * 1024 * 1024;
            let offset = 0;
            while (offset < size) {
                throwIfSourceImportAborted(controller.signal);
                updateSourceImportProgress('正在读取源文档', offset, size);
                const chunk = new Uint8Array(await file.slice(offset, offset + chunkSize).arrayBuffer());
                throwIfSourceImportAborted(controller.signal);
                if (chunk.byteLength <= 0) throw new Error('文档分块为空，无法继续导入');
                await staging.write({ uploadId, offset, bytes: chunk });
                offset += chunk.byteLength;
                updateSourceImportProgress('正在上传源文档', offset, size);
                setUploadFeedback('info', `正在上传源文档 · ${formatSourceBytes(offset)} / ${formatSourceBytes(size)}`);
            }
            throwIfSourceImportAborted(controller.signal);
            const completed = await staging.complete(uploadId);
            sourceStagingUploadId = null;
            if (!completed?.success || !completed.sourceFileId) {
                throw new Error(completed?.reason ? `文档流式导入未通过校验：${completed.reason}` : '文档流式导入失败');
            }
            updateSourceImportProgress('正在解析文档结构');
            sourceProcessingStarted = true;
            await processSourceFileReference(completed.sourceFileId, completed.fileName, completed.sizeBytes, { controller });
            return;
        } catch (error) {
            if (uploadId) await staging.abort(uploadId).catch(() => {});
            sourceStagingUploadId = null;
            if (sourceProcessingStarted) return;
            if (error?.name === 'AbortError') {
                setUploadFeedback('info', '已停止导入，可重新选择文档。');
                updateSourceImportProgress();
                if (sourceImportController === controller) sourceImportController = null;
                sourceImportInFlight = false;
                sourceTransportUploadId = null;
                setUploadParsing(false);
                return;
            }
            if (size > MAX_BROWSER_SOURCE_BYTES) {
                console.error('文档流式导入失败，拒绝大文件兼容回退:', error);
                setUploadFeedback('error', '当前环境无法继续流式导入大文件，请在 Electron 中重试，或选择不超过 16MiB 的文档。');
                showToast('大文件流式导入失败，未继续整块读取', 'error');
                updateSourceImportProgress();
                if (sourceImportController === controller) sourceImportController = null;
                sourceImportInFlight = false;
                sourceTransportUploadId = null;
                setUploadParsing(false);
                return;
            }
            console.warn('文档流式导入失败，使用受限兼容读取:', error);
            showToast('流式导入不可用，改用不超过 16MiB 的兼容读取', 'warning');
        }
    }
    if (size > MAX_BROWSER_SOURCE_BYTES) {
        setUploadFeedback('error', '当前环境不支持超过 16MiB 的兼容导入，请使用 Electron 原生选择/拖拽，或选择较小文档。');
        updateSourceImportProgress();
        sourceImportInFlight = false;
        sourceImportController = null;
        sourceTransportUploadId = null;
        setUploadParsing(false);
        return;
    }
    if (!workflowApi) {
        setUploadFeedback('error', '当前页面没有连接桌面工作流服务，请使用小猪wordTTS桌面应用导入文档。');
        showToast('工作流服务未连接，请使用桌面应用重试', 'error');
        updateSourceImportProgress();
        sourceImportInFlight = false;
        sourceImportController = null;
        sourceTransportUploadId = null;
        setUploadParsing(false);
        return;
    }
    try {
        throwIfSourceImportAborted(controller.signal);
        updateSourceImportProgress('正在读取兼容文档', 0, size);
        const bytes = await readBoundedSourceFile(file, MAX_BROWSER_SOURCE_BYTES, controller.signal);
        throwIfSourceImportAborted(controller.signal);
        await processSourceBytes(bytes, filename, { controller });
    } catch (error) {
        if (error?.name !== 'AbortError') {
            console.error('兼容方式读取文档失败:', error);
            setUploadFeedback('error', `文档读取失败：${error.message || '请重新选择文件'}`);
        } else {
            setUploadFeedback('info', '已停止导入，可重新选择文档。');
        }
    } finally {
        if (sourceImportController === controller) sourceImportController = null;
        sourceImportInFlight = false;
        sourceTransportUploadId = null;
        updateSourceImportProgress();
        setUploadParsing(false);
    }
}

let isParsing = false;  // 防止解析重入

async function processSourceBytes(bytes, filename, options = {}) {
    return processSourceContent(bytes, filename, bytes?.byteLength, options);
}

async function processSourceFileReference(sourceFileId, filename, sizeBytes, options = {}) {
    const size = Number(sizeBytes);
    if (!sourceFileId || !Number.isSafeInteger(size) || size <= 0) {
        void releaseNativeSourceFile(sourceFileId);
        showToast('文档大小无效，请重新选择', 'error');
        return;
    }
    return processSourceContent({ sourceFileId: String(sourceFileId) }, filename, size, options);
}

async function releaseNativeSourceFile(sourceFileId) {
    if (!sourceFileId || !isElectron || typeof window.electronAPI?.releaseSourceFile !== 'function') return;
    try {
        await window.electronAPI.releaseSourceFile(String(sourceFileId));
    } catch (error) {
        console.warn('释放原生文档句柄失败:', error);
    }
}

async function processSourceContent(content, filename, expectedSizeBytes, options = {}) {
    const isBytes = content instanceof Uint8Array;
    const hasSourceFileReference = Boolean(content && typeof content === 'object' && content.sourceFileId);
    const sourceFileId = hasSourceFileReference ? String(content.sourceFileId) : '';
    if (isForcedUpdateBlocking()) {
        void releaseNativeSourceFile(sourceFileId);
        return;
    }
    if (isParsing || isRestarting) {
        void releaseNativeSourceFile(sourceFileId);
        return;  // 防止重入
    }
    const expectedSize = Number(expectedSizeBytes);
    if ((!isBytes && !hasSourceFileReference) || !Number.isSafeInteger(expectedSize) || expectedSize <= 0) {
        void releaseNativeSourceFile(sourceFileId);
        showToast('文档内容为空，请重新选择', 'error');
        return;
    }
    const safeFilename = String(filename || 'source.docx').split(/[\\/]/).pop() || 'source.docx';
    const extension = safeFilename.toLowerCase().slice(safeFilename.lastIndexOf('.'));
    if (!['.docx', '.xlsx'].includes(extension)) {
        void releaseNativeSourceFile(sourceFileId);
        showToast('请选择 .docx 或 .xlsx 格式的文档', 'error');
        return;
    }
    isParsing = true;
    const attemptId = ++parseAttemptId;
    const controller = options.controller || sourceImportController || new AbortController();
    if (!sourceImportController) {
        sourceImportController = controller;
        sourceImportInFlight = true;
    }
    parseAbortController = controller;

    const uploadZone = $('upload-zone');
    uploadZone.classList.add('has-file');
    setUploadParsing(true);
    setUploadFeedback('info', '正在读取并核对文档结构，请稍候…');
    uploadZone.querySelector('.upload-text-large').textContent = safeFilename;
    uploadZone.querySelector('.upload-hint').textContent = '正在解析文档结构...';
    $('status-text').textContent = `正在解析: ${safeFilename}`;

    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        throwIfSourceImportAborted(controller.signal);
        const initialConfiguration = buildWorkflowConfiguration(
            collectConfig(false),
            safeFilename,
            currentConfig?.account_scope,
        );
        const draft = await workflowApi.createWorkflow({
            workflow_type: 'tts',
            configuration: initialConfiguration,
        });
        throwIfSourceImportAborted(controller.signal);
        const imported = await workflowApi.createSourceImport(draft.workflow_id, {
            metadata: { filename: safeFilename },
            expected_size_bytes: expectedSize,
            content_type: safeFilename.toLowerCase().endsWith('.xlsx')
                ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        });
        sourceImportId = imported.source_import_id || null;
        setUploadParsing(true);
        updateSourceImportProgress('正在写入受控存储');
        throwIfSourceImportAborted(controller.signal);
        sourceTransportUploadId = `source-upload-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
        await workflowApi.writeSourceImport(imported.source_import_id, imported.staging_generation, content, {
            signal: controller.signal,
            uploadId: sourceTransportUploadId,
            onProgress: ({ receivedBytes, totalBytes }) => {
                updateSourceImportProgress('正在上传源文档', receivedBytes, totalBytes || expectedSize);
                setUploadFeedback(
                    'info',
                    `正在上传源文档 · ${formatSourceBytes(receivedBytes)} / ${formatSourceBytes(totalBytes || expectedSize)}`,
                );
            },
        });
        throwIfSourceImportAborted(controller.signal);
        const ready = await workflowApi.getSourceImport(imported.source_import_id);
        throwIfSourceImportAborted(controller.signal);
        if (!ready.source_artifact_id) throw new Error('文档内容未能写入受控存储');
        // Committing the source artifact advances the workflow aggregate's
        // state_version. Re-read it before publishing parse output instead of
        // reusing the draft version captured before the upload.
        const sourceWorkflow = await workflowApi.getWorkflow(draft.workflow_id);
        throwIfSourceImportAborted(controller.signal);
        const data = await workflowApi.parseWorkflow(draft.workflow_id, {
            expected_state_version: Number(sourceWorkflow?.state_version ?? draft.state_version),
            source_artifact_id: ready.source_artifact_id,
        });
        if (controller.signal.aborted || attemptId !== parseAttemptId) return;
        const parseResults = Array.isArray(data.parse_results) ? data.parse_results : [];
        if (parseResults.length === 0) {
            throw new Error('未识别到支持的题型内容，请检查文档结构后重试');
        }
        resetTaskVoiceConfiguration();
        currentSession = {
            session_id: draft.workflow_id,
            source_filename: data.source_filename || safeFilename,
            source_artifact_id: data.source_artifact_id || ready.source_artifact_id,
            import_id: imported.source_import_id,
            state_version: Number(data.state_version || data.current_snapshot?.state_version || draft.state_version),
            parse_results: parseResults,
        };
        currentWorkspace = null;
        workflowStore?.prepare?.(currentSession.session_id, {
            workflow: {
                workflow_id: currentSession.session_id,
                state_version: currentSession.state_version,
                execution_state: 'PREPARING',
                control_state: 'RUNNING',
                result_status: 'IN_PROGRESS',
            },
            lastSeq: 0,
        });

        updateSessionLabels(currentSession.source_filename, currentSession.parse_results);
        renderContentReview(currentSession.parse_results);
        uploadZone.querySelector('.upload-hint').textContent = '解析完成，正在打开声音配置';
        setUploadFeedback('success', `解析完成：已识别 ${summarizeParseResults(currentSession.parse_results).total} 条内容。`);
        $('status-text').textContent = `解析成功 — ${currentSession.source_filename}`;
        showToast('文档解析成功，进入配置步骤');

        // 解析完成后先进入可编辑核对，再由用户确认后配置声音。
        showContentReview();
        void hydrateWorkflowWorkspace(currentSession.session_id, { silent: true });

    } catch (err) {
        if (err.name === 'AbortError' || attemptId !== parseAttemptId) {
            if (err.name === 'AbortError' && sourceImportId) {
                await abortSourceImportIfPossible(sourceImportId);
            }
            if (attemptId === parseAttemptId) {
                setUploadFeedback('info', sourceImportId
                    ? '已停止等待；源文件状态已保留，请确认后再重新导入。'
                    : '已停止导入，可重新选择文档。');
                $('status-text').textContent = '已停止导入';
            }
            return;
        }
        const errorMessage = String(err?.message || '未知错误');
        const startupRecoveryFailed = err?.code === 'PERSISTENCE_ERROR'
            && /workflow startup recovery failed/i.test(errorMessage);
        const feedbackMessage = startupRecoveryFailed
            ? '工作流启动恢复失败，请重启应用后重试。'
            : `文档解析失败：${errorMessage}`;
        console.error('导入失败:', err);
        showToast(startupRecoveryFailed ? '工作流启动恢复失败，请重启应用后重试' : `解析失败: ${errorMessage}`, 'error');
        uploadZone.classList.remove('has-file');
        uploadZone.querySelector('.upload-text-large').textContent = '拖拽文档到这里，或点击选择';
        uploadZone.querySelector('.upload-hint').textContent = '请检查文档格式或内容后重新选择';
        setUploadFeedback('error', feedbackMessage);
        $('status-text').textContent = startupRecoveryFailed
            ? '工作流启动恢复失败，请重启应用'
            : '文档解析失败，请重新选择';
    } finally {
        if (attemptId === parseAttemptId) {
            if (parseAbortController === controller) parseAbortController = null;
            isParsing = false;
            if (sourceImportController === controller) {
                sourceImportController = null;
                sourceImportInFlight = false;
                sourceImportId = null;
                sourceTransportUploadId = null;
            }
            // Clear the flags before syncing the controls. Otherwise the
            // completed parse leaves the toolbar's new-task button disabled.
            setUploadParsing(false);
            updateSourceImportProgress();
        }
        await releaseNativeSourceFile(sourceFileId);
    }
}

// ============================================================================
// Step 2 & 3: 配置 & 生成
// ============================================================================

function collectConfig(useDefaults) {
    if (useDefaults) {
        return normalizeClientConfig({
            generation_mode: DEFAULT_GENERATION_MODE,
            rate: 50,
            volume: 50,
            pitch: 50,
            format: 'mp3',
            quality: '128 kbps（标准）',
            preview: false,
            default_female_voice: currentConfig?.default_female_voice || 'amanda',
            default_male_voice: currentConfig?.default_male_voice || 'george',
            role_configs: {
                [DEFAULT_FEMALE_ROLE_KEY]: createDefaultVoiceParams(DEFAULT_FEMALE_ROLE_KEY),
                [DEFAULT_MALE_ROLE_KEY]: createDefaultVoiceParams(DEFAULT_MALE_ROLE_KEY),
            },
            role_voices: {},
        });
    }
    const activeParams = activeVoiceParams();
    return normalizeClientConfig({
        generation_mode: selectedGenerationMode(),
        rate: activeParams.rate,
        volume: activeParams.volume,
        pitch: activeParams.pitch,
        // 不从页面控件读取格式；格式始终由当前产品规则固定为 MP3。
        format: 'mp3',
        quality: $('quality').value,
        preview: $('preview').checked,
        default_female_voice: selectedDefaultFemaleVoice,
        default_male_voice: selectedDefaultMaleVoice,
        role_configs: voiceParamConfigs,
        role_voices: roleVoiceMap,
    });
}

function buildWorkflowConfiguration(config, sourceFilename = '', accountScope = '') {
    const normalized = normalizeClientConfig(config);
    const rawFilename = String(sourceFilename || config?.source_filename || '').trim().split(/[\\/]/).pop() || 'source.docx';
    const sourceSuffix = ['.docx', '.xlsx'].find(suffix => rawFilename.toLowerCase().endsWith(suffix)) || '';
    const boundedSourceFilename = rawFilename.length <= 256 || !sourceSuffix
        ? rawFilename.slice(0, 256)
        : `${rawFilename.slice(0, -sourceSuffix.length).slice(0, 256 - sourceSuffix.length)}${sourceSuffix}`;
    return {
        ...normalized,
        source_filename: boundedSourceFilename,
        provider: 'xunfei',
        account_scope: String(accountScope || config?.account_scope || 'xunfei-default').trim() || 'xunfei-default',
    };
}

function collectPersistedConfig() {
    return normalizePersistedConfig(collectConfig(false));
}

// ============================================================================
// SSE 进度流
// ============================================================================

async function connectSSE(sessionId) {
    clearSSEReconnectTimer();
    const connectionToken = ++sseConnectionToken;
    if (workflowStream) {
        await workflowStream.close().catch(() => {});
        workflowStream = null;
    }
    if (!workflowApi) return;
    const preparedStore = workflowStore?.prepare?.(sessionId, {
        workflow: currentSession?.session_id === sessionId
            ? { workflow_id: sessionId, ...currentSession }
            : null,
        lastEventId: currentSession?.session_id === sessionId
            ? (currentSession.last_event_id || currentSession.latest_event_id || null)
            : null,
        lastSeq: currentSession?.session_id === sessionId
            ? Number(currentSession.latest_seq || 0)
            : 0,
    });
    const recoveryNoticePending = sseRetryCount > 0;
    try {
        const persistedCursor = preparedStore?.lastEventId || workflowStore?.lastEventIdFor?.(sessionId) || null;
        const stream = await workflowApi.openWorkflowEvents(
            sessionId,
            persistedCursor || currentSession?.last_event_id || currentSession?.latest_event_id || null,
        );
        if (connectionToken !== sseConnectionToken || currentSession?.session_id !== sessionId) {
            await stream.close().catch(() => {});
            return;
        }
        workflowStream = stream;
        stream.onFrame((frame) => {
            if (connectionToken !== sseConnectionToken || currentSession?.session_id !== sessionId) return;
            const reduced = workflowStore ? workflowStore.consume(frame) : { accepted: true };
            if (!reduced.accepted) {
                if (reduced.reason === 'gap') {
                    resetWorkflowEventCursor(sessionId);
                    handleWorkflowStreamError(
                        Object.assign(
                            new Error('workflow event gap: expected ' + reduced.expectedSeq + ', got ' + reduced.actualSeq),
                            { code: 'EVENT_GAP' },
                        ),
                        sessionId,
                        connectionToken,
                    );
                }
                return;
            }
            const event = frame?.event;
            if (frame?.kind === 'snapshot') {
                const snapshot = frame.snapshot?.state || {};
                const snapshotForRender = {
                    ...snapshot,
                    latest_event_id: frame.snapshot?.snapshot_event_id || snapshot.latest_event_id,
                    latest_seq: frame.snapshot?.snapshot_seq ?? snapshot.latest_seq,
                };
                mergeWorkflowSnapshotIntoSession(snapshotForRender, currentSession);
                currentSession.last_event_id = frame.snapshot?.snapshot_event_id
                    || currentSession.last_event_id
                    || currentSession.latest_event_id
                    || null;
                // Snapshot frames are authoritative state, not just a cursor
                // update. Paint them immediately so pause/resume/stop/error
                // states are visible before the debounced workspace refresh.
                renderLiveWorkflowSnapshot(snapshotForRender, currentSession);
                if (snapshot.execution_state === 'TERMINAL') {
                    $('status-text').textContent = '任务已结束';
                }
                scheduleWorkspaceRefresh(sessionId);
                return;
            }
            if (!event) return;
            currentSession.last_event_id = event.event_id || currentSession.last_event_id || null;
            currentSession.latest_event_id = event.event_id || currentSession.latest_event_id || null;
            currentSession.latest_seq = Math.max(Number(currentSession.latest_seq || 0), Number(event.seq || 0));
            handleWorkflowEvent(event, sessionId);
            if (recoveryNoticePending) {
                addLogEntry({
                    level: 'success', stage: 'system', kind: 'notice', status: 'success',
                    key: 'connection:status', title: '生成服务连接已恢复', detail: '任务记录与进度已重新同步',
                });
            }
        });
        stream.onError((error) => handleWorkflowStreamError(error, sessionId, connectionToken));
        sseRetryCount = 0;
    } catch (error) {
        handleWorkflowStreamError(error, sessionId, connectionToken);
    }
}

const WORKFLOW_CONTROL_EVENT_TYPES = new Set([
    'WORKFLOW_PAUSE',
    'WORKFLOW_PAUSED',
    'WORKFLOW_RESUME',
    'WORKFLOW_CANCEL',
    'WORKFLOW_CANCELLED',
]);

function applyWorkflowControlEvent(event, sessionId) {
    const eventType = String(event?.event_type || '');
    if (!WORKFLOW_CONTROL_EVENT_TYPES.has(eventType) || currentSession?.session_id !== sessionId) return false;
    const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
    const patch = {
        WORKFLOW_PAUSE: { control_state: 'PAUSE_REQUESTED' },
        WORKFLOW_PAUSED: { control_state: 'PAUSED' },
        WORKFLOW_RESUME: { control_state: 'RUNNING' },
        WORKFLOW_CANCEL: { control_state: 'TERMINATING', execution_state: 'BLOCKED' },
        WORKFLOW_CANCELLED: {
            control_state: 'TERMINATED',
            execution_state: 'TERMINAL',
            result_status: payload.result_status || 'CANCELLED',
            last_error_code: 'WORKFLOW_CANCELLED',
            last_error_message: String(payload.reason || payload.message || '任务已取消').slice(0, 2000),
        },
    }[eventType];
    if (!patch) return false;
    Object.assign(currentSession, patch);
    const workflowId = String(currentWorkspace?.snapshot?.workflow_id || currentSession.session_id || '');
    if (currentWorkspace && workflowId === String(sessionId)) {
        currentWorkspace = {
            ...currentWorkspace,
            snapshot: {
                ...(currentWorkspace.snapshot || {}),
                ...patch,
                latest_event_id: event.event_id || currentWorkspace.snapshot?.latest_event_id,
                latest_seq: Number(event.seq) || currentWorkspace.snapshot?.latest_seq,
            },
        };
    }
    renderWorkspaceShell(currentWorkspace, currentWorkspace?.snapshot || currentSession);
    return true;
}

function handleWorkflowEvent(event, sessionId) {
    // A retryable failure closes the stream and settles the generation UI.
    // Ignore already-buffered runtime updates that arrive after that error;
    // otherwise a late "still processing" event can overwrite the actionable
    // retry state and make the page look permanently stuck.
    if (!isGenerating || generationResult !== null) return;
    const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
    // The stream is a freshness signal; the server workspace remains the
    // authoritative source for item partitions, blockers and actions.
    scheduleWorkspaceRefresh(sessionId);
    const eventType = String(event.event_type || '');
    const isControlEvent = applyWorkflowControlEvent(event, sessionId);
    if (generationWorkflowOwnsRuntimeView() && !isControlEvent) {
        renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
        return;
    }
    const attemptKey = String(payload.attempt_id || event.attempt_id || payload.submission_id || event.seq || 'current');
    const eventMessage = String(payload.message || payload.error || payload.error_code || '生成任务未能完成');
    if (eventType === 'WORKFLOW_PAUSE') {
        addLogEntry({
            level: 'info', stage: 'synthesize', kind: 'stage', status: 'warning', seq: event.seq,
            key: `workflow:pause:${attemptKey}`, title: '正在暂停任务', detail: '已收到暂停请求，等待当前处理点结束。',
        });
    } else if (eventType === 'WORKFLOW_PAUSED') {
        addLogEntry({
            level: 'info', stage: 'synthesize', kind: 'stage', status: 'warning', seq: event.seq,
            key: `workflow:paused:${attemptKey}`, title: '任务已暂停', detail: '任务停在安全点，不会继续提交新的内容。',
        });
    } else if (eventType === 'WORKFLOW_RESUME') {
        addLogEntry({
            level: 'info', stage: 'synthesize', kind: 'stage', status: 'running', seq: event.seq,
            key: `workflow:resume:${attemptKey}`, title: '正在恢复任务', detail: '恢复请求已提交，正在同步任务进度。',
        });
    } else if (eventType === 'TTS_PLAN_PREPARED') {
        $('status-text').textContent = '已准备生成计划，正在连接讯飞浏览器…';
        $('generation-live-status').textContent = '已准备生成计划，正在连接讯飞浏览器…';
        $('progress-stats').textContent = `正在连接讯飞浏览器 · 0 / ${payload.item_count || summarizeParseResults(currentSession?.parse_results).total || '—'}`;
        addLogEntry({ level: 'info', stage: 'prepare', kind: 'stage', status: 'running', seq: event.seq, key: `tts:plan:${attemptKey}`, title: '已准备生成计划', detail: `共 ${payload.item_count || 0} 条内容` });
    } else if (event.event_type === 'TTS_RUNTIME_STATUS') {
        const elapsed = Number(payload.elapsed_seconds);
        const elapsedText = Number.isFinite(elapsed) && elapsed >= 1 ? `（已等待 ${Math.round(elapsed)} 秒）` : '';
        const waiting = payload.status === 'waiting';
        const message = String(payload.message || (waiting ? '讯飞浏览器正在处理，任务仍在运行' : '正在启动讯飞浏览器会话'));
        $('status-text').textContent = message;
        $('generation-live-status').textContent = message;
        $('progress-stats').textContent = `${message}${elapsedText} · 0 / ${summarizeParseResults(currentSession?.parse_results).total || '—'}`;
        setProgressIndeterminate(true);
        addLogEntry({
            level: 'progress', stage: 'synthesize', kind: 'stage', status: 'running', seq: event.seq,
            key: `tts:runtime:${attemptKey}`,
            title: waiting ? '讯飞浏览器仍在处理' : '正在启动讯飞浏览器',
            detail: `${message}${elapsedText}；如果浏览器窗口被关闭，任务会进入可恢复状态。`,
        });
    } else if (event.event_type === 'TTS_RUNTIME_PROGRESS') {
        const completed = Number(payload.completed_segments);
        const total = Number(payload.total_segments);
        const progress = Number.isFinite(completed) && Number.isFinite(total)
            ? { completed, total }
            : null;
        const itemLabel = payload.item_id ? `条目 ${payload.item_id}` : '当前作品';
        const message = String(payload.message || `讯飞浏览器正在处理${itemLabel}`);
        $('status-text').textContent = message;
        $('generation-live-status').textContent = message;
        if (progress && total > 0) {
            $('progress-stats').textContent = `${message} · ${Math.max(0, completed)} / ${Math.max(0, total)}`;
        }
        addLogEntry({
            level: payload.error ? 'warn' : 'progress', stage: 'synthesize', kind: 'stage',
            status: payload.error ? 'warning' : 'running', seq: event.seq,
            key: `tts:runtime:${attemptKey}`,
            title: payload.error ? '讯飞条目处理需要检查' : '讯飞条目处理进度',
            detail: message,
            progress,
        });
    } else if (event.event_type === 'TTS_SUBMISSION_IN_FLIGHT') {
        setProgressIndeterminate(true);
        $('status-text').textContent = '正在连接讯飞浏览器并提交作品…';
        $('generation-live-status').textContent = '正在连接讯飞浏览器并提交作品…';
        $('progress-stats').textContent = `正在提交讯飞作品 · 0 / ${summarizeParseResults(currentSession?.parse_results).total || '—'}`;
        addLogEntry({ level: 'progress', stage: 'synthesize', kind: 'stage', status: 'running', seq: event.seq, key: `tts:in-flight:${attemptKey}`, title: '正在提交讯飞作品', detail: '已进入外部提交阶段；浏览器启动或恢复期间请保持应用开启。' });
    } else if (event.event_type === 'PROVIDER_RECEIPT_OBSERVED') {
        setProgressIndeterminate(true);
        $('status-text').textContent = '作品已提交，正在下载并核验音频…';
        $('generation-live-status').textContent = '作品已提交，正在下载并核验音频…';
        addLogEntry({ level: 'progress', stage: 'synthesize', kind: 'stage', status: 'running', seq: event.seq, key: `tts:receipt:${attemptKey}`, title: '已找到讯飞作品', detail: payload.receipt_id ? `正在下载作品并核验音频（Receipt ${payload.receipt_id}）` : '正在下载作品并核验音频' });
    } else if (event.event_type === 'TTS_SUBMISSION_AMBIGUOUS') {
        addLogEntry({ level: 'error', stage: 'synthesize', kind: 'summary', status: 'error', seq: event.seq, key: `tts:ambiguous:${attemptKey}`, title: '提交未完成，可重新生成', detail: eventMessage });
        handleSSEEvent({ type: 'error', msg: eventMessage, ambiguous: false });
    } else if (event.event_type === 'TTS_SUBMISSION_REJECTED') {
        addLogEntry({ level: 'error', stage: 'synthesize', kind: 'summary', status: 'error', seq: event.seq, key: `tts:rejected:${attemptKey}`, title: '讯飞提交未被接受', detail: eventMessage });
        handleSSEEvent({ type: 'error', msg: eventMessage, ambiguous: false });
    } else if (event.event_type === 'TTS_OUTPUT_VERIFIED') {
        // The event is evidence of the worker's write, not itself the final
        // UI state.  Read the authoritative snapshot and verified artifact
        // projection before moving to the result page.
        setProgressIndeterminate(true);
        $('status-text').textContent = '音频已写入，正在确认最终任务状态…';
        void finalizeSuccessfulWorkflowEvent({
            type: 'done',
            artifact_ids: payload.artifact_ids || [],
            event_seq: event.seq,
            event_key: `tts:verified:${attemptKey}`,
        }, sessionId).catch((error) => {
            console.warn('核验生成终态失败，保留任务页等待重连:', error);
            $('status-text').textContent = '正在确认最终任务状态，请稍候…';
        });
    } else if (event.event_type === 'GENERATION_TASK_FAILED') {
        void workflowApi?.getWorkflow(sessionId).then((snapshot) => {
            if (currentSession?.session_id !== sessionId) return;
            mergeWorkflowSnapshotIntoSession(snapshot, currentSession);
            const settled = isTerminalWorkflowSnapshot(snapshot)
                || ['WAITING_RETRY', 'WAITING_USER', 'BLOCKED'].includes(snapshot?.execution_state);
            if (!settled) {
                $('status-text').textContent = '生成服务正在恢复任务状态…';
                return;
            }
            // getWorkflow carries the durable error fields but is not a full
            // workspace refresh. Publish it to the live workspace immediately
            // so a scheduled refresh cannot briefly paint the old RUNNING UI.
            renderLiveWorkflowSnapshot(snapshot, currentSession);
            addLogEntry({ level: 'error', stage: 'complete', kind: 'summary', status: 'error', seq: event.seq, key: `task:failed:${attemptKey}`, title: '生成任务未能完成', detail: eventMessage });
            handleSSEEvent({ type: 'error', msg: eventMessage, ambiguous: false });
        }).catch((error) => {
            console.warn('生成失败后同步工作流状态失败，保留任务页:', error);
            $('status-text').textContent = '生成失败，正在等待任务状态同步…';
        });
    } else if (event.event_type === 'WORKFLOW_CANCEL') {
        renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
        $('status-text').textContent = '正在停止生成任务…';
    } else if (event.event_type === 'WORKFLOW_CANCELLED') {
        void finalizeCancelledWorkflowEvent({ event_seq: event.seq }, sessionId).catch((error) => {
            console.warn('取消后同步工作流状态失败:', error);
            $('status-text').textContent = '取消结果刷新失败，请重新打开任务查看。';
        });
    }
}

async function refreshGeneratedArtifacts(sessionId) {
    if (!workflowApi) return [];
    let workspace = null;
    let items;
    let artifacts;
    // The workspace endpoint is the only response that carries item state,
    // verified Artifact facts, and delivery scope from one server snapshot.
    // Reading three endpoints in parallel can combine different revisions and
    // briefly render an item with another attempt's filename or status.
    if (typeof workflowApi.getWorkspace === 'function') {
        workspace = await workflowApi.getWorkspace(sessionId);
    }
    if (workspace && Array.isArray(workspace.items) && Array.isArray(workspace.artifacts)) {
        items = workspace.items;
        artifacts = workspace.artifacts;
    } else {
        // Keep older renderer/API combinations usable, but never prefer this
        // split projection when the authoritative workspace is available.
        [items, artifacts] = await Promise.all([
            workflowApi.listItems(sessionId),
            workflowApi.listArtifacts(sessionId),
        ]);
    }
    // The request can outlive a restart/new task.  Never let a late response
    // from the old workflow overwrite the result list of the current task.
    if (currentSession?.session_id !== sessionId) return [];
    generatedFiles = resultFilesFromArtifacts(items, artifacts, workspace);
    if (workspace) {
        currentWorkspace = workspace;
        workflowStore?.hydrate?.(workspace, { snapshot: workspace.snapshot });
        currentSession.delivery = workspace.delivery ? {
            zip_available: Boolean(workspace.delivery.zip_available),
            zip_artifact_id: workspace.delivery.zip_artifact_id || null,
        } : null;
        if (Array.isArray(workspace.items)) {
            currentSession.parse_results = workspaceItemsToParseResults(workspace);
        }
        currentSession.progress = workspace.progress ? {
            total: nonNegativeCount(workspace.progress.total),
            completed: nonNegativeCount(workspace.progress.completed),
            failed: nonNegativeCount(workspace.progress.failed),
            cancelled: nonNegativeCount(workspace.progress.cancelled),
            skipped: nonNegativeCount(workspace.progress.skipped),
            pending: nonNegativeCount(workspace.progress.pending),
            deliverable: nonNegativeCount(workspace.progress.deliverable, generatedFiles.length),
            deliverable_percent: nonNegativeCount(workspace.progress.deliverable_percent),
        } : null;
        mergeWorkflowSnapshotIntoSession(workspace.snapshot, currentSession);
        renderWorkspaceShell(currentWorkspace, currentSession);
    }
    return generatedFiles;
}

async function finalizeSuccessfulWorkflowEvent(event, sessionId) {
    if (!workflowApi || !sessionId || currentSession?.session_id !== sessionId) return false;
    await refreshGeneratedArtifacts(sessionId);
    if (currentSession?.session_id !== sessionId) return false;
    const workspace = currentWorkspace?.snapshot?.workflow_id === sessionId ? currentWorkspace : null;
    const snapshot = workspace?.snapshot || null;
    if (!isTerminalWorkflowSnapshot(snapshot)
        || !['SUCCEEDED', 'PARTIAL_SUCCESS'].includes(String(snapshot.result_status || ''))) {
        $('status-text').textContent = '音频已写入，任务状态仍在确认中…';
        return false;
    }
    const progress = workspaceProgress(workspace);
    const deliveryBlockers = (Array.isArray(workspace?.blockers) ? workspace.blockers : []).filter(blocker => (
        ['BLOCKING', 'ERROR'].includes(String(blocker?.severity || '').toUpperCase())
        && ['ARTIFACT_MISSING_OR_UNVERIFIED', 'ARTIFACT_FORMAT_UNSUPPORTED', 'ARTIFACT_METADATA_CONFLICT'].includes(String(blocker?.code || '').toUpperCase())
    ));
    if (
        progress.pending > 0
        || progress.deliverable > generatedFiles.length
        || deliveryBlockers.length > 0
    ) {
        const blocker = deliveryBlockers[0];
        $('status-text').textContent = blocker
            ? `任务已结束，但${blocker.title || '交付产物'}仍未通过核验。`
            : '音频已写入，仍有交付产物正在确认中…';
        renderWorkspaceShell(currentWorkspace, currentSession);
        return false;
    }
    addLogEntry({
        level: 'success',
        stage: 'complete',
        kind: 'summary',
        status: 'success',
        seq: event.event_seq,
        key: event.event_key || `workflow:verified:${sessionId}`,
        title: '音频已完成核验',
        detail: '生成文件已写入本地任务空间。',
    });
    handleDone({
        type: 'done',
        workflow_id: sessionId,
        completed: progress.completed,
        failed: progress.failed,
        cancelled: progress.cancelled,
        skipped: progress.skipped,
        total: progress.total,
        file_list: generatedFiles,
    });
    return true;
}

async function finalizeCancelledWorkflowEvent(event, sessionId) {
    if (!workflowApi || !sessionId || currentSession?.session_id !== sessionId) return false;
    await refreshGeneratedArtifacts(sessionId);
    if (currentSession?.session_id !== sessionId) return false;
    const workspace = currentWorkspace?.snapshot?.workflow_id === sessionId ? currentWorkspace : null;
    const snapshot = workspace?.snapshot || null;
    if (!isTerminalWorkflowSnapshot(snapshot)) {
        $('status-text').textContent = '取消结果刷新失败，请重新打开任务查看。';
        return false;
    }
    if (isHardStoppedWorkflowSnapshot(snapshot)) {
        resetGenerationAfterHardStop(currentSession, snapshot);
        return true;
    }
    const progress = workspaceProgress(workspace);
    if (snapshot.result_status === 'SUCCEEDED' || snapshot.result_status === 'PARTIAL_SUCCESS') {
        return finalizeSuccessfulWorkflowEvent({
            type: 'done',
            event_seq: event.event_seq,
            event_key: `workflow:cancelled:${sessionId}`,
        }, sessionId);
    }
    if (snapshot.result_status !== 'CANCELLED') {
        $('status-text').textContent = '任务已结束，正在同步最终结果…';
        return false;
    }
    handleSSEEvent({
        type: 'cancelled',
        completed: progress.completed,
        cancelled: progress.cancelled,
        total: progress.total,
    });
    return true;
}

function resetWorkflowEventCursor(sessionId) {
    workflowStore?.resetCursor?.(sessionId);
    if (currentSession?.session_id !== sessionId) return;
    // The next connection must request the server snapshot. Keeping the old
    // cursor in currentSession would make Store.prepare resurrect it as its
    // initial cursor even after localStorage has been cleared.
    currentSession.last_event_id = null;
    currentSession.latest_event_id = null;
    currentSession.latest_seq = 0;
}

function handleWorkflowStreamError(error, sessionId, connectionToken) {
    if (connectionToken !== sseConnectionToken || currentSession?.session_id !== sessionId || !isGenerating) return;
    const requiresSnapshotResync = error?.code === 'CURSOR_EXPIRED'
        || error?.code === 'EVENT_GAP'
        || Number(error?.status) === 410;
    if (requiresSnapshotResync) resetWorkflowEventCursor(sessionId);
    if (workflowStream) {
        workflowStream.close().catch(() => {});
        workflowStream = null;
    }
    sseRetryCount += 1;
    if (sseRetryCount >= SSE_MAX_RETRIES) {
        handleSSEEvent({ type: 'error', msg: '与生成服务的连接已中断；已写入的任务记录仍然保留' });
        return;
    }
    const delay = Math.min(1000 * (2 ** (sseRetryCount - 1)), 10000);
    sseReconnectTimer = setTimeout(async () => {
        sseReconnectTimer = null;
        if (connectionToken !== sseConnectionToken || !isGenerating) return;
        if (requiresSnapshotResync) await hydrateWorkflowWorkspace(sessionId, { silent: true });
        if (connectionToken === sseConnectionToken && isGenerating) void connectSSE(sessionId);
    }, delay);
}

function handleSSEEvent(event) {
    switch (event.type) {
        case 'log_init':
            addLogEntries(Array.isArray(event.entries) ? event.entries : []);
            break;

        case 'log':
            if (generationWorkflowOwnsRuntimeView()
                && ['running', 'progress', 'info'].includes(String(event.entry?.status || '').toLowerCase())) break;
            addLogEntry(event.entry);
            break;

        case 'stats':
            if (generationWorkflowOwnsRuntimeView()) {
                renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
                break;
            }
            lastStats = event;
            updateProgress(event);
            updateStats(event);
            break;

        case 'status':
            if (generationWorkflowOwnsRuntimeView()) {
                renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
                break;
            }
            $('status-text').textContent = event.text;
            if ($('generation-live-status') && event.text) {
                $('generation-live-status').textContent = event.text;
            }
            break;

        case 'download':
            lastDownloadEvent = event;
            updateFileList(event);
            break;

        case 'done':
            if (generationWorkflowOwnsRuntimeView()) {
                renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
                break;
            }
            // A transport-level done frame is only a freshness signal. The
            // workspace must confirm terminal execution/control/result facts
            // and verified item artifacts before the UI enters delivery.
            void finalizeSuccessfulWorkflowEvent(event, currentSession?.session_id).catch(error => {
                console.warn('完成事件后的工作区核验失败:', error);
                $('status-text').textContent = '正在确认最终任务状态，请稍候…';
            });
            break;

        case 'cancelled':
            if (hardStopNavigationRequested
                && resetGenerationAfterHardStop(currentSession, currentWorkspace?.snapshot || currentSession)) {
                break;
            }
            if (!logEntriesByKey.has('task:summary')) {
                addLogEntry({
                    level: 'warn',
                    stage: 'complete',
                    kind: 'summary',
                    status: 'warning',
                    key: 'task:summary',
                    title: '任务已取消',
                    detail: `已完成 ${event.completed || 0} / ${event.total || 0} 条，已取消 ${event.cancelled || 0} 条`,
                    duration_ms: event.duration_ms,
                });
            }
            generationResult = 'cancelled';
            transientGenerationErrorMessage = '';
            resetGenerateState();
            setProgressIndeterminate(false);
            $('gen-title').textContent = '任务已取消';
            $('generation-file-name').textContent = `已取消「${currentSession?.source_filename || '当前文档'}」的生成任务。`;
            $('status-text').textContent = '生成任务已取消';
            const cancelledTotal = Math.max(
                0,
                Math.round(Number(event.total) || summarizeParseResults(currentSession?.parse_results).total || 0),
            );
            const cancelledCompleted = Math.max(0, Math.round(Number(event.completed) || 0));
            const cancelledCount = Math.max(0, Math.round(Number(event.cancelled) || 0));
            const cancelledPercent = terminalProgressPercent(cancelledCompleted, cancelledTotal);
            setProgressReadoutMode(true, true);
            setProgressBarPercent(cancelledPercent);
            $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(cancelledPercent));
            $('progress-bar').parentElement?.setAttribute('aria-valuetext', `${cancelledPercent}% 可交付`);
            if ($('progress-percent')) $('progress-percent').textContent = String(cancelledPercent);
            if ($('progress-completed')) $('progress-completed').textContent = String(cancelledCompleted);
            if ($('progress-remaining')) $('progress-remaining').textContent = String(Math.max(cancelledTotal - cancelledCompleted - cancelledCount, 0));
            if ($('progress-cancelled')) $('progress-cancelled').textContent = String(cancelledCount);
            if ($('progress-stats')) $('progress-stats').textContent = `${cancelledCompleted} / ${cancelledTotal || cancelledCompleted} · 已取消 ${cancelledCount}`;
            setGenerationVisualState('stopped');
            showToast('任务已取消');
            break;

        case 'error': {
            const errorMessage = String(event?.msg || '生成服务返回了未说明的错误').trim()
                || '生成服务返回了未说明的错误';
            transientGenerationErrorMessage = `生成出错：${errorMessage}`;
            if (!logEntriesByKey.has('task:summary')) {
                addLogEntry({
                    level: 'error',
                    stage: 'complete',
                    kind: 'summary',
                    status: 'error',
                    key: 'task:summary',
                    title: '生成任务未能完成',
                    detail: errorMessage,
                    duration_ms: event.duration_ms,
                });
            }
            showToast(`错误: ${errorMessage}`);
            generationResult = 'error';
            resetGenerateState();
            setProgressIndeterminate(false);
            if (workflowStream) {
                workflowStream.close().catch(() => {});
                workflowStream = null;
            }
            clearSSEReconnectTimer();
            sseConnectionToken++;
            $('gen-title').textContent = '生成出错';
            $('generation-file-name').textContent = `「${currentSession?.source_filename || '当前文档'}」生成遇到问题；可查看任务详情后重试。`;
            $('status-text').textContent = `错误: ${errorMessage}`;
            setGenerationVisualState('error');
            syncGenerationRecoveryState(
                currentWorkspace,
                workspaceUserState(currentWorkspace, currentSession),
            );
            syncTransientGenerationErrorShell(transientGenerationErrorMessage);
            break;
        }

        case 'end':
            resetGenerateState();
            setProgressIndeterminate(false);
            if (workflowStream) {
                workflowStream.close().catch(() => {});
                workflowStream = null;
            }
            clearSSEReconnectTimer();
            sseConnectionToken++;
            // 如果未收到 done 或 error 事件，说明生成异常终止
            if (generationResult === null) {
                generationResult = 'error';
                transientGenerationErrorMessage = '生成任务意外停止。你可以重试，或返回配置页检查设置。';
                addLogEntry({
                    level: 'warn',
                    stage: 'complete',
                    kind: 'summary',
                    status: 'warning',
                    key: 'task:summary',
                    title: '生成任务意外停止',
                    detail: '未收到明确的完成、失败或取消状态，可重试任务并检查生成服务',
                });
                $('gen-title').textContent = '生成已停止';
                $('generation-file-name').textContent = `「${currentSession?.source_filename || '当前文档'}」生成意外停止；可重试或返回配置检查设置。`;
                $('status-text').textContent = '生成已停止，请检查日志或重新开始';
                setGenerationVisualState('stopped');
                syncGenerationRecoveryState(
                    currentWorkspace,
                    workspaceUserState(currentWorkspace, currentSession),
                );
                syncTransientGenerationErrorShell(transientGenerationErrorMessage);
            }
            break;

        case 'heartbeat':
            // 心跳证明连接已稳定跨过一个服务端等待周期。
            sseRetryCount = 0;
            break;
    }
}

// ============================================================================
// 日志渲染
// ============================================================================

const LOG_STAGE_LABELS = {
    prepare: '准备任务',
    parse: '识别文档',
    synthesize: '生成音频',
    package: '整理交付',
    archive: '保存记录',
    complete: '任务完成',
    system: '系统状态',
};

const LOG_STAGE_ORDER = ['prepare', 'parse', 'synthesize', 'package', 'archive', 'complete'];

function formatLogDuration(durationMs) {
    const value = Number(durationMs);
    if (!Number.isFinite(value) || value < 0) return '';
    if (value < 1000) return `${Math.round(value)} 毫秒`;
    if (value < 60000) return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)} 秒`;
    const minutes = Math.floor(value / 60000);
    const seconds = Math.round((value % 60000) / 1000);
    return `${minutes} 分 ${seconds} 秒`;
}

function normalizeLogEntry(rawEntry = {}) {
    const allowedLevels = ['success', 'error', 'warn', 'progress', 'info'];
    const allowedStatuses = ['running', 'success', 'warning', 'error', 'info'];
    const level = allowedLevels.includes(rawEntry.level) ? rawEntry.level : 'info';
    const fallbackStatus = {
        success: 'success',
        error: 'error',
        warn: 'warning',
        progress: 'running',
        info: 'info',
    }[level];
    const status = allowedStatuses.includes(rawEntry.status) ? rawEntry.status : fallbackStatus;
    const backendSeq = Number(rawEntry.seq);
    const hasBackendSeq = Number.isFinite(backendSeq) && backendSeq > 0;
    if (hasBackendSeq && logSeenSeq.has(backendSeq)) return null;
    if (hasBackendSeq) logSeenSeq.add(backendSeq);
    const localSeq = hasBackendSeq ? backendSeq : 1000000 + (++logLocalSeq);
    const stableKey = String(rawEntry.key || `event:${localSeq}`);
    return {
        ...rawEntry,
        seq: localSeq,
        key: stableKey,
        level,
        status,
        stage: String(rawEntry.stage || 'system'),
        kind: String(rawEntry.kind || 'notice'),
        title: String(rawEntry.title || rawEntry.msg || '任务记录'),
        detail: String(rawEntry.detail || (rawEntry.title ? rawEntry.msg || '' : '')),
        time: String(rawEntry.time || new Date().toLocaleTimeString('zh-CN', { hour12: false })),
        item: rawEntry.item && typeof rawEntry.item === 'object' ? rawEntry.item : null,
        work: rawEntry.work && typeof rawEntry.work === 'object' ? rawEntry.work : null,
        segments: rawEntry.segments && typeof rawEntry.segments === 'object' ? rawEntry.segments : null,
        progress: rawEntry.progress && typeof rawEntry.progress === 'object' ? rawEntry.progress : null,
    };
}

function logStatusLabel(entry) {
    if (entry.status === 'running') return '处理中';
    if (entry.status === 'success') return '已完成';
    if (entry.status === 'warning') return '需关注';
    if (entry.status === 'error') return '失败';
    return '已记录';
}

function createLogMeta(label, value, className = '') {
    if (value === undefined || value === null || value === '') return null;
    const meta = document.createElement('span');
    meta.className = `log-meta-item${className ? ` ${className}` : ''}`;
    const key = document.createElement('small');
    key.textContent = label;
    const content = document.createElement('strong');
    content.textContent = String(value);
    meta.append(key, content);
    return meta;
}

function createLogEntryElement(entry) {
    const article = document.createElement('article');
    article.className = `log-entry is-${entry.status}`;
    article.dataset.logKey = entry.key;
    article.dataset.logStatus = entry.status;
    article.dataset.logLevel = entry.level;
    article.dataset.logKind = entry.kind;
    article.setAttribute('aria-label', `${entry.title}，${logStatusLabel(entry)}`);

    const time = document.createElement('time');
    time.className = 'log-time';
    time.textContent = entry.time;
    if (entry.ts) time.dateTime = entry.ts;

    const node = document.createElement('span');
    node.className = 'log-node';
    node.setAttribute('aria-hidden', 'true');

    const content = document.createElement('div');
    content.className = 'log-content';
    const head = document.createElement('div');
    head.className = 'log-entry-head';
    const headingCopy = document.createElement('div');
    headingCopy.className = 'log-entry-heading';
    const stage = document.createElement('span');
    stage.className = 'log-stage-badge';
    stage.textContent = LOG_STAGE_LABELS[entry.stage] || '任务记录';
    const title = document.createElement('strong');
    title.className = 'log-entry-title';
    title.textContent = entry.title;
    headingCopy.append(stage, title);
    const status = document.createElement('span');
    status.className = 'log-status-badge';
    status.textContent = logStatusLabel(entry);
    head.append(headingCopy, status);
    content.appendChild(head);

    if (entry.detail) {
        const detail = document.createElement('p');
        detail.className = 'log-detail';
        detail.textContent = entry.detail;
        content.appendChild(detail);
    }

    const meta = document.createElement('div');
    meta.className = 'log-meta';
    const metaItems = [];
    if (entry.item) {
        metaItems.push(createLogMeta('题型', entry.item.doc_type));
        metaItems.push(createLogMeta('分类', entry.item.category));
        metaItems.push(createLogMeta('音色', entry.item.voice));
        metaItems.push(createLogMeta('文件', entry.item.filename, 'is-file'));
    }
    if (entry.progress) {
        metaItems.push(createLogMeta('进度', `${entry.progress.completed || 0}/${entry.progress.total || 0}`));
        if (Number(entry.progress.failed) > 0) {
            metaItems.push(createLogMeta('失败', entry.progress.failed, 'is-issue'));
        }
    }
    if (entry.work) {
        const workIndex = Number(entry.work.index);
        const workTotal = Number(entry.work.total);
        const workLabel = Number.isFinite(workIndex) && Number.isFinite(workTotal) && workTotal > 0
            ? `${workIndex}/${workTotal}`
            : entry.work.status || '';
        metaItems.push(createLogMeta('合并作品', workLabel));
        metaItems.push(createLogMeta('包含题目', entry.work.item_count));
        metaItems.push(createLogMeta('作品状态', entry.work.status));
    }
    if (entry.segments) {
        const sliced = entry.segments.sliced ?? entry.segments.completed ?? 0;
        const exported = entry.segments.exported ?? 0;
        metaItems.push(createLogMeta('切割题目', `${sliced}/${entry.segments.total || 0}`));
        if (entry.segments.exported !== undefined) {
            metaItems.push(createLogMeta('整理题目', `${exported}/${entry.segments.total || 0}`));
        }
    }
    const duration = formatLogDuration(entry.duration_ms);
    if (duration) metaItems.push(createLogMeta('耗时', duration));
    metaItems.filter(Boolean).forEach(item => meta.appendChild(item));
    if (meta.childElementCount > 0) content.appendChild(meta);

    if (entry.item?.text_preview) {
        const source = document.createElement('details');
        source.className = 'log-source-preview';
        const summary = document.createElement('summary');
        summary.textContent = '查看内容摘要';
        const sourceText = document.createElement('p');
        sourceText.textContent = entry.item.text_preview;
        source.append(summary, sourceText);
        content.appendChild(source);
    }

    article.append(time, node, content);
    return article;
}

function logEntryMatchesFilter(entry) {
    if (logFilter === 'running') return entry.status === 'running';
    if (logFilter === 'success') return entry.status === 'success';
    if (logFilter === 'issues') return entry.status === 'warning' || entry.status === 'error';
    return true;
}

function applyLogFilter() {
    logEntriesByKey.forEach(record => {
        record.element.hidden = !logEntryMatchesFilter(record.entry);
    });
    const visibleCount = [...logEntriesByKey.values()].filter(record => !record.element.hidden).length;
    const empty = $('progress-log').querySelector('.log-empty');
    if (visibleCount === 0 && logEntriesByKey.size > 0) {
        if (!empty) {
            const notice = document.createElement('div');
            notice.className = 'log-empty is-filtered';
            notice.textContent = '当前筛选条件下没有任务记录。';
            $('progress-log').appendChild(notice);
        }
    } else if (empty?.classList.contains('is-filtered')) {
        empty.remove();
    }
}

function updateLogStageRail(entry) {
    if (entry.kind !== 'stage' && entry.kind !== 'summary') return;
    if (generationWorkflowOwnsRuntimeView() && entry.status === 'running') return;
    let stageIndex = LOG_STAGE_ORDER.indexOf(entry.stage);
    if (entry.stage === 'complete') {
        // 终态只收束已经走到的处理阶段，并由独立的“完成”节点承载
        // 整体成功/部分完成/失败，避免把未执行阶段误画成绿色。
        const reachedStageIndex = logStageIndex;
        LOG_STAGE_ORDER.forEach((stage, index) => {
            if (stage === 'complete' || index > reachedStageIndex) return;
            const existing = logStageStates.get(stage);
            if (!existing || existing === 'running' || existing === 'info') {
                const isCurrentStage = index === reachedStageIndex;
                if (isCurrentStage && entry.status === 'error') logStageStates.set(stage, 'error');
                else if (isCurrentStage && entry.status === 'warning') logStageStates.set(stage, 'warning');
                else logStageStates.set(stage, 'success');
            }
        });
        logStageStates.set('complete', entry.status);
        logStageIndex = stageIndex;
    }
    if (stageIndex < 0) return;
    if (entry.stage !== 'complete') {
        if (stageIndex < logStageIndex && entry.status === 'running') return;
        logStageIndex = Math.max(logStageIndex, stageIndex);
        logStageStates.set(entry.stage, entry.status);
        LOG_STAGE_ORDER.forEach((stage, index) => {
            if (index < logStageIndex && !logStageStates.has(stage)) {
                logStageStates.set(stage, 'success');
            }
        });
    }
    renderLogStageRail();
}

function renderLogStageRail() {
    $$('#log-stage-rail [data-log-stage]').forEach((stageEl, index) => {
        const state = logStageStates.get(stageEl.dataset.logStage);
        stageEl.classList.remove('is-active', 'is-complete', 'is-error', 'is-warning');
        if (state === 'success') stageEl.classList.add('is-complete');
        else if (state === 'warning') stageEl.classList.add('is-warning');
        else if (state === 'error') stageEl.classList.add('is-error');
        else if (state === 'running' || (index === logStageIndex && !state)) stageEl.classList.add('is-active');
        const statusText = state === 'success'
            ? '已完成'
            : state === 'warning'
                ? '需关注'
                : state === 'error'
                    ? '失败'
                    : state === 'running'
                        ? '进行中'
                        : '未开始';
        const stageLabel = LOG_STAGE_LABELS[stageEl.dataset.logStage] || stageEl.textContent.trim();
        stageEl.setAttribute('aria-label', `${stageLabel}：${statusText}`);
        if (state === 'running') stageEl.setAttribute('aria-current', 'step');
        else stageEl.removeAttribute('aria-current');
    });
}

function trimLogTimeline() {
    while (logEntriesByKey.size > LOG_DOM_LIMIT) {
        let removableKey = null;
        for (const [key, record] of logEntriesByKey) {
            if (record.entry.kind === 'item' && record.entry.status === 'success') {
                removableKey = key;
                break;
            }
        }
        removableKey ||= logEntriesByKey.keys().next().value;
        const record = logEntriesByKey.get(removableKey);
        record?.element.remove();
        logEntriesByKey.delete(removableKey);
    }
}

function updateLogTimelineHeader(lastEntry = null) {
    logEntryCount = logEntriesByKey.size;
    const issueCount = [...logEntriesByKey.values()].filter(({ entry }) => entry.status === 'warning' || entry.status === 'error').length;
    $('log-count').textContent = logEntryCount >= LOG_DOM_LIMIT
        ? `最近 ${LOG_DOM_LIMIT} 个节点`
        : `${logEntryCount} 个节点`;
    $('log-issue-count').textContent = String(issueCount);
    if (lastStats?.total) {
        const total = integerProgressCount(lastStats.total);
        const processed = integerProgressCount(
            lastStats.processed ?? ((lastStats.completed || 0) + (lastStats.failed || 0)),
            total,
        );
        const pending = Math.max(0, total - processed);
        const eta = formatLogDuration(lastStats.eta_ms);
        $('log-summary').textContent = `已处理 ${processed} / ${total} · 剩余 ${pending}${issueCount ? ` · ${issueCount} 条异常记录` : ''}${eta ? ` · 预计还需 ${eta}` : ''}`;
    } else if (lastEntry) {
        $('log-summary').textContent = lastEntry.title;
    }
}

function updateLogNewRecordsButton() {
    const button = $('log-new-records-btn');
    button.hidden = logAutoFollow || logUnseenCount === 0;
    button.textContent = logUnseenCount > 0 ? `有 ${logUnseenCount} 条新动态 · 回到最新` : '回到最新';
}

function setLogDetailsExpanded(expanded) {
    const panel = $('log-panel');
    if (!panel) return;
    const layout = panel.closest('.generation-layout');
    const button = $('log-toggle-btn');
    panel.classList.toggle('is-collapsed', !expanded);
    layout?.classList.toggle('is-log-collapsed', !expanded);
    button?.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    if (button) button.textContent = expanded ? '收起详情' : '展开详情';
    if (expanded) setLogAutoFollow(true, { scrollToEnd: true });
}

function setLogAutoFollow(enabled, { scrollToEnd = false } = {}) {
    logAutoFollow = Boolean(enabled);
    const button = $('log-follow-btn');
    button.classList.toggle('is-active', logAutoFollow);
    button.setAttribute('aria-pressed', logAutoFollow ? 'true' : 'false');
    button.textContent = logAutoFollow ? '跟随最新' : '已暂停跟随';
    if (logAutoFollow) logUnseenCount = 0;
    updateLogNewRecordsButton();
    if (scrollToEnd) {
        requestAnimationFrame(() => {
            const body = $('progress-log');
            body.scrollTop = body.scrollHeight;
        });
    }
}

function setLogFilter(filter) {
    logFilter = ['all', 'running', 'success', 'issues'].includes(filter) ? filter : 'all';
    $$('[data-log-filter]').forEach(button => {
        const active = button.dataset.logFilter === logFilter;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    applyLogFilter();
}

function upsertLogEntry(rawEntry) {
    const entry = normalizeLogEntry(rawEntry);
    if (!entry) return { changed: false, isNew: false, entry: null };
    const existing = logEntriesByKey.get(entry.key);
    if (existing && Number(existing.entry.seq) >= Number(entry.seq)) {
        return { changed: false, isNew: false, entry: existing.entry };
    }
    const existingDetails = existing?.element?.querySelector('.log-source-preview');
    const preserveDetailsOpen = Boolean(existingDetails?.open);
    const restoreDetailsFocus = Boolean(
        existing?.element && existing.element.contains(document.activeElement)
    );
    const element = createLogEntryElement(entry);
    const nextDetails = element.querySelector('.log-source-preview');
    if (nextDetails && preserveDetailsOpen) nextDetails.open = true;
    existing?.element?.remove();
    $('progress-log').querySelector('.log-empty')?.remove();
    $('progress-log').appendChild(element);
    if (existing) logEntriesByKey.delete(entry.key);
    logEntriesByKey.set(entry.key, { entry, element });
    element.hidden = !logEntryMatchesFilter(entry);
    updateLogStageRail(entry);
    if (entry.status === 'warning' || entry.status === 'error') {
        $('log-live-announcer').textContent = `${entry.title}，${logStatusLabel(entry)}`;
    }
    if (nextDetails && restoreDetailsFocus) {
        requestAnimationFrame(() => nextDetails.querySelector('summary')?.focus());
    }
    return { changed: true, isNew: !existing, entry };
}

function finalizeLogUpdate(results) {
    const changed = results.filter(result => result.changed);
    if (changed.length === 0) return;
    trimLogTimeline();
    applyLogFilter();
    const lastEntry = changed[changed.length - 1].entry;
    updateLogTimelineHeader(lastEntry);
    if (logAutoFollow) {
        requestAnimationFrame(() => {
            const body = $('progress-log');
            body.scrollTop = body.scrollHeight;
        });
    } else {
        const visibleChanges = changed.filter(result => logEntryMatchesFilter(result.entry));
        if (visibleChanges.length > 0) {
            logUnseenCount += visibleChanges.length;
            updateLogNewRecordsButton();
        }
    }
}

function addLogEntry(entry) {
    finalizeLogUpdate([upsertLogEntry(entry)]);
}

function addLogEntries(entries) {
    const sorted = [...entries].sort((a, b) => (Number(a?.seq) || 0) - (Number(b?.seq) || 0));
    finalizeLogUpdate(sorted.map(entry => upsertLogEntry(entry)));
}

function resetLogTimeline(emptyText = '任务开始后，这里会按阶段展示详细处理记录。') {
    logEntriesByKey.clear();
    logSeenSeq.clear();
    logEntryCount = 0;
    logFilter = 'all';
    logAutoFollow = true;
    logUnseenCount = 0;
    logLocalSeq = 0;
    logStageIndex = -1;
    logStageStates.clear();
    $('progress-log').replaceChildren();
    const empty = document.createElement('div');
    empty.className = 'log-empty';
    empty.textContent = emptyText;
    $('progress-log').appendChild(empty);
    $('log-count').textContent = '0 个节点';
    $('log-issue-count').textContent = '0';
    $('log-summary').textContent = '阶段记录和异常项实时更新';
    $$('#log-stage-rail [data-log-stage]').forEach(stage => {
        stage.classList.remove('is-active', 'is-complete', 'is-warning', 'is-error');
        const stageLabel = LOG_STAGE_LABELS[stage.dataset.logStage] || stage.textContent.trim();
        stage.setAttribute('aria-label', `${stageLabel}：未开始`);
        stage.removeAttribute('aria-current');
    });
    setLogFilter('all');
    setLogAutoFollow(true);
    // 任务时间线是生成页的默认详情视图；每次新任务重置后都保持展开，
    // 这样浏览器启动、提交和失败原因不会被折叠在进度卡下面。
    setLogDetailsExpanded(true);
}

// ============================================================================
// 进度 & 统计
// ============================================================================

function integerProgressCount(value, total = Number.POSITIVE_INFINITY) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 0;
    const count = Math.max(0, Math.floor(number + 0.5));
    return Number.isFinite(total) ? Math.min(count, Math.max(0, Math.floor(Number(total) || 0))) : count;
}

function visualProgressPercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 0;
    return Math.min(Math.max(number, 0), 99);
}

function terminalProgressPercent(deliverable, total) {
    const safeTotal = Math.max(0, Math.round(Number(total) || 0));
    const safeDeliverable = Math.max(0, Math.round(Number(deliverable) || 0));
    if (safeTotal === 0) return safeDeliverable > 0 ? 100 : 0;
    return Math.min(100, Math.round((Math.min(safeDeliverable, safeTotal) / safeTotal) * 100));
}

function setProgressReadoutMode(terminal = false, hasIssues = false) {
    const title = $('progress-panel-title');
    const copyLabel = document.querySelector?.('#page-3 .generation-v2-copy-label');
    if (title) title.textContent = terminal ? '可交付进度' : '处理进度';
    if (copyLabel) copyLabel.textContent = terminal && hasIssues ? '结果状态' : '当前任务';
}

function updateProgress(event) {
    if (generationWorkflowOwnsRuntimeView()) {
        renderGenerationViewState(currentWorkspace, workspaceUserState(currentWorkspace, currentSession));
        return;
    }
    setProgressIndeterminate(false);
    const total = integerProgressCount(event.total);
    const completed = integerProgressCount(event.completed, total);
    const failed = integerProgressCount(event.failed, total);
    const cancelled = integerProgressCount(event.cancelled, total);
    const processed = integerProgressCount(
        event.processed ?? (completed + failed + cancelled),
        total,
    );
    const pct = total > 0 ? Math.round((processed / total) * 100) : 0;
    const phase = String(event.phase || '');
    const isBatchSubmit = phase === 'batch-submit';
    const isBatchDownload = phase === 'batch-download';
    const isBatchExport = phase === 'batch-export';
    const isCompositeSubmit = phase === 'composite-submit';
    const isCompositeDownload = phase === 'composite-download';
    const isCompositeCut = phase === 'composite-cut';
    const isCompositeExport = phase === 'composite-export';
    const isCompositeError = phase === 'composite-error';
    const isPackage = phase === 'package';
    const isArchive = phase === 'archive';
    const isBatchPhase = isBatchSubmit || isBatchDownload || isBatchExport;
    const isPostProcessPhase = isPackage || isArchive;
    const mode = normalizeGenerationMode(event.generation_mode || lastGenerationConfig?.generation_mode);
    const work = event.work && typeof event.work === 'object' ? event.work : null;
    const segments = event.segments && typeof event.segments === 'object' ? event.segments : null;
    updateGenerationModeUI(mode);
    // stats 只是阶段快照，不代表任务终态；即使 completed 已经等于 total，
    // 后面仍可能在打包 ZIP、保存历史记录。只有 done 事件才允许进度条到 100%，
    // 其余状态统一保留尾部空间，避免用户看到 100% 后继续等待。
    const visualPct = visualProgressPercent(pct);
    const eta = formatLogDuration(event.eta_ms);
    setProgressBarPercent(visualPct);
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(visualPct));
    $('progress-bar').parentElement?.setAttribute('aria-valuetext', `${visualPct}% 处理中`);
    setProgressReadoutMode(false);
    const workTotal = integerProgressCount(work?.total ?? 0);
    const workCompleted = integerProgressCount(work?.completed ?? 0, workTotal || Number.POSITIVE_INFINITY);
    const workSubmitted = integerProgressCount(work?.submitted ?? 0, workTotal || Number.POSITIVE_INFINITY);
    const workDownloaded = integerProgressCount(work?.downloaded ?? 0, workTotal || Number.POSITIVE_INFINITY);
    const segmentTotal = integerProgressCount(segments?.total ?? 0);
    const segmentSliced = integerProgressCount(
        segments?.sliced ?? segments?.completed ?? 0,
        segmentTotal || Number.POSITIVE_INFINITY,
    );
    const segmentExported = integerProgressCount(
        segments?.exported ?? (isCompositeExport ? completed : 0),
        segmentTotal || Number.POSITIVE_INFINITY,
    );
    const compositeWorkCopy = workTotal > 0
        ? `作品 ${workCompleted}/${workTotal} · 已提交 ${workSubmitted} · 已下载 ${workDownloaded}`
        : '';
    const compositeSegmentCopy = segmentTotal > 0
        ? `题目切割 ${segmentSliced}/${segmentTotal}`
        : '';
    const compositeExportCopy = segmentTotal > 0
        ? `题目整理 ${segmentExported}/${segmentTotal}`
        : '';
    let completedLabel = '已完成';
    if (isCompositeSubmit) completedLabel = '已提交作品';
    else if (isCompositeDownload) completedLabel = '已下载作品';
    else if (isCompositeCut) completedLabel = '已切割题目';
    else if (isCompositeExport) completedLabel = '已整理题目';
    else if (isBatchSubmit) completedLabel = '已提交';
    else if (isBatchDownload) completedLabel = '已下载';
    else if (isBatchExport) completedLabel = '已整理';
    else if (isPackage) completedLabel = '正在整理';
    else if (isArchive) completedLabel = '正在归档';
    $('progress-completed-label').textContent = completedLabel;
    let phaseCopy = `${completed} / ${total}`;
    if (isCompositeSubmit) phaseCopy = `合并作品提交中 · ${compositeWorkCopy}`;
    else if (isCompositeDownload) phaseCopy = `合并音频下载中 · ${compositeWorkCopy}`;
    else if (isCompositeCut) phaseCopy = `按停顿安全切割中 · ${compositeSegmentCopy || compositeWorkCopy}`;
    else if (isCompositeExport) phaseCopy = `独立音频整理中 · ${compositeExportCopy || compositeWorkCopy}`;
    else if (isCompositeError) phaseCopy = `合并作品出现异常 · ${compositeWorkCopy}`;
    else if (isBatchSubmit) phaseCopy = `已提交 ${processed} / ${total} · 等待下载`;
    else if (isBatchDownload) phaseCopy = `已下载 ${processed} / ${total} · 等待整理`;
    else if (isBatchExport) phaseCopy = `已整理 ${processed} / ${total} · 正在输出`;
    else if (isPackage) phaseCopy = `正在打包交付文件 · 已生成 ${completed} / ${total}`;
    else if (isArchive) phaseCopy = `正在保存历史记录 · 已生成 ${completed} / ${total}`;
    $('progress-stats').textContent = phaseCopy
        + (failed > 0 ? `  ·  失败 ${failed}` : '')
        + (cancelled > 0 ? `  ·  已取消 ${cancelled}` : '')
        + (eta ? `  ·  预计 ${eta}` : '');
    $('progress-percent').textContent = String(Math.round(visualPct));
    const displayedCompleted = isCompositeCut
        ? String(segmentTotal > 0 ? segmentSliced : processed)
        : isCompositeExport
            ? String(segmentTotal > 0 ? segmentExported : processed)
            : isBatchPhase || isPostProcessPhase
                ? String(processed)
                : String(completed);
    $('progress-completed').textContent = displayedCompleted;
    $('progress-remaining').textContent = String(Math.max(total - processed, 0));
    $('progress-failed').textContent = String(failed);
    if ($('progress-cancelled')) $('progress-cancelled').textContent = String(cancelled);
    updateLogTimelineHeader();
}

function updateStats(event) {
    const container = $('type-stats');
    container.innerHTML = '';

    if (event.by_type) {
        for (const [type, counts] of Object.entries(event.by_type)) {
            const color = (currentConfig && currentConfig.type_colors && currentConfig.type_colors[type]) || '#a8a29e';
            const pill = document.createElement('span');
            pill.className = 'type-stat-pill';

            const dot = document.createElement('span');
            dot.className = 'type-stat-dot';
            dot.style.background = color;

            const label = document.createElement('span');
            label.textContent = type;

            const count = document.createElement('span');
            count.className = 'type-stat-count';
            count.textContent = `${counts.done}/${counts.total}`;

            pill.appendChild(dot);
            pill.appendChild(label);
            pill.appendChild(count);
            container.appendChild(pill);
        }
    }

    // 底部状态栏
    const statsBar = $('stats-bar');
    statsBar.innerHTML = '';

    if (event.by_type) {
        for (const [type, counts] of Object.entries(event.by_type)) {
            const color = (currentConfig && currentConfig.type_colors && currentConfig.type_colors[type]) || '#a8a29e';
            const pill = document.createElement('span');
            pill.className = 'stat-pill';

            const dot = document.createElement('span');
            dot.className = 'stat-dot';
            dot.style.background = color;

            const label = document.createElement('span');
            label.textContent = type + ' ';

            const count = document.createElement('span');
            count.className = 'stat-count';
            count.textContent = `${counts.done}/${counts.total}`;

            label.appendChild(count);
            pill.appendChild(dot);
            pill.appendChild(label);
            statsBar.appendChild(pill);
        }
    }

    const totalPill = document.createElement('span');
    totalPill.className = 'stat-pill';
    const totalLabel = document.createElement('span');
    totalLabel.textContent = '成功 ';
    const totalCount = document.createElement('span');
    totalCount.className = 'stat-count';
    totalCount.textContent = `${event.completed}/${event.total}`;
    totalLabel.appendChild(totalCount);
    totalPill.appendChild(totalLabel);
    statsBar.appendChild(totalPill);

    if (event.failed > 0) {
        const failPill = document.createElement('span');
        failPill.className = 'stat-pill error-pill';
        const failLabel = document.createElement('span');
        failLabel.textContent = '失败 ';
        const failCount = document.createElement('span');
        failCount.className = 'stat-count';
        failCount.textContent = String(event.failed);
        failLabel.appendChild(failCount);
        failPill.appendChild(failLabel);
        statsBar.appendChild(failPill);
    }
    if (event.cancelled > 0) {
        const cancelledPill = document.createElement('span');
        cancelledPill.className = 'stat-pill cancelled-pill';
        const cancelledLabel = document.createElement('span');
        cancelledLabel.textContent = '已取消 ';
        const cancelledCount = document.createElement('span');
        cancelledCount.className = 'stat-count';
        cancelledCount.textContent = String(event.cancelled);
        cancelledLabel.appendChild(cancelledCount);
        cancelledPill.appendChild(cancelledLabel);
        statsBar.appendChild(cancelledPill);
    }
}

// ============================================================================
// 文件列表更新
// ============================================================================

function updateFileList(event) {
    if (event.file_list && event.file_list.length > 0) {
        generatedFiles = event.file_list;
    }
}

// ============================================================================
// Step 4: 完成
// ============================================================================

function handleDone(event) {
    resetGenerateState();
    setProgressIndeterminate(false);
    generationResult = 'done';
    transientGenerationErrorMessage = '';
    hideGenerationRecovery();

    clearSSEReconnectTimer();
    sseConnectionToken++;
    if (workflowStream) {
        workflowStream.close().catch(() => {});
        workflowStream = null;
    }

    // 合并最后的统计数据（done 事件本身不携带 completed/failed）
    const doneData = {
        ...event,
        completed: lastStats ? lastStats.completed : (event.completed || 0),
        failed: lastStats ? lastStats.failed : (event.failed || 0),
        cancelled: Math.max(Number(lastStats?.cancelled) || 0, Number(event.cancelled) || 0),
        skipped: Math.max(Number(lastStats?.skipped) || 0, Number(event.skipped) || 0),
        total: lastStats ? lastStats.total : (event.total || 0),
        failed_items: lastStats?.failed_items || event.failed_items || [],
    };
    latestCurrentResultEvent = {
        ...doneData,
        workflow_id: doneData.workflow_id || currentSession?.session_id || null,
    };

    // 更新生成页面状态。终态进度代表“可交付结果”而不是“处理过的
    // 条目”；失败和取消也算 processed，但不能把交付进度伪装成 100%。
    const totalCount = Math.max(0, Math.round(Number(doneData.total) || 0));
    const completedCount = Math.max(0, Math.min(totalCount || Number.MAX_SAFE_INTEGER, Math.round(Number(doneData.completed) || 0)));
    const failedCount = Math.max(0, Math.min(totalCount || Number.MAX_SAFE_INTEGER, Math.round(Number(doneData.failed) || 0)));
    const cancelledCount = Math.max(0, Math.min(totalCount || Number.MAX_SAFE_INTEGER, Math.round(Number(doneData.cancelled) || 0)));
    const skippedCount = Math.max(0, Math.min(totalCount || Number.MAX_SAFE_INTEGER, Math.round(Number(doneData.skipped) || 0)));
    const unresolved = failedCount + cancelledCount;
    const allFailed = totalCount > 0 && completedCount === 0 && unresolved + skippedCount >= totalCount;
    $('gen-title').textContent = allFailed
        ? '本次生成未完成'
        : (unresolved > 0 ? '音频已部分生成' : '生成完成');
    setGenerationVisualState(allFailed ? 'error' : (unresolved > 0 ? 'warning' : 'done'));
    const terminalPercent = terminalProgressPercent(completedCount, totalCount);
    setProgressReadoutMode(true, unresolved > 0 || skippedCount > 0);
    setProgressBarPercent(terminalPercent);
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(terminalPercent));
    $('progress-bar').parentElement?.setAttribute('aria-valuetext', `${terminalPercent}% 可交付`);
    $('progress-percent').textContent = String(terminalPercent);
    $('progress-completed-label').textContent = '已完成';
    $('progress-completed').textContent = String(completedCount);
    $('progress-remaining').textContent = String(Math.max(totalCount - completedCount - failedCount - cancelledCount - skippedCount, 0));
    $('progress-failed').textContent = String(failedCount);
    if ($('progress-cancelled')) $('progress-cancelled').textContent = String(cancelledCount);
    if ($('progress-skipped')) $('progress-skipped').textContent = String(skippedCount);
    $('progress-stats').textContent = `${completedCount} / ${totalCount || completedCount}`
        + (failedCount > 0 ? `  ·  失败 ${failedCount}` : '')
        + (cancelledCount > 0 ? `  ·  已取消 ${cancelledCount}` : '')
        + (skippedCount > 0 ? `  ·  已跳过 ${skippedCount}` : '');

    if (unresolved > 0 || skippedCount > 0) {
        const firstFailure = Array.isArray(doneData.failed_items)
            ? doneData.failed_items.find(item => String(item?.error || item?.user_message || '').trim())
            : null;
        const firstReason = String(firstFailure?.error || firstFailure?.user_message || '')
            .trim()
            .replace(/\s+/g, ' ')
            .slice(0, 240);
        const recoveryMessage = allFailed
            ? (firstReason
                ? `没有生成可交付音频。首条失败原因：${firstReason}`
                : '没有生成可交付音频，请展开任务时间线查看失败原因后重试。')
            : `本次有 ${unresolved + skippedCount} 条内容未进入交付范围，可查看任务时间线后重试。`;
        // This is already a terminal result. The actionable retry control is
        // the result-page failed-item action; the generation recovery panel
        // must not expose a button that routes through the non-terminal retry
        // flow and then silently falls back to configuration.
        showGenerationRecovery(recoveryMessage, {
            title: allFailed ? '生成失败' : '部分完成',
            retryVisible: false,
        });
    } else {
        hideGenerationRecovery();
    }

    // 构建结果页面
    buildResultPage(doneData);
    void refreshHistoryRecords({ showLoading: false });

    // 短暂停留展示完成态，再进入交付中心。
    const completedAttemptId = generationAttemptId;
    const completedSessionId = currentSession?.session_id;
    resultNavigationTimer = setTimeout(() => {
        resultNavigationTimer = null;
        // 仅在当前仍在生成页（step 3）时才跳转，避免 restart 后误跳
        if (currentView === 'workflow' && currentStep === 3 && completedAttemptId === generationAttemptId && currentSession?.session_id === completedSessionId) {
            goToStep(4);
        }
    }, 950);

    showToast(unresolved > 0 ? `任务结束，${doneData.failed || 0} 条失败、${doneData.cancelled || 0} 条已取消` : '处理完成');
}

function prepareAudioFilters(files) {
    const searchInput = $('audio-search-input');
    const typeFilter = $('audio-type-filter');
    const toolbar = $('audio-toolbar');
    const empty = $('audio-filter-empty');
    if (searchInput) searchInput.value = '';
    if (empty) empty.hidden = true;
    if (toolbar) toolbar.hidden = files.length < 5;
    if (!typeFilter) return;

    typeFilter.replaceChildren();
    const allOption = document.createElement('option');
    allOption.value = '';
    allOption.textContent = '全部题型';
    typeFilter.appendChild(allOption);
    [...new Set(files.map(file => file?.doc_type).filter(Boolean))]
        .sort((a, b) => String(a).localeCompare(String(b), 'zh-CN'))
        .forEach(type => {
            const option = document.createElement('option');
            option.value = type;
            option.textContent = type;
            typeFilter.appendChild(option);
        });
    window.WordTTSUI?.syncSelect(typeFilter);
}

function filterAudioItems() {
    const audioList = $('audio-list');
    if (!audioList) return;
    const query = String($('audio-search-input')?.value || '').trim().toLocaleLowerCase('zh-CN');
    const selectedType = $('audio-type-filter')?.value || '';
    const items = [...audioList.querySelectorAll('.audio-item')];
    let visibleCount = 0;
    items.forEach(item => {
        const matchesQuery = !query || (item.dataset.searchText || '').includes(query);
        const matchesType = !selectedType || item.dataset.docType === selectedType;
        const visible = matchesQuery && matchesType;
        item.hidden = !visible;
        if (visible) visibleCount++;
        else if (item._audioElement && !item._audioElement.paused) item._audioElement.pause();
    });
    const count = $('audio-count');
    if (count) count.textContent = query || selectedType
        ? `${visibleCount} / ${items.length} 个文件`
        : `${items.length} 个文件`;
    const empty = $('audio-filter-empty');
    if (empty) empty.hidden = visibleCount > 0 || items.length === 0;
}

function scheduleAudioFilter() {
    if (audioFilterFrame !== null) return;
    const schedule = window.requestAnimationFrame
        ? callback => window.requestAnimationFrame(callback)
        : callback => window.setTimeout(callback, 0);
    audioFilterFrame = schedule(() => {
        audioFilterFrame = null;
        filterAudioItems();
    });
}

function resultVoiceKeysForFile(file) {
    const values = (Array.isArray(file?.voice_keys)
        ? [...file.voice_keys]
        : (file?.voice_keys ? [file.voice_keys] : []))
        .concat(Array.isArray(file?.metadata?.voice_keys)
            ? file.metadata.voice_keys
            : (file?.metadata?.voice_keys ? [file.metadata.voice_keys] : []))
        .concat(file?.voice_key || [])
        .filter(value => String(value ?? '').trim());

    // 兼容早期历史记录可能保存的单个 voice 字段；只接受能在当前目录
    // 精确匹配到 key 或名称的值，避免把“女声/男声”等展示文本误当成 key。
    if (!values.length && file?.voice) {
        const legacyValue = String(file.voice).trim();
        const normalizedLegacyKey = canonicalVoiceKey(legacyValue);
        const normalizedLegacyName = legacyValue.toLocaleLowerCase('zh-CN');
        const legacyVoice = voiceCatalog.find(voice => (
            normalizeVoiceKey(voice.key) === normalizedLegacyKey
            || String(voice.name || '').trim().toLocaleLowerCase('zh-CN') === normalizedLegacyName
        ));
        if (legacyVoice) values.push(legacyVoice.key);
        else if (normalizedLegacyKey && normalizedLegacyKey !== legacyValue) values.push(normalizedLegacyKey);
    }

    const canonicalize = value => {
        const normalized = canonicalVoiceKey(value);
        if (!normalized) return '';
        const normalizedName = String(value ?? '').trim().toLocaleLowerCase('zh-CN');
        const catalogVoice = voiceCatalog.find(voice => (
            voice.key === normalized
            || String(voice.name || '').trim().toLocaleLowerCase('zh-CN') === normalizedName
        ));
        return catalogVoice?.key || normalized;
    };
    return [...new Set(values.map(canonicalize).filter(Boolean))];
}

function createResultVoiceStrip(file) {
    const strip = document.createElement('div');
    strip.className = 'audio-voice-strip';

    const label = document.createElement('span');
    label.className = 'audio-voice-caption';
    label.textContent = '音色';
    strip.appendChild(label);

    const voiceKeys = resultVoiceKeysForFile(file);
    label.textContent = voiceKeys.length > 1 ? `音色 · ${voiceKeys.length} 种` : '音色';
    label.title = voiceKeys.length > 1 ? `本段音频使用 ${voiceKeys.length} 种音色` : '本段音频使用的音色';
    if (!voiceKeys.length) {
        const empty = document.createElement('span');
        empty.className = 'audio-voice-empty';
        empty.textContent = '历史文件未记录音色信息';
        strip.appendChild(empty);
        return strip;
    }

    voiceKeys.forEach(key => {
        const voice = getResultVoiceEntry(key);
        const chip = document.createElement('span');
        chip.className = 'audio-voice-chip';

        const avatarButton = document.createElement('button');
        avatarButton.type = 'button';
        avatarButton.className = 'audio-voice-avatar-button';
        avatarButton.dataset.audioName = `音色 ${voice.name}`;
        avatarButton.setAttribute('aria-label', `试听音色 ${voice.name}`);
        const hasSample = Boolean(voice.audio_url || voice.fallback_audio_url);
        avatarButton.title = hasSample ? `试听 ${voice.name}` : `${voice.name}暂无示例音频`;
        avatarButton.disabled = !hasSample;

        const avatar = document.createElement('span');
        avatar.className = 'voice-avatar audio-result-avatar';
        renderVoiceAvatar(avatar, voice, false, true);
        avatarButton.appendChild(avatar);

        const playState = document.createElement('span');
        playState.className = 'audio-voice-play-state';
        playState.setAttribute('aria-hidden', 'true');
        playState.innerHTML = '<svg class="icon-play" width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg><svg class="icon-pause" width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style="display:none"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
        avatarButton.appendChild(playState);
        avatarButton.addEventListener('click', event => {
            event.stopPropagation();
            playVoiceSample(voice, avatarButton);
        });

        const name = document.createElement('span');
        name.className = 'audio-voice-name';
        name.textContent = voice.name;
        name.title = voice.name;

        chip.append(avatarButton, name);
        strip.appendChild(chip);
    });
    return strip;
}

async function refreshResultVoiceAssets(files) {
    const resultFiles = Array.isArray(files) ? files : [];
    const voiceKeys = [...new Set(resultFiles.flatMap(resultVoiceKeysForFile))];
    if (!voiceKeys.length) return;

    const resultContext = activeResultContext;
    await queueVoiceAssetCache(voiceKeys);
    if (!resultContext || activeResultContext !== resultContext) return;

    // 只替换音色条，不重建 Audio、波形和原文折叠状态，避免缓存完成后
    // 造成结果页闪烁或打断用户正在试听的音频。
    document.querySelectorAll('#audio-list .audio-item').forEach(item => {
        const strip = item.querySelector('.audio-voice-strip');
        if (!strip || !item._resultFile) return;
        strip.replaceWith(createResultVoiceStrip(item._resultFile));
    });
}

function rendererReadableArtifactStream(transport) {
    if (transport && typeof transport.getReader === 'function') return transport;
    if (!transport || typeof transport.onData !== 'function') {
        throw new Error('Artifact 流式读取通道不可用');
    }

    let stream = null;
    let closed = false;
    let pendingAck = false;
    let ackInFlight = false;
    let ackQueued = false;
    const metadata = {};
    let removeData = () => {};
    let removeMetadata = () => {};
    let removeEnd = () => {};
    let removeError = () => {};

    const closeTransport = () => {
        removeData();
        removeMetadata();
        removeEnd();
        removeError();
        removeData = removeMetadata = removeEnd = removeError = () => {};
        return Promise.resolve(transport.close?.()).catch(() => {});
    };
    const fail = (controller, error) => {
        if (closed) return;
        closed = true;
        void closeTransport();
        controller.error(error instanceof Error ? error : new Error(String(error || 'workflow artifact stream failed')));
    };
    const requestAck = (controller) => {
        if (closed || !pendingAck || typeof transport.ack !== 'function') return;
        if (ackInFlight) {
            ackQueued = true;
            return;
        }
        pendingAck = false;
        ackInFlight = true;
        Promise.resolve(transport.ack()).catch((error) => fail(controller, error)).finally(() => {
            ackInFlight = false;
            if (ackQueued) {
                ackQueued = false;
                requestAck(controller);
            }
        });
    };

    stream = new ReadableStream({
        start(controller) {
            removeMetadata = transport.onMetadata?.((value) => {
                Object.assign(metadata, value || {});
                if (stream) stream.metadata = metadata;
            }) || (() => {});
            removeData = transport.onData((value) => {
                if (closed) return;
                try {
                    const bytes = value instanceof Uint8Array ? value : new Uint8Array(value || []);
                    pendingAck = true;
                    controller.enqueue(bytes);
                } catch (error) {
                    fail(controller, error);
                }
            }) || (() => {});
            removeEnd = transport.onEnd?.(() => {
                if (closed) return;
                closed = true;
                void closeTransport();
                controller.close();
            }) || (() => {});
            removeError = transport.onError?.((value) => {
                const error = value instanceof Error
                    ? value
                    : Object.assign(new Error(value?.message || 'workflow artifact stream failed'), value || {});
                fail(controller, error);
            }) || (() => {});
        },
        pull(controller) {
            requestAck(controller);
        },
        cancel() {
            if (closed) return;
            closed = true;
            void closeTransport();
        },
    });
    stream.metadata = metadata;
    return stream;
}

async function openRendererArtifactStream(artifactId) {
    if (!workflowApi || !artifactId) throw new Error('Artifact 标识缺失');
    const transport = await workflowApi.openArtifact(artifactId);
    return rendererReadableArtifactStream(transport);
}

async function readArtifactBytes(artifactId, maxBytes = MAX_BUFFERED_ARTIFACT_BYTES) {
    if (!workflowApi || !artifactId) throw new Error('Artifact 标识缺失');
    const byteLimit = Number.isSafeInteger(maxBytes) && maxBytes > 0
        ? maxBytes
        : MAX_BUFFERED_ARTIFACT_BYTES;
    const stream = await openRendererArtifactStream(artifactId);
    const reader = stream.getReader();
    const chunks = [];
    let total = 0;
    try {
        while (true) {
            const part = await reader.read();
            if (part.done) break;
            const chunk = part.value instanceof Uint8Array ? part.value : new Uint8Array(part.value || []);
            if (total + chunk.byteLength > byteLimit) {
                const error = new Error(`Artifact 超过浏览器有界读取上限（${Math.round(byteLimit / 1024 / 1024)} MiB）`);
                error.code = 'ARTIFACT_TOO_LARGE_FOR_BUFFER';
                await reader.cancel(error).catch(() => {});
                throw error;
            }
            chunks.push(chunk);
            total += chunk.byteLength;
        }
    } finally {
        reader.releaseLock?.();
    }
    const bytes = new Uint8Array(total);
    let offset = 0;
    chunks.forEach(chunk => {
        bytes.set(chunk, offset);
        offset += chunk.byteLength;
    });
    return bytes;
}

function artifactMime(format) {
    return {
        mp3: 'audio/mpeg',
        wav: 'audio/wav',
        zip: 'application/zip',
        json: 'application/json',
    }[String(format || '').toLowerCase()] || 'application/octet-stream';
}

function filenameWithExtension(filename, format, fallback = '下载文件') {
    const raw = String(filename || '').trim().split(/[\\/]/).pop() || fallback;
    const extension = String(format || '').trim().toLowerCase().replace(/^\./, '');
    if (!extension || raw.toLowerCase().endsWith(`.${extension}`)) return raw;
    return `${raw}.${extension}`;
}

function deliveryZipFilename(sourceFilename = '') {
    const source = String(sourceFilename || PRODUCT_NAME).trim().split(/[\\/]/).pop() || PRODUCT_NAME;
    const stem = source.replace(/\.(docx|xlsx)$/i, '') || PRODUCT_NAME;
    return filenameWithExtension(`${stem}_tts`, 'zip', `${PRODUCT_NAME}_tts`);
}

function createAbortError(message = '音频播放源已取消') {
    const error = new Error(message);
    error.name = 'AbortError';
    return error;
}

function supportsMediaSourceMime(mimeType) {
    if (typeof MediaSource !== 'function' || typeof MediaSource.isTypeSupported !== 'function') return false;
    try {
        return Boolean(mimeType) && MediaSource.isTypeSupported(mimeType);
    } catch (_) {
        return false;
    }
}

function waitForNativeAudioReady(audio, timeoutMs = 15000) {
    if (!audio) return Promise.reject(new Error('音频播放器不可用'));
    if (audio.readyState >= 2) return Promise.resolve(audio);
    if (audio.error) {
        const error = new Error('音频资源无法解码');
        error.code = 'MEDIA_DECODE_ERROR';
        return Promise.reject(error);
    }
    return new Promise((resolve, reject) => {
        let settled = false;
        const timeout = Math.max(1000, Number(timeoutMs) || 15000);
        let timer = null;
        const cleanup = () => {
            audio.removeEventListener('loadeddata', onReady);
            audio.removeEventListener('canplay', onReady);
            audio.removeEventListener('error', onError);
            audio.removeEventListener('abort', onAbort);
            if (timer) clearTimeout(timer);
        };
        const finish = (handler, value) => {
            if (settled) return;
            settled = true;
            cleanup();
            handler(value);
        };
        const onReady = () => finish(resolve, audio);
        const onError = () => {
            const error = new Error('音频资源无法解码');
            error.code = 'MEDIA_DECODE_ERROR';
            finish(reject, error);
        };
        const onAbort = () => finish(reject, createAbortError('音频加载已取消'));
        audio.addEventListener('loadeddata', onReady, { once: true });
        audio.addEventListener('canplay', onReady, { once: true });
        audio.addEventListener('error', onError, { once: true });
        audio.addEventListener('abort', onAbort, { once: true });
        timer = setTimeout(() => {
            const error = new Error('音频加载超时');
            error.code = 'MEDIA_LOAD_TIMEOUT';
            finish(reject, error);
        }, timeout);
        if (audio.readyState >= 2) finish(resolve, audio);
    });
}

function waitForMediaSourceOpen(mediaSource, isCurrent) {
    if (mediaSource?.readyState === 'open') return Promise.resolve();
    return new Promise((resolve, reject) => {
        let settled = false;
        const cleanup = () => {
            mediaSource?.removeEventListener('sourceopen', onOpen);
            mediaSource?.removeEventListener('sourceclose', onClose);
            mediaSource?.removeEventListener('error', onError);
        };
        const finish = (handler, value) => {
            if (settled) return;
            settled = true;
            cleanup();
            handler(value);
        };
        const onOpen = () => {
            if (!isCurrent()) return finish(reject, createAbortError());
            finish(resolve);
        };
        const onClose = () => finish(reject, new Error('音频 MediaSource 已关闭'));
        const onError = () => finish(reject, new Error('音频 MediaSource 打开失败'));
        mediaSource?.addEventListener('sourceopen', onOpen, { once: true });
        mediaSource?.addEventListener('sourceclose', onClose, { once: true });
        mediaSource?.addEventListener('error', onError, { once: true });
    });
}

function appendMediaSourceChunk(sourceBuffer, chunk, isCurrent) {
    if (!isCurrent()) return Promise.reject(createAbortError());
    return new Promise((resolve, reject) => {
        let settled = false;
        const cleanup = () => {
            sourceBuffer?.removeEventListener('updateend', onUpdateEnd);
            sourceBuffer?.removeEventListener('error', onError);
            sourceBuffer?.removeEventListener('abort', onAbort);
        };
        const finish = (handler, value) => {
            if (settled) return;
            settled = true;
            cleanup();
            handler(value);
        };
        const onUpdateEnd = () => {
            if (!isCurrent()) return finish(reject, createAbortError());
            finish(resolve);
        };
        const onError = () => finish(reject, new Error('音频数据追加失败'));
        const onAbort = () => finish(reject, createAbortError());
        sourceBuffer?.addEventListener('updateend', onUpdateEnd, { once: true });
        sourceBuffer?.addEventListener('error', onError, { once: true });
        sourceBuffer?.addEventListener('abort', onAbort, { once: true });
        try {
            if (!sourceBuffer || sourceBuffer.updating) throw new Error('音频缓冲区正在更新');
            sourceBuffer.appendBuffer(chunk);
        } catch (error) {
            finish(reject, error);
        }
    });
}

function buildResultPage(event, suppliedContext = null) {
    destroyWaveSurfers();
    const workflowSourceTotal = summarizeParseResults(currentSession?.parse_results).total;
    const workspace = suppliedContext?.workspace || currentWorkspace;
    const workspaceCounts = workspaceProgress(workspace);
    const context = suppliedContext || {
        mode: 'current',
        sessionId: currentSession?.session_id,
        workflowId: currentSession?.session_id || event.workflow_id || null,
        sourceFilename: currentSession?.source_filename,
        files: generatedFiles,
        completed: event.completed ?? workspaceCounts.completed ?? generatedFiles.length ?? 0,
        failed: event.failed ?? workspaceCounts.failed ?? 0,
        cancelled: event.cancelled ?? workspaceCounts.cancelled ?? 0,
        total: workflowSourceTotal || event.total || workspaceCounts.total || 0,
        format: lastGenerationConfig?.format || currentConfig?.format || 'mp3',
        preview: Boolean(lastGenerationConfig?.preview && workflowSourceTotal > 3),
        zipAvailable: Boolean(currentSession?.delivery?.zip_available || event.zip_artifact_id),
        zipArtifactId: currentSession?.delivery?.zip_artifact_id || event.zip_artifact_id || null,
        failedItems: Array.isArray(event.failed_items) ? event.failed_items : [],
        stateVersion: Number(currentSession?.state_version || 0),
        executionState: currentSession?.execution_state || event.execution_state || null,
        resultStatus: currentSession?.result_status || event.result_status || null,
        workspace,
    };
    activeResultContext = context;
    const isHistory = context.mode === 'history';
    const resultFiles = Array.isArray(context.files) ? context.files : [];
    const success = resultFiles.length;
    const deliveryBlockers = (Array.isArray(workspace?.blockers) ? workspace.blockers : []).filter(blocker => (
        ['BLOCKING', 'ERROR'].includes(String(blocker?.severity || '').toUpperCase())
        && ['ARTIFACT_MISSING_OR_UNVERIFIED', 'ARTIFACT_FORMAT_UNSUPPORTED', 'ARTIFACT_METADATA_CONFLICT'].includes(String(blocker?.code || '').toUpperCase())
    ));
    const deliveryAffectedItemIds = new Set(
        deliveryBlockers.flatMap(blocker => Array.isArray(blocker?.affected_item_ids) ? blocker.affected_item_ids.map(String) : []),
    );
    const deliveryIssueCount = deliveryBlockers.length > 0
        ? Math.max(1, deliveryAffectedItemIds.size)
        : 0;
    const { missingFiles, failed, cancelled, unresolved } = resultSummaryCounts(
        context,
        success,
        workspaceCounts,
        deliveryIssueCount,
    );
    const hasDeliveryIssue = deliveryIssueCount > 0;
    const resultTitle = $('result-title');
    const resultEyebrow = $('result-eyebrow');
    const resultIcon = document.querySelector('.result-success-icon');
    const generateFullBtn = $('generate-full-btn');
    const rerunTaskBtn = $('rerun-task-btn');
    const resultWarning = $('result-warning');
    const resultWarningText = $('result-warning-text');
    const failureList = $('result-failure-list');
    const retryFailedBtn = $('retry-failed-btn');
    const warningActions = document.querySelector('.result-warning-actions');
    const backToHistoryBtn = $('back-to-history-btn');
    const sourceTotal = Math.max(0, Number(context.total) || workflowSourceTotal || success + failed + cancelled);
    const isPreviewResult = Boolean(context.preview);
    const failedItems = Array.isArray(context.failedItems) ? context.failedItems : [];

    if (generateFullBtn) {
        generateFullBtn.hidden = isHistory || !lastGenerationConfig?.preview || workflowSourceTotal <= 3 || success === 0;
    }
    if (rerunTaskBtn) {
        const rerunAction = workflowAdapter.action?.(workspace, 'RERUN');
        rerunTaskBtn.hidden = rerunAction?.enabled !== true;
        rerunTaskBtn.disabled = false;
        rerunTaskBtn.title = rerunAction?.enabled === true ? '' : (rerunAction?.reason || '当前任务不能重新运行');
    }
    if (backToHistoryBtn) backToHistoryBtn.hidden = !isHistory;
    if (warningActions) warningActions.hidden = isHistory;
    if (resultWarning) resultWarning.hidden = unresolved === 0;
    if (resultWarningText && unresolved > 0) {
        const availabilityNote = success > 0
            ? '其余已验证音频仍可试听和下载。'
            : '当前没有可试听或下载的音频文件。';
        const historyIssueSummary = [
            failed > 0 ? `${failed} 条未完成或音频缺失` : '',
            cancelled > 0 ? `${cancelled} 条已取消` : '',
        ].filter(Boolean).join('、');
        resultWarningText.textContent = isHistory
            ? (hasDeliveryIssue
                ? `这条历史记录有 ${deliveryIssueCount} 条音频产物尚未通过交付核验${historyIssueSummary ? `；另有 ${historyIssueSummary}` : ''}。${availabilityNote}`
                : `这条历史记录有 ${historyIssueSummary || '部分内容未能生成'}。${availabilityNote}`)
            : (success > 0
                ? `${hasDeliveryIssue ? `有 ${deliveryIssueCount} 条音频产物待交付核验；` : ''}${failed} 条失败、${cancelled} 条已取消。沿用当前设置只重试安全失败项；修改参数后会重新生成全部内容。`
                : (hasDeliveryIssue
                    ? `本次有 ${deliveryIssueCount} 条音频产物尚未通过交付核验，请先重新同步或处理任务详情。`
                    : `本次共有 ${failed} 条失败、${cancelled} 条已取消。请根据任务详情处理后再重试。`));
    }
    if (retryFailedBtn) retryFailedBtn.hidden = isHistory
        || failed === 0
        || !lastGenerationConfig
        || isTerminalWorkflowSnapshot(workspace?.snapshot || currentSession);
    if (failureList) {
        failureList.innerHTML = '';
        const displayedItems = failedItems.slice(0, 5);
        failureList.hidden = displayedItems.length === 0;
        displayedItems.forEach(item => {
            const row = document.createElement('li');
            const name = document.createElement('strong');
            name.textContent = item.id || item.doc_type || '未命名内容';
            name.title = name.textContent;
            const reason = document.createElement('span');
            reason.textContent = item.error || '生成服务未返回具体原因';
            row.appendChild(name);
            row.appendChild(reason);
            failureList.appendChild(row);
        });
        if (failed > displayedItems.length && displayedItems.length > 0) {
            const remaining = document.createElement('li');
            remaining.className = 'result-failure-more';
            remaining.textContent = isHistory
                ? `另有 ${failed - displayedItems.length} 条未完成内容未展开。`
                : `另有 ${failed - displayedItems.length} 条失败内容未展开，重试时会自动包含。`;
            failureList.appendChild(remaining);
        }
    }

    if (resultIcon) resultIcon.classList.remove('has-warning', 'has-error');
    if (success === 0 && unresolved > 0) {
        if (resultEyebrow) resultEyebrow.textContent = hasDeliveryIssue ? '交付需要处理' : (isPreviewResult ? '试听需要处理' : '任务需要处理');
        if (resultTitle) resultTitle.textContent = hasDeliveryIssue ? '音频产物尚未通过核验' : (isPreviewResult ? '本次试听未能生成音频' : '本次任务未能生成音频');
        if (resultIcon) resultIcon.classList.add('has-error');
    } else if (unresolved > 0) {
        if (resultEyebrow) resultEyebrow.textContent = isPreviewResult ? '试听部分完成' : '任务部分完成';
        if (resultTitle) resultTitle.textContent = isPreviewResult ? '部分试听音频已经准备好' : '部分音频已经准备好';
        if (resultIcon) resultIcon.classList.add('has-warning');
    } else if (isPreviewResult) {
        if (resultEyebrow) resultEyebrow.textContent = '试听生成完成';
        if (resultTitle) resultTitle.textContent = '试听音频已经准备好';
    } else if (resultTitle) {
        if (resultEyebrow) resultEyebrow.textContent = '任务已完成';
        resultTitle.textContent = '音频已经准备好了';
    }

    // 摘要
    let summaryText = isHistory
        ? `「${context.sourceFilename || '未命名文档'}」可用 ${success} 个音频文件${unresolved > 0 ? `，${failed} 个失败、${cancelled} 个已取消或缺失` : ''}`
        : (isPreviewResult
            ? `本次试听生成 ${success} 个音频${unresolved > 0 ? `，${failed} 个失败、${cancelled} 个已取消` : ''}；确认效果后可继续生成完整文档`
            : `成功生成 ${success} 个音频文件${unresolved > 0 ? `，${failed} 个失败、${cancelled} 个已取消${hasDeliveryIssue ? `、${deliveryIssueCount} 个产物待核验` : ''}` : ''}`);
    $('result-summary').textContent = summaryText;
    $('result-success-label').textContent = isPreviewResult ? '试听文件' : '已生成';
    $('result-success-count').textContent = String(success);
    $('result-success-caption').textContent = isPreviewResult && !isHistory
        ? `本次范围：前 ${Math.min(sourceTotal, 3)} 条`
        : '音频文件';
    $('result-secondary-label').textContent = isPreviewResult && !isHistory ? '文档总量' : '未完成';
    $('result-failed-count').textContent = String(isPreviewResult && !isHistory ? sourceTotal : unresolved);
    if ($('result-cancelled-count')) $('result-cancelled-count').textContent = String(cancelled);
    const unfinishedCaption = [
        failed > 0 ? (missingFiles > 0 ? '失败或缺失' : '失败') : '',
        cancelled > 0 ? '已取消' : '',
        hasDeliveryIssue ? '待核验' : '',
    ].filter(Boolean).join(' / ') || '待处理内容';
    $('result-secondary-caption').textContent = isPreviewResult && !isHistory
        ? '完整文档内容'
        : unfinishedCaption;
    const resultFormat = resultFiles[0]?.format
        || context.format
        || workspace?.configuration?.effective?.format
        || '';
    $('result-format-value').textContent = String(resultFormat || '待同步').toUpperCase();

    // ZIP 卡片
    const zipCard = $('zip-card');
    const resultHero = $('result-hero');
    const zipFilename = deliveryZipFilename(context.sourceFilename);
    const zipName = zipCard?.querySelector('.zip-name');
    if (zipName) {
        // Show the exact suggested package name in the delivery card. The
        // button used to say only “准备交付包”, which hid a missing/incorrect
        // extension until after the native save dialog opened.
        zipName.textContent = zipFilename;
        zipName.title = zipFilename;
    }
    // The delivery projection is authoritative for an already-created ZIP.
    // A terminal result with verified audio still exposes the on-demand
    // action; the server creates and verifies the ZIP when it is clicked.
    const zipState = resultZipState(context, success);
    if (zipState.visible) {
        // Restore the stylesheet's grid layout. An inline flex override makes
        // the download button stretch into a full-height blue column.
        zipCard.style.removeProperty('display');
        resultHero?.classList.remove('has-no-package');
        const scope = workflowAdapter.deliveryScope?.(workspace || context) || {
            included: [],
            excluded: [],
            reasons: {},
            zipArtifactId: null,
            zipAvailable: false,
        };
        const hasScope = Boolean(
            (workspace || context)?.delivery
            && Array.isArray((workspace || context).delivery.included_item_ids)
            && Array.isArray((workspace || context).delivery.excluded_item_ids),
        );
        const includedCount = scope.included.length;
        const excludedCount = scope.excluded.length;
        const deliveryScopeEl = $('delivery-scope');
        const exclusionNote = $('delivery-exclusion-note');
        const exclusionList = $('delivery-exclusion-list');
        if (deliveryScopeEl) {
            deliveryScopeEl.textContent = `交付范围：${includedCount} 条已验证音频${excludedCount > 0 ? ` · ${excludedCount} 条未纳入` : ''}`;
        }
        if (exclusionNote) {
            exclusionNote.hidden = excludedCount === 0;
            if (excludedCount > 0) {
                const reasonLabels = {
                    ITEM_CANCELLED: '已取消',
                    ITEM_FAILED: '生成失败',
                    ITEM_SKIPPED: '已跳过',
                    REQUIRES_RECONCILE: '未完成',
                    ARTIFACT_MISSING_OR_UNVERIFIED: '产物待核验',
                    ARTIFACT_FORMAT_UNSUPPORTED: '格式未验证',
                    NOT_GENERATED: '尚未生成',
                    NOT_SELECTED: '未选择',
                    ITEM_ARTIFACT_CONFLICT: '产物状态冲突',
                };
                const labels = [...new Set(scope.excluded.map(itemId => reasonLabels[scope.reasons?.[itemId]] || '未纳入'))];
                exclusionNote.textContent = `未纳入原因：${labels.join('、')}`;
            } else {
                exclusionNote.textContent = '';
            }
        }
        if (exclusionList) {
            exclusionList.replaceChildren();
            const details = workflowAdapter.exclusionDetails?.(workspace || context) || scope.excluded.map(itemId => ({
                itemId,
                reasonLabel: scope.reasons?.[itemId] || '未纳入',
                contentPreview: '正文未随列表加载',
            }));
            exclusionList.hidden = details.length === 0;
            details.slice(0, 500).forEach(detail => {
                const row = document.createElement('li');
                const label = document.createElement('strong');
                label.textContent = detail.sequence ? `第 ${detail.sequence} 条 · ${detail.reasonLabel}` : `${detail.itemId} · ${detail.reasonLabel}`;
                const content = document.createElement('span');
                content.textContent = detail.contentPreview || '正文未随列表加载';
                row.append(label, content);
                if (detail.sourceLocator) {
                    const source = document.createElement('small');
                    source.textContent = `来源：${detail.sourceLocator}`;
                    row.appendChild(source);
                }
                exclusionList.appendChild(row);
            });
            if (details.length > 500) {
                const more = document.createElement('li');
                more.className = 'delivery-exclusion-more';
                more.textContent = `另有 ${details.length - 500} 条未展开，完整范围仍由服务端交付投影控制。`;
                exclusionList.appendChild(more);
            }
        }
        $('zip-desc').textContent = zipState.ready
            ? `ZIP 压缩包包含 ${includedCount} 个已验证的音频文件`
            : `点击下载时自动整理 ${includedCount} 个已验证的音频文件`;
        const zipButton = $('download-zip-btn');
        if (zipButton) {
            zipButton.disabled = !hasScope || includedCount === 0;
            zipButton.title = zipButton.disabled ? '等待交付范围核验' : `下载 ${zipFilename}`;
        }
    } else {
        zipCard.style.display = 'none';
        resultHero?.classList.add('has-no-package');
        $('delivery-exclusion-list')?.replaceChildren();
        if ($('delivery-exclusion-list')) $('delivery-exclusion-list').hidden = true;
    }

    // 音频列表
    const audioList = $('audio-list');
    const audioListSection = document.querySelector('.audio-list-section');
    audioList.innerHTML = '';
    prepareAudioFilters(resultFiles);

    if (resultFiles.length === 0) {
        audioList.innerHTML = '<div class="audio-empty">暂无音频文件</div>';
        $('audio-count').textContent = '0 个文件';
        if (audioListSection) audioListSection.hidden = true;
        return;
    }

    if (audioListSection) audioListSection.hidden = false;
    $('audio-count').textContent = `${resultFiles.length} 个文件`;
    const renderToken = waveformRenderToken;
    const itemFragment = document.createDocumentFragment();

    resultFiles.forEach((f, index) => {
        const color = (currentConfig && currentConfig.type_colors && currentConfig.type_colors[f.doc_type]) || '#a8a29e';

        // 使用 DOM API 安全构建，避免 innerHTML 注入风险
        const item = document.createElement('article');
        item.className = 'audio-item';
        item.setAttribute('aria-label', f.filename);
        item.style.setProperty('--item-index', String(Math.min(index, 5)));
        item.dataset.docType = f.doc_type || '';
        item.dataset.searchText = [f.filename, f.doc_type, f.category, f.text, f.text_preview]
            .filter(Boolean)
            .join(' ')
            .toLocaleLowerCase('zh-CN');

        // --- 头部：序号 + 文件信息 + 下载按钮 ---
        const header = document.createElement('div');
        header.className = 'audio-item-header';

        const indexBadge = document.createElement('span');
        indexBadge.className = 'audio-index';
        indexBadge.textContent = String(index + 1).padStart(2, '0');

        const dot = document.createElement('span');
        dot.className = 'audio-dot';
        dot.style.background = color;

        const info = document.createElement('div');
        info.className = 'audio-info';

        const name = document.createElement('div');
        name.className = 'audio-name';
        name.textContent = f.filename;

        const meta = document.createElement('div');
        meta.className = 'audio-meta';
        const metaText = document.createElement('span');
        metaText.textContent = [f.doc_type, f.category].filter(Boolean).join(' · ') || '音频文件';
        meta.appendChild(dot);
        meta.appendChild(metaText);

        info.appendChild(name);
        info.appendChild(meta);

        const dlBtn = document.createElement('button');
        dlBtn.className = 'audio-download-btn';
        dlBtn.title = '下载此文件';
        dlBtn.setAttribute('aria-label', `下载 ${f.filename}`);
        dlBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg><span>下载</span>`;
        dlBtn.addEventListener('click', async () => {
            if (dlBtn.disabled) return;
            dlBtn.disabled = true;
            dlBtn.classList.add('is-busy');
            try {
                await downloadFile(f, context);
            } finally {
                dlBtn.disabled = false;
                dlBtn.classList.remove('is-busy');
            }
        });

        header.appendChild(indexBadge);
        header.appendChild(info);
        header.appendChild(dlBtn);
        item.appendChild(header);

        // 每个生成文件携带本题实际使用的音色；头像优先使用已完成的本机
        // 缓存，缓存尚未完成时先显示目录资源，试听仍保留原始地址回退。
        // 全局只允许一个试听同时播放。
        item.appendChild(createResultVoiceStrip(f));
        item._resultFile = f;

        // 原生 Audio 负责播放；可用时通过 MediaSource 逐块追加 Artifact，
        // 不支持该 MIME 的浏览器才退回到有界 Blob。WaveSurfer 只负责绘制
        // 波形与定位，不再拥有另一份音频数据。
        const audio = new Audio();
        // Do not start multiple Artifact reads while the result list is being
        // painted. Playback and waveform loading request bytes on demand;
        // this keeps the renderer's one-shot ticket streams deterministic.
        audio.preload = 'none';
        item._audioElement = audio;
        item._artifactId = f.artifact_id || null;
        let audioReadyPromise = null;
        let audioObjectUrl = null;
        let audioStreamReader = null;
        let audioMediaSource = null;
        let audioStreamTask = null;
        let audioSourceGeneration = 0;
        const resetAudioSource = () => {
            audioSourceGeneration += 1;
            audio._artifactStreamError = null;
            const reader = audioStreamReader;
            audioStreamReader = null;
            if (reader) void reader.cancel().catch(() => {});
            if (audioMediaSource?.readyState === 'open') {
                try { audioMediaSource.endOfStream(); } catch (_) { /* stream may already be closing */ }
            }
            audioMediaSource = null;
            audioStreamTask = null;
            if (audioObjectUrl) {
                try { URL.revokeObjectURL(audioObjectUrl); } catch (_) { /* ignore */ }
                artifactObjectUrls.delete(audioObjectUrl);
                audioObjectUrl = null;
            }
            try {
                audio.pause();
                audio.removeAttribute('src');
                audio.load();
            } catch (_) { /* ignore */ }
            audioReadyPromise = null;
        };
        item.resetAudioSource = resetAudioSource;
        const streamAudioWithMediaSource = async () => {
            const generation = ++audioSourceGeneration;
            const isCurrent = () => (
                generation === audioSourceGeneration
                && renderToken === waveformRenderToken
                && item.isConnected
            );
            const mediaSource = new MediaSource();
            const url = URL.createObjectURL(mediaSource);
            audioMediaSource = mediaSource;
            audioObjectUrl = url;
            artifactObjectUrls.add(url);
            audio.preload = 'auto';
            audio.src = url;
            audio.load();

            let started = false;
            let resolveStarted;
            let rejectStarted;
            const startedPromise = new Promise((resolve, reject) => {
                resolveStarted = resolve;
                rejectStarted = reject;
            });
            let reader = null;
            const pump = (async () => {
                try {
                    await waitForMediaSourceOpen(mediaSource, isCurrent);
                    if (!isCurrent()) throw createAbortError();
                    const sourceBuffer = mediaSource.addSourceBuffer(f.mime_type);
                    const stream = await openRendererArtifactStream(item._artifactId);
                    reader = stream.getReader();
                    audioStreamReader = reader;
                    let receivedChunk = false;
                    while (true) {
                        if (!isCurrent()) throw createAbortError();
                        const part = await reader.read();
                        if (part.done) break;
                        const chunk = part.value instanceof Uint8Array
                            ? part.value
                            : new Uint8Array(part.value || []);
                        if (chunk.byteLength === 0) continue;
                        await appendMediaSourceChunk(sourceBuffer, chunk, isCurrent);
                        receivedChunk = true;
                        if (!started) {
                            started = true;
                            resolveStarted(audio);
                        }
                    }
                    if (!receivedChunk) throw new Error('Artifact 音频流为空');
                    if (isCurrent() && mediaSource.readyState === 'open') {
                        try { mediaSource.endOfStream(); } catch (_) { /* ignore close race */ }
                    }
                } catch (error) {
                    if (!started) rejectStarted(error);
                    else if (isCurrent() && error?.name !== 'AbortError') {
                        audio._artifactStreamError = error;
                        console.warn('Artifact 音频流中断:', error);
                    }
                } finally {
                    if (audioStreamReader === reader) audioStreamReader = null;
                }
            })();
            audioStreamTask = pump;
            await startedPromise;
            return audio;
        };
        item.ensureAudioReady = async () => {
            if (audio.src) return audio;
            if (!item._artifactId) throw new Error('音频 Artifact 不可用');
            if (!audioReadyPromise) {
                const pending = (async () => {
                    const mimeType = f.mime_type || artifactMime(f.format);
                    const declaredSize = Number(f.size_bytes);
                    // Short MP3 segments are more reliable as one verified
                    // Blob: MediaSource can report a started stream before
                    // Chromium has enough frames to decode/play it, while a
                    // Blob gives Audio and WaveSurfer one stable resource.
                    // Retain MSE only for artifacts too large for the bounded
                    // renderer buffer.
                    const useMediaSource = Number.isSafeInteger(declaredSize)
                        && declaredSize > MAX_BUFFERED_ARTIFACT_BYTES
                        && supportsMediaSourceMime(mimeType);
                    if (useMediaSource) {
                        try {
                            return await streamAudioWithMediaSource();
                        } catch (error) {
                            resetAudioSource();
                            if (declaredSize > MAX_BUFFERED_ARTIFACT_BYTES) {
                                error.code = error.code || 'ARTIFACT_STREAM_UNSUPPORTED';
                                throw error;
                            }
                        }
                    }
                    if (declaredSize > MAX_BUFFERED_ARTIFACT_BYTES) {
                        const error = new Error('当前环境不支持该音频格式的流式播放，且文件超过浏览器有界读取上限');
                        error.code = 'ARTIFACT_STREAM_UNSUPPORTED';
                        throw error;
                    }
                    const bytes = await readArtifactBytes(item._artifactId, MAX_BUFFERED_ARTIFACT_BYTES);
                    if (renderToken !== waveformRenderToken || !item.isConnected) throw createAbortError('结果页已切换');
                    const url = URL.createObjectURL(new Blob([bytes], { type: mimeType }));
                    audioObjectUrl = url;
                    artifactObjectUrls.add(url);
                    audio.preload = 'auto';
                    audio.src = url;
                    audio.load();
                    return audio;
                })();
                audioReadyPromise = pending.catch(error => {
                    // A transient ticket/network failure must not poison the
                    // item forever; the next play/retry requests fresh bytes.
                    audioReadyPromise = null;
                    throw error;
                });
            }
            return audioReadyPromise;
        };
        audioElements.push(audio);

        const waveformWrap = document.createElement('div');
        waveformWrap.className = 'waveform-wrap';

        const playBtn = document.createElement('button');
        playBtn.className = 'waveform-play-btn';
        playBtn.title = `播放 ${f.filename}`;
        playBtn.dataset.audioName = f.filename;
        playBtn.setAttribute('aria-label', `播放 ${f.filename}`);
        playBtn.innerHTML = '<svg class="icon-play" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg><svg class="icon-pause" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style="display:none"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
        waveformWrap.appendChild(playBtn);
        audio._playButton = playBtn;

        const canvasWrap = document.createElement('div');
        canvasWrap.className = 'waveform-canvas-wrap';
        canvasWrap.tabIndex = 0;
        canvasWrap.setAttribute('role', 'slider');
        canvasWrap.setAttribute('aria-label', `${f.filename} 播放进度`);
        canvasWrap.setAttribute('aria-valuemin', '0');
        canvasWrap.setAttribute('aria-valuemax', '0');
        canvasWrap.setAttribute('aria-valuenow', '0');
        canvasWrap.setAttribute('aria-busy', 'true');

        const retryWaveformButton = document.createElement('button');
        retryWaveformButton.type = 'button';
        retryWaveformButton.className = 'waveform-retry-btn';
        retryWaveformButton.textContent = '重试波形';
        retryWaveformButton.setAttribute('aria-label', `重试加载 ${f.filename} 的波形`);
        retryWaveformButton.hidden = true;
        canvasWrap.appendChild(retryWaveformButton);

        const placeholder = document.createElement('div');
        placeholder.className = 'waveform-placeholder';
        placeholder.setAttribute('aria-hidden', 'true');
        const waveSeed = Array.from(f.filename || '').reduce((sum, char) => sum + char.charCodeAt(0), 0);
        for (let barIndex = 0; barIndex < WAVEFORM_PLACEHOLDER_BARS; barIndex++) {
            const bar = document.createElement('span');
            const height = 22 + ((waveSeed + barIndex * 29 + (barIndex % 7) * 13) % 64);
            bar.style.setProperty('--wave-height', `${height}%`);
            placeholder.appendChild(bar);
        }
        canvasWrap.appendChild(placeholder);

        const wsContainer = document.createElement('div');
        wsContainer.className = 'waveform-container';
        wsContainer.setAttribute('aria-hidden', 'true');
        canvasWrap.appendChild(wsContainer);

        const timeLabel = document.createElement('span');
        timeLabel.className = 'waveform-time';
        timeLabel.textContent = '00:00 / 00:00';
        canvasWrap.appendChild(timeLabel);

        waveformWrap.appendChild(canvasWrap);
        item.appendChild(waveformWrap);

        // --- 原文折叠展示，避免长列表出现嵌套滚动 ---
        const textSection = document.createElement('details');
        textSection.className = 'audio-text-section';
        textSection.open = true;

        const textSummary = document.createElement('summary');
        textSummary.textContent = '查看对应原文';
        textSection.appendChild(textSummary);

        const textBody = document.createElement('div');
        textBody.className = 'audio-text-body';
        // 显示完整原文，保留换行
        const fullText = f.text || f.text_preview || '';
        if (fullText) {
            textBody.textContent = fullText;
        } else {
            textBody.textContent = '（无原文数据）';
            textBody.style.opacity = '0.5';
        }
        textSection.appendChild(textBody);
        item.appendChild(textSection);

        itemFragment.appendChild(item);

        const updateNativeTime = () => {
            const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
            const current = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
            timeLabel.textContent = `${formatTime(current)} / ${formatTime(duration)}`;
            canvasWrap.setAttribute('aria-valuemax', String(Math.round(duration)));
            canvasWrap.setAttribute('aria-valuenow', String(Math.round(current)));
            canvasWrap.setAttribute('aria-valuetext', `${formatTime(current)} / ${formatTime(duration)}`);
            canvasWrap.setAttribute('aria-busy', duration > 0 ? 'false' : 'true');
        };

        audio.addEventListener('loadedmetadata', updateNativeTime);
        audio.addEventListener('durationchange', updateNativeTime);
        audio.addEventListener('timeupdate', updateNativeTime);
        audio.addEventListener('play', () => {
            if (audio._playRequestToken !== audioPlayRequestToken) {
                audio.pause();
                return;
            }
            currentPlayingAudio = audio;
            item.classList.add('is-playing');
            playBtn.classList.remove('is-buffering');
            updatePlayIcon(playBtn, true);
        });
        audio.addEventListener('pause', () => {
            if (currentPlayingAudio === audio) currentPlayingAudio = null;
            item.classList.remove('is-playing');
            playBtn.classList.remove('is-buffering');
            updatePlayIcon(playBtn, false);
        });
        audio.addEventListener('ended', () => {
            if (currentPlayingAudio === audio) currentPlayingAudio = null;
            item.classList.remove('is-playing');
            playBtn.classList.remove('is-buffering');
            updatePlayIcon(playBtn, false);
            try { audio.currentTime = 0; } catch (_) { /* ignore */ }
            updateNativeTime();
        });
        audio.addEventListener('waiting', () => {
            if (!audio.paused) playBtn.classList.add('is-buffering');
        });
        audio.addEventListener('playing', () => playBtn.classList.remove('is-buffering'));
        audio.addEventListener('canplay', () => playBtn.classList.remove('is-buffering'));

        let waveSurfer = null;
        let waveLoadRelease = null;
        let waveLoadTimeout = null;
        item._waveformInitialized = false;
        item._waveformFailed = false;

        const finishWaveformLoad = () => {
            if (waveLoadTimeout) {
                clearTimeout(waveLoadTimeout);
                waveLoadTimeout = null;
            }
            const release = waveLoadRelease;
            waveLoadRelease = null;
            if (typeof release === 'function') release();
        };

        item.cancelWaveformLoad = (markFailed = true) => {
            if (markFailed) {
                item._waveformFailed = true;
                canvasWrap.classList.remove('is-wave-ready');
                canvasWrap.classList.add('is-wave-error');
                retryWaveformButton.hidden = false;
            }
            const activeWaveSurfer = waveSurfer;
            waveSurfer = null;
            if (activeWaveSurfer) {
                const instanceIndex = wavesurferInstances.indexOf(activeWaveSurfer);
                if (instanceIndex >= 0) wavesurferInstances.splice(instanceIndex, 1);
                try {
                    activeWaveSurfer.setMediaElement(new Audio());
                    activeWaveSurfer.destroy();
                } catch (_) { /* ignore */ }
            }
            item._waveformInitialized = false;
            finishWaveformLoad();
        };

        item.initializeWaveform = (onSettled = () => {}) => {
            if (item._waveformInitialized || item._waveformFailed) return waveSurfer;
            if (renderToken !== waveformRenderToken || !item.isConnected || canvasWrap.getBoundingClientRect().width <= 1) return null;
            item._waveformInitialized = true;
            waveLoadRelease = onSettled;
            waveLoadTimeout = setTimeout(() => item.cancelWaveformLoad(true), 30000);
            waveformObserver?.unobserve(item);
            void item.ensureAudioReady().then(() => {
                if (renderToken !== waveformRenderToken || !item.isConnected || !item._waveformInitialized) return;
                waveSurfer = createWaveSurfer(
                    wsContainer,
                    audio,
                    color,
                    canvasWrap,
                    readyWs => {
                        retryWaveformButton.hidden = true;
                        const duration = readyWs?.getDuration?.() || 0;
                        if (duration > 0) {
                            if (!Number.isFinite(audio.duration) || audio.duration <= 0) {
                                timeLabel.textContent = `00:00 / ${formatTime(duration)}`;
                            }
                            canvasWrap.setAttribute('aria-valuemax', String(Math.round(duration)));
                            canvasWrap.setAttribute('aria-busy', 'false');
                        }
                        finishWaveformLoad();
                    },
                    () => {
                        item._waveformFailed = true;
                        item._waveformInitialized = false;
                        waveSurfer = null;
                        retryWaveformButton.hidden = false;
                        finishWaveformLoad();
                    },
                );
                if (!waveSurfer) finishWaveformLoad();
            }).catch(() => {
                if (renderToken !== waveformRenderToken) return;
                item._waveformFailed = true;
                item._waveformInitialized = false;
                retryWaveformButton.hidden = false;
                finishWaveformLoad();
            });
            return item;
        };

        retryWaveformButton.addEventListener('click', event => {
            event.stopPropagation();
            if (renderToken !== waveformRenderToken || !item.isConnected) return;
            item._waveformFailed = false;
            item._waveformInitialized = false;
            canvasWrap.classList.remove('is-wave-error');
            retryWaveformButton.hidden = true;
            queueWaveformInitialization(item, true);
        });

        audio.addEventListener('error', () => {
            // A successful ticket read can still produce an unsupported or
            // truncated media resource.  Do not let the broken Blob URL make
            // every later click reuse the same failed source; the next click
            // must obtain a fresh Artifact ticket and bytes.
            resetAudioSource();
            item.cancelWaveformLoad(true);
            if (currentPlayingAudio === audio) currentPlayingAudio = null;
            playBtn.classList.remove('is-buffering');
            updatePlayIcon(playBtn, false);
        });

        playBtn.addEventListener('click', async () => {
            queueWaveformInitialization(item, true);
            const requestId = ++audioPlayRequestToken;
            const shouldPause = !audio.paused || currentPlayingAudio === audio;
            audio._playRequestToken = requestId;
            if (shouldPause) {
                audio.pause();
                playBtn.classList.remove('is-buffering');
                if (currentPlayingAudio === audio) currentPlayingAudio = null;
                return;
            }
            audioElements.forEach(otherAudio => {
                if (otherAudio === audio) return;
                otherAudio._playRequestToken = 0;
                otherAudio.pause();
                if (otherAudio._playButton) {
                    otherAudio._playButton.classList.remove('is-buffering');
                    updatePlayIcon(otherAudio._playButton, false);
                }
            });
            currentPlayingAudio = audio;
            playBtn.classList.add('is-buffering');
            try {
                await item.ensureAudioReady();
                if (requestId !== audioPlayRequestToken || renderToken !== waveformRenderToken) return;
                await waitForNativeAudioReady(audio);
                if (requestId !== audioPlayRequestToken || renderToken !== waveformRenderToken) return;
                await audio.play();
            } catch (error) {
                if (requestId !== audioPlayRequestToken) return;
                if (currentPlayingAudio === audio) currentPlayingAudio = null;
                playBtn.classList.remove('is-buffering');
                updatePlayIcon(playBtn, false);
                if (error?.name === 'AbortError') return;
                console.error('音频播放失败:', error);
                if (error?.code === 'ARTIFACT_STREAM_UNSUPPORTED' || error?.code === 'ARTIFACT_TOO_LARGE_FOR_BUFFER') {
                    showToast('当前环境无法播放这个大音频，请使用 Electron 桌面版下载或播放', 'warning');
                } else {
                    showToast('音频暂时无法播放，请稍后重试');
                }
            }
        });

        canvasWrap.addEventListener('keydown', async event => {
            if (event.key === ' ' || event.key === 'Enter') {
                event.preventDefault();
                playBtn.click();
                return;
            }
            const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
            if (!duration) return;
            if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
                event.preventDefault();
                const delta = event.key === 'ArrowLeft' ? -5 : 5;
                audio.currentTime = Math.min(duration, Math.max(0, audio.currentTime + delta));
                updateNativeTime();
            } else if (event.key === 'Home' || event.key === 'End') {
                event.preventDefault();
                audio.currentTime = event.key === 'Home' ? 0 : duration;
                updateNativeTime();
            }
        });

        waveformItems.push(item);
    });
    audioList.appendChild(itemFragment);
    void refreshResultVoiceAssets(resultFiles);
}

// ============================================================================
// 音频波形图 (wavesurfer.js)
// ============================================================================

/**
 * 将 hex 颜色转换为带透明度的 rgba 格式。
 */
function colorWithAlpha(color, alpha) {
    const hex6 = color.match(/^#([0-9a-f]{6})$/i);
    if (hex6) {
        const r = parseInt(hex6[1].substring(0, 2), 16);
        const g = parseInt(hex6[1].substring(2, 4), 16);
        const b = parseInt(hex6[1].substring(4, 6), 16);
        return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    }
    const hex3 = color.match(/^#([0-9a-f]{3})$/i);
    if (hex3) {
        const r = parseInt(hex3[1][0] + hex3[1][0], 16);
        const g = parseInt(hex3[1][1] + hex3[1][1], 16);
        const b = parseInt(hex3[1][2] + hex3[1][2], 16);
        return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    }
    return color;
}

function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '00:00';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/**
 * 更新播放按钮图标（播放/暂停）。
 */
function updatePlayIcon(playBtn, isPlaying) {
    const playIcon = playBtn.querySelector('.icon-play');
    const pauseIcon = playBtn.querySelector('.icon-pause');
    if (!playIcon || !pauseIcon) return;
    const pausePath = pauseIcon.querySelector('path');
    if (pausePath) pausePath.setAttribute('d', 'M6 19h4V5H6v14zm8-14v14h4V5h-4z');
    playIcon.style.display = isPlaying ? 'none' : 'block';
    pauseIcon.style.display = isPlaying ? 'block' : 'none';
    const audioName = playBtn.dataset.audioName ? ` ${playBtn.dataset.audioName}` : '';
    playBtn.setAttribute('aria-label', `${isPlaying ? '暂停' : '播放'}${audioName}`);
    playBtn.title = `${isPlaying ? '暂停' : '播放'}${audioName}`;
}

/**
 * 使用 wavesurfer.js 绘制波形。播放由传入的原生 Audio 元素负责，
 * Artifact 的读取策略由上层选择 MediaSource 或有界 Blob。
 * @param {HTMLElement} container - 波形挂载容器
 * @param {HTMLAudioElement} media - 与播放器共享的原生音频元素
 * @param {string} color - 波形颜色
 * @param {HTMLElement} canvasWrap - 波形视觉外层
 */
function createWaveSurfer(container, media, color, canvasWrap, onReady = null, onLoadError = null) {
    if (typeof WaveSurfer === 'undefined') {
        console.error('WaveSurfer 库未加载');
        canvasWrap.classList.add('is-wave-error');
        if (typeof onLoadError === 'function') onLoadError();
        return null;
    }

    container.replaceChildren();
    canvasWrap.classList.remove('is-wave-ready', 'is-wave-error');

    let ws;
    try {
        ws = WaveSurfer.create({
            container,
            media,
            url: media.currentSrc || media.src,
            height: 43,
            waveColor: colorWithAlpha(color, 0.24),
            progressColor: color,
            cursorColor: colorWithAlpha(color, 0.42),
            cursorWidth: 1,
            barWidth: 2,
            barGap: 2,
            barRadius: 2,
            normalize: true,
            interact: true,
            dragToSeek: true,
            fillParent: true,
            hideScrollbar: true,
        });
    } catch (error) {
        console.error('WaveSurfer 初始化失败:', error);
        canvasWrap.classList.add('is-wave-error');
        if (typeof onLoadError === 'function') onLoadError();
        return null;
    }

    wavesurferInstances.push(ws);

    ws.on('ready', () => {
        canvasWrap.classList.add('is-wave-ready');
        canvasWrap.classList.remove('is-wave-error');
        if (typeof onReady === 'function') onReady(ws);
    });

    ws.on('error', (err) => {
        console.error('WaveSurfer 错误:', err);
        const instanceIndex = wavesurferInstances.indexOf(ws);
        if (instanceIndex >= 0) wavesurferInstances.splice(instanceIndex, 1);
        try {
            // WaveSurfer.destroy() 会暂停外部 media；先切到空 media，避免波形失败打断正在播放的原音频。
            ws.setMediaElement(new Audio());
            ws.destroy();
        } catch (_) { /* ignore */ }
        canvasWrap.classList.remove('is-wave-ready');
        canvasWrap.classList.add('is-wave-error');
        if (typeof onLoadError === 'function') onLoadError();
    });

    return ws;
}

// ============================================================================
// 下载
// ============================================================================

function nativeFileFailureMessage(reason) {
    const messages = {
        'window-unavailable': '当前应用窗口不可用，请重新打开应用后再试。',
        'untrusted-sender': '当前页面没有访问本机文件的权限。',
        'path-check-failed': '待保存文件不在应用的安全目录中。',
        'file-not-found': '待保存文件已经不存在，可能已被清理。',
        'file-check-error': '无法检查待保存文件。',
        'file-too-large': '文件超过本机允许的大小上限。',
        'file-read-error': '无法读取所选文档内容。',
        'content-invalid': '待保存内容无效。',
        'write-error': '无法把文件写入所选位置。',
        'dialog-error': '系统文件对话框未能打开。',
        'copy-error': '无法把文件复制到所选位置。',
        'download-error': '生成文件下载失败，任务中的 Artifact 仍会保留。',
        'ipc-error': '桌面文件服务暂时没有响应。',
    };
    return messages[reason] || '文件操作没有完成。';
}

async function showNativeFileDialogError(title, result = {}) {
    const reason = result?.reason || 'ipc-error';
    const diagnostic = result?.error ? `\n技术信息：${result.error}` : '';
    await showAlertDialog({
        kicker: '本机文件',
        title,
        message: nativeFileFailureMessage(reason),
        detail: `请稍后重试；如果问题持续存在，请重新启动应用。${diagnostic}`,
        tone: 'danger',
        confirmLabel: '知道了',
    });
}

async function saveNativeFile(sourceBytes, suggestedName) {
    try {
        const result = await window.electronAPI.saveFile(sourceBytes, suggestedName);
        if (result?.success) {
            showToast('下载成功');
            return true;
        }
        if (result?.reason === 'user-cancelled') {
            showToast('已取消');
            return false;
        }
        await showNativeFileDialogError('下载文件失败', result || {
            reason: 'ipc-error',
            error: '主进程未返回文件操作结果',
        });
        return false;
    } catch (error) {
        console.error('调用系统保存框失败:', error);
        await showNativeFileDialogError('下载文件失败', {
            reason: 'ipc-error',
            error: error?.message,
        });
        return false;
    }
}

function hideArtifactTransferProgress() {
    const panel = $('artifact-transfer');
    if (panel) panel.hidden = true;
    const cancelButton = $('cancel-artifact-transfer-btn');
    if (cancelButton) {
        cancelButton.disabled = false;
        cancelButton.removeAttribute('aria-busy');
    }
}

function renderArtifactTransferProgress(progress = {}) {
    const panel = $('artifact-transfer');
    const label = $('artifact-transfer-label');
    const value = $('artifact-transfer-value');
    const bar = $('artifact-transfer-bar');
    const cancelButton = $('cancel-artifact-transfer-btn');
    if (!panel || !label || !value || !bar) return;
    panel.hidden = false;
    const state = String(progress.state || 'transferring');
    const received = Math.max(0, Number(progress.receivedBytes) || 0);
    const total = Number(progress.totalBytes);
    const hasTotal = Number.isFinite(total) && total > 0;
    const percent = hasTotal ? Math.min(100, Math.floor(received * 100 / total)) : null;
    label.textContent = state === 'starting'
        ? '正在准备文件传输'
        : state === 'cancelling'
            ? '正在取消文件传输'
        : state === 'cancelled'
            ? '已取消文件传输'
            : state === 'failed'
                ? '文件传输失败'
                : state === 'completed'
                    ? '文件已保存'
                    : '正在保存文件';
    if (percent === null) {
        bar.removeAttribute('value');
        value.textContent = received > 0 ? formatSourceBytes(received) : '处理中';
    } else {
        bar.value = percent;
        value.textContent = `${percent}%`;
    }
    if (cancelButton) {
        const active = !['completed', 'failed', 'cancelled'].includes(state);
        cancelButton.hidden = !active;
        cancelButton.disabled = Boolean(activeArtifactTransfer?.cancelRequested) || !active;
        cancelButton.setAttribute('aria-busy', activeArtifactTransfer?.cancelRequested ? 'true' : 'false');
    }
}

async function cancelArtifactTransfer() {
    const transfer = activeArtifactTransfer;
    if (!transfer || transfer.cancelRequested) return false;
    transfer.cancelRequested = true;
    renderArtifactTransferProgress({ ...transfer.lastProgress, state: 'cancelling' });
    try {
        if (typeof window.electronAPI?.cancelArtifactDownload !== 'function') {
            transfer.cancelRequested = false;
            renderArtifactTransferProgress(transfer.lastProgress);
            showToast('当前版本暂不支持取消文件传输', 'warning');
            return false;
        }
        await window.electronAPI.cancelArtifactDownload(transfer.transferId);
        return true;
    } catch (error) {
        transfer.cancelRequested = false;
        renderArtifactTransferProgress(transfer.lastProgress);
        showToast('取消下载请求未能发出，请稍后重试', 'warning');
        return false;
    }
}

async function saveNativeArtifactStream(artifactId, suggestedName) {
    if (typeof window.electronAPI?.startArtifactDownload === 'function'
        && typeof window.electronAPI?.onArtifactDownloadProgress === 'function') {
        if (activeArtifactTransfer) {
            showToast('已有一个文件正在下载', 'warning');
            return false;
        }
        const transferId = `artifact-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
        let resolveTransfer;
        const resultPromise = new Promise(resolve => { resolveTransfer = resolve; });
        let resultTimeout = null;
        activeArtifactTransfer = {
            transferId,
            artifactId: String(artifactId),
            suggestedName,
            resolve: resolveTransfer,
            cancelRequested: false,
            lastProgress: { transferId, state: 'starting', receivedBytes: 0, totalBytes: null },
        };
        renderArtifactTransferProgress(activeArtifactTransfer.lastProgress);
        try {
            const started = await window.electronAPI.startArtifactDownload(artifactId, suggestedName, transferId);
            if (!started?.success) {
                if (activeArtifactTransfer?.transferId === transferId) activeArtifactTransfer = null;
                hideArtifactTransferProgress();
                if (started?.reason === 'user-cancelled') {
                    showToast('已取消');
                    return false;
                }
                await showNativeFileDialogError('下载文件失败', started || { reason: 'ipc-error' });
                return false;
            }
            const result = await Promise.race([
                resultPromise,
                new Promise(resolve => {
                    resultTimeout = window.setTimeout(() => resolve({
                        success: false,
                        reason: 'download-timeout',
                        error: '文件传输长时间没有返回结果，请检查任务状态后重试',
                    }), 15 * 60 * 1000);
                }),
            ]);
            if (result?.reason === 'download-timeout') {
                void cancelArtifactTransfer();
                if (activeArtifactTransfer?.transferId === transferId) activeArtifactTransfer = null;
                hideArtifactTransferProgress();
            }
            if (result?.success) {
                showToast('下载成功');
                return true;
            }
            if (result?.reason === 'user-cancelled') {
                showToast('已取消');
                return false;
            }
            await showNativeFileDialogError('下载文件失败', result || { reason: 'download-error' });
            return false;
        } catch (error) {
            console.error('调用流式保存服务失败:', error);
            if (activeArtifactTransfer?.transferId === transferId) activeArtifactTransfer = null;
            hideArtifactTransferProgress();
            await showNativeFileDialogError('下载文件失败', {
                reason: 'ipc-error',
                error: error?.message,
            });
            return false;
        } finally {
            if (resultTimeout) window.clearTimeout(resultTimeout);
        }
    }
    try {
        const result = await window.electronAPI.saveArtifactStream(artifactId, suggestedName);
        if (result?.success) {
            showToast('下载成功');
            return true;
        }
        if (result?.reason === 'user-cancelled') {
            showToast('已取消');
            return false;
        }
        await showNativeFileDialogError('下载文件失败', result || {
            reason: 'ipc-error',
            error: '主进程未返回流式文件操作结果',
        });
        return false;
    } catch (error) {
        console.error('调用流式保存服务失败:', error);
        await showNativeFileDialogError('下载文件失败', {
            reason: 'ipc-error',
            error: error?.message,
        });
        return false;
    }
}

async function saveArtifactBytes(bytes, suggestedName, format = '') {
    if (!(bytes instanceof Uint8Array)) throw new TypeError('Artifact 内容不是字节流');
    if (isElectron) return saveNativeFile(bytes, suggestedName);
    const url = URL.createObjectURL(new Blob([bytes], { type: artifactMime(format) }));
    try {
        const link = document.createElement('a');
        link.href = url;
        link.download = suggestedName;
        link.click();
        showToast('下载成功');
        return true;
    } finally {
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
}

async function hydrateDownloadContext(target, workflowId) {
    if (typeof workflowApi?.getWorkspace !== 'function') {
        return target?.workspace || currentWorkspace || null;
    }
    const workspace = await workflowApi.getWorkspace(workflowId);
    if (target?.mode === 'current'
        && currentSession?.session_id
        && String(currentSession.session_id) !== String(workflowId)) {
        const error = new Error('当前任务已切换');
        error.name = 'AbortError';
        throw error;
    }
    if (!workspace || !Array.isArray(workspace.items) || !Array.isArray(workspace.artifacts)) {
        const error = new Error('任务工作区暂时无法读取');
        error.code = 'WORKSPACE_UNAVAILABLE';
        throw error;
    }

    const files = resultFilesFromArtifacts(workspace.items, workspace.artifacts, workspace);
    target.workspace = workspace;
    target.files = files;
    target.delivery = workspace.delivery || null;
    target.zipAvailable = workspace.delivery?.zip_available === true;
    target.zipArtifactId = workspace.delivery?.zip_artifact_id || null;
    target.stateVersion = Number(workspace.snapshot?.state_version ?? target.stateVersion ?? 0);

    if (target.mode === 'current' && currentSession?.session_id === workflowId) {
        currentWorkspace = workspace;
        workflowStore?.hydrate?.(workspace, { snapshot: workspace.snapshot });
        mergeWorkflowSnapshotIntoSession(workspace.snapshot, currentSession);
        currentSession.delivery = workspace.delivery ? {
            zip_available: workspace.delivery.zip_available === true,
            zip_artifact_id: workspace.delivery.zip_artifact_id || null,
        } : null;
        renderWorkspaceShell(currentWorkspace, currentSession);
    }
    return workspace;
}

async function downloadZip(context = activeResultContext) {
    const target = context || (currentSession ? {
        mode: 'current',
        sessionId: currentSession.session_id,
        workflowId: currentSession.session_id,
        sourceFilename: currentSession.source_filename,
    } : null);
    if (!target) return;
    try {
        const workflowId = target.workflowId || target.sessionId || target.recordId;
        if (!workflowApi || !workflowId) throw new Error('工作流标识缺失');
        // A history-row context may contain a ZIP id from an earlier list
        // refresh. Hydrate the server-owned workspace before selecting bytes;
        // otherwise a concurrent retry in another window could make the row's
        // immutable ZIP stale while the button still downloads it.
        let projectedWorkspace = target.mode === 'current'
            ? (currentWorkspace || target.workspace || null)
            : (target.workspace || null);
        if (typeof workflowApi.getWorkspace === 'function') {
            // Refresh all item, Artifact, and delivery facts together immediately
            // before choosing the bytes. A stale ZIP id or stale filename must
            // never be used just because the result page was left open.
            projectedWorkspace = await hydrateDownloadContext(target, workflowId);
        }
        const projectedDelivery = projectedWorkspace?.delivery
            || (!projectedWorkspace ? target.delivery : null)
            || null;
        const hasAuthoritativeDelivery = Boolean(
            projectedDelivery && typeof projectedDelivery === 'object'
            && ('zip_available' in projectedDelivery || 'zip_artifact_id' in projectedDelivery),
        );
        // A raw Artifact list is not enough to identify the current delivery
        // scope: it can contain immutable ZIPs from an older run or an older
        // subset export.  Only the server-owned workspace projection may
        // select an already-created ZIP; otherwise ask the idempotent export
        // command to derive the current full scope.
        let artifactId = hasAuthoritativeDelivery
            ? (projectedDelivery.zip_available === true ? projectedDelivery.zip_artifact_id : null)
            : (target.mode === 'history' ? null : (target.zipArtifactId || null));
        if (!artifactId) {
            const exportAction = workflowAdapter.action?.(
                projectedWorkspace || (target.mode === 'current' ? currentWorkspace : null),
                'EXPORT_ZIP',
            );
            if (target.mode === 'current' && exportAction?.enabled === true && workflowCommandCoordinator) {
                const outcome = await workflowCommandCoordinator.run(exportAction, {
                    reason: 'desktop-export-zip',
                });
                if (!outcome.ok) {
                    const error = new Error(outcome.reason || 'ZIP 交付操作未完成');
                    error.code = outcome.reason === 'action-disabled-after-refresh'
                        ? 'STATE_CONFLICT'
                        : 'EXPORT_ZIP_FAILED';
                    throw error;
                }
                artifactId = outcome.response?.artifact?.artifact_id
                    || outcome.workspace?.delivery?.zip_artifact_id
                    || null;
            } else {
                const currentSnapshot = target.mode === 'current'
                    ? await refreshCurrentWorkflowSnapshot(currentSession)
                    : await workflowApi.getWorkflow(workflowId);
                const expectedStateVersion = Number(
                    currentSnapshot?.state_version
                    ?? target.stateVersion
                    ?? currentSession?.state_version
                    ?? 0,
                );
                const artifact = await workflowApi.createExportZip(workflowId, {
                    expected_state_version: expectedStateVersion,
                });
                artifactId = artifact?.artifact_id || null;
            }
            if (artifactId) {
                target.zipArtifactId = artifactId;
                target.zipAvailable = true;
            }
        }
        if (!artifactId) {
            showToast('当前工作流没有可下载的 ZIP Artifact');
            return false;
        }
        const downloadName = deliveryZipFilename(target.sourceFilename);
        if (isElectron && typeof window.electronAPI?.saveArtifactStream === 'function') {
            return saveNativeArtifactStream(artifactId, downloadName);
        }
        const bytes = await readArtifactBytes(artifactId);
        return saveArtifactBytes(bytes, downloadName, 'zip');
    } catch (err) {
        console.error('下载 ZIP 异常:', err);
        showToast('下载失败：Artifact 暂时不可用');
        return false;
    }
}

async function downloadFile(fileOrFilename, context = activeResultContext) {
    const target = context || (currentSession ? {
        mode: 'current',
        sessionId: currentSession.session_id,
        workflowId: currentSession.session_id,
        files: generatedFiles,
    } : null);
    const requestedFile = fileOrFilename && typeof fileOrFilename === 'object'
        ? fileOrFilename
        : null;
    const requestedArtifactId = String(requestedFile?.artifact_id || '').trim();
    const requestedFilename = String(
        requestedFile?.filename || fileOrFilename || '',
    ).trim();
    if (!target || (!requestedArtifactId && !requestedFilename)) return;
    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        const workflowId = target.workflowId || target.sessionId || target.recordId;
        if (workflowId && typeof workflowApi.getWorkspace === 'function') {
            await hydrateDownloadContext(target, workflowId);
        }
        const file = (Array.isArray(target.files) ? target.files : []).find(item => (
            requestedArtifactId
                ? String(item?.artifact_id || '') === requestedArtifactId
                : item?.filename === requestedFilename
        ));
        if (!file?.artifact_id) {
            showToast('这条音频已被更新或暂未通过核验，请刷新任务后重试', 'warning');
            return false;
        }
        const filename = filenameWithExtension(
            file.filename || requestedFilename,
            file.format || 'mp3',
            '音频文件',
        );
        if (isElectron && typeof window.electronAPI?.saveArtifactStream === 'function') {
            return saveNativeArtifactStream(file.artifact_id, filename);
        }
        const bytes = await readArtifactBytes(file.artifact_id);
        return saveArtifactBytes(bytes, filename, file.format || String(filename).split('.').pop());
    } catch (err) {
        console.error('下载音频异常:', err);
        showToast('下载失败：Artifact 暂时不可用');
        return false;
    }
}

// ============================================================================
// 重新开始
// ============================================================================

function resetGenerateState() {
    clearGenerationStartupTimer();
    setProgressIndeterminate(false);
    isGenerating = false;
    if (!cancelWorkflowPromise) generationCancelRequested = false;
    syncRestartButtonState();
    const historyNav = $('history-nav-btn');
    if (historyNav) historyNav.disabled = isRestarting || isParsing;
    updateGenerationCancelUI();
}

async function requestRestart() {
    if (isRestarting) return;
    if (currentSession) {
        let confirmation = {
            kicker: '当前任务',
            title: '更换当前文档？',
            message: '当前文档会话将结束，随后可以导入新的文档。',
            detail: '尚未保存到历史记录的临时结果会被清理。',
            tone: 'warning',
            confirmLabel: '更换文档',
        };
        if (isGenerating) {
            confirmation = {
                kicker: '生成任务进行中',
                title: '中止并新建任务？',
                message: '当前音频仍在生成，新建任务会立即中止本次处理。',
                detail: '本次尚未完成的结果会被清理，此操作无法撤销。',
                tone: 'danger',
                confirmLabel: '中止并新建',
            };
        } else if (generatedFiles.length > 0 || currentStep === 4) {
            confirmation = latestCurrentResultEvent?.workflow_id
                ? {
                    kicker: '结果已保存',
                    title: '开始一个新任务？',
                    message: '本次结果已经保存在历史记录中，可以安全开始新任务。',
                    tone: 'info',
                    confirmLabel: '开始新任务',
                }
                : {
                    kicker: '结果尚未保存',
                    title: '仍要开始新任务？',
                    message: '本次结果未能保存到历史记录，新建任务会清理当前结果。',
                    detail: '请先确认需要的音频已经下载到本机。',
                    tone: 'danger',
                    confirmLabel: '清理并新建',
                };
        }
        if (!await showConfirmDialog(confirmation)) return;
    }
    setRestartingUI(true);
    try {
        await restart();
    } finally {
        setRestartingUI(false);
        setAppInteractive($('service-state')?.classList.contains('is-ready') === true);
    }
}

async function restart({ notify = true } = {}) {
    destroyWaveSurfers();
    if (activeArtifactTransfer) {
        await cancelArtifactTransfer();
        activeArtifactTransfer = null;
        hideArtifactTransferProgress();
    }
    // 先让所有在途异步回调失效，避免清理请求期间旧任务重新接管页面。
    parseAttemptId++;
    const sourceImportToAbort = sourceImportId;
    const sourceUploadToAbort = sourceStagingUploadId;
    sourceImportController?.abort();
    sourceImportController = null;
    sourceImportInFlight = false;
    sourceImportId = null;
    sourceStagingUploadId = null;
    sourceTransportUploadId = null;
    if (sourceUploadToAbort && typeof window.electronAPI?.sourceUpload?.abort === 'function') {
        await window.electronAPI.sourceUpload.abort(sourceUploadToAbort).catch(() => {});
    }
    if (sourceImportToAbort) await abortSourceImportIfPossible(sourceImportToAbort, 'desktop-restart');
    if (parseAbortController) {
        parseAbortController.abort();
        parseAbortController = null;
    }
    isParsing = false;
    generationAttemptId++;
    if (generateAbortController) {
        generateAbortController.abort();
        generateAbortController = null;
    }
    if (resultNavigationTimer) {
        clearTimeout(resultNavigationTimer);
        resultNavigationTimer = null;
    }
    clearSSEReconnectTimer();
    clearGenerationStartupTimer();
    sseConnectionToken++;

    // 断开 SSE
    if (workflowStream) {
        workflowStream.close().catch(() => {});
        workflowStream = null;
    }

    const sessionToCleanup = currentSession;
    let cleanupConfirmed = true;
    // 取消接口现在会立即完成本地终态。先完成这一次本地取消，再清空
    // renderer 会话，避免“旧任务还在后台、页面却已经新建任务”的竞态。
    if (sessionToCleanup) {
        try {
            const snapshot = await cancelCurrentWorkflow(sessionToCleanup, {
                reason: 'desktop-restart',
            });
            cleanupConfirmed = isCancellationSettledSnapshot(snapshot);
        } catch (error) {
            console.error('任务取消失败:', error);
            cleanupConfirmed = false;
        }
        if (!cleanupConfirmed) {
            setServiceState('warning', '任务停止失败');
            $('retry-service-btn').hidden = false;
            $('status-text').textContent = '任务停止失败，请重试停止后再开始新任务';
            return false;
        }
    }
    currentSession = null;
    isGenerating = false;

    // 重置状态
    generatedFiles = [];
    currentWorkspace = null;
    activeWorkspace = 'import';
    clearTimeout(workspaceRefreshTimer);
    workspaceRefreshTimer = null;
    activeResultContext = null;
    latestCurrentResultEvent = null;
    historyRequestToken++;
    logEntryCount = 0;
    lastStats = null;
    lastDownloadEvent = null;
    sseRetryCount = 0;
    generationResult = null;
    transientGenerationErrorMessage = '';
    lastGenerationConfig = null;
    resetTaskVoiceConfiguration();

    // 重置 Step 1
    const uploadZone = $('upload-zone');
    uploadZone.classList.remove('has-file', 'has-error', 'is-processing', 'dragover');
    uploadZone.setAttribute('aria-busy', 'false');
    uploadZone.querySelector('.upload-text-large').textContent = '拖拽文档到这里，或点击选择';
    uploadZone.querySelector('.upload-hint').textContent = '支持 .docx / .xlsx 文件 · 选择后会自动解析';
    setUploadFeedback();
    updateSourceImportProgress();
    setUploadParsing(false);
    updateSessionLabels();

    // 刷新预设列表（可能在上一次操作中保存了新配置）
    refreshPresetUI();

    // 重置 Step 3
    setProgressBarPercent(0);
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', '0');
    $('progress-bar').parentElement?.setAttribute('aria-valuetext', '0% 处理中');
    setProgressReadoutMode(false);
    setProgressIndeterminate(false);
    $('progress-stats').textContent = '准备中...';
    $('progress-percent').textContent = '0';
    $('progress-completed-label').textContent = '已完成';
    $('progress-completed').textContent = '0';
    $('progress-remaining').textContent = '—';
    $('progress-failed').textContent = '0';
    if ($('progress-cancelled')) $('progress-cancelled').textContent = '0';
    if ($('progress-skipped')) $('progress-skipped').textContent = '0';
    if ($('progress-deliverable')) $('progress-deliverable').textContent = '0 / 0';
    $('gen-title').textContent = '正在生成音频';
    setGenerationVisualState('running');
    hideGenerationRecovery();
    resetLogTimeline('任务开始后，这里会按阶段展示详细处理记录。');
    $('type-stats').innerHTML = '';

    // 重置 Step 4
    $('audio-list').innerHTML = '<div class="audio-empty">暂无音频文件</div>';
    $('audio-count').textContent = '0 个文件';
    prepareAudioFilters([]);
    $('audio-filter-empty').hidden = true;
    document.querySelector('.audio-list-section').hidden = false;
    $('result-summary').textContent = '';
    $('result-success-label').textContent = '已生成';
    $('result-success-count').textContent = '0';
    $('result-success-caption').textContent = '音频文件';
    $('result-secondary-label').textContent = '未完成';
    $('result-failed-count').textContent = '0';
    if ($('result-cancelled-count')) $('result-cancelled-count').textContent = '0';
    $('result-secondary-caption').textContent = '待处理内容';
    $('result-format-value').textContent = 'MP3';
    $('result-hero').classList.remove('has-no-package');
    $('generate-full-btn').hidden = true;
    $('rerun-task-btn')?.setAttribute('hidden', 'hidden');
    $('back-to-history-btn').hidden = true;
    $('result-warning').hidden = true;
    $('zip-card').style.removeProperty('display');
    $('delivery-scope').textContent = '等待交付范围核验';
    $('delivery-exclusion-note').hidden = true;
    $('delivery-exclusion-note').textContent = '';
    $('delivery-exclusion-list')?.replaceChildren();
    if ($('delivery-exclusion-list')) $('delivery-exclusion-list').hidden = true;
    $('artifact-transfer')?.setAttribute('hidden', 'hidden');
    if ($('artifact-transfer-bar')) $('artifact-transfer-bar').value = 0;
    if ($('artifact-transfer-value')) $('artifact-transfer-value').textContent = '0%';
    $('download-zip-btn').disabled = false;
    $('result-failure-list').innerHTML = '';
    $('retry-failed-btn').hidden = true;
    $('result-eyebrow').textContent = '任务已完成';
    $('result-title').textContent = '音频已经准备好了';
    document.querySelector('.result-success-icon')?.classList.remove('has-warning', 'has-error');

    // 重置状态栏
    $('status-text').textContent = cleanupConfirmed ? '就绪' : '请重新连接生成服务';
    $('stats-bar').innerHTML = '';

    // 回到首页
    goToStep(1);
    await refreshHistoryRecords({ showLoading: false });
    if (notify) {
        showToast(cleanupConfirmed
            ? '已重置，可以开始新任务'
            : '当前任务已关闭，请重新连接生成服务后继续');
    }
    return cleanupConfirmed;
}

// ============================================================================
// Toast
// ============================================================================

let toastTimer = null;
function showToast(msg, tone = 'info') {
    let toast = document.querySelector('.toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.className = 'toast';
        document.body.appendChild(toast);
    }
    toast.classList.remove('is-info', 'is-success', 'is-warning', 'is-error');
    toast.classList.add(`is-${tone}`);
    toast.setAttribute('role', tone === 'error' ? 'alert' : 'status');
    toast.setAttribute('aria-live', tone === 'error' ? 'assertive' : 'polite');
    toast.textContent = msg;
    toast.classList.add('show');

    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        toast.classList.remove('show');
    }, tone === 'error' ? 5200 : 2800);
}

// ============================================================================
// Store 订阅式进度投影（T8）
// ============================================================================

/**
 * 进度 UI 的权威数值来自 workflowStore 的 workspace 投影：只有被 Store
 * 接受（顺序校验、去重）的 snapshot/event 才会推进这里的渲染。断线重连、
 * 快照重同步或补齐后，订阅回调会自动把界面拉回投影的最新值，不再依赖
 * “逐个事件各写一次 DOM”的时序。
 */
let lastWorkspaceRenderKey = '';
function renderWorkflowWorkspace(storeState) {
    const workspace = storeState?.workspace;
    if (!workspace || !isGenerating || generationResult) return;
    const shellWorkspace = storeState?.workspaceData || currentWorkspace || {};
    const shellSnapshot = storeState?.workflowProjection || shellWorkspace.snapshot || currentSession;
    const authoritativeShellWorkspace = shellSnapshot
        ? { ...shellWorkspace, snapshot: shellSnapshot }
        : shellWorkspace;
    const shellState = workspaceUserState(authoritativeShellWorkspace, shellSnapshot);
    const controlState = String(
        shellSnapshot?.control_state
        || workspace.controlState
        || shellWorkspace?.snapshot?.control_state
        || '',
    ).toUpperCase();
    if (generationWorkflowOwnsRuntimeView(shellWorkspace, shellSnapshot)
        || GENERATION_RUNTIME_FROZEN_CONTROL_STATES.has(controlState)) {
        renderGenerationViewState(authoritativeShellWorkspace, shellState, workspaceProgress(authoritativeShellWorkspace));
        return;
    }
    const phase = String(workspace.phase || '');
    const message = String(workspace.runtime?.message || '');
    const segments = workspace.segments || {};
    const itemProgress = workspaceProgress(authoritativeShellWorkspace);
    const itemTotal = generationProgressTotal(itemProgress, authoritativeShellWorkspace);
    const itemCompleted = Math.max(0, Math.min(
        itemTotal || Number.MAX_SAFE_INTEGER,
        Math.round(Number(itemProgress?.completed) || 0),
    ));
    const itemPercent = itemTotal > 0
        ? Math.min(99, Math.round((itemCompleted / itemTotal) * 100))
        : 0;
    const itemIssues = generationProgressIssueSummary(itemProgress);
    const hasSegments = Number(segments.total) > 0;
    const completed = hasSegments ? Math.min(Math.max(0, Number(segments.completed) || 0), Number(segments.total)) : 0;
    const renderKey = [
        phase,
        message,
        hasSegments ? `${completed}/${segments.total}` : 'none',
        `${itemCompleted}/${itemTotal}`,
        itemIssues,
        String(workspace.executionState ?? ''),
        controlState,
        String(workspace.resultStatus ?? ''),
    ].join('|');
    if (renderKey === lastWorkspaceRenderKey) return;
    lastWorkspaceRenderKey = renderKey;

    if (hasSegments) {
        const percent = Math.min(100, Math.round((completed / Number(segments.total)) * 100));
        setProgressBarPercent(percent);
        $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(percent));
        $('progress-bar').parentElement?.setAttribute('aria-valuetext', `${percent}% 处理中`);
        $('progress-percent').textContent = String(percent);
        setProgressIndeterminate(false);
        $('progress-stats').textContent = `${message || '讯飞浏览器处理中'} · ${completed} / ${segments.total}`;
    } else if (phase && phase !== 'attention') {
        // 分段计数还没产生（浏览器启动/准备阶段）：保持不确定进度，只
        // 同步阶段文案。这里必须同时重置百分比；否则一次失败后恢复
        // 时会把旧快照的 99% 留在进度条上，造成“脚本已运行但界面不动”
        // 的假象。
        setProgressBarPercent(itemPercent);
        $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(itemPercent));
        $('progress-bar').parentElement?.setAttribute('aria-valuetext', `${itemPercent}% 处理中`);
        $('progress-percent').textContent = String(itemPercent);
        setProgressIndeterminate(true);
        const count = `${itemCompleted} / ${itemTotal || '—'}`;
        $('progress-stats').textContent = `${message || '正在准备生成任务'} · ${count}${itemIssues ? ` · ${itemIssues}` : ''}`;
    }
    if (message && $('generation-live-status')?.textContent !== message) {
        $('generation-live-status').textContent = message;
        $('status-text').textContent = message;
    }
}

function renderWorkflowStoreState(storeState) {
    const storedWorkspace = storeState?.workspaceData;
    const workflowId = String(storeState?.workflowId || '');
    const activeId = String(currentSession?.session_id || '');
    if (storedWorkspace && workflowId && workflowId === activeId) {
        // Keep the rich server workspace as the source of item/delivery facts,
        // while letting the ordered event projection update the shell's status
        // immediately between scheduled full workspace hydrations.
        const projectedSnapshot = storeState.workflowProjection || storedWorkspace.snapshot;
        currentWorkspace = {
            ...(currentWorkspace || {}),
            ...storedWorkspace,
            snapshot: projectedSnapshot ? { ...projectedSnapshot } : null,
        };
        renderWorkspaceShell(currentWorkspace, currentWorkspace.snapshot || currentSession);
    }
    renderWorkflowWorkspace(storeState);
}

if (workflowStore && typeof workflowStore.subscribe === 'function') {
    workflowStore.subscribe(renderWorkflowStoreState);
}

// ============================================================================
// 启动
// ============================================================================

init();
