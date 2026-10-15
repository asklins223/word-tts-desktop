#!/usr/bin/env node
'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createRequire } = require('node:module');

const repositoryDirectory = path.resolve(__dirname, '..');
const electronDirectory = path.join(repositoryDirectory, 'electron');
const sourcePackage = JSON.parse(fs.readFileSync(path.join(electronDirectory, 'package.json'), 'utf8'));
const keyDocumentPath = path.join(repositoryDirectory, 'client-code-key.json');
const packageRequire = createRequire(path.join(electronDirectory, 'package.json'));
const JavaScriptObfuscator = packageRequire('javascript-obfuscator');
if (!fs.existsSync(keyDocumentPath)) {
    throw new Error('缺少根目录 client-code-key.json；正式构建与 CI 必须使用随版本发布的客户端密钥文件。');
}
const keyDocument = JSON.parse(fs.readFileSync(keyDocumentPath, 'utf8'));

function validateBuildKey(document) {
    if (document?.version !== 1 || document?.algorithm !== 'AES-256-GCM') {
        throw new Error('client-code-key.json 缺少受支持的算法/版本');
    }
    if (typeof document.keyHex !== 'string' || !/^[a-f0-9]{64}$/i.test(document.keyHex)) {
        throw new Error('client-code-key.json 必须包含 32 字节十六进制密钥');
    }
    const key = Buffer.from(document.keyHex, 'hex');
    const keyId = crypto.createHash('sha256').update(key).digest('hex').slice(0, 16);
    if (String(document.keyId || '').toLowerCase() !== keyId) {
        key.fill(0);
        throw new Error('client-code-key.json 的 keyId 与密钥内容不匹配');
    }
    return { key, keyId };
}

const { key, keyId } = validateBuildKey(keyDocument);

function copyDirectory(sourceDirectory, destinationDirectory) {
    fs.mkdirSync(destinationDirectory, { recursive: true });
    for (const entry of fs.readdirSync(sourceDirectory, { withFileTypes: true })) {
        const sourcePath = path.join(sourceDirectory, entry.name);
        const destinationPath = path.join(destinationDirectory, entry.name);
        if (entry.isDirectory()) {
            copyDirectory(sourcePath, destinationPath);
        } else if (entry.isFile()) {
            fs.copyFileSync(sourcePath, destinationPath);
        } else {
            throw new Error(`不支持的 Electron 构建文件类型：${sourcePath}`);
        }
    }
}

function obfuscatorOptions(relativePath, target) {
    const prefix = `_w${crypto.createHash('sha256').update(relativePath).digest('hex').slice(0, 10)}_`;
    return {
        compact: true,
        controlFlowFlattening: false,
        deadCodeInjection: false,
        debugProtection: false,
        disableConsoleOutput: false,
        identifierNamesGenerator: 'hexadecimal',
        identifiersPrefix: prefix,
        numbersToExpressions: false,
        renameGlobals: false,
        renameProperties: false,
        selfDefending: false,
        simplify: true,
        stringArray: true,
        stringArrayEncoding: ['rc4'],
        stringArrayThreshold: 0.75,
        target,
    };
}

function encryptSource(source, relativePath) {
    const iv = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
    cipher.setAAD(Buffer.from(`${relativePath}\0${keyId}`, 'utf8'));
    const encrypted = Buffer.concat([
        cipher.update(Buffer.from(source, 'utf8')),
        cipher.final(),
    ]);
    const header = Buffer.concat([
        Buffer.from('WTC1', 'ascii'),
        Buffer.from(keyId, 'hex'),
        iv,
        cipher.getAuthTag(),
    ]);
    return Buffer.concat([header, encrypted]);
}

