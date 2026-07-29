/**
 * Word → TTS — Frontend Logic v2
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
let waveformRenderToken = 0;     // 使离开结果页后排队中的回调失效
let isRestarting = false;        // 防止 cleanup 等待期间重复重置或重新上传
let pendingCleanupSessionId = null; // 未确认清理完成前禁止创建同名新任务

// ============================================================================
// 配置预设管理 (localStorage 持久化)
// ============================================================================

const PRESET_STORAGE_KEY = 'wordtts_presets_v1';

/**
 * 从 localStorage 读取所有预设。
 */
function loadPresets() {
    try {
        const raw = localStorage.getItem(PRESET_STORAGE_KEY);
        if (!raw) return [];
        const arr = JSON.parse(raw);
        return Array.isArray(arr) ? arr : [];
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
        localStorage.setItem(PRESET_STORAGE_KEY, JSON.stringify(presets));
        return true;
    } catch (e) {
        console.error('保存预设失败:', e);
        showToast('保存失败：存储空间不足');
        return false;
    }
}

/**
 * 自定义输入对话框（替代 Electron 不支持的 window.prompt）。
 * @param {string} title - 对话框标题
 * @param {string} message - 提示文字
 * @param {string} defaultValue - 输入框默认值
 * @returns {Promise<string|null>} 用户输入的值，取消时返回 null
 */
function showPromptDialog(title, message, defaultValue = '') {
    return new Promise((resolve) => {
        const overlay = $('prompt-overlay');
        const titleEl = $('prompt-title');
        const messageEl = $('prompt-message');
        const input = $('prompt-input');
        const okBtn = $('prompt-ok');
        const cancelBtn = $('prompt-cancel');
        const previousFocus = document.activeElement;

        titleEl.textContent = title;
        messageEl.textContent = message;
        input.value = defaultValue;

        $('app').setAttribute('inert', '');
        overlay.setAttribute('aria-hidden', 'false');
        overlay.classList.add('active');
        // 延迟聚焦以确保 transition 完成
        setTimeout(() => { input.focus(); input.select(); }, 50);

        let resolved = false;
        const done = (value) => {
            if (resolved) return;
            resolved = true;
            overlay.classList.remove('active');
            overlay.setAttribute('aria-hidden', 'true');
            $('app').removeAttribute('inert');
            // 清理事件监听器（一次性）
            okBtn.removeEventListener('click', onOk);
            cancelBtn.removeEventListener('click', onCancel);
            overlay.removeEventListener('keydown', onKeydown);
            if (previousFocus && typeof previousFocus.focus === 'function') {
                requestAnimationFrame(() => previousFocus.focus());
            }
            resolve(value);
        };

        const onOk = () => done(input.value);
        const onCancel = () => done(null);
        const onKeydown = (e) => {
            if (e.key === 'Enter' && e.target === input) { e.preventDefault(); done(input.value); }
            else if (e.key === 'Escape') { e.preventDefault(); done(null); }
            else if (e.key === 'Tab') {
                const focusable = [input, cancelBtn, okBtn];
                const currentIndex = focusable.indexOf(document.activeElement);
                const nextIndex = e.shiftKey
                    ? (currentIndex <= 0 ? focusable.length - 1 : currentIndex - 1)
                    : (currentIndex >= focusable.length - 1 ? 0 : currentIndex + 1);
                e.preventDefault();
                focusable[nextIndex].focus();
            }
        };

        okBtn.addEventListener('click', onOk);
        cancelBtn.addEventListener('click', onCancel);
        overlay.addEventListener('keydown', onKeydown);
    });
}

/**
 * 生成预设描述文字。
 */
function presetSummary(config) {
    if (!config) return '配置数据缺失';
    const parts = [];
    parts.push(`语速 ${config.rate ?? 1.0}x`);
    parts.push(`音量 ${Math.round((config.volume ?? 1) * 100)}%`);
    parts.push(`音调 ${config.pitch ?? 1}x`);
    parts.push((config.format || 'mp3').toUpperCase());
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
}

