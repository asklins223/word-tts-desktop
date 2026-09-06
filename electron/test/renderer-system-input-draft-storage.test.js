const assert = require('node:assert/strict');
const test = require('node:test');
const {
    systemInputDraftIdentity,
    systemInputDraftStorageKey,
    saveSystemInputStoredDraft,
    readSystemInputStoredDraft,
    clearSystemInputStoredDraft,
} = require('../renderer/modules/system-input/draft-storage');

const now = 1700000000000;

function memoryStorage() {
    const records = new Map();
    return {
        get length() { return records.size; },
        key: index => [...records.keys()][index] || null,
        getItem: key => records.get(key) ?? null,
        setItem: (key, value) => records.set(key, String(value)),
        removeItem: key => records.delete(key),
    };
}

function workspace() {
    return {
        snapshot: { workflow_id: 'workflow-1', source_artifact_id: 'source-1', state_version: 7, draft_revision: 2 },
        items: [
            { item_id: 'item-2', sequence: 2, content_hash: 'hash-2', result_status: 'PENDING' },
            { item_id: 'item-1', sequence: 1, content_hash: 'hash-1', result_status: 'PENDING' },
        ],
        system_input: {
            input_type: 'textbook',
            units: [
                { unit_id: 'unit-1', ordinal: 1, input_type: 'textbook', source_range: { start: 1, end: 4 }, configuration: {} },
                { unit_id: 'unit-2', ordinal: 2, input_type: 'textbook', source_range: { start: 5, end: 8 }, configuration: {} },
            ],
        },
    };
}

function draft() {
    return {
        selectedUnitId: 'unit-2',
        unitDraftEntries: [['unit-1', { textbookNameZh: '', textbookVersion: '人教版' }], ['unit-2', {}]],
        deliveryMode: 'audio_and_input',
        inputType: 'textbook',
        templateEditor: { visible: true, name: '未完成方案' },
        editorState: {
            defaults: { textbookVersion: '人教版' },
            overrides: { 'unit-2': ['textbookVersion'] },
            reviewAcknowledgements: ['unit-1'],
        },
    };
}

test('incomplete multi-unit drafts and editor state survive an application restart without mutating the workspace', () => {
    const storage = memoryStorage();
    const current = workspace();
    const before = structuredClone(current);
    const value = draft();
    const saved = saveSystemInputStoredDraft(current, value, { storage, now });
    assert.equal(saved.ok, true);
    value.unitDraftEntries[0][1].textbookNameZh = 'later unsaved change';
    const restored = readSystemInputStoredDraft(structuredClone(current), { storage, now: now + 1000 });
    assert.deepEqual(restored.draft, draft());
    assert.equal(restored.savedAt, now);
    assert.deepEqual(current, before);
    assert.equal(current.system_input.delivery_mode, undefined);
});

test('identity is stable across projection ordering, execution progress and saved target changes', () => {
    const first = workspace();
    const changed = workspace();
    changed.items.reverse();
    changed.system_input.units.reverse();
    changed.system_input.units[1].source_range = { end: 4, start: 1 };
    changed.snapshot.state_version += 10;
    changed.snapshot.draft_revision += 1;
    changed.items[0].result_status = 'SUCCEEDED';
    changed.system_input.units[0].configuration = { textbook_name_zh: 'new target' };
    assert.equal(systemInputDraftStorageKey(first), systemInputDraftStorageKey(changed));
});

test('different workflows, documents, parsed text or unit boundaries never restore an old draft', () => {
    const storage = memoryStorage();
    const current = workspace();
    saveSystemInputStoredDraft(current, draft(), { storage, now });
    const changes = [
        next => { next.snapshot.workflow_id = 'workflow-2'; },
        next => { next.snapshot.source_artifact_id = 'source-2'; },
        next => { next.items[0].content_hash = 'new-text-hash'; },
        next => { next.items[0].sequence = 3; },
        next => { next.system_input.units[0].source_range.end = 5; },
        next => { next.system_input.units.pop(); },
        next => { next.system_input.units[0].input_type = 'paper'; },
    ];
    for (const change of changes) {
        const next = workspace();
        change(next);
        assert.notEqual(systemInputDraftStorageKey(current), systemInputDraftStorageKey(next));
        assert.equal(readSystemInputStoredDraft(next, { storage, now }), null);
    }
    assert.ok(readSystemInputStoredDraft(current, { storage, now }));
});

test('legacy projections without content hashes use parsed segment facts rather than generated audio', () => {
    const first = workspace();
    first.items = [];
    first.system_input.content_segments = [{ segment_id: 'segment-1', raw_text: 'Hello', audio_artifact_id: 'audio-1' }];
    const next = structuredClone(first);
    next.system_input.content_segments[0].audio_artifact_id = 'audio-2';
    assert.equal(systemInputDraftStorageKey(first), systemInputDraftStorageKey(next));
    next.system_input.content_segments[0].raw_text = 'Hello again';
    assert.notEqual(systemInputDraftStorageKey(first), systemInputDraftStorageKey(next));
});

