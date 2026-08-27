/**
 * 小猪wordTTS — Frontend Logic v2
 * =================================
 * 四步向导式流程：上传 → 配置 → 生成 → 结果
 */

// ============================================================================
// 常量 & 全局状态
// ============================================================================

const isElectron = typeof window.electronAPI !== 'undefined';
const platform = isElectron ? window.electronAPI.platform : 'web';
const backendConfig = isElectron ? window.electronAPI.backend : null;
const API_BASE = backendConfig?.url || 'http://127.0.0.1:7863';
const API_TOKEN = backendConfig?.token || '';
const PRODUCT_NAME = '小猪wordTTS';

function apiUrl(path) {
    if (!API_TOKEN) return `${API_BASE}${path}`;
    const separator = path.includes('?') ? '&' : '?';
    return `${API_BASE}${path}${separator}token=${encodeURIComponent(API_TOKEN)}`;
}

let currentStep = 1;
let currentView = 'workflow';    // 'workflow' | 'history' | 'history-result'
let historyReturnStep = 1;       // 从历史中心返回工作流时恢复原步骤
let historyRecords = [];
let historyRequestToken = 0;     // 使较早的历史列表/详情请求失效
let activeResultContext = null;  // 当前交付页对应当前任务或历史记录
let latestCurrentResultEvent = null; // 从历史详情返回时恢复当前任务的交付页
let currentSession = null;       // { session_id, source_filename, file_path, parse_results }
let currentConfig = null;        // API 返回的配置
let desktopSettings = null;      // Electron 主进程 settings.json 的只读快照
let settingsMutationToken = 0;   // 使过期的设置写入回调不能覆盖较新的本地状态
let windowState = null;           // 主进程确认的窗口状态
let privacyModeActive = false;    // 仅表示用户是否进入防偷窥动作
let contentDraft = null;          // 当前任务的可编辑解析结果副本
let contentEditorDirty = false;
let taskControlState = 'idle';
let browserState = {
    visibility: 'unavailable',
    permission_required: false,
    last_error: '自动化浏览器尚未启动',
};
let controlActionInFlight = false;
let clientConfigInitialized = false; // 防止连接重试时用服务端默认值覆盖用户当前设置
let voiceCatalog = [
    { key: 'amanda', name: 'Amanda', gender: 'female', gender_label: '女声', language: ['英语'], tags: ['英语'], categories: ['女声', '英语'] },
    { key: 'george', name: 'George', gender: 'male', gender_label: '男声', language: ['英语'], tags: ['英语'], categories: ['男声', '英语'] },
];
let voiceCatalogMode = null;
let voiceFilterOptions = [];
let activeVoiceFilter = 'all';
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
let eventSource = null;          // SSE 连接
let sseReconnectTimer = null;    // SSE 延迟重连计时器
let sseStableTimer = null;       // 连接稳定后重置累计重试次数
let sseConnectionToken = 0;      // 使旧连接回调失效
let isGenerating = false;
let parseAbortController = null; // 当前文档解析请求
let parseAttemptId = 0;          // 使已取消的解析响应失效
let generateAbortController = null; // 当前生成启动请求
let generationAttemptId = 0;        // 使旧生成任务回调失效
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
let lastGenerationConfig = null;  // 最近一次实际提交的配置（用于试听后继续生成全部）
let wavesurferInstances = [];    // 波形仅负责可视化与定位，播放由原生 Audio 优先处理
let audioElements = [];          // 结果页原生音频元素（支持无需等待波形解码即可播放）
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
let pendingCleanupSessionId = null; // 未确认清理完成前禁止创建同名新任务

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
    const normalized = normalizePersistedConfig(config);
    if (isElectron && desktopSettings) {
        const previousSettings = desktopSettings;
        desktopSettings = {
            ...desktopSettings,
            tts: { ...(desktopSettings.tts || {}), current_config: normalized },
        };
        void persistDesktopSettingsPatch(
            { tts: { current_config: normalized } },
            previousSettings,
            '保存桌面配置失败',
        );
        return true;
    }
    try {
        localStorage.setItem(CURRENT_CONFIG_STORAGE_KEY, JSON.stringify(normalized));
        return true;
    } catch (e) {
        console.error('保存当前配置失败:', e);
        return false;
    }
}

