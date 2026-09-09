const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

// 与 renderer-config.test.js 相同的加载方式：把渲染层脚本拼进一个
// vm 上下文，再暴露需要直测的课文录入函数。
function loadRendererTextbookFunctions() {
    const rendererDir = path.join(__dirname, '..', 'renderer');
    const html = fs.readFileSync(path.join(rendererDir, 'index.html'), 'utf8');
    const scripts = [...html.matchAll(/<script\s+src="([^"]+)"><\/script>/g)]
        .map(match => match[1])
        .filter(script => script !== 'vendor/wavesurfer.min.js' && script !== 'ui-components.js');
    const source = scripts
        .map(script => fs.readFileSync(path.join(rendererDir, script), 'utf8'))
        .join('\n')
        .replace(/\ninit\(\);\s*$/, '\n');
    const storage = new Map();
    const document = {
        documentElement: { dataset: {}, style: {} },
        getElementById: () => null,
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: () => {},
        body: { classList: { toggle: () => {}, add: () => {}, remove: () => {} }, dataset: {} },
        hidden: false,
    };
    const context = {
        console,
        document,
        window: {
            electronAPI: undefined,
            matchMedia: () => ({ matches: false }),
        },
        localStorage: {
            getItem: key => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, String(value)),
        },
        navigator: {},
        setTimeout,
        clearTimeout,
        URL,
        Blob,
        FormData,
        AbortController,
        ReadableStream,
        Map,
        Set,
    };
    vm.createContext(context);
    vm.runInContext(`${source}\nglobalThis.__textbookTests = { systemInputSuggestedConfiguration, systemInputTypeSelectionLocked, systemInputPaperCategoryLocked, systemInputUnitMissingFields, systemInputUnitConfiguration, systemInputNormalizeUnitConfiguration, systemInputTextbookFormValue, reviewItemIsTextbook, reviewTypeGroupIsTextbook };`, context);
    return context.__textbookTests;
}

test('课文按钮不再标注“待接入页面录入”', () => {
    const rendererDir = path.join(__dirname, '..', 'renderer');
    const html = fs.readFileSync(path.join(rendererDir, 'index.html'), 'utf8');
    assert.equal(/data-value="textbook"[^>]*待接入/.test(html), false);
    assert.ok(html.includes('id="system-input-textbook-name-zh"'));
    assert.ok(html.includes('id="system-input-textbook-name-en"'));
    assert.ok(html.includes('id="system-input-textbook-form"'));
    assert.ok(html.includes('id="system-input-textbook-version"'));
    assert.ok(html.includes('id="system-input-textbook-stage"'));
    assert.ok(html.includes('id="system-input-textbook-grade"'));
    assert.ok(html.includes('id="system-input-textbook-volume"'));
    assert.ok(html.includes('id="system-input-textbook-unit"'));
    assert.ok(html.includes('id="system-input-textbook-lesson"'));
    assert.ok(html.includes('id="system-input-textbook-sync-btn"'));
    assert.equal(html.includes('data-system-input-choice-group="system-input-textbook-form"'), false);
    assert.ok(html.includes('id="system-input-textbook-form-readout"'));
    assert.ok(html.includes('id="system-input-textbook-form-detected"'));
    assert.ok(html.includes('根据文档结构自动识别'));
    assert.ok(html.includes('data-system-input-picker-for="system-input-textbook-version"'));
    assert.equal(html.includes('list="system-input-textbook-form-options"'), false);
    assert.equal(html.includes('<datalist id="system-input-textbook-'), false);
    assert.ok(html.includes('id="system-input-type-detected"'));
    assert.ok(html.includes('id="system-input-paper-category-detected"'));
});

test('课文形式不再作为用户可选字段，并优先展示解析器识别结果', () => {
    const api = loadRendererTextbookFunctions();
    assert.equal(api.systemInputTextbookFormValue(
        { textbookForm: '同步课文' },
        { evidence: { textbook_form: '角色扮演' } },
    ), '角色扮演');
    assert.equal(api.systemInputTextbookFormValue(
        { textbookForm: '角色扮演' },
        { evidence: { textbook_form: '同步课文' } },
    ), '同步课文');
});

test('识别结果干净时锁定录入类型选择，冲突时解锁', () => {
    const api = loadRendererTextbookFunctions();
    assert.equal(api.systemInputTypeSelectionLocked({
        suggested_configuration: { input_type: 'textbook', input_type_status: 'suggested' },
    }), true);
    assert.equal(api.systemInputTypeSelectionLocked({
        suggested_configuration: { input_type: 'textbook', input_type_status: 'user_override' },
    }), true);
    assert.equal(api.systemInputTypeSelectionLocked({
        suggested_configuration: { input_type: 'paper', input_type_status: 'conflict' },
    }), false);
    assert.equal(api.systemInputTypeSelectionLocked({
        suggested_configuration: { input_type: 'vocabulary', input_type_status: 'suggested' },
    }), false);
    assert.equal(api.systemInputTypeSelectionLocked({}), false);
});

