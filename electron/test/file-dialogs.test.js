'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
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
