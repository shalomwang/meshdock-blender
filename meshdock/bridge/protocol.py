from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1_000_000
ALLOWED_METHODS = frozenset(
    {
        "get_pipeline_capabilities",
        "get_generation_constraints",
        "get_process_capabilities",
        "provider_status",
        "list_reference_images",
        "create_asset_job",
        "create_asset_batch",
        "generate_candidates",
        "list_asset_jobs",
        "cancel_asset_job",
        "pause_asset_job",
        "unpause_asset_job",
        "set_asset_job_priority",
        "get_job_status",
        "resume_asset_job",
        "retry_asset_job",
        "regenerate_candidate",
        "archive_asset_job",
        "purge_archived_jobs",
        "import_candidate",
        "normalize_candidate",
        "render_review_pack",
        "submit_candidate_process",
        "get_process_status",
        "cancel_process_task",
        "pause_process_task",
        "unpause_process_task",
        "resume_process_task",
        "list_candidate_actions",
        "activate_candidate_action",
        "export_selected_asset",
        "ingest_tripo_webhook",
        "get_blender_task_status",
        "cancel_blender_task",
    }
)

_SECRET_FIELD = re.compile(
    r"(^|[_-])(api[_-]?key|authorization|secret(?:[_-]?key)?|access[_-]?token|token|credential)s?($|[_-])",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")


def bridge_descriptor_path() -> Path:
    override = (
        os.environ.get("MESHDOCK_BRIDGE_DESCRIPTOR")
        or os.environ.get("AI3D_PIPELINE_BRIDGE_DESCRIPTOR")
    )
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_RUNTIME_DIR", Path.home() / ".cache"))
    return (base / "MeshDock" / "bridge.json").resolve()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SECRET_FIELD.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        cleaned = _BEARER.sub("Bearer [REDACTED]", value)
        try:
            parts = urlsplit(cleaned)
            if parts.scheme in {"http", "https"} and parts.query:
                cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, "[REDACTED]", parts.fragment))
        except ValueError:
            pass
        return cleaned
    return value


def safe_json(value: Any) -> bytes:
    return json.dumps(redact(value), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
