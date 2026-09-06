const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function loadBatchModel() {
    const context = vm.createContext({});
    const renderer = path.join(__dirname, '..', 'renderer');
    for (const filename of [
        'modules/core/module-bridge.js',
        'modules/system-input/normalizers.js',
        'modules/system-input/cascade.js',
        'modules/system-input/batch-model.js',
    ]) {
        vm.runInContext(fs.readFileSync(path.join(renderer, filename), 'utf8'), context, { filename });
    }
    return context.WORDTTS_RENDERER.modules['systemInput.batchModel'];
}

const api = loadBatchModel();
const plain = value => JSON.parse(JSON.stringify(value));

test('批量编辑只更改选定条目和显式字段，默认仅补空值', () => {
    const original = [
        { unit_id: 'a', paperName: '第一套', year: 2025, answerTimeMinutes: 30 },
        { unit_id: 'b', paperName: '第二套', year: '', answerTimeMinutes: 25 },
        { unit_id: 'c', paperName: '第三套' },
    ];
    const result = api.systemInputBatchApply(original, {
        unitIds: ['a', 'b'], fields: ['year'],
        values: { year: 2026, answerTimeMinutes: 50, paperName: '不能批量改名' },
    });
    assert.deepEqual(plain(result.configurations), [original[0], { ...original[1], year: 2026 }, original[2]]);
    assert.deepEqual(plain(result.updatedUnitIds), ['b']);
    assert.equal(original[1].year, '');
});

test('未勾选条目或字段时不扩大操作范围', () => {
    const original = [{ unit_id: 'a', year: 2025 }];
    assert.deepEqual(plain(api.systemInputBatchApply(original, { fields: ['year'], values: { year: 2026 } }).changes), []);
    assert.deepEqual(plain(api.systemInputBatchApply(original, { unitIds: ['a'], values: { year: 2026 } }).changes), []);
});

test('空白值不会被传播，零值视为已填并交给表单校验', () => {
    const original = [{ unit_id: 'a', year: 0, answerTimeMinutes: '' }];
    const result = api.systemInputBatchApply(original, {
        unitIds: ['a'], fields: ['year', 'answerTimeMinutes'],
        values: { year: 2026, answerTimeMinutes: '  ' },
    });
    assert.deepEqual(plain(result.changes), []);
    assert.equal(api.systemInputBatchIsEmpty({ name: '  ' }), true);
    assert.equal(api.systemInputBatchIsEmpty({ id: 0 }), false);
});

test('显式覆盖允许只改部分套卷，也允许清空字段', () => {
    const result = api.systemInputBatchApply([
        { unit_id: 'a', paperName: '第一套', year: 2025 },
        { unit_id: 'b', paperName: '第二套', year: 2025 },
    ], { unitIds: ['b'], fields: ['year'], values: { year: '' }, mode: 'overwrite' });
    assert.equal(result.configurations[0].year, 2025);
    assert.equal(result.configurations[1].year, '');
    assert.equal(result.configurations[1].paperName, '第二套');
});

test('课文名称和目录独立字段必须显式选择，共用字段清单不包含它们', () => {
    const original = [{ unit_id: 'a', textbookNameZh: '课文一', textbookUnit: 'Unit 1' }];
    const values = { textbookNameZh: '课文二', textbookUnit: 'Unit 2', textbookForm: '同步课文' };
    const common = api.systemInputBatchFields('textbook', { commonOnly: true });
    assert.equal(common.includes('textbookNameZh'), false);
    assert.equal(common.includes('textbookUnit'), false);
    assert.equal(common.includes('textbookLesson'), false);
    const result = api.systemInputBatchApply(original, {
        inputType: 'textbook', unitIds: ['a'], fields: ['textbookNameZh'], values, mode: 'overwrite',
    });
    assert.equal(result.configurations[0].textbookNameZh, '课文二');
    assert.equal(result.configurations[0].textbookUnit, 'Unit 1');
    assert.equal(result.configurations[0].textbookForm, undefined);
});

test('禁止更换条目身份或复制内容、答案、音频和跨类型字段', () => {
    const result = api.systemInputBatchApply([{ unit_id: 'a', paperName: '第一套' }], {
        unitIds: ['a'], fields: ['unit_id', 'content', 'answers', 'audio', 'textbookNameZh', 'provinceId'],
        values: {
            unit_id: 'other', content: 'secret', answers: ['A'], audio: '/tmp/audio.mp3', textbookNameZh: '课文',
            provinceId: { id: 1, name: '浙江省', audio: '/tmp/audio.mp3', content: 'secret', answers: ['B'] },
        },
        mode: 'overwrite',
    });
    assert.deepEqual(plain(result.configurations[0]), {
        unit_id: 'a', paperName: '第一套', provinceId: { id: 1, name: '浙江省' },
    });
});

