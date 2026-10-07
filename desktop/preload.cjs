'use strict';
const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('modport', Object.freeze({
  getLanguage: () => ipcRenderer.invoke('modport:get-language'),
  setLanguage: language => ipcRenderer.invoke('modport:set-language', language),
  getUpdateStatus: () => ipcRenderer.invoke('modport:get-update-status'),
  checkForUpdates: () => ipcRenderer.invoke('modport:check-for-updates'),
  openUpdateDownload: () => ipcRenderer.invoke('modport:open-update-download'),
  openContributionLink: url => ipcRenderer.invoke('modport:open-contribution-link', url),
  onUpdateStatus: callback => {
    if (typeof callback !== 'function') throw new TypeError('Update status callback must be a function');
    const listener = (_event, status) => callback(status);
    ipcRenderer.on('modport:update-status', listener);
    return () => ipcRenderer.removeListener('modport:update-status', listener);
  },
  request: ({method, path, body}) => ipcRenderer.invoke('modport:request', {method, path, body}),
  selectSourceDirectory: () => ipcRenderer.invoke('modport:select-source-directory'),
  openWorkspace: instanceId => ipcRenderer.invoke('modport:open-workspace', instanceId)
}));
