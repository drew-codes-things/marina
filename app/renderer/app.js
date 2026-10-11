import { THREE, GLTFLoader, VRMLoaderPlugin, VRMUtils } from './vendor/vrm-bundle.js';

// In a browser the page comes from the bridge itself (see web-shim.js).
const WEB = !!window.marina?.web;
const BRIDGE = WEB ? '' : 'http://127.0.0.1:8765';

const el = (id) => document.getElementById(id);
const canvas     = el('stage');
const bubble     = el('bubble');
const bubbleText = el('bubble-text');
const notice     = el('notice');
const input      = el('input');
const btnSend    = el('btn-send');
const btnMic     = el('btn-mic');
const dot        = el('dot');
const statusText = el('status-text');

const renderer = new THREE.WebGLRenderer({
  canvas,
  alpha: true,
  antialias: true,
  preserveDrawingBuffer: true,
});
renderer.setClearColor(0x000000, 0);
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();

const camera = new THREE.PerspectiveCamera(21, 1, 0.1, 20);

const key = new THREE.DirectionalLight(0xffffff, 2.0);
key.position.set(1, 1.6, 2.2);
scene.add(key);
scene.add(new THREE.AmbientLight(0xffffff, 1.2));

const lookTarget = new THREE.Object3D();
lookTarget.position.set(0, 0, -1);
camera.add(lookTarget);
scene.add(camera);

let vrm = null;

function resize() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  if (vrm) frameUpperBody(vrm);
}
window.addEventListener('resize', resize);
resize();

let bones = {};
let basePose = {};
let springs = [];
let mouthCloseTargets = [];

const loader = new GLTFLoader();
loader.register((parser) => new VRMLoaderPlugin(parser));

function frameUpperBody(v) {
  const head = v.humanoid?.getNormalizedBoneNode('head');
  const target = new THREE.Vector3();
  if (head) {
    v.scene.updateWorldMatrix(true, true);
    head.getWorldPosition(target);
  } else {
    new THREE.Box3().setFromObject(v.scene).getCenter(target);
  }

  // A tall, narrow screen (a phone) would show little more than her face, so
  // pull back until at least MIN_WIDTH metres fit across, and lower the aim
  // so her head stays near the top rather than floating in the middle.
  const MIN_WIDTH = 0.5;
  const VIEW_HEIGHT = Math.max(0.67, MIN_WIDTH / camera.aspect);
  const DROP = 0.06 + (VIEW_HEIGHT - 0.67) * 0.3;
  const fovRad = (camera.fov * Math.PI) / 180;
  const dist = VIEW_HEIGHT / (2 * Math.tan(fovRad / 2));

  camera.position.set(target.x, target.y - DROP, target.z + dist);
  camera.lookAt(target.x, target.y - DROP, target.z);
  camera.updateMatrixWorld(true);
}

async function mountVRM(arrayBuffer, label) {
  const gltf = await loader.parseAsync(arrayBuffer, '');
  const next = gltf.userData.vrm;
  if (!next) throw new Error('That file loaded, but it has no VRM data in it.');

  if (vrm) {
    scene.remove(vrm.scene);
    VRMUtils.deepDispose?.(vrm.scene);
    vrm = null;
  }

  VRMUtils.rotateVRM0(next);
  VRMUtils.removeUnnecessaryVertices?.(next.scene);
  VRMUtils.combineSkeletons?.(next.scene);

  next.scene.traverse((o) => { o.frustumCulled = false; });

  buildRestPose(next);

  collectSprings(next);
  collectMouthClose(next);
  collectBrows(next);
  restyleFace(next);

  if (next.lookAt) {
    next.lookAt.target = lookTarget;
    next.lookAt.autoUpdate = true;
  }

  scene.add(next.scene);
  vrm = next;
  frameUpperBody(next);

  const version = next.meta?.metaVersion === '1' ? 'VRM 1.0' : 'VRM 0.x';
  setStatus('ok', `${label ?? 'model'} · ${version}`);
  hideNotice('model');
}

const IDLE_BONES = [
  'hips', 'spine', 'chest', 'upperChest', 'neck', 'head',
  'leftShoulder', 'rightShoulder',
  'leftUpperArm', 'rightUpperArm',
  'leftLowerArm', 'rightLowerArm',
  'leftHand', 'rightHand',
];

const REST_POSE = {
  leftShoulder:  [0, 0, 0.05],
  rightShoulder: [0, 0, -0.06],
  leftUpperArm:  [0.06, 0, -1.24],
  rightUpperArm: [0.04, 0, 1.20],
  leftLowerArm:  [0, 0.24, -0.10],
  rightLowerArm: [0, -0.20, 0.09],
  leftHand:      [0, 0, -0.07],
  rightHand:     [0, 0, 0.05],
  spine:         [0.02, 0, 0],
  chest:         [0.01, 0, 0],
};

function buildRestPose(v) {
  const h = v.humanoid;
  bones = {};
  basePose = {};
  if (!h) return;

  for (const name of IDLE_BONES) {
    const node = h.getNormalizedBoneNode(name);
    if (!node) continue;
    const pose = REST_POSE[name];
    if (pose) node.rotation.set(pose[0], pose[1], pose[2]);
    bones[name] = node;
    basePose[name] = { x: node.rotation.x, y: node.rotation.y, z: node.rotation.z };
  }
}

function poseBone(name, dx, dy, dz) {
  const node = bones[name];
  const base = basePose[name];
  if (!node || !base) return;
  node.rotation.set(base.x + dx, base.y + (dy || 0), base.z + (dz || 0));
}

const LIPS = {
  region: { u0: 0.40, v0: 0.72, u1: 0.62, v1: 0.80 },
  minSaturation: 0.18,
  saturation: 0.42,
  lighten: 1.06,
  hueShift: -0.012,
};

const INNER_MOUTH = { saturation: 0.62, lighten: 0.98 };

function rgbToHsv(r, g, b) {
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  let h = 0;
  if (d !== 0) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h /= 6;
    if (h < 0) h += 1;
  }
  return [h, max === 0 ? 0 : d / max, max];
}

function hsvToRgb(h, s, v) {
  const i = Math.floor(h * 6), f = h * 6 - i;
  const p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
  switch (i % 6) {
    case 0: return [v, t, p];
    case 1: return [q, v, p];
    case 2: return [p, v, t];
    case 3: return [p, q, v];
    case 4: return [t, p, v];
    default: return [v, p, q];
  }
}

function recolourTexture(tex, box, opts) {
  const src = tex?.image;
  if (!src || !src.width) return false;

  const canvas = document.createElement('canvas');
  canvas.width = src.width;
  canvas.height = src.height;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(src, 0, 0);

  const x0 = box ? Math.floor(box.u0 * canvas.width) : 0;
  const x1 = box ? Math.ceil(box.u1 * canvas.width) : canvas.width;
  const y0 = box ? Math.floor(box.v0 * canvas.height) : 0;
  const y1 = box ? Math.ceil(box.v1 * canvas.height) : canvas.height;

  const img = ctx.getImageData(x0, y0, x1 - x0, y1 - y0);
  const d = img.data;
  let touched = 0;

  for (let i = 0; i < d.length; i += 4) {
    if (d[i + 3] < 8) continue;
    const [h, sat, val] = rgbToHsv(d[i] / 255, d[i + 1] / 255, d[i + 2] / 255);
    if (sat < (opts.minSaturation ?? 0)) continue;

    let nh = h + (opts.hueShift || 0);
    if (nh < 0) nh += 1; else if (nh > 1) nh -= 1;
    const ns = Math.max(0, Math.min(1, sat * opts.saturation));
    const nv = Math.max(0, Math.min(1, val * (opts.lighten ?? 1)));

    const [r, g, b] = hsvToRgb(nh, ns, nv);
    d[i] = Math.round(r * 255);
    d[i + 1] = Math.round(g * 255);
    d[i + 2] = Math.round(b * 255);
    touched++;
  }

  ctx.putImageData(img, x0, y0);

  tex.image = canvas;
  tex.needsUpdate = true;
  return touched;
}

function restyleFace(v) {
  let lips = 0;
  let mouth = 0;

  v.scene.traverse((obj) => {
    const mats = Array.isArray(obj.material) ? obj.material : obj.material ? [obj.material] : [];
    for (const m of mats) {
      const name = m.name || '';
      if (/Face_00_SKIN/i.test(name)) {
        lips += recolourTexture(m.map, LIPS.region, LIPS) || 0;
      } else if (/FaceMouth/i.test(name)) {
        mouth += recolourTexture(m.map, null, { ...INNER_MOUTH, minSaturation: 0.15 }) || 0;
      }
    }
  });

  if (!lips) console.warn('Lip recolour: no matching pixels, check LIPS.region.');
  return { lips, mouth };
}

const MOUTH_CLOSE_MORPH = 'Fcl_MTH_Close';

const BROW_MORPHS = ['Fcl_BRW_Fun', 'Fcl_BRW_Surprised', 'Fcl_BRW_Sorrow'];
let browTargets = {};

function collectBrows(v) {
  browTargets = {};
  for (const name of BROW_MORPHS) browTargets[name] = [];
  v.scene.traverse((o) => {
    const dict = o.morphTargetDictionary;
    if (!o.isSkinnedMesh || !dict) return;
    for (const name of BROW_MORPHS) {
      if (name in dict) browTargets[name].push({ mesh: o, index: dict[name] });
    }
  });
}

