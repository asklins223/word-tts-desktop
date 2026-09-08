/** Renderer module: review.document */
(function attachRendererFeature_review_document(root) {
    'use strict';

const reviewImagePreviewCache = new Map();
let reviewImageDialogBound = false;
let reviewImageDialogState = { returnFocus: null, scale: 1, url: null };
let reviewPanelViewportSyncBound = false;
let reviewPanelViewportSyncFrame = 0;
const REVIEW_PAGE_PAREN_SPEAKER_MARKER_RE = /^[ \t]*\([WwMm]\)(?:[ \t]*[:：])?[ \t]*/gim;
const REVIEW_PAGE_COLON_SPEAKER_MARKER_RE = /^[ \t]*[WwMm][ \t]*[:：][ \t]*/gim;
const REVIEW_PAGE_ANSWER_SPLIT_RE = /\s*\/\s*/;

function syncReviewPanelViewport() {
    reviewPanelViewportSyncFrame = 0;
    const page = $('content-review-view');
    const panels = [
        page?.querySelector('#review-document-nav'),
        page?.querySelector('.review-outline-panel'),
        page?.querySelector('.review-inspector'),
    ].filter(Boolean);
    const clearance = Number.parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue('--review-actions-clearance'),
    ) || 0;
    panels.forEach(panel => {
        const rect = panel.getBoundingClientRect();
        if (panel.hidden || (rect.width === 0 && rect.height === 0) || rect.bottom <= 0) {
            panel.style.removeProperty('--review-scroll-viewport-height');
            return;
        }
        const availableHeight = Math.floor(window.innerHeight - clearance - rect.top);
        if (availableHeight > 0) {
            panel.style.setProperty('--review-scroll-viewport-height', `${availableHeight}px`);
        } else {
            panel.style.removeProperty('--review-scroll-viewport-height');
        }
    });
}

function scheduleReviewPanelViewportSync() {
    if (reviewPanelViewportSyncFrame) return;
    reviewPanelViewportSyncFrame = requestAnimationFrame(syncReviewPanelViewport);
}

function bindReviewPanelViewportSync(page) {
    if (!reviewPanelViewportSyncBound) {
        const stepPage = page?.closest('.step-page');
        const scrollPage = stepPage?.querySelector('.page-scroll');
        scrollPage?.addEventListener('scroll', scheduleReviewPanelViewportSync, { passive: true });
        window.addEventListener('resize', scheduleReviewPanelViewportSync, { passive: true });
        window.visualViewport?.addEventListener('resize', scheduleReviewPanelViewportSync, { passive: true });
        reviewPanelViewportSyncBound = true;
    }
    scheduleReviewPanelViewportSync();
}

function ensureReviewViewShell() {
    const page = $('content-review-view');
    const workbench = page?.querySelector('.review-workbench');
    if (!page || !workbench) return null;
    workbench.id = 'review-audio-workbench';

    const outlineTitle = page.querySelector('#review-outline-title');
    if (outlineTitle) outlineTitle.textContent = '内容目录';
    const outlineNote = page.querySelector('.review-outline-panel .panel-note');
    outlineNote?.childNodes.forEach(node => {
        if (node.nodeType === 3 && node.textContent.trim()) {
            node.textContent = '点击题型或条目，快速定位要核对的内容。';
        }
    });
    const listHeadingCopy = page.querySelector('.review-list-panel > .panel-heading > div');
    const listHeading = listHeadingCopy?.parentElement;
    listHeading?.classList.add('review-list-heading');
    if (listHeadingCopy && !listHeadingCopy.querySelector('#review-list-description')) {
        const listDescription = document.createElement('p');
        listDescription.id = 'review-list-description';
        listDescription.className = 'review-list-description';
        listHeadingCopy.appendChild(listDescription);
    }
    const listToolbar = page.querySelector('.review-list-toolbar');
    if (listToolbar?.children.length >= 2) {
        listToolbar.children[0].textContent = '文档顺序';
        listToolbar.children[1].textContent = '状态 · 来源';
    }

    let switcher = page.querySelector('#review-view-switch');
    if (!switcher) {
        switcher = document.createElement('section');
        switcher.className = 'review-view-switch';
        switcher.id = 'review-view-switch';
        const copy = document.createElement('div');
        copy.className = 'review-view-switch-copy';
        const title = document.createElement('strong');
        title.textContent = '内容核对视图';
        const note = document.createElement('p');
        note.textContent = '文稿核对只在识别到可录入的文档结构时显示。';
        copy.append(title, note);

        const tabs = document.createElement('div');
        tabs.className = 'review-view-tabs';
        tabs.setAttribute('role', 'tablist');
        tabs.setAttribute('aria-label', '内容核对视图');
        [
            ['document', '文稿核对', '查看文档原文、题型和录入字段'],
            ['audio', '音频核对', '查看音频条目、文本和绑定状态'],
        ].forEach(([mode, label, description]) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'review-view-tab';
            button.dataset.reviewView = mode;
            if (mode === 'document') button.id = 'review-document-tab';
            button.setAttribute('role', 'tab');
            button.setAttribute('aria-controls', mode === 'document' ? 'review-document-view' : 'review-audio-workbench');
            button.setAttribute('aria-label', `${label}：${description}`);
            button.title = description;
            button.textContent = label;
            button.addEventListener('click', () => {
                if (mode === 'document' && button.hidden) return;
                if (reviewViewMode === mode) return;
                reviewViewMode = mode;
                renderContentReview();
            });
            tabs.appendChild(button);
        });
        const status = document.createElement('div');
        status.className = 'review-unit-status';
        status.id = 'review-unit-status';
        status.setAttribute('role', 'status');
        const statusLabel = document.createElement('strong');
        statusLabel.id = 'review-unit-status-label';
        const statusCount = document.createElement('span');
        statusCount.id = 'review-unit-status-count';
        status.append(statusLabel, statusCount);
        switcher.append(copy, tabs, status);
        workbench.parentNode.insertBefore(switcher, workbench);
    }

    let documentView = page.querySelector('#review-document-view');
    if (!documentView) {
        documentView = document.createElement('section');
        documentView.className = 'review-document-view';
        documentView.id = 'review-document-view';
        documentView.setAttribute('aria-labelledby', 'review-document-title');

        const heading = document.createElement('div');
        heading.className = 'review-document-heading';
        const headingCopy = document.createElement('div');
        const title = document.createElement('h2');
        title.id = 'review-document-title';
        title.textContent = '文稿核对';
        const note = document.createElement('p');
        note.id = 'review-document-description';
        note.textContent = '按文档原顺序查看题型、题号、原文和录入字段。';
        const format = document.createElement('span');
        format.className = 'review-document-format-badge';
        format.id = 'review-document-format';
        format.hidden = true;
        headingCopy.append(title, note, format);
        const count = document.createElement('span');
        count.className = 'review-document-count';
        count.id = 'review-document-count';
        heading.append(headingCopy, count);

        const boundary = document.createElement('div');
        boundary.className = 'review-document-boundary';
        boundary.id = 'review-document-boundary';
        boundary.setAttribute('role', 'status');

        const nav = document.createElement('nav');
        nav.className = 'review-document-nav';
        nav.id = 'review-document-nav';
        nav.setAttribute('aria-label', '文档题型和题号目录');
        nav.hidden = true;

        const units = document.createElement('div');
        units.className = 'review-document-units';
        units.id = 'review-document-units';
        units.setAttribute('role', 'list');
        units.setAttribute('aria-label', '文档内容');
        const empty = document.createElement('div');
        empty.className = 'empty-state review-document-empty';
        empty.id = 'review-document-empty';
        empty.hidden = true;
        const emptyTitle = document.createElement('strong');
        emptyTitle.textContent = '当前文档暂不开放文稿录入';
        const emptyNote = document.createElement('p');
        emptyNote.textContent = '当前任务仍可使用音频核对；文档录入脚本只支持已识别且字段完整的四种文档结构。';
        empty.append(emptyTitle, emptyNote);
        const content = document.createElement('div');
        content.className = 'review-document-content';
        content.append(heading, boundary, units, empty);
        documentView.append(nav, content);
        workbench.parentNode.insertBefore(documentView, workbench);
    }
    bindReviewPanelViewportSync(page);
    return { page, switcher, documentView, workbench };
}

