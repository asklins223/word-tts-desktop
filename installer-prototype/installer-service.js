'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const fsp = fs.promises;
const os = require('node:os');
const path = require('node:path');
const { execFileSync, spawn } = require('node:child_process');

const PRODUCT_NAME = '小猪wordTTS';
const APP_EXECUTABLE = '小猪wordTTS.exe';
const UNINSTALLER_EXECUTABLE = '小猪wordTTS-uninstaller.exe';
const INSTALL_STATE_FILE = 'install-state.json';
const INSTALL_STATE_VERSION = 1;
const REGISTRY_SUBKEY = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\WordTTS';
const REGISTRY_INSTALL_LOCATION_VALUE = 'InstallLocation';
const LEGACY_REGISTRY_SUBKEYS = [
    REGISTRY_SUBKEY,
    'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\com.wordtts.desktop',
];
const NUMERIC_IDENTIFIER = '(?:0|[1-9]\\d*)';
const NON_NUMERIC_IDENTIFIER = '(?:[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)';
const PRERELEASE_IDENTIFIER = `(?:${NUMERIC_IDENTIFIER}|${NON_NUMERIC_IDENTIFIER})`;
const BUILD_IDENTIFIER = '[0-9A-Za-z-]+';
const VERSION_PATTERN = new RegExp(
    `^${NUMERIC_IDENTIFIER}\\.${NUMERIC_IDENTIFIER}\\.${NUMERIC_IDENTIFIER}`
    + `(?:-${PRERELEASE_IDENTIFIER}(?:\\.${PRERELEASE_IDENTIFIER})*)?`
    + `(?:\\+${BUILD_IDENTIFIER}(?:\\.${BUILD_IDENTIFIER})*)?$`,
);

class InstallerError extends Error {
    constructor(code, message, cause = null) {
        super(message);
        this.name = 'InstallerError';
        this.code = code;
        if (cause) this.cause = cause;
    }
}

function decodeArgumentValue(value) {
    const text = String(value || '');
    if (text.startsWith('"') && text.endsWith('"')) return text.slice(1, -1);
    return text;
}

function parseInstallerArguments(argv = []) {
    const result = {
        mode: null,
        targetPath: null,
        targetVersion: null,
        planPath: null,
        elevated: false,
        autoStart: false,
        headless: false,
    };
    const values = Array.isArray(argv) ? argv.map(decodeArgumentValue) : [];
    const nextArgument = (index, option) => {
        const next = values[index + 1];
        if (!next || next.startsWith('--')) {
            throw new InstallerError('INVALID_ARGUMENT', `${option} 后面必须提供参数值。`);
        }
        return next;
    };
    for (let index = 0; index < values.length; index += 1) {
        const value = values[index];
        if (value === '--elevated') result.elevated = true;
        else if (value === '--auto-start') result.autoStart = true;
        else if (value === '--headless') result.headless = true;
        else if (value === '--mode') result.mode = nextArgument(index++, value);
        else if (value === '--target') result.targetPath = nextArgument(index++, value);
        else if (value === '--target-version') result.targetVersion = nextArgument(index++, value);
        else if (value === '--plan') result.planPath = nextArgument(index++, value);
        else if (value.startsWith('--mode=')) result.mode = value.slice('--mode='.length) || nextArgument(index, '--mode');
        else if (value.startsWith('--target=')) result.targetPath = value.slice('--target='.length) || nextArgument(index, '--target');
        else if (value.startsWith('--target-version=')) result.targetVersion = value.slice('--target-version='.length) || nextArgument(index, '--target-version');
        else if (value.startsWith('--plan=')) result.planPath = value.slice('--plan='.length) || nextArgument(index, '--plan');
    }
    return result;
}

function normalizeVersion(value, label) {
    const text = String(value || '').trim().replace(/^v/i, '');
    if (!text) return '';
    if (!VERSION_PATTERN.test(text)) {
        throw new InstallerError('INVALID_VERSION', `${label}不是有效版本号。`);
    }
    return text;
}

function parseComparableVersion(value) {
    const text = String(value || '').trim().replace(/^v/i, '');
    if (!VERSION_PATTERN.test(text)) return null;
    const withoutBuild = text.split('+', 1)[0];
    const prereleaseIndex = withoutBuild.indexOf('-');
    const core = prereleaseIndex < 0
        ? withoutBuild
        : withoutBuild.slice(0, prereleaseIndex);
    return {
        core: core.split('.'),
        prerelease: prereleaseIndex < 0
            ? []
            : withoutBuild.slice(prereleaseIndex + 1).split('.'),
    };
}

function compareNumericIdentifiers(left, right) {
    const leftText = String(left);
    const rightText = String(right);
    if (leftText.length !== rightText.length) return leftText.length > rightText.length ? 1 : -1;
    if (leftText === rightText) return 0;
    return leftText > rightText ? 1 : -1;
}

function compareVersions(left, right) {
    const a = parseComparableVersion(left);
    const b = parseComparableVersion(right);
    if (!a || !b) return 0;
    for (let index = 0; index < a.core.length; index += 1) {
        const result = compareNumericIdentifiers(a.core[index], b.core[index]);
        if (result !== 0) return result;
    }
    if (a.prerelease.length === 0 && b.prerelease.length === 0) return 0;
    if (a.prerelease.length === 0) return 1;
    if (b.prerelease.length === 0) return -1;
    const length = Math.max(a.prerelease.length, b.prerelease.length);
    for (let index = 0; index < length; index += 1) {
        if (index >= a.prerelease.length) return -1;
        if (index >= b.prerelease.length) return 1;
        const leftPart = a.prerelease[index];
        const rightPart = b.prerelease[index];
        if (leftPart === rightPart) continue;
        const leftNumeric = /^\d+$/.test(leftPart);
        const rightNumeric = /^\d+$/.test(rightPart);
        if (leftNumeric && rightNumeric) return compareNumericIdentifiers(leftPart, rightPart);
        if (leftNumeric !== rightNumeric) return leftNumeric ? -1 : 1;
        return leftPart > rightPart ? 1 : -1;
    }
    return 0;
}

