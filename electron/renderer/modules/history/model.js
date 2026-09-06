/** Renderer module: history.model */
(function attachRendererFeature_history_model(root) {
    'use strict';

function setHistoryCounts(count) {
    const safeCount = Math.max(0, Math.min(Number(count) || 0, 20));
    if ($('history-nav-count')) $('history-nav-count').textContent = String(safeCount);
    if ($('history-count')) $('history-count').textContent = `${safeCount} / 20`;
}

function historyDateLabel(value) {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '完成时间未知';
    return parsed.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
    });
}

function historyFormatLabel(record) {
    return String(record?.format || '未知').toUpperCase();
}

function historyGenerationModeLabel(record) {
    // 历史清单升级前没有该字段，按原有逐条流程解释，避免把旧任务误标成
    // 新的合并切割模式。
    return record?.generation_mode
        ? generationModeLabel(record.generation_mode)
        : GENERATION_MODE_LABELS[GENERATION_MODE_SINGLE];
}

function nonNegativeCount(value, fallback = 0) {
    if (value === null || value === undefined || value === '') return fallback;
    const number = Number(value);
    return Number.isFinite(number) ? Math.max(0, Math.floor(number)) : fallback;
}

function hasOwnRecordField(source, key) {
    return Boolean(
        source
        && typeof source === 'object'
        && Object.prototype.hasOwnProperty.call(source, key),
    );
}

function historyProgressCounts(progress = {}, record = {}, fileCount = 0) {
    const authoritative = progress && typeof progress === 'object' ? progress : {};
    const fallbackRecord = record && typeof record === 'object' ? record : {};
    const count = (key, fallback = 0) => {
        const fallbackValue = nonNegativeCount(fallbackRecord[key], fallback);
        return hasOwnRecordField(authoritative, key)
            ? nonNegativeCount(authoritative[key], fallbackValue)
            : fallbackValue;
    };
    return {
        completed: count('completed', fileCount),
        total: count('total', fileCount),
        failed: count('failed'),
        cancelled: count('cancelled'),
    };
}

function resultSummaryCounts(context = {}, resultCount = 0, workspaceCounts = {}, deliveryIssueCount = 0) {
    const resultContext = context && typeof context === 'object' ? context : {};
    const authoritativeCounts = workspaceCounts && typeof workspaceCounts === 'object' ? workspaceCounts : {};
    const count = key => Math.max(0, Number(resultContext[key] ?? authoritativeCounts[key]) || 0);
    const reportedCompleted = count('completed');
    const success = Math.max(0, Number(resultCount) || 0);
    const missingFiles = Math.max(0, reportedCompleted - success);
    const failed = count('failed') + missingFiles;
    const cancelled = count('cancelled');
    const deliveryIssues = Math.max(0, Number(deliveryIssueCount) || 0);
    // A missing result file is already represented by the gap between the
    // server's completed count and the files we can actually read. Only
    // additional delivery blockers should increase the unresolved total.
    const unresolved = failed + cancelled + Math.max(0, deliveryIssues - missingFiles);
    return {
        reportedCompleted,
        success,
        missingFiles,
        failed,
        cancelled,
        deliveryIssues,
        unresolved,
    };
}

function historyActiveCandidateState(candidate) {
    if (!candidate?.workspace?.snapshot?.workflow_id) return 'unavailable';
    if (candidate.can_takeover === true) return 'takeover';
    if (candidate.can_resume === true) return 'resume';
    return 'context';
}

function historyActiveActionLabel(record) {
    if (isTerminalWorkflowSnapshot(record)) {
        // The delivery page owns the system-input progress card, so an
        // unsettled input phase is the reason to open a terminal task.
        if (!historyRecordInputSettled(record)) return '查看录入';
        return '查看交付';
    }
    switch (historyActiveCandidateState(record?.active_candidate)) {
        case 'takeover':
            return '继续生成';
        case 'resume':
        case 'context':
            return '恢复上下文';
        default:
            return '查看状态';
    }
}

function historyActiveStatusLabel(candidate) {
    switch (historyActiveCandidateState(candidate)) {
        case 'takeover':
            return '可继续生成';
        case 'resume':
            return '可恢复上下文';
        case 'context':
            return '待处理';
        default:
            return '状态待同步';
    }
}