function applyReviewViewMode(documentSupported = false) {
    const canShowDocument = documentSupported === true;
    let mode = reviewViewMode === 'audio' ? 'audio' : 'document';
    if (!canShowDocument) mode = 'audio';
    // A fail-closed render is a temporary presentation state, not a user
    // choice. Keep the default document preference intact so the later
    // authoritative workspace refresh can open the document view. Once the
    // document is supported, persist only the actual selected mode.
    if (canShowDocument) reviewViewMode = mode;
    const page = $('content-review-view');
    const documentView = $('review-document-view');
    const audioView = $('review-audio-workbench') || page?.querySelector('.review-workbench');
    const documentTab = page?.querySelector('#review-document-tab');
    const audioTab = page?.querySelector('[data-review-view="audio"]');
    const switcher = page?.querySelector('#review-view-switch');
    const switcherTitle = switcher?.querySelector('.review-view-switch-copy > strong');
    const switcherNote = switcher?.querySelector('.review-view-switch-copy > p');
    const tabs = switcher?.querySelector('.review-view-tabs');
    if (switcher) {
        switcher.classList.toggle('is-audio-only', !canShowDocument);
        switcher.dataset.documentSupported = canShowDocument ? 'true' : 'false';
    }
    if (switcherTitle) {
        switcherTitle.textContent = canShowDocument ? '内容核对视图' : '音频核对视图';
    }
    if (switcherNote) {
        switcherNote.textContent = canShowDocument
            ? '文稿核对只在识别到可录入的文档结构时显示。'
            : '当前文档暂不支持文稿录入，仅展示音频条目和绑定状态。';
    }
    if (tabs) tabs.setAttribute('aria-label', canShowDocument ? '内容核对视图' : '音频核对视图');
    if (documentTab) {
        documentTab.hidden = !canShowDocument;
        documentTab.setAttribute('aria-hidden', canShowDocument ? 'false' : 'true');
        if (!canShowDocument) {
            documentTab.tabIndex = -1;
            if (document.activeElement === documentTab) audioTab?.focus({ preventScroll: true });
        }
    }
    if (audioTab) audioTab.hidden = false;
    if (documentView) {
        documentView.hidden = mode !== 'document' || !canShowDocument;
        documentView.setAttribute('aria-hidden', documentView.hidden ? 'true' : 'false');
    }
    if (audioView) {
        audioView.id = 'review-audio-workbench';
        audioView.hidden = mode !== 'audio';
        audioView.setAttribute('aria-hidden', audioView.hidden ? 'true' : 'false');
    }
    page?.setAttribute('data-review-view', mode);
    document.body.dataset.reviewView = mode;
    page?.querySelectorAll('.review-view-tab').forEach(button => {
        const selected = button.dataset.reviewView === mode;
        button.setAttribute('aria-selected', selected ? 'true' : 'false');
        button.tabIndex = selected ? 0 : -1;
    });
    const description = page?.querySelector('.workspace-heading p');
    if (description) {
        description.textContent = mode === 'document'
            ? '先看文档原文和题型字段，再确认是否适合进入录入流程。'
            : '核对每条音频对应的正文、来源、音色和当前绑定状态。';
    }
    const reviewTitle = $('review-title');
    if (reviewTitle) reviewTitle.textContent = mode === 'audio' ? '音频核对' : '核对解析内容';
    const listTitle = $('review-items-title');
    if (listTitle) listTitle.textContent = mode === 'audio' ? '音频核对' : '解析出的内容';
    const listDescription = $('review-list-description');
    if (listDescription) {
        listDescription.textContent = mode === 'audio'
            ? '按文档顺序检查正文，确认后进入配置中心。'
            : '按原文顺序查看题型、题号和录入字段。';
    }
    return mode;
}

function reviewPageInputForItem(item) {
    const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
    const value = metadata.page_input || item?.page_input;
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

function reviewRecordRetellingQuestionNumber(pageInput) {
    const retelling = pageInput?.retelling && typeof pageInput.retelling === 'object'
        ? pageInput.retelling
        : null;
    const explicit = reviewDisplayFactValue(
        retelling?.number
        ?? retelling?.question_number
        ?? retelling?.questionNumber,
    );
    if (explicit) return explicit;

    const recordingQuestions = Array.isArray(pageInput?.recording?.questions)
        ? pageInput.recording.questions
        : [];
    const numericNumbers = recordingQuestions
        .map(question => Number(question?.number))
        .filter(number => Number.isFinite(number));
    if (numericNumbers.length) return String(Math.max(...numericNumbers) + 1);
    return String(recordingQuestions.length + 1);
}

function reviewPageQuestionList(pageInput) {
    if (!pageInput || typeof pageInput !== 'object') return [];
    if (pageInput.type === '听后选择') {
        return (Array.isArray(pageInput.materials) ? pageInput.materials : [])
            .flatMap(material => Array.isArray(material?.questions) ? material.questions : []);
    }
    if (pageInput.type === '信息获取') {
        return (Array.isArray(pageInput.materials) ? pageInput.materials : [])
            .flatMap(material => Array.isArray(material?.questions) ? material.questions : []);
    }
    if (pageInput.type === '听后记录并转述信息') {
        const questions = Array.isArray(pageInput.recording?.questions)
            ? pageInput.recording.questions.slice()
            : [];
        if (pageInput.retelling && typeof pageInput.retelling === 'object') {
            questions.push({
                ...pageInput.retelling,
                number: reviewRecordRetellingQuestionNumber(pageInput),
            });
        }
        return questions;
    }
    if (pageInput.type === '信息转述及询问') {
        const questions = [];
        if (pageInput.retelling && typeof pageInput.retelling === 'object') {
            questions.push({
                ...pageInput.retelling,
                number: pageInput.retelling.number ?? pageInput.retelling.question_number ?? '1',
            });
        }
        if (Array.isArray(pageInput.asking)) questions.push(...pageInput.asking);
        return questions;
    }
    return Array.isArray(pageInput.questions) ? pageInput.questions : [];
}

function reviewDocumentQuestionCountForItems(items) {
    return (Array.isArray(items) ? items : []).reduce((total, item) => {
        const pageInput = reviewPageInputForItem(item);
        if (!pageInput) return total + 1;
        const questions = reviewPageQuestionList(pageInput);
        return total + (questions.length || 1);
    }, 0);
}

function reviewDocumentItemsAreTextbook(items) {
    const list = Array.isArray(items) ? items : [];
    return list.length > 0 && list.every(reviewItemIsTextbook);
}

function reviewPageQuestionNumbers(pageInput) {
    return reviewPageQuestionList(pageInput)
        .map(question => reviewDisplayFactValue(question?.number))
        .filter(Boolean);
}

function reviewDocumentQuestionLabel(pageInput, fallbackIndex) {
    const numbers = reviewPageQuestionNumbers(pageInput);
    if (numbers.length === 1) return `第${numbers[0]}题`;
    if (numbers.length > 1) return `第${numbers[0]}–${numbers[numbers.length - 1]}题`;
    return `第${fallbackIndex + 1}条内容`;
}

function reviewDocumentItemLabel(item, index) {
    const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
    const number = reviewDisplayFactValue(item?.number ?? metadata.number);
    if (number) return `第${number}题`;
    return reviewDocumentQuestionLabel(reviewPageInputForItem(item), index);
}

function reviewDocumentItemLocator(item) {
    return reviewDisplayFactValue(
        item?.source_locator
        || item?.sourceLocator
        || item?.metadata?.source_locator,
    );
}

function reviewDocumentItemValue(item, keys) {
    const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
    for (const key of keys) {
        const value = item?.[key] ?? metadata[key];
        const display = reviewDisplayFactValue(value);
        if (display) return display;
    }
    return '';
}

function reviewScoreNumber(value) {
    const display = reviewDisplayFactValue(value);
    if (!display) return null;
    const normalized = display
        .replace(/[，,\s]/g, '')
        .replace(/分数?$/, '');
    const match = normalized.match(/-?\d+(?:\.\d+)?/);
    if (!match) return null;
    const score = Number(match[0]);
    return Number.isFinite(score) ? score : null;
}

function reviewScoreNumberText(value) {
    const score = reviewScoreNumber(value);
    if (score === null) return '';
    return Number.isInteger(score) ? String(score) : String(Number(score.toFixed(2)));
}

function reviewScoreText(value, label = '总分') {
    const score = reviewScoreNumberText(value);
    return score ? `${label} ${score} 分` : '';
}

function reviewQuestionScoreTotal(questions) {
    let total = 0;
    let hasScore = false;
    (Array.isArray(questions) ? questions : []).forEach(question => {
        const score = reviewScoreNumber(question?.score);
        if (score === null) return;
        total += score;
        hasScore = true;
    });
    return hasScore ? total : null;
}

function reviewRecordSectionScore(pageInput) {
    const sectionScores = pageInput?.section_scores || pageInput?.sectionScores;
    const explicit = sectionScores && typeof sectionScores === 'object'
        ? sectionScores['第一节听后记录']
            ?? sectionScores['第一节 听后记录']
            ?? sectionScores.recording
        : null;
    const explicitScore = reviewScoreNumber(explicit);
    if (explicitScore !== null) return explicitScore;
    return reviewQuestionScoreTotal(pageInput?.recording?.questions);
}

function reviewRetellingSectionScore(pageInput) {
    const retelling = pageInput?.retelling && typeof pageInput.retelling === 'object'
        ? pageInput.retelling
        : null;
    if (!retelling) return null;
    const sectionScores = pageInput?.section_scores || pageInput?.sectionScores;
    const explicit = sectionScores && typeof sectionScores === 'object'
        ? sectionScores['第二节信息转述']
            ?? sectionScores['第二节 转述信息']
            ?? sectionScores.retelling
        : null;
    const explicitScore = reviewScoreNumber(explicit);
    if (explicitScore !== null) return explicitScore;
    return reviewScoreNumber(retelling.score);
}

function reviewPageTotalScore(pageInput, item) {
    const pageScore = reviewScoreNumber(
        pageInput?.section_score
        ?? pageInput?.sectionScore
        ?? pageInput?.total_score
        ?? pageInput?.totalScore,
    );

    if (pageInput?.type === '听后记录并转述信息') {
        if (pageScore !== null) return pageScore;
        const recordingScore = reviewRecordSectionScore(pageInput);
        const retellingScore = reviewRetellingSectionScore(pageInput);
        const total = (recordingScore ?? 0) + (retellingScore ?? 0);
        return recordingScore !== null || retellingScore !== null ? total : null;
    }
    if (pageInput?.type === '信息转述及询问') {
        if (pageScore !== null) return pageScore;
        const retellingScore = reviewRetellingSectionScore(pageInput);
        const askingScore = reviewQuestionScoreTotal(pageInput.asking);
        const total = (retellingScore ?? 0) + (askingScore ?? 0);
        return retellingScore !== null || askingScore !== null ? total : null;
    }
    const itemScore = reviewScoreNumber(reviewDocumentItemValue(item, ['score']));
    if (itemScore !== null) return itemScore;
    if (pageScore !== null) return pageScore;
    return reviewQuestionScoreTotal(reviewPageQuestionList(pageInput));
}

function reviewTypeGroupTotalScore(typeGroup) {
    let total = 0;
    let hasScore = false;
    (Array.isArray(typeGroup?.items) ? typeGroup.items : []).forEach(item => {
        const score = reviewPageTotalScore(reviewPageInputForItem(item), item);
        if (score === null) return;
        total += score;
        hasScore = true;
    });
    return hasScore ? total : null;
}

function reviewItemIsTextbook(item) {
    const metadata = item?.metadata && typeof item.metadata === 'object'
        ? item.metadata
        : {};
    const values = [
        item?.doc_type,
        metadata.doc_type,
        metadata.document_type,
        item?.input_type,
        metadata.input_type,
    ]
        .map(value => String(value || '').trim().toLowerCase())
        .filter(Boolean);
    return values.some(value => (
        value === '课文跟读'
        || value === 'text_reading'
        || value.includes('课文')
        || value.includes('textbook')
    ));
}

function reviewTypeGroupIsTextbook(typeGroup) {
    const items = Array.isArray(typeGroup?.items) ? typeGroup.items : [];
    if (items.length > 0) return items.every(reviewItemIsTextbook);
    const name = String(typeGroup?.name || '').trim().toLowerCase();
    return name === '课文跟读' || name === 'text_reading' || name.includes('课文');
}

function syncReviewDocumentSelection(index, sectionKey = '', questionNumber = '') {
    const rawIndex = index === null || index === undefined
        || (typeof index === 'string' && !index.trim())
        ? null
        : Number(index);
    const selectedIndex = Number.isInteger(rawIndex) && rawIndex >= 0 ? rawIndex : null;
    $$('.review-document-item').forEach(item => {
        const selected = selectedIndex !== null && Number(item.dataset.reviewIndex) === selectedIndex;
        item.classList.toggle('is-selected', selected);
        item.setAttribute('aria-current', selected ? 'true' : 'false');
    });
    $$('.review-nav-number').forEach(button => {
        const buttonSection = button.dataset.reviewSection;
        const buttonQuestion = button.dataset.reviewQuestion;
        const selected = selectedIndex !== null
            && Number(button.dataset.reviewIndex) === selectedIndex
            && (!buttonSection
                ? true
                : buttonSection === sectionKey
                    && (!buttonQuestion || buttonQuestion === String(questionNumber)));
        button.classList.toggle('is-active', selected);
        button.setAttribute('aria-current', selected ? 'true' : 'false');
    });
    if (selectedIndex !== null) {
        const activeButton = $('review-document-nav')?.querySelector('.review-nav-number.is-active');
        if (activeButton) activeButton.scrollIntoView({ block: 'nearest' });
    }
}

function reviewChineseOrdinal(number) {
    const numerals = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];
    const value = Math.max(1, Math.floor(Number(number) || 1));
    if (value <= 10) return numerals[value - 1];
    if (value < 20) return `十${numerals[value - 11]}`;
    if (value < 100) {
        const tens = Math.floor(value / 10);
        const ones = value % 10;
        return `${numerals[tens - 1]}十${ones ? numerals[ones - 1] : ''}`;
    }
    return String(value);
}

