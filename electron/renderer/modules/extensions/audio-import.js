/** Renderer module: extensions.audioImport */
(function attachRendererFeature_extensions_audioImport(root) {
    'use strict';

let audioAsrPollToken = 0;
let activeAudioImport = null;
let audioImportQuestionPicker = null;
let activeAudioImportPreview = null;
let audioImportPreviewToken = 0;
let audioImportAnalysisProgressToken = 0;
let audioImportAnalysisProgressTimer = null;
const AUDIO_IMPORT_PREVIEW_MAX_BYTES = 64 * 1024 * 1024;
const AUDIO_IMPORT_PROGRESS_POLL_MS = 450;
const AUDIO_IMPORT_EXTENSIONS = new Set(['.mp3', '.wav', '.m4a', '.aac', '.ogg', '.opus', '.flac', '.zip']);
const AUDIO_IMPORT_FILENAME_COLLATOR = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' });

function formatModelSize(bytes) {
    const size = Number(bytes) || 0;
    if (size <= 0) return '约 229 MB';
    return `${(size / (1024 * 1024)).toFixed(0)} MB`;
}

function renderAudioAsrExtension(status = {}) {
    const message = $('audio-asr-extension-message');
    const button = $('audio-asr-install-btn');
    const progress = $('audio-asr-extension-progress');
    const progressBar = $('audio-asr-progress-bar');
    const progressValue = $('audio-asr-progress-value');
    const progressLabel = $('audio-asr-progress-label');
    const progressDetail = $('audio-asr-progress-detail');
    const modelSize = $('audio-asr-model-size');
    const card = $('audio-asr-extension-card');
    const state = String(status.state || 'not_installed');
    const installed = Boolean(status.installed);
    const downloading = state === 'downloading';
    if (message) message.textContent = status.error && state === 'failed'
        ? `${status.message || '安装失败'}：${status.error}`
        : String(status.message || (installed ? '本地语音识别已安装' : '模型尚未安装'));
    if (button) {
        button.disabled = downloading;
        button.hidden = installed;
        button.textContent = state === 'failed' ? '重试下载' : (downloading ? '正在下载…' : '下载并安装');
        button.setAttribute('aria-busy', downloading ? 'true' : 'false');
    }
    if (progress) progress.hidden = !downloading;
    if (progressBar) progressBar.value = Math.max(0, Math.min(100, Number(status.progress) || 0));
    if (progressValue) progressValue.textContent = `${Math.max(0, Math.min(100, Number(status.progress) || 0))}%`;
    if (progressLabel) progressLabel.textContent = downloading ? '正在下载模型' : '模型下载';
    if (progressDetail) progressDetail.textContent = downloading
        ? '下载在后台继续；可切换页面，完成后模型会保存在本机。'
        : (state === 'failed' ? '检查网络连接后可以重试。' : '模型只需下载一次。');
    if (modelSize) modelSize.textContent = formatModelSize(status.model_bytes);
    if (card) card.dataset.state = installed ? 'installed' : state;
}

async function refreshAudioAsrExtension() {
    if (!workflowApi?.getAudioAsrExtension) return null;
    try {
        const status = await workflowApi.getAudioAsrExtension();
        renderAudioAsrExtension(status);
        return status;
    } catch (error) {
        renderAudioAsrExtension({ state: 'failed', message: '无法读取本地识别扩展状态', error: error.message });
        return null;
    }
}

async function installAudioAsrExtension() {
    const button = $('audio-asr-install-btn');
    if (!workflowApi?.installAudioAsrExtension || button?.disabled) return;
    const pollToken = ++audioAsrPollToken;
    if (button) button.disabled = true;
    try {
        renderAudioAsrExtension(await workflowApi.installAudioAsrExtension());
        for (let attempt = 0; attempt < 3600 && pollToken === audioAsrPollToken; attempt += 1) {
            const status = await workflowApi.getAudioAsrExtension();
            renderAudioAsrExtension(status);
            if (status.installed || ['failed', 'not_installed'].includes(String(status.state))) break;
            await new Promise(resolve => window.setTimeout(resolve, 1000));
        }
    } catch (error) {
        renderAudioAsrExtension({ state: 'failed', message: '模型安装失败', error: error.message });
    } finally {
        if (button && !button.hidden) button.disabled = false;
    }
}

function safeAudioImportFileSize(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function updateAudioImportModeUI() {
    const state = activeAudioImport;
    if (!state) return;
    const mode = state.mode === 'continuous' ? 'continuous' : 'files';
    const continuous = mode === 'continuous';
    document.querySelectorAll('[data-audio-import-mode]').forEach(button => {
        const selected = button.dataset.audioImportMode === mode;
        button.classList.toggle('active', selected);
        button.setAttribute('aria-pressed', selected ? 'true' : 'false');
        button.disabled = Boolean(state.busy || state.commitSucceeded);
    });
    const title = $('audio-import-drop-title');
    const hint = $('audio-import-drop-hint');
    const zone = $('audio-import-dropzone');
    const folderButton = $('audio-import-folder-btn');
    if (title) title.textContent = continuous ? '拖入一整段音频' : '拖入分音频、文件夹或 ZIP';
    if (hint) hint.textContent = continuous
        ? '只选一个完整录音；系统会按题目原文匹配英文内容和需要朗读的中文引导语。'
        : '支持 MP3、WAV、M4A、AAC、OGG、OPUS、FLAC；也可以选择整个文件夹或 ZIP。';
    if (zone) zone.setAttribute('aria-label', continuous ? '拖入一整段音频' : '拖入分音频、文件夹或 ZIP');
    if (folderButton) folderButton.hidden = continuous;
    const selectButton = $('audio-import-select-btn');
    if (selectButton) selectButton.textContent = state.files.length
        ? (continuous ? '替换整段音频' : '替换音频组')
        : (continuous ? '选择整段音频' : '选择音频或 ZIP');
}

async function setAudioImportMode(mode) {
    const state = activeAudioImport;
    const next = mode === 'continuous' ? 'continuous' : 'files';
    if (!state || state.busy || state.commitSucceeded || state.mode === next) return;
    state.busy = true;
    updateAudioImportActions();
    try {
        await cleanupAudioImportBatch(state);
        state.mode = next;
        state.batchId = null;
        state.files = [];
        state.analysis = null;
        state.commitIdempotencyKey = null;
        state.commitResponse = null;
        state.commitSucceeded = false;
        state.filter = 'all';
        if ($('audio-import-search')) $('audio-import-search').value = '';
    } finally {
        state.busy = false;
    }
    updateAudioImportModeUI();
    renderAudioImportFiles();
    setAudioImportStatus(next === 'continuous'
        ? '请选择一整段录音；系统会匹配题目内容，包括需要朗读的中文引导语。'
        : '请选择与待处理题目一一对应的分音频，ZIP 和文件夹仍可使用。', 'info');
}

function audioImportPreviewMime(file, reportedMime = '') {
    const declared = String(reportedMime || '').split(';', 1)[0].trim().toLowerCase();
    if (declared.startsWith('audio/')) return declared;
    const extension = audioFileExtension(file);
    return ({
        '.mp3': 'audio/mpeg',
        '.wav': 'audio/wav',
        '.m4a': 'audio/mp4',
        '.aac': 'audio/aac',
        '.ogg': 'audio/ogg',
        '.opus': 'audio/ogg',
        '.flac': 'audio/flac',
    })[extension] || 'application/octet-stream';
}

function updateAudioImportPreviewButton(button, playing = false) {
    if (!button) return;
    const fileName = String(button.dataset.audioImportPreviewName || '音频');
    button.innerHTML = playing
        ? '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>'
        : '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
    button.title = playing ? `暂停试听 ${fileName}` : `试听 ${fileName}`;
    button.setAttribute('aria-label', button.title);
    button.setAttribute('aria-pressed', playing ? 'true' : 'false');
}

function stopAudioImportPreview() {
    const playback = activeAudioImportPreview;
    if (!playback) return;
    activeAudioImportPreview = null;
    audioImportPreviewToken += 1;
    if (playback.reader) void playback.reader.cancel().catch(() => {});
    else if (playback.stream) void playback.stream.cancel().catch(() => {});
    try {
        if (playback.sourceBuffer?.updating) playback.sourceBuffer.abort();
    } catch (_) { /* best-effort release of the media buffer */ }
    try {
        playback.audio?.pause();
        playback.audio?.removeAttribute('src');
        playback.audio?.load();
    } catch (_) { /* best-effort release of the preview element */ }
    if (playback.url) {
        try { URL.revokeObjectURL(playback.url); } catch (_) { /* ignore */ }
    }
    if (playback.button?.isConnected) {
        playback.button.dataset.loading = 'false';
        updateAudioImportPreviewButton(playback.button, false);
        const file = activeAudioImport?.files?.[playback.index];
        playback.button.disabled = Boolean(activeAudioImport?.busy || activeAudioImport?.commitSucceeded || !file?.uploaded);
    }
}

async function playAudioImportMediaSource(stream, playback, mimeType, isCurrent) {
    const mediaSource = new MediaSource();
    playback.mediaSource = mediaSource;
    playback.url = URL.createObjectURL(mediaSource);
    playback.audio.src = playback.url;
    playback.audio.preload = 'auto';
    playback.audio.load();
    await waitForMediaSourceOpen(mediaSource, isCurrent);
    if (!isCurrent()) return;
    const sourceBuffer = mediaSource.addSourceBuffer(mimeType);
    playback.sourceBuffer = sourceBuffer;
    const reader = stream.getReader();
    playback.reader = reader;
    let totalBytes = 0;
    let playPromise = null;
    try {
        while (true) {
            const part = await reader.read();
            if (part.done) break;
            if (!isCurrent()) return;
            const chunk = part.value instanceof Uint8Array ? part.value : new Uint8Array(part.value || []);
            totalBytes += chunk.byteLength;
            await appendMediaSourceChunk(sourceBuffer, chunk, isCurrent);
            if (!playPromise && playback.audio.readyState >= 2) {
                playback.loading = false;
                playPromise = playback.audio.play();
                await playPromise;
            }
        }
    } finally {
        if (playback.reader === reader) playback.reader = null;
        try { reader.releaseLock(); } catch (_) { /* reader may already be closed */ }
    }
    if (!isCurrent()) return;
    if (!totalBytes) throw new Error('音频内容为空');
    if (mediaSource.readyState === 'open' && !sourceBuffer.updating) mediaSource.endOfStream();
    if (!playPromise) {
        await waitForNativeAudioReady(playback.audio);
        if (!isCurrent()) return;
        playback.loading = false;
        playPromise = playback.audio.play();
    }
    await playPromise;
}

async function toggleAudioImportPreview(fileIndex, button) {
    const state = activeAudioImport;
    const file = state?.files?.[fileIndex];
    if (!state || !file?.uploaded || state.busy || state.commitSucceeded || !button) return;
    const current = activeAudioImportPreview;
    if (current?.index === fileIndex && current.audio?.src) {
        try {
            if (current.audio.paused) await current.audio.play();
            else current.audio.pause();
        } catch (error) {
            showToast(`${file.fileName} 试听失败：${error.message || '无法播放音频'}`, 'error');
            stopAudioImportPreview();
        }
        return;
    }
    if (current?.index === fileIndex && current.loading) return;
    stopAudioImportPreview();

    const token = ++audioImportPreviewToken;
    const audio = new Audio();
    audio.preload = 'none';
    const playback = { index: fileIndex, button, audio, stream: null, reader: null, url: '', token, loading: true };
    activeAudioImportPreview = playback;
    button.dataset.loading = 'true';
    button.disabled = true;
    button.textContent = '…';
    button.title = `正在加载 ${file.fileName}`;
    button.setAttribute('aria-label', button.title);
    const isCurrent = () => activeAudioImportPreview === playback && audioImportPreviewToken === token;
    const syncPlaybackButton = () => {
        if (!isCurrent() || !button.isConnected) return;
        playback.loading = false;
        button.dataset.loading = 'false';
        updateAudioImportPreviewButton(button, !audio.paused && !audio.ended);
        button.disabled = Boolean(state.busy || state.commitSucceeded);
    };
    audio.addEventListener('play', syncPlaybackButton);
    audio.addEventListener('pause', syncPlaybackButton);
    audio.addEventListener('ended', syncPlaybackButton);
    audio.addEventListener('error', () => {
        if (!isCurrent()) return;
        showToast(`${file.fileName} 的音频格式无法在本机播放`, 'error');
        stopAudioImportPreview();
    });

    try {
        const stream = await openRendererAudioImportStream(state.workflowId, state.batchId, fileIndex);
        if (!isCurrent()) {
            await stream.cancel().catch(() => {});
            return;
        }
        playback.stream = stream;
        const expectedBytes = Number(stream.metadata?.content_length || file.sizeBytes) || 0;
        const mimeType = audioImportPreviewMime(file, stream.metadata?.content_type);
        if (supportsMediaSourceMime(mimeType)) {
            await playAudioImportMediaSource(stream, playback, mimeType, isCurrent);
        } else {
            if (expectedBytes > AUDIO_IMPORT_PREVIEW_MAX_BYTES) {
                await stream.cancel().catch(() => {});
                throw new Error(`当前音频格式无法流式播放，文件超过 ${Math.round(AUDIO_IMPORT_PREVIEW_MAX_BYTES / (1024 * 1024))} MB`);
            }
            const reader = stream.getReader();
            playback.reader = reader;
            const chunks = [];
            let totalBytes = 0;
            try {
                while (true) {
                    const part = await reader.read();
                    if (part.done) break;
                    if (!isCurrent()) return;
                    const chunk = part.value instanceof Uint8Array ? part.value : new Uint8Array(part.value || []);
                    totalBytes += chunk.byteLength;
                    if (totalBytes > AUDIO_IMPORT_PREVIEW_MAX_BYTES) {
                        await reader.cancel().catch(() => {});
                        throw new Error(`当前音频格式无法流式播放，文件超过 ${Math.round(AUDIO_IMPORT_PREVIEW_MAX_BYTES / (1024 * 1024))} MB`);
                    }
                    chunks.push(chunk);
                }
            } finally {
                if (playback.reader === reader) playback.reader = null;
                try { reader.releaseLock(); } catch (_) { /* reader may already be closed */ }
            }
            if (!isCurrent()) return;
            if (!totalBytes) throw new Error('音频内容为空');
            playback.url = URL.createObjectURL(new Blob(chunks, { type: mimeType }));
            audio.src = playback.url;
            audio.preload = 'auto';
            audio.load();
            await waitForNativeAudioReady(audio);
            if (!isCurrent()) return;
            playback.loading = false;
            await audio.play();
        }
        syncPlaybackButton();
    } catch (error) {
        if (!isCurrent()) return;
        showToast(`${file.fileName} 试听失败：${error.message || '无法播放音频'}`, 'error');
        stopAudioImportPreview();
    }
}

function setAudioImportStatus(message, tone = 'info') {
    const status = $('audio-import-status');
    if (!status) return;
    status.textContent = String(message || '');
    status.dataset.tone = tone;
    const card = $('audio-import-status-card');
    if (card) card.dataset.tone = tone;
}

function renderAudioImportAnalysisProgress(progress = {}) {
    const title = $('audio-import-analysis-title');
    const stage = $('audio-import-analysis-stage');
    const detail = $('audio-import-analysis-detail');
    const count = $('audio-import-analysis-count');
    const bar = $('audio-import-analysis-bar');
    const fill = $('audio-import-analysis-fill');
    const total = Math.max(0, Number(progress.total) || 0);
    const completed = Math.max(0, Math.min(total, Number(progress.completed) || 0));
    const stageLabels = {
        connecting: '正在启动',
        loading_model: '准备识别',
        detecting_pauses: '检测停顿',
        opening_recognition: '识别音频',
        full_review: '复核长音频',
        continuous_recognition: '识别片段',
        matching: '匹配题目',
        cutting_audio: '生成片段',
        complete: '已完成',
        failed: '识别失败',
    };
    const stageUnits = {
        opening_recognition: '条音频',
        full_review: '条长音频',
        continuous_recognition: '个语音片段',
        matching: '项内容',
        cutting_audio: '个题目音频',
    };
    if (title) title.textContent = String(progress.label || '正在启动本机识别…');
    if (stage) stage.textContent = stageLabels[String(progress.stage || '')] || '本机识别';
    if (detail) detail.textContent = total
        ? `当前步骤已完成 ${completed} / ${total} ${stageUnits[String(progress.stage || '')] || '项'}`
        : '当前步骤正在本机处理，完成后会自动进入下一步。';
    if (count) count.textContent = total ? `${completed} / ${total}` : '处理中';
    if (bar) {
        if (total) {
            const fraction = completed / total;
            bar.dataset.state = 'determinate';
            bar.setAttribute('aria-valuenow', String(Math.round(fraction * 100)));
            bar.setAttribute('aria-valuetext', `${completed} / ${total}`);
            if (fill) fill.style.transform = `scaleX(${fraction})`;
        } else {
            bar.dataset.state = 'indeterminate';
            bar.removeAttribute('aria-valuenow');
            bar.setAttribute('aria-valuetext', '正在处理');
            if (fill) fill.style.removeProperty('transform');
        }
    }
}

function startAudioImportAnalysisProgress(workflowId, batchId) {
    if (audioImportAnalysisProgressTimer) window.clearTimeout(audioImportAnalysisProgressTimer);
    const token = ++audioImportAnalysisProgressToken;
    const progressCard = $('audio-import-analysis-card');
    const statusCard = $('audio-import-status-card');
    if (progressCard) {
        progressCard.hidden = false;
        progressCard.setAttribute('aria-busy', 'true');
    }
    if (statusCard) statusCard.hidden = true;
    renderAudioImportAnalysisProgress({
        stage: 'connecting',
        label: '正在启动本机识别…',
    });

    const poll = async () => {
        if (token !== audioImportAnalysisProgressToken || activeAudioImport?.phase !== 'analyzing') return;
        try {
            const progress = await workflowApi.getAudioImportAnalysisProgress(workflowId, batchId);
            if (token === audioImportAnalysisProgressToken
                && (progress?.active || ['complete', 'failed'].includes(String(progress?.stage || '')))) {
                renderAudioImportAnalysisProgress(progress);
            }
        } catch (_) {
            // Keep the indeterminate card visible while the analysis request continues.
        }
        if (token === audioImportAnalysisProgressToken && activeAudioImport?.phase === 'analyzing') {
            audioImportAnalysisProgressTimer = window.setTimeout(poll, AUDIO_IMPORT_PROGRESS_POLL_MS);
        }
    };
    void poll();
    return token;
}

function stopAudioImportAnalysisProgress(token) {
    if (token !== audioImportAnalysisProgressToken) return;
    if (audioImportAnalysisProgressTimer) window.clearTimeout(audioImportAnalysisProgressTimer);
    audioImportAnalysisProgressTimer = null;
    audioImportAnalysisProgressToken += 1;
    const progressCard = $('audio-import-analysis-card');
    const statusCard = $('audio-import-status-card');
    if (progressCard) {
        progressCard.hidden = true;
        progressCard.setAttribute('aria-busy', 'false');
    }
    if (statusCard) statusCard.hidden = false;
}

function questionOptionLabel(item) {
    const sequence = String(Number(item.sequence) + 1).padStart(2, '0');
    const content = String(item.text || '').replace(/\s+/g, ' ').trim();
    return `第 ${sequence} 题 · ${content.slice(0, 78)}${content.length > 78 ? '…' : ''}`;
}

function normalizeQuestionSearch(value) {
    return String(value || '')
        .normalize('NFKC')
        .toLocaleLowerCase()
        .replace(/[\s\p{P}\p{S}]+/gu, '');
}

function filenameQuestionSuggestion(fileName, items = []) {
    const basename = String(fileName || '').split(/[\\/]/).pop() || '';
    const stem = basename.replace(/\.[^.]+$/, '').trim();
    const match = stem.match(/^(?:q(?:uestion)?[-_ ]*)?0*(\d+)$/i) || stem.match(/(?:^|[-_ ])0*(\d+)$/);
    if (!match) return '';
    const sequence = Number(match[1]);
    if (!Number.isInteger(sequence) || sequence < 1) return '';
    const matches = items.filter(item => Number(item.sequence) + 1 === sequence);
    return matches.length === 1 ? String(matches[0].item_id) : '';
}

function audioImportBindingStats() {
    const state = activeAudioImport;
    const inputs = [...document.querySelectorAll('input[data-audio-import-file-index]')];
    const values = Array(state?.files?.length || 0).fill('');
    inputs.forEach(input => {
        const index = Number(input.dataset.audioImportFileIndex);
        if (Number.isInteger(index) && index >= 0 && index < values.length) values[index] = String(input.value || '');
    });
    const required = new Set((state?.items || []).map(item => String(item.item_id)));
    const counts = new Map();
    values.filter(value => required.has(value)).forEach(value => counts.set(value, (counts.get(value) || 0) + 1));
    const matched = values.filter(value => value && required.has(value) && counts.get(value) === 1).length;
    const duplicateCount = [...counts.values()].filter(count => count > 1).length;
    return { total: state?.items?.length || 0, files: state?.files?.length || 0, values, counts, required, matched, duplicateCount };
}

function applyAudioImportFileFilters() {
    const state = activeAudioImport;
    const list = $('audio-import-file-list');
    const query = normalizeQuestionSearch($('audio-import-search')?.value || '');
    const filter = state?.filter || 'all';
    const stats = audioImportBindingStats();
    const rows = [...(list?.querySelectorAll('.audio-import-row') || [])];
    let visibleRows = 0;
    rows.forEach(row => {
        const index = Number(row.dataset.audioImportRowIndex);
        const file = state?.files[index];
        const value = stats.values[index] || '';
        const duplicate = Boolean(value && stats.required.has(value) && stats.counts.get(value) > 1);
        const matched = Boolean(value && stats.required.has(value) && !duplicate);
        const text = normalizeQuestionSearch(`${file?.fileName || ''} ${file?.transcript || ''} ${file?.recognition_error || ''} ${row.querySelector('.audio-import-question-value')?.textContent || ''}`);
        const passesFilter = filter === 'all' || (filter === 'matched' ? matched : !matched);
        row.hidden = !passesFilter || Boolean(query && !text.includes(query));
        if (!row.hidden) visibleRows += 1;
        row.classList.toggle('is-matched', matched);
        row.classList.toggle('has-duplicate', duplicate);
    });
    const filterEmpty = $('audio-import-filter-empty');
    if (filterEmpty) filterEmpty.hidden = rows.length === 0 || visibleRows > 0;
    const matchedCount = $('audio-import-filter-matched-count');
    const pendingCount = $('audio-import-filter-pending-count');
    const allCount = $('audio-import-filter-all-count');
    if (matchedCount) matchedCount.textContent = String(stats.matched);
    if (pendingCount) pendingCount.textContent = String(Math.max(0, stats.files - stats.matched));
    if (allCount) allCount.textContent = String(stats.files);
    const coverage = $('audio-import-coverage-count');
    if (coverage) coverage.textContent = `${stats.matched} / ${stats.total}`;
    const missingItems = state?.analysis ? state.items.filter(item => !stats.counts.has(String(item.item_id))) : [];
    const footerDetail = $('audio-import-footer-detail');
    if (footerDetail) footerDetail.textContent = missingItems.length
        ? `缺少音频：${missingItems.map(questionOptionLabel).join('；')}`
        : '音频缺失、题目重复或未匹配时不能导入';
    const footer = $('audio-import-footer-count');
    if (footer) {
        if (!stats.files) footer.textContent = '等待音频文件';
        else if (stats.files !== stats.total) footer.textContent = `音频数量 ${stats.files} / ${stats.total}`;
        else if (stats.matched === stats.total) footer.textContent = '全部题目已唯一匹配，可以导入';
        else footer.textContent = `还有 ${Math.max(0, stats.total - stats.matched)} 道题待确认`;
    }
    const sourceSummary = $('audio-import-source-summary');
    const continuous = state?.mode === 'continuous';
    if (sourceSummary) sourceSummary.textContent = stats.files
        ? (continuous ? `整段音频已选择 · 重新选择会替换当前录音` : `已选择 ${stats.files} 个音频文件 · 重新选择会替换当前组`)
        : (continuous ? '还没有选择整段音频' : '还没有选择音频');
    const selectButton = $('audio-import-select-btn');
    if (selectButton) selectButton.textContent = stats.files
        ? (continuous ? '替换整段音频' : '替换音频组')
        : (continuous ? '选择整段音频' : '选择音频或 ZIP');
    const folderButton = $('audio-import-folder-btn');
    if (folderButton) folderButton.textContent = stats.files ? '替换文件夹' : '选择文件夹';
    if (folderButton) folderButton.hidden = continuous;
    $('audio-import-workspace')?.classList.toggle('has-files', stats.files > 0);
    document.querySelectorAll('[data-audio-import-filter]').forEach(button => {
        const active = button.dataset.audioImportFilter === filter;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    const caption = $('audio-import-list-caption');
    if (caption) caption.textContent = stats.files
        ? (continuous
            ? `从整段录音切出 ${stats.files} 个候选片段 · 已匹配 ${stats.matched} 个 · 待确认 ${Math.max(0, stats.files - stats.matched)} 个${missingItems.length ? ` · 缺少 ${missingItems.length} 条音频` : ''}`
            : `按文件名数字顺序排列 · 共 ${stats.files} 个 · 已匹配 ${stats.matched} 个 · 待确认 ${Math.max(0, stats.files - stats.matched)} 个`)
        : (continuous ? '选择整段录音后，系统会切出对应内容并匹配题目。' : '文件上传后会自动识别；无法确认的项目可手动选择。');
    updateAudioImportModeUI();
}

function closeAudioImportQuestionPicker({ restoreFocus = false } = {}) {
    const picker = audioImportQuestionPicker;
    if (!picker || picker.popover.hidden) return;
    picker.popover.hidden = true;
    picker.trigger?.setAttribute('aria-expanded', 'false');
    const trigger = picker.trigger;
    picker.trigger = null;
    picker.input = null;
    picker.fileIndex = -1;
    if (restoreFocus && !trigger?.disabled) trigger?.focus({ preventScroll: true });
}

function renderAudioImportQuestionOptions(query = '') {
    const picker = audioImportQuestionPicker;
    const state = activeAudioImport;
    if (!picker || !state) return;
    const normalizedQuery = normalizeQuestionSearch(query);
    const matches = state.items.filter(item => {
        if (!normalizedQuery) return true;
        const sequence = String(Number(item.sequence) + 1);
        const searchable = normalizeQuestionSearch(`${sequence} ${item.sequence} ${item.text || ''}`);
        return searchable.includes(normalizedQuery);
    });
    picker.options.replaceChildren();
    picker.count.textContent = `${matches.length} / ${state.items.length} 道题`;

    const addOption = (item, { clear = false } = {}) => {
        const id = clear ? '' : String(item.item_id);
        const selected = String(picker.input?.value || '') === id;
        const option = document.createElement('button');
        option.type = 'button';
        option.className = 'audio-import-question-option';
        if (clear) option.classList.add('is-clear');
        option.dataset.itemId = id;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', selected ? 'true' : 'false');
        if (selected) option.classList.add('is-selected');
        const sequence = document.createElement('span');
        sequence.className = 'audio-import-question-option-index';
        sequence.textContent = clear ? '—' : String(Number(item.sequence) + 1).padStart(2, '0');
        const copy = document.createElement('span');
        copy.className = 'audio-import-question-option-copy';
        const title = document.createElement('strong');
        title.textContent = clear ? '暂不绑定' : `第 ${String(Number(item.sequence) + 1).padStart(2, '0')} 题`;
        const content = document.createElement('small');
        content.textContent = clear ? '之后可以再选择对应题目' : String(item.text || '').replace(/\s+/g, ' ').trim();
        copy.append(title, content);
        const check = document.createElement('span');
        check.className = 'audio-import-question-option-check';
        check.setAttribute('aria-hidden', 'true');
        check.textContent = '✓';
        option.append(sequence, copy, check);
        option.addEventListener('click', () => {
            if (!picker.input || !activeAudioImport) return;
            picker.input.value = id;
            const file = activeAudioImport.files[picker.fileIndex];
            if (file) {
                file.selected_item_id = id;
                file.binding_was_edited = true;
                file.match_source = 'manual';
            }
            const trigger = picker.trigger;
            updateAudioImportQuestionTrigger(trigger, picker.input, item || null);
            const bindingLabel = trigger?.closest('.audio-import-binding')?.querySelector('.audio-import-binding-label');
            if (bindingLabel) bindingLabel.textContent = id ? '手动核对' : '待确认';
            closeAudioImportQuestionPicker({ restoreFocus: true });
            updateAudioImportActions();
            applyAudioImportFileFilters();
        });
        option.addEventListener('keydown', event => {
            const options = [...picker.options.querySelectorAll('.audio-import-question-option')];
            const index = options.indexOf(option);
            let nextIndex = -1;
            if (event.key === 'ArrowDown') nextIndex = Math.min(options.length - 1, index + 1);
            if (event.key === 'ArrowUp') nextIndex = Math.max(0, index - 1);
            if (event.key === 'Home') nextIndex = 0;
            if (event.key === 'End') nextIndex = options.length - 1;
            if (nextIndex < 0) return;
            event.preventDefault();
            options[nextIndex]?.focus();
        });
        picker.options.appendChild(option);
    };

    if (!normalizedQuery) addOption(null, { clear: true });
    matches.forEach(item => addOption(item));
    if (!matches.length) {
        const empty = document.createElement('p');
        empty.className = 'audio-import-question-empty';
        empty.textContent = '没有找到匹配题目，试试题号或题目关键词。';
        picker.options.appendChild(empty);
    }
    picker.options.scrollTop = 0;
}

function updateAudioImportQuestionTrigger(trigger, input, item = null) {
    if (!trigger || !input) return;
    const value = trigger.querySelector('.audio-import-question-value');
    const selected = item || activeAudioImport?.items.find(question => String(question.item_id) === String(input.value || ''));
    const hasValue = Boolean(input.value && selected);
    trigger.classList.toggle('is-selected', hasValue);
    trigger.title = hasValue ? questionOptionLabel(selected) : '选择这段音频对应的题目';
    if (value) value.textContent = hasValue ? questionOptionLabel(selected) : '请选择对应题目';
}

function ensureAudioImportQuestionPicker() {
    if (audioImportQuestionPicker) return audioImportQuestionPicker;
    const popover = document.createElement('section');
    popover.id = 'audio-import-question-picker';
    popover.className = 'audio-import-question-picker';
    popover.hidden = true;
    popover.setAttribute('role', 'dialog');
    popover.setAttribute('aria-label', '选择题目');
    const heading = document.createElement('header');
    heading.className = 'audio-import-question-picker-heading';
    const title = document.createElement('strong');
    title.textContent = '选择对应题目';
    const count = document.createElement('small');
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'audio-import-question-picker-close';
    close.setAttribute('aria-label', '关闭题目选择');
    close.textContent = '×';
    close.addEventListener('click', () => closeAudioImportQuestionPicker({ restoreFocus: true }));
    heading.append(title, count, close);
    const searchWrap = document.createElement('label');
    searchWrap.className = 'audio-import-question-search';
    const searchIcon = document.createElement('span');
    searchIcon.setAttribute('aria-hidden', 'true');
    searchIcon.textContent = '⌕';
    const search = document.createElement('input');
    search.type = 'search';
    search.autocomplete = 'off';
    search.spellcheck = false;
    search.placeholder = '搜索题号或题目内容';
    search.setAttribute('role', 'combobox');
    search.setAttribute('aria-autocomplete', 'list');
    search.setAttribute('aria-expanded', 'true');
    search.setAttribute('aria-controls', 'audio-import-question-options');
    search.addEventListener('input', () => renderAudioImportQuestionOptions(search.value));
    search.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            closeAudioImportQuestionPicker({ restoreFocus: true });
            return;
        }
        if (event.key === 'ArrowDown') {
            event.preventDefault();
            popover.querySelector('.audio-import-question-option')?.focus();
            return;
        }
        if (event.key === 'Enter') {
            event.preventDefault();
            if (search.value.trim()) popover.querySelector('.audio-import-question-option:not(.is-clear)')?.click();
        }
    });
    searchWrap.append(searchIcon, search);
    const options = document.createElement('div');
    options.id = 'audio-import-question-options';
    options.className = 'audio-import-question-options';
    options.setAttribute('role', 'listbox');
    popover.append(heading, searchWrap, options);
    document.body.appendChild(popover);

    audioImportQuestionPicker = { popover, count, search, options, trigger: null, input: null, fileIndex: -1 };
    document.addEventListener('pointerdown', event => {
        const picker = audioImportQuestionPicker;
        if (!picker || picker.popover.hidden) return;
        if (picker.popover.contains(event.target) || picker.trigger?.contains(event.target)) return;
        closeAudioImportQuestionPicker();
    }, true);
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape' || !audioImportQuestionPicker || audioImportQuestionPicker.popover.hidden) return;
        event.preventDefault();
        event.stopPropagation();
        closeAudioImportQuestionPicker({ restoreFocus: true });
    }, true);
    document.addEventListener('scroll', event => {
        if (audioImportQuestionPicker && !audioImportQuestionPicker.popover.hidden && !audioImportQuestionPicker.popover.contains(event.target)) {
            closeAudioImportQuestionPicker();
        }
    }, true);
    window.addEventListener('resize', () => closeAudioImportQuestionPicker());
    return audioImportQuestionPicker;
}

