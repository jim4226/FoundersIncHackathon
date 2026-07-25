/* Consumes the semantic event stream from /ws and draws the bench.
   The page never touches EEG itself -- it renders `effort`, `attention` and
   `flag` events, which is why swapping the simulator for a real headband (or a
   recorded replay) changes nothing here. */

const LOW = 35, HIGH = 65;

const el = (id) => document.getElementById(id);
const nodes = {
  effortValue: el('effort-value'), effortLabel: el('effort-label'), effortFill: el('effort-fill'),
  components: el('components'), blinkRate: el('blink-rate'), saccades: el('saccades'),
  sourceKind: el('source-kind'), contact: el('contact'), gesture: el('gesture'),
  guardrail: el('guardrail'), guardrailText: el('guardrail-text'),
  variants: el('variants'), gazeMarker: el('gaze-marker'), tableCaption: el('table-caption'),
  flagToast: el('flag-toast'), history: el('history'),
  agentForm: el('agent-form'), agentInput: el('agent-input'), agentSend: el('agent-send'),
  simGaze: el('sim-gaze'), simEffort: el('sim-effort'),
};

let latest = { effort: null, attention: null, variants: [] };
const entries = new Map();
const hostedStatic = location.hostname.endsWith('.netlify.app')
  || new URLSearchParams(location.search).has('replay');

let replayMode = false;
let replayTimer = null;
let reconnectTimer = null;
let socketHadMessage = false;

const replay = {
  effort: 72,
  gaze: -0.65,
  attentionSince: Date.now(),
  lastTick: Date.now(),
  flagUntil: 0,
  saccades: 0,
  sequence: 1,
  variants: [
    {
      id: 'replay-shell-a',
      label: 'Shell A',
      position: -0.65,
      summary: 'Wraparound grip, 31 mm thick, single-shot mould, 2 mm walls.',
      version: 1,
      status: 'candidate',
      attention_seconds: 0,
    },
    {
      id: 'replay-shell-b',
      label: 'Shell B',
      position: 0.65,
      summary: 'Slab back with vented fin, 27 mm thick, side-action tool required.',
      version: 1,
      status: 'candidate',
      attention_seconds: 0,
    },
  ],
};

/* ------------------------------------------------------------- rendering */

function effortColour(v) {
  if (v === null || v === undefined) return 'var(--dim)';
  if (v < LOW) return 'var(--warn)';
  if (v >= HIGH) return 'var(--focus)';
  return 'var(--text)';
}

function labelFor(v) {
  if (v === null || v === undefined) return 'calibrating';
  if (v < LOW) return 'diffuse';
  if (v >= HIGH) return 'focused';
  return 'engaged';
}

function renderEffort(e) {
  if (!e) return;
  const v = e.effort;
  const calibrating = e.calibrating || v === null;

  nodes.effortValue.textContent = calibrating ? '--' : Math.round(v);
  nodes.effortValue.style.color = effortColour(calibrating ? null : v);
  nodes.effortLabel.textContent = calibrating
    ? `calibrating ${Math.round((e.calibrationProgress || 0) * 100)}%`
    : labelFor(v);
  nodes.effortFill.style.width = `${calibrating ? 0 : v}%`;
  nodes.effortFill.style.background = v < LOW
    ? 'linear-gradient(90deg,#8a5a1e,var(--warn))'
    : 'linear-gradient(90deg,var(--focus-dim),var(--focus))';

  nodes.blinkRate.textContent = e.blinkRate?.toFixed(1) ?? '--';

  const comps = e.components || {};
  const rows = [
    ['ocular', 'ocular', comps.ocular],
    ['cortical', 'cortical', comps.cortical],
    ['stillness', 'stillness', comps.stillness],
  ];
  nodes.components.innerHTML = rows.map(([key, name, val]) => {
    const pending = val === null || val === undefined;
    return `<div class="comp ${pending ? 'pending' : ''}">
      <span class="comp-name">${name}</span>
      <span class="comp-track"><span class="comp-bar ${key}" style="width:${pending ? 0 : val}%"></span></span>
      <span class="comp-val">${pending ? '···' : Math.round(val)}</span>
    </div>`;
  }).join('');

  // The guardrail is the safety story: below threshold, irreversible physical
  // operations are refused. Shown here as the banner the projector would draw.
  const unsafe = !calibrating && v < LOW;
  nodes.guardrail.classList.toggle('hidden', !unsafe);
  if (unsafe) {
    nodes.guardrailText.textContent =
      `Effort ${Math.round(v)} / 100 at ${e.blinkRate?.toFixed(0)} blinks-per-minute. ` +
      `Tool power and destructive project operations are locked until you re-engage, ` +
      `or double-blink to override.`;
  }

  const q = e.contactQuality;
  nodes.contact.textContent = q === undefined ? '—' : `${Math.round(q * 100)}%`;
  nodes.contact.className = 'signal-value ' + (q >= 0.75 ? 'ok' : q >= 0.5 ? '' : 'bad');
}

