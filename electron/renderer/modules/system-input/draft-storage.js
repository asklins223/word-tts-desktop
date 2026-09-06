/** Local, incomplete target drafts. This module never saves or starts a workflow. */
(function attachRendererFeature_systemInput_draftStorage(root) {
    'use strict';

const SYSTEM_INPUT_DRAFT_SCHEMA = 1;
const SYSTEM_INPUT_DRAFT_PREFIX = 'wordtts.system-input-draft.v1:';
const SYSTEM_INPUT_DRAFT_TTL = 14 * 24 * 60 * 60 * 1000;
const SYSTEM_INPUT_DRAFT_MAX_BYTES = 384 * 1024;
const SYSTEM_INPUT_DRAFT_TOTAL_BYTES = 2 * 1024 * 1024;
const SYSTEM_INPUT_DRAFT_MAX_COUNT = 12;

function systemInputDraftStableJson(value) {
    if (Array.isArray(value)) return `[${value.map(systemInputDraftStableJson).join(',')}]`;
    if (value && typeof value === 'object') {
        return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${systemInputDraftStableJson(value[key])}`).join(',')}}`;
    }
    return JSON.stringify(value ?? null);
}

function systemInputDraftFingerprint(value) {
    const source = typeof value === 'string' ? value : systemInputDraftStableJson(value);
    let first = 2166136261;
    let second = 3339675911;
    for (let index = 0; index < source.length; index += 1) {
        const character = source.charCodeAt(index);
        first = Math.imul(first ^ character, 16777619);
        second = Math.imul(second ^ character, 2246822519);
    }
    return `${(first >>> 0).toString(16).padStart(8, '0')}${(second >>> 0).toString(16).padStart(8, '0')}`;
}

function systemInputDraftIdentity(workspace) {
    const snapshot = workspace?.snapshot;
    const workflowId = String(snapshot?.workflow_id || '').trim();
    const systemInput = workspace?.system_input;
    if (!workflowId || !systemInput || !Array.isArray(systemInput.units)) return null;
    const units = systemInput.units.map(unit => ({
        unitId: String(unit?.unit_id || ''),
        ordinal: unit?.ordinal ?? null,
        inputType: String(unit?.input_type || ''),
        sourceRange: unit?.source_range || {},
    })).sort((left, right) => left.unitId.localeCompare(right.unitId));
    if (units.some(unit => !unit.unitId) || new Set(units.map(unit => unit.unitId)).size !== units.length) return null;
    const items = (Array.isArray(workspace.items) ? workspace.items : []).map(item => ({
        itemId: String(item?.item_id || ''),
        sequence: item?.sequence ?? null,
        contentHash: String(item?.content_hash || item?.content_ref?.content_hash || item?.content_id || ''),
    })).sort((left, right) => left.itemId.localeCompare(right.itemId));
    const sourceArtifactId = String(snapshot?.source_artifact_id || '');
    if (!sourceArtifactId && !items.length && !units.length) return null;
    // Older projections may omit item hashes. Fingerprint stable parsed facts,
    // excluding audio artifacts, timestamps, execution state and target settings.
    const contentFallback = items.length && items.every(item => item.contentHash) ? null
        : (Array.isArray(systemInput.content_segments) ? systemInput.content_segments : []).map(segment => ({
            segmentId: String(segment?.segment_id || ''),
            unitId: String(segment?.unit_id || ''),
            itemId: String(segment?.item_id || ''),
            ordinal: segment?.ordinal ?? null,
            sourceLocator: String(segment?.source_locator || ''),
            rawText: String(segment?.raw_text || ''),
            ttsText: String(segment?.tts_text || ''),
        })).sort((left, right) => left.segmentId.localeCompare(right.segmentId));
    return {
        workflowId,
        sourceArtifactId,
        items,
        units,
        fallbackHash: contentFallback?.length ? systemInputDraftFingerprint(contentFallback) : '',
    };
}

function systemInputDraftKeyForIdentity(identity) {
    return identity ? `${SYSTEM_INPUT_DRAFT_PREFIX}${encodeURIComponent(identity.workflowId)}:${systemInputDraftFingerprint(identity)}` : '';
}

function systemInputDraftStorageKey(workspace) {
    return systemInputDraftKeyForIdentity(systemInputDraftIdentity(workspace));
}

function systemInputDraftStorage(options) {
    if (Object.prototype.hasOwnProperty.call(options, 'storage')) return options.storage;
    try {
        if (typeof getRendererStorage === 'function') return getRendererStorage();
        return root.localStorage || null;
    } catch (_) {
        return null;
    }
}

function systemInputDraftIsSafeJson(value, depth = 0) {
    if (depth > 32) return false;
    if (!value || typeof value !== 'object') return true;
    return Object.keys(value).every(key => !['__proto__', 'prototype', 'constructor'].includes(key)
        && systemInputDraftIsSafeJson(value[key], depth + 1));
}

function systemInputDraftIsPayload(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value) || !systemInputDraftIsSafeJson(value)) return false;
    if (value.unitDraftEntries === undefined) return true;
    return Array.isArray(value.unitDraftEntries) && value.unitDraftEntries.every(entry => Array.isArray(entry)
        && entry.length === 2 && typeof entry[0] === 'string' && entry[0]
        && entry[1] && typeof entry[1] === 'object' && !Array.isArray(entry[1]));
}

