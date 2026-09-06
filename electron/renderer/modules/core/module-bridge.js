/**
 * Renderer module bridge.
 *
 * The desktop renderer still uses classic scripts so the packaged app does
 * not need a bundler. Each feature registers a namespaced public surface while
 * the compatibility aliases keep the migration behaviour-preserving.
 */
(function attachRendererModuleBridge(root) {
    'use strict';

    const registry = root.WORDTTS_RENDERER || {};
    const modules = registry.modules || {};
    const aliasOwners = registry.aliasOwners || {};

    function registerRendererModule(name, exports = {}) {
        const moduleName = String(name || '').trim();
        if (!moduleName) throw new Error('Renderer module name is required');
        if (Object.prototype.hasOwnProperty.call(modules, moduleName)) {
            throw new Error(`Renderer module already registered: ${moduleName}`);
        }
        const publicExports = Object.freeze({ ...exports });
        Object.keys(publicExports).forEach((key) => {
            const previousOwner = aliasOwners[key];
            if (previousOwner && previousOwner !== moduleName) {
                throw new Error(`Renderer export alias collision: ${key} (${previousOwner}, ${moduleName})`);
            }
        });
        modules[moduleName] = publicExports;

        // Compatibility bridge for the legacy renderer calls. New code should
        // consume WORDTTS_RENDERER.modules[name] explicitly.
        Object.entries(publicExports).forEach(([key, value]) => {
            aliasOwners[key] = moduleName;
            root[key] = value;
        });
        return publicExports;
    }

    function getRendererModule(name) {
        return modules[String(name || '')] || null;
    }

    function getRendererContext() {
        return registry.context || null;
    }

    registry.modules = modules;
    registry.aliasOwners = aliasOwners;
    registry.register = registerRendererModule;
    registry.getModule = getRendererModule;
    registry.getContext = getRendererContext;
    root.WORDTTS_RENDERER = registry;
    root.registerRendererModule = registerRendererModule;
})(typeof globalThis !== 'undefined' ? globalThis : window);
