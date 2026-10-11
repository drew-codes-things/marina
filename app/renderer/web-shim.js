// Only loaded when a browser gets Marina from the bridge (server/process/web.py).
// Stands in for Electron's preload.js so app.js runs unchanged on a phone.
(() => {
  const noop = () => {};

  window.marina = {
    web: true,
    loadVRM: async () => {
      const res = await fetch('model.vrm');
      if (!res.ok) return { error: 'There is no avatar on the server.' };
      return { name: 'model.vrm', buffer: await res.arrayBuffer() };
    },
    pickVRM: async () => ({ canceled: true }),
    quit: noop,
    minimize: noop,
    clickThrough: noop,
    captureScreen: async () => null,
    openersChanged: noop,
    onToggleListen: noop,
    onPickModel: noop,
    onLookAtScreen: noop,
    onBridgeDown: noop,
    onBridgeUp: noop,
    onSetOpeners: noop,
    onInterrupt: noop,
    onSetPreset: noop,
    onSetStage: noop,
  };

  document.documentElement.classList.add('web');

  // iOS mutes Web Audio when the ring switch is on silent unless the page
  // says it is playing media.
  if (navigator.audioSession) navigator.audioSession.type = 'playback';

  document.addEventListener('DOMContentLoaded', () => {
    const foot = document.querySelector('.pick-foot');
    if (foot) foot.textContent = 'History and memory stay on your home server either way.';
  });
})();
