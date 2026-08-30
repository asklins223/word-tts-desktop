const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function loadRendererConfigFunctions() {
    const sourcePath = path.join(__dirname, '..', 'renderer', 'app.js');
    const source = fs.readFileSync(sourcePath, 'utf8').replace(/\ninit\(\);\s*$/, '\n');
    const storage = new Map();
    const mediaState = { prefersDark: false };
    const document = {
        documentElement: { dataset: {}, style: {} },
        getElementById: () => null,
    };
    const context = {
        console,
        document,
        window: {
            electronAPI: undefined,
            matchMedia: () => ({ matches: mediaState.prefersDark }),
        },
        localStorage: {
            getItem: key => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, String(value)),
        },
        navigator: {},
        setTimeout,
        clearTimeout,
        URL,
        Blob,
        FormData,
        AbortController,
        ReadableStream,
        Map,
        Set,
    };
    vm.createContext(context);
    vm.runInContext(`${source}\nglobalThis.__rendererTests = { clampParamValue, normalizeClientConfig, normalizePersistedConfig, buildWorkflowConfiguration, saveCurrentConfig, integerProgressCount, visualProgressPercent, terminalProgressPercent, generationStatePresentation, generationRecoveryPresentation, generationProgressPercentForView, generationProgressCopy, generationProgressAriaText, resultVoiceKeysForFile, resultFilesFromArtifacts, resultZipState, historyStatusPresentation, historyActiveCandidateState, historyActiveActionLabel, historyActiveStatusLabel, activeCandidateHintText, readBoundedSourceFile, nonNegativeCount, historyProgressCounts, resultSummaryCounts, setVoiceCatalog, getVoiceFilterOptions, migrateVoiceSelections, canonicalVoiceKey, getResultVoiceEntry, voiceAssetCacheReady, workflowSnapshotIsOlder, workflowSnapshotBelongsToSession, mergeWorkflowSnapshotIntoSession, isTerminalWorkflowSnapshot, isHardStoppedWorkflowSnapshot, isAcceptedGenerationSnapshot, isCancellationSettledSnapshot, shouldAdoptResumedGeneration, generationWorkspaceNavigationAllowed, generationWorkflowOwnsRuntimeView, reviewTypePathForItem, reviewVoicePresentation, normalizeUpdateState, updateStatusPresentation, formatUpdateBytes };`, context);
    vm.runInContext('globalThis.__rendererTests.initializeTheme = initializeTheme; globalThis.__rendererTests.setWorkspaceTheme = setWorkspaceTheme;', context);
    return { api: context.__rendererTests, storage, document, mediaState };
}

test('长期配置只保留默认男女声的独立参数，不保存文档角色', () => {
    const { api } = loadRendererConfigFunctions();
    const persisted = api.normalizePersistedConfig({
        default_female_voice: 'speaker:shared',
        default_male_voice: 'speaker:shared',
        role_configs: {
            __default_female__: { rate: 10, volume: 20, pitch: 30 },
            __default_male__: { rate: 40, volume: 50, pitch: 60 },
            'role:reporter': { rate: 70, volume: 80, pitch: 90 },
        },
        role_voices: { reporter: 'speaker:reporter' },
        voice_configs: {
            'speaker:shared': { rate: 99, volume: 99, pitch: 99 },
            'speaker:reporter': { rate: 1, volume: 1, pitch: 1 },
        },
    });

    assert.deepEqual(JSON.parse(JSON.stringify(persisted.role_configs)), {
        __default_female__: { rate: 10, volume: 20, pitch: 30 },
        __default_male__: { rate: 40, volume: 50, pitch: 60 },
    });
    assert.equal(persisted.generation_mode, 'composite_cut');
    assert.equal('role_voices' in persisted, false);
    assert.equal('voice_configs' in persisted, false);
});

test('生成方式预设支持默认合并模式和原有单条模式', () => {
    const { api } = loadRendererConfigFunctions();

    assert.equal(
        api.normalizePersistedConfig({ generation_mode: 'single_segment' }).generation_mode,
        'single_segment',
    );
    assert.equal(
        api.normalizePersistedConfig({ generation_mode: 'unsupported' }).generation_mode,
        'composite_cut',
    );
});

test('男女默认音色相同时仍保持各自的默认语速', () => {
    const { api } = loadRendererConfigFunctions();
    const normalized = api.normalizeClientConfig({
        default_female_voice: 'speaker:shared',
        default_male_voice: 'speaker:shared',
        voice_configs: { 'speaker:shared': { volume: 60 } },
    });

    assert.deepEqual(JSON.parse(JSON.stringify(normalized.role_configs)), {
        __default_female__: { rate: 50, volume: 60, pitch: 50 },
        __default_male__: { rate: 35, volume: 60, pitch: 50 },
    });
});

test('核对页展示解析出的多级题型和默认男女声', () => {
    const { api } = loadRendererConfigFunctions();

    assert.deepEqual(
        JSON.parse(JSON.stringify(api.reviewTypePathForItem({
            doc_type: '课文跟读',
            item_type: '句子跟读',
        }))),
        ['课文跟读', '句子跟读'],
    );
    assert.deepEqual(
        JSON.parse(JSON.stringify(api.reviewTypePathForItem({
            metadata: { type_path: ['信息获取', '听选信息'] },
            item_type: '听选信息题目',
        }))),
        ['信息获取', '听选信息'],
    );
    assert.equal(api.reviewVoicePresentation({ voice: 'female' }).voice, '默认女声');
    assert.equal(api.reviewVoicePresentation({ voice: 'male' }).voice, '默认男声');
});

test('生成前会把当前文档和讯飞配置写入工作流快照', () => {
    const { api } = loadRendererConfigFunctions();
    const configuration = api.buildWorkflowConfiguration({
        generation_mode: 'single_segment',
        default_female_voice: 'speaker:linda',
        default_male_voice: 'speaker:steve',
        role_configs: {
            __default_female__: { rate: 62, pitch: 48, volume: 55 },
            __default_male__: { rate: 31, pitch: 52, volume: 49 },
        },
        role_voices: { teacher: 'speaker:teacher' },
    }, 'lesson.docx', 'xunfei-main');

    assert.equal(configuration.source_filename, 'lesson.docx');
    assert.equal(configuration.provider, 'xunfei');
    assert.equal(configuration.account_scope, 'xunfei-main');
    assert.equal(configuration.default_female_voice, 'speaker:linda');
    assert.equal(configuration.default_male_voice, 'speaker:steve');
    assert.equal(configuration.role_voices.teacher, 'speaker:teacher');
});

