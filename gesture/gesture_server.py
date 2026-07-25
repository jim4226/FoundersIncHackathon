"""
Gesture service for the Prism hologram-design demo.

Reads a webcam, recognizes thumbs-up / thumbs-down with MediaPipe's built-in
Gesture Recognizer, debounces for demo stability, and broadcasts an
approve/reject event over a WebSocket that the renderer subscribes to.

It also *subscribes back* to the bench (`ws://localhost:8000/ws`) when it is
running, so the hand overlay reflects the fused verdict: the silhouette glow
tracks live EEG effort, a tag shows which design is in focus, and a vote flash
shows what actually happened (promoted / held-because-diffuse / no-focus). If
the bench is offline the overlay degrades to the standalone camera demo.

Run:  python gesture_server.py
Keys: ESC quit  |  a = force approve  |  r = force reject  (stage fallback)
"""

import os
import time
import json
import threading

import cv2
import numpy as np
import mediapipe as mp
from websockets.sync.server import serve

HAND_CONNECTIONS = mp.solutions.hands.HAND_CONNECTIONS

# ----------------------------- config ---------------------------------
CONF_THRESHOLD = 0.6      # min gesture confidence to consider
HOLD_SECONDS   = 0.6      # gesture must be held this long before it fires
COOLDOWN_SECS  = 2.0      # after firing, ignore new triggers this long
CAM_INDEX      = 0        # dedicated webcam pointed at the hand
WS_PORT        = 8765
MODEL_PATH     = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "gesture_recognizer.task")  # resolve next to script
BENCH_WS_URL   = os.environ.get("BENCH_WS_URL", "ws://localhost:8000/ws")
FLASH_SECS     = 2.5      # how long a vote verdict stays on screen

GESTURE_MAP = {"Thumb_Up": "approve", "Thumb_Down": "reject"}

# BGR colours shared by overlay + verdict flash
C_APPROVE = (90, 220, 60)    # green
C_REJECT  = (60, 60, 255)    # red
C_HOLD    = (0, 180, 255)    # amber — recorded but not binding
C_NEUTRAL = (180, 180, 180)  # grey

# ------------------------- websocket broadcast ------------------------
clients, clients_lock = set(), threading.Lock()


def ws_handler(conn):
    with clients_lock:
        clients.add(conn)
    try:
        for _ in conn:          # hold the connection open; we only push
            pass
    finally:
        with clients_lock:
            clients.discard(conn)


def broadcast(event: dict):
    msg = json.dumps(event)
    with clients_lock:
        dead = []
        for c in clients:
            try:
                c.send(msg)
            except Exception:
                dead.append(c)
        for c in dead:
            clients.discard(c)
    print("SENT:", msg)


def start_ws():
    with serve(ws_handler, "0.0.0.0", WS_PORT) as server:
        print(f"[ws] broadcasting on ws://localhost:{WS_PORT}")
        server.serve_forever()


# ------------------------- gesture recognizer -------------------------
BaseOptions = mp.tasks.BaseOptions
GestureRecognizer = mp.tasks.vision.GestureRecognizer
GestureRecognizerOptions = mp.tasks.vision.GestureRecognizerOptions
VisionRunningMode = mp.tasks.vision.RunningMode

latest = {"name": None, "score": 0.0, "landmarks": None}


def on_result(result, output_image, timestamp_ms):
    if result.gestures and result.gestures[0]:
        top = result.gestures[0][0]
        latest["name"], latest["score"] = top.category_name, top.score
    else:
        latest["name"], latest["score"] = None, 0.0
    latest["landmarks"] = result.hand_landmarks[0] if result.hand_landmarks else None


# ------------------ debounce / one-gesture-one-event ------------------
_state = {"candidate": None, "since": 0.0, "last_fire": 0.0}