function activeCandidateHintText(candidates = [], truncated = false) {
    if (!Array.isArray(candidates) || candidates.length === 0) return '';
    const counts = candidates.reduce((result, candidate) => {
        const state = historyActiveCandidateState(candidate);
        result[state] = (result[state] || 0) + 1;
        return result;
    }, {});
    const parts = [];
    if (counts.takeover) parts.push(`${counts.takeover} 个任务可继续生成`);
    if (counts.resume) parts.push(`${counts.resume} 个任务可恢复上下文`);
    if (counts.context) parts.push(`${counts.context} 个任务待处理`);
    if (counts.unavailable) parts.push(`${counts.unavailable} 个任务状态待同步`);
    const suffix = truncated ? '（列表已截断）' : '';
    return `${parts.join('，') || `${candidates.length} 个任务状态待同步`}${suffix}`;
}

function historyStatusPresentation(record) {
    const executionState = String(record?.execution_state || '');
    const controlState = String(record?.control_state || '');
    const resultStatus = String(record?.result_status || '');
    const requiresReconcile = Boolean(record?.active_candidate?.requires_reconcile)
        || executionState === 'WAITING_USER'
        || resultStatus === 'AMBIGUOUS';
    if (requiresReconcile) return { label: '待处理', className: 'is-partial' };
    if (executionState === 'TERMINAL') {
        if (resultStatus === 'SUCCEEDED') {
            const completed = nonNegativeCount(record?.completed, nonNegativeCount(record?.available_files));
            const total = nonNegativeCount(record?.total);
            const cancelled = nonNegativeCount(record?.cancelled);
            const skipped = nonNegativeCount(record?.skipped);
            const failed = nonNegativeCount(record?.failed);
            // 已取消和已跳过的条目不欠交付；只有既没有产物也没有终态豁免的
            // 条目才说明服务端的完成事实还没有同步到可交付文件。
            const unresolved = Math.max(0, total - completed - failed - cancelled - skipped);
            if (unresolved > 0) return { label: '交付待同步', className: 'is-partial' };
            return { label: '已完成', className: '' };
        }
        if (resultStatus === 'PARTIAL_SUCCESS') return { label: '部分完成', className: 'is-partial' };
        if (resultStatus === 'CANCELLED') return { label: '已取消', className: 'is-partial' };
        return { label: '生成失败', className: 'is-danger' };
    }
    if (controlState === 'PAUSED' || controlState === 'PAUSE_REQUESTED') {
        return { label: '已暂停', className: 'is-active' };
    }
    if (executionState === 'WAITING_RETRY') return { label: '等待重试', className: 'is-active' };
    if (executionState === 'RECOVERING') return { label: '恢复中', className: 'is-active' };
    if (executionState === 'CREATED' || String(record?.status || '') === 'DRAFT') {
        return { label: '待配置', className: 'is-active' };
    }
    return { label: '生成中', className: 'is-active' };
}

function historyRecordDeliveryMode(record) {
    // Older history rows predate the system-input fields; they can only have
    // been audio-only tasks, so the absence of the field is meaningful.
    return record?.delivery_mode === 'audio_and_input' ? 'audio_and_input' : 'audio_only';
}

function historyRecordInputStatus(record) {
    if (historyRecordDeliveryMode(record) !== 'audio_and_input') return 'not_enabled';
    const status = String(record?.input_status || '').trim().toLowerCase();
    return status || 'pending_config';
}