test('多人配音基础目录会迁移旧 flat 音色 key', () => {
    const { api } = loadRendererConfigFunctions();
    api.setVoiceCatalog(
        [{ key: 'common:100', name: '欣畅' }],
        [],
        { 'speaker:591199169': 'common:100' },
    );

    assert.equal(api.canonicalVoiceKey('speaker:591199169'), 'common:100');
    assert.equal(
        api.normalizeClientConfig({ default_female_voice: 'speaker:591199169' }).default_female_voice,
        'common:100',
    );
});

test('音色分类把英语和多语种放在最近使用之前', () => {
    const { api } = loadRendererConfigFunctions();
    api.setVoiceCatalog([], [
        { key: 'female', label: '女声', count: 1 },
        { key: 'tag:多语种', label: '多语种', count: 1 },
        { key: 'tag:英语', label: '英语', count: 1 },
        { key: 'male', label: '男声', count: 1 },
    ]);

    assert.deepEqual(
        JSON.parse(JSON.stringify(api.getVoiceFilterOptions().map(filter => filter.label))),
        ['全部音色', '英语', '多语种', '最近使用', '女声', '男声'],
    );
});

test('前端音色参数对非有限数字与后端保持一致', () => {
    const { api } = loadRendererConfigFunctions();

    assert.equal(api.clampParamValue(Infinity), 50);
    assert.equal(api.clampParamValue(-Infinity), 50);
    assert.equal(api.clampParamValue('not-a-number'), 50);
});

test('首次主题默认跟随系统，只有用户选择才持久化', () => {
    const { api, storage, document, mediaState } = loadRendererConfigFunctions();
    mediaState.prefersDark = true;
    api.initializeTheme();

    assert.equal(document.documentElement.dataset.theme, 'dark');
    assert.equal(storage.has('wordtts_theme_preference'), false);

    api.setWorkspaceTheme('light');
    assert.equal(storage.get('wordtts_theme_preference'), 'light');
});

test('工作流快照同步只推进状态版本，不会被旧 SSE 快照回退', () => {
    const { api } = loadRendererConfigFunctions();
    const session = { session_id: 'workflow-1', state_version: 2 };

    api.mergeWorkflowSnapshotIntoSession({
        workflow_id: 'workflow-1',
        state_version: 3,
        execution_state: 'WAITING_RETRY',
        latest_event_id: 'event-3',
    }, session);
    assert.equal(session.state_version, 3);
    assert.equal(session.execution_state, 'WAITING_RETRY');

    api.mergeWorkflowSnapshotIntoSession({
        workflow_id: 'workflow-1',
        state_version: 2,
        execution_state: 'RUNNING',
        latest_event_id: 'event-old',
    }, session);
    assert.equal(session.state_version, 3);
    assert.equal(session.execution_state, 'WAITING_RETRY');
    assert.equal(session.latest_event_id, 'event-3');
});

test('工作流快照同步也不会让事件 seq 回退', () => {
    const { api } = loadRendererConfigFunctions();
    const session = { session_id: 'workflow-1', state_version: 3, latest_seq: 8, latest_event_id: 'event-8' };

    api.mergeWorkflowSnapshotIntoSession({
        state_version: 3,
        latest_seq: 7,
        latest_event_id: 'event-7',
        execution_state: 'RUNNING',
    }, session);
    assert.equal(session.latest_seq, 8);
    assert.equal(session.latest_event_id, 'event-8');
});

test('渲染层能识别比当前会话更旧的原始快照', () => {
    const { api } = loadRendererConfigFunctions();
    assert.equal(api.workflowSnapshotIsOlder(
        { workflow_id: 'workflow-1', state_version: 2, latest_seq: 4 },
        { session_id: 'workflow-1', state_version: 3, latest_seq: 5 },
    ), true);
    assert.equal(api.workflowSnapshotIsOlder(
        { workflow_id: 'workflow-1', state_version: 3, latest_seq: 5 },
        { session_id: 'workflow-1', state_version: 3, latest_seq: 5 },
    ), false);
});

test('异工作流快照不会污染当前会话状态', () => {
    const { api } = loadRendererConfigFunctions();
    const session = {
        session_id: 'workflow-1',
        state_version: 3,
        latest_seq: 5,
        control_state: 'PAUSED',
    };

    assert.equal(api.workflowSnapshotBelongsToSession(
        { workflow_id: 'workflow-2', state_version: 99, control_state: 'RUNNING' },
        session,
    ), false);
    api.mergeWorkflowSnapshotIntoSession({
        workflow_id: 'workflow-2',
        state_version: 99,
        latest_seq: 99,
        control_state: 'RUNNING',
    }, session);
    assert.deepEqual(JSON.parse(JSON.stringify(session)), {
        session_id: 'workflow-1',
        state_version: 3,
        latest_seq: 5,
        control_state: 'PAUSED',
    });
});

test('已接受的工作流不能再次当成可编辑草稿提交', () => {
    const { api } = loadRendererConfigFunctions();
    assert.equal(api.isAcceptedGenerationSnapshot({
        execution_state: 'RUNNING',
        control_state: 'RUNNING',
        result_status: 'IN_PROGRESS',
    }), true);
    assert.equal(api.isAcceptedGenerationSnapshot({
        execution_state: 'BLOCKED',
        control_state: 'TERMINATING',
        result_status: 'IN_PROGRESS',
    }), true);
    assert.equal(api.isTerminalWorkflowSnapshot({
        execution_state: 'TERMINAL',
        control_state: 'TERMINATED',
        result_status: 'CANCELLED',
    }), true);
});