function resolveTargetVersion(appVersion, targetVersion) {
    const runtimeVersion = normalizeVersion(appVersion, '安装包版本');
    const metadataVersion = normalizeVersion(targetVersion, '更新目标版本');
    if (runtimeVersion && metadataVersion && runtimeVersion !== metadataVersion) {
        throw new InstallerError(
            'TARGET_VERSION_MISMATCH',
            `更新信息要求安装 ${metadataVersion}，但安装包实际是 ${runtimeVersion}。请重新检查更新。`,
        );
    }
    return metadataVersion || runtimeVersion;
}

function encodePowerShellCommand(script) {
    return Buffer.from(String(script), 'utf16le').toString('base64');
}

function randomSuffix() {
    return `${Date.now()}-${crypto.randomBytes(6).toString('hex')}`;
}

function existsSyncSafe(filePath) {
    try {
        return withNativeFileSystemSync(() => fs.existsSync(filePath));
    } catch (_) {
        return false;
    }
}

function withNativeFileSystemSync(operation) {
    const previousNoAsar = process.noAsar;
    process.noAsar = true;
    try {
        return operation();
    } finally {
        if (previousNoAsar === undefined) delete process.noAsar;
        else process.noAsar = previousNoAsar;
    }
}

async function withNativeFileSystem(operation) {
    const previousNoAsar = process.noAsar;
    process.noAsar = true;
    try {
        return await operation();
    } finally {
        if (previousNoAsar === undefined) delete process.noAsar;
        else process.noAsar = previousNoAsar;
    }
}

function pathEquals(left, right, platform) {
    const pathApi = platform === 'win32' ? path.win32 : path;
    const normalize = value => pathApi.normalize(String(value)).replace(/[\\/]+$/, '').toLowerCase();
    return normalize(left) === normalize(right);
}

