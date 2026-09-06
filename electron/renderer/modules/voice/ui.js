/** Renderer module: voice.ui */
(function attachRendererFeature_voice_ui(root) {
    'use strict';

function voiceDisplayName(key) {
    return getVoiceEntry(key).name;
}

function getVoiceInitials(name) {
    const value = String(name || '?').trim();
    const ascii = value.match(/[A-Za-z0-9]/g);
    if (ascii?.length) return ascii.slice(0, 2).join('').toUpperCase();
    return value.slice(0, 1) || '?';
}

function renderVoiceAvatar(container, voice, large = false, eager = false) {
    if (!container) return;
    container.replaceChildren();
    container.classList.toggle('voice-avatar-large', large);
    container.classList.remove('has-image');
    const fallback = document.createElement('span');
    fallback.textContent = getVoiceInitials(voice?.name);
    container.appendChild(fallback);
    const sources = [...new Set([voice?.img_url, voice?.fallback_img_url].filter(Boolean))];
    if (!sources.length) return;
    const image = document.createElement('img');
    image.alt = '';
    const loadImmediately = large || eager;
    image.loading = loadImmediately ? 'eager' : 'lazy';
    image.decoding = 'async';
    image.hidden = loadImmediately;
    image.addEventListener('load', () => {
        fallback.hidden = true;
        image.hidden = false;
        container.classList.add('has-image');
    }, { once: true });
    image.addEventListener('error', () => {
        const fallbackSrc = image.dataset.fallbackSrc;
        if (fallbackSrc) {
            delete image.dataset.fallbackSrc;
            image.src = fallbackSrc;
            return;
        }
        image.remove();
        container.classList.remove('has-image');
    });
    container.appendChild(image);
    if (loadImmediately) {
        image.src = sources[0];
        if (sources[1]) image.dataset.fallbackSrc = sources[1];
    } else {
        image.dataset.src = sources[0];
        if (sources[1]) image.dataset.fallbackSrc = sources[1];
    }
}

function observeVoiceAvatars() {
    if (voiceAvatarObserver) {
        voiceAvatarObserver.disconnect();
        voiceAvatarObserver = null;
    }
    const grid = $('voice-browser-grid');
    if (!grid) return;
    const lazyImages = [...grid.querySelectorAll('img[data-src]')];
    if (!lazyImages.length) return;

    const activate = image => {
        const src = image.dataset.src;
        if (!src) return;
        image.hidden = true;
        image.loading = 'eager';
        image.src = src;
        delete image.dataset.src;
    };
    if (!('IntersectionObserver' in window)) {
        lazyImages.forEach(activate);
        return;
    }
    voiceAvatarObserver = new IntersectionObserver(entries => {
        entries.forEach(entry => {
            if (!entry.isIntersecting) return;
            activate(entry.target);
            voiceAvatarObserver?.unobserve(entry.target);
        });
    }, { root: grid, rootMargin: '120px 0px' });
    lazyImages.forEach(image => voiceAvatarObserver.observe(image));
}

function getRecentVoiceKeys() {
    try {
        const raw = JSON.parse(rendererStorage?.getItem(VOICE_RECENT_STORAGE_KEY) || '[]');
        return Array.isArray(raw)
            ? [...new Set(raw.map(key => canonicalVoiceKey(key)).filter(Boolean))].slice(0, 12)
            : [];
    } catch (_) {
        return [];
    }
}

function rememberVoiceUse(key) {
    const normalizedKey = canonicalVoiceKey(key);
    if (!normalizedKey) return;
    const recent = [normalizedKey, ...getRecentVoiceKeys().filter(item => item !== normalizedKey)].slice(0, 12);
    try {
        rendererStorage?.setItem(VOICE_RECENT_STORAGE_KEY, JSON.stringify(recent));
    } catch (_) {
        // localStorage 不可用时不影响当前音色选择。
    }
}

function setVoiceCatalog(entries, filters = [], aliases = {}) {
    voiceAliasMap = Object.fromEntries(
        Object.entries(aliases && typeof aliases === 'object' ? aliases : {})
            .slice(0, 4096)
            .map(([alias, target]) => [normalizeVoiceKey(alias), normalizeVoiceKey(target)])
            .filter(([alias, target]) => alias && target && alias !== target),
    );
    const normalized = Array.isArray(entries) ? entries.map(normalizeVoiceEntry) : [];
    const byKey = new Map();
    [...normalized, ...voiceCatalog].forEach(voice => {
        if (voice?.key && !byKey.has(voice.key)) byKey.set(voice.key, voice);
    });
    voiceCatalog = [...byKey.values()];
    const filterMap = new Map([['all', { key: 'all', label: '全部音色' }]]);
    if (Array.isArray(filters)) {
        filters.forEach(filter => {
            const key = String(filter?.key || '').trim();
            const label = String(filter?.label || '').trim();
            if (key && label && key !== 'all') filterMap.set(key, { key, label, count: filter.count });
        });
    }
    if (!filterMap.has('female')) filterMap.set('female', { key: 'female', label: '女声' });
    if (!filterMap.has('male')) filterMap.set('male', { key: 'male', label: '男声' });
    filterMap.set('recent', { key: 'recent', label: '最近使用' });
    const priorityFilters = ['英语', '多语种']
        .map(label => [...filterMap.values()].find(filter => filter.label === label))
        .filter(Boolean);
    const priorityKeys = new Set(priorityFilters.map(filter => filter.key));
    voiceFilterOptions = [
        filterMap.get('all'),
        ...priorityFilters,
        filterMap.get('recent'),
        ...[...filterMap.values()].filter(filter => (
            filter.key !== 'all'
            && filter.key !== 'recent'
            && !priorityKeys.has(filter.key)
        )),
    ].filter(Boolean);
}

function getVoiceFilterOptions() {
    return voiceFilterOptions.map(filter => ({ ...filter }));
}

function migrateVoiceSelections() {
    let changed = false;
    const migrate = value => {
        const normalized = canonicalVoiceKey(value);
        if (normalized !== normalizeVoiceKey(value)) changed = true;
        return normalized;
    };
    const female = migrate(selectedDefaultFemaleVoice);
    const male = migrate(selectedDefaultMaleVoice);
    if (female) selectedDefaultFemaleVoice = female;
    if (male) selectedDefaultMaleVoice = male;

    const migratedRoles = {};
    Object.entries(roleVoiceMap || {}).forEach(([role, key]) => {
        const normalizedRole = normalizeRoleKeyClient(role);
        const migratedKey = migrate(key);
        if (normalizedRole && migratedKey) migratedRoles[normalizedRole] = migratedKey;
    });
    if (JSON.stringify(migratedRoles) !== JSON.stringify(roleVoiceMap || {})) changed = true;
    roleVoiceMap = migratedRoles;

    try {
        const raw = JSON.parse(rendererStorage?.getItem(VOICE_RECENT_STORAGE_KEY) || '[]');
        const recent = Array.isArray(raw)
            ? [...new Set(raw.map(key => canonicalVoiceKey(key)).filter(Boolean))].slice(0, 12)
            : [];
        if (JSON.stringify(recent) !== JSON.stringify(raw)) {
            rendererStorage?.setItem(VOICE_RECENT_STORAGE_KEY, JSON.stringify(recent));
            changed = true;
        }
    } catch (_) {
        // localStorage 不可用时不影响当前音色迁移。
    }
    return changed;
}

function roleLooksLikeLabel(label) {
    const value = String(label || '').trim();
    return Boolean(value)
        && value.length <= 48
        && value.split(/\s+/).length <= 4
        && !/^\d/.test(value)
        && !value.includes('://')
        && !/[\\/.,!?。！？；;，,]/.test(value);
}

function inferRoleVoice(label) {
    if (normalizeRoleKeyClient(label) === QUESTION_STEM_ROLE_KEY) {
        return canonicalVoiceKey(QUESTION_STEM_VOICE_KEY);
    }
    const value = String(label || '').trim().toLocaleLowerCase('en-US');
    if (/^(mr|mr\.|sir|男|先生)\b/.test(value)) return selectedDefaultMaleVoice;
    return selectedDefaultFemaleVoice;
}

/**
 * 从后端已经解析完成的完整文档结果中提取角色名。
 *
 * 一个文档可能包含多个题型、多个录音稿条目；不能只看第一条 item。
 * 这里只识别录音稿行首的「角色名: 内容」形式，W/M 标记交给音频解析器，
 * 避免把普通正文中的冒号误当成可配置角色。新版 7 上规则会把一个角色
 * 单独拆成一个音频，因此解析器同时提供可信的 item.role 元数据。
 */
function extractParsedRoleLabels(parseResults) {
    const labels = [];
    const seen = new Set();
    if (!Array.isArray(parseResults)) return labels;

    parseResults.forEach(result => {
        if (!Array.isArray(result?.items)) return;
        result.items.forEach(item => {
            const explicitRoles = [];
            if (typeof item?.role === 'string' && item.role.trim()) {
                explicitRoles.push(item.role.trim());
            }
            if (Array.isArray(item?.roles)) {
                item.roles.forEach(role => {
                    if (typeof role === 'string' && role.trim()) explicitRoles.push(role.trim());
                });
            }
            explicitRoles.forEach(label => {
                if (!roleLooksLikeLabel(label)) return;
                const key = normalizeRoleKeyClient(label);
                if (!key || seen.has(key)) return;
                seen.add(key);
                labels.push({ key, label });
            });

            const lines = String(item?.text || '').split(/\r?\n/);
            const itemLabels = [];
            const itemSeen = new Set();
            lines.forEach(line => {
                const value = line.trim();
                if (!value || /^[WwMm]\s*[:：]/.test(value) || /^\([WwMm]\)/.test(value)) return;
                const match = /^([^:：\n]{1,60}?)\s*[:：]\s*(.*)$/.exec(value);
                const label = match?.[1]?.trim() || '';
                if (!match || !roleLooksLikeLabel(label)) return;
                const key = normalizeRoleKeyClient(label);
                if (!key || itemSeen.has(key)) return;
                itemSeen.add(key);
                itemLabels.push({ key, label });
            });
            // 没有可信 role 元数据时，至少出现两个不同的行首角色名，
            // 才把该 item 视为对话题；单独一行的普通说明不进入角色配置。
            if (itemLabels.length < 2) return;
            itemLabels.forEach(role => {
                if (seen.has(role.key)) return;
                seen.add(role.key);
                labels.push(role);
            });
        });
    });
    return labels;
}

function discoverVoiceRoles(parseResults = currentSession?.parse_results) {
    const roles = [
        { key: DEFAULT_FEMALE_ROLE_KEY, label: '默认女声', kind: 'default-female' },
        { key: DEFAULT_MALE_ROLE_KEY, label: '默认男声', kind: 'default-male' },
    ];
    const parsedRoles = extractParsedRoleLabels(parseResults);
    const currentRoleKeys = new Set(parsedRoles.map(role => role.key));
    const hasDocumentContext = Array.isArray(parseResults);

    // 角色映射属于当前文档；切换文档后不把上一份文档的角色配置继续提交。
    // 没有导入文档时先保留本地预设中的角色配置，避免初始化渲染把它们清掉。
    if (hasDocumentContext) {
        roleVoiceMap = Object.fromEntries(
            Object.entries(roleVoiceMap).filter(([key]) => currentRoleKeys.has(key)),
        );
    }
    parsedRoles.forEach(({ key, label }) => {
        if (!roleVoiceMap[key]) roleVoiceMap[key] = inferRoleVoice(label);
        roles.push({
            key,
            label,
            kind: key === QUESTION_STEM_ROLE_KEY ? 'question-stem' : 'role',
        });
    });
    voiceRoles = roles;
    const validParamKeys = new Set(roles.map(roleConfigKeyForRole));
    if (hasDocumentContext) {
        voiceParamConfigs = Object.fromEntries(
            Object.entries(voiceParamConfigs).filter(([key]) => validParamKeys.has(key)),
        );
    }
    roles.forEach(role => {
        const configKey = roleConfigKeyForRole(role);
        if (!voiceParamConfigs[configKey]) voiceParamConfigs[configKey] = createDefaultVoiceParams(configKey);
    });
    if (!roles.some(role => role.key === activeVoiceRole)) activeVoiceRole = roles[0].key;
    const roleCount = $('voice-role-count');
    if (roleCount) roleCount.textContent = parsedRoles.length > 0
        ? `${parsedRoles.length} 个角色`
        : '2 个默认角色';
    const roleNote = document.querySelector?.('.voice-role-note');
    if (roleNote) {
        roleNote.textContent = parsedRoles.some(role => role.key === QUESTION_STEM_ROLE_KEY)
            ? '题干音色默认使用晓燕（40 / 50 / 50），可在左侧角色中手动修改；其他无角色内容使用默认女声。'
            : '无角色标识的内容使用默认女声；单词和例句始终使用默认女声。';
    }
    return roles;
}

function resetTaskVoiceConfiguration() {
    stopVoicePreview();
    roleVoiceMap = {};
    voiceParamConfigs = {
        [DEFAULT_FEMALE_ROLE_KEY]: normalizeVoiceParams(
            voiceParamConfigs[DEFAULT_FEMALE_ROLE_KEY],
            DEFAULT_FEMALE_VOICE_PARAMS,
        ),
        [DEFAULT_MALE_ROLE_KEY]: normalizeVoiceParams(
            voiceParamConfigs[DEFAULT_MALE_ROLE_KEY],
            DEFAULT_MALE_VOICE_PARAMS,
        ),
    };
    activeVoiceRole = DEFAULT_FEMALE_ROLE_KEY;
    voiceRoles = [];
    setVoiceDetailCollapsed(false);
}

function activeVoiceKeyForRole(role = voiceRoles.find(item => item.key === activeVoiceRole)) {
    if (role?.kind === 'default-male') return selectedDefaultMaleVoice;
    if (role?.kind === 'default-female') return selectedDefaultFemaleVoice;
    const roleKey = role?.key || activeVoiceRole;
    if (normalizeRoleKeyClient(roleKey) === QUESTION_STEM_ROLE_KEY) {
        return roleVoiceMap[roleKey] || canonicalVoiceKey(QUESTION_STEM_VOICE_KEY);
    }
    return roleVoiceMap[roleKey] || selectedDefaultFemaleVoice;
}

function activeVoiceParams() {
    const role = voiceRoles.find(item => item.key === activeVoiceRole);
    const configKey = roleConfigKeyForRole(role || activeVoiceRole);
    if (!voiceParamConfigs[configKey]) voiceParamConfigs[configKey] = createDefaultVoiceParams(configKey);
    return voiceParamConfigs[configKey];
}

function renderRoleList() {
    const container = $('voice-role-list');
    if (!container) return;
    container.replaceChildren();
    voiceRoles.forEach(role => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `voice-role-item${role.key === activeVoiceRole ? ' is-active' : ''}${role.kind === 'default-male' ? ' is-male' : ''}${role.kind === 'question-stem' ? ' is-question-stem' : ''}`;
        button.dataset.roleKey = role.key;
        button.setAttribute('aria-pressed', role.key === activeVoiceRole ? 'true' : 'false');
        if (role.kind === 'question-stem') {
            button.title = '题干默认使用晓燕 · 语速 40 · 语调/音量 50，可手动修改';
        }

        const mark = document.createElement('span');
        mark.className = 'voice-role-mark';
        mark.textContent = role.kind === 'default-female'
            ? '女'
            : role.kind === 'default-male'
                ? '男'
                : role.kind === 'question-stem'
                    ? '题干'
                    : getVoiceInitials(role.label);
        const copy = document.createElement('span');
        copy.className = 'voice-role-copy';
        const name = document.createElement('strong');
        name.textContent = role.label;
        const voice = document.createElement('small');
        voice.textContent = voiceDisplayName(activeVoiceKeyForRole(role));
        copy.append(name, voice);
        button.append(mark, copy);
        container.appendChild(button);
    });
}

function voiceSearchText(voice) {
    return voice.search_text || [voice.name, voice.gender_label, ...(voice.language || []), ...(voice.tags || []), ...(voice.categories || [])]
        .join(' ').toLocaleLowerCase('zh-CN');
}

function voiceMatchesFilter(voice, filterKey) {
    if (!filterKey || filterKey === 'all') return true;
    if (filterKey === 'recent') return getRecentVoiceKeys().includes(voice.key);
    if (filterKey === 'female' || filterKey === 'male') return voice.gender === filterKey;
    const label = filterKey.startsWith('tag:') ? filterKey.slice(4) : filterKey;
    return [...(voice.categories || []), ...(voice.tags || []), ...(voice.language || [])].includes(label);
}

function voiceTags(voice, limit = 2) {
    const values = [voice.gender_label, ...(voice.tags || []), ...(voice.language || []), ...(voice.categories || [])];
    return [...new Set(values.filter(Boolean))].slice(0, limit);
}

function voiceHasPreview(voice) {
    return Boolean(String(voice?.audio_url || voice?.fallback_audio_url || '').trim());
}

function createVoicePreviewButton(voice) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'voice-entry-preview';
    button.dataset.voicePreviewKey = voice.key;
    button.dataset.audioName = `音色 ${voice.name}`;
    const hasPreview = voiceHasPreview(voice);
    button.disabled = !hasPreview;
    button.title = hasPreview ? `试听 ${voice.name}` : `${voice.name}暂无示例音频`;
    button.setAttribute('aria-label', hasPreview ? `试听 ${voice.name}` : `${voice.name}暂无示例音频`);
    button.innerHTML = '<svg class="icon-play" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg><svg class="icon-pause" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style="display:none"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
    button.setAttribute('aria-pressed', 'false');
    return button;
}

