'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const fsp = fs.promises;
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');
const { once } = require('node:events');
const { finished } = require('node:stream/promises');
const { spawn } = require('node:child_process');
const {
    computeOperations,
    OperationKind,
} = require('./node_modules/electron-updater/out/differentialDownloader/downloadPlanBuilder');

const DEFAULT_METADATA_NAME = 'latest-win.json';
const DEFAULT_BLOCKMAP_SUFFIX = '.blockmap';
const UPDATE_ERROR_LIMIT = 500;
const UPDATE_METADATA_TIMEOUT_MS = 15_000;
const BLOCKMAP_MAX_BYTES = 8 * 1024 * 1024;
const DIFFERENTIAL_FALLBACK_THRESHOLD = 0.95;
const NUMERIC_IDENTIFIER = '(?:0|[1-9]\\d*)';
const NON_NUMERIC_IDENTIFIER = '(?:[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)';
const PRERELEASE_IDENTIFIER = `(?:${NUMERIC_IDENTIFIER}|${NON_NUMERIC_IDENTIFIER})`;
const BUILD_IDENTIFIER = '[0-9A-Za-z-]+';
const VERSION_PATTERN = new RegExp(
    `^${NUMERIC_IDENTIFIER}\\.${NUMERIC_IDENTIFIER}\\.${NUMERIC_IDENTIFIER}`
    + `(?:-${PRERELEASE_IDENTIFIER}(?:\\.${PRERELEASE_IDENTIFIER})*)?`
    + `(?:\\+${BUILD_IDENTIFIER}(?:\\.${BUILD_IDENTIFIER})*)?$`,
);

