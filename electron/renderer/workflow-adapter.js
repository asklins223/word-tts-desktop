/*
 * 小猪wordTTS · workspace adapter
 *
 * Keeps API quirks and presentation fallbacks out of the main renderer file.
 * The adapter never invents a downloadable filename or an enabled command.
 */
(function attachWorkflowAdapter(root) {
    'use strict';

    const viewUtils = root?.WORDTTS_WORKFLOW_VIEW_UTILS
        || (typeof module !== 'undefined' && module.exports
            ? require('./workflow-view-utils')
            : {});
    const reducer = root?.WORDTTS_WORKFLOW_REDUCER || {};
    const normalizeWorkspace = reducer.normalizeWorkspace || (workspace => workspace || {});
    const isReadyAudioArtifact = reducer.isReadyAudioArtifact || (artifact => Boolean(
        artifact
        && artifact.artifact_type === 'tts-segment'
        && artifact.lifecycle_state === 'READY'
        && artifact.verified === true
        && String(artifact.format || '').toLowerCase().replace(/^\./, '') === 'mp3'
        && String(artifact.filename || '').toLowerCase().endsWith('.mp3')
        && String(artifact.mime_type || '').toLowerCase() === 'audio/mpeg'
        && /^[0-9a-f]{64}$/i.test(String(artifact.sha256 || ''))
        && Number.isSafeInteger(Number(artifact.size_bytes))
        && Number(artifact.size_bytes) > 0
    ));

    function safeText(value, fallback = '') {
        const text = String(value ?? '').trim();
        return text || fallback;
    }

    function normalizeWorkspaceForView(workspace, snapshot = null) {
        return normalizeWorkspace(workspace, snapshot);
    }

    function ttsArtifacts(workspace) {
        return (Array.isArray(workspace?.artifacts) ? workspace.artifacts : [])
            .filter(artifact => (
                artifact
                && artifact.item_id
                && artifact.artifact_type === 'tts-segment'
            ));
    }

    function latestTtsArtifacts(workspace) {
        const artifacts = ttsArtifacts(workspace);
        if (typeof reducer.latestTtsArtifacts === 'function') {
            return reducer.latestTtsArtifacts(artifacts);
        }
        const latestByItem = new Map();
        artifacts.forEach(artifact => {
            const itemId = String(artifact.item_id);
            const current = latestByItem.get(itemId);
            if (!current) {
                latestByItem.set(itemId, artifact);
                return;
            }
            const candidateCreatedAt = String(artifact.created_at || '');
            const currentCreatedAt = String(current.created_at || '');
            if (
                candidateCreatedAt.localeCompare(currentCreatedAt) > 0
                || (
                    candidateCreatedAt === currentCreatedAt
                    && String(artifact.artifact_id || '').localeCompare(String(current.artifact_id || '')) > 0
                )
            ) {
                latestByItem.set(itemId, artifact);
            }
        });
        return [...latestByItem.values()];
    }

    function verifiedTtsArtifacts(workspace) {
        return latestTtsArtifacts(workspace).filter(isReadyAudioArtifact);
    }

    function deliverableItemIds(workspace) {
        const itemById = new Map((Array.isArray(workspace?.items) ? workspace.items : [])
            .map(item => [String(item.item_id), item]));
        return [...new Set(verifiedTtsArtifacts(workspace)
            .filter(artifact => itemById.get(String(artifact.item_id))?.status === 'SUCCEEDED')
            .map(artifact => String(artifact.item_id)))];
    }

    function blockerSummary(workspace) {
        const first = typeof viewUtils.firstBlocker === 'function'
            ? viewUtils.firstBlocker(workspace)
            : (Array.isArray(workspace?.blockers) ? workspace.blockers[0] : null);
        return first ? {
            code: safeText(first.code, 'WORKFLOW_BLOCKED'),
            title: safeText(first.title, '任务需要处理'),
            message: safeText(first.message, '请查看任务详情后再继续。'),
            severity: safeText(first.severity, 'ERROR'),
            affectedItemIds: Array.isArray(first.affected_item_ids) ? first.affected_item_ids.map(String) : [],
            requiresReconcile: first.requires_reconcile === true,
        } : null;
    }

    function action(workspace, actionType) {
        const actions = Array.isArray(workspace?.available_actions) ? workspace.available_actions : [];
        return actions.find(item => String(item?.type || '') === String(actionType) && item.enabled === true)
            || actions.find(item => String(item?.type || '') === String(actionType))
            || null;
    }

    function actionEnabled(workspace, actionType) {
        return action(workspace, actionType)?.enabled === true;
    }

    function deliveryScope(workspace) {
        const delivery = workspace?.delivery || {};
        const included = Array.isArray(delivery.included_item_ids) ? delivery.included_item_ids.map(String) : deliverableItemIds(workspace);
        const excluded = Array.isArray(delivery.excluded_item_ids) ? delivery.excluded_item_ids.map(String) : [];
        const reasons = delivery.exclusion_reasons && typeof delivery.exclusion_reasons === 'object'
            ? delivery.exclusion_reasons
            : {};
        return {
            included,
            excluded,
            reasons,
            zipArtifactId: delivery.zip_artifact_id || null,
            zipAvailable: delivery.zip_available === true && Boolean(delivery.zip_artifact_id),
        };
    }

    function exclusionDetails(workspace) {
        const scope = deliveryScope(workspace);
        const itemById = new Map((Array.isArray(workspace?.items) ? workspace.items : [])
            .map(item => [String(item.item_id), item]));
        const labels = {
            ITEM_CANCELLED: '已取消',
            ITEM_FAILED: '生成失败',
            ITEM_SKIPPED: '已跳过',
            REQUIRES_RECONCILE: '待对账',
            ARTIFACT_MISSING_OR_UNVERIFIED: '产物待核验',
            NOT_GENERATED: '尚未生成',
            NOT_SELECTED: '未选择',
            ITEM_ARTIFACT_CONFLICT: '产物状态冲突',
            ARTIFACT_FORMAT_UNSUPPORTED: '格式未验证',
        };
        return scope.excluded.map(itemId => {
            const item = itemById.get(String(itemId)) || {};
            const reason = safeText(scope.reasons?.[itemId], 'NOT_GENERATED').toUpperCase();
            const content = safeText(item.normalized_content || item.text_preview, '正文未随列表加载');
            return {
                itemId: String(itemId),
                sequence: Number.isFinite(Number(item.sequence)) ? Number(item.sequence) + 1 : null,
                reason,
                reasonLabel: labels[reason] || '未纳入',
                status: safeText(item.status, 'UNKNOWN'),
                contentPreview: content.length > 120 ? `${content.slice(0, 120)}…` : content,
                sourceLocator: safeText(item.source_locator, ''),
            };
        });
    }

    function issueMessage(error, fallback = '任务状态暂时无法同步，请重试。') {
        const source = error && typeof error === 'object' ? error : {};
        const code = safeText(source.code || source.error_code).toUpperCase();
        const messages = {
            STATE_CONFLICT: '任务状态刚刚发生变化，已刷新最新状态后再试。',
            EVENT_GAP: '实时记录出现缺口，正在重新同步任务快照。',
            CURSOR_EXPIRED: '实时连接已过期，正在重新同步任务快照。',
            NETWORK_ERROR: '生成服务连接中断，已完成的任务记录仍然保留。',
            PROVIDER_RATE_LIMITED: '讯飞服务当前繁忙，请稍后重试。',
            SUBMISSION_AMBIGUOUS: '外部提交结果待核验，不会自动重复提交。',
            ARTIFACT_MISSING_OR_UNVERIFIED: '音频已标记完成，但产物仍未通过读取核验。',
            ARTIFACT_FORMAT_UNSUPPORTED: '音频产物不是已验证的 MP3，暂不开放交付。',
            CONFIGURATION_CONFLICT: '配置已被其他窗口修改，已刷新最新版本，请确认后再保存。',
            CONFIG_FROZEN: '任务已经开始执行，当前配置已冻结；请创建新任务后修改。',
            ITEM_ALREADY_DELIVERED: '该条目已有已核验产物，不能再编辑。',
            ITEM_CONTENT_TOO_LARGE: '条目正文超过当前工作区允许的读取大小。',
            USER_CANCELLED: '操作已取消，已完成的任务事实仍会保留。',
            COMMAND_TIMEOUT: '操作结果暂时未确认，正在保留当前任务状态，不能重复提交。',
            WORKSPACE_READ_TIMEOUT: '任务工作区读取超时，请稍后重试。',
            TRANSFER_ALREADY_RUNNING: '已有一个文件传输正在进行。',
        };
        return { code: code || 'WORKFLOW_SYNC_ERROR', message: messages[code] || safeText(source.message, fallback), severity: ['SUBMISSION_AMBIGUOUS', 'COMMAND_TIMEOUT', 'USER_CANCELLED'].includes(code) ? 'warning' : 'error' };
    }

    const exports = {
        action,
        actionEnabled,
        blockerSummary,
        deliverableItemIds,
        deliveryScope,
        exclusionDetails,
        issueMessage,
        latestTtsArtifacts,
        normalizeWorkspace: normalizeWorkspaceForView,
        ttsArtifacts,
        verifiedTtsArtifacts,
    };
    if (typeof module !== 'undefined' && module.exports) module.exports = exports;
    if (root) root.WORDTTS_WORKFLOW_ADAPTER = exports;
})(typeof globalThis !== 'undefined' ? globalThis : null);
