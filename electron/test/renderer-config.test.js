const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function readRendererSource() {
    const rendererDir = path.join(__dirname, '..', 'renderer');
    const html = fs.readFileSync(path.join(rendererDir, 'index.html'), 'utf8');
    const scripts = [...html.matchAll(/<script\s+src="([^"]+)"><\/script>/g)]
        .map(match => match[1])
        .filter(script => script !== 'vendor/wavesurfer.min.js' && script !== 'ui-components.js');
    return scripts
        .map(script => fs.readFileSync(path.join(rendererDir, script), 'utf8'))
        .join('\n')
        .replace(/\ninit\(\);\s*$/, '\n');
}

function readRendererStyles() {
    return fs.readFileSync(
        path.join(__dirname, '..', 'renderer', 'styles.css'),
        'utf8',
    );
}

function loadRendererConfigFunctions() {
    const source = readRendererSource();
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
    vm.runInContext(`${source}\nglobalThis.__rendererTests = { clampParamValue, normalizeClientConfig, normalizePersistedConfig, buildWorkflowConfiguration, saveCurrentConfig, integerProgressCount, visualProgressPercent, terminalProgressPercent, generationStatePresentation, generationRecoveryPresentation, generationRecoveryIsSuppressed, generationProgressPercentForView, generationProgressCopy, generationProgressAriaText, compositeRuntimeReadout, compositeRuntimeProjection, runtimeProgressNeedsIndeterminate, resultVoiceKeysForFile, resultVoiceKeysForItem, resultVoiceKeysFromAcceptedContent, resultFilesFromArtifacts, resultZipState, filenameWithExtension, deliveryZipFilename, resultVoiceKeyFromAcceptedConfiguration, historyStatusPresentation, historyRecordDeliveryMode, historyRecordInputStatus, historyInputStatusPresentation, historyInputUnitsProgress, historyDeliveryTagLabel, historyRecordInputAttention, historyRecordInputSettled, historyRecordMatchesFilter, historyActiveCandidateState, historyActiveActionLabel, historyActiveStatusLabel, activeCandidateHintText, readBoundedSourceFile, nonNegativeCount, historyProgressCounts, resultSummaryCounts, setVoiceCatalog, getVoiceFilterOptions, migrateVoiceSelections, canonicalVoiceKey, getResultVoiceEntry, voiceAssetCacheReady, workflowSnapshotIsOlder, workflowSnapshotBelongsToSession, mergeWorkflowSnapshotIntoSession, isTerminalWorkflowSnapshot, isHardStoppedWorkflowSnapshot, isAcceptedGenerationSnapshot, isCancellationSettledSnapshot, shouldAdoptResumedGeneration, generationWorkspaceNavigationAllowed, generationWorkflowOwnsRuntimeView, reviewTypePathForItem, reviewVoicePresentation, reviewDocumentSequence, reviewDocumentEntrySupport, reviewDocumentQuestionCountForItems, reviewItemsInDocumentOrder, reviewGroupsWithUnits, reviewUnitCountPresentation, buildReviewUnitModels, reviewOutlineReportedCount, buildReviewOutlineModel, reviewOutlineDefaultExpanded, reviewOutlineFirstItem, normalizeUpdateState, hasInstallableUpdate, updateStatusPresentation, formatUpdateBytes, rendererReadableArtifactStream, sourceFileDisplayName, pendingSourceFilePresentation, globalFileDropPresentation, handleIncomingSourceFile, setUploadParsing, schedulePendingServiceSourceFileImport, systemInputDocumentEntryIsSupported, systemInputUnitMissingFields, systemInputUnitStatusPresentation, systemInputPaperNameForUnit, systemInputConfigurationEditable, systemInputCommonConfiguration, systemInputMergeAppTemplateIntoUnit, systemInputPaperDisclosureDetail, systemInputRangeDisclosureDetail, systemInputUnitDisclosureDetail, systemInputRegionCascadeState, systemInputPlatformTemplateInteractionPresentation, flattenSystemInputRegions, projectSystemInput, pendingSourceFile: () => pendingSourceFile, setSourceImportServiceState: state => { sourceImportServiceState = state; }, setSourceImportBusy: busy => { isParsing = Boolean(busy); sourceImportInFlight = Boolean(busy); } };`, context);
    vm.runInContext('globalThis.__rendererTests.pendingServiceSourceFile = () => pendingServiceSourceFile;', context);
    vm.runInContext('globalThis.__rendererTests.createDefaultVoiceParams = createDefaultVoiceParams; globalThis.__rendererTests.inferRoleVoice = inferRoleVoice; globalThis.__rendererTests.discoverVoiceRoles = discoverVoiceRoles; globalThis.__rendererTests.activeVoiceKeyForRole = activeVoiceKeyForRole; globalThis.__rendererTests.collectConfig = collectConfig; globalThis.__rendererTests.setCurrentSession = value => { currentSession = value; };', context);
    vm.runInContext('globalThis.__rendererTests.singleSegmentRuntimeActive = singleSegmentRuntimeActive; globalThis.__rendererTests.singleSegmentRuntimeReadout = singleSegmentRuntimeReadout;', context);
    vm.runInContext('globalThis.__rendererTests.historyFilters = historyFilters;', context);
    vm.runInContext('globalThis.__rendererTests.systemInputCascadeFieldSelected = systemInputCascadeFieldSelected;', context);
    vm.runInContext('globalThis.__rendererTests.systemInputTemplateConfigurationForUnit = systemInputTemplateConfigurationForUnit; globalThis.__rendererTests.systemInputTemplateConfigurationKey = systemInputTemplateConfigurationKey; globalThis.__rendererTests.systemInputAppTemplateEntries = systemInputAppTemplateEntries; globalThis.__rendererTests.systemInputAppTemplateNameForGroup = systemInputAppTemplateNameForGroup; globalThis.__rendererTests.systemInputAppTemplatePlatformReference = systemInputAppTemplatePlatformReference; globalThis.__rendererTests.systemInputAppTemplatePayload = systemInputAppTemplatePayload;', context);
    vm.runInContext('globalThis.__rendererTests.systemInputRunIsComplete = systemInputRunIsComplete; globalThis.__rendererTests.systemInputEntryStatus = systemInputEntryStatus; globalThis.__rendererTests.systemInputEvidenceHash = systemInputEvidenceHash; globalThis.__rendererTests.systemInputRunWasStopped = systemInputRunWasStopped; globalThis.__rendererTests.systemInputRunControlState = systemInputRunControlState; globalThis.__rendererTests.systemInputVerificationOutcome = systemInputVerificationOutcome;', context);
    vm.runInContext('globalThis.__rendererTests.systemInputDeliveryModeIsLocked = WORDTTS_RENDERER.getModule("systemInput.delivery").systemInputDeliveryModeIsLocked;', context);
    vm.runInContext('globalThis.__rendererTests.systemInputUnitConfiguration = systemInputUnitConfiguration; globalThis.__rendererTests.systemInputNormalizeUnitConfiguration = systemInputNormalizeUnitConfiguration; globalThis.__rendererTests.systemInputDefaultAnswerTimeForCategory = systemInputDefaultAnswerTimeForCategory;', context);
    vm.runInContext('globalThis.__rendererTests.systemInputAppTemplateTargetUnits = systemInputAppTemplateTargetUnits;', context);
    vm.runInContext('globalThis.__rendererTests.systemInputDiagnosticText = systemInputDiagnosticText;', context);
    vm.runInContext('globalThis.__rendererTests.systemInputReviewDocumentName = systemInputReviewDocumentName; globalThis.__rendererTests.systemInputReviewPresentation = systemInputReviewPresentation;', context);
    vm.runInContext('globalThis.__rendererTests.reviewPageText = WORDTTS_RENDERER.getModule("review.document").reviewPageText; globalThis.__rendererTests.reviewTextbookParagraphGroups = WORDTTS_RENDERER.getModule("review.document").reviewTextbookParagraphGroups; globalThis.__rendererTests.reviewTextbookDetectedForm = WORDTTS_RENDERER.getModule("review.document").reviewTextbookDetectedForm; globalThis.__rendererTests.reviewTextbookFormForGroup = WORDTTS_RENDERER.getModule("review.document").reviewTextbookFormForGroup; globalThis.__rendererTests.buildReviewTypeGroups = WORDTTS_RENDERER.getModule("review.document").buildReviewTypeGroups;', context);
    vm.runInContext('globalThis.__rendererTests.deliveryStageInputHasStarted = deliveryStageInputHasStarted; globalThis.__rendererTests.deliveryStageResultIsReady = deliveryStageResultIsReady; globalThis.__rendererTests.deliveryStageStatus = deliveryStageStatus;', context);
    vm.runInContext('globalThis.__rendererTests.rendererContext = WORDTTS_RENDERER.getContext();', context);
    vm.runInContext('globalThis.__rendererTests.initializeTheme = initializeTheme; globalThis.__rendererTests.setWorkspaceTheme = setWorkspaceTheme;', context);
    return { api: context.__rendererTests, storage, document, mediaState };
}

test('文稿核对仅隐藏圆括号说话人标识并保留系统录入的 W/M 冒号标识', () => {
    const { api } = loadRendererConfigFunctions();
    assert.equal(
        api.reviewPageText('W: First line.\n(M) Second line.\nM： Third line.\n(W)： Fourth line.'),
        'W: First line.\nSecond line.\nM： Third line.\nFourth line.',
    );
    assert.equal(
        api.reviewPageText(
            'First line.\nSecond line.',
            'W: First line.\n(M) Second line.',
        ),
        'W: First line.\nSecond line.',
    );
});

test('系统录入审阅优先展示平台试卷/课文名，平台不支持链接时给出非失败提示', () => {
    const { api } = loadRendererConfigFunctions();
    assert.equal(
        api.systemInputReviewDocumentName({
            document_name: 'source-paper.docx',
            unit_label: '第一套',
            configuration: { paperName: '2026 春季听说考试' },
        }),
        '2026 春季听说考试',
    );
    assert.equal(
        api.systemInputReviewDocumentName({
            document_name: 'source-text.docx',
            unit_label: 'Unit 2',
            configuration: { textbookNameZh: 'My School Life' },
        }),
        'My School Life',
    );
    const unavailable = api.systemInputReviewPresentation({
        review_url_status: 'unavailable',
        review_url_source: 'platform_not_supported',
    }, 'succeeded');
    assert.equal(unavailable.label, '平台未提供链接');
    assert.match(unavailable.detail, /文档名/);
});

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

test('信息转述题干显示为可编辑的题干音色角色，并使用晓燕推荐值', () => {
    const { api } = loadRendererConfigFunctions();
    const parseResults = [{
        items: [{
            category: '信息转述录音稿',
            role: null,
            text: 'Emma introduces her plan card.',
        }, {
            category: '信息转述题目指导文字',
            role: '题干音色',
            voice: 'female',
            text: '你将听到 Emma 介绍她的计划卡。',
        }, {
            category: '信息转述题干',
            role: '题干音色',
            text: '你的介绍可以这样开始：Let me tell you about Emma.',
        }, {
            category: '询问信息题干',
            role: '题干音色',
            text: '请根据以下提示向 Emma 提两个问题。',
        }],
    }];
    api.setCurrentSession({ parse_results: parseResults });

    const roles = api.discoverVoiceRoles(parseResults);
    const stemRole = roles.find(role => role.label === '题干音色');
    assert.equal(stemRole.kind, 'question-stem');
    assert.equal(api.activeVoiceKeyForRole(stemRole), 'common:10000023');
    assert.deepEqual(
        JSON.parse(JSON.stringify(api.createDefaultVoiceParams('role:题干音色'))),
        { rate: 40, volume: 50, pitch: 50 },
    );

    const recommended = api.collectConfig(true);
    assert.equal(recommended.default_female_voice, 'amanda');
    assert.equal(recommended.default_male_voice, 'george');
    assert.equal(recommended.role_voices['题干音色'], 'common:10000023');
    assert.deepEqual(
        JSON.parse(JSON.stringify(recommended.role_configs['role:题干音色'])),
        { rate: 40, volume: 50, pitch: 50 },
    );
});

test('系统输入投影保留询问信息的题目指导文字和音频标识', () => {
    const { api } = loadRendererConfigFunctions();
    const askingInstruction = '你希望了解更多，请根据以下提示向 Emma 提两个问题。';
    const projected = api.projectSystemInput({
        content_segments: [{
            item_id: 'info-retelling-1',
            page_input: {
                type: '信息转述及询问',
                recording: {
                    listening_text: 'Emma introduces her plan card.',
                    asking_instruction_text: askingInstruction,
                    asking_instruction_occurrence: 0,
                    asking_instruction_audio_filename_stem: '询问信息题干-1',
                },
                asking: [{ number: 11, prompt: '你想问什么？' }],
            },
        }],
    });
    const recording = projected.content_segments[0].page_input.recording;
    assert.equal(recording.asking_instruction_text, askingInstruction);
    assert.equal(recording.asking_instruction_occurrence, 0);
    assert.equal(recording.asking_instruction_audio_filename_stem, '询问信息题干-1');
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
    assert.deepEqual(
        JSON.parse(JSON.stringify(api.reviewTypePathForItem({
            doc_type: '信息获取',
            item_type: '听选信息题目',
            type_path: ['听选信息题目'],
        }))),
        ['信息获取', '听选信息'],
    );
    assert.deepEqual(
        JSON.parse(JSON.stringify(api.reviewTypePathForItem({
            doc_type: '听后选择',
            item_type: '听后选择录音稿',
            type_path: ['听后选择'],
        }))),
        ['听后选择'],
    );
    assert.deepEqual(
        JSON.parse(JSON.stringify(api.reviewTypePathForItem({
            doc_type: '模仿朗读',
            item_type: '模仿朗读-试卷正文',
            type_path: ['模仿朗读'],
        }))),
        ['模仿朗读'],
    );
    assert.deepEqual(
        JSON.parse(JSON.stringify(api.reviewTypePathForItem({
            doc_type: '综合题',
            item_type: 'question',
            type_path: ['综合题', '阅读理解', '阅读理解'],
        }))),
        ['综合题', '阅读理解', '阅读理解'],
    );
    assert.equal(api.reviewVoicePresentation({ voice: 'female' }).voice, '默认女声');
    assert.equal(api.reviewVoicePresentation({ voice: 'male' }).voice, '默认男声');
});

test('文稿核对开放已接入的文档结构，并支持单独听后选择', () => {
    const { api } = loadRendererConfigFunctions();
    const fullPaper = api.reviewDocumentEntrySupport([
        { metadata: { page_input: { type: '听后选择' }, doc_type: '听后选择' } },
        { metadata: { page_input: { type: '听后应答' }, doc_type: '听后应答' } },
        {
            metadata: {
                page_input: { type: '模仿朗读' },
                doc_type: '模仿朗读',
                major_section_profile: 'imitation_boxed_special',
                entry_profile: 'imitation_reading_v1',
                capabilities: { external_input: true },
            },
        },
        { metadata: { page_input: { type: '听后记录并转述信息' }, doc_type: '听后记录并转述信息' } },
    ]);
    assert.equal(fullPaper.supported, true);
    assert.equal(fullPaper.format, 'listening_paper');

    const standaloneSelection = api.reviewDocumentEntrySupport([
        {
            metadata: {
                page_input: { type: '听后选择' },
                doc_type: '听后选择',
            },
        },
    ]);
    assert.equal(standaloneSelection.supported, true);
    assert.equal(standaloneSelection.format, 'listening_selection');

    const imitation = api.reviewDocumentEntrySupport([
        {
            metadata: {
                page_input: { type: '模仿朗读' },
                doc_type: '模仿朗读',
                major_section_profile: 'imitation_boxed_special',
                entry_profile: 'imitation_reading_v1',
                capabilities: { external_input: true },
            },
        },
    ]);
    assert.equal(imitation.supported, true);
    assert.equal(imitation.format, 'imitation_reading');

    const unitSourceImitation = api.reviewDocumentEntrySupport([
        {
            metadata: {
                page_input: {
                    type: '模仿朗读',
                    questions: [{ listening_text: 'A recording script.', score: 6 }],
                },
                doc_type: '模仿朗读',
                source: '外网',
                unit_label: 'U5 · 外网 · 第1稿',
                type_path: ['模仿朗读', '录音稿'],
                major_section_profile: 'imitation_unit_source_special',
                entry_profile: 'imitation_reading_v1',
                capabilities: { external_input: true },
            },
        },
    ]);
    assert.equal(unitSourceImitation.supported, true);
    assert.equal(unitSourceImitation.format, 'imitation_reading');

    const response = api.reviewDocumentEntrySupport([
        {
            metadata: {
                page_input: { type: '听后应答' },
                doc_type: '听后应答',
                major_section_profile: 'response_colored_options_special',
                entry_profile: 'listening_response_v1',
                capabilities: { external_input: true },
            },
        },
    ]);
    assert.equal(response.supported, true);
    assert.equal(response.format, 'listening_response');

    const incompleteResponse = api.reviewDocumentEntrySupport([
        {
            metadata: {
                page_input: { type: '听后应答' },
                doc_type: '听后应答',
            },
        },
    ]);
    assert.equal(incompleteResponse.supported, false);
    assert.equal(incompleteResponse.major_section_profile_invalid_count, 1);
    assert.equal(incompleteResponse.entry_profile_invalid_count, 1);
    assert.equal(incompleteResponse.entry_capability_invalid_count, 1);
    assert.match(incompleteResponse.reason, /听后应答/);

    const recordRetelling = api.reviewDocumentEntrySupport([
        {
            metadata: {
                page_input: { type: '听后记录并转述信息' },
                doc_type: '听后记录并转述信息',
                major_section_profile: 'record_retelling_table_special',
                entry_profile: 'listening_record_retelling_v1',
                capabilities: { external_input: true },
            },
        },
    ]);
    assert.equal(recordRetelling.supported, true);
    assert.equal(recordRetelling.format, 'listening_record_retelling');

    const incompleteRecordRetelling = api.reviewDocumentEntrySupport([
        {
            metadata: {
                page_input: { type: '听后记录并转述信息' },
                doc_type: '听后记录并转述信息',
            },
        },
    ]);
    assert.equal(incompleteRecordRetelling.supported, false);
    assert.equal(incompleteRecordRetelling.entry_profile_invalid_count, 1);
    assert.equal(incompleteRecordRetelling.entry_capability_invalid_count, 1);
    assert.deepEqual(
        JSON.parse(JSON.stringify(incompleteRecordRetelling.expected_types)),
        ['听后记录并转述信息'],
    );

    const legacyImitation = api.reviewDocumentEntrySupport([
        {
            metadata: {
                page_input: { type: '模仿朗读' },
                doc_type: '模仿朗读',
                major_section_profile: 'imitation_legacy_unit_source',
                entry_profile: null,
                capabilities: {
                    parse: true,
                    audio: true,
                    normalize: true,
                    external_input: false,
                },
            },
        },
    ]);
    assert.equal(legacyImitation.supported, false);
    assert.equal(legacyImitation.entry_profile_invalid_count, 1);
    assert.match(legacyImitation.reason, /画像/);

    const legacyCategoryAlias = api.reviewDocumentEntrySupport([
        {
            metadata: {
                page_input: { type: '模仿朗读' },
                category: '模仿朗读-外网',
            },
        },
    ]);
    assert.equal(legacyCategoryAlias.supported, false);
    assert.equal(legacyCategoryAlias.entry_profile_invalid_count, 1);

    const incompleteImitation = api.reviewDocumentEntrySupport([
        {
            metadata: {
                page_input: { type: '模仿朗读' },
                doc_type: '模仿朗读',
                major_section_profile: 'imitation_boxed_special',
                entry_profile: 'imitation_reading_v1',
                capabilities: { external_input: false },
            },
        },
    ]);
    assert.equal(incompleteImitation.supported, false);
    assert.equal(incompleteImitation.entry_capability_invalid_count, 1);
    assert.match(incompleteImitation.reason, /字段尚未确认/);

    const staleUpgrade = api.reviewDocumentEntrySupport([
        {
            metadata: {
                page_input: { type: '模仿朗读' },
                doc_type: '模仿朗读-外网',
                major_section_profile: 'imitation_legacy_unit_source',
                entry_profile: 'imitation_reading_v1',
                capabilities: { external_input: true },
            },
        },
    ]);
    assert.equal(staleUpgrade.supported, false);
    assert.equal(staleUpgrade.major_section_profile_invalid_count, 1);
    assert.match(staleUpgrade.reason, /版式画像/);

    const unsupported = api.reviewDocumentEntrySupport([
        { metadata: { doc_type: '信息获取' } },
    ]);
    assert.equal(unsupported.supported, false);
    assert.equal(unsupported.status, 'unsupported');

    const malformed = api.reviewDocumentEntrySupport([
        { metadata: { page_input: { type: '听后选择' } } },
        { metadata: { page_input: { type: '听后应答' } } },
        { metadata: { page_input: { type: '模仿朗读' } } },
        { metadata: { page_input: { type: '未注册页面类型' } } },
    ]);
    assert.equal(malformed.supported, false);
});

