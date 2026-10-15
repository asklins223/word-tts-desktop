'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { verifyStartupRelease } = require('../startup-release-check');

const RELEASE_URL = 'https://github.com/asklins223/word-tts-desktop/releases';
const LATEST = `${RELEASE_URL}/latest/download/latest-win.json`;
const CURRENT = `${RELEASE_URL}/download/v1.2.3/latest-win.json`;

function metadata(version, { platform = 'win32', architecture = 'x64' } = {}) {
    return {
        version,
        files: [{
            url: platform === 'win32'
                ? `wordTTS-Setup-${version}-x64.exe`
                : `wordTTS-${version}-${architecture}.zip`,
            sha512: Buffer.alloc(64, 7).toString('base64'),
            size: 1234,
        }],
    };
}

function artifactUrl(info) {
    return `${RELEASE_URL}/download/v${info.version}/${info.files[0].url}`;
}

function createFixture(routes) {
    const requests = [];
    return {
        requests,
        fetchImpl: async (url, options) => {
            requests.push({ url, ...options });
            const route = routes[url] ?? 404;
            if (route instanceof Error) throw route;
            const hasRange = options.headers?.get?.('range') === 'bytes=0-3';
            const status = hasRange && route === 200
                ? 206
                : typeof route === 'number' ? route : 200;
            return {
                ok: status >= 200 && status < 300,
                status,
                json: async () => route,
                text: async () => typeof route === 'string' ? route : JSON.stringify(route),
                body: hasRange
                    ? new ReadableStream({
                        start(controller) {
                            const bytes = String(url).toLowerCase().endsWith('.exe')
                                ? Buffer.from([0x4d, 0x5a, 0x90, 0x00])
                                : Buffer.from([0x50, 0x4b, 0x03, 0x04]);
                            controller.enqueue(bytes);
                            controller.close();
                        },
                    })
                    : { cancel: async () => {} },
                headers: {
                    get(name) {
                        const normalized = String(name).toLowerCase();
                        if (hasRange && normalized === 'content-range') return 'bytes 0-3/1234';
                        if (hasRange && normalized === 'content-length') return '4';
                        return null;
                    },
                },
            };
        },
    };
}

function verify(fixture, options = {}) {
    return verifyStartupRelease({
        isPackaged: true,
        currentVersion: '1.2.3',
        platform: 'win32',
        architecture: 'x64',
        releaseUrl: RELEASE_URL,
        fetchImpl: fixture.fetchImpl,
        ...options,
    });
}

function isServiceError(error) {
    assert.equal(error.code, 'STARTUP_SERVICE_UNAVAILABLE');
    assert.match(error.message, /应用已停用/);
    return true;
}

test('当前版本线上包可用时放行，探测去重且不使用缓存', async () => {
    const current = metadata('1.2.3');
    const fixture = createFixture({ [LATEST]: current, [CURRENT]: current, [artifactUrl(current)]: 200 });
    const result = await verify(fixture);
    assert.equal(result.required, true);
    assert.equal(result.version, '1.2.3');
    assert.equal(result.updateAvailable, false);
    assert.equal(result.artifactUrl, artifactUrl(current));
    assert.deepEqual(fixture.requests.map(request => request.method), ['GET', 'GET', 'GET']);
    assert.equal(fixture.requests.at(-1).headers.get('range'), 'bytes=0-3');
    assert.ok(fixture.requests.every(request => request.cache === 'no-store'));
});

test('当前版本撤销但有实际可用的更高版本时放行', async () => {
    const newer = metadata('1.3.0');
    const fixture = createFixture({ [LATEST]: newer, [CURRENT]: 404, [artifactUrl(newer)]: 200 });
    const result = await verify(fixture);
    assert.equal(result.version, '1.3.0');
    assert.equal(result.updateAvailable, true);
});

test('当前版本撤销且 latest 回退到低版本时阻止启动', async () => {
    const fixture = createFixture({ [LATEST]: metadata('1.2.2'), [CURRENT]: 404 });
    await assert.rejects(verify(fixture), isServiceError);
    assert.ok(fixture.requests.every(request => request.method === 'GET'));
});

test('元数据仍在但当前和更高版本的具体安装包都被撤销时阻止启动', async () => {
    const current = metadata('1.2.3');
    const newer = metadata('1.3.0');
    const fixture = createFixture({ [LATEST]: newer, [CURRENT]: current });
    await assert.rejects(verify(fixture), isServiceError);
    assert.deepEqual(fixture.requests.filter(request => request.method === 'GET' && request.headers?.get?.('range'))
        .map(request => request.url), [artifactUrl(newer), artifactUrl(current)]);
});

test('只有更高版本的元数据而没有可下载包时不能放行', async () => {
    const fixture = createFixture({ [LATEST]: metadata('9.0.0'), [CURRENT]: 404 });
    await assert.rejects(verify(fixture), isServiceError);
});

test('新包不可用但当前包仍可用时允许启动', async () => {
    const current = metadata('1.2.3');
    const fixture = createFixture({
        [LATEST]: metadata('1.3.0'),
        [CURRENT]: current,
        [artifactUrl(current)]: 200,
    });
    assert.equal((await verify(fixture)).version, '1.2.3');
});

test('强制更新包不可用时，即使当前包仍可用也不能放行', async () => {
    const current = metadata('1.2.3');
    const fixture = createFixture({
        [LATEST]: { ...metadata('1.3.0'), updateMode: 'force' },
        [CURRENT]: current,
        [artifactUrl(current)]: 200,
    });
    await assert.rejects(verify(fixture), isServiceError);
});