/**
 * 安全设置 <select> 的值。
 *
 * JavaScript 中 String(1.0) === "1"，但 <option value="1.0"> 的值是 "1.0"，
 * 直接赋值会匹配失败。此函数先尝试精确匹配，再回退到数值匹配。
 */
function setSelectValue(selectEl, value, defaultValue) {
    const str = String(value ?? defaultValue);
    // 精确匹配
    for (const opt of selectEl.options) {
        if (opt.value === str) {
            selectEl.value = str;
            return;
        }
    }
    // 数值匹配（处理 1.0 → "1" vs "1.0" 等情况）
    const num = parseFloat(str);
    if (!isNaN(num)) {
        for (const opt of selectEl.options) {
            if (parseFloat(opt.value) === num) {
                selectEl.value = opt.value;
                return;
            }
        }
    }
}

/**
 * 将配置应用到 Step 2 表单。
 */
function applyConfigToForm(config) {
    if (!config) return;
    setSelectValue($('rate'), config.rate, 1.0);
    setSelectValue($('volume'), config.volume, 1);
    setSelectValue($('pitch'), config.pitch, 1);
    setSelectValue($('pause'), config.pause, 0);
    setSelectValue($('format'), config.format, 'mp3');
    setSelectValue($('quality'), config.quality, '128 kbps（标准）');
    $('proxy').value = config.proxy ?? '';
    $('preview').checked = !!config.preview;
    enforceOutputCompatibility($('format'));
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
    const config = collectConfig(false);
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
    if (select) select.value = preset.id;

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
    applyConfigToForm(preset.config);
    showToast(`已应用配置「${preset.name}」`);
}

/**
 * 删除选中的预设。
 */