test('配置声音时不能提前进入生成任务步骤', () => {
    const { api } = loadRendererConfigFunctions();

    assert.equal(api.generationWorkspaceNavigationAllowed({
        activeWorkspaceName: 'voice',
        hasSession: true,
        generationActive: false,
        generationAccepted: false,
        generationResultState: null,
    }), false);
    assert.equal(api.generationWorkspaceNavigationAllowed({
        activeWorkspaceName: 'voice',
        hasSession: true,
        generationActive: true,
        generationAccepted: false,
        generationResultState: null,
    }), true);
    assert.equal(api.generationWorkspaceNavigationAllowed({
        activeWorkspaceName: 'generation',
        hasSession: true,
        generationActive: false,
        generationAccepted: false,
        generationResultState: null,
    }), true);
});

test('恢复异常工作流时生成页给出持久化错误和安全重试入口', () => {
    const { api } = loadRendererConfigFunctions();
    const workspace = {
        snapshot: {
            workflow_id: 'workflow-1',
            execution_state: 'WAITING_RETRY',
            control_state: 'RUNNING',
            result_status: 'IN_PROGRESS',
            last_error_code: 'TRANSIENT_PROVIDER_ERROR',
            last_error_message: '讯飞页面已关闭，未完成的内容可以重试',
        },
        progress: { total: 3, completed: 1, failed: 2, cancelled: 0, skipped: 0 },
        available_actions: [{ type: 'RETRY', enabled: true }],
    };

    const presentation = api.generationRecoveryPresentation(workspace, { key: 'WAITING_RETRY' }, {
        generationResultState: null,
    });
    assert.equal(presentation.title, '任务已中断，可重试');
    assert.equal(presentation.retryVisible, true);
    assert.match(presentation.message, /讯飞页面已关闭/);
});

test('服务端未授权重试时，恢复异常页不会显示空操作按钮', () => {
    const { api } = loadRendererConfigFunctions();
    const workspace = {
        snapshot: {
            workflow_id: 'workflow-1',
            execution_state: 'WAITING_RETRY',
            control_state: 'RUNNING',
            result_status: 'IN_PROGRESS',
            last_error_code: 'WORKFLOW_BLOCKED',
            last_error_message: '当前任务需要处理',
        },
        progress: { total: 3, completed: 1, failed: 0, cancelled: 0, skipped: 0 },
        available_actions: [{ type: 'RETRY', enabled: false, reason: '没有可安全重试的内容' }],
    };

    const presentation = api.generationRecoveryPresentation(workspace, { key: 'WAITING_RETRY' }, {
        generationResultState: 'error',
        transientMessage: '旧的渲染器错误',
    });
    assert.equal(presentation.retryVisible, false);
});

test('启动阶段的临时错误仍保留可执行的重试入口', () => {
    const { api } = loadRendererConfigFunctions();
    const presentation = api.generationRecoveryPresentation({
        snapshot: {
            workflow_id: 'workflow-1',
            execution_state: 'RUNNING',
            control_state: 'RUNNING',
            result_status: 'IN_PROGRESS',
        },
        progress: { total: 3, completed: 0, failed: 0, cancelled: 0, skipped: 0 },
        available_actions: [],
    }, { key: 'RUNNING' }, {
        generationResultState: 'error',
        transientMessage: '生成服务暂时不可用',
    });
    assert.equal(presentation.retryVisible, true);
});

test('终态失败由结果页处理，不在生成异常面板显示错误的重试入口', () => {
    const { api } = loadRendererConfigFunctions();
    const presentation = api.generationRecoveryPresentation({
        snapshot: {
            workflow_id: 'workflow-1',
            execution_state: 'TERMINAL',
            control_state: 'TERMINATED',
            result_status: 'FAILED',
            last_error_message: '生成失败',
        },
        progress: { total: 3, completed: 0, failed: 3, cancelled: 0, skipped: 0 },
        available_actions: [{ type: 'RETRY', enabled: true }],
    }, { key: 'FAILED', terminal: true });
    assert.equal(presentation.retryVisible, false);
});

test('历史接管暂停任务时沿用暂停态投影并冻结进度', () => {
    const { api } = loadRendererConfigFunctions();
    const presentation = api.generationStatePresentation({ key: 'PAUSED', terminal: false });

    assert.equal(presentation.key, 'PAUSED');
    assert.equal(presentation.visualState, 'paused');
    assert.equal(presentation.indeterminate, false);
    assert.equal(presentation.freezeProgress, true);
});

test('冻结态进度条只反映权威完成数，不被迟到运行统计或失败数推到末尾', () => {
    const { api } = loadRendererConfigFunctions();
    const presentation = api.generationStatePresentation({ key: 'PAUSED', terminal: false });
    const progress = { total: 37, completed: 0, failed: 37, cancelled: 0, skipped: 0 };

    assert.equal(api.generationProgressPercentForView(presentation, progress, 37), 0);
    assert.match(api.generationProgressCopy(presentation, progress, 37), /已暂停 · 0 \/ 37 · 37 条失败/);
});

test('生成进度摘要区分排队、失败、取消和跳过条目', () => {
    const { api } = loadRendererConfigFunctions();
    const presentation = api.generationStatePresentation({ key: 'PAUSED', terminal: false });
    const progress = { total: 10, completed: 3, pending: 2, failed: 1, cancelled: 1, skipped: 1 };

    assert.equal(
        api.generationProgressCopy(presentation, progress, 10),
        '任务已暂停 · 3 / 10 · 2 条待处理 · 1 条失败 · 1 条已取消 · 1 条已跳过',
    );
});

test('用户停止后的终态不会再开放生成步骤', () => {
    const { api } = loadRendererConfigFunctions();
    const snapshot = {
        workflow_id: 'workflow-1',
        execution_state: 'TERMINAL',
        control_state: 'TERMINATED',
        result_status: 'CANCELLED',
        last_error_code: 'WORKFLOW_CANCELLED',
    };

    assert.equal(api.isHardStoppedWorkflowSnapshot(snapshot), true);
    assert.equal(api.generationWorkspaceNavigationAllowed({
        activeWorkspaceName: 'voice',
        hasSession: true,
        generationActive: false,
        generationAccepted: false,
        generationResultState: null,
        generationSnapshot: snapshot,
    }), false);
    assert.equal(api.isHardStoppedWorkflowSnapshot({
        ...snapshot,
        result_status: 'PARTIAL_SUCCESS',
    }), true);
    assert.equal(api.isHardStoppedWorkflowSnapshot({
        ...snapshot,
        result_status: 'PARTIAL_SUCCESS',
        last_error_code: null,
        latest_event: { event_type: 'WORKFLOW_CANCELLED' },
    }), true);
    assert.equal(api.isHardStoppedWorkflowSnapshot({
        ...snapshot,
        result_status: 'PARTIAL_SUCCESS',
        last_error_code: null,
        latest_event: { event_type: 'TTS_OUTPUT_VERIFIED' },
    }), false);
});

