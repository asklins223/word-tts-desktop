'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fsNative = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { Readable } = require('node:stream');
const { createNativeFileDialogs } = require('../file-dialogs');

function createHarness(overrides = {}) {
    const calls = { open: [], save: [], copy: [], focus: 0, restore: 0, show: 0 };
    const owner = {
        webContents: { mainFrame: {} },
        isDestroyed: () => false,
        isMinimized: overrides.isMinimized || (() => false),
        isVisible: overrides.isVisible || (() => true),
        restore: () => { calls.restore += 1; },
        show: () => { calls.show += 1; },
        focus: () => { calls.focus += 1; },
    };
    const event = { sender: owner.webContents, senderFrame: owner.webContents.mainFrame };
    const dialog = {
        showOpenDialog: async (...args) => {
            calls.open.push(args);
            return { canceled: false, filePaths: ['/safe/lesson.docx'] };
        },
        showSaveDialog: async (...args) => {
            calls.save.push(args);
            return { canceled: false, filePath: '/chosen/lesson.mp3' };
        },
        ...overrides.dialog,
    };
    const fs = {
        existsSync: () => true,
        copyFileSync: (...args) => { calls.copy.push(args); },
        ...overrides.fs,
    };
    const BrowserWindow = {
        fromWebContents: () => owner,
        ...overrides.BrowserWindow,
    };
    const service = createNativeFileDialogs({
        app: { getPath: () => '/Downloads', ...overrides.app },
        BrowserWindow,
        dialog,
        fs,
        isAllowedFilePath: overrides.isAllowedFilePath || (() => true),
        getMainWindow: overrides.getMainWindow || (() => owner),
        isTrustedSender: overrides.isTrustedSender || (() => true),
        logger: { log() {}, error() {} },
        maxContentBytes: overrides.maxContentBytes,
        maxSourceBytes: overrides.maxSourceBytes,
        maxStreamBytes: overrides.maxStreamBytes,
    });
    return { service, owner, event, calls };
}

test('选择 Word 使用 IPC 发起窗口作为原生对话框父窗口', async () => {
    const { service, owner, event, calls } = createHarness();
    const result = await service.selectFile(event);

    assert.deepEqual(result, { success: true, filePath: '/safe/lesson.docx' });
    assert.equal(calls.open.length, 1);
    assert.equal(calls.open[0][0], owner);
    assert.deepEqual(calls.open[0][1].filters, [
        { name: 'Word/Excel 文档', extensions: ['docx', 'xlsx'] },
        { name: 'Word 文档', extensions: ['docx'] },
        { name: 'Excel 文档', extensions: ['xlsx'] },
    ]);
    assert.deepEqual(calls.open[0][1].properties, ['openFile']);
    assert.equal(calls.focus, 1);
});

test('原生源文件选择返回受控流句柄和大小，不读取整份文件到内存', async () => {
    let closed = 0;
    const { service, event } = createHarness({
        fs: {
            constants: { O_RDONLY: 0, O_NOFOLLOW: 0 },
            promises: {
                open: async () => ({
                    stat: async () => ({ isFile: () => true, size: 12345 }),
                    close: async () => { closed += 1; },
                }),
            },
        },
    });
    const result = await service.selectFileSource(event);

    assert.equal(result.success, true);
    assert.equal(result.fileName, 'lesson.docx');
    assert.equal(result.sizeBytes, 12345);
    assert.ok(result.handle);
    await result.handle.close();
    assert.equal(closed, 1);

    const separateBounds = createHarness({
        maxContentBytes: 4,
        maxSourceBytes: 16,
        fs: {
            constants: { O_RDONLY: 0, O_NOFOLLOW: 0 },
            promises: {
                open: async () => ({
                    stat: async () => ({ isFile: () => true, size: 8 }),
                    close: async () => {},
                }),
            },
        },
    });
    const sourceWithinStreamBound = await separateBounds.service.selectFileSource(separateBounds.event);
    assert.equal(sourceWithinStreamBound.success, true);
    await sourceWithinStreamBound.handle.close();

    const sourceTooLarge = createHarness({
        maxSourceBytes: 4,
        fs: {
            constants: { O_RDONLY: 0, O_NOFOLLOW: 0 },
            promises: {
                open: async () => ({
                    stat: async () => ({ isFile: () => true, size: 8 }),
                    close: async () => {},
                }),
            },
        },
    });
    assert.deepEqual(await sourceTooLarge.service.selectFileSource(sourceTooLarge.event), {
        success: false,
        reason: 'file-too-large',
    });

    const bounded = createHarness({
        maxContentBytes: 4,
        fs: {
            constants: { O_RDONLY: 0, O_NOFOLLOW: 0 },
            promises: {
                open: async () => ({
                    stat: async () => ({ isFile: () => true, size: 8 }),
                    readFile: async () => { throw new Error('must not read oversized compatibility input'); },
                    close: async () => {},
                }),
            },
        },
    });
    assert.deepEqual(await bounded.service.selectFileContent(bounded.event), {
        success: false,
        reason: 'file-too-large',
    });
});

