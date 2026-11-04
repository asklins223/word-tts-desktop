'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { JSDOM } = require('jsdom');

test('武汉新题型核对页展示两节应答、答案和各自图片', async () => {
    const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'outside-only' });
    const { window } = dom;
    let reviewDocument;
    const imageReads = [];
    window.registerRendererModule = (_name, module) => { reviewDocument = module; };
    window.reviewDisplayFactValue = (value, fallback = '') => String(value ?? '').trim() || fallback;
    window.reviewContentForItem = item => item?.normalized_content || '';
    window.URL.createObjectURL = () => 'blob:test-image';
    window.URL.revokeObjectURL = () => {};
    window.WORDTTS_RENDERER = { getModule: () => ({
        readArtifactBytes: async id => { imageReads.push(id); return new Uint8Array([137, 80, 78, 71]); },
    }) };
    const file = path.join(__dirname, '..', 'renderer', 'modules', 'review', 'document.js');
    const source = fs.readFileSync(file, 'utf8').replace(
        'registerRendererModule("review.document", {',
        'registerRendererModule("review.document", { renderDocumentPageFacts,',
    );
    window.eval(source);

    const first = window.document.createElement('div');
    const second = window.document.createElement('div');
    const retelling = window.document.createElement('div');
    window.document.body.append(first, second, retelling);
    const tasks = [];
    reviewDocument.renderDocumentPageFacts(first, {
        type: '听后应答', questions: [{
            number: 15, section: '第一节', prompt: 'What is this?', image_artifact_id: 'image-q15',
            options: [{ option_id: 'A', text: '★ Biology.' }], answer: 'A', score: 1,
        }],
    }, { metadata: {} }, tasks);
    reviewDocument.renderDocumentPageFacts(second, {
        type: '听后应答', questions: [{
            number: 16, number_inferred: true, section: '第二节',
            listening_text: 'W: What is it?\nM: English.', prompt: 'What is the subject?',
            reference_answers_source: 'document', reference_answers: ['English.', 'It is English.'],
        }],
    }, { metadata: {} }, tasks);
    reviewDocument.renderDocumentPageFacts(retelling, {
        type: '听后转述', recording: { listening_text: 'I like IT.', image_artifact_id: 'image-map' },
        retelling: { prompt: 'John likes IT.', reference_answers: ['John likes IT.', 'IT is his favourite.'] },
    }, { metadata: {} }, tasks);
    await Promise.all(tasks.map(hydrate => hydrate()));

    assert.match(first.textContent, /题目配图/);
    assert.match(second.textContent, /W: What is it\?/);
    assert.match(second.textContent, /屏幕问题/);
    assert.match(second.textContent, /题号根据相邻题目推断/);
    assert.match(second.textContent, /It is English\./);
    assert.match(retelling.textContent, /听后转述思维导图/);
    assert.match(retelling.textContent, /IT is his favourite\./);
    assert.deepEqual(imageReads, ['image-q15', 'image-map']);
    assert.equal(first.querySelector('img').hidden, false);
    assert.equal(retelling.querySelector('img').hidden, false);
    dom.window.close();
});