function historyInputStatusPresentation(record) {
    const status = historyRecordInputStatus(record);
    // 音频未结束前，待配置/待录入只是后续步骤，用进行中色调；音频结束后
    // 它们成为剩下的待办，与“需要处理”筛选桶共用警示色调。
    const audioTerminal = isTerminalWorkflowSnapshot(record);
    const waiting = audioTerminal
        ? { className: 'is-input is-partial', settled: false, attention: true }
        : { className: 'is-input is-active', settled: false, attention: false };
    switch (status) {
        case 'succeeded':
            return { label: '录入完成', className: 'is-input is-done', settled: true, attention: false };
        case 'running':
            return { label: '录入中', className: 'is-input is-active', settled: false, attention: false };
        case 'needs_reconcile':
            return { label: '录入待核验', className: 'is-input is-partial', settled: false, attention: true };
        case 'failed_retryable':
            return { label: '录入需重试', className: 'is-input is-partial', settled: false, attention: true };
        case 'failed':
            return { label: '录入失败', className: 'is-input is-danger', settled: false, attention: true };
        case 'pending_execute':
            return { label: '待录入', ...waiting };
        case 'pending_config':
            return { label: '录入待配置', ...waiting };
        default:
            // not_enabled 是旧记录的常态，不产出徽标文案，也不参与历史搜索，
            // 避免“录入”关键词命中所有仅音频任务。其余无法识别的状态按待核验
            // 保守处理，不能当成已落定，否则未来新增状态会被误归入“已完成”。
            if (status === 'not_enabled') {
                return { label: '', className: '', settled: true, attention: false };
            }
            return { label: '录入待核验', className: 'is-input is-partial', settled: false, attention: true };
    }
}

function historyInputUnitsProgress(record) {
    if (historyRecordDeliveryMode(record) !== 'audio_and_input') return null;
    const total = nonNegativeCount(record?.input_units_total);
    const succeeded = Math.min(nonNegativeCount(record?.input_units_succeeded), total || nonNegativeCount(record?.input_units_succeeded));
    if (total <= 0) return null;
    return { total, succeeded };
}

function historyDeliveryTagLabel(record) {
    return historyRecordDeliveryMode(record) === 'audio_and_input' ? '音频 + 录入' : '仅音频';
}

function historyRecordInputAttention(record) {
    return historyInputStatusPresentation(record).attention === true;
}

function historyRecordInputSettled(record) {
    return historyInputStatusPresentation(record).settled === true;
}

function historyRecordMatchesFilter(record) {
    const query = String(historyFilters.query || '').trim();
    if (query) {
        const searchable = [
            record?.source_filename,
            record?.format,
            record?.result_status,
            record?.execution_state,
            historyStatusPresentation(record).label,
            historyDeliveryTagLabel(record),
            historyInputStatusPresentation(record).label,
        ].filter(Boolean).join(' ').toLocaleLowerCase('zh-CN');
        if (!searchable.includes(query)) return false;
    }
    const presentation = historyStatusPresentation(record);
    const terminal = isTerminalWorkflowSnapshot(record);
    const attention = presentation.className.includes('is-danger')
        || presentation.className.includes('is-partial')
        || historyRecordInputAttention(record);
    switch (historyFilters.status) {
        case 'input':
            return historyRecordDeliveryMode(record) === 'audio_and_input';
        case 'active':
            return !attention && (!terminal || !historyRecordInputSettled(record));
        case 'attention':
            return attention;
        case 'done':
            return terminal && !attention && historyRecordInputSettled(record);
        default:
            return true;
    }
}

function historyRecordTimestamp(record) {
    const key = historyFilters.sort === 'created' ? 'created_at' : 'updated_at';
    const value = Date.parse(String(record?.[key] || record?.updated_at || record?.created_at || ''));
    return Number.isFinite(value) ? value : 0;
}

function itemDisplayFacts(item) {
    const metadata = item?.metadata && typeof item.metadata === 'object' && !Array.isArray(item.metadata)
        ? item.metadata
        : {};
    const docType = String(
        metadata.doc_type
        || item?.doc_type
        || item?.item_type
        || '',
    ).trim();
    let category = String(metadata.category || item?.category || '').trim();
    // Older rows only have item_type. Use it as the document label once, not
    // as both “document type” and “category”, which produced duplicate labels
    // such as “模仿朗读-框内英文 · 模仿朗读-框内英文”.
    if (!docType && item?.item_type) category = String(item.item_type).trim();
    if (category === docType) category = '';
    return {
        docType: docType || '音频',
        category,
    };
}