function applyIdleBrow(t) {
  const emoting = Math.max(
    cueOut.expr.happy || 0, cueOut.expr.sad || 0, cueOut.expr.angry || 0,
    cueOut.expr.surprised || 0, cueOut.expr.relaxed || 0,
  );
  const room = Math.max(0, 1 - emoting * 2);

  const values = {
    Fcl_BRW_Fun: room * (0.06 + 0.05 * noise1(t * 0.19 + 7) + browFlash * 0.22),
    Fcl_BRW_Surprised: room * Math.max(0, mouthOpen * 0.14 + browLift * 0.42
      + 0.03 * noise1(t * 0.23 + 19)),
    Fcl_BRW_Sorrow: room * Math.max(0, 0.05 * noise1(t * 0.14 + 55)),
  };

  for (const name of BROW_MORPHS) {
    const list = browTargets[name];
    if (!list) continue;
    const v = Math.max(0, Math.min(1, values[name] || 0));
    for (let i = 0; i < list.length; i++) {
      const tgt = list[i];
      tgt.mesh.morphTargetInfluences[tgt.index] = v;
    }
  }
}

function collectMouthClose(v) {
  mouthCloseTargets = [];
  v.scene.traverse((o) => {
    const dict = o.morphTargetDictionary;
    if (o.isSkinnedMesh && dict && MOUTH_CLOSE_MORPH in dict) {
      mouthCloseTargets.push({ mesh: o, index: dict[MOUTH_CLOSE_MORPH] });
    }
  });
}

function applyRestingMouth() {
  if (!mouthCloseTargets.length) return;

  const emoting = Math.max(
    cueOut.expr.happy || 0, cueOut.expr.sad || 0,
    cueOut.expr.angry || 0, cueOut.expr.surprised || 0,
  );
  const close = Math.max(0, 1 - mouthOpen * 2.2 - emoting * 1.2);

  for (let i = 0; i < mouthCloseTargets.length; i++) {
    const t = mouthCloseTargets[i];
    t.mesh.morphTargetInfluences[t.index] = close;
  }
}

function collectSprings(v) {
  springs = [];
  const mgr = v.springBoneManager;
  if (!mgr || !mgr.joints) return;
  for (const joint of mgr.joints) {
    springs.push({
      joint,
      dir: joint.settings.gravityDir.clone(),
      power: joint.settings.gravityPower,
    });
  }
}

async function loadFromDisk() {
  const res = await window.marina.loadVRM();
  if (res.error) {
    setStatus('bad', 'no model');
    showNotice(`${res.error}\n\nIn VRoid Studio: Export → VRM, then use the model button in the top-right to pick the file.`, 'model');
    return;
  }
  try {
    await mountVRM(res.buffer, res.name);
  } catch (e) {
    setStatus('bad', 'model failed');
    showNotice(`Could not load ${res.name}: ${e.message}`, 'model');
  }
}

const pointer = { x: 0, y: 0 };
window.addEventListener('mousemove', (e) => {
  pointer.x = (e.clientX / window.innerWidth) * 2 - 1;
  pointer.y = (e.clientY / window.innerHeight) * 2 - 1;
});

let blinkTimer = 1 + Math.random() * 3;
let blinkPending = 0;
let blinkT = 999;
let blinkDur = 0.14;

let moodTimer = 0;
let mood = 0;
let moodTarget = 0;

const AVERSIONS = [
  { x: -0.38, y:  0.27, hold: [1.1, 2.4] },
  { x:  0.36, y:  0.25, hold: [1.1, 2.4] },
  { x: -0.27, y: -0.23, hold: [1.4, 3.0] },
  { x:  0.25, y: -0.21, hold: [1.4, 3.0] },
  { x: -0.48, y:  0.04, hold: [1.6, 3.4] },
  { x:  0.46, y: -0.02, hold: [1.6, 3.4] },
];

const speaking = () => mouthOpen > 0.02 || playing > 0;

let gazeTimer = 0;
let gazeAway = false;
const gaze = { x: 0, y: 0 };
const gazeTarget = { x: 0, y: 0 };

const gazeHead = { x: 0, y: 0 };
const gazeHeadV = { x: 0, y: 0 };

const headS = { x: 0, y: 0, z: 0 };
const headV = { x: 0, y: 0, z: 0 };

const torsoS = { x: 0, y: 0 };
const torsoV = { x: 0, y: 0 };

let envFast = 0, envSlow = 0;
let browFlash = 0;

const _hash = (i) => {
  const s = Math.sin(i * 127.1) * 43758.5453;
  return (s - Math.floor(s)) * 2 - 1;
};
function noise1(x) {
  const i = Math.floor(x), f = x - i;

  const u = f * f * f * (f * (f * 6 - 15) + 10);
  return _hash(i) * (1 - u) + _hash(i + 1) * u;
}

function fbm(x) {
  return noise1(x) * 0.72 + noise1(x * 2.7 + 13.7) * 0.28;
}

let audioCtx = null;
let analyser = null;
let freqData = null;
let timeData = null;
let mouthOpen = 0;

function ensureAudio() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0.35;
    freqData = new Uint8Array(analyser.frequencyBinCount);
    timeData = new Uint8Array(analyser.fftSize);
    analyser.connect(audioCtx.destination);
  }
  if (audioCtx.state === 'suspended') audioCtx.resume();
  return audioCtx;
}

// Mobile browsers only start audio from inside a tap.
if (WEB) document.addEventListener('pointerdown', () => ensureAudio(), true);

function decodeBase64Wav(base64) {
  const bin = atob(base64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return ensureAudio().decodeAudioData(bytes.buffer);
}

const utterance = {
  epoch: -1,
  nextStart: 0,
  chunks: [],
  ended: false,
};

let playing = 0;

function chunksSpoken() {
  if (utterance.epoch < 0) return 0;
  const now = audioCtx.currentTime;
  let n = 0;
  for (const c of utterance.chunks) if (c.end <= now) n = Math.max(n, c.index + 1);
  return n;
}

async function enqueueChunk(base64, cues) {
  const ctx = ensureAudio();
  const buffer = await decodeBase64Wav(base64);

  const LEAD = 0.06;
  if (utterance.epoch < 0) {
    utterance.epoch = ctx.currentTime + LEAD;
    utterance.nextStart = utterance.epoch;
    cueEpoch = utterance.epoch;
    pendingCues = [];
  } else if (utterance.nextStart < ctx.currentTime) {
    utterance.nextStart = ctx.currentTime;
  }

  const start = utterance.nextStart;
  const index = utterance.chunks.length;

  const src = ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(analyser);
  playing++;
  src.onended = () => { playing = Math.max(0, playing - 1); };
  src.start(start);

  utterance.chunks.push({ index, start, end: start + buffer.duration, src });
  utterance.nextStart = start + buffer.duration;

  appendCues(cues, start - utterance.epoch, buffer.duration);
}

function stopSpeaking() {
  if (utterance.epoch < 0) return 0;
  const spoken = chunksSpoken();
  for (const c of utterance.chunks) {
    try { c.src.stop(); } catch {}
  }
  utterance.chunks = [];
  utterance.epoch = -1;
  utterance.ended = true;
  playing = 0;
  cueEpoch = -1;
  pendingCues = [];
  return spoken;
}

function untilSpoken() {
  if (utterance.epoch < 0 || !utterance.chunks.length) return Promise.resolve();
  const last = utterance.chunks[utterance.chunks.length - 1];
  const remaining = Math.max(0, last.end - audioCtx.currentTime);
  return new Promise((r) => setTimeout(r, remaining * 1000 + 40));
}

function endUtterance() {
  utterance.epoch = -1;
  utterance.chunks = [];
  utterance.ended = true;
  cueEpoch = -1;
}

async function speak(base64, onStart) {
  stopSpeaking();
  utterance.ended = false;
  const ctx = ensureAudio();
  const buffer = await decodeBase64Wav(base64);
  utterance.epoch = ctx.currentTime + 0.06;
  utterance.nextStart = utterance.epoch;
  cueEpoch = utterance.epoch;
  pendingCues = [];

  const src = ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(analyser);
  playing++;
  utterance.chunks.push({ index: 0, start: utterance.epoch,
                          end: utterance.epoch + buffer.duration, src });
  utterance.nextStart = utterance.epoch + buffer.duration;

  onStart?.(buffer.duration);

  return new Promise((resolve) => {
    src.onended = () => {
      playing = Math.max(0, playing - 1);
      endUtterance();
      resolve();
    };
    src.start(utterance.epoch);
  });
}

const DB_FLOOR = -46;
const DB_CEIL = -14;

const BANDS = { f1: [250, 900], f2: [900, 2500], sib: [4000, 9000] };
let bandBins = null;

function resolveBands() {
  const nyquist = audioCtx.sampleRate / 2;
  const n = analyser.frequencyBinCount;
  const toBin = (hz) => Math.max(0, Math.min(n - 1, Math.round((hz / nyquist) * n)));
  bandBins = {
    f1: BANDS.f1.map(toBin),
    f2: BANDS.f2.map(toBin),
    sib: BANDS.sib.map(toBin),
  };
}

function bandEnergy(range) {
  let sum = 0;
  for (let i = range[0]; i < range[1]; i++) sum += freqData[i];
  return sum / Math.max(1, (range[1] - range[0]) * 255);
}

const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);