function renderVoiceFilters() {
    const container = $('voice-filter-row');
    if (!container) return;
    const overflow = $('voice-filter-overflow');
    const isPrimaryFilter = filter => filter.key === 'all'
        || filter.key === 'female'
        || filter.key === 'male'
        || filter.key === 'recent'
        || ['英语', '多语种'].includes(filter.label);
    const primaryFilters = voiceFilterOptions.filter(isPrimaryFilter);
    const secondaryFilters = voiceFilterOptions.filter(filter => !isPrimaryFilter(filter));
    const activeSecondary = secondaryFilters.some(filter => filter.key === activeVoiceFilter);
    const expanded = voiceFiltersExpanded || activeSecondary;
    const createFilterButton = filter => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `voice-filter-chip${filter.key === activeVoiceFilter ? ' is-active' : ''}`;
        button.dataset.voiceFilter = filter.key;
        button.setAttribute('aria-pressed', filter.key === activeVoiceFilter ? 'true' : 'false');
        button.textContent = filter.label;
        return button;
    };

    container.replaceChildren(...primaryFilters.map(createFilterButton));
    if (secondaryFilters.length > 0) {
        const moreButton = document.createElement('button');
        moreButton.type = 'button';
        moreButton.className = `voice-filter-chip voice-filter-more${expanded ? ' is-expanded' : ''}`;
        moreButton.dataset.voiceFilterMore = 'true';
        moreButton.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        moreButton.textContent = expanded ? '收起更多' : `更多筛选 · ${secondaryFilters.length}`;
        container.appendChild(moreButton);
    }

    if (overflow) {
        overflow.replaceChildren(...secondaryFilters.map(createFilterButton));
        overflow.hidden = !expanded || secondaryFilters.length === 0;
    }
}