test('expired, corrupt and mismatched-schema records are ignored and removed', () => {
    const storage = memoryStorage();
    const current = workspace();
    const key = systemInputDraftStorageKey(current);
    const mutations = [
        record => { record.schemaVersion = 0; },
        record => { record.expiresAt = now - 1; },
        record => { record.savedAt = now + 120000; },
        record => { record.identity.units[0].unitId = 'another-unit'; },
        record => { record.draft.unitDraftEntries = [['unit-1', null]]; },
    ];
    for (const mutate of mutations) {
        saveSystemInputStoredDraft(current, draft(), { storage, now });
        const record = JSON.parse(storage.getItem(key));
        mutate(record);
        storage.setItem(key, JSON.stringify(record));
        assert.equal(readSystemInputStoredDraft(current, { storage, now }), null);
        assert.equal(storage.getItem(key), null);
    }
    storage.setItem(key, '{bad JSON');
    assert.equal(readSystemInputStoredDraft(current, { storage, now }), null);
    assert.equal(storage.getItem(key), null);
});

test('storage access failures retain in-memory editing semantics without throwing', () => {
    const blocked = {
        getItem() { throw new Error('blocked'); },
        setItem() { throw new Error('quota exceeded'); },
        removeItem() { throw new Error('blocked'); },
    };
    assert.deepEqual(saveSystemInputStoredDraft(workspace(), draft(), { storage: null, now }), { ok: false, reason: 'unavailable' });
    assert.deepEqual(saveSystemInputStoredDraft(workspace(), draft(), { storage: blocked, now }), { ok: false, reason: 'storage_failed' });
    assert.equal(readSystemInputStoredDraft(workspace(), { storage: blocked, now }), null);
    assert.equal(clearSystemInputStoredDraft(workspace(), { storage: blocked }), false);
});

test('oversized or non-JSON drafts do not overwrite a previously saved draft', () => {
    const storage = memoryStorage();
    const current = workspace();
    saveSystemInputStoredDraft(current, draft(), { storage, now });
    assert.deepEqual(saveSystemInputStoredDraft(current, { name: 'x'.repeat(200000) }, { storage, now }), { ok: false, reason: 'too_large' });
    const cyclic = {};
    cyclic.self = cyclic;
    assert.deepEqual(saveSystemInputStoredDraft(current, cyclic, { storage, now }), { ok: false, reason: 'invalid_draft' });
    assert.deepEqual(readSystemInputStoredDraft(current, { storage, now }).draft, draft());
});

test('untrusted prototype payloads and ambiguous identities cannot be stored or restored', () => {
    const storage = memoryStorage();
    const unsafe = JSON.parse('{"unitDraftEntries":[],"editorState":{"__proto__":{"polluted":true}}}');
    assert.deepEqual(saveSystemInputStoredDraft(workspace(), unsafe, { storage, now }), { ok: false, reason: 'invalid_draft' });
    const duplicate = workspace();
    duplicate.system_input.units[1].unit_id = 'unit-1';
    assert.equal(systemInputDraftIdentity(duplicate), null);
    assert.equal(systemInputDraftStorageKey({}), '');
    assert.deepEqual(saveSystemInputStoredDraft({}, draft(), { storage, now }), { ok: false, reason: 'invalid_identity' });
});

test('clearing a saved or discarded draft affects only that workflow and content version', () => {
    const storage = memoryStorage();
    const first = workspace();
    const other = workspace();
    other.snapshot.workflow_id = 'workflow-2';
    saveSystemInputStoredDraft(first, draft(), { storage, now });
    saveSystemInputStoredDraft(other, draft(), { storage, now });
    assert.equal(clearSystemInputStoredDraft(first, { storage }), true);
    assert.equal(readSystemInputStoredDraft(first, { storage, now }), null);
    assert.ok(readSystemInputStoredDraft(other, { storage, now }));
});

test('retention bounds local draft count and size without removing unrelated storage', () => {
    const storage = memoryStorage();
    storage.setItem('user-preferences', 'keep');
    for (let index = 0; index < 20; index += 1) {
        const current = workspace();
        current.snapshot.workflow_id = `workflow-${index}`;
        assert.equal(saveSystemInputStoredDraft(current, { note: 'x'.repeat(50000) }, { storage, now: now + index }).ok, true);
    }
    assert.equal(storage.length, 13);
    assert.equal(storage.getItem('user-preferences'), 'keep');
    const old = workspace();
    old.snapshot.workflow_id = 'workflow-0';
    assert.equal(readSystemInputStoredDraft(old, { storage, now: now + 20 }), null);
    for (let index = 20; index < 30; index += 1) {
        const current = workspace();
        current.snapshot.workflow_id = `workflow-${index}`;
        saveSystemInputStoredDraft(current, { note: 'x'.repeat(150000) }, { storage, now: now + index });
    }
    const total = Array.from({ length: storage.length }, (_, index) => storage.getItem(storage.key(index)).length * 2).reduce((sum, bytes) => sum + bytes, 0);
    assert.ok(total <= 2 * 1024 * 1024 + 8);
});
