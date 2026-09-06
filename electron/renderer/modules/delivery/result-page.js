/** Renderer module: delivery.resultPage */
(function attachRendererFeature_delivery_resultPage(root) {
    'use strict';

function buildResultPage(event, suppliedContext = null) {
    destroyWaveSurfers();
    const workflowSourceTotal = summarizeParseResults(currentSession?.parse_results).total;
    const workspace = suppliedContext?.workspace || currentWorkspace;
    const workspaceCounts = workspaceProgress(workspace);
    const context = suppliedContext || {
        mode: 'current',
        sessionId: currentSession?.session_id,
        workflowId: currentSession?.session_id || event.workflow_id || null,
        sourceFilename: currentSession?.source_filename,
        files: generatedFiles,
        completed: event.completed ?? workspaceCounts.completed ?? generatedFiles.length ?? 0,
        failed: event.failed ?? workspaceCounts.failed ?? 0,
        cancelled: event.cancelled ?? workspaceCounts.cancelled ?? 0,
        total: workflowSourceTotal || event.total || workspaceCounts.total || 0,
        format: lastGenerationConfig?.format || currentConfig?.format || 'mp3',
        preview: Boolean(lastGenerationConfig?.preview && workflowSourceTotal > 3),
        zipAvailable: Boolean(currentSession?.delivery?.zip_available || event.zip_artifact_id),
        zipArtifactId: currentSession?.delivery?.zip_artifact_id || event.zip_artifact_id || null,
        failedItems: Array.isArray(event.failed_items) ? event.failed_items : [],
        stateVersion: Number(currentSession?.state_version || 0),
        executionState: currentSession?.execution_state || event.execution_state || null,
        resultStatus: currentSession?.result_status || event.result_status || null,
        workspace,
    };
    activeResultContext = context;
    const finalDeliveryButton = ensureFinalDeliveryResultButton();
    // The result page is also the delivery center for a task opened from
    // history.  Its workspace is fetched independently of currentWorkspace,
    // so render the system-input projection here as well as in the active
    // workflow shell; otherwise historical results lose the saved target,
    // per-unit status and entry IDs entirely.
    renderSystemInputSurface(workspace);
    const isHistory = context.mode === 'history';
    const resultFiles = Array.isArray(context.files) ? context.files : [];
    const success = resultFiles.length;
    const deliveryBlockers = (Array.isArray(workspace?.blockers) ? workspace.blockers : []).filter(blocker => (
        ['BLOCKING', 'ERROR'].includes(String(blocker?.severity || '').toUpperCase())
        && ['ARTIFACT_MISSING_OR_UNVERIFIED', 'ARTIFACT_FORMAT_UNSUPPORTED', 'ARTIFACT_METADATA_CONFLICT'].includes(String(blocker?.code || '').toUpperCase())
    ));
    const deliveryAffectedItemIds = new Set(
        deliveryBlockers.flatMap(blocker => Array.isArray(blocker?.affected_item_ids) ? blocker.affected_item_ids.map(String) : []),
    );
    const deliveryIssueCount = deliveryBlockers.length > 0
        ? Math.max(1, deliveryAffectedItemIds.size)
        : 0;
    const { missingFiles, failed, cancelled, unresolved } = resultSummaryCounts(
        context,
        success,
        workspaceCounts,
        deliveryIssueCount,
    );
    const hasDeliveryIssue = deliveryIssueCount > 0;
    const resultTitle = $('result-title');
    const resultEyebrow = $('result-eyebrow');
    const resultIcon = document.querySelector('.result-success-icon');
    const generateFullBtn = $('generate-full-btn');
    const rerunTaskBtn = $('rerun-task-btn');
    const resultWarning = $('result-warning');
    const resultWarningText = $('result-warning-text');
    const failureList = $('result-failure-list');
    const retryFailedBtn = $('retry-failed-btn');
    const warningActions = document.querySelector('.result-warning-actions');
    const backToHistoryBtn = $('back-to-history-btn');
    const sourceTotal = Math.max(0, Number(context.total) || workflowSourceTotal || success + failed + cancelled);
    const isPreviewResult = Boolean(context.preview);
    const failedItems = Array.isArray(context.failedItems) ? context.failedItems : [];

    if (finalDeliveryButton) finalDeliveryButton.hidden = success === 0 && unresolved > 0;

    if (generateFullBtn) {
        generateFullBtn.hidden = isHistory || !lastGenerationConfig?.preview || workflowSourceTotal <= 3 || success === 0;
    }
    if (rerunTaskBtn) {
        const rerunAction = workflowAdapter.action?.(workspace, 'RERUN');
        rerunTaskBtn.hidden = rerunAction?.enabled !== true;
        rerunTaskBtn.disabled = false;
        rerunTaskBtn.title = rerunAction?.enabled === true ? '' : (rerunAction?.reason || '当前任务不能重新运行');
    }
    if (backToHistoryBtn) backToHistoryBtn.hidden = !isHistory;
    if (warningActions) warningActions.hidden = isHistory;
    if (resultWarning) resultWarning.hidden = unresolved === 0;
    if (resultWarningText && unresolved > 0) {
        const availabilityNote = success > 0
            ? '其余已验证音频仍可试听和下载。'
            : '当前没有可试听或下载的音频文件。';
        const historyIssueSummary = [
            failed > 0 ? `${failed} 条未完成或音频缺失` : '',
            cancelled > 0 ? `${cancelled} 条已取消` : '',
        ].filter(Boolean).join('、');
        resultWarningText.textContent = isHistory
            ? (hasDeliveryIssue
                ? `这条历史记录有 ${deliveryIssueCount} 条音频产物尚未通过交付核验${historyIssueSummary ? `；另有 ${historyIssueSummary}` : ''}。${availabilityNote}`
                : `这条历史记录有 ${historyIssueSummary || '部分内容未能生成'}。${availabilityNote}`)
            : (success > 0
                ? `${hasDeliveryIssue ? `有 ${deliveryIssueCount} 条音频产物待交付核验；` : ''}${failed} 条失败、${cancelled} 条已取消。沿用当前设置只重试安全失败项；修改参数后会重新生成全部内容。`
                : (hasDeliveryIssue
                    ? `本次有 ${deliveryIssueCount} 条音频产物尚未通过交付核验，请先重新同步或处理任务详情。`
                    : `本次共有 ${failed} 条失败、${cancelled} 条已取消。请根据任务详情处理后再重试。`));
    }
    if (retryFailedBtn) retryFailedBtn.hidden = isHistory
        || failed === 0
        || !lastGenerationConfig
        || isTerminalWorkflowSnapshot(workspace?.snapshot || currentSession);
    if (failureList) {
        failureList.innerHTML = '';
        const displayedItems = failedItems.slice(0, 5);
        failureList.hidden = displayedItems.length === 0;
        displayedItems.forEach(item => {
            const row = document.createElement('li');
            const name = document.createElement('strong');
            name.textContent = item.id || item.doc_type || '未命名内容';
            name.title = name.textContent;
            const reason = document.createElement('span');
            reason.textContent = item.error || '生成服务未返回具体原因';
            row.appendChild(name);
            row.appendChild(reason);
            failureList.appendChild(row);
        });
        if (failed > displayedItems.length && displayedItems.length > 0) {
            const remaining = document.createElement('li');
            remaining.className = 'result-failure-more';
            remaining.textContent = isHistory
                ? `另有 ${failed - displayedItems.length} 条未完成内容未展开。`
                : `另有 ${failed - displayedItems.length} 条失败内容未展开，重试时会自动包含。`;
            failureList.appendChild(remaining);
        }
    }

    if (resultIcon) resultIcon.classList.remove('has-warning', 'has-error');
    if (success === 0 && unresolved > 0) {
        if (resultEyebrow) resultEyebrow.textContent = hasDeliveryIssue ? '交付需要处理' : (isPreviewResult ? '试听需要处理' : '任务需要处理');
        if (resultTitle) resultTitle.textContent = hasDeliveryIssue ? '音频产物尚未通过核验' : (isPreviewResult ? '本次试听未能生成音频' : '本次任务未能生成音频');
        if (resultIcon) resultIcon.classList.add('has-error');
    } else if (unresolved > 0) {
        if (resultEyebrow) resultEyebrow.textContent = isPreviewResult ? '试听部分完成' : '任务部分完成';
        if (resultTitle) resultTitle.textContent = isPreviewResult ? '部分试听音频已经准备好' : '部分音频已经准备好';
        if (resultIcon) resultIcon.classList.add('has-warning');
    } else if (isPreviewResult) {
        if (resultEyebrow) resultEyebrow.textContent = '试听生成完成';
        if (resultTitle) resultTitle.textContent = '试听音频已经准备好';
    } else if (resultTitle) {
        if (resultEyebrow) resultEyebrow.textContent = '任务已完成';
        resultTitle.textContent = '音频已经准备好了';
    }

    // 摘要
    let summaryText = isHistory
        ? `「${context.sourceFilename || '未命名文档'}」可用 ${success} 个音频文件${unresolved > 0 ? `，${failed} 个失败、${cancelled} 个已取消或缺失` : ''}`
        : (isPreviewResult
            ? `本次试听生成 ${success} 个音频${unresolved > 0 ? `，${failed} 个失败、${cancelled} 个已取消` : ''}；确认效果后可继续生成完整文档`
            : `成功生成 ${success} 个音频文件${unresolved > 0 ? `，${failed} 个失败、${cancelled} 个已取消${hasDeliveryIssue ? `、${deliveryIssueCount} 个产物待核验` : ''}` : ''}`);
    $('result-summary').textContent = summaryText;
    $('result-success-label').textContent = isPreviewResult ? '试听文件' : '已生成';
    $('result-success-count').textContent = String(success);
    $('result-success-caption').textContent = isPreviewResult && !isHistory
        ? `本次范围：前 ${Math.min(sourceTotal, 3)} 条`
        : '音频文件';
    $('result-secondary-label').textContent = isPreviewResult && !isHistory ? '文档总量' : '未完成';
    $('result-failed-count').textContent = String(isPreviewResult && !isHistory ? sourceTotal : unresolved);
    if ($('result-cancelled-count')) $('result-cancelled-count').textContent = String(cancelled);
    const unfinishedCaption = [
        failed > 0 ? (missingFiles > 0 ? '失败或缺失' : '失败') : '',
        cancelled > 0 ? '已取消' : '',
        hasDeliveryIssue ? '待核验' : '',
    ].filter(Boolean).join(' / ') || '待处理内容';
    $('result-secondary-caption').textContent = isPreviewResult && !isHistory
        ? '完整文档内容'
        : unfinishedCaption;
    const resultFormat = resultFiles[0]?.format
        || context.format
        || workspace?.configuration?.effective?.format
        || '';
    $('result-format-value').textContent = String(resultFormat || '待同步').toUpperCase();

    // ZIP 卡片
    const zipCard = $('zip-card');
    const resultHero = $('result-hero');
    const zipFilename = deliveryZipFilename(context.sourceFilename);
    const zipName = zipCard?.querySelector('.zip-name');
    if (zipName) {
        // Show the exact suggested package name in the delivery card. The
        // button used to say only “准备交付包”, which hid a missing/incorrect
        // extension until after the native save dialog opened.
        zipName.textContent = zipFilename;
        zipName.title = zipFilename;
    }
    // The delivery projection is authoritative for an already-created ZIP.
    // A terminal result with verified audio still exposes the on-demand
    // action; the server creates and verifies the ZIP when it is clicked.
    const zipState = resultZipState(context, success);
    if (zipState.visible) {
        // Restore the stylesheet's grid layout. An inline flex override makes
        // the download button stretch into a full-height blue column.
        zipCard.style.removeProperty('display');
        resultHero?.classList.remove('has-no-package');
        const scope = workflowAdapter.deliveryScope?.(workspace || context) || {
            included: [],
            excluded: [],
            reasons: {},
            zipArtifactId: null,
            zipAvailable: false,
        };
        const hasScope = Boolean(
            (workspace || context)?.delivery
            && Array.isArray((workspace || context).delivery.included_item_ids)
            && Array.isArray((workspace || context).delivery.excluded_item_ids),
        );
        const includedCount = scope.included.length;
        const excludedCount = scope.excluded.length;
        const deliveryScopeEl = $('delivery-scope');
        const exclusionNote = $('delivery-exclusion-note');
        const exclusionList = $('delivery-exclusion-list');
        if (deliveryScopeEl) {
            deliveryScopeEl.textContent = `交付范围：${includedCount} 条已验证音频${excludedCount > 0 ? ` · ${excludedCount} 条未纳入` : ''}`;
        }
        if (exclusionNote) {
            exclusionNote.hidden = excludedCount === 0;
            if (excludedCount > 0) {
                const reasonLabels = {
                    ITEM_CANCELLED: '已取消',
                    ITEM_FAILED: '生成失败',
                    ITEM_SKIPPED: '已跳过',
                    REQUIRES_RECONCILE: '未完成',
                    ARTIFACT_MISSING_OR_UNVERIFIED: '产物待核验',
                    ARTIFACT_FORMAT_UNSUPPORTED: '格式未验证',
                    NOT_GENERATED: '尚未生成',
                    NOT_SELECTED: '未选择',
                    ITEM_ARTIFACT_CONFLICT: '产物状态冲突',
                };
                const labels = [...new Set(scope.excluded.map(itemId => reasonLabels[scope.reasons?.[itemId]] || '未纳入'))];
                exclusionNote.textContent = `未纳入原因：${labels.join('、')}`;
            } else {
                exclusionNote.textContent = '';
            }
        }
        if (exclusionList) {
            exclusionList.replaceChildren();
            const details = workflowAdapter.exclusionDetails?.(workspace || context) || scope.excluded.map(itemId => ({
                itemId,
                reasonLabel: scope.reasons?.[itemId] || '未纳入',
                contentPreview: '正文未随列表加载',
            }));
            exclusionList.hidden = details.length === 0;
            details.slice(0, 500).forEach(detail => {
                const row = document.createElement('li');
                const label = document.createElement('strong');
                label.textContent = detail.sequence ? `第 ${detail.sequence} 条 · ${detail.reasonLabel}` : `${detail.itemId} · ${detail.reasonLabel}`;
                const content = document.createElement('span');
                content.textContent = detail.contentPreview || '正文未随列表加载';
                row.append(label, content);
                if (detail.sourceLocator) {
                    const source = document.createElement('small');
                    source.textContent = `来源：${detail.sourceLocator}`;
                    row.appendChild(source);
                }
                exclusionList.appendChild(row);
            });
            if (details.length > 500) {
                const more = document.createElement('li');
                more.className = 'delivery-exclusion-more';
                more.textContent = `另有 ${details.length - 500} 条未展开，完整范围仍由服务端交付投影控制。`;
                exclusionList.appendChild(more);
            }
        }
        $('zip-desc').textContent = zipState.ready
            ? `ZIP 压缩包包含 ${includedCount} 个已验证的音频文件`
            : `点击下载时自动整理 ${includedCount} 个已验证的音频文件`;
        const zipButton = $('download-zip-btn');
        if (zipButton) {
            zipButton.disabled = !hasScope || includedCount === 0;
            zipButton.title = zipButton.disabled ? '等待交付范围核验' : `下载 ${zipFilename}`;
        }
    } else {
        zipCard.style.display = 'none';
        resultHero?.classList.add('has-no-package');
        $('delivery-exclusion-list')?.replaceChildren();
        if ($('delivery-exclusion-list')) $('delivery-exclusion-list').hidden = true;
    }

    // 音频列表
    const audioList = $('audio-list');
    const audioListSection = document.querySelector('.audio-list-section');
    audioList.innerHTML = '';
    prepareAudioFilters(resultFiles);

    if (resultFiles.length === 0) {
        audioList.innerHTML = '<div class="audio-empty">暂无音频文件</div>';
        $('audio-count').textContent = '0 个文件';
        if (audioListSection) audioListSection.hidden = true;
        return;
    }

    if (audioListSection) audioListSection.hidden = false;
    $('audio-count').textContent = `${resultFiles.length} 个文件`;
    const renderToken = waveformRenderToken;
    const itemFragment = document.createDocumentFragment();

    resultFiles.forEach((f, index) => {
        const color = (currentConfig && currentConfig.type_colors && currentConfig.type_colors[f.doc_type]) || '#a8a29e';

        // 使用 DOM API 安全构建，避免 innerHTML 注入风险
        const item = document.createElement('article');
        item.className = 'audio-item';
        item.setAttribute('aria-label', f.filename);
        item.style.setProperty('--item-index', String(Math.min(index, 5)));
        item.dataset.docType = f.doc_type || '';
        item.dataset.searchText = [f.filename, f.doc_type, f.category, f.text, f.text_preview]
            .filter(Boolean)
            .join(' ')
            .toLocaleLowerCase('zh-CN');

        // --- 头部：序号 + 文件信息 + 下载按钮 ---
        const header = document.createElement('div');
        header.className = 'audio-item-header';

        const indexBadge = document.createElement('span');
        indexBadge.className = 'audio-index';
        indexBadge.textContent = String(index + 1).padStart(2, '0');

        const dot = document.createElement('span');
        dot.className = 'audio-dot';
        dot.style.background = color;

        const info = document.createElement('div');
        info.className = 'audio-info';

        const name = document.createElement('div');
        name.className = 'audio-name';
        name.textContent = f.filename;

        const meta = document.createElement('div');
        meta.className = 'audio-meta';
        const metaText = document.createElement('span');
        metaText.textContent = [f.doc_type, f.category].filter(Boolean).join(' · ') || '音频文件';
        meta.appendChild(dot);
        meta.appendChild(metaText);

        info.appendChild(name);
        info.appendChild(meta);

        const dlBtn = document.createElement('button');
        dlBtn.className = 'audio-download-btn';
        dlBtn.title = '下载此文件';
        dlBtn.setAttribute('aria-label', `下载 ${f.filename}`);
        dlBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg><span>下载</span>`;
        dlBtn.addEventListener('click', async () => {
            if (dlBtn.disabled) return;
            dlBtn.disabled = true;
            dlBtn.classList.add('is-busy');
            try {
                await downloadFile(f, context);
            } finally {
                dlBtn.disabled = false;
                dlBtn.classList.remove('is-busy');
            }
        });

        header.appendChild(indexBadge);
        header.appendChild(info);
        header.appendChild(dlBtn);
        item.appendChild(header);

        // 每个生成文件携带本题实际使用的音色；头像优先使用已完成的本机
        // 缓存，缓存尚未完成时先显示目录资源，试听仍保留原始地址回退。
        // 全局只允许一个试听同时播放。
        item.appendChild(createResultVoiceStrip(f));
        item._resultFile = f;

        // 原生 Audio 负责播放；可用时通过 MediaSource 逐块追加 Artifact，
        // 不支持该 MIME 的浏览器才退回到有界 Blob。WaveSurfer 只负责绘制
        // 波形与定位，不再拥有另一份音频数据。
        const audio = new Audio();
        // Do not start multiple Artifact reads while the result list is being
        // painted. Playback and waveform loading request bytes on demand;
        // this keeps the renderer's one-shot ticket streams deterministic.
        audio.preload = 'none';
        item._audioElement = audio;
        item._artifactId = f.artifact_id || null;
        let audioReadyPromise = null;
        let audioObjectUrl = null;
        let audioStreamReader = null;
        let audioMediaSource = null;
        let audioStreamTask = null;
        let audioSourceGeneration = 0;
        const resetAudioSource = () => {
            audioSourceGeneration += 1;
            audio._artifactStreamError = null;
            const reader = audioStreamReader;
            audioStreamReader = null;
            if (reader) void reader.cancel().catch(() => {});
            if (audioMediaSource?.readyState === 'open') {
                try { audioMediaSource.endOfStream(); } catch (_) { /* stream may already be closing */ }
            }
            audioMediaSource = null;
            audioStreamTask = null;
            if (audioObjectUrl) {
                try { URL.revokeObjectURL(audioObjectUrl); } catch (_) { /* ignore */ }
                artifactObjectUrls.delete(audioObjectUrl);
                audioObjectUrl = null;
            }
            try {
                audio.pause();
                audio.removeAttribute('src');
                audio.load();
            } catch (_) { /* ignore */ }
            audioReadyPromise = null;
        };
        item.resetAudioSource = resetAudioSource;
        const streamAudioWithMediaSource = async () => {
            const generation = ++audioSourceGeneration;
            const isCurrent = () => (
                generation === audioSourceGeneration
                && renderToken === waveformRenderToken
                && item.isConnected
            );
            const mediaSource = new MediaSource();
            const url = URL.createObjectURL(mediaSource);
            audioMediaSource = mediaSource;
            audioObjectUrl = url;
            artifactObjectUrls.add(url);
            audio.preload = 'auto';
            audio.src = url;
            audio.load();

            let started = false;
            let resolveStarted;
            let rejectStarted;
            const startedPromise = new Promise((resolve, reject) => {
                resolveStarted = resolve;
                rejectStarted = reject;
            });
            let reader = null;
            const pump = (async () => {
                try {
                    await waitForMediaSourceOpen(mediaSource, isCurrent);
                    if (!isCurrent()) throw createArtifactAbortError();
                    const sourceBuffer = mediaSource.addSourceBuffer(f.mime_type);
                    const stream = await openRendererArtifactStream(item._artifactId);
                    reader = stream.getReader();
                    audioStreamReader = reader;
                    let receivedChunk = false;
                    while (true) {
                        if (!isCurrent()) throw createArtifactAbortError();
                        const part = await reader.read();
                        if (part.done) break;
                        const chunk = part.value instanceof Uint8Array
                            ? part.value
                            : new Uint8Array(part.value || []);
                        if (chunk.byteLength === 0) continue;
                        await appendMediaSourceChunk(sourceBuffer, chunk, isCurrent);
                        receivedChunk = true;
                        if (!started) {
                            started = true;
                            resolveStarted(audio);
                        }
                    }
                    if (!receivedChunk) throw new Error('Artifact 音频流为空');
                    if (isCurrent() && mediaSource.readyState === 'open') {
                        try { mediaSource.endOfStream(); } catch (_) { /* ignore close race */ }
                    }
                } catch (error) {
                    if (!started) rejectStarted(error);
                    else if (isCurrent() && error?.name !== 'AbortError') {
                        audio._artifactStreamError = error;
                        console.warn('Artifact 音频流中断:', error);
                    }
                } finally {
                    if (audioStreamReader === reader) audioStreamReader = null;
                }
            })();
            audioStreamTask = pump;
            await startedPromise;
            return audio;
        };
        item.ensureAudioReady = async () => {
            if (audio.src) return audio;
            if (!item._artifactId) throw new Error('音频 Artifact 不可用');
            if (!audioReadyPromise) {
                const pending = (async () => {
                    const mimeType = f.mime_type || artifactMime(f.format);
                    const declaredSize = Number(f.size_bytes);
                    // Short MP3 segments are more reliable as one verified
                    // Blob: MediaSource can report a started stream before
                    // Chromium has enough frames to decode/play it, while a
                    // Blob gives Audio and WaveSurfer one stable resource.
                    // Retain MSE only for artifacts too large for the bounded
                    // renderer buffer.
                    const useMediaSource = Number.isSafeInteger(declaredSize)
                        && declaredSize > MAX_BUFFERED_ARTIFACT_BYTES
                        && supportsMediaSourceMime(mimeType);
                    if (useMediaSource) {
                        try {
                            return await streamAudioWithMediaSource();
                        } catch (error) {
                            resetAudioSource();
                            if (declaredSize > MAX_BUFFERED_ARTIFACT_BYTES) {
                                error.code = error.code || 'ARTIFACT_STREAM_UNSUPPORTED';
                                throw error;
                            }
                        }
                    }
                    if (declaredSize > MAX_BUFFERED_ARTIFACT_BYTES) {
                        const error = new Error('当前环境不支持该音频格式的流式播放，且文件超过浏览器有界读取上限');
                        error.code = 'ARTIFACT_STREAM_UNSUPPORTED';
                        throw error;
                    }
                    const bytes = await readArtifactBytes(item._artifactId, MAX_BUFFERED_ARTIFACT_BYTES);
                    if (renderToken !== waveformRenderToken || !item.isConnected) throw createArtifactAbortError('结果页已切换');
                    const url = URL.createObjectURL(new Blob([bytes], { type: mimeType }));
                    audioObjectUrl = url;
                    artifactObjectUrls.add(url);
                    audio.preload = 'auto';
                    audio.src = url;
                    audio.load();
                    return audio;
                })();
                audioReadyPromise = pending.catch(error => {
                    // A transient ticket/network failure must not poison the
                    // item forever; the next play/retry requests fresh bytes.
                    audioReadyPromise = null;
                    throw error;
                });
            }
            return audioReadyPromise;
        };
        audioElements.push(audio);

        const waveformWrap = document.createElement('div');
        waveformWrap.className = 'waveform-wrap';

        const playBtn = document.createElement('button');
        playBtn.className = 'waveform-play-btn';
        playBtn.title = `播放 ${f.filename}`;
        playBtn.dataset.audioName = f.filename;
        playBtn.setAttribute('aria-label', `播放 ${f.filename}`);
        playBtn.innerHTML = '<svg class="icon-play" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg><svg class="icon-pause" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style="display:none"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
        waveformWrap.appendChild(playBtn);
        audio._playButton = playBtn;

        const canvasWrap = document.createElement('div');
        canvasWrap.className = 'waveform-canvas-wrap';
        canvasWrap.tabIndex = 0;
        canvasWrap.setAttribute('role', 'slider');
        canvasWrap.setAttribute('aria-label', `${f.filename} 播放进度`);
        canvasWrap.setAttribute('aria-valuemin', '0');
        canvasWrap.setAttribute('aria-valuemax', '0');
        canvasWrap.setAttribute('aria-valuenow', '0');
        canvasWrap.setAttribute('aria-busy', 'true');

        const retryWaveformButton = document.createElement('button');
        retryWaveformButton.type = 'button';
        retryWaveformButton.className = 'waveform-retry-btn';
        retryWaveformButton.textContent = '重试波形';
        retryWaveformButton.setAttribute('aria-label', `重试加载 ${f.filename} 的波形`);
        retryWaveformButton.hidden = true;
        canvasWrap.appendChild(retryWaveformButton);

        const placeholder = document.createElement('div');
        placeholder.className = 'waveform-placeholder';
        placeholder.setAttribute('aria-hidden', 'true');
        const waveSeed = Array.from(f.filename || '').reduce((sum, char) => sum + char.charCodeAt(0), 0);
        for (let barIndex = 0; barIndex < WAVEFORM_PLACEHOLDER_BARS; barIndex++) {
            const bar = document.createElement('span');
            const height = 22 + ((waveSeed + barIndex * 29 + (barIndex % 7) * 13) % 64);
            bar.style.setProperty('--wave-height', `${height}%`);
            placeholder.appendChild(bar);
        }
        canvasWrap.appendChild(placeholder);

        const wsContainer = document.createElement('div');
        wsContainer.className = 'waveform-container';
        wsContainer.setAttribute('aria-hidden', 'true');
        canvasWrap.appendChild(wsContainer);

        const timeLabel = document.createElement('span');
        timeLabel.className = 'waveform-time';
        timeLabel.textContent = '00:00 / 00:00';
        canvasWrap.appendChild(timeLabel);

        waveformWrap.appendChild(canvasWrap);
        item.appendChild(waveformWrap);

        // --- 原文折叠展示，避免长列表出现嵌套滚动 ---
        const textSection = document.createElement('details');
        textSection.className = 'audio-text-section';
        textSection.open = true;

        const textSummary = document.createElement('summary');
        textSummary.textContent = '查看对应原文';
        textSection.appendChild(textSummary);

        const textBody = document.createElement('div');
        textBody.className = 'audio-text-body';
        // 显示完整原文，保留换行
        const fullText = f.text || f.text_preview || '';
        if (fullText) {
            textBody.textContent = fullText;
        } else {
            textBody.textContent = '（无原文数据）';
            textBody.style.opacity = '0.5';
        }
        textSection.appendChild(textBody);
        item.appendChild(textSection);

        itemFragment.appendChild(item);

        const updateNativeTime = () => {
            const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
            const current = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
            timeLabel.textContent = `${formatTime(current)} / ${formatTime(duration)}`;
            canvasWrap.setAttribute('aria-valuemax', String(Math.round(duration)));
            canvasWrap.setAttribute('aria-valuenow', String(Math.round(current)));
            canvasWrap.setAttribute('aria-valuetext', `${formatTime(current)} / ${formatTime(duration)}`);
            canvasWrap.setAttribute('aria-busy', duration > 0 ? 'false' : 'true');
        };

        audio.addEventListener('loadedmetadata', updateNativeTime);
        audio.addEventListener('durationchange', updateNativeTime);
        audio.addEventListener('timeupdate', updateNativeTime);
        audio.addEventListener('play', () => {
            if (audio._playRequestToken !== audioPlayRequestToken) {
                audio.pause();
                return;
            }
            currentPlayingAudio = audio;
            item.classList.add('is-playing');
            playBtn.classList.remove('is-buffering');
            updatePlayIcon(playBtn, true);
        });
        audio.addEventListener('pause', () => {
            if (currentPlayingAudio === audio) currentPlayingAudio = null;
            item.classList.remove('is-playing');
            playBtn.classList.remove('is-buffering');
            updatePlayIcon(playBtn, false);
        });
        audio.addEventListener('ended', () => {
            if (currentPlayingAudio === audio) currentPlayingAudio = null;
            item.classList.remove('is-playing');
            playBtn.classList.remove('is-buffering');
            updatePlayIcon(playBtn, false);
            try { audio.currentTime = 0; } catch (_) { /* ignore */ }
            updateNativeTime();
        });
        audio.addEventListener('waiting', () => {
            if (!audio.paused) playBtn.classList.add('is-buffering');
        });
        audio.addEventListener('playing', () => playBtn.classList.remove('is-buffering'));
        audio.addEventListener('canplay', () => playBtn.classList.remove('is-buffering'));

        let waveSurfer = null;
        let waveLoadRelease = null;
        let waveLoadTimeout = null;
        item._waveformInitialized = false;
        item._waveformFailed = false;

        const finishWaveformLoad = () => {
            if (waveLoadTimeout) {
                clearTimeout(waveLoadTimeout);
                waveLoadTimeout = null;
            }
            const release = waveLoadRelease;
            waveLoadRelease = null;
            if (typeof release === 'function') release();
        };

        item.cancelWaveformLoad = (markFailed = true) => {
            if (markFailed) {
                item._waveformFailed = true;
                canvasWrap.classList.remove('is-wave-ready');
                canvasWrap.classList.add('is-wave-error');
                retryWaveformButton.hidden = false;
            }
            const activeWaveSurfer = waveSurfer;
            waveSurfer = null;
            if (activeWaveSurfer) {
                const instanceIndex = wavesurferInstances.indexOf(activeWaveSurfer);
                if (instanceIndex >= 0) wavesurferInstances.splice(instanceIndex, 1);
                try {
                    activeWaveSurfer.setMediaElement(new Audio());
                    activeWaveSurfer.destroy();
                } catch (_) { /* ignore */ }
            }
            item._waveformInitialized = false;
            finishWaveformLoad();
        };

        item.initializeWaveform = (onSettled = () => {}) => {
            if (item._waveformInitialized || item._waveformFailed) return waveSurfer;
            if (renderToken !== waveformRenderToken || !item.isConnected || canvasWrap.getBoundingClientRect().width <= 1) return null;
            item._waveformInitialized = true;
            waveLoadRelease = onSettled;
            waveLoadTimeout = setTimeout(() => item.cancelWaveformLoad(true), 30000);
            waveformObserver?.unobserve(item);
            void item.ensureAudioReady().then(() => {
                if (renderToken !== waveformRenderToken || !item.isConnected || !item._waveformInitialized) return;
                waveSurfer = createWaveSurfer(
                    wsContainer,
                    audio,
                    color,
                    canvasWrap,
                    readyWs => {
                        retryWaveformButton.hidden = true;
                        const duration = readyWs?.getDuration?.() || 0;
                        if (duration > 0) {
                            if (!Number.isFinite(audio.duration) || audio.duration <= 0) {
                                timeLabel.textContent = `00:00 / ${formatTime(duration)}`;
                            }
                            canvasWrap.setAttribute('aria-valuemax', String(Math.round(duration)));
                            canvasWrap.setAttribute('aria-busy', 'false');
                        }
                        finishWaveformLoad();
                    },
                    () => {
                        item._waveformFailed = true;
                        item._waveformInitialized = false;
                        waveSurfer = null;
                        retryWaveformButton.hidden = false;
                        finishWaveformLoad();
                    },
                );
                if (!waveSurfer) finishWaveformLoad();
            }).catch(() => {
                if (renderToken !== waveformRenderToken) return;
                item._waveformFailed = true;
                item._waveformInitialized = false;
                retryWaveformButton.hidden = false;
                finishWaveformLoad();
            });
            return item;
        };

        retryWaveformButton.addEventListener('click', event => {
            event.stopPropagation();
            if (renderToken !== waveformRenderToken || !item.isConnected) return;
            item._waveformFailed = false;
            item._waveformInitialized = false;
            canvasWrap.classList.remove('is-wave-error');
            retryWaveformButton.hidden = true;
            queueWaveformInitialization(item, true);
        });

        audio.addEventListener('error', () => {
            // A successful ticket read can still produce an unsupported or
            // truncated media resource.  Do not let the broken Blob URL make
            // every later click reuse the same failed source; the next click
            // must obtain a fresh Artifact ticket and bytes.
            resetAudioSource();
            item.cancelWaveformLoad(true);
            if (currentPlayingAudio === audio) currentPlayingAudio = null;
            playBtn.classList.remove('is-buffering');
            updatePlayIcon(playBtn, false);
        });

        playBtn.addEventListener('click', async () => {
            queueWaveformInitialization(item, true);
            const requestId = ++audioPlayRequestToken;
            const shouldPause = !audio.paused || currentPlayingAudio === audio;
            audio._playRequestToken = requestId;
            if (shouldPause) {
                audio.pause();
                playBtn.classList.remove('is-buffering');
                if (currentPlayingAudio === audio) currentPlayingAudio = null;
                return;
            }
            audioElements.forEach(otherAudio => {
                if (otherAudio === audio) return;
                otherAudio._playRequestToken = 0;
                otherAudio.pause();
                if (otherAudio._playButton) {
                    otherAudio._playButton.classList.remove('is-buffering');
                    updatePlayIcon(otherAudio._playButton, false);
                }
            });
            currentPlayingAudio = audio;
            playBtn.classList.add('is-buffering');
            try {
                await item.ensureAudioReady();
                if (requestId !== audioPlayRequestToken || renderToken !== waveformRenderToken) return;
                await waitForNativeAudioReady(audio);
                if (requestId !== audioPlayRequestToken || renderToken !== waveformRenderToken) return;
                await audio.play();
            } catch (error) {
                if (requestId !== audioPlayRequestToken) return;
                if (currentPlayingAudio === audio) currentPlayingAudio = null;
                playBtn.classList.remove('is-buffering');
                updatePlayIcon(playBtn, false);
                if (error?.name === 'AbortError') return;
                console.error('音频播放失败:', error);
                if (error?.code === 'ARTIFACT_STREAM_UNSUPPORTED' || error?.code === 'ARTIFACT_TOO_LARGE_FOR_BUFFER') {
                    showToast('当前环境无法播放这个大音频，请使用 Electron 桌面版下载或播放', 'warning');
                } else {
                    showToast('音频暂时无法播放，请稍后重试');
                }
            }
        });

        canvasWrap.addEventListener('keydown', async event => {
            if (event.key === ' ' || event.key === 'Enter') {
                event.preventDefault();
                playBtn.click();
                return;
            }
            const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
            if (!duration) return;
            if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
                event.preventDefault();
                const delta = event.key === 'ArrowLeft' ? -5 : 5;
                audio.currentTime = Math.min(duration, Math.max(0, audio.currentTime + delta));
                updateNativeTime();
            } else if (event.key === 'Home' || event.key === 'End') {
                event.preventDefault();
                audio.currentTime = event.key === 'Home' ? 0 : duration;
                updateNativeTime();
            }
        });

        waveformItems.push(item);
    });
    audioList.appendChild(itemFragment);
    void refreshResultVoiceAssets(resultFiles);
}


registerRendererModule("delivery.resultPage", {
    buildResultPage,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
