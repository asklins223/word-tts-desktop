/** Renderer module: app.configLoader */
(function attachRendererFeature_app_configLoader(root) {
    'use strict';

// ============================================================================
// 配置加载
// ============================================================================

async function loadConfig({ shouldContinue = () => true } = {}) {
    const maxRetries = 3;
    for (let attempt = 1; attempt <= maxRetries; attempt++) {
        if (!shouldContinue()) return false;
        try {
            if (!workflowApi) throw new Error('工作流服务未初始化');
            const loadedConfig = await workflowApi.getConfig();
            if (!shouldContinue()) return false;
            currentConfig = loadedConfig;

            setVoiceCatalog(
                currentConfig.voices,
                currentConfig.voice_filters,
                currentConfig.voice_aliases,
            );
            const migratedVoiceSelections = migrateVoiceSelections();
            if (!clientConfigInitialized) {
                const normalized = normalizeClientConfig(currentConfig);
                selectedDefaultFemaleVoice = normalized.default_female_voice;
                selectedDefaultMaleVoice = normalized.default_male_voice;
                voiceParamConfigs = Object.fromEntries(
                    Object.entries(normalized.role_configs || {}).map(([key, value]) => [key, { ...value }]),
                );
                if (!voiceParamConfigs[DEFAULT_FEMALE_ROLE_KEY]) voiceParamConfigs[DEFAULT_FEMALE_ROLE_KEY] = createDefaultVoiceParams(DEFAULT_FEMALE_ROLE_KEY);
                if (!voiceParamConfigs[DEFAULT_MALE_ROLE_KEY]) voiceParamConfigs[DEFAULT_MALE_ROLE_KEY] = createDefaultVoiceParams(DEFAULT_MALE_ROLE_KEY);
                clientConfigInitialized = true;
            }
            if (migratedVoiceSelections) rememberCurrentConfig();
            renderVoiceWorkspace();

            // 刷新摘要中的音色显示
            updateConfigSummary();

            return true;  // 成功，退出重试
        } catch (err) {
            if (!shouldContinue()) return false;
            console.error(`加载配置失败 (尝试 ${attempt}/${maxRetries}):`, err);
            if (attempt < maxRetries) {
                await new Promise(r => setTimeout(r, 1000 * attempt));
            } else {
                showToast('加载配置失败，部分功能可能不可用');
            }
        }
    }
    return false;
}


registerRendererModule("app.configLoader", {
    loadConfig,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

