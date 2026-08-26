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
    vm.runInContext(`${source}\nglobalThis.__rendererTests = { normalizePersistedConfig, saveCurrentConfig, integerProgressCount, resultVoiceKeysForFile, setVoiceCatalog, getResultVoiceEntry, voiceAssetCacheReady };`, context);
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
    assert.equal(persisted.generation_mode, 'composite_cut');
    assert.equal('role_voices' in persisted, false);
    assert.equal('voice_configs' in persisted, false);
});

test('生成方式预设支持默认合并模式和原有单条模式', () => {
    const { api } = loadRendererConfigFunctions();

    assert.equal(
        api.normalizePersistedConfig({ generation_mode: 'single_segment' }).generation_mode,
        'single_segment',
    );
    assert.equal(
        api.normalizePersistedConfig({ generation_mode: 'unsupported' }).generation_mode,
        'composite_cut',
    );
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

test('进度计数始终按整数四舍五入并限制在总数内', () => {
    const { api } = loadRendererConfigFunctions();

    assert.equal(api.integerProgressCount(3.6, 37), 4);
    assert.equal(api.integerProgressCount(33.4, 37), 33);
    assert.equal(api.integerProgressCount(999.9, 37), 37);
    assert.equal(api.integerProgressCount('not-a-number', 37), 0);
});

test('结果页按文件音色元数据去重，并兼容可精确匹配的旧 voice 字段', () => {
    const { api } = loadRendererConfigFunctions();
    api.setVoiceCatalog([{ key: 'speaker:linda', name: 'Linda-品质' }]);

    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({
        voice_keys: ['speaker:linda', 'speaker:linda', '', null],
        voice_key: 'speaker:george',
    }))), ['speaker:linda', 'speaker:george']);
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({
        voice_keys: [''],
        voice: 'Linda-品质',
    }))), ['speaker:linda']);
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({ voice_keys: 'speaker:linda' }))), ['speaker:linda']);
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({ voice_keys: ['Linda-品质'] }))), ['speaker:linda']);
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({ voice: 'linda-品质' }))), ['speaker:linda']);
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({ voice: 'Amanda' }))), ['amanda']);
    assert.deepEqual(JSON.parse(JSON.stringify(api.resultVoiceKeysForFile({ voice: '女声' }))), []);
});

test('结果页头像缓存未完成时先显示目录远程资源，缓存完成后才切换本地地址', () => {
    const { api } = loadRendererConfigFunctions();
    const remoteAvatar = 'https://example.test/linda.jpg';
    const remoteSample = 'https://example.test/linda.mp3';
    api.setVoiceCatalog([{
        key: 'speaker:linda',
        name: 'Linda-品质',
        img_url: remoteAvatar,
        audio_url: remoteSample,
    }]);

    let voice = api.getResultVoiceEntry('speaker:linda');
    assert.equal(voice.img_url, remoteAvatar);
    assert.equal(voice.fallback_img_url, '');
    assert.equal(voice.audio_url, remoteSample);

    api.voiceAssetCacheReady.add('speaker:linda');
    voice = api.getResultVoiceEntry('speaker:linda');
    assert.match(voice.img_url, /\/api\/voice-assets\/speaker%3Alinda\/avatar/);
    assert.equal(voice.fallback_img_url, remoteAvatar);
    assert.match(voice.audio_url, /\/api\/voice-assets\/speaker%3Alinda\/sample/);
    assert.equal(voice.fallback_audio_url, remoteSample);
});
