/* The phone half of the desk camera.

   Grabs the rear camera, centre-crops each frame to the 16:9 the bench works
   in, and ships it as JPEG over a WebSocket. Detection happens on the laptop,
   so this page stays a dumb sensor -- which is what makes it swappable with
   the overhead webcam without anything downstream noticing.

   Two things that matter more than they look:

   * Frames are sent only when the socket has drained. A phone on congested
     venue WiFi that queues frames faster than it can flush them ends up
     showing the desk as it was ten seconds ago, and an overlay that lags the
     table is worse than no overlay.
   * `getUserMedia` needs a secure context. Over plain http:// the page loads
     and the camera is then refused by the browser, so that case is detected up
     front and explained rather than left as a silent black rectangle. */

const FRAME_W = 960, FRAME_H = 540;
const TARGET_FPS = 10;
const JPEG_QUALITY = 0.55;
const MAX_BUFFERED = 512 * 1024;   // bytes in flight before we skip a frame

const el = (id) => document.getElementById(id);
const nodes = {
  dot: el('dot'), state: el('state'), stage: el('stage'), preview: el('preview'),
  count: el('count'), controls: el('controls'), calibrate: el('calibrate'), hint: el('hint'),
  notice: el('notice'), noticeTitle: el('notice-title'),
  noticeBody: el('notice-body'), noticeUrl: el('notice-url'),
};

const token = new URLSearchParams(location.search).get('k') || '';
const canvas = document.createElement('canvas');
canvas.width = FRAME_W;
canvas.height = FRAME_H;
const ctx = canvas.getContext('2d', { alpha: false });

let ws = null;
let attempts = 0;
let stream = null;
let sending = false;
let skipped = 0;

/* ----------------------------------------------------------------- status */

function status(text, kind) {
  nodes.state.textContent = text;
  nodes.dot.className = `dot${kind ? ` ${kind}` : ''}`;
}

function fail(title, body, url) {
  nodes.notice.classList.remove('hidden');
  nodes.stage.classList.add('hidden');
  nodes.controls.classList.add('hidden');
  nodes.noticeTitle.textContent = title;
  nodes.noticeBody.textContent = body;
  nodes.noticeUrl.textContent = url || '';
  status('not streaming', 'bad');
}

/* ----------------------------------------------------------------- camera */

async function startCamera() {
  if (!window.isSecureContext) {
    // Every mobile browser refuses the camera on a plain-http LAN address.
    const secure = location.href.replace(/^http:/, 'https:');
    fail('This page needs HTTPS',
      'Phone browsers only allow camera access on a secure page. Restart the '
      + 'bench with --https, then scan the QR code again — the phone will warn '
      + 'once about the self-signed certificate, and you tap through it.',
      secure);
    return;
  }

  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: { ideal: 'environment' },
        width: { ideal: 1280 },
        height: { ideal: 720 },
      },
      audio: false,
    });
  } catch (err) {
    fail('Camera blocked',
      `The browser refused the camera (${err.name}). Check the permission for `
      + 'this site, close any other app using the camera, and reload.');
    return;
  }

  nodes.preview.srcObject = stream;
  await nodes.preview.play().catch(() => {});
  keepAwake();
  connect();
  setInterval(pushFrame, 1000 / TARGET_FPS);
}

// Nobody is touching the phone once it is propped over the desk, so without
// this the screen sleeps and the camera track stops.
async function keepAwake() {
  try {
    if ('wakeLock' in navigator) await navigator.wakeLock.request('screen');
  } catch { /* not fatal — the stream just ends when the screen sleeps */ }
}

/* -------------------------------------------------------------- transport */

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/phone?k=${encodeURIComponent(token)}`);

  ws.onopen = () => {
    attempts = 0;
    status('streaming', 'on');
    ws.send(JSON.stringify({ type: 'hello', device: navigator.userAgent.slice(0, 120) }));
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === 'phone_state') {
      nodes.count.textContent = msg.calibrated
        ? `${msg.objects} object${msg.objects === 1 ? '' : 's'} on the desk`
        : 'waiting for an empty-desk reference';
      nodes.calibrate.textContent = msg.calibrated
        ? 'Re-capture empty desk' : 'Capture empty desk';
    }
    if (msg.type === 'rejected') {
      fail('Pairing token rejected',
        'This QR code is from an earlier run of the bench. Reload the control '
        + 'screen and scan the code it shows now.');
      ws.close();
    }
  };

  ws.onclose = () => {
    if (!nodes.notice.classList.contains('hidden')) return;
    attempts += 1;
    status('reconnecting…', 'bad');
    // Nothing is answering. The camera works -- that is visible above -- so the
    // missing half is the bench, and saying so beats an endless "reconnecting"
    // for someone who scanned a QR code off a slide rather than off a laptop.
    if (attempts >= 3) {
      nodes.count.textContent = 'not paired';
      nodes.hint.innerHTML = '<b>No bench is answering on this network.</b> This page is '
        + 'what the QR code opens — the camera is live, but the frames have nowhere to go. '
        + 'Run <code>python -m backend.server --lan --https</code> on your laptop and scan the '
        + 'code on its control view.';
    }
    setTimeout(connect, 1200);
  };
}

function pushFrame() {
  if (!ws || ws.readyState !== WebSocket.OPEN || sending) return;
  const video = nodes.preview;
  if (!video.videoWidth) return;

  if (ws.bufferedAmount > MAX_BUFFERED) {
    skipped += 1;
    status(`streaming · ${skipped} dropped`, 'on');
    return;
  }

  sending = true;
  try {
    drawCropped(video);
    const jpeg = canvas.toDataURL('image/jpeg', JPEG_QUALITY).split(',', 2)[1];
    ws.send(JSON.stringify({ type: 'frame', jpeg }));
  } finally {
    sending = false;
  }
}

function drawCropped(video) {
  // Centre-crop rather than squash: the desk view maps bounding boxes straight
  // onto this frame, so a distorted aspect ratio would misplace every overlay.
  const vw = video.videoWidth, vh = video.videoHeight;
  const scale = Math.max(FRAME_W / vw, FRAME_H / vh);
  const dw = vw * scale, dh = vh * scale;
  ctx.drawImage(video, (FRAME_W - dw) / 2, (FRAME_H - dh) / 2, dw, dh);
}

/* --------------------------------------------------------------- controls */

nodes.calibrate.addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'calibrate' }));
    nodes.count.textContent = 'capturing reference…';
  }
});

// A phone that loses the camera when you switch apps should pick it back up.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    keepAwake();
    nodes.preview.play().catch(() => {});
  }
});

startCamera();