test('部分完成但已停止的终态仍冻结生成页，迟到运行事件不能继续驱动进度', () => {
    const { api } = loadRendererConfigFunctions();
    const stoppedPartial = {
        workflow_id: 'workflow-1',
        execution_state: 'TERMINAL',
        control_state: 'TERMINATED',
        result_status: 'PARTIAL_SUCCESS',
        last_error_code: 'WORKFLOW_CANCELLED',
    };
    const ordinaryPartial = { ...stoppedPartial, last_error_code: 'ARTIFACT_MISSING_OR_UNVERIFIED' };

    assert.equal(api.generationWorkflowOwnsRuntimeView(null, stoppedPartial), true);
    assert.equal(api.generationWorkflowOwnsRuntimeView(null, ordinaryPartial), false);
});

test('从历史恢复暂停任务时，恢复后的运行态会重新接管实时进度流', () => {
    const { api } = loadRendererConfigFunctions();
    const running = {
        workflow_id: 'workflow-1',
        execution_state: 'RUNNING',
        control_state: 'RUNNING',
        result_status: 'IN_PROGRESS',
    };

    assert.equal(api.shouldAdoptResumedGeneration(
        'RESUME',
        { snapshot: running },
        null,
        { generationActive: false, startInFlight: false },
    ), true);
    assert.equal(api.shouldAdoptResumedGeneration(
        'RESUME',
        { snapshot: { ...running, control_state: 'PAUSED' } },
        null,
        { generationActive: false, startInFlight: false },
    ), false);
    assert.equal(api.shouldAdoptResumedGeneration(
        'RESUME',
        { snapshot: running },
        null,
        { generationActive: true, startInFlight: false },
    ), false);
});

