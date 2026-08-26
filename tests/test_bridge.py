from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from meshdock.bridge.client import BlenderBridgeClient
from meshdock.bridge.server import BlenderBridgeServer


def test_authenticated_bridge_round_trip(tmp_path: Path) -> None:
    descriptor = tmp_path / "bridge.json"
    server = BlenderBridgeServer(
        lambda method, params: {"method": method, "provider": params.get("provider")},
        descriptor=descriptor,
    )
    server.start()
    result: dict[str, object] = {}

    def call() -> None:
        result.update(BlenderBridgeClient(descriptor, timeout=2.0).call("provider_status", {"provider": "mock"}))

    worker = threading.Thread(target=call)
    worker.start()
    deadline = time.monotonic() + 2.0
    while worker.is_alive() and time.monotonic() < deadline:
        server.drain_on_main_thread()
        worker.join(timeout=0.01)
    server.stop()

    assert result == {"method": "provider_status", "provider": "mock"}
    assert not descriptor.exists()


def test_bridge_descriptor_contains_no_provider_credentials(tmp_path: Path) -> None:
    descriptor = tmp_path / "bridge.json"
    server = BlenderBridgeServer(lambda _method, _params: {}, descriptor=descriptor)
    server.start()
    try:
        value = json.loads(descriptor.read_text(encoding="utf-8"))
        assert set(value) == {"protocol_version", "host", "port", "token", "pid"}
        assert value["host"] == "127.0.0.1"
    finally:
        server.stop()
