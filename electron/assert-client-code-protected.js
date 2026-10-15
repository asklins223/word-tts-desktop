'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

module.exports = async function assertClientCodeProtected(context) {
    const appDirectory = context?.packager?.info?.appDir || context?.packager?.appDir;
    if (!appDirectory) throw new Error('无法检查客户端代码保护构建目录');

    const rendererDirectory = path.join(appDirectory, 'renderer');
    const loaderSource = 'modules/app/client-code-loader.js';
    const htmlPath = path.join(rendererDirectory, 'index.html');
    const keyPath = path.join(appDirectory, 'client-code-key.json');
    const html = fs.readFileSync(htmlPath, 'utf8');
    const scriptElements = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)];
    const scriptOpeningCount = [...html.matchAll(/<script\b/gi)].length;
    const scriptClosingCount = [...html.matchAll(/<\/script\s*>/gi)].length;
    const loaderPath = path.join(rendererDirectory, loaderSource);

    if (!/<html\b[^>]*data-protected-bootstrap="true"/.test(html)
        || Buffer.byteLength(html, 'utf8') > 4096
        || /<(?:div|header|main|nav|section|form)\b/i.test(html)
        || /<link\b/i.test(html)) {
        throw new Error('Electron 打包已阻止：index.html 必须是只含加载器的最小启动壳。');
    }
    if (scriptOpeningCount !== scriptElements.length || scriptClosingCount !== scriptElements.length
        || scriptElements.length !== 1 || scriptElements[0][2].trim() !== '') {
        throw new Error('Electron 打包已阻止：明文启动壳包含无法识别或内联的 script 标签。');
    }
    if (scriptElements[0][1].trim() !== `src="${loaderSource}"`) {
        throw new Error('Electron 打包已阻止：明文启动壳只能加载客户端资源加载器。');
    }
    if (!/script-src\s+'self'\s+blob:/.test(html) || !/style-src\s+'self'\s+'unsafe-inline'/.test(html)) {
        throw new Error('Electron 打包已阻止：客户端资源加载所需的 CSP 配置缺失。');
    }
    if (!fs.existsSync(path.join(rendererDirectory, 'index.html.enc'))) {
        throw new Error('Electron 打包已阻止：缺少加密后的客户端 HTML。');
    }
    if (!fs.existsSync(loaderPath)) {
        throw new Error('Electron 打包已阻止：缺少客户端资源加载器。');
    }
    if (!fs.existsSync(keyPath)) {
        throw new Error('Electron 打包已阻止：缺少随应用打包的客户端密钥。');
    }
    let keyDocument;
    try {
        keyDocument = JSON.parse(fs.readFileSync(keyPath, 'utf8'));
    } catch (_) {
        throw new Error('Electron 打包已阻止：客户端密钥文件不是有效 JSON。');
    }
    if (keyDocument?.version !== 1 || keyDocument?.algorithm !== 'AES-256-GCM'
        || typeof keyDocument.keyHex !== 'string' || !/^[a-f0-9]{64}$/i.test(keyDocument.keyHex)) {
        throw new Error('Electron 打包已阻止：客户端密钥文件格式无效。');
    }
    const expectedKeyId = crypto.createHash('sha256')
        .update(Buffer.from(keyDocument.keyHex, 'hex'))
        .digest('hex')
        .slice(0, 16);
    if (typeof keyDocument.keyId !== 'string' || keyDocument.keyId.toLowerCase() !== expectedKeyId) {
        throw new Error('Electron 打包已阻止：客户端密钥标识与密钥内容不匹配。');
    }

    const encryptedPaths = [];
    const plaintextPaths = [];
    function collectRendererAssets(directory) {
        for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
            const absolutePath = path.join(directory, entry.name);
            if (entry.isSymbolicLink()) {
                throw new Error('Electron 打包已阻止：renderer 目录包含不受支持的符号链接。');
            }
            if (entry.isDirectory()) {
                collectRendererAssets(absolutePath);
                continue;
            }
            if (!entry.isFile()) continue;

            const relativePath = path.relative(rendererDirectory, absolutePath).split(path.sep).join('/');
            if (/\.(?:html|css|m?js|cjs)\.enc$/i.test(entry.name)) encryptedPaths.push(relativePath);
            if (/\.(?:html|css|m?js|cjs)$/i.test(entry.name)
                && relativePath !== 'index.html'
                && relativePath !== loaderSource) {
                plaintextPaths.push(relativePath);
            }
        }
    }
    collectRendererAssets(rendererDirectory);

    const htmlAssets = encryptedPaths.filter(relativePath => relativePath.endsWith('.html.enc'));
    const styleAssets = encryptedPaths.filter(relativePath => relativePath.endsWith('.css.enc'));
    const scriptAssets = encryptedPaths.filter(relativePath => /\.js\.enc$/i.test(relativePath));
    if (htmlAssets.length !== 1 || htmlAssets[0] !== 'index.html.enc'
        || styleAssets.length === 0 || scriptAssets.length === 0) {
        throw new Error('Electron 打包已阻止：安装包缺少完整的 HTML、CSS 或 JavaScript 加密资源。');
    }
    if (plaintextPaths.length > 0) {
        throw new Error(`Electron 打包已阻止：renderer 目录仍包含明文客户端代码（${plaintextPaths.slice(0, 3).join(', ')}）。`);
    }
};
