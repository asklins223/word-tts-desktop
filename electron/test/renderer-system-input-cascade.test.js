const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function loadCascade() {
    const context = vm.createContext({});
    const renderer = path.join(__dirname, '..', 'renderer');
    for (const filename of [
        'modules/core/module-bridge.js',
        'modules/system-input/normalizers.js',
        'modules/system-input/cascade.js',
    ]) {
        vm.runInContext(fs.readFileSync(path.join(renderer, filename), 'utf8'), context, { filename });
    }
    return context.WORDTTS_RENDERER.modules['systemInput.cascade'];
}

const api = loadCascade();
const plain = value => JSON.parse(JSON.stringify(value));

test('所有录入级联共享同一份依赖图，并能返回完整后代', () => {
    assert.deepEqual(plain(api.systemInputCascadeParents('paper', 'districtIds')), ['provinceId', 'cityId']);
    assert.deepEqual(
        plain(api.systemInputCascadeDescendants('paper', 'provinceId')),
        ['cityId', 'districtIds', 'platformTemplateName', 'platformTemplateId', 'platformTemplateVersion'],
    );
    assert.deepEqual(
        plain(api.systemInputCascadeParents('paper', 'platformTemplateName')),
        ['paperCategory', 'provinceId', 'cityId'],
    );
    assert.deepEqual(
        plain(api.systemInputCascadeDescendants('textbook', 'textbookVersion')),
        ['textbookStage', 'textbookGrade', 'textbookVolume', 'textbookUnit', 'textbookLesson'],
    );
    assert.equal(api.systemInputCascadeLocalField('textbook', 'textbookGrade'), 'grade');
    assert.equal(api.systemInputCascadeCanonicalField('textbook', 'grade'), 'textbookGrade');
});

test('级联就绪判断识别对象值、祖先路径和仅听说考试可用的考试类型', () => {
    const paperValues = {
        provinceId: { id: 'p1', name: '省A' },
        cityId: { id: 'c1', name: '城市A' },
        stageId: { id: 2, name: '初中' },
        paperCategory: '题型专项',
    };
    assert.equal(api.systemInputCascadeParentsReady('paper', 'districtIds', paperValues), true);
    assert.equal(api.systemInputCascadeParentsReady('paper', 'gradeId', paperValues), true);
    assert.equal(api.systemInputCascadeParentsReady('paper', 'paperType', paperValues), false);
    paperValues.paperCategory = '听说考试';
    assert.equal(api.systemInputCascadeParentsReady('paper', 'paperType', paperValues), true);
    paperValues.paperCategory = { name: ' 听说考试 ' };
    assert.equal(api.systemInputCascadeAvailable('paper', 'paperType', paperValues), true);
});

test('级联关系校验复用图中的父级字段映射，而不是由批量编辑重复实现', () => {
    const sources = {
        cities: [{ id: 'c1', name: '城市A', parentId: 'p1' }],
        districts: [{ id: 'd1', name: '区县A', parentId: 'c1' }],
        grades: [{ id: 7, name: '七年级', stageId: 2 }],
    };
    const values = {
        provinceId: { id: 'p1', name: '省A' },
        cityId: { id: 'c1', name: '城市A' },
        stageId: { id: 2, name: '初中' },
    };
    assert.equal(api.systemInputCascadeCompatibility('paper', 'cityId', { id: 'c1' }, values, sources), true);
    assert.equal(api.systemInputCascadeCompatibility('paper', 'cityId', { id: 'c2' }, values, sources), false);
    assert.equal(api.systemInputCascadeCompatibility('paper', 'districtIds', [{ id: 'd1' }], values, sources), true);
    assert.equal(api.systemInputCascadeCompatibility('paper', 'gradeId', { id: 7 }, values, sources), true);
});

