/** Renderer module: systemInput.textbookCatalog */
(function attachRendererFeature_systemInput_textbookCatalog(root) {
    'use strict';

const TEXTBOOK_DIRECTORY_FIELDS = Object.freeze([
    { key: 'version', id: 'system-input-textbook-version', label: '版本', placeholder: '搜索教材版本' },
    { key: 'stage', id: 'system-input-textbook-stage', label: '学段', placeholder: '搜索学段' },
    { key: 'grade', id: 'system-input-textbook-grade', label: '年级', placeholder: '搜索年级' },
    { key: 'volume', id: 'system-input-textbook-volume', label: '册别', placeholder: '搜索册别' },
    { key: 'unit', id: 'system-input-textbook-unit', label: '教材单元', placeholder: '搜索教材单元' },
    { key: 'lesson', id: 'system-input-textbook-lesson', label: '课时', placeholder: '搜索课时' },
]);

const TEXTBOOK_DIRECTORY_KEY_TO_CONFIG = Object.freeze({
    ...TEXTBOOK_DIRECTORY_FIELD_ALIASES,
});
const TEXTBOOK_CONFIG_TO_DIRECTORY = Object.freeze(Object.fromEntries(
    Object.entries(TEXTBOOK_DIRECTORY_KEY_TO_CONFIG).map(([key, configurationKey]) => [configurationKey, key]),
));
const TEXTBOOK_PARENT_FIELDS = Object.freeze(Object.fromEntries(
    TEXTBOOK_DIRECTORY_FIELDS.map(({ key }) => [
        key,
        systemInputCascadeParents('textbook', TEXTBOOK_DIRECTORY_KEY_TO_CONFIG[key])
            .map(parent => TEXTBOOK_CONFIG_TO_DIRECTORY[parent]),
    ]),
));

const TEXTBOOK_CATALOG_SYNC_META_SCHEMA = 1;
const TEXTBOOK_CATALOG_SYNC_META_STORAGE_KEY = 'wordtts.system-input.textbook-catalog-sync-meta.v1';

// The snapshot is refreshed whenever another entry populates the form. It is
// only used to distinguish a repeated picker commit from a real edit.
let textbookRenderedDirectoryValues = null;
let textbookCatalogSyncMeta = null;

function textbookConfigurationKey(field) {
    return `textbook${field.charAt(0).toUpperCase()}${field.slice(1)}`;
}

function textbookDirectoryValues(configuration = {}) {
    const source = configuration && typeof configuration === 'object' ? configuration : {};
    return Object.fromEntries(TEXTBOOK_DIRECTORY_FIELDS.map(({ key }) => [
        key,
        textbookChoiceName(source[textbookConfigurationKey(key)]
            ?? source[`textbook_${key}`]
            ?? source[key]),
    ]));
}

function textbookText(value) {
    return String(value ?? '').trim();
}

function textbookCatalogStorage() {
    try {
        if (typeof getRendererStorage === 'function') {
            const storage = getRendererStorage();
            if (storage && typeof storage.getItem === 'function' && typeof storage.setItem === 'function') return storage;
        }
    } catch (_) {
        // Storage is optional; the server-side catalogue remains authoritative.
    }
    try {
        if (typeof rendererStorage !== 'undefined'
            && rendererStorage
            && typeof rendererStorage.getItem === 'function'
            && typeof rendererStorage.setItem === 'function') return rendererStorage;
    } catch (_) {
        // Keep the in-memory fallback below.
    }
    try {
        if (root?.localStorage
            && typeof root.localStorage.getItem === 'function'
            && typeof root.localStorage.setItem === 'function') return root.localStorage;
    } catch (_) {
        // Access to browser storage can be denied by the host.
    }
    return null;
}

function normalizeTextbookCatalogSyncMeta(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const rawTimestamp = textbookText(value.synced_at ?? value.syncedAt);
    const date = rawTimestamp ? new Date(rawTimestamp) : null;
    if (!date || Number.isNaN(date.getTime())) return null;
    const count = Number(value.record_count ?? value.recordCount ?? 0);
    const sourceTotal = Number(value.source_total ?? value.sourceTotal ?? count);
    const sourceTotalScope = textbookText(value.source_total_scope ?? value.sourceTotalScope) === 'source_books'
        ? 'source_books'
        : 'expanded_paths';
    const pageCount = Number(value.page_count ?? value.pageCount ?? 0);
    const normalizedCount = Number.isFinite(count) && count >= 0 ? Math.floor(count) : 0;
    const normalizedSourceTotal = Number.isFinite(sourceTotal) && sourceTotal >= 0
        ? Math.floor(sourceTotal)
        : normalizedCount;
    return {
        schemaVersion: TEXTBOOK_CATALOG_SYNC_META_SCHEMA,
        syncedAt: date.toISOString(),
        recordCount: normalizedCount,
        sourceTotal: sourceTotalScope === 'source_books'
            ? normalizedSourceTotal
            : Math.max(normalizedSourceTotal, normalizedCount),
        sourceTotalScope,
        pageCount: Number.isFinite(pageCount) && pageCount >= 0 ? Math.floor(pageCount) : 0,
    };
}

function readTextbookCatalogSyncMeta() {
    const storage = textbookCatalogStorage();
    if (!storage) return null;
    try {
        const raw = storage.getItem(TEXTBOOK_CATALOG_SYNC_META_STORAGE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (parsed?.schemaVersion !== TEXTBOOK_CATALOG_SYNC_META_SCHEMA) return null;
        return normalizeTextbookCatalogSyncMeta(parsed);
    } catch (_) {
        return null;
    }
}

function persistTextbookCatalogSyncMeta(catalog = {}, sync = {}) {
    const status = String(sync?.status || '').toUpperCase();
    const catalogCount = Number(catalog?.record_count);
    const syncCount = Number(sync?.record_count);
    const persistedCount = Number(textbookCatalogSyncMeta?.recordCount);
    const count = [catalogCount, syncCount, persistedCount]
        .filter(value => Number.isFinite(value) && value >= 0)
        .reduce((largest, value) => Math.max(largest, value), 0);
    // The empty catalogue response carries a generated timestamp as well, but
    // it does not represent a completed user sync. Only a non-empty directory
    // is allowed to create or refresh the durable “last sync” record.
    if (count <= 0) return false;
    const catalogSourceTotal = Number(catalog?.source_total);
    const syncSourceTotal = Number(sync?.source_total);
    const persistedSourceTotal = Number(textbookCatalogSyncMeta?.sourceTotal);
    const sourceTotalScopes = [
        catalog?.source_total_scope,
        sync?.source_total_scope,
        textbookCatalogSyncMeta?.sourceTotalScope,
    ].map(value => textbookText(value));
    const sourceTotalScope = sourceTotalScopes.includes('source_books')
        ? 'source_books'
        : sourceTotalScopes.includes('expanded_paths')
            ? 'expanded_paths'
            : 'expanded_paths';
    const sourceTotals = [catalogSourceTotal, syncSourceTotal, persistedSourceTotal]
        .filter(value => Number.isFinite(value) && value >= 0)
        .map(value => Math.floor(value));
    const sourceTotal = sourceTotalScope === 'source_books'
        ? (sourceTotals.length ? Math.max(...sourceTotals) : count)
        : sourceTotals.reduce((largest, value) => Math.max(largest, value), count);
    const catalogPageCount = Number(catalog?.page_count);
    const syncPageCount = Number(sync?.page_count);
    const persistedPageCount = Number(textbookCatalogSyncMeta?.pageCount);
    const pageCount = [catalogPageCount, syncPageCount, persistedPageCount]
        .filter(value => Number.isFinite(value) && value >= 0)
        .reduce((largest, value) => Math.max(largest, value), 0);
    const timestamp = catalogCount > 0 && catalog?.synced_at
        ? catalog.synced_at
        : syncCount > 0 && sync?.synced_at
            ? sync.synced_at
            : status === 'SUCCEEDED' && syncCount > 0
                ? sync.finished_at
                : textbookCatalogSyncMeta?.syncedAt;
    const normalized = normalizeTextbookCatalogSyncMeta({
        synced_at: timestamp,
        record_count: count,
        source_total: sourceTotal,
        source_total_scope: sourceTotalScope,
        page_count: pageCount,
    });
    if (!normalized) return false;
    textbookCatalogSyncMeta = normalized;
    const storage = textbookCatalogStorage();
    if (!storage) return false;
    try {
        storage.setItem(TEXTBOOK_CATALOG_SYNC_META_STORAGE_KEY, JSON.stringify(normalized));
        return true;
    } catch (_) {
        // A full or disabled localStorage must not block catalogue use.
        return false;
    }
}

function textbookToast(message, tone = 'info') {
    if (typeof showToast === 'function') showToast(message, tone);
}

function textbookNormalized(value) {
    const text = textbookText(value).normalize('NFKC');
    return typeof systemInputNormalizeLabel === 'function'
        ? systemInputNormalizeLabel(text)
        : text.replace(/\s+/g, '').toLocaleLowerCase('zh-CN');
}

function textbookChoiceName(value) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
        return textbookText(value.name ?? value.label ?? value.text ?? value.value ?? value.id);
    }
    return textbookText(value);
}

function textbookChoiceId(value, fallback = '') {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
        return textbookText(value.id ?? value.value ?? value.code) || fallback;
    }
    return fallback;
}

