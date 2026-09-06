/** Renderer module: delivery.transfer */
(function attachRendererFeature_delivery_transfer(root) {
    'use strict';

function nativeFileFailureMessage(reason) {
    const messages = {
        'window-unavailable': '当前应用窗口不可用，请重新打开应用后再试。',
        'untrusted-sender': '当前页面没有访问本机文件的权限。',
        'path-check-failed': '待保存文件不在应用的安全目录中。',
        'file-not-found': '待保存文件已经不存在，可能已被清理。',
        'file-check-error': '无法检查待保存文件。',
        'file-too-large': '文件超过本机允许的大小上限。',
        'file-read-error': '无法读取所选文档内容。',
        'content-invalid': '待保存内容无效。',
        'write-error': '无法把文件写入所选位置。',
        'dialog-error': '系统文件对话框未能打开。',
        'copy-error': '无法把文件复制到所选位置。',
        'download-error': '生成文件下载失败，任务中的 Artifact 仍会保留。',
        'ipc-error': '桌面文件服务暂时没有响应。',
    };
    return messages[reason] || '文件操作没有完成。';
}

async function showNativeFileDialogError(title, result = {}) {
    const reason = result?.reason || 'ipc-error';
    const diagnostic = result?.error ? `\n技术信息：${result.error}` : '';
    await showAlertDialog({
        kicker: '本机文件',
        title,
        message: nativeFileFailureMessage(reason),
        detail: `请稍后重试；如果问题持续存在，请重新启动应用。${diagnostic}`,
        tone: 'danger',
        confirmLabel: '知道了',
    });
}

async function saveNativeFile(sourceBytes, suggestedName) {
    try {
        const result = await window.electronAPI.saveFile(sourceBytes, suggestedName);
        if (result?.success) {
            showToast('下载成功');
            return true;
        }
        if (result?.reason === 'user-cancelled') {
            showToast('已取消');
            return false;
        }
        await showNativeFileDialogError('下载文件失败', result || {
            reason: 'ipc-error',
            error: '主进程未返回文件操作结果',
        });
        return false;
    } catch (error) {
        console.error('调用系统保存框失败:', error);
        await showNativeFileDialogError('下载文件失败', {
            reason: 'ipc-error',
            error: error?.message,
        });
        return false;
    }
}

function hideArtifactTransferProgress() {
    const panel = $('artifact-transfer');
    if (panel) panel.hidden = true;
    const cancelButton = $('cancel-artifact-transfer-btn');
    if (cancelButton) {
        cancelButton.disabled = false;
        cancelButton.removeAttribute('aria-busy');
    }
}

function renderArtifactTransferProgress(progress = {}) {
    const panel = $('artifact-transfer');
    const label = $('artifact-transfer-label');
    const value = $('artifact-transfer-value');
    const bar = $('artifact-transfer-bar');
    const cancelButton = $('cancel-artifact-transfer-btn');
    if (!panel || !label || !value || !bar) return;
    panel.hidden = false;
    const state = String(progress.state || 'transferring');
    const received = Math.max(0, Number(progress.receivedBytes) || 0);
    const total = Number(progress.totalBytes);
    const hasTotal = Number.isFinite(total) && total > 0;
    const percent = hasTotal ? Math.min(100, Math.floor(received * 100 / total)) : null;
    label.textContent = state === 'starting'
        ? '正在准备文件传输'
        : state === 'cancelling'
            ? '正在取消文件传输'
        : state === 'cancelled'
            ? '已取消文件传输'
            : state === 'failed'
                ? '文件传输失败'
                : state === 'completed'
                    ? '文件已保存'
                    : '正在保存文件';
    if (percent === null) {
        bar.removeAttribute('value');
        value.textContent = received > 0 ? formatSourceBytes(received) : '处理中';
    } else {
        bar.value = percent;
        value.textContent = `${percent}%`;
    }
    if (cancelButton) {
        const active = !['completed', 'failed', 'cancelled'].includes(state);
        cancelButton.hidden = !active;
        cancelButton.disabled = Boolean(activeArtifactTransfer?.cancelRequested) || !active;
        cancelButton.setAttribute('aria-busy', activeArtifactTransfer?.cancelRequested ? 'true' : 'false');
    }
}

async function cancelArtifactTransfer() {
    const transfer = activeArtifactTransfer;
    if (!transfer || transfer.cancelRequested) return false;
    transfer.cancelRequested = true;
    renderArtifactTransferProgress({ ...transfer.lastProgress, state: 'cancelling' });
    try {
        if (typeof window.electronAPI?.cancelArtifactDownload !== 'function') {
            transfer.cancelRequested = false;
            renderArtifactTransferProgress(transfer.lastProgress);
            showToast('当前版本暂不支持取消文件传输', 'warning');
            return false;
        }
        await window.electronAPI.cancelArtifactDownload(transfer.transferId);
        return true;
    } catch (error) {
        transfer.cancelRequested = false;
        renderArtifactTransferProgress(transfer.lastProgress);
        showToast('取消下载请求未能发出，请稍后重试', 'warning');
        return false;
    }
}

async function saveNativeArtifactStream(artifactId, suggestedName) {
    if (typeof window.electronAPI?.startArtifactDownload === 'function'
        && typeof window.electronAPI?.onArtifactDownloadProgress === 'function') {
        if (activeArtifactTransfer) {
            showToast('已有一个文件正在下载', 'warning');
            return false;
        }
        const transferId = `artifact-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
        let resolveTransfer;
        const resultPromise = new Promise(resolve => { resolveTransfer = resolve; });
        let resultTimeout = null;
        activeArtifactTransfer = {
            transferId,
            artifactId: String(artifactId),
            suggestedName,
            resolve: resolveTransfer,
            cancelRequested: false,
            lastProgress: { transferId, state: 'starting', receivedBytes: 0, totalBytes: null },
        };
        renderArtifactTransferProgress(activeArtifactTransfer.lastProgress);
        try {
            const started = await window.electronAPI.startArtifactDownload(artifactId, suggestedName, transferId);
            if (!started?.success) {
                if (activeArtifactTransfer?.transferId === transferId) activeArtifactTransfer = null;
                hideArtifactTransferProgress();
                if (started?.reason === 'user-cancelled') {
                    showToast('已取消');
                    return false;
                }
                await showNativeFileDialogError('下载文件失败', started || { reason: 'ipc-error' });
                return false;
            }
            const result = await Promise.race([
                resultPromise,
                new Promise(resolve => {
                    resultTimeout = window.setTimeout(() => resolve({
                        success: false,
                        reason: 'download-timeout',
                        error: '文件传输长时间没有返回结果，请检查任务状态后重试',
                    }), 15 * 60 * 1000);
                }),
            ]);
            if (result?.reason === 'download-timeout') {
                void cancelArtifactTransfer();
                if (activeArtifactTransfer?.transferId === transferId) activeArtifactTransfer = null;
                hideArtifactTransferProgress();
            }
            if (result?.success) {
                showToast('下载成功');
                return true;
            }
            if (result?.reason === 'user-cancelled') {
                showToast('已取消');
                return false;
            }
            await showNativeFileDialogError('下载文件失败', result || { reason: 'download-error' });
            return false;
        } catch (error) {
            console.error('调用流式保存服务失败:', error);
            if (activeArtifactTransfer?.transferId === transferId) activeArtifactTransfer = null;
            hideArtifactTransferProgress();
            await showNativeFileDialogError('下载文件失败', {
                reason: 'ipc-error',
                error: error?.message,
            });
            return false;
        } finally {
            if (resultTimeout) window.clearTimeout(resultTimeout);
        }
    }
    try {
        const result = await window.electronAPI.saveArtifactStream(artifactId, suggestedName);
        if (result?.success) {
            showToast('下载成功');
            return true;
        }
        if (result?.reason === 'user-cancelled') {
            showToast('已取消');
            return false;
        }
        await showNativeFileDialogError('下载文件失败', result || {
            reason: 'ipc-error',
            error: '主进程未返回流式文件操作结果',
        });
        return false;
    } catch (error) {
        console.error('调用流式保存服务失败:', error);
        await showNativeFileDialogError('下载文件失败', {
            reason: 'ipc-error',
            error: error?.message,
        });
        return false;
    }
}

async function saveArtifactBytes(bytes, suggestedName, format = '') {
    if (!(bytes instanceof Uint8Array)) throw new TypeError('Artifact 内容不是字节流');
    if (isElectron) return saveNativeFile(bytes, suggestedName);
    const url = URL.createObjectURL(new Blob([bytes], { type: artifactMime(format) }));
    try {
        const link = document.createElement('a');
        link.href = url;
        link.download = suggestedName;
        link.click();
        showToast('下载成功');
        return true;
    } finally {
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
}

async function hydrateDownloadContext(target, workflowId) {
    if (typeof workflowApi?.getWorkspace !== 'function') {
        return target?.workspace || currentWorkspace || null;
    }
    const workspace = await workflowApi.getWorkspace(workflowId);
    if (target?.mode === 'current'
        && currentSession?.session_id
        && String(currentSession.session_id) !== String(workflowId)) {
        const error = new Error('当前任务已切换');
        error.name = 'AbortError';
        throw error;
    }
    if (!workspace || !Array.isArray(workspace.items) || !Array.isArray(workspace.artifacts)) {
        const error = new Error('任务工作区暂时无法读取');
        error.code = 'WORKSPACE_UNAVAILABLE';
        throw error;
    }

    const files = resultFilesFromArtifacts(workspace.items, workspace.artifacts, workspace);
    target.workspace = workspace;
    target.files = files;
    target.delivery = workspace.delivery || null;
    target.zipAvailable = workspace.delivery?.zip_available === true;
    target.zipArtifactId = workspace.delivery?.zip_artifact_id || null;
    target.stateVersion = Number(workspace.snapshot?.state_version ?? target.stateVersion ?? 0);

    if (target.mode === 'current' && currentSession?.session_id === workflowId) {
        currentWorkspace = workspace;
        workflowStore?.hydrate?.(workspace, { snapshot: workspace.snapshot });
        mergeWorkflowSnapshotIntoSession(workspace.snapshot, currentSession);
        currentSession.delivery = workspace.delivery ? {
            zip_available: workspace.delivery.zip_available === true,
            zip_artifact_id: workspace.delivery.zip_artifact_id || null,
        } : null;
        if (typeof renderWorkspaceAfterHydrate === 'function') {
            renderWorkspaceAfterHydrate(currentWorkspace, currentSession, workflowId);
        } else {
            renderWorkspaceShell(currentWorkspace, currentSession);
        }
    }
    return workspace;
}

async function downloadZip(context = activeResultContext) {
    const target = context || (currentSession ? {
        mode: 'current',
        sessionId: currentSession.session_id,
        workflowId: currentSession.session_id,
        sourceFilename: currentSession.source_filename,
    } : null);
    if (!target) return;
    try {
        const workflowId = target.workflowId || target.sessionId || target.recordId;
        if (!workflowApi || !workflowId) throw new Error('工作流标识缺失');
        // A history-row context may contain a ZIP id from an earlier list
        // refresh. Hydrate the server-owned workspace before selecting bytes;
        // otherwise a concurrent retry in another window could make the row's
        // immutable ZIP stale while the button still downloads it.
        let projectedWorkspace = target.mode === 'current'
            ? (currentWorkspace || target.workspace || null)
            : (target.workspace || null);
        if (typeof workflowApi.getWorkspace === 'function') {
            // Refresh all item, Artifact, and delivery facts together immediately
            // before choosing the bytes. A stale ZIP id or stale filename must
            // never be used just because the result page was left open.
            projectedWorkspace = await hydrateDownloadContext(target, workflowId);
        }
        const projectedDelivery = projectedWorkspace?.delivery
            || (!projectedWorkspace ? target.delivery : null)
            || null;
        const hasAuthoritativeDelivery = Boolean(
            projectedDelivery && typeof projectedDelivery === 'object'
            && ('zip_available' in projectedDelivery || 'zip_artifact_id' in projectedDelivery),
        );
        // A raw Artifact list is not enough to identify the current delivery
        // scope: it can contain immutable ZIPs from an older run or an older
        // subset export.  Only the server-owned workspace projection may
        // select an already-created ZIP; otherwise ask the idempotent export
        // command to derive the current full scope.
        let artifactId = hasAuthoritativeDelivery
            ? (projectedDelivery.zip_available === true ? projectedDelivery.zip_artifact_id : null)
            : (target.mode === 'history' ? null : (target.zipArtifactId || null));
        if (!artifactId) {
            const exportAction = workflowAdapter.action?.(
                projectedWorkspace || (target.mode === 'current' ? currentWorkspace : null),
                'EXPORT_ZIP',
            );
            if (target.mode === 'current' && exportAction?.enabled === true && workflowCommandCoordinator) {
                const outcome = await workflowCommandCoordinator.run(exportAction, {
                    reason: 'desktop-export-zip',
                });
                if (!outcome.ok) {
                    const error = new Error(outcome.reason || 'ZIP 交付操作未完成');
                    error.code = outcome.reason === 'action-disabled-after-refresh'
                        ? 'STATE_CONFLICT'
                        : 'EXPORT_ZIP_FAILED';
                    throw error;
                }
                artifactId = outcome.response?.artifact?.artifact_id
                    || outcome.workspace?.delivery?.zip_artifact_id
                    || null;
            } else {
                const currentSnapshot = target.mode === 'current'
                    ? await refreshCurrentWorkflowSnapshot(currentSession)
                    : await workflowApi.getWorkflow(workflowId);
                const expectedStateVersion = Number(
                    currentSnapshot?.state_version
                    ?? target.stateVersion
                    ?? currentSession?.state_version
                    ?? 0,
                );
                const artifact = await workflowApi.createExportZip(workflowId, {
                    expected_state_version: expectedStateVersion,
                });
                artifactId = artifact?.artifact_id || null;
            }
            if (artifactId) {
                target.zipArtifactId = artifactId;
                target.zipAvailable = true;
            }
        }
        if (!artifactId) {
            showToast('当前工作流没有可下载的 ZIP Artifact');
            return false;
        }
        const downloadName = deliveryZipFilename(target.sourceFilename);
        if (isElectron && typeof window.electronAPI?.saveArtifactStream === 'function') {
            return saveNativeArtifactStream(artifactId, downloadName);
        }
        const bytes = await readArtifactBytes(artifactId);
        return saveArtifactBytes(bytes, downloadName, 'zip');
    } catch (err) {
        console.error('下载 ZIP 异常:', err);
        showToast('下载失败：Artifact 暂时不可用');
        return false;
    }
}

async function downloadFile(fileOrFilename, context = activeResultContext) {
    const target = context || (currentSession ? {
        mode: 'current',
        sessionId: currentSession.session_id,
        workflowId: currentSession.session_id,
        files: generatedFiles,
    } : null);
    const requestedFile = fileOrFilename && typeof fileOrFilename === 'object'
        ? fileOrFilename
        : null;
    const requestedArtifactId = String(requestedFile?.artifact_id || '').trim();
    const requestedFilename = String(
        requestedFile?.filename || fileOrFilename || '',
    ).trim();
    if (!target || (!requestedArtifactId && !requestedFilename)) return;
    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        const workflowId = target.workflowId || target.sessionId || target.recordId;
        if (workflowId && typeof workflowApi.getWorkspace === 'function') {
            await hydrateDownloadContext(target, workflowId);
        }
        const file = (Array.isArray(target.files) ? target.files : []).find(item => (
            requestedArtifactId
                ? String(item?.artifact_id || '') === requestedArtifactId
                : item?.filename === requestedFilename
        ));
        if (!file?.artifact_id) {
            showToast('这条音频已被更新或暂未通过核验，请刷新任务后重试', 'warning');
            return false;
        }
        const filename = filenameWithExtension(
            file.filename || requestedFilename,
            file.format || 'mp3',
            '音频文件',
        );
        if (isElectron && typeof window.electronAPI?.saveArtifactStream === 'function') {
            return saveNativeArtifactStream(file.artifact_id, filename);
        }
        const bytes = await readArtifactBytes(file.artifact_id);
        return saveArtifactBytes(bytes, filename, file.format || String(filename).split('.').pop());
    } catch (err) {
        console.error('下载音频异常:', err);
        showToast('下载失败：Artifact 暂时不可用');
        return false;
    }
}


registerRendererModule("delivery.transfer", {
    nativeFileFailureMessage,
    showNativeFileDialogError,
    saveNativeFile,
    hideArtifactTransferProgress,
    renderArtifactTransferProgress,
    cancelArtifactTransfer,
    saveNativeArtifactStream,
    saveArtifactBytes,
    hydrateDownloadContext,
    downloadZip,
    downloadFile,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