function renderVoiceCards() {
    const grid = $('voice-browser-grid');
    const empty = $('voice-browser-empty');
    if (!grid || !empty) return;
    const query = String($('voice-search-input')?.value || '').trim().toLocaleLowerCase('zh-CN');
    const selectedKey = activeVoiceKeyForRole();
    const matches = voiceCatalog.filter(voice => voiceMatchesFilter(voice, activeVoiceFilter) && (!query || voiceSearchText(voice).includes(query)));
    if (voiceAvatarObserver) {
        voiceAvatarObserver.disconnect();
        voiceAvatarObserver = null;
    }
    const fragment = document.createDocumentFragment();
    matches.forEach((voice, index) => {
        const card = document.createElement('div');
        card.className = `voice-entry${voice.key === selectedKey ? ' is-selected' : ''}`;
        card.dataset.voiceKey = voice.key;
        card.setAttribute('role', 'listitem');
        card.setAttribute('aria-label', `${voice.name}，${voiceHasPreview(voice) ? '可试听' : '暂无示例音频'}`);
        const selectButton = document.createElement('button');
        selectButton.type = 'button';
        selectButton.className = 'voice-entry-select';
        selectButton.setAttribute('aria-pressed', voice.key === selectedKey ? 'true' : 'false');
        selectButton.setAttribute('aria-label', `选择音色 ${voice.name}`);
        const avatar = document.createElement('span');
        avatar.className = 'voice-avatar';
        // 首屏卡片直接加载，滚动到后续音色时再按可见区域加载，避免
        // 387 个音色同时请求头像而又保证当前列表不会只显示首字母。
        renderVoiceAvatar(avatar, voice, false, index < 20);
        const copy = document.createElement('span');
        copy.className = 'voice-entry-copy';
        const name = document.createElement('strong');
        name.textContent = voice.name;
        const tags = document.createElement('span');
        tags.className = 'voice-entry-tags';
        voiceTags(voice).forEach((tag, index) => {
            const tagEl = document.createElement('span');
            if (index === 0) tagEl.classList.add('is-gender');
            tagEl.textContent = tag;
            tags.appendChild(tagEl);
        });
        copy.append(name, tags);
        selectButton.append(avatar, copy);
        const actions = document.createElement('span');
        actions.className = 'voice-entry-actions';
        actions.appendChild(createVoicePreviewButton(voice));
        card.append(selectButton, actions);
        fragment.appendChild(card);
    });
    // 一次性挂载，避免每个音色卡片都触发布局计算。
    grid.replaceChildren(fragment);
    observeVoiceAvatars();
    empty.hidden = matches.length > 0;
}

