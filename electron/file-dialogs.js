'use strict';

const path = require('path');
const { randomBytes } = require('crypto');
const { Transform } = require('stream');
const { pipeline } = require('stream/promises');

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
    maxContentBytes = 512 * 1024 * 1024,
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

    function makeTemporaryPath(destination) {
        return `${destination}.wordtts-${process.pid}-${Date.now()}-${randomBytes(8).toString('hex')}.part`;
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

    async function selectFileContent(event) {
        const selected = await selectFile(event);
        if (!selected?.success || !selected.filePath) return selected;
        const selectedPath = selected.filePath;
        try {
            let content;
            if (typeof fs.promises.open === 'function') {
                // Open with O_NOFOLLOW where the platform exposes it, then
                // fstat/read through the same descriptor.  This closes the
                // lstat-then-read symlink/replace race on Unix-like hosts.
                const flags = (fs.constants?.O_RDONLY || 0) | (fs.constants?.O_NOFOLLOW || 0);
                const handle = await fs.promises.open(selectedPath, flags);
                try {
                    const stat = await handle.stat();
                    if (!stat.isFile()) {
                        return { success: false, reason: 'file-check-error', error: '只能读取普通文档文件' };
                    }
                    if (stat.size > maxContentBytes) {
                        return { success: false, reason: 'file-too-large' };
                    }
                    content = await handle.readFile();
                } finally {
                    await handle.close();
                }
            } else {
                // Keep the injected CI adapter usable while the real Node fs
                // path above remains the production security boundary.
                const stat = await fs.promises.lstat(selectedPath);
                if (!stat.isFile() || stat.isSymbolicLink()) {
                    return { success: false, reason: 'file-check-error', error: '只能读取普通文档文件' };
                }
                if (stat.size > maxContentBytes) {
                    return { success: false, reason: 'file-too-large' };
                }
                content = await fs.promises.readFile(selectedPath);
            }
            if (content.length > maxContentBytes) {
                return { success: false, reason: 'file-too-large' };
            }
            return {
                success: true,
                fileName: path.basename(selectedPath),
                bytes: new Uint8Array(content),
            };
        } catch (error) {
            logger.error('[main] 读取所选文档失败:', error);
            return { success: false, reason: 'file-read-error', error: error.message };
        }
    }

    async function selectFileSource(event) {
        const selected = await selectFile(event);
        if (!selected?.success || !selected.filePath) return selected;
        if (typeof fs.promises?.open !== 'function') {
            return {
                success: false,
                reason: 'file-read-error',
                error: '当前文件系统不支持安全的流式读取',
            };
        }

        let handle = null;
        try {
            const flags = (fs.constants?.O_RDONLY || 0) | (fs.constants?.O_NOFOLLOW || 0);
            handle = await fs.promises.open(selected.filePath, flags);
            const stat = await handle.stat();
            if (!stat.isFile()) {
                await handle.close();
                handle = null;
                return { success: false, reason: 'file-check-error', error: '只能读取普通文档文件' };
            }
            if (stat.size > maxContentBytes) {
                await handle.close();
                handle = null;
                return { success: false, reason: 'file-too-large' };
            }
            return {
                success: true,
                fileName: path.basename(selected.filePath),
                sizeBytes: stat.size,
                handle,
            };
        } catch (error) {
            if (handle) {
                try { await handle.close(); } catch (_) { /* best effort cleanup */ }
            }
            logger.error('[main] 打开所选文档流失败:', error);
            return { success: false, reason: 'file-read-error', error: error.message };
        }
    }

    async function saveFileContent(event, content, suggestedName) {
        const currentWindow = getMainWindow();
        if (!currentWindow || currentWindow.isDestroyed()) {
            return { success: false, reason: 'window-unavailable' };
        }
        const ownerWindow = getDialogOwnerWindow(event);
        if (!ownerWindow) {
            return { success: false, reason: 'untrusted-sender' };
        }
        let bytes;
        try {
            if (Buffer.isBuffer(content)) bytes = content;
            else if (content instanceof Uint8Array) bytes = Buffer.from(content);
            else if (content instanceof ArrayBuffer) bytes = Buffer.from(new Uint8Array(content));
            else throw new TypeError('保存内容必须是字节缓冲区');
        } catch (error) {
            return { success: false, reason: 'content-invalid', error: error.message };
        }
        if (bytes.length > maxContentBytes) return { success: false, reason: 'file-too-large' };

        let result;
        try {
            focusDialogOwner(ownerWindow);
            const safeSuggestedName = path.basename(String(suggestedName || '音频文件'))
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
        if (result.canceled || !result.filePath) return { success: false, reason: 'user-cancelled' };
        let temporaryFile = null;
        try {
            temporaryFile = makeTemporaryPath(result.filePath);
            await fs.promises.writeFile(temporaryFile, bytes, { flag: 'wx' });
            const handle = await fs.promises.open(temporaryFile, 'r');
            try {
                await handle.sync();
            } finally {
                await handle.close();
            }
            await fs.promises.rename(temporaryFile, result.filePath);
            temporaryFile = null;
            return { success: true };
        } catch (error) {
            logger.error('[main] 保存文件失败:', error);
            return { success: false, reason: 'write-error', error: error.message };
        } finally {
            if (temporaryFile) {
                try { await fs.promises.unlink(temporaryFile); } catch (_) { /* best effort cleanup */ }
            }
        }
    }

    async function saveFileStream(event, openStream, suggestedName) {
        const currentWindow = getMainWindow();
        if (!currentWindow || currentWindow.isDestroyed()) {
            return { success: false, reason: 'window-unavailable' };
        }
        const ownerWindow = getDialogOwnerWindow(event);
        if (!ownerWindow) {
            return { success: false, reason: 'untrusted-sender' };
        }
        if (typeof openStream !== 'function') {
            return { success: false, reason: 'content-invalid', error: '文件流服务不可用' };
        }

        let result;
        try {
            focusDialogOwner(ownerWindow);
            const safeSuggestedName = path.basename(String(suggestedName || '下载文件'))
                .replace(/[\u0000-\u001f\u007f]/g, '')
                .trim() || '下载文件';
            result = await dialog.showSaveDialog(ownerWindow, {
                title: '保存文件',
                defaultPath: path.join(app.getPath('downloads'), safeSuggestedName),
            });
        } catch (error) {
            logger.error('[main] 打开流式文件保存框失败:', error);
            return { success: false, reason: 'dialog-error', error: error.message };
        }
        if (!result || typeof result !== 'object') {
            return { success: false, reason: 'dialog-error', error: '系统保存框未返回有效结果' };
        }
        if (result.canceled || !result.filePath) return { success: false, reason: 'user-cancelled' };

        let response = null;
        let temporaryPath = null;
        try {
            response = await openStream();
            const status = Number(response?.statusCode || 0);
            if (status < 200 || status >= 300) {
                response?.resume?.();
                return {
                    success: false,
                    reason: status === 413 ? 'file-too-large' : 'download-error',
                    error: `HTTP ${status}`,
                };
            }
            const declaredLength = Number(response.headers?.['content-length']);
            if (Number.isFinite(declaredLength) && declaredLength > maxContentBytes) {
                response.destroy?.(new Error('file is too large'));
                return { success: false, reason: 'file-too-large' };
            }

            temporaryPath = makeTemporaryPath(result.filePath);
            const target = fs.createWriteStream(temporaryPath, { flags: 'wx' });
            let received = 0;
            const bounded = new Transform({
                transform(chunk, _encoding, callback) {
                    received += chunk.length;
                    if (received > maxContentBytes) {
                        const error = new Error('file is too large');
                        error.code = 'RESOURCE_EXHAUSTED';
                        callback(error);
                        return;
                    }
                    callback(null, chunk);
                },
            });
            await pipeline(response, bounded, target);
            const handle = await fs.promises.open(temporaryPath, 'r');
            try {
                await handle.sync();
            } finally {
                await handle.close();
            }
            await fs.promises.rename(temporaryPath, result.filePath);
            temporaryPath = null;
            logger.log('[main] 流式文件保存成功:', result.filePath, `(${received} bytes)`);
            return { success: true };
        } catch (error) {
            response?.destroy?.();
            logger.error('[main] 流式文件保存失败:', error);
            return {
                success: false,
                reason: error?.code === 'RESOURCE_EXHAUSTED' ? 'file-too-large' : 'download-error',
                error: error?.message,
            };
        } finally {
            if (temporaryPath) {
                try { await fs.promises.unlink(temporaryPath); } catch (_) { /* best effort cleanup */ }
            }
        }
    }

    return Object.freeze({
        getDialogOwnerWindow,
        selectFile,
        saveFileByPath,
        selectFileContent,
        selectFileSource,
        saveFileContent,
        saveFileStream,
    });
}

module.exports = { createNativeFileDialogs };