function renderAttention(a, variants) {
  if (!a) return;
  nodes.saccades.textContent = a.saccades ?? '--';
  nodes.gazeMarker.style.left = `${((a.position + 1) / 2) * 100}%`;

  const focusedId = a.confidence > 0.35 ? a.zone : null;
  nodes.variants.innerHTML = (variants || []).map((v) => {
    const isFocused = v.id === focusedId && v.status !== 'archived';
    const dwell = isFocused ? Math.min(1, a.dwell / 1.2) : 0;
    return `<div class="variant ${isFocused ? 'focused' : ''} ${v.status} ${flashClass(v.id)}" data-id="${v.id}">
      <div class="variant-top">
        <h3>${v.label}</h3>
        <span class="variant-ver">v${v.version}</span>
      </div>
      <p>${v.summary}</p>
      <div class="dwell"><div class="dwell-fill" style="width:${dwell * 100}%"></div></div>
      <div class="variant-foot">
        <span class="attention-time">${v.attention_seconds.toFixed(0)}s attention</span>
        <span class="badge ${v.status}">${v.status}</span>
      </div>
    </div>`;
  }).join('');

  const selected = (variants || []).find((v) => v.id === a.selected);
  nodes.tableCaption.textContent = selected
    ? `Holding on ${selected.label} — double-blink to flag it, jaw-clench to promote.`
    : 'Look at a design. It highlights. No calibration.';
}

function renderEntry(contribution, agent) {
  entries.set(contribution.id, { contribution, agent });
  const empty = nodes.history.querySelector('.empty');
  if (empty) empty.remove();

  const existing = nodes.history.querySelector(`[data-cid="${contribution.id}"]`);
  const effort = contribution.effort === null ? 'n/a' : Math.round(contribution.effort);
  const html = `
    <div class="entry ${contribution.label}" data-cid="${contribution.id}">
      <div class="entry-head">
        <span>${contribution.author} · ${contribution.kind}</span>
        <span class="entry-effort">effort ${effort}${contribution.flagged ? ' · flagged' : ''}</span>
      </div>
      <div class="entry-body">${escapeHtml(contribution.body)}</div>
      ${agent ? `<div class="entry-agent"><span class="regime ${agent.regime}">${agent.regime}</span>
        ${escapeHtml(agent.text)}</div>` : ''}
    </div>`;

  if (existing) existing.outerHTML = html;
  else nodes.history.insertAdjacentHTML('afterbegin', html);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// The variant list is re-rendered at 8 Hz, so a class applied directly to the
// node would be wiped on the next tick. Flashes live here and are re-applied
// during render until they expire.
const flashes = new Map();

function flashVariant(variantId, vote, binding) {
  flashes.set(variantId, {
    cls: `vote-${vote}${binding ? '' : '-held'}`,
    until: Date.now() + 1800,
  });
}

function flashClass(variantId) {
  const flash = flashes.get(variantId);
  if (!flash) return '';
  if (Date.now() > flash.until) { flashes.delete(variantId); return ''; }
  return flash.cls;
}

let toastTimer;
function toast(message) {
  nodes.flagToast.textContent = message;
  nodes.flagToast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => nodes.flagToast.classList.remove('show'), 3200);
}