function scheduleVoiceCardsRender() {
    if (voiceCardsRenderFrame !== null) return;
    const schedule = window.requestAnimationFrame
        ? callback => window.requestAnimationFrame(callback)
        : callback => window.setTimeout(callback, 0);
    voiceCardsRenderFrame = schedule(() => {
        voiceCardsRenderFrame = null;
        renderVoiceCards();
    });
}

function renderRecentVoiceList() {
    const list = $('voice-recent-list');
    const empty = $('voice-recent-empty');
    const count = $('voice-recent-count');
    if (!list || !empty || !count) return;
    const recent = getRecentVoiceKeys();
    count.textContent = String(recent.length);
    list.replaceChildren();
    recent.slice(0, 6).forEach(key => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'voice-recent-chip';
        button.dataset.recentVoiceKey = key;
        button.textContent = voiceDisplayName(key);
        list.appendChild(button);
    });
    empty.hidden = recent.length > 0;
}

function setVoiceParamInputs(params) {
    ['rate', 'pitch', 'volume'].forEach(param => {
        const value = clampParamValue(params?.[param]);
        const range = $(`voice-${param}`);
        const number = $(`voice-${param}-number`);
        if (range) range.value = String(value);
        if (number) number.value = String(value);
    });
}

function renderVoiceDetails() {
    const role = voiceRoles.find(item => item.key === activeVoiceRole);
    const voice = getResultVoiceEntry(activeVoiceKeyForRole(role));
    const detailRole = $('voice-detail-role');
    const detailName = $('voice-detail-name');
    const detailMeta = $('voice-detail-meta');
    if (detailRole) detailRole.textContent = role?.label || '当前角色';
    if (detailName) detailName.textContent = voice.name;
    if (detailMeta) detailMeta.textContent = [...(voice.language || []), voice.gender_label].filter(Boolean).join(' · ') || voice.gender_label;
    const detailDefaults = document.querySelector?.('.detail-section-heading span');
    if (detailDefaults) detailDefaults.textContent = role?.kind === 'question-stem'
        ? '0–100 · 题干默认 40 / 50 / 50'
        : '0–100 · 默认 50';
    renderVoiceAvatar($('voice-detail-avatar'), voice, true);
    setVoiceParamInputs(activeVoiceParams());
    const previewButton = $('voice-preview-btn');
    if (previewButton) {
        const hasPreview = voiceHasPreview(voice);
        previewButton.disabled = !hasPreview;
        previewButton.title = hasPreview ? `试听 ${voice.name}` : `${voice.name}暂无示例音频`;
        previewButton.setAttribute('aria-label', hasPreview ? `试听 ${voice.name}` : `${voice.name}暂无示例音频`);
        previewButton.dataset.audioName = `音色 ${voice.name}`;
        setVoicePreviewButtonState(
            voicePreviewAudio?._previewButton?.id === 'voice-preview-btn',
        );
    }
}

