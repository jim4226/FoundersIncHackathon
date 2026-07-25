/* "Hold it" — the CAD part rendered into the operator's own hand.
 *
 * Three things make this more than a sticker on a webcam feed:
 *
 *   1. Scale is real. Pixels-per-millimetre comes from the operator's knuckle
 *      span, so a 118 mm enclosure is 118 mm against their actual fingers. That
 *      is the entire reason to do this at the bench instead of in a viewport --
 *      "is it too thick" is a question a screen cannot answer.
 *   2. Pose comes from the palm, not the bounding box. The part is seated on a
 *      basis built from wrist and knuckles, so it rolls and yaws with the hand.
 *   3. Fingers occlude it. After the part is drawn, the camera's own pixels are
 *      re-drawn clipped to the finger silhouette, so the hand closes over the
 *      part rather than the part floating on top of the hand.
 *
 * Hand tracking is MediaPipe when it loads and a simulated hand when it does
 * not, which is the same degradation rule the rest of the bench follows: no
 * webcam, no network, still a demo.
 */

const TASKS_VISION = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14';
const HAND_MODEL =
  'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task';

// Landmark indices we care about (MediaPipe hand topology).
const WRIST = 0, INDEX_MCP = 5, MIDDLE_MCP = 9, RING_MCP = 13, PINKY_MCP = 17;

// Anthropometry, adult median. Two independent estimates of the same scale;
// both only ever foreshorten, so the larger one is the honest one.
const KNUCKLE_SPAN_MM = 80;   // index MCP -> pinky MCP
const PALM_LENGTH_MM = 97;    // wrist -> middle MCP

const HAND_BONES = [
  [0, 1], [1, 2], [2, 3], [3, 4],
  [0, 5], [5, 6], [6, 7], [7, 8],
  [9, 10], [10, 11], [11, 12],
  [13, 14], [14, 15], [15, 16],
  [0, 17], [17, 18], [18, 19], [19, 20],
  [5, 9], [9, 13], [13, 17],
];

/* A canonical right hand in palm-space millimetres, used to draw the simulated
   hand. Same 21-point topology as MediaPipe so one draw path serves both. */
const CANONICAL = (() => {
  const pts = [
    [0, 52, -6],
    [-30, 38, 4], [-46, 24, 10], [-56, 8, 16], [-62, -4, 20],
    [-40, 0, 0], [-42, -24, 2], [-43, -40, 3], [-43, -52, 3],
    [-13, 2, 0], [-14, -28, 3], [-14, -46, 4], [-14, -58, 4],
    [13, 0, 0], [15, -25, 3], [16, -42, 4], [16, -53, 4],
    [40, -3, 0], [43, -21, 2], [45, -34, 3], [46, -43, 3],
  ];
  // Re-centre on the same palm centroid the tracker computes, so the simulated
  // hand and a real hand put the part in the same place.
  const key = [WRIST, INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP];
  const c = [0, 1, 2].map((k) => key.reduce((s, i) => s + pts[i][k], 0) / key.length);
  return pts.map((p) => [p[0] - c[0], p[1] - c[1], p[2] - c[2]]);
})();

const el = (id) => document.getElementById(id);
const canvas = el('stage');
const ctx = canvas.getContext('2d');
const video = el('cam');

const state = {
  project: BenchCAD.projects[0],
  partIndex: 0,
  deviceId: null,          // which camera; null = whatever the browser picks
  cameras: [],
  mirror: true,            // self-view cameras read backwards without this
  spanMm: KNUCKLE_SPAN_MM,
  tilt: -0.32,
  spin: 0,
  dims: true,
  wire: false,
  occlude: true,
  skeleton: true,
  frozen: false,
  mode: 'starting',        // starting | tracking | searching | simulated
  detail: '',
  energy: 0.62,            // drives the glow; bench effort overrides it
  glow: '#45e0c0',
  benchLabel: null,
  fps: 0,
};