/* ---------------------------------------------------------- hosted replay */

function replayZoneAt(position) {
  const active = replay.variants.filter((v) => v.status !== 'archived');
  const nearest = active.length
    ? active.reduce((best, variant) =>
      Math.abs(variant.position - position) < Math.abs(best.position - position)
        ? variant : best)
    : null;
  const distance = nearest ? Math.abs(nearest.position - position) : 1;
  return nearest && distance < 0.45 ? nearest.id : null;
}

function replayAttention() {
  const zone = replayZoneAt(replay.gaze);
  const dwell = zone ? (Date.now() - replay.attentionSince) / 1000 : 0;

  return {
    position: replay.gaze,
    zone,
    selected: zone && dwell >= 1.2 ? zone : null,
    confidence: zone ? Math.min(1, 0.45 + dwell / 2) : 0,
    dwell,
    saccades: replay.saccades,
  };
}

function replayEffort() {
  const normalized = replay.effort / 100;
  return {
    effort: replay.effort,
    calibrating: false,
    calibrationProgress: 1,
    blinkRate: 26 - 21 * normalized,
    contactQuality: 1,
    components: {
      ocular: replay.effort,
      cortical: Math.min(100, 42 + replay.effort * 0.55),
      stillness: 92,
    },
  };
}

function renderReplayTick() {
  const now = Date.now();
  const elapsed = Math.min(1, (now - replay.lastTick) / 1000);
  replay.lastTick = now;

  const attention = replayAttention();
  const focused = replay.variants.find((v) => v.id === attention.zone);
  if (focused && attention.confidence > 0.35) {
    focused.attention_seconds += elapsed;
  }

  const effort = replayEffort();
  latest = { effort, attention, variants: replay.variants };
  renderEffort(effort);
  renderAttention(attention, replay.variants);

  nodes.sourceKind.textContent = 'replay · hosted';
  nodes.sourceKind.className = 'signal-value ok';
  nodes.gesture.textContent = 'demo controls';
  nodes.gesture.className = 'signal-value ok';
}

function startReplayMode() {
  if (replayMode) return;
  replayMode = true;
  replay.lastTick = Date.now();
  nodes.simGaze.value = Math.round(replay.gaze * 100);
  nodes.simEffort.value = replay.effort;
  renderReplayTick();
  replayTimer = setInterval(renderReplayTick, 400);
  toast('Hosted replay mode — no local hardware required.');
}

function stopReplayMode() {
  if (!replayMode) return;
  replayMode = false;
  clearInterval(replayTimer);
  replayTimer = null;
  toast('Live bench connected.');
}

function setReplayGaze(value) {
  const next = Math.max(-1, Math.min(1, Number(value)));
  const previousZone = replayZoneAt(replay.gaze);
  const nextZone = replayZoneAt(next);
  if (nextZone !== previousZone) {
    replay.saccades += 1;
    replay.attentionSince = Date.now();
  } else if (Math.abs(next - replay.gaze) > 0.08) {
    replay.saccades += 1;
  }
  replay.gaze = next;
}

function replayContribution(body, status = 'merged', flagged = false) {
  const contribution = {
    id: `replay-${replay.sequence++}`,
    author: 'you',
    body,
    kind: 'decision',
    effort: replay.effort,
    flagged,
    label: flagged ? 'flagged' : labelFor(replay.effort),
    status,
  };
  renderEntry(contribution, null);
  return contribution;
}