function protectRenderer(appDirectory) {
    const rendererDirectory = path.join(appDirectory, 'renderer');
    const htmlPath = path.join(rendererDirectory, 'index.html');
    let html = fs.readFileSync(htmlPath, 'utf8');
    const loaderSource = 'modules/app/client-code-loader.js';
    const protectedScripts = new Set();
    const protectedStyles = new Set();
    const encryptedScripts = new Set();
    const encryptedStyles = new Set();
    let loaderCount = 0;

    html = html.replace(/<link\s+rel="stylesheet"\s+href="([^"]+)"\s*>/g, (tag, source) => {
        if (!source.endsWith('.css') || source.startsWith('/') || source.includes('..') || source.includes('\\')) {
            throw new Error(`无法安全保护 renderer 样式引用：${source}`);
        }
        const absolutePath = path.resolve(rendererDirectory, source);
        const relative = path.relative(rendererDirectory, absolutePath).split(path.sep).join('/');
        if (!absolutePath.startsWith(`${rendererDirectory}${path.sep}`) || !fs.existsSync(absolutePath)) {
            throw new Error(`renderer 样式不存在或路径越界：${source}`);
        }
        protectedStyles.add(relative);
        return `<link data-protected-style="${relative}.enc">`;
    });

    html = html.replace(/<script\s+src="([^"]+)"><\/script>/g, (tag, source) => {
        if (source === loaderSource) {
            loaderCount += 1;
            return '';
        }
        if (!source.endsWith('.js') || source.startsWith('/') || source.includes('..') || source.includes('\\')) {
            throw new Error(`无法安全保护 renderer 脚本引用：${source}`);
        }
        const absolutePath = path.resolve(rendererDirectory, source);
        const relative = path.relative(rendererDirectory, absolutePath).split(path.sep).join('/');
        if (!absolutePath.startsWith(`${rendererDirectory}${path.sep}`) || !fs.existsSync(absolutePath)) {
            throw new Error(`renderer 脚本不存在或路径越界：${source}`);
        }
        protectedScripts.add(relative);
        return `<script data-protected-src="${relative}.enc"></script>`;
    });

    if (protectedScripts.size === 0) throw new Error('renderer/index.html 中没有可保护的脚本');
    if (protectedStyles.size === 0) throw new Error('renderer/index.html 中没有可保护的样式');
    if (loaderCount !== 1) throw new Error('renderer/index.html 必须恰好引用一次客户端代码加载器');

    function encryptRendererDirectory(directory) {
        for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
            const absolutePath = path.join(directory, entry.name);
            if (entry.isDirectory()) {
                encryptRendererDirectory(absolutePath);
                continue;
            }
            if (!entry.isFile() || absolutePath === path.join(rendererDirectory, 'index.html')) {
                continue;
            }

            const relative = path.relative(rendererDirectory, absolutePath).split(path.sep).join('/');
            const isScript = entry.name.endsWith('.js') && absolutePath !== path.join(rendererDirectory, loaderSource);
            const isStyle = entry.name.endsWith('.css');
            if (!isScript && !isStyle) continue;

            const source = fs.readFileSync(absolutePath, 'utf8');
            if (isScript) encryptedScripts.add(relative);
            if (isStyle) encryptedStyles.add(relative);
            const protectedSource = isScript
                ? JavaScriptObfuscator.obfuscate(source, obfuscatorOptions(relative, 'browser-no-eval')).getObfuscatedCode()
                : source;
            const encryptedRelative = `${relative}.enc`;
            const encryptedPath = path.join(rendererDirectory, ...encryptedRelative.split('/'));
            fs.mkdirSync(path.dirname(encryptedPath), { recursive: true });
            fs.writeFileSync(encryptedPath, encryptSource(protectedSource, encryptedRelative));
            fs.unlinkSync(absolutePath);
        }
    }

    encryptRendererDirectory(rendererDirectory);
    const samePaths = (left, right) => JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
    if (!samePaths(encryptedScripts, protectedScripts)) {
        throw new Error('renderer/index.html 的脚本引用与待加密脚本文件不一致');
    }
    if (!samePaths(encryptedStyles, protectedStyles)) {
        throw new Error('renderer/index.html 的样式引用与待加密 CSS 文件不一致');
    }
    const scriptElements = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)];
    const scriptOpeningCount = [...html.matchAll(/<script\b/gi)].length;
    const scriptClosingCount = [...html.matchAll(/<\/script\s*>/gi)].length;
    if (scriptOpeningCount !== scriptElements.length || scriptClosingCount !== scriptElements.length
        || scriptElements.length !== protectedScripts.size
        || scriptElements.some(([, attributes, body]) => {
            const match = attributes.trim().match(/^data-protected-src="([a-zA-Z0-9._/-]+\.js\.enc)"$/);
            return body.trim() !== '' || !match || !protectedScripts.has(match[1].slice(0, -'.enc'.length));
        })
        || /<link\b[^>]*\brel="stylesheet"/i.test(html)) {
        throw new Error('renderer/index.html 仍包含未保护的脚本或样式引用');
    }
    html = html.replace("script-src 'self';", "script-src 'self' blob:;");
    if (!/script-src\s+'self'\s+blob:/.test(html)) {
        throw new Error('renderer/index.html 的 CSP 不允许运行时装入受保护脚本');
    }
    const encryptedHtmlRelative = 'index.html.enc';
    fs.writeFileSync(path.join(rendererDirectory, encryptedHtmlRelative), encryptSource(html, encryptedHtmlRelative));
    fs.writeFileSync(htmlPath, [
        '<!DOCTYPE html>',
        '<html lang="zh-CN" data-protected-bootstrap="true">',
        '<head>',
        '    <meta charset="UTF-8">',
        '    <meta http-equiv="Content-Security-Policy" content="default-src \'self\'; base-uri \'none\'; object-src \'none\'; script-src \'self\' blob:; style-src \'self\' \'unsafe-inline\'; img-src \'self\' data: blob: https://openstorage.xfyousheng.com https://pygfile.peiyinge.com; media-src \'self\' blob: data: https://pygfile.peiyinge.com http://ceph.xfyousheng.com; font-src \'self\' data:; connect-src \'self\' blob:\">',
        '</head>',
        '<body>',
        `    <script src="${loaderSource}"></script>`,
        '</body>',
        '</html>',
        '',
    ].join('\n'), 'utf8');
    return { scriptCount: protectedScripts.size, styleCount: protectedStyles.size };
}

