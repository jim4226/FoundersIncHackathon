"""HTTP + WebSocket surface for the bench.

The design rule here is that the EEG pipeline emits *semantic events* --
`focus_changed`, `flag`, `effort` -- over a WebSocket, and nothing downstream
knows or cares whether a headband, a phone, or the simulator produced them. That
decoupling is deliberate: the Prism integration, the web UI and the replay mode
all consume the identical stream, so the two halves of the team can build in
parallel and a dead headband degrades the demo instead of ending it.

Run:  python -m backend.server                (loopback, simulated subject)
      python -m backend.server --lan --https  (phone / other LAN devices)
      EEG_SOURCE=osc python -m backend.server --lan --https
      EEG_SOURCE=brainflow MUSE_BOARD_ID=39 python -m backend.server --lan --https
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import ipaddress
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
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
OPERATOR_COOKIE = "bench_operator"


@dataclass(frozen=True)
class AccessConfig:
    """Process-local access policy.

    Loopback mode intentionally keeps the zero-setup hackathon experience. LAN
    mode is explicit and gets a new high-entropy operator token on every start.
    The phone pairing token remains separate and can only open `/ws/phone`.
    """

    lan_mode: bool = False
    secure: bool = False
    port: int = 8000
    bind_host: str = "127.0.0.1"
    allowed_hosts: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
    operator_token: str | None = field(default=None, repr=False)


ACCESS = AccessConfig()


def _authority(value: str | None) -> tuple[str | None, int | None]:
    """Return a normalized (host, port) from a Host header-like value."""
    if not value:
        return None, None
    try:
        parsed = urlsplit(f"//{value}")
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None, None
    return (host.rstrip(".").lower() if host else None), port


def _configure_access(
    *,
    lan_mode: bool,
    secure: bool,
    port: int,
    advertised_host: str | None = None,
    operator_token: str | None = None,
) -> AccessConfig:
    """Install the access policy before uvicorn starts (also unit-testable)."""
    global ACCESS

    hosts = {"127.0.0.1", "localhost", "::1"}
    advertised, _ = _authority(advertised_host)
    if advertised:
        hosts.add(advertised)

    token = (operator_token or secrets.token_urlsafe(32)) if lan_mode else None
    ACCESS = AccessConfig(
        lan_mode=lan_mode,
        secure=secure,
        port=port,
        bind_host="0.0.0.0" if lan_mode else "127.0.0.1",
        allowed_hosts=frozenset(hosts),
        operator_token=token,
    )
    return ACCESS


def _host_allowed(host_header: str | None) -> bool:
    host, port = _authority(host_header)
    if host not in ACCESS.allowed_hosts:
        return False
    expected_default = 443 if ACCESS.secure else 80
    return port == ACCESS.port if port is not None else ACCESS.port == expected_default


def _origin_allowed(origin: str | None, host_header: str | None = None) -> bool:
    """Reject browser requests and sockets arriving from another web origin.

    Non-browser clients may omit Origin, but still need the operator/phone token
    and a valid Host header.
    """
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    expected_scheme = "https" if ACCESS.secure else "http"
    host = parsed.hostname.rstrip(".").lower() if parsed.hostname else None
    allowed = (
        parsed.scheme == expected_scheme
        and host in ACCESS.allowed_hosts
        and port == ACCESS.port
    )
    if not allowed or not host_header:
        return allowed
    request_host, request_port = _authority(host_header)
    if request_port is None:
        request_port = 443 if ACCESS.secure else 80
    return host == request_host and port == request_port


def _peer_is_loopback(client_host: str | None) -> bool:
    """Use the socket peer, not a spoofable header, for the local trust boundary."""
    if not client_host:
        return False
    try:
        return ipaddress.ip_address(client_host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _client_allowed(client_host: str | None) -> bool:
    """Default-deny external peers unless explicit authenticated LAN mode is active."""
    return ACCESS.lan_mode or _peer_is_loopback(client_host)


def _operator_token_matches(
    cookie_token: str | None,
    authorization: str | None,
) -> bool:
    if not ACCESS.lan_mode:
        return True
    supplied = cookie_token
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            supplied = value
    return bool(
        supplied
        and ACCESS.operator_token
        and secrets.compare_digest(supplied, ACCESS.operator_token)
    )


def _operator_access_allowed(
    *,
    client_host: str | None,
    host: str | None,
    origin: str | None,
    cookie_token: str | None,
    authorization: str | None,
) -> bool:
    return (
        _client_allowed(client_host)
        and _host_allowed(host)
        and _origin_allowed(origin, host)
        and _operator_token_matches(cookie_token, authorization)
    )


def _phone_transport_allowed(
    *, client_host: str | None, host: str | None, origin: str | None
) -> bool:
    return (
        _client_allowed(client_host)
        and _host_allowed(host)
        and _origin_allowed(origin, host)
    )


def _phone_access_allowed(
    *,
    client_host: str | None,
    host: str | None,
    origin: str | None,
    phone_token: str | None,
) -> bool:
    return _phone_transport_allowed(
        client_host=client_host, host=host, origin=origin
    ) and pairing.check(phone_token)


def _internal_access_allowed(
    *, client_host: str | None, host: str | None, origin: str | None
) -> bool:
    """Authorize the read-only helper feed by an actual loopback socket peer."""
    return (
        _peer_is_loopback(client_host)
        and _host_allowed(host)
        and origin is None
    )


def _safe_next(value: str | None) -> str:
    """Keep the post-login redirect same-origin and path-only."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or "\\" in value or "\r" in value or "\n" in value:
        return "/"
    return value

