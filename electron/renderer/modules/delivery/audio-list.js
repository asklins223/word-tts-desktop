/** Renderer module: delivery.audioList */
(function attachRendererFeature_delivery_audioList(root) {
    'use strict';

function prepareAudioFilters(files) {
    const searchInput = $('audio-search-input');
    const typeFilter = $('audio-type-filter');
    const toolbar = $('audio-toolbar');
    const empty = $('audio-filter-empty');
    if (searchInput) searchInput.value = '';
    if (empty) empty.hidden = true;
    if (toolbar) toolbar.hidden = files.length < 5;
    if (!typeFilter) return;

    typeFilter.replaceChildren();
    const allOption = document.createElement('option');
    allOption.value = '';
    allOption.textContent = '全部题型';
    typeFilter.appendChild(allOption);
    [...new Set(files.map(file => file?.doc_type).filter(Boolean))]
        .sort((a, b) => String(a).localeCompare(String(b), 'zh-CN'))
        .forEach(type => {
            const option = document.createElement('option');
            option.value = type;
            option.textContent = type;
            typeFilter.appendChild(option);
        });
    window.WordTTSUI?.syncSelect(typeFilter);
}

function filterAudioItems() {
    const audioList = $('audio-list');
    if (!audioList) return;
    const query = String($('audio-search-input')?.value || '').trim().toLocaleLowerCase('zh-CN');
    const selectedType = $('audio-type-filter')?.value || '';
    const items = [...audioList.querySelectorAll('.audio-item')];
    let visibleCount = 0;
    items.forEach(item => {
        const matchesQuery = !query || (item.dataset.searchText || '').includes(query);
        const matchesType = !selectedType || item.dataset.docType === selectedType;
        const visible = matchesQuery && matchesType;
        item.hidden = !visible;
        if (visible) visibleCount++;
        else if (item._audioElement && !item._audioElement.paused) item._audioElement.pause();
    });
    const count = $('audio-count');
    if (count) count.textContent = query || selectedType
        ? `${visibleCount} / ${items.length} 个文件`
        : `${items.length} 个文件`;
    const empty = $('audio-filter-empty');
    if (empty) empty.hidden = visibleCount > 0 || items.length === 0;
}

function scheduleAudioFilter() {
    if (audioFilterFrame !== null) return;
    const schedule = window.requestAnimationFrame
        ? callback => window.requestAnimationFrame(callback)
        : callback => window.setTimeout(callback, 0);
    audioFilterFrame = schedule(() => {
        audioFilterFrame = null;
        filterAudioItems();
    });
}

function resultVoiceKeysForFile(file) {
    const values = (Array.isArray(file?.voice_keys)
        ? [...file.voice_keys]
        : (file?.voice_keys ? [file.voice_keys] : []))
        .concat(Array.isArray(file?.metadata?.voice_keys)
            ? file.metadata.voice_keys
            : (file?.metadata?.voice_keys ? [file.metadata.voice_keys] : []))
        .concat(file?.voice_key || [])
        .filter(value => String(value ?? '').trim());

    // 兼容早期历史记录可能保存的单个 voice 字段；只接受能在当前目录
    // 精确匹配到 key 或名称的值，避免把“女声/男声”等展示文本误当成 key。
    if (!values.length && file?.voice) {
        const legacyValue = String(file.voice).trim();
        const normalizedLegacyKey = canonicalVoiceKey(legacyValue);
        const normalizedLegacyName = legacyValue.toLocaleLowerCase('zh-CN');
        const legacyVoice = voiceCatalog.find(voice => (
            normalizeVoiceKey(voice.key) === normalizedLegacyKey
            || String(voice.name || '').trim().toLocaleLowerCase('zh-CN') === normalizedLegacyName
        ));
        if (legacyVoice) values.push(legacyVoice.key);
        else if (normalizedLegacyKey && normalizedLegacyKey !== legacyValue) values.push(normalizedLegacyKey);
    }

    const canonicalize = value => {
        const normalized = canonicalVoiceKey(value);
        if (!normalized) return '';
        const normalizedName = String(value ?? '').trim().toLocaleLowerCase('zh-CN');
        const catalogVoice = voiceCatalog.find(voice => (
            voice.key === normalized
            || String(voice.name || '').trim().toLocaleLowerCase('zh-CN') === normalizedName
        ));
        return catalogVoice?.key || normalized;
    };
    return [...new Set(values.map(canonicalize).filter(Boolean))];
}

function createResultVoiceStrip(file) {
    const strip = document.createElement('div');
    strip.className = 'audio-voice-strip';

    const label = document.createElement('span');
    label.className = 'audio-voice-caption';
    label.textContent = '音色';
    strip.appendChild(label);

    const voiceKeys = resultVoiceKeysForFile(file);
    label.textContent = voiceKeys.length > 1 ? `音色 · ${voiceKeys.length} 种` : '音色';
    label.title = voiceKeys.length > 1 ? `本段音频使用 ${voiceKeys.length} 种音色` : '本段音频使用的音色';
    if (!voiceKeys.length) {
        const empty = document.createElement('span');
        empty.className = 'audio-voice-empty';
        empty.textContent = '历史文件未记录音色信息';
        strip.appendChild(empty);
        return strip;
    }

    voiceKeys.forEach(key => {
        const voice = getResultVoiceEntry(key);
        const chip = document.createElement('span');
        chip.className = 'audio-voice-chip';

        const avatarButton = document.createElement('button');
        avatarButton.type = 'button';
        avatarButton.className = 'audio-voice-avatar-button';
        avatarButton.dataset.audioName = `音色 ${voice.name}`;
        avatarButton.setAttribute('aria-label', `试听音色 ${voice.name}`);
        const hasSample = Boolean(voice.audio_url || voice.fallback_audio_url);
        avatarButton.title = hasSample ? `试听 ${voice.name}` : `${voice.name}暂无示例音频`;
        avatarButton.disabled = !hasSample;

        const avatar = document.createElement('span');
        avatar.className = 'voice-avatar audio-result-avatar';
        renderVoiceAvatar(avatar, voice, false, true);
        avatarButton.appendChild(avatar);

        const playState = document.createElement('span');
        playState.className = 'audio-voice-play-state';
        playState.setAttribute('aria-hidden', 'true');
        playState.innerHTML = '<svg class="icon-play" width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg><svg class="icon-pause" width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style="display:none"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
        avatarButton.appendChild(playState);
        avatarButton.addEventListener('click', event => {
            event.stopPropagation();
            playVoiceSample(voice, avatarButton);
        });

        const name = document.createElement('span');
        name.className = 'audio-voice-name';
        name.textContent = voice.name;
        name.title = voice.name;

        chip.append(avatarButton, name);
        strip.appendChild(chip);
    });
    return strip;
}

async function refreshResultVoiceAssets(files) {
    const resultFiles = Array.isArray(files) ? files : [];
    const voiceKeys = [...new Set(resultFiles.flatMap(resultVoiceKeysForFile))];
    if (!voiceKeys.length) return;

    const resultContext = activeResultContext;
    await queueVoiceAssetCache(voiceKeys);
    if (!resultContext || activeResultContext !== resultContext) return;

    // 只替换音色条，不重建 Audio、波形和原文折叠状态，避免缓存完成后
    // 造成结果页闪烁或打断用户正在试听的音频。
    document.querySelectorAll('#audio-list .audio-item').forEach(item => {
        const strip = item.querySelector('.audio-voice-strip');
        if (!strip || !item._resultFile) return;
        strip.replaceWith(createResultVoiceStrip(item._resultFile));
    });
}


registerRendererModule("delivery.audioList", {
    prepareAudioFilters,
    filterAudioItems,
    scheduleAudioFilter,
    resultVoiceKeysForFile,
    createResultVoiceStrip,
    refreshResultVoiceAssets,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

