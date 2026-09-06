/** Renderer module: systemInput.disclosures */
(function attachRendererFeature_systemInput_disclosures(root) {
    'use strict';

function bindSystemInputDisclosures(root) {
    if (!root) return;
    root.querySelectorAll('details[data-system-input-disclosure]').forEach(section => {
        const summary = section.querySelector('summary');
        if (!summary || section.dataset.systemInputDisclosureBound === 'true') return;
        const syncExpandedState = () => {
            summary.setAttribute('aria-expanded', section.open ? 'true' : 'false');
        };
        section.addEventListener('toggle', () => {
            if (section.id === 'system-input-unit-section' && $('system-input-drawer')?.classList.contains('is-workspace')) section.open = true;
            syncExpandedState();

        });
        section.dataset.systemInputDisclosureBound = 'true';
        syncExpandedState();
    });
}

function systemInputDisclosureStatus(id, value, state = '') {
    const element = $(id);
    if (!element) return;
    element.textContent = value;
    element.classList.toggle('is-complete', state === 'complete');
    element.classList.toggle('is-attention', state === 'attention');
}

function systemInputDisclosureDetail(id, value) {
    const element = $(id);
    if (!element) return;
    const text = String(value || '').trim();
    element.textContent = text;
    element.hidden = !text;
    if (text) element.title = text;
    else element.removeAttribute('title');
}

function systemInputDisclosureSummaryValue(configuration, ...keys) {
    for (const key of keys) {
        const value = systemInputDisplayValue(configuration?.[key]);
        if (value) return value;
    }
    return '';
}

function systemInputDisclosureSummaryJoin(values) {
    return values.map(value => String(value || '').trim()).filter(Boolean).join(' · ');
}

function systemInputDisclosureSummaryWithMissing(values, missing = []) {
    const configured = systemInputDisclosureSummaryJoin(values);
    const pending = missing.length ? `待填：${missing.join('、')}` : '';
    return [configured, pending].filter(Boolean).join(' · ');
}

function systemInputPaperDisclosureDetail(configuration, missing = []) {
    const templateLabels = new Set(['平台题型模板', '专项题型模板', '试卷模板']);
    return systemInputDisclosureSummaryWithMissing([
        systemInputDisclosureSummaryValue(configuration, 'paperName', 'paper_name'),
        systemInputDisclosureSummaryValue(configuration, 'paperCategory', 'paper_category'),
        systemInputDisclosureSummaryValue(configuration, 'platformTemplateName', 'platform_template_name'),
    ], missing.filter(label => ['试卷名称', '试卷分类'].includes(label) || templateLabels.has(label)));
}

function systemInputRangeDisclosureDetail(configuration, missing = []) {
    const missingSet = new Set(missing);
    const valueOrMissing = (label, value, suffix = '') => value
        ? `${value}${suffix}`
        : missingSet.has(label) ? `${label}待填` : '';
    const districtValues = configuration?.districtIds ?? configuration?.district_id;
    const districts = (Array.isArray(districtValues) ? districtValues : [districtValues])
        .map(value => systemInputDisplayValue(value))
        .filter(Boolean);
    const values = [
        valueOrMissing('省份', systemInputDisclosureSummaryValue(configuration, 'provinceId', 'province_id')),
        valueOrMissing('城市', systemInputDisclosureSummaryValue(configuration, 'cityId', 'city_id')),
        districts.length ? districts.join('、') : '',
        valueOrMissing('学段', systemInputDisclosureSummaryValue(configuration, 'stageId', 'stage_id')),
        valueOrMissing('年级', systemInputDisclosureSummaryValue(configuration, 'gradeId', 'grade_id')),
        valueOrMissing('年份', systemInputDisclosureSummaryValue(configuration, 'year'), '年'),
        valueOrMissing('答题时间', systemInputDisclosureSummaryValue(configuration, 'answerTimeMinutes', 'answer_time_minutes'), '分钟'),
    ];
    return systemInputDisclosureSummaryJoin(values);
}

function systemInputUnitDisclosureDetail(presentations) {
    const incomplete = presentations.filter(presentation => !presentation.complete);
    if (!incomplete.length) return presentations.map(presentation => presentation.label).join(' · ');
    return incomplete.map(presentation => {
        const missing = presentation.missing.slice(0, 3).join('、');
        const suffix = presentation.missing.length > 3 ? '…' : '';
        return `${presentation.label}：${missing}${suffix}`;
    }).join('；');
}

function systemInputDisclosureConfigurations(systemInput) {
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const inputType = $('system-input-type')?.value || systemInput?.input_type || 'paper';
    return units.map((unit, index) => {
        const unitId = String(unit?.unit_id || '');
        if (!unitId) return null;
        const configuration = systemInputUnitDrafts.get(unitId)
            || systemInputUnitConfiguration(unit, systemInput);
        return {
            unitId,
            configuration: systemInputNormalizeUnitConfiguration(
                configuration,
                unit,
                index,
                units.length,
                units,
                inputType,
            ),
        };
    }).filter(Boolean);
}

function renderSystemInputDisclosureSummaries(systemInput) {
    if (!systemInput) return;
    const inputType = $('system-input-type')?.value || systemInput.input_type || 'paper';
    const typeLabel = inputType === 'textbook' ? '课文' : inputType === 'vocabulary' ? '词汇' : '试卷';
    systemInputDisclosureStatus(
        'system-input-type-summary',
        typeLabel,
        inputType === 'vocabulary' ? 'attention' : 'complete',
    );
    const appTemplate = String($('system-input-app-template')?.value || '').trim();
    systemInputDisclosureStatus(
        'system-input-optional-summary',
        appTemplate ? '已选择模板' : '可选',
        appTemplate ? 'complete' : '',
    );
    systemInputDisclosureDetail('system-input-optional-detail', appTemplate);

    const configurations = systemInputDisclosureConfigurations(systemInput);
    const selectedId = String(systemInputSelectedUnitId || configurations[0]?.unitId || '');
    const selected = configurations.find(item => item.unitId === selectedId) || configurations[0];
    const selectedMissing = systemInputUnitMissingFields(selected?.configuration || {}, inputType);
    const textbookApplicable = inputType === 'textbook';
    systemInputDisclosureStatus(
        'system-input-textbook-summary',
        !textbookApplicable ? '不适用' : selectedMissing.length ? `待补充 ${selectedMissing.length} 项` : '必填已补齐',
        !textbookApplicable ? '' : selectedMissing.length ? 'attention' : 'complete',
    );
    systemInputDisclosureDetail(
        'system-input-textbook-detail',
        textbookApplicable
            ? systemInputDisclosureSummaryWithMissing([], selectedMissing)
            : '课文和词汇不使用课文分类信息字段。',
    );
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const unitPresentations = configurations.map((item, index) => {
        const unit = units.find(candidate => String(candidate?.unit_id || '') === item.unitId) || units[index];
        return systemInputUnitStatusPresentation(unit, item.configuration, index, units.length);
    });
    const completeUnitCount = unitPresentations.filter(presentation => presentation.complete).length;
    systemInputDisclosureStatus(
        'system-input-unit-progress',
        `${completeUnitCount} / ${unitPresentations.length} 已补齐`,
        completeUnitCount === unitPresentations.length ? 'complete' : 'attention',
    );
    systemInputDisclosureDetail('system-input-unit-detail', systemInputUnitDisclosureDetail(unitPresentations));
    const paperFields = new Set(['试卷名称', '试卷分类', '平台题型模板']);
    const rangeFields = new Set(['省份', '城市', '学段', '年级', '年份', '答题时间']);
    const paperMissing = selectedMissing.filter(label => paperFields.has(label));
    const rangeMissing = selectedMissing.filter(label => rangeFields.has(label));
    const paperApplicable = inputType === 'paper';
    const paperStatus = !paperApplicable
        ? '不适用'
        : paperMissing.length ? `待补充 ${paperMissing.length} 项` : '必填已补齐';
    systemInputDisclosureStatus(
        'system-input-paper-summary',
        paperStatus,
        !paperApplicable ? '' : paperMissing.length ? 'attention' : 'complete',
    );
    systemInputDisclosureDetail(
        'system-input-paper-detail',
        paperApplicable
            ? systemInputPaperDisclosureDetail(selected?.configuration || {}, paperMissing)
            : '当前录入类型使用自己的配置模型，试卷字段不会被带入。',
    );
    systemInputDisclosureStatus(
        'system-input-range-summary',
        !paperApplicable ? '不适用' : rangeMissing.length ? `待补充 ${rangeMissing.length} 项` : '必填已补齐',
        !paperApplicable ? '' : rangeMissing.length ? 'attention' : 'complete',
    );
    systemInputDisclosureDetail(
        'system-input-range-detail',
        paperApplicable
            ? systemInputRangeDisclosureDetail(selected?.configuration || {}, rangeMissing)
            : inputType === 'textbook'
                ? '课文录入不使用省市区、年份等试卷字段；课文分类信息在上方单独填写。'
                : '词汇的页面字段将在对应适配器接入时单独定义。',
    );

    const category = String($('system-input-paper-category')?.value || selected?.configuration?.paperCategory || '').trim();
    const paperTypeAvailable = paperApplicable
        && systemInputCascadeAvailable('paper', 'paperType', { paperCategory: category });
    const paperTypeMissing = paperTypeAvailable && selectedMissing.includes('考试类型');
    systemInputDisclosureStatus(
        'system-input-paper-type-summary',
        !paperApplicable ? '不适用' : paperTypeMissing ? '待补充' : '必填已补齐',
        !paperApplicable ? '' : paperTypeMissing ? 'attention' : 'complete',
    );
    systemInputDisclosureDetail(
        'system-input-paper-type-detail',
        paperApplicable
            ? systemInputDisclosureSummaryWithMissing([
                systemInputDisclosureSummaryValue(selected?.configuration || {}, 'paperType', 'paper_type'),
            ], paperTypeMissing ? ['考试类型'] : [])
            : '课文和词汇不使用试卷考试类型字段。',
    );
}

function resetSystemInputDisclosureState(systemInput) {
    const configurations = systemInputDisclosureConfigurations(systemInput);
    const selectedId = String(systemInputSelectedUnitId || configurations[0]?.unitId || '');
    const selected = configurations.find(item => item.unitId === selectedId) || configurations[0];
    const inputType = $('system-input-type')?.value || systemInput?.input_type || 'paper';
    const selectedMissing = systemInputUnitMissingFields(selected?.configuration || {}, inputType);
    const rangeFields = new Set(['省份', '城市', '学段', '年级', '年份', '答题时间']);
    const category = String($('system-input-paper-category')?.value || selected?.configuration?.paperCategory || '').trim();
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const anyIncompleteUnit = configurations.some(item => systemInputUnitMissingFields(item.configuration, inputType).length > 0);
    const defaults = {
        'system-input-optional-fields': false,
        'system-input-unit-section': units.length > 1,
        'system-input-type-fields': false,
        'system-input-paper-fields': inputType === 'paper',
        'system-input-textbook-fields': inputType === 'textbook' && selectedMissing.length > 0,
        'system-input-range-fields': inputType === 'paper' && selectedMissing.some(label => rangeFields.has(label)),
        'system-input-paper-type-section': inputType === 'paper'
            && systemInputCascadeAvailable('paper', 'paperType', { paperCategory: category })
            && selectedMissing.includes('考试类型'),
    };
    Object.entries(defaults).forEach(([id, open]) => {
        const section = $(id);
        if (section?.tagName === 'DETAILS') section.open = Boolean(open);
    });
}

function openSystemInputDisclosureForField(fieldId) {
    const field = $(fieldId);
    const section = field?.closest('details[data-system-input-disclosure]');
    if (section) section.open = true;
}

function systemInputValidationContainer(fieldId) {
    const input = $(fieldId);
    const picker = document.querySelector(`[data-system-input-picker-for="${CSS.escape(fieldId)}"]`);
    return input?.closest('.system-input-form-field')
        || picker?.closest('.system-input-form-field')
        || null;
}

function systemInputValidationControl(fieldId) {
    if (fieldId === 'system-input-unit-overview') {
        return $('system-input-unit-overview')?.querySelector('button') || null;
    }
    const input = $(fieldId);
    if (input && input.type !== 'hidden') return input;
    return document.querySelector(`[data-system-input-picker-for="${CSS.escape(fieldId)}"] input`)
        || systemInputValidationContainer(fieldId)?.querySelector('[data-value]')
        || input;
}

function clearSystemInputErrorDescription(container) {
    if (!container) return;
    container.querySelectorAll('.system-input-field-error').forEach(error => {
        container.querySelectorAll('[aria-describedby]').forEach(control => {
            const ids = (control.getAttribute('aria-describedby') || '').split(/\s+/).filter(id => id && id !== error.id);
            if (ids.length) control.setAttribute('aria-describedby', ids.join(' '));
            else control.removeAttribute('aria-describedby');
        });
        error.remove();
    });
}

function clearSystemInputValidationErrors() {
    document.querySelectorAll('.system-input-form-field.has-error').forEach(field => {
        field.classList.remove('has-error');
        delete field.dataset.error;
        clearSystemInputErrorDescription(field);
        field.querySelectorAll('[aria-invalid="true"]').forEach(control => control.removeAttribute('aria-invalid'));
    });
    const summary = $('system-input-validation-summary');
    if (summary) summary.hidden = true;
    const message = $('system-input-validation-message');
    if (message) message.textContent = '';
}

function clearSystemInputValidationError(fieldId) {
    const container = systemInputValidationContainer(fieldId);
    if (!container) return;
    container.classList.remove('has-error');
    delete container.dataset.error;
    clearSystemInputErrorDescription(container);
    container.querySelectorAll('[aria-invalid="true"]').forEach(control => control.removeAttribute('aria-invalid'));
    if (!document.querySelector('.system-input-form-field.has-error')) {
        const summary = $('system-input-validation-summary');
        if (summary) summary.hidden = true;
    }
}

function setSystemInputValidationError(fieldId, message) {
    const container = systemInputValidationContainer(fieldId);
    if (!container) return;
    container.classList.add('has-error');
    container.dataset.error = message;
    const control = systemInputValidationControl(fieldId);
    clearSystemInputErrorDescription(container);
    const error = document.createElement('span');
    error.id = `${fieldId}-error`;
    error.className = 'system-input-field-error';
    error.textContent = message;
    container.appendChild(error);
    if (control && control.type !== 'hidden') {
        control.setAttribute('aria-invalid', 'true');
        control.setAttribute('aria-describedby', [...new Set([...(control.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean), error.id])].join(' '));
    }
}

function systemInputPageLabelComplete(value) {
    const label = systemInputDisplayValue(value);
    return Boolean(label) && !/^\d+$/.test(label);
}

function systemInputChoiceComplete(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    const identifier = value.id ?? value.value;
    const label = systemInputDisplayValue({
        name: value.name,
        label: value.label,
        text: value.text,
    });
    return identifier !== undefined
        && identifier !== null
        && typeof identifier !== 'boolean'
        && Boolean(String(identifier).trim())
        && systemInputPageLabelComplete(label);
}

function systemInputUnitMissingFields(configuration, inputType = '') {
    const source = configuration && typeof configuration === 'object' && !Array.isArray(configuration)
        ? configuration
        : {};
    const resolvedInputType = String(inputType || source.input_type || source.inputType || 'paper').trim();
    if (resolvedInputType === 'textbook') {
        // 课文页面的九个分类信息字段全部是平台必填项。
        const displayValueFor = key => {
            const snake = key.replace(/[A-Z]/g, character => `_${character.toLowerCase()}`);
            const raw = [key, snake]
                .map(candidate => source[candidate])
                .find(value => value !== undefined && value !== null);
            return systemInputDisplayValue(raw).trim();
        };
        return [
            ['textbookNameZh', '课文名称（中文）'],
            ['textbookNameEn', '课文名称（英文）'],
            ['textbookForm', '课文形式'],
            ['textbookVersion', '版本'],
            ['textbookStage', '学段'],
            ['textbookGrade', '年级'],
            ['textbookVolume', '册别'],
            ['textbookUnit', '单元'],
            ['textbookLesson', '课时'],
        ]
            .filter(([key]) => !displayValueFor(key))
            .map(([, label]) => label);
    }
    if (resolvedInputType !== 'paper') return [];
    const valueFor = (...keys) => keys
        .map(key => source[key])
        .find(value => value !== undefined && value !== null);
    const missing = [];
    const paperName = systemInputDisplayValue(valueFor('paperName', 'paper_name')).trim();
    if (!paperName) missing.push('试卷名称');

    const category = systemInputDisplayValue(
        valueFor('paperCategory', 'paper_category'),
    ).trim();
    if (!['题型专项', '听说考试'].includes(category)) missing.push('试卷分类');

    const platformTemplateName = systemInputDisplayValue(
        valueFor('platformTemplateName', 'platform_template_name'),
    );
    const platformTemplateLabel = '平台题型模板';
    if (!systemInputPageLabelComplete(platformTemplateName)) {
        missing.push(platformTemplateLabel);
    } else if (typeof systemInputPlatformTemplateCatalogAssessment === 'function'
        && typeof systemInputPlatformTemplateCatalog !== 'undefined'
        && systemInputPlatformTemplateCatalog !== null) {
        const templateAssessment = systemInputPlatformTemplateCatalogAssessment(source);
        if (!['matched'].includes(templateAssessment.status) && !missing.includes(platformTemplateLabel)) {
            missing.push(platformTemplateLabel);
        }
    }

    const requiredChoices = [
        ['provinceId', 'province_id', '省份'],
        ['cityId', 'city_id', '城市'],
        ['stageId', 'stage_id', '学段'],
        ['gradeId', 'grade_id', '年级'],
    ];
    requiredChoices.forEach(([key, alias, label]) => {
        if (!systemInputChoiceComplete(valueFor(key, alias))) missing.push(label);
    });

    const yearText = String(valueFor('year') ?? '').trim();
    const year = Number(yearText);
    if (!yearText || !Number.isInteger(year) || year < 2000 || year > 2100) missing.push('年份');

    const answerTimeText = String(valueFor('answerTimeMinutes', 'answer_time_minutes') ?? '').trim();
    const answerTime = Number(answerTimeText);
    if (!answerTimeText || !Number.isInteger(answerTime) || answerTime < 1 || answerTime > 60) {
        missing.push('答题时间');
    }

    if (systemInputCascadeAvailable('paper', 'paperType', { paperCategory: category })
        && !systemInputChoiceComplete(valueFor('paperType', 'paper_type'))) {
        missing.push('考试类型');
    }
    // Presence alone is not enough for a cascade value. A stale city,
    // district, or grade can survive a document restore even though it no
    // longer belongs to the selected parent. When the local catalog is not
    // loaded the compatibility helper returns undefined, so async loading
    // cannot create a false validation error.
    if (typeof systemInputCascadeCompatibility === 'function') {
        const cascadeValues = {
            provinceId: valueFor('provinceId', 'province_id'),
            cityId: valueFor('cityId', 'city_id'),
            districtIds: valueFor('districtIds', 'district_ids', 'district_id'),
            stageId: valueFor('stageId', 'stage_id'),
            gradeId: valueFor('gradeId', 'grade_id'),
        };
        const indexes = typeof systemInputRegionIndexes !== 'undefined' ? systemInputRegionIndexes : {};
        const sources = {
            cities: Array.isArray(indexes?.cities) ? indexes.cities : [],
            districts: Array.isArray(indexes?.districts) ? indexes.districts : [],
            grades: typeof SYSTEM_INPUT_GRADES !== 'undefined' ? SYSTEM_INPUT_GRADES : [],
        };
        [
            ['cityId', '城市'],
            ['districtIds', '区县'],
            ['gradeId', '年级'],
        ].forEach(([key, label]) => {
            const value = cascadeValues[key];
            if (!systemInputCascadeValuePresent(value)) return;
            if (systemInputCascadeCompatibility('paper', key, value, cascadeValues, sources) === false
                && !missing.includes(label)) missing.push(label);
        });
    }
    return missing;
}

function renderSystemInputValidationSummary(errors) {
    const summary = $('system-input-validation-summary');
    const message = $('system-input-validation-message');
    if (!summary || !message) return;
    if (!errors.length) {
        summary.hidden = true;
        message.textContent = '';
        return;
    }
    message.replaceChildren();
    errors.forEach(error => {
        const link = document.createElement('button');
        link.type = 'button'; link.className = 'btn-text';
        link.textContent = error.label;
        link.addEventListener('click', () => focusSystemInputValidationError(error));
        message.appendChild(link);
    });
    summary.hidden = false;
}

function focusSystemInputValidationError(error) {
    const fieldByLabel = { '试卷名称': 'system-input-paper-name', '试卷分类': 'system-input-paper-category', '平台题型模板': 'system-input-platform-template-search', '省份': 'system-input-province', '城市': 'system-input-city', '年份': 'system-input-year', '答题时间': 'system-input-answer-time', '考试类型': 'system-input-paper-type-search' };
    // 学段/年级/版本等同名标签在试卷与课文中指向不同控件；按当前
    // 录入类型解析，避免聚焦到已隐藏的试卷字段上。
    const paperOnlyFieldByLabel = { '学段': 'system-input-stage', '年级': 'system-input-grade' };
    const textbookFieldByLabel = { '课文名称（中文）': 'system-input-textbook-name-zh', '课文名称（英文）': 'system-input-textbook-name-en', '课文形式': 'system-input-textbook-form', '版本': 'system-input-textbook-version', '学段': 'system-input-textbook-stage', '年级': 'system-input-textbook-grade', '册别': 'system-input-textbook-volume', '单元': 'system-input-textbook-unit', '课时': 'system-input-textbook-lesson' };
    const isTextbookFocus = ($('system-input-type')?.value || 'paper') === 'textbook';
    if (error.unitId && String(error.unitId) !== String(systemInputSelectedUnitId)) {
        const select = $('system-input-unit-select');
        if (select) { select.value = error.unitId; select.dispatchEvent(new Event('change', { bubbles: true })); }
        validateSystemInputConfigurationForm({ focus: false });
    }
    const labelLookup = isTextbookFocus
        ? { ...fieldByLabel, ...paperOnlyFieldByLabel, ...textbookFieldByLabel }
        : { ...fieldByLabel, ...paperOnlyFieldByLabel };
    // A multi-unit validation error represents the whole target, not just the
    // first missing field. Keep it on the target-level route so the detail
    // editor can expose every missing field in one pass.
    const id = error.fieldId
        || (error.unitId ? 'system-input-unit-overview' : labelLookup[error.missing?.[0]])
        || 'system-input-unit-overview';
    if (typeof root.systemInputTargetFocusField === 'function'
        && root.systemInputTargetFocusField(id, error.unitId, error.missing)) return;
    openSystemInputDisclosureForField(id);
    requestAnimationFrame(() => {
        const control = systemInputValidationControl(id);
        control?.scrollIntoView?.({ block: 'center', behavior: 'auto' });
        control?.focus?.({ preventScroll: true });
    });
}

function validateSystemInputConfigurationForm({ focus = true } = {}) {
    clearSystemInputValidationErrors();
    const errors = [];
    const addError = (fieldId, label, message) => {
        if (errors.some(error => error.fieldId === fieldId)) return;
        errors.push({ fieldId, label, message });
        setSystemInputValidationError(fieldId, message);
    };
    const inputType = $('system-input-type')?.value || 'paper';
    if (!['paper', 'textbook', 'vocabulary'].includes(inputType)) {
        addError('system-input-type', '录入类型', '请选择受支持的录入类型');
    }
    if (inputType === 'textbook') {
        const textbookRequired = [
            ['system-input-textbook-name-zh', '课文名称（中文）', '请输入课文名称（中文）'],
            ['system-input-textbook-name-en', '课文名称（英文）', '请输入课文名称（英文）'],
            ['system-input-textbook-form', '课文形式', '请输入课文形式（角色扮演 或 同步课文）'],
            ['system-input-textbook-version', '版本', '请输入教材版本，例如：人教版'],
            ['system-input-textbook-stage', '学段', '请输入学段，例如：初中'],
            ['system-input-textbook-grade', '年级', '请输入年级，例如：七年级'],
            ['system-input-textbook-volume', '册别', '请输入册别，例如：上册'],
            ['system-input-textbook-unit', '单元', '请输入单元，例如：Unit 1'],
            ['system-input-textbook-lesson', '课时', '请输入课时，例如：Section A'],
        ];
        textbookRequired.forEach(([fieldId, label, message]) => {
            if (!String($(fieldId)?.value || '').trim()) addError(fieldId, label, message);
        });
    } else if (inputType !== 'paper') {
        // Vocabulary configurations are intentionally saveable so their
        // durable units and content mappings can be prepared ahead of the
        // page adapters. External execution remains server-gated.
        renderSystemInputValidationSummary(errors);
        return errors.length === 0;
    }
    if (inputType === 'textbook') {
        // 课文多单元时同样逐单元校验必填字段，错误定位到对应单元。
        const systemInput = systemInputInteractionWorkspace()?.system_input;
        const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
        if (units.length > 1) {
            saveSystemInputCurrentUnitDraft(systemInput);
            const selectedUnitId = String(systemInputSelectedUnitId || units[0]?.unit_id || '');
            units.forEach((unit, index) => {
                const unitId = String(unit?.unit_id || '');
                if (!unitId || unitId === selectedUnitId) return;
                const configuration = systemInputUnitDrafts.get(unitId)
                    || systemInputNormalizeUnitConfiguration(
                        systemInputUnitConfiguration(unit, systemInput),
                        unit,
                        index,
                        units.length,
                        units,
                        inputType,
                    );
                const missing = systemInputUnitMissingFields(configuration, inputType);
                if (!missing.length) return;
                const label = systemInputDisplayValue(unit?.label, `第${index + 1}个录入单元`);
                errors.push({
                    fieldId: null, unitId, missing,
                    label: `${label}（${missing.join('、')}）`,
                    message: `请补齐${label}的${missing.join('、')}`,
                });
            });
        }
        renderSystemInputValidationSummary(errors);
        if (errors.length && focus) {
            focusSystemInputValidationError(errors[0]);
        }
        return errors.length === 0;
    }
    if (!String($('system-input-paper-name')?.value || '').trim()) {
        addError('system-input-paper-name', '试卷名称', '请输入试卷名称');
    }
    const category = $('system-input-paper-category')?.value || '';
    if (!['题型专项', '听说考试'].includes(category)) {
        addError('system-input-paper-category', '试卷分类', '请选择试卷分类');
    }
    if (!systemInputPageLabelComplete(systemInputPlatformTemplateNameForForm())) {
        addError('system-input-platform-template-search', '平台题型模板', '请选择平台题型模板');
    }

    const requiredChoice = (fieldId, kind, label) => {
        if (!systemInputChoiceFromField(fieldId, kind)) {
            addError(fieldId, label, `请从列表中选择${label}`);
        }
    };
    requiredChoice('system-input-province', 'provinces', '省份');
    requiredChoice('system-input-city', 'cities', '城市');
    requiredChoice('system-input-stage', 'stages', '学段');
    requiredChoice('system-input-grade', 'grades', '年级');

    const yearText = String($('system-input-year')?.value || '').trim();
    const year = Number(yearText);
    if (!yearText || !Number.isInteger(year) || year < 2000 || year > 2100) {
        addError('system-input-year', '年份', '请输入 2000–2100 之间的整数年份');
    }
    const answerTimeText = String($('system-input-answer-time')?.value || '').trim();
    const answerTime = Number(answerTimeText);
    if (!answerTimeText || !Number.isInteger(answerTime) || answerTime < 1 || answerTime > 60) {
        addError('system-input-answer-time', '答题时间', '请输入 1–60 之间的整数分钟');
    }
    if (systemInputCascadeAvailable('paper', 'paperType', { paperCategory: category })) {
        const paperType = String($('system-input-paper-type')?.value || '').trim();
        if (!systemInputChoiceFromText(paperType, 'paperTypes')) {
            addError('system-input-paper-type-search', '考试类型', '请选择考试类型');
        }
    }

    const systemInput = systemInputInteractionWorkspace()?.system_input;
    const units = Array.isArray(systemInput?.units) ? systemInput.units : [];
    if (inputType === 'paper' && units.length > 1) {
        saveSystemInputCurrentUnitDraft(systemInput);
        const selectedUnitId = String(systemInputSelectedUnitId || units[0]?.unit_id || '');
        units.forEach((unit, index) => {
            const unitId = String(unit?.unit_id || '');
            if (!unitId || unitId === selectedUnitId) return;
            const configuration = systemInputUnitDrafts.get(unitId)
                || systemInputNormalizeUnitConfiguration(
                    systemInputUnitConfiguration(unit, systemInput),
                    unit,
                    index,
                    units.length,
                    units,
                    inputType,
                );
            const missing = systemInputUnitMissingFields(configuration, inputType);
            if (!missing.length) return;
            const label = systemInputDisplayValue(unit?.label, `第${index + 1}套`);
            errors.push({
                fieldId: null, unitId, missing,
                label: `${label}（${missing.join('、')}）`,
                message: `请补齐${label}的${missing.join('、')}`,
            });
        });
    }

    renderSystemInputValidationSummary(errors);
    if (errors.length && focus) {
        focusSystemInputValidationError(errors[0]);
    }
    return errors.length === 0;
}


registerRendererModule("systemInput.disclosures", {
    bindSystemInputDisclosures,
    systemInputDisclosureStatus,
    systemInputDisclosureDetail,
    systemInputDisclosureSummaryValue,
    systemInputDisclosureSummaryJoin,
    systemInputDisclosureSummaryWithMissing,
    systemInputPaperDisclosureDetail,
    systemInputRangeDisclosureDetail,
    systemInputUnitDisclosureDetail,
    systemInputDisclosureConfigurations,
    renderSystemInputDisclosureSummaries,
    resetSystemInputDisclosureState,
    openSystemInputDisclosureForField,
    systemInputValidationContainer,
    systemInputValidationControl,
    clearSystemInputValidationErrors,
    clearSystemInputValidationError,
    setSystemInputValidationError,
    systemInputPageLabelComplete,
    systemInputChoiceComplete,
    systemInputUnitMissingFields,
    renderSystemInputValidationSummary,
    validateSystemInputConfigurationForm,
    focusSystemInputValidationError,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