const VISEMES = ['aa', 'ih', 'ou', 'ee', 'oh'];
const viseme = { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };
const visemeTarget = { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };

function updateMouth(dt) {
  let open = 0;

  for (const v of VISEMES) visemeTarget[v] = 0;

  if (playing > 0 && analyser) {
    if (!bandBins) resolveBands();

    analyser.getByteTimeDomainData(timeData);
    let sum = 0;
    for (let i = 0; i < timeData.length; i++) {
      const a = (timeData[i] - 128) / 128;
      sum += a * a;
    }
    const rms = Math.sqrt(sum / timeData.length);
    const db = 20 * Math.log10(rms + 1e-6);

    open = clamp01((db - DB_FLOOR) / (DB_CEIL - DB_FLOOR));

    open = Math.pow(open, 1.35) * 0.92;
    if (open < 0.05) open = 0;

    analyser.getByteFrequencyData(freqData);
    const e1 = bandEnergy(bandBins.f1);
    const e2 = bandEnergy(bandBins.f2);
    const e3 = bandEnergy(bandBins.sib);

    const voiced = e1 + e2;
    const frontness = voiced > 0.001 ? clamp01(e2 / voiced) : 0.4;
    const sibilance = (voiced + e3) > 0.001 ? clamp01(e3 / (voiced + e3)) : 0;

    if (sibilance > 0.45) open *= 1 - (sibilance - 0.45) * 1.2;

    const w = {
      ee: clamp01((frontness - 0.58) * 3.4),
      ih: clamp01(1 - Math.abs(frontness - 0.52) * 4.2),
      aa: clamp01(1 - Math.abs(frontness - 0.34) * 3.6),
      oh: clamp01((0.36 - frontness) * 3.6),
      ou: clamp01((0.28 - frontness) * 4.0),
    };

    w.aa *= 0.5 + open * 0.5;
    w.ou *= 1.15 - open * 0.4;
    w.ee += sibilance * 0.5;

    let total = 0;
    for (const v of VISEMES) total += w[v];
    if (total > 0.001) {
      for (const v of VISEMES) visemeTarget[v] = (w[v] / total) * open;
    } else {
      visemeTarget.aa = open;
    }
  }

  for (const v of VISEMES) {
    const t = visemeTarget[v];
    const rate = t > viseme[v] ? 24 : 12;
    viseme[v] += (t - viseme[v]) * Math.min(1, rate * dt);
  }

  mouthOpen = clamp01(viseme.aa + viseme.oh + viseme.ee * 0.6 + viseme.ih * 0.6 + viseme.ou * 0.5);

  const em = vrm?.expressionManager;
  if (!em) return;
  for (const v of VISEMES) em.setValue(v, viseme[v]);
}

const TAU = Math.PI * 2;

function updateBody(t, dtBody) {
  envFast += (mouthOpen - envFast) * Math.min(1, dtBody * 14);
  envSlow += (mouthOpen - envSlow) * Math.min(1, dtBody * 2.2);
  const stress = Math.max(0, envFast - envSlow);

  const bphase = t * 0.21 + 0.07 * noise1(t * 0.05);
  const bw = bphase - Math.floor(bphase);
  const breath = (bw < 0.4
    ? Math.sin((bw / 0.4) * Math.PI * 0.5)
    : Math.cos(((bw - 0.4) / 0.6) * Math.PI * 0.5)) * 2 - 1;

  const energy = 0.68 + 0.42 * noise1(t * 0.035 + 3);

  const shift = fbm(t * 0.031 + 61) * energy;

  const TK = 2.6, TC = 2.9;
  torsoV.y += (TK * (gazeHead.x - torsoS.y) - TC * torsoV.y) * dtBody;
  torsoV.x += (TK * (-gazeHead.y - torsoS.x) - TC * torsoV.x) * dtBody;
  torsoS.y += torsoV.y * dtBody;
  torsoS.x += torsoV.x * dtBody;

  const twist = torsoS.y * 0.30;
  const lean  = torsoS.x * 0.10;

  poseBone('hips',
    lean * 0.4,
    twist * 0.30 + shift * 0.020,
    shift * -0.016);
  poseBone('spine',
    -0.004 * breath + lean * 0.5,
    twist * 0.34 + shift * 0.014,
    shift * 0.010);
  poseBone('chest',
    -0.013 * breath + lean * 0.7,
    twist * 0.22,
    shift * 0.008);
  poseBone('upperChest',
    -0.008 * breath,
    twist * 0.14,
    shift * 0.005);

  const lift = cueOut.shoulder;
  poseBone('leftShoulder',
    -0.010 * breath - lift - stress * 0.06, 0, 0.006 * breath + lift * 0.5);
  poseBone('rightShoulder',
    -0.010 * breath - lift - stress * 0.06, 0, -0.006 * breath - lift * 0.5);

  const armL = fbm(t * 0.077 + 11) * energy;
  const armR = fbm(t * 0.071 + 29) * energy;
  const swing = twist * 0.55;

  poseBone('leftUpperArm',
    0.012 * armL - 0.010 * breath + cueOut.lux,
    swing * 0.5 + cueOut.luy,
    -0.030 * armL - swing * 0.35 - lift * 0.35 + cueOut.luz);
  poseBone('rightUpperArm',
    0.012 * armR - 0.010 * breath + cueOut.rux,
    swing * 0.5 + cueOut.ruy,
    0.028 * armR - swing * 0.35 + lift * 0.35 + cueOut.ruz);
  poseBone('leftLowerArm', cueOut.llx, 0.030 * armL + swing * 0.25 + cueOut.lly, -0.014 * armL + cueOut.llz);
  poseBone('rightLowerArm', cueOut.rlx, -0.028 * armR + swing * 0.25 + cueOut.rly, 0.013 * armR + cueOut.rlz);
  poseBone('leftHand', 0.018 * armR + cueOut.lhx, cueOut.lhy, -0.014 * armL + cueOut.lhz);
  poseBone('rightHand', 0.017 * armL + cueOut.rhx, cueOut.rhy, 0.013 * armR + cueOut.rhz);

  const nx = fbm(t * 0.13);
  const ny = fbm(t * 0.11 + 40);
  const nz = fbm(t * 0.09 + 80);

  const followX = -gazeHead.y * 0.19;
  const followY = gazeHead.x * 0.40;

  const idleX = pointer.y * 0.09 + 0.016 * nx * energy;
  const idleY = pointer.x * 0.17 + 0.034 * ny * energy;
  const idleZ = 0.018 * nz * energy;

  const HK = 5.0, HC = 4.2;
  headV.x += (HK * (idleX - headS.x) - HC * headV.x) * dtBody;
  headV.y += (HK * (idleY - headS.y) - HC * headV.y) * dtBody;
  headV.z += (HK * (idleZ - headS.z) - HC * headV.z) * dtBody;
  headS.x += headV.x * dtBody;
  headS.y += headV.y * dtBody;
  headS.z += headV.z * dtBody;

  const x = headS.x + followX + stress * 0.60 + envSlow * 0.012 + cueOut.hx;
  const y = headS.y + followY + envSlow * 0.06 * fbm(t * 0.9 + 5) + cueOut.hy;
  const z = headS.z - followY * 0.13 + cueOut.hz;

  poseBone('neck', x * 0.40, y * 0.40, z * 0.5);
  poseBone('head', x * 0.60, y * 0.60, z * 0.5);
}

// Arm channels a gesture or resting pose can drive: r/l, then upper arm, lower arm
// and hand, then the axis. 'rlz' is the right lower arm's z rotation.
const ARM_CHANNELS = ['rux', 'ruy', 'ruz', 'rlx', 'rly', 'rlz', 'rhx', 'rhy', 'rhz',
  'lux', 'luy', 'luz', 'llx', 'lly', 'llz', 'lhx', 'lhy', 'lhz'];

const ease = (p) => Math.sin(p * Math.PI);
const settle = (p) => Math.sin(p * Math.PI) * (1 - p);

