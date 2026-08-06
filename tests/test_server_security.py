from __future__ import annotations

import asyncio
import ast
import json
import logging
import threading
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from websockets.sync.client import connect
from websockets.sync.server import serve

from backend import pairing, server


class ServerSecurityTests(unittest.TestCase):
    def tearDown(self) -> None:
        server._configure_access(lan_mode=False, secure=False, port=8000)

    def _configure_lan(self) -> str:
        token = "operator-token-that-is-distinct-from-the-phone-token"
        server._configure_access(
            lan_mode=True,
            secure=True,
            port=8443,
            advertised_host="192.168.50.12",
            operator_token=token,
        )
        return token

    @staticmethod
    async def _websocket_messages(
        path: str,
        *,
        cookie: str | None = None,
        token: str | None = None,
        client_host: str = "192.168.50.20",
        host: str = "192.168.50.12:8443",
        origin: str | None = "https://192.168.50.12:8443",
    ) -> list[dict]:
        headers = [(b"host", host.encode())]
        if origin:
            headers.append((b"origin", origin.encode()))
        if cookie:
            headers.append((b"cookie", f"{server.OPERATOR_COOKIE}={cookie}".encode()))
        incoming = iter(
            [
                {"type": "websocket.connect"},
                {"type": "websocket.disconnect", "code": 1000},
            ]
        )
        sent: list[dict] = []

        async def receive() -> dict:
            try:
                return next(incoming)
            except StopIteration:
                return {"type": "websocket.disconnect", "code": 1000}

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "wss",
            "server": ("192.168.50.12", 8443),
            "client": (client_host, 55000),
            "root_path": "",
            "path": path,
            "raw_path": path.encode(),
            "query_string": (f"k={token}" if token else "").encode(),
            "headers": headers,
            "subprotocols": [],
            "state": {},
        }
        await server.app(scope, receive, send)
        return sent

    @staticmethod
    async def _http_messages(
        path: str,
        *,
        client_host: str,
        host: str,
    ) -> list[dict]:
        incoming = iter(
            [{"type": "http.request", "body": b"", "more_body": False}]
        )
        sent: list[dict] = []

        async def receive() -> dict:
            try:
                return next(incoming)
            except StopIteration:
                return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
            "client": (client_host, 55000),
            "root_path": "",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", host.encode())],
            "state": {},
        }
        await server.app(scope, receive, send)
        return sent

    def test_loopback_is_the_default_and_needs_no_token(self) -> None:
        access = server._configure_access(
            lan_mode=False, secure=False, port=8000
        )
        self.assertEqual(access.bind_host, "127.0.0.1")
        self.assertIsNone(access.operator_token)
        self.assertTrue(server._host_allowed("localhost:8000"))
        self.assertTrue(server._origin_allowed("http://127.0.0.1:8000"))
        self.assertFalse(server._host_allowed("anything.invalid:8000"))
        self.assertFalse(server._origin_allowed("https://anything.invalid"))
        self.assertTrue(server._operator_token_matches(None, None))

    def test_lan_mode_generates_a_strong_per_process_operator_token(self) -> None:
        first = server._configure_access(
            lan_mode=True,
            secure=True,
            port=8443,
            advertised_host="192.168.50.12",
        )
        second = server._configure_access(
            lan_mode=True,
            secure=True,
            port=8443,
            advertised_host="192.168.50.12",
        )
        self.assertEqual(first.bind_host, "0.0.0.0")
        self.assertGreaterEqual(len(first.operator_token or ""), 43)
        self.assertNotEqual(first.operator_token, second.operator_token)

    def test_lan_host_and_origin_are_restricted(self) -> None:
        self._configure_lan()
        self.assertTrue(server._host_allowed("192.168.50.12:8443"))
        self.assertTrue(server._host_allowed("localhost:8443"))
        self.assertFalse(server._host_allowed("evil.example:8443"))
        self.assertFalse(server._host_allowed("192.168.50.12:8000"))

        self.assertTrue(server._origin_allowed("https://192.168.50.12:8443"))
        self.assertTrue(server._origin_allowed("https://localhost:8443"))
        self.assertTrue(server._origin_allowed(None))  # non-browser client
        self.assertFalse(server._origin_allowed("http://192.168.50.12:8443"))
        self.assertFalse(server._origin_allowed("https://evil.example:8443"))
        self.assertFalse(server._origin_allowed("null"))
        self.assertFalse(
            server._origin_allowed(
                "https://localhost:8443", "192.168.50.12:8443"
            )
        )

    def test_operator_cookie_or_bearer_is_required_for_state_access(self) -> None:
        token = self._configure_lan()
        common = {
            "client_host": "192.168.50.20",
            "host": "192.168.50.12:8443",
            "origin": "https://192.168.50.12:8443",
        }
        self.assertFalse(
            server._operator_access_allowed(
                **common, cookie_token=None, authorization=None
            )
        )
        self.assertTrue(
            server._operator_access_allowed(
                **common, cookie_token=token, authorization=None
            )
        )
        self.assertTrue(
            server._operator_access_allowed(
                **common, cookie_token=None, authorization=f"Bearer {token}"
            )
        )
        self.assertFalse(
            server._operator_access_allowed(
                **common,
                cookie_token=token,
                authorization="Bearer deliberately-wrong",
            )
        )

    def test_phone_token_is_strong_separate_and_camera_only(self) -> None:
        operator_token = self._configure_lan()
        common = {
            "client_host": "192.168.50.20",
            "host": "192.168.50.12:8443",
            "origin": "https://192.168.50.12:8443",
        }
        self.assertGreaterEqual(len(pairing.TOKEN), 32)
        self.assertNotEqual(pairing.TOKEN, operator_token)
        self.assertTrue(
            server._phone_access_allowed(**common, phone_token=pairing.TOKEN)
        )
        self.assertFalse(
            server._phone_access_allowed(**common, phone_token=operator_token)
        )
        self.assertFalse(
            server._operator_access_allowed(
                **common, cookie_token=pairing.TOKEN, authorization=None
            )
        )

    def test_websocket_endpoints_enforce_separate_tokens(self) -> None:
        operator_token = self._configure_lan()

        denied_main = asyncio.run(self._websocket_messages("/ws"))
        self.assertEqual(denied_main[0]["type"], "websocket.close")
        accepted_main = asyncio.run(
            self._websocket_messages("/ws", cookie=operator_token)
        )
        self.assertEqual(accepted_main[0]["type"], "websocket.accept")
        self.assertEqual(accepted_main[1]["type"], "websocket.send")

        denied_phone = asyncio.run(
            self._websocket_messages("/ws/phone", token=operator_token)
        )
        self.assertEqual(denied_phone[0]["type"], "websocket.accept")
        self.assertEqual(denied_phone[1]["type"], "websocket.send")
        self.assertEqual(
            json.loads(denied_phone[1]["text"])["type"], "rejected"
        )
        self.assertEqual(denied_phone[2]["type"], "websocket.close")
        accepted_phone = asyncio.run(
            self._websocket_messages("/ws/phone", token=pairing.TOKEN)
        )
        self.assertEqual(accepted_phone[0]["type"], "websocket.accept")

    def test_direct_asgi_entrypoint_cannot_expand_loopback_policy(self) -> None:
        server._configure_access(lan_mode=False, secure=False, port=8000)
        denied = asyncio.run(self._http_messages(
            "/api/project",
            client_host="203.0.113.20",
            host="localhost:8000",
        ))
        response = next(item for item in denied if item["type"] == "http.response.start")
        self.assertEqual(response["status"], 403)

        denied_ws = asyncio.run(self._websocket_messages(
            "/ws",
            client_host="203.0.113.20",
            host="localhost:8000",
            origin="http://localhost:8000",
        ))
        self.assertEqual(denied_ws[0]["type"], "websocket.close")

        allowed = asyncio.run(self._http_messages(
            "/api/project",
            client_host="127.0.0.1",
            host="localhost:8000",
        ))
        response = next(
            item for item in allowed if item["type"] == "http.response.start"
        )
        self.assertEqual(response["status"], 200)

    def test_internal_status_feed_is_loopback_only_and_reduced(self) -> None:
        self._configure_lan()
        denied = asyncio.run(self._websocket_messages(
            "/ws/internal/status",
            client_host="192.168.50.20",
            host="localhost:8443",
            origin=None,
        ))
        self.assertEqual(denied[0]["type"], "websocket.close")

        accepted = asyncio.run(self._websocket_messages(
            "/ws/internal/status",
            client_host="127.0.0.1",
            host="localhost:8443",
            origin=None,
        ))
        self.assertEqual(accepted[0]["type"], "websocket.accept")
        self.assertEqual(accepted[1]["type"], "websocket.send")

        reduced = server._internal_payload(
            {
                "type": "tick",
                "effort": {"effort": 82},
                "attention": {"zone": "left"},
                "variants": [{"id": "left", "label": "A", "secret": "no"}],
                "project": {"private": True},
                "phone": {"device": "private"},
            }
        )
        assert reduced is not None
        self.assertEqual(reduced["variants"], [{"id": "left", "label": "A"}])
        self.assertNotIn("project", reduced)
        self.assertNotIn("phone", reduced)

    def test_companion_streams_bind_loopback(self) -> None:
        root = Path(__file__).resolve().parent.parent
        for relative in (
            "desk/desk_server.py",
            "gesture/gesture_server.py",
            "gesture/mock_votes.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertIn('"127.0.0.1"', source)
            self.assertNotIn('serve(ws_handler, "0.0.0.0"', source)
            serve_calls = [
                node
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "serve"
            ]
            self.assertEqual(len(serve_calls), 1)
            origins = next(
                (item.value for item in serve_calls[0].keywords if item.arg == "origins"),
                None,
            )
            self.assertIsInstance(origins, ast.List)
            self.assertEqual(len(origins.elts), 1)
            self.assertIsNone(origins.elts[0].value)

    def test_companion_origin_policy_rejects_hostile_webpages(self) -> None:
        def handler(socket) -> None:
            socket.send("local-only")

        quiet_logger = logging.getLogger("bench.tests.websocket-origin")
        quiet_logger.addHandler(logging.NullHandler())
        quiet_logger.setLevel(logging.CRITICAL)
        with serve(
            handler,
            "127.0.0.1",
            0,
            origins=[None],
            logger=quiet_logger,
        ) as socket_server:
            thread = threading.Thread(
                target=socket_server.serve_forever, daemon=True
            )
            thread.start()
            port = socket_server.socket.getsockname()[1]
            try:
                with connect(f"ws://127.0.0.1:{port}") as socket:
                    self.assertEqual(socket.recv(), "local-only")
                with self.assertRaises(Exception):
                    with connect(
                        f"ws://127.0.0.1:{port}",
                        origin="https://evil.example",
                    ):
                        pass
            finally:
                socket_server.shutdown()
                thread.join(timeout=2)

    def test_post_login_redirect_cannot_leave_the_origin(self) -> None:
        self.assertEqual(server._safe_next("/project?tab=history"), "/project?tab=history")
        self.assertEqual(server._safe_next("https://evil.example"), "/")
        self.assertEqual(server._safe_next("//evil.example/path"), "/")
        self.assertEqual(server._safe_next("/\\evil.example"), "/")

    def test_cli_binds_loopback_unless_lan_is_explicit(self) -> None:
        with (
            patch("sys.argv", ["backend.server"]),
            patch("uvicorn.run") as run,
            patch("builtins.print"),
        ):
            server.main()
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")

    def test_cli_lan_mode_requires_tls_and_binds_all_interfaces(self) -> None:
        with (
            patch("sys.argv", ["backend.server", "--lan"]),
            redirect_stderr(StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            server.main()
        self.assertEqual(raised.exception.code, 2)

        with (
            patch("sys.argv", ["backend.server", "--lan", "--https"]),
            patch("backend.pairing.lan_ip", return_value="192.168.50.12"),
            patch("backend.tls.ensure_cert", return_value=("cert.pem", "key.pem")),
            patch("uvicorn.run") as run,
            patch("builtins.print"),
        ):
            server.main()
        self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")
        self.assertEqual(run.call_args.kwargs["ssl_certfile"], "cert.pem")
        self.assertGreaterEqual(len(server.ACCESS.operator_token or ""), 43)


if __name__ == "__main__":
    unittest.main()
