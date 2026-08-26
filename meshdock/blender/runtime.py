from __future__ import annotations

import base64
import os
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..bridge.server import BlenderBridgeServer
from ..core.errors import ValidationError
from ..core.errors import InvalidTransitionError, PipelineError
from ..core.models import utc_now
from ..core.service import AssetPipelineService
from ..credentials import SessionCredentialVault
from ..providers import (
    CompareAdapter, HunyuanDirectAdapter, MockProvider, TokenHubAdapter, TripoAdapter,
)
from ..providers.webhooks import TripoWebhookVerifier


@dataclass(slots=True)
class _BlenderTask:
    operation: str
    callback: Any = field(repr=False)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "queued"
    result: Any = None
    error: dict[str, Any] | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def public_dict(self) -> dict[str, Any]:
        value = {
            "id": self.id, "operation": self.operation, "state": self.state,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }
        if self.state == "completed":
            value["result"] = self.result
        if self.error:
            value["error"] = self.error
        return value


class BlenderPipelineRuntime:
    def __init__(self) -> None:
        self.credentials = SessionCredentialVault()
        self.webhooks = TripoWebhookVerifier(lambda: self.credentials.use("tripo_webhook"))
        self._blender_tasks: dict[str, _BlenderTask] = {}
        self._blender_queue: deque[str] = deque()
        self._blender_task_lock = threading.RLock()
        self._auto_import_jobs: set[str] = set()
        self._auto_import_errors: dict[str, str] = {}
        tripo_cn = TripoAdapter(
            self.credentials, provider_id="tripo_cn",
            base_url="https://openapi.tripo3d.com/v3",
        )
        tripo_global = TripoAdapter(
            self.credentials, provider_id="tripo_global",
            base_url="https://openapi.tripo3d.ai/v3",
        )
        hunyuan_direct = HunyuanDirectAdapter(self.credentials)
        tokenhub_cn = TokenHubAdapter(
            self.credentials, provider_id="tokenhub_cn",
            base_url="https://tokenhub.tencentmaas.com/v1/api/3d",
        )
        tokenhub_global = TokenHubAdapter(
            self.credentials, provider_id="tokenhub_global",
            base_url="https://tokenhub-intl.tencentcloudmaas.com/v1/api/3d",
        )
        providers = {
            "tripo_cn": tripo_cn, "tripo_global": tripo_global,
            "hunyuan_direct": hunyuan_direct,
            "tokenhub_cn": tokenhub_cn, "tokenhub_global": tokenhub_global,
            "compare_cn": CompareAdapter(tripo_cn, hunyuan_direct, provider_id="compare_cn"),
            "compare_global": CompareAdapter(
                tripo_global, tokenhub_global, provider_id="compare_global"
            ),
            # Hidden recovery adapter for 0.7.x jobs, which paired the Tripo
            # global endpoint with mainland Hunyuan TokenHub.
            "compare_legacy": CompareAdapter(
                tripo_global, tokenhub_cn, provider_id="compare_legacy"
            ),
        }
        # The deterministic mock exists only for automated development checks.
        # It is absent from normal runtime capabilities, Blender UI and public MCP schemas.
        if os.environ.get("MESHDOCK_DEV_MOCK") == "1":
            providers["mock"] = MockProvider()
        from .scene_ops import (
            activate_candidate_action, arrange_candidate_collections, candidate_actions, delete_candidate_scene,
            export_reviewed_asset, import_candidate, prepare_game_asset, render_review_pack,
            set_candidate_visibility,
        )

        self.service = AssetPipelineService(
            providers,
            importer=import_candidate,
            preparer=prepare_game_asset,
            exporter=export_reviewed_asset,
            review_renderer=render_review_pack,
            visibility=set_candidate_visibility,
            candidate_remover=delete_candidate_scene,
            action_lister=candidate_actions,
            action_activator=activate_candidate_action,
        )
        self.bridge = BlenderBridgeServer(self.dispatch)
        self._arrange_candidate_collections = arrange_candidate_collections

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        routes = {
            "get_pipeline_capabilities": lambda: self.service.capabilities(),
            "get_generation_constraints": lambda: self.service.generation_constraints(
                str(params.get("provider", "auto")), dict(params.get("advanced", {}))
            ),
            "get_process_capabilities": lambda: self.service.available_process_operations(
                str(params.get("job_id", "")), str(params.get("candidate_id", "")),
                str(params.get("provider", "auto")),
            ),
            "provider_status": lambda: self.service.provider_status(params.get("provider")),
            "list_reference_images": lambda: self.service.list_reference_images(),
            "create_asset_job": lambda: self._create_bridge_job(dict(params.get("spec", {}))),
            "create_asset_batch": lambda: self._create_bridge_batch(
                list(params.get("specs", [])), generate=bool(params.get("generate", False))
            ),
            "list_asset_jobs": lambda: self.service.list_jobs(
                include_archived=bool(params.get("include_archived", False)), limit=int(params.get("limit", 50))
            ),
            "generate_candidates": lambda: self.service.generate_candidates(str(params.get("job_id", ""))),
            "cancel_asset_job": lambda: self.service.cancel_job(str(params.get("job_id", ""))),
            "pause_asset_job": lambda: self.service.pause_job(str(params.get("job_id", ""))),
            "unpause_asset_job": lambda: self.service.resume_paused_job(str(params.get("job_id", ""))),
            "set_asset_job_priority": lambda: self.service.set_job_priority(
                str(params.get("job_id", "")), int(params.get("priority", 50))
            ),
            "get_job_status": lambda: self.service.get_job(str(params.get("job_id", ""))),
            "resume_asset_job": lambda: self.service.resume_job(str(params.get("job_id", ""))),
            "retry_asset_job": lambda: self.service.retry_job(str(params.get("job_id", ""))),
            "regenerate_candidate": lambda: self.service.regenerate_candidate(
                str(params.get("job_id", "")), str(params.get("candidate_id", ""))
            ),
            "archive_asset_job": lambda: self.service.archive_job(str(params.get("job_id", ""))),
            "restore_asset_job": lambda: self.service.restore_job(str(params.get("job_id", ""))),
            "purge_archived_jobs": lambda: self.service.purge_archived_jobs(
                int(params.get("older_than_days", 30))
            ),
            "import_candidate": lambda: self._submit_blender_task(
                "import_candidate", lambda: self.service.import_candidate(
                    str(params.get("job_id", "")), str(params.get("candidate_id", ""))
                )
            ),
            "normalize_candidate": lambda: self._submit_blender_task(
                "normalize_candidate", lambda: self.service.normalize_candidate(
                    str(params.get("job_id", "")), str(params.get("candidate_id", "")),
                    dict(params.get("options", {})),
                )
            ),
            "render_review_pack": lambda: self._submit_blender_task(
                "render_review_pack", lambda: self.service.render_review_pack(
                    str(params.get("job_id", "")), str(params.get("candidate_id", ""))
                )
            ),
            "submit_candidate_process": lambda: self.service.submit_candidate_process(
                str(params.get("job_id", "")), str(params.get("candidate_id", "")),
                str(params.get("operation", "")), str(params.get("provider", "auto")),
                dict(params.get("params", {})),
            ),
            "get_process_status": lambda: self.service.get_process_status(
                str(params.get("job_id", "")), str(params.get("task_id", ""))
            ),
            "cancel_process_task": lambda: self.service.cancel_process_task(
                str(params.get("job_id", "")), str(params.get("task_id", ""))
            ),
            "pause_process_task": lambda: self.service.pause_process_task(
                str(params.get("job_id", "")), str(params.get("task_id", ""))
            ),
            "unpause_process_task": lambda: self.service.resume_paused_process_task(
                str(params.get("job_id", "")), str(params.get("task_id", ""))
            ),
            "resume_process_task": lambda: self.service.resume_process_task(
                str(params.get("job_id", "")), str(params.get("task_id", ""))
            ),
            "list_candidate_actions": lambda: self.service.list_candidate_actions(
                str(params.get("job_id", "")), str(params.get("candidate_id", ""))
            ),
            "activate_candidate_action": lambda: self.service.activate_candidate_action(
                str(params.get("job_id", "")), str(params.get("candidate_id", "")),
                str(params.get("action", "")),
            ),
            "export_selected_asset": lambda: self._queue_export(params),
            "get_blender_task_status": lambda: self.get_blender_task(
                str(params.get("task_id", ""))
            ),
            "cancel_blender_task": lambda: self.cancel_blender_task(
                str(params.get("task_id", ""))
            ),
            "ingest_tripo_webhook": lambda: self._ingest_tripo_webhook(params),
        }
        try:
            route = routes[method]
        except KeyError as exc:
            raise ValidationError("unsupported bridge method") from exc
        return route()

    def _submit_blender_task(self, operation: str, callback) -> dict[str, Any]:
        task = _BlenderTask(operation=operation, callback=callback)
        with self._blender_task_lock:
            self._blender_tasks[task.id] = task
            self._blender_queue.append(task.id)
        return task.public_dict()

    @staticmethod
    def _reject_bridge_account_binding(spec: dict[str, Any]) -> None:
        advanced = spec.get("advanced")
        if isinstance(advanced, dict) and any(
            isinstance(options, dict) and "account_profile" in options
            for options in advanced.values()
        ):
            raise ValidationError("account selection is available only in Blender")

    def _create_bridge_job(self, spec: dict[str, Any]) -> dict[str, Any]:
        self._reject_bridge_account_binding(spec)
        return self.service.create_job(spec)

    def _create_bridge_batch(self, specs: list[Any], *, generate: bool) -> dict[str, Any]:
        for spec in specs:
            if isinstance(spec, dict):
                self._reject_bridge_account_binding(spec)
        return self.service.create_batch(specs, generate=generate)

    def _queue_export(self, params: dict[str, Any]) -> dict[str, Any]:
        job_id = str(params.get("job_id", ""))
        export_format = str(params.get("format", "glb"))
        options = dict(params.get("options", {}))
        self.service.validate_export_request(job_id, export_format)
        return self._submit_blender_task(
            "export_selected_asset",
            lambda: self.service.export_selected_asset(job_id, export_format, options),
        )

    def get_blender_task(self, task_id: str) -> dict[str, Any]:
        with self._blender_task_lock:
            task = self._blender_tasks.get(task_id)
            if task is None:
                raise ValidationError("Blender operation task does not exist")
            return task.public_dict()

    def cancel_blender_task(self, task_id: str) -> dict[str, Any]:
        with self._blender_task_lock:
            task = self._blender_tasks.get(task_id)
            if task is None:
                raise ValidationError("Blender operation task does not exist")
            if task.state != "queued":
                raise InvalidTransitionError("only a queued Blender operation can be cancelled safely")
            task.state = "cancelled"
            task.updated_at = utc_now()
            try:
                self._blender_queue.remove(task.id)
            except ValueError:
                pass
            return task.public_dict()

    def drain_blender_tasks(self, limit: int = 1) -> int:
        processed = 0
        while processed < limit:
            with self._blender_task_lock:
                if not self._blender_queue:
                    break
                task = self._blender_tasks[self._blender_queue.popleft()]
                if task.state != "queued":
                    continue
                task.state = "running"
                task.updated_at = utc_now()
            try:
                result = task.callback()
                with self._blender_task_lock:
                    task.result = result
                    task.state = "completed"
                    task.updated_at = utc_now()
            except Exception as exc:
                with self._blender_task_lock:
                    task.error = (
                        exc.public_error() if isinstance(exc, PipelineError)
                        else {"code": "blender_operation_failed", "message": "Blender operation failed"}
                    )
                    task.state = "failed"
                    task.updated_at = utc_now()
            processed += 1
        return processed

    def request_auto_import(self, job_id: str) -> None:
        self._auto_import_errors.pop(job_id, None)
        self._auto_import_jobs.add(job_id)

    def auto_import_status(self, job_id: str) -> str:
        if job_id in self._auto_import_errors:
            return "failed"
        if job_id in self._auto_import_jobs:
            return "pending"
        return "idle"

    def import_all_models(self, job_id: str) -> dict[str, Any]:
        import bpy

        job = self.service._get_job(job_id)
        imported_ids: list[str] = []
        for candidate in job.candidates:
            collection_exists = bool(
                candidate.imported_collection
                and bpy.data.collections.get(candidate.imported_collection) is not None
            )
            if not collection_exists:
                self.service.import_candidate(job_id, candidate.id)
            imported_ids.append(candidate.id)
        layout = self._arrange_candidate_collections(job, imported_ids)
        return {"job_id": job_id, "model_count": len(imported_ids), "layout": layout}

    def drain_auto_imports(self) -> int:
        completed = 0
        for job_id in tuple(self._auto_import_jobs):
            try:
                status = self.service.get_job(job_id)
                if status["state"] in {"failed", "cancelled"}:
                    self._auto_import_jobs.discard(job_id)
                    continue
                if status["state"] not in {"candidates_ready", "candidate_imported"}:
                    continue
                self.import_all_models(job_id)
                self._auto_import_jobs.discard(job_id)
                completed += 1
            except Exception as exc:
                self._auto_import_jobs.discard(job_id)
                self._auto_import_errors[job_id] = str(exc)
        return completed

    def shutdown_blender_tasks(self) -> None:
        """Cancel work that has not entered Blender's main thread yet."""
        with self._blender_task_lock:
            queued = set(self._blender_queue)
            self._blender_queue.clear()
            for task_id in queued:
                task = self._blender_tasks.get(task_id)
                if task is not None and task.state == "queued":
                    task.state = "cancelled"
                    task.updated_at = utc_now()

    def _ingest_tripo_webhook(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = base64.b64decode(str(params.get("raw_body_base64", "")), validate=True)
        except Exception as exc:
            raise ValidationError("webhook body encoding is invalid") from exc
        verified = self.webhooks.verify(
            raw,
            timestamp=str(params.get("timestamp", "")),
            signature=str(params.get("signature", "")),
            delivery_id=str(params.get("delivery_id", "")),
        )
        if verified.get("duplicate"):
            return {"accepted": True, "duplicate": True}
        # Webhook payloads carry provider task ids, so the service matches the
        # owning regional job without exposing account metadata.
        result = self.service.ingest_provider_event("tripo_global", dict(verified["payload"]))
        result["duplicate"] = False
        return result


_runtime: BlenderPipelineRuntime | None = None


def get_runtime() -> BlenderPipelineRuntime:
    if _runtime is None:
        raise RuntimeError("asset pipeline runtime is not registered")
    return _runtime


def start_runtime() -> BlenderPipelineRuntime:
    global _runtime
    if _runtime is None:
        _runtime = BlenderPipelineRuntime()
        _runtime.bridge.start()
    return _runtime


def stop_runtime() -> None:
    global _runtime
    if _runtime is not None:
        _runtime.bridge.stop()
        _runtime.shutdown_blender_tasks()
        _runtime.service.shutdown()
        _runtime.credentials.clear()
        _runtime = None
