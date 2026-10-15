'use strict';

const {
    buildUpdateArtifactUrl,
    compareVersions,
    deriveUpdatePolicy,
    isValidUpdatePolicyMetadata,
    parseVersion,
    probeUpdateArtifact,
    updateArtifactForPlatform,
} = require('./update-manager');
const { normalizeWindowsMetadata } = require('./windows-update-client');
const { fetchTrustedReleaseUrl, isTrustedReleaseUrl } = require('./release-network');

const STARTUP_CHECK_TIMEOUT_MS = 20_000;
const METADATA_TIMEOUT_MS = 8_000;
const MAX_METADATA_BYTES = 1024 * 1024;
const RELEASE_CHECK_INTERVAL_MS = 30 * 60 * 1_000;

function serviceError(detail) {
    return Object.assign(new Error(
        '无法确认线上存在可用的当前版本或更高版本安装包，应用已停用。'
        + '\n请检查网络连接或联系服务提供方后重新打开软件。',
    ), {
        code: 'STARTUP_SERVICE_UNAVAILABLE',
        detail,
    });
}

function metadataUrls(releaseUrl, currentVersion, platform) {
    const base = new URL(releaseUrl);
    if (base.protocol !== 'https:' || !/^\/[^/]+\/[^/]+\/releases\/?$/.test(base.pathname)) {
        throw serviceError('发布地址无效。');
    }
    const releases = `${base.origin}${base.pathname.replace(/\/$/, '')}`;
    const name = platform === 'win32' ? 'latest-win.json' : 'latest-mac.yml';
    return [
        { url: `${releases}/latest/download/${name}`, expectedVersion: null },
        {
            url: `${releases}/download/${encodeURIComponent(`v${currentVersion}`)}/${name}`,
            expectedVersion: currentVersion,
        },
    ];
}

async function fetchMetadata(source, { fetchImpl, platform, signal }) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), METADATA_TIMEOUT_MS);
    const requestSignal = AbortSignal.any([signal, controller.signal]);
    let abort;
    const cancelled = new Promise((_, reject) => {
        abort = () => reject(requestSignal.reason || new Error('发布信息请求已取消。'));
        requestSignal.addEventListener('abort', abort, { once: true });
        if (requestSignal.aborted) abort();
    });
    const request = async () => {
        const response = await fetchTrustedReleaseUrl(source.url, {
            method: 'GET',
            cache: 'no-store',
            signal: requestSignal,
        }, fetchImpl);
        if (response?.url && !isTrustedReleaseUrl(source.url, response.url)) {
            await response.body?.cancel?.();
            throw new Error('线上发布信息跳转到未经授权的地址。');
        }
        if (!response?.ok) {
            await response?.body?.cancel?.();
            throw new Error(`发布信息请求失败（HTTP ${Number(response?.status) || 0}）。`);
        }
        const text = await readMetadataText(response);
        const info = platform === 'win32'
            ? normalizeWindowsMetadata(JSON.parse(text))
            : require('js-yaml').load(text);
        if (!info || typeof info !== 'object' || !parseVersion(info.version)) {
            throw new Error('发布信息缺少有效版本号。');
        }
        const version = String(info.version).trim().replace(/^v/i, '');
        if (source.expectedVersion && compareVersions(version, source.expectedVersion) !== 0) {
            throw new Error('当前版本的发布信息与安装版本不一致。');
        }
        const tag = String(info.tag || `v${version}`).trim();
        if (!parseVersion(tag) || compareVersions(tag, version) !== 0) {
            throw new Error('发布标签与安装包版本不一致。');
        }
        if (!isValidUpdatePolicyMetadata({ ...info, version })) {
            throw new Error('发布更新策略无效或最低支持版本高于当前发布版本。');
        }
        return { ...info, version, tag };
    };
    try {
        return await Promise.race([request(), cancelled]);
    } finally {
        clearTimeout(timeout);
        requestSignal.removeEventListener('abort', abort);
    }
}

async function readMetadataText(response) {
    const contentLength = Number(response.headers?.get?.('content-length'));
    if (Number.isFinite(contentLength) && contentLength > MAX_METADATA_BYTES) {
        await response.body?.cancel?.();
        throw new Error('线上发布信息超过允许大小。');
    }
    const reader = response.body?.getReader?.();
    if (!reader) {
        if (typeof response.text !== 'function') {
            throw new Error('线上发布信息响应无法安全读取。');
        }
        const text = await response.text();
        if (Buffer.byteLength(text, 'utf8') > MAX_METADATA_BYTES) {
            throw new Error('线上发布信息超过允许大小。');
        }
        return text;
    }
    const chunks = [];
    let totalBytes = 0;
    try {
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            totalBytes += value.byteLength;
            if (totalBytes > MAX_METADATA_BYTES) {
                await reader.cancel();
                throw new Error('线上发布信息超过允许大小。');
            }
            chunks.push(value);
        }
    } finally {
        reader.releaseLock?.();
    }
    const bytes = new Uint8Array(totalBytes);
    let offset = 0;
    for (const chunk of chunks) {
        bytes.set(chunk, offset);
        offset += chunk.byteLength;
    }
    return new TextDecoder().decode(bytes);
}

