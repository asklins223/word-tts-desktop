'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {
    PORTABLE_UNINSTALL_RELOCATION_BLOCK,
    PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK,
    DEFAULT_7Z_COMPRESSION_LEVEL,
    createBuildProject,
    describeBuildProgress,
    patchPortableTemplate,
} = require('../../scripts/build_windows_installer');

const APP_DIR = path.join(__dirname, '..');
const packageJson = JSON.parse(
    fs.readFileSync(path.join(APP_DIR, 'package.json'), 'utf8'),
);
const buildFiles = packageJson.build?.files;

test('Windows 自绘 portable 模板收尾时切换到 TEMP，不重建安装目录', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'wordtts-portable-template-'));
    const templatePath = path.join(root, 'portable.nsi');
    const original = [
        'Section',
        '  StrCpy $INSTDIR "$PLUGINSDIR\\app"',
        '  !ifdef UNPACK_DIR_NAME',
        '    StrCpy $INSTDIR "$TEMP\\${UNPACK_DIR_NAME}"',
        '  !endif',
        '  SetOutPath $EXEDIR',
        '\tRMDir /r $INSTDIR',
        'SectionEnd',
        '',
    ].join('\n');
    try {
        fs.writeFileSync(templatePath, original, 'utf8');
        const restore = patchPortableTemplate(templatePath);
        assert.match(fs.readFileSync(templatePath, 'utf8'), /SetOutPath \$TEMP/);
        assert.doesNotMatch(fs.readFileSync(templatePath, 'utf8'), /SetOutPath \$EXEDIR/);
        assert.match(fs.readFileSync(templatePath, 'utf8'), /StrCpy \$INSTDIR "\$PLUGINSDIR\\app"/);
        assert.match(fs.readFileSync(templatePath, 'utf8'), /WORDTTS_PORTABLE_UNIQUE_PLUGIN_DIR/);
        assert.doesNotMatch(
            fs.readFileSync(templatePath, 'utf8'),
            /\$TEMP\\\$\{UNPACK_DIR_NAME\}/,
        );
        assert.match(fs.readFileSync(templatePath, 'utf8'), /WORDTTS_RELOCATED_UNINSTALLER/);
        assert.match(fs.readFileSync(templatePath, 'utf8'), /wordtts_continue_portable:/);
        assert.match(fs.readFileSync(templatePath, 'utf8'), /\$EXEPATH\.cleanup\.cmd/);
        assert.match(fs.readFileSync(templatePath, 'utf8'), /wordtts_stage_cleanup_done:/);
        restore();
        assert.equal(fs.readFileSync(templatePath, 'utf8'), original);
    } finally {
        fs.rmSync(root, { recursive: true, force: true });
    }
});

test('Windows portable 外壳在 Electron 退出后启动自身清理批处理', () => {
    assert.match(PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK, /ReadEnvStr \$R0 "WORDTTS_RELOCATED_UNINSTALLER"/);
    assert.match(PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK, /StrCmp \$R0 "\$EXEPATH"/);
    assert.match(PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK, /\$EXEPATH\.cleanup\.cmd/);
    assert.match(PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK, /\$SYSDIR\\cmd\.exe/);
    assert.match(PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK, /staged cleanup failed: cmd launch error/);
    assert.doesNotMatch(PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK, /powershell/i);
});

test('自绘 portable 直接读取已完成 payload，并使用可重复启动的 7z 路径', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'wordtts-installer-project-'));
    const payloadDir = path.join(root, 'win-unpacked');
    const workDir = path.join(root, 'build-project');
    const outputDir = path.join(root, 'release');
    try {
        fs.mkdirSync(payloadDir, { recursive: true });
        fs.writeFileSync(path.join(payloadDir, '小猪wordTTS.exe'), 'fixture');
        const { packageJson } = createBuildProject({
            payloadDir,
            outputDir,
            workDir,
        });
        assert.equal(packageJson.build.compression, 'normal');
        assert.deepEqual(packageJson.build.extraResources, [
            { from: payloadDir, to: 'payload' },
        ]);
        assert.equal(packageJson.build.portable.useZip, undefined);
        assert.equal(packageJson.build.portable.unpackDirName, false);
        assert.equal(packageJson.build.win.signExts, undefined);
        assert.equal(DEFAULT_7Z_COMPRESSION_LEVEL, '5');
        assert.equal(fs.existsSync(path.join(workDir, 'payload')), false);
    } finally {
        fs.rmSync(root, { recursive: true, force: true });
    }
});

