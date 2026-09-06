/**
 * Shared renderer context.
 *
 * The legacy renderer still exposes compatibility aliases for the current
 * feature code. New modules should read dependencies and cross-feature state
 * from this context instead of reaching into unrelated implementation files.
 */
(function attachRendererRuntimeContext(root) {
    'use strict';

    const registry = root.WORDTTS_RENDERER || {};
    const state = {};
    const bindState = (name, read, write) => {
        Object.defineProperty(state, name, {
            configurable: false,
            enumerable: true,
            get: read,
            set: write,
        });
    };

    [
        ['currentStep', () => currentStep, value => { currentStep = value; }],
        ['currentView', () => currentView, value => { currentView = value; }],
        ['activeWorkspace', () => activeWorkspace, value => { activeWorkspace = value; }],
        ['historyReturnStep', () => historyReturnStep, value => { historyReturnStep = value; }],
        ['historyRecords', () => historyRecords, value => { historyRecords = value; }],
        ['historyFilters', () => historyFilters, value => { historyFilters = value; }],
        ['activeResultContext', () => activeResultContext, value => { activeResultContext = value; }],
        ['latestCurrentResultEvent', () => latestCurrentResultEvent, value => { latestCurrentResultEvent = value; }],
        ['currentSession', () => currentSession, value => { currentSession = value; }],
        ['currentWorkspace', () => currentWorkspace, value => { currentWorkspace = value; }],
        ['activeWorkflowCandidates', () => activeWorkflowCandidates, value => { activeWorkflowCandidates = value; }],
        ['activeWorkflowListTruncated', () => activeWorkflowListTruncated, value => { activeWorkflowListTruncated = value; }],
        ['themePreference', () => themePreference, value => { themePreference = value; }],
        ['currentConfig', () => currentConfig, value => { currentConfig = value; }],
        ['voiceCatalog', () => voiceCatalog, value => { voiceCatalog = value; }],
        ['voiceAliasMap', () => voiceAliasMap, value => { voiceAliasMap = value; }],
        ['voiceFilterOptions', () => voiceFilterOptions, value => { voiceFilterOptions = value; }],
        ['activeVoiceFilter', () => activeVoiceFilter, value => { activeVoiceFilter = value; }],
        ['voiceFiltersExpanded', () => voiceFiltersExpanded, value => { voiceFiltersExpanded = value; }],
        ['activeVoiceRole', () => activeVoiceRole, value => { activeVoiceRole = value; }],
        ['voiceRoles', () => voiceRoles, value => { voiceRoles = value; }],
        ['roleVoiceMap', () => roleVoiceMap, value => { roleVoiceMap = value; }],
        ['voiceParamConfigs', () => voiceParamConfigs, value => { voiceParamConfigs = value; }],
        ['selectedDefaultFemaleVoice', () => selectedDefaultFemaleVoice, value => { selectedDefaultFemaleVoice = value; }],
        ['selectedDefaultMaleVoice', () => selectedDefaultMaleVoice, value => { selectedDefaultMaleVoice = value; }],
        ['reviewViewMode', () => reviewViewMode, value => { reviewViewMode = value; }],
        ['isGenerating', () => isGenerating, value => { isGenerating = value; }],
        ['generatedFiles', () => generatedFiles, value => { generatedFiles = value; }],
        ['updateState', () => updateState, value => { updateState = value; }],
    ].forEach(([name, read, write]) => bindState(name, read, write));

    const context = registry.context || {};
    context.modules = registry.modules || {};
    context.env = Object.freeze({ isElectron, platform });
    context.services = Object.freeze({
        api: workflowApi,
        store: workflowStore,
        storage: rendererStorage,
        reducer: workflowReducer,
        adapter: workflowAdapter,
        commandCoordinator: workflowCommandCoordinator,
    });
    context.state = state;
    registry.context = context;
    root.WORDTTS_RENDERER = registry;
})(typeof globalThis !== 'undefined' ? globalThis : window);
