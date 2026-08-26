from __future__ import annotations

import threading
import uuid
import inspect
import json
import platform
import shutil
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..providers.base import ProviderAdapter
from .errors import (
    CandidateNotFoundError,
    InvalidTransitionError,
    JobCancelledError,
    JobNotFoundError,
    PipelineError,
    ProviderUnavailableError,
    ValidationError,
)
from .models import (
    AssetJob, AssetSpec, Candidate, JobState, ProcessState, ProcessTask, ProviderChoice, utc_now,
)
from .profiles import PRESET_CATALOG, get_profile, public_profiles
from .provider_ids import (
    COMPARE_PROVIDERS, GENERATION_PROVIDERS, HUNYUAN_PROVIDERS, MOCK,
    REAL_PROVIDERS, TOKENHUB_PROVIDERS, TRIPO_PROVIDERS, canonical_provider_id, option_namespace,
    provider_family,
)
from .references import ReferenceStore
from .staging import StagingStore

ImportCallback = Callable[[AssetJob, str], str]
PrepareCallback = Callable[[AssetJob, str, dict[str, Any]], dict[str, Any]]
ExportCallback = Callable[[AssetJob, str, Path, str, dict[str, Any]], dict[str, Any]]
ReviewPackCallback = Callable[[AssetJob, str, Path], dict[str, Any]]
VisibilityCallback = Callable[[AssetJob, str | None], None]
RemoveCandidateCallback = Callable[[AssetJob, str], None]
ListActionsCallback = Callable[[AssetJob, str], list[str]]
ActivateActionCallback = Callable[[AssetJob, str, str], dict[str, Any]]


