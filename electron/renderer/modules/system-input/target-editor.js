/** Batch target workspace. Drafts never call the platform or enable input. */
(function attachTargetEditor(root) {
    'use strict';

const labels = {
    provinceId: '省份', cityId: '城市', districtIds: '区县', stageId: '学段', gradeId: '年级', year: '年份',
    paperName: '试卷名称', paperCategory: '试卷分类', paperType: '考试类型', platformTemplateName: '平台题型模板',
    answerTimeMinutes: '答题时间（分钟）', textbookNameZh: '课文名称（中文）', textbookNameEn: '课文名称（英文）',
    textbookVersion: '教材版本', textbookStage: '学段', textbookGrade: '年级', textbookVolume: '册别',
    textbookUnit: '教材单元', textbookLesson: '课时', textbookForm: '课文形式',
};
// Required (必填) markers, mirroring the main system-input form in index.html.
// 区县 (districtIds) 是多选可选字段，不合规字段（试卷分类/考试类型/题型模板）、
// 单套独立字段（试卷名称/课文名称/单元/课时）不标必选，其中课文名称/单元/课时由文档自动带入。
const requiredMarkers = Object.freeze({
    provinceId: true, cityId: true, stageId: true, gradeId: true, year: true, answerTimeMinutes: true,
    textbookNameZh: true, textbookNameEn: true, textbookForm: true, textbookVersion: true,
    textbookStage: true, textbookGrade: true, textbookVolume: true, textbookUnit: true, textbookLesson: true,
});
let state = null;
let draftTimer = null;
let rendering = false;
let activeTargetModal = null;
const targetSelectWrappers = new Set();
let targetSelectDocumentBound = false;
let targetSelectSequence = 0;
// 区县 tag 多选独立维护自己的下拉关闭逻辑，不加入 targetSelectWrappers——
// 否则共享的 scroll 监听会把菜单内部滚动误判为页面滚动而直接收起。
const districtPickers = new Set();
let districtDocumentBound = false;
function copy(value, seen = new WeakMap()) {
    // Target values are plain configuration data, but they may temporarily
    // contain undefined fields while a cascade is being rebuilt. Cloning via
    // JSON.parse(JSON.stringify(...)) turns that valid transient state into
    // the exact `JSON.parse(undefined)` error users saw after choosing a
    // custom option. Keep the clone JSON-independent and cycle-safe.
    if (value === null || typeof value !== 'object') return value;
    if (seen.has(value)) return seen.get(value);
    const clone = Array.isArray(value) ? [] : {};
    seen.set(value, clone);
    Object.entries(value).forEach(([key, entry]) => {
        clone[key] = copy(entry, seen);
    });
    return clone;
}
const text = value => Array.isArray(value) ? value.map(item => systemInputDisplayValue(item)).join('、') : systemInputDisplayValue(value);
function targetFieldAliases(key) {
    if (key === 'districtIds') return ['district_ids', 'district_id'];
    const snake = String(key).replace(/[A-Z]/g, character => `_${character.toLowerCase()}`);
    return snake === key ? [] : [snake];
}
function targetFieldValue(values, key) {
    if (!values || typeof values !== 'object') return undefined;
    const has = candidate => Object.prototype.hasOwnProperty.call(values, candidate);
    const canonical = has(key) ? values[key] : undefined;
    const present = value => typeof systemInputCascadeValuePresent === 'function'
        ? systemInputCascadeValuePresent(value)
        : value !== undefined && value !== null && String(value).trim() !== '';
    if (present(canonical)) return canonical;
    const alias = targetFieldAliases(key).find(candidate => has(candidate) && present(values[candidate]));
    return alias ? values[alias] : canonical;
}
function targetCascadeChoiceMatches(value, candidate) {
    const valueObject = value && typeof value === 'object' && !Array.isArray(value) ? value : null;
    const candidateObject = candidate && typeof candidate === 'object' && !Array.isArray(candidate) ? candidate : null;
    const valueId = valueObject ? (valueObject.id ?? valueObject.value) : undefined;
    const candidateId = candidateObject ? (candidateObject.id ?? candidateObject.value) : undefined;
    // When both sides carry IDs, equal labels are not enough: two regions or
    // catalog branches can legitimately share a name.
    if (systemInputCascadeValuePresent(valueId) && systemInputCascadeValuePresent(candidateId)) {
        return String(valueId) === String(candidateId);
    }
    return systemInputCascadeChoiceMatches(value, candidate) || text(value) === text(candidate);
}
const equal = (a, b) => systemInputConfigurationValueKey(a) === systemInputConfigurationValueKey(b);
const type = () => $('system-input-type')?.value || 'paper';
const units = () => systemInputInteractionWorkspace()?.system_input?.units || [];
const configs = () => {
    const allUnits = units();
    const inputType = type();
    return allUnits.map((unit, index) => {
        const unitId = String(unit?.unit_id || '');
        const normalized = systemInputNormalizeUnitConfiguration(
            systemInputUnitDrafts.get(unitId) || systemInputUnitConfiguration(
                unit,
                systemInputInteractionWorkspace()?.system_input,
            ),
            unit,
            index,
            allUnits.length,
            allUnits,
            inputType,
        );
        const configuration = targetEditorWithPaperDefaults(normalized);
        // The editor's details and batch dialogs edit the same per-unit draft
        // map that the final save reads. Materialize only missing defaults so
        // the hidden legacy form cannot overwrite them with an empty value.
        if (inputType === 'paper' && unitId) systemInputUnitDrafts.set(unitId, configuration);
        return { ...configuration, unit_id: unitId };
    });
};
function targetEditorTypeLabel(inputType = type()) {
    return inputType === 'textbook' ? '课文' : inputType === 'vocabulary' ? '词汇' : '试卷';
}
function targetEditorTypeUnavailable(inputType = type()) {
    return systemInputTypeCapability(inputType, systemInputInteractionWorkspace())?.external_supported !== true;
}
const commonKeys = () => type() === 'textbook'
    ? ['textbookVersion', 'textbookStage', 'textbookGrade', 'textbookVolume', 'textbookForm']
    : type() === 'paper'
        ? ['provinceId', 'cityId', 'districtIds', 'stageId', 'gradeId', 'year', 'answerTimeMinutes']
        : [];
const targetEditorPaperFieldOrder = Object.freeze([
    'paperName', 'provinceId', 'cityId', 'districtIds', 'stageId', 'gradeId',
    'paperType', 'platformTemplateName', 'year', 'answerTimeMinutes',
]);
const TARGET_PLATFORM_TEMPLATE_MENU_LIMIT = 100;

function targetEditorPaperCategoryForDefaults(configuration = {}) {
    const systemInput = systemInputInteractionWorkspace()?.system_input || {};
    const unitId = String(configuration?.unit_id || '');
    const unit = units().find(candidate => String(candidate?.unit_id || '') === unitId);
    const suggested = typeof systemInputSuggestedConfiguration === 'function'
        ? systemInputSuggestedConfiguration(systemInput)
        : systemInput?.suggested_configuration;
    return [
        targetFieldValue(configuration, 'paperCategory'),
        unit?.paper_category,
        unit?.paperCategory,
        suggested?.paper_category,
        suggested?.paperCategory,
        systemInput?.paper_category,
        systemInput?.paperCategory,
    ].map(value => text(value)).find(Boolean) || '题型专项';
}

function targetEditorPaperDefaultYear() {
    return typeof SYSTEM_INPUT_DEFAULT_YEAR === 'number' ? SYSTEM_INPUT_DEFAULT_YEAR : 2026;
}

function targetEditorPaperDefaultAnswerTime(category) {
    if (typeof systemInputDefaultAnswerTimeForCategory === 'function') {
        return Number(systemInputDefaultAnswerTimeForCategory(category));
    }
    return String(category || '').trim() === '听说考试' ? 60 : 20;
}

function targetEditorWithPaperDefaults(configuration = {}) {
    if (type() !== 'paper') return configuration;
    const next = copy(configuration);
    const category = targetEditorPaperCategoryForDefaults(next);
    if (!systemInputCascadeValuePresent(targetFieldValue(next, 'year'))) {
        next.year = targetEditorPaperDefaultYear();
    }
    if (!systemInputCascadeValuePresent(targetFieldValue(next, 'answerTimeMinutes'))) {
        next.answerTimeMinutes = targetEditorPaperDefaultAnswerTime(category);
    }
    return next;
}

function el(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
}
function button(label, handler, className = 'btn-secondary btn-sm') {
    const node = el('button', className, label); node.type = 'button'; node.addEventListener('click', handler); return node;
}
function installTargetLazySelectValueBridge(select) {
    if (!select || typeof select._targetSelectOptionProvider !== 'function' || select.dataset.targetLazyValueBridge === 'true') return;
    let prototype = Object.getPrototypeOf(select);
    let descriptor = null;
    let descriptorOwner = null;
    while (prototype && !descriptor) {
        descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
        if (descriptor) descriptorOwner = prototype;
        prototype = Object.getPrototypeOf(prototype);
    }
    if (!descriptor?.get || !descriptor?.set) return;
    const bridgeKey = '__targetLazyValueBridgeInstalled';
    if (!descriptorOwner?.[bridgeKey]) {
        Object.defineProperty(descriptorOwner, 'value', {
            configurable: descriptor.configurable,
            enumerable: descriptor.enumerable,
            get: descriptor.get,
            set(value) {
                if (typeof this?._targetSelectOptionProvider === 'function') {
                    const wanted = String(value ?? '');
                    if (wanted && ![...this.options].some(option => String(option.value) === wanted)) {
                        this.append(new Option(wanted, wanted));
                    }
                }
                descriptor.set.call(this, value);
                // jsdom exposes select elements through a Proxy whose set trap
                // requires a truthy setter result; browsers ignore this return.
                return true;
            },
        });
        Object.defineProperty(descriptorOwner, bridgeKey, { value: true, configurable: true });
    }
    select.dataset.targetLazyValueBridge = 'true';
}
function labeledControl(label, control, required = false) {
    const node = el('label', 'target-field');
    node.dataset.required = required ? 'true' : 'false';
    node.append(el('span', '', label));
    // 必填标识使用绝对定位徽标，作为独立子元素，避免污染标签文本（保持首个
    // span 的 textContent 恰好等于字段名，便于既有的 label 匹配逻辑和辅助技术读取）。
    if (required) node.append(el('small', 'target-required-mark', '必填'));
    node.append(control); return node;
}
function targetPlatformTemplateChoices(configuration = {}) {
    const names = [];
    const seen = new Set();
    const append = value => {
        const name = String(value ?? '').trim();
        const normalized = systemInputNormalizeLabel(name);
        if (!normalized || seen.has(normalized)) return;
        seen.add(normalized); names.push(name);
    };
    const candidates = typeof systemInputPlatformTemplateCatalogAssessment === 'function'
        && systemInputPlatformTemplateCatalog !== null
        ? systemInputPlatformTemplateCatalogAssessment(configuration).candidates
        : (Array.isArray(systemInputPlatformTemplates) ? systemInputPlatformTemplates : []);
    candidates.forEach(template => {
        append(template?.name || template?.platform_template_name || template?.label);
    });
    // Platform templates are server-owned catalogue data. Do not invent
    // fallback names in the editor: an empty catalogue should be visible as
    // empty, while a saved legacy value remains available for review.
    append(configuration.platformTemplateName);
    append(configuration.platform_template_name);
    return names;
}
function targetPlatformTemplatePickerOptions(configuration = {}, query = '') {
    const choices = targetPlatformTemplateChoices(configuration);
    const normalizedQuery = systemInputNormalizeLabel(query);
    const matched = normalizedQuery
        ? choices.filter(value => systemInputNormalizeLabel(value).includes(normalizedQuery))
        : choices;
    const current = text(targetFieldValue(configuration, 'platformTemplateName'));
    const visible = matched.slice(0, TARGET_PLATFORM_TEMPLATE_MENU_LIMIT);
    const currentIndex = visible.findIndex(value => systemInputNormalizeLabel(value) === systemInputNormalizeLabel(current));
    if (current && currentIndex < 0 && matched.some(value => systemInputNormalizeLabel(value) === systemInputNormalizeLabel(current))) {
        visible.unshift(current);
        visible.splice(TARGET_PLATFORM_TEMPLATE_MENU_LIMIT);
    }
    return {
        options: visible.map(value => ({
            value,
            textContent: value,
            selected: systemInputNormalizeLabel(value) === systemInputNormalizeLabel(current),
            disabled: false,
        })),
        total: matched.length,
        truncated: matched.length > visible.length,
    };
}
function createTargetCheckbox({ checked = false, disabled = false, label = '', onChange } = {}) {
    const wrap = el('span', 'target-checkbox');
    const input = el('input');
    input.type = 'checkbox'; input.checked = Boolean(checked); input.disabled = Boolean(disabled);
    if (label) input.setAttribute('aria-label', label);
    const sync = () => {
        wrap.classList.toggle('is-checked', input.checked);
        wrap.classList.toggle('is-disabled', input.disabled);
    };
    input._targetCheckboxSync = sync;
    input.addEventListener('change', () => { sync(); onChange?.(input.checked, input); });
    wrap.append(input); sync();
    return { wrap, input, sync };
}
function enhanceTargetNumberControl(input, { min = '', max = '', step = 1, label = '' } = {}) {
    if (!input || input.dataset.targetNumberEnhanced === 'true') return input;
    const host = input.parentElement;
    if (!host) return input;
    input.dataset.targetNumberEnhanced = 'true';
    input.type = 'number'; input.step = String(step);
    if (min !== '') input.min = String(min);
    if (max !== '') input.max = String(max);
    input.inputMode = 'numeric';
    const wrapper = el('div', 'target-number');
    host.insertBefore(wrapper, input); wrapper.append(input);
    const actions = el('span', 'target-number-actions');
    const decrement = button('−', () => {
        if (input.disabled) return;
        const current = Number(input.value);
        const fallback = min === '' ? 0 : Number(min);
        const next = Number.isFinite(current) ? current - Number(step) : fallback;
        input.value = String(Math.max(min === '' ? -Infinity : Number(min), next));
        input.dispatchEvent(new Event('input', { bubbles: true }));
    }, 'target-number-step');
    const increment = button('+', () => {
        if (input.disabled) return;
        const current = Number(input.value);
        const fallback = min === '' ? 0 : Number(min);
        const next = Number.isFinite(current) ? current + Number(step) : fallback;
        input.value = String(Math.min(max === '' ? Infinity : Number(max), next));
        input.dispatchEvent(new Event('input', { bubbles: true }));
    }, 'target-number-step');
    decrement.setAttribute('aria-label', `减少${label || '数值'}`);
    increment.setAttribute('aria-label', `增加${label || '数值'}`);
    actions.append(decrement, increment); wrapper.append(actions);
    const sync = () => {
        decrement.disabled = Boolean(input.disabled);
        increment.disabled = Boolean(input.disabled);
        wrapper.classList.toggle('is-disabled', Boolean(input.disabled));
    };
    input._targetNumberSync = sync; sync();
    return input;
}
function enhanceTargetSelect(select) {
    if (!select || select.dataset.targetSelectEnhanced === 'true') return select;
    const host = select.parentElement;
    if (!host) return select;
    installTargetLazySelectValueBridge(select);
    // Generated controls (especially one per target row) do not have an ID.
    // Never derive their menu ID from the shared field name: six textbook
    // rows would otherwise all point at the same aria-controls target.
    const selectKey = select.id || `target-select-${++targetSelectSequence}`;
    const menuId = `${selectKey}-menu`;
    const wrapper = el('div', 'target-select');
    wrapper.dataset.targetSelectId = selectKey;
    host.insertBefore(wrapper, select);
    wrapper.append(select);
    select.dataset.targetSelectEnhanced = 'true';
    select.classList.add('target-native-select');
    // A stale native focus can survive a rerender. Move it away before the
    // source is hidden from assistive technology, otherwise Chromium reports
    // an aria-hidden/focus violation and the visible trigger loses focus.
    if (document.activeElement === select) select.blur();
    select.inert = true;
    select.setAttribute('aria-hidden', 'true');
    select.tabIndex = -1;
    const trigger = el('button', 'target-select-trigger');
    trigger.type = 'button'; trigger.setAttribute('role', 'combobox'); trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-autocomplete', 'none');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-controls', menuId);
    const triggerBaseLabel = select.getAttribute('aria-label') || '选择';
    trigger.setAttribute('aria-label', triggerBaseLabel);
    const valueNode = el('span', 'target-select-value');
    // The arrow is drawn by CSS. Keeping the span empty avoids rendering a
    // glyph and the CSS chevron at the same time (the old version showed two
    // down arrows in every select).
    const arrow = el('span', 'target-select-arrow'); arrow.setAttribute('aria-hidden', 'true');
    trigger.append(valueNode, arrow); wrapper.append(trigger);
    // `inert` and tabindex prevent normal keyboard focus, but Chromium can
    // still focus a detached/native source during a fast rerender. Always
    // return that focus to the visible custom trigger instead of leaving focus
    // inside an aria-hidden element.
    select.addEventListener('focus', () => {
        if (document.activeElement !== select) return;
        select.blur();
        if (!trigger.disabled) trigger.focus({ preventScroll: true });
    });
    const menu = el('div', 'target-select-menu'); menu.id = menuId; menu.setAttribute('role', 'listbox'); menu.hidden = true; wrapper.append(menu);
    const placeholder = () => {
        const explicit = String(select.dataset.targetPlaceholder || '').trim();
        if (explicit) return explicit;
        const aria = String(select.getAttribute('aria-label') || '').trim();
        const field = aria.split(' · ').at(-1) || '选项';
        return field.startsWith('选择') ? field : `选择${field}`;
    };
    const optionLabel = option => String((option?.label ?? option?.textContent) || '').trim() || placeholder();
    const selectedOptions = () => [...select.options].filter(option => option.selected && option.value !== '');
    const updateLabel = () => {
        const selected = selectedOptions();
        valueNode.textContent = selected.length ? selected.map(optionLabel).join('、') : placeholder();
        trigger.classList.toggle('is-placeholder', !selected.length || (!select.multiple && select.value === ''));
        trigger.disabled = Boolean(select.disabled);
        wrapper.classList.toggle('is-disabled', Boolean(select.disabled));
        trigger.setAttribute('aria-label', selected.length
            ? `${triggerBaseLabel}：${selected.map(optionLabel).join('、')}`
            : triggerBaseLabel);
        trigger.setAttribute('aria-expanded', menu.hidden ? 'false' : 'true');
    };
    const setActiveOption = option => {
        const options = [...menu.querySelectorAll('button:not(:disabled)')];
        options.forEach(item => item.classList.toggle('is-active', item === option));
        if (option?.id) trigger.setAttribute('aria-activedescendant', option.id);
        else trigger.removeAttribute('aria-activedescendant');
        option?.scrollIntoView?.({ block: 'nearest' });
    };
    const close = ({ restore = false } = {}) => {
        menu.hidden = true; trigger.setAttribute('aria-expanded', 'false'); trigger.removeAttribute('aria-activedescendant'); wrapper.classList.remove('is-open');
        if (restore) trigger.focus({ preventScroll: true });
    };
    const openQuery = { value: '' };
    const optionsBoxClass = 'target-select-options';
    const ensureNativeOption = option => {
        const value = String(option?.value ?? '');
        if (!value) return null;
        const existing = [...select.options].find(candidate => String(candidate.value) === value);
        if (existing) return existing;
        const native = new Option(optionLabel(option), value);
        select.append(native);
        return native;
    };
    const optionRow = (option, index) => {
        const item = el('button', 'target-select-option'); item.type = 'button'; item.id = `${menuId}-option-${index}`; item.setAttribute('role', 'option'); item.dataset.value = option.value; item.dataset.index = String(index); item.disabled = option.disabled;
        item.setAttribute('aria-selected', option.selected ? 'true' : 'false'); item.textContent = optionLabel(option);
        item.addEventListener('pointermove', () => setActiveOption(item));
        item.addEventListener('click', () => {
            if (select.multiple) {
                const native = ensureNativeOption(option);
                if (native) native.selected = !native.selected;
            } else {
                ensureNativeOption(option);
                select.value = option.value;
                close({ restore: true });
            }
            select.dispatchEvent(new Event('change', { bubbles: true }));
            updateLabel();
            if (select.multiple) open();
        });
        return item;
    };
    const renderMenu = () => {
        const box = menu.querySelector('.' + optionsBoxClass);
        if (!box) return;
        box.replaceChildren();
        const query = systemInputNormalizeLabel(openQuery.value);
        const provided = typeof select._targetSelectOptionProvider === 'function'
            ? select._targetSelectOptionProvider(query) || {}
            : null;
        const menuOptions = provided
            ? (Array.isArray(provided) ? provided : provided.options || [])
            : [...select.options].filter(option => option.value !== ''
                && (!query || systemInputNormalizeLabel(optionLabel(option)).includes(query)));
        let rendered = 0;
        menuOptions.forEach((option, index) => {
            // Empty options are native placeholders only. They must never be
            // rendered as actionable menu items: an empty choice made a
            // cascade look selected while leaving every child locked.
            if (option.value === '') return;
            box.append(optionRow(option, index)); rendered++;
        });
        if (provided?.truncated) {
            const total = Number(provided.total) || 0;
            const hint = el('span', 'target-select-hint', total
                ? `结果较多（共 ${total} 个），请继续搜索`
                : '结果较多，请继续搜索');
            hint.setAttribute('role', 'status');
            box.append(hint);
        }
        // Keep a saved multi-selection removable with a summary clear, even
        // when a search filter temporarily hides the selected rows themselves.
        if (selectedOptions().length) {
            const clear = el('button', 'target-select-clear', select.multiple ? '清除全部' : '清除选择');
            clear.type = 'button'; clear.id = `${menuId}-clear`; clear.setAttribute('role', 'option'); clear.setAttribute('aria-selected', 'false');
            clear.addEventListener('pointermove', () => setActiveOption(clear));
            clear.addEventListener('click', () => {
                if (select.multiple) [...select.options].forEach(option => { option.selected = false; });
                else select.value = '';
                close({ restore: true });
                select.dispatchEvent(new Event('change', { bubbles: true }));
                updateLabel();
            });
            box.append(clear);
        }
        if (!rendered && !query) {
            const empty = el('span', 'target-select-empty', '暂无可选项');
            empty.setAttribute('role', 'status'); box.append(empty);
        } else if (!rendered) {
            const empty = el('span', 'target-select-empty', '无匹配选项');
            empty.setAttribute('role', 'status'); box.append(empty);
        }
    };
    const open = () => {
        if (select.disabled) return;
        document.querySelectorAll('.target-select-menu:not([hidden])').forEach(other => {
            if (other !== menu) other.closest('.target-select')?._targetSelectClose?.();
        });
        openQuery.value = '';
        menu.replaceChildren();
        const search = el('input', 'target-select-search');
        search.type = 'search'; search.placeholder = select.multiple ? '搜索并多选…' : '搜索选项…';
        search.autocomplete = 'off'; search.spellcheck = false;
        search.setAttribute('aria-label', `${triggerBaseLabel}搜索`);
        search.addEventListener('click', event => event.stopPropagation());
        search.addEventListener('input', () => { openQuery.value = search.value; renderMenu(); });
        search.addEventListener('keydown', event => {
            if (event.key === 'ArrowDown') {
                event.preventDefault();
                const first = menu.querySelector('.' + optionsBoxClass + ' button:not(:disabled)');
                if (first) { setActiveOption(first); first.focus({ preventScroll: true }); }
            }
            if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close({ restore: true }); }
        });
        const box = el('div', optionsBoxClass); box.setAttribute('role', 'presentation');
        menu.append(search, box);
        renderMenu();
        const rect = trigger.getBoundingClientRect();
        const below = window.innerHeight - rect.bottom - 12;
        const above = below < 170 && rect.top > below;
        const available = Math.max(96, Math.min(300, above ? rect.top - 12 : below));
        Object.assign(menu.style, { position: 'fixed', left: `${Math.max(8, rect.left)}px`, top: above ? 'auto' : `${rect.bottom + 4}px`, bottom: above ? `${window.innerHeight - rect.top + 4}px` : 'auto', width: `${Math.min(rect.width, window.innerWidth - rect.left - 8)}px`, maxHeight: `${available}px` });
        menu.hidden = false; wrapper.classList.add('is-open'); trigger.setAttribute('aria-expanded', 'true');
        const selected = menu.querySelector('.' + optionsBoxClass + ' [aria-selected="true"]:not(:disabled)') || menu.querySelector('.' + optionsBoxClass + ' button:not(:disabled)');
        setActiveOption(selected);
        requestAnimationFrame(() => search.focus({ preventScroll: true }));
    };
    trigger.addEventListener('click', () => menu.hidden ? open() : close({ restore: true }));
    trigger.addEventListener('keydown', event => {
        if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
            event.preventDefault();
            if (menu.hidden) open();
            else {
                const options = [...menu.querySelectorAll('button:not(:disabled)')];
                const current = options.findIndex(item => item.classList.contains('is-active'));
                const next = event.key === 'ArrowUp'
                    ? (current <= 0 ? options.length - 1 : current - 1)
                    : (current < 0 || current >= options.length - 1 ? 0 : current + 1);
                setActiveOption(options[next]);
            }
        }
        if (['Enter', ' '].includes(event.key)) {
            event.preventDefault();
            if (menu.hidden) open();
            else (menu.querySelector('button.is-active:not(:disabled)') || menu.querySelector('button:not(:disabled)'))?.click();
        }
        // Escape belongs to the picker only while its menu is open. When the
        // menu is closed, let the event reach the dialog/editor close handler.
        if (event.key === 'Escape' && !menu.hidden) { event.preventDefault(); event.stopPropagation(); close({ restore: true }); }
    });
    trigger.addEventListener('focusout', () => {
        requestAnimationFrame(() => {
            if (!wrapper.contains(document.activeElement)) close();
        });
    });
    select.addEventListener('change', updateLabel);
    select._targetSelectSync = updateLabel;
    targetSelectWrappers.add(wrapper);
    if (!targetSelectDocumentBound) {
        document.addEventListener('pointerdown', event => {
            targetSelectWrappers.forEach(item => {
                if (!document.contains(item)) { targetSelectWrappers.delete(item); return; }
                if (!item.contains(event.target)) item._targetSelectClose?.();
            });
        }, true);
        window.addEventListener('resize', () => {
            targetSelectWrappers.forEach(item => item._targetSelectClose?.());
        });
        document.addEventListener('scroll', event => {
            // A fixed menu is positioned from its trigger. Close it when the
            // workspace moves so it can never drift away from the field.
            if (!event.target?.closest?.('.target-select-menu')) {
                targetSelectWrappers.forEach(item => item._targetSelectClose?.());
            }
        }, true);
        targetSelectDocumentBound = true;
    }
    wrapper._targetSelectClose = close;
    updateLabel();
    return select;
}
function setDraftStatus(message) { const node = $('target-draft-status'); if (node) node.textContent = message; }
function targetEditorActive() { return Boolean(state && $('system-input-drawer') && !$('system-input-drawer').hidden); }
function targetModalCard(node) { return node?.querySelector?.('.target-modal-card') || node; }
function targetModalFocusableElements(node) {
    const card = targetModalCard(node);
    if (!card) return [];
    return [...card.querySelectorAll(
        'button:not([disabled]), input:not([type="hidden"]):not([disabled]), select:not(.target-native-select):not([disabled]), textarea:not([disabled]), a[href], summary, [tabindex]:not([tabindex="-1"])',
    )].filter(element => !element.hidden && !element.closest?.('[hidden]') && element.getAttribute('aria-hidden') !== 'true');
}
function trapTargetModalFocus(event) {
    if (event.key !== 'Tab' || !activeTargetModal || activeTargetModal.hidden) return;
    const focusable = targetModalFocusableElements(activeTargetModal);
    const card = targetModalCard(activeTargetModal);
    if (!focusable.length) {
        event.preventDefault();
        card?.focus?.({ preventScroll: true });
        return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!activeTargetModal.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus({ preventScroll: true });
    } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus({ preventScroll: true });
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus({ preventScroll: true });
    }
}
function releaseTargetNativeSelectFocus({ restoreToModal = false } = {}) {
    const active = document.activeElement;
    if (!active?.classList?.contains('target-native-select')) return false;
    const trigger = active.closest?.('.target-select')?.querySelector('.target-select-trigger:not([disabled])');
    active.blur?.();
    const modalCard = restoreToModal && activeTargetModal && !activeTargetModal.hidden
        ? targetModalCard(activeTargetModal)
        : null;
    (modalCard || trigger)?.focus?.({ preventScroll: true });
    return true;
}
function setTargetModalBackgroundInert(inert) {
    const drawer = $('system-input-drawer');
    const background = [drawer?.querySelector('.system-input-drawer-panel'), $('sidebar'), $('toolbar')].filter(Boolean);
    background.forEach(node => {
        if (inert) { if (!node.inert) { node.dataset.targetModalInert = 'true'; node.inert = true; } }
        else if (node.dataset.targetModalInert === 'true') { node.inert = false; delete node.dataset.targetModalInert; }
    });
}
function syncTargetWorkspaceVisibility() {
    const drawer = $('system-input-drawer');
    const panel = drawer?.querySelector('.system-input-drawer-panel');
    if (!drawer || !panel) return;
    const modalOpen = Boolean(activeTargetModal && !activeTargetModal.hidden);
    // Child dialogs are mounted outside the drawer. Hide the shell from
    // assistive technology while any child dialog owns the interaction, but
    // keep the same target-list workspace visible behind every child dialog.
    if (modalOpen) releaseTargetNativeSelectFocus({ restoreToModal: true });
    drawer.setAttribute('aria-hidden', modalOpen || drawer.hidden ? 'true' : 'false');
    panel.hidden = false;
    panel.setAttribute('aria-hidden', 'false');
}
function openTargetModal(node, focusId = '') {
    if (!node) return false;
    if (activeTargetModal && activeTargetModal !== node) closeTargetModal(activeTargetModal, { restoreFocus: false });
    node.hidden = false;
    node.setAttribute('aria-hidden', 'false');
    if (node.tagName === 'DETAILS') node.open = true;
    node.classList.add('is-open');
    const card = targetModalCard(node);
    card?.setAttribute('tabindex', '-1');
    activeTargetModal = node;
    syncTargetWorkspaceVisibility();
    document.body.classList.add('target-modal-open');
    setTargetModalBackgroundInert(true);
    requestAnimationFrame(() => {
        const target = focusId ? $(focusId) : [...card?.querySelectorAll?.(
            '.target-select-trigger:not(:disabled), input:not([type="hidden"]):not([disabled]), '
            + 'select:not(.target-native-select):not([hidden]):not([disabled]):not([aria-hidden="true"]), '
            + 'button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) || []].find(item => !item.closest?.('[hidden]'));
        target?.focus?.({ preventScroll: true });
    });
    return true;
}
function closeTargetModal(node = activeTargetModal, { restoreFocus = true } = {}) {
    if (!node) return false;
    const restoreTarget = restoreFocus && state?.lastModalTrigger?.isConnected
        && !node.contains(state.lastModalTrigger)
        ? state.lastModalTrigger
        : null;
    const closingActiveModal = activeTargetModal === node;
    // The drawer panel is inert while a detached child dialog is open. Release
    // that lock before returning focus; otherwise focus() silently fails and
    // the close button remains inside the subtree we are about to aria-hide.
    if (closingActiveModal) setTargetModalBackgroundInert(false);
    // Move focus out before hiding the dialog. Setting aria-hidden on a
    // subtree that still owns focus is rejected by Chromium and leaves
    // keyboard users without a reliable return point.
    if (node.contains(document.activeElement)) {
        const fallback = restoreTarget || $('system-input-drawer-title') || document.body;
        fallback?.focus?.({ preventScroll: true });
        if (node.contains(document.activeElement)) document.activeElement?.blur?.();
    }
    if (node.tagName === 'DETAILS') node.open = false;
    node.hidden = true;
    node.setAttribute('aria-hidden', 'true');
    node.classList.remove('is-open');
    if (activeTargetModal === node) {
        activeTargetModal = null;
        document.body.classList.remove('target-modal-open');
        setTargetModalBackgroundInert(false);
    }
    if (node.id === 'target-detail' && state) state.detail = null;
    syncTargetWorkspaceVisibility();
    if (restoreTarget && !node.contains(document.activeElement)) restoreTarget.focus({ preventScroll: true });
    return true;
}
function closeTargetDetail({ restoreFocus = true } = {}) {
    const unitId = String(state?.detail?.unitId || '');
    const closed = closeTargetModal($('target-detail'), { restoreFocus: false });
    if (!closed) return false;
    refreshSystemInputTargetEditor();
    if (restoreFocus) {
        // Prefer the exact action that opened the detail editor (“编辑详情”)
        // so focus returns to the button the user actually clicked.
        const trigger = state?.lastModalTrigger;
        const triggerIsRow = trigger?.isConnected
            && !$('target-detail')?.contains(trigger)
            && trigger.closest?.('.target-row-actions');
        if (triggerIsRow) {
            trigger.focus({ preventScroll: true });
            return true;
        }
        const row = [...$('target-rows')?.querySelectorAll('tr') || []]
            .find(candidate => candidate.querySelector('[data-unit-id="' + CSS.escape(unitId) + '"]'));
        const action = row?.querySelector('.target-row-actions button');
        (action || $('target-batch-open') || $('system-input-drawer-title'))?.focus?.({ preventScroll: true });
    }
    return true;
}
function closeTargetEditorSurface(node = activeTargetModal) {
    if (node?.id === 'target-detail') return closeTargetDetail();
    return closeTargetModal(node);
}
function targetEditorCloseActiveModal() { return closeTargetEditorSurface(activeTargetModal); }
function targetModalIsOpen(node) { return Boolean(node && !node.hidden); }
function targetEditorSyncBoundaryReview() {
    const section = $('system-input-boundary-review');
    if (!section?.classList.contains('target-modal') || !state || !targetEditorActive()) return false;
    if (section.hidden) {
        if (activeTargetModal === section) closeTargetModal(section);
        return false;
    }
    if (activeTargetModal !== section) {
        state.lastModalTrigger = document.activeElement;
        openTargetModal(section);
    }
    return true;
}
function targetEditorState() {
    const pendingBatch = Boolean(state?.batch?.result?.changes?.length);
    return state ? copy({ defaults: state.defaults, overrides: state.overrides, acknowledgements: state.acknowledgements,
        commonPending: state.commonPending, commonOpen: targetModalIsOpen($('target-common')),
        batch: state.batch ? {
            values: state.batch.values,
            fields: [...state.batch.fields],
            pending: pendingBatch,
            mode: $('target-batch-mode')?.value === 'overwrite' ? 'overwrite' : 'fill-empty',
            context: state.batch.context || 'batch',
            templateName: state.batch.templateName || '',
        } : null,
        // Row selection is a transient checklist interaction. Persist it only
        // while an unapplied batch preview needs to be restored; reopening a
        // normal editor must never look as if rows were preselected.
        selected: pendingBatch ? [...state.selected] : [], batchOpen: targetModalIsOpen($('target-batch')),
    }) : null;
}
// The template workflow lives in a separate renderer module. Expose only the
// current checklist ids so “勾选条目” keeps working when the table is filtered
// and some checked rows are temporarily out of the DOM.
root.systemInputTargetSelectedUnitIds = () => state ? [...state.selected] : [];
function targetEditorCanSave() {
    if (!state) return true;
    const workspace = systemInputInteractionWorkspace();
    if (systemInputDraftStorageKey(workspace) !== state.identity) {
        showToast('文档结构已变化，原草稿已保留。请返回后重新打开录入目标。', 'warning'); return false;
    }
    if (!systemInputConfigurationEditable(workspace?.system_input)) {
        showToast('录入已经开始或有结果待核验，当前目标不可修改。', 'warning'); return false;
    }
    return true;
}
function targetEditorPrepareSave() {
    if (!state) return true;
    // 「批次共用设置」只是辅助性的批量填充工具：它只负责把某些共用字段的默认值
    // 一次性带进整批条目。真正要保存的目标内容来自各条单元自己的配置
    // （collectSystemInputConfiguration 读取的是 systemInputUnitDrafts），
    // 并不会依赖 commonPending 这个“待填缓冲”。所以即使这里还留有没有点
    // “更新批次默认值”的改动，也不会丢失任何目标数据——直接从保存阻塞项里去掉，
    // 避免保存时被一个非必填的辅助面板反复拦截。
    //
    // 与之相对，「批量修改」是用户主动勾选条目后进行的一整批编辑预览，若未应用就
    // 直接保存会把这些仍未落地的改动悄悄丢掉，因此保留它在保存前的拦截提醒。

    // 顺手把仍未应用的共用填充状态静默回收：它们只是辅助默认值，保存时不再以
    // “待应用状态”的形式阻塞或弹出。
    if (state.commonPending) {
        state.commonPending = copy(state.defaults || {});
        scheduleTargetEditorDraft();
    }

    if (state.batch && state.batch.result?.changes?.length) {
        openTargetModal($('target-batch'));
        showToast(state.batch.context === 'template'
            ? '方案预览还有未应用的变更，请先确认并应用。'
            : '批量修改还有未应用的预览，请先点击“应用本次修改”。', 'warning');
        return false;
    }
    return true;
}
function targetEditorSetBusy(busy) {
    const form = $('system-input-form'); if (form) form.inert = busy;
    const drawer = $('system-input-drawer');
    document.querySelectorAll('.target-modal').forEach(node => {
        if (busy) { node.dataset.wasInert = String(node.inert); node.inert = true; }
        else if (node.dataset.wasInert !== undefined) { node.inert = node.dataset.wasInert === 'true'; delete node.dataset.wasInert; }
    });
    const actions = $('system-input-drawer')?.querySelector('.system-input-drawer-actions');
    actions?.querySelectorAll('button').forEach(node => {
        if (busy) { node.dataset.wasDisabled = String(node.disabled); node.disabled = true; }
        else if (node.dataset.wasDisabled !== undefined) { node.disabled = node.dataset.wasDisabled === 'true'; delete node.dataset.wasDisabled; }
    });
    if (busy) setDraftStatus('正在保存全部录入目标…');
}
function targetCompatibility(field, value, next) {
    const indexes = typeof systemInputRegionIndexes !== 'undefined' ? systemInputRegionIndexes : {};
    const grades = typeof SYSTEM_INPUT_GRADES !== 'undefined' ? SYSTEM_INPUT_GRADES : [];
    return systemInputCascadeCompatibility('paper', field, value, next, {
        cities: indexes.cities,
        districts: indexes.districts,
        grades,
    });
}
function targetEditorPersist() {
    clearTimeout(draftTimer);
    if (!targetEditorActive() || state.persisting) return;
    state.persisting = true;
    try {
        const draft = captureSystemInputDrawerDraft(state.workspace);
        if (!draft) return;
        draft.editorState = targetEditorState();
        const result = saveSystemInputStoredDraft(state.workspace, draft);
        state.dirty = true;
        setDraftStatus(result.ok ? '草稿已暂存本机 · 尚未保存目标' : '草稿仅保留在当前窗口 · 本机暂存失败');
    } finally { state.persisting = false; }
}
function scheduleTargetEditorDraft() {
    if (!targetEditorActive()) return;
    state.dirty = true;
    setDraftStatus('正在暂存草稿…');
    clearTimeout(draftTimer); draftTimer = setTimeout(targetEditorPersist, 350);
}
function targetEditorClosed({ saved = false } = {}) {
    clearTimeout(draftTimer);
    targetSelectWrappers.forEach(wrapper => wrapper._targetSelectClose?.());
    if (saved && state) {
        clearSystemInputStoredDraft(state.workspace);
        // Keep inheritance metadata without restoring old form values.
        const metadata = targetEditorState(); delete metadata.commonPending; delete metadata.batch; delete metadata.selected;
        metadata.commonOpen = false; metadata.batchOpen = false;
        saveSystemInputStoredDraft(state.workspace, { committed: true, unitDraftEntries: [], editorState: metadata });
    }
    // Detached child dialogs are outside the drawer, so the drawer close
    // path cannot blur their focused controls for us.
    const focusedTargetModal = document.activeElement?.closest?.('.target-modal');
    if (focusedTargetModal) document.activeElement.blur?.();
    document.querySelectorAll('[data-target-editor-inert]').forEach(node => { node.inert = false; delete node.dataset.targetEditorInert; });
    document.querySelectorAll('.target-modal').forEach(node => {
        node.hidden = true;
        node.setAttribute('aria-hidden', 'true');
        node.classList.remove('is-open');
        if (node.tagName === 'DETAILS') node.open = false;
    });
    activeTargetModal = null;
    document.body.classList.remove('target-modal-open');
    setTargetModalBackgroundInert(false);
    const drawer = $('system-input-drawer');
    const panel = drawer?.querySelector('.system-input-drawer-panel');
    if (panel) {
        panel.hidden = false;
        panel.setAttribute('aria-hidden', 'false');
    }
    drawer?.setAttribute('aria-hidden', 'true');
    state = null;
}
function targetEditorRecordManual(configuration) {
    if (!state) return;
    const id = String(configuration.unit_id || '');
    const previous = state.observed.get(id);
    if (previous) {
        const overrides = new Set(state.overrides[id] || []);
        commonKeys().forEach(key => {
            if (equal(targetFieldValue(configuration, key), targetFieldValue(previous, key))) return;
            if (equal(targetFieldValue(configuration, key), targetFieldValue(state.defaults, key))) overrides.delete(key); else overrides.add(key);
        });
        state.overrides[id] = [...overrides];
    }
    state.observed.set(id, copy(configuration));
}
function assessment(configuration) {
    const capability = systemInputTypeCapability(type(), systemInputInteractionWorkspace());
    const missing = systemInputUnitMissingFields(configuration, type());
    const catalog = type() === 'textbook'
        ? systemInputTextbookCatalogAssessment(configuration, { records: systemInputTextbookCatalog?.records || [] })
        : type() === 'paper' && systemInputPlatformTemplateCatalog !== null
            ? systemInputPlatformTemplateCatalogAssessment(configuration)
            : null;
    const fingerprint = JSON.stringify(configuration);
    const pending = Boolean(catalog && ['conflict', 'manual', 'unavailable'].includes(catalog.status)
        && state?.acknowledgements[String(configuration.unit_id)] !== fingerprint);
    const unavailable = targetEditorTypeUnavailable();
    return {
        missing,
        catalog,
        pending,
        unavailable,
        fingerprint,
        reason: String(capability?.reason || '当前录入类型的页面适配器尚未接入').trim(),
        label: unavailable ? '待接入外部能力' : missing.length ? `待补 ${missing.length} 项` : pending ? '目录待核对' : '配置齐全',
    };
}
const targetEditorFormFieldKeys = Object.freeze({
    'system-input-paper-name': 'paperName',
    'system-input-paper-category': 'paperCategory',
    'system-input-platform-template-search': 'platformTemplateName',
    'system-input-province': 'provinceId',
    'system-input-city': 'cityId',
    'system-input-districts': 'districtIds',
    'system-input-stage': 'stageId',
    'system-input-grade': 'gradeId',
    'system-input-year': 'year',
    'system-input-answer-time': 'answerTimeMinutes',
    'system-input-paper-type-search': 'paperType',
    'system-input-textbook-name-zh': 'textbookNameZh',
    'system-input-textbook-name-en': 'textbookNameEn',
    'system-input-textbook-form': 'textbookForm',
    'system-input-textbook-version': 'textbookVersion',
    'system-input-textbook-stage': 'textbookStage',
    'system-input-textbook-grade': 'textbookGrade',
    'system-input-textbook-volume': 'textbookVolume',
    'system-input-textbook-unit': 'textbookUnit',
    'system-input-textbook-lesson': 'textbookLesson',
});
const targetEditorMissingFieldKeysByLabel = Object.freeze({
    '试卷名称': 'paperName', '试卷分类': 'paperCategory', '平台题型模板': 'platformTemplateName',
    '省份': 'provinceId', '城市': 'cityId', '区县': 'districtIds', '学段': 'stageId',
    '年级': 'gradeId', '年份': 'year', '答题时间': 'answerTimeMinutes',
    '答题时间（分钟）': 'answerTimeMinutes', '考试类型': 'paperType',
    '课文名称（中文）': 'textbookNameZh', '课文名称（英文）': 'textbookNameEn',
    '课文形式': 'textbookForm', '版本': 'textbookVersion', '册别': 'textbookVolume',
    '单元': 'textbookUnit', '课时': 'textbookLesson',
});
function targetEditorMissingFieldKeys(configuration) {
    return [...new Set(systemInputUnitMissingFields(configuration, type())
        .map(label => targetEditorMissingFieldKeysByLabel[label])
        .filter(Boolean))];
}
function targetEditorShouldPreserveInlineFocus(body) {
    const active = document.activeElement;
    if (!active || !body?.contains(active)) return false;
    return Boolean(
        active.matches('.target-inline')
        || active.closest('.target-select, .target-number'),
    );
}
function markChanged(id) { if (state) state.changed.add(String(id)); }
function commitInline(id, key, value) {
    if (!targetEditorCanSave()) return;
    const current = copy(systemInputUnitDrafts.get(id) || {});
    const previous = targetFieldValue(current, key);
    targetFieldAliases(key).forEach(alias => delete current[alias]);
    current[key] = value;
    const cascadeChanged = !equal(previous, value)
        && systemInputCascadeFieldIsKnown(targetCascadeInputType(key), key);
    if (cascadeChanged) {
        systemInputCascadeResetDescendants(targetCascadeInputType(key), current, key, { removeEmpty: true });
    }
    const scopeChanged = !equal(previous, value)
        && ['paperCategory', 'provinceId', 'cityId', 'stageId', 'gradeId'].includes(key)
        && (key !== 'stageId' && key !== 'gradeId' || systemInputPlatformTemplateKind(current) === 'paper');
    if (scopeChanged) {
        delete current.platformTemplateName;
        delete current.platformTemplateId;
        delete current.platformTemplateVersion;
    }
    if (key === 'platformTemplateName') {
        const match = systemInputPlatformTemplateCatalogAssessment(current).candidates.find(template => (
            systemInputNormalizeLabel(template.name) === systemInputNormalizeLabel(value)
        ));
        current.platformTemplateName = match?.name || value;
        if (match?.platform_template_id) current.platformTemplateId = match.platform_template_id;
        else delete current.platformTemplateId;
        if (match?.platform_template_version) current.platformTemplateVersion = match.platform_template_version;
        else delete current.platformTemplateVersion;
    }
    systemInputUnitDrafts.set(id, current); targetEditorRecordManual(current); markChanged(id);
    if (id === String(systemInputSelectedUnitId)) {
        // The visible workspace list is refreshed below. Keep the hidden legacy
        // form in sync for save collection without rebuilding the whole list a
        // second time for every inline edit.
        populateSystemInputUnitForm(systemInputInteractionWorkspace()?.system_input, { refreshTargetEditor: false });
    }
    // A cascade parent changes the availability and value of later controls.
    // Bypass the normal inline-focus preservation once so stale descendant
    // menus cannot remain visible after the underlying draft was cleared.
    refreshSystemInputTargetEditor({ force: cascadeChanged || scopeChanged }); scheduleTargetEditorDraft();
}
function focusTargetBatchField(key) {
    const field = $('target-batch-fields')?.querySelector(`[data-target-field="${key}"]`);
    if (!field) return false;
    const control = field.querySelector(
        '.target-select-trigger:not(:disabled), input:not([type="hidden"]):not([disabled]), textarea:not([disabled]), button:not([disabled])',
    );
    control?.focus?.({ preventScroll: true });
    return Boolean(control);
}
function openTargetBatchForUnit(id, presetFields = []) {
    if (!state || type() === 'vocabulary') return false;
    const unitId = String(id || '');
    const configuration = configs().find(item => String(item.unit_id) === unitId);
    if (!configuration) return false;
    const fields = targetEditorBatchFieldsForPreset(configuration, presetFields);
    state.selected = new Set([unitId]);
    refreshSystemInputTargetEditor();
    prepareBatch(configuration, fields, { open: true });
    requestAnimationFrame(() => {
        const first = fields.find(key => $('target-batch-fields')?.querySelector(`[data-target-field="${key}"]`));
        if (first) focusTargetBatchField(first);
    });
    return true;
}
function focusTargetDetailField(key) {
    const field = $('target-detail-fields')?.querySelector('[data-target-field="' + CSS.escape(key) + '"]');
    if (!field) return false;
    const control = field.querySelector(
        '.target-select-trigger:not(:disabled), input:not([type="hidden"]):not([disabled]), textarea:not([disabled]), button:not([disabled])',
    );
    control?.focus?.({ preventScroll: true });
    return Boolean(control);
}
function targetEditorDetailStatusText(configuration) {
    const status = assessment(configuration);
    if (status.unavailable) return status.reason + '，当前仅可保存配置。';
    if (status.missing.length) return '待补字段：' + status.missing.join('、');
    if (status.pending) return status.catalog?.label || '目录待核对';
    return '必填字段已补齐，修改会直接暂存到当前目标。';
}
function targetEditorPaperCategoryPresentation(configuration = {}) {
    const systemInput = systemInputInteractionWorkspace()?.system_input || {};
    const unitId = String(configuration?.unit_id || '');
    const unit = units().find(candidate => String(candidate?.unit_id || '') === unitId);
    const suggested = typeof systemInputSuggestedConfiguration === 'function'
        ? systemInputSuggestedConfiguration(systemInput)
        : systemInput?.suggested_configuration;
    const category = [
        targetFieldValue(configuration, 'paperCategory'),
        unit?.paper_category,
        suggested?.paper_category,
        systemInput?.paper_category,
    ].map(value => text(value)).find(Boolean) || '';
    const status = String(
        unit?.paper_category_status
        || suggested?.paper_category_status
        || systemInput?.paper_category_status
        || '',
    ).trim().toLowerCase();
    const statusLabel = {
        suggested: '文档自动识别',
        confirmed: '已确认',
        user_override: '已确认',
        conflict: '识别结果待确认',
    }[status] || (category ? '当前分类' : '等待识别');
    return {
        category: category || '待识别',
        status,
        statusLabel,
        detected: Boolean(category),
    };
}
function renderTargetDetailCategory(configuration = {}) {
    const container = $('target-detail-classification');
    const tag = $('target-detail-category-tag');
    const note = $('target-detail-category-note');
    if (!container || !tag || !note) return false;
    const visible = type() === 'paper';
    container.hidden = !visible;
    if (!visible) return false;
    const presentation = targetEditorPaperCategoryPresentation(configuration);
    tag.textContent = presentation.category;
    tag.dataset.status = presentation.status || 'unknown';
    tag.classList.toggle('is-pending', !presentation.detected || presentation.status === 'conflict');
    note.textContent = presentation.statusLabel;
    container.setAttribute('aria-label', `试卷分类：${presentation.category}，${presentation.statusLabel}`);
    return true;
}
function targetEditorSelectedConfigurations() {
    const selected = state?.selected instanceof Set ? state.selected : new Set();
    return configs().filter(configuration => selected.has(String(configuration?.unit_id || '')));
}
function targetEditorBatchCategoryPresentation() {
    const selected = targetEditorSelectedConfigurations();
    const presentations = selected.map(targetEditorPaperCategoryPresentation);
    if (!presentations.length) return targetEditorPaperCategoryPresentation(state?.batch?.values || {});
    const categories = [...new Set(presentations.map(presentation => presentation.category || '待识别'))];
    if (categories.length > 1) {
        return {
            category: '多种分类',
            status: 'conflict',
            statusLabel: '所选目标分类不一致',
            detected: true,
        };
    }
    const statusLabels = [...new Set(presentations.map(presentation => presentation.statusLabel).filter(Boolean))];
    if (statusLabels.length > 1) {
        return {
            ...presentations[0],
            status: 'conflict',
            statusLabel: '各条目的识别状态不同',
        };
    }
    return presentations[0];
}
function renderTargetBatchCategory() {
    const container = $('target-batch-classification');
    const tag = $('target-batch-category-tag');
    const note = $('target-batch-category-note');
    if (!container || !tag || !note) return false;
    const visible = type() === 'paper';
    container.hidden = !visible;
    if (!visible) return false;
    const presentation = targetEditorBatchCategoryPresentation();
    tag.textContent = presentation.category;
    tag.dataset.status = presentation.status || 'unknown';
    tag.classList.toggle('is-pending', !presentation.detected || presentation.status === 'conflict');
    note.textContent = presentation.statusLabel;
    container.setAttribute('aria-label', `试卷分类：${presentation.category}，${presentation.statusLabel}`);
    return true;
}
function targetEditorBatchCascadeValues(values = {}) {
    if (type() !== 'paper') return values;
    const hasCategory = Object.prototype.hasOwnProperty.call(values, 'paperCategory')
        || Object.prototype.hasOwnProperty.call(values, 'paper_category');
    if (hasCategory) return values;
    const categories = [...new Set(targetEditorSelectedConfigurations()
        .map(configuration => text(targetFieldValue(configuration, 'paperCategory')).trim())
        .filter(Boolean))];
    return categories.length === 1 ? { ...values, paperCategory: categories[0] } : values;
}
function targetEditorBatchFields(inputType = type(), values = {}) {
    const cascadeValues = targetEditorBatchCascadeValues(values);
    const fields = systemInputBatchFields(inputType)
        .filter(key => labels[key])
        // 文档分类由识别结果负责；批量窗口只展示它的结果，不再提供手工覆盖项。
        .filter(key => key !== 'paperCategory')
        // “考试类型”只对听说考试有意义，题型专项不留下禁用空控件。
        .filter(key => key !== 'paperType'
            || systemInputCascadeAvailable('paper', 'paperType', cascadeValues));
    if (inputType !== 'paper') return fields;
    // Keep the two automatic range values together after the platform
    // template. The same ordered list feeds both detail and batch dialogs so
    // the two editing surfaces cannot drift apart again.
    return [
        ...targetEditorPaperFieldOrder.filter(key => fields.includes(key)),
        ...fields.filter(key => !targetEditorPaperFieldOrder.includes(key)),
    ];
}
function targetEditorBatchCascadeContext(key, values = {}, selectedFields = new Set()) {
    const cascadeValues = targetEditorBatchCascadeValues(values);
    const cascadeFields = new Set(selectedFields || []);
    // The classifier-owned category is not a user-selectable batch field, but
    // it still satisfies the parent requirement for its visible descendants.
    if (type() === 'paper'
        && ['paperType', 'platformTemplateName'].includes(key)
        && systemInputCascadeValuePresent(targetFieldValue(cascadeValues, 'paperCategory'))) {
        cascadeFields.add('paperCategory');
    }
    return { values: cascadeValues, fields: cascadeFields };
}
function targetEditorBatchReady(key, values = {}, selectedFields = new Set()) {
    const context = targetEditorBatchCascadeContext(key, values, selectedFields);
    return targetCascadeBatchReady(key, context.values, context.fields);
}
function targetEditorBatchHint(key, values = {}, selectedFields = new Set()) {
    const context = targetEditorBatchCascadeContext(key, values, selectedFields);
    return targetCascadeBatchHint(key, context.values, context.fields);
}
function updateTargetDetailSummary(configuration = null) {
    const summary = $('target-detail-summary');
    if (!summary || !state?.detail) return false;
    const current = configuration || configs().find(item => String(item.unit_id) === String(state.detail.unitId));
    if (!current) return false;
    const status = assessment(current);
    const complete = !status.unavailable && !status.missing.length && !status.pending;
    summary.classList.toggle('is-complete', complete);
    summary.classList.toggle('is-attention', !complete);
    summary.textContent = targetEditorDetailStatusText(current);
    return true;
}
function commitTargetDetail(key, sourceValues = null) {
    if (!state?.detail || !targetEditorCanSave()) return;
    const id = String(state.detail.unitId || '');
    const allUnits = units();
    const index = allUnits.findIndex(unit => String(unit?.unit_id || '') === id);
    const unit = index >= 0 ? allUnits[index] : null;
    if (!unit || !id) return;
    // Cascading controls rebuild their field grid after a parent changes.
    // The rebuilt controls own the latest values object, while `state.detail`
    // may still point at the snapshot committed by the previous control.
    // Always accept the field grid's live values so a child selection cannot
    // be silently dropped after that rebuild.
    const configuration = copy(sourceValues || state.detail.values || {});
    targetFieldAliases(key).forEach(alias => delete configuration[alias]);
    configuration.unit_id = id;
    systemInputUnitDrafts.set(id, configuration);
    state.detail.values = configuration;
    targetEditorRecordManual(configuration);
    markChanged(id);
    // Keep the legacy form source synchronized when the detail editor is
    // editing the currently selected unit. Submission collects that source
    // once more, so it must never overwrite a value just entered in the
    // detached detail dialog with stale hidden-form data.
    if (id === String(systemInputSelectedUnitId)) {
        // Detail changes are committed to the draft map immediately. The
        // detached dialog owns the visible state until it closes, so syncing
        // the hidden source must not trigger another full list render here.
        populateSystemInputUnitForm(systemInputInteractionWorkspace()?.system_input, { refreshTargetEditor: false });
    }
    renderTargetDetailCategory(configuration);
    updateTargetDetailSummary(configuration);
    scheduleTargetEditorDraft();
}
function renderTargetDetail() {
    if (!state?.detail || !$('target-detail')) return false;
    const id = String(state.detail.unitId || '');
    const configuration = configs().find(item => String(item.unit_id) === id);
    if (!configuration) return false;
    const unit = units().find(item => String(item?.unit_id || '') === id);
    const values = copy(configuration);
    state.detail.values = values;
    $('target-detail-title').textContent = '编辑详情 · ' + (unit?.label || id);
    $('target-detail-description').textContent = '在这里编辑该目标的完整录入字段；修改会直接暂存，不会影响其他目标。';
    renderTargetDetailCategory(configuration);
    updateTargetDetailSummary(configuration);
    renderValueFields(
        $('target-detail-fields'),
        targetEditorDetailFields(type(), values),
        values,
        commitTargetDetail,
        null,
        '编辑详情',
    );
    return true;
}
function openTargetDetailForUnit(id, presetFields = []) {
    if (!state || type() === 'vocabulary') return false;
    const unitId = String(id || '');
    const configuration = configs().find(item => String(item.unit_id) === unitId);
    if (!configuration || !$('target-detail')) return false;
    state.detail = {
        unitId,
        focusFields: [...new Set(Array.isArray(presetFields) ? presetFields : [])],
        values: null,
    };
    state.lastModalTrigger = document.activeElement;
    renderTargetDetail();
    openTargetModal($('target-detail'));
    requestAnimationFrame(() => {
        const fields = state?.detail?.focusFields?.length
            ? state.detail.focusFields
            : targetEditorDetailFields(type(), configuration);
        const first = fields.find(key => $('target-detail-fields')?.querySelector('[data-target-field="' + CSS.escape(key) + '"]'));
        if (first) focusTargetDetailField(first);
    });
    return true;
}
function openTargetEditForUnit(id, presetFields = []) {
    return openTargetDetailForUnit(id, presetFields);
}
function targetEditorFocusField(fieldId, unitId = '', missing = []) {
    if (!targetEditorActive()) return false;
    if (fieldId === 'system-input-type') {
        const target = document.querySelector('[data-system-input-choice-group="system-input-type"] [aria-checked="true"]');
        target?.focus?.({ preventScroll: true });
        return Boolean(target);
    }
    const configuration = configs().find(item => String(item.unit_id) === String(unitId || systemInputSelectedUnitId))
        || configs()[0];
    if (!configuration) return false;
    const fieldKey = targetEditorFormFieldKeys[fieldId];
    const missingKeys = (Array.isArray(missing) ? missing : [])
        .map(label => targetEditorMissingFieldKeysByLabel[label])
        .filter(Boolean);
    const fields = fieldKey
        ? [fieldKey]
        : missingKeys.length
            ? missingKeys
            : targetEditorMissingFieldKeys(configuration);
    return openTargetEditForUnit(configuration.unit_id, fields);
}

const textbookCascadeFields = Object.freeze(systemInputCascadeFields('textbook'));
const targetCascadeFields = Object.freeze([...new Set([
    ...textbookCascadeFields,
    ...systemInputCascadeFields('paper'),
])]);

function targetCascadeInputType(key) {
    return textbookCascadeFields.includes(key) ? 'textbook' : 'paper';
}

function targetCascadeParents(key) {
    return systemInputCascadeParents(targetCascadeInputType(key), key);
}

function targetEditorBatchFieldsForPreset(configuration, presetFields = []) {
    const fields = targetEditorBatchFields(type(), configuration);
    const requestedFields = Array.isArray(presetFields) ? presetFields : [];
    const seeds = requestedFields.length ? requestedFields : targetEditorMissingFieldKeys(configuration);
    const selected = new Set(seeds.filter(key => fields.includes(key)));
    const visiting = new Set();
    const addParentPath = field => {
        if (visiting.has(field)) return;
        visiting.add(field);
        targetCascadeParents(field).forEach(parent => {
            if (!fields.includes(parent)) return;
            selected.add(parent);
            addParentPath(parent);
        });
        visiting.delete(field);
    };
    [...selected].forEach(addParentPath);
    return fields.filter(field => selected.has(field));
}

function targetEditorDetailFields(inputType = type(), values = {}) {
    return targetEditorBatchFields(inputType, values);
}

function targetPaperRegionState(values = {}) {
    return systemInputRegionCascadeState({
        provinceText: targetFieldValue(values, 'provinceId'),
        cityText: targetFieldValue(values, 'cityId'),
        stageText: text(targetFieldValue(values, 'stageId')),
    });
}

function targetCascadeParentsReady(key, values = {}) {
    if (key === 'platformTemplateName' && systemInputPlatformTemplateCatalog === null) return true;
    if (!systemInputCascadeParentsReady(targetCascadeInputType(key), key, values)) return false;
    if (targetCascadeInputType(key) !== 'paper') return true;
    if (key === 'platformTemplateName' && systemInputPlatformTemplateCatalog !== null) {
        return systemInputPlatformTemplateCatalogAssessment(values).missing.length === 0;
    }
    const region = targetPaperRegionState(values);
    // The shared graph checks that a parent has a value; the region index
    // additionally has to confirm that the value belongs to its parent.
    // Without this second check, an old city under a new province would leave
    // the district control enabled with an invalid, hidden selection.
    if (key === 'cityId') return Boolean(region.selectedProvince);
    if (key === 'districtIds') return Boolean(region.selectedCity);
    if (key === 'gradeId') return Boolean(region.selectedStage);
    return true;
}

function targetCascadeBatchReady(key, values = {}, selectedFields = new Set()) {
    if (key === 'platformTemplateName' && systemInputPlatformTemplateCatalog === null) return true;
    if (!targetCascadeParentsReady(key, values)) return false;
    if (key === 'platformTemplateName' && systemInputPlatformTemplateCatalog !== null) {
        const assessment = systemInputPlatformTemplateCatalogAssessment(values);
        const required = SYSTEM_INPUT_PLATFORM_TEMPLATE_SCOPE_FIELDS[assessment.kind] || [];
        if (required.some(field => !systemInputCascadeFieldSelected('paper', selectedFields, field)
            || !systemInputCascadeValuePresent(targetFieldValue(values, field)))) return false;
    }
    return systemInputCascadeBatchReady(targetCascadeInputType(key), key, values, selectedFields);
}

function targetCascadeBatchHint(key, values = {}, selectedFields = new Set()) {
    if (key === 'platformTemplateName' && systemInputPlatformTemplateCatalog === null) return '';
    if (key === 'platformTemplateName' && systemInputPlatformTemplateCatalog !== null) {
        const assessment = systemInputPlatformTemplateCatalogAssessment(values);
        const required = SYSTEM_INPUT_PLATFORM_TEMPLATE_SCOPE_FIELDS[assessment.kind] || [];
        const missing = required.find(field => !systemInputCascadeFieldSelected('paper', selectedFields, field)
            || !systemInputCascadeValuePresent(targetFieldValue(values, field)));
        if (missing) return `先勾选并选择${labels[missing] || missing}`;
        if (!['ready', 'matched'].includes(assessment.status)) return assessment.message;
    }
    return targetRegionCascadeHint(key, values)
        || systemInputCascadeHint(targetCascadeInputType(key), key, values, {
        selectedFields,
        labelFor: field => labels[field] || field,
        });
}

function targetCascadeChildren(key) {
    return systemInputCascadeChildren(targetCascadeInputType(key), key);
}

function targetCascadeHint(key, values = {}) {
    if (key === 'platformTemplateName' && systemInputPlatformTemplateCatalog === null) return '';
    if (key === 'platformTemplateName' && systemInputPlatformTemplateCatalog !== null) {
        const assessment = systemInputPlatformTemplateCatalogAssessment(values);
        if (assessment.status !== 'ready' && assessment.status !== 'matched') return assessment.message;
    }
    return targetRegionCascadeHint(key, values)
        || systemInputCascadeHint(targetCascadeInputType(key), key, values, {
            labelFor: field => labels[field] || field,
        });
}

function targetRegionCascadeHint(key, values = {}) {
    if (targetCascadeInputType(key) !== 'paper') return '';
    const regionKeys = new Set(['cityId', 'districtIds']);
    const sharedReady = systemInputCascadeParentsReady('paper', key, values);
    if (!sharedReady) return '';
    const indexes = typeof systemInputRegionIndexes !== 'undefined' ? systemInputRegionIndexes : {};
    const hasRegionIndex = Array.isArray(indexes?.provinces)
        && indexes.provinces.length
        && Array.isArray(indexes?.cities)
        && indexes.cities.length;
    if (regionKeys.has(key) && !hasRegionIndex) {
        const loadState = typeof systemInputRegionLoadState !== 'undefined'
            ? String(systemInputRegionLoadState || '').trim()
            : '';
        if (loadState === 'loading') return '正在加载省市区目录…';
        if (loadState === 'error') return '省市区目录加载失败，请返回后重试';
        return '省市区目录未就绪，请稍后重试';
    }
    const region = targetPaperRegionState(values);
    if (key === 'cityId' && systemInputCascadeValuePresent(targetFieldValue(values, 'provinceId'))
        && !region.selectedProvince) {
        return '当前省份未匹配地区目录，请重新选择省份';
    }
    if (key === 'districtIds' && systemInputCascadeValuePresent(targetFieldValue(values, 'cityId'))
        && !region.selectedCity) {
        return region.selectedProvince
            ? '当前城市不属于所选省份，请重新选择城市'
            : '当前省份未匹配地区目录，请重新选择省份';
    }
    if (key === 'gradeId' && systemInputCascadeValuePresent(targetFieldValue(values, 'stageId'))
        && !region.selectedStage) {
        return '当前学段未匹配年级目录，请重新选择学段';
    }
    return '';
}

function textbookCascadeOptionName(value) {
    return systemInputCascadeDisplayValue(value);
}

function textbookCascadeOptions(key, values = {}) {
    if (!textbookCascadeFields.includes(key)) return null;
    const records = Array.isArray(systemInputTextbookCatalog?.records)
        ? systemInputTextbookCatalog.records.filter(record => record && typeof record === 'object')
        : [];
    const valuesByCascadeKey = Object.fromEntries(
        textbookCascadeFields.map(field => [field, targetFieldValue(values, field)]),
    );
    const localKey = systemInputCascadeLocalField('textbook', key);
    const options = systemInputCascadeCatalogOptions({
        inputType: 'textbook',
        field: key,
        values: valuesByCascadeKey,
        records,
        fallback: systemInputTextbookCatalog?.options?.[localKey] || systemInputTextbookCatalog?.options?.[key],
        getRecordValue: (record, field) => record?.[systemInputCascadeLocalField('textbook', field)],
        currentValue: targetFieldValue(values, key),
        currentDetail: '当前填写值，待核对',
        // With a loaded catalog, a value that does not belong to the selected
        // parent must not remain an actionable option. The row status already
        // asks for review; showing the stale choice would let it be selected
        // again without repairing the path. If the parent path is incomplete,
        // the shared helper still preserves the saved value for review.
        preserveCurrent: records.length === 0,
    });
    return options.map(option => textbookCascadeOptionName(option.value));
}

function textbookCascadeParentsReady(key, values = {}) {
    return targetCascadeParentsReady(key, values);
}

function textbookCascadeChildren(key) {
    return targetCascadeChildren(key).filter(candidate => textbookCascadeFields.includes(candidate));
}

function textbookCascadeHint(key, values = {}) {
    return textbookCascadeFields.includes(key) ? targetCascadeHint(key, values) : '';
}

function inlineControl(configuration, key) {
    const input = ['textbookNameZh', 'textbookNameEn'].includes(key)
        ? el('textarea', 'target-inline target-inline-name')
        : el('input', 'target-inline');
    input.value = text(targetFieldValue(configuration, key));
    const unit = units().find(item => String(item.unit_id) === String(configuration.unit_id));
    input.setAttribute('aria-label', `${unit?.label || '当前条目'} · ${labels[key]}`);
    input.dataset.unitId = String(configuration.unit_id); input.dataset.field = key;
    input.autocomplete = 'off';
    if (input.tagName === 'TEXTAREA') {
        input.rows = 2;
        input.wrap = 'soft';
        input.spellcheck = false;
    }
    if (key === 'answerTimeMinutes') { input.type = 'number'; input.min = '1'; input.max = '60'; input.step = '1'; }
    if (['textbookUnit', 'textbookLesson'].includes(key)) {
        const select = el('select', 'target-inline');
        select.setAttribute('aria-label', input.getAttribute('aria-label'));
        select.dataset.unitId = input.dataset.unitId; select.dataset.field = key;
        const choices = textbookCascadeOptions(key, configuration) || [];
        const ready = textbookCascadeParentsReady(key, configuration);
        const current = text(targetFieldValue(configuration, key));
        select.append(new Option(
            current && !choices.includes(current)
                ? `${current}（待核对）`
                : ready ? '选择…' : (textbookCascadeHint(key, configuration) || '先选择上级教材项'),
            current && !choices.includes(current) ? current : '',
        ));
        choices.forEach(value => select.append(new Option(value, value)));
        select.value = current;
        select.disabled = !ready || (!choices.length && !current);
        select.addEventListener('change', () => commitInline(String(configuration.unit_id), key, select.value));
        return select;
    }
    function inputWithChange() {
        const update = () => commitInline(String(configuration.unit_id), key, key === 'answerTimeMinutes' && input.value ? Number(input.value) : input.value.trim());
        input.addEventListener('input', update);
        input.addEventListener('change', update);
        return input;
    }
    if (key === 'platformTemplateName') {
        const select = el('select', 'target-inline');
        select.setAttribute('aria-label', input.getAttribute('aria-label'));
        select.dataset.unitId = input.dataset.unitId; select.dataset.field = key;
        const templateAssessment = systemInputPlatformTemplateCatalog !== null
            ? systemInputPlatformTemplateCatalogAssessment(configuration)
            : null;
        select.dataset.targetPlaceholder = templateAssessment?.message || '选择平台题型模板';
        select.append(new Option('', ''));
        const current = text(targetFieldValue(configuration, key));
        if (current) select.append(new Option(current, current));
        select.value = current;
        // Large platform catalogues are searchable, so only materialize the
        // current value here. The custom menu asks for a bounded result page
        // when it opens or when the user types a query.
        select._targetSelectOptionProvider = query => targetPlatformTemplatePickerOptions(configuration, query);
        select.disabled = Boolean(templateAssessment && (templateAssessment.missing.length || templateAssessment.status === 'unavailable'));
        const update = () => commitInline(String(configuration.unit_id), key, select.value);
        select.addEventListener('change', update);
        // Keep programmatic updates from older integrations working while the
        // visible control remains a picker rather than a free-text input.
        select.addEventListener('input', update);
        return select;
    }
    return inputWithChange();
}
function refreshSystemInputTargetEditor({ force = false } = {}) {
    if (!state || rendering || !$('target-rows')) return;
    if (state.inputType !== type()) {
        state.inputType = type(); state.defaults = {}; state.overrides = {}; state.commonPending = {}; state.undo = null;
        state.selected.clear();
        state.batch = null; state.detail = null;
        closeTargetModal($('target-batch'), { restoreFocus: false });
        closeTargetModal($('target-detail'), { restoreFocus: false });
        closeTargetModal($('target-common'), { restoreFocus: false });
        renderCommon();
    }
    rendering = true;
    try {
        const rows = configs();
        $('target-overview').dataset.inputType = type();
        const syncCard = $('target-catalog-sync');
        if (syncCard) syncCard.hidden = type() !== 'textbook';
        const templateSyncCard = $('target-catalog-sync-template');
        if (templateSyncCard) templateSyncCard.hidden = type() !== 'paper';
        const isVocabulary = type() === 'vocabulary';
        const overview = $('target-overview');
        const actionBar = $('target-common-open')?.closest('.target-action-bar');
        const selection = $('target-select-visible')?.closest('.target-selection');
        const overviewCopy = overview?.querySelector('.target-overview-head p');
        const drawerCopy = $('system-input-drawer-title')?.parentElement?.querySelector('p');
        // Every target count uses the same list workspace. A one-row task is
        // still a batch of one: it keeps the same editing surface and makes
        // future fields/behaviour land in one place.
        $('target-overview').hidden = false;
        const hideBatchTools = isVocabulary || !rows.length;
        overview?.classList.toggle('is-empty', hideBatchTools);
        if (!rows.length || isVocabulary) closeTargetModal($('target-common'), { restoreFocus: false });
        $('target-common-open').hidden = !rows.length || isVocabulary;
        $('target-template-open').hidden = !rows.length || isVocabulary;
        $('target-batch-open').hidden = !rows.length || isVocabulary;
        $('system-input-save-template-btn').hidden = isVocabulary;
        if (actionBar) actionBar.hidden = hideBatchTools;
        if (selection) selection.hidden = isVocabulary || !rows.length;
        // The old single-target form remains only as an internal source for
        // collection/validation. Both one- and multi-target rows use the same
        // detail dialog; the batch tool is available only from its explicit
        // toolbar action.
        $('target-editor-form-source').hidden = true;
        $('target-next-issue').hidden = isVocabulary || !rows.length;
        if (overviewCopy) overviewCopy.textContent = isVocabulary
            ? '词汇配置可以先保存；页面录入适配器接入后，再补充可编辑字段。'
            : '所有目标都在这张清单中编辑；选中条目后可统一补齐或替换字段。';
        if (drawerCopy) drawerCopy.textContent = isVocabulary
            ? '先保存词汇配置；页面录入适配器接入后再提交。'
            : '统一在目标清单中核对和编辑；保存后由录入流程提交。';
        const all = rows.map(configuration => ({ configuration, status: assessment(configuration), unit: units().find(unit => String(unit.unit_id) === String(configuration.unit_id)) }));
        const pending = all.filter(row => row.status.missing.length || row.status.pending || row.status.unavailable);
        const countLabel = {
            试卷: '套试卷',
            课文: '条课文',
            词汇: '条词汇',
        }[targetEditorTypeLabel()] || '条目标';
        $('target-overview-count').textContent = `${rows.length} ${countLabel} · ${pending.length} 条待处理`;
        $('system-input-save-btn').textContent = `保存录入目标（${rows.length} 条）`;
        const selectionNote = state.selected.size ? `已选 ${state.selected.size} 条用于批量修改` : '先勾选条目';
        $('target-selection-note').textContent = selectionNote;
        $('target-batch-open').dataset.tooltip = `批量修改 · ${selectionNote}`;
        $('target-batch-open').disabled = isVocabulary || !state.selected.size;
        $('target-next-issue').disabled = isVocabulary || !pending.length;
        $('target-undo').disabled = !state.undo;
        const fields = type() === 'textbook'
            ? ['textbookNameZh', 'textbookNameEn', 'textbookUnit', 'textbookLesson']
            : type() === 'paper'
                ? ['paperName', 'platformTemplateName', 'answerTimeMinutes']
                : [];
        const header = $('target-columns'); header.replaceChildren();
        [isVocabulary ? '文档内容' : '选择 / 文档内容', ...fields.map(key => labels[key]), '状态 / 操作'].forEach(label => header.append(el('th', '', label)));
        const body = $('target-rows');
        // Keep the active row editor stable while typing or using a stepper.
        // A focused row action/source button is not an edit session and must
        // not prevent async catalogue/template data from refreshing the list.
        if (!force && targetEditorShouldPreserveInlineFocus(body)) return;
        body.replaceChildren();
        const query = systemInputNormalizeLabel($('target-search')?.value || '');
        const onlyPending = $('target-filter')?.value === 'pending';
        all.filter(({ configuration, unit, status }) => (!onlyPending || status.missing.length || status.pending || status.unavailable)
            && (!query || systemInputNormalizeLabel([unit?.label, ...Object.values(configuration).map(text)].join(' ')).includes(query)))
            .forEach(({ configuration, status, unit }) => {
                const id = String(configuration.unit_id); const row = el('tr', state.changed.has(id) ? 'is-changed' : '');
                const source = el('td', 'target-source');
                const label = el(status.unavailable ? 'div' : 'label', 'target-source-choice');
                if (!status.unavailable) {
                    const rowCheckbox = createTargetCheckbox({
                        checked: state.selected.has(id),
                        label: `选择${unit?.label || id}用于批量修改`,
                        onChange: checked => { checked ? state.selected.add(id) : state.selected.delete(id); refreshSelection(); },
                    });
                    rowCheckbox.input.dataset.unitId = id;
                    label.append(rowCheckbox.wrap);
                }
                label.append(el('strong', '', unit?.label || id)); source.append(label);
                const sourceButton = button('查看来源', () => showSource(unit), 'btn-text btn-sm'); source.append(sourceButton); row.append(source);
                fields.forEach(key => {
                    const cell = el('td', `target-cell-${key}`); cell.dataset.label = labels[key];
                    const control = inlineControl(configuration, key);
                    cell.append(control);
                    if (key === 'answerTimeMinutes') enhanceTargetNumberControl(control, { min: 1, max: 60, label: labels[key] });
                    // The control is created while the row is detached. Wrap
                    // it only after it has a host so every row gets the same
                    // custom trigger and its own menu ID.
                    if (control.tagName === 'SELECT') {
                        enhanceTargetSelect(control);
                        control._targetSelectSync?.();
                    }
                    row.append(cell);
                });
                const statusCell = el('td', 'target-row-state'); statusCell.dataset.label = '状态 / 操作';
                statusCell.append(el('span', `target-status${status.unavailable || status.missing.length || status.pending ? ' is-attention' : ''}`, status.label));
                if (status.missing.length) statusCell.append(el('small', '', status.missing.join('、')));
                if (status.catalog) statusCell.append(el('small', '', status.catalog.label));
                if (status.unavailable) statusCell.append(el('small', '', `${status.reason}，当前仅可保存配置。`));
                if ((state.overrides[id] || []).length) statusCell.append(el('small', '', '共用字段已单独设置'));
                if (!status.unavailable) {
                    const actions = el('div', 'target-row-actions');
                    // 只保留「编辑详情」一个入口——无论配置是否齐全都能编辑。
                    // 配置齐全的条目也可能想继续调整省份/年级等配置字段，而这些
                    // 字段并不在行的内联单元格里；若入口仅对缺失字段开放，配置齐全
                    // 后这些配置就再也改不了，等于把目标“锁死”。存在待补字段时，
                    // 状态列已用红色小字列出缺失字段作为提示，点击「编辑详情」会
                    // 自动聚焦到缺失字段，无需再单设一个「编辑待补字段」按钮。
                    actions.append(button(
                        '编辑详情',
                        () => openTargetEditForUnit(
                            id,
                            status.missing.length ? targetEditorMissingFieldKeys(configuration) : [],
                        ),
                        'btn-text btn-sm',
                    ));
                    if (status.pending && !status.missing.length) {
                        const acknowledge = button('已核对目录', () => { state.acknowledgements[id] = status.fingerprint; document.activeElement?.blur(); refreshSystemInputTargetEditor(); scheduleTargetEditorDraft(); }, 'btn-text btn-sm');
                        acknowledge.dataset.targetCatalogAck = 'true';
                        actions.append(acknowledge);
                    }
                    if (actions.children.length) statusCell.append(actions);
                }
                row.append(statusCell); body.append(row);
            });
        const hasRows = body.children.length > 0;
        const table = $('target-overview')?.querySelector('.target-table');
        if (table) table.hidden = !hasRows;
        $('target-empty').hidden = hasRows;
        $('target-select-visible').disabled = !hasRows;
        const commonText = commonKeys().map(key => text(targetFieldValue(state.commonPending || state.defaults, key))).filter(Boolean).join(' · ');
        $('target-common-summary').textContent = commonText || '设置一次，应用到本批次；单独设置的条目会保留';
        // Platform template names are provided by the bounded lazy picker;
        // the legacy datalist is kept empty so a catalogue refresh never
        // recreates one hidden native option per server record.
        $('target-platform-names').replaceChildren();
    } finally { rendering = false; }
}
// Async catalog/region/template reads can finish after the drawer and one of
// its child dialogs are already open. Repaint the shared fields in place so
// the newly loaded siblings become available without asking the user to close
// and reopen the dialog.
function targetEditorRefreshAfterDataLoad() {
    if (!state || rendering || !targetEditorActive()) return false;
    renderCommon();
    refreshSystemInputTargetEditor();
    if (state.batch && targetModalIsOpen($('target-batch'))) {
        const fields = targetEditorBatchFields(type(), state.batch.values);
        renderTargetBatchCategory();
        renderValueFields($('target-batch-fields'), fields, state.batch.values, previewBatch, state.batch.fields);
        previewBatch();
    }
    if (state.detail && targetModalIsOpen($('target-detail'))) renderTargetDetail();
    return true;
}
function refreshSelection() {
    $('target-selection-note').textContent = `已选 ${state.selected.size} 条用于批量修改`;
    $('target-batch-open').disabled = !state.selected.size;
    updateSystemInputAppTemplateAction?.(systemInputInteractionWorkspace()?.system_input);
    if (state.batch && targetModalIsOpen($('target-batch'))) {
        renderTargetBatchCategory();
        previewBatch();
    }
}
function showSource(unit) {
    const section = $('target-source-preview');
    state.lastModalTrigger = document.activeElement;
    openTargetModal(section);
    $('target-source-title').textContent = `文档内容 · ${unit?.label || '当前条目'}`;
    const ids = new Set(unit?.item_ids || unit?.source_item_ids || []);
    const segments = (systemInputInteractionWorkspace()?.system_input?.content_segments || []).filter(segment => String(segment.unit_id) === String(unit?.unit_id));
    const items = (systemInputInteractionWorkspace()?.items || []).filter(item => ids.has(item.item_id)
        || (unit?.unit_id && (item.metadata?.unit_id === unit.unit_id || item.unit_id === unit.unit_id)));
    const visibleCount = segments.length ? Math.min(segments.length, 8) : Math.min(items.length, 6);
    const totalCount = segments.length || items.length;
    const sourceDescription = $('target-source-description');
    if (sourceDescription) sourceDescription.textContent = totalCount > visibleCount
        ? `只读预览，当前显示前 ${visibleCount} 段，共 ${totalCount} 段；完整内容请返回内容核对查看。`
        : '只读原文片段，用于确认条目边界和名称。';
    $('target-source-text').textContent = segments.length ? segments.slice(0, 8).map(segment => segment.raw_text || segment.tts_text || '').filter(Boolean).join('\n\n') : items.length ? items.slice(0, 6).map(item => item.text || item.display_text || item.content?.text || item.metadata?.text || '').filter(Boolean).join('\n\n')
        || '此条目没有可显示的原文片段。' : `来源：${unit?.label || '当前条目'}\n此条目没有可显示的原文片段，可返回内容核对查看。`;
}

function fieldOptions(key, values) {
    const cascade = targetPaperRegionState(values);
    const preserveCurrent = (options, field, includeCurrent = true) => {
        const source = Array.isArray(options) ? options : [];
        if (!includeCurrent) return source;
        const raw = targetFieldValue(values, field);
        const current = field === 'districtIds'
            ? (Array.isArray(raw) ? raw : systemInputCascadeValuePresent(raw) ? [raw] : [])
            : systemInputCascadeValuePresent(raw) ? [raw] : [];
        const matches = (value, option) => targetCascadeChoiceMatches(value, option);
        return [...source, ...current.filter(value => !source.some(option => matches(value, option)))];
    };
    // Keep a saved choice visible while the bundled region snapshot is still
    // loading (or when an older snapshot no longer contains that choice).
    // Once the parent is changed, the cascade reset clears the preserved
    // descendant, so this cannot resurrect a stale child after a real edit.
    if (key === 'provinceId') return preserveCurrent(systemInputRegionIndexes.provinces, key);
    if (key === 'cityId') return preserveCurrent(
        cascade.cities,
        key,
        !cascade.selectedProvince || !systemInputRegionIndexes.cities?.length,
    );
    if (key === 'districtIds') return preserveCurrent(
        cascade.districts,
        key,
        !cascade.selectedCity || !systemInputRegionIndexes.districts?.length,
    );
    if (key === 'stageId') return SYSTEM_INPUT_STAGES;
    if (key === 'gradeId') return cascade.grades;
    if (key === 'paperType') {
        return systemInputCascadeAvailable('paper', key, values)
            ? SYSTEM_INPUT_PAPER_TYPES
            : [];
    }
    if (key === 'paperCategory') return SYSTEM_INPUT_PAPER_CATEGORIES;
    if (key === 'platformTemplateName') return targetPlatformTemplateChoices(values);
    if (key === 'textbookForm') return SYSTEM_INPUT_TEXTBOOK_FORMS;
    if (textbookCascadeFields.includes(key)) return textbookCascadeOptions(key, values);
    return null;
}
// 区县使用可删除 tag（chip）多选，替代原来的原生 multiple select。复用主表单的
// chip 视觉：每个已选项显示为胶囊，带 × 删除按钮；输入框即搜索框，可点选候选。
function buildTargetDistrictPicker({ key, values, options, cascadeReady, ariaContext, onChange }) {
    const root = el('div', 'target-district-picker');
    root.classList.toggle('is-disabled', !cascadeReady);
    const chips = el('div', 'target-district-chips');
    const input = el('input', 'target-district-search');
    input.type = 'search'; input.autocomplete = 'off'; input.spellcheck = false;
    input.placeholder = cascadeReady ? '搜索区县，可多选' : '先选择城市';
    input.disabled = !cascadeReady;
    input.setAttribute('role', 'combobox'); input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-label', `${ariaContext} · ${labels[key]}`);
    const menu = el('div', 'target-district-menu'); menu.setAttribute('role', 'listbox'); menu.hidden = true;
    root.append(chips, input, menu);

    const getSelected = () => {
        const raw = targetFieldValue(values, key);
        return Array.isArray(raw)
            ? raw
            : systemInputCascadeValuePresent(raw) ? [raw] : [];
    };
    const toStored = option => {
        const label = text(option);
        const identifier = option && typeof option === 'object' ? (option.id ?? option.value) : undefined;
        return systemInputCascadeValuePresent(identifier) ? { id: identifier, name: label } : { name: label };
    };
    const commit = () => { onChange(key, values); scheduleTargetEditorDraft(); };
    const hideMenu = () => {
        menu.hidden = true; input.setAttribute('aria-expanded', 'false');
        root.classList.remove('is-open');
    };
    const renderChips = () => {
        chips.replaceChildren();
        getSelected().forEach((choice, index) => {
            const chip = el('span', 'system-input-chip', systemInputDisplayValue(choice));
            const remove = el('button', '', '×');
            remove.type = 'button';
            remove.setAttribute('aria-label', `移除${systemInputDisplayValue(choice)}`);
            remove.addEventListener('click', () => {
                const next = getSelected().filter((_, i) => i !== index);
                values[key] = next;
                renderChips(); renderMenu(input.value); commit();
            });
            chip.append(remove);
            chips.append(chip);
        });
        chips.hidden = !getSelected().length;
    };
    const renderMenu = query => {
        menu.replaceChildren();
        const q = systemInputNormalizeLabel(query);
        const selected = getSelected();
        let rendered = 0;
        (Array.isArray(options) ? options : []).forEach(option => {
            const label = text(option);
            if (q && !systemInputNormalizeLabel(label).includes(q)) return;
            const active = selected.some(choice => targetCascadeChoiceMatches(choice, option));
            const item = el('button', 'target-select-option', label);
            item.type = 'button'; item.setAttribute('role', 'option');
            item.setAttribute('aria-selected', active ? 'true' : 'false');
            item.disabled = !cascadeReady;
            item.addEventListener('click', () => {
                const next = getSelected().filter(choice => !targetCascadeChoiceMatches(choice, option));
                if (active) values[key] = next;
                else { next.push(toStored(option)); values[key] = next; }
                renderChips(); renderMenu(input.value); commit();
            });
            menu.append(item); rendered++;
        });
        if (!rendered) {
            menu.append(el('span', 'target-select-empty', q ? '无匹配区县' : '暂无可选区县'));
        }
        const rect = input.getBoundingClientRect();
        const below = window.innerHeight - rect.bottom - 10;
        const available = Math.max(96, Math.min(240, below));
        Object.assign(menu.style, {
            position: 'fixed', left: `${Math.max(8, rect.left)}px`, top: `${rect.bottom + 4}px`,
            width: `${Math.min(rect.width, window.innerWidth - rect.left - 8)}px`, maxHeight: `${available}px`,
        });
    };
    const showMenu = () => {
        if (!cascadeReady || menu.hidden === false) return;
        // 同一时刻只开一个候选下拉菜单。
        districtPickers.forEach(picker => { if (picker !== root) picker._districtHide?.(); });
        menu.hidden = false; input.setAttribute('aria-expanded', 'true');
        root.classList.add('is-open'); renderMenu(input.value);
    };
    input.addEventListener('focus', showMenu);
    input.addEventListener('input', () => { if (menu.hidden) showMenu(); else renderMenu(input.value); });
    input.addEventListener('click', () => { if (menu.hidden) showMenu(); else renderMenu(input.value); });
    root._districtHide = hideMenu;
    districtPickers.add(root);
    bindDistrictDocHandlers();

    const setDisabled = disabled => {
        const blocked = Boolean(disabled) || !cascadeReady;
        input.disabled = blocked;
        input.placeholder = blocked ? '先选择城市' : '搜索区县，可多选';
        if (blocked) hideMenu();
        root.classList.toggle('is-disabled', blocked);
    };
    renderChips();
    return { root, setDisabled };
}
// 区县 tag 多选使用独立的下拉关闭监听：内部滚动不关闭、页面滚动/外部点击/缩放才关闭。
function bindDistrictDocHandlers() {
    if (districtDocumentBound || !document) return;
    districtDocumentBound = true;
    document.addEventListener('pointerdown', event => {
        districtPickers.forEach(picker => {
            if (!document.contains(picker)) { districtPickers.delete(picker); return; }
            if (!picker.contains(event.target)) picker._districtHide?.();
        });
    }, true);
    window.addEventListener('resize', () => {
        districtPickers.forEach(picker => picker._districtHide?.());
    });
    document.addEventListener('scroll', event => {
        // 菜单内部滚动不应收起；只有工作区/页面滚动才收起。
        if (event.target?.closest?.('.target-district-menu')) return;
        districtPickers.forEach(picker => picker._districtHide?.());
    }, true);
}
function renderValueFields(container, keys, values, onChange, selectedFields = null, ariaContext = selectedFields ? '批量修改' : '批次默认') {
    const active = document.activeElement;
    const focusedFieldKey = container?.contains?.(active)
        ? active.closest?.('.target-field')?.dataset?.targetField || ''
        : '';
    // Replacing a field grid can otherwise leave the old hidden source select
    // as document.activeElement for one frame. Release it before the subtree
    // is replaced so the surrounding dialog can safely remain aria-hidden.
    releaseTargetNativeSelectFocus();
    container.replaceChildren();
    keys.forEach(key => {
        const lazyPlatformTemplate = key === 'platformTemplateName';
        const options = lazyPlatformTemplate ? null : fieldOptions(key, values);
        let control; let districtApi = null;
        const isCascade = targetCascadeFields.includes(key);
        const cascadeHint = selectedFields
            ? targetEditorBatchHint(key, values, selectedFields)
            : targetCascadeHint(key, values);
        const cascadeAvailable = !isCascade
            || systemInputCascadeAvailable(targetCascadeInputType(key), key, values);
        const cascadeReady = cascadeAvailable && (selectedFields
            ? targetEditorBatchReady(key, values, selectedFields)
            : targetCascadeParentsReady(key, values));
        if (options || lazyPlatformTemplate) {
            // 区县走独立的可删除 tag 多选；其余选择项仍用带搜索的 enhanceTargetSelect。
            if (key === 'districtIds') {
                districtApi = buildTargetDistrictPicker({ key, values, options, cascadeReady, ariaContext, onChange });
                control = districtApi.root;
            } else {
                control = el('select');
                // Keep one empty native option so programmatic form updates can
                // represent “no value” without inventing a visible menu choice.
                // The custom menu deliberately filters it out above.
                control.append(new Option('', ''));
                if (lazyPlatformTemplate) {
                    const templateAssessment = systemInputPlatformTemplateCatalog !== null
                        ? systemInputPlatformTemplateCatalogAssessment(values)
                        : null;
                    const current = text(targetFieldValue(values, key));
                    if (current) control.append(new Option(current, current));
                    control.value = current;
                    control.dataset.targetPlaceholder = templateAssessment?.message || '选择平台题型模板';
                    control._targetSelectOptionProvider = query => targetPlatformTemplatePickerOptions(values, query);
                    control.disabled = Boolean(templateAssessment && (
                        templateAssessment.missing.length || templateAssessment.status === 'unavailable'
                    ));
                } else {
                    options.forEach((option, index) => {
                        const nativeOption = new Option(text(option), typeof option === 'string' ? text(option) : String(index));
                        nativeOption.dataset.targetOptionIndex = String(index);
                        control.append(nativeOption);
                    });
                    const index = options.findIndex(option => text(option) === text(targetFieldValue(values, key)));
                    control.value = index >= 0 ? control.options[index + (control.options[0]?.value === '' ? 1 : 0)]?.value || '' : '';
                }
            }
            const handleSelectChange = () => {
                const before = copy(targetFieldValue(values, key));
                const selected = option => {
                    if (option === undefined || option === null) return '';
                    if (typeof option === 'string') return text(option);
                    const label = text(option);
                    // paperCategory and textbookForm are page-form scalars.
                    // Storing a choice object for either one would reach the
                    // executor as "[object Object]" instead of the visible
                    // form value. The other choices are replayable page
                    // references and must retain their stable ID plus label.
                    if (key === 'paperCategory' || key === 'textbookForm') return label;
                    const identifier = option?.id ?? option?.value;
                    return identifier === undefined
                        ? { name: label }
                        : { id: identifier, name: label };
                };
                const optionIndex = option => {
                    if (!option) return -1;
                    const indexed = option.dataset?.targetOptionIndex;
                    if (indexed !== undefined && indexed !== '') {
                        const parsed = Number(indexed);
                        return Number.isInteger(parsed) ? parsed : -1;
                    }
                    if (option.value === '') return -1;
                    const parsed = Number(option.value);
                    if (Number.isInteger(parsed) && parsed >= 0 && parsed < options.length) return parsed;
                    return options.findIndex(candidate => text(candidate) === text(option.textContent));
                };
                const selectedValue = option => {
                    const index = optionIndex(option);
                    return index >= 0 && index < options.length ? selected(options[index]) : '';
                };
                values[key] = lazyPlatformTemplate
                    ? control.value === '' ? '' : control.value
                    : control.multiple
                        ? Array.from(control.selectedOptions).map(selectedValue).filter(value => value !== '')
                        : control.value === '' ? '' : selectedValue(control.selectedOptions[0]);
                if (key === 'platformTemplateName') {
                    const chosenName = text(values[key]);
                    const matchedTemplate = systemInputPlatformTemplateCatalogAssessment(values).candidates.find(template => (
                        systemInputNormalizeLabel(template.name) === systemInputNormalizeLabel(chosenName)
                    ));
                    values.platformTemplateName = matchedTemplate?.name || chosenName;
                    if (matchedTemplate?.platform_template_id) values.platformTemplateId = matchedTemplate.platform_template_id;
                    else delete values.platformTemplateId;
                    if (matchedTemplate?.platform_template_version) values.platformTemplateVersion = matchedTemplate.platform_template_version;
                    else delete values.platformTemplateVersion;
                }
                const changed = !equal(before, values[key]);
                if (changed && isCascade) {
                    // A changed parent invalidates every descendant. The
                    // shared graph clears values and batch flags together so
                    // common settings and batch editing cannot diverge.
                    systemInputCascadeResetDescendants(
                        targetCascadeInputType(key),
                        values,
                        key,
                        { selectedFields },
                    );
                }
                const templateScopeChanged = changed
                    && ['paperCategory', 'provinceId', 'cityId', 'stageId', 'gradeId'].includes(key)
                    && (key !== 'stageId' && key !== 'gradeId' || systemInputPlatformTemplateKind(values) === 'paper');
                if (templateScopeChanged) {
                    delete values.platformTemplateName;
                    delete values.platformTemplateId;
                    delete values.platformTemplateVersion;
                    selectedFields?.delete?.('platformTemplateName');
                }
                onChange(key, values); scheduleTargetEditorDraft();
                if (changed && (targetCascadeChildren(key).length || templateScopeChanged)) {
                    renderValueFields(container, keys, values, onChange, selectedFields, ariaContext);
                }
            };
            if (key !== 'districtIds') {
                control.addEventListener('change', handleSelectChange);
                // Older callers used an input event for text controls. Supporting
                // it here keeps the reusable picker compatible with those callers.
                control.addEventListener('input', handleSelectChange);
            }
        } else {
            control = el('input'); control.value = text(targetFieldValue(values, key)); control.autocomplete = 'off';
            if (['year', 'answerTimeMinutes'].includes(key)) { control.type = 'number'; control.min = key === 'year' ? '2000' : '1'; control.max = key === 'year' ? '2100' : '60'; }
            control.addEventListener('input', () => { values[key] = control.type === 'number' && control.value ? Number(control.value) : control.value.trim(); onChange(key, values); scheduleTargetEditorDraft(); });
        }
            control.setAttribute('aria-label', `${ariaContext} · ${labels[key]}`);
        const field = labeledControl(labels[key], control, Boolean(requiredMarkers[key]));
        field.dataset.targetField = key;
        if (isCascade) {
            field.classList.add('target-field-cascade');
            control.dataset.targetCascade = 'true';
            control.dataset.targetCascadeReady = cascadeReady ? 'true' : 'false';
        }
        // 区县 tag 多选用 setDisabled 控制；其余控件直接读写 .disabled。
        const applyFieldDisabled = disabled => {
            if (districtApi) districtApi.setDisabled(Boolean(disabled));
            else control.disabled = Boolean(disabled);
        };
        if (selectedFields) {
            const cascadeUnavailable = isCascade && !cascadeReady;
            // A child field cannot be applied until its complete parent path
            // exists. Disable its field checkbox as well as the control so a
            // user cannot create a checked-but-uneditable batch row.
            if (cascadeUnavailable) selectedFields.delete(key);
            const fieldCheckbox = createTargetCheckbox({
                checked: selectedFields.has(key),
                disabled: cascadeUnavailable,
                label: `批量修改${labels[key]}`,
                onChange: checked => {
                    checked ? selectedFields.add(key) : selectedFields.delete(key);
                    applyFieldDisabled(!checked || (isCascade && !targetEditorBatchReady(key, values, selectedFields)));
                    control._targetSelectSync?.(); control._targetNumberSync?.();
                    onChange(key, values); scheduleTargetEditorDraft();
                    // Checking a parent with an existing value should unlock
                    // its children immediately; no extra value change should
                    // be required to refresh the cascade.
                    if (targetCascadeChildren(key).length) renderValueFields(container, keys, values, onChange, selectedFields, ariaContext);
                },
            });
            applyFieldDisabled(!fieldCheckbox.input.checked || cascadeUnavailable);
            const heading = field.firstChild; heading.prepend(fieldCheckbox.wrap);
        }
        if (cascadeHint) field.append(el('small', 'target-field-hint', cascadeHint));
        container.append(field);
        if (['year', 'answerTimeMinutes'].includes(key)) {
            enhanceTargetNumberControl(control, {
                min: key === 'year' ? 2000 : 1,
                max: key === 'year' ? 2100 : 60,
                label: labels[key],
            });
        }
        if (control.tagName === 'SELECT') {
            if (!selectedFields && isCascade && !cascadeReady) control.disabled = true;
            enhanceTargetSelect(control);
        }
    });
    // A parent selection rebuilds the field grid. Restore focus to the field
    // that initiated the rebuild so keyboard users do not get dropped onto
    // the document body after every cascade choice.
    if (focusedFieldKey) {
        const field = container.querySelector(`[data-target-field="${CSS.escape(focusedFieldKey)}"]`);
        const control = field?.querySelector(
            '.target-select-trigger:not(:disabled), input:not([type="hidden"]):not([disabled]), textarea:not([disabled]), button:not([disabled])',
        );
        control?.focus?.({ preventScroll: true });
    }
}
function updateConfigurations(result, { defaults = null } = {}) {
    const oldMetadata = targetEditorState();
    state.undo = { changes: result.changes, metadata: oldMetadata };
    if (defaults) state.defaults = copy(defaults);
    result.configurations.forEach(configuration => {
        const id = String(configuration.unit_id); systemInputUnitDrafts.set(id, configuration);
        if (!defaults) targetEditorRecordManual(configuration);
        state.observed.set(id, copy(configuration));
    });
    result.updatedUnitIds.forEach(markChanged);
    state.review = result.review || [];
    populateSystemInputUnitForm(systemInputInteractionWorkspace()?.system_input);
    refreshSystemInputTargetEditor(); scheduleTargetEditorDraft();
    const message = result.updatedUnitIds.length ? `已修改 ${result.updatedUnitIds.length} 条、${result.changes.length} 个字段，可撤销。${state.review.length ? '关联目录需要重新核对。' : ''}` : '没有需要修改的字段。';
    $('target-operation-status').textContent = message;
}
function updateBatchModalPresentation() {
    const title = $('target-batch-title');
    const description = title?.closest('.target-modal-heading')?.querySelector('p');
    const apply = $('target-batch-apply');
    const templatePreview = state?.batch?.context === 'template';
    const templateName = String(state?.batch?.templateName || '').trim();
    if (title) title.textContent = templatePreview ? '方案预览' : '批量修改';
    if (description) description.textContent = templatePreview
        ? `确认「${templateName || '已选方案'}」将写入的字段和值，再应用到所选条目。`
        : type() === 'paper'
            ? '试卷分类由文档自动识别；先勾选需要批量写入的字段，再选择要写入的值。'
            : '先勾选字段，再选择要写入的值；级联字段必须按上级逐级解锁。';
    if (apply) apply.textContent = templatePreview ? '确认并应用方案' : '应用本次修改';
    renderTargetBatchCategory();
}
function prepareBatch(values = null, presetFields = null, {
    open = true,
    mode = 'fill-empty',
    context = 'batch',
    templateName = '',
} = {}) {
    const batchValues = targetEditorBatchCascadeValues(
        targetEditorWithPaperDefaults(
            copy(values || systemInputUnitDrafts.get(String(systemInputSelectedUnitId)) || {}),
        ),
    );
    const fields = targetEditorBatchFields(type(), batchValues);
    const selectedFields = Array.isArray(presetFields)
        ? presetFields.filter(key => fields.includes(key))
        : [];
    state.batch = {
        values: batchValues,
        fields: new Set(selectedFields),
        result: null,
        context: context === 'template' ? 'template' : 'batch',
        templateName: context === 'template' ? String(templateName || '').trim() : '',
    };
    updateBatchModalPresentation();
    if (open) {
        state.lastModalTrigger = document.activeElement;
        openTargetModal($('target-batch'));
    }
    $('target-batch-mode').value = mode === 'overwrite' ? 'overwrite' : 'fill-empty';
    $('target-batch-mode')._targetSelectSync?.();
    renderValueFields($('target-batch-fields'), fields, state.batch.values, previewBatch, state.batch.fields);
    previewBatch();
}
function previewBatch() {
    if (!state?.batch) return;
    const { values, fields } = state.batch;
    const result = systemInputBatchApply(configs(), { inputType: type(), unitIds: [...state.selected], fields: [...fields], values, mode: $('target-batch-mode').value, compatible: targetCompatibility });
    state.batch.result = result;
    const overwritten = result.changes.filter(change => change.before !== undefined && text(change.before));
    $('target-batch-preview').textContent = `将修改 ${result.updatedUnitIds.length} 条的${[...fields].map(key => labels[key]).join('、') || '所选字段'}，共 ${result.changes.length} 处变化；${overwritten.length} 处已有值将被替换。`;
    $('target-batch-apply').disabled = !result.changes.length;
}
function applyCommon() {
    if (!targetEditorCanSave()) return;
    const result = systemInputBatchUpdateDefaults(configs(), { inputType: type(), previousDefaults: state.defaults, defaults: state.commonPending, overrides: state.overrides, compatible: targetCompatibility });
    updateConfigurations(result, { defaults: state.commonPending });
    closeTargetModal($('target-common'));
}
function renderCommon() {
    state.commonPending = state.commonPending || copy(state.defaults);
    const updateCommonSummary = () => {
        const summary = $('target-common-summary');
        if (!summary) return;
        const commonText = commonKeys().map(key => text(targetFieldValue(state.commonPending, key))).filter(Boolean).join(' · ');
        summary.textContent = commonText || '设置一次，应用到本批次；单独设置的条目会保留';
    };
    const book = $('target-book');
    book.hidden = type() !== 'textbook';
    book.dataset.targetPlaceholder = '选择教材，一次带入版本、学段、年级和册别';
    book.closest('.target-select')?.toggleAttribute('hidden', book.hidden);
    book.replaceChildren(new Option('选择教材，一次带入版本、学段、年级和册别', ''));
    const records = systemInputTextbookCatalog?.records || [];
    const books = [...new Map(records.map(record => {
        const values = { textbookVersion: text(record.version), textbookStage: text(record.stage), textbookGrade: text(record.grade), textbookVolume: text(record.volume) };
        return [JSON.stringify(values), values];
    })).values()];
    books.forEach((values, index) => book.append(new Option(Object.values(values).join(' / '), String(index))));
    // Keep the catalogue shortcut in sync with the actual common values. A
    // re-render used to reset the trigger to its placeholder even though the
    // four directory fields were still populated, and clearing the shortcut
    // left those fields behind as a stale hidden selection.
    const currentBookValues = commonKeys()
        .filter(key => ['textbookVersion', 'textbookStage', 'textbookGrade', 'textbookVolume'].includes(key))
        .reduce((values, key) => ({ ...values, [key]: text(targetFieldValue(state.commonPending, key)) }), {});
    const selectedBookIndex = type() === 'textbook' && books.length
        ? books.findIndex(values => ['textbookVersion', 'textbookStage', 'textbookGrade', 'textbookVolume']
            .every(key => currentBookValues[key] && currentBookValues[key] === values[key]))
        : -1;
    book.value = selectedBookIndex >= 0 ? String(selectedBookIndex) : '';
    book.onchange = () => {
        const selected = books[Number(book.value)];
        if (book.value === '' || !selected) {
            ['textbookVersion', 'textbookStage', 'textbookGrade', 'textbookVolume']
                .forEach(key => { delete state.commonPending[key]; });
        } else {
            Object.assign(state.commonPending, selected);
        }
        renderValueFields($('target-common-fields'), commonKeys(), state.commonPending, updateCommonSummary);
        updateCommonSummary();
        scheduleTargetEditorDraft();
    };
    renderValueFields($('target-common-fields'), commonKeys(), state.commonPending, updateCommonSummary);
    updateCommonSummary();
    enhanceTargetSelect(book); book.closest('.target-select')?.toggleAttribute('hidden', book.hidden); book._targetSelectSync?.();
}
function targetEditorApplyTemplate(template) {
    if (type() === 'vocabulary') {
        showToast('词汇页面录入适配器尚未接入，暂不能应用常用方案。', 'warning'); return false;
    }
    const configuration = template?.configuration || {};
    const multi = units().length > 1;
    // A single entry has no scope decision to make. The scope controls are
    // hidden in that case, but the target editor still needs to accept a
    // selected template and apply it to the only entry.
    const scope = multi ? ($('system-input-app-template-scope')?.value || '') : 'all';
    if (multi && !['all', 'selected'].includes(scope)) {
        showToast('请选择应用范围：勾选条目或全部单元。', 'warning'); return false;
    }
    if (scope === 'all') state.selected = new Set(units().map(unit => String(unit.unit_id)));
    if (!state.selected.size) { showToast('请先在清单中勾选要带入方案的条目。', 'warning'); return false; }
    const inputType = type();
    const values = Object.fromEntries(systemInputBatchFields(inputType).map(key => [key, systemInputBatchValue(configuration, key)]).filter(([, value]) => value !== undefined));
    const fields = Object.keys(values).filter(key => targetEditorBatchFields(inputType, values).includes(key)
        && !['paperName', 'textbookNameZh', 'textbookNameEn', 'textbookUnit', 'textbookLesson'].includes(key));
    // Reflect the chosen scope in the checklist behind the modal. This keeps
    // the preview auditable and avoids a second, invisible selection step.
    refreshSystemInputTargetEditor();
    prepareBatch(values, fields, { context: 'template', templateName: template.name });
    refreshSelection();
    try { rendererStorage?.setItem(`wordtts.target-recent.${type()}`, String(template.app_template_id)); } catch (_) { /* optional preference */ }
    return true;
}
async function targetEditorSaveTemplate() {
    if (type() === 'vocabulary') {
        showToast('词汇页面录入适配器尚未接入，暂不能保存常用方案。', 'warning'); return false;
    }
    if (systemInputTemplateBusy || !workflowApi?.createSystemInputTemplate) return false;
    const source = $('target-template-save-scope')?.value === 'common'
        ? (state.commonPending || state.defaults)
        : systemInputUnitDrafts.get(String(systemInputSelectedUnitId));
    const allowed = systemInputBatchFields(type(), { commonOnly: true });
    const configuration = Object.fromEntries(allowed.filter(key => !systemInputBatchIsEmpty(source?.[key])).map(key => [key, source[key]]));
    if (!Object.keys(configuration).length) { showToast('请先填写要保存的录入字段。', 'warning'); return false; }
    const name = await showPromptDialog('保存常用录入方案', `包含：${Object.keys(configuration).map(key => labels[key]).filter(Boolean).join('、')}。请输入方案名称：`, type() === 'paper' ? '试卷常用设置' : '教材常用设置');
    if (!name?.trim()) return false;
    systemInputTemplateBusy = true;
    const saveButton = $('system-input-save-template-btn'); saveButton.disabled = true;
    const inputType = type();
    try {
        await workflowApi.createSystemInputTemplate(systemInputAppTemplatePayload(inputType, name.trim(), configuration), { idempotencyKey: `target-template-${Date.now()}` });
        await loadSystemInputTemplates(inputType, { force: true });
        showToast('常用录入方案已保存', 'success'); return true;
    } catch (error) { showToast(`保存方案失败：${error.message || '请重试'}`, 'error'); return false; }
    finally { systemInputTemplateBusy = false; saveButton.disabled = false; }
}
function targetEditorValidateCatalog() {
    if (!targetEditorActive() || type() !== 'textbook') return true;
    const pending = configs().find(configuration => { const status = assessment(configuration); return status.pending && !status.missing.length; });
    if (!pending) return true;
    const pendingId = String(pending.unit_id);
    state.selected = new Set([pendingId]);
    $('target-search').value = '';
    $('target-filter').value = 'all';
    $('target-filter')._targetSelectSync?.();
    refreshSystemInputTargetEditor();
    const acknowledge = [...$('target-rows')?.querySelectorAll('tr') || []]
        .find(row => row.querySelector(`[data-unit-id="${CSS.escape(pendingId)}"]`))
        ?.querySelector('[data-target-catalog-ack="true"]');
    acknowledge?.focus?.({ preventScroll: true });
    showToast('请先点击该条目上的“已核对目录”，再保存目标。', 'warning'); return false;
}
function mountSystemInputTargetEditor() {
    const drawer = $('system-input-drawer'); if (!drawer) return;
    // Keep the large dialog outside the content stacking context so its
    // backdrop and top spacing remain consistent across the whole app.
    if (drawer.parentElement !== document.body) document.body.append(drawer);
    drawer.classList.add('is-workspace'); drawer.setAttribute('role', 'dialog'); drawer.setAttribute('aria-modal', 'true');
    if (drawer.querySelector('#target-overview')) return;
    const form = $('system-input-form');
    const panel = drawer.querySelector('.system-input-drawer-panel');
    const optional = $('system-input-optional-fields');
    if (optional && panel) {
        optional.classList.add('target-modal', 'target-template-modal');
        optional.hidden = true;
        optional.setAttribute('aria-hidden', 'true');
        optional.setAttribute('role', 'dialog');
        optional.setAttribute('aria-modal', 'true');
        optional.open = false;
        document.body.append(optional);
    }
    const typeFields = $('system-input-type-fields');
    if (typeFields && panel) panel.insertBefore(typeFields, form);
    // Directory sync is a prerequisite for textbook cascades. Keep its status
    // in the fixed shell so users see it before entering the scrollable list or
    // opening any child dialog.
    const textbookFields = $('system-input-textbook-fields');
    const textbookToolbar = textbookFields?.querySelector('.system-input-textbook-toolbar');
    if (textbookToolbar && panel) {
        const syncCard = el('section', 'target-catalog-sync');
        syncCard.id = 'target-catalog-sync';
        syncCard.hidden = true;
        syncCard.setAttribute('aria-labelledby', 'target-catalog-sync-title');
        const copy = el('div', 'target-catalog-sync-copy');
        copy.append(
            el('h3', '', '教材目录'),
            el('p', '', '同步后可使用下方教材级联选项。'),
        );
        copy.querySelector('h3').id = 'target-catalog-sync-title';
        syncCard.append(copy, textbookToolbar);
        panel.insertBefore(syncCard, form);
    }
    // 同步模版是试卷类型独有的功能。它和教材目录一样作为目录同步的前置条件，
    // 把平台模板同步工具栏放进固定外壳，用户进入清单前即可看到同步状态。
    const platformTemplateField = $('system-input-platform-template-field');
    const platformTemplateToolbar = platformTemplateField?.querySelector('.system-input-platform-template-toolbar');
    if (platformTemplateToolbar && panel) {
        const syncCard = el('section', 'target-catalog-sync');
        syncCard.id = 'target-catalog-sync-template';
        syncCard.hidden = true;
        syncCard.setAttribute('aria-labelledby', 'target-catalog-sync-template-title');
        const copy = el('div', 'target-catalog-sync-copy');
        copy.append(
            el('h3', '', '平台模板目录'),
            el('p', '', '同步后可按省份、学段和年级选择平台题型模板。'),
        );
        copy.querySelector('h3').id = 'target-catalog-sync-template-title';
        syncCard.append(copy, platformTemplateToolbar);
        panel.insertBefore(syncCard, form);
    }
    $('system-input-unit-section').hidden = true;
    const overview = el('section', 'target-overview'); overview.id = 'target-overview';
    overview.innerHTML = '<div class="target-overview-head"><div><h3>录入目标清单</h3><p>所有目标都在这张清单中编辑；选中条目后可统一补齐或替换字段。</p></div><span id="target-overview-count"></span></div><aside class="target-action-bar" aria-label="批量工具"><button type="button" class="target-action" id="target-common-open" aria-label="批次共用设置" title="批次共用设置：一次设置，按需继承" data-tooltip="批次共用设置 · 一次设置，按需继承"><span class="target-action-icon is-common" aria-hidden="true"></span><span class="target-action-copy"><strong>批次共用设置</strong><small>一次设置，按需继承</small></span></button><button type="button" class="target-action" id="target-template-open" aria-label="常用录入方案" title="常用录入方案：先预览，再应用" data-tooltip="常用录入方案 · 先预览，再应用"><span class="target-action-icon is-template" aria-hidden="true"></span><span class="target-action-copy"><strong>常用录入方案</strong><small>先预览，再应用</small></span></button><button type="button" class="target-action" id="target-batch-open" aria-label="批量修改" title="批量修改：先勾选条目" data-tooltip="批量修改 · 先勾选条目"><span class="target-action-icon is-batch" aria-hidden="true"></span><span class="target-action-copy"><strong>批量修改</strong><small id="target-selection-note">先勾选条目</small></span></button></aside><div class="target-toolbar"><label class="target-search"><span class="sr-only">搜索文档内容或目标名称</span><input type="search" id="target-search" placeholder="搜索文档内容或目标名称"></label><label><span class="sr-only">筛选目标</span><select id="target-filter" aria-label="筛选目标"><option value="all">全部条目</option><option value="pending">只看待处理</option></select></label><button type="button" class="btn-text btn-sm" id="target-next-issue">处理下一个问题</button></div><div class="target-selection"><button type="button" class="btn-text btn-sm" id="target-select-visible">选择筛选结果</button><button type="button" class="btn-text btn-sm" id="target-clear-selection">清除选择</button><span>勾选目标后，可用统一工具编辑更多字段</span></div><table class="target-table"><thead><tr id="target-columns"></tr></thead><tbody id="target-rows"></tbody></table><p id="target-empty" hidden>没有符合条件的条目，请调整搜索或筛选。</p><datalist id="target-platform-names"></datalist>';
    overview.setAttribute('aria-labelledby', 'target-overview-title');
    overview.querySelector('.target-overview-head h3')?.setAttribute('id', 'target-overview-title');
    const overviewMain = el('div', 'target-overview-main');
    ['.target-toolbar', '.target-selection', '.target-table', '#target-empty', '#target-platform-names']
        .map(selector => overview.querySelector(selector))
        .filter(Boolean)
        .forEach(node => overviewMain.append(node));
    const actionBar = overview.querySelector('.target-action-bar');
    if (actionBar) overview.append(actionBar);
    overview.append(overviewMain);
    const common = el('section', 'target-modal target-common'); common.id = 'target-common'; common.hidden = true; common.setAttribute('aria-hidden', 'true');
    common.innerHTML = '<div class="target-modal-card" role="dialog" aria-modal="true" aria-labelledby="target-common-title"><header class="target-modal-heading"><div><h3 id="target-common-title">批次共用设置</h3><p>只设置需要复用的字段；已有单独修改的条目仍保留自己的值。</p><span id="target-common-summary" class="target-modal-summary"></span></div><button type="button" class="btn-icon target-modal-close" id="target-common-close" aria-label="关闭批次共用设置"><span class="target-close-icon" aria-hidden="true"></span></button></header><div class="target-modal-body"><select id="target-book" aria-label="选择教材"></select><div id="target-common-fields" class="target-field-grid"></div></div><footer class="target-modal-actions"><span class="target-modal-help">应用后可在清单中继续调整单独条目</span><button type="button" class="btn-secondary btn-sm" id="target-common-apply">更新批次默认值</button></footer></div>';
    Object.defineProperty(common, 'open', { configurable: true, get: () => !common.hidden, set: value => value ? openTargetModal(common) : closeTargetModal(common) });
    const batch = el('section', 'target-modal target-batch'); batch.id = 'target-batch'; batch.hidden = true; batch.setAttribute('aria-hidden', 'true');
    batch.innerHTML = '<div class="target-modal-card" role="dialog" aria-modal="true" aria-labelledby="target-batch-title"><header class="target-modal-heading"><div><h3 id="target-batch-title">批量修改</h3><p>试卷分类由文档自动识别；先勾选需要批量写入的字段，再选择要写入的值。</p><div id="target-batch-classification" class="target-detail-classification" aria-live="polite" hidden><span class="target-detail-classification-label">试卷分类</span><span id="target-batch-category-tag" class="target-detail-category-tag"></span><small id="target-batch-category-note"></small></div></div><button type="button" class="btn-icon target-modal-close" id="target-batch-close" aria-label="关闭批量修改"><span class="target-close-icon" aria-hidden="true"></span></button></header><div class="target-modal-body"><div id="target-batch-fields" class="target-field-grid"></div><p id="target-batch-preview" class="target-modal-preview" role="status"></p></div><footer class="target-modal-actions"><label class="target-mode-label"><span>修改方式</span><select id="target-batch-mode" aria-label="修改方式"><option value="fill-empty">只补空值</option><option value="overwrite">替换已有值</option></select></label><button type="button" class="btn-secondary btn-sm" id="target-batch-apply">应用本次修改</button></footer></div>';
    const detail = el('section', 'target-modal target-detail'); detail.id = 'target-detail'; detail.hidden = true; detail.setAttribute('aria-hidden', 'true');
    detail.innerHTML = '<div class="target-modal-card" role="dialog" aria-modal="true" aria-labelledby="target-detail-title"><header class="target-modal-heading"><div><h3 id="target-detail-title">编辑详情</h3><p id="target-detail-description">在这里编辑该目标的完整录入字段；修改会直接暂存，不会影响其他目标。</p><div id="target-detail-classification" class="target-detail-classification" aria-live="polite" hidden><span class="target-detail-classification-label">试卷分类</span><span id="target-detail-category-tag" class="target-detail-category-tag"></span><small id="target-detail-category-note"></small></div><span id="target-detail-summary" class="target-modal-summary" role="status"></span></div><button type="button" class="btn-icon target-modal-close" id="target-detail-close" aria-label="关闭编辑详情"><span class="target-close-icon" aria-hidden="true"></span></button></header><div class="target-modal-body"><div id="target-detail-fields" class="target-field-grid"></div></div><footer class="target-modal-actions"><span class="target-modal-help">只修改当前目标，其他目标不会被带入。</span><button type="button" class="btn-secondary btn-sm" id="target-detail-done">完成编辑</button></footer></div>';
    // Keep the existing form controls as a hidden canonical source for
    // collection and validation. Per-target edits use the same detail dialog
    // for one or many targets; the batch tool remains an explicit separate
    // action and never appears merely because a target needs one missing field.
    const formSource = el('div', 'target-editor-form-source'); formSource.id = 'target-editor-form-source'; formSource.hidden = true; formSource.setAttribute('aria-hidden', 'true');
    ['system-input-textbook-fields', 'system-input-paper-fields', 'system-input-paper-type-section', 'system-input-range-fields']
        .forEach(id => formSource.append($(id)));
    enhanceTargetNumberControl($('system-input-year'), { min: 2000, max: 2100, label: '年份' });
    enhanceTargetNumberControl($('system-input-answer-time'), { min: 1, max: 60, label: '答题时间' });
    const source = el('section', 'target-modal target-source-preview'); source.id = 'target-source-preview'; source.hidden = true; source.setAttribute('aria-hidden', 'true');
    source.innerHTML = '<div class="target-modal-card" role="dialog" aria-modal="true" aria-labelledby="target-source-title" aria-describedby="target-source-description"><header class="target-modal-heading"><div><h3 id="target-source-title">文档内容</h3><p id="target-source-description">只读原文片段，用于确认条目边界和名称。</p></div><button type="button" class="btn-icon target-modal-close" id="target-source-close" aria-label="关闭原文预览"><span class="target-close-icon" aria-hidden="true"></span></button></header><div class="target-modal-body"><p id="target-source-text"></p></div></div>';
    form.insertBefore(overview, $('system-input-validation-summary'));
    document.body.append(common, batch, detail, source, formSource);
    const boundary = $('system-input-boundary-review');
    if (boundary) {
        boundary.classList.add('target-modal', 'target-boundary-modal');
        boundary.setAttribute('aria-hidden', 'true');
        boundary.setAttribute('role', 'dialog');
        boundary.setAttribute('aria-modal', 'true');
        document.body.append(boundary);
    }
    const actions = drawer.querySelector('.system-input-drawer-actions');
    const status = el('span', 'target-draft-status', '修改会暂存为本机草稿'); status.id = 'target-draft-status'; status.setAttribute('role', 'status'); actions.prepend(status);
    const undo = button('撤销批量修改', () => {
        if (!state?.undo || !targetEditorCanSave()) return;
        const undoState = state.undo;
        const result = systemInputBatchUndo(configs(), undoState.changes);
        const restored = Array.isArray(result) ? result : result.configurations;
        restored.forEach(configuration => systemInputUnitDrafts.set(String(configuration.unit_id), configuration));
        state.defaults = undoState.metadata.defaults;
        // Rebuild override metadata from the values that survived the undo.
        // A hand edit made after the batch operation must remain an explicit
        // exception even when the batch change itself was skipped.
        state.overrides = {};
        restored.forEach(configuration => {
            const id = String(configuration.unit_id); const fields = commonKeys().filter(key => !equal(targetFieldValue(configuration, key), targetFieldValue(state.defaults, key)));
            if (fields.length) state.overrides[id] = fields;
        });
        state.commonPending = copy(state.defaults);
        state.undo = null; state.changed.clear(); populateSystemInputUnitForm(systemInputInteractionWorkspace()?.system_input);
        state.observed = new Map(configs().map(configuration => [String(configuration.unit_id), copy(configuration)]));
        renderCommon(); refreshSystemInputTargetEditor(); scheduleTargetEditorDraft(); $('target-operation-status').textContent = '已撤销本次批量修改；后续单独编辑的字段会保留。';
    }, 'btn-text btn-sm'); undo.id = 'target-undo'; actions.insertBefore(undo, $('system-input-save-btn'));
    const operation = el('p', 'target-operation-status'); operation.id = 'target-operation-status'; operation.setAttribute('role', 'status'); form.append(operation);
    const discard = button('放弃草稿', async () => {
        if (!await showConfirmDialog({ title: '放弃未保存的目标修改？', message: '恢复上次正式保存的录入目标。', confirmLabel: '放弃草稿' })) return;
        clearSystemInputStoredDraft(state.workspace); clearSystemInputDrawerDraft(); state.defaults = {}; state.overrides = {}; state.acknowledgements = {}; state.undo = null;
        systemInputPopulateForm(systemInputInteractionWorkspace()?.system_input);
        closeSystemInputConfigDrawer({ preserveDraft: false });
    }, 'btn-text btn-sm'); discard.id = 'target-discard'; actions.insertBefore(discard, $('system-input-drawer-cancel'));
    const saveTemplate = $('system-input-save-template-btn');
    if (saveTemplate) {
        saveTemplate.textContent = '保存为常用方案';
        saveTemplate.title = '保存可复用字段；名称、文档内容和教材单元、课时不会带入方案';
    }
    const templateHeadingCopy = optional?.querySelector('.system-input-section-heading-copy');
    if (templateHeadingCopy) {
        const heading = templateHeadingCopy.querySelector('h3');
        const description = templateHeadingCopy.querySelector('p');
        if (heading) heading.textContent = '常用录入方案';
        if (description) description.textContent = '选择已保存方案，先查看将写入哪些字段和条目，再确认应用。';
    }
    const templateFieldLabel = optional?.querySelector('label[for="system-input-app-template"]');
    if (templateFieldLabel) {
        const labelText = templateFieldLabel.firstChild;
        if (labelText) labelText.textContent = '选择已保存方案 ';
        const input = $('system-input-app-template');
        if (input) input.placeholder = '搜索方案名称';
    }
    const templateSave = el('section', 'target-template-save');
    const templateSaveHeading = el('div', 'target-template-save-heading');
    templateSaveHeading.append(
        el('strong', '', '保存为新方案'),
        el('small', '', '把当前表单中的可复用字段保存下来，下次选择方案后再决定应用范围。'),
    );
    const saveScope = el('select'); saveScope.id = 'target-template-save-scope'; saveScope.setAttribute('aria-label', '保存内容');
    saveScope.append(new Option('批次共用字段', 'common'), new Option('当前表单字段', 'current'));
    const saveScopeField = el('label', 'target-template-save-field');
    saveScopeField.append(el('span', '', '保存内容'), saveScope);
    const saveControls = el('div', 'target-template-save-controls');
    saveControls.append(saveScopeField, saveTemplate);
    templateSave.append(templateSaveHeading, saveControls);
    optional.querySelector('.system-input-section-body').append(templateSave);
    const templateHint = optional?.querySelector('.system-input-featured-hint');
    if (templateHint) templateHint.textContent = '方案只保存录入字段；名称、文档内容、教材单元和课时不会带入。';
    enhanceTargetSelect($('target-filter'));
    enhanceTargetSelect(saveScope);
    const templateSummary = optional?.querySelector('summary');
    if (templateSummary) {
    const templateClose = button('关闭', () => closeTargetModal(optional), 'btn-text btn-sm'); templateClose.id = 'target-template-close'; templateClose.addEventListener('click', event => event.stopPropagation()); templateSummary.append(templateClose);
    }
    bindSystemInputDisclosures?.(formSource);
    bindSystemInputChoiceGroups?.(formSource);
    if (optional) {
        bindSystemInputDisclosures?.(optional);
        // The template dialog is moved out of the drawer before the
        // surface-level binding runs. Bind its custom scope cards here so
        // clicking “勾选条目/全部单元” updates both the hidden value and
        // the visible selected state.
        bindSystemInputChoiceGroups?.(optional);
        optional.addEventListener('toggle', () => {
            // The legacy template panel is a <details> element retained for
            // its existing fields. Treat its native collapse gesture as a
            // dialog close so the modal lock and focus are always released.
            if (!optional.open && !optional.hidden && activeTargetModal === optional) closeTargetModal(optional);
        });
    }
    $('target-search').addEventListener('input', refreshSystemInputTargetEditor); $('target-filter').addEventListener('change', refreshSystemInputTargetEditor);
    $('target-common-open').addEventListener('click', event => { state.lastModalTrigger = event.currentTarget; openTargetModal(common); renderCommon(); });
    $('target-template-open').addEventListener('click', event => { state.lastModalTrigger = event.currentTarget; openTargetModal(optional); optional.open = true; });
    $('target-batch-open').addEventListener('click', () => prepareBatch());
    $('target-common-close').addEventListener('click', () => closeTargetModal(common));
    $('target-batch-close').addEventListener('click', () => closeTargetModal(batch));
    $('target-detail-close').addEventListener('click', () => closeTargetDetail());
    $('target-detail-done').addEventListener('click', () => closeTargetDetail());
    $('target-batch-mode').addEventListener('change', previewBatch);
    enhanceTargetSelect($('target-batch-mode'));
    $('target-batch-apply').addEventListener('click', () => { if (!targetEditorCanSave()) return; previewBatch(); const result = state.batch.result; updateConfigurations(result); state.batch = null; closeTargetModal(batch); });
    $('target-common-apply').addEventListener('click', applyCommon);
    $('target-source-close').addEventListener('click', () => closeTargetModal(source));
    $('target-clear-selection').addEventListener('click', () => { state.selected.clear(); refreshSystemInputTargetEditor(); });
    $('target-select-visible').addEventListener('click', () => { $('target-rows').querySelectorAll('input[type="checkbox"]').forEach(check => { if (!check.checked) check.click(); }); });
    $('target-next-issue').addEventListener('click', () => {
        if (type() === 'vocabulary') return;
        const rows = configs(); const start = rows.findIndex(item => String(item.unit_id) === String(systemInputSelectedUnitId));
        const next = [...rows.slice(start + 1), ...rows.slice(0, start + 1)].map(configuration => ({ configuration, status: assessment(configuration) }))
            .find(({ status }) => !status.unavailable && (status.missing.length || status.pending));
        if (!next) return;
        const fields = targetEditorMissingFieldKeys(next.configuration);
        if (fields.length) {
            openTargetEditForUnit(String(next.configuration.unit_id), fields);
            return;
        }
        state.selected = new Set([String(next.configuration.unit_id)]);
        refreshSystemInputTargetEditor();
        const nextId = String(next.configuration.unit_id);
        [...$('target-rows')?.querySelectorAll('tr') || []]
            .find(row => row.querySelector(`[data-unit-id="${CSS.escape(nextId)}"]`))
            ?.querySelector('[data-target-catalog-ack="true"]')
            ?.focus?.({ preventScroll: true });
    });
    drawer.addEventListener('input', event => { if (event.target.id?.startsWith('system-input-')) scheduleTargetEditorDraft(); });
    drawer.addEventListener('change', event => { if (event.target.id?.startsWith('system-input-')) scheduleTargetEditorDraft(); });
    const syncDetachedField = event => {
        const target = event.target;
        if (!target?.closest?.('.target-modal') || !target.id?.startsWith('system-input-')) return;
        if (typeof clearSystemInputValidationError === 'function') clearSystemInputValidationError(target.id);
        if (typeof syncSystemInputUnitDraftFromForm === 'function') syncSystemInputUnitDraftFromForm(event);
        scheduleTargetEditorDraft();
    };
    document.addEventListener('input', syncDetachedField);
    document.addEventListener('change', syncDetachedField);
    drawer.addEventListener('focusout', event => { if (event.target.classList?.contains('target-inline')) requestAnimationFrame(refreshSystemInputTargetEditor); });
    drawer.addEventListener('keydown', event => { if (event.key === 'Escape' && activeTargetModal) { event.preventDefault(); event.stopImmediatePropagation(); closeTargetEditorSurface(); } });
    document.addEventListener('keydown', event => {
        if (!targetEditorActive()) return;
        if (activeTargetModal) {
            trapTargetModalFocus(event);
            if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); closeTargetEditorSurface(); }
            return;
        }
        if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); closeSystemInputConfigDrawer(); }
    });
    [common, batch, detail, source, optional].filter(Boolean).forEach(node => node.addEventListener('click', event => { if (event.target === node) closeTargetEditorSurface(node); }));
    document.querySelector('#sidebar')?.addEventListener('click', event => { if (targetEditorActive() && event.target.closest('button')) closeSystemInputConfigDrawer(); }, true);
    window.addEventListener('beforeunload', targetEditorPersist);
}
function openSystemInputTargetEditor(workspace, draft = null) {
    mountSystemInputTargetEditor();
    const metadata = draft?.editorState || {};
    const rows = configs(); const common = systemInputCommonConfiguration(rows, type());
    // Only an unapplied batch preview owns row selection across a close/open
    // cycle. Ordinary checked rows are intentionally ephemeral.
    const restoreBatchSelection = Boolean(metadata.batch?.pending);
    const initialSelected = restoreBatchSelection
        ? (metadata.selected || []).filter(id => rows.some(row => String(row.unit_id) === id))
        : rows.length === 1 && type() !== 'vocabulary' ? [String(rows[0].unit_id)] : [];
    state = { workspace, inputType: type(), identity: systemInputDraftStorageKey(workspace), defaults: metadata.defaults || Object.fromEntries(commonKeys().filter(key => common[key] !== undefined).map(key => [key, common[key]])), commonPending: metadata.commonPending, overrides: metadata.overrides || {}, acknowledgements: metadata.acknowledgements || {}, selected: new Set(initialSelected), changed: new Set(), undo: null, observed: new Map(rows.map(configuration => [String(configuration.unit_id), copy(configuration)])), dirty: Boolean(draft && !draft.committed) };
    closeTargetModal($('target-common'), { restoreFocus: false });
    closeTargetModal($('target-batch'), { restoreFocus: false });
    closeTargetModal($('target-detail'), { restoreFocus: false });
    closeTargetModal($('target-source-preview'), { restoreFocus: false });
    if ($('target-editor-form-source')) $('target-editor-form-source').hidden = true;
    if ($('system-input-optional-fields')) {
        $('system-input-optional-fields').hidden = true;
        $('system-input-optional-fields').setAttribute('aria-hidden', 'true');
        $('system-input-optional-fields').open = false;
    }
    const isVocabulary = type() === 'vocabulary';
    const drawer = $('system-input-drawer');
    const panel = drawer?.querySelector('.system-input-drawer-panel');
    if (panel) {
        panel.hidden = false;
        panel.setAttribute('aria-hidden', 'false');
    }
    drawer?.setAttribute('aria-hidden', 'false');
    $('target-batch').hidden = true; $('target-detail').hidden = true; $('target-source-preview').hidden = true;
    $('target-search').value = ''; $('target-filter').value = 'all'; $('target-operation-status').textContent = '';
    $('target-filter')._targetSelectSync?.();
    updateSystemInputAppTemplateAction?.(workspace?.system_input, { resetScope: true });
    $('system-input-unit-section').hidden = true;
    renderCommon(); refreshSystemInputTargetEditor();
    if (metadata.commonOpen) openTargetModal($('target-common'));
    if (metadata.batch) prepareBatch(metadata.batch.values, metadata.batch.fields, {
        open: Boolean(metadata.batchOpen),
        mode: metadata.batch.mode,
        context: metadata.batch.context,
        templateName: metadata.batch.templateName,
    });
    $('target-template-save-scope').value = rows.length > 1 ? 'common' : 'current';
    $('target-template-save-scope')._targetSelectSync?.();
    setDraftStatus(state.dirty ? '已恢复本机草稿 · 尚未保存目标' : '修改会自动暂存本机 · 保存后才更新目标');
    const form = $('system-input-form'); form.scrollTop = 0;
    document.querySelectorAll('#app, #sidebar, #toolbar, #content > .step-page.active, #content > .workspace-action-dock').forEach(node => { if (!node.inert) { node.inert = true; node.dataset.targetEditorInert = 'true'; } });
    $('system-input-drawer-title')?.focus({ preventScroll: true });
    targetEditorSyncBoundaryReview();
}

root.systemInputTargetFocusField = targetEditorFocusField;
registerRendererModule('systemInput.targetEditor', {
    mountSystemInputTargetEditor, openSystemInputTargetEditor, refreshSystemInputTargetEditor,
    targetEditorRefreshAfterDataLoad,
    targetEditorActive, targetEditorState, targetEditorCanSave, targetEditorPersist, scheduleTargetEditorDraft,
    targetEditorClosed, targetEditorRecordManual, targetEditorApplyTemplate, targetEditorValidateCatalog,
    targetEditorSetBusy, targetEditorSaveTemplate, targetEditorCloseActiveModal, targetEditorPrepareSave,
    targetEditorSyncBoundaryReview, targetEditorFocusField,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
