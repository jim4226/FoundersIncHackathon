/* Dependency-free CAD geometry + renderer.
 *
 * The models are built parametrically in *millimetres* -- not decorative art --
 * because the whole point of putting a part in someone's hand is that the size
 * is real. Shell A is 118 x 64 x 31 mm here for the same reason it is in
 * enclosure_rev_c.step, so when the palm frame is scaled from the operator's own
 * knuckle span the render lands at 1:1 against their fingers.
 *
 * No Three.js, no WebGL, no CDN: a painter's-algorithm renderer over 2D canvas.
 * That is a deliberate constraint -- this file has to run inside a sandboxed
 * preview page with a strict CSP as happily as it runs on the bench.
 *
 *   BenchCAD.models          -> { id: model }
 *   BenchCAD.render(ctx, model, frame, opts)
 *
 * `frame` is expressed in ordinary screen pixels (y down); the renderer handles
 * the flip to a right-handed space internally so callers never think about it.
 */
(function (global) {
  'use strict';

  /* ------------------------------------------------------------ vector ops */

  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const cross = (a, b) => [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0],
  ];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const norm = (a) => {
    const l = Math.hypot(a[0], a[1], a[2]) || 1;
    return [a[0] / l, a[1] / l, a[2] / l];
  };
  const scale = (a, k) => [a[0] * k, a[1] * k, a[2] * k];
  const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];

  /* ----------------------------------------------------------- mesh builder */

  function Mesh() {
    this.v = [];   // [x, y, z] in mm, model space
    this.f = [];   // { i: [vertex indices, CCW seen from outside], m: material }
  }

  Mesh.prototype.push = function (pts) {
    const base = this.v.length;
    for (const p of pts) this.v.push(p);
    return base;
  };

  /* `anchor` overrides the point used for depth sorting. A painter's algorithm
     sorts by face centroid, which breaks for a decal lying on a much larger
     face: the display glass sits 0.35 mm proud of the top cap, but it is also
     offset 14 mm along the body, and once the part is tilted that offset moves
     its centroid further from the camera than the cap it sits on -- so the cap
     paints over it. Sorting the decal at its host's centre instead fixes the
     ordering without a depth buffer. */
  Mesh.prototype.face = function (idx, mat, anchor) {
    this.f.push({ i: idx, m: mat, a: anchor });
  };

  /* Axis-aligned box centred at (cx, cy, cz). */
  Mesh.prototype.box = function (c, size, mat) {
    const [cx, cy, cz] = c;
    const [w, h, d] = size;
    const x0 = cx - w / 2, x1 = cx + w / 2;
    const y0 = cy - h / 2, y1 = cy + h / 2;
    const z0 = cz - d / 2, z1 = cz + d / 2;
    const b = this.push([
      [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
      [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
    ]);
    const f = (a, bb, c2, d2) => this.face([b + a, b + bb, b + c2, b + d2], mat);
    f(0, 3, 2, 1);   // -z
    f(4, 5, 6, 7);   // +z
    f(0, 1, 5, 4);   // -y
    f(1, 2, 6, 5);   // +x
    f(2, 3, 7, 6);   // +y
    f(3, 0, 4, 7);   // -x
    return this;
  };

  /* A rounded-rectangle profile in XY, counter-clockwise seen from +z. */
  function roundedRect(w, h, r, seg) {
    r = Math.min(r, w / 2, h / 2);
    const x = w / 2 - r, y = h / 2 - r;
    const pts = [];
    const corners = [[x, y, 0], [-x, y, Math.PI / 2], [-x, -y, Math.PI], [x, -y, -Math.PI / 2]];
    for (const [cx, cy, a0] of corners) {
      for (let i = 0; i <= seg; i++) {
        const a = a0 + (i / seg) * (Math.PI / 2);
        pts.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]);
      }
    }
    return pts;
  }

  /* Extrude a profile through a stack of {z, k} sections (k scales the profile
     about its centre, which is how the chamfers on a moulded edge are made). */
  Mesh.prototype.extrude = function (profile, sections, mat, capMat) {
    const n = profile.length;
    const rings = sections.map((s) =>
      this.push(profile.map(([x, y]) => [x * (s.kx ?? s.k ?? 1), y * (s.ky ?? s.k ?? 1), s.z])));

    for (let r = 0; r < rings.length - 1; r++) {
      for (let i = 0; i < n; i++) {
        const j = (i + 1) % n;
        this.face([rings[r] + i, rings[r] + j, rings[r + 1] + j, rings[r + 1] + i], mat);
      }
    }
    const bottom = rings[0], top = rings[rings.length - 1];
    const cm = capMat || mat;
    this.face(Array.from({ length: n }, (_, i) => bottom + (n - 1 - i)), cm);  // -z, reversed
    this.face(Array.from({ length: n }, (_, i) => top + i), cm);               // +z
    return this;
  };

  /* Cylinder between two points. Used for lens barrels and can caps. */
  Mesh.prototype.cylinder = function (from, to, r, mat, seg) {
    seg = seg || 24;
    const axis = norm(sub(to, from));
    let ref = Math.abs(axis[2]) < 0.9 ? [0, 0, 1] : [1, 0, 0];
    const u = norm(cross(ref, axis));
    const v = cross(axis, u);

    const ring = (o) => this.push(Array.from({ length: seg }, (_, i) => {
      const a = (i / seg) * Math.PI * 2;
      return add(o, add(scale(u, r * Math.cos(a)), scale(v, r * Math.sin(a))));
    }));
    const a0 = ring(from), a1 = ring(to);
    for (let i = 0; i < seg; i++) {
      const j = (i + 1) % seg;
      this.face([a0 + i, a0 + j, a1 + j, a1 + i], mat);
    }
    this.face(Array.from({ length: seg }, (_, i) => a0 + (seg - 1 - i)), mat);
    this.face(Array.from({ length: seg }, (_, i) => a1 + i), mat);
    return this;
  };

  /* A flat panel parallel to XY -- display glass, silkscreen, copper pour.
     Always a decal on the face below it, so it sorts on the body's own axis. */
  Mesh.prototype.panel = function (c, size, mat, r) {
    const [w, h] = size;
    const prof = r ? roundedRect(w, h, r, 4) : [
      [w / 2, h / 2], [-w / 2, h / 2], [-w / 2, -h / 2], [w / 2, -h / 2],
    ];
    const base = this.push(prof.map(([x, y]) => [c[0] + x, c[1] + y, c[2]]));
    this.face(Array.from({ length: prof.length }, (_, i) => base + i), mat, [0, 0, c[2]]);
    return this;
  };

  /* ------------------------------------------------------------- materials */

  const MAT = {
    shellDark:  { c: [58, 66, 82],   spec: 0.30 },
    shellLight: { c: [92, 103, 124], spec: 0.22 },
    rubber:     { c: [30, 34, 43],   spec: 0.08 },
    glass:      { c: [14, 30, 34],   spec: 0.45, emit: [8, 44, 40] },
    lens:       { c: [20, 26, 34],   spec: 0.55, emit: [6, 28, 27] },
    metal:      { c: [150, 158, 172], spec: 0.75 },
    accent:     { c: [46, 150, 130], spec: 0.50, emit: [10, 34, 30] },
    pcb:        { c: [22, 82, 70],   spec: 0.25 },
    pcbEdge:    { c: [16, 60, 52],   spec: 0.20 },
    gold:       { c: [201, 162, 39], spec: 0.80 },
    ic:         { c: [26, 28, 33],   spec: 0.35 },
    passive:    { c: [70, 62, 58],   spec: 0.30 },
  };

  /* ---------------------------------------------------------------- models */

  /* Shell A -- the wraparound-grip enclosure. Body is a single-shot moulding,
     so the whole outer surface is one chamfered extrusion; the grip pad and the
     lens boss are the only things that break it. */
  function shellA() {
    const m = new Mesh();
    const W = 118, H = 64, D = 31, c = 1.6;
    const prof = roundedRect(W, H, 11, 5);
    m.extrude(prof, [
      { z: -D / 2,     kx: (W - 2 * c) / W, ky: (H - 2 * c) / H },
      { z: -D / 2 + c, k: 1 },
      { z:  D / 2 - c, k: 1 },
      { z:  D / 2,     kx: (W - 2 * c) / W, ky: (H - 2 * c) / H },
    ], MAT.shellDark, MAT.shellLight);

    // Display: recessed bezel, then glass sitting just proud of it.
    m.panel([-14, 1, D / 2 + 0.05], [78, 46], MAT.rubber, 3);
    m.panel([-14, 1, D / 2 + 0.35], [72, 40], MAT.glass, 2);

    // Lens boss on the leading edge -- this is the end you point at things.
    m.cylinder([26, -H / 2 + 2, 0], [26, -H / 2 - 7, 0], 12, MAT.shellLight, 28);
    m.cylinder([26, -H / 2 - 7.01, 0], [26, -H / 2 - 8.5, 0], 9.5, MAT.lens, 28);

    // Wraparound grip pad + ribs on the back.
    m.box([34, 0, -D / 2 - 1.2], [40, 58, 3], MAT.rubber);
    for (let i = 0; i < 4; i++) m.box([22 + i * 8, 0, -D / 2 - 2.6], [3, 50, 1.6], MAT.shellDark);

    // Shutter button and status LED on the top face.
    m.cylinder([40, 22, D / 2], [40, 22, D / 2 + 2.2], 6, MAT.accent, 20);
    m.cylinder([40, 8, D / 2], [40, 8, D / 2 + 0.6], 1.8, MAT.metal, 12);

    return {
      id: 'shell_a',
      label: 'Shell A',
      part: 'enclosure_rev_c.step',
      kind: 'step',
      mm: [W, H, D],
      mass: '214 g',
      summary: 'Wraparound grip, 31 mm thick, single-shot mould, 2 mm walls.',
      mount: 'grip',
      mesh: m,
      annotations: [
        { kind: 'dim', from: [-W / 2, -H / 2 - 10, D / 2], to: [W / 2, -H / 2 - 10, D / 2], label: '118 mm' },
        { kind: 'dim', from: [-W / 2 - 9, -H / 2, -D / 2], to: [-W / 2 - 9, -H / 2, D / 2], label: '31 mm' },
        { kind: 'note', at: [34, 0, -D / 2 - 2.6], label: 'wraparound grip · 2 mm wall' },
        { kind: 'note', at: [26, -H / 2 - 8.5, 0], label: 'Lepton 3.5 aperture', dy: 34 },
      ],
    };
  }

  /* Shell B -- slab back with a vented fin stack. Thinner and sharper than A,
     and it needs a side-action tool, which is the trade the fins buy. */
  function shellB() {
    const m = new Mesh();
    const W = 118, H = 64, D = 27, c = 1.0;
    const prof = roundedRect(W, H, 4, 3);
    m.extrude(prof, [
      { z: -D / 2,     kx: (W - 2 * c) / W, ky: (H - 2 * c) / H },
      { z: -D / 2 + c, k: 1 },
      { z:  D / 2 - c, k: 1 },
      { z:  D / 2,     kx: (W - 2 * c) / W, ky: (H - 2 * c) / H },
    ], MAT.shellDark, MAT.shellLight);

    m.panel([-12, 1, D / 2 + 0.05], [84, 50], MAT.rubber, 2);
    m.panel([-12, 1, D / 2 + 0.35], [80, 46], MAT.glass, 1.5);

    m.cylinder([28, -H / 2 + 2, 0], [28, -H / 2 - 6, 0], 12, MAT.shellLight, 28);
    m.cylinder([28, -H / 2 - 6.01, 0], [28, -H / 2 - 7.4, 0], 9.5, MAT.lens, 28);

    // The vented fin stack: seven fins, 3 mm pitch, running front to back.
    for (let i = 0; i < 7; i++) {
      m.box([-30 + i * 9, 0, -D / 2 - 2.5], [4, 52, 5], MAT.shellLight);
    }
    // Parting line the side-action tool leaves down each flank.
    m.panel([0, -H / 2 - 0.01, 0], [W - 10, 0.8], MAT.shellDark);

    m.cylinder([44, 22, D / 2], [44, 22, D / 2 + 2.2], 6, MAT.accent, 20);

    return {
      id: 'shell_b',
      label: 'Shell B',
      part: 'enclosure_rev_c.step',
      kind: 'step',
      mm: [W, H, D],
      mass: '189 g',
      summary: 'Slab back with vented fin, 27 mm thick, side-action tool required.',
      mount: 'grip',
      mesh: m,
      annotations: [
        { kind: 'dim', from: [-W / 2, -H / 2 - 10, D / 2], to: [W / 2, -H / 2 - 10, D / 2], label: '118 mm' },
        { kind: 'dim', from: [-W / 2 - 9, -H / 2, -D / 2], to: [-W / 2 - 9, -H / 2, D / 2], label: '27 mm' },
        { kind: 'note', at: [-30, 0, -D / 2 - 5], label: '7 × fin · 9 mm pitch', side: 'left' },
        { kind: 'note', at: [0, -H / 2, 0], label: 'side-action parting line', dy: 30 },
      ],
    };
  }

  /* The carrier board. This one lies flat *on* the palm rather than being
     gripped -- a bare PCB is a thing you present, not a thing you hold. */
  function carrier() {
    const m = new Mesh();
    const W = 96, H = 54, T = 1.6;
    const prof = roundedRect(W, H, 3, 3);
    m.extrude(prof, [{ z: -T / 2, k: 1 }, { z: T / 2, k: 1 }], MAT.pcbEdge, MAT.pcb);

    // Lepton socket: shrouded connector with the sensor aperture in the middle.
    m.box([-24, 4, T / 2 + 2.8], [21, 21, 5.6], MAT.ic);
    m.cylinder([-24, 4, T / 2 + 5.6], [-24, 4, T / 2 + 6.4], 5.2, MAT.lens, 20);

    // STM32H7 QFN, DDR, and the USB-C PD receptacle on the near edge.
    m.box([8, -6, T / 2 + 0.6], [12, 12, 1.2], MAT.ic);
    m.box([26, 8, T / 2 + 0.5], [9, 9, 1.0], MAT.ic);
    m.box([34, -H / 2 + 3.4, T / 2 + 1.6], [9, 7.3, 3.2], MAT.metal);

    // Bulk caps, a crystal, and a row of 0402 passives.
    m.cylinder([-4, 18, T / 2], [-4, 18, T / 2 + 5.4], 2.6, MAT.metal, 16);
    m.cylinder([4, 18, T / 2], [4, 18, T / 2 + 5.4], 2.6, MAT.metal, 16);
    m.box([18, 16, T / 2 + 0.5], [3.2, 2.5, 1.0], MAT.metal);
    for (let i = 0; i < 9; i++) m.box([-2 + i * 4, -18, T / 2 + 0.25], [1.0, 0.5, 0.5], MAT.passive);
    for (let i = 0; i < 6; i++) m.box([-40, -14 + i * 5, T / 2 + 0.25], [0.5, 1.0, 0.5], MAT.passive);

    // Gold: castellated edge pads and four mounting rings.
    for (let i = 0; i < 12; i++) m.panel([-38 + i * 6.5, H / 2 - 1.6, T / 2 + 0.02], [3, 2.6], MAT.gold);
    for (const [x, y] of [[-42, 22], [42, 22], [-42, -22], [42, -22]]) {
      m.cylinder([x, y, -T / 2 - 0.02], [x, y, T / 2 + 0.02], 2.6, MAT.gold, 14);
    }

    return {
      id: 'carrier',
      label: 'Carrier board',
      part: 'carrier_board.kicad_pcb',
      kind: 'gerber',
      mm: [W, H, T],
      mass: '31 g',
      summary: '4-layer carrier, Lepton socket, USB-C PD.',
      mount: 'flat',
      mesh: m,
      annotations: [
        { kind: 'dim', from: [-W / 2, -H / 2 - 8, T / 2], to: [W / 2, -H / 2 - 8, T / 2], label: '96 mm' },
        { kind: 'note', at: [-24, 4, T / 2 + 6.4], label: 'Lepton 3.5 socket', side: 'left' },
        { kind: 'note', at: [34, -H / 2 + 3.4, T / 2 + 3.2], label: 'USB-C PD' },
      ],
    };
  }

  /* ---------------------------------------------------------- silhouettes */

  /* An edge is on the silhouette when the polygon on the other side of it faces
     away (or there is no other side). Adjacency is cached per mesh and stored
     *per face*, so the outline can be stroked inside the depth-sorted draw loop
     -- otherwise a rib on the far side of the part would paint its glowing
     outline straight through the body in front of it. */
  function faceEdges(mesh) {
    if (mesh._fedges) return mesh._fedges;
    const owners = new Map();
    mesh.f.forEach((face, fi) => {
      const n = face.i.length;
      for (let k = 0; k < n; k++) {
        const a = face.i[k], b = face.i[(k + 1) % n];
        const key = a < b ? `${a},${b}` : `${b},${a}`;
        const list = owners.get(key);
        if (list) list.push(fi); else owners.set(key, [fi]);
      }
    });
    mesh._fedges = mesh.f.map((face, fi) => {
      const n = face.i.length;
      const out = [];
      for (let k = 0; k < n; k++) {
        const a = face.i[k], b = face.i[(k + 1) % n];
        const key = a < b ? `${a},${b}` : `${b},${a}`;
        const list = owners.get(key);
        const other = list.length === 2 ? (list[0] === fi ? list[1] : list[0]) : -1;
        out.push({ a, b, other });
      }
      return out;
    });
    return mesh._fedges;
  }

  /* Newell's method. The three-point cross product is wrong here: a chamfered
     corner puts several consecutive vertices on the same small arc, so the first
     three are near-collinear and the normal comes out as numerical noise. */
  function faceNormal(P, idx) {
    let nx = 0, ny = 0, nz = 0;
    for (let k = 0; k < idx.length; k++) {
      const a = P[idx[k]], b = P[idx[(k + 1) % idx.length]];
      nx += (a[1] - b[1]) * (a[2] + b[2]);
      ny += (a[2] - b[2]) * (a[0] + b[0]);
      nz += (a[0] - b[0]) * (a[1] + b[1]);
    }
    return norm([nx, ny, nz]);
  }

  /* -------------------------------------------------------------- lighting */

  const LIGHT = norm([-0.42, 0.62, 0.66]);   // over the operator's left shoulder
  const VIEW = [0, 0, 1];
  const HALF = norm([LIGHT[0], LIGHT[1], LIGHT[2] + 1]);

  function shade(mat, n, opts) {
    const lambert = Math.max(0, dot(n, LIGHT));
    // Flat facets make a tight specular lobe all-or-nothing: a face either
    // catches the whole highlight and washes out, or none of it. Kept broad and
    // weak so it reads as sheen across an edge rather than a white panel.
    const spec = (mat.spec || 0) * Math.pow(Math.max(0, dot(n, HALF)), 7);
    const facing = Math.abs(n[2]);
    // Rim term: the part is lit by a projector in a dark room, so the grazing
    // edges pick up the accent rather than going black.
    const rim = Math.pow(1 - facing, 2.6) * (0.35 + 0.45 * opts.energy);
    const glow = opts.glowRGB;
    const base = mat.c;
    const emit = mat.emit || [0, 0, 0];

    const mix = (i) =>
      base[i] * (0.26 + 0.80 * lambert) +
      emit[i] * (0.55 + 0.45 * opts.energy) +
      96 * spec +
      glow[i] * rim;

    return `rgb(${Math.min(255, mix(0)) | 0},${Math.min(255, mix(1)) | 0},${Math.min(255, mix(2)) | 0})`;
  }

  /* ---------------------------------------------------------------- render */

  const DEFAULT_GLOW = [69, 224, 192];

  function hexToRgb(hex) {
    if (Array.isArray(hex)) return hex;
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ''));
    if (!m) return DEFAULT_GLOW;
    const v = parseInt(m[1], 16);
    return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
  }

  /* frame: { ox, oy, u, v, s }
   *   ox, oy  palm origin in canvas pixels
   *   u       across-palm unit vector, [x, y(down), z(toward viewer)]
   *   v       along-palm unit vector, fingertip-ward
   *   s       pixels per millimetre, from the operator's own knuckle span
   *
   * The caller works entirely in screen coordinates. Inside, y is flipped so the
   * basis is right-handed, the normal is derived rather than supplied, and it is
   * forced toward the camera -- so the part always sits on the visible face of
   * the hand, whichever way round the hand is turned.
   */
  function render(ctx, model, frame, opts) {
    opts = opts || {};
    const energy = opts.energy === undefined ? 0.6 : Math.max(0, Math.min(1, opts.energy));
    const glowRGB = hexToRgb(opts.glow || DEFAULT_GLOW);
    const alpha = opts.alpha === undefined ? 1 : opts.alpha;
    if (alpha <= 0.01) return;

    // Flip into a right-handed space (y up), orthonormalise, derive the normal.
    let u = norm([frame.u[0], -frame.u[1], frame.u[2]]);
    let v = [frame.v[0], -frame.v[1], frame.v[2]];
    v = norm(sub(v, scale(u, dot(v, u))));
    let n = cross(u, v);
    if (n[2] < 0) { u = scale(u, -1); n = cross(u, v); }

    const s = frame.s;
    const O = [frame.ox, -frame.oy, 0];

    // Seat the part on the palm: gripped things tilt toward the operator and
    // ride in the hollow of the hand; flat things simply lie on the surface.
    const tilt = model.mount === 'flat' ? 0 : (opts.tilt === undefined ? -0.32 : opts.tilt);
    const lift = (opts.lift === undefined ? model.mm[2] / 2 + 2 : opts.lift);
    const spin = opts.spin || 0;

    const ct = Math.cos(tilt), st = Math.sin(tilt);
    const cs = Math.cos(spin), ss = Math.sin(spin);

    // One transform, used for geometry, sort anchors and annotation leaders --
    // so a callout cannot drift away from the feature it points at.
    const toCam = ([x, y, z]) => {
      // spin about the palm normal, then tilt about the across-palm axis
      [x, y] = [x * cs - y * ss, x * ss + y * cs];
      [y, z] = [y * ct - z * st, y * st + z * ct];
      z += lift;
      return [
        O[0] + s * (u[0] * x + v[0] * y + n[0] * z),
        O[1] + s * (u[1] * x + v[1] * y + n[1] * z),
        O[2] + s * (u[2] * x + v[2] * y + n[2] * z),
      ];
    };

    // Weak perspective anchored at the palm, so near edges swell a little.
    const D = 1100;
    const project = (p) => {
      const k = D / Math.max(120, D - (p[2] - O[2]));
      return [O[0] + (p[0] - O[0]) * k, O[1] + (p[1] - O[1]) * k, p[2]];
    };

    const P = model.mesh.v.map(toCam);
    const proj = P.map(project);
    const sx = (p) => p[0];
    const sy = (p) => -p[1];

    // Face normals and depths, in the unprojected space so shading stays stable.
    const faces = model.mesh.f;
    const info = faces.map((f) => {
      const nrm = faceNormal(P, f.i);
      let z;
      if (f.a) {
        z = toCam(f.a)[2] + 0.01;   // decal: sort on its host's axis, just above
      } else {
        z = 0;
        for (const idx of f.i) z += P[idx][2];
        z /= f.i.length;
      }
      return { nrm, z, front: nrm[2] > 0 };
    });
    const fedges = faceEdges(model.mesh);

    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.lineJoin = 'round';

    if (opts.shadow !== false) drawShadow(ctx, proj, sx, sy, alpha);

    const order = faces.map((_, i) => i).filter((i) => info[i].front || opts.wire);
    order.sort((a, b) => info[a].z - info[b].z);

    const glowStroke = `rgba(${glowRGB[0]},${glowRGB[1]},${glowRGB[2]},${0.40 + 0.45 * energy})`;

    for (const fi of order) {
      const f = faces[fi], d = info[fi];
      ctx.beginPath();
      const pts = f.i.map((idx) => proj[idx]);
      ctx.moveTo(sx(pts[0]), sy(pts[0]));
      for (let k = 1; k < pts.length; k++) ctx.lineTo(sx(pts[k]), sy(pts[k]));
      ctx.closePath();

      if (opts.wire) {
        ctx.strokeStyle = d.front
          ? `rgba(${glowRGB[0]},${glowRGB[1]},${glowRGB[2]},0.75)`
          : `rgba(${glowRGB[0]},${glowRGB[1]},${glowRGB[2]},0.16)`;
        ctx.lineWidth = d.front ? 1 : 0.6;
        ctx.stroke();
        continue;
      }

      ctx.fillStyle = shade(f.m, d.nrm, { energy, glowRGB });
      ctx.fill();
      // Faint facet lines: this is what makes it read as CAD and not as a toy.
      ctx.strokeStyle = 'rgba(0,0,0,0.28)';
      ctx.lineWidth = 0.5;
      ctx.stroke();

      // Outline only where this face meets open space or a back-facing one.
      const sil = fedges[fi].filter((e) => e.other < 0 || !info[e.other].front);
      if (!sil.length) continue;
      ctx.save();
      ctx.strokeStyle = glowStroke;
      ctx.lineWidth = 1.4 + 1.2 * energy;
      ctx.shadowColor = `rgba(${glowRGB[0]},${glowRGB[1]},${glowRGB[2]},0.85)`;
      ctx.shadowBlur = 8 + 12 * energy;
      ctx.beginPath();
      for (const e of sil) {
        const a = proj[e.a], b = proj[e.b];
        ctx.moveTo(sx(a), sy(a));
        ctx.lineTo(sx(b), sy(b));
      }
      ctx.stroke();
      ctx.restore();
    }

    if (opts.dims !== false && model.annotations) {
      const place = (pt) => { const p = project(toCam(pt)); return [sx(p), sy(p)]; };
      drawAnnotations(ctx, model, place, glowRGB);
    }

    ctx.restore();
  }

  function drawShadow(ctx, proj, sx, sy, alpha) {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const p of proj) {
      const x = sx(p), y = sy(p);
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    const rx = (maxX - minX) * 0.52, ry = (maxY - minY) * 0.46;
    const g = ctx.createRadialGradient(cx, cy + ry * 0.25, 1, cx, cy + ry * 0.25, Math.max(rx, ry));
    g.addColorStop(0, `rgba(0,0,0,${0.42 * alpha})`);
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.save();
    ctx.globalAlpha = 1;
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.ellipse(cx, cy + ry * 0.25, Math.max(rx, ry), Math.max(rx, ry) * 0.72, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  /* Annotation anchors live in model space and go through the identical
     transform as the geometry, so a callout stays welded to its feature. */
  function drawAnnotations(ctx, model, place, glow) {
    const stroke = `rgba(${glow[0]},${glow[1]},${glow[2]},0.62)`;
    ctx.save();
    ctx.font = '500 11px ui-monospace, SFMono-Regular, Menlo, monospace';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 1;

    for (const a of model.annotations) {
      if (a.kind === 'dim') {
        const p0 = place(a.from), p1 = place(a.to);
        ctx.strokeStyle = stroke;
        ctx.beginPath();
        ctx.moveTo(p0[0], p0[1]);
        ctx.lineTo(p1[0], p1[1]);
        ctx.stroke();
        // End ticks, perpendicular to the dimension line.
        const dx = p1[0] - p0[0], dy = p1[1] - p0[1];
        const l = Math.hypot(dx, dy) || 1;
        const px = (-dy / l) * 4, py = (dx / l) * 4;
        ctx.beginPath();
        ctx.moveTo(p0[0] - px, p0[1] - py); ctx.lineTo(p0[0] + px, p0[1] + py);
        ctx.moveTo(p1[0] - px, p1[1] - py); ctx.lineTo(p1[0] + px, p1[1] + py);
        ctx.stroke();
        label(ctx, a.label, (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2, glow, true);
      } else {
        const p = place(a.at);
        const dir = a.side === 'left' ? -1 : 1;
        const ex = p[0] + 26 * dir, ey = p[1] + (a.dy === undefined ? -22 : a.dy);
        ctx.strokeStyle = stroke;
        ctx.beginPath();
        ctx.moveTo(p[0], p[1]);
        ctx.lineTo(ex, ey);
        ctx.lineTo(ex + 10 * dir, ey);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(p[0], p[1], 2, 0, Math.PI * 2);
        ctx.fillStyle = stroke;
        ctx.fill();
        ctx.textAlign = dir > 0 ? 'left' : 'right';
        label(ctx, a.label, ex + 14 * dir, ey, glow, false);
      }
    }
    ctx.restore();
  }

  function label(ctx, text, x, y, glow, centred) {
    const w = ctx.measureText(text).width;
    const lx = centred ? x - w / 2 : (ctx.textAlign === 'right' ? x - w : x);
    ctx.fillStyle = 'rgba(8,9,12,0.78)';
    ctx.fillRect(lx - 5, y - 9, w + 10, 18);
    ctx.strokeStyle = `rgba(${glow[0]},${glow[1]},${glow[2]},0.28)`;
    ctx.lineWidth = 1;
    ctx.strokeRect(lx - 5, y - 9, w + 10, 18);
    ctx.fillStyle = `rgb(${glow[0]},${glow[1]},${glow[2]})`;
    const saved = ctx.textAlign;
    ctx.textAlign = 'left';
    ctx.fillText(text, lx, y + 0.5);
    ctx.textAlign = saved;
  }

  /* ------------------------------------------------------------- projects */

  const models = {};
  for (const build of [shellA, shellB, carrier]) {
    const m = build();
    models[m.id] = m;
  }

  /* One project for now. The shape is the shape the bench already uses -- a
     project with parts, each mapped to a variant id when one exists -- so
     adding the second project is data, not code. */
  const projects = [{
    id: 'thermal_rev_c',
    name: 'Thermal Camera Rev C',
    description: 'Handheld thermal imager. Lepton 3.5 core, custom carrier PCB, injection-moulded enclosure.',
    parts: [
      { model: 'shell_a', variantLabel: 'Shell A' },
      { model: 'shell_b', variantLabel: 'Shell B' },
      { model: 'carrier', variantLabel: null },
    ],
  }];

  global.BenchCAD = { models, projects, render, MAT, Mesh, roundedRect };
})(typeof window !== 'undefined' ? window : globalThis);
