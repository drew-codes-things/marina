const fs = require('fs');
const path = require('path');
const { execFile } = require('child_process');
const { app, ipcMain, screen } = require('electron');

const WINDOW_SIZE = { width: 420, height: 680 };
const SETTLE_MS = 300;
const SAVE_DELAY_MS = 400;
const DRAG_PAUSE_MS = 300;
const MAX_SHAPE_RECTS = 2000;
const SCREEN_MARGIN = 24;
const OVER_FULLSCREEN_TYPE = '_KDE_NET_WM_WINDOW_TYPE_CRITICAL_NOTIFICATION';
const ORDINARY_TYPE = '_NET_WM_WINDOW_TYPE_NORMAL';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const insideAppImage = /\/\.mount_/.test(process.execPath);

function relaunchAsX11IfNeeded() {
  const ozone = app.commandLine.getSwitchValue('ozone-platform');
  if (ozone === 'wayland' && !process.env.MARINA_WAYLAND && !insideAppImage) {
    app.relaunch({ args: [...process.argv.slice(1), '--ozone-platform=x11'] });
    app.exit(0);
  }
}

const settingsFile = () => path.join(app.getPath('userData'), 'linux-overlay.json');

function readSettings() {
  try {
    return JSON.parse(fs.readFileSync(settingsFile(), 'utf8'));
  } catch {
    return {};
  }
}

let overFullScreen = readSettings().overFullScreen ?? true;

function setOverFullScreen(value) {
  overFullScreen = !!value;
  try {
    fs.writeFileSync(settingsFile(), JSON.stringify({ overFullScreen }));
  } catch {}
}

function canLayerOverFullScreen() {
  return /KDE/.test(process.env.XDG_CURRENT_DESKTOP || '')
    && app.commandLine.getSwitchValue('ozone-platform') !== 'wayland';
}

let win = null;
let isStage = () => false;
let pinnedSize = null;
let positionOffset = { x: 0, y: 0 };
let lastMove = null;

function setWindowType(type) {
  return new Promise((resolve) => {
    if (!win || win.isDestroyed()) return resolve(false);
    const id = '0x' + win.getNativeWindowHandle().readUInt32LE(0).toString(16);
    execFile('xprop', ['-id', id, '-f', '_NET_WM_WINDOW_TYPE', '32a', '-set', '_NET_WM_WINDOW_TYPE', type], (err) => {
      if (err) console.log(`Could not set the window type (${err.message}). Is xprop installed?`);
      resolve(!err);
    });
  });
}

function pinSize() {
  if (!win || win.isDestroyed()) return;
  pinnedSize = pinnedSize || win.getSize();
  win.setMinimumSize(...pinnedSize);
  win.setMaximumSize(...pinnedSize);
}

function unpinSize() {
  if (!win || win.isDestroyed()) return;
  win.setMinimumSize(0, 0);
  win.setMaximumSize(0, 0);
}

async function placeExactly(x, y) {
  win.setPosition(x, y);
  await sleep(SETTLE_MS);
  const [actualX, actualY] = win.getPosition();
  positionOffset = { x: x - actualX, y: y - actualY };
  if (positionOffset.x || positionOffset.y) win.setPosition(x + positionOffset.x, y + positionOffset.y);
}

async function applyLayer() {
  const layerable = canLayerOverFullScreen();
  const above = overFullScreen && layerable && !isStage();
  const done = layerable ? await setWindowType(above ? OVER_FULLSCREEN_TYPE : ORDINARY_TYPE) : false;
  if (win && !win.isDestroyed()) win.webContents.send('set-drag-mode', above && done);
}

function defaultPosition() {
  const { workArea } = screen.getPrimaryDisplay();
  return {
    x: workArea.x + workArea.width - WINDOW_SIZE.width - SCREEN_MARGIN,
    y: workArea.y + workArea.height - WINDOW_SIZE.height - SCREEN_MARGIN,
  };
}

function toShapeRects(rects) {
  return rects.slice(0, MAX_SHAPE_RECTS)
    .map((r) => ({ x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) }))
    .filter((r) => [r.x, r.y, r.width, r.height].every(Number.isFinite) && r.width > 0 && r.height > 0);
}

function registerIpc(getWin, stageOn) {
  ipcMain.on('shape', (_e, rects) => {
    const target = getWin();
    if (!target || target.isDestroyed() || !Array.isArray(rects)) return;
    target.setShape(toShapeRects(rects));
  });

  ipcMain.on('move-by', (_e, dx, dy) => {
    const target = getWin();
    if (!target || target.isDestroyed() || stageOn() || !Number.isFinite(dx) || !Number.isFinite(dy)) return;
    const dragging = lastMove && Date.now() - lastMove.at < DRAG_PAUSE_MS;
    const [x, y] = target.getPosition();
    const from = dragging ? lastMove : { x, y };
    const next = { x: from.x + Math.round(dx), y: from.y + Math.round(dy) };
    target.setPosition(next.x + positionOffset.x, next.y + positionOffset.y);
    lastMove = { ...next, at: Date.now() };
  });
}

function windowOptions(saved) {
  const fallback = defaultPosition();
  return {
    width: WINDOW_SIZE.width,
    height: WINDOW_SIZE.height,
    x: saved?.x ?? fallback.x,
    y: saved?.y ?? fallback.y,
    fullscreenable: true,
    icon: path.join(__dirname, 'assets', 'icon.png'),
  };
}

function attach(created, { stage, save, position }) {
  win = created;
  isStage = stage;
  win.webContents.on('did-finish-load', applyLayer);
  win.webContents.once('did-finish-load', async () => {
    pinSize();
    await placeExactly(position.x, position.y);
  });
  let saveTimer = null;
  win.on('move', () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { if (!isStage()) save(); }, SAVE_DELAY_MS);
  });
}

async function setStage(on) {
  if (!win || win.isDestroyed()) return;
  if (on) {
    await applyLayer();
    unpinSize();
  }
  win.setFullScreen(on);
  if (!on) {
    await sleep(SETTLE_MS * 2);
    pinSize();
    await applyLayer();
  }
  win.webContents.send('set-stage', on);
  if (on) { win.show(); win.focus(); }
}

function resetPosition() {
  if (!win || win.isDestroyed()) return;
  placeExactly(60, 60);
  win.show();
}

module.exports = {
  relaunchAsX11IfNeeded,
  canLayerOverFullScreen,
  overFullScreen: () => overFullScreen,
  setOverFullScreen,
  applyLayer,
  windowOptions,
  registerIpc,
  attach,
  setStage,
  resetPosition,
};
