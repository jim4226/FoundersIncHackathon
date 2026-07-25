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

# Two frames this similar mean nothing in shot is moving -- used to decide when
# a phone being propped over the desk has come to rest.
STILL_THRESHOLD = 2.2


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


def for_wire(objects: list[dict]) -> list[dict]:
    """Detections as they travel over the WebSocket -- centroid split into cx/cy."""
    return [
        {k: v for k, v in obj.items() if k != "centroid"} | {
            "cx": round(obj["centroid"][0], 1),
            "cy": round(obj["centroid"][1], 1),
        }
        for obj in objects
    ]
