/** Renderer module: review.domain */
(function attachRendererFeature_review_domain(root) {
    'use strict';

function reviewItemIsEditable(item, workspace = authoritativeWorkspace()) {
    const status = String(item?.status || '').toUpperCase();
    const hasContent = readItemContentCache(item?.item_id) !== undefined
        || typeof item?.normalized_content === 'string';
    return workspaceActionEnabled('SAVE_CONFIGURATION', workspace)
        && ['PENDING', 'SKIPPED'].includes(status)
        && hasContent
        && Boolean(item?.item_id);
}

function reviewStatusLabel(status) {
    switch (String(status || '').toUpperCase()) {
        case 'SUCCEEDED': return '已完成';
        case 'SKIPPED': return '已跳过';
        case 'FAILED': return '生成失败';
        case 'CANCELLED': return '已取消';
        case 'RUNNING': return '生成中';
        case 'PENDING': return '待处理';
        default: return String(status || '待处理');
    }
}

// The parser has two useful type dimensions: the document family and the
// leaf item type.  Keep the code-to-label map here so the review surface can
// show either the parser's Chinese label or the atomic model's stable code.
const REVIEW_TYPE_LABELS = Object.freeze({
    info_acquisition: '信息获取',
    listening_info: '听选信息',
    answer_question: '回答问题',
    listening_choice: '听后选择',
    listening_response: '听后应答',
    info_retelling: '信息转述及询问',
    asking_info: '询问信息',
    listening_record_retelling: '听后记录并转述信息',
    imitation_reading: '模仿朗读',
    text_reading: '课文跟读',
    text_reading_sentence: '句子跟读',
    text_reading_paragraph: '段落跟读',
    text_reading_discourse: '语篇跟读',
    vocabulary: '词汇',
});

const REVIEW_TYPE_ALIASES = Object.freeze({
    '听选信息题目': '听选信息',
    '听选信息录音稿': '听选信息',
    '回答问题题目': '回答问题',
    '回答问题录音稿': '回答问题',
    '听后选择录音稿': '听后选择',
    '听后应答录音稿': '听后应答',
    '听后记录并转述信息录音稿': '听后记录并转述信息',
    '信息转述录音稿': '信息转述',
    '询问信息录音稿': '询问信息',
    '模仿朗读录音稿': '模仿朗读',
    '课文跟读录音稿': '课文跟读',
});

const REVIEW_TYPE_FAMILIES = Object.freeze({
    listening_info: '信息获取',
    answer_question: '信息获取',
    listening_choice: '听后选择',
    listening_response: '听后应答',
    info_retelling: '信息转述及询问',
    asking_info: '信息转述及询问',
    listening_record_retelling: '听后记录并转述信息',
    imitation_reading: '模仿朗读',
    text_reading_sentence: '课文跟读',
    text_reading_paragraph: '课文跟读',
    text_reading_discourse: '课文跟读',
    vocabulary: '词汇',
});

const REVIEW_GENERIC_TYPES = new Set([
    'document', 'audio', 'content', 'question', 'stimulus', 'work_item',
    '题目', '题干', '录音稿', '正文', '文本', '文档', '内容',
]);

function reviewTypeKey(value) {
    return String(value ?? '').trim().toLocaleLowerCase('zh-CN');
}

function reviewTypeLabel(value) {
    const raw = String(value ?? '').trim();
    if (!raw) return '';
    const key = reviewTypeKey(raw);
    return REVIEW_TYPE_LABELS[key] || REVIEW_TYPE_ALIASES[raw] || REVIEW_TYPE_ALIASES[key] || raw;
}

function reviewTypeParts(value) {
    if (Array.isArray(value)) {
        return value.flatMap(entry => reviewTypeParts(entry));
    }
    if (value && typeof value === 'object') {
        return reviewTypeParts(
            value.label || value.display_name || value.name || value.title || value.code,
        );
    }
    const raw = String(value ?? '').trim();
    if (!raw) return [];
    return raw
        .split(/\s*(?:\/|>|＞|→|›|»|·)\s*/)
        .map(part => reviewTypeLabel(part))
        .filter(part => part && !/^\d+$/.test(part));
}

function reviewCleanTypeParts(value) {
    return reviewTypeParts(value)
        .map(part => reviewTypeLabel(part))
        .filter(part => part && !reviewIsGenericType(part));
}

function reviewTypePartsMatch(left, right) {
    return left.length === right.length
        && left.every((part, index) => reviewTypeKey(part) === reviewTypeKey(right[index]));
}

function reviewTypeSequenceIndex(parts, sequence) {
    if (!sequence.length || sequence.length > parts.length) return -1;
    for (let index = 0; index <= parts.length - sequence.length; index += 1) {
        if (reviewTypePartsMatch(parts.slice(index, index + sequence.length), sequence)) return index;
    }
    return -1;
}

function reviewAppendUniqueTypeParts(target, values) {
    values.forEach(value => {
        reviewCleanTypeParts(value).forEach(part => {
            if (!target.some(existing => reviewTypeKey(existing) === reviewTypeKey(part))) {
                target.push(part);
            }
        });
    });
}

function reviewIsGenericType(value) {
    return REVIEW_GENERIC_TYPES.has(reviewTypeKey(value));
}

function reviewPushTypePart(parts, value) {
    reviewTypeParts(value).forEach(part => {
        if (reviewIsGenericType(part)) return;
        if (!parts.some(existing => reviewTypeKey(existing) === reviewTypeKey(part))) {
            parts.push(part);
        }
    });
}

function reviewTypePathForItem(item, fallbackDocType = '') {
    const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
    const subtypeCode = item?.question_type
        || item?.sub_type_code
        || metadata.question_type
        || metadata.sub_type_code;
    const subtypeLabel = reviewTypeLabel(subtypeCode);
    const explicitPath = item?.type_path
        || item?.typePath
        || item?.type_hierarchy
        || item?.typeHierarchy
        || metadata.type_path
        || metadata.type_hierarchy
        || metadata.typeHierarchy;
    let family = item?.major_type
        || metadata.major_type
        || metadata.doc_type
        || item?.doc_type
        || fallbackDocType;
    if (reviewIsGenericType(family) && subtypeCode) family = REVIEW_TYPE_FAMILIES[reviewTypeKey(subtypeCode)] || family;
    const explicitParts = reviewCleanTypeParts(explicitPath);
    const familyParts = reviewCleanTypeParts(family);
    const fallbackParts = [
        // item_type/category is the parser's concrete leaf type for legacy and
        // current source imports.  Stable subtype codes cover atomic
        // projections where the item type is only “audio”.
        item?.category,
        item?.item_type,
        metadata.category,
        subtypeLabel,
    ];
    const roleCode = reviewTypeKey(item?.role);
    if (REVIEW_TYPE_LABELS[roleCode]) fallbackParts.push(roleCode);

    const parts = [];
    if (explicitParts.length > 0) {
        const familyPrefix = familyParts.length > 0
            && reviewTypePartsMatch(explicitParts.slice(0, familyParts.length), familyParts);
        const familyIndex = familyPrefix ? 0 : reviewTypeSequenceIndex(explicitParts, familyParts);
        if (familyPrefix || familyParts.length === 0) {
            // A supplied type_path is authoritative, including repeated names
            // at different levels.  Do not flatten it with set-like de-duping.
            parts.push(...explicitParts);
        } else if (familyIndex > 0) {
            // Older projections occasionally put the family after a leaf
            // label. Restore the navigable order without losing the path.
            parts.push(...familyParts, ...explicitParts.slice(0, familyIndex), ...explicitParts.slice(familyIndex + familyParts.length));
        } else {
            // A relative path is still a path: anchor it under the document
            // family so the outline can consistently render a major node.
            parts.push(...familyParts, ...explicitParts);
        }

        // An explicit path is authoritative. If it contains only the family,
        // that means this item has no real subtype. Do not infer one from
        // parser presentation categories such as “模仿朗读-试卷正文” or
        // “模仿朗读-框内英文”; those describe the source/material variant,
        // not a navigable question-type level.
    } else {
        parts.push(...familyParts);
        // Without an explicit path, retain the legacy category fallback so
        // parsers that only expose a leaf category (for example
        // “句子跟读” under “课文跟读”) still produce a useful hierarchy.
        reviewAppendUniqueTypeParts(parts, fallbackParts);
    }

    return parts.length ? parts : ['未分类'];
}

function reviewRoleLabel(item) {
    const raw = String(item?.role ?? item?.metadata?.role ?? '').trim();
    if (!raw) return '';
    const key = reviewTypeKey(raw);
    if (['default', 'default_female', 'default_male', '__default_female__', '__default_male__', 'forced_female', 'speaker'].includes(key)) return '';
    if (REVIEW_TYPE_LABELS[key]) return '';
    return raw.replace(/^role:/i, '').trim();
}

function reviewGenderFromValue(value) {
    const key = String(value ?? '').trim().toLocaleLowerCase('zh-CN');
    if (!key) return '';
    if (['female', '女', '女声', '女生', '女性', 'woman', 'girl', 'f', 'w', 'default_female', '__default_female__', 'forced_female', 'forced-female'].includes(key)) return 'female';
    if (['male', '男', '男声', '男生', '男性', 'man', 'boy', 'm', 'default_male', '__default_male__', 'forced_male', 'forced-male'].includes(key)) return 'male';
    return '';
}

function reviewConcreteVoiceKey(item, roleLabel = '') {
    const policyKeys = new Set(['default', 'speaker', 'forced_female', 'forced-female', 'forced_male', 'forced-male']);
    const rawItemKey = String(item?.voice_key || '').trim();
    if (rawItemKey && !policyKeys.has(reviewTypeKey(rawItemKey))) return rawItemKey;
    if (roleLabel) {
        const roleKey = normalizeRoleKeyClient(roleLabel);
        const configured = roleVoiceMap?.[roleKey]
            || currentConfig?.role_voices?.[roleKey]
            || currentConfig?.role_voices?.[roleLabel];
        if (configured) return String(configured);
    }
    return '';
}

function reviewItemGender(item) {
    const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
    const directValues = [
        item?.voice,
        item?.voice_gender,
        item?.gender,
        metadata.voice,
        metadata.voice_gender,
        metadata.gender,
    ];
    for (const value of directValues) {
        const gender = reviewGenderFromValue(value);
        if (gender) return gender;
    }
    const itemKey = reviewTypeKey(item?.voice_key);
    if (['forced_female', 'forced-female'].includes(itemKey)) return 'female';
    if (['forced_male', 'forced-male'].includes(itemKey)) return 'male';
    const roleRaw = item?.role;
    const roleGender = reviewGenderFromValue(roleRaw);
    if (roleGender) return roleGender;

    const roleLabel = reviewRoleLabel(item);
    const configuredVoiceKey = reviewConcreteVoiceKey(item, roleLabel);
    const configuredVoice = configuredVoiceKey ? getVoiceEntry(configuredVoiceKey) : null;
    if (configuredVoice?.gender === 'female' || configuredVoice?.gender === 'male') return configuredVoice.gender;

    const normalizedKey = canonicalVoiceKey(item?.voice_key || '');
    if (normalizedKey && canonicalVoiceKey(selectedDefaultMaleVoice) === normalizedKey) return 'male';
    if (normalizedKey && canonicalVoiceKey(selectedDefaultFemaleVoice) === normalizedKey) return 'female';
    // Unmarked content follows the product's documented default: female.
    return 'female';
}

function reviewVoicePresentation(item) {
    const role = reviewRoleLabel(item);
    const gender = reviewItemGender(item);
    const genderLabel = gender === 'male' ? '男声' : '女声';
    if (!role) return { role: '', voice: `默认${genderLabel}` };
    const voiceKey = reviewConcreteVoiceKey(item, role);
    const entry = voiceKey ? getVoiceEntry(voiceKey) : null;
    return {
        role: `角色：${role}`,
        voice: entry?.name ? `音色：${entry.name}` : '音色：按角色配置',
    };
}


registerRendererModule("review.domain", {
    reviewItemIsEditable,
    reviewStatusLabel,
    REVIEW_TYPE_LABELS,
    REVIEW_TYPE_ALIASES,
    REVIEW_TYPE_FAMILIES,
    REVIEW_GENERIC_TYPES,
    reviewTypeKey,
    reviewTypeLabel,
    reviewTypeParts,
    reviewCleanTypeParts,
    reviewTypePartsMatch,
    reviewTypeSequenceIndex,
    reviewAppendUniqueTypeParts,
    reviewIsGenericType,
    reviewPushTypePart,
    reviewTypePathForItem,
    reviewRoleLabel,
    reviewGenderFromValue,
    reviewConcreteVoiceKey,
    reviewItemGender,
    reviewVoicePresentation,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

