'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { createSourceUploadStaging } = require('../source-staging');

async function makeStaging(overrides = {}) {
    const stagingDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'wordtts-staging-'));
    const staging = createSourceUploadStaging({
        fs,
        path,
        stagingDir,
        logger: { debug: () => {} },
        ...overrides,
    });
    return { staging, stagingDir };
}

const SENDER = 'sender-1';

async function beginDocx(staging, sizeBytes = 10, fileName = 'lesson.docx') {
    return staging.begin({ fileName, sizeBytes, senderId: SENDER });
}

test('begin 校验扩展名与大小上限，拒绝非法输入', async () => {
    const { staging } = await makeStaging();
    await assert.rejects(
        () => staging.begin({ fileName: 'notes.pdf', sizeBytes: 10, senderId: SENDER }),
        /unsupported source document type/,
    );
    await assert.rejects(
        () => staging.begin({ fileName: 'lesson.docx', sizeBytes: 0, senderId: SENDER }),
        /size is invalid/,
    );
    await assert.rejects(
        () => staging.begin({ fileName: 'lesson.docx', sizeBytes: 600 * 1024 * 1024, senderId: SENDER }),
        /exceeds the limit/,
    );
});

test('分块必须按顺序写入，乱序或越界会销毁会话并清理暂存文件', async () => {
    const { staging, stagingDir } = await makeStaging({ chunkSize: 4 });
    const { uploadId } = await beginDocx(staging, 10);
    const stagedPath = path.join(stagingDir, `${uploadId}.docx`);
    assert.ok(fs.existsSync(stagedPath));

    await staging.write({ uploadId, offset: 0, bytes: new Uint8Array([1, 2, 3, 4]) }, SENDER);
    await assert.rejects(
        () => staging.write({ uploadId, offset: 8, bytes: new Uint8Array([9, 10]) }, SENDER),
        /chunks must arrive in order/,
    );
    assert.equal(fs.existsSync(stagedPath), false, '乱序失败后必须删除暂存文件');
    await assert.rejects(
        () => staging.write({ uploadId, offset: 4, bytes: new Uint8Array([5]) }, SENDER),
        /missing or expired/,
    );
});

test('写入超过声明大小立即终止会话', async () => {
    const { staging } = await makeStaging({ chunkSize: 4 });
    const { uploadId } = await beginDocx(staging, 4);
    await assert.rejects(
        () => staging.write({ uploadId, offset: 0, bytes: new Uint8Array([1, 2, 3, 4, 5]) }, SENDER),
        /exceeds the declared size/,
    );
    await assert.rejects(() => staging.complete({ uploadId }, SENDER, { openStagedHandle: async () => ({ success: true }) }), /missing or expired/);
});

test('complete 校验字节总数，成功后返回一次性句柄且内容逐字节一致', async () => {
    const { staging } = await makeStaging({ chunkSize: 4 });
    const { uploadId } = await beginDocx(staging, 10, '课件.docx');
    const payload = Buffer.from('hello word'.split('').map((_, index) => index));
    await staging.write({ uploadId, offset: 0, bytes: new Uint8Array([104, 101, 108, 108]) }, SENDER);
    await staging.write({ uploadId, offset: 4, bytes: new Uint8Array([111, 32, 119, 111]) }, SENDER);
    await staging.write({ uploadId, offset: 8, bytes: new Uint8Array([114, 100]) }, SENDER);

    let verifiedBytes = null;
    const result = await staging.complete({ uploadId }, SENDER, {
        openStagedHandle: async ({ filePath, fileName, sizeBytes, fileHandle }) => {
            try {
                verifiedBytes = await fileHandle.readFile();
                assert.equal(sizeBytes, 10);
                assert.equal(fileName, '课件.docx');
            } finally {
                await fileHandle.close();
            }
            return { success: true, sourceFileId: 'sf-test-1', fileName, sizeBytes };
        },
    });
    assert.deepEqual(result, { success: true, sourceFileId: 'sf-test-1', fileName: '课件.docx', sizeBytes: 10 });
    assert.equal(verifiedBytes.length, 10);
    assert.equal(verifiedBytes[0], 104);
    assert.equal(verifiedBytes[9], 100);

    // complete 是一次性操作：会话已消费，重复 complete 必须拒绝。
    await assert.rejects(
        () => staging.complete({ uploadId }, SENDER, { openStagedHandle: async () => ({ success: true }) }),
        /missing or expired/,
    );
});

test('用户数据目录迁移为符号链接时仍可跨分块写入并完成上传', async (t) => {
    if (process.platform === 'win32') {
        t.skip('Windows CI may not permit creating directory symlinks');
        return;
    }
    const parent = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'wordtts-staging-link-'));
    const realDir = path.join(parent, 'real-staging');
    const linkedDir = path.join(parent, 'source-staging');
    await fs.promises.mkdir(realDir);
    await fs.promises.symlink(realDir, linkedDir, 'dir');
    try {
        const staging = createSourceUploadStaging({
            fs,
            path,
            stagingDir: linkedDir,
            chunkSize: 4,
            logger: { debug: () => {} },
        });
        const { uploadId } = await beginDocx(staging, 6);
        await staging.write({ uploadId, offset: 0, bytes: new Uint8Array([1, 2, 3, 4]) }, SENDER);
        await staging.write({ uploadId, offset: 4, bytes: new Uint8Array([5, 6]) }, SENDER);

        let content = null;
        const result = await staging.complete({ uploadId }, SENDER, {
            openStagedHandle: async ({ fileHandle, fileName, sizeBytes }) => {
                try {
                    content = await fileHandle.readFile();
                } finally {
                    await fileHandle.close();
                }
                return { success: true, sourceFileId: 'sf-linked-1', fileName, sizeBytes };
            },
        });
        assert.equal(result.success, true);
        assert.deepEqual([...content], [1, 2, 3, 4, 5, 6]);
    } finally {
        await fs.promises.rm(parent, { recursive: true, force: true });
    }
});

