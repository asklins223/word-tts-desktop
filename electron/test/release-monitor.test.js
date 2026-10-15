'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createReleaseMonitor, RELEASE_CHECK_INTERVAL_MS } = require('../startup-release-check');

function fixture(check) {
    const timers = [];
    const cleared = [];
    const failures = [];
    const monitor = createReleaseMonitor({
        check,
        onFailure: async error => failures.push(error),
        setTimeoutImpl: (callback, delay) => {
            const timer = { callback, delay };
            timers.push(timer);
            return timer;
        },
        clearTimeoutImpl: timer => cleared.push(timer),
    });
    return { timers, cleared, failures, monitor };
}

test('启动后不重复检查，首次及后续定时检查均间隔 30 分钟', async () => {
    let checks = 0;
    const f = fixture(async () => { checks += 1; });
    assert.equal(RELEASE_CHECK_INTERVAL_MS, 1_800_000);
    assert.equal(checks, 0);
    assert.equal(f.timers[0].delay, 1_800_000);
    await f.timers[0].callback();
    assert.equal(checks, 1);
    assert.equal(f.timers[1].delay, 1_800_000);
    f.monitor.dispose();
    assert.deepEqual(f.cleared, [f.timers[1]]);
});

test('前一次检查完成前不会安排下一次检查，退出期间不会重新安排', async () => {
    let finish;
    const f = fixture(() => new Promise(resolve => { finish = resolve; }));
    const pending = f.timers[0].callback();
    assert.equal(f.timers.length, 1);
    f.monitor.dispose();
    finish();
    await pending;
    assert.equal(f.timers.length, 1);
});

test('服务检查失败立即通知停用并停止调度', async () => {
    const failure = new Error('线上服务失效');
    const f = fixture(async () => { throw failure; });
    await f.timers[0].callback();
    assert.deepEqual(f.failures, [failure]);
    assert.equal(f.timers.length, 1);
});

test('退出期间未完成的检查失败不会再次弹错误', async () => {
    let fail;
    const f = fixture(() => new Promise((_, reject) => { fail = reject; }));
    const pending = f.timers[0].callback();
    f.monitor.dispose();
    fail(new Error('cancelled'));
    await pending;
    assert.deepEqual(f.failures, []);
});