function promoteReplayVariant(variantId) {
  const target = replay.variants.find((v) => v.id === variantId);
  if (!target) return { error: 'unknown variant' };
  if (target.status === 'promoted') {
    toast(`${target.label} is already promoted.`);
    return target;
  }

  replay.variants.forEach((variant) => {
    variant.status = variant.id === variantId ? 'promoted' : 'archived';
  });
  target.version += 1;
  replayContribution(`Promoted ${target.label} to the main branch.`, 'merged', true);
  renderReplayTick();
  toast(`${target.label} promoted to main.`);
  return target;
}

function replayPost(path, body) {
  if (path === '/api/sim') {
    if (body.gaze !== undefined) setReplayGaze(body.gaze);
    if (body.effort !== undefined) {
      replay.effort = Math.round(Math.max(0, Math.min(1, Number(body.effort))) * 100);
    }
    renderReplayTick();
    return { effort: replay.effort, gaze: replay.gaze };
  }

  if (path === '/api/sim/blink') {
    replay.flagUntil = Date.now() + 8000;
    toast('Double-blink — deliberate flag recorded.');
    return { ok: true };
  }

  if (path === '/api/sim/clench') {
    const attention = replayAttention();
    if (!attention.zone) {
      toast('Confirm ignored — look at a design first.');
      return { error: 'no active design' };
    }
    return promoteReplayVariant(attention.zone);
  }

  if (path === '/api/agent') {
    const flagged = Date.now() < replay.flagUntil;
    const regime = flagged || replay.effort >= HIGH
      ? 'CONSIDERED'
      : replay.effort < LOW ? 'DIFFUSE' : 'REVIEW';
    const status = regime === 'DIFFUSE' ? 'challenged' : regime === 'REVIEW' ? 'proposed' : 'merged';
    const contribution = replayContribution(body.message, status, flagged);
    const agent = {
      regime,
      text: regime === 'DIFFUSE'
        ? 'Held for review. Which constraint matters most here: thermal, cost, or lead time?'
        : regime === 'REVIEW'
          ? 'Recorded as a proposal. Confirm the affected artifact before merging.'
          : 'Recorded as considered intent and added to the project history.',
    };
    renderEntry(contribution, agent);
    return { contribution, agent };
  }

  const match = path.match(/^\/api\/variants\/([^/]+)\/promote$/);
  if (match) return promoteReplayVariant(match[1]);
  return { error: `unsupported replay action: ${path}` };
}

/* ------------------------------------------------------------- transport */

