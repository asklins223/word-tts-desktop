'use strict';

// Windows may allow a newly launched GUI process to create a visible window
// without making it the foreground window. This is especially common when the
// process was launched by an installer that is closing at the same time. Keep
// the OS-specific activation sequence in a small, dependency-free module so it
// can be exercised without booting Electron in unit tests.
const DEFAULT_ACTIVATION_DELAYS_MS = Object.freeze([0, 80, 320, 1_000]);

function isUsableWindow(window) {
    return Boolean(
        window
        && (typeof window.isDestroyed !== 'function' || !window.isDestroyed()),
    );
}

function callIfAvailable(receiver, method, ...args) {
    if (typeof receiver?.[method] !== 'function') return undefined;
    return receiver[method](...args);
}

function activateWindowToFront({ app, window, platform = process.platform, forceWindows = true } = {}) {
    if (!isUsableWindow(window)) return false;

    try {
        if (window.isMinimized?.()) callIfAvailable(window, 'restore');
        // Calling show even when the window is already visible is intentional:
        // it re-enters the normal Win32 activation path after a hidden launch.
        callIfAvailable(window, 'show');

        if (platform === 'darwin') {
            callIfAvailable(app, 'focus', { steal: true });
        } else {
            // Electron documents app.focus() as focusing the application's
            // first window on Windows. It complements BrowserWindow.focus()
            // when the process was launched outside the shell.
            callIfAvailable(app, 'focus');
        }
        callIfAvailable(window, 'moveTop');
        callIfAvailable(window, 'focus');

        // Windows' foreground-lock policy can reject the first focus request
        // from a process started by Setup.exe. Temporarily making the window
        // topmost gives that process an eligible activation opportunity. The
        // original topmost state is restored immediately so the app does not
        // remain above other applications.
        if (
            platform === 'win32'
            && forceWindows
            && window.isFocused?.() === false
            && typeof window.setAlwaysOnTop === 'function'
        ) {
            const wasAlwaysOnTop = window.isAlwaysOnTop?.() === true;
            try {
                if (!wasAlwaysOnTop) callIfAvailable(window, 'setAlwaysOnTop', true);
                callIfAvailable(window, 'show');
                callIfAvailable(window, 'moveTop');
                callIfAvailable(window, 'focus');
            } finally {
                if (!wasAlwaysOnTop) callIfAvailable(window, 'setAlwaysOnTop', false);
            }
        }

        // A second pass after the topmost handoff is useful on Windows where
        // focus state is updated asynchronously by the window manager.
        if (platform === 'darwin') callIfAvailable(app, 'focus', { steal: true });
        else callIfAvailable(app, 'focus');
        callIfAvailable(window, 'focus');
        return true;
    } catch (_) {
        // Activation is best effort. A destroyed window or a platform-specific
        // API failure must not take down the already-started application.
        return isUsableWindow(window);
    }
}

function scheduleWindowActivation({
    app,
    getWindow,
    platform = process.platform,
    forceWindows = true,
    delaysMs = DEFAULT_ACTIVATION_DELAYS_MS,
    setTimeoutImpl = setTimeout,
    clearTimeoutImpl = clearTimeout,
} = {}) {
    let cancelled = false;
    const timers = [];
    const delays = Array.isArray(delaysMs) && delaysMs.length > 0
        ? delaysMs
        : DEFAULT_ACTIVATION_DELAYS_MS;

    const cancel = () => {
        if (cancelled) return;
        cancelled = true;
        timers.splice(0).forEach(timer => clearTimeoutImpl(timer));
    };

    delays.forEach((delay) => {
        const timer = setTimeoutImpl(() => {
            if (cancelled) return;
            const window = typeof getWindow === 'function' ? getWindow() : null;
            if (!activateWindowToFront({ app, window, platform, forceWindows })) return;
            // Stop retrying as soon as the OS reports focus. If the first
            // attempt is rejected, later timers remain available.
            if (typeof window?.isFocused !== 'function' || window.isFocused()) cancel();
        }, Math.max(0, Number(delay) || 0));
        timers.push(timer);
    });

    return cancel;
}

module.exports = {
    DEFAULT_ACTIVATION_DELAYS_MS,
    activateWindowToFront,
    scheduleWindowActivation,
};
