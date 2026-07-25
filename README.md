# Bench

**The workbench knows what you're looking at, and how hard you're thinking.**

Physical products get built in a mess. CAD lives in two different packages, the
STEP files are on someone's desktop, the feedback is in email, and two weeks
later nobody remembers why the wall thickness changed. Software solved this with
git and then handed it to agents. Hardware never got either.

Bench is the missing input layer. An overhead camera-projector watches the desk
and draws on it. An EEG headband watches the person. Together they turn what
happens at a physical workbench into a project history an agent can actually
reason over.

---

## The three pieces

| Piece | What it does | Status |
|---|---|---|
| **Desk live view** | Prism's overhead camera + projector. Sees what's on the table, recognises objects, projects the UI back down onto the surface. | Needs Prism SDK — see *Open questions* |
| **Control view** | The screen version of the desk. Lay out what's on it, add design variants, drive the demo. | ✅ Built |
| **Operator state** | Muse EEG. Effort level, and a deliberate flag gesture. | ✅ Built and tested |
| **Gesture** | MediaPipe thumbs-up/down → `design_vote` on `ws://localhost:8765`. | ✅ Built (`gesture/`) |
| **Hold it** | The CAD part rendered into your own hand at 1:1, on the webcam. | ✅ Built (`/hold`) |
| **Boxic** | The living project record for hardware. Where history, versions and decisions actually live. | ✅ Mapped (`backend/boxic.py`) — needs one write tool |

### How the three inputs divide the work

This is the answer to "why do you need all of this?", and each part is doing a
job the others structurally cannot:

> **Gesture** says *what the verdict is.* 👍 approve, 👎 reject.
> **Attention** says *what it applies to.* A thumbs-up alone doesn't name a design.
> **Effort** says *whether it binds.* Nodding along is not deciding.

An approve while focused promotes the design. The same approve while diffuse is
recorded but held for review. That distinction is invisible to a camera and
trivial for the headband — which is the cleanest statement of why the EEG earns
its place on the desk.

---

## The idea in one paragraph

You put two printed design variants on the desk. Prism sees them and projects a
label under each. You look at one — **it highlights.** You didn't press anything
and you never calibrated anything. Meanwhile the headband is tracking how engaged
you are. Type a lazy request to the agent while you're diffuse and it refuses and
asks you a question instead. Type a considered one while you're locked in and it
acts and merges. Double-blink to flag something as deliberate and override the
meter. At the end of the session you have a project history where every entry
carries the state of the person who wrote it — which is training data no
software repo has ever had.

---

## The look: Boxic Surface

The front end is built in the **Boxic Surface** visual language, because the
bench *is* the Boxic surface — the desk half of the same product, and the place
its project record gets written from.

The governing idea is that the interface is **projected light on a table**.
Black is not a background colour, it is the absence of projection, so everything
visible is light the projector is actually casting. That has consequences that
look like mistakes if you don't know the rule:

- **No filled cards, and no rounded corners.** Structure is carried by
  hairlines, rules and corner tracking marks. A design on the table is a
  *detection* — a rule down its left edge and a label beside it — and the one
  you are attending to gets projected corner brackets, not a highlight.
- **Section headings are a label plus a rule** running to the edge of the
  column, which is the single most characteristic device in the system.
- **Type does the work.** Funnel Display, heavily tracked and uppercase, for
  names; JetBrains Mono for everything else, including body text.

| Token | | Used for |
|---|---|---|
| `--blue` | `#A9C7D6` | primary projection — measurement, focus, dwell, the part in your hand |
| `--red` | `#FF7454` | attention and human decision — gaze marker, flags, the selected part |
| `--yellow` | `#F4B52A` | held or guarded — the effort guardrail, votes cast while diffuse |
| `--ok` | `#8FBF9F` | merged, promoted, connected |
| `--cream` | `#F2EDE4` | type, on black |

Both typefaces are **inlined as base64** in `web/fonts.css` rather than linked
from Google Fonts. A linked font CDN fails silently to Arial, which is the
failure you notice last — and it fails exactly when the venue wifi does. 65 KB
buys a front end with no network dependency at all.

The CAD renderer follows the same split the surface makes between an object and
the light cast on it: **materials stay physical** — a soldermask is green
because the board is green — while the *projected* layer (glow, silhouette,
dimension callouts) carries the palette above.

---

## Hold it — the part in your hand, at 1:1

`http://localhost:8000/hold`

Show your palm to the camera and the part is *in it*. Not a thumbnail beside the
video — seated on your hand, rolling as your hand rolls, at the size it will
actually be.

Three things earn that claim:

