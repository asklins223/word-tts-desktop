/** Renderer module: review.actions */
(function attachRendererFeature_review_actions(root) {
    'use strict';

function renderReviewInspector(item, index = 0) {
    const empty = $('review-inspector-empty');
    const content = $('review-inspector-content');
    if (!item) {
        if (empty) empty.hidden = false;
        if (content) content.hidden = true;
        return;
    }
    if (empty) empty.hidden = true;
    if (content) content.hidden = false;
    const typePath = reviewTypePathForItem(item, item.doc_type || item.category || '未分类');
    const itemTitle = typePath[0] || String(item.doc_type || item.category || '解析条目');
    const voicePresentation = reviewVoicePresentation(item);
    const locator = item.source_locator || item.sourceLocator || item.metadata?.source_locator;
    const sourceLabel = locator
        ? String(locator)
        : (item.content_ref?.content_id ? '正文按需加载' : '未提供');
    const body = reviewContentForItem(item);
    if ($('review-inspector-index')) $('review-inspector-index').textContent = String(index + 1).padStart(2, '0');
    if ($('review-inspector-item-title')) $('review-inspector-item-title').textContent = itemTitle;
    if ($('review-inspector-status')) {
        const status = $('review-inspector-status');
        status.textContent = reviewStatusLabel(item.status);
        status.className = `inspector-status is-${String(item.status || 'PENDING').toLowerCase()}`;
    }
    if ($('review-inspector-source')) $('review-inspector-source').textContent = sourceLabel;
    if ($('review-inspector-type')) $('review-inspector-type').textContent = typePath.join(' / ');
    if ($('review-inspector-voice')) $('review-inspector-voice').textContent = voicePresentation.voice;
    if ($('review-inspector-body')) $('review-inspector-body').textContent = body || (item.content_ref ? '正文较长，请点击条目中的“加载全文”。' : '（无可预览文本）');

    const inspectorFacts = content?.querySelector('.inspector-facts');
    inspectorFacts?.querySelectorAll('.inspector-audio-binding').forEach(row => row.remove());
    const appendFact = (label, value) => {
        if (!inspectorFacts) return;
        const row = document.createElement('div');
        row.className = 'inspector-audio-binding';
        const key = document.createElement('dt');
        key.textContent = label;
        const fact = document.createElement('dd');
        fact.textContent = value;
        row.append(key, fact);
        inspectorFacts.appendChild(row);
    };
    const audioId = reviewDocumentItemValue(item, ['audio_artifact_id', 'audioArtifactId']);
    if (audioId) appendFact('音频绑定', `已绑定 · ${audioId}`);
}

function selectReviewItem(item, index, row) {
    const selectedIndex = Number(index);
    reviewSelectedIndex = Number.isInteger(selectedIndex) && selectedIndex >= 0 ? selectedIndex : null;
    reviewSelectedItemId = item?.item_id ? String(item.item_id) : '';
    $$('.review-item-row').forEach(entry => {
        const selected = entry === row;
        entry.classList.toggle('is-selected', selected);
        entry.setAttribute('aria-current', selected ? 'true' : 'false');
    });
    syncReviewOutlineSelection(index);
    syncReviewDocumentSelection(index);
    renderReviewInspector(item, index);
}

async function loadReviewItemContent(item, button) {
    const workflowId = currentSession?.session_id;
    const contentRef = item?.content_ref;
    if (!workflowId || !contentRef?.content_id || !workflowApi?.getItemContent) return;
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        button.textContent = '加载中…';
    }
    try {
        const workspace = authoritativeWorkspace(workflowId);
        const expectedStateVersion = workspace?.snapshot?.state_version;
        const maxResponseBytes = Math.max(
            1024,
            Math.min(65536, Number(contentRef.max_response_bytes) || 65536),
        );
        let offsetBytes = 0;
        let completeContent = '';
        let loaded = false;
        // Long item bodies are addressable through content_ref and read in
        // bounded UTF-8 chunks.  Never turn a partial response into editable
        // text or loop forever if a malformed server projection stops making
        // progress.
        for (let chunkIndex = 0; chunkIndex < 2048; chunkIndex += 1) {
            const response = await workflowApi.getItemContent(
                workflowId,
                item.item_id,
                contentRef.content_id,
                expectedStateVersion,
                { offsetBytes, maxResponseBytes },
            );
            if (typeof response?.content !== 'string') throw new Error('服务端未返回条目正文');
            completeContent += response.content;
            if (response.truncated !== true) {
                loaded = true;
                break;
            }
            const nextOffset = Number(response.next_offset_bytes);
            if (!Number.isSafeInteger(nextOffset) || nextOffset <= offsetBytes) {
                throw new Error('服务端条目正文分块没有向前推进');
            }
            offsetBytes = nextOffset;
        }
        if (!loaded) throw new Error('条目正文分块数量超过安全上限');
        rememberItemContentCache(item.item_id, completeContent, workflowId);
        renderContentReview();
        showToast('已加载条目全文');
    } catch (error) {
        console.error('加载条目全文失败:', error);
        if (error?.code === 'STATE_CONFLICT') await hydrateWorkflowWorkspace(workflowId, { silent: true });
        showToast(workflowAdapter.issueMessage?.(error, '条目全文暂时无法加载')?.message || '条目全文暂时无法加载', 'error');
    } finally {
        if (button) {
            button.disabled = false;
            button.removeAttribute('aria-busy');
            button.textContent = '加载全文';
        }
    }
}

async function patchCurrentWorkspaceItem(itemId, patch) {
    const workflowId = currentSession?.session_id;
    const workspace = authoritativeWorkspace(workflowId);
    if (!workflowId || !workspace || !workflowApi?.patchWorkspace) throw new Error('当前任务工作区不可编辑');
    const action = workspaceAction('SAVE_CONFIGURATION', workspace);
    if (action?.enabled !== true) {
        const error = new Error(action?.reason || '当前任务状态不允许编辑条目');
        error.code = 'CONFIG_FROZEN';
        throw error;
    }
    const stateVersion = Number(workspace.snapshot?.state_version);
    const configurationRevision = Number(workspace.configuration?.configuration_revision);
    if (!Number.isInteger(stateVersion) || !Number.isInteger(configurationRevision)) {
        throw new Error('工作区版本缺失，无法安全保存条目');
    }
    const updated = await workflowApi.patchWorkspace(workflowId, {
        expected_state_version: stateVersion,
        configuration_revision: configurationRevision,
        item_overrides: [{ item_id: String(itemId), patch }],
    }, {
        idempotencyKey: `renderer-item-edit-${workflowId}-${itemId}-${stateVersion}-${Date.now()}`,
    });
    if (!updated) throw new Error('服务端未返回更新后的工作区');
    if (typeof patch.normalized_content === 'string') {
        rememberItemContentCache(itemId, patch.normalized_content, workflowId);
    }
    currentWorkspace = updated;
    currentSession.parse_results = workspaceItemsToParseResults(updated);
    mergeWorkflowSnapshotIntoSession(updated.snapshot, currentSession);
    workflowStore?.hydrate?.(updated, { snapshot: updated.snapshot });
    if (typeof renderWorkspaceAfterHydrate === 'function') {
        renderWorkspaceAfterHydrate(updated, updated.snapshot, currentSession?.session_id);
    } else {
        renderWorkspaceShell(updated, updated.snapshot);
    }
    updateSessionLabels(currentSession.source_filename, currentSession.parse_results);
    renderContentReview(currentSession.parse_results);
    return updated;
}


registerRendererModule("review.actions", {
    renderReviewInspector,
    selectReviewItem,
    loadReviewItemContent,
    patchCurrentWorkspaceItem,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