function renderVoiceWorkspace() {
    discoverVoiceRoles();
    renderRoleList();
    renderVoiceFilters();
    renderVoiceCards();
    renderVoiceDetails();
    renderRecentVoiceList();
    setVoiceDetailCollapsed(isVoiceDetailCollapsed);
}

function selectVoiceForActiveRole(key) {
    const normalizedKey = canonicalVoiceKey(key);
    if (!normalizedKey) return;
    stopVoicePreview();
    const role = voiceRoles.find(item => item.key === activeVoiceRole);
    if (role?.kind === 'default-male') selectedDefaultMaleVoice = normalizedKey;
    else if (role?.kind === 'default-female') selectedDefaultFemaleVoice = normalizedKey;
    else roleVoiceMap[activeVoiceRole] = normalizedKey;
    const configKey = roleConfigKeyForRole(role || activeVoiceRole);
    if (!voiceParamConfigs[configKey]) voiceParamConfigs[configKey] = createDefaultVoiceParams(configKey);
    rememberVoiceUse(normalizedKey);
    void queueVoiceAssetCache(normalizedKey);
    renderVoiceWorkspace();
    updateConfigSummary();
    rememberCurrentConfig();
}

function updateActiveVoiceParam(param, value) {
    const params = activeVoiceParams();
    params[param] = clampParamValue(value);
    setVoiceParamInputs(params);
    updateConfigSummary();
    rememberCurrentConfig();
}