const smooth = { ox: 0, oy: 0, s: 0, u: null, v: null, alpha: 0, ready: false };
let landmarks = null;      // latest 21 points in canvas pixels
let landmarker = null;
let lastSeen = 0;

const part = () => state.project.parts[state.partIndex];
const model = () => BenchCAD.models[part().model];

/* ------------------------------------------------------------------ setup */

const dpr = () => Math.min(2, window.devicePixelRatio || 1);

function resize() {
  const rect = canvas.getBoundingClientRect();
  const r = dpr();
  canvas.width = Math.round(rect.width * r);
  canvas.height = Math.round(rect.height * r);
  ctx.setTransform(r, 0, 0, r, 0, 0);
}
window.addEventListener('resize', resize);

/* ------------------------------------------------------------- the camera */

/* A laptop lid camera and a webcam pointed at the desk are both plausible
   sources and the browser's default is not reliably the one you want, so the
   source is picked explicitly and remembered. */

const CAM_KEY = 'bench.hold.camera';
const MIRROR_KEY = 'bench.hold.mirror';

function stopCamera() {
  const stream = video.srcObject;
  if (!stream) return;
  stream.getTracks().forEach((t) => t.stop());
  video.srcObject = null;
}

async function startCamera(deviceId) {
  stopCamera();
  const size = { width: { ideal: 1280 }, height: { ideal: 720 } };
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: deviceId ? { deviceId: { exact: deviceId }, ...size } : { facingMode: 'user', ...size },
      audio: false,
    });
    video.srcObject = stream;
  } catch (err) {
    // A remembered camera can be unplugged, or busy in another app. Falling
    // back to the default beats refusing to open anything at all.
    if (!deviceId) throw err;
    const stream = await navigator.mediaDevices.getUserMedia({ video: size, audio: false });
    video.srcObject = stream;
    flash('That camera is unavailable — using the default.', '#ffb547');
  }
  await video.play();

  const track = video.srcObject.getVideoTracks()[0];
  state.deviceId = (track && track.getSettings().deviceId) || deviceId || null;
  if (state.deviceId) localStorage.setItem(CAM_KEY, state.deviceId);

  // Switching source teleports the hand; drop the smoothing so it re-seats
  // instantly rather than sliding across the frame.
  smooth.ready = false;
  landmarks = null;
  return track ? track.label : '';
}

/* Device labels are blank until camera permission has been granted, so this is
   always called *after* the first successful getUserMedia. */
async function refreshCameras() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  state.cameras = devices.filter((d) => d.kind === 'videoinput');

  const select = el('camera');
  select.replaceChildren(...state.cameras.map((cam, i) => {
    const option = document.createElement('option');
    option.value = cam.deviceId;
    option.textContent = cam.label || `Camera ${i + 1}`;
    return option;
  }));

  if (state.deviceId && state.cameras.some((c) => c.deviceId === state.deviceId)) {
    select.value = state.deviceId;
  }
  select.disabled = state.cameras.length === 0;
  el('camera-count').textContent = state.cameras.length > 1
    ? `${state.cameras.length} available`
    : (state.cameras.length ? 'only one' : 'none found');
}

async function useCamera(deviceId) {
  try {
    const label = await startCamera(deviceId);
    await refreshCameras();
    setMode(state.mode === 'tracking' ? 'tracking' : 'searching', label || 'show your hand');
  } catch (err) {
    setMode('simulated', String(err && err.message ? err.message : err).slice(0, 80));
  }
}

function cycleCamera() {
  if (state.cameras.length < 2) return;
  const at = state.cameras.findIndex((c) => c.deviceId === state.deviceId);
  useCamera(state.cameras[(at + 1) % state.cameras.length].deviceId);
}

/* The tracker is the one thing fetched from the network. A blocked CDN tends to
   hang rather than fail, and a demo stuck on "loading" forever is worse than one
   that admits it can't get there -- so the load is raced against a deadline. */
const TRACKER_TIMEOUT_MS = 12000;

