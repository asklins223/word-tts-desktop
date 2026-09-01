'use strict';

// CI-only black-box verification for the custom Windows Setup.exe route.
// It deliberately exercises the same updater client used by the desktop app:
// the old local uninstaller is copied to a temporary install and the new
// artifact is served by a local HTTP server with real 206 Range responses.

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const fsp = fs.promises;
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');

const rootDir = path.resolve(__dirname, '..');
const { buildBlockMap } = require(path.join(
    rootDir,
    'electron',
    'node_modules',
    'app-builder-lib',
    'out',
    'targets',
    'blockmap',
    'blockmap',
));
const {
    createWindowsUpdateClient,
} = require(path.join(rootDir, 'electron', 'windows-update-client'));

function argumentValue(argv, name) {
    const index = argv.indexOf(name);
    if (index >= 0) {
        const value = argv[index + 1];
        if (!value || value.startsWith('--')) throw new Error(`${name} 后面必须提供参数值`);
        return value;
    }
    const prefix = `${name}=`;
    const inline = argv.find(value => value.startsWith(prefix));
    if (inline === undefined) return undefined;
    const value = inline.slice(prefix.length);
    if (!value) throw new Error(`${name} 后面必须提供参数值`);
    return value;
}

function requiredArgument(argv, name) {
    const value = argumentValue(argv, name);
    if (!value) throw new Error(`缺少 ${name}`);
    return value;
}

function decodeBlockMap(bytes, label, expectedSize = null) {
    let decoded = Buffer.from(bytes);
    try {
        decoded = zlib.gunzipSync(decoded);
    } catch (error) {
        if (decoded[0] !== 0x7b && decoded[0] !== 0x5b) {
            throw new Error(`${label} 不是有效的 gzip/JSON blockmap: ${error.message}`);
        }
    }
    let blockMap;
    try {
        blockMap = JSON.parse(decoded.toString('utf8'));
    } catch (error) {
        throw new Error(`${label} JSON 解析失败: ${error.message}`);
    }
    const file = blockMap?.files?.length === 1 ? blockMap.files[0] : null;
    assert.ok(file, `${label} 必须只包含一个文件条目`);
    assert.equal(Number(file.offset || 0), 0, `${label} offset 必须为 0`);
    assert.ok(Array.isArray(file.sizes) && file.sizes.length > 0, `${label} 缺少 sizes`);
    assert.equal(file.sizes.length, file.checksums?.length, `${label} sizes/checksums 数量不一致`);
    assert.ok(file.sizes.every(size => Number.isSafeInteger(Number(size)) && Number(size) > 0), `${label} 含无效分块大小`);
    if (expectedSize !== null) {
        assert.equal(
            file.sizes.reduce((sum, size) => sum + Number(size), 0),
            Number(expectedSize),
            `${label} 分块没有覆盖完整安装包`,
        );
    }
    assert.ok(file.checksums.every(checksum => typeof checksum === 'string' && checksum.length > 0), `${label} 含无效校验值`);
    return blockMap;
}

async function fileDigest(filePath) {
    const hash = crypto.createHash('sha512');
    const stream = fs.createReadStream(filePath);
    let size = 0;
    for await (const chunk of stream) {
        size += chunk.length;
        hash.update(chunk);
    }
    return { size, sha512: hash.digest('base64') };
}

async function containsBytes(filePath, needle) {
    const wanted = Buffer.from(needle);
    const stream = fs.createReadStream(filePath, { highWaterMark: 256 * 1024 });
    let carry = Buffer.alloc(0);
    for await (const chunk of stream) {
        const data = Buffer.concat([carry, chunk]);
        if (data.includes(wanted)) return true;
        carry = data.subarray(Math.max(0, data.length - wanted.length + 1));
    }
    return false;
}

function blockMapExpectedSize(blockMap) {
    return blockMap.files[0].sizes.reduce((sum, size) => sum + Number(size), 0);
}

function blockOffset(blockMap, index) {
    const file = blockMap.files[0];
    return Number(file.offset || 0) + file.sizes
        .slice(0, index)
        .reduce((sum, size) => sum + Number(size), 0);
}