function textbookFieldValue(field) {
    const descriptor = TEXTBOOK_DIRECTORY_FIELDS.find(item => item.key === field);
    return textbookText(descriptor ? $(descriptor.id)?.value : '');
}

function textbookCatalogRecords() {
    return Array.isArray(systemInputTextbookCatalog?.records)
        ? systemInputTextbookCatalog.records.filter(record => record && typeof record === 'object')
        : [];
}

function textbookRecordChoice(record, field) {
    if (!record || typeof record !== 'object') return null;
    const choice = record[field];
    const name = textbookChoiceName(choice);
    return name ? { id: textbookChoiceId(choice, name), name } : null;
}

function textbookRecordMatchesValues(record, values, fields) {
    return fields.every(field => !values[field]
        || textbookNormalized(textbookRecordChoice(record, field)?.name) === textbookNormalized(values[field]));
}

function textbookUniqueDirectoryRecords(records) {
    const seen = new Set();
    return records.filter(record => {
        // Repeated content rows can share a directory. Different directory
        // IDs with identical labels are still ambiguous and must stay visible.
        const key = JSON.stringify(TEXTBOOK_DIRECTORY_FIELDS.map(({ key: field }) => {
            const choice = textbookRecordChoice(record, field);
            return [choice?.id || '', textbookNormalized(choice?.name)];
        }));
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
    });
}