async function startTracker() {
  const load = (async () => {
    const vision = await import(`${TASKS_VISION}/vision_bundle.mjs`);
    const fileset = await vision.FilesetResolver.forVisionTasks(`${TASKS_VISION}/wasm`);
    return vision.HandLandmarker.createFromOptions(fileset, {
      baseOptions: { modelAssetPath: HAND_MODEL, delegate: 'GPU' },
      runningMode: 'VIDEO',
      numHands: 1,
    });
  })();

  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error('hand model unreachable — check the network')),
      TRACKER_TIMEOUT_MS);
  });

  try {
    landmarker = await Promise.race([load, deadline]);
  } finally {
    clearTimeout(timer);
  }
}

async function boot() {
  resize();
  renderRail();
  connectBench();

  const params = new URLSearchParams(location.search);
  state.mirror = localStorage.getItem(MIRROR_KEY) !== 'off';
  el('toggle-mirror').checked = state.mirror;

  if (params.has('sim')) {
    setMode('simulated', 'forced by ?sim');
  } else if (!navigator.mediaDevices || !window.isSecureContext) {
    // Browsers only expose cameras on https:// or localhost. Reaching the page
    // by LAN address over plain http is the usual way to land here, and the
    // symptom is a bare "undefined" from mediaDevices -- so name it instead.
    setMode('simulated', `no camera on ${location.protocol}//${location.hostname} — open it as localhost or over https`);
    el('camera-count').textContent = 'blocked';
  } else {
    try {
      setMode('starting', 'opening camera');
      const label = await startCamera(params.get('cam') || localStorage.getItem(CAM_KEY));
      await refreshCameras();
      setMode('starting', 'loading hand model');
      await startTracker();
      setMode('searching', label || 'show your hand');
      navigator.mediaDevices.addEventListener('devicechange', refreshCameras);
    } catch (err) {
      // Camera without tracking cannot place a part, so drop back to the fully
      // simulated view rather than leaving a live feed with nothing on it.
      stopCamera();
      setMode('simulated', String(err && err.message ? err.message : err).slice(0, 80));
    }
  }
  requestAnimationFrame(loop);
}

function setMode(mode, detail) {
  state.mode = mode;
  state.detail = detail || '';
  const badge = el('mode');
  badge.textContent = {
    starting: 'starting', tracking: 'hand locked',
    searching: 'looking for a hand', simulated: 'simulated hand',
  }[mode];
  badge.className = 'mode ' + mode;
  el('mode-detail').textContent = state.detail;
}

/* --------------------------------------------------------------- tracking */

/* Build the palm frame the renderer wants: an origin, an across-palm axis, an
   along-palm axis, and a scale. Every quantity is a *relationship between
   landmarks* rather than an absolute, which is what makes it survive the hand
   moving toward and away from the camera. */
function frameFromLandmarks(pts) {
  const c = [WRIST, INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP];
  const o = [0, 1, 2].map((k) => c.reduce((s, i) => s + pts[i][k], 0) / c.length);

  const across = sub3(pts[PINKY_MCP], pts[INDEX_MCP]);
  // Along-palm points *wrist-ward*: the part is seated so its lens end reaches
  // past the fingertips, which is how you would actually hold the thing.
  const along = sub3(pts[WRIST], pts[MIDDLE_MCP]);

  const dSpan = Math.hypot(across[0], across[1], across[2]);
  const dPalm = Math.hypot(along[0], along[1], along[2]);
  const s = Math.max(dSpan / state.spanMm, dPalm / (PALM_LENGTH_MM * (state.spanMm / KNUCKLE_SPAN_MM)));

  return { ox: o[0], oy: o[1], u: unit3(across), v: unit3(along), s };
}

const sub3 = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const unit3 = (a) => {
  const l = Math.hypot(a[0], a[1], a[2]) || 1;
  return [a[0] / l, a[1] / l, a[2] / l];
};

/* Exponential smoothing with a velocity-aware rate: sticky when the hand is
   still (kills landmark jitter, which is what makes AR look cheap) and loose
   when it moves (no rubber-banding). */