function resultHasUsableAcceptedVoiceConfiguration(workspace = null, item = null) {
    const configuration = workspace?.configuration?.effective;
    if (!configuration || typeof configuration !== 'object' || Array.isArray(configuration)) return false;
    // A workspace can briefly expose an empty `effective` object while it is
    // being hydrated.  Treating that placeholder as an accepted snapshot
    // hides the real voice_keys stored on legacy artifacts and makes the
    // delivery page invent the female fallback.  The normalized server
    // projection always carries both default fields, even when an old record
    // or a hydration placeholder has both values set to null. Field presence
    // alone is therefore not evidence that a voice was accepted; require an
    // actual non-empty voice key. Role-only configs are also meaningful for
    // explicitly role-labelled items.
    const hasDefaultVoice = [
        configuration.default_female_voice,
        configuration.default_male_voice,
    ].some(value => String(value ?? '').trim());
    const roleVoices = configuration.role_voices;
    const role = normalizeRoleKeyClient(item?.role);
    const hasRoleVoiceForItem = role && roleVoices
        && typeof roleVoices === 'object'
        && !Array.isArray(roleVoices)
        && Boolean(roleVoices[role] || roleVoices[`role:${role}`]);
    return hasDefaultVoice || Boolean(hasRoleVoiceForItem);
}

function resultVoiceKeyFromAcceptedConfiguration(item, workspace = null) {
    const configuration = workspace?.configuration?.effective;
    if (!configuration || typeof configuration !== 'object' || Array.isArray(configuration)) return '';
    // An empty effective object is a hydration placeholder, not a frozen
    // generation decision.  Returning the built-in Amanda/George fallback
    // here would make old Artifact voice facts look like a new accepted
    // configuration and can overwrite the actual voice used by that record.
    if (!resultHasUsableAcceptedVoiceConfiguration(workspace, item)) return '';
    const explicit = String(item?.voice_key || '').trim();
    if (explicit) return explicit;

    const role = normalizeRoleKeyClient(item?.role);
    const roleVoices = configuration.role_voices && typeof configuration.role_voices === 'object'
        && !Array.isArray(configuration.role_voices)
        ? configuration.role_voices
        : {};
    const roleVoice = role && (
        roleVoices[role]
        || roleVoices[`role:${role}`]
    );
    if (roleVoice) return String(roleVoice).trim();

    const metadata = item?.metadata && typeof item.metadata === 'object' && !Array.isArray(item.metadata)
        ? item.metadata
        : {};
    const itemGender = [
        item?.voice,
        item?.voice_gender,
        item?.gender,
        metadata.voice,
        metadata.voice_gender,
        metadata.gender,
    ].map(reviewGenderFromValue).find(Boolean);
    if (itemGender === 'male') {
        return String(configuration.default_male_voice || 'george').trim();
    }
    if (itemGender === 'female') {
        return String(configuration.default_female_voice || 'amanda').trim();
    }

    // Keep this fallback aligned with WorkflowEngine._effective_plan_item for
    // older workspaces whose durable provider plan predates the voice
    // projection. It is only enabled when the accepted workspace contains its
    // own frozen configuration, never from the renderer's current settings.
    const roleText = String(item?.role || '').trim();
    const maleRole = /^(mr|mr\.|sir|男|先生)\b/i.test(roleText);
    return String(
        (maleRole ? configuration.default_male_voice : configuration.default_female_voice)
        || (maleRole ? 'george' : 'amanda'),
    ).trim();
}

