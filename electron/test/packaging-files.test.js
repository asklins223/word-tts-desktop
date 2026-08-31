'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');

const APP_DIR = path.join(__dirname, '..');
const packageJson = JSON.parse(
    fs.readFileSync(path.join(APP_DIR, 'package.json'), 'utf8'),
);
const buildFiles = packageJson.build?.files;

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

test('Windows NSIS 自定义页面已被纳入构建配置', () => {
    const nsisInclude = path.join(APP_DIR, 'build', 'installer.nsh');

    assert.ok(fs.existsSync(nsisInclude), `installer asset must exist: ${nsisInclude}`);
    assert.equal(packageJson.build?.nsis?.include, 'build/installer.nsh');
    assert.equal(packageJson.build?.nsis?.installerHeader, null);
    assert.equal(packageJson.build?.nsis?.installerSidebar, null);
    assert.equal(packageJson.build?.nsis?.allowToChangeInstallationDirectory, undefined);
    const windowsWorkflow = fs.readFileSync(
        path.join(APP_DIR, '..', '.github', 'workflows', 'build-windows.yml'),
        'utf8',
    );
    const windowsBuildScript = fs.readFileSync(
        path.join(APP_DIR, '..', 'build_electron_windows.bat'),
        'utf8',
    );
    assert.ok(
        (windowsWorkflow.match(/\$installerName = "小猪wordTTS-Setup-\$env:UPDATE_VERSION-x64\.exe"/g) || []).length >= 2,
        'installer smoke and release steps must bind to the current x64 setup executable',
    );
    assert.ok(
        (windowsWorkflow.match(/Get-Item -LiteralPath \(Join-Path "electron\/release" \$installerName\)/g) || []).length >= 2,
        'installer smoke and release steps must resolve the exact setup executable path',
    );
    assert.doesNotMatch(windowsWorkflow, /-Filter "\*-Setup-\*\.exe"/);
    assert.doesNotMatch(windowsWorkflow, /Get-ChildItem -Path "electron\/release" -Filter "\*\.exe"/);
    assert.match(windowsBuildScript, /electron-builder --win --publish never/);
    assert.match(windowsBuildScript, /Setup-!PACKAGE_VERSION!-x64\.exe/);
    assert.doesNotMatch(windowsBuildScript, /构建产物 \(unpacked\)/);
    assert.doesNotMatch(windowsBuildScript, /可直接使用 win-unpacked 目录/);
    assert.equal(packageJson.scripts.build.includes('--publish never'), true);
    assert.equal(packageJson.scripts['build:mac'].includes('--publish never'), true);
    assert.equal(packageJson.scripts['build:win'].includes('--publish never'), true);
    assert.ok(
        (windowsWorkflow.match(/\$\{\{ steps\.find-installer\.outputs\.installer_path \}\}/g) || []).length >= 1,
        'Windows build artifact upload must use the validated installer path',
    );
    assert.match(
        windowsWorkflow,
        /Download Windows build artifact[\s\S]*Verify release files[\s\S]*installer="electron\/release\/小猪wordTTS-Setup-\$\{RELEASE_VERSION\}-x64\.exe/,
        'Windows release job must download and verify the exact installer before publishing',
    );
    assert.match(windowsWorkflow, /draft: true/);
    assert.match(windowsWorkflow, /Prepare canonical GitHub asset names[\s\S]*cp.*github_installer[\s\S]*wordTTS-Setup-/);
    assert.match(windowsWorkflow, /files:[\s\S]*electron\/release\/wordTTS-Setup-\$\{\{ steps\.version\.outputs\.version \}\}-x64\.exe/);
    assert.match(windowsWorkflow, /Publish only after both platform assets are ready[\s\S]*releases_endpoint="repos\/\$\{GITHUB_REPOSITORY\}\/releases"[\s\S]*--paginate --slurp[\s\S]*draft=false/);
    assert.doesNotMatch(windowsWorkflow, /releases\/tags\/\$\{GITHUB_REF_NAME\}/);
    const macWorkflow = fs.readFileSync(
        path.join(APP_DIR, '..', '.github', 'workflows', 'build-macos.yml'),
        'utf8',
    );
    const macBuildScript = fs.readFileSync(
        path.join(APP_DIR, '..', 'build_electron.sh'),
        'utf8',
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
        macWorkflow,
        /Download macOS build artifact[\s\S]*Verify release files[\s\S]*test -s electron\/release\/latest-mac\.yml/,
        'macOS release job must download and verify the packaged artifacts before publishing',
    );
    assert.match(macWorkflow, /draft: true/);
    assert.match(macWorkflow, /Prepare canonical GitHub asset names[\s\S]*cp.*github_zip[\s\S]*cp.*github_dmg/);
    assert.match(macWorkflow, /files:[\s\S]*electron\/release\/wordTTS-\$\{\{ needs\.build\.outputs\.version \}\}-\$\{\{ needs\.build\.outputs\.architecture \}\}\.dmg/);
    assert.match(macWorkflow, /Publish only after both platform assets are ready[\s\S]*releases_endpoint="repos\/\$\{GITHUB_REPOSITORY\}\/releases"[\s\S]*--paginate --slurp[\s\S]*draft=false/);
    assert.doesNotMatch(macWorkflow, /releases\/tags\/\$\{GITHUB_REF_NAME\}/);
    assert.match(macWorkflow, /local_zip="electron\/release\/小猪wordTTS-/);
    assert.match(macWorkflow, /local_dmg="electron\/release\/小猪wordTTS-/);
    assert.match(macBuildScript, /builder_zip_path[\s\S]*rm -f \"\$builder_zip_path\"[\s\S]*latest-mac\.yml/);
    const nsisText = fs.readFileSync(nsisInclude, 'utf8');
    assert.match(nsisText, /\$\{VERSION\}/, 'NSIS custom pages must use the electron-builder version macro');
    assert.doesNotMatch(nsisText, /"3\.0\.[0-9]+"/, 'NSIS custom pages must not freeze a release version');
    assert.match(nsisText, /customWelcomePage/);
    assert.match(nsisText, /customFinishPage/);
    assert.match(nsisText, /Page custom InstallerWelcomeCreate InstallerWelcomeLeave/);
    assert.match(nsisText, /Page custom InstallerFinishCreate InstallerFinishLeave/);
    assert.doesNotMatch(nsisText, /!insertmacro MUI_PAGE_WELCOME/);
    assert.doesNotMatch(nsisText, /!insertmacro MUI_PAGE_FINISH/);
    assert.match(nsisText, /MUI_PAGE_CUSTOMFUNCTION_SHOW/);
    assert.match(nsisText, /customPageAfterChangeDir/);
    assert.match(nsisText, /InstallerHideStockChrome/);
    assert.match(nsisText, /InstallerBuildFrame/);
    assert.match(nsisText, /InstallerBuildCompactFrame/);
    assert.match(nsisText, /Function InstallerDirectoryCreate/);
    assert.match(nsisText, /Function InstallerInstallFilesCreate/);
    assert.match(nsisText, /INSTALLER_PROGRESS_BASE_WIDTH 416/);
    assert.match(nsisText, /INSTALLER_PROGRESS_BASE_HEIGHT 242/);
    assert.match(nsisText, /InstallerCreateProgressLabel/);
    assert.match(nsisText, /InstallerCreateProgressMarker/);
    assert.match(nsisText, /nsDialogs::Create 1044/);
    assert.match(nsisText, /CreateWindowEx/);
    assert.match(nsisText, /InstallerInstallModeToggle/);
    assert.match(nsisText, /DwmSetWindowAttribute/);
    assert.match(nsisText, /MUI_INSTFILESPAGE_COLORS/);
    assert.match(nsisText, /MUI_INSTFILESPAGE_COLORS "23201D F6F1E8"/);
    assert.match(nsisText, /INSTALLER_ACCENT "F06445"/);
    assert.match(nsisText, /INSTALLER_SIGNAL "FFC857"/);
    assert.doesNotMatch(nsisText, /9BBC0F|8BAC0F|306230|0F380F|315CFF|6C5CE7|F06A4F/);
    assert.doesNotMatch(nsisText, /Consolas|installer_frame_step|installer_compact_active/);
    assert.doesNotMatch(nsisText, /PageEx custom/);
    assert.match(nsisText, /WS_CAPTION/);
    assert.match(nsisText, /StdUtils\.ExecShellAsUser/);
    assert.match(nsisText, /\$launchLink/);
    assert.doesNotMatch(nsisText, /ExecShell\s+"open"/);
    assert.match(nsisText, /NSD_OnClick/);
    assert.match(nsisText, /Function InstallerFinishToggleOpen\s+Pop \$0/);
    assert.match(nsisText, /HIDE_RUN_AFTER_FINISH/);

    const dialogControls = [...nsisText.matchAll(
        /\$\{NSD_Create(?:Label|GroupBox|CheckBox|DirRequest|BrowseButton)\}\s+(\d+)u\s+(\d+)u\s+(\d+)u\s+(\d+)u/g,
    )].map((match) => ({
        x: Number(match[1]),
        y: Number(match[2]),
        width: Number(match[3]),
        height: Number(match[4]),
    }));
    assert.ok(dialogControls.length > 0, 'expected custom NSIS dialog controls');
    for (const control of dialogControls) {
        assert.ok(
            control.x >= 0 && control.x + control.width <= 315,
            `custom control must stay inside the NSIS content width: ${JSON.stringify(control)}`,
        );
        assert.ok(
            control.y >= 0 && control.y + control.height <= 193,
            `custom control must stay above the NSIS action row: ${JSON.stringify(control)}`,
        );
    }

});