function buildReviewTypeGroups(items) {
    const groups = [];
    const groupsByName = new Map();
    (Array.isArray(items) ? items : []).forEach(item => {
        const typePath = reviewTypePathForItem(item, String(item?.doc_type || ''));
        const name = reviewTypeLabel(typePath[0] || item?.doc_type || '未分类') || '未分类';
        let typeGroup = groupsByName.get(name);
        if (!typeGroup) {
            typeGroup = { name, items: [] };
            groupsByName.set(name, typeGroup);
            groups.push(typeGroup);
        }
        typeGroup.items.push(item);
    });
    return groups;
}

function reviewNavItemNumber(item, indexInGroup) {
    const metadata = item?.metadata && typeof item.metadata === 'object' ? item.metadata : {};
    const display = reviewDisplayFactValue(item?.number ?? metadata.number);
    if (display) return display.slice(0, 4);
    const numbers = reviewPageQuestionNumbers(reviewPageInputForItem(item));
    if (numbers.length) return numbers.length > 1 ? `${numbers[0]}–${numbers[numbers.length - 1]}` : numbers[0];
    return String(indexInGroup + 1);
}

function focusReviewDocumentItem(item, reviewIndex, sectionKey = '', questionNumber = '') {
    const targetIndex = Number(reviewIndex);
    if (!Number.isInteger(targetIndex) || targetIndex < 0) return;
    reviewSelectedIndex = targetIndex;
    reviewSelectedItemId = item?.item_id ? String(item.item_id) : '';
    syncReviewDocumentSelection(targetIndex, sectionKey, questionNumber);
    syncReviewOutlineSelection(targetIndex);
    renderReviewInspector(item, targetIndex);
    const itemTarget = $(`review-document-item-${targetIndex}`);
    const target = sectionKey
        ? itemTarget?.querySelector(`[data-review-document-section="${sectionKey}"]`) || itemTarget
        : itemTarget;
    if (!target) return;
    const reducedMotion = typeof window.matchMedia === 'function'
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    target.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'center' });
}

function reviewInfoAcquisitionSectionMeta(sectionValue, material = null) {
    const explicit = reviewDisplayFactValue(sectionValue);
    const compact = explicit.replace(/\s+/g, '').toLowerCase();
    if (compact.includes('听选信息')) {
        return { key: 'info-selection', index: '第一节', title: '听选信息' };
    }
    if (compact.includes('回答问题')) {
        return { key: 'info-response', index: '第二节', title: '回答问题' };
    }
    if (explicit) {
        return { key: 'info-other', index: '小节', title: explicit };
    }
    const questions = Array.isArray(material?.questions) ? material.questions : [];
    const isSelection = questions.some(question => (
        Array.isArray(question?.options) && question.options.length > 0
    ));
    return isSelection
        ? { key: 'info-selection', index: '第一节', title: '听选信息' }
        : { key: 'info-response', index: '第二节', title: '回答问题' };
}

function reviewInfoAcquisitionNavSections(typeGroup) {
    const models = new Map();
    const entries = Array.isArray(typeGroup?.items) ? typeGroup.items : [];
    entries.forEach((item, itemIndex) => {
        const pageInput = reviewPageInputForItem(item);
        const materials = Array.isArray(pageInput?.materials) ? pageInput.materials : [];
        const material = materials[0] || null;
        const meta = reviewInfoAcquisitionSectionMeta(material?.section, material);
        let model = models.get(meta.key);
        if (!model) {
            model = {
                ...meta,
                items: [],
                scoreTotal: 0,
                hasScore: false,
            };
            models.set(meta.key, model);
        }
        const reviewIndex = reviewOutlineItemIndex(item) ?? itemIndex;
        model.items.push({
            number: reviewNavItemNumber(item, itemIndex),
            label: reviewDocumentItemLabel(item, itemIndex),
            item,
            reviewIndex,
        });
        const score = reviewPageTotalScore(pageInput, item);
        if (score !== null) {
            model.scoreTotal += score;
            model.hasScore = true;
        }
    });
    return Array.from(models.values()).map(model => ({
        ...model,
        score: model.hasScore ? model.scoreTotal : null,
    }));
}

function reviewRecordRetellingNavSections(typeGroup) {
    const entries = Array.isArray(typeGroup?.items) ? typeGroup.items : [];
    let pageEntry = null;
    for (let itemIndex = 0; itemIndex < entries.length; itemIndex += 1) {
        const item = entries[itemIndex];
        const pageInput = reviewPageInputForItem(item);
        if (!['听后记录并转述信息', '信息转述及询问'].includes(pageInput?.type)) continue;
        pageEntry = { item, itemIndex, pageInput };
        break;
    }
    if (!pageEntry) return [];

    const reviewIndex = reviewOutlineItemIndex(pageEntry.item) ?? pageEntry.itemIndex;
    if (pageEntry.pageInput.type === '信息转述及询问') {
        const retelling = pageEntry.pageInput.retelling && typeof pageEntry.pageInput.retelling === 'object'
            ? pageEntry.pageInput.retelling
            : null;
        const asking = Array.isArray(pageEntry.pageInput.asking) ? pageEntry.pageInput.asking : [];
        return [
            {
                key: 'retelling',
                index: '第一节',
                title: '信息转述',
                score: reviewRetellingSectionScore(pageEntry.pageInput),
                items: retelling
                    ? [{
                        number: reviewDisplayFactValue(
                            retelling.number ?? retelling.question_number,
                            '1',
                        ),
                        label: '第1题',
                        item: pageEntry.item,
                        reviewIndex,
                    }]
                    : [],
            },
            {
                key: 'asking',
                index: '第二节',
                title: '询问信息',
                score: reviewQuestionScoreTotal(asking),
                items: asking.map((question, questionIndex) => {
                    const number = reviewDisplayFactValue(question?.number, String(questionIndex + 1));
                    return {
                        number,
                        label: `第${number}题`,
                        item: pageEntry.item,
                        reviewIndex,
                    };
                }),
            },
        ];
    }
    const recordingQuestions = Array.isArray(pageEntry.pageInput.recording?.questions)
        ? pageEntry.pageInput.recording.questions
        : [];
    const sections = [{
        key: 'recording',
        index: '第一节',
        title: '听后记录',
        score: reviewRecordSectionScore(pageEntry.pageInput),
        items: recordingQuestions.map((question, questionIndex) => {
            const number = reviewDisplayFactValue(question?.number, String(questionIndex + 1));
            return {
                number,
                label: `第${number}题`,
                item: pageEntry.item,
                reviewIndex,
            };
        }),
    }];

    const retelling = pageEntry.pageInput.retelling && typeof pageEntry.pageInput.retelling === 'object'
        ? pageEntry.pageInput.retelling
        : null;
    const retellingNumber = retelling ? reviewRecordRetellingQuestionNumber(pageEntry.pageInput) : '';
    sections.push({
        key: 'retelling',
        index: '第二节',
        title: '转述信息',
        score: reviewRetellingSectionScore(pageEntry.pageInput),
        items: retelling
            ? [{
                number: retellingNumber,
                label: `第${retellingNumber}题`,
                item: pageEntry.item,
                reviewIndex,
            }]
            : [],
    });
    return sections;
}

