const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const fieldNames = ['version', 'stage', 'grade', 'volume', 'unit', 'lesson'];
const shared = { version: '人教版', stage: '初中', grade: '七年级', volume: '上册' };
const complete = { ...shared, unit: 'Unit 1', lesson: 'Section A' };

function catalogRecord(values = {}, suffix = '') {
    return Object.fromEntries(Object.entries({ ...complete, ...values }).map(([key, name]) => [
        key, { id: `${key}:${name}${suffix}`, name },
    ]));
}

function configuration(values) {
    return Object.fromEntries(Object.entries(values).map(([key, value]) => [
        `textbook${key.charAt(0).toUpperCase()}${key.slice(1)}`, value,
    ]));
}

function loadCatalog(records = [], { storage = null } = {}) {
    const inputs = new Map(fieldNames.map(key => [`system-input-textbook-${key}`, {
        value: complete[key],
        setAttribute() {},
    }]));
    const statusNode = { textContent: '', classList: { toggle() {} } };
    const noteNode = { textContent: '' };
    const syncButton = { disabled: false, textContent: '', setAttribute() {} };
    const elements = new Map([
        ...inputs,
        ['system-input-textbook-catalog-status', statusNode],
        ['system-input-textbook-catalog-note', noteNode],
        ['system-input-textbook-sync-btn', syncButton],
    ]);
    const choices = new Map();
    const toasts = [];
    const context = {
        localStorage: storage,
        systemInputTextbookCatalog: { records, record_count: records.length },
        systemInputTextbookCatalogSync: { status: 'IDLE', record_count: 0 },
        systemInputTextbookCatalogSyncBusy: false,
        $: id => elements.get(id),
        setSystemInputPickerOptions: (id, options) => choices.set(id, options),
        setSystemInputPickerEnabled() {},
        showToast: message => toasts.push(message),
        registerRendererModule: (_name, exports) => {
            Object.assign(context, exports);
            context.api = exports;
        },
    };
    vm.createContext(context);
    const cascade = fs.readFileSync(path.join(__dirname, '../renderer/modules/system-input/cascade.js'), 'utf8');
    const source = fs.readFileSync(path.join(__dirname, '../renderer/modules/system-input/textbook-catalog.js'), 'utf8');
    vm.runInContext(cascade, context);
    vm.runInContext(source, context);
    return { api: context.api, inputs, choices, toasts, statusNode, noteNode, syncButton };
}

function memoryStorage() {
    const values = new Map();
    return {
        getItem: key => values.get(key) ?? null,
        setItem: (key, value) => values.set(key, String(value)),
    };
}

test('重复选择教材上级不清空已填写的下级，也不提示无效变更', () => {
    const { api, inputs, toasts } = loadCatalog([catalogRecord()]);
    api.renderSystemInputTextbookOptions();
    const change = api.handleSystemInputTextbookFieldChange('system-input-textbook-version');
    assert.deepEqual(Array.from(change.changedFields), []);
    assert.equal(inputs.get('system-input-textbook-lesson').value, 'Section A');
    assert.equal(inputs.get('system-input-textbook-unit').value, 'Unit 1');
    assert.deepEqual(toasts, []);
});

test('变更教材版本保留兼容的年级、单元和课时', () => {
    const records = [catalogRecord(), catalogRecord({ version: '外研版' })];
    const { api, inputs, toasts } = loadCatalog(records);
    api.renderSystemInputTextbookOptions();
    inputs.get('system-input-textbook-version').value = '外研版';
    const change = api.handleSystemInputTextbookFieldChange('system-input-textbook-version');
    assert.deepEqual(Array.from(change.changedFields), ['version']);
    assert.deepEqual(Array.from(change.invalidFields), []);
    assert.equal(inputs.get('system-input-textbook-grade').value, '七年级');
    assert.equal(inputs.get('system-input-textbook-unit').value, 'Unit 1');
    assert.equal(inputs.get('system-input-textbook-lesson').value, 'Section A');
    assert.deepEqual(toasts, []);
});

test('变更到不兼容的年级保留原单元供对照，并显式标记目录冲突', () => {
    const records = [catalogRecord(), catalogRecord({ grade: '八年级', unit: 'Unit 2' })];
    const { api, inputs, choices, toasts } = loadCatalog(records);
    api.renderSystemInputTextbookOptions();
    inputs.get('system-input-textbook-grade').value = '八年级';
    const change = api.handleSystemInputTextbookFieldChange('system-input-textbook-grade');
    assert.equal(change.assessment.status, 'conflict');
    assert.ok(change.invalidFields.includes('unit'));
    assert.equal(inputs.get('system-input-textbook-unit').value, 'Unit 1');
    assert.equal(inputs.get('system-input-textbook-lesson').value, 'Section A');
    assert.match(choices.get('system-input-textbook-unit')[0].detail, /不匹配/);
    assert.equal(toasts.length, 1);
    api.handleSystemInputTextbookFieldChange('system-input-textbook-grade');
    assert.equal(toasts.length, 1);
});

