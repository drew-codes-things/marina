const { app, BrowserWindow, Menu, Tray, desktopCapturer, ipcMain, globalShortcut, nativeImage, screen, shell, systemPreferences, dialog } = require('electron');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const BUNDLED_MODELS_DIR = path.join(__dirname, 'models');

function userModelsDir() {
  return path.join(app.getPath('userData'), 'models');
}

function findModel() {
  for (const dir of [userModelsDir(), BUNDLED_MODELS_DIR]) {
    let files = [];
    try {
      files = fs.readdirSync(dir).filter((f) => f.toLowerCase().endsWith('.vrm'));
    } catch {
      continue;
    }
    if (!files.length) continue;
    files.sort();
    const chosen = files.includes('model.vrm') ? 'model.vrm' : files[0];
    return { dir, name: chosen, file: path.join(dir, chosen) };
  }
  return null;
}

function readAsTransferable(file) {
  const buf = fs.readFileSync(file);
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
}

// Packaged, everything the backend needs ships inside the bundle and the
// files it writes go to Application Support. In a checkout both are the repo.
function backend() {
  if (app.isPackaged) {
    const root = path.join(process.resourcesPath, 'backend');
    return {
      root,
      data: app.getPath('userData'),
      python: path.join(root, 'python', 'bin', 'python3.12'),
      script: path.join(root, 'server', 'marina_server.py'),
    };
  }
  const root = path.join(__dirname, '..');
  return {
    root,
    data: root,
    python: path.join(root, '.venv', 'bin', 'python'),
    script: path.join(root, 'server', 'marina_server.py'),
  };
}

function configFile() {
  return path.join(backend().data, 'character_config.yaml');
}

const BRIDGE_URL = 'http://127.0.0.1:8765';

async function bridgeAlive() {
  try {
    const res = await fetch(`${BRIDGE_URL}/health`, { signal: AbortSignal.timeout(1500) });
    return res.ok;
  } catch {
    return false;
  }
}

async function startBridge() {
  if (await bridgeAlive()) {
    console.log('Bridge already running; not starting another.');
    return;
  }

  const { root, data, python, script } = backend();

  if (!fs.existsSync(python) || !fs.existsSync(script)) {
    dialog.showErrorBox(
      'Marina cannot find her backend',
      `Expected:\n  ${python}\n  ${script}\n\n` +
      (app.isPackaged
        ? 'This copy of Marina.app looks incomplete. Download it again.'
        : 'Run ./setup-mac.sh in the project folder.'),
    );
    return;
  }

  try {
    fs.mkdirSync(data, { recursive: true });
  } catch {}

  const logPath = path.join(app.getPath('userData'), 'bridge.log');
  let out = 'ignore';
  try {
    fs.mkdirSync(path.dirname(logPath), { recursive: true });
    out = fs.openSync(logPath, 'a');
    fs.writeSync(out, `\n--- started ${new Date().toISOString()} ---\n`);
  } catch {
    out = 'ignore';
  }

  bridge = spawn(python, [script], {
    cwd: root,
    stdio: ['ignore', out, out],
    env: {
      ...process.env,
      PYTHONUNBUFFERED: '1',
      MARINA_ROOT: root,
      MARINA_DATA: data,
    },
  });
  bridge.on('exit', (code) => {
    console.log(`Bridge exited (${code})`);
    bridge = null;
    if (!quitting) scheduleRestart(code);
  });
}

let quitting = false;
const RESTART_DELAYS = [1000, 2000, 5000, 10000, 30000];
let restartCount = 0;
let restartTimer = null;
let lastHealthy = Date.now();

function scheduleRestart(code) {
  if (restartTimer) return;

  if (Date.now() - lastHealthy > 60000) restartCount = 0;

  if (restartCount >= RESTART_DELAYS.length) {
    notifyRenderer('bridge-down',
      'Marina\u2019s backend keeps failing to start. See the bridge log.');
    return;
  }

  const wait = RESTART_DELAYS[restartCount++];
  notifyRenderer('bridge-down', `Backend stopped (${code}). Restarting\u2026`);
  restartTimer = setTimeout(async () => {
    restartTimer = null;
    await startBridge();
  }, wait);
}

function notifyRenderer(channel, message) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, message);
}