function renderReviewDocumentNav(models, presentation, nav) {
    const multiUnit = models.length > 1;
    const unitMeasure = presentation?.inputType === 'paper' ? '套' : '单元';
    const fragment = document.createDocumentFragment();
    models.forEach((unit, unitIndex) => {
        const typeGroups = buildReviewTypeGroups(unit.items);
        if (!typeGroups.length) return;
        const section = document.createElement('section');
        section.className = 'review-nav-unit';
        if (multiUnit) {
            const ordinal = `第${reviewChineseOrdinal(unitIndex + 1)}${unitMeasure}`;
            const unitTitle = document.createElement('div');
            unitTitle.className = 'review-nav-unit-title';
            unitTitle.textContent = unit.label && unit.label !== ordinal
                ? `${ordinal} · ${unit.label}`
                : ordinal;
            section.appendChild(unitTitle);
        }
        typeGroups.forEach((typeGroup, groupIndex) => {
            const group = document.createElement('div');
            group.className = 'review-nav-group';
            const isRecordRetelling = [
                '听后记录并转述信息',
                '信息转述及询问',
            ].includes(reviewTypeKey(typeGroup.name));
            const isInfoAcquisition = reviewTypeKey(typeGroup.name) === '信息获取';
            if (isRecordRetelling) group.classList.add('is-record-retelling');
            if (isInfoAcquisition) group.classList.add('is-info-acquisition');
            const groupTitle = document.createElement('div');
            groupTitle.className = 'review-nav-group-title';
            const groupTitleText = document.createElement('span');
            groupTitleText.className = 'review-nav-group-title-text';
            groupTitleText.textContent = `${reviewChineseOrdinal(groupIndex + 1)}、${typeGroup.name}${isRecordRetelling ? '。' : ''}`;
            groupTitle.appendChild(groupTitleText);
            // 课文没有分值概念，导航组不展示总分。
            const groupScoreText = reviewTypeGroupIsTextbook(typeGroup)
                ? ''
                : reviewScoreText(reviewTypeGroupTotalScore(typeGroup));
            if (groupScoreText) {
                const groupScore = document.createElement('small');
                groupScore.className = 'review-nav-group-score';
                groupScore.textContent = groupScoreText;
                groupTitle.appendChild(groupScore);
            }
            const recordSections = isRecordRetelling
                ? reviewRecordRetellingNavSections(typeGroup)
                : isInfoAcquisition
                    ? reviewInfoAcquisitionNavSections(typeGroup)
                    : [];
            if (recordSections.length) {
                recordSections.forEach(sectionModel => {
                    const subsection = document.createElement('div');
                    subsection.className = 'review-nav-subgroup';
                    const subsectionTitle = document.createElement('div');
                    subsectionTitle.className = 'review-nav-subgroup-title';
                    const subsectionIndex = document.createElement('span');
                    subsectionIndex.textContent = sectionModel.index;
                    const subsectionName = document.createElement('strong');
                    subsectionName.textContent = sectionModel.title;
                    subsectionTitle.append(subsectionIndex, subsectionName);
                    const subsectionScoreText = reviewScoreText(sectionModel.score);
                    if (subsectionScoreText) {
                        const subsectionScore = document.createElement('small');
                        subsectionScore.className = 'review-nav-subgroup-score';
                        subsectionScore.textContent = subsectionScoreText;
                        subsectionTitle.appendChild(subsectionScore);
                    }
                    const numbers = document.createElement('div');
                    numbers.className = 'review-nav-numbers';
                    sectionModel.items.forEach(navItem => {
                        const number = document.createElement('button');
                        number.type = 'button';
                        number.className = 'review-nav-number';
                        number.dataset.reviewIndex = String(navItem.reviewIndex);
                        number.dataset.reviewSection = sectionModel.key;
                        number.dataset.reviewQuestion = String(navItem.number);
                        number.textContent = navItem.number;
                        number.setAttribute(
                            'aria-label',
                            `跳转到${sectionModel.index}${sectionModel.title}：${navItem.label}`,
                        );
                        number.title = navItem.label;
                        number.addEventListener('click', () => {
                            focusReviewDocumentItem(
                                navItem.item,
                                navItem.reviewIndex,
                                sectionModel.key,
                                navItem.number,
                            );
                        });
                        numbers.appendChild(number);
                    });
                    subsection.append(subsectionTitle, numbers);
                    group.appendChild(subsection);
                });
            } else {
                const numbers = document.createElement('div');
                numbers.className = 'review-nav-numbers';
                typeGroup.items.forEach((item, itemIndex) => {
                    const reviewIndex = reviewOutlineItemIndex(item) ?? itemIndex;
                    const number = document.createElement('button');
                    number.type = 'button';
                    number.className = 'review-nav-number';
                    number.dataset.reviewIndex = String(reviewIndex);
                    number.textContent = reviewNavItemNumber(item, itemIndex);
                    number.setAttribute(
                        'aria-label',
                        `跳转到${typeGroup.name}：${reviewDocumentItemLabel(item, itemIndex)}`,
                    );
                    number.title = reviewDocumentItemLabel(item, itemIndex);
                    number.addEventListener('click', () => { focusReviewDocumentItem(item, reviewIndex); });
                    numbers.appendChild(number);
                });
                group.appendChild(numbers);
            }
            group.prepend(groupTitle);
            section.appendChild(group);
        });
        fragment.appendChild(section);
    });
    nav.hidden = fragment.childElementCount === 0;
    nav.replaceChildren(fragment);
}

function reviewDocumentTextBlock(
    parent,
    label,
    text,
    className = 'review-document-script',
    sourceText = '',
) {
    const value = reviewPageText(text, sourceText);
    if (!value) return null;
    const section = document.createElement('section');
    section.className = `${className}-block`;
    const labelElement = document.createElement('span');
    labelElement.className = 'review-document-field-label';
    labelElement.textContent = label;
    const body = document.createElement('p');
    body.className = className;
    body.textContent = value;
    section.append(labelElement, body);
    parent.appendChild(section);
    return section;
}

// The parser keeps speaker markers in the raw TTS text so the audio pipeline
// can route voices without reading them aloud. The page-facing representation
// removes only (W)/(M); W:/M: are intentional system-input content.
function reviewPageText(value, sourceValue = '') {
    const pageText = reviewDisplayFactValue(value)
        .replace(REVIEW_PAGE_PAREN_SPEAKER_MARKER_RE, '')
        .trim();
    const sourceText = reviewDisplayFactValue(sourceValue)
        .replace(REVIEW_PAGE_PAREN_SPEAKER_MARKER_RE, '')
        .trim();
    if (!pageText) return sourceText;
    const legacyPageText = sourceText
        .replace(REVIEW_PAGE_COLON_SPEAKER_MARKER_RE, '')
        .trim();
    return legacyPageText !== sourceText && legacyPageText === pageText
        ? sourceText
        : pageText;
}

function reviewDocumentScore(question, item) {
    const questionScore = reviewDisplayFactValue(question?.score);
    if (questionScore) return reviewScoreNumberText(questionScore) || questionScore;

    // An item-level score is the page total for grouped questions (for
    // example, a two-question listening passage). Never repeat that total on
    // every question when the parser did not provide question-level scores.
    // It is safe to use it as the question score only for a single-question
    // page, such as imitation reading.
    const pageQuestions = reviewPageQuestionList(reviewPageInputForItem(item));
    if (pageQuestions.length <= 1) {
        const itemScore = reviewDocumentItemValue(item, ['score']);
        return reviewScoreNumberText(itemScore) || itemScore;
    }
    return '';
}

function reviewDocumentOptionMatchesAnswer(option, answer) {
    const answerText = reviewDisplayFactValue(answer).toLowerCase();
    if (!answerText) return false;
    return answerText === reviewDisplayFactValue(option?.option_id).toLowerCase()
        || answerText === reviewDisplayFactValue(option?.text).toLowerCase();
}

function reviewDocumentQuestionPrompt(question, options, sourceText = '') {
    return reviewPageText(
        question?.prompt || question?.listening_text,
        sourceText,
    );
}

function renderReviewDocumentOptions(parent, options, answer, label = '选项') {
    if (!Array.isArray(options) || !options.length) return false;
    const labelElement = document.createElement('span');
    labelElement.className = 'review-document-field-label review-document-options-label';
    labelElement.textContent = label;
    const list = document.createElement('ul');
    list.className = 'review-document-options';
    options.forEach(option => {
        const row = document.createElement('li');
        const isAnswer = reviewDocumentOptionMatchesAnswer(option, answer);
        row.className = `review-document-option${isAnswer ? ' is-answer' : ''}`;
        const optionId = document.createElement('span');
        optionId.className = 'review-document-option-id';
        optionId.textContent = reviewDisplayFactValue(option?.option_id, '—');
        const optionText = document.createElement('span');
        optionText.textContent = reviewDisplayFactValue(option?.text, '未提供');
        row.append(optionId, optionText);
        if (isAnswer) {
            const answerMarker = document.createElement('span');
            answerMarker.className = 'review-document-option-answer';
            answerMarker.textContent = '正确答案';
            row.appendChild(answerMarker);
        }
        list.appendChild(row);
    });
    parent.append(labelElement, list);
    return true;
}

