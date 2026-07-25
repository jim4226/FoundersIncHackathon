"""Desk vision — our own stand-in for Prism's overhead sensor.

Prism watches a table from above, recognises whatever is on it, and projects
back down. We have no SDK for it, so this does the seeing half with a second
webcam and puts the drawing half on a screen instead of a projector. For a demo
that is arguably better: the audience watches the desk and the overlay together
in one frame, rather than trying to read projected light off a table under stage
lighting.

Detection itself lives in `backend/vision.py`, because a phone paired by QR code
runs the identical algorithm on the identical frames -- see `backend/phone.py`.
One implementation, two cameras, and the bench cannot tell which one it has.

Run:  python desk/desk_server.py                 # real camera
      python desk/desk_server.py --synthetic     # no camera needed
Keys: c = capture empty-desk reference   |   ESC = quit
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from websockets.sync.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import vision  # noqa: E402 - path has to be set before this import

WS_PORT = 8766
CAM_INDEX = 1          # the DESK camera; gesture_server.py uses 0
FRAME_W, FRAME_H = vision.FRAME_W, vision.FRAME_H
JPEG_QUALITY = vision.JPEG_QUALITY
TARGET_FPS = vision.TARGET_FPS

Tracker = vision.Tracker
detect = vision.detect

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

        gray = vision.to_gray(frame)

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
                "origin": "camera",
                "objects": vision.for_wire(objects),
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