function normalizeTargetPath(targetPath, platform = process.platform) {
    const raw = String(targetPath || '').trim();
    const pathApi = platform === 'win32' ? path.win32 : path;
    if (!raw || !pathApi.isAbsolute(raw)) {
        throw new InstallerError('INVALID_TARGET', '安装位置必须是绝对路径。');
    }
    if (/[\0\r\n"]/.test(raw)) {
        throw new InstallerError('INVALID_TARGET', '安装位置包含不支持的字符。');
    }
    const normalized = pathApi.normalize(raw);
    if (pathEquals(normalized, pathApi.parse(normalized).root, platform)) {
        throw new InstallerError('INVALID_TARGET', '不能把磁盘根目录作为安装位置。');
    }
    return normalized;
}

function validateInstallTargetPath(targetPath, platform = process.platform, environment = process.env) {
    const normalized = normalizeTargetPath(targetPath, platform);
    if (platform !== 'win32') return normalized;

    const root = path.win32.parse(normalized).root;
    const relative = path.win32.relative(root, normalized);
    // normalizeTargetPath already rejects the root itself. Keep this explicit
    // guard here as well because this function is the destructive-operation
    // boundary and should not rely on a caller having performed both checks.
    if (!relative) {
        throw new InstallerError('INVALID_TARGET', '安装位置必须是磁盘下的独立应用文件夹。');
    }

    const systemRoot = environment.SystemRoot
        || environment.WINDIR
        || path.win32.join(environment.SystemDrive || root || 'C:', 'Windows');
    if (isPathInside(normalized, systemRoot, platform)) {
        throw new InstallerError('INVALID_TARGET', '不能把 Windows 系统目录作为安装位置。');
    }

    const protectedDirectories = [
        environment.USERPROFILE,
        environment.PUBLIC,
        environment.ProgramData,
        environment.ProgramFiles,
        environment['ProgramFiles(x86)'],
    ].filter(Boolean);
    if (protectedDirectories.some(directory => pathEquals(normalized, directory, platform))) {
        throw new InstallerError('INVALID_TARGET', '请选择应用专用的子文件夹，不能直接使用系统目录。');
    }
    return normalized;
}

function isPathInside(candidate, parent, platform = process.platform) {
    const pathApi = platform === 'win32' ? path.win32 : path;
    const relative = pathApi.relative(pathApi.resolve(parent), pathApi.resolve(candidate));
    return relative === '' || (relative !== '..' && !relative.startsWith(`..${pathApi.sep}`) && !pathApi.isAbsolute(relative));
}

function parseJsonFile(filePath) {
    try {
        return withNativeFileSystemSync(() => JSON.parse(fs.readFileSync(filePath, 'utf8')));
    } catch (_) {
        return null;
    }
}

function defaultInstallPaths(environment, platform) {
    if (platform !== 'win32') {
        return [path.join(os.homedir(), 'Applications', PRODUCT_NAME)];
    }
    const localAppData = environment.LOCALAPPDATA || path.join(environment.USERPROFILE || os.homedir(), 'AppData', 'Local');
    const programFiles = environment.ProgramFiles || 'C:\\Program Files';
    return [
        path.win32.join(localAppData, 'Programs', PRODUCT_NAME),
        path.win32.join(localAppData, PRODUCT_NAME),
        path.win32.join(programFiles, PRODUCT_NAME),
        environment['ProgramFiles(x86)']
            ? path.win32.join(environment['ProgramFiles(x86)'], PRODUCT_NAME)
            : null,
    ];
}

function readRegistryInstallLocations(environment, execFileSyncImpl = execFileSync) {
    if (typeof execFileSyncImpl !== 'function') return [];
    const locations = [];
    const seen = new Set();
    for (const root of ['HKCU', 'HKLM']) {
        for (const subkey of LEGACY_REGISTRY_SUBKEYS) {
            const key = `${root}\\${subkey}`;
            try {
                const output = execFileSyncImpl(
                    'reg.exe',
                    ['query', key, '/v', REGISTRY_INSTALL_LOCATION_VALUE],
                    {
                        encoding: 'utf8',
                        windowsHide: true,
                        maxBuffer: 256 * 1024,
                        stdio: ['ignore', 'pipe', 'ignore'],
                    },
                );
                const match = String(output || '').match(
                    new RegExp(`^\\s*${REGISTRY_INSTALL_LOCATION_VALUE}\\s+REG_\\S+\\s+(.+?)\\s*$`, 'im'),
                );
                const location = match?.[1]?.trim();
                if (location && !seen.has(location.toLowerCase())) {
                    seen.add(location.toLowerCase());
                    locations.push(location);
                }
            } catch (_) {
                // A missing key or an unreadable registry hive is normal. The
                // default install locations remain available as a fallback.
            }
        }
    }
    return locations;
}

function dataDirectory(environment, platform) {
    if (platform === 'win32') {
        return path.win32.join(environment.APPDATA || path.win32.join(environment.USERPROFILE || os.homedir(), 'AppData', 'Roaming'), 'WordTTS');
    }
    if (platform === 'darwin') return path.join(os.homedir(), 'Library', 'Application Support', 'WordTTS');
    return path.join(os.homedir(), '.wordtts');
}

function findInstalledPath(candidates, platform) {
    return candidates.find(candidate => existsSyncSafe(path.join(candidate, APP_EXECUTABLE)))
        || candidates.find(candidate => existsSyncSafe(path.join(candidate, 'resources', 'app.asar')))
        || null;
}

function formatProgress(onProgress, payload) {
    if (typeof onProgress !== 'function') return;
    try {
        onProgress({
            percent: Math.min(100, Math.max(0, Math.round(Number(payload.percent) || 0))),
            phase: String(payload.phase || 'prepare'),
            stage: String(payload.stage || ''),
            file: String(payload.file || ''),
            count: String(payload.count || ''),
        });
    } catch (_) {
        // A renderer notification must never turn a completed file operation
        // into a failed install or update.
    }
}

async function pathExists(filePath) {
    try {
        await withNativeFileSystem(() => fsp.access(filePath));
        return true;
    } catch (error) {
        // ENOENT/ENOTDIR are the only states that mean the path is absent.
        // Treating EACCES, sharing violations, or malformed paths as "missing"
        // makes destructive operations continue against an unknown filesystem
        // state and can report a false successful uninstall.
        if (error?.code === 'ENOENT' || error?.code === 'ENOTDIR') return false;
        throw error;
    }
}

async function directoryHasEntries(directoryPath) {
    try {
        return (await withNativeFileSystem(() => fsp.readdir(directoryPath))).length > 0;
    } catch (error) {
        if (error.code === 'ENOENT') return false;
        throw new InstallerError('TARGET_UNAVAILABLE', '无法检查安装位置，请检查文件夹权限。', error);
    }
}

async function removePath(filePath) {
    if (!filePath || !(await pathExists(filePath))) return;
    await withNativeFileSystem(() => fsp.rm(filePath, { recursive: true, force: true, maxRetries: 5, retryDelay: 120 }));
}

async function listFiles(root) {
    return withNativeFileSystem(async () => {
        const files = [];
        const directories = [];
        async function visit(current) {
            const entries = await fsp.readdir(current, { withFileTypes: true });
            for (const entry of entries) {
                const absolute = path.join(current, entry.name);
                if (entry.isDirectory()) {
                    directories.push(absolute);
                    await visit(absolute);
                } else {
                    files.push({ absolute, relative: path.relative(root, absolute), symbolicLink: entry.isSymbolicLink() });
                }
            }
        }
        await visit(root);
        return { files, directories };
    });
}

async function copyTree(source, destination, onProgress, isCancelled) {
    if (!(await pathExists(source))) throw new InstallerError('PAYLOAD_MISSING', '安装包内缺少应用文件。');
    const inventory = await listFiles(source);
    await fsp.mkdir(destination, { recursive: true });
    for (const directory of inventory.directories) {
        const relative = path.relative(source, directory);
        await fsp.mkdir(path.join(destination, relative), { recursive: true });
    }
    let copied = 0;
    const total = Math.max(1, inventory.files.length);
    for (const file of inventory.files) {
        if (typeof isCancelled === 'function' && isCancelled()) {
            throw new InstallerError('CANCELLED', '操作已取消。');
        }
        const destinationFile = path.join(destination, file.relative);
        await fsp.mkdir(path.dirname(destinationFile), { recursive: true });
        if (file.symbolicLink) {
            const link = await fsp.readlink(file.absolute);
            await fsp.symlink(link, destinationFile);
        } else {
            // Electron treats a path ending in app.asar as an archive unless
            // ASAR routing is disabled. The payload intentionally contains a
            // real app.asar file, so copy it through the native filesystem.
            await withNativeFileSystem(() => fsp.copyFile(file.absolute, destinationFile));
        }
        copied += 1;
        const percent = 12 + Math.round((copied / total) * 58);
        formatProgress(onProgress, {
            percent,
            phase: 'write',
            stage: '正在写入应用文件',
            file: `正在复制 ${path.basename(file.absolute)}…`,
            count: `${copied} / ${inventory.files.length || 1} 个文件`,
        });
    }
}

function execFilePromise(execFileImpl, executable, args, options = {}) {
    return new Promise((resolve, reject) => {
        execFileImpl(executable, args, options, (error, stdout, stderr) => {
            if (error) {
                error.stdout = stdout;
                error.stderr = stderr;
                reject(error);
                return;
            }
            resolve({ stdout, stderr });
        });
    });
}

function waitForChildSpawn(child) {
    if (!child || typeof child.once !== 'function') return Promise.resolve(child);
    return new Promise((resolve, reject) => {
        let settled = false;
        const onSpawn = () => {
            if (settled) return;
            settled = true;
            resolve(child);
        };
        const onError = error => {
            if (settled) return;
            settled = true;
            child.removeListener?.('spawn', onSpawn);
            reject(error);
        };
        // Keep the error listener attached after a successful spawn as well;
        // an error event without a listener would otherwise terminate the
        // short-lived installer process.
        child.once('spawn', onSpawn);
        child.once('error', onError);
    });
}

function runPowerShellDefault(script, execFileImpl) {
    return execFilePromise(
        execFileImpl,
        'powershell.exe',
        ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encodePowerShellCommand(script)],
        { windowsHide: true, maxBuffer: 1024 * 1024 },
    ).catch(error => {
        const details = [error?.stderr, error?.stdout]
            .map(value => String(value || '').trim())
            .filter(Boolean)
            .join('\n');
        if (details) error.message = `${error.message}\n${details}`;
        throw error;
    });
}

function powershellLiteral(value) {
    return `'${String(value).replace(/'/g, "''")}'`;
}