function openAudioImportQuestionPicker(trigger, input, fileIndex) {
    if (!trigger || trigger.disabled) return;
    const picker = ensureAudioImportQuestionPicker();
    if (!picker.popover.hidden && picker.trigger === trigger) {
        closeAudioImportQuestionPicker();
        return;
    }
    closeAudioImportQuestionPicker();
    picker.trigger = trigger;
    picker.input = input;
    picker.fileIndex = fileIndex;
    document.querySelectorAll('[data-audio-import-question-trigger]').forEach(button => button.setAttribute('aria-expanded', 'false'));
    trigger.setAttribute('aria-expanded', 'true');
    picker.search.value = '';
    renderAudioImportQuestionOptions('');
    picker.popover.hidden = false;
    const rect = trigger.getBoundingClientRect();
    const viewportPadding = 12;
    const width = Math.min(window.innerWidth - viewportPadding * 2, Math.max(360, Math.min(520, rect.width)));
    const maxHeight = Math.max(220, Math.min(430, window.innerHeight - viewportPadding * 2));
    picker.popover.style.width = `${width}px`;
    picker.popover.style.maxHeight = `${maxHeight}px`;
    const left = Math.max(viewportPadding, Math.min(rect.left, window.innerWidth - width - viewportPadding));
    const height = Math.min(picker.popover.scrollHeight, maxHeight);
    const roomBelow = window.innerHeight - rect.bottom - viewportPadding;
    const top = roomBelow >= Math.min(height, 250) || rect.top < height + viewportPadding
        ? Math.min(rect.bottom + 6, window.innerHeight - height - viewportPadding)
        : Math.max(viewportPadding, rect.top - height - 6);
    picker.popover.style.left = `${left}px`;
    picker.popover.style.top = `${top}px`;
    picker.search.focus({ preventScroll: true });
}

