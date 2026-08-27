/**
 * 小猪wordTTS 桌面设置仓库
 *
 * 设置只由 Electron 主进程读写。渲染层拿到的是深拷贝，更新采用深合并
 * 和原子替换；损坏文件会先保留为可恢复备份，再回退到默认设置。
 */

const fs = require('fs');
const path = require('path');

const SETTINGS_SCHEMA_VERSION = 1;
const DEFAULT_RECOVERY_ACCELERATOR = 'CommandOrControl+Alt+Shift+W';

const DEFAULT_SETTINGS = Object.freeze({
    schema_version: SETTINGS_SCHEMA_VERSION,
    window: {
        startup_mode: 'full',
        last_mode: 'full',
        restore_mode: 'full',
        full_bounds: null,
        compact_bounds: null,
    },
    privacy: {
        auto_pause_on_hide: true,
        auto_resume_on_restore: false,
        keep_browser_hidden: true,
        restore_on_complete: false,
        restore_on_failure: true,
    },
    browser: {
        show_on_login: true,
        hide_after_login: true,
        allow_system_chrome: false,
        default_hidden: true,
    },
    task: {
        retry_count: 1,
        operation_timeout_seconds: 120,
        keep_logs: true,
        completion_notification: true,
        open_output_dir: false,
        close_browser_on_finish: true,
        keep_history: true,
        history_limit: 20,
    },
    shortcuts: {
        recover: DEFAULT_RECOVERY_ACCELERATOR,
        privacy: 'CommandOrControl+Alt+Shift+P',
        pause_resume: 'CommandOrControl+Alt+Shift+Space',
        terminate: 'CommandOrControl+Alt+Shift+X',
        compact: 'CommandOrControl+Alt+Shift+C',
    },
    tts: {
        current_config: null,
        presets: [],
    },
    runtime: {
        active_session: null,
    },
    migrations: {
        legacy_local_storage: false,
    },
});

function clone(value) {
    return JSON.parse(JSON.stringify(value));
}

function isPlainObject(value) {
    return value && typeof value === 'object' && !Array.isArray(value);
}

function deepMerge(base, patch) {
    const result = isPlainObject(base) ? clone(base) : {};
    if (!isPlainObject(patch)) return result;
    Object.entries(patch).forEach(([key, value]) => {
        if (isPlainObject(value) && isPlainObject(result[key])) {
            result[key] = deepMerge(result[key], value);
        } else if (value !== undefined) {
            result[key] = clone(value);
        }
    });
    return result;
}

function finiteNumber(value, fallback, min, max) {
    const number = Number(value);
    if (!Number.isFinite(number)) return fallback;
    return Math.min(max, Math.max(min, Math.round(number)));
}

function coerceBoolean(value, fallback = false) {
    if (typeof value === 'boolean') return value;
    if (typeof value === 'number') return value !== 0;
    if (typeof value === 'string') {
        const normalized = value.trim().toLowerCase();
        if (['', '0', 'false', 'no', 'n', '否', '关闭'].includes(normalized)) return false;
        if (['1', 'true', 'yes', 'y', '是', '开启'].includes(normalized)) return true;
    }
    return Boolean(fallback);
}

function normalizeBounds(value) {
    if (!isPlainObject(value)) return null;
    const width = finiteNumber(value.width, 0, 1, 10000);
    const height = finiteNumber(value.height, 0, 1, 10000);
    if (!width || !height) return null;
    const result = { width, height };
    if (Number.isFinite(Number(value.x))) result.x = Math.round(Number(value.x));
    if (Number.isFinite(Number(value.y))) result.y = Math.round(Number(value.y));
    return result;
}

function normalizeAccelerator(value, fallback) {
    const text = String(value ?? '').trim();
    if (!text || text.length > 120 || /[\r\n]/.test(text)) return fallback;
    return text;
}

function normalizePresets(value) {
    if (!Array.isArray(value)) return [];
    return value.slice(0, 50).flatMap((preset) => {
        if (!isPlainObject(preset)) return [];
        const id = String(preset.id || '').trim().slice(0, 120);
        const name = String(preset.name || '').trim().slice(0, 120);
        if (!id || !name) return [];
        return [{
            ...preset,
            id,
            name,
            config: isPlainObject(preset.config) ? clone(preset.config) : {},
            created_at: Number.isFinite(Number(preset.created_at))
                ? Number(preset.created_at)
                : Date.now(),
        }];
    });
}

