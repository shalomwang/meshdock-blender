"""Temporary background Blender host for the STDIO MCP smoke test."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import meshdock  # noqa: E402
from meshdock.blender.runtime import get_runtime  # noqa: E402

STOP_FILE = ROOT / "staging" / "mcp-smoke.stop"


def main() -> None:
    STOP_FILE.unlink(missing_ok=True)
    meshdock.register()
    print("AI3D_BRIDGE_SMOKE_HOST_READY", flush=True)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not STOP_FILE.exists():
            runtime = get_runtime()
            runtime.bridge.drain_on_main_thread()
            runtime.drain_blender_tasks()
            time.sleep(0.01)
    finally:
        meshdock.unregister()
        STOP_FILE.unlink(missing_ok=True)
        print("AI3D_BRIDGE_SMOKE_HOST_STOPPED", flush=True)


if __name__ == "__main__":
    main()
