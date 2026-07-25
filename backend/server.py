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

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import boxic, pairing
from .agent import ProjectAgent
from .eeg.attention import LateralAttention
from .eeg.metrics import EffortEstimator
from .desk import DeskBridge, bind_objects_to_variants, handled_variant
from .eeg.sources import SimulatedSource, build_source
from .gesture import GestureBridge
from .phone import PhoneCamera
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
_last_desk_ts: float = 0.0


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
            "gesture": {"status": gestures.status},
            "desk": ({"status": "phone", "calibrated": phone.calibrated,
                      "objects": len(phone.objects), "origin": "phone",
                      "hands": len(phone.hands), "handsAvailable": phone.hands_available}
                     if phone.live else
                     {"status": desk.status, "calibrated": desk.calibrated,
                      "objects": len(desk.objects), "origin": "camera",
                      "hands": len(desk.hands), "handsAvailable": desk.hands_available}),
            "phone": phone.to_dict(),
        })


async def _on_vote(vote: str, payload: dict) -> None:
    """Route a thumbs-up/down onto whatever the operator is currently attending to.

    The gesture supplies the verdict, the attention estimate supplies the
    referent, and the effort score decides whether the verdict binds. A
    considered approve promotes the design; a considered reject sends it back to
    the drawing board. The same vote cast while diffuse is recorded and held for
    review either way, because nodding (or shaking) along is not deciding.
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

    # Only a considered vote moves the design through its lifecycle. Approve ->
    # promoted, reject -> back to design. A diffuse vote is recorded but inert.
    promoted = reverted = None
    if decisive and vote == "approve":
        promoted = store.promote_variant(variant.id)
        attention.set_zones(store.zones())
    elif decisive and vote == "reject":
        reverted = store.revert_variant(variant.id)
        attention.set_zones(store.zones())

    verb = "Approved" if vote == "approve" else "Rejected"
    body = (
        f"{verb} {variant.label} by gesture while attending to it"
        f" ({payload.get('confidence', 0):.0%} gesture confidence)."
    )
    if reverted:
        body += " Sent back to design — back to the drawing board."
    if not binding:
        # Unmeasured is not the same claim as diffuse. Saying "the operator was
        # diffuse" when the headband had not produced a reading yet would be
        # asserting a measurement we never took.
        body += (
            " No effort reading yet — held for review rather than merged."
            if effort is None else
            " Operator was diffuse — held for review rather than merged."
        )

    store.add_contribution(
        author="you", body=body, kind="decision", effort=effort,
        flagged=False, variant_id=variant.id,
        status="merged" if binding else "challenged",
    )

    await _broadcast({
        "type": "vote", "vote": vote, "variant": variant.to_dict(),
        "effort": effort, "binding": binding, "decisive": decisive,
        "promoted": promoted.to_dict() if promoted else None,
        "reverted": reverted.to_dict() if reverted else None,
        "project": store.to_dict(),
    })


gestures = GestureBridge(_on_vote)


async def _ingest_desk_frame(payload: dict) -> None:
    """Re-anchor the attention zones onto where the objects actually are.

    The camera says where things sit on the desk; the headband says which one is
    being attended to. Binding them here means the EEG resolves against real
    physical placement instead of hardcoded positions, and moving an object
    across the table moves its zone with it.

    Both cameras land here -- the overhead webcam via `DeskBridge` and a paired
    phone via `PhoneCamera` -- because they produce the identical frame shape.
    Nothing below this function knows which one is on the desk today.
    """
    objects = payload.get("objects") or []
    hands = payload.get("hands") or []
    if objects:
        attention.set_zones(bind_objects_to_variants(objects, store.variants))

    # Handling time accrues to whichever design a hand is actually on. Kept
    # separate from attention: looking at something and picking it up are
    # different kinds of interest, and collapsing them would overstate both.
    global _last_desk_ts
    now = time.time()
    elapsed = min(0.5, now - _last_desk_ts) if _last_desk_ts else 0.0
    _last_desk_ts = now

    held = handled_variant(objects, hands, store.variants)
    if held and elapsed:
        store.add_interaction(held, elapsed)

    await _broadcast({
        "type": "desk",
        "origin": payload.get("origin", "camera"),
        "calibrated": payload.get("calibrated", False),
        "objects": objects,
        "hands": hands,
        "handsAvailable": payload.get("handsAvailable", False),
        "handledVariant": held,
        "width": payload.get("width"),
        "height": payload.get("height"),
        "jpeg": payload.get("jpeg"),
    })


async def _on_desk_frame(payload: dict) -> None:
    """Webcam frames, ignored while a phone is streaming.

    Both cameras can be running at once -- the webcam service does not know the
    phone exists -- and two sources fighting over one overlay is a flicker the
    audience would see. The phone wins because someone deliberately scanned a
    code to put it there; the webcam takes back over on its own a couple of
    seconds after the phone drops.
    """
    if phone.live:
        return
    await _ingest_desk_frame(payload)


desk = DeskBridge(_on_desk_frame)
phone = PhoneCamera(_ingest_desk_frame)


@app.on_event("startup")
async def _startup() -> None:
    source.start()
    gestures.start()
    desk.start()
    app.state.tick = asyncio.create_task(_tick_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    task = getattr(app.state, "tick", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await gestures.stop()
    await desk.stop()
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


@app.websocket("/ws/phone")
async def phone_socket(ws: WebSocket) -> None:
    """A paired phone, streaming JPEG frames of the desk.

    The token is the one the QR code carried. It is not a security boundary --
    anyone on the LAN who can read the presenter's screen has it -- but it does
    stop a phone still holding a code from a previous run from quietly taking
    over the desk feed mid-demo.
    """
    await ws.accept()
    if not pairing.check(ws.query_params.get("k")):
        await ws.send_text(json.dumps({"type": "rejected", "reason": "bad pairing token"}))
        await ws.close(code=1008)
        return

    phone.connected()
    try:
        while True:
            message = json.loads(await ws.receive_text())
            kind = message.get("type")

            if kind == "frame":
                await phone.handle_frame(message)
                await ws.send_text(json.dumps({
                    "type": "phone_state",
                    "calibrated": phone.calibrated,
                    "objects": len(phone.objects),
                }))
            elif kind == "calibrate":
                phone.calibrate()
            elif kind == "hello":
                phone.device = str(message.get("device"))[:120]
    except (WebSocketDisconnect, json.JSONDecodeError):
        pass
    finally:
        phone.disconnected()


# ------------------------------------------------------------------ REST


def _pair_info(request: Request) -> dict:
    """Scheme and port taken from the request the control view itself made.

    Whether the bench is behind TLS decides whether the phone will be allowed
    to open its camera at all, and the honest source of that answer is the
    connection the browser is already using -- not a flag we hope matches.
    """
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    return pairing.info(secure=request.url.scheme == "https", port=port)


@app.get("/api/pair")
def get_pair(request: Request) -> dict:
    """Everything the control view needs to render the pairing panel."""
    return _pair_info(request)


@app.get("/api/pair/qr.svg")
def get_pair_qr(request: Request) -> Response:
    svg = pairing.qr_svg(_pair_info(request)["url"])
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/desk/calibrate")
def post_desk_calibrate() -> dict:
    """Take the next phone frame as the empty desk.

    The equivalent for the webcam service is pressing `c` in its preview window;
    the phone has no keyboard, so the reference is captured from here or from
    the button on the phone page itself.
    """
    phone.calibrate()
    return {"ok": True, "source": "phone" if phone.live else "idle"}


class AgentRequest(BaseModel):
    message: str
    author: str = "you"
    override_effort: float | None = None
    flagged: bool = False
    # "change" writes to the project and is gated on effort; "ask" reads it and
    # is not. Reads must not be recorded as decisions -- logging every question
    # as a change would corrupt the record this system exists to keep.
    intent: str = "change"


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
    if req.intent == "ask":
        result = await asyncio.to_thread(agent.answer, req.message)
        payload = {"contribution": None, "agent": result, "question": req.message}
        await _broadcast({"type": "agent_answer", **payload})
        return payload

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
        flagged=True, variant_id=variant.id, status="merged",
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
        """Control view — the operator's screen."""
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/surface")
    def surface_view() -> FileResponse:
        """Boxic Surface — the projected table interface."""
        return FileResponse(WEB_DIR / "surface.html")

    @app.get("/about")
    def about_view() -> FileResponse:
        """The explainer, which is the front door on the hosted copy."""
        return FileResponse(WEB_DIR / "landing.html")

    @app.get("/desk")
    def desk_view() -> FileResponse:
        """Desk view — the second screen, camera plus overlay."""
        return FileResponse(WEB_DIR / "desk.html")

    @app.get("/project")
    def project_view() -> FileResponse:
        """The record — what the bench session actually produced."""
        return FileResponse(WEB_DIR / "project.html")

    @app.get("/phone")
    def phone_view() -> FileResponse:
        """Phone view — what the QR code opens. Streams the desk to the bench."""
        return FileResponse(WEB_DIR / "phone.html")


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Bench")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--https", action="store_true",
                        default=os.environ.get("BENCH_HTTPS", "") not in ("", "0", "false"),
                        help="serve TLS with a self-signed cert, so a paired "
                             "phone is allowed to open its camera")
    args = parser.parse_args()

    ssl: dict = {}
    if args.https:
        from .tls import ensure_cert

        certfile, keyfile = ensure_cert(pairing.lan_ip())
        ssl = {"ssl_certfile": certfile, "ssl_keyfile": keyfile}

    scheme = "https" if args.https else "http"
    print(f"[bench] control  {scheme}://localhost:{args.port}")
    print(f"[bench] desk     {scheme}://localhost:{args.port}/desk")
    print(f"[bench] phone    {pairing.pair_url(args.https, args.port)}")
    if not args.https:
        print("[bench] note: phone camera needs --https (browsers refuse "
              "getUserMedia on a plain-http LAN address)")

    uvicorn.run(app, host="0.0.0.0", port=args.port, **ssl)


if __name__ == "__main__":
    main()