/**
 * Assess saved/custom values against the cached catalog only. This function
 * never calls the platform, never mutates configuration, and only suggests
 * empty unit/lesson fields after the shared textbook path has been selected.
 * Both camelCase configuration keys and plain catalog field keys are accepted.
 */
function systemInputTextbookCatalogAssessment(configuration = {}, { records = textbookCatalogRecords(), hints = {} } = {}) {
    const catalog = (Array.isArray(records) ? records : []).filter(record => record && typeof record === 'object');
    const values = textbookDirectoryValues(configuration);
    const hintValues = textbookDirectoryValues(hints);
    const fields = {};
    TEXTBOOK_DIRECTORY_FIELDS.forEach(({ key, label }) => {
        const value = values[key];
        const parents = TEXTBOOK_PARENT_FIELDS[key];
        let status = 'missing';
        let message = `待选择${label}`;
        if (value && !catalog.length) {
            status = 'unavailable';
            message = '目录未同步，当前填写值待核对';
        } else if (value && parents.some(parent => !values[parent])) {
            status = 'pending';
            message = '已保留原值；补齐上级教材项后核对';
        } else if (value) {
            const pathMatches = catalog.filter(record => textbookRecordMatchesValues(record, values, parents));
            if (pathMatches.some(record => textbookRecordMatchesValues(record, values, [key]))) {
                status = 'matched';
                message = '已匹配本地目录';
            } else if (catalog.some(record => textbookRecordMatchesValues(record, values, [key]))) {
                status = 'conflict';
                message = '与上级教材项不匹配，已保留原值，请重新选择';
            } else {
                status = 'manual';
                message = '手动填写，未在本地目录中找到，待核对';
            }
        }
        fields[key] = { key, configurationKey: textbookConfigurationKey(key), label, value, status, message };
    });

    const fieldKeys = TEXTBOOK_DIRECTORY_FIELDS.map(({ key }) => key);
    let candidates = textbookUniqueDirectoryRecords(catalog.filter(record => textbookRecordMatchesValues(record, values, fieldKeys)));
    const sharedReady = ['version', 'stage', 'grade', 'volume'].every(key => values[key]);
    const hintFields = ['unit', 'lesson'].filter(key => !values[key] && hintValues[key]);
    if (sharedReady && hintFields.length) {
        // A detected hint that is absent from the directory must not silently
        // turn into a suggestion for some other unit or lesson.
        candidates = candidates.filter(record => textbookRecordMatchesValues(record, hintValues, hintFields));
    }
    const suggestions = {};
    if (sharedReady && candidates.length === 1) {
        ['unit', 'lesson'].forEach(key => {
            const choice = textbookRecordChoice(candidates[0], key);
            if (!values[key] && choice) suggestions[textbookConfigurationKey(key)] = choice.name;
        });
    }

    let status = 'incomplete';
    let label = sharedReady ? '待选教材单元 / 课时' : '待补教材信息';
    let message = label;
    if (!catalog.length) {
        status = 'unavailable';
        label = '目录未同步，待核对';
        message = '已保留填写内容，可同步教材目录后核对';
    } else if (Object.values(fields).some(field => field.status === 'manual')) {
        status = 'manual';
        label = '手动填写，待核对';
        message = '部分填写内容未在本地目录中找到，请核对名称或刷新目录';
    } else if (Object.values(fields).some(field => field.status === 'conflict')) {
        status = 'conflict';
        label = '目录组合待核对';
        message = '教材目录与上级设置不匹配，原值已保留';
    } else if (sharedReady && hintFields.length && !candidates.length) {
        status = 'conflict';
        label = '识别建议待核对';
        message = '识别出的教材单元或课时未在所选教材目录中找到';
    } else if (fieldKeys.every(key => values[key])) {
        status = candidates.length === 1 ? 'matched' : 'conflict';
        label = candidates.length === 1 ? '已匹配本地目录' : '目录存在同名项';
        message = candidates.length === 1
            ? '当前目录组合存在于本地同步记录中；保存时仍会按录入流程校验'
            : '本地目录中有多个同名目标，请核对教材目录';
    } else if (Object.keys(suggestions).length) {
        status = 'suggested';
        label = '有目录建议';
        message = '所选教材下有唯一匹配目录，可补入空白的教材单元 / 课时';
    }
    return { status, label, message, suggestions, fields, candidates };
}

