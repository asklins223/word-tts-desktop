const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function choice(id, name) { return { id, name }; }

function loadCatalog(records) {
    const context = vm.createContext({
        systemInputPlatformTemplateCatalog: { records, record_count: records.length },
        systemInputPlatformTemplates: records,
        systemInputPlatformTemplatePendingSelection: null,
        systemInputPlatformTemplateCatalogSync: { status: 'SUCCEEDED' },
        systemInputPlatformTemplateCatalogSyncBusy: false,
        systemInputPlatformTemplateCatalogPollTimer: null,
        systemInputPlatformTemplateRequestId: 0,
        systemInputPlatformTemplatesType: 'paper',
        systemInputPlatformTemplateBusy: false,
        $: () => null,
        setTimeout,
        clearTimeout,
    });
    const renderer = path.join(__dirname, '..', 'renderer');
    for (const filename of [
        'modules/core/module-bridge.js',
        'modules/system-input/normalizers.js',
        'modules/system-input/cascade.js',
        'modules/system-input/platform-templates.js',
    ]) {
        vm.runInContext(fs.readFileSync(path.join(renderer, filename), 'utf8'), context, { filename });
    }
    return context.WORDTTS_RENDERER.modules['systemInput.platformTemplates'];
}

const records = [
    {
        platform_template_key: 'question:q-hb', template_kind: 'question',
        platform_template_id: 'q-hb', name: '模仿朗读', enabled: true,
        province: choice('42', '湖北省'), city: choice('4201', '武汉市'),
    },
    {
        platform_template_key: 'question:q-gd', template_kind: 'question',
        platform_template_id: 'q-gd', name: '信息获取', enabled: true,
        province: choice('44', '广东省'), city: choice('4406', '佛山市'),
    },
    {
        platform_template_key: 'paper:p-7', template_kind: 'paper',
        platform_template_id: 'p-7', name: '人教版 听说测试题模板', enabled: true,
        province: choice('42', '湖北省'), city: choice('4201', '武汉市'),
        stage: choice('2', '初中'), grade: choice('7', '七年级'),
    },
    {
        platform_template_key: 'paper:p-8', template_kind: 'paper',
        platform_template_id: 'p-8', name: '人教版 听说测试题模板', enabled: true,
        province: choice('42', '湖北省'), city: choice('4201', '武汉市'),
        stage: choice('2', '初中'), grade: choice('8', '八年级'),
    },
];

test('专项题型模板只按省市筛选，不受年级变化影响', () => {
    const api = loadCatalog(records);
    const result = api.systemInputPlatformTemplateCatalogAssessment({
        paperCategory: '题型专项',
        provinceId: choice('42', '湖北省'),
        cityId: choice('4201', '武汉市'),
        stageId: choice('2', '初中'),
        gradeId: choice('99', '不存在的年级'),
    });
    assert.equal(result.kind, 'question');
    assert.equal(result.status, 'ready');
    assert.deepEqual(result.candidates.map(item => item.platform_template_id), ['q-hb']);
});

test('试卷模板等待地区、学段和年级齐全后按完整范围筛选', () => {
    const api = loadCatalog(records);
    const pending = api.systemInputPlatformTemplateCatalogAssessment({
        paperCategory: '听说考试',
        provinceId: choice('42', '湖北省'),
        cityId: choice('4201', '武汉市'),
        stageId: choice('2', '初中'),
    });
    assert.equal(pending.status, 'pending');
    assert.deepEqual(Array.from(pending.missing), ['gradeId']);

    const ready = api.systemInputPlatformTemplateCatalogAssessment({
        paperCategory: '听说考试',
        provinceId: choice('42', '湖北省'),
        cityId: choice('4201', '武汉市'),
        stageId: choice('2', '初中'),
        gradeId: choice('8', '八年级'),
    });
    assert.deepEqual(ready.candidates.map(item => item.platform_template_id), ['p-8']);
});

test('已保存模板与当前范围冲突时明确标记，不能作为匹配结果', () => {
    const api = loadCatalog(records);
    const result = api.systemInputPlatformTemplateCatalogAssessment({
        paperCategory: '听说考试',
        provinceId: choice('42', '湖北省'),
        cityId: choice('4201', '武汉市'),
        stageId: choice('2', '初中'),
        gradeId: choice('8', '八年级'),
        platformTemplateId: 'p-7',
        platformTemplateName: '人教版 听说测试题模板',
    });
    assert.equal(result.status, 'conflict');
    assert.equal(result.matched, null);
    assert.match(result.message, /不适用/);
});