function reviewDocumentQuestion(
    parent,
    question,
    item,
    {
        showPrompt = true,
        showHeading = true,
        promptLabel = '题目',
        optionsLabel = '选项',
        splitReferenceAnswers = false,
        preferReferenceAnswers = false,
    } = {},
) {
    if (!question || typeof question !== 'object') return;
    const row = document.createElement('div');
    row.className = 'review-document-question';
    const heading = document.createElement('div');
    heading.className = 'review-document-question-heading';
    const number = document.createElement('strong');
    const numberValue = reviewDisplayFactValue(question.number);
    number.textContent = numberValue ? `第${numberValue}题` : '题目';
    heading.appendChild(number);
    const score = reviewDocumentScore(question, item);
    if (score) {
        const scoreBadge = document.createElement('span');
        scoreBadge.className = 'review-document-score';
        scoreBadge.textContent = `${score} 分`;
        heading.appendChild(scoreBadge);
    }
    if (showHeading) row.appendChild(heading);
    const options = Array.isArray(question.options) ? question.options : [];
    if (showPrompt) {
        reviewDocumentTextBlock(
            row,
            promptLabel,
            reviewDocumentQuestionPrompt(question, options, reviewContentForItem(item)),
            'review-document-prompt',
        );
    }
    const references = question.reference_answers ?? question.reference_answer;
    const referenceValues = reviewReferenceAnswerValues(references, {
        splitSlash: splitReferenceAnswers,
    });
    const referenceSource = reviewDisplayFactValue(question.reference_answers_source).toLowerCase();
    const useReferenceAnswers = referenceValues.length > 0 && (
        referenceSource === 'document'
        || (
            preferReferenceAnswers
            && referenceSource !== 'red_option'
            && referenceValues.length > 1
        )
    );
    const optionsRendered = !useReferenceAnswers
        && renderReviewDocumentOptions(row, options, question.answer, optionsLabel);
    if (useReferenceAnswers) {
        renderReviewReferenceAnswers(row, references, '参考答案', {
            splitSlash: splitReferenceAnswers,
        });
    }
    const answer = reviewDisplayFactValue(question.answer);
    const answerOption = options.find(option => reviewDocumentOptionMatchesAnswer(option, question.answer));
    if (!useReferenceAnswers && answerOption && !optionsRendered) {
        const answerLine = document.createElement('p');
        answerLine.className = 'review-document-answer-line';
        answerLine.textContent = `正确答案：${reviewDisplayFactValue(answerOption.text, '未提供')}`;
        row.appendChild(answerLine);
    } else if (!useReferenceAnswers && answer && !optionsRendered) {
        const answerLine = document.createElement('p');
        answerLine.className = 'review-document-answer-line';
        answerLine.textContent = `参考答案：${answer}`;
        row.appendChild(answerLine);
    } else if (!useReferenceAnswers && options.length && !answerOption && !answer) {
        const answerLine = document.createElement('p');
        answerLine.className = 'review-document-answer-line is-missing';
        answerLine.textContent = '正确答案：未识别';
        row.appendChild(answerLine);
    }
    if (!useReferenceAnswers && !answer && references) {
        renderReviewReferenceAnswers(row, references, '参考答案', { splitSlash: splitReferenceAnswers });
    }
    const answerTime = reviewDisplayFactValue(question.answer_time);
    if (answerTime) {
        const meta = document.createElement('p');
        meta.className = 'review-document-question-meta';
        meta.textContent = `作答时长 ${answerTime} 秒`;
        row.appendChild(meta);
    }
    parent.appendChild(row);
}

function reviewDocumentImageArtifactId(item) {
    const pageInput = reviewPageInputForItem(item);
    return reviewDisplayFactValue(
        pageInput?.recording?.image_artifact_id
        || pageInput?.recording?.table_image_artifact_id,
    );
}

function reviewArtifactReader() {
    const module = root.WORDTTS_RENDERER?.getModule?.('delivery.artifacts');
    return module?.readArtifactBytes || root.readArtifactBytes || null;
}

function reviewImageUrlForArtifact(artifactId) {
    const key = String(artifactId || '').trim();
    if (!key) return Promise.reject(new Error('表格图片产物标识缺失'));
    const cached = reviewImagePreviewCache.get(key);
    if (cached?.url) return Promise.resolve(cached.url);
    if (cached?.promise) return cached.promise;
    const reader = reviewArtifactReader();
    if (typeof reader !== 'function') return Promise.reject(new Error('图片读取服务尚未准备好'));
    const promise = Promise.resolve(reader(key)).then(bytes => {
        if (!bytes || !bytes.byteLength) throw new Error('表格图片内容为空');
        if (typeof Blob !== 'function'
            || typeof URL === 'undefined'
            || typeof URL.createObjectURL !== 'function') {
            throw new Error('当前环境不支持图片预览');
        }
        const url = URL.createObjectURL(new Blob([bytes], { type: 'image/png' }));
        reviewImagePreviewCache.set(key, { url });
        // Do not revoke the URL currently shown in the modal.  The renderer
        // can finish hydrating several document cards asynchronously, so a
        // plain FIFO eviction could otherwise blank an image the user is
        // actively viewing.
        while (reviewImagePreviewCache.size > 12) {
            let evicted = false;
            for (const [candidateKey, entry] of reviewImagePreviewCache) {
                if (entry?.url && entry.url === reviewImageDialogState.url) continue;
                if (!entry?.url) continue;
                if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(entry.url);
                reviewImagePreviewCache.delete(candidateKey);
                evicted = true;
                break;
            }
            if (!evicted) break;
        }
        return url;
    }).catch(error => {
        reviewImagePreviewCache.delete(key);
        throw error;
    });
    reviewImagePreviewCache.set(key, { promise });
    return promise;
}

function ensureReviewImageDialog() {
    const dialog = $('review-image-dialog');
    if (!dialog || reviewImageDialogBound) return dialog;
    const close = dialog.querySelector('[data-review-image-action="close"]');
    close?.addEventListener('click', event => {
        // Keep the explicit close action and the Escape/backdrop paths on the
        // same cleanup routine so no stale preview state can survive.
        event.preventDefault();
        closeReviewImageDialog();
    });
    dialog.querySelectorAll('[data-review-image-action]').forEach(button => {
        const action = button.dataset.reviewImageAction;
        if (action === 'close') return;
        button.addEventListener('click', () => {
            if (action === 'zoom-in') setReviewImageZoom(reviewImageDialogState.scale + .25);
            if (action === 'zoom-out') setReviewImageZoom(reviewImageDialogState.scale - .25);
            if (action === 'reset') setReviewImageZoom(1);
        });
    });
    dialog.addEventListener('click', event => {
        if (event.target === dialog) closeReviewImageDialog();
    });
    dialog.addEventListener('cancel', event => {
        event.preventDefault();
        closeReviewImageDialog();
    });
    dialog.addEventListener('keydown', event => {
        if (!['Escape', 'Esc'].includes(event.key)) return;
        event.preventDefault();
        closeReviewImageDialog();
    });
    // Electron may route Escape through the native modal layer without
    // dispatching the event to the dialog itself. Capture it at document level
    // as a final fallback so the preview can never leave a dead modal behind.
    document.addEventListener('keydown', event => {
        const dialogIsVisible = Boolean(dialog.open || reviewImageDialogState.url);
        if (!['Escape', 'Esc'].includes(event.key) || !dialogIsVisible) return;
        event.preventDefault();
        event.stopPropagation();
        closeReviewImageDialog();
    }, true);
    dialog.addEventListener('close', () => {
        restoreReviewImageDialogFocus();
    });
    reviewImageDialogBound = true;
    return dialog;
}

function restoreReviewImageDialogFocus() {
    const returnFocus = reviewImageDialogState.returnFocus;
    reviewImageDialogState = { returnFocus: null, scale: 1, url: null };
    if (returnFocus && typeof returnFocus.focus === 'function' && document.contains(returnFocus)) {
        returnFocus.focus({ preventScroll: true });
    }
}

function setReviewImageZoom(value) {
    const dialog = ensureReviewImageDialog();
    if (!dialog) return;
    const scale = Math.max(.5, Math.min(3, Number(value) || 1));
    reviewImageDialogState.scale = scale;
    const image = $('review-image-dialog-image');
    if (image) image.style.transform = `scale(${scale})`;
    const readout = $('review-image-dialog-zoom');
    if (readout) readout.textContent = `${Math.round(scale * 100)}%`;
    dialog.querySelector('[data-review-image-action="zoom-out"]')?.toggleAttribute('disabled', scale <= .5);
    dialog.querySelector('[data-review-image-action="zoom-in"]')?.toggleAttribute('disabled', scale >= 3);
}

function openReviewImageDialog(url, label, returnFocus = null) {
    const dialog = ensureReviewImageDialog();
    if (!dialog || !url) return;
    if (dialog.open && typeof dialog.close === 'function') dialog.close();
    dialog.hidden = false;
    const image = $('review-image-dialog-image');
    if (image) {
        image.src = url;
        image.alt = label || '表格图片放大预览';
    }
    const caption = $('review-image-dialog-caption');
    if (caption) caption.textContent = label || '听后记录表格截取';
    reviewImageDialogState.returnFocus = returnFocus;
    reviewImageDialogState.url = url;
    setReviewImageZoom(1);
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.hidden = false;
    requestAnimationFrame(() => dialog.querySelector('[data-review-image-action="close"]')?.focus());
}

function closeReviewImageDialog() {
    const dialog = $('review-image-dialog');
    if (!dialog) return;
    if (typeof dialog.close === 'function' && dialog.open) {
        try {
            dialog.close();
        } catch (_error) {
            // Fall through to the attribute-based fallback below.
        }
    }
    // `close()` dispatches a close event in Chromium, but the renderer also
    // runs in environments where <dialog> is only partially implemented.
    // Always hide and clear the state so a failed native close cannot leave a
    // stale backdrop or a dead return-focus target behind.
    dialog.removeAttribute('open');
    dialog.hidden = true;
    if (reviewImageDialogState.returnFocus || reviewImageDialogState.url) restoreReviewImageDialogFocus();
}

