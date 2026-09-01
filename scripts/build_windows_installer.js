'use strict';

/**
 * Build the self-drawing Windows setup executable.
 *
 * The application itself is built by electron-builder's `dir` target first.
 * This script creates a short-lived electron-builder project that embeds that
 * directory as an external payload and packages the approved HTML UI as a
 * portable executable. Keeping the payload outside the asar makes the setup
 * runtime able to copy files without depending on an archive implementation.
 */

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { normalizeProjectVersion, readProjectVersion } = require('./project_version');

const rootDir = path.resolve(__dirname, '..');
const installerSourceDir = path.join(rootDir, 'installer-prototype');
const electronDir = path.join(rootDir, 'electron');
const PORTABLE_UNINSTALL_RELOCATION_LABEL = 'wordtts_continue_portable';
const PORTABLE_UNINSTALL_RELOCATION_BLOCK = [
    '  # An installed portable executable cannot remove its own directory.',
    '  # Relocate the uninstaller before extracting Electron so the staged',
    '  # wrapper owns no file or current-directory handle below $EXEDIR.',
    '  StrCmp $EXEFILE "小猪wordTTS-uninstaller.exe" 0 wordtts_continue_portable',
    "  System::Call 'Kernel32::GetCurrentProcessId()i.R1'",
    '  StrCpy $R2 "$TEMP\\wordtts-uninstaller-stage-$R1-0-0.exe"',
    "  System::Call 'Kernel32::CopyFile(t, t, i)i (\"$EXEPATH\", \"$R2\", 0).R0'",
    '  StrCmp $R0 0 wordtts_continue_portable',
    "  System::Call 'Kernel32::SetEnvironmentVariable(t, t)i (\"WORDTTS_RELOCATED_UNINSTALLER\", \"$R2\").R0'",
    '  StrCmp $R0 0 wordtts_relocation_failed',
    "  System::Call 'Kernel32::SetEnvironmentVariable(t, t)i (\"WORDTTS_RELOCATION_SOURCE_PID\", \"$R1\").R0'",
    '  StrCmp $R0 0 wordtts_relocation_failed',
    '  ${StdUtils.GetAllParameters} $R0 0',
    '  SetOutPath $TEMP',
    '  ClearErrors',
    '  Exec \'"$R2" $R0 --mode=uninstall --target="$EXEDIR" --uninstall-relocated\'',
    '  IfErrors wordtts_relocation_failed',
    '  SetErrorLevel 0',
    '  Quit',
    'wordtts_relocation_failed:',
    '  Delete "$R2"',
    'wordtts_continue_portable:',
    '',
].join('\n');

const PORTABLE_UNINSTALL_SELF_CLEANUP_LABEL = 'wordtts_stage_cleanup_done';
const PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK = [
    '  # Electron has finished and the staged portable wrapper is about to',
    '  # release its own executable. Launch the batch prepared by Electron',
    '  # from this wrapper so there is no child-to-parent cleanup handshake.',
    '  ReadEnvStr $R0 "WORDTTS_RELOCATED_UNINSTALLER"',
    '  StrCmp $R0 "$EXEPATH" 0 wordtts_stage_cleanup_done',
    '  StrCpy $R1 "$EXEPATH.cleanup.cmd"',
    '  StrCpy $R2 "$R1.log"',
    '  IfFileExists "$R1" 0 wordtts_stage_cleanup_missing',
    '  SetOutPath $TEMP',
    '  ClearErrors',
    '  Exec \'"$SYSDIR\\cmd.exe" /d /q /c ""$R1" "$EXEPATH" "$R2""\'',
    '  IfErrors wordtts_stage_cleanup_launch_failed wordtts_stage_cleanup_done',
    'wordtts_stage_cleanup_missing:',
    '  ClearErrors',
    '  FileOpen $R3 "$R1.startup.log" a',
    '  IfErrors wordtts_stage_cleanup_done',
    '  FileWrite $R3 "staged cleanup failed: cleanup script missing$\\r$\\n"',
    '  FileClose $R3',
    '  Goto wordtts_stage_cleanup_done',
    'wordtts_stage_cleanup_launch_failed:',
    '  ClearErrors',
    '  FileOpen $R3 "$R1.startup.log" a',
    '  IfErrors wordtts_stage_cleanup_done',
    '  FileWrite $R3 "staged cleanup failed: cmd launch error$\\r$\\n"',
    '  FileClose $R3',
    'wordtts_stage_cleanup_done:',
    '',
].join('\n');

