from __future__ import annotations

import re
import uuid
import copy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .errors import InvalidTransitionError, ValidationError
from .provider_ids import canonical_provider_id

_ASSET_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SECRET_KEY = re.compile(
    r"(^|[_-])(api[_-]?key|authorization|secret(?:[_-]?key)?|access[_-]?token|token|credential)s?($|[_-])",
    re.IGNORECASE,
)


def _contains_secret_field(value: object) -> bool:
    if isinstance(value, dict):
        return any(_SECRET_KEY.search(str(key)) or _contains_secret_field(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_field(item) for item in value)
    return False


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ProviderChoice(StrEnum):
    AUTO = "auto"
    MOCK = "mock"
    TRIPO_CN = "tripo_cn"
    TRIPO_GLOBAL = "tripo_global"
    TRIPO = "tripo_global"  # Source-compatible alias; old manifests migrate in from_dict.
    HUNYUAN_DIRECT = "hunyuan_direct"
    HUNYUAN = "hunyuan_direct"
    TOKENHUB_CN = "tokenhub_cn"
    TOKENHUB_GLOBAL = "tokenhub_global"
    COMPARE_CN = "compare_cn"
    COMPARE_GLOBAL = "compare_global"
    COMPARE = "compare_global"
    COMPARE_LEGACY = "compare_legacy"


class InputMode(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    MULTIVIEW = "multiview"


class JobState(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    SUBMITTED = "submitted"
    PROCESSING = "processing"
    RECOVERY_PENDING = "recovery_pending"
    CANDIDATES_READY = "candidates_ready"
    CANDIDATE_IMPORTED = "candidate_imported"
    APPROVED = "approved"
    EXPORTED = "exported"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ProcessState(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    RECOVERY_PENDING = "recovery_pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.DRAFT: frozenset({JobState.QUEUED, JobState.SUBMITTED, JobState.CANCELLED, JobState.FAILED}),
    JobState.QUEUED: frozenset({JobState.SUBMITTED, JobState.CANCELLED, JobState.FAILED}),
    JobState.SUBMITTED: frozenset({JobState.PROCESSING, JobState.CANCELLED, JobState.FAILED}),
    JobState.PROCESSING: frozenset(
        {JobState.CANDIDATES_READY, JobState.RECOVERY_PENDING, JobState.CANCELLED, JobState.FAILED}
    ),
    JobState.RECOVERY_PENDING: frozenset({JobState.PROCESSING, JobState.CANCELLED, JobState.FAILED}),
    JobState.CANDIDATES_READY: frozenset({JobState.CANDIDATE_IMPORTED, JobState.FAILED}),
    JobState.CANDIDATE_IMPORTED: frozenset({JobState.APPROVED, JobState.FAILED}),
    JobState.APPROVED: frozenset({JobState.EXPORTED, JobState.FAILED}),
    JobState.EXPORTED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.FAILED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class AssetSpec:
    asset_name: str
    prompt: str = ""
    input_mode: InputMode = InputMode.TEXT
    reference_images: dict[str, str] = field(default_factory=dict)
    asset_profile: str = "small_prop_v1"
    provider: ProviderChoice = ProviderChoice.AUTO
    candidate_count: int = 1
    priority: int = 50
    max_estimated_credits: float = 0.0
    allow_over_budget: bool = False
    advanced: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _ASSET_NAME.fullmatch(self.asset_name):
            raise ValidationError("asset_name must be 2-64 lowercase snake_case characters")
        prompt = self.prompt.strip()
        if len(prompt) > 4000 or (self.input_mode == InputMode.TEXT and len(prompt) < 3):
            raise ValidationError("text input requires a 3-4000 character prompt")
        if self.input_mode == InputMode.TEXT and self.reference_images:
            raise ValidationError("text input cannot include reference images")
        if self.input_mode == InputMode.IMAGE:
            if set(self.reference_images) != {"front"}:
                raise ValidationError("image input requires exactly one front reference")
        if self.input_mode == InputMode.MULTIVIEW:
            if "front" not in self.reference_images or not 2 <= len(self.reference_images) <= 8:
                raise ValidationError("multiview input requires a front view and 1-7 additional views")
        allowed_views = {
            "front", "left", "right", "back", "top", "bottom", "left_front", "right_front"
        }
        if not set(self.reference_images).issubset(allowed_views):
            raise ValidationError("reference image view is unsupported")
        if any(not re.fullmatch(r"[a-f0-9]{32}", value) for value in self.reference_images.values()):
            raise ValidationError("reference image id is invalid")
        if not (1 <= self.candidate_count <= 4):
            raise ValidationError("candidate_count must be between 1 and 4")
        if not (0 <= self.priority <= 100):
            raise ValidationError("priority must be between 0 and 100")
        if self.max_estimated_credits < 0:
            raise ValidationError("max_estimated_credits cannot be negative")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", self.asset_profile):
            raise ValidationError("asset_profile is invalid")
        if not isinstance(self.advanced, dict):
            raise ValidationError("advanced must be an object")
        if _contains_secret_field(self.advanced):
            raise ValidationError("provider credentials cannot be supplied inside an asset specification")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AssetSpec":
        try:
            provider = ProviderChoice(canonical_provider_id(value.get("provider", ProviderChoice.AUTO)))
            input_mode = InputMode(value.get("input_mode", InputMode.TEXT))
        except ValueError as exc:
            raise ValidationError("unsupported provider or input mode") from exc
        return cls(
            asset_name=str(value.get("asset_name", "")),
            prompt=str(value.get("prompt", "")),
            input_mode=input_mode,
            reference_images={str(k): str(v) for k, v in value.get("reference_images", {}).items()},
            asset_profile=str(value.get("asset_profile", "small_prop_v1")),
            provider=provider,
            candidate_count=int(value.get("candidate_count", 1)),
            priority=int(value.get("priority", 50)),
            max_estimated_credits=float(value.get("max_estimated_credits", 0.0)),
            allow_over_budget=bool(value.get("allow_over_budget", False)),
            advanced=dict(value.get("advanced", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["provider"] = self.provider.value
        result["input_mode"] = self.input_mode.value
        return result

    def public_dict(self) -> dict[str, Any]:
        result = self.to_dict()
        advanced = copy.deepcopy(result.get("advanced", {}))
        for options in advanced.values():
            if isinstance(options, dict):
                options.pop("account_profile", None)
        result["advanced"] = advanced
        return result


@dataclass(slots=True)
class Candidate:
    id: str
    provider: str
    label: str
    local_model_path: str
    format: str
    metadata: dict[str, Any] = field(default_factory=dict)
    imported_collection: str | None = None
    review_status: str = "pending"
    review_note: str = ""

    def public_dict(self) -> dict[str, Any]:
        # Provider task ids are durable recovery state, not public orchestration
        # data. Keep them in persisted_dict only, just like local paths and URLs.
        public_metadata = copy.deepcopy(self.metadata)
        public_metadata.pop("provider_task_id", None)
        return {
            "id": self.id,
            "provider": self.provider,
            "label": self.label,
            "format": self.format,
            "metadata": public_metadata,
            "imported": self.imported_collection is not None,
            "imported_collection": self.imported_collection,
            "review_status": self.review_status,
            "review_note": self.review_note,
        }

    def persisted_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Candidate":
        return cls(
            id=str(value["id"]), provider=str(canonical_provider_id(value["provider"])),
            label=str(value.get("label", value["id"])), local_model_path=str(value["local_model_path"]),
            format=str(value.get("format", "unknown")), metadata=dict(value.get("metadata", {})),
            imported_collection=value.get("imported_collection"),
            review_status=str(value.get("review_status", "pending")),
            review_note=str(value.get("review_note", "")),
        )


@dataclass(slots=True)
class ReferenceImage:
    id: str
    view: str
    filename: str
    format: str
    bytes: int
    width: int
    height: int
    sha256: str
    local_path: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "view": self.view,
            "filename": self.filename,
            "format": self.format,
            "bytes": self.bytes,
            "width": self.width,
            "height": self.height,
            "sha256": self.sha256,
        }

    def persisted_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReferenceImage":
        return cls(
            id=str(value["id"]), view=str(value["view"]), filename=str(value["filename"]),
            format=str(value["format"]), bytes=int(value["bytes"]), width=int(value["width"]),
            height=int(value["height"]), sha256=str(value["sha256"]),
            local_path=str(value["local_path"]),
        )



@dataclass(slots=True)
class JobEvent:
    at: str
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "JobEvent":
        return cls(at=str(value["at"]), kind=str(value["kind"]), detail=dict(value.get("detail", {})))


@dataclass(slots=True)
class ProcessTask:
    operation: str
    provider: str
    source_candidate_id: str
    params: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: ProcessState = ProcessState.QUEUED
    progress: float = 0.0
    provider_task_id: str | None = None
    artifact_id: str | None = None
    result_candidate_id: str | None = None
    error: dict[str, Any] | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    paused: bool = False

    def public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["state"] = self.state.value
        result.pop("provider_task_id", None)
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProcessTask":
        return cls(
            operation=str(value["operation"]), provider=str(canonical_provider_id(value["provider"])),
            source_candidate_id=str(value["source_candidate_id"]), params=dict(value.get("params", {})),
            id=str(value["id"]), state=ProcessState(str(value.get("state", "failed"))),
            progress=float(value.get("progress", 0.0)), provider_task_id=value.get("provider_task_id"),
            artifact_id=value.get("artifact_id"),
            result_candidate_id=value.get("result_candidate_id"),
            error=dict(value["error"]) if isinstance(value.get("error"), dict) else None,
            created_at=str(value.get("created_at", utc_now())),
            updated_at=str(value.get("updated_at", utc_now())),
            paused=bool(value.get("paused", False)),
        )


@dataclass(slots=True)
class AssetJob:
    spec: AssetSpec
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: JobState = JobState.DRAFT
    progress: float = 0.0
    phase: str = ""
    provider_task_ids: dict[str, str] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    references: list[ReferenceImage] = field(default_factory=list)
    selected_candidate_id: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    process_tasks: list[ProcessTask] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    events: list[JobEvent] = field(default_factory=list)
    error: dict[str, Any] | None = None
    active_provider: str | None = None
    parent_job_id: str | None = None
    archived: bool = False
    paused: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def transition(self, target: JobState, *, kind: str, detail: dict[str, Any] | None = None) -> None:
        if target not in _TRANSITIONS[self.state]:
            raise InvalidTransitionError(f"cannot transition {self.state.value} to {target.value}")
        self.state = target
        self.updated_at = utc_now()
        self.events.append(JobEvent(at=self.updated_at, kind=kind, detail=detail or {}))

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state.value,
            "progress": round(self.progress, 3),
            "phase": self.phase,
            "spec": self.spec.public_dict(),
            "candidates": [candidate.public_dict() for candidate in self.candidates],
            "references": [reference.public_dict() for reference in self.references],
            "selected_candidate_id": self.selected_candidate_id,
            "artifacts": [
                {key: value for key, value in artifact.items() if key != "local_path"}
                for artifact in self.artifacts
            ],
            "process_tasks": [task.public_dict() for task in self.process_tasks],
            "usage": self.usage,
            "active_provider": self.active_provider,
            "parent_job_id": self.parent_job_id,
            "archived": self.archived,
            "paused": self.paused,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def persisted_dict(self) -> dict[str, Any]:
        result = self.public_dict()
        result["spec"] = self.spec.to_dict()
        result["provider_task_ids"] = self.provider_task_ids
        result["candidates"] = [candidate.persisted_dict() for candidate in self.candidates]
        result["references"] = [reference.persisted_dict() for reference in self.references]
        result["artifacts"] = self.artifacts
        result["process_tasks"] = [{**task.public_dict(), "provider_task_id": task.provider_task_id} for task in self.process_tasks]
        result["events"] = [asdict(event) for event in self.events]
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AssetJob":
        try:
            raw_state = str(value.get("state", JobState.DRAFT.value))
            state = JobState.CANDIDATE_IMPORTED if raw_state == "inspected" else JobState(raw_state)
            spec = AssetSpec.from_dict(dict(value["spec"]))
            candidates = [Candidate.from_dict(dict(item)) for item in value.get("candidates", [])]
            references = [ReferenceImage.from_dict(dict(item)) for item in value.get("references", [])]
            events = [JobEvent.from_dict(dict(item)) for item in value.get("events", [])]
            process_tasks = [ProcessTask.from_dict(dict(item)) for item in value.get("process_tasks", [])]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("job manifest is invalid") from exc
        return cls(
            spec=spec,
            id=str(value["id"]),
            state=state,
            progress=float(value.get("progress", 0.0)),
            phase=str(value.get("phase", "")),
            provider_task_ids={str(k): str(v) for k, v in value.get("provider_task_ids", {}).items()},
            candidates=candidates,
            references=references,
            selected_candidate_id=value.get("selected_candidate_id"),
            artifacts=list(value.get("artifacts", [])),
            process_tasks=process_tasks,
            usage=dict(value.get("usage", {})),
            events=events,
            error=dict(value["error"]) if isinstance(value.get("error"), dict) else None,
            active_provider=canonical_provider_id(value.get("active_provider")),
            parent_job_id=value.get("parent_job_id"),
            archived=bool(value.get("archived", False)),
            paused=bool(value.get("paused", False)),
            created_at=str(value.get("created_at", utc_now())),
            updated_at=str(value.get("updated_at", utc_now())),
        )