const CUES = {
  nod:       { dur: 1.0, run: (p, o) => { o.hx += Math.sin(p * TAU * 1.5) * 0.20 * (1 - p); } },
  shake:     { dur: 1.1, run: (p, o) => { o.hy += Math.sin(p * TAU * 2) * 0.20 * (1 - p); } },
  tilt:      { dur: 1.6, run: (p, o) => { o.hz += ease(p) * 0.30; o.hy += ease(p) * 0.06; } },
  shrug:     { dur: 1.3, run: (p, o) => { o.shoulder += ease(p) * 0.14; o.hx += ease(p) * 0.05; } },
  lean:      { dur: 1.5, run: (p, o) => { o.hx += ease(p) * 0.10; o.expr.happy = ease(p) * 0.15; } },
  laugh:     { dur: 1.8, run: (p, o) => {
                 o.expr.happy = ease(p) * 0.9;
                 o.hx += Math.sin(p * TAU * 4) * 0.07 * (1 - p);
                 o.hz += Math.sin(p * TAU * 2) * 0.04;
               } },
  smile:     { dur: 2.0, run: (p, o) => { o.expr.happy = ease(p) * 0.75; } },
  wink:      { dur: 0.7, run: (p, o) => {
                 o.blinkLeft = p < 0.55 ? Math.min(1, p * 4) : Math.max(0, 1 - (p - 0.55) * 5);
                 o.expr.happy = ease(p) * 0.4;
               } },
  eyeroll:   { dur: 1.4, run: (p, o) => {
                 o.gazeY += ease(p) * 0.85;
                 o.hz += ease(p) * 0.08;
                 o.expr.relaxed = ease(p) * 0.3;
               } },
  sigh:      { dur: 2.0, run: (p, o) => {
                 o.hx += ease(p) * 0.16;
                 o.expr.sad = ease(p) * 0.45;
                 o.shoulder -= ease(p) * 0.06;
               } },
  pout:      { dur: 1.8, run: (p, o) => { o.expr.angry = ease(p) * 0.55; o.hy += ease(p) * 0.07; } },
  sad:       { dur: 2.0, run: (p, o) => { o.expr.sad = ease(p) * 0.7; o.hx += ease(p) * 0.12; } },
  surprised: { dur: 1.2, run: (p, o) => {
                 o.expr.surprised = ease(p) * 0.85;
                 o.hx -= settle(p) * 0.18;
               } },
  blush:     { dur: 2.2, run: (p, o) => {
                 o.expr.happy = ease(p) * 0.4;
                 o.hy += ease(p) * 0.16;
                 o.hx += ease(p) * 0.09;
               } },
  think:     { dur: 2.0, run: (p, o) => {
                 o.gazeY += ease(p) * 0.5;
                 o.gazeX += ease(p) * 0.4;
                 o.hz += ease(p) * 0.14;
               } },
  brow:      { dur: 1.4, run: (p, o) => {
                 o.expr.surprised = ease(p) * 0.35;
                 o.hz += ease(p) * 0.10;
                 o.hx -= ease(p) * 0.05;
               } },
  stare:     { dur: 1.6, run: (p, o) => {
                 o.expr.relaxed = ease(p) * 0.25;
                 o.hx += ease(p) * 0.04;
               } },
  yawn:      { dur: 2.2, run: (p, o) => {
                 o.hx += ease(p) * 0.18;
                 o.shoulder += ease(p) * 0.10;
               } },
  emote:     { dur: 1.0, run: (p, o) => { o.hx += Math.sin(p * TAU) * 0.06; } },
  // The arm itself is placed by applyWaveIK(); this only reports progress
  // and adds the face.
  wave:      { dur: 2.8, run: (p, o) => {
                 waveIK.p = p;
                 const up = raiseLower(p, 0.22, 0.25);
                 o.lArm = Math.max(o.lArm, up);
                 o.hz -= 0.04 * up;
                 o.hy += 0.03 * up;
                 o.expr.happy = Math.max(o.expr.happy, up * 0.3);
               } },
};

const smooth01 = (x) => x * x * (3 - 2 * x);


// 0 to 1 and back: rises over the first `rise` of the gesture, falls over the last `fall`.
function raiseLower(p, rise, fall) {
  return smooth01(Math.min(1, p / rise)) * smooth01(Math.min(1, (1 - p) / fall));
}


// Where the arms rest, as offsets from REST_POSE: elbows slightly bent and a
// little away from her sides, so they don't hang dead straight.
const ARM_REST = { rux: -0.15, ruz: -0.08, rly: 0.5, lux: -0.15, luz: 0.08, lly: -0.5 };

const CUE_EXPRESSIONS = ['happy', 'sad', 'angry', 'relaxed', 'surprised'];

let pendingCues = [];
let activeCues = [];
let cueClock = -1;

let cueEpoch = -1;

// rArm and lArm say how much a gesture owns that arm, 0 to 1, so the resting
// pose underneath steps aside instead of adding to it.
const CUE_CHANNELS = ['hx', 'hy', 'hz', 'shoulder', 'gazeX', 'gazeY', 'rArm', 'lArm',
  ...ARM_CHANNELS];

function clearCue(o) {
  for (const k of CUE_CHANNELS) o[k] = 0;
  o.blinkLeft = 0;
  for (const name of CUE_EXPRESSIONS) o.expr[name] = 0;
}

const cueOut = { expr: {} };
const cueScratch = { expr: {} };
clearCue(cueOut);
clearCue(cueScratch);

function scheduleCues(cues, duration) {
  pendingCues = (cues || []).map((c) => ({
    animation: c.animation,
    at: Math.max(0, (c.fraction ?? 0) * duration - 0.15),
  }));
  activeCues = [];
  cueClock = pendingCues.length ? 0 : -1;
}

function appendCues(cues, offset, duration) {
  for (const c of cues || []) {
    pendingCues.push({
      animation: c.animation,
      at: Math.max(0, offset + (c.fraction ?? 0) * duration - 0.15),
    });
  }
  pendingCues.sort((a, b) => a.at - b.at);
}

const IDLE_GESTURES = ['tilt', 'lean', 'smile', 'nod', 'shrug', 'think', 'brow', 'sigh', 'eyeroll', 'stare'];
// In front of strangers, sighs, eye-rolls and blank stares read as rude.
const PRESENTING_GESTURES = ['tilt', 'lean', 'smile', 'nod', 'brow', 'think'];

// Set on <html> while the presentation personality is active.
const presenting = () => document.documentElement.classList.contains('presenting');

let gestureBag = [];
let lastGesture = null;
let gestureTimer = 6 + Math.random() * 8;

function drawGesture() {
  if (!gestureBag.length) {
    gestureBag = (presenting() ? PRESENTING_GESTURES : IDLE_GESTURES).slice();
    for (let i = gestureBag.length - 1; i > 0; i--) {
      const j = (Math.random() * (i + 1)) | 0;
      [gestureBag[i], gestureBag[j]] = [gestureBag[j], gestureBag[i]];
    }

    if (gestureBag[gestureBag.length - 1] === lastGesture && gestureBag.length > 1) {
      const swap = (Math.random() * (gestureBag.length - 1)) | 0;
      [gestureBag[gestureBag.length - 1], gestureBag[swap]] =
        [gestureBag[swap], gestureBag[gestureBag.length - 1]];
    }
  }
  lastGesture = gestureBag.pop();
  return lastGesture;
}

function updateIdleGestures(dt) {
  if (cueClock >= 0 || mouthOpen > 0.05 || busy || recording) {
    gestureTimer = Math.max(gestureTimer, 2.5);
    return;
  }

  gestureTimer -= dt;
  if (gestureTimer > 0) return;
  gestureTimer = 7 + Math.random() * 11;

  const def = CUES[drawGesture()];

  if (def) activeCues.push({ run: def.run, elapsed: 0, dur: def.dur * 1.8, gain: 0.5 });
}

function updateCues(dt) {
  clearCue(cueOut);
  waveIK.p = -1;

  if (cueEpoch >= 0 && audioCtx) {
    cueClock = audioCtx.currentTime - cueEpoch;
  } else if (cueClock >= 0) {
    cueClock += dt;
  }

  if (cueClock >= 0) {
    while (pendingCues.length && pendingCues[0].at <= cueClock) {
      const next = pendingCues.shift();
      const def = CUES[next.animation] || CUES.emote;
      activeCues.push({ run: def.run, elapsed: 0, dur: def.dur });
    }

    if (cueEpoch < 0 && !pendingCues.length && !activeCues.length) cueClock = -1;
  }

  for (let i = activeCues.length - 1; i >= 0; i--) {
    const c = activeCues[i];
    c.elapsed += dt;
    const p = c.elapsed / c.dur;
    if (p >= 1) { activeCues.splice(i, 1); continue; }

    if (c.gain === undefined || c.gain === 1) { c.run(p, cueOut); continue; }

    clearCue(cueScratch);
    c.run(p, cueScratch);
    for (const k of CUE_CHANNELS) cueOut[k] += cueScratch[k] * c.gain;
    cueOut.blinkLeft = Math.max(cueOut.blinkLeft, cueScratch.blinkLeft * c.gain);
    for (const name of CUE_EXPRESSIONS) {
      cueOut.expr[name] = Math.max(cueOut.expr[name], cueScratch.expr[name] * c.gain);
    }
  }
}

const WIND_STRENGTH = 0.15;

const _wind = new THREE.Vector3();
const _force = new THREE.Vector3();

function updateHair(t) {
  if (!springs.length) return;

  const gust = 0.55 + 0.45 * Math.sin(t * TAU * 0.037);
  const wx = (Math.sin(t * TAU * 0.13) * 0.6
            + Math.sin(t * TAU * 0.29 + 1.3) * 0.28
            + Math.sin(t * TAU * 0.61 + 2.4) * 0.10) * gust;
  const wz = (Math.sin(t * TAU * 0.11 + 2.1) * 0.5
            + Math.sin(t * TAU * 0.23 + 0.7) * 0.24
            + Math.sin(t * TAU * 0.53 + 1.1) * 0.09) * gust;

  _wind.set(wx * WIND_STRENGTH, 0, wz * WIND_STRENGTH);

  for (let i = 0; i < springs.length; i++) {
    const s = springs[i];
    _force.copy(s.dir).multiplyScalar(s.power).add(_wind);
    const len = _force.length();
    if (len < 1e-6) continue;
    s.joint.settings.gravityDir.copy(_force).divideScalar(len);
    s.joint.settings.gravityPower = len;
  }
}