test('批量级联要求父级字段也被勾选，父级变化会清空所有后代', () => {
    const values = {
        textbookVersion: '人教版',
        textbookStage: '初中',
        textbookGrade: '七年级',
        textbookVolume: '上册',
        textbookUnit: 'Unit 1',
        textbookLesson: 'Section A',
    };
    const selected = new Set(['textbookVersion']);
    // 只勾选版本时，年级仍缺少学段这个父级字段。
    assert.equal(api.systemInputCascadeBatchReady('textbook', 'textbookGrade', values, selected), false);
    selected.add('textbookStage');
    assert.equal(api.systemInputCascadeBatchReady('textbook', 'textbookGrade', values, selected), true);
    selected.add('textbookGrade');
    assert.equal(api.systemInputCascadeBatchReady('textbook', 'textbookUnit', values, selected), false);
    selected.add('textbookVolume');
    selected.add('textbookUnit');
    assert.equal(api.systemInputCascadeBatchReady('textbook', 'textbookLesson', values, selected), true);

    const cleared = api.systemInputCascadeResetDescendants('textbook', values, 'textbookVersion', {
        selectedFields: selected,
    });
    assert.deepEqual(plain(cleared), [
        'textbookStage', 'textbookGrade', 'textbookVolume', 'textbookUnit', 'textbookLesson',
    ]);
    assert.deepEqual(plain(values), {
        textbookVersion: '人教版',
        textbookStage: '',
        textbookGrade: '',
        textbookVolume: '',
        textbookUnit: '',
        textbookLesson: '',
    });
    assert.deepEqual([...selected], ['textbookVersion']);
});

test('清理级联后代时同时移除旧 snake_case 别名，避免保存边界带出旧值', () => {
    const values = {
        textbookVersion: '外研版',
        textbook_version: '人教版',
        textbookUnit: 'Unit 1',
        textbook_unit: 'Unit 1',
        textbookLesson: 'Section A',
        textbook_lesson: 'Section A',
    };
    api.systemInputCascadeResetDescendants('textbook', values, 'textbookVersion', { removeEmpty: true });
    assert.deepEqual(plain(values), {
        textbookVersion: '外研版',
    });
});

test('目录选项只显示当前父路径，并把失配的已保存值置于首位供核对', () => {
    const records = [
        { version: { name: '人教版' }, stage: { name: '初中' }, grade: { name: '七年级' }, volume: { name: '上册' }, unit: { name: 'Unit 1' } },
        { version: { name: '人教版' }, stage: { name: '初中' }, grade: { name: '八年级' }, volume: { name: '上册' }, unit: { name: 'Unit 2' } },
    ];
    const options = api.systemInputCascadeCatalogOptions({
        inputType: 'textbook',
        field: 'textbookUnit',
        values: {
            textbookVersion: '人教版',
            textbookStage: '初中',
            textbookGrade: '八年级',
            textbookVolume: '上册',
            textbookUnit: 'Unit 1',
        },
        records,
        getRecordValue: (record, field) => record[api.systemInputCascadeLocalField('textbook', field)],
        currentValue: 'Unit 1',
        currentDetail: '与上级教材项不匹配',
    });
    assert.deepEqual(plain(options.map(option => [option.value, option.detail])), [
        ['Unit 1', '与上级教材项不匹配'],
        ['Unit 2', ''],
    ]);
});

test('级联公共入口同时兼容 camelCase 和 snake_case 配置键', () => {
    const values = {
        paper_category: '听说考试',
        province_id: { id: 'p1', name: '省A' },
        city_id: { id: 'c1', name: '城市A' },
    };
    const sources = {
        cities: [{ id: 'c1', name: '城市A', parentId: 'p1' }],
    };
    assert.equal(api.systemInputCascadeAvailable('paper', 'paper_type', values), true);
    assert.equal(api.systemInputCascadeParentsReady('paper', 'district_ids', values), true);
    assert.equal(api.systemInputCascadeCompatibility('paper', 'city_id', { id: 'c1' }, values, sources), true);

    const selected = new Set(['province_id', 'city_id']);
    assert.equal(api.systemInputCascadeBatchReady('paper', 'district_ids', values, selected), true);
    assert.equal(api.systemInputCascadeFirstMissingParent('paper', 'district_ids', values, selected), '');
    assert.equal(api.systemInputCascadeLocalField('textbook', 'textbook_unit'), 'unit');

    const options = api.systemInputCascadeCatalogOptions({
        inputType: 'textbook',
        field: 'textbook_unit',
        values: {
            textbook_version: '人教版',
            textbook_stage: '初中',
            textbook_grade: '七年级',
            textbook_volume: '上册',
        },
        records: [
            { version: { name: '人教版' }, stage: { name: '初中' }, grade: { name: '七年级' }, volume: { name: '上册' }, unit: { name: 'Unit 1' } },
            { version: { name: '人教版' }, stage: { name: '初中' }, grade: { name: '七年级' }, volume: { name: '上册' }, unit: { name: 'Unit 2' } },
        ],
        getRecordValue: (record, field) => record[api.systemInputCascadeLocalField('textbook', field)],
    });
    assert.deepEqual(plain(options.map(option => option.value)), ['Unit 1', 'Unit 2']);
});