test('默认值更新保留单条差异、明确覆盖和名称，跟随旧默认值的条目继续继承', () => {
    const original = [
        { unit_id: 'a', paperName: '甲', year: 2025, answerTimeMinutes: 20 },
        { unit_id: 'b', paperName: '乙', year: 2024, answerTimeMinutes: 20 },
        { unit_id: 'c', paperName: '丙', year: 2025 },
        { unit_id: 'd', paperName: '丁' },
    ];
    const result = api.systemInputBatchUpdateDefaults(original, {
        previousDefaults: { year: 2025, answerTimeMinutes: 20 },
        defaults: { year: 2026, answerTimeMinutes: 30, paperName: '不得复制' },
        overrides: { c: ['year'] },
    });
    assert.deepEqual(plain(result.configurations.map(row => row.year)), [2026, 2024, 2025, 2026]);
    assert.deepEqual(plain(result.configurations.map(row => row.paperName)), ['甲', '乙', '丙', '丁']);
    assert.deepEqual(plain(result.configurations.map(row => row.answerTimeMinutes)), [30, 30, 30, 30]);
});

test('同一个上级选项重复选择不会清空下级或要求重新核对', () => {
    const original = [{ unit_id: 'a', provinceId: { id: 1, name: '浙江' }, cityId: { id: 2, name: '杭州' } }];
    const result = api.systemInputBatchApply(original, {
        unitIds: ['a'], fields: ['provinceId'], values: { provinceId: { name: '浙江', id: '1' } }, mode: 'overwrite',
        compatible: () => false,
    });
    assert.deepEqual(plain(result.changes), []);
    assert.deepEqual(plain(result.review), []);
    assert.deepEqual(plain(result.configurations), original);
});

test('教材更换后未知目录保留作对照，明确失效目录清空并可以完整撤销', () => {
    const original = [{
        unit_id: 'a', textbookNameZh: '对话', textbookVersion: '人教版', textbookStage: '初中',
        textbookGrade: '七年级', textbookVolume: '上册', textbookUnit: 'Unit 1', textbookLesson: 'Section A',
    }];
    const options = {
        inputType: 'textbook', unitIds: ['a'], fields: ['textbookVersion'],
        values: { textbookVersion: '外研版' }, mode: 'overwrite',
    };
    const unknown = api.systemInputBatchApply(original, options);
    assert.equal(unknown.configurations[0].textbookLesson, 'Section A');
    assert.ok(unknown.review.some(item => item.field === 'textbookLesson' && item.reason === 'parent-changed'));
    const known = api.systemInputBatchApply(original, {
        ...options,
        compatible: field => field === 'textbookUnit' ? false : ['textbookStage', 'textbookGrade', 'textbookVolume'].includes(field),
    });
    assert.equal(known.configurations[0].textbookUnit, undefined);
    assert.equal(known.configurations[0].textbookLesson, undefined);
    assert.equal(known.configurations[0].textbookNameZh, '对话');
    assert.deepEqual(plain(api.systemInputBatchUndo(known.configurations, known.changes).configurations), original);
});

test('填充下级时也校验它与已有上级的关系', () => {
    const result = api.systemInputBatchApply([{ unit_id: 'a', provinceId: '浙江省' }], {
        unitIds: ['a'], fields: ['cityId'], values: { cityId: '南京市' }, compatible: () => false,
    });
    assert.equal(result.configurations[0].cityId, undefined);
    assert.equal(result.review[0].reason, 'incompatible-parent');
});

test('新省份不能沿用明确失配的旧城市与区县，清空上级同样清空下级', () => {
    const original = [{ unit_id: 'a', provinceId: '浙江省', cityId: '杭州市', districtIds: ['西湖区'] }];
    const changed = api.systemInputBatchApply(original, {
        unitIds: ['a'], fields: ['provinceId'], values: { provinceId: '江苏省' }, mode: 'overwrite', compatible: () => false,
    });
    assert.deepEqual(plain(changed.configurations), [{ unit_id: 'a', provinceId: '江苏省' }]);
    assert.equal(changed.review.length, 2);
    const cleared = api.systemInputBatchApply(original, {
        unitIds: ['a'], fields: ['provinceId'], values: { provinceId: '' }, mode: 'overwrite',
    });
    assert.equal(cleared.configurations[0].cityId, undefined);
    assert.equal(cleared.configurations[0].districtIds, undefined);
});