/** Keep values for comparison instead of destructively clearing descendants. */
function systemInputReconcileTextbookDirectory(previous = {}, next = {}, records = textbookCatalogRecords()) {
    const before = textbookDirectoryValues(previous);
    const values = textbookDirectoryValues(next);
    const assessment = systemInputTextbookCatalogAssessment(values, { records });
    const changedFields = TEXTBOOK_DIRECTORY_FIELDS.map(({ key }) => key)
        .filter(key => textbookNormalized(before[key]) !== textbookNormalized(values[key]));
    const invalidFields = TEXTBOOK_DIRECTORY_FIELDS.map(({ key }) => key)
        .filter(key => ['manual', 'conflict', 'pending'].includes(assessment.fields[key].status));
    return { values, changedFields, invalidFields, assessment };
}

function textbookOptionDetail(record, field) {
    const parents = (TEXTBOOK_PARENT_FIELDS[field] || []).map(parent => textbookRecordChoice(record, parent)?.name).filter(Boolean);
    return parents.join(' · ');
}

function textbookCatalogOptions(field, assessment) {
    const records = textbookCatalogRecords();
    const values = Object.fromEntries(TEXTBOOK_DIRECTORY_FIELDS.map(({ key }) => [
        TEXTBOOK_DIRECTORY_KEY_TO_CONFIG[key],
        textbookFieldValue(key),
    ]));
    const canonicalField = TEXTBOOK_DIRECTORY_KEY_TO_CONFIG[field];
    const current = textbookFieldValue(field);
    return systemInputCascadeCatalogOptions({
        inputType: 'textbook',
        field: canonicalField,
        values,
        records,
        fallback: systemInputTextbookCatalog?.options?.[field],
        getRecordValue: (record, key) => textbookRecordChoice(
            record,
            TEXTBOOK_CONFIG_TO_DIRECTORY[key],
        ),
        getRecordDetail: record => textbookOptionDetail(record, field),
        currentValue: current,
        currentDetail: assessment?.fields[field]?.message || '当前填写值，待核对',
    });
}