const WINDOWS_SHORTCUT_CSHARP = String.raw`
using System;
using System.Runtime.InteropServices;

namespace WordTtsInstaller
{
    [ComImport]
    [Guid("000214F9-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IShellLinkW
    {
        void GetPath([Out] System.Text.StringBuilder pszFile, int cch, IntPtr pfd, uint fFlags);
        void GetIDList(out IntPtr ppidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out] System.Text.StringBuilder pszName, int cch);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetWorkingDirectory([Out] System.Text.StringBuilder pszDir, int cch);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
        void GetArguments([Out] System.Text.StringBuilder pszArgs, int cch);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
        void GetHotkey(out ushort pwHotkey);
        void SetHotkey(ushort wHotkey);
        void GetShowCmd(out int piShowCmd);
        void SetShowCmd(int piShowCmd);
        void GetIconLocation([Out] System.Text.StringBuilder pszIconPath, int cch, out int piIcon);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);
        void Resolve(IntPtr hwnd, uint fFlags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
    }

    [ComImport]
    [Guid("0000010B-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPersistFile
    {
        void GetClassID(out Guid pClassID);
        [PreserveSig] int IsDirty();
        void Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, uint dwMode);
        void Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, bool fRemember);
        void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);
        void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);
    }

    public static class ShortcutWriter
    {
        private static readonly Guid ShellLinkClassId = new Guid("00021401-0000-0000-C000-000000000046");

        public static void Create(string shortcutPath, string targetPath, string workingDirectory, string iconPath)
        {
            var shellLinkType = Type.GetTypeFromCLSID(ShellLinkClassId, true);
            var shellLink = (IShellLinkW)Activator.CreateInstance(shellLinkType);
            try
            {
                shellLink.SetPath(targetPath);
                shellLink.SetWorkingDirectory(workingDirectory);
                shellLink.SetIconLocation(iconPath, 0);
                ((IPersistFile)shellLink).Save(shortcutPath, true);
            }
            finally
            {
                Marshal.ReleaseComObject(shellLink);
            }
        }
    }
}
`;

function shortcutPaths(environment, scope) {
    const desktop = scope === 'per-machine'
        ? path.win32.join(environment.PUBLIC || 'C:\\Users\\Public', 'Desktop')
        : path.win32.join(environment.USERPROFILE || os.homedir(), 'Desktop');
    const startMenu = scope === 'per-machine'
        ? path.win32.join(environment.ProgramData || 'C:\\ProgramData', 'Microsoft', 'Windows', 'Start Menu', 'Programs')
        : path.win32.join(environment.APPDATA || path.win32.join(environment.USERPROFILE || os.homedir(), 'AppData', 'Roaming'), 'Microsoft', 'Windows', 'Start Menu', 'Programs');
    return {
        desktop: path.win32.join(desktop, `${PRODUCT_NAME}.lnk`),
        startMenu: path.win32.join(startMenu, `${PRODUCT_NAME}.lnk`),
    };
}

function registryRoot(scope) {
    return scope === 'per-machine' ? 'HKLM:' : 'HKCU:';
}

function registryPowerShellPath(scope, suffix = REGISTRY_SUBKEY) {
    return `${registryRoot(scope)}\\${suffix.replace(/\\/g, '\\')}`;
}

function createShortcutScript(shortcutPath, executablePath, workingDirectory) {
    return [
        `$shortcutPath = ${powershellLiteral(shortcutPath)}`,
        `$targetPath = ${powershellLiteral(executablePath)}`,
        `$workingDirectory = ${powershellLiteral(workingDirectory)}`,
        'New-Item -ItemType Directory -Force -Path (Split-Path -Parent $shortcutPath) | Out-Null',
        `Add-Type -TypeDefinition @'\n${WINDOWS_SHORTCUT_CSHARP}\n'@`,
        '[WordTtsInstaller.ShortcutWriter]::Create($shortcutPath, $targetPath, $workingDirectory, $targetPath)',
    ].join('\n');
}

function registryScript(scope, state) {
    const key = registryPowerShellPath(scope);
    const values = {
        DisplayName: PRODUCT_NAME,
        DisplayVersion: state.version,
        Publisher: '小猪wordTTS',
        InstallLocation: state.installPath,
        DisplayIcon: `${state.executable},0`,
        UninstallString: `"${state.uninstaller}" --mode=uninstall --target="${state.installPath}"`,
        NoModify: 1,
        NoRepair: 1,
    };
    const lines = [`New-Item -Path ${powershellLiteral(key)} -Force | Out-Null`];
    for (const [name, value] of Object.entries(values)) {
        const type = typeof value === 'number' ? 'DWord' : 'String';
        lines.push(`New-ItemProperty -Path ${powershellLiteral(key)} -Name ${powershellLiteral(name)} -Value ${powershellLiteral(value)} -PropertyType ${type} -Force | Out-Null`);
    }
    return lines.join('\n');
}

function removeRegistryScript(scope) {
    return LEGACY_REGISTRY_SUBKEYS
        .map(suffix => `$key = ${powershellLiteral(registryPowerShellPath(scope, suffix))}; Remove-Item -LiteralPath $key -Recurse -Force -ErrorAction SilentlyContinue`)
        // PowerShell can preserve a non-zero native exit code after a
        // SilentlyContinue registry miss. Missing legacy entries are already
        // the desired end state, so make that outcome explicit to callers.
        .concat('exit 0')
        .join('\n');
}