function audioImportHasCompleteBinding() {
    const state = activeAudioImport;
    if (!state?.batchId || state.analysis?.status === 'no_matches'
        || state.files.length !== state.items.length || state.files.some(file => !file.uploaded)) return false;
    const { values } = audioImportBindingStats();
    if (values.length !== state.files.length) return false;
    const selected = values.filter(Boolean);
    const required = new Set(state.items.map(item => String(item.item_id)));
    return selected.length === state.items.length
        && new Set(selected).size === selected.length
        && selected.every(itemId => required.has(itemId));
}

function updateAudioImportActions() {
    const state = activeAudioImport;
    const analyze = $('audio-import-analyze-btn');
    const commit = $('audio-import-commit-btn');
    const continuous = state?.mode === 'continuous';
    const countMatches = Boolean(state && state.items.length > 0 && (continuous ? state.files.length === 1 : state.files.length === state.items.length));
    const uploaded = Boolean(state?.files?.length) && state.files.every(file => file.uploaded);
    if (analyze) {
        analyze.hidden = !state?.files?.length || Boolean(state.analysis);
        analyze.disabled = !state || state.busy || !countMatches || !uploaded;
        analyze.textContent = state?.phase === 'analyzing' ? '正在识别…' : '重试识别';
    }
    if (commit) {
        commit.disabled = !state || state.busy || (!state.commitSucceeded && (!state.analysis || state.analysis.status === 'no_matches' || !audioImportHasCompleteBinding()));
        commit.textContent = state?.commitSucceeded ? '重试打开交付页' : (state?.phase === 'committing' ? '正在导入…' : '导入并进入交付');
    }
    document.querySelectorAll('input[data-audio-import-file-index]').forEach(select => {
        select.disabled = Boolean(state?.busy || state?.commitSucceeded || state?.analysis?.status === 'no_matches');
    });
    document.querySelectorAll('[data-audio-import-question-trigger]').forEach(button => {
        button.disabled = Boolean(state?.busy || state?.commitSucceeded || state?.analysis?.status === 'no_matches');
    });
    document.querySelectorAll('[data-audio-import-preview-index]').forEach(button => {
        const file = state?.files?.[Number(button.dataset.audioImportPreviewIndex)];
        button.disabled = Boolean(!file?.uploaded || state?.busy || state?.commitSucceeded || button.dataset.loading === 'true');
    });
    applyAudioImportFileFilters();
    if (!state || state.busy || !state.analysis) return;
    if (state.analysis.status === 'no_matches') {
        setAudioImportStatus('没有找到可与题目对应的语音片段。请确认录音内容，或切换到“分音频识别”。', 'warning');
        return;
    }
    if (state.mode === 'continuous' && Array.isArray(state.analysis.missing_item_ids) && state.analysis.missing_item_ids.length) {
        setAudioImportStatus(`整段音频只切出 ${state.files.length} / ${state.items.length} 个题目片段，缺少 ${state.analysis.missing_item_ids.length} 道题的音频，不能导入。`, 'warning');
        return;
    }
    const stats = audioImportBindingStats();
    if (stats.duplicateCount) {
        setAudioImportStatus(`有 ${stats.duplicateCount} 道题被重复绑定，请在“待确认”中调整。`, 'warning');
    } else if (stats.matched < state.items.length) {
        setAudioImportStatus(`还差 ${state.items.length - stats.matched} 道题待确认；全部唯一匹配后即可导入。`, 'warning');
    } else {
        setAudioImportStatus('全部题目已唯一匹配，可以导入。', 'success');
    }
}

