const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('marina', {
  loadVRM: () => ipcRenderer.invoke('load-vrm'),
  pickVRM: () => ipcRenderer.invoke('pick-vrm'),
  quit: () => ipcRenderer.send('quit'),
  minimize: () => ipcRenderer.send('minimize'),
  clickThrough: (ignore) => ipcRenderer.send('click-through', ignore),
  onToggleListen: (cb) => ipcRenderer.on('toggle-listen', () => cb()),
  onPickModel: (cb) => ipcRenderer.on('pick-model', () => cb()),
  captureScreen: () => ipcRenderer.invoke('capture-screen'),
  onLookAtScreen: (cb) => ipcRenderer.on('look-at-screen', () => cb()),
  onBridgeDown: (cb) => ipcRenderer.on('bridge-down', (_e, msg) => cb(msg)),
  onBridgeUp: (cb) => ipcRenderer.on('bridge-up', () => cb()),
  onSetOpeners: (cb) => ipcRenderer.on('set-openers', (_e, on) => cb(on)),
  onInterrupt: (cb) => ipcRenderer.on('interrupt', () => cb()),
  onSetPreset: (cb) => ipcRenderer.on('set-preset', (_e, name) => cb(name)),
  onSetStage: (cb) => ipcRenderer.on('set-stage', (_e, on) => cb(on)),
  openersChanged: (on) => ipcRenderer.send('openers-changed', on),
});
