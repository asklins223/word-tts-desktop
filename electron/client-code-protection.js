'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const REMOTE_KEY_URL = 'https://raw.githubusercontent.com/asklins223/word-tts-desktop/main/client-code-key.json';
const ENVELOPE_MAGIC = Buffer.from('WTC1', 'ascii');
const KEY_ID_BYTES = 8;
const IV_BYTES = 12;
const AUTH_TAG_BYTES = 16;
const HEADER_BYTES = ENVELOPE_MAGIC.length + KEY_ID_BYTES + IV_BYTES + AUTH_TAG_BYTES;
const MAX_KEY_DOCUMENT_BYTES = 4096;
const KEY_FETCH_TIMEOUT_MS = 12_000;

async function readResponseTextBounded(response, maxBytes) {
    const reader = response.body?.getReader?.();
    if (!reader) {
        throw new Error('GitHub 返回的客户端密钥响应无法安全读取');
    }

    const chunks = [];
    let totalBytes = 0;
    try {
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            const chunk = Buffer.from(value);
            totalBytes += chunk.length;
            if (totalBytes > maxBytes) {
                await reader.cancel().catch(() => {});
                throw new Error('GitHub 返回的客户端密钥文件过大');
            }
            chunks.push(chunk);
        }
    } catch (error) {
        try { await reader.cancel(); } catch (_) { /* best effort */ }
        throw error;
    } finally {
        reader.releaseLock();
    }

    try {
        return new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks, totalBytes));
    } catch (_) {
        throw new Error('GitHub 返回的客户端密钥不是有效 UTF-8');
    }
}

function validateKeyDocument(document) {
    if (!document || typeof document !== 'object' || Array.isArray(document)) {
        throw new Error('仓库中的客户端密钥格式无效');
    }
    if (document.version !== 1 || document.algorithm !== 'AES-256-GCM') {
        throw new Error('仓库中的客户端密钥版本或算法不受支持');
    }
    if (typeof document.keyHex !== 'string' || !/^[a-f0-9]{64}$/i.test(document.keyHex)) {
        throw new Error('仓库中的客户端密钥内容无效');
    }

    const key = Buffer.from(document.keyHex, 'hex');
    const expectedKeyId = crypto.createHash('sha256').update(key).digest('hex').slice(0, KEY_ID_BYTES * 2);
    if (typeof document.keyId !== 'string' || document.keyId.toLowerCase() !== expectedKeyId) {
        key.fill(0);
        throw new Error('仓库中的客户端密钥标识不匹配');
    }
    return { key, keyId: expectedKeyId };
}

async function fetchClientCodeKey({ fetchImpl = globalThis.fetch, timeoutMs = KEY_FETCH_TIMEOUT_MS } = {}) {
    if (typeof fetchImpl !== 'function') {
        throw new Error('当前运行环境不支持联网读取客户端密钥');
    }

    let response;
    try {
        response = await fetchImpl(REMOTE_KEY_URL, {
            method: 'GET',
            headers: {
                Accept: 'application/json',
                'Cache-Control': 'no-cache, no-store',
                Pragma: 'no-cache',
            },
            cache: 'no-store',
            redirect: 'error',
            signal: AbortSignal.timeout(timeoutMs),
        });
    } catch (error) {
        throw new Error(`无法从 GitHub 获取客户端密钥：${error.message}`);
    }
    if (!response?.ok) {
        try { await response?.body?.cancel?.(); } catch (_) { /* best effort */ }
        throw new Error(`无法从 GitHub 获取客户端密钥（HTTP ${response?.status || '无响应'}）`);
    }

    const declaredLength = Number(response.headers?.get?.('content-length'));
    if (Number.isFinite(declaredLength) && declaredLength > MAX_KEY_DOCUMENT_BYTES) {
        try { await response.body?.cancel?.(); } catch (_) { /* best effort */ }
        throw new Error('GitHub 返回的客户端密钥文件过大');
    }
    const rawDocument = await readResponseTextBounded(response, MAX_KEY_DOCUMENT_BYTES);

    let document;
    try {
        document = JSON.parse(rawDocument);
    } catch (_) {
        throw new Error('GitHub 返回的客户端密钥不是有效 JSON');
    }
    return validateKeyDocument(document);
}

