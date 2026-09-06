/** Renderer module: review.page */
(function attachRendererFeature_review_page(root) {
    'use strict';

function renderContentReview(parseResults = currentSession?.parse_results) {
    const workspace = authoritativeWorkspace();
    const workspaceGroups = Array.isArray(workspace?.items)
        ? workspaceItemsToParseResults(workspace)
        : null;
    const groups = workspaceGroups || (Array.isArray(parseResults) ? parseResults : []);
    const orderedGroups = reviewGroupsInDocumentOrder(reviewGroupsWithUnits(groups));
    const summary = summarizeParseResults(groups);
    const items = reviewItemsInDocumentOrder(orderedGroups);
    items.forEach((item, index) => { item.reviewIndex = index; });
    const audioTypeCount = new Set(items.map(item => {
        const typePath = reviewTypePathForItem(item, item?.doc_type || '未分类');
        return typePath[0] || item?.doc_type || '未分类';
    }).filter(Boolean)).size;
    const audioOverview = `${items.length || summary.total} 条音频 · ${audioTypeCount} 类题型`;
    // Do not let an initial/partial workspace fall back to renderer-side
    // type detection.  The service projection is the only authority for
    // opening the document-entry flow.
    const entrySupport = reviewDocumentEntrySupport(
        items,
        workspace?.system_input || {},
    );
    const reviewShell = ensureReviewViewShell();
    const activeReviewViewMode = applyReviewViewMode(entrySupport.supported === true);
    const unitPresentation = reviewUnitCountPresentation(orderedGroups, workspace?.system_input);
    const unitModels = buildReviewUnitModels(orderedGroups, items, workspace?.system_input);
    const isAudioView = activeReviewViewMode === 'audio';
    if ($('review-unit-status-label')) {
        $('review-unit-status-label').textContent = isAudioView && entrySupport.supported
            ? '音频目录'
            : (entrySupport.supported ? `${entrySupport.label} · 已支持` : '文稿录入暂不支持');
    }
    if ($('review-unit-status-count')) {
        $('review-unit-status-count').textContent = isAudioView && entrySupport.supported
            ? audioOverview
            : (entrySupport.supported
                ? `${unitModels.length} 个录入单元`
                : (entrySupport.reason || '当前仅保留音频核对'));
    }
    const outline = $('review-outline');
    const reviewItems = $('review-items');
    const empty = $('review-empty');
    if ($('review-summary')) {
        $('review-summary').textContent = isAudioView
            ? audioOverview
            : `${summary.total} 条 · ${unitPresentation.short}`;
    }
    if ($('review-item-count')) $('review-item-count').textContent = `${items.length || summary.total} 条`;
    const renderedItems = items.slice(0, REVIEW_RENDER_LIMIT);
    if (outline) {
        renderReviewOutline(
            outline,
            buildReviewOutlineModel(orderedGroups, renderedItems, { flattenSingleUnit: isAudioView }),
        );
    }
    if (reviewShell) {
        renderReviewDocumentView(
            unitModels,
            unitPresentation,
            entrySupport,
            workspace?.source_filename || currentSession?.source_filename || '',
        );
    }
    if (!reviewItems) return;
    reviewItems.replaceChildren();
    if (items.length === 0) {
        reviewSelectedItemId = '';
        reviewSelectedIndex = null;
        if (empty) empty.hidden = false;
        syncReviewOutlineSelection(null);
        syncReviewDocumentSelection(null);
        renderReviewInspector(null);
        return;
    }
    if (empty) empty.hidden = true;
    const fragment = document.createDocumentFragment();
    renderedItems.forEach((item, index) => {
        const row = document.createElement('article');
        row.className = 'review-item-row';
        row.setAttribute('role', 'listitem');
        row.tabIndex = 0;
        row.setAttribute('aria-current', 'false');
        const typePath = reviewTypePathForItem(item, item.doc_type || '未分类');
        const voicePresentation = reviewVoicePresentation(item);
        row.setAttribute('aria-label', `查看第 ${index + 1} 条${typePath.length ? ` ${typePath.join('，')}` : ''}`);
        if (item.item_id) row.dataset.itemId = String(item.item_id);
        row.dataset.reviewIndex = String(index);
        row.classList.add(`is-${String(item.status || 'PENDING').toLowerCase()}`);
        const indexEl = document.createElement('span');
        indexEl.className = 'review-item-index';
        indexEl.textContent = String(index + 1).padStart(2, '0');
        const body = document.createElement('div');
        body.className = 'review-item-body';
        const meta = document.createElement('div');
        meta.className = 'review-item-meta';
        const type = document.createElement('strong');
        type.textContent = typePath[0] || '未分类';
        meta.appendChild(type);
        if (typePath.length > 1) {
            const typeDetail = document.createElement('span');
            typeDetail.className = 'review-item-type-path';
            typeDetail.textContent = `小题 · ${typePath.slice(1).join(' / ')}`;
            meta.appendChild(typeDetail);
        }
        if (voicePresentation.role) {
            const role = document.createElement('span');
            role.className = 'review-item-role';
            role.textContent = voicePresentation.role;
            meta.appendChild(role);
        }
        const voice = document.createElement('span');
        voice.className = 'review-item-voice';
        voice.textContent = voicePresentation.voice;
        meta.appendChild(voice);
        const audioState = document.createElement('span');
        audioState.className = 'review-item-audio-state';
        audioState.textContent = item.audio_artifact_id ? '已绑定音频' : '待生成音频';
        meta.appendChild(audioState);
        const itemText = reviewContentForItem(item);
        const contentRef = item.content_ref;
        const editable = reviewItemIsEditable(item, workspace);
        let contentPreview = null;
        let content;
        if (editable) {
            content = document.createElement('textarea');
            content.className = 'review-item-editor';
            content.value = itemText;
            content.rows = Math.min(8, Math.max(3, itemText.split('\n').length));
            content.placeholder = contentRef && !itemText
                ? '正文较长，请先加载全文'
                : '输入要提交给讯飞的正文';
            content.disabled = Boolean(contentRef && !itemText);
            content.dataset.reviewEditor = String(item.item_id || '');
            content.setAttribute('aria-label', `编辑第 ${index + 1} 条内容`);
            contentPreview = document.createElement('p');
            contentPreview.className = 'review-item-content-preview';
            contentPreview.textContent = itemText || (contentRef ? '（正文较长，点击“加载全文”查看）' : '（无可预览文本）');
            content.addEventListener('input', () => {
                if (contentPreview) contentPreview.textContent = content.value || '（无可预览文本）';
            });
        } else {
            content = document.createElement('p');
            content.className = 'review-item-content';
            content.textContent = itemText || (contentRef ? '（正文较长，点击“加载全文”查看）' : '（无可预览文本）');
        }
        const locator = item.source_locator || item.sourceLocator || item.metadata?.source_locator;
        const contentNodes = contentPreview ? [content, contentPreview] : [content];
        if (locator) {
            const source = document.createElement('small');
            source.className = 'review-item-locator';
            source.textContent = `来源：${String(locator).slice(0, 180)}`;
            body.append(meta, source, ...contentNodes);
        } else {
            body.append(meta, ...contentNodes);
        }
        const details = document.createElement('div');
        details.className = 'review-item-details';
        if (item.skip_reason) {
            const skip = document.createElement('span');
            skip.className = 'review-item-skip-reason';
            skip.textContent = `跳过原因：${item.skip_reason}`;
            details.appendChild(skip);
        }
        if (contentRef && !itemText) {
            const loadButton = document.createElement('button');
            loadButton.type = 'button';
            loadButton.className = 'btn-ghost btn-sm review-item-load';
            loadButton.textContent = '加载全文';
            loadButton.addEventListener('click', () => { void loadReviewItemContent(item, loadButton); });
            details.appendChild(loadButton);
        }
        if (editable) {
            const saveButton = document.createElement('button');
            saveButton.type = 'button';
            saveButton.className = 'btn-secondary btn-sm';
            saveButton.textContent = '保存修改';
            saveButton.addEventListener('click', async () => {
                if (content.disabled) return;
                const nextText = String(content.value || '').trim();
                if (!nextText) {
                    showToast('条目正文不能为空', 'error');
                    content.focus();
                    return;
                }
                saveButton.disabled = true;
                saveButton.setAttribute('aria-busy', 'true');
                try {
                    await patchCurrentWorkspaceItem(item.item_id, { normalized_content: nextText });
                    showToast('条目修改已保存');
                } catch (error) {
                    console.error('保存条目修改失败:', error);
                    if (error?.code === 'STATE_CONFLICT' || error?.code === 'CONFIGURATION_CONFLICT') {
                        await hydrateWorkflowWorkspace(currentSession?.session_id, { silent: true });
                    }
                    showToast(workflowAdapter.issueMessage?.(error, '条目修改未保存')?.message || '条目修改未保存', 'error');
                } finally {
                    saveButton.disabled = false;
                    saveButton.removeAttribute('aria-busy');
                }
            });
            details.appendChild(saveButton);
            const skipButton = document.createElement('button');
            skipButton.type = 'button';
            skipButton.className = 'btn-ghost btn-sm';
            const isSkipped = String(item.status || '').toUpperCase() === 'SKIPPED';
            skipButton.textContent = isSkipped ? '恢复条目' : '跳过条目';
            skipButton.addEventListener('click', async () => {
                let skipReason = item.skip_reason || '';
                if (!isSkipped) {
                    skipReason = await showPromptDialog(
                        '跳过这条内容？',
                        '跳过后它不会提交给讯飞，也不会进入交付范围。之后仍可恢复；请填写原因。',
                        '用户跳过',
                    );
                    if (skipReason === null) return;
                    skipReason = String(skipReason || '用户跳过').trim().slice(0, 500) || '用户跳过';
                }
                skipButton.disabled = true;
                try {
                    await patchCurrentWorkspaceItem(item.item_id, isSkipped
                        ? { status: 'PENDING', skip_reason: null }
                        : { status: 'SKIPPED', skip_reason: skipReason });
                    showToast(isSkipped ? '条目已恢复' : '条目已跳过');
                } catch (error) {
                    console.error('更新条目状态失败:', error);
                    showToast(workflowAdapter.issueMessage?.(error, '条目状态未更新')?.message || '条目状态未更新', 'error');
                } finally {
                    skipButton.disabled = false;
                }
            });
            details.appendChild(skipButton);
        }
        if (details.childElementCount > 0) body.appendChild(details);
        row.append(indexEl, body);
        const selectRow = event => {
            if (event?.target?.closest('button, textarea, input, select')) return;
            selectReviewItem(item, index, row);
        };
        row.addEventListener('click', selectRow);
        row.addEventListener('keydown', event => {
            if (event.target !== row || !['Enter', ' '].includes(event.key)) return;
            event.preventDefault();
            selectReviewItem(item, index, row);
        });
        fragment.appendChild(row);
    });
    if (items.length > REVIEW_RENDER_LIMIT) {
        const more = document.createElement('p');
        more.className = 'review-truncated-note';
        more.textContent = `已展示前 ${REVIEW_RENDER_LIMIT} 条，剩余 ${items.length - REVIEW_RENDER_LIMIT} 条仍会按完整解析结果生成。`;
        fragment.appendChild(more);
    }
    reviewItems.appendChild(fragment);
    const previousSelectedId = reviewSelectedItemId;
    const previousSelectedIndex = reviewSelectedIndex;
    let selectedIndex = previousSelectedId
        ? renderedItems.findIndex(item => String(item?.item_id || '') === previousSelectedId)
        : -1;
    if (selectedIndex < 0 && Number.isInteger(previousSelectedIndex)
        && previousSelectedIndex >= 0 && previousSelectedIndex < renderedItems.length) {
        selectedIndex = previousSelectedIndex;
    }
    if (selectedIndex < 0) selectedIndex = 0;
    const selectedItem = renderedItems[selectedIndex];
    const selectedRow = [...reviewItems.querySelectorAll('.review-item-row')]
        .find(entry => Number(entry.dataset.reviewIndex) === selectedIndex);
    if (selectedItem && selectedRow) {
        selectReviewItem(selectedItem, selectedIndex, selectedRow);
    } else {
        reviewSelectedItemId = '';
        reviewSelectedIndex = null;
        syncReviewOutlineSelection(null);
        renderReviewInspector(null);
    }
}

function showContentReview() {
    currentView = 'workflow';
    currentStep = 2;
    setActiveWorkspaceView('review');
    renderContentReview();
    $$('.step-page').forEach(page => page.classList.remove('active'));
    $('page-2')?.classList.add('active');
    updateStepper();
    // updateStepper re-renders the workspace shell and may run after an
    // in-flight workspace refresh. Keep the visible nested view authoritative
    // for this navigation action.
    setActiveWorkspaceView('review');
    const scrollPage = $('page-2')?.querySelector('.page-scroll');
    if (scrollPage) scrollPage.scrollTop = 0;
    requestAnimationFrame(() => $('review-title')?.focus({ preventScroll: true }));
}


registerRendererModule("review.page", {
    renderContentReview,
    showContentReview,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
