/** Pure configuration operations for the recording-target editor. */
(function attachRendererFeature_systemInput_batchModel(root) {
    'use strict';

const BATCH_FIELDS = Object.freeze({
    paper: Object.freeze([
        'paperName', 'provinceId', 'cityId', 'districtIds', 'stageId', 'gradeId', 'year',
        'paperCategory', 'paperType', 'platformTemplateName', 'platformTemplateId',
        'platformTemplateVersion', 'answerTimeMinutes',
    ]),
    textbook: Object.freeze([
        'textbookNameZh', 'textbookNameEn', 'textbookVersion', 'textbookStage',
        // Keep the directory cascade contiguous. `textbookForm` is an
        // independent page-form value and belongs after the unit/lesson path
        // in both the detail and batch editors.
        'textbookGrade', 'textbookVolume', 'textbookUnit', 'textbookLesson', 'textbookForm',
    ]),
});
const BATCH_INDIVIDUAL_FIELDS = new Set([
    'paperName', 'textbookNameZh', 'textbookNameEn', 'textbookUnit', 'textbookLesson',
]);
const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

function batchClone(value) {
    if (Array.isArray(value)) return value.map(batchClone);
    if (value && typeof value === 'object') {
        return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, batchClone(item)]));
    }
    return value;
}

function batchAliases(field) {
    if (field === 'districtIds') return ['district_ids', 'district_id'];
    const snake = field.replace(/[A-Z]/g, character => `_${character.toLowerCase()}`);
    return snake === field ? [] : [snake];
}

function systemInputBatchValue(configuration, field) {
    if (!configuration || typeof configuration !== 'object') return undefined;
    if (hasOwn(configuration, field)) return configuration[field];
    const alias = batchAliases(field).find(key => hasOwn(configuration, key));
    return alias ? configuration[alias] : undefined;
}

function systemInputBatchIsEmpty(value) {
    if (value === undefined || value === null) return true;
    if (typeof value === 'string') return !value.trim();
    if (Array.isArray(value)) return !value.length || value.every(systemInputBatchIsEmpty);
    if (typeof value === 'object') {
        return ['id', 'value', 'name', 'label', 'text'].every(key => systemInputBatchIsEmpty(value[key]));
    }
    return false;
}

function batchValueKey(value) {
    if (Array.isArray(value)) return JSON.stringify(value.map(batchValueKey).sort());
    if (value && typeof value === 'object') {
        const identifier = value.id ?? value.value;
        if (!systemInputBatchIsEmpty(identifier)) return `choice:${String(identifier)}`;
        return `choice:${String(value.name ?? value.label ?? value.text ?? '').trim()}`;
    }
    if (systemInputBatchIsEmpty(value)) return 'empty';
    return `value:${String(value).trim()}`;
}

function systemInputBatchFields(inputType = 'paper', { commonOnly = false } = {}) {
    return (BATCH_FIELDS[inputType] || []).filter(field => !commonOnly || !BATCH_INDIVIDUAL_FIELDS.has(field));
}

// Only platform choice data is accepted within values. Payloads from parsed
// content, answers and audio must never hitch a ride in a batch configuration.
function batchSafeValue(value) {
    if (Array.isArray(value)) return value.map(batchSafeValue).filter(item => item !== undefined);
    if (value && typeof value === 'object') {
        return Object.fromEntries(['id', 'value', 'name', 'label', 'text']
            .filter(key => hasOwn(value, key) && ['string', 'number', 'boolean'].includes(typeof value[key]))
            .map(key => [key, value[key]]));
    }
    return value === null || ['string', 'number', 'boolean', 'undefined'].includes(typeof value)
        ? value
        : undefined;
}

function batchWrite(configuration, field, value) {
    batchAliases(field).forEach(alias => delete configuration[alias]);
    if (value === undefined) delete configuration[field];
    else configuration[field] = batchClone(value);
}

function batchCascadeValues(configuration, inputType) {
    return Object.fromEntries(systemInputCascadeFields(inputType).map(field => [
        field,
        systemInputBatchValue(configuration, field),
    ]));
}

