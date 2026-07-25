/* The desk view: live camera with the attention overlay drawn on top.

   Detected objects arrive with pixel bounding boxes in the camera's own frame,
   and the <img> is letterboxed to fit the screen, so every box has to be mapped
   through the same scale-and-offset the browser used. Getting that wrong is the
   classic overlay bug -- boxes that drift away from the objects as the window
   resizes -- so the transform is computed from the rendered size every frame. */

const LOW = 35, HIGH = 65;

const feed = document.getElementById('desk-feed');
const canvas = document.getElementById('desk-overlay');
const ctx = canvas.getContext('2d');
const waiting = document.getElementById('waiting');
const guard = document.getElementById('guard');
const banner = document.getElementById('banner');
const hud = {
  effort: document.getElementById('hud-effort'),
  label: document.getElementById('hud-label'),
  desk: document.getElementById('s-desk'),
  gesture: document.getElementById('s-gesture'),
  eeg: document.getElementById('s-eeg'),
};

let objects = [];
let hands = [];
let handledVariant = null;
let frameSize = { width: 960, height: 540 };
let attention = null;
let effort = null;
let variants = [];
const flashes = new Map();

/* ------------------------------------------------------------- geometry */

function layout() {
  // Reproduce the letterboxing the browser applied to the <img> so overlay
  // boxes land on the objects at any window size.
  const box = feed.getBoundingClientRect();
  canvas.width = box.width;
  canvas.height = box.height;
  canvas.style.left = `${box.left}px`;
  canvas.style.top = `${box.top}px`;
  return { scaleX: box.width / frameSize.width, scaleY: box.height / frameSize.height };
}

/* -------------------------------------------------------------- drawing */

function colourFor(objectId) {
  const flash = flashes.get(objectId);
  if (flash && Date.now() < flash.until) return flash.colour;
  return null;
}

function draw() {
  requestAnimationFrame(draw);
  if (feed.hidden) return;

  const { scaleX, scaleY } = layout();
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const focusedIdx = focusedIndex();

  objects.forEach((obj, i) => {
    const [x, y, w, h] = obj.bbox;
    const rx = x * scaleX, ry = y * scaleY, rw = w * scaleX, rh = h * scaleY;
    const isFocused = i === focusedIdx;
    const flash = colourFor(obj.id);
    const colour = flash || (isFocused ? '#45e0c0' : 'rgba(140,150,168,0.55)');

    ctx.save();
    if (isFocused || flash) {
      ctx.shadowColor = colour;
      ctx.shadowBlur = 42;
      // translucent fill so the highlight reads as light on the object
      ctx.fillStyle = flash ? `${colour}33` : 'rgba(69,224,192,0.16)';
      roundRect(rx, ry, rw, rh, 14);
      ctx.fill();
    }
    ctx.strokeStyle = colour;
    ctx.lineWidth = isFocused || flash ? 4 : 2;
    roundRect(rx, ry, rw, rh, 14);
    ctx.stroke();
    ctx.restore();

    // label above the box
    const variant = variants[i];
    const name = variant ? variant.label : obj.label;
    ctx.font = `${isFocused ? '600 ' : ''}20px -apple-system, system-ui, sans-serif`;
    ctx.fillStyle = colour;
    ctx.fillText(name, rx, Math.max(24, ry - 12));

    if (isFocused && variant) {
      ctx.font = '14px ui-monospace, monospace';
      ctx.fillStyle = 'rgba(232,236,244,0.75)';
      ctx.fillText(`${variant.attention_seconds.toFixed(0)}s attention`, rx, ry + rh + 24);

      // dwell ring: fills as the look is held, so intent is visible
      const dwell = Math.min(1, (attention?.dwell ?? 0) / 1.2);
      if (dwell > 0) {
        ctx.strokeStyle = '#45e0c0';
        ctx.lineWidth = 4;
        ctx.beginPath();
        ctx.arc(rx + rw - 22, ry + 22, 14, -Math.PI / 2, -Math.PI / 2 + dwell * Math.PI * 2);
        ctx.stroke();
      }
    }
  });

  drawHands(scaleX, scaleY);
}

// MediaPipe hand topology: 21 landmarks, five chains off the wrist.
const HAND_BONES = [
  [0,1],[1,2],[2,3],[3,4], [0,5],[5,6],[6,7],[7,8], [5,9],[9,10],[10,11],[11,12],
  [9,13],[13,14],[14,15],[15,16], [13,17],[0,17],[17,18],[18,19],[19,20],
];
const FINGERTIPS = new Set([4, 8, 12, 16, 20]);