function connect() {
  if (hostedStatic) {
    startReplayMode();
    return;
  }

  socketHadMessage = false;
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws;
  try {
    ws = new WebSocket(`${protocol}://${location.host}/ws`);
  } catch (_error) {
    startReplayMode();
    return;
  }

  const fallbackTimer = setTimeout(() => {
    if (!socketHadMessage) startReplayMode();
  }, 1800);

  ws.onmessage = (event) => {
    socketHadMessage = true;
    clearTimeout(fallbackTimer);
    stopReplayMode();
    const msg = JSON.parse(event.data);

    if (msg.type === 'tick') {
      latest = { effort: msg.effort, attention: msg.attention, variants: msg.variants };
      renderEffort(msg.effort);
      renderAttention(msg.attention, msg.variants);
      nodes.sourceKind.textContent = `${msg.source.kind} · ${msg.source.status}`;
      nodes.sourceKind.className = 'signal-value ' +
        (msg.source.status === 'streaming' ? 'ok' : msg.source.error ? 'bad' : '');

      const g = msg.gesture?.status ?? 'idle';
      nodes.gesture.textContent = g;
      nodes.gesture.className = 'signal-value ' +
        (g === 'connected' ? 'ok' : g.startsWith('disconnected') ? 'bad' : '');
    }

    if (msg.type === 'flag') {
      const where = latest.variants.find((v) => v.id === msg.flag.zone);
      const how = msg.flag.source === 'jaw_clench' ? 'Jaw clench' : 'Double-blink';
      toast(`${how} — flagged${where ? ` while looking at ${where.label}` : ''}.`);
      if (msg.flag.source === 'jaw_clench' && msg.flag.zone) promote(msg.flag.zone);
    }

    if (msg.type === 'project') {
      (msg.project.contributions || []).forEach((c) => renderEntry(c, null));
    }

    if (msg.type === 'agent_response') {
      renderEntry(msg.contribution, msg.agent);
    }

    if (msg.type === 'promoted') {
      toast(`${msg.variant.label} promoted to main.`);
      latest.variants = msg.project.variants;
    }

    // The hero moment: a thumbs-up carries a verdict but not a subject. The
    // attention estimate names the design, and the effort score decides whether
    // the verdict is binding or merely noted.
    if (msg.type === 'vote') {
      const verb = msg.vote === 'approve' ? 'Approved' : 'Rejected';
      const eff = msg.effort === null ? 'unmeasured' : Math.round(msg.effort);
      flashVariant(msg.variant.id, msg.vote, msg.binding);
      let outcome;
      if (msg.reverted) outcome = `sent back to design (v${msg.reverted.version}) — back to the drawing board`;
      else if (msg.promoted) outcome = 'promoted to main';
      else if (msg.binding) outcome = 'staged for review';
      else outcome = null;
      toast(outcome
        ? `${verb} ${msg.variant.label} — effort ${eff}, ${outcome}.`
        : `${verb} ${msg.variant.label} — but effort ${eff}. Held for review, not merged.`);
      latest.variants = msg.project.variants;
      (msg.project.contributions || []).slice(-1).forEach((c) => renderEntry(c, null));
    }

    if (msg.type === 'vote_unresolved') {
      toast(`Vote ignored — ${msg.reason}.`);
    }
  };

  ws.onerror = () => ws.close();
  ws.onclose = () => {
    clearTimeout(fallbackTimer);
    if (!socketHadMessage) startReplayMode();
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, replayMode ? 5000 : 1200);
  };
}

async function post(path, body) {
  if (replayMode) return replayPost(path, body || {});

  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error(`Request failed: ${res.status}`);
  return res.json();
}

function promote(variantId) {
  post(`/api/variants/${variantId}/promote`);
}

/* --------------------------------------------------------------- controls */

nodes.agentForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const message = nodes.agentInput.value.trim();
  if (!message) return;

  nodes.agentSend.disabled = true;
  nodes.agentSend.textContent = 'Thinking…';
  try {
    await post('/api/agent', { message });
    nodes.agentInput.value = '';
  } finally {
    nodes.agentSend.disabled = false;
    nodes.agentSend.textContent = 'Send at current effort';
  }
});

nodes.simGaze.addEventListener('input', (e) => post('/api/sim', { gaze: e.target.value / 100 }));
nodes.simEffort.addEventListener('input', (e) => post('/api/sim', { effort: e.target.value / 100 }));
el('btn-blink').addEventListener('click', () => post('/api/sim/blink'));
el('btn-clench').addEventListener('click', () => post('/api/sim/clench'));

// Keyboard is the reliable stage path: sliders are hard to hit under lights.
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'TEXTAREA') return;
  const map = {
    ArrowLeft: () => { nodes.simGaze.value = -80; post('/api/sim', { gaze: -0.8 }); },
    ArrowRight: () => { nodes.simGaze.value = 80; post('/api/sim', { gaze: 0.8 }); },
    ArrowUp: () => { nodes.simEffort.value = 92; post('/api/sim', { effort: 0.92 }); },
    ArrowDown: () => { nodes.simEffort.value = 6; post('/api/sim', { effort: 0.06 }); },
    b: () => post('/api/sim/blink'),
    c: () => post('/api/sim/clench'),
  };
  if (map[e.key]) { e.preventDefault(); map[e.key](); }
});

connect();
