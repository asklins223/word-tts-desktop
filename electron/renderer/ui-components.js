/**
 * 小猪wordTTS UI components
 * =====================
 * Reusable select-only combobox and in-app dialog primitives for the renderer.
 * Native <select> elements remain the business-state source of truth, while the
 * generated controls own all visible interaction.
 */

(() => {
    'use strict';

    const selectRegistry = new WeakMap();
    let openSelect = null;
    let selectSerial = 0;

    function clamp(value, min, max) {
        return Math.min(Math.max(value, min), max);
    }

    function textLabelFor(select) {
        const explicit = select.getAttribute('aria-label');
        if (explicit) return explicit.trim();
        if (select.id) {
            const label = document.querySelector(`label[for="${CSS.escape(select.id)}"]`);
            if (label?.textContent?.trim()) return label.textContent.trim();
        }
        const visibleLabel = select.closest('.config-field')?.querySelector('.field-label');
        return visibleLabel?.childNodes?.[0]?.textContent?.trim() || select.title || '选择选项';
    }

    function closeOpenSelect(options) {
        if (!openSelect) return;
        openSelect.close(options);
    }

    class AppSelect {
        constructor(select) {
            this.select = select;
            this.id = `app-select-${select.id || ++selectSerial}`;
            this.label = textLabelFor(select);
            this.isOpen = false;
            this.activeIndex = -1;
            this.typeahead = '';
            this.typeaheadTimer = null;

            this.root = document.createElement('div');
            this.root.className = 'app-select';
            this.root.dataset.sourceId = select.id || '';

            this.trigger = document.createElement('button');
            this.trigger.type = 'button';
            this.trigger.className = 'app-select-trigger';
            this.trigger.id = `${this.id}-trigger`;
            this.trigger.setAttribute('role', 'combobox');
            this.trigger.setAttribute('aria-haspopup', 'listbox');
            this.trigger.setAttribute('aria-expanded', 'false');
            this.trigger.setAttribute('aria-controls', `${this.id}-listbox`);
            this.trigger.setAttribute('aria-label', this.label);

            this.value = document.createElement('span');
            this.value.className = 'app-select-value';
            this.chevron = document.createElement('span');
            this.chevron.className = 'app-select-chevron';
            this.chevron.setAttribute('aria-hidden', 'true');
            this.trigger.append(this.value, this.chevron);

            this.popover = document.createElement('div');
            this.popover.className = 'app-select-popover';
            this.popover.hidden = true;
            this.popover.dataset.owner = select.id || this.id;

            this.listbox = document.createElement('div');
            this.listbox.className = 'app-select-listbox';
            this.listbox.id = `${this.id}-listbox`;
            this.listbox.setAttribute('role', 'listbox');
            this.listbox.setAttribute('aria-label', this.label);
            this.popover.appendChild(this.listbox);

            this.root.appendChild(this.trigger);
            select.insertAdjacentElement('afterend', this.root);
            document.body.appendChild(this.popover);

            // Progressive enhancement: only hide the source after the custom UI exists.
            select.classList.add('app-select-source');
            select.tabIndex = -1;
            select.setAttribute('aria-hidden', 'true');

            this.trigger.addEventListener('click', () => this.toggle());
            this.trigger.addEventListener('keydown', (event) => this.onTriggerKeydown(event));
            this.select.addEventListener('change', () => this.refresh());

            this.observer = new MutationObserver(() => this.refresh());
            this.observer.observe(select, {
                attributes: true,
                attributeFilter: ['disabled', 'label', 'selected', 'title', 'value'],
                childList: true,
                subtree: true,
            });

            this.refresh();
        }

        optionRecords() {
            return Array.from(this.select.options).map((option, index) => ({
                index,
                value: option.value,
                label: option.textContent?.trim() || option.label || option.value,
                disabled: option.disabled,
                selected: index === this.select.selectedIndex,
            }));
        }

        refresh() {
            const records = this.optionRecords();
            const selected = records.find(record => record.selected) || records[0] || null;
            const disabled = this.select.disabled || records.length === 0;

            this.value.textContent = selected?.label || '暂无可选项';
            this.trigger.title = this.select.title || selected?.label || '';
            this.trigger.setAttribute('aria-label', `${this.label}：${selected?.label || '暂无可选项'}`);
            this.trigger.disabled = disabled;
            this.trigger.setAttribute('aria-disabled', disabled ? 'true' : 'false');
            this.root.classList.toggle('is-disabled', disabled);
            this.root.classList.toggle('is-placeholder', !selected || selected.value === '');
            this.renderOptions(records);

            if (disabled && this.isOpen) this.close();
            if (this.isOpen) {
                const preferred = selected && !selected.disabled
                    ? selected.index
                    : records.find(record => !record.disabled)?.index ?? -1;
                this.setActiveIndex(preferred, false);
                this.placePopover();
            }
        }

        renderOptions(records) {
            const fragment = document.createDocumentFragment();
            records.forEach(record => {
                const option = document.createElement('button');
                option.type = 'button';
                option.className = 'app-select-option';
                option.id = `${this.id}-option-${record.index}`;
                option.dataset.index = String(record.index);
                option.setAttribute('role', 'option');
                option.setAttribute('aria-selected', record.selected ? 'true' : 'false');
                option.disabled = record.disabled;
                option.tabIndex = -1;
                option.classList.toggle('is-selected', record.selected);

                const label = document.createElement('span');
                label.className = 'app-select-option-label';
                label.textContent = record.label;
                const mark = document.createElement('span');
                mark.className = 'app-select-option-mark';
                mark.textContent = '✓';
                mark.setAttribute('aria-hidden', 'true');
                option.append(label, mark);

                option.addEventListener('pointermove', () => {
                    if (!record.disabled) this.setActiveIndex(record.index, false);
                });
                option.addEventListener('click', () => this.choose(record.index));
                fragment.appendChild(option);
            });
            this.listbox.replaceChildren(fragment);
        }

        toggle() {
            if (this.isOpen) this.close({ restoreFocus: true });
            else this.open();
        }

        open() {
            if (this.trigger.disabled || this.isOpen) return;
            if (openSelect && openSelect !== this) openSelect.close();
            openSelect = this;
            this.isOpen = true;
            this.root.classList.add('is-open');
            this.trigger.setAttribute('aria-expanded', 'true');
            this.popover.hidden = false;
            this.popover.classList.add('is-open');

            const records = this.optionRecords();
            const preferred = records.find(record => record.selected && !record.disabled)?.index
                ?? records.find(record => !record.disabled)?.index
                ?? -1;
            this.setActiveIndex(preferred, false);
            this.placePopover();
            requestAnimationFrame(() => this.scrollActiveIntoView());
        }

        close({ restoreFocus = false } = {}) {
            if (!this.isOpen) return;
            this.isOpen = false;
            this.root.classList.remove('is-open');
            this.trigger.setAttribute('aria-expanded', 'false');
            this.trigger.removeAttribute('aria-activedescendant');
            this.popover.classList.remove('is-open', 'opens-upward');
            this.popover.hidden = true;
            this.activeIndex = -1;
            if (openSelect === this) openSelect = null;
            if (restoreFocus) this.trigger.focus({ preventScroll: true });
        }

        placePopover() {
            if (!this.isOpen) return;
            const rect = this.trigger.getBoundingClientRect();
            if (rect.width < 1 || rect.height < 1) {
                this.close();
                return;
            }

            const viewportPadding = 12;
            const gap = 7;
            const width = Math.min(Math.max(rect.width, 180), window.innerWidth - viewportPadding * 2);
            const left = clamp(rect.left, viewportPadding, window.innerWidth - width - viewportPadding);
            const below = window.innerHeight - rect.bottom - viewportPadding - gap;
            const above = rect.top - viewportPadding - gap;
            const opensUpward = below < 190 && above > below;
            const available = Math.max(104, opensUpward ? above : below);

            this.popover.classList.toggle('opens-upward', opensUpward);
            this.popover.style.width = `${width}px`;
            this.popover.style.left = `${left}px`;
            this.listbox.style.maxHeight = `${Math.min(292, available)}px`;
            this.popover.style.visibility = 'hidden';

            const height = this.popover.getBoundingClientRect().height;
            const top = opensUpward
                ? Math.max(viewportPadding, rect.top - gap - height)
                : Math.min(rect.bottom + gap, window.innerHeight - viewportPadding - height);
            this.popover.style.top = `${top}px`;
            this.popover.style.visibility = 'visible';
        }

        setActiveIndex(index, shouldScroll = true) {
            const options = Array.from(this.listbox.querySelectorAll('.app-select-option'));
            const target = options.find(option => Number(option.dataset.index) === index && !option.disabled);
            options.forEach(option => option.classList.toggle('is-active', option === target));
            this.activeIndex = target ? index : -1;
            if (target) this.trigger.setAttribute('aria-activedescendant', target.id);
            else this.trigger.removeAttribute('aria-activedescendant');
            if (target && shouldScroll) target.scrollIntoView({ block: 'nearest' });
        }

        scrollActiveIntoView() {
            this.listbox.querySelector('.app-select-option.is-active')?.scrollIntoView({ block: 'nearest' });
        }

        moveActive(delta) {
            const records = this.optionRecords();
            if (!records.length) return;
            let index = this.activeIndex;
            for (let step = 0; step < records.length; step++) {
                index = clamp(index + delta, 0, records.length - 1);
                if (!records[index]?.disabled) {
                    this.setActiveIndex(index);
                    return;
                }
                if (index === 0 || index === records.length - 1) return;
            }
        }

        moveToBoundary(position) {
            const records = this.optionRecords();
            const candidates = position === 'start' ? records : [...records].reverse();
            const match = candidates.find(record => !record.disabled);
            if (match) this.setActiveIndex(match.index);
        }

        choose(index) {
            const option = this.select.options[index];
            if (!option || option.disabled || this.select.disabled) return;
            const changed = this.select.selectedIndex !== index;
            this.select.selectedIndex = index;
            if (changed) {
                this.select.dispatchEvent(new Event('input', { bubbles: true }));
                this.select.dispatchEvent(new Event('change', { bubbles: true }));
            }
            this.refresh();
            this.close({ restoreFocus: true });
        }

        handleTypeahead(character) {
            clearTimeout(this.typeaheadTimer);
            this.typeahead += character.toLocaleLowerCase('zh-CN');
            this.typeaheadTimer = setTimeout(() => { this.typeahead = ''; }, 700);

            const records = this.optionRecords();
            const start = Math.max(this.activeIndex, this.select.selectedIndex, -1);
            const ordered = [...records.slice(start + 1), ...records.slice(0, start + 1)];
            const match = ordered.find(record => !record.disabled && record.label.toLocaleLowerCase('zh-CN').startsWith(this.typeahead));
            if (!match) return;
            if (!this.isOpen) this.open();
            this.setActiveIndex(match.index);
        }

        onTriggerKeydown(event) {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                if (!this.isOpen) this.open();
                else this.moveActive(event.key === 'ArrowDown' ? 1 : -1);
                return;
            }
            if (event.key === 'Home' || event.key === 'End') {
                event.preventDefault();
                if (!this.isOpen) this.open();
                this.moveToBoundary(event.key === 'Home' ? 'start' : 'end');
                return;
            }
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                if (this.isOpen && this.activeIndex >= 0) this.choose(this.activeIndex);
                else this.open();
                return;
            }
            if (event.key === 'Escape' && this.isOpen) {
                event.preventDefault();
                this.close({ restoreFocus: true });
                return;
            }
            if (event.key === 'Tab') {
                this.close();
                return;
            }
            if (!event.ctrlKey && !event.metaKey && !event.altKey && event.key.length === 1 && event.key.trim()) {
                this.handleTypeahead(event.key);
            }
        }
    }

    function enhanceSelects(root = document) {
        const selects = root.matches?.('select')
            ? [root]
            : Array.from(root.querySelectorAll?.('select:not([data-native-control])') || []);
        selects.forEach(select => {
            if (selectRegistry.has(select)) return;
            const component = new AppSelect(select);
            selectRegistry.set(select, component);
        });
    }

    function syncSelect(selectOrId) {
        const select = typeof selectOrId === 'string' ? document.getElementById(selectOrId) : selectOrId;
        if (!select) return;
        if (!selectRegistry.has(select)) enhanceSelects(select);
        selectRegistry.get(select)?.refresh();
    }

    document.addEventListener('pointerdown', (event) => {
        if (!openSelect) return;
        if (openSelect.root.contains(event.target) || openSelect.popover.contains(event.target)) return;
        openSelect.close();
    });

    document.addEventListener('scroll', (event) => {
        if (!openSelect || openSelect.popover.contains(event.target)) return;
        openSelect.close();
    }, true);

    window.addEventListener('resize', () => closeOpenSelect());

    class AppDialogService {
        constructor(dialog) {
            this.dialog = dialog;
            this.kicker = dialog.querySelector('#app-dialog-kicker');
            this.title = dialog.querySelector('#app-dialog-title');
            this.message = dialog.querySelector('#app-dialog-message');
            this.detail = dialog.querySelector('#app-dialog-detail');
            this.icon = dialog.querySelector('#app-dialog-icon-symbol');
            this.field = dialog.querySelector('#app-dialog-field');
            this.inputLabel = dialog.querySelector('#app-dialog-input-label');
            this.input = dialog.querySelector('#app-dialog-input');
            this.cancelButton = dialog.querySelector('#app-dialog-cancel');
            this.confirmButton = dialog.querySelector('#app-dialog-confirm');
            this.queue = [];
            this.active = null;

            this.cancelButton.addEventListener('click', () => this.finish(this.cancelValue()));
            this.confirmButton.addEventListener('click', () => this.finish(this.confirmValue()));
            this.dialog.addEventListener('cancel', (event) => {
                event.preventDefault();
                this.finish(this.cancelValue());
            });
            this.dialog.addEventListener('keydown', (event) => this.onKeydown(event));
        }

        confirm(options = {}) {
            return this.enqueue('confirm', options);
        }

        prompt(options = {}) {
            return this.enqueue('prompt', options);
        }

        alert(options = {}) {
            return this.enqueue('alert', options);
        }

        enqueue(mode, options) {
            return new Promise(resolve => {
                this.queue.push({ mode, options, resolve });
                this.showNext();
            });
        }

        showNext() {
            if (this.active || this.queue.length === 0) return;
            const request = this.queue.shift();
            const options = request.options || {};
            const tone = ['info', 'success', 'warning', 'danger'].includes(options.tone) ? options.tone : 'info';
            request.previousFocus = document.activeElement;
            this.active = request;
            closeOpenSelect();

            this.dialog.dataset.tone = tone;
            this.dialog.setAttribute('role', tone === 'danger' ? 'alertdialog' : 'dialog');
            this.kicker.textContent = options.kicker || (request.mode === 'prompt' ? '保存到本机' : request.mode === 'alert' ? '应用消息' : '需要确认');
            this.title.textContent = options.title || (request.mode === 'prompt' ? '请输入内容' : '确认操作');
            this.message.textContent = options.message || '';
            this.detail.textContent = options.detail || '';
            this.detail.hidden = !options.detail;
            this.icon.textContent = tone === 'danger' || tone === 'warning' ? '!' : tone === 'success' ? '✓' : 'i';
            this.field.hidden = request.mode !== 'prompt';
            this.inputLabel.textContent = options.inputLabel || '名称';
            this.input.value = options.defaultValue || '';
            this.input.placeholder = options.placeholder || '';
            this.cancelButton.hidden = request.mode === 'alert';
            this.cancelButton.textContent = options.cancelLabel || '取消';
            this.confirmButton.textContent = options.confirmLabel || (request.mode === 'prompt' ? '保存' : request.mode === 'alert' ? '知道了' : '继续');
            this.confirmButton.classList.toggle('is-danger', tone === 'danger');
            this.dialog.setAttribute('aria-describedby', options.detail
                ? 'app-dialog-message app-dialog-detail'
                : 'app-dialog-message');

            this.dialog.showModal();
            requestAnimationFrame(() => {
                if (request.mode === 'prompt') {
                    this.input.focus();
                    this.input.select();
                } else if (request.mode === 'alert') {
                    this.confirmButton.focus();
                } else if (tone === 'danger' || tone === 'warning') {
                    this.cancelButton.focus();
                } else {
                    this.confirmButton.focus();
                }
            });
        }

        cancelValue() {
            return this.active?.mode === 'prompt' ? null : false;
        }

        confirmValue() {
            if (this.active?.mode === 'prompt') return this.input.value;
            return true;
        }

        finish(value) {
            if (!this.active) return;
            const request = this.active;
            this.active = null;
            if (this.dialog.open) this.dialog.close();
            if (request.previousFocus?.isConnected && typeof request.previousFocus.focus === 'function') {
                request.previousFocus.focus({ preventScroll: true });
            }
            request.resolve(value);
            queueMicrotask(() => this.showNext());
        }

        onKeydown(event) {
            if (!this.active) return;
            if (event.key === 'Enter' && this.active.mode === 'prompt' && event.target === this.input) {
                event.preventDefault();
                this.finish(this.input.value);
                return;
            }
            if (event.key !== 'Tab') return;
            const focusable = [this.input, this.cancelButton, this.confirmButton]
                .filter(element => !element.disabled && !element.closest('[hidden]'));
            if (!focusable.length) return;
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        }
    }

    const dialogElement = document.getElementById('app-dialog');
    const dialogService = dialogElement ? new AppDialogService(dialogElement) : null;

    window.WordTTSUI = Object.freeze({
        enhanceSelects,
        syncSelect,
        closeSelects: closeOpenSelect,
        confirm: options => dialogService?.confirm(options) ?? Promise.resolve(false),
        prompt: options => dialogService?.prompt(options) ?? Promise.resolve(null),
        alert: options => dialogService?.alert(options) ?? Promise.resolve(false),
    });
})();
