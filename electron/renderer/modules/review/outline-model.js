/** Renderer module: review.outlineModel */
(function attachRendererFeature_review_outlineModel(root) {
    'use strict';

function reviewOutlineReportedCount(group, fallbackItems = []) {
    const reported = Number(group?.item_count);
    if (Number.isFinite(reported) && reported >= 0) return Math.max(reported, fallbackItems.length);
    return fallbackItems.length;
}

function reviewOutlineNode(key, name, level) {
    return {
        key,
        name,
        level,
        itemCount: 0,
        items: [],
        children: [],
    };
}

function reviewOutlineRelativeTypePath(item, groupName) {
    const path = reviewTypePathForItem(item, groupName)
        .map(part => reviewTypeLabel(part))
        .filter(part => part && reviewTypeKey(part) !== '未分类');
    const normalizedGroupName = reviewTypeKey(groupName);
    const groupIndex = path.findIndex(part => reviewTypeKey(part) === normalizedGroupName);
    return groupIndex >= 0 ? path.slice(groupIndex + 1) : path;
}

function reviewOutlineEnsureChild(parent, name) {
    const normalizedName = reviewTypeLabel(name) || '未标注小题型';
    const childKey = reviewTypeKey(normalizedName);
    let child = parent.children.find(entry => reviewTypeKey(entry.name) === childKey);
    if (!child) {
        child = reviewOutlineNode(
            `${parent.key}/${childKey}`,
            normalizedName,
            Number(parent.level || 1) + 1,
        );
        parent.children.push(child);
    }
    return child;
}

function reviewOutlineNormalizeDirectItems(node) {
    const children = Array.isArray(node?.children) ? node.children : [];
    children.forEach(child => reviewOutlineNormalizeDirectItems(child));
    if (children.length === 0 || !Array.isArray(node?.items) || node.items.length === 0) return;

    // A node can contain both a typed branch and items without a deeper type.
    // Keep the branch visible by giving those items an explicit, collapsible
    // bucket instead of exposing question leaves by default.
    const untyped = reviewOutlineEnsureChild(node, '未标注小题型');
    untyped.items.push(...node.items);
    untyped.itemCount += node.items.length;
    node.items = [];
    reviewOutlineNormalizeDirectItems(untyped);
}

function reviewOutlineDefaultExpanded(model) {
    const children = Array.isArray(model?.children) ? model.children : [];
    const items = Array.isArray(model?.items) ? model.items : [];
    // Keep every type level visible, but hide question leaves until the user
    // opens the deepest type node.  A node with no deeper type is therefore
    // collapsed by default when it owns concrete items.
    return children.length > 0 && items.length === 0;
}

function reviewOutlineFirstItem(model) {
    let firstItem = null;
    let firstIndexedItem = null;
    let firstIndex = Number.MAX_SAFE_INTEGER;
    const visit = node => {
        if (!node) return;
        const items = Array.isArray(node.items) ? node.items : [];
        items.forEach(item => {
            if (!firstItem) firstItem = item;
            const index = reviewOutlineItemIndex(item);
            if (index !== null && index < firstIndex) {
                firstIndex = index;
                firstIndexedItem = item;
            }
        });
        const children = Array.isArray(node.children) ? node.children : [];
        children.forEach(visit);
    };
    visit(model);
    return firstIndexedItem || firstItem;
}

function reviewOutlineItemIndex(item) {
    const raw = item?.reviewIndex;
    if (raw === null || raw === undefined || (typeof raw === 'string' && !raw.trim())) return null;
    const index = Number(raw);
    return Number.isInteger(index) && index >= 0 ? index : null;
}

function reviewOutlineRebaseLevel(node, level = 1) {
    if (!node || typeof node !== 'object') return node;
    node.level = level;
    const children = Array.isArray(node.children) ? node.children : [];
    children.forEach(child => reviewOutlineRebaseLevel(child, level + 1));
    return node;
}

function buildReviewOutlineModel(groups, items, options = {}) {
    const sourceGroups = Array.isArray(groups) ? groups : [];
    const sourceItems = Array.isArray(items) ? items : [];
    const model = sourceGroups.map((group, groupIndex) => {
        const groupItems = sourceItems.filter(item => item?.groupIndex === groupIndex);
        const fullGroupItems = Array.isArray(group?.items) ? group.items : groupItems;
        const rawGroupValue = group?.content_type || group?.doc_type || group?.category || `内容组 ${groupIndex + 1}`;
        const rawGroupParts = reviewTypeParts(rawGroupValue)
            .map(part => reviewTypeLabel(part))
            .filter(Boolean);
        const isMeaningfulTypePart = part => part
            && !reviewIsGenericType(part)
            && reviewTypeKey(part) !== '未分类';
        const firstPath = fullGroupItems
            .map(item => reviewTypePathForItem(item, String(rawGroupValue)))
            .find(path => path.some(isMeaningfulTypePart))
            || [];
        const rawGroupName = group?.outline_root_name
            || rawGroupParts.find(isMeaningfulTypePart)
            || firstPath.find(isMeaningfulTypePart)
            || rawGroupParts[0]
            || `内容组 ${groupIndex + 1}`;
        const groupName = reviewTypeLabel(rawGroupName) || `内容组 ${groupIndex + 1}`;
        const groupPrefix = rawGroupParts.length > 1
            && reviewTypeKey(rawGroupParts[0]) === reviewTypeKey(groupName)
            ? rawGroupParts.slice(1)
            : [];
        const root = reviewOutlineNode(
            `major:${groupIndex}:${reviewTypeKey(groupName)}`,
            groupName,
            1,
        );
        root.rootKind = group?.outline_root_kind || 'type';
        root.itemCount = reviewOutlineReportedCount(group, fullGroupItems);
        groupItems.forEach(item => {
            let relativePath = reviewOutlineRelativeTypePath(item, groupName);
            if (relativePath.length === 0 && groupPrefix.length > 0) relativePath = groupPrefix;
            let node = root;
            relativePath.forEach(part => {
                node = reviewOutlineEnsureChild(node, part);
                node.itemCount += 1;
            });
            node.items.push(item);
        });
        reviewOutlineNormalizeDirectItems(root);
        return root;
    });

    // A single input unit is an implementation detail, not a navigation
    // choice. Keep the unit wrapper for multi-unit documents, but let the
    // audio outline start directly at the document's type directory when
    // there is only one unit to browse.
    if (options?.flattenSingleUnit === true && model.length === 1) {
        const [root] = model;
        const children = Array.isArray(root?.children) ? root.children : [];
        if (root?.rootKind === 'unit' && children.length > 0) {
            return children.map(child => reviewOutlineRebaseLevel(child, 1));
        }
    }
    return model;
}


registerRendererModule("review.outlineModel", {
    reviewOutlineReportedCount,
    reviewOutlineNode,
    reviewOutlineRelativeTypePath,
    reviewOutlineEnsureChild,
    reviewOutlineNormalizeDirectItems,
    reviewOutlineDefaultExpanded,
    reviewOutlineFirstItem,
    reviewOutlineItemIndex,
    reviewOutlineRebaseLevel,
    buildReviewOutlineModel,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
