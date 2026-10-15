'use strict';

const path = require('node:path');
const { compareVersions, deriveUpdatePolicy } = require('./update-manager');

async function checkStartupUpdate(manager, {
    timeoutMs = 20_000,
    allowInProgress = false,
    refresh = false,
} = {}) {
    let timeout;
    try {
        const state = await Promise.race([
            manager.check({ refresh }),
            new Promise((_, reject) => {
                timeout = setTimeout(() => reject(new Error('启动更新检查超时。')), timeoutMs);
            }),
        ]);
        const accepted = ['available', 'up-to-date'];
        if (allowInProgress) accepted.push('downloading', 'downloaded', 'installing');
        if (!accepted.includes(state?.status)) {
            throw new Error(state?.error?.message || '无法确认线上更新信息。');
        }
        return state;
    } finally {
        clearTimeout(timeout);
    }
}

/** Business startup is allowed only after this returns true. */
async function runStartupUpdate({ app, dialog, manager, release, createProgressWindow, isQuitting = () => false }) {
    if (!release.required) return true;
    if (!release.updateAvailable) {
        // The startup gate has already proved that the installed version's
        // package is online. Do not perform a second, independent /latest
        // lookup here: it can fail or race with the gate even though the
        // verified current-version package is still usable.
        if (typeof manager.markVerifiedCurrentRelease === 'function') {
            manager.markVerifiedCurrentRelease(release.version);
        } else if (release.latestMetadataAvailable === false) {
            return true;
        } else {
            await checkStartupUpdate(manager);
        }
        return true;
    }
    // The release gate has already fetched and probed this exact package. Use
    // that metadata directly so a second, racing read of /latest cannot
    // replace or invalidate the verified startup decision.
    let state = release.updateAvailable && typeof manager.acceptVerifiedUpdate === 'function'
        ? await manager.acceptVerifiedUpdate(release.updateInfo, {
            verifiedArtifactUrl: release.artifactUrl,
        })
        : await checkStartupUpdate(manager);
    if (isQuitting()) return false;
    const hasInstallableUpdate = state.canDownload
        || (state.status === 'downloaded' && state.canInstall);
    if (release.updateAvailable && (!hasInstallableUpdate
        || compareVersions(state.version, release.version) < 0)) {
        throw new Error('线上安装包与更新信息不一致，请稍后重试。');
    }
    if ((!state.canDownload && !(state.status === 'downloaded' && state.canInstall)) || !state.version) return true;

    // Keep a force policy already verified during this launch even if an
    // overlapping metadata fetch temporarily reports an optional policy.
    const forced = state.isForced || (release.updateAvailable
        && deriveUpdatePolicy(release.updateInfo, state.currentVersion).isForced);
    const choice = await dialog.showMessageBox({
        type: forced ? 'warning' : 'info',
        title: forced ? '小猪wordTTS 必须更新' : '小猪wordTTS 发现新版本',
        message: `${forced ? '必须更新到' : '发现新版本'} v${state.version}`,
        detail: `${state.updateMessage || ''}\n\n${forced
            ? '此版本为强制更新，完成更新前无法进入软件。'
            : '你可以立即更新，也可以稍后更新并继续使用软件。'}`.trim(),
        buttons: ['立即更新', forced ? '退出软件' : '稍后更新'],
        defaultId: 0,
        cancelId: 1,
        noLink: true,
    });
    if (isQuitting()) return false;
    if (choice.response !== 0) {
        if (!forced) return true;
        app.quit();
        return false;
    }

    const progress = createProgressWindow();
    try {
        while (!isQuitting()) {
            state = await manager.download();
            if (isQuitting()) return false;
            if (state.status === 'downloaded' && state.canInstall) {
                state = await manager.install();
                if (state.status === 'installing') return false;
            }
            const failure = await dialog.showMessageBox({
                type: 'error',
                title: '小猪wordTTS 更新失败',
                message: state.error?.message || '更新未能完成，请重试。',
                detail: forced ? '完成强制更新后才能进入软件。' : '可以重试更新，或稍后更新并继续使用软件。',
                buttons: forced ? ['重试', '退出软件'] : ['重试', '稍后更新', '退出软件'],
                defaultId: 0,
                cancelId: forced ? 1 : 2,
                noLink: true,
            });
            if (isQuitting()) return false;
            if (failure.response === 0) continue;
            if (!forced && failure.response === 1) return true;
            app.quit();
            return false;
        }
        return false;
    } finally {
        progress.close();
    }
}

function createStartupUpdateWindow({ app, BrowserWindow, canCreate }) {
    if (!canCreate()) throw new Error('启动服务检查尚未通过，无法创建更新窗口。');
    const win = new BrowserWindow({
        width: 460,
        height: 270,
        resizable: false,
        maximizable: false,
        fullscreenable: false,
        show: false,
        autoHideMenuBar: true,
        title: '小猪wordTTS 正在更新',
        backgroundColor: '#f4f9ff',
        webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
    });
    let finishing = false;
    let loaded = false;
    let latestState = { status: 'downloading' };
    const update = state => {
        latestState = state;
        if (win.isDestroyed()) return;
        const percent = Math.min(100, Math.max(0, Number(state.progress?.percent) || 0));
        win.setProgressBar(state.status === 'error' ? -1 : percent > 0 ? percent / 100 : 2);
        if (!loaded) return;
        const payload = {
            title: state.status === 'installing' ? '正在启动安装' : state.status === 'error' ? '更新未能完成' : '正在下载更新',
            detail: state.status === 'error' ? state.error?.message || '请在提示中选择重试或退出。'
                : state.status === 'installing' ? '安装完成后，请重新打开软件。' : `已下载 ${percent.toFixed(1)}%，请保持网络连接。`,
            percent,
        };
        void win.webContents.executeJavaScript(`(() => {
            const state = ${JSON.stringify(payload)};
            document.getElementById('title').textContent = state.title;
            document.getElementById('detail').textContent = state.detail;
            document.getElementById('progress').value = state.percent;
        })()`).catch(() => {});
    };
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.on('will-navigate', event => event.preventDefault());
    win.webContents.once('did-finish-load', () => {
        loaded = true;
        update(latestState);
    });
    win.webContents.on('did-fail-load', (_event, code) => {
        if (code !== -3 && !finishing) app.quit();
    });
    win.webContents.on('render-process-gone', () => { if (!finishing) app.quit(); });
    win.once('ready-to-show', () => { if (!finishing && !win.isDestroyed()) win.show(); });
    win.on('closed', () => { if (!finishing) app.quit(); });
    void win.loadFile(path.join(__dirname, 'startup-update.html')).catch(() => { if (!finishing) app.quit(); });
    return {
        update,
        close() {
            finishing = true;
            if (!win.isDestroyed()) win.destroy();
        },
    };
}

module.exports = { checkStartupUpdate, createStartupUpdateWindow, runStartupUpdate };
