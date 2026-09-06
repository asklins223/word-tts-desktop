/** Renderer module: core.dom */
(function attachRendererFeature_core_dom(root) {
    'use strict';

// ============================================================================
// DOM 引用
// ============================================================================

const $ = (id) => document.getElementById(id);
const $$ = (sel) => document.querySelectorAll(sel);


registerRendererModule("core.dom", {
    $,
    $$,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