function createReviewImagePreview(
    item,
    artifactId,
    {
        label: mediaLabel = '听后记录表格截取',
        note: mediaNote = '录入时会把这张图片作为页面内容提交。',
        alt: mediaAlt = mediaLabel,
    } = {},
) {
    const figure = document.createElement('figure');
    figure.className = 'review-document-media';
    const caption = document.createElement('figcaption');
    caption.className = 'review-document-media-caption';
    const copy = document.createElement('div');
    const label = document.createElement('strong');
    label.textContent = mediaLabel;
    const note = document.createElement('small');
    note.textContent = mediaNote;
    copy.append(label, note);
    const openButton = document.createElement('button');
    openButton.type = 'button';
    openButton.className = 'btn-secondary btn-sm review-document-media-open';
    openButton.textContent = '放大查看';
    openButton.disabled = true;
    caption.append(copy, openButton);
    const frame = document.createElement('div');
    frame.className = 'review-document-media-frame';
    const placeholder = document.createElement('span');
    placeholder.className = 'review-document-media-placeholder';
    placeholder.textContent = `正在读取${mediaLabel}…`;
    const image = document.createElement('img');
    image.className = 'review-document-media-image';
    image.alt = mediaAlt;
    image.setAttribute('role', 'button');
    image.setAttribute('aria-label', `点击查看${mediaLabel}大图`);
    image.tabIndex = 0;
    image.dataset.reviewImageTrigger = 'true';
    image.title = '点击查看大图';
    image.hidden = true;
    frame.append(placeholder, image);
    figure.append(caption, frame);
    let imageUrl = '';
    const openPreview = event => {
        if (event?.type === 'keydown' && !['Enter', ' '].includes(event.key)) return;
        event?.preventDefault();
        if (imageUrl) openReviewImageDialog(imageUrl, mediaLabel, event?.currentTarget || openButton);
    };
    image.addEventListener('click', openPreview);
    image.addEventListener('keydown', openPreview);
    openButton.addEventListener('click', event => {
        event.stopPropagation();
        if (imageUrl) openReviewImageDialog(imageUrl, mediaLabel, openButton);
    });
    return {
        figure,
        hydrate: async () => {
            try {
                const url = await reviewImageUrlForArtifact(artifactId);
                if (!figure.isConnected) return;
                imageUrl = url;
                image.src = url;
                image.hidden = false;
                placeholder.hidden = true;
                openButton.disabled = false;
                openButton.title = `在大图窗口中查看${mediaLabel}`;
            } catch (error) {
                if (!figure.isConnected) return;
                placeholder.classList.add('is-error');
                placeholder.textContent = `${mediaLabel}暂时无法预览：${error?.message || '读取失败'}`;
            }
        },
    };
}

function reviewReferenceAnswerValues(references, { splitSlash = false } = {}) {
    const source = Array.isArray(references) ? references : [references];
    return source
        .flatMap(value => {
            const display = reviewPageText(value);
            if (!display) return [];
            return splitSlash ? display.split(REVIEW_PAGE_ANSWER_SPLIT_RE) : [display];
        })
        .map(value => value.trim())
        .filter(Boolean);
}

function renderReviewReferenceAnswers(
    parent,
    references,
    title = '转述参考答案',
    { splitSlash = false } = {},
) {
    const values = reviewReferenceAnswerValues(references, { splitSlash });
    if (!values.length) return null;
    const details = document.createElement('details');
    details.className = 'review-document-reference-details';
    // A long retelling document can carry 15 near-duplicate references.
    // Keep short answer sets open, but let long sets start compact so the
    // table image and both sections remain visible without excessive scroll.
    details.open = values.length <= 3;
    const summary = document.createElement('summary');
    const label = document.createElement('span');
    label.textContent = title;
    const count = document.createElement('small');
    count.textContent = `${values.length} 份 · 逐条`;
    summary.append(label, count);
    const list = document.createElement('ol');
    list.className = 'review-document-reference-list';
    values.forEach((value, index) => {
        const row = document.createElement('li');
        row.className = 'review-document-reference-item';
        const number = document.createElement('span');
        number.className = 'review-document-reference-number';
        number.textContent = String(index + 1).padStart(2, '0');
        const body = document.createElement('p');
        body.textContent = value;
        row.append(number, body);
        list.appendChild(row);
    });
    details.append(summary, list);
    parent.appendChild(details);
    return details;
}

function createReviewDocumentSubsection(parent, sectionKey, index, title, countText, score = null) {
    const section = document.createElement('section');
    section.className = `review-document-subsection review-document-subsection-${sectionKey}`;
    section.dataset.reviewDocumentSection = sectionKey;

    const heading = document.createElement('header');
    heading.className = 'review-document-subsection-heading';
    const copy = document.createElement('div');
    copy.className = 'review-document-subsection-copy';
    const indexElement = document.createElement('span');
    indexElement.className = 'review-document-subsection-index';
    indexElement.textContent = index;
    const titleElement = document.createElement('h5');
    titleElement.textContent = title;
    copy.append(indexElement, titleElement);
    const count = document.createElement('small');
    count.textContent = countText;
    const meta = document.createElement('div');
    meta.className = 'review-document-subsection-meta';
    meta.appendChild(count);
    const scoreText = reviewScoreText(score);
    if (scoreText) {
        const scoreElement = document.createElement('small');
        scoreElement.className = 'review-document-subsection-score';
        scoreElement.textContent = scoreText;
        meta.appendChild(scoreElement);
    }
    heading.append(copy, meta);

    const body = document.createElement('div');
    body.className = 'review-document-subsection-body';
    section.append(heading, body);
    parent.appendChild(section);
    return body;
}

function renderRecordFacts(parent, pageInput, item, imageTasks) {
    const recording = pageInput?.recording && typeof pageInput.recording === 'object'
        ? pageInput.recording
        : {};
    const questions = Array.isArray(recording.questions) ? recording.questions : [];
    const recordingBody = createReviewDocumentSubsection(
        parent,
        'recording',
        '第一节',
        '听后记录',
        questions.length ? `${questions.length} 小题` : '待补齐',
        reviewRecordSectionScore(pageInput),
    );
    reviewDocumentTextBlock(
        recordingBody,
        '听力原文',
        recording.listening_text || reviewContentForItem(item),
        'review-document-script',
        reviewContentForItem(item),
    );
    const imageArtifactId = reviewDisplayFactValue(
        recording.image_artifact_id || recording.table_image_artifact_id,
    );
    if (imageArtifactId) {
        const preview = createReviewImagePreview(item, imageArtifactId);
        recordingBody.appendChild(preview.figure);
        imageTasks.push(preview.hydrate);
    }
    if (questions.length) {
        const heading = document.createElement('h5');
        heading.className = 'review-document-subheading';
        heading.textContent = '填空答案';
        recordingBody.appendChild(heading);
        const list = document.createElement('div');
        list.className = 'review-document-answer-list';
        questions.forEach(question => {
            const row = document.createElement('div');
            row.className = 'review-document-answer-row';
            const questionNumber = document.createElement('strong');
            questionNumber.textContent = reviewDisplayFactValue(question?.number, '题目');
            const answers = [
                question?.answers,
                question?.reference_answers,
                question?.answer,
            ].find(value => (
                Array.isArray(value)
                    ? value.some(entry => Boolean(reviewDisplayFactValue(entry)))
                    : Boolean(reviewDisplayFactValue(value))
            ));
            const answer = document.createElement('div');
            answer.className = 'review-document-answer-values';
            const values = Array.isArray(answers) ? answers : [answers];
            values.map(value => reviewDisplayFactValue(value)).filter(Boolean).forEach(value => {
                const valueElement = document.createElement('span');
                valueElement.textContent = value;
                answer.appendChild(valueElement);
            });
            if (!answer.childElementCount) {
                const valueElement = document.createElement('span');
                valueElement.textContent = '未提供';
                answer.appendChild(valueElement);
            }
            row.append(questionNumber, answer);
            const score = reviewDocumentScore(question, item);
            if (score) {
                const scoreBadge = document.createElement('small');
                scoreBadge.textContent = `${score} 分`;
                row.appendChild(scoreBadge);
            }
            list.appendChild(row);
        });
        recordingBody.appendChild(list);
    }
    const retelling = pageInput?.retelling && typeof pageInput.retelling === 'object'
        ? pageInput.retelling
        : null;
    const retellingBody = createReviewDocumentSubsection(
        parent,
        'retelling',
        '第二节',
        '转述信息',
        retelling ? '1 小题' : '待补齐',
        reviewRetellingSectionScore(pageInput),
    );
    if (retelling) {
        reviewDocumentTextBlock(retellingBody, '转述题干', retelling.prompt, 'review-document-prompt');
        const references = Array.isArray(retelling.reference_answers) ? retelling.reference_answers : [];
        renderReviewReferenceAnswers(retellingBody, references);
        const time = reviewDisplayFactValue(retelling.answer_time);
        const meta = document.createElement('p');
        meta.className = 'review-document-retelling-meta';
        meta.textContent = [
            '听力原文与音频复用第一节',
            time ? `作答时长 ${time} 秒` : '',
        ].filter(Boolean).join(' · ');
        retellingBody.appendChild(meta);
    } else {
        const empty = document.createElement('p');
        empty.className = 'review-document-subsection-empty';
        empty.textContent = '暂未提取到转述题干和参考答案。';
        retellingBody.appendChild(empty);
    }
}

function renderInfoAcquisitionFacts(parent, pageInput, item) {
    const materials = Array.isArray(pageInput?.materials) ? pageInput.materials : [];
    if (!materials.length) {
        const body = createReviewDocumentSubsection(
            parent,
            'info-acquisition',
            '信息获取',
            '信息获取',
            '待补齐',
            null,
        );
        reviewDocumentTextBlock(body, '听力原文', reviewContentForItem(item));
        return;
    }

    const groups = [];
    materials.forEach(material => {
        const meta = reviewInfoAcquisitionSectionMeta(material?.section, material);
        let group = groups.find(candidate => candidate.meta.key === meta.key);
        if (!group) {
            group = { meta, materials: [] };
            groups.push(group);
        }
        group.materials.push(material);
    });

    groups.forEach(group => {
        const questions = group.materials.flatMap(material => (
            Array.isArray(material?.questions) ? material.questions : []
        ));
        const body = createReviewDocumentSubsection(
            parent,
            group.meta.key,
            group.meta.index,
            group.meta.title,
            `${group.materials.length} 段录音`,
            reviewQuestionScoreTotal(questions),
        );
        group.materials.forEach((material, materialIndex) => {
            if (group.materials.length > 1) {
                const heading = document.createElement('h5');
                heading.className = 'review-document-subheading';
                heading.textContent = `第${materialIndex + 1}段录音`;
                body.appendChild(heading);
            }
            reviewDocumentTextBlock(
                body,
                '听力原文',
                material?.listening_text || reviewContentForItem(item),
                'review-document-script',
                reviewContentForItem(item),
            );
            (Array.isArray(material?.questions) ? material.questions : []).forEach(question => {
                reviewDocumentQuestion(body, question, item, {
                    splitReferenceAnswers: true,
                    preferReferenceAnswers: true,
                });
            });
        });
    });
}