function resultVoiceKeysFromAcceptedContent(item, workspace = null) {
    const text = String(item?.normalized_content ?? item?.text ?? item?.content ?? '').trim();
    if (!text) return [];

    const hasAcceptedConfiguration = resultHasUsableAcceptedVoiceConfiguration(workspace, item);
    const configuration = hasAcceptedConfiguration && workspace?.configuration?.effective
        && typeof workspace.configuration.effective === 'object'
        && !Array.isArray(workspace.configuration.effective)
        ? workspace.configuration.effective
        : {};
    const configuredItemVoice = String(item?.voice_key || '').trim()
        || (hasAcceptedConfiguration ? resultVoiceKeyFromAcceptedConfiguration(item, workspace) : '')
        || String(configuration.default_female_voice || '').trim();
    const femaleVoice = String(
        configuration.default_female_voice
        || configuredItemVoice
        || (hasAcceptedConfiguration ? 'amanda' : '')
    ).trim();
    const maleVoice = String(
        configuration.default_male_voice
        || (hasAcceptedConfiguration ? 'george' : '')
    ).trim();
    const defaultVoice = configuredItemVoice || femaleVoice;
    const roleVoices = configuration.role_voices
        && typeof configuration.role_voices === 'object'
        && !Array.isArray(configuration.role_voices)
        ? configuration.role_voices
        : {};
    const roleVoiceMapForResult = {};
    Object.entries(roleVoices).slice(0, 256).forEach(([role, voice]) => {
        const value = String(voice || '').trim();
        if (!value) return;
        const roleKey = normalizeRoleKeyClient(role);
        if (roleKey) roleVoiceMapForResult[roleKey] = value;
        if (roleKey.startsWith('role:')) {
            const bareRoleKey = normalizeRoleKeyClient(roleKey.slice(5));
            if (bareRoleKey) roleVoiceMapForResult[bareRoleKey] = value;
        }
    });
    const roleVoiceFor = role => {
        const roleKey = normalizeRoleKeyClient(role);
        return roleVoiceMapForResult[roleKey]
            || roleVoiceMapForResult[`role:${roleKey}`]
            || '';
    };
    const inferRoleVoiceForResult = role => /^(mr|mr\.|sir|男|先生)\b/i.test(String(role || '').trim())
        ? maleVoice
        : femaleVoice;
    const values = [];
    const append = value => {
        const key = String(value || '').trim();
        if (key && !values.includes(key)) values.push(key);
    };
    const lines = text.split(/\r?\n/);
    const candidateRoleKeys = new Set();
    lines.forEach(line => {
        const value = line.trim();
        if (!value || /^[WwMm]\s*[:：]/.test(value) || /^\([WwMm]\)/.test(value)) return;
        const match = /^([^:：\n]{1,60}?)\s*[:：]\s*(.*)$/.exec(value);
        if (match && roleLooksLikeLabel(match[1])) {
            candidateRoleKeys.add(normalizeRoleKeyClient(match[1]));
        }
    });
    const allowInferredRoles = candidateRoleKeys.size >= 2;
    let activeVoice = defaultVoice || femaleVoice;
    lines.forEach(line => {
        const value = line.trim();
        if (!value) return;

        const speakerMatch = /^([WwMm])\s*[:：]\s*(.*)$/.exec(value)
            || /^\(([WwMm])\)\s*(.*)$/.exec(value);
        if (speakerMatch) {
            activeVoice = speakerMatch[1].toUpperCase() === 'W' ? femaleVoice : maleVoice;
            if (String(speakerMatch[2] || '').trim()) append(activeVoice);
            return;
        }

        const roleMatch = /^([^:：\n]{1,60}?)\s*[:：]\s*(.*)$/.exec(value);
        if (roleMatch && roleLooksLikeLabel(roleMatch[1])) {
            const mappedVoice = roleVoiceFor(roleMatch[1]);
            if (mappedVoice || allowInferredRoles) {
                activeVoice = mappedVoice || inferRoleVoiceForResult(roleMatch[1]);
                if (String(roleMatch[2] || '').trim()) append(activeVoice);
                return;
            }
        }
        append(activeVoice);
    });
    return values;
}

function resultContentHasExplicitVoiceEvidence(item) {
    const text = String(item?.normalized_content ?? item?.text ?? item?.content ?? '').trim();
    if (!text) return false;
    const lines = text.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (lines.some(line => (
        /^[WwMm]\s*[:：]/.test(line)
        || /^\([WwMm]\)\s*/.test(line)
    ))) return true;
    const roleLabels = new Set();
    lines.forEach(line => {
        const match = /^([^:：\n]{1,60}?)\s*[:：]\s*(.*)$/.exec(line);
        if (match && roleLooksLikeLabel(match[1])) {
            roleLabels.add(normalizeRoleKeyClient(match[1]));
        }
    });
    return roleLabels.size >= 2;
}