function renderAudioImportFiles() {
    stopAudioImportPreview();
    closeAudioImportQuestionPicker();
    const list = $('audio-import-file-list');
    const state = activeAudioImport;
    if (!list) return;
    list.replaceChildren();
    if (!state?.files?.length) {
        const empty = document.createElement('div');
        empty.className = 'audio-import-empty';
        const noMatches = state?.analysis?.status === 'no_matches';
        const continuous = state?.mode === 'continuous';
        const title = document.createElement('strong');
        title.textContent = noMatches ? '没有找到可切割的匹配片段' : '匹配清单会显示在这里';
        const hint = document.createElement('span');
        hint.textContent = noMatches
            ? '请检查题目文本与整段录音，或切换到“分音频识别”重新导入。'
            : continuous
                ? '选择一段完整录音后，系统会识别并切出与题目匹配的语音片段。'
                : '拖入分音频、文件夹或 ZIP，系统会自动上传、识别并匹配题目。';
        empty.append(title, hint);
        list.appendChild(empty);
        updateAudioImportActions();
        return;
    }
    const orderedFiles = state.files
        .map((file, index) => ({ file, index }))
        .sort((left, right) => AUDIO_IMPORT_FILENAME_COLLATOR.compare(left.file.fileName || '', right.file.fileName || '') || left.index - right.index);
    orderedFiles.forEach(({ file, index }) => {
        const row = document.createElement('article');
        row.className = 'audio-import-row';
        row.dataset.audioImportRowIndex = String(index);
        row.setAttribute('role', 'listitem');
        const heading = document.createElement('div');
        heading.className = 'audio-import-row-heading';
        const title = document.createElement('div');
        title.className = 'audio-import-row-title';
        const name = document.createElement('strong');
        name.textContent = file.fileName;
        const previewButton = document.createElement('button');
        previewButton.type = 'button';
        previewButton.className = 'audio-import-preview-button';
        previewButton.dataset.audioImportPreviewIndex = String(index);
        previewButton.dataset.audioImportPreviewName = file.fileName;
        previewButton.dataset.loading = 'false';
        previewButton.disabled = !file.uploaded;
        updateAudioImportPreviewButton(previewButton, false);
        previewButton.addEventListener('click', () => void toggleAudioImportPreview(index, previewButton));
        const meta = document.createElement('small');
        meta.textContent = `${safeAudioImportFileSize(file.sizeBytes)}${file.segment_count > 1 ? ` · 合并 ${file.segment_count} 次朗读` : file.recognition_scope === 'segment' ? ' · 已切割' : ''}${file.uploaded ? ' · 已上传' : ' · 等待上传'}`;
        title.append(previewButton, name);
        heading.append(title, meta);
        const recognition = document.createElement('div');
        recognition.className = 'audio-import-row-recognition';
        const recognitionState = document.createElement('span');
        recognitionState.className = 'audio-import-row-recognition-state';
        const transcript = document.createElement('small');
        transcript.className = 'audio-import-transcript';
        if (file.recognition_error) {
            recognitionState.textContent = '识别失败';
            recognitionState.dataset.tone = 'warning';
            transcript.textContent = file.recognition_error;
        } else if (file.transcript) {
            recognitionState.textContent = file.recognition_scope === 'full' ? '整段复核' : '已识别';
            recognitionState.dataset.tone = 'success';
            transcript.textContent = file.transcript;
        } else {
            recognitionState.textContent = file.uploaded ? '待识别' : '待上传';
            recognitionState.dataset.tone = 'neutral';
            transcript.textContent = file.filename_suggested_item_id ? '文件序号可用于建议匹配，识别完成后再核对。' : '识别音频开头后会在这里显示内容';
        }
        recognition.append(recognitionState, transcript);
        row.append(heading, recognition);

        const label = document.createElement('div');
        label.className = 'audio-import-binding';
        const labelText = document.createElement('span');
        const suggestedId = file.suggested_item_id || file.filename_suggested_item_id || '';
        const selectedId = file.binding_was_edited ? String(file.selected_item_id || '') : String(file.selected_item_id || suggestedId);
        labelText.textContent = file.match_source === 'speech'
            ? '语音识别匹配'
            : file.match_source === 'filename' || (!file.binding_was_edited && file.filename_suggested_item_id && selectedId === String(file.filename_suggested_item_id))
                ? '文件序号建议'
                : file.binding_was_edited ? '手动核对' : '选择对应题目';
        labelText.className = 'audio-import-binding-label';
        const selection = document.createElement('div');
        selection.className = 'audio-import-question-selection';
        const select = document.createElement('input');
        select.type = 'hidden';
        select.dataset.audioImportFileIndex = String(index);
        select.value = selectedId;
        const trigger = document.createElement('button');
        trigger.type = 'button';
        trigger.className = 'audio-import-question-trigger';
        trigger.dataset.audioImportQuestionTrigger = String(index);
        trigger.setAttribute('aria-label', `为 ${file.fileName} 选择题目`);
        trigger.setAttribute('aria-haspopup', 'dialog');
        trigger.setAttribute('aria-expanded', 'false');
        trigger.setAttribute('aria-controls', 'audio-import-question-picker');
        const value = document.createElement('span');
        value.className = 'audio-import-question-value';
        const chevron = document.createElement('span');
        chevron.className = 'audio-import-question-chevron';
        chevron.setAttribute('aria-hidden', 'true');
        trigger.append(value, chevron);
        updateAudioImportQuestionTrigger(trigger, select);
        trigger.addEventListener('click', () => openAudioImportQuestionPicker(trigger, select, index));
        selection.append(select, trigger);
        label.append(labelText, selection);
        row.appendChild(label);

        if (Array.isArray(file.candidates) && file.candidates.length > 1) {
            const candidate = file.candidates.slice(0, 2).map(entry => {
                const item = state.items.find(question => String(question.item_id) === String(entry.item_id));
                if (!item) return '';
                const rawScore = Number(entry.score);
                const score = Number.isFinite(rawScore) ? Math.round(rawScore <= 1 ? rawScore * 100 : rawScore) : null;
                return `第 ${String(Number(item.sequence) + 1).padStart(2, '0')} 题${score === null ? '' : ` · ${score}%`}`;
            }).filter(Boolean).join('　·　');
            const hints = document.createElement('small');
            hints.className = 'audio-import-candidates';
            hints.textContent = candidate ? `相似候选：${candidate}` : '';
            if (candidate) recognition.appendChild(hints);
        }
        list.appendChild(row);
    });
    const filterEmpty = document.createElement('div');
    filterEmpty.id = 'audio-import-filter-empty';
    filterEmpty.className = 'audio-import-filter-empty';
    filterEmpty.hidden = true;
    filterEmpty.textContent = '当前筛选没有匹配的音频；调整筛选条件或搜索词。';
    list.appendChild(filterEmpty);
    updateAudioImportActions();
}

