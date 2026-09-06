/** Renderer module: workflow.service */
(function attachRendererFeature_workflow_service(root) {
    'use strict';

async function connectService(showToastOnStart = false) {
    const connectionAttemptId = ++serviceConnectionAttemptId;
    const retryButton = $('retry-service-btn');
    if (retryButton) retryButton.hidden = true;
    sourceImportServiceState = 'connecting';
    setAppInteractive(false);
    setServiceState('', '正在连接服务');
    refreshPendingServiceSourceFilePresentation();
    if (showToastOnStart) showToast('正在连接生成服务...');

    if (isElectron) {
        let ready = false;
        try {
            ready = await window.electronAPI.serverReady();
        } catch (error) {
            console.error('检查生成服务状态失败:', error);
        }
        if (!isCurrentServiceConnectionAttempt(connectionAttemptId)) return false;
        if (!ready) {
            sourceImportServiceState = 'unavailable';
            setServiceState('error', '服务连接失败');
            refreshPendingServiceSourceFilePresentation();
            if (retryButton) retryButton.hidden = false;
            showToast('生成服务启动失败，请重试连接');
            return false;
        }
    }

    const configLoaded = await loadConfig({
        shouldContinue: () => isCurrentServiceConnectionAttempt(connectionAttemptId),
    });
    if (!isCurrentServiceConnectionAttempt(connectionAttemptId)) return false;
    if (!configLoaded) {
        sourceImportServiceState = 'unavailable';
        setServiceState('warning', '服务状态异常');
        refreshPendingServiceSourceFilePresentation();
        if (retryButton) retryButton.hidden = false;
        showToast('生成服务暂不可用，请重试连接');
        return false;
    }

    if (currentConfig?.tts_engine === 'xunfei' && currentConfig.xunfei_available === false) {
        sourceImportServiceState = 'unavailable';
        setServiceState('error', '讯飞配音依赖未就绪');
        refreshPendingServiceSourceFilePresentation();
        if (retryButton) retryButton.hidden = false;
        showToast('讯飞配音依赖未就绪，请安装 Playwright 浏览器后重试');
        return false;
    }

    sourceImportServiceState = 'ready';
    setServiceState('ready', '服务已连接');
    setAppInteractive(true);
    schedulePendingServiceSourceFileImport();
    if (currentSession?.session_id) void hydrateWorkflowWorkspace(currentSession.session_id);
    return true;
}

function isCurrentServiceConnectionAttempt(attemptId) {
    return attemptId === serviceConnectionAttemptId;
}


registerRendererModule("workflow.service", {
    connectService,
    isCurrentServiceConnectionAttempt,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

