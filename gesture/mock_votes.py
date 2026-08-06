"""Stand-in for gesture_server.py that needs no webcam and no MediaPipe.

Speaks the identical protocol on the identical port, so the bench cannot tell
the difference. Two uses:

  * Building and testing the bench half without a camera, or on a machine where
    MediaPipe has no wheel (it currently has none for Python 3.13/3.14).
  * Stage insurance. If the camera will not open thirty seconds before you
    present, run this instead and drive votes from the keyboard. The rest of the
    demo is unchanged, and the votes still route through attention and effort
    exactly as they would from a real thumbs-up.

    python gesture/mock_votes.py              # interactive: a / r / q
    python gesture/mock_votes.py --script     # timed approve/reject loop
"""

from __future__ import annotations

import argparse
import json
import threading
import time

from websockets.sync.server import serve

WS_PORT = 8765
WS_HOST = "127.0.0.1"   # votes are consumed by the bench on this laptop

clients, clients_lock = set(), threading.Lock()


def ws_handler(conn):
    with clients_lock:
        clients.add(conn)
    print(f"[ws] client connected ({len(clients)} total)")
    try:
        for _ in conn:
            pass
    finally:
        with clients_lock:
            clients.discard(conn)


def emit(vote: str, source: str = "keyboard", confidence: float = 0.93) -> None:
    event = {
        "type": "design_vote",
        "vote": vote,
        "source": source,
        "confidence": confidence,
        "ts": time.time(),
    }
    message = json.dumps(event)
    with clients_lock:
        dead = []
        for c in clients:
            try:
                c.send(message)
            except Exception:  # noqa: BLE001 - drop the client, keep serving
                dead.append(c)
        for c in dead:
            clients.discard(c)
    print("SENT:", message)


def run_script() -> None:
    """Timed loop, for leaving the demo running unattended."""
    while True:
        time.sleep(8)
        emit("approve", source="gesture")
        time.sleep(8)
        emit("reject", source="gesture")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", action="store_true",
                        help="emit alternating votes on a timer instead of reading keys")
    args = parser.parse_args()

    server = serve(
        ws_handler,
        WS_HOST,
        WS_PORT,
        origins=[None],  # non-browser local bridge only; blocks hostile webpages
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[ws] broadcasting on ws://localhost:{WS_PORT}")

    if args.script:
        run_script()
        return

    print("keys:  a = approve   r = reject   q = quit")
    try:
        while True:
            key = input("> ").strip().lower()
            if key == "a":
                emit("approve")
            elif key == "r":
                emit("reject")
            elif key in ("q", "quit", "exit"):
                break
    except (KeyboardInterrupt, EOFError):
        pass


if __name__ == "__main__":
    main()
