/** Renderer module: systemInput.configuration */
(function attachRendererFeature_systemInput_configuration(root) {
    'use strict';

function systemInputOptionalNumber(value) {
    const text = String(value || '').trim();
    if (!text) return undefined;
    const number = Number(text);
    return Number.isFinite(number) ? number : text;
}

function systemInputAppTemplateForValue(value) {
    const rawValue = value && typeof value === 'object'
        ? (value.app_template_id || value.name || '')
        : value;
    const wantedId = String(rawValue || '').trim();
    if (wantedId) {
        const byId = systemInputTemplates.find(template => (
            String(template.app_template_id || '').trim() === wantedId
        ));
        if (byId) return byId;
    }
    const wanted = systemInputNormalizeLabel(rawValue);
    return systemInputTemplates.find(template => systemInputNormalizeLabel(template.name) === wanted) || null;
}

function systemInputSelectedAppTemplate(value = $('system-input-app-template')?.value || '') {
    const picker = systemInputPickerRegistry?.get?.('system-input-app-template');
    const selectedId = String(picker?.selectedValue || '').trim();
    if (selectedId) {
        const selected = systemInputTemplates.find(template => (
            String(template.app_template_id || '').trim() === selectedId
        ));
        if (selected) return selected;
    }
    return systemInputAppTemplateForValue(value);
}

function systemInputPaperNameForUnit(baseName) {
    // A unit label identifies the parsed content boundary, not the paper name
    // entered by the user. Keep this helper as the compatibility boundary for
    // callers that still pass the old unit/index arguments, but never derive
    // or rewrite a paper name from those arguments.
    return String(baseName ?? '').trim();
}

function systemInputSuggestedPaperName() {
    // Convenience default derived from the imported document name, so the
    // common case does not require retyping the same title. Deliberately
    // restricted to single-unit tasks by callers: multi-unit tasks must not
    // silently submit several same-named platform papers.
    const raw = String(
        (typeof currentSession !== 'undefined' && currentSession?.source_filename)
        || (typeof activeResultContext !== 'undefined' && activeResultContext?.sourceFilename)
        || (typeof currentWorkspace !== 'undefined' && currentWorkspace?.source_filename)
        || '',
    ).trim();
    if (!raw) return '';
    const base = raw.replace(/\.(docx?|xlsx|pdf)$/i, '').replace(/\s+/g, ' ').trim();
    return base ? base.slice(0, 80) : '';
}

function systemInputTemplateFieldValue(source, ...keys) {
    if (!source || typeof source !== 'object' || Array.isArray(source)) return undefined;
    return keys
        .map(key => source[key])
        .find(value => value !== undefined && value !== null);
}

function systemInputConfigurationValueKey(value) {
    if (Array.isArray(value)) {
        return JSON.stringify(value.map(item => systemInputConfigurationValueKey(item)).sort());
    }
    if (value && typeof value === 'object') {
        const identifier = value.id ?? value.value;
        const label = systemInputDisplayValue({
            name: value.name,
            label: value.label,
            text: value.text,
        });
        return JSON.stringify({
            id: identifier === undefined || identifier === null ? '' : String(identifier),
            label,
        });
    }
    return JSON.stringify(value);
}

function systemInputCommonConfiguration(sources, inputType = 'paper') {
    const units = Array.isArray(sources)
        ? sources.filter(source => source && typeof source === 'object' && !Array.isArray(source))
        : [];
    if (!units.length) return {};
    const normalizedType = String(inputType || 'paper').trim() || 'paper';
    const fields = normalizedType === 'textbook'
        ? [
            ['textbookNameZh', 'textbookNameZh', 'textbook_name_zh'],
            ['textbookNameEn', 'textbookNameEn', 'textbook_name_en'],
            ['textbookForm', 'textbookForm', 'textbook_form'],
            ['textbookVersion', 'textbookVersion', 'textbook_version'],
            ['textbookStage', 'textbookStage', 'textbook_stage'],
            ['textbookGrade', 'textbookGrade', 'textbook_grade'],
            ['textbookVolume', 'textbookVolume', 'textbook_volume'],
            ['textbookUnit', 'textbookUnit', 'textbook_unit'],
            ['textbookLesson', 'textbookLesson', 'textbook_lesson'],
        ]
        : normalizedType === 'paper'
            ? [
                ['paperCategory', 'paperCategory', 'paper_category'],
                ['paperType', 'paperType', 'paper_type'],
                ['provinceId', 'provinceId', 'province_id'],
                ['cityId', 'cityId', 'city_id'],
                ['districtIds', 'districtIds', 'district_ids'],
                ['stageId', 'stageId', 'stage_id'],
                ['gradeId', 'gradeId', 'grade_id'],
                ['year', 'year'],
                ['answerTimeMinutes', 'answerTimeMinutes', 'answer_time_minutes'],
                ['platformTemplateId', 'platformTemplateId', 'platform_template_id'],
                ['platformTemplateVersion', 'platformTemplateVersion', 'platform_template_version'],
                ['platformTemplateName', 'platformTemplateName', 'platform_template_name'],
            ]
            : [];
    const result = {};
    fields.forEach(([outputKey, ...keys]) => {
        const values = units.map(unit => systemInputTemplateFieldValue(unit, ...keys));
        if (values.some(value => value === undefined || value === null)) return;
        const firstKey = systemInputConfigurationValueKey(values[0]);
        if (values.every(value => systemInputConfigurationValueKey(value) === firstKey)) {
            result[outputKey] = systemInputCopy(values[0]);
        }
    });
    if (normalizedType === 'paper'
        && !systemInputCascadeAvailable('paper', 'paperType', { paperCategory: result.paperCategory })) {
        delete result.paperType;
    }
    return result;
}

function systemInputTemplateConfigurationForUnit(configuration, inputType = 'paper') {
    return systemInputCommonConfiguration([configuration], inputType);
}

function systemInputTemplateConfigurationKey(configuration) {
    const source = configuration && typeof configuration === 'object' && !Array.isArray(configuration)
        ? configuration
        : {};
    return JSON.stringify(
        Object.keys(source)
            .sort()
            .map(key => [key, systemInputConfigurationValueKey(source[key])]),
    );
}

function systemInputAppTemplateEntries(unitConfigurations, units = [], inputType = 'paper') {
    const sources = Array.isArray(unitConfigurations) ? unitConfigurations : [];
    const groups = [];
    const groupsByKey = new Map();
    sources.forEach((source, index) => {
        const configuration = systemInputTemplateConfigurationForUnit(source, inputType);
        const key = systemInputTemplateConfigurationKey(configuration);
        let group = groupsByKey.get(key);
        if (!group) {
            group = {
                key,
                configuration,
                unitIndexes: [],
                unitIds: [],
                unitLabels: [],
            };
            groupsByKey.set(key, group);
            groups.push(group);
        }
        const unit = Array.isArray(units) ? units[index] : null;
        const unitId = String(source?.unit_id || unit?.unit_id || '').trim();
        group.unitIndexes.push(index);
        if (unitId) group.unitIds.push(unitId);
        group.unitLabels.push(systemInputDisplayValue(unit?.label, `第${index + 1}套`));
    });
    return groups;
}

function systemInputAppTemplateNameForGroup(baseName, group, units = [], multiple = false) {
    const base = String(baseName || '').trim() || '应用配置';
    if (!multiple || !group?.unitIndexes?.length) return base;
    const labels = group.unitIndexes.map((index) => {
        const unit = Array.isArray(units) ? units[index] : null;
        return systemInputDisplayValue(unit?.label, `第${index + 1}套`);
    });
    const suffix = ` · ${labels.join('、')}`;
    const available = Math.max(1, 256 - suffix.length);
    return `${base.slice(0, available)}${suffix}`.slice(0, 256);
}

function systemInputAppTemplatePlatformReference(configuration) {
    const source = configuration && typeof configuration === 'object' && !Array.isArray(configuration)
        ? configuration
        : {};
    const reference = systemInputPlatformTemplateReference({
        platform_template_id: source.platformTemplateId || source.platform_template_id,
        name: source.platformTemplateName || source.platform_template_name,
        platform_template_version: source.platformTemplateVersion || source.platform_template_version,
    });
    return reference || {};
}

function systemInputMergeAppTemplateIntoUnit(
    unitConfiguration,
    templateConfiguration,
    platformTemplate = null,
    inputType = '',
) {
    const next = systemInputCopy(unitConfiguration);
    const assignedFields = new Set();
    const assign = (outputKey, ...keys) => {
        const value = systemInputTemplateFieldValue(templateConfiguration, ...keys);
        if (value !== undefined) {
            next[outputKey] = systemInputCopy(value);
            assignedFields.add(outputKey);
        }
    };
    // 课文页面字段同样可以由模板带入；同时兼容早期模板可能使用的
    // snake_case 键，避免“能保存但带入不完整”。试卷模板不含这些键时无副作用。
    [
        ['textbookNameZh', 'textbookNameZh', 'textbook_name_zh'],
        ['textbookNameEn', 'textbookNameEn', 'textbook_name_en'],
        ['textbookForm', 'textbookForm', 'textbook_form'],
        ['textbookVersion', 'textbookVersion', 'textbook_version'],
        ['textbookStage', 'textbookStage', 'textbook_stage'],
        ['textbookGrade', 'textbookGrade', 'textbook_grade'],
        ['textbookVolume', 'textbookVolume', 'textbook_volume'],
        ['textbookUnit', 'textbookUnit', 'textbook_unit'],
        ['textbookLesson', 'textbookLesson', 'textbook_lesson'],
    ].forEach(([outputKey, ...keys]) => assign(outputKey, ...keys));
    assign('paperCategory', 'paperCategory', 'paper_category');
    assign('provinceId', 'provinceId', 'province_id');
    assign('cityId', 'cityId', 'city_id');
    assign('districtIds', 'districtIds', 'district_ids');
    assign('stageId', 'stageId', 'stage_id');
    assign('gradeId', 'gradeId', 'grade_id');
    assign('year', 'year');
    assign('answerTimeMinutes', 'answerTimeMinutes', 'answer_time_minutes');
    const category = systemInputTemplateFieldValue(templateConfiguration, 'paperCategory', 'paper_category');
    if (systemInputCascadeAvailable('paper', 'paperType', { paperCategory: category })) {
        assign('paperType', 'paperType', 'paper_type');
    }
    else if (category !== undefined) delete next.paperType;
    if (platformTemplate) {
        const reference = systemInputPlatformTemplateReference(platformTemplate);
        if (reference?.name) {
            next.platformTemplateName = reference.name;
            assignedFields.add('platformTemplateName');
        }
        if (reference?.platform_template_id) {
            next.platformTemplateId = reference.platform_template_id;
            assignedFields.add('platformTemplateId');
        }
        else delete next.platformTemplateId;
        if (reference?.platform_template_version) {
            next.platformTemplateVersion = reference.platform_template_version;
            assignedFields.add('platformTemplateVersion');
        }
        else delete next.platformTemplateVersion;
    }
    const normalizedInputType = String(inputType || '').trim() || (
        Object.keys(templateConfiguration || {}).some(key => /^textbook(?:_|[A-Z])/.test(key))
            ? 'textbook'
            : 'paper'
    );
    systemInputCascadeResetUnselectedDescendants(
        normalizedInputType,
        next,
        assignedFields,
        { removeEmpty: true },
    );
    if (normalizedInputType === 'paper'
        && systemInputPlatformTemplateCatalog !== null
        && next.platformTemplateName) {
        const assessment = systemInputPlatformTemplateCatalogAssessment(next);
        if (assessment.status !== 'matched') {
            delete next.platformTemplateName;
            delete next.platformTemplateId;
            delete next.platformTemplateVersion;
        }
    }
    return next;
}

function collectSystemInputConfiguration(systemInput) {
    const inputType = $('system-input-type')?.value || systemInput?.input_type || 'paper';
    const appTemplateText = $('system-input-app-template')?.value.trim() || '';
    const selectedAppTemplate = systemInputSelectedAppTemplate(appTemplateText);
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    saveSystemInputCurrentUnitDraft(systemInput);
    const values = units.map((unit, index) => {
        const unitId = String(unit?.unit_id || '');
        const draft = systemInputUnitDrafts.get(unitId);
        if (draft && typeof draft === 'object') {
            return systemInputNormalizeUnitConfiguration(
                draft,
                unit,
                index,
                units.length,
                units,
                inputType,
            );
        }
        // A multi-unit drawer may have an incomplete in-memory draft map
        // after a mode switch or a renderer refresh. Never use the currently
        // visible unit's form as another unit's fallback; that would silently
        // overwrite the omitted set. Rehydrate the missing unit from the
        // authoritative projection and let validation ask the user to fill it
        // if it is not already complete.
        return systemInputNormalizeUnitConfiguration(
            systemInputUnitConfiguration(unit, systemInput),
            unit,
            index,
            units.length,
            units,
            inputType,
        );
    });
    const configuration = {
        delivery_mode: $('system-input-delivery-mode')?.value || 'audio_only',
        input_type: inputType,
        paper_category: inputType === 'paper'
            ? ($('system-input-paper-category')?.value || '题型专项')
            : undefined,
        units: values,
    };
    if (selectedAppTemplate?.app_template_id && (!units.length || values.every(unit => (
        String(unit?.app_template_id || '') === String(selectedAppTemplate.app_template_id)
    )))) {
        configuration.app_template_id = selectedAppTemplate.app_template_id;
    }
    return Object.fromEntries(Object.entries(configuration).filter(([, item]) => item !== undefined));
}

function collectSystemInputAppTemplateFormConfiguration() {
    const inputType = $('system-input-type')?.value || 'paper';
    if (inputType === 'textbook') {
        const fields = [
            ['textbookNameZh', 'system-input-textbook-name-zh'],
            ['textbookNameEn', 'system-input-textbook-name-en'],
            ['textbookForm', 'system-input-textbook-form'],
            ['textbookVersion', 'system-input-textbook-version'],
            ['textbookStage', 'system-input-textbook-stage'],
            ['textbookGrade', 'system-input-textbook-grade'],
            ['textbookVolume', 'system-input-textbook-volume'],
            ['textbookUnit', 'system-input-textbook-unit'],
            ['textbookLesson', 'system-input-textbook-lesson'],
        ];
        return Object.fromEntries(fields
            .map(([key, fieldId]) => [key, String($(fieldId)?.value || '').trim()])
            .filter(([, value]) => value));
    }
    if (inputType !== 'paper') return {};
    const category = $('system-input-paper-category')?.value || '题型专项';
    const platformTemplate = systemInputPlatformTemplateReference(systemInputSelectedPlatformTemplate());
    const platformTemplateName = systemInputPlatformTemplateNameForForm();
    const currentConfiguration = {
        paperCategory: category,
        provinceId: systemInputChoiceFromField('system-input-province', 'provinces'),
        cityId: systemInputChoiceFromField('system-input-city', 'cities'),
        districtIds: systemInputDistrictSelections.map(item => systemInputChoice(item)).filter(Boolean),
        stageId: systemInputChoiceFromField('system-input-stage', 'stages'),
        gradeId: systemInputChoiceFromField('system-input-grade', 'grades'),
        year: systemInputOptionalNumber($('system-input-year')?.value),
        answerTimeMinutes: systemInputOptionalNumber($('system-input-answer-time')?.value),
        platformTemplateId: platformTemplate?.platform_template_id || undefined,
        platformTemplateName: platformTemplateName || undefined,
        platformTemplateVersion: platformTemplate?.platform_template_version || undefined,
    };
    if (systemInputCascadeAvailable('paper', 'paperType', { paperCategory: category })) {
        currentConfiguration.paperType = systemInputChoiceFromText($('system-input-paper-type')?.value, 'paperTypes');
    }
    return Object.fromEntries(Object.entries(currentConfiguration).filter(([, item]) => item !== undefined && item !== null));
}

function collectSystemInputAppTemplateUnitConfigurations() {
    const inputType = $('system-input-type')?.value || 'paper';
    const systemInput = systemInputInteractionWorkspace()?.system_input;
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    if (!units.length) return [collectSystemInputAppTemplateFormConfiguration()];

    saveSystemInputCurrentUnitDraft(systemInput);
    return units.map((unit, index) => {
        const unitId = String(unit?.unit_id || '');
        const draft = systemInputUnitDrafts.get(unitId)
            || systemInputUnitConfiguration(unit, systemInput)
            || collectSystemInputUnitFromForm(systemInput, unit, index, units.length);
        return systemInputNormalizeUnitConfiguration(
            draft,
            unit,
            index,
            units.length,
            units,
            inputType,
        );
    });
}

function collectSystemInputAppTemplateConfiguration() {
    const inputType = $('system-input-type')?.value || 'paper';
    const configurations = collectSystemInputAppTemplateUnitConfigurations();
    return configurations.length > 1
        ? systemInputCommonConfiguration(configurations, inputType)
        : (configurations[0] || {});
}

function setSystemInputAppTemplateSelection(template) {
    const selected = template && typeof template === 'object' ? template : null;
    setSystemInputField('system-input-app-template', selected?.name || '');
    syncSystemInputPicker('system-input-app-template', {
        selectedValue: selected?.app_template_id || '',
    });
}

function systemInputAppTemplatePayload(inputType, name, configuration) {
    const platformTemplate = systemInputAppTemplatePlatformReference(configuration);
    // A template stores reusable configuration only. unit_id is rejected by
    // the backend outright, and the document-derived paper name must not be
    // stored either — applying a template never carries it (the confirm
    // dialog promises exactly that), so keeping it would just be a stale
    // document title waiting for a future merge path to forget the rule.
    const source = configuration && typeof configuration === 'object' ? configuration : {};
    const reusable = Object.fromEntries(Object.entries(source).filter(([key]) => (
        !['unit_id', 'entry_id', 'app_template_id', 'paperName', 'paper_name'].includes(key)
    )));
    return {
        input_type: inputType,
        name,
        configuration: reusable,
        platform_template_id: platformTemplate.platform_template_id || undefined,
        platform_template_name: platformTemplate.name || undefined,
        platform_template_version: platformTemplate.platform_template_version || undefined,
    };
}

async function applySystemInputAppTemplate() {
    const input = $('system-input-app-template');
    const template = systemInputSelectedAppTemplate(input?.value || '');
    if (!template) {
        showToast('请先搜索并选择一个应用配置模板', 'warning');
        return false;
    }
    const systemInput = systemInputInteractionWorkspace()?.system_input;
    const inputType = String(
        $('system-input-type')?.value || systemInput?.input_type || 'paper',
    ).trim() || 'paper';
    const templateType = String(template.input_type || '').trim();
    if (templateType && templateType !== inputType) {
        showToast(`当前是${inputType === 'textbook' ? '课文' : inputType === 'vocabulary' ? '词汇' : '试卷'}类型，不能应用${templateType === 'textbook' ? '课文' : templateType === 'vocabulary' ? '词汇' : '试卷'}模板`, 'warning');
        return false;
    }
    if (targetEditorActive()) return targetEditorApplyTemplate(template);
    const configuration = template.configuration && typeof template.configuration === 'object'
        ? template.configuration : {};
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const multiUnit = units.length > 1;
    const scope = multiUnit
        ? systemInputAppTemplateApplyScopeValue(systemInput)
        : 'current';
    if (multiUnit && !['all', 'selected'].includes(scope)) {
        showToast('请选择应用范围：勾选条目或全部单元。', 'warning');
        return false;
    }
    const applyToAll = multiUnit && scope === 'all';
    const targetUnits = systemInputAppTemplateTargetUnits(units, systemInputSelectedUnitId, scope);
    if (multiUnit && scope === 'selected' && !targetUnits.length) {
        showToast('请先在清单中勾选要带入方案的条目。', 'warning');
        return false;
    }
    const targetLabels = targetUnits.map((unit, index) => systemInputDisplayValue(unit?.label, `第${index + 1}套`));
    const confirmed = await showConfirmDialog({
        kicker: '应用配置模板',
        title: applyToAll
            ? '应用到全部录入单元？'
            : multiUnit
                ? '应用到勾选条目？'
                : '应用这个配置模板？',
        message: applyToAll
            ? `「${template.name}」的字段将带入全部 ${units.length} 套录入单元。`
            : multiUnit
                ? `「${template.name}」的字段将带入已勾选的 ${targetUnits.length} 个条目。`
                : `「${template.name}」的字段将带入当前录入目标。`,
        detail: applyToAll
            ? `受影响：${targetLabels.join('、')}。模板包含的配置字段会覆盖各单元当前值；试卷名称、文档内容、分值、答案和音频不会被模板带入。`
            : multiUnit
                ? `受影响：${targetLabels.join('、')}。其他单元保持不变；模板包含的配置字段会覆盖当前值，试卷名称、文档内容、分值、答案和音频不会被模板带入。`
                : '模板包含的配置字段会覆盖当前值；文档内容、分值、答案、音频和试卷名称不会被模板带入。',
        tone: applyToAll ? 'warning' : 'info',
        confirmLabel: applyToAll ? '应用到全部单元' : multiUnit ? '应用到勾选条目' : '应用模板',
    });
    if (!confirmed) return false;

    const platformTemplate = systemInputPlatformTemplateReference({
        platform_template_key: template.platform_template_key,
        platform_template_id: configuration.platformTemplateId
            || configuration.platform_template_id
            || template.platform_template_id,
        name: configuration.platformTemplateName
            || configuration.platform_template_name
            || template.platform_template_name,
        platform_template_version: configuration.platformTemplateVersion
            || configuration.platform_template_version
            || template.platform_template_version,
    });
    if (units.length) {
        saveSystemInputCurrentUnitDraft(systemInput);
        targetUnits.forEach((unit) => {
            if (!unit) return;
            const index = units.indexOf(unit);
            const unitId = String(unit?.unit_id || '');
            if (!unitId) return;
            const current = systemInputUnitDrafts.get(unitId)
                || systemInputUnitConfiguration(unit, systemInput);
            const merged = systemInputMergeAppTemplateIntoUnit(current, configuration, platformTemplate, inputType);
            if (template.app_template_id) merged.app_template_id = template.app_template_id;
            else delete merged.app_template_id;
            systemInputUnitDrafts.set(
                unitId,
                systemInputNormalizeUnitConfiguration(
                    merged,
                    unit,
                    index,
                    units.length,
                    units,
                    inputType,
                ),
            );
        });
        populateSystemInputUnitForm(systemInput);
        setSystemInputAppTemplateSelection(template);
        systemInputLastProvinceValue = $('system-input-province')?.value || '';
        systemInputLastCityValue = $('system-input-city')?.value || '';
        updateSystemInputFormVisibility();
        showToast(
            applyToAll
                ? `已将应用模板「${template.name}」带入全部 ${units.length} 套录入单元，请检查后保存`
                : multiUnit
                    ? `已将应用模板「${template.name}」带入 ${targetUnits.length} 个勾选条目，请检查后保存`
                    : `已带入应用模板「${template.name}」，请检查后保存`,
            'success',
        );
        return true;
    }

    const assign = (fieldId, ...keys) => {
        const value = keys.map(key => configuration[key]).find(value => value !== undefined && value !== null);
        if (value !== undefined) setSystemInputField(fieldId, systemInputDisplayValue(value));
    };
    assign('system-input-paper-category', 'paperCategory', 'paper_category');
    assign('system-input-province', 'provinceId', 'province_id');
    assign('system-input-city', 'cityId', 'city_id');
    assign('system-input-stage', 'stageId', 'stage_id');
    assign('system-input-grade', 'gradeId', 'grade_id');
    assign('system-input-year', 'year');
    assign('system-input-answer-time', 'answerTimeMinutes', 'answer_time_minutes');
    const category = $('system-input-paper-category')?.value || '题型专项';
    if (systemInputCascadeAvailable('paper', 'paperType', { paperCategory: category })) {
        assign('system-input-paper-type', 'paperType', 'paper_type');
    }
    else {
        setSystemInputField('system-input-paper-type', '');
        syncSystemInputPickerInput('system-input-paper-type-search', '', '');
    }
    if (($('system-input-type')?.value || 'paper') === 'textbook') {
        const textbookFields = [
            ['system-input-textbook-name-zh', 'textbookNameZh', 'textbook_name_zh'],
            ['system-input-textbook-name-en', 'textbookNameEn', 'textbook_name_en'],
            ['system-input-textbook-form', 'textbookForm', 'textbook_form'],
            ['system-input-textbook-version', 'textbookVersion', 'textbook_version'],
            ['system-input-textbook-stage', 'textbookStage', 'textbook_stage'],
            ['system-input-textbook-grade', 'textbookGrade', 'textbook_grade'],
            ['system-input-textbook-volume', 'textbookVolume', 'textbook_volume'],
            ['system-input-textbook-unit', 'textbookUnit', 'textbook_unit'],
            ['system-input-textbook-lesson', 'textbookLesson', 'textbook_lesson'],
        ];
        textbookFields.forEach(([fieldId, ...keys]) => {
            const value = keys.map(key => configuration[key])
                .find(candidate => candidate !== undefined && candidate !== null);
            if (value !== undefined) setSystemInputField(fieldId, String(value));
        });
    }
    const districts = configuration.districtIds ?? configuration.district_ids;
    if (Array.isArray(districts)) {
        systemInputDistrictSelections = districts.map(value => systemInputDistrictChoice(value) || systemInputChoice(value)).filter(Boolean);
        renderSystemInputDistrictChips();
    }
    if (platformTemplate) setSystemInputPlatformTemplateSelection(platformTemplate);
    setSystemInputAppTemplateSelection(template);
    systemInputLastProvinceValue = $('system-input-province')?.value || '';
    systemInputLastCityValue = $('system-input-city')?.value || '';
    renderSystemInputRegionOptions();
    updateSystemInputFormVisibility();
    showToast(`已带入应用模板「${template.name}」，请检查后保存配置`, 'success');
    return true;
}

async function saveSystemInputAppTemplate() {
    if (targetEditorActive()) return targetEditorSaveTemplate();
    if (systemInputTemplateBusy || !workflowApi?.createSystemInputTemplate) return false;
    if (!validateSystemInputConfigurationForm()) return false;
    const inputType = $('system-input-type')?.value || 'paper';
    const systemInput = systemInputInteractionWorkspace()?.system_input;
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const unitConfigurations = collectSystemInputAppTemplateUnitConfigurations();
    const entries = systemInputAppTemplateEntries(unitConfigurations, units, inputType);
    const isMultiTemplate = entries.length > 1;
    const defaultName = $('system-input-app-template')?.value.trim()
        || `${inputType === 'paper' ? '试卷' : inputType === 'textbook' ? '课文' : '词汇'}配置`;
    const name = await showPromptDialog(
        isMultiTemplate ? '保存多套应用模板' : '保存应用配置模板',
        isMultiTemplate
            ? `请输入模板组名称（将按 ${entries.length} 套不同配置分别保存，名称不同不会拆分）：`
            : '请输入模板名称：',
        defaultName,
    );
    if (!name || !name.trim()) return false;
    const button = $('system-input-save-template-btn');
    systemInputTemplateBusy = true;
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
    }
    const createdTemplates = [];
    const saveBatchId = Date.now();
    try {
        for (const [entryIndex, entry] of entries.entries()) {
            const templateName = systemInputAppTemplateNameForGroup(
                name.trim(),
                entry,
                units,
                isMultiTemplate,
            );
            const response = await workflowApi.createSystemInputTemplate(
                systemInputAppTemplatePayload(inputType, templateName, entry.configuration),
                { idempotencyKey: `renderer-system-input-template-${inputType}-${saveBatchId}-${entryIndex}` },
            );
            createdTemplates.push(response);
        }
        await loadSystemInputTemplates(inputType, { force: true });
        const createdByKey = new Map(entries.map((entry, index) => [
            entry.key,
            createdTemplates[index],
        ]));
        if (units.length) {
            entries.forEach(entry => {
                const template = createdByKey.get(entry.key);
                entry.unitIndexes.forEach(unitIndex => {
                    const unit = units[unitIndex];
                    const unitId = String(unit?.unit_id || unitConfigurations[unitIndex]?.unit_id || '').trim();
                    if (!unitId) return;
                    const next = systemInputCopy(unitConfigurations[unitIndex]);
                    if (template?.app_template_id) next.app_template_id = template.app_template_id;
                    systemInputUnitDrafts.set(unitId, next);
                });
            });
            const selectedIndex = units.findIndex(unit => (
                String(unit?.unit_id || '') === String(systemInputSelectedUnitId || '')
            ));
            const selectedEntry = entries.find(entry => entry.unitIndexes.includes(selectedIndex)) || entries[0];
            populateSystemInputUnitForm(systemInput);
            setSystemInputAppTemplateSelection(createdByKey.get(selectedEntry.key));
        } else {
            setSystemInputAppTemplateSelection(createdTemplates[0]);
        }
        if (isMultiTemplate) {
            showToast(`已按 ${entries.length} 套不同配置保存应用模板，请在对应录入单元中选择使用`, 'success');
        } else {
            showToast(`应用配置模板「${createdTemplates[0]?.name || name.trim()}」已保存`, 'success');
        }
        return true;
    } catch (error) {
        const message = workflowAdapter.issueMessage?.(error)?.message
            || `保存应用模板失败：${error.message || '请稍后重试'}`;
        const partial = createdTemplates.length
            ? `已保存 ${createdTemplates.length} / ${entries.length} 条，请检查模板列表。`
            : '';
        showToast([message, partial].filter(Boolean).join(' '), 'error');
        return false;
    } finally {
        systemInputTemplateBusy = false;
        if (button) {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    }
}

function applySystemInputWorkspaceResponse(response) {
    const workspace = response?.workspace;
    if (isHistoryResultView()) return updateHistoryResultWorkspace(workspace);
    if (!workspace?.snapshot?.workflow_id || String(workspace.snapshot.workflow_id) !== String(currentSession?.session_id || '')) return null;
    currentWorkspace = workspace;
    mergeWorkflowSnapshotIntoSession(workspace.snapshot, currentSession);
    workflowStore?.hydrate?.(workspace, { snapshot: workspace.snapshot });
    if (typeof renderWorkspaceAfterHydrate === 'function') {
        renderWorkspaceAfterHydrate(workspace, workspace.snapshot, currentSession?.session_id);
    } else {
        renderWorkspaceShell(workspace, workspace.snapshot);
    }
    return workspace;
}

async function submitSystemInputConfiguration(event) {
    event?.preventDefault?.();
    const historyContext = isHistoryResultView() ? activeResultContext : null;
    const workflowId = historyResultWorkflowId(historyContext) || String(currentSession?.session_id || '');
    if (systemInputConfigurationBusy || !workflowApi?.saveSystemInputConfiguration || !workflowId) return false;
    if (!targetEditorCanSave() || !targetEditorPrepareSave() || !validateSystemInputConfigurationForm() || !targetEditorValidateCatalog()) return false;
    const button = $('system-input-save-btn');
    let workspace = historyContext?.workspace || authoritativeWorkspace(workflowId, currentWorkspace);
    if (!workspace?.snapshot) {
        workspace = historyContext
            ? await workflowApi.getWorkspace(workflowId)
            : await hydrateWorkflowWorkspace(workflowId, { silent: false });
    }
    const expectedStateVersion = Number(workspace?.snapshot?.state_version ?? currentSession?.state_version ?? 0);
    if (!Number.isInteger(expectedStateVersion) || expectedStateVersion < 0) {
        showToast('任务版本缺失，暂时不能保存录入目标', 'error');
        return false;
    }
    const configuration = collectSystemInputConfiguration(workspace?.system_input || {});
    // The drawer is only for configuring a system-input target. Saving it
    // must enable the corresponding delivery path; switching back to
    // audio-only remains an explicit action in the delivery choice panel.
    configuration.delivery_mode = 'audio_and_input';
    const revisionValue = workspace?.configuration?.configuration_revision
        ?? workspace?.system_input?.entries?.[0]?.configuration_revision;
    const configurationRevision = Number.isInteger(Number(revisionValue)) ? Number(revisionValue) : undefined;
    systemInputConfigurationBusy = true;
    targetEditorSetBusy(true);
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
    }
    try {
        const response = await workflowApi.saveSystemInputConfiguration(workflowId, {
            expected_state_version: expectedStateVersion,
            ...(configurationRevision ? { configuration_revision: configurationRevision } : {}),
            configuration,
        }, { idempotencyKey: `renderer-system-input-config-${workflowId}-${expectedStateVersion}` });
        let updated = applySystemInputWorkspaceResponse(response);
        if (!updated && historyContext && typeof workflowApi.getWorkspace === 'function') {
            updated = updateHistoryResultWorkspace(await workflowApi.getWorkspace(workflowId), historyContext);
        }
        if (!updated) throw new Error('服务端未返回更新后的工作区');
        closeSystemInputConfigDrawer({ preserveDraft: false, committed: true });
        showToast('录入目标已保存', 'success');
        return true;
    } catch (error) {
        showToast(workflowAdapter.issueMessage?.(error)?.message || `保存录入目标失败：${error.message || '请稍后重试'}`, 'error');
        if (historyContext) await refreshHistoryResultWorkspace(historyContext, { silent: true });
        else await hydrateWorkflowWorkspace(workflowId, { silent: true });
        return false;
    } finally {
        systemInputConfigurationBusy = false;
        targetEditorSetBusy(false);
        if (button) {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    }
}


registerRendererModule("systemInput.configuration", {
    systemInputOptionalNumber,
    systemInputAppTemplateForValue,
    systemInputSelectedAppTemplate,
    systemInputPaperNameForUnit,
    systemInputSuggestedPaperName,
    systemInputTemplateFieldValue,
    systemInputConfigurationValueKey,
    systemInputCommonConfiguration,
    systemInputTemplateConfigurationForUnit,
    systemInputTemplateConfigurationKey,
    systemInputAppTemplateEntries,
    systemInputAppTemplateNameForGroup,
    systemInputAppTemplatePlatformReference,
    systemInputMergeAppTemplateIntoUnit,
    collectSystemInputConfiguration,
    collectSystemInputAppTemplateUnitConfigurations,
    collectSystemInputAppTemplateConfiguration,
    setSystemInputAppTemplateSelection,
    systemInputAppTemplatePayload,
    applySystemInputAppTemplate,
    saveSystemInputAppTemplate,
    applySystemInputWorkspaceResponse,
    submitSystemInputConfiguration,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
