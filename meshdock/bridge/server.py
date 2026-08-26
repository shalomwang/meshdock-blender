from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from ..core.errors import AuthenticationError, PipelineError, ValidationError
from .protocol import (
    ALLOWED_METHODS,
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    bridge_descriptor_path,
    safe_json,
)

Dispatch = Callable[[str, dict[str, Any]], Any]


@dataclass(slots=True)
class _PendingCall:
    method: str
    params: dict[str, Any]
    completed: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Exception | None = None


class BlenderBridgeServer:
    """Authenticated loopback bridge with Blender-main-thread dispatch."""

    def __init__(self, dispatch: Dispatch, descriptor: Path | None = None) -> None:
        self._dispatch = dispatch
        self._descriptor = descriptor or bridge_descriptor_path()
        self._token = secrets.token_urlsafe(32)
        self._pending: queue.Queue[_PendingCall] = queue.Queue(maxsize=128)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        if self.running:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "MeshDockBridge/0.9"

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                if self.path != "/rpc":
                    self._write(404, {"ok": False, "error": {"code": "not_found"}})
                    return
                supplied = self.headers.get("Authorization", "")
                expected = f"Bearer {owner._token}"
                if not hmac.compare_digest(supplied, expected):
                    self._write(401, {"ok": False, "error": {"code": AuthenticationError.code}})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if not (0 < length <= MAX_REQUEST_BYTES):
                    self._write(413, {"ok": False, "error": {"code": "invalid_request_size"}})
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                    method = str(payload["method"])
                    params = payload.get("params", {})
                    if method not in ALLOWED_METHODS or not isinstance(params, dict):
                        raise ValidationError("method or params are invalid")
                    result = owner.submit(method, params, timeout=115.0)
                    self._write(200, {"ok": True, "result": result})
                except PipelineError as exc:
                    self._write(400, {"ok": False, "error": exc.public_error()})
                except TimeoutError:
                    self._write(504, {"ok": False, "error": {"code": "bridge_timeout"}})
                except Exception:
                    # Tracebacks go nowhere near the MCP boundary: they may contain URLs.
                    self._write(500, {"ok": False, "error": {"code": "internal_error"}})

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _write(self, status: int, payload: dict[str, Any]) -> None:
                body = safe_json(payload)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="meshdock-bridge", daemon=True
        )
        self._thread.start()
        port = int(self._server.server_address[1])
        self._descriptor.parent.mkdir(parents=True, exist_ok=True)
        temp = self._descriptor.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "host": "127.0.0.1",
                    "port": port,
                    "token": self._token,
                    "pid": os.getpid(),
                }
            ),
            encoding="utf-8",
        )
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(self._descriptor)

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self._descriptor.unlink(missing_ok=True)
        except OSError:
            pass
        self._token = secrets.token_urlsafe(32)

    def submit(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        call = _PendingCall(method=method, params=params)
        try:
            self._pending.put(call, timeout=1.0)
        except queue.Full as exc:
            raise TimeoutError("bridge queue is full") from exc
        if not call.completed.wait(timeout):
            raise TimeoutError("Blender did not process the bridge request")
        if call.error:
            raise call.error
        return call.result

    def drain_on_main_thread(self, limit: int = 8) -> int:
        processed = 0
        while processed < limit:
            try:
                call = self._pending.get_nowait()
            except queue.Empty:
                break
            try:
                call.result = self._dispatch(call.method, call.params)
            except Exception as exc:
                call.error = exc
            finally:
                call.completed.set()
                processed += 1
        return processed
