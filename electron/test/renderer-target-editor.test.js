const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const fixture = require('./helpers/target-editor-fixture.cjs');
function editor(t, type = 'paper', count = 3, storage = {}) {
    const dom = new JSDOM(fixture.html, { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true });
    const w = dom.window;
    w.HTMLElement.prototype.scrollIntoView = function () {};
    w.matchMedia = () => ({ matches: false, addEventListener() {} });
    w.CSS = { escape: value => String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };
    Object.entries(storage).forEach(([key, value]) => w.localStorage.setItem(key, value));
    t.after(async () => { await new Promise(resolve => setTimeout(resolve, 20)); w.close(); });
    const context = dom.getInternalVMContext();
    const run = source => vm.runInContext(source, context);
    run(fixture.prelude());
    for (const script of fixture.scripts) run(fs.readFileSync(path.join(fixture.renderer, script), 'utf8'));
    run(fixture.setup(fixture.workspace(type, count)));
    const byId = id => w.document.getElementById(id);
    const change = (input, value) => { input.value = value; input.dispatchEvent(new w.Event('change', { bubbles: true })); };
    return { w, run, byId, change, read: () => JSON.parse(JSON.stringify(w.__readTargets())) };
}

test('batch overview edits only checked units and chosen fields; undo keeps later manual edits', t => {
    const { w, byId, change, read } = editor(t);
    assert.equal(byId('target-rows').children.length, 3);
    assert.equal(byId('target-editor-form-source').hidden, true);
    const checks = byId('target-rows').querySelectorAll('input[type=checkbox]'); checks[0].click(); checks[2].click();
    byId('target-batch-open').click();
    const checkbox = byId('target-batch-fields').querySelector('[aria-label="批量修改平台题型模板"]'); checkbox.click();
    const value = byId('target-batch-fields').querySelector('[aria-label="批量修改 · 平台题型模板"]');
    value.value = '听后回答'; value.dispatchEvent(new w.Event('input', { bubbles: true }));
    change(byId('target-batch-mode'), 'overwrite');
    byId('target-batch-apply').click();
    assert.deepEqual(read().map(row => row.platformTemplateName), ['听后回答', '模仿朗读', '听后回答']);
    assert.deepEqual(read().map(row => row.paperName), ['Unit 1 听说测试', 'Unit 2 听说测试', 'Unit 3 听说测试']);
    // Platform template is a catalogue selection, so manual edits must use a
    // value exposed by the shared picker rather than an arbitrary string.
    change(byId('target-rows').querySelector('[data-unit-id="unit-1"][data-field="platformTemplateName"]'), '听后应答');
    byId('target-undo').click();
    assert.deepEqual(read().map(row => row.platformTemplateName), ['听后应答', '模仿朗读', '模仿朗读']);
    assert.equal(w.__saved.length, 0);
});

test('incomplete drafts survive close and a fresh renderer; explicit save alone enables input', async t => {
    const first = editor(t);
    first.change(first.byId('target-rows').querySelector('[data-unit-id="unit-2"][data-field="paperName"]'), '');
    first.w.closeSystemInputConfigDrawer();
    assert.equal(first.w.__saved.length, 0);
    const storage = Object.fromEntries(Array.from({length: first.w.localStorage.length}, (_, i) => { const key = first.w.localStorage.key(i); return [key, first.w.localStorage.getItem(key)]; }));
    const second = editor(t, 'paper', 3, storage);
    assert.equal(second.read()[1].paperName, undefined);
    assert.match(second.byId('target-draft-status').textContent, /恢复/);
    await second.w.submitSystemInputConfiguration();
    assert.equal(second.w.__saved.length, 0);
    second.change(second.byId('target-rows').querySelector('[data-unit-id="unit-2"][data-field="paperName"]'), '补好的名称');
    // Select actual supported stage / grade identifiers from the renderer choices.
    second.run(`for (const [id, config] of systemInputUnitDrafts) { config.stageId = {id:SYSTEM_INPUT_STAGES[0].id,name:SYSTEM_INPUT_STAGES[0].name}; const grade = SYSTEM_INPUT_GRADES.find(item=>item.stageId===config.stageId.id); config.gradeId={id:grade.id,name:grade.name}; } populateSystemInputUnitForm(currentWorkspace.system_input);`);
    await second.w.submitSystemInputConfiguration();
    assert.equal(second.w.__saved.length, 1, JSON.stringify({messages:second.w.__messages, errors:second.byId('system-input-validation-message').textContent, rows:second.read()}));
    assert.equal(second.w.__saved[0].configuration.delivery_mode, 'audio_and_input');
    assert.equal(second.byId('system-input-drawer').hidden, true);
});

test('ordinary checklist selection is transient and is not restored as a default', t => {
    const first = editor(t, 'paper', 3);
    first.byId('target-rows').querySelector('input[type="checkbox"]').click();
    first.w.closeSystemInputConfigDrawer();
    const storage = Object.fromEntries(Array.from({ length: first.w.localStorage.length }, (_, index) => {
        const key = first.w.localStorage.key(index);
        return [key, first.w.localStorage.getItem(key)];
    }));
    const second = editor(t, 'paper', 3, storage);
    assert.equal([...second.byId('target-rows').querySelectorAll('input[type="checkbox"]')]
        .some(input => input.checked), false);
});

test('batch tool cards expose readable copy without a default selected state', t => {
    const { byId } = editor(t, 'paper', 3);
    ['target-common-open', 'target-template-open', 'target-batch-open'].forEach(id => {
        const card = byId(id);
        assert.equal(card.classList.contains('is-selected'), false, `${id} must not look selected on first render`);
        assert.equal(card.hasAttribute('aria-pressed'), false, `${id} must not advertise a pressed state`);
        assert.ok(card.querySelector('.target-action-copy strong')?.textContent.trim(), `${id} needs a visible title`);
        assert.ok(card.querySelector('.target-action-copy small')?.textContent.trim(), `${id} needs a visible description`);
    });
});

test('target editor close controls use a stable authored icon with an accessible label', t => {
    const { byId } = editor(t, 'paper', 3);
    assert.ok(byId('system-input-drawer-close').querySelector('.target-close-icon'));
    byId('target-common-open').click();
    assert.ok(byId('target-common-close').querySelector('.target-close-icon'));
    byId('target-common-close').click();
    byId('target-rows').querySelector('input[type="checkbox"]').click();
    byId('target-batch-open').click();
    assert.ok(byId('target-batch-close').querySelector('.target-close-icon'));
    assert.equal(byId('target-batch-close').getAttribute('aria-label'), '关闭批量修改');
    assert.equal(byId('target-editor-form-source').hidden, true);
});

test('vocabulary targets show the adapter boundary instead of paper controls', t => {
    const { w, byId } = editor(t, 'vocabulary', 3);
    const overview = byId('target-overview');
    const rows = [...byId('target-rows').children];

    assert.equal(overview.hidden, false);
    assert.equal(byId('target-editor-form-source').hidden, true);
    assert.equal(byId('target-overview-count').textContent, '3 条词汇 · 3 条待处理');
    assert.match(overview.querySelector('.target-overview-head p').textContent, /适配器/);
    assert.match(byId('system-input-drawer').querySelector('.system-input-drawer-heading p').textContent, /适配器/);
    assert.deepEqual(rows.map(row => row.querySelectorAll('[data-field]').length), [0, 0, 0]);
    assert.ok(rows.every(row => row.querySelector('.target-status')?.textContent === '待接入外部能力'));
    assert.ok(rows.every(row => !row.querySelector('.target-row-actions')));
    ['target-common-open', 'target-template-open', 'target-batch-open', 'system-input-save-template-btn'].forEach(id => assert.equal(byId(id).hidden, true, id));
    assert.equal(byId('target-select-visible').closest('.target-selection').hidden, true);
    assert.equal(byId('target-next-issue').hidden, true);

    assert.equal(w.targetEditorApplyTemplate({ name: '不应应用', configuration: { year: 2030 } }), false);
    assert.match(w.__messages.at(-1).message, /适配器/);
});

test('switching input types refreshes the shared target list immediately', t => {
    const { w, byId } = editor(t, 'paper', 3);
    const textbookChoice = w.document.querySelector(
        '[data-system-input-choice-group="system-input-type"] [data-value="textbook"]',
    );

    assert.ok(textbookChoice);
    textbookChoice.click();

    assert.equal(byId('system-input-type').value, 'textbook');
    assert.equal(byId('target-overview').dataset.inputType, 'textbook');
    assert.equal(byId('system-input-optional-fields').hidden, true);
    assert.equal(byId('system-input-optional-fields').getAttribute('aria-hidden'), 'true');
    assert.deepEqual([...byId('target-columns').children].map(node => node.textContent.trim()), [
        '选择 / 文档内容', '课文名称（中文）', '课文名称（英文）', '教材单元', '课时', '状态 / 操作',
    ]);
    assert.deepEqual([...byId('target-rows').children].map(row =>
        [...row.querySelectorAll('[data-field]')].map(node => node.dataset.field)), [
        ['textbookNameZh', 'textbookNameEn', 'textbookUnit', 'textbookLesson'],
        ['textbookNameZh', 'textbookNameEn', 'textbookUnit', 'textbookLesson'],
        ['textbookNameZh', 'textbookNameEn', 'textbookUnit', 'textbookLesson'],
    ]);
    assert.equal(byId('target-overview-count').textContent, '3 条课文 · 3 条待处理');
});

test('primary textbook form shows a read-only auto-detected form tag', t => {
    const { w, byId } = editor(t, 'textbook', 1);
    assert.equal(byId('system-input-textbook-form-readout').hidden, false);
    assert.equal(byId('system-input-textbook-form-detected').textContent, '同步课文');
    assert.equal(byId('system-input-textbook-form-note').textContent, '文档结构自动识别');
    assert.equal(byId('system-input-textbook-form').type, 'hidden');
    assert.equal(w.document.querySelector('[data-system-input-choice-group="system-input-textbook-form"]'), null);
});

test('a single vocabulary target stays in the non-editable overview', t => {
    const { byId } = editor(t, 'vocabulary', 1);
    assert.equal(byId('system-input-drawer').classList.contains('is-single-target'), false);
    assert.equal(byId('target-overview').hidden, false);
    assert.equal(byId('target-editor-form-source').hidden, true);
});

test('detached shared child editors expose dialog semantics', t => {
    const { byId } = editor(t, 'paper', 3);
    ['system-input-optional-fields', 'system-input-boundary-review'].forEach(id => {
        const node = byId(id);
        assert.equal(node.getAttribute('role'), 'dialog', `${id} should be a dialog`);
        assert.equal(node.getAttribute('aria-modal'), 'true', `${id} should be modal`);
        assert.ok(node.getAttribute('aria-labelledby'), `${id} should name the dialog`);
    });
});

test('application template entry point preserves the shared editor result', async t => {
    const paper = editor(t, 'paper', 1);
    paper.run(`systemInputTemplates = [{app_template_id:'paper-template', input_type:'paper', name:'试卷方案', configuration:{year:2030}}]; setSystemInputAppTemplateSelection(systemInputTemplates[0]);`);
    assert.equal(await paper.w.applySystemInputAppTemplate(), true);

    const vocabulary = editor(t, 'vocabulary', 1);
    vocabulary.run(`systemInputTemplates = [{app_template_id:'vocabulary-template', input_type:'vocabulary', name:'词汇方案', configuration:{}}]; setSystemInputAppTemplateSelection(systemInputTemplates[0]);`);
    assert.equal(await vocabulary.w.applySystemInputAppTemplate(), false);
});

