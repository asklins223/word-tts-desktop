"""JavaScript snippets injected into the Xunfei page.

The snippets are kept separate from browser lifecycle and job orchestration.
They are intentionally copied without behavioral changes during the first
extraction pass.
"""

from __future__ import annotations


# ============================================================================
# 页面注入 JS（集中管理）
# ============================================================================

class JS:
    CHECK_MODAL_HAS_TEXT = """
    (keywords) => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '');
        const expected = (keywords || []).map(normalize);
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            if (expected.every(kw => kw && text.includes(kw))) return true;
        }
        return false;
    }
    """

    CLICK_BTN_IN_MODAL = """
    (buttonText) => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const target = String(buttonText || '').replace(/\\s+/g, '');
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const btns = modal.querySelectorAll('button, [role="button"], .ant-btn');
            for (const b of btns) {
                if (!visible(b)) continue;
                const label = String(b.textContent || '').replace(/\\s+/g, '').trim();
                if (label === target) {
                    b.click();
                    return true;
                }
            }
        }
        return false;
    }
    """

    CLOSE_ALL_MODALS = """
    (excludeKeywords) => {
        const modals = document.querySelectorAll('.ant-modal');
        let closed = 0;
        for (const modal of modals) {
            const style = window.getComputedStyle(modal);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (modal.getBoundingClientRect().width === 0) continue;
            const text = modal.textContent || '';
            if (excludeKeywords.some(kw => text.includes(kw))) continue;
            const closeBtn = modal.querySelector('button.ant-modal-close, .ant-modal-close-x');
            if (closeBtn && closeBtn.offsetParent !== null) {
                closeBtn.click();
                closed++;
            }
        }
        return closed;
    }
    """

    DISMISS_LOCAL_DRAFT_PROMPT = """
    () => {
        // 讯飞编辑页会把上一次被中断的编辑内容保存在自己的 local
        // storage 中。重新打开持久化 Chrome profile 后，页面会先显示
        // “发现本地缓存”拦截层；它通常不是 ant-modal，也没有稳定的
        // class/role，不能只依赖通用弹窗选择器。当前 WordTTS 任务已经
        // 在自己的数据库中保存了正文，因此这里选择“不恢复本地编辑缓存”
        // 的操作，让自动化继续输入本次任务的内容。讯飞在存在云端版本时
        // 会把同一个操作显示为“使用云端缓存”，也必须视为可继续的分支。
        const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };

        const roots = [];
        for (const element of document.querySelectorAll('body *')) {
            if (!visible(element)) continue;
            const text = normalize(element.innerText || element.textContent || '');
            const cacheNotice = text.includes('发现本地缓存')
                || text.includes('检测到本地编辑缓存')
                || text.includes('本地编辑缓存');
            const recoveryChoice = text.includes('恢复本地缓存')
                || text.includes('是否恢复')
                || text.includes('空白开始')
                || text.includes('使用云端缓存');
            if (!cacheNotice || !recoveryChoice) continue;
            roots.push({element, size: text.length});
        }
        roots.sort((left, right) => left.size - right.size);
        for (const {element} of roots) {
            const controls = element.querySelectorAll(
                'button, [role="button"], [tabindex]'
            );
            for (const control of controls) {
                if (!visible(control)) continue;
                if (control.disabled || control.getAttribute('aria-disabled') === 'true') continue;
                const label = normalize(control.innerText || control.textContent || '');
                if (label === '空白开始'
                    || label === '从空白开始'
                    || label === '新建空白'
                    || label === '不恢复缓存'
                    || label === '使用云端缓存') {
                    control.click();
                    return 'clicked';
                }
            }
        }
        return roots.length ? 'blocked' : 'not_found';
    }
    """

    CHECK_NO_VISIBLE_MODAL = """
    () => {
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        return !Array.from(document.querySelectorAll(
            '.ant-modal, [role="dialog"], .el-dialog, .el-message-box'
        )).some(visible);
    }
    """

    GET_EDITOR_TEXT = """
    () => {
        const editor = document.querySelector('.ssml-editor');
        return editor?.textContent?.trim() || '';
    }
    """

    GET_SELECTION_TEXT = """
    () => {
        const selection = window.getSelection?.();
        if (!selection || selection.rangeCount === 0) return '';
        const range = selection.getRangeAt(0);
        // 讯飞把 speaker 标签和正文放在同一个标注节点里。浏览器的
        // Selection.toString() 会把不可编辑的 “Amanda-教育” 标签也读出来，
        // 导致已经标注过的区间在下一次修正时被误判为选区漂移。只从选区
        // 克隆片段中移除标签元节点，保留真正的 speaker-content 正文。
        const fragment = range.cloneContents();
        fragment.querySelectorAll(
            '.ssml-tag, .ssml-editor-placeholder, [data-type="range_anchor"]'
        ).forEach((node) => node.remove());
        return fragment.textContent || '';
    }
    """

    SELECT_EDITOR_RANGE = """
    ([firstIndex, lastIndex]) => {
        const paragraphs = Array.from(
            document.querySelectorAll('.ssml-editor p')
        );
        const first = paragraphs[Number(firstIndex)];
        const last = paragraphs[Number(lastIndex)];
        if (!first || !last) return null;

        const isEditorMetadataText = (node) => {
            const parent = node?.parentElement;
            return Boolean(parent?.closest(
                '.ssml-tag, .ssml-editor-placeholder, [data-type="range_anchor"]'
            ));
        };
        const firstTextNode = (root) => {
            const walker = document.createTreeWalker(
                root,
                NodeFilter.SHOW_TEXT,
            );
            let current = walker.nextNode();
            while (current) {
                if (!isEditorMetadataText(current) && current.textContent?.trim()) return current;
                current = walker.nextNode();
            }
            return null;
        };
        const lastTextNode = (root) => {
            const walker = document.createTreeWalker(
                root,
                NodeFilter.SHOW_TEXT,
            );
            let current = null;
            let next = walker.nextNode();
            while (next) {
                if (!isEditorMetadataText(next) && next.textContent?.trim()) current = next;
                next = walker.nextNode();
            }
            return current;
        };

        const startNode = firstTextNode(first);
        const endNode = lastTextNode(last);
        if (!startNode || !endNode) return null;
        const editor = first.closest('.ssml-editor');
        if (!editor) return null;

        editor.focus();
        const range = document.createRange();
        range.setStart(startNode, 0);
        range.setEnd(endNode, endNode.textContent?.length || 0);
        const selection = window.getSelection();
        if (!selection) return null;
        selection.removeAllRanges();
        selection.addRange(range);
        const fragment = range.cloneContents();
        fragment.querySelectorAll(
            '.ssml-tag, .ssml-editor-placeholder, [data-type="range_anchor"]'
        ).forEach((node) => node.remove());
        return fragment.textContent || '';
    }
    """

    SELECT_EDITOR_ROW = """
    (rowIndex) => {
        const paragraph = document.querySelectorAll('.ssml-editor p')[Number(rowIndex)];
        if (!paragraph) return null;
        const editor = paragraph.closest('.ssml-editor');
        if (!editor) return null;
        paragraph.scrollIntoView({
            block: 'center',
            inline: 'nearest',
            behavior: 'auto',
        });
        const isMetadata = (node) => Boolean(
            node?.parentElement?.closest(
                '.ssml-tag, .ssml-editor-placeholder, [data-type="range_anchor"]'
            )
        );
        const textNodes = [];
        const walker = document.createTreeWalker(paragraph, NodeFilter.SHOW_TEXT);
        let node = walker.nextNode();
        while (node) {
            // 选行供 ArrowRight 折叠时也不能把 ProseMirror 的缩进/换行
            // 空白当成正文，否则光标会落在真实文本之后的空白节点。
            if (!isMetadata(node) && node.textContent?.trim().length) textNodes.push(node);
            node = walker.nextNode();
        }
        const first = textNodes[0];
        const last = textNodes[textNodes.length - 1];
        if (!first || !last) return null;
        // JS 设置 Selection 后必须同时把焦点交还给 contenteditable；否则
        // 后续的真实键盘 ArrowRight 可能落到工具栏/搜索框而不是编辑器。
        editor.focus();
        const range = document.createRange();
        range.setStart(first, 0);
        range.setEnd(last, last.textContent?.length || 0);
        const selection = window.getSelection();
        if (!selection) return null;
        selection.removeAllRanges();
        selection.addRange(range);
        const rect = paragraph.getBoundingClientRect();
        return {
            text: range.cloneContents().textContent || '',
            box: {
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height,
            },
            activeEditor: document.activeElement === editor,
        };
    }
    """

    CHECK_CARET_AT_ROW_END = """
    (rowIndex) => {
        const paragraph = document.querySelectorAll('.ssml-editor p')[Number(rowIndex)];
        if (!paragraph) return { ok: false, reason: 'row_missing' };
        const editor = paragraph.closest('.ssml-editor');
        const selection = window.getSelection?.();
        if (!editor || !selection || selection.rangeCount !== 1) {
            return { ok: false, reason: 'selection_missing' };
        }

        const isMetadata = (node) => Boolean(
            node?.parentElement?.closest(
                '.ssml-tag, .ssml-editor-placeholder, [data-type="range_anchor"]'
            )
        );
        const walker = document.createTreeWalker(paragraph, NodeFilter.SHOW_TEXT);
        let last = null;
        let node = walker.nextNode();
        while (node) {
            if (!isMetadata(node) && node.textContent?.trim().length) last = node;
            node = walker.nextNode();
        }
        if (!last) return { ok: false, reason: 'no_text' };

        const range = selection.getRangeAt(0);
        const closestParagraph = (value) => {
            const element = value?.nodeType === Node.ELEMENT_NODE
                ? value
                : value?.parentElement;
            return element?.closest?.('p') || null;
        };
        const expectedText = String(last.textContent || '');
        let expectedOffset = expectedText.length;
        while (expectedOffset > 0 && /\\s/.test(expectedText[expectedOffset - 1])) {
            expectedOffset -= 1;
        }
        const inTargetRow = (
            closestParagraph(range.startContainer) === paragraph
            && closestParagraph(range.endContainer) === paragraph
        );
        const activeEditor = document.activeElement === editor
            || editor.contains(document.activeElement);
        const atEnd = (
            range.collapsed
            && range.startContainer === last
            && range.endContainer === last
            && range.startOffset === expectedOffset
            && range.endOffset === expectedOffset
        );
        return {
            ok: Boolean(activeEditor && inTargetRow && atEnd),
            activeEditor,
            inTargetRow,
            collapsed: range.collapsed === true,
            atEnd,
            expectedOffset,
            actualOffset: range.startOffset,
        };
    }
    """

    PLACE_CARET_AT_ROW_END = """
    (rowIndex) => {
        // 一次 evaluate 完成“光标放到行尾”：聚焦编辑器、滚动到该行、
        // 把原生 Selection 折叠到该行最后一个正文文本节点末尾。停顿按钮
        // 只在编辑器当前原生光标处插入标记，所以折叠光标与原脚本
        // “选整行 -> ArrowRight”等价，但省掉两三次重型 select_text 往返。
        // 焦点必须在同一次调用里归还编辑器，否则工具栏点击时
        // activeElement 还在音色面板上，点“2s”只会打开菜单。
        const paragraph = document.querySelectorAll('.ssml-editor p')[Number(rowIndex)];
        if (!paragraph) return { ok: false, reason: 'row_missing' };
        const editor = paragraph.closest('.ssml-editor');
        if (!editor) return { ok: false, reason: 'editor_missing' };
        paragraph.scrollIntoView({
            block: 'center',
            inline: 'nearest',
            behavior: 'auto',
        });
        const isMetadata = (node) => Boolean(
            node?.parentElement?.closest(
                '.ssml-tag, .ssml-editor-placeholder, [data-type="range_anchor"]'
            )
        );
        const textNodes = [];
        const walker = document.createTreeWalker(paragraph, NodeFilter.SHOW_TEXT);
        let node = walker.nextNode();
        while (node) {
            // 只认非空白正文节点：段内换行缩进不是内容，光标必须落在
            // 真实正文末尾，否则停顿标记会插到空白节点后面。
            if (!isMetadata(node) && node.textContent?.trim().length) textNodes.push(node);
            node = walker.nextNode();
        }
        const last = textNodes[textNodes.length - 1];
        if (!last) return { ok: false, reason: 'no_text' };
        editor.focus();
        const range = document.createRange();
        const text = String(last.textContent || '');
        let offset = text.length;
        while (offset > 0 && /\\s/.test(text[offset - 1])) offset -= 1;
        range.setStart(last, offset);
        range.setEnd(last, offset);
        const selection = window.getSelection();
        if (!selection) return { ok: false, reason: 'no_selection' };
        selection.removeAllRanges();
        selection.addRange(range);
        const actual = selection.rangeCount === 1
            ? selection.getRangeAt(0)
            : null;
        const closestParagraph = (node) => {
            const element = node?.nodeType === Node.ELEMENT_NODE
                ? node
                : node?.parentElement;
            return element?.closest?.('p') || null;
        };
        const activeEditor = document.activeElement === editor
            || editor.contains(document.activeElement);
        const accepted = Boolean(
            actual
            && actual.collapsed
            && actual.startContainer === last
            && actual.endContainer === last
            && actual.startOffset === offset
            && actual.endOffset === offset
            && closestParagraph(actual.startContainer) === paragraph
            && closestParagraph(actual.endContainer) === paragraph
            && activeEditor
        );
        return {
            ok: accepted,
            reason: accepted ? null : 'selection_not_at_row_end',
            text,
            collapsed: Boolean(actual?.collapsed),
            activeEditor,
            offset,
        };
    }
    """

    READ_COMPOSITE_QUEUE_STATE = """
    () => {
        const paragraphs = Array.from(
            document.querySelectorAll('.ssml-editor p')
        );
        const pendingNodes = Array.from(
            document.querySelectorAll('.msq-pending-range')
        );
        // 某些页面版本会在一个待处理区间内部再渲染装饰子节点；只把
        // 最外层节点计为一个，避免队列数量被 DOM 实现细节放大。
        const pendingRoots = pendingNodes.filter((node) => !pendingNodes.some(
            (other) => other !== node && other.contains(node)
        ));
        const indexOfParagraph = (paragraph) => paragraphs.indexOf(paragraph);
        const readRowIndex = (node) => {
            const paragraph = node.closest?.('.ssml-editor p');
            if (paragraph) return indexOfParagraph(paragraph);
            for (const attribute of (
                'data-row-index', 'data-paragraph-index', 'data-editor-row'
            )) {
                const raw = node.getAttribute?.(attribute);
                if (raw == null || !/^\\d+$/.test(String(raw).trim())) continue;
                const index = Number(raw);
                if (Number.isInteger(index) && index >= 0 && index < paragraphs.length) {
                    return index;
                }
            }
            return null;
        };
        const mappedRows = pendingRoots.map(readRowIndex);
        const rowIndices = mappedRows.every((index) => index !== null)
            ? mappedRows
            : null;
        const badgeText = Array.from(
            document.querySelectorAll('.msq-queue-badge')
        ).map((element) => element.innerText || element.textContent || '').join(' ');
        const badgeMatch = badgeText.match(/已选\\s*(\\d+)\\s*段/);
        return {
            pendingCount: pendingRoots.length,
            rowIndices,
            badgeCount: badgeMatch ? Number(badgeMatch[1]) : 0,
        };
    }
    """

    CLEAR_EDITOR = """
    () => {
        const editor = document.querySelector('.ssml-editor');
        if (editor) {
            editor.focus();
            editor.innerHTML = '<p><br></p>';
            editor.dispatchEvent(new Event('input', {bubbles: true}));
            return true;
        }
        return false;
    }
    """

    SET_PARAM_INPUT = """
    ([index, value]) => {
        const inputs = document.querySelectorAll('input.w-12');
        if (inputs.length <= index) return false;
        const inp = inputs[index];
        inp.focus();
        inp.value = String(value);
        inp.dispatchEvent(new Event('input', {bubbles: true}));
        inp.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
    }
    """

    READ_PARAM_INPUTS = """
    () => {
        return Array.from(document.querySelectorAll('input.w-12')).slice(0, 3).map(i => i.value);
    }
    """

    CHECK_VOICE_SELECTED = """
    (name) => {
        const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
        const expected = normalize(name);
        const visible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const selected = (button) => {
            const ariaSelected = button.getAttribute('aria-selected');
            const style = window.getComputedStyle(button);
            const inlineStyle = String(button.getAttribute('style') || '').replace(/\\s+/g, '').toLowerCase();
            const hasSelectedBorder = style.borderColor === 'rgb(26, 145, 255)'
                || inlineStyle.includes('border:1pxsolidrgb(26,145,255)')
                || inlineStyle.includes('border:1pxsolid#1a91ff');
            return ariaSelected === 'true'
                || button.classList.contains('active')
                || button.classList.contains('selected')
                || button.classList.contains('is-selected')
                || hasSelectedBorder;
        };
        const voiceLabel = (button) => {
            const label = button.querySelector('p, strong, [class*="name"], [class*="title"]');
            const alt = button.querySelector('img[alt]')?.getAttribute('alt') || '';
            return normalize(`${label?.textContent || button.textContent} ${alt}`);
        };
        for (const b of document.querySelectorAll('button')) {
            if (!visible(b) || !selected(b)) continue;
            const label = voiceLabel(b);
            // 先按音色卡片的主名称精确匹配，避免“Linda-品质”被另一个
            // 同名/相似名称卡片或隐藏 DOM 误判为已选中。
            if (label === expected || label.includes(expected)) return b.textContent?.trim() || label;
        }
        return null;
    }
    """

    SEARCH_AND_CLICK_VOICE = """
    (name) => {
        const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
        const expected = normalize(name);
        const visible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const labelOf = (button) => normalize([
            button.querySelector('p, strong, [class*="name"], [class*="title"]')?.textContent,
            button.textContent,
            button.querySelector('img[alt]')?.getAttribute('alt'),
        ].filter(Boolean).join(' '));
        const buttons = Array.from(document.querySelectorAll('button'))
            .filter((button) => visible(button) && labelOf(button).length < 100);
        // 搜索结果的卡片主名称优先精确匹配；只有页面没有提供独立名称节点
        // 时才退回到整张卡片包含匹配。
        const exact = buttons.find((button) => {
            const label = labelOf(button);
            return label === expected || label.includes(expected);
        });
        const target = exact || buttons.find((button) => labelOf(button).includes(expected));
        if (target) {
            target.click();
            return true;
        }
        return false;
    }
    """

    CHECK_SEARCH_RESULT = """
    (name) => {
        const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
        const expected = normalize(name);
        const visible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        for (const b of document.querySelectorAll('button')) {
            if (!visible(b)) continue;
            const label = normalize([
                b.querySelector('p, strong, [class*="name"], [class*="title"]')?.textContent,
                b.textContent,
                b.querySelector('img[alt]')?.getAttribute('alt'),
            ].filter(Boolean).join(' '));
            if (label === expected || (label.includes(expected) && label.length < 100)) return true;
        }
        return false;
    }
    """

    CHECK_GO_DOWNLOAD = """
    () => {
        const els = document.querySelectorAll('a, button, span, div');
        for (const el of els) {
            if (el.children.length === 0 && el.textContent?.trim() === '去下载' && el.offsetParent !== null) {
                return true;
            }
        }
        return false;
    }
    """

    CLICK_GO_DOWNLOAD = """
    () => {
        const els = document.querySelectorAll('a, button, span, div');
        for (const el of els) {
            if (el.children.length === 0 && el.textContent?.trim() === '去下载' && el.offsetParent !== null) {
                el.click();
                return true;
            }
        }
        return false;
    }
    """

    CHECK_DOWNLOAD_PAGE = """
    () => {
        const text = String(document.body?.innerText || '').replace(/\\s+/g, '');
        const checkboxes = document.querySelectorAll('input.ant-checkbox-input, input[type="checkbox"]');
        return text.includes('作品名称') && text.includes('审核通过') && checkboxes.length > 0;
    }
    """

    GET_DOWNLOAD_ROWS = """
    () => {
        const rowFromInput = (input) => {
            let parent = input;
            for (let level = 0; parent && level < 9; level += 1) {
                const classes = Array.from(parent.classList || []);
                if (classes.some((name) => name.endsWith('__item'))) return parent;
                parent = parent.parentElement;
            }
            const button = input.closest('[class*="__botton"]');
            return button ? button.parentElement : null;
        };
        const rows = [];
        const seen = new Set();
        for (const input of document.querySelectorAll('input.ant-checkbox-input, input[type="checkbox"]')) {
            const row = rowFromInput(input);
            if (!row || seen.has(row)) continue;
            const name = row.querySelector('[class*="__name"]');
            seen.add(row);
            rows.push({
                index: rows.length,
                text: String(row.innerText || '').replace(/\\s+/g, ' ').trim(),
                works_name: String(name?.innerText || '').replace(/\\s+/g, ' ').trim(),
            });
        }
        return rows;
    }
    """

    SELECT_DOWNLOAD_ROWS = """
    (targets) => {
        const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
        const rowFromInput = (input) => {
            let parent = input;
            for (let level = 0; parent && level < 9; level += 1) {
                const classes = Array.from(parent.classList || []);
                if (classes.some((name) => name.endsWith('__item'))) return parent;
                parent = parent.parentElement;
            }
            const button = input.closest('[class*="__botton"]');
            return button ? button.parentElement : null;
        };
        const rows = [];
        const seen = new Set();
        for (const input of document.querySelectorAll('input.ant-checkbox-input, input[type="checkbox"]')) {
            const row = rowFromInput(input);
            if (!row || seen.has(row)) continue;
            seen.add(row);
            rows.push({
                row,
                input: row.querySelector('input.ant-checkbox-input, input[type="checkbox"]'),
                text: normalize(row.innerText || ''),
                name: normalize(row.querySelector('[class*="__name"]')?.innerText || ''),
            });
        }

        const used = new Set();
        const selected = [];
        const missing = [];
        for (const target of Array.isArray(targets) ? targets : []) {
            const orderNo = normalize(target?.order_no);
            const worksName = normalize(target?.works_name);
            let found = -1;
            if (orderNo) {
                found = rows.findIndex((row, index) => (
                    !used.has(index) && row.text.includes(orderNo)
                ));
            }
            if (found < 0 && Number.isInteger(target?.row_index)) {
                const index = target.row_index;
                if (index >= 0 && index < rows.length && !used.has(index)) found = index;
            }
            if (found < 0 && worksName) {
                found = rows.findIndex((row, index) => (
                    !used.has(index) && row.name === worksName
                ));
            }
            if (found < 0) {
                missing.push({
                    works_id: String(target?.works_id || ''),
                    order_no: String(target?.order_no || ''),
                    works_name: String(target?.works_name || ''),
                });
                continue;
            }

            const checkbox = rows[found].input;
            if (!checkbox) {
                missing.push({
                    works_id: String(target?.works_id || ''),
                    order_no: String(target?.order_no || ''),
                    works_name: String(target?.works_name || ''),
                });
                continue;
            }
            if (!checkbox.checked) checkbox.click();
            if (!checkbox.checked) {
                missing.push({
                    works_id: String(target?.works_id || ''),
                    order_no: String(target?.order_no || ''),
                    works_name: String(target?.works_name || ''),
                });
                continue;
            }
            used.add(found);
            selected.push({
                works_id: String(target?.works_id || ''),
                order_no: String(target?.order_no || ''),
                works_name: String(target?.works_name || ''),
                row_index: found,
            });
        }
        return {selected, missing, row_count: rows.length};
    }
    """

    SCROLL_DOWNLOAD_LIST = """
    () => {
        let moved = false;
        const containers = document.querySelectorAll(
            '[class*="__scrolledList"], [class*="scrolledList"], [class*="scroll"]'
        );
        for (const container of containers) {
            if (container.scrollHeight > container.clientHeight) {
                container.scrollTop = container.scrollHeight;
                moved = true;
            }
        }
        window.scrollTo(0, document.body.scrollHeight);
        return moved;
    }
    """

    CLICK_DOWNLOAD_PAGE_BUTTON = """
    () => {
        for (const button of document.querySelectorAll('button')) {
            const style = window.getComputedStyle(button);
            const rect = button.getBoundingClientRect();
            const label = String(button.textContent || '').replace(/\\s+/g, '').trim();
            if (label !== '下载' || style.display === 'none' || style.visibility === 'hidden'
                || rect.width === 0 || rect.height === 0 || button.disabled) continue;
            button.click();
            return true;
        }
        return false;
    }
    """

    CHECK_FREE_MODAL = """
    () => {
        const modals = document.querySelectorAll('.ant-modal');
        for (const m of modals) {
            const style = window.getComputedStyle(m);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            const text = m.textContent || '';
            if (text.includes('本单免费') || text.includes('免费')) return true;
        }
        return false;
    }
    """

    CHECK_INSUFFICIENT = """
    () => {
        const body = document.body;
        if (!body) return false;
        // textContent 会包含 display:none 的模板、历史提示和隐藏弹窗，
        // 不能据此判断本次合成是否真的出现了额度错误。
        const text = body.innerText || '';
        return text.includes('余额不足') || text.includes('次数不足') || text.includes('额度不足');
    }
    """

    CHECK_RATE_LIMITED = """
    () => {
        const body = document.body;
        if (!body) return false;
        // 只读取当前页面的可见文本，避免把隐藏 DOM 中的旧提示误判为
        // 本次生成的频控错误。
        const text = body.innerText || '';
        return text.includes('操作频繁') || text.includes('稍后再试') || text.includes('请求过于频繁');
    }
    """

    PROBE_SYNTH_STATE = """
    (aiKeywordVariants) => {
        // 一轮只做一次页面扫描，供确认合成、AI 弹窗和订单等待共同使用。
        // React/Ant Design 页面可能延迟挂载，因此这里只负责“当前状态快照”，
        // Python 侧仍会持续轮询，不能把一次未命中当成页面没有弹窗。
        const normalize = (value) => String(value || '').replace(/\\s+/g, '');
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const modalSelector =
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box';
        const modals = Array.from(document.querySelectorAll(modalSelector))
            .filter(visible);
        const variants = Array.isArray(aiKeywordVariants) ? aiKeywordVariants : [];
        const bodyText = normalize(document.body?.innerText || '');
        let aiModal = false;
        let order = bodyText.includes('去下载');
        let free = false;
        let login = false;
        let confirm = false;
        let aiSwitch = 'not_found';
        const switchSelector = '[role="switch"], .ant-switch, button[aria-pressed]';
        const findAiSwitch = (modal) => {
            const switches = Array.from(modal.querySelectorAll(switchSelector));
            const labels = modal.querySelectorAll('span, div, label');
            const aiLabel = Array.from(labels).find((el) => (
                el.children.length === 0 && normalize(el.textContent) === 'AI标识'
            ));
            let parent = aiLabel;
            for (let level = 0; parent && level < 6; level += 1) {
                const rowSwitch = parent.querySelector(switchSelector);
                if (rowSwitch) return rowSwitch;
                parent = parent.parentElement;
            }
            return switches[0] || null;
        };

        for (const modal of modals) {
            const text = normalize(modal.innerText || modal.textContent || '');
            const isAi = variants.some(group => (
                Array.isArray(group)
                && group.length > 0
                && group.every(keyword => text.includes(normalize(keyword)))
            ));
            if (isAi) aiModal = true;
            if (text.includes('本单免费') || text.includes('免费')) free = true;
            if (
                text.includes('登录')
                && (text.includes('扫码') || text.includes('手机号') || text.includes('验证码'))
            ) login = true;

            if (text.includes('确认合成')) confirm = true;
            if (!confirm) {
                const buttons = modal.querySelectorAll('button, [role="button"]');
                for (const button of buttons) {
                    if (!visible(button)) continue;
                    if (normalize(button.innerText || button.textContent) === '确认合成') {
                        confirm = true;
                        break;
                    }
                }
            }

            // 优先按“AI 标识”所在行寻找开关；AI 说明弹窗没有 switch，
            // 且“不再提示”优先判定为说明弹窗。
            if (aiSwitch === 'not_found' && !text.includes('不再提示')) {
                const sw = findAiSwitch(modal);
                if (sw) {
                    const ariaChecked = sw.getAttribute('aria-checked');
                    const ariaPressed = sw.getAttribute('aria-pressed');
                    const isOn = ariaChecked === 'true'
                        || ariaPressed === 'true'
                        || sw.classList.contains('ant-switch-checked');
                    aiSwitch = isOn ? 'on' : 'off';
                }
            }
        }

        let state = null;
        if (aiModal) state = 'ai_modal';
        else if (bodyText.includes('余额不足') || bodyText.includes('次数不足') || bodyText.includes('额度不足')) {
            state = 'insufficient';
        } else if (bodyText.includes('操作频繁') || bodyText.includes('稍后再试') || bodyText.includes('请求过于频繁')) {
            state = 'rate_limited';
        } else if (login) {
            state = 'login';
        } else if (order || free) {
            state = 'order';
        } else if (confirm) {
            state = 'confirm';
        }

        return {
            state,
            ai_modal: aiModal,
            ai_switch: aiSwitch,
            order,
            free,
            login,
            confirm,
        };
    }
    """

    CHECK_LOGIN_MODAL = """
    () => {
        const modals = document.querySelectorAll('.ant-modal');
        for (const m of modals) {
            const style = window.getComputedStyle(m);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (m.getBoundingClientRect().width === 0) continue;
            const text = m.textContent || '';
            if ((text.includes('扫码') || text.includes('手机号') || text.includes('验证码')) && text.includes('登录')) return true;
        }
        return false;
    }
    """

    CHECK_NO_REMIND = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '');
        for (const modal of modals) {
            if (!visible(modal)) continue;
            if (!normalize(modal.textContent || '').includes('不再提示')) continue;

            // 优先点真实 checkbox input。Ant Design 的 input 可能是透明的，
            // 不能依赖 offsetParent/可见尺寸判断它是否可点击。
            const inputs = modal.querySelectorAll('input[type="checkbox"], .ant-checkbox-input');
            for (const input of inputs) {
                if (!input.checked) {
                    input.click();
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    return 'clicked_input';
                }
            }
            for (const input of inputs) {
                if (input.checked) return 'already';
            }

            // 兼容没有 input 的自定义 checkbox：按“ 不再提示 ”文字找到
            // 最近的 label / role=checkbox 容器并点击。
            const controls = modal.querySelectorAll(
                '.ant-checkbox-wrapper, label, [role="checkbox"], button'
            );
            for (const control of controls) {
                if (!normalize(control.textContent || '').includes('不再提示')) continue;
                const ariaChecked = control.getAttribute('aria-checked');
                if (ariaChecked === 'true' || control.classList.contains('ant-checkbox-checked')) {
                    return 'already';
                }
                control.click();
                return 'clicked_label';
            }
        }
        return 'not_found';
    }
    """

    CLICK_AI_SWITCH = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '');
        const switchSelector = '[role="switch"], .ant-switch, button[aria-pressed]';
        const findSwitch = (modal) => {
            const switches = Array.from(modal.querySelectorAll(switchSelector));
            // 讯飞当前 DOM 的开关和“AI 标识”文字在同一行；先按这行找，
            // 避免弹窗里存在其它开关时误点到别的设置。
            const aiLabel = Array.from(modal.querySelectorAll('*')).find((el) => {
                return el.children.length === 0 && normalize(el.textContent) === 'AI标识';
            });
            let parent = aiLabel;
            for (let level = 0; parent && level < 6; level += 1) {
                const rowSwitch = parent.querySelector(switchSelector);
                if (rowSwitch) return rowSwitch;
                parent = parent.parentElement;
            }
            return switches[0] || null;
        };
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            if (!text.includes('作品设置') && !text.includes('确认合成') && !text.includes('作品名称')) continue;
            if (text.includes('不再提示')) continue;
            const sw = findSwitch(modal);
            if (!sw) continue;
            if (!visible(sw)) continue;
            const ariaChecked = sw.getAttribute('aria-checked');
            const ariaPressed = sw.getAttribute('aria-pressed');
            const isOn = ariaChecked === 'true'
                || ariaPressed === 'true'
                || sw.classList.contains('ant-switch-checked');
            if (!isOn) {
                return 'already_off';
            }
            // 直接调用真实 button 的 click，确保 React/Ant Design 的事件
            // 处理器收到的是 button[role=switch] 的点击，而不是只点内部装饰节点。
            sw.click();
            return 'clicked';
        }
        return 'not_found';
    }
    """

    SET_MP3_FORMAT = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
        const labelText = (input) => {
            const label = input.closest('label');
            if (label) return normalize(label.textContent || '').toLowerCase();
            return normalize(input.parentElement?.textContent || '').toLowerCase();
        };
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            const radios = Array.from(
                modal.querySelectorAll('input[type="radio"][name="exportFormat"]')
            );
            // AI 说明弹窗的正文也会提到“作品设置”，必须同时要求真实
            // exportFormat 单选项，避免把说明弹窗误当成作品设置弹窗。
            if (!text.includes('作品设置') || radios.length === 0) continue;

            const mp3 = radios.find((input) => {
                const value = normalize(input.value).toLowerCase();
                const label = labelText(input);
                return value === 'mp3'
                    || label === 'mp3'
                    || label.startsWith('mp3');
            });
            if (!mp3) {
                return {
                    status: 'mp3_not_found',
                    checked: false,
                    radio_count: radios.length,
                };
            }
            if (mp3.disabled) {
                return {
                    status: 'mp3_disabled',
                    checked: Boolean(mp3.checked),
                    radio_count: radios.length,
                };
            }
            if (mp3.checked) {
                return {
                    status: 'already_mp3',
                    checked: true,
                    radio_count: radios.length,
                };
            }

            // 必须点击真实 radio input/label，让 React/Ant Design 的受控
            // 状态更新；不能只给 checked 属性赋值。
            mp3.click();
            if (!mp3.checked) {
                const label = mp3.closest('label');
                if (label) label.click();
            }
            return {
                status: 'clicked_mp3',
                checked: Boolean(mp3.checked),
                radio_count: radios.length,
            };
        }
        return {status: 'not_found', checked: false, radio_count: 0};
    }
    """

    GET_MP3_FORMAT = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
        const labelText = (input) => {
            const label = input.closest('label');
            if (label) return normalize(label.textContent || '').toLowerCase();
            return normalize(input.parentElement?.textContent || '').toLowerCase();
        };
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            const radios = Array.from(
                modal.querySelectorAll('input[type="radio"][name="exportFormat"]')
            );
            if (!text.includes('作品设置') || radios.length === 0) continue;
            const mp3 = radios.find((input) => {
                const value = normalize(input.value).toLowerCase();
                const label = labelText(input);
                return value === 'mp3'
                    || label === 'mp3'
                    || label.startsWith('mp3');
            });
            if (!mp3) {
                return {
                    status: 'mp3_not_found',
                    checked: false,
                    radio_count: radios.length,
                };
            }
            return {
                status: mp3.checked ? 'mp3' : 'other',
                checked: Boolean(mp3.checked),
                radio_count: radios.length,
            };
        }
        return {status: 'not_found', checked: false, radio_count: 0};
    }
    """

    CHECK_AI_SWITCH_OFF = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '');
        const switchSelector = '[role="switch"], .ant-switch, button[aria-pressed]';
        const findSwitch = (modal) => {
            const switches = Array.from(modal.querySelectorAll(switchSelector));
            const aiLabel = Array.from(modal.querySelectorAll('*')).find((el) => {
                return el.children.length === 0 && normalize(el.textContent) === 'AI标识';
            });
            let parent = aiLabel;
            for (let level = 0; parent && level < 6; level += 1) {
                const rowSwitch = parent.querySelector(switchSelector);
                if (rowSwitch) return rowSwitch;
                parent = parent.parentElement;
            }
            return switches[0] || null;
        };
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            if (!text.includes('作品设置') && !text.includes('确认合成') && !text.includes('作品名称')) continue;
            if (text.includes('不再提示')) continue;
            const sw = findSwitch(modal);
            if (!sw || !visible(sw)) continue;
            const ariaChecked = sw.getAttribute('aria-checked');
            const ariaPressed = sw.getAttribute('aria-pressed');
            const isOn = ariaChecked === 'true'
                || ariaPressed === 'true'
                || sw.classList.contains('ant-switch-checked');
            return !isOn;
        }
        return false;
    }
    """

    GET_AI_SWITCH_STATE = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '');
        const switchSelector = '[role="switch"], .ant-switch, button[aria-pressed]';
        const findSwitch = (modal) => {
            const switches = Array.from(modal.querySelectorAll(switchSelector));
            const aiLabel = Array.from(modal.querySelectorAll('*')).find((el) => {
                return el.children.length === 0 && normalize(el.textContent) === 'AI标识';
            });
            let parent = aiLabel;
            for (let level = 0; parent && level < 6; level += 1) {
                const rowSwitch = parent.querySelector(switchSelector);
                if (rowSwitch) return rowSwitch;
                parent = parent.parentElement;
            }
            return switches[0] || null;
        };
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            if (!text.includes('作品设置') && !text.includes('确认合成') && !text.includes('作品名称')) continue;
            if (text.includes('不再提示')) continue;
            const sw = findSwitch(modal);
            if (!sw || !visible(sw)) continue;
            const ariaChecked = sw.getAttribute('aria-checked');
            const ariaPressed = sw.getAttribute('aria-pressed');
            const isOn = ariaChecked === 'true'
                || ariaPressed === 'true'
                || sw.classList.contains('ant-switch-checked');
            return isOn ? 'on' : 'off';
        }
        return 'not_found';
    }
    """

    CLICK_AI_CONFIRM = """
    () => {
        const modals = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
        const confirmLabels = new Set(['确认', '确定', '知道了', '我知道了', '继续']);
        for (const modal of modals) {
            if (!visible(modal)) continue;
            const text = normalize(modal.textContent || '');
            if (!text.includes('不再提示')) continue;
            const btns = modal.querySelectorAll('button, [role="button"], .ant-btn');
            for (const b of btns) {
                if (!visible(b)) continue;
                const label = normalize(b.textContent || '');
                if (confirmLabels.has(label)) { b.click(); return true; }
            }
        }
        return false;
    }
    """

    SNAPSHOT_DIALOGS = """
    () => {
        const roots = document.querySelectorAll(
            '.ant-modal, .ant-modal-content, [role="dialog"], ' +
            '.el-dialog, .el-message-box'
        );
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && rect.width > 0
                && rect.height > 0;
        };
        return Array.from(roots)
            .filter(visible)
            .map((root) => ({
                className: String(root.className || '').slice(0, 160),
                text: String(root.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 500),
                buttons: Array.from(root.querySelectorAll('button, [role="button"]'))
                    .filter(visible)
                    .map((button) => String(button.textContent || '').replace(/\\s+/g, ' ').trim())
                    .filter(Boolean)
                    .slice(0, 12),
                checkboxes: Array.from(root.querySelectorAll('input[type="checkbox"], [role="checkbox"]'))
                    .map((input) => ({
                        checked: Boolean(input.checked) || input.getAttribute('aria-checked') === 'true',
                        className: String(input.className || '').slice(0, 100),
                    }))
                    .slice(0, 8),
            }));
    }
    """

    GET_API_CREDENTIALS = """
    () => {
        const readCookie = (name) => {
            try {
                if (typeof window.getCookie === 'function') {
                    return window.getCookie(name) || '';
                }
            } catch (_) {}
            const prefix = `${name}=`;
            const item = document.cookie.split('; ').find(v => v.startsWith(prefix));
            return item ? decodeURIComponent(item.slice(prefix.length)) : '';
        };
        let sessid = '';
        try {
            if (typeof window.getSessid === 'function') {
                sessid = window.getSessid() || '';
            }
        } catch (_) {}
        const fromSpread = readCookie('XF_FTYPE')
            || readCookie('fromSpread')
            || String(window._fromSpread || '');
        return {userId: readCookie('uid'), sessid, fromSpread};
    }
    """

    POST_API_JSON = """
    async ([url, param, base, headers]) => {
        try {
            const response = await fetch(url, {
                method: 'POST',
                credentials: 'include',
                headers: Object.assign({'Content-Type': 'application/json'}, headers || {}),
                body: JSON.stringify({param, base})
            });
            let data = null;
            try { data = await response.json(); } catch (_) {}
            return {httpStatus: response.status, data};
        } catch (error) {
            return {httpStatus: 0, error: String(error)};
        }
    }
    """

# AI 标识弹窗关键词变体（文案可能变化，逐个尝试）
AI_FLAG_KEYWORD_VARIANTS = [
    ["AI", "标识", "不再提示"],
    ["AI", "标识", "说明"],
    ["人工智能", "不再提示"],
    ["AI生成", "不再提示"],
    ["标识", "不再提示"],
    ["AI", "不再提示"],
    ["不再提示"],
]