test('原生文件框打开前会恢复并显示当前窗口', async () => {
    const { service, event, calls } = createHarness({
        isMinimized: () => true,
        isVisible: () => false,
    });
    assert.deepEqual(await service.selectFile(event), {
        success: true,
        filePath: '/safe/lesson.docx',
    });
    assert.equal(calls.restore, 1);
    assert.equal(calls.show, 1);
    assert.equal(calls.focus, 1);
});

test('原生文件框即使返回过滤器之外的路径也会拒绝导入', async () => {
    const { service, event } = createHarness({
        dialog: { showOpenDialog: async () => ({ canceled: false, filePaths: ['/safe/notes.pdf'] }) },
    });

    assert.deepEqual(await service.selectFile(event), {
        success: false,
        reason: 'unsupported-file-type',
    });
});

test('选择 Word 的取消、无窗口与系统异常均返回结构化原因', async (t) => {
    await t.test('用户取消', async () => {
        const { service, event } = createHarness({
            dialog: { showOpenDialog: async () => ({ canceled: true, filePaths: [] }) },
        });
        assert.deepEqual(await service.selectFile(event), { success: false, reason: 'user-cancelled' });
    });

    await t.test('窗口不可用', async () => {
        const { service, event } = createHarness({
            BrowserWindow: { fromWebContents: () => null },
            getMainWindow: () => null,
        });
        assert.deepEqual(await service.selectFile(event), { success: false, reason: 'window-unavailable' });
    });

    await t.test('系统对话框异常', async () => {
        const { service, event } = createHarness({
            dialog: { showOpenDialog: async () => { throw new Error('open failed'); } },
        });
        assert.deepEqual(await service.selectFile(event), {
            success: false,
            reason: 'dialog-error',
            error: 'open failed',
        });
    });
});

test('保存文件弹出系统保存框并复制到用户选择的位置', async () => {
    const { service, owner, event, calls } = createHarness();
    const result = await service.saveFileByPath(event, '/safe/lesson.mp3', '../lesson.mp3');

    assert.deepEqual(result, { success: true });
    assert.equal(calls.save.length, 1);
    assert.equal(calls.save[0][0], owner);
    assert.equal(calls.save[0][1].defaultPath, path.join('/Downloads', 'lesson.mp3'));
    assert.deepEqual(calls.copy, [['/safe/lesson.mp3', '/chosen/lesson.mp3']]);
});

test('保存内存字节在 Windows 上使用可写句柄 fsync 再原子替换目标文件', async () => {
    const writes = [];
    const opens = [];
    const renames = [];
    const unlinks = [];
    const { service, event } = createHarness({
        fs: {
            promises: {
                writeFile: async (...args) => { writes.push(args); },
                open: async (...args) => {
                    opens.push(args);
                    if (args[1] === 'r') {
                        const error = new Error('fsync requires a writable descriptor');
                        error.code = 'EPERM';
                        throw error;
                    }
                    return {
                        sync: async () => {},
                        close: async () => {},
                    };
                },
                rename: async (...args) => { renames.push(args); },
                unlink: async (...args) => { unlinks.push(args); },
            },
        },
    });

    const result = await service.saveFileContent(event, new Uint8Array([1, 2, 3]), '../lesson.mp3');

    assert.deepEqual(result, { success: true });
    assert.equal(writes.length, 1);
    assert.equal(writes[0][1].toString('hex'), '010203');
    assert.deepEqual(writes[0][2], { flag: 'wx' });
    assert.equal(opens.length, 1);
    assert.equal(opens[0][0], writes[0][0]);
    assert.equal(opens[0][1], 'r+');
    assert.deepEqual(renames, [[writes[0][0], '/chosen/lesson.mp3']]);
    assert.deepEqual(unlinks, []);
});

