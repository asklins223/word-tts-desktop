/** Renderer module: core.notifications */
(function attachRendererFeature_core_notifications(root) {
    'use strict';

// ============================================================================
// Toast
// ============================================================================

function showToast(msg, tone = 'info') {
    let toast = document.querySelector('.toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.className = 'toast';
        document.body.appendChild(toast);
    }
    toast.classList.remove('is-info', 'is-success', 'is-warning', 'is-error');
    toast.classList.add(`is-${tone}`);
    toast.setAttribute('role', tone === 'error' ? 'alert' : 'status');
    toast.setAttribute('aria-live', tone === 'error' ? 'assertive' : 'polite');
    toast.textContent = msg;
    toast.classList.add('show');

    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
        toast.classList.remove('show');
    }, tone === 'error' ? 5200 : 2800);
}

// ============================================================================
// Store 订阅式进度投影（T8）
// ============================================================================

/**
 * 进度 UI 的权威数值来自 workflowStore 的 workspace 投影：只有被 Store
 * 接受（顺序校验、去重）的 snapshot/event 才会推进这里的渲染。断线重连、
 * 快照重同步或补齐后，订阅回调会自动把界面拉回投影的最新值，不再依赖
 * “逐个事件各写一次 DOM”的时序。
 */

registerRendererModule("core.notifications", {
    showToast,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

