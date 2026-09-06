/** Renderer module: delivery.artifacts */
(function attachRendererFeature_delivery_artifacts(root) {
    'use strict';

function rendererReadableArtifactStream(transport) {
    if (transport && typeof transport.getReader === 'function') return transport;
    if (!transport || typeof transport.onData !== 'function') {
        throw new Error('Artifact 流式读取通道不可用');
    }

    let stream = null;
    let closed = false;
    let pendingAck = false;
    let ackInFlight = false;
    let ackQueued = false;
    const metadata = {};
    let removeData = () => {};
    let removeMetadata = () => {};
    let removeEnd = () => {};
    let removeError = () => {};

    const closeTransport = () => {
        removeData();
        removeMetadata();
        removeEnd();
        removeError();
        removeData = removeMetadata = removeEnd = removeError = () => {};
        return Promise.resolve(transport.close?.()).catch(() => {});
    };
    const fail = (controller, error) => {
        if (closed) return;
        closed = true;
        void closeTransport();
        controller.error(error instanceof Error ? error : new Error(String(error || 'workflow artifact stream failed')));
    };
    const requestAck = (controller) => {
        if (closed || !pendingAck || typeof transport.ack !== 'function') return;
        if (ackInFlight) {
            ackQueued = true;
            return;
        }
        pendingAck = false;
        ackInFlight = true;
        Promise.resolve(transport.ack()).catch((error) => fail(controller, error)).finally(() => {
            ackInFlight = false;
            if (ackQueued) {
                ackQueued = false;
                requestAck(controller);
            }
        });
    };

    stream = new ReadableStream({
        start(controller) {
            removeMetadata = transport.onMetadata?.((value) => {
                Object.assign(metadata, value || {});
                if (stream) stream.metadata = metadata;
            }) || (() => {});
            removeData = transport.onData((value) => {
                if (closed) return;
                try {
                    const bytes = value instanceof Uint8Array ? value : new Uint8Array(value || []);
                    pendingAck = true;
                    controller.enqueue(bytes);
                } catch (error) {
                    fail(controller, error);
                }
            }) || (() => {});
            removeEnd = transport.onEnd?.(() => {
                if (closed) return;
                closed = true;
                void closeTransport();
                controller.close();
            }) || (() => {});
            removeError = transport.onError?.((value) => {
                const error = value instanceof Error
                    ? value
                    : Object.assign(new Error(value?.message || 'workflow artifact stream failed'), value || {});
                fail(controller, error);
            }) || (() => {});
        },
        pull(controller) {
            requestAck(controller);
        },
        cancel() {
            if (closed) return;
            closed = true;
            void closeTransport();
        },
    });
    stream.metadata = metadata;
    return stream;
}

async function openRendererArtifactStream(artifactId) {
    if (!workflowApi || !artifactId) throw new Error('Artifact 标识缺失');
    const transport = await workflowApi.openArtifact(artifactId);
    return rendererReadableArtifactStream(transport);
}

async function readArtifactBytes(artifactId, maxBytes = MAX_BUFFERED_ARTIFACT_BYTES) {
    if (!workflowApi || !artifactId) throw new Error('Artifact 标识缺失');
    const byteLimit = Number.isSafeInteger(maxBytes) && maxBytes > 0
        ? maxBytes
        : MAX_BUFFERED_ARTIFACT_BYTES;
    const stream = await openRendererArtifactStream(artifactId);
    const reader = stream.getReader();
    const chunks = [];
    let total = 0;
    try {
        while (true) {
            const part = await reader.read();
            if (part.done) break;
            const chunk = part.value instanceof Uint8Array ? part.value : new Uint8Array(part.value || []);
            if (total + chunk.byteLength > byteLimit) {
                const error = new Error(`Artifact 超过浏览器有界读取上限（${Math.round(byteLimit / 1024 / 1024)} MiB）`);
                error.code = 'ARTIFACT_TOO_LARGE_FOR_BUFFER';
                await reader.cancel(error).catch(() => {});
                throw error;
            }
            chunks.push(chunk);
            total += chunk.byteLength;
        }
    } finally {
        reader.releaseLock?.();
    }
    const bytes = new Uint8Array(total);
    let offset = 0;
    chunks.forEach(chunk => {
        bytes.set(chunk, offset);
        offset += chunk.byteLength;
    });
    return bytes;
}

function artifactMime(format) {
    return {
        mp3: 'audio/mpeg',
        wav: 'audio/wav',
        png: 'image/png',
        jpg: 'image/jpeg',
        jpeg: 'image/jpeg',
        zip: 'application/zip',
        json: 'application/json',
    }[String(format || '').toLowerCase()] || 'application/octet-stream';
}

function filenameWithExtension(filename, format, fallback = '下载文件') {
    const raw = String(filename || '').trim().split(/[\\/]/).pop() || fallback;
    const extension = String(format || '').trim().toLowerCase().replace(/^\./, '');
    if (!extension || raw.toLowerCase().endsWith(`.${extension}`)) return raw;
    return `${raw}.${extension}`;
}

function deliveryZipFilename(sourceFilename = '') {
    const source = String(sourceFilename || PRODUCT_NAME).trim().split(/[\\/]/).pop() || PRODUCT_NAME;
    const stem = source.replace(/\.(docx|xlsx)$/i, '') || PRODUCT_NAME;
    return filenameWithExtension(`${stem}_tts`, 'zip', `${PRODUCT_NAME}_tts`);
}

function createArtifactAbortError(message = '音频播放源已取消') {
    const error = new Error(message);
    error.name = 'AbortError';
    return error;
}

function supportsMediaSourceMime(mimeType) {
    if (typeof MediaSource !== 'function' || typeof MediaSource.isTypeSupported !== 'function') return false;
    try {
        return Boolean(mimeType) && MediaSource.isTypeSupported(mimeType);
    } catch (_) {
        return false;
    }
}

function waitForNativeAudioReady(audio, timeoutMs = 15000) {
    if (!audio) return Promise.reject(new Error('音频播放器不可用'));
    if (audio.readyState >= 2) return Promise.resolve(audio);
    if (audio.error) {
        const error = new Error('音频资源无法解码');
        error.code = 'MEDIA_DECODE_ERROR';
        return Promise.reject(error);
    }
    return new Promise((resolve, reject) => {
        let settled = false;
        const timeout = Math.max(1000, Number(timeoutMs) || 15000);
        let timer = null;
        const cleanup = () => {
            audio.removeEventListener('loadeddata', onReady);
            audio.removeEventListener('canplay', onReady);
            audio.removeEventListener('error', onError);
            audio.removeEventListener('abort', onAbort);
            if (timer) clearTimeout(timer);
        };
        const finish = (handler, value) => {
            if (settled) return;
            settled = true;
            cleanup();
            handler(value);
        };
        const onReady = () => finish(resolve, audio);
        const onError = () => {
            const error = new Error('音频资源无法解码');
            error.code = 'MEDIA_DECODE_ERROR';
            finish(reject, error);
        };
        const onAbort = () => finish(reject, createArtifactAbortError('音频加载已取消'));
        audio.addEventListener('loadeddata', onReady, { once: true });
        audio.addEventListener('canplay', onReady, { once: true });
        audio.addEventListener('error', onError, { once: true });
        audio.addEventListener('abort', onAbort, { once: true });
        timer = setTimeout(() => {
            const error = new Error('音频加载超时');
            error.code = 'MEDIA_LOAD_TIMEOUT';
            finish(reject, error);
        }, timeout);
        if (audio.readyState >= 2) finish(resolve, audio);
    });
}

function waitForMediaSourceOpen(mediaSource, isCurrent) {
    if (mediaSource?.readyState === 'open') return Promise.resolve();
    return new Promise((resolve, reject) => {
        let settled = false;
        const cleanup = () => {
            mediaSource?.removeEventListener('sourceopen', onOpen);
            mediaSource?.removeEventListener('sourceclose', onClose);
            mediaSource?.removeEventListener('error', onError);
        };
        const finish = (handler, value) => {
            if (settled) return;
            settled = true;
            cleanup();
            handler(value);
        };
        const onOpen = () => {
            if (!isCurrent()) return finish(reject, createArtifactAbortError());
            finish(resolve);
        };
        const onClose = () => finish(reject, new Error('音频 MediaSource 已关闭'));
        const onError = () => finish(reject, new Error('音频 MediaSource 打开失败'));
        mediaSource?.addEventListener('sourceopen', onOpen, { once: true });
        mediaSource?.addEventListener('sourceclose', onClose, { once: true });
        mediaSource?.addEventListener('error', onError, { once: true });
    });
}

function appendMediaSourceChunk(sourceBuffer, chunk, isCurrent) {
    if (!isCurrent()) return Promise.reject(createArtifactAbortError());
    return new Promise((resolve, reject) => {
        let settled = false;
        const cleanup = () => {
            sourceBuffer?.removeEventListener('updateend', onUpdateEnd);
            sourceBuffer?.removeEventListener('error', onError);
            sourceBuffer?.removeEventListener('abort', onAbort);
        };
        const finish = (handler, value) => {
            if (settled) return;
            settled = true;
            cleanup();
            handler(value);
        };
        const onUpdateEnd = () => {
            if (!isCurrent()) return finish(reject, createArtifactAbortError());
            finish(resolve);
        };
        const onError = () => finish(reject, new Error('音频数据追加失败'));
        const onAbort = () => finish(reject, createArtifactAbortError());
        sourceBuffer?.addEventListener('updateend', onUpdateEnd, { once: true });
        sourceBuffer?.addEventListener('error', onError, { once: true });
        sourceBuffer?.addEventListener('abort', onAbort, { once: true });
        try {
            if (!sourceBuffer || sourceBuffer.updating) throw new Error('音频缓冲区正在更新');
            sourceBuffer.appendBuffer(chunk);
        } catch (error) {
            finish(reject, error);
        }
    });
}

function ensureFinalDeliveryResultButton() {
    if ($('open-final-delivery-btn')) return $('open-final-delivery-btn');
    const actions = document.querySelector('#page-4 .result-header-actions');
    if (!actions) return null;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn-secondary';
    button.id = 'open-final-delivery-btn';
    button.textContent = '查看最终结果';
    button.title = '打开音频与系统录入的最终结果页';
    const newFileButton = $('new-file-btn');
    if (newFileButton?.parentElement === actions) newFileButton.before(button);
    else actions.appendChild(button);
    button.addEventListener('click', () => {
        showFinalDeliveryPage({ workspace: finalDeliveryContextWorkspace(), refresh: false });
    });
    return button;
}


registerRendererModule("delivery.artifacts", {
    rendererReadableArtifactStream,
    openRendererArtifactStream,
    readArtifactBytes,
    artifactMime,
    filenameWithExtension,
    deliveryZipFilename,
    createArtifactAbortError,
    supportsMediaSourceMime,
    waitForNativeAudioReady,
    waitForMediaSourceOpen,
    appendMediaSourceChunk,
    ensureFinalDeliveryResultButton,
});
})(typeof globalThis !== 'undefined' ? globalThis : window);