function setAudioImportUploadProgress(index, receivedBytes = 0, totalBytes = 0) {
    const state = activeAudioImport;
    if (!state) return;
    const progress = $('audio-import-progress');
    const bar = $('audio-import-progress-bar');
    const label = $('audio-import-progress-label');
    const value = $('audio-import-progress-value');
    if (progress) progress.hidden = false;
    const uploaded = state.files.slice(0, index).filter(file => file.uploaded).length;
    const fraction = totalBytes > 0 ? Math.min(1, receivedBytes / totalBytes) : 0;
    if (bar) bar.value = Math.round(((uploaded + fraction) / Math.max(1, state.files.length)) * 100);
    if (label) label.textContent = `正在上传 ${state.files[index]?.fileName || ''}`;
    if (value) value.textContent = `${uploaded} / ${state.files.length}`;
}

async function releaseSelectedAudioFiles(files = []) {
    for (const file of files) {
        if (!file?.sourceFileId || file.consumed) continue;
        try { await window.electronAPI?.releaseSourceFile?.(file.sourceFileId); } catch (_) { /* expired handles are safe to ignore */ }
        file.consumed = true;
    }
}

async function cleanupAudioImportBatch(state = activeAudioImport) {
    if (!state) return;
    await releaseSelectedAudioFiles(state.files);
    if (state.batchId && workflowApi?.abortAudioImportBatch) {
        try { await workflowApi.abortAudioImportBatch(state.workflowId, state.batchId); } catch (_) { /* batch may already be committed or expired */ }
    }
}

