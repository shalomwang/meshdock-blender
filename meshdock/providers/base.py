from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Event
from typing import Any

from ..core.errors import ProviderUnavailableError, ValidationError
from ..core.models import AssetJob, AssetSpec, Candidate, ReferenceImage

ProgressCallback = Any


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    provider: str
    configured: bool
    available: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class InputConstraint:
    mode: str
    min_images: int = 0
    max_images: int = 0
    required_views: tuple[str, ...] = ()
    allowed_views: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()
    min_dimension: int = 0
    max_dimension: int = 0
    max_file_bytes: int = 0
    max_total_bytes: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("required_views", "allowed_views", "formats"):
            value[key] = list(value[key])
        return value


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    provider: str
    generation_modes: tuple[str, ...]
    output_formats: tuple[str, ...]
    postprocess: tuple[str, ...] = ()
    input_constraints: dict[str, InputConstraint] = field(default_factory=dict)
    process_schema: dict[str, Any] = field(default_factory=dict)
    advanced_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["generation_modes"] = list(self.generation_modes)
        value["output_formats"] = list(self.output_formats)
        value["postprocess"] = list(self.postprocess)
        value["input_constraints"] = {
            key: constraint.to_dict() for key, constraint in self.input_constraints.items()
        }
        return value


class ProviderAdapter(ABC):
    id: str
    option_namespace: str

    @abstractmethod
    def status(self) -> ProviderStatus:
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    @abstractmethod
    def generate(
        self,
        job: AssetJob,
        destination: Path,
        *,
        cancel_event: Event | None = None,
        progress: ProgressCallback | None = None,
        task_submitted: ProgressCallback | None = None,
    ) -> list[Candidate]:
        """Submit, wait, and immediately download candidates into destination.

        Async provider mechanics stay inside the adapter so the public task model does
        not expose expiring provider URLs or provider-specific task shapes.
        """
        raise NotImplementedError

    def resume(
        self,
        job: AssetJob,
        destination: Path,
        *,
        cancel_event: Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> list[Candidate]:
        raise ProviderUnavailableError(f"{self.id} does not support task recovery")

    def validate_advanced(self, spec: AssetSpec) -> None:
        namespace = getattr(self, "option_namespace", self.id)
        if spec.advanced and namespace not in spec.advanced:
            # Common advanced options may be added later. For now, require namespacing.
            unknown = ", ".join(sorted(spec.advanced))
            raise ValidationError(f"advanced options must be namespaced under {namespace}: {unknown}")

    def resolve_input_constraints(
        self, advanced: dict[str, Any] | None = None
    ) -> dict[str, InputConstraint]:
        return self.capabilities().input_constraints

    def validate_input_references(
        self, spec: AssetSpec, references: list[ReferenceImage]
    ) -> None:
        constraints = self.resolve_input_constraints(spec.advanced)
        constraint = constraints.get(spec.input_mode.value)
        if constraint is None:
            raise ValidationError(f"{self.id} does not support {spec.input_mode.value} input")
        views = {item.view for item in references}
        if not constraint.min_images <= len(references) <= constraint.max_images:
            raise ValidationError(
                f"{self.id} {constraint.mode} input requires "
                f"{constraint.min_images}-{constraint.max_images} images"
            )
        missing = set(constraint.required_views) - views
        if missing:
            raise ValidationError(f"missing required views: {', '.join(sorted(missing))}")
        unsupported = views - set(constraint.allowed_views)
        if unsupported:
            raise ValidationError(
                f"{self.id} does not support views: {', '.join(sorted(unsupported))}"
            )
        if constraint.formats:
            invalid_formats = {item.format for item in references} - set(constraint.formats)
            if invalid_formats:
                raise ValidationError(
                    f"{self.id} {constraint.mode} input does not support: "
                    f"{', '.join(sorted(invalid_formats))}"
                )
        if constraint.min_dimension and any(
            min(item.width, item.height) < constraint.min_dimension for item in references
        ):
            raise ValidationError(
                f"{self.id} images must be at least {constraint.min_dimension}px per side"
            )
        if constraint.max_dimension and any(
            max(item.width, item.height) > constraint.max_dimension for item in references
        ):
            raise ValidationError(
                f"{self.id} images must be at most {constraint.max_dimension}px per side"
            )
        if constraint.max_file_bytes and any(
            item.bytes > constraint.max_file_bytes for item in references
        ):
            raise ValidationError(
                f"{self.id} image files must be at most "
                f"{constraint.max_file_bytes // 1_000_000} MB each"
            )
        if constraint.max_total_bytes and sum(item.bytes for item in references) > constraint.max_total_bytes:
            raise ValidationError(
                f"{self.id} image input exceeds the {constraint.max_total_bytes // 1_000_000} MB total limit"
            )

    def process(
        self,
        job: AssetJob,
        candidate: Candidate,
        operation: str,
        params: dict[str, Any],
        destination: Path,
        *,
        cancel_event: Event | None = None,
        progress: ProgressCallback | None = None,
        task_submitted: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        raise ProviderUnavailableError(f"{self.id} does not support {operation}")

    def validate_process(
        self, job: AssetJob, candidate: Candidate, operation: str, params: dict[str, Any]
    ) -> None:
        """Validate process parameters before they are persisted or billed."""
        return

    def estimate_generation_credits(self, spec: AssetSpec) -> float | None:
        return None

    def estimate_process_credits(self, operation: str, params: dict[str, Any]) -> float | None:
        return None

    def resume_process(
        self,
        job: AssetJob,
        candidate: Candidate,
        operation: str,
        params: dict[str, Any],
        provider_task_id: str,
        destination: Path,
        *,
        cancel_event: Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        raise ProviderUnavailableError(f"{self.id} does not support process task recovery")