function watchBridge() {
  setInterval(async () => {
    if (quitting) return;
    if (await bridgeAlive()) {
      if (restartCount) notifyRenderer('bridge-up', '');
      restartCount = 0;
      lastHealthy = Date.now();
      return;
    }

    if (bridge && Date.now() - lastHealthy > 45000) {
      console.log('Bridge is up but not responding; restarting it.');
      lastHealthy = Date.now();
      try { bridge.kill('SIGKILL'); } catch {}
    }
  }, 5000);
}

function stopBridge() {
  if (!bridge) return;
  bridge.kill('SIGTERM');
  bridge = null;
}

const STATE_FILE = () => path.join(app.getPath('userData'), 'window-state.json');

let win = null;
let tray = null;
let bridge = null;

function readState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE(), 'utf8'));
  } catch {
    return null;
  }
}

function writeState() {
  if (!win || win.isDestroyed()) return;
  const [x, y] = win.getPosition();
  const [width, height] = win.getSize();
  try {
    fs.writeFileSync(STATE_FILE(), JSON.stringify({ x, y, width, height }));
  } catch {}
}

let ignoring = true;
ipcMain.on('click-through', (_e, ignore) => {
  if (!win || win.isDestroyed() || stage || ignore === ignoring) return;
  ignoring = ignore;
  win.setIgnoreMouseEvents(ignore, { forward: true });
});

// Stage view: full screen on a solid backdrop, for turning the laptop round
// to show her to people. The renderer paints the backdrop.
let stage = false;

function setStage(on) {
  if (!win) return;
  stage = !!on;
  win.setSimpleFullScreen(stage);
  win.setIgnoreMouseEvents(!stage, { forward: true });
  ignoring = !stage;
  win.webContents.send('set-stage', stage);
  if (stage) { win.show(); win.focus(); }
}

function createWindow() {
  const saved = readState();
  const { workArea } = screen.getPrimaryDisplay();
  const width = saved?.width ?? 420;
  const height = saved?.height ?? 680;

  win = new BrowserWindow({
    width,
    height,
    x: saved?.x ?? workArea.x + workArea.width - width - 24,
    y: saved?.y ?? workArea.y + workArea.height - height - 24,

    transparent: true,
    frame: false,
    hasShadow: false,
    backgroundColor: '#00000000',
    alwaysOnTop: true,
    resizable: true,
    skipTaskbar: true,
    fullscreenable: false,
    titleBarStyle: 'customButtonsOnHover',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });

  win.setIgnoreMouseEvents(true, { forward: true });

  win.setAlwaysOnTop(true, 'floating');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  win.webContents.setBackgroundThrottling(false);
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  ignoring = true;

  win.on('moved', () => { if (!stage) writeState(); });
  win.on('resized', () => { if (!stage) writeState(); });
  win.on('closed', () => { win = null; });

  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
}

let openersOn = true;

// The personalities the bridge offers, for the menu bar. Fetched when the
// menu is about to show, since the bridge owns the current choice.
let presetState = { preset: null, presets: [] };

async function fetchPresets() {
  try {
    const res = await fetch(`${BRIDGE_URL}/preset`, { signal: AbortSignal.timeout(1500) });
    if (res.ok) presetState = await res.json();
  } catch {}
}

function buildTray() {
  const icon = nativeImage.createFromPath(path.join(__dirname, 'assets', 'trayTemplate.png'));
  icon.setTemplateImage(true);
  tray = new Tray(icon);
  tray.setToolTip('Marina');

  const refresh = () => {
    tray.setContextMenu(Menu.buildFromTemplate([
      {
        label: win && win.isVisible() ? 'Hide Marina' : 'Show Marina',
        click: () => {
          if (!win || win.isDestroyed()) return createWindow();
          win.isVisible() ? win.hide() : win.show();
        },
      },
      { type: 'separator' },
      {
        label: 'Let her speak first',
        type: 'checkbox',
        checked: openersOn,
        click: (item) => {
          openersOn = item.checked;
          win?.webContents.send('set-openers', openersOn);
        },
      },
      { type: 'separator' },
      ...presetState.presets.map((p) => ({
        label: `${p.label} mode`,
        type: 'radio',
        checked: p.name === presetState.preset,
        click: () => {
          presetState.preset = p.name;
          win?.webContents.send('set-preset', p.name);
        },
      })),
      ...(presetState.presets.length ? [{ type: 'separator' }] : []),
      {
        label: 'Stage view (full screen)',
        type: 'checkbox',
        checked: stage,
        accelerator: 'Command+Shift+F',
        click: (item) => setStage(item.checked),
      },
      { type: 'separator' },
      { label: 'Change model…', click: () => { win?.show(); win?.webContents.send('pick-model'); } },
      { label: 'Reset position', click: () => { if (win) { win.setBounds({ x: 60, y: 60, width: 420, height: 680 }); win.show(); } } },
      { label: 'Reload', click: () => win?.reload() },
      { label: 'Edit config…', click: () => shell.openPath(configFile()) },
      { label: 'Open bridge log', click: () => shell.openPath(path.join(app.getPath('userData'), 'bridge.log')) },
      { type: 'separator' },
      { label: 'Quit Marina', accelerator: 'Command+Shift+Q', click: () => app.quit() },
    ]));
  };

  refresh();
  tray.on('mouse-move', () => { refresh(); fetchPresets().then(refresh); });
  fetchPresets().then(refresh);
  return refresh;
}