function argumentValue(argv, name, fallback = null) {
    const index = argv.indexOf(name);
    if (index >= 0) {
        const value = argv[index + 1];
        if (!value || value.startsWith('--')) throw new Error(`${name} 后面必须提供参数值`);
        return value;
    }
    const prefix = `${name}=`;
    const inline = argv.find(value => value.startsWith(prefix));
    if (inline !== undefined) {
        const value = inline.slice(prefix.length);
        if (!value) throw new Error(`${name} 后面必须提供参数值`);
        return value;
    }
    return fallback;
}

function parseArguments(argv = process.argv.slice(2)) {
    return {
        payloadDir: path.resolve(argumentValue(argv, '--payload', path.join(electronDir, 'release', 'win-unpacked'))),
        outputDir: path.resolve(argumentValue(argv, '--output-dir', path.join(electronDir, 'release'))),
        version: argumentValue(argv, '--version', null),
        dryRun: argv.includes('--dry-run'),
    };
}

function copySourceTree(sourceDir, targetDir) {
    fs.cpSync(sourceDir, targetDir, {
        recursive: true,
        filter(source) {
            const relative = path.relative(sourceDir, source);
            if (!relative) return true;
            return !relative.split(path.sep).includes('payload')
                && !relative.split(path.sep).includes('node_modules')
                && !relative.split(path.sep).includes('.git');
        },
    });
}

function createBuildProject({ payloadDir, outputDir, version, workDir }) {
    if (!fs.existsSync(payloadDir)) {
        throw new Error(`Windows 应用 payload 不存在: ${payloadDir}`);
    }
    const appExecutable = path.join(payloadDir, '小猪wordTTS.exe');
    if (!fs.existsSync(appExecutable)) {
        throw new Error(`payload 中缺少小猪wordTTS.exe: ${appExecutable}`);
    }
    copySourceTree(installerSourceDir, workDir);
    fs.copyFileSync(path.join(rootDir, 'version.json'), path.join(workDir, 'version.json'));
    fs.cpSync(payloadDir, path.join(workDir, 'payload'), { recursive: true });
    fs.mkdirSync(path.join(workDir, 'build'), { recursive: true });
    fs.copyFileSync(path.join(electronDir, 'build', 'icon.ico'), path.join(workDir, 'build', 'icon.ico'));

    const sourcePackage = JSON.parse(fs.readFileSync(path.join(installerSourceDir, 'package.json'), 'utf8'));
    const appPackage = JSON.parse(fs.readFileSync(path.join(electronDir, 'package.json'), 'utf8'));
    const canonicalVersion = readProjectVersion();
    const resolvedVersion = version
        ? normalizeProjectVersion(version, '构建版本')
        : canonicalVersion;
    if (resolvedVersion !== canonicalVersion) {
        throw new Error(`构建版本 ${resolvedVersion} 与 version.json 中的版本 ${canonicalVersion} 不一致，请先更新 version.json`);
    }
    if (appPackage.version !== resolvedVersion) {
        throw new Error(`Electron package 版本 ${appPackage.version} 未同步到项目版本 ${resolvedVersion}，请先运行 node scripts/project_version.js --sync`);
    }
    const packageJson = {
        ...sourcePackage,
        version: resolvedVersion,
        main: 'installer-main.js',
        build: {
            appId: 'com.wordtts.installer',
            productName: '小猪wordTTS 安装程序',
            executableName: '小猪wordTTS-Setup',
            electronVersion: appPackage.devDependencies.electron,
            asar: true,
            compression: 'maximum',
            directories: {
                output: outputDir,
                buildResources: 'build',
            },
            files: [
                'index.html',
                'styles.css',
                'app.js',
                'installer-main.js',
                'installer-preload.js',
                'installer-service.js',
                'version.json',
                'assets/**/*',
            ],
            extraResources: [
                { from: 'payload', to: 'payload' },
            ],
            win: {
                target: [{ target: 'portable', arch: ['x64'] }],
                icon: 'build/icon.ico',
                signAndEditExecutable: true,
            },
            portable: {
                artifactName: '小猪wordTTS-Setup-${version}-${arch}.${ext}',
            },
        },
    };
    fs.writeFileSync(path.join(workDir, 'package.json'), `${JSON.stringify(packageJson, null, 2)}\n`, 'utf8');
    return { packageJson, resolvedVersion };
}

