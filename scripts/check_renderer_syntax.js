#!/usr/bin/env node

/**
 * Parse every local renderer script in the same order used by index.html.
 * Keeping this as a small executable makes the syntax check usable by both
 * npm scripts and the Python release gate without depending on a shell.
 */

'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const rendererDirectory = path.join(__dirname, '..', 'electron', 'renderer');
const html = fs.readFileSync(path.join(rendererDirectory, 'index.html'), 'utf8');
const scripts = [...html.matchAll(/<script\s+src="([^"]+)"><\/script>/g)]
    .map(match => match[1])
    .filter(source => !/^(?:https?:)?\/\//i.test(source));

if (scripts.length === 0) {
    throw new Error('renderer/index.html does not declare any local scripts');
}

function collectRendererScripts(directory, prefix = '') {
    return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
        const relativePath = path.join(prefix, entry.name);
        const absolutePath = path.join(directory, entry.name);
        if (entry.isDirectory()) return collectRendererScripts(absolutePath, relativePath);
        return entry.isFile() && entry.name.endsWith('.js') ? [relativePath] : [];
    });
}

const declaredScripts = new Set();
for (const source of scripts) {
    const scriptPath = path.resolve(rendererDirectory, source);
    const relativePath = path.relative(rendererDirectory, scriptPath);
    declaredScripts.add(relativePath.split(path.sep).join('/'));
}
const rendererScripts = new Set(
    collectRendererScripts(rendererDirectory).map(source => source.split(path.sep).join('/')),
);
const orphanedScripts = [...rendererScripts].filter(source => !declaredScripts.has(source));
const missingScripts = [...declaredScripts].filter(source => !rendererScripts.has(source));
if (orphanedScripts.length || missingScripts.length) {
    const problems = [];
    if (orphanedScripts.length) problems.push(`not declared by index.html: ${orphanedScripts.join(', ')}`);
    if (missingScripts.length) problems.push(`declared but missing: ${missingScripts.join(', ')}`);
    throw new Error(`renderer script manifest mismatch (${problems.join('; ')})`);
}

for (const source of scripts) {
    const scriptPath = path.resolve(rendererDirectory, source);
    const relativePath = path.relative(rendererDirectory, scriptPath);
    if (!scriptPath.startsWith(`${rendererDirectory}${path.sep}`)) {
        throw new Error(`renderer script escapes renderer directory: ${source}`);
    }
    if (!fs.statSync(scriptPath).isFile()) {
        throw new Error(`renderer script does not exist: ${relativePath}`);
    }
    const result = spawnSync(process.execPath, ['--check', scriptPath], {
        encoding: 'utf8',
        stdio: 'inherit',
    });
    if (result.status !== 0) {
        process.exit(result.status || 1);
    }
}

console.log(`renderer syntax ok: ${scripts.length} scripts`);
