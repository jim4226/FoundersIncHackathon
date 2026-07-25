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
| **Desk view** | Second camera over the desk. Detects whatever you put down and draws the overlay on a second screen. Our own stand-in for Prism. | ✅ Built (`desk/`) |
| **Phone camera** | Scan a QR code and the phone *becomes* the desk camera. No second webcam. | ✅ Built (`backend/phone.py`) |
| **Control view** | The operator's screen. Effort breakdown, project history, agent console, stage controls. | ✅ Built |
| **Operator state** | Muse EEG. Effort level, and a deliberate flag gesture. | ✅ Built and tested |
| **Gesture** | MediaPipe thumbs-up/down → `design_vote` on `ws://localhost:8765`. | ✅ Built (`gesture/`) |
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

## The hosted demo

**<https://foundersinchacknight.netlify.app>** — the front end, live, with no
hardware anywhere near it.

Netlify cannot run the bench: it is a Python process on a laptop holding open
WebSockets to a headband and two cameras. Deploying the UI alone would put a
control view on the internet that hangs forever on a dead socket, which is worse
than deploying nothing. So the pages carry one more event source.

`web/demo.js` speaks the identical `tick` / `desk` / `vote` / `flag` stream the
server speaks, and the pages consume it without knowing the difference — the
same rule the backend was built on ("nothing downstream knows whether a
headband, a phone, or the simulator produced them"), extended one hop further,
into the browser. It engages **only when nothing answers**: the shim opens the
real socket first and falls back after 1.4 s of silence, so running the bench
locally keeps it entirely out of the way. Every page it drives is stamped
`DEMO`, because a simulated effort score presented as a measured one would be
the single dishonest thing in this project.

| | |
|---|---|
| `?demo=1` | simulate without trying the real socket first |
| `?live=1` | never simulate — fail exactly like production would |

It is not a video: the stage controls, the arrow keys, the agent box and the
promote flow all work against the simulator, and left alone it runs a loop that
makes the argument twice — the same approve, at two different effort levels,
landing differently.

`netlify.toml` publishes `web/` with no build step. `/static/*` rewrites onto
the same directory so one set of files works under both servers, and `/` serves
the explainer rather than the operator console, because someone arriving from a
link needs to know what they are looking at first.

The one thing the hosted copy cannot do is pair a phone — frames have to reach a
bench on your LAN. Scan the code there and the phone page opens, camera live,
and says so.

---

## Two screens

| Screen | URL | Who looks at it |
|---|---|---|
| **Landing** | `/` (hosted) | Anyone arriving from a link. The explainer. Locally, `/` is the control view. |
| **Desk** | `/desk` | The audience. Live camera of the table with the overlay drawn on top. |
| **Control** | `/` | You. Effort breakdown, agent console, stage controls, the pairing QR. |
| **Record** | `/project` | The payoff. What the session produced: decision log, designs, talk-to-your-project. |
| **Phone** | `/phone` | Nobody — it's propped over the desk being a camera. |

We have no Prism SDK, so `desk/desk_server.py` does the seeing half with a
second webcam and puts the drawing half on a screen instead of a projector. For
a demo that is arguably better — the audience sees the desk and the overlay in
one frame, rather than trying to read projected light off a table under stage
lights.

Detection is background subtraction against an empty-desk reference, not a
trained model: no download, no labels, no GPU, runs at camera rate, and it does
not care what you put down. Two heuristics carry it — anything touching the
frame border is an arm reaching in rather than an object on the desk, and
objects are matched between frames by nearest centroid so ids stay stable while
you slide something around.

The join is one number. The camera reports each object's normalised left-to-right
`position` on `[-1, +1]`; that is the same axis the headband reports gaze on. So
**the camera says where things are, the headband says which one you mean**, and
neither needs to know the other exists. Move an object across the table and its
zone moves with it.

### Hands

The desk camera also runs MediaPipe hand landmarks, so the index fingertip
answers *which of these am I touching* — a far more direct referent than
inferring it from gaze. Reaching for a thing is unambiguous in a way that looking
towards it is not, which leaves the headband only to say **how much it mattered**.

Handling time is tracked separately from looking time. Picking something up and
staring at it are different kinds of interest, and collapsing them would
overstate both — so the record carries `looked at 42s · handled 11s` per design,
and the agent reasons over both.

This forced a fix worth knowing about: reaching for an object merges its blob
into your arm, and the arm runs off the frame edge, so the border heuristic that
correctly rejects arms was taking the object with it. A design vanished at the
exact moment someone touched it. Tracked objects now survive ~2.5 s of occlusion,
flagged rather than dropped.

Hand tracking is **optional** — without `mediapipe` or the model file the desk
still detects objects, it just can't report what the hand is on:

```bash
pip install mediapipe
curl -o desk/hand_landmarker.task \
  https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task
```

> On headless Linux MediaPipe needs GL libraries: `apt-get install libgles2 libegl1`.
> macOS needs nothing extra.

## Run it

```bash
pip install -r requirements.txt
python -m backend.server --https  # simulated subject, no hardware needed
# control view  https://localhost:8000
# desk view     https://localhost:8000/desk
# phone camera  scan the QR on the control view
```

### The desk camera is a phone

The fastest way to get a camera over the desk is the one already in your
pocket. The control view shows a QR code; scan it, prop the phone over the
table, and it starts streaming. The phone sends JPEG frames, the bench runs the
same background subtraction it runs on a webcam, and the desk view draws the
same overlay — `backend/phone.py` and `desk/desk_server.py` share one detector
(`backend/vision.py`), so **nothing downstream can tell which camera it has.**

Two things about that path are worth knowing before you rely on it on stage:

- **It needs HTTPS.** Browsers only hand out the camera in a secure context, and
  `http://192.168.1.24:8000` is not one. `--https` generates a self-signed
  certificate naming your current LAN IP (via `openssl`, into `.bench-certs/`).
  The phone warns once about it, you tap through, and the camera works. Without
  `--https` the phone page still loads and then tells you exactly why it can't
  open the camera, rather than showing a black rectangle.
- **Both cameras can be running.** While a phone is streaming it owns the desk
  feed; the webcam service takes back over ~2.5 s after the phone drops. So the
  phone is also the fallback for a webcam that won't open, and vice versa.

Calibration is the same idea as the webcam's `c` key: clear the desk and tap
**Capture empty desk** on the phone (or the button on the control view).
If nobody does, the phone self-calibrates a second or so after the picture
stops moving, so an uncalibrated demo shows something rather than nothing.

The pairing URL carries a token that changes every run — a phone still holding
an old QR code cannot quietly take over the desk feed mid-demo. Set
`BENCH_PAIR_TOKEN` to pin it, or `BENCH_HOST` to advertise a different address
(a tunnel, say) instead of the detected LAN IP.

The overhead webcam, if you'd rather use one, in its own terminal:

```bash
python desk/desk_server.py                 # real camera (--camera N to pick one)
python desk/desk_server.py --synthetic     # fake desk, no camera needed
```

Clear the desk and press `c` in its preview window to capture the reference
frame, then put your objects down. `--synthetic` and `--headless` capture the
reference automatically.

> **Camera indices matter.** `gesture_server.py` uses camera `0` for the hand and
> `desk_server.py` uses `1` for the desk. If either grabs the wrong one, pass
> `--camera N` to the desk service or edit `CAM_INDEX` in the gesture one. Pairing
> a phone sidesteps this entirely: it leaves camera `0` to the gesture module and
> needs no index at all.

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
Phone ──ws:/ws/phone──┐                                     │          └▶ control view
                      ├─▶ vision.py ──▶ zone mapping ───────┤
Desk cam ──ws:8766────┘   (one detector)                    │
                                              ProjectAgent ◀─┘
                                                    └──▶ Boxic (decisions + versions)
```

That decoupling is the point: the two halves of the team build in parallel, and
a dead headband degrades the demo instead of ending it.

```
backend/eeg/metrics.py     effort score, blink + clench detection
backend/eeg/attention.py   calibration-free left/right attention
backend/eeg/sources.py     Mind Monitor / BrainFlow / simulator
backend/gesture.py         bridge to gesture_server.py votes
backend/desk.py            bridge to the desk camera, binds objects to variants
backend/vision.py          desk object detection, shared by both cameras
backend/phone.py           a phone paired by QR, streaming as the desk camera
backend/pairing.py         LAN address, pairing token, the QR itself
backend/tls.py             self-signed cert, so the phone may open its camera
backend/boxic.py           mapping onto Boxic versions + decisions
backend/store.py           project, variants, effort-scored history
backend/agent.py           the effort gate
backend/server.py          WebSocket + REST
gesture/                   MediaPipe thumbs up/down (teammate's module)
desk/                      desk camera: object detection + hand landmarks
web/surface.html           Boxic Surface — the projected table, served at /
web/landing.html           the explainer, at /about on the hosted copy
web/demo.js                browser-side bench, for when nothing answers
web/index.html             control view
web/desk.html              desk view (second screen)
web/project.html           the record — decision log + talk to your project
web/phone.html             phone view (what the QR code opens)
```

Run both processes for the full loop:

```bash
python -m backend.server                 # :8000  bench
python gesture/gesture_server.py         # :8765  votes
```

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

- **Prism.** No public developer surface, so `desk/` is our own version of the
  seeing half. If Prism access lands, it drops in behind the same interface —
  it only has to report object positions on `[-1, +1]`.
- **Lighting.** Background subtraction is sensitive to the room lights changing
  after the reference frame is captured. Re-press `c` if the desk drifts, and
  capture the reference under the lighting you will actually present in.
- **Which Muse?** Muse 2 / S: every software path works. Athena (MS-03): needs
  BrainFlow ≥ 5.22.2, muselsl ≥ 2.5.0, or Mind Monitor ≥ 2.4.3 — BlueMuse and
  Petal are out.
- **Screen the presenter.** ~5–10% of people have very low resting alpha, and
  TP9/TP10 sit behind the ears where thick or curly hair blocks contact. Check
  on day one, and have a backup presenter.