function renderListeningSelectionFacts(parent, pageInput, item) {
    const materials = Array.isArray(pageInput?.materials) ? pageInput.materials : [];
    if (!materials.length) {
        reviewDocumentTextBlock(
            parent,
            '听力原文',
            reviewContentForItem(item),
            'review-document-script',
            reviewContentForItem(item),
        );
        return;
    }

    materials.forEach((material, materialIndex) => {
        const questions = Array.isArray(material?.questions) ? material.questions : [];
        const section = document.createElement('section');
        section.className = 'review-document-material';
        section.dataset.reviewDocumentMaterial = String(materialIndex + 1);

        if (materials.length > 1) {
            const heading = document.createElement('header');
            heading.className = 'review-document-material-heading';
            const title = document.createElement('h5');
            title.textContent = `第${materialIndex + 1}段录音`;
            const meta = document.createElement('small');
            meta.textContent = `${questions.length} 小题`;
            heading.append(title, meta);
            section.appendChild(heading);
        }

        reviewDocumentTextBlock(
            section,
            '听力原文',
            material?.listening_text || reviewContentForItem(item),
            'review-document-script',
            reviewContentForItem(item),
        );
        questions.forEach(question => reviewDocumentQuestion(section, question, item, {
            optionsLabel: '选项',
        }));
        parent.appendChild(section);
    });
}

function renderInfoRetellingFacts(parent, pageInput, item, imageTasks) {
    const recording = pageInput?.recording && typeof pageInput.recording === 'object'
        ? pageInput.recording
        : {};
    const retelling = pageInput?.retelling && typeof pageInput.retelling === 'object'
        ? pageInput.retelling
        : null;
    const recordingBody = createReviewDocumentSubsection(
        parent,
        'retelling',
        '第一节',
        '信息转述',
        retelling ? '1 小题' : '待补齐',
        reviewRetellingSectionScore(pageInput),
    );
    reviewDocumentTextBlock(recordingBody, '题目指导文字', recording.instruction_text);
    reviewDocumentTextBlock(
        recordingBody,
        '听力原文',
        recording.listening_text || reviewContentForItem(item),
        'review-document-script',
        reviewContentForItem(item),
    );
    const imageArtifactId = reviewDisplayFactValue(
        recording.image_artifact_id || recording.table_image_artifact_id,
    );
    if (imageArtifactId) {
        const preview = createReviewImagePreview(item, imageArtifactId, {
            label: '信息转述思维导图',
            note: '录入时会把这张图片作为信息转述页面内容提交。',
            alt: '信息转述思维导图',
        });
        recordingBody.appendChild(preview.figure);
        imageTasks.push(preview.hydrate);
    }
    if (retelling) {
        reviewDocumentTextBlock(recordingBody, '题干', retelling.prompt, 'review-document-prompt');
        renderReviewReferenceAnswers(recordingBody, retelling.reference_answers);
    } else {
        const empty = document.createElement('p');
        empty.className = 'review-document-subsection-empty';
        empty.textContent = '暂未提取到转述题干和参考答案。';
        recordingBody.appendChild(empty);
    }

    const asking = Array.isArray(pageInput?.asking) ? pageInput.asking : [];
    const askingBody = createReviewDocumentSubsection(
        parent,
        'asking',
        '第二节',
        '询问信息',
        asking.length ? `${asking.length} 小题` : '待补齐',
        reviewQuestionScoreTotal(asking),
    );
    reviewDocumentTextBlock(
        askingBody,
        '题目指导文字',
        recording.asking_instruction_text,
    );
    asking.forEach(question => {
        reviewDocumentQuestion(askingBody, question, item, {
            promptLabel: '题干',
            splitReferenceAnswers: true,
        });
    });
    if (!asking.length) {
        const empty = document.createElement('p');
        empty.className = 'review-document-subsection-empty';
        empty.textContent = '暂未提取到询问信息题干和参考答案。';
        askingBody.appendChild(empty);
    }
}

function reviewImitationReferenceAnswersAllowed(item) {
    const metadata = item?.metadata && typeof item.metadata === 'object'
        ? item.metadata
        : {};
    // The three imitation-reading special-paper layouts can carry a passage
    // that looks like an answer, so neither the layout profile nor the mere
    // presence of stale reference_answers is sufficient.  The document-level
    // exam form is the only authority for showing a reference-answer row.
    const examForm = reviewDisplayFactValue(
        item?.exam_form ?? metadata.exam_form,
    ).toLowerCase();
    return examForm === 'paper';
}

function renderDocumentPageFacts(parent, pageInput, item, imageTasks) {
    if (!pageInput) {
        // 课文条目没有页面录入事实，正文就是朗读文本本身。
        reviewDocumentTextBlock(
            parent,
            reviewItemIsTextbook(item) ? '朗读文本' : '解析文本',
            reviewContentForItem(item),
        );
        return;
    }
    const type = pageInput.type;
    if (type === '听后记录并转述信息') {
        renderRecordFacts(parent, pageInput, item, imageTasks);
        return;
    }
    if (type === '信息获取') {
        renderInfoAcquisitionFacts(parent, pageInput, item);
        return;
    }
    if (type === '信息转述及询问') {
        renderInfoRetellingFacts(parent, pageInput, item, imageTasks);
        return;
    }
    const questions = reviewPageQuestionList(pageInput);
    if (type === '听后选择') {
        renderListeningSelectionFacts(parent, pageInput, item);
        return;
    }
    if (type === '听后应答') {
        questions.forEach(question => reviewDocumentQuestion(parent, question, item, {
            showHeading: false,
            promptLabel: '听力原文',
            optionsLabel: '应答语',
        }));
        if (!questions.length) reviewDocumentTextBlock(parent, '听力原文', reviewContentForItem(item));
        return;
    }
    if (type === '模仿朗读') {
        const question = questions[0] || {};
        reviewDocumentTextBlock(
            parent,
            '朗读原文',
            question.listening_text || reviewContentForItem(item),
            'review-document-script',
            reviewContentForItem(item),
        );
        const references = Array.isArray(question.reference_answers) ? question.reference_answers : [];
        if (references.length && reviewImitationReferenceAnswersAllowed(item)) {
            // Use the shared reference-answer disclosure so套卷 answers get
            // the same count, spacing, open state, and accessible structure
            // as the other document sections.
            renderReviewReferenceAnswers(parent, references, '参考答案');
        }
        return;
    }
    reviewDocumentTextBlock(parent, '解析文本', reviewContentForItem(item));
}

