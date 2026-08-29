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
const workflowApi = isElectron ? window.electronAPI.workflow : null;
const workflowStore = isElectron && typeof window.createWorkflowStore === 'function'
    ? window.createWorkflowStore({ storage: window.localStorage })
    : null;
const PRODUCT_NAME = '小猪wordTTS';

let currentStep = 1;
let currentView = 'workflow';    // 'workflow' | 'history' | 'history-result'
let historyReturnStep = 1;       // 从历史中心返回工作流时恢复原步骤
let historyRecords = [];
let historyRequestToken = 0;     // 使较早的历史列表/详情请求失效
let activeResultContext = null;  // 当前交付页对应当前任务或历史记录
let latestCurrentResultEvent = null; // 从历史详情返回时恢复当前任务的交付页
let currentSession = null;       // { session_id, source_filename, source_artifact_id, parse_results }
let currentConfig = null;        // API 返回的配置
let clientConfigInitialized = false; // 防止连接重试时用服务端默认值覆盖用户当前设置
let voiceCatalog = [
    { key: 'amanda', name: '英语-Amanda', gender: 'female', gender_label: '女声', language: ['英语'], tags: ['英语'], categories: ['女声', '英语'] },
    { key: 'george', name: '英语-George', gender: 'male', gender_label: '男声', language: ['英语'], tags: ['英语'], categories: ['男声', '英语'] },
];
let voiceAliasMap = {};
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
let generateAbortController = null; // 当前生成启动请求
let generationAttemptId = 0;        // 使旧生成任务回调失效
let generationStartInFlight = false; // 防止启动握手尚未结束时重复提交
let generationStartAttemptId = 0;
let cancelWorkflowPromise = null;    // 同一任务只允许一个取消请求链
let generationCancelRequested = false;
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
let lastAmbiguousRecoveryTarget = null; // 最近一次需要人工确认的外部提交目标
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
    try {
        localStorage.setItem(CURRENT_CONFIG_STORAGE_KEY, JSON.stringify(normalizePersistedConfig(config)));
        return true;
    } catch (e) {
        console.error('保存当前配置失败:', e);
        return false;
    }
}