function loadCurrentConfig() {
    if (isElectron && desktopSettings) {
        const config = desktopSettings.tts?.current_config;
        return config && typeof config === 'object' && !Array.isArray(config)
            ? normalizePersistedConfig(config)
            : null;
    }
    try {
        const raw = localStorage.getItem(CURRENT_CONFIG_STORAGE_KEY);
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

async function persistDesktopSettingsPatch(patch, previousSettings, failureMessage) {
    const token = ++settingsMutationToken;
    try {
        const result = await window.electronAPI.settings.update(patch);
        if (token !== settingsMutationToken) return result;
        if (result?.success && result.settings) {
            desktopSettings = result.settings;
            return result;
        }
        throw new Error(result?.reason || failureMessage);
    } catch (error) {
        if (token !== settingsMutationToken) return { success: false, reason: error.message };
        // 主进程写盘失败时重新读取主进程快照，避免把渲染器的乐观值
        // 留在内存里；读取失败才退回调用前的快照。
        try {
            const current = await window.electronAPI.settings.get();
            if (current?.success && current.settings) desktopSettings = current.settings;
            else desktopSettings = previousSettings;
        } catch (_) {
            desktopSettings = previousSettings;
        }
        console.error(`${failureMessage}:`, error);
        showToast(failureMessage, 'warning');
        return { success: false, reason: error.message };
    }
}

/**
 * 从 localStorage 读取所有预设。
 */
function loadPresets() {
    if (isElectron && desktopSettings) {
        const presets = desktopSettings.tts?.presets;
        return Array.isArray(presets)
            ? presets
                .filter(p => p && typeof p === 'object' && p.id && p.name)
                .map(p => ({ ...p, config: normalizePersistedConfig(p.config) }))
            : [];
    }
    try {
        const raw = localStorage.getItem(PRESET_STORAGE_KEY);
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
                localStorage.setItem(PRESET_STORAGE_KEY, JSON.stringify(sanitized));
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
    const sanitized = Array.isArray(presets)
        ? presets.map(p => ({ ...p, config: normalizePersistedConfig(p?.config) }))
        : [];
    if (isElectron && desktopSettings) {
        const previousSettings = desktopSettings;
        desktopSettings = {
            ...desktopSettings,
            tts: { ...(desktopSettings.tts || {}), presets: sanitized },
        };
        void persistDesktopSettingsPatch(
            { tts: { presets: sanitized } },
            previousSettings,
            '保存桌面预设失败',
        );
        return true;
    }
    try {
        localStorage.setItem(PRESET_STORAGE_KEY, JSON.stringify(sanitized));
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

function bindNativeAppNotices() {
    if (!isElectron || typeof window.electronAPI?.onAppNotice !== 'function') return;
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
 * 渲染 Step 1 的预设列表。
 */
function renderStep1Presets() {
    const container = $('step1-preset-list');
    if (!container) return;
    const presets = loadPresets();
    container.innerHTML = '';

    if (presets.length === 0) {
        container.innerHTML = '<div class="preset-empty">暂无保存的配置，请在第二步保存</div>';
        return;
    }

    presets.forEach(p => {
        const card = document.createElement('div');
        card.className = 'preset-card';

        const info = document.createElement('div');
        info.className = 'preset-card-info';

        const name = document.createElement('div');
        name.className = 'preset-card-name';
        name.textContent = p.name;

        const desc = document.createElement('div');
        desc.className = 'preset-card-desc';
        desc.textContent = presetSummary(p.config);

        info.appendChild(name);
        info.appendChild(desc);

        const goBtn = document.createElement('button');
        goBtn.className = 'preset-card-go';
        goBtn.title = '使用此配置直接生成';
        goBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>';
        goBtn.addEventListener('click', () => {
            if (!currentSession) {
                showToast('请先上传文档');
                return;
            }
            goToStep(3);
            startProcessing(false, p.config);
        });

        card.appendChild(info);
        card.appendChild(goBtn);
        container.appendChild(card);
    });
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
    if (Number.isNaN(n)) return 50;
    return Math.min(100, Math.max(0, n));
}

function normalizeVoiceKey(value, fallback = '') {
    const key = String(value ?? '').trim();
    return key ? key.slice(0, 160) : fallback;
}

function normalizeRoleKeyClient(value) {
    return String(value ?? '').trim().replace(/\s+/g, ' ').toLocaleLowerCase('zh-CN').slice(0, 80);
}

function createDefaultVoiceParams() {
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

function catalogDataForMode(mode) {
    const normalizedMode = normalizeGenerationMode(mode);
    const composite = normalizedMode === GENERATION_MODE_COMPOSITE;
    const entries = composite ? currentConfig?.composite_voices : currentConfig?.voices;
    const filters = composite ? currentConfig?.composite_voice_filters : currentConfig?.voice_filters;
    // 兼容旧后端或离线缓存：没有双目录字段时继续使用原 voices。
    return {
        entries: Array.isArray(entries) && entries.length
            ? entries
            : (Array.isArray(currentConfig?.voices) ? currentConfig.voices : []),
        filters: Array.isArray(filters) && filters.length
            ? filters
            : (Array.isArray(currentConfig?.voice_filters) ? currentConfig.voice_filters : []),
    };
}

function catalogEntryForKey(key, mode = selectedGenerationMode()) {
    const normalizedKey = normalizeVoiceKey(key);
    if (!normalizedKey) return null;
    const { entries } = catalogDataForMode(mode);
    const normalizedEntries = entries.map(normalizeVoiceEntry);
    // 先精确匹配变体 key，再尝试复合音色的别名。否则同一组里的第一条
    // 变体会抢先匹配到另一条变体的 variant_keys，切换模式时就会串音色。
    return normalizedEntries.find(voice => voice.key === normalizedKey)
        || normalizedEntries.find(voice => (
            String(voice.name || '').trim().toLocaleLowerCase('zh-CN')
                === normalizedKey.toLocaleLowerCase('zh-CN')
            || String(voice.composite_key || '').trim() === normalizedKey
            || (Array.isArray(voice.variant_keys) && voice.variant_keys.includes(normalizedKey))
            || (Array.isArray(voice.variant_names) && voice.variant_names.some(name => (
                String(name).trim().toLocaleLowerCase('zh-CN')
                    === normalizedKey.toLocaleLowerCase('zh-CN')
            )))
        )) || null;
}

function resolveVoiceKeyForMode(key, mode = selectedGenerationMode()) {
    const normalizedKey = normalizeVoiceKey(key);
    if (!normalizedKey) return '';
    const entry = catalogEntryForKey(normalizedKey, mode);
    if (!entry) return normalizedKey;
    if (normalizeGenerationMode(mode) === GENERATION_MODE_COMPOSITE) {
        const variantKeys = Array.isArray(entry.variant_keys) ? entry.variant_keys : [];
        const variantNames = Array.isArray(entry.variant_names) ? entry.variant_names : [];
        const normalizedInput = normalizedKey.toLocaleLowerCase('zh-CN');
        const variantIndex = variantKeys.findIndex(item => normalizeVoiceKey(item) === normalizedKey);
        if (variantIndex >= 0) return normalizeVoiceKey(variantKeys[variantIndex], normalizedKey);
        const nameIndex = variantNames.findIndex(item => (
            String(item).trim().toLocaleLowerCase('zh-CN') === normalizedInput
        ));
        if (nameIndex >= 0 && variantKeys[nameIndex]) {
            return normalizeVoiceKey(variantKeys[nameIndex], normalizedKey);
        }
        return normalizeVoiceKey(entry.composite_key || entry.key, normalizedKey);
    }
    return normalizeVoiceKey(entry.key, normalizedKey);
}

function migrateVoiceSelectionsForMode(mode) {
    selectedDefaultFemaleVoice = resolveVoiceKeyForMode(selectedDefaultFemaleVoice, mode) || 'amanda';
    selectedDefaultMaleVoice = resolveVoiceKeyForMode(selectedDefaultMaleVoice, mode) || 'george';
    roleVoiceMap = Object.fromEntries(
        Object.entries(roleVoiceMap || {}).map(([role, key]) => [
            role,
            resolveVoiceKeyForMode(key, mode),
        ]),
    );
}

function setVoiceCatalogForMode(mode, { render = false } = {}) {
    const normalizedMode = normalizeGenerationMode(mode);
    const { entries, filters } = catalogDataForMode(normalizedMode);
    migrateVoiceSelectionsForMode(normalizedMode);
    setVoiceCatalog(entries, filters, normalizedMode);
    if (render) renderVoiceWorkspace();
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
    if (currentConfig && voiceCatalogMode !== normalizedMode) {
        setVoiceCatalogForMode(normalizedMode, { render: true });
    }
}

function normalizeClientConfig(config = {}) {
    const raw = config && typeof config === 'object' ? config : {};
    const generationMode = normalizeGenerationMode(raw.generation_mode ?? DEFAULT_GENERATION_MODE);
    const formats = ['mp3'];
    const qualities = [
        '48 kbps（低）',
        '128 kbps（标准）',
        '192 kbps（高）',
        '320 kbps（极高）',
    ];
    const baseParams = normalizeVoiceParams(raw);
    const defaultFemaleVoice = normalizeVoiceKey(
        resolveVoiceKeyForMode(
            raw.default_female_voice || currentConfig?.default_female_voice,
            generationMode,
        ),
        'amanda',
    );
    const defaultMaleVoice = normalizeVoiceKey(
        resolveVoiceKeyForMode(
            raw.default_male_voice || currentConfig?.default_male_voice,
            generationMode,
        ),
        'george',
    );
    const normalizedVoiceConfigs = {};
    const rawVoiceConfigs = raw.voice_configs && typeof raw.voice_configs === 'object'
        ? raw.voice_configs
        : {};
    Object.entries(rawVoiceConfigs).slice(0, 512).forEach(([key, value]) => {
        const normalizedKey = normalizeVoiceKey(key);
        if (normalizedKey) normalizedVoiceConfigs[normalizedKey] = normalizeVoiceParams(value, baseParams);
    });
    if (!normalizedVoiceConfigs[defaultFemaleVoice]) normalizedVoiceConfigs[defaultFemaleVoice] = { ...baseParams };
    if (!normalizedVoiceConfigs[defaultMaleVoice]) normalizedVoiceConfigs[defaultMaleVoice] = { ...baseParams };

    const normalizedRoleConfigs = {};
    const rawRoleConfigs = raw.role_configs && typeof raw.role_configs === 'object'
        ? raw.role_configs
        : {};
    Object.entries(rawRoleConfigs).slice(0, 512).forEach(([key, value]) => {
        const normalizedKey = normalizeRoleConfigKeyClient(key);
        if (normalizedKey) normalizedRoleConfigs[normalizedKey] = normalizeVoiceParams(value, baseParams);
    });
    // 旧版配置按音色保存参数。首次升级时将旧值复制到两个默认槽位，
    // 之后槽位各自维护，不再因为选用了同一音色而互相覆盖。
    if (!normalizedRoleConfigs[DEFAULT_FEMALE_ROLE_KEY]) {
        normalizedRoleConfigs[DEFAULT_FEMALE_ROLE_KEY] = normalizeVoiceParams(
            normalizedVoiceConfigs[defaultFemaleVoice],
            baseParams,
        );
    }
    if (!normalizedRoleConfigs[DEFAULT_MALE_ROLE_KEY]) {
        normalizedRoleConfigs[DEFAULT_MALE_ROLE_KEY] = normalizeVoiceParams(
            normalizedVoiceConfigs[defaultMaleVoice],
            baseParams,
        );
    }

    const normalizedRoleVoices = {};
    const rawRoleVoices = raw.role_voices && typeof raw.role_voices === 'object'
        ? raw.role_voices
        : {};
    Object.entries(rawRoleVoices).slice(0, 128).forEach(([role, key]) => {
        const roleKey = normalizeRoleKeyClient(role);
        const voiceKey = resolveVoiceKeyForMode(key, generationMode);
        if (roleKey && voiceKey) normalizedRoleVoices[roleKey] = voiceKey;
    });

    return {
        generation_mode: generationMode,
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
    const compositeName = String(item.composite_name || item.common_name || name).trim();
    const variantNames = toList(item.variant_names || item.variantNames);
    const variantLabels = toList(item.variant_labels || item.variantLabels);
    const variantKeys = toList(item.variant_keys || item.variantKeys);
    const categories = [...new Set([...toList(item.categories), ...language, ...tags, genderLabel])].slice(0, 24);
    return {
        ...item,
        key: key || `name:${name.toLocaleLowerCase('zh-CN')}`,
        name,
        composite_name: compositeName,
        variant_names: variantNames,
        variant_labels: variantLabels,
        variant_keys: variantKeys,
        composite_key: normalizeVoiceKey(item.composite_key || item.compositeKey),
        emot_desc: String(item.emot_desc || item.emotDesc || '').trim(),
        gender: gender || 'unknown',
        gender_label: genderLabel,
        language,
        tags,
        categories,
        img_url: String(item.img_url || item.imgUrl || '').trim(),
        audio_url: String(item.audio_url || item.audioUrl || '').trim(),
        search_text: [name, compositeName, ...variantNames, ...variantLabels, genderLabel, ...language, ...tags, ...categories]
            .join(' ').toLocaleLowerCase('zh-CN'),
    };
}

function getVoiceEntry(key) {
    const rawKey = String(key ?? '').trim();
    const normalizedKey = normalizeVoiceKey(rawKey);
    const normalizedName = rawKey.toLocaleLowerCase('zh-CN');
    return voiceCatalog.find(voice => (
        voice.key === normalizedKey
        || String(voice.name || '').trim().toLocaleLowerCase('zh-CN') === normalizedName
        || String(voice.composite_key || '').trim() === normalizedKey
        || (Array.isArray(voice.variant_keys) && voice.variant_keys.includes(normalizedKey))
        || (Array.isArray(voice.variant_names) && voice.variant_names.some(name => (
            String(name).trim().toLocaleLowerCase('zh-CN') === normalizedName
        )))
    ))
        || normalizeVoiceEntry({ key: normalizedKey, name: normalizedKey || '未选择音色' });
}

function voiceEntryMatchesKey(voice, key) {
    const normalizedKey = normalizeVoiceKey(key);
    if (!normalizedKey || !voice) return false;
    if (voiceCatalogMode === GENERATION_MODE_SINGLE) return voice.key === normalizedKey;
    return voice.key === normalizedKey
        || String(voice.composite_key || '').trim() === normalizedKey
        || (Array.isArray(voice.variant_keys) && voice.variant_keys.includes(normalizedKey));
}

function voiceAssetUrl(key, kind) {
    const normalizedKey = normalizeVoiceKey(key);
    if (!normalizedKey || !['avatar', 'sample'].includes(kind)) return '';
    return apiUrl(`/api/voice-assets/${encodeURIComponent(normalizedKey)}/${kind}`);
}

function queueVoiceAssetCache(keys) {
    const values = Array.isArray(keys) ? keys : [keys];
    const normalizedKeys = [...new Set(values.map(value => normalizeVoiceKey(value)).filter(Boolean))];
    const pendingKeys = normalizedKeys.filter(key => (
        !voiceAssetCacheReady.has(key) && !voiceAssetCacheRequests.has(key)
    ));
    const inFlightRequests = [...new Set(normalizedKeys
        .map(key => voiceAssetCacheRequests.get(key))
        .filter(Boolean))];
    let cacheRequest = null;

    if (pendingKeys.length) {
        const request = fetch(apiUrl('/api/voice-assets/cache'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ voice_keys: pendingKeys }),
        })
            .then(response => {
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                return response.json();
            })
            .then(data => {
                const cached = data?.cached && typeof data.cached === 'object' ? data.cached : {};
                pendingKeys.forEach(key => {
                    if (cached[key] && typeof cached[key] === 'object') voiceAssetCacheReady.add(key);
                });
                return data;
            })
            .catch(error => {
                // 头像/试听缓存是增强项，讯飞资源不可达时结果页仍会回退到原始 URL。
                console.debug('音色资产缓存暂不可用:', error);
                return null;
            })
            .finally(() => {
                pendingKeys.forEach(key => {
                    if (voiceAssetCacheRequests.get(key) === request) voiceAssetCacheRequests.delete(key);
                });
            });
        pendingKeys.forEach(key => voiceAssetCacheRequests.set(key, request));
        cacheRequest = request;
    }

    // 如果生成流程刚刚发起过同一批缓存请求，结果页必须等待它们完成，
    // 否则首次渲染会错过缓存完成时机，头像节点被移除后就不会再回来。
    const requests = [...new Set([...inFlightRequests, cacheRequest].filter(Boolean))];
    if (!requests.length) return Promise.resolve(null);
    return Promise.all(requests).then(results => results.find(Boolean) || null);
}

function getResultVoiceEntry(key) {
    const voice = getVoiceEntry(key);
    const normalizedKey = normalizeVoiceKey(voice.key || key);
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
        const raw = JSON.parse(localStorage.getItem(VOICE_RECENT_STORAGE_KEY) || '[]');
        return Array.isArray(raw) ? raw.map(key => normalizeVoiceKey(key)).filter(Boolean).slice(0, 12) : [];
    } catch (_) {
        return [];
    }
}

function rememberVoiceUse(key) {
    const normalizedKey = normalizeVoiceKey(key);
    if (!normalizedKey) return;
    const recent = [normalizedKey, ...getRecentVoiceKeys().filter(item => item !== normalizedKey)].slice(0, 12);
    try {
        localStorage.setItem(VOICE_RECENT_STORAGE_KEY, JSON.stringify(recent));
    } catch (_) {
        // localStorage 不可用时不影响当前音色选择。
    }
}

function setVoiceCatalog(entries, filters = [], mode = null) {
    const normalized = Array.isArray(entries) ? entries.map(normalizeVoiceEntry) : [];
    const byKey = new Map();
    normalized.forEach(voice => {
        if (voice?.key && !byKey.has(voice.key)) byKey.set(voice.key, voice);
    });
    if (!byKey.has('amanda')) byKey.set('amanda', normalizeVoiceEntry({
        key: 'amanda', name: 'Amanda', gender: 'female', gender_label: '女声',
        language: ['英语'], tags: ['英语'], categories: ['女声', '英语'],
    }));
    if (!byKey.has('george')) byKey.set('george', normalizeVoiceEntry({
        key: 'george', name: 'George', gender: 'male', gender_label: '男声',
        language: ['英语'], tags: ['英语'], categories: ['男声', '英语'],
    }));
    voiceCatalog = [...byKey.values()];
    if (mode) voiceCatalogMode = normalizeGenerationMode(mode);
    const filterMap = new Map([['all', { key: 'all', label: '全部音色' }], ['recent', { key: 'recent', label: '最近使用' }]]);
    if (Array.isArray(filters)) {
        filters.forEach(filter => {
            const key = String(filter?.key || '').trim();
            const label = String(filter?.label || '').trim();
            if (key && label && key !== 'all') filterMap.set(key, { key, label, count: filter.count });
        });
    }
    if (!filterMap.has('female')) filterMap.set('female', { key: 'female', label: '女声' });
    if (!filterMap.has('male')) filterMap.set('male', { key: 'male', label: '男声' });
    voiceFilterOptions = [...filterMap.values()];
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
        if (!voiceParamConfigs[configKey]) voiceParamConfigs[configKey] = createDefaultVoiceParams();
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
            DEFAULT_VOICE_PARAMS,
        ),
        [DEFAULT_MALE_ROLE_KEY]: normalizeVoiceParams(
            voiceParamConfigs[DEFAULT_MALE_ROLE_KEY],
            DEFAULT_VOICE_PARAMS,
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
    if (!voiceParamConfigs[configKey]) voiceParamConfigs[configKey] = createDefaultVoiceParams();
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
        button.setAttribute('role', 'option');
        button.setAttribute('aria-selected', role.key === activeVoiceRole ? 'true' : 'false');

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

function renderVoiceFilters() {
    const container = $('voice-filter-row');
    if (!container) return;
    container.replaceChildren();
    voiceFilterOptions.forEach(filter => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `voice-filter-chip${filter.key === activeVoiceFilter ? ' is-active' : ''}`;
        button.dataset.voiceFilter = filter.key;
        button.setAttribute('role', 'tab');
        button.setAttribute('aria-selected', filter.key === activeVoiceFilter ? 'true' : 'false');
        button.textContent = filter.label;
        container.appendChild(button);
    });
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
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `voice-card${voiceEntryMatchesKey(voice, selectedKey) ? ' is-selected' : ''}`;
        button.dataset.voiceKey = voice.key;
        button.setAttribute('role', 'option');
        button.setAttribute('aria-selected', voiceEntryMatchesKey(voice, selectedKey) ? 'true' : 'false');
        const avatar = document.createElement('span');
        avatar.className = 'voice-avatar';
        // 首屏卡片直接加载，滚动到后续音色时再按可见区域加载，避免
        // 387 个音色同时请求头像而又保证当前列表不会只显示首字母。
        renderVoiceAvatar(avatar, voice, false, index < 20);
        const copy = document.createElement('span');
        copy.className = 'voice-card-copy';
        const name = document.createElement('strong');
        name.textContent = voice.name;
        const tags = document.createElement('span');
        tags.className = 'voice-card-tags';
        voiceTags(voice).forEach((tag, index) => {
            const tagEl = document.createElement('span');
            if (index === 0) tagEl.classList.add('is-gender');
            tagEl.textContent = tag;
            tags.appendChild(tagEl);
        });
        copy.append(name, tags);
        button.append(avatar, copy);
        fragment.appendChild(button);
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
    const voice = getVoiceEntry(activeVoiceKeyForRole(role));
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
        previewButton.disabled = !voice.audio_url;
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
    const normalizedKey = normalizeVoiceKey(key);
    if (!normalizedKey) return;
    stopVoicePreview();
    const role = voiceRoles.find(item => item.key === activeVoiceRole);
    if (role?.kind === 'default-male') selectedDefaultMaleVoice = normalizedKey;
    else if (role?.kind === 'default-female') selectedDefaultFemaleVoice = normalizedKey;
    else roleVoiceMap[activeVoiceRole] = normalizedKey;
    const configKey = roleConfigKeyForRole(role || activeVoiceRole);
    if (!voiceParamConfigs[configKey]) voiceParamConfigs[configKey] = createDefaultVoiceParams();
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
    const voice = getVoiceEntry(activeVoiceKeyForRole());
    if (!voice.audio_url) return;
    playVoiceSample(voice, $('voice-preview-btn'));
}

function bindVoiceWorkspaceEvents() {
    $('voice-search-input')?.addEventListener('input', scheduleVoiceCardsRender);
    $('voice-filter-row')?.addEventListener('click', event => {
        const button = event.target.closest('[data-voice-filter]');
        if (!button) return;
        activeVoiceFilter = button.dataset.voiceFilter || 'all';
        renderVoiceFilters();
        renderVoiceCards();
    });
    $('voice-role-list')?.addEventListener('click', event => {
        const button = event.target.closest('[data-role-key]');
        if (!button) return;
        stopVoicePreview();
        activeVoiceRole = button.dataset.roleKey || '__default_female__';
        renderVoiceWorkspace();
    });
    $('voice-browser-grid')?.addEventListener('click', event => {
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
            DEFAULT_VOICE_PARAMS,
        ),
        [DEFAULT_MALE_ROLE_KEY]: normalizeVoiceParams(
            normalized.role_configs?.[DEFAULT_MALE_ROLE_KEY],
            DEFAULT_VOICE_PARAMS,
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
    voiceParamConfigs[DEFAULT_FEMALE_ROLE_KEY] ||= createDefaultVoiceParams();
    voiceParamConfigs[DEFAULT_MALE_ROLE_KEY] ||= createDefaultVoiceParams();
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
    renderStep1Presets();
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
async function startProcessing(useDefaults, presetConfig) {
    if (isRestarting) return;
    if (!currentSession) {
        showToast('当前文档会话已失效，请重新导入文档');
        goToStep(1);
        return;
    }
    if (isGenerating) return;

    if (!(await ensureContentSaved())) return;
    const session = currentSession;
    const config = normalizeClientConfig(presetConfig || collectConfig(useDefaults));
    const taskSettings = desktopSettings?.task || {};
    const boundedInteger = (value, fallback, minimum, maximum) => {
        const number = Number(value);
        return Number.isFinite(number)
            ? Math.max(minimum, Math.min(maximum, Math.round(number)))
            : fallback;
    };
    const taskPolicy = {
        retry_count: boundedInteger(taskSettings.retry_count, 1, 0, 10),
        operation_timeout_seconds: boundedInteger(taskSettings.operation_timeout_seconds, 120, 10, 3600),
        keep_logs: taskSettings.keep_logs !== false,
        completion_notification: taskSettings.completion_notification !== false,
        open_output_dir: taskSettings.open_output_dir === true,
        close_browser_on_finish: taskSettings.close_browser_on_finish !== false,
        keep_history: taskSettings.keep_history !== false,
        history_limit: boundedInteger(taskSettings.history_limit, 20, 1, 20),
    };
    updateGenerationModeUI(config.generation_mode);
    const sourceTotal = summarizeParseResults(session.parse_results).total;
    const isPreviewScope = Boolean(config.preview && sourceTotal > 3);
    const generationTotal = isPreviewScope ? Math.min(sourceTotal, 3) : sourceTotal;
    const attemptId = ++generationAttemptId;
    const controller = new AbortController();
    generateAbortController = controller;
    destroyWaveSurfers();
    clearSSEReconnectTimer();
    isGenerating = true;
    if ($('history-nav-btn')) $('history-nav-btn').disabled = true;
    generatedFiles = [];
    logEntryCount = 0;
    lastStats = null;
    lastDownloadEvent = null;
    sseRetryCount = 0;
    generationResult = null;
    taskControlState = 'starting';
    updateSessionLabels(session.source_filename, session.parse_results, {
        preview: isPreviewScope,
        total: generationTotal,
    });
    hideGenerationRecovery();
    setGenerationVisualState('running');
    renderTaskControlState();

    // 重置生成页面 UI
    $('progress-bar').style.width = '0%';
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', '0');
    $('progress-stats').textContent = '准备中...';
    $('progress-percent').textContent = '0';
    $('progress-completed-label').textContent = '已完成';
    $('progress-completed').textContent = '0';
    $('progress-remaining').textContent = generationTotal || '—';
    $('progress-failed').textContent = '0';
    $('generation-live-status').textContent = '正在启动音频引擎';
    $('gen-title').textContent = '正在生成音频';
    $('gen-animation').classList.remove('done');
    resetLogTimeline('生成任务即将开始，正在等待第一条处理记录…');
    $('type-stats').innerHTML = '';

    lastGenerationConfig = { ...config };
    persistActiveSession();
    void queueVoiceAssetCache([
        config.default_female_voice,
        config.default_male_voice,
        ...Object.values(config.role_voices || {}),
    ]);

    try {
        const resp = await fetch(apiUrl('/api/generate'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: session.session_id,
                source_filename: session.source_filename,
                file_path: session.file_path,
                content_version: session.content_version,
                browser_policy: {
                    show_on_login: desktopSettings?.browser?.show_on_login !== false,
                    hide_after_login: desktopSettings?.browser?.hide_after_login !== false,
                    allow_system_chrome: desktopSettings?.browser?.allow_system_chrome === true,
                    keep_browser_hidden: privacyModeActive
                        || desktopSettings?.privacy?.keep_browser_hidden !== false,
                    close_browser_on_finish: taskPolicy.close_browser_on_finish,
                },
                task_policy: taskPolicy,
                config: config,
            }),
            signal: controller.signal,
        });

        if (!resp.ok) {
            let detail = '启动失败';
            try {
                const err = await resp.json();
                detail = err.detail || detail;
            } catch (_) {
                // 服务端返回非 JSON 响应
            }
            throw new Error(detail);
        }

        if (controller.signal.aborted || attemptId !== generationAttemptId || currentSession?.session_id !== session.session_id) return;
        generateAbortController = null;
        connectSSE(session.session_id);
        $('status-text').textContent = '生成中...';

    } catch (err) {
        if (err.name === 'AbortError' || attemptId !== generationAttemptId) return;
        generateAbortController = null;
        generationResult = 'error';
        const serviceUnavailable = err instanceof TypeError || /failed to fetch/i.test(err.message || '');
        const failureMessage = serviceUnavailable
            ? '无法连接生成服务，请重试连接后再次生成。'
            : `启动失败：${err.message}`;
        $('gen-title').textContent = '任务未能启动';
        $('generation-file-name').textContent = `未能启动「${session.source_filename || '当前文档'}」；设置与解析结果仍会保留。`;
        $('status-text').textContent = failureMessage;
        if (serviceUnavailable) {
            setServiceState('error', '服务连接中断');
            $('retry-service-btn').hidden = false;
        }
        setGenerationVisualState('error');
        addLogEntry({
            level: 'error',
            stage: 'complete',
            kind: 'summary',
            status: 'error',
            key: 'task:summary',
            title: '生成任务未能启动',
            detail: failureMessage,
        });
        showGenerationRecovery(failureMessage);
        showToast(failureMessage, 'error');
        resetGenerateState();
    }
}

// ============================================================================
// DOM 引用
// ============================================================================

const $ = (id) => document.getElementById(id);
const $$ = (sel) => document.querySelectorAll(sel);

const STEP_TITLES = {
    1: '01 / 导入文档',
    2: '02 / 核对与设置',
    3: '03 / 生成音频',
    4: '04 / 试听与下载',
};

function setServiceState(state, label) {
    const service = $('service-state');
    if (!service) return;
    service.classList.remove('is-ready', 'is-warning', 'is-error');
    if (state) service.classList.add(`is-${state}`);
    const labelEl = service.querySelector('.service-label');
    if (labelEl) labelEl.textContent = label;
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

function setUploadParsing(parsing) {
    const uploadZone = $('upload-zone');
    if (!uploadZone) return;
    uploadZone.classList.toggle('is-processing', parsing);
    uploadZone.setAttribute('aria-busy', parsing ? 'true' : 'false');
    const restartBtn = $('restart-btn');
    if (restartBtn) restartBtn.disabled = parsing || isRestarting;
    const historyNav = $('history-nav-btn');
    if (historyNav) historyNav.disabled = parsing || isGenerating || isRestarting;
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

function showGenerationRecovery(message) {
    const panel = $('generation-recovery');
    const messageEl = $('generation-error-message');
    if (messageEl) messageEl.textContent = message || '生成任务未能继续，请重试或返回调整设置。';
    if (panel) panel.hidden = false;
}

function hideGenerationRecovery() {
    const panel = $('generation-recovery');
    if (panel) panel.hidden = true;
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
    waveformItems.forEach(item => item.cancelWaveformLoad?.(false));
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
        } catch (e) { /* ignore */ }
    });
    audioElements = [];
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
        if (el) el.classList.remove('is-error', 'is-stopped', 'is-done');
    });
    if (animation && state !== 'done') animation.classList.remove('done');

    const labels = {
        running: '任务进行中',
        starting: '任务启动中',
        pause_requested: '正在暂停',
        paused: '任务已暂停',
        resume_requested: '正在恢复',
        terminating: '正在终止',
        done: '处理完成',
        error: '需要处理',
        warning: '部分完成',
        stopped: '任务已停止',
    };
    if (badgeLabel) badgeLabel.textContent = labels[state] || labels.running;
    const liveLabels = {
        running: '批量任务进行中',
        starting: '正在启动音频引擎',
        pause_requested: '正在等待安全暂停点',
        paused: '任务已暂停，等待恢复',
        resume_requested: '正在恢复任务',
        terminating: '正在安全终止任务',
        done: '批量任务已完成',
        warning: '任务完成，部分内容需处理',
        error: '生成遇到问题，请检查记录',
        stopped: '任务已停止',
    };
    if (liveStatus) liveStatus.textContent = liveLabels[state] || liveLabels.running;
    const liveLabelTexts = {
        running: '当前阶段',
        starting: '准备任务',
        pause_requested: '暂停请求',
        paused: '已暂停',
        resume_requested: '恢复请求',
        terminating: '终止请求',
        done: '任务完成',
        warning: '部分完成',
        error: '生成异常',
        stopped: '任务停止',
    };
    if (liveLabelText) liveLabelText.textContent = liveLabelTexts[state] || liveLabelTexts.running;

    if (state === 'done') {
        animation?.classList.add('done');
        badge?.classList.add('is-done');
        logDot?.classList.add('is-done');
    } else if (['paused', 'pause_requested', 'resume_requested', 'terminating'].includes(state)) {
        animation?.classList.add('is-stopped');
        badge?.classList.add('is-stopped');
        logDot?.classList.add('is-stopped');
    } else if (state === 'error') {
        animation?.classList.add('is-error');
        badge?.classList.add('is-error');
        logDot?.classList.add('is-error');
    } else if (state === 'stopped' || state === 'warning') {
        animation?.classList.add('is-stopped');
        badge?.classList.add('is-stopped');
        logDot?.classList.add('is-stopped');
    }
}

function setAppInteractive(enabled) {
    const effectiveEnabled = enabled && !isRestarting && !pendingCleanupSessionId;
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
}

async function confirmPendingCleanup(timeoutMs = 12000) {
    const sessionId = pendingCleanupSessionId;
    if (!sessionId) return true;

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(apiUrl(`/api/cleanup/${sessionId}`), {
            method: 'POST',
            signal: controller.signal,
        });
        if (response.status === 404) {
            if (pendingCleanupSessionId === sessionId) pendingCleanupSessionId = null;
            return true;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        if (pendingCleanupSessionId === sessionId) pendingCleanupSessionId = null;
        return true;
    } catch (error) {
        console.error('任务清理未确认:', error);
        return false;
    } finally {
        clearTimeout(timeout);
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
        'history-nav-btn',
        'history-start-btn',
        'history-back-btn',
        'back-to-history-btn',
    ].forEach(id => {
        const button = $(id);
        if (button) button.disabled = restarting;
    });
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

    if (pendingCleanupSessionId) {
        setServiceState('', '正在确认任务清理');
        const cleanupConfirmed = await confirmPendingCleanup();
        if (!cleanupConfirmed) {
            setServiceState('warning', '任务清理待确认');
            if (retryButton) retryButton.hidden = false;
            showToast('旧任务仍在清理，请稍后重试连接');
            return false;
        }
    }

    setServiceState('ready', '服务已连接');
    setAppInteractive(true);
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

async function init() {
    applyPerformanceMode();
    if (platform === 'darwin') {
        document.body.classList.add('platform-darwin');
    } else if (platform === 'win32') {
        document.body.classList.add('platform-win32');
    }

    bindNativeAppNotices();
    await loadDesktopSettings();
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
        await restoreActiveSession();
        showToast('就绪');
    }
}

function bindEvents() {
    bindWindowAndControlEvents();
    // 重新开始按钮（工具栏）
    $('restart-btn').addEventListener('click', requestRestart);
    $('retry-service-btn').addEventListener('click', async () => {
        const connected = await connectService(true);
        if (connected) await refreshHistoryRecords({ showLoading: currentView === 'history' });
    });
    $('history-nav-btn').addEventListener('click', () => showHistoryPage());
    $('back-to-history-btn').addEventListener('click', () => showHistoryPage());
    $('history-back-btn').addEventListener('click', returnToWorkflow);
    $('history-start-btn').addEventListener('click', returnToWorkflow);

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
        if (isRestarting || uploadZone.getAttribute('aria-disabled') === 'true') return;
        uploadZone.classList.add('dragover');
    });
    uploadZone.addEventListener('dragleave', () => {
        uploadZone.classList.remove('dragover');
    });
    uploadZone.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadZone.classList.remove('dragover');
        if (isRestarting || uploadZone.getAttribute('aria-disabled') === 'true') return;
        const file = e.dataTransfer.files[0];
        if (file && (file.name.toLowerCase().endsWith('.docx') || file.name.toLowerCase().endsWith('.xlsx'))) {
            handleFileSelected(file);
        } else {
            setUploadFeedback('error', '文件格式不支持，请重新选择 .docx 或 .xlsx 文档。');
            showToast('请选择 .docx 或 .xlsx 格式的文档', 'error');
        }
    });

    // 隐藏的 file input（浏览器模式）
    $('hidden-file-input').addEventListener('change', (e) => {
        const file = e.target.files[0];
        if (file) handleFileSelected(file);
        // 重置 value 以便重复选择同一文件时仍能触发 change 事件
        e.target.value = '';
    });

    // 兼容旧版首页上的快速开始入口（新版流程已收敛到配置页）
    const quickStartBtn = $('quick-start-btn');
    if (quickStartBtn) {
        quickStartBtn.addEventListener('click', () => {
            if (!currentSession) {
                showToast('请先上传文档');
                return;
            }
            goToStep(3);
            startProcessing(true);
        });
    }

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
        if (!isGenerating) startProcessing(false, lastGenerationConfig || undefined);
    });
    $('return-config-btn').addEventListener('click', () => {
        if (!isGenerating) {
            hideGenerationRecovery();
            goToStep(2);
        }
    });
    $('skip-config-btn').addEventListener('click', () => {
        applyConfigToForm(collectConfig(true), { includeRoles: true });
        showToast('已恢复推荐设置');
    });
    $('start-generate-btn').addEventListener('click', () => {
        goToStep(3);
        startProcessing(false);
    });

    $('content-save-btn')?.addEventListener('click', async () => {
        await saveEditedContent();
    });
    $('content-reset-btn')?.addEventListener('click', () => {
        contentDraft = cloneParseResults(currentSession?.parse_results || []);
        contentEditorDirty = false;
        renderContentEditor();
        showToast('已恢复最近保存的文档内容');
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
    $('generate-full-btn').addEventListener('click', () => {
        if (!lastGenerationConfig) return;
        destroyWaveSurfers();
        applyConfigToForm({ ...lastGenerationConfig, preview: false }, { includeRoles: true });
        goToStep(2);
        showToast('已保留试听设置，确认后可生成完整文档');
    });
    $('result-return-config-btn').addEventListener('click', () => {
        destroyWaveSurfers();
        if (lastGenerationConfig) applyConfigToForm(lastGenerationConfig, { includeRoles: true });
        goToStep(2);
        showToast('已返回配置；修改参数后会重新生成全部内容');
    });
    $('retry-failed-btn').addEventListener('click', () => {
        if (!lastGenerationConfig || isGenerating || isRestarting) return;
        destroyWaveSurfers();
        goToStep(3);
        startProcessing(false, { ...lastGenerationConfig });
        showToast('正在沿用当前设置重试失败项');
    });
    $('new-file-btn').addEventListener('click', requestRestart);
}

// ============================================================================
// 配置加载
// ============================================================================

const VOICE_CATALOG_REFRESH_INTERVAL_MS = 10000;
const VOICE_CATALOG_REFRESH_MAX_ATTEMPTS = 4;
let voiceCatalogRefreshTimer = null;
let voiceCatalogRefreshAttempts = 0;
let voiceCatalogRefreshInFlight = false;

function isLiveVoiceCatalog(config = currentConfig) {
    return config?.voice_catalog_meta?.catalog_source === 'live';
}

function clearVoiceCatalogRefresh() {
    if (voiceCatalogRefreshTimer !== null) {
        clearTimeout(voiceCatalogRefreshTimer);
        voiceCatalogRefreshTimer = null;
    }
    voiceCatalogRefreshAttempts = 0;
}

function scheduleVoiceCatalogRefresh() {
    if (
        isLiveVoiceCatalog()
        || voiceCatalogRefreshTimer !== null
        || voiceCatalogRefreshInFlight
        || voiceCatalogRefreshAttempts >= VOICE_CATALOG_REFRESH_MAX_ATTEMPTS
    ) return;

    voiceCatalogRefreshTimer = setTimeout(async () => {
        voiceCatalogRefreshTimer = null;
        if (isLiveVoiceCatalog()) {
            clearVoiceCatalogRefresh();
            return;
        }
        // 目录刷新只是首屏增强项，不能在生成/解析或用户正在配置音色时
        // 抢占请求或触发重绘；空闲后会继续轮询。
        if (isGenerating || isParsing || isRestarting || currentStep === 2) {
            // 用户在配置页时延后更久，避免选中音色被频繁重绘覆盖
            voiceCatalogRefreshTimer = setTimeout(scheduleVoiceCatalogRefresh, 15000);
            return;
        }

        voiceCatalogRefreshAttempts += 1;
        voiceCatalogRefreshInFlight = true;
        let loaded = false;
        try {
            loaded = await loadConfig({ silent: true, scheduleCatalogRefresh: false });
        } finally {
            voiceCatalogRefreshInFlight = false;
        }
        if (loaded && isLiveVoiceCatalog()) {
            clearVoiceCatalogRefresh();
            return;
        }
        scheduleVoiceCatalogRefresh();
    }, VOICE_CATALOG_REFRESH_INTERVAL_MS);
}

async function loadConfig({ silent = false, scheduleCatalogRefresh = true } = {}) {
    if (!silent) voiceCatalogRefreshAttempts = 0;
    const maxRetries = 3;
    for (let attempt = 1; attempt <= maxRetries; attempt++) {
        try {
            const resp = await fetch(apiUrl('/api/config'));
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            currentConfig = await resp.json();

            setVoiceCatalogForMode(selectedGenerationMode());
            if (!clientConfigInitialized) {
                const normalized = normalizeClientConfig(currentConfig);
                selectedDefaultFemaleVoice = normalized.default_female_voice;
                selectedDefaultMaleVoice = normalized.default_male_voice;
                voiceParamConfigs = Object.fromEntries(
                    Object.entries(normalized.role_configs || {}).map(([key, value]) => [key, { ...value }]),
                );
                if (!voiceParamConfigs[DEFAULT_FEMALE_ROLE_KEY]) voiceParamConfigs[DEFAULT_FEMALE_ROLE_KEY] = createDefaultVoiceParams();
                if (!voiceParamConfigs[DEFAULT_MALE_ROLE_KEY]) voiceParamConfigs[DEFAULT_MALE_ROLE_KEY] = createDefaultVoiceParams();
                clientConfigInitialized = true;
                renderVoiceWorkspace();
            } else if (silent) {
                // 后台静默刷新：只更新目录并轻量重绘，避免覆盖用户正在进行的音色选择
                renderVoiceFilters();
                scheduleVoiceCardsRender();
                renderVoiceDetails();
                renderRecentVoiceList();
            } else {
                renderVoiceWorkspace();
            }

            // 刷新摘要中的音色显示
            updateConfigSummary();

            if (isLiveVoiceCatalog()) {
                clearVoiceCatalogRefresh();
            } else if (scheduleCatalogRefresh) {
                scheduleVoiceCatalogRefresh();
            }

            return true;  // 成功，退出重试
        } catch (err) {
            console.error(`加载配置失败 (尝试 ${attempt}/${maxRetries}):`, err);
            if (attempt < maxRetries) {
                await new Promise(r => setTimeout(r, 1000 * attempt));
            } else {
                if (!silent) showToast('加载配置失败，部分功能可能不可用');
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

function goToStep(step) {
    currentView = 'workflow';
    currentStep = step;
    if (step === 2 && currentSession) {
        renderContentEditor();
        renderVoiceWorkspace();
    }

    const historyNav = $('history-nav-btn');
    historyNav?.classList.remove('active');
    historyNav?.removeAttribute('aria-current');
    const backToHistoryBtn = $('back-to-history-btn');
    if (backToHistoryBtn) backToHistoryBtn.hidden = true;

    // 切换页面
    $$('.step-page').forEach(p => p.classList.remove('active'));
    $(`page-${step}`).classList.add('active');

    updateStepper();

    // 滚动到顶部
    const scrollPage = $(`page-${step}`).querySelector('.page-scroll, .page-center');
    if (scrollPage) scrollPage.scrollTop = 0;
    if (step === 4) updateResultScrollTopButton();

    const heading = $(`page-${step}`).querySelector('h1');
    if (heading) requestAnimationFrame(() => heading.focus({ preventScroll: true }));

    if (step === 4) {
        // 结果页在构建波形时仍处于 display:none；等待两帧，确保 WaveSurfer 获得正确容器宽度。
        requestAnimationFrame(() => requestAnimationFrame(activateResultWaveforms));
    }
}

function updateStepper() {
    $$('.step-indicator').forEach(el => {
        const step = parseInt(el.dataset.step);
        el.classList.remove('active', 'completed');
        el.removeAttribute('aria-current');
        if (step < currentStep) {
            el.classList.add('completed');
        } else if (step === currentStep && currentView === 'workflow') {
            el.classList.add('active');
            el.setAttribute('aria-current', 'step');
        }
    });

    $$('.step-line').forEach(el => {
        const line = parseInt(el.dataset.line);
        el.classList.toggle('active', line < currentStep);
    });

    const toolbarStep = $('toolbar-step');
    if (toolbarStep) {
        toolbarStep.textContent = currentView === 'workflow'
            ? (STEP_TITLES[currentStep] || '')
            : '历史记录';
    }
}

function setHistoryNavActive(active) {
    const historyNav = $('history-nav-btn');
    if (!historyNav) return;
    historyNav.classList.toggle('active', active);
    if (active) historyNav.setAttribute('aria-current', 'page');
    else historyNav.removeAttribute('aria-current');
}

function activateStandalonePage(pageId, view) {
    currentView = view;
    $$('.step-page').forEach(page => page.classList.remove('active'));
    const page = $(pageId);
    page?.classList.add('active');
    setHistoryNavActive(true);
    updateStepper();

    const scrollPage = page?.querySelector('.page-scroll, .page-center');
    if (scrollPage) scrollPage.scrollTop = 0;
    if (pageId === 'page-4') updateResultScrollTopButton();
    const heading = page?.querySelector('h1');
    if (heading) requestAnimationFrame(() => heading.focus({ preventScroll: true }));
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
    if (historyBackBtn) historyBackBtn.textContent = currentSession ? '← 返回当前任务' : '← 返回导入文档';
    if (refresh) void refreshHistoryRecords();
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
    return String(record?.format || 'mp3').toUpperCase();
}

function historyGenerationModeLabel(record) {
    // 历史清单升级前没有该字段，按原有逐条流程解释，避免把旧任务误标成
    // 新的合并切割模式。
    return record?.generation_mode
        ? generationModeLabel(record.generation_mode)
        : GENERATION_MODE_LABELS[GENERATION_MODE_SINGLE];
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
    list.replaceChildren();
    empty.hidden = records.length > 0;
    if (records.length === 0) return;

    records.forEach(record => {
        const availableCount = Math.max(0, Number(record.available_files) || 0);
        const completed = Math.max(0, Number(record.completed) || availableCount);
        const failed = Math.max(0, Number(record.failed) || 0);
        const missingCount = Math.max(0, completed - availableCount);
        const item = document.createElement('article');
        item.className = 'history-item surface-card';

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
        const terminated = Boolean(record.terminated || record.status === 'terminated');
        status.className = `history-status-badge${terminated || failed > 0 || missingCount > 0 ? ' is-partial' : ''}`;
        status.textContent = terminated
            ? '已终止'
            : (availableCount === 0
                ? '文件缺失'
                : (missingCount > 0 ? '部分缺失' : (failed > 0 ? '部分完成' : '已完成')));
        titleRow.append(title, status);

        const meta = document.createElement('div');
        meta.className = 'history-item-meta';
        const completedAt = document.createElement('span');
        completedAt.textContent = historyDateLabel(record.completed_at);
        const scope = document.createElement('span');
        scope.textContent = record.preview ? '试听任务' : '完整任务';
        const mode = document.createElement('span');
        mode.textContent = historyGenerationModeLabel(record);
        meta.append(completedAt, scope, mode);

        const stats = document.createElement('div');
        stats.className = 'history-item-stats';
        const audioStat = document.createElement('span');
        audioStat.className = 'history-stat';
        const audioStrong = document.createElement('strong');
        audioStrong.textContent = String(availableCount);
        audioStat.append(audioStrong, document.createTextNode(' 个音频'));
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

        main.append(titleRow, meta, stats);

        const actions = document.createElement('div');
        actions.className = 'history-item-actions';
        const viewBtn = createHistoryAction('查看结果', 'btn-primary', () => viewHistoryRecord(record.id));
        viewBtn.disabled = availableCount === 0;
        if (record.zip_available) {
            const zipBtn = createHistoryAction('下载 ZIP', 'btn-ghost', async () => {
                zipBtn.disabled = true;
                try {
                    await downloadZip({
                        mode: 'history',
                        recordId: record.id,
                        sourceFilename: record.source_filename,
                    });
                } finally {
                    zipBtn.disabled = false;
                }
            });
            actions.appendChild(zipBtn);
        }
        const deleteBtn = createHistoryAction('删除', 'btn-ghost history-delete-btn', () => deleteHistoryRecord(record, deleteBtn));
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
    notice.className = `${className} surface-card`;
    notice.textContent = message;
    list.appendChild(notice);
}

async function refreshHistoryRecords({ showLoading = true } = {}) {
    const requestToken = ++historyRequestToken;
    if (showLoading && currentView === 'history') renderHistoryMessage('正在读取本机历史记录…');
    try {
        const response = await fetch(apiUrl('/api/history'));
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (requestToken !== historyRequestToken) return historyRecords;
        historyRecords = Array.isArray(data.records) ? data.records.slice(0, 20) : [];
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
        const response = await fetch(apiUrl(`/api/history/${encodeURIComponent(historyId)}`));
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const record = await response.json();
        if (requestToken !== historyRequestToken) return;
        const context = {
            mode: 'history',
            recordId: record.id,
            sourceFilename: record.source_filename,
            files: Array.isArray(record.files) ? record.files.filter(file => file.available !== false) : [],
            completed: Number(record.completed) || 0,
            failed: Number(record.failed) || 0,
            total: Number(record.total) || 0,
            format: record.format || 'mp3',
            generationMode: record.generation_mode || GENERATION_MODE_SINGLE,
            preview: Boolean(record.preview),
            zipAvailable: Boolean(record.zip_available),
            failedItems: Array.isArray(record.failed_items) ? record.failed_items : [],
        };
        buildResultPage({
            completed: context.completed,
            failed: context.failed,
            total: context.total,
            failed_items: context.failedItems,
            zip_path: context.zipAvailable ? 'history' : null,
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
    if (!record?.id || isRestarting) return;
    const filename = record.source_filename || '未命名文档';
    const fileCount = Math.max(0, Number(record.available_files) || 0);
    const confirmed = await showConfirmDialog({
        kicker: '历史记录',
        title: '删除这条生成记录？',
        message: `将删除「${filename}」及其 ${fileCount} 个音频文件。`,
        detail: '文件会从本机历史记录与输出目录中移除，删除后无法恢复。',
        tone: 'danger',
        confirmLabel: '删除记录',
    });
    if (!confirmed) return;
    historyRequestToken++;
    if (button) button.disabled = true;
    try {
        const response = await fetch(apiUrl(`/api/history/${encodeURIComponent(record.id)}`), { method: 'DELETE' });
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.detail || `HTTP ${response.status}`);
        }
        const deletedCurrentResult = latestCurrentResultEvent?.history_id === record.id;
        historyRecords = historyRecords.filter(item => item.id !== record.id);
        if (deletedCurrentResult) {
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
            if ($('history-back-btn')) $('history-back-btn').textContent = '← 返回导入文档';
        }
        setHistoryCounts(historyRecords.length);
        if (currentView === 'history') renderHistoryRecords(historyRecords);
        showToast(deletedCurrentResult ? '当前任务结果及历史记录已删除' : '历史记录已删除');
    } catch (error) {
        console.error('删除历史记录失败:', error);
        showToast(`删除失败：${error.message || '请稍后重试'}`);
        if (button) button.disabled = false;
    }
}

// ============================================================================
// Step 1: 文件上传
// ============================================================================

async function selectFile() {
    if (isParsing || isRestarting || $('upload-zone')?.getAttribute('aria-disabled') === 'true') return;
    if (isElectron) {
        try {
            const result = await window.electronAPI.selectFile();
            // 兼容旧主进程直接返回字符串的格式，避免开发热重载时前后端版本错位。
            const filePath = typeof result === 'string'
                ? result
                : result?.success === true && typeof result.filePath === 'string'
                    ? result.filePath
                    : '';
            if (filePath) {
                handleFilePath(filePath);
            } else if (result != null && result?.reason !== 'user-cancelled') {
await showNativeFileDialogError('选择文档失败', result?.reason ? result : {
                reason: result?.success === true ? 'dialog-error' : 'ipc-error',
                error: '主进程未返回有效的文件路径',
                });
            }
        } catch (error) {
            console.error('打开文件选择框失败:', error);
            await showNativeFileDialogError('选择文档失败', {
                reason: 'ipc-error',
                error: error?.message,
            });
        }
    } else {
        $('hidden-file-input').click();
    }
}

function handleFileSelected(file) {
    if (isRestarting || $('upload-zone')?.getAttribute('aria-disabled') === 'true') return;
    if (file.path) {
        handleFilePath(file.path);
    } else {
        uploadFile(file);
    }
}

let isParsing = false;  // 防止解析重入

async function handleFilePath(filePath) {
    if (isParsing || isRestarting) return;  // 防止重入
    isParsing = true;
    const attemptId = ++parseAttemptId;
    const controller = new AbortController();
    parseAbortController = controller;

    const filename = filePath.split(/[\\/]/).pop();
    const uploadZone = $('upload-zone');
    uploadZone.classList.add('has-file');
    setUploadParsing(true);
    setUploadFeedback('info', '正在读取并核对文档结构，请稍候…');
    uploadZone.querySelector('.upload-text-large').textContent = filename;
    uploadZone.querySelector('.upload-hint').textContent = '正在解析文档结构...';
    $('status-text').textContent = `正在解析: ${filename}`;

    try {
        const resp = await fetch(apiUrl('/api/parse'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_path: filePath }),
            signal: controller.signal,
        });

        if (!resp.ok) {
            let detail = '解析失败';
            try {
                const err = await resp.json();
                detail = err.detail || detail;
            } catch (_) {
                // 服务端返回非 JSON 响应（如 502 HTML 页面）
            }
            throw new Error(detail);
        }

        const data = await resp.json();
        if (controller.signal.aborted || attemptId !== parseAttemptId) return;
        if (!Array.isArray(data.parse_results) || data.parse_results.length === 0) {
            throw new Error('未识别到支持的题型内容，请检查文档结构后重试');
        }
        resetTaskVoiceConfiguration();
        currentSession = {
            session_id: data.session_id,
            source_filename: data.source_filename,
            file_path: data.file_path,
            parse_results: data.parse_results || [],
            content_version: data.content_version || null,
        };
        contentDraft = cloneParseResults(currentSession.parse_results);
        contentEditorDirty = false;
        renderContentEditor();
        persistActiveSession();

        updateSessionLabels(data.source_filename || filename, currentSession.parse_results);
        uploadZone.querySelector('.upload-hint').textContent = '解析完成，正在打开声音配置';
        setUploadFeedback('success', `解析完成：已识别 ${summarizeParseResults(currentSession.parse_results).total} 条内容。`);
        $('status-text').textContent = `解析成功 — ${data.source_filename}`;
        showToast('文档解析成功，进入配置步骤');

        // 解析完成后进入配置步骤
        goToStep(2);

    } catch (err) {
        if (err.name === 'AbortError' || attemptId !== parseAttemptId) return;
        console.error('解析失败:', err);
        showToast(`解析失败: ${err.message}`, 'error');
        uploadZone.classList.remove('has-file');
        uploadZone.querySelector('.upload-text-large').textContent = '拖拽文档到这里，或点击选择';
        uploadZone.querySelector('.upload-hint').textContent = '请检查文档格式或内容后重新选择';
        setUploadFeedback('error', `文档解析失败：${err.message}`);
        $('status-text').textContent = '文档解析失败，请重新选择';
    } finally {
        if (attemptId === parseAttemptId) {
            parseAbortController = null;
            setUploadParsing(false);
            isParsing = false;
        }
    }
}

async function uploadFile(file) {
    if (isParsing || isRestarting) return;
    isParsing = true;
    const attemptId = ++parseAttemptId;
    const controller = new AbortController();
    parseAbortController = controller;

    const formData = new FormData();
    formData.append('file', file);
    const uploadZone = $('upload-zone');

    // 立即更新上传区域，给用户即时反馈
    uploadZone.classList.add('has-file');
    setUploadParsing(true);
    setUploadFeedback('info', '正在上传并核对文档结构，请稍候…');
    uploadZone.querySelector('.upload-text-large').textContent = file.name;
    uploadZone.querySelector('.upload-hint').textContent = '正在上传并解析文档...';
    $('status-text').textContent = `正在上传: ${file.name}`;

    try {
        const resp = await fetch(apiUrl('/api/parse/upload'), {
            method: 'POST',
            body: formData,
            signal: controller.signal,
        });
        if (!resp.ok) {
            let detail = '上传失败';
            try {
                const err = await resp.json();
                detail = err.detail || detail;
            } catch (_) {
                // 服务端返回非 JSON 响应
            }
            throw new Error(detail);
        }
        const data = await resp.json();
        if (controller.signal.aborted || attemptId !== parseAttemptId) return;
        if (!Array.isArray(data.parse_results) || data.parse_results.length === 0) {
            throw new Error('未识别到支持的题型内容，请检查文档结构后重试');
        }
        resetTaskVoiceConfiguration();
        currentSession = {
            session_id: data.session_id,
            source_filename: data.source_filename,
            file_path: data.file_path,
            parse_results: data.parse_results || [],
            content_version: data.content_version || null,
        };
        contentDraft = cloneParseResults(currentSession.parse_results);
        contentEditorDirty = false;
        renderContentEditor();
        persistActiveSession();
        updateSessionLabels(data.source_filename || file.name, currentSession.parse_results);
        uploadZone.querySelector('.upload-hint').textContent = '解析完成，正在打开声音配置';
        setUploadFeedback('success', `解析完成：已识别 ${summarizeParseResults(currentSession.parse_results).total} 条内容。`);
        $('status-text').textContent = `解析成功 — ${data.source_filename || file.name}`;
        showToast('文档解析成功，进入配置步骤');
        goToStep(2);
    } catch (err) {
        if (err.name === 'AbortError' || attemptId !== parseAttemptId) return;
        showToast(`上传失败: ${err.message}`, 'error');
        // 重置上传区域
        uploadZone.classList.remove('has-file');
        uploadZone.querySelector('.upload-text-large').textContent = '拖拽文档到这里，或点击选择';
        uploadZone.querySelector('.upload-hint').textContent = '请检查网络或文档后重新选择';
        setUploadFeedback('error', `文档上传失败：${err.message}`);
        $('status-text').textContent = '文档上传失败，请重新选择';
    } finally {
        if (attemptId === parseAttemptId) {
            parseAbortController = null;
            setUploadParsing(false);
            isParsing = false;
        }
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
                [DEFAULT_FEMALE_ROLE_KEY]: createDefaultVoiceParams(),
                [DEFAULT_MALE_ROLE_KEY]: createDefaultVoiceParams(),
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

function collectPersistedConfig() {
    return normalizePersistedConfig(collectConfig(false));
}

// ============================================================================
// 桌面状态、任务恢复与可编辑内容
// ============================================================================

let activeSessionPersistToken = 0;
let browserVisibilityBeforePrivacy = null;

function cloneParseResults(value) {
    if (!Array.isArray(value)) return [];
    try {
        return JSON.parse(JSON.stringify(value));
    } catch (_) {
        return [];
    }
}

function settingsStorageValue(key) {
    try {
        return localStorage.getItem(key);
    } catch (_) {
        return null;
    }
}

function clearLegacyStorageKeys() {
    try {
        localStorage.removeItem(PRESET_STORAGE_KEY);
        localStorage.removeItem(CURRENT_CONFIG_STORAGE_KEY);
    } catch (_) {
        // localStorage 被策略禁用时不影响桌面设置已写入的结果。
    }
}

async function loadDesktopSettings() {
    if (!isElectron || !window.electronAPI.settings) return;
    try {
        const result = await window.electronAPI.settings.get();
        if (result?.success && result.settings) desktopSettings = result.settings;
    } catch (error) {
        console.error('读取桌面设置失败:', error);
        showToast('桌面设置读取失败，将使用当前会话默认值', 'warning');
    }

    // 旧版本只把配置存进 renderer localStorage。迁移只执行一次，迁移完成后
    // 清理两个旧 key，避免旧页面再次覆盖主进程设置。
    if (desktopSettings && !desktopSettings.migrations?.legacy_local_storage) {
        let currentConfig = null;
        let presets = [];
        try {
            const currentRaw = settingsStorageValue(CURRENT_CONFIG_STORAGE_KEY);
            const presetsRaw = settingsStorageValue(PRESET_STORAGE_KEY);
            currentConfig = currentRaw ? JSON.parse(currentRaw) : null;
            presets = presetsRaw ? JSON.parse(presetsRaw) : [];
        } catch (_) {
            currentConfig = null;
            presets = [];
        }
        if (currentConfig || (Array.isArray(presets) && presets.length > 0)) {
            try {
                const migrated = await window.electronAPI.settings.importLegacy({
                    current_config: currentConfig,
                    presets: Array.isArray(presets) ? presets : [],
                });
                if (migrated?.success) {
                    if (migrated.settings) desktopSettings = migrated.settings;
                    clearLegacyStorageKeys();
                }
            } catch (error) {
                console.error('迁移旧版配置失败:', error);
            }
        } else {
            // 即使旧 key 不存在也记下迁移完成，避免每次启动重复检查。
            try {
                const migrated = await window.electronAPI.settings.update({
                    migrations: { legacy_local_storage: true },
                });
                if (migrated?.success && migrated.settings) desktopSettings = migrated.settings;
            } catch (_) { /* 内存默认值仍可使用 */ }
        }
    }

    try {
        const result = await window.electronAPI.getWindowState();
        if (result?.success) applyWindowState(result.state);
    } catch (error) {
        console.error('读取窗口状态失败:', error);
    }
}

function updateDesktopSettingsSnapshot(settings) {
    if (settings && typeof settings === 'object') {
        settingsMutationToken++;
        desktopSettings = settings;
    }
    renderBrowserState();
    renderTaskControlState();
}

function persistActiveSession() {
    if (!isElectron || !desktopSettings || !window.electronAPI.settings || !currentSession) return;
    const token = ++activeSessionPersistToken;
    const payload = {
        session_id: String(currentSession.session_id || '').slice(0, 220),
        source_filename: String(currentSession.source_filename || '').slice(0, 240),
        file_path: String(currentSession.file_path || '').slice(0, 1000),
        content_version: currentSession.content_version || null,
        parse_results: cloneParseResults(currentSession.parse_results),
        last_generation_config: lastGenerationConfig ? normalizePersistedConfig(lastGenerationConfig) : null,
        control_state: taskControlState,
        saved_at: new Date().toISOString(),
    };
    void window.electronAPI.settings.update({
        runtime: { active_session: payload },
    }).then(result => {
        if (token !== activeSessionPersistToken) return;
        if (result?.success && result.settings) desktopSettings = result.settings;
    }).catch(error => console.error('保存活动任务状态失败:', error));
}

function clearPersistedActiveSession() {
    if (!isElectron || !window.electronAPI.settings) return;
    activeSessionPersistToken++;
    void window.electronAPI.settings.update({ runtime: { active_session: null } })
        .then(result => {
            if (result?.success && result.settings) desktopSettings = result.settings;
        })
        .catch(error => console.error('清理活动任务状态失败:', error));
}

function sessionFromInfo(info, fallback = {}) {
    const parseResults = Array.isArray(info?.parse_results)
        ? info.parse_results
        : (Array.isArray(fallback.parse_results) ? fallback.parse_results : []);
    return {
        session_id: String(info?.session_id || fallback.session_id || ''),
        source_filename: info?.source_filename || fallback.source_filename || '',
        file_path: info?.file_path || fallback.file_path || '',
        parse_results: cloneParseResults(parseResults),
        content_version: info?.content_version || fallback.content_version || null,
    };
}

async function getSessionInfo(sessionId) {
    const response = await fetch(apiUrl(`/api/session/${encodeURIComponent(sessionId)}`));
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        const error = new Error(data.detail || `HTTP ${response.status}`);
        error.status = response.status;
        throw error;
    }
    return data;
}

function restoreProgressSummary(progress) {
    if (!progress || typeof progress !== 'object') return;
    const total = Number(progress.total_items) || 0;
    const completed = Number(progress.completed) || 0;
    const failed = Number(progress.failed) || 0;
    updateProgress({ total, completed, failed, processed: completed + failed });
    if (Array.isArray(progress.files)) {
        const available = progress.files.filter(item => item?.status === 'done');
        if (available.length > 0) generatedFiles = available;
    }
}

async function restoreActiveSession() {
    if (!isElectron) return false;
    const persisted = desktopSettings?.runtime?.active_session;
    if (!persisted?.session_id) return false;
    try {
        const info = await getSessionInfo(persisted.session_id);
        currentSession = sessionFromInfo(info, persisted);
        if (!currentSession.session_id || currentSession.parse_results.length === 0) {
            clearPersistedActiveSession();
            return false;
        }
        contentDraft = cloneParseResults(currentSession.parse_results);
        contentEditorDirty = false;
        taskControlState = info.control_state || persisted.control_state || 'idle';
        browserState = info.browser_state || browserState;
        lastStats = info.last_stats || null;
        renderContentEditor();
        updateSessionLabels(currentSession.source_filename, currentSession.parse_results);
        renderBrowserState();
        renderTaskControlState();

        if (persisted.last_generation_config) {
            lastGenerationConfig = normalizeClientConfig(persisted.last_generation_config);
            applyConfigToForm(lastGenerationConfig, { includeRoles: true });
        }

        const finalDownload = info.final_download || null;
        if (finalDownload) {
            lastDownloadEvent = finalDownload;
            updateFileList(finalDownload);
        }

        const active = Boolean(info.task_active)
            || ['starting', 'running', 'pause_requested', 'paused', 'resume_requested', 'terminating'].includes(taskControlState);
        if (active && !info.ended) {
            isGenerating = true;
            generationResult = null;
            restoreProgressSummary(info.progress);
            goToStep(3);
            connectSSE(currentSession.session_id);
            showToast('已恢复当前生成任务');
            return true;
        }

        if (info.final_done) {
            isGenerating = true;
            generationResult = null;
            restoreProgressSummary(info.progress);
            goToStep(3);
            handleDone(info.final_done);
            return true;
        }
        if (info.final_terminated) {
            isGenerating = true;
            restoreProgressSummary(info.progress);
            goToStep(3);
            handleTerminated(info.final_terminated);
            return true;
        }
        if (info.final_error) {
            goToStep(3);
            handleSSEEvent(info.final_error);
            return true;
        }
        if (info.final_cancelled) {
            goToStep(3);
            handleSSEEvent(info.final_cancelled);
            return true;
        }

        goToStep(2);
        return true;
    } catch (error) {
        if (error.status === 404) clearPersistedActiveSession();
        else console.error('恢复活动任务失败:', error);
        return false;
    }
}

function contentEditorValue(value) {
    return String(value ?? '');
}

function renderContentEditor() {
    const list = $('content-editor-list');
    if (!list) return;
    list.replaceChildren();
    const draft = Array.isArray(contentDraft) ? contentDraft : [];
    const count = draft.reduce((sum, section) => sum + (Array.isArray(section?.items) ? section.items.length : 0), 0);
    const countEl = $('content-editor-count');
    if (countEl) countEl.textContent = `${count} 条`;
    const dirtyEl = $('content-editor-dirty');
    if (dirtyEl) dirtyEl.hidden = !contentEditorDirty;
    const saveButton = $('content-save-btn');
    if (saveButton) saveButton.disabled = !contentEditorDirty || !currentSession || isGenerating || isRestarting;
    const resetButton = $('content-reset-btn');
    if (resetButton) resetButton.disabled = !currentSession || isGenerating || isRestarting;

    if (draft.length === 0) {
        const empty = document.createElement('p');
        empty.className = 'content-editor-empty';
        empty.textContent = '解析结果为空，请重新导入文档。';
        list.appendChild(empty);
        return;
    }

    draft.forEach((section, sectionIndex) => {
        const details = document.createElement('details');
        details.className = 'content-editor-section-card';
        details.open = true;
        const summary = document.createElement('summary');
        const summaryCopy = document.createElement('span');
        summaryCopy.className = 'content-editor-section-title';
        summaryCopy.textContent = `${sectionIndex + 1}. ${section?.doc_type || '未命名题型'}`;
        const summaryCount = document.createElement('small');
        summaryCount.textContent = `${Array.isArray(section?.items) ? section.items.length : 0} 条`;
        summary.append(summaryCopy, summaryCount);
        details.appendChild(summary);

        const body = document.createElement('div');
        body.className = 'content-editor-section-body';
        const typeLabel = document.createElement('label');
        typeLabel.className = 'content-editor-field content-editor-type-field';
        typeLabel.innerHTML = '<span>题型名称</span>';
        const typeInput = document.createElement('input');
        typeInput.type = 'text';
        typeInput.value = contentEditorValue(section?.doc_type);
        typeInput.dataset.contentSection = String(sectionIndex);
        typeInput.dataset.contentField = 'doc_type';
        typeInput.maxLength = 240;
        typeLabel.appendChild(typeInput);
        body.appendChild(typeLabel);

        (Array.isArray(section?.items) ? section.items : []).forEach((item, itemIndex) => {
            const row = document.createElement('article');
            row.className = 'content-editor-item';
            const header = document.createElement('div');
            header.className = 'content-editor-item-heading';
            const heading = document.createElement('strong');
            heading.textContent = `内容 ${itemIndex + 1}`;
            const category = document.createElement('span');
            category.textContent = item?.category || '未分类';
            header.append(heading, category);
            row.appendChild(header);

            const meta = document.createElement('div');
            meta.className = 'content-editor-meta-fields';
            [['category', '分类'], ['number', '题号'], ['filename_stem', '文件名主体']].forEach(([field, labelText]) => {
                const label = document.createElement('label');
                label.className = 'content-editor-field';
                const labelTextNode = document.createElement('span');
                labelTextNode.textContent = labelText;
                const input = document.createElement('input');
                input.type = field === 'number' ? 'text' : 'text';
                input.value = contentEditorValue(item?.[field]);
                input.maxLength = field === 'filename_stem' ? 120 : 240;
                input.dataset.contentSection = String(sectionIndex);
                input.dataset.contentItem = String(itemIndex);
                input.dataset.contentField = field;
                label.append(labelTextNode, input);
                meta.appendChild(label);
            });
            row.appendChild(meta);

            const textLabel = document.createElement('label');
            textLabel.className = 'content-editor-field content-editor-text-field';
            const textLabelText = document.createElement('span');
            textLabelText.textContent = '文本内容';
            const textarea = document.createElement('textarea');
            textarea.rows = 3;
            textarea.maxLength = 20000;
            textarea.value = contentEditorValue(item?.text);
            textarea.dataset.contentSection = String(sectionIndex);
            textarea.dataset.contentItem = String(itemIndex);
            textarea.dataset.contentField = 'text';
            textLabel.append(textLabelText, textarea);
            row.appendChild(textLabel);
            body.appendChild(row);
        });
        details.appendChild(body);
        list.appendChild(details);
    });
}

function handleContentEditorInput(event) {
    const target = event.target;
    if (!target?.dataset?.contentField || !Array.isArray(contentDraft)) return;
    const sectionIndex = Number(target.dataset.contentSection);
    const itemIndex = target.dataset.contentItem === undefined ? null : Number(target.dataset.contentItem);
    const field = target.dataset.contentField;
    const section = contentDraft[sectionIndex];
    if (!section) return;
    if (itemIndex === null) {
        if (field === 'doc_type') section.doc_type = target.value;
    } else if (section.items?.[itemIndex]) {
        section.items[itemIndex][field] = target.value;
    }
    section.item_count = Array.isArray(section.items) ? section.items.length : 0;
    contentEditorDirty = true;
    const dirtyEl = $('content-editor-dirty');
    if (dirtyEl) dirtyEl.hidden = false;
    const saveButton = $('content-save-btn');
    if (saveButton) saveButton.disabled = !currentSession || isGenerating || isRestarting;
    updateContentEditorSectionHeadings();
}

function updateContentEditorSectionHeadings() {
    $('content-editor-list')?.querySelectorAll('.content-editor-section-card').forEach((card, index) => {
        const input = card.querySelector('[data-content-field="doc_type"]');
        const title = card.querySelector('.content-editor-section-title');
        if (input && title) title.textContent = `${index + 1}. ${input.value || '未命名题型'}`;
    });
}

async function saveEditedContent() {
    if (!currentSession || !contentEditorDirty) return true;
    if (isGenerating) {
        showToast('生成过程中不能修改文档内容', 'warning');
        return false;
    }
    const button = $('content-save-btn');
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
    }
    try {
        const response = await fetch(apiUrl(`/api/session/${encodeURIComponent(currentSession.session_id)}/content`), {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                parse_results: contentDraft,
                content_version: currentSession.content_version,
            }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '内容保存失败');
        currentSession.parse_results = cloneParseResults(data.parse_results || contentDraft);
        currentSession.content_version = data.content_version || currentSession.content_version;
        contentDraft = cloneParseResults(currentSession.parse_results);
        contentEditorDirty = false;
        generatedFiles = [];
        latestCurrentResultEvent = null;
        lastDownloadEvent = null;
        generationResult = null;
        resetTaskVoiceConfiguration();
        renderContentEditor();
        renderVoiceWorkspace();
        updateSessionLabels(currentSession.source_filename, currentSession.parse_results);
        updateConfigSummary();
        persistActiveSession();
        showToast(data.changed === false ? '内容未发生变化' : '文档内容已保存，旧音频与断点已失效', 'success');
        return true;
    } catch (error) {
        console.error('保存编辑内容失败:', error);
        showToast(`内容保存失败：${error.message || '请稍后重试'}`, 'error');
        return false;
    } finally {
        if (button) {
            button.removeAttribute('aria-busy');
            button.disabled = !contentEditorDirty || !currentSession || isGenerating || isRestarting;
        }
    }
}

function ensureContentSaved() {
    if (!contentEditorDirty) return Promise.resolve(true);
    return saveEditedContent();
}

function applyWindowState(state) {
    if (!state || typeof state !== 'object') return;
    windowState = state;
    const mode = ['full', 'compact', 'hidden'].includes(state.mode) ? state.mode : 'full';
    // 主进程的恢复快捷键/再次启动可以在渲染层没有点击“退出防偷窥”时
    // 直接把窗口召回；窗口已经可见时，界面也必须退出本地隐私动作状态。
    if (mode !== 'hidden' && privacyModeActive) {
        const shouldRestoreBrowser = ['visible', 'minimized'].includes(browserVisibilityBeforePrivacy);
        const restoreBrowserMinimized = browserVisibilityBeforePrivacy === 'minimized';
        privacyModeActive = false;
        browserVisibilityBeforePrivacy = null;
        // 恢复快捷键/再次启动不会经过 togglePrivacyMode 的退出分支，必须
        // 在这里补回进入防偷窥前的浏览器状态，避免 UI 已恢复但 Chrome
        // 仍被留在隐藏状态。
        if (shouldRestoreBrowser && currentSession) {
            void setBrowserVisibility(true, { minimize: restoreBrowserMinimized });
        }
    }
    document.body?.setAttribute('data-window-mode', mode);
    const indicator = $('window-mode-indicator');
    if (indicator) indicator.textContent = mode === 'compact' ? '小窗模式' : (mode === 'hidden' ? '窗口已隐藏' : '完整窗口');
    const compactButton = $('compact-toggle-btn');
    if (compactButton) compactButton.textContent = mode === 'compact' ? '完整窗口' : '小窗';
    const compactLabel = $('sidebar-compact-label');
    if (compactLabel) compactLabel.textContent = mode === 'compact' ? '完整窗口' : '小窗';
    const sidebarCompact = $('sidebar-compact-btn');
    if (sidebarCompact) sidebarCompact.title = mode === 'compact' ? '切换到完整窗口' : '切换到小窗模式';
    const privacyButton = $('privacy-toggle-btn');
    if (privacyButton) {
        privacyButton.textContent = privacyModeActive ? '显示窗口' : '一键隐藏';
        privacyButton.title = privacyModeActive ? '退出一键隐藏，恢复窗口' : '一键隐藏应用窗口和自动化浏览器';
    }
    const privacyLabel = $('sidebar-privacy-label');
    if (privacyLabel) privacyLabel.textContent = privacyModeActive ? '显示窗口' : '一键隐藏';
    const sidebarPrivacy = $('sidebar-privacy-btn');
    if (sidebarPrivacy) {
        sidebarPrivacy.title = privacyModeActive ? '退出一键隐藏，恢复窗口' : '一键隐藏应用窗口和自动化浏览器';
    }
}

const TASK_CONTROL_LABELS = {
    idle: '准备中',
    starting: '启动中',
    running: '生成中',
    pause_requested: '正在暂停',
    paused: '已暂停',
    resume_requested: '正在恢复',
    terminating: '正在终止',
    terminated: '已终止',
    cancelled: '已取消',
    completed: '已完成',
    failed: '生成失败',
};

function renderTaskControlState() {
    const state = taskControlState || 'idle';
    const label = TASK_CONTROL_LABELS[state] || state;
    const stateEl = $('task-control-state');
    if (stateEl) stateEl.textContent = `任务状态：${label}`;
    const checkpoint = $('task-control-checkpoint');
    if (checkpoint) checkpoint.textContent = currentSession ? (lastStats?.checkpoint || '后端将在安全检查点响应控制') : '等待生成任务启动';
    const activeStates = new Set(['starting', 'running', 'pause_requested', 'paused', 'resume_requested', 'terminating']);
    const canPause = ['starting', 'running'].includes(state);
    const canResume = ['pause_requested', 'paused', 'resume_requested'].includes(state);
    const canTerminate = activeStates.has(state) && state !== 'terminating';
    const pause = $('task-pause-btn');
    const resume = $('task-resume-btn');
    const terminate = $('task-terminate-btn');
    if (pause) pause.disabled = !currentSession || !canPause || controlActionInFlight;
    if (resume) {
        resume.hidden = !canResume;
        resume.disabled = !currentSession || !canResume || controlActionInFlight;
    }
    if (terminate) terminate.disabled = !currentSession || !canTerminate || controlActionInFlight;
    const bar = $('task-control-bar');
    if (bar) {
        bar.dataset.state = state;
        bar.classList.toggle('is-paused', state === 'paused' || state === 'pause_requested');
        bar.classList.toggle('is-terminal', ['terminated', 'cancelled', 'completed', 'failed'].includes(state));
    }
}

function backendErrorMessage(detail, fallback = '请求失败') {
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (detail && typeof detail === 'object') {
        if (detail.browser_state && typeof detail.browser_state === 'object') {
            if (detail.browser_state.permission_required) {
                return '自动化浏览器需要辅助功能权限，请在系统设置中允许本应用控制窗口';
            }
            if (typeof detail.browser_state.last_error === 'string' && detail.browser_state.last_error.trim()) {
                return detail.browser_state.last_error;
            }
        }
        if (typeof detail.reason === 'string' && detail.reason.trim()) return detail.reason;
        if (typeof detail.message === 'string' && detail.message.trim()) return detail.message;
        if (typeof detail.error === 'string' && detail.error.trim()) return detail.error;
    }
    return fallback;
}

async function postTaskControl(action) {
    if (!currentSession || controlActionInFlight) return false;
    const allowedActions = new Set(['pause', 'resume', 'terminate']);
    if (!allowedActions.has(action)) return false;
    if (action === 'terminate') {
        const confirmed = await showConfirmDialog({
            kicker: '终止任务',
            title: '终止并保留已生成文件？',
            message: '当前任务会在安全检查点停止，已经生成的音频会保留并进入历史记录。',
            detail: '未完成内容不会自动删除；如需重新生成，请之后新建任务。',
            tone: 'danger',
            confirmLabel: '终止任务',
        });
        if (!confirmed) return false;
    }
    controlActionInFlight = true;
    renderTaskControlState();
    try {
        let result;
        const ipcAction = window.electronAPI?.task?.[action];
        if (isElectron && typeof ipcAction === 'function') {
            const wrapped = await ipcAction(currentSession.session_id);
            if (!wrapped?.ok) throw new Error(backendErrorMessage(wrapped?.data?.detail || wrapped?.reason, `无法${action}任务`));
            result = wrapped.data;
        } else {
            const response = await fetch(apiUrl(`/api/session/${encodeURIComponent(currentSession.session_id)}/${action}`), { method: 'POST' });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `无法${action}任务`);
            result = data;
        }
        if (result?.control_state) taskControlState = result.control_state;
        renderTaskControlState();
        persistActiveSession();
        return true;
    } catch (error) {
        showToast(error.message || '任务控制请求失败', 'error');
        return false;
    } finally {
        controlActionInFlight = false;
        renderTaskControlState();
    }
}

function renderBrowserState() {
    const state = browserState || {};
    const visibility = state.visibility || 'unavailable';
    const labels = {
        visible: '自动化浏览器：已显示',
        hidden: '自动化浏览器：已隐藏',
        minimized: '自动化浏览器：已最小化',
        manual_required: '自动化浏览器：需要辅助功能权限',
        unavailable: '自动化浏览器：未启动或不可控制',
    };
    const label = $('browser-state-label');
    if (label) label.textContent = labels[visibility] || `自动化浏览器：${visibility}`;
    const detail = $('browser-state-detail');
    if (detail) detail.textContent = state.permission_required
        ? '请在系统设置中允许本应用控制辅助功能'
        : (state.last_error || '');
    const bar = $('browser-state-bar');
    if (bar) {
        bar.dataset.state = visibility;
        bar.classList.toggle('is-warning', visibility === 'manual_required');
        bar.classList.toggle('is-hidden', visibility === 'hidden');
    }
    const toggle = $('browser-toggle-btn');
    const available = Boolean(currentSession) && ['visible', 'hidden', 'minimized'].includes(visibility);
    if (toggle) {
        toggle.hidden = !currentSession;
        toggle.disabled = !available || state.permission_required;
        toggle.textContent = visibility === 'hidden' || visibility === 'minimized' ? '显示浏览器' : '隐藏浏览器';
        toggle.title = state.permission_required ? '需要辅助功能权限' : '显示或隐藏自动化浏览器';
    }
}

async function setBrowserVisibility(visible, options = {}) {
    if (!currentSession) return false;
    const minimize = visible && options?.minimize === true;
    try {
        let result;
        const ipcAction = window.electronAPI?.browser?.[visible ? 'show' : 'hide'];
        if (isElectron && typeof ipcAction === 'function') {
            const wrapped = visible
                ? await ipcAction(currentSession.session_id, { minimize })
                : await ipcAction(currentSession.session_id);
            if (!wrapped?.ok) {
                const detail = wrapped?.data?.detail;
                if (detail && typeof detail === 'object' && detail.browser_state) {
                    browserState = detail.browser_state;
                    renderBrowserState();
                }
                throw new Error(backendErrorMessage(detail || wrapped?.reason, '自动化浏览器窗口不可控制'));
            }
            result = wrapped.data;
        } else {
            const request = { method: 'POST' };
            if (visible && minimize) {
                request.headers = { 'Content-Type': 'application/json' };
                request.body = JSON.stringify({ minimize: true });
            }
            const response = await fetch(apiUrl(`/api/session/${encodeURIComponent(currentSession.session_id)}/browser/${visible ? 'show' : 'hide'}`), request);
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                if (data?.detail && typeof data.detail === 'object' && data.detail.browser_state) {
                    browserState = data.detail.browser_state;
                    renderBrowserState();
                }
                throw new Error(typeof data.detail === 'string' ? data.detail : '自动化浏览器窗口不可控制');
            }
            result = data;
        }
        if (result?.browser_state) browserState = result.browser_state;
        renderBrowserState();
        return true;
    } catch (error) {
        const detail = error.message || '自动化浏览器窗口控制失败';
        showToast(detail, 'warning');
        renderBrowserState();
        return false;
    }
}

async function requestBrowserState() {
    if (!currentSession) return false;
    let requestSucceeded = false;
    try {
        if (isElectron && typeof window.electronAPI?.browser?.getState === 'function') {
            const wrapped = await window.electronAPI.browser.getState(currentSession.session_id);
            requestSucceeded = Boolean(wrapped?.ok);
            if (wrapped?.ok && wrapped.data?.browser_state) browserState = wrapped.data.browser_state;
            else if (wrapped?.data?.detail?.browser_state) browserState = wrapped.data.detail.browser_state;
        } else {
            const response = await fetch(apiUrl(`/api/session/${encodeURIComponent(currentSession.session_id)}/browser`));
            const data = await response.json().catch(() => ({}));
            requestSucceeded = response.ok;
            if (response.ok && data.browser_state) browserState = data.browser_state;
            else if (data?.detail?.browser_state) browserState = data.detail.browser_state;
        }
        renderBrowserState();
        return requestSucceeded;
    } catch (_) {
        renderBrowserState();
        return false;
    }
}

async function togglePrivacyMode() {
    if (!isElectron) {
        showToast('一键隐藏只在桌面应用中可用', 'warning');
        return;
    }
    if (privacyModeActive) {
        privacyModeActive = false;
        const showResult = await window.electronAPI.showWindow();
        if (showResult?.state) applyWindowState(showResult.state);
        let browserRestored = true;
        if (browserVisibilityBeforePrivacy === 'visible' || browserVisibilityBeforePrivacy === 'minimized') {
            browserRestored = await setBrowserVisibility(true, {
                minimize: browserVisibilityBeforePrivacy === 'minimized',
            });
        }
        browserVisibilityBeforePrivacy = null;
        if (desktopSettings?.privacy?.auto_resume_on_restore && ['paused', 'pause_requested'].includes(taskControlState)) {
            void postTaskControl('resume');
        }
        if (!showResult?.success || !browserRestored) {
            showToast(
                !showResult?.success
                    ? '应用窗口未能恢复，请使用恢复快捷键或再次启动应用'
                    : '应用窗口已恢复，但自动化浏览器仍未能显示',
                'warning',
            );
        } else {
            showToast('已退出一键隐藏');
        }
        return;
    }

    // 进入防偷窥前重新读取一次原生窗口状态，不能只相信渲染层上一次
    // 的缓存；尤其是恢复快捷键、权限变更或后端重连后，缓存可能已经过期。
    const browserStateFresh = await requestBrowserState();
    const previousBrowserVisibility = browserState.visibility || 'unavailable';
    const shouldRestoreBrowser = ['visible', 'minimized'].includes(previousBrowserVisibility);
    // 防偷窥模式的前置条件是浏览器已经由后端确认隐藏。隐藏失败时不应
    // 继续隐藏小猪窗口，否则用户会得到“已保护”的错误承诺。
    // 运行中的任务即使状态为 unavailable 也必须尝试隐藏并在失败时停止；
    // 非运行任务没有已启动的自动化浏览器时则允许单独隐藏小猪窗口。
    const mustHideBrowser = currentSession && (
        !browserStateFresh
        || isGenerating
        || ['visible', 'minimized', 'manual_required'].includes(previousBrowserVisibility)
    );
    if (mustHideBrowser && !(await setBrowserVisibility(false))) {
        browserVisibilityBeforePrivacy = null;
        return;
    }

    const result = await window.electronAPI.hideWindow(true);
    if (!result?.success) {
        if (currentSession && shouldRestoreBrowser) {
            await setBrowserVisibility(true, {
                minimize: previousBrowserVisibility === 'minimized',
            });
        }
        browserVisibilityBeforePrivacy = null;
        showToast('应用窗口未能隐藏，一键隐藏未开启', 'warning');
        return;
    }
    privacyModeActive = true;
    browserVisibilityBeforePrivacy = previousBrowserVisibility;
    if (desktopSettings?.privacy?.auto_pause_on_hide && isGenerating) {
        void postTaskControl('pause');
    }
    if (result?.state) applyWindowState(result.state);
    showToast('已一键隐藏，恢复快捷键可重新显示窗口');
}

async function toggleCompactMode() {
    if (!isElectron) {
        document.body?.classList.toggle('web-compact-preview');
        showToast('网页模式已切换紧凑布局');
        return;
    }
    const nextMode = windowState?.mode === 'compact' ? 'full' : 'compact';
    const result = await window.electronAPI.setWindowMode(nextMode);
    if (result?.success && result.state) applyWindowState(result.state);
}

async function hideApplicationWindow() {
    if (!isElectron) return;
    if (desktopSettings?.privacy?.auto_pause_on_hide && isGenerating) void postTaskControl('pause');
    const result = await window.electronAPI.hideWindow();
    if (result?.state) applyWindowState(result.state);
}

async function showApplicationWindow() {
    if (!isElectron) return;
    const result = await window.electronAPI.showWindow();
    if (result?.state) applyWindowState(result.state);
    if (desktopSettings?.privacy?.auto_resume_on_restore && ['paused', 'pause_requested'].includes(taskControlState)) {
        void postTaskControl('resume');
    }
}

function maybeRestoreAfterTaskTerminal(kind) {
    if (!isElectron || !privacyModeActive) return;
    const settingKey = kind === 'failure' ? 'restore_on_failure' : 'restore_on_complete';
    if (desktopSettings?.privacy?.[settingKey] === true) {
        void showApplicationWindow();
    }
}

function handleGlobalShortcut(payload) {
    const action = typeof payload === 'string' ? payload : payload?.action;
    if (action === 'privacy-toggle') void togglePrivacyMode();
    else if (action === 'task-pause-resume') {
        if (!currentSession) return;
        if (['paused', 'pause_requested'].includes(taskControlState)) void postTaskControl('resume');
        else if (['starting', 'running', 'resume_requested'].includes(taskControlState)) void postTaskControl('pause');
        else if (taskControlState === 'resume_requested') void postTaskControl('resume');
    } else if (action === 'task-terminate') {
        if (!currentSession) return;
        const activeStates = new Set(['starting', 'running', 'pause_requested', 'paused', 'resume_requested']);
        if (activeStates.has(taskControlState)) void postTaskControl('terminate');
    } else if (action === 'compact-toggle') void toggleCompactMode();
}

function populateSettingsDialog() {
    const settings = desktopSettings || {};
    const privacy = settings.privacy || {};
    const browser = settings.browser || {};
    const task = settings.task || {};
    const shortcuts = settings.shortcuts || {};
    const startupModeInput = $('setting-startup-mode');
    if (startupModeInput) {
        // 防御：确保两个启动模式选项都存在，避免旧设置或异常 DOM 导致只有“完整窗口”
        const hasFull = Boolean(startupModeInput.querySelector('option[value="full"]'));
        const hasCompact = Boolean(startupModeInput.querySelector('option[value="compact"]'));
        if (!hasFull || !hasCompact) {
            startupModeInput.replaceChildren();
            const fullOpt = document.createElement('option');
            fullOpt.value = 'full';
            fullOpt.textContent = '完整窗口';
            const compactOpt = document.createElement('option');
            compactOpt.value = 'compact';
            compactOpt.textContent = '小窗工作台';
            startupModeInput.append(fullOpt, compactOpt);
        }
        startupModeInput.value = settings.window?.startup_mode === 'compact' ? 'compact' : 'full';
        window.WordTTSUI?.syncSelect(startupModeInput);
    }
    [['setting-auto-pause', privacy.auto_pause_on_hide], ['setting-auto-resume', privacy.auto_resume_on_restore],
        ['setting-keep-browser-hidden', privacy.keep_browser_hidden], ['setting-hide-after-login', browser.hide_after_login],
        ['setting-show-on-login', browser.show_on_login], ['setting-allow-system-chrome', browser.allow_system_chrome],
        ['setting-restore-complete', privacy.restore_on_complete], ['setting-restore-failure', privacy.restore_on_failure],
        ['setting-close-browser', task.close_browser_on_finish], ['setting-keep-logs', task.keep_logs],
        ['setting-completion-notification', task.completion_notification], ['setting-open-output', task.open_output_dir],
        ['setting-keep-history', task.keep_history]].forEach(([id, value]) => {
        const input = $(id);
        if (input) input.checked = value !== false;
    });
    [['setting-recover-shortcut', shortcuts.recover], ['setting-privacy-shortcut', shortcuts.privacy],
        ['setting-pause-shortcut', shortcuts.pause_resume], ['setting-terminate-shortcut', shortcuts.terminate],
        ['setting-compact-shortcut', shortcuts.compact], ['setting-retry-count', task.retry_count],
        ['setting-timeout', task.operation_timeout_seconds], ['setting-history-limit', task.history_limit]].forEach(([id, value]) => {
        const input = $(id);
        if (input) input.value = value ?? '';
    });
    const status = $('settings-status');
    if (status) status.textContent = isElectron ? '修改后点击保存生效。恢复窗口快捷键不能留空。' : '网页模式不保存桌面设置。';
}

function formatShortcutFromEvent(event) {
    const parts = [];
    if (event.ctrlKey || event.metaKey) parts.push('CommandOrControl');
    if (event.altKey) parts.push('Alt');
    if (event.shiftKey) parts.push('Shift');
    const key = event.key;
    if (['Control', 'Meta', 'Alt', 'Shift', 'OS'].includes(key)) {
        return parts.length ? parts.join('+') : null;
    }
    let keyName = '';
    // Use code for physical key when possible to avoid Shift case confusion.
    const code = event.code || '';
    if (/^Key[A-Z]$/.test(code)) {
        keyName = code.slice(3);
    } else if (/^Digit[0-9]$/.test(code)) {
        keyName = code.slice(5);
    } else if (/^Numpad[0-9]$/.test(code)) {
        keyName = code.replace('Numpad', 'num');
    } else if (/^F\d+$/.test(key)) {
        keyName = key.toUpperCase();
    } else if (key === ' ') {
        keyName = 'Space';
    } else if (key.length === 1) {
        keyName = key.toUpperCase();
    } else {
        const map = {
            Enter: 'Enter', Tab: 'Tab', Escape: 'Escape', Backspace: 'Backspace', Delete: 'Delete',
            ArrowUp: 'Up', ArrowDown: 'Down', ArrowLeft: 'Left', ArrowRight: 'Right',
            Home: 'Home', End: 'End', PageUp: 'PageUp', PageDown: 'PageDown',
            Insert: 'Insert', CapsLock: 'CapsLock',
            '+': 'Plus', '-': 'Minus', '=': 'Plus', _: 'Minus', ',': 'Comma', '.': 'Period',
            '/': 'Slash', '\\': 'Backslash', ';': 'Semicolon', "'": 'Quote',
            '[': 'BracketLeft', ']': 'BracketRight', '`': 'Backquote', ' ': 'Space'
        };
        keyName = map[key] || key;
    }
    if (!keyName) return parts.length ? parts.join('+') : null;
    // Avoid duplicate when Shift already adds case: e.g., "A" vs "Shift+A" we still want Shift+A
    if (!parts.includes(keyName)) parts.push(keyName);
    else if (keyName === 'Plus' || keyName === 'Minus') parts.push(keyName);
    return parts.join('+');
}

function attachShortcutCapture(input) {
    if (!input || input.dataset.shortcutCaptureAttached === '1') return;
    input.dataset.shortcutCaptureAttached = '1';
    input.addEventListener('focus', () => {
        input.select();
        input.dataset.previousValue = input.value;
    });
    input.addEventListener('blur', () => {
        // 空值仅恢复键允许留空
        if (input.dataset.shortcutField === 'recover' && !String(input.value || '').trim()) {
            // 恢复快捷键不能为空，恢复旧值
            input.value = input.dataset.previousValue || 'CommandOrControl+Alt+Shift+W';
        }
    });
    input.addEventListener('keydown', (event) => {
        if (event.key === 'Tab' && !event.ctrlKey && !event.metaKey && !event.altKey) {
            return;
        }
        if (event.key === 'Escape' && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey) {
            event.preventDefault();
            input.value = input.dataset.previousValue || '';
            input.blur();
            return;
        }
        if (event.key === 'Backspace' && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey) {
            event.preventDefault();
            input.value = '';
            input.dispatchEvent(new Event('input', { bubbles: true }));
            return;
        }
        const accel = formatShortcutFromEvent(event);
        if (!accel) return;
        const hasKey = accel.split('+').some(part => !['CommandOrControl', 'Alt', 'Shift'].includes(part));
        if (!hasKey) {
            // 仅修饰键：显示当前修饰状态，等待最终按键
            event.preventDefault();
            input.value = accel;
            return;
        }
        event.preventDefault();
        input.value = accel;
        input.dispatchEvent(new Event('input', { bubbles: true }));
    });
}

function initShortcutCapture() {
    $$('.shortcut-input').forEach(attachShortcutCapture);
}

function switchSettingsTab(tab) {
    const target = String(tab || 'window').trim() || 'window';
    $$('.settings-nav-item').forEach(btn => {
        const active = btn.dataset.settingsTab === target;
        btn.classList.toggle('is-active', active);
        btn.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    $$('.settings-pane').forEach(pane => {
        pane.classList.toggle('is-active', pane.dataset.settingsPane === target);
    });
}

function openSettingsDialog() {
    if (!isElectron || !desktopSettings) {
        showToast('桌面设置只在 Electron 应用中可用', 'warning');
        return;
    }
    populateSettingsDialog();
    initShortcutCapture();
    switchSettingsTab('window');
    const dialog = $('settings-dialog');
    if (dialog?.showModal) dialog.showModal();
}

async function saveSettingsFromDialog() {
    if (!isElectron || !window.electronAPI.settings) return;
    const bool = id => Boolean($(id)?.checked);
    const value = id => String($(id)?.value || '').trim();
    const integer = (id, fallback, minimum, maximum) => {
        const number = Number($(id)?.value);
        return Number.isFinite(number)
            ? Math.max(minimum, Math.min(maximum, Math.round(number)))
            : fallback;
    };
    const retryCount = integer('setting-retry-count', 1, 0, 10);
    const timeout = integer('setting-timeout', 120, 10, 3600);
    const historyLimit = integer('setting-history-limit', 20, 1, 20);
    const patch = {
        window: {
            startup_mode: ['full', 'compact'].includes($('setting-startup-mode')?.value)
                ? $('setting-startup-mode').value
                : 'full',
        },
        privacy: {
            auto_pause_on_hide: bool('setting-auto-pause'),
            auto_resume_on_restore: bool('setting-auto-resume'),
            keep_browser_hidden: bool('setting-keep-browser-hidden'),
            restore_on_complete: bool('setting-restore-complete'),
            restore_on_failure: bool('setting-restore-failure'),
        },
        browser: {
            show_on_login: bool('setting-show-on-login'),
            hide_after_login: bool('setting-hide-after-login'),
            allow_system_chrome: bool('setting-allow-system-chrome'),
        },
        task: {
            retry_count: retryCount,
            operation_timeout_seconds: timeout,
            keep_logs: bool('setting-keep-logs'),
            completion_notification: bool('setting-completion-notification'),
            open_output_dir: bool('setting-open-output'),
            close_browser_on_finish: bool('setting-close-browser'),
            keep_history: bool('setting-keep-history'),
            history_limit: historyLimit,
        },
        shortcuts: {
            recover: value('setting-recover-shortcut'),
            privacy: value('setting-privacy-shortcut'),
            pause_resume: value('setting-pause-shortcut'),
            terminate: value('setting-terminate-shortcut'),
            compact: value('setting-compact-shortcut'),
        },
    };
    const status = $('settings-status');
    try {
        const result = await window.electronAPI.settings.update(patch);
        if (!result?.success) throw new Error(result?.reason || '设置保存失败');
        updateDesktopSettingsSnapshot(result.settings);
        $('settings-dialog')?.close();
        showToast('桌面设置已保存', 'success');
    } catch (error) {
        if (status) status.textContent = `保存失败：${error.message || '快捷键可能已被占用'}`;
        showToast(`设置保存失败：${error.message || '请检查快捷键'}`, 'error');
        populateSettingsDialog();
    }
}

async function resetSettingsFromDialog() {
    const confirmed = await showConfirmDialog({
        kicker: '桌面设置',
        title: '恢复全部默认设置？',
        message: '窗口尺寸、快捷键和任务策略会回到默认值。',
        detail: '当前文档和已经生成的音频不会受到影响。',
        tone: 'warning',
        confirmLabel: '恢复默认',
    });
    if (!confirmed) return;
    try {
        const result = await window.electronAPI.settings.reset();
        if (!result?.success) throw new Error(result?.reason || '恢复默认失败');
        updateDesktopSettingsSnapshot(result.settings);
        populateSettingsDialog();
        const windowResult = await window.electronAPI.getWindowState();
        if (windowResult?.success) applyWindowState(windowResult.state);
        showToast('已恢复默认设置', 'success');
    } catch (error) {
        showToast(`恢复默认失败：${error.message || '请稍后重试'}`, 'error');
    }
}

function bindWindowAndControlEvents() {
    $('compact-toggle-btn')?.addEventListener('click', () => void toggleCompactMode());
    $('privacy-toggle-btn')?.addEventListener('click', () => void togglePrivacyMode());
    $('sidebar-compact-btn')?.addEventListener('click', () => void toggleCompactMode());
    $('sidebar-privacy-btn')?.addEventListener('click', () => void togglePrivacyMode());
    $('sidebar-settings-btn')?.addEventListener('click', openSettingsDialog);
    $('hide-window-btn')?.addEventListener('click', () => {
        if (windowState?.mode === 'hidden') void showApplicationWindow();
        else void hideApplicationWindow();
    });
    $('browser-toggle-btn')?.addEventListener('click', () => {
        const hidden = ['hidden', 'minimized'].includes(browserState?.visibility);
        void setBrowserVisibility(hidden);
    });
    $('settings-btn')?.addEventListener('click', openSettingsDialog);
    $('task-pause-btn')?.addEventListener('click', () => void postTaskControl('pause'));
    $('task-resume-btn')?.addEventListener('click', () => void postTaskControl('resume'));
    $('task-terminate-btn')?.addEventListener('click', () => void postTaskControl('terminate'));
    $('content-editor-list')?.addEventListener('input', handleContentEditorInput);
    $('settings-form')?.addEventListener('submit', event => {
        event.preventDefault();
        void saveSettingsFromDialog();
    });
    $('settings-close-btn')?.addEventListener('click', () => $('settings-dialog')?.close());
    $('settings-cancel-btn')?.addEventListener('click', () => $('settings-dialog')?.close());
    $('settings-reset-btn')?.addEventListener('click', () => void resetSettingsFromDialog());
    $$('.settings-nav-item').forEach(btn => {
        btn.addEventListener('click', () => switchSettingsTab(btn.dataset.settingsTab));
    });
    initShortcutCapture();
    if (isElectron) {
        window.electronAPI.onWindowState?.(applyWindowState);
        window.electronAPI.onGlobalShortcut?.(handleGlobalShortcut);
        window.electronAPI.getWindowState?.().then(result => {
            if (result?.success) applyWindowState(result.state);
        }).catch(() => {});
    }
    renderTaskControlState();
    renderBrowserState();
}

// ============================================================================
// SSE 进度流
// ============================================================================

function connectSSE(sessionId) {
    clearSSEReconnectTimer();
    const connectionToken = ++sseConnectionToken;
    if (eventSource) {
        eventSource.close();
    }

    let sseClosed = false;
    let recoveryNoticePending = sseRetryCount > 0;

    const source = new EventSource(apiUrl(`/api/progress/${sessionId}`));
    eventSource = source;

    source.onopen = () => {
        if (connectionToken !== sseConnectionToken) {
            source.close();
            return;
        }
        // 连续稳定一段时间后视为一次成功连接，避免长任务把互不相关的
        // 短暂断线累计到最大重试次数。
        sseStableTimer = setTimeout(() => {
            sseStableTimer = null;
            if (connectionToken === sseConnectionToken && eventSource === source) {
                sseRetryCount = 0;
            }
        }, 10000);
    };

    source.onmessage = (e) => {
        if (connectionToken !== sseConnectionToken || currentSession?.session_id !== sessionId) return;
        try {
            const data = JSON.parse(e.data);
            handleSSEEvent(data);
            if (recoveryNoticePending) {
                recoveryNoticePending = false;
                addLogEntry({
                    level: 'success',
                    stage: 'system',
                    kind: 'notice',
                    status: 'success',
                    key: 'connection:status',
                    title: '生成服务连接已恢复',
                    detail: '任务记录与进度已重新同步',
                });
            }
        } catch (err) {
            console.error('SSE 解析错误:', err);
        }
    };

    source.onerror = () => {
        if (connectionToken !== sseConnectionToken || currentSession?.session_id !== sessionId) {
            source.close();
            return;
        }
        console.error('SSE 连接错误');
        if (!sseClosed && isGenerating) {
            sseClosed = true;
            if (sseStableTimer) {
                clearTimeout(sseStableTimer);
                sseStableTimer = null;
            }
            source.close();
            if (eventSource === source) eventSource = null;

            // 超过最大重试次数，判定后端不可用
            sseRetryCount++;
            if (sseRetryCount >= SSE_MAX_RETRIES) {
                resetGenerateState();
                generationResult = 'error';
                $('gen-title').textContent = '连接中断';
                $('generation-file-name').textContent = `「${currentSession?.source_filename || '当前文档'}」的生成连接已中断；已完成的记录会继续保留。`;
                $('status-text').textContent = '与服务器连接中断，请检查后端服务';
                setServiceState('error', '服务连接中断');
                $('retry-service-btn').hidden = false;
                setGenerationVisualState('error');
                showGenerationRecovery('与生成服务的连接已中断。请确认服务正常后重试，或返回配置页。');
                addLogEntry({
                    level: 'error',
                    stage: 'complete',
                    kind: 'summary',
                    status: 'error',
                    key: 'connection:status',
                    title: '生成服务连接中断',
                    detail: '多次尝试仍无法恢复连接，请检查服务状态后重试',
                });
                showToast('与服务器连接中断，请重试');
                return;
            }

            // 指数退避重连
            const delay = Math.min(2000 * Math.pow(1.5, sseRetryCount - 1), 10000);
            sseReconnectTimer = setTimeout(() => {
                sseReconnectTimer = null;
                if (connectionToken === sseConnectionToken && isGenerating && currentSession?.session_id === sessionId) {
                    addLogEntry({
                        level: 'warn',
                        stage: 'system',
                        kind: 'notice',
                        status: 'warning',
                        key: 'connection:status',
                        title: '正在恢复生成服务连接',
                        detail: `第 ${sseRetryCount} / ${SSE_MAX_RETRIES} 次尝试，现有任务记录会继续保留`,
                    });
                    connectSSE(sessionId);
                }
            }, delay);
        }
    };
}

function handleSSEEvent(event) {
    switch (event.type) {
        case 'log_init':
            addLogEntries(Array.isArray(event.entries) ? event.entries : []);
            break;

        case 'log':
            addLogEntry(event.entry);
            break;

        case 'stats':
            lastStats = event;
            updateProgress(event);
            updateStats(event);
            break;

        case 'control_state':
            taskControlState = event.state || taskControlState;
            if (event.checkpoint) lastStats = { ...(lastStats || {}), checkpoint: event.checkpoint };
            renderTaskControlState();
            if (['pause_requested', 'paused', 'resume_requested', 'terminating'].includes(taskControlState)) {
                setGenerationVisualState(taskControlState);
            } else if (['starting', 'running'].includes(taskControlState)) {
                setGenerationVisualState('running');
            }
            persistActiveSession();
            break;

        case 'browser_state':
            browserState = { ...(browserState || {}), ...event };
            delete browserState.type;
            delete browserState.event_seq;
            renderBrowserState();
            break;

        case 'content_updated':
            if (event.content_version && currentSession) {
                currentSession.content_version = event.content_version;
                persistActiveSession();
            }
            break;

        case 'status':
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
            handleDone(event);
            break;

        case 'cancelled':
            if (!logEntriesByKey.has('task:summary')) {
                addLogEntry({
                    level: 'warn',
                    stage: 'complete',
                    kind: 'summary',
                    status: 'warning',
                    key: 'task:summary',
                    title: '任务已取消',
                    detail: `已完成 ${event.completed || 0} / ${event.total || 0} 条`,
                    duration_ms: event.duration_ms,
                });
            }
            generationResult = 'cancelled';
            resetGenerateState();
            $('gen-title').textContent = '任务已取消';
            $('generation-file-name').textContent = `已取消「${currentSession?.source_filename || '当前文档'}」的生成任务。`;
            $('status-text').textContent = '生成任务已取消';
            setGenerationVisualState('stopped');
            taskControlState = 'cancelled';
            renderTaskControlState();
            persistActiveSession();
            showToast('任务已取消');
            break;

        case 'terminated':
            handleTerminated(event);
            break;

        case 'error':
            if (!logEntriesByKey.has('task:summary')) {
                addLogEntry({
                    level: 'error',
                    stage: 'complete',
                    kind: 'summary',
                    status: 'error',
                    key: 'task:summary',
                    title: '生成任务未能完成',
                    detail: event.msg || '生成服务返回了未说明的错误',
                    duration_ms: event.duration_ms,
                });
            }
            showToast(`错误: ${event.msg}`);
            generationResult = 'error';
            taskControlState = 'failed';
            renderTaskControlState();
            resetGenerateState();
            if (eventSource) {
                eventSource.close();
                eventSource = null;
            }
            clearSSEReconnectTimer();
            sseConnectionToken++;
            $('gen-title').textContent = '生成出错';
            $('generation-file-name').textContent = `「${currentSession?.source_filename || '当前文档'}」生成遇到问题；可查看任务详情后重试。`;
            $('status-text').textContent = `错误: ${event.msg}`;
            setGenerationVisualState('error');
            persistActiveSession();
            showGenerationRecovery(`生成出错：${event.msg}`);
            maybeRestoreAfterTaskTerminal('failure');
            break;

        case 'end':
            resetGenerateState();
            if (eventSource) {
                eventSource.close();
                eventSource = null;
            }
            clearSSEReconnectTimer();
            sseConnectionToken++;
            // 如果未收到 done 或 error 事件，说明生成异常终止
            if (generationResult === null) {
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
                showGenerationRecovery('生成任务意外停止。你可以重试，或返回配置页检查设置。');
            }
            renderTaskControlState();
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
    $('log-summary').textContent = '展开查看阶段记录和异常项';
    $$('#log-stage-rail [data-log-stage]').forEach(stage => {
        stage.classList.remove('is-active', 'is-complete', 'is-warning', 'is-error');
        const stageLabel = LOG_STAGE_LABELS[stage.dataset.logStage] || stage.textContent.trim();
        stage.setAttribute('aria-label', `${stageLabel}：未开始`);
        stage.removeAttribute('aria-current');
    });
    setLogFilter('all');
    setLogAutoFollow(true);
    // 主进度卡是生成页的默认关注点，时间线保留为可展开的诊断详情。
    setLogDetailsExpanded(false);
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

function updateProgress(event) {
    const total = integerProgressCount(event.total);
    const completed = integerProgressCount(event.completed, total);
    const failed = integerProgressCount(event.failed, total);
    const processed = integerProgressCount(
        event.processed ?? (completed + failed),
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
    $('progress-bar').style.width = `${visualPct}%`;
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(visualPct));
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
}

// ============================================================================
// 文件列表更新
// ============================================================================

function updateFileList(event) {
    if (event.file_list && event.file_list.length > 0) {
        generatedFiles = event.file_list;
    }
}

async function openOutputAssetAfterDone(doneData = {}) {
    if (
        !isElectron
        || desktopSettings?.task?.open_output_dir !== true
        || !currentSession
        || typeof window.electronAPI?.showInFolder !== 'function'
    ) return;
    const filename = doneData.zip_available
        ? 'output.zip'
        : String(generatedFiles.find(file => file?.filename)?.filename || '').trim();
    if (!filename) return;
    try {
        const response = await fetch(apiUrl(
            `/api/file-path?session_id=${encodeURIComponent(currentSession.session_id)}`
            + `&filename=${encodeURIComponent(filename)}`,
        ));
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload.path) {
            throw new Error(payload.detail || '输出文件尚未准备好');
        }
        if (!await window.electronAPI.showInFolder(payload.path)) {
            throw new Error('系统未允许打开输出目录');
        }
    } catch (error) {
        showToast(`输出目录未能打开：${error.message || '请手动下载文件'}`, 'warning');
    }
}

function showTaskCompletionToast(message, tone = 'success') {
    if (desktopSettings?.task?.completion_notification !== false) {
        showToast(message, tone);
    }
}

// ============================================================================
// Step 4: 完成
// ============================================================================

function handleTerminated(event = {}) {
    if (event.file_list) updateFileList(event);
    resetGenerateState();
    generationResult = 'terminated';
    taskControlState = 'terminated';
    renderTaskControlState();
    hideGenerationRecovery();
    clearSSEReconnectTimer();
    sseConnectionToken++;
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }
    const total = Number(event.total) || summarizeParseResults(currentSession?.parse_results).total;
    const completed = Number(event.completed) || generatedFiles.length;
    const failed = Number(event.failed) || Math.max(0, total - completed);
    const resultEvent = {
        ...event,
        completed,
        failed,
        total,
        zip_available: Boolean(event.zip_available || lastDownloadEvent?.zip_available),
    };
    latestCurrentResultEvent = resultEvent;
    const context = {
        mode: 'current',
        sessionId: currentSession?.session_id,
        historyId: event.history_id || null,
        sourceFilename: currentSession?.source_filename,
        files: generatedFiles,
        completed,
        failed,
        total,
        format: lastGenerationConfig?.format || currentConfig?.format || 'mp3',
        preview: Boolean(lastGenerationConfig?.preview && total > 3),
        zipAvailable: Boolean(resultEvent.zip_available),
        failedItems: Array.isArray(event.failed_items) ? event.failed_items : [],
        terminated: true,
    };
    $('gen-title').textContent = '任务已终止，已生成文件已保留';
    $('generation-file-name').textContent = `「${currentSession?.source_filename || '当前文档'}」已在安全检查点停止，已生成的音频仍可试听和下载。`;
    $('status-text').textContent = '任务已终止，已生成文件已保留';
    setGenerationVisualState('stopped');
    buildResultPage(resultEvent, context);
    void refreshHistoryRecords({ showLoading: false });
    goToStep(4);
    persistActiveSession();
    showTaskCompletionToast(
        generatedFiles.length > 0 ? '任务已终止，已生成文件已保留' : '任务已终止，尚未生成可交付文件',
        'warning',
    );
}

function handleDone(event) {
    resetGenerateState();
    generationResult = 'done';
    taskControlState = 'completed';
    renderTaskControlState();
    hideGenerationRecovery();

    clearSSEReconnectTimer();
    sseConnectionToken++;
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }

    // 合并最后的统计数据（done 事件本身不携带 completed/failed）
    const doneData = {
        ...event,
        completed: lastStats ? lastStats.completed : (event.completed || 0),
        failed: lastStats ? lastStats.failed : (event.failed || 0),
        total: lastStats ? lastStats.total : (event.total || 0),
        failed_items: lastStats?.failed_items || event.failed_items || [],
    };
    latestCurrentResultEvent = doneData;

    // 更新生成页面状态
    const allFailed = doneData.total > 0 && doneData.failed >= doneData.total;
    $('gen-title').textContent = allFailed
        ? '本次生成未完成'
        : (doneData.failed > 0 ? '音频已部分生成' : '生成完成');
    setGenerationVisualState(allFailed ? 'error' : (doneData.failed > 0 ? 'warning' : 'done'));
    $('progress-bar').style.width = '100%';
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', '100');
    $('progress-percent').textContent = '100';
    $('progress-completed-label').textContent = '已完成';
    $('progress-completed').textContent = String(doneData.completed || 0);
    $('progress-remaining').textContent = '0';
    $('progress-failed').textContent = String(doneData.failed || 0);
    $('progress-stats').textContent = `${doneData.completed || 0} / ${doneData.total || doneData.completed || 0}`
        + (doneData.failed > 0 ? `  ·  失败 ${doneData.failed}` : '');

    // 构建结果页面
    buildResultPage(doneData);
    void refreshHistoryRecords({ showLoading: false });
    maybeRestoreAfterTaskTerminal(allFailed || doneData.failed > 0 ? 'failure' : 'complete');

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

    if (
        generatedFiles.length > 0
        && !doneData.history_id
        && desktopSettings?.task?.keep_history !== false
    ) {
        showToast('音频已完成，但历史记录保存失败，请先下载结果');
    } else {
        showTaskCompletionToast(doneData.failed > 0 ? `任务结束，${doneData.failed} 条生成失败` : '处理完成');
    }
    void openOutputAssetAfterDone(doneData);
    persistActiveSession();
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
        .concat(file?.voice_key || [])
        .filter(value => String(value ?? '').trim());

    // 兼容早期历史记录可能保存的单个 voice 字段；只接受能在当前目录
    // 精确匹配到 key 或名称的值，避免把“女声/男声”等展示文本误当成 key。
    if (!values.length && file?.voice) {
        const legacyValue = String(file.voice).trim();
        const normalizedLegacyName = legacyValue.toLocaleLowerCase('zh-CN');
        const legacyVoice = voiceCatalog.find(voice => (
            normalizeVoiceKey(voice.key) === normalizeVoiceKey(legacyValue)
            || String(voice.name || '').trim().toLocaleLowerCase('zh-CN') === normalizedLegacyName
        ));
        if (legacyVoice) values.push(legacyVoice.key);
    }

    const canonicalize = value => {
        const normalized = normalizeVoiceKey(value);
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
        playState.innerHTML = '<svg class="icon-play" width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg><svg class="icon-pause" width="12" height="12" viewBox="0 0 24 24" fill="currentColor" style="display:none"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
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

function buildResultPage(event, suppliedContext = null) {
    destroyWaveSurfers();
    const workflowSourceTotal = summarizeParseResults(currentSession?.parse_results).total;
    const context = suppliedContext || {
        mode: 'current',
        sessionId: currentSession?.session_id,
        historyId: event.history_id || null,
        sourceFilename: currentSession?.source_filename,
        files: generatedFiles,
        completed: event.completed || generatedFiles.length || 0,
        failed: event.failed || 0,
        total: workflowSourceTotal || event.total || 0,
        format: lastGenerationConfig?.format || currentConfig?.format || 'mp3',
        preview: Boolean(lastGenerationConfig?.preview && workflowSourceTotal > 3),
        zipAvailable: Boolean(event.zip_path || event.zip_available),
        failedItems: Array.isArray(event.failed_items) ? event.failed_items : [],
        terminated: Boolean(event.terminated || event.type === 'terminated'),
    };
    activeResultContext = context;
    const isHistory = context.mode === 'history';
    const isTerminated = Boolean(context.terminated);
    const resultFiles = Array.isArray(context.files) ? context.files : [];
    const success = isHistory
        ? resultFiles.length
        : (resultFiles.length || Number(context.completed) || 0);
    const missingFiles = isHistory ? Math.max(0, (Number(context.completed) || 0) - resultFiles.length) : 0;
    const failed = Math.max(0, Number(context.failed) || 0) + missingFiles;
    const resultTitle = $('result-title');
    const resultEyebrow = $('result-eyebrow');
    const resultIcon = document.querySelector('.result-success-icon');
    const generateFullBtn = $('generate-full-btn');
    const resultWarning = $('result-warning');
    const resultWarningText = $('result-warning-text');
    const failureList = $('result-failure-list');
    const retryFailedBtn = $('retry-failed-btn');
    const warningActions = document.querySelector('.result-warning-actions');
    const backToHistoryBtn = $('back-to-history-btn');
    const sourceTotal = Math.max(0, Number(context.total) || workflowSourceTotal || success + failed);
    const isPreviewResult = Boolean(context.preview);
    const failedItems = Array.isArray(context.failedItems) ? context.failedItems : [];

    if (generateFullBtn) {
        generateFullBtn.hidden = isHistory || !lastGenerationConfig?.preview || workflowSourceTotal <= 3 || success === 0;
    }
    if (backToHistoryBtn) backToHistoryBtn.hidden = !isHistory;
    if (warningActions) warningActions.hidden = isHistory;
    if (resultWarning) resultWarning.hidden = failed === 0 && !isTerminated;
    if (resultWarningText && (failed > 0 || isTerminated)) {
        resultWarningText.textContent = isTerminated
            ? (success > 0
                ? `任务已安全终止，已生成的 ${success} 个音频文件已保留；其余内容可以新建任务后继续处理。`
                : '任务已安全终止，当前没有可交付的音频文件。')
            : isHistory
            ? (missingFiles > 0
                ? `这条历史记录有 ${missingFiles} 个音频文件已不在本机，其余文件仍可试听和下载。`
                : `这条历史记录有 ${failed} 条内容未能生成，已完成的音频仍可正常使用。`)
            : (success > 0
                ? `有 ${failed} 条内容未能生成。沿用当前设置只重试失败项；修改参数后会重新生成全部内容。`
                : `本次共有 ${failed} 条内容生成失败。你可以沿用当前设置重试，或返回配置检查网络与声音设置。`);
    }
    if (retryFailedBtn) retryFailedBtn.hidden = isHistory || failed === 0 || !lastGenerationConfig;
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
    if (isTerminated) {
        if (resultEyebrow) resultEyebrow.textContent = '任务已终止';
        if (resultTitle) resultTitle.textContent = success > 0 ? '已生成文件已保留' : '任务已终止';
        if (resultIcon) resultIcon.classList.add(success > 0 ? 'has-warning' : 'has-error');
    } else if (success === 0 && failed > 0) {
        if (resultEyebrow) resultEyebrow.textContent = isPreviewResult ? '试听需要处理' : '任务需要处理';
        if (resultTitle) resultTitle.textContent = isPreviewResult ? '本次试听未能生成音频' : '本次任务未能生成音频';
        if (resultIcon) resultIcon.classList.add('has-error');
    } else if (failed > 0) {
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
        ? `「${context.sourceFilename || '未命名文档'}」可用 ${success} 个音频文件${failed > 0 ? `，${failed} 个未完成或已缺失` : ''}`
        : (isTerminated
            ? `任务已终止，已保留 ${success} 个音频文件${failed > 0 ? `，${failed} 个内容尚未完成` : ''}`
            : (isPreviewResult
                ? `本次试听生成 ${success} 个音频${failed > 0 ? `，失败 ${failed} 个` : ''}；确认效果后可继续生成完整文档`
                : `成功生成 ${success} 个音频文件${failed > 0 ? `，失败 ${failed} 个` : ''}`));
    if (!isHistory && success > 0 && !context.historyId && !isTerminated) {
        summaryText += '；本次未能写入历史记录，请先下载后再新建任务';
    }
    $('result-summary').textContent = summaryText;
    $('result-success-label').textContent = isPreviewResult ? '试听文件' : '已生成';
    $('result-success-count').textContent = String(success);
    $('result-success-caption').textContent = isPreviewResult && !isHistory
        ? `本次范围：前 ${Math.min(sourceTotal, 3)} 条`
        : '音频文件';
    $('result-secondary-label').textContent = isPreviewResult && !isHistory ? '文档总量' : '未完成';
    $('result-failed-count').textContent = String(isPreviewResult && !isHistory ? sourceTotal : failed);
    $('result-secondary-caption').textContent = isPreviewResult && !isHistory ? '完整文档内容' : '待处理内容';
    $('result-format-value').textContent = String(context.format || 'MP3').toUpperCase();

    // ZIP 卡片
    const zipCard = $('zip-card');
    const resultHero = $('result-hero');
    if (context.zipAvailable) {
        zipCard.style.display = 'flex';
        resultHero?.classList.remove('has-no-package');
        $('zip-desc').textContent = `ZIP 压缩包包含 ${success} 个已生成的音频文件`;
    } else {
        zipCard.style.display = 'none';
        resultHero?.classList.add('has-no-package');
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
        const audioUrl = isHistory
            ? apiUrl(`/api/history/${encodeURIComponent(context.recordId)}/file/${encodeURIComponent(f.filename)}`)
            : apiUrl(`/api/download/file/${encodeURIComponent(context.sessionId)}/${encodeURIComponent(f.filename)}`);

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
        dlBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg><span>下载</span>`;
        dlBtn.addEventListener('click', async () => {
            if (dlBtn.disabled) return;
            dlBtn.disabled = true;
            dlBtn.classList.add('is-busy');
            try {
                await downloadFile(f.filename, context);
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

        // 原生 Audio 负责流式播放；WaveSurfer 只在后台解码并绘制波形。
        const audio = new Audio();
        audio.preload = index < 2 ? 'auto' : 'none';
        audio.src = audioUrl;
        item._audioElement = audio;
        audioElements.push(audio);

        const waveformWrap = document.createElement('div');
        waveformWrap.className = 'waveform-wrap';

        const playBtn = document.createElement('button');
        playBtn.className = 'waveform-play-btn';
        playBtn.title = `播放 ${f.filename}`;
        playBtn.dataset.audioName = f.filename;
        playBtn.setAttribute('aria-label', `播放 ${f.filename}`);
        playBtn.innerHTML = '<svg class="icon-play" width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg><svg class="icon-pause" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="display:none"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
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
            if (audio.preload === 'none') audio.preload = 'metadata';
            if (audio.readyState === 0) {
                try { audio.load(); } catch (_) { /* ignore */ }
            }
            waveformObserver?.unobserve(item);
            waveSurfer = createWaveSurfer(
                wsContainer,
                audio,
                color,
                canvasWrap,
                readyWs => {
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
                    finishWaveformLoad();
                },
            );
            return waveSurfer;
        };

        audio.addEventListener('error', () => {
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
                await audio.play();
            } catch (error) {
                if (requestId !== audioPlayRequestToken) return;
                if (currentPlayingAudio === audio) currentPlayingAudio = null;
                playBtn.classList.remove('is-buffering');
                updatePlayIcon(playBtn, false);
                if (error?.name === 'AbortError') return;
                console.error('音频播放失败:', error);
                showToast('音频暂时无法播放，请稍后重试');
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
    playIcon.style.display = isPlaying ? 'none' : 'block';
    pauseIcon.style.display = isPlaying ? 'block' : 'none';
    const audioName = playBtn.dataset.audioName ? ` ${playBtn.dataset.audioName}` : '';
    playBtn.setAttribute('aria-label', `${isPlaying ? '暂停' : '播放'}${audioName}`);
    playBtn.title = `${isPlaying ? '暂停' : '播放'}${audioName}`;
}

/**
 * 使用 wavesurfer.js 绘制波形。播放由传入的原生 Audio 元素负责，
 * 因此完整音频的 fetch/decode 不再阻塞首次播放。
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
        'dialog-error': '系统文件对话框未能打开。',
        'copy-error': '无法把文件复制到所选位置。',
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

async function saveNativeFile(sourcePath, suggestedName) {
    try {
        const result = await window.electronAPI.saveFileByPath(sourcePath, suggestedName);
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

async function downloadZip(context = activeResultContext) {
    const target = context || (currentSession ? {
        mode: 'current',
        sessionId: currentSession.session_id,
        sourceFilename: currentSession.source_filename,
    } : null);
    if (!target) return;
    const isHistoryDownload = target.mode === 'history';
    const pathEndpoint = isHistoryDownload
        ? `/api/history/${encodeURIComponent(target.recordId)}/file-path?filename=output.zip`
        : `/api/file-path?session_id=${encodeURIComponent(target.sessionId)}&filename=output.zip`;
    const browserEndpoint = isHistoryDownload
        ? `/api/history/${encodeURIComponent(target.recordId)}/zip`
        : `/api/download/zip/${encodeURIComponent(target.sessionId)}`;

    if (isElectron) {
        try {
            const resp = await fetch(apiUrl(pathEndpoint));
            if (resp.ok) {
                const data = await resp.json();
                if (data.path) {
                    // 使用源文件名作为 ZIP 下载文件名
                    const sourceName = String(target.sourceFilename || PRODUCT_NAME).replace(/\.(docx|xlsx)$/i, '');
                    await saveNativeFile(data.path, `${sourceName}_tts.zip`);
                } else {
                    showToast('ZIP 文件不存在');
                }
            } else {
                showToast(isHistoryDownload ? '下载失败：历史 ZIP 已不存在' : '下载失败：文件不存在或会话已过期');
            }
        } catch (err) {
            console.error('下载异常:', err);
            showToast('下载失败');
        }
    } else {
        window.open(apiUrl(browserEndpoint), '_blank');
    }
}

async function downloadFile(filename, context = activeResultContext) {
    const target = context || (currentSession ? {
        mode: 'current',
        sessionId: currentSession.session_id,
    } : null);
    if (!target || !filename) return;
    const isHistoryDownload = target.mode === 'history';
    const pathEndpoint = isHistoryDownload
        ? `/api/history/${encodeURIComponent(target.recordId)}/file-path?filename=${encodeURIComponent(filename)}`
        : `/api/file-path?session_id=${encodeURIComponent(target.sessionId)}&filename=${encodeURIComponent(filename)}`;
    const browserEndpoint = isHistoryDownload
        ? `/api/history/${encodeURIComponent(target.recordId)}/file/${encodeURIComponent(filename)}`
        : `/api/download/file/${encodeURIComponent(target.sessionId)}/${encodeURIComponent(filename)}`;

    if (isElectron) {
        try {
            const resp = await fetch(apiUrl(pathEndpoint));
            if (resp.ok) {
                const data = await resp.json();
                if (data.path) {
                    await saveNativeFile(data.path, filename);
                } else {
                    showToast('文件不存在');
                }
            } else {
                showToast(isHistoryDownload ? '下载失败：历史音频已不存在' : '下载失败：文件不存在或会话已过期');
            }
        } catch (err) {
            console.error('下载异常:', err);
            showToast('下载失败');
        }
    } else {
        window.open(apiUrl(browserEndpoint), '_blank');
    }
}

// ============================================================================
// 重新开始
// ============================================================================

function resetGenerateState() {
    isGenerating = false;
    const historyNav = $('history-nav-btn');
    if (historyNav) historyNav.disabled = isRestarting || isParsing;
    renderTaskControlState();
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
            confirmation = latestCurrentResultEvent?.history_id
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

async function restart() {
    destroyWaveSurfers();
    let cleanupConfirmed = true;

    // 先让所有在途异步回调失效，避免清理请求期间旧任务重新接管页面。
    parseAttemptId++;
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
    sseConnectionToken++;

    const sessionToCleanup = currentSession;
    currentSession = null;
    isGenerating = false;
    contentDraft = null;
    contentEditorDirty = false;
    taskControlState = 'idle';
    browserState = {
        visibility: 'unavailable',
        permission_required: false,
        last_error: '自动化浏览器尚未启动',
    };
    // 断开 SSE
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }

    // 如果有会话，通知后端清理
    if (sessionToCleanup) {
        pendingCleanupSessionId = sessionToCleanup.session_id;
        cleanupConfirmed = await confirmPendingCleanup();
        if (!cleanupConfirmed) {
            cleanupConfirmed = false;
            setServiceState('warning', '任务清理待确认');
            $('retry-service-btn').hidden = false;
        }
    }
    // 只有后端确认清理完成（或确认会话已经不存在）才删除恢复索引；
    // 网络超时/安全退出超时必须保留它，防止应用重启后无法找回旧任务。
    if (!sessionToCleanup || cleanupConfirmed) clearPersistedActiveSession();

    // 重置状态
    generatedFiles = [];
    activeResultContext = null;
    latestCurrentResultEvent = null;
    historyRequestToken++;
    logEntryCount = 0;
    lastStats = null;
    lastDownloadEvent = null;
    sseRetryCount = 0;
    generationResult = null;
    lastGenerationConfig = null;
    resetTaskVoiceConfiguration();

    // 重置 Step 1
    const uploadZone = $('upload-zone');
    uploadZone.classList.remove('has-file', 'has-error', 'is-processing', 'dragover');
    uploadZone.setAttribute('aria-busy', 'false');
    uploadZone.querySelector('.upload-text-large').textContent = '拖拽文档到这里，或点击选择';
    uploadZone.querySelector('.upload-hint').textContent = '支持 .docx / .xlsx 文件 · 选择后会自动解析';
    setUploadFeedback();
    updateSessionLabels();

    // 刷新预设列表（可能在上一次操作中保存了新配置）
    refreshPresetUI();

    // 重置 Step 3
    $('progress-bar').style.width = '0%';
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', '0');
    $('progress-stats').textContent = '准备中...';
    $('progress-percent').textContent = '0';
    $('progress-completed-label').textContent = '已完成';
    $('progress-completed').textContent = '0';
    $('progress-remaining').textContent = '—';
    $('progress-failed').textContent = '0';
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
    $('result-secondary-caption').textContent = '待处理内容';
    $('result-format-value').textContent = 'MP3';
    $('result-hero').classList.remove('has-no-package');
    $('generate-full-btn').hidden = true;
    $('back-to-history-btn').hidden = true;
    $('result-warning').hidden = true;
    $('result-failure-list').innerHTML = '';
    $('retry-failed-btn').hidden = true;
    $('result-eyebrow').textContent = '任务已完成';
    $('result-title').textContent = '音频已经准备好了';
    document.querySelector('.result-success-icon')?.classList.remove('has-warning', 'has-error');

    // 重置状态栏
    $('status-text').textContent = cleanupConfirmed ? '就绪' : '请重新连接生成服务';
    $('stats-bar').innerHTML = '';
    renderContentEditor();
    renderBrowserState();
    renderTaskControlState();

    // 回到首页
    goToStep(1);
    await refreshHistoryRecords({ showLoading: false });
    showToast(cleanupConfirmed
        ? '已重置，可以开始新任务'
        : '当前任务已关闭，请重新连接生成服务后继续');
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
// 启动
// ============================================================================

init();