test('生成页提供停止入口，并把配置冻结竞态收敛到接管流程', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
    const html = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'index.html'), 'utf8');
    assert.match(source, /adoptAcceptedGeneration\(session, authoritative/);
    assert.match(source, /cancelCurrentWorkflow\(session/);
    assert.match(source, /desktop-return-to-configuration/);
    assert.match(source, /adoptResumedGenerationIfNeeded\(type/);
    assert.match(source, /void connectSSE\(session\.session_id\)/);
    assert.match(source, /if \(isAcceptedGenerationSnapshot\(workspace\.snapshot\)\) \{[\s\S]*?goToStep\(3\);[\s\S]*?adoptAcceptedGeneration/);
    assert.match(source, /const presentation = renderGenerationViewState\(\s*currentWorkspace/);
    assert.match(source, /const snapshotForRender = \{/);
    assert.match(source, /renderLiveWorkflowSnapshot\(snapshotForRender, currentSession\)/);
    assert.match(html, /id="cancel-generation-btn"/);
});

test('解析或取消完成后新建任务按钮不会残留禁用状态', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
    assert.match(source, /function syncRestartButtonState\(sourceBusy = null\)/);
    assert.match(source, /const active = Boolean\(parsing \|\| isParsing \|\| sourceImportInFlight\)/);
    assert.match(source, /function resetGenerateState\(\) \{[\s\S]*?syncRestartButtonState\(\);/);

    const processStart = source.indexOf('async function processSourceContent');
    const finalizerStart = source.indexOf('    } finally {', processStart);
    const finalizerEnd = source.indexOf('\n    }\n}', finalizerStart);
    assert.ok(processStart >= 0 && finalizerStart > processStart && finalizerEnd > finalizerStart);

    const finalizer = source.slice(finalizerStart, finalizerEnd);
    const buttonReset = finalizer.indexOf('setUploadParsing(false);');
    assert.ok(buttonReset >= 0);
    assert.ok(finalizer.indexOf('isParsing = false;') < buttonReset);
    assert.ok(finalizer.indexOf('sourceImportInFlight = false;') < buttonReset);
});

test('返回配置会为已终止任务创建新的可编辑工作流', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
    assert.match(source, /async function createEditableWorkflowFromTerminal\(session, snapshot\)/);
    assert.match(source, /renderer-return-config-rerun-/);
    assert.match(source, /renderer-rerun-\$\{session\.session_id\}-\$\{expectedGroupStateVersion\}/);
    assert.match(source, /if \(isTerminalWorkflowSnapshot\(snapshot\)\) \{\s+const nextWorkspace = await createEditableWorkflowFromTerminal\(session, snapshot\)/s);
    assert.match(source, /session = currentSession;\s+snapshot = nextWorkspace\.snapshot;/s);
});

test('生成页默认展开任务时间线，并忽略结束后的迟到进度事件', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
    const html = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'index.html'), 'utf8');

    assert.doesNotMatch(html, /generation-v2-layout is-log-collapsed/);
    assert.doesNotMatch(html, /generation-v2-log is-collapsed/);
    assert.match(html, /id="log-toggle-btn"[^>]*aria-expanded="true"[^>]*>收起详情<\/button>/);
    assert.match(source, /setLogDetailsExpanded\(true\)/);
    assert.match(source, /if \(!isGenerating \|\| generationResult !== null\) return;/);
});

test('生成页状态文案以暂停控制态为准，不会被旧的运行中消息覆盖', () => {
    const { api } = loadRendererConfigFunctions();
    const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
    const html = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'index.html'), 'utf8');

    const paused = api.generationStatePresentation(
        { key: 'PAUSED', label: '已暂停' },
        { runtimeMessage: '讯飞浏览器正在处理，任务仍在运行' },
    );
    assert.equal(paused.key, 'PAUSED');
    assert.equal(paused.badge, '已暂停');
    assert.equal(paused.liveStatus, '任务已暂停，可恢复执行');
    assert.equal(paused.indeterminate, false);
    assert.equal(paused.freezeProgress, true);

    const pendingPause = api.generationStatePresentation(
        { key: 'RUNNING' },
        { pendingPause: true, runtimeMessage: '旧的运行中消息' },
    );
    assert.equal(pendingPause.key, 'PAUSE_REQUESTED');
    assert.equal(pendingPause.badge, '正在暂停');
    assert.equal(pendingPause.liveStatus, '正在暂停，等待当前处理点结束…');

    const pendingResume = api.generationStatePresentation(
        { key: 'PAUSED' },
        { pendingResume: true },
    );
    assert.equal(pendingResume.key, 'RESUME_REQUESTED');
    assert.equal(pendingResume.badge, '正在恢复');
    assert.equal(pendingResume.indeterminate, false);
    const transientFailure = api.generationStatePresentation(
        { key: 'RUNNING', terminal: false },
        { generationResultState: 'error', transientMessage: '生成服务连接中断' },
    );
    assert.equal(transientFailure.key, 'FAILED');
    assert.equal(transientFailure.terminal, true);
    assert.equal(api.generationProgressAriaText(
        { key: 'WAITING_RETRY', terminal: false },
        99,
    ), '99% 等待重试');
    assert.match(source, /generation-v2-pig-status/);
    assert.match(source, /PAUSED: \['PAUSED', '任务已暂停，等待恢复'\]/);
    assert.match(html, /id="generation-v2-pig-status"/);
    assert.match(html, /id="generation-v2-pig-message"/);
    assert.match(source, /const presentation = generationStatePresentation\(state, \{/);
    assert.match(source, /setActionButton\('pause-generation-btn', 'PAUSE', presentation\.key ===/);
});

test('任务时间线的真实详情节点拥有独立的层级样式', () => {
    const styles = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');

    for (const className of ['log-node', 'log-content', 'log-detail', 'log-meta', 'log-status-badge']) {
        assert.match(styles, new RegExp(`generation-v2-log \\.${className}`));
    }
    assert.match(styles, /generation-v2-log \.log-body::before/);
    assert.match(styles, /log-source-preview\[open\] summary::before/);
});

test('恢复面板完整展示持久化错误，不用单行省略隐藏失败原因', () => {
    const styles = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');
    const recoveryCopy = styles.match(/#page-3\.generation-v2-page \.generation-recovery-copy p \{[\s\S]*?\n\}/)?.[0] || '';
    assert.match(recoveryCopy, /white-space:\s*normal/);
    assert.match(recoveryCopy, /overflow-wrap:\s*anywhere/);
    assert.doesNotMatch(recoveryCopy, /text-overflow:\s*ellipsis/);
});

test('无边框窗口的顶部区域可拖动且不会拦截交互控件', () => {
    const styles = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');
    const html = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'index.html'), 'utf8');
    const mainSource = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');

    assert.match(html, /class="window-drag-region"/);
    assert.match(styles, /body\.platform-darwin \.window-drag-region \{[\s\S]*-webkit-app-region:\s*drag;/);
    assert.match(styles, /#sidebar,\s*#toolbar \{[^}]*-webkit-app-region:\s*drag;/s);
    assert.match(styles, /#sidebar button,\s*#sidebar button \*,[\s\S]*-webkit-app-region:\s*no-drag;/s);
    assert.match(mainSource, /windowOptions\.frame = false/);
});

test('启动保持导入页，核对操作栏固定在窗口且不再暴露配置并生成', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
    const html = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'index.html'), 'utf8');
    const styles = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');

    assert.doesNotMatch(source, /showHistoryPage\(\{ refresh: false \}\)/);
    assert.doesNotMatch(source, /showToast\('就绪'\)/);
    assert.doesNotMatch(source, /workspace-status-banner/);
    assert.doesNotMatch(html, /id="workspace-status-action"/);
    assert.doesNotMatch(html, /workspace-status-banner/);
    assert.match(html, /id="task-status-badge"/);
    assert.doesNotMatch(styles, /workspace-status-banner/);
    assert.equal((html.match(/class="workspace-action-dock review-actions"/g) || []).length, 1);
    assert.match(source, /pinReviewActionsToWindow\(\)/);
    assert.match(source, /reviewPage\.appendChild\(actions\)/);
    assert.match(styles, /\.review-actions \{[^}]*position: fixed;[^}]*right: 0; bottom: 0; left: 0;/);
    assert.match(styles, /body\[data-active-workspace="review"\] #page-2\.active \.review-actions/);
});

test('试听开关和生成方式单选项覆盖整块卡片，避免焦点滚动外层步骤页', () => {
    const styles = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');

    for (const selector of ['.preview-option', '.generation-mode-option']) {
        assert.match(styles, new RegExp(`${selector.replace('.', '\\.') } \\{[^}]*position: relative;`));
        assert.match(styles, new RegExp(`${selector.replace('.', '\\.') } input \\{[^}]*inset: 0;[^}]*pointer-events: auto;`));
        assert.match(styles, new RegExp(`${selector.replace('.', '\\.') }:focus-within \\{`));
    }
});

test('生成页复用全局主题色，不再维护独立的明暗色板', () => {
    const styles = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');

    assert.match(styles, /--gen-canvas: var\(--canvas\)/);
    assert.match(styles, /--gen-surface: var\(--surface\)/);
    assert.match(styles, /--gen-blue: var\(--primary\)/);
    assert.match(styles, /--gen-error-soft: var\(--danger-soft\)/);
    assert.match(styles, /#page-3\.generation-v2-page \{[^}]*background: transparent;/s);
    assert.doesNotMatch(styles, /html\[data-theme="dark"\] #page-3\.generation-v2-page/);
});

test('提交未完成时保留重新生成入口，不暴露对账提示', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
    assert.match(source, /提交未完成，可重新生成/);
    assert.match(source, /ambiguous: false/);
    assert.doesNotMatch(source, /先确认未提交后再重试/);
});

test('试听媒体发生错误时会清理旧 Blob URL，后续点击可重新取 Artifact', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
    assert.match(source, /let audioObjectUrl = null/);
    assert.match(source, /artifactObjectUrls\.delete\(audioObjectUrl\)/);
    assert.match(source, /audioReadyPromise = null/);
    assert.match(source, /audio\.addEventListener\('error', \(\) => \{\s*\/\/ A successful ticket read/s);
});

test('当前配置写入 localStorage 前会清理旧角色数据', () => {
    const { api, storage } = loadRendererConfigFunctions();
    api.saveCurrentConfig({
        default_female_voice: 'amanda',
        default_male_voice: 'george',
        role_configs: {
            __default_female__: { rate: 35, volume: 50, pitch: 50 },
            __default_male__: { rate: 65, volume: 50, pitch: 50 },
            'role:mr yan': { rate: 1, volume: 2, pitch: 3 },
        },
        role_voices: { 'mr yan': 'george' },
    });

    const saved = JSON.parse(storage.get('wordtts_current_config_xunfei_v3'));
    assert.deepEqual(saved.role_configs, {
        __default_female__: { rate: 35, volume: 50, pitch: 50 },
        __default_male__: { rate: 65, volume: 50, pitch: 50 },
    });
    assert.equal('role_voices' in saved, false);
});

test('进度计数始终按整数四舍五入并限制在总数内', () => {
    const { api } = loadRendererConfigFunctions();

    assert.equal(api.integerProgressCount(3.6, 37), 4);
    assert.equal(api.integerProgressCount(33.4, 37), 33);
    assert.equal(api.integerProgressCount(999.9, 37), 37);
    assert.equal(api.integerProgressCount('not-a-number', 37), 0);
    assert.equal(api.visualProgressPercent(100), 99);
    assert.equal(api.visualProgressPercent(-4), 0);
    assert.equal(api.terminalProgressPercent(0, 37), 0);
    assert.equal(api.terminalProgressPercent(10, 37), 27);
    assert.equal(api.terminalProgressPercent(37, 37), 100);
});

test('结果页按文件音色元数据去重，并兼容可精确匹配的旧 voice 字段', () => {
    const { api } = loadRendererConfigFunctions();
    api.setVoiceCatalog([{ key: 'speaker:linda', name: 'Linda-品质' }]);

    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({
        voice_keys: ['speaker:linda', 'speaker:linda', '', null],
        voice_key: 'speaker:george',
    }))), ['speaker:linda', 'speaker:george']);
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({
        voice_keys: [''],
        voice: 'Linda-品质',
    }))), ['speaker:linda']);
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({ voice_keys: 'speaker:linda' }))), ['speaker:linda']);
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({ voice_keys: ['Linda-品质'] }))), ['speaker:linda']);
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({ voice: 'linda-品质' }))), ['speaker:linda']);
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({ voice: 'Amanda' }))), ['amanda']);
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({ voice: '女声' }))), []);
});