test('临时文件名偶发冲突时会重试而不是直接返回 write-error', async () => {
    let attempts = 0;
    const writes = [];
    const { service, event } = createHarness({
        fs: {
            promises: {
                writeFile: async (...args) => {
                    attempts += 1;
                    writes.push(args);
                    if (attempts === 1) {
                        const error = new Error('temporary name already exists');
                        error.code = 'EEXIST';
                        throw error;
                    }
                },
                open: async () => ({ sync: async () => {}, close: async () => {} }),
                rename: async () => {},
                unlink: async () => {},
            },
        },
    });

    const result = await service.saveFileContent(event, new Uint8Array([1, 2, 3]), 'lesson.mp3');

    assert.deepEqual(result, { success: true });
    assert.equal(attempts, 2);
    assert.notEqual(writes[0][0], writes[1][0]);
});

test('保存内存字节超过上限时不会弹出保存框', async () => {
    const { service, event, calls } = createHarness({ maxContentBytes: 2 });
    const result = await service.saveFileContent(event, Buffer.from([1, 2, 3]), 'lesson.mp3');

    assert.deepEqual(result, { success: false, reason: 'file-too-large' });
    assert.equal(calls.save.length, 0);
});

test('流式保存在 Windows 上使用可写句柄 fsync 再替换目标文件', async () => {
    const directory = fsNative.mkdtempSync(path.join(os.tmpdir(), 'wordtts-stream-success-'));
    const targetPath = path.join(directory, 'lesson.mp3');
    const openCalls = [];
    const originalOpen = fsNative.promises.open.bind(fsNative.promises);
    const fs = {
        ...fsNative,
        promises: {
            ...fsNative.promises,
            open: async (...args) => {
                openCalls.push(args);
                if (args[1] === 'r') {
                    const error = new Error('fsync requires a writable descriptor');
                    error.code = 'EPERM';
                    throw error;
                }
                return originalOpen(...args);
            },
        },
    };
    try {
        const { service, event } = createHarness({
            fs,
            dialog: { showSaveDialog: async () => ({ canceled: false, filePath: targetPath }) },
            maxStreamBytes: 16 * 1024,
        });
        const source = Readable.from([Buffer.from('verified mp3 bytes')]);
        source.statusCode = 200;
        source.headers = { 'content-length': '18' };
        const result = await service.saveFileStream(event, async () => source, 'lesson.mp3');

        assert.deepEqual(result, { success: true });
        assert.equal(fsNative.readFileSync(targetPath, 'utf8'), 'verified mp3 bytes');
        assert.equal(openCalls.length, 1);
        assert.equal(openCalls[0][1], 'r+');
    } finally {
        fsNative.rmSync(directory, { recursive: true, force: true });
    }
});

test('流式保存按块报告进度，并在取消时清理临时文件且不替换目标', async () => {
    const directory = fsNative.mkdtempSync(path.join(os.tmpdir(), 'wordtts-stream-test-'));
    const targetPath = path.join(directory, 'lesson.mp3');
    const progress = [];
    try {
        const { service, event } = createHarness({
            fs: fsNative,
            dialog: { showSaveDialog: async () => ({ canceled: false, filePath: targetPath }) },
            maxContentBytes: 4,
            maxStreamBytes: 16 * 1024,
        });
        const controller = new AbortController();
        const source = Readable.from([
            Buffer.alloc(4 * 1024, 1),
            Buffer.alloc(4 * 1024, 2),
            Buffer.alloc(4 * 1024, 3),
        ]);
        source.statusCode = 200;
        source.headers = {};
        const resultPromise = service.saveFileStream(
            event,
            async () => source,
            'lesson.mp3',
            {
                signal: controller.signal,
                onProgress: (value) => {
                    progress.push(value);
                    if (value.receivedBytes >= 4 * 1024) controller.abort();
                },
            },
        );
        const result = await resultPromise;

        assert.deepEqual(result, {
            success: false,
            reason: 'user-cancelled',
            error: 'file save was cancelled',
        });
        assert.equal(progress[0].receivedBytes, 4 * 1024);
        assert.equal(progress.at(-1).totalBytes, null);
        assert.equal(fsNative.existsSync(targetPath), false);
        assert.deepEqual(fsNative.readdirSync(directory), []);
    } finally {
        fsNative.rmSync(directory, { recursive: true, force: true });
    }
});