function batchCompatibility(field, value, next, previous, options, writtenFields) {
    const parents = systemInputCascadeParents(options.inputType, field);
    const cascadeValues = batchCascadeValues(next, options.inputType);
    // A child cannot be valid without its parent, even before catalog data loads.
    if (!systemInputCascadeAvailable(options.inputType, field, cascadeValues)
        || !systemInputCascadeParentsReady(options.inputType, field, cascadeValues)) return false;
    if (typeof options.compatible === 'function') {
        const result = options.compatible(field, value, next, previous);
        if (result === true || result === false) return result;
    }
    if (field === 'platformTemplateId' || field === 'platformTemplateVersion') {
        // A reference copied together is coherent; an old reference under a
        // newly selected template name must be cleared rather than submitted.
        return writtenFields.has(field);
    }
    return undefined;
}

function batchDependencyGroup(inputType, field) {
    const group = new Set([field]);
    const dependencies = systemInputCascadeDependencyEntries(inputType);
    let expanded = true;
    while (expanded) {
        expanded = false;
        dependencies.forEach(([child, parents]) => {
            if (![child, ...parents].some(key => group.has(key))) return;
            [child, ...parents].forEach(key => {
                if (!group.has(key)) { group.add(key); expanded = true; }
            });
        });
    }
    return [...group];
}

function batchDiff(previous, next, fields, reasons = {}, inputType = 'paper') {
    const unitId = String(previous.unit_id ?? '');
    return fields.flatMap(field => {
        const beforePresent = hasOwn(previous, field);
        const afterPresent = hasOwn(next, field);
        const aliasesBefore = Object.fromEntries(batchAliases(field)
            .filter(alias => hasOwn(previous, alias)).map(alias => [alias, batchClone(previous[alias])]));
        const aliasesAfter = Object.fromEntries(batchAliases(field)
            .filter(alias => hasOwn(next, alias)).map(alias => [alias, batchClone(next[alias])]));
        if (beforePresent === afterPresent
            && JSON.stringify(previous[field]) === JSON.stringify(next[field])
            && JSON.stringify(aliasesBefore) === JSON.stringify(aliasesAfter)) return [];
        return [{
            unitId, field,
            before: batchClone(systemInputBatchValue(previous, field)),
            after: batchClone(systemInputBatchValue(next, field)),
            beforePresent, afterPresent, aliasesBefore, aliasesAfter,
            reason: reasons[field] || 'batch',
            dependencyAfter: Object.fromEntries(batchDependencyGroup(inputType, field)
                .filter(key => key !== field)
                .map(key => [key, batchClone(systemInputBatchValue(next, key))])),
        }];
    });
}

function batchTransform(configurations, options, shouldWrite) {
    const inputType = options.inputType || 'paper';
    const allFields = systemInputBatchFields(inputType);
    const permitted = new Set(allFields);
    const fields = [...new Set(options.fields || [])].filter(field => permitted.has(field));
    const selected = new Set((options.unitIds || []).map(String));
    const values = options.values && typeof options.values === 'object' ? options.values : {};
    const changes = [];
    const review = [];
    const nextConfigurations = (Array.isArray(configurations) ? configurations : []).map(previous => {
        if (!previous || typeof previous !== 'object' || Array.isArray(previous)) return previous;
        const next = batchClone(previous);
        const unitId = String(previous.unit_id ?? '');
        if (!unitId || !selected.has(unitId)) return next;
        const reasons = {};
        const writtenFields = new Set();
        fields.forEach(field => {
            if (!hasOwn(values, field) || !shouldWrite(previous, field)) return;
            const value = batchSafeValue(values[field]);
            // Fill-empty and common settings never spread empty draft values.
            if (options.mode !== 'overwrite' && systemInputBatchIsEmpty(value)) return;
            writtenFields.add(field);
            if (batchValueKey(systemInputBatchValue(previous, field)) === batchValueKey(value)) return;
            batchWrite(next, field, value);
            reasons[field] = options.reason || 'batch';
        });
        systemInputCascadeDependencyEntries(inputType).forEach(([field, parents]) => {
            const changedParent = parents.find(parent => (
                batchValueKey(systemInputBatchValue(previous, parent)) !== batchValueKey(systemInputBatchValue(next, parent))
            ));
            const changedField = batchValueKey(systemInputBatchValue(previous, field)) !== batchValueKey(systemInputBatchValue(next, field));
            const value = systemInputBatchValue(next, field);
            if ((!changedParent && !changedField) || systemInputBatchIsEmpty(value)) return;
            const parentField = changedParent || parents[0];
            const compatible = batchCompatibility(field, value, next, previous, { ...options, inputType }, writtenFields);
            if (compatible === true) return;
            const reason = compatible === false ? 'incompatible-parent' : 'parent-changed';
            review.push({ unitId, field, previousValue: batchClone(value), parentField, reason });
            if (compatible === false) {
                batchWrite(next, field, undefined);
                reasons[field] = reason;
            }
        });
        changes.push(...batchDiff(previous, next, allFields, reasons, inputType));
        return next;
    });
    return {
        configurations: nextConfigurations,
        changes,
        updatedUnitIds: [...new Set(changes.map(change => change.unitId))],
        review,
    };
}

