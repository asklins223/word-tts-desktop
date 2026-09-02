'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    activateWindowToFront,
    scheduleWindowActivation,
} = require('../window-activation');

function createWindowMock({ focused = false, visible = false, minimized = false } = {}) {
    const calls = [];
    let state = {
        focused,
        visible,
        minimized,
        alwaysOnTop: false,
    };
    const window = {
        calls,
        isDestroyed: () => false,
        isFocused: () => state.focused,
        isVisible: () => state.visible,
        isMinimized: () => state.minimized,
        isAlwaysOnTop: () => state.alwaysOnTop,
        restore: () => {
            calls.push('restore');
            state.minimized = false;
        },
        show: () => {
            calls.push('show');
            state.visible = true;
        },
        moveTop: () => calls.push('moveTop'),
        focus: () => {
            calls.push('focus');
            if (state.alwaysOnTop) state.focused = true;
        },
        setAlwaysOnTop: value => {
            calls.push(`alwaysOnTop:${value}`);
            state.alwaysOnTop = value;
        },
    };
    return window;
}

test('Windows 激活会恢复、显示、置顶到前台，并恢复原始置顶状态', () => {
    const window = createWindowMock({ focused: false, visible: false, minimized: true });
    const appCalls = [];
    const app = { focus: () => appCalls.push('focus') };

    assert.equal(activateWindowToFront({ app, window, platform: 'win32' }), true);
    assert.equal(window.isFocused(), true);
    assert.equal(window.isVisible(), true);
    assert.equal(window.isMinimized(), false);
    assert.equal(window.isAlwaysOnTop(), false);
    assert.deepEqual(appCalls, ['focus', 'focus']);
    assert.ok(window.calls.includes('alwaysOnTop:true'));
    assert.ok(window.calls.includes('alwaysOnTop:false'));
    assert.ok(window.calls.indexOf('alwaysOnTop:true') < window.calls.indexOf('alwaysOnTop:false'));
});

test('已在前台的窗口不会被永久设置为置顶', () => {
    const window = createWindowMock({ focused: true, visible: true });
    const app = { focus() {} };

    assert.equal(activateWindowToFront({ app, window, platform: 'win32' }), true);
    assert.equal(window.isAlwaysOnTop(), false);
    assert.doesNotMatch(window.calls.join(','), /alwaysOnTop/);
});

test('激活重试会在首次被 Windows 拒绝时继续，成功后取消剩余定时器', () => {
    const window = createWindowMock({ focused: false, visible: true });
    // Make the first two attempts fail even though the helper tried all
    // activation calls; the third attempt represents the window manager
    // accepting foreground activation.
    let focusCalls = 0;
    window.focus = () => {
        window.calls.push('focus');
        focusCalls += 1;
    };
    window.isFocused = () => focusCalls >= 5;
    const timers = [];
    const cleared = [];
    const cancel = scheduleWindowActivation({
        app: { focus() {} },
        getWindow: () => window,
        platform: 'win32',
        forceWindows: false,
        delaysMs: [0, 1, 2],
        setTimeoutImpl: (callback, delay) => {
            const timer = { callback, delay };
            timers.push(timer);
            return timer;
        },
        clearTimeoutImpl: timer => cleared.push(timer),
    });

    assert.equal(timers.length, 3);
    timers[0].callback();
    timers[1].callback();
    timers[2].callback();
    assert.ok(window.calls.filter(call => call === 'focus').length >= 3);
    assert.ok(cleared.length >= 1);
    cancel();
});

test('已销毁窗口不会触发激活调用', () => {
    const calls = [];
    const window = { isDestroyed: () => true, focus: () => calls.push('focus') };
    assert.equal(activateWindowToFront({ app: { focus: () => calls.push('app-focus') }, window, platform: 'win32' }), false);
    assert.deepEqual(calls, []);
});
