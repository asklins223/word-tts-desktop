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
        overlay.classList.add('active');
        // 延迟聚焦以确保 transition 完成
        setTimeout(() => { input.focus(); input.select(); }, 50);

        let resolved = false;
        const done = (value) => {
            if (resolved) return;
            resolved = true;
            overlay.classList.remove('active');
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
    if ($('format').value !== 'wav' && $('quality').value.startsWith('无损')) {
        setSelectValue($('quality'), '128 kbps（标准）', '128 kbps（标准）');
    }
    updateConfigSummary();
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
    $('progress-log').innerHTML = '<div class="log-empty">等待任务开始...</div>';
    $('log-count').textContent = '0 条';
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
        $('gen-title').textContent = '任务未能启动';
        $('status-text').textContent = `启动失败: ${err.message}`;
        setGenerationVisualState('error');
        showGenerationRecovery(`启动失败：${err.message}`);
        showToast(`启动失败: ${err.message}`);
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
    2: '02 / 配置声音',
    3: '03 / 生成音频',
    4: '04 / 交付结果',
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
    if (sourceName) sourceName.textContent = displayName;
    if (sourceMeta) sourceMeta.textContent = filename
        ? (total > 0 ? `已识别 ${total} 条 · ${types.length} 种题型` : '文档解析完成')
        : '当前文档';
    if (summaryDocument) summaryDocument.textContent = total > 0
        ? `${total} 条 · ${types.length} 种题型`
        : '等待解析';
    if (generateButtonLabel) generateButtonLabel.textContent = total > 0
        ? `开始生成 ${total} 条音频`
        : '开始生成音频';
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
    if (currentStep !== 4 || waveformItems.length === 0) return;
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
    if (connected) showToast('就绪');
}

function bindEvents() {
    // 重新开始按钮（工具栏）
    $('restart-btn').addEventListener('click', requestRestart);
    $('retry-service-btn').addEventListener('click', () => connectService(true));

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
            showToast('请选择 .docx 格式的 Word 文档');
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
        goToStep(3);
        startProcessing(true);
    });
    $('start-generate-btn').addEventListener('click', () => {
        goToStep(3);
        startProcessing(false);
    });

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
                    maleNameEl.textContent = 'TTSMaker 788 Alfie';
                    maleDescEl.textContent = 'm/M 标识 → 男声 · 通过 TTSMaker 网站生成';
                } else {
                    maleNameEl.textContent = 'RemyMultilingual (edge-tts)';
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
    currentStep = step;

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
        } else if (step === currentStep) {
            el.classList.add('active');
            el.setAttribute('aria-current', 'step');
        }
    });

    $$('.step-line').forEach(el => {
        const line = parseInt(el.dataset.line);
        el.classList.toggle('active', line < currentStep);
    });

    const toolbarStep = $('toolbar-step');
    if (toolbarStep) toolbarStep.textContent = STEP_TITLES[currentStep] || '';
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
        $('status-text').textContent = `解析成功 — ${data.source_filename}`;
        showToast('文档解析成功，进入配置步骤');

        // 解析完成后进入配置步骤
        goToStep(2);

    } catch (err) {
        if (err.name === 'AbortError' || attemptId !== parseAttemptId) return;
        console.error('解析失败:', err);
        showToast(`解析失败: ${err.message}`);
        uploadZone.classList.remove('has-file');
        uploadZone.querySelector('.upload-text-large').textContent = '拖拽文档到这里，或点击选择';
        uploadZone.querySelector('.upload-hint').textContent = '支持 .docx 文件 · 选择后会自动解析';
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
        $('status-text').textContent = `解析成功 — ${data.source_filename || file.name}`;
        showToast('文档解析成功，进入配置步骤');
        goToStep(2);
    } catch (err) {
        if (err.name === 'AbortError' || attemptId !== parseAttemptId) return;
        showToast(`上传失败: ${err.message}`);
        // 重置上传区域
        uploadZone.classList.remove('has-file');
        uploadZone.querySelector('.upload-text-large').textContent = '拖拽文档到这里，或点击选择';
        uploadZone.querySelector('.upload-hint').textContent = '支持 .docx 文件 · 选择后会自动解析';
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
            if (sseRetryCount > SSE_MAX_RETRIES) {
                isGenerating = false;
                generationResult = 'error';
                $('gen-title').textContent = '连接中断';
                $('status-text').textContent = '与服务器连接中断，请检查后端服务';
                setGenerationVisualState('error');
                showGenerationRecovery('与生成服务的连接已中断。请确认服务正常后重试，或返回配置页。');
                showToast('与服务器连接中断，请重试');
                return;
            }

            // 指数退避重连
            const delay = Math.min(2000 * Math.pow(1.5, sseRetryCount - 1), 10000);
            sseReconnectTimer = setTimeout(() => {
                sseReconnectTimer = null;
                if (connectionToken === sseConnectionToken && isGenerating && currentSession?.session_id === sessionId) {
                    logEntryCount = 0;
                    $('progress-log').innerHTML = `<div class="log-empty">重新连接中... (${sseRetryCount}/${SSE_MAX_RETRIES})</div>`;
                    $('log-count').textContent = '0 条';
                    connectSSE(sessionId);
                }
            }, delay);
        }
    };
}