test('保存文件的所有失败分支都返回非空结构化原因', async (t) => {
    const cases = [
        {
            name: '窗口不可用',
            overrides: { BrowserWindow: { fromWebContents: () => null }, getMainWindow: () => null },
            expected: { success: false, reason: 'window-unavailable' },
        },
        {
            name: '路径越界',
            overrides: { isAllowedFilePath: () => false },
            expected: { success: false, reason: 'path-check-failed' },
        },
        {
            name: '路径校验异常',
            overrides: { isAllowedFilePath: () => { throw new Error('path failed'); } },
            expected: { success: false, reason: 'path-check-failed', error: 'path failed' },
        },
        {
            name: '源文件不存在',
            overrides: { fs: { existsSync: () => false } },
            expected: { success: false, reason: 'file-not-found' },
        },
        {
            name: '源文件检查异常',
            overrides: { fs: { existsSync: () => { throw new Error('check failed'); } } },
            expected: { success: false, reason: 'file-check-error', error: 'check failed' },
        },
        {
            name: '用户取消',
            overrides: { dialog: { showSaveDialog: async () => ({ canceled: true }) } },
            expected: { success: false, reason: 'user-cancelled' },
        },
        {
            name: '保存框异常',
            overrides: { dialog: { showSaveDialog: async () => { throw new Error('dialog failed'); } } },
            expected: { success: false, reason: 'dialog-error', error: 'dialog failed' },
        },
        {
            name: '保存框返回异常',
            overrides: { dialog: { showSaveDialog: async () => null } },
            expected: { success: false, reason: 'dialog-error', error: '系统保存框未返回有效结果' },
        },
        {
            name: '复制异常',
            overrides: { fs: { copyFileSync: () => { throw new Error('copy failed'); } } },
            expected: { success: false, reason: 'copy-error', error: 'copy failed' },
        },
    ];

    for (const item of cases) {
        await t.test(item.name, async () => {
            const { service, event } = createHarness(item.overrides);
            const result = await service.saveFileByPath(event, '/safe/lesson.mp3', 'lesson.mp3');
            assert.deepEqual(result, item.expected);
            assert.equal(typeof result.reason, 'string');
            assert.notEqual(result.reason, 'unknown');
        });
    }
});

test('非主 frame 不能借用顶层窗口打开文件对话框', async () => {
    const { service, event } = createHarness();
    event.senderFrame = {};
    assert.deepEqual(await service.selectFile(event), { success: false, reason: 'untrusted-sender' });
});

test('未知窗口、空 frame 与非本地页面均不能调用原生文件对话框', async (t) => {
    await t.test('未知窗口', async () => {
        const foreignWindow = {
            webContents: { mainFrame: {} },
            isDestroyed: () => false,
        };
        const { service, event } = createHarness({
            BrowserWindow: { fromWebContents: () => foreignWindow },
        });
        assert.deepEqual(await service.selectFile(event), { success: false, reason: 'untrusted-sender' });
    });

    await t.test('空 frame', async () => {
        const { service, event } = createHarness();
        event.senderFrame = null;
        assert.deepEqual(await service.selectFile(event), { success: false, reason: 'untrusted-sender' });
    });

    await t.test('非本地页面', async () => {
        const { service, event } = createHarness({ isTrustedSender: () => false });
        assert.deepEqual(await service.selectFile(event), { success: false, reason: 'untrusted-sender' });
    });
});