app.whenReady().then(() => {
  startBridge();
  watchBridge();
  createWindow();
  buildTray();

  globalShortcut.register('CommandOrControl+Shift+Q', () => app.quit());
  globalShortcut.register('CommandOrControl+Shift+H', () => {
    if (!win) return createWindow();
    win.isVisible() ? win.hide() : win.show();
  });

  globalShortcut.register('CommandOrControl+Shift+.', () => {
    win?.webContents.send('interrupt');
  });
  globalShortcut.register('CommandOrControl+Shift+F', () => setStage(!stage));
  globalShortcut.register('CommandOrControl+Shift+Space', () => {
    win?.webContents.send('toggle-listen');
  });

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('before-quit', () => { quitting = true; });
app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  if (restartTimer) clearTimeout(restartTimer);
  stopBridge();
});
app.on('window-all-closed', () => app.quit());

ipcMain.handle('load-vrm', async () => {
  const found = findModel();
  if (!found) {
    return { error: `No .vrm found in ${userModelsDir()} or ${BUNDLED_MODELS_DIR}` };
  }
  return { name: found.name, buffer: readAsTransferable(found.file) };
});

ipcMain.handle('pick-vrm', async () => {
  const res = await dialog.showOpenDialog(win, {
    title: 'Choose a VRM model',
    filters: [{ name: 'VRM', extensions: ['vrm'] }],
    properties: ['openFile'],
  });
  if (res.canceled || res.filePaths.length === 0) return { canceled: true };

  const src = res.filePaths[0];
  const dir = userModelsDir();
  try {
    fs.mkdirSync(dir, { recursive: true });

    for (const f of fs.readdirSync(dir)) {
      if (f.toLowerCase().endsWith('.vrm')) fs.rmSync(path.join(dir, f), { force: true });
    }
    const dest = path.join(dir, 'model.vrm');
    fs.copyFileSync(src, dest);
    return { name: path.basename(src), buffer: readAsTransferable(dest) };
  } catch (e) {
    dialog.showErrorBox('Could not save that model', `${dir}\n\n${e.message}`);
    return { canceled: true };
  }
});

ipcMain.handle('capture-screen', async () => {
  if (process.platform === 'darwin') {
    const status = systemPreferences.getMediaAccessStatus('screen');
    if (status !== 'granted') {
      return {
        error: 'macOS has not granted screen recording permission.\n\n'
             + 'System Settings > Privacy & Security > Screen & System Audio '
             + 'Recording > enable Marina, then restart her.',
      };
    }
  }

  const wasVisible = win && !win.isDestroyed() && win.isVisible();
  if (wasVisible) win.hide();

  try {
    await new Promise((r) => setTimeout(r, 220));

    const { width, height } = screen.getPrimaryDisplay().size;
    const scale = Math.min(1, 1400 / width);
    const sources = await desktopCapturer.getSources({
      types: ['screen'],
      thumbnailSize: {
        width: Math.round(width * scale),
        height: Math.round(height * scale),
      },
    });

    if (!sources.length) return { error: 'No screen source available.' };
    const shot = sources[0].thumbnail;
    if (shot.isEmpty()) return { error: 'Screen capture came back empty.' };

    return { image: shot.toJPEG(72).toString('base64'), mime: 'image/jpeg' };
  } catch (e) {
    return { error: `Screen capture failed: ${e.message}` };
  } finally {
    if (wasVisible) win.show();
  }
});

ipcMain.on('openers-changed', (_e, on) => { openersOn = !!on; });
ipcMain.on('quit', () => app.quit());
ipcMain.on('minimize', () => win?.hide());
