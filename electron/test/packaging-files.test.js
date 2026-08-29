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
