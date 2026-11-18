const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('nle', {
  pickDir: (opts) => ipcRenderer.invoke('pick-dir', opts),
  pickFile: (opts) => ipcRenderer.invoke('pick-file', opts),
  openPath: (p) => ipcRenderer.invoke('open-path', p),
  defaultOut: () => ipcRenderer.invoke('default-out'),
  convert: (req) => ipcRenderer.invoke('convert', req),
  onLog: (cb) => ipcRenderer.on('log', (_e, obj) => cb(obj)),
  version: () => process.versions.electron,
});