function smoothFrame(f, dt) {
  if (!smooth.ready) {
    Object.assign(smooth, { ...f, alpha: 0, ready: true });
    return;
  }
  const travel = Math.hypot(f.ox - smooth.ox, f.oy - smooth.oy) / Math.max(1, f.s);
  const k = 1 - Math.exp(-dt * (7 + Math.min(26, travel * 2.2)));
  smooth.ox += (f.ox - smooth.ox) * k;
  smooth.oy += (f.oy - smooth.oy) * k;
  smooth.s += (f.s - smooth.s) * k;
  smooth.u = smooth.u ? smooth.u.map((x, i) => x + (f.u[i] - x) * k) : f.u;
  smooth.v = smooth.v ? smooth.v.map((x, i) => x + (f.v[i] - x) * k) : f.v;
  smooth.u = unit3(smooth.u);
  smooth.v = unit3(smooth.v);
}

/* Landmarks are mirrored to match the painted frame, and z is scaled into
   pixels (MediaPipe reports it on roughly the same scale as x). Mirroring flips
   the basis handedness, which the renderer already absorbs -- it derives the
   palm normal and forces it toward the camera either way. */
function toCanvas(lm, w, h) {
  return lm.map((p) => [(state.mirror ? 1 - p.x : p.x) * w, p.y * h, -p.z * w]);
}

/* ---------------------------------------------------------------- drawing */

/* The single place the camera frame is painted. The occlusion pass re-uses it
   so the two can never disagree about fit or mirroring -- a half-mirrored
   occlusion would put the fingers on the wrong side of the part. */
function paintVideo(w, h) {
  const scale = Math.max(w / video.videoWidth, h / video.videoHeight);
  const dw = video.videoWidth * scale, dh = video.videoHeight * scale;
  ctx.save();
  if (state.mirror) {
    ctx.translate(w, 0);
    ctx.scale(-1, 1);
  }
  ctx.drawImage(video, (w - dw) / 2, (h - dh) / 2, dw, dh);
  ctx.restore();
}

function drawVideo(w, h) {
  if (!video.videoWidth) {
    ctx.fillStyle = '#08090c';
    ctx.fillRect(0, 0, w, h);
    return;
  }
  paintVideo(w, h);
  // Knock the room back so the projected part reads as the brightest thing.
  ctx.fillStyle = 'rgba(8,9,12,0.42)';
  ctx.fillRect(0, 0, w, h);
}

/* Over a camera feed the operator's real hand is already there, so the skeleton
   is a thin confirmation that tracking is locked. With no camera there is
   nothing underneath, so `mm` fattens the bones into capsules and the hand has
   to carry the image itself. */
