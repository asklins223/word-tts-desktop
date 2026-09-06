/** Shared cascade graph and field behavior for system-input surfaces. */
(function attachRendererFeature_systemInput_cascade(root) {
    'use strict';

const TEXTBOOK_DIRECTORY_FIELD_ALIASES = Object.freeze({
    version: 'textbookVersion',
    stage: 'textbookStage',
    grade: 'textbookGrade',
    volume: 'textbookVolume',
    unit: 'textbookUnit',
    lesson: 'textbookLesson',
});

const SYSTEM_INPUT_CASCADE_GRAPHS = Object.freeze({
    paper: Object.freeze({
        fields: Object.freeze([
            'provinceId', 'cityId', 'districtIds', 'stageId', 'gradeId',
            'paperCategory', 'paperType', 'platformTemplateName',
            'platformTemplateId', 'platformTemplateVersion',
        ]),
        parents: Object.freeze({
            provinceId: Object.freeze([]),
            cityId: Object.freeze(['provinceId']),
            districtIds: Object.freeze(['provinceId', 'cityId']),
            stageId: Object.freeze([]),
            gradeId: Object.freeze(['stageId']),
            paperCategory: Object.freeze([]),
            paperType: Object.freeze(['paperCategory']),
            // Both platform template kinds are region-scoped. Paper templates
            // additionally use stage/grade; that category-dependent part is
            // enforced by the platform-template catalogue assessor.
            platformTemplateName: Object.freeze(['paperCategory', 'provinceId', 'cityId']),
            platformTemplateId: Object.freeze(['platformTemplateName']),
            platformTemplateVersion: Object.freeze(['platformTemplateName', 'platformTemplateId']),
        }),
        emptyValues: Object.freeze({
            districtIds: Object.freeze([]),
        }),
        availability: Object.freeze({
            // Persisted configurations can contain object-shaped labels or
            // harmless whitespace. Availability must follow the same label
            // normalization as the option matcher, otherwise an old value
            // can be treated as disabled merely because its formatting
            // differs from the current page form.
            paperType: values => systemInputCascadeNormalizeValue(
                systemInputCascadeReadValue('paper', values, 'paperCategory'),
            )
                === systemInputCascadeNormalizeValue('听说考试'),
        }),
        compatibility: Object.freeze({
            cityId: Object.freeze({
                records: 'cities',
                parents: Object.freeze({ provinceId: 'parentId' }),
            }),
            districtIds: Object.freeze({
                records: 'districts',
                parents: Object.freeze({ cityId: 'parentId' }),
            }),
            gradeId: Object.freeze({
                records: 'grades',
                parents: Object.freeze({ stageId: 'stageId' }),
            }),
        }),
        hints: Object.freeze({
            paperType: '仅“听说考试”需要填写考试类型',
        }),
    }),
    textbook: Object.freeze({
        fields: Object.freeze([
            'textbookVersion', 'textbookStage', 'textbookGrade',
            'textbookVolume', 'textbookUnit', 'textbookLesson',
        ]),
        parents: Object.freeze({
            textbookVersion: Object.freeze([]),
            textbookStage: Object.freeze(['textbookVersion']),
            textbookGrade: Object.freeze(['textbookVersion', 'textbookStage']),
            textbookVolume: Object.freeze(['textbookVersion', 'textbookStage', 'textbookGrade']),
            textbookUnit: Object.freeze(['textbookVersion', 'textbookStage', 'textbookGrade', 'textbookVolume']),
            textbookLesson: Object.freeze(['textbookVersion', 'textbookStage', 'textbookGrade', 'textbookVolume', 'textbookUnit']),
        }),
        emptyValues: Object.freeze({}),
        availability: Object.freeze({}),
        hints: Object.freeze({}),
    }),
});

const EMPTY_CASCADE_GRAPH = Object.freeze({
    fields: Object.freeze([]),
    parents: Object.freeze({}),
    emptyValues: Object.freeze({}),
    availability: Object.freeze({}),
    compatibility: Object.freeze({}),
    hints: Object.freeze({}),
});

function systemInputCascadeDisplayValue(value, fallback = '') {
    if (typeof systemInputDisplayValue === 'function') {
        return systemInputDisplayValue(value, fallback);
    }
    if (value && typeof value === 'object' && !Array.isArray(value)) {
        for (const key of ['name', 'label', 'text', 'value', 'id']) {
            const candidate = String(value[key] ?? '').trim();
            if (candidate) return candidate;
        }
        return fallback;
    }
    return String(value ?? '').trim() || fallback;
}

function systemInputCascadeNormalizeValue(value) {
    if (typeof systemInputNormalizeLabel === 'function') {
        return systemInputNormalizeLabel(systemInputCascadeDisplayValue(value));
    }
    return systemInputCascadeDisplayValue(value).replace(/\s+/g, '').toLocaleLowerCase('zh-CN');
}

function systemInputCascadeValuePresent(value) {
    if (Array.isArray(value)) return value.length > 0 && value.some(systemInputCascadeValuePresent);
    if (value && typeof value === 'object') {
        return ['id', 'value', 'name', 'label', 'text'].some(key => (
            Object.prototype.hasOwnProperty.call(value, key)
            && systemInputCascadeValuePresent(value[key])
        ));
    }
    if (value === undefined || value === null) return false;
    return String(value).trim() !== '';
}

function systemInputCascadeChoiceMatches(value, candidate) {
    const valueIsObject = value && typeof value === 'object' && !Array.isArray(value);
    const candidateIsObject = candidate && typeof candidate === 'object' && !Array.isArray(candidate);
    const valueId = valueIsObject ? (value.id ?? value.value) : undefined;
    const candidateId = candidateIsObject ? (candidate.id ?? candidate.value) : undefined;
    if (systemInputCascadeValuePresent(valueId) && systemInputCascadeValuePresent(candidateId)) {
        return String(valueId) === String(candidateId);
    }
    if (valueIsObject && systemInputCascadeValuePresent(valueId) && !candidateIsObject) {
        if (String(valueId) === String(candidate)) return true;
    }
    if (!valueIsObject && candidateIsObject && systemInputCascadeValuePresent(candidateId)) {
        if (String(value) === String(candidateId)) return true;
    }
    const valueLabel = systemInputCascadeNormalizeValue(value);
    const candidateLabel = systemInputCascadeNormalizeValue(candidate);
    return Boolean(valueLabel && candidateLabel && valueLabel === candidateLabel);
}

function systemInputCascadeInputType(inputType = 'paper') {
    const normalized = String(inputType || 'paper').trim();
    return Object.prototype.hasOwnProperty.call(SYSTEM_INPUT_CASCADE_GRAPHS, normalized)
        ? normalized
        : 'paper';
}

function systemInputCascadeGraph(inputType = 'paper') {
    return SYSTEM_INPUT_CASCADE_GRAPHS[systemInputCascadeInputType(inputType)] || EMPTY_CASCADE_GRAPH;
}

function systemInputCascadeFields(inputType = 'paper') {
    return [...systemInputCascadeGraph(inputType).fields];
}

function systemInputCascadeDependencies(inputType = 'paper') {
    return systemInputCascadeGraph(inputType).parents;
}

function systemInputCascadeDependencyEntries(inputType = 'paper') {
    return Object.entries(systemInputCascadeDependencies(inputType))
        .filter(([, parents]) => parents.length);
}

function systemInputCascadeFieldIsKnown(inputType, field) {
    const canonical = systemInputCascadeCanonicalField(inputType, field);
    return systemInputCascadeGraph(inputType).fields.includes(canonical);
}

function systemInputCascadeParents(inputType, field) {
    const canonical = systemInputCascadeCanonicalField(inputType, field);
    return [...(systemInputCascadeDependencies(inputType)[canonical] || [])];
}

function systemInputCascadeChildren(inputType, field) {
    const canonical = systemInputCascadeCanonicalField(inputType, field);
    return systemInputCascadeFields(inputType).filter(candidate => (
        systemInputCascadeParents(inputType, candidate).includes(canonical)
    ));
}

function systemInputCascadeDescendants(inputType, field) {
    const descendants = [];
    const seen = new Set();
    const queue = systemInputCascadeChildren(inputType, field);
    while (queue.length) {
        const child = queue.shift();
        if (seen.has(child)) continue;
        seen.add(child);
        descendants.push(child);
        queue.push(...systemInputCascadeChildren(inputType, child));
    }
    return descendants;
}

function systemInputCascadeCanonicalField(inputType, field) {
    const raw = String(field ?? '');
    const localAliases = inputType === 'textbook' ? TEXTBOOK_DIRECTORY_FIELD_ALIASES : {};
    if (localAliases[raw]) return localAliases[raw];
    const graphFields = systemInputCascadeGraph(inputType).fields;
    return graphFields.find(candidate => {
        const snake = candidate.replace(/[A-Z]/g, character => `_${character.toLowerCase()}`);
        return raw === snake || (candidate === 'districtIds' && raw === 'district_id');
    }) || raw;
}

function systemInputCascadeFieldAliases(inputType, field) {
    const canonical = systemInputCascadeCanonicalField(inputType, field);
    if (!systemInputCascadeFieldIsKnown(inputType, canonical)) return [];
    const snake = canonical.replace(/[A-Z]/g, character => `_${character.toLowerCase()}`);
    const aliases = snake === canonical ? [] : [snake];
    if (canonical === 'districtIds') aliases.push('district_id');
    return [...new Set(aliases)];
}

function systemInputCascadeReadValue(inputType, values = {}, field) {
    if (!values || typeof values !== 'object') return undefined;
    const canonical = systemInputCascadeCanonicalField(inputType, field);
    const hasOwn = key => Object.prototype.hasOwnProperty.call(values, key);
    if (hasOwn(canonical) && systemInputCascadeValuePresent(values[canonical])) return values[canonical];
    for (const alias of systemInputCascadeFieldAliases(inputType, canonical)) {
        if (hasOwn(alias) && systemInputCascadeValuePresent(values[alias])) return values[alias];
    }
    return hasOwn(canonical) ? values[canonical] : undefined;
}

function systemInputCascadeFieldSelected(inputType, selectedFields, field) {
    if (!selectedFields) return false;
    const has = typeof selectedFields.has === 'function'
        ? key => selectedFields.has(key)
        : key => Array.isArray(selectedFields) && selectedFields.includes(key);
    const canonical = systemInputCascadeCanonicalField(inputType, field);
    return has(canonical) || systemInputCascadeFieldAliases(inputType, canonical).some(has);
}

function systemInputCascadeLocalField(inputType, field) {
    const canonical = systemInputCascadeCanonicalField(inputType, field);
    if (inputType !== 'textbook') return canonical;
    return Object.entries(TEXTBOOK_DIRECTORY_FIELD_ALIASES)
        .find(([, candidate]) => candidate === canonical)?.[0] || canonical;
}

function systemInputCascadeAvailable(inputType, field, values = {}) {
    const canonical = systemInputCascadeCanonicalField(inputType, field);
    const predicate = systemInputCascadeGraph(inputType).availability?.[canonical];
    return typeof predicate === 'function' ? Boolean(predicate(values)) : true;
}

/**
 * Validate a selected choice against the loaded records for every declared
 * parent. The graph owns the relationship metadata; callers only provide the
 * current catalog/index arrays. An unavailable catalog returns undefined so
 * a transient data load cannot turn a valid saved value into a false error.
 */
function systemInputCascadeCompatibility(inputType, field, value, values = {}, sources = {}) {
    const canonical = systemInputCascadeCanonicalField(inputType, field);
    const rule = systemInputCascadeGraph(inputType).compatibility?.[canonical];
    if (!rule) return undefined;
    const records = Array.isArray(sources?.[rule.records])
        ? sources[rule.records].filter(record => record && typeof record === 'object')
        : [];
    if (!records.length) return undefined;
    const choices = Array.isArray(value) ? value : [value];
    if (!choices.length) return true;
    const valueKey = rule.valueKey || 'id';
    const labelKey = rule.labelKey || 'name';
    return choices.every(choice => records.some(record => {
        const recordChoice = {
            id: record[valueKey],
            name: record[labelKey],
        };
        if (!systemInputCascadeChoiceMatches(choice, recordChoice)) return false;
        return Object.entries(rule.parents || {}).every(([parent, recordKey]) => {
            if (!systemInputCascadeChoiceMatches(
                systemInputCascadeReadValue(inputType, values, parent),
                record[recordKey],
            )) return false;
            // A descendant is invalid when one of its own parents is already
            // attached to the wrong ancestor. This matters for a restored
            // province/city/district triple: checking district -> city alone
            // would incorrectly accept a district from an incompatible city.
            const parentResult = systemInputCascadeCompatibility(
                inputType,
                parent,
                systemInputCascadeReadValue(inputType, values, parent),
                values,
                sources,
            );
            return parentResult !== false;
        });
    }));
}

function systemInputCascadeParentsReady(inputType, field, values = {}) {
    if (!systemInputCascadeFieldIsKnown(inputType, field)) return true;
    const visiting = new Set();
    const visited = new Set();
    const visit = current => {
        if (visited.has(current)) return true;
        if (visiting.has(current)) return false;
        visiting.add(current);
        if (!systemInputCascadeAvailable(inputType, current, values)) {
            visiting.delete(current);
            return false;
        }
        const ready = systemInputCascadeParents(inputType, current).every(parent => (
            systemInputCascadeValuePresent(systemInputCascadeReadValue(inputType, values, parent))
            && visit(parent)
        ));
        visiting.delete(current);
        if (ready) visited.add(current);
        return ready;
    };
    return visit(field);
}

function systemInputCascadeBatchReady(inputType, field, values = {}, selectedFields = new Set()) {
    if (!systemInputCascadeFieldIsKnown(inputType, field)) return true;
    if (!systemInputCascadeAvailable(inputType, field, values)) return false;
    const selected = selectedFields && typeof selectedFields.has === 'function'
        ? selectedFields
        : new Set(Array.isArray(selectedFields) ? selectedFields : []);
    return systemInputCascadeParents(inputType, field).every(parent => (
        systemInputCascadeFieldSelected(inputType, selected, parent)
        && systemInputCascadeValuePresent(systemInputCascadeReadValue(inputType, values, parent))
        && systemInputCascadeParentsReady(inputType, parent, values)
        && systemInputCascadeBatchReady(inputType, parent, values, selected)
    ));
}

function systemInputCascadeFirstMissingParent(inputType, field, values = {}, selectedFields = null) {
    const selected = selectedFields === null
        ? null
        : selectedFields && typeof selectedFields.has === 'function'
            ? selectedFields
            : new Set(Array.isArray(selectedFields) ? selectedFields : []);
    return systemInputCascadeParents(inputType, field).find(parent => (
        (selected && !systemInputCascadeFieldSelected(inputType, selected, parent))
        || !systemInputCascadeValuePresent(systemInputCascadeReadValue(inputType, values, parent))
        || !systemInputCascadeParentsReady(inputType, parent, values)
        || (selected && !systemInputCascadeBatchReady(inputType, parent, values, selected))
    )) || '';
}

function systemInputCascadeHint(inputType, field, values = {}, {
    selectedFields = null,
    labelFor = key => key,
} = {}) {
    const canonical = systemInputCascadeCanonicalField(inputType, field);
    if (!systemInputCascadeFieldIsKnown(inputType, canonical)) return '';
    if (!systemInputCascadeAvailable(inputType, canonical, values)) {
        return systemInputCascadeGraph(inputType).hints?.[canonical] || '';
    }
    const ready = selectedFields === null
        ? systemInputCascadeParentsReady(inputType, canonical, values)
        : systemInputCascadeBatchReady(inputType, canonical, values, selectedFields);
    if (ready) return '';
    const missing = systemInputCascadeFirstMissingParent(inputType, canonical, values, selectedFields);
    if (!missing) return '';
    if (selectedFields === null) return '先选择' + labelFor(missing);
    const selected = selectedFields && typeof selectedFields.has === 'function'
        ? selectedFields
        : new Set(Array.isArray(selectedFields) ? selectedFields : []);
    return systemInputCascadeFieldSelected(inputType, selected, missing)
        && systemInputCascadeValuePresent(systemInputCascadeReadValue(inputType, values, missing))
        && systemInputCascadeParentsReady(inputType, missing, values)
        ? '先选择' + labelFor(missing)
        : '先勾选并选择' + labelFor(missing);
}

function systemInputCascadeEmptyValue(inputType, field) {
    const canonical = systemInputCascadeCanonicalField(inputType, field);
    const configured = systemInputCascadeGraph(inputType).emptyValues?.[canonical];
    return Array.isArray(configured) ? [] : (configured ?? '');
}

/**
 * Clear every descendant of a changed parent, not only its direct child.
 * The selected-field set is optional and is used by the batch UI to prevent
 * an old child checkbox from applying an implicit empty value.
 */
function systemInputCascadeResetDescendants(inputType, values = {}, field, {
    selectedFields = null,
    removeEmpty = false,
} = {}) {
    const canonical = systemInputCascadeCanonicalField(inputType, field);
    systemInputCascadeFieldAliases(inputType, canonical).forEach(alias => delete values[alias]);
    const descendants = systemInputCascadeDescendants(inputType, canonical);
    descendants.forEach(child => {
        systemInputCascadeFieldAliases(inputType, child).forEach(alias => delete values[alias]);
        if (removeEmpty) delete values[child];
        else values[child] = systemInputCascadeEmptyValue(inputType, child);
        selectedFields?.delete?.(child);
        systemInputCascadeFieldAliases(inputType, child).forEach(alias => selectedFields?.delete?.(alias));
    });
    return descendants;
}

/**
 * Clear descendants that were not explicitly supplied by a compound update
 * such as applying a saved template. This keeps partial updates from leaving
 * an old child attached to a newly selected parent while still allowing a
 * complete parent/child path to arrive together.
 */
function systemInputCascadeResetUnselectedDescendants(
    inputType,
    values = {},
    changedFields = [],
    { selectedFields = null, removeEmpty = false } = {},
) {
    const changed = new Set(
        (changedFields && typeof changedFields[Symbol.iterator] === 'function'
            ? [...changedFields]
            : [])
            .map(field => systemInputCascadeCanonicalField(inputType, field)),
    );
    changed.forEach(field => systemInputCascadeFieldAliases(inputType, field).forEach(alias => delete values[alias]));
    const descendants = [...new Set(
        [...changed].flatMap(field => systemInputCascadeDescendants(inputType, field)),
    )];
    descendants.forEach(child => {
        if (changed.has(child)) return;
        systemInputCascadeFieldAliases(inputType, child).forEach(alias => delete values[alias]);
        if (removeEmpty) delete values[child];
        else values[child] = systemInputCascadeEmptyValue(inputType, child);
        selectedFields?.delete?.(child);
        systemInputCascadeFieldAliases(inputType, child).forEach(alias => selectedFields?.delete?.(alias));
    });
    return descendants;
}

/**
 * Build one consistent list of catalog-backed child options. The caller
 * supplies only the record shape; parent readiness, filtering, deduplication
 * and preservation of a saved/manual value stay shared.
 */
function systemInputCascadeCatalogOptions({
    inputType = 'textbook',
    field,
    values = {},
    records = [],
    fallback = [],
    getRecordValue = (record, key) => record?.[key],
    getRecordDetail = () => '',
    currentValue,
    currentDetail = '',
    preserveCurrent = true,
} = {}) {
    const canonicalField = systemInputCascadeCanonicalField(inputType, field);
    const resolvedCurrentValue = currentValue === undefined
        ? systemInputCascadeReadValue(inputType, values, canonicalField)
        : currentValue;
    const parents = systemInputCascadeParents(inputType, canonicalField);
    const sourceRecords = Array.isArray(records)
        ? records.filter(record => record && typeof record === 'object')
        : [];
    const options = [];
    const seen = new Set();
    const append = (raw, detail = '', source = null) => {
        const label = systemInputCascadeDisplayValue(raw);
        const normalized = systemInputCascadeNormalizeValue(label);
        if (!normalized || seen.has(normalized)) return;
        seen.add(normalized);
        options.push({
            value: label,
            label,
            detail: String(detail || '').trim(),
            raw: source || raw,
        });
    };
    const parentsReady = systemInputCascadeParentsReady(inputType, canonicalField, values);
    if (parents.length && !parentsReady) {
        if (systemInputCascadeValuePresent(resolvedCurrentValue)) {
            append(
                resolvedCurrentValue,
                currentDetail || '当前已保存值；先选择上级后可重新选择',
                { name: systemInputCascadeDisplayValue(resolvedCurrentValue) },
            );
        }
        return options;
    }
    if (sourceRecords.length) {
        sourceRecords.forEach(record => {
            const matches = parents.every(parent => (
                systemInputCascadeChoiceMatches(
                    systemInputCascadeReadValue(inputType, values, parent),
                    getRecordValue(record, parent),
                )
            ));
            if (matches) append(
                getRecordValue(record, canonicalField),
                getRecordDetail(record, canonicalField),
                record,
            );
        });
    } else {
        (Array.isArray(fallback) ? fallback : []).forEach(choice => append(choice));
    }
    if (preserveCurrent && systemInputCascadeValuePresent(resolvedCurrentValue)) {
        const currentLabel = systemInputCascadeDisplayValue(resolvedCurrentValue);
        const currentKey = systemInputCascadeNormalizeValue(currentLabel);
        if (!seen.has(currentKey)) {
            options.unshift({
                value: currentLabel,
                label: currentLabel,
                detail: String(currentDetail || '').trim(),
                raw: { name: currentLabel },
            });
        }
    }
    return options;
}

registerRendererModule('systemInput.cascade', {
    TEXTBOOK_DIRECTORY_FIELD_ALIASES,
    SYSTEM_INPUT_CASCADE_GRAPHS,
    systemInputCascadeDisplayValue,
    systemInputCascadeNormalizeValue,
    systemInputCascadeValuePresent,
    systemInputCascadeChoiceMatches,
    systemInputCascadeFieldSelected,
    systemInputCascadeReadValue,
    systemInputCascadeGraph,
    systemInputCascadeFields,
    systemInputCascadeDependencies,
    systemInputCascadeDependencyEntries,
    systemInputCascadeFieldIsKnown,
    systemInputCascadeParents,
    systemInputCascadeChildren,
    systemInputCascadeDescendants,
    systemInputCascadeCanonicalField,
    systemInputCascadeFieldAliases,
    systemInputCascadeLocalField,
    systemInputCascadeAvailable,
    systemInputCascadeCompatibility,
    systemInputCascadeParentsReady,
    systemInputCascadeBatchReady,
    systemInputCascadeFirstMissingParent,
    systemInputCascadeHint,
    systemInputCascadeEmptyValue,
    systemInputCascadeResetDescendants,
    systemInputCascadeResetUnselectedDescendants,
    systemInputCascadeCatalogOptions,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
