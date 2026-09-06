/** Renderer module: delivery.fileList */
(function attachRendererFeature_delivery_fileList(root) {
    'use strict';

// ============================================================================
// 文件列表更新
// ============================================================================

function updateFileList(event) {
    if (event.file_list && event.file_list.length > 0) {
        generatedFiles = event.file_list;
    }
}


registerRendererModule("delivery.fileList", {
    updateFileList,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

