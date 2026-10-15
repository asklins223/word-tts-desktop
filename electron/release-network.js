'use strict';

const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);
const GITHUB_RELEASE_ASSET_HOSTS = new Set([
    'release-assets.githubusercontent.com',
    'objects.githubusercontent.com',
]);
const MAX_RELEASE_REDIRECTS = 5;
const SENSITIVE_REDIRECT_HEADERS = new Set([
    'authorization',
    'cookie',
    'cookie2',
    'proxy-authorization',
]);

function isTrustedReleaseUrl(baseUrl, candidateUrl) {
    try {
        const base = new URL(String(baseUrl));
        const candidate = new URL(String(candidateUrl), base);
        if (base.protocol !== 'https:' || candidate.protocol !== 'https:'
            || base.username || base.password || candidate.username || candidate.password
            || base.port || candidate.port) return false;
        const allowedHosts = new Set([base.hostname.toLowerCase()]);
        if (base.hostname.toLowerCase() === 'github.com') {
            GITHUB_RELEASE_ASSET_HOSTS.forEach(host => allowedHosts.add(host));
        }
        return allowedHosts.has(candidate.hostname.toLowerCase());
    } catch (_) {
        return false;
    }
}

/**
 * Follow only HTTPS redirects that remain on the configured release host or
 * GitHub's release-asset hosts. `fetch(..., redirect: 'follow')` contacts each
 * target before callers can inspect response.url, which is unsafe in the main
 * process because a compromised redirect could reach localhost or a LAN host.
 */
async function fetchTrustedReleaseUrl(url, init = {}, fetchImpl = globalThis.fetch) {
    if (typeof fetchImpl !== 'function' || !isTrustedReleaseUrl(url, url)) {
        throw Object.assign(new Error('线上发布地址不是受信任的 HTTPS 地址。'), {
            code: 'UNTRUSTED_RELEASE_URL',
        });
    }
    const method = String(init.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
        throw Object.assign(new Error('线上发布请求只允许 GET 或 HEAD。'), {
            code: 'UNSUPPORTED_RELEASE_METHOD',
        });
    }
    let currentUrl = new URL(String(url));
    let requestInit = { ...init, method, headers: new Headers(init.headers || {}) };
    for (let redirects = 0; redirects <= MAX_RELEASE_REDIRECTS; redirects += 1) {
        const response = await fetchImpl(currentUrl.toString(), {
            ...requestInit,
            headers: new Headers(requestInit.headers),
            redirect: 'manual',
        });
        const status = Number(response?.status) || 0;
        const location = response?.headers?.get?.('location');
        if (!REDIRECT_STATUSES.has(status) || !location) {
            if (response?.url && !isTrustedReleaseUrl(url, response.url)) {
                try { await response.body?.cancel?.(); } catch (_) { /* response will be rejected */ }
                throw Object.assign(new Error('线上发布响应来自未经授权的地址。'), {
                    code: 'UNTRUSTED_RELEASE_REDIRECT',
                });
            }
            return response;
        }
        if (redirects === MAX_RELEASE_REDIRECTS) {
            try { await response.body?.cancel?.(); } catch (_) { /* best effort */ }
            throw Object.assign(new Error('线上发布地址重定向次数过多。'), {
                code: 'RELEASE_REDIRECT_LIMIT',
            });
        }
        let nextUrl;
        try {
            nextUrl = new URL(location, currentUrl);
        } catch (_) {
            try { await response.body?.cancel?.(); } catch (_) { /* best effort */ }
            throw Object.assign(new Error('线上发布地址包含无效的重定向。'), {
                code: 'INVALID_RELEASE_REDIRECT',
            });
        }
        if (!isTrustedReleaseUrl(url, nextUrl)) {
            try { await response.body?.cancel?.(); } catch (_) { /* best effort */ }
            throw Object.assign(new Error('线上发布地址跳转到未经授权的服务器。'), {
                code: 'UNTRUSTED_RELEASE_REDIRECT',
            });
        }
        if (currentUrl.origin !== nextUrl.origin) {
            for (const header of SENSITIVE_REDIRECT_HEADERS) requestInit.headers.delete(header);
        }
        try { await response.body?.cancel?.(); } catch (_) { /* best effort */ }
        currentUrl = nextUrl;
    }
    throw Object.assign(new Error('线上发布地址重定向失败。'), { code: 'RELEASE_REDIRECT_FAILED' });
}

module.exports = { fetchTrustedReleaseUrl, isTrustedReleaseUrl };