test('缺失或矛盾的线上强更策略不会悄悄降级为可选更新', async () => {
    const invalid = { ...metadata('1.2.3'), updateMode: 'force', minimumSupportedVersion: '2.0.0' };
    const fixture = createFixture({ [LATEST]: invalid, [CURRENT]: invalid, [artifactUrl(invalid)]: 200 });
    await assert.rejects(verify(fixture), isServiceError);
});

test('latest 缺少当前平台信息时仍确认当前版本的线上包', async () => {
    const current = metadata('1.2.3');
    const fixture = createFixture({ [CURRENT]: current, [artifactUrl(current)]: 200 });
    assert.equal((await verify(fixture)).version, '1.2.3');
});

test('线上地址不可达、服务返回错误或元数据损坏时阻止启动', async t => {
    for (const [name, route] of [
        ['断网', new Error('fetch failed')],
        ['服务错误', 503],
        ['元数据损坏', { version: 'invalid' }],
        ['错误平台', metadata('1.2.3', { platform: 'darwin' })],
    ]) {
        await t.test(name, async () => {
            const fixture = createFixture({ [LATEST]: route, [CURRENT]: route });
            await assert.rejects(verify(fixture), isServiceError);
        });
    }
});

test('当前版本地址不能使用其他版本元数据冒充当前包', async () => {
    const newer = metadata('1.3.0');
    const fixture = createFixture({ [CURRENT]: newer, [artifactUrl(newer)]: 200 });
    await assert.rejects(verify(fixture), isServiceError);
});

test('安装包地址使用文件头范围 GET 验证可下载', async () => {
    const current = metadata('1.2.3');
    const fixture = createFixture({ [LATEST]: current, [CURRENT]: current });
    const fetchMetadata = fixture.fetchImpl;
    fixture.fetchImpl = (url, options) => {
        if (url !== artifactUrl(current)) return fetchMetadata(url, options);
        fixture.requests.push({ url, ...options });
        return Promise.resolve({
            status: 206,
            headers: { get: name => String(name).toLowerCase() === 'content-range' ? 'bytes 0-3/1234'
                : String(name).toLowerCase() === 'content-length' ? '4' : null },
            body: new ReadableStream({ start(controller) { controller.enqueue(Buffer.from([0x4d, 0x5a, 0x90, 0x00])); controller.close(); } }),
        });
    };
    assert.equal((await verify(fixture)).version, '1.2.3');
    assert.equal(fixture.requests.at(-1).method, 'GET');
    assert.equal(fixture.requests.at(-1).headers.get('range'), 'bytes=0-3');
});

test('macOS 只接受当前架构或 universal ZIP', async t => {
    for (const architecture of ['arm64', 'x64', 'universal']) {
        await t.test(architecture, async () => {
            const info = metadata('1.2.3', { platform: 'darwin', architecture });
            const fixture = createFixture({
                [`${RELEASE_URL}/latest/download/latest-mac.yml`]: info,
                [artifactUrl(info)]: 200,
            });
            const pending = verify(fixture, { platform: 'darwin', architecture: 'arm64' });
            if (architecture === 'x64') await assert.rejects(pending, isServiceError);
            else assert.equal((await pending).version, '1.2.3');
        });
    }
});

test('所有探测都受整体启动时限约束，超时后取消请求', async () => {
    const signals = [];
    const fixture = {
        fetchImpl: async (_url, options) => {
            signals.push(options.signal);
            return new Promise(() => {});
        },
    };
    const started = Date.now();
    await assert.rejects(verify(fixture, { timeoutMs: 25 }), isServiceError);
    assert.ok(Date.now() - started < 1000);
    assert.ok(signals.every(signal => signal.aborted));
});

test('响应正文和包探测挂起也会超时并阻止启动', async t => {
    const current = metadata('1.2.3');
    await t.test('正文挂起', async () => {
        const fixture = { fetchImpl: async () => ({ ok: true, json: () => new Promise(() => {}) }) };
        await assert.rejects(verify(fixture, { timeoutMs: 25 }), isServiceError);
    });
    await t.test('包挂起', async () => {
        const fixture = createFixture({ [LATEST]: current, [CURRENT]: current });
        const fetchMetadata = fixture.fetchImpl;
        fixture.fetchImpl = (url, options) => url === artifactUrl(current)
            ? new Promise(() => {}) : fetchMetadata(url, options);
        await assert.rejects(verify(fixture, { timeoutMs: 25 }), isServiceError);
    });
});

test('开发和 smoke 模式不发起网络请求', async () => {
    const fixture = { fetchImpl: () => { throw new Error('unexpected network request'); } };
    assert.deepEqual(await verify(fixture, { isPackaged: false }), { required: false });
    assert.deepEqual(await verify(fixture, { isSmokeTest: true }), { required: false });
});

test('正式启动不接受无效地址和无法验证的平台', async () => {
    const fixture = createFixture({});
    for (const options of [
        { releaseUrl: 'invalid' },
        { releaseUrl: RELEASE_URL.replace('https:', 'http:') },
        { currentVersion: 'invalid' },
        { platform: 'linux' },
        { architecture: 'ia32' },
    ]) {
        await assert.rejects(verify(fixture, options), isServiceError);
    }
    assert.equal(fixture.requests.length, 0);
});
