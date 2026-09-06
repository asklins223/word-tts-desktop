/** Renderer module: voice.configDomain */
(function attachRendererFeature_voice_configDomain(root) {
    'use strict';

function presetSummary(config) {
    if (!config) return '配置数据缺失';
    const normalized = normalizePersistedConfig(config);
    const female = normalized.role_configs?.[DEFAULT_FEMALE_ROLE_KEY] || DEFAULT_VOICE_PARAMS;
    const male = normalized.role_configs?.[DEFAULT_MALE_ROLE_KEY] || DEFAULT_VOICE_PARAMS;
    const parts = [];
    parts.push(`女 ${female.rate}/${female.pitch}/${female.volume}`);
    parts.push(`男 ${male.rate}/${male.pitch}/${male.volume}`);
    parts.push((normalized.format || 'mp3').toUpperCase());
    return parts.join(' · ');
}

/**
 * 渲染 Step 2 的预设下拉框。
 */
function renderStep2PresetSelect() {
    const select = $('preset-select');
    if (!select) return;
    const presets = loadPresets();

    // 保留第一个 option
    select.innerHTML = '<option value="">选择已保存的配置</option>';
    presets.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = `${p.name} (${presetSummary(p.config)})`;
        select.appendChild(opt);
    });
    window.WordTTSUI?.syncSelect(select);
}

/** 讯飞参数滑块（0-100）：range 与 number 双向联动。 */
function clampParamValue(v) {
    if (v === null || v === undefined || v === '') return 50;
    const n = Math.round(Number(v));
    if (!Number.isFinite(n)) return 50;
    return Math.min(100, Math.max(0, n));
}

function normalizeVoiceKey(value, fallback = '') {
    const key = String(value ?? '').trim();
    return key ? key.slice(0, 160) : fallback;
}

function canonicalVoiceKey(value) {
    let key = normalizeVoiceKey(value);
    const legacyDefaultKeys = {
        amanda: 'amanda',
        george: 'george',
        '英语-amanda': 'amanda',
        '英语-george': 'george',
    };
    key = legacyDefaultKeys[key.toLocaleLowerCase('zh-CN')] || key;
    const seen = new Set();
    for (let index = 0; key && index < 8 && !seen.has(key); index += 1) {
        seen.add(key);
        const next = normalizeVoiceKey(voiceAliasMap[key]);
        if (!next || next === key) break;
        key = next;
    }
    return key;
}

function normalizeRoleKeyClient(value) {
    return String(value ?? '').trim().replace(/\s+/g, ' ').toLocaleLowerCase('zh-CN').slice(0, 80);
}

function createDefaultVoiceParams(roleKey = null) {
    if (
        roleKey === QUESTION_STEM_ROLE_CONFIG_KEY
        || normalizeRoleKeyClient(roleKey) === QUESTION_STEM_ROLE_KEY
    ) {
        return { ...QUESTION_STEM_VOICE_PARAMS };
    }
    if (roleKey === DEFAULT_MALE_ROLE_KEY) return { ...DEFAULT_MALE_VOICE_PARAMS };
    if (roleKey === DEFAULT_FEMALE_ROLE_KEY) return { ...DEFAULT_FEMALE_VOICE_PARAMS };
    return { ...DEFAULT_VOICE_PARAMS };
}

function normalizeRoleConfigKeyClient(value) {
    let raw = String(value ?? '').trim();
    if (raw === DEFAULT_FEMALE_ROLE_KEY || raw === DEFAULT_MALE_ROLE_KEY) return raw;
    if (raw.startsWith(ROLE_CONFIG_PREFIX)) raw = raw.slice(ROLE_CONFIG_PREFIX.length);
    const roleKey = normalizeRoleKeyClient(raw);
    return roleKey ? `${ROLE_CONFIG_PREFIX}${roleKey}` : '';
}

