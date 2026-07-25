/* Consumes the semantic event stream from /ws and draws the bench.
   The page never touches EEG itself -- it renders `effort`, `attention` and
   `flag` events, which is why swapping the simulator for a real headband (or a
   recorded replay) changes nothing here. */

const LOW = 35, HIGH = 65;

const el = (id) => document.getElementById(id);
const nodes = {
  effortValue: el('effort-value'), effortLabel: el('effort-label'), effortFill: el('effort-fill'),
  components: el('components'), blinkRate: el('blink-rate'), saccades: el('saccades'),
  sourceKind: el('source-kind'), contact: el('contact'),
  guardrail: el('guardrail'), guardrailText: el('guardrail-text'),
  variants: el('variants'), gazeMarker: el('gaze-marker'), tableCaption: el('table-caption'),
  flagToast: el('flag-toast'), history: el('history'),
  agentForm: el('agent-form'), agentInput: el('agent-input'), agentSend: el('agent-send'),
  simGaze: el('sim-gaze'), simEffort: el('sim-effort'),
};

let latest = { effort: null, attention: null, variants: [] };
const entries = new Map();

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
    return `<div class="variant ${isFocused ? 'focused' : ''} ${v.status}" data-id="${v.id}">
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

let toastTimer;
function toast(message) {
  nodes.flagToast.textContent = message;
  nodes.flagToast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => nodes.flagToast.classList.remove('show'), 3200);
}

/* ------------------------------------------------------------- transport */

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);

    if (msg.type === 'tick') {
      latest = { effort: msg.effort, attention: msg.attention, variants: msg.variants };
      renderEffort(msg.effort);
      renderAttention(msg.attention, msg.variants);
      nodes.sourceKind.textContent = `${msg.source.kind} · ${msg.source.status}`;
      nodes.sourceKind.className = 'signal-value ' +
        (msg.source.status === 'streaming' ? 'ok' : msg.source.error ? 'bad' : '');
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
  };

  ws.onclose = () => setTimeout(connect, 1200);
}

async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
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