test('缺少本地目录时保留所有手填值，不能报告目录校验成功', () => {
    const { api } = loadCatalog();
    const result = api.systemInputTextbookCatalogAssessment(configuration(complete), { records: [] });
    assert.equal(result.status, 'unavailable');
    assert.match(result.label, /待核对/);
    assert.deepEqual(Object.keys(result.suggestions), []);
    assert.equal(result.fields.lesson.value, 'Section A');
});

test('字段齐全只表示匹配本地目录，不表示已经通过平台验证', () => {
    const { api } = loadCatalog();
    const result = api.systemInputTextbookCatalogAssessment(configuration(complete), { records: [catalogRecord()] });
    assert.equal(result.status, 'matched');
    assert.equal(result.label, '已匹配本地目录');
    assert.match(result.message, /仍会.*校验/);
    assert.deepEqual(Object.keys(result.suggestions), []);
});

test('唯一课时建议只补空字段，不覆盖已设置的教材单元', () => {
    const { api } = loadCatalog();
    const saved = configuration({ ...shared, unit: 'Unit 1' });
    const before = JSON.stringify(saved);
    const records = [catalogRecord(), catalogRecord({ unit: 'Unit 2', lesson: 'Section B' })];
    const result = api.systemInputTextbookCatalogAssessment(saved, { records });
    assert.equal(result.status, 'suggested');
    assert.equal(result.suggestions.textbookLesson, 'Section A');
    assert.equal(result.suggestions.textbookUnit, undefined);
    assert.equal(JSON.stringify(saved), before);
});

test('教材未选齐、多候选和同名不同目录 ID 均不自动给出唯一目标', () => {
    const { api } = loadCatalog();
    const incomplete = api.systemInputTextbookCatalogAssessment({ textbookVersion: '人教版' }, { records: [catalogRecord()] });
    assert.equal(incomplete.status, 'incomplete');
    assert.deepEqual(Object.keys(incomplete.suggestions), []);

    const records = [catalogRecord(), catalogRecord({ unit: 'Unit 2' })];
    const multiple = api.systemInputTextbookCatalogAssessment(configuration(shared), { records });
    assert.equal(multiple.status, 'incomplete');
    assert.deepEqual(Object.keys(multiple.suggestions), []);

    const ambiguous = api.systemInputTextbookCatalogAssessment(configuration(complete), {
        records: [catalogRecord(), catalogRecord({}, ':other-id')],
    });
    assert.equal(ambiguous.status, 'conflict');
    assert.equal(ambiguous.label, '目录存在同名项');
});

test('相同目录的重复内容记录去重，识别提示只用于缺失字段', () => {
    const { api } = loadCatalog();
    const records = [catalogRecord(), catalogRecord(), catalogRecord({ unit: 'Unit 2', lesson: 'Section B' })];
    const suggested = api.systemInputTextbookCatalogAssessment(configuration(shared), {
        records,
        hints: { textbook_unit: 'Unit 2', textbook_lesson: 'Section B' },
    });
    assert.equal(suggested.status, 'suggested');
    assert.equal(suggested.suggestions.textbookUnit, 'Unit 2');
    assert.equal(suggested.suggestions.textbookLesson, 'Section B');

    const saved = api.systemInputTextbookCatalogAssessment(configuration(complete), {
        records,
        hints: { unit: 'Unit 2', lesson: 'Section B' },
    });
    assert.equal(saved.status, 'matched');
    assert.deepEqual(Object.keys(saved.suggestions), []);
});

test('识别提示不存在时提示核对，不能建议另一条目录', () => {
    const { api } = loadCatalog();
    const result = api.systemInputTextbookCatalogAssessment(configuration(shared), {
        records: [catalogRecord()],
        hints: { unit: 'Unit 9' },
    });
    assert.equal(result.status, 'conflict');
    assert.equal(result.label, '识别建议待核对');
    assert.deepEqual(Object.keys(result.suggestions), []);
});

test('手填名称与冲突目录分别标记，并保留原值', () => {
    const { api } = loadCatalog();
    const result = api.systemInputReconcileTextbookDirectory(complete, { ...complete, lesson: '自主拓展' }, [catalogRecord()]);
    assert.equal(result.assessment.status, 'manual');
    assert.equal(result.values.lesson, '自主拓展');
    assert.deepEqual(Array.from(result.changedFields), ['lesson']);
    assert.deepEqual(Array.from(result.invalidFields), ['lesson']);
});

test('大小写、全角和空白差异不触发清空或假冲突', () => {
    const { api } = loadCatalog();
    const next = { ...complete, unit: ' ＵＮＩＴ　１ ', lesson: 'section a' };
    const result = api.systemInputReconcileTextbookDirectory(complete, next, [catalogRecord()]);
    assert.equal(result.assessment.status, 'matched');
    assert.deepEqual(Array.from(result.changedFields), []);
    assert.deepEqual(Array.from(result.invalidFields), []);
});