test('结果页头像缓存未完成时先显示目录远程资源，缓存完成后才切换本地地址', () => {
    const { api } = loadRendererConfigFunctions();
    const remoteAvatar = 'https://example.test/linda.jpg';
    const remoteSample = 'https://example.test/linda.mp3';
    api.setVoiceCatalog([{
        key: 'speaker:linda',
        name: 'Linda-品质',
        img_url: remoteAvatar,
        audio_url: remoteSample,
    }]);

    let voice = api.getResultVoiceEntry('speaker:linda');
    assert.equal(voice.img_url, remoteAvatar);
    assert.equal(voice.fallback_img_url, '');
    assert.equal(voice.audio_url, remoteSample);

    api.voiceAssetCacheReady.add('speaker:linda');
    voice = api.getResultVoiceEntry('speaker:linda');
    assert.match(voice.img_url, /\/api\/v1\/voice-assets\/speaker%3Alinda\/avatar/);
    assert.equal(voice.fallback_img_url, remoteAvatar);
    assert.match(voice.audio_url, /\/api\/v1\/voice-assets\/speaker%3Alinda\/sample/);
    assert.equal(voice.fallback_audio_url, remoteSample);
});

test('结果页只接受成功条目的 READY 已验证音频，并使用服务端交付元数据', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [
        { item_id: 'item-ok', item_type: '句子', status: 'SUCCEEDED', sequence: 4, normalized_content: 'ok' },
        { item_id: 'item-failed', item_type: '句子', status: 'FAILED', sequence: 1, normalized_content: 'failed' },
    ];
    const artifacts = [
        { artifact_id: 'old', item_id: 'item-ok', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, created_at: '2026-01-01T00:00:00Z' },
        { artifact_id: 'new', item_id: 'item-ok', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, created_at: '2026-01-02T00:00:00Z' },
        { artifact_id: 'failed-ready', item_id: 'item-failed', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, created_at: '2026-01-03T00:00:00Z' },
    ];
    const workspace = {
        items: items.map(({ item_id, status }) => ({ item_id, status })),
        artifacts: [
            { artifact_id: 'old', item_id: 'item-ok', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, filename: '005.mp3', format: 'mp3', mime_type: 'audio/mpeg', size_bytes: 10, sha256: 'a'.repeat(64) },
            { artifact_id: 'new', item_id: 'item-ok', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, filename: 'server-name.mp3', format: 'mp3', mime_type: 'audio/mpeg', size_bytes: 12, sha256: 'b'.repeat(64) },
            { artifact_id: 'failed-ready', item_id: 'item-failed', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, filename: '002.mp3', format: 'mp3', mime_type: 'audio/mpeg', size_bytes: 8, sha256: 'c'.repeat(64) },
        ],
    };

    const files = api.resultFilesFromArtifacts(items, artifacts, workspace);
    assert.equal(files.length, 1);
    assert.equal(files[0].artifact_id, 'new');
    assert.equal(files[0].filename, 'server-name.mp3');
    assert.equal(files[0].format, 'mp3');
    assert.equal(files[0].mime_type, 'audio/mpeg');
});

test('结果页不会把最新 WAV 产物回退为旧 MP3 交付', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [{ item_id: 'item-1', item_type: '句子', status: 'SUCCEEDED', sequence: 0 }];
    const artifacts = [
        { artifact_id: 'old', item_id: 'item-1', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, created_at: '2026-01-01T00:00:00Z' },
        { artifact_id: 'new', item_id: 'item-1', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, created_at: '2026-01-02T00:00:00Z' },
    ];
    const workspace = {
        items,
        artifacts: [
            { artifact_id: 'old', item_id: 'item-1', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, filename: '001.mp3', format: 'mp3', mime_type: 'audio/mpeg', size_bytes: 10, sha256: 'a'.repeat(64) },
            { artifact_id: 'new', item_id: 'item-1', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, filename: '001.wav', format: 'wav', mime_type: 'audio/wav', size_bytes: 12, sha256: 'b'.repeat(64) },
        ],
    };
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultFilesFromArtifacts(items, artifacts, workspace))), []);
});

