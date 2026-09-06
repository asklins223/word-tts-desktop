/** Renderer module: source.transport */
(function attachRendererFeature_source_transport(root) {
    'use strict';


async function processSourceBytes(bytes, filename, options = {}) {
    return processSourceContent(bytes, filename, bytes?.byteLength, options);
}

async function processSourceFileReference(sourceFileId, filename, sizeBytes, options = {}) {
    const size = Number(sizeBytes);
    if (!sourceFileId || !Number.isSafeInteger(size) || size <= 0) {
        void releaseNativeSourceFile(sourceFileId);
        showToast('文档大小无效，请重新选择', 'error');
        return;
    }
    return processSourceContent({ sourceFileId: String(sourceFileId) }, filename, size, options);
}

async function releaseNativeSourceFile(sourceFileId) {
    if (!sourceFileId || !isElectron || typeof window.electronAPI?.releaseSourceFile !== 'function') return;
    try {
        await window.electronAPI.releaseSourceFile(String(sourceFileId));
    } catch (error) {
        console.warn('释放原生文档句柄失败:', error);
    }
}

async function processSourceContent(content, filename, expectedSizeBytes, options = {}) {
    const isBytes = content instanceof Uint8Array;
    const hasSourceFileReference = Boolean(content && typeof content === 'object' && content.sourceFileId);
    const sourceFileId = hasSourceFileReference ? String(content.sourceFileId) : '';
    if (isForcedUpdateBlocking()) {
        void releaseNativeSourceFile(sourceFileId);
        return;
    }
    if (isParsing || isRestarting) {
        void releaseNativeSourceFile(sourceFileId);
        return;  // 防止重入
    }
    const expectedSize = Number(expectedSizeBytes);
    if ((!isBytes && !hasSourceFileReference) || !Number.isSafeInteger(expectedSize) || expectedSize <= 0) {
        void releaseNativeSourceFile(sourceFileId);
        showToast('文档内容为空，请重新选择', 'error');
        return;
    }
    const safeFilename = String(filename || 'source.docx').split(/[\\/]/).pop() || 'source.docx';
    const extension = safeFilename.toLowerCase().slice(safeFilename.lastIndexOf('.'));
    if (!['.docx', '.xlsx'].includes(extension)) {
        void releaseNativeSourceFile(sourceFileId);
        showToast('请选择 .docx 或 .xlsx 格式的文档', 'error');
        return;
    }
    isParsing = true;
    const attemptId = ++parseAttemptId;
    const controller = options.controller || sourceImportController || new AbortController();
    if (!sourceImportController) {
        sourceImportController = controller;
        sourceImportInFlight = true;
    }
    parseAbortController = controller;

    const uploadZone = $('upload-zone');
    uploadZone.classList.add('has-file');
    setUploadParsing(true);
    setUploadFeedback('info', '正在读取并核对文档结构，请稍候…');
    uploadZone.querySelector('.upload-text-large').textContent = safeFilename;
    uploadZone.querySelector('.upload-hint').textContent = '正在解析文档结构...';
    $('status-text').textContent = `正在解析: ${safeFilename}`;

    try {
        if (!workflowApi) throw new Error('工作流服务未初始化');
        throwIfSourceImportAborted(controller.signal);
        const initialConfiguration = buildWorkflowConfiguration(
            collectConfig(false),
            safeFilename,
            currentConfig?.account_scope,
        );
        const draft = await workflowApi.createWorkflow({
            workflow_type: 'tts',
            configuration: initialConfiguration,
        });
        throwIfSourceImportAborted(controller.signal);
        const imported = await workflowApi.createSourceImport(draft.workflow_id, {
            metadata: { filename: safeFilename },
            expected_size_bytes: expectedSize,
            content_type: safeFilename.toLowerCase().endsWith('.xlsx')
                ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        });
        sourceImportId = imported.source_import_id || null;
        setUploadParsing(true);
        updateSourceImportProgress('正在写入受控存储');
        throwIfSourceImportAborted(controller.signal);
        sourceTransportUploadId = `source-upload-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
        await workflowApi.writeSourceImport(imported.source_import_id, imported.staging_generation, content, {
            signal: controller.signal,
            uploadId: sourceTransportUploadId,
            onProgress: ({ receivedBytes, totalBytes }) => {
                updateSourceImportProgress('正在上传源文档', receivedBytes, totalBytes || expectedSize);
                setUploadFeedback(
                    'info',
                    `正在上传源文档 · ${formatSourceBytes(receivedBytes)} / ${formatSourceBytes(totalBytes || expectedSize)}`,
                );
            },
        });
        throwIfSourceImportAborted(controller.signal);
        const ready = await workflowApi.getSourceImport(imported.source_import_id);
        throwIfSourceImportAborted(controller.signal);
        if (!ready.source_artifact_id) throw new Error('文档内容未能写入受控存储');
        // Committing the source artifact advances the workflow aggregate's
        // state_version. Re-read it before publishing parse output instead of
        // reusing the draft version captured before the upload.
        const sourceWorkflow = await workflowApi.getWorkflow(draft.workflow_id);
        throwIfSourceImportAborted(controller.signal);
        const data = await workflowApi.parseWorkflow(draft.workflow_id, {
            expected_state_version: Number(sourceWorkflow?.state_version ?? draft.state_version),
            source_artifact_id: ready.source_artifact_id,
        });
        if (controller.signal.aborted || attemptId !== parseAttemptId) return;
        const parseResults = Array.isArray(data.parse_results) ? data.parse_results : [];
        if (parseResults.length === 0) {
            throw new Error('未识别到支持的题型内容，请检查文档结构后重试');
        }
        resetTaskVoiceConfiguration();
        resetReviewNavigationState();
        currentSession = {
            session_id: draft.workflow_id,
            source_filename: data.source_filename || safeFilename,
            source_artifact_id: data.source_artifact_id || ready.source_artifact_id,
            import_id: imported.source_import_id,
            state_version: Number(data.state_version || data.current_snapshot?.state_version || draft.state_version),
            parse_results: parseResults,
        };
        currentWorkspace = null;
        workflowStore?.prepare?.(currentSession.session_id, {
            workflow: {
                workflow_id: currentSession.session_id,
                state_version: currentSession.state_version,
                execution_state: 'PREPARING',
                control_state: 'RUNNING',
                result_status: 'IN_PROGRESS',
            },
            lastSeq: 0,
        });

        updateSessionLabels(currentSession.source_filename, currentSession.parse_results);
        renderContentReview(currentSession.parse_results);
        uploadZone.querySelector('.upload-hint').textContent = '解析完成，正在打开声音配置';
        setUploadFeedback('success', `解析完成：已识别 ${summarizeParseResults(currentSession.parse_results).total} 条内容。`);
        $('status-text').textContent = `解析成功 — ${currentSession.source_filename}`;
        showToast('文档解析成功，进入配置步骤');

        // 解析完成后先进入可编辑核对，再由用户确认后进入配置中心。
        showContentReview();
        void hydrateWorkflowWorkspace(currentSession.session_id, { silent: true });

    } catch (err) {
        if (err.name === 'AbortError' || attemptId !== parseAttemptId) {
            if (err.name === 'AbortError' && sourceImportId) {
                await abortSourceImportIfPossible(sourceImportId);
            }
            if (attemptId === parseAttemptId) {
                setUploadFeedback('info', sourceImportId
                    ? '已停止等待；源文件状态已保留，请确认后再重新导入。'
                    : '已停止导入，可重新选择文档。');
                $('status-text').textContent = '已停止导入';
            }
            return;
        }
        const errorMessage = String(err?.message || '未知错误');
        const startupRecoveryFailed = err?.code === 'PERSISTENCE_ERROR'
            && /workflow startup recovery failed/i.test(errorMessage);
        const feedbackMessage = startupRecoveryFailed
            ? '工作流启动恢复失败，请重启应用后重试。'
            : `文档解析失败：${errorMessage}`;
        console.error('导入失败:', err);
        showToast(startupRecoveryFailed ? '工作流启动恢复失败，请重启应用后重试' : `解析失败: ${errorMessage}`, 'error');
        uploadZone.classList.remove('has-file');
        uploadZone.querySelector('.upload-text-large').textContent = '拖拽文档到这里，或点击选择';
        uploadZone.querySelector('.upload-hint').textContent = '请检查文档格式或内容后重新选择';
        setUploadFeedback('error', feedbackMessage);
        $('status-text').textContent = startupRecoveryFailed
            ? '工作流启动恢复失败，请重启应用'
            : '文档解析失败，请重新选择';
    } finally {
        if (attemptId === parseAttemptId) {
            if (parseAbortController === controller) parseAbortController = null;
            isParsing = false;
            if (sourceImportController === controller) {
                sourceImportController = null;
                sourceImportInFlight = false;
                sourceImportId = null;
                sourceTransportUploadId = null;
            }
            // Clear the flags before syncing the controls. Otherwise the
            // completed parse leaves the toolbar's new-task button disabled.
            setUploadParsing(false);
            updateSourceImportProgress();
        }
        await releaseNativeSourceFile(sourceFileId);
    }
}


registerRendererModule("source.transport", {
    processSourceBytes,
    processSourceFileReference,
    releaseNativeSourceFile,
    processSourceContent,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