function drawHand(pts, bones, alpha, mm, s) {
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.beginPath();
  for (const [a, b] of bones) {
    ctx.moveTo(pts[a][0], pts[a][1]);
    ctx.lineTo(pts[b][0], pts[b][1]);
  }
  if (mm) {
    ctx.strokeStyle = 'rgba(20,58,54,0.80)';
    ctx.lineWidth = mm * s;
    ctx.stroke();
    ctx.strokeStyle = 'rgba(69,224,192,0.30)';
    ctx.lineWidth = mm * s * 0.86;
    ctx.stroke();
  }
  ctx.strokeStyle = 'rgba(120,244,220,0.50)';
  ctx.shadowColor = 'rgba(69,224,192,0.55)';
  ctx.shadowBlur = 8;
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.shadowBlur = 0;
  ctx.fillStyle = 'rgba(232,236,244,0.5)';
  for (const [, b] of bones) {
    ctx.beginPath();
    ctx.arc(pts[b][0], pts[b][1], 2.4, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();
}

/* Re-draw the camera's own pixels over the finished render, clipped to the
   fingers. Where the part did not overlap a finger this paints video back onto
   identical video and is invisible; where it did, the hand closes over it. */
const FINGER_CHAINS = [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12], [13, 14, 15, 16], [17, 18, 19, 20]];
// Segments past the knuckle -- the only ones that can curl in front of a part.
const FINGERS_OVER = FINGER_CHAINS.flatMap((c) => [[c[1], c[2]], [c[2], c[3]]]);

function occludeWithFingers(pts, w, h, span) {
  if (!video.videoWidth) return;
  ctx.save();
  ctx.beginPath();
  for (const chain of FINGER_CHAINS) {
    // Only the segments beyond the knuckle can curl in front of the part.
    for (let i = 1; i < chain.length; i++) {
      const a = pts[chain[i - 1]], b = pts[chain[i]];
      const r = span * (0.055 - 0.008 * i);
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
      ctx.arc(b[0], b[1], r, 0, Math.PI * 2);
      ctx.arc(a[0], a[1], r, 0, Math.PI * 2);
      // Widen the stroke into a capsule by stamping circles along it.
      const steps = 6;
      for (let t = 1; t < steps; t++) {
        const x = a[0] + (b[0] - a[0]) * (t / steps);
        const y = a[1] + (b[1] - a[1]) * (t / steps);
        ctx.moveTo(x + r, y);
        ctx.arc(x, y, r, 0, Math.PI * 2);
      }
    }
  }
  ctx.clip();
  paintVideo(w, h);
  ctx.restore();
}

function drawReticle(f, alpha) {
  ctx.save();
  ctx.globalAlpha = alpha * 0.5;
  ctx.strokeStyle = 'rgba(69,224,192,0.5)';
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 5]);
  ctx.beginPath();
  ctx.ellipse(f.ox, f.oy, f.s * 46, f.s * 30, Math.atan2(f.u[1], f.u[0]), 0, Math.PI * 2);
  ctx.stroke();
  ctx.restore();
}

/* ------------------------------------------------------------- simulation */

function simulatedFrame(t, w, h) {
  const drift = Math.sin(t * 0.55), sway = Math.sin(t * 0.37 + 1.1);
  const roll = 0.30 * drift, yaw = 0.22 * sway;
  const u = unit3([Math.cos(roll), Math.sin(roll) * 0.25, Math.sin(yaw) * 0.75]);
  const v = unit3([-Math.sin(roll) * 0.35, -Math.cos(roll), Math.sin(t * 0.3) * 0.35]);
  const base = Math.min(w, h);
  return {
    ox: w / 2 + sway * base * 0.045,
    oy: h * 0.58 + drift * base * 0.03,
    u, v,
    s: (base * 0.0042) * (1 + 0.03 * Math.sin(t * 0.8)),
  };
}

/* Place the canonical hand through the frame, giving 21 screen points that the
   skeleton and the occlusion pass can consume exactly as if they were real. */
function simulatedLandmarks(f) {
  let u = [f.u[0], f.u[1], f.u[2]];
  let v = [f.v[0], f.v[1], f.v[2]];
  const n = [
    u[1] * v[2] - u[2] * v[1],
    u[2] * v[0] - u[0] * v[2],
    u[0] * v[1] - u[1] * v[0],
  ];
  return CANONICAL.map(([x, y, z]) => [
    f.ox + f.s * (u[0] * x + v[0] * y + n[0] * z),
    f.oy + f.s * (u[1] * x + v[1] * y + n[1] * z),
    f.s * (u[2] * x + v[2] * y + n[2] * z),
  ]);
}

/* -------------------------------------------------------------- main loop */

let lastT = performance.now();
let fpsAvg = 0;