test('上次同步时间写入本机存储，重新加载后仍可展示', () => {
    const storage = memoryStorage();
    const first = loadCatalog([], { storage });
    first.api.applySystemInputTextbookCatalogResponse({
        catalog: { records: [], synced_at: '2026-09-05T08:30:00+00:00', record_count: 12 },
        sync: { status: 'SUCCEEDED' },
    });
    const stored = first.api.readTextbookCatalogSyncMeta();
    assert.equal(stored.schemaVersion, 1);
    assert.equal(stored.syncedAt, '2026-09-05T08:30:00.000Z');
    assert.equal(stored.recordCount, 12);
    assert.equal(stored.sourceTotal, 12);
    assert.equal(stored.pageCount, 0);

    const second = loadCatalog([], { storage });
    second.api.renderSystemInputTextbookCatalogStatus();
    assert.equal(second.statusNode.textContent, '已同步 12 条');
    assert.match(second.noteNode.textContent, /上次同步时间：2026\/09\/05 16:30/);
    assert.match(second.noteNode.textContent, /目录更新频率低/);
});

test('目录缓存重载时仍保留来源总数和覆盖差异', () => {
    const storage = memoryStorage();
    const first = loadCatalog([], { storage });
    first.api.applySystemInputTextbookCatalogResponse({
        catalog: {
            records: [catalogRecord(), catalogRecord({ unit: 'Unit 2' }), catalogRecord({ unit: 'Unit 3' })],
            record_count: 3,
            source_total: 6,
            page_count: 1,
            synced_at: '2026-09-05T08:30:00+00:00',
        },
        sync: { status: 'SUCCEEDED', record_count: 3, source_total: 6, page_count: 1 },
    });

    // A restart may return a legacy cache shape without the newer source
    // counters. The durable sync metadata is still enough to explain the
    // coverage gap instead of silently reporting a complete catalogue.
    const second = loadCatalog(
        [catalogRecord(), catalogRecord({ unit: 'Unit 2' }), catalogRecord({ unit: 'Unit 3' })],
        { storage },
    );
    second.api.renderSystemInputTextbookCatalogStatus();
    assert.equal(second.statusNode.textContent, '已加载 3 / 6 条');
    assert.match(second.noteNode.textContent, /平台返回 6 条，当前目录可用 3 条/);
});

test('目录来源条数大于可用记录时明确展示覆盖差异和缺失字段', () => {
    const { api, statusNode, noteNode } = loadCatalog([], { storage: memoryStorage() });
    api.applySystemInputTextbookCatalogResponse({
        catalog: {
            records: [{
                stage: { id: '2', name: '初中' },
                grade: { id: '7', name: '七年级' },
                volume: { id: '1', name: '上册' },
            }],
            record_count: 3,
            source_total: 6,
            synced_at: '2026-09-05T08:30:00+00:00',
        },
        sync: { status: 'SUCCEEDED', record_count: 3, source_total: 6 },
    });
    assert.equal(statusNode.textContent, '已加载 3 / 6 条');
    assert.match(noteNode.textContent, /平台返回 6 条，当前目录可用 3 条/);
    assert.match(noteNode.textContent, /未返回字段：版本、教材单元、课时/);
});

test('嵌套教材目录按教材数和展开路径数分别展示，不误报覆盖缺口', () => {
    const storage = memoryStorage();
    const records = [catalogRecord(), catalogRecord({ unit: 'Unit 2' }), catalogRecord({ unit: 'Unit 3' })];
    const first = loadCatalog([], { storage });
    first.api.applySystemInputTextbookCatalogResponse({
        catalog: {
            records,
            record_count: records.length,
            source_total: 6,
            source_total_scope: 'source_books',
            synced_at: '2026-09-05T08:30:00+00:00',
        },
        sync: {
            status: 'SUCCEEDED',
            record_count: records.length,
            source_total: 6,
            source_total_scope: 'source_books',
        },
    });
    assert.equal(first.statusNode.textContent, '已加载 6 本教材 · 3 条路径');
    assert.match(first.noteNode.textContent, /平台返回 6 本教材，展开为 3 条可选路径/);
    assert.equal(first.api.readTextbookCatalogSyncMeta().sourceTotalScope, 'source_books');

    const second = loadCatalog(records, { storage });
    second.api.renderSystemInputTextbookCatalogStatus();
    assert.equal(second.statusNode.textContent, '已加载 6 本教材 · 3 条路径');
    assert.match(second.noteNode.textContent, /上次同步时间：2026\/09\/05 16:30/);
});

test('空目录返回的生成时间不会伪装成上次同步时间', () => {
    const storage = memoryStorage();
    const { api } = loadCatalog([], { storage });
    assert.equal(api.persistTextbookCatalogSyncMeta(
        { synced_at: '2026-09-05T08:30:00+00:00', record_count: 0 },
        { status: 'IDLE' },
    ), false);
    assert.equal(api.readTextbookCatalogSyncMeta(), null);
});