test('文稿视图按页面题目计数，并校准两类专项卷的字段层级', () => {
    const { api } = loadRendererConfigFunctions();
    const responseItems = Array.from({ length: 7 }, (_, index) => ({
        metadata: {
            page_input: {
                type: '听后应答',
                questions: [{ number: index + 9 }],
            },
        },
    }));
    const recordItems = [{
        metadata: {
            page_input: {
                type: '听后记录并转述信息',
                recording: {
                    questions: [{ number: 17 }, { number: 18 }, { number: 19 }],
                },
                retelling: { number: 20 },
            },
        },
    }];

    assert.equal(api.reviewDocumentQuestionCountForItems(responseItems), 7);
    assert.equal(api.reviewDocumentQuestionCountForItems(recordItems), 4);

    const source = readRendererSource();
    assert.match(source, /promptLabel: '听力原文'/);
    assert.match(source, /optionsLabel: '应答语'/);
    assert.match(source, /听力原文与音频复用第一节/);
    assert.match(source, /details\.open = values\.length <= 3/);
    assert.match(source, /renderListeningSelectionFacts/);
    assert.match(source, /renderReviewDocumentOptions/);
    assert.match(source, /review-document-material/);
});

test('文稿核对缺少服务端前置判断时默认关闭，不在前端自行放行', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [
        { metadata: { page_input: { type: '模仿朗读' } } },
    ];

    const missingProjection = api.reviewDocumentEntrySupport(items, {});
    assert.equal(missingProjection.supported, false);
    assert.equal(missingProjection.status, 'unsupported');
    assert.match(missingProjection.reason, /服务端录入结构判断/);

    const explicitlySupported = api.reviewDocumentEntrySupport(items, {
        document_entry_support: {
            schema_version: 'document-entry-preflight-v1',
            supported: true,
            status: 'legacy-value',
        format: 'imitation_reading',
        structured_count: 1,
        invalid_count: 0,
        major_section_profile_invalid_count: 0,
        entry_profile_invalid_count: 0,
            entry_capability_invalid_count: 0,
            detected_types: ['模仿朗读'],
            expected_types: ['模仿朗读'],
        },
    });
    assert.equal(explicitlySupported.supported, false);
    assert.equal(explicitlySupported.status, 'unsupported');

    const currentItems = [{
        metadata: {
            page_input: { type: '模仿朗读' },
            doc_type: '模仿朗读',
            major_section_profile: 'imitation_boxed_special',
            entry_profile: 'imitation_reading_v1',
            capabilities: { external_input: true },
        },
    }];
    const currentProjection = api.reviewDocumentEntrySupport(currentItems, {
        document_entry_support: {
            schema_version: 'document-entry-preflight-v1',
            supported: true,
            status: 'legacy-value',
            format: 'imitation_reading',
            structured_count: 1,
            invalid_count: 0,
            major_section_profile_invalid_count: 0,
            entry_profile_invalid_count: 0,
            entry_capability_invalid_count: 0,
            detected_types: ['模仿朗读'],
            expected_types: ['模仿朗读'],
        },
    });
    assert.equal(currentProjection.supported, true);
    assert.equal(currentProjection.status, 'supported');

    const recordItems = [{
        metadata: {
            page_input: { type: '听后记录并转述信息' },
            doc_type: '听后记录并转述信息',
            major_section_profile: 'record_retelling_table_special',
            entry_profile: 'listening_record_retelling_v1',
            capabilities: { external_input: true },
        },
    }];
    const recordProjection = api.reviewDocumentEntrySupport(recordItems, {
        document_entry_support: {
            schema_version: 'document-entry-preflight-v1',
            supported: true,
            status: 'supported',
            format: 'listening_record_retelling',
            structured_count: 1,
            invalid_count: 0,
            major_section_profile_invalid_count: 0,
            entry_profile_invalid_count: 0,
            entry_capability_invalid_count: 0,
            detected_types: ['听后记录并转述信息'],
            expected_types: ['听后记录并转述信息'],
        },
    });
    assert.equal(recordProjection.supported, true);
    assert.equal(recordProjection.status, 'supported');

    const truncatedProjection = api.reviewDocumentEntrySupport(currentItems, {
        document_entry_support: {
            schema_version: 'document-entry-preflight-v1',
            supported: true,
            status: 'supported',
            format: 'imitation_reading',
            structured_count: 2,
            invalid_count: 0,
            major_section_profile_invalid_count: 0,
            entry_profile_invalid_count: 0,
            entry_capability_invalid_count: 0,
            detected_types: ['模仿朗读'],
            expected_types: ['模仿朗读'],
        },
    });
    assert.equal(truncatedProjection.supported, false);

    const mismatchedProjection = api.reviewDocumentEntrySupport([
        { metadata: { page_input: { type: '听后选择' }, doc_type: '听后选择' } },
    ], {
        document_entry_support: {
            schema_version: 'document-entry-preflight-v1',
            supported: true,
            status: 'supported',
            format: 'listening_paper',
            invalid_count: 0,
            entry_profile_invalid_count: 0,
            entry_capability_invalid_count: 0,
        },
    });
    assert.equal(mismatchedProjection.supported, false);
    assert.match(mismatchedProjection.reason, /版本或条目事实/);
});

test('文稿核对接受没有 page_input 的课文跟读预检', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [
        { metadata: { doc_type: '课文跟读', category: '句子跟读' } },
        { metadata: { doc_type: '课文跟读', category: '段落跟读' } },
    ];
    const support = api.reviewDocumentEntrySupport(items, {
        document_entry_support: {
            schema_version: 'document-entry-preflight-v1',
            supported: true,
            status: 'supported',
            format: 'text_reading',
            structured_count: 0,
            invalid_count: 0,
            major_section_profile_invalid_count: 0,
            entry_profile_invalid_count: 0,
            entry_capability_invalid_count: 0,
            detected_types: [],
            document_types: ['课文跟读'],
            expected_types: [],
        },
    });
    assert.equal(support.supported, true);
    assert.equal(support.status, 'supported');
    assert.equal(support.format, 'text_reading');
    assert.equal(support.supportedCount, 2);

    const mixed = api.reviewDocumentEntrySupport([
        ...items,
        { metadata: { doc_type: '课文跟读', page_input: { type: '听后选择' } } },
    ], {
        document_entry_support: {
            schema_version: 'document-entry-preflight-v1',
            supported: true,
            status: 'supported',
            format: 'text_reading',
            structured_count: 0,
            invalid_count: 0,
            major_section_profile_invalid_count: 0,
            entry_profile_invalid_count: 0,
            entry_capability_invalid_count: 0,
            detected_types: [],
            document_types: ['课文跟读'],
            expected_types: [],
        },
    });
    assert.equal(mixed.supported, false);
    assert.match(mixed.reason, /版本或条目事实/);
});

test('文稿核对拒绝缓存中的旧版 supported true 判断，避免旧页面事实重新打开录入视图', () => {
    const { api } = loadRendererConfigFunctions();
    const stale = api.reviewDocumentEntrySupport([
        { metadata: { page_input: { type: '模仿朗读' }, doc_type: '模仿朗读' } },
    ], {
        document_entry_support: {
            supported: true,
            status: 'supported',
            format: 'imitation_reading',
        },
    });
    assert.equal(stale.supported, false);
    assert.equal(stale.status, 'unsupported');
    assert.match(stale.reason, /版本或条目事实过旧或不完整/);
});

test('系统录入入口复用当前预检，不让旧缓存打开录入配置', () => {
    const { api } = loadRendererConfigFunctions();
    const currentSupport = {
        schema_version: 'document-entry-preflight-v1',
        supported: true,
        status: 'supported',
        format: 'listening_paper',
        structured_count: 4,
        invalid_count: 0,
        major_section_profile_invalid_count: 0,
        entry_profile_invalid_count: 0,
        entry_capability_invalid_count: 0,
        detected_types: ['听后选择', '听后应答', '模仿朗读', '听后记录并转述信息'],
        expected_types: ['听后选择', '听后应答', '模仿朗读', '听后记录并转述信息'],
    };
    const fullPaper = {
        items: [
            { metadata: { page_input: { type: '听后选择' }, doc_type: '听后选择' } },
            { metadata: { page_input: { type: '听后应答' }, doc_type: '听后应答' } },
            {
                metadata: {
            page_input: { type: '模仿朗读' },
            doc_type: '模仿朗读',
            major_section_profile: 'imitation_boxed_special',
            entry_profile: 'imitation_reading_v1',
                    capabilities: { external_input: true },
                },
            },
            { metadata: { page_input: { type: '听后记录并转述信息' }, doc_type: '听后记录并转述信息' } },
        ],
        system_input: {
            available: true,
            document_entry_support: currentSupport,
        },
    };
    assert.equal(api.systemInputDocumentEntryIsSupported(fullPaper), true);

    const stale = {
        items: [{ metadata: { doc_type: '模仿朗读' } }],
        system_input: {
            available: true,
            document_entry_support: {
                supported: true,
                status: 'supported',
                format: 'imitation_reading',
            },
        },
    };
    assert.equal(api.systemInputDocumentEntryIsSupported(stale), false);
});

test('核对页先按录入单元分组，不把两个专项卷压成一个题型根节点', () => {
    const { api } = loadRendererConfigFunctions();
    const groups = api.reviewGroupsWithUnits([{
        doc_type: '模仿朗读',
        items: [
            { unit_id: 'imitation-reading-question-16', unit_label: '第16题专项卷', type_path: ['模仿朗读'], sequence: 0, text: 'A.' },
            { unit_id: 'imitation-reading-question-17', unit_label: '第17题专项卷', type_path: ['模仿朗读'], sequence: 1, text: 'B.' },
        ],
    }]);

    assert.deepEqual(
        JSON.parse(JSON.stringify(groups.map(group => [group.outline_root_kind, group.outline_root_name, group.items.length]))),
        [
            ['unit', '第16题专项卷', 1],
            ['unit', '第17题专项卷', 1],
        ],
    );
    const presentation = api.reviewUnitCountPresentation(groups, {
        input_type: 'paper',
        unit_count_status: 'multiple_confirmed',
        units: [
            {
                unit_id: 'imitation-reading-question-16',
                evidence: {
                    unit_grouping: {
                        candidate_boundaries: [{ first_sequence: 0 }, { first_sequence: 1 }],
                    },
                },
            },
            {
                unit_id: 'imitation-reading-question-17',
                evidence: {
                    unit_grouping: {
                        candidate_boundaries: [{ first_sequence: 0 }, { first_sequence: 1 }],
                    },
                },
            },
        ],
    });
    assert.equal(presentation.label, '已识别多套（2套）');
    assert.equal(presentation.short, '2 套');
    assert.equal(presentation.candidateCount, 2);

    const pendingPresentation = api.reviewUnitCountPresentation(groups, {
        input_type: 'paper',
        unit_count_status: 'multiple_candidate',
        units: [{
            evidence: {
                unit_grouping: {
                    candidate_boundaries: [{ first_sequence: 0 }, { first_sequence: 1 }],
                },
            },
        }],
    });
    assert.equal(pendingPresentation.label, '发现可能多套，待确认');
    assert.equal(pendingPresentation.candidateCount, 2);
    assert.equal(api.buildReviewUnitModels(groups, [
        { groupIndex: 0, reviewIndex: 0, text: 'A.' },
        { groupIndex: 1, reviewIndex: 1, text: 'B.' },
    ]).length, 2);
});

test('模仿朗读文稿视图只在套卷中展示参考答案入口', () => {
    const source = readRendererSource();
    const styles = readRendererStyles();

    assert.match(source, /function reviewImitationReferenceAnswersAllowed\(item\)/);
    assert.match(source, /return examForm === 'paper';/);
    assert.match(
        source,
        /if \(references\.length && reviewImitationReferenceAnswersAllowed\(item\)\)/,
    );
    assert.match(source, /renderReviewReferenceAnswers\(parent, references, '参考答案'\)/);
    assert.match(styles, /\.review-document-reference-details summary::before/);
    assert.match(styles, /\.review-document-reference-details summary::-webkit-details-marker/);
});

test('核对页没有单元证据时仍保持单个默认录入单元', () => {
    const { api } = loadRendererConfigFunctions();
    const presentation = api.reviewUnitCountPresentation([
        { doc_type: '模仿朗读', items: [{ text: 'A.' }, { text: 'B.' }] },
    ], { input_type: 'paper', unit_count_status: 'single_default', units: [{}] });
    assert.equal(presentation.label, '当前按单套组织');
    assert.equal(api.buildReviewUnitModels([
        { doc_type: '模仿朗读', items: [{ text: 'A.' }, { text: 'B.' }] },
    ], [
        { groupIndex: 0, reviewIndex: 0, text: 'A.' },
        { groupIndex: 0, reviewIndex: 1, text: 'B.' },
    ]).length, 1);
});

test('核对页目录按大题型、小题型和具体题目分级且保留大题型条数', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [
        {
            groupIndex: 0,
            reviewIndex: 0,
            type_path: ['信息获取', '听选信息'],
            text: '第一道听选信息题',
        },
        {
            groupIndex: 0,
            reviewIndex: 1,
            type_path: ['信息获取', '听选信息'],
            text: '第二道听选信息题',
        },
        {
            groupIndex: 0,
            reviewIndex: 2,
            type_path: ['信息获取', '回答问题'],
            text: '回答问题题',
        },
    ];
    const model = api.buildReviewOutlineModel([
        { doc_type: '信息获取', item_count: 14, items },
    ], items);

    assert.equal(model.length, 1);
    assert.equal(model[0].name, '信息获取');
    assert.equal(model[0].itemCount, 14);
    assert.equal(api.reviewOutlineReportedCount({ item_count: 1 }, items), 3);
    const visibleOnlyModel = api.buildReviewOutlineModel([
        { doc_type: '信息获取', items },
    ], items.slice(0, 1));
    assert.equal(visibleOnlyModel[0].itemCount, 3);
    assert.deepEqual(
        JSON.parse(JSON.stringify(model[0].children.map(child => [child.name, child.itemCount, child.items.map(item => item.reviewIndex)]))),
        [
            ['听选信息', 2, [0, 1]],
            ['回答问题', 1, [2]],
        ],
    );
});

test('音频核对只有一个录入单元时直接展示题型目录', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [
        { groupIndex: 0, reviewIndex: 0, type_path: ['听后选择'], text: '第一条' },
        { groupIndex: 0, reviewIndex: 1, type_path: ['听后应答'], text: '第二条' },
    ];
    const model = api.buildReviewOutlineModel([
        {
            outline_root_kind: 'unit',
            outline_root_name: '第1套',
            doc_type: '听说测试题',
            item_count: 2,
            items,
        },
    ], items, { flattenSingleUnit: true });

    assert.deepEqual(
        JSON.parse(JSON.stringify(model.map(node => [node.name, node.level, node.itemCount]))),
        [['听后选择', 1, 1], ['听后应答', 1, 1]],
    );
});

