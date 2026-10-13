const { app, BrowserWindow, ipcMain } = require('electron');
const http = require('http');
const fs = require('fs'); const path = require('path');
const ROOT = path.join(__dirname, '..');

ipcMain.handle('load-vrm', async () => {
  const buf = fs.readFileSync(path.join(ROOT, 'models', 'model.vrm'));
  return { name: 'model.vrm', buffer: buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) };
});
ipcMain.handle('pick-vrm', async () => ({ canceled: true }));
ipcMain.on('quit', () => {}); ipcMain.on('minimize', () => {});

const MISSING = ["To use the mouse and keyboard: ydotool isn't installed."];
const bridge = { enabled: false, posts: [] };

function fakeBridge() {
  return http.createServer((req, res) => {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Headers', '*');
    res.setHeader('Access-Control-Allow-Methods', '*');
    if (req.method === 'OPTIONS') { res.writeHead(204); return res.end(); }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    if (req.url !== '/computer') {
      return res.end(JSON.stringify({ ok: true, model: 'fake-model', tts: 'fake', whisper_loaded: true }));
    }
    const reply = () => res.end(JSON.stringify(
      { enabled: bridge.enabled, screen: true, control: false, missing: bridge.enabled ? MISSING : [] }));
    if (req.method !== 'POST') return reply();
    let body = '';
    req.on('data', (chunk) => { body += chunk; });
    req.on('end', () => {
      bridge.posts.push(JSON.parse(body));
      bridge.enabled = JSON.parse(body).enabled;
      reply();
    });
  });
}

const button = () => `(() => {
  const b = document.getElementById('btn-computer');
  const notice = document.getElementById('notice');
  return { on: b.classList.contains('on'), pressed: b.getAttribute('aria-pressed'), title: b.title,
           notice: notice.classList.contains('hidden') ? null : document.getElementById('notice-text').textContent };
})()`;

const failures = [];
const check = (name, ok, detail) => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : `  (${detail})`}`);
  if (!ok) failures.push(name);
};
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

app.whenReady().then(async () => {
  const srv = fakeBridge();
  await new Promise((r) => srv.listen(8765, '127.0.0.1', r));
  const win = new BrowserWindow({ width: 420, height: 680, show: false,
    webPreferences: { preload: path.join(ROOT, 'preload.js'), contextIsolation: true, backgroundThrottling: false } });
  await win.loadFile(path.join(ROOT, 'renderer', 'index.html'));
  await wait(2500);

  let s = await win.webContents.executeJavaScript(button());
  check('starts off, matching the bridge', !s.on && s.pressed === 'false', JSON.stringify(s));

  await win.webContents.executeJavaScript(`document.getElementById('btn-computer').click(); true`);
  await wait(800);
  s = await win.webContents.executeJavaScript(button());
  check('click turns it on', bridge.enabled && s.on && s.pressed === 'true', JSON.stringify({ bridge, s }));
  check('the bridge was asked for enabled: true', JSON.stringify(bridge.posts) === '[{"enabled":true}]',
    JSON.stringify(bridge.posts));
  check('says what is missing on the desktop', !!s.notice && s.notice.includes('ydotool'), JSON.stringify(s.notice));
  check('title says it is on and can be turned off', /Click to turn off/.test(s.title) && /needs setup/.test(s.title), s.title);

  await win.webContents.executeJavaScript(`document.getElementById('btn-computer').click(); true`);
  await wait(800);
  s = await win.webContents.executeJavaScript(button());
  check('second click turns it off', !bridge.enabled && !s.on && s.pressed === 'false', JSON.stringify({ bridge, s }));

  bridge.enabled = true;
  await win.loadFile(path.join(ROOT, 'renderer', 'index.html'));
  await wait(2500);
  s = await win.webContents.executeJavaScript(button());
  check('a restart picks up the bridge state', s.on && s.pressed === 'true', JSON.stringify(s));

  srv.close();
  console.log(failures.length ? `\n${failures.length} failed` : '\nall passed');
  app.exit(failures.length ? 1 : 0);
});