function runBuilder(projectDir, electronDir, env = process.env) {
    const executable = process.platform === 'win32' ? 'npx.cmd' : 'npx';
    const result = spawnSync(
        executable,
        ['electron-builder', '--projectDir', projectDir, '--win', 'portable', '--x64', '--publish', 'never'],
        {
            cwd: electronDir,
            env,
            stdio: 'inherit',
            // .cmd shims are not directly executable by Node on every
            // supported Windows runtime. Let cmd.exe dispatch npx.cmd while
            // keeping the POSIX path free of an unnecessary shell.
            shell: process.platform === 'win32',
        },
    );
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error(`自绘 Windows 安装程序构建失败，退出码: ${result.status}`);
}

function patchPortableTemplate(templatePath) {
    const original = fs.readFileSync(templatePath, 'utf8');
    const exedirMarker = /^[ \t]+SetOutPath \$EXEDIR\r?\n(?=[ \t]*RMDir \/r \$INSTDIR)/m;
    const tempMarker = /^[ \t]+SetOutPath \$TEMP\r?\n(?=[ \t]*RMDir \/r \$INSTDIR)/m;
    const countMatches = (value, pattern) => (
        value.match(new RegExp(pattern.source, `${pattern.flags}g`)) || []
    ).length;
    let patched = original;
    let restoreContent = original;
    if (exedirMarker.test(original)) {
        if (countMatches(original, exedirMarker) !== 1) {
            throw new Error(`无法确认 electron-builder portable 模板的收尾路径: ${templatePath}`);
        }
        patched = patched.replace(exedirMarker, value => value.replace('$EXEDIR', '$TEMP'));
        restoreContent = original;
    } else if (tempMarker.test(original)) {
        // Already patched (e.g. previous run left the file patched). Keep the
        // patched content for this build but restore to the unpatched state
        // afterwards so the working tree stays clean.
        if (countMatches(original, tempMarker) !== 1) {
            throw new Error(`无法确认 electron-builder portable 模板的收尾路径: ${templatePath}`);
        }
        restoreContent = original.replace(tempMarker, value => value.replace('$TEMP', '$EXEDIR'));
    } else {
        throw new Error(`无法确认 electron-builder portable 模板的收尾路径: ${templatePath}`);
    }
    const relocationPattern = new RegExp(`^[ \\t]*${PORTABLE_UNINSTALL_RELOCATION_LABEL}:\\r?\\n`, 'm');
    if (!relocationPattern.test(patched)) {
        if (!/^Section\r?\n/m.test(patched)) {
            throw new Error(`无法确认 electron-builder portable 模板的 Section: ${templatePath}`);
        }
        patched = patched.replace(/^Section\r?\n/m, value => `${value}${PORTABLE_UNINSTALL_RELOCATION_BLOCK}`);
    }
    const selfCleanupPattern = new RegExp(`^[ \\t]*${PORTABLE_UNINSTALL_SELF_CLEANUP_LABEL}:\\r?\\n`, 'm');
    if (!selfCleanupPattern.test(patched)) {
        const finalCleanupMarker = /^[ \t]+RMDir \/r \$INSTDIR\r?\n(?=SectionEnd)/m;
        if (countMatches(patched, finalCleanupMarker) !== 1) {
            throw new Error(`无法确认 electron-builder portable 模板的最终清理位置: ${templatePath}`);
        }
        patched = patched.replace(
            finalCleanupMarker,
            value => `${value}${PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK}`,
        );
    }
    if (relocationPattern.test(restoreContent)) {
        restoreContent = restoreContent.replace(PORTABLE_UNINSTALL_RELOCATION_BLOCK, '');
    }
    if (selfCleanupPattern.test(restoreContent)) {
        restoreContent = restoreContent.replace(PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK, '');
    }
    if (patched !== original) {
        fs.writeFileSync(templatePath, patched, 'utf8');
    }
    // Verify the patch is active before letting the builder run.
    const active = fs.readFileSync(templatePath, 'utf8');
    if (!tempMarker.test(active)
        || exedirMarker.test(active)
        || !relocationPattern.test(active)
        || !selfCleanupPattern.test(active)
        || !active.includes('$EXEPATH.cleanup.cmd')
        || !active.includes('$SYSDIR\\cmd.exe')
        || !active.includes('WORDTTS_RELOCATED_UNINSTALLER')) {
        throw new Error(`自绘安装程序模板补丁未生效: ${templatePath}`);
    }
    return () => fs.writeFileSync(templatePath, restoreContent, 'utf8');
}