function cleanupScript(targetPath, scriptPath, pid) {
    const logPath = `${scriptPath}.log`;
    return [
        `$target = ${powershellLiteral(targetPath)}`,
        `$script = ${powershellLiteral(scriptPath)}`,
        `$log = ${powershellLiteral(logPath)}`,
        `$processId = ${Number(pid) || 0}`,
        '$targetRoot = ([IO.Path]::GetFullPath($target)).TrimEnd(\'\\\') + \'\\\'',
        'function Write-CleanupLog([string]$message) {',
        '  try { Add-Content -LiteralPath $log -Value ((Get-Date -Format o) + " " + $message) -ErrorAction SilentlyContinue } catch {}',
        '}',
        'Write-CleanupLog ("cleanup started target=" + $target + " pid=" + $processId)',
        'for ($attempt = 0; $attempt -lt 240; $attempt++) {',
        '  Start-Sleep -Milliseconds 500',
        '  if ($processId -gt 0) {',
        '    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue',
        '    # Do not use taskkill /T here: the cleanup process is spawned by',
        '    # the uninstaller and would be killed together with its parent.',
        '  }',
        '  # Chromium/Python helpers can outlive the Electron parent. Only stop',
        '  # processes whose executable is inside this install, never by name.',
        '  if (($attempt -lt 20) -or (($attempt % 10) -eq 0)) {',
        '    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue',
        '    foreach ($process in $processes) {',
        '      $processPath = [string]$process.ExecutablePath',
        '      if (-not $processPath) { continue }',
        '      try { $fullProcessPath = [IO.Path]::GetFullPath($processPath) } catch { continue }',
        '      if ($fullProcessPath.StartsWith($targetRoot, [StringComparison]::OrdinalIgnoreCase)) {',
        '        $childProcessId = [int]$process.ProcessId',
        '        if ($childProcessId -eq $PID) { continue }',
        '        Stop-Process -Id $childProcessId -Force -ErrorAction SilentlyContinue',
        '      }',
        '    }',
        '  }',
        '  # rmdir is less prone than the PowerShell provider to leaving a',
        '  # large Chromium/Python tree half-removed after a sharing violation.',
        '  & cmd.exe /d /c "rmdir /s /q `"$target`"" 2>$null | Out-Null',
        '  Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue',
        '  if (-not (Test-Path -LiteralPath $target)) {',
        '    Write-CleanupLog ("cleanup complete attempt=" + $attempt)',
        '    break',
        '  }',
        '  if (($attempt % 10) -eq 0) { Write-CleanupLog ("target still exists attempt=" + $attempt) }',
        '}',
        'if (-not (Test-Path -LiteralPath $target)) {',
        '  Start-Process -FilePath "cmd.exe" -ArgumentList @("/d", "/c", "del /f /q `"$script`" `"$log`"") -WindowStyle Hidden -Wait',
        '}',
    ].join('\n');
}