test('Windows portable 长压缩阶段报告真实归档体积和增长量', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'wordtts-installer-progress-'));
    try {
        const waiting = describeBuildProgress(root, 15_000);
        assert.equal(waiting.bytes, null);
        assert.match(waiting.message, /已运行 15秒/);
        assert.match(waiting.message, /正在准备 7z 归档/);

        const archivePath = path.join(root, 'word-tts-3.0.4-x64.nsis.7z');
        fs.writeFileSync(archivePath, Buffer.alloc(2 * 1024 * 1024));
        const compressing = describeBuildProgress(root, 65_000, 1024 * 1024);
        assert.equal(compressing.bytes, 2 * 1024 * 1024);
        assert.match(compressing.message, /7z 压缩中/);
        assert.match(compressing.message, /1分05秒/);
        assert.match(compressing.message, /已写入 2\.0 MiB/);
        assert.match(compressing.message, /本周期 \+1\.0 MiB/);

        const installerPath = path.join(root, '小猪wordTTS-Setup-3.0.4-x64.exe');
        fs.writeFileSync(installerPath, Buffer.alloc(3 * 1024 * 1024));
        const assembling = describeBuildProgress(root, 80_000, compressing.bytes);
        assert.equal(assembling.bytes, 3 * 1024 * 1024);
        assert.match(assembling.message, /NSIS 封装中/);
    } finally {
        fs.rmSync(root, { recursive: true, force: true });
    }
});

test('Windows portable 卸载外壳在解压 Electron 前先迁移到 TEMP', () => {
    assert.match(PORTABLE_UNINSTALL_RELOCATION_BLOCK, /StrCmp \$EXEFILE "小猪wordTTS-uninstaller\.exe"/);
    assert.match(PORTABLE_UNINSTALL_RELOCATION_BLOCK, /GetCurrentProcessId\(\)i\.R1/);
    assert.match(PORTABLE_UNINSTALL_RELOCATION_BLOCK, /stage-\$R1-0-0\.exe/);
    assert.match(PORTABLE_UNINSTALL_RELOCATION_BLOCK, /Kernel32::CopyFile/);
    assert.match(PORTABLE_UNINSTALL_RELOCATION_BLOCK, /WORDTTS_RELOCATION_SOURCE_PID/);
    assert.match(PORTABLE_UNINSTALL_RELOCATION_BLOCK, /--mode=uninstall/);
    assert.match(PORTABLE_UNINSTALL_RELOCATION_BLOCK, /--target="\$EXEDIR"/);
    assert.match(PORTABLE_UNINSTALL_RELOCATION_BLOCK, /--uninstall-relocated/);
    assert.ok(
        PORTABLE_UNINSTALL_RELOCATION_BLOCK.indexOf('CopyFile')
            < PORTABLE_UNINSTALL_RELOCATION_BLOCK.indexOf('Exec'),
    );
    assert.ok(
        PORTABLE_UNINSTALL_RELOCATION_BLOCK.indexOf('SetOutPath $TEMP')
            < PORTABLE_UNINSTALL_RELOCATION_BLOCK.indexOf('Exec'),
    );
    assert.ok(
        PORTABLE_UNINSTALL_RELOCATION_BLOCK.indexOf('Exec')
            < PORTABLE_UNINSTALL_RELOCATION_BLOCK.indexOf('SetErrorLevel 0'),
    );
    assert.ok(
        PORTABLE_UNINSTALL_RELOCATION_BLOCK.indexOf('SetErrorLevel 0')
            < PORTABLE_UNINSTALL_RELOCATION_BLOCK.indexOf('Quit'),
    );
});