function audioFileExtension(file) {
    return String(file?.fileName || file?.name || '').split(/[\\/]/).pop().toLowerCase().match(/\.[^.]+$/)?.[0] || '';
}

function audioImportFileSpecs(files) {
    return files.map(file => ({ file_name: file.fileName, size_bytes: file.sizeBytes }));
}

async function readDroppedDirectory(entry, output = [], traversal = { entries: 0 }) {
    const reader = entry.createReader();
    while (true) {
        const entries = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
        if (!entries.length) break;
        for (const child of entries) {
            traversal.entries += 1;
            if (traversal.entries > 10000) throw new Error('拖入的文件夹条目超过 10000 个');
            if (child.isDirectory) await readDroppedDirectory(child, output, traversal);
            else if (child.isFile) {
                const file = await new Promise((resolve, reject) => child.file(resolve, reject));
                const extension = audioFileExtension(file);
                // Match the native folder picker: ignore unrelated documents
                // and nested archives while collecting supported audio files.
                if (AUDIO_IMPORT_EXTENSIONS.has(extension) && extension !== '.zip') {
                    output.push(file);
                    if (output.length > 2000) throw new Error('音频文件不能超过 2000 个');
                }
            }
        }
    }
    return output;
}

async function filesFromAudioDrop(dataTransfer) {
    const entries = [...(dataTransfer?.items || [])]
        .map(item => item.webkitGetAsEntry?.())
        .filter(Boolean);
    if (!entries.length) return [...(dataTransfer?.files || [])];
    const files = [];
    for (const entry of entries) {
        if (entry.isDirectory) files.push(...await readDroppedDirectory(entry));
        else if (entry.isFile) {
            const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
            files.push(file);
        }
    }
    return files;
}

async function selectAudioImportFiles({ source = 'files', droppedFiles = null } = {}) {
    const state = activeAudioImport;
    if (!state || state.busy) return;
    if (state.mode === 'continuous' && source === 'folder') {
        showToast('整段音频模式只接受一个完整录音文件', 'warning');
        return;
    }
    const selected = source === 'folder'
        ? await window.electronAPI?.selectAudioFolder?.()
        : source === 'drop'
            ? await window.electronAPI?.openAudioDropFiles?.(droppedFiles || [])
            : await window.electronAPI?.selectAudioFiles?.();
    if (!selected?.success) {
        if (selected?.reason !== 'user-cancelled') {
            showToast(selected?.reason === 'file-too-large' ? '音频文件为空或超过 512 MB' : (selected?.error || '无法读取所选音频文件'), 'error');
        }
        return;
    }
    const files = Array.isArray(selected.files) ? selected.files : [];
    const zipFiles = files.filter(file => audioFileExtension(file) === '.zip');
    if (state.mode === 'continuous' && (files.length !== 1 || zipFiles.length)) {
        await releaseSelectedAudioFiles(files);
        showToast('整段音频模式请选择一个音频文件；多个已切好的音频请切换到“分音频识别”。', 'warning');
        return;
    }
    if (zipFiles.length && (zipFiles.length !== 1 || files.length !== 1)) {
        await releaseSelectedAudioFiles(files);
        showToast('ZIP 压缩包请单独导入，不能和普通音频混选', 'warning');
        return;
    }
    const isArchive = zipFiles.length === 1;
    state.busy = true;
    state.phase = 'uploading';
    updateAudioImportActions();
    try {
        await cleanupAudioImportBatch(state);
        state.batchId = null;
        state.files = [];
        state.analysis = null;
        state.filter = 'all';
        if ($('audio-import-search')) $('audio-import-search').value = '';
        state.pendingArchiveName = isArchive ? files[0].fileName : '';
        renderAudioImportFiles();
        const batch = await workflowApi.createAudioImportBatch(state.workflowId, isArchive
            ? { archive: { file_name: files[0].fileName, size_bytes: files[0].sizeBytes } }
            : { files: audioImportFileSpecs(files), mode: state.mode });
        state.batchId = String(batch.batch_id || '');
        state.items = Array.isArray(batch.items) ? batch.items : [];
        state.files = isArchive ? [] : files.map((file, index) => {
            const filenameSuggestion = filenameQuestionSuggestion(file.fileName, state.items);
            return {
                ...file,
                index,
                uploaded: false,
                consumed: false,
                transcript: '',
                suggested_item_id: null,
                filename_suggested_item_id: filenameSuggestion,
                match_source: filenameSuggestion ? 'filename' : '',
                selected_item_id: '',
                binding_was_edited: false,
                candidates: [],
            };
        });
        state.analysis = null;
        const progress = $('audio-import-progress');
        if (progress) progress.hidden = false;
        if (isArchive) {
            setAudioImportStatus(`正在上传并读取 ZIP：${files[0].fileName}`, 'info');
            const progressLabel = $('audio-import-progress-label');
            const progressValue = $('audio-import-progress-value');
            if (progressLabel) progressLabel.textContent = `正在上传 ${files[0].fileName}`;
            if (progressValue) progressValue.textContent = 'ZIP';
            state.currentUploadId = `audio-import-archive-${state.batchId}`;
            try {
                const expanded = await workflowApi.uploadAudioImportArchive(state.workflowId, state.batchId, files[0], {
                    uploadId: state.currentUploadId,
                    onProgress: progressEvent => {
                        const bar = $('audio-import-progress-bar');
                        if (bar && progressEvent.totalBytes > 0) {
                            bar.value = Math.round(Math.min(1, progressEvent.receivedBytes / progressEvent.totalBytes) * 100);
                        }
                    },
                });
                files[0].consumed = true;
                state.files = (expanded.files || []).map((file, index) => ({
                    fileName: file.file_name,
                    sizeBytes: Number(file.size_bytes) || 0,
                    index,
                    uploaded: true,
                    consumed: true,
                    transcript: '',
                    suggested_item_id: null,
                    filename_suggested_item_id: filenameQuestionSuggestion(file.file_name, state.items),
                    match_source: filenameQuestionSuggestion(file.file_name, state.items) ? 'filename' : '',
                    selected_item_id: '',
                    binding_was_edited: false,
                    candidates: [],
                }));
            } catch (error) {
                files[0].consumed = true;
                throw error;
            } finally {
                state.currentUploadId = null;
            }
        }
        renderAudioImportFiles();
        const fileCountMatches = state.files.length === state.items.length;
        if (!isArchive && state.mode === 'files' && !fileCountMatches) {
            await releaseSelectedAudioFiles(state.files);
            state.busy = false;
            state.phase = '';
            if (progress) progress.hidden = true;
            setAudioImportStatus(`音频数量与待处理题目数量不一致（${state.files.length} / ${state.items.length}），请重新选择完整的一组。`, 'warning');
            updateAudioImportActions();
            return;
        }
        for (let index = 0; !isArchive && index < state.files.length; index += 1) {
            const file = state.files[index];
            const uploadId = `audio-import-${state.batchId}-${index}`;
            state.currentUploadId = uploadId;
            try {
                await workflowApi.uploadAudioImportFile(state.workflowId, state.batchId, index, file, {
                    uploadId,
                    onProgress: progressEvent => setAudioImportUploadProgress(index, progressEvent.receivedBytes, progressEvent.totalBytes),
                });
                file.consumed = true;
                file.uploaded = true;
            } catch (error) {
                // The main process consumes and closes a native handle once an
                // upload request begins, including its error path.
                file.consumed = true;
                await releaseSelectedAudioFiles(state.files.slice(index + 1));
                throw error;
            } finally {
                state.currentUploadId = null;
            }
            setAudioImportUploadProgress(index + 1, 0, 0);
        }
        if (progress) progress.hidden = true;
        state.busy = false;
        state.phase = '';
        renderAudioImportFiles();
        const shouldAnalyze = state.mode === 'continuous' || state.files.length === state.items.length;
        setAudioImportStatus(state.mode === 'continuous'
            ? '整段录音已上传，正在识别录音内容并按题目切割…'
            : shouldAnalyze
                ? '音频已上传，正在识别开头并匹配题目…'
                : `音频数量与待处理题目数量不一致（${state.files.length} / ${state.items.length}），请重新选择完整的一组。`,
        shouldAnalyze ? 'info' : 'warning');
        updateAudioImportActions();
        if (shouldAnalyze) void analyzeAudioImport();
    } catch (error) {
        await releaseSelectedAudioFiles(files);
        state.busy = false;
        state.phase = '';
        if ($('audio-import-progress')) $('audio-import-progress').hidden = true;
        setAudioImportStatus(`上传未完成：${error.message}`, 'error');
        updateAudioImportActions();
    }
}