function createInstallerService(options = {}) {
    const platform = options.platform || process.platform;
    const environment = options.environment || process.env;
    const resourcesPath = options.resourcesPath || process.resourcesPath || __dirname;
    const setupExecutable = options.setupExecutable || null;
    const tempDirectory = options.tempDirectory || os.tmpdir();
    const userDataPath = options.dataPath || dataDirectory(environment, platform);
    const execFileImpl = options.execFile || require('node:child_process').execFile;
    const execFileSyncImpl = options.execFileSync || execFileSync;
    const runPowerShell = options.runPowerShell || (script => runPowerShellDefault(script, execFileImpl));
    const spawnImpl = options.spawn || spawn;
    const payloadPath = options.payloadPath || path.join(resourcesPath, 'payload');
    let lastPlan = null;

    function validateServiceTargetPath(targetPath) {
        const normalized = validateInstallTargetPath(targetPath, platform, environment);
        if (isPathInside(normalized, userDataPath, platform)
            || isPathInside(userDataPath, normalized, platform)) {
            throw new InstallerError('INVALID_TARGET', '安装位置不能与小猪的个人数据目录重合或互相包含。');
        }
        return normalized;
    }

    function targetCandidates() {
        const candidates = [];
        const add = value => {
            if (!value) return;
            const normalized = platform === 'win32' ? path.win32.normalize(String(value)) : path.normalize(String(value));
            // Registry values are external input. Ignore malformed or
            // protected locations during auto-detection so one stale key
            // cannot make the installer fail before it reaches a valid
            // default location.
            try { validateServiceTargetPath(normalized); } catch (_) { return; }
            if (!candidates.some(item => pathEquals(item, normalized, platform))) candidates.push(normalized);
        };
        add(options.targetPath);
        if (lastPlan?.targetPath) add(lastPlan.targetPath);
        if (platform === 'win32') {
            for (const candidate of readRegistryInstallLocations(environment, execFileSyncImpl)) add(candidate);
        }
        for (const candidate of defaultInstallPaths(environment, platform)) add(candidate);
        return candidates;
    }

    function detectInstalledPath() {
        return findInstalledPath(targetCandidates(), platform);
    }

    function readState(targetPath) {
        const normalized = normalizeTargetPath(targetPath, platform);
        const state = parseJsonFile(path.join(normalized, INSTALL_STATE_FILE));
        if (!state || state.format !== INSTALL_STATE_VERSION || state.product !== PRODUCT_NAME) return null;
        if (!state.installPath || !pathEquals(state.installPath, normalized, platform)) return null;
        if (!['per-user', 'per-machine'].includes(state.scope)) return null;
        let version;
        try {
            version = normalizeVersion(state.version, '已安装版本');
        } catch (_) {
            return null;
        }
        // Only the service may derive executable/data paths.  The state file
        // lives in a user-writable install directory, so returning arbitrary
        // path fields from it would let a malformed file redirect rollback
        // shortcuts or the uninstall registry entry outside this install.
        const storedShortcuts = state.shortcuts && typeof state.shortcuts === 'object'
            ? state.shortcuts
            : {};
        return {
            ...state,
            version,
            installPath: normalized,
            executable: path.join(normalized, APP_EXECUTABLE),
            uninstaller: path.join(normalized, UNINSTALLER_EXECUTABLE),
            dataPath: userDataPath,
            shortcuts: {
                desktop: storedShortcuts.desktop !== false,
                startMenu: storedShortcuts.startMenu !== false,
            },
        };
    }

    function getConfig({ appVersion, arguments: args = {}, operationPlan = null } = {}) {
        const plan = operationPlan && typeof operationPlan === 'object' ? operationPlan : null;
        const portableFile = environment.PORTABLE_EXECUTABLE_FILE || process.execPath;
        const inferredExecutable = String(portableFile || '').toLowerCase();
        const inferredUninstall = inferredExecutable.endsWith(UNINSTALLER_EXECUTABLE.toLowerCase());
        const explicitMode = plan?.mode || args.mode || null;
        const targetVersion = resolveTargetVersion(
            appVersion || options.version,
            plan?.targetVersion || args.targetVersion,
        );
        const executablePath = portableFile;
        const detected = plan?.targetPath || args.targetPath || (inferredUninstall ? path.dirname(executablePath) : null) || detectInstalledPath();
        const defaults = defaultInstallPaths(environment, platform).filter(Boolean);
        const fallback = defaults[0];
        const targetPath = validateServiceTargetPath(detected || fallback);
        const state = readState(targetPath);
        const installed = Boolean(
            state
            || existsSyncSafe(path.join(targetPath, APP_EXECUTABLE))
            || existsSyncSafe(path.join(targetPath, 'resources', 'app.asar')),
        );
        const scope = plan?.scope || state?.scope || (platform === 'win32' && targetPath.toLowerCase().includes('program files') ? 'per-machine' : 'per-user');
        const detectedMode = inferredUninstall ? 'uninstall' : installed ? 'update' : 'install';
        const mode = ['install', 'update', 'uninstall'].includes(explicitMode) ? explicitMode : detectedMode;
        const fixedMode = Boolean(explicitMode || inferredUninstall || installed);
        const sourcePlan = plan || null;
        lastPlan = sourcePlan;
        return {
            productName: PRODUCT_NAME,
            // On an update, targetVersion comes from latest-win.json and is
            // checked against the Setup executable's own app version above.
            version: targetVersion,
            targetVersion,
            installedVersion: String(state?.version || (installed ? '已安装' : '未安装')),
            architecture: platform === 'win32' ? 'Windows x64' : platform,
            platform,
            mode,
            fixedMode,
            autoStart: Boolean(args.autoStart || sourcePlan?.autoStart),
            targetPath,
            defaultTargetPaths: {
                perUser: defaults[0] || targetPath,
                perMachine: defaults[2] || defaults[0] || targetPath,
            },
            scope: scope === 'per-machine' ? 'per-machine' : 'per-user',
            installed,
            allowedModes: [mode],
            payloadPath,
            dataPath: userDataPath,
            shortcuts: state?.shortcuts || { desktop: true, startMenu: true },
            setupExecutable,
            operationPlan: sourcePlan,
        };
    }

    async function closeRunningApp(targetPath) {
        if (platform !== 'win32') return;
        const executable = path.win32.join(targetPath, APP_EXECUTABLE);
        if (!existsSyncSafe(executable)) return;
        const stopMatchingProcesses = force => runPowerShell([
            `$target = ${powershellLiteral(executable)}`,
            `$processes = Get-CimInstance Win32_Process -Filter ${powershellLiteral(`Name = '${APP_EXECUTABLE}'`)} -ErrorAction SilentlyContinue`,
            'foreach ($process in $processes) {',
            '  $processPath = [string]$process.ExecutablePath',
            '  if ($processPath -and [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetFullPath($processPath), [IO.Path]::GetFullPath($target))) {',
            `    Stop-Process -Id ([int]$process.ProcessId)${force ? ' -Force' : ''} -ErrorAction SilentlyContinue`,
            '  }',
            '}',
        ].join('\n'));
        // Match by the executable's full path instead of /IM alone. A user
        // may have a portable copy or another installation open elsewhere;
        // an uninstaller must never terminate those unrelated processes.
        try { await stopMatchingProcesses(false); } catch (_) { /* no running instance is normal */ }
        await new Promise(resolve => setTimeout(resolve, 700));
        try { await stopMatchingProcesses(true); } catch (_) { /* the process may already be gone */ }
    }

    async function stageSetupExecutable() {
        if (!setupExecutable || !(await pathExists(setupExecutable))) return null;
        await fsp.mkdir(tempDirectory, { recursive: true });
        const staged = path.join(tempDirectory, `wordtts-setup-${randomSuffix()}.exe`);
        try {
            await fsp.copyFile(setupExecutable, staged);
        } catch (error) {
            // copyFile can leave a partial destination when the source is
            // locked or the temp volume runs out of space. Do not accumulate
            // a misleading setup executable in the shared temp directory.
            await fsp.rm(staged, { force: true }).catch(() => {});
            throw error;
        }
        return staged;
    }

    async function replaceApplication({ targetPath, mode, scope, version, desktopShortcut, startMenuShortcut, refreshShortcuts = true, onProgress, isCancelled }) {
        const normalizedTarget = validateServiceTargetPath(targetPath);
        const stagingPath = `${normalizedTarget}.staging-${randomSuffix()}`;
        const backupPath = `${normalizedTarget}.backup-${randomSuffix()}`;
        let targetStat = null;
        try {
            targetStat = await fsp.stat(normalizedTarget);
        } catch (error) {
            if (error.code !== 'ENOENT') {
                throw new InstallerError('TARGET_UNAVAILABLE', '无法访问安装位置，请检查文件夹权限。', error);
            }
        }
        if (targetStat && !targetStat.isDirectory()) {
            throw new InstallerError('INVALID_TARGET', '安装位置必须是文件夹。');
        }
        const oldState = readState(normalizedTarget);
        if (mode === 'update' && oldState && compareVersions(version, oldState.version) <= 0) {
            throw new InstallerError(
                'NOT_NEWER_VERSION',
                `当前安装版本是 ${oldState.version}，不能用 ${version} 覆盖或降级。`,
            );
        }
        const setupCopy = await stageSetupExecutable();
        try {
            formatProgress(onProgress, { percent: 4, phase: 'prepare', stage: '正在准备应用文件', file: '正在检查安装包…', count: '准备中' });
            const hasInstalledApplication = await pathExists(path.join(normalizedTarget, APP_EXECUTABLE))
                || await pathExists(path.join(normalizedTarget, 'resources', 'app.asar'));
            if (mode === 'update' && !oldState && !hasInstalledApplication) {
                throw new InstallerError('NOT_INSTALLED', '目标位置没有可更新的小猪wordTTS安装。');
            }
            if (mode === 'install' && (await pathExists(path.join(normalizedTarget, APP_EXECUTABLE)))) {
                throw new InstallerError('ALREADY_INSTALLED', '目标文件夹中已经安装了小猪wordTTS，请选择更新。');
            }
            if (mode === 'install'
                && await pathExists(normalizedTarget)
                && await directoryHasEntries(normalizedTarget)) {
                throw new InstallerError('TARGET_NOT_EMPTY', '安装位置不是空文件夹，请选择新的位置或使用更新。');
            }
            await fsp.mkdir(path.dirname(normalizedTarget), { recursive: true });
            await removePath(stagingPath);
            await copyTree(payloadPath, stagingPath, onProgress, isCancelled);
            if (!(await pathExists(path.join(stagingPath, APP_EXECUTABLE)))) {
                throw new InstallerError('PAYLOAD_INVALID', `安装包中未找到 ${APP_EXECUTABLE}。`);
            }
            const state = {
                format: INSTALL_STATE_VERSION,
                product: PRODUCT_NAME,
                version,
                scope,
                installPath: normalizedTarget,
                executable: path.join(normalizedTarget, APP_EXECUTABLE),
                uninstaller: path.join(normalizedTarget, UNINSTALLER_EXECUTABLE),
                dataPath: userDataPath,
                shortcuts: !refreshShortcuts && mode === 'update'
                    ? (oldState?.shortcuts || { desktop: true, startMenu: true })
                    : { desktop: Boolean(desktopShortcut), startMenu: Boolean(startMenuShortcut) },
                installedAt: new Date().toISOString(),
            };
            await fsp.writeFile(path.join(stagingPath, INSTALL_STATE_FILE), `${JSON.stringify(state, null, 2)}\n`, 'utf8');
            if (setupCopy) await fsp.copyFile(setupCopy, path.join(stagingPath, UNINSTALLER_EXECUTABLE));

            if (typeof isCancelled === 'function' && isCancelled()) {
                throw new InstallerError('CANCELLED', '操作已取消。');
            }
            await closeRunningApp(normalizedTarget);
            if (typeof isCancelled === 'function' && isCancelled()) {
                throw new InstallerError('CANCELLED', '操作已取消。');
            }
            const hasExisting = await pathExists(normalizedTarget);
            let backupMoved = false;
            let newTargetMoved = false;
            try {
                if (hasExisting) {
                    await fsp.rename(normalizedTarget, backupPath);
                    backupMoved = true;
                }
                try {
                    await fsp.rename(stagingPath, normalizedTarget);
                    newTargetMoved = true;
                } catch (error) {
                    if (backupMoved && await pathExists(backupPath)) {
                        await fsp.rename(backupPath, normalizedTarget).then(
                            () => { backupMoved = false; },
                            () => {},
                        );
                    }
                    throw new InstallerError('REPLACE_FAILED', '无法替换旧应用文件，请关闭小猪wordTTS后重试。', error);
                }
                formatProgress(onProgress, { percent: 76, phase: 'shortcut', stage: '正在创建快捷方式', file: '正在准备应用入口…', count: '最后一步' });
                if (platform === 'win32' && refreshShortcuts) {
                    await syncShortcuts(state, { desktopShortcut, startMenuShortcut });
                    await runPowerShell(registryScript(scope, state));
                } else if (platform === 'win32') {
                    await runPowerShell(registryScript(scope, state));
                }
                if (backupMoved) {
                    await removePath(backupPath);
                    backupMoved = false;
                }
                formatProgress(onProgress, { percent: 94, phase: 'finish', stage: '正在完成设置', file: '正在保存安装信息…', count: '即将完成' });
                return { success: true, state, previousState: oldState };
            } catch (error) {
                // The directory swap and shell integration form one logical
                // operation. If either part fails, remove the new target and
                // restore the old target when a backup exists. Keeping the
                // known-good backup is safer than reporting success with a
                // half-updated application.
                if (newTargetMoved) {
                    if (platform === 'win32') {
                        try {
                            await syncShortcuts(state, { desktopShortcut: false, startMenuShortcut: false });
                            await runPowerShell(removeRegistryScript(scope));
                        } catch (_) { /* best-effort cleanup of new shell entries */ }
                    }
                    await removePath(normalizedTarget).catch(() => {});
                    if (backupMoved && await pathExists(backupPath)) {
                        try {
                            await fsp.rename(backupPath, normalizedTarget);
                            backupMoved = false;
                        } catch (_) { /* retain the backup for manual recovery */ }
                    }
                    if (platform === 'win32' && oldState && await pathExists(normalizedTarget)) {
                        try {
                            await syncShortcuts(oldState, oldState.shortcuts || { desktop: true, startMenu: true });
                            await runPowerShell(registryScript(oldState.scope, oldState));
                        } catch (_) { /* best-effort rollback of shell integration */ }
                    }
                }
                throw error;
            } finally {
                if (!backupMoved) await removePath(backupPath).catch(() => {});
            }
        } finally {
            await removePath(stagingPath).catch(() => {});
            if (setupCopy) await removePath(setupCopy).catch(() => {});
        }
    }

    async function syncShortcuts(state, { desktopShortcut, startMenuShortcut }) {
        const links = shortcutPaths(environment, state.scope);
        if (desktopShortcut) {
            await runPowerShell(createShortcutScript(links.desktop, state.executable, state.installPath));
        } else {
            await removePath(links.desktop);
        }
        if (startMenuShortcut) {
            await runPowerShell(createShortcutScript(links.startMenu, state.executable, state.installPath));
        } else {
            await removePath(links.startMenu);
        }
    }

    async function scheduleUninstallCleanup(targetPath) {
        const scriptPath = path.join(tempDirectory, `wordtts-uninstall-${randomSuffix()}.ps1`);
        const script = cleanupScript(targetPath, scriptPath, process.pid);
        await fsp.writeFile(scriptPath, script, 'utf8');
        let child;
        try {
            child = spawnImpl(
                platform === 'win32' ? 'powershell.exe' : process.execPath,
                platform === 'win32'
                    // Windows PowerShell can interpret a UTF-8 script without
                    // a BOM using the active ANSI code page. Encode the
                    // command as UTF-16LE so Chinese install paths survive
                    // the handoff regardless of the system locale.
                    ? ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encodePowerShellCommand(script)]
                    : ['-e', ''],
                {
                    detached: true,
                    stdio: 'ignore',
                    windowsHide: true,
                    // The uninstaller is launched with its install directory
                    // as the working directory. Letting the detached cleanup
                    // process inherit that directory keeps a Windows handle
                    // open on the directory we are trying to remove, so the
                    // final rmdir can never succeed. Run from the temp folder
                    // that contains the cleanup script instead.
                    cwd: tempDirectory,
                },
            );
            if (!child || typeof child !== 'object') {
                throw new Error('卸载清理进程没有成功创建。');
            }
            await waitForChildSpawn(child);
            child.unref?.();
        } catch (error) {
            await fsp.rm(scriptPath, { force: true }).catch(() => {});
            throw new InstallerError('UNINSTALL_CLEANUP_FAILED', '无法启动卸载清理进程，请重试。', error);
        }
        return scriptPath;
    }

    async function uninstall({ targetPath, scope, keepUserData = true, deleteCache = false, onProgress, isCancelled }) {
        const normalizedTarget = validateServiceTargetPath(targetPath);
        const state = readState(normalizedTarget);
        const hasApplication = await pathExists(path.join(normalizedTarget, APP_EXECUTABLE))
            || await pathExists(path.join(normalizedTarget, 'resources', 'app.asar'));
        if (!state && !hasApplication) {
            throw new InstallerError('NOT_INSTALLED', '目标位置没有可识别的小猪wordTTS安装。');
        }
        if (typeof isCancelled === 'function' && isCancelled()) {
            throw new InstallerError('CANCELLED', '操作已取消。');
        }
        const effectiveState = state || {
            scope,
            installPath: normalizedTarget,
            executable: path.join(normalizedTarget, APP_EXECUTABLE),
            uninstaller: path.join(normalizedTarget, UNINSTALLER_EXECUTABLE),
            dataPath: userDataPath,
            shortcuts: { desktop: true, startMenu: true },
        };
        formatProgress(onProgress, { percent: 6, phase: 'prepare', stage: '正在准备卸载', file: '正在检查应用文件…', count: '准备中' });
        await closeRunningApp(normalizedTarget);
        if (typeof isCancelled === 'function' && isCancelled()) {
            throw new InstallerError('CANCELLED', '操作已取消。');
        }
        if (platform === 'win32') {
            const links = shortcutPaths(environment, effectiveState.scope || scope);
            await removePath(links.desktop);
            await removePath(links.startMenu);
            await runPowerShell(removeRegistryScript(effectiveState.scope || scope));
        }
        formatProgress(onProgress, { percent: 45, phase: 'write', stage: '正在移除应用文件', file: '正在清理应用目录…', count: '正在移除' });
        formatProgress(onProgress, { percent: 78, phase: 'shortcut', stage: '正在清理快捷方式', file: '正在移除开始菜单和桌面快捷方式…', count: '最后一步' });
        let scheduledCleanup = false;
        try {
            await removePath(normalizedTarget);
        } catch (error) {
            if (platform === 'win32') {
                await scheduleUninstallCleanup(normalizedTarget);
                scheduledCleanup = true;
            } else {
                throw new InstallerError('UNINSTALL_FAILED', '无法移除应用目录，请检查文件权限。', error);
            }
        }
        if (platform === 'win32' && await pathExists(normalizedTarget)) {
            await scheduleUninstallCleanup(normalizedTarget);
            scheduledCleanup = true;
        }
        // Never trust an install-state file to redirect uninstall into an
        // arbitrary directory. The service's own data directory is the only
        // location this operation is allowed to remove. Do this only after
        // the application directory has been removed or a Windows cleanup
        // process has been started, so a failed program removal cannot first
        // destroy the user's data.
        const dataPath = userDataPath;
        try {
            if (!keepUserData) {
                await removePath(dataPath);
            } else if (deleteCache) {
                for (const cachePath of ['cache', 'Cache', 'source-staging']) {
                    await removePath(path.join(dataPath, cachePath));
                }
            }
        } catch (error) {
            throw new InstallerError('USER_DATA_CLEANUP_FAILED', '程序已经移除，但个人数据清理没有完成，请检查文件权限。', error);
        }
        formatProgress(onProgress, { percent: 96, phase: 'finish', stage: '正在完成卸载', file: '正在清理安装信息…', count: '即将完成' });
        return { success: true, scheduledCleanup, keptUserData: keepUserData, deletedCache: deleteCache };
    }

    async function run(plan, progress = {}) {
        const mode = ['install', 'update', 'uninstall'].includes(plan?.mode) ? plan.mode : null;
        if (!mode) throw new InstallerError('INVALID_OPERATION', '没有有效的安装操作。');
        const targetPath = validateServiceTargetPath(plan.targetPath);
        const scope = plan.scope === 'per-machine' ? 'per-machine' : 'per-user';
        lastPlan = { ...plan, targetPath };
        if (mode === 'uninstall') {
            return uninstall({ ...plan, targetPath, scope, onProgress: progress.onProgress, isCancelled: progress.isCancelled });
        }
        const planVersion = plan?.version || '';
        const metadataVersion = plan?.targetVersion || '';
        const version = planVersion && metadataVersion
            ? resolveTargetVersion(planVersion, metadataVersion)
            : normalizeVersion(planVersion || metadataVersion || options.version, '安装包版本');
        if (!version) throw new InstallerError('INVALID_VERSION', '安装包版本不能为空。');
        const result = await replaceApplication({ ...plan, mode, targetPath, scope, version, onProgress: progress.onProgress, isCancelled: progress.isCancelled });
        formatProgress(progress.onProgress, { percent: 100, phase: 'finish', stage: '正在完成设置', file: mode === 'update' ? '更新已完成。' : '安装已完成。', count: '完成' });
        return result;
    }

    async function launchInstalledApp(targetPath) {
        const normalizedTarget = validateServiceTargetPath(targetPath);
        const executable = path.join(normalizedTarget, APP_EXECUTABLE);
        if (!existsSyncSafe(executable)) throw new InstallerError('APP_MISSING', '未找到已安装的应用程序。');
        let child;
        try {
            child = spawnImpl(executable, [], { cwd: normalizedTarget, detached: true, stdio: 'ignore', windowsHide: true });
            if (!child || typeof child !== 'object') {
                throw new Error('应用进程没有成功创建。');
            }
            await waitForChildSpawn(child);
            child.unref?.();
        } catch (error) {
            if (error instanceof InstallerError) throw error;
            throw new InstallerError('APP_LAUNCH_FAILED', '无法启动小猪wordTTS，请从开始菜单重试。', error);
        }
    }

    return {
        getConfig,
        run,
        launchInstalledApp,
        normalizeTargetPath: target => normalizeTargetPath(target, platform),
        detectInstalledPath,
        readState,
        payloadPath,
        constants: { PRODUCT_NAME, APP_EXECUTABLE, UNINSTALLER_EXECUTABLE, INSTALL_STATE_FILE },
    };
}

module.exports = {
    APP_EXECUTABLE,
    INSTALL_STATE_FILE,
    InstallerError,
    PRODUCT_NAME,
    UNINSTALLER_EXECUTABLE,
    createInstallerService,
    decodeArgumentValue,
    encodePowerShellCommand,
    isPathInside,
    readRegistryInstallLocations,
    validateInstallTargetPath,
    normalizeTargetPath,
    parseInstallerArguments,
    compareVersions,
};
