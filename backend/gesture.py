"""Bridge to the gesture module's vote stream.

`gesture/gesture_server.py` runs a MediaPipe thumbs-up/down recogniser and
broadcasts votes on ws://localhost:8765:

    {"type": "design_vote", "vote": "approve", "source": "gesture",
     "confidence": 0.94, "ts": 1690000000.0}

A vote on its own is ambiguous, and that ambiguity is the whole reason the two
halves of this system belong together. A thumbs-up says WHAT the verdict is. It
does not say WHICH of the things on the table it applies to, and it does not say
whether the person meant it. So:

    gesture  ->  the verdict          (approve / reject)
    attention ->  the referent        (which design you were looking at)
    effort    ->  whether it binds    (considered, or just a reflex)

An approve while focused promotes the design. The same approve while diffuse is
recorded but held for review rather than merged -- you nodded along, you didn't
decide. That distinction is invisible to a camera and trivial for the headband.

The bridge reconnects on its own, because the gesture process is started and
stopped independently and a dead socket must never take the bench down with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Awaitable, Callable

GESTURE_WS_URL = os.environ.get("GESTURE_WS_URL", "ws://localhost:8765")
RECONNECT_DELAY_S = 3.0


class GestureBridge:
    """Subscribes to the gesture server and hands votes to a callback.

    `on_vote(vote, payload)` is awaited for each confirmed vote, where `vote` is
    "approve" or "reject". Connection state is exposed for the UI so a missing
    gesture process reads as a labelled degraded mode rather than silence.
    """

    def __init__(self, on_vote: Callable[[str, dict], Awaitable[None]], url: str | None = None):
        self.url = url or GESTURE_WS_URL
        self.on_vote = on_vote
        self.status = "idle"
        self.last_vote: dict | None = None
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
            except Exception as exc:  # noqa: BLE001 - the bench outlives the bridge
                self.status = f"disconnected ({type(exc).__name__})"
            await asyncio.sleep(RECONNECT_DELAY_S)

    async def _consume(self) -> None:
        import websockets

        self.status = "connecting"
        async with websockets.connect(self.url, open_timeout=5) as ws:
            self.status = "connected"
            async for raw in ws:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") != "design_vote":
                    continue
                vote = payload.get("vote")
                if vote not in ("approve", "reject"):
                    continue
                self.last_vote = payload
                await self.on_vote(vote, payload)
