/* Bench without a bench.

   The real event stream comes from a Python process on a laptop wired to a
   headband and two cameras. On a static host there is no such process, and a
   control view hanging on a dead socket is worse than no demo at all. So this
   file is another event source: it speaks the same `tick` / `desk` / `vote` /
   `flag` messages the server speaks, and the pages consume it without knowing
   the difference. That is the same decoupling the backend was built around --
   "nothing downstream knows whether a headband, a phone, or the simulator
   produced them" -- extended one hop further, into the browser.

   It activates only when nothing answers: the shim opens the real socket
   first, and falls back after a second and a bit of silence. Run the bench
   locally and this file stays out of the way entirely.

       ?demo=1   simulate without trying the real socket first
       ?live=1   never simulate; fail like production would

   The simulation is honest about what it is. Every page it drives says DEMO in
   the corner, because a fake effort score presented as a real one would be the
   one dishonest thing in this project. */

(() => {
  const params = new URLSearchParams(location.search);
  if (params.get('live') === '1') return;

  const FORCE = params.get('demo') === '1';
  // Set once anything proves there is no bench on this host, so the probe
  // happens once per page rather than once per request.
  let noBench = FORCE;
  const PROBE_MS = 1400;          // how long a real bench gets to answer
  const LOW = 35, HIGH = 65;
  const TICK_HZ = 8;

  /* ------------------------------------------------------------- project */

  const project = {
    name: 'Thermal Camera Rev C',
    description: 'Handheld thermal imager. Lepton 3.5 core, custom carrier PCB, '
      + 'injection-moulded enclosure.',
    artifacts: [
      ['art_0001', 'enclosure_rev_c.step', 'step', 'Two-part ABS shell, 118 x 64 x 31 mm, M2 bosses.'],
      ['art_0002', 'carrier_board.kicad_pcb', 'gerber', '4-layer carrier, Lepton socket, USB-C PD.'],
      ['art_0003', 'thermal_sim.inp', 'sim', 'Steady-state thermal, 2.1 W core dissipation.'],
      ['art_0004', 'bom.csv', 'bom', '41 line items, 3 single-sourced.'],
      ['art_0005', 'firmware/main.c', 'firmware', 'STM32H7 capture loop, 9 Hz frame rate.'],
    ].map(([id, name, kind, summary]) => ({
      id, name, kind, summary, version: 1, updated_at: Date.now() / 1000,
    })),
    variants: [
      {
        id: 'var_0006', label: 'Shell A', position: -0.65, version: 1, status: 'candidate',
        summary: 'Wraparound grip, 31 mm thick, single-shot mould, 2 mm walls.',
        attention_seconds: 0, notes: [],
      },
      {
        id: 'var_0007', label: 'Shell B', position: 0.65, version: 1, status: 'candidate',
        summary: 'Slab back with vented fin, 27 mm thick, side-action tool required.',
        attention_seconds: 0, notes: [],
      },
    ],
    contributions: [],
    stats: {},
    thresholds: { low: LOW, high: HIGH },
  };

  let nextId = 8;
  const id = (prefix) => `${prefix}_${String(nextId++).padStart(4, '0')}`;

  function restat() {
    const merged = project.contributions.filter((c) => c.status === 'merged').length;
    const scored = project.contributions.filter((c) => c.effort !== null);
    project.stats = {
      totalContributions: project.contributions.length,
      merged,
      challenged: project.contributions.filter((c) => c.status === 'challenged').length,
      flagged: project.contributions.filter((c) => c.flagged).length,
      averageEffort: scored.length
        ? Math.round(scored.reduce((a, c) => a + c.effort, 0) / scored.length * 10) / 10 : null,
      artifacts: project.artifacts.length,
    };
  }

  function label(effort, flagged) {
    if (flagged) return 'flagged';
    if (effort === null) return 'unknown';
    if (effort < LOW) return 'diffuse';
    if (effort >= HIGH) return 'focused';
    return 'neutral';
  }

  function contribute({ body, kind, effort, flagged = false, status, variantId = null }) {
    const c = {
      id: id('c'), author: 'you', body, kind, effort, flagged,
      weight: flagged ? 0.95 : Math.round(Math.min(1, Math.max(0.05, effort / 100)) * 1000) / 1000,
      label: label(effort, flagged), status,
      created_at: Date.now() / 1000, artifact_id: null, variant_id: variantId,
      agent_response: null,
    };
    project.contributions.push(c);
    restat();
    return c;
  }

  restat();

  /* ---------------------------------------------------------------- state */

  const sim = {
    effortTarget: 78, effort: 78,
    gazeTarget: -0.65, gaze: -0.65,
    zone: null, dwell: 0, selected: null, saccades: 0,
    blinkRate: 8.4,
    lastTouched: 0,
    frames: 0,
  };

  const sockets = new Set();      // every open shim, on every page in this tab

  function emit(message) {
    const raw = JSON.stringify(message);
    sockets.forEach((s) => s.deliver(raw));
  }

  const noise = (amp) => (Math.random() - 0.5) * 2 * amp;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  function step(dt) {
    // Effort eases towards its target with a little jitter, so the meter looks
    // measured rather than set. Blink rate is the inverse relationship the
    // README documents: ~5/min absorbed, ~26/min diffuse.
    sim.effort = clamp(sim.effort + (sim.effortTarget - sim.effort) * 0.09 + noise(0.7), 0, 100);
    sim.blinkRate = clamp(26 - (sim.effort / 100) * 20 + noise(0.8), 3, 30);

    const before = sim.gaze;
    sim.gaze += (sim.gazeTarget - sim.gaze) * 0.22;
    if (Math.abs(sim.gaze - before) > 0.05) sim.saccades += 1;

    const live = project.variants.filter((v) => v.status !== 'archived');
    let nearest = null, best = 0.5;
    live.forEach((v) => {
      const d = Math.abs(v.position - sim.gaze);
      if (d < best) { best = d; nearest = v; }
    });

    if (nearest && nearest.id === sim.zone) {
      sim.dwell += dt;
    } else {
      sim.dwell = 0;
      sim.zone = nearest ? nearest.id : null;
    }
    sim.selected = sim.dwell > 1.2 ? sim.zone : null;
    if (sim.zone) {
      const v = project.variants.find((x) => x.id === sim.zone);
      if (v) v.attention_seconds = Math.round((v.attention_seconds + dt) * 10) / 10;
    }
  }

  function effortPayload() {
    const e = sim.effort;
    return {
      timestamp: Date.now() / 1000,
      effort: Math.round(e * 10) / 10,
      components: {
        ocular: Math.round(clamp(e + noise(4), 0, 100)),
        cortical: Math.round(clamp(e * 0.86 + 8 + noise(5), 0, 100)),
        stillness: Math.round(clamp(e * 0.5 + 46 + noise(3), 0, 100)),
      },
      blinkRate: Math.round(sim.blinkRate * 10) / 10,
      bands: { alpha: 0.42, beta: 0.21, theta: 0.18 },
      contactQuality: 0.93,
      speaking: false,
      moving: false,
      calibrating: false,
      calibrationProgress: 1,
      artifact: null,
    };
  }

  function attentionPayload() {
    const confidence = sim.zone ? clamp(0.55 + sim.dwell * 0.2, 0, 0.97) : 0.1;
    return {
      timestamp: Date.now() / 1000,
      position: Math.round(sim.gaze * 1000) / 1000,
      zone: sim.zone,
      dwell: Math.round(sim.dwell * 100) / 100,
      selected: sim.selected,
      confidence: Math.round(confidence * 1000) / 1000,
      heog: Math.round(sim.gaze * -42 * 10) / 10,
      ocularPosition: Math.round(sim.gaze * 0.8 * 1000) / 1000,
      alphaLateralisation: Math.round(sim.gaze * 0.6 * 1000) / 1000,
      alphaPosition: Math.round(sim.gaze * 1000) / 1000,
      saccades: sim.saccades,
    };
  }

  /* ------------------------------------------------------------ desk feed */

  /* The desk view draws bounding boxes over a camera frame, so the demo has to
     produce an actual frame -- there is no camera here, so one gets painted.
     The objects it paints are at the same positions it reports, which is the
     only thing that makes the overlay land where it should. */

  const canvas = document.createElement('canvas');
  canvas.width = 960; canvas.height = 540;
  const ctx = canvas.getContext('2d', { alpha: false });
  let t = 0;

  function paintDesk() {
    t += 1 / TICK_HZ;
    ctx.fillStyle = '#15171c';
    ctx.fillRect(0, 0, 960, 540);
    ctx.strokeStyle = 'rgba(255,255,255,0.03)';
    ctx.lineWidth = 1;
    for (let x = 0; x < 960; x += 48) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, 540); ctx.stroke(); }
    for (let y = 0; y < 540; y += 48) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(960, y); ctx.stroke(); }

    const objects = [];
    project.variants.filter((v) => v.status !== 'archived').forEach((v, i) => {
      const drift = Math.sin(t * 0.5 + i) * 9;
      const cx = ((v.position + 1) / 2) * 960 + drift;
      const w = i === 0 ? 188 : 208, h = i === 0 ? 178 : 158;
      const x = cx - w / 2, y = 190 + Math.cos(t * 0.4 + i) * 7;

      const grad = ctx.createLinearGradient(x, y, x, y + h);
      grad.addColorStop(0, '#b9bec8');
      grad.addColorStop(1, '#8c93a0');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.roundRect(x, y, w, h, i === 0 ? 26 : 12);
      ctx.fill();
      ctx.strokeStyle = 'rgba(255,255,255,0.35)';
      ctx.lineWidth = 2;
      ctx.stroke();

      objects.push({
        id: `obj_${i + 1}`, label: `Object ${i + 1}`,
        bbox: [Math.round(x), Math.round(y), w, h],
        area: w * h,
        position: Math.round(((cx / 960) * 2 - 1) * 1000) / 1000,
        positionY: Math.round((((y + h / 2) / 540) * 2 - 1) * 1000) / 1000,
        cx: Math.round(cx * 10) / 10,
        cy: Math.round((y + h / 2) * 10) / 10,
      });
    });

    sim.frames += 1;
    return { objects, jpeg: canvas.toDataURL('image/jpeg', 0.5).split(',', 2)[1] };
  }

  /* --------------------------------------------------------------- events */

  function flag(source) {
    emit({
      type: 'flag',
      flag: {
        source, timestamp: Date.now() / 1000, zone: sim.zone,
        effort: Math.round(sim.effort),
        gap_ms: source === 'double_blink' ? 284 : null,
      },
    });
  }

  function promote(variantId) {
    const v = project.variants.find((x) => x.id === variantId);
    if (!v) return null;
    v.status = 'promoted';
    v.version += 1;
    project.variants.forEach((o) => { if (o.id !== v.id && o.status !== 'archived') o.status = 'archived'; });
    return v;
  }

  function revert(variantId) {
    const v = project.variants.find((x) => x.id === variantId);
    if (!v) return null;
    v.status = 'redesign';
    return v;
  }

  function vote(kind) {
    const variant = sim.zone ? project.variants.find((v) => v.id === sim.zone) : null;
    if (!variant) {
      emit({ type: 'vote_unresolved', vote: kind,
             reason: 'no design was being attended to when the vote landed' });
      return;
    }
    const effort = Math.round(sim.effort);
    const binding = effort >= LOW;
    const decisive = effort >= HIGH;

    let promoted = null, reverted = null;
    if (decisive && kind === 'approve') promoted = promote(variant.id);
    if (decisive && kind === 'reject') reverted = revert(variant.id);

    let body = `${kind === 'approve' ? 'Approved' : 'Rejected'} ${variant.label} by gesture `
      + 'while attending to it (94% gesture confidence).';
    if (reverted) body += ' Sent back to design — back to the drawing board.';
    if (!binding) body += ' Operator was diffuse — held for review rather than merged.';

    contribute({ body, kind: 'decision', effort, status: binding ? 'merged' : 'challenged',
                 variantId: variant.id });

    emit({
      type: 'vote', vote: kind, variant: { ...variant }, effort, binding, decisive,
      promoted: promoted ? { ...promoted } : null,
      reverted: reverted ? { ...reverted } : null,
      project,
    });
  }

  // Mirrors backend/agent.py's offline responder, including the gate: the same
  // sentence gets a different answer depending on the measured state.
  function agentReply(message, effort, flagged) {
    const regime = (flagged || (effort !== null && effort >= HIGH)) ? 'CONSIDERED'
      : (effort !== null && effort < LOW) ? 'DIFFUSE' : 'REVIEW';
    const words = message.replace(/\s+/g, ' ').trim().replace(/\.$/, '').split(' ');
    const subject = words.slice(0, 8).join(' ').toLowerCase() + (words.length > 8 ? '…' : '');

    const text = regime === 'DIFFUSE'
      ? `Holding off on ${subject} — the request is underspecified as written, and there `
        + 'are three merged decisions it would contradict.\n\nOne question before I touch '
        + 'anything: what is the constraint driving this — thermal, cost, or lead time?\n\n'
        + "If you meant it as stated, double-blink to flag it and I'll apply it as-is."
      : regime === 'REVIEW'
        ? `Applied to ${subject} and staged for review.\n\nLeast certain assumption: that `
          + 'this supersedes the earlier decision on the same artifact rather than sitting '
          + "alongside it. Flag it if that's wrong."
        : `Done — ${subject} updated and merged.\n\nConsequence for the build: this touches `
          + 'the thermal path, so `thermal_sim.inp` is now stale and the BOM has one fewer '
          + 'single-sourced line.';

    return { regime, text, acted: regime !== 'DIFFUSE', effort, flagged };
  }

  // The read path: the same summary the server's offline responder gives, built
  // from measured dwell rather than from what anyone said they preferred.
  function answerPayload() {
    const considered = [...project.variants]
      .sort((a, b) => b.attention_seconds - a.attention_seconds)[0];
    const lines = [
      `${project.name} — ${project.description}`, '',
      `${project.stats.totalContributions} recorded decisions, ${project.stats.merged} merged, `
      + `${project.stats.challenged} challenged. Average operator effort `
      + `${project.stats.averageEffort ?? 'n/a'}.`, '',
      'Designs on the table:',
      ...project.variants.map((v) => `  · ${v.label} (v${v.version}, ${v.status}) — `
        + `${v.attention_seconds.toFixed(0)}s of measured attention. ${v.summary}`),
    ];
    if (considered && considered.attention_seconds > 0) {
      lines.push('', `Most considered: ${considered.label}, by measured dwell.`);
    }
    return { regime: 'ANSWER', text: lines.join('\n'), acted: false,
             effort: null, flagged: false };
  }

  /* ------------------------------------------------------------- autopilot */

  /* Nobody is at the keyboard on a shared link, so the demo drives itself: a
     loop that makes the argument the project exists to make -- the same
     approve, at two different effort levels, lands differently. Any manual
     input suspends it for a quarter of a minute. */

  const script = [
    { at: 0, run: () => { sim.effortTarget = 88; sim.gazeTarget = -0.65; } },
    { at: 6, run: () => flag('double_blink') },
    { at: 9, run: () => vote('approve') },
    { at: 15, run: () => { sim.gazeTarget = 0.65; } },
    { at: 20, run: () => { sim.effortTarget = 14; } },
    { at: 26, run: () => vote('approve') },
    { at: 33, run: () => { sim.effortTarget = 84; } },
    { at: 40, run: () => { sim.gazeTarget = -0.65; } },
  ];
  const LOOP_S = 48;
  let clock = 0, cursor = 0;

  function autopilot(dt) {
    if (Date.now() - sim.lastTouched < 15000) return;
    clock += dt;
    while (cursor < script.length && clock >= script[cursor].at) script[cursor++].run();
    if (clock >= LOOP_S) {
      clock = 0; cursor = 0;
      // Reset the table so the loop can make its point again.
      project.variants.forEach((v, i) => {
        v.status = 'candidate';
        v.attention_seconds = 0;
        v.position = i === 0 ? -0.65 : 0.65;
      });
      project.contributions = [];
      restat();
      emit({ type: 'project', project });
    }
  }

  const touched = () => { sim.lastTouched = Date.now(); };
  document.addEventListener('keydown', touched, true);
  document.addEventListener('pointerdown', touched, true);

  /* ------------------------------------------------------------- the loop */

  let running = false;

  function start() {
    if (running) return;
    running = true;
    const dt = 1 / TICK_HZ;
    setInterval(() => {
      if (!sockets.size) return;
      step(dt);
      autopilot(dt);

      const desk = paintDesk();
      emit({
        type: 'desk', origin: 'phone', calibrated: true,
        objects: desk.objects, width: 960, height: 540, jpeg: desk.jpeg,
      });
      emit({
        type: 'tick',
        effort: effortPayload(),
        attention: attentionPayload(),
        variants: project.variants.map((v) => ({ ...v })),
        source: { kind: 'demo', status: 'streaming', error: null },
        gesture: { status: 'connected' },
        desk: { status: 'phone', calibrated: true, objects: desk.objects.length, origin: 'phone' },
        phone: {
          status: 'streaming', live: true, calibrated: true,
          objects: desk.objects.length, frames: sim.frames, device: 'demo', error: null,
        },
      });
    }, 1000 / TICK_HZ);
  }

  /* ----------------------------------------------------------- the shims */

  function markPage() {
    if (document.getElementById('demo-badge')) return;
    const badge = document.createElement('div');
    badge.id = 'demo-badge';
    badge.innerHTML = '<b>DEMO</b> simulated bench · '
      + '<a href="/">what this is</a>';
    badge.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:9999;'
      + 'font:11px/1.4 ui-monospace,SF Mono,Menlo,monospace;letter-spacing:.04em;'
      + 'background:rgba(16,19,25,0.92);border:1px solid #232936;border-radius:999px;'
      + 'padding:7px 13px;color:#8b95a8;backdrop-filter:blur(6px)';
    badge.querySelector('b').style.cssText = 'color:#45e0c0;letter-spacing:.1em';
    badge.querySelector('a').style.cssText = 'color:#8b95a8';
    document.body.appendChild(badge);
  }

  const RealWebSocket = window.WebSocket;

  class BenchSocket {
    /* Tries the real bench first and forwards it verbatim; if nothing answers,
       becomes the simulator. The page assigns onmessage/onclose as usual and
       never learns which of the two it got. */

    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.onopen = this.onmessage = this.onclose = this.onerror = null;
      this._real = null;
      this._simulating = false;

      if (FORCE) { setTimeout(() => this._simulate(), 0); return; }

      try {
        this._real = new RealWebSocket(url);
      } catch {
        setTimeout(() => this._simulate(), 0);
        return;
      }

      const probe = setTimeout(() => {
        if (this.readyState !== 1) { this._drop(); this._simulate(); }
      }, PROBE_MS);

      this._real.onopen = (e) => {
        clearTimeout(probe);
        this.readyState = 1;
        this.onopen?.(e);
      };
      this._real.onmessage = (e) => this.onmessage?.(e);
      this._real.onerror = (e) => { if (!this._simulating) this.onerror?.(e); };
      this._real.onclose = (e) => {
        clearTimeout(probe);
        if (this._simulating) return;
        // Never opened? There is no bench here. Opened and then dropped? That
        // is a real bench going away, and the page should reconnect as usual.
        if (this.readyState === 1) { this.readyState = 3; this.onclose?.(e); }
        else this._simulate();
      };
    }

    _drop() {
      if (!this._real) return;
      this._real.onopen = this._real.onmessage = this._real.onclose = this._real.onerror = null;
      try { this._real.close(); } catch { /* already gone */ }
      this._real = null;
    }

    _simulate() {
      if (this._simulating) return;
      this._simulating = true;
      noBench = true;
      this._drop();
      this.readyState = 1;

      // The phone socket is a different protocol and a different lie: pretending
      // frames are reaching a bench would be untrue in a way the others are not,
      // so it is left unconnected and the page says so.
      if (/\/ws\/phone/.test(this.url)) { this.readyState = 3; this.onclose?.({}); return; }

      markPage();
      sockets.add(this);
      start();
      this.onopen?.({});
      this.deliver(JSON.stringify({ type: 'project', project }));
    }

    deliver(raw) {
      this.onmessage?.({ data: raw });
    }

    send() { /* the pages are send-only for keepalive; nothing to do */ }

    close() {
      this._drop();
      sockets.delete(this);
      this.readyState = 3;
    }
  }

  BenchSocket.CONNECTING = 0; BenchSocket.OPEN = 1;
  BenchSocket.CLOSING = 2; BenchSocket.CLOSED = 3;
  window.WebSocket = BenchSocket;

  /* REST. The real endpoint is tried first and wins whenever it answers, so a
     bench on this host serves its own API as normal; only a 404 or a dead host
     -- neither of which any real bench produces -- falls through to the
     simulated reply. Probing per request rather than waiting for the socket to
     decide matters because the pairing panel fetches on page load, well before
     a WebSocket has had time to time out. */

  const realFetch = window.fetch.bind(window);
  const json = (body) => new Response(JSON.stringify(body),
    { status: 200, headers: { 'Content-Type': 'application/json' } });

  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    if (!url.startsWith('/api/')) return realFetch(input, init);

    if (!noBench) {
      try {
        const res = await realFetch(input, init);
        // Anything that answers successfully is a real bench and wins. A static
        // host answers these with 404/405/501, and a dead one throws. Either
        // way the verdict is remembered, so the console collects one failed
        // request rather than one per interaction.
        if (res.ok) return res;
        if ([404, 405, 501].includes(res.status)) noBench = true;
        else return res;
      } catch { noBench = true; }
    }

    touched();
    const body = init?.body ? JSON.parse(init.body) : {};
    const path = url.split('?')[0];

    if (path === '/api/project') return json(project);

    if (path === '/api/pair') {
      return json({
        url: `${location.origin}/phone`,
        ip: location.hostname, port: location.port || (location.protocol === 'https:' ? 443 : 80),
        secure: location.protocol === 'https:', token: 'demo',
        cameraAllowed: location.protocol === 'https:', demo: true,
      });
    }

    if (path === '/api/sim') {
      if (body.effort !== undefined) sim.effortTarget = body.effort * 100;
      if (body.gaze !== undefined) sim.gazeTarget = body.gaze;
      return json({ effort: sim.effortTarget / 100, gaze: sim.gazeTarget });
    }

    if (path === '/api/sim/blink') { flag('double_blink'); return json({ ok: true }); }

    if (path === '/api/sim/clench') {
      flag('jaw_clench');
      return json({ ok: true });
    }

    if (path === '/api/desk/calibrate') return json({ ok: true, source: 'demo' });

    // "ask" reads the project and is not gated or recorded; "change" writes and
    // is. Same split as the server, for the same reason: logging every question
    // as a decision would corrupt the record this exists to keep.
    if (path === '/api/agent' && body.intent === 'ask') {
      const answer = answerPayload();
      emit({ type: 'agent_answer', contribution: null, agent: answer, question: body.message });
      return json({ contribution: null, agent: answer, question: body.message });
    }

    if (path === '/api/agent') {
      const effort = body.override_effort ?? Math.round(sim.effort);
      const result = agentReply(body.message, effort, body.flagged || false);
      const contribution = contribute({
        body: body.message, kind: 'change', effort, flagged: body.flagged || false,
        status: result.regime === 'DIFFUSE' ? 'challenged' : 'merged',
      });
      contribution.agent_response = result.text;
      const payload = { contribution, agent: result };
      emit({ type: 'agent_response', ...payload });
      return json(payload);
    }

    const promoteMatch = path.match(/^\/api\/variants\/([^/]+)\/promote$/);
    if (promoteMatch) {
      const variant = promote(promoteMatch[1]);
      if (!variant) return json({ error: 'unknown variant' });
      contribute({ body: `Promoted ${variant.label} to the main branch.`, kind: 'decision',
                   effort: Math.round(sim.effort), flagged: true, status: 'merged',
                   variantId: variant.id });
      emit({ type: 'promoted', variant: { ...variant }, project });
      return json({ ...variant });
    }

    return json({ error: 'not available in the demo' });
  };
})();
