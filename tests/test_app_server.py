from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_crew.app_server import (
    AppServerAmbiguousRequestError,
    AppServerConnection,
    AppServerError,
    AppServerProtocolError,
    AppServerRequestUnavailableError,
    check_app_server,
    resolve_app_server_endpoint,
)


_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@dataclass(frozen=True)
class Frame:
    fin: bool
    rsv: int
    opcode: int
    masked: bool
    length_code: int
    payload: bytes


class FakeUnixWebSocketServer:
    def __init__(
        self,
        script,
        *,
        response_factory=None,
        handshake_chunk_size: int | None = None,
    ) -> None:
        self.directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.socket_path = Path(self.directory.name) / "app-server.sock"
        self.endpoint = f"unix://{self.socket_path}"
        self.script = script
        self.response_factory = response_factory or _valid_handshake_response
        self.handshake_chunk_size = handshake_chunk_size
        self.ready = threading.Event()
        self.errors: list[BaseException] = []
        self.handshake_headers: dict[str, str] = {}
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(timeout=1):
            raise AssertionError("fake Unix WebSocket server did not start")
        if self.errors:
            raise self.errors[0]
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.thread.join(timeout=2)
        try:
            if self.thread.is_alive() and exc_type is None:
                raise AssertionError("fake Unix WebSocket server did not stop")
            if self.errors and exc_type is None:
                raise self.errors[0]
        finally:
            self.directory.cleanup()

    def _serve(self) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(self.socket_path))
                listener.listen(1)
                self.ready.set()
                connection, _ = listener.accept()
                with connection:
                    request = _read_http_headers(connection)
                    self.handshake_headers = _parse_http_request_headers(request)
                    response = self.response_factory(self.handshake_headers)
                    _send_bytes(
                        connection,
                        response,
                        chunk_size=self.handshake_chunk_size,
                    )
                    if self.script is not None:
                        self.script(connection)
        except BaseException as error:
            self.errors.append(error)
            self.ready.set()


def _valid_handshake_response(headers: dict[str, str]) -> bytes:
    key = headers["sec-websocket-key"]
    accept = base64.b64encode(
        hashlib.sha1(key.encode("ascii") + _GUID).digest()
    ).decode("ascii")
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: keep-alive, Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    ).encode("ascii")


def _read_http_headers(connection: socket.socket) -> bytes:
    request = bytearray()
    while b"\r\n\r\n" not in request:
        chunk = connection.recv(1024)
        if not chunk:
            break
        request.extend(chunk)
    return bytes(request)


def _parse_http_request_headers(request: bytes) -> dict[str, str]:
    lines = request.decode("ascii").split("\r\n")
    if lines[0] != "GET / HTTP/1.1":
        raise AssertionError(f"unexpected request line: {lines[0]!r}")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, value = line.split(":", 1)
        headers[name.lower()] = value.strip()
    return headers


def _read_exact(connection: socket.socket, length: int) -> bytes:
    output = bytearray()
    while len(output) < length:
        chunk = connection.recv(length - len(output))
        if not chunk:
            raise EOFError("client disconnected")
        output.extend(chunk)
    return bytes(output)


def _read_frame(connection: socket.socket) -> Frame:
    first, second = _read_exact(connection, 2)
    length_code = second & 0x7F
    if length_code < 126:
        length = length_code
    elif length_code == 126:
        length = struct.unpack("!H", _read_exact(connection, 2))[0]
    else:
        length = struct.unpack("!Q", _read_exact(connection, 8))[0]
    masked = bool(second & 0x80)
    mask = _read_exact(connection, 4) if masked else b""
    payload = _read_exact(connection, length)
    if masked:
        payload = bytes(
            value ^ mask[index % 4] for index, value in enumerate(payload)
        )
    return Frame(
        fin=bool(first & 0x80),
        rsv=(first & 0x70) >> 4,
        opcode=first & 0x0F,
        masked=masked,
        length_code=length_code,
        payload=payload,
    )


