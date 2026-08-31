import fs from 'node:fs';
import zlib from 'node:zlib';

const [, , inputPath, outputPath] = process.argv;

if (!inputPath || !outputPath) {
  console.error('Usage: node clean-checkerboard.mjs <input.png> <output.png>');
  process.exit(1);
}

const source = fs.readFileSync(inputPath);
const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
if (!source.subarray(0, 8).equals(signature)) throw new Error('Input is not a PNG');

let width;
let height;
let bitDepth;
let colorType;
let interlace;
const idatParts = [];

for (let offset = 8; offset < source.length;) {
  const length = source.readUInt32BE(offset);
  const type = source.toString('ascii', offset + 4, offset + 8);
  const data = source.subarray(offset + 8, offset + 8 + length);
  offset += length + 12;

  if (type === 'IHDR') {
    width = data.readUInt32BE(0);
    height = data.readUInt32BE(4);
    bitDepth = data[8];
    colorType = data[9];
    interlace = data[12];
  } else if (type === 'IDAT') {
    idatParts.push(data);
  }
}

if (!width || !height || bitDepth !== 8 || ![2, 6].includes(colorType) || interlace !== 0) {
  throw new Error('Only non-interlaced 8-bit RGB/RGBA PNGs are supported');
}

const channels = colorType === 6 ? 4 : 3;
const bytesPerRow = width * channels;
const decoded = zlib.inflateSync(Buffer.concat(idatParts));
const rgba = Buffer.alloc(width * height * 4);
const previous = Buffer.alloc(bytesPerRow);
let sourceOffset = 0;

function paeth(a, b, c) {
  const p = a + b - c;
  const pa = Math.abs(p - a);
  const pb = Math.abs(p - b);
  const pc = Math.abs(p - c);
  return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
}

for (let y = 0; y < height; y += 1) {
  const filter = decoded[sourceOffset++];
  const row = Buffer.from(decoded.subarray(sourceOffset, sourceOffset + bytesPerRow));
  sourceOffset += bytesPerRow;

  for (let i = 0; i < bytesPerRow; i += 1) {
    const left = i >= channels ? row[i - channels] : 0;
    const up = previous[i] ?? 0;
    const upLeft = i >= channels ? previous[i - channels] : 0;
    if (filter === 1) row[i] = (row[i] + left) & 255;
    else if (filter === 2) row[i] = (row[i] + up) & 255;
    else if (filter === 3) row[i] = (row[i] + Math.floor((left + up) / 2)) & 255;
    else if (filter === 4) row[i] = (row[i] + paeth(left, up, upLeft)) & 255;
    else if (filter !== 0) throw new Error(`Unsupported PNG filter: ${filter}`);
  }

  for (let x = 0; x < width; x += 1) {
    const sourceIndex = x * channels;
    const targetIndex = (y * width + x) * 4;
    rgba[targetIndex] = row[sourceIndex];
    rgba[targetIndex + 1] = row[sourceIndex + 1];
    rgba[targetIndex + 2] = row[sourceIndex + 2];
    rgba[targetIndex + 3] = channels === 4 ? row[sourceIndex + 3] : 255;
  }
  row.copy(previous);
}

const borderColors = new Map();
const borderSize = Math.min(24, Math.floor(Math.min(width, height) / 12));
const countColor = (x, y) => {
  const index = (y * width + x) * 4;
  const r = rgba[index];
  const g = rgba[index + 1];
  const b = rgba[index + 2];
  if (Math.max(r, g, b) - Math.min(r, g, b) > 18) return;
  const key = `${r},${g},${b}`;
  borderColors.set(key, (borderColors.get(key) || 0) + 1);
};

for (let y = 0; y < borderSize; y += 1) {
  for (let x = 0; x < width; x += 1) {
    countColor(x, y);
    countColor(x, height - 1 - y);
  }
}
for (let x = 0; x < borderSize; x += 1) {
  for (let y = borderSize; y < height - borderSize; y += 1) {
    countColor(x, y);
    countColor(width - 1 - x, y);
  }
}

const backgroundColors = [...borderColors.entries()]
  .sort((a, b) => b[1] - a[1])
  .slice(0, 4)
  .map(([key]) => key.split(',').map(Number));

if (backgroundColors.length === 0) throw new Error('Could not identify checkerboard colors');

const isBackgroundLike = (index) => {
  if (rgba[index + 3] === 0) return true;
  const r = rgba[index];
  const g = rgba[index + 1];
  const b = rgba[index + 2];
  return backgroundColors.some(([br, bg, bb]) => {
    const distance = Math.abs(r - br) + Math.abs(g - bg) + Math.abs(b - bb);
    return distance <= 42;
  });
};

const visited = new Uint8Array(width * height);
const queue = new Int32Array(width * height);
let head = 0;
let tail = 0;

const enqueueIfBackground = (x, y) => {
  if (x < 0 || y < 0 || x >= width || y >= height) return;
  const pixel = y * width + x;
  if (visited[pixel]) return;
  const index = pixel * 4;
  if (!isBackgroundLike(index)) return;
  visited[pixel] = 1;
  queue[tail++] = pixel;
};

for (let x = 0; x < width; x += 1) {
  enqueueIfBackground(x, 0);
  enqueueIfBackground(x, height - 1);
}
for (let y = 1; y < height - 1; y += 1) {
  enqueueIfBackground(0, y);
  enqueueIfBackground(width - 1, y);
}

while (head < tail) {
  const pixel = queue[head++];
  const x = pixel % width;
  const y = Math.floor(pixel / width);
  enqueueIfBackground(x - 1, y);
  enqueueIfBackground(x + 1, y);
  enqueueIfBackground(x, y - 1);
  enqueueIfBackground(x, y + 1);
}

for (let pixel = 0; pixel < visited.length; pixel += 1) {
  if (!visited[pixel]) continue;
  const index = pixel * 4;
  rgba[index] = 0;
  rgba[index + 1] = 0;
  rgba[index + 2] = 0;
  rgba[index + 3] = 0;
}

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function pngChunk(type, data) {
  const typeBuffer = Buffer.from(type, 'ascii');
  const chunk = Buffer.alloc(12 + data.length);
  chunk.writeUInt32BE(data.length, 0);
  typeBuffer.copy(chunk, 4);
  data.copy(chunk, 8);
  chunk.writeUInt32BE(crc32(Buffer.concat([typeBuffer, data])), 8 + data.length);
  return chunk;
}

const scanlines = Buffer.alloc((width * 4 + 1) * height);
for (let y = 0; y < height; y += 1) {
  const rowStart = y * (width * 4 + 1);
  scanlines[rowStart] = 0;
  rgba.copy(scanlines, rowStart + 1, y * width * 4, (y + 1) * width * 4);
}

const header = Buffer.alloc(13);
header.writeUInt32BE(width, 0);
header.writeUInt32BE(height, 4);
header[8] = 8;
header[9] = 6;
const output = Buffer.concat([
  signature,
  pngChunk('IHDR', header),
  pngChunk('IDAT', zlib.deflateSync(scanlines, { level: 9 })),
  pngChunk('IEND', Buffer.alloc(0)),
]);

fs.writeFileSync(outputPath, output);
console.log(`Removed connected checkerboard background using ${backgroundColors.length} border colors; transparent pixels: ${tail}.`);