function renderReviewDocumentView(unitModels, presentation, entrySupport = null, sourceFilename = '') {
    const units = $('review-document-units');
    const empty = $('review-document-empty');
    const nav = $('review-document-nav');
    if (!units) return;
    // A workspace refresh replaces the image trigger elements. Close any
    // active preview first so the native dialog cannot retain focus or a stale
    // object URL after its return-focus target has been detached.
    const imageDialog = $('review-image-dialog');
    if (reviewImageDialogState.url || imageDialog?.open) closeReviewImageDialog();
    const models = Array.isArray(unitModels) ? unitModels : [];
    const resolvedEntrySupport = entrySupport || reviewDocumentEntrySupport(
        models.flatMap(unit => Array.isArray(unit?.items) ? unit.items : []),
        currentWorkspace?.system_input,
    );
    const resolvedSourceFilename = sourceFilename
        || currentWorkspace?.source_filename
        || currentSession?.source_filename
        || activeResultContext?.sourceFilename
        || '';
    const supported = resolvedEntrySupport?.supported === true;
    const documentView = $('review-document-view');
    if (documentView) documentView.classList.toggle('is-empty', !supported || models.length === 0);
    const format = $('review-document-format');
    if (format) {
        format.hidden = !supported;
        format.textContent = supported ? `${resolvedEntrySupport.label} · 可进入录入流程` : '暂不支持文稿录入';
        format.className = `review-document-format-badge is-${supported ? 'supported' : 'unsupported'}`;
    }
    if ($('review-document-description')) {
        $('review-document-description').textContent = supported
            ? `${resolvedSourceFilename || '当前文档'} · 按原文顺序查看录入脚本会读取的内容。`
            : '当前任务仍保留音频核对；文稿录入入口会在识别到支持的文档结构后出现。';
    }
    if (nav) {
        if (supported) renderReviewDocumentNav(models, presentation, nav);
        else {
            nav.hidden = true;
            nav.replaceChildren();
        }
    }
    units.replaceChildren();
    const documentItems = models.flatMap(unit => Array.isArray(unit?.items) ? unit.items : []);
    const totalItems = reviewDocumentQuestionCountForItems(documentItems);
    const documentMeasure = reviewDocumentItemsAreTextbook(documentItems) ? '条内容' : '题';
    if ($('review-document-count')) {
        $('review-document-count').textContent = supported
            ? `${resolvedEntrySupport.label} · ${totalItems} ${documentMeasure}`
            : '仅保留音频核对';
    }
    const boundary = $('review-document-boundary');
    if (boundary) {
        if (!supported) {
            boundary.textContent = resolvedEntrySupport?.reason || '当前文档未匹配已接入的录入脚本。';
            boundary.className = 'review-document-boundary is-unsupported';
        } else {
            const candidateHint = presentation?.status === 'multiple_candidate'
                && presentation?.candidateCount > 1
                ? ` 候选范围 ${presentation.candidateCount} 个，需在配置中心确认后才能提交系统。`
                : presentation?.status === 'multiple_confirmed'
                    && presentation?.candidateCount > 1
                    ? ` 已根据 ${presentation.candidateCount} 个独立结构范围完成分组。`
                    : '';
            boundary.textContent = `${resolvedEntrySupport.label}结构已识别。${candidateHint || '下面按题型展示文稿和页面录入字段。'}`;
            boundary.className = `review-document-boundary is-${presentation?.status || 'single_default'} is-supported`;
        }
    }
    if (!supported || !models.length) {
        if (empty) {
            empty.hidden = false;
            const title = empty.querySelector('strong');
            const note = empty.querySelector('p');
            if (title) title.textContent = supported ? '暂时没有可展示的文稿' : '当前文档暂不开放文稿录入';
            if (note) note.textContent = supported
                ? '请重新解析文档，或检查源文件是否包含可读正文。'
                : (resolvedEntrySupport?.reason || '请继续使用音频核对，待接入该文档结构后再开启录入。');
        }
        return;
    }
    if (empty) empty.hidden = true;
    const multiUnit = models.length > 1;
    const unitMeasure = presentation?.inputType === 'paper' ? '套' : '单元';
    const imageTasks = [];
    const fragment = document.createDocumentFragment();
    models.forEach((rawUnit, unitIndex) => {
        const unit = rawUnit && typeof rawUnit === 'object' && !Array.isArray(rawUnit)
            ? rawUnit
            : {};
        const unitItems = Array.isArray(unit?.items) ? unit.items : [];
        const card = document.createElement('article');
        card.className = 'review-document-unit';
        card.setAttribute('role', 'listitem');
        card.id = `review-document-unit-${unitIndex + 1}`;
        if (unit.unit_id) card.dataset.unitId = unit.unit_id;

        const heading = document.createElement('header');
        heading.className = 'review-document-unit-heading';
        const marker = document.createElement('span');
        marker.className = 'review-document-unit-index';
        marker.textContent = String(unitIndex + 1).padStart(2, '0');
        const copy = document.createElement('div');
        copy.className = 'review-document-unit-copy';
        const kicker = document.createElement('span');
        kicker.className = 'panel-kicker';
        kicker.textContent = multiUnit ? `第${reviewChineseOrdinal(unitIndex + 1)}${unitMeasure}` : '当前文档';
        const title = document.createElement('h3');
        title.textContent = multiUnit ? unit.label : (resolvedSourceFilename || unit.label || '当前文档');
        const groupNames = [...new Set(unitItems.flatMap(item => {
            const typePath = reviewTypePathForItem(item, item?.doc_type || '');
            return [typePath[0] || item?.doc_type].filter(Boolean);
        }))];
        const subtitle = document.createElement('p');
        const unitMeasureText = reviewDocumentItemsAreTextbook(unitItems) ? '条内容' : '题';
        const unitContentCount = reviewDocumentQuestionCountForItems(unitItems);
        subtitle.textContent = `${resolvedEntrySupport.label} · ${groupNames.join(' / ') || '解析内容'} · ${unitContentCount} ${unitMeasureText}`;
        copy.append(kicker, title, subtitle);
        const range = document.createElement('span');
        range.className = 'review-document-unit-range';
        range.textContent = multiUnit ? '按录入单元查看' : '按文档原顺序';
        heading.append(marker, copy, range);
        card.appendChild(heading);

        const groupsWrap = document.createElement('div');
        groupsWrap.className = 'review-document-groups';
        buildReviewTypeGroups(unitItems).forEach((typeGroup, groupIndex) => {
            const groupSection = document.createElement('section');
            groupSection.className = 'review-document-group';
            const groupTitle = document.createElement('h4');
            groupTitle.className = 'review-document-group-title';
            const titleText = document.createElement('span');
            titleText.className = 'review-document-group-title-label';
            titleText.textContent = `${reviewChineseOrdinal(groupIndex + 1)}、${typeGroup.name}`;
            const titleCount = document.createElement('small');
            const groupIsTextbook = reviewTypeGroupIsTextbook(typeGroup);
            titleCount.textContent = groupIsTextbook
                ? `${typeGroup.items.length} 条内容`
                : `${reviewDocumentQuestionCountForItems(typeGroup.items)} 题`;
            const groupMeta = document.createElement('div');
            groupMeta.className = 'review-document-group-meta';
            groupMeta.appendChild(titleCount);
            const groupScoreText = groupIsTextbook
                ? ''
                : reviewScoreText(reviewTypeGroupTotalScore(typeGroup));
            if (groupScoreText) {
                const groupScore = document.createElement('small');
                groupScore.className = 'review-document-group-score';
                groupScore.textContent = groupScoreText;
                groupMeta.appendChild(groupScore);
            }
            groupTitle.append(titleText, groupMeta);
            groupSection.appendChild(groupTitle);

            typeGroup.items.forEach((item, itemIndex) => {
                const block = document.createElement('article');
                block.className = 'review-document-item';
                block.tabIndex = 0;
                const reviewIndex = reviewOutlineItemIndex(item) ?? itemIndex;
                block.id = `review-document-item-${reviewIndex}`;
                block.dataset.reviewIndex = String(reviewIndex);
                block.setAttribute('aria-current', reviewSelectedIndex === reviewIndex ? 'true' : 'false');
                const pageInput = reviewPageInputForItem(item);
                const itemHeading = document.createElement('div');
                itemHeading.className = 'review-document-item-heading';
                const itemNumber = document.createElement('span');
                itemNumber.className = 'review-document-item-number';
                itemNumber.textContent = reviewNavItemNumber(item, itemIndex);
                const itemCopy = document.createElement('div');
                const itemTitle = document.createElement('strong');
                itemTitle.textContent = reviewDocumentItemLabel(item, itemIndex);
                const path = document.createElement('span');
                path.className = 'review-document-item-path';
                const itemIsTextbook = reviewItemIsTextbook(item);
                path.textContent = pageInput?.type === '听后记录并转述信息'
                    ? '录入字段 · 第一节听后记录 / 第二节转述信息'
                    : pageInput?.type === '信息转述及询问'
                        ? '录入字段 · 第一节信息转述 / 第二节询问信息'
                    : itemIsTextbook
                        ? `朗读内容 · ${reviewTypePathForItem(item, item.doc_type || unit.label).join(' / ')}`
                        : `录入字段 · ${reviewTypePathForItem(item, item.doc_type || unit.label).join(' / ')}`;
                itemCopy.append(itemTitle, path);
                itemHeading.append(itemNumber, itemCopy);
                block.appendChild(itemHeading);

                renderDocumentPageFacts(block, pageInput, item, imageTasks);

                const facts = document.createElement('dl');
                facts.className = 'review-document-facts';
                const appendFact = (label, value) => {
                    const row = document.createElement('div');
                    const key = document.createElement('dt');
                    key.textContent = label;
                    const fact = document.createElement('dd');
                    fact.textContent = value || '未提供';
                    row.append(key, fact);
                    facts.appendChild(row);
                };
                appendFact(groupIsTextbook ? '内容类型' : '题型', typeGroup.name);
                if (!groupIsTextbook) {
                    const score = reviewDocumentItemValue(item, ['score']);
                    const scoreNumber = reviewScoreNumberText(score);
                    appendFact('分值', scoreNumber ? `${scoreNumber} 分` : (score || '见题目字段'));
                }
                const sourceFact = reviewDocumentItemValue(item, ['source']);
                const isUnitSourceSpecial = reviewDocumentItemValue(
                    item,
                    ['major_section_profile'],
                ) === 'imitation_unit_source_special';
                appendFact(
                    '来源',
                    isUnitSourceSpecial && sourceFact
                        ? sourceFact
                        : reviewDocumentItemLocator(item),
                );
                const pageInputStatus = reviewDocumentItemValue(item, ['page_input_status']);
                if (pageInputStatus === 'invalid') appendFact('录入字段', '格式无效，开始前会被拦截');
                block.appendChild(facts);

                const actions = document.createElement('div');
                actions.className = 'review-document-item-actions';
                if (item.content_ref && !reviewContentForItem(item)) {
                    const loadButton = document.createElement('button');
                    loadButton.type = 'button';
                    loadButton.className = 'btn-ghost btn-sm';
                    loadButton.textContent = '加载全文';
                    loadButton.addEventListener('click', () => { void loadReviewItemContent(item, loadButton); });
                    actions.appendChild(loadButton);
                }
                const select = event => {
                    if (event?.target?.closest('button, summary, [data-review-image-trigger="true"]')) return;
                    reviewSelectedIndex = reviewIndex;
                    reviewSelectedItemId = item?.item_id ? String(item.item_id) : '';
                    syncReviewDocumentSelection(reviewIndex);
                    syncReviewOutlineSelection(reviewIndex);
                    renderReviewInspector(item, reviewIndex);
                };
                block.addEventListener('click', select);
                block.addEventListener('keydown', event => {
                    if (!['Enter', ' '].includes(event.key)) return;
                    event.preventDefault();
                    select(event);
                });
                if (actions.childElementCount > 0) block.appendChild(actions);
                groupSection.appendChild(block);
            });
            groupsWrap.appendChild(groupSection);
        });
        card.appendChild(groupsWrap);
        fragment.appendChild(card);
    });
    units.appendChild(fragment);
    syncReviewDocumentSelection(reviewSelectedIndex);
    imageTasks.forEach(task => { void task(); });
    scheduleReviewPanelViewportSync();
}


registerRendererModule("review.document", {
    ensureReviewViewShell,
    applyReviewViewMode,
    reviewDocumentQuestionCountForItems,
    reviewDocumentItemLabel,
    reviewDocumentItemLocator,
    reviewDocumentItemValue,
    reviewItemIsTextbook,
    reviewTypeGroupIsTextbook,
    reviewPageText,
    syncReviewDocumentSelection,
    renderReviewDocumentView,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
