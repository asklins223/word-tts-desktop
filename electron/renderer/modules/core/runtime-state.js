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
let currentView = 'workflow';    // 'workflow' | 'history' | 'history-result' | 'system-input-progress' | 'final-delivery' | 'version'
let activeWorkspace = 'import';  // import | review | voice | generation | delivery
let historyReturnStep = 1;       // 从历史中心返回工作流时恢复原步骤
let historyRecords = [];
let historyFilters = { query: '', status: 'all', sort: 'updated' };
let historyRequestToken = 0;     // 使较早的历史列表/详情请求失效
let activeResultContext = null;  // 当前交付页对应当前任务或历史记录
let latestCurrentResultEvent = null; // 从历史详情返回时恢复当前任务的交付页
let currentSession = null;       // { session_id, source_filename, source_artifact_id, parse_results }
let currentWorkspace = null;     // server-owned workspace projection for the active task
let systemInputPanelReady = false;
let systemInputRegionTree = [];
let systemInputRegionIndexes = { provinces: [], cities: [], districts: [] };
// Region data is a bundled read-only snapshot. Keep its lifecycle explicit so
// the target editor can distinguish “still loading” from “loaded but empty”
// and repaint an already-open modal when the async read finishes.
let systemInputRegionLoadState = 'idle'; // idle | loading | ready | error
let systemInputRegionLoadError = '';
let systemInputRegionLoadPromise = null;
let systemInputTemplates = [];
let systemInputTemplatesType = '';
let systemInputTemplateRequestId = 0;
let systemInputPlatformTemplates = [];
let systemInputPlatformTemplatesType = '';
let systemInputPlatformTemplateRequestId = 0;
let systemInputPlatformTemplateCatalog = null;
let systemInputPlatformTemplateCatalogSync = {
    sync_id: null,
    status: 'IDLE',
    message: '尚未同步平台模板目录',
    record_count: 0,
};
let systemInputPlatformTemplateCatalogPollTimer = null;
let systemInputPlatformTemplateCatalogSyncBusy = false;
let systemInputPlatformTemplatePendingSelection = null;
let systemInputDeliveryModeDraft = '';
let systemInputDeliveryModeBusy = false;
let systemInputBoundaryBusy = false;
let systemInputDistrictSelections = [];
let systemInputPickerOpen = null;
let systemInputPickerDocumentBound = false;
let systemInputPickerRegistry = new Map();
let systemInputUnitDrafts = new Map();
let systemInputSelectedUnitId = '';
let systemInputDrawerWorkflowId = '';
let systemInputConfigurationBusy = false;
let systemInputTemplateBusy = false;
let systemInputReconciliationBusy = false;
let systemInputVerificationBusy = false;
let systemInputRunControlBusy = false;
let systemInputTextbookCatalog = null;
let systemInputTextbookCatalogSync = {
    sync_id: null,
    status: 'IDLE',
    message: '尚未同步教材目录',
    record_count: 0,
};
let systemInputTextbookCatalogRequestId = 0;
let systemInputTextbookCatalogPollTimer = null;
let systemInputTextbookCatalogSyncBusy = false;
const systemInputAutoVerifiedRuns = new Set();
let systemInputLastProvinceValue = '';
let systemInputLastCityValue = '';
let systemInputDrawerPreviousFocus = null;
let systemInputDrawerDraft = null;
let systemInputDrawerDraftWorkflowId = '';
let activeWorkflowCandidates = [];
let activeWorkflowListTruncated = false;
let workspaceRefreshTimer = null;
let workspaceRefreshInFlight = null;
let systemInputRefreshTimer = null;
let systemInputRefreshInFlight = false;
const SYSTEM_INPUT_SUBPAGE_VIEWS = new Set(['system-input-progress', 'final-delivery']);
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
let reviewOutlineExpansion = new Map();
let reviewSelectedItemId = '';
let reviewSelectedIndex = null;
let reviewViewMode = 'document';
const REVIEW_RENDER_LIMIT = 500;

function resetReviewNavigationState() {
    reviewOutlineExpansion.clear();
    reviewSelectedItemId = '';
    reviewSelectedIndex = null;
    reviewViewMode = 'document';
}