function loop(now) {
  requestAnimationFrame(loop);
  const dt = Math.min(0.1, (now - lastT) / 1000);
  lastT = now;
  fpsAvg += (1 / Math.max(dt, 1e-3) - fpsAvg) * 0.05;
  state.fps = fpsAvg;

  // CSS pixels: the context carries the device-pixel-ratio transform already.
  const rect = canvas.getBoundingClientRect();
  const w = rect.width, h = rect.height;
  if (Math.abs(canvas.width - rect.width * dpr()) > 1) resize();

  ctx.clearRect(0, 0, w, h);
  drawVideo(w, h);

  let frame = null;

  if (landmarker && video.readyState >= 2 && !state.frozen) {
    let result = null;
    try {
      result = landmarker.detectForVideo(video, now);
    } catch (_) { /* a dropped frame must not stop the loop */ }
    if (result && result.landmarks && result.landmarks.length) {
      landmarks = toCanvas(result.landmarks[0], w, h);
      lastSeen = now;
      if (state.mode !== 'tracking') setMode('tracking', '');
    } else if (now - lastSeen > 400 && state.mode === 'tracking') {
      setMode('searching', 'show your hand');
    }
  }

  const live = landmarks && (now - lastSeen) < 700;
  if (live) {
    frame = frameFromLandmarks(landmarks);
  } else if (state.mode === 'simulated' || !landmarker) {
    frame = simulatedFrame(now / 1000, w, h);
    landmarks = simulatedLandmarks(frame);
  }

  // Fade rather than pop when the hand leaves the frame.
  const target = frame ? 1 : 0;
  smooth.alpha += (target - smooth.alpha) * (1 - Math.exp(-dt * 6));

  if (frame) smoothFrame(frame, dt);

  if (smooth.ready && smooth.alpha > 0.02) {
    const f = { ox: smooth.ox, oy: smooth.oy, u: smooth.u, v: smooth.v, s: smooth.s };
    const hasVideo = !!video.videoWidth;
    if (state.skeleton && landmarks) {
      drawHand(landmarks, HAND_BONES, smooth.alpha * (hasVideo ? 0.8 : 1), hasVideo ? 0 : 15, smooth.s);
    }
    drawReticle(f, smooth.alpha);

    BenchCAD.render(ctx, model(), f, {
      energy: state.energy,
      glow: state.glow,
      alpha: smooth.alpha,
      tilt: state.tilt,
      spin: state.spin,
      dims: state.dims,
      wire: state.wire,
    });

    if (state.occlude && landmarks) {
      if (hasVideo) {
        // The camera's own pixels are the truth about what is in front.
        occludeWithFingers(landmarks, w, h, smooth.s * KNUCKLE_SPAN_MM);
      } else if (state.skeleton && model().mount === 'grip') {
        drawHand(landmarks, FINGERS_OVER, smooth.alpha, 15, smooth.s);
      }
    }
  }

  drawHud();
}

function drawHud() {
  const m = model();
  el('readout-size').textContent = `${m.mm[0]} × ${m.mm[1]} × ${m.mm[2]} mm · ${m.mass}`;
  el('readout-scale').textContent = smooth.s ? `${smooth.s.toFixed(2)} px/mm · 1:1` : '—';
  el('readout-fps').textContent = `${state.fps.toFixed(0)} fps`;
}

/* ------------------------------------------------------------------- rail */

function renderRail() {
  el('project-name').textContent = state.project.name;
  el('project-desc').textContent = state.project.description;
  el('rail').innerHTML = state.project.parts.map((p, i) => {
    const m = BenchCAD.models[p.model];
    return `<button class="card ${i === state.partIndex ? 'on' : ''}" data-i="${i}">
      <div class="card-top">
        <span class="card-label">${m.label}</span>
        <span class="card-key">${i + 1}</span>
      </div>
      <div class="card-file">${m.part}</div>
      <p>${m.summary}</p>
      <div class="card-foot">
        <span>${m.mm[0]} × ${m.mm[1]} × ${m.mm[2]} mm</span>
        <span class="mount ${m.mount}">${m.mount === 'flat' ? 'rests on palm' : 'held in palm'}</span>
      </div>
    </button>`;
  }).join('');

  el('rail').querySelectorAll('.card').forEach((b) => {
    b.addEventListener('click', () => select(Number(b.dataset.i)));
  });
}

function select(i) {
  if (i < 0 || i >= state.project.parts.length) return;
  state.partIndex = i;
  state.spin = 0;
  renderRail();
}

/* --------------------------------------------------------------- the bench */

/* Optional. When the bench is up, the part in your hand is the part the bench
   says you are attending to, and the glow tracks measured effort -- so the
   render is not a viewer, it is the same fused state the table is showing. */
