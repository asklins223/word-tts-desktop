#!/usr/bin/env node

const fs = require('fs');
const os = require('os');
const path = require('path');

const backendRoot = process.argv[2];
if (!backendRoot) {
    console.error('用法: node scripts/stage_playwright_browser.js <server_backend目录>');
    process.exit(2);
}

const cacheCandidates = [
    process.env.PLAYWRIGHT_BROWSERS_PATH,
    process.platform === 'darwin'
        ? path.join(os.homedir(), 'Library', 'Caches', 'ms-playwright')
        : null,
    process.platform === 'win32' && process.env.LOCALAPPDATA
        ? path.join(process.env.LOCALAPPDATA, 'ms-playwright')
        : null,
    path.join(os.homedir(), '.cache', 'ms-playwright'),
].filter(Boolean);

let browserSource = null;
for (const cacheRoot of cacheCandidates) {
    if (!fs.existsSync(cacheRoot)) continue;
    const chromiumDirs = fs.readdirSync(cacheRoot, { withFileTypes: true })
        .filter((entry) => entry.isDirectory() && /^chromium-\d+$/.test(entry.name))
        .sort((a, b) => Number(b.name.slice(9)) - Number(a.name.slice(9)));
    if (chromiumDirs.length > 0) {
        browserSource = path.join(cacheRoot, chromiumDirs[0].name);
        break;
    }
}

if (!browserSource) {
    console.error('未找到 Playwright Chromium，请先运行: playwright install chromium');
    process.exit(1);
}

const internalDir = path.join(backendRoot, '_internal');
if (!fs.existsSync(internalDir)) {
    console.error(`PyInstaller 后端目录不完整，缺少: ${internalDir}`);
    process.exit(1);
}

const browserRoot = path.join(internalDir, 'playwright_browsers');
const browserDestination = path.join(browserRoot, path.basename(browserSource));
fs.mkdirSync(browserRoot, { recursive: true });
fs.cpSync(browserSource, browserDestination, {
    recursive: true,
    force: true,
    dereference: false,
    preserveTimestamps: true,
    verbatimSymlinks: true,
});

console.log(`[browser] 已原样复制 Chromium: ${browserSource}`);
console.log(`[browser] 目标: ${browserDestination}`);