test('closing the drawer resets every child target modal accessibility state', t => {
    const { w, byId } = editor(t, 'paper', 3);
    byId('target-rows').querySelector('input[type="checkbox"]').click();
    byId('target-batch-open').click();
    assert.equal(byId('target-batch').getAttribute('aria-hidden'), 'false');

    w.closeSystemInputConfigDrawer();

    ['target-common', 'target-batch', 'target-detail', 'target-source-preview', 'system-input-optional-fields', 'system-input-boundary-review']
        .forEach(id => {
            const node = byId(id);
            assert.equal(node.hidden, true, `${id} should be hidden`);
            assert.equal(node.getAttribute('aria-hidden'), 'true', `${id} should be aria-hidden`);
            assert.equal(node.classList.contains('is-open'), false, `${id} should not be open`);
        });
    assert.equal(w.document.body.classList.contains('target-modal-open'), false);
});

test('empty target workspaces remove the batch rail instead of leaving a blank column', t => {
    const { byId } = editor(t, 'paper', 0);
    assert.equal(byId('target-rows').children.length, 0);
    assert.equal(byId('target-overview').classList.contains('is-empty'), true);
    assert.equal(byId('target-overview').querySelector('.target-action-bar').hidden, true);
    assert.equal(byId('target-empty').hidden, false);
});