function textbookCatalogReady() {
    return textbookCatalogRecords().length > 0;
}

function renderSystemInputTextbookOptions() {
    const values = Object.fromEntries(TEXTBOOK_DIRECTORY_FIELDS.map(({ key }) => [key, textbookFieldValue(key)]));
    const cascadeValues = Object.fromEntries(TEXTBOOK_DIRECTORY_FIELDS.map(({ key }) => [
        TEXTBOOK_DIRECTORY_KEY_TO_CONFIG[key],
        values[key],
    ]));
    const assessment = systemInputTextbookCatalogAssessment(values);
    const catalogReady = textbookCatalogReady();
    TEXTBOOK_DIRECTORY_FIELDS.forEach(({ key, id, label, placeholder }) => {
        setSystemInputPickerOptions(id, textbookCatalogOptions(key, assessment));
        const parents = TEXTBOOK_PARENT_FIELDS[key] || [];
        const enabled = systemInputCascadeParentsReady(
            'textbook',
            TEXTBOOK_DIRECTORY_KEY_TO_CONFIG[key],
            cascadeValues,
        );
        setSystemInputPickerEnabled(id, enabled, {
            enabledPlaceholder: catalogReady ? `选择${label}` : placeholder,
            disabledPlaceholder: `先选择${TEXTBOOK_DIRECTORY_FIELDS.find(item => item.key === parents.at(-1))?.label || '上级教材项'}`,
        });
        const input = $(id);
        if (input) {
            // Once a directory is available these fields are selectors, not
            // free-form text boxes. Existing manual values stay visible as a
            // reviewable option, while new values must follow the cascade.
            input.readOnly = catalogReady;
            input.setAttribute('aria-readonly', catalogReady ? 'true' : 'false');
            if (typeof input.toggleAttribute === 'function') {
                input.toggleAttribute('data-textbook-cascade-readonly', catalogReady);
            } else if (catalogReady) {
                // Keep the renderer compatible with the lightweight DOM
                // doubles used by the catalog logic tests.
                input.setAttribute('data-textbook-cascade-readonly', '');
            }
            input.setAttribute('aria-label', label);
            input.setAttribute('data-textbook-catalog-status', assessment.fields[key].status);
            renderSystemInputTextbookFieldNote(input, assessment.fields[key]);
        }
    });
    textbookRenderedDirectoryValues = values;
}

