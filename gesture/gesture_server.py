"""
Gesture service for the Prism hologram-design demo.

Reads a webcam, recognizes thumbs-up / thumbs-down with MediaPipe's built-in
Gesture Recognizer, debounces for demo stability, and broadcasts an
approve/reject event over a WebSocket that the renderer subscribes to.

Run:  python gesture_server.py
Keys: ESC quit  |  a = force approve  |  r = force reject  (stage fallback)
"""

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
MODEL_PATH     = "gesture_recognizer.task"

GESTURE_MAP = {"Thumb_Up": "approve", "Thumb_Down": "reject"}

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


def draw_hand(frame, landmarks, color):
    """Neon skeleton + translucent silhouette glow over the tracked hand."""
    h, w = frame.shape[:2]
    pts = [(int(lm.x * w), int(lm.y * h)) for lm in landmarks]

    # translucent filled silhouette (convex hull) for the "glow" body
    overlay = frame.copy()
    hull = cv2.convexHull(np.array(pts, dtype=np.int32))
    cv2.fillConvexPoly(overlay, hull, color)
    cv2.addWeighted(overlay, 0.22, frame, 0.78, 0, dst=frame)

    # bones: thick color glow underneath, thin white core on top
    for a, b in HAND_CONNECTIONS:
        cv2.line(frame, pts[a], pts[b], color, 6, cv2.LINE_AA)
        cv2.line(frame, pts[a], pts[b], (255, 255, 255), 1, cv2.LINE_AA)

    # joints
    for p in pts:
        cv2.circle(frame, p, 5, color, -1, cv2.LINE_AA)
        cv2.circle(frame, p, 8, color, 1, cv2.LINE_AA)


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
            name = latest["name"]
            label = f'{name} {latest["score"]:.2f}' if name else "..."
            # BGR: green approve, red reject, cyan neutral
            color = (90, 220, 60) if name == "Thumb_Up" else \
                    (60, 60, 255) if name == "Thumb_Down" else (255, 255, 0)
            if latest["landmarks"]:
                draw_hand(frame, latest["landmarks"], color)
            cv2.putText(frame, label, (20, 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 3)
            if fired:
                cv2.putText(frame, fired.upper(), (20, 120),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.6, color, 4)
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