test('Windows portable 模板即使上次构建中断也能恢复为原始内容', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'wordtts-portable-template-recovery-'));
    const templatePath = path.join(root, 'portable.nsi');
    const original = [
        'Section',
        '  StrCpy $INSTDIR "$PLUGINSDIR\\app"',
        '  !ifdef UNPACK_DIR_NAME',
        '    StrCpy $INSTDIR "$TEMP\\${UNPACK_DIR_NAME}"',
        '  !endif',
        '  SetOutPath $EXEDIR',
        '\tRMDir /r $INSTDIR',
        'SectionEnd',
        '',
    ].join('\n');
    const interrupted = original
        .replace(
            [
                '  !ifdef UNPACK_DIR_NAME',
                '    StrCpy $INSTDIR "$TEMP\\${UNPACK_DIR_NAME}"',
                '  !endif',
            ].join('\n'),
            '  ; WORDTTS_PORTABLE_UNIQUE_PLUGIN_DIR',
        )
        .replace('Section\n', `Section\n${PORTABLE_UNINSTALL_RELOCATION_BLOCK}`)
        .replace('\tRMDir /r $INSTDIR\n', `\tRMDir /r $INSTDIR\n${PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK}`)
        .replace('SetOutPath $EXEDIR', 'SetOutPath $TEMP');
    try {
        fs.writeFileSync(templatePath, interrupted, 'utf8');
        const restore = patchPortableTemplate(templatePath);
        assert.equal(fs.readFileSync(templatePath, 'utf8'), interrupted);
        restore();
        assert.equal(fs.readFileSync(templatePath, 'utf8'), original);
    } finally {
        fs.rmSync(root, { recursive: true, force: true });
    }
});