function createRangeServer({ installerPath, newBlockMapBytes, oldBlockMapBytes, version }) {
    const metrics = {
        rangeRequests: 0,
        rangeBytes: 0,
        fullArtifactRequests: 0,
        blockMapRequests: 0,
    };
    const newArtifactName = `wordTTS-Setup-${version}-x64.exe`;
    const oldArtifactName = 'wordTTS-Setup-0.0.0-x64.exe';
    const newBlockMapName = `${newArtifactName}.blockmap`;
    const oldBlockMapName = `${oldArtifactName}.blockmap`;
    const server = http.createServer((request, response) => {
        const requestPath = decodeURIComponent(String(request.url || '').split('?', 1)[0]);
        const fileName = path.posix.basename(requestPath);
        if (fileName === newBlockMapName || fileName === oldBlockMapName) {
            metrics.blockMapRequests += 1;
            const bytes = fileName === oldBlockMapName ? oldBlockMapBytes : newBlockMapBytes;
            response.writeHead(200, {
                'Content-Length': bytes.length,
                'Content-Type': 'application/octet-stream',
                'Cache-Control': 'no-store',
            });
            response.end(bytes);
            return;
        }
        if (fileName !== newArtifactName && fileName !== oldArtifactName) {
            response.writeHead(404);
            response.end();
            return;
        }

        const stat = fs.statSync(installerPath);
        const range = String(request.headers.range || '').match(/^bytes=(\d+)-(\d+)$/i);
        if (!range) {
            metrics.fullArtifactRequests += 1;
            response.writeHead(200, {
                'Content-Length': stat.size,
                'Content-Type': 'application/octet-stream',
                'Cache-Control': 'no-store',
            });
            fs.createReadStream(installerPath).pipe(response);
            return;
        }

        const start = Number(range[1]);
        const end = Number(range[2]);
        if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end < start || end >= stat.size) {
            response.writeHead(416, { 'Content-Range': `bytes */${stat.size}` });
            response.end();
            return;
        }
        metrics.rangeRequests += 1;
        metrics.rangeBytes += end - start + 1;
        response.writeHead(206, {
            'Content-Length': end - start + 1,
            'Content-Range': `bytes ${start}-${end}/${stat.size}`,
            'Accept-Ranges': 'bytes',
            'Content-Type': 'application/octet-stream',
            'Cache-Control': 'no-store',
        });
        fs.createReadStream(installerPath, { start, end }).pipe(response);
    });
    return { server, metrics };
}