function loadCurrentConfig() {
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

/**
 * 从 localStorage 读取所有预设。
 */
function loadPresets() {
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
    try {
        const sanitized = Array.isArray(presets)
            ? presets.map(p => ({ ...p, config: normalizePersistedConfig(p?.config) }))
            : [];
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
        const raw = JSON.parse(localStorage.getItem(VOICE_RECENT_STORAGE_KEY) || '[]');
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
        localStorage.setItem(VOICE_RECENT_STORAGE_KEY, JSON.stringify(recent));
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
        const raw = JSON.parse(localStorage.getItem(VOICE_RECENT_STORAGE_KEY) || '[]');
        const recent = Array.isArray(raw)
            ? [...new Set(raw.map(key => canonicalVoiceKey(key)).filter(Boolean))].slice(0, 12)
            : [];
        if (JSON.stringify(recent) !== JSON.stringify(raw)) {
            localStorage.setItem(VOICE_RECENT_STORAGE_KEY, JSON.stringify(recent));
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
        button.className = `voice-card${voice.key === selectedKey ? ' is-selected' : ''}`;
        button.dataset.voiceKey = voice.key;
        button.setAttribute('role', 'option');
        button.setAttribute('aria-selected', voice.key === selectedKey ? 'true' : 'false');
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
    if ($('history-nav-btn')) $('history-nav-btn').disabled = true;
    generatedFiles = [];
    logEntryCount = 0;
    lastStats = null;
    lastDownloadEvent = null;
    sseRetryCount = 0;
    generationResult = null;
    lastAmbiguousRecoveryTarget = null;
    updateSessionLabels(session.source_filename, session.parse_results, {
        preview: isPreviewScope,
        total: generationTotal,
    });
    hideGenerationRecovery();
    setGenerationVisualState('running');

    // 重置生成页面 UI
    $('progress-bar').style.width = '0%';
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', '0');
    setProgressIndeterminate(true);
    $('progress-stats').textContent = `正在准备生成计划 · 0 / ${generationTotal || '—'}`;
    $('progress-percent').textContent = '0';
    $('progress-completed-label').textContent = '已完成';
    $('progress-completed').textContent = '0';
    $('progress-remaining').textContent = generationTotal || '—';
    $('progress-failed').textContent = '0';
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
        const configurationRevisionBeforePatch = Number(
            workspaceBeforePatch?.configuration?.configuration_revision,
        );
        if (!Number.isInteger(configurationRevisionBeforePatch) || configurationRevisionBeforePatch < 1) {
            throw new Error('工作区配置版本缺失，无法安全提交生成任务');
        }
        const patched = await workflowApi.patchDraft(session.session_id, {
            expected_state_version: session.state_version,
            configuration_revision: configurationRevisionBeforePatch,
            configuration: persistedConfiguration,
        });
        mergeWorkflowSnapshotIntoSession(patched, session);
        const workspaceAfterPatch = await workflowApi.getWorkspace(session.session_id);
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
                : `启动失败：${err.message}`;
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
        showGenerationRecovery(failureMessage);
        showToast(failureMessage, 'error');
        resetGenerateState();
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

function showGenerationRecovery(message, { ambiguous = false, target = null } = {}) {
    const panel = $('generation-recovery');
    const messageEl = $('generation-error-message');
    if (messageEl) {
        messageEl.textContent = ambiguous
            ? `${message || '外部生成结果待核验。'} 已暂停自动重试；请先核验提交结果，确认未提交后再重试。`
            : (message || '生成任务未能继续，请重试或返回调整设置。');
    }
    if (panel) panel.hidden = false;
    if (
        target && ambiguous && target.attempt_id && target.work_unit_id
        && Number.isInteger(Number(target.target_state_version))
    ) {
        lastAmbiguousRecoveryTarget = { ...target, target_state_version: Number(target.target_state_version) };
    }
    const resolveButton = $('resolve-not-submitted-btn');
    if (resolveButton) resolveButton.hidden = !ambiguous || !lastAmbiguousRecoveryTarget;
    const retryButton = $('retry-generation-btn');
    if (retryButton) {
        // An ambiguous retry is reconciliation-only. It must not look like a
        // normal new submission: opening the browser and waiting on the works
        // list is expected, but it is not a generation action.
        retryButton.hidden = Boolean(ambiguous);
        retryButton.disabled = Boolean(ambiguous);
    }
}

function hideGenerationRecovery() {
    const panel = $('generation-recovery');
    if (panel) panel.hidden = true;
    const resolveButton = $('resolve-not-submitted-btn');
    if (resolveButton) resolveButton.hidden = true;
    const retryButton = $('retry-generation-btn');
    if (retryButton) {
        retryButton.hidden = false;
        retryButton.disabled = false;
    }
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
        if (el) el.classList.remove('is-error', 'is-stopped', 'is-done');
    });
    if (animation && state !== 'done') animation.classList.remove('done');

    const labels = {
        running: '任务进行中',
        done: '处理完成',
        error: '需要处理',
        warning: '部分完成',
        stopped: '任务已停止',
    };
    if (badgeLabel) badgeLabel.textContent = labels[state] || labels.running;
    const liveLabels = {
        running: '批量任务进行中',
        done: '批量任务已完成',
        warning: '任务完成，部分内容需处理',
        error: '生成遇到问题，请检查记录',
        stopped: '任务已停止',
    };
    if (liveStatus) liveStatus.textContent = liveLabels[state] || liveLabels.running;
    const liveLabelTexts = {
        running: '当前阶段',
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

/**
 * 将服务端工作流快照合并到当前会话。
 *
 * 生成期间事件流可能先送达一个较早的快照；状态版本只能向前推进，
 * 不能让旧快照把已经拿到的版本回退，否则用户返回配置后重试会把
 * stale expected_state_version 提交给后端。
 */
function mergeWorkflowSnapshotIntoSession(snapshot, session = currentSession) {
    if (!snapshot || !session) return session;
    const snapshotVersion = Number(snapshot.state_version);
    const sessionVersion = Number(session.state_version);
    const snapshotSeq = Number(snapshot.latest_seq);
    const sessionSeq = Number(session.latest_seq);
    if (
        Number.isInteger(snapshotSeq) && snapshotSeq >= 0
        && Number.isInteger(sessionSeq) && sessionSeq >= 0
        && snapshotSeq < sessionSeq
    ) return session;
    if (Number.isInteger(snapshotVersion) && snapshotVersion >= 0) {
        // 旧快照的其它字段也不能覆盖当前会话，否则除了版本号外，
        // execution_state/游标也会被回退到中断前的状态。
        if (Number.isInteger(sessionVersion) && snapshotVersion < sessionVersion) return session;
        session.state_version = snapshotVersion;
    }
    ['execution_state', 'control_state', 'result_status', 'cleanup_state', 'source_artifact_id', 'latest_event_id', 'latest_seq'].forEach((key) => {
        if (snapshot[key] !== undefined && snapshot[key] !== null) session[key] = snapshot[key];
    });
    const snapshotGroupVersion = Number(snapshot.group_state_version);
    const sessionGroupVersion = Number(session.group_state_version);
    if (Number.isInteger(snapshotGroupVersion) && snapshotGroupVersion >= 0
        && (!Number.isInteger(sessionGroupVersion) || snapshotGroupVersion >= sessionGroupVersion)) {
        session.group_state_version = snapshotGroupVersion;
    }
    return session;
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
    mergeWorkflowSnapshotIntoSession(snapshot, session);
    return snapshot;
}

const ACCEPTED_GENERATION_EXECUTION_STATES = new Set([
    'RUNNING',
    'RECOVERING',
    'BLOCKED',
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
        && (
            String(snapshot.execution_state || '') === 'TERMINAL'
            || TERMINAL_WORKFLOW_RESULT_STATES.has(String(snapshot.result_status || ''))
        )
    );
}

function isAcceptedGenerationSnapshot(snapshot) {
    if (!snapshot || isTerminalWorkflowSnapshot(snapshot)) return false;
    return ACCEPTED_GENERATION_EXECUTION_STATES.has(String(snapshot.execution_state || ''))
        || String(snapshot.control_state || '') === 'TERMINATING';
}

function isWaitingForGenerationCleanup(snapshot) {
    if (!snapshot || isTerminalWorkflowSnapshot(snapshot)) return false;
    return String(snapshot.cleanup_state || '') !== 'SUCCEEDED'
        && ['WAITING_RETRY', 'WAITING_USER'].includes(String(snapshot.execution_state || ''));
}

function isCancellationSettledSnapshot(snapshot) {
    return Boolean(
        snapshot
        && (
            isTerminalWorkflowSnapshot(snapshot)
            || (
                String(snapshot.cleanup_state || '') === 'SUCCEEDED'
                && String(snapshot.control_state || '') === 'TERMINATING'
            )
        )
    );
}

/**
 * The ZIP Artifact is created on demand.  A terminal result with at least one
 * verified audio file must therefore keep the ZIP action visible even before
 * the first export has been materialized.
 */
function resultZipState(context, resultCount) {
    const hasArtifact = Boolean(context?.zipAvailable && context?.zipArtifactId);
    const count = Number(resultCount);
    const hasDeliverableAudio = Number.isFinite(count) && count > 0;
    const terminal = String(context?.executionState || '') === 'TERMINAL'
        || TERMINAL_WORKFLOW_RESULT_STATES.has(String(context?.resultStatus || ''));
    return {
        visible: hasArtifact || (hasDeliverableAudio && terminal),
        ready: hasArtifact,
    };
}

function updateGenerationCancelUI() {
    const button = $('cancel-generation-btn');
    if (!button) return;
    const sessionActive = Boolean(
        currentSession?.session_id
        && !isTerminalWorkflowSnapshot(currentSession)
        && (
            isGenerating
            || generationStartInFlight
            || cancelWorkflowPromise
            || isAcceptedGenerationSnapshot(currentSession)
            || isWaitingForGenerationCleanup(currentSession)
        )
    );
    button.hidden = !sessionActive;
    button.disabled = Boolean(cancelWorkflowPromise) || isRestarting;
    button.textContent = cancelWorkflowPromise
        ? '正在停止…'
        : (String(currentSession?.control_state || '') === 'TERMINATING' ? '确认停止' : '停止生成');
    if (cancelWorkflowPromise) button.setAttribute('aria-busy', 'true');
    else button.removeAttribute('aria-busy');
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
    isGenerating = true;
    hideGenerationRecovery();

    const stopping = String(snapshot.control_state || '') === 'TERMINATING'
        || String(snapshot.execution_state || '') === 'BLOCKED';
    setGenerationVisualState(stopping ? 'stopped' : 'running');
    setProgressIndeterminate(true);
    $('gen-title').textContent = stopping ? '正在停止生成' : '正在生成音频';
    $('generation-live-status').textContent = stopping ? '正在确认任务停止状态…' : reason;
    $('status-text').textContent = stopping ? '正在停止生成任务…' : reason;
    const total = summarizeParseResults(session.parse_results).total;
    if (!lastStats) {
        $('progress-stats').textContent = `${reason} · 0 / ${total || '—'}`;
    }
    updateGenerationCancelUI();
    void connectSSE(session.session_id);
    return true;
}

function workflowProgressCounts(snapshot, session = currentSession) {
    const progress = snapshot?.progress || session?.progress || {};
    const total = Number(progress.total) || summarizeParseResults(session?.parse_results).total || 0;
    const completed = Number(progress.completed) || generatedFiles.length || 0;
    const failed = Number(progress.failed) || 0;
    return {
        total: Math.max(0, total),
        completed: Math.max(0, Math.min(completed, total || completed)),
        failed: Math.max(0, failed),
    };
}

function cancellationDelay(milliseconds) {
    return new Promise(resolve => setTimeout(resolve, milliseconds));
}

/**
 * 发出一次幂等取消命令，并等待后端完成本地清理。取消是协作式的：
 * 讯飞提交已经跨过外部边界时，后端会保留 WAITING_USER/BLOCKED，不能
 * 把它伪装成普通 CANCELLED，也不能因此放开配置修改。
 */
async function cancelCurrentWorkflow(session = currentSession, {
    reason = 'desktop-user-cancel',
    timeoutMs = 30000,
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
    updateGenerationCancelUI();

    const operation = (async () => {
        let snapshot = await workflowApi.getWorkflow(sessionId);
        if (currentSession?.session_id === sessionId) {
            mergeWorkflowSnapshotIntoSession(snapshot, currentSession);
        }
        if (isTerminalWorkflowSnapshot(snapshot)) return snapshot;

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
                if (currentSession?.session_id === sessionId) {
                    mergeWorkflowSnapshotIntoSession(snapshot, currentSession);
                }
                break;
            } catch (error) {
                if (error?.code !== 'STATE_CONFLICT' || commandAttempts >= 1) throw error;
                snapshot = await workflowApi.getWorkflow(sessionId);
                if (currentSession?.session_id === sessionId) {
                    mergeWorkflowSnapshotIntoSession(snapshot, currentSession);
                }
            }
            commandAttempts++;
        }

        const deadline = Date.now() + Math.max(1000, Number(timeoutMs) || 30000);
        while (!isCancellationSettledSnapshot(snapshot) && Date.now() < deadline) {
            await cancellationDelay(250);
            snapshot = await workflowApi.getWorkflow(sessionId);
            if (currentSession?.session_id === sessionId) {
                mergeWorkflowSnapshotIntoSession(snapshot, currentSession);
            }
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

function applyCancellationOutcome(session, snapshot) {
    if (!snapshot || currentSession?.session_id !== session?.session_id) return;
    mergeWorkflowSnapshotIntoSession(snapshot, currentSession);
    if (isTerminalWorkflowSnapshot(snapshot)) {
        const counts = workflowProgressCounts(snapshot, currentSession);
        if (['CANCELLED', 'PARTIAL_SUCCESS'].includes(String(snapshot.result_status || ''))) {
            handleSSEEvent({ type: 'cancelled', ...counts });
        } else if (snapshot.result_status === 'SUCCEEDED') {
            // 取消请求可能正好与最后一个完成事件并发；重新读取 Artifact，
            // 让用户得到正常结果页而不是一个假“已取消”。
            void refreshGeneratedArtifacts(session.session_id).then(() => {
                if (currentSession?.session_id !== session.session_id) return;
                handleDone({
                    type: 'done',
                    ...workflowProgressCounts(snapshot, currentSession),
                    file_list: generatedFiles,
                });
            }).catch(() => {});
        }
        return;
    }
    if (isCancellationSettledSnapshot(snapshot)) {
        resetGenerateState();
        generationResult = 'error';
        setProgressIndeterminate(false);
        setGenerationVisualState('warning');
        $('gen-title').textContent = '任务待核验';
        $('status-text').textContent = '本地任务已停止，但讯飞提交结果仍待核验';
        showGenerationRecovery(
            '本地浏览器任务已经停止，但讯飞作品结果尚未确认。请先在讯飞作品列表核验，确认未提交后再重试；配置暂不可修改。',
            { ambiguous: true },
        );
    } else {
        // 超时不表示取消失败：保留 RUNNING/TERMINATING 的权威状态，
        // 让按钮和 SSE 继续可用，避免又创建第二个任务。
        isGenerating = true;
        updateGenerationCancelUI();
        $('status-text').textContent = '停止请求已发出，讯飞浏览器仍在收尾，请稍候…';
        $('generation-live-status').textContent = '正在等待讯飞浏览器结束当前操作…';
    }
}

async function returnToConfigSafely({ buttonId = 'return-config-btn' } = {}) {
    const button = $(buttonId);
    if (button?.dataset.busy === 'true') return false;
    if (button) {
        button.dataset.busy = 'true';
        button.disabled = true;
    }
    const session = currentSession;
    try {
        if (!session || !workflowApi) {
            goToStep(2);
            return true;
        }
        let snapshot = await refreshCurrentWorkflowSnapshot(session);
        const mustStop = generationStartInFlight
            || isGenerating
            || isAcceptedGenerationSnapshot(snapshot)
            || isWaitingForGenerationCleanup(snapshot);
        if (mustStop && !isTerminalWorkflowSnapshot(snapshot)) {
            snapshot = await cancelCurrentWorkflow(session, {
                reason: 'desktop-return-to-configuration',
            });
            applyCancellationOutcome(session, snapshot);
            if (!isTerminalWorkflowSnapshot(snapshot)) {
                showToast('任务仍在停止或等待讯飞结果核验，暂不能返回可编辑配置', 'warning');
                return false;
            }
        }
        snapshot = await holdAutomaticRetry(session) || snapshot;
        // hold 与调度器之间仍可能发生一次竞态：如果调度器已经把任务
        // 推进到 RUNNING，不能继续打开配置页，而要立即走同一取消路径。
        if (isAcceptedGenerationSnapshot(snapshot) || isWaitingForGenerationCleanup(snapshot)) {
            snapshot = await cancelCurrentWorkflow(session, {
                reason: 'desktop-return-to-configuration-race',
            });
            applyCancellationOutcome(session, snapshot);
            if (!isTerminalWorkflowSnapshot(snapshot)) {
                showToast('任务在返回配置时被自动化重新接管，暂不能编辑配置', 'warning');
                return false;
            }
        }
        if (currentSession?.session_id !== session.session_id) return false;
        hideGenerationRecovery();
        goToStep(2);
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
    const snapshot = await refreshCurrentWorkflowSnapshot(session);
    if (!snapshot || !['WAITING_RETRY', 'WAITING_USER'].includes(String(snapshot.execution_state))) {
        return snapshot;
    }
    const expectedStateVersion = Number(session.state_version);
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
        // A scheduler tick may have advanced the version between the GET and
        // hold command. Re-read once; navigation remains available even if
        // the service is momentarily unavailable.
        if (error?.code === 'STATE_CONFLICT') {
            try {
                return await refreshCurrentWorkflowSnapshot(session);
            } catch (_) {
                // Keep the original snapshot for the caller's best-effort UI.
            }
        }
        console.warn('暂停后台自动重试失败:', error);
        return snapshot;
    }
}

function verifiedItemIdsFromArtifacts(artifacts) {
    return new Set((Array.isArray(artifacts) ? artifacts : [])
        .filter(artifact => (
            artifact?.item_id
            && artifact.lifecycle_state === 'READY'
            && artifact.verified === true
            && ['tts-segment', 'tts-output'].includes(String(artifact.artifact_type || ''))
        ))
        .map(artifact => String(artifact.item_id)));
}

/**
 * Retry only durable FAILED items from the still-open run. AMBIGUOUS items
 * intentionally stay on the reconciliation path and are never mass-retried.
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
                ? '有提交结果待核验，请先在任务详情中处理，不会自动重试'
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
        generation_mode: config.generation_mode,
        provider: 'xunfei',
        account_scope: currentConfig?.account_scope || 'xunfei-default',
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

async function recoveryEvidenceHash(target) {
    const source = [
        'desktop-user-confirmed-not-submitted',
        currentSession?.session_id || '',
        target?.attempt_id || '',
        target?.work_unit_id || '',
    ].join(':');
    const subtle = globalThis.crypto?.subtle;
    if (subtle && typeof TextEncoder === 'function') {
        const digest = await subtle.digest('SHA-256', new TextEncoder().encode(source));
        return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
    }
    // The API only needs a traceable evidence token on runtimes without Web
    // Crypto; the normal Electron renderer uses the SHA-256 branch above.
    return `manual-${source}`.slice(0, 128);
}

async function resolveAmbiguousSubmission() {
    if (isGenerating || !lastAmbiguousRecoveryTarget || !currentSession || !workflowApi) return;
    const target = { ...lastAmbiguousRecoveryTarget };
    const expectedTargetVersion = Number(target.target_state_version);
    if (!target.attempt_id || !target.work_unit_id || !Number.isInteger(expectedTargetVersion)) {
        showToast('缺少可核验的提交目标，请先重试以刷新任务记录', 'error');
        return;
    }
    const confirmed = await showConfirmDialog({
        kicker: '外部提交核验',
        title: '确认讯飞没有生成作品？',
        message: '只有在讯飞作品列表中确认没有本次作品后，才能继续重试。',
        detail: '确认后本次提交会被标记为“未提交”，允许再次生成；如果作品实际已经存在，可能造成重复扣费。',
        tone: 'danger',
        confirmLabel: '确认未提交并重试',
    });
    if (!confirmed) return;
    const button = $('resolve-not-submitted-btn');
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
    }
    try {
        await refreshCurrentWorkflowSnapshot();
        const evidenceHash = await recoveryEvidenceHash(target);
        const response = await workflowApi.resolve({
            attempt_id: target.attempt_id,
            expected_state_version: Number(currentSession.state_version || 0),
            expected_target_state_version: expectedTargetVersion,
            target: { target_type: 'WORK_UNIT', work_unit_id: target.work_unit_id },
            decision: 'NOT_SUBMITTED',
            evidence: {
                source: 'desktop-user-confirmed-not-submitted',
                evidence_hash: evidenceHash,
                summary: '用户确认讯飞作品列表中没有本次作品',
            },
        });
        mergeWorkflowSnapshotIntoSession(response?.current_snapshot, currentSession);
        lastAmbiguousRecoveryTarget = null;
        hideGenerationRecovery();
        showToast('已记录未提交核验，正在安全重试');
        goToStep(3);
        await startProcessing(false, lastGenerationConfig || undefined);
    } catch (error) {
        console.error('核验讯飞未提交失败:', error);
        try {
            await refreshCurrentWorkflowSnapshot();
        } catch (_) {
            // The original conflict/error remains the actionable message.
        }
        showToast(`核验失败：${error.message || '任务状态已变化，请刷新后重试'}`, 'error');
    } finally {
        if (button) {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    }
}

async function confirmPendingCleanup(session = null) {
    const sessionId = pendingCleanupSessionId;
    if (!sessionId) return true;
    const cleanupFinished = (snapshot) => Boolean(
        snapshot
        && (
            snapshot.cleanup_state === 'SUCCEEDED'
            || snapshot.execution_state === 'TERMINAL'
            || snapshot.control_state === 'TERMINATED'
        )
    );
    try {
        if (workflowApi) {
            let snapshot = await workflowApi.getWorkflow(sessionId);
            if (!cleanupFinished(snapshot)) {
                for (let attempt = 0; attempt < 2; attempt += 1) {
                    try {
                        snapshot = await workflowApi.sendCommand(sessionId, 'cancel', {
                            expected_state_version: Number(snapshot.state_version || session?.state_version || 0),
                            reason: 'desktop-restart',
                        });
                        break;
                    } catch (error) {
                        if (error?.code !== 'STATE_CONFLICT' || attempt === 1) throw error;
                        snapshot = await workflowApi.getWorkflow(sessionId);
                        if (cleanupFinished(snapshot)) break;
                    }
                }
            }
            // The backend closes the generation slot asynchronously.  Wait
            // for that durable cleanup marker before allowing a new document
            // to start; otherwise a new task can sit behind an interrupted
            // browser call and look frozen at 0%.
            // 讯飞页面可能正在结束一次 Playwright 操作；五秒不足以让
            // 协作式取消释放浏览器线程，随后新任务就会像卡在 0% 一样。
            const deadline = Date.now() + 30000;
            while (!cleanupFinished(snapshot) && Date.now() < deadline) {
                await new Promise(resolve => setTimeout(resolve, 250));
                snapshot = await workflowApi.getWorkflow(sessionId);
            }
            if (!cleanupFinished(snapshot)) return false;
        }
        if (pendingCleanupSessionId === sessionId) pendingCleanupSessionId = null;
        return true;
    } catch (error) {
        console.error('任务取消未确认:', error);
        return false;
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
        'resolve-not-submitted-btn',
        'return-config-btn',
        'cancel-generation-btn',
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
        showToast('就绪');
    }
}

function bindEvents() {
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
        if (!isGenerating && !lastAmbiguousRecoveryTarget) {
            startProcessing(false, lastGenerationConfig || undefined);
        } else if (!isGenerating && lastAmbiguousRecoveryTarget) {
            showToast('当前提交结果待核验，请先确认未提交后再重试', 'error');
        }
    });
    $('resolve-not-submitted-btn')?.addEventListener('click', resolveAmbiguousSubmission);
    $('return-config-btn').addEventListener('click', () => {
        void returnToConfigSafely();
    });
    $('cancel-generation-btn')?.addEventListener('click', async () => {
        if (!currentSession || cancelWorkflowPromise) return;
        const session = currentSession;
        const button = $('cancel-generation-btn');
        if (button) button.disabled = true;
        try {
            const snapshot = await cancelCurrentWorkflow(session, {
                reason: 'desktop-user-cancel',
            });
            applyCancellationOutcome(session, snapshot);
            if (!isTerminalWorkflowSnapshot(snapshot) && !isCancellationSettledSnapshot(snapshot)) {
                showToast('停止请求已发出，任务仍在收尾', 'warning');
            }
        } catch (error) {
            console.error('停止生成失败:', error);
            showToast(`停止生成失败：${error.message || '请稍后重试'}`, 'error');
            $('status-text').textContent = '停止请求未完成，请再次点击“停止生成”';
        } finally {
            updateGenerationCancelUI();
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
    $('result-return-config-btn').addEventListener('click', async () => {
        destroyWaveSurfers();
        if (lastGenerationConfig) applyConfigToForm(lastGenerationConfig, { includeRoles: true });
        const moved = await returnToConfigSafely({ buttonId: 'result-return-config-btn' });
        if (moved) showToast('已返回配置；修改参数后会重新生成全部内容');
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

function goToStep(step) {
    currentView = 'workflow';
    currentStep = step;
    if (step === 2 && currentSession) renderVoiceWorkspace();

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
    return String(record?.format || '未知').toUpperCase();
}

function historyGenerationModeLabel(record) {
    // 历史清单升级前没有该字段，按原有逐条流程解释，避免把旧任务误标成
    // 新的合并切割模式。
    return record?.generation_mode
        ? generationModeLabel(record.generation_mode)
        : GENERATION_MODE_LABELS[GENERATION_MODE_SINGLE];
}

function historyStatusPresentation(record) {
    const executionState = String(record?.execution_state || '');
    const controlState = String(record?.control_state || '');
    const resultStatus = String(record?.result_status || '');
    const requiresReconcile = Boolean(record?.active_candidate?.requires_reconcile)
        || executionState === 'WAITING_USER'
        || resultStatus === 'AMBIGUOUS';
    if (requiresReconcile) return { label: '待处理/对账', className: 'is-partial' };
    if (executionState === 'TERMINAL') {
        if (resultStatus === 'SUCCEEDED') {
            const completed = Number(record?.completed) || 0;
            const total = Number(record?.total) || 0;
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

function resultFilesFromArtifacts(items, artifacts, workspace = null) {
    const itemById = new Map((Array.isArray(items) ? items : []).map(item => [String(item.item_id), item]));
    const workspaceItems = new Map((Array.isArray(workspace?.items) ? workspace.items : [])
        .map(item => [String(item.item_id), item]));
    const workspaceArtifacts = new Map((Array.isArray(workspace?.artifacts) ? workspace.artifacts : [])
        .map(artifact => [String(artifact.artifact_id), artifact]));
    const latestByItem = new Map();
    const seenItemIds = new Set();

    // A READY Artifact is deliverable only when the authoritative item state
    // is SUCCEEDED and its server-owned filename/format/MIME metadata passes
    // validation. Never synthesize a filename or default a missing format to
    // MP3: doing so turns stale/conflicting facts into a false download.
    (Array.isArray(artifacts) ? artifacts : [])
        .slice()
        .sort((left, right) => (
            String(right.created_at || '').localeCompare(String(left.created_at || ''))
            || String(right.artifact_id || '').localeCompare(String(left.artifact_id || ''))
        ))
        .forEach(artifact => {
            const itemId = String(artifact?.item_id || '');
            if (!itemId || seenItemIds.has(itemId)) return;
            if (!['tts-segment', 'tts-output'].includes(String(artifact.artifact_type || ''))) return;
            // The newest TTS artifact is authoritative for this item. If it is
            // not deliverable, do not fall back to an older attempt's audio.
            seenItemIds.add(itemId);

            const item = itemById.get(itemId) || {};
            const workspaceItem = workspaceItems.get(itemId);
            const metadata = workspaceArtifacts.get(String(artifact.artifact_id)) || artifact;
            const itemStatus = String(workspaceItem?.status || item.status || '');
            const lifecycleState = String(metadata.lifecycle_state || artifact.lifecycle_state || '');
            const verified = metadata.verified === true && artifact.verified !== false;
            const format = String(metadata.format || artifact.format || '').trim().toLowerCase().replace(/^\./, '');
            const filename = String(metadata.filename || artifact.filename || '').trim();
            const mimeType = String(metadata.mime_type || artifact.mime_type || '').trim().toLowerCase();
            const extension = filename.includes('.') ? filename.split('.').pop().toLowerCase() : '';
            const sizeBytes = Number(metadata.size_bytes ?? artifact.size_bytes);
            const expectedMime = artifactMime(format);
            if (
                itemStatus !== 'SUCCEEDED'
                || lifecycleState !== 'READY'
                || !verified
                || !filename
                || filename.includes('/')
                || filename.includes('\\')
                || /[\x00-\x1f\x7f]/.test(filename)
                || !format
                || !mimeType
                || extension !== format
                || expectedMime === 'application/octet-stream'
                || mimeType !== expectedMime
                || !Number.isSafeInteger(sizeBytes)
                || sizeBytes <= 0
            ) return;

            latestByItem.set(itemId, {
                filename,
                artifact_id: String(artifact.artifact_id),
                available: true,
                doc_type: item.item_type || '音频',
                category: item.item_type || '',
                item_id: itemId,
                text: item.normalized_content || '',
                text_preview: String(item.normalized_content || '').slice(0, 160),
                role: item.role || null,
                voice_key: item.voice_key || null,
                size_bytes: sizeBytes,
                format,
                mime_type: mimeType,
                duration_ms: Number.isFinite(Number(metadata.duration_ms)) ? Number(metadata.duration_ms) : null,
            });
        });

    return [...latestByItem.values()].sort((left, right) => {
        const leftSequence = Number(itemById.get(left.item_id)?.sequence ?? Number.MAX_SAFE_INTEGER);
        const rightSequence = Number(itemById.get(right.item_id)?.sequence ?? Number.MAX_SAFE_INTEGER);
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
    list.replaceChildren();
    empty.hidden = records.length > 0;
    if (records.length === 0) return;

    records.forEach(record => {
        const availableCount = Math.max(0, Number(record.available_files) || 0);
        const completed = Math.max(0, Number(record.completed) || availableCount);
        const failed = Math.max(0, Number(record.failed) || 0);
        const total = Math.max(completed, Number(record.total) || 0);
        const pending = Math.max(0, total - completed - failed);
        const presentation = historyStatusPresentation(record);
        const terminal = String(record.execution_state || '') === 'TERMINAL';
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
        const viewBtn = createHistoryAction(terminal ? '查看结果' : '查看状态', 'btn-primary', () => viewHistoryRecord(record.id));
        viewBtn.disabled = terminal && availableCount === 0;
        if (viewBtn.disabled) viewBtn.title = '当前没有可交付的已验证音频';
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
        const deleteBtn = createHistoryAction('归档', 'btn-ghost history-delete-btn', () => deleteHistoryRecord(record, deleteBtn));
        deleteBtn.disabled = !terminal;
        if (!terminal) deleteBtn.title = '任务结束后才可归档';
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
        if (!workflowApi) throw new Error('工作流服务未初始化');
        const [data, activeCandidates] = await Promise.all([
            workflowApi.listWorkflows(100),
            typeof workflowApi.listActiveWorkflows === 'function'
                ? workflowApi.listActiveWorkflows(100).catch(() => [])
                : Promise.resolve([]),
        ]);
        if (requestToken !== historyRequestToken) return historyRecords;
        const activeByWorkflowId = new Map(
            (Array.isArray(activeCandidates) ? activeCandidates : [])
                .map(candidate => [String(candidate?.workflow?.workflow_id || ''), candidate])
                .filter(([workflowId]) => workflowId),
        );
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
            workflowApi.getWorkspace(historyId),
        ]);
        if (requestToken !== historyRequestToken) return;
        const authoritativeSnapshot = workspace?.snapshot || workflow;
        const files = resultFilesFromArtifacts(items, artifacts, workspace);
        if (authoritativeSnapshot?.execution_state !== 'TERMINAL') {
            const progress = workspace?.progress || {};
            const candidate = record.active_candidate || {};
            const completed = Number(progress.completed) || files.length;
            const total = Number(progress.total) || Number(record.total) || completed;
            if (candidate.can_resume === true && !candidate.requires_reconcile) {
                const confirmed = await showConfirmDialog({
                    kicker: '活动任务',
                    title: '恢复这条暂停任务？',
                    message: `当前进度：${completed} / ${total}。恢复后服务会从持久化状态继续执行。`,
                    detail: '只会发送带状态版本保护的恢复命令，不会重复提交已有未决的外部调用。',
                    tone: 'info',
                    confirmLabel: '恢复任务',
                });
                if (confirmed) {
                    try {
                        await workflowApi.sendCommand(historyId, 'resume', {
                            expected_state_version: Number(authoritativeSnapshot.state_version || record.state_version || 0),
                            reason: 'desktop-history-resume',
                        });
                        showToast('已发送恢复命令，任务会在后台继续处理');
                        await refreshHistoryRecords({ showLoading: false });
                    } catch (resumeError) {
                        console.error('恢复历史任务失败:', resumeError);
                        showToast(`恢复失败：${resumeError.message || '任务状态已变化，请刷新后重试'}`, 'error');
                    }
                }
                return;
            }
            const reason = candidate.requires_reconcile
                ? '存在未决外部副作用，必须先完成对账；应用不会自动重复提交。'
                : candidate.can_resume
                    ? '任务处于暂停状态，可从当前任务页恢复。'
                    : candidate.can_takeover
                        ? '任务没有未决外部副作用，服务启动后可安全接管。'
                        : '当前任务仍在处理或等待用户决策，请等待服务状态同步。';
            await showAlertDialog({
                kicker: '活动任务',
                title: record.source_filename || '未命名文档',
                message: `当前进度：${completed} / ${total}；任务尚未结束。`,
                detail: reason,
                tone: candidate.requires_reconcile ? 'warning' : 'info',
                confirmLabel: '知道了',
            });
            return;
        }
        const progress = workspace?.progress || {};
        const delivery = workspace?.delivery || {};
        const completed = Number.isInteger(Number(progress.completed))
            ? Number(progress.completed)
            : (Number(record.completed) || files.length);
        const total = Number.isInteger(Number(progress.total))
            ? Number(progress.total)
            : (Number(record.total) || files.length);
        const failed = Math.max(
            Number(progress.failed) || 0,
            total - completed,
            Number(record.failed) || 0,
        );
        const context = {
            mode: 'history',
            recordId: historyId,
            workflowId: historyId,
            sourceFilename: record.source_filename || '未命名文档.docx',
            files,
            artifacts: Array.isArray(workspace?.artifacts) ? workspace.artifacts : artifacts,
            completed,
            failed,
            total: total || Number(authoritativeSnapshot.item_count) || files.length,
            format: files[0]?.format || record.format || null,
            generationMode: record.generation_mode || GENERATION_MODE_SINGLE,
            preview: Boolean(record.preview),
            zipAvailable: Boolean(delivery.zip_available),
            zipArtifactId: delivery.zip_artifact_id || null,
            failedItems: Array.isArray(record.failed_items) ? record.failed_items : [],
            stateVersion: Number(authoritativeSnapshot.state_version || record.state_version || 0),
            executionState: authoritativeSnapshot.execution_state || record.execution_state || null,
            resultStatus: authoritativeSnapshot.result_status || record.result_status || null,
        };
        buildResultPage({
            workflow_id: historyId,
            completed: context.completed,
            failed: context.failed,
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
    const filename = record.source_filename || '未命名文档';
    const fileCount = Math.max(0, Number(record.available_files) || 0);
    const confirmed = await showConfirmDialog({
        kicker: '历史记录',
        title: '归档这条生成记录？',
        message: `将从历史列表隐藏「${filename}」及其 ${fileCount} 个音频文件。`,
        detail: '审计事件和 Artifact 会保留，不会物理删除文件；归档后可由受控恢复工具处理。',
        tone: 'danger',
        confirmLabel: '归档记录',
    });
    if (!confirmed) return;
    historyRequestToken++;
    if (button) button.disabled = true;
    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        await workflowApi.archiveWorkflow(workflowId, {
            expected_state_version: Number(record.state_version || 0),
            reason: 'desktop-history-archive',
        });
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
            if ($('history-back-btn')) $('history-back-btn').textContent = '← 返回导入文档';
        }
        setHistoryCounts(historyRecords.length);
        if (currentView === 'history') renderHistoryRecords(historyRecords);
        showToast(archivedCurrentResult ? '当前任务已归档，Artifact 仍保留' : '历史记录已归档');
    } catch (error) {
        console.error('归档历史记录失败:', error);
        showToast(`归档失败：${error.message || '请稍后重试'}`);
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
            const result = typeof window.electronAPI.selectFileStream === 'function'
                ? await window.electronAPI.selectFileStream()
                : await window.electronAPI.selectFile();
            if (result?.success && result.sourceFileId && result.fileName) {
                processSourceFileReference(result.sourceFileId, result.fileName, result.sizeBytes);
            } else if (result?.success && result.bytes && result.fileName) {
                // Compatibility fallback for an older preload that does not
                // expose the opaque native file stream handle.
                processSourceBytes(result.bytes, result.fileName);
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
        }
    } else {
        $('hidden-file-input').click();
    }
}

function handleFileSelected(file) {
    if (isRestarting || $('upload-zone')?.getAttribute('aria-disabled') === 'true') return;
    void ingestSourceFile(file);
}

/**
 * 导入拖拽/选择的源文档。Electron 下通过主进程分块暂存：渲染层每次只
 * 持有一个分块（约 4MB），文件内容由主进程在允许目录内落盘后按一次性
 * 句柄流式上传，300MB 级文档不再整块进入渲染进程内存。暂存不可用时
 * （例如浏览器模式）回退到原来的整块读取路径。
 */
async function ingestSourceFile(file) {
    if (!file || typeof file.slice !== 'function') return;
    const staging = isElectron ? window.electronAPI?.sourceUpload : null;
    if (staging && typeof staging.begin === 'function') {
        let uploadId = null;
        try {
            const opened = await staging.begin({ fileName: file.name, sizeBytes: file.size });
            uploadId = opened.uploadId;
            const chunkSize = Number(opened.chunkSize) || 4 * 1024 * 1024;
            let offset = 0;
            while (offset < Number(file.size)) {
                const chunk = new Uint8Array(await file.slice(offset, offset + chunkSize).arrayBuffer());
                await staging.write({ uploadId, offset, bytes: chunk });
                offset += chunk.byteLength;
            }
            const completed = await staging.complete(uploadId);
            if (!completed?.success || !completed.sourceFileId) {
                throw new Error(completed?.reason ? `文档流式导入未通过校验：${completed.reason}` : '文档流式导入失败');
            }
            processSourceFileReference(completed.sourceFileId, completed.fileName, completed.sizeBytes);
            return;
        } catch (error) {
            if (uploadId) await staging.abort(uploadId).catch(() => {});
            console.warn('拖拽文档流式导入失败，回退整块读取:', error);
            showToast(`文档流式导入失败，改为直接读取：${error.message || '未知错误'}`, 'warning');
        }
    }
    void file.arrayBuffer().then((buffer) => processSourceBytes(new Uint8Array(buffer), file.name));
}

let isParsing = false;  // 防止解析重入

async function processSourceBytes(bytes, filename) {
    return processSourceContent(bytes, filename, bytes?.byteLength);
}

async function processSourceFileReference(sourceFileId, filename, sizeBytes) {
    const size = Number(sizeBytes);
    if (!sourceFileId || !Number.isSafeInteger(size) || size <= 0) {
        showToast('文档大小无效，请重新选择', 'error');
        return;
    }
    return processSourceContent({ sourceFileId: String(sourceFileId) }, filename, size);
}

async function processSourceContent(content, filename, expectedSizeBytes) {
    if (isParsing || isRestarting) return;  // 防止重入
    const isBytes = content instanceof Uint8Array;
    const hasSourceFileReference = Boolean(content && typeof content === 'object' && content.sourceFileId);
    const expectedSize = Number(expectedSizeBytes);
    if ((!isBytes && !hasSourceFileReference) || !Number.isSafeInteger(expectedSize) || expectedSize <= 0) {
        showToast('文档内容为空，请重新选择', 'error');
        return;
    }
    isParsing = true;
    const attemptId = ++parseAttemptId;
    const controller = new AbortController();
    parseAbortController = controller;

    const safeFilename = String(filename || 'source.docx').split(/[\\/]/).pop() || 'source.docx';
    const uploadZone = $('upload-zone');
    uploadZone.classList.add('has-file');
    setUploadParsing(true);
    setUploadFeedback('info', '正在读取并核对文档结构，请稍候…');
    uploadZone.querySelector('.upload-text-large').textContent = safeFilename;
    uploadZone.querySelector('.upload-hint').textContent = '正在解析文档结构...';
    $('status-text').textContent = `正在解析: ${safeFilename}`;

    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        const initialConfiguration = buildWorkflowConfiguration(
            collectConfig(false),
            safeFilename,
            currentConfig?.account_scope,
        );
        const draft = await workflowApi.createWorkflow({
            workflow_type: 'tts',
            configuration: initialConfiguration,
        });
        const imported = await workflowApi.createSourceImport(draft.workflow_id, {
            metadata: { filename: safeFilename },
            expected_size_bytes: expectedSize,
            content_type: safeFilename.toLowerCase().endsWith('.xlsx')
                ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        });
        await workflowApi.writeSourceImport(imported.source_import_id, imported.staging_generation, content);
        const ready = await workflowApi.getSourceImport(imported.source_import_id);
        if (!ready.source_artifact_id) throw new Error('文档内容未能写入受控存储');
        // Committing the source artifact advances the workflow aggregate's
        // state_version. Re-read it before publishing parse output instead of
        // reusing the draft version captured before the upload.
        const sourceWorkflow = await workflowApi.getWorkflow(draft.workflow_id);
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

        updateSessionLabels(currentSession.source_filename, currentSession.parse_results);
        uploadZone.querySelector('.upload-hint').textContent = '解析完成，正在打开声音配置';
        setUploadFeedback('success', `解析完成：已识别 ${summarizeParseResults(currentSession.parse_results).total} 条内容。`);
        $('status-text').textContent = `解析成功 — ${currentSession.source_filename}`;
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
    return {
        ...normalized,
        source_filename: String(sourceFilename || config?.source_filename || '').trim().slice(0, 256),
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
                mergeWorkflowSnapshotIntoSession({
                    ...snapshot,
                    latest_event_id: frame.snapshot?.snapshot_event_id || snapshot.latest_event_id,
                    latest_seq: frame.snapshot?.snapshot_seq ?? snapshot.latest_seq,
                }, currentSession);
                currentSession.last_event_id = frame.snapshot?.snapshot_event_id
                    || currentSession.last_event_id
                    || currentSession.latest_event_id
                    || null;
                $('status-text').textContent = snapshot.execution_state === 'TERMINAL' ? '任务已结束' : '生成记录已同步';
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

function handleWorkflowEvent(event, sessionId) {
    const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
    const attemptKey = String(payload.attempt_id || event.attempt_id || payload.submission_id || event.seq || 'current');
    const eventMessage = String(payload.message || payload.error || payload.error_code || '生成任务未能完成');
    const recoveryTarget = payload.target && typeof payload.target === 'object'
        ? {
            attempt_id: payload.attempt_id || event.attempt_id || null,
            work_unit_id: payload.target.work_unit_id || payload.work_unit_id || null,
            target_state_version: Number(payload.target_state_version),
            workflow_state_version: Number(payload.workflow_state_version || currentSession?.state_version || 0),
            submission_id: payload.submission_id || null,
        }
        : null;
    if (event.event_type === 'TTS_PLAN_PREPARED') {
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
        const target = recoveryTarget || {
            attempt_id: payload.attempt_id || event.attempt_id || null,
            work_unit_id: payload.work_unit_id || null,
            target_state_version: Number(payload.target_state_version),
            workflow_state_version: Number(payload.workflow_state_version || currentSession?.state_version || 0),
            submission_id: payload.submission_id || null,
        };
        addLogEntry({ level: 'warn', stage: 'synthesize', kind: 'summary', status: 'warning', seq: event.seq, key: `tts:ambiguous:${attemptKey}`, title: '讯飞提交结果待核验', detail: eventMessage });
        handleSSEEvent({ type: 'error', msg: eventMessage, ambiguous: true, recoveryTarget: target });
    } else if (event.event_type === 'TTS_SUBMISSION_REJECTED') {
        addLogEntry({ level: 'error', stage: 'synthesize', kind: 'summary', status: 'error', seq: event.seq, key: `tts:rejected:${attemptKey}`, title: '讯飞提交未被接受', detail: eventMessage });
        handleSSEEvent({ type: 'error', msg: eventMessage, ambiguous: false });
    } else if (event.event_type === 'TTS_OUTPUT_VERIFIED') {
        // The event is evidence of the worker's write, not itself the final
        // UI state.  Read the authoritative snapshot and verified artifact
        // projection before moving to the result page.
        setProgressIndeterminate(true);
        $('status-text').textContent = '音频已写入，正在确认最终任务状态…';
        void Promise.all([
            workflowApi?.getWorkflow(sessionId),
            refreshGeneratedArtifacts(sessionId),
        ]).then(([snapshot]) => {
            if (currentSession?.session_id !== sessionId) return;
            mergeWorkflowSnapshotIntoSession(snapshot, currentSession);
            if (snapshot?.execution_state !== 'TERMINAL'
                || !['SUCCEEDED', 'PARTIAL_SUCCESS'].includes(snapshot?.result_status)) {
                $('status-text').textContent = '音频已写入，任务状态仍在确认中…';
                return;
            }
            const total = summarizeParseResults(currentSession?.parse_results).total;
            const completed = generatedFiles.length;
            const failed = Math.max(0, total - completed);
            addLogEntry({ level: 'success', stage: 'complete', kind: 'summary', status: 'success', seq: event.seq, key: `tts:verified:${attemptKey}`, title: '音频已完成核验', detail: '生成文件已写入本地任务空间。' });
            handleDone({ type: 'done', completed, failed, total, artifact_ids: payload.artifact_ids || [], file_list: generatedFiles });
        }).catch((error) => {
            console.warn('核验生成终态失败，保留任务页等待重连:', error);
            $('status-text').textContent = '正在确认最终任务状态，请稍候…';
        });
    } else if (event.event_type === 'GENERATION_TASK_FAILED') {
        void workflowApi?.getWorkflow(sessionId).then((snapshot) => {
            if (currentSession?.session_id !== sessionId) return;
            mergeWorkflowSnapshotIntoSession(snapshot, currentSession);
            const settled = snapshot?.execution_state === 'TERMINAL'
                || ['WAITING_RETRY', 'WAITING_USER', 'BLOCKED'].includes(snapshot?.execution_state);
            if (!settled) {
                $('status-text').textContent = '生成服务正在恢复任务状态…';
                return;
            }
            addLogEntry({ level: 'error', stage: 'complete', kind: 'summary', status: 'error', seq: event.seq, key: `task:failed:${attemptKey}`, title: '生成任务未能完成', detail: eventMessage });
            handleSSEEvent({ type: 'error', msg: eventMessage, ambiguous: payload.error_code === 'SUBMISSION_AMBIGUOUS' || snapshot.execution_state === 'WAITING_USER', recoveryTarget });
        }).catch((error) => {
            console.warn('生成失败后同步工作流状态失败，保留任务页:', error);
            $('status-text').textContent = '生成失败，正在等待任务状态同步…';
        });
    } else if (event.event_type === 'WORKFLOW_CANCEL') {
        setProgressIndeterminate(true);
        $('status-text').textContent = '正在停止生成任务…';
    } else if (event.event_type === 'WORKFLOW_CANCELLED') {
        void Promise.all([
            workflowApi?.getWorkflow(sessionId),
            refreshGeneratedArtifacts(sessionId),
        ]).then(([snapshot]) => {
            if (currentSession?.session_id !== sessionId) return;
            mergeWorkflowSnapshotIntoSession(snapshot, currentSession);
            if (snapshot?.execution_state !== 'TERMINAL') {
                $('status-text').textContent = '正在确认取消结果…';
                return;
            }
            const total = summarizeParseResults(currentSession?.parse_results).total;
            handleSSEEvent({ type: 'cancelled', completed: generatedFiles.length, total });
        }).catch((error) => {
            console.warn('取消后同步工作流状态失败:', error);
            $('status-text').textContent = '正在确认取消结果…';
        });
    }
}

async function refreshGeneratedArtifacts(sessionId) {
    if (!workflowApi) return [];
    const [items, artifacts, workspace] = await Promise.all([
        workflowApi.listItems(sessionId),
        workflowApi.listArtifacts(sessionId),
        workflowApi.getWorkspace(sessionId),
    ]);
    // The request can outlive a restart/new task.  Never let a late response
    // from the old workflow overwrite the result list of the current task.
    if (currentSession?.session_id !== sessionId) return [];
    generatedFiles = resultFilesFromArtifacts(items, artifacts, workspace);
    if (workspace) {
        currentSession.delivery = workspace.delivery ? {
            zip_available: Boolean(workspace.delivery.zip_available),
            zip_artifact_id: workspace.delivery.zip_artifact_id || null,
        } : null;
        currentSession.progress = workspace.progress ? {
            total: Number(workspace.progress.total) || 0,
            completed: Number(workspace.progress.completed) || 0,
            failed: Number(workspace.progress.failed) || 0,
            skipped: Number(workspace.progress.skipped) || 0,
            pending: Number(workspace.progress.pending) || 0,
        } : null;
        mergeWorkflowSnapshotIntoSession(workspace.snapshot, currentSession);
    }
    return generatedFiles;
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
    sseReconnectTimer = setTimeout(() => {
        sseReconnectTimer = null;
        if (connectionToken === sseConnectionToken && isGenerating) void connectSSE(sessionId);
    }, delay);
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
            setProgressIndeterminate(false);
            $('gen-title').textContent = '任务已取消';
            $('generation-file-name').textContent = `已取消「${currentSession?.source_filename || '当前文档'}」的生成任务。`;
            $('status-text').textContent = '生成任务已取消';
            setGenerationVisualState('stopped');
            showToast('任务已取消');
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
            $('status-text').textContent = `错误: ${event.msg}`;
            setGenerationVisualState('error');
            showGenerationRecovery(`生成出错：${event.msg}`, {
                ambiguous: Boolean(event.ambiguous),
                target: event.recoveryTarget || null,
            });
            break;

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
    setProgressIndeterminate(false);
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

// ============================================================================
// Step 4: 完成
// ============================================================================

function handleDone(event) {
    resetGenerateState();
    setProgressIndeterminate(false);
    generationResult = 'done';
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
        total: lastStats ? lastStats.total : (event.total || 0),
        failed_items: lastStats?.failed_items || event.failed_items || [],
    };
    latestCurrentResultEvent = {
        ...doneData,
        workflow_id: doneData.workflow_id || currentSession?.session_id || null,
    };

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

    showToast(doneData.failed > 0 ? `任务结束，${doneData.failed} 条生成失败` : '处理完成');
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

async function readArtifactBytes(artifactId) {
    if (!workflowApi || !artifactId) throw new Error('Artifact 标识缺失');
    const stream = await workflowApi.openArtifact(artifactId);
    const reader = stream.getReader();
    const chunks = [];
    let total = 0;
    try {
        while (true) {
            const part = await reader.read();
            if (part.done) break;
            const chunk = part.value instanceof Uint8Array ? part.value : new Uint8Array(part.value || []);
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

function buildResultPage(event, suppliedContext = null) {
    destroyWaveSurfers();
    const workflowSourceTotal = summarizeParseResults(currentSession?.parse_results).total;
    const context = suppliedContext || {
        mode: 'current',
        sessionId: currentSession?.session_id,
        workflowId: currentSession?.session_id || event.workflow_id || null,
        sourceFilename: currentSession?.source_filename,
        files: generatedFiles,
        completed: event.completed || generatedFiles.length || 0,
        failed: event.failed || 0,
        total: workflowSourceTotal || event.total || 0,
        format: lastGenerationConfig?.format || currentConfig?.format || 'mp3',
        preview: Boolean(lastGenerationConfig?.preview && workflowSourceTotal > 3),
        zipAvailable: Boolean(currentSession?.delivery?.zip_available || event.zip_artifact_id),
        zipArtifactId: currentSession?.delivery?.zip_artifact_id || event.zip_artifact_id || null,
        failedItems: Array.isArray(event.failed_items) ? event.failed_items : [],
        stateVersion: Number(currentSession?.state_version || 0),
        executionState: currentSession?.execution_state || event.execution_state || null,
        resultStatus: currentSession?.result_status || event.result_status || null,
    };
    activeResultContext = context;
    const isHistory = context.mode === 'history';
    const resultFiles = Array.isArray(context.files) ? context.files : [];
    const reportedCompleted = Math.max(0, Number(context.completed) || 0);
    const success = resultFiles.length;
    const missingFiles = Math.max(0, reportedCompleted - success);
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
    if (resultWarning) resultWarning.hidden = failed === 0;
    if (resultWarningText && failed > 0) {
        resultWarningText.textContent = isHistory
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
    if (success === 0 && failed > 0) {
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
        : (isPreviewResult
            ? `本次试听生成 ${success} 个音频${failed > 0 ? `，失败 ${failed} 个` : ''}；确认效果后可继续生成完整文档`
            : `成功生成 ${success} 个音频文件${failed > 0 ? `，失败 ${failed} 个` : ''}`);
    $('result-summary').textContent = summaryText;
    $('result-success-label').textContent = isPreviewResult ? '试听文件' : '已生成';
    $('result-success-count').textContent = String(success);
    $('result-success-caption').textContent = isPreviewResult && !isHistory
        ? `本次范围：前 ${Math.min(sourceTotal, 3)} 条`
        : '音频文件';
    $('result-secondary-label').textContent = isPreviewResult && !isHistory ? '文档总量' : '未完成';
    $('result-failed-count').textContent = String(isPreviewResult && !isHistory ? sourceTotal : failed);
    $('result-secondary-caption').textContent = isPreviewResult && !isHistory ? '完整文档内容' : '待处理内容';
    $('result-format-value').textContent = String(resultFiles[0]?.format || context.format || '待同步').toUpperCase();

    // ZIP 卡片
    const zipCard = $('zip-card');
    const resultHero = $('result-hero');
    // The delivery projection is authoritative for an already-created ZIP.
    // A terminal result with verified audio still exposes the on-demand
    // action; the server creates and verifies the ZIP when it is clicked.
    const zipState = resultZipState(context, success);
    if (zipState.visible) {
        zipCard.style.display = 'flex';
        resultHero?.classList.remove('has-no-package');
        $('zip-desc').textContent = zipState.ready
            ? `ZIP 压缩包包含 ${success} 个已生成的音频文件`
            : `点击下载时自动整理 ${success} 个已生成的音频文件`;
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
        item._audioElement = audio;
        item._artifactId = f.artifact_id || null;
        let audioReadyPromise = null;
        let audioObjectUrl = null;
        const resetAudioSource = () => {
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
        item.ensureAudioReady = async () => {
            if (audio.src) return audio;
            if (!item._artifactId) throw new Error('音频 Artifact 不可用');
            if (!audioReadyPromise) {
                const pending = (async () => {
                    const bytes = await readArtifactBytes(item._artifactId);
                    if (renderToken !== waveformRenderToken) throw new Error('结果页已切换');
                    const url = URL.createObjectURL(new Blob([bytes], { type: artifactMime(f.format) }));
                    audioObjectUrl = url;
                    artifactObjectUrls.add(url);
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
    waveformItems.slice(0, 2).forEach(item => {
        void item.ensureAudioReady?.().catch(error => console.warn('预加载 Artifact 失败:', error));
    });
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

async function saveNativeArtifactStream(artifactId, suggestedName) {
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
        let artifactId = target.zipArtifactId;
        if (!artifactId) {
            const artifacts = target.artifacts || await workflowApi.listArtifacts(workflowId);
            artifactId = artifacts.find(artifact => (
                artifact.lifecycle_state === 'READY'
                && artifact.verified === true
                && artifact.artifact_type === 'export-zip'
            ))?.artifact_id;
        }
        if (!artifactId) {
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
            if (artifactId) {
                target.zipArtifactId = artifactId;
                target.zipAvailable = true;
            }
        }
        if (!artifactId) {
            showToast('当前工作流没有可下载的 ZIP Artifact');
            return false;
        }
        const sourceName = String(target.sourceFilename || PRODUCT_NAME).replace(/\.(docx|xlsx)$/i, '');
        if (isElectron && typeof window.electronAPI?.saveArtifactStream === 'function') {
            return saveNativeArtifactStream(artifactId, `${sourceName}_tts.zip`);
        }
        const bytes = await readArtifactBytes(artifactId);
        return saveArtifactBytes(bytes, `${sourceName}_tts.zip`, 'zip');
    } catch (err) {
        console.error('下载 ZIP 异常:', err);
        showToast('下载失败：Artifact 暂时不可用');
        return false;
    }
}

async function downloadFile(filename, context = activeResultContext) {
    const target = context || (currentSession ? {
        mode: 'current',
        sessionId: currentSession.session_id,
        workflowId: currentSession.session_id,
        files: generatedFiles,
    } : null);
    if (!target || !filename) return;
    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        const file = (Array.isArray(target.files) ? target.files : []).find(item => item.filename === filename);
        if (!file?.artifact_id) {
            showToast('音频 Artifact 不存在');
            return false;
        }
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
    clearGenerationStartupTimer();
    sseConnectionToken++;

    const sessionToCleanup = currentSession;
    currentSession = null;
    isGenerating = false;

    // 断开 SSE
    if (workflowStream) {
        workflowStream.close().catch(() => {});
        workflowStream = null;
    }

    // 如果有会话，通知后端清理
    if (sessionToCleanup) {
        pendingCleanupSessionId = sessionToCleanup.session_id;
        cleanupConfirmed = await confirmPendingCleanup(sessionToCleanup);
        if (!cleanupConfirmed) {
            cleanupConfirmed = false;
            setServiceState('warning', '任务清理待确认');
            $('retry-service-btn').hidden = false;
        }
    }

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
    lastAmbiguousRecoveryTarget = null;
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
    setProgressIndeterminate(false);
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
    const phase = String(workspace.phase || '');
    const message = String(workspace.runtime?.message || '');
    const segments = workspace.segments || {};
    const hasSegments = Number(segments.total) > 0;
    const completed = hasSegments ? Math.min(Math.max(0, Number(segments.completed) || 0), Number(segments.total)) : 0;
    const renderKey = [
        phase,
        message,
        hasSegments ? `${completed}/${segments.total}` : 'none',
        String(workspace.items.total ?? ''),
        String(workspace.executionState ?? ''),
    ].join('|');
    if (renderKey === lastWorkspaceRenderKey) return;
    lastWorkspaceRenderKey = renderKey;

    if (hasSegments) {
        const percent = Math.min(100, Math.round((completed / Number(segments.total)) * 100));
        $('progress-bar').style.width = `${percent}%`;
        $('progress-bar').parentElement?.setAttribute('aria-valuenow', String(percent));
        $('progress-percent').textContent = String(percent);
        setProgressIndeterminate(false);
        $('progress-stats').textContent = `${message || '讯飞浏览器处理中'} · ${completed} / ${segments.total}`;
    } else if (phase && phase !== 'attention') {
        // 分段计数还没产生（浏览器启动/准备阶段）：保持不确定进度，只
        // 同步阶段文案。
        setProgressIndeterminate(true);
        if (message) $('progress-stats').textContent = `${message} · 0 / ${summarizeParseResults(currentSession?.parse_results).total || '—'}`;
    }
    if (message && $('generation-live-status')?.textContent !== message) {
        $('generation-live-status').textContent = message;
        $('status-text').textContent = message;
    }
}

if (workflowStore && typeof workflowStore.subscribe === 'function') {
    workflowStore.subscribe(renderWorkflowWorkspace);
}

// ============================================================================
// 启动
// ============================================================================

init();