def maybe_fire(now: float):
    name, score = latest["name"], latest["score"]
    mapped = GESTURE_MAP.get(name) if score >= CONF_THRESHOLD else None

    if mapped != _state["candidate"]:          # gesture changed -> reset timer
        _state["candidate"], _state["since"] = mapped, now
        return None

    held = now - _state["since"]
    cooled = now - _state["last_fire"]
    if mapped and held >= HOLD_SECONDS and cooled >= COOLDOWN_SECS:
        _state["last_fire"] = now
        _state["candidate"], _state["since"] = None, now  # require fresh hold
        return mapped
    return None


# ------------------- bench feedback loop (consumer) -------------------
# Best-effort subscription to the bench so the overlay can show the fused
# verdict. Everything here degrades to None/False when the bench is offline.
bench = {"connected": False, "effort": None, "focus_label": None, "flash": None}
bench_lock = threading.Lock()


def set_flash(text, color, secs=FLASH_SECS):
    with bench_lock:
        bench["flash"] = {"text": text, "color": color, "until": time.time() + secs}


def handle_bench_message(msg):
    t = msg.get("type")
    if t == "tick":
        eff = (msg.get("effort") or {}).get("effort")
        att = msg.get("attention") or {}
        zone = att.get("zone") if att.get("confidence", 0) > 0.35 else None
        label = next((v.get("label") for v in msg.get("variants", [])
                      if v.get("id") == zone), None)
        with bench_lock:
            bench["effort"] = eff
            bench["focus_label"] = label
    elif t == "focus_changed":
        with bench_lock:
            bench["focus_label"] = (msg.get("variant") or {}).get("label")
    elif t == "vote":
        label = (msg.get("variant") or {}).get("label", "design")
        if msg.get("vote") == "approve":
            if msg.get("decisive") and msg.get("promoted"):
                set_flash(f"APPROVED - {label} promoted", C_APPROVE)
            elif msg.get("binding"):
                set_flash(f"APPROVED - {label} - staged", C_APPROVE)
            else:
                set_flash(f"HELD - {label} - you were diffuse", C_HOLD)
        else:  # reject
            if msg.get("reverted"):
                set_flash(f"REJECTED - {label} - back to the drawing board", C_REJECT)
            elif msg.get("binding"):
                set_flash(f"REJECTED - {label} - staged", C_REJECT)
            else:
                set_flash(f"HELD - {label} - you were diffuse", C_HOLD)
    elif t == "vote_unresolved":
        set_flash("no design in focus", C_NEUTRAL)