test('核对页目录按实际题型路径递归，并默认隐藏具体题目', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [
        {
            groupIndex: 0,
            reviewIndex: 0,
            type_path: ['综合题'],
            text: '没有继续细分题型的内容',
        },
        {
            groupIndex: 0,
            reviewIndex: 1,
            type_path: ['综合题', '阅读理解', '第一篇'],
            text: '第一篇第一题',
        },
        {
            groupIndex: 0,
            reviewIndex: 2,
            type_path: ['综合题', '阅读理解', '第一篇'],
            text: '第一篇第二题',
        },
        {
            groupIndex: 0,
            reviewIndex: 3,
            type_path: ['综合题', '阅读理解', '第二篇'],
            text: '第二篇第一题',
        },
    ];
    const [root] = api.buildReviewOutlineModel([
        { doc_type: '综合题', item_count: 4, items },
    ], items);
    const reading = root.children.find(child => child.name === '阅读理解');
    const firstPassage = reading.children.find(child => child.name === '第一篇');
    const untyped = root.children.find(child => child.name === '未标注小题型');

    assert.equal(root.itemCount, 4);
    assert.deepEqual(
        JSON.parse(JSON.stringify(root.children.map(child => child.name))),
        ['阅读理解', '未标注小题型'],
    );
    assert.deepEqual(
        JSON.parse(JSON.stringify(reading.children.map(child => [child.name, child.itemCount]))),
        [['第一篇', 2], ['第二篇', 1]],
    );
    assert.deepEqual(
        JSON.parse(JSON.stringify(firstPassage.items.map(item => item.reviewIndex))),
        [1, 2],
    );
    assert.deepEqual(
        JSON.parse(JSON.stringify(untyped.items.map(item => item.reviewIndex))),
        [0],
    );
    assert.equal(api.reviewOutlineDefaultExpanded(root), true);
    assert.equal(api.reviewOutlineDefaultExpanded(reading), true);
    assert.equal(api.reviewOutlineDefaultExpanded(firstPassage), false);
    assert.equal(api.reviewOutlineDefaultExpanded(untyped), false);
    assert.equal(api.reviewOutlineFirstItem(root).reviewIndex, 0);
    assert.equal(api.reviewOutlineFirstItem(reading).reviewIndex, 1);
});

test('没有小题型时不伪造目录层级，且大题型默认收起题目', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [
        { groupIndex: 0, reviewIndex: 0, type_path: ['词汇'], text: 'apple' },
        { groupIndex: 0, reviewIndex: 1, type_path: ['词汇'], text: 'book' },
    ];
    const [root] = api.buildReviewOutlineModel([
        { doc_type: '词汇', item_count: 2, items },
    ], items);

    assert.equal(root.children.length, 0);
    assert.deepEqual(
        JSON.parse(JSON.stringify(root.items.map(item => item.reviewIndex))),
        [0, 1],
    );
    assert.equal(api.reviewOutlineDefaultExpanded(root), false);
});

test('核对页按文档全局序号排列，不使用题型内的 number 交错排序', () => {
    const { api } = loadRendererConfigFunctions();
    const ordered = api.reviewItemsInDocumentOrder([
        {
            doc_type: '大题甲',
            items: [
                { item_id: 'a-2', sequence: 2, number: 1, text: '甲题二' },
                { item_id: 'a-4', sequence: 4, number: 2, text: '甲题四' },
            ],
        },
        {
            doc_type: '大题乙',
            items: [
                { item_id: 'b-1', sequence: 1, number: 1, text: '乙题一' },
                { item_id: 'b-3', sequence: 3, number: 2, text: '乙题三' },
            ],
        },
    ]);
    assert.deepEqual(
        JSON.parse(JSON.stringify(ordered.map(item => [item.item_id, item.sequence]))),
        [['b-1', 1], ['a-2', 2], ['b-3', 3], ['a-4', 4]],
    );

    const legacyOrder = api.reviewItemsInDocumentOrder([
        { doc_type: '大题甲', items: [{ item_id: 'a-2', number: 2 }, { item_id: 'a-1', number: 1 }] },
        { doc_type: '大题乙', items: [{ item_id: 'b-1', number: 1 }] },
    ]);
    assert.deepEqual(
        JSON.parse(JSON.stringify(legacyOrder.map(item => item.item_id))),
        ['a-2', 'a-1', 'b-1'],
    );
});