function updateGaze(dt, t) {
  gazeTimer -= dt;
  if (recording && gazeAway) gazeTimer = 0;
  if (gazeTimer <= 0) {
    const from = { x: gazeTarget.x, y: gazeTarget.y };

    if (gazeAway) {
      gazeAway = false;
      gazeTarget.x = (Math.random() - 0.5) * 0.10;
      gazeTarget.y = (Math.random() - 0.5) * 0.07;

      gazeTimer = (speaking() ? 2.6 : 1.6) + Math.random() * 3.4;
      browFlash = 1;
    } else if (!recording && Math.random() < (speaking() ? 0.16 : 0.45)) {
      gazeAway = true;
      const a = AVERSIONS[(Math.random() * AVERSIONS.length) | 0];

      const near = speaking() ? 0.55 : 1;
      gazeTarget.x = (a.x + (Math.random() - 0.5) * 0.10) * near;
      gazeTarget.y = (a.y + (Math.random() - 0.5) * 0.08) * near;
      gazeTimer = (a.hold[0] + Math.random() * (a.hold[1] - a.hold[0]))
                * (speaking() ? 0.45 : 1);
    } else {
      gazeTarget.x = (Math.random() - 0.5) * 0.14;
      gazeTarget.y = (Math.random() - 0.5) * 0.10;
      gazeTimer = 1.3 + Math.random() * 2.2;
    }

    const jump = Math.hypot(gazeTarget.x - from.x, gazeTarget.y - from.y);
    if (jump > 0.28 && blinkTimer > 0.9 && Math.random() < 0.4) blinkTimer = 0.02;
  }

  const dist = Math.hypot(gazeTarget.x - gaze.x, gazeTarget.y - gaze.y);
  const k = Math.min(1, dt * (13 - Math.min(7, dist * 9)));
  gaze.x += (gazeTarget.x - gaze.x) * k;
  gaze.y += (gazeTarget.y - gaze.y) * k;

  const driftX = noise1(t * 1.7) * 0.012;
  const driftY = noise1(t * 1.4 + 31) * 0.009;

  const K = 6, C = 4.4;
  gazeHeadV.x += (K * (gazeTarget.x - gazeHead.x) - C * gazeHeadV.x) * dt;
  gazeHeadV.y += (K * (gazeTarget.y - gazeHead.y) - C * gazeHeadV.y) * dt;
  gazeHead.x += gazeHeadV.x * dt;
  gazeHead.y += gazeHeadV.y * dt;

  browFlash = Math.max(0, browFlash - dt * 2.6);

  lookTarget.position.set(
    pointer.x * 0.45 + gaze.x + driftX + cueOut.gazeX,
    -pointer.y * 0.30 + gaze.y + driftY + cueOut.gazeY,
    -1,
  );
}

function updateBlink(dt) {
  blinkTimer -= dt;
  if (blinkTimer <= 0) {
    blinkT = 0;
    blinkDur = 0.11 + Math.random() * 0.07;
    if (blinkPending > 0) {
      blinkPending -= 1;
      blinkTimer = 2.4 + Math.random() * 4.6;
    } else if (Math.random() < 0.25) {
      blinkPending = 1;
      blinkTimer = 0.24;
    } else {
      blinkTimer = 2.4 + Math.random() * 4.6;
    }
  }

  blinkT += dt;
  const bp = blinkT / blinkDur;
  const blink = bp >= 1 ? 0
    : bp < 0.32
      ? Math.pow(bp / 0.32, 0.62)
      : Math.pow(1 - (bp - 0.32) / 0.68, 1.7);
  const em = vrm.expressionManager;
  if (!em) return;

  if (cueOut.blinkLeft > 0.01) {
    em.setValue('blink', 0);
    em.setValue('blinkLeft', Math.max(blink, cueOut.blinkLeft));
    em.setValue('blinkRight', blink);
  } else {
    em.setValue('blinkLeft', 0);
    em.setValue('blinkRight', 0);
    em.setValue('blink', blink);
  }
}

function updateMood(dt) {
  const em = vrm.expressionManager;
  if (!em) return;

  moodTimer -= dt;
  if (moodTimer <= 0) {
    moodTimer = 4 + Math.random() * 9;

    moodTarget = presenting()
      ? 0.10 + Math.random() * 0.12
      : Math.random() < 0.5 ? 0 : 0.05 + Math.random() * 0.10;
  }
  mood += (moodTarget - mood) * Math.min(1, dt * 1.3);

  const idleHappy = mood * (1 - Math.min(1, mouthOpen * 1.4));
  em.setValue('happy', Math.max(idleHappy, cueOut.expr.happy));
  em.setValue('sad', cueOut.expr.sad);
  em.setValue('angry', cueOut.expr.angry);
  em.setValue('relaxed', cueOut.expr.relaxed);
  em.setValue('surprised', cueOut.expr.surprised);
}

// What she is doing shapes her whole posture: leaning in to listen, glancing
// away to think, lifting her brows on stressed words, smiling when she's done.
const activity = {
  listen: 0, think: 0, thinkSide: 1,
  nodIn: 2, beatCool: 0, wasBusy: false,
  quietFor: 0, attractIn: 40,
};
let browLift = 0;
let debugHold = null;

function pushCue(name, gain = 1) {
  const def = CUES[name];
  if (def) activeCues.push({ run: def.run, elapsed: 0, dur: def.dur, gain });
}

function updateActivity(dt) {
  const talking = speaking();
  const thinking = busy && !recording && !talking;

  activity.listen += ((recording ? 1 : 0) - activity.listen) * Math.min(1, dt * 3);
  if (thinking && activity.think < 0.05) activity.thinkSide = Math.random() < 0.5 ? -1 : 1;
  activity.think += ((thinking ? 1 : 0) - activity.think) * Math.min(1, dt * 2.5);

  const L = activity.listen;
  cueOut.hx += 0.07 * L;
  cueOut.hz += 0.05 * L;
  cueOut.expr.happy = Math.max(cueOut.expr.happy, 0.12 * L);
  if (recording) {
    activity.nodIn -= dt;
    if (activity.nodIn <= 0) {
      activity.nodIn = 2.2 + Math.random() * 2.6;
      pushCue('nod', 0.4);
    }
  } else {
    activity.nodIn = 1.6;
  }

  const T = activity.think;
  cueOut.gazeX += 0.32 * T * activity.thinkSide;
  cueOut.gazeY += 0.30 * T;
  cueOut.hz += 0.07 * T * activity.thinkSide;
  cueOut.hx -= 0.03 * T;

  // A small nod on a stressed syllable, at most every 0.9 s.
  const stress = Math.max(0, envFast - envSlow);
  activity.beatCool -= dt;
  if (talking && stress > 0.16 && activity.beatCool <= 0) {
    activity.beatCool = 0.9 + Math.random() * 0.8;
    pushCue('nod', 0.22);
  }
  const lift = talking ? clamp01(stress * 3.2) * 0.8 : 0.3 * L;
  browLift += (lift - browLift) * Math.min(1, dt * (lift > browLift ? 14 : 4));

  if (activity.wasBusy && !busy && !recording) pushCue('smile', 0.4);
  activity.wasBusy = busy || recording;

  // Arms stay at rest; a gesture that owns an arm (the wave) takes it over.
  const freeR = 1 - clamp01(cueOut.rArm);
  const freeL = 1 - clamp01(cueOut.lArm);
  for (const k in ARM_REST) cueOut[k] += ARM_REST[k] * (k[0] === 'r' ? freeR : freeL);

  if (debugHold) for (const k in debugHold) cueOut[k] += debugHold[k];

  // At an event, catch the eye of people walking past with a smile now and
  // then. She only waves when she's greeting someone.
  if (busy || recording || talking) {
    activity.quietFor = 0;
  } else {
    activity.quietFor += dt;
  }
  if (presenting() && activity.quietFor > 30) {
    activity.attractIn -= dt;
    if (activity.attractIn <= 0) {
      activity.attractIn = 35 + Math.random() * 30;
      pushCue('smile', 0.7);
    }
  }
}

// Waving, by inverse kinematics. Nudging joint angles can't keep the hand on a
// clean path, so the wave says where the left hand should be relative to her
// shoulder and works out the elbow. Directions come from the model's own root,
// so they hold however she is turned.
const waveIK = { p: -1 };
const _ikA = new THREE.Vector3(), _ikB = new THREE.Vector3(), _ikC = new THREE.Vector3();
const _ikE = new THREE.Vector3(), _ikT = new THREE.Vector3(), _ikP = new THREE.Vector3();
const _ikD = new THREE.Vector3(), _ikV = new THREE.Vector3(), _ikM = new THREE.Vector3();
const _out = new THREE.Vector3(), _upv = new THREE.Vector3(), _fwd = new THREE.Vector3();
const _fing = new THREE.Vector3(), _palm = new THREE.Vector3(), _thumb = new THREE.Vector3();
const _ikQ = new THREE.Quaternion(), _ikQP = new THREE.Quaternion(), _ikQW = new THREE.Quaternion();
const _ikMat = new THREE.Matrix4();

// Turn a bone in world space, blended in by `weight`.
function setWorldQuat(node, worldQuat, weight) {
  node.parent.getWorldQuaternion(_ikQP);
  _ikQ.copy(_ikQP).invert().multiply(worldQuat);
  node.quaternion.slerp(_ikQ, weight);
  node.updateMatrixWorld(true);
}