> **Scale is real.** Pixels-per-millimetre is derived from the operator's own
> index-to-pinky knuckle span, not from a slider. A 118 mm enclosure is 118 mm
> against their fingers.
> **Pose comes from the palm.** The part is seated on a basis built from wrist
> and knuckles, so it rolls and yaws with the hand instead of sliding across it.
> **Fingers occlude it.** After the part is drawn, the camera's own pixels are
> re-drawn clipped to the finger silhouette, so the hand closes *over* the part.

Which is the point: **"is 31 mm too thick to hold" is not a question a viewport
can answer.** It is the question the desk exists to answer, and until now the
only way to ask it was to print the thing.

Each part declares how it wants to be met. The enclosures are `grip` — tilted
into the hollow of the palm, lens reaching past the fingertips. The carrier board
is `flat`, because a bare PCB is something you present, not something you hold.

It is the same event stream as the table, so it is not a separate viewer: the
part in your hand is **the part the bench says you are attending to**, and the
glow tracks measured effort — amber below the threshold, exactly like the
guardrail on the table.

| Key | Effect |
|---|---|
| `1` `2` `3` | Shell A / Shell B / carrier board |
| `←` `→` `↑` `↓` | rotate · tilt in the palm |
| `d` `w` `o` `h` | dimensions · wireframe · fingers-in-front · hand skeleton |
| `c` `m` | next camera · mirror |
| `[` `]` | calibrate knuckle span, if the scale looks off |
| `f` | freeze tracking |

### Run it on your own laptop

**The browser has to be on the machine the camera is plugged into.** No amount
of port-forwarding gets a remote page to your webcam, so `/hold` has to be
served from your laptop. It needs no backend and no Python packages to do that:

```bash
git clone https://github.com/jim4226/FoundersIncHackathon.git
cd FoundersIncHackathon
git checkout claude/cad-renders-hand-display-0d7rrq

cd web && python3 -m http.server 8080
```

Open **`http://localhost:8080/hold.html`** and allow the camera. That is the
whole setup — every asset the page needs is in `web/`, and the geometry and
renderer have no dependencies at all. Only the hand tracker is fetched from the
network, and without a bench running the header just reads `bench offline`.

Then, when you want the fused loop as well, run the real thing and use
`http://localhost:8000/hold` instead:

```bash
pip install -r requirements.txt
python -m backend.server
```

> **It must be `localhost`.** Browsers only expose cameras in a *secure
> context* — `https://`, or `localhost`. Reaching the same server by LAN
> address (`http://192.168.1.x:8080`) gives you a page with no camera and no
> obvious reason why. The page detects this and says so rather than failing
> blank, but the fix is always to open it as `localhost`.

### Two cameras, two jobs

The bench runs **two** cameras at once and they must not fight over a device:

| Camera | Watches | Owned by |
|---|---|---|
| **Laptop lid** | your hand | the browser, on `/hold` |
| **Desk camera** | the table | `gesture_server.py` / Prism |

A camera can only be opened by one process, so the desk camera needs to be told
which index it is once the browser is holding the lid camera:

```bash
CAM_INDEX=1 python gesture/gesture_server.py     # desk camera, not the lid
```

On the `/hold` side, pick the lid camera from the **Camera** dropdown (`c`
cycles). The choice is remembered, so this is a one-time setup per machine, and
`/hold?cam=<deviceId>` pins it for a scripted demo. **Mirror** is on by default
— correct for a lid camera facing you, and the thing to turn off if you ever
point `/hold` at an overhead camera instead, since otherwise moving left moves
the render right.

If the tracker can't be fetched within 12 s the page releases the camera and
falls back to the simulated hand, rather than sitting on "loading" with the lid
light on.

**Geometry is parametric, in millimetres, from the same numbers as the STEP
file** — no mesh assets, and no WebGL or Three.js either. `web/cad.js` is a
painter's-algorithm renderer over 2D canvas: silhouette outlines resolved
per-face so a rib on the far side can't paint through the body, Newell normals
because chamfered corners make the three-point cross product meaningless, and
decals depth-sorted on their host's axis so the display glass doesn't vanish
under the top face it sits on.

Only the hand tracker is fetched from the network (MediaPipe Tasks Vision). When
it can't be — no webcam, blocked CDN, venue wifi — the page falls back to a
simulated hand driven by the identical palm frame, so the demo degrades instead
of dying. Force it with `/hold?sim=1`.

---

## Why the EEG isn't a gimmick

This is the question a judge asks in the first ten seconds, so here is the answer.

The EEG does **two** jobs nothing else on the desk can do:

1. **It knows which moments mattered.** A camera pointed at a bench for eight
   hours produces eight hours of nothing. The brain is the only sensor that
   knows *that one* was worth keeping.
2. **It works when your hands don't.** Your hands are holding a part in
   alignment, or covered in flux. That's when you most need to mark something
   and least can.

And the effort score is a **safety guardrail.** Hardware hurts you in ways
software doesn't. Below threshold, destructive operations lock.

---

## What we actually measure (and what we refuse to)