function roleConfigKeyForRole(role) {
    if (role?.kind === 'default-female' || role === DEFAULT_FEMALE_ROLE_KEY) {
        return DEFAULT_FEMALE_ROLE_KEY;
    }
    if (role?.kind === 'default-male' || role === DEFAULT_MALE_ROLE_KEY) {
        return DEFAULT_MALE_ROLE_KEY;
    }
    const raw = typeof role === 'string' ? role : (role?.key || role?.label || '');
    const normalized = normalizeRoleKeyClient(raw);
    return normalized ? `${ROLE_CONFIG_PREFIX}${normalized}` : DEFAULT_FEMALE_ROLE_KEY;
}

function normalizeVoiceParams(raw, fallback = { rate: 50, volume: 50, pitch: 50 }) {
    const values = raw && typeof raw === 'object' ? raw : {};
    return {
        rate: clampParamValue(values.rate ?? fallback.rate),
        volume: clampParamValue(values.volume ?? fallback.volume),
        pitch: clampParamValue(values.pitch ?? fallback.pitch),
    };
}

function normalizeGenerationMode(value) {
    return value === GENERATION_MODE_SINGLE
        ? GENERATION_MODE_SINGLE
        : GENERATION_MODE_COMPOSITE;
}

function generationModeLabel(value) {
    return GENERATION_MODE_LABELS[normalizeGenerationMode(value)] || GENERATION_MODE_LABELS[DEFAULT_GENERATION_MODE];
}

function generationModeDescription(value) {
    return normalizeGenerationMode(value) === GENERATION_MODE_SINGLE
        ? '保留原有单条生成流程，逐段生成后直接整理。'
        : '一次提交全部文本，按人工停顿安全切回单段音频。';
}

function selectedGenerationMode() {
    return normalizeGenerationMode(document.querySelector('input[name="generation-mode"]:checked')?.value);
}

function updateGenerationModeUI(mode) {
    const normalizedMode = normalizeGenerationMode(mode);
    const label = generationModeLabel(normalizedMode);
    const composite = normalizedMode === GENERATION_MODE_COMPOSITE;
    const summary = $('summary-mode');
    const strategy = $('generation-strategy');
    const consoleTitle = $('generation-console-title');
    const consoleDescription = $('generation-console-description');
    const progressDetail = $('progress-mode-detail');
    const stageDetail = $('generation-synthesis-stage-detail');
    if (summary) summary.textContent = label;
    if (strategy) strategy.innerHTML = `<i></i>${label}`;
    if (consoleTitle) consoleTitle.textContent = composite ? '合并生成音频' : '逐条生成音频';
    if (consoleDescription) consoleDescription.textContent = generationModeDescription(normalizedMode);
    if (progressDetail) progressDetail.textContent = composite
        ? '合并生成后切割 · 按停顿恢复单段'
        : '单条生成 · 逐段整理文件';
    if (stageDetail) stageDetail.textContent = composite ? '按停顿切割' : '逐条提交';
    document.querySelectorAll('input[name="generation-mode"]').forEach(input => {
        const selected = input.value === normalizedMode;
        input.checked = selected;
        input.closest('.generation-mode-option')?.classList.toggle('is-selected', selected);
    });
}

