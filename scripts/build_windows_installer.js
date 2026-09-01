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
const { spawn } = require('node:child_process');
const { normalizeProjectVersion, readProjectVersion } = require('./project_version');

const rootDir = path.resolve(__dirname, '..');
const installerSourceDir = path.join(rootDir, 'installer-prototype');
const electronDir = path.join(rootDir, 'electron');
const DEFAULT_7Z_COMPRESSION_LEVEL = '5';
const BUILD_PROGRESS_INTERVAL_MS = 15_000;
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
    const resolvedPayloadDir = path.resolve(payloadDir);
    if (!fs.existsSync(resolvedPayloadDir)) {
        throw new Error(`Windows 应用 payload 不存在: ${resolvedPayloadDir}`);
    }
    const appExecutable = path.join(resolvedPayloadDir, '小猪wordTTS.exe');
    if (!fs.existsSync(appExecutable)) {
        throw new Error(`payload 中缺少小猪wordTTS.exe: ${appExecutable}`);
    }
    copySourceTree(installerSourceDir, workDir);
    fs.copyFileSync(path.join(rootDir, 'version.json'), path.join(workDir, 'version.json'));
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
            // The payload contains already-compressed Chromium and media
            // assets. `maximum` adds a more expensive 7z pass without
            // materially shrinking the portable setup; normal keeps release
            // size reasonable while avoiding that build-time penalty.
            compression: 'normal',
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
                // Read directly from the completed dir build. Staging this
                // tree under workDir first creates a second ~1 GB copy before
                // electron-builder copies it into its own output.
                { from: resolvedPayloadDir, to: 'payload' },
            ],
            win: {
                target: [{ target: 'portable', arch: ['x64'] }],
                icon: 'build/icon.ico',
                signAndEditExecutable: true,
            },
            portable: {
                artifactName: '小猪wordTTS-Setup-${version}-${arch}.${ext}',
                // Keep the portable payload in electron-builder's normal 7z
                // archive. Each invocation must unpack into its own NSIS
                // plugin directory so a second update cannot collide with a
                // stale build-wide TEMP directory.
                unpackDirName: false,
            },
        },
    };
    fs.writeFileSync(path.join(workDir, 'package.json'), `${JSON.stringify(packageJson, null, 2)}\n`, 'utf8');
    return { packageJson, resolvedVersion };
}

function formatDuration(milliseconds) {
    const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return minutes > 0 ? `${minutes}分${String(seconds).padStart(2, '0')}秒` : `${seconds}秒`;
}

function formatMiB(bytes) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function findPortableBuildOutput(outputDir) {
    let entries;
    try {
        entries = fs.readdirSync(outputDir, { withFileTypes: true });
    } catch (error) {
        if (error.code === 'ENOENT') return null;
        throw error;
    }

    const files = [];
    for (const entry of entries) {
        if (!entry.isFile()) continue;
        const type = entry.name.endsWith('.nsis.7z')
            ? 'archive'
            : entry.name.endsWith('.exe')
                ? 'installer'
                : null;
        if (!type) continue;
        const filePath = path.join(outputDir, entry.name);
        try {
            files.push({
                type,
                name: entry.name,
                size: fs.statSync(filePath).size,
            });
        } catch (error) {
            // The archive can be renamed or removed between readdir and stat
            // when electron-builder moves from 7z creation to NSIS assembly.
            if (error.code !== 'ENOENT') throw error;
        }
    }

    files.sort((left, right) => {
        // Once the final executable appears, NSIS assembly is the newest stage
        // even if electron-builder has not removed its intermediate archive yet.
        const typeOrder = { archive: 1, installer: 2 };
        return typeOrder[right.type] - typeOrder[left.type] || right.size - left.size;
    });
    return files[0] || null;
}

function describeBuildProgress(outputDir, elapsedMs, previousBytes = null) {
    const output = findPortableBuildOutput(outputDir);
    const elapsed = formatDuration(elapsedMs);
    if (!output) {
        return {
            bytes: null,
            message: `[打包进度] 已运行 ${elapsed}；正在准备 7z 归档，请稍候…`,
        };
    }

    const growth = previousBytes != null && output.size > previousBytes
        ? `，本周期 +${formatMiB(output.size - previousBytes)}`
        : '';
    const stage = output.type === 'archive' ? '7z 压缩中' : 'NSIS 封装中';
    return {
        bytes: output.size,
        message: `[打包进度] ${stage}：已运行 ${elapsed}，${output.name} 已写入 ${formatMiB(output.size)}${growth}`,
    };
}

function startBuildProgressReporter(outputDir, intervalMs = BUILD_PROGRESS_INTERVAL_MS) {
    const startedAt = Date.now();
    let previousBytes = null;
    console.log(`[打包进度] electron-builder 已启动；每 ${Math.round(intervalMs / 1000)} 秒报告 7z/NSIS 输出体积。`);
    const timer = setInterval(() => {
        try {
            const progress = describeBuildProgress(outputDir, Date.now() - startedAt, previousBytes);
            previousBytes = progress.bytes;
            console.log(progress.message);
        } catch (error) {
            // Progress reporting is observability only and must never turn a
            // successful package build into a failure.
            console.warn(`[打包进度] 暂时无法读取输出目录：${error.message}`);
        }
    }, intervalMs);
    timer.unref?.();
    return (result) => {
        clearInterval(timer);
        console.log(`[打包进度] electron-builder ${result}，总耗时 ${formatDuration(Date.now() - startedAt)}。`);
    };
}