The Muse has four dry electrodes: TP9, AF7, AF8, TP10, referenced to FPz. That
montage decides everything. We were rigorous about this because overclaiming is
how EEG demos die.

**What drives the effort score:**

| Signal | What it is | Weight |
|---|---|---|
| **Blink rate** | Ocular (EOG) on AF7/AF8. ~5/min when absorbed, ~26/min when diffuse — a 5× effect on the two electrodes with the best contact. | 60% |
| **Alpha** | Cortical, 8–13 Hz at TP9/TP10. The one genuine brain signal here. | 25% |
| **Stillness** | The headband's IMU. Not a brain signal, and the UI says so. | 15% |

**What we deliberately did NOT build**, and why — this list is a credibility
asset, not an apology:

- **No frontal-theta "focus score."** The workload literature measures frontal
  *midline* theta at Fz/FCz. Muse has no midline electrode and references to
  FPz, which *cancels* frontal-midline potentials rather than attenuating them.
  Worse, the blink artifact sits in the same band 50× larger — so the meter
  would be a blink counter in a lab coat.
- **No theta/beta "attention" metric.** Discredited in its own clinical
  literature, and on dry electrodes the beta band is dominated by jaw muscle. A
  rising score would mostly mean the presenter clenched.
- **No error-related potentials.** The ERN comes from anterior cingulate and
  peaks at FCz. We have no central electrode. Structurally unmeasurable here.
- **Nothing that "reads your thoughts."** Four dry electrodes, two of which are
  functionally EOG sensors.

### The part with no calibration

Left-versus-right attention needs **no calibration step**, and the reason is
worth saying out loud: every quantity is a **contrast between two electrodes**,
not a level. Levels drift with fit, sweat and session time. A left-minus-right
difference divides that drift out.

Two complementary signals:

- **Horizontal EOG (AF7 − AF8)** is fast. The eye is a standing dipole, so
  looking right drives AF8 positive and AF7 negative. Lands in tens of
  milliseconds — but the headband is AC-coupled, so it decays within seconds.
- **Alpha lateralisation (TP9 vs TP10)** is slow and *holds*. Alpha suppresses
  contralateral to attended space. As a ratio it is self-normalising, and it
  tracks covert attention even when the eyes are still.

So the EOG catches the switch and the alpha holds the state. The blend is
weighted by which signal is currently saying anything — verified: after holding
a look for 15 s the ocular term had decayed to −0.08 while alpha carried the
position at −1.00, and the highlight stayed locked on.

Blinks and gaze are separated by physics, not thresholds: both eyelids move
together so a blink is **common mode** (AF7 + AF8), while gaze rotates the
dipole one way so it is **differential** (AF7 − AF8).

---

## Measured performance

From `backend/eeg` against the simulated subject:

```
focused          effort 94/100 at  6.0 blinks/min
diffuse          effort 20/100 at 28.0 blinks/min
gaze zones       4/4 correct (left/right/left/right)
flag gesture     3/3 detected, 281–288 ms inter-blink gaps
false positives  0 across 30 s of heavy natural blinking at 28/min
diffuse → locked 15 s     |     re-engaged → unlocked 6 s
```

---

## Run it

```bash
pip install -r requirements.txt
python -m backend.server          # simulated subject, no hardware needed
# open http://localhost:8000
```

With a real Muse:

```bash
EEG_SOURCE=osc python -m backend.server        # Mind Monitor on a phone → OSC/UDP
EEG_SOURCE=brainflow MUSE_BOARD_ID=39 python -m backend.server
```

**Use the Mind Monitor path on stage.** It moves the Bluetooth link off the
laptop and into the presenter's pocket — ~30 cm instead of ~3 m, worth about
20 dB of link budget in a room with several hundred radios fighting over an
83 MHz band. Board IDs: Muse 2 = 38, Muse S = 39, Muse S Athena = 67.

> Venue WiFi usually enables AP client isolation, which silently drops
> phone→laptop UDP with no error anywhere. If no packets arrive, put the laptop
> on the phone's hotspot. **Test this first, not at 3am.**

### Stage controls

Arrow keys beat sliders under stage lights.

| Key | Effect |
|---|---|
| `←` / `→` | look left / right |
| `↑` / `↓` | focused / diffuse |
| `b` | double-blink (flag) |
| `c` | jaw clench (confirm) |

---

## Architecture

The EEG pipeline emits **semantic events** — `effort`, `focus_changed`, `flag` —
over a WebSocket. Nothing downstream knows whether a headband, a phone, or the
simulator produced them.

