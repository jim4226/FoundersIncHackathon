"""The phone as the desk camera.

Scan a QR code on the control screen, prop the phone over the desk, done. No
second webcam, no camera indices to argue about, no driver that only works on
one of the two laptops in the room. The phone opens `/phone`, streams JPEG
frames over `ws://.../ws/phone`, and this module runs the same detection the
overhead webcam path runs (`backend/vision.py`) and emits the identical
`desk_frame` payload.

That last part is the point: nothing downstream learns a new event shape.
`DeskBridge` frames and phone frames arrive at the same callback, bind to the
same variants, and drive the same overlay. The phone is a camera the bench
cannot distinguish from the one it already had.

The reference frame -- the picture of the empty desk that everything is
subtracted from -- is captured on request from the phone (or the control view),
and once automatically as soon as the stream holds still, so a demo that nobody
calibrated still shows something rather than nothing.
"""

from __future__ import annotations

import base64
import time
from typing import Awaitable, Callable

import numpy as np

from . import vision

AUTO_CALIBRATE_AFTER_S = 2.0   # give the operator time to aim before self-calibrating
STALE_AFTER_S = 2.5            # no frame this long and the phone is treated as gone


class PhoneCamera:
    """Decodes phone frames into desk objects.

    `on_frame(payload)` is awaited per frame with a `desk_frame`-shaped dict, so
    a phone and the overhead webcam are interchangeable upstream of it.
    """

    def __init__(self, on_frame: Callable[[dict], Awaitable[None]]):
        self.on_frame = on_frame
        self.status = "idle"
        self.error: str | None = None
        self.calibrated = False
        self.objects: list[dict] = []
        self.last_jpeg: str | None = None
        self.last_frame_at = 0.0
        self.frames = 0
        self.device: str | None = None

        self._tracker = vision.Tracker()
        self._reference: np.ndarray | None = None
        self._previous_gray: np.ndarray | None = None
        self._recalibrate = False
        self._connected_at = 0.0

    # -------------------------------------------------------------- lifecycle

    @property
    def live(self) -> bool:
        """Streaming now. While true the phone owns the desk pipeline."""
        return self.status == "streaming" and time.time() - self.last_frame_at < STALE_AFTER_S

    def connected(self, device: str | None = None) -> None:
        self.status = "connected"
        self.device = device
        self.error = None
        self._connected_at = time.time()
        self._previous_gray = None

    def disconnected(self, reason: str | None = None) -> None:
        self.status = "idle"
        self.error = reason
        self.objects = []
        self._previous_gray = None

    def calibrate(self) -> None:
        """Take the next frame as the empty desk."""
        self._recalibrate = True

    # ----------------------------------------------------------------- frames

    async def handle_frame(self, message: dict) -> None:
        jpeg_b64 = message.get("jpeg")
        if not jpeg_b64:
            return

        frame = _decode(jpeg_b64)
        if frame is None:
            self.error = "undecodable frame"
            return

        # The desk view draws boxes in the frame's own pixel space, so the image
        # it displays has to be the image the boxes were computed on.
        if frame.shape[1] != vision.FRAME_W or frame.shape[0] != vision.FRAME_H:
            import cv2

            frame = cv2.resize(frame, (vision.FRAME_W, vision.FRAME_H))
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, vision.JPEG_QUALITY])
            jpeg_b64 = base64.b64encode(buf).decode("ascii") if ok else jpeg_b64

        gray = vision.to_gray(frame)
        now = time.time()

        if self._recalibrate:
            self._reference = gray.copy()
            self._tracker.reset()
            self._recalibrate = False
            self.calibrated = True
        elif self._reference is None and now - self._connected_at > AUTO_CALIBRATE_AFTER_S:
            # Only once the phone has stopped moving: a reference captured while
            # someone is still positioning the phone is a reference of a blur,
            # and every subsequent frame then looks like one enormous object.
            if vision.is_still(gray, self._previous_gray):
                self._reference = gray.copy()
                self.calibrated = True

        self._previous_gray = gray

        objects = (self._tracker.update(vision.detect(frame, self._reference), vision.FRAME_W)
                   if self._reference is not None else [])

        self.status = "streaming"
        self.last_frame_at = now
        self.frames += 1
        self.objects = objects
        self.last_jpeg = jpeg_b64

        await self.on_frame({
            "type": "desk_frame",
            "ts": now,
            "origin": "phone",
            "calibrated": self._reference is not None,
            "width": vision.FRAME_W,
            "height": vision.FRAME_H,
            "objects": vision.for_wire(objects),
            "jpeg": jpeg_b64,
        })

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "live": self.live,
            "calibrated": self.calibrated,
            "objects": len(self.objects),
            "frames": self.frames,
            "device": self.device,
            "error": self.error,
        }


def _decode(jpeg_b64: str) -> np.ndarray | None:
    """base64 JPEG (with or without a data: prefix) to BGR."""
    if "," in jpeg_b64[:64]:
        jpeg_b64 = jpeg_b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(jpeg_b64, validate=False)
    except Exception:  # noqa: BLE001 - a mangled frame must not kill the socket
        return None

    import cv2

    frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    return frame if frame is not None and frame.size else None
