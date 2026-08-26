#!/usr/bin/env node

const fs = require('fs');
const os = require('os');
const path = require('path');

const backendRoot = process.argv[2];
if (!backendRoot) {
    console.error('用法: node scripts/stage_playwright_browser.js <server_backend目录>');
    process.exit(2);
}

const internalDir = path.join(backendRoot, '_internal');
if (!fs.existsSync(internalDir)) {
    console.error(`PyInstaller 后端目录不完整，缺少: ${internalDir}`);
    process.exit(1);
}

const browsersJsonPath = path.join(
    internalDir,
    'playwright',
    'driver',
    'package',
    'browsers.json',
);
if (!fs.existsSync(browsersJsonPath)) {
    console.error(`无法确定 Chromium 版本，缺少 Playwright 清单: ${browsersJsonPath}`);
    process.exit(1);
}

let browsersManifest;
try {
    browsersManifest = JSON.parse(fs.readFileSync(browsersJsonPath, 'utf8'));
} catch (error) {
    console.error(`Playwright 清单无法解析: ${browsersJsonPath}`);
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(1);
}

const chromium = Array.isArray(browsersManifest.browsers)
    ? browsersManifest.browsers.find((browser) => browser && browser.name === 'chromium')
    : null;
const chromiumRevision = chromium && String(chromium.revision || '').trim();
if (!chromiumRevision || !/^\d+$/.test(chromiumRevision)) {
    console.error(`Playwright 清单中缺少有效的 chromium revision: ${browsersJsonPath}`);
    process.exit(1);
}

const expectedChromiumDir = `chromium-${chromiumRevision}`;

const cacheCandidates = [
    process.env.PLAYWRIGHT_BROWSERS_PATH !== '0'
        ? process.env.PLAYWRIGHT_BROWSERS_PATH
        : null,
    process.platform === 'darwin'
        ? path.join(os.homedir(), 'Library', 'Caches', 'ms-playwright')
        : null,
    process.platform === 'win32' && process.env.LOCALAPPDATA
        ? path.join(process.env.LOCALAPPDATA, 'ms-playwright')
        : null,
    path.join(os.homedir(), '.cache', 'ms-playwright'),
].filter(Boolean).filter((candidate, index, candidates) => (
    candidates.indexOf(candidate) === index
));

let browserSource = null;
for (const cacheRoot of cacheCandidates) {
    const candidate = path.join(cacheRoot, expectedChromiumDir);
    if (fs.existsSync(candidate) && fs.statSync(candidate).isDirectory()) {
        browserSource = candidate;
        break;
    }
}

if (!browserSource) {
    const checkedLocations = cacheCandidates
        .map((cacheRoot) => `  - ${path.join(cacheRoot, expectedChromiumDir)}`)
        .join('\n');
    console.error(
        `未找到后端 Playwright 要求的 Chromium revision ${chromiumRevision}。\n`
        + `清单: ${browsersJsonPath}\n`
        + `已检查:\n${checkedLocations}\n`
        + '请使用构建后端的同一 Python 环境运行: playwright install chromium',
    );
    process.exit(1);
}

function normalizedLocaleName(name, extension) {
    const basename = path.basename(name);
    return basename.slice(0, -extension.length).toLowerCase().replace(/-/g, '_');
}

function shouldKeepLocale(locale) {
    return locale === 'base'
        || locale === 'en'
        || locale.startsWith('en_')
        || locale === 'zh_cn'
        || locale.startsWith('zh_cn_')
        || locale === 'zh_hans'
        || locale.startsWith('zh_hans_');
}

function isLocalePak(name) {
    return /^[a-z]{2,3}(?:[-_][a-z0-9]{2,16})*\.pak$/i.test(name);
}

