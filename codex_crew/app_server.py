"""Bounded Unix WebSocket JSON-RPC transport for Codex app-server."""

from __future__ import annotations

import base64
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time
from typing import Any


DEFAULT_APP_SERVER_ENDPOINT = "unix://"
DEFAULT_CONTROL_SOCKET = Path("app-server-control/app-server-control.sock")
DEFAULT_HANDSHAKE_TIMEOUT_SECONDS = 2.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_CLOSE_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_HANDSHAKE_BYTES = 16 * 1024
DEFAULT_MAX_FRAME_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_MESSAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_CLOSE_NOTIFICATIONS = 32
DEFAULT_MAX_QUEUED_NOTIFICATIONS = 1024
_WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class AppServerError(RuntimeError):
    """Base error for endpoint, WebSocket, or JSON-RPC failures."""


class AppServerProtocolError(AppServerError):
    """The peer violated the WebSocket or JSON-RPC transport contract."""


class AppServerConnectionClosed(AppServerProtocolError):
    """The WebSocket closed before the caller expected it to close."""

    def __init__(self, message: str, *, at_frame_boundary: bool = False) -> None:
        self.at_frame_boundary = at_frame_boundary
        super().__init__(message)


class AppServerRpcError(AppServerError):
    """A matched JSON-RPC response carried an error object."""

    def __init__(
        self,
        *,
        request_id: int,
        method: str,
        code: int,
        message: str,
        data: Any = None,
    ) -> None:
        self.request_id = request_id
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        super().__init__(
            f"app-server RPC {method!r} id={request_id} failed "
            f"with code {code}: {message}"
        )


class AppServerRequestUnavailableError(AppServerRpcError):
    """The app-server returned retryable code -32001; no retry was attempted."""


class AppServerAmbiguousRequestError(AppServerError):
    """A sent request lost its response; callers must reconcile before retrying."""

    def __init__(self, *, request_id: int, method: str, reason: str) -> None:
        self.request_id = request_id
        self.method = method
        self.reason = reason
        super().__init__(
            f"app-server request {method!r} id={request_id} has ambiguous outcome: "
            f"{reason}; request was not retried"
        )


@dataclass(frozen=True)
class AppServerEndpoint:
    endpoint: str
    socket_path: Path


@dataclass(frozen=True)
class AppServerNotification:
    method: str
    params: Any


NotificationHandler = Callable[[str, Any], None]
ServerRequestHandler = Callable[[Any], Any]


def resolve_app_server_endpoint(
    endpoint: str = DEFAULT_APP_SERVER_ENDPOINT,
    *,
    codex_home: str | Path | None = None,
) -> AppServerEndpoint:
    """Resolve the supported unix:// forms without accepting other transports."""

    if endpoint == DEFAULT_APP_SERVER_ENDPOINT:
        configured_home = (
            codex_home if codex_home is not None else os.environ.get("CODEX_HOME")
        )
        home = (
            Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".codex"
        )
        socket_path = (home / DEFAULT_CONTROL_SOCKET).resolve(strict=False)
        return AppServerEndpoint(endpoint=endpoint, socket_path=socket_path)

    if not endpoint.startswith(DEFAULT_APP_SERVER_ENDPOINT):
        raise AppServerError(
            f"unsupported app-server endpoint {endpoint!r}; "
            "only unix:// and unix://PATH are supported"
        )

    path_text = endpoint[len(DEFAULT_APP_SERVER_ENDPOINT) :]
    if not path_text or "\x00" in path_text:
        raise AppServerError(
            f"invalid app-server Unix endpoint {endpoint!r}; "
            "an explicit socket path is required after unix://"
        )
    socket_path = Path(path_text).expanduser()
    if not socket_path.is_absolute():
        socket_path = Path.cwd() / socket_path
    return AppServerEndpoint(
        endpoint=endpoint,
        socket_path=socket_path.resolve(strict=False),
    )


