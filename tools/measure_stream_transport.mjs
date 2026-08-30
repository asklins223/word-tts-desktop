#!/usr/bin/env node

/**
 * Measure the bounded stream path used by the desktop transport.
 *
 * This is a local harness, not a claim about Electron, a Provider, or a
 * target device.  By default it uses a synthetic backpressured stream so a
 * large test does not require creating a fixture file.  Pass --input to
 * measure an existing file without copying it into the repository.
 */

import fs from 'node:fs';
import path from 'node:path';
import { Readable, Transform, Writable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { performance } from 'node:perf_hooks';

const DEFAULT_BYTES = 32 * 1024 * 1024;
const DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024;
const MAX_BYTES = 2 * 1024 * 1024 * 1024;
const MAX_CHUNK_SIZE = 16 * 1024 * 1024;

function usage() {
    console.log([
        '用法: node tools/measure_stream_transport.mjs [选项]',
        '',
        '  --bytes N          合成输入大小，默认 33554432',
        '  --chunk-size N     单块大小，默认 4194304，最大 16777216',
        '  --cancel-after N   收到 N 字节后触发取消；0 表示不取消',
        '  --input PATH       使用已有文件作为输入（覆盖 --bytes）',
        '  --help             显示帮助',
        '',
        '输出为一行 JSON；synthetic=true 仅表示本地背压/取消链路的可重复夹具。',
    ].join('\n'));
}

function parsePositiveInteger(value, name, { allowZero = false, max = MAX_BYTES } = {}) {
    const number = Number(value);
    if (!Number.isSafeInteger(number) || (allowZero ? number < 0 : number <= 0) || number > max) {
        throw new Error(`${name} must be an integer in the supported range`);
    }
    return number;
}

function parseArgs(argv) {
    const options = {
        bytes: DEFAULT_BYTES,
        chunkSize: DEFAULT_CHUNK_SIZE,
        cancelAfter: 0,
        input: null,
    };
    for (let index = 0; index < argv.length; index += 1) {
        const arg = argv[index];
        if (arg === '--help' || arg === '-h') {
            usage();
            return null;
        }
        const next = argv[index + 1];
        if (arg === '--bytes') {
            options.bytes = parsePositiveInteger(next, '--bytes');
            index += 1;
        } else if (arg === '--chunk-size') {
            options.chunkSize = parsePositiveInteger(next, '--chunk-size', { max: MAX_CHUNK_SIZE });
            index += 1;
        } else if (arg === '--cancel-after') {
            options.cancelAfter = parsePositiveInteger(next, '--cancel-after', { allowZero: true });
            index += 1;
        } else if (arg === '--input') {
            if (!next || next.startsWith('--')) throw new Error('--input requires a path');
            options.input = path.resolve(next);
            index += 1;
        } else {
            throw new Error(`unknown option: ${arg}`);
        }
    }
    return options;
}

async function* syntheticChunks(totalBytes, chunkSize) {
    let remaining = totalBytes;
    while (remaining > 0) {
        const size = Math.min(remaining, chunkSize);
        yield Buffer.alloc(size);
        remaining -= size;
    }
}

function makeSource(options) {
    if (!options.input) {
        return {
            source: Readable.from(syntheticChunks(options.bytes, options.chunkSize)),
            totalBytes: options.bytes,
            synthetic: true,
            input: null,
        };
    }
    const stat = fs.statSync(options.input);
    if (!stat.isFile()) throw new Error('--input must point to a regular file');
    return {
        source: fs.createReadStream(options.input, { highWaterMark: options.chunkSize }),
        totalBytes: stat.size,
        synthetic: false,
        input: options.input,
    };
}

function abortError() {
    const error = new Error('transport cancelled by harness');
    error.name = 'AbortError';
    error.code = 'USER_CANCELLED';
    return error;
}

async function measure(options) {
    const { source, totalBytes, synthetic, input } = makeSource(options);
    const controller = new AbortController();
    const startedAt = performance.now();
    let receivedBytes = 0;
    let chunks = 0;
    let maxChunkBytes = 0;
    let firstProgressMs = null;
    let cancelTriggered = false;
    const progress = new Transform({
        transform(chunk, _encoding, callback) {
            const bytes = Number(chunk?.byteLength || chunk?.length || 0);
            receivedBytes += bytes;
            chunks += 1;
            maxChunkBytes = Math.max(maxChunkBytes, bytes);
            if (firstProgressMs === null) firstProgressMs = performance.now() - startedAt;
            if (
                !cancelTriggered
                && options.cancelAfter > 0
                && receivedBytes >= options.cancelAfter
                && receivedBytes < totalBytes
            ) {
                cancelTriggered = true;
                controller.abort(abortError());
            }
            callback(null, chunk);
        },
    });
    const sink = new Writable({
        highWaterMark: options.chunkSize,
        write(_chunk, _encoding, callback) {
            setImmediate(callback);
        },
    });
    let error = null;
    try {
        await pipeline(source, progress, sink, { signal: controller.signal });
    } catch (caught) {
        error = caught;
        if (!cancelTriggered) throw caught;
    }
    const elapsedMs = performance.now() - startedAt;
    const cancelled = cancelTriggered || error?.name === 'AbortError' || error?.code === 'ABORT_ERR';
    const result = {
        input,
        synthetic,
        requested_bytes: options.input ? null : options.bytes,
        total_bytes: totalBytes,
        received_bytes: receivedBytes,
        chunks,
        max_chunk_bytes: maxChunkBytes,
        first_progress_ms: firstProgressMs === null ? null : Number(firstProgressMs.toFixed(3)),
        elapsed_ms: Number(elapsedMs.toFixed(3)),
        throughput_mib_s: elapsedMs > 0 ? Number((receivedBytes / 1024 / 1024 / (elapsedMs / 1000)).toFixed(3)) : null,
        cancelled,
        status: cancelled ? 'cancelled' : 'completed',
        rss_bytes: process.memoryUsage().rss,
    };
    if (error && !cancelled) result.error = String(error.message || error);
    return result;
}

async function main() {
    try {
        const options = parseArgs(process.argv.slice(2));
        if (options === null) return;
        if (options.input && options.cancelAfter > 0 && options.cancelAfter >= fs.statSync(options.input).size) {
            throw new Error('--cancel-after must be smaller than the input file size');
        }
        const result = await measure(options);
        console.log(JSON.stringify(result));
    } catch (error) {
        console.error(JSON.stringify({ status: 'error', error: String(error?.message || error) }));
        process.exitCode = 1;
    }
}

await main();