function renderSystemInputTextbookFieldNote(input, field) {
    const container = input.closest?.('.system-input-form-field');
    if (!container || typeof document?.createElement !== 'function') return;
    const noteId = `${input.id}-catalog-note`;
    let note = $(noteId);
    // The section already has one catalog sync notice; repeat only messages
    // specific to an individual value beside its input.
    const show = Boolean(field.value) && ['manual', 'conflict', 'pending'].includes(field.status);
    if (!note && !show) return;
    if (!note) {
        note = document.createElement('small');
        note.id = noteId;
        note.className = 'system-input-form-hint';
        container.appendChild(note);
    }
    note.hidden = !show;
    note.textContent = show ? field.message : '';
    const describedBy = new Set((input.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
    if (show) describedBy.add(noteId);
    else describedBy.delete(noteId);
    if (describedBy.size) input.setAttribute('aria-describedby', [...describedBy].join(' '));
    else input.removeAttribute('aria-describedby');
}

function handleSystemInputTextbookFieldChange(fieldId) {
    const descriptor = TEXTBOOK_DIRECTORY_FIELDS.find(item => item.id === fieldId);
    if (!descriptor) return;
    const next = Object.fromEntries(TEXTBOOK_DIRECTORY_FIELDS.map(({ key }) => [key, textbookFieldValue(key)]));
    const result = systemInputReconcileTextbookDirectory(textbookRenderedDirectoryValues || next, next);
    renderSystemInputTextbookOptions();
    if (result.changedFields.includes(descriptor.key)) {
        const descendants = new Set(systemInputCascadeDescendants(
            'textbook',
            TEXTBOOK_DIRECTORY_KEY_TO_CONFIG[descriptor.key],
        ).map(key => TEXTBOOK_CONFIG_TO_DIRECTORY[key]));
        const affected = result.invalidFields.filter(key => descendants.has(key));
        if (affected.length) textbookToast('上级教材项已修改，相关原值已保留，请核对标记项。', 'info');
    }
    return result;
}

function formatTextbookSyncTime(value) {
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) return '';
    return date.toLocaleString('zh-CN', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit',
    });
}

function renderSystemInputTextbookCatalogStatus() {
    const statusNode = $('system-input-textbook-catalog-status');
    const noteNode = $('system-input-textbook-catalog-note');
    const button = $('system-input-textbook-sync-btn');
    if (!statusNode && !noteNode && !button) return;
    const sync = systemInputTextbookCatalogSync || {};
    const catalog = systemInputTextbookCatalog || {};
    const status = String(sync.status || 'IDLE').toUpperCase();
    const persisted = textbookCatalogSyncMeta || readTextbookCatalogSyncMeta() || {};
    const catalogCount = Number(catalog.record_count) || (Array.isArray(catalog.records) ? catalog.records.length : 0);
    const syncCount = Number(sync.record_count) || 0;
    const count = Math.max(catalogCount, syncCount, Number(persisted.recordCount) || 0);
    const pathCount = catalogCount || count;
    const sourceTotalScopes = [
        catalog.source_total_scope,
        sync.source_total_scope,
        persisted.sourceTotalScope,
    ].map(value => textbookText(value));
    const resolvedSourceTotalScope = sourceTotalScopes.includes('source_books')
        ? 'source_books'
        : sourceTotalScopes.includes('expanded_paths')
            ? 'expanded_paths'
            : 'expanded_paths';
    const sourceTotalValues = [
        Number(catalog.source_total) || 0,
        Number(sync.source_total) || 0,
        Number(persisted.sourceTotal) || 0,
    ];
    const sourceTotal = resolvedSourceTotalScope === 'source_books'
        ? (sourceTotalValues.some(value => value > 0) ? Math.max(...sourceTotalValues) : count)
        : Math.max(...sourceTotalValues, count);
    const catalogRecords = Array.isArray(catalog.records)
        ? catalog.records.filter(record => record && typeof record === 'object')
        : [];
    const missingFields = TEXTBOOK_DIRECTORY_FIELDS
        .filter(({ key }) => !catalogRecords.some(record => textbookRecordChoice(record, key)))
        .map(({ label }) => label);
    const hasCoverageGap = resolvedSourceTotalScope !== 'source_books' && pathCount > 0 && sourceTotal > pathCount;
    const syncedAt = formatTextbookSyncTime(
        (catalogCount > 0 ? catalog.synced_at : '')
        || (syncCount > 0 ? sync.synced_at : '')
        || (status === 'SUCCEEDED' && syncCount > 0 ? sync.finished_at : '')
        || persisted.syncedAt,
    );
    const running = status === 'RUNNING';
    const failed = status === 'FAILED';
    if (statusNode) {
        statusNode.classList.toggle('is-running', running);
        statusNode.classList.toggle('is-ready', !running && !failed && count > 0);
        statusNode.classList.toggle('is-error', failed);
        statusNode.textContent = running
            ? '同步中'
            : failed
                ? '同步失败'
                : count > 0
                    ? resolvedSourceTotalScope === 'source_books'
                        ? `已加载 ${sourceTotal} 本教材 · ${pathCount} 条路径`
                        : hasCoverageGap
                            ? `已加载 ${catalogCount} / ${sourceTotal} 条`
                            : `已同步 ${count} 条`
                    : '尚未同步';
    }
    if (noteNode) {
        const lastSync = syncedAt ? `上次同步时间：${syncedAt}` : '';
        noteNode.textContent = running
            ? [lastSync, '正在打开教材管理页面；如需登录，请在浏览器窗口完成登录。'].filter(Boolean).join('；')
            : failed
                ? [lastSync, `本次同步失败：${textbookText(sync.message) || '请重试。'}`].filter(Boolean).join('；')
                : count > 0
                    ? [
                        lastSync || '上次同步时间：刚刚',
                        resolvedSourceTotalScope === 'source_books'
                            ? `平台返回 ${sourceTotal} 本教材，展开为 ${pathCount} 条可选路径${missingFields.length ? `；未返回字段：${missingFields.join('、')}` : ''}`
                            : hasCoverageGap
                            ? `平台返回 ${sourceTotal} 条，当前目录可用 ${catalogCount} 条${missingFields.length ? `；未返回字段：${missingFields.join('、')}` : ''}`
                            : '目录更新频率低，可按需手动刷新。',
                    ].join('；')
                    : '首次使用请手动同步教材列表，系统不会自动读取平台数据。';
    }
    if (button) {
        button.disabled = running || systemInputTextbookCatalogSyncBusy;
        button.textContent = running || systemInputTextbookCatalogSyncBusy
            ? '同步中…'
            : count > 0
                ? '重新同步'
                : '同步教材列表';
        button.setAttribute('aria-busy', running ? 'true' : 'false');
    }
}

