'use strict';

const { randomBytes } = require('crypto');

const DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024;
const DEFAULT_TTL_MS = 10 * 60 * 1000;
const DEFAULT_MAX_SESSIONS = 8;

/**
 * 拖拽导入的分块暂存区。
 *
 * 渲染层拖入的文档不能再整块读进渲染进程内存（300MB 级文档是明确的支持
 * 目标）。渲染层按块读取 File 并通过 IPC 递给主进程，主进程把块写入受管
 * 暂存文件；begin 时用独占方式打开并持有文件句柄，complete 时把这个句柄
 * 移交给与原生对话框路径相同的一次性 sourceFileId，后续
 * `workflow-source-upload` 无需感知差异。任何一步失败或超时都会删除暂存
 * 文件，绝不留下无主临时数据。
 */
function createSourceUploadStaging({
    fs,
    path,
    stagingDir,
    maxBytes = 512 * 1024 * 1024,
    chunkSize = DEFAULT_CHUNK_SIZE,
    ttlMs = DEFAULT_TTL_MS,
    maxSessions = DEFAULT_MAX_SESSIONS,
    allowedExtensions = ['.docx', '.xlsx'],
    logger = console,
}) {
    if (!fs || !path || !stagingDir) throw new TypeError('fs, path and stagingDir are required');
    if (!Number.isSafeInteger(maxSessions) || maxSessions < 1) {
        throw new TypeError('maxSessions must be a positive safe integer');
    }
    const sessions = new Map();
    let pendingBegins = 0;

    const destroySession = async (session, reason) => {
        if (!session) return;
        sessions.delete(session.uploadId);
        if (session.expiryTimer) {
            clearTimeout(session.expiryTimer);
            session.expiryTimer = null;
        }
        try { await session.handle?.close?.(); } catch (_) { /* best effort */ }
        session.handle = null;
        if (session.stagingPath) {
            try { await fs.promises.unlink(session.stagingPath); } catch (_) { /* best effort */ }
            session.stagingPath = null;
        }
        if (reason && logger.debug) logger.debug(`[staging] upload ${session.uploadId} discarded: ${reason}`);
    };

    const armExpiry = (session) => {
        if (session.expiryTimer) clearTimeout(session.expiryTimer);
        session.expiryTimer = setTimeout(() => {
            void destroySession(sessions.get(session.uploadId), 'ttl-expired');
        }, ttlMs);
        session.expiryTimer.unref?.();
    };

    const assertActive = (uploadId) => {
        const session = sessions.get(String(uploadId || ''));
        if (!session) throw new Error('source upload session is missing or expired');
        return session;
    };

    return Object.freeze({
        get chunkSize() { return chunkSize; },
        get activeCount() { return sessions.size; },

        async begin({ fileName, sizeBytes, senderId }) {
            const safeName = String(fileName || 'source.docx').split(/[\\/]/).pop()
                .replace(/[\u0000-\u001f\u007f]/g, '').trim() || 'source.docx';
            const extension = path.extname(safeName).toLowerCase();
            const size = Number(sizeBytes);
            if (!allowedExtensions.includes(extension)) {
                throw new Error(`unsupported source document type: ${extension || 'unknown'}`);
            }
            if (!Number.isSafeInteger(size) || size <= 0 || size > maxBytes) {
                throw new Error(`source document size is invalid or exceeds the limit (${size})`);
            }
            if (sessions.size + pendingBegins >= maxSessions) {
                const error = new Error('too many source upload sessions are active');
                error.code = 'RESOURCE_EXHAUSTED';
                throw error;
            }
            pendingBegins += 1;
            try {
                const uploadId = `upload_${randomBytes(12).toString('hex')}`;
                await fs.promises.mkdir(stagingDir, { recursive: true });
                const stagingPath = path.join(stagingDir, `${uploadId}${extension}`);
                // Keep the exclusively-created descriptor for the entire
                // session. Re-opening by path for every chunk made a
                // legitimate symlinked user-data directory vulnerable to
                // O_NOFOLLOW/ELOOP failures and also introduced a replace
                // window between the path check and the actual write.
                const handle = await fs.promises.open(stagingPath, 'wx+');
                const session = {
                    uploadId,
                    senderId,
                    fileName: safeName,
                    sizeBytes: size,
                    stagingPath,
                    handle,
                    bytesWritten: 0,
                    expiryTimer: null,
                };
                sessions.set(uploadId, session);
                armExpiry(session);
                return { uploadId, fileName: safeName, sizeBytes: size, chunkSize };
            } finally {
                pendingBegins -= 1;
            }
        },

        async write({ uploadId, offset, bytes }, senderId) {
            const session = assertActive(uploadId);
            if (session.senderId !== senderId) throw new Error('source upload session belongs to another sender');
            const offsetNumber = Number(offset);
            if (!Number.isSafeInteger(offsetNumber) || offsetNumber < 0 || offsetNumber % chunkSize !== 0) {
                throw new Error('source upload chunk offset is invalid');
            }
            const chunk = bytes instanceof Uint8Array ? Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength) : bytes;
            if (!Buffer.isBuffer(chunk) || chunk.length <= 0) throw new Error('source upload chunk is empty');
            if (offsetNumber + chunk.length > session.sizeBytes) {
                await destroySession(session, 'size-exceeded');
                throw new Error('source upload exceeds the declared size');
            }
            if (offsetNumber !== session.bytesWritten) {
                await destroySession(session, 'out-of-order-chunk');
                throw new Error('source upload chunks must arrive in order');
            }
            // The descriptor was opened exclusively in begin() and is reused
            // here, so a user-data symlink/replace cannot redirect a later
            // chunk and a valid migrated data directory cannot trigger ELOOP.
            const handle = session.handle;
            if (!handle) {
                await destroySession(session, 'write-handle-missing');
                throw new Error('source upload file handle is unavailable');
            }
            try {
                let written = 0;
                while (written < chunk.length) {
                    const result = await handle.write(
                        chunk,
                        written,
                        chunk.length - written,
                        offsetNumber + written,
                    );
                    const bytesWritten = Number(result?.bytesWritten);
                    if (!Number.isSafeInteger(bytesWritten) || bytesWritten <= 0) {
                        throw new Error('source upload write made no progress');
                    }
                    written += bytesWritten;
                }
            } catch (error) {
                await destroySession(session, 'write-failed');
                throw error;
            }
            session.bytesWritten = offsetNumber + chunk.length;
            armExpiry(session);
            return { received: session.bytesWritten };
        },

        async complete({ uploadId }, senderId, { openStagedHandle }) {
            const session = assertActive(uploadId);
            if (session.senderId !== senderId) throw new Error('source upload session belongs to another sender');
            if (session.bytesWritten !== session.sizeBytes) {
                const expected = session.sizeBytes;
                await destroySession(session, 'incomplete');
                throw new Error(`source upload is incomplete: ${session.bytesWritten}/${expected}`);
            }
            const fileHandle = session.handle;
            if (!fileHandle) {
                await destroySession(session, 'verify-handle-missing');
                throw new Error('source upload file handle is unavailable');
            }
            try {
                await fileHandle.sync();
                const stat = await fileHandle.stat();
                if (!stat.isFile() || stat.size !== session.sizeBytes) {
                    throw new Error('staged source file does not match the declared size');
                }
            } catch (error) {
                try { await fileHandle.close(); } catch (_) { /* best effort */ }
                await destroySession(session, 'verify-failed');
                throw error;
            }
            sessions.delete(session.uploadId);
            if (session.expiryTimer) clearTimeout(session.expiryTimer);
            let opened;
            try {
                opened = await openStagedHandle({
                    filePath: session.stagingPath,
                    fileName: session.fileName,
                    sizeBytes: session.sizeBytes,
                    fileHandle,
                });
            } catch (error) {
                try { await fileHandle.close(); } catch (_) { /* best effort */ }
                await destroySession(session, 'handle-open-failed');
                throw error;
            }
            if (!opened?.success || !opened.sourceFileId) {
                try { await fileHandle.close(); } catch (_) { /* best effort */ }
                await destroySession(session, 'handle-open-failed');
                return opened || { success: false, reason: 'handle-open-failed' };
            }
            // 暂存文件的所有权移交给了调用方的 handle 生命周期；
            // session 不再负责删除。
            session.handle = null;
            session.stagingPath = null;
            return {
                success: true,
                sourceFileId: opened.sourceFileId,
                fileName: opened.fileName || session.fileName,
                sizeBytes: opened.sizeBytes ?? session.sizeBytes,
            };
        },

        async abort({ uploadId }) {
            const session = sessions.get(String(uploadId || ''));
            if (!session) return { success: true };
            await destroySession(session, 'aborted');
            return { success: true };
        },

        async disposeAll(reason = 'shutdown') {
            const pending = [...sessions.values()];
            for (const session of pending) {
                await destroySession(session, reason);
            }
            return pending.length;
        },

        async disposeSender(senderId, reason = 'renderer-reloaded') {
            const pending = [...sessions.values()].filter(session => session.senderId === senderId);
            for (const session of pending) {
                await destroySession(session, reason);
            }
            return pending.length;
        },
    });
}

module.exports = {
    createSourceUploadStaging,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAX_SESSIONS,
    DEFAULT_TTL_MS,
};
