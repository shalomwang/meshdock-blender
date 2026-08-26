from __future__ import annotations

from typing import Any

from meshdock.sidecar.server import (
    ASSET_SPEC_SCHEMA, MCP_PROTOCOL_VERSION, McpServer, TOOL_NAMES,
)


class FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params or {}))
        return {"ok_from": method}


def test_initialize_returns_instructions_and_tools_capability() -> None:
    server = McpServer(FakeBridge())
    response = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "test-version"}}
    )
    assert response["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert response["result"]["capabilities"]["tools"] == {"listChanged": False}
    assert "Never ask for keys" in response["result"]["instructions"]


def test_create_job_wraps_arguments_as_spec() -> None:
    bridge = FakeBridge()
    server = McpServer(bridge)
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "create_asset_job",
                "arguments": {"asset_name": "small_crate", "prompt": "A small crate"},
            },
        }
    )
    assert response["result"]["isError"] is False
    assert bridge.calls == [
        ("create_asset_job", {"spec": {"asset_name": "small_crate", "prompt": "A small crate"}})
    ]


def test_unknown_tool_is_rejected_before_bridge() -> None:
    bridge = FakeBridge()
    server = McpServer(bridge)
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "execute_python", "arguments": {"code": "pass"}},
        }
    )
    assert response["error"]["code"] == -32602
    assert bridge.calls == []


def test_dynamic_capability_and_task_control_tools_are_exposed() -> None:
    assert {
        "get_generation_constraints", "get_process_capabilities", "pause_asset_job",
        "unpause_asset_job", "set_asset_job_priority", "regenerate_candidate",
        "pause_process_task", "unpause_process_task",
    } <= TOOL_NAMES


def test_development_mock_is_not_part_of_the_public_mcp_schema() -> None:
    assert "mock" not in ASSET_SPEC_SCHEMA["properties"]["provider"]["enum"]