// Swing `node` so its child lands on `target`.
function aimBone(node, child, target, weight) {
  node.getWorldPosition(_ikA);
  child.getWorldPosition(_ikB);
  _ikD.subVectors(_ikB, _ikA).normalize();
  _ikV.subVectors(target, _ikA).normalize();
  _ikQ.setFromUnitVectors(_ikD, _ikV);
  node.getWorldQuaternion(_ikQW).premultiply(_ikQ);
  setWorldQuat(node, _ikQW, weight);
}

function solveArm(upper, lower, hand, target, pole, weight) {
  upper.getWorldPosition(_ikA);
  lower.getWorldPosition(_ikB);
  hand.getWorldPosition(_ikC);
  const l1 = _ikA.distanceTo(_ikB);
  const l2 = _ikB.distanceTo(_ikC);
  _ikD.subVectors(target, _ikA);
  const d = Math.min(_ikD.length(), (l1 + l2) * 0.995);
  _ikD.normalize();
  const along = (l1 * l1 - l2 * l2 + d * d) / (2 * d);
  const off = Math.sqrt(Math.max(0, l1 * l1 - along * along));
  _ikV.subVectors(pole, _ikA);
  _ikV.addScaledVector(_ikD, -_ikV.dot(_ikD)).normalize();
  _ikE.copy(_ikA).addScaledVector(_ikD, along).addScaledVector(_ikV, off);
  _ikT.copy(_ikA).addScaledVector(_ikD, d);
  aimBone(upper, lower, _ikE, weight);
  aimBone(lower, hand, _ikT, weight);
}

const bez = (a, b, c, t, out) => out.copy(a).multiplyScalar((1 - t) * (1 - t))
  .addScaledVector(b, 2 * (1 - t) * t).addScaledVector(c, t * t);

function applyWaveIK() {
  const p = waveIK.p;
  const upper = bones.leftUpperArm, lower = bones.leftLowerArm, hand = bones.leftHand;
  if (p < 0 || !upper || !lower || !hand) return;

  // Her own axes: out is towards her left hand, forward is towards the viewer.
  vrm.scene.getWorldQuaternion(_ikQW);
  _out.set(1, 0, 0).applyQuaternion(_ikQW);
  _upv.set(0, 1, 0).applyQuaternion(_ikQW);
  _fwd.set(0, 0, 1).applyQuaternion(_ikQW);

  upper.updateWorldMatrix(true, true);
  const shoulder = upper.getWorldPosition(new THREE.Vector3());
  const rest = hand.getWorldPosition(new THREE.Vector3());

  // Hand up and out beside her head, a little in front of her; on the way up
  // and down it passes in front of her chest. Units are metres.
  // On a narrow screen (a phone) there's less room at her side, so the hand
  // stays closer in: beside her head at temple height, clear of her face.
  // (The shoulder joint sits near her neck, well inside the visible shoulder.)
  const narrow = camera.aspect < 0.75;
  const top = narrow
    ? shoulder.clone().addScaledVector(_out, 0.07)
      .addScaledVector(_upv, 0.15).addScaledVector(_fwd, 0.14)
    : shoulder.clone().addScaledVector(_out, 0.24)
      .addScaledVector(_upv, 0.13).addScaledVector(_fwd, 0.14);
  const via = shoulder.clone().addScaledVector(_out, 0.04)
    .addScaledVector(_upv, -0.12).addScaledVector(_fwd, 0.26);

  const RAISE = 0.22, LOWER = 0.78;
  let weight, roll = 0;
  if (p < RAISE) {
    const r = smooth01(p / RAISE);
    bez(rest, via, top, r, _ikM);
    weight = smooth01(Math.min(1, p / (RAISE * 0.4)));
  } else if (p > LOWER) {
    const r = smooth01((p - LOWER) / (1 - LOWER));
    bez(top, via, rest, r, _ikM);
    weight = smooth01(Math.min(1, (1 - p) / ((1 - LOWER) * 0.4)));
  } else {
    // The wave: the hand sweeps side to side and rocks with it.
    const w = (p - RAISE) / (LOWER - RAISE);
    const swing = Math.sin(w * TAU * 2.5) * Math.min(1, w * 6, (1 - w) * 6);
    _ikM.copy(top).addScaledVector(_out, (narrow ? 0.04 : 0.06) * swing);
    roll = 0.32 * swing;
    weight = 1;
  }

  // Elbow points down and out, like a natural wave.
  _ikP.copy(shoulder).addScaledVector(_out, 0.5).addScaledVector(_upv, -0.45)
    .addScaledVector(_fwd, -0.05);
  solveArm(upper, lower, hand, _ikM, _ikP, weight);

  // Palm to the viewer, fingers up, rocking slightly. At rest the left hand's
  // fingers point along +x and the palm faces -y.
  _fing.copy(_upv).multiplyScalar(Math.cos(roll)).addScaledVector(_out, Math.sin(roll + 0.15));
  _fing.normalize();
  _palm.copy(_fwd).addScaledVector(_fing, -_fwd.dot(_fing)).normalize();
  _thumb.crossVectors(_palm, _fing);
  _ikMat.makeBasis(_fing, _palm.clone().negate(), _thumb);
  _ikQW.setFromRotationMatrix(_ikMat);
  const handWeight = p < RAISE ? smooth01(p / RAISE) : p > LOWER
    ? smooth01((1 - p) / (1 - LOWER)) : 1;
  setWorldQuat(hand, _ikQW, handWeight);
}

const timer = new THREE.Timer();

function tick() {
  requestAnimationFrame(tick);
  timer.update();
  const dt = Math.min(timer.getDelta(), 0.1);
  const t = timer.getElapsed();

  if (vrm) {
    updateMouth(dt);
    updateCues(dt);
    updateActivity(dt);
    updateIdleGestures(dt);
    updateGaze(dt, t);
    updateBody(t, dt);
    applyWaveIK();
    updateBlink(dt);
    updateMood(dt);
    updateHair(t);

    vrm.update(dt);

    applyRestingMouth();
    applyIdleBrow(t);
  }

  renderer.render(scene, camera);
  updateClickThrough();
}

const gl = renderer.getContext();
const probe = new Uint8Array(4);
const UI = ['#bar', '#chrome', '#status', '#bubble', '#picker', '#notice', '#drag-strip'];

function overUI(x, y) {
  const hit = document.elementFromPoint(x, y);
  if (!hit) return false;
  for (const sel of UI) {
    const box = hit.closest(sel);
    if (!box) continue;

    if (sel === '#drag-strip') return true;
    return parseFloat(getComputedStyle(box).opacity) > 0.05;
  }
  return false;
}

function overAvatar(x, y) {
  const r = renderer.getPixelRatio();
  const px = Math.round(x * r);
  const py = Math.round((window.innerHeight - y) * r);
  if (px < 0 || py < 0 || px >= gl.drawingBufferWidth || py >= gl.drawingBufferHeight) return false;
  gl.readPixels(px, py, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, probe);
  return probe[3] > 12;
}

let solid = null;
let cursor = null;

window.addEventListener('mousemove', (e) => {
  cursor = [e.clientX, e.clientY];

  document.body.classList.add('near');
});
window.addEventListener('mouseleave', () => {
  cursor = null;
  document.body.classList.remove('near');
  setSolid(false);
});

function setSolid(next) {
  if (next === solid) return;
  solid = next;
  window.marina.clickThrough(!next);
}

function updateClickThrough() {
  if (document.documentElement.classList.contains('stage')) return;
  if (!cursor) return;
  const [x, y] = cursor;
  setSolid(overUI(x, y) || overAvatar(x, y));
}

tick();

let busy = false;
let recording = false;

function setStatus(kind, text) {
  // Guests don't need to see model names or file names.
  if (kind === 'ok' && presenting()) text = 'ready';
  dot.className = kind;
  statusText.textContent = text;
}

let noticeKind = null;

window.marina.onBridgeDown?.((msg) => {
  setStatus('bad', 'backend restarting');
  showNotice(msg || 'Backend stopped. Restarting\u2026', 'bridge');
});
window.marina.onBridgeUp?.(() => {
  hideNotice('bridge');
  setStatus('ok', 'ready');
});

function showNotice(msg, kind = 'general') {
  noticeKind = kind;
  el('notice-text').textContent = msg;
  notice.classList.remove('hidden');
}

function hideNotice(kind = null) {
  if (kind !== null && noticeKind !== kind) return;
  noticeKind = null;
  notice.classList.add('hidden');
}

el('notice-close').addEventListener('click', () => hideNotice());

// What she heard, so people watching can see she understood. Most useful in
// a noisy room.
let heardTimer = null;
function showHeard(text) {
  const h = el('heard');
  h.textContent = `\u201c${text}\u201d`;
  h.classList.remove('hidden');
  clearTimeout(heardTimer);
  heardTimer = setTimeout(() => h.classList.add('hidden'), 5000 + text.length * 50);
}

let bubbleTimer = null;
function say(text) {
  bubbleText.textContent = text;
  bubble.classList.remove('hidden');
  clearTimeout(bubbleTimer);
  bubbleTimer = setTimeout(() => bubble.classList.add('hidden'), 6000 + text.length * 45);
}

const BRIDGE_DOWN = `Marina's bridge isn't running.\nIn the project folder:  python server/marina_server.py`;

async function post(path, body) {
  const res = await fetch(BRIDGE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`bridge returned ${res.status}`);
  return res.json();
}

function setBusy(on, label) {
  busy = on;
  btnSend.disabled = on;
  const see = el('btn-see');
  if (see) see.disabled = on;
  setStatus(on ? 'busy' : 'ok', on ? label : 'ready');
}