function normalizeSettings(raw) {
    const merged = deepMerge(DEFAULT_SETTINGS, isPlainObject(raw) ? raw : {});
    const normalized = {
        ...merged,
        schema_version: SETTINGS_SCHEMA_VERSION,
        window: {
            ...DEFAULT_SETTINGS.window,
            ...merged.window,
            startup_mode: ['full', 'compact'].includes(merged.window?.startup_mode)
                ? merged.window.startup_mode : DEFAULT_SETTINGS.window.startup_mode,
            last_mode: ['full', 'compact'].includes(merged.window?.last_mode)
                ? merged.window.last_mode : DEFAULT_SETTINGS.window.last_mode,
            restore_mode: ['full', 'compact'].includes(merged.window?.restore_mode)
                ? merged.window.restore_mode : DEFAULT_SETTINGS.window.restore_mode,
            full_bounds: normalizeBounds(merged.window?.full_bounds),
            compact_bounds: normalizeBounds(merged.window?.compact_bounds),
        },
        privacy: {
            ...DEFAULT_SETTINGS.privacy,
            ...merged.privacy,
        },
        browser: {
            ...DEFAULT_SETTINGS.browser,
            ...merged.browser,
        },
        task: {
            ...DEFAULT_SETTINGS.task,
            ...merged.task,
            retry_count: finiteNumber(merged.task?.retry_count, 1, 0, 10),
            operation_timeout_seconds: finiteNumber(
                merged.task?.operation_timeout_seconds,
                120,
                10,
                3600,
            ),
            history_limit: finiteNumber(merged.task?.history_limit, 20, 1, 20),
        },
        shortcuts: {
            ...DEFAULT_SETTINGS.shortcuts,
            ...merged.shortcuts,
            recover: normalizeAccelerator(
                merged.shortcuts?.recover,
                DEFAULT_RECOVERY_ACCELERATOR,
            ),
            privacy: normalizeAccelerator(
                merged.shortcuts?.privacy,
                DEFAULT_SETTINGS.shortcuts.privacy,
            ),
            pause_resume: normalizeAccelerator(
                merged.shortcuts?.pause_resume,
                DEFAULT_SETTINGS.shortcuts.pause_resume,
            ),
            terminate: normalizeAccelerator(
                merged.shortcuts?.terminate,
                DEFAULT_SETTINGS.shortcuts.terminate,
            ),
            compact: normalizeAccelerator(
                merged.shortcuts?.compact,
                DEFAULT_SETTINGS.shortcuts.compact,
            ),
        },
        tts: {
            ...DEFAULT_SETTINGS.tts,
            ...merged.tts,
            current_config: isPlainObject(merged.tts?.current_config)
                ? clone(merged.tts.current_config) : null,
            presets: normalizePresets(merged.tts?.presets),
        },
        runtime: {
            ...DEFAULT_SETTINGS.runtime,
            ...merged.runtime,
            active_session: isPlainObject(merged.runtime?.active_session)
                ? clone(merged.runtime.active_session) : null,
        },
        migrations: {
            ...DEFAULT_SETTINGS.migrations,
            ...merged.migrations,
            legacy_local_storage: coerceBoolean(
                merged.migrations?.legacy_local_storage,
                DEFAULT_SETTINGS.migrations.legacy_local_storage,
            ),
        },
    };

    ['auto_pause_on_hide', 'auto_resume_on_restore', 'keep_browser_hidden', 'restore_on_complete', 'restore_on_failure']
        .forEach((key) => { normalized.privacy[key] = coerceBoolean(normalized.privacy[key], DEFAULT_SETTINGS.privacy[key]); });
    ['show_on_login', 'hide_after_login', 'allow_system_chrome', 'default_hidden']
        .forEach((key) => { normalized.browser[key] = coerceBoolean(normalized.browser[key], DEFAULT_SETTINGS.browser[key]); });
    ['keep_logs', 'completion_notification', 'open_output_dir', 'close_browser_on_finish', 'keep_history']
        .forEach((key) => { normalized.task[key] = coerceBoolean(normalized.task[key], DEFAULT_SETTINGS.task[key]); });
    normalized.migrations.legacy_local_storage = coerceBoolean(
        normalized.migrations.legacy_local_storage,
        DEFAULT_SETTINGS.migrations.legacy_local_storage,
    );

    return normalized;
}

function createSettingsStore(settingsPath, dependencies = {}) {
    const io = {
        fs: dependencies.fs || fs,
        path: dependencies.path || path,
    };
    const filePath = io.path.resolve(settingsPath);
    let settings = null;

    const writeAtomic = (value) => {
        const directory = io.path.dirname(filePath);
        io.fs.mkdirSync(directory, { recursive: true });
        const temporaryPath = `${filePath}.${process.pid}.tmp`;
        const payload = `${JSON.stringify(value, null, 2)}\n`;
        io.fs.writeFileSync(temporaryPath, payload, { encoding: 'utf8', mode: 0o600 });
        io.fs.renameSync(temporaryPath, filePath);
    };

    const load = () => {
        if (settings) return clone(settings);
        try {
            const raw = io.fs.readFileSync(filePath, 'utf8');
            settings = normalizeSettings(JSON.parse(raw));
        } catch (error) {
            settings = normalizeSettings(DEFAULT_SETTINGS);
            try {
                if (io.fs.existsSync(filePath)) {
                    const backupPath = `${filePath}.corrupt-${Date.now()}`;
                    io.fs.renameSync(filePath, backupPath);
                }
                writeAtomic(settings);
            } catch (_) {
                // 读写权限问题不能阻止应用启动；内存中的默认设置仍可用。
            }
        }
        return clone(settings);
    };

    const get = () => clone(settings || load());

    const update = (patch) => {
        const previous = clone(settings || load());
        const next = normalizeSettings(deepMerge(previous, patch));
        try {
            writeAtomic(next);
            settings = next;
        } catch (error) {
            // 原子写入失败时，内存状态也必须回滚；否则当前运行期间看到的
            // 设置与磁盘内容不一致，后续快捷键/窗口状态会继续漂移。
            settings = previous;
            throw error;
        }
        return clone(settings);
    };

    const replace = (value) => {
        const previous = clone(settings || load());
        const next = normalizeSettings(value);
        try {
            writeAtomic(next);
            settings = next;
        } catch (error) {
            settings = previous;
            throw error;
        }
        return clone(settings);
    };

    const reset = () => replace(DEFAULT_SETTINGS);

    return {
        filePath,
        load,
        get,
        update,
        replace,
        reset,
        normalize: normalizeSettings,
    };
}

module.exports = {
    DEFAULT_RECOVERY_ACCELERATOR,
    DEFAULT_SETTINGS,
    SETTINGS_SCHEMA_VERSION,
    createSettingsStore,
    normalizeSettings,
};
