"""Bridge to the desk camera.

`desk/desk_server.py` broadcasts what it sees on ws://localhost:8766:

    {"type": "desk_frame", "calibrated": true, "objects": [
       {"id": "obj_1", "label": "Object 1", "position": -0.58, "bbox": [...]}, ...]}

`position` is the object's normalised left-to-right placement on [-1, +1] --
the same axis `LateralAttention` reports gaze on. That is the whole join: the
camera says WHERE things are, the headband says WHICH ONE you are attending to,
and neither needs to know about the other.

Objects on the desk are bound to design variants in order, left to right. When
the camera sees fewer objects than the project has variants, the extras keep
their last known position rather than snapping to centre -- a hand passing over
the desk briefly hides an object, and the highlight should not jump because
someone reached across.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Awaitable, Callable

DESK_WS_URL = os.environ.get("DESK_WS_URL", "ws://localhost:8766")
RECONNECT_DELAY_S = 3.0


class DeskBridge:
    """Subscribes to the desk camera and hands frames to a callback.

    `on_frame(payload)` is awaited for each frame. The latest JPEG is kept here
    so the bench can relay it to the desk view without every client opening its
    own connection to the camera process.
    """

    def __init__(self, on_frame: Callable[[dict], Awaitable[None]], url: str | None = None):
        self.url = url or DESK_WS_URL
        self.on_frame = on_frame
        self.status = "idle"
        self.calibrated = False
        self.objects: list[dict] = []
        self.hands: list[dict] = []
        self.hands_available = False
        self.last_jpeg: str | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            try:
                await self._consume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the bench outlives the camera
                self.status = f"disconnected ({type(exc).__name__})"
            await asyncio.sleep(RECONNECT_DELAY_S)

    async def _consume(self) -> None:
        import websockets

        self.status = "connecting"
        async with websockets.connect(self.url, open_timeout=5, max_size=16 * 1024 * 1024) as ws:
            self.status = "connected"
            async for raw in ws:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") != "desk_frame":
                    continue
                self.calibrated = bool(payload.get("calibrated"))
                self.objects = payload.get("objects") or []
                self.hands = payload.get("hands") or []
                self.hands_available = bool(payload.get("handsAvailable"))
                self.last_jpeg = payload.get("jpeg")
                await self.on_frame(payload)


def handled_variant(objects: list[dict], hands: list[dict], variants: list) -> str | None:
    """Which variant a hand is currently on, if any.

    The hand is a far more direct referent than inferred gaze: reaching for a
    thing is unambiguous in a way that looking in its direction is not. The
    camera resolves WHAT, which leaves the headband only to say how much it
    mattered -- and the two agreeing is a stronger signal than either alone.
    """
    touched = {h.get("touching") for h in hands if h.get("touching")}
    if not touched:
        return None

    live = [v for v in variants if v.status != "archived"]
    ordered = sorted(objects, key=lambda o: o.get("position", 0.0))
    for index, obj in enumerate(ordered):
        if obj.get("id") in touched and index < len(live):
            return live[index].id
    return None


def bind_objects_to_variants(objects: list[dict], variants: list) -> dict[str, float]:
    """Map detected objects onto variant ids, left to right.

    Returns the zone map `LateralAttention` consumes. Variants with no matching
    object keep their configured position, so a momentarily occluded object does
    not drag the highlight across the table.
    """
    live = [v for v in variants if v.status != "archived"]
    ordered = sorted(objects, key=lambda o: o.get("position", 0.0))

    zones: dict[str, float] = {}
    for index, variant in enumerate(live):
        if index < len(ordered):
            zones[variant.id] = float(ordered[index].get("position", variant.position))
        else:
            zones[variant.id] = variant.position
    return zones