```
Muse ──┬─ Mind Monitor (OSC/UDP) ──┐
       ├─ BrainFlow (native BLE) ───┼──▶ EffortEstimator ───┐
       └─ Simulator ────────────────┘    LateralAttention ──┤
                                                            │
Webcam ──▶ gesture_server.py ──ws:8765──▶ GestureBridge ─────┤
                                                            ├──▶ /ws ──▶ desk view
Prism ──▶ objects + hand position ──▶ zone mapping ──────────┤          ├▶ control view
                                                            │          └▶ /hold (CAD in hand)
                                                            │
                                              ProjectAgent ◀─┘
                                                    └──▶ Boxic (decisions + versions)
```

That decoupling is the point: the two halves of the team build in parallel, and
a dead headband degrades the demo instead of ending it.

```
web/style.css              Boxic Surface tokens + the table view
web/hold.css               the camera view
web/fonts.css              Funnel Display + JetBrains Mono, inlined
backend/eeg/metrics.py     effort score, blink + clench detection
backend/eeg/attention.py   calibration-free left/right attention
backend/eeg/sources.py     Mind Monitor / BrainFlow / simulator
backend/gesture.py         bridge to gesture_server.py votes
backend/boxic.py           mapping onto Boxic versions + decisions
backend/store.py           project, variants, effort-scored history
backend/agent.py           the effort gate
backend/server.py          WebSocket + REST
gesture/                   MediaPipe thumbs up/down (teammate's module)
web/                       control view
web/cad.js                 parametric CAD geometry + 2D-canvas renderer
web/hold.js                hand tracking, palm frame, finger occlusion
```

Run both processes for the full loop:

```bash
python -m backend.server                 # :8000  bench  (+ /hold, lid camera)
CAM_INDEX=1 python gesture/gesture_server.py    # :8765  votes, desk camera
```

`CAM_INDEX` defaults to `0`. Set it when the browser already holds index 0 for
the `/hold` view — see *Two cameras, two jobs* above.

No webcam, or MediaPipe won't install (it has no wheel for Python 3.13/3.14)?
`gesture/mock_votes.py` speaks the identical protocol on the identical port, so
the bench cannot tell the difference:

```bash
python gesture/mock_votes.py             # interactive: a / r / q
python gesture/mock_votes.py --script    # timed approve/reject loop
```

This is also the stage insurance. If the camera won't open two minutes before
you present, run the mock and drive votes from the keyboard — votes still route
through attention and the effort gate exactly as a real thumbs-up would.

The bridge reconnects on its own, so either process can start and stop without
taking the other down.

---

## Next: the glue

**1. Hand position beats gaze for the referent.** Right now `LateralAttention`
maps EOG + alpha onto left/right zones. But if Prism reports *which object the
hand is on or near*, that's a far more robust referent than inferring gaze —
and it's what "interacted with the most" actually means. Then the split gets
cleaner:

> **Prism answers *what*. The EEG answers *how much it mattered*.**

The EEG stops having to carry the referent at all and only supplies engagement
plus the flag. Fewer failure modes, stronger story. `LateralAttention.set_zones()`
already takes an arbitrary `{id: position}` map, so this is a small change once
object positions are available.

**2. Boxic needs one write tool.** Boxic stores a whole project as a JSON
document in `workspace_projects.data`, with `Version` and `ProjectDecision`
records inside it. `ProjectDecision` already carries an `origin` field —
`"conversation" | "manual"` — distinguishing a decision reached by talking to
the project from one typed in by hand. **Bench is simply a third origin:**
`"bench"`, a decision reached at the physical desk with the operator's measured
state attached.

```jsonc
{
  "id": "dec_a1b2c3", "authorHandle": "you", "versionId": "ver_var_0006",
  "title": "Approved Shell A by gesture while attending to it",
  "origin": "bench",          // ← new value, existing field
  "effort": 88, "weight": 0.88, "flagged": false   // ← extra keys, no migration
}
```

Because `data` is `jsonb`, the extra keys ride along without a schema change —
anything reading decisions today keeps working. Bench variants map onto Boxic
`Version` records, carrying `attentionSeconds` (measured dwell), so the record
shows which option was actually considered rather than only which one won.

`backend/boxic.py` does this mapping now and `GET /api/boxic/export` returns an
importable document. The only missing piece is on Boxic's side: its MCP server
currently exposes `list_workspaces`, `list_projects` and `get_project` — all
read-only. **One `commit_decision` tool taking the shape above closes the loop.**

---

## Open questions

- **Prism SDK.** No public developer surface documented. Resolve early: can we
  get object detections and project custom content, or do we roll our own
  overhead camera + projector rig?
- **Which Muse?** Muse 2 / S: every software path works. Athena (MS-03): needs
  BrainFlow ≥ 5.22.2, muselsl ≥ 2.5.0, or Mind Monitor ≥ 2.4.3 — BlueMuse and
  Petal are out.
- **Screen the presenter.** ~5–10% of people have very low resting alpha, and
  TP9/TP10 sit behind the ears where thick or curly hair blocks contact. Check
  on day one, and have a backup presenter.