test('结果页不会在最新 TTS 产物无效时回退到旧音频', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [{ item_id: 'item-1', item_type: '句子', status: 'SUCCEEDED', sequence: 0 }];
    const artifacts = [
        { artifact_id: 'old', item_id: 'item-1', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, created_at: '2026-01-01T00:00:00Z' },
        { artifact_id: 'new', item_id: 'item-1', artifact_type: 'tts-segment', lifecycle_state: 'TEMP', verified: false, created_at: '2026-01-02T00:00:00Z' },
    ];
    const workspace = {
        items,
        artifacts: [
            { artifact_id: 'old', item_id: 'item-1', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, filename: '001.mp3', format: 'mp3', mime_type: 'audio/mpeg', size_bytes: 10 },
            { artifact_id: 'new', item_id: 'item-1', artifact_type: 'tts-segment', lifecycle_state: 'TEMP', verified: false, filename: '001.mp3', format: 'mp3', mime_type: 'audio/mpeg', size_bytes: 10 },
        ],
    };

    assert.deepEqual(JSON.parse(JSON.stringify(api.resultFilesFromArtifacts(items, artifacts, workspace))), []);
});

test('结果页不会用原始 Artifact 列表复活 workspace 已标记的元数据冲突', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [{ item_id: 'item-1', item_type: '句子', status: 'SUCCEEDED', sequence: 0 }];
    const artifacts = [{
        artifact_id: 'old', item_id: 'item-1', artifact_type: 'tts-segment',
        lifecycle_state: 'READY', verified: true, format: 'mp3', size_bytes: 10,
        sha256: 'a'.repeat(64), created_at: '2026-01-01T00:00:00Z',
    }, {
        artifact_id: 'new', item_id: 'item-1', artifact_type: 'tts-segment',
        lifecycle_state: 'READY', verified: true, format: 'mp3', size_bytes: 12,
        sha256: 'b'.repeat(64), created_at: '2026-01-02T00:00:00Z',
    }];
    const workspace = {
        items,
        artifacts: [{
            artifact_id: 'new', item_id: 'item-1', artifact_type: 'tts-segment',
            lifecycle_state: 'READY', verified: true,
            // The server hides conflicting facts instead of exposing an
            // unsafe filename/size/hash projection to the renderer.
            filename: null, format: null, mime_type: null, size_bytes: null, sha256: null,
        }],
    };
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultFilesFromArtifacts(items, artifacts, workspace))), []);

    // An empty authoritative projection means “no exposed artifacts”; it is
    // not permission to fall back to a legacy list that still contains bytes.
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultFilesFromArtifacts(items, artifacts, {
        items,
        artifacts: [],
    }))), []);
});

test('新完成任务在 ZIP 尚未创建时仍保留整理入口', () => {
    const { api } = loadRendererConfigFunctions();

    assert.deepEqual(JSON.parse(JSON.stringify(api.resultZipState({
        executionState: 'TERMINAL',
        resultStatus: 'SUCCEEDED',
        zipAvailable: false,
        zipArtifactId: null,
    }, 2))), { visible: true, ready: false });

    assert.deepEqual(JSON.parse(JSON.stringify(api.resultZipState({
        executionState: 'TERMINAL',
        resultStatus: 'SUCCEEDED',
        zipAvailable: true,
        zipArtifactId: 'zip-1',
    }, 2))), { visible: true, ready: true });

    assert.deepEqual(JSON.parse(JSON.stringify(api.resultZipState({
        executionState: 'RUNNING',
        resultStatus: 'IN_PROGRESS',
        zipAvailable: false,
        zipArtifactId: null,
    }, 2))), { visible: false, ready: false });
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultZipState({
        executionState: 'TERMINAL',
        resultStatus: 'SUCCEEDED',
        zipAvailable: false,
        zipArtifactId: null,
    }, 0))), { visible: false, ready: false });

    // A workspace projection with no ZIP is authoritative even when an older
    // result context still carries a ready-looking ZIP id.
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultZipState({
        executionState: 'TERMINAL',
        resultStatus: 'SUCCEEDED',
        zipAvailable: true,
        zipArtifactId: 'stale-zip',
        workspace: {
            delivery: {
                zip_available: false,
                zip_artifact_id: null,
                included_item_ids: ['item-1'],
                excluded_item_ids: [],
                exclusion_reasons: {},
            },
        },
    }, 1))), { visible: true, ready: false });
});

test('ZIP 下载不从普通 Artifact 列表猜测旧导出', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
    assert.doesNotMatch(source, /artifacts\.find\(artifact => \(\s*artifact\.lifecycle_state === 'READY'[\s\S]*artifact\.artifact_type === 'export-zip'/);
    assert.match(source, /target\.mode === 'history' && !projectedWorkspace/);
    assert.match(source, /projectedDelivery\.zip_available === true/);
    assert.match(source, /hasAuthoritativeDelivery/);
});

test('历史记录按终态区分归档和删除', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
    assert.match(source, /terminal \? '归档' : '删除'/);
    assert.match(source, /workflowApi\.deleteWorkflow/);
    assert.match(source, /未完成任务及其相关本地数据已删除/);
});