async function handleResult(result) {
  if (result.error) showNotice(result.error, 'bridge'); else hideNotice('bridge');
  noteBackendUsed(result);

  if (result.speech) say(result.speech);

  if (result.audio) {
    setStatus('busy', 'speaking');
    utterance.ended = false;
    const req = newRequest();
    armBargeIn(req);
    await speak(result.audio, (duration) => scheduleCues(result.cues, duration));
    disarmBargeIn(req);
    if (inflight === req) inflight = null;
  } else if (result.cues && result.cues.length) {
    scheduleCues(result.cues, Math.max(1.5, (result.speech || '').length / 14));
  }
  setBusy(false);
}

async function* readEvents(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) yield JSON.parse(line);
    }
  }
  if (buf.trim()) yield JSON.parse(buf.trim());
}

let inflight = null;

async function playChunk(ev, req) {
  noteBackendUsed(ev);
  if (ev.error) showNotice(ev.error, 'bridge');

  if (ev.speech) {
    req.text = (req.text ? req.text + ' ' : '') + ev.speech;
    say(req.text);
  }
  if (ev.audio) {
    if (!req.chunks) setStatus('busy', 'speaking');
    await enqueueChunk(ev.audio, ev.cues);
  } else if (ev.cues && ev.cues.length) {
    appendCues(ev.cues, 0, Math.max(1.5, (ev.speech || '').length / 14));
    if (cueEpoch < 0 && cueClock < 0) cueClock = 0;
  }
  req.chunks++;

  if (req.chunks === 1) armBargeIn(req);
}

async function consumeReply(res, req) {
  if (!res.ok) throw new Error(`bridge returned ${res.status}`);
  for await (const ev of readEvents(res)) {
    if (req.cancelled) continue;
    if (ev.type === 'chunk') await playChunk(ev, req);
    else if (ev.type === 'start' && req.voice && ev.transcript) showHeard(ev.transcript);
    else if (ev.type === 'error') showNotice(ev.message, 'bridge');
  }
  if (!req.cancelled) {
    hideNotice('bridge');
    await untilSpoken();
    endUtterance();
  }
}

function newRequest() {
  const req = { cancelled: false, text: '', chunks: 0, barge: null };
  utterance.ended = false;
  inflight = req;
  return req;
}

async function send(text) {
  if (busy || !text.trim()) return;
  setBusy(true, 'thinking');
  say('…');
  const req = newRequest();

  try {
    await consumeReply(await fetch(BRIDGE + '/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    }), req);
  } catch {
    if (!req.cancelled) bridgeLost();
  } finally {
    disarmBargeIn(req);
    if (inflight === req) inflight = null;
    setBusy(false);
  }
}

let bargeEnabled = true;

async function armBargeIn(req) {
  if (!bargeEnabled || req.barge || req.cancelled) return;
  const controller = new AbortController();
  req.barge = controller;

  try {
    const res = await fetch(`${BRIDGE}/barge/listen?timeout=45`,
                            { signal: controller.signal });
    if (!res.ok) return;
    const next = { cancelled: false, text: '', chunks: 0, barge: null };

    for await (const ev of readEvents(res)) {
      if (ev.type === 'disabled') { bargeEnabled = false; return; }
      if (ev.type === 'speech') {
        req.tookOver = true;
        await interrupt();
        inflight = next;
        setBusy(true, 'listening…');
      } else if (ev.type === 'transcript') {
        setBusy(true, 'thinking');
        say('…');
      } else if (ev.type === 'chunk') {
        await playChunk(ev, next);
      } else if (ev.type === 'cancelled') {
        setBusy(false);
        return;
      } else if (ev.type === 'error') {
        showNotice(ev.message, 'bridge');
      }
    }

    if (next.chunks) {
      await untilSpoken();
      endUtterance();
      setBusy(false);
    }

    disarmBargeIn(next);
    if (inflight === next) inflight = null;
  } catch {} finally {
    if (req.barge === controller) req.barge = null;
  }
}

let idleMuted = false;
let idlePoll = null;

async function waitForOpener() {
  if (idleMuted) return;
  const controller = new AbortController();
  idlePoll = controller;
  const req = { cancelled: false, text: '', chunks: 0, barge: null };

  try {
    const res = await fetch(`${BRIDGE}/idle/listen?timeout=120`,
                            { signal: controller.signal });
    if (!res.ok) throw new Error('idle poll failed');

    for await (const ev of readEvents(res)) {
      if (ev.type === 'disabled') { idleMuted = true; return; }

      if (busy || recording) return;
      if (ev.type === 'chunk') {
        if (!req.chunks) { inflight = req; setBusy(true, 'speaking'); }
        await playChunk(ev, req);
      }
    }
    if (req.chunks) {
      await untilSpoken();
      endUtterance();
    }
  } catch {} finally {
    if (idlePoll === controller) idlePoll = null;

    if (req.chunks) {
      disarmBargeIn(req);
      if (inflight === req) inflight = null;
      setBusy(false);
    }
  }
}

let openerLoopRunning = false;

async function startOpenerPoll() {
  if (openerLoopRunning) return;
  openerLoopRunning = true;
  for (;;) {
    await waitForOpener();
    await new Promise((r) => setTimeout(r, idleMuted ? 30000 : 1500));
  }
}

function setIdleMuted(value) {
  idleMuted = !!value;
  post('/idle/mute', { muted: idleMuted }).catch(() => {});
  if (idleMuted && idlePoll) {
    try { idlePoll.abort(); } catch {}
  }
}

function disarmBargeIn(req) {
  if (req?.barge && !req.tookOver) {
    try { req.barge.abort(); } catch {}
    req.barge = null;
  }
}

async function interrupt() {
  if (!inflight && utterance.epoch < 0) return 0;
  const heard = stopSpeaking();
  if (inflight) inflight.cancelled = true;
  setBusy(false);
  try {
    await post('/interrupt', { chunks: heard });
  } catch {}
  return heard;
}

// The Mac app records through the bridge. A browser has to record itself
// and upload the clip.
let webRec = null;

async function webStartRecording() {
  if (navigator.audioSession) navigator.audioSession.type = 'play-and-record';
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true },
  });
  const rec = new MediaRecorder(stream);
  const parts = [];
  rec.ondataavailable = (e) => { if (e.data.size) parts.push(e.data); };
  rec.start();
  webRec = { rec, stream, parts };
}

function webStopRecording() {
  const r = webRec;
  webRec = null;
  return new Promise((resolve) => {
    r.rec.onstop = () => {
      r.stream.getTracks().forEach((t) => t.stop());
      if (navigator.audioSession) navigator.audioSession.type = 'playback';
      resolve(new Blob(r.parts, { type: r.rec.mimeType }));
    };
    r.rec.stop();
  });
}

async function webUploadClip() {
  const clip = await webStopRecording();
  const ext = clip.type.includes('mp4') ? 'm4a' : clip.type.includes('ogg') ? 'ogg' : 'webm';
  const form = new FormData();
  form.append('audio', clip, `clip.${ext}`);
  return fetch('/voice/stream', { method: 'POST', body: form });
}

async function toggleListen() {
  if (busy && !recording) return;

  if (!recording) {
    try {
      if (WEB) await webStartRecording();
      else await post('/listen/start');
      recording = true;
      btnMic.classList.add('recording');
      setStatus('busy', 'listening…');
      hideNotice('bridge');
    } catch (e) {
      if (WEB) showNotice(`Can't use the microphone: ${e.message}`);
      else bridgeLost();
    }
    return;
  }

  recording = false;
  btnMic.classList.remove('recording');
  setBusy(true, 'transcribing');
  const req = newRequest();
  req.voice = true;
  try {
    const res = WEB
      ? await webUploadClip()
      : await fetch(BRIDGE + '/listen/stop/stream', { method: 'POST' });

    await consumeReply(res, req);
  } catch (e) {
    if (!req.cancelled) showNotice(`Voice failed: ${e.message}`);
  } finally {
    disarmBargeIn(req);
    if (inflight === req) inflight = null;
    setBusy(false);
  }
}

function sendFromInput() {
  const text = input.value;
  input.value = '';
  send(text);
}

btnSend.addEventListener('click', sendFromInput);

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendFromInput();
  if (e.key === 'Escape') input.blur();
});

async function lookAtScreen() {
  if (busy) return;
  setBusy(true, 'looking…');
  hideNotice('bridge');

  const shot = await window.marina.captureScreen();
  if (shot.error) {
    setBusy(false);
    showNotice(shot.error, 'vision');
    return;
  }

  const question = input.value.trim();
  input.value = '';
  say(question || 'Let me look…');

  setBusy(true, 'thinking');
  try {
    await handleResult(await post('/see', {
      image: shot.image, mime: shot.mime, question,
    }));
  } catch {
    setBusy(false);
    bridgeLost();
  }
}

el('btn-see').addEventListener('click', lookAtScreen);
window.marina.onLookAtScreen(lookAtScreen);

btnMic.addEventListener('click', toggleListen);
window.marina.onToggleListen(toggleListen);

window.marina.onInterrupt?.(() => { interrupt(); });

window.marina.onSetPreset?.((name) => { switchPreset(name); });

window.marina.onSetStage?.((on) => {
  document.documentElement.classList.toggle('stage', on);
  if (on) setSolid(true);
  resize();
});