function handleDeletePreset() {
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

    if (!window.confirm(`确定删除配置「${preset.name}」吗？`)) return;

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

    const session = currentSession;
    const config = presetConfig || collectConfig(useDefaults);
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
    updateSessionLabels(session.source_filename, session.parse_results, {
        preview: isPreviewScope,
        total: generationTotal,
    });
    hideGenerationRecovery();
    setGenerationVisualState('running');

    // 重置生成页面 UI
    $('progress-bar').style.width = '0%';
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', '0');
    $('progress-stats').textContent = '准备中...';
    $('progress-percent').textContent = '0';
    $('progress-completed').textContent = '0';
    $('progress-remaining').textContent = generationTotal || '—';
    $('progress-failed').textContent = '0';
    $('generation-live-status').textContent = '正在启动音频引擎';
    $('gen-title').textContent = '正在生成音频';
    $('gen-animation').classList.remove('done');
    resetLogTimeline('生成任务即将开始，正在等待第一条处理记录…');
    $('type-stats').innerHTML = '';

    lastGenerationConfig = { ...config };

    try {
        const resp = await fetch(apiUrl('/api/generate'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: session.session_id,
                source_filename: session.source_filename,
                file_path: session.file_path,
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
    const displayName = filename || '已导入的 Word 文档';
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
            : 'WordTTS 正在准备当前文档，请保持应用开启。';
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

function selectedOptionLabel(id) {
    const select = $(id);
    return select && select.selectedOptions.length
        ? select.selectedOptions[0].textContent.trim()
        : '';
}

function updateConfigSummary() {
    const mapping = [
        ['summary-rate', 'rate'],
        ['summary-volume', 'volume'],
        ['summary-pitch', 'pitch'],
        ['summary-pause', 'pause'],
    ];
    mapping.forEach(([targetId, selectId]) => {
        const target = $(targetId);
        const label = selectedOptionLabel(selectId);
        if (target && label) target.textContent = label;
    });

    const output = $('summary-output');
    if (output) {
        const format = $('format') ? $('format').value.toUpperCase() : 'MP3';
        const quality = $('quality') ? $('quality').value : '128 kbps（标准）';
        const qualityShort = quality.match(/^(\d+\s*kbps|无损)/)?.[1] || quality;
        output.textContent = `${format} · ${qualityShort}`;
    }

    const scope = $('summary-scope');
    if (scope) scope.textContent = $('preview')?.checked ? '试听前 3 条' : '完整文档';
    updateSessionLabels(currentSession?.source_filename || '', currentSession?.parse_results);
}

function enforceOutputCompatibility(changedControl) {
    const format = $('format');
    const quality = $('quality');
    if (!format || !quality) return;

    const isLossless = quality.value.startsWith('无损');
    if (changedControl === quality && isLossless && format.value !== 'wav') {
        format.value = 'wav';
        showToast('无损质量仅适用于 WAV，已自动切换格式');
    } else if (changedControl === format && format.value !== 'wav' && isLossless) {
        setSelectValue(quality, '128 kbps（标准）', '128 kbps（标准）');
        showToast('当前格式不支持无损质量，已恢复为 128 kbps');
    }
    if (format.value === 'wav') {
        setSelectValue(quality, '无损（仅 wav 生效）', '无损（仅 wav 生效）');
        quality.disabled = true;
        quality.title = 'WAV 使用无损输出，无需选择码率';
    } else {
        quality.disabled = false;
        quality.title = '';
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
        done: '处理完成',
        error: '需要处理',
        warning: '部分完成',
        stopped: '任务已停止',
    };
    if (badgeLabel) badgeLabel.textContent = labels[state] || labels.running;
    const liveLabels = {
        running: '音频引擎运行中',
        done: '所有音频处理完成',
        warning: '任务完成，部分内容需处理',
        error: '生成遇到问题，请检查记录',
        stopped: '任务已停止',
    };
    if (liveStatus) liveStatus.textContent = liveLabels[state] || liveLabels.running;
    const liveLabelTexts = {
        running: '实时合成',
        done: '合成完成',
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
        const ready = await window.electronAPI.serverReady();
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

async function init() {
    if (platform === 'darwin') {
        document.body.classList.add('platform-darwin');
    } else if (platform === 'win32') {
        document.body.classList.add('platform-win32');
    }

    bindEvents();

    // 初始化预设 UI
    refreshPresetUI();

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
        if (file && file.name.toLowerCase().endsWith('.docx')) {
            handleFileSelected(file);
        } else {
            setUploadFeedback('error', '文件格式不支持，请重新选择 .docx 文档。');
            showToast('请选择 .docx 格式的 Word 文档', 'error');
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
    ['rate', 'volume', 'pitch', 'pause'].forEach(id => {
        $(id).addEventListener('change', updateConfigSummary);
    });
    $('format').addEventListener('change', (e) => enforceOutputCompatibility(e.currentTarget));
    $('quality').addEventListener('change', (e) => enforceOutputCompatibility(e.currentTarget));
    $('preview').addEventListener('change', updateConfigSummary);
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
        applyConfigToForm(collectConfig(true));
        showToast('已恢复推荐设置');
    });
    $('start-generate-btn').addEventListener('click', () => {
        goToStep(3);
        startProcessing(false);
    });

    $('audio-search-input').addEventListener('input', filterAudioItems);
    $('audio-type-filter').addEventListener('change', filterAudioItems);

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
        applyConfigToForm({ ...lastGenerationConfig, preview: false });
        goToStep(2);
        showToast('已保留试听设置，确认后可生成完整文档');
    });
    $('result-return-config-btn').addEventListener('click', () => {
        destroyWaveSurfers();
        if (lastGenerationConfig) applyConfigToForm(lastGenerationConfig);
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

function bindSliderDisplay(slider, display, formatter) {
    if (!slider || !display) return;
    const update = () => { display.textContent = formatter(slider.value); };
    slider.addEventListener('input', update);
    update();
}

// ============================================================================
// 配置加载
// ============================================================================

async function loadConfig() {
    const maxRetries = 3;
    for (let attempt = 1; attempt <= maxRetries; attempt++) {
        try {
            const resp = await fetch(apiUrl('/api/config'));
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            currentConfig = await resp.json();

            // 男声引擎显示（TTSMaker 或 edge-tts）
            const maleNameEl = $('voice-male-name');
            const maleDescEl = $('voice-male-desc');
            if (maleNameEl && maleDescEl) {
                if (currentConfig.ttsmaker_available) {
                    maleNameEl.textContent = 'Alfie · TTSMaker 788';
                    maleDescEl.textContent = 'm/M 标识 → 男声 · 通过 TTSMaker 网站生成';
                } else {
                    maleNameEl.textContent = 'Remy · edge-tts';
                    maleDescEl.textContent = 'm/M 标识 → 男声 · TTSMaker 不可用，回退到 edge-tts';
                }
            }

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

function goToStep(step) {
    currentView = 'workflow';
    currentStep = step;

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
        status.className = `history-status-badge${failed > 0 || missingCount > 0 ? ' is-partial' : ''}`;
        status.textContent = availableCount === 0
            ? '文件缺失'
            : (missingCount > 0 ? '部分缺失' : (failed > 0 ? '部分完成' : '已完成'));
        titleRow.append(title, status);

        const meta = document.createElement('div');
        meta.className = 'history-item-meta';
        const completedAt = document.createElement('span');
        completedAt.textContent = historyDateLabel(record.completed_at);
        const scope = document.createElement('span');
        scope.textContent = record.preview ? '试听任务' : '完整任务';
        meta.append(completedAt, scope);

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
    if (!window.confirm(`删除「${filename}」及其 ${fileCount} 个音频文件？\n删除后无法恢复。`)) return;
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
            if (uploadHint) uploadHint.textContent = '支持 .docx 文件 · 选择后会自动解析';
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
        const filePath = await window.electronAPI.selectFile();
        if (filePath) {
            handleFilePath(filePath);
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
        currentSession = {
            session_id: data.session_id,
            source_filename: data.source_filename,
            file_path: data.file_path,
            parse_results: data.parse_results || [],
        };

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
        currentSession = {
            session_id: data.session_id,
            source_filename: data.source_filename,
            file_path: data.file_path,
            parse_results: data.parse_results || [],
        };
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
        return {
            rate: 1.0,
            volume: 1,
            pitch: 1,
            pause: 0,
            format: 'mp3',
            quality: '128 kbps（标准）',
            proxy: '',
            preview: false,
        };
    }
    return {
        rate: parseFloat($('rate').value),
        volume: parseFloat($('volume').value),
        pitch: parseFloat($('pitch').value),
        pause: parseInt($('pause').value),
        format: $('format').value,
        quality: $('quality').value,
        proxy: $('proxy').value || '',
        preview: $('preview').checked,
    };
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
            showGenerationRecovery(`生成出错：${event.msg}`);
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
        const processed = Number(lastStats.processed ?? ((lastStats.completed || 0) + (lastStats.failed || 0)));
        const pending = Number(lastStats.pending ?? Math.max(lastStats.total - processed, 0));
        const eta = formatLogDuration(lastStats.eta_ms);
        $('log-summary').textContent = `已处理 ${processed} / ${lastStats.total} · 剩余 ${pending}${issueCount ? ` · ${issueCount} 条异常记录` : ''}${eta ? ` · 预计还需 ${eta}` : ''}`;
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
    $('log-summary').textContent = '准备记录生成阶段与每条音频结果';
    $$('#log-stage-rail [data-log-stage]').forEach(stage => {
        stage.classList.remove('is-active', 'is-complete', 'is-warning', 'is-error');
        const stageLabel = LOG_STAGE_LABELS[stage.dataset.logStage] || stage.textContent.trim();
        stage.setAttribute('aria-label', `${stageLabel}：未开始`);
        stage.removeAttribute('aria-current');
    });
    setLogFilter('all');
    setLogAutoFollow(true);
    setLogDetailsExpanded(true);
}

// ============================================================================
// 进度 & 统计
// ============================================================================

function updateProgress(event) {
    const processed = Math.min((event.completed || 0) + (event.failed || 0), event.total || 0);
    const pct = event.total > 0 ? (processed / event.total) * 100 : 0;
    const eta = formatLogDuration(event.eta_ms);
    $('progress-bar').style.width = `${pct.toFixed(1)}%`;
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', pct.toFixed(1));
    $('progress-stats').textContent = `${event.completed} / ${event.total}`
        + (event.failed > 0 ? `  ·  失败 ${event.failed}` : '')
        + (eta ? `  ·  预计 ${eta}` : '');
    $('progress-percent').textContent = String(Math.round(pct));
    $('progress-completed').textContent = String(event.completed || 0);
    $('progress-remaining').textContent = String(Math.max((event.total || 0) - processed, 0));
    $('progress-failed').textContent = String(event.failed || 0);
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
    generationResult = 'done';
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

    if (generatedFiles.length > 0 && !doneData.history_id) {
        showToast('音频已完成，但历史记录保存失败，请先下载结果');
    } else {
        showToast(doneData.failed > 0 ? `任务结束，${doneData.failed} 条生成失败` : '处理完成');
    }
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
        zipAvailable: Boolean(event.zip_path),
        failedItems: Array.isArray(event.failed_items) ? event.failed_items : [],
    };
    activeResultContext = context;
    const isHistory = context.mode === 'history';
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
    if (!isHistory && success > 0 && !context.historyId) {
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
        for (let barIndex = 0; barIndex < 64; barIndex++) {
            const bar = document.createElement('span');
            const height = 18 + ((waveSeed + barIndex * 29 + (barIndex % 7) * 13) % 68);
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

        audioList.appendChild(item);

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
                    const sourceName = String(target.sourceFilename || 'WordTTS').replace(/\.docx$/i, '');
                    const result = await window.electronAPI.saveFileByPath(data.path, `${sourceName}_tts.zip`);
                    if (result && result.success) {
                        showToast('下载成功');
                    } else {
                        const reason = result?.reason || 'unknown';
                        if (reason === 'user-cancelled') {
                            showToast('已取消');
                        } else if (reason === 'path-check-failed') {
                            showToast('下载失败：文件路径不在允许范围内');
                        } else if (reason === 'file-not-found') {
                            showToast('下载失败：ZIP 文件不存在');
                        } else if (reason === 'copy-error') {
                            showToast('下载失败：无法复制文件');
                        } else {
                            showToast(`下载失败 (${reason})`);
                        }
                    }
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
                    const result = await window.electronAPI.saveFileByPath(data.path, filename);
                    if (result && result.success) {
                        showToast('下载成功');
                    } else {
                        const reason = result?.reason || 'unknown';
                        if (reason === 'user-cancelled') {
                            showToast('已取消');
                        } else if (reason === 'path-check-failed') {
                            showToast('下载失败：文件路径不在允许范围内');
                        } else if (reason === 'file-not-found') {
                            showToast('下载失败：源文件不存在');
                        } else if (reason === 'copy-error') {
                            showToast('下载失败：无法复制文件');
                        } else {
                            showToast(`下载失败 (${reason})`);
                        }
                    }
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
}

async function requestRestart() {
    if (isRestarting) return;
    if (currentSession) {
        let message = '更换文档会结束当前未完成任务，确定继续吗？';
        if (isGenerating) {
            message = '当前音频仍在生成。新建任务会中止处理并清理本次结果，确定继续吗？';
        } else if (generatedFiles.length > 0 || currentStep === 4) {
            message = latestCurrentResultEvent?.history_id
                ? '本次结果已保存到历史记录。确定开始新任务吗？'
                : '本次结果未能保存到历史记录。新建任务会清理当前结果，请先确认已完成下载。确定继续吗？';
        }
        if (!window.confirm(message)) return;
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

    // 重置 Step 1
    const uploadZone = $('upload-zone');
    uploadZone.classList.remove('has-file', 'has-error', 'is-processing', 'dragover');
    uploadZone.setAttribute('aria-busy', 'false');
    uploadZone.querySelector('.upload-text-large').textContent = '拖拽文档到这里，或点击选择';
    uploadZone.querySelector('.upload-hint').textContent = '支持 .docx 文件 · 选择后会自动解析';
    setUploadFeedback();
    updateSessionLabels();

    // 刷新预设列表（可能在上一次操作中保存了新配置）
    refreshPresetUI();

    // 重置 Step 3
    $('progress-bar').style.width = '0%';
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', '0');
    $('progress-stats').textContent = '准备中...';
    $('progress-percent').textContent = '0';
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
// 启动
// ============================================================================

init();