function pruneChromiumLocales(root) {
    let removed = 0;

    function visit(directory) {
        for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
            const entryPath = path.join(directory, entry.name);

            if (/\.lproj$/i.test(entry.name)
                && (entry.isDirectory() || entry.isSymbolicLink())) {
                const locale = normalizedLocaleName(entry.name, '.lproj');
                if (!shouldKeepLocale(locale)) {
                    fs.rmSync(entryPath, { recursive: true, force: true });
                    removed += 1;
                }
                continue;
            }

            if (entry.isDirectory() && entry.name.toLowerCase() === 'locales') {
                for (const localeEntry of fs.readdirSync(entryPath, { withFileTypes: true })) {
                    if (!localeEntry.isFile() || !isLocalePak(localeEntry.name)) continue;
                    const locale = normalizedLocaleName(localeEntry.name, '.pak');
                    if (!shouldKeepLocale(locale)) {
                        fs.rmSync(path.join(entryPath, localeEntry.name), { force: true });
                        removed += 1;
                    }
                }
            }

            if (entry.isDirectory()) visit(entryPath);
        }
    }

    visit(root);
    return removed;
}

function pruneChromiumOptionalPayload(root) {
    // 讯飞自动化只需要普通网页渲染，不播放 DRM 内容。Chromium 自带的
    // Widevine 目录在 macOS/Windows 上约占 20MB，删除它不会影响登录、
    // 页面操作或音频生成。
    let removed = 0;

    function visit(directory) {
        for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
            const entryPath = path.join(directory, entry.name);
            if (entry.name.toLowerCase() === 'widevinecdm') {
                fs.rmSync(entryPath, { recursive: true, force: true });
                removed += 1;
                continue;
            }
            if (entry.isDirectory()) visit(entryPath);
        }
    }

    visit(root);
    return removed;
}

function prunePlaywrightUnusedPayload(driverPackageRoot) {
    // Python 端只通过 sync_api 启动 Playwright driver 和 Chromium，不使用
    // trace viewer、录制器、HTML 报告、TypeScript 类型或开发期协议资料。
    // 保留 cli.js、coreBundle.js、utilsBundle.js、browsers.json 等运行时文件。
    const removable = [
        path.join(driverPackageRoot, 'lib', 'vite'),
        path.join(driverPackageRoot, 'types'),
        path.join(driverPackageRoot, 'api.json'),
        path.join(driverPackageRoot, 'protocol.yml'),
    ];
    let removed = 0;
    for (const target of removable) {
        if (!fs.existsSync(target)) continue;
        fs.rmSync(target, { recursive: true, force: true });
        removed += 1;
    }
    return removed;
}

const browserRoot = path.join(internalDir, 'playwright_browsers');
const browserDestination = path.join(browserRoot, expectedChromiumDir);
if (path.resolve(browserSource) === path.resolve(browserDestination)) {
    console.error(`Chromium 缓存目录不能与打包目标相同: ${browserDestination}`);
    process.exit(1);
}

fs.mkdirSync(browserRoot, { recursive: true });

for (const entry of fs.readdirSync(browserRoot, { withFileTypes: true })) {
    if (entry.isDirectory() && /^chromium-\d+$/.test(entry.name)) {
        fs.rmSync(path.join(browserRoot, entry.name), { recursive: true, force: true });
    }
}

fs.cpSync(browserSource, browserDestination, {
    recursive: true,
    force: true,
    dereference: false,
    preserveTimestamps: true,
    verbatimSymlinks: true,
});

const removedLocales = pruneChromiumLocales(browserDestination);
const removedChromiumPayload = pruneChromiumOptionalPayload(browserDestination);
const removedPlaywrightPayload = prunePlaywrightUnusedPayload(
    path.join(internalDir, 'playwright', 'driver', 'package')
);

console.log(`[browser] 已复制 Chromium revision ${chromiumRevision}: ${browserSource}`);
console.log(`[browser] 目标: ${browserDestination}`);
console.log(`[browser] 已移除 ${removedLocales} 个非中文简体/英文语言资源`);
console.log(`[browser] 已移除 ${removedChromiumPayload} 个 Chromium 可选目录`);
console.log(`[browser] 已移除 ${removedPlaywrightPayload} 个 Playwright 非运行时目录/文件`);
