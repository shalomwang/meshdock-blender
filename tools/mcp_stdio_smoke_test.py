"""Exercise the actual newline-delimited STDIO MCP server against Blender."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STOP_FILE = ROOT / "staging" / "mcp-smoke.stop"


def main() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "meshdock.sidecar"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    counter = 0

    def request(method: str, params=None):
        nonlocal counter
        counter += 1
        message = {"jsonrpc": "2.0", "id": counter, "method": method}
        if params is not None:
            message["params"] = params
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        assert response["id"] == counter, response
        return response

    try:
        initialized = request("initialize", {"protocolVersion": "2025-06-18"})
        assert initialized["result"]["serverInfo"]["version"] == "0.9.7"
        listed = request("tools/list")
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert "select_candidate" not in names
        assert {
            "list_asset_jobs", "generate_candidates", "normalize_candidate",
            "export_selected_asset", "get_blender_task_status", "cancel_blender_task",
            "restore_asset_job",
        } <= names
        create_tool = next(
            tool for tool in listed["result"]["tools"] if tool["name"] == "create_asset_job"
        )
        assert "mock" not in create_tool["inputSchema"]["properties"]["provider"]["enum"]
        capabilities = request(
            "tools/call", {"name": "get_pipeline_capabilities", "arguments": {}}
        )["result"]
        assert capabilities["isError"] is False, capabilities
        providers = capabilities["structuredContent"]["providers"]
        assert all(item["provider"] != "mock" for item in providers)
        print("MCP_STDIO_SMOKE_OK", len(providers))
    finally:
        STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STOP_FILE.write_text("stop", encoding="utf-8")
        if process.stdin:
            process.stdin.close()
        process.wait(timeout=5)


if __name__ == "__main__":
    main()