function drawHands(scaleX, scaleY) {
  hands.forEach((hand) => {
    const pts = hand.landmarks.map(([x, y]) => [x * frameSize.width * scaleX,
                                                y * frameSize.height * scaleY]);
    if (!pts.length) return;
    // Green while on a design, neutral otherwise — so "this is the one in hand"
    // reads without looking anywhere else on screen.
    const live = !!hand.touching;
    const colour = live ? '#45e0c0' : 'rgba(190,200,215,0.75)';

    ctx.save();
    ctx.shadowColor = colour;
    ctx.shadowBlur = live ? 18 : 8;
    ctx.strokeStyle = colour;
    ctx.lineWidth = live ? 3.5 : 2.5;
    ctx.lineCap = 'round';
    HAND_BONES.forEach(([a, b]) => {
      if (!pts[a] || !pts[b]) return;
      ctx.beginPath();
      ctx.moveTo(pts[a][0], pts[a][1]);
      ctx.lineTo(pts[b][0], pts[b][1]);
      ctx.stroke();
    });
    // White core over the glow, the trick that makes the skeleton read crisply.
    ctx.shadowBlur = 0;
    ctx.strokeStyle = 'rgba(255,255,255,0.85)';
    ctx.lineWidth = 1;
    HAND_BONES.forEach(([a, b]) => {
      if (!pts[a] || !pts[b]) return;
      ctx.beginPath();
      ctx.moveTo(pts[a][0], pts[a][1]);
      ctx.lineTo(pts[b][0], pts[b][1]);
      ctx.stroke();
    });

    pts.forEach(([x, y], i) => {
      ctx.fillStyle = FINGERTIPS.has(i) ? colour : 'rgba(255,255,255,0.7)';
      ctx.beginPath();
      ctx.arc(x, y, FINGERTIPS.has(i) ? 4 : 2.2, 0, Math.PI * 2);
      ctx.fill();
    });

    // Ring on the index fingertip: the landmark that decides the referent.
    const tip = pts[8];
    if (tip) {
      ctx.strokeStyle = colour;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(tip[0], tip[1], live ? 16 : 11, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.restore();
  });
}

function roundRect(x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function focusedIndex() {
  // Objects and variants are bound left-to-right by the backend, so the
  // attended variant's index is the attended object's index.
  if (!attention || attention.confidence <= 0.35 || !attention.zone) return -1;
  return variants.findIndex((v) => v.id === attention.zone);
}

/* --------------------------------------------------------------- render */

function renderHud() {
  const v = effort?.effort;
  const calibrating = !effort || effort.calibrating || v === null || v === undefined;
  hud.effort.textContent = calibrating ? '--' : Math.round(v);
  hud.effort.style.color = calibrating ? 'var(--dim)'
    : v < LOW ? 'var(--warn)' : v >= HIGH ? 'var(--focus)' : 'var(--text)';
  hud.label.textContent = calibrating ? 'calibrating'
    : v < LOW ? 'diffuse' : v >= HIGH ? 'focused' : 'engaged';

  guard.classList.toggle('on', !calibrating && v < LOW);
}

function setStatus(node, text, ok) {
  node.textContent = text;
  node.className = ok === false ? 'bad' : '';
}

/* ------------------------------------------------------------ transport */

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);

    if (msg.type === 'desk') {
      if (msg.jpeg) {
        feed.src = `data:image/jpeg;base64,${msg.jpeg}`;
        feed.hidden = false;
        waiting.style.display = 'none';
      }
      objects = msg.objects || [];
      hands = msg.hands || [];
      handledVariant = msg.handledVariant || null;
      if (msg.width) frameSize = { width: msg.width, height: msg.height };
      if (!msg.calibrated) {
        banner.innerHTML = 'Clear the desk, then press <span class="accent">c</span> in the camera window to calibrate.';
      }
    }

    if (msg.type === 'tick') {
      effort = msg.effort;
      attention = msg.attention;
      variants = (msg.variants || []).filter((v) => v.status !== 'archived');
      renderHud();

      setStatus(hud.eeg, `${msg.source.kind} · ${msg.source.status}`,
        msg.source.status === 'streaming' || msg.source.status === 'listening');
      setStatus(hud.gesture, msg.gesture?.status ?? 'idle',
        msg.gesture?.status === 'connected');
      const d = msg.desk;
      setStatus(hud.desk,
        d ? `${d.status}${d.status === 'connected'
              ? ` · ${d.objects} obj${d.handsAvailable ? ` · ${d.hands} hand` : ' · no hands'}`
              : ''}` : 'idle',
        d?.status === 'connected');

      if (feed.hidden === false) {
        const held = handledVariant ? variants.find((v) => v.id === handledVariant) : null;
        const idx = focusedIndex();
        const variant = held || (idx >= 0 ? variants[idx] : null);
        banner.innerHTML = variant
          ? `${held ? 'Holding' : 'Looking at'} <span class="accent">${variant.label}</span>` +
            ` — thumbs-up to approve, double-blink to flag.`
          : 'Look at a design on the desk, or pick one up.';
      }
    }

    if (msg.type === 'vote') {
      const colour = msg.vote === 'approve'
        ? (msg.binding ? '#45e0c0' : '#ffb547')
        : (msg.binding ? '#ff5f56' : '#ffb547');
      const idx = variants.findIndex((v) => v.id === msg.variant.id);
      if (idx >= 0 && objects[idx]) {
        flashes.set(objects[idx].id, { colour, until: Date.now() + 2000 });
      }
      const verb = msg.vote === 'approve' ? 'Approved' : 'Rejected';
      banner.innerHTML = msg.binding
        ? `<span class="accent">${verb} ${msg.variant.label}</span> — effort ${Math.round(msg.effort)}, ${msg.decisive ? 'merged' : 'staged for review'}.`
        : `<span class="warn">${verb} ${msg.variant.label}</span> — effort ${Math.round(msg.effort)}. Held for review, not merged.`;
    }

    if (msg.type === 'flag') {
      banner.innerHTML = `<span class="accent">Flagged.</span> Marked as deliberate.`;
    }
  };

  ws.onclose = () => setTimeout(connect, 1200);
}

window.addEventListener('resize', layout);
connect();
draw();