function applySystemInputTextbookCatalogResponse(response) {
    if (response?.catalog && typeof response.catalog === 'object') {
        systemInputTextbookCatalog = response.catalog;
    }
    if (response?.sync && typeof response.sync === 'object') {
        systemInputTextbookCatalogSync = { ...systemInputTextbookCatalogSync, ...response.sync };
    }
    persistTextbookCatalogSyncMeta(systemInputTextbookCatalog, systemInputTextbookCatalogSync);
    renderSystemInputTextbookOptions();
    renderSystemInputTextbookCatalogStatus();
    root.targetEditorRefreshAfterDataLoad?.();
}

function stopSystemInputTextbookCatalogPolling() {
    if (systemInputTextbookCatalogPollTimer) {
        clearTimeout(systemInputTextbookCatalogPollTimer);
        systemInputTextbookCatalogPollTimer = null;
    }
}

function scheduleSystemInputTextbookCatalogPoll(syncId, requestId, delay = 900) {
    stopSystemInputTextbookCatalogPolling();
    systemInputTextbookCatalogPollTimer = setTimeout(() => {
        void pollSystemInputTextbookCatalogSync(syncId, requestId);
    }, delay);
}

async function pollSystemInputTextbookCatalogSync(syncId, requestId) {
    if (!workflowApi?.getTextbookCatalogSync || requestId !== systemInputTextbookCatalogRequestId) return;
    try {
        const response = await workflowApi.getTextbookCatalogSync(syncId);
        if (requestId !== systemInputTextbookCatalogRequestId) return;
        applySystemInputTextbookCatalogResponse(response);
        const status = String(systemInputTextbookCatalogSync?.status || '').toUpperCase();
        if (status === 'RUNNING') {
            scheduleSystemInputTextbookCatalogPoll(syncId, requestId);
            return;
        }
        systemInputTextbookCatalogSyncBusy = false;
        renderSystemInputTextbookCatalogStatus();
        if (status === 'SUCCEEDED') {
            textbookToast(systemInputTextbookCatalogSync.message || '教材目录同步完成', 'success');
        } else if (status === 'FAILED') {
            textbookToast(systemInputTextbookCatalogSync.message || '教材目录同步失败，请重试', 'warning');
        }
    } catch (error) {
        if (requestId !== systemInputTextbookCatalogRequestId) return;
        systemInputTextbookCatalogSyncBusy = false;
        systemInputTextbookCatalogSync = {
            ...systemInputTextbookCatalogSync,
            status: 'FAILED',
            message: textbookText(error?.message) || '教材目录同步状态读取失败，请重试。',
        };
        renderSystemInputTextbookCatalogStatus();
        textbookToast(systemInputTextbookCatalogSync.message, 'warning');
    }
}