def _send_frame(
    connection: socket.socket,
    opcode: int,
    payload: bytes,
    *,
    fin: bool = True,
    rsv: int = 0,
    masked: bool = False,
    force_length: int | None = None,
    declared_length: int | None = None,
    chunk_size: int | None = None,
) -> None:
    first = (0x80 if fin else 0) | ((rsv & 0x7) << 4) | opcode
    length = len(payload) if declared_length is None else declared_length
    header = bytearray([first])
    length_mode = force_length
    if length_mode is None:
        length_mode = 7 if length < 126 else 16 if length < 65536 else 64
    if length_mode == 7:
        header.append((0x80 if masked else 0) | length)
    elif length_mode == 16:
        header.append((0x80 if masked else 0) | 126)
        header.extend(struct.pack("!H", length))
    elif length_mode == 64:
        header.append((0x80 if masked else 0) | 127)
        header.extend(struct.pack("!Q", length))
    else:
        raise AssertionError(f"unknown frame length mode: {length_mode}")
    if masked:
        mask = b"mask"
        header.extend(mask)
        payload = bytes(
            value ^ mask[index % 4] for index, value in enumerate(payload)
        )
    _send_bytes(connection, bytes(header) + payload, chunk_size=chunk_size)


def _send_bytes(
    connection: socket.socket, payload: bytes, *, chunk_size: int | None = None
) -> None:
    if chunk_size is None:
        connection.sendall(payload)
        return
    for index in range(0, len(payload), chunk_size):
        connection.sendall(payload[index : index + chunk_size])


def _read_json_frame(connection: socket.socket) -> tuple[Frame, dict]:
    frame = _read_frame(connection)
    return frame, json.loads(frame.payload.decode("utf-8"))


def _send_json(
    connection: socket.socket,
    message: dict,
    *,
    force_length: int | None = None,
    chunk_size: int | None = None,
) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode()
    _send_frame(
        connection,
        0x1,
        payload,
        force_length=force_length,
        chunk_size=chunk_size,
    )


def _initialize(connection: socket.socket, observed: list[tuple[Frame, dict]]) -> None:
    frame, request = _read_json_frame(connection)
    observed.append((frame, request))
    if request.get("method") != "initialize" or request.get("id") != 1:
        raise AssertionError(f"unexpected initialize request: {request!r}")
    _send_json(connection, {"id": 1, "result": {"userAgent": "fake/0.149.1"}})
    frame, notification = _read_json_frame(connection)
    observed.append((frame, notification))
    if notification != {"method": "initialized"}:
        raise AssertionError(f"unexpected initialized notification: {notification!r}")


def _finish_close(connection: socket.socket) -> Frame:
    close_frame = _read_frame(connection)
    if close_frame.opcode != 0x8:
        raise AssertionError(f"expected close frame, got opcode {close_frame.opcode}")
    _send_frame(connection, 0x8, close_frame.payload)
    return close_frame


