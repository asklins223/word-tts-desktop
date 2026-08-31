'use strict';

const { contextBridge, ipcRenderer } = require('electron');

function subscribe(channel, callback) {
    if (typeof callback !== 'function') return () => {};
    const handler = (_event, payload) => callback(payload);
    ipcRenderer.on(channel, handler);
    return () => ipcRenderer.removeListener(channel, handler);
}

contextBridge.exposeInMainWorld('installerAPI', {
    getConfig: () => ipcRenderer.invoke('installer-config'),
    chooseDirectory: (defaultPath) => ipcRenderer.invoke('installer-choose-directory', defaultPath),
    start: (plan) => ipcRenderer.invoke('installer-start', plan),
    cancel: () => ipcRenderer.invoke('installer-cancel'),
    launch: () => ipcRenderer.invoke('installer-launch'),
    close: () => ipcRenderer.invoke('installer-close'),
    minimize: () => ipcRenderer.invoke('installer-minimize'),
    onProgress: (callback) => subscribe('installer-progress', callback),
    onComplete: (callback) => subscribe('installer-complete', callback),
    onError: (callback) => subscribe('installer-error', callback),
});