/** Apply only explicitly selected fields to explicitly selected unit IDs. */
function systemInputBatchApply(configurations, options = {}) {
    const mode = options.mode === 'overwrite' ? 'overwrite' : 'fill-empty';
    return batchTransform(configurations, { ...options, mode }, (previous, field) => (
        mode === 'overwrite' || systemInputBatchIsEmpty(systemInputBatchValue(previous, field))
    ));
}

/** Advance defaults while retaining independent per-unit edits. */
function systemInputBatchUpdateDefaults(configurations, options = {}) {
    const inputType = options.inputType || 'paper';
    const defaults = options.defaults || {};
    const previousDefaults = options.previousDefaults || {};
    const unitIds = options.unitIds || (Array.isArray(configurations) ? configurations : [])
        .filter(configuration => configuration && typeof configuration === 'object')
        .map(configuration => configuration.unit_id);
    return batchTransform(configurations, {
        ...options, inputType, unitIds,
        fields: systemInputBatchFields(inputType, { commonOnly: true }),
        values: defaults, mode: 'defaults', reason: 'default',
    }, (previous, field) => {
        const overrides = options.overrides?.[String(previous.unit_id)] || [];
        if (Array.isArray(overrides) ? overrides.includes(field) : overrides[field] === true) return false;
        const current = systemInputBatchValue(previous, field);
        return systemInputBatchIsEmpty(current)
            || (hasOwn(previousDefaults, field) && batchValueKey(current) === batchValueKey(previousDefaults[field]));
    });
}

/** Undo only values still owned by the batch, preserving subsequent edits. */
function systemInputBatchUndo(configurations, changes = []) {
    const pending = new Map();
    changes.forEach(change => {
        if (!change || ![...BATCH_FIELDS.paper, ...BATCH_FIELDS.textbook].includes(change.field)) return;
        const rows = pending.get(String(change.unitId)) || [];
        rows.push(change);
        pending.set(String(change.unitId), rows);
    });
    const reverted = [];
    const skipped = [];
    const nextConfigurations = (Array.isArray(configurations) ? configurations : []).map(previous => {
        if (!previous || typeof previous !== 'object' || Array.isArray(previous)) return previous;
        const next = batchClone(previous);
        (pending.get(String(previous.unit_id ?? '')) || []).slice().reverse().forEach(change => {
            const aliases = Object.fromEntries(batchAliases(change.field)
                .filter(alias => hasOwn(next, alias)).map(alias => [alias, next[alias]]));
            if (hasOwn(next, change.field) !== change.afterPresent
                || JSON.stringify(systemInputBatchValue(next, change.field)) !== JSON.stringify(change.after)
                || JSON.stringify(aliases) !== JSON.stringify(change.aliasesAfter || {})
                || Object.entries(change.dependencyAfter || {}).some(([field, value]) => (
                    JSON.stringify(systemInputBatchValue(previous, field)) !== JSON.stringify(value)
                ))) {
                skipped.push(change);
                return;
            }
            batchWrite(next, change.field, change.beforePresent ? change.before : undefined);
            Object.entries(change.aliasesBefore || {}).forEach(([alias, value]) => {
                if (batchAliases(change.field).includes(alias)) next[alias] = batchClone(value);
            });
            reverted.push(change);
        });
        return next;
    });
    return {
        configurations: nextConfigurations,
        changes: reverted,
        updatedUnitIds: [...new Set(reverted.map(change => change.unitId))],
        skipped,
        review: [],
    };
}

registerRendererModule('systemInput.batchModel', {
    systemInputBatchFields,
    systemInputBatchValue,
    systemInputBatchIsEmpty,
    systemInputBatchApply,
    systemInputBatchUpdateDefaults,
    systemInputBatchUndo,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
