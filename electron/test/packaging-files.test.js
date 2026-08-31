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

test('Windows NSIS 使用原生安装页面，不加载自定义安装器 UI', () => {
    const nsisInclude = path.join(APP_DIR, 'build', 'installer.nsh');
    const installerHeader = path.join(APP_DIR, 'build', 'installerHeader.bmp');
    const installerSidebar = path.join(APP_DIR, 'build', 'installerSidebar.bmp');

    assert.equal(fs.existsSync(nsisInclude), false, `custom installer include must be removed: ${nsisInclude}`);
    assert.equal(fs.existsSync(installerHeader), false, `custom installer header must be removed: ${installerHeader}`);
    assert.equal(fs.existsSync(installerSidebar), false, `custom installer sidebar must be removed: ${installerSidebar}`);
    assert.equal(packageJson.build?.nsis?.include, undefined);
    assert.equal(packageJson.build?.nsis?.installerHeader, undefined);
    assert.equal(packageJson.build?.nsis?.installerSidebar, undefined);
    assert.equal(packageJson.build?.nsis?.allowToChangeInstallationDirectory, true);
    assert.equal(packageJson.build?.nsis?.installerHeaderIcon, 'build/icon.ico');
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
});