function handleSSEEvent(event) {
    switch (event.type) {
        case 'log_init':
            event.entries.forEach(entry => addLogEntry(entry));
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

        case 'error':
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
                $('gen-title').textContent = '生成已停止';
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

function addLogEntry(entry) {
    const logBody = $('progress-log');
    const shouldFollowTail = logBody.scrollHeight - logBody.scrollTop - logBody.clientHeight < 36;
    const empty = logBody.querySelector('.log-empty');
    if (empty) empty.remove();

    const iconMap = {
        success: '✓',
        error: '✗',
        warn: '⚠',
        progress: '⟳',
        info: '•',
    };

    const div = document.createElement('div');
    div.className = 'log-entry';

    const timeSpan = document.createElement('span');
    timeSpan.className = 'log-time';
    timeSpan.textContent = entry.time;

    const iconSpan = document.createElement('span');
    // 只允许已知 level 作为 class，防止注入
    const safeLevel = ['success', 'error', 'warn', 'progress', 'info'].includes(entry.level) ? entry.level : 'info';
    iconSpan.className = `log-icon ${safeLevel}`;
    iconSpan.textContent = iconMap[entry.level] || '•';

    const msgSpan = document.createElement('span');
    msgSpan.className = 'log-msg';
    msgSpan.textContent = entry.msg;

    div.appendChild(timeSpan);
    div.appendChild(iconSpan);
    div.appendChild(msgSpan);

    logBody.appendChild(div);
    if (shouldFollowTail) logBody.scrollTop = logBody.scrollHeight;

    logEntryCount++;
    $('log-count').textContent = `${logEntryCount} 条`;
}

// ============================================================================
// 进度 & 统计
// ============================================================================

function updateProgress(event) {
    const processed = Math.min((event.completed || 0) + (event.failed || 0), event.total || 0);
    const pct = event.total > 0 ? (processed / event.total) * 100 : 0;
    $('progress-bar').style.width = `${pct.toFixed(1)}%`;
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', pct.toFixed(1));
    $('progress-stats').textContent = `${event.completed} / ${event.total}` + (event.failed > 0 ? `  ·  失败 ${event.failed}` : '');
    $('progress-percent').textContent = String(Math.round(pct));
    $('progress-completed').textContent = String(event.completed || 0);
    $('progress-remaining').textContent = String(Math.max((event.total || 0) - processed, 0));
    $('progress-failed').textContent = String(event.failed || 0);
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

    // 短暂停留展示完成态，再进入交付中心。
    const completedAttemptId = generationAttemptId;
    const completedSessionId = currentSession?.session_id;
    resultNavigationTimer = setTimeout(() => {
        resultNavigationTimer = null;
        // 仅在当前仍在生成页（step 3）时才跳转，避免 restart 后误跳
        if (currentStep === 3 && completedAttemptId === generationAttemptId && currentSession?.session_id === completedSessionId) {
            goToStep(4);
        }
    }, 950);

    showToast(doneData.failed > 0 ? `任务结束，${doneData.failed} 条生成失败` : '处理完成');
}

function buildResultPage(event) {
    destroyWaveSurfers();
    const failed = event.failed || 0;
    const success = generatedFiles.length || event.completed || 0;
    const resultTitle = $('result-title');
    const resultEyebrow = $('result-eyebrow');
    const resultIcon = document.querySelector('.result-success-icon');
    const generateFullBtn = $('generate-full-btn');
    const resultWarning = $('result-warning');
    const resultWarningText = $('result-warning-text');
    const failureList = $('result-failure-list');
    const retryFailedBtn = $('retry-failed-btn');
    const sourceTotal = summarizeParseResults(currentSession?.parse_results).total;
    const isPreviewResult = Boolean(lastGenerationConfig?.preview && sourceTotal > 3);
    const failedItems = Array.isArray(event.failed_items) ? event.failed_items : [];

    if (generateFullBtn) {
        generateFullBtn.hidden = !lastGenerationConfig?.preview || sourceTotal <= 3 || success === 0;
    }
    if (resultWarning) resultWarning.hidden = failed === 0;
    if (resultWarningText && failed > 0) {
        resultWarningText.textContent = success > 0
            ? `有 ${failed} 条内容未能生成。沿用当前设置只重试失败项；修改参数后会重新生成全部内容。`
            : `本次共有 ${failed} 条内容生成失败。你可以沿用当前设置重试，或返回配置检查网络与声音设置。`;
    }
    if (retryFailedBtn) retryFailedBtn.hidden = failed === 0 || !lastGenerationConfig;
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
            remaining.textContent = `另有 ${failed - displayedItems.length} 条失败内容未展开，重试时会自动包含。`;
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
    let summaryText = isPreviewResult
        ? `本次试听生成 ${success} 个音频${failed > 0 ? `，失败 ${failed} 个` : ''}；确认效果后可继续生成完整文档`
        : `成功生成 ${success} 个音频文件${failed > 0 ? `，失败 ${failed} 个` : ''}`;
    $('result-summary').textContent = summaryText;
    $('result-success-label').textContent = isPreviewResult ? '试听文件' : '已交付';
    $('result-success-count').textContent = String(success);
    $('result-success-caption').textContent = isPreviewResult
        ? `本次范围：前 ${Math.min(sourceTotal, 3)} 条`
        : '音频文件';
    $('result-secondary-label').textContent = isPreviewResult ? '文档总量' : '未完成';
    $('result-failed-count').textContent = String(isPreviewResult ? sourceTotal : failed);
    $('result-secondary-caption').textContent = isPreviewResult ? '完整文档内容' : '待处理内容';
    $('result-format-value').textContent = String(lastGenerationConfig?.format || currentConfig?.format || 'MP3').toUpperCase();

    // ZIP 卡片
    const zipCard = $('zip-card');
    const resultHero = $('result-hero');
    if (event.zip_path) {
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

    if (generatedFiles.length === 0) {
        audioList.innerHTML = '<div class="audio-empty">暂无音频文件</div>';
        $('audio-count').textContent = '0 个文件';
        if (audioListSection) audioListSection.hidden = true;
        return;
    }

    if (audioListSection) audioListSection.hidden = false;
    $('audio-count').textContent = `${generatedFiles.length} 个文件`;
    const renderToken = waveformRenderToken;

    generatedFiles.forEach((f, index) => {
        const color = (currentConfig && currentConfig.type_colors && currentConfig.type_colors[f.doc_type]) || '#a8a29e';
        const audioUrl = apiUrl(`/api/download/file/${currentSession.session_id}/${encodeURIComponent(f.filename)}`);

        // 使用 DOM API 安全构建，避免 innerHTML 注入风险
        const item = document.createElement('article');
        item.className = 'audio-item';
        item.setAttribute('aria-label', f.filename);
        item.style.setProperty('--item-index', String(Math.min(index, 5)));

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
                await downloadFile(f.filename);
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

async function downloadZip() {
    if (!currentSession) return;

    if (isElectron) {
        try {
            const resp = await fetch(apiUrl(`/api/file-path?session_id=${currentSession.session_id}&filename=output.zip`));
            if (resp.ok) {
                const data = await resp.json();
                if (data.path) {
                    // 使用源文件名作为 ZIP 下载文件名
                    const sourceName = currentSession.source_filename.replace(/\.docx$/i, '');
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
                showToast('下载失败：文件不存在或会话已过期');
            }
        } catch (err) {
            console.error('下载异常:', err);
            showToast('下载失败');
        }
    } else {
        window.open(apiUrl(`/api/download/zip/${currentSession.session_id}`), '_blank');
    }
}

async function downloadFile(filename) {
    if (!currentSession) return;

    if (isElectron) {
        try {
            const resp = await fetch(apiUrl(`/api/file-path?session_id=${currentSession.session_id}&filename=${encodeURIComponent(filename)}`));
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
                showToast('下载失败：文件不存在或会话已过期');
            }
        } catch (err) {
            console.error('下载异常:', err);
            showToast('下载失败');
        }
    } else {
        window.open(apiUrl(`/api/download/file/${currentSession.session_id}/${encodeURIComponent(filename)}`), '_blank');
    }
}

// ============================================================================
// 重新开始
// ============================================================================

function resetGenerateState() {
    isGenerating = false;
}

async function requestRestart() {
    if (isRestarting) return;
    if (currentSession) {
        let message = '更换文档会结束当前任务并清空任务记录，确定继续吗？';
        if (isGenerating) {
            message = '当前音频仍在生成。新建任务会中止处理并清理本次结果，确定继续吗？';
        } else if (generatedFiles.length > 0 || currentStep === 4) {
            message = '新建任务会清理当前任务的生成结果，请先确认已完成下载。确定继续吗？';
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
    logEntryCount = 0;
    lastStats = null;
    lastDownloadEvent = null;
    sseRetryCount = 0;
    generationResult = null;
    lastGenerationConfig = null;

    // 重置 Step 1
    const uploadZone = $('upload-zone');
    uploadZone.classList.remove('has-file', 'is-processing', 'dragover');
    uploadZone.setAttribute('aria-busy', 'false');
    uploadZone.querySelector('.upload-text-large').textContent = '拖拽文档到这里，或点击选择';
    uploadZone.querySelector('.upload-hint').textContent = '支持 .docx 文件 · 选择后会自动解析';
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
    $('progress-log').innerHTML = '<div class="log-empty">等待任务开始...</div>';
    $('log-count').textContent = '0 条';
    $('type-stats').innerHTML = '';

    // 重置 Step 4
    $('audio-list').innerHTML = '<div class="audio-empty">暂无音频文件</div>';
    $('audio-count').textContent = '0 个文件';
    document.querySelector('.audio-list-section').hidden = false;
    $('result-summary').textContent = '';
    $('result-success-label').textContent = '已交付';
    $('result-success-count').textContent = '0';
    $('result-success-caption').textContent = '音频文件';
    $('result-secondary-label').textContent = '未完成';
    $('result-failed-count').textContent = '0';
    $('result-secondary-caption').textContent = '待处理内容';
    $('result-format-value').textContent = 'MP3';
    $('result-hero').classList.remove('has-no-package');
    $('generate-full-btn').hidden = true;
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
    showToast(cleanupConfirmed
        ? '已重置，可以开始新任务'
        : '当前任务已关闭，请重新连接生成服务后继续');
}

// ============================================================================
// Toast
// ============================================================================

let toastTimer = null;
function showToast(msg) {
    let toast = document.querySelector('.toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.className = 'toast';
        toast.setAttribute('role', 'status');
        toast.setAttribute('aria-live', 'polite');
        document.body.appendChild(toast);
    }
    toast.textContent = msg;
    toast.classList.add('show');

    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        toast.classList.remove('show');
    }, 2500);
}

// ============================================================================
// 启动
// ============================================================================

init();
