'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { parseVersion } = require('./update_policy');

const rootDir = path.resolve(__dirname, '..');
const tag = String(process.env.RELEASE_TAG || '').trim();
if (!tag) {
  throw new Error('RELEASE_TAG 未设置');
}

if (!parseVersion(tag) || !tag.startsWith('v')) {
  throw new Error(`非法 release tag: ${tag}`);
}

const changelogPath = path.join(rootDir, 'CHANGELOG.md');
const outputPath = path.join(rootDir, 'release-notes.md');
const lines = fs.readFileSync(changelogPath, 'utf8').split(/\r?\n/);
const heading = `## ${tag}`;
const startIndex = lines.findIndex((line) => line.trim() === heading);

if (startIndex < 0) {
  throw new Error(`CHANGELOG.md 中未找到 ${heading}`);
}

const versionHeading = (line) => {
  const match = line.trim().match(/^##\s+(v\S+)$/);
  return Boolean(match && parseVersion(match[1]));
};
const endIndex = lines.findIndex(
  (line, index) => index > startIndex && versionHeading(line),
);

const notes = lines
  .slice(startIndex, endIndex < 0 ? lines.length : endIndex)
  .join('\n')
  .trim();

if (!notes) {
  throw new Error(`${heading} 对应的更新内容为空`);
}

fs.writeFileSync(outputPath, `${notes}\n`, 'utf8');
console.log(`已生成 ${path.relative(process.cwd(), outputPath)}：${heading}`);