test('更换模板名称不能遗留旧 ID，显式复制完整引用时保留新引用', () => {
    const scope = { paperCategory: '题型专项', provinceId: '湖北省', cityId: '武汉市' };
    const original = [{ unit_id: 'a', ...scope, platformTemplateName: '旧模板', platformTemplateId: 'old', platformTemplateVersion: 1 }];
    const result = api.systemInputBatchApply(original, {
        unitIds: ['a'], fields: ['platformTemplateName'], values: { platformTemplateName: '新模板' }, mode: 'overwrite',
    });
    assert.deepEqual(plain(result.configurations), [{ unit_id: 'a', ...scope, platformTemplateName: '新模板' }]);
    const full = api.systemInputBatchApply(original, {
        unitIds: ['a'], fields: ['platformTemplateName', 'platformTemplateId', 'platformTemplateVersion'],
        values: { platformTemplateName: '新模板', platformTemplateId: 'new', platformTemplateVersion: 2 }, mode: 'overwrite',
    });
    assert.deepEqual(plain(full.configurations), [{ unit_id: 'a', ...scope, platformTemplateName: '新模板', platformTemplateId: 'new', platformTemplateVersion: 2 }]);
});

test('旧 snake_case 配置不会被当成空白覆盖，修改和撤销都保留正确字段', () => {
    const original = [{ unit_id: 'a', paper_name: '历史套卷', grade_id: { id: 3, name: '三年级' }, stage_id: '小学', year: 2025 }];
    const fill = api.systemInputBatchApply(original, {
        unitIds: ['a'], fields: ['gradeId'], values: { gradeId: '四年级' },
    });
    assert.deepEqual(plain(fill.changes), []);
    const change = api.systemInputBatchApply(original, {
        unitIds: ['a'], fields: ['gradeId'], values: { gradeId: '四年级' }, mode: 'overwrite', compatible: () => true,
    });
    assert.equal(change.configurations[0].grade_id, undefined);
    assert.equal(change.configurations[0].gradeId, '四年级');
    assert.deepEqual(plain(api.systemInputBatchUndo(change.configurations, change.changes).configurations), original);
});

test('批量修改可撤销，撤销不会覆盖之后的独立手工编辑', () => {
    const original = [{ unit_id: 'a', paperName: '套卷', year: 2025, answerTimeMinutes: 20 }];
    const applied = api.systemInputBatchApply(original, {
        unitIds: ['a'], fields: ['year', 'answerTimeMinutes'], values: { year: 2026, answerTimeMinutes: 30 }, mode: 'overwrite',
    });
    assert.deepEqual(plain(api.systemInputBatchUndo(applied.configurations, applied.changes).configurations), original);
    applied.configurations[0].year = 2027;
    const undone = api.systemInputBatchUndo(applied.configurations, applied.changes);
    assert.equal(undone.configurations[0].year, 2027);
    assert.equal(undone.configurations[0].answerTimeMinutes, 20);
    assert.equal(undone.skipped.length, 1);
});

test('批量操作后单独改了关联城市，撤销不会只恢复省份而留下不匹配城市', () => {
    const applied = api.systemInputBatchApply([{ unit_id: 'a', provinceId: '江苏', cityId: '南京', year: 2025 }], {
        unitIds: ['a'], fields: ['provinceId', 'cityId', 'year'],
        values: { provinceId: '浙江', cityId: '杭州', year: 2026 }, mode: 'overwrite', compatible: () => true,
    });
    applied.configurations[0].cityId = '宁波';
    const undone = api.systemInputBatchUndo(applied.configurations, applied.changes);
    assert.deepEqual(plain(undone.configurations), [{ unit_id: 'a', provinceId: '浙江', cityId: '宁波', year: 2025 }]);
    assert.equal(undone.skipped.length, 2);
});

test('调用方修改返回值或补丁源，不会共享引用污染原配置和撤销记录', () => {
    const original = [{ unit_id: 'a', districtIds: [] }];
    const districts = [{ id: 3, name: '西湖区' }];
    const result = api.systemInputBatchApply(original, {
        unitIds: ['a'], fields: ['districtIds'], values: { districtIds: districts },
    });
    districts[0].name = '源变化';
    result.configurations[0].districtIds = [{ id: 4, name: '滨江区' }];
    assert.deepEqual(original, [{ unit_id: 'a', districtIds: [] }]);
    // The original unit has no parents, so the incompatible copied district
    // is cleared and its attempted value survives only in the review record.
    assert.equal(result.review[0].previousValue[0].name, '西湖区');
});
