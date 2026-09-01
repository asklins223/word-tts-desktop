'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const EventEmitter = require('node:events');
const fs = require('node:fs');
const fsp = fs.promises;
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {
    buildWindowsArtifactUrl,
    buildWindowsBlockMapUrl,
    buildWindowsMetadataUrl,
    createWindowsUpdateClient,
    downloadResponseToFile,
    normalizeWindowsMetadata,
} = require('../windows-update-client');

const RELEASE_URL = 'https://github.com/asklins223/word-tts-desktop/releases';

test('Windows 更新地址固定到 latest-win.json 和 Release 下载资产', () => {
    assert.equal(
        buildWindowsMetadataUrl(RELEASE_URL),
        'https://github.com/asklins223/word-tts-desktop/releases/latest/download/latest-win.json',
    );
    assert.equal(
        buildWindowsArtifactUrl(RELEASE_URL, 'v3.0.2', 'wordTTS-Setup-3.0.2-x64.exe'),
        'https://github.com/asklins223/word-tts-desktop/releases/download/v3.0.2/wordTTS-Setup-3.0.2-x64.exe',
    );
    assert.equal(
        buildWindowsBlockMapUrl(RELEASE_URL, 'v3.0.2', 'wordTTS-Setup-3.0.2-x64.exe'),
        'https://github.com/asklins223/word-tts-desktop/releases/download/v3.0.2/wordTTS-Setup-3.0.2-x64.exe.blockmap',
    );
    assert.equal(
        buildWindowsBlockMapUrl('', 'v3.0.2', 'https://cdn.example.test/setup.exe?download=1'),
        'https://cdn.example.test/setup.exe.blockmap?download=1',
    );
});

test('Windows 更新元数据拒绝非 exe、空校验值和无效大小', () => {
    assert.throws(
        () => normalizeWindowsMetadata({ version: '3.0.2', artifact: { url: 'update.zip', sha512: 'x', size: 1 } }),
        error => error.code === 'INVALID_UPDATE_METADATA',
    );
    assert.throws(
        () => normalizeWindowsMetadata({ version: '3.0.2', artifact: { url: 'update.exe', sha512: '', size: 1 } }),
        error => error.code === 'INVALID_UPDATE_METADATA',
    );
    assert.throws(
        () => normalizeWindowsMetadata({ version: '3.0.2', artifact: { url: 'update.exe', sha512: 'x', size: 0 } }),
        error => error.code === 'INVALID_UPDATE_METADATA',
    );
    assert.throws(
        () => normalizeWindowsMetadata({ version: '3.0.2', artifact: { url: 'other-app-3.0.2-x64.exe', sha512: 'x', size: 1 } }),
        error => error.code === 'INVALID_UPDATE_METADATA',
    );
    assert.throws(
        () => normalizeWindowsMetadata({ platform: 'darwin', version: '3.0.2', artifact: { url: 'wordTTS-Setup-3.0.2-x64.exe', sha512: 'x', size: 1 } }),
        error => error.code === 'INVALID_UPDATE_METADATA',
    );
    assert.throws(
        () => normalizeWindowsMetadata({ version: '3.0.2-01', artifact: { url: 'wordTTS-Setup-3.0.2-01-x64.exe', sha512: 'x', size: 1 } }),
        error => error.code === 'INVALID_UPDATE_METADATA',
    );
    assert.throws(
        () => normalizeWindowsMetadata({ version: '3.0.2', artifact: { url: 'nested/wordTTS-Setup-3.0.2-x64.exe', sha512: 'x', size: 1 } }),
        error => error.code === 'INVALID_UPDATE_METADATA',
    );
    assert.equal(
        buildWindowsArtifactUrl(RELEASE_URL, 'v3.0.2', '../wordTTS-Setup-3.0.2-x64.exe'),
        null,
    );
});

test('Windows 更新元数据会跳过错误候选并统一选中的安装包字段', () => {
    const normalized = normalizeWindowsMetadata({
        version: '3.0.2',
        tag: 'v3.0.2',
        artifact: { url: 'other-app-3.0.2-x64.exe', sha512: 'wrong', size: 1 },
        files: [
            { url: 'wordTTS-Setup-3.0.2-x64.exe', sha512: 'right', size: 2 },
        ],
    });
    assert.deepEqual(normalized.artifact, normalized.files[0]);
    assert.equal(normalized.path, 'wordTTS-Setup-3.0.2-x64.exe');
    assert.equal(normalized.sha512, 'right');
    assert.equal(normalized.size, 2);
});

