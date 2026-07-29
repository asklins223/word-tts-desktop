#!/usr/bin/env node

const { spawnSync } = require('child_process');

const scriptArgs = process.argv.slice(2);
if (scriptArgs.length === 0) {
    console.error('Usage: node scripts/run_python.js <script.py> [args...]');
    process.exit(2);
}

const candidates = process.platform === 'win32'
    ? [{ command: 'python', prefix: [] }, { command: 'py', prefix: ['-3'] }]
    : [{ command: 'python3', prefix: [] }, { command: 'python', prefix: [] }];

for (const candidate of candidates) {
    const result = spawnSync(
        candidate.command,
        [...candidate.prefix, ...scriptArgs],
        { stdio: 'inherit' },
    );
    if (result.error?.code === 'ENOENT') continue;
    if (result.error) {
        console.error(`[python] ${candidate.command} 启动失败: ${result.error.message}`);
        process.exit(1);
    }
    process.exit(result.status ?? 1);
}

console.error('[python] 未找到 python3、python 或 py，请先安装 Python 3');
process.exit(1);