function resultItemHasAcceptedVoiceFacts(item, workspace = null) {
    if (resultHasUsableAcceptedVoiceConfiguration(workspace, item)) return true;
    if (String(item?.voice_key || '').trim()) return true;
    if (Array.isArray(item?.segments) && item.segments.some(segment => (
        String(segment?.voice_key || '').trim()
        || (Array.isArray(segment?.voice_keys) && segment.voice_keys.length > 0)
    ))) return true;
    const metadata = item?.metadata && typeof item.metadata === 'object' && !Array.isArray(item.metadata)
        ? item.metadata
        : {};
    return [
        item?.voice,
        item?.voice_gender,
        item?.gender,
        metadata.voice,
        metadata.voice_gender,
        metadata.gender,
    ].map(reviewGenderFromValue).some(Boolean)
        || (resultHasUsableAcceptedVoiceConfiguration(workspace, item)
            && resultContentHasExplicitVoiceEvidence(item));
}

function resultVoiceKeysForItem(item, workspace = null) {
    const values = [];
    const append = value => {
        if (Array.isArray(value)) {
            value.forEach(append);
            return;
        }
        const key = String(value ?? '').trim();
        if (key && !values.includes(key)) values.push(key);
    };
    const legacyVoiceKeys = [];
    const appendLegacy = value => {
        if (Array.isArray(value)) {
            value.forEach(appendLegacy);
            return;
        }
        const key = String(value ?? '').trim();
        if (key && !legacyVoiceKeys.includes(key)) legacyVoiceKeys.push(key);
    };
    appendLegacy(item?.voice_keys);
    appendLegacy(item?.metadata?.voice_keys);
    appendLegacy(item?.voice_key);
    const segmentVoiceKeys = [];
    const appendSegment = value => {
        if (Array.isArray(value)) {
            value.forEach(appendSegment);
            return;
        }
        const key = String(value ?? '').trim();
        if (key && !segmentVoiceKeys.includes(key)) segmentVoiceKeys.push(key);
    };
    if (Array.isArray(item?.segments)) {
        item.segments.forEach(segment => {
            appendSegment(segment?.voice_keys);
            appendSegment(segment?.voice_key);
        });
    }
    const contentVoiceKeys = resultVoiceKeysFromAcceptedContent(item, workspace);
    // Explicit W/M/role markers describe the actual audio and are stronger
    // than stale WorkItem/artifact voice arrays from an older attempt.
    if (resultContentHasExplicitVoiceEvidence(item)) {
        // Without a frozen configuration those markers have no reliable
        // mapping to catalog keys. Keep any concrete item fact as a fallback;
        // otherwise resultFilesFromArtifacts will retain the Artifact facts.
        if (contentVoiceKeys.length) return contentVoiceKeys;
        if (legacyVoiceKeys.length) {
            legacyVoiceKeys.forEach(append);
            return values;
        }
        return [];
    }
    // Segment-level metadata is the precise source for callers that already
    // split the item into voice-specific pieces.
    if (segmentVoiceKeys.length) return segmentVoiceKeys;
    // For an unmarked question, the accepted snapshot (including parser
    // gender metadata) determines exactly one default slot. Do not union it
    // with legacy arrays: that was the source of the “one male voice shown as
    // two voices” symptom.
    const acceptedVoiceKey = resultHasUsableAcceptedVoiceConfiguration(workspace, item)
        ? resultVoiceKeyFromAcceptedConfiguration(item, workspace)
        : '';
    if (acceptedVoiceKey) return [acceptedVoiceKey];
    // Preserve old records only when no accepted configuration can resolve
    // the item. This keeps historical data readable without allowing it to
    // override a frozen generation decision.
    if (legacyVoiceKeys.length) {
        legacyVoiceKeys.forEach(append);
        return values;
    }
    return contentVoiceKeys;
}

