/**
 * Word → TTS — Frontend Logic v2
 * =================================
 * 四步向导式流程：上传 → 配置 → 生成 → 结果
 */

// ============================================================================
// 常量 & 全局状态
// ============================================================================

const API_BASE = 'http://127.0.0.1:7863';
const isElectron = typeof window.electronAPI !== 'undefined';
const platform = isElectron ? window.electronAPI.platform : 'web';

let currentStep = 1;
let currentSession = null;       // { session_id, source_filename, file_path }
let currentConfig = null;        // API 返回的配置
let eventSource = null;          // SSE 连接
let isGenerating = false;
let generatedFiles = [];         // 生成完成的文件列表
let logEntryCount = 0;
let lastStats = null;             // 最近一次 stats 事件数据
let lastDownloadEvent = null;     // 最近一次 download 事件数据
let sseRetryCount = 0;            // SSE 重连次数计数
const SSE_MAX_RETRIES = 5;        // SSE 最大重连次数
let generationResult = null;      // 'done' | 'error' | null — 跟踪生成结果状态
let wavesurferInstances = [];    // 所有已创建的 WaveSurfer 实例（用于清理和单播放控制）
let currentPlayingWs = null;     // 当前正在播放的 WaveSurfer 实例

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

        titleEl.textContent = title;
        messageEl.textContent = message;
        input.value = defaultValue;

        overlay.classList.add('active');
        // 延迟聚焦以确保 transition 完成
        setTimeout(() => { input.focus(); input.select(); }, 50);

        let resolved = false;
        const done = (value) => {
            if (resolved) return;
            resolved = true;
            overlay.classList.remove('active');
            // 清理事件监听器（一次性）
            okBtn.removeEventListener('click', onOk);
            cancelBtn.removeEventListener('click', onCancel);
            input.removeEventListener('keydown', onKeydown);
            resolve(value);
        };

        const onOk = () => done(input.value);
        const onCancel = () => done(null);
        const onKeydown = (e) => {
            if (e.key === 'Enter') { e.preventDefault(); done(input.value); }
            else if (e.key === 'Escape') { e.preventDefault(); done(null); }
        };

        okBtn.addEventListener('click', onOk);
        cancelBtn.addEventListener('click', onCancel);
        input.addEventListener('keydown', onKeydown);
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
    select.innerHTML = '<option value="">-- 选择配置 --</option>';
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
    if (!currentSession || isGenerating) return;

    isGenerating = true;
    generatedFiles = [];
    logEntryCount = 0;
    lastStats = null;
    lastDownloadEvent = null;
    sseRetryCount = 0;
    generationResult = null;
    updateSessionLabels(currentSession.source_filename);

    // 重置生成页面 UI
    $('progress-bar').style.width = '0%';
    $('progress-bar').parentElement?.setAttribute('aria-valuenow', '0');
    $('progress-stats').textContent = '准备中...';
    $('gen-title').textContent = '正在生成音频...';
    $('gen-animation').classList.remove('done');
    $('progress-log').innerHTML = '<div class="log-empty">等待开始...</div>';
    $('log-count').textContent = '0 条';
    $('type-stats').innerHTML = '';

    const config = presetConfig || collectConfig(useDefaults);

    try {
        const resp = await fetch(`${API_BASE}/api/generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: currentSession.session_id,
                source_filename: currentSession.source_filename,
                file_path: currentSession.file_path,
                config: config,
            }),
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

        connectSSE(currentSession.session_id);
        $('status-text').textContent = '生成中...';

    } catch (err) {
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

function updateSessionLabels(filename = '') {
    const displayName = filename || '已导入的 Word 文档';
    const sourceName = $('source-file-name');
    const generationName = $('generation-file-name');
    if (sourceName) sourceName.textContent = displayName;
    if (generationName) {
        generationName.textContent = filename
            ? `正在处理「${displayName}」，请保持应用开启。`
            : 'WordTTS 正在准备当前文档，请保持应用开启。';
    }
}

function setUploadParsing(parsing) {
    const uploadZone = $('upload-zone');
    if (!uploadZone) return;
    uploadZone.classList.toggle('is-processing', parsing);
    uploadZone.setAttribute('aria-busy', parsing ? 'true' : 'false');
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

    // 等待服务器就绪
    if (isElectron) {
        setServiceState('', '正在连接服务');
        showToast('正在启动后端服务...');
        const ready = await window.electronAPI.serverReady();
        if (!ready) {
            setServiceState('error', '服务连接失败');
            showToast('后端服务启动失败，请检查 Python 环境');
            return;
        }
    }

    const configLoaded = await loadConfig();
    setServiceState(configLoaded ? 'ready' : 'warning', configLoaded ? '服务已连接' : '服务状态异常');
    updateStepper();
    updateConfigSummary();
    showToast('就绪');
}

function bindEvents() {
    // 重新开始按钮（工具栏）
    $('restart-btn').addEventListener('click', requestRestart);

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
        uploadZone.classList.add('dragover');
    });
    uploadZone.addEventListener('dragleave', () => {
        uploadZone.classList.remove('dragover');
    });
    uploadZone.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadZone.classList.remove('dragover');
        const file = e.dataTransfer.files[0];
        if (file && file.name.endsWith('.docx')) {
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
    $('change-file-btn').addEventListener('click', () => restart());
    $('back-to-upload-btn').addEventListener('click', () => restart());
    $('skip-config-btn').addEventListener('click', () => {
        goToStep(3);
        startProcessing(true);
    });
    $('start-generate-btn').addEventListener('click', () => {
        goToStep(3);
        startProcessing(false);
    });

    // Step 4: 下载
    $('download-zip-btn').addEventListener('click', downloadZip);
    $('new-file-btn').addEventListener('click', () => restart());
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
            const resp = await fetch(`${API_BASE}/api/config`);
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
    if (file.path) {
        handleFilePath(file.path);
    } else {
        uploadFile(file);
    }
}

let isParsing = false;  // 防止解析重入

async function handleFilePath(filePath) {
    if (isParsing) return;  // 防止重入
    isParsing = true;

    const filename = filePath.split(/[\\/]/).pop();
    const uploadZone = $('upload-zone');
    uploadZone.classList.add('has-file');
    uploadZone.querySelector('.upload-text-large').textContent = filename;
    uploadZone.querySelector('.upload-hint').textContent = '点击重新选择文件';
    $('status-text').textContent = `正在解析: ${filename}`;

    try {
        const resp = await fetch(`${API_BASE}/api/parse`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_path: filePath }),
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
        currentSession = {
            session_id: data.session_id,
            source_filename: data.source_filename,
            file_path: data.file_path,
        };

        $('status-text').textContent = `解析成功 — ${data.source_filename}`;
        showToast('文档解析成功，进入配置步骤');

        // 自动进入 Step 2
        setTimeout(() => goToStep(2), 600);

    } catch (err) {
        console.error('解析失败:', err);
        showToast(`解析失败: ${err.message}`);
        uploadZone.classList.remove('has-file');
        uploadZone.querySelector('.upload-text-large').textContent = '点击选择或拖拽 .docx 文件到此处';
        uploadZone.querySelector('.upload-hint').textContent = '支持 .docx 格式';
    } finally {
        isParsing = false;
    }
}

async function uploadFile(file) {
    if (isParsing) return;
    isParsing = true;

    const formData = new FormData();
    formData.append('file', file);
    const uploadZone = $('upload-zone');

    // 立即更新上传区域，给用户即时反馈
    uploadZone.classList.add('has-file');
    uploadZone.querySelector('.upload-text-large').textContent = file.name;
    uploadZone.querySelector('.upload-hint').textContent = '正在上传...';
    $('status-text').textContent = `正在上传: ${file.name}`;

    try {
        const resp = await fetch(`${API_BASE}/api/parse/upload`, {
            method: 'POST',
            body: formData,
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
        currentSession = {
            session_id: data.session_id,
            source_filename: data.source_filename,
            file_path: data.file_path,
        };
        showToast('文档解析成功');
        setTimeout(() => goToStep(2), 600);
    } catch (err) {
        showToast(`上传失败: ${err.message}`);
        // 重置上传区域
        uploadZone.classList.remove('has-file');
        uploadZone.querySelector('.upload-text-large').textContent = '点击选择或拖拽 .docx 文件到此处';
        uploadZone.querySelector('.upload-hint').textContent = '支持 .docx 格式';
    } finally {
        isParsing = false;
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
    if (eventSource) {
        eventSource.close();
    }

    let sseClosed = false;

    eventSource = new EventSource(`${API_BASE}/api/progress/${sessionId}`);

    // 连接成功时重置重试计数
    eventSource.onopen = () => {
        sseRetryCount = 0;
    };

    eventSource.onmessage = (e) => {
        try {
            const data = JSON.parse(e.data);
            handleSSEEvent(data);
        } catch (err) {
            console.error('SSE 解析错误:', err);
        }
    };

    eventSource.onerror = () => {
        console.error('SSE 连接错误');
        if (!sseClosed && isGenerating) {
            sseClosed = true;
            eventSource.close();

            // 超过最大重试次数，判定后端不可用
            sseRetryCount++;
            if (sseRetryCount > SSE_MAX_RETRIES) {
                isGenerating = false;
                generationResult = 'error';
                $('gen-title').textContent = '连接中断';
                $('status-text').textContent = '与服务器连接中断，请检查后端服务';
                showToast('与服务器连接中断，请重试');
                return;
            }

            // 指数退避重连
            const delay = Math.min(2000 * Math.pow(1.5, sseRetryCount - 1), 10000);
            setTimeout(() => {
                if (isGenerating && currentSession) {
                    logEntryCount = 0;
                    $('progress-log').innerHTML = `<div class="log-empty">重新连接中... (${sseRetryCount}/${SSE_MAX_RETRIES})</div>`;
                    $('log-count').textContent = '0 条';
                    connectSSE(currentSession.session_id);
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
            $('gen-title').textContent = '生成出错';
            $('status-text').textContent = `错误: ${event.msg}`;
            break;

        case 'end':
            resetGenerateState();
            if (eventSource) {
                eventSource.close();
                eventSource = null;
            }
            // 如果未收到 done 或 error 事件，说明生成异常终止
            if (generationResult === null) {
                $('gen-title').textContent = '生成已停止';
                $('status-text').textContent = '生成已停止，请检查日志或重新开始';
            }
            break;

        case 'heartbeat':
            break;
    }
}

// ============================================================================
// 日志渲染
// ============================================================================

function addLogEntry(entry) {
    const logBody = $('progress-log');
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
    logBody.scrollTop = logBody.scrollHeight;

    logEntryCount++;
    $('log-count').textContent = `${logEntryCount} 条`;
}

// ============================================================================
// 进度 & 统计
// ============================================================================

function updateProgress(event) {
    const pct = event.total > 0 ? (event.completed / event.total) * 100 : 0;
    $('progress-bar').style.width = `${pct.toFixed(1)}%`;
    $('progress-stats').textContent = `${event.completed} / ${event.total}` + (event.failed > 0 ? `  ·  失败 ${event.failed}` : '');
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
    totalLabel.textContent = '共 ';
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

    // 更新生成页面状态
    $('gen-title').textContent = '生成完成！';
    $('gen-animation').classList.add('done');
    $('progress-bar').style.width = '100%';

    // 合并最后的统计数据（done 事件本身不携带 completed/failed）
    const doneData = {
        ...event,
        completed: lastStats ? lastStats.completed : (event.completed || 0),
        failed: lastStats ? lastStats.failed : (event.failed || 0),
        total: lastStats ? lastStats.total : (event.total || 0),
    };

    // 构建结果页面
    buildResultPage(doneData);

    // 自动跳转到结果页（延迟 1 秒让用户看到完成动画）
    setTimeout(() => {
        // 仅在当前仍在生成页（step 3）时才跳转，避免 restart 后误跳
        if (currentStep === 3) {
            goToStep(4);
        }
    }, 1000);

    showToast('处理完成');
}

function buildResultPage(event) {
    const total = lastStats ? lastStats.total : generatedFiles.length;
    const failed = event.failed || 0;
    const success = event.completed || generatedFiles.length;

    // 摘要
    let summaryText = `成功生成 ${success} 个音频文件`;
    if (failed > 0) {
        summaryText += `，失败 ${failed} 个`;
    }
    $('result-summary').textContent = summaryText;

    // ZIP 卡片
    const zipCard = $('zip-card');
    if (event.zip_path) {
        zipCard.style.display = 'flex';
        $('zip-desc').textContent = `包含全部 ${total} 个音频文件`;
    } else {
        zipCard.style.display = 'none';
    }

    // 音频列表
    const audioList = $('audio-list');
    audioList.innerHTML = '';

    if (total === 0) {
        audioList.innerHTML = '<div class="audio-empty">暂无音频文件</div>';
        $('audio-count').textContent = '0 个文件';
        return;
    }

    $('audio-count').textContent = `${total} 个文件`;

    generatedFiles.forEach((f) => {
        const color = (currentConfig && currentConfig.type_colors && currentConfig.type_colors[f.doc_type]) || '#a8a29e';
        const audioUrl = `${API_BASE}/api/download/file/${currentSession.session_id}/${encodeURIComponent(f.filename)}`;

        // 使用 DOM API 安全构建，避免 innerHTML 注入风险
        const item = document.createElement('div');
        item.className = 'audio-item';

        // --- 头部：颜色点 + 文件名 + 元信息 + 下载按钮 ---
        const header = document.createElement('div');
        header.className = 'audio-item-header';

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
        meta.textContent = `${f.doc_type} · ${f.category}`;

        info.appendChild(name);
        info.appendChild(meta);

        const dlBtn = document.createElement('button');
        dlBtn.className = 'audio-download-btn';
        dlBtn.title = '下载此文件';
        dlBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`;
        dlBtn.addEventListener('click', () => downloadFile(f.filename));

        header.appendChild(dot);
        header.appendChild(info);
        header.appendChild(dlBtn);
        item.appendChild(header);

        // --- 波形图 + 播放按钮 (使用 wavesurfer.js) ---
        const waveformWrap = document.createElement('div');
        waveformWrap.className = 'waveform-wrap';

        const playBtn = document.createElement('button');
        playBtn.className = 'waveform-play-btn';
        playBtn.title = '播放/暂停';
        playBtn.setAttribute('aria-label', '播放');
        playBtn.innerHTML = '<svg class="icon-play" width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg><svg class="icon-pause" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="display:none"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
        waveformWrap.appendChild(playBtn);

        const canvasWrap = document.createElement('div');
        canvasWrap.className = 'waveform-canvas-wrap';

        const wsContainer = document.createElement('div');
        wsContainer.className = 'waveform-container';
        canvasWrap.appendChild(wsContainer);

        const timeLabel = document.createElement('span');
        timeLabel.className = 'waveform-time';
        timeLabel.textContent = '00:00 / 00:00';
        canvasWrap.appendChild(timeLabel);

        waveformWrap.appendChild(canvasWrap);
        item.appendChild(waveformWrap);

        // --- 原文展示 ---
        const textSection = document.createElement('div');
        textSection.className = 'audio-text-section';

        const textHeader = document.createElement('div');
        textHeader.className = 'audio-text-header';
        textHeader.textContent = '原文';
        textSection.appendChild(textHeader);

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

        // --- 使用 wavesurfer.js 初始化波形 ---
        createWaveSurfer(wsContainer, audioUrl, color, playBtn, timeLabel);
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
    playBtn.setAttribute('aria-label', isPlaying ? '暂停' : '播放');
}

/**
 * 使用 wavesurfer.js 创建波形图实例。
 * @param {HTMLElement} container - 波形挂载容器
 * @param {string} url - 音频文件 URL
 * @param {string} color - 波形颜色
 * @param {HTMLButtonElement} playBtn - 播放/暂停按钮
 * @param {HTMLElement} timeLabel - 时间显示元素
 */
function createWaveSurfer(container, url, color, playBtn, timeLabel) {
    // 检查 WaveSurfer 是否可用
    if (typeof WaveSurfer === 'undefined') {
        console.error('WaveSurfer 库未加载');
        container.textContent = '波形不可用';
        container.style.cssText = 'display:flex;align-items:center;justify-content:center;height:48px;color:var(--text-muted);font-size:12px;';
        return;
    }

    const ws = WaveSurfer.create({
        container: container,
        url: url,
        height: 48,
        waveColor: colorWithAlpha(color, 0.25),
        progressColor: color,
        cursorColor: 'rgba(0, 0, 0, 0.15)',
        cursorWidth: 1,
        barWidth: 2,
        barGap: 1,
        barRadius: 2,
        normalize: true,
        interact: true,
    });

    wavesurferInstances.push(ws);

    // 播放按钮点击
    playBtn.addEventListener('click', () => {
        if (currentPlayingWs && currentPlayingWs !== ws) {
            currentPlayingWs.pause();
        }
        ws.playPause();
    });

    // 波形加载完成
    ws.on('ready', () => {
        const duration = ws.getDuration();
        timeLabel.textContent = `00:00 / ${formatTime(duration)}`;
    });

    // 播放开始
    ws.on('play', () => {
        currentPlayingWs = ws;
        updatePlayIcon(playBtn, true);
    });

    // 暂停
    ws.on('pause', () => {
        if (currentPlayingWs === ws) currentPlayingWs = null;
        updatePlayIcon(playBtn, false);
    });

    // 播放结束
    ws.on('finish', () => {
        currentPlayingWs = null;
        updatePlayIcon(playBtn, false);
        timeLabel.textContent = `00:00 / ${formatTime(ws.getDuration())}`;
    });

    // 播放进度更新
    ws.on('audioprocess', () => {
        const current = ws.getCurrentTime();
        const duration = ws.getDuration();
        timeLabel.textContent = `${formatTime(current)} / ${formatTime(duration)}`;
    });

    // seek 后更新时间
    ws.on('seeking', () => {
        const current = ws.getCurrentTime();
        const duration = ws.getDuration();
        timeLabel.textContent = `${formatTime(current)} / ${formatTime(duration)}`;
    });

    // 错误处理
    ws.on('error', (err) => {
        console.error('WaveSurfer 错误:', err);
        container.textContent = '波形加载失败';
        container.style.cssText = 'display:flex;align-items:center;justify-content:center;height:48px;color:var(--text-muted);font-size:12px;';
        timeLabel.textContent = '';
    });
}

// ============================================================================
// 下载
// ============================================================================

async function downloadZip() {
    if (!currentSession) return;

    if (isElectron) {
        try {
            const resp = await fetch(`${API_BASE}/api/file-path?session_id=${currentSession.session_id}&filename=output.zip`);
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
        window.open(`${API_BASE}/api/download/zip/${currentSession.session_id}`, '_blank');
    }
}

async function downloadFile(filename) {
    if (!currentSession) return;

    if (isElectron) {
        try {
            const resp = await fetch(`${API_BASE}/api/file-path?session_id=${currentSession.session_id}&filename=${encodeURIComponent(filename)}`);
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
        window.open(`${API_BASE}/api/download/file/${currentSession.session_id}/${encodeURIComponent(filename)}`, '_blank');
    }
}

// ============================================================================
// 重新开始
// ============================================================================

function resetGenerateState() {
    isGenerating = false;
}

async function restart() {
    // 销毁所有 WaveSurfer 实例并停止播放
    wavesurferInstances.forEach(ws => {
        try { ws.destroy(); } catch (e) { /* ignore */ }
    });
    wavesurferInstances = [];
    currentPlayingWs = null;

    // 断开 SSE
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }

    // 如果有会话，通知后端清理
    if (currentSession) {
        try {
            await fetch(`${API_BASE}/api/cleanup/${currentSession.session_id}`, {
                method: 'POST',
            });
        } catch (e) {
            // 忽略清理错误
        }
    }

    // 重置状态
    currentSession = null;
    isGenerating = false;
    generatedFiles = [];
    logEntryCount = 0;
    lastStats = null;
    lastDownloadEvent = null;
    sseRetryCount = 0;
    generationResult = null;

    // 重置 Step 1
    const uploadZone = $('upload-zone');
    uploadZone.classList.remove('has-file');
    uploadZone.querySelector('.upload-text-large').textContent = '点击选择或拖拽 .docx 文件到此处';
    uploadZone.querySelector('.upload-hint').textContent = '支持 .docx 格式';

    // 刷新预设列表（可能在上一次操作中保存了新配置）
    refreshPresetUI();

    // 重置 Step 3
    $('progress-bar').style.width = '0%';
    $('progress-stats').textContent = '准备中...';
    $('gen-title').textContent = '正在生成音频...';
    $('gen-animation').classList.remove('done');
    $('progress-log').innerHTML = '<div class="log-empty">等待开始...</div>';
    $('log-count').textContent = '0 条';
    $('type-stats').innerHTML = '';

    // 重置 Step 4
    $('audio-list').innerHTML = '<div class="audio-empty">暂无音频文件</div>';
    $('audio-count').textContent = '0 个文件';
    $('result-summary').textContent = '';

    // 重置状态栏
    $('status-text').textContent = '就绪';
    $('stats-bar').innerHTML = '';

    // 回到首页
    goToStep(1);
    showToast('已重置，可以开始新任务');
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
