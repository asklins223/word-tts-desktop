(() => {
    'use strict';

    if (!document.documentElement.hasAttribute('data-protected-bootstrap')) return;

    const showFatalError = (error) => {
        const message = document.createElement('main');
        message.setAttribute('role', 'alert');
        message.style.cssText = 'max-width:640px;margin:12vh auto;padding:28px;font:16px/1.6 system-ui,sans-serif;color:#172033;background:#fff;border:1px solid #d7e0ee;border-radius:16px;box-shadow:0 16px 50px #17203320';
        const title = document.createElement('h1');
        title.textContent = '客户端界面加载失败';
        const detail = document.createElement('p');
        detail.textContent = error?.message || '应用无法加载，请联网后重新启动。';
        message.append(title, detail);
        document.body.replaceChildren(message);
    };

    const validateAssetPath = (relativePath, type) => {
        const extension = type === 'html' ? 'html' : type === 'css' ? 'css' : 'js';
        if (typeof relativePath !== 'string'
            || !new RegExp(`^[a-zA-Z0-9._/-]+\\.${extension}\\.enc$`).test(relativePath)
            || relativePath.split('/').some(part => !part || part === '.' || part === '..')) {
            throw new Error('客户端资源清单无效');
        }
    };

    (async () => {
        try {
            const api = window.electronAPI;
            if (typeof api?.loadProtectedClientAsset !== 'function') {
                throw new Error('应用没有提供客户端资源加载接口');
            }
            const loadAsset = async (relativePath, type) => {
                validateAssetPath(relativePath, type);
                const source = await api.loadProtectedClientAsset(relativePath);
                if (typeof source !== 'string' || source.length === 0) {
                    throw new Error(`无法读取客户端资源：${relativePath}`);
                }
                return source;
            };

            const html = await loadAsset('index.html.enc', 'html');
            const parsed = new DOMParser().parseFromString(html, 'text/html');
            if (!parsed.documentElement || !parsed.head || !parsed.body) {
                throw new Error('客户端 HTML 结构无效');
            }
            if (parsed.querySelector('script:not([data-protected-src])')) {
                throw new Error('客户端 HTML 包含未保护的脚本');
            }
            if (parsed.querySelector('link[rel~="stylesheet"]:not([data-protected-style])')) {
                throw new Error('客户端 HTML 包含未保护的样式表');
            }

            const parsedStyles = [...parsed.querySelectorAll('link[data-protected-style]')];
            const parsedScripts = [...parsed.querySelectorAll('script[data-protected-src]')];
            if (parsedStyles.length === 0 || parsedScripts.length === 0) {
                throw new Error('客户端 HTML 缺少受保护的脚本或样式');
            }

            document.replaceChild(document.importNode(parsed.documentElement, true), document.documentElement);

            for (const placeholder of document.querySelectorAll('link[data-protected-style]')) {
                const relativePath = placeholder.dataset.protectedStyle;
                const source = await loadAsset(relativePath, 'css');
                const style = document.createElement('style');
                style.textContent = source;
                placeholder.replaceWith(style);
            }

            for (const placeholder of document.querySelectorAll('script[data-protected-src]')) {
                const relativePath = placeholder.dataset.protectedSrc;
                const source = await loadAsset(relativePath, 'js');
                const scriptUrl = URL.createObjectURL(new Blob([source], { type: 'text/javascript;charset=utf-8' }));
                const script = document.createElement('script');
                script.src = scriptUrl;
                script.async = false;
                try {
                    await new Promise((resolve, reject) => {
                        script.addEventListener('load', resolve, { once: true });
                        script.addEventListener('error', () => reject(new Error(`客户端脚本执行失败：${relativePath}`)), { once: true });
                        placeholder.replaceWith(script);
                    });
                } finally {
                    URL.revokeObjectURL(scriptUrl);
                }
            }

            const ready = await api.protectedClientAssetsReady();
            if (ready !== true) throw new Error('客户端资源未完成初始化');
        } catch (error) {
            try {
                await window.electronAPI?.protectedClientAssetsFailed?.(error?.message || '客户端界面加载失败');
            } catch (_) {
                showFatalError(error);
            }
            throw error;
        }
    })();
})();
