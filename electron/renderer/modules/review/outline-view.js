/** Renderer module: review.outlineView */
(function attachRendererFeature_review_outlineView(root) {
    'use strict';

function reviewOutlineNodeLabel(level, nodeKind = 'type') {
    if (nodeKind === 'unit') return '录入单元';
    if (level === 1) return '大题型';
    if (level === 2) return '小题型';
    return `第${level}级题型`;
}

function reviewOutlineItemExcerpt(item) {
    const text = reviewContentForItem(item).replace(/\s+/g, ' ').trim();
    if (!text) return '无正文预览';
    return text.length > 56 ? `${text.slice(0, 56)}…` : text;
}

function reviewAudioNavOrdinal(value) {
    const numerals = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];
    const number = Math.max(1, Math.floor(Number(value) || 1));
    if (number <= 10) return numerals[number - 1];
    if (number < 20) return `十${numerals[number - 11] || number - 10}`;
    return String(number);
}

function reviewOutlineDescendantItems(model) {
    const result = [];
    const visit = node => {
        if (!node) return;
        if (Array.isArray(node.items)) result.push(...node.items);
        if (Array.isArray(node.children)) node.children.forEach(visit);
    };
    visit(model);
    return result.sort((left, right) => {
        const leftIndex = reviewOutlineItemIndex(left);
        const rightIndex = reviewOutlineItemIndex(right);
        if (leftIndex === null && rightIndex === null) return 0;
        if (leftIndex === null) return 1;
        if (rightIndex === null) return -1;
        return leftIndex - rightIndex;
    });
}

function reviewAudioNavNumberList(parent, items) {
    const numbers = document.createElement('div');
    numbers.className = 'review-nav-numbers';
    items.forEach(item => {
        const targetIndex = reviewOutlineItemIndex(item);
        if (targetIndex === null) return;
        const number = document.createElement('button');
        number.type = 'button';
        number.className = 'review-nav-number';
        number.dataset.reviewIndex = String(targetIndex);
        number.textContent = String(targetIndex + 1).padStart(2, '0');
        const excerpt = reviewOutlineItemExcerpt(item);
        number.setAttribute('aria-label', `跳转到第 ${targetIndex + 1} 条解析内容：${excerpt}`);
        number.title = excerpt;
        number.addEventListener('click', () => jumpToReviewItem(item, targetIndex));
        numbers.appendChild(number);
    });
    if (numbers.childElementCount > 0) parent.appendChild(numbers);
    return numbers;
}

function reviewAudioNavSubgroup(parent, model) {
    const items = reviewOutlineDescendantItems(model);
    if (!items.length) return;
    const subgroup = document.createElement('div');
    subgroup.className = 'review-nav-subgroup';
    const title = document.createElement('div');
    title.className = 'review-nav-subgroup-title';
    const label = document.createElement('strong');
    label.textContent = model.name;
    const count = document.createElement('small');
    count.className = 'review-nav-subgroup-score';
    count.textContent = `${items.length} 条`;
    title.append(label, count);
    subgroup.append(title);
    reviewAudioNavNumberList(subgroup, items);
    parent.appendChild(subgroup);
}

function reviewAudioNavGroup(parent, model, index) {
    const items = reviewOutlineDescendantItems(model);
    if (!items.length) return;
    const group = document.createElement('section');
    group.className = 'review-nav-group review-audio-nav-group';
    const title = document.createElement('div');
    title.className = 'review-nav-group-title';
    const label = document.createElement('span');
    label.className = 'review-nav-group-title-text';
    label.textContent = `${reviewAudioNavOrdinal(index + 1)}、${model.name}`;
    const count = document.createElement('small');
    count.className = 'review-nav-group-score';
    count.textContent = `${items.length} 条`;
    title.append(label, count);
    group.appendChild(title);

    const children = Array.isArray(model.children) ? model.children : [];
    const directItems = Array.isArray(model.items) ? model.items : [];
    if (children.length > 0) {
        children.forEach(child => reviewAudioNavSubgroup(group, child));
        if (directItems.length) reviewAudioNavNumberList(group, directItems);
    } else {
        reviewAudioNavNumberList(group, items);
    }
    parent.appendChild(group);
}

function renderReviewAudioDirectory(outline, model) {
    const navigation = document.createElement('div');
    navigation.className = 'review-audio-nav';
    navigation.setAttribute('aria-label', '按题型和题号浏览解析内容');
    const roots = Array.isArray(model) ? model : [];
    roots.forEach((root, rootIndex) => {
        if (root?.rootKind !== 'unit') {
            reviewAudioNavGroup(navigation, root, rootIndex);
            return;
        }
        const items = reviewOutlineDescendantItems(root);
        if (!items.length) return;
        const unit = document.createElement('section');
        unit.className = 'review-nav-unit';
        const unitTitle = document.createElement('div');
        unitTitle.className = 'review-nav-unit-title';
        unitTitle.textContent = root.name || `第${reviewAudioNavOrdinal(rootIndex + 1)}套`;
        unit.appendChild(unitTitle);
        const children = Array.isArray(root.children) ? root.children : [];
        children.forEach((child, childIndex) => reviewAudioNavGroup(unit, child, childIndex));
        if (!children.length) reviewAudioNavNumberList(unit, items);
        navigation.appendChild(unit);
    });
    outline.appendChild(navigation);
}

function createReviewOutlineChevron() {
    const icon = document.createElement('span');
    icon.className = 'review-outline-chevron';
    icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>';
    return icon;
}

function setReviewOutlineExpanded(node, children, disclosure, model, level, expanded) {
    reviewOutlineExpansion.set(model.key, expanded);
    node.classList.toggle('is-collapsed', !expanded);
    node.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    children.hidden = !expanded;
    disclosure.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    const nodeKind = level === 1 && model?.rootKind === 'unit' ? 'unit' : 'type';
    disclosure.setAttribute(
        'aria-label',
        `${expanded ? '收起' : '展开'}${reviewOutlineNodeLabel(level, nodeKind)}：${model.name}`,
    );
    disclosure.title = expanded ? '收起此层级' : '展开此层级';
}

function createReviewOutlineJumpButton({ kind, level, nodeKind = 'type', name, count, target, targetIndex }) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `review-outline-jump review-outline-jump-${kind}`;
    const hasTargetIndex = Number.isInteger(targetIndex) && targetIndex >= 0;
    const nodeLabel = reviewOutlineNodeLabel(level, nodeKind);
    const label = target
        ? `跳转到${nodeLabel}：${name}，共 ${count} 条`
        : `${nodeLabel}：${name}，当前没有可跳转的条目`;
    button.setAttribute('aria-label', label);
    button.title = target ? `跳转到${name}` : label;
    button.disabled = !target || !hasTargetIndex;

    const copy = document.createElement('span');
    copy.className = 'review-outline-jump-copy';
    const kindLabel = document.createElement('small');
    kindLabel.className = 'review-outline-node-kind';
    kindLabel.textContent = nodeLabel;
    const title = document.createElement('strong');
    title.className = 'review-outline-jump-title';
    title.textContent = name;
    copy.append(kindLabel, title);

    const countEl = document.createElement('span');
    countEl.className = 'review-outline-count';
    countEl.textContent = `${count} 条`;
    button.append(copy, countEl);
    if (target && hasTargetIndex) {
        button.addEventListener('click', () => jumpToReviewItem(target, targetIndex));
    }
    return button;
}