function escapeRegExp(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function normalizeError(error, fallback = 'Windows 更新服务暂时不可用') {
    return {
        code: String(error?.code || 'WINDOWS_UPDATE_ERROR').slice(0, 128),
        message: String(error?.message || error || fallback).slice(0, UPDATE_ERROR_LIMIT),
    };
}

function clientDisposedError() {
    return Object.assign(new Error('更新客户端已关闭。'), { code: 'UPDATE_CLIENT_DISPOSED' });
}

function waitForChildSpawn(child) {
    if (!child || typeof child.once !== 'function') return Promise.resolve(child);
    return new Promise((resolve, reject) => {
        let settled = false;
        const onSpawn = () => {
            if (settled) return;
            settled = true;
            resolve(child);
        };
        const onError = error => {
            if (settled) return;
            settled = true;
            child.removeListener?.('spawn', onSpawn);
            reject(error);
        };
        child.once('spawn', onSpawn);
        // Keep this listener attached after a successful spawn too. A later
        // child-process error must not terminate the Electron host process.
        child.once('error', onError);
    });
}

function buildWindowsMetadataUrl(releaseUrl, metadataName = DEFAULT_METADATA_NAME) {
    try {
        const url = new URL(String(releaseUrl || ''));
        const releaseIndex = url.pathname.indexOf('/releases');
        if (releaseIndex < 0) return null;
        const repositoryPath = url.pathname.slice(0, releaseIndex);
        return `${url.origin}${repositoryPath}/releases/latest/download/${encodeURIComponent(metadataName)}`;
    } catch (_) {
        return null;
    }
}

function buildWindowsArtifactUrl(releaseUrl, tag, artifactName) {
    const rawName = String(artifactName || '').trim();
    if (!rawName) return null;
    if (/^https?:\/\//i.test(rawName)) return rawName;
    if (/[\\/]/.test(rawName.split(/[?#]/, 1)[0])) return null;
    try {
        const releasesUrl = new URL(String(releaseUrl || ''));
        const releaseIndex = releasesUrl.pathname.indexOf('/releases');
        if (releaseIndex < 0) return null;
        const repositoryPath = releasesUrl.pathname.slice(0, releaseIndex);
        const filePath = rawName
            .split(/[?#]/, 1)[0]
            .split('/')
            .filter(Boolean)
            .map(segment => encodeURIComponent(decodeURIComponent(segment)))
            .join('/');
        const releaseTag = encodeURIComponent(String(tag || '').trim());
        if (!repositoryPath || !releaseTag || !filePath) return null;
        return `${releasesUrl.origin}${repositoryPath}/releases/download/${releaseTag}/${filePath}`;
    } catch (_) {
        return null;
    }
}

function buildWindowsBlockMapUrl(releaseUrl, tag, artifactName) {
    const blockMapReference = appendBlockMapSuffix(artifactName);
    if (!blockMapReference) return null;
    return buildWindowsArtifactUrl(releaseUrl, tag, blockMapReference);
}

function appendBlockMapSuffix(reference) {
    const raw = String(reference || '').trim();
    if (!raw) return null;
    if (/^https?:\/\//i.test(raw)) {
        try {
            const url = new URL(raw);
            if (!url.pathname.toLowerCase().endsWith(DEFAULT_BLOCKMAP_SUFFIX)) {
                url.pathname = `${url.pathname}${DEFAULT_BLOCKMAP_SUFFIX}`;
            }
            return url.toString();
        } catch (_) {
            return null;
        }
    }
    const match = raw.match(/^([^?#]*)([?#].*)?$/);
    const name = match?.[1] || '';
    if (!name) return null;
    return name.toLowerCase().endsWith(DEFAULT_BLOCKMAP_SUFFIX)
        ? raw
        : `${name}${DEFAULT_BLOCKMAP_SUFFIX}${match?.[2] || ''}`;
}

function replaceArtifactVersion(reference, fromVersion, toVersion) {
    const from = String(fromVersion || '').trim().replace(/^v/i, '');
    const to = String(toVersion || '').trim().replace(/^v/i, '');
    const raw = String(reference || '').trim();
    if (!from || !to || !raw) return null;
    const name = updateArtifactFileName(raw);
    const pattern = new RegExp(`-${escapeRegExp(from)}-x64\\.exe(?:\\.blockmap)?$`, 'i');
    const replacedName = name.replace(pattern, `-${to}-x64.exe${name.toLowerCase().endsWith(DEFAULT_BLOCKMAP_SUFFIX) ? DEFAULT_BLOCKMAP_SUFFIX : ''}`);
    if (replacedName === name) return null;
    if (/^https?:\/\//i.test(raw)) {
        try {
            const url = new URL(raw);
            const segments = url.pathname.split('/');
            segments[segments.length - 1] = replacedName;
            url.pathname = segments.join('/');
            return url.toString();
        } catch (_) {
            return null;
        }
    }
    return replacedName;
}

function updateArtifactFileName(value) {
    const raw = String(value || '').trim().split(/[?#]/, 1)[0];
    if (!raw) return '';
    const encodedName = raw.split('/').filter(Boolean).pop() || raw;
    try {
        return decodeURIComponent(encodedName);
    } catch (_) {
        return encodedName;
    }
}

function isSafeArtifactReference(value) {
    const raw = String(value || '').trim().split(/[?#]/, 1)[0];
    if (!raw) return false;
    // Release metadata generated by this project uses a single asset name.
    // Keep absolute HTTP(S) URLs for backwards compatibility, but never let
    // a relative path escape the release asset namespace.
    if (/^https?:\/\//i.test(raw)) return true;
    return !/[\\/]/.test(raw);
}

function normalizeBlockMapReference(value) {
    const raw = typeof value === 'object' && value !== null
        ? value.url
        : value;
    const reference = String(raw || '').trim();
    if (!reference || !isSafeArtifactReference(reference)) return '';
    return reference.toLowerCase().split(/[?#]/, 1)[0].endsWith(DEFAULT_BLOCKMAP_SUFFIX)
        ? reference
        : '';
}

function normalizeWindowsMetadata(payload) {
    if (!payload || typeof payload !== 'object') {
        throw Object.assign(new Error('更新元数据格式不正确。'), { code: 'INVALID_UPDATE_METADATA' });
    }
    const version = String(payload.version || '').trim().replace(/^v/i, '');
    const tag = String(payload.tag || `v${version}`).trim();
    const normalizedTag = tag.replace(/^v/i, '');
    const expectedArtifactName = VERSION_PATTERN.test(version)
        ? new RegExp(
            '^(?:wordTTS|小猪wordTTS)-Setup-' + escapeRegExp(version) + '-x64\\.exe$',
            'i',
        )
        : null;
    const rawArtifacts = [
        payload.artifact,
        ...(Array.isArray(payload.files) ? payload.files : []),
        payload.path ? { url: payload.path, sha512: payload.sha512, size: payload.size } : null,
    ].filter(file => file && typeof file === 'object');
    const artifact = rawArtifacts
        .map(file => {
            const blockmap = normalizeBlockMapReference(file.blockmap || payload.blockmap);
            return {
                url: String(file.url || '').trim(),
                sha512: String(file.sha512 || payload.sha512 || '').trim(),
                size: Number(file.size ?? payload.size),
                ...(blockmap ? { blockmap } : {}),
            };
        })
        .find(file => {
            const artifactName = updateArtifactFileName(file.url);
            return Boolean(
                expectedArtifactName
                && expectedArtifactName.test(artifactName)
                && isSafeArtifactReference(file.url)
                && file.url.toLowerCase().split(/[?#]/, 1)[0].endsWith('.exe')
                && file.sha512
                && Number.isSafeInteger(file.size)
                && file.size > 0,
            );
        });
    if (!VERSION_PATTERN.test(version)
        || (payload.platform !== undefined && payload.platform !== 'win32')
        || !artifact
        || !VERSION_PATTERN.test(normalizedTag)
        || normalizedTag !== version
    ) {
        throw Object.assign(new Error('更新元数据缺少有效的 Windows 安装包信息。'), { code: 'INVALID_UPDATE_METADATA' });
    }
    return {
        ...payload,
        version,
        files: [artifact],
        artifact,
        path: artifact.url,
        sha512: artifact.sha512,
        size: artifact.size,
        tag,
    };
}

function sameWindowsMetadata(left, right) {
    return Boolean(
        left
        && right
        && left.version === right.version
        && left.path === right.path
        && left.sha512 === right.sha512
        && Number(left.size) === Number(right.size),
    );
}

async function fetchJson(url, fetchImpl = globalThis.fetch, timeoutMs = UPDATE_METADATA_TIMEOUT_MS) {
    if (!url || typeof fetchImpl !== 'function') {
        throw Object.assign(new Error('更新地址不可用。'), { code: 'UPDATE_METADATA_UNAVAILABLE' });
    }
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timeout = setTimeout(
        () => controller?.abort(),
        Math.max(500, Number(timeoutMs) || UPDATE_METADATA_TIMEOUT_MS),
    );
    timeout.unref?.();
    try {
        const response = await fetchImpl(url, {
            method: 'GET',
            redirect: 'follow',
            cache: 'no-store',
            ...(controller ? { signal: controller.signal } : {}),
        });
        if (!response || !response.ok) {
            const error = new Error(`更新元数据请求失败（HTTP ${Number(response?.status) || 0}）。`);
            error.code = 'UPDATE_METADATA_HTTP_ERROR';
            throw error;
        }
        return normalizeWindowsMetadata(await response.json());
    } finally {
        clearTimeout(timeout);
    }
}

function writeChunk(stream, chunk, streamFailure = null) {
    if (stream.write(chunk)) return Promise.resolve();
    const drain = once(stream, 'drain').then(() => undefined);
    return streamFailure ? Promise.race([drain, streamFailure]) : drain;
}

function closeWriteStream(stream, finishedPromise = null) {
    stream.end();
    return finishedPromise || new Promise((resolve, reject) => {
        stream.once('error', reject);
        stream.once('close', resolve);
    });
}

async function verifyFileIntegrity(filePath, expected, fsImpl = fs, fsPromisesImpl = fsp) {
    let stat;
    try {
        stat = await fsPromisesImpl.stat(filePath);
    } catch (error) {
        throw error;
    }
    if (!stat.isFile()) {
        throw Object.assign(new Error('更新安装包不是有效文件。'), { code: 'UPDATE_PACKAGE_INVALID' });
    }
    if (Number(expected.size) > 0 && stat.size !== Number(expected.size)) {
        throw Object.assign(new Error('更新安装包大小校验失败。'), { code: 'UPDATE_SIZE_MISMATCH' });
    }
    const hash = crypto.createHash('sha512');
    const stream = fsImpl.createReadStream(filePath);
    try {
        for await (const chunk of stream) hash.update(chunk);
    } catch (error) {
        stream.destroy();
        throw error;
    }
    const sha512 = hash.digest('base64');
    if (sha512 !== expected.sha512) {
        throw Object.assign(new Error('更新安装包校验失败，文件可能已损坏。'), { code: 'UPDATE_CHECKSUM_MISMATCH' });
    }
    return { size: stat.size, sha512 };
}

async function downloadResponseToFile(response, destination, expectedSize, onProgress, fsImpl = fs) {
    const file = fsImpl.createWriteStream(destination);
    // Start watching the writable stream before the first chunk is written.
    // A disk-full/permission error can otherwise happen between two network
    // chunks and become an unhandled `error` event while the downloader is
    // waiting on the response body.
    const fileFinished = finished(file, { cleanup: true });
    const streamFailure = fileFinished.then(
        () => new Promise(() => {}),
        error => Promise.reject(error),
    );
    streamFailure.catch(() => {});
    const hash = crypto.createHash('sha512');
    const startedAt = Date.now();
    let transferred = 0;
    const totalFromHeaders = Number(response?.headers?.get?.('content-length')) || 0;
    const total = expectedSize > 0 ? expectedSize : totalFromHeaders;
    const emitProgress = () => {
        const elapsedSeconds = Math.max(0.001, (Date.now() - startedAt) / 1000);
        const percent = total > 0 ? Math.min(99, (transferred / total) * 100) : 0;
        try {
            onProgress?.({
                percent,
                transferred,
                total,
                bytesPerSecond: transferred / elapsedSeconds,
            });
        } catch (_) {
            // A progress listener is UI plumbing; it must not invalidate a
            // successfully written update package.
        }
    };
    try {
        if (response?.body?.getReader) {
            const reader = response.body.getReader();
            while (true) {
                const part = await Promise.race([reader.read(), streamFailure]);
                if (part.done) break;
                const chunk = Buffer.from(part.value);
                hash.update(chunk);
                transferred += chunk.length;
                await writeChunk(file, chunk, streamFailure);
                emitProgress();
            }
        } else if (response?.body && Symbol.asyncIterator in Object(response.body)) {
            const reader = response.body[Symbol.asyncIterator]();
            while (true) {
                const part = await Promise.race([reader.next(), streamFailure]);
                if (part.done) break;
                const chunk = Buffer.from(part);
                hash.update(chunk);
                transferred += chunk.length;
                await writeChunk(file, chunk, streamFailure);
                emitProgress();
            }
        } else if (typeof response?.arrayBuffer === 'function') {
            const chunk = Buffer.from(await response.arrayBuffer());
            hash.update(chunk);
            transferred = chunk.length;
            await writeChunk(file, chunk, streamFailure);
            emitProgress();
        } else {
            throw Object.assign(new Error('更新下载响应没有可读取的内容。'), { code: 'UPDATE_DOWNLOAD_EMPTY' });
        }
        await closeWriteStream(file, fileFinished);
    } catch (error) {
        file.destroy();
        await fileFinished.catch(() => {});
        throw error;
    }
    return {
        transferred,
        total,
        sha512: hash.digest('base64'),
    };
}

async function responseToBuffer(response, maxBytes = BLOCKMAP_MAX_BYTES) {
    const chunks = [];
    let total = 0;
    const append = value => {
        const chunk = Buffer.from(value || []);
        total += chunk.length;
        if (total > maxBytes) {
            throw Object.assign(new Error('更新差分索引超过允许大小。'), { code: 'UPDATE_BLOCKMAP_TOO_LARGE' });
        }
        chunks.push(chunk);
    };
    if (response?.body?.getReader) {
        const reader = response.body.getReader();
        while (true) {
            const part = await reader.read();
            if (part.done) break;
            append(part.value);
        }
    } else if (response?.body && Symbol.asyncIterator in Object(response.body)) {
        const reader = response.body[Symbol.asyncIterator]();
        while (true) {
            const part = await reader.next();
            if (part.done) break;
            append(part.value);
        }
    } else if (typeof response?.arrayBuffer === 'function') {
        append(await response.arrayBuffer());
    } else {
        throw Object.assign(new Error('更新差分索引没有可读取的内容。'), { code: 'UPDATE_BLOCKMAP_EMPTY' });
    }
    return Buffer.concat(chunks, total);
}

function validateBlockMap(blockMap, expectedSize, label) {
    if (!blockMap || typeof blockMap !== 'object' || !Array.isArray(blockMap.files) || blockMap.files.length !== 1) {
        throw Object.assign(new Error(`${label}格式不正确。`), { code: 'UPDATE_BLOCKMAP_INVALID' });
    }
    const file = blockMap.files[0];
    if (!file || !Array.isArray(file.sizes) || !Array.isArray(file.checksums)
        || file.sizes.length === 0 || file.sizes.length !== file.checksums.length) {
        throw Object.assign(new Error(`${label}缺少有效分块信息。`), { code: 'UPDATE_BLOCKMAP_INVALID' });
    }
    const offset = Number(file.offset || 0);
    if (!Number.isSafeInteger(offset) || offset < 0) {
        throw Object.assign(new Error(`${label}起始位置不正确。`), { code: 'UPDATE_BLOCKMAP_INVALID' });
    }
    const payloadSize = file.sizes.reduce((sum, value) => {
        const size = Number(value);
        if (!Number.isSafeInteger(size) || size <= 0) {
            throw Object.assign(new Error(`${label}包含无效分块大小。`), { code: 'UPDATE_BLOCKMAP_INVALID' });
        }
        return sum + size;
    }, 0);
    if (!Number.isSafeInteger(payloadSize) || offset + payloadSize !== Number(expectedSize)) {
        throw Object.assign(new Error(`${label}与安装包大小不一致。`), { code: 'UPDATE_BLOCKMAP_INVALID' });
    }
    if (file.checksums.some(value => typeof value !== 'string' || value.length === 0)) {
        throw Object.assign(new Error(`${label}包含无效分块校验值。`), { code: 'UPDATE_BLOCKMAP_INVALID' });
    }
    return blockMap;
}

async function fetchBlockMap(url, fetchImpl, signal = null) {
    const response = await fetchImpl(url, {
        method: 'GET',
        redirect: 'follow',
        cache: 'no-store',
        ...(signal ? { signal } : {}),
    });
    if (!response || !response.ok) {
        throw Object.assign(new Error(`更新差分索引请求失败（HTTP ${Number(response?.status) || 0}）。`), {
            code: 'UPDATE_BLOCKMAP_HTTP_ERROR',
        });
    }
    let bytes = await responseToBuffer(response);
    try {
        bytes = zlib.gunzipSync(bytes);
    } catch (error) {
        // A few self-hosted mirrors serve the JSON sidecar uncompressed. Keep
        // accepting that form, but reject arbitrary binary data below.
        if (bytes[0] !== 0x7b && bytes[0] !== 0x5b) {
            throw Object.assign(new Error('更新差分索引解压失败。'), {
                code: 'UPDATE_BLOCKMAP_INVALID',
                cause: error,
            });
        }
    }
    try {
        return JSON.parse(bytes.toString('utf8'));
    } catch (error) {
        throw Object.assign(new Error('更新差分索引解析失败。'), {
            code: 'UPDATE_BLOCKMAP_INVALID',
            cause: error,
        });
    }
}

async function fetchRange(url, start, end, fetchImpl, signal = null) {
    if (end <= start) return Buffer.alloc(0);
    const response = await fetchImpl(url, {
        method: 'GET',
        redirect: 'follow',
        headers: { Range: `bytes=${start}-${end - 1}` },
        ...(signal ? { signal } : {}),
    });
    if (!response || Number(response.status) !== 206) {
        throw Object.assign(new Error('更新服务器不支持分块下载。'), { code: 'UPDATE_RANGE_UNSUPPORTED' });
    }
    const bytes = await responseToBuffer(response, end - start);
    if (bytes.length !== end - start) {
        throw Object.assign(new Error('更新分块大小校验失败。'), { code: 'UPDATE_RANGE_SIZE_MISMATCH' });
    }
    return bytes;
}

async function readLocalRange(handle, start, end) {
    const length = end - start;
    if (length <= 0) return Buffer.alloc(0);
    const buffer = Buffer.allocUnsafe(length);
    let position = 0;
    while (position < length) {
        const result = await handle.read(buffer, position, length - position, start + position);
        if (!result || result.bytesRead <= 0) break;
        position += result.bytesRead;
    }
    if (position !== length) {
        throw Object.assign(new Error('本地安装包分块读取失败。'), { code: 'UPDATE_LOCAL_RANGE_MISMATCH' });
    }
    return buffer;
}

async function writeDifferentialFile({
    oldPath,
    destination,
    newUrl,
    operations,
    downloadSize,
    onProgress,
    fetchImpl,
    fsImpl,
    fsPromisesImpl,
    signal,
}) {
    const oldHandle = await fsPromisesImpl.open(oldPath, 'r');
    const file = fsImpl.createWriteStream(destination);
    const fileFinished = finished(file, { cleanup: true });
    const streamFailure = fileFinished.then(
        () => new Promise(() => {}),
        error => Promise.reject(error),
    );
    streamFailure.catch(() => {});
    const startedAt = Date.now();
    let transferred = 0;
    const emitProgress = () => {
        const elapsedSeconds = Math.max(0.001, (Date.now() - startedAt) / 1000);
        try {
            onProgress?.({
                percent: downloadSize > 0 ? Math.min(99, (transferred / downloadSize) * 100) : 100,
                transferred,
                total: downloadSize,
                bytesPerSecond: transferred / elapsedSeconds,
            });
        } catch (_) {
            // UI progress is deliberately non-fatal.
        }
    };
    emitProgress();
    try {
        for (const operation of operations) {
            if (signal?.aborted) throw Object.assign(new Error('更新下载已取消。'), { name: 'AbortError' });
            const bytes = operation.kind === OperationKind.COPY
                ? await readLocalRange(oldHandle, operation.start, operation.end)
                : await fetchRange(newUrl, operation.start, operation.end, fetchImpl, signal);
            await writeChunk(file, bytes, streamFailure);
            if (operation.kind === OperationKind.DOWNLOAD) transferred += bytes.length;
            emitProgress();
        }
        await closeWriteStream(file, fileFinished);
    } catch (error) {
        file.destroy();
        await fileFinished.catch(() => {});
        throw error;
    } finally {
        await oldHandle.close().catch(() => {});
    }
    return { transferred, total: downloadSize };
}

function blockMapReferenceForArtifact(artifact) {
    const explicit = normalizeBlockMapReference(artifact?.blockmap);
    if (explicit) return explicit;
    const rawArtifact = String(artifact?.url || '').trim();
    return appendBlockMapSuffix(/^https?:\/\//i.test(rawArtifact)
        ? rawArtifact
        : updateArtifactFileName(rawArtifact));
}

function createWindowsUpdateClient(options = {}) {
    const releaseUrl = options.releaseUrl || '';
    const metadataUrl = options.metadataUrl || buildWindowsMetadataUrl(releaseUrl);
    const fetchImpl = options.fetchImpl || globalThis.fetch;
    const fsImpl = options.fs || fs;
    const fspImpl = options.fsPromises || fsImpl.promises || fsp;
    const tempDirectory = options.tempDirectory || os.tmpdir();
    const spawnImpl = options.spawn || spawn;
    const app = options.app || null;
    const environment = options.environment || process.env;
    const currentVersion = String(options.currentVersion || '').trim().replace(/^v/i, '');
    const useDifferential = options.useDifferential !== false;
    const logger = options.logger || {
        info() {},
        warn() {},
        debug: null,
    };
    const metadataTimeoutMs = options.metadataTimeoutMs;
    let latestInfo = null;
    let downloadedPath = null;
    let downloadedInfo = null;
    let disposed = false;
    let activeDownloadController = null;

    async function clearDownloadedFile() {
        const stalePath = downloadedPath;
        downloadedPath = null;
        downloadedInfo = null;
        if (stalePath) await fspImpl.rm(stalePath, { force: true }).catch(() => {});
    }

    async function tryDifferentialDownload(normalized, artifact, url, destination, onProgress, signal) {
        if (!useDifferential || !VERSION_PATTERN.test(currentVersion)
            || currentVersion === normalized.version || !url) return null;
        const installedExecutable = app?.getPath?.('exe');
        const oldPath = options.currentInstallerPath
            || (installedExecutable
                ? path.join(path.dirname(installedExecutable), '小猪wordTTS-uninstaller.exe')
                : null);
        if (!oldPath) return null;

        let oldStat;
        try {
            oldStat = await fspImpl.stat(oldPath);
        } catch (_) {
            return null;
        }
        if (!oldStat.isFile() || oldStat.size <= 0) return null;

        const oldArtifactReference = replaceArtifactVersion(artifact.url, normalized.version, currentVersion);
        const newBlockMapReference = blockMapReferenceForArtifact(artifact);
        const oldBlockMapReference = replaceArtifactVersion(
            newBlockMapReference,
            normalized.version,
            currentVersion,
        );
        if (!oldArtifactReference || !oldBlockMapReference) return null;
        const newBlockMapUrl = buildWindowsArtifactUrl(
            releaseUrl,
            normalized.tag,
            newBlockMapReference,
        );
        const oldBlockMapUrl = buildWindowsArtifactUrl(
            releaseUrl,
            `v${currentVersion}`,
            oldBlockMapReference,
        );
        if (!newBlockMapUrl || !oldBlockMapUrl) return null;

        try {
            const [oldBlockMap, newBlockMap] = await Promise.all([
                fetchBlockMap(oldBlockMapUrl, fetchImpl, signal),
                fetchBlockMap(newBlockMapUrl, fetchImpl, signal),
            ]);
            validateBlockMap(oldBlockMap, oldStat.size, '旧版本差分索引');
            validateBlockMap(newBlockMap, artifact.size, '新版本差分索引');
            const operations = computeOperations(oldBlockMap, newBlockMap, logger);
            const downloadSize = operations
                .filter(operation => operation.kind === OperationKind.DOWNLOAD)
                .reduce((sum, operation) => sum + operation.end - operation.start, 0);
            // A nearly-full set of ranges costs more round trips than one
            // sequential download. The blockmap lookup is cheap, so choose
            // the full path whenever differential saving is negligible.
            if (downloadSize >= Number(artifact.size) * DIFFERENTIAL_FALLBACK_THRESHOLD) {
                return null;
            }
            logger.info?.(`Windows 差分更新：下载 ${downloadSize}/${artifact.size} bytes`);
            await writeDifferentialFile({
                oldPath,
                destination,
                newUrl: url,
                operations,
                downloadSize,
                onProgress,
                fetchImpl,
                fsImpl,
                fsPromisesImpl: fspImpl,
                signal,
            });
            await verifyFileIntegrity(destination, artifact, fsImpl, fspImpl);
            return destination;
        } catch (error) {
            await fspImpl.rm(destination, { force: true }).catch(() => {});
            if (error?.name === 'AbortError') throw error;
            // Blockmaps are an optimization, not a release prerequisite for
            // already-installed clients. Missing sidecars, mirrors without
            // Range support, or a stale local uninstaller all fall back to the
            // existing full Setup download.
            logger.debug?.(`Windows 差分更新不可用，回退全量下载：${error?.message || error}`);
            return null;
        }
    }

    async function check() {
        if (disposed) throw clientDisposedError();
        latestInfo = await fetchJson(metadataUrl, fetchImpl, metadataTimeoutMs);
        return latestInfo;
    }

    async function download(info, onProgress) {
        if (disposed) throw clientDisposedError();
        const normalized = normalizeWindowsMetadata(info || latestInfo);
        if (downloadedPath && downloadedInfo && sameWindowsMetadata(downloadedInfo, normalized)) {
            try {
                await verifyFileIntegrity(downloadedPath, normalized.files[0], fsImpl, fspImpl);
                return downloadedPath;
            } catch (_) {
                await clearDownloadedFile();
            }
        }
        await clearDownloadedFile();
        const artifact = normalized.files[0];
        const url = buildWindowsArtifactUrl(releaseUrl, normalized.tag, artifact.url);
        if (!url) throw Object.assign(new Error('无法生成 Windows 更新下载地址。'), { code: 'UPDATE_ARTIFACT_URL_INVALID' });
        await fspImpl.mkdir(tempDirectory, { recursive: true });
        const destination = path.join(tempDirectory, `wordtts-update-${normalized.version}-${Date.now()}-${crypto.randomBytes(4).toString('hex')}.exe`);
        const downloadController = typeof AbortController === 'function' ? new AbortController() : null;
        activeDownloadController = downloadController;
        let committed = false;
        try {
            const differentialPath = await tryDifferentialDownload(
                normalized,
                artifact,
                url,
                destination,
                onProgress,
                downloadController?.signal,
            );
            if (differentialPath) {
                if (disposed) throw clientDisposedError();
                downloadedPath = differentialPath;
                downloadedInfo = normalized;
                committed = true;
                try {
                    onProgress?.({ percent: 100, transferred: Number(artifact.size), total: Number(artifact.size), bytesPerSecond: 0 });
                } catch (_) {
                    // UI callbacks are non-fatal.
                }
                if (disposed) {
                    await clearDownloadedFile();
                    throw clientDisposedError();
                }
                return differentialPath;
            }
            const response = await fetchImpl(url, {
                method: 'GET',
                redirect: 'follow',
                ...(downloadController ? { signal: downloadController.signal } : {}),
            });
            if (!response || !response.ok) {
                throw Object.assign(new Error(`更新安装包下载失败（HTTP ${Number(response?.status) || 0}）。`), { code: 'UPDATE_DOWNLOAD_HTTP_ERROR' });
            }
            const result = await downloadResponseToFile(response, destination, Number(artifact.size) || 0, onProgress, fsImpl);
            if (Number(artifact.size) > 0 && result.transferred !== Number(artifact.size)) {
                throw Object.assign(new Error('更新安装包大小校验失败。'), { code: 'UPDATE_SIZE_MISMATCH' });
            }
            if (result.sha512 !== artifact.sha512) {
                throw Object.assign(new Error('更新安装包校验失败，文件可能已损坏。'), { code: 'UPDATE_CHECKSUM_MISMATCH' });
            }
            if (disposed) throw clientDisposedError();
            downloadedPath = destination;
            downloadedInfo = normalized;
            committed = true;
            try {
                onProgress?.({ percent: 100, transferred: result.transferred, total: result.total || result.transferred, bytesPerSecond: 0 });
            } catch (_) {
                // See emitProgress: UI callbacks cannot turn a valid download into
                // a failed operation.
            }
            if (disposed) {
                await clearDownloadedFile();
                throw clientDisposedError();
            }
            return destination;
        } catch (error) {
            if (!committed) await fspImpl.rm(destination, { force: true }).catch(() => {});
            if (disposed && error?.name === 'AbortError') throw clientDisposedError();
            throw error;
        } finally {
            if (activeDownloadController === downloadController) activeDownloadController = null;
        }
    }

    async function install(info = latestInfo) {
        if (disposed) throw clientDisposedError();
        if (!downloadedPath || !downloadedInfo) throw Object.assign(new Error('更新安装包还没有下载完成。'), { code: 'UPDATE_NOT_DOWNLOADED' });
        const normalized = normalizeWindowsMetadata(info || downloadedInfo);
        if (!sameWindowsMetadata(downloadedInfo, normalized)) {
            throw Object.assign(new Error('更新信息已变化，请重新下载最新安装包。'), { code: 'UPDATE_METADATA_MISMATCH' });
        }
        try {
            await verifyFileIntegrity(downloadedPath, normalized.files[0], fsImpl, fspImpl);
        } catch (error) {
            await clearDownloadedFile();
            if (['UPDATE_SIZE_MISMATCH', 'UPDATE_CHECKSUM_MISMATCH', 'UPDATE_PACKAGE_INVALID'].includes(error?.code)) {
                throw error;
            }
            throw Object.assign(new Error('更新安装包已不存在，请重新下载。'), { code: 'UPDATE_PACKAGE_MISSING' });
        }
        if (disposed || !downloadedPath) throw clientDisposedError();
        const executable = app?.getPath?.('exe');
        const targetPath = executable ? path.dirname(executable) : options.targetPath;
        if (!targetPath) throw Object.assign(new Error('无法确定当前应用安装位置。'), { code: 'UPDATE_TARGET_UNKNOWN' });
        const installerPath = downloadedPath;
        const cleanupSpawnedInstaller = () => {
            if (downloadedPath === installerPath) {
                downloadedPath = null;
                downloadedInfo = null;
            }
            return fspImpl.rm(installerPath, { force: true }).catch(() => {});
        };
        let child;
        try {
            child = spawnImpl(installerPath, [
                '--mode=update',
                '--auto-start',
                '--target-version',
                normalized.version,
                '--target',
                targetPath,
            ], {
                cwd: path.dirname(installerPath),
                detached: true,
                stdio: 'ignore',
                // Setup.exe owns a visible installer UI. Let Windows treat it
                // as a normal GUI launch so the subsequent app handoff can
                // participate in foreground activation.
                windowsHide: false,
                // The portable wrapper normally sets this itself, but an
                // updater can inherit the current app's value. Point the
                // child at the downloaded Setup explicitly so its installer
                // service derives paths from the new executable, not stale
                // environment state.
                env: {
                    ...environment,
                    PORTABLE_EXECUTABLE_FILE: installerPath,
                },
            });
            if (!child || typeof child !== 'object') {
                throw Object.assign(new Error('无法启动更新安装程序。'), { code: 'UPDATE_INSTALLER_START_FAILED' });
            }
        } catch (error) {
            await cleanupSpawnedInstaller();
            if (error?.code === 'UPDATE_INSTALLER_START_FAILED') throw error;
            throw Object.assign(new Error('无法启动 Windows 更新安装程序，请重试。'), {
                code: 'UPDATE_INSTALLER_START_FAILED',
                cause: error,
            });
        }
        if (typeof child?.once === 'function') {
            child.once('error', cleanupSpawnedInstaller);
            child.once('close', cleanupSpawnedInstaller);
        } else if (typeof child?.on === 'function') {
            child.on('error', cleanupSpawnedInstaller);
            child.on('close', cleanupSpawnedInstaller);
        }
        try {
            await waitForChildSpawn(child);
        } catch (error) {
            await cleanupSpawnedInstaller();
            throw Object.assign(new Error('无法启动 Windows 更新安装程序，请重试。'), {
                code: 'UPDATE_INSTALLER_START_FAILED',
                cause: error,
            });
        }
        child.unref?.();
        downloadedPath = null;
        downloadedInfo = null;
        options.quit?.();
        return { success: true, path: installerPath, version: normalized.version };
    }

    function dispose() {
        if (disposed) return;
        disposed = true;
        activeDownloadController?.abort();
        latestInfo = null;
        void clearDownloadedFile();
    }

    return {
        check,
        download,
        install,
        dispose,
        getMetadataUrl: () => metadataUrl,
        getDownloadedPath: () => downloadedPath,
    };
}

module.exports = {
    DEFAULT_METADATA_NAME,
    DEFAULT_BLOCKMAP_SUFFIX,
    buildWindowsArtifactUrl,
    buildWindowsBlockMapUrl,
    buildWindowsMetadataUrl,
    createWindowsUpdateClient,
    downloadResponseToFile,
    fetchBlockMap,
    normalizeWindowsMetadata,
    replaceArtifactVersion,
};
