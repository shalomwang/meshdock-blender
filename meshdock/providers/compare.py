from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

from ..core.errors import ProviderUnavailableError, ValidationError
from ..core.models import AssetJob, AssetSpec, Candidate, ProviderChoice
from .base import InputConstraint, ProviderAdapter, ProviderCapabilities, ProviderStatus


class CompareAdapter(ProviderAdapter):
    """Runs two explicit suppliers while preserving their native option namespaces."""

    id = "compare_global"
    option_namespace = "compare"

    def __init__(
        self, first: ProviderAdapter, second: ProviderAdapter, *, provider_id: str = "compare_global"
    ) -> None:
        if provider_id not in {"compare_cn", "compare_global", "compare_legacy"}:
            raise ValueError("unsupported compare provider id")
        self.id = provider_id
        self._providers = (first, second)

    def status(self) -> ProviderStatus:
        statuses = [provider.status() for provider in self._providers]
        configured = all(item.configured for item in statuses)
        return ProviderStatus(provider=self.id, configured=configured, available=configured)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.id,
            generation_modes=("text", "image", "multiview"),
            output_formats=("glb",),
            postprocess=(),
            input_constraints=self.resolve_input_constraints({}),
            advanced_schema={
                "description": "Use each compared supplier's own advanced namespace."
            },
        )

    def resolve_input_constraints(
        self, advanced: dict | None = None
    ) -> dict[str, InputConstraint]:
        child_constraints = [
            provider.resolve_input_constraints(advanced or {}) for provider in self._providers
        ]
        shared_modes = set.intersection(*(set(item) for item in child_constraints))
        result: dict[str, InputConstraint] = {}
        for mode in shared_modes:
            constraints = [item[mode] for item in child_constraints]
            allowed = set(constraints[0].allowed_views)
            formats = set(constraints[0].formats)
            required: set[str] = set()
            for constraint in constraints:
                allowed &= set(constraint.allowed_views)
                formats &= set(constraint.formats)
                required |= set(constraint.required_views)
            result[mode] = InputConstraint(
                mode=mode,
                min_images=max(item.min_images for item in constraints),
                max_images=min(item.max_images for item in constraints),
                required_views=tuple(view for view in ("front", "left", "back", "right") if view in required),
                allowed_views=tuple(view for view in ("front", "left", "back", "right") if view in allowed),
                formats=tuple(sorted(formats)),
                min_dimension=max(item.min_dimension for item in constraints),
                max_dimension=min(
                    (item.max_dimension for item in constraints if item.max_dimension), default=0
                ),
                max_file_bytes=min(
                    (item.max_file_bytes for item in constraints if item.max_file_bytes), default=0
                ),
                max_total_bytes=min(
                    (item.max_total_bytes for item in constraints if item.max_total_bytes), default=0
                ),
                note="Intersection of Tripo and Hunyuan input requirements.",
            )
        return result

    def validate_advanced(self, spec: AssetSpec) -> None:
        if spec.candidate_count < 2:
            raise ValidationError("compare requires at least two candidates")
        allowed = {provider.option_namespace for provider in self._providers}
        unknown = set(spec.advanced) - allowed
        if unknown:
            raise ValidationError(f"unsupported compare option namespaces: {', '.join(sorted(unknown))}")

    def validate_input_references(self, spec: AssetSpec, references) -> None:
        super().validate_input_references(spec, references)
        for provider in self._providers:
            provider.validate_input_references(spec, references)

    def estimate_generation_credits(self, spec: AssetSpec) -> float | None:
        counts = (spec.candidate_count // 2 + spec.candidate_count % 2, spec.candidate_count // 2)
        total = 0.0
        for provider, count in zip(self._providers, counts):
            child = replace(
                spec, provider=ProviderChoice(provider.id), candidate_count=count,
                advanced={
                    provider.option_namespace:
                    spec.advanced.get(provider.option_namespace, {})
                },
            )
            estimate = provider.estimate_generation_credits(child)
            if estimate is None:
                return None
            total += estimate
        return total

    def generate(
        self,
        job: AssetJob,
        destination: Path,
        *,
        cancel_event: Event | None = None,
        progress=None,
        task_submitted=None,
    ) -> list[Candidate]:
        self.validate_advanced(job.spec)
        counts = (job.spec.candidate_count // 2 + job.spec.candidate_count % 2, job.spec.candidate_count // 2)
        progress_values = [0.0, 0.0]
        progress_lock = threading.Lock()

        def run(index: int) -> list[Candidate]:
            provider = self._providers[index]
            child_spec = replace(
                job.spec,
                provider=ProviderChoice(provider.id),
                candidate_count=counts[index],
                advanced={
                    provider.option_namespace:
                    job.spec.advanced.get(provider.option_namespace, {})
                },
            )
            child = AssetJob(
                spec=child_spec, id=job.id, provider_task_ids=job.provider_task_ids,
                references=job.references,
            )

            def child_progress(value: float) -> None:
                with progress_lock:
                    progress_values[index] = value
                    if progress:
                        progress(sum(progress_values) / 2.0)

            return provider.generate(
                child, destination, cancel_event=cancel_event, progress=child_progress,
                task_submitted=task_submitted,
            )

        results: list[list[Candidate]] = []
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai3d-compare") as pool:
            futures = [(provider, pool.submit(run, index)) for index, provider in enumerate(self._providers)]
            for provider, future in futures:
                try:
                    results.append(future.result())
                except Exception:
                    failures.append(provider.id)
        if not results:
            raise ProviderUnavailableError("all compare providers failed")
        candidates = [candidate for group in results for candidate in group]
        if failures:
            for candidate in candidates:
                candidate.metadata["compare_partial_failures"] = failures
        return candidates
    def resume(
        self,
        job: AssetJob,
        destination: Path,
        *,
        cancel_event: Event | None = None,
        progress=None,
    ) -> list[Candidate]:
        active = [
            provider for provider in self._providers
            if any(
                key.startswith(f"{provider.option_namespace}_")
                for key in job.provider_task_ids
            )
        ]
        if not active:
            raise ProviderUnavailableError("compare job has no provider tasks to resume")

        def run(provider: ProviderAdapter) -> list[Candidate]:
            child_spec = replace(
                job.spec,
                provider=ProviderChoice(provider.id),
                advanced={
                    provider.option_namespace:
                    job.spec.advanced.get(provider.option_namespace, {})
                },
            )
            child = AssetJob(
                spec=child_spec, id=job.id, provider_task_ids=job.provider_task_ids,
                references=job.references,
            )
            return provider.resume(child, destination, cancel_event=cancel_event, progress=progress)

        results: list[list[Candidate]] = []
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=len(active), thread_name_prefix="ai3d-compare-recovery") as pool:
            futures = [(provider, pool.submit(run, provider)) for provider in active]
            for provider, future in futures:
                try:
                    results.append(future.result())
                except Exception:
                    failures.append(provider.id)
        if not results:
            raise ProviderUnavailableError("all compare provider recoveries failed")
        candidates = [candidate for group in results for candidate in group]
        if failures:
            for candidate in candidates:
                candidate.metadata["compare_partial_failures"] = failures
        return candidates