test('Windows 更新优先使用 Setup.exe blockmap 做分块差分，失败时仍可回退全量', async () => {
    const { buildBlockMap } = require('../node_modules/app-builder-lib/out/targets/blockmap/blockmap');
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-windows-update-differential-'));
    const installedDirectory = path.join(root, 'installed');
    const oldInstallerPath = path.join(installedDirectory, '小猪wordTTS-uninstaller.exe');
    const oldBytes = Buffer.alloc(384 * 1024);
    for (let index = 0; index < oldBytes.length; index += 1) oldBytes[index] = (index * 31 + 7) % 251;
    const newBytes = Buffer.from(oldBytes);
    newBytes.fill(0x5a, 120 * 1024, 148 * 1024);
    const oldBlockmapPath = path.join(root, 'old.exe.blockmap');
    const newBlockmapPath = path.join(root, 'new.exe.blockmap');
    const metadata = {
        schemaVersion: 1,
        platform: 'win32',
        version: '3.0.2',
        tag: 'v3.0.2',
        artifact: {
            url: 'wordTTS-Setup-3.0.2-x64.exe',
            sha512: crypto.createHash('sha512').update(newBytes).digest('base64'),
            size: newBytes.length,
        },
    };
    const requests = [];
    try {
        await fsp.mkdir(installedDirectory, { recursive: true });
        await fsp.writeFile(oldInstallerPath, oldBytes);
        await buildBlockMap(oldInstallerPath, 'gzip', oldBlockmapPath);
        const newArtifactPath = path.join(root, 'new.exe');
        await fsp.writeFile(newArtifactPath, newBytes);
        await buildBlockMap(newArtifactPath, 'gzip', newBlockmapPath);
        const oldBlockmapBytes = await fsp.readFile(oldBlockmapPath);
        const newBlockmapBytes = await fsp.readFile(newBlockmapPath);
        const client = createWindowsUpdateClient({
            releaseUrl: RELEASE_URL,
            currentVersion: '3.0.1',
            tempDirectory: path.join(root, 'temp'),
            app: { getPath: () => path.join(installedDirectory, '小猪wordTTS.exe') },
            fetchImpl: async (url, request = {}) => {
                requests.push({ url, request });
                if (url.endsWith('3.0.1-x64.exe.blockmap')) {
                    return {
                        ok: true,
                        status: 200,
                        arrayBuffer: async () => oldBlockmapBytes.buffer.slice(oldBlockmapBytes.byteOffset, oldBlockmapBytes.byteOffset + oldBlockmapBytes.byteLength),
                    };
                }
                if (url.endsWith('3.0.2-x64.exe.blockmap')) {
                    return {
                        ok: true,
                        status: 200,
                        arrayBuffer: async () => newBlockmapBytes.buffer.slice(newBlockmapBytes.byteOffset, newBlockmapBytes.byteOffset + newBlockmapBytes.byteLength),
                    };
                }
                const range = String(request.headers?.Range || request.headers?.range || '');
                const match = range.match(/^bytes=(\d+)-(\d+)$/);
                if (!match) throw new Error(`unexpected non-range request: ${url}`);
                const start = Number(match[1]);
                const end = Number(match[2]) + 1;
                const bytes = newBytes.subarray(start, end);
                return {
                    ok: true,
                    status: 206,
                    arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
                };
            },
        });

        const downloadedPath = await client.download(metadata);
        assert.deepEqual(await fsp.readFile(downloadedPath), newBytes);
        const rangeRequests = requests.filter(entry => entry.request.headers?.Range || entry.request.headers?.range);
        assert.ok(rangeRequests.length > 0, 'expected at least one HTTP range request');
        const downloadedBytes = rangeRequests.reduce((sum, entry) => {
            const match = String(entry.request.headers.Range || entry.request.headers.range).match(/bytes=\d+-(\d+)/);
            return sum + Number(match[1]) + 1;
        }, 0);
        assert.ok(downloadedBytes < newBytes.length * 0.95, 'differential path should download less than the full Setup');
        client.dispose();
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('Windows 更新在差分索引缺失、Range 不可用、差分损坏或节省不足时回退全量', async () => {
    const { buildBlockMap } = require('../node_modules/app-builder-lib/out/targets/blockmap/blockmap');
    const scenarios = ['missing-blockmap', 'range-unsupported', 'corrupt-range', 'invalid-blockmap', 'no-savings'];
    for (const scenario of scenarios) {
        const root = await fsp.mkdtemp(path.join(os.tmpdir(), `wordtts-windows-update-fallback-${scenario}-`));
        const installedDirectory = path.join(root, 'installed');
        const oldInstallerPath = path.join(installedDirectory, '小猪wordTTS-uninstaller.exe');
        const oldBytes = Buffer.alloc(384 * 1024);
        for (let index = 0; index < oldBytes.length; index += 1) oldBytes[index] = (index * 17 + 13) % 251;
        const newBytes = scenario === 'no-savings'
            ? Buffer.alloc(oldBytes.length, 0x5a)
            : Buffer.from(oldBytes);
        if (scenario !== 'no-savings') newBytes.fill(0x5a, 120 * 1024, 148 * 1024);
        const oldBlockmapPath = path.join(root, 'old.exe.blockmap');
        const newBlockmapPath = path.join(root, 'new.exe.blockmap');
        const metadata = {
            schemaVersion: 1,
            platform: 'win32',
            version: '3.0.2',
            tag: 'v3.0.2',
            artifact: {
                url: 'wordTTS-Setup-3.0.2-x64.exe',
                sha512: crypto.createHash('sha512').update(newBytes).digest('base64'),
                size: newBytes.length,
            },
        };
        const requests = [];
        try {
            await fsp.mkdir(installedDirectory, { recursive: true });
            await fsp.writeFile(oldInstallerPath, oldBytes);
            await buildBlockMap(oldInstallerPath, 'gzip', oldBlockmapPath);
            const newArtifactPath = path.join(root, 'new.exe');
            await fsp.writeFile(newArtifactPath, newBytes);
            await buildBlockMap(newArtifactPath, 'gzip', newBlockmapPath);
            const oldBlockmapBytes = await fsp.readFile(oldBlockmapPath);
            const newBlockmapBytes = await fsp.readFile(newBlockmapPath);
            const responseFor = (bytes, status = 200) => {
                const copy = Buffer.from(bytes);
                return {
                    ok: status >= 200 && status < 300,
                    status,
                    arrayBuffer: async () => copy.buffer.slice(copy.byteOffset, copy.byteOffset + copy.byteLength),
                };
            };
            const client = createWindowsUpdateClient({
                releaseUrl: RELEASE_URL,
                currentVersion: '3.0.1',
                tempDirectory: path.join(root, 'temp'),
                app: { getPath: () => path.join(installedDirectory, '小猪wordTTS.exe') },
                fetchImpl: async (url, request = {}) => {
                    requests.push({ url, request });
                    if (url.includes('.blockmap')) {
                        if (scenario === 'missing-blockmap') return responseFor(Buffer.from('missing'), 404);
                        if (scenario === 'invalid-blockmap') return responseFor(Buffer.from('{}'));
                        if (url.includes('3.0.1-x64.exe.blockmap')) return responseFor(oldBlockmapBytes);
                        return responseFor(newBlockmapBytes);
                    }
                    const range = String(request.headers?.Range || request.headers?.range || '');
                    const match = range.match(/^bytes=(\d+)-(\d+)$/);
                    if (match) {
                        const start = Number(match[1]);
                        const end = Number(match[2]) + 1;
                        if (scenario === 'range-unsupported') return responseFor(newBytes, 200);
                        if (scenario === 'corrupt-range') return responseFor(Buffer.alloc(end - start, 0));
                        return responseFor(newBytes.subarray(start, end), 206);
                    }
                    return responseFor(newBytes);
                },
            });

            const downloadedPath = await client.download(metadata);
            assert.deepEqual(await fsp.readFile(downloadedPath), newBytes, scenario);
            const rangeRequests = requests.filter(entry => entry.request.headers?.Range || entry.request.headers?.range);
            const fullRequests = requests.filter(entry => !entry.url.includes('.blockmap')
                && !(entry.request.headers?.Range || entry.request.headers?.range));
            assert.ok(fullRequests.length >= 1, `${scenario}: expected full-download fallback`);
            if (['range-unsupported', 'corrupt-range'].includes(scenario)) {
                assert.ok(rangeRequests.length >= 1, `${scenario}: expected differential attempt before fallback`);
            } else {
                assert.equal(rangeRequests.length, 0, `${scenario}: should not issue ranges`);
            }
            client.dispose();
        } finally {
            await fsp.rm(root, { recursive: true, force: true });
        }
    }
});

test('Windows 更新包落盘失败时会收敛为可捕获错误而不会悬挂', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-windows-update-write-error-'));
    try {
        await assert.rejects(
            () => downloadResponseToFile(
                {
                    ok: true,
                    arrayBuffer: async () => Buffer.from('setup').buffer,
                },
                path.join(root, 'missing-parent', 'setup.exe'),
                5,
                null,
            ),
            error => error?.code === 'ENOENT',
        );
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('Windows 更新客户端下载后校验并启动自绘 Setup.exe', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-windows-update-'));
    const bytes = Buffer.from('self-drawing setup bytes', 'utf8');
    const sha512 = crypto.createHash('sha512').update(bytes).digest('base64');
    const metadata = {
        schemaVersion: 1,
        platform: 'win32',
        version: '3.0.2',
        tag: 'v3.0.2',
        artifact: {
            url: 'wordTTS-Setup-3.0.2-x64.exe',
            sha512,
            size: bytes.length,
        },
        updateMode: 'optional',
    };
    const requests = [];
    const spawnCalls = [];
    let quitCalled = false;
    try {
        const client = createWindowsUpdateClient({
            releaseUrl: RELEASE_URL,
            tempDirectory: path.join(root, 'temp'),
            app: { getPath: () => path.join(root, 'installed', '小猪wordTTS.exe') },
            fetchImpl: async (url, request) => {
                requests.push({ url, request });
                if (url.endsWith('latest-win.json')) {
                    return {
                        ok: true,
                        status: 200,
                        json: async () => metadata,
                    };
                }
                return {
                    ok: true,
                    status: 200,
                    headers: { get: name => name.toLowerCase() === 'content-length' ? String(bytes.length) : null },
                    arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
                };
            },
            spawn: (executable, args, options) => {
                spawnCalls.push({ executable, args, options });
                return { unref() {} };
            },
            quit: () => { quitCalled = true; },
        });

        assert.deepEqual((await client.check()).artifact, metadata.artifact);
        const progress = [];
        const downloadedPath = await client.download(metadata, value => progress.push(value));
        assert.equal(fs.readFileSync(downloadedPath, 'utf8'), bytes.toString('utf8'));
        assert.equal(progress.at(-1).percent, 100);
        assert.equal(requests[0].request.method, 'GET');
        assert.equal(requests[1].request.method, 'GET');

        const result = await client.install(metadata);
        assert.equal(result.success, true);
        assert.equal(spawnCalls.length, 1);
        assert.equal(spawnCalls[0].args[0], '--mode=update');
        assert.equal(spawnCalls[0].args[1], '--auto-start');
        assert.equal(spawnCalls[0].args[2], '--target-version');
        assert.equal(spawnCalls[0].args[3], '3.0.2');
        assert.equal(spawnCalls[0].args[4], '--target');
        assert.equal(spawnCalls[0].args[5], path.join(root, 'installed'));
        assert.equal(spawnCalls[0].options.env.PORTABLE_EXECUTABLE_FILE, downloadedPath);
        assert.equal(quitCalled, true);
        client.dispose();
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('Windows 更新安装不会把新元数据配到旧的已下载安装包', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-windows-update-mismatch-'));
    const bytes = Buffer.from('self-drawing setup bytes', 'utf8');
    const sha512 = crypto.createHash('sha512').update(bytes).digest('base64');
    const metadata = version => ({
        schemaVersion: 1,
        platform: 'win32',
        version,
        tag: `v${version}`,
        artifact: {
            url: `wordTTS-Setup-${version}-x64.exe`,
            sha512,
            size: bytes.length,
        },
    });
    const spawnCalls = [];
    try {
        const client = createWindowsUpdateClient({
            releaseUrl: RELEASE_URL,
            tempDirectory: path.join(root, 'temp'),
            app: { getPath: () => path.join(root, 'installed', '小猪wordTTS.exe') },
            fetchImpl: async () => ({
                ok: true,
                status: 200,
                arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
            }),
            spawn: (_executable, args) => {
                spawnCalls.push(args);
                return { unref() {} };
            },
        });

        await client.download(metadata('3.0.2'));
        await assert.rejects(
            () => client.install(metadata('3.0.3')),
            error => error.code === 'UPDATE_METADATA_MISMATCH',
        );
        assert.equal(spawnCalls.length, 0);
        client.dispose();
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('Windows 更新安装器启动失败时返回可重试错误而不是卡在安装中', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-windows-update-spawn-error-'));
    const bytes = Buffer.from('self-drawing setup bytes', 'utf8');
    const sha512 = crypto.createHash('sha512').update(bytes).digest('base64');
    const metadata = {
        version: '3.0.2',
        tag: 'v3.0.2',
        artifact: {
            url: 'wordTTS-Setup-3.0.2-x64.exe',
            sha512,
            size: bytes.length,
        },
    };
    const child = new EventEmitter();
    child.unref = () => {};
    let spawnStarted;
    const spawnStartedPromise = new Promise(resolve => { spawnStarted = resolve; });
    try {
        const client = createWindowsUpdateClient({
            releaseUrl: RELEASE_URL,
            tempDirectory: path.join(root, 'temp'),
            app: { getPath: () => path.join(root, 'installed', '小猪wordTTS.exe') },
            fetchImpl: async () => ({
                ok: true,
                status: 200,
                arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
            }),
            spawn: () => {
                spawnStarted();
                return child;
            },
        });
        const downloadedPath = await client.download(metadata);
        const install = client.install(metadata);
        await spawnStartedPromise;
        child.emit('error', Object.assign(new Error('permission denied'), { code: 'EACCES' }));
        await assert.rejects(
            install,
            error => error.code === 'UPDATE_INSTALLER_START_FAILED',
        );
        assert.equal(client.getDownloadedPath(), null);
        assert.equal(fs.existsSync(downloadedPath), false);
        client.dispose();
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('Windows 更新安装前会再次校验已下载文件，文件被删除时要求重新下载', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-windows-update-missing-'));
    const bytes = Buffer.from('self-drawing setup bytes', 'utf8');
    const sha512 = crypto.createHash('sha512').update(bytes).digest('base64');
    const metadata = {
        version: '3.0.2',
        tag: 'v3.0.2',
        artifact: {
            url: 'wordTTS-Setup-3.0.2-x64.exe',
            sha512,
            size: bytes.length,
        },
    };
    try {
        const client = createWindowsUpdateClient({
            releaseUrl: RELEASE_URL,
            tempDirectory: path.join(root, 'temp'),
            app: { getPath: () => path.join(root, 'installed', '小猪wordTTS.exe') },
            fetchImpl: async () => ({
                ok: true,
                status: 200,
                arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
            }),
            spawn: () => ({ unref() {} }),
        });
        const downloadedPath = await client.download(metadata);
        await fsp.rm(downloadedPath, { force: true });
        await assert.rejects(
            () => client.install(metadata),
            error => error.code === 'UPDATE_PACKAGE_MISSING',
        );
        client.dispose();
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});

test('Windows 更新客户端释放时会中止进行中的下载并清理临时文件', async () => {
    const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-windows-update-dispose-'));
    const bytes = Buffer.from('self-drawing setup bytes', 'utf8');
    const sha512 = crypto.createHash('sha512').update(bytes).digest('base64');
    const metadata = {
        version: '3.0.2',
        tag: 'v3.0.2',
        artifact: {
            url: 'wordTTS-Setup-3.0.2-x64.exe',
            sha512,
            size: bytes.length,
        },
    };
    let requestStarted;
    const requestStartedPromise = new Promise(resolve => { requestStarted = resolve; });
    let requestSignal;
    try {
        const client = createWindowsUpdateClient({
            releaseUrl: RELEASE_URL,
            tempDirectory: path.join(root, 'temp'),
            fetchImpl: async (_url, request) => {
                requestSignal = request.signal;
                requestStarted();
                await new Promise((_, reject) => {
                    request.signal.addEventListener('abort', () => {
                        const error = new Error('aborted');
                        error.name = 'AbortError';
                        reject(error);
                    }, { once: true });
                });
                return { ok: true, arrayBuffer: async () => bytes.buffer };
            },
        });
        const download = client.download(metadata);
        await requestStartedPromise;
        client.dispose();
        assert.equal(requestSignal.aborted, true);
        await assert.rejects(download, error => error.code === 'UPDATE_CLIENT_DISPOSED');
        assert.equal(fs.existsSync(path.join(root, 'temp')), true);
        assert.deepEqual(fs.readdirSync(path.join(root, 'temp')), []);
    } finally {
        await fsp.rm(root, { recursive: true, force: true });
    }
});