class AppServerConnection:
    """One initialized JSON-RPC connection over WebSocket on a Unix socket."""

    def __init__(
        self,
        endpoint: str = DEFAULT_APP_SERVER_ENDPOINT,
        *,
        codex_home: str | Path | None = None,
        client_info: Mapping[str, str] | None = None,
        notification_handler: NotificationHandler | None = None,
        server_request_handlers: Mapping[str, ServerRequestHandler] | None = None,
        handshake_timeout_seconds: float = DEFAULT_HANDSHAKE_TIMEOUT_SECONDS,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
        max_handshake_bytes: int = DEFAULT_MAX_HANDSHAKE_BYTES,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        max_close_notifications: int = DEFAULT_MAX_CLOSE_NOTIFICATIONS,
        max_queued_notifications: int = DEFAULT_MAX_QUEUED_NOTIFICATIONS,
    ) -> None:
        self.resolved_endpoint = resolve_app_server_endpoint(
            endpoint, codex_home=codex_home
        )
        self.notification_handler = notification_handler
        self.server_request_handlers = dict(server_request_handlers or {})
        self.handshake_timeout_seconds = _positive_timeout(
            handshake_timeout_seconds, "handshake"
        )
        self.request_timeout_seconds = _positive_timeout(
            request_timeout_seconds, "request"
        )
        self.close_timeout_seconds = _positive_timeout(
            close_timeout_seconds, "close"
        )
        self.max_handshake_bytes = _positive_limit(
            max_handshake_bytes, "handshake"
        )
        self.max_frame_bytes = _positive_limit(max_frame_bytes, "frame")
        self.max_message_bytes = _positive_limit(max_message_bytes, "message")
        self.max_close_notifications = _positive_limit(
            max_close_notifications, "close notification"
        )
        self.max_queued_notifications = _positive_limit(
            max_queued_notifications, "queued notification"
        )
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._buffer = bytearray()
        self._next_request_id = 1
        self._outstanding: dict[int, str] = {}
        self._completed_ids: set[int] = set()
        self._notifications: deque[AppServerNotification] = deque()
        self._initialized = False
        self._closed = False
        self._close_sent = False

        try:
            self._connect_and_upgrade()
            initialize_info = dict(
                client_info
                or {
                    "name": "codex_crew",
                    "title": "codex-crew",
                    "version": "0.1.0",
                }
            )
            self._request(
                "initialize",
                {"clientInfo": initialize_info},
                timeout_seconds=self.request_timeout_seconds,
            )
            self._send_json({"method": "initialized"}, self._deadline())
            self._initialized = True
        except Exception:
            self._abort()
            raise

    @property
    def endpoint(self) -> str:
        return self.resolved_endpoint.endpoint

    @property
    def socket_path(self) -> Path:
        return self.resolved_endpoint.socket_path

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> AppServerConnection:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            self.close()
            return
        try:
            self.close()
        except AppServerError:
            pass

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        if not self._initialized:
            raise AppServerProtocolError("app-server connection is not initialized")
        if method == "initialize":
            raise AppServerProtocolError(
                "initialize is automatic and may only occur once per connection"
            )
        return self._request(method, params, timeout_seconds=timeout_seconds)

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        if not self._initialized:
            raise AppServerProtocolError("app-server connection is not initialized")
        if method == "initialized":
            raise AppServerProtocolError(
                "initialized is automatic and may only occur once per connection"
            )
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = dict(params)
        self._send_json(message, self._deadline())

    def receive_notification(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> AppServerNotification:
        """Return the next bounded notification without sending a request."""

        if not self._initialized:
            raise AppServerProtocolError("app-server connection is not initialized")
        if self._closed:
            raise AppServerConnectionClosed("app-server connection is closed")
        if self._notifications:
            return self._notifications.popleft()
        timeout = (
            self.request_timeout_seconds
            if timeout_seconds is None
            else _positive_timeout(timeout_seconds, "notification")
        )
        deadline = time.monotonic() + timeout
        try:
            while True:
                incoming = self._receive_json(deadline)
                if "method" in incoming:
                    is_notification = "id" not in incoming
                    self._handle_incoming_call(incoming, deadline)
                    if is_notification:
                        return self._notifications.popleft()
                    continue
                response_id = incoming.get("id")
                if isinstance(response_id, bool) or not isinstance(
                    response_id, (int, str)
                ):
                    raise AppServerProtocolError(
                        f"invalid app-server response id {response_id!r}"
                    )
                if response_id in self._completed_ids:
                    raise AppServerProtocolError(
                        f"duplicate app-server response id {response_id!r}"
                    )
                raise AppServerProtocolError(
                    f"unknown app-server response id {response_id!r} "
                    "while waiting for a notification"
                )
        except socket.timeout as error:
            raise AppServerError(
                "timed out waiting for an app-server notification"
            ) from error
        except AppServerError:
            self._abort()
            raise
        except OSError as error:
            self._abort()
            raise AppServerError(
                f"app-server socket failed while waiting for a notification: {error}"
            ) from error

    def close(self) -> None:
        if self._closed:
            return
        deadline = time.monotonic() + self.close_timeout_seconds
        try:
            if not self._close_sent:
                self._send_frame(0x8, struct.pack("!H", 1000), deadline)
                self._close_sent = True
            notification_count = 0
            while True:
                opcode, payload = self._receive_frame(deadline)
                if opcode == 0x8:
                    self._validate_close_payload(payload)
                    self._abort()
                    return
                if opcode == 0x9:
                    self._send_frame(0xA, payload, deadline)
                    continue
                if opcode == 0xA:
                    continue
                if opcode == 0x1:
                    notification_count += 1
                    if notification_count > self.max_close_notifications:
                        raise AppServerProtocolError(
                            "app-server exceeded the close-phase notification limit "
                            f"of {self.max_close_notifications}"
                        )
                    self._handle_close_notification(
                        self._decode_json_payload(payload), deadline
                    )
                    continue
                raise AppServerProtocolError(
                    f"app-server sent unsupported WebSocket opcode 0x{opcode:x} "
                    "while closing"
                )
        except socket.timeout as error:
            self._abort()
            raise AppServerProtocolError(
                "app-server WebSocket close handshake timed out"
            ) from error
        except AppServerConnectionClosed as error:
            self._abort()
            if error.at_frame_boundary:
                return
            raise AppServerProtocolError(
                "app-server disconnected mid-frame while closing the WebSocket"
            ) from error
        except AppServerError:
            self._abort()
            raise
        except OSError as error:
            self._abort()
            raise AppServerProtocolError(
                f"app-server WebSocket close handshake failed: {error}"
            ) from error

    def _connect_and_upgrade(self) -> None:
        _validate_socket_path(self.resolved_endpoint.socket_path)
        deadline = time.monotonic() + self.handshake_timeout_seconds
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            self._set_remaining_timeout(deadline)
            self._socket.connect(str(self.resolved_endpoint.socket_path))
            self._set_remaining_timeout(deadline)
            self._socket.sendall(request)
            headers = self._read_handshake_headers(deadline)
        except socket.timeout as error:
            raise AppServerError(
                f"app-server WebSocket handshake timed out: {self.socket_path}"
            ) from error
        except OSError as error:
            raise AppServerError(
                f"cannot connect to app-server Unix socket {self.socket_path}: {error}"
            ) from error
        self._validate_upgrade_response(headers, key)

    def _read_handshake_headers(self, deadline: float) -> bytes:
        delimiter = b"\r\n\r\n"
        while delimiter not in self._buffer:
            if len(self._buffer) >= self.max_handshake_bytes:
                raise AppServerProtocolError(
                    "app-server WebSocket handshake exceeded the response limit"
                )
            self._set_remaining_timeout(deadline)
            chunk = self._socket.recv(min(4096, self.max_handshake_bytes))
            if not chunk:
                raise AppServerProtocolError(
                    "app-server disconnected during the WebSocket handshake"
                )
            self._buffer.extend(chunk)
        boundary = self._buffer.index(delimiter) + len(delimiter)
        if boundary > self.max_handshake_bytes:
            raise AppServerProtocolError(
                "app-server WebSocket handshake exceeded the response limit"
            )
        headers = bytes(self._buffer[:boundary])
        del self._buffer[:boundary]
        return headers

    def _validate_upgrade_response(self, raw_headers: bytes, key: str) -> None:
        try:
            lines = raw_headers.decode("iso-8859-1").split("\r\n")
        except UnicodeDecodeError as error:
            raise AppServerProtocolError(
                "app-server WebSocket handshake headers are invalid"
            ) from error
        status_parts = lines[0].split(" ", 2) if lines else []
        if len(status_parts) < 2 or status_parts[:2] != ["HTTP/1.1", "101"]:
            received = lines[0] if lines else "no HTTP status"
            raise AppServerProtocolError(
                "app-server WebSocket handshake failed: expected HTTP/1.1 101, "
                f"received {received!r}"
            )
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if not line:
                continue
            if ":" not in line:
                raise AppServerProtocolError(
                    f"malformed app-server WebSocket handshake header: {line!r}"
                )
            name, value = line.split(":", 1)
            headers.setdefault(name.strip().lower(), []).append(value.strip())
        if not _header_has_token(headers, "upgrade", "websocket"):
            raise AppServerProtocolError(
                "app-server WebSocket handshake is missing Upgrade: websocket"
            )
        if not _header_has_token(headers, "connection", "upgrade"):
            raise AppServerProtocolError(
                "app-server WebSocket handshake is missing Connection: Upgrade"
            )
        accept_values = headers.get("sec-websocket-accept", [])
        expected = base64.b64encode(
            hashlib.sha1(key.encode("ascii") + _WEBSOCKET_GUID).digest()
        ).decode("ascii")
        if len(accept_values) != 1 or accept_values[0] != expected:
            raise AppServerProtocolError(
                "app-server WebSocket handshake has an invalid Sec-WebSocket-Accept"
            )

    def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        *,
        timeout_seconds: float | None,
    ) -> Any:
        if self._closed:
            raise AppServerConnectionClosed("app-server connection is closed")
        timeout = self.request_timeout_seconds if timeout_seconds is None else _positive_timeout(
            timeout_seconds, "request"
        )
        request_id = self._next_request_id
        self._next_request_id += 1
        if request_id in self._outstanding or request_id in self._completed_ids:
            raise AppServerProtocolError(
                f"app-server request id {request_id} is not unique"
            )
        self._outstanding[request_id] = method
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = dict(params)
        deadline = time.monotonic() + timeout
        try:
            self._send_json(message, deadline)
            while True:
                incoming = self._receive_json(deadline)
                if "method" in incoming:
                    self._handle_incoming_call(incoming, deadline)
                    continue
                response_id = incoming.get("id")
                if isinstance(response_id, bool) or not isinstance(
                    response_id, (int, str)
                ):
                    raise AppServerProtocolError(
                        f"invalid app-server response id {response_id!r}"
                    )
                if response_id in self._completed_ids:
                    raise AppServerProtocolError(
                        f"duplicate app-server response id {response_id!r}"
                    )
                if response_id not in self._outstanding:
                    raise AppServerProtocolError(
                        f"unknown app-server response id {response_id!r}"
                    )
                if response_id != request_id:
                    raise AppServerProtocolError(
                        f"app-server response id {response_id!r} does not match "
                        f"outstanding id {request_id}"
                    )
                if ("result" in incoming) == ("error" in incoming):
                    raise AppServerProtocolError(
                        f"app-server response id {request_id} must contain exactly "
                        "one of result or error"
                    )
                del self._outstanding[request_id]
                self._completed_ids.add(request_id)
                if "error" in incoming:
                    self._raise_rpc_error(request_id, method, incoming["error"])
                return incoming.get("result")
        except (socket.timeout, TimeoutError) as error:
            self._outstanding.pop(request_id, None)
            self._abort()
            raise AppServerAmbiguousRequestError(
                request_id=request_id,
                method=method,
                reason="timed out before a matching response",
            ) from error
        except AppServerConnectionClosed as error:
            self._outstanding.pop(request_id, None)
            raise AppServerAmbiguousRequestError(
                request_id=request_id,
                method=method,
                reason=str(error),
            ) from error
        except OSError as error:
            self._outstanding.pop(request_id, None)
            self._abort()
            raise AppServerAmbiguousRequestError(
                request_id=request_id,
                method=method,
                reason=f"socket failure before a matching response: {error}",
            ) from error
        except AppServerRpcError:
            raise
        except AppServerProtocolError:
            self._outstanding.pop(request_id, None)
            self._abort()
            raise

    def _handle_incoming_call(self, message: dict[str, Any], deadline: float) -> None:
        method = message.get("method")
        if not isinstance(method, str) or not method:
            raise AppServerProtocolError(
                "app-server request/notification method must be a non-empty string"
            )
        params = message.get("params")
        if "id" not in message:
            if len(self._notifications) >= self.max_queued_notifications:
                raise AppServerProtocolError(
                    "app-server notification queue exceeded its limit of "
                    f"{self.max_queued_notifications}"
                )
            self._notifications.append(AppServerNotification(method, params))
            if self.notification_handler is not None:
                try:
                    self.notification_handler(method, params)
                except Exception as error:
                    raise AppServerProtocolError(
                        f"app-server notification handler {method!r} failed: {error}"
                    ) from error
            return
        server_request_id = message["id"]
        if isinstance(server_request_id, bool) or not isinstance(
            server_request_id, (int, str)
        ):
            raise AppServerProtocolError(
                f"invalid app-server server request id {server_request_id!r}"
            )
        handler = self.server_request_handlers.get(method)
        if handler is None:
            raise AppServerProtocolError(
                f"unknown app-server server request method {method!r} "
                f"id={server_request_id!r}"
            )
        try:
            result = handler(params)
        except Exception as error:
            self._send_json(
                {
                    "id": server_request_id,
                    "error": {"code": -32000, "message": str(error)},
                },
                deadline,
            )
            raise AppServerProtocolError(
                f"app-server server request handler {method!r} failed: {error}"
            ) from error
        self._send_json({"id": server_request_id, "result": result}, deadline)

    def _handle_close_notification(
        self, message: dict[str, Any], deadline: float
    ) -> None:
        if "method" not in message:
            raise AppServerProtocolError(
                "app-server sent JSON-RPC response "
                f"id={message.get('id')!r} while closing"
            )
        method = message.get("method")
        if not isinstance(method, str) or not method:
            raise AppServerProtocolError(
                "app-server request/notification method must be a non-empty string"
            )
        if "id" in message:
            raise AppServerProtocolError(
                f"app-server sent server request {method!r} "
                f"id={message['id']!r} while closing; close-phase handlers are disabled"
            )
        if "result" in message or "error" in message:
            raise AppServerProtocolError(
                f"app-server sent malformed notification {method!r} while closing"
            )
        self._handle_incoming_call(message, deadline)

    def _raise_rpc_error(
        self, request_id: int, method: str, error: Any
    ) -> None:
        if not isinstance(error, dict):
            raise AppServerProtocolError(
                f"app-server response id {request_id} has a malformed error object"
            )
        code = error.get("code")
        message = error.get("message")
        if isinstance(code, bool) or not isinstance(code, int) or not isinstance(message, str):
            raise AppServerProtocolError(
                f"app-server response id {request_id} has a malformed error object"
            )
        error_type = (
            AppServerRequestUnavailableError if code == -32001 else AppServerRpcError
        )
        raise error_type(
            request_id=request_id,
            method=method,
            code=code,
            message=message,
            data=error.get("data"),
        )

    def _send_json(self, message: Mapping[str, Any], deadline: float) -> None:
        try:
            payload = json.dumps(
                message, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise AppServerProtocolError(
                f"app-server JSON-RPC message is not serializable: {error}"
            ) from error
        if len(payload) > self.max_message_bytes:
            raise AppServerProtocolError(
                f"app-server JSON-RPC message exceeds {self.max_message_bytes} bytes"
            )
        self._send_frame(0x1, payload, deadline)

    def _receive_json(self, deadline: float) -> dict[str, Any]:
        while True:
            opcode, payload = self._receive_frame(deadline)
            if opcode == 0x9:
                self._send_frame(0xA, payload, deadline)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                self._handle_peer_close(payload, deadline)
            if opcode != 0x1:
                raise AppServerProtocolError(
                    f"unsupported app-server WebSocket opcode 0x{opcode:x}"
                )
            return self._decode_json_payload(payload)

    def _decode_json_payload(self, payload: bytes) -> dict[str, Any]:
        if len(payload) > self.max_message_bytes:
            raise AppServerProtocolError(
                f"app-server JSON-RPC message exceeds {self.max_message_bytes} bytes"
            )
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise AppServerProtocolError(
                "app-server WebSocket text frame is not valid UTF-8"
            ) from error
        try:
            message = json.loads(text)
        except json.JSONDecodeError as error:
            raise AppServerProtocolError(
                f"app-server WebSocket text frame is not valid JSON: {error.msg}"
            ) from error
        if not isinstance(message, dict):
            raise AppServerProtocolError(
                "app-server JSON-RPC message must be a JSON object"
            )
        return message

    def _send_frame(self, opcode: int, payload: bytes, deadline: float) -> None:
        if self._closed:
            raise AppServerConnectionClosed("app-server connection is closed")
        if len(payload) > self.max_frame_bytes:
            raise AppServerProtocolError(
                f"app-server WebSocket frame exceeds {self.max_frame_bytes} bytes"
            )
        if opcode >= 0x8 and len(payload) > 125:
            raise AppServerProtocolError(
                "app-server WebSocket control frame exceeds 125 bytes"
            )
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(
            value ^ mask[index % 4] for index, value in enumerate(payload)
        )
        self._set_remaining_timeout(deadline)
        self._socket.sendall(bytes(header) + masked)

    def _receive_frame(self, deadline: float) -> tuple[int, bytes]:
        first, second = self._read_exact(
            2, deadline, permit_frame_boundary_eof=True
        )
        if first & 0x70:
            raise AppServerProtocolError(
                "app-server WebSocket frame has unsupported RSV bits"
            )
        if not first & 0x80:
            raise AppServerProtocolError(
                "fragmented app-server WebSocket frames are not supported"
            )
        opcode = first & 0x0F
        if opcode not in {0x1, 0x2, 0x8, 0x9, 0xA}:
            raise AppServerProtocolError(
                f"unsupported app-server WebSocket opcode 0x{opcode:x}"
            )
        if second & 0x80:
            raise AppServerProtocolError(
                "app-server sent an invalid masked server frame"
            )
        length_code = second & 0x7F
        if length_code < 126:
            length = length_code
        elif length_code == 126:
            length = struct.unpack("!H", self._read_exact(2, deadline))[0]
            if length < 126:
                raise AppServerProtocolError(
                    "app-server WebSocket frame uses a non-canonical 16-bit length"
                )
        else:
            raw_length = self._read_exact(8, deadline)
            if raw_length[0] & 0x80:
                raise AppServerProtocolError(
                    "app-server WebSocket 64-bit length has its high bit set"
                )
            length = struct.unpack("!Q", raw_length)[0]
            if length < 65536:
                raise AppServerProtocolError(
                    "app-server WebSocket frame uses a non-canonical 64-bit length"
                )
        if opcode >= 0x8 and length > 125:
            raise AppServerProtocolError(
                "app-server WebSocket control frame exceeds 125 bytes"
            )
        if length > self.max_frame_bytes:
            raise AppServerProtocolError(
                f"app-server WebSocket frame exceeds {self.max_frame_bytes} bytes"
            )
        payload = self._read_exact(length, deadline)
        return opcode, payload

    def _read_exact(
        self,
        length: int,
        deadline: float,
        *,
        permit_frame_boundary_eof: bool = False,
    ) -> bytes:
        output = bytearray()
        if self._buffer:
            take = min(length, len(self._buffer))
            output.extend(self._buffer[:take])
            del self._buffer[:take]
        while len(output) < length:
            self._set_remaining_timeout(deadline)
            chunk = self._socket.recv(length - len(output))
            if not chunk:
                at_frame_boundary = permit_frame_boundary_eof and not output
                self._abort()
                raise AppServerConnectionClosed(
                    "app-server closed the transport at a WebSocket frame boundary"
                    if at_frame_boundary
                    else "app-server disconnected before a complete WebSocket frame",
                    at_frame_boundary=at_frame_boundary,
                )
            output.extend(chunk)
        return bytes(output)

    def _handle_peer_close(self, payload: bytes, deadline: float) -> None:
        self._validate_close_payload(payload)
        if not self._close_sent:
            self._send_frame(0x8, payload, deadline)
            self._close_sent = True
        code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
        self._abort()
        detail = f" with code {code}" if code is not None else ""
        raise AppServerConnectionClosed(f"app-server closed the WebSocket{detail}")

    def _validate_close_payload(self, payload: bytes) -> None:
        if len(payload) == 1:
            raise AppServerProtocolError(
                "app-server WebSocket close frame has an invalid one-byte payload"
            )
        if len(payload) > 2:
            try:
                payload[2:].decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise AppServerProtocolError(
                    "app-server WebSocket close reason is not valid UTF-8"
                ) from error

    def _set_remaining_timeout(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("deadline exceeded")
        self._socket.settimeout(remaining)

    def _deadline(self) -> float:
        return time.monotonic() + self.request_timeout_seconds

    def _abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()


def check_app_server(
    endpoint: str = DEFAULT_APP_SERVER_ENDPOINT,
    *,
    codex_home: str | Path | None = None,
    timeout_seconds: float = DEFAULT_HANDSHAKE_TIMEOUT_SECONDS,
) -> AppServerEndpoint:
    """Initialize one app-server connection, close it, and create no thread."""

    connection = AppServerConnection(
        endpoint,
        codex_home=codex_home,
        handshake_timeout_seconds=timeout_seconds,
        request_timeout_seconds=timeout_seconds,
        close_timeout_seconds=timeout_seconds,
    )
    try:
        connection.close()
    except Exception:
        connection._abort()
        raise
    return connection.resolved_endpoint


def _validate_socket_path(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as error:
        raise AppServerError(f"app-server Unix socket does not exist: {path}") from error
    except OSError as error:
        raise AppServerError(
            f"cannot inspect app-server Unix socket {path}: {error}"
        ) from error
    if not stat.S_ISSOCK(mode):
        raise AppServerError(f"app-server endpoint is not a Unix socket: {path}")


def _header_has_token(
    headers: Mapping[str, list[str]], name: str, expected: str
) -> bool:
    tokens = {
        token.strip().lower()
        for value in headers.get(name, [])
        for token in value.split(",")
    }
    return expected.lower() in tokens


def _positive_timeout(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise AppServerError(f"app-server {label} timeout must be greater than zero")
    return float(value)


def _positive_limit(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AppServerError(f"app-server {label} limit must be a positive integer")
    return value