function systemInputDraftRemove(storage, key) {
    try {
        if (typeof storage?.removeItem !== 'function') return false;
        storage.removeItem(key);
        return true;
    } catch (_) {
        return false;
    }
}

function systemInputDraftRecordIsCurrent(record, now) {
    return record?.schemaVersion === SYSTEM_INPUT_DRAFT_SCHEMA
        && Number.isFinite(record.savedAt) && record.savedAt <= now + 60 * 1000
        && Number.isFinite(record.expiresAt) && record.expiresAt > now
        && record.expiresAt <= record.savedAt + SYSTEM_INPUT_DRAFT_TTL
        && systemInputDraftIsPayload(record.draft);
}

function systemInputPruneStoredDrafts(storage, currentKey, newBytes, now) {
    if (typeof storage.key !== 'function' || !Number.isFinite(storage.length)) return;
    const records = [];
    // Copy keys first: removal changes the storage indexes.
    const keys = Array.from({ length: storage.length }, (_, index) => storage.key(index));
    keys.filter(key => typeof key === 'string' && key.startsWith(SYSTEM_INPUT_DRAFT_PREFIX) && key !== currentKey).forEach(key => {
        try {
            const raw = storage.getItem(key);
            const record = JSON.parse(raw);
            if (!raw || raw.length * 2 > SYSTEM_INPUT_DRAFT_MAX_BYTES || !systemInputDraftRecordIsCurrent(record, now)) {
                systemInputDraftRemove(storage, key);
                return;
            }
            records.push({ key, bytes: raw.length * 2, savedAt: record.savedAt });
        } catch (_) {
            systemInputDraftRemove(storage, key);
        }
    });
    records.sort((left, right) => left.savedAt - right.savedAt);
    let totalBytes = newBytes + records.reduce((sum, record) => sum + record.bytes, 0);
    while (records.length && (records.length + 1 > SYSTEM_INPUT_DRAFT_MAX_COUNT || totalBytes > SYSTEM_INPUT_DRAFT_TOTAL_BYTES)) {
        const oldest = records.shift();
        systemInputDraftRemove(storage, oldest.key);
        totalBytes -= oldest.bytes;
    }
}

function saveSystemInputStoredDraft(workspace, draft, options = {}) {
    const identity = systemInputDraftIdentity(workspace);
    if (!identity) return { ok: false, reason: 'invalid_identity' };
    const storage = systemInputDraftStorage(options);
    if (typeof storage?.setItem !== 'function') return { ok: false, reason: 'unavailable' };
    const now = options.now ?? Date.now();
    if (!Number.isFinite(now)) return { ok: false, reason: 'invalid_draft' };
    let serialized;
    try {
        const payload = JSON.parse(JSON.stringify(draft));
        if (!systemInputDraftIsPayload(payload)) return { ok: false, reason: 'invalid_draft' };
        serialized = JSON.stringify({
            schemaVersion: SYSTEM_INPUT_DRAFT_SCHEMA,
            identity,
            savedAt: now,
            expiresAt: now + SYSTEM_INPUT_DRAFT_TTL,
            draft: payload,
        });
    } catch (_) {
        return { ok: false, reason: 'invalid_draft' };
    }
    // localStorage uses UTF-16. Bound the serialized record before touching it.
    const bytes = serialized.length * 2;
    if (bytes > SYSTEM_INPUT_DRAFT_MAX_BYTES) return { ok: false, reason: 'too_large' };
    const key = systemInputDraftKeyForIdentity(identity);
    try {
        systemInputPruneStoredDrafts(storage, key, bytes, now);
        storage.setItem(key, serialized);
        return { ok: true, savedAt: now, key };
    } catch (_) {
        return { ok: false, reason: 'storage_failed' };
    }
}

function readSystemInputStoredDraft(workspace, options = {}) {
    const identity = systemInputDraftIdentity(workspace);
    if (!identity) return null;
    const storage = systemInputDraftStorage(options);
    const key = systemInputDraftKeyForIdentity(identity);
    const now = options.now ?? Date.now();
    if (typeof storage?.getItem !== 'function' || !Number.isFinite(now)) return null;
    try {
        const raw = storage.getItem(key);
        if (!raw) return null;
        if (raw.length * 2 > SYSTEM_INPUT_DRAFT_MAX_BYTES) {
            systemInputDraftRemove(storage, key);
            return null;
        }
        const record = JSON.parse(raw);
        // Comparing the complete identity also guards against key hash collisions.
        if (!systemInputDraftRecordIsCurrent(record, now)
            || systemInputDraftStableJson(record.identity) !== systemInputDraftStableJson(identity)) {
            systemInputDraftRemove(storage, key);
            return null;
        }
        return { draft: record.draft, savedAt: record.savedAt, key };
    } catch (_) {
        systemInputDraftRemove(storage, key);
        return null;
    }
}

function clearSystemInputStoredDraft(workspace, options = {}) {
    const key = systemInputDraftStorageKey(workspace);
    return key ? systemInputDraftRemove(systemInputDraftStorage(options), key) : false;
}

const exports = {
    systemInputDraftIdentity,
    systemInputDraftStorageKey,
    saveSystemInputStoredDraft,
    readSystemInputStoredDraft,
    clearSystemInputStoredDraft,
};
if (typeof registerRendererModule === 'function') registerRendererModule('systemInput.draftStorage', exports);
if (typeof module !== 'undefined' && module.exports) module.exports = exports;
})(typeof globalThis !== 'undefined' ? globalThis : window);