async function main(argv = process.argv.slice(2)) {
    const installerPath = path.resolve(requiredArgument(argv, '--installer'));
    const blockmapPath = path.resolve(requiredArgument(argv, '--blockmap'));
    const version = requiredArgument(argv, '--version').replace(/^v/i, '');
    assert.ok(fs.existsSync(installerPath), `安装包不存在: ${installerPath}`);
    assert.ok(fs.existsSync(blockmapPath), `blockmap 不存在: ${blockmapPath}`);

    const installerDigest = await fileDigest(installerPath);
    assert.ok(installerDigest.size > 0, '安装包不能为空');
    assert.ok(await containsBytes(installerPath, 'wordtts-payload.7z'), 'Setup.exe 未嵌入 lazy payload');
    assert.ok(await containsBytes(installerPath, 'wordtts-7za.exe'), 'Setup.exe 未嵌入 payload 解压器');

    const newBlockMapBytes = await fsp.readFile(blockmapPath);
    const newBlockMap = decodeBlockMap(newBlockMapBytes, '新版本 blockmap', installerDigest.size);

    const tempRoot = await fsp.mkdtemp(path.join(os.tmpdir(), 'wordtts-windows-update-audit-'));
    const oldInstallerPath = path.join(tempRoot, '小猪wordTTS-uninstaller.exe');
    const oldBlockMapPath = path.join(tempRoot, 'old.blockmap');
    const tempDownloadDir = path.join(tempRoot, 'downloads');
    let server;
    let client;
    try {
        await fsp.copyFile(installerPath, oldInstallerPath);
        const blockIndex = Math.floor(newBlockMap.files[0].sizes.length / 2);
        const mutationOffset = blockOffset(newBlockMap, blockIndex)
            + Math.floor(Number(newBlockMap.files[0].sizes[blockIndex]) / 2);
        const handle = await fsp.open(oldInstallerPath, 'r+');
        try {
            const byte = Buffer.alloc(1);
            await handle.read(byte, 0, 1, mutationOffset);
            byte[0] ^= 0x5a;
            await handle.write(byte, 0, 1, mutationOffset);
        } finally {
            await handle.close();
        }
        await buildBlockMap(oldInstallerPath, 'gzip', oldBlockMapPath);
        const oldBlockMapBytes = await fsp.readFile(oldBlockMapPath);
        decodeBlockMap(oldBlockMapBytes, '旧版本 blockmap', installerDigest.size);

        const rangeServer = createRangeServer({
            installerPath,
            newBlockMapBytes,
            oldBlockMapBytes,
            version,
        });
        server = rangeServer.server;
        await new Promise((resolve, reject) => {
            server.once('error', reject);
            server.listen(0, '127.0.0.1', resolve);
        });
        const address = server.address();
        const baseUrl = `http://127.0.0.1:${address.port}/repo`;
        const artifactUrl = `${baseUrl}/releases/download/v${version}/wordTTS-Setup-${version}-x64.exe`;
        const blockmapUrl = `${artifactUrl}.blockmap`;
        client = createWindowsUpdateClient({
            releaseUrl: `${baseUrl}/releases/tag/v${version}`,
            currentVersion: '0.0.0',
            currentInstallerPath: oldInstallerPath,
            tempDirectory: tempDownloadDir,
            fetchImpl: globalThis.fetch,
            logger: { info() {}, warn() {}, debug() {} },
        });
        const downloadedPath = await client.download({
            platform: 'win32',
            version,
            tag: `v${version}`,
            artifact: {
                url: artifactUrl,
                blockmap: blockmapUrl,
                sha512: installerDigest.sha512,
                size: installerDigest.size,
            },
        });
        const downloadedDigest = await fileDigest(downloadedPath);
        assert.deepEqual(downloadedDigest, installerDigest, '差分下载结果与新 Setup.exe 不一致');
        assert.ok(rangeServer.metrics.blockMapRequests >= 2, '差分下载没有读取新旧 blockmap');
        assert.ok(rangeServer.metrics.rangeRequests > 0, '差分下载没有产生 HTTP Range 请求');
        assert.equal(rangeServer.metrics.fullArtifactRequests, 0, '差分校验退化成了全量安装包请求');
        assert.ok(
            rangeServer.metrics.rangeBytes < installerDigest.size * 0.95,
            `差分下载量过大: ${rangeServer.metrics.rangeBytes}/${installerDigest.size}`,
        );
        console.log(JSON.stringify({
            installerBytes: installerDigest.size,
            blockCount: newBlockMap.files[0].sizes.length,
            rangeRequests: rangeServer.metrics.rangeRequests,
            rangeBytes: rangeServer.metrics.rangeBytes,
            rangePercent: Number(((rangeServer.metrics.rangeBytes / installerDigest.size) * 100).toFixed(3)),
            blockMapRequests: rangeServer.metrics.blockMapRequests,
            fullArtifactRequests: rangeServer.metrics.fullArtifactRequests,
            result: 'PASS',
        }, null, 2));
    } finally {
        client?.dispose();
        if (server) {
            await new Promise(resolve => {
                server.close(resolve);
                server.closeAllConnections?.();
            });
        }
        await fsp.rm(tempRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    }
}

if (require.main === module) {
    main().catch(error => {
        console.error(`Windows 安装包/增量更新审计失败: ${error.stack || error.message || error}`);
        process.exitCode = 1;
    });
}

module.exports = {
    blockMapExpectedSize,
    decodeBlockMap,
    main,
};
