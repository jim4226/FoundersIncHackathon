"""HTTP + WebSocket surface for the bench.

The design rule here is that the EEG pipeline emits *semantic events* --
`focus_changed`, `flag`, `effort` -- over a WebSocket, and nothing downstream
knows or cares whether a headband, a phone, or the simulator produced them. That
decoupling is deliberate: the Prism integration, the web UI and the replay mode
all consume the identical stream, so the two halves of the team can build in
parallel and a dead headband degrades the demo instead of ending it.

Run:  python -m backend.server            (simulated subject, default)
      EEG_SOURCE=osc python -m backend.server
      EEG_SOURCE=brainflow MUSE_BOARD_ID=39 python -m backend.server
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import boxic
from .agent import ProjectAgent
from .eeg.attention import LateralAttention
from .eeg.metrics import EffortEstimator
from .eeg.sources import SimulatedSource, build_source
from .gesture import GestureBridge
from .store import HIGH_EFFORT_THRESHOLD, LOW_EFFORT_THRESHOLD, ProjectStore

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TICK_HZ = 8.0

app = FastAPI(title="Bench")

store = ProjectStore()
estimator = EffortEstimator()
attention = LateralAttention(store.zones())
agent = ProjectAgent(store)
source = build_source(os.environ.get("EEG_SOURCE", "sim"), estimator, attention)

_clients: set[WebSocket] = set()
_state: dict = {"effort": None, "attention": None, "lastFlag": None}


async def _broadcast(payload: dict) -> None:
    if not _clients:
        return
    message = json.dumps(payload)
    for ws in list(_clients):
        try:
            await ws.send_text(message)
        except Exception:  # noqa: BLE001 - a dead client must not stall the loop
            _clients.discard(ws)


async def _tick_loop() -> None:
    """Poll the estimator, accumulate dwell, and publish. ~8 Hz."""
    interval = 1.0 / TICK_HZ
    last_zone: str | None = None
    last_tick = time.time()

    while True:
        await asyncio.sleep(interval)
        now = time.time()
        elapsed, last_tick = now - last_tick, now

        reading = estimator.compute()
        if reading is not None:
            _state["effort"] = reading.to_dict()

        att = attention.compute(estimator.buffers)
        _state["attention"] = att.to_dict()

        # Dwell accrues against whichever variant is being looked at.
        if att.zone and att.confidence > 0.35:
            store.add_attention(att.zone, elapsed)
            if att.zone != last_zone:
                last_zone = att.zone
                variant = store.variant(att.zone)
                await _broadcast({
                    "type": "focus_changed",
                    "zone": att.zone,
                    "variant": variant.to_dict() if variant else None,
                })

        for flag in estimator.drain_flags():
            enriched = store.record_flag({
                **flag,
                "zone": att.zone,
                "effort": reading.effort if reading else None,
            })
            _state["lastFlag"] = enriched
            await _broadcast({"type": "flag", "flag": enriched})

        await _broadcast({
            "type": "tick",
            "effort": _state["effort"],
            "attention": _state["attention"],
            "variants": [v.to_dict() for v in store.variants],
            "source": {"kind": source.name, "status": source.status, "error": source.error},
        })


async def _on_vote(vote: str, payload: dict) -> None:
    """Route a thumbs-up/down onto whatever the operator is currently attending to.

    The gesture supplies the verdict, the attention estimate supplies the
    referent, and the effort score decides whether the verdict binds. An approve
    while focused promotes the design; the same approve while diffuse is
    recorded and held for review, because nodding along is not deciding.
    """
    att = _state.get("attention") or {}
    effort = (_state.get("effort") or {}).get("effort")
    target_id = att.get("zone") if att.get("confidence", 0) > 0.35 else None
    variant = store.variant(target_id) if target_id else None

    if variant is None:
        await _broadcast({
            "type": "vote_unresolved", "vote": vote,
            "reason": "no design was being attended to when the vote landed",
        })
        return

    binding = effort is not None and effort >= LOW_EFFORT_THRESHOLD
    decisive = effort is not None and effort >= HIGH_EFFORT_THRESHOLD

    verb = "Approved" if vote == "approve" else "Rejected"
    body = (
        f"{verb} {variant.label} by gesture while attending to it"
        f" ({payload.get('confidence', 0):.0%} gesture confidence)."
    )
    if not binding:
        body += " Operator was diffuse — held for review rather than merged."

    store.add_contribution(
        author="you", body=body, kind="decision", effort=effort,
        flagged=False, artifact_id=None,
        status="merged" if binding else "challenged",
    )

    promoted = None
    if vote == "approve" and decisive:
        promoted = store.promote_variant(variant.id)
        attention.set_zones(store.zones())

    await _broadcast({
        "type": "vote", "vote": vote, "variant": variant.to_dict(),
        "effort": effort, "binding": binding, "decisive": decisive,
        "promoted": promoted.to_dict() if promoted else None,
        "project": store.to_dict(),
    })


gestures = GestureBridge(_on_vote)


@app.on_event("startup")
async def _startup() -> None:
    source.start()
    gestures.start()
    app.state.tick = asyncio.create_task(_tick_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    task = getattr(app.state, "tick", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await gestures.stop()
    source.stop()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "project", "project": store.to_dict()}))
        while True:
            await ws.receive_text()   # client is send-only; this just holds the socket
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


# ------------------------------------------------------------------ REST


class AgentRequest(BaseModel):
    message: str
    author: str = "you"
    override_effort: float | None = None
    flagged: bool = False


@app.get("/api/project")
def get_project() -> dict:
    return store.to_dict()


@app.get("/api/boxic/export")
def boxic_export() -> dict:
    """The session as a Boxic project document, ready to import."""
    return boxic.export_document(store)


@app.get("/api/state")
def get_state() -> dict:
    return {**_state, "source": {"kind": source.name, "status": source.status}}


@app.post("/api/agent")
async def post_agent(req: AgentRequest) -> dict:
    """Send something to the agent, scored by the state you were in.

    The effort attached is the one measured *now*, at the moment of submission --
    not an average over the session. That is the whole mechanism: the same
    sentence gets a different response depending on who you were when you wrote it.
    """
    effort = req.override_effort
    if effort is None:
        effort = (_state.get("effort") or {}).get("effort")

    flagged = req.flagged
    last = _state.get("lastFlag")
    if last and time.time() - last.get("timestamp", 0) < 8.0:
        flagged = True     # a recent deliberate flag applies to this submission

    contribution = store.add_contribution(
        author=req.author, body=req.message, kind="change",
        effort=effort, flagged=flagged,
    )
    result = await asyncio.to_thread(agent.respond, req.message, effort, flagged)
    store.attach_agent_response(contribution.id, result["text"])
    updated = store.set_status(
        contribution.id, "challenged" if result["regime"] == "DIFFUSE" else "merged"
    )

    payload = {"contribution": (updated or contribution).to_dict(), "agent": result}
    await _broadcast({"type": "agent_response", **payload})
    return payload


@app.post("/api/variants/{variant_id}/promote")
async def promote(variant_id: str) -> dict:
    variant = store.promote_variant(variant_id)
    if variant is None:
        return {"error": "unknown variant"}
    store.add_contribution(
        author="you", body=f"Promoted {variant.label} to the main branch.",
        kind="decision", effort=(_state.get("effort") or {}).get("effort"),
        flagged=True, status="merged",
    )
    attention.set_zones(store.zones())
    await _broadcast({"type": "promoted", "variant": variant.to_dict(),
                      "project": store.to_dict()})
    return variant.to_dict()


# ------------------------------------------------- simulator stage controls


class SimRequest(BaseModel):
    effort: float | None = None
    gaze: float | None = None


@app.post("/api/sim")
def post_sim(req: SimRequest) -> dict:
    """Drive the synthetic subject. Only active when EEG_SOURCE=sim."""
    if not isinstance(source, SimulatedSource):
        return {"error": f"source is {source.name}, not sim"}
    if req.effort is not None:
        source.set_effort(req.effort)
    if req.gaze is not None:
        source.look_at(req.gaze)
    return {"effort": source.target_effort, "gaze": source.gaze_target}


@app.post("/api/sim/blink")
def post_sim_blink() -> dict:
    if isinstance(source, SimulatedSource):
        source.request_blink()
    return {"ok": True}


@app.post("/api/sim/clench")
def post_sim_clench() -> dict:
    if isinstance(source, SimulatedSource):
        source.request_clench()
    return {"ok": True}


# ------------------------------------------------------------------ static

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
