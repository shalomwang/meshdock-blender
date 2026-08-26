from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..core.errors import BridgeUnavailableError, PipelineError
from .protocol import ALLOWED_METHODS, PROTOCOL_VERSION, bridge_descriptor_path


class RemotePipelineError(PipelineError):
    def __init__(self, code: str, message: str, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.code = code


class BlenderBridgeClient:
    def __init__(self, descriptor: Path | None = None, timeout: float = 120.0) -> None:
        self.descriptor = descriptor or bridge_descriptor_path()
        self.timeout = timeout

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method not in ALLOWED_METHODS:
            raise ValueError("method is not allowed")
        try:
            descriptor = json.loads(self.descriptor.read_text(encoding="utf-8"))
            if descriptor.get("protocol_version") != PROTOCOL_VERSION:
                raise BridgeUnavailableError("Blender bridge protocol version mismatch")
            if descriptor.get("host") != "127.0.0.1":
                raise BridgeUnavailableError("Blender bridge is not loopback-only")
            port = int(descriptor["port"])
            token = str(descriptor["token"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise BridgeUnavailableError("Start Blender and enable MeshDock") from exc

        body = json.dumps({"method": method, "params": params or {}}).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/rpc",
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
            except Exception:
                raise BridgeUnavailableError("Blender bridge rejected the request") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BridgeUnavailableError("Blender bridge is unavailable") from exc
        if not payload.get("ok"):
            error = payload.get("error", {})
            diagnostics = error.get("diagnostics")
            raise RemotePipelineError(
                str(error.get("code", "remote_error")),
                str(error.get("message", "request failed")),
                diagnostics if isinstance(diagnostics, dict) else None,
            )
        return payload.get("result")