function setResultVoicePreviewButtonState(button, isPlaying) {
    if (!button) return;
    button.classList.toggle('is-playing', Boolean(isPlaying));
    button.setAttribute('aria-pressed', isPlaying ? 'true' : 'false');
    updatePlayIcon(button, Boolean(isPlaying));
}

function stopVoicePreview() {
    const audio = voicePreviewAudio;
    if (audio) {
        const triggerButton = audio._previewButton;
        audio.pause();
        try { audio.currentTime = 0; } catch (_) { /* ignore */ }
        voicePreviewAudio = null;
        if (triggerButton?.id === 'voice-preview-btn') {
            setVoicePreviewButtonState(false);
        } else {
            setResultVoicePreviewButtonState(triggerButton, false);
        }
    }
    setVoicePreviewButtonState(false);
}

function setVoicePreviewButtonState(isPlaying) {
    const button = $('voice-preview-btn');
    if (!button) return;
    button.classList.toggle('is-playing', Boolean(isPlaying));
    button.setAttribute('aria-pressed', isPlaying ? 'true' : 'false');
    updatePlayIcon(button, Boolean(isPlaying));
}

function stopGeneratedAudioPlayback() {
    audioPlayRequestToken++;
    audioElements.forEach(audio => {
        audio._playRequestToken = 0;
        audio.pause();
        if (audio._playButton) {
            audio._playButton.classList.remove('is-buffering');
            updatePlayIcon(audio._playButton, false);
        }
    });
    currentPlayingAudio = null;
}

function playVoiceSample(voice, triggerButton) {
    const localSource = String(voice?.audio_url || '').trim();
    const fallbackSource = String(voice?.fallback_audio_url || '').trim();
    const source = localSource || fallbackSource;
    if (!source || !triggerButton) return;

    if (
        voicePreviewAudio
        && voicePreviewAudio._previewButton === triggerButton
        && voicePreviewAudio._previewKey === voice?.key
    ) {
        stopVoicePreview();
        return;
    }

    stopGeneratedAudioPlayback();
    stopVoicePreview();
    const audio = new Audio(source);
    audio.preload = 'auto';
    audio._previewButton = triggerButton;
    audio._previewKey = voice?.key || source;
    audio._previewSource = source;
    audio._previewFallbackTried = !localSource || !fallbackSource || localSource === fallbackSource;
    voicePreviewAudio = audio;
    let playbackAttempt = 0;

    const setPlaying = isPlaying => {
        if (triggerButton.id === 'voice-preview-btn') setVoicePreviewButtonState(isPlaying);
        else setResultVoicePreviewButtonState(triggerButton, isPlaying);
    };
    const finish = () => {
        if (voicePreviewAudio !== audio) return;
        stopVoicePreview();
    };
    const handleFailure = (attempt, attemptedSource) => {
        if (voicePreviewAudio !== audio) return;
        if (attempt !== playbackAttempt || attemptedSource !== audio.src) return;
        if (!audio._previewFallbackTried && fallbackSource && fallbackSource !== attemptedSource) {
            audio._previewFallbackTried = true;
            audio.src = fallbackSource;
            const resolvedFallbackSource = audio.src;
            playbackAttempt += 1;
            const fallbackAttempt = playbackAttempt;
            void audio.play().catch(() => handleFailure(fallbackAttempt, resolvedFallbackSource));
            return;
        }
        finish();
        showToast('当前音色试听暂时不可用', 'warning');
    };

    setPlaying(true);
    audio.addEventListener('ended', finish, { once: true });
    audio.addEventListener('error', () => handleFailure(playbackAttempt, audio.src));
    playbackAttempt += 1;
    const initialAttempt = playbackAttempt;
    const resolvedInitialSource = audio.src;
    void audio.play().catch(() => handleFailure(initialAttempt, resolvedInitialSource));
}

function setVoiceDetailCollapsed(collapsed) {
    isVoiceDetailCollapsed = Boolean(collapsed);
    const layout = document.querySelector('.voice-browser-layout');
    const panel = $('voice-detail-panel');
    const button = $('voice-detail-toggle');
    layout?.classList.toggle('is-detail-collapsed', isVoiceDetailCollapsed);
    panel?.classList.toggle('is-collapsed', isVoiceDetailCollapsed);
    if (button) {
        button.setAttribute('aria-expanded', isVoiceDetailCollapsed ? 'false' : 'true');
        button.setAttribute('aria-label', isVoiceDetailCollapsed ? '展开详细配置' : '收起详细配置');
        button.title = isVoiceDetailCollapsed ? '展开详细配置' : '收起详细配置';
        const label = button.querySelector('.voice-detail-toggle-label');
        if (label) {
            label.textContent = isVoiceDetailCollapsed
                ? '展开详细配置'
                : '收起详细配置';
        }
    }
}