async function analyzeAudioImport() {
    const state = activeAudioImport;
    const continuous = state?.mode === 'continuous';
    if (!state?.batchId || state.busy || (continuous ? state.files.length !== 1 : state.files.length !== state.items.length)) return;
    state.busy = true;
    state.phase = 'analyzing';
    const progressToken = startAudioImportAnalysisProgress(state.workflowId, state.batchId);
    updateAudioImportActions();
    try {
        const analysis = await workflowApi.analyzeAudioImportBatch(state.workflowId, state.batchId);
        state.analysis = analysis;
        const byIndex = new Map((analysis.files || []).map(file => [Number(file.file_index), file]));
        if (continuous) {
            state.files = (analysis.files || []).map((recognized, index) => ({
                fileName: String(recognized.file_name || `片段-${index + 1}.mp3`),
                sizeBytes: Number(recognized.size_bytes) || 0,
                index,
                uploaded: true,
                consumed: true,
                ...recognized,
                suggested_item_id: recognized.suggested_item_id ? String(recognized.suggested_item_id) : null,
                filename_suggested_item_id: null,
                match_source: recognized.suggested_item_id ? 'speech' : '',
                selected_item_id: '',
                binding_was_edited: false,
            }));
        } else {
            state.files = state.files.map((file, index) => {
                const recognized = byIndex.get(index) || {};
                const suggestedId = recognized.suggested_item_id || file.filename_suggested_item_id || null;
                return {
                    ...file,
                    ...recognized,
                    suggested_item_id: suggestedId ? String(suggestedId) : null,
                    match_source: recognized.suggested_item_id ? 'speech' : (file.filename_suggested_item_id ? 'filename' : ''),
                };
            });
        }
        if (analysis.status === 'file_count_mismatch') {
            setAudioImportStatus(`音频数量与待处理题目数量不一致（${analysis.selected_file_count} / ${analysis.required_item_count}），不能导入。`, 'warning');
        } else if (analysis.status === 'no_matches') {
            setAudioImportStatus('没有找到可与题目对应的语音片段。请检查题目文本或录音，也可以切换到“分音频识别”。', 'warning');
        } else if (continuous) {
            const matched = state.files.filter(file => file.suggested_item_id).length;
            const missing = Number(analysis.missing_item_ids?.length) || Math.max(0, state.items.length - state.files.length);
            setAudioImportStatus(
                `整段音频切割完成：找到 ${state.files.length} 个题目片段，自动匹配 ${matched} / ${state.items.length} 道题${missing ? `，缺少 ${missing} 道题的音频` : ''}。请核对后再导入。`,
                missing || matched < state.items.length ? 'warning' : 'success',
            );
        } else {
            const matched = state.files.filter(file => file.suggested_item_id).length;
            const expanded = state.files.filter(file => file.recognition_scope === 'full').length;
            setAudioImportStatus(
                `识别完成：自动匹配 ${matched} / ${state.items.length} 道题；${expanded} 段音频进行了整段复核。请核对并补齐所有绑定。`,
                matched === state.items.length ? 'success' : 'warning',
            );
        }
        renderAudioImportFiles();
    } catch (error) {
        setAudioImportStatus(`识别失败：${error.message}`, 'error');
        showToast(error.message || '本机音频识别失败', 'error');
    } finally {
        stopAudioImportAnalysisProgress(progressToken);
        state.busy = false;
        state.phase = '';
        updateAudioImportActions();
    }
}

