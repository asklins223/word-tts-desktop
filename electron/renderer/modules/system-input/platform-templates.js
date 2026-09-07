/** Renderer module: systemInput.platformTemplates */
(function attachRendererFeature_systemInput_platformTemplates(root) {
    'use strict';

const SYSTEM_INPUT_LEGACY_PLATFORM_TEMPLATE_KEY = '__current_platform_template__';
const SYSTEM_INPUT_PLATFORM_TEMPLATE_SCOPE_FIELDS = Object.freeze({
    question: Object.freeze(['provinceId', 'cityId']),
    paper: Object.freeze(['provinceId', 'cityId', 'stageId', 'gradeId']),
});
const SYSTEM_INPUT_PLATFORM_TEMPLATE_RECORD_SCOPE_FIELDS = Object.freeze({
    provinceId: 'province',
    cityId: 'city',
    stageId: 'stage',
    gradeId: 'grade',
});
let systemInputPlatformTemplateCatalogIndex = null;

function systemInputPlatformTemplateKind(configuration = {}) {
    const category = systemInputDisplayValue(
        configuration.paperCategory ?? configuration.paper_category ?? $('system-input-paper-category')?.value,
    );
    return systemInputNormalizeLabel(category) === systemInputNormalizeLabel('听说考试') ? 'paper' : 'question';
}

function systemInputPlatformTemplateChoiceKeys(value) {
    const object = value && typeof value === 'object' && !Array.isArray(value) ? value : null;
    const identifier = object ? (object.id ?? object.value) : value;
    const keys = [];
    if (systemInputCascadeValuePresent(identifier)) keys.push(`id:${String(identifier)}`);
    const label = systemInputNormalizeLabel(systemInputDisplayValue(value));
    if (label) keys.push(`label:${label}`);
    return [...new Set(keys)];
}

function systemInputPlatformTemplateChoiceCacheKey(value) {
    const object = value && typeof value === 'object' && !Array.isArray(value) ? value : null;
    const identifier = object ? (object.id ?? object.value) : value;
    if (systemInputCascadeValuePresent(identifier)) return `id:${String(identifier)}`;
    return `label:${systemInputNormalizeLabel(systemInputDisplayValue(value))}`;
}

function systemInputPlatformTemplateReferenceCacheKey(value) {
    const reference = systemInputPlatformTemplateReference(value);
    if (!reference) return '';
    return [
        `key:${reference.platform_template_key || ''}`,
        `id:${systemInputNormalizeLabel(reference.platform_template_id || '')}`,
        `version:${systemInputNormalizeLabel(reference.platform_template_version || '')}`,
        `name:${systemInputNormalizeLabel(reference.name || '')}`,
    ].join('\u0001');
}

function resetSystemInputPlatformTemplateCatalogIndex() {
    systemInputPlatformTemplateCatalogIndex = null;
}

function systemInputPlatformTemplateCatalogIndexForCurrentSource() {
    const catalog = systemInputPlatformTemplateCatalog && typeof systemInputPlatformTemplateCatalog === 'object'
        ? systemInputPlatformTemplateCatalog
        : null;
    const source = catalog ? catalog.records : systemInputPlatformTemplates;
    const owner = catalog || systemInputPlatformTemplates;
    if (systemInputPlatformTemplateCatalogIndex
        && systemInputPlatformTemplateCatalogIndex.owner === owner
        && systemInputPlatformTemplateCatalogIndex.source === source) {
        return systemInputPlatformTemplateCatalogIndex;
    }

    const records = Array.isArray(source)
        ? source.filter(record => record && typeof record === 'object')
        : [];
    const byKind = Object.fromEntries(Object.entries(SYSTEM_INPUT_PLATFORM_TEMPLATE_SCOPE_FIELDS).map(([kind, fields]) => [
        kind,
        {
            // Keep the source array's realm/prototype. Renderer tests pass
            // fixture arrays across a VM boundary, and browser callers still
            // get the native array implementation of their source.
            records: records.slice(0, 0),
            fields: Object.fromEntries(fields.map(field => [field, new Map()])),
        },
    ]));
    const byKey = new Map();
    records.forEach(record => {
        const templateKey = String(record.platform_template_key || '').trim();
        if (templateKey && !byKey.has(templateKey)) byKey.set(templateKey, record);
        if (record.enabled === false) return;
        const declaredKind = String(record.template_kind || '').trim();
        const kinds = declaredKind ? [declaredKind] : Object.keys(byKind);
        kinds.forEach(kind => {
            const kindIndex = byKind[kind];
            if (!kindIndex) return;
            kindIndex.records.push(record);
            SYSTEM_INPUT_PLATFORM_TEMPLATE_SCOPE_FIELDS[kind].forEach(field => {
                const choice = systemInputPlatformTemplateRecordChoice(
                    record,
                    SYSTEM_INPUT_PLATFORM_TEMPLATE_RECORD_SCOPE_FIELDS[field],
                );
                systemInputPlatformTemplateChoiceKeys(choice).forEach(key => {
                    const bucket = kindIndex.fields[field].get(key) || [];
                    bucket.push(record);
                    kindIndex.fields[field].set(key, bucket);
                });
            });
        });
    });
    systemInputPlatformTemplateCatalogIndex = {
        owner,
        source,
        records,
        byKey,
        byKind,
        candidates: new Map(),
        matches: new Map(),
    };
    return systemInputPlatformTemplateCatalogIndex;
}

function systemInputPlatformTemplateIndexedCandidates(index, values, kind, required) {
    const kindIndex = index.byKind[kind];
    if (!kindIndex) return [];
    let pool = kindIndex.records;
    let poolSize = pool.length;

    // Use the narrowest single-field bucket as the candidate pool, then keep
    // the original matcher as the final authority. This preserves the old
    // ID/label compatibility rules while avoiding a full catalogue scan for
    // every row and every render pass.
    required.forEach(field => {
        if (!pool) return;
        const buckets = new Set();
        systemInputPlatformTemplateChoiceKeys(values[field]).forEach(key => {
            (kindIndex.fields[field].get(key) || []).forEach(record => buckets.add(record));
        });
        const candidates = pool.slice(0, 0);
        buckets.forEach(record => candidates.push(record));
        if (!candidates.length) {
            pool = [];
            poolSize = 0;
            return;
        }
        if (candidates.length < poolSize) {
            pool = candidates;
            poolSize = candidates.length;
        }
    });
    return pool.filter(record => systemInputPlatformTemplateRecordMatchesScope(record, values, kind));
}

function systemInputPlatformTemplateScopeValues(configuration = null) {
    const source = configuration && typeof configuration === 'object' ? configuration : {};
    const read = (camel, snake, fieldId) => source[camel] ?? source[snake] ?? $(fieldId)?.value ?? '';
    return {
        paperCategory: read('paperCategory', 'paper_category', 'system-input-paper-category'),
        provinceId: read('provinceId', 'province_id', 'system-input-province'),
        cityId: read('cityId', 'city_id', 'system-input-city'),
        stageId: read('stageId', 'stage_id', 'system-input-stage'),
        gradeId: read('gradeId', 'grade_id', 'system-input-grade'),
        platformTemplateName: source.platformTemplateName ?? source.platform_template_name ?? '',
        platformTemplateId: source.platformTemplateId ?? source.platform_template_id ?? '',
        platformTemplateVersion: source.platformTemplateVersion ?? source.platform_template_version ?? '',
    };
}

function systemInputPlatformTemplateCatalogRecords() {
    return systemInputPlatformTemplateCatalogIndexForCurrentSource().records;
}

function systemInputPlatformTemplateRecordChoice(record, field) {
    if (!record || typeof record !== 'object') return null;
    const value = record[field];
    if (value && typeof value === 'object' && !Array.isArray(value)) {
        return { id: value.id ?? value.value ?? '', name: value.name ?? value.label ?? value.text ?? '' };
    }
    const text = systemInputDisplayValue(value);
    return text ? { id: text, name: text } : null;
}

function systemInputPlatformTemplateRecordMatchesScope(record, values, kind) {
    if (!record || record.enabled === false) return false;
    if (record.template_kind && record.template_kind !== kind) return false;
    return SYSTEM_INPUT_PLATFORM_TEMPLATE_SCOPE_FIELDS[kind].every(field => (
        systemInputCascadeChoiceMatches(
            values[field],
            systemInputPlatformTemplateRecordChoice(record, SYSTEM_INPUT_PLATFORM_TEMPLATE_RECORD_SCOPE_FIELDS[field]),
        )
    ));
}

function systemInputPlatformTemplateCatalogAssessment(configuration = {}, template = null) {
    const values = systemInputPlatformTemplateScopeValues(configuration);
    const kind = systemInputPlatformTemplateKind(values);
    const required = SYSTEM_INPUT_PLATFORM_TEMPLATE_SCOPE_FIELDS[kind];
    const missing = required.filter(field => !systemInputCascadeValuePresent(values[field]));
    const index = systemInputPlatformTemplateCatalogIndexForCurrentSource();
    const records = index.records;
    const scopeCacheKey = `${kind}\u0001${required.map(field => systemInputPlatformTemplateChoiceCacheKey(values[field])).join('\u0001')}`;
    let candidates = [];
    if (!missing.length) {
        candidates = index.candidates.get(scopeCacheKey);
        if (!candidates) {
            candidates = systemInputPlatformTemplateIndexedCandidates(index, values, kind, required);
            index.candidates.set(scopeCacheKey, candidates);
        }
    }
    const reference = systemInputPlatformTemplateReference(template) || systemInputPlatformTemplateReference({
        platform_template_id: values.platformTemplateId,
        platform_template_name: values.platformTemplateName,
        platform_template_version: values.platformTemplateVersion,
    });
    const referenceCacheKey = reference ? `${scopeCacheKey}\u0001${systemInputPlatformTemplateReferenceCacheKey(reference)}` : '';
    let matched = null;
    if (reference) {
        if (index.matches.has(referenceCacheKey)) matched = index.matches.get(referenceCacheKey);
        else {
            matched = candidates.find(record => systemInputPlatformTemplateMatches(reference, record)) || null;
            index.matches.set(referenceCacheKey, matched);
        }
    }
    let status = 'ready';
    let message = candidates.length ? `可选 ${candidates.length} 个${kind === 'paper' ? '试卷模板' : '专项题型模板'}` : '当前范围没有可用模板';
    if (!records.length) {
        status = 'unavailable';
        message = '模板目录尚未同步，请先同步模板数据';
    } else if (missing.length) {
        status = 'pending';
        message = kind === 'paper' ? '先选择省份、城市、学段和年级' : '先选择省份和城市';
    } else if (reference && !matched) {
        status = 'conflict';
        message = '当前模板不适用于所选范围，请重新选择';
    } else if (reference && matched) {
        status = 'matched';
        message = `已匹配${kind === 'paper' ? '地区、学段和年级' : '地区'}`;
    } else if (!candidates.length) {
        status = 'empty';
        message = '当前范围没有可用模板，请调整范围或重新同步';
    }
    return { status, message, kind, missing, records, candidates, matched, reference, values };
}

function systemInputPlatformTemplateScopeDetail(template) {
    const kind = template?.template_kind || 'question';
    const fields = SYSTEM_INPUT_PLATFORM_TEMPLATE_SCOPE_FIELDS[kind] || SYSTEM_INPUT_PLATFORM_TEMPLATE_SCOPE_FIELDS.question;
    const scope = fields
        .map(field => systemInputDisplayValue(systemInputPlatformTemplateRecordChoice(
            template,
            SYSTEM_INPUT_PLATFORM_TEMPLATE_RECORD_SCOPE_FIELDS[field],
        )))
        .filter(Boolean);
    const questionTypes = Array.isArray(template?.question_types)
        ? template.question_types.map(item => systemInputDisplayValue(item)).filter(Boolean)
        : [];
    return [scope.join(' · '), questionTypes.length ? `含 ${questionTypes.join('、')}` : ''].filter(Boolean).join('；');
}

function systemInputPlatformTemplateReference(value) {
    if (!value || typeof value !== 'object') return null;
    const key = String(value.platform_template_key || value.platformTemplateKey || '').trim();
    const id = String(value.platform_template_id ?? value.platformTemplateId ?? '').trim();
    const name = String(value.name ?? value.platform_template_name ?? value.platformTemplateName ?? '').trim();
    const version = String(value.platform_template_version ?? value.platformTemplateVersion ?? '').trim();
    if (!key && !id && !name) return null;
    return {
        ...(key ? { platform_template_key: key } : {}),
        ...(id ? { platform_template_id: id } : {}),
        ...(name ? { name } : {}),
        ...(version ? { platform_template_version: version } : {}),
    };
}

function systemInputPlatformTemplateMatches(left, right) {
    const a = systemInputPlatformTemplateReference(left);
    const b = systemInputPlatformTemplateReference(right);
    if (!a || !b) return false;
    if (a.platform_template_key && b.platform_template_key) {
        return a.platform_template_key === b.platform_template_key;
    }
    if (a.platform_template_id && b.platform_template_id) {
        return systemInputNormalizeLabel(a.platform_template_id) === systemInputNormalizeLabel(b.platform_template_id)
            && (!a.platform_template_version || !b.platform_template_version
                || systemInputNormalizeLabel(a.platform_template_version) === systemInputNormalizeLabel(b.platform_template_version));
    }
    return Boolean(a.name && b.name)
        && systemInputNormalizeLabel(a.name) === systemInputNormalizeLabel(b.name);
}

function systemInputPlatformTemplateForKey(key) {
    const value = String(key || '');
    if (value === SYSTEM_INPUT_LEGACY_PLATFORM_TEMPLATE_KEY) {
        return systemInputPlatformTemplatePendingSelection;
    }
    return systemInputPlatformTemplateCatalogIndexForCurrentSource().byKey.get(value) || null;
}

function systemInputSelectedPlatformTemplate() {
    return systemInputPlatformTemplateForKey($('system-input-platform-template')?.value || '');
}

function systemInputPlatformTemplateNameForForm() {
    const selected = systemInputPlatformTemplateReference(systemInputSelectedPlatformTemplate());
    // The visible control is selection-only. Keep the name tied to the
    // selected catalogue reference so an edited text value can never leak
    // into the submitted configuration as an unverified template.
    return selected?.name || '';
}

function systemInputPlatformTemplateLabel(template) {
    const reference = systemInputPlatformTemplateReference(template);
    if (!reference) return '';
    return reference.name || '未命名平台模板';
}

function systemInputPlatformTemplateForName(name) {
    const normalizedName = systemInputNormalizeLabel(name);
    if (!normalizedName) return null;
    return systemInputPlatformTemplates.find(template => (
        systemInputNormalizeLabel(systemInputPlatformTemplateLabel(template)) === normalizedName
    )) || null;
}

function systemInputPlatformTemplateInteractionPresentation(name, selected, busy = false) {
    // 平台模板只允许从同步目录中选择，不允许自定义新建或保存常用模板。
    // 交互状态简化为零配置的“选择即确认”，不提供保存/管理入口。
    return {
        busy: Boolean(busy),
        isSaved: false,
        note: '模板仅来自同步的平台模板目录；在“发布范围”选择省份、城市、学段和年级后即可选择。',
        showSave: false,
        showManage: false,
    };
}

function updateSystemInputPlatformTemplateActions() {
    // 平台模板只允许从同步目录中选择，已移除“保存为常用/管理/重命名/删除”
    // 等自定义配置入口。选择状态与提示由 renderSystemInputPlatformTemplateOptions
    // 基于平台目录的匹配状态统一更新，此处不再需要操作任何表单动作按钮。
}

function renderSystemInputPlatformTemplateOptions(preferred = null) {
    const field = $('system-input-platform-template');
    const search = $('system-input-platform-template-search');
    if (!field || !search) return;
    const previousValue = String(field.value || '');
    const wanted = systemInputPlatformTemplateReference(preferred) || systemInputPlatformTemplatePendingSelection;
    const assessment = systemInputPlatformTemplateCatalogAssessment({}, wanted);
    const hiddenSource = Boolean(field.closest?.('[hidden]'));
    const picker = typeof systemInputPickerRegistry !== 'undefined'
        ? systemInputPickerRegistry.get?.('system-input-platform-template-search')
        : null;
    // The legacy form is a hidden source while the unified target workspace
    // is active. Keep its selected key synchronized, but defer constructing
    // one picker option object per catalogue record until that form is shown.
    const scopedTemplates = hiddenSource ? [] : systemInputPlatformTemplateCatalog === null
        ? systemInputPlatformTemplates
        : assessment.candidates;
    const options = hiddenSource ? null : scopedTemplates.map(template => ({
            value: String(template.platform_template_key || ''),
            label: systemInputPlatformTemplateLabel(template),
            detail: systemInputPlatformTemplateScopeDetail(template) || '已匹配当前范围',
            raw: template,
        })).filter(option => option.value && option.label);
    const optionSource = systemInputPlatformTemplateCatalog === null
        ? systemInputPlatformTemplates
        : systemInputPlatformTemplateCatalog.records;
    if (hiddenSource && picker && picker._systemInputPlatformTemplateOptionsSource !== optionSource) {
        const sourceTemplates = systemInputPlatformTemplateCatalog === null
            ? (Array.isArray(systemInputPlatformTemplates) ? systemInputPlatformTemplates : [])
            : systemInputPlatformTemplateCatalogRecords();
        const sourceOptions = sourceTemplates.map(template => ({
            value: String(template.platform_template_key || ''),
            label: systemInputPlatformTemplateLabel(template),
            detail: systemInputPlatformTemplateScopeDetail(template) || '已匹配当前范围',
            raw: template,
        })).filter(option => option.value && option.label);
        setSystemInputPickerOptions('system-input-platform-template-search', sourceOptions);
        picker._systemInputPlatformTemplateOptionsSource = optionSource;
    }
    let selectedKey = '';
    const matched = hiddenSource ? null : wanted && scopedTemplates.find(template => systemInputPlatformTemplateMatches(wanted, template));
    if (hiddenSource && wanted) {
        // The pending reference is authoritative while the source form is
        // hidden. The legacy sentinel keeps that reference available to save
        // collection without scanning every catalogue record to resolve a
        // key that the user cannot currently see.
        selectedKey = SYSTEM_INPUT_LEGACY_PLATFORM_TEMPLATE_KEY;
    } else if (matched?.platform_template_key) {
        selectedKey = String(matched.platform_template_key);
    } else if (wanted && (wanted.name || wanted.platform_template_id)) {
        if (options) options.push({
                value: SYSTEM_INPUT_LEGACY_PLATFORM_TEMPLATE_KEY,
                label: `${systemInputPlatformTemplateLabel(wanted)}（当前配置）`,
                detail: assessment.status === 'conflict' ? '当前配置与所选范围不匹配' : '当前配置中的名称引用',
                raw: wanted,
            });
        selectedKey = SYSTEM_INPUT_LEGACY_PLATFORM_TEMPLATE_KEY;
    } else if (previousValue && (options
        ? options.some(option => option.value === previousValue)
        : scopedTemplates.some(template => String(template.platform_template_key || '') === previousValue))) {
        selectedKey = previousValue;
    }
    field.value = selectedKey;
    systemInputPlatformTemplatePendingSelection = systemInputPlatformTemplateForKey(selectedKey)
        ? systemInputPlatformTemplateReference(systemInputPlatformTemplateForKey(selectedKey))
        : systemInputPlatformTemplatePendingSelection;
    search.value = selectedKey ? systemInputPlatformTemplateLabel(systemInputPlatformTemplateForKey(selectedKey)) : '';
    syncSystemInputPicker('system-input-platform-template-search', { selectedValue: selectedKey });
    if (options) {
        setSystemInputPickerOptions('system-input-platform-template-search', options);
        if (picker) picker._systemInputPlatformTemplateOptionsSource = optionSource;
    }
    const legacyMode = systemInputPlatformTemplateCatalog === null;
    const catalogReady = legacyMode || assessment.records.length > 0;
    const parentsReady = legacyMode || assessment.missing.length === 0;
    setSystemInputPickerEnabled('system-input-platform-template-search', catalogReady && parentsReady, {
        enabledPlaceholder: `选择${assessment.kind === 'paper' ? '试卷模板' : '专项题型模板'}`,
        disabledPlaceholder: catalogReady ? assessment.message : '先同步模板数据',
    });
    const label = assessment.kind === 'paper' ? '试卷模板' : '专项题型模板';
    const labelElement = $('system-input-platform-template-label');
    if (labelElement) labelElement.innerHTML = `${label} <small>必填</small>`;
    search.setAttribute('aria-label', label);
    updateSystemInputPlatformTemplateActions();
    const note = $('system-input-platform-template-note');
    if (note) note.textContent = assessment.message;
    renderSystemInputPlatformTemplateCatalogStatus();
}

function handleSystemInputPlatformTemplateChange(option = null) {
    const selected = option || systemInputSelectedPlatformTemplate();
    const field = $('system-input-platform-template');
    if (field && option) field.value = String(option.value || '');
    if (option) {
        const search = $('system-input-platform-template-search');
        if (search) search.value = option.label || '';
    }
    systemInputPlatformTemplatePendingSelection = selected
        ? systemInputPlatformTemplateReference(selected.raw || selected)
        : null;
    updateSystemInputPlatformTemplateActions();
}

function handleSystemInputPlatformTemplateInput(value) {
    const field = $('system-input-platform-template');
    const matched = systemInputPlatformTemplateForName(value);
    const selectedKey = String(matched?.platform_template_key || '');
    if (field) field.value = selectedKey;
    systemInputPlatformTemplatePendingSelection = matched
        ? systemInputPlatformTemplateReference(matched)
        : null;
    syncSystemInputPicker('system-input-platform-template-search', { selectedValue: selectedKey });
    updateSystemInputPlatformTemplateActions();
}

function setSystemInputPlatformTemplateSelection(value) {
    systemInputPlatformTemplatePendingSelection = systemInputPlatformTemplateReference(value);
    renderSystemInputPlatformTemplateOptions(systemInputPlatformTemplatePendingSelection);
}

async function loadSystemInputPlatformTemplates(inputType = $('system-input-type')?.value || 'paper', { force = false } = {}) {
    const type = String(inputType || 'paper').trim() || 'paper';
    if (!workflowApi?.getPlatformTemplateCatalog && !workflowApi?.listSystemInputPlatformTemplates) return;
    if (!force && systemInputPlatformTemplatesType === type) return;
    const requestId = ++systemInputPlatformTemplateRequestId;
    systemInputPlatformTemplatesType = type;
    systemInputPlatformTemplates = [];
    resetSystemInputPlatformTemplateCatalogIndex();
    renderSystemInputPlatformTemplateOptions();
    try {
        if (workflowApi?.getPlatformTemplateCatalog) {
            const response = await workflowApi.getPlatformTemplateCatalog();
            if (requestId !== systemInputPlatformTemplateRequestId || systemInputPlatformTemplatesType !== type) return;
            applySystemInputPlatformTemplateCatalogResponse(response);
            const syncId = systemInputPlatformTemplateCatalogSync?.sync_id;
            if (String(systemInputPlatformTemplateCatalogSync?.status || '').toUpperCase() === 'RUNNING' && syncId) {
                scheduleSystemInputPlatformTemplateCatalogPoll(syncId, requestId);
            }
            return;
        }
        const templates = await workflowApi.listSystemInputPlatformTemplates(type);
        if (requestId !== systemInputPlatformTemplateRequestId || systemInputPlatformTemplatesType !== type) return;
        systemInputPlatformTemplates = Array.isArray(templates) ? templates.slice(0, 256) : [];
        resetSystemInputPlatformTemplateCatalogIndex();
        renderSystemInputPlatformTemplateOptions();
        // The target editor can be open while the catalogue request is in
        // flight. Rebuild its shared controls once the authoritative list
        // arrives so a temporary empty state never becomes permanent.
        root.targetEditorRefreshAfterDataLoad?.();
    } catch (_) {
        // A local catalog failure must not turn into a platform request. Keep
        // the current saved reference visible as a legacy option if present.
        renderSystemInputPlatformTemplateOptions();
    }
}

function applySystemInputPlatformTemplateCatalogResponse(response) {
    if (response?.catalog && typeof response.catalog === 'object') {
        systemInputPlatformTemplateCatalog = response.catalog;
        systemInputPlatformTemplates = Array.isArray(response.catalog.records)
            ? response.catalog.records.slice(0, 20_000)
            : [];
        resetSystemInputPlatformTemplateCatalogIndex();
    }
    if (response?.sync && typeof response.sync === 'object') {
        systemInputPlatformTemplateCatalogSync = {
            ...systemInputPlatformTemplateCatalogSync,
            ...response.sync,
        };
    }
    renderSystemInputPlatformTemplateOptions();
    renderSystemInputPlatformTemplateCatalogStatus();
    root.targetEditorRefreshAfterDataLoad?.();
}

function renderSystemInputPlatformTemplateCatalogStatus() {
    const statusElement = $('system-input-platform-template-catalog-status');
    const note = $('system-input-platform-template-catalog-note');
    const button = $('system-input-platform-template-sync-btn');
    const sync = systemInputPlatformTemplateCatalogSync || {};
    const catalog = systemInputPlatformTemplateCatalog || {};
    const status = String(sync.status || 'IDLE').toUpperCase();
    const running = status === 'RUNNING';
    const questionCount = Number(catalog.question_template_count ?? sync.question_template_count ?? 0) || 0;
    const paperCount = Number(catalog.paper_template_count ?? sync.paper_template_count ?? 0) || 0;
    if (statusElement) {
        statusElement.textContent = running
            ? '同步中'
            : status === 'FAILED'
                ? '同步失败'
                : questionCount + paperCount > 0
                    ? `专项 ${questionCount} · 试卷 ${paperCount}`
                    : '尚未同步';
        statusElement.classList.toggle('is-running', running);
        statusElement.classList.toggle('is-ready', !running && status !== 'FAILED' && questionCount + paperCount > 0);
        statusElement.classList.toggle('is-error', status === 'FAILED');
    }
    if (note) {
        note.textContent = running || status === 'FAILED'
            ? (sync.message || '正在读取平台模板目录……')
            : '专项模板按省/市筛选；试卷模板按省/市、学段、年级筛选。';
    }
    if (button) {
        button.disabled = running || systemInputPlatformTemplateCatalogSyncBusy;
        button.textContent = running || systemInputPlatformTemplateCatalogSyncBusy ? '正在同步…' : '同步模板数据';
    }
}

function stopSystemInputPlatformTemplateCatalogPolling() {
    if (!systemInputPlatformTemplateCatalogPollTimer) return;
    clearTimeout(systemInputPlatformTemplateCatalogPollTimer);
    systemInputPlatformTemplateCatalogPollTimer = null;
}

function scheduleSystemInputPlatformTemplateCatalogPoll(syncId, requestId, delay = 900) {
    stopSystemInputPlatformTemplateCatalogPolling();
    systemInputPlatformTemplateCatalogPollTimer = setTimeout(() => {
        void pollSystemInputPlatformTemplateCatalogSync(syncId, requestId);
    }, delay);
}

async function pollSystemInputPlatformTemplateCatalogSync(syncId, requestId) {
    if (!workflowApi?.getPlatformTemplateCatalogSync || requestId !== systemInputPlatformTemplateRequestId) return;
    try {
        const response = await workflowApi.getPlatformTemplateCatalogSync(syncId);
        if (requestId !== systemInputPlatformTemplateRequestId) return;
        applySystemInputPlatformTemplateCatalogResponse(response);
        const status = String(systemInputPlatformTemplateCatalogSync?.status || '').toUpperCase();
        if (status === 'RUNNING') {
            scheduleSystemInputPlatformTemplateCatalogPoll(syncId, requestId);
            return;
        }
        systemInputPlatformTemplateCatalogSyncBusy = false;
        renderSystemInputPlatformTemplateCatalogStatus();
        if (typeof showToast === 'function') showToast(
            systemInputPlatformTemplateCatalogSync.message || (status === 'SUCCEEDED' ? '平台模板同步完成' : '平台模板同步失败'),
            status === 'SUCCEEDED' ? 'success' : 'warning',
        );
    } catch (error) {
        if (requestId !== systemInputPlatformTemplateRequestId) return;
        systemInputPlatformTemplateCatalogSyncBusy = false;
        systemInputPlatformTemplateCatalogSync = {
            ...systemInputPlatformTemplateCatalogSync,
            status: 'FAILED',
            message: String(error?.message || '读取平台模板同步状态失败'),
        };
        renderSystemInputPlatformTemplateCatalogStatus();
    }
}

async function startSystemInputPlatformTemplateCatalogSync() {
    if (!workflowApi?.startPlatformTemplateCatalogSync || systemInputPlatformTemplateCatalogSyncBusy) return;
    systemInputPlatformTemplateCatalogSyncBusy = true;
    const requestId = ++systemInputPlatformTemplateRequestId;
    systemInputPlatformTemplateCatalogSync = {
        ...systemInputPlatformTemplateCatalogSync,
        status: 'RUNNING',
        message: '正在打开平台模板管理页面……',
    };
    renderSystemInputPlatformTemplateCatalogStatus();
    try {
        const response = await workflowApi.startPlatformTemplateCatalogSync({
            idempotencyKey: `renderer-platform-template-catalog-sync-${Date.now()}`,
        });
        if (requestId !== systemInputPlatformTemplateRequestId) return;
        applySystemInputPlatformTemplateCatalogResponse(response);
        const syncId = systemInputPlatformTemplateCatalogSync?.sync_id;
        if (!syncId) throw new Error('平台模板同步未返回任务编号');
        scheduleSystemInputPlatformTemplateCatalogPoll(syncId, requestId, 350);
    } catch (error) {
        if (requestId !== systemInputPlatformTemplateRequestId) return;
        systemInputPlatformTemplateCatalogSyncBusy = false;
        systemInputPlatformTemplateCatalogSync = {
            ...systemInputPlatformTemplateCatalogSync,
            status: 'FAILED',
            message: String(error?.message || '平台模板同步启动失败'),
        };
        renderSystemInputPlatformTemplateCatalogStatus();
        if (typeof showToast === 'function') showToast(systemInputPlatformTemplateCatalogSync.message, 'warning');
    }
}

function refreshSystemInputPlatformTemplatesForScope({ clear = false } = {}) {
    if (clear) {
        systemInputPlatformTemplatePendingSelection = null;
        setSystemInputField('system-input-platform-template', '');
        syncSystemInputPickerInput('system-input-platform-template-search', '', '');
    }
    renderSystemInputPlatformTemplateOptions(systemInputPlatformTemplatePendingSelection);
    root.targetEditorRefreshAfterDataLoad?.();
}


registerRendererModule("systemInput.platformTemplates", {
    SYSTEM_INPUT_LEGACY_PLATFORM_TEMPLATE_KEY,
    SYSTEM_INPUT_PLATFORM_TEMPLATE_SCOPE_FIELDS,
    systemInputPlatformTemplateReference,
    systemInputPlatformTemplateMatches,
    systemInputPlatformTemplateForKey,
    systemInputSelectedPlatformTemplate,
    systemInputPlatformTemplateNameForForm,
    systemInputPlatformTemplateLabel,
    systemInputPlatformTemplateForName,
    systemInputPlatformTemplateInteractionPresentation,
    systemInputPlatformTemplateKind,
    systemInputPlatformTemplateCatalogAssessment,
    systemInputPlatformTemplateCatalogRecords,
    updateSystemInputPlatformTemplateActions,
    renderSystemInputPlatformTemplateOptions,
    handleSystemInputPlatformTemplateChange,
    handleSystemInputPlatformTemplateInput,
    setSystemInputPlatformTemplateSelection,
    loadSystemInputPlatformTemplates,
    applySystemInputPlatformTemplateCatalogResponse,
    renderSystemInputPlatformTemplateCatalogStatus,
    startSystemInputPlatformTemplateCatalogSync,
    refreshSystemInputPlatformTemplatesForScope,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