test('泛化内容组会从后续有题型信息的条目推导目录根节点', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [
        { groupIndex: 0, reviewIndex: 0, item_type: 'audio', text: '未标注内容' },
        {
            groupIndex: 0,
            reviewIndex: 1,
            type_path: ['信息获取', '听选信息'],
            text: '听选信息题',
        },
    ];
    const [root] = api.buildReviewOutlineModel([
        { doc_type: 'document', item_count: 2, items },
    ], items);

    assert.equal(root.name, '信息获取');
    assert.deepEqual(
        JSON.parse(JSON.stringify(root.children.map(child => [child.name, child.itemCount]))),
        [['听选信息', 1], ['未标注小题型', 1]],
    );
    assert.equal(api.reviewOutlineDefaultExpanded(root), true);
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

test('版本中心只有在主进程确认当前平台安装包可下载时才显示更新', () => {
    const { api } = loadRendererConfigFunctions();
    assert.equal(api.hasInstallableUpdate({ status: 'available', version: '3.0.1', canDownload: false }), false);
    assert.equal(api.hasInstallableUpdate({ status: 'available', version: '3.0.1', canDownload: true }), true);
    assert.equal(api.hasInstallableUpdate({ status: 'downloading', version: '3.0.1', canDownload: false }), true);
    assert.equal(api.hasInstallableUpdate({ status: 'downloaded', version: '3.0.1', canInstall: true }), true);
    assert.equal(api.hasInstallableUpdate({ status: 'downloaded', version: '3.0.1', canInstall: false }), false);
    assert.equal(api.hasInstallableUpdate({ status: 'up-to-date', version: '3.0.1', canDownload: true }), false);
    assert.equal(api.hasInstallableUpdate({ status: 'available', version: null, canDownload: true }), false);
    assert.equal(api.updateStatusPresentation({ status: 'available', version: '3.0.1' }).code, 'VERIFYING ASSET');
});

test('版本中心有可用更新时使用独立的高对比度强调色', () => {
    const styles = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');
    assert.match(styles, /--release-accent:\s*#b74725/);
    assert.match(styles, /--release-accent-strong:\s*#a63f20/);
    assert.match(styles, /\.version-nav-btn\.has-update/);
    assert.match(styles, /\.version-status-card\.is-info\.has-update/);
    assert.match(styles, /\.version-nav-btn\.has-update \.version-nav-badge[^}]*box-shadow/);
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
    assert.equal(api.workflowSnapshotIsOlder(
        { workflow_id: 'workflow-1' },
        { session_id: 'workflow-1', state_version: 3, latest_seq: 5 },
    ), true);
});

test('工作区刷新后会重新叠加 Store 运行时投影，避免把实时进度刷回聚合值', () => {
    const source = readRendererSource();
    assert.match(source, /function renderWorkspaceAfterHydrate\(workspace, snapshot = null, workflowId = currentSession\?\.session_id\)/);
    assert.match(source, /workflowStore\?\.hydrate\?\.\(effectiveWorkspace, \{ snapshot: effectiveSnapshot \}\);[\s\S]*?renderWorkspaceAfterHydrate\(effectiveWorkspace, effectiveSnapshot, workflowId\);/);
    assert.match(source, /workflowStore\?\.hydrate\?\.\(workspace, \{ snapshot: workspace\.snapshot \}\);[\s\S]*?renderWorkspaceAfterHydrate\(currentWorkspace, currentSession, sessionId\);/);
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

test('生成重试请求开始后隐藏旧异常卡片，只有重试失败才恢复', () => {
    const { api } = loadRendererConfigFunctions();

    assert.equal(api.generationRecoveryIsSuppressed({ generationActive: false, retryInFlight: true }), true);
    assert.equal(api.generationRecoveryIsSuppressed({ generationActive: true, retryInFlight: false }), true);
    assert.equal(api.generationRecoveryIsSuppressed({ generationActive: false, retryInFlight: false }), false);
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

test('合并生成运行期按作品阶段给出进度读数，合成等待按已等待时长爬升', () => {
    const { api } = loadRendererConfigFunctions();

    // 非合并模式（无 total_works）与未知阶段不参与，返回 null。
    assert.equal(api.compositeRuntimeReadout(null), null);
    assert.equal(api.compositeRuntimeReadout({
        stage: 'submitted', total_works: 0, submitted_works: 0,
    }), null);
    assert.equal(api.compositeRuntimeReadout({
        stage: 'processing', item_id: 'item-1', total_works: 3,
    }), null);

    const preparing = api.compositeRuntimeReadout({
        stage: 'preparing', total_works: 1, submitted_works: 0, downloaded_works: 0,
        item_count: 12, elapsed_seconds: 8,
    });
    assert.equal(preparing.percent, 5);
    assert.match(preparing.stats, /正在提交合并作品/);
    assert.equal(preparing.stageLabel, '提交作品');

    const early = api.compositeRuntimeReadout({
        stage: 'submitted', total_works: 1, submitted_works: 1, downloaded_works: 0,
        item_count: 12, elapsed_seconds: 10,
    });
    const late = api.compositeRuntimeReadout({
        stage: 'submitted', total_works: 1, submitted_works: 1, downloaded_works: 0,
        item_count: 12, elapsed_seconds: 3600,
    });
    assert.ok(early.percent >= 12 && early.percent < 40, `起步进度应贴近 12%：${early.percent}`);
    assert.ok(late.percent > early.percent && late.percent <= 85, `长等待应爬升但封顶 85%：${late.percent}`);
    assert.match(early.stats, /合并作品已提交 · 讯飞合成中/);
    assert.match(late.stats, /已等待/);
    assert.equal(early.stageLabel, '讯飞合成中');

    const downloaded = api.compositeRuntimeReadout({
        stage: 'downloaded', total_works: 1, submitted_works: 1, downloaded_works: 1,
        item_count: 12, elapsed_seconds: 400,
    });
    assert.equal(downloaded.percent, 90);
    assert.match(downloaded.stats, /合并音频已下载 · 正在按停顿切割/);
    assert.equal(downloaded.stageLabel, '按停顿切割');

    const saved = api.compositeRuntimeReadout({
        stage: 'saved', total_works: 1, submitted_works: 1, downloaded_works: 1,
        item_count: 12, elapsed_seconds: 401,
    });
    assert.equal(saved.percent, 90);
    assert.equal(saved.stageLabel, '按停顿切割');

    const multiWork = api.compositeRuntimeReadout({
        stage: 'downloaded', total_works: 2, submitted_works: 2, downloaded_works: 1,
        item_count: 6, elapsed_seconds: 400,
    });
    assert.match(multiWork.stats, /作品 1\/2/);
    assert.ok(multiWork.percent > 0 && multiWork.percent < 90);

    const multiWorkAfterLongWait = api.compositeRuntimeReadout({
        stage: 'downloaded', total_works: 10, submitted_works: 10, downloaded_works: 1,
        item_count: 6, elapsed_seconds: 10000,
    });
    const longWaitSubmitted = api.compositeRuntimeReadout({
        stage: 'submitted', total_works: 10, submitted_works: 10, downloaded_works: 0,
        item_count: 6, elapsed_seconds: 10000,
    });
    assert.ok(multiWorkAfterLongWait.percent >= longWaitSubmitted.percent);

    const nextWorkPreparing = api.compositeRuntimeReadout({
        stage: 'preparing', total_works: 2, submitted_works: 1, downloaded_works: 1,
        item_count: 12, elapsed_seconds: 401,
    });
    assert.equal(nextWorkPreparing.percent, multiWork.percent);

    const nextWorkSubmitted = api.compositeRuntimeReadout({
        stage: 'submitted', total_works: 2, submitted_works: 2, downloaded_works: 1,
        item_count: 12, elapsed_seconds: 401,
    });
    assert.equal(nextWorkSubmitted.percent, multiWork.percent);

    const compositeError = api.compositeRuntimeReadout({
        stage: 'error', total_works: 2, submitted_works: 2, downloaded_works: 1,
        item_count: 12, elapsed_seconds: 401,
    });
    assert.match(compositeError.stats, /合并作品需要处理/);
    assert.equal(compositeError.stageLabel, '需要处理');
});

test('Store 渲染器合并阶段优先使用作品计数，不会被 0 分段快照覆盖', () => {
    const { api } = loadRendererConfigFunctions();
    const runtime = api.compositeRuntimeProjection(
        {
            runtime: {
                stage: 'submitted',
                status: 'submitted',
                elapsedSeconds: 42,
            },
            works: { total: 1, submitted: 1, downloaded: 0, items: 12 },
            // 合并模式在真正下载前会把待切割题目数作为 total_segments，
            // 但 completed_segments 仍为 0；这里不能拿它覆盖作品阶段进度。
            segments: { completed: 0, total: 12 },
        },
        {
            runtime: {
                message: '合并作品已提交，讯飞正在合成音频',
                stage: 'submitted',
                elapsed_seconds: 42,
            },
        },
    );

    assert.equal(runtime.total_works, 1);
    assert.equal(runtime.submitted_works, 1);
    assert.equal(runtime.downloaded_works, 0);
    assert.equal(runtime.item_count, 12);
    assert.ok(api.compositeRuntimeReadout(runtime).percent > 0);

    // Full workspace hydration is authoritative if it has already advanced
    // beyond the scalar event projection kept by the store.
    const hydratedRuntime = api.compositeRuntimeProjection(
        {
            runtime: { stage: 'submitted', elapsedSeconds: 10 },
            works: { total: 1, submitted: 1, downloaded: 0, items: 12 },
        },
        {
            runtime: {
                stage: 'downloading', elapsed_seconds: 60, total_works: 1,
                submitted_works: 1, downloaded_works: 1, item_count: 12,
            },
        },
    );
    assert.equal(hydratedRuntime.stage, 'downloading');
    assert.equal(hydratedRuntime.downloaded_works, 1);
    assert.equal(hydratedRuntime.item_count, 12);

    // The server workspace carries the latest runtime event under
    // snapshot.latest_event, not necessarily under snapshot.runtime. A
    // refresh must still reconstruct the same determinate readout.
    const eventRuntime = api.compositeRuntimeProjection({
        snapshot: {
            latest_event: {
                event_type: 'TTS_RUNTIME_STATUS',
                payload: {
                    stage: 'submitted',
                    total_works: 1,
                    submitted_works: 1,
                    elapsed_seconds: 30,
                },
            },
        },
    });
    assert.equal(eventRuntime.stage, 'submitted');
    assert.equal(eventRuntime.total_works, 1);
    assert.equal(api.compositeRuntimeReadout(eventRuntime).percent > 0, true);
});

test('单条模式运行期按分段阶段给出动态进度，不会停在 0 或提前到 100', () => {
    const { api } = loadRendererConfigFunctions();
    const workspace = {
        items: [{ item_id: 'item-1', status: 'RUNNING' }],
    };
    const progress = { total: 10, completed: 2 };
    const submitted = {
        item_id: 'item-1',
        stage: 'submitted',
        status: 'submitted',
        completed_segments: 1,
        total_segments: 1,
        elapsed_seconds: 40,
    };

    assert.equal(api.singleSegmentRuntimeActive(submitted), true);
    const submittedReadout = api.singleSegmentRuntimeReadout(
        submitted,
        progress,
        10,
        workspace,
    );
    assert.ok(submittedReadout.percent > 20 && submittedReadout.percent < 50);
    assert.match(submittedReadout.stats, /已提交 1\/1 段/);

    const savedReadout = api.singleSegmentRuntimeReadout(
        {
            ...submitted,
            stage: 'saved',
            status: 'saved',
        },
        progress,
        10,
        workspace,
    );
    assert.ok(savedReadout.percent > submittedReadout.percent);
    assert.ok(savedReadout.percent < 100);
    assert.match(savedReadout.stats, /已保存 1\/1 段/);

    const readyReadout = api.singleSegmentRuntimeReadout(
        {
            ...submitted,
            stage: 'ready',
            status: 'ready',
        },
        progress,
        10,
        workspace,
    );
    assert.match(readyReadout.stats, /已完成 1\/1 段/);

    const errorReadout = api.singleSegmentRuntimeReadout(
        {
            ...submitted,
            stage: 'error',
            status: 'error',
        },
        progress,
        10,
        workspace,
    );
    assert.match(errorReadout.stats, /需处理 1\/1 段/);
    assert.equal(api.singleSegmentRuntimeActive({
        item_id: '', total_segments: 1, stage: 'submitted',
    }), false);
});

test('未知合并阶段不会回退到分段 100%，心跳也不会覆盖确定性进度', () => {
    const { api } = loadRendererConfigFunctions();
    const source = readRendererSource();

    assert.equal(api.runtimeProgressNeedsIndeterminate({
        snapshot: { runtime: { stage: 'submitted', total_works: 1, elapsed_seconds: 30 } },
    }), false);
    assert.equal(api.runtimeProgressNeedsIndeterminate({
        snapshot: { runtime: { stage: 'cut_error', total_works: 1 } },
    }), true);
    assert.equal(api.runtimeProgressNeedsIndeterminate({
        snapshot: { runtime: { stage: 'future-provider-stage', total_works: 1 } },
    }), true);
    assert.equal(api.runtimeProgressNeedsIndeterminate({
        snapshot: {
            latest_event: {
                event_type: 'TTS_RUNTIME_PROGRESS',
                payload: { stage: 'submitted', total_works: 1, submitted_works: 1 },
            },
        },
    }), false);

    const compositeFallback = source.indexOf('} else if (compositeRuntimeActive) {');
    const segmentFallback = source.indexOf('} else if (hasSegments) {');
    assert.ok(compositeFallback >= 0 && compositeFallback < segmentFallback);
    assert.match(source, /const percent = Math\.min\(99, Math\.round\(\(completed \/ Number\(segments\.total\)\) \* 100\)\);/);
    assert.match(source, /const message = String\(shellSnapshot\?\.runtime\?\.message \|\| workspace\.runtime\?\.message/);
    assert.match(source, /const shellSnapshot = storeState\?\.workflowProjection/);
    assert.match(source, /setProgressIndeterminate\(runtimeProgressNeedsIndeterminate\(\)\)/);
});

test('事件流压缩后仍把 Store 的运行时投影带回生成工作区', () => {
    const { api } = loadRendererConfigFunctions();
    const storeRenderer = api.rendererContext.modules['workflow.storeRenderer'];
    const merged = storeRenderer.mergeStoreRuntimeIntoWorkspace(
        {
            snapshot: { workflow_id: 'workflow-1', latest_seq: 20 },
            works: { total: 1, submitted: 0, downloaded: 0, items: 12 },
        },
        {
            runtime: {
                status: 'waiting',
                stage: 'submitted',
                message: '讯飞浏览器正在处理，任务仍在运行',
                itemId: null,
                elapsedSeconds: 42.6,
            },
            works: { total: 1, submitted: 1, downloaded: 0, items: 12 },
        },
    );

    assert.equal(merged.runtime.stage, 'submitted');
    assert.equal(merged.runtime.elapsedSeconds, 42.6);
    assert.equal(merged.works.total, 1);
    assert.equal(merged.works.submitted, 1);
    assert.equal(merged.works.downloaded, 0);
    assert.equal(merged.works.items, 12);
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
    const source = readRendererSource();
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
    // Git checks out text with platform-specific line endings on some
    // runners. Normalize before inspecting the renderer source so this
    // structural assertion tests the code order rather than CRLF/LF.
    const source = readRendererSource().replace(/\r\n?/g, '\n');
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

test('服务连接期间拖入的文档会被保留，并在连接恢复后自动进入原导入链路', async () => {
    const { api } = loadRendererConfigFunctions();
    const source = readRendererSource();
    const html = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'index.html'), 'utf8');

    assert.equal(api.sourceFileDisplayName({ name: '/tmp/课堂材料.docx' }), '课堂材料.docx');
    assert.deepEqual(JSON.parse(JSON.stringify(api.pendingSourceFilePresentation('课堂材料.docx', 'connecting'))), {
        hint: '正在等待生成服务连接，连接后会自动开始导入。',
        feedback: '已接收 课堂材料.docx，正在等待生成服务连接；连接后会自动导入。',
        status: '等待服务连接：课堂材料.docx',
    });
    assert.deepEqual(JSON.parse(JSON.stringify(api.pendingSourceFilePresentation('课堂材料.docx', 'unavailable'))), {
        hint: '生成服务暂不可用。重试连接成功后会自动开始导入。',
        feedback: '已保留 课堂材料.docx。生成服务暂不可用；点击“重试连接”后会自动导入。',
        status: '等待服务重试：课堂材料.docx',
    });
    assert.deepEqual(JSON.parse(JSON.stringify(api.globalFileDropPresentation('connecting'))), {
        title: '松开后等待服务连接',
        hint: '支持 .docx / .xlsx · 服务连接后会自动导入',
    });
    assert.deepEqual(JSON.parse(JSON.stringify(api.pendingSourceFilePresentation('课堂材料.docx', 'ready', true))), {
        hint: '服务已连接，当前导入完成后会自动开始。',
        feedback: '已接收 课堂材料.docx，当前文档导入完成后会自动开始。',
        status: '等待当前导入：课堂材料.docx',
    });

    const firstDrop = { name: '/tmp/课堂材料.docx' };
    const replacementDrop = { name: '/tmp/课堂材料.docx' };
    api.setSourceImportServiceState('connecting');
    await api.handleIncomingSourceFile(firstDrop);
    assert.strictEqual(api.pendingServiceSourceFile(), firstDrop);
    api.setSourceImportServiceState('unavailable');
    await api.handleIncomingSourceFile(replacementDrop);
    assert.strictEqual(api.pendingServiceSourceFile(), replacementDrop);

    // A connection can recover before a previous import's finalizer has
    // completed. The queued file must remain intact, then start once that
    // finalizer clears the busy flags.
    api.setSourceImportServiceState('ready');
    api.setSourceImportBusy(true);
    assert.equal(api.schedulePendingServiceSourceFileImport(), false);
    assert.strictEqual(api.pendingServiceSourceFile(), replacementDrop);
    api.setSourceImportBusy(false);
    api.setUploadParsing(false);
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.strictEqual(api.pendingServiceSourceFile(), null);

    assert.match(source, /let pendingServiceSourceFile = null;/);
    assert.match(source, /let serviceConnectionAttemptId = 0;/);
    assert.match(source, /if \(!sourceImportServiceIsReady\(\)\) \{\s+queueSourceFileUntilServiceReady\(file\);\s+return;/);
    assert.match(source, /const connectionAttemptId = \+\+serviceConnectionAttemptId;/);
    assert.match(source, /if \(!isCurrentServiceConnectionAttempt\(connectionAttemptId\)\) return false;/);
    assert.match(source, /loadConfig\(\{\s+shouldContinue: \(\) => isCurrentServiceConnectionAttempt\(connectionAttemptId\),\s+\}\);/);
    assert.match(source, /async function loadConfig\(\{ shouldContinue = \(\) => true \} = \{\}\) \{/);
    assert.match(source, /sourceImportServiceState = 'ready';\s+setServiceState\('ready', '服务已连接'\);\s+setAppInteractive\(true\);\s+schedulePendingServiceSourceFileImport\(\);/);
    assert.match(source, /function schedulePendingServiceSourceFileImport\(\) \{[\s\S]*?Promise\.resolve\(\)\.then/);
    assert.match(source, /if \(!active\) schedulePendingServiceSourceFileImport\(\);/);
    assert.match(source, /async function cancelSourceImport\(\) \{[\s\S]*?if \(!pendingServiceSourceFile\) return;[\s\S]*?已取消等待/);
    assert.match(html, /id="global-drop-title"/);
    assert.match(html, /id="global-drop-hint"/);

    const zoneDropStart = source.indexOf("uploadZone.addEventListener('drop'");
    const globalDropStart = source.indexOf("window.addEventListener('dragenter'", zoneDropStart);
    assert.ok(zoneDropStart >= 0 && globalDropStart > zoneDropStart);
    const zoneDropHandler = source.slice(zoneDropStart, globalDropStart);
    assert.doesNotMatch(zoneDropHandler, /aria-disabled/);
    assert.match(zoneDropHandler, /void handleIncomingSourceFile\(file\);/);
});

test('返回配置会为已终止任务创建新的可编辑工作流', () => {
    const source = readRendererSource();
    assert.match(source, /async function createEditableWorkflowFromTerminal\(session, snapshot\)/);
    assert.match(source, /renderer-return-config-rerun-/);
    assert.match(source, /renderer-rerun-\$\{session\.session_id\}-\$\{expectedGroupStateVersion\}/);
    assert.match(source, /if \(isTerminalWorkflowSnapshot\(snapshot\)\) \{\s+const nextWorkspace = await createEditableWorkflowFromTerminal\(session, snapshot\)/s);
    assert.match(source, /session = currentSession;\s+snapshot = nextWorkspace\.snapshot;/s);
});

test('生成页默认展开任务时间线，并忽略结束后的迟到进度事件', () => {
    const source = readRendererSource();
    const html = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'index.html'), 'utf8');

    assert.doesNotMatch(html, /generation-v2-layout is-log-collapsed/);
    assert.doesNotMatch(html, /generation-v2-log is-collapsed/);
    assert.match(html, /id="log-toggle-btn"[^>]*aria-expanded="true"[^>]*>收起详情<\/button>/);
    assert.match(source, /setLogDetailsExpanded\(true\)/);
    assert.match(source, /if \(!isGenerating \|\| generationResult !== null\) return;/);
});

test('生成页状态文案以暂停控制态为准，不会被旧的运行中消息覆盖', () => {
    const { api } = loadRendererConfigFunctions();
    const source = readRendererSource();
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
    const source = readRendererSource();
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

test('内容核对按文稿能力切换视图，不支持时只展示音频核对', () => {
    const source = readRendererSource();
    const styles = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');

    assert.match(source, /let reviewViewMode = 'document'/);
    assert.match(source, /dataset\.reviewView = mode/);
    assert.match(source, /reviewGroupsWithUnits\(groups\)/);
    assert.match(source, /renderReviewDocumentView\(\s*unitModels,\s*unitPresentation,\s*entrySupport/);
    assert.match(source, /\['document', '文稿核对'/);
    assert.match(source, /\['audio', '音频核对'/);
    assert.match(source, /documentTab\.hidden = !canShowDocument/);
    assert.match(source, /documentView\.hidden = mode !== 'document' \|\| !canShowDocument/);
    assert.match(source, /documentView\.setAttribute\('aria-hidden', documentView\.hidden \? 'true' : 'false'\)/);
    assert.match(source, /audioView\.setAttribute\('aria-hidden', audioView\.hidden \? 'true' : 'false'\)/);
    assert.match(source, /switcher\.classList\.toggle\('is-audio-only', !canShowDocument\)/);
    assert.match(source, /switcherTitle\.textContent = canShowDocument \? '内容核对视图' : '音频核对视图'/);
    assert.match(source, /return mode;/);
    assert.match(source, /const activeReviewViewMode = applyReviewViewMode\(entrySupport\.supported === true\);/);
    assert.match(source, /const isAudioView = activeReviewViewMode === 'audio';/);
    assert.match(source, /const entrySupport = reviewDocumentEntrySupport\(/);
    assert.match(source, /const reviewShell = ensureReviewViewShell\(\);/);
    assert.match(styles, /\.review-view-tabs \{/);
    assert.match(styles, /\.review-document-view \{/);
    assert.match(styles, /\.review-unit-status \{/);
});

test('长内容的内容目录为固定操作栏预留底部滚动空间', () => {
    const rendererSource = readRendererSource();
    const styles = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');

    assert.match(styles, /--review-actions-clearance:\s*76px;/);
    assert.match(styles, /\.review-document-nav \{[^}]*padding: 12px 11px calc\(13px \+ var\(--review-actions-clearance\)\);[^}]*scroll-padding-bottom: var\(--review-actions-clearance\);/);
    assert.match(styles, /\.review-document-nav \{[^}]*max-height: min\(620px, var\(--review-scroll-viewport-height, calc\(100dvh - 150px - var\(--review-actions-clearance\)\)\)\);/);
    assert.match(styles, /\.review-outline-panel \{[^}]*height: min\(620px, var\(--review-scroll-viewport-height, calc\(100dvh - 150px - var\(--review-actions-clearance\)\)\)\);[^}]*max-height: var\(--review-scroll-viewport-height, calc\(100dvh - 150px - var\(--review-actions-clearance\)\)\);/);
    assert.match(styles, /\.review-outline \{[^}]*padding-bottom: var\(--review-actions-clearance\);[^}]*scroll-padding-bottom: var\(--review-actions-clearance\);/);
    assert.match(styles, /\.review-items \{[^}]*padding-bottom: var\(--review-actions-clearance\);[^}]*scroll-padding-bottom: var\(--review-actions-clearance\);/);
    assert.match(styles, /\.review-inspector \{[\s\S]*height: min\(620px, var\(--review-scroll-viewport-height, calc\(100dvh - 150px - var\(--review-actions-clearance\)\)\)\);[\s\S]*max-height: var\(--review-scroll-viewport-height, calc\(100dvh - 150px - var\(--review-actions-clearance\)\)\);[\s\S]*padding-right: 20px;[\s\S]*padding-bottom: calc\(20px \+ var\(--review-actions-clearance\)\);[\s\S]*scroll-padding-bottom: var\(--review-actions-clearance\);/);
    assert.match(styles, /\.review-inspector \{ padding: 14px; padding-bottom: calc\(14px \+ var\(--review-actions-clearance\)\); \}/);
    assert.match(rendererSource, /syncReviewPanelViewport/);
    assert.match(rendererSource, /getBoundingClientRect\(\)/);
    assert.match(rendererSource, /window\.innerHeight - clearance - rect\.top/);
    assert.match(rendererSource, /scrollPage\?\.addEventListener\('scroll', scheduleReviewPanelViewportSync/);
});

test('文稿核对只在存在音频产物时展示音频绑定', () => {
    const source = readRendererSource();

    assert.match(source, /const audioId = reviewDocumentItemValue\(item, \['audio_artifact_id', 'audioArtifactId'\]\);/);
    assert.match(source, /if \(audioId\) appendFact\('音频绑定', `已绑定 · \$\{audioId\}`\);/);
    assert.doesNotMatch(source, /appendFact\('音频绑定', audioId \? `已绑定 · \$\{audioId\}` : '尚未绑定音频'\);/);
});

test('文稿视图按课文子类型、段落边界和自动识别形式展示校对分类', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [
        {
            doc_type: '课文跟读',
            item_type: '语篇跟读',
            metadata: {
                category: '语篇跟读',
                paragraph_id: 'article-1-paragraph-1',
                paragraph_title: '',
            },
        },
        {
            doc_type: '课文跟读',
            item_type: '语篇跟读',
            metadata: {
                category: '语篇跟读',
                paragraph_id: 'article-1-paragraph-2',
                paragraph_title: 'The watermelon farmer',
            },
        },
        {
            doc_type: '课文跟读',
            item_type: '语篇跟读',
            metadata: {
                category: '语篇跟读',
                paragraph_id: 'article-1-paragraph-2',
                paragraph_title: 'The watermelon farmer',
            },
        },
        { doc_type: '课文跟读', item_type: '句子跟读', metadata: { category: '句子跟读' } },
    ];
    const groups = JSON.parse(JSON.stringify(api.buildReviewTypeGroups(items)));
    assert.deepEqual(groups.map(group => group.name), ['语篇跟读', '句子跟读']);

    const discourse = groups[0];
    const paragraphs = JSON.parse(JSON.stringify(api.reviewTextbookParagraphGroups(discourse)));
    assert.deepEqual(paragraphs.map(paragraph => paragraph.title), ['', 'The watermelon farmer']);
    assert.deepEqual(paragraphs.map(paragraph => paragraph.items.length), [1, 2]);
    assert.equal(api.reviewTextbookFormForGroup(discourse), '段落');
    assert.equal(api.reviewTextbookFormForGroup({
        name: '语篇跟读',
        items: [items[0]],
    }), '同步课文');
    assert.equal(
        api.reviewTextbookFormForGroup(
            { name: '语篇跟读', items: [items[0]] },
            { unit_id: 'unit-on-the-fast-track', label: 'Section B · On the Fast Track' },
            {
                units: [{
                    unit_id: 'unit-on-the-fast-track',
                    label: 'Section B · On the Fast Track',
                    evidence: { textbook_form: '段落' },
                }],
            },
        ),
        '段落',
    );
    assert.equal(
        api.reviewTextbookFormForGroup(
            {
                name: '语篇跟读',
                items: [{
                    ...items[0],
                    metadata: { ...items[0].metadata, entry_form: '角色扮演' },
                }],
            },
            { unit_id: 'unit-on-the-fast-track' },
            { units: [{ unit_id: 'unit-on-the-fast-track', evidence: { textbook_form: '段落' } }] },
        ),
        '段落',
    );
    assert.equal(
        api.reviewTextbookFormForGroup(
            { name: '语篇跟读', items: [{ ...items[0], role: 'Reporter' }] },
            { unit_id: 'unit-on-the-fast-track' },
            { units: [{ unit_id: 'unit-on-the-fast-track', evidence: { textbook_form: '段落' } }] },
        ),
        '角色扮演',
    );
    assert.equal(api.reviewTextbookFormForGroup(groups[1]), '角色扮演');

    const source = readRendererSource();
    const styles = readRendererStyles();
    assert.match(source, /review-document-paragraph-heading/);
    assert.match(source, /review-document-classification-tag/);
    assert.match(styles, /\.review-document-paragraph \{/);
    assert.match(styles, /\.review-document-classification-tag\.is-form/);
});

test('多单元录入目标保留切换兼容逻辑，并区分持久草稿与已保存设置', () => {
    const source = readRendererSource();
    const html = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'index.html'), 'utf8');
    const styles = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');

    assert.match(html, /id="system-input-unit-overview"[^>]*role="list"/);
    assert.match(html, /id="system-input-unit-progress"/);
    assert.match(html, /点击下方卡片切换要编辑的录入单元/);
    assert.doesNotMatch(html, /id="system-input-unit-search"/);
    assert.doesNotMatch(source, /bindSystemInputPicker\('system-input-unit-search'/);
    assert.match(html, /id="system-input-app-template-scope-field"[^>]*hidden/);
    assert.match(html, /data-system-input-choice-group="system-input-app-template-scope"/);
    assert.match(html, /id="system-input-platform-template-search"[^>]*readonly/);
    assert.match(html, /placeholder="选择平台题型模板"/);
    assert.match(source, /selectionOnly: true/);
    assert.doesNotMatch(source, /应用到当前单元/);
    assert.match(source, /应用到勾选条目/);
    assert.match(source, /应用到全部单元/);
    assert.match(source, /systemInputAppTemplateApplyScopeValue/);
    assert.match(source, /systemInputAppTemplateTargetUnits/);
    assert.match(source, /updateSystemInputAppTemplateAction\(systemInput, \{ resetScope: true \}\)/);
    assert.match(source, /title: '同步当前配置到全部单元？'/);
    assert.match(source, /受影响：\$\{targetLabels\.join\('、'\)\}/);
    assert.match(source, /systemInputSelectedUnitId = nextId;[\s\S]{0,500}renderSystemInputUnitPicker\(systemInput\);/);
    assert.match(source, /openSystemInputConfigDrawer\(currentWorkspace, \{[\s\S]{0,220}deliveryMode: systemInputVisibleDeliveryMode\(currentWorkspace\)/);
    assert.match(source, /if \(draft && !draft\.committed\) \{[\s\S]{0,500}if \(requestedMode\) setSystemInputField\('system-input-delivery-mode', requestedMode\);/);
    assert.match(source, /configuration\.delivery_mode = 'audio_and_input'/);
    assert.match(source, /configButton\.textContent = inputEnabled \? '编辑录入目标' : '设置并开启录入'/);
    assert.match(source, /return systemInputNormalizeUnitConfiguration\(\s*systemInputUnitConfiguration\(unit, systemInput\),/);
    assert.match(html, /选择要编辑的单元；名称和题型模板按单元独立保存；应用模板可选勾选条目或全部单元/);
    assert.match(styles, /\.system-input-unit-overview \{/);
    assert.match(styles, /\.system-input-unit-overview-button\[aria-current="true"\]/);
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
    const source = readRendererSource();
    assert.match(source, /提交未完成，可重新生成/);
    assert.match(source, /ambiguous: false/);
    assert.doesNotMatch(source, /先确认未提交后再重试/);
});

test('试听媒体发生错误时会清理旧 Blob URL，后续点击可重新取 Artifact', () => {
    const source = readRendererSource();
    assert.match(source, /let audioObjectUrl = null/);
    assert.match(source, /artifactObjectUrls\.delete\(audioObjectUrl\)/);
    assert.match(source, /audioReadyPromise = null/);
    assert.match(source, /audio\.addEventListener\('error', \(\) => \{\s*\/\/ A successful ticket read/s);
});

test('从系统录入子页返回音频交付时重建播放器和波形资源', () => {
    const source = readRendererSource();

    assert.match(source, /function rebuildAudioDeliveryPageForReturn\([\s\S]*?buildResultPage\(event, context\);/);
    assert.match(source, /const returningFromTaskSubpage = step === 4 && isSystemInputSubpageView\(\);[\s\S]*?rebuildAudioDeliveryPageForReturn\(\{ historyResult: returningHistoryResult \}\);/);
    assert.match(source, /function returnFromTaskSubpage\(\) \{[\s\S]*?activateStandalonePage\('page-4', 'history-result'\);[\s\S]*?rebuildAudioDeliveryPageForReturn\(\{ historyResult: true \}\);[\s\S]*?activateResultWaveforms/);
});

test('Renderer 会在 contextBridge-safe Artifact 事件桥上重建可消费的 ReadableStream', async () => {
    const { api } = loadRendererConfigFunctions();
    let dataListener;
    let endListener;
    let ackCount = 0;
    let closeCount = 0;
    const transport = {
        onMetadata() { return () => {}; },
        onData(listener) { dataListener = listener; return () => {}; },
        onEnd(listener) { endListener = listener; return () => {}; },
        onError() { return () => {}; },
        ack() { ackCount += 1; return Promise.resolve(true); },
        close() { closeCount += 1; return Promise.resolve(true); },
    };

    const stream = api.rendererReadableArtifactStream(transport);
    const reader = stream.getReader();
    dataListener(new Uint8Array([7, 8]));
    assert.deepEqual(Array.from((await reader.read()).value), [7, 8]);
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(ackCount, 1);
    endListener();
    assert.equal((await reader.read()).done, true);
    assert.equal(closeCount, 1);
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

test('活动任务进度条按已完成条目计算，不把失败映射成 99%', () => {
    const { api } = loadRendererConfigFunctions();

    assert.equal(api.generationProgressPercentForView(
        { terminal: false, freezeProgress: false },
        { completed: 0, failed: 1, percent: 99 },
        1,
    ), 0);
    assert.equal(api.generationProgressPercentForView(
        { terminal: false, freezeProgress: false },
        { completed: 1, failed: 1, percent: 99 },
        2,
    ), 50);
});

test('交付文件名和 ZIP 建议名始终带服务端格式后缀', () => {
    const { api } = loadRendererConfigFunctions();

    assert.equal(api.filenameWithExtension('001', 'mp3'), '001.mp3');
    assert.equal(api.filenameWithExtension('001.mp3', 'mp3'), '001.mp3');
    assert.equal(api.deliveryZipFilename('七上 Starter.docx'), '七上 Starter_tts.zip');
    assert.equal(api.deliveryZipFilename('无扩展名文档'), '无扩展名文档_tts.zip');
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

test('结果页使用条目 metadata 展示题型和分类，避免把 item_type 重复展示', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [{
        item_id: 'item-display',
        item_type: 'question',
        status: 'SUCCEEDED',
        sequence: 0,
        normalized_content: '原文',
    }];
    const artifacts = [{
        artifact_id: 'artifact-display',
        item_id: 'item-display',
        artifact_type: 'tts-segment',
        lifecycle_state: 'READY',
        verified: true,
        created_at: '2026-01-01T00:00:00Z',
    }];
    const workspace = {
        items: [{
            ...items[0],
            metadata: { doc_type: '模仿朗读', category: '框内英文' },
            role: 'student',
            voice_key: 'speaker:linda',
        }],
        artifacts: [{
            ...artifacts[0],
            filename: '001.mp3',
            format: 'mp3',
            mime_type: 'audio/mpeg',
            size_bytes: 12,
            sha256: 'a'.repeat(64),
        }],
    };

    const [file] = api.resultFilesFromArtifacts(items, artifacts, workspace);
    assert.equal(file.doc_type, '模仿朗读');
    assert.equal(file.category, '框内英文');
    assert.equal(file.role, 'student');
    assert.equal(file.voice_key, 'speaker:linda');
});

test('结果页只有 item_type 时只展示一次条目类型', () => {
    const { api } = loadRendererConfigFunctions();
    const [file] = api.resultFilesFromArtifacts(
        [{ item_id: 'item-legacy', item_type: '句子跟读', status: 'SUCCEEDED', sequence: 0 }],
        [{
            artifact_id: 'artifact-legacy',
            item_id: 'item-legacy',
            artifact_type: 'tts-segment',
            lifecycle_state: 'READY',
            verified: true,
            filename: '001.mp3',
            format: 'mp3',
            mime_type: 'audio/mpeg',
            size_bytes: 8,
            sha256: 'b'.repeat(64),
        }],
    );
    assert.equal(file.doc_type, '句子跟读');
    assert.equal(file.category, '');
});

test('结果页从已接受工作区配置补齐实际使用的默认音色', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [{
        item_id: 'item-voice-fallback',
        item_type: '句子',
        status: 'SUCCEEDED',
        sequence: 0,
        normalized_content: 'hello',
        role: null,
        voice_key: null,
    }];
    const workspace = {
        items,
        configuration: {
            effective: {
                default_female_voice: 'speaker:linda',
                default_male_voice: 'speaker:steve',
                role_voices: {},
            },
        },
        artifacts: [{
            artifact_id: 'artifact-voice-fallback',
            item_id: 'item-voice-fallback',
            artifact_type: 'tts-segment',
            lifecycle_state: 'READY',
            verified: true,
            filename: '001.mp3',
            format: 'mp3',
            mime_type: 'audio/mpeg',
            size_bytes: 8,
            sha256: 'c'.repeat(64),
        }],
    };

    const [file] = api.resultFilesFromArtifacts(items, [], workspace);
    assert.equal(file.voice_key, 'speaker:linda');
});

test('结果页按条目实际性别选择默认音色，不把解析默认值和实际音色叠加', () => {
    const { api } = loadRendererConfigFunctions();
    api.setVoiceCatalog([
        { key: 'speaker:linda', name: '英语-Linda' },
        { key: 'speaker:steve', name: '英语-Steve' },
    ]);
    const items = [{
        item_id: 'item-male-question',
        item_type: '听选信息题目',
        status: 'SUCCEEDED',
        sequence: 0,
        normalized_content: 'How many subjects does Mary have at school?',
        metadata: { voice: 'male' },
        voice_key: null,
    }];
    const workspace = {
        items,
        configuration: {
            effective: {
                default_female_voice: 'speaker:linda',
                default_male_voice: 'speaker:steve',
                role_voices: {},
            },
        },
        artifacts: [{
            artifact_id: 'artifact-male-question',
            item_id: 'item-male-question',
            artifact_type: 'tts-segment',
            lifecycle_state: 'READY',
            verified: true,
            filename: '听选信息题目-1.mp3',
            format: 'mp3',
            mime_type: 'audio/mpeg',
            size_bytes: 8,
            sha256: 'e'.repeat(64),
        }],
    };

    const [file] = api.resultFilesFromArtifacts(items, [], workspace);
    assert.equal(file.voice_key, 'speaker:steve');
    assert.deepEqual(JSON.parse(JSON.stringify(file.voice_keys)), ['speaker:steve']);

    // 旧工作区可能同时保存了错误的默认女声和带 M 标记的正文；正文中
    // 的实际标记优先，但不能再把 WorkItem 默认值拼成第二种音色。
    const stale = api.resultVoiceKeysForItem({
        normalized_content: 'M: How many subjects does Mary have at school?',
        metadata: { voice: 'male' },
        voice_key: 'speaker:linda',
    }, workspace);
    assert.deepEqual(JSON.parse(JSON.stringify(stale)), ['speaker:steve']);
});

test('结果页不会把旧 Artifact 音色数组叠加到已接受的单一性别音色', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [{
        item_id: 'item-stale-artifact-voice',
        item_type: '听选信息题目',
        status: 'SUCCEEDED',
        sequence: 0,
        normalized_content: 'How many subjects does Mary have at school?',
        metadata: { voice: 'male' },
    }];
    const artifacts = [{
        artifact_id: 'artifact-stale-artifact-voice',
        item_id: 'item-stale-artifact-voice',
        artifact_type: 'tts-segment',
        lifecycle_state: 'READY',
        verified: true,
        filename: '听选信息题目-1.mp3',
        format: 'mp3',
        mime_type: 'audio/mpeg',
        size_bytes: 8,
        sha256: '1'.repeat(64),
        voice_keys: ['speaker:linda', 'speaker:steve'],
    }];
    const workspace = {
        items,
        configuration: {
            effective: {
                default_female_voice: 'speaker:linda',
                default_male_voice: 'speaker:steve',
                role_voices: {},
            },
        },
        artifacts,
    };

    const [file] = api.resultFilesFromArtifacts(items, artifacts, workspace);
    assert.deepEqual(JSON.parse(JSON.stringify(file.voice_keys)), ['speaker:steve']);
});

test('结果页兼容旧记录时保留 Artifact 的真实音色数组', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [{
        item_id: 'item-legacy-voice-facts',
        item_type: '听后选择',
        status: 'SUCCEEDED',
        sequence: 0,
        normalized_content: 'A legacy recording without frozen configuration.',
    }];
    const artifacts = [{
        artifact_id: 'artifact-legacy-voice-facts',
        item_id: 'item-legacy-voice-facts',
        artifact_type: 'tts-segment',
        lifecycle_state: 'READY',
        verified: true,
        filename: '001.mp3',
        format: 'mp3',
        mime_type: 'audio/mpeg',
        size_bytes: 8,
        sha256: '2'.repeat(64),
        voice_keys: ['speaker:linda', 'speaker:steve'],
    }];

    const [file] = api.resultFilesFromArtifacts(items, artifacts);
    assert.deepEqual(JSON.parse(JSON.stringify(file.voice_keys)), ['speaker:linda', 'speaker:steve']);
});

test('结果页不会把正在水合的空配置误判为已接受配置', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [{
        item_id: 'item-empty-effective-config',
        item_type: '听后选择',
        status: 'SUCCEEDED',
        sequence: 0,
        normalized_content: 'A legacy recording while the workspace is hydrating.',
    }];
    const artifacts = [{
        artifact_id: 'artifact-empty-effective-config',
        item_id: 'item-empty-effective-config',
        artifact_type: 'tts-segment',
        lifecycle_state: 'READY',
        verified: true,
        filename: '001.mp3',
        format: 'mp3',
        mime_type: 'audio/mpeg',
        size_bytes: 8,
        sha256: '3'.repeat(64),
        voice_keys: ['speaker:linda', 'speaker:steve'],
    }];

    const [file] = api.resultFilesFromArtifacts(items, artifacts, {
        items,
        artifacts,
        configuration: { effective: {} },
    });
    assert.deepEqual(JSON.parse(JSON.stringify(file.voice_keys)), ['speaker:linda', 'speaker:steve']);
});

test('结果页不会把默认音色字段为空的旧配置当成已接受配置', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [{
        item_id: 'item-null-effective-voices',
        item_type: '听后选择',
        status: 'SUCCEEDED',
        sequence: 0,
        normalized_content: 'An old record with an incomplete configuration projection.',
    }];
    const artifacts = [{
        artifact_id: 'artifact-null-effective-voices',
        item_id: 'item-null-effective-voices',
        artifact_type: 'tts-segment',
        lifecycle_state: 'READY',
        verified: true,
        filename: '001.mp3',
        format: 'mp3',
        mime_type: 'audio/mpeg',
        size_bytes: 8,
        sha256: '5'.repeat(64),
        voice_keys: ['speaker:legacy-one', 'speaker:legacy-two'],
    }];

    const [file] = api.resultFilesFromArtifacts(items, artifacts, {
        items,
        artifacts,
        configuration: {
            effective: {
                default_female_voice: null,
                default_male_voice: null,
                role_voices: {},
            },
        },
    });
    assert.deepEqual(
        JSON.parse(JSON.stringify(file.voice_keys)),
        ['speaker:legacy-one', 'speaker:legacy-two'],
    );
});

test('历史原文带 M/W 标记但没有已接受配置时保留 Artifact 的真实音色', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [{
        item_id: 'item-legacy-marked-content',
        item_type: '听后选择',
        status: 'SUCCEEDED',
        sequence: 0,
        normalized_content: 'W: Are you ready?\nM: Yes, I am.',
    }];
    const artifacts = [{
        artifact_id: 'artifact-legacy-marked-content',
        item_id: 'item-legacy-marked-content',
        artifact_type: 'tts-segment',
        lifecycle_state: 'READY',
        verified: true,
        filename: '001.mp3',
        format: 'mp3',
        mime_type: 'audio/mpeg',
        size_bytes: 8,
        sha256: '4'.repeat(64),
        voice_keys: ['speaker:legacy-woman', 'speaker:legacy-man'],
    }];
    const workspace = {
        items,
        artifacts,
        configuration: { effective: {} },
    };

    assert.deepEqual(
        JSON.parse(JSON.stringify(api.resultVoiceKeysForItem(items[0], workspace))),
        [],
    );
    const [file] = api.resultFilesFromArtifacts(items, artifacts, workspace);
    assert.deepEqual(
        JSON.parse(JSON.stringify(file.voice_keys)),
        ['speaker:legacy-woman', 'speaker:legacy-man'],
    );
    assert.equal(file.voice_key, 'speaker:legacy-woman');
});

test('没有已接受配置且没有具体音色事实时不凭空生成默认音色', () => {
    const { api } = loadRendererConfigFunctions();
    assert.deepEqual(
        JSON.parse(JSON.stringify(api.resultVoiceKeysForItem({
            normalized_content: 'M: An old question without a frozen voice.',
        }, { configuration: { effective: {} } }))),
        [],
    );
    assert.equal(
        api.resultVoiceKeyFromAcceptedConfiguration(
            { normalized_content: 'An unmarked old question.' },
            { configuration: { effective: {} } },
        ),
        '',
    );
});

test('结果页保持角色指定音色优先于解析器性别默认槽位', () => {
    const { api } = loadRendererConfigFunctions();
    const workspace = {
        configuration: {
            effective: {
                default_female_voice: 'speaker:linda',
                default_male_voice: 'speaker:steve',
                role_voices: { teacher: 'speaker:teacher' },
            },
        },
    };
    assert.equal(
        api.resultVoiceKeyFromAcceptedConfiguration({
            role: 'Teacher',
            metadata: { voice: 'male' },
        }, workspace),
        'speaker:teacher',
    );
});

test('结果页使用持久化题目序号排序，即使工作区兼容行缺少 sequence', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [
        { item_id: 'item-2', item_type: '听后选择', status: 'SUCCEEDED', sequence: 2, normalized_content: 'third' },
        { item_id: 'item-0', item_type: '听后选择', status: 'SUCCEEDED', sequence: 0, normalized_content: 'first' },
        { item_id: 'item-1', item_type: '听后选择', status: 'SUCCEEDED', sequence: 1, normalized_content: 'second' },
    ];
    const artifacts = [
        { artifact_id: 'artifact-2', item_id: 'item-2', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, created_at: '2026-01-01T00:00:03Z' },
        { artifact_id: 'artifact-0', item_id: 'item-0', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, created_at: '2026-01-01T00:00:01Z' },
        { artifact_id: 'artifact-1', item_id: 'item-1', artifact_type: 'tts-segment', lifecycle_state: 'READY', verified: true, created_at: '2026-01-01T00:00:02Z' },
    ];
    const workspace = {
        items: items.map(item => ({ item_id: item.item_id, status: item.status })),
        artifacts: [
            { ...artifacts[0], filename: '听后选择-3.mp3', format: 'mp3', mime_type: 'audio/mpeg', size_bytes: 8, sha256: 'f'.repeat(64) },
            { ...artifacts[1], filename: '听后选择-1.mp3', format: 'mp3', mime_type: 'audio/mpeg', size_bytes: 8, sha256: 'a'.repeat(64) },
            { ...artifacts[2], filename: '听后选择-2.mp3', format: 'mp3', mime_type: 'audio/mpeg', size_bytes: 8, sha256: 'b'.repeat(64) },
        ],
    };

    const files = api.resultFilesFromArtifacts(items, artifacts, workspace);
    assert.deepEqual(
        JSON.parse(JSON.stringify(files.map(file => [file.filename, file.sequence]))),
        [
            ['听后选择-1.mp3', 0],
            ['听后选择-2.mp3', 1],
            ['听后选择-3.mp3', 2],
        ],
    );
});

test('结果页忽略同题目的非 TTS 产物，不遮蔽有效音频', () => {
    const { api } = loadRendererConfigFunctions();
    const items = [{ item_id: 'item-with-parse-output', item_type: '听后选择', status: 'SUCCEEDED', sequence: 0 }];
    const artifacts = [
        {
            artifact_id: 'parse-output-newer',
            item_id: 'item-with-parse-output',
            artifact_type: 'parse-output',
            created_at: '2026-01-02T00:00:00Z',
        },
        {
            artifact_id: 'tts-audio',
            item_id: 'item-with-parse-output',
            artifact_type: 'tts-segment',
            lifecycle_state: 'READY',
            verified: true,
            created_at: '2026-01-01T00:00:00Z',
            filename: '听后选择-1.mp3',
            format: 'mp3',
            mime_type: 'audio/mpeg',
            size_bytes: 8,
            sha256: '1'.repeat(64),
        },
    ];

    const [file] = api.resultFilesFromArtifacts(items, artifacts);
    assert.equal(file.artifact_id, 'tts-audio');
    assert.equal(file.filename, '听后选择-1.mp3');
});

test('交付页从录音稿汇总一段音频实际使用的多个音色', () => {
    const { api } = loadRendererConfigFunctions();
    api.setVoiceCatalog([
        { key: 'speaker:linda', name: '英语-Linda' },
        { key: 'speaker:steve', name: '英语-Steve' },
    ]);
    const items = [{
        item_id: 'item-dialogue',
        item_type: '听后选择',
        status: 'SUCCEEDED',
        sequence: 0,
        normalized_content: 'W: Are you Tom Black?\nM: No, I\'m Jack Green.',
        voice_key: 'speaker:linda',
    }];
    const workspace = {
        items,
        configuration: {
            effective: {
                default_female_voice: 'speaker:linda',
                default_male_voice: 'speaker:steve',
                role_voices: {},
            },
        },
        artifacts: [{
            artifact_id: 'artifact-dialogue',
            item_id: 'item-dialogue',
            artifact_type: 'tts-segment',
            lifecycle_state: 'READY',
            verified: true,
            filename: '001.mp3',
            format: 'mp3',
            mime_type: 'audio/mpeg',
            size_bytes: 8,
            sha256: 'd'.repeat(64),
        }],
    };

    const [file] = api.resultFilesFromArtifacts(items, [], workspace);
    assert.deepEqual(JSON.parse(JSON.stringify(file.voice_keys)), ['speaker:linda', 'speaker:steve']);
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile(file))), ['speaker:linda', 'speaker:steve']);
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
    const source = readRendererSource();
    assert.doesNotMatch(source, /artifacts\.find\(artifact => \(\s*artifact\.lifecycle_state === 'READY'[\s\S]*artifact\.artifact_type === 'export-zip'/);
    assert.match(source, /async function hydrateDownloadContext\(target, workflowId\)/);
    assert.match(source, /projectedWorkspace = await hydrateDownloadContext\(target, workflowId\)/);
    assert.match(source, /projectedDelivery\.zip_available === true/);
    assert.match(source, /hasAuthoritativeDelivery/);
});

test('历史记录按终态区分归档和删除', () => {
    const source = readRendererSource();
    assert.match(source, /terminal \? '归档' : '删除'/);
    assert.match(source, /workflowApi\.deleteWorkflow/);
    assert.match(source, /未完成任务及其相关本地数据已删除/);
});

test('历史完成任务复用交付中心的系统录入面板和工作区', () => {
    const source = readRendererSource();

    assert.match(source, /activeResultContext = context;[\s\S]{0,500}renderSystemInputSurface\(workspace\);/);
    assert.match(source, /activateStandalonePage\('page-4', 'history-result'\);/);
    assert.match(source, /if \(!isHistoryResultView\(\) && currentView !== 'history-result'\) \{[\s\S]*?renderWorkspaceAfterHydrate\(currentWorkspace, currentSession, currentSession\?\.session_id\);/);
    assert.match(source, /openSystemInputConfigDrawer\(currentWorkspace, \{[\s\S]{0,220}deliveryMode: systemInputVisibleDeliveryMode\(currentWorkspace\)/);
    assert.match(source, /function performHistorySystemInputAction\(actionType\)/);
    assert.match(source, /const historyContext = isHistoryResultView\(\) \? activeResultContext : null;/);
});

test('录入配置抽屉默认只展开主编辑区并按需展开缺失区域', () => {
    const source = readRendererSource();
    const template = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'index.html'), 'utf8');
    const styles = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');
    const disclosureTags = [...template.matchAll(/<details[^>]*data-system-input-disclosure[^>]*>/g)].map(match => match[0]);

    // 6 个折叠区：多单元、课文、试卷、范围、应用模板和录入单元；录入类型改为紧凑标签条。
    assert.equal(disclosureTags.length, 6);
    assert.equal(disclosureTags.filter(tag => /\bopen\b/.test(tag)).length, 1);
    assert.match(template, /id="system-input-paper-fields" open data-system-input-disclosure/);
    assert.match(template, /id="system-input-textbook-fields" hidden data-system-input-disclosure/);
    assert.match(template, /id="system-input-optional-fields"[^>]*data-system-input-disclosure/);
    assert.match(template, /id="system-input-range-fields"[^>]*data-system-input-disclosure/);
    assert.match(template, /id="system-input-paper-detail"/);
    assert.match(template, /id="system-input-range-detail"/);
    assert.match(template, /class="system-input-type-strip" id="system-input-type-fields"/);
    assert.match(template, /class="system-input-type-tag"/);
    assert.match(template, /id="system-input-unit-section"[\s\S]*id="system-input-textbook-fields"/);
    assert.doesNotMatch(template, /id="system-input-optional-fields"[^>]*data-system-input-paper-only/);
    assert.doesNotMatch(template, /system-input-form-section-featured/);
    assert.match(source, /function resetSystemInputDisclosureState\(systemInput\)/);
    assert.match(source, /'system-input-paper-fields': inputType === 'paper'/);
    assert.match(source, /'system-input-range-fields': inputType === 'paper' && selectedMissing\.some/);
    assert.match(source, /focusSystemInputValidationError\(errors\[0\]\)/);
    assert.match(source, /'system-input-unit-section': units\.length > 1/);
    assert.match(source, /control\?\.scrollIntoView\?\.\(\{ block: 'center', behavior: 'auto' \}\)/);
    assert.match(source, /if \(drawer\?\.contains\(document\.activeElement\)\) document\.activeElement\.blur\(\);/);
    assert.match(source, /function renderSystemInputDistrictChips\(\)[\s\S]{0,1400}renderSystemInputDisclosureSummaries\(systemInputInteractionWorkspace\(\)\?\.system_input\);/);
    assert.match(source, /picker\.onChoose\?\.\(option\);[\s\S]*?picker\.input\.dispatchEvent\(new Event\('change', \{ bubbles: true \}\)\);/);
    assert.doesNotMatch(source, /otherIncompleteCount/);
    assert.match(styles, /\.system-input-section-meta \{/);
    assert.match(styles, /grid-template-columns: minmax\(0, 1fr\) minmax\(200px, 240px\) 24px/);
    assert.match(styles, /\.system-input-section-meta \{[^}]*justify-items: start/);
    assert.match(styles, /\.system-input-section-summary \{[^}]*justify-self: start/);
    assert.match(styles, /\.system-input-section-detail \{[^}]*text-align: left/);
    assert.match(styles, /\.system-input-unit-progress \{[^}]*justify-self: start/);
    assert.match(template, /<div class="system-input-section-heading-copy"><h3/);
    assert.doesNotMatch(template, /<span class="system-input-section-heading-copy"><h3/);
    assert.match(styles, /scroll-padding: 16px 0 28px/);
    assert.match(styles, /grid-template-areas: "copy copy" "meta arrow"/);
});

test('录入配置折叠摘要展示实际目标值，并把多单元缺失归到单元摘要', () => {
    const { api } = loadRendererConfigFunctions();
    const complete = {
        paperName: '第16题专项卷',
        paperCategory: '题型专项',
        platformTemplateName: '模仿朗读',
        provinceId: { id: 'hubei', name: '湖北省' },
        cityId: { id: 'wuhan', name: '武汉市' },
        districtIds: [{ id: 'hongshan', name: '洪山区' }],
        stageId: { id: 'junior', name: '初中' },
        gradeId: { id: 'grade9', name: '九年级' },
        year: 2026,
        answerTimeMinutes: 20,
    };
    const missing = { ...complete, provinceId: '', year: '' };

    assert.equal(
        api.systemInputPaperDisclosureDetail(complete, []),
        '第16题专项卷 · 题型专项 · 模仿朗读',
    );
    assert.match(
        api.systemInputRangeDisclosureDetail(complete, []),
        /湖北省 · 武汉市 · 洪山区 · 初中 · 九年级 · 2026年 · 20分钟/,
    );
    assert.match(
        api.systemInputRangeDisclosureDetail(missing, ['省份', '年份']),
        /省份待填 · 武汉市 · 洪山区 · 初中 · 九年级 · 年份待填 · 20分钟/,
    );
    assert.equal(
        api.systemInputUnitDisclosureDetail([
            { label: '第16题专项卷', complete: true, missing: [] },
            { label: '第17题专项卷', complete: false, missing: ['发布范围', '年份'] },
        ]),
        '第17题专项卷：发布范围、年份',
    );
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
    // 已取消的条目不欠交付，不应该把已完成的任务顶成“交付待同步”。
    assert.equal(api.historyStatusPresentation({
        execution_state: 'TERMINAL',
        control_state: 'TERMINATED',
        result_status: 'SUCCEEDED',
        completed: 9,
        available_files: 9,
        cancelled: 1,
        total: 10,
    }).label, '已完成');

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

test('历史列表为录入任务提供双阶段状态和入口动作', () => {
    const { api } = loadRendererConfigFunctions();
    const terminal = { execution_state: 'TERMINAL', control_state: 'TERMINATED', result_status: 'SUCCEEDED' };

    // 旧历史行没有录入字段时按仅音频任务解释，不伪造录入状态。
    assert.equal(api.historyRecordDeliveryMode({}), 'audio_only');
    assert.equal(api.historyRecordInputStatus({}), 'not_enabled');
    assert.equal(api.historyRecordDeliveryMode({ delivery_mode: 'audio_and_input' }), 'audio_and_input');
    assert.equal(api.historyInputStatusPresentation({ delivery_mode: 'audio_and_input' }).label, '录入待配置');
    assert.equal(api.historyInputStatusPresentation({ delivery_mode: 'audio_and_input', input_status: 'running' }).label, '录入中');
    assert.equal(api.historyInputStatusPresentation({ delivery_mode: 'audio_and_input', input_status: 'pending_execute' }).label, '待录入');
    assert.equal(api.historyInputStatusPresentation({ delivery_mode: 'audio_and_input', input_status: 'succeeded' }).label, '录入完成');
    assert.equal(api.historyInputStatusPresentation({ delivery_mode: 'audio_and_input', input_status: 'needs_reconcile' }).label, '录入待核验');
    assert.equal(api.historyInputStatusPresentation({ delivery_mode: 'audio_and_input', input_status: 'failed_retryable' }).label, '录入需重试');
    assert.equal(api.historyInputStatusPresentation({ delivery_mode: 'audio_and_input', input_status: 'failed' }).label, '录入失败');
    // 无法识别的录入状态按待核验保守处理，不当作已落定。
    assert.equal(api.historyInputStatusPresentation({ delivery_mode: 'audio_and_input', input_status: 'future_state' }).label, '录入待核验');
    assert.equal(api.historyInputStatusPresentation({ delivery_mode: 'audio_and_input', input_status: 'future_state' }).settled, false);
    assert.equal(api.historyInputStatusPresentation({ delivery_mode: 'audio_only', input_status: 'whatever' }).settled, true);

    // 音频未结束前，录入待配置只是后续步骤；音频结束后才成为需要处理的事实。
    assert.equal(api.historyRecordInputAttention({
        delivery_mode: 'audio_and_input',
        input_status: 'pending_config',
        execution_state: 'RUNNING',
        result_status: 'IN_PROGRESS',
    }), false);
    assert.equal(api.historyRecordInputAttention({
        delivery_mode: 'audio_and_input',
        input_status: 'pending_config',
        ...terminal,
    }), true);
    assert.equal(api.historyRecordInputAttention({
        delivery_mode: 'audio_and_input',
        input_status: 'needs_reconcile',
        execution_state: 'RUNNING',
        result_status: 'IN_PROGRESS',
    }), true);
    assert.equal(api.historyRecordInputSettled({ delivery_mode: 'audio_and_input', input_status: 'succeeded' }), true);
    assert.equal(api.historyRecordInputSettled({ delivery_mode: 'audio_and_input', input_status: 'running' }), false);
    // 音频结束后，待配置/待录入成为待办，徽标用警示色调并进入“需要处理”。
    assert.match(api.historyInputStatusPresentation({
        delivery_mode: 'audio_and_input',
        input_status: 'pending_config',
        execution_state: 'RUNNING',
        result_status: 'IN_PROGRESS',
    }).className, /is-active/);
    assert.match(api.historyInputStatusPresentation({
        delivery_mode: 'audio_and_input',
        input_status: 'pending_config',
        ...terminal,
    }).className, /is-partial/);
    assert.match(api.historyInputStatusPresentation({
        delivery_mode: 'audio_and_input',
        input_status: 'pending_execute',
        ...terminal,
    }).className, /is-partial/);
    assert.equal(api.historyDeliveryTagLabel({ delivery_mode: 'audio_and_input' }), '音频 + 录入');
    assert.equal(api.historyDeliveryTagLabel({}), '仅音频');

    // 录入单元进度取服务端事实，并夹在总数内；没有单元时不展示。
    assert.equal(api.historyInputUnitsProgress({}), null);
    assert.equal(api.historyInputUnitsProgress({ delivery_mode: 'audio_and_input', input_units_total: 0 }), null);
    assert.deepEqual(JSON.parse(JSON.stringify(
        api.historyInputUnitsProgress({ delivery_mode: 'audio_and_input', input_units_total: 3, input_units_succeeded: 9 }),
    )), { total: 3, succeeded: 3 });

    // 终态音频任务的入口动作跟随录入阶段，而不是永远停在交付。
    assert.equal(api.historyActiveActionLabel({ ...terminal }), '查看交付');
    assert.equal(api.historyActiveActionLabel({ ...terminal, delivery_mode: 'audio_and_input', input_status: 'succeeded' }), '查看交付');
    assert.equal(api.historyActiveActionLabel({ ...terminal, delivery_mode: 'audio_and_input', input_status: 'running' }), '查看录入');
    assert.equal(api.historyActiveActionLabel({ ...terminal, delivery_mode: 'audio_and_input', input_status: 'needs_reconcile' }), '查看录入');
});

test('历史筛选把录入阶段并入进行中、需要处理和已完成', () => {
    const { api } = loadRendererConfigFunctions();
    const terminal = { execution_state: 'TERMINAL', control_state: 'TERMINATED', result_status: 'SUCCEEDED' };
    const audioOnly = { ...terminal, source_filename: 'a.docx' };
    const inputDone = { ...terminal, delivery_mode: 'audio_and_input', input_status: 'succeeded', source_filename: 'b.docx' };
    const inputRunning = { ...terminal, delivery_mode: 'audio_and_input', input_status: 'running', source_filename: 'c.docx' };
    const inputPending = { ...terminal, delivery_mode: 'audio_and_input', input_status: 'pending_config', source_filename: 'd.docx' };
    const inputReconcile = { ...terminal, delivery_mode: 'audio_and_input', input_status: 'needs_reconcile', source_filename: 'e.docx' };
    const inputPendingWhileGenerating = {
        delivery_mode: 'audio_and_input',
        input_status: 'pending_config',
        execution_state: 'RUNNING',
        result_status: 'IN_PROGRESS',
        source_filename: 'f.docx',
    };

    api.historyFilters.query = '';
    api.historyFilters.status = 'input';
    assert.equal(api.historyRecordMatchesFilter(audioOnly), false);
    assert.equal(api.historyRecordMatchesFilter(inputDone), true);
    assert.equal(api.historyRecordMatchesFilter(inputRunning), true);

    api.historyFilters.status = 'done';
    assert.equal(api.historyRecordMatchesFilter(audioOnly), true);
    assert.equal(api.historyRecordMatchesFilter(inputDone), true);
    assert.equal(api.historyRecordMatchesFilter(inputRunning), false);

    api.historyFilters.status = 'active';
    assert.equal(api.historyRecordMatchesFilter(inputRunning), true);
    assert.equal(api.historyRecordMatchesFilter(audioOnly), false);
    assert.equal(api.historyRecordMatchesFilter(inputPendingWhileGenerating), true);

    api.historyFilters.status = 'attention';
    assert.equal(api.historyRecordMatchesFilter(inputReconcile), true);
    assert.equal(api.historyRecordMatchesFilter(inputPending), true);
    assert.equal(api.historyRecordMatchesFilter(inputPendingWhileGenerating), false);
    assert.equal(api.historyRecordMatchesFilter(inputDone), false);

    // 搜索命中录入标识和录入状态文案；仅音频任务不响应“录入”关键词。
    api.historyFilters.status = 'all';
    api.historyFilters.query = '录入';
    assert.equal(api.historyRecordMatchesFilter(audioOnly), false);
    assert.equal(api.historyRecordMatchesFilter(inputDone), true);
    api.historyFilters.query = '';
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

test('系统录入级联选择只暴露当前父级下一级选项', () => {
    const { api } = loadRendererConfigFunctions();
    assert.equal(api.systemInputCascadeFieldSelected('paper', new Set(['province_id']), 'provinceId'), true);
    assert.equal(api.systemInputCascadeFieldSelected('paper', new Set(['provinceId']), 'cityId'), false);
    const indexes = {
        provinces: [
            { id: 'p1', name: '省A', path: ['省A'] },
            { id: 'p2', name: '省B', path: ['省B'] },
        ],
        cities: [
            { id: 'c1', name: '城市A', parentId: 'p1', path: ['省A', '城市A'] },
            { id: 'c2', name: '城市B', parentId: 'p2', path: ['省B', '城市B'] },
        ],
        districts: [
            { id: 'd1', name: '区县A', parentId: 'c1', path: ['省A', '城市A', '区县A'] },
            { id: 'd2', name: '区县B', parentId: 'c2', path: ['省B', '城市B', '区县B'] },
        ],
    };
    const ids = values => values.map(value => value.id);

    const empty = api.systemInputRegionCascadeState({ indexes });
    assert.deepEqual(JSON.parse(JSON.stringify(ids(empty.cities))), []);
    assert.deepEqual(JSON.parse(JSON.stringify(ids(empty.districts))), []);
    assert.deepEqual(JSON.parse(JSON.stringify(ids(empty.grades))), []);

    const provinceOnly = api.systemInputRegionCascadeState({ provinceText: '省A', indexes });
    assert.deepEqual(JSON.parse(JSON.stringify(ids(provinceOnly.cities))), ['c1']);
    assert.deepEqual(JSON.parse(JSON.stringify(ids(provinceOnly.districts))), []);

    const citySelected = api.systemInputRegionCascadeState({
        provinceText: '省A',
        cityText: '城市A',
        indexes,
    });
    assert.deepEqual(JSON.parse(JSON.stringify(ids(citySelected.districts))), ['d1']);

    const mismatchedCity = api.systemInputRegionCascadeState({
        provinceText: '省A',
        cityText: '城市B',
        indexes,
    });
    assert.deepEqual(JSON.parse(JSON.stringify(ids(mismatchedCity.districts))), []);

    const stageOnly = api.systemInputRegionCascadeState({ stageText: '初中', indexes });
    assert.deepEqual(JSON.parse(JSON.stringify(ids(stageOnly.grades))), [7, 8, 9]);
});

test('系统录入答题时间按试卷分类使用不同默认值', () => {
    const { api } = loadRendererConfigFunctions();

    assert.equal(api.systemInputDefaultAnswerTimeForCategory('听说考试'), 60);
    assert.equal(api.systemInputDefaultAnswerTimeForCategory('题型专项'), 20);
    assert.equal(api.systemInputDefaultAnswerTimeForCategory(''), 20);
});

test('平台题型模板只能从同步目录选择，不提供自定义保存/管理配置', () => {
    const { api } = loadRendererConfigFunctions();

    const empty = api.systemInputPlatformTemplateInteractionPresentation('', null);
    assert.equal(empty.showSave, false);
    assert.equal(empty.showManage, false);
    assert.equal(empty.isSaved, false);
    assert.match(empty.note, /同步的平台模板目录/);

    const custom = api.systemInputPlatformTemplateInteractionPresentation('临时专项模板', null);
    assert.equal(custom.showSave, false);
    assert.equal(custom.showManage, false);

    const saved = api.systemInputPlatformTemplateInteractionPresentation('模仿朗读', {
        platform_template_key: 'template-1',
        name: '模仿朗读',
    });
    assert.equal(saved.showSave, false);
    assert.equal(saved.showManage, false);
    assert.equal(saved.isSaved, false);

    const busy = api.systemInputPlatformTemplateInteractionPresentation('模仿朗读', {
        platform_template_key: 'template-1',
        name: '模仿朗读',
    }, true);
    assert.equal(busy.busy, true);
});

test('系统录入多单元校验要求每个单元都有完整页面配置', () => {
    const { api } = loadRendererConfigFunctions();
    const complete = {
        paperName: '外研九上-U6-第1套',
        paperCategory: '题型专项',
        platformTemplateName: '模仿朗读',
        provinceId: { id: 440000, name: '广东省' },
        cityId: { id: 440600, name: '佛山市' },
        stageId: { id: 2, name: '初中' },
        gradeId: { id: 9, name: '九年级' },
        year: 2026,
        answerTimeMinutes: 20,
    };

    assert.deepEqual(
        JSON.parse(JSON.stringify(api.systemInputUnitMissingFields(complete))),
        [],
    );
    assert.deepEqual(
        JSON.parse(JSON.stringify(api.systemInputUnitMissingFields({
            ...complete,
            platformTemplateName: '123',
            provinceId: 440000,
            answerTimeMinutes: '',
        }))),
        ['平台题型模板', '省份', '答题时间'],
    );
    assert.deepEqual(
        JSON.parse(JSON.stringify(api.systemInputUnitMissingFields({
            ...complete,
            paperCategory: '听说考试',
        }))),
        ['考试类型'],
    );
});

test('系统录入多单元配置展示每个单元的完成度和缺失字段', () => {
    const { api } = loadRendererConfigFunctions();
    const complete = {
        paperName: '外研九上-U6-第1套',
        paperCategory: '题型专项',
        platformTemplateName: '模仿朗读',
        provinceId: { id: 440000, name: '广东省' },
        cityId: { id: 440600, name: '佛山市' },
        stageId: { id: 2, name: '初中' },
        gradeId: { id: 9, name: '九年级' },
        year: 2026,
        answerTimeMinutes: 20,
    };
    const ready = api.systemInputUnitStatusPresentation(
        { unit_id: 'unit-1', label: '第16题专项卷' },
        complete,
        0,
        2,
    );
    assert.equal(ready.label, '第16题专项卷');
    assert.equal(ready.complete, true);
    assert.deepEqual(JSON.parse(JSON.stringify(ready.missing)), []);
    assert.equal(ready.status, '已补齐');

    const incomplete = api.systemInputUnitStatusPresentation(
        { unit_id: 'unit-2', label: '第17题专项卷' },
        { ...complete, paperName: '', cityId: null, platformTemplateName: '' },
        1,
        2,
    );
    assert.equal(incomplete.complete, false);
    assert.deepEqual(JSON.parse(JSON.stringify(incomplete.missing)), ['试卷名称', '平台题型模板', '城市']);
    assert.equal(incomplete.status, '待补齐 3 项');
});

test('多套试卷名称切换和批量同步不会改写单元名称', () => {
    const { api } = loadRendererConfigFunctions();
    const units = [
        { unit_id: 'unit-16', label: '第16题专项卷' },
        { unit_id: 'unit-17', label: '第17题专项卷' },
    ];

    assert.equal(
        api.systemInputPaperNameForUnit(
            '外研九上-1-第17题专项卷-第16题专项卷',
            units[0],
            0,
            units.length,
            units,
        ),
        '外研九上-1-第17题专项卷-第16题专项卷',
    );
    assert.equal(
        api.systemInputPaperNameForUnit(
            '外研九上-1-第16题专项卷',
            units[1],
            1,
            units.length,
            units,
        ),
        '外研九上-1-第16题专项卷',
    );
    assert.equal(
        api.systemInputPaperNameForUnit('', units[0], 0, units.length, units),
        '',
    );
});

test('多套配置会合并公共默认值，且不会把工作区内容带进单元配置', () => {
    const { api } = loadRendererConfigFunctions();
    const units = [
        { unit_id: 'unit-16', label: '第16题专项卷' },
        { unit_id: 'unit-17', label: '第17题专项卷' },
    ];
    const common = {
        input_type: 'paper',
        delivery_mode: 'audio_and_input',
        paperCategory: '题型专项',
        provinceId: { id: 440000, name: '广东省' },
        cityId: { id: 440600, name: '佛山市' },
        stageId: { id: 2, name: '初中' },
        gradeId: { id: 9, name: '九年级' },
        year: 2026,
        answerTimeMinutes: 20,
        platformTemplateName: '模仿朗读',
        content_segments: [{ raw_text: '不得进入配置' }],
    };
    const normalized = api.systemInputNormalizeUnitConfiguration(
        api.systemInputUnitConfiguration(
            { unit_id: 'unit-17', configuration: { paperName: '外研九上-1-第16题专项卷' } },
            common,
        ),
        units[1],
        1,
        units.length,
        units,
    );

    assert.equal(normalized.paperName, '外研九上-1-第16题专项卷');
    assert.equal(normalized.provinceId.name, '广东省');
    assert.equal(normalized.platformTemplateName, '模仿朗读');
    assert.equal('content_segments' in normalized, false);
});

test('保存归一化会清掉失效的纸张子级和脱离父级的模板引用', () => {
    const { api } = loadRendererConfigFunctions();
    const normalized = api.systemInputNormalizeUnitConfiguration(
        {
            unit_id: 'unit-stale',
            paperCategory: '题型专项',
            paperType: { id: 1, name: '旧考试类型' },
            platformTemplateId: 'template-old',
            platformTemplateVersion: '1',
        },
        { unit_id: 'unit-stale' },
        0,
        1,
        [],
        'paper',
    );

    assert.equal('paperType' in normalized, false);
    assert.equal('platformTemplateId' in normalized, false);
    assert.equal('platformTemplateVersion' in normalized, false);
});

test('保存归一化会把旧版试卷分类、模板名称和标题对象还原为页面文本', () => {
    const { api } = loadRendererConfigFunctions();
    const normalized = api.systemInputNormalizeUnitConfiguration(
        {
            unit_id: 'unit-legacy-paper',
            paperName: { name: '旧版试卷' },
            paperCategory: { id: 'listening', name: '听说考试' },
            paperType: { id: 1, name: '阶段测试题' },
            platformTemplateName: { id: 'mimic', name: '模仿朗读' },
        },
        { unit_id: 'unit-legacy-paper' },
        0,
        1,
        [],
        'paper',
    );
    assert.equal(normalized.paperName, '旧版试卷');
    assert.equal(normalized.paperCategory, '听说考试');
    assert.equal(normalized.platformTemplateName, '模仿朗读');
    assert.deepEqual(normalized.paperType, { id: 1, name: '阶段测试题' });
    assert.equal('paper_category' in normalized, false);
    assert.equal('platform_template_name' in normalized, false);
});

test('词汇单元归一化不会继承试卷或课文页面字段', () => {
    const { api } = loadRendererConfigFunctions();
    const normalized = api.systemInputNormalizeUnitConfiguration(
        {
            unit_id: 'unit-vocabulary',
            input_type: 'vocabulary',
            paperName: '旧试卷名称',
            paperCategory: '题型专项',
            paperType: { id: 1, name: '旧考试类型' },
            provinceId: { id: 440000, name: '广东省' },
            platformTemplateName: '旧平台模板',
            textbookNameZh: '旧课文',
            textbookForm: '同步课文',
            textbookLesson: 'Section A',
        },
        { unit_id: 'unit-vocabulary' },
        0,
        1,
        [],
        'vocabulary',
    );

    assert.equal(normalized.unit_id, 'unit-vocabulary');
    assert.equal(normalized.input_type, 'vocabulary');
    ['paperName', 'paperCategory', 'paperType', 'provinceId', 'platformTemplateName',
        'textbookNameZh', 'textbookForm', 'textbookLesson']
        .forEach(key => assert.equal(key in normalized, false, `${key} should be removed`));
});

test('系统录入应用模板只保存多单元之间的公共页面配置', () => {
    const { api } = loadRendererConfigFunctions();
    const common = {
        paperCategory: '题型专项',
        provinceId: { id: 440000, name: '广东省' },
        cityId: { id: 440600, name: '佛山市' },
        districtIds: [{ id: 440605, name: '南海区' }],
        stageId: { id: 2, name: '初中' },
        gradeId: { id: 9, name: '九年级' },
        year: 2026,
        answerTimeMinutes: 20,
        platformTemplateName: '模仿朗读',
    };
    const configuration = api.systemInputCommonConfiguration([
        { ...common, paperName: '第一套' },
        { ...common, paperName: '第二套' },
    ]);

    assert.deepEqual(JSON.parse(JSON.stringify(configuration)), common);

    const mixed = api.systemInputCommonConfiguration([
        common,
        { ...common, cityId: { id: 440100, name: '广州市' } },
    ]);
    assert.equal(mixed.provinceId.name, '广东省');
    assert.equal('cityId' in mixed, false);
    assert.equal('paperName' in mixed, false);
});

test('课文录入保存应用模板时排除自动识别的课文形式', () => {
    const { api } = loadRendererConfigFunctions();
    const textbook = {
        textbookNameZh: '交朋友',
        textbookNameEn: 'Making new friends',
        textbookForm: '同步课文',
        textbookVersion: '外研版',
        textbookStage: '初中',
        textbookGrade: '七年级',
        textbookVolume: '上册',
        textbookUnit: 'Unit 1',
        textbookLesson: 'Section B',
    };

    const configuration = api.systemInputCommonConfiguration([
        { ...textbook, textbookNameZh: '交朋友 · 第一套' },
        { ...textbook, textbookNameZh: '交朋友 · 第二套' },
    ], 'textbook');
    const { textbookNameZh: _textbookNameZh, textbookForm: _textbookForm, ...sharedTextbook } = textbook;
    assert.deepEqual(JSON.parse(JSON.stringify(configuration)), sharedTextbook);
    assert.equal('textbookNameZh' in configuration, false);
    assert.equal('textbookForm' in configuration, false);

    const entries = api.systemInputAppTemplateEntries([
        { ...textbook, unit_id: 'unit-1', textbookNameZh: '第一篇' },
        { ...textbook, unit_id: 'unit-2', textbookNameZh: '第二篇' },
    ], [
        { unit_id: 'unit-1', label: '第一套' },
        { unit_id: 'unit-2', label: '第二套' },
    ], 'textbook');
    assert.equal(entries.length, 2);
    assert.equal(entries[0].configuration.textbookLesson, 'Section B');
    assert.equal(entries[1].configuration.textbookNameZh, '第二篇');
    assert.equal('textbookForm' in entries[0].configuration, false);
    assert.equal('paperCategory' in entries[0].configuration, false);
});

test('多套配置除名称外不一致时拆分为多条应用模板', () => {
    const { api } = loadRendererConfigFunctions();
    const common = {
        paperCategory: '题型专项',
        provinceId: { id: 440000, name: '广东省' },
        cityId: { id: 440600, name: '佛山市' },
        stageId: { id: 2, name: '初中' },
        gradeId: { id: 9, name: '九年级' },
        year: 2026,
        answerTimeMinutes: 20,
    };
    const units = [
        { unit_id: 'unit-1', label: '第一套' },
        { unit_id: 'unit-2', label: '第二套' },
    ];
    const sameExceptNames = api.systemInputAppTemplateEntries([
        { ...common, paperName: '第一套' },
        { ...common, paperName: '第二套' },
    ], units);
    assert.equal(sameExceptNames.length, 1);
    assert.equal('paperName' in sameExceptNames[0].configuration, false);

    const differentConfigurations = api.systemInputAppTemplateEntries([
        { ...common, paperName: '第一套', cityId: { id: 440600, name: '佛山市' }, platformTemplateName: '模仿朗读' },
        { ...common, paperName: '第二套', cityId: { id: 440100, name: '广州市' }, platformTemplateName: '听后选择' },
    ], units);
    assert.equal(differentConfigurations.length, 2);
    assert.deepEqual(
        JSON.parse(JSON.stringify(differentConfigurations.map(entry => entry.unitIds))),
        [['unit-1'], ['unit-2']],
    );
    assert.equal(differentConfigurations[0].configuration.cityId.name, '佛山市');
    assert.equal(differentConfigurations[1].configuration.cityId.name, '广州市');
    assert.equal(differentConfigurations[0].configuration.platformTemplateName, '模仿朗读');
    assert.equal(differentConfigurations[1].configuration.platformTemplateName, '听后选择');
    assert.equal(api.systemInputAppTemplateNameForGroup('听说配置', differentConfigurations[0], units, true), '听说配置 · 第一套');
});

test('拆分后的应用模板各自携带对应的平台题型模板引用', () => {
    const { api } = loadRendererConfigFunctions();
    const payload = api.systemInputAppTemplatePayload(
        'paper',
        '模仿朗读 · 第一套',
        {
            paperCategory: '题型专项',
            platformTemplateId: 'platform-1',
            platformTemplateName: '模仿朗读',
            platformTemplateVersion: 'v1',
        },
    );
    assert.equal(payload.platform_template_id, 'platform-1');
    assert.equal(payload.platform_template_name, '模仿朗读');
    assert.equal(payload.platform_template_version, 'v1');
    assert.equal(payload.configuration.platformTemplateName, '模仿朗读');
});

test('应用模板载荷剥离任务特定字段（unit_id、试卷名称等）', () => {
    const { api } = loadRendererConfigFunctions();
    // Unit drafts carry the task's unit id and the document-derived paper
    // name. The backend rejects unit ids outright, and a stored paper name
    // would only be a stale document title.
    const payload = api.systemInputAppTemplatePayload(
        'paper',
        '广东初中模仿朗读',
        {
            unit_id: 'unit-7',
            entry_id: 'entry-7',
            app_template_id: 'app-template-old',
            paperName: '七上StarterUnit2听说测试题',
            paperCategory: '题型专项',
            provinceId: { id: 440000, name: '广东省' },
            platformTemplateName: '模仿朗读',
        },
    );
    assert.equal(payload.configuration.unit_id, undefined);
    assert.equal(payload.configuration.entry_id, undefined);
    assert.equal(payload.configuration.app_template_id, undefined);
    assert.equal(payload.configuration.paperName, undefined);
    assert.equal(payload.configuration.paper_name, undefined);
    assert.equal(payload.configuration.paperCategory, '题型专项');
    assert.deepEqual(payload.configuration.provinceId, { id: 440000, name: '广东省' });
    assert.equal(payload.configuration.platformTemplateName, '模仿朗读');
});

test('多套应用模板可选择当前单元或全部单元', () => {
    const { api } = loadRendererConfigFunctions();
    const units = [
        { unit_id: 'unit-1' },
        { unit_id: 'unit-2' },
        { unit_id: 'unit-3' },
    ];
    assert.deepEqual(
        JSON.parse(JSON.stringify(api.systemInputAppTemplateTargetUnits(units, 'unit-2', 'current').map(unit => unit.unit_id))),
        ['unit-2'],
    );
    assert.deepEqual(
        JSON.parse(JSON.stringify(api.systemInputAppTemplateTargetUnits(units, 'unit-2', 'all').map(unit => unit.unit_id))),
        ['unit-1', 'unit-2', 'unit-3'],
    );
    assert.deepEqual(
        JSON.parse(JSON.stringify(api.systemInputAppTemplateTargetUnits(units, 'unit-2', 'unknown').map(unit => unit.unit_id))),
        ['unit-2'],
    );
});

test('应用系统录入模板保留单元名称并覆盖可复用字段', () => {
    const { api } = loadRendererConfigFunctions();
    const merged = api.systemInputMergeAppTemplateIntoUnit(
        {
            unit_id: 'unit-1',
            paperName: '第一套',
            paperCategory: '听说考试',
            paperType: { id: 1, name: '旧类型' },
            provinceId: { id: 440000, name: '旧省份' },
        },
        {
            paperCategory: '题型专项',
            provinceId: { id: 330000, name: '浙江省' },
            districtIds: [],
        },
        { name: '模仿朗读' },
    );

    assert.equal(merged.paperName, '第一套');
    assert.deepEqual(JSON.parse(JSON.stringify(merged.provinceId)), { id: 330000, name: '浙江省' });
    assert.deepEqual(JSON.parse(JSON.stringify(merged.districtIds)), []);
    assert.equal('paperType' in merged, false);
    assert.equal(merged.platformTemplateName, '模仿朗读');

    const textbookMerged = api.systemInputMergeAppTemplateIntoUnit(
        { unit_id: 'unit-textbook', textbookForm: '同步课文' },
        {
            textbook_name_zh: 'Making new friends',
            textbookForm: '角色扮演',
            textbook_stage: '初中',
            textbook_lesson: 'Section B',
        },
    );
    assert.equal(textbookMerged.textbookNameZh, 'Making new friends');
    assert.equal(textbookMerged.textbookStage, '初中');
    assert.equal(textbookMerged.textbookLesson, 'Section B');
    assert.equal(textbookMerged.textbookForm, '同步课文');
});

test('仅有安全预检失败的系统录入运行允许重新编辑配置', () => {
    const { api } = loadRendererConfigFunctions();
    const source = readRendererSource();

    assert.equal(api.systemInputConfigurationEditable({ available: true }), true);
    assert.equal(api.systemInputConfigurationEditable({
        available: true,
        input_run: { status: 'FAILED' },
        configuration_editable: true,
    }), true);
    assert.equal(api.systemInputConfigurationEditable({
        available: true,
        input_run: { status: 'RUNNING' },
        configuration_editable: false,
    }), false);
    // Missing the new server fact must fail closed when a run exists.
    assert.equal(api.systemInputConfigurationEditable({
        available: true,
        input_run: { status: 'FAILED' },
    }), false);
    assert.match(source, /const deliveryModeLocked = systemInputDeliveryModeIsLocked\(systemInput\);/);
    assert.match(source, /if \(!systemInput\s+\|\| systemInput\.available === false\s+\|\| systemInputDeliveryModeIsLocked\(systemInput\)\s+\|\| !systemInputConfigurationEditable\(systemInput\)\)/);
});

test('系统录入失败状态展示预检步骤和页面图片归属诊断', () => {
    const { api } = loadRendererConfigFunctions();
    const diagnostic = api.systemInputDiagnosticText({
        input_run: {
            status: 'FAILED',
            attempts: [{
                status: 'FAILED',
                error_code: 'ARTIFACT_INVALID',
                error_message: '信息记录表图片产物无法读取',
                evidence: {
                    details: {
                        workflow_id: 'workflow-rerun',
                        artifact_owner_workflow_id: 'workflow-parent',
                    },
                },
            }],
        },
    });
    assert.match(diagnostic, /ARTIFACT_INVALID/);
    assert.match(diagnostic, /页面图片仍归属父任务/);

    const preflightDiagnostic = api.systemInputDiagnosticText({
        input_run: {
            status: 'FAILED',
            error_code: 'INPUT_PLATFORM_PREFLIGHT_FAILED',
            error_message: '乐学君平台页面会话预检失败',
            attempts: [{
                status: 'FAILED',
                error_code: 'INPUT_PLATFORM_PREFLIGHT_FAILED',
                error_message: '乐学君平台页面会话预检失败',
                evidence: {
                    details: {
                        phase: 'preflight',
                        step: 'wait_until_ready',
                        error_message: '页面仍处于登录页',
                    },
                },
            }],
        },
    });
    assert.match(preflightDiagnostic, /预检步骤：wait_until_ready/);
    assert.match(preflightDiagnostic, /原因：页面仍处于登录页/);
});

test('系统录入完成后交付方式保持只读，旧投影也不会重新开放切换', () => {
    const { api } = loadRendererConfigFunctions();
    const source = readRendererSource();
    const stylesSource = fs.readFileSync(
        path.join(__dirname, '..', 'renderer', 'styles.css'),
        'utf8',
    );

    assert.equal(api.systemInputDeliveryModeIsLocked({ input_status: 'succeeded' }), true);
    assert.equal(api.systemInputDeliveryModeIsLocked({ input_run: { status: 'SUCCEEDED' } }), true);
    assert.equal(api.systemInputDeliveryModeIsLocked({
        input_status: 'pending_execute',
        input_run: { status: 'FAILED' },
        configuration_editable: true,
    }), false);
    assert.equal(api.systemInputDeliveryModeIsLocked({
        input_status: 'running',
        input_run: { status: 'RUNNING' },
        configuration_editable: false,
    }), true);
    assert.match(source, /input.disabled = modeDisabled;/);
    assert.match(source, /option\?\.classList\.toggle\('is-disabled', modeDisabled\)/);
    assert.match(source, /systemInputDeliveryModeIsLocked\(systemInput\)/);
    assert.match(stylesSource, /\.system-input-route-option\.is-disabled/);
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

test('Renderer 通过命名模块和共享 context 组织跨功能依赖', () => {
    const { api } = loadRendererConfigFunctions();
    const context = api.rendererContext;

    assert.equal(context.env.platform, 'web');
    assert.equal(context.services.api, null);
    assert.equal(context.state.currentView, 'workflow');
    assert.equal(typeof context.modules['review.outlineModel'].buildReviewOutlineModel, 'function');
    assert.equal(typeof context.modules['generation.sse'].connectSSE, 'function');

    context.state.currentView = 'history';
    assert.equal(context.state.currentView, 'history');
});

test('Renderer 只保留单一工作台入口', () => {
    const mainSource = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');
    const preloadSource = fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8');
    const rendererDirectory = path.join(__dirname, '..', 'renderer');
    const projectRoot = path.join(__dirname, '..', '..');

    assert.match(mainSource, /path\.join\(__dirname, 'renderer', 'index\.html'\)/);
    assert.doesNotMatch(mainSource, /WORDTTS_RENDERER_SHELL|index-legacy/);
    assert.doesNotMatch(preloadSource, /WORDTTS_RENDERER_SHELL|rendererShell/);
    assert.equal(fs.existsSync(path.join(rendererDirectory, 'index.html')), true);
    assert.equal(fs.existsSync(path.join(rendererDirectory, 'index-legacy.html')), false);
    assert.equal(fs.existsSync(path.join(rendererDirectory, 'legacy-app.js')), false);
    assert.equal(fs.existsSync(path.join(rendererDirectory, 'legacy-styles.css')), false);
    assert.equal(fs.existsSync(path.join(projectRoot, 'app.js')), false);
    assert.equal(fs.existsSync(path.join(projectRoot, 'installer-prototype', 'app.js')), true);
});

test('系统录入配置入口不切换到进度页，进度页不承载配置入口', () => {
    const rendererDirectory = path.join(__dirname, '..', 'renderer');
    const html = fs.readFileSync(path.join(rendererDirectory, 'index.html'), 'utf8');
    const surfaceSource = fs.readFileSync(
        path.join(rendererDirectory, 'modules', 'system-input', 'surface.js'),
        'utf8',
    );
    const pagesSource = fs.readFileSync(
        path.join(rendererDirectory, 'modules', 'system-input', 'pages.js'),
        'utf8',
    );

    const configHandler = surfaceSource.match(
        /\$\('system-input-config-btn'\)\?\.addEventListener\('click', \(\) => \{[\s\S]*?(?=\n    \$\('system-input-start-btn')/,
    )?.[0] || '';
    assert.match(configHandler, /openSystemInputConfigDrawer\(workspace, \{ deliveryMode: 'audio_and_input' \}\);/);
    assert.doesNotMatch(configHandler, /showSystemInputProgressPage/);
    assert.doesNotMatch(pagesSource, /system-input-page-config-btn/);

    const progressStart = html.indexOf('id="page-system-input"');
    const finalStart = html.indexOf('id="page-final-delivery"');
    const generationStart = html.indexOf('id="page-3"');
    const deliveryStart = html.indexOf('id="page-4"');
    const progressPage = progressStart >= 0 && finalStart > progressStart
        ? html.slice(progressStart, finalStart)
        : '';
    const generationPage = generationStart >= 0 && deliveryStart > generationStart
        ? html.slice(generationStart, deliveryStart)
        : '';
    assert.ok(progressPage, '系统录入进度页应存在');
    assert.doesNotMatch(progressPage, /system-input-page-config-btn|编辑录入目标|设置录入目标/);
    assert.doesNotMatch(generationPage, /system-input-(?:config|open-config|page)/);
});

test('最终结果页默认收起未启用的录入对账栏，并由渲染状态恢复', () => {
    const rendererDirectory = path.join(__dirname, '..', 'renderer');
    const html = fs.readFileSync(path.join(rendererDirectory, 'index.html'), 'utf8');
    const pagesSource = fs.readFileSync(
        path.join(rendererDirectory, 'modules', 'system-input', 'pages.js'),
        'utf8',
    );
    const finalStart = html.indexOf('id="page-final-delivery"');
    const historyStart = html.indexOf('id="page-history"');
    const finalPage = finalStart >= 0 && historyStart > finalStart
        ? html.slice(finalStart, historyStart)
        : '';
    assert.ok(finalPage, '最终结果页应存在');
    assert.match(finalPage, /class="bench-panel final-delivery-input-panel rebuild-surface"[^>]* hidden/);
    assert.match(finalPage, /class="bench-panel final-delivery-entries-panel rebuild-surface"[^>]* hidden/);
    assert.match(pagesSource, /finalFrame\.dataset\.deliveryMode = inputEnabled \? 'audio-and-input' : 'audio-only';/);
    assert.match(pagesSource, /if \(entriesPanel\) entriesPanel\.hidden = !inputEnabled;/);
});

test('系统录入只有逐单元成功结果才允许进入完整交付', () => {
    const { api } = loadRendererConfigFunctions();

    assert.equal(api.systemInputRunIsComplete({
        input_status: 'succeeded',
        units: [{ unit_id: 'unit-1' }],
        entries: [],
    }), false);
    assert.equal(api.systemInputRunIsComplete({
        input_status: 'succeeded',
        units: [{ unit_id: 'unit-1' }],
        entries: [{ unit_id: 'unit-1' }],
    }), false);
    assert.equal(api.systemInputRunIsComplete({
        input_status: 'succeeded',
        units: [{ unit_id: 'unit-1' }],
        entries: [{ unit_id: 'unit-1', input_status: 'succeeded' }],
    }), true);
    assert.equal(api.systemInputRunIsComplete({
        input_status: 'succeeded',
        units: [{ unit_id: 'unit-1' }, { unit_id: 'unit-2' }],
        entries: [
            { unit_id: 'unit-1', input_status: 'succeeded' },
            { unit_id: 'stale-unit', input_status: 'succeeded' },
        ],
    }), false);
    assert.equal(api.systemInputRunIsComplete({
        input_status: 'succeeded',
        input_run: {
            status: 'SUCCEEDED',
            control: { stop_requested: true },
        },
        units: [{ unit_id: 'unit-1' }],
        entries: [{ unit_id: 'unit-1', input_status: 'succeeded' }],
    }), false);
    assert.deepEqual(JSON.parse(JSON.stringify(api.systemInputRunControlState({
        input_run: {
            input_run_id: 'run-1',
            status: 'SUCCEEDED',
            control: { pause_requested: false, stop_requested: true },
        },
    }))), {
        active: true,
        paused: false,
        stopping: true,
    });
    assert.equal(api.systemInputEntryStatus({
        input_status: 'succeeded',
        input_run: { status: 'SUCCEEDED' },
    }, null), 'needs_reconcile');
});

test('用户停止或关闭浏览器后的录入不会自动继续', () => {
    const { api } = loadRendererConfigFunctions();
    assert.equal(api.systemInputRunWasStopped({
        input_run: { status: 'FAILED', error_code: 'INPUT_RUN_STOPPED' },
    }), true);
    assert.equal(api.systemInputRunWasStopped({
        input_run: { status: 'RUNNING', control: { stop_requested: true } },
    }), true);
    assert.equal(api.systemInputRunWasStopped({
        input_run: { status: 'AMBIGUOUS', error_code: 'INPUT_BROWSER_CLOSED' },
    }), true);
    assert.equal(api.systemInputRunWasStopped({
        input_run: { status: 'AMBIGUOUS', error_code: 'INPUT_RESULT_UNCONFIRMED' },
    }), false);

    const pagesSource = fs.readFileSync(
        path.join(__dirname, '..', 'renderer', 'modules', 'system-input', 'pages.js'),
        'utf8',
    );
    assert.match(pagesSource, /if \(systemInputRunWasStopped\(systemInput\)\) return;/);
    const resolveStart = pagesSource.indexOf('async function resolveSystemInputAmbiguity');
    const verifyStart = pagesSource.indexOf('async function runSystemInputVerification');
    assert.ok(resolveStart >= 0 && verifyStart > resolveStart);
    assert.doesNotMatch(pagesSource.slice(resolveStart, verifyStart), /automatic &&/);
    assert.match(
        pagesSource.slice(verifyStart, verifyStart + 1200),
        /if \(automatic && systemInputRunWasStopped\(systemInput\)\) return false;/,
    );
});

test('多个待核验单元不会被旧单元的核验结果阻止自动核验', () => {
    const { api } = loadRendererConfigFunctions();
    const systemInput = {
        input_run: {
            attempts: [
                {
                    attempt_id: 'attempt-old',
                    evidence: {
                        external_record_verification: { status: 'resolved' },
                    },
                },
                {
                    attempt_id: 'attempt-current',
                    evidence: {},
                },
            ],
        },
    };
    assert.deepEqual(
        JSON.parse(JSON.stringify(api.systemInputVerificationOutcome(systemInput, 'attempt-old'))),
        { status: 'resolved' },
    );
    assert.equal(api.systemInputVerificationOutcome(systemInput, 'attempt-current'), null);
});

test('录入结果核验的证据哈希在重试时保持稳定', () => {
    const { api } = loadRendererConfigFunctions();
    const payload = {
        workflowId: 'workflow-1',
        inputRunId: 'run-1',
        attemptId: 'attempt-1',
        decision: 'NOT_SUBMITTED',
        externalId: null,
    };
    assert.equal(api.systemInputEvidenceHash(payload), api.systemInputEvidenceHash(payload));
    assert.match(api.systemInputEvidenceHash(payload), /^desktop-\d+-[0-9a-f]{8}$/);
    assert.notEqual(
        api.systemInputEvidenceHash(payload),
        api.systemInputEvidenceHash({ ...payload, decision: 'CONFIRMED', externalId: 'IN-1' }),
    );
});

test('确认和启动系统录入成功后才切换页面，失败不会先离开交付中心', () => {
    const rendererDirectory = path.join(__dirname, '..', 'renderer');
    const html = fs.readFileSync(path.join(rendererDirectory, 'index.html'), 'utf8');
    const surfaceSource = fs.readFileSync(
        path.join(rendererDirectory, 'modules', 'system-input', 'surface.js'),
        'utf8',
    );
    const pagesSource = fs.readFileSync(
        path.join(rendererDirectory, 'modules', 'system-input', 'pages.js'),
        'utf8',
    );
    const bootstrapSource = fs.readFileSync(
        path.join(rendererDirectory, 'modules', 'app', 'bootstrap.js'),
        'utf8',
    );

    const startHandler = surfaceSource.match(
        /\$\('system-input-start-btn'\)\?\.addEventListener\('click', \(\) => \{[\s\S]*?(?=\n    \$\('system-input-drawer-close')/,
    )?.[0] || '';
    const deliveryTemplateStart = html.indexOf('id="system-input-card"');
    const deliveryTemplateEnd = html.indexOf('</section>', deliveryTemplateStart);
    const deliveryTemplate = deliveryTemplateStart >= 0 && deliveryTemplateEnd > deliveryTemplateStart
        ? html.slice(deliveryTemplateStart, deliveryTemplateEnd)
        : '';

    assert.ok(deliveryTemplate, '音频交付卡模板应存在');
    assert.doesNotMatch(deliveryTemplate, /id="system-input-accept-btn"/);
    assert.match(deliveryTemplate, /id="system-input-start-btn"[^>]*>确认音频并开始录入/);
    assert.doesNotMatch(startHandler, /showSystemInputProgressPage\(\{ workspace, refresh: false \}\);\s*void runSystemInputDeliveryAction\('START_INPUT'\)/);
    assert.match(startHandler, /runSystemInputDeliveryAction\('ACCEPT_AUDIO_AND_START'\)/);
    assert.match(startHandler, /const inputRunMissing = !systemInput\.input_run;/);
    assert.match(startHandler, /if \(inputRunMissing && systemInput\.delivery_mode === 'audio_and_input' && acceptancePending\)/);
    assert.doesNotMatch(startHandler, /if \(inputStatus === 'not_enabled' && !systemInput\.input_run\)[\s\S]*?ACCEPT_AUDIO_AND_START/);
    assert.match(startHandler, /workspaceAction\('START_INPUT', workspace\)/);
    assert.match(pagesSource, /async function performSystemInputAcceptanceAndStart\(\)/);
    assert.match(pagesSource, /if \(actionType === 'ACCEPT_AUDIO_AND_START'\) return performSystemInputAcceptanceAndStart\(\);/);
    assert.match(pagesSource, /if \(!updated\) throw new Error\('服务端未返回更新后的工作区'\);\s*routeAfterSystemInputAudioAcceptance\(updated\);/);
    assert.match(bootstrapSource, /if \(systemInputRunIsComplete\(systemInput\) \|\| inputStatus === 'succeeded'\)/);
});

test('交付阶段导航不会绕过音频验收或提前打开最终结果', () => {
    const { api } = loadRendererConfigFunctions();

    assert.equal(api.deliveryStageInputHasStarted({
        delivery_mode: 'audio_and_input',
        audio_acceptance: { status: 'pending' },
        input_status: 'not_enabled',
    }), false);
    assert.equal(api.deliveryStageInputHasStarted({
        delivery_mode: 'audio_and_input',
        audio_acceptance: { status: 'accepted' },
        input_status: 'not_enabled',
    }), true);
    assert.equal(api.deliveryStageResultIsReady({
        delivery_mode: 'audio_and_input',
        input_status: 'not_enabled',
    }), false);
    assert.equal(api.deliveryStageResultIsReady({
        delivery_mode: 'audio_and_input',
        input_run: { status: 'RUNNING' },
        input_status: 'running',
    }), false);
    assert.equal(api.deliveryStageResultIsReady({
        delivery_mode: 'audio_and_input',
        input_run: { status: 'SUCCEEDED' },
        input_status: 'succeeded',
    }), true);
    assert.equal(api.deliveryStageResultIsReady({
        delivery_mode: 'audio_only',
        input_status: 'not_enabled',
    }), true);
});

test('最终结果已具备时在交付轨道显示成功态', () => {
    const { api } = loadRendererConfigFunctions();

    assert.deepEqual(JSON.parse(JSON.stringify(api.deliveryStageStatus(
        'result',
        { system_input: { delivery_mode: 'audio_only' } },
        'audio',
    ))), {
        state: 'complete',
        label: '可查看',
        detail: '汇总音频与系统录入结果',
    });
    assert.equal(api.deliveryStageStatus(
        'result',
        { system_input: { delivery_mode: 'audio_and_input', input_status: 'running', input_run: { status: 'RUNNING' } } },
        'audio',
    ).state, 'locked');
    assert.equal(api.deliveryStageStatus(
        'result',
        { system_input: { delivery_mode: 'audio_only' } },
        'result',
    ).state, 'active');
});

test('交付子页隐藏多余工具栏并保持音频返回入口可用', () => {
    const rendererDirectory = path.join(__dirname, '..', 'renderer');
    const stylesSource = fs.readFileSync(path.join(rendererDirectory, 'styles.css'), 'utf8');
    const navigationSource = fs.readFileSync(
        path.join(rendererDirectory, 'modules', 'app', 'navigation.js'),
        'utf8',
    );
    const subpageStyles = fs.readFileSync(path.join(rendererDirectory, 'task-subpages.css'), 'utf8');
    const bootstrapSource = fs.readFileSync(
        path.join(rendererDirectory, 'modules', 'app', 'bootstrap.js'),
        'utf8',
    );

    assert.doesNotMatch(stylesSource, /body\.platform-darwin #sidebar\s*\{[^}]*padding-top/);
    assert.doesNotMatch(stylesSource, /body\.platform-win32 #sidebar\s*\{[^}]*padding-top/);
    assert.match(subpageStyles, /body\.platform-darwin:has\(\.task-subpage\.active\) #sidebar\s*\{\s*padding-top: var\(--mac-titlebar-height\);/);
    assert.match(subpageStyles, /body\.platform-win32:has\(\.task-subpage\.active\) #sidebar\s*\{\s*padding-top: var\(--windows-titlebar-height\);/);
    assert.match(subpageStyles, /body:has\(\.task-subpage\.active\) #toolbar \{ display: none; \}/);
    assert.match(subpageStyles, /body:has\(\.task-subpage\.active\) \.app-main \{ grid-template-rows: minmax\(0, 1fr\); \}/);
    assert.match(navigationSource, /const canReturnToAudio = key === 'audio' && isTaskSubpage && activeWorkspace === 'delivery';/);
    assert.match(navigationSource, /if \(key === 'audio' && isTaskSubpage && activeWorkspace === 'delivery'\) \{\s*returnFromTaskSubpage\(\);/);
    assert.match(bootstrapSource, /const target = event\.target\?\.closest\?\.\('\.task-subpage-back'\) \|\| null;/);
});