const DEFAULT_FEMALE_ROLE_KEY = '__default_female__';
const DEFAULT_MALE_ROLE_KEY = '__default_male__';
const ROLE_CONFIG_PREFIX = 'role:';
const DEFAULT_VOICE_PARAMS = { rate: 50, volume: 50, pitch: 50 };
const DEFAULT_FEMALE_VOICE_PARAMS = { rate: 50, volume: 50, pitch: 50 };
const DEFAULT_MALE_VOICE_PARAMS = { rate: 35, volume: 50, pitch: 50 };
const QUESTION_STEM_ROLE_LABEL = '题干音色';
const QUESTION_STEM_ROLE_KEY = QUESTION_STEM_ROLE_LABEL.toLocaleLowerCase('zh-CN');
const QUESTION_STEM_ROLE_CONFIG_KEY = `${ROLE_CONFIG_PREFIX}${QUESTION_STEM_ROLE_KEY}`;
const QUESTION_STEM_VOICE_KEY = 'common:10000023'; // 晓燕
const QUESTION_STEM_VOICE_PARAMS = { rate: 40, volume: 50, pitch: 50 };
const GENERATION_MODE_COMPOSITE = 'composite_cut';
const GENERATION_MODE_SINGLE = 'single_segment';
const DEFAULT_GENERATION_MODE = GENERATION_MODE_COMPOSITE;
const GENERATION_MODE_LABELS = {
    [GENERATION_MODE_COMPOSITE]: '全部生成后切割',
    [GENERATION_MODE_SINGLE]: '单条单条生成',
};
const SYSTEM_INPUT_STAGES = [
    { id: 1, name: '小学' },
    { id: 2, name: '初中' },
    { id: 3, name: '高中' },
];
const SYSTEM_INPUT_GRADES = [
    { id: 1, name: '一年级', stageId: 1 },
    { id: 2, name: '二年级', stageId: 1 },
    { id: 3, name: '三年级', stageId: 1 },
    { id: 4, name: '四年级', stageId: 1 },
    { id: 5, name: '五年级', stageId: 1 },
    { id: 6, name: '六年级', stageId: 1 },
    { id: 7, name: '七年级', stageId: 2 },
    { id: 8, name: '八年级', stageId: 2 },
    { id: 9, name: '九年级', stageId: 2 },
    { id: 10, name: '高一', stageId: 3 },
    { id: 11, name: '高二', stageId: 3 },
    { id: 12, name: '高三', stageId: 3 },
];
const SYSTEM_INPUT_PAPER_TYPES = [
    { id: 1, name: '阶段测试题' },
    { id: 2, name: '期末模拟题' },
    { id: 3, name: '单元测试题' },
];
const SYSTEM_INPUT_PAPER_CATEGORIES = [
    { id: 'special', name: '题型专项' },
    { id: 'listening', name: '听说考试' },
];
const SYSTEM_INPUT_TEXTBOOK_FORMS = [
    { id: 'synced', name: '同步课文' },
    { id: 'roleplay', name: '角色扮演' },
];
const SYSTEM_INPUT_DEFAULT_YEAR = 2026;
const SYSTEM_INPUT_DEFAULT_ANSWER_TIME = 20;
const SYSTEM_INPUT_PAPER_DEFAULT_ANSWER_TIME = 60;
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
// 服务启动时也接收用户拖入的 File；这里只保留浏览器的文件引用，不读取
// 内容，待服务连接完成后仍复用下面已有的受控流式导入链路。
let pendingServiceSourceFile = null;
let sourceImportServiceState = 'connecting'; // connecting | ready | unavailable
let pendingServiceSourceImportScheduled = false;
let serviceConnectionAttemptId = 0;
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
                if (typeof renderWorkspaceAfterHydrate === 'function') {
                    renderWorkspaceAfterHydrate(workspace, workspace.snapshot, currentSession.session_id);
                } else {
                    renderWorkspaceShell(workspace, workspace.snapshot);
                }
            }
        },
    })
    : null;


// Cross-feature lifecycle flags live in the runtime layer so feature
// modules can be loaded independently without duplicating mutable state.
let isParsing = false;
let toastTimer = null;
let lastWorkspaceRenderKey = '';
