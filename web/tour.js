/* The walkthrough. Next, next, next -- across pages.

   Six surfaces, each of which is obvious once someone tells you what it is and
   opaque until then. So this is a tour that survives navigation: the step index
   lives in sessionStorage, a step that belongs to another page navigates there
   and picks up where it left off, and the card reads the same on the cream
   landing page as it does on the black instrument panels.

   It is inert until started. No auto-run, nothing rendered, no listeners that
   matter -- a page with this script and a tour nobody launched is the page
   exactly as it was. That is deliberate: the tour is an addition to six
   finished pages, not a change to them.

   Start it from the landing page, or put ?tour=1 on any URL.  */

(() => {
  const KEY = 'bench.tour.step';
  const path = () => {
    const p = location.pathname.replace(/\/+$/, '') || '/';
    // The same file is reachable under two names on two servers: FastAPI
    // serves /control at / and Netlify rewrites /control to /index.html.
    const alias = {
      '/index.html': '/control', '/': document.body.classList.contains('tour-landing') ? '/' : '/control',
      '/landing.html': '/', '/about': '/', '/surface.html': '/surface',
      '/desk.html': '/desk', '/project.html': '/project',
      '/hold.html': '/hold', '/phone.html': '/phone',
    };
    return alias[p] || p;
  };

  /* ------------------------------------------------------------- the script */

  const STEPS = [
    // --- the front door
    { page: '/', at: '.hero h1', title: 'Bench, in one line',
      body: 'A camera watches the desk. A headband watches the person. Everything either of them '
          + 'notices ends up in a project history an agent can read. This tour walks the six '
          + 'surfaces that make that true — about two minutes.' },
    { page: '/', at: '.launch', title: 'Everything here is simulated',
      body: 'No page on this host is talking to hardware, and every one of them says so. When '
          + 'nothing answers on the WebSocket, the pages fall back to a browser-side bench that '
          + 'speaks the identical event stream — so what you are about to see is the real UI on '
          + 'fake sensors, not a mockup.' },
    { page: '/', at: '.grid3', title: 'Why three sensors and not one',
      body: 'A thumbs-up is a verdict with no subject. Attention supplies the subject. Effort '
          + 'decides whether the verdict binds at all — approve while focused and the design is '
          + 'promoted; approve while diffuse and it is recorded but held.' },

    // --- the table
    { page: '/surface', at: '#hud', title: 'Boxic Surface — the table',
      body: 'This is the projector\'s view of a desk: a mapped surface with physical objects on '
          + 'it and project files living in the same space. It boots into its own sequence — let '
          + 'it run behind this card.' },
    { page: '/surface', at: '#physLayer', title: 'What the camera sees',
      body: 'Each detected object gets a dashed box, corner tracking marks and a label placed '
          + 'beside it — never on top of it, because the projector would be drawing on the object '
          + 'itself. Drag one; the label follows.' },
    { page: '/surface', at: '#digiLayer', title: 'Files are objects too',
      body: 'CAD, boards and notes sit on the table next to the physical things, carrying version, '
          + 'status, owner and comment count. Drag a file onto a physical object and the two are '
          + 'linked — that mug is now part of the record as an ergonomic reference.' },
    { page: '/surface', at: '#viewer', title: 'Review, in the room',
      body: 'Selecting a file opens it for everyone at the table. Orbit it, explode it, section '
          + 'it, and pin a comment to the geometry itself — so the note lives on the part rather '
          + 'than in somebody\'s inbox.' },
    { page: '/surface', at: '#boxieBtn', title: 'Ask the project',
      body: 'Boxie answers from what is on the table and what has happened to it: why a version '
          + 'changed, what is blocking a supplier, which option was actually considered.' },

    // --- the operator's screen
    { page: '/control', at: '.panel-left', title: 'The operator, measured',
      body: 'Effort is 60% blink rate, 25% posterior alpha, 15% stillness — and the panel names '
          + 'each contribution rather than showing one mystical number. Below 35 the guardrail '
          + 'engages and destructive operations lock.' },
    { page: '/control', at: '#table', title: 'Look at a design, it highlights',
      body: 'No calibration step, because every quantity is a contrast between two electrodes '
          + 'rather than a level: fast eye movement catches the switch, alpha lateralisation holds '
          + 'the position after the ocular signal has decayed.' },
    { page: '/control', at: '.stage', title: 'Drive it yourself',
      body: 'Arrow keys beat sliders under stage lights: ← → move the gaze, ↑ ↓ move the effort, '
          + 'b double-blinks to flag, c records a jaw-clench flag. Push effort down and watch the agent '
          + 'change its mind about what you asked it.' },
    { page: '/control', at: '#pairing', title: 'Your phone is the desk camera',
      body: 'Scan the code and the phone streams frames the bench runs the same detector over — '
          + 'the same one the overhead webcam gets. Nothing downstream can tell which camera it '
          + 'has. On this host the code opens the pairing page; a real bench points it at its own '
          + 'LAN address.' },
    { page: '/control', at: '#agent-form', title: 'The gate',
      body: 'The same sentence gets a different answer depending on who you were when you wrote '
          + 'it. Focused: it acts and merges. Diffuse: it refuses to touch the project and asks '
          + 'you one pointed question instead.' },

    // --- the second screen
    { page: '/desk', at: '#desk-feed', title: 'The desk view',
      body: 'What the audience watches: the camera frame with the overlay drawn on top. Boxes '
          + 'track the objects, the attended one glows, and the ring fills as a look is held.' },
    { page: '/desk', at: '#s-desk', title: 'Hands, on whichever camera',
      body: 'MediaPipe landmarks run on the desk feed too. The index fingertip answers which '
          + 'design you are touching — a far more direct referent than inferring it from gaze — '
          + 'and handling time is recorded separately from looking time.' },

    // --- the payoff
    { page: '/project', at: '#versions', title: 'The record',
      body: 'Every design carries the seconds it was actually looked at, so the history shows '
          + 'which option was genuinely considered rather than only which one won.' },
    { page: '/project', at: '#commits', title: 'Every entry is stamped',
      body: 'This is the column a software repo does not have: the measured state of the person '
          + 'who wrote each entry. A BOM swap made at 3am while tab-completing is not the same '
          + 'evidence as one that was flagged and defended.' },
    { page: '/project', at: '#ask-input', title: 'Talk to the record',
      body: 'Reads are never gated on effort — someone who has lost the thread asking "walk me '
          + 'through this" is the system working. Ask it which design was actually considered.' },

    // --- the part in your hand
    { page: '/hold', at: '#rail', title: 'Hold it',
      body: 'A viewport cannot answer "is 31 mm too thick to hold". Pick a part, show your palm '
          + 'to the camera, and it is seated in your hand at the size it will actually be.' },
    { page: '/hold', at: '#stage', title: 'Real scale, real pose, real occlusion',
      body: 'Millimetres come from your own knuckle span, not a slider. The part sits on a basis '
          + 'built from wrist and knuckles so it rolls with your hand, and the camera\'s own pixels '
          + 'are redrawn over your fingers so the hand closes over it.' },

    // --- out
    { page: '/hold', at: null, title: 'That is the loop',
      body: 'The desk says what. The headband says how much it mattered. The gesture says the '
          + 'verdict. The record keeps all three, and the agent reads them. Everything you just '
          + 'saw runs on a laptop with a headband and two cameras — the recording of that is '
          + 'linked from the front page.', end: true },
  ];

  /* ------------------------------------------------------------------ chrome */

  const CSS = `
  .tour-veil{position:fixed;inset:0;z-index:99998;background:rgba(8,7,12,.62);
    backdrop-filter:blur(1.5px);animation:tourIn .22s ease}
  .tour-ring{position:fixed;z-index:99999;border:3px solid #FF7454;border-radius:14px;
    box-shadow:0 0 0 4000px rgba(8,7,12,.62),0 0 34px rgba(255,116,84,.55);
    pointer-events:none;transition:all .34s cubic-bezier(.22,.61,.36,1)}
  .tour-card{position:fixed;z-index:100000;width:min(400px,calc(100vw - 32px));
    background:#191520;color:#FFF3E4;border:3px solid #FF7454;border-radius:20px;
    padding:20px 22px 18px;box-shadow:0 10px 0 rgba(0,0,0,.45);
    font:15px/1.6 'Nunito','Segoe UI',system-ui,sans-serif;animation:tourIn .22s ease}
  @keyframes tourIn{from{opacity:0;transform:translateY(8px)}}
  .tour-step{font:800 11px/1 'Funnel Display',system-ui,sans-serif;letter-spacing:.16em;
    text-transform:uppercase;color:#FF9B85;margin-bottom:9px}
  .tour-title{font:800 21px/1.2 'Funnel Display',system-ui,sans-serif;margin-bottom:9px}
  .tour-body{color:#D9D2E0;font-size:14.5px}
  .tour-row{display:flex;align-items:center;gap:9px;margin-top:17px}
  .tour-btn{font:700 14px 'Funnel Display',system-ui,sans-serif;cursor:pointer;
    border:2.5px solid #FFF3E4;background:transparent;color:#FFF3E4;border-radius:999px;
    padding:8px 16px;transition:transform .12s,box-shadow .12s}
  .tour-btn.go{background:#FF7454;border-color:#FF7454;color:#fff;box-shadow:0 4px 0 #8f2f1c}
  .tour-btn.go:hover{transform:translateY(2px);box-shadow:0 2px 0 #8f2f1c}
  .tour-btn.ghost{border:0;color:#9C93AE;padding:8px 6px;margin-left:auto;font-weight:600}
  .tour-btn.ghost:hover{color:#FFF3E4}
  .tour-dots{display:flex;gap:4px;margin-top:14px}
  .tour-dots i{height:3px;flex:1;background:rgba(255,243,228,.18);border-radius:2px}
  .tour-dots i.on{background:#FF7454}
  .tour-start{position:fixed;right:18px;bottom:18px;z-index:9998}
  @media (max-width:620px){.tour-card{left:16px!important;right:16px;bottom:16px!important;top:auto!important}}`;

  let i = 0, veil, ring, card;

  const save = (n) => { try { sessionStorage.setItem(KEY, n); } catch (_) {} };
  const load = () => { try { return sessionStorage.getItem(KEY); } catch (_) { return null; } };
  const clear = () => { try { sessionStorage.removeItem(KEY); } catch (_) {} };

  function styleOnce() {
    if (document.getElementById('tour-css')) return;
    const s = document.createElement('style');
    s.id = 'tour-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function place(step) {
    const el = step.at ? document.querySelector(step.at) : null;
    const box = el ? el.getBoundingClientRect() : null;

    if (box && box.width && box.height && box.bottom > 0 && box.top < innerHeight) {
      ring.style.display = 'block';
      ring.style.left = `${box.left - 6}px`;
      ring.style.top = `${box.top - 6}px`;
      ring.style.width = `${box.width + 12}px`;
      ring.style.height = `${box.height + 12}px`;
      veil.style.display = 'none';        // the ring's huge shadow is the veil

      // Put the card wherever the highlight is not.
      const w = Math.min(400, innerWidth - 32);
      const right = box.right + 20 + w < innerWidth;
      const left = box.left - 20 - w > 0;
      card.style.width = `${w}px`;
      if (right) { card.style.left = `${box.right + 18}px`; card.style.right = 'auto'; }
      else if (left) { card.style.left = `${box.left - w - 18}px`; card.style.right = 'auto'; }
      else { card.style.left = `${Math.max(16, Math.min(box.left, innerWidth - w - 16))}px`; }

      const below = box.bottom + 20 + 240 < innerHeight;
      card.style.top = below ? `${box.bottom + 16}px`
                             : `${Math.max(16, Math.min(box.top, innerHeight - 300))}px`;
    } else {
      // No anchor on this page (or it is off screen): centre it.
      ring.style.display = 'none';
      veil.style.display = 'block';
      card.style.width = `${Math.min(430, innerWidth - 32)}px`;
      card.style.left = '50%';
      card.style.top = '50%';
      card.style.transform = 'translate(-50%,-50%)';
      return;
    }
    card.style.transform = 'none';
  }

  function render() {
    const step = STEPS[i];
    const dots = STEPS.map((_, n) => `<i class="${n <= i ? 'on' : ''}"></i>`).join('');
    card.innerHTML = `
      <div class="tour-step">Step ${i + 1} of ${STEPS.length}</div>
      <div class="tour-title"></div>
      <div class="tour-body"></div>
      <div class="tour-row">
        ${i ? '<button class="tour-btn" data-tour="back">← Back</button>' : ''}
        <button class="tour-btn go" data-tour="next">${step.end ? 'Finish' : 'Next →'}</button>
        <button class="tour-btn ghost" data-tour="skip">${step.end ? '' : 'Skip tour'}</button>
      </div>
      <div class="tour-dots">${dots}</div>`;
    // textContent, not innerHTML: the copy is data, and this way it can never
    // inject markup into six pages it does not own.
    card.querySelector('.tour-title').textContent = step.title;
    card.querySelector('.tour-body').textContent = step.body;
    place(step);
  }

  function goto(n) {
    if (n < 0) return;
    if (n >= STEPS.length) return stop();
    const step = STEPS[n];
    save(n);
    if (step.page !== path()) {          // the next stop is on another surface
      location.href = step.page;
      return;
    }
    if (!card) { start(n); return; }     // first step: build the chrome first
    i = n;
    render();
  }

  function stop() {
    clear();
    [veil, ring, card].forEach((n) => n && n.remove());
    veil = ring = card = null;
    removeEventListener('resize', onMove);
    removeEventListener('scroll', onMove, true);
    removeEventListener('keydown', onKey);
  }

  const onMove = () => card && place(STEPS[i]);
  const onKey = (e) => {
    if (!card) return;
    if (e.key === 'ArrowRight' || e.key === 'Enter') { e.preventDefault(); goto(i + 1); }
    if (e.key === 'ArrowLeft') { e.preventDefault(); goto(i - 1); }
    if (e.key === 'Escape') stop();
  };

  function start(at) {
    styleOnce();
    veil = document.createElement('div'); veil.className = 'tour-veil';
    ring = document.createElement('div'); ring.className = 'tour-ring';
    card = document.createElement('div'); card.className = 'tour-card';
    document.body.append(veil, ring, card);

    card.addEventListener('click', (e) => {
      const what = e.target.closest('[data-tour]')?.dataset.tour;
      if (what === 'next') goto(i + 1);
      if (what === 'back') goto(i - 1);
      if (what === 'skip') stop();
    });
    veil.addEventListener('click', stop);
    addEventListener('resize', onMove);
    addEventListener('scroll', onMove, true);
    addEventListener('keydown', onKey);

    i = at;
    render();
  }

  /* -------------------------------------------------------------- launching */

  window.BenchTour = { start: () => goto(0), stop };

  function boot() {
    const params = new URLSearchParams(location.search);
    if (params.get('tour') === '1') { goto(0); return; }

    const saved = parseInt(load(), 10);
    if (!Number.isNaN(saved) && STEPS[saved]) {
      // Resume only on the surface this step belongs to, so a stale index from a
      // closed tab cannot pop a card onto an unrelated page.
      if (STEPS[saved].page === path()) {
        // Give the page a moment to build itself -- surface and hold both draw
        // their layout in script, and an anchor measured too early is a zero box.
        setTimeout(() => start(saved), 700);
      }
    }
  }

  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', boot);
  else boot();
})();
