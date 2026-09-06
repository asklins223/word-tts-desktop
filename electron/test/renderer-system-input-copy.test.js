const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');

function loadCopyModule() {
    const dom = new JSDOM('<!doctype html><html><body></body></html>');
    let copiedText = '';
    dom.window.document.execCommand = command => {
        if (command === 'copy') copiedText = dom.window.document.activeElement?.value || '';
        return command === 'copy';
    };
    const toasts = [];
    const context = vm.createContext({
        console,
        document: dom.window.document,
        window: dom.window,
        navigator: dom.window.navigator,
        showToast: (message, tone) => toasts.push({ message, tone }),
        setTimeout,
        clearTimeout,
    });
    const renderer = path.join(__dirname, '..', 'renderer');
    for (const filename of [
        'modules/core/module-bridge.js',
        'modules/system-input/delivery.js',
    ]) {
        vm.runInContext(fs.readFileSync(path.join(renderer, filename), 'utf8'), context, { filename });
    }
    return {
        api: context.WORDTTS_RENDERER.modules['systemInput.delivery'],
        document: dom.window.document,
        copiedText: () => copiedText,
        toasts,
    };
}

test('审阅文档名支持逐项复制和多套按行批量复制', async () => {
    const { api, document, copiedText, toasts } = loadCopyModule();
    const scope = document.createElement('section');
    const first = api.createSystemInputDocumentCopyButton('第一套试卷');
    const second = api.createSystemInputDocumentCopyButton('第二套课文');
    const all = document.createElement('button');
    all.type = 'button';
    all.innerHTML = '<span data-copy-label>复制全部文档名</span>';
    scope.append(first, second, all);
    document.body.appendChild(scope);

    await api.copySystemInputDocumentName(first.dataset.copyText, first);
    assert.equal(copiedText(), '第一套试卷');
    assert.equal(toasts.at(-1).tone, 'success');

    await api.copySystemInputDocumentNames(scope, all);
    assert.equal(copiedText(), '第一套试卷\n第二套课文');
    assert.equal(toasts.at(-1).message, '已复制 2 个审阅文档名');
});
