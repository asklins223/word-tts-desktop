/** Renderer module: updates.updateCenter */
(function attachRendererFeature_updates_updateCenter(root) {
    'use strict';

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

registerRendererModule("updates.updateCenter", {
    UPDATE_STATUS_VALUES,
    normalizeUpdateState,
    hasInstallableUpdate,
    versionDisplay,
    updateStatusPresentation,
    isForcedUpdateBlocking,
    formatUpdateBytes,
    formatUpdateDate,
    renderVersionReleaseNotes,
    renderVersionCenter,
    applyUpdateState,
    bindNativeAppUpdates,
    runUpdateAction,
    openUpdateReleasePage,
    bindNativeAppNotices,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

