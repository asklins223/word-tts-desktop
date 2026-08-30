'use strict';

/**
 * GitHub Releases based update coordinator.
 *
 * The renderer only sees the serializable state produced here. The actual
 * electron-updater instance stays in the main process so release metadata,
 * download paths, and installation actions never cross the context bridge.
 */

const UPDATE_STATUSES = new Set([
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

const NUMERIC_IDENTIFIER = '(?:0|[1-9]\\d*)';
const NON_NUMERIC_IDENTIFIER = '(?:[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)';
const PRERELEASE_IDENTIFIER = `(?:${NUMERIC_IDENTIFIER}|${NON_NUMERIC_IDENTIFIER})`;
const BUILD_IDENTIFIER = '[0-9A-Za-z-]+';
const VERSION_PATTERN = new RegExp(
    `^v?(${NUMERIC_IDENTIFIER})\\.(${NUMERIC_IDENTIFIER})\\.(${NUMERIC_IDENTIFIER})`
    + `(?:-(${PRERELEASE_IDENTIFIER}(?:\\.${PRERELEASE_IDENTIFIER})*))?`
    + `(?:\\+${BUILD_IDENTIFIER}(?:\\.${BUILD_IDENTIFIER})*)?$`,
);
const RELEASE_NOTES_LIMIT = 200_000;
const DEFAULT_RELEASE_URL = 'https://github.com/asklins223/word-tts-desktop/releases';

function parseVersion(value) {
    const match = String(value || '').trim().match(VERSION_PATTERN);
    if (!match) return null;
    return {
        major: Number(match[1]),
        minor: Number(match[2]),
        patch: Number(match[3]),
        majorText: match[1],
        minorText: match[2],
        patchText: match[3],
        prerelease: match[4] ? match[4].split('.') : [],
    };
}

function compareNumericIdentifiers(left, right) {
    const leftText = String(left);
    const rightText = String(right);
    if (leftText.length !== rightText.length) return leftText.length > rightText.length ? 1 : -1;
    if (leftText === rightText) return 0;
    return leftText > rightText ? 1 : -1;
}

function compareVersions(left, right) {
    const a = parseVersion(left);
    const b = parseVersion(right);
    if (!a || !b) return 0;
    for (const key of ['major', 'minor', 'patch']) {
        const result = compareNumericIdentifiers(a[`${key}Text`], b[`${key}Text`]);
        if (result !== 0) return result;
    }
    if (a.prerelease.length === 0 && b.prerelease.length === 0) return 0;
    if (a.prerelease.length === 0) return 1;
    if (b.prerelease.length === 0) return -1;
    const length = Math.max(a.prerelease.length, b.prerelease.length);
    for (let index = 0; index < length; index += 1) {
        if (index >= a.prerelease.length) return -1;
        if (index >= b.prerelease.length) return 1;
        const leftPart = a.prerelease[index];
        const rightPart = b.prerelease[index];
        if (leftPart === rightPart) continue;
        const leftNumeric = /^\d+$/.test(leftPart);
        const rightNumeric = /^\d+$/.test(rightPart);
        if (leftNumeric && rightNumeric) {
            return compareNumericIdentifiers(leftPart, rightPart);
        }
        if (leftNumeric !== rightNumeric) return leftNumeric ? -1 : 1;
        return leftPart > rightPart ? 1 : -1;
    }
    return 0;
}

function normalizeNotes(notes) {
    if (typeof notes === 'string') return notes.slice(0, RELEASE_NOTES_LIMIT);
    if (!Array.isArray(notes)) return '';
    return notes
        .map((entry) => {
            if (typeof entry === 'string') return entry;
            if (!entry || typeof entry !== 'object') return '';
            const version = String(entry.version || '').trim();
            const note = String(entry.note || entry.releaseNotes || '').trim();
            return version && note ? `## v${version.replace(/^v/, '')}\n\n${note}` : note;
        })
        .filter(Boolean)
        .join('\n\n')
        .slice(0, RELEASE_NOTES_LIMIT);
}

function normalizeProgress(progress) {
    if (!progress || typeof progress !== 'object') return null;
    const percent = Number(progress.percent);
    return {
        percent: Number.isFinite(percent) ? Math.min(100, Math.max(0, percent)) : 0,
        transferred: Math.max(0, Number(progress.transferred) || 0),
        total: Math.max(0, Number(progress.total) || 0),
        bytesPerSecond: Math.max(0, Number(progress.bytesPerSecond) || 0),
    };
}

function normalizeError(error) {
    return {
        code: String(error?.code || 'UPDATE_ERROR').slice(0, 128),
        message: String(error?.message || error || '更新服务暂时不可用').slice(0, 500),
    };
}

function deriveUpdatePolicy(updateInfo, currentVersion) {
    const info = updateInfo && typeof updateInfo === 'object' ? updateInfo : {};
    const updateMode = info.updateMode === 'force' || info.mode === 'force' ? 'force' : 'optional';
    const minimumSupportedVersion = parseVersion(info.minimumSupportedVersion)
        ? String(info.minimumSupportedVersion).replace(/^v/, '')
        : null;
    const version = parseVersion(info.version) ? String(info.version).replace(/^v/, '') : null;
    const belowMinimum = Boolean(
        minimumSupportedVersion
        && parseVersion(currentVersion)
        && compareVersions(currentVersion, minimumSupportedVersion) < 0,
    );
    return {
        version,
        updateMode,
        minimumSupportedVersion,
        isForced: updateMode === 'force' || belowMinimum,
        releaseName: String(info.releaseName || '').slice(0, 300),
        releaseNotes: normalizeNotes(info.releaseNotes),
        releaseDate: info.releaseDate ? String(info.releaseDate).slice(0, 80) : null,
        updateMessage: String(info.updateMessage || info.message || '').slice(0, 500),
    };
}

function cloneState(state) {
    return JSON.parse(JSON.stringify(state));
}

function createInitialState({ currentVersion, platform, enabled, releaseUrl }) {
    return {
        status: enabled ? 'idle' : 'disabled',
        currentVersion: String(currentVersion || '0.0.0'),
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
        platform: String(platform || process.platform),
        canDownload: false,
        canInstall: false,
        releaseUrl: String(releaseUrl || DEFAULT_RELEASE_URL),
    };
}

function createUpdateManager(options = {}) {
    const env = options.env || process.env;
    const app = options.app || null;
    const currentVersion = String(
        options.appVersion
        || app?.getVersion?.()
        || app?.version
        || '0.0.0',
    );
    const isPackaged = options.isPackaged ?? Boolean(app?.isPackaged);
    const isSmokeTest = Boolean(options.isSmokeTest);
    const enabled = Boolean(
        isPackaged
        && ['win32', 'darwin'].includes(options.platform || process.platform)
        && !isSmokeTest
        && env.WORDTTS_DISABLE_AUTO_UPDATE !== '1'
        && options.disabled !== true,
    );
    const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();
    const send = typeof options.send === 'function' ? options.send : () => {};
    const releaseUrl = options.releaseUrl || DEFAULT_RELEASE_URL;
    let updater = options.autoUpdater || null;
    let state = createInitialState({
        currentVersion,
        platform: options.platform || process.platform,
        enabled,
        releaseUrl,
    });
    let disposed = false;
    let started = false;
    let initialTimer = null;
    let intervalTimer = null;
    let checkPromise = null;
    let updateInfo = null;
    const listeners = [];

    const publish = (patch) => {
        if (disposed) return;
        state = { ...state, ...patch };
        if (!UPDATE_STATUSES.has(state.status)) state.status = 'error';
        state.canDownload = state.status === 'available' || state.status === 'error'
            ? Boolean(updateInfo && state.version)
            : false;
        state.canInstall = state.status === 'downloaded';
        send(cloneState(state));
    };

    const publishInfo = (info, status = 'available') => {
        const policy = deriveUpdatePolicy(info, currentVersion);
        // electron-updater normally filters this for us, but a stale Release,
        // malformed provider response, or a delayed event must never turn an
        // older package into an installable update in the UI.
        if (!policy.version || compareVersions(policy.version, currentVersion) <= 0) return false;
        const sameUpdate = Boolean(
            state.version
            && policy.version
            && compareVersions(state.version, policy.version) === 0,
        );
        const preserveLifecycle = sameUpdate
            && ['downloading', 'downloaded', 'installing'].includes(state.status);
        updateInfo = info && typeof info === 'object' ? info : null;
        publish({
            // A periodic check can overlap with a long download or happen
            // after a package has already been downloaded. Keep that
            // installable lifecycle state when the server reports the same
            // version instead of making the user download it again.
            status: preserveLifecycle ? state.status : status,
            ...policy,
            latestVersion: policy.version,
            progress: preserveLifecycle ? state.progress : null,
            error: null,
            checkedAt: now(),
        });
    };

    const normalizeLatestVersion = (candidate) => {
        const normalizedCandidate = parseVersion(candidate)
            ? String(candidate).replace(/^v/, '')
            : null;
        const normalizedCurrent = parseVersion(currentVersion)
            ? String(currentVersion).replace(/^v/, '')
            : null;
        if (!normalizedCandidate || !normalizedCurrent) return normalizedCandidate || normalizedCurrent;
        return compareVersions(normalizedCandidate, normalizedCurrent) < 0
            ? normalizedCurrent
            : normalizedCandidate;
    };

    const clearUpdateInfo = (latestVersion = currentVersion) => {
        if (updateInfo && ['downloading', 'downloaded', 'installing'].includes(state.status)) {
            // Do not discard a package that is already being downloaded or
            // installed just because a later metadata check has no update.
            publish({
                error: null,
                checkedAt: now(),
                latestVersion: normalizeLatestVersion(latestVersion) || state.latestVersion,
            });
            return;
        }
        updateInfo = null;
        publish({
            status: 'up-to-date',
            version: null,
            isForced: false,
            updateMode: null,
            minimumSupportedVersion: null,
            releaseName: '',
            releaseNotes: '',
            releaseDate: null,
            updateMessage: '',
            progress: null,
            error: null,
            checkedAt: now(),
            // Keep the latest version visible on the version page even when
            // electron-updater returned no update event.
            latestVersion: normalizeLatestVersion(latestVersion),
        });
    };

    const publishError = (error) => {
        publish({ status: 'error', error: normalizeError(error), checkedAt: now() });
    };

    if (enabled && !updater) {
        try {
            // Lazy loading is important: development, smoke-test, and browser
            // renderer tests must not instantiate native updater backends.
            updater = require('electron-updater').autoUpdater;
        } catch (error) {
            publishError(error);
        }
    }

    function attach(eventName, handler) {
        if (!updater || typeof updater.on !== 'function') return;
        updater.on(eventName, handler);
        listeners.push([eventName, handler]);
    }

    if (enabled && updater) {
        updater.autoDownload = false;
        // Installation is an explicit action in the version center. This
        // also prevents closing the window from unexpectedly restarting the
        // app into a new version after an optional download.
        updater.autoInstallOnAppQuit = false;
        attach('checking-for-update', () => {
            if (updateInfo && ['downloading', 'downloaded', 'installing'].includes(state.status)) {
                publish({ error: null });
                return;
            }
            publish({
                status: 'checking',
                error: null,
                progress: null,
            });
        });
        attach('update-available', (info) => {
            if (!publishInfo(info)) {
                if (state.status === 'checking') clearUpdateInfo(info?.version);
            }
        });
        attach('update-not-available', (info) => {
            // Keep the server response available for diagnostics without
            // treating it as an installable update.
            clearUpdateInfo(info?.version);
        });
        attach('download-progress', (progress) => publish({
            status: 'downloading',
            progress: normalizeProgress(progress),
            error: null,
        }));
        attach('update-downloaded', () => publish({
            status: 'downloaded',
            progress: { percent: 100, transferred: state.progress?.total || 0, total: state.progress?.total || 0, bytesPerSecond: 0 },
            error: null,
        }));
        attach('error', (error) => publishError(error));
    }

    async function check() {
        if (!enabled || !updater || disposed) return getStatus();
        if (['downloading', 'downloaded', 'installing'].includes(state.status)) return getStatus();
        if (checkPromise) return checkPromise;
        publish({ status: 'checking', error: null, progress: null });
        checkPromise = Promise.resolve()
            .then(() => updater.checkForUpdates())
            .then((result) => {
                const resultInfo = result?.updateInfo || result?.versionInfo;
                if (resultInfo && state.status === 'checking') {
                    if (result?.isUpdateAvailable !== false
                        && compareVersions(resultInfo.version, currentVersion) > 0) {
                        publishInfo(resultInfo);
                    }
                    else clearUpdateInfo(resultInfo.version);
                } else if (state.status === 'checking') {
                    clearUpdateInfo();
                }
                return getStatus();
            })
            .catch((error) => {
                publishError(error);
                return getStatus();
            })
            .finally(() => {
                checkPromise = null;
            });
        return checkPromise;
    }

    async function download() {
        if (!enabled || !updater || disposed) return getStatus();
        if (!updateInfo || !state.version) return getStatus();
        if (state.status === 'downloaded' || state.status === 'downloading') return getStatus();
        publish({ status: 'downloading', progress: state.progress || null, error: null });
        try {
            await updater.downloadUpdate();
            if (state.status === 'downloading') {
                publish({
                    status: 'downloaded',
                    progress: { percent: 100, transferred: state.progress?.total || 0, total: state.progress?.total || 0, bytesPerSecond: 0 },
                    error: null,
                });
            }
        } catch (error) {
            publishError(error);
        }
        return getStatus();
    }

    async function install() {
        if (!enabled || !updater || disposed || state.status !== 'downloaded') return getStatus();
        publish({ status: 'installing', error: null });
        try {
            // Keep the app running after the installer when possible. The
            // explicit user action is still required, including for forced
            // updates, so closing the window cannot surprise the user.
            await Promise.resolve(updater.quitAndInstall(false, true));
        } catch (error) {
            publishError(error);
        }
        return getStatus();
    }

    function start({ delayMs = 6_000, intervalMs = 6 * 60 * 60 * 1_000 } = {}) {
        if (!enabled || !updater || started || disposed) return getStatus();
        started = true;
        const run = () => {
            if (disposed) return;
            void check();
            intervalTimer = setTimeout(run, Math.max(60_000, Number(intervalMs) || 6 * 60 * 60 * 1_000));
            intervalTimer.unref?.();
        };
        initialTimer = setTimeout(run, Math.max(0, Number(delayMs) || 0));
        initialTimer.unref?.();
        return getStatus();
    }

    function getStatus() {
        return cloneState(state);
    }

    function dispose() {
        if (disposed) return;
        disposed = true;
        if (initialTimer) clearTimeout(initialTimer);
        if (intervalTimer) clearTimeout(intervalTimer);
        listeners.forEach(([eventName, handler]) => updater?.removeListener?.(eventName, handler));
        listeners.length = 0;
    }

    return {
        check,
        download,
        install,
        start,
        getStatus,
        dispose,
        isEnabled: () => enabled,
    };
}

module.exports = {
    compareVersions,
    createUpdateManager,
    deriveUpdatePolicy,
    normalizeNotes,
    parseVersion,
};
