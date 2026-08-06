# Gesture module — approve / reject for the Prism hologram-design demo

Thumbs-up 👍 = **approve**, thumbs-down 👎 = **reject**. Runs a webcam through
MediaPipe's built-in Gesture Recognizer, debounces for demo stability, and
broadcasts votes over a WebSocket that the renderer subscribes to.

```
[webcam] → gesture_server.py → ws://localhost:8765 → renderer → Prism projection
```

## Event format

Every confirmed gesture broadcasts one JSON message:

```json
{ "type": "design_vote", "vote": "approve", "source": "gesture",
  "confidence": 0.94, "ts": 1690000000.0 }
```

`vote` is `"approve"` or `"reject"`. `source` is `"gesture"` or `"keyboard"`
(the stage-fallback keys).

## Setup

Requires Python 3.9–3.12 (MediaPipe has no 3.13/3.14 wheels yet).

```bash
# from this folder, with a 3.12 venv active:
pip install -r requirements.txt
# download the built-in gesture model bundle:
curl -O https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task
```

## Run

```bash
python gesture_server.py
```

A preview window opens with a live confidence label. Hold a thumbs-up for
~0.6 s → an `approve` event fires. Open `test_client.html` in a browser to see
votes land in real time.

Keys in the preview window:

- `ESC` — quit
- `a` — force an approve event (stage fallback if the camera misbehaves)
- `r` — force a reject event

## Camera-free fallback (`mock_votes.py`)

If MediaPipe won't install (no wheel for Python 3.13/3.14) or the camera won't
open before you present, run the stand-in instead. It speaks the identical
protocol on the identical port, so the bench can't tell the difference — votes
still route through attention and effort exactly as a real thumbs-up would.

```bash
python mock_votes.py            # interactive: a = approve, r = reject, q = quit
python mock_votes.py --script   # timed approve/reject loop
```

## Feedback loop with the bench

When the bench (`backend/server.py`, port 8000) is running, this process also
**subscribes back** to `ws://localhost:8000/ws`, so the camera overlay becomes a
live readout of the fused verdict — on the one screen the audience is watching:

- **Effort aura** — the silhouette glow opacity + skeleton thickness track live
  EEG effort. Locked-in = bright and solid, diffuse = thin and dim.
- **Focus tag** — a `focus: Shell A` label (top-right) shows which design the
  operator is attending to, so the hand shows its referent *before* the verdict.
- **Verdict flash** — when a vote resolves, the overlay shows what *actually*
  happened, not the raw gesture: `APPROVED - Shell A promoted`,
  `HELD - Shell A - you were diffuse`, `REJECTED - Shell B`, or
  `no design in focus`. Same thumbs-up, visibly different outcome.

This needs **no backend changes** — the bench already broadcasts `tick` and
`vote` events. It's fully optional and best-effort: if the bench is offline the
overlay falls back to the standalone camera demo (shown as
`bench offline (standalone)`). Point it elsewhere with `BENCH_WS_URL`.

Full loop — run both processes:

```bash
python -m backend.server            # :8000  bench (from repo root)
python gesture/gesture_server.py    # :8765  votes + subscribes back to :8000
```

## Tuning (top of `gesture_server.py`)

| constant         | what it does                                        |
|------------------|-----------------------------------------------------|
| `CONF_THRESHOLD` | min confidence to count a gesture (0.6)             |
| `HOLD_SECONDS`   | how long the gesture must be held before firing     |
| `COOLDOWN_SECS`  | quiet period after a vote so one gesture = one event|
| `CAM_INDEX`      | which webcam (use a dedicated one, not Prism's cam) |

## Consuming votes elsewhere

**JS**
```js
const ws = new WebSocket("ws://localhost:8765");
ws.onmessage = e => { const {vote} = JSON.parse(e.data); /* approve | reject */ };
```

**Python**
```python
from websockets.sync.client import connect
import json
with connect("ws://localhost:8765") as ws:
    for msg in ws:
        vote = json.loads(msg)["vote"]
```

## Demo tips

- Dedicated webcam at chest height, hand well-lit and clear of the background.
- Keep the hold + cooldown so a rotating thumb doesn't flicker up/down.
- Have the demo-er drop their hand to neutral between votes.
- If the camera acts up on stage, tap `a`/`r` and the demo continues.