function normalizeClientConfig(config = {}) {
    const raw = config && typeof config === 'object' ? config : {};
    const formats = ['mp3'];
    const qualities = [
        '48 kbps（低）',
        '128 kbps（标准）',
        '192 kbps（高）',
        '320 kbps（极高）',
    ];
    const baseParams = normalizeVoiceParams(raw, DEFAULT_VOICE_PARAMS);
    const defaultFemaleVoice = canonicalVoiceKey(
        raw.default_female_voice || currentConfig?.default_female_voice,
    ) || 'amanda';
    const defaultMaleVoice = canonicalVoiceKey(
        raw.default_male_voice || currentConfig?.default_male_voice,
    ) || 'george';
    const normalizedVoiceConfigs = {};
    const legacyVoiceConfigValues = {};
    const rawVoiceConfigs = raw.voice_configs && typeof raw.voice_configs === 'object'
        ? raw.voice_configs
        : {};
    Object.entries(rawVoiceConfigs).slice(0, 512).forEach(([key, value]) => {
        const normalizedKey = canonicalVoiceKey(key);
        if (normalizedKey) {
            legacyVoiceConfigValues[normalizedKey] = value;
            let fallback = baseParams;
            if (normalizedKey === defaultMaleVoice || normalizedKey === 'george') fallback = DEFAULT_MALE_VOICE_PARAMS;
            else if (normalizedKey === defaultFemaleVoice || normalizedKey === 'amanda') fallback = DEFAULT_FEMALE_VOICE_PARAMS;
            normalizedVoiceConfigs[normalizedKey] = normalizeVoiceParams(value, fallback);
        }
    });
    if (!normalizedVoiceConfigs[defaultFemaleVoice]) normalizedVoiceConfigs[defaultFemaleVoice] = { ...DEFAULT_FEMALE_VOICE_PARAMS };
    if (!normalizedVoiceConfigs[defaultMaleVoice]) normalizedVoiceConfigs[defaultMaleVoice] = { ...DEFAULT_MALE_VOICE_PARAMS };

    const normalizedRoleConfigs = {};
    const rawRoleConfigs = raw.role_configs && typeof raw.role_configs === 'object'
        ? raw.role_configs
        : {};
    Object.entries(rawRoleConfigs).slice(0, 512).forEach(([key, value]) => {
        const normalizedKey = normalizeRoleConfigKeyClient(key);
        if (normalizedKey) {
            let fallback = baseParams;
            if (normalizedKey === DEFAULT_MALE_ROLE_KEY) fallback = DEFAULT_MALE_VOICE_PARAMS;
            else if (normalizedKey === DEFAULT_FEMALE_ROLE_KEY) fallback = DEFAULT_FEMALE_VOICE_PARAMS;
            else if (normalizedKey === QUESTION_STEM_ROLE_CONFIG_KEY) fallback = QUESTION_STEM_VOICE_PARAMS;
            normalizedRoleConfigs[normalizedKey] = normalizeVoiceParams(value, fallback);
        }
    });
    // 旧版配置按音色保存参数。首次升级时将旧值复制到两个默认槽位，
    // 之后槽位各自维护，不再因为选用了同一音色而互相覆盖。
    const legacyParamsForRole = (voiceKey, fallback) => normalizeVoiceParams(
        defaultFemaleVoice === defaultMaleVoice
            ? legacyVoiceConfigValues[voiceKey]
            : normalizedVoiceConfigs[voiceKey],
        fallback,
    );
    if (!normalizedRoleConfigs[DEFAULT_FEMALE_ROLE_KEY]) {
        normalizedRoleConfigs[DEFAULT_FEMALE_ROLE_KEY] = normalizeVoiceParams(
            legacyParamsForRole(defaultFemaleVoice, DEFAULT_FEMALE_VOICE_PARAMS),
            DEFAULT_FEMALE_VOICE_PARAMS,
        );
    }
    if (!normalizedRoleConfigs[DEFAULT_MALE_ROLE_KEY]) {
        normalizedRoleConfigs[DEFAULT_MALE_ROLE_KEY] = normalizeVoiceParams(
            legacyParamsForRole(defaultMaleVoice, DEFAULT_MALE_VOICE_PARAMS),
            DEFAULT_MALE_VOICE_PARAMS,
        );
    }

    const normalizedRoleVoices = {};
    const rawRoleVoices = raw.role_voices && typeof raw.role_voices === 'object'
        ? raw.role_voices
        : {};
    Object.entries(rawRoleVoices).slice(0, 128).forEach(([role, key]) => {
        const roleKey = normalizeRoleKeyClient(role);
        const voiceKey = canonicalVoiceKey(key);
        if (roleKey && voiceKey) normalizedRoleVoices[roleKey] = voiceKey;
    });

    return {
        generation_mode: normalizeGenerationMode(raw.generation_mode ?? DEFAULT_GENERATION_MODE),
        rate: baseParams.rate,
        volume: baseParams.volume,
        pitch: baseParams.pitch,
        format: formats.includes(raw.format) ? raw.format : 'mp3',
        quality: qualities.includes(raw.quality) ? raw.quality : '128 kbps（标准）',
        preview: Boolean(raw.preview),
        default_female_voice: defaultFemaleVoice,
        default_male_voice: defaultMaleVoice,
        voice_configs: normalizedVoiceConfigs,
        role_configs: normalizedRoleConfigs,
        role_voices: normalizedRoleVoices,
    };
}