function toggleVoicePreview() {
    const voice = getResultVoiceEntry(activeVoiceKeyForRole());
    if (!voiceHasPreview(voice)) return;
    playVoiceSample(voice, $('voice-preview-btn'));
}

function bindVoiceWorkspaceEvents() {
    $('voice-search-input')?.addEventListener('input', () => {
        stopVoicePreview();
        scheduleVoiceCardsRender();
    });
    const handleVoiceFilterClick = event => {
        const moreButton = event.target.closest('[data-voice-filter-more]');
        if (moreButton) {
            voiceFiltersExpanded = moreButton.getAttribute('aria-expanded') !== 'true';
            renderVoiceFilters();
            return;
        }
        const button = event.target.closest('[data-voice-filter]');
        if (!button) return;
        stopVoicePreview();
        activeVoiceFilter = button.dataset.voiceFilter || 'all';
        renderVoiceFilters();
        renderVoiceCards();
    };
    $('voice-filter-row')?.addEventListener('click', handleVoiceFilterClick);
    $('voice-filter-overflow')?.addEventListener('click', handleVoiceFilterClick);
    $('voice-role-list')?.addEventListener('click', event => {
        const button = event.target.closest('[data-role-key]');
        if (!button) return;
        stopVoicePreview();
        activeVoiceRole = button.dataset.roleKey || '__default_female__';
        renderVoiceWorkspace();
    });
    $('voice-browser-grid')?.addEventListener('click', event => {
        const previewButton = event.target.closest('[data-voice-preview-key]');
        if (previewButton) {
            const voice = getResultVoiceEntry(previewButton.dataset.voicePreviewKey);
            if (voiceHasPreview(voice)) playVoiceSample(voice, previewButton);
            return;
        }
        const button = event.target.closest('[data-voice-key]');
        if (button) selectVoiceForActiveRole(button.dataset.voiceKey);
    });
    $('voice-recent-list')?.addEventListener('click', event => {
        const button = event.target.closest('[data-recent-voice-key]');
        if (button) selectVoiceForActiveRole(button.dataset.recentVoiceKey);
    });
    $('voice-preview-btn')?.addEventListener('click', toggleVoicePreview);
    $('voice-detail-toggle')?.addEventListener('click', () => {
        setVoiceDetailCollapsed(!isVoiceDetailCollapsed);
    });
    ['rate', 'pitch', 'volume'].forEach(param => {
        const range = $(`voice-${param}`);
        const number = $(`voice-${param}-number`);
        range?.addEventListener('input', () => updateActiveVoiceParam(param, range.value));
        number?.addEventListener('input', () => updateActiveVoiceParam(param, number.value));
        number?.addEventListener('blur', () => updateActiveVoiceParam(param, number.value));
    });
    $$('[data-voice-param-reset]').forEach(button => {
        button.addEventListener('click', () => {
            const role = voiceRoles.find(item => item.key === activeVoiceRole);
            const defaults = createDefaultVoiceParams(roleConfigKeyForRole(role || activeVoiceRole));
            updateActiveVoiceParam(button.dataset.voiceParamReset, defaults[button.dataset.voiceParamReset] ?? 50);
        });
    });
}

function setSelectValue(selectEl, value, defaultValue) {
    const str = String(value ?? defaultValue);
    // 精确匹配
    for (const opt of selectEl.options) {
        if (opt.value === str) {
            selectEl.value = str;
            window.WordTTSUI?.syncSelect(selectEl);
            return;
        }
    }
    // 数值匹配（处理 1.0 → "1" vs "1.0" 等情况）
    const num = parseFloat(str);
    if (!isNaN(num)) {
        for (const opt of selectEl.options) {
            if (parseFloat(opt.value) === num) {
                selectEl.value = opt.value;
                window.WordTTSUI?.syncSelect(selectEl);
                return;
            }
        }
    }
    window.WordTTSUI?.syncSelect(selectEl);
}

/**
 * 将配置应用到 Step 2 表单。
 */