function decryptClientAsset(envelope, relativePath, key, expectedKeyId) {
    if (!Buffer.isBuffer(envelope) || envelope.length < HEADER_BYTES) {
        throw new Error(`客户端资源密文不完整：${relativePath}`);
    }
    if (!envelope.subarray(0, ENVELOPE_MAGIC.length).equals(ENVELOPE_MAGIC)) {
        throw new Error(`客户端资源密文格式不受支持：${relativePath}`);
    }

    let offset = ENVELOPE_MAGIC.length;
    const keyId = envelope.subarray(offset, offset + KEY_ID_BYTES).toString('hex');
    offset += KEY_ID_BYTES;
    if (keyId !== expectedKeyId) {
        throw new Error(`客户端资源密文与仓库密钥不匹配：${relativePath}`);
    }
    const iv = envelope.subarray(offset, offset + IV_BYTES);
    offset += IV_BYTES;
    const authTag = envelope.subarray(offset, offset + AUTH_TAG_BYTES);
    offset += AUTH_TAG_BYTES;

    const decipher = crypto.createDecipheriv('aes-256-gcm', key, iv);
    decipher.setAAD(Buffer.from(`${relativePath}\0${keyId}`, 'utf8'));
    decipher.setAuthTag(authTag);
    const plaintext = Buffer.concat([
        decipher.update(envelope.subarray(offset)),
        decipher.final(),
    ]);

    try {
        return new TextDecoder('utf-8', { fatal: true }).decode(plaintext);
    } catch (_) {
        throw new Error(`客户端资源解密后不是有效 UTF-8：${relativePath}`);
    } finally {
        plaintext.fill(0);
    }
}

function collectEncryptedAssets(rendererDirectory, currentDirectory = rendererDirectory) {
    const entries = fs.readdirSync(currentDirectory, { withFileTypes: true });
    const encryptedFiles = [];
    for (const entry of entries) {
        const absolutePath = path.join(currentDirectory, entry.name);
        if (entry.isSymbolicLink()) {
            throw new Error('客户端资源目录包含不受支持的符号链接');
        }
        if (entry.isDirectory()) {
            encryptedFiles.push(...collectEncryptedAssets(rendererDirectory, absolutePath));
            continue;
        }
        if (!entry.isFile() || !/\.(?:js|css|html)\.enc$/i.test(entry.name)) continue;

        const relativePath = path.relative(rendererDirectory, absolutePath).split(path.sep).join('/');
        if (!relativePath || relativePath.startsWith('../') || path.posix.normalize(relativePath) !== relativePath) {
            throw new Error('客户端资源路径无效');
        }
        encryptedFiles.push({ absolutePath, relativePath });
    }
    return encryptedFiles;
}

async function loadProtectedClientAssets(rendererDirectory, options = {}) {
    const keyPath = options.keyPath || path.join(path.dirname(rendererDirectory), 'client-code-key.json');
    let keyDocument;
    try {
        const stat = fs.statSync(keyPath);
        if (!stat.isFile() || stat.size <= 0 || stat.size > MAX_KEY_DOCUMENT_BYTES) {
            throw new Error('安装包中的客户端密钥文件大小或类型无效');
        }
        keyDocument = JSON.parse(fs.readFileSync(keyPath, 'utf8'));
    } catch (error) {
        throw new Error(`无法读取安装包中的客户端密钥：${error.message}`);
    }
    const { key, keyId } = validateKeyDocument(keyDocument);
    try {
        const encryptedFiles = collectEncryptedAssets(rendererDirectory);
        if (encryptedFiles.length === 0) {
            throw new Error('安装包中没有找到已加密的客户端资源');
        }

        const assets = new Map();
        for (const { absolutePath, relativePath } of encryptedFiles) {
            if (!/\.(?:js|css|html)\.enc$/i.test(relativePath)) {
                throw new Error(`客户端资源扩展名无效：${relativePath}`);
            }
            const source = decryptClientAsset(fs.readFileSync(absolutePath), relativePath, key, keyId);
            assets.set(relativePath, source);
        }
        if (!assets.has('index.html.enc')) {
            throw new Error('安装包中没有找到已加密的客户端 HTML');
        }
        if (![...assets.keys()].some(relativePath => relativePath.endsWith('.css.enc'))) {
            throw new Error('安装包中没有找到已加密的客户端 CSS');
        }
        if (![...assets.keys()].some(relativePath => relativePath.endsWith('.js.enc'))) {
            throw new Error('安装包中没有找到已加密的客户端脚本');
        }
        return assets;
    } finally {
        key.fill(0);
    }
}

module.exports = {
    REMOTE_KEY_URL,
    decryptClientAsset,
    // Keep the script-named export for existing callers that verify the
    // envelope format independently of the protected asset type.
    decryptClientScript: decryptClientAsset,
    fetchClientCodeKey,
    loadProtectedClientAssets,
    validateKeyDocument,
};
