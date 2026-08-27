const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
    DEFAULT_RECOVERY_ACCELERATOR,
    createSettingsStore,
    normalizeSettings,
} = require('../settings');

function temporarySettingsPath() {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'wordtts-settings-'));
    return {
        directory,
        filePath: path.join(directory, 'settings.json'),
    };
}

test('settings store returns safe defaults and preserves schema', () => {
    const { directory, filePath } = temporarySettingsPath();
    try {
        const store = createSettingsStore(filePath);
        const settings = store.load();
        assert.equal(settings.schema_version, 1);
        assert.equal(settings.shortcuts.recover, DEFAULT_RECOVERY_ACCELERATOR);
        assert.equal(settings.window.last_mode, 'full');
        assert.equal(settings.privacy.auto_pause_on_hide, true);
        assert.equal(fs.existsSync(filePath), true);
    } finally {
        fs.rmSync(directory, { recursive: true, force: true });
    }
});

test('settings update is a deep merge and writes atomically', () => {
    const { directory, filePath } = temporarySettingsPath();
    try {
        const store = createSettingsStore(filePath);
        store.load();
        const updated = store.update({
            window: { last_mode: 'compact' },
            shortcuts: { compact: 'CommandOrControl+K' },
        });
        assert.equal(updated.window.last_mode, 'compact');
        assert.equal(updated.window.restore_mode, 'full');
        assert.equal(updated.shortcuts.compact, 'CommandOrControl+K');
        assert.equal(updated.shortcuts.privacy, 'CommandOrControl+Alt+Shift+P');
        assert.equal(fs.existsSync(`${filePath}.${process.pid}.tmp`), false);
        const reloaded = createSettingsStore(filePath).load();
        assert.deepEqual(reloaded, updated);
    } finally {
        fs.rmSync(directory, { recursive: true, force: true });
    }
});

test('corrupt settings are backed up and replaced with defaults', () => {
    const { directory, filePath } = temporarySettingsPath();
    try {
        fs.writeFileSync(filePath, '{not-json', 'utf8');
        const store = createSettingsStore(filePath);
        const settings = store.load();
        assert.equal(settings.schema_version, 1);
        const backups = fs.readdirSync(directory).filter(name => name.startsWith('settings.json.corrupt-'));
        assert.equal(backups.length, 1);
        assert.doesNotThrow(() => JSON.parse(fs.readFileSync(filePath, 'utf8')));
    } finally {
        fs.rmSync(directory, { recursive: true, force: true });
    }
});

test('normalization rejects invalid window bounds without throwing', () => {
    const settings = normalizeSettings({
        window: { full_bounds: { width: 'bad', height: 0 }, compact_bounds: { width: 480, height: 600 } },
        task: { retry_count: 999, operation_timeout_seconds: 1 },
    });
    assert.equal(settings.window.full_bounds, null);
    assert.deepEqual(settings.window.compact_bounds, { width: 480, height: 600 });
    assert.equal(settings.task.retry_count, 10);
    assert.equal(settings.task.operation_timeout_seconds, 10);
});

test('normalization treats persisted string booleans safely', () => {
    const settings = normalizeSettings({
        privacy: { auto_pause_on_hide: 'false' },
        browser: { default_hidden: '0' },
        task: { keep_history: 'no' },
        migrations: { legacy_local_storage: 'false' },
    });

    assert.equal(settings.privacy.auto_pause_on_hide, false);
    assert.equal(settings.browser.default_hidden, false);
    assert.equal(settings.task.keep_history, false);
    assert.equal(settings.migrations.legacy_local_storage, false);
});

test('failed atomic settings update rolls back in-memory state', () => {
    const { directory, filePath } = temporarySettingsPath();
    const originalRename = fs.renameSync;
    try {
        const store = createSettingsStore(filePath);
        store.load();
        fs.renameSync = () => { throw new Error('rename failed'); };

        assert.throws(
            () => store.update({ window: { last_mode: 'compact' } }),
            /rename failed/,
        );
        assert.equal(store.get().window.last_mode, 'full');
    } finally {
        fs.renameSync = originalRename;
        fs.rmSync(directory, { recursive: true, force: true });
    }
});