function connectBench() {
  let ws;
  try {
    ws = new WebSocket(`ws://${location.host}/ws`);
  } catch (_) {
    el('bench').textContent = 'bench offline';
    return;
  }

  ws.onopen = () => { el('bench').textContent = 'bench connected'; el('bench').className = 'signal-value ok'; };
  ws.onclose = () => {
    el('bench').textContent = 'bench offline';
    el('bench').className = 'signal-value';
    setTimeout(connectBench, 2500);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'tick') {
      const effort = msg.effort && msg.effort.effort;
      if (typeof effort === 'number') {
        state.energy = Math.max(0.15, Math.min(1, effort / 100));
        state.glow = effort < 35 ? '#ffb547' : '#45e0c0';
        el('effort').textContent = `${Math.round(effort)}`;
      }
      const att = msg.attention || {};
      const zone = (att.confidence || 0) > 0.35 ? att.zone : null;
      const v = (msg.variants || []).find((x) => x.id === zone);
      if (v && v.label !== state.benchLabel) {
        state.benchLabel = v.label;
        const idx = state.project.parts.findIndex((p) => p.variantLabel === v.label);
        if (idx >= 0 && idx !== state.partIndex && el('follow').checked) select(idx);
      }
      el('focus').textContent = v ? v.label : '—';
    }
    if (msg.type === 'vote') {
      flash(`${msg.vote === 'approve' ? 'APPROVED' : 'REJECTED'} · ${msg.variant.label}` +
        (msg.binding ? '' : ' · held, you were diffuse'), msg.vote === 'approve' ? '#45e0c0' : '#ff5f56');
    }
  };
}

let flashTimer;
function flash(text, colour) {
  const node = el('flash');
  node.textContent = text;
  node.style.color = colour;
  node.classList.add('show');
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => node.classList.remove('show'), 2600);
}

/* --------------------------------------------------------------- controls */

document.addEventListener('keydown', (e) => {
  const keys = {
    '1': () => select(0), '2': () => select(1), '3': () => select(2),
    ArrowLeft: () => { state.spin -= 0.12; },
    ArrowRight: () => { state.spin += 0.12; },
    ArrowUp: () => { state.tilt = Math.max(-1.2, state.tilt - 0.06); },
    ArrowDown: () => { state.tilt = Math.min(1.2, state.tilt + 0.06); },
    d: () => { state.dims = !state.dims; },
    w: () => { state.wire = !state.wire; },
    o: () => { state.occlude = !state.occlude; },
    h: () => { state.skeleton = !state.skeleton; },
    f: () => { state.frozen = !state.frozen; },
    c: () => cycleCamera(),
    m: () => { el('toggle-mirror').checked = !state.mirror; setMirror(!state.mirror); },
    '[': () => { state.spanMm = Math.max(60, state.spanMm - 2); showSpan(); },
    ']': () => { state.spanMm = Math.min(110, state.spanMm + 2); showSpan(); },
  };
  if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
});

function showSpan() {
  flash(`knuckle span ${state.spanMm} mm — scale calibration`, '#45e0c0');
}

function setMirror(on) {
  state.mirror = on;
  localStorage.setItem(MIRROR_KEY, on ? 'on' : 'off');
}

el('camera').addEventListener('change', (e) => useCamera(e.target.value));
el('toggle-mirror').addEventListener('change', (e) => setMirror(e.target.checked));
el('rotate').addEventListener('input', (e) => { state.spin = (e.target.value / 100) * Math.PI; });
el('tilt').addEventListener('input', (e) => { state.tilt = (e.target.value / 100) * 1.2; });
el('span').addEventListener('input', (e) => { state.spanMm = Number(e.target.value); });
el('toggle-dims').addEventListener('change', (e) => { state.dims = e.target.checked; });
el('toggle-wire').addEventListener('change', (e) => { state.wire = e.target.checked; });
el('toggle-occlude').addEventListener('change', (e) => { state.occlude = e.target.checked; });

boot();