function appendReviewOutlineLeaf(parent, item, level, position, setSize) {
    const node = document.createElement('li');
    node.className = 'review-outline-node review-outline-leaf';
    node.setAttribute('role', 'treeitem');
    node.setAttribute('aria-level', String(level));
    node.setAttribute('aria-posinset', String(position + 1));
    node.setAttribute('aria-setsize', String(setSize));
    const targetIndex = reviewOutlineItemIndex(item);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'review-outline-leaf-jump';
    button.setAttribute(
        'aria-label',
        `跳转到第 ${targetIndex !== null ? targetIndex + 1 : position + 1} 条解析内容：${reviewOutlineItemExcerpt(item)}`,
    );
    button.title = '跳转到对应解析条目';
    if (targetIndex === null) button.disabled = true;

    const number = document.createElement('span');
    number.className = 'review-outline-leaf-number';
    number.textContent = String(targetIndex !== null ? targetIndex + 1 : position + 1).padStart(2, '0');
    const title = document.createElement('span');
    title.className = 'review-outline-leaf-title';
    title.textContent = reviewOutlineItemExcerpt(item);
    button.append(number, title);
    if (targetIndex !== null) {
        button.dataset.reviewIndex = String(targetIndex);
        button.addEventListener('click', () => jumpToReviewItem(item, targetIndex));
    }
    node.appendChild(button);
    parent.appendChild(node);
}

