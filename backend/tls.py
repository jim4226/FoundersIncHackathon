"""A self-signed certificate, so the phone is allowed to open its camera.

Browsers only hand `getUserMedia` to a secure context. `localhost` counts;
`http://192.168.1.24:8000` does not, on any current mobile browser. So the
phone-camera path needs HTTPS, and on a hackathon LAN there is no certificate
authority to ask -- hence a self-signed one, generated on first run.

The phone will show a warning once ("this connection is not private"), the
presenter taps through, and the camera works from then on. That is the whole
cost, and it beats every alternative: a tunnel needs internet the venue may not
give you, and a real certificate needs a domain pointed at a laptop.

Generated with the `openssl` binary rather than a Python TLS library, because
openssl is already on every machine this will run on and `cryptography` is
another wheel to install at 3am.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CERT_DIR = Path(__file__).resolve().parent.parent / ".bench-certs"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"
STAMP_FILE = CERT_DIR / "issued-for"     # the LAN IP the current cert names


def ensure_cert(ip: str) -> tuple[str, str]:
    """Return (certfile, keyfile), generating them if missing or stale.

    The certificate names the LAN IP in its SAN, so it is regenerated whenever
    the laptop lands on a different network -- which at a hackathon is often.
    """
    CERT_DIR.mkdir(exist_ok=True)
    issued_for = STAMP_FILE.read_text().strip() if STAMP_FILE.exists() else None

    if CERT_FILE.exists() and KEY_FILE.exists() and issued_for == ip:
        return str(CERT_FILE), str(KEY_FILE)

    san = f"subjectAltName=IP:{ip},IP:127.0.0.1,DNS:localhost"
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
        "-days", "365", "-subj", "/CN=bench", "-addext", san,
        # Some packaged OpenSSL builds point at a nonexistent global config.
        # Every field we need is explicit, so an empty platform-native config
        # keeps certificate generation portable (NUL on Windows, /dev/null on
        # POSIX) without inheriting machine-wide settings.
        "-config", os.devnull,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "openssl is not on PATH, so the HTTPS certificate cannot be made. "
            "Install openssl to use authenticated LAN/phone mode, or run the "
            "bench loopback-only without --lan."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"openssl failed to generate a certificate: {detail[0]}") from exc

    STAMP_FILE.write_text(ip)
    print(f"[tls] self-signed certificate for {ip} in {CERT_DIR}")
    return str(CERT_FILE), str(KEY_FILE)