class AppServerTests(unittest.TestCase):
    def test_resolves_default_codex_home_and_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"CODEX_HOME": str(root)}):
                default = resolve_app_server_endpoint()
            explicit_path = root / "custom.sock"
            explicit_endpoint = f"unix://{explicit_path}"
            explicit = resolve_app_server_endpoint(explicit_endpoint)

        self.assertEqual("unix://", default.endpoint)
        self.assertEqual(
            (
                root / "app-server-control" / "app-server-control.sock"
            ).resolve(strict=False),
            default.socket_path,
        )
        self.assertEqual(explicit_endpoint, explicit.endpoint)
        self.assertEqual(explicit_path.resolve(strict=False), explicit.socket_path)

    def test_check_validates_handshake_initializes_masks_and_creates_no_thread(self) -> None:
        observed: list[tuple[Frame, dict]] = []
        close_frames: list[Frame] = []
        close_pongs: list[Frame] = []

        def script(connection: socket.socket) -> None:
            _initialize(connection, observed)
            close_frame = _read_frame(connection)
            if close_frame.opcode != 0x8:
                raise AssertionError(
                    f"expected close frame, got opcode {close_frame.opcode}"
                )
            close_frames.append(close_frame)
            _send_json(
                connection,
                {
                    "method": "remoteControl/status/changed",
                    "params": {"status": "disabled"},
                    "emittedAtMs": 1,
                },
            )
            _send_frame(connection, 0x9, b"close-ping")
            close_pongs.append(_read_frame(connection))
            _send_frame(connection, 0x8, close_frame.payload)

        with FakeUnixWebSocketServer(
            script, handshake_chunk_size=3
        ) as server:
            result = check_app_server(server.endpoint, timeout_seconds=0.5)

        self.assertEqual(server.endpoint, result.endpoint)
        key = server.handshake_headers["sec-websocket-key"]
        self.assertEqual(16, len(base64.b64decode(key)))
        self.assertEqual("websocket", server.handshake_headers["upgrade"].lower())
        self.assertIn("upgrade", server.handshake_headers["connection"].lower())
        self.assertEqual(["initialize", "initialized"], [item[1]["method"] for item in observed])
        self.assertTrue(all(frame.masked for frame, _ in observed))
        self.assertTrue(all(frame.opcode == 0x1 and frame.fin for frame, _ in observed))
        self.assertTrue(close_frames[0].masked)
        self.assertEqual(0xA, close_pongs[0].opcode)
        self.assertTrue(close_pongs[0].masked)
        self.assertEqual(b"close-ping", close_pongs[0].payload)
        self.assertFalse(any(item[1]["method"].startswith("thread/") for item in observed))

    def test_close_phase_rejects_non_notification_data(self) -> None:
        cases = {
            "response": (
                lambda connection: _send_json(
                    connection, {"id": 999, "result": {}}
                ),
                "JSON-RPC response id=999 while closing",
            ),
            "server-request": (
                lambda connection: _send_json(
                    connection,
                    {
                        "method": "known/serverRequest",
                        "id": "server-close",
                        "params": {},
                    },
                ),
                "server request 'known/serverRequest'.*while closing",
            ),
            "notification-with-result": (
                lambda connection: _send_json(
                    connection, {"method": "notice", "result": {}}
                ),
                "malformed notification 'notice' while closing",
            ),
            "binary": (
                lambda connection: _send_frame(connection, 0x2, b"{}"),
                "opcode 0x2 while closing",
            ),
            "malformed-json": (
                lambda connection: _send_frame(connection, 0x1, b"{"),
                "not valid JSON",
            ),
            "fragmented": (
                lambda connection: _send_frame(
                    connection, 0x1, b"{}", fin=False
                ),
                "fragmented",
            ),
        }

        for label, (send_invalid, pattern) in cases.items():
            with self.subTest(label=label):
                def script(connection: socket.socket) -> None:
                    initialized: list[tuple[Frame, dict]] = []
                    _initialize(connection, initialized)
                    close_frame = _read_frame(connection)
                    if close_frame.opcode != 0x8:
                        raise AssertionError(
                            f"expected close frame, got opcode {close_frame.opcode}"
                        )
                    send_invalid(connection)

                with FakeUnixWebSocketServer(script) as server:
                    connection = AppServerConnection(
                        server.endpoint,
                        server_request_handlers={
                            "known/serverRequest": lambda params: {"ok": True}
                        },
                    )
                    with self.assertRaisesRegex(AppServerProtocolError, pattern):
                        connection.close()
                self.assertTrue(connection.closed)

    def test_close_accepts_clean_boundary_eof_and_rejects_partial_frame(self) -> None:
        def clean_eof_script(connection: socket.socket) -> None:
            initialized: list[tuple[Frame, dict]] = []
            _initialize(connection, initialized)
            close_frame = _read_frame(connection)
            if close_frame.opcode != 0x8:
                raise AssertionError(
                    f"expected close frame, got opcode {close_frame.opcode}"
                )

        with FakeUnixWebSocketServer(clean_eof_script) as server:
            connection = AppServerConnection(server.endpoint)
            connection.close()
        self.assertTrue(connection.closed)

        def partial_frame_script(connection: socket.socket) -> None:
            initialized: list[tuple[Frame, dict]] = []
            _initialize(connection, initialized)
            close_frame = _read_frame(connection)
            if close_frame.opcode != 0x8:
                raise AssertionError(
                    f"expected close frame, got opcode {close_frame.opcode}"
                )
            connection.sendall(b"\x81")

        with FakeUnixWebSocketServer(partial_frame_script) as server:
            connection = AppServerConnection(server.endpoint)
            with self.assertRaisesRegex(AppServerProtocolError, "mid-frame"):
                connection.close()
        self.assertTrue(connection.closed)

    def test_rejects_invalid_upgrade_status_and_headers(self) -> None:
        def response_factory(case: str):
            def response(headers: dict[str, str]) -> bytes:
                valid = _valid_handshake_response(headers).decode("ascii")
                if case == "status":
                    valid = valid.replace(
                        "HTTP/1.1 101 Switching Protocols", "HTTP/1.1 200 OK"
                    )
                elif case == "upgrade":
                    valid = valid.replace("Upgrade: websocket", "Upgrade: h2c")
                elif case == "connection":
                    valid = valid.replace(
                        "Connection: keep-alive, Upgrade", "Connection: close"
                    )
                else:
                    valid = valid.replace(
                        "Sec-WebSocket-Accept:", "Sec-WebSocket-Accept: wrong"
                    )
                return valid.encode("ascii")

            return response

        patterns = {
            "status": "expected HTTP/1.1 101",
            "upgrade": "missing Upgrade",
            "connection": "missing Connection",
            "accept": "invalid Sec-WebSocket-Accept",
        }
        for case, pattern in patterns.items():
            with self.subTest(case=case):
                with FakeUnixWebSocketServer(
                    None, response_factory=response_factory(case)
                ) as server:
                    with self.assertRaisesRegex(AppServerProtocolError, pattern):
                        AppServerConnection(server.endpoint)

        def oversized_response(headers: dict[str, str]) -> bytes:
            return b"HTTP/1.1 101 Switching Protocols\r\nX-Fill: " + b"x" * 256

        with FakeUnixWebSocketServer(
            None, response_factory=oversized_response
        ) as server:
            with self.assertRaisesRegex(AppServerProtocolError, "response limit"):
                AppServerConnection(server.endpoint, max_handshake_bytes=64)

    def test_interleaves_notification_server_request_ping_and_response(self) -> None:
        observed: dict[str, object] = {}
        notifications: list[tuple[str, object]] = []

        def script(connection: socket.socket) -> None:
            initialized: list[tuple[Frame, dict]] = []
            _initialize(connection, initialized)
            request_frame, request = _read_json_frame(connection)
            observed["request_frame"] = request_frame
            observed["request"] = request
            _send_json(
                connection,
                {"method": "thread/status/changed", "params": {"status": "idle"}},
            )
            _send_json(
                connection,
                {"method": "known/serverRequest", "id": "server-1", "params": {"x": 2}},
            )
            _, server_response = _read_json_frame(connection)
            observed["server_response"] = server_response
            _send_frame(connection, 0x9, b"ping-payload")
            observed["pong"] = _read_frame(connection)
            _send_json(connection, {"id": request["id"], "result": {"ok": True}})
            _finish_close(connection)

        with FakeUnixWebSocketServer(script) as server:
            connection = AppServerConnection(
                server.endpoint,
                notification_handler=lambda method, params: notifications.append(
                    (method, params)
                ),
                server_request_handlers={
                    "known/serverRequest": lambda params: {"sum": params["x"] + 3}
                },
            )
            result = connection.request("probe/run", {"value": 1})
            queued = connection.receive_notification(timeout_seconds=0.5)
            connection.close()

        self.assertEqual({"ok": True}, result)
        self.assertEqual(
            [("thread/status/changed", {"status": "idle"})], notifications
        )
        self.assertEqual("thread/status/changed", queued.method)
        self.assertEqual({"status": "idle"}, queued.params)
        self.assertEqual(
            {"id": "server-1", "result": {"sum": 5}}, observed["server_response"]
        )
        pong = observed["pong"]
        self.assertIsInstance(pong, Frame)
        self.assertEqual(0xA, pong.opcode)
        self.assertTrue(pong.masked)
        self.assertEqual(b"ping-payload", pong.payload)
        self.assertTrue(observed["request_frame"].masked)

    def test_uses_16_and_64_bit_lengths_with_strictly_monotonic_ids(self) -> None:
        observed: list[tuple[int, int]] = []
        notifications: list[str] = []

        def script(connection: socket.socket) -> None:
            initialized: list[tuple[Frame, dict]] = []
            _initialize(connection, initialized)
            first_frame, first = _read_json_frame(connection)
            observed.append((first["id"], first_frame.length_code))
            _send_json(
                connection,
                {"method": "notice", "params": {"text": "n" * 180}},
                force_length=16,
                chunk_size=5,
            )
            _send_json(connection, {"id": first["id"], "result": "short"})
            second_frame, second = _read_json_frame(connection)
            observed.append((second["id"], second_frame.length_code))
            _send_json(
                connection,
                {"id": second["id"], "result": "r" * 66000},
                force_length=64,
                chunk_size=17,
            )
            _finish_close(connection)

        with FakeUnixWebSocketServer(script) as server:
            connection = AppServerConnection(
                server.endpoint,
                notification_handler=lambda method, params: notifications.append(method),
            )
            first = connection.request("probe/short", {"text": "x" * 200})
            second = connection.request("probe/long", {"text": "y" * 66000})
            connection.close()

        self.assertEqual("short", first)
        self.assertEqual(66000, len(second))
        self.assertEqual([(2, 126), (3, 127)], observed)
        self.assertEqual(["notice"], notifications)

    def test_preserves_minus_32001_rpc_error_without_retry(self) -> None:
        requests: list[dict] = []

        def script(connection: socket.socket) -> None:
            initialized: list[tuple[Frame, dict]] = []
            _initialize(connection, initialized)
            _, request = _read_json_frame(connection)
            requests.append(request)
            _send_json(
                connection,
                {
                    "id": request["id"],
                    "error": {
                        "code": -32001,
                        "message": "temporarily unavailable",
                        "data": {"retryAfterMs": 50},
                    },
                },
            )
            _finish_close(connection)

        with FakeUnixWebSocketServer(script) as server:
            connection = AppServerConnection(server.endpoint)
            with self.assertRaises(AppServerRequestUnavailableError) as caught:
                connection.request("thread/start", {})
            connection.close()

        error = caught.exception
        self.assertEqual(2, error.request_id)
        self.assertEqual("thread/start", error.method)
        self.assertEqual(-32001, error.code)
        self.assertEqual("temporarily unavailable", error.message)
        self.assertEqual({"retryAfterMs": 50}, error.data)
        self.assertEqual(1, len(requests))

    def test_timeout_and_disconnect_report_ambiguous_request_without_retry(self) -> None:
        def timeout_script(connection: socket.socket) -> None:
            initialized: list[tuple[Frame, dict]] = []
            _initialize(connection, initialized)
            _read_json_frame(connection)
            time.sleep(0.15)

        def disconnect_script(connection: socket.socket) -> None:
            initialized: list[tuple[Frame, dict]] = []
            _initialize(connection, initialized)
            _read_json_frame(connection)

        for label, script in (
            ("timeout", timeout_script),
            ("disconnect", disconnect_script),
        ):
            with self.subTest(label=label):
                with FakeUnixWebSocketServer(script) as server:
                    connection = AppServerConnection(
                        server.endpoint, request_timeout_seconds=0.05
                    )
                    with self.assertRaises(AppServerAmbiguousRequestError) as caught:
                        connection.request("turn/start", {"threadId": "thread-1"})
                self.assertEqual(2, caught.exception.request_id)
                self.assertEqual("turn/start", caught.exception.method)
                self.assertIn("not retried", str(caught.exception))

    def test_unknown_and_duplicate_responses_fail_closed(self) -> None:
        def unknown_script(connection: socket.socket) -> None:
            initialized: list[tuple[Frame, dict]] = []
            _initialize(connection, initialized)
            _read_json_frame(connection)
            _send_json(connection, {"id": 999, "result": {}})

        def duplicate_script(connection: socket.socket) -> None:
            initialized: list[tuple[Frame, dict]] = []
            _initialize(connection, initialized)
            _, first = _read_json_frame(connection)
            _send_json(connection, {"id": first["id"], "result": "first"})
            _read_json_frame(connection)
            _send_json(connection, {"id": first["id"], "result": "duplicate"})

        for label, script, pattern in (
            ("unknown", unknown_script, "unknown app-server response id 999"),
            ("duplicate", duplicate_script, "duplicate app-server response id 2"),
        ):
            with self.subTest(label=label):
                with FakeUnixWebSocketServer(script) as server:
                    connection = AppServerConnection(server.endpoint)
                    if label == "duplicate":
                        self.assertEqual("first", connection.request("first", {}))
                    with self.assertRaisesRegex(AppServerProtocolError, pattern):
                        connection.request("probe", {})
                self.assertTrue(connection.closed)

        def event_wait_script(connection: socket.socket) -> None:
            initialized: list[tuple[Frame, dict]] = []
            _initialize(connection, initialized)
            _, request = _read_json_frame(connection)
            _send_json(connection, {"id": request["id"], "result": "done"})
            _send_json(connection, {"id": 999, "result": {}})

        with FakeUnixWebSocketServer(event_wait_script) as server:
            connection = AppServerConnection(server.endpoint)
            self.assertEqual("done", connection.request("probe", {}))
            with self.assertRaisesRegex(
                AppServerProtocolError,
                "unknown app-server response id 999.*waiting for a notification",
            ):
                connection.receive_notification(timeout_seconds=0.5)
        self.assertTrue(connection.closed)

    def test_unknown_server_request_fails_closed(self) -> None:
        def script(connection: socket.socket) -> None:
            initialized: list[tuple[Frame, dict]] = []
            _initialize(connection, initialized)
            _read_json_frame(connection)
            _send_json(
                connection,
                {"method": "unknown/serverRequest", "id": "server-9", "params": {}},
            )

        with FakeUnixWebSocketServer(script) as server:
            connection = AppServerConnection(server.endpoint)
            with self.assertRaisesRegex(
                AppServerProtocolError, "unknown app-server server request"
            ):
                connection.request("probe", {})
        self.assertTrue(connection.closed)

    def test_rejects_malformed_data_frames(self) -> None:
        cases = {
            "rsv": (dict(opcode=0x1, payload=b"{}", rsv=1), "RSV bits"),
            "masked-server": (
                dict(opcode=0x1, payload=b"{}", masked=True),
                "masked server frame",
            ),
            "binary": (dict(opcode=0x2, payload=b"{}"), "opcode 0x2"),
            "fragmented": (
                dict(opcode=0x1, payload=b"{}", fin=False),
                "fragmented",
            ),
            "utf8": (dict(opcode=0x1, payload=b"\xff"), "not valid UTF-8"),
            "json": (dict(opcode=0x1, payload=b"{"), "not valid JSON"),
            "json-array": (
                dict(opcode=0x1, payload=b"[]"),
                "must be a JSON object",
            ),
        }
        for label, (frame_options, pattern) in cases.items():
            with self.subTest(label=label):
                def script(connection: socket.socket, options=frame_options) -> None:
                    initialized: list[tuple[Frame, dict]] = []
                    _initialize(connection, initialized)
                    _read_json_frame(connection)
                    _send_frame(connection, **options)

                with FakeUnixWebSocketServer(script) as server:
                    connection = AppServerConnection(server.endpoint)
                    with self.assertRaisesRegex(AppServerProtocolError, pattern):
                        connection.request("probe", {})
                self.assertTrue(connection.closed)

    def test_rejects_oversized_and_noncanonical_frames(self) -> None:
        def frame_script(options):
            def script(connection: socket.socket) -> None:
                initialized: list[tuple[Frame, dict]] = []
                _initialize(connection, initialized)
                _read_json_frame(connection)
                _send_frame(connection, **options)

            return script

        cases = (
            (
                "oversized",
                frame_script(
                    dict(
                        opcode=0x1,
                        payload=b"",
                        force_length=16,
                        declared_length=300,
                    )
                ),
                "exceeds 256 bytes",
                256,
            ),
            (
                "noncanonical16",
                frame_script(dict(opcode=0x1, payload=b"{}", force_length=16)),
                "non-canonical 16-bit",
                1024,
            ),
            (
                "noncanonical64",
                frame_script(dict(opcode=0x1, payload=b"{}", force_length=64)),
                "non-canonical 64-bit",
                1024,
            ),
        )
        for label, script, pattern, limit in cases:
            with self.subTest(label=label):
                with FakeUnixWebSocketServer(script) as server:
                    connection = AppServerConnection(
                        server.endpoint, max_frame_bytes=limit
                    )
                    with self.assertRaisesRegex(AppServerProtocolError, pattern):
                        connection.request("probe", {})
                self.assertTrue(connection.closed)

    def test_peer_close_before_response_is_ambiguous_and_close_is_idempotent(self) -> None:
        observed_close_replies: list[Frame] = []

        def peer_close_script(connection: socket.socket) -> None:
            initialized: list[tuple[Frame, dict]] = []
            _initialize(connection, initialized)
            _read_json_frame(connection)
            _send_frame(connection, 0x8, struct.pack("!H", 1000) + b"bye")
            observed_close_replies.append(_read_frame(connection))

        with FakeUnixWebSocketServer(peer_close_script) as server:
            connection = AppServerConnection(server.endpoint)
            with self.assertRaises(AppServerAmbiguousRequestError) as caught:
                connection.request("probe", {})
        self.assertIn("closed the WebSocket with code 1000", caught.exception.reason)
        self.assertEqual(0x8, observed_close_replies[0].opcode)
        self.assertTrue(observed_close_replies[0].masked)

        def normal_close_script(connection: socket.socket) -> None:
            initialized: list[tuple[Frame, dict]] = []
            _initialize(connection, initialized)
            _finish_close(connection)

        with FakeUnixWebSocketServer(normal_close_script) as server:
            connection = AppServerConnection(server.endpoint)
            with self.assertRaisesRegex(AppServerProtocolError, "automatic"):
                connection.request("initialize", {})
            with self.assertRaisesRegex(AppServerProtocolError, "automatic"):
                connection.notify("initialized")
            connection.close()
            connection.close()
        self.assertTrue(connection.closed)

    def test_endpoint_failures_remain_clear_and_fail_closed(self) -> None:
        with self.assertRaisesRegex(AppServerError, "unsupported.*only unix"):
            check_app_server("ws://127.0.0.1:4500")

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            missing = root / "missing.sock"
            with self.assertRaisesRegex(AppServerError, "does not exist"):
                check_app_server(f"unix://{missing}")

            regular = root / "regular.file"
            regular.write_text("not a socket\n", encoding="utf-8")
            with self.assertRaisesRegex(AppServerError, "not a Unix socket"):
                check_app_server(f"unix://{regular}")


if __name__ == "__main__":
    unittest.main()
