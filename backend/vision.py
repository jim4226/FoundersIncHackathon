"""Desk object detection, shared by every camera that can see the desk.

`desk/desk_server.py` (a second webcam) and `backend/phone.py` (a phone paired
by QR code) both need the same answer to the same question: given a frame and a
picture of the empty desk, what is sitting on it and where. Keeping one
implementation here means the two paths cannot drift, and the bench genuinely
cannot tell which camera produced a frame.

Detection is background subtraction against an empty-desk reference, not a
trained model: no download, no labels, no GPU, runs at camera rate, and it does
not care what you put down. Two heuristics carry it:

  * Anything touching the frame border is an arm reaching in, not an object on
    the desk. Objects get placed within the frame; hands arrive from outside it.
  * Objects are matched between frames by nearest centroid, so ids stay stable
    while you slide something around.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import cv2
import numpy as np

FRAME_W, FRAME_H = 960, 540
JPEG_QUALITY = 72
TARGET_FPS = 12

MIN_AREA_FRAC = 0.004  # ignore specks
MAX_AREA_FRAC = 0.30   # ignore whole-frame lighting changes
DIFF_THRESHOLD = 28
BORDER_MARGIN = 6      # px; a blob this close to the edge is an arm
MATCH_RADIUS_FRAC = 0.12
# ~2.5 s at 12 fps. Long enough to cover a hand resting on an object, short
# enough that something actually removed from the desk disappears promptly.
OCCLUSION_GRACE_FRAMES = 30

# Hand tracking. Optional: without mediapipe or the model file both cameras
# still detect objects, they just cannot report which one your hand is on.
HAND_MODEL = os.environ.get(
    "HAND_MODEL",
    str(Path(__file__).resolve().parent.parent / "desk" / "hand_landmarker.task"))
INDEX_TIP = 8          # MediaPipe hand landmark index for the index fingertip
TOUCH_RADIUS_FRAC = 0.16

# Two frames this similar mean nothing in shot is moving -- used to decide when
# a phone being propped over the desk has come to rest.
STILL_THRESHOLD = 2.2


class Tracker:
    """Nearest-centroid matching so an object keeps its id while it moves.

    Objects also SURVIVE briefly after they stop being detected, which is not a
    nicety -- it is required for correctness. Reaching for an object merges its
    blob into the arm, and the arm runs off the edge of the frame, so the border
    heuristic that (correctly) rejects arms takes the object with it. Without
    persistence a design vanishes at the exact moment someone touches it, which
    is the one moment the system most needs to know it is there.

    A vanished object keeps its last known position and is flagged `occluded`
    until the grace period expires.
    """

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
            det["missing"] = 0
            det["occluded"] = False
            result.append(det)

        # Carry unmatched priors forward for a moment before giving up on them.
        for prior in unmatched:
            missing = prior.get("missing", 0) + 1
            if missing <= OCCLUSION_GRACE_FRAMES:
                carried = dict(prior)
                carried["missing"] = missing
                carried["occluded"] = True
                result.append(carried)

        # Sort left to right so labels read naturally on the desk.
        result.sort(key=lambda d: d["centroid"][0])
        self.tracked = result
        return result

    def reset(self) -> None:
        self.tracked.clear()


def to_gray(frame: np.ndarray) -> np.ndarray:
    """The blurred greyscale a reference frame and a live frame are compared in."""
    return cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (11, 11), 0)


def is_still(gray: np.ndarray, previous: np.ndarray | None) -> bool:
    """True when consecutive frames are near-identical, i.e. the camera has settled."""
    if previous is None or previous.shape != gray.shape:
        return False
    return float(cv2.absdiff(gray, previous).mean()) < STILL_THRESHOLD


def detect(frame: np.ndarray, reference: np.ndarray) -> list[dict]:
    """Blobs that are present now and were not on the empty desk."""
    h, w = frame.shape[:2]
    gray = to_gray(frame)
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


# ------------------------------------------------------------------ hands


class Hands:
    """MediaPipe hand landmarks over the desk.

    Overhead, the interesting landmark is the index fingertip: it answers "which
    of these am I touching", which is a far more direct referent than inferring
    it from gaze. The camera resolves WHAT, the headband is left to say how much
    it mattered.

    Entirely optional. If mediapipe is absent or the model has not been
    downloaded, `detect` returns nothing and the desk carries on doing object
    detection -- the demo degrades rather than dies. Both cameras use this: the
    overhead webcam in desk/desk_server.py and a paired phone in backend/phone.py.
    """

    def __init__(self, model_path: str = HAND_MODEL):
        self.available = False
        self.reason = ""
        self._landmarker = None
        try:
            import mediapipe as mp
        except ImportError:
            self.reason = "mediapipe not installed"
            return
        if not os.path.exists(model_path):
            self.reason = f"model missing: {os.path.basename(model_path)}"
            return
        try:
            base = mp.tasks.BaseOptions(model_asset_path=model_path)
            options = mp.tasks.vision.HandLandmarkerOptions(
                base_options=base,
                running_mode=mp.tasks.vision.RunningMode.VIDEO,
                num_hands=2,
                min_hand_detection_confidence=0.5,
                min_hand_presence_confidence=0.5,
                min_tracking_confidence=0.5,
            )
            self._landmarker = mp.tasks.vision.HandLandmarker.create_from_options(options)
            self._mp = mp
            self.available = True
        except Exception as exc:  # noqa: BLE001 - hands are a bonus, never fatal
            self.reason = f"{type(exc).__name__}: {exc}"

    def detect(self, frame: np.ndarray, timestamp_ms: int) -> list[dict]:
        if not self.available:
            return []
        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
            result = self._landmarker.detect_for_video(image, timestamp_ms)
        except Exception:  # noqa: BLE001 - a dropped frame is not worth dying for
            return []

        hands = []
        for i, landmarks in enumerate(result.hand_landmarks or []):
            handedness = "unknown"
            try:
                handedness = result.handedness[i][0].category_name
            except (IndexError, AttributeError):
                pass
            tip = landmarks[INDEX_TIP]
            hands.append({
                "handedness": handedness,
                # Normalised 0-1 so the overlay can scale them to any display.
                "landmarks": [[round(p.x, 4), round(p.y, 4)] for p in landmarks],
                "indexTip": [round(tip.x, 4), round(tip.y, 4)],
                "position": round(tip.x * 2 - 1, 3),
            })
        return hands


def resolve_touch(hands: list[dict], objects: list[dict], width: int, height: int) -> None:
    """Annotate each hand with the object its index fingertip is on or nearest.

    Inside the bounding box counts as touching outright; otherwise the nearest
    object within TOUCH_RADIUS_FRAC of the frame width counts as reaching for.
    Objects are annotated in turn so the overlay can highlight what is in hand.
    """
    for obj in objects:
        obj["touchedBy"] = None

    radius = width * TOUCH_RADIUS_FRAC
    for hand in hands:
        tx, ty = hand["indexTip"][0] * width, hand["indexTip"][1] * height
        hand["touching"] = None
        best, best_dist = None, radius

        for obj in objects:
            x, y, w, h = obj["bbox"]
            if x <= tx <= x + w and y <= ty <= y + h:
                best, best_dist = obj, -1.0     # inside the box wins outright
                break
            dist = math.dist((tx, ty), (obj["centroid"][0], obj["centroid"][1]))
            if dist < best_dist:
                best, best_dist = obj, dist

        if best is not None:
            hand["touching"] = best["id"]
            best["touchedBy"] = hand["handedness"]


def for_wire(objects: list[dict]) -> list[dict]:
    """Detections as they travel over the WebSocket -- centroid split into cx/cy."""
    return [
        {k: v for k, v in obj.items() if k != "centroid"} | {
            "cx": round(obj["centroid"][0], 1),
            "cy": round(obj["centroid"][1], 1),
        }
        for obj in objects
    ]