test('target overview keeps named tools before the list at every width', () => {
    const css = fs.readFileSync(path.join(fixture.renderer, 'target-editor-overrides.css'), 'utf8');
    assert.match(css, /\.target-overview\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0,\s*1fr\);/);
    assert.match(css, /grid-template-areas:\s*"head"\s+"actions"\s+"main";/);
    assert.match(css, /\.target-overview-main\s*\{[\s\S]*?align-content:\s*start;/);
    assert.match(css, /\.target-action-bar\s*\{[\s\S]*?grid-template-columns:\s*repeat\(3,\s*minmax\(0,\s*1fr\)\);/);
    assert.match(css, /@container target-editor \(max-width: 620px\)[\s\S]*?grid-template-columns:\s*minmax\(0,\s*1fr\);/);
});

test('detached target detail dialog has viewport breakpoints outside the drawer container', () => {
    const css = fs.readFileSync(path.join(fixture.renderer, 'target-editor.css'), 'utf8');
    assert.match(css, /@media \(max-width: 820px\)[\s\S]*?\.target-modal \.target-field-grid\s*\{\s*grid-template-columns:\s*repeat\(2,/);
    assert.match(css, /@media \(max-width: 520px\)[\s\S]*?\.target-modal \.target-field-grid\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\);/);
    assert.match(css, /\.target-modal \.target-modal-card\s*\{\s*width:\s*calc\(100vw - 20px\);/);
});

test('target overview DOM order follows the visual reading order for keyboard users', t => {
    const { byId } = editor(t, 'paper', 3);
    const overview = byId('target-overview');
    assert.deepEqual([...overview.children].map(node => node.className), [
        'target-overview-head',
        'target-action-bar',
        'target-overview-main',
    ]);
});

test('empty target filters hide the table shell and keep the empty state actionable', t => {
    const { w, byId } = editor(t, 'paper', 3);
    const overview = byId('target-overview');
    const search = byId('target-search');
    search.value = '不存在的目标';
    search.dispatchEvent(new w.Event('input', { bubbles: true }));
    assert.equal(overview.querySelector('.target-table').hidden, true);
    assert.equal(byId('target-empty').hidden, false);
    assert.equal(byId('target-select-visible').disabled, true);
    assert.equal(overview.getAttribute('aria-labelledby'), 'target-overview-title');
    assert.equal(byId('target-overview-title').tagName, 'H3');
});

test('textbook defaults preserve each name and target path; source and manual catalog review remain accessible', t => {
    const { w, byId, run, change, read } = editor(t, 'textbook');
    assert.equal(byId('target-rows').children.length, 3);
    change(byId('target-rows').querySelector('[data-unit-id="unit-3"][data-field="textbookNameEn"]'), 'My own title');
    run(`document.getElementById('target-common').open = true;`);
    byId('target-common-apply').click();
    assert.deepEqual(read().map(row => row.textbookUnit), ['Unit 1', 'Unit 2', 'Unit 3']);
    assert.equal(read()[2].textbookNameEn, 'My own title');
    byId('target-rows').querySelector('.target-source > button').click();
    assert.match(byId('target-source-text').textContent, /Emma/);
    assert.equal(w.__saved.length, 0);
});

test('source preview discloses when long content is intentionally truncated', t => {
    const { byId, run } = editor(t, 'textbook');
    run(`currentWorkspace.system_input.content_segments = Array.from({length: 10}, (_, index) => ({
        unit_id: 'unit-1', segment_id: 'long-' + index, raw_text: '原文片段 ' + (index + 1), ordinal: index,
    }));`);
    byId('target-rows').querySelector('.target-source > button').click();
    assert.match(byId('target-source-description').textContent, /显示前 8 段，共 10 段/);
    assert.doesNotMatch(byId('target-source-text').textContent, /原文片段 9/);
});

test('single target uses the shared list and save failure retains edits', async t => {
    const { w, byId, change, read } = editor(t, 'textbook', 1);
    assert.equal(byId('target-overview').hidden, false);
    assert.equal(byId('target-rows').children.length, 1);
    assert.equal(byId('target-editor-form-source').hidden, true);
    change(byId('target-rows').querySelector('[data-field="textbookNameZh"]'), '新的课文名');
    w.__saveFailure = true;
    await w.submitSystemInputConfiguration();
    assert.equal(w.__saved.length, 1);
    assert.equal(byId('system-input-drawer').hidden, false);
    assert.equal(read()[0].textbookNameZh, '新的课文名');
});

test('single target keeps the shared list visible while batch editing', t => {
    const { w, byId } = editor(t, 'paper', 1);
    const drawer = byId('system-input-drawer');
    const panel = drawer.querySelector('.system-input-drawer-panel');
    const detail = byId('target-editor-form-source');
    assert.equal(drawer.classList.contains('is-single-target'), false);
    assert.equal(drawer.classList.contains('is-single-target-modal-open'), false);
    assert.equal(panel.hidden, false);
    assert.equal(panel.getAttribute('aria-hidden'), 'false');
    assert.equal(drawer.getAttribute('aria-hidden'), 'false');
    assert.equal(detail.hidden, true);
    assert.equal(detail.getAttribute('aria-hidden'), 'true');

    byId('target-batch-open').click();
    assert.equal(byId('target-batch').hidden, false);
    assert.equal(panel.hidden, false);
    assert.equal(drawer.getAttribute('aria-hidden'), 'true');
    byId('target-batch-close').click();
    assert.equal(byId('target-batch').hidden, true);
    assert.equal(panel.hidden, false);
    assert.equal(panel.getAttribute('aria-hidden'), 'false');
    assert.equal(drawer.getAttribute('aria-hidden'), 'false');
    byId('system-input-drawer-close').click();
    assert.equal(drawer.hidden, true);
    assert.doesNotMatch(w.document.body.className, /target-modal-open/);
});

test('closing a target modal returns focus to its trigger before hiding it', t => {
    const { w, byId } = editor(t, 'paper', 1);
    const trigger = byId('target-batch-open');
    trigger.focus();
    trigger.click();
    const close = byId('target-batch-close');
    close.focus();
    close.click();
    assert.equal(w.document.activeElement, trigger);
    assert.equal(byId('target-batch').getAttribute('aria-hidden'), 'true');
});

test('single target exposes the same complete batch field grid as multiple targets', t => {
    const expected = {
        paper: ['试卷名称', '省份', '城市', '区县', '学段', '年级', '平台题型模板', '年份', '答题时间（分钟）'],
        textbook: ['课文名称（中文）', '课文名称（英文）', '教材版本', '学段', '年级', '册别', '教材单元', '课时'],
    };
    Object.entries(expected).forEach(([inputType, labels]) => {
        const { byId } = editor(t, inputType, 1);
        byId('target-batch-open').click();
        const fields = [...byId('target-batch-fields').querySelectorAll('.target-field')];
        assert.deepEqual(fields.map(field => field.querySelector(':scope > span')?.textContent), labels, inputType);
        assert.equal(fields.filter(field => field.querySelector('input[type="checkbox"]')).length, labels.length, inputType);
        if (inputType === 'paper') {
            assert.equal(byId('target-batch-classification').hidden, false);
            assert.equal(byId('target-batch-category-tag').textContent, '题型专项');
            assert.equal(byId('target-batch-category-note').textContent, '当前分类');
            assert.equal(fields.some(field => field.dataset.targetField === 'paperCategory'), false);
            assert.equal(fields.some(field => field.dataset.targetField === 'paperType'), false);
        } else {
            assert.equal(byId('target-batch-textbook-form').hidden, false);
            assert.equal(byId('target-batch-textbook-form-tag').textContent, '同步课文');
            assert.equal(byId('target-batch-textbook-form-note').textContent, '文档结构自动识别');
            assert.equal(fields.some(field => field.dataset.targetField === 'textbookForm'), false);
            byId('target-batch-close').click();
            byId('target-rows').querySelector('.target-row-actions button').click();
            assert.deepEqual(
                [...byId('target-detail-fields').querySelectorAll('.target-field')]
                    .map(field => field.querySelector(':scope > span')?.textContent),
                labels,
            );
        }
    });
});

test('paper target dialogs materialize year and category-specific answer-time defaults', t => {
    const special = editor(t, 'paper', 1);
    special.run(`const current = {...systemInputUnitDrafts.get('unit-1')};
        delete current.year;
        delete current.answerTimeMinutes;
        systemInputUnitDrafts.set('unit-1', current);
        refreshSystemInputTargetEditor();`);
    assert.equal(special.run('systemInputUnitDrafts.get("unit-1").year'), 2026);
    assert.equal(special.run('systemInputUnitDrafts.get("unit-1").answerTimeMinutes'), 20);
    const detailAction = [...special.byId('target-rows').querySelectorAll('.target-row-actions button')]
        .find(button => button.textContent.includes('编辑详情'));
    detailAction.click();
    const detailField = label => [...special.byId('target-detail-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);
    assert.equal(detailField('年份').querySelector('input').value, '2026');
    assert.equal(detailField('答题时间（分钟）').querySelector('input').value, '20');
    special.byId('target-detail-close').click();

    const listening = editor(t, 'paper', 1);
    listening.run(`const current = {...systemInputUnitDrafts.get('unit-1'), paperCategory:'听说考试'};
        delete current.year;
        delete current.answerTimeMinutes;
        systemInputUnitDrafts.set('unit-1', current);
        refreshSystemInputTargetEditor();`);
    listening.byId('target-batch-open').click();
    const batchField = label => [...listening.byId('target-batch-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);
    assert.equal(batchField('年份').querySelector('input:not([type="checkbox"])').value, '2026');
    assert.equal(batchField('答题时间（分钟）').querySelector('input:not([type="checkbox"])').value, '60');
});

test('single-target missing-field entry opens direct detail editing', t => {
    const cases = [
        { inputType: 'paper', missing: 'cityId', targetLabel: '城市' },
        { inputType: 'textbook', missing: 'textbookUnit', targetLabel: '教材单元' },
    ];
    cases.forEach(({ inputType, missing, targetLabel }) => {
        const { byId, run } = editor(t, inputType, 1);
        run(`const current = {...systemInputUnitDrafts.get('unit-1')}; delete current.${missing}; systemInputUnitDrafts.set('unit-1', current); refreshSystemInputTargetEditor();`);
        const row = [...byId('target-rows').children]
            .find(candidate => candidate.querySelector('[data-unit-id="unit-1"]'));
        const action = [...row.querySelectorAll('.target-row-actions button')]
            .find(button => button.textContent.includes('编辑详情'));
        assert.ok(action, inputType);
        action.click();
        assert.equal(byId('target-detail').hidden, false, inputType);
        assert.equal(byId('target-batch').hidden, true, inputType);
        const fields = [...byId('target-detail-fields').querySelectorAll('.target-field')];
        assert.equal(fields.some(field => field.querySelector('input[type="checkbox"]')), false, inputType);
        const target = fields.find(field => field.querySelector(':scope > span')?.textContent === targetLabel);
        assert.equal(target.querySelector('.target-select-trigger')?.disabled, false, inputType);
    });
});

test('multi-target missing-field entry opens the detail editor instead of batch modification', t => {
    const { w, byId, run } = editor(t, 'paper', 2);
    run("const current = {...systemInputUnitDrafts.get('unit-2')}; delete current.paperName; delete current.cityId; systemInputUnitDrafts.set('unit-2', current); refreshSystemInputTargetEditor();");
    const row = [...byId('target-rows').children]
        .find(candidate => candidate.querySelector('[data-unit-id="unit-2"]'));
    const action = [...row.querySelectorAll('.target-row-actions button')]
        .find(button => button.textContent.includes('编辑详情'));
    assert.ok(action);
    action.click();

    assert.equal(byId('target-detail').hidden, false);
    assert.equal(byId('target-batch').hidden, true);
    assert.equal(byId('target-detail-title').textContent, '编辑详情 · 第 2 套 · Unit 2 听说测试');
    const fields = [...byId('target-detail-fields').querySelectorAll('.target-field')];
    assert.equal(fields.some(field => field.querySelector('input[type="checkbox"]')), false);
    const labels = fields.map(field => field.querySelector(':scope > span')?.textContent);
    assert.ok(labels.includes('试卷名称'));
    assert.ok(labels.includes('省份'));
    assert.ok(labels.includes('城市'));

    const name = byId('target-detail-fields').querySelector('[data-target-field="paperName"] input');
    name.value = '第二套补好的试卷';
    name.dispatchEvent(new w.Event('input', { bubbles: true }));
    assert.equal(run('systemInputUnitDrafts.get("unit-2").paperName'), '第二套补好的试卷');

    byId('target-detail-done').click();
    assert.equal(byId('target-detail').hidden, true);
    assert.equal(byId('target-batch').hidden, true);
    // 焦点应回到所在行的操作按钮上（程序化 click 不会聚焦，回退到行内第一个
    // 操作按钮“编辑详情”；真实鼠标点击时会回到实际点击的那个按钮）。
    assert.ok(w.document.activeElement?.closest?.('.target-row-actions')
        || /编辑详情/.test(w.document.activeElement?.textContent || ''));
});

test('target detail paper cascades stay scoped to the selected region and clear stale descendants', t => {
    const { w, byId, run } = editor(t, 'paper', 2);
    run(`const current = {...systemInputUnitDrafts.get('unit-2'), districtIds: [{id:440106,name:'天河区'}]};
        delete current.cityId;
        systemInputUnitDrafts.set('unit-2', current);
        refreshSystemInputTargetEditor();`);
    const row = [...byId('target-rows').children]
        .find(candidate => candidate.querySelector('[data-unit-id="unit-2"]'));
    const action = [...row.querySelectorAll('.target-row-actions button')]
        .find(button => button.textContent.includes('编辑详情'));
    assert.ok(action);
    action.click();

    const fields = () => [...byId('target-detail-fields').querySelectorAll('.target-field')];
    const field = label => fields().find(node => node.querySelector(':scope > span')?.textContent === label);
    const menuLabels = label => {
        const wrap = field(label);
        const search = wrap.querySelector('.target-district-search');
        if (search) {
            search.dispatchEvent(new w.Event('focus', { bubbles: true }));
            const labels = [...wrap.querySelectorAll('.target-district-menu .target-select-option')].map(node => node.textContent);
            return labels;
        }
        const wrapper = wrap.querySelector('.target-select');
        const trigger = wrapper.querySelector('.target-select-trigger');
        trigger.click();
        const labels = [...wrapper.querySelectorAll('.target-select-option')].map(node => node.textContent);
        trigger.click();
        return labels;
    };
    const choose = (label, optionText) => {
        const wrap = field(label);
        const search = wrap.querySelector('.target-district-search');
        if (search) {
            search.dispatchEvent(new w.Event('focus', { bubbles: true }));
            const option = [...wrap.querySelectorAll('.target-district-menu .target-select-option')]
                .find(node => node.textContent === optionText);
            assert.ok(option, `${label} should expose ${optionText}`);
            option.click();
            return;
        }
        const wrapper = wrap.querySelector('.target-select');
        wrapper.querySelector('.target-select-trigger').click();
        const option = [...wrapper.querySelectorAll('.target-select-option')]
            .find(node => node.textContent === optionText);
        assert.ok(option, `${label} should expose ${optionText}`);
        option.click();
    };

    assert.equal(field('城市').querySelector('.target-select-trigger').disabled, false);
    assert.deepEqual(menuLabels('城市'), ['广州市', '深圳市']);
    choose('省份', '湖南省');
    assert.equal(run('systemInputCascadeValuePresent(systemInputUnitDrafts.get("unit-2").cityId)'), false);
    assert.equal(run('systemInputUnitDrafts.get("unit-2").districtIds.length'), 0);
    assert.deepEqual(menuLabels('城市'), ['长沙市']);
    choose('城市', '长沙市');
    assert.equal(field('区县').querySelector('.target-district-search').disabled, false);
    assert.deepEqual(menuLabels('区县'), ['芙蓉区']);
    choose('区县', '芙蓉区');
    assert.equal(run('systemInputUnitDrafts.get("unit-2").cityId.name'), '长沙市');
    assert.equal(run('systemInputUnitDrafts.get("unit-2").districtIds[0].name'), '芙蓉区');
});

test('target detail textbook cascades unlock the directory path one parent at a time', t => {
    const { byId, run } = editor(t, 'textbook', 2);
    run(`const current = {...systemInputUnitDrafts.get('unit-2')};
        ['textbookStage', 'textbookGrade', 'textbookVolume', 'textbookUnit', 'textbookLesson']
            .forEach(key => delete current[key]);
        systemInputUnitDrafts.set('unit-2', current);
        refreshSystemInputTargetEditor();`);
    const row = [...byId('target-rows').children]
        .find(candidate => candidate.querySelector('[data-unit-id="unit-2"]'));
    const action = [...row.querySelectorAll('.target-row-actions button')]
        .find(button => button.textContent.includes('编辑详情'));
    assert.ok(action);
    action.click();

    const fields = () => [...byId('target-detail-fields').querySelectorAll('.target-field')];
    const field = label => fields().find(node => node.querySelector(':scope > span')?.textContent === label);
    const menuLabels = label => {
        const wrapper = field(label).querySelector('.target-select');
        const trigger = wrapper.querySelector('.target-select-trigger');
        trigger.click();
        const labels = [...wrapper.querySelectorAll('.target-select-option')].map(node => node.textContent);
        trigger.click();
        return labels;
    };
    const choose = (label, optionText) => {
        const wrapper = field(label).querySelector('.target-select');
        wrapper.querySelector('.target-select-trigger').click();
        const option = [...wrapper.querySelectorAll('.target-select-option')]
            .find(node => node.textContent === optionText);
        assert.ok(option, `${label} should expose ${optionText}`);
        option.click();
    };

    assert.equal(field('学段').querySelector('.target-select-trigger').disabled, false);
    assert.equal(field('年级').querySelector('.target-select-trigger').disabled, true);
    assert.equal(field('教材单元').querySelector('.target-select-trigger').disabled, true);
    choose('学段', '初中');
    assert.equal(field('年级').querySelector('.target-select-trigger').disabled, false);
    choose('年级', '七年级');
    assert.equal(field('册别').querySelector('.target-select-trigger').disabled, false);
    choose('册别', '上册');
    assert.deepEqual(menuLabels('教材单元'), Array.from({length: 10}, (_, index) => `Unit ${index + 1}`));
    choose('教材单元', 'Unit 2');
    assert.equal(field('课时').querySelector('.target-select-trigger').disabled, false);
    assert.deepEqual(menuLabels('课时'), ['Section A']);
    choose('课时', 'Section A');
    assert.equal(run('systemInputUnitDrafts.get("unit-2").textbookUnit'), 'Unit 2');
    assert.equal(run('systemInputUnitDrafts.get("unit-2").textbookLesson'), 'Section A');
});

test('target detail presents the detected textbook form as a read-only tag', t => {
    const { byId, run } = editor(t, 'textbook', 1);
    run("const current = {...systemInputUnitDrafts.get('unit-1'), textbookForm:'同步课文'}; systemInputUnitDrafts.set('unit-1', current); currentWorkspace.system_input.units[0].evidence = {textbook_form:'角色扮演'}; refreshSystemInputTargetEditor();");
    const action = [...byId('target-rows').querySelectorAll('.target-row-actions button')]
        .find(button => button.textContent.includes('编辑详情'));
    assert.ok(action);
    action.click();

    assert.equal(byId('target-detail-textbook-form').hidden, false);
    assert.equal(byId('target-detail-textbook-form-tag').textContent, '角色扮演');
    assert.equal(byId('target-detail-textbook-form-note').textContent, '文档结构自动识别');
    assert.equal(byId('target-detail-fields').querySelector('[data-target-field="textbookForm"]'), null);
    assert.equal(byId('target-rows').querySelector('.target-textbook-form-tag').textContent, '角色扮演');
    assert.equal(run('systemInputUnitDrafts.get("unit-1").textbookForm'), '同步课文');
    assert.equal(run('typeof systemInputUnitDrafts.get("unit-1").textbookForm'), 'string');
});

test('target detail presents the detected paper category as a tag and hides irrelevant paper controls', t => {
    const { byId, run } = editor(t, 'paper', 1);
    run("currentWorkspace.system_input.suggested_configuration = {paper_category:'题型专项', paper_category_status:'suggested'}; refreshSystemInputTargetEditor();");
    const action = [...byId('target-rows').querySelectorAll('.target-row-actions button')]
        .find(button => button.textContent.includes('编辑详情'));
    assert.ok(action);
    action.click();

    const field = label => [...byId('target-detail-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);
    assert.equal(byId('target-detail-classification').hidden, false);
    assert.equal(byId('target-detail-category-tag').textContent, '题型专项');
    assert.equal(byId('target-detail-category-note').textContent, '文档自动识别');
    assert.equal(field('试卷分类'), undefined);
    assert.equal(field('考试类型'), undefined);
});

test('target detail keeps exam type only for listening papers', t => {
    const { byId, run } = editor(t, 'paper', 1);
    run(`const current = {...systemInputUnitDrafts.get('unit-1'), paperCategory:'听说考试'};
        delete current.paperType;
        systemInputUnitDrafts.set('unit-1', current);
        currentWorkspace.system_input.suggested_configuration = {paper_category:'听说考试', paper_category_status:'suggested'};
        refreshSystemInputTargetEditor();`);
    const action = [...byId('target-rows').querySelectorAll('.target-row-actions button')]
        .find(button => button.textContent.includes('编辑详情'));
    assert.ok(action);
    action.click();

    const field = label => [...byId('target-detail-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);
    const examType = field('考试类型');
    assert.equal(field('试卷分类'), undefined);
    assert.ok(examType);
    assert.equal(examType.querySelector('.target-select-trigger').disabled, false);
    examType.querySelector('.target-select-trigger').click();
    [...examType.querySelectorAll('.target-select-option')]
        .find(option => option.textContent === '阶段测试题')
        ?.click();
    assert.equal(run('systemInputUnitDrafts.get("unit-1").paperType.name'), '阶段测试题');
});

test('target detail disables region descendants for an incompatible restored parent', t => {
    const { byId, run } = editor(t, 'paper', 1);
    run(`const current = {...systemInputUnitDrafts.get('unit-1'), provinceId:{id:430000,name:'湖南省'}, cityId:{id:440100,name:'广州市'}, districtIds:[{id:440106,name:'天河区'}]};
        systemInputUnitDrafts.set('unit-1', current);
        refreshSystemInputTargetEditor();`);
    const row = byId('target-rows').firstElementChild;
    assert.match(row.textContent, /城市/);
    assert.match(row.textContent, /区县/);
    row.querySelector('.target-row-actions button').click();

    const field = label => [...byId('target-detail-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);
    const city = field('城市');
    const district = field('区县');
    assert.equal(city.querySelector('.target-select-trigger').disabled, false);
    assert.equal(district.querySelector('.target-district-search').disabled, true);
    assert.match(district.querySelector('.target-field-hint').textContent, /不属于所选省份/);
    city.querySelector('.target-select-trigger').click();
    assert.deepEqual([...city.querySelectorAll('.target-select-option')].map(option => option.textContent), ['长沙市']);
});

test('target detail resolves duplicate region labels by stable IDs', t => {
    const { w, byId, run } = editor(t, 'paper', 1);
    run(`systemInputRegionIndexes = {
            provinces: [{id: 1, name: '同名省'}, {id: 2, name: '同名省'}],
            cities: [{id: 11, name: '同名市', parentId: 1}, {id: 22, name: '同名市', parentId: 2}],
            districts: [{id: 111, name: '甲区', parentId: 11}, {id: 222, name: '乙区', parentId: 22}],
        };
        const current = {...systemInputUnitDrafts.get('unit-1'),
            provinceId: {id: 2, name: '同名省'}, cityId: {id: 22, name: '同名市'},
            districtIds: [{id: 222, name: '乙区'}], paperName: ''};
        systemInputUnitDrafts.set('unit-1', current);
        refreshSystemInputTargetEditor();`);
    const row = byId('target-rows').firstElementChild;
    row.querySelector('.target-row-actions button').click();

    const field = label => [...byId('target-detail-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);
    const city = field('城市');
    const district = field('区县');
    city.querySelector('.target-select-trigger').click();
    assert.deepEqual([...city.querySelectorAll('.target-select-option')].map(option => option.textContent), ['同名市']);
    city.querySelector('.target-select-trigger').click();
    assert.equal(district.querySelector('.target-district-search').disabled, false);
    district.querySelector('.target-district-search').dispatchEvent(new w.Event('focus', { bubbles: true }));
    assert.deepEqual([...district.querySelectorAll('.target-district-menu .target-select-option')].map(option => option.textContent), ['乙区']);
});

test('target detail cascade rebuild keeps focus on the parent control', t => {
    const { w, byId, run } = editor(t, 'paper', 1);
    run("const current = {...systemInputUnitDrafts.get('unit-1')}; delete current.paperName; systemInputUnitDrafts.set('unit-1', current); refreshSystemInputTargetEditor();");
    const row = byId('target-rows').firstElementChild;
    row.querySelector('.target-row-actions button').click();
    const field = label => [...byId('target-detail-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);
    const province = field('省份');
    const trigger = province.querySelector('.target-select-trigger');
    trigger.focus();
    trigger.click();
    [...province.querySelectorAll('.target-select-option')]
        .find(option => option.textContent === '湖南省')
        ?.click();
    assert.equal(w.document.activeElement,
        byId('target-detail-fields').querySelector('[data-target-field="provinceId"] .target-select-trigger'));
});

test('filling a missing field in direct detail editing persists immediately', t => {
    const { w, byId, run } = editor(t, 'paper', 1);
    run(`const current = {...systemInputUnitDrafts.get('unit-1')}; delete current.paperName; systemInputUnitDrafts.set('unit-1', current); refreshSystemInputTargetEditor();`);
    const action = [...byId('target-rows').querySelectorAll('.target-row-actions button')]
        .find(button => button.textContent.includes('编辑详情'));
    assert.ok(action);
    action.click();
    const field = [...byId('target-detail-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === '试卷名称');
    const input = field.querySelector('input');
    input.value = '补好的试卷名称';
    assert.doesNotThrow(() => input.dispatchEvent(new w.Event('input', { bubbles: true })));
    assert.equal(run('systemInputUnitDrafts.get("unit-1").paperName'), '补好的试卷名称');
    assert.match(byId('target-detail-summary').textContent, /必填字段已补齐/);
});

test('multi-target validation entry points open detail editing with all requested fields', t => {
    const { byId, run } = editor(t, 'paper', 2);
    run(`const current = {...systemInputUnitDrafts.get('unit-2')}; delete current.paperName; delete current.cityId; systemInputUnitDrafts.set('unit-2', current); refreshSystemInputTargetEditor();`);
    assert.equal(run('validateSystemInputConfigurationForm({focus:false})'), false);
    byId('system-input-validation-message').querySelector('button').click();
    const fields = [...byId('target-detail-fields').querySelectorAll('.target-field')];
    const visible = fields
        .map(field => field.querySelector(':scope > span')?.textContent)
        .filter(label => ['试卷名称', '省份', '城市'].includes(label));
    assert.deepEqual(visible, ['试卷名称', '省份', '城市']);
    assert.equal(byId('target-detail').hidden, false);
    assert.equal(byId('target-batch').hidden, true);
});

test('single and multi-target workspaces share the same direct detail editor', t => {
    const single = editor(t, 'paper', 1);
    assert.equal(single.byId('target-editor-form-source').getAttribute('aria-hidden'), 'true');
    assert.equal(single.byId('target-editor-form-source').hidden, true);
    assert.equal(single.byId('target-detail').hidden, true);
    assert.ok(single.byId('target-detail-close'));
    assert.ok(single.byId('target-detail-done'));
    single.run("const current = {...systemInputUnitDrafts.get('unit-1')}; delete current.paperName; systemInputUnitDrafts.set('unit-1', current); refreshSystemInputTargetEditor();");
    single.byId('target-rows').querySelector('.target-row-actions button').click();
    assert.equal(single.byId('target-detail').hidden, false);
    assert.equal(single.byId('target-batch').hidden, true);

    const multi = editor(t, 'paper', 3);
    assert.equal(multi.byId('target-editor-form-source').getAttribute('aria-hidden'), 'true');
    assert.equal(multi.byId('target-editor-form-source').hidden, true);
    assert.equal(multi.byId('target-detail').hidden, true);
    assert.ok(multi.byId('target-detail-close'));
    assert.ok(multi.byId('target-detail-done'));
});

test('a complete single target still exposes the full detail editor for later edits', t => {
    // 配置齐全（必填字段已补齐）的单条目标也必须能继续编辑：省份/年级等配置字段
    // 不在行的内联单元格里，若“编辑详情”入口仅对缺失字段开放，配置齐全后就再也
    // 改不了这些配置，等于把目标“锁死”。
    const { byId } = editor(t, 'paper', 1);
    const row = byId('target-rows').firstElementChild;
    assert.match(row.querySelector('.target-status').textContent, /配置齐全/);
    const editButton = [...row.querySelectorAll('.target-row-actions button')]
        .find(button => button.textContent.includes('编辑详情'));
    assert.ok(editButton, 'a complete unit must still offer an 编辑详情 entry');
    editButton.click();
    assert.equal(byId('target-detail').hidden, false);
    const field = label => [...byId('target-detail-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);
    assert.ok(field('省份'), 'detail editor should expose the province field');
    assert.ok(field('城市'), 'detail editor should expose the city field');
    assert.ok(field('年级'), 'detail editor should expose the grade field');
    assert.equal(field('省份').querySelector('.target-select-trigger').disabled, false);
});

test('nested target dialogs keep keyboard focus inside the active dialog', t => {
    const { w, byId } = editor(t, 'paper', 3);
    const appRoot = w.document.querySelector('#app');
    const drawer = byId('system-input-drawer');
    assert.equal(appRoot?.inert, true);
    assert.equal(drawer.getAttribute('aria-hidden'), 'false');
    byId('target-common-open').click();
    const dialog = byId('target-common');
    assert.equal(drawer.getAttribute('aria-hidden'), 'true');
    const focusable = [...dialog.querySelectorAll(
        'button:not([disabled]), input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])',
    )].filter(node => !node.hidden && !node.closest('[hidden]'));
    assert.ok(focusable.length > 1);
    const first = focusable[0];
    const last = focusable.at(-1);
    last.focus();
    last.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
    assert.equal(w.document.activeElement, first);
    first.focus();
    first.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }));
    assert.equal(w.document.activeElement, last);
    byId('target-common-close').click();
    assert.equal(drawer.getAttribute('aria-hidden'), 'false');
});

test('refresh preserves an active inline editor but still rebuilds after a source action gets focus', t => {
    const { w, byId } = editor(t, 'paper', 3);
    const inline = byId('target-rows').querySelector('[data-unit-id="unit-1"][data-field="paperName"]');
    inline.focus();
    w.refreshSystemInputTargetEditor();
    assert.equal(w.document.activeElement, inline);

    const action = byId('target-rows').querySelector('.target-source > button');
    action.focus();
    byId('target-filter').value = 'pending';
    w.refreshSystemInputTargetEditor();
    assert.equal(byId('target-rows').children.length, 0);
    assert.equal(byId('target-empty').hidden, false);
    assert.equal(byId('target-overview-count').textContent, '3 套试卷 · 0 条待处理');
});

test('closing the target editor restores the background interaction state', t => {
    const { w, byId } = editor(t, 'paper', 3);
    const appRoot = w.document.querySelector('#app');
    assert.equal(appRoot?.inert, true);
    byId('system-input-drawer-close').click();
    assert.equal(appRoot?.inert, false);
});

test('single target Escape closes the whole editor window', t => {
    const { w, byId } = editor(t, 'paper', 1);
    const drawer = byId('system-input-drawer');
    w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    assert.equal(drawer.hidden, true);
    assert.doesNotMatch(w.document.body.className, /target-modal-open/);
});

test('an unapplied batch default draft does not block formal save', async t => {
    const { w, byId } = editor(t, 'paper', 3);
    byId('target-common-open').click();
    const year = byId('target-common-fields').querySelector('input[type="number"]');
    year.value = '2031';
    year.dispatchEvent(new w.Event('input', { bubbles: true }));
    await w.submitSystemInputConfiguration();
    assert.equal(w.__saved.length, 1, 'a pending common helper value must not block the save');
    assert.equal(byId('target-common').hidden, true, 'no common modal should pop up on save');
});

test('saving a common template uses the visible unapplied common draft', async t => {
    const { w, byId } = editor(t, 'paper', 3);
    byId('target-common-open').click();
    const year = byId('target-common-fields').querySelector('input[type="number"]');
    year.value = '2031';
    year.dispatchEvent(new w.Event('input', { bubbles: true }));
    byId('target-common-close').click();

    assert.equal(await w.targetEditorSaveTemplate(), true);
    assert.equal(w.__templates.length, 1);
    assert.equal(w.__templates[0].configuration.year, 2031);
});

test('a closed batch preview survives reopening and is surfaced before save', async t => {
    const first = editor(t, 'paper', 3);
    first.byId('target-rows').querySelector('input[type="checkbox"]').click();
    first.byId('target-batch-open').click();
    first.byId('target-batch-fields').querySelector('[aria-label="批量修改平台题型模板"]').click();
    const value = first.byId('target-batch-fields').querySelector('[aria-label="批量修改 · 平台题型模板"]');
    value.value = '听后回答'; value.dispatchEvent(new first.w.Event('input', { bubbles: true }));
    first.change(first.byId('target-batch-mode'), 'overwrite');
    first.byId('target-batch-close').click();
    first.w.closeSystemInputConfigDrawer();
    const storage = Object.fromEntries(Array.from({length: first.w.localStorage.length}, (_, i) => {
        const key = first.w.localStorage.key(i); return [key, first.w.localStorage.getItem(key)];
    }));
    const second = editor(t, 'paper', 3, storage);
    assert.equal(second.byId('target-batch').hidden, true);
    await second.w.submitSystemInputConfiguration();
    assert.equal(second.w.__saved.length, 0);
    assert.equal(second.byId('target-batch').hidden, false);
    assert.equal(second.byId('target-batch-fields').querySelector('[aria-label="批量修改 · 平台题型模板"]').value, '听后回答');
});

test('target filters use the shared custom control and keep the native value in sync', t => {
    const { w, byId } = editor(t, 'paper', 3);
    const trigger = byId('target-filter').closest('.target-select').querySelector('.target-select-trigger');
    trigger.click();
    const option = byId('target-filter').closest('.target-select').querySelector('[data-value="pending"]');
    assert.ok(option);
    option.click();
    assert.equal(byId('target-filter').value, 'pending');
    assert.equal(byId('target-filter').closest('.target-select').querySelector('.target-select-value').textContent, '只看待处理');
});

test('open target menus close when the scroll container moves', t => {
    const { w, byId } = editor(t, 'paper', 3);
    const wrapper = byId('target-filter').closest('.target-select');
    wrapper.querySelector('.target-select-trigger').click();
    assert.equal(wrapper.querySelector('.target-select-menu').hidden, false);
    w.document.dispatchEvent(new w.Event('scroll', { bubbles: true }));
    assert.equal(wrapper.querySelector('.target-select-menu').hidden, true);
});

test('Escape closes a target menu before it closes the containing dialog', t => {
    const { w, byId } = editor(t, 'paper', 3);
    byId('target-rows').querySelector('input[type="checkbox"]').click();
    byId('target-batch-open').click();
    const wrapper = byId('target-batch-mode').closest('.target-select');
    const trigger = wrapper.querySelector('.target-select-trigger');
    trigger.click();
    assert.equal(wrapper.querySelector('.target-select-menu').hidden, false);
    trigger.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    assert.equal(wrapper.querySelector('.target-select-menu').hidden, true);
    assert.equal(byId('target-batch').hidden, false);
});

test('Enter chooses the active custom-select option instead of skipping ahead', t => {
    const { w, byId } = editor(t, 'paper', 3);
    const wrapper = byId('target-filter').closest('.target-select');
    const trigger = wrapper.querySelector('.target-select-trigger');
    trigger.click();
    const active = wrapper.querySelector('.target-select-option.is-active');
    assert.equal(active?.dataset.value, 'all');
    assert.equal(trigger.getAttribute('aria-activedescendant'), active.id);

    trigger.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));

    assert.equal(byId('target-filter').value, 'all');
    assert.equal(wrapper.querySelector('.target-select-menu').hidden, true);
    assert.equal(trigger.hasAttribute('aria-activedescendant'), false);
});

test('Escape on a closed custom select reaches the containing dialog', t => {
    const { w, byId } = editor(t, 'paper', 3);
    byId('target-rows').querySelector('input[type="checkbox"]').click();
    byId('target-batch-open').click();
    const trigger = byId('target-batch-mode').closest('.target-select').querySelector('.target-select-trigger');
    trigger.focus();
    trigger.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    assert.equal(byId('target-batch').hidden, true);
    assert.equal(byId('system-input-drawer').hidden, false);
});

test('textbook names use readable multi-line editors and template scope is custom', t => {
    const { byId } = editor(t, 'textbook', 3);
    assert.equal(byId('target-rows').querySelector('[data-field="textbookNameZh"]').tagName, 'TEXTAREA');
    assert.equal(byId('target-rows').querySelector('[data-field="textbookNameEn"]').rows, 2);
    assert.ok(byId('target-template-save-scope').closest('.target-select'));
});

test('synced textbook directory fields behave as read-only cascading pickers in the shared batch tool', t => {
    const { byId } = editor(t, 'textbook', 3);
    byId('target-rows').querySelector('input[type="checkbox"]').click();
    byId('target-batch-open').click();
    ['version', 'stage', 'grade', 'volume', 'unit', 'lesson'].forEach(field => {
        const input = byId('target-batch-fields').querySelector(`[aria-label="批量修改 · ${({
            version: '教材版本', stage: '学段', grade: '年级', volume: '册别', unit: '教材单元', lesson: '课时',
        })[field]}"]`);
        assert.equal(input?.tagName, 'SELECT', field);
        assert.ok(input?.closest('.target-select'), field);
    });
    assert.equal(byId('target-editor-form-source').hidden, true);
    assert.equal(byId('target-rows').querySelector('[data-field="textbookNameZh"]').tagName, 'TEXTAREA');
});

test('textbook sync stays in the fixed shell and directory fields remain cascaded selects', t => {
    const { byId } = editor(t, 'textbook', 3);
    assert.equal(byId('target-catalog-sync').hidden, false);
    byId('target-rows').querySelector('input[type="checkbox"]')?.click();
    byId('target-batch-open').click();
    ['textbookVersion', 'textbookStage', 'textbookGrade', 'textbookVolume', 'textbookUnit', 'textbookLesson'].forEach(field => {
        const control = byId('target-batch-fields').querySelector(`[aria-label="批量修改 · ${({
            textbookVersion: '教材版本', textbookStage: '学段', textbookGrade: '年级', textbookVolume: '册别', textbookUnit: '教材单元', textbookLesson: '课时',
        })[field]}"]`);
        assert.equal(control?.tagName, 'SELECT', field);
        assert.ok(control.closest('.target-select'), field);
    });
    const inlineUnit = byId('target-rows').querySelector('[data-field="textbookUnit"]');
    const inlineLesson = byId('target-rows').querySelector('[data-field="textbookLesson"]');
    assert.equal(inlineUnit?.tagName, 'SELECT');
    assert.equal(inlineLesson?.tagName, 'SELECT');
    assert.equal(inlineUnit?.dataset.targetSelectEnhanced, 'true');
    assert.equal(inlineLesson?.dataset.targetSelectEnhanced, 'true');
    assert.ok(inlineUnit?.closest('.target-select'));
    assert.ok(inlineLesson?.closest('.target-select'));
    const menus = [...byId('target-batch-fields').querySelectorAll('.target-select-menu')].map(node => node.id);
    assert.equal(new Set(menus).size, menus.length);
    assert.ok([...byId('target-batch-fields').querySelectorAll('.target-select-arrow')].every(node => node.textContent === ''));
    const inlineMenus = [...byId('target-rows').querySelectorAll('.target-select-menu')].map(node => node.id);
    assert.equal(new Set(inlineMenus).size, inlineMenus.length);
    assert.ok([...byId('target-rows').querySelectorAll('.target-select-trigger')].every(trigger => trigger.getAttribute('aria-controls')));
    const customSelects = [...byId('target-overview').ownerDocument.querySelectorAll('#target-overview select, #target-common select, #target-batch select')];
    assert.ok(customSelects.length > 0);
    assert.ok(customSelects.every(select => select.classList.contains('target-native-select')));
    assert.ok(customSelects.every(select => select.getAttribute('aria-hidden') === 'true'));
    assert.ok(customSelects.every(select => select.tabIndex === -1));
});

test('平台模板同步卡仅对试卷类型显示，并停留在固定外壳', t => {
    const paper = editor(t, 'paper', 3);
    assert.equal(paper.byId('target-catalog-sync-template').hidden, false);
    assert.equal(paper.byId('target-catalog-sync-template').querySelector('#system-input-platform-template-sync-btn').disabled, false);
    // 试卷类型不显示教材目录卡
    assert.equal(paper.byId('target-catalog-sync').hidden, true);

    const textbook = editor(t, 'textbook', 3);
    assert.equal(textbook.byId('target-catalog-sync-template').hidden, true);
    assert.equal(textbook.byId('target-catalog-sync').hidden, false);
});

test('平台题型模板字段不再提供保存/管理/重命名/删除等自定义配置', t => {
    const { byId } = editor(t, 'paper', 3);
    assert.equal(byId('system-input-save-platform-template-btn'), null);
    assert.equal(byId('system-input-manage-platform-template-btn'), null);
    assert.equal(byId('system-input-edit-platform-template-btn'), null);
    assert.equal(byId('system-input-delete-platform-template-btn'), null);
    assert.equal(byId('system-input-platform-template-editor'), null);
});

test('large platform template catalogues keep target pickers lazy and searchable', t => {
    const { w, byId, run, read } = editor(t, 'paper', 3);
    run(`systemInputPlatformTemplateCatalog = {
        records: Array.from({ length: 500 }, (_, index) => ({
            platform_template_key: 'fixture:large:' + index,
            template_kind: 'question',
            name: '专项模板 ' + index,
            enabled: true,
            province: { id: 440000, name: '广东省' },
            city: { id: 440100, name: '广州市' },
        })),
        record_count: 500,
    };
    systemInputPlatformTemplates = systemInputPlatformTemplateCatalog.records;
    refreshSystemInputTargetEditor({ force: true });`);

    const select = byId('target-rows').querySelector('[data-unit-id="unit-1"][data-field="platformTemplateName"]');
    assert.equal(select.options.length, 2, 'only the empty option and current value are materialized');

    const wrapper = select.closest('.target-select');
    wrapper.querySelector('.target-select-trigger').click();
    assert.equal(wrapper.querySelectorAll('.target-select-option').length, 100);
    assert.match(wrapper.textContent, /结果较多（共 501 个）/);

    select.value = '专项模板 499';
    select.dispatchEvent(new w.Event('input', { bubbles: true }));
    assert.equal(read()[0].platformTemplateName, '专项模板 499');
});

test('changing an inline textbook unit clears the dependent lesson', t => {
    const { byId, read } = editor(t, 'textbook', 3);
    const unit = byId('target-rows').querySelector('[data-unit-id="unit-1"][data-field="textbookUnit"]');
    const wrapper = unit.closest('.target-select');
    wrapper.querySelector('.target-select-trigger').click();
    const option = [...wrapper.querySelectorAll('.target-select-option')]
        .find(item => item.textContent === 'Unit 2');
    assert.ok(option);
    option.click();

    assert.equal(read()[0].textbookUnit, 'Unit 2');
    assert.equal('textbookLesson' in read()[0], false);
    const lesson = byId('target-rows').querySelector('[data-unit-id="unit-1"][data-field="textbookLesson"]');
    assert.equal(lesson.value, '');
    assert.equal(lesson.classList.contains('target-native-select'), true);
    assert.equal(lesson.getAttribute('aria-hidden'), 'true');
    assert.equal(lesson.tabIndex, -1);
});

test('checking a textbook parent enables its custom trigger and refreshes descendants', t => {
    const { w, byId } = editor(t, 'textbook', 3);
    byId('target-rows').querySelector('input[type="checkbox"]')?.click();
    byId('target-batch-open').click();
    const field = label => [...byId('target-batch-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);
    const version = field('教材版本');
    const versionCheck = version.querySelector('input[type="checkbox"]');
    const versionSelect = version.querySelector('select');
    versionCheck.click();
    assert.equal(versionSelect.disabled, false);
    assert.equal(version.querySelector('.target-select-trigger').disabled, false);
    versionSelect.value = versionSelect.options[1]?.value || '';
    versionSelect.dispatchEvent(new w.Event('change', { bubbles: true }));
    const stage = field('学段');
    assert.ok([...stage.querySelectorAll('select option')].some(option => option.textContent === '初中'));
    assert.equal(stage.querySelector('.target-select-trigger').disabled, true);
    stage.querySelector('input[type="checkbox"]').click();
    assert.equal(stage.querySelector('.target-select-trigger').disabled, false);
});

test('textbook child fields stay unavailable until the parent checkbox and value are selected', t => {
    const { w, byId } = editor(t, 'textbook', 3);
    byId('target-rows').querySelector('input[type="checkbox"]')?.click();
    byId('target-batch-open').click();
    const child = [...byId('target-batch-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === '学段');
    // A saved parent value alone is not enough for a batch operation: the
    // parent field must be explicitly included in this operation first.
    assert.equal(child.querySelector('input[type="checkbox"]').disabled, true);
    const versionField = [...byId('target-batch-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === '教材版本');
    const versionCheck = versionField.querySelector('input[type="checkbox"]');
    const version = versionField.querySelector('[aria-label="批量修改 · 教材版本"]');
    versionCheck.click();
    const unlocked = [...byId('target-batch-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === '学段');
    assert.equal(unlocked.querySelector('input[type="checkbox"]').disabled, false);
    assert.equal(unlocked.querySelector('select').disabled, true);

    // Clearing the selected parent value locks its descendants again.
    version.value = '';
    version.dispatchEvent(new w.Event('change', { bubbles: true }));
    const refreshed = [...byId('target-batch-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === '学段');
    assert.equal(refreshed.querySelector('input[type="checkbox"]').disabled, true);
    assert.equal(refreshed.querySelector('.target-select-trigger').disabled, true);
});

test('changing a textbook parent clears descendant batch flags before preview', t => {
    const { w, byId } = editor(t, 'textbook', 3);
    byId('target-rows').querySelector('input[type="checkbox"]')?.click();
    byId('target-batch-open').click();
    const field = label => [...byId('target-batch-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);
    const unit = field('教材单元');
    const lesson = field('课时');
    unit.querySelector('input[type="checkbox"]').click();
    lesson.querySelector('input[type="checkbox"]').click();
    const unitSelect = unit.querySelector('select');
    const secondOption = [...unitSelect.options].find(option => option.textContent === 'Unit 2');
    unitSelect.value = secondOption.value;
    unitSelect.dispatchEvent(new w.Event('change', { bubbles: true }));
    const refreshedLesson = field('课时');
    assert.equal(refreshedLesson.querySelector('input[type="checkbox"]').checked, false);
    assert.equal(refreshedLesson.querySelector('.target-select-trigger').disabled, true);
    assert.equal(refreshedLesson.querySelector('select').value, '');
});

test('common textbook cascades unlock from the selected version and hide placeholder options', t => {
    const { w, byId } = editor(t, 'textbook', 3);
    byId('target-common-open').click();
    const fields = () => [...byId('target-common-fields').querySelectorAll('.target-field')];
    const field = label => fields().find(node => node.querySelector(':scope > span')?.textContent === label);
    const clear = label => {
        const select = field(label).querySelector('select');
        select.value = '';
        select.dispatchEvent(new w.Event('change', { bubbles: true }));
    };
    // Start from a blank directory path, then make the same first action as a
    // user: choose the version. Descendants must become usable immediately.
    clear('教材版本');
    const version = field('教材版本').querySelector('select');
    const versionWrapper = version.closest('.target-select');
    versionWrapper.querySelector('.target-select-trigger').click();
    assert.equal(versionWrapper.querySelector('.target-select-option')?.textContent, '人教版');
    assert.equal(versionWrapper.querySelector('.target-select-option')?.textContent.includes('请选择'), false);
    versionWrapper.querySelector('.target-select-option').click();
    const stage = field('学段');
    assert.equal(stage.querySelector('select').disabled, false);
    assert.equal(stage.querySelector('.target-select-trigger').disabled, false);
    assert.ok([...stage.querySelectorAll('select option')].some(option => option.textContent === '初中'));
    assert.match(byId('target-common-summary').textContent, /人教版/);
});

test('batch textbook cascades unlock from a custom parent choice without JSON errors', t => {
    const { w, byId } = editor(t, 'textbook', 3);
    byId('target-rows').querySelector('input[type="checkbox"]')?.click();
    byId('target-batch-open').click();
    const fields = () => [...byId('target-batch-fields').querySelectorAll('.target-field')];
    const field = label => fields().find(node => node.querySelector(':scope > span')?.textContent === label);

    const versionField = field('教材版本');
    versionField.querySelector('input[type="checkbox"]').click();
    const version = versionField.querySelector('select');
    version.value = '';
    version.dispatchEvent(new w.Event('change', { bubbles: true }));

    const refreshedVersion = field('教材版本');
    const versionWrapper = refreshedVersion.querySelector('.target-select');
    versionWrapper.querySelector('.target-select-trigger').click();
    versionWrapper.querySelector('.target-select-option')?.click();

    const stage = field('学段');
    assert.equal(stage.querySelector('input[type="checkbox"]').disabled, false);
    assert.equal(stage.querySelector('.target-select-trigger').disabled, true);
    stage.querySelector('input[type="checkbox"]').click();
    assert.equal(stage.querySelector('.target-select-trigger').disabled, false);
});

test('legacy snake_case cascade values still populate the shared batch controls', t => {
    const { byId, run } = editor(t, 'paper', 1);
    run(`const current = {...systemInputUnitDrafts.get('unit-1')};
        current.provinceId = ''; current.province_id = {id: 440000, name: '广东省'};
        delete current.cityId; current.city_id = {id: 440100, name: '广州市'};
        delete current.stageId; current.stage_id = {id: 2, name: '初中'};
        delete current.gradeId; current.grade_id = {id: 7, name: '七年级'};
        systemInputUnitDrafts.set('unit-1', current); refreshSystemInputTargetEditor();`);
    const rowCheck = byId('target-rows').querySelector('input[type="checkbox"]');
    if (rowCheck && !rowCheck.checked) rowCheck.click();
    byId('target-batch-open').click();
    const field = label => [...byId('target-batch-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);

    assert.notEqual(field('省份').querySelector('select').value, '');
    assert.notEqual(field('城市').querySelector('select').value, '');
    assert.equal(field('年级').querySelector('select').value !== '', true);
    assert.equal(field('城市').querySelector('input[type="checkbox"]').disabled, true);
    field('省份').querySelector('input[type="checkbox"]').click();
    assert.equal(field('城市').querySelector('input[type="checkbox"]').disabled, false);
});

test('legacy scalar district aliases do not break the custom batch cascade', t => {
    const { w, byId, run } = editor(t, 'paper', 1);
    const errors = [];
    w.addEventListener('error', event => {
        errors.push(event.error || event.message);
        event.preventDefault();
    });
    run(`const current = {...systemInputUnitDrafts.get('unit-1')};
        delete current.districtIds;
        current.district_id = {id: 440106, name: '天河区'};
        systemInputUnitDrafts.set('unit-1', current);
        refreshSystemInputTargetEditor();`);
    const rowCheck = byId('target-rows').querySelector('input[type="checkbox"]');
    if (rowCheck && !rowCheck.checked) rowCheck.click();
    byId('target-batch-open').click();

    const district = [...byId('target-batch-fields').querySelectorAll('.target-field')]
        .find(field => field.querySelector(':scope > span')?.textContent === '区县');
    assert.equal(errors.length, 0, errors.map(error => error?.message || error).join('\n'));
    assert.ok(district);
    assert.match(district.querySelector('.target-district-chips').textContent, /天河区/);
    assert.ok(district.querySelector('.target-district-search'));
    assert.equal(district.querySelector('.target-district-search').getAttribute('aria-hidden'), null);
});

test('template scope cards are bound after the custom modal is moved out of the drawer', t => {
    const { byId } = editor(t, 'paper', 3);
    byId('target-rows').querySelector('input[type="checkbox"]')?.click();
    byId('target-template-open').click();

    const scope = byId('system-input-app-template-scope-field');
    const selected = scope.querySelector('[data-value="selected"]');
    const all = scope.querySelector('[data-value="all"]');
    const apply = byId('system-input-apply-app-template-btn');
    assert.equal(scope.hidden, false);
    selected.click();
    assert.equal(byId('system-input-app-template-scope').value, 'selected');
    assert.equal(selected.getAttribute('aria-checked'), 'true');
    assert.equal(selected.classList.contains('is-selected'), true);
    assert.equal(apply.disabled, false);

    all.click();
    assert.equal(byId('system-input-app-template-scope').value, 'all');
    assert.equal(all.getAttribute('aria-checked'), 'true');
    assert.equal(all.classList.contains('is-selected'), true);
    assert.equal(selected.getAttribute('aria-checked'), 'false');
});

test('custom target selects keep hidden native sources out of the focus path', async t => {
    const { w, byId } = editor(t, 'paper', 3);
    byId('target-common-open').click();
    await new Promise(resolve => w.requestAnimationFrame(resolve));
    const source = byId('target-common-fields').querySelector('.target-native-select');
    assert.ok(source);
    assert.equal(source.getAttribute('aria-hidden'), 'true');
    assert.equal(source.inert, true);
    assert.notEqual(w.document.activeElement, source);
    source.focus();
    assert.notEqual(w.document.activeElement, source);
    assert.ok(byId('target-common-fields').querySelector('.target-select-trigger'));
});

test('common textbook shortcut mirrors the directory path and clearing it clears all four levels', t => {
    const { byId } = editor(t, 'textbook', 3);
    byId('target-common-open').click();
    const book = byId('target-book');
    const bookWrapper = book.closest('.target-select');
    assert.notEqual(book.value, '', 'the current textbook path should be reflected in the shortcut');
    assert.match(bookWrapper.querySelector('.target-select-value').textContent, /人教版/);

    bookWrapper.querySelector('.target-select-trigger').click();
    bookWrapper.querySelector('.target-select-clear').click();
    assert.equal(book.value, '');
    ['教材版本', '学段', '年级', '册别'].forEach(label => {
        const field = [...byId('target-common-fields').querySelectorAll('.target-field')]
            .find(node => node.querySelector(':scope > span')?.textContent === label);
        assert.equal(field.querySelector('select').value, '', `${label} should clear with the textbook shortcut`);
    });
    assert.doesNotMatch(byId('target-common-summary').textContent, /人教版/);

    // Selecting a catalogue entry again restores the path, and closing then
    // reopening the modal keeps the shortcut and fields in agreement.
    bookWrapper.querySelector('.target-select-trigger').click();
    bookWrapper.querySelector('.target-select-option').click();
    assert.match(bookWrapper.querySelector('.target-select-value').textContent, /人教版/);
    byId('target-common-close').click();
    byId('target-common-open').click();
    assert.notEqual(byId('target-book').value, '');
    assert.match(byId('target-book').closest('.target-select').querySelector('.target-select-value').textContent, /人教版/);
});

test('paper common cascades disable children until each parent is selected', t => {
    const { w, byId } = editor(t, 'paper', 3);
    byId('target-common-open').click();
    const fields = () => [...byId('target-common-fields').querySelectorAll('.target-field')];
    const field = label => fields().find(node => node.querySelector(':scope > span')?.textContent === label);
    const clearWithMenu = label => {
        const wrap = field(label);
        const search = wrap.querySelector('.target-district-search');
        if (search) {
            search.dispatchEvent(new w.Event('focus', { bubbles: true }));
            wrap.querySelector('.target-district-menu .target-select-clear')?.click();
            return;
        }
        const wrapper = wrap.querySelector('.target-select');
        wrapper.querySelector('.target-select-trigger').click();
        wrapper.querySelector('.target-select-clear')?.click();
    };
    clearWithMenu('省份');
    clearWithMenu('学段');
    clearWithMenu('区县');
    assert.equal(field('城市').querySelector('.target-select-trigger').disabled, true);
    assert.equal(field('区县').querySelector('.target-district-search').disabled, true);
    assert.equal(field('年级').querySelector('.target-select-trigger').disabled, true);
    assert.match(field('城市').querySelector('.target-field-hint').textContent, /先选择省份/);
    assert.match(field('年级').querySelector('.target-field-hint').textContent, /先选择学段/);

    const choose = (label, optionText) => {
        const wrap = field(label);
        const search = wrap.querySelector('.target-district-search');
        if (search) {
            search.dispatchEvent(new w.Event('focus', { bubbles: true }));
            const option = [...wrap.querySelectorAll('.target-district-menu .target-select-option')].find(item => item.textContent === optionText);
            assert.ok(option, `${label} should expose ${optionText}`);
            option.click();
            return;
        }
        const wrapper = wrap.querySelector('.target-select');
        wrapper.querySelector('.target-select-trigger').click();
        const option = [...wrapper.querySelectorAll('.target-select-option')].find(item => item.textContent === optionText);
        assert.ok(option, `${label} should expose ${optionText}`);
        option.click();
    };
    choose('省份', '广东省');
    assert.equal(field('城市').querySelector('.target-select-trigger').disabled, false);
    choose('城市', '广州市');
    assert.equal(field('区县').querySelector('.target-district-search').disabled, false);
    choose('学段', '初中');
    assert.equal(field('年级').querySelector('.target-select-trigger').disabled, false);
    assert.equal(field('城市').querySelector('.target-select-option'), null);
});

test('reopening a region picker keeps the complete sibling list', t => {
    const { w, byId } = editor(t, 'paper', 3);
    byId('target-common-open').click();

    const fields = () => [...byId('target-common-fields').querySelectorAll('.target-field')];
    const field = label => fields().find(node => node.querySelector(':scope > span')?.textContent === label);
    const choose = (label, optionText) => {
        const wrap = field(label);
        const search = wrap.querySelector('.target-district-search');
        if (search) {
            search.dispatchEvent(new w.Event('focus', { bubbles: true }));
            const option = [...wrap.querySelectorAll('.target-district-menu .target-select-option')].find(item => item.textContent === optionText);
            assert.ok(option, `${label} should expose ${optionText}`);
            option.click();
            return;
        }
        const wrapper = wrap.querySelector('.target-select');
        wrapper.querySelector('.target-select-trigger').click();
        const option = [...wrapper.querySelectorAll('.target-select-option')].find(item => item.textContent === optionText);
        assert.ok(option, `${label} should expose ${optionText}`);
        option.click();
    };
    const clear = label => {
        const wrap = field(label);
        const search = wrap.querySelector('.target-district-search');
        if (search) {
            search.dispatchEvent(new w.Event('focus', { bubbles: true }));
            wrap.querySelector('.target-district-menu .target-select-clear')?.click();
            return;
        }
        const wrapper = wrap.querySelector('.target-select');
        wrapper.querySelector('.target-select-trigger').click();
        wrapper.querySelector('.target-select-clear')?.click();
    };
    const labels = label => {
        const wrap = field(label);
        const search = wrap.querySelector('.target-district-search');
        if (search) {
            search.dispatchEvent(new w.Event('focus', { bubbles: true }));
            return [...wrap.querySelectorAll('.target-district-menu .target-select-option')].map(node => node.textContent);
        }
        const wrapper = wrap.querySelector('.target-select');
        wrapper.querySelector('.target-select-trigger').click();
        return [...wrapper.querySelectorAll('.target-select-option')].map(node => node.textContent);
    };

    clear('省份');
    clear('城市');
    clear('区县');
    choose('省份', '广东省');
    assert.deepEqual(labels('省份'), ['广东省', '湖南省']);
    choose('城市', '广州市');
    assert.deepEqual(labels('城市'), ['广州市', '深圳市']);
    choose('区县', '天河区');
    assert.deepEqual(labels('区县'), ['天河区', '白云区']);

    // Changing the top-level value and opening it again must not filter the
    // menu down to the committed value.
    choose('省份', '湖南省');
    const province = field('省份').querySelector('.target-select');
    province.querySelector('.target-select-trigger').click();
    assert.deepEqual(labels('省份'), ['广东省', '湖南省']);
    assert.equal(byId('target-common').hidden, false);
});

test('异步加载省市区后会把已保存区县重新绑定到稳定 ID', async t => {
    const { byId, run } = editor(t, 'paper', 1);
    run(`const current = {...systemInputUnitDrafts.get('unit-1'), districtIds: ['天河区']};
        systemInputUnitDrafts.set('unit-1', current);
        systemInputRegionTree = [];
        systemInputRegionIndexes = {provinces: [], cities: [], districts: []};
        systemInputRegionLoadPromise = null;
        systemInputRegionLoadState = 'idle';
        populateSystemInputUnitForm(currentWorkspace.system_input);
        electronAPI.readRegionTree = async () => ${JSON.stringify(require('./helpers/target-editor-fixture.cjs').fixtureRegionTree)};`);
    await run('loadSystemInputRegions()');

    const state = JSON.parse(run('JSON.stringify({selection: systemInputDistrictSelections[0], saved: collectSystemInputConfiguration(currentWorkspace.system_input).units[0].districtIds[0]})'));
    assert.equal(state.selection.id, 440106);
    assert.equal(state.saved.id, 440106);
    assert.equal(state.selection.name, '天河区');
    assert.match(byId('system-input-district-chips').textContent, /天河区/);
});

test('共享录入选择器搜索后再打开仍保留完整候选列表', async t => {
    const { w, byId } = editor(t, 'paper', 3);
    const visibleMenuLabels = fieldId => [...byId(`${fieldId}-menu`).querySelectorAll('.system-input-picker-option:not(.is-custom) strong')]
        .map(node => node.textContent.trim());
    const menuLabels = fieldId => {
        const input = byId(fieldId);
        input.click();
        return visibleMenuLabels(fieldId);
    };
    const choose = (fieldId, label, query = label.slice(0, 1)) => {
        const input = byId(fieldId);
        input.click();
        input.value = query;
        input.dispatchEvent(new w.Event('input', { bubbles: true }));
        const option = [...byId(`${fieldId}-menu`).querySelectorAll('.system-input-picker-option:not(.is-custom)')]
            .find(node => node.querySelector('strong')?.textContent.trim() === label);
        assert.ok(option, `${fieldId} should expose ${label} while searching`);
        option.click();
    };

    // A catalogue refresh can resync a committed value while the menu is
    // still open. That refresh must end the old search session as well.
    const province = byId('system-input-province');
    province.click();
    province.value = '广';
    province.dispatchEvent(new w.Event('input', { bubbles: true }));
    province.value = '广东省';
    w.syncSystemInputPicker('system-input-province', { selectedValue: '440000' });
    assert.deepEqual(visibleMenuLabels('system-input-province'), ['广东省', '湖南省']);
    w.closeSystemInputPicker();

    choose('system-input-province', '广东省', '广');
    assert.deepEqual(menuLabels('system-input-province'), ['广东省', '湖南省']);
    choose('system-input-city', '广州市', '广');
    assert.deepEqual(menuLabels('system-input-city'), ['广州市', '深圳市']);
    choose('system-input-districts', '天河区', '天');
    assert.deepEqual(menuLabels('system-input-districts'), ['天河区', '白云区']);

    choose('system-input-stage', '初中', '初');
    assert.deepEqual(menuLabels('system-input-stage'), ['小学', '初中', '高中']);
    choose('system-input-grade', '七年级', '七');
    assert.deepEqual(menuLabels('system-input-grade'), ['七年级', '八年级', '九年级']);

    w.document.querySelector('[data-system-input-choice-group="system-input-paper-category"] [data-value="听说考试"]').click();
    choose('system-input-paper-type-search', '期末模拟题', '期末');
    assert.deepEqual(menuLabels('system-input-paper-type-search'), ['阶段测试题', '期末模拟题', '单元测试题']);

    // The platform catalogue arrives through an async adapter. Its picker is
    // still shared, so a selected template must not become its option set.
    await new Promise(resolve => setTimeout(resolve, 0));
    choose('system-input-platform-template-search', '听后应答', '听后');
    assert.deepEqual(menuLabels('system-input-platform-template-search'), ['模仿朗读', '听后应答', '听后回答', '听后选择']);
});

test('共享录入选择器不会截断较长的候选目录', t => {
    const { w, byId, run } = editor(t, 'paper', 1);
    const options = Array.from({ length: 96 }, (_, index) => ({
        value: `province-${index + 1}`,
        label: `省份${index + 1}`,
    }));
    run(`systemInputRegionIndexes = {...systemInputRegionIndexes, provinces: ${JSON.stringify(options)}}; setSystemInputPickerOptions('system-input-province', ${JSON.stringify(options)});`);

    const input = byId('system-input-province');
    const labels = () => [...byId('system-input-province-menu')
        .querySelectorAll('.system-input-picker-option:not(.is-custom) strong')]
        .map(node => node.textContent.trim());
    input.click();
    assert.equal(labels().length, 96);
    assert.equal(labels().at(-1), '省份96');

    const selected = [...byId('system-input-province-menu')
        .querySelectorAll('.system-input-picker-option:not(.is-custom)')]
        .find(node => node.querySelector('strong')?.textContent.trim() === '省份96');
    assert.ok(selected);
});

test('教材目录选择器重开仍展示同一父路径下的全部单元', t => {
    const { w, byId } = editor(t, 'textbook', 1);
    const fieldId = 'system-input-textbook-unit';
    const menuLabels = () => {
        byId(fieldId).click();
        return [...byId(`${fieldId}-menu`).querySelectorAll('.system-input-picker-option:not(.is-custom) strong')]
            .map(node => node.textContent.trim());
    };
    byId(fieldId).value = 'Unit 2';
    byId(fieldId).dispatchEvent(new w.Event('input', { bubbles: true }));
    const option = [...byId(`${fieldId}-menu`).querySelectorAll('.system-input-picker-option:not(.is-custom)')]
        .find(node => node.querySelector('strong')?.textContent.trim() === 'Unit 2');
    assert.ok(option);
    option.click();
    assert.deepEqual(menuLabels(), Array.from({ length: 10 }, (_, index) => `Unit ${index + 1}`));
});

test('选择器恢复输入焦点不会把刚关闭的菜单重新打开', t => {
    const { byId } = editor(t, 'paper', 1);
    const input = byId('system-input-province');
    const menu = byId('system-input-province-menu');
    input.click();
    const option = [...menu.querySelectorAll('.system-input-picker-option:not(.is-custom)')]
        .find(node => node.querySelector('strong')?.textContent.trim() === '湖南省');
    assert.ok(option);
    byId('system-input-paper-name').focus();
    option.click();
    assert.equal(menu.hidden, true);
    assert.equal(input.getAttribute('aria-expanded'), 'false');
});

test('级联选择器搜索不会提前写入草稿或清空已选子级', t => {
    const { w, byId, run } = editor(t, 'paper', 1);
    const province = byId('system-input-province');
    const city = byId('system-input-city');
    const districts = byId('system-input-districts');
    const stage = byId('system-input-stage');
    const grade = byId('system-input-grade');

    w.addSystemInputDistrictChoice({ id: 440106, name: '天河区' });
    assert.equal(byId('system-input-district-chips').children.length, 1);

    province.click();
    province.value = '湖';
    province.dispatchEvent(new w.Event('input', { bubbles: true }));
    assert.equal(city.value, '广州市');
    assert.equal(city.disabled, false);
    assert.equal(districts.disabled, false);
    assert.equal(byId('system-input-district-chips').children.length, 1);
    assert.equal(run('systemInputUnitDrafts.get("unit-1").provinceId.name'), '广东省');

    city.click();
    city.value = '深';
    city.dispatchEvent(new w.Event('input', { bubbles: true }));
    assert.equal(districts.disabled, false);
    assert.equal(byId('system-input-district-chips').children.length, 1);
    assert.equal(run('systemInputUnitDrafts.get("unit-1").cityId.name'), '广州市');

    stage.click();
    stage.value = '高';
    stage.dispatchEvent(new w.Event('input', { bubbles: true }));
    assert.equal(grade.value, '七年级');
    assert.equal(grade.disabled, false);
    assert.equal(run('systemInputUnitDrafts.get("unit-1").stageId.name'), '初中');
});

test('固定选择器未提交搜索时关闭或保存仍保留原级联值', t => {
    const { w, byId, run } = editor(t, 'paper', 1);
    const province = byId('system-input-province');
    const city = byId('system-input-city');

    province.click();
    province.value = '湖';
    province.dispatchEvent(new w.Event('input', { bubbles: true }));
    // Saving can read the form directly while the picker is still open.
    run('saveSystemInputCurrentUnitDraft(currentWorkspace.system_input)');
    assert.equal(run('systemInputUnitDrafts.get("unit-1").provinceId.name'), '广东省');
    assert.equal(run('systemInputUnitDrafts.get("unit-1").cityId.name'), '广州市');

    w.closeSystemInputPicker();
    assert.equal(province.value, '广东省');
    assert.equal(city.value, '广州市');
    assert.equal(city.disabled, false);
});

test('考试类型搜索未提交时不会覆盖已提交的隐藏值', t => {
    const { w, byId, run } = editor(t, 'paper', 1);
    w.document.querySelector('[data-system-input-choice-group="system-input-paper-category"] [data-value="听说考试"]').click();
    const input = byId('system-input-paper-type-search');
    input.click();
    input.value = '期末';
    input.dispatchEvent(new w.Event('input', { bubbles: true }));
    const option = [...byId('system-input-paper-type-search-menu')
        .querySelectorAll('.system-input-picker-option:not(.is-custom)')]
        .find(node => node.querySelector('strong')?.textContent.trim() === '期末模拟题');
    assert.ok(option);
    option.click();
    assert.equal(byId('system-input-paper-type').value, '期末模拟题');

    input.click();
    input.value = '阶段';
    input.dispatchEvent(new w.Event('input', { bubbles: true }));
    run('saveSystemInputCurrentUnitDraft(currentWorkspace.system_input)');
    assert.equal(run('systemInputUnitDrafts.get("unit-1").paperType.name'), '期末模拟题');

    w.closeSystemInputPicker();
    assert.equal(input.value, '期末模拟题');
});

test('paper batch fields use the detected category and only expose exam type for listening papers', t => {
    const { w, byId, run, read } = editor(t, 'paper', 3);
    run(`const current = {...systemInputUnitDrafts.get('unit-1'), paperCategory:'听说考试'};
        delete current.paperType;
        systemInputUnitDrafts.set('unit-1', current);
        currentWorkspace.system_input.suggested_configuration = {paper_category:'听说考试', paper_category_status:'suggested'};
        refreshSystemInputTargetEditor();`);
    byId('target-rows').querySelector('input[type="checkbox"]').click();
    byId('target-batch-open').click();
    const batchChecks = [...byId('target-batch-fields').querySelectorAll('input[type="checkbox"]')];
    assert.ok(batchChecks.length > 0);
    assert.ok(batchChecks.every(input => input.checked === false), 'batch fields must not start selected');
    const field = label => [...byId('target-batch-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);
    ['省份', '城市', '学段', '年级', '考试类型'].forEach(label => {
        assert.equal(field(label).querySelector('select')?.tagName, 'SELECT', label);
    });
    assert.equal(field('试卷分类'), undefined);
    assert.equal(byId('target-batch-category-tag').textContent, '听说考试');
    assert.equal(byId('target-batch-category-note').textContent, '文档自动识别');
    assert.ok(field('区县').querySelector('.target-district-picker'), '区县 uses the tag multi-select picker');
    assert.equal(field('平台题型模板').querySelector('select')?.tagName, 'SELECT');
    assert.ok(field('平台题型模板').querySelector('.target-select'));
    assert.ok(field('答题时间（分钟）').querySelector('.target-number'));
    const paperType = field('考试类型');
    assert.equal(paperType.querySelector('input[type="checkbox"]').disabled, false);
    paperType.querySelector('input[type="checkbox"]').click();
    const paperTypeSelect = paperType.querySelector('select');
    paperTypeSelect.value = [...paperTypeSelect.options]
        .find(option => option.textContent === '阶段测试题')?.value || '';
    paperTypeSelect.dispatchEvent(new w.Event('change', { bubbles: true }));
    byId('target-batch-mode').value = 'overwrite';
    byId('target-batch-mode').dispatchEvent(new w.Event('change', { bubbles: true }));
    byId('target-batch-apply').click();
    assert.equal(read()[0].paperCategory, '听说考试');
    assert.equal(read()[0].paperType.name, '阶段测试题');
});

test('paper batch hides the auto-detected category and irrelevant exam type for special papers', t => {
    const { byId } = editor(t, 'paper', 3);
    byId('target-rows').querySelector('input[type="checkbox"]').click();
    byId('target-batch-open').click();
    const field = label => [...byId('target-batch-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);
    assert.equal(byId('target-batch-category-tag').textContent, '题型专项');
    assert.equal(field('试卷分类'), undefined);
    assert.equal(field('考试类型'), undefined);
});

test('paper batch stage selection unlocks grade in order', t => {
    const { w, byId } = editor(t, 'paper', 3);
    byId('target-rows').querySelector('input[type="checkbox"]').click();
    byId('target-batch-open').click();
    const field = label => [...byId('target-batch-fields').querySelectorAll('.target-field')]
        .find(node => node.querySelector(':scope > span')?.textContent === label);
    const stage = field('学段');
    const grade = field('年级');
    assert.equal(grade.querySelector('input[type="checkbox"]').disabled, true);
    stage.querySelector('input[type="checkbox"]').click();
    assert.equal(field('年级').querySelector('input[type="checkbox"]').disabled, false);
    const stageSelect = field('学段').querySelector('select');
    stageSelect.value = [...stageSelect.options].find(option => option.textContent === '初中')?.value || '';
    stageSelect.dispatchEvent(new w.Event('change', { bubbles: true }));
    const unlockedGrade = field('年级');
    assert.equal(unlockedGrade.querySelector('input[type="checkbox"]').disabled, false);
    assert.equal(unlockedGrade.querySelector('select').disabled, true);
    unlockedGrade.querySelector('input[type="checkbox"]').click();
    assert.equal(field('年级').querySelector('.target-select-trigger').disabled, false);
    assert.ok([...field('年级').querySelectorAll('select option')].some(option => option.textContent === '七年级'));
});

test('切回题型专项会清除听说考试类型的隐藏值和可见选择', t => {
    const { w, byId } = editor(t, 'paper', 1);
    w.setSystemInputField('system-input-paper-category', '听说考试');
    w.setSystemInputField('system-input-paper-type', '期末模拟题');
    w.syncSystemInputPickerInput('system-input-paper-type-search', '期末模拟题', '期末模拟题');
    assert.equal(byId('system-input-paper-type').value, '期末模拟题');
    assert.equal(byId('system-input-paper-type-search').value, '期末模拟题');

    w.setSystemInputField('system-input-paper-category', '题型专项');
    byId('system-input-paper-category').dispatchEvent(new w.Event('change', { bubbles: true }));

    assert.equal(byId('system-input-paper-type').value, '');
    assert.equal(byId('system-input-paper-type-search').value, '');
    assert.equal(byId('system-input-paper-type-section').hidden, true);
});

test('boundary review uses the same modal lock as the target editor', t => {
    const { w, byId } = editor(t, 'paper', 3);
    const workspace = w.__workspace;
    const systemInput = workspace.system_input;
    systemInput.unit_count_status = 'multiple_candidate';
    systemInput.units[0].evidence = {
        unit_grouping: {
            candidate_boundaries: [
                { item_ids: ['item-1'] },
                { item_ids: ['item-2'] },
            ],
        },
    };
    w.renderSystemInputBoundaryReview(systemInput, workspace);
    const boundary = byId('system-input-boundary-review');
    assert.equal(boundary.hidden, false);
    assert.equal(boundary.classList.contains('is-open'), true);
    assert.match(w.document.body.className, /target-modal-open/);
    assert.equal(byId('system-input-drawer').querySelector('.system-input-drawer-panel').inert, true);

    systemInput.unit_count_status = 'single';
    w.renderSystemInputBoundaryReview(systemInput, workspace);
    assert.equal(boundary.hidden, true);
    assert.equal(boundary.classList.contains('is-open'), false);
    assert.doesNotMatch(w.document.body.className, /target-modal-open/);
});