function appendReviewOutlineParent(parent, model, level, position, setSize, idState) {
    const nodeLevel = Number.isInteger(model?.level) ? model.level : level;
    const nodeKind = nodeLevel === 1 && model?.rootKind === 'unit' ? 'unit' : 'type';
    const childModels = Array.isArray(model?.children) ? model.children : [];
    const leafItems = Array.isArray(model?.items) ? model.items : [];
    const hasContent = childModels.length > 0 || leafItems.length > 0;
    const node = document.createElement('li');
    node.className = `review-outline-node review-outline-parent is-level-${nodeLevel}`;
    node.setAttribute('role', 'treeitem');
    node.setAttribute('aria-level', String(nodeLevel));
    node.setAttribute('aria-posinset', String(position + 1));
    node.setAttribute('aria-setsize', String(setSize));
    node.dataset.level = String(nodeLevel);

    const row = document.createElement('div');
    row.className = 'review-outline-node-row';
    const target = reviewOutlineFirstItem(model);
    const targetIndex = reviewOutlineItemIndex(target);
    const jump = createReviewOutlineJumpButton({
        kind: nodeLevel === 1 ? 'major' : 'minor',
        level: nodeLevel,
        nodeKind,
        name: model.name,
        count: model.itemCount,
        target,
        targetIndex,
    });

    let disclosure = null;
    let children = null;
    if (hasContent) {
        disclosure = document.createElement('button');
        disclosure.type = 'button';
        disclosure.className = 'review-outline-disclosure';
        disclosure.appendChild(createReviewOutlineChevron());
        const childId = `review-outline-children-${idState.value}`;
        idState.value += 1;
        disclosure.setAttribute('aria-controls', childId);
        row.appendChild(disclosure);

        children = document.createElement('ul');
        children.id = childId;
        children.className = 'review-outline-children';
        children.setAttribute('role', 'group');
        const siblingCount = childModels.length + leafItems.length;
        childModels.forEach((child, childIndex) => {
            appendReviewOutlineParent(children, child, nodeLevel + 1, childIndex, siblingCount, idState);
        });
        leafItems.forEach((item, itemIndex) => {
            appendReviewOutlineLeaf(
                children,
                item,
                nodeLevel + 1,
                childModels.length + itemIndex,
                siblingCount,
            );
        });

        const savedExpansion = reviewOutlineExpansion.get(model.key);
        const expanded = typeof savedExpansion === 'boolean'
            ? savedExpansion
            : reviewOutlineDefaultExpanded(model);
        setReviewOutlineExpanded(node, children, disclosure, model, nodeLevel, expanded);
        disclosure.addEventListener('click', event => {
            event.preventDefault();
            event.stopPropagation();
            const nextExpanded = node.getAttribute('aria-expanded') !== 'true';
            setReviewOutlineExpanded(node, children, disclosure, model, nodeLevel, nextExpanded);
        });
    }
    if (!hasContent) {
        const placeholder = document.createElement('span');
        placeholder.className = 'review-outline-disclosure-placeholder';
        placeholder.setAttribute('aria-hidden', 'true');
        row.appendChild(placeholder);
    }
    row.appendChild(jump);
    node.appendChild(row);
    if (children) node.appendChild(children);
    parent.appendChild(node);
}

function renderReviewOutline(outline, model) {
    outline.replaceChildren();
    outline.setAttribute('role', 'navigation');
    outline.setAttribute('aria-label', '按题型和题号浏览解析内容');
    renderReviewAudioDirectory(outline, model);
}

function syncReviewOutlineSelection(index) {
    const rawIndex = index === null || index === undefined
        || (typeof index === 'string' && !index.trim())
        ? null
        : Number(index);
    const selectedIndex = Number.isInteger(rawIndex) && rawIndex >= 0 ? rawIndex : null;
    $$('#review-outline .review-outline-leaf-jump, #review-outline .review-nav-number').forEach(button => {
        const selected = selectedIndex !== null && Number(button.dataset.reviewIndex) === selectedIndex;
        button.classList.toggle('is-active', selected);
        button.setAttribute('aria-current', selected ? 'true' : 'false');
    });
}

function jumpToReviewItem(item, index = item?.reviewIndex) {
    const targetIndex = index === null || index === undefined
        || (typeof index === 'string' && !index.trim())
        ? null
        : Number(index);
    if (!Number.isInteger(targetIndex) || targetIndex < 0) return;
    const list = $('review-items');
    const row = [...(list?.querySelectorAll('.review-item-row') || [])]
        .find(entry => Number(entry.dataset.reviewIndex) === targetIndex);
    if (!row) {
        showToast('该条内容未在当前列表中展示', 'warning');
        return;
    }
    selectReviewItem(item, targetIndex, row);
    const reducedMotion = typeof window.matchMedia === 'function'
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    row.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'nearest' });
}


registerRendererModule("review.outlineView", {
    reviewOutlineNodeLabel,
    reviewOutlineItemExcerpt,
    reviewAudioNavOrdinal,
    reviewOutlineDescendantItems,
    reviewAudioNavNumberList,
    reviewAudioNavSubgroup,
    reviewAudioNavGroup,
    renderReviewAudioDirectory,
    createReviewOutlineChevron,
    setReviewOutlineExpanded,
    createReviewOutlineJumpButton,
    appendReviewOutlineLeaf,
    appendReviewOutlineParent,
    renderReviewOutline,
    syncReviewOutlineSelection,
    jumpToReviewItem,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
