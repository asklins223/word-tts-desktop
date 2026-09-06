/** Renderer module: voice.assets */
(function attachRendererFeature_voice_assets(root) {
    'use strict';

function voiceAssetUrl(key, kind) {
    const normalizedKey = canonicalVoiceKey(key);
    if (!normalizedKey || !['avatar', 'sample'].includes(kind)) return '';
    const cacheKey = `${normalizedKey}:${kind}`;
    if (voiceAssetObjectUrls.has(cacheKey)) return voiceAssetObjectUrls.get(cacheKey);
    // Chromium loads file:// pages without the capability header.  In the
    // packaged app the renderer therefore receives a Blob URL created from
    // bytes fetched by the preload proxy; it must never address the backend
    // directly.  Keep the versioned relative path for the browser preview
    // and its configuration-only tests.
    if (isElectron) return '';
    // Keep the asset path versioned.  It is a presentation-only cache endpoint,
    // but it still belongs to the same capability-protected local API.
    return ['', 'api', 'v1', 'voice-assets', encodeURIComponent(normalizedKey), kind].join('/');
}

function clearVoiceAssetObjectUrls() {
    voiceAssetObjectUrls.forEach(url => {
        try { URL.revokeObjectURL(url); } catch (_) { /* ignore */ }
    });
    voiceAssetObjectUrls.clear();
}

function rememberVoiceAssetObjectUrl(key, kind, url) {
    const cacheKey = `${key}:${kind}`;
    const oldUrl = voiceAssetObjectUrls.get(cacheKey);
    if (oldUrl && oldUrl !== url) {
        try { URL.revokeObjectURL(oldUrl); } catch (_) { /* ignore */ }
    }
    while (voiceAssetObjectUrls.size >= VOICE_ASSET_OBJECT_URL_LIMIT && !voiceAssetObjectUrls.has(cacheKey)) {
        const oldest = voiceAssetObjectUrls.keys().next().value;
        if (!oldest) break;
        const oldestUrl = voiceAssetObjectUrls.get(oldest);
        try { URL.revokeObjectURL(oldestUrl); } catch (_) { /* ignore */ }
        voiceAssetObjectUrls.delete(oldest);
    }
    voiceAssetObjectUrls.set(cacheKey, url);
}

async function loadCachedVoiceAsset(key, kind) {
    const normalizedKey = canonicalVoiceKey(key);
    if (!normalizedKey || !workflowApi?.readVoiceAsset) return false;
    if (voiceAssetObjectUrls.has(`${normalizedKey}:${kind}`)) return true;
    try {
        const asset = await workflowApi.readVoiceAsset(normalizedKey, kind);
        const bytes = asset?.bytes;
        if (!(bytes instanceof Uint8Array) || bytes.byteLength === 0 || typeof URL?.createObjectURL !== 'function') return false;
        const contentType = asset.contentType || (kind === 'sample' ? 'audio/mpeg' : 'image/jpeg');
        const url = URL.createObjectURL(new Blob([bytes], { type: contentType }));
        rememberVoiceAssetObjectUrl(normalizedKey, kind, url);
        return true;
    } catch (error) {
        console.warn(`音色${kind}资源读取失败:`, error);
        return false;
    }
}

function queueVoiceAssetCache(keys) {
    const values = Array.isArray(keys) ? keys : [keys];
    const normalizedKeys = [...new Set(values.map(value => canonicalVoiceKey(value)).filter(Boolean))];
    const pendingKeys = normalizedKeys.filter(key => (
        !voiceAssetCacheReady.has(key) && !voiceAssetCacheRequests.has(key)
    ));
    const inFlightRequests = [...new Set(normalizedKeys
        .map(key => voiceAssetCacheRequests.get(key))
        .filter(Boolean))];
    let cacheRequest = null;

    if (pendingKeys.length) {
        if (workflowApi?.cacheVoiceAssets) {
            const request = workflowApi.cacheVoiceAssets(pendingKeys)
                .then(response => {
                    const cached = response?.cached;
                    return Promise.all(pendingKeys.map(async key => {
                        const result = cached?.[key] || {};
                        const kinds = ['avatar', 'sample'].filter(kind => Boolean(result[kind]));
                        if (!isElectron) return { key, ready: false };
                        const loaded = await Promise.all(kinds.map(kind => loadCachedVoiceAsset(key, kind)));
                        const ready = loaded.some(Boolean);
                        if (ready) voiceAssetCacheReady.add(key);
                        return { key, ready };
                    })).then(() => response);
                })
                .catch(error => {
                    // Voice media is an enhancement; a catalog URL remains a
                    // valid fallback when the local cache cannot be populated.
                    console.warn('音色资源缓存失败:', error);
                    return null;
                });
            pendingKeys.forEach(key => voiceAssetCacheRequests.set(key, request));
            cacheRequest = request.finally(() => {
                pendingKeys.forEach(key => {
                    if (voiceAssetCacheRequests.get(key) === request) voiceAssetCacheRequests.delete(key);
                });
            });
        } else {
            // Configuration-only renderer tests and non-Electron preview pages
            // do not have a cache transport.  Keep the remote catalog URL.
            cacheRequest = Promise.resolve(null);
        }
    }

    // 如果生成流程刚刚发起过同一批缓存请求，结果页必须等待它们完成，
    // 否则首次渲染会错过缓存完成时机，头像节点被移除后就不会再回来。
    const requests = [...new Set([...inFlightRequests, cacheRequest].filter(Boolean))];
    if (!requests.length) return Promise.resolve(null);
    return Promise.all(requests).then(results => results.find(Boolean) || null);
}

function getResultVoiceEntry(key) {
    const voice = getVoiceEntry(key);
    const normalizedKey = canonicalVoiceKey(voice.key || key);
    const useCachedAssets = voiceAssetCacheReady.has(normalizedKey);
    return {
        ...voice,
        // 缓存完成前直接使用目录中的远程资源，避免把尚未生成的本地地址
        // 当成首选地址；缓存完成后再切换到本地资源，减少结果页的网络依赖。
        img_url: voice.img_url
            ? (useCachedAssets ? voiceAssetUrl(normalizedKey, 'avatar') : voice.img_url)
            : '',
        fallback_img_url: useCachedAssets ? voice.img_url : '',
        audio_url: voice.audio_url
            ? (useCachedAssets ? voiceAssetUrl(normalizedKey, 'sample') : voice.audio_url)
            : '',
        fallback_audio_url: useCachedAssets ? voice.audio_url : '',
    };
}


registerRendererModule("voice.assets", {
    voiceAssetUrl,
    clearVoiceAssetObjectUrls,
    rememberVoiceAssetObjectUrl,
    loadCachedVoiceAsset,
    queueVoiceAssetCache,
    getResultVoiceEntry,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);