// For app/test scripts: play a gesture or fake a state without a mic.
if (new URLSearchParams(location.search).has('debug')) {
  window.marinaDebug = {
    cue: (name, gain) => pushCue(name, gain),
    heard: (text) => showHeard(text),
    say: (text) => say(text),
    hold: (offsets) => { debugHold = offsets; },
    turn: (radians) => { if (vrm) vrm.scene.rotation.y = radians; },
    state: (name) => {
      recording = name === 'listen';
      busy = name === 'think';
    },
  };
}

// Space starts and stops listening, like the mic button, unless you're typing.
document.addEventListener('keydown', (e) => {
  if (e.code !== 'Space' || e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'BUTTON') return;
  if (!el('picker').classList.contains('hidden')) return;
  e.preventDefault();
  toggleListen();
});

window.marina.onSetOpeners?.((on) => {
  setIdleMuted(!on);
  if (!idleMuted) startOpenerPoll();
});

let brainMode = 'auto';
let personality = { preset: 'default', presets: [] };
// Filled in from the bridge; these are only what to show before it answers.
let brainLabels = { server: 'GPU server', local: 'This Mac' };
let brainOrder = ['server', 'local'];

function brainLabel(name) { return brainLabels[name] || name || '?'; }

function learnBrains(labels) {
  if (!labels || !Object.keys(labels).length) return;
  brainLabels = labels;
  brainOrder = Object.keys(labels);
}

function paintBrain(mode, current, model) {
  brainMode = mode;
  const b = el('btn-brain');
  if (!b) return;
  const where = mode === 'auto' ? `auto → ${brainLabel(current)}` : brainLabel(mode);
  const style = personality.presets.find((p) => p.name === personality.preset)?.label;
  b.title = `${style ? `${style} mode · ` : ''}${where}${model ? ` · ${model}` : ''} (click to change)`;
  b.classList.toggle('on', mode !== 'auto' && mode !== brainOrder[0]);
}

function pickItem(label, sub, selected, onClick, dim) {
  const d = document.createElement('div');
  d.className = 'pick-item' + (selected ? ' on' : '') + (dim ? ' dim' : '');
  d.innerHTML = `<span class="tick">${selected ? '✓' : ''}</span><span>${label}</span>`;
  if (sub) d.title = sub;
  if (!dim) d.addEventListener('click', onClick);
  return d;
}

async function openPicker() {
  const panel = el('picker');
  const list = el('pick-list');
  list.textContent = 'loading…';
  panel.classList.remove('hidden');

  let data;
  try {
    data = await (await fetch(`${BRIDGE}/models`)).json();
  } catch {
    list.textContent = 'bridge unreachable';
    return;
  }

  const backends = data.backends || ['server', 'local'].map((name) => ({
    name, label: brainLabel(name), models: data[name] || [],
  }));
  learnBrains(Object.fromEntries(backends.map((b) => [b.name, b.label])));

  list.innerHTML = '';

  if (data.presets?.length > 1) {
    personality = { preset: data.preset, presets: data.presets };
    markPresenting(data.preset);
    pickGroup(list, 'Mode');
    for (const p of data.presets) {
      list.appendChild(pickItem(p.label, `Switch to the ${p.label} personality`,
        data.preset === p.name,
        async () => { closePicker(); await switchPreset(p.name); }));
    }
    pickGroup(list, 'Brain');
  }

  list.appendChild(pickItem(
    'Automatic', `Try ${backends.map((b) => b.label).join(', then ')}`,
    data.mode === 'auto',
    async () => { await post('/backend', { mode: 'auto' }); await refreshBrain(); closePicker(); },
  ));

  for (const { name, label, models } of backends) {
    pickGroup(list, label);

    if (!models.length) {
      list.appendChild(pickItem(
        name === 'local' ? 'no models found' : 'unreachable', '', false, null, true));
      continue;
    }
    for (const m of models) {
      const on = data.mode === name && data.selected[name] === m;
      list.appendChild(pickItem(m, `Run ${m} on ${label}`, on, async () => {
        await post('/model', { backend: name, model: m });
        await refreshBrain();
        say(name === 'local' ? `Running ${m} here.` : `Using ${m} on ${label}.`);
        closePicker();
      }));
    }
  }
}

function closePicker() { el('picker').classList.add('hidden'); }

function pickGroup(list, text) {
  const head = document.createElement('div');
  head.className = 'pick-group';
  head.textContent = text;
  list.appendChild(head);
}

// Personality, e.g. Personal or Presentation. The bridge applies it from the
// next reply on, and each one keeps its own conversation.
function markPresenting(preset) {
  document.documentElement.classList.toggle('presenting', preset === 'presentation');
}

async function switchPreset(name) {
  let res;
  try {
    res = await post('/preset', { name });
  } catch {
    bridgeLost();
    return;
  }
  if (!res.ok) { showNotice(res.error); return; }
  personality = { preset: res.preset, presets: res.presets };
  markPresenting(res.preset);
  if (res.idle) idleMuted = !!res.idle.muted || res.idle.enabled === false;
  if (res.barge_in !== undefined) bargeEnabled = res.barge_in;
  const label = res.presets.find((p) => p.name === res.preset)?.label || res.preset;
  say(`${label} mode.`);
  await refreshBrain();
}

// Falling back to the next brain is normal in Automatic, so it only shows in
// the status line, not as a warning.
function noteBackendUsed(data) {
  if (!data || !data.backend) return;
  setStatus('ok', `${brainLabel(data.backend)} · ${data.model || ''}`.trim());
}

async function refreshBrain() {
  try {
    const h = await (await fetch(`${BRIDGE}/health`)).json();
    learnBrains(h.llm_labels);
    if (h.preset) { personality.preset = h.preset; markPresenting(h.preset); }
    paintBrain(h.llm_mode, h.llm_using, h.model);
    setStatus('ok', `${brainLabel(h.llm_using)} · ${h.model}`);
  } catch {}
}

function togglePicker() {
  el('picker').classList.contains('hidden') ? openPicker() : closePicker();
}
el('status').addEventListener('click', togglePicker);
el('status').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); togglePicker(); }
});
el('btn-brain').addEventListener('click', () => {
  togglePicker();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePicker(); });

el('btn-quit').addEventListener('click', () => window.marina.quit());
el('btn-hide').addEventListener('click', () => window.marina.minimize());

async function chooseModel() {
  const res = await window.marina.pickVRM();
  if (res.canceled) return;
  try {
    await mountVRM(res.buffer, res.name);
  } catch (e) {
    showNotice(`Could not load ${res.name}: ${e.message}`, 'model');
  }
}

el('btn-model').addEventListener('click', chooseModel);
window.marina.onPickModel(chooseModel);

el('btn-reset').addEventListener('click', async () => {
  try {
    await post('/reset');
    say('Fine, forgotten.');
  } catch {}
});

window.__marina = {
  get vrm() { return vrm; },
  get bones() { return bones; },
  get mouthOpen() { return mouthOpen; },
  get viseme() { return viseme; },
  restyleFace,
  LIPS,
  get camera() { return camera; },
  get springs() { return springs; },
  get mouthCloseTargets() { return mouthCloseTargets; },
  get cueOut() { return cueOut; },
  get activeCues() { return activeCues.length; },
  drawGesture: () => drawGesture(),
  hitTest: (x, y) => overUI(x, y) || overAvatar(x, y),
  scheduleCues,
  speak,
  send,
  interrupt,
  isSpeaking: () => playing > 0,

  get utterance() {
    return {
      epoch: utterance.epoch,
      now: audioCtx ? audioCtx.currentTime : 0,
      chunks: utterance.chunks.map((c) => ({ index: c.index, start: c.start, end: c.end })),
    };
  },
  get pendingCues() { return pendingCues.slice(); },
  audioState: () => (audioCtx ? audioCtx.state : 'none'),
  THREE,
};

let bridgeReady = false;
let polling = false;

async function pollForBridge({ quietFor = 16000 } = {}) {
  if (polling) return;
  polling = true;

  const started = Date.now();
  let announced = false;

  while (true) {
    try {
      const res = await fetch(`${BRIDGE}/health`, { signal: AbortSignal.timeout(2000) });
      if (res.ok) {
        const info = await res.json();
        bridgeReady = true;
        polling = false;

        const see = el('btn-see');
        if (see) see.hidden = !info.vision;
        learnBrains(info.llm_labels);
        if (info.preset) markPresenting(info.preset);
        paintBrain(info.llm_mode || 'auto', info.llm_using, info.model);
        setStatus('ok', `ready · ${info.model}`);
        hideNotice('bridge');

        post('/warmup').catch(() => {});
        bargeEnabled = info.barge_in !== false;
        if (info.idle) idleMuted = !!info.idle.muted || info.idle.enabled === false;
        startOpenerPoll();
        return;
      }
    } catch {}

    const waited = Date.now() - started;
    if (waited < quietFor) {
      setStatus('busy', 'starting…');
    } else if (!announced) {
      announced = true;
      setStatus('bad', 'bridge down');
      showNotice(BRIDGE_DOWN, 'bridge');
    }

    await new Promise((r) => setTimeout(r, waited < quietFor ? 400 : 2000));
  }
}

function bridgeLost() {
  bridgeReady = false;
  pollForBridge({ quietFor: 0 });
}

el('btn-see').hidden = true;

pollForBridge();
loadFromDisk();