test('试卷分类识别结果只在干净状态下锁定', () => {
    const api = loadRendererTextbookFunctions();
    assert.equal(api.systemInputPaperCategoryLocked({
        suggested_configuration: { paper_category: '题型专项', paper_category_status: 'suggested' },
    }), true);
    assert.equal(api.systemInputPaperCategoryLocked({
        suggested_configuration: { paper_category: '听说考试', paper_category_status: 'user_override' },
    }), true);
    assert.equal(api.systemInputPaperCategoryLocked({
        suggested_configuration: { paper_category: '题型专项', paper_category_status: 'conflict' },
    }), false);
    assert.equal(api.systemInputPaperCategoryLocked({
        suggested_configuration: { paper_category_status: 'not_applicable' },
    }), false);
});

test('课文单元缺用户字段校验，自动识别的课文形式不参与必填校验', () => {
    const api = loadRendererTextbookFunctions();
    const complete = {
        textbookNameZh: 'Section A',
        textbookNameEn: 'How do we get to know each other?',
        textbookVersion: '人教版',
        textbookStage: '初中',
        textbookGrade: '七年级',
        textbookVolume: '上册',
        textbookUnit: 'Unit 1',
        textbookLesson: 'Section A',
    };
    // vm 上下文里的数组来自另一个 realm，用 Array.from 落回当前 realm。
    assert.deepEqual(Array.from(api.systemInputUnitMissingFields(complete, 'textbook')), []);
    const missing = api.systemInputUnitMissingFields({
        ...complete,
        textbookNameZh: '',
        textbookLesson: '  ',
    }, 'textbook');
    assert.deepEqual(Array.from(missing), ['课文名称（中文）', '课时']);
    // 试卷校验不受课文分支影响
    assert.deepEqual(Array.from(api.systemInputUnitMissingFields(complete, 'paper')), [
        '试卷名称', '试卷分类', '平台题型模板', '省份', '城市', '学段', '年级', '年份', '答题时间',
    ]);
});

test('课文单元配置归一化保留课文键并剔除空值', () => {
    const api = loadRendererTextbookFunctions();
    const normalized = api.systemInputNormalizeUnitConfiguration({
        unit_id: 'unit-1',
        textbookNameZh: 'Section A',
        textbookForm: '  ',
        textbookNameEn: 'How do we get to know each other?',
        paperName: 'must-not-leak',
    }, { unit_id: 'unit-1' }, 0, 1, [], 'textbook');
    assert.equal(normalized.textbookNameZh, 'Section A');
    assert.equal(normalized.textbookNameEn, 'How do we get to know each other?');
    assert.equal(normalized.textbookForm, undefined);
    assert.equal(normalized.paperName, undefined);
    // 试卷路径不受影响
    const paperNormalized = api.systemInputNormalizeUnitConfiguration({
        unit_id: 'unit-1',
        paperName: '外研9上-U6',
        textbookNameZh: 'must-not-leak',
    }, { unit_id: 'unit-1' }, 0, 1, [], 'paper');
    assert.equal(paperNormalized.paperName, '外研9上-U6');
    assert.equal(paperNormalized.textbookNameZh, undefined);
});

test('旧版对象形态的课文目录值和课文形式会归一化为页面文本', () => {
    const api = loadRendererTextbookFunctions();
    const normalized = api.systemInputNormalizeUnitConfiguration({
        unit_id: 'unit-legacy-textbook',
        textbookNameZh: 'Section A',
        textbook_form: { id: 'roleplay', name: '角色扮演' },
        textbook_version: { id: 'pep', name: '人教版' },
        textbook_stage: { id: 2, name: '初中' },
        textbook_grade: { id: 7, name: '七年级' },
        textbook_volume: { id: 'upper', name: '上册' },
        textbook_unit: { name: 'Unit 1' },
        textbook_lesson: { name: 'Section A' },
    }, { unit_id: 'unit-legacy-textbook' }, 0, 1, [], 'textbook');
    assert.equal(normalized.textbookForm, '角色扮演');
    assert.equal(normalized.textbookVersion, '人教版');
    assert.equal(normalized.textbookStage, '初中');
    assert.equal(normalized.textbookGrade, '七年级');
    assert.equal(normalized.textbookVolume, '上册');
    assert.equal(normalized.textbookUnit, 'Unit 1');
    assert.equal(normalized.textbookLesson, 'Section A');
    assert.equal('textbook_form' in normalized, false);
});

test('课文核对视图识别课文条目并隐藏分值', () => {
    const api = loadRendererTextbookFunctions();
    const textbookItem = { doc_type: '课文跟读', item_type: '句子跟读' };
    assert.equal(api.reviewItemIsTextbook(textbookItem), true);
    assert.equal(api.reviewTypeGroupIsTextbook({ name: '句子跟读', items: [textbookItem] }), true);
    assert.equal(api.reviewTypeGroupIsTextbook({ name: '模仿朗读', items: [{ doc_type: '模仿朗读' }] }), false);
});