test('build.files 覆盖 main/preload 直接依赖的每一个本地模块', () => {
    assert.ok(Array.isArray(buildFiles) && buildFiles.length > 0, 'build.files must be an explicit list');

    const required = new Set();
    for (const entry of ['main.js', 'preload.js']) {
        const text = fs.readFileSync(path.join(APP_DIR, entry), 'utf8');
        for (const match of text.matchAll(/require\((['"])(\.[^'"]+)\1\)/g)) {
            required.add(match[2]);
        }
    }
    assert.ok(required.size > 0, 'expected local requires in main.js/preload.js');

    for (const specifier of required) {
        const base = specifier.replace(/^\.\//, '');
        const target = [base, `${base}.js`].find(
            (candidate) => fs.existsSync(path.join(APP_DIR, candidate)),
        );
        assert.ok(target, `required module ${specifier} does not exist in the app directory`);

        const covered = buildFiles.some((pattern) => {
            if (!pattern.includes('*')) return pattern === target;
            const [dirPart] = pattern.split('/**');
            return target === dirPart || target.startsWith(`${dirPart}/`);
        });
        assert.ok(
            covered,
            `${target} is required by main.js/preload.js but missing from build.files; `
            + 'the packaged app would crash with "Cannot find module" at startup',
        );
    }
});

test('build.files 引用的关键文件都真实存在', () => {
    for (const pattern of buildFiles) {
        if (pattern.includes('*')) {
            const [dirPart] = pattern.split('/**');
            assert.ok(
                fs.existsSync(path.join(APP_DIR, dirPart)),
                `glob root ${dirPart} must exist`,
            );
            continue;
        }
        assert.ok(
            fs.existsSync(path.join(APP_DIR, pattern)),
            `build.files entry ${pattern} does not exist`,
        );
    }
});

test('Windows 使用完整的自绘 Setup.exe，并覆盖安装、更新、卸载生命周期', () => {
    assert.equal(packageJson.build?.win?.target?.[0]?.target, 'dir');
    assert.equal(packageJson.build?.win?.target?.[0]?.arch?.[0], 'x64');
    assert.equal(packageJson.build?.nsis, undefined, 'Windows must not fall back to electron-builder NSIS');
    assert.ok(buildFiles.includes('windows-update-client.js'));

    const installerSourceDir = path.join(APP_DIR, '..', 'installer-prototype');
    for (const entry of [
        'package.json',
        'index.html',
        'styles.css',
        'app.js',
        'installer-main.js',
        'installer-preload.js',
        'installer-service.js',
        'assets/app-icon.png',
        'assets/installer-pig-doc-mic.png',
    ]) {
        assert.ok(fs.existsSync(path.join(installerSourceDir, entry)), `installer source is missing ${entry}`);
    }

    const windowsWorkflow = fs.readFileSync(
        path.join(APP_DIR, '..', '.github', 'workflows', 'build-windows.yml'),
        'utf8',
    );
    const releaseWorkflow = fs.readFileSync(
        path.join(APP_DIR, '..', '.github', 'workflows', 'build-release.yml'),
        'utf8',
    );
    const windowsBuildScript = fs.readFileSync(
        path.join(APP_DIR, '..', 'build_electron_windows.bat'),
        'utf8',
    );
    const windowsInstallerBuildScript = fs.readFileSync(
        path.join(APP_DIR, '..', 'scripts', 'build_windows_installer.js'),
        'utf8',
    );
    const installerMain = fs.readFileSync(
        path.join(installerSourceDir, 'installer-main.js'),
        'utf8',
    );
    const installerService = fs.readFileSync(
        path.join(installerSourceDir, 'installer-service.js'),
        'utf8',
    );
    assert.match(packageJson.scripts['build:win'], /electron-builder --win dir --publish never/);
    assert.match(packageJson.scripts['build:win'], /build_windows_installer\.js --payload/);
    assert.match(windowsWorkflow, /electron-builder --win dir --publish never/);
    assert.match(windowsWorkflow, /build_windows_installer\.js --payload release\/win-unpacked/);
    assert.match(windowsWorkflow, /name: Cache electron-builder toolchains/);
    assert.match(windowsWorkflow, /path: \$\{\{ runner\.temp \}\}\/electron-builder-cache/);
    assert.match(windowsWorkflow, /ELECTRON_BUILDER_CACHE: \$\{\{ runner\.temp \}\}\/electron-builder-cache/);
    assert.match(windowsWorkflow, /ELECTRON_BUILDER_7Z_FILTER: BCJ/);
    assert.match(windowsWorkflow, /ELECTRON_BUILDER_COMPRESSION_LEVEL: '5'/);
    assert.match(windowsWorkflow, /npm ci --prefer-offline --no-audit --fund=false/);
    assert.match(
        windowsWorkflow,
        /- name: Upload artifact[\s\S]*?name: 小猪wordTTS-Windows[\s\S]*?compression-level: 0/,
    );
    assert.match(windowsInstallerBuildScript, /patchPortableTemplate\(portableTemplatePath\)/);
    assert.match(windowsInstallerBuildScript, /'--win', 'portable', '--x64'/);
    assert.match(windowsInstallerBuildScript, /ELECTRON_BUILDER_7Z_FILTER: 'BCJ'/);
    assert.match(windowsInstallerBuildScript, /DEFAULT_7Z_COMPRESSION_LEVEL = '5'/);
    assert.match(windowsInstallerBuildScript, /每 .* 秒报告 7z\/NSIS 输出体积/);
    assert.match(windowsInstallerBuildScript, /unpackDirName: false/);
    assert.match(windowsInstallerBuildScript, /WORDTTS_PORTABLE_UNIQUE_PLUGIN_DIR/);
    assert.doesNotMatch(windowsInstallerBuildScript, /useZip:\s*true/);
    assert.match(windowsWorkflow, /--headless", "--mode=install"/);
    assert.match(windowsWorkflow, /--headless", "--mode=update"/);
    assert.match(windowsWorkflow, /--headless", "--mode=uninstall"/);
    assert.match(windowsWorkflow, /Remove-Item Env:ELECTRON_RUN_AS_NODE/);
    assert.match(windowsWorkflow, /Remove-Item Env:PLAYWRIGHT_BROWSERS_PATH/);
    assert.match(windowsWorkflow, /Remove-Item Env:PLAYWRIGHT_NODEJS_PATH/);
    assert.match(windowsWorkflow, /小猪wordTTS-uninstaller\.exe/);
    assert.match(installerMain, /relocateInstalledUninstaller/);
    assert.match(installerMain, /WORDTTS_RELOCATED_UNINSTALLER/);
    assert.match(installerMain, /waitForSourceWrapperExit/);
    assert.match(installerMain, /requestSingleInstanceLock/);
    assert.match(installerMain, /second-instance/);
    assert.match(installerMain, /applyForwardedArguments/);
    assert.match(installerMain, /PORTABLE_EXECUTABLE_FILE = \$installer/);
    assert.match(installerMain, /delegateToElevatedInstance\(\s*plan,\s*installerConfig/);
    assert.match(installerService, /install-location\.json/);
    assert.match(
        installerService,
        /if \(stagedExecutable\) \{[\s\S]*await removeInstallTarget\(normalizedTarget\)[\s\S]*prepareRelocatedExecutableCleanup\(stagedExecutable\)/,
    );
    assert.match(windowsInstallerBuildScript, /PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK/);
    assert.match(windowsInstallerBuildScript, /\$EXEPATH\.cleanup\.cmd/);
    assert.match(installerService, /安装目录删除后仍然存在/);
    assert.match(windowsWorkflow, /\[handoff\] NSIS relocated uninstaller active/);
    assert.match(windowsWorkflow, /\[complete\] installer operation succeeded/);
    assert.match(windowsWorkflow, /staged cleanup complete/);
    assert.match(windowsWorkflow, /TEMP 卸载外壳没有在限定时间内完成自清理/);
    assert.ok(
        (windowsWorkflow.match(/\$installerName = "小猪wordTTS-Setup-\$env:UPDATE_VERSION-x64\.exe"/g) || []).length >= 2,
        'installer smoke and artifact validation steps must bind to the current x64 setup executable',
    );
    assert.doesNotMatch(windowsWorkflow, /blockmap|latest\.yml/);
    assert.doesNotMatch(windowsWorkflow, /-Filter "\*-Setup-\*\.exe"/);
    assert.doesNotMatch(windowsWorkflow, /Get-ChildItem -Path "electron\/release" -Filter "\*\.exe"/);
    assert.match(windowsBuildScript, /electron-builder --win dir --publish never/);
    assert.match(windowsBuildScript, /scripts\\build_windows_installer\.js --payload/);
    assert.match(windowsBuildScript, /Setup-!PACKAGE_VERSION!-x64\.exe/);
    assert.doesNotMatch(windowsBuildScript, /NSIS/);
    assert.doesNotMatch(windowsBuildScript, /可直接使用 win-unpacked 目录/);
    assert.equal(packageJson.scripts.build.includes('--publish never'), true);
    assert.equal(packageJson.scripts['build:mac'].includes('--publish never'), true);
    assert.equal(packageJson.scripts['build:win'].includes('--publish never'), true);
    assert.match(windowsWorkflow, /steps\.find-installer\.outputs\.installer_path/);
    assert.match(
        releaseWorkflow,
        /Download Windows build artifact[\s\S]*Verify release files[\s\S]*installer="electron\/release\/小猪wordTTS-Setup-\$\{RELEASE_VERSION\}-x64\.exe/,
        'Unified release job must download and verify the exact Windows installer before publishing',
    );
    assert.match(releaseWorkflow, /needs: \[macos, windows\]/);
    assert.match(releaseWorkflow, /concurrency:[\s\S]*cancel-in-progress: true/);
    assert.equal(
        (releaseWorkflow.match(/version: \$\{\{ github\.event\.inputs\.version \|\| '' \}\}/g) || []).length,
        2,
        'reusable workflow inputs must use the caller-supported github context',
    );
    assert.doesNotMatch(releaseWorkflow, /version: \$\{\{ inputs\.version/);
    assert.match(releaseWorkflow, /draft: true/);
    assert.match(releaseWorkflow, /id: release[\s\S]*uses: softprops\/action-gh-release@v2/);
    assert.match(releaseWorkflow, /fail_on_unmatched_files: true/);
    assert.match(releaseWorkflow, /Prepare canonical GitHub asset names[\s\S]*mv.*github_installer[\s\S]*wordTTS-Setup-/);
    assert.match(releaseWorkflow, /files:[\s\S]*electron\/release\/wordTTS-Setup-\$\{\{ needs\.windows\.outputs\.version \}\}-x64\.exe/);
    assert.match(releaseWorkflow, /electron\/release\/latest-win\.json/);
    assert.doesNotMatch(releaseWorkflow, /latest\.yml|blockmap/);
    assert.match(releaseWorkflow, /Publish release after all assets upload[\s\S]*RELEASE_ID: \$\{\{ steps\.release\.outputs\.id \}\}[\s\S]*releases\/\$\{RELEASE_ID\}[\s\S]*draft=false/);
    assert.doesNotMatch(releaseWorkflow, /--paginate --slurp|sleep [0-9]/);
    assert.doesNotMatch(releaseWorkflow, /releases\/tags\/\$\{GITHUB_REF_NAME\}/);
    const macWorkflow = fs.readFileSync(
        path.join(APP_DIR, '..', '.github', 'workflows', 'build-macos.yml'),
        'utf8',
    );
    const macBuildScript = fs.readFileSync(
        path.join(APP_DIR, '..', 'build_electron.sh'),
        'utf8',
    );
    assert.match(macWorkflow, /npm ci --prefer-offline --no-audit --fund=false/);
    assert.match(
        macWorkflow,
        /- name: Upload artifact[\s\S]*?name: 小猪wordTTS-macOS[\s\S]*?compression-level: 0/,
    );
    assert.match(
        macWorkflow,
        /WORDTTS_SKIP_PYTHON_DEPENDENCY_INSTALL: '1'/,
        'macOS build must tell the packaging script to reuse the already-installed Python environment',
    );
    assert.match(
        macBuildScript,
        /WORDTTS_SKIP_PYTHON_DEPENDENCY_INSTALL:-0.*pip install/s,
        'macOS packaging script must support skipping a duplicate Python dependency install',
    );
    assert.match(
        macWorkflow,
        /Resolve final macOS artifacts[\s\S]*test -s \"\$zip_path\"[\s\S]*test -s \"\$dmg_path\"/,
        'macOS workflow must validate the exact final ZIP and DMG paths',
    );
    assert.ok(
        (macWorkflow.match(/\$\{\{ steps\.mac-artifacts\.outputs\.(?:dmg|zip) \}\}/g) || []).length >= 2,
        'macOS build artifact upload must use the validated exact paths',
    );
    assert.match(
        releaseWorkflow,
        /Download macOS build artifact[\s\S]*Verify release files[\s\S]*test -s electron\/release\/latest-mac\.yml/,
        'Unified release job must download and verify the packaged macOS artifacts before publishing',
    );
    assert.match(releaseWorkflow, /draft: true/);
    assert.match(releaseWorkflow, /Prepare canonical GitHub asset names[\s\S]*mv.*github_zip[\s\S]*mv.*github_dmg/);
    assert.match(releaseWorkflow, /files:[\s\S]*electron\/release\/wordTTS-\$\{\{ needs\.macos\.outputs\.version \}\}-\$\{\{ needs\.macos\.outputs\.architecture \}\}\.dmg/);
    assert.match(releaseWorkflow, /Publish release after all assets upload[\s\S]*RELEASE_ID: \$\{\{ steps\.release\.outputs\.id \}\}[\s\S]*releases\/\$\{RELEASE_ID\}[\s\S]*draft=false/);
    assert.doesNotMatch(releaseWorkflow, /releases\/tags\/\$\{GITHUB_REF_NAME\}/);
    assert.match(releaseWorkflow, /local_zip="electron\/release\/小猪wordTTS-/);
    assert.match(releaseWorkflow, /local_dmg="electron\/release\/小猪wordTTS-/);
    assert.match(macBuildScript, /builder_zip_path[\s\S]*rm -f \"\$builder_zip_path\"[\s\S]*latest-mac\.yml/);
});

test('打包冒烟使用完整 Chromium，并固定 Windows Python 输出为 UTF-8', () => {
    const macosWorkflow = fs.readFileSync(
        path.join(APP_DIR, '..', '.github', 'workflows', 'build-macos.yml'),
        'utf8',
    );
    const windowsWorkflow = fs.readFileSync(
        path.join(APP_DIR, '..', '.github', 'workflows', 'build-windows.yml'),
        'utf8',
    );

    assert.match(
        macosWorkflow,
        /launch_persistent_context\(tmp, channel="chromium", headless=True/,
    );
    assert.doesNotMatch(macosWorkflow, /launch_persistent_context\(tmp, headless=True/);
    assert.equal(
        (windowsWorkflow.match(/channel='chromium'/g) || []).length,
        2,
        'Windows 两个持久化 Chromium 冒烟都必须显式使用完整 Chromium',
    );
    assert.doesNotMatch(windowsWorkflow, /launch_persistent_context\(tmp, headless=True/);
    assert.match(
        windowsWorkflow,
        /name: Smoke test persistent Chromium \(Windows\)[\s\S]*?PYTHONUTF8: '1'[\s\S]*?PYTHONIOENCODING: 'utf-8'/,
    );
    assert.match(
        windowsWorkflow,
        /name: Smoke test custom install, update and uninstall[\s\S]*?PYTHONUTF8: '1'[\s\S]*?PYTHONIOENCODING: 'utf-8'/,
    );
});