class AssetPipelineService:
    def __init__(
        self,
        providers: dict[str, ProviderAdapter],
        staging: StagingStore | None = None,
        *,
        importer: ImportCallback | None = None,
        preparer: PrepareCallback | None = None,
        exporter: ExportCallback | None = None,
        review_renderer: ReviewPackCallback | None = None,
        visibility: VisibilityCallback | None = None,
        candidate_remover: RemoveCandidateCallback | None = None,
        action_lister: ListActionsCallback | None = None,
        action_activator: ActivateActionCallback | None = None,
        max_concurrent_jobs: int = 2,
        references: ReferenceStore | None = None,
        provider_concurrency: dict[str, int] | None = None,
    ) -> None:
        self.providers = providers
        self.staging = staging or StagingStore()
        self.importer = importer
        self.preparer = preparer
        self.exporter = exporter
        self.review_renderer = review_renderer
        self.visibility = visibility
        self.candidate_remover = candidate_remover
        self.action_lister = action_lister
        self.action_activator = action_activator
        self._jobs: dict[str, AssetJob] = {}
        self._lock = threading.RLock()
        self._shutting_down = False
        self._workers: dict[str, threading.Thread] = {}
        self._cancellations: dict[str, threading.Event] = {}
        self._process_workers: dict[str, threading.Thread] = {}
        self._process_cancellations: dict[str, threading.Event] = {}
        self._generation_queue: list[str] = []
        self._queue_condition = threading.Condition(self._lock)
        # Generation and post-processing have separate capacity so one workload cannot
        # starve the other. Provider-specific semaphores still enforce upstream limits.
        self._generation_slots = threading.BoundedSemaphore(max(1, int(max_concurrent_jobs)))
        self._process_slots = threading.BoundedSemaphore(max(1, int(max_concurrent_jobs) // 2))
        limits = {
            provider_id: (
                1 if provider_family(provider_id) == "compare" else 2 if provider_id == MOCK else 3
            )
            for provider_id in providers
        }
        limits.update(dict(provider_concurrency or {}))
        self._provider_limits = dict(limits)
        self._provider_slots = {
            provider_id: threading.BoundedSemaphore(max(1, int(limit)))
            for provider_id, limit in limits.items()
        }
        self.references = references or ReferenceStore(self.staging.root)
        for restored in self.staging.load_jobs():
            restored.paused = False
            if not restored.active_provider and restored.provider_task_ids:
                if restored.spec.provider != ProviderChoice.AUTO:
                    restored.active_provider = restored.spec.provider.value
                else:
                    has_tripo = any(key.startswith("tripo_") for key in restored.provider_task_ids)
                    has_old_hunyuan = any(key.startswith("hunyuan_") for key in restored.provider_task_ids)
                    restored.active_provider = (
                        "compare_legacy" if has_tripo and has_old_hunyuan
                        else "tripo_global" if has_tripo
                        else "tokenhub_cn" if has_old_hunyuan
                        else None
                    )
            if restored.state in {JobState.QUEUED, JobState.SUBMITTED, JobState.PROCESSING}:
                restored.state = JobState.RECOVERY_PENDING
                restored.error = None
                restored.events.append(
                    self._event("recovery_pending", {"reason": "previous Blender session ended"})
                )
                self.staging.save_job(restored)
            for task in restored.process_tasks:
                task.paused = False
                if task.state in {ProcessState.QUEUED, ProcessState.PROCESSING}:
                    task.state = ProcessState.RECOVERY_PENDING
                    task.updated_at = utc_now()
                    task.error = {"code": "recovery_pending", "message": "Previous Blender session ended"}
                    self.staging.save_job(restored)
            self._jobs[restored.id] = restored

    def capabilities(self) -> dict[str, Any]:
        return {
            "pipeline_version": "0.9.6",
            "preset_catalog": PRESET_CATALOG,
            "generation_presets": public_profiles(),
            "input_modes": ["text", "image", "multiview"],
            "providers": [
                adapter.capabilities().to_dict()
                for provider_id, adapter in self.providers.items()
                if provider_id != "compare_legacy"
            ],
            "workflow": [
                "register_references", "create", "queue", "generate", "preview", "import",
                "compare", "render_review_pack", "provider_process", "rig", "animate",
                "normalize", "select", "export",
            ],
            "human_approval_required_for": ["select_candidate", "export_selected_asset"],
            "mcp_forbidden_capabilities": [
                "read_provider_credentials",
                "set_provider_credentials",
                "execute_python",
                "read_arbitrary_file",
                "select_candidate",
            ],
            "cost_estimates_are_advisory": True,
            "provider_concurrency": dict(self._provider_limits),
        }

    def provider_status(self, provider: str | None = None) -> dict[str, Any]:
        if provider:
            adapter = self.providers.get(str(canonical_provider_id(provider)))
            if adapter is None:
                raise ProviderUnavailableError("unknown provider")
            return adapter.status().to_dict()
        return {
            "providers": [
                item.status().to_dict()
                for provider_id, item in self.providers.items()
                if provider_id != "compare_legacy"
            ]
        }

    def create_support_bundle(self) -> dict[str, Any]:
        """Write a user-triggered, secret/path/prompt-free diagnostic snapshot."""
        diagnostic_keys = {
            "provider", "operation", "model", "http_status", "attempts",
            "transport", "request_id", "provider_code",
        }

        def safe_error(error: Any) -> dict[str, Any] | None:
            if not isinstance(error, dict):
                return None
            result: dict[str, Any] = {}
            code = error.get("code")
            if isinstance(code, (str, int)):
                result["code"] = str(code)[:64]
            diagnostics = error.get("diagnostics")
            if isinstance(diagnostics, dict):
                safe_diagnostics = {
                    str(key): str(value)[:128]
                    for key, value in diagnostics.items()
                    if key in diagnostic_keys and isinstance(value, (str, int, float, bool))
                }
                if safe_diagnostics:
                    result["diagnostics"] = safe_diagnostics
            return result or None

        with self._lock:
            jobs = [
                {
                    "id": job.id,
                    "state": job.state.value,
                    "active_provider": job.active_provider,
                    "candidate_count": len(job.candidates),
                    "process_states": [task.state.value for task in job.process_tasks],
                    "error": safe_error(job.error),
                    "created_at": job.created_at,
                    "updated_at": job.updated_at,
                }
                for job in self._jobs.values()
            ]
        value = {
            "schema": 1,
            "pipeline_version": "0.9.6",
            "maintainer": "Shalom Wang",
            "platform": {"system": platform.system(), "release": platform.release(), "python": platform.python_version()},
            "providers": self.provider_status()["providers"],
            "jobs": jobs,
        }
        directory = self.staging.root / "support"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"support_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        temporary = destination.with_suffix(".json.part")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
        return {"filename": destination.name, "bytes": destination.stat().st_size}

    def ingest_provider_event(self, provider: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Journal a verified webhook hint without trusting it as a local artifact."""
        if provider_family(provider) != "tripo":
            raise ValidationError("unsupported webhook provider")
        event_type = str(payload.get("type", payload.get("event", "")))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if event_type not in {"task.completed", "task.failed", "balance.low"}:
            raise ValidationError("unsupported provider event")
        matched = 0
        with self._lock:
            for job in self._jobs.values():
                if task_id and str(task_id) not in job.provider_task_ids.values():
                    continue
                if event_type == "balance.low" and provider_family(job.active_provider or "") not in {"tripo", "compare"}:
                    continue
                job.events.append(
                    self._event("provider_webhook", {"provider": provider, "event": event_type})
                )
                if event_type == "task.completed" and job.state == JobState.PROCESSING:
                    job.progress = max(job.progress, 0.95)
                self.staging.save_job(job)
                matched += 1
        return {"accepted": True, "matched_jobs": matched}

    def generation_constraints(
        self, provider: str, advanced: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            choice = ProviderChoice(canonical_provider_id(provider))
        except ValueError as exc:
            raise ValidationError("unsupported provider") from exc
        if choice == ProviderChoice.AUTO:
            adapter = self._select_provider(choice)
        else:
            adapter = self.providers.get(choice.value)
            if adapter is None:
                raise ProviderUnavailableError("provider adapter is unavailable")
        constraints = adapter.resolve_input_constraints(dict(advanced or {}))
        return {
            "requested_provider": provider,
            "resolved_provider": adapter.id,
            "modes": list(constraints),
            "constraints": {
                mode: constraint.to_dict() for mode, constraint in constraints.items()
            },
        }

    def available_process_operations(
        self, job_id: str, candidate_id: str, provider: str = "auto"
    ) -> dict[str, Any]:
        with self._lock:
            job = self._get_job(job_id)
            candidate = self._get_candidate(job, candidate_id)
            requested = str(canonical_provider_id(provider.strip().lower()))
            site_providers = self._regional_providers(candidate.provider)
            provider_ids = (
                [candidate.provider, *site_providers]
                if requested == "auto" else [requested]
            )
            operations: dict[str, dict[str, Any]] = {}
            resolved: list[str] = []
            for provider_id in provider_ids:
                if provider_id in resolved:
                    continue
                adapter = self.providers.get(provider_id)
                if adapter is None or not adapter.status().available:
                    continue
                if provider_family(adapter.id) == "tokenhub" and (
                    candidate.provider != adapter.id
                    or not str(candidate.metadata.get("provider_model", "")).startswith("hy-3d-")
                ):
                    continue
                resolved.append(adapter.id)
                capabilities = adapter.capabilities()
                for operation in capabilities.postprocess:
                    schema = dict(capabilities.process_schema.get(operation, {}))
                    source_formats = schema.get("source_formats")
                    has_native_task = (
                        candidate.provider == adapter.id
                        and isinstance(candidate.metadata.get("provider_task_id"), str)
                    )
                    if (
                        source_formats and candidate.format.lower() not in source_formats
                        and not (provider_family(adapter.id) == "tripo" and has_native_task)
                    ):
                        continue
                    if (
                        operation == "animate"
                        and candidate.metadata.get("process_operation") != "rig"
                    ):
                        continue
                    operations.setdefault(operation, {"providers": [], **schema})
                    operations[operation]["providers"].append(adapter.id)
            return {
                "job_id": job.id, "candidate_id": candidate.id,
                "requested_provider": requested, "resolved_providers": resolved,
                "operations": operations,
            }

    def create_job(self, spec_value: dict[str, Any]) -> dict[str, Any]:
        spec = AssetSpec.from_dict(spec_value)
        get_profile(spec.asset_profile)
        self._preflight_spec(spec)
        job = AssetJob(spec=spec)
        if spec.reference_images:
            job.references = self.references.materialize(spec.reference_images, self.staging.job_dir(job.id))
        job.events.append(self._event("job_created", {"source": "bridge_or_ui"}))
        with self._lock:
            self._jobs[job.id] = job
            self.staging.save_job(job)
        return job.public_dict()

    def _preflight_spec(self, spec: AssetSpec) -> None:
        """Validate provider-dependent inputs before creating any billable/staged job."""
        if spec.provider == ProviderChoice.AUTO:
            adapter = self._select_provider(spec.provider)
        else:
            adapter = self.providers.get(spec.provider.value)
            if adapter is None:
                raise ProviderUnavailableError("provider adapter is unavailable")
        adapter.validate_advanced(spec)
        references = [
            replace(self.references.get(reference_id), view=view)
            for view, reference_id in spec.reference_images.items()
        ]
        adapter.validate_input_references(spec, references)

    def _replacement_job(
        self, original: AssetJob, spec_value: dict[str, Any], kind: str, detail: dict[str, Any]
    ) -> AssetJob:
        """Clone materialized inputs so retry/regeneration survives reference-library cleanup."""
        spec = AssetSpec.from_dict(spec_value)
        get_profile(spec.asset_profile)
        replacement = AssetJob(spec=spec, parent_job_id=original.id)
        if original.references:
            destination_dir = self.staging.job_dir(replacement.id) / "references"
            destination_dir.mkdir(parents=True, exist_ok=True)
            for reference in original.references:
                source = Path(reference.local_path)
                if not source.is_file():
                    raise ValidationError("the original job reference file is unavailable")
                destination = destination_dir / f"{reference.view}.{reference.format}"
                shutil.copyfile(source, destination)
                replacement.references.append(
                    replace(reference, local_path=str(destination.resolve()))
                )
        if spec.provider == ProviderChoice.AUTO:
            adapter = self._select_provider(spec.provider)
        else:
            adapter = self.providers.get(spec.provider.value)
            if adapter is None:
                raise ProviderUnavailableError("provider adapter is unavailable")
        adapter.validate_advanced(spec)
        adapter.validate_input_references(spec, replacement.references)
        replacement.events.append(self._event(kind, detail))
        with self._lock:
            self._jobs[replacement.id] = replacement
            self.staging.save_job(replacement)
        return replacement

    def create_batch(self, specs: list[dict[str, Any]], *, generate: bool = False) -> dict[str, Any]:
        if not 1 <= len(specs) <= 20:
            raise ValidationError("a batch must contain 1-20 asset specifications")
        parsed = [AssetSpec.from_dict(dict(spec)) for spec in specs]
        for spec in parsed:
            get_profile(spec.asset_profile)
            for reference_id in spec.reference_images.values():
                self.references.get(reference_id)
        jobs = [self.create_job(spec.to_dict()) for spec in parsed]
        if generate:
            jobs = [self.generate_candidates(job["id"]) for job in jobs]
        return {"count": len(jobs), "jobs": jobs}

    def purge_archived_jobs(self, older_than_days: int = 30) -> dict[str, Any]:
        days = max(0, min(int(older_than_days), 3650))
        cutoff = datetime.now(UTC) - timedelta(days=days)
        removed: list[str] = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if not job.archived:
                    continue
                try:
                    updated = datetime.fromisoformat(job.updated_at)
                except ValueError:
                    continue
                if updated <= cutoff:
                    self.staging.delete_job(job_id)
                    self._jobs.pop(job_id, None)
                    removed.append(job_id)
        return {"removed_count": len(removed), "removed_job_ids": removed}

    def register_reference_image(self, source: Path, view: str) -> dict[str, object]:
        return self.references.register(source, view)

    def list_reference_images(self) -> dict[str, object]:
        return {"references": self.references.list()}

    def remove_reference_image(self, reference_id: str) -> None:
        self.references.remove(reference_id)

    def generate_candidates(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if self._shutting_down:
                raise ProviderUnavailableError("pipeline runtime is shutting down")
            job = self._get_job(job_id)
            adapter = self._select_provider(job.spec.provider)
            adapter.validate_advanced(job.spec)
            adapter.validate_input_references(job.spec, job.references)
            estimate = adapter.estimate_generation_credits(job.spec)
            if estimate is not None:
                job.usage["estimated_credits"] = float(estimate)
                limit = job.spec.max_estimated_credits
                if limit > 0 and estimate > limit and not job.spec.allow_over_budget:
                    self.staging.save_job(job)
                    raise ValidationError(
                        f"estimated cost {estimate:g} credits exceeds the job limit {limit:g}"
                    )
            if job.state != JobState.DRAFT:
                raise InvalidTransitionError("only a draft job can be submitted for generation")
            job.active_provider = adapter.id
            job.transition(JobState.QUEUED, kind="generation_queued", detail={"provider": adapter.id})
            job.progress = 0.01
            self.staging.save_job(job)
            cancellation = threading.Event()
            self._cancellations[job.id] = cancellation
            self._generation_queue.append(job.id)
            worker = threading.Thread(
                target=self._generation_worker,
                args=(job, adapter, cancellation),
                name=f"ai3d-generation-{job.id[:8]}", daemon=True,
            )
            self._workers[job.id] = worker
            worker.start()
        # Give an immediately available slot a chance to leave QUEUED before returning.
        # This preserves responsive asynchronous provider work while avoiding a transient
        # queued-only result for fast local providers.
        worker.join(timeout=0.05)
        with self._lock:
            return job.public_dict()

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_job(job_id)
            if job.state not in {JobState.DRAFT, JobState.QUEUED, JobState.SUBMITTED, JobState.PROCESSING, JobState.RECOVERY_PENDING}:
                raise InvalidTransitionError(f"cannot cancel a {job.state.value} job")
            cancellation = self._cancellations.get(job.id)
            if cancellation:
                cancellation.set()
            if job.id in self._generation_queue:
                self._generation_queue.remove(job.id)
            self._queue_condition.notify_all()
            job.transition(JobState.CANCELLED, kind="job_cancelled", detail={"scope": "local"})
            job.error = {"code": JobCancelledError.code, "message": "Job cancelled locally"}
            self.staging.save_job(job)
            return job.public_dict()

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self._get_job(job_id).public_dict()

    def list_jobs(self, *, include_archived: bool = False, limit: int = 50) -> dict[str, Any]:
        with self._lock:
            jobs = [job for job in self._jobs.values() if include_archived or not job.archived]
            jobs.sort(key=lambda item: item.updated_at, reverse=True)
            return {"jobs": [job.public_dict() for job in jobs[:max(1, min(int(limit), 200))]]}

    def retry_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            original = self._get_job(job_id)
            spec = original.spec.to_dict()
        retry = self._replacement_job(
            original, spec, "job_retried", {"parent_job_id": original.id}
        )
        return self.generate_candidates(retry.id)

    def regenerate_candidate(self, job_id: str, candidate_id: str) -> dict[str, Any]:
        with self._lock:
            original = self._get_job(job_id)
            candidate = self._get_candidate(original, candidate_id)
            spec = original.spec.to_dict()
            spec["candidate_count"] = 1
            if candidate.provider in {MOCK, *REAL_PROVIDERS}:
                spec["provider"] = candidate.provider
                namespace = option_namespace(candidate.provider)
                if namespace in {"tripo", "hunyuan_direct", "tokenhub"}:
                    spec["advanced"] = {
                        namespace: original.spec.advanced.get(namespace, {})
                    }
        replacement = self._replacement_job(
            original, spec, "candidate_regeneration",
            {"parent_job_id": original.id, "candidate_id": candidate.id},
        )
        return self.generate_candidates(replacement.id)

    def set_job_priority(self, job_id: str, priority: int) -> dict[str, Any]:
        value = int(priority)
        if not 0 <= value <= 100:
            raise ValidationError("priority must be between 0 and 100")
        with self._queue_condition:
            job = self._get_job(job_id)
            if job.state not in {JobState.DRAFT, JobState.QUEUED}:
                raise InvalidTransitionError("priority can change only before provider submission")
            job.spec = replace(job.spec, priority=value)
            job.events.append(self._event("priority_changed", {"priority": value}))
            self.staging.save_job(job)
            self._queue_condition.notify_all()
            return job.public_dict()

    def pause_job(self, job_id: str) -> dict[str, Any]:
        with self._queue_condition:
            job = self._get_job(job_id)
            if job.state not in {JobState.QUEUED, JobState.SUBMITTED, JobState.PROCESSING}:
                raise InvalidTransitionError("only an active job can be paused")
            if job.paused:
                return job.public_dict()
            job.paused = True
            job.events.append(self._event("job_paused", {"scope": "local_orchestration"}))
            self.staging.save_job(job)
            return job.public_dict()

    def resume_paused_job(self, job_id: str) -> dict[str, Any]:
        with self._queue_condition:
            job = self._get_job(job_id)
            if not job.paused:
                return job.public_dict()
            job.paused = False
            job.events.append(self._event("job_unpaused", {}))
            self.staging.save_job(job)
            self._queue_condition.notify_all()
            return job.public_dict()

    def archive_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_job(job_id)
            if job.state in {JobState.QUEUED, JobState.SUBMITTED, JobState.PROCESSING}:
                raise InvalidTransitionError("cancel an active job before archiving it")
            job.archived = True
            job.events.append(self._event("job_archived", {}))
            self.staging.save_job(job)
            return job.public_dict()

    def resume_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_job(job_id)
            if job.state != JobState.RECOVERY_PENDING or not job.active_provider:
                raise InvalidTransitionError("job has no provider task pending recovery")
            adapter = self.providers.get(job.active_provider)
            if adapter is None or not adapter.status().available:
                raise ProviderUnavailableError("the original provider is not available")
            cancellation = threading.Event()
            self._cancellations[job.id] = cancellation
            self._generation_queue.append(job.id)
            worker = threading.Thread(
                target=self._generation_worker,
                args=(job, adapter, cancellation),
                kwargs={"resume": True},
                name=f"ai3d-recovery-{job.id[:8]}",
                daemon=True,
            )
            self._workers[job.id] = worker
            worker.start()
            return job.public_dict()

    def import_candidate(self, job_id: str, candidate_id: str) -> dict[str, Any]:
        if self.importer is None:
            raise ProviderUnavailableError("candidate import requires the Blender runtime")
        with self._lock:
            job = self._get_job(job_id)
            candidate = self._get_candidate(job, candidate_id)
        collection_name = self.importer(job, candidate_id)
        with self._lock:
            candidate.imported_collection = collection_name
            if job.state == JobState.CANDIDATES_READY:
                job.transition(
                    JobState.CANDIDATE_IMPORTED,
                    kind="candidate_imported",
                    detail={"candidate_id": candidate_id, "collection": collection_name},
                )
            self.staging.save_job(job)
            return job.public_dict()

    def prepare_game_asset(
        self, job_id: str, candidate_id: str, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self.preparer is None:
            raise ProviderUnavailableError("candidate normalization requires the Blender runtime")
        with self._lock:
            job = self._get_job(job_id)
            self._get_candidate(job, candidate_id)
            if job.state not in {JobState.CANDIDATE_IMPORTED, JobState.APPROVED}:
                raise InvalidTransitionError("import the candidate before normalization")
        result = self.preparer(job, candidate_id, dict(options or {}))
        with self._lock:
            derived_collection = result.get("derived_collection")
            if isinstance(derived_collection, str):
                source = self._get_candidate(job, candidate_id)
                derived_id = f"normalized_{uuid.uuid4().hex[:16]}"
                job.candidates.append(
                    Candidate(
                        id=derived_id,
                        provider="blender",
                        label=f"{source.label} · normalized",
                        local_model_path=source.local_model_path,
                        format=source.format,
                        metadata={"parent_candidate_id": source.id, "normalization": dict(options or {})},
                        imported_collection=derived_collection,
                    )
                )
                result["result_candidate_id"] = derived_id
            job.events.append(self._event("candidate_normalized", {"candidate_id": candidate_id}))
            self.staging.save_job(job)
        return result

    def normalize_candidate(
        self, job_id: str, candidate_id: str, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.prepare_game_asset(job_id, candidate_id, options)

    def list_candidate_actions(self, job_id: str, candidate_id: str) -> dict[str, Any]:
        if self.action_lister is None:
            raise ProviderUnavailableError("animation inspection requires the Blender runtime")
        with self._lock:
            job = self._get_job(job_id)
            candidate = self._get_candidate(job, candidate_id)
            if not candidate.imported_collection:
                raise InvalidTransitionError("import the character candidate first")
        return {"job_id": job_id, "candidate_id": candidate_id, "actions": self.action_lister(job, candidate_id)}

    def activate_candidate_action(
        self, job_id: str, candidate_id: str, action_name: str
    ) -> dict[str, Any]:
        if self.action_activator is None:
            raise ProviderUnavailableError("animation preview requires the Blender runtime")
        with self._lock:
            job = self._get_job(job_id)
            candidate = self._get_candidate(job, candidate_id)
            if not candidate.imported_collection:
                raise InvalidTransitionError("import the character candidate first")
        return self.action_activator(job, candidate_id, action_name)

    def select_candidate(self, job_id: str, candidate_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_job(job_id)
            candidate = self._get_candidate(job, candidate_id)
            if not candidate.imported_collection:
                raise InvalidTransitionError("import the candidate before selecting it")
            job.selected_candidate_id = candidate_id
            for item in job.candidates:
                if item.review_status == "selected":
                    item.review_status = "pending"
            candidate.review_status = "selected"
            if job.state not in {JobState.APPROVED, JobState.EXPORTED}:
                if job.state != JobState.CANDIDATE_IMPORTED:
                    raise InvalidTransitionError("candidate is not ready for selection")
                job.transition(JobState.APPROVED, kind="candidate_selected", detail={"candidate_id": candidate_id})
            else:
                if job.state == JobState.EXPORTED:
                    job.state = JobState.APPROVED
                    job.updated_at = utc_now()
                job.events.append(self._event("candidate_reselected", {"candidate_id": candidate_id}))
            self.staging.save_job(job)
            return job.public_dict()

    def reject_candidate(self, job_id: str, candidate_id: str, note: str = "") -> dict[str, Any]:
        with self._lock:
            job = self._get_job(job_id)
            candidate = self._get_candidate(job, candidate_id)
            if job.selected_candidate_id == candidate_id:
                raise InvalidTransitionError("the selected candidate cannot be rejected")
            candidate.review_status = "rejected"
            candidate.review_note = note.strip()[:500]
            job.events.append(self._event("candidate_rejected", {"candidate_id": candidate_id}))
            self.staging.save_job(job)
            return job.public_dict()

    def set_candidate_visibility(self, job_id: str, candidate_id: str | None) -> dict[str, Any]:
        if self.visibility is None:
            raise ProviderUnavailableError("candidate visibility requires the Blender runtime")
        with self._lock:
            job = self._get_job(job_id)
            if candidate_id is not None:
                self._get_candidate(job, candidate_id)
        self.visibility(job, candidate_id)
        return {"job_id": job_id, "visible_candidate_id": candidate_id}

    def render_review_pack(self, job_id: str, candidate_id: str) -> dict[str, Any]:
        if self.review_renderer is None:
            raise ProviderUnavailableError("review rendering requires the Blender runtime")
        with self._lock:
            job = self._get_job(job_id)
            candidate = self._get_candidate(job, candidate_id)
            if not candidate.imported_collection:
                raise InvalidTransitionError("import the candidate before rendering a review pack")
            destination = self.staging.job_dir(job.id) / "reviews" / candidate_id
        artifact = self.review_renderer(job, candidate_id, destination)
        with self._lock:
            job.artifacts.append(artifact)
            job.events.append(self._event("review_pack_rendered", {"candidate_id": candidate_id}))
            self.staging.save_job(job)
        return {"job": job.public_dict(), "artifact": artifact}

    def delete_candidate(self, job_id: str, candidate_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_job(job_id)
            candidate = self._get_candidate(job, candidate_id)
            if job.selected_candidate_id == candidate_id:
                raise InvalidTransitionError("the selected candidate cannot be deleted")
        if self.candidate_remover and candidate.imported_collection:
            self.candidate_remover(job, candidate_id)
        with self._lock:
            model_path = Path(candidate.local_model_path)
            shared_model = any(
                item.id != candidate_id and Path(item.local_model_path) == model_path
                for item in job.candidates
            )
            if not shared_model:
                model_path.unlink(missing_ok=True)
            preview_path = self.candidate_preview_path(job_id, candidate_id)
            if preview_path is not None:
                preview_path.unlink(missing_ok=True)
            job.candidates = [item for item in job.candidates if item.id != candidate_id]
            job.events.append(self._event("candidate_deleted", {"candidate_id": candidate_id}))
            self.staging.save_job(job)
            return job.public_dict()

    def submit_candidate_process(
        self,
        job_id: str,
        candidate_id: str,
        operation: str,
        provider: str = "auto",
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "retopology", "uv", "texture", "segment", "convert",
            "rig_check", "rig", "animate",
        }
        if operation not in allowed:
            raise ValidationError("unsupported candidate process operation")
        with self._lock:
            if self._shutting_down:
                raise ProviderUnavailableError("pipeline runtime is shutting down")
            job = self._get_job(job_id)
            candidate = self._get_candidate(job, candidate_id)
            adapter = self._select_process_provider(provider, candidate, operation)
            process_params = dict(params or {})
            adapter.validate_process(job, candidate, operation, process_params)
            estimate = adapter.estimate_process_credits(operation, process_params)
            current_estimate = float(job.usage.get("estimated_credits", 0.0))
            if estimate is not None:
                projected = current_estimate + float(estimate)
                limit = job.spec.max_estimated_credits
                if limit > 0 and projected > limit and not job.spec.allow_over_budget:
                    raise ValidationError(
                        f"projected cost {projected:g} credits exceeds the job limit {limit:g}"
                    )
                job.usage["estimated_credits"] = projected
            task = ProcessTask(
                operation=operation, provider=adapter.id, source_candidate_id=candidate_id,
                params=process_params,
            )
            job.process_tasks.append(task)
            job.events.append(self._event("candidate_process_queued", {"task_id": task.id, "operation": operation}))
            self.staging.save_job(job)
            cancellation = threading.Event()
            self._process_cancellations[task.id] = cancellation
            worker = threading.Thread(
                target=self._process_worker,
                args=(job, candidate, task, adapter, cancellation),
                name=f"ai3d-process-{task.id[:8]}", daemon=True,
            )
            self._process_workers[task.id] = worker
            worker.start()
            return task.public_dict()

    def get_process_status(self, job_id: str, task_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_job(job_id)
            return self._get_process_task(job, task_id).public_dict()

    def cancel_process_task(self, job_id: str, task_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_job(job_id)
            task = self._get_process_task(job, task_id)
            if task.state not in {
                ProcessState.QUEUED, ProcessState.PROCESSING, ProcessState.RECOVERY_PENDING,
            }:
                raise InvalidTransitionError("process task is not active")
            cancellation = self._process_cancellations.get(task.id)
            if cancellation:
                cancellation.set()
            task.state = ProcessState.CANCELLED
            task.updated_at = utc_now()
            task.error = {"code": "cancelled", "message": "Process cancelled locally"}
            self.staging.save_job(job)
            return task.public_dict()

    def pause_process_task(self, job_id: str, task_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_job(job_id)
            task = self._get_process_task(job, task_id)
            if task.state not in {ProcessState.QUEUED, ProcessState.PROCESSING}:
                raise InvalidTransitionError("only an active process task can be paused")
            if task.paused:
                return task.public_dict()
            task.paused = True
            task.updated_at = utc_now()
            self.staging.save_job(job)
            return task.public_dict()

    def resume_paused_process_task(self, job_id: str, task_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_job(job_id)
            task = self._get_process_task(job, task_id)
            if not task.paused:
                return task.public_dict()
            task.paused = False
            task.updated_at = utc_now()
            self.staging.save_job(job)
            return task.public_dict()

    def resume_process_task(self, job_id: str, task_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._get_job(job_id)
            task = self._get_process_task(job, task_id)
            if task.state != ProcessState.RECOVERY_PENDING or not task.provider_task_id:
                raise InvalidTransitionError("process task has no recoverable provider submission")
            candidate = self._get_candidate(job, task.source_candidate_id)
            adapter = self.providers.get(task.provider)
            if adapter is None or not adapter.status().available:
                raise ProviderUnavailableError("the process provider is not available")
            task.error = None
            cancellation = threading.Event()
            self._process_cancellations[task.id] = cancellation
            worker = threading.Thread(
                target=self._process_worker,
                args=(job, candidate, task, adapter, cancellation),
                kwargs={"resume": True},
                name=f"ai3d-process-recovery-{task.id[:8]}", daemon=True,
            )
            self._process_workers[task.id] = worker
            worker.start()
            return task.public_dict()

    def _select_process_provider(
        self, requested: str, candidate: Candidate, operation: str
    ) -> ProviderAdapter:
        requested = str(canonical_provider_id(requested))
        source_provider = str(canonical_provider_id(candidate.provider))
        regional = self._regional_providers(source_provider)
        choices = [source_provider, *regional] if requested == "auto" else [requested]
        for provider_id in choices:
            adapter = self.providers.get(provider_id)
            if provider_family(provider_id) == "tokenhub" and (
                source_provider != provider_id
                or not str(candidate.metadata.get("provider_model", "")).startswith("hy-3d-")
            ):
                continue
            if (
                adapter and adapter.status().available
                and operation in adapter.capabilities().postprocess
            ):
                return adapter
        raise ProviderUnavailableError(f"no configured provider supports {operation}")

    @staticmethod
    def _regional_providers(source_provider: str) -> tuple[str, ...]:
        """Prefer suppliers that can use the same legal/network account region.

        Direct Hunyuan is a mainland Tencent product. TokenHub is a separate
        aggregator with independent China/international accounts.
        """
        source = str(canonical_provider_id(source_provider))
        if source in {"tripo_cn", "hunyuan_direct", "tokenhub_cn"}:
            return ("tripo_cn", "hunyuan_direct", "tokenhub_cn")
        if source in {"tripo_global", "tokenhub_global"}:
            return ("tripo_global", "tokenhub_global")
        return REAL_PROVIDERS

    @staticmethod
    def _get_process_task(job: AssetJob, task_id: str) -> ProcessTask:
        for task in job.process_tasks:
            if task.id == task_id:
                return task
        raise JobNotFoundError("process task does not exist")

    def _process_worker(self, job, candidate, task, adapter, cancellation, *, resume: bool = False) -> None:
        acquired = False
        provider_acquired = False
        try:
            while not cancellation.is_set():
                if task.paused:
                    cancellation.wait(0.2)
                    continue
                if self._process_slots.acquire(timeout=0.2):
                    acquired = True
                    break
            if not acquired:
                raise JobCancelledError("process was cancelled while queued")
            provider_slot = self._provider_slots.get(adapter.id)
            if provider_slot is not None:
                while not cancellation.is_set():
                    if provider_slot.acquire(timeout=0.2):
                        provider_acquired = True
                        break
                if not provider_acquired:
                    raise JobCancelledError("process was cancelled while waiting for provider capacity")
            with self._lock:
                if self._shutting_down or task.state == ProcessState.CANCELLED:
                    return
                task.state = ProcessState.PROCESSING
                task.progress = 0.01
                task.updated_at = utc_now()
                self.staging.save_job(job)

            def update_progress(value: float) -> None:
                while task.paused and not cancellation.wait(0.2):
                    pass
                with self._lock:
                    if not self._shutting_down and task.state == ProcessState.PROCESSING:
                        task.progress = min(max(float(value), 0.01), 0.99)
                        task.updated_at = utc_now()
                        self.staging.save_job(job)

            def task_submitted(provider_task_id: str) -> None:
                with self._lock:
                    task.provider_task_id = provider_task_id
                    if self._shutting_down:
                        task.state = ProcessState.RECOVERY_PENDING
                        task.error = None
                    task.updated_at = utc_now()
                    self.staging.save_job(job)

            destination = self.staging.job_dir(job.id) / "processed" / task.id
            if resume:
                result = adapter.resume_process(
                    job, candidate, task.operation, task.params, str(task.provider_task_id),
                    destination, cancel_event=cancellation, progress=update_progress,
                )
            else:
                result = adapter.process(
                    job, candidate, task.operation, task.params, destination,
                    cancel_event=cancellation, progress=update_progress,
                    task_submitted=task_submitted,
                )
            with self._lock:
                if self._shutting_down or task.state == ProcessState.CANCELLED:
                    return
                artifact_id = uuid.uuid4().hex
                artifact = {
                    "id": artifact_id,
                    "kind": task.operation,
                    "provider": adapter.id,
                    "source_candidate_id": candidate.id,
                    "parent_artifact_id": candidate.metadata.get("artifact_id"),
                    **result,
                }
                job.artifacts.append(artifact)
                result_candidate_id = None
                if isinstance(result.get("local_path"), str) and isinstance(result.get("format"), str):
                    result_candidate_id = f"derived_{artifact_id[:16]}"
                    job.candidates.append(
                        Candidate(
                            id=result_candidate_id,
                            provider=adapter.id,
                            label=f"{candidate.label} · {task.operation}",
                            local_model_path=str(result["local_path"]),
                            format=str(result["format"]),
                            metadata={
                                "artifact_id": artifact_id,
                                "parent_candidate_id": candidate.id,
                                "provider_task_id": result.get("provider_task_id"),
                                "provider_model": result.get("provider_model"),
                                "preview_file": result.get("preview_file"),
                                "character_asset": task.operation in {"rig", "animate"},
                                "contains_animation": task.operation == "animate",
                                "process_operation": task.operation,
                            },
                        )
                    )
                task.artifact_id = artifact_id
                task.result_candidate_id = result_candidate_id
                task.provider_task_id = result.get("provider_task_id", task.provider_task_id)
                task.state = ProcessState.COMPLETED
                task.progress = 1.0
                task.updated_at = utc_now()
                credits = result.get("credits_consumed")
                if isinstance(credits, (int, float)):
                    job.usage["credits_consumed"] = float(job.usage.get("credits_consumed", 0)) + float(credits)
                job.events.append(self._event("candidate_process_completed", {"task_id": task.id, "artifact_id": artifact_id}))
                self.staging.save_job(job)
        except JobCancelledError:
            with self._lock:
                if self._shutting_down:
                    return
                task.state = ProcessState.CANCELLED
                task.error = {"code": "cancelled", "message": "Process cancelled locally"}
                task.updated_at = utc_now()
                self.staging.save_job(job)
        except Exception as exc:
            with self._lock:
                if self._shutting_down:
                    return
                error_code = exc.code if isinstance(exc, PipelineError) else "provider_error"
                task.state = ProcessState.FAILED
                if isinstance(exc, PipelineError):
                    task.error = exc.public_error()
                    diagnostics = dict(task.error.get("diagnostics", {}))
                    diagnostics.update({"provider": adapter.id, "operation": task.operation})
                    task.error["diagnostics"] = diagnostics
                else:
                    task.error = {"code": error_code, "message": "Provider processing failed"}
                task.updated_at = utc_now()
                self.staging.save_job(job)
        finally:
            if provider_acquired:
                self._provider_slots[adapter.id].release()
            if acquired:
                self._process_slots.release()
            with self._lock:
                self._process_workers.pop(task.id, None)
                self._process_cancellations.pop(task.id, None)

    def approve_candidate(self, job_id: str, candidate_id: str) -> dict[str, Any]:
        return self.select_candidate(job_id, candidate_id)

    def validate_export_request(self, job_id: str, export_format: str) -> None:
        """Reject invalid exports before they enter Blender's main-thread queue."""
        if self.exporter is None:
            raise ProviderUnavailableError("asset export requires the Blender runtime")
        with self._lock:
            job = self._get_job(job_id)
            if job.state not in {JobState.APPROVED, JobState.EXPORTED} or not job.selected_candidate_id:
                raise InvalidTransitionError("select a candidate in Blender before export")
        if export_format.lower() not in {"glb", "gltf", "fbx", "obj", "stl", "usd"}:
            raise ValidationError("unsupported Blender export format")

    def export_selected_asset(
        self,
        job_id: str,
        export_format: str = "glb",
        options: dict[str, Any] | None = None,
        destination_root: Path | None = None,
    ) -> dict[str, Any]:
        self.validate_export_request(job_id, export_format)
        with self._lock:
            job = self._get_job(job_id)
            candidate_id = job.selected_candidate_id
            normalized_format = export_format.lower()
            extensions = {"glb": "glb", "gltf": "gltf", "fbx": "fbx", "obj": "obj", "stl": "stl", "usd": "usd"}
            root = destination_root.resolve() if destination_root else self.staging.job_dir(job.id) / "exports"
            destination = root / f"{job.spec.asset_name}.{extensions[normalized_format]}"
            if destination.exists():
                for index in range(1, 1000):
                    versioned = destination.with_name(f"{destination.stem}_{index:03d}{destination.suffix}")
                    if not versioned.exists():
                        destination = versioned
                        break
            export_options = dict(options or {})
            export_options["_staging_only"] = destination_root is None
        if len(inspect.signature(self.exporter).parameters) <= 3:
            artifact = self.exporter(job, candidate_id, destination)  # Backward-compatible callback.
        else:
            artifact = self.exporter(job, candidate_id, destination, normalized_format, export_options)
        with self._lock:
            job.artifacts.append(artifact)
            if job.state == JobState.APPROVED:
                job.transition(
                    JobState.EXPORTED, kind="selected_asset_exported",
                    detail={"artifact": artifact["filename"]},
                )
            else:
                job.updated_at = utc_now()
                job.events.append(
                    self._event("selected_asset_exported", {"artifact": artifact["filename"]})
                )
            self.staging.save_job(job)
            return {"job": job.public_dict(), "artifact": artifact}

    def export_reviewed_asset(self, job_id: str) -> dict[str, Any]:
        return self.export_selected_asset(job_id)

    def _select_provider(self, choice: ProviderChoice) -> ProviderAdapter:
        if choice == ProviderChoice.AUTO:
            for provider_id in (*REAL_PROVIDERS, MOCK):
                adapter = self.providers.get(provider_id)
                if adapter and adapter.status().available:
                    return adapter
        else:
            adapter = self.providers.get(choice.value)
            if adapter and adapter.status().available:
                return adapter
        raise ProviderUnavailableError(f"provider {choice.value} is not available")

    def _get_job(self, job_id: str) -> AssetJob:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise JobNotFoundError("job does not exist in this Blender session") from exc

    @staticmethod
    def _get_candidate(job: AssetJob, candidate_id: str):
        for candidate in job.candidates:
            if candidate.id == candidate_id:
                return candidate
        raise CandidateNotFoundError("candidate does not exist")

    @staticmethod
    def _event(kind: str, detail: dict[str, Any]):
        from .models import JobEvent, utc_now

        return JobEvent(at=utc_now(), kind=kind, detail=detail)

    def candidate_local_path(self, job_id: str, candidate_id: str) -> Path:
        with self._lock:
            candidate = self._get_candidate(self._get_job(job_id), candidate_id)
            return Path(candidate.local_model_path)

    def candidate_preview_path(self, job_id: str, candidate_id: str) -> Path | None:
        with self._lock:
            candidate = self._get_candidate(self._get_job(job_id), candidate_id)
            filename = candidate.metadata.get("preview_file")
            if not isinstance(filename, str) or Path(filename).name != filename:
                return None
            path = Path(candidate.local_model_path).parent / filename
            return path if path.is_file() else None

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._shutting_down = True
            for cancellation in self._cancellations.values():
                cancellation.set()
            workers = list(self._workers.values())
            for cancellation in self._process_cancellations.values():
                cancellation.set()
            workers.extend(self._process_workers.values())
            for job in self._jobs.values():
                if job.state in {JobState.QUEUED, JobState.SUBMITTED, JobState.PROCESSING}:
                    if job.provider_task_ids:
                        job.state = JobState.RECOVERY_PENDING
                        job.error = None
                        reason = "Blender runtime stopped after provider submission"
                    else:
                        job.state = JobState.FAILED
                        job.error = {"code": "runtime_stopped", "message": "Runtime stopped before provider submission"}
                        reason = "Blender runtime stopped before provider submission"
                    job.updated_at = utc_now()
                    job.events.append(self._event("runtime_stopped", {"reason": reason}))
                    self.staging.save_job(job)
                for task in job.process_tasks:
                    if task.state in {ProcessState.QUEUED, ProcessState.PROCESSING}:
                        task.state = (
                            ProcessState.RECOVERY_PENDING if task.provider_task_id
                            else ProcessState.FAILED
                        )
                        task.error = None if task.provider_task_id else {
                            "code": "runtime_stopped", "message": "Runtime stopped before provider submission"
                        }
                        task.updated_at = utc_now()
                        self.staging.save_job(job)
        deadline = time.monotonic() + max(0.0, timeout)
        for worker in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)

    def _generation_worker(self, job: AssetJob, adapter: ProviderAdapter, cancellation: threading.Event, *, resume: bool = False) -> None:
        def update_progress(value: float) -> None:
            while job.paused and not cancellation.wait(0.2):
                pass
            with self._lock:
                if not self._shutting_down and job.state == JobState.PROCESSING:
                    job.progress = min(max(0.1 + float(value) * 0.89, 0.1), 0.99)
                    self.staging.save_job(job)
        def task_submitted(key: str, provider_task_id: str) -> None:
            """Durably record each remote id before the adapter can continue."""
            with self._lock:
                if not self._shutting_down and job.state not in {JobState.SUBMITTED, JobState.PROCESSING}:
                    return
                job.provider_task_ids[str(key)] = str(provider_task_id)
                if self._shutting_down:
                    job.state = JobState.RECOVERY_PENDING
                    job.error = None
                job.updated_at = utc_now()
                job.events.append(
                    self._event("provider_task_submitted", {"provider": key.partition("_")[0], "slot": key})
                )
                self.staging.save_job(job)
        acquired = False
        provider_acquired = False

        try:
            with self._queue_condition:
                while not cancellation.is_set():
                    queued = [self._jobs[item] for item in self._generation_queue if item in self._jobs]
                    queued.sort(key=lambda item: (-item.spec.priority, item.created_at))
                    if not job.paused and queued and queued[0].id == job.id:
                        self._generation_queue.remove(job.id)
                        self._queue_condition.notify_all()
                        break
                    self._queue_condition.wait(timeout=0.2)
                if cancellation.is_set():
                    raise JobCancelledError("job was cancelled while queued")
            while not cancellation.is_set():
                if job.paused:
                    cancellation.wait(0.2)
                    continue
                if self._generation_slots.acquire(timeout=0.2):
                    acquired = True
                    break
            if not acquired:
                raise JobCancelledError("job was cancelled while queued")
            provider_slot = self._provider_slots.get(adapter.id)
            if provider_slot is not None:
                while not cancellation.is_set():
                    if provider_slot.acquire(timeout=0.2):
                        provider_acquired = True
                        break
                if not provider_acquired:
                    raise JobCancelledError("job was cancelled while waiting for provider capacity")
            with self._lock:
                if job.state == JobState.CANCELLED:
                    return
                if resume:
                    job.transition(JobState.PROCESSING, kind="provider_tasks_resumed")
                else:
                    job.transition(JobState.SUBMITTED, kind="generation_submitted", detail={"provider": adapter.id})
                    job.progress = 0.05
                    job.transition(JobState.PROCESSING, kind="generation_processing")
                job.progress = 0.1
                self.staging.save_job(job)
            action = adapter.resume if resume else adapter.generate
            candidates = action(
                job,
                self.staging.job_dir(job.id),
                cancel_event=cancellation,
                progress=update_progress,
                **({"task_submitted": task_submitted} if not resume else {}),
            )
            with self._lock:
                if job.state == JobState.CANCELLED:
                    return
                job.candidates = candidates
                credits = [item.metadata.get("credits_consumed") for item in candidates]
                numeric_credits = [float(value) for value in credits if isinstance(value, (int, float))]
                if numeric_credits:
                    job.usage["credits_consumed"] = sum(numeric_credits)
                job.progress = 1.0
                job.transition(
                    JobState.CANDIDATES_READY,
                    kind="candidates_staged",
                    detail={"count": len(candidates), "provider": adapter.id},
                )
                self.staging.save_job(job)
        except JobCancelledError:
            with self._lock:
                if self._shutting_down:
                    return
                if job.state != JobState.CANCELLED:
                    job.transition(JobState.CANCELLED, kind="job_cancelled", detail={"scope": "local"})
                    job.error = {"code": JobCancelledError.code, "message": "Job cancelled locally"}
                    self.staging.save_job(job)
        except Exception as exc:
            with self._lock:
                if self._shutting_down:
                    return
                if job.state == JobState.CANCELLED:
                    return
                error_code = exc.code if isinstance(exc, PipelineError) else "provider_error"
                if isinstance(exc, PipelineError):
                    job.error = exc.public_error()
                    diagnostics = dict(job.error.get("diagnostics", {}))
                    options = job.spec.advanced.get(adapter.id, {})
                    diagnostics.update({
                        "provider": adapter.id,
                        "operation": "generate",
                        "model": str(options.get("model", "default")) if isinstance(options, dict) else "default",
                    })
                    job.error["diagnostics"] = diagnostics
                else:
                    job.error = {"code": error_code, "message": "Provider operation failed"}
                job.transition(JobState.FAILED, kind="generation_failed", detail={"code": error_code})
                self.staging.save_job(job)
        finally:
            if provider_acquired:
                self._provider_slots[adapter.id].release()
            if acquired:
                self._generation_slots.release()
            with self._lock:
                self._workers.pop(job.id, None)
                self._cancellations.pop(job.id, None)
                if job.id in self._generation_queue:
                    self._generation_queue.remove(job.id)
                self._queue_condition.notify_all()