app = FastAPI(title="Bench")

_PROTECTED_PAGES = {
    "/",
    "/index.html",
    "/control",
    "/desk",
    "/project",
    "/surface",
    "/hold",
    "/docs",
    "/redoc",
    "/openapi.json",
}


@app.middleware("http")
async def _enforce_access(request: Request, call_next):
    """Apply Host/Origin checks and operator auth before state can be read."""
    if not _client_allowed(request.client.host if request.client else None):
        return JSONResponse({"error": "external access is disabled"}, status_code=403)
    if not _host_allowed(request.headers.get("host")):
        return JSONResponse({"error": "untrusted Host header"}, status_code=400)
    if not _origin_allowed(
        request.headers.get("origin"), request.headers.get("host")
    ):
        return JSONResponse({"error": "cross-origin request denied"}, status_code=403)

    if ACCESS.lan_mode:
        authorized = _operator_token_matches(
            request.cookies.get(OPERATOR_COOKIE),
            request.headers.get("authorization"),
        )
        if request.url.path.startswith("/api/") and not authorized:
            return JSONResponse(
                {"error": "operator authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if request.url.path in _PROTECTED_PAGES and not authorized:
            destination = quote(_safe_next(request.url.path), safe="/")
            return RedirectResponse(
                f"/auth/operator?next={destination}", status_code=303
            )

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response

store = ProjectStore()
estimator = EffortEstimator()
attention = LateralAttention(store.zones())
agent = ProjectAgent(store)
source = build_source(os.environ.get("EEG_SOURCE", "sim"), estimator, attention)

_clients: set[WebSocket] = set()
_internal_clients: set[WebSocket] = set()
_state: dict = {"effort": None, "attention": None, "lastFlag": None}
_last_desk_ts: float = 0.0


def _variant_summary(value: dict | None) -> dict | None:
    if not value:
        return None
    return {key: value.get(key) for key in ("id", "label")}


def _internal_payload(payload: dict) -> dict | None:
    """Reduce the operator stream to the overlay fields local helpers need."""
    kind = payload.get("type")
    if kind == "tick":
        return {
            "type": "tick",
            "effort": payload.get("effort"),
            "attention": payload.get("attention"),
            "variants": [
                _variant_summary(item) for item in payload.get("variants", [])
            ],
        }
    if kind == "focus_changed":
        return {
            "type": kind,
            "zone": payload.get("zone"),
            "variant": _variant_summary(payload.get("variant")),
        }
    if kind == "vote":
        return {
            "type": kind,
            "vote": payload.get("vote"),
            "variant": _variant_summary(payload.get("variant")),
            "binding": payload.get("binding"),
            "decisive": payload.get("decisive"),
            "promoted": _variant_summary(payload.get("promoted")),
            "reverted": _variant_summary(payload.get("reverted")),
        }
    if kind == "vote_unresolved":
        return {
            "type": kind,
            "vote": payload.get("vote"),
            "reason": payload.get("reason"),
        }
    return None


async def _send_to(clients: set[WebSocket], message: str) -> None:
    for ws in list(clients):
        try:
            await ws.send_text(message)
        except Exception:  # noqa: BLE001 - a dead client must not stall the loop
            clients.discard(ws)


async def _broadcast(payload: dict) -> None:
    if _clients:
        await _send_to(_clients, json.dumps(payload))
    internal = _internal_payload(payload)
    if internal and _internal_clients:
        await _send_to(_internal_clients, json.dumps(internal))


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


@app.get("/auth/operator", response_class=HTMLResponse)
def operator_login_form(next: str = "/") -> HTMLResponse:
    """Render a tiny local login form without putting the token in a URL."""
    destination = _safe_next(next)
    if not ACCESS.lan_mode:
        return RedirectResponse(destination, status_code=303)

    escaped_next = html.escape(destination, quote=True)
    response = HTMLResponse(
        f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Bench operator access</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;background:#08090c;color:#e8ecf4;display:grid;
place-items:center;min-height:100vh;margin:0}}main{{width:min(34rem,calc(100% - 2rem));padding:2rem;
background:#101319;border:1px solid #293040;border-radius:14px}}label,input,button{{display:block;
width:100%;box-sizing:border-box}}input,button{{font:inherit;padding:.8rem;margin-top:.5rem;border-radius:8px}}
input{{background:#08090c;color:#fff;border:1px solid #465069}}button{{margin-top:1rem;background:#45e0c0;
border:0;color:#08110f;font-weight:700;cursor:pointer}}p{{color:#aab3c3}}code{{color:#fff}}</style>
<main><h1>Bench operator access</h1><p>LAN mode protects project state and controls.
Paste the per-session operator token printed by <code>backend.server</code>.</p>
<form method="post" action="/auth/operator">
<input type="hidden" name="next" value="{escaped_next}">
<label>Operator token<input name="token" type="password" autocomplete="one-time-code"
spellcheck="false" autofocus required></label><button type="submit">Unlock this browser</button>
</form></main></html>"""
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/auth/operator")
async def operator_login(request: Request) -> Response:
    """Exchange the console token for a process-scoped, HttpOnly cookie."""
    try:
        length = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        return HTMLResponse("Invalid Content-Length.", status_code=400)
    if length > 4096:
        return HTMLResponse("Login request too large.", status_code=413)
    raw = await request.body()
    if len(raw) > 4096:  # also cap chunked requests without Content-Length
        return HTMLResponse("Login request too large.", status_code=413)
    try:
        form = parse_qs(raw.decode("utf-8"), max_num_fields=4)
    except (UnicodeDecodeError, ValueError):
        return HTMLResponse("Invalid login request.", status_code=400)

    destination = _safe_next((form.get("next") or ["/"])[0])
    if not ACCESS.lan_mode:
        return RedirectResponse(destination, status_code=303)

    supplied = (form.get("token") or [None])[0]
    if not (
        supplied
        and ACCESS.operator_token
        and secrets.compare_digest(supplied, ACCESS.operator_token)
    ):
        return HTMLResponse(
            "Operator token rejected. Return to the terminal for the current token.",
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )

    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        OPERATOR_COOKIE,
        ACCESS.operator_token,
        max_age=12 * 60 * 60,
        httponly=True,
        secure=ACCESS.secure,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


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
    if not _operator_access_allowed(
        client_host=ws.client.host if ws.client else None,
        host=ws.headers.get("host"),
        origin=ws.headers.get("origin"),
        cookie_token=ws.cookies.get(OPERATOR_COOKIE),
        authorization=ws.headers.get("authorization"),
    ):
        await ws.close(code=1008)
        return
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


@app.websocket("/ws/internal/status")
async def internal_status_socket(ws: WebSocket) -> None:
    """Read-only, reduced feedback for helper processes on this machine only."""
    if not _internal_access_allowed(
        client_host=ws.client.host if ws.client else None,
        host=ws.headers.get("host"),
        origin=ws.headers.get("origin"),
    ):
        await ws.close(code=1008)
        return
    await ws.accept()
    _internal_clients.add(ws)
    try:
        snapshot = _internal_payload({
            "type": "tick",
            "effort": _state["effort"],
            "attention": _state["attention"],
            "variants": [variant.to_dict() for variant in store.variants],
        })
        await ws.send_text(json.dumps(snapshot))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _internal_clients.discard(ws)


@app.websocket("/ws/phone")
async def phone_socket(ws: WebSocket) -> None:
    """A paired phone, streaming JPEG frames of the desk.

    The QR carries a separate, least-privilege token. It can submit camera
    frames here and nowhere else; it cannot read project state or use operator
    controls. It also expires naturally whenever the process restarts.
    """
    if not _phone_transport_allowed(
        client_host=ws.client.host if ws.client else None,
        host=ws.headers.get("host"),
        origin=ws.headers.get("origin"),
    ):
        await ws.close(code=1008)
        return
    await ws.accept()
    if not pairing.check(ws.query_params.get("k")):
        await ws.send_text(json.dumps({
            "type": "rejected",
            "reason": "invalid or stale phone pairing token",
        }))
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
    info = pairing.info(secure=request.url.scheme == "https", port=port)
    info["lanMode"] = ACCESS.lan_mode
    info["cameraAllowed"] = bool(ACCESS.lan_mode and info["cameraAllowed"])
    if not ACCESS.lan_mode:
        # Do not advertise a LAN URL that the loopback-only server cannot serve,
        # and do not expose even the least-privilege phone token unnecessarily.
        info["url"] = None
        info["token"] = None
    return info


@app.get("/api/pair")
def get_pair(request: Request) -> dict:
    """Everything the control view needs to render the pairing panel."""
    return _pair_info(request)


@app.get("/api/pair/qr.svg")
def get_pair_qr(request: Request) -> Response:
    if not ACCESS.lan_mode:
        return Response(
            content="LAN pairing is disabled; restart with --lan --https.",
            media_type="text/plain",
            status_code=409,
        )
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

    @app.get("/hold")
    def hold_view() -> FileResponse:
        """Hold it — the CAD part rendered at 1:1 into the operator's hand."""
        return FileResponse(WEB_DIR / "hold.html")


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Bench")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument(
        "--lan",
        action="store_true",
        help="explicitly expose Bench on the LAN; requires --https and protects "
             "all project state and controls with a per-session operator token",
    )
    parser.add_argument("--https", action="store_true",
                        default=os.environ.get("BENCH_HTTPS", "") not in ("", "0", "false"),
                        help="serve TLS with a self-signed cert, so a paired "
                             "phone is allowed to open its camera")
    args = parser.parse_args()

    if args.lan and not args.https:
        parser.error(
            "--lan requires --https so operator and phone tokens are not sent in cleartext"
        )

    advertised_host = pairing.lan_ip() if args.lan else None
    access = _configure_access(
        lan_mode=args.lan,
        secure=args.https,
        port=args.port,
        advertised_host=advertised_host,
    )

    ssl: dict = {}
    if args.https:
        from .tls import ensure_cert

        certfile, keyfile = ensure_cert(advertised_host or "127.0.0.1")
        ssl = {"ssl_certfile": certfile, "ssl_keyfile": keyfile}

    scheme = "https" if args.https else "http"
    local_base = f"{scheme}://localhost:{args.port}"
    print(f"[bench] control  {local_base}")
    print(f"[bench] desk     {local_base}/desk")
    if access.lan_mode:
        lan_base = f"{scheme}://{advertised_host}:{args.port}"
        print("[bench] !!! LAN MODE: project state and controls require operator auth !!!")
        print(f"[bench] operator login  {lan_base}/auth/operator")
        print(f"[bench] operator token  {access.operator_token}")
        print(f"[bench] phone           {pairing.pair_url(args.https, args.port)}")
        print("[bench] use a trusted/private network; tokens expire when this process exits")
    else:
        print("[bench] loopback-only; phone pairing is disabled")
        print("[bench] use --lan --https to pair a phone or open Bench from another device")

    uvicorn.run(app, host=access.bind_host, port=args.port, **ssl)


if __name__ == "__main__":
    main()
