'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const APP_DIR = path.join(__dirname, '..');
const REPOSITORY_DIR = path.join(APP_DIR, '..');
const VERIFY_SCRIPT = path.join(REPOSITORY_DIR, 'scripts', 'verify_macos_update_signature.sh');
const BUNDLE_ID = 'com.wordtts.desktop';
const SIGNING_REQUIREMENT = '=designated => identifier "' + BUNDLE_ID + '"';

function run(command, args, options = {}) {
    const result = spawnSync(command, args, {
        encoding: 'utf8',
        ...options,
    });
    assert.equal(
        result.status,
        0,
        [
            command + ' ' + args.join(' ') + ' failed with ' + result.status,
            result.stdout,
            result.stderr,
        ].filter(Boolean).join('\n'),
    );
    return (result.stdout || '') + (result.stderr || '');
}

function createSignedFixture(root, version) {
    const appPath = path.join(root, 'WordTTS-' + version + '.app');
    const contentsPath = path.join(appPath, 'Contents');
    const executablePath = path.join(contentsPath, 'MacOS', 'WordTTS');
    fs.mkdirSync(path.dirname(executablePath), { recursive: true });
    fs.copyFileSync('/usr/bin/true', executablePath);
    fs.chmodSync(executablePath, 0o755);
    fs.writeFileSync(path.join(contentsPath, 'Info.plist'), [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0"><dict>',
        '<key>CFBundleIdentifier</key><string>com.wordtts.desktop</string>',
        '<key>CFBundleExecutable</key><string>WordTTS</string>',
        '<key>CFBundlePackageType</key><string>APPL</string>',
        '<key>CFBundleVersion</key><string>' + version + '</string>',
        '</dict></plist>',
        '',
    ].join('\n'));
    run('codesign', [
        '--force',
        '--sign', '-',
        '--identifier', BUNDLE_ID,
        '--requirements', SIGNING_REQUIREMENT,
        appPath,
    ]);
    return appPath;
}

function signingDetails(appPath) {
    return run('codesign', ['--display', '--verbose=4', appPath]);
}

function designatedRequirement(appPath) {
    const output = run('codesign', ['--display', '--requirements', '-', appPath]);
    return output.split(/\r?\n/).find(line => /^(# )?designated => /.test(line)) || '';
}

test('macOS ad-hoc 更新签名跨不同构建保持 ShipIt 兼容', {
    skip: process.platform !== 'darwin',
}, () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'wordtts-macos-signature-'));
    try {
        const oldApp = createSignedFixture(root, '3.0.4');
        const newApp = createSignedFixture(root, '3.0.5');
        const oldRequirement = designatedRequirement(oldApp);
        const newRequirement = designatedRequirement(newApp);

        assert.equal(oldRequirement, 'designated => identifier "' + BUNDLE_ID + '"');
        assert.equal(newRequirement, oldRequirement);

        const oldCdHash = signingDetails(oldApp).match(/^CDHash=(.+)$/m)?.[1];
        const newCdHash = signingDetails(newApp).match(/^CDHash=(.+)$/m)?.[1];
        assert.ok(oldCdHash && newCdHash);
        assert.notEqual(oldCdHash, newCdHash, 'fixture builds must have different content hashes');

        const oldRequirementExpression = '=' + oldRequirement.replace(/^designated => /, '');
        run('codesign', [
            '--verify',
            '--deep',
            '--strict',
            '-R', oldRequirementExpression,
            newApp,
        ]);
        run('/bin/bash', [VERIFY_SCRIPT, newApp], {
            env: { ...process.env, WORDTTS_MAC_BUNDLE_ID: BUNDLE_ID },
        });
    } finally {
        fs.rmSync(root, { recursive: true, force: true });
    }
});