def bench_subscriber():
    """Reconnecting client to the bench /ws. Never raises into the main loop."""
    from websockets.sync.client import connect
    while True:
        try:
            with connect(BENCH_WS_URL, open_timeout=5) as ws:
                with bench_lock:
                    bench["connected"] = True
                print(f"[bench] connected to {BENCH_WS_URL}")
                for raw in ws:
                    try:
                        handle_bench_message(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass  # bench not up yet, or dropped — retry below
        with bench_lock:
            bench["connected"], bench["effort"], bench["focus_label"] = False, None, None
        time.sleep(3.0)


def draw_hand(frame, landmarks, color, effort=None):
    """Neon skeleton + silhouette glow. Aura intensity tracks EEG effort."""
    h, w = frame.shape[:2]
    pts = [(int(lm.x * w), int(lm.y * h)) for lm in landmarks]

    # effort in [0,1] drives fill opacity + bone thickness; None = neutral
    e = 0.6 if effort is None else max(0.0, min(1.0, effort / 100.0))
    fill_alpha = 0.12 + 0.28 * e
    glow = int(4 + 8 * e)

    # translucent filled silhouette (convex hull) for the "glow" body
    overlay = frame.copy()
    hull = cv2.convexHull(np.array(pts, dtype=np.int32))
    cv2.fillConvexPoly(overlay, hull, color)
    cv2.addWeighted(overlay, fill_alpha, frame, 1 - fill_alpha, 0, dst=frame)

    # bones: thick color glow underneath, thin white core on top
    for a, b in HAND_CONNECTIONS:
        cv2.line(frame, pts[a], pts[b], color, glow, cv2.LINE_AA)
        cv2.line(frame, pts[a], pts[b], (255, 255, 255), 1, cv2.LINE_AA)

    # joints
    for p in pts:
        cv2.circle(frame, p, 5, color, -1, cv2.LINE_AA)
        cv2.circle(frame, p, max(6, glow), color, 1, cv2.LINE_AA)


def draw_hud(frame):
    """Bench panel (effort bar + focus) top-right, and the vote verdict flash."""
    h, w = frame.shape[:2]
    with bench_lock:
        connected, effort = bench["connected"], bench["effort"]
        focus, flash = bench["focus_label"], bench["flash"]

    x0 = w - 250
    if connected:
        cv2.putText(frame, "BENCH", (x0, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 255, 200), 2)
        if effort is not None:
            bar_w, fill = 210, int(210 * max(0.0, min(1.0, effort / 100.0)))
            bc = C_APPROVE if effort >= 65 else C_HOLD if effort >= 35 else C_REJECT
            cv2.rectangle(frame, (x0, 52), (x0 + bar_w, 72), (70, 70, 70), 1)
            cv2.rectangle(frame, (x0, 52), (x0 + fill, 72), bc, -1)
            cv2.putText(frame, f"effort {effort:.0f}", (x0, 92),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (235, 235, 235), 1)
        cv2.putText(frame, f"focus: {focus or '--'}", (x0, 116),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (235, 235, 235), 1)
    else:
        cv2.putText(frame, "bench offline (standalone)", (x0 - 60, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (140, 140, 140), 1)

    if flash and time.time() < flash["until"]:
        text, color = flash["text"], flash["color"]
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.0, 3)
        cx, cy = (w - tw) // 2, h - 55
        cv2.rectangle(frame, (cx - 20, cy - th - 20), (cx + tw + 20, cy + 18),
                      (0, 0, 0), -1)
        cv2.putText(frame, text, (cx, cy),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 3, cv2.LINE_AA)


def emit(vote: str, source: str):
    broadcast({
        "type": "design_vote",
        "vote": vote,
        "source": source,
        "confidence": round(latest["score"], 2),
        "ts": time.time(),
    })


# ------------------------------ main ----------------------------------
def main():
    threading.Thread(target=start_ws, daemon=True).start()
    threading.Thread(target=bench_subscriber, daemon=True).start()

    options = GestureRecognizerOptions(
        base_options=BaseOptions(model_asset_path=MODEL_PATH),
        running_mode=VisionRunningMode.LIVE_STREAM,
        num_hands=1,
        result_callback=on_result,
    )

    cap = cv2.VideoCapture(CAM_INDEX)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open camera index {CAM_INDEX}")

    with GestureRecognizer.create_from_options(options) as recognizer:
        while cap.isOpened():
            ok, frame = cap.read()
            if not ok:
                break
            frame = cv2.flip(frame, 1)  # mirror feels natural to the user
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            recognizer.recognize_async(mp_image, int(time.time() * 1000))

            now = time.time()
            fired = maybe_fire(now)
            if fired:
                emit(fired, source="gesture")

            # ---- on-screen feedback (looks good + helps debugging) ----
            with bench_lock:
                b_connected, b_effort = bench["connected"], bench["effort"]
            name = latest["name"]
            label = f'{name} {latest["score"]:.2f}' if name else "..."
            # BGR: green approve, red reject, cyan neutral
            color = C_APPROVE if name == "Thumb_Up" else \
                    C_REJECT if name == "Thumb_Down" else (255, 255, 0)
            if latest["landmarks"]:
                draw_hand(frame, latest["landmarks"], color, b_effort)
            cv2.putText(frame, label, (20, 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 3)
            # When the bench is up it owns the verdict (the fused outcome shows
            # in the flash); standalone, show the raw local fire.
            if fired and not b_connected:
                cv2.putText(frame, fired.upper(), (20, 120),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.6, color, 4)
            draw_hud(frame)
            cv2.imshow("gesture", frame)

            key = cv2.waitKey(1) & 0xFF
            if key == 27:                 # ESC
                break
            elif key == ord("a"):         # stage fallback: force approve
                emit("approve", source="keyboard")
            elif key == ord("r"):         # stage fallback: force reject
                emit("reject", source="keyboard")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
