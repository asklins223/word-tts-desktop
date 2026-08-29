'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const electronDir = path.join(root, 'electron');
const schemaPath = path.join(root, 'contracts', 'openapi.yaml');
const generatedPath = path.join(root, 'contracts', 'generated.ts');
const tempPath = path.join(
    os.tmpdir(),
    `wordtts-openapi-${process.pid}-${Date.now()}.ts`,
);
const executable = process.platform === 'win32'
    ? path.join(electronDir, 'node_modules', '.bin', 'openapi-typescript.cmd')
    : path.join(electronDir, 'node_modules', '.bin', 'openapi-typescript');

let exitCode = 0;
try {
    const result = spawnSync(executable, [schemaPath, '-o', tempPath], {
        cwd: electronDir,
        encoding: 'utf8',
        stdio: 'inherit',
    });
    if (result.error) throw result.error;
    if (result.status !== 0) {
        exitCode = result.status || 1;
    } else if (!fs.existsSync(generatedPath)) {
        console.error(`generated contract is missing: ${generatedPath}`);
        exitCode = 1;
    } else if (fs.readFileSync(tempPath, 'utf8') !== fs.readFileSync(generatedPath, 'utf8')) {
        console.error(
            'contracts/generated.ts is out of date; run `npm run generate:contracts` from electron',
        );
        exitCode = 1;
    } else {
        console.log('generated contract is in sync');
    }
} catch (error) {
    console.error(`contract generation check failed: ${error.message}`);
    exitCode = 1;
} finally {
    try { fs.unlinkSync(tempPath); } catch (_) { /* no temporary output */ }
}

process.exitCode = exitCode;
