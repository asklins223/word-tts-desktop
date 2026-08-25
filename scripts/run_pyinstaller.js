#!/usr/bin/env node

const { spawnSync } = require('child_process');

const scriptArgs = process.argv.slice(2);
if (scriptArgs.length === 0) {
    console.error('Usage: node scripts/run_pyinstaller.js <spec-file> [args...]');
    process.exit(2);
}

const candidates = process.platform === 'win32'
    ? [
        { command: 'python', prefix: [] },
        { command: 'py', prefix: ['-3'] },
    ]
    : [
        { command: 'python3', prefix: [] },
        { command: 'python', prefix: [] },
    ];

for (const candidate of candidates) {
    const result = spawnSync(
        candidate.command,
        [...candidate.prefix, '-m', 'PyInstaller', ...scriptArgs],
        { stdio: 'inherit' },
    );
    if (result.error?.code === 'ENOENT') continue;
    if (result.error) {
        console.error(`[pyinstaller] ${candidate.command} 启动失败: ${result.error.message}`);
        process.exit(1);
    }
    process.exit(result.status ?? 1);
}

console.error('[pyinstaller] 未找到 python3、python 或 py，请先安装 PyInstaller');
process.exit(1);