/**
 * This check deliberately does not use the auto-update enabled flag or a local
 * cache. Every packaged launch must prove a supported package is online before
 * main.js starts a backend, registers IPC, or creates any BrowserWindow.
 */
async function verifyStartupRelease({
    isPackaged,
    isSmokeTest = false,
    currentVersion,
    releaseUrl,
    platform = process.platform,
    architecture = process.arch,
    fetchImpl = globalThis.fetch,
    timeoutMs = STARTUP_CHECK_TIMEOUT_MS,
} = {}) {
    if (!isPackaged || isSmokeTest) return { required: false };
    if (!parseVersion(currentVersion) || !['win32', 'darwin'].includes(platform)
        || !['x64', 'arm64'].includes(architecture)
        || (platform === 'win32' && architecture !== 'x64')
        || typeof fetchImpl !== 'function') {
        throw serviceError('无法检查当前平台的线上安装包。');
    }
    const normalizedVersion = String(currentVersion).trim().replace(/^v/i, '');
    let sources;
    try {
        sources = metadataUrls(releaseUrl, normalizedVersion, platform);
    } catch (error) {
        throw serviceError(error.message);
    }

    const controller = new AbortController();
    let timeout;
    const deadline = new Promise((_, reject) => {
        timeout = setTimeout(() => {
            controller.abort();
            reject(serviceError('线上安装包检查超时。'));
        }, Math.max(1, Number(timeoutMs) || STARTUP_CHECK_TIMEOUT_MS));
    });
    const check = async () => {
        // A removed latest release can leave an older release at /latest.
        // Check the installed tag as well so neither an older version nor a
        // missing latest platform asset is mistaken for a valid replacement.
        const results = await Promise.allSettled(sources.map(source => fetchMetadata(source, {
            fetchImpl,
            platform,
            signal: controller.signal,
        })));
        const failures = results.filter(result => result.status === 'rejected')
            .map(result => result.reason?.message || String(result.reason));
        const candidates = results.filter(result => result.status === 'fulfilled')
            .map(result => result.value)
            .filter(info => compareVersions(info.version, normalizedVersion) >= 0)
            .sort((a, b) => compareVersions(b.version, a.version));
        const latestMetadataAvailable = results[0]?.status === 'fulfilled';
        const visited = new Set();
        for (const info of candidates) {
            if (controller.signal.aborted) throw serviceError('线上安装包检查已取消。');
            const artifact = updateArtifactForPlatform(info, platform, architecture);
            const url = artifact && buildUpdateArtifactUrl(releaseUrl, info.tag, artifact.url);
            if (!url || visited.has(url)) continue;
            visited.add(url);
            const result = await probeUpdateArtifact(url, {
                fetchImpl,
                signal: controller.signal,
                expectedSize: artifact.size,
            });
            if (result.available) {
                return {
                    required: true,
                    latestMetadataAvailable,
                    version: info.version,
                    artifactUrl: url,
                    updateAvailable: compareVersions(info.version, normalizedVersion) > 0,
                    updateInfo: info,
                };
            }
            failures.push(`${info.version}: ${result.status || result.error?.message || result.reason}`);
            if (compareVersions(info.version, normalizedVersion) > 0
                && deriveUpdatePolicy(info, normalizedVersion).isForced) {
                throw serviceError(`强制更新包不可用。\n${failures.join('\n')}`);
            }
        }
        throw serviceError(failures.join('\n') || '没有适用于当前平台和架构的当前版本或更高版本安装包。');
    };
    try {
        return await Promise.race([check(), deadline]);
    } finally {
        clearTimeout(timeout);
        controller.abort();
    }
}

function createReleaseMonitor({
    check,
    onFailure,
    setTimeoutImpl = setTimeout,
    clearTimeoutImpl = clearTimeout,
} = {}) {
    let disposed = false;
    let timer = null;
    const schedule = () => {
        if (disposed) return;
        timer = setTimeoutImpl(async () => {
            timer = null;
            try {
                await check();
                schedule();
            } catch (error) {
                if (disposed) return;
                disposed = true;
                await onFailure(error);
            }
        }, RELEASE_CHECK_INTERVAL_MS);
        timer?.unref?.();
    };
    // Startup has already checked once. The first in-app check is 30 minutes
    // later, and another timer is scheduled only after that check completes.
    schedule();
    return {
        dispose() {
            disposed = true;
            if (timer !== null) clearTimeoutImpl(timer);
            timer = null;
        },
    };
}

module.exports = { createReleaseMonitor, RELEASE_CHECK_INTERVAL_MS, verifyStartupRelease };