async function loadSystemInputTextbookCatalog({ force = false } = {}) {
    if (!workflowApi?.getTextbookCatalog) return;
    if (!force && systemInputTextbookCatalog) {
        renderSystemInputTextbookOptions();
        renderSystemInputTextbookCatalogStatus();
        return;
    }
    const requestId = ++systemInputTextbookCatalogRequestId;
    try {
        const response = await workflowApi.getTextbookCatalog();
        if (requestId !== systemInputTextbookCatalogRequestId) return;
        applySystemInputTextbookCatalogResponse(response);
        const syncId = systemInputTextbookCatalogSync?.sync_id;
        if (String(systemInputTextbookCatalogSync?.status || '').toUpperCase() === 'RUNNING' && syncId) {
            scheduleSystemInputTextbookCatalogPoll(syncId, requestId);
        }
    } catch (_) {
        // The picker remains usable with saved/custom values when the cache
        // endpoint is unavailable; syncing is still available on retry.
        renderSystemInputTextbookCatalogStatus();
    }
}

async function startSystemInputTextbookCatalogSync() {
    if (!workflowApi?.startTextbookCatalogSync || systemInputTextbookCatalogSyncBusy) return;
    systemInputTextbookCatalogSyncBusy = true;
    const requestId = ++systemInputTextbookCatalogRequestId;
    systemInputTextbookCatalogSync = {
        ...systemInputTextbookCatalogSync,
        status: 'RUNNING',
        message: '正在打开教材管理页面；如果需要登录，请在浏览器窗口完成登录。',
    };
    renderSystemInputTextbookCatalogStatus();
    try {
        const response = await workflowApi.startTextbookCatalogSync();
        if (requestId !== systemInputTextbookCatalogRequestId) return;
        applySystemInputTextbookCatalogResponse(response);
        const syncId = systemInputTextbookCatalogSync?.sync_id;
        if (!syncId) throw new Error('同步任务未返回任务编号');
        scheduleSystemInputTextbookCatalogPoll(syncId, requestId, 350);
    } catch (error) {
        if (requestId !== systemInputTextbookCatalogRequestId) return;
        systemInputTextbookCatalogSyncBusy = false;
        systemInputTextbookCatalogSync = {
            ...systemInputTextbookCatalogSync,
            status: 'FAILED',
            message: textbookText(error?.message) || '教材目录同步启动失败，请重试。',
        };
        renderSystemInputTextbookCatalogStatus();
        textbookToast(systemInputTextbookCatalogSync.message, 'warning');
    }
}

registerRendererModule('systemInput.textbookCatalog', {
    systemInputTextbookCatalogAssessment,
    systemInputReconcileTextbookDirectory,
    renderSystemInputTextbookOptions,
    handleSystemInputTextbookFieldChange,
    renderSystemInputTextbookCatalogStatus,
    applySystemInputTextbookCatalogResponse,
    loadSystemInputTextbookCatalog,
    startSystemInputTextbookCatalogSync,
    readTextbookCatalogSyncMeta,
    persistTextbookCatalogSyncMeta,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
