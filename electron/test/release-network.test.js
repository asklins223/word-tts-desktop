'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { fetchTrustedReleaseUrl, isTrustedReleaseUrl } = require('../release-network');

function response(status, headers = {}, url = '') {
    return {
        status,
        url,
        headers: { get: name => headers[String(name).toLowerCase()] ?? null },
        body: { cancel: async () => {} },
    };
}

test('发布源只接受 HTTPS、同源或 GitHub Release 资产重定向', () => {
    const source = 'https://github.com/asklins223/word-tts-desktop/releases/download/v1.2.3/app.exe';
    assert.equal(isTrustedReleaseUrl(source, source), true);
    assert.equal(isTrustedReleaseUrl(source, 'https://release-assets.githubusercontent.com/asset'), true);
    for (const target of [
        'http://github.com/asklins223/word-tts-desktop/app.exe',
        'https://github.com.attacker.test/app.exe',
        'https://127.0.0.1/app.exe',
        'https://github.com:8443/app.exe',
        'https://user:pass@github.com/app.exe',
    ]) assert.equal(isTrustedReleaseUrl(source, target), false, target);
});

test('跨源 Release 资产重定向会剥离凭据类请求头', async () => {
    const requests = [];
    const result = await fetchTrustedReleaseUrl(
        'https://github.com/asklins223/word-tts-desktop/releases/download/v1.2.3/app.exe',
        { headers: { Authorization: 'Bearer secret', Cookie: 'session=secret', 'X-Trace': 'ok' } },
        async (url, init) => {
            requests.push({ url, headers: new Headers(init.headers) });
            if (url.startsWith('https://github.com/')) {
                return response(302, { location: 'https://release-assets.githubusercontent.com/asset' });
            }
            return response(206, {}, url);
        },
    );

    assert.equal(result.status, 206);
    assert.equal(requests.length, 2);
    assert.equal(requests[0].headers.get('authorization'), 'Bearer secret');
    assert.equal(requests[1].headers.get('authorization'), null);
    assert.equal(requests[1].headers.get('cookie'), null);
    assert.equal(requests[1].headers.get('x-trace'), 'ok');
});

test('拒绝重定向到本机、未知域名、超限重定向和非只读方法', async () => {
    const source = 'https://github.com/asklins223/word-tts-desktop/releases/download/v1.2.3/app.exe';
    let calls = 0;
    await assert.rejects(
        fetchTrustedReleaseUrl(source, {}, async () => {
            calls += 1;
            return response(302, { location: 'http://127.0.0.1:8080/private' });
        }),
        error => error.code === 'UNTRUSTED_RELEASE_REDIRECT',
    );
    assert.equal(calls, 1, 'untrusted destination must never be requested');

    calls = 0;
    await assert.rejects(
        fetchTrustedReleaseUrl(source, {}, async () => {
            calls += 1;
            return response(302, { location: '/again' });
        }),
        error => error.code === 'RELEASE_REDIRECT_LIMIT',
    );
    assert.equal(calls, 6);

    await assert.rejects(
        fetchTrustedReleaseUrl(source, { method: 'POST' }, async () => response(200)),
        error => error.code === 'UNSUPPORTED_RELEASE_METHOD',
    );
});