/**
 * 长期配置只保存默认男女声和它们各自的三项参数。
 * 文档角色属于当前任务，不能因为预设或页面恢复而跨文档复用。
 */
function normalizePersistedConfig(config = {}) {
    const normalized = normalizeClientConfig(config);
    const fallback = normalizeVoiceParams(normalized, DEFAULT_VOICE_PARAMS);
    const femaleParams = normalizeVoiceParams(
        normalized.role_configs?.[DEFAULT_FEMALE_ROLE_KEY]
            || normalized.voice_configs?.[normalized.default_female_voice]
            || normalized,
        fallback,
    );
    const maleParams = normalizeVoiceParams(
        normalized.role_configs?.[DEFAULT_MALE_ROLE_KEY]
            || normalized.voice_configs?.[normalized.default_male_voice]
            || normalized,
        fallback,
    );
    return {
        generation_mode: normalized.generation_mode,
        rate: femaleParams.rate,
        volume: femaleParams.volume,
        pitch: femaleParams.pitch,
        format: 'mp3',
        quality: normalized.quality,
        preview: normalized.preview,
        default_female_voice: normalized.default_female_voice,
        default_male_voice: normalized.default_male_voice,
        role_configs: {
            [DEFAULT_FEMALE_ROLE_KEY]: femaleParams,
            [DEFAULT_MALE_ROLE_KEY]: maleParams,
        },
    };
}

function normalizeVoiceEntry(raw) {
    const item = raw && typeof raw === 'object' ? raw : {};
    const key = normalizeVoiceKey(item.key || item.voice_key || item.id);
    const name = String(item.name || item.speakerName || item.speaker_name || key || '未命名音色').trim();
    const gender = String(item.gender || '').toLowerCase();
    const genderLabel = item.gender_label || (gender === 'female' ? '女声' : gender === 'male' ? '男声' : '音色');
    const toList = value => Array.isArray(value)
        ? value.map(entry => String(entry || '').trim()).filter(Boolean)
        : String(value || '').split(/[、,，|/]/).map(entry => entry.trim()).filter(Boolean);
    const language = toList(item.language || item.languages);
    const tags = toList(item.tags);
    const categories = [...new Set([...toList(item.categories), ...language, ...tags, genderLabel])].slice(0, 24);
    return {
        ...item,
        key: key || `name:${name.toLocaleLowerCase('zh-CN')}`,
        name,
        gender: gender || 'unknown',
        gender_label: genderLabel,
        language,
        tags,
        categories,
        img_url: String(item.img_url || item.imgUrl || '').trim(),
        audio_url: String(item.audio_url || item.audioUrl || '').trim(),
        search_text: [name, genderLabel, ...language, ...tags, ...categories]
            .join(' ').toLocaleLowerCase('zh-CN'),
    };
}

function getVoiceEntry(key) {
    const rawKey = String(key ?? '').trim();
    const normalizedKey = canonicalVoiceKey(rawKey);
    const normalizedName = rawKey.toLocaleLowerCase('zh-CN');
    return voiceCatalog.find(voice => (
        voice.key === normalizedKey
        || String(voice.name || '').trim().toLocaleLowerCase('zh-CN') === normalizedName
    ))
        || normalizeVoiceEntry({ key: normalizedKey, name: normalizedKey || '未选择音色' });
}


registerRendererModule("voice.configDomain", {
    presetSummary,
    renderStep2PresetSelect,
    clampParamValue,
    normalizeVoiceKey,
    canonicalVoiceKey,
    normalizeRoleKeyClient,
    createDefaultVoiceParams,
    normalizeRoleConfigKeyClient,
    roleConfigKeyForRole,
    normalizeVoiceParams,
    normalizeGenerationMode,
    generationModeLabel,
    generationModeDescription,
    selectedGenerationMode,
    updateGenerationModeUI,
    normalizeClientConfig,
    normalizePersistedConfig,
    normalizeVoiceEntry,
    getVoiceEntry,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