function runBuilder(projectDir, electronDir, outputDir, env = process.env) {
    const executable = process.platform === 'win32' ? 'npx.cmd' : 'npx';
    // electron-builder 26.15.x can emit BCJ2-filtered 7z archives. The
    // NSIS runtime extractor bundled with portable installers does not
    // understand BCJ2 reliably, so keep the archive in 7z format but use the
    // single-stream BCJ filter that Nsis7z can extract during install/update.
    const builderEnv = {
        ...env,
        ELECTRON_BUILDER_7Z_FILTER: 'BCJ',
        // electron-builder 26.15.x maps every non-store 7z build to -mx=9,
        // even when build.compression is "normal". Level 5 cut a representative
        // 347 MiB payload from 248s at level 7 to 71s for only 2.6% more bytes.
        ELECTRON_BUILDER_COMPRESSION_LEVEL:
            env.ELECTRON_BUILDER_COMPRESSION_LEVEL || DEFAULT_7Z_COMPRESSION_LEVEL,
    };
    console.log(
        `[打包配置] 7z 压缩等级=${builderEnv.ELECTRON_BUILDER_COMPRESSION_LEVEL}，过滤器=${builderEnv.ELECTRON_BUILDER_7Z_FILTER}`,
    );
    return new Promise((resolve, reject) => {
        let child;
        try {
            child = spawn(
                executable,
                ['electron-builder', '--projectDir', projectDir, '--win', 'portable', '--x64', '--publish', 'never'],
                {
                    cwd: electronDir,
                    env: builderEnv,
                    stdio: 'inherit',
                    // .cmd shims are not directly executable by Node on every
                    // supported Windows runtime. Let cmd.exe dispatch npx.cmd while
                    // keeping the POSIX path free of an unnecessary shell.
                    shell: process.platform === 'win32',
                },
            );
        } catch (error) {
            reject(error);
            return;
        }

        const stopProgress = startBuildProgressReporter(outputDir);
        let settled = false;
        const finish = (error = null) => {
            if (settled) return;
            settled = true;
            stopProgress(error ? '失败' : '完成');
            if (error) reject(error);
            else resolve();
        };
        child.once('error', finish);
        child.once('close', (code, signal) => {
            if (code === 0) {
                finish();
                return;
            }
            const detail = signal ? `信号: ${signal}` : `退出码: ${code}`;
            finish(new Error(`自绘 Windows 安装程序构建失败，${detail}`));
        });
    });
}

function patchPortableTemplate(templatePath) {
    const original = fs.readFileSync(templatePath, 'utf8');
    const exedirMarker = /^[ \t]+SetOutPath \$EXEDIR\r?\n(?=[ \t]*RMDir \/r \$INSTDIR)/m;
    const tempMarker = /^[ \t]+SetOutPath \$TEMP\r?\n(?=[ \t]*RMDir \/r \$INSTDIR)/m;
    const portableUnpackPattern = /^[ \t]*!ifdef UNPACK_DIR_NAME\r?\n[ \t]*StrCpy \$INSTDIR "\$TEMP\\\$\{UNPACK_DIR_NAME\}"\r?\n[ \t]*!endif\r?\n/m;
    const portableUnpackMarker = /^[ \t]*; WORDTTS_PORTABLE_UNIQUE_PLUGIN_DIR\r?\n/m;
    const portableUnpackReplacement = '  ; WORDTTS_PORTABLE_UNIQUE_PLUGIN_DIR\n';
    const portableUnpackRestoreBlock = [
        '  !ifdef UNPACK_DIR_NAME',
        '    StrCpy $INSTDIR "$TEMP\\${UNPACK_DIR_NAME}"',
        '  !endif',
        '',
    ].join('\n');
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
    const unpackBlockCount = countMatches(original, portableUnpackPattern);
    const unpackMarkerCount = countMatches(original, portableUnpackMarker);
    if (unpackBlockCount > 1 || unpackMarkerCount > 1
        || (unpackBlockCount === 0 && unpackMarkerCount === 0)
        || (unpackBlockCount > 0 && unpackMarkerCount > 0)) {
        throw new Error(`无法确认 electron-builder portable 模板的解压目录: ${templatePath}`);
    }
    if (unpackBlockCount === 1) {
        // electron-builder 26.15.x still defines UNPACK_DIR_NAME when the
        // documented `unpackDirName: false` option is used. Remove the
        // template branch so $PLUGINSDIR remains the per-launch directory.
        patched = patched.replace(portableUnpackPattern, portableUnpackReplacement);
    } else if (!portableUnpackMarker.test(patched)) {
        throw new Error(`无法启用 electron-builder portable 的独立解压目录: ${templatePath}`);
    }
    if (portableUnpackMarker.test(restoreContent)) {
        restoreContent = restoreContent.replace(portableUnpackMarker, portableUnpackRestoreBlock);
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
        || !portableUnpackMarker.test(active)
        || portableUnpackPattern.test(active)
        || !relocationPattern.test(active)
        || !selfCleanupPattern.test(active)
        || !active.includes('$EXEPATH.cleanup.cmd')
        || !active.includes('$SYSDIR\\cmd.exe')
        || !active.includes('WORDTTS_RELOCATED_UNINSTALLER')) {
        throw new Error(`自绘安装程序模板补丁未生效: ${templatePath}`);
    }
    return () => fs.writeFileSync(templatePath, restoreContent, 'utf8');
}

async function main() {
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
                await runBuilder(workDir, electronDir, builderOutputDir, process.env);
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
    main().catch((error) => {
        console.error(`[错误] ${error.message}`);
        process.exitCode = 1;
    });
}

module.exports = {
    PORTABLE_UNINSTALL_RELOCATION_BLOCK,
    PORTABLE_UNINSTALL_SELF_CLEANUP_BLOCK,
    DEFAULT_7Z_COMPRESSION_LEVEL,
    createBuildProject,
    describeBuildProgress,
    patchPortableTemplate,
    parseArguments,
};
