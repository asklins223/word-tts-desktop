const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function loadRendererConfigFunctions() {
    const sourcePath = path.join(__dirname, '..', 'renderer', 'app.js');
    const source = fs.readFileSync(sourcePath, 'utf8').replace(/\ninit\(\);\s*$/, '\n');
    const storage = new Map();
    const context = {
        console,
        document: {},
        window: { electronAPI: undefined },
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
        Map,
        Set,
    };
    vm.createContext(context);
    vm.runInContext(`${source}\nglobalThis.__rendererTests = { clampParamValue, normalizeClientConfig, normalizePersistedConfig, buildWorkflowConfiguration, saveCurrentConfig, integerProgressCount, visualProgressPercent, resultVoiceKeysForFile, resultFilesFromArtifacts, resultZipState, historyStatusPresentation, setVoiceCatalog, getVoiceFilterOptions, migrateVoiceSelections, canonicalVoiceKey, getResultVoiceEntry, voiceAssetCacheReady, mergeWorkflowSnapshotIntoSession, isTerminalWorkflowSnapshot, isAcceptedGenerationSnapshot, isWaitingForGenerationCleanup, isCancellationSettledSnapshot };`, context);
    return { api: context.__rendererTests, storage };
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

test('已接受或尚未清理的工作流不能再次当成可编辑草稿提交', () => {
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
    assert.equal(api.isWaitingForGenerationCleanup({
        execution_state: 'WAITING_RETRY',
        control_state: 'RUNNING',
        cleanup_state: 'NONE',
        result_status: 'IN_PROGRESS',
    }), true);
    assert.equal(api.isAcceptedGenerationSnapshot({
        execution_state: 'WAITING_RETRY',
        control_state: 'RUNNING',
        cleanup_state: 'SUCCEEDED',
        result_status: 'IN_PROGRESS',
    }), false);
    assert.equal(api.isTerminalWorkflowSnapshot({
        execution_state: 'TERMINAL',
        result_status: 'CANCELLED',
    }), true);
});

test('生成页提供停止入口，并把配置冻结竞态收敛到接管流程', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
    const html = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'index.html'), 'utf8');
    assert.match(source, /adoptAcceptedGeneration\(session, authoritative/);
    assert.match(source, /cancelCurrentWorkflow\(session/);
    assert.match(source, /desktop-return-to-configuration/);
    assert.match(html, /id="cancel-generation-btn"/);
});

test('待核验提交不会暴露普通重试入口，避免把只读对账误显示成重新生成', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
    assert.match(source, /retryButton\.hidden = Boolean\(ambiguous\)/);
    assert.match(source, /先确认未提交后再重试/);
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
            { artifact_id: 'old', item_id: 'item-ok', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, filename: '005.mp3', format: 'mp3', mime_type: 'audio/mpeg', size_bytes: 10 },
            { artifact_id: 'new', item_id: 'item-ok', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, filename: 'server-name.wav', format: 'wav', mime_type: 'audio/wav', size_bytes: 12 },
            { artifact_id: 'failed-ready', item_id: 'item-failed', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, filename: '002.mp3', format: 'mp3', mime_type: 'audio/mpeg', size_bytes: 8 },
        ],
    };

    const files = api.resultFilesFromArtifacts(items, artifacts, workspace);
    assert.equal(files.length, 1);
    assert.equal(files[0].artifact_id, 'new');
    assert.equal(files[0].filename, 'server-name.wav');
    assert.equal(files[0].format, 'wav');
    assert.equal(files[0].mime_type, 'audio/wav');
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
});

test('历史记录状态投影不会把活动任务显示为完成或文件缺失', () => {
    const { api } = loadRendererConfigFunctions();
    assert.equal(api.historyStatusPresentation({ execution_state: 'RUNNING', result_status: 'IN_PROGRESS' }).label, '生成中');
    assert.equal(api.historyStatusPresentation({ execution_state: 'WAITING_USER', result_status: 'IN_PROGRESS' }).label, '待处理/对账');
    assert.equal(api.historyStatusPresentation({ execution_state: 'TERMINAL', result_status: 'SUCCEEDED' }).label, '已完成');
    assert.equal(api.historyStatusPresentation({ execution_state: 'TERMINAL', result_status: 'FAILED' }).label, '生成失败');
});

test('Electron 窗口保持隔离并允许受信任的 CommonJS preload 加载工作流模块', () => {
    const mainSource = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
    const preloadSource = fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8');

    assert.match(mainSource, /contextIsolation:\s*true/);
    assert.match(mainSource, /nodeIntegration:\s*false/);
    assert.match(mainSource, /sandbox:\s*false/);
    assert.match(preloadSource, /require\(['"]\.\/workflow-api['"]\)/);
});
