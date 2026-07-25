"""Desk vision — our own stand-in for Prism's overhead sensor.

Prism watches a table from above, recognises whatever is on it, and projects
back down. We have no SDK for it, so this does the seeing half with a second
webcam and puts the drawing half on a screen instead of a projector. For a demo
that is arguably better: the audience watches the desk and the overlay together
in one frame, rather than trying to read projected light off a table under stage
lighting.

Detection is background subtraction against an empty-desk reference, not a
trained model. That is a deliberate choice: it needs no download, no labels and
no GPU, it runs at camera rate on a laptop, and it genuinely does not care what
you put down -- which is the property that matters here. Any object is a blob.

Two heuristics do most of the work:

  * Anything touching the frame border is an arm reaching in, not an object on
    the desk. Objects get placed within the frame; hands arrive from outside it.
  * Objects are matched between frames by nearest centroid, so ids stay stable
    while you slide something around.

Run:  python desk/desk_server.py                 # real camera
      python desk/desk_server.py --synthetic     # no camera needed
Keys: c = capture empty-desk reference   |   ESC = quit
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import threading
import time

import cv2
import numpy as np
from websockets.sync.server import serve

WS_PORT = 8766
CAM_INDEX = 1          # the DESK camera; gesture_server.py uses 0
FRAME_W, FRAME_H = 960, 540
JPEG_QUALITY = 72
TARGET_FPS = 12

MIN_AREA_FRAC = 0.004  # ignore specks
MAX_AREA_FRAC = 0.30   # ignore whole-frame lighting changes
DIFF_THRESHOLD = 28
BORDER_MARGIN = 6      # px; a blob this close to the edge is an arm
MATCH_RADIUS_FRAC = 0.12

clients, clients_lock = set(), threading.Lock()
state = {"objects": [], "reference": None, "frame": None}


# ------------------------------------------------------------- websocket

def ws_handler(conn):
    with clients_lock:
        clients.add(conn)
    print(f"[ws] client connected ({len(clients)} total)")
    try:
        for _ in conn:
            pass
    finally:
        with clients_lock:
            clients.discard(conn)


def broadcast(message: dict) -> None:
    raw = json.dumps(message)
    with clients_lock:
        dead = []
        for c in clients:
            try:
                c.send(raw)
            except Exception:  # noqa: BLE001 - drop the client, keep serving
                dead.append(c)
        for c in dead:
            clients.discard(c)


def start_ws() -> None:
    with serve(ws_handler, "0.0.0.0", WS_PORT, max_size=8 * 1024 * 1024) as server:
        print(f"[ws] desk broadcasting on ws://localhost:{WS_PORT}")
        server.serve_forever()


# ------------------------------------------------------------- detection

class Tracker:
    """Nearest-centroid matching so an object keeps its id while it moves."""

    def __init__(self):
        self._next_id = 1
        self.tracked: list[dict] = []

    def update(self, detections: list[dict], width: int) -> list[dict]:
        radius = width * MATCH_RADIUS_FRAC
        unmatched = list(self.tracked)
        result = []

        for det in detections:
            best, best_dist = None, radius
            for prior in unmatched:
                dist = math.dist(det["centroid"], prior["centroid"])
                if dist < best_dist:
                    best, best_dist = prior, dist
            if best is not None:
                unmatched.remove(best)
                det["id"] = best["id"]
                det["label"] = best["label"]
            else:
                det["id"] = f"obj_{self._next_id}"
                det["label"] = f"Object {self._next_id}"
                self._next_id += 1
            result.append(det)

        # Sort left to right so labels read naturally on the desk.
        result.sort(key=lambda d: d["centroid"][0])
        self.tracked = result
        return result


def detect(frame: np.ndarray, reference: np.ndarray) -> list[dict]:
    """Blobs that are present now and were not on the empty desk."""
    h, w = frame.shape[:2]
    gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (11, 11), 0)
    delta = cv2.absdiff(reference, gray)
    _, mask = cv2.threshold(delta, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=2)
    mask = cv2.dilate(mask, None, iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    frame_area = float(h * w)
    found = []

    for contour in contours:
        area = cv2.contourArea(contour)
        if not (MIN_AREA_FRAC * frame_area <= area <= MAX_AREA_FRAC * frame_area):
            continue
        x, y, bw, bh = cv2.boundingRect(contour)
        # A blob running off the edge of the frame is an arm reaching in.
        if (x <= BORDER_MARGIN or y <= BORDER_MARGIN
                or x + bw >= w - BORDER_MARGIN or y + bh >= h - BORDER_MARGIN):
            continue
        cx, cy = x + bw / 2, y + bh / 2
        found.append({
            "centroid": (cx, cy),
            "bbox": [x, y, bw, bh],
            "area": area,
            # Normalised left-to-right position, the axis the EEG resolves.
            "position": round((cx / w) * 2 - 1, 3),
            "positionY": round((cy / h) * 2 - 1, 3),
        })

    return found


# ------------------------------------------------------------- synthetic

def synthetic_frames():
    """A fake desk with two objects that drift, for building without a camera."""
    t = 0.0
    while True:
        frame = np.full((FRAME_H, FRAME_W, 3), 24, np.uint8)
        cv2.rectangle(frame, (0, 0), (FRAME_W, FRAME_H), (32, 30, 28), -1)
        # faint desk grain so the reference frame is not perfectly uniform
        noise = np.random.normal(0, 2.5, (FRAME_H, FRAME_W, 1)).astype(np.int16)
        frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)

        if t > 1.0:   # objects appear only after the reference is captured
            drift = int(14 * math.sin(t * 0.6))
            cv2.rectangle(frame, (200 + drift, 190), (370 + drift, 350), (168, 172, 178), -1)
            cv2.rectangle(frame, (200 + drift, 190), (370 + drift, 350), (210, 214, 220), 3)
            cv2.circle(frame, (700 - drift, 268), 88, (150, 160, 172), -1)
            cv2.circle(frame, (700 - drift, 268), 88, (200, 208, 216), 3)

        yield frame
        t += 1.0 / TARGET_FPS
        time.sleep(1.0 / TARGET_FPS)


# ------------------------------------------------------------------ main

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthetic", action="store_true",
                        help="generate a fake desk instead of opening a camera")
    parser.add_argument("--camera", type=int, default=CAM_INDEX,
                        help="camera index for the DESK cam (gesture uses 0)")
    parser.add_argument("--headless", action="store_true",
                        help="no preview window; auto-captures the reference frame")
    args = parser.parse_args()

    threading.Thread(target=start_ws, daemon=True).start()
    tracker = Tracker()

    if args.synthetic:
        frames = synthetic_frames()
        cap = None
    else:
        cap = cv2.VideoCapture(args.camera)
        if not cap.isOpened():
            raise RuntimeError(
                f"Could not open desk camera {args.camera}. "
                f"Try --camera 0/1/2, or --synthetic to run without one.")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
        frames = None

    reference = None
    started = time.time()
    print("Place nothing on the desk, then press 'c' to capture the reference.")

    while True:
        if frames is not None:
            frame = next(frames)
        else:
            ok, frame = cap.read()
            if not ok:
                break
            frame = cv2.resize(frame, (FRAME_W, FRAME_H))

        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (11, 11), 0)

        # Headless and synthetic runs capture the reference on their own after a
        # moment, so the service is usable without anyone at the keyboard.
        if reference is None and (args.headless or args.synthetic) and time.time() - started > 1.0:
            reference = gray.copy()
            print("[desk] reference captured automatically")

        objects = tracker.update(detect(frame, reference), FRAME_W) if reference is not None else []

        # Broadcast the CLEAN frame. Boxes and labels are the desk view's job --
        # it knows which variant each object is bound to and which one is being
        # attended to, and drawing them here as well just collides with that.
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if ok:
            broadcast({
                "type": "desk_frame",
                "ts": time.time(),
                "calibrated": reference is not None,
                "width": FRAME_W,
                "height": FRAME_H,
                "objects": [
                    {k: v for k, v in obj.items() if k != "centroid"} | {
                        "cx": round(obj["centroid"][0], 1),
                        "cy": round(obj["centroid"][1], 1),
                    }
                    for obj in objects
                ],
                "jpeg": base64.b64encode(buf).decode("ascii"),
            })

        if not args.headless and not args.synthetic:
            # The local window is for whoever is setting the camera up: it shows
            # what the detector sees so you can tell a bad reference frame from
            # a bad camera angle. The audience never looks at this one.
            display = frame.copy()
            for obj in objects:
                x, y, w, h = obj["bbox"]
                cv2.rectangle(display, (x, y), (x + w, y + h), (224, 200, 69), 2)
                cv2.putText(display, obj["label"], (x, y - 9),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (224, 200, 69), 2)
            banner = "press 'c' with an empty desk" if reference is None else f"{len(objects)} object(s)"
            cv2.putText(display, banner, (18, 38),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (224, 200, 69), 2)
            cv2.imshow("desk", display)
            key = cv2.waitKey(1) & 0xFF
            if key == 27:
                break
            if key == ord("c"):
                reference = gray.copy()
                tracker.tracked.clear()
                print("[desk] reference captured")
        else:
            time.sleep(1.0 / TARGET_FPS)

    if cap:
        cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
