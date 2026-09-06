/** Renderer module: workflow.config */
(function attachRendererFeature_workflow_config(root) {
    'use strict';

// ============================================================================
// Step 2 & 3: 配置 & 生成
// ============================================================================

function collectRecommendedDocumentRoleConfig() {
    const roleConfigs = {
        [DEFAULT_FEMALE_ROLE_KEY]: createDefaultVoiceParams(DEFAULT_FEMALE_ROLE_KEY),
        [DEFAULT_MALE_ROLE_KEY]: createDefaultVoiceParams(DEFAULT_MALE_ROLE_KEY),
    };
    const roleVoices = {};
    const parsedRoles = extractParsedRoleLabels(currentSession?.parse_results);
    parsedRoles.forEach(role => {
        const configKey = roleConfigKeyForRole(role);
        roleConfigs[configKey] = createDefaultVoiceParams(configKey);
        roleVoices[role.key] = inferRoleVoice(role.label);
    });
    return { roleConfigs, roleVoices };
}

function collectConfig(useDefaults) {
    if (useDefaults) {
        const recommendedRoles = collectRecommendedDocumentRoleConfig();
        return normalizeClientConfig({
            generation_mode: DEFAULT_GENERATION_MODE,
            rate: 50,
            volume: 50,
            pitch: 50,
            format: 'mp3',
            quality: '128 kbps（标准）',
            preview: false,
            default_female_voice: currentConfig?.default_female_voice || 'amanda',
            default_male_voice: currentConfig?.default_male_voice || 'george',
            role_configs: recommendedRoles.roleConfigs,
            role_voices: recommendedRoles.roleVoices,
        });
    }
    const activeParams = activeVoiceParams();
    return normalizeClientConfig({
        generation_mode: selectedGenerationMode(),
        rate: activeParams.rate,
        volume: activeParams.volume,
        pitch: activeParams.pitch,
        // 不从页面控件读取格式；格式始终由当前产品规则固定为 MP3。
        format: 'mp3',
        quality: $('quality').value,
        preview: $('preview').checked,
        default_female_voice: selectedDefaultFemaleVoice,
        default_male_voice: selectedDefaultMaleVoice,
        role_configs: voiceParamConfigs,
        role_voices: roleVoiceMap,
    });
}

function buildWorkflowConfiguration(config, sourceFilename = '', accountScope = '') {
    const normalized = normalizeClientConfig(config);
    const rawFilename = String(sourceFilename || config?.source_filename || '').trim().split(/[\\/]/).pop() || 'source.docx';
    const sourceSuffix = ['.docx', '.xlsx'].find(suffix => rawFilename.toLowerCase().endsWith(suffix)) || '';
    const boundedSourceFilename = rawFilename.length <= 256 || !sourceSuffix
        ? rawFilename.slice(0, 256)
        : `${rawFilename.slice(0, -sourceSuffix.length).slice(0, 256 - sourceSuffix.length)}${sourceSuffix}`;
    return {
        ...normalized,
        source_filename: boundedSourceFilename,
        provider: 'xunfei',
        account_scope: String(accountScope || config?.account_scope || 'xunfei-default').trim() || 'xunfei-default',
    };
}

function collectPersistedConfig() {
    return normalizePersistedConfig(collectConfig(false));
}


registerRendererModule("workflow.config", {
    collectConfig,
    buildWorkflowConfiguration,
    collectPersistedConfig,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
