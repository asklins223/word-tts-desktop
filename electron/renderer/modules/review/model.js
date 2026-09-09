/** Renderer module: review.model */
(function attachRendererFeature_review_model(root) {
    'use strict';

function workspaceItemsToParseResults(workspace) {
    const items = Array.isArray(workspace?.items) ? workspace.items : [];
    const systemInput = workspace?.system_input;
    const systemUnits = Array.isArray(systemInput?.units) ? systemInput.units : [];
    const systemSegments = Array.isArray(systemInput?.content_segments)
        ? systemInput.content_segments
        : [];
    const segmentsByItem = new Map();
    systemSegments.forEach(segment => {
        const itemId = String(segment?.item_id || '');
        if (itemId && !segmentsByItem.has(itemId)) segmentsByItem.set(itemId, segment);
    });
    const unitsById = new Map(systemUnits.map(unit => [String(unit?.unit_id || ''), unit]));
    const unitForItem = (item, segment, metadata) => {
        const directId = segment?.unit_id || metadata.unit_id;
        if (directId && unitsById.has(String(directId))) return unitsById.get(String(directId));
        const labels = [metadata.unit, metadata.unit_label, metadata.paper_set, metadata.set_number]
            .map(value => systemInputDisplayValue(value))
            .filter(Boolean);
        return systemUnits.find(unit => labels.includes(systemInputDisplayValue(unit?.label))) || null;
    };
    const normalizeItem = (item, index, unit = null) => {
        const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
        const docType = String(
            metadata.doc_type || metadata.category || item?.item_type || '未分类',
        );
        const itemType = String(
            item?.item_type || metadata.category || docType || 'document',
        );
        const itemId = String(item?.item_id || `workspace-item-${index}`);
        const segment = segmentsByItem.get(itemId) || null;
        const loadedContent = readItemContentCache(itemId);
        const normalizedContent = loadedContent !== undefined
            ? loadedContent
            : (typeof item?.normalized_content === 'string' ? item.normalized_content : null);
        const unitId = String(unit?.unit_id || segment?.unit_id || metadata.unit_id || '').trim() || null;
        const unitLabel = unit?.label || metadata.unit_label || metadata.unit || null;
        const enrichedMetadata = {
            ...metadata,
            ...(unitId && !metadata.unit_id ? { unit_id: unitId } : {}),
            ...(unitLabel && !metadata.unit_label ? { unit_label: unitLabel } : {}),
            ...(segment?.raw_text && metadata.raw_text == null ? { raw_text: segment.raw_text } : {}),
            ...(segment?.score != null && metadata.score == null ? { score: segment.score } : {}),
            ...(segment?.audio_filename_stem && metadata.audio_filename_stem == null
                ? { audio_filename_stem: segment.audio_filename_stem }
                : {}),
            ...(segment?.category && metadata.category == null ? { category: segment.category } : {}),
            // Paragraph/role facts are also present in content_segments.  Use
            // them as a lossless fallback when opening a workspace produced
            // by an older backend whose public item metadata was filtered
            // before these parser-owned fields were added to the allowlist.
            ...(segment?.role && metadata.role == null ? { role: segment.role } : {}),
            ...(segment?.paragraph_id && metadata.paragraph_id == null
                ? { paragraph_id: segment.paragraph_id }
                : {}),
            ...(segment?.paragraph_scope && metadata.paragraph_scope == null
                ? { paragraph_scope: segment.paragraph_scope }
                : {}),
            ...(segment?.paragraph_title != null && metadata.paragraph_title == null
                ? { paragraph_title: segment.paragraph_title }
                : {}),
            ...(segment?.audio_only_auxiliary === true ? { audio_only_auxiliary: true } : {}),
            // The generic item metadata projection is intentionally capped;
            // page_input has its own bounded system-input projection so a
            // long reference-answer list is not silently reduced to 32 items.
            ...(segment?.page_input ? { page_input: segment.page_input } : {}),
            ...(segment?.page_input_status ? { page_input_status: segment.page_input_status } : {}),
        };
        return {
            item_id: itemId,
            doc_type: docType,
            // Keep the parser's leaf type.  The old renderer copied doc_type
            // into category here, which made every row look like the same
            // top-level type (for example, every reading item became
            // “课文跟读” and hid “句子/段落/语篇跟读”).
            category: itemType,
            item_type: itemType,
            sequence: Number(item?.sequence ?? index),
            text: normalizedContent,
            content: normalizedContent,
            normalized_content: normalizedContent,
            content_ref: item?.content_ref || null,
            source_locator: item?.source_locator || null,
            metadata: enrichedMetadata,
            role: item?.role || null,
            voice_key: item?.voice_key || null,
            voice: item?.voice || metadata.voice || metadata.voice_gender || metadata.gender || null,
            question_type: item?.question_type || metadata.question_type || null,
            sub_type_code: item?.sub_type_code || metadata.sub_type_code || null,
            type_path: item?.type_path || metadata.type_path || metadata.type_hierarchy || null,
            unit_id: unitId,
            unit_label: unitLabel,
            listening_text: segment?.raw_text || enrichedMetadata.raw_text || null,
            score: segment?.score ?? enrichedMetadata.score ?? null,
            audio_artifact_id: segment?.audio_artifact_id || null,
            audio_filename_stem: segment?.audio_filename_stem || metadata.audio_filename_stem || null,
            audio_only_auxiliary: segment?.audio_only_auxiliary === true
                || metadata.audio_only_auxiliary === true,
            status: item?.status || 'PENDING',
            skip_reason: item?.skip_reason || null,
            error_code: item?.error_code || null,
            user_message: item?.user_message || null,
        };
    };

    // The system-input projection is the authoritative compatibility map for
    // multi-unit documents. Keep the old type-group projection when the full
    // graph is unavailable, so 2A/legacy workspaces render unchanged.
    if (systemUnits.length > 0) {
        const groups = new Map(systemUnits.map(unit => [String(unit?.unit_id || ''), {
            unit_id: String(unit?.unit_id || ''),
            unit_label: systemInputDisplayValue(unit?.label, '录入单元'),
            items: [],
        }]));
        const fallbackGroups = new Map();
        items.forEach((item, index) => {
            const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
            const segment = segmentsByItem.get(String(item?.item_id || `workspace-item-${index}`));
            const unit = unitForItem(item, segment, metadata);
            if (unit && groups.has(String(unit.unit_id))) {
                groups.get(String(unit.unit_id)).items.push(normalizeItem(item, index, unit));
                return;
            }
            const docType = String(metadata.doc_type || metadata.category || item?.item_type || '未分类');
            if (!fallbackGroups.has(docType)) fallbackGroups.set(docType, []);
            fallbackGroups.get(docType).push(normalizeItem(item, index));
        });
        const projected = [...groups.values()].map(group => {
            const contentTypes = [...new Set(group.items.map(item => item.doc_type).filter(Boolean))];
            return {
                doc_type: contentTypes[0] || '未分类',
                category: contentTypes[0] || '未分类',
                content_type: contentTypes[0] || '未分类',
                content_types: contentTypes,
                outline_root_name: group.unit_label,
                outline_root_kind: 'unit',
                unit_id: group.unit_id,
                unit_label: group.unit_label,
                item_count: group.items.length,
                items: group.items,
            };
        });
        fallbackGroups.forEach((groupItems, docType) => projected.push({
            doc_type: docType,
            category: docType,
            content_type: docType,
            content_types: [docType],
            item_count: groupItems.length,
            items: groupItems,
        }));
        if (projected.length) return projected;
    }

    const groups = new Map();
    items.forEach((item, index) => {
        const normalized = normalizeItem(item, index);
        if (!groups.has(normalized.doc_type)) groups.set(normalized.doc_type, []);
        groups.get(normalized.doc_type).push(normalized);
    });
    return [...groups.entries()].map(([docType, groupItems]) => ({
        doc_type: docType,
        category: docType,
        content_type: docType,
        content_types: [docType],
        item_count: groupItems.length,
        items: groupItems,
    }));
}

function reviewContentForItem(item) {
    const itemId = String(item?.item_id || '');
    const cacheKey = itemContentCacheKey(itemId);
    if (itemId && itemContentCache.has(cacheKey)) return readItemContentCache(itemId);
    if (typeof item?.normalized_content === 'string') return item.normalized_content;
    if (typeof item?.text === 'string') return item.text;
    if (typeof item?.content === 'string') return item.content;
    return '';
}

function reviewDocumentSequence(item) {
    const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
    const raw = item?.sequence
        ?? item?.document_sequence
        ?? item?.source_sequence
        ?? metadata.sequence
        ?? metadata.document_sequence
        ?? metadata.source_sequence;
    if (raw === null || raw === undefined || (typeof raw === 'string' && !raw.trim())) return null;
    const sequence = Number(raw);
    return Number.isFinite(sequence) ? sequence : null;
}

function reviewGroupsInDocumentOrder(groups) {
    const sourceGroups = Array.isArray(groups) ? groups : [];
    const allItems = sourceGroups.flatMap(group => (
        Array.isArray(group?.items) ? group.items : []
    ));
    // `number` is often reset inside each question type. Only reorder groups
    // when every item carries the parser/workspace's document-wide sequence;
    // otherwise the returned group order is the only trustworthy fallback.
    const hasCompleteSequence = allItems.length > 0
        && allItems.every(item => reviewDocumentSequence(item) !== null);
    if (!hasCompleteSequence) return sourceGroups;

    return sourceGroups
        .map((group, groupIndex) => {
            const groupItems = Array.isArray(group?.items) ? group.items : [];
            const sequences = groupItems
                .map(item => reviewDocumentSequence(item))
                .filter(sequence => sequence !== null);
            return {
                group,
                groupIndex,
                firstSequence: sequences.length ? Math.min(...sequences) : Number.MAX_SAFE_INTEGER,
            };
        })
        .sort((left, right) => left.firstSequence - right.firstSequence || left.groupIndex - right.groupIndex)
        .map(entry => entry.group);
}

function reviewItemsInDocumentOrder(groups) {
    const orderedGroups = reviewGroupsInDocumentOrder(groups);
    let sourceOrder = 0;
    const sourceItems = orderedGroups.flatMap((group, groupIndex) => {
        const groupItems = Array.isArray(group?.items) ? group.items : [];
        return groupItems.map(item => ({
            ...item,
            doc_type: item?.doc_type || group?.doc_type || group?.category || '未分类',
            sequence: reviewDocumentSequence(item),
            groupIndex,
            sourceOrder: sourceOrder++,
        }));
    });
    const hasCompleteSequence = sourceItems.length > 0
        && sourceItems.every(item => item.sequence !== null);
    if (!hasCompleteSequence) return sourceItems;
    return sourceItems.sort((left, right) => (
        left.sequence - right.sequence
        || left.sourceOrder - right.sourceOrder
    ));
}

function reviewDisplayFactValue(value, fallback = '') {
    if (value === null || value === undefined || value === '') return fallback;
    if (Array.isArray(value)) {
        const values = value.map(entry => reviewDisplayFactValue(entry)).filter(Boolean);
        return values.join('、') || fallback;
    }
    if (value && typeof value === 'object') {
        const label = value.name || value.label || value.text || value.value || value.id;
        if (label !== undefined && label !== null && String(label).trim()) return String(label).trim();
        try {
            return JSON.stringify(value);
        } catch (_error) {
            return fallback;
        }
    }
    return String(value).trim() || fallback;
}

function reviewUnitMarkerForItem(item, group = null, groupIndex = 0) {
    const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
    const read = key => item?.[key] ?? metadata[key];
    const unitId = reviewDisplayFactValue(read('unit_id'));
    const label = reviewDisplayFactValue(
        read('unit_label')
        || read('unit')
        || read('paper_set')
        || read('set_number')
        || read('set'),
    );
    if (unitId || label) {
        return {
            key: unitId ? `id:${unitId}` : `label:${label}`,
            unit_id: unitId,
            label: label || `录入单元 ${groupIndex + 1}`,
        };
    }
    const groupUnitId = reviewDisplayFactValue(group?.unit_id);
    const groupLabel = reviewDisplayFactValue(
        group?.unit_label || group?.outline_root_name,
    );
    if (group?.outline_root_kind === 'unit' || groupUnitId || groupLabel) {
        return {
            key: groupUnitId ? `id:${groupUnitId}` : `group:${groupIndex}`,
            unit_id: groupUnitId,
            label: groupLabel || `录入单元 ${groupIndex + 1}`,
        };
    }
    return null;
}

function reviewGroupsWithUnits(groups) {
    const sourceGroups = Array.isArray(groups) ? groups : [];
    const entries = sourceGroups.flatMap((group, groupIndex) => (
        (Array.isArray(group?.items) ? group.items : []).map(item => ({
            item,
            group,
            groupIndex,
            marker: reviewUnitMarkerForItem(item, group, groupIndex),
        }))
    ));
    if (!entries.some(entry => entry.marker)) return sourceGroups;

    const buckets = new Map();
    entries.forEach(entry => {
        const marker = entry.marker || {
            key: `group:${entry.groupIndex}`,
            unit_id: reviewDisplayFactValue(entry.group?.unit_id),
            label: reviewDisplayFactValue(entry.group?.outline_root_name) || `录入单元 ${entry.groupIndex + 1}`,
        };
        let bucket = buckets.get(marker.key);
        if (!bucket) {
            bucket = {
                marker,
                firstGroup: entry.group,
                groupIndexes: new Set(),
                items: [],
            };
            buckets.set(marker.key, bucket);
        }
        bucket.groupIndexes.add(entry.groupIndex);
        bucket.items.push(entry.item);
    });

    return [...buckets.values()].map((bucket, bucketIndex) => {
        const contentTypes = [...new Set(
            bucket.items
                .map(item => item?.doc_type || item?.item_type || item?.category)
                .filter(Boolean),
        )];
        const base = bucket.firstGroup || {};
        const unitLabel = bucket.marker.label || `录入单元 ${bucketIndex + 1}`;
        return {
            ...base,
            doc_type: contentTypes[0] || base.doc_type || '未分类',
            category: contentTypes[0] || base.category || base.doc_type || '未分类',
            content_type: contentTypes[0] || base.content_type || base.doc_type || '未分类',
            content_types: contentTypes.length ? contentTypes : (base.content_types || []),
            outline_root_name: unitLabel,
            outline_root_kind: 'unit',
            unit_id: bucket.marker.unit_id || base.unit_id || null,
            unit_label: unitLabel,
            item_count: bucket.items.length,
            items: bucket.items,
        };
    });
}

function reviewInputTypeForGroups(systemInput, groups) {
    const explicit = String(systemInput?.input_type || '').trim().toLowerCase();
    if (explicit) return explicit;
    const values = (Array.isArray(groups) ? groups : []).flatMap(group => [
        group?.doc_type,
        group?.category,
        ...(Array.isArray(group?.content_types) ? group.content_types : []),
        ...(Array.isArray(group?.items) ? group.items.flatMap(item => [
            item?.doc_type,
            item?.item_type,
            item?.category,
        ]) : []),
    ]).map(value => String(value || '').toLocaleLowerCase('zh-CN'));
    if (values.some(value => value.includes('词汇') || value.includes('单词') || value.includes('vocabulary'))) return 'vocabulary';
    if (values.some(value => value.includes('课文') || value.includes('跟读') || value.includes('textbook'))) return 'textbook';
    return 'paper';
}

function reviewUnitCountPresentation(groups, systemInput = null) {
    const sourceGroups = Array.isArray(groups) ? groups : [];
    const explicitGroupCount = sourceGroups.filter(group => (
        group?.outline_root_kind === 'unit'
        || group?.unit_id
        || group?.unit_label
    )).length;
    const systemUnitCount = Array.isArray(systemInput?.units) ? systemInput.units.length : 0;
    const inputType = reviewInputTypeForGroups(systemInput, sourceGroups);
    const isPaper = inputType === 'paper';
    const systemStatus = String(systemInput?.unit_count_status || '').trim();
    const candidateBoundaries = [];
    const candidateBoundaryKeys = new Set();
    (Array.isArray(systemInput?.units) ? systemInput.units : []).forEach(unit => {
        const boundaries = Array.isArray(unit?.evidence?.unit_grouping?.candidate_boundaries)
            ? unit.evidence.unit_grouping.candidate_boundaries
            : [];
        boundaries.forEach(boundary => {
            if (!boundary || typeof boundary !== 'object') return;
            const itemIds = Array.isArray(boundary.item_ids)
                ? boundary.item_ids.map(value => String(value || '')).filter(Boolean)
                : [];
            const key = itemIds.length
                ? `items:${itemIds.join('|')}`
                : `range:${String(boundary.first_sequence ?? '')}:${String(boundary.last_sequence ?? '')}:${String(boundary.first_locator ?? '')}:${String(boundary.last_locator ?? '')}`;
            if (candidateBoundaryKeys.has(key)) return;
            candidateBoundaryKeys.add(key);
            candidateBoundaries.push(boundary);
        });
    });
    // A confirmed projection keeps the original candidate ranges in its
    // audit evidence.  Those ranges are repeated on every unit, so they
    // cannot override the authoritative status and turn a confirmed split
    // back into a pending one in the renderer.
    const isCandidate = systemStatus === 'multiple_candidate'
        || (!systemStatus && candidateBoundaries.length > 1);
    const count = Math.max(explicitGroupCount, systemUnitCount, 1);
    const multiple = systemStatus === 'multiple_confirmed' || (!isCandidate && explicitGroupCount > 1);
    const noun = isPaper ? '套' : '个录入单元';
    let label = isPaper ? '当前按单套组织' : '当前按单个录入单元组织';
    if (isCandidate) {
        label = isPaper ? '发现可能多套，待确认' : '发现可能多个单元，待确认';
    } else if (multiple) {
        label = isPaper ? `已识别多套（${count}套）` : `已识别多个录入单元（${count}个）`;
    }
    return {
        inputType,
        status: isCandidate ? 'multiple_candidate' : (multiple ? 'multiple_confirmed' : 'single_default'),
        count,
        label,
        short: multiple ? `${count} ${noun}` : (isCandidate ? '待确认' : `1 ${noun}`),
        candidateCount: candidateBoundaries.length,
    };
}

const REVIEW_DOCUMENT_ENTRY_PROFILES = [
    {
        format: 'legacy_listening_paper',
        label: '听说测试题',
        types: ['模仿朗读', '信息获取', '信息转述及询问'],
    },
    {
        format: 'legacy_info_retelling',
        label: '信息转述及询问',
        types: ['信息转述及询问'],
    },
    {
        format: 'listening_paper',
        label: '听说测试题',
        types: ['听后选择', '听后应答', '模仿朗读', '听后记录并转述信息'],
    },
    {
        format: 'listening_selection',
        label: '听后选择',
        types: ['听后选择'],
    },
    {
        format: 'imitation_reading',
        label: '模仿朗读',
        types: ['模仿朗读'],
    },
    {
        format: 'listening_response',
        label: '听后应答',
        types: ['听后应答'],
    },
    {
        format: 'listening_record_retelling',
        label: '听后记录并转述信息',
        types: ['听后记录并转述信息'],
    },
    {
        // Textbook reading items intentionally do not carry page_input.  The
        // parser's document type is the structure evidence for this shape.
        format: 'text_reading',
        label: '课文跟读',
        types: [],
    },
];

const IMITATION_READING_ENTRY_PROFILE = 'imitation_reading_v1';
const RESPONSE_ENTRY_PROFILE = 'listening_response_v1';
const RESPONSE_COLORED_OPTIONS_SPECIAL_PROFILE = 'response_colored_options_special';
const RECORD_RETELLING_ENTRY_PROFILE = 'listening_record_retelling_v1';
const RECORD_RETELLING_TABLE_SPECIAL_PROFILE = 'record_retelling_table_special';
const SUPPORTED_IMITATION_SECTION_PROFILES = new Set([
    'imitation_unit_source_special',
    'imitation_boxed_special',
    'imitation_numbered_exam_special',
]);

function isImitationDocumentType(value) {
    const normalized = String(value || '').trim().toLowerCase();
    return normalized === '模仿朗读'
        || normalized === 'imitation_reading'
        || normalized.startsWith('模仿朗读-')
        || normalized.startsWith('模仿朗读/')
        || normalized.startsWith('imitation_reading-')
        || normalized.startsWith('imitation_reading/');
}
const DOCUMENT_ENTRY_SCHEMA_VERSION = 'document-entry-preflight-v1';

function unsupportedStaleDocumentEntrySupport(list, explicit) {
    return {
        schema_version: String(explicit?.schema_version || '').trim() || null,
        total: list.length,
        supportedCount: 0,
        supported: false,
        status: 'unsupported',
        format: null,
        label: '暂不支持',
        reason: '文档录入结构判断版本或条目事实过旧或不完整，当前暂不支持文稿录入，请重新解析文档。',
        detected_types: [],
        document_types: [],
        expected_types: REVIEW_DOCUMENT_ENTRY_PROFILES[0].types.slice(),
        invalid_count: Number.isInteger(explicit?.invalid_count) && explicit.invalid_count >= 0
            ? explicit.invalid_count
            : 0,
        major_section_profile_invalid_count: Number.isInteger(explicit?.major_section_profile_invalid_count)
            && explicit.major_section_profile_invalid_count >= 0
            ? explicit.major_section_profile_invalid_count
            : 0,
        entry_profile_invalid_count: Number.isInteger(explicit?.entry_profile_invalid_count)
            && explicit.entry_profile_invalid_count >= 0
            ? explicit.entry_profile_invalid_count
            : 0,
        entry_capability_invalid_count: Number.isInteger(explicit?.entry_capability_invalid_count)
            && explicit.entry_capability_invalid_count >= 0
            ? explicit.entry_capability_invalid_count
            : 0,
    };
}

function reviewItemDocumentTypes(item, metadata) {
    return [
        metadata?.doc_type,
        metadata?.document_type,
        metadata?.category,
        item?.doc_type,
        item?.category,
        item?.item_type,
    ].map(value => String(value || '').trim()).filter(Boolean);
}

function reviewItemIsImitation(item, metadata, pageType = '') {
    return isImitationDocumentType(pageType)
        || reviewItemDocumentTypes(item, metadata).some(isImitationDocumentType);
}

function reviewItemIsAudioOnlyAuxiliary(item) {
    const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
    return item?.audio_only_auxiliary === true || metadata.audio_only_auxiliary === true;
}

function reviewImitationEntryFactsAreCurrent(list) {
    for (const item of list) {
        const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
        const pageInput = metadata.page_input || item?.page_input;
        const pageType = pageInput && typeof pageInput === 'object' && !Array.isArray(pageInput)
            ? String(pageInput.type || '').trim()
            : '';
        if (!reviewItemIsImitation(item, metadata, pageType)) continue;
        const entryProfile = String(
            metadata.entry_profile || item?.entry_profile || '',
        ).trim();
        const majorSectionProfile = String(
            metadata.major_section_profile || item?.major_section_profile || '',
        ).trim();
        const capabilities = metadata.capabilities || item?.capabilities;
        if (
            !SUPPORTED_IMITATION_SECTION_PROFILES.has(majorSectionProfile)
            ||
            entryProfile !== IMITATION_READING_ENTRY_PROFILE
            || !capabilities
            || typeof capabilities !== 'object'
            || Array.isArray(capabilities)
            || capabilities.external_input !== true
        ) {
            return false;
        }
    }
    // This is an additional row-level check for imitation-reading rows, not
    // a requirement that every supported paper contain one. Pure listening
    // papers are also valid document-entry structures.
    return true;
}

function reviewRecordRetellingEntryFactsAreCurrent(list) {
    return list.every(item => {
        const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
        const pageInput = metadata.page_input || item?.page_input;
        if (String(pageInput?.type || '').trim() !== '听后记录并转述信息') return true;
        const capabilities = metadata.capabilities || item?.capabilities;
        return String(
            metadata.major_section_profile || item?.major_section_profile || '',
        ).trim() === RECORD_RETELLING_TABLE_SPECIAL_PROFILE
            && String(metadata.entry_profile || item?.entry_profile || '').trim()
                === RECORD_RETELLING_ENTRY_PROFILE
            && capabilities
            && typeof capabilities === 'object'
            && !Array.isArray(capabilities)
            && capabilities.external_input === true;
    });
}

function reviewResponseEntryFactsAreCurrent(list) {
    return list.every(item => {
        const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
        const pageInput = metadata.page_input || item?.page_input;
        if (String(pageInput?.type || '').trim() !== '听后应答') return true;
        const capabilities = metadata.capabilities || item?.capabilities;
        return String(
            metadata.major_section_profile || item?.major_section_profile || '',
        ).trim() === RESPONSE_COLORED_OPTIONS_SPECIAL_PROFILE
            && String(metadata.entry_profile || item?.entry_profile || '').trim()
                === RESPONSE_ENTRY_PROFILE
            && capabilities
            && typeof capabilities === 'object'
            && !Array.isArray(capabilities)
            && capabilities.external_input === true;
    });
}

function reviewEntryProfilesAreCurrent(list, format) {
    if (String(format || '').trim().startsWith('legacy_')) return true;
    if (String(format || '').trim() === 'listening_response') {
        return reviewResponseEntryFactsAreCurrent(list);
    }
    if (String(format || '').trim() === 'listening_record_retelling') {
        return reviewRecordRetellingEntryFactsAreCurrent(list);
    }
    return reviewImitationEntryFactsAreCurrent(list);
}

function reviewEntryTypeSetMatches(values, expected) {
    if (!Array.isArray(values) || !Array.isArray(expected)) return false;
    const normalized = values.map(value => String(value || '').trim()).filter(Boolean);
    if (normalized.length !== expected.length || new Set(normalized).size !== expected.length) return false;
    return expected.every(type => normalized.includes(type));
}

function reviewEntryPageFactsMatchFormat(list, format) {
    const profile = REVIEW_DOCUMENT_ENTRY_PROFILES.find(
        candidate => candidate.format === String(format || '').trim(),
    );
    if (!profile || !Array.isArray(list) || list.length === 0) return false;
    const detectedTypes = new Set();
    for (const item of list) {
        const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
        const pageInput = metadata.page_input || item?.page_input;
        if (!pageInput || typeof pageInput !== 'object' || Array.isArray(pageInput)) return false;
        const type = String(pageInput.type || '').trim();
        if (!profile.types.includes(type)) return false;
        detectedTypes.add(type);
    }
    return reviewEntryTypeSetMatches([...detectedTypes], profile.types);
}

function reviewTextbookEntryFactsMatchFormat(list) {
    if (!Array.isArray(list) || list.length === 0) return false;
    return list.every(item => {
        const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
        const pageInput = metadata.page_input || item?.page_input;
        if (pageInput) return false;
        const documentType = String(
            metadata.doc_type
            || metadata.document_type
            || item?.doc_type
            || '',
        ).trim();
        return documentType === '课文跟读';
    });
}

function reviewEntryFactsMatchFormat(list, format) {
    if (String(format || '').trim() === 'text_reading') {
        return reviewTextbookEntryFactsMatchFormat(list);
    }
    return reviewEntryPageFactsMatchFormat(list, format);
}

function reviewDocumentEntrySupport(items, systemInput = null) {
    const list = Array.isArray(items) ? items : [];
    const activeList = list.filter(item => !reviewItemIsAudioOnlyAuxiliary(item));
    const explicit = systemInput?.document_entry_support;
    if (explicit && typeof explicit === 'object' && typeof explicit.supported === 'boolean') {
        // A renderer can briefly receive a cached projection while the
        // workspace refresh is in flight.  Never trust an old
        // ``supported: true`` report that predates the profile/capability
        // contract; the safe recovery is to re-parse the document.
        const explicitProfile = REVIEW_DOCUMENT_ENTRY_PROFILES.find(
            candidate => candidate.format === String(explicit.format || '').trim(),
        );
        const currentPreflight = explicit.schema_version === DOCUMENT_ENTRY_SCHEMA_VERSION
            && ['invalid_count', 'major_section_profile_invalid_count', 'entry_profile_invalid_count', 'entry_capability_invalid_count']
                .every(field => Number.isInteger(explicit[field]) && explicit[field] >= 0)
            && explicit.invalid_count === 0
            && explicit.major_section_profile_invalid_count === 0
            && explicit.entry_profile_invalid_count === 0
            && explicit.entry_capability_invalid_count === 0
            && explicitProfile
            && Number.isInteger(explicit.structured_count)
            && (
                explicitProfile.format === 'text_reading'
                    ? explicit.structured_count === 0
                    : explicit.structured_count === activeList.length
            )
            && reviewEntryTypeSetMatches(explicit.detected_types, explicitProfile.types)
            && reviewEntryTypeSetMatches(explicit.expected_types, explicitProfile.types)
            && activeList.length > 0
            && reviewEntryFactsMatchFormat(activeList, explicitProfile.format)
            && reviewEntryProfilesAreCurrent(activeList, explicitProfile.format);
        if (explicit.supported === true && !currentPreflight) {
            return unsupportedStaleDocumentEntrySupport(list, explicit);
        }
        return {
            ...explicit,
            status: explicit.supported === true ? 'supported' : 'unsupported',
            total: list.length,
            supportedCount: explicitProfile?.format === 'text_reading'
                ? activeList.length
                : activeList.filter(item => {
                const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
                return Boolean(metadata.page_input || item?.page_input);
                }).length,
        };
    }

    // The server-side preflight is authoritative in the real workspace.  Do
    // not reopen the document-entry view from a stale/partial projection by
    // re-detecting the page types in the renderer.  The local fallback below
    // remains for legacy callers that intentionally omit systemInput.
    const hasSystemInputProjection = systemInput !== null
        && systemInput !== undefined
        && typeof systemInput === 'object'
        && !Array.isArray(systemInput);
    if (hasSystemInputProjection) {
        const reason = systemInput.available === false
            ? '当前工作区还没有完成文档录入结构判断，暂不支持文稿录入。'
            : '当前文档尚未通过服务端录入结构判断，暂不支持文稿录入。';
        return {
            schema_version: 'document-entry-preflight-v1',
            total: list.length,
            supportedCount: 0,
            supported: false,
            status: 'unsupported',
            format: null,
            label: '暂不支持',
            reason,
            detected_types: [],
            document_types: [],
            expected_types: REVIEW_DOCUMENT_ENTRY_PROFILES[0].types.slice(),
            invalid_count: 0,
        };
    }

    const detectedTypes = [];
    const documentTypes = [];
    let invalidCount = 0;
    let majorSectionProfileInvalidCount = 0;
    let entryProfileInvalidCount = 0;
    let entryCapabilityInvalidCount = 0;
    activeList.forEach(item => {
        const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
        const pageInput = metadata.page_input || item?.page_input;
        const pageInputStatus = String(
            metadata.page_input_status || item?.page_input_status || '',
        ).trim();
        if (pageInputStatus === 'invalid') invalidCount += 1;
        if (pageInput && typeof pageInput === 'object' && !Array.isArray(pageInput)) {
            const type = String(pageInput.type || '').trim();
            if (type && !detectedTypes.includes(type)) detectedTypes.push(type);
        }
        const documentTypeValues = reviewItemDocumentTypes(item, metadata);
        const documentType = documentTypeValues[0] || '';
        if (documentType && !documentTypes.includes(documentType)) documentTypes.push(documentType);
        const pageType = pageInput && typeof pageInput === 'object' && !Array.isArray(pageInput)
            ? String(pageInput.type || '').trim()
            : '';
        if (reviewItemIsImitation(item, metadata, pageType)) {
            const majorSectionProfile = String(
                metadata.major_section_profile || item?.major_section_profile || '',
            ).trim();
            if (!SUPPORTED_IMITATION_SECTION_PROFILES.has(majorSectionProfile)) {
                majorSectionProfileInvalidCount += 1;
            }
            const entryProfile = String(
                metadata.entry_profile || item?.entry_profile || '',
            ).trim();
            if (entryProfile !== IMITATION_READING_ENTRY_PROFILE) {
                entryProfileInvalidCount += 1;
            }
            const capabilities = metadata.capabilities || item?.capabilities;
            if (
                !capabilities
                || typeof capabilities !== 'object'
                || Array.isArray(capabilities)
                || capabilities.external_input !== true
            ) {
                entryCapabilityInvalidCount += 1;
            }
        }
    });
    const detectedSet = new Set(detectedTypes);
    const profile = REVIEW_DOCUMENT_ENTRY_PROFILES.find(candidate => (
        candidate.format !== 'text_reading'
        && invalidCount === 0
        && majorSectionProfileInvalidCount === 0
        && entryProfileInvalidCount === 0
        && entryCapabilityInvalidCount === 0
        && detectedSet.size === candidate.types.length
        && candidate.types.every(type => detectedSet.has(type))
    )) || null;
    if (profile?.format === 'listening_response') {
        activeList.forEach(item => {
            const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
            const pageInput = metadata.page_input || item?.page_input;
            if (String(pageInput?.type || '').trim() !== '听后应答') return;
            if (String(
                metadata.major_section_profile || item?.major_section_profile || '',
            ).trim() !== RESPONSE_COLORED_OPTIONS_SPECIAL_PROFILE) {
                majorSectionProfileInvalidCount += 1;
            }
            if (String(metadata.entry_profile || item?.entry_profile || '').trim()
                !== RESPONSE_ENTRY_PROFILE) {
                entryProfileInvalidCount += 1;
            }
            const capabilities = metadata.capabilities || item?.capabilities;
            if (!capabilities
                || typeof capabilities !== 'object'
                || Array.isArray(capabilities)
                || capabilities.external_input !== true) {
                entryCapabilityInvalidCount += 1;
            }
        });
    } else if (profile?.format === 'listening_record_retelling') {
        activeList.forEach(item => {
            const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
            const pageInput = metadata.page_input || item?.page_input;
            if (String(pageInput?.type || '').trim() !== '听后记录并转述信息') return;
            if (String(
                metadata.major_section_profile || item?.major_section_profile || '',
            ).trim() !== RECORD_RETELLING_TABLE_SPECIAL_PROFILE) {
                majorSectionProfileInvalidCount += 1;
            }
            if (String(metadata.entry_profile || item?.entry_profile || '').trim()
                !== RECORD_RETELLING_ENTRY_PROFILE) {
                entryProfileInvalidCount += 1;
            }
            const capabilities = metadata.capabilities || item?.capabilities;
            if (!capabilities
                || typeof capabilities !== 'object'
                || Array.isArray(capabilities)
                || capabilities.external_input !== true) {
                entryCapabilityInvalidCount += 1;
            }
        });
    }
    const recordRetellingProfile = profile?.format === 'listening_record_retelling';
    const responseProfile = profile?.format === 'listening_response';
    const validationProfileLabel = recordRetellingProfile
        ? '听后记录并转述信息'
        : responseProfile
            ? '听后应答'
            : '模仿朗读';
    const currentProfile = (
        majorSectionProfileInvalidCount === 0
        && entryProfileInvalidCount === 0
        && entryCapabilityInvalidCount === 0
    ) ? profile : null;
    const status = currentProfile ? 'supported' : 'unsupported';
    return {
        schema_version: 'document-entry-preflight-v1',
        total: list.length,
        supportedCount: activeList.filter(item => {
            const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
            return Boolean(metadata.page_input || item?.page_input);
        }).length,
        supported: status === 'supported',
        status,
        format: currentProfile?.format || null,
        label: currentProfile?.label || '暂不支持',
        reason: currentProfile
            ? null
            : majorSectionProfileInvalidCount
                ? `存在未开放或未确认的${validationProfileLabel}版式画像，当前暂不支持文稿录入。`
                : entryProfileInvalidCount
                ? `存在未开放或未确认的${validationProfileLabel}录入画像，当前暂不支持文稿录入。`
                : entryCapabilityInvalidCount
                    ? `${validationProfileLabel}的录入字段尚未确认完整，当前暂不支持文稿录入。`
                    : detectedTypes.length || documentTypes.length || invalidCount
                        ? '当前录入脚本只支持“听说测试题”“听后选择”“模仿朗读”“听后应答”和“听后记录并转述信息”五种文档结构。'
                        : '尚未获得文档录入结构判断，当前暂不支持文稿录入。',
        detected_types: detectedTypes,
        document_types: documentTypes.slice(0, 16),
        // Preserve the structurally matched shape in diagnostics even when
        // stale/missing entry facts correctly keep the action fail-closed.
        expected_types: profile?.types || REVIEW_DOCUMENT_ENTRY_PROFILES[0].types.slice(),
        invalid_count: invalidCount,
        major_section_profile_invalid_count: majorSectionProfileInvalidCount,
        entry_profile_invalid_count: entryProfileInvalidCount,
        entry_capability_invalid_count: entryCapabilityInvalidCount,
    };
}

function buildReviewUnitModels(groups, items, systemInput = null) {
    const sourceGroups = Array.isArray(groups) ? groups : [];
    const sourceItems = (Array.isArray(items) ? items : [])
        .filter(item => !reviewItemIsAudioOnlyAuxiliary(item));
    const hasExplicitUnits = sourceGroups.some(group => (
        group?.outline_root_kind === 'unit'
        || group?.unit_id
        || group?.unit_label
    ));
    if (!hasExplicitUnits) {
        const inputType = reviewInputTypeForGroups(systemInput, sourceGroups);
        return [{
            key: 'unit:default',
            unit_id: '',
            label: inputType === 'paper' ? '当前文档（单套）' : '当前文档',
            items: sourceItems,
            groups: sourceGroups,
        }];
    }
    return sourceGroups.map((group, groupIndex) => {
        const groupItems = sourceItems.filter(item => item?.groupIndex === groupIndex);
        const label = reviewDisplayFactValue(
            group?.unit_label || group?.outline_root_name,
        ) || `录入单元 ${groupIndex + 1}`;
        return {
            key: reviewDisplayFactValue(group?.unit_id) || `unit:position:${groupIndex + 1}`,
            unit_id: reviewDisplayFactValue(group?.unit_id),
            label,
            items: groupItems,
            groups: [group],
        };
    }).filter(unit => unit.items.length > 0 || sourceGroups.length === 1);
}


registerRendererModule("review.model", {
    workspaceItemsToParseResults,
    reviewContentForItem,
    reviewDocumentSequence,
    reviewGroupsInDocumentOrder,
    reviewItemsInDocumentOrder,
    reviewDisplayFactValue,
    reviewUnitMarkerForItem,
    reviewGroupsWithUnits,
    reviewInputTypeForGroups,
    reviewUnitCountPresentation,
    reviewDocumentEntrySupport,
    buildReviewUnitModels,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
