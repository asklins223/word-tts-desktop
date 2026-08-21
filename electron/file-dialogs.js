'use strict';

const path = require('path');

/**
 * 构造 Electron 原生文件对话框服务。
 * 依赖显式注入，既让主进程保持精简，也便于在无图形界面的 CI 中验证 IPC 契约。
 */
function createNativeFileDialogs({
    app,
    BrowserWindow,
    dialog,
    fs,
    isAllowedFilePath,
    getMainWindow,
    isTrustedSender = () => true,
    logger = console,
}) {
    function getDialogOwnerWindow(event) {
        const currentWindow = getMainWindow();
        if (!currentWindow || currentWindow.isDestroyed()) return null;
        if (!event?.sender || !event?.senderFrame || !isTrustedSender(event)) return null;
        const senderWindow = event?.sender
            ? BrowserWindow.fromWebContents(event.sender)
            : null;
        if (!senderWindow || senderWindow.isDestroyed() || senderWindow !== currentWindow) return null;
        if (event.sender !== currentWindow.webContents) return null;
        if (event.senderFrame !== currentWindow.webContents.mainFrame) return null;
        return currentWindow;
    }

    function focusDialogOwner(ownerWindow) {
        if (!ownerWindow || ownerWindow.isDestroyed()) return;
        if (ownerWindow.isMinimized()) ownerWindow.restore();
        if (!ownerWindow.isVisible()) ownerWindow.show();
        ownerWindow.focus();
    }

    async function selectFile(event) {
        const currentWindow = getMainWindow();
        if (!currentWindow || currentWindow.isDestroyed()) {
            return { success: false, reason: 'window-unavailable' };
        }
        const ownerWindow = getDialogOwnerWindow(event);
        if (!ownerWindow) {
            return { success: false, reason: 'untrusted-sender' };
        }
        try {
            focusDialogOwner(ownerWindow);
            const result = await dialog.showOpenDialog(ownerWindow, {
                title: '选择文档',
                filters: [
                    { name: 'Word/Excel 文档', extensions: ['docx', 'xlsx'] },
                    { name: 'Word 文档', extensions: ['docx'] },
                    { name: 'Excel 文档', extensions: ['xlsx'] },
                ],
                properties: ['openFile'],
            });
            if (result.canceled || result.filePaths.length === 0) {
                return { success: false, reason: 'user-cancelled' };
            }
            return { success: true, filePath: result.filePaths[0] };
        } catch (error) {
            logger.error('[main] 打开文件选择框失败:', error);
            return { success: false, reason: 'dialog-error', error: error.message };
        }
    }

    async function saveFileByPath(event, sourcePath, suggestedName) {
        const currentWindow = getMainWindow();
        if (!currentWindow || currentWindow.isDestroyed()) {
            return { success: false, reason: 'window-unavailable' };
        }
        const ownerWindow = getDialogOwnerWindow(event);
        if (!ownerWindow) {
            return { success: false, reason: 'untrusted-sender' };
        }

        let sourceAllowed = false;
        try {
            sourceAllowed = isAllowedFilePath(sourcePath);
        } catch (error) {
            logger.error('[main] 校验源文件路径失败:', error);
            return { success: false, reason: 'path-check-failed', error: error.message };
        }
        if (!sourceAllowed) {
            logger.error('[main] 拒绝复制允许目录外的文件:', sourcePath);
            return { success: false, reason: 'path-check-failed' };
        }

        try {
            if (!fs.existsSync(sourcePath)) {
                logger.error('[main] 源文件不存在:', sourcePath);
                return { success: false, reason: 'file-not-found' };
            }
        } catch (error) {
            logger.error('[main] 检查源文件失败:', error);
            return { success: false, reason: 'file-check-error', error: error.message };
        }

        let result;
        try {
            focusDialogOwner(ownerWindow);
            const rawSuggestedName = String(suggestedName || path.basename(sourcePath));
            const safeSuggestedName = path.basename(rawSuggestedName)
                .replace(/[\u0000-\u001f\u007f]/g, '')
                .trim() || '音频文件';
            result = await dialog.showSaveDialog(ownerWindow, {
                title: '保存文件',
                defaultPath: path.join(app.getPath('downloads'), safeSuggestedName),
            });
        } catch (error) {
            logger.error('[main] 打开文件保存框失败:', error);
            return { success: false, reason: 'dialog-error', error: error.message };
        }

        if (!result || typeof result !== 'object') {
            return { success: false, reason: 'dialog-error', error: '系统保存框未返回有效结果' };
        }
        if (result.canceled || !result.filePath) {
            return { success: false, reason: 'user-cancelled' };
        }
        try {
            fs.copyFileSync(sourcePath, result.filePath);
            logger.log('[main] 文件复制成功:', sourcePath, '->', result.filePath);
            return { success: true };
        } catch (error) {
            logger.error('[main] 复制文件失败:', error);
            return { success: false, reason: 'copy-error', error: error.message };
        }
    }

    return Object.freeze({
        getDialogOwnerWindow,
        selectFile,
        saveFileByPath,
    });
}

module.exports = { createNativeFileDialogs };