test('字节数不足时 complete 拒绝并清理暂存文件', async () => {
    const { staging, stagingDir } = await makeStaging({ chunkSize: 4 });
    const { uploadId } = await beginDocx(staging, 10);
    await staging.write({ uploadId, offset: 0, bytes: new Uint8Array([1, 2]) }, SENDER);
    const stagedPath = path.join(stagingDir, `${uploadId}.docx`);
    await assert.rejects(
        () => staging.complete({ uploadId }, SENDER, { openStagedHandle: async () => ({ success: true }) }),
        /incomplete: 2\/10/,
    );
    assert.equal(fs.existsSync(stagedPath), false);
});

test('openStagedHandle 失败时保持 fail-closed：句柄关闭、暂存文件删除', async () => {
    const { staging, stagingDir } = await makeStaging({ chunkSize: 4 });
    const { uploadId } = await beginDocx(staging, 4);
    await staging.write({ uploadId, offset: 0, bytes: new Uint8Array([1, 2, 3, 4]) }, SENDER);
    let closed = false;
    const result = await staging.complete({ uploadId }, SENDER, {
        openStagedHandle: async ({ fileHandle }) => {
            const original = fileHandle.close.bind(fileHandle);
            fileHandle.close = async () => { closed = true; return original(); };
            return { success: false, reason: 'untrusted-path' };
        },
    });
    assert.deepEqual(result, { success: false, reason: 'untrusted-path' });
    assert.equal(closed, true);
    assert.equal(fs.existsSync(path.join(stagingDir, `${uploadId}.docx`)), false);
});

test('openStagedHandle 抛错时也会关闭句柄并清理暂存文件', async () => {
    const { staging, stagingDir } = await makeStaging({ chunkSize: 4 });
    const { uploadId } = await beginDocx(staging, 4);
    await staging.write({ uploadId, offset: 0, bytes: new Uint8Array([1, 2, 3, 4]) }, SENDER);
    let closed = false;
    await assert.rejects(
        () => staging.complete({ uploadId }, SENDER, {
            openStagedHandle: async ({ fileHandle }) => {
                const original = fileHandle.close.bind(fileHandle);
                fileHandle.close = async () => { closed = true; return original(); };
                throw new Error('registration failed');
            },
        }),
        /registration failed/,
    );
    assert.equal(closed, true);
    assert.equal(fs.existsSync(path.join(stagingDir, `${uploadId}.docx`)), false);
    assert.equal(staging.activeCount, 0);
});

test('abort 与 TTL 过期都会删除暂存文件', async () => {
    // Windows CI can spend tens of milliseconds opening the second exclusive
    // handle. Keep the TTL short enough to test expiry without allowing the
    // first session to expire before the test reaches its explicit abort.
    const { staging, stagingDir } = await makeStaging({ chunkSize: 4, ttlMs: 250 });
    const aborted = await beginDocx(staging, 4);
    const expired = await beginDocx(staging, 4);
    await staging.write({ uploadId: aborted.uploadId, offset: 0, bytes: new Uint8Array([1, 2, 3, 4]) }, SENDER);

    await staging.abort({ uploadId: aborted.uploadId });
    assert.equal(fs.existsSync(path.join(stagingDir, `${aborted.uploadId}.docx`)), false);

    await new Promise((resolve) => setTimeout(resolve, 500));
    assert.equal(fs.existsSync(path.join(stagingDir, `${expired.uploadId}.docx`)), false);
    await assert.rejects(
        () => staging.write({ uploadId: expired.uploadId, offset: 0, bytes: new Uint8Array([1]) }, SENDER),
        /missing or expired/,
    );
});

test('其他 sender 不能读写他人的上传会话', async () => {
    const { staging } = await makeStaging({ chunkSize: 4 });
    const { uploadId } = await beginDocx(staging, 4);
    await assert.rejects(
        () => staging.write({ uploadId, offset: 0, bytes: new Uint8Array([1]) }, 'sender-2'),
        /belongs to another sender/,
    );
    await assert.rejects(
        () => staging.complete({ uploadId }, 'sender-2', { openStagedHandle: async () => ({ success: true }) }),
        /belongs to another sender/,
    );
});

test('暂存区限制并发会话数，并可按 sender 清理', async () => {
    const { staging, stagingDir } = await makeStaging({ maxSessions: 1 });
    const first = await beginDocx(staging, 4);
    await assert.rejects(
        () => staging.begin({ fileName: 'second.docx', sizeBytes: 4, senderId: SENDER }),
        error => error.code === 'RESOURCE_EXHAUSTED',
    );
    assert.equal(await staging.disposeSender(SENDER, 'renderer-reloaded'), 1);
    assert.equal(staging.activeCount, 0);
    assert.equal(fs.existsSync(path.join(stagingDir, `${first.uploadId}.docx`)), false);
});

test('disposeAll 清理全部未完成会话', async () => {
    const { staging, stagingDir } = await makeStaging({ chunkSize: 4 });
    const first = await beginDocx(staging, 4);
    const second = await beginDocx(staging, 8, 'other.docx');
    assert.equal(await staging.disposeAll('test'), 2);
    assert.equal(fs.existsSync(path.join(stagingDir, `${first.uploadId}.docx`)), false);
    assert.equal(fs.existsSync(path.join(stagingDir, `${second.uploadId}.docx`)), false);
    assert.equal(staging.activeCount, 0);
});
