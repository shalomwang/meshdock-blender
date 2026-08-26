from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Protocol

from ..bridge.client import BlenderBridgeClient
from ..core.errors import PipelineError

MCP_PROTOCOL_VERSION = "2025-06-18"
REAL_PROVIDER_ENUM = [
    "tripo_cn", "tripo_global", "hunyuan_direct", "tokenhub_cn", "tokenhub_global",
]
GENERATION_PROVIDER_ENUM = [
    "auto", *REAL_PROVIDER_ENUM, "compare_cn", "compare_global",
]
PROCESS_PROVIDER_ENUM = ["auto", *REAL_PROVIDER_ENUM]


class Bridge(Protocol):
    def call(self, method: str, params: dict[str, Any] | None = None) -> Any: ...


ASSET_SPEC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["asset_name"],
    "properties": {
        "asset_name": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_]{1,63}$",
            "description": "Stable lowercase snake_case asset name.",
        },
        "prompt": {"type": "string", "maxLength": 4000, "default": ""},
        "input_mode": {"type": "string", "enum": ["text", "image", "multiview"], "default": "text"},
        "reference_images": {
            "type": "object",
            "additionalProperties": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
            "description": "Opaque reference ids registered by a Blender user, keyed by view.",
            "default": {},
        },
        "asset_profile": {"type": "string", "default": "small_prop_v1"},
        "provider": {
            "type": "string",
            "enum": GENERATION_PROVIDER_ENUM,
            "default": "auto",
        },
        "candidate_count": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
        "max_estimated_credits": {"type": "number", "minimum": 0, "default": 0},
        "allow_over_budget": {"type": "boolean", "default": False},
        "priority": {"type": "integer", "minimum": 0, "maximum": 100, "default": 50},
        "advanced": {
            "type": "object",
            "description": "Provider-namespaced options validated by the selected adapter.",
            "default": {},
        },
    },
}


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required or [],
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_pipeline_capabilities",
        "description": "Read provider capabilities, generation presets, task lifecycle and forbidden low-level capabilities.",
        "inputSchema": _object_schema({}),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "provider_status",
        "description": "Read provider configured/available booleans. Never returns credentials or credential metadata.",
        "inputSchema": _object_schema(
            {"provider": {"type": "string", "enum": [*REAL_PROVIDER_ENUM, "compare_cn", "compare_global"]}}
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "get_generation_constraints",
        "description": "Resolve provider/model-specific input modes, view counts, required views, formats and size limits before creating a job.",
        "inputSchema": _object_schema({
            "provider": {"type": "string", "enum": GENERATION_PROVIDER_ENUM, "default": "auto"},
            "advanced": {"type": "object", "default": {}},
        }),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "get_process_capabilities",
        "description": "Resolve only post-process operations and providers compatible with a concrete candidate.",
        "inputSchema": _object_schema({
            "job_id": {"type": "string"}, "candidate_id": {"type": "string"},
            "provider": {"type": "string", "enum": PROCESS_PROVIDER_ENUM, "default": "auto"},
        }, ["job_id", "candidate_id"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "list_reference_images",
        "description": "List opaque image references previously registered by a Blender user. Never returns local paths.",
        "inputSchema": _object_schema({}),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "list_asset_jobs",
        "description": "List recent persistent jobs, including recovery state, usage and opaque candidate ids.",
        "inputSchema": _object_schema({
            "include_archived": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        }),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "create_asset_job",
        "description": "Validate a structured asset specification and create a Blender-owned draft job.",
        "inputSchema": ASSET_SPEC_SCHEMA,
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "create_asset_batch",
        "description": "Create 1-20 persistent asset jobs; optional generation may incur provider charges.",
        "inputSchema": _object_schema(
            {
                "specs": {"type": "array", "minItems": 1, "maxItems": 20, "items": ASSET_SPEC_SCHEMA},
                "generate": {"type": "boolean", "default": False},
            },
            ["specs"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "generate_candidates",
        "description": "Generate 1-4 candidates for an existing job and immediately stage them locally. Real providers may incur cost; request approval before calling.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}}, ["job_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "get_job_status",
        "description": "Read normalized job progress and opaque candidate ids. Does not return provider URLs or local paths.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}}, ["job_id"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "cancel_asset_job",
        "description": "Stop local polling/download work for a submitted job. Provider-side work may continue and may still be billed.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}}, ["job_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    },
    {
        "name": "resume_asset_job",
        "description": "Resume polling and local download for provider tasks submitted by a previous Blender session. Does not submit new generation tasks.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}}, ["job_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "pause_asset_job",
        "description": "Pause local queueing, provider polling and downloads. Remote provider work may continue.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}}, ["job_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "unpause_asset_job",
        "description": "Continue a locally paused generation job without resubmitting provider work.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}}, ["job_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "set_asset_job_priority",
        "description": "Change draft/queued priority from 0 to 100; active provider work is not preempted.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}, "priority": {"type": "integer", "minimum": 0, "maximum": 100}}, ["job_id", "priority"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "retry_asset_job",
        "description": "Create and submit a new job with the same specification. This may incur provider charges and requires approval.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}}, ["job_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "regenerate_candidate",
        "description": "Create one new candidate from the source candidate's original specification. May incur provider charges and requires approval.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}, "candidate_id": {"type": "string"}}, ["job_id", "candidate_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "archive_asset_job",
        "description": "Hide an inactive job from the default job list without deleting its staged files.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}}, ["job_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "purge_archived_jobs",
        "description": "Permanently delete archived job staging directories older than the requested age.",
        "inputSchema": _object_schema(
            {"older_than_days": {"type": "integer", "minimum": 0, "maximum": 3650, "default": 30}}
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    },
    {
        "name": "import_candidate",
        "description": "Queue import of one staged candidate into an isolated Blender Collection and return an opaque Blender task id.",
        "inputSchema": _object_schema(
            {"job_id": {"type": "string"}, "candidate_id": {"type": "string"}},
            ["job_id", "candidate_id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "normalize_candidate",
        "description": "Queue creation of a non-destructive normalized Blender copy and return an opaque Blender task id.",
        "inputSchema": _object_schema(
            {
                "job_id": {"type": "string"}, "candidate_id": {"type": "string"},
                "options": {"type": "object", "default": {}},
            },
            ["job_id", "candidate_id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "render_review_pack",
        "description": "Queue a consistent local turntable review render and return an opaque Blender task id.",
        "inputSchema": _object_schema(
            {"job_id": {"type": "string"}, "candidate_id": {"type": "string"}},
            ["job_id", "candidate_id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    *[
        {
            "name": tool_name,
            "description": description,
            "inputSchema": _object_schema(
                {
                    "job_id": {"type": "string"},
                    "candidate_id": {"type": "string"},
                    "provider": {"type": "string", "enum": PROCESS_PROVIDER_ENUM, "default": "auto"},
                    "params": {"type": "object", "default": {}},
                },
                ["job_id", "candidate_id"],
            ),
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
        }
        for tool_name, description in (
            ("retopologize_candidate", "Create an immutable retopologized artifact from a candidate."),
            ("unwrap_candidate", "Create an immutable UV-unwrapped artifact from a candidate."),
            ("texture_candidate", "Create an immutable retextured artifact from a candidate."),
            ("segment_candidate", "Create an immutable segmented-parts artifact from a candidate."),
            ("convert_candidate", "Create an immutable converted-format artifact from a candidate."),
            ("check_riggability", "Check character riggability and return provider diagnostics without creating a model."),
            ("rig_candidate", "Create an immutable auto-rigged character artifact."),
            ("animate_candidate", "Create an immutable animated character artifact from a rig result."),
        )
    ],
    {
        "name": "get_process_status",
        "description": "Read one provider post-processing task and its resulting opaque artifact id.",
        "inputSchema": _object_schema(
            {"job_id": {"type": "string"}, "task_id": {"type": "string"}}, ["job_id", "task_id"]
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "cancel_process_task",
        "description": "Stop local polling/download for an active post-processing task.",
        "inputSchema": _object_schema(
            {"job_id": {"type": "string"}, "task_id": {"type": "string"}}, ["job_id", "task_id"]
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    },
    {
        "name": "pause_process_task",
        "description": "Pause local polling/download for an active provider process; remote work may continue.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}, "task_id": {"type": "string"}}, ["job_id", "task_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "unpause_process_task",
        "description": "Continue local polling/download for a paused provider process.",
        "inputSchema": _object_schema({"job_id": {"type": "string"}, "task_id": {"type": "string"}}, ["job_id", "task_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "resume_process_task",
        "description": "Resume provider polling/download for a post-process task interrupted by a prior Blender session.",
        "inputSchema": _object_schema(
            {"job_id": {"type": "string"}, "task_id": {"type": "string"}},
            ["job_id", "task_id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "list_candidate_actions",
        "description": "List animation Actions imported with one character candidate.",
        "inputSchema": _object_schema(
            {"job_id": {"type": "string"}, "candidate_id": {"type": "string"}},
            ["job_id", "candidate_id"],
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "activate_candidate_action",
        "description": "Activate one candidate-owned animation Action for Blender preview.",
        "inputSchema": _object_schema(
            {
                "job_id": {"type": "string"}, "candidate_id": {"type": "string"},
                "action": {"type": "string", "maxLength": 256},
            },
            ["job_id", "candidate_id", "action"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "export_selected_asset",
        "description": "Queue export of the Blender-user-selected candidate and return an opaque Blender task id.",
        "inputSchema": _object_schema(
            {
                "job_id": {"type": "string"},
                "format": {"type": "string", "enum": ["glb", "gltf", "fbx", "obj", "stl", "usd"], "default": "glb"},
                "options": {"type": "object", "default": {}},
            },
            ["job_id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "get_blender_task_status",
        "description": "Read queued/running/completed state and result for a long Blender scene operation.",
        "inputSchema": _object_schema({"task_id": {"type": "string"}}, ["task_id"]),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "cancel_blender_task",
        "description": "Cancel a Blender operation only while it remains queued; running scene operations are not interrupted unsafely.",
        "inputSchema": _object_schema({"task_id": {"type": "string"}}, ["task_id"]),
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    },
]

TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)

INSTRUCTIONS = (
    "Use this server only for high-level 3D generation and candidate processing. Read capabilities and persistent "
    "jobs first. Ask for approval before generation or retry because real providers may charge, and before import "
    "because it changes the Blender scene. Never ask for keys: credentials are resolved only inside Blender. "
    "The server cannot select a candidate; export works only after a Blender user selects one."
)


@dataclass(slots=True)
class McpServer:
    bridge: Bridge

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        if request_id is None and method and method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "meshdock-blender", "version": "0.9.6"},
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = request.get("params", {})
                name = str(params.get("name", ""))
                arguments = params.get("arguments", {})
                if name not in TOOL_NAMES or not isinstance(arguments, dict):
                    return self._error(request_id, -32602, "Unknown tool or invalid arguments")
                bridge_method, bridge_params = self._bridge_call(name, arguments)
                value = self.bridge.call(bridge_method, bridge_params)
                text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                result = {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": value if isinstance(value, dict) else {"result": value},
                    "isError": False,
                }
            else:
                return self._error(request_id, -32601, "Method not found")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except PipelineError as exc:
            value = {"error": exc.public_error()}
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                    "structuredContent": value,
                    "isError": True,
                },
            }
        except Exception:
            return self._error(request_id, -32603, "Internal error")

    @staticmethod
    def _bridge_call(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if name == "create_asset_job":
            return name, {"spec": arguments}
        operations = {
            "retopologize_candidate": "retopology",
            "unwrap_candidate": "uv",
            "texture_candidate": "texture",
            "segment_candidate": "segment",
            "convert_candidate": "convert",
            "check_riggability": "rig_check",
            "rig_candidate": "rig",
            "animate_candidate": "animate",
        }
        if name in operations:
            return "submit_candidate_process", {**arguments, "operation": operations[name]}
        return name, arguments

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def run_stdio(server: McpServer | None = None) -> int:
    active = server or McpServer(BlenderBridgeClient())
    for raw_line in sys.stdin.buffer:
        if not raw_line.strip():
            continue
        try:
            request = json.loads(raw_line)
            if not isinstance(request, dict):
                raise ValueError
            response = active.handle(request)
        except Exception:
            response = McpServer._error(None, -32700, "Parse error")
        if response is not None:
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            sys.stdout.buffer.write(encoded + b"\n")
            sys.stdout.buffer.flush()
    return 0