function main() {
    const args = parseArguments();
    const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'wordtts-installer-build-'));
    // electron-builder cleans its output directory before packaging. Keep
    // the setup build away from electron/release so it cannot remove the
    // application's win-unpacked directory that the next smoke step checks.
    const builderOutputDir = path.join(workDir, 'release');
    try {
        const { resolvedVersion } = createBuildProject({
            ...args,
            outputDir: builderOutputDir,
            workDir,
        });
        const outputName = `小猪wordTTS-Setup-${resolvedVersion}-x64.exe`;
        const outputPath = path.join(args.outputDir, outputName);
        console.log(`自绘 Windows 安装程序配置已生成: ${outputPath}`);
        if (args.dryRun) return;
        // Never let a failed builder invocation pass because a previous run
        // left an installer with the same version in the output directory.
        fs.rmSync(outputPath, { force: true });
        try {
            // electron-builder's stock portable template switches the output
            // directory to $EXEDIR after the embedded app exits. When the
            // executable is an installed uninstaller, that creates the just-
            // removed install directory again. Keep the working directory
            // outside the target while retaining the final extraction cleanup.
            const portableTemplatePath = path.join(
                electronDir,
                'node_modules',
                'app-builder-lib',
                'templates',
                'nsis',
                'portable.nsi',
            );
            const restorePortableTemplate = patchPortableTemplate(portableTemplatePath);
            try {
                runBuilder(workDir, electronDir, process.env);
            } finally {
                restorePortableTemplate();
            }
            const builtPath = path.join(builderOutputDir, outputName);
            if (!fs.existsSync(builtPath) || fs.statSync(builtPath).size <= 0) {
                throw new Error(`electron-builder 未生成有效的自绘安装程序: ${builtPath}`);
            }
            fs.mkdirSync(args.outputDir, { recursive: true });
            fs.copyFileSync(builtPath, outputPath);
            if (!fs.existsSync(outputPath) || fs.statSync(outputPath).size <= 0) {
                throw new Error(`无法复制有效的自绘安装程序到输出目录: ${outputPath}`);
            }
        } catch (error) {
            // A failed builder may leave a partial file behind. Remove it too,
            // otherwise a later release step could mistake that file for a
            // successful same-version build.
            fs.rmSync(outputPath, { force: true });
            throw error;
        }
        console.log(`自绘 Windows 安装程序构建完成: ${outputPath}`);
    } finally {
        fs.rmSync(workDir, { recursive: true, force: true });
    }
}

if (require.main === module) {
    try {
        main();
    } catch (error) {
        console.error(`[错误] ${error.message}`);
        process.exitCode = 1;
    }
}

module.exports = {
    PORTABLE_UNINSTALL_RELOCATION_BLOCK,
    PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK,
    createBuildProject,
    patchPortableTemplate,
    parseArguments,
};