function resultFilesFromArtifacts(items, artifacts, workspace = null) {
    const hasAuthoritativeWorkspaceArtifacts = Boolean(workspace && Array.isArray(workspace.artifacts));
    // A workspace response is the complete, server-owned projection.  Prefer
    // it over the bounded legacy artifact list so a large task cannot expose
    // an older page of artifacts, and an empty projection cannot be revived by
    // raw rows from a second endpoint.
    const sourceArtifacts = hasAuthoritativeWorkspaceArtifacts
        ? workspace.artifacts
        : (Array.isArray(artifacts) ? artifacts : []);
    const itemById = new Map((Array.isArray(items) ? items : []).map(item => [String(item.item_id), item]));
    const workspaceItems = new Map((Array.isArray(workspace?.items) ? workspace.items : [])
        .map(item => [String(item.item_id), item]));
    const workspaceArtifacts = new Map((Array.isArray(workspace?.artifacts) ? workspace.artifacts : [])
        .map(artifact => [String(artifact.artifact_id), artifact]));
    const rawArtifactsById = new Map((Array.isArray(artifacts) ? artifacts : [])
        .map(artifact => [String(artifact.artifact_id), artifact]));
    const latestByItem = new Map();
    const seenItemIds = new Set();

    // A READY Artifact is deliverable only when the authoritative item state
    // is SUCCEEDED and its server-owned MP3 filename/format/MIME metadata
    // passes validation. Never synthesize a filename or default a missing
    // format to MP3: doing so turns stale/conflicting facts into a false
    // download.
    sourceArtifacts
        .slice()
        .sort((left, right) => (
            // Workspace normally carries created_at. Keep the raw list as a
            // sort-only fallback for older clients/fixtures that omitted that
            // non-sensitive field; it never supplies delivery metadata.
            String(right.created_at || rawArtifactsById.get(String(right.artifact_id))?.created_at || '')
                .localeCompare(String(left.created_at || rawArtifactsById.get(String(left.artifact_id))?.created_at || ''))
            || String(right.artifact_id || '').localeCompare(String(left.artifact_id || ''))
        ))
        .forEach(artifact => {
            const itemId = String(artifact?.item_id || '');
            if (artifact.artifact_type !== 'tts-segment') return;
            if (!itemId || seenItemIds.has(itemId)) return;
            // The newest TTS artifact is authoritative for this item. If it is
            // not deliverable, do not fall back to an older attempt's audio.
            seenItemIds.add(itemId);

            const workspaceItem = workspaceItems.get(itemId);
            // Workspace fields win, while sparse legacy test/compatibility
            // rows may still fill fields that the old endpoint did not send.
            // Explicit nulls from the workspace remain authoritative.
            const item = workspaceItem
                ? { ...(itemById.get(itemId) || {}), ...workspaceItem }
                : (itemById.get(itemId) || {});
            const hasWorkspaceMetadata = hasAuthoritativeWorkspaceArtifacts
                || workspaceArtifacts.has(String(artifact.artifact_id));
            const metadata = hasWorkspaceMetadata
                ? workspaceArtifacts.get(String(artifact.artifact_id))
                : artifact;
            const itemStatus = String(workspaceItem?.status || item.status || '');
            // A present workspace record is authoritative even when it
            // intentionally redacts conflicting format/size/hash facts. Do
            // not revive those facts from the raw Artifact list and turn a
            // metadata conflict into a downloadable result.
            const lifecycleState = String(metadata?.lifecycle_state || (hasWorkspaceMetadata ? '' : artifact.lifecycle_state) || '');
            const verified = metadata?.verified === true && artifact.verified !== false;
            const format = String(metadata?.format || (hasWorkspaceMetadata ? '' : artifact.format) || '').trim().toLowerCase().replace(/^\./, '');
            const filename = String(metadata?.filename || (hasWorkspaceMetadata ? '' : artifact.filename) || '').trim();
            const mimeType = String(metadata?.mime_type || (hasWorkspaceMetadata ? '' : artifact.mime_type) || '').trim().toLowerCase();
            const extension = filename.includes('.') ? filename.split('.').pop().toLowerCase() : '';
            const sizeBytes = Number(hasWorkspaceMetadata ? metadata?.size_bytes : (metadata?.size_bytes ?? artifact.size_bytes));
            const expectedMime = artifactMime(format);
            const sha256 = String(metadata?.sha256 || (hasWorkspaceMetadata ? '' : artifact.sha256) || '').trim().toLowerCase();
            if (
                itemStatus !== 'SUCCEEDED'
                || lifecycleState !== 'READY'
                || !verified
                || !filename
                || filename.includes('/')
                || filename.includes('\\')
                || /[\x00-\x1f\x7f]/.test(filename)
                || format !== 'mp3'
                || !mimeType
                || extension !== 'mp3'
                || expectedMime !== 'audio/mpeg'
                || mimeType !== 'audio/mpeg'
                || !Number.isSafeInteger(sizeBytes)
                || sizeBytes <= 0
                || !/^[0-9a-f]{64}$/.test(sha256)
            ) return;

            const displayFacts = itemDisplayFacts(item);
            const text = String(item.normalized_content ?? '');
            const artifactVoiceKeys = Array.isArray(artifact?.voice_keys)
                ? artifact.voice_keys
                : (artifact?.voice_keys ? [artifact.voice_keys] : []);
            const acceptedVoiceKeys = resultVoiceKeysForItem(item, workspace);
            // Accepted item/content metadata is authoritative. Artifact voice
            // arrays are only a compatibility fallback for old records where
            // the item has no resolvable voice fact; unioning both revives a
            // stale default as a second voice in the delivery page.
            const useAcceptedVoiceKeys = resultItemHasAcceptedVoiceFacts(item, workspace);
            const voiceKeys = [...new Set(((useAcceptedVoiceKeys && acceptedVoiceKeys.length)
                ? acceptedVoiceKeys
                : artifactVoiceKeys
            ).map(value => String(value || '').trim()).filter(Boolean))];
            const primaryVoiceKey = (useAcceptedVoiceKeys && acceptedVoiceKeys[0])
                || artifactVoiceKeys[0]
                || resultVoiceKeyFromAcceptedConfiguration(item, workspace)
                || acceptedVoiceKeys[0]
                || '';
            const sequenceValue = Number(item.sequence);
            latestByItem.set(itemId, {
                filename,
                artifact_id: String(artifact.artifact_id),
                available: true,
                doc_type: displayFacts.docType,
                category: displayFacts.category,
                item_id: itemId,
                text,
                text_preview: text.slice(0, 160),
                role: item.role ?? null,
                voice_keys: voiceKeys,
                voice_key: primaryVoiceKey || null,
                sequence: Number.isSafeInteger(sequenceValue) && sequenceValue >= 0
                    ? sequenceValue
                    : null,
                size_bytes: sizeBytes,
                format,
                mime_type: mimeType,
                sha256,
                duration_ms: Number.isFinite(Number(metadata.duration_ms)) ? Number(metadata.duration_ms) : null,
            });
        });

    return [...latestByItem.values()].sort((left, right) => {
        const leftSequence = left.sequence ?? Number.MAX_SAFE_INTEGER;
        const rightSequence = right.sequence ?? Number.MAX_SAFE_INTEGER;
        return leftSequence - rightSequence || left.filename.localeCompare(right.filename);
    });
}

function createHistoryAction(label, className, handler) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.textContent = label;
    button.addEventListener('click', handler);
    return button;
}


registerRendererModule("history.model", {
    setHistoryCounts,
    historyDateLabel,
    historyFormatLabel,
    historyGenerationModeLabel,
    nonNegativeCount,
    hasOwnRecordField,
    historyProgressCounts,
    resultSummaryCounts,
    historyActiveCandidateState,
    historyActiveActionLabel,
    historyActiveStatusLabel,
    activeCandidateHintText,
    historyStatusPresentation,
    historyRecordDeliveryMode,
    historyRecordInputStatus,
    historyInputStatusPresentation,
    historyInputUnitsProgress,
    historyDeliveryTagLabel,
    historyRecordInputAttention,
    historyRecordInputSettled,
    historyRecordMatchesFilter,
    historyRecordTimestamp,
    itemDisplayFacts,
    resultHasUsableAcceptedVoiceConfiguration,
    resultVoiceKeyFromAcceptedConfiguration,
    resultVoiceKeysFromAcceptedContent,
    resultContentHasExplicitVoiceEvidence,
    resultItemHasAcceptedVoiceFacts,
    resultVoiceKeysForItem,
    resultFilesFromArtifacts,
    createHistoryAction,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

