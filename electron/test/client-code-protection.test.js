'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const test = require('node:test');
const {
    decryptClientScript,
    fetchClientCodeKey,
} = require('../client-code-protection');

const KEY = Buffer.alloc(32, 0x6b);
const KEY_ID = crypto.createHash('sha256').update(KEY).digest('hex').slice(0, 16);

function keyDocument() {
    return JSON.stringify({
        version: 1,
        algorithm: 'AES-256-GCM',
        keyId: KEY_ID,
        keyHex: KEY.toString('hex'),
    });
}

function keyResponse(chunks, headers = {}, onCancel = () => {}, keepOpen = false) {
    return {
        ok: true,
        status: 200,
        headers: { get: name => headers[String(name).toLowerCase()] ?? null },
        body: new ReadableStream({
            start(controller) {
                chunks.forEach(chunk => controller.enqueue(Buffer.from(chunk)));
                if (!keepOpen) controller.close();
            },
            cancel: onCancel,
        }),
    };
}

test('客户端密钥通过固定的无重定向 URL 读取并校验 keyId', async () => {
    let request;
    const result = await fetchClientCodeKey({
        fetchImpl: async (url, init) => {
            request = { url, init };
            return keyResponse([keyDocument()], { 'content-length': String(Buffer.byteLength(keyDocument())) });
        },
    });

    assert.equal(result.keyId, KEY_ID);
    assert.deepEqual(result.key, KEY);
    assert.equal(request.url, 'https://raw.githubusercontent.com/asklins223/word-tts-desktop/main/client-code-key.json');
    assert.equal(request.init.redirect, 'error');
    assert.equal(request.init.cache, 'no-store');
});

test('客户端密钥响应在声明长度或真实流长度超限时会被拒绝并取消', async () => {
    let declaredCancelled = false;
    await assert.rejects(
        fetchClientCodeKey({
            fetchImpl: async () => keyResponse([Buffer.from('')], { 'content-length': '4097' }, () => { declaredCancelled = true; }, true),
        }),
        /文件过大/,
    );
    assert.equal(declaredCancelled, true);

    let streamCancelled = false;
    await assert.rejects(
        fetchClientCodeKey({
            fetchImpl: async () => keyResponse([Buffer.alloc(4096, 0x61), Buffer.from('x')], {}, () => { streamCancelled = true; }, true),
        }),
        /文件过大/,
    );
    assert.equal(streamCancelled, true);
});

test('客户端密钥响应不支持无界 text() 回退', async () => {
    await assert.rejects(
        fetchClientCodeKey({ fetchImpl: async () => ({ ok: true, status: 200, text: async () => keyDocument() }) }),
        /无法安全读取/,
    );
});

test('AES-GCM 同时校验密文完整性和脚本相对路径', () => {
    const relativePath = 'modules/app/app.js.enc';
    const iv = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv('aes-256-gcm', KEY, iv);
    cipher.setAAD(Buffer.from(`${relativePath}\0${KEY_ID}`, 'utf8'));
    const ciphertext = Buffer.concat([cipher.update('sensitive renderer source'), cipher.final()]);
    const envelope = Buffer.concat([
        Buffer.from('WTC1', 'ascii'),
        Buffer.from(KEY_ID, 'hex'),
        iv,
        cipher.getAuthTag(),
        ciphertext,
    ]);

    assert.equal(decryptClientScript(envelope, relativePath, KEY, KEY_ID), 'sensitive renderer source');
    assert.throws(() => decryptClientScript(envelope, 'modules/app/other.js.enc', KEY, KEY_ID));
    const tampered = Buffer.from(envelope);
    tampered[tampered.length - 1] ^= 1;
    assert.throws(() => decryptClientScript(tampered, relativePath, KEY, KEY_ID));
});