test('历史记录状态投影不会把活动任务显示为完成或文件缺失', () => {
    const { api } = loadRendererConfigFunctions();
    assert.equal(api.historyStatusPresentation({ execution_state: 'RUNNING', result_status: 'IN_PROGRESS' }).label, '生成中');
    assert.equal(api.historyStatusPresentation({ execution_state: 'WAITING_USER', result_status: 'IN_PROGRESS' }).label, '待处理');
    assert.equal(api.historyStatusPresentation({ execution_state: 'TERMINAL', result_status: 'SUCCEEDED' }).label, '已完成');
    assert.equal(api.historyStatusPresentation({
        execution_state: 'TERMINAL',
        result_status: 'SUCCEEDED',
        completed: 0,
        available_files: 2,
        total: 2,
    }).label, '交付待同步');
    assert.equal(api.historyStatusPresentation({ execution_state: 'TERMINAL', result_status: 'FAILED' }).label, '生成失败');

    // A zero in the server workspace is authoritative; Math.max-style
    // merging would incorrectly revive stale history counts.
    assert.deepEqual(JSON.parse(JSON.stringify(api.historyProgressCounts(
        { completed: 0, total: 3, failed: 0, cancelled: 0 },
        { completed: 2, total: 3, failed: 4, cancelled: 1 },
        2,
    ))), {
        completed: 0,
        total: 3,
        failed: 0,
        cancelled: 0,
    });
    assert.deepEqual(JSON.parse(JSON.stringify(api.historyProgressCounts(
        {},
        { completed: 2, total: 3, failed: 1, cancelled: 0 },
        2,
    ))), {
        completed: 2,
        total: 3,
        failed: 1,
        cancelled: 0,
    });

    // The same missing item must not be counted again as a delivery blocker.
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultSummaryCounts(
        { completed: 2, failed: 0, cancelled: 0 },
        1,
        { completed: 2, failed: 9, cancelled: 9 },
        1,
    ))), {
        reportedCompleted: 2,
        success: 1,
        missingFiles: 1,
        failed: 1,
        cancelled: 0,
        deliveryIssues: 1,
        unresolved: 1,
    });
});

test('历史活动任务只在服务端能力允许时显示继续生成', () => {
    const { api } = loadRendererConfigFunctions();
    const workspace = { snapshot: { workflow_id: 'workflow-1' } };

    assert.equal(api.historyActiveCandidateState({ workspace, can_takeover: true }), 'takeover');
    assert.equal(api.historyActiveActionLabel({ execution_state: 'RUNNING', result_status: 'IN_PROGRESS', active_candidate: { workspace, can_takeover: true } }), '继续生成');
    assert.equal(api.historyActiveActionLabel({ execution_state: 'PAUSED', result_status: 'IN_PROGRESS', active_candidate: { workspace, can_resume: true } }), '恢复上下文');
    assert.equal(api.historyActiveActionLabel({ execution_state: 'RUNNING', result_status: 'IN_PROGRESS', active_candidate: { workspace } }), '恢复上下文');
    assert.equal(api.historyActiveActionLabel({ execution_state: 'WAITING_USER', result_status: 'IN_PROGRESS', active_candidate: { workspace, requires_reconcile: true } }), '恢复上下文');
    assert.equal(api.historyActiveActionLabel({ execution_state: 'RUNNING', result_status: 'IN_PROGRESS' }), '查看状态');
    assert.equal(api.historyActiveStatusLabel({ workspace, can_takeover: true }), '可继续生成');
    assert.equal(api.historyActiveStatusLabel({ workspace, can_resume: true }), '可恢复上下文');
    assert.equal(api.historyActiveStatusLabel({ workspace }), '待处理');
});

test('活动任务提示按接管、恢复和待处理能力分组', () => {
    const { api } = loadRendererConfigFunctions();
    const workspace = { snapshot: { workflow_id: 'workflow-1' } };
    const text = api.activeCandidateHintText([
        { workspace, can_takeover: true },
        { workspace, can_resume: true },
        { workspace },
        { workspace },
        { workspace: null },
    ], true);

    assert.equal(text, '1 个任务可继续生成，1 个任务可恢复上下文，2 个任务待处理，1 个任务状态待同步（列表已截断）');
});

test('兼容导入按流式分块读取并执行明确大小上限', async () => {
    const { api } = loadRendererConfigFunctions();
    let arrayBufferCalled = false;
    const file = {
        size: 6,
        stream: () => new ReadableStream({
            start(controller) {
                controller.enqueue(new Uint8Array([1, 2, 3]));
                controller.enqueue(new Uint8Array([4, 5, 6]));
                controller.close();
            },
        }),
        arrayBuffer: async () => {
            arrayBufferCalled = true;
            return new ArrayBuffer(6);
        },
    };
    const bytes = await api.readBoundedSourceFile(file, 8, null);
    assert.deepEqual([...bytes], [1, 2, 3, 4, 5, 6]);
    assert.equal(arrayBufferCalled, false);

    await assert.rejects(
        api.readBoundedSourceFile({
            size: 6,
            stream: () => new ReadableStream({
                start(controller) {
                    controller.enqueue(new Uint8Array([1, 2, 3, 4, 5]));
                    controller.enqueue(new Uint8Array([6]));
                },
            }),
        }, 4, null),
        error => error.code === 'SOURCE_SIZE_LIMIT',
    );
});

test('Electron 窗口保持隔离并允许受信任的 CommonJS preload 加载工作流模块', () => {
    const mainSource = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
    const preloadSource = fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8');

    assert.match(mainSource, /contextIsolation:\s*true/);
    assert.match(mainSource, /nodeIntegration:\s*false/);
    assert.match(mainSource, /sandbox:\s*false/);
    assert.match(preloadSource, /require\(['"]\.\/workflow-api['"]\)/);
});

test('Electron 窗口关闭后清理逻辑不访问已销毁的 webContents', () => {
    const mainSource = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');

    assert.match(mainSource, /const windowWebContentsId = win\.webContents\.id/);
    assert.match(mainSource, /closeWorkflowStreamsForSender\(windowWebContentsId, ['"]window-closed['"]\)/);
    assert.doesNotMatch(mainSource, /closeWorkflowStreamsForSender\(win\.webContents\.id, ['"]window-closed['"]\)/);
});

test('Renderer 只保留单一工作台入口', () => {
    const mainSource = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
    const preloadSource = fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8');
    const rendererDirectory = path.join(__dirname, '..', 'renderer');

    assert.match(mainSource, /path\.join\(__dirname, 'renderer', 'index\.html'\)/);
    assert.doesNotMatch(mainSource, /WORDTTS_RENDERER_SHELL|index-legacy/);
    assert.doesNotMatch(preloadSource, /WORDTTS_RENDERER_SHELL|rendererShell/);
    assert.equal(fs.existsSync(path.join(rendererDirectory, 'index.html')), true);
    assert.equal(fs.existsSync(path.join(rendererDirectory, 'index-legacy.html')), false);
    assert.equal(fs.existsSync(path.join(rendererDirectory, 'legacy-app.js')), false);
    assert.equal(fs.existsSync(path.join(rendererDirectory, 'legacy-styles.css')), false);
});