function applyConfigToForm(config, { includeRoles = true } = {}) {
    if (!config) return;
    stopVoicePreview();
    clientConfigInitialized = true;
    const normalized = normalizeClientConfig(config);
    const existingTaskRoleVoiceMap = currentSession ? { ...roleVoiceMap } : {};
    const existingTaskRoleConfigs = currentSession
        ? Object.fromEntries(
            Object.entries(voiceParamConfigs).filter(([key]) => (
                key !== DEFAULT_FEMALE_ROLE_KEY && key !== DEFAULT_MALE_ROLE_KEY
            )),
        )
        : {};
    selectedDefaultFemaleVoice = normalized.default_female_voice;
    selectedDefaultMaleVoice = normalized.default_male_voice;
    updateGenerationModeUI(normalized.generation_mode);
    const defaultRoleConfigs = {
        [DEFAULT_FEMALE_ROLE_KEY]: normalizeVoiceParams(
            normalized.role_configs?.[DEFAULT_FEMALE_ROLE_KEY],
            DEFAULT_FEMALE_VOICE_PARAMS,
        ),
        [DEFAULT_MALE_ROLE_KEY]: normalizeVoiceParams(
            normalized.role_configs?.[DEFAULT_MALE_ROLE_KEY],
            DEFAULT_MALE_VOICE_PARAMS,
        ),
    };
    if (includeRoles) {
        voiceParamConfigs = Object.fromEntries(
            Object.entries(normalized.role_configs || {}).map(([key, value]) => [key, { ...value }]),
        );
        roleVoiceMap = { ...(normalized.role_voices || {}) };
    } else {
        // 预设/页面恢复只覆盖默认男女声；当前文档角色始终由用户自己维护。
        voiceParamConfigs = { ...existingTaskRoleConfigs, ...defaultRoleConfigs };
        roleVoiceMap = existingTaskRoleVoiceMap;
    }
    voiceParamConfigs[DEFAULT_FEMALE_ROLE_KEY] ||= createDefaultVoiceParams(DEFAULT_FEMALE_ROLE_KEY);
    voiceParamConfigs[DEFAULT_MALE_ROLE_KEY] ||= createDefaultVoiceParams(DEFAULT_MALE_ROLE_KEY);
    renderVoiceWorkspace();
    setSelectValue($('format'), normalized.format, 'mp3');
    setSelectValue($('quality'), normalized.quality, '128 kbps（标准）');
    $('preview').checked = normalized.preview;
    enforceOutputCompatibility();
    rememberCurrentConfig();
}

/**
 * 刷新所有预设 UI。
 */
function refreshPresetUI() {
    renderStep2PresetSelect();
}

/**
 * 保存当前表单配置为预设。
 */
async function handleSavePreset() {
    const config = collectPersistedConfig();
    const name = await showPromptDialog('保存配置', '请输入配置名称：', `配置 ${new Date().toLocaleDateString('zh-CN')}`);
    if (!name || !name.trim()) return;

    const presets = loadPresets();
    const preset = {
        id: `preset_${Date.now()}`,
        name: name.trim(),
        config: config,
        created_at: Date.now(),
    };
    presets.push(preset);
    if (!savePresets(presets)) return;
    refreshPresetUI();

    // 选中新保存的预设
    const select = $('preset-select');
    if (select) {
        select.value = preset.id;
        window.WordTTSUI?.syncSelect(select);
    }

    showToast(`已保存配置「${preset.name}」`);
}

/**
 * 应用选中的预设到表单。
 */
function handleApplyPreset() {
    const select = $('preset-select');
    const presetId = select ? select.value : '';
    if (!presetId) {
        showToast('请先选择一个配置');
        return;
    }
    const presets = loadPresets();
    const preset = presets.find(p => p.id === presetId);
    if (!preset) {
        showToast('配置不存在');
        return;
    }
    applyConfigToForm(preset.config, { includeRoles: false });
    showToast(`已应用配置「${preset.name}」`);
}

/**
 * 删除选中的预设。
 */
async function handleDeletePreset() {
    const select = $('preset-select');
    const presetId = select ? select.value : '';
    if (!presetId) {
        showToast('请先选择一个配置');
        return;
    }
    const presets = loadPresets();
    const preset = presets.find(p => p.id === presetId);
    if (!preset) {
        showToast('配置不存在，可能已被删除');
        return;
    }

    const confirmed = await showConfirmDialog({
        kicker: '配置管理',
        title: '删除这个配置？',
        message: `「${preset.name}」将从已保存配置中移除。`,
        detail: '此操作不会影响已经生成的音频，但删除后无法恢复。',
        tone: 'danger',
        confirmLabel: '删除配置',
    });
    if (!confirmed) return;

    const filtered = presets.filter(p => p.id !== presetId);
    if (!savePresets(filtered)) return;
    refreshPresetUI();
    showToast(`已删除配置「${preset.name}」`);
}

/**
 * 启动生成。useDefaults=true 使用默认配置；presetConfig 不为空时直接使用该配置。
 */

registerRendererModule("voice.ui", {
    voiceDisplayName,
    getVoiceInitials,
    renderVoiceAvatar,
    observeVoiceAvatars,
    getRecentVoiceKeys,
    rememberVoiceUse,
    setVoiceCatalog,
    getVoiceFilterOptions,
    migrateVoiceSelections,
    roleLooksLikeLabel,
    inferRoleVoice,
    extractParsedRoleLabels,
    discoverVoiceRoles,
    resetTaskVoiceConfiguration,
    activeVoiceKeyForRole,
    activeVoiceParams,
    renderRoleList,
    voiceSearchText,
    voiceMatchesFilter,
    voiceTags,
    voiceHasPreview,
    createVoicePreviewButton,
    renderVoiceFilters,
    renderVoiceCards,
    scheduleVoiceCardsRender,
    renderRecentVoiceList,
    setVoiceParamInputs,
    renderVoiceDetails,
    renderVoiceWorkspace,
    selectVoiceForActiveRole,
    updateActiveVoiceParam,
    setResultVoicePreviewButtonState,
    stopVoicePreview,
    setVoicePreviewButtonState,
    stopGeneratedAudioPlayback,
    playVoiceSample,
    setVoiceDetailCollapsed,
    toggleVoicePreview,
    bindVoiceWorkspaceEvents,
    setSelectValue,
    applyConfigToForm,
    refreshPresetUI,
    handleSavePreset,
    handleApplyPreset,
    handleDeletePreset,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
