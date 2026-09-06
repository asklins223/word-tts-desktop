/** Renderer module: systemInput.regions */
(function attachRendererFeature_systemInput_regions(root) {
    'use strict';

function systemInputRegionNode(node, path = []) {
    if (!node || typeof node !== 'object') return null;
    const name = systemInputDisplayValue(node);
    if (!name) return null;
    const id = node.id ?? node.value;
    if (id === undefined || id === null) return null;
    return {
        id,
        name,
        level: Number(node.level) || path.length + 1,
        parentId: node.parentId ?? node.parent_id ?? null,
        path: [...path, name],
    };
}

function flattenSystemInputRegions(value) {
    const roots = Array.isArray(value) ? value : (Array.isArray(value?.data) ? value.data : []);
    const indexes = { provinces: [], cities: [], districts: [] };
    const visit = (node, path, inheritedLevel = 0) => {
        const current = systemInputRegionNode(node, path);
        if (!current) return;
        const level = Number(current.level) || inheritedLevel + 1;
        current.level = level;
        const target = level === 1 ? indexes.provinces : level === 2 ? indexes.cities : indexes.districts;
        target.push(current);
        const children = Array.isArray(node.children) ? node.children : [];
        children.forEach(child => visit(child, current.path, level));
    };
    roots.forEach(node => visit(node, []));
    const unique = (items) => [...new Map(items.map(item => [String(item.id), item])).values()];
    indexes.provinces = unique(indexes.provinces);
    indexes.cities = unique(indexes.cities);
    indexes.districts = unique(indexes.districts);
    return indexes;
}

function systemInputRegionRoots(value) {
    if (Array.isArray(value)) return value;
    return Array.isArray(value?.data) ? value.data : [];
}

function systemInputRegionPayload(value) {
    const roots = systemInputRegionRoots(value);
    if (!roots.length) return null;
    const indexes = flattenSystemInputRegions(roots);
    // Do not accept a response that merely has a JSON object shape. A partial
    // or error payload would otherwise replace a valid snapshot with an empty
    // picker and make every dependent field look unavailable.
    if (!indexes.provinces.length || !indexes.cities.length) return null;
    return { roots, indexes };
}

function systemInputRegionMatches(value, item) {
    if (!item) return false;
    const valueObject = value && typeof value === 'object' && !Array.isArray(value) ? value : null;
    const valueId = valueObject ? valueObject.id ?? valueObject.value : undefined;
    const itemId = item.id;
    // Region labels are not a stable key: the same city name can occur under
    // different provinces. When both sides have IDs, never fall back to a
    // same-label match, or a restored choice can open the wrong branch.
    const hasId = value => value !== undefined && value !== null && String(value).trim() !== '';
    if (hasId(valueId) && hasId(itemId)) {
        return String(valueId) === String(itemId);
    }
    const display = typeof systemInputDisplayValue === 'function'
        ? systemInputDisplayValue(value)
        : value;
    const wanted = systemInputNormalizeLabel(display);
    if (!wanted) return false;
    const labels = [item.name, item.path?.join(' / '), item.path?.at(-1)];
    return labels.some(label => systemInputNormalizeLabel(label) === wanted);
}

function systemInputFindRegionInIndexes(value, kind, indexes = systemInputRegionIndexes, options = {}) {
    const entries = Array.isArray(indexes?.[kind]) ? indexes[kind] : [];
    const hasParent = Object.prototype.hasOwnProperty.call(options, 'parentId');
    return entries.find(item => {
        if (hasParent && String(item.parentId) !== String(options.parentId)) return false;
        return systemInputRegionMatches(value, item);
    }) || null;
}

function systemInputFindRegion(value, kind, options = {}) {
    return systemInputFindRegionInIndexes(value, kind, systemInputRegionIndexes, options);
}

function systemInputRegionCascadeState({
    provinceText = '',
    cityText = '',
    stageText = '',
    indexes = systemInputRegionIndexes,
} = {}) {
    const regionIndexes = indexes && typeof indexes === 'object' ? indexes : {};
    const selectedProvince = systemInputFindRegionInIndexes(provinceText, 'provinces', regionIndexes);
    const cities = selectedProvince
        ? (Array.isArray(regionIndexes.cities) ? regionIndexes.cities : [])
            .filter(item => String(item.parentId) === String(selectedProvince.id))
        : [];
    const selectedCity = selectedProvince
        ? systemInputFindRegionInIndexes(cityText, 'cities', regionIndexes, { parentId: selectedProvince.id })
        : null;
    const districts = selectedCity
        ? (Array.isArray(regionIndexes.districts) ? regionIndexes.districts : [])
            .filter(item => String(item.parentId) === String(selectedCity.id))
        : [];
    const selectedStage = systemInputFindFixedChoice(stageText, 'stages');
    const grades = selectedStage
        ? SYSTEM_INPUT_GRADES.filter(item => item.stageId === selectedStage.id)
        : [];
    return {
        selectedProvince,
        selectedCity,
        selectedStage,
        cities,
        districts,
        grades,
    };
}

function renderSystemInputRegionOptions() {
    const cascade = systemInputRegionCascadeState({
        provinceText: $('system-input-province')?.value || '',
        cityText: $('system-input-city')?.value || '',
        stageText: $('system-input-stage')?.value || '',
    });
    setSystemInputPickerOptions('system-input-province', systemInputRegionIndexes.provinces);
    setSystemInputPickerOptions('system-input-city', cascade.cities);
    setSystemInputPickerOptions('system-input-districts', cascade.districts);
    // District selections can be restored before the async region snapshot
    // finishes loading. Rebind those display-only values to their stable
    // region IDs as soon as the matching city index becomes available.
    if (typeof systemInputRefreshDistrictSelectionIdentities === 'function') {
        systemInputRefreshDistrictSelectionIdentities();
    }
    setSystemInputPickerOptions('system-input-stage', SYSTEM_INPUT_STAGES);
    setSystemInputPickerOptions('system-input-grade', cascade.grades);
    setSystemInputPickerOptions('system-input-paper-type-search', SYSTEM_INPUT_PAPER_TYPES);
    setSystemInputPickerEnabled('system-input-city', Boolean(cascade.selectedProvince), {
        enabledPlaceholder: '搜索城市',
        disabledPlaceholder: '先选择省份，再搜索城市',
    });
    setSystemInputPickerEnabled('system-input-districts', Boolean(cascade.selectedCity), {
        enabledPlaceholder: '搜索区县，点击选项添加',
        disabledPlaceholder: '先选择城市，再搜索区县',
    });
    setSystemInputPickerEnabled('system-input-grade', Boolean(cascade.selectedStage), {
        enabledPlaceholder: '搜索年级',
        disabledPlaceholder: '先选择学段，再搜索年级',
    });
    return cascade;
}

function handleSystemInputProvinceChange() {
    const input = $('system-input-province');
    const value = input?.value || '';
    if (systemInputNormalizeLabel(value) !== systemInputNormalizeLabel(systemInputLastProvinceValue)) {
        refreshSystemInputPlatformTemplatesForScope({ clear: true });
        const values = {
            provinceId: value,
            cityId: $('system-input-city')?.value || '',
            districtIds: systemInputDistrictSelections,
        };
        const cleared = systemInputCascadeResetDescendants('paper', values, 'provinceId');
        if (cleared.includes('cityId')) {
            setSystemInputField('system-input-city', '');
            systemInputLastCityValue = '';
        }
        if (cleared.includes('districtIds')) {
            systemInputDistrictSelections = [];
            renderSystemInputDistrictChips();
        }
    }
    systemInputLastProvinceValue = value;
    renderSystemInputRegionOptions();
    refreshSystemInputPlatformTemplatesForScope();
}

function handleSystemInputCityChange() {
    const input = $('system-input-city');
    const value = input?.value || '';
    if (systemInputNormalizeLabel(value) !== systemInputNormalizeLabel(systemInputLastCityValue)) {
        refreshSystemInputPlatformTemplatesForScope({ clear: true });
        const values = {
            cityId: value,
            districtIds: systemInputDistrictSelections,
        };
        const cleared = systemInputCascadeResetDescendants('paper', values, 'cityId');
        if (cleared.includes('districtIds')) {
            systemInputDistrictSelections = [];
            renderSystemInputDistrictChips();
        }
    }
    systemInputLastCityValue = value;
    renderSystemInputRegionOptions();
    refreshSystemInputPlatformTemplatesForScope();
}

function handleSystemInputStageChange() {
    const stage = systemInputFindFixedChoice($('system-input-stage')?.value || '', 'stages');
    const grade = systemInputFindFixedChoice($('system-input-grade')?.value || '', 'grades');
    const gradeMismatch = !stage || (grade && grade.stageId !== stage.id);
    if (gradeMismatch) {
        setSystemInputField('system-input-grade', '');
        systemInputCascadeResetDescendants('paper', {
            stageId: stage?.id || '',
            gradeId: grade?.id || '',
        }, 'stageId');
    }
    renderSystemInputRegionOptions();
    if (systemInputPlatformTemplateKind() === 'paper') {
        refreshSystemInputPlatformTemplatesForScope({ clear: true });
    } else {
        refreshSystemInputPlatformTemplatesForScope();
    }
}

function handleSystemInputGradeChange() {
    if (systemInputPlatformTemplateKind() === 'paper') {
        refreshSystemInputPlatformTemplatesForScope({ clear: true });
    } else {
        refreshSystemInputPlatformTemplatesForScope();
    }
}

async function loadSystemInputRegions() {
    if (systemInputRegionTree.length || systemInputRegionIndexes.provinces.length) {
        systemInputRegionLoadState = 'ready';
        renderSystemInputRegionOptions();
        return systemInputRegionIndexes;
    }
    if (systemInputRegionLoadPromise) return systemInputRegionLoadPromise;
    systemInputRegionLoadState = 'loading';
    systemInputRegionLoadError = '';
    systemInputRegionLoadPromise = (async () => {
        let value = null;
        try {
            if (typeof window.electronAPI?.readRegionTree === 'function') {
                value = await window.electronAPI.readRegionTree();
            }
            let payload = systemInputRegionPayload(value);
            // Electron reads the bundled snapshot through IPC. The browser
            // preview and a recovery renderer can still use the same static
            // asset when IPC is absent or returns an error envelope.
            if (!payload && typeof fetch === 'function') {
                const response = await fetch('data/region-tree.json', { cache: 'force-cache' });
                if (response.ok) payload = systemInputRegionPayload(await response.json());
            }
            if (!payload) throw new Error('省市区数据快照为空或格式不完整');
            systemInputRegionTree = payload.roots;
            systemInputRegionIndexes = payload.indexes;
            systemInputRegionLoadState = 'ready';
            renderSystemInputRegionOptions();
            // The drawer can be opened before the asynchronous IPC completes.
            // Refresh both the visible form and any open target modal so the
            // newly available sibling options appear without another reopen.
            root.targetEditorRefreshAfterDataLoad?.();
            return systemInputRegionIndexes;
        } catch (error) {
            systemInputRegionLoadState = 'error';
            systemInputRegionLoadError = String(error?.message || '省市区数据加载失败');
            // Keep a previously valid snapshot if a forced retry fails.
            if (!systemInputRegionIndexes.provinces.length) renderSystemInputRegionOptions();
            return systemInputRegionIndexes;
        } finally {
            systemInputRegionLoadPromise = null;
        }
    })();
    return systemInputRegionLoadPromise;
}

async function loadSystemInputTemplates(inputType = $('system-input-type')?.value || 'paper', { force = false } = {}) {
    const type = String(inputType || 'paper').trim() || 'paper';
    if (!workflowApi?.listSystemInputTemplates) return;
    if (!force && systemInputTemplatesType === type) return;
    const requestId = ++systemInputTemplateRequestId;
    systemInputTemplatesType = type;
    systemInputTemplates = [];
    try {
        const templates = await workflowApi.listSystemInputTemplates(type);
        if (requestId !== systemInputTemplateRequestId || systemInputTemplatesType !== type) return;
        systemInputTemplates = Array.isArray(templates) ? templates.slice(0, 256) : [];
        let recent = '';
        try { recent = rendererStorage?.getItem(`wordtts.target-recent.${type}`) || ''; } catch (_) { /* optional preference */ }
        systemInputTemplates.sort((left, right) => Number(String(right.app_template_id) === recent) - Number(String(left.app_template_id) === recent));
        setSystemInputPickerOptions('system-input-app-template', systemInputTemplates.map(template => {
            const name = String(template.name || '').trim();
            const platformName = String(template.platform_template_name || '').trim();
            const suffix = `${String(template.app_template_id) === recent ? '最近使用 · ' : ''}${platformName ? `平台题型模板：${platformName}` : '本机保存的录入配置'}`;
            return {
                value: String(template.app_template_id || name),
                label: name,
                detail: suffix,
                raw: template,
            };
        }));
    } catch (_) {
        // Template suggestions are optional. The page itself remains the
        // authority for the template choice when the read-only list is down.
    }
}


registerRendererModule("systemInput.regions", {
    systemInputRegionNode,
    flattenSystemInputRegions,
    systemInputRegionRoots,
    systemInputRegionPayload,
    systemInputRegionMatches,
    systemInputFindRegionInIndexes,
    systemInputFindRegion,
    systemInputRegionCascadeState,
    renderSystemInputRegionOptions,
    handleSystemInputProvinceChange,
    handleSystemInputCityChange,
    handleSystemInputStageChange,
    handleSystemInputGradeChange,
    loadSystemInputRegions,
    loadSystemInputTemplates,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
