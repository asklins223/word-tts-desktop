const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function loadRendererConfigFunctions() {
    const sourcePath = path.join(__dirname, '..', 'renderer', 'app.js');
    const source = fs.readFileSync(sourcePath, 'utf8').replace(/\ninit\(\);\s*$/, '\n');
    const storage = new Map();
    const context = {
        console,
        document: {},
        window: { electronAPI: undefined },
        localStorage: {
            getItem: key => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, String(value)),
        },
        navigator: {},
        setTimeout,
        clearTimeout,
        URL,
        Blob,
        FormData,
        AbortController,
        Map,
        Set,
    };
    vm.createContext(context);
    vm.runInContext(`${source}\nglobalThis.__rendererTests = { normalizePersistedConfig, saveCurrentConfig };`, context);
    return { api: context.__rendererTests, storage };
}

test('长期配置只保留默认男女声的独立参数，不保存文档角色', () => {
    const { api } = loadRendererConfigFunctions();
    const persisted = api.normalizePersistedConfig({
        default_female_voice: 'speaker:shared',
        default_male_voice: 'speaker:shared',
        role_configs: {
            __default_female__: { rate: 10, volume: 20, pitch: 30 },
            __default_male__: { rate: 40, volume: 50, pitch: 60 },
            'role:reporter': { rate: 70, volume: 80, pitch: 90 },
        },
        role_voices: { reporter: 'speaker:reporter' },
        voice_configs: {
            'speaker:shared': { rate: 99, volume: 99, pitch: 99 },
            'speaker:reporter': { rate: 1, volume: 1, pitch: 1 },
        },
    });

    assert.deepEqual(JSON.parse(JSON.stringify(persisted.role_configs)), {
        __default_female__: { rate: 10, volume: 20, pitch: 30 },
        __default_male__: { rate: 40, volume: 50, pitch: 60 },
    });
    assert.equal('role_voices' in persisted, false);
    assert.equal('voice_configs' in persisted, false);
});

test('当前配置写入 localStorage 前会清理旧角色数据', () => {
    const { api, storage } = loadRendererConfigFunctions();
    api.saveCurrentConfig({
        default_female_voice: 'amanda',
        default_male_voice: 'george',
        role_configs: {
            __default_female__: { rate: 35, volume: 50, pitch: 50 },
            __default_male__: { rate: 65, volume: 50, pitch: 50 },
            'role:mr yan': { rate: 1, volume: 2, pitch: 3 },
        },
        role_voices: { 'mr yan': 'george' },
    });

    const saved = JSON.parse(storage.get('wordtts_current_config_xunfei_v3'));
    assert.deepEqual(saved.role_configs, {
        __default_female__: { rate: 35, volume: 50, pitch: 50 },
        __default_male__: { rate: 65, volume: 50, pitch: 50 },
    });
    assert.equal('role_voices' in saved, false);
});
