/** Renderer module: config.presets */
(function attachRendererFeature_config_presets(root) {
    'use strict';

// ============================================================================
// 配置预设管理 (localStorage 持久化)
// ============================================================================

// 讯飞参数与旧版倍率/音色配置不兼容，使用新存储命名空间避免旧值被误套用。
const PRESET_STORAGE_KEY = 'wordtts_presets_xunfei_v3';
const CURRENT_CONFIG_STORAGE_KEY = 'wordtts_current_config_xunfei_v3';

/**
 * 保存尚未创建为预设的当前配置。Windows 渲染进程或页面意外重载后，
 * 仍可恢复用户刚刚选择的参数。
 */
function saveCurrentConfig(config) {
    if (!config) return false;
    try {
        rendererStorage?.setItem(CURRENT_CONFIG_STORAGE_KEY, JSON.stringify(normalizePersistedConfig(config)));
        return true;
    } catch (e) {
        console.error('保存当前配置失败:', e);
        return false;
    }
}

function loadCurrentConfig() {
    try {
        const raw = rendererStorage?.getItem(CURRENT_CONFIG_STORAGE_KEY);
        if (!raw) return null;
        const config = JSON.parse(raw);
        return config && typeof config === 'object' && !Array.isArray(config)
            ? normalizePersistedConfig(config)
            : null;
    } catch (e) {
        console.error('读取当前配置失败:', e);
        return null;
    }
}

function rememberCurrentConfig() {
    saveCurrentConfig(collectPersistedConfig());
}

/**
 * 从 localStorage 读取所有预设。
 */
function loadPresets() {
    try {
        const raw = rendererStorage?.getItem(PRESET_STORAGE_KEY);
        if (!raw) return [];
        const arr = JSON.parse(raw);
        if (!Array.isArray(arr)) return [];
        const sanitized = arr
            .filter(p => p && typeof p === 'object' && p.id && p.name)
            .map(p => ({ ...p, config: normalizePersistedConfig(p.config) }));
        // 迁移旧版本：旧预设可能带有角色映射/角色参数，读取时立即清理，
        // 保证后续任何一次保存都不会继续把文档角色写进长期配置。
        if (JSON.stringify(arr) !== JSON.stringify(sanitized)) {
            try {
                rendererStorage?.setItem(PRESET_STORAGE_KEY, JSON.stringify(sanitized));
            } catch (_) {
                // 迁移失败不影响本次使用，savePresets 仍会在下次操作时重试。
            }
        }
        return sanitized;
    } catch (e) {
        console.error('读取预设失败:', e);
        return [];
    }
}

/**
 * 保存预设列表到 localStorage。
 */
function savePresets(presets) {
    try {
        const sanitized = Array.isArray(presets)
            ? presets.map(p => ({ ...p, config: normalizePersistedConfig(p?.config) }))
            : [];
        rendererStorage?.setItem(PRESET_STORAGE_KEY, JSON.stringify(sanitized));
        return true;
    } catch (e) {
        console.error('保存预设失败:', e);
        showToast('保存失败：存储空间不足');
        return false;
    }
}

function showPromptDialog(title, message, defaultValue = '') {
    return window.WordTTSUI.prompt({
        title,
        message,
        defaultValue,
        inputLabel: '配置名称',
        confirmLabel: '保存配置',
    });
}

function showConfirmDialog(options) {
    return window.WordTTSUI.confirm(options);
}

function showAlertDialog(options) {
    return window.WordTTSUI.alert(options);
}


registerRendererModule("config.presets", {
    PRESET_STORAGE_KEY,
    CURRENT_CONFIG_STORAGE_KEY,
    saveCurrentConfig,
    loadCurrentConfig,
    rememberCurrentConfig,
    loadPresets,
    savePresets,
    showPromptDialog,
    showConfirmDialog,
    showAlertDialog,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