async function commitAudioImport() {
    const state = activeAudioImport;
    if (!audioImportHasCompleteBinding() || state.busy) return;
    stopAudioImportPreview();
    state.busy = true;
    state.phase = 'committing';
    updateAudioImportActions();
    setAudioImportStatus('正在验证音频并绑定题目，完成后会直接打开交付页。', 'info');
    try {
        const bindings = [...document.querySelectorAll('input[data-audio-import-file-index]')].map(select => ({
            file_index: Number(select.dataset.audioImportFileIndex),
            item_id: String(select.value),
        }));
        if (!state.commitIdempotencyKey) {
            state.commitIdempotencyKey = `audio-import-${window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
        }
        let response = state.commitResponse || null;
        if (!response) {
            response = await workflowApi.commitAudioImportBatch(
                state.workflowId,
                state.batchId,
                { bindings },
                { idempotencyKey: state.commitIdempotencyKey },
            );
            state.commitResponse = response;
            state.commitSucceeded = true;
        }
        const workspace = await hydrateWorkflowWorkspace(state.workflowId, {
            snapshot: response.current_snapshot || null,
            silent: false,
        });
        if (!workspace) throw new Error('音频已提交，但交付工作区暂时无法刷新，请稍后重试');
        const itemsById = new Map((workspace.items || []).map(item => [String(item.item_id), item]));
        const files = (workspace.artifacts || [])
            .filter(artifact => artifact.artifact_type === 'tts-segment' && artifact.format === 'mp3' && artifact.item_id)
            .map(artifact => {
                const item = itemsById.get(String(artifact.item_id)) || {};
                const metadata = item.metadata && typeof item.metadata === 'object' ? item.metadata : {};
                return {
                    ...artifact,
                    format: 'mp3',
                    filename: artifact.filename || `audio-${Number(item.sequence) + 1}.mp3`,
                    doc_type: String(metadata.doc_type || item.item_type || '题目'),
                    category: String(item.item_type || metadata.category || ''),
                    text: String(item.normalized_content || ''),
                    role: item.role || metadata.role || null,
                    voice_key: item.voice_key || null,
                    sequence: Number(item.sequence) || 0,
                };
            })
            .sort((left, right) => left.sequence - right.sequence);
        generatedFiles = files;
        generationResult = 'done';
        const event = {
            type: 'done',
            workflow_id: state.workflowId,
            completed: files.length,
            failed: 0,
            cancelled: 0,
            total: Number(workspace.progress?.total) || files.length,
            failed_items: [],
            input_mode: 'external_audio',
        };
        latestCurrentResultEvent = event;
        buildResultPage(event, {
            mode: 'current',
            sessionId: state.workflowId,
            workflowId: state.workflowId,
            sourceFilename: currentSession?.source_filename,
            files,
            completed: files.length,
            failed: 0,
            cancelled: 0,
            total: event.total,
            format: 'mp3',
            preview: false,
            workspace,
        });
        setAudioImportWorkspaceVisible(false);
        activeAudioImport = null;
        root.WORDTTS_RENDERER?.getModule?.('systemInput.delivery')?.exitExternalAudioImportMode?.({ cleanup: false });
        goToStep(4);
        void refreshHistoryRecords({ showLoading: false });
        showToast('外部音频已全部绑定并进入交付');
    } catch (error) {
        state.busy = false;
        state.phase = '';
        const message = state.commitSucceeded
            ? `音频已导入，但交付工作区同步失败：${error.message}。可以重试同步，或稍后从历史记录打开。`
            : `导入未完成：${error.message}`;
        setAudioImportStatus(message, state.commitSucceeded ? 'warning' : 'error');
        showToast(state.commitSucceeded ? '音频已经导入，交付工作区暂时无法同步' : (error.message || '音频导入失败'), state.commitSucceeded ? 'warning' : 'error');
        updateAudioImportActions();
    }
}

function setAudioImportWorkspaceVisible(visible) {
    if (!visible) closeAudioImportQuestionPicker();
    const workspace = $('audio-import-workspace');
    const voiceView = $('voice-config-view');
    const heading = voiceView?.querySelector('.voice-heading');
    const deliveryChoice = $('system-input-delivery-choice-panel');
    const anchor = deliveryChoice || heading;
    if (visible && workspace && anchor && workspace.parentElement !== anchor.parentElement) {
        anchor.insertAdjacentElement('afterend', workspace);
    }
    if (workspace) workspace.hidden = !visible;
    if (heading) heading.hidden = visible;
    if (deliveryChoice) deliveryChoice.hidden = visible;
    voiceView?.classList.toggle('audio-import-mode-active', visible);
    const voiceWorkbench = $('voice-workbench');
    const productionPlan = $('production-plan');
    if (voiceWorkbench) voiceWorkbench.hidden = visible;
    if (productionPlan) productionPlan.hidden = visible;
}

function isAudioImportBusy() {
    return Boolean(activeAudioImport?.busy);
}

function canOpenAudioImportWorkflow(workflowId) {
    return !activeAudioImport
        || activeAudioImport.workflowId === String(workflowId || '')
        || !activeAudioImport.busy;
}

async function hideAudioImportWorkspace() {
    const state = activeAudioImport;
    if (state?.busy) return false;
    stopAudioImportPreview();
    activeAudioImport = null;
    setAudioImportWorkspaceVisible(false);
    await cleanupAudioImportBatch(state);
    return true;
}

async function openAudioImportWorkspace(workspace = currentWorkspace) {
    if (!isElectron || !workflowApi || !currentSession?.session_id) {
        showToast('请先导入并解析题目文档', 'warning');
        return false;
    }
    const workflowId = String(currentSession.session_id);
    if (!canOpenAudioImportWorkflow(workflowId)) {
        showToast('另一个任务的音频正在上传、识别或提交，请先等待完成', 'warning');
        return false;
    }
    if (!workspace || workspace.snapshot?.workflow_id !== workflowId) {
        workspace = await hydrateWorkflowWorkspace(workflowId, { silent: false });
    }
    if (currentSession?.session_id !== workflowId) return false;
    if (!workspace?.items?.length) {
        showToast('请先完成文档解析，再导入题目音频', 'warning');
        return false;
    }
    const extension = await refreshAudioAsrExtension();
    if (currentSession?.session_id !== workflowId) return false;
    if (!extension?.installed) {
        showToast('需要先在设置中心的“扩展”板块下载并安装本地语音识别', 'warning');
        root.WORDTTS_RENDERER?.getModule?.('systemInput.delivery')?.exitExternalAudioImportMode?.({ cleanup: false });
        showExtensionsPage();
        return false;
    }
    const container = $('audio-import-workspace');
    if (!container) return false;
    if (activeAudioImport?.workflowId !== workflowId) {
        if (activeAudioImport && !await hideAudioImportWorkspace()) return false;
        if (currentSession?.session_id !== workflowId) return false;
        const items = workspace.items
        .filter(item => String(item.status || '') !== 'SKIPPED')
        .map(item => ({
            item_id: String(item.item_id),
            sequence: Number(item.sequence) || 0,
            item_type: String(item.item_type || ''),
            text: String(item.normalized_content || item.text_preview || ''),
        }));
        activeAudioImport = { workflowId, batchId: null, mode: 'files', files: [], items, analysis: null, busy: false, phase: '', filter: 'all' };
        setAudioImportStatus(`当前有 ${items.length} 道待处理题目。请导入覆盖全部题目的音频。`, 'info');
    }
    setAudioImportWorkspaceVisible(true);
    updateAudioImportModeUI();
    renderAudioImportFiles();
    return true;
}

async function openAudioImportDialog() {
    const delivery = root.WORDTTS_RENDERER?.getModule?.('systemInput.delivery');
    if (delivery?.activateExternalAudioImportMode?.(currentWorkspace)) return true;
    return openAudioImportWorkspace(currentWorkspace);
}

function leaveAudioImportWorkspace() {
    const delivery = root.WORDTTS_RENDERER?.getModule?.('systemInput.delivery');
    if (delivery?.exitExternalAudioImportMode) {
        if (!delivery.exitExternalAudioImportMode()) return;
    } else {
        void hideAudioImportWorkspace();
    }
}

async function handleAudioImportDrop(event) {
    event.preventDefault();
    event.stopPropagation();
    root.WORDTTS_RENDERER?.getModule?.('source.fileController')?.setGlobalFileDropActive?.(false);
    $('audio-import-dropzone')?.classList.remove('dragover');
    if (isAudioImportBusy()) return;
    try {
        const files = await filesFromAudioDrop(event.dataTransfer);
        if (!files.length) {
            showToast('拖入的文件夹中没有可读取的音频', 'warning');
            return;
        }
        await selectAudioImportFiles({ source: 'drop', droppedFiles: files });
    } catch (error) {
        showToast(`读取拖入内容失败：${error.message || '请重试'}`, 'error');
    }
}

function initAudioImportModule() {
    // Renderer helpers are also evaluated in lightweight VM-based tests with
    // a partial document. Keep module registration usable there; the real
    // Electron renderer always provides querySelectorAll.
    if (typeof document === 'undefined' || typeof document.querySelectorAll !== 'function') return;

    $('audio-asr-install-btn')?.addEventListener('click', () => { void installAudioAsrExtension(); });
    document.querySelectorAll('[data-audio-import-mode]').forEach(button => {
        button.addEventListener('click', () => { void setAudioImportMode(button.dataset.audioImportMode); });
    });
    $('audio-import-select-btn')?.addEventListener('click', () => { void selectAudioImportFiles(); });
    $('audio-import-folder-btn')?.addEventListener('click', () => { void selectAudioImportFiles({ source: 'folder' }); });
    $('audio-import-analyze-btn')?.addEventListener('click', () => { void analyzeAudioImport(); });
    $('audio-import-commit-btn')?.addEventListener('click', () => { void commitAudioImport(); });
    $('audio-import-search')?.addEventListener('input', applyAudioImportFileFilters);
    document.querySelectorAll('[data-audio-import-filter]').forEach(button => {
        button.addEventListener('click', () => {
            if (!activeAudioImport) return;
            activeAudioImport.filter = button.dataset.audioImportFilter || 'all';
            applyAudioImportFileFilters();
        });
    });
    $('audio-import-back-btn')?.addEventListener('click', leaveAudioImportWorkspace);
    $('audio-import-cancel-btn')?.addEventListener('click', leaveAudioImportWorkspace);
    const dropzone = $('audio-import-dropzone');
    dropzone?.addEventListener('dragenter', event => {
        event.preventDefault();
        event.stopPropagation();
        root.WORDTTS_RENDERER?.getModule?.('source.fileController')?.setGlobalFileDropActive?.(true);
        dropzone.classList.add('dragover');
    });
    dropzone?.addEventListener('dragover', event => {
        event.preventDefault();
        event.stopPropagation();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
        root.WORDTTS_RENDERER?.getModule?.('source.fileController')?.setGlobalFileDropActive?.(true);
        dropzone.classList.add('dragover');
    });
    dropzone?.addEventListener('dragleave', event => {
        if (!dropzone.contains(event.relatedTarget)) dropzone.classList.remove('dragover');
    });
    dropzone?.addEventListener('drop', event => { void handleAudioImportDrop(event); });
    window.electronAPI?.onSourceUploadProgress?.(progress => {
        const state = activeAudioImport;
        if (!state || progress?.uploadId !== state.currentUploadId) return;
        if (String(state.currentUploadId).startsWith('audio-import-archive-')) {
            const fraction = Number(progress.totalBytes) > 0
                ? Math.min(1, Number(progress.receivedBytes) / Number(progress.totalBytes))
                : 0;
            const bar = $('audio-import-progress-bar');
            const value = $('audio-import-progress-value');
            if (bar) bar.value = Math.round(fraction * 100);
            if (value) value.textContent = `${Math.round(fraction * 100)}%`;
            return;
        }
        const index = Number(String(progress.uploadId).split('-').at(-1));
        if (Number.isInteger(index)) setAudioImportUploadProgress(index, progress.receivedBytes, progress.totalBytes);
    });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAudioImportModule, { once: true });
} else {
    initAudioImportModule();
}

registerRendererModule('extensions.audioImport', {
    refreshAudioAsrExtension,
    installAudioAsrExtension,
    openAudioImportDialog,
    openAudioImportWorkspace,
    handleAudioImportDrop,
    hideAudioImportWorkspace,
    isBusy: isAudioImportBusy,
    canOpenWorkflow: canOpenAudioImportWorkflow,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