function protectMainProcessScripts(appDirectory, filePatterns) {
    for (const relativePath of filePatterns.filter(file => file.endsWith('.js'))) {
        const absolutePath = path.join(appDirectory, relativePath);
        const obfuscated = JavaScriptObfuscator.obfuscate(
            fs.readFileSync(absolutePath, 'utf8'),
            obfuscatorOptions(relativePath, 'node'),
        ).getObfuscatedCode();
        fs.writeFileSync(absolutePath, obfuscated, 'utf8');
    }
}

function stageApplication(appDirectory, { ciSmokeBuild = false } = {}) {
    fs.mkdirSync(appDirectory, { recursive: true });
    for (const pattern of sourcePackage.build.files) {
        if (pattern === 'renderer/**/*') {
            copyDirectory(path.join(electronDirectory, 'renderer'), path.join(appDirectory, 'renderer'));
        } else if (pattern === 'client-code-key.json') {
            fs.copyFileSync(keyDocumentPath, path.join(appDirectory, pattern));
        } else if (!pattern.includes('*')) {
            fs.copyFileSync(path.join(electronDirectory, pattern), path.join(appDirectory, pattern));
        } else {
            throw new Error(`构建文件清单暂不支持该模式：${pattern}`);
        }
    }
    if (ciSmokeBuild) {
        const mainPath = path.join(appDirectory, 'main.js');
        const mainSource = fs.readFileSync(mainPath, 'utf8');
        const productionMarker = 'const CI_SMOKE_BUILD = false;';
        if (mainSource.split(productionMarker).length !== 2) {
            throw new Error('主进程缺少唯一的正式包 CI smoke 禁用标记');
        }
        fs.writeFileSync(mainPath, mainSource.replace(productionMarker, 'const CI_SMOKE_BUILD = true;'), 'utf8');
    }
    const appMetadata = {
        name: sourcePackage.name,
        version: sourcePackage.version,
        description: sourcePackage.description,
        main: sourcePackage.main,
        author: sourcePackage.author,
        license: sourcePackage.license,
        dependencies: sourcePackage.dependencies,
    };
    fs.writeFileSync(path.join(appDirectory, 'package.json'), `${JSON.stringify(appMetadata, null, 2)}\n`, 'utf8');
    const protectedRendererAssets = protectRenderer(appDirectory);
    protectMainProcessScripts(appDirectory, sourcePackage.build.files);
    return protectedRendererAssets;
}

function main() {
    if (!Array.isArray(sourcePackage.build?.files)) throw new Error('electron/package.json 缺少构建文件清单');
    const ciSmokeBuild = process.argv.includes('--ci-smoke-build');
    const builderArguments = process.argv.slice(2).filter(argument => argument !== '--ci-smoke-build');
    if (ciSmokeBuild) {
        if (builderArguments.some(argument => /^--config(?:$|[.=])|^-c(?:$|[.=])/i.test(argument))) {
            throw new Error('CI smoke 包不允许覆盖独立输出目录');
        }
        builderArguments.push('--config.directories.output=release-smoke');
    }
    const stageRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'wordtts-protected-build-'));
    const appDirectory = path.join(stageRoot, 'app');
    let exitCode = 1;
    try {
        let protectedRendererAssets;
        try {
            protectedRendererAssets = stageApplication(appDirectory, { ciSmokeBuild });
        } finally {
            key.fill(0);
        }
        console.log(`[构建] 已混淆主进程代码，并加密 ${protectedRendererAssets.scriptCount} 个 renderer 脚本、${protectedRendererAssets.styleCount} 个 CSS 文件及 1 个 HTML 文件。`);
        const electronBuilderCli = path.join(electronDirectory, 'node_modules', 'electron-builder', 'cli.js');
        const args = [
            electronBuilderCli,
            ...builderArguments,
            `--config.directories.app=${appDirectory}`,
            `--config.beforePack=${path.join(electronDirectory, 'assert-client-code-protected.js')}`,
            '--config.npmRebuild=false',
        ];
        const result = spawnSync(process.execPath, args, {
            cwd: electronDirectory,
            stdio: 'inherit',
            env: process.env,
        });
        if (result.error) throw result.error;
        exitCode = Number.isInteger(result.status) ? result.status : 1;
    } finally {
        fs.rmSync(stageRoot, { recursive: true, force: true });
    }
    process.exitCode = exitCode;
}

try {
    main();
} catch (error) {
    console.error(`[构建] 客户端混淆/加密失败：${error.stack || error.message}`);
    process.exitCode = 1;
}
