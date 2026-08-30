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
    // The byte-returning IPC method is only a compatibility path for older
    // renderers. Large documents must use selectFileSource instead, which has
    // its own larger bounded stream limit.
    maxContentBytes = 16 * 1024 * 1024,
    maxSourceBytes = 512 * 1024 * 1024,
    maxStreamBytes = 512 * 1024 * 1024,
}) {
    const supportedDocumentExtensions = new Set(['.docx', '.xlsx']);

    function isSupportedDocumentPath(filePath) {
        return supportedDocumentExtensions.has(path.extname(String(filePath || '')).toLowerCase());
    }

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

    const TEMPORARY_PATH_ATTEMPTS = 8;

    async function writeExclusiveTemporaryFile(destination, content) {
        let lastError = null;
        for (let attempt = 0; attempt < TEMPORARY_PATH_ATTEMPTS; attempt += 1) {
            const candidate = makeTemporaryPath(destination);
            try {
                await fs.promises.writeFile(candidate, content, { flag: 'wx' });
                return candidate;
            } catch (error) {
                lastError = error;
                // A random-name collision is recoverable.  Do not let the
                // caller's cleanup unlink a path we did not create.
                if (error?.code !== 'EEXIST' || attempt + 1 >= TEMPORARY_PATH_ATTEMPTS) {
                    throw error;
                }
            }
        }
        throw lastError || new Error('could not allocate a temporary file');
    }

    async function createExclusiveTemporaryStream(destination) {
        let lastError = null;
        for (let attempt = 0; attempt < TEMPORARY_PATH_ATTEMPTS; attempt += 1) {
            const candidate = makeTemporaryPath(destination);
            let target = null;
            try {
                target = fs.createWriteStream(candidate, { flags: 'wx' });
                await new Promise((resolve, reject) => {
                    const onOpen = () => {
                        target.removeListener('error', onError);
                        resolve();
                    };
                    const onError = (error) => {
                        target.removeListener('open', onOpen);
                        reject(error);
                    };
                    target.once('open', onOpen);
                    target.once('error', onError);
                });
                return { path: candidate, stream: target };
            } catch (error) {
                lastError = error;
                target?.destroy?.();
                if (error?.code !== 'EEXIST' || attempt + 1 >= TEMPORARY_PATH_ATTEMPTS) {
                    throw error;
                }
            }
        }
        throw lastError || new Error('could not allocate a temporary stream');
    }

    async function assertNotSymlink(filePath) {
        if (typeof fs.promises?.lstat !== 'function') return;
        const stat = await fs.promises.lstat(filePath);
        if (stat.isSymbolicLink?.() || !stat.isFile?.()) {
            const error = new Error('只能读取普通文档文件');
            error.code = 'SYMLINK_REJECTED';
            throw error;
        }
    }

    async function canonicalReadPath(filePath) {
        await assertNotSymlink(filePath);
        if (typeof fs.promises?.realpath !== 'function') return filePath;
        try {
            // Open the resolved path on platforms without O_NOFOLLOW.  If the
            // selected entry is swapped for a link after the initial lstat,
            // the descriptor still targets the path resolved here rather than
            // following the replacement at the user-selected pathname.
            return await fs.promises.realpath(filePath);
        } catch (error) {
            if (error?.code === 'ENOENT') throw error;
            return filePath;
        }
    }

    async function openReadOnlyRegularFile(filePath, flags) {
        const hasNoFollow = Number.isInteger(fs.constants?.O_NOFOLLOW)
            && fs.constants.O_NOFOLLOW !== 0;
        const openPath = hasNoFollow ? filePath : await canonicalReadPath(filePath);
        let handle;
        try {
            handle = await fs.promises.open(openPath, flags);
            // Windows does not expose O_NOFOLLOW in Node.  Re-check the path
            // after opening so a pre-placed/replaced symlink is rejected
            // before the descriptor is read; the descriptor itself remains
            // the object used for all subsequent I/O.
            if (!hasNoFollow) {
                await assertNotSymlink(filePath);
                await assertNotSymlink(openPath);
            }
            return handle;
        } catch (error) {
            try { await handle?.close?.(); } catch (_) { /* best effort */ }
            throw error;
        }
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
            if (!result || typeof result !== 'object' || !Array.isArray(result.filePaths)) {
                return { success: false, reason: 'dialog-error', error: '系统文件框未返回有效结果' };
            }
            if (result.canceled || result.filePaths.length === 0) {
                return { success: false, reason: 'user-cancelled' };
            }
            const filePath = result.filePaths[0];
            if (!isSupportedDocumentPath(filePath)) {
                return { success: false, reason: 'unsupported-file-type' };
            }
            return { success: true, filePath };
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
                const handle = await openReadOnlyRegularFile(selectedPath, flags);
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
            return {
                success: false,
                reason: error?.code === 'SYMLINK_REJECTED' ? 'file-check-error' : 'file-read-error',
                error: error.message,
            };
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
            handle = await openReadOnlyRegularFile(selected.filePath, flags);
            const stat = await handle.stat();
            if (!stat.isFile()) {
                await handle.close();
                handle = null;
                return { success: false, reason: 'file-check-error', error: '只能读取普通文档文件' };
            }
            if (stat.size > maxSourceBytes) {
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
            return {
                success: false,
                reason: error?.code === 'SYMLINK_REJECTED' ? 'file-check-error' : 'file-read-error',
                error: error.message,
            };
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
            temporaryFile = await writeExclusiveTemporaryFile(result.filePath, bytes);
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

    async function saveFileStream(event, openStream, suggestedName, options = {}) {
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
        if (options?.signal?.aborted) return { success: false, reason: 'user-cancelled' };

        let response = null;
        let target = null;
        let temporaryPath = null;
        let abortListenerAttached = false;
        const signal = options?.signal || null;
        const onAbort = () => {
            const error = new Error('file save was cancelled');
            error.name = 'AbortError';
            error.code = 'USER_CANCELLED';
            response?.destroy?.(error);
            target?.destroy?.(error);
        };
        try {
            signal?.addEventListener?.('abort', onAbort, { once: true });
            abortListenerAttached = Boolean(signal);
            response = await openStream({ signal });
            if (signal?.aborted) return { success: false, reason: 'user-cancelled' };
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
            if (Number.isFinite(declaredLength) && declaredLength > maxStreamBytes) {
                response.destroy?.(new Error('file is too large'));
                return { success: false, reason: 'file-too-large' };
            }

            const temporary = await createExclusiveTemporaryStream(result.filePath);
            temporaryPath = temporary.path;
            target = temporary.stream;
            let received = 0;
            const bounded = new Transform({
                transform(chunk, _encoding, callback) {
                    received += chunk.length;
                    if (received > maxStreamBytes) {
                        const error = new Error('file is too large');
                        error.code = 'RESOURCE_EXHAUSTED';
                        callback(error);
                        return;
                    }
                    try {
                        options?.onProgress?.({
                            receivedBytes: received,
                            totalBytes: Number.isFinite(declaredLength) && declaredLength >= 0 ? declaredLength : null,
                        });
                    } catch (_) { /* UI progress is best effort */ }
                    callback(null, chunk);
                },
            });
            if (signal) await pipeline(response, bounded, target, { signal });
            else await pipeline(response, bounded, target);
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
            target?.destroy?.();
            logger.error('[main] 流式文件保存失败:', error);
            return {
                success: false,
                reason: signal?.aborted || error?.name === 'AbortError' || error?.code === 'USER_CANCELLED'
                    ? 'user-cancelled'
                    : error?.code === 'RESOURCE_EXHAUSTED' ? 'file-too-large' : 'download-error',
                error: error?.message,
            };
        } finally {
            if (abortListenerAttached) signal?.removeEventListener?.('abort', onAbort);
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
