"""Pairing a phone: the LAN address, the token, and the QR code that carries both.

The whole interaction is meant to be one scan. The control view renders a QR of

    https://<lan-ip>:8000/phone?k=<token>

the presenter points a phone at it, the phone opens the camera page and starts
streaming. Nothing is typed and nothing is installed.

Two details that decide whether this works in a real room:

  * **The address has to be the LAN one.** `localhost` is meaningless to the
    phone, and `gethostname()` resolves to 127.0.0.1 on most Linux boxes. The
    reliable trick is to open a UDP socket towards a public address and ask the
    kernel which interface it picked -- no packet is ever sent.
  * **`getUserMedia` needs a secure context.** Browsers refuse camera access on
    `http://192.168.x.x`, so the advertised scheme follows whether the server
    was started with TLS (see `backend/tls.py`). Over plain HTTP the phone page
    will load and then be denied the camera, which is why the control view says
    so out loud rather than letting the presenter discover it on stage.
"""

from __future__ import annotations

import os
import secrets
import socket

# Separate from the operator token: this grants only camera-frame submission to
# `/ws/phone`. Twenty-four random bytes keep the default safe even on a noisy,
# shared venue network; the value changes whenever the process restarts.
TOKEN = os.environ.get("BENCH_PAIR_TOKEN") or secrets.token_urlsafe(24)


def lan_ip() -> str:
    """The address of the interface that would reach the outside world."""
    override = os.environ.get("BENCH_HOST")
    if override:
        return override

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))     # no packet leaves; this just picks a route
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def pair_url(secure: bool, port: int) -> str:
    scheme = "https" if secure else "http"
    return f"{scheme}://{lan_ip()}:{port}/phone?k={TOKEN}"


def check(token: str | None) -> bool:
    """Constant-time token comparison. Cheap, and keeps a stray device out."""
    return bool(token) and secrets.compare_digest(token, TOKEN)


def qr_svg(data: str, scale: int = 6) -> str:
    """The pairing URL as an inline SVG QR.

    Dark modules on a white card rather than the other way round: inverted
    codes are legal but plenty of phone scanners are slower at them, and this
    one is being read across a room under stage lighting.
    """
    import io

    import segno

    buf = io.BytesIO()
    segno.make(data, error="m").save(
        buf, kind="svg", scale=scale, border=2,
        dark="#08090c", light="#ffffff", svgclass=None, lineclass=None,
    )
    return buf.getvalue().decode("utf-8")


def info(secure: bool, port: int) -> dict:
    url = pair_url(secure, port)
    return {
        "url": url,
        "ip": lan_ip(),
        "port": port,
        "secure": secure,
        "token": TOKEN,
        # Over plain HTTP the phone will load the page and then be refused the
        # camera by the browser, so the UI needs to know before the scan.
        "cameraAllowed": secure,
    }
