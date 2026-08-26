from __future__ import annotations

import re
import time
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from ..core.errors import (
    JobCancelledError,
    ProviderProtocolError,
    ProviderTaskError,
    ProviderTimeoutError,
    ValidationError,
)
from ..core.models import AssetJob, AssetSpec, Candidate, InputMode, ReferenceImage
from ..core.profiles import get_profile
from .base import InputConstraint, ProviderAdapter, ProviderCapabilities, ProviderStatus
from .http import HttpTransport, SecureHttpClient
from .artifacts import download_declared_resources, download_model


class CredentialReader(Protocol):
    def configured(self, provider: str) -> bool: ...
    def use(self, provider: str, profile_id: str | None = None) -> str: ...


class TripoAdapter(ProviderAdapter):
    """Tripo V3 text/image/multiview generation with immediate local download."""

    id = "tripo_global"
    option_namespace = "tripo"
    base_url = "https://openapi.tripo3d.ai/v3"
    default_model = "v3.1-20260211"

    _ALLOWED_ADVANCED = {
        "model", "negative_prompt", "model_seed", "image_seed", "texture_seed",
        "face_limit", "texture", "pbr", "texture_quality", "geometry_quality",
        "auto_size", "quad", "smart_low_poly", "generate_parts", "compress", "export_uv",
        "enable_image_autofix", "texture_alignment", "orientation", "account_profile",
    }
    _VIEWS = ("front", "left", "back", "right")

    @classmethod
    def _input_constraints(cls) -> dict[str, InputConstraint]:
        return {
            "text": InputConstraint(mode="text"),
            "image": InputConstraint(
                mode="image", min_images=1, max_images=1,
                required_views=("front",), allowed_views=("front",),
                formats=("png", "jpg", "webp"), min_dimension=128,
                note="One front image.",
            ),
            "multiview": InputConstraint(
                mode="multiview", min_images=2, max_images=4,
                required_views=("front",), allowed_views=cls._VIEWS,
                formats=("png", "jpg", "webp"), min_dimension=128,
                note="2-4 views; front is required. Canonical order: front, left, back, right.",
            ),
        }

    def __init__(
        self,
        credentials: CredentialReader,
        http: HttpTransport | None = None,
        *,
        poll_interval: float = 2.0,
        timeout: float = 20 * 60,
        sleep=time.sleep,
        provider_id: str = "tripo_global",
        base_url: str = "https://openapi.tripo3d.ai/v3",
    ) -> None:
        if provider_id not in {"tripo_cn", "tripo_global"}:
            raise ValueError("unsupported Tripo provider id")
        if base_url not in {
            "https://openapi.tripo3d.com/v3", "https://openapi.tripo3d.ai/v3",
        }:
            raise ValueError("unsupported Tripo base URL")
        self.id = provider_id
        self.base_url = base_url
        self._credentials = credentials
        self._http = http or SecureHttpClient()
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._sleep = sleep

    def resolve_input_constraints(
        self, advanced: dict[str, Any] | None = None
    ) -> dict[str, InputConstraint]:
        options = (advanced or {}).get(self.option_namespace, {})
        if not isinstance(options, dict):
            raise ValidationError("advanced.tripo must be an object")
        unknown = set(options) - self._ALLOWED_ADVANCED
        if unknown:
            raise ValidationError(f"unsupported Tripo options: {', '.join(sorted(unknown))}")
        model = str(options.get("model", self.default_model))
        if model not in {"v3.1-20260211", "v3.0-20250812", "v2.5-20250123", "P1-20260311"}:
            raise ValidationError("unsupported Tripo generation model")
        return self._input_constraints()

    def status(self) -> ProviderStatus:
        configured = self._credentials.configured(self.id)
        return ProviderStatus(provider=self.id, configured=configured, available=configured)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.id,
            generation_modes=("text", "image", "multiview"),
            output_formats=("glb", "fbx", "obj"),
            postprocess=(
                "retopology", "texture", "segment", "convert",
                "rig_check", "rig", "animate",
            ),
            input_constraints=self._input_constraints(),
            process_schema={
                "retopology": {"defaults": {"face_limit": 5000, "quad": False, "bake": True}},
                "texture": {"defaults": {"texture_quality": "detailed", "pbr": True}},
                "segment": {"defaults": {"segmentation_granularity": "balanced"}},
                "convert": {"defaults": {"format": "FBX"}},
                "rig_check": {"defaults": {}, "source_formats": ["glb"]},
                "rig": {
                    "defaults": {"model": "v1.0-20240301", "rig_type": "biped", "spec": "tripo", "out_format": "glb"},
                    "fields": {
                        "model": {"enum": ["v1.0-20240301", "v2.5-20260210"]},
                        "rig_type": {"enum": ["biped", "quadruped", "hexapod", "octopod", "avian", "serpentine", "aquatic"]},
                        "spec": {"enum": ["tripo", "mixamo"]},
                        "out_format": {"enum": ["glb", "fbx"]},
                    },
                },
                "animate": {"defaults": {"animation": "preset:idle", "out_format": "glb", "bake_animation": True}},
            },
            advanced_schema={
                "tripo": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "model": {"enum": ["v3.1-20260211", "v3.0-20250812", "v2.5-20250123", "P1-20260311"]},
                        "negative_prompt": {"type": "string", "maxLength": 255},
                        "face_limit": {
                            "type": "integer", "minimum": 1, "maximum": 2000000,
                            "modelRanges": {
                                "P1-20260311": [50, 20000],
                                "v2.5-20250123": [1, 500000],
                                "v3.0-20250812": [1, 2000000],
                                "v3.1-20260211": [1, 2000000],
                            },
                            "settingRanges": {
                                "v3.0-20250812": {
                                    "standardMaximum": 1000000,
                                    "detailedMaximum": 2000000,
                                    "quadMaximum": 150000,
                                    "smartLowPolyTriangles": [500, 20000],
                                    "smartLowPolyQuads": [500, 10000],
                                },
                                "v3.1-20260211": {
                                    "standardMaximum": 1500000,
                                    "detailedMaximum": 2000000,
                                    "quadMaximum": 150000,
                                    "smartLowPolyTriangles": [500, 20000],
                                    "smartLowPolyQuads": [500, 10000],
                                },
                            },
                        },
                        "texture": {"type": "boolean"},
                        "pbr": {"type": "boolean"},
                        "texture_quality": {
                            "enum": ["standard", "detailed", "extreme"],
                            "models": ["v3.0-20250812", "v3.1-20260211", "P1-20260311"],
                            "extremeModels": ["v3.0-20250812", "v3.1-20260211"],
                        },
                        "geometry_quality": {
                            "enum": ["standard", "detailed"],
                            "models": ["v3.0-20250812", "v3.1-20260211"],
                        },
                        "auto_size": {
                            "type": "boolean", "models": ["v3.0-20250812", "v3.1-20260211"],
                        },
                        "quad": {
                            "type": "boolean", "models": ["v3.0-20250812", "v3.1-20260211"],
                        },
                        "smart_low_poly": {
                            "type": "boolean", "models": ["v3.0-20250812", "v3.1-20260211"],
                        },
                        "generate_parts": {
                            "type": "boolean", "models": ["v3.0-20250812", "v3.1-20260211"],
                        },
                        "compress": {
                            "enum": ["geometry"], "models": ["v3.0-20250812", "v3.1-20260211"],
                        },
                        "export_uv": {"type": "boolean"},
                        "enable_image_autofix": {"type": "boolean", "inputModes": ["image"]},
                        "texture_alignment": {"enum": ["original_image", "geometry"], "inputModes": ["image"]},
                        "orientation": {"enum": ["default", "align_image"], "inputModes": ["image"]},
                    },
                }
            },
        )

    def validate_advanced(self, spec: AssetSpec) -> None:
        super().validate_advanced(spec)
        if len(spec.prompt) > 1024:
            raise ValidationError("Tripo prompt exceeds 1024 characters")
        options = spec.advanced.get(self.option_namespace, {})
        if not isinstance(options, dict):
            raise ValidationError("advanced.tripo must be an object")
        unknown = set(options) - self._ALLOWED_ADVANCED
        if unknown:
            raise ValidationError(f"unsupported Tripo options: {', '.join(sorted(unknown))}")
        if options.get("generate_parts") and (options.get("texture", True) or options.get("pbr", True)):
            raise ValidationError("Tripo generate_parts requires texture=false and pbr=false")
        if options.get("generate_parts") and options.get("quad"):
            raise ValidationError("Tripo generate_parts is incompatible with quad=true")
        model = str(options.get("model", self.default_model))
        if model not in {"v3.1-20260211", "v3.0-20250812", "v2.5-20250123", "P1-20260311"}:
            raise ValidationError("unsupported Tripo generation model")
        image_only = {"enable_image_autofix", "texture_alignment", "orientation"} & set(options)
        if image_only and spec.input_mode != InputMode.IMAGE:
            raise ValidationError(
                "Tripo options are image-only: " + ", ".join(sorted(image_only))
            )
        if options.get("texture_alignment") not in {None, "original_image", "geometry"}:
            raise ValidationError("unsupported Tripo texture alignment")
        if options.get("orientation") not in {None, "default", "align_image"}:
            raise ValidationError("unsupported Tripo image orientation")
        if options.get("orientation") == "align_image" and options.get("texture", True) is False:
            raise ValidationError("align_image orientation requires texture=true")
        profile_id = options.get("account_profile")
        if profile_id is not None and not re.fullmatch(r"[a-f0-9]{32}", str(profile_id)):
            raise ValidationError("Tripo credential selection is invalid")
        if model == "v2.5-20250123":
            restricted = {
                "texture_quality", "geometry_quality", "auto_size", "quad",
                "smart_low_poly", "generate_parts", "compress",
            } & set(options)
            if restricted:
                raise ValidationError(
                    "Tripo v2.5 does not support: " + ", ".join(sorted(restricted))
                )
        if model == "P1-20260311":
            restricted = {
                "geometry_quality", "auto_size", "quad", "smart_low_poly",
                "generate_parts", "compress",
            } & set(options)
            if restricted:
                raise ValidationError(
                    "Tripo P1 does not support: " + ", ".join(sorted(restricted))
                )
            if options.get("texture_quality") not in {None, "standard", "detailed"}:
                raise ValidationError("Tripo P1 supports standard or detailed textures only")
        if "face_limit" in options:
            face_limit = int(options["face_limit"])
            if model == "P1-20260311" and not 50 <= face_limit <= 20_000:
                raise ValidationError("Tripo P1 face_limit must be between 50 and 20000")
            if model != "P1-20260311" and face_limit < 1:
                raise ValidationError("Tripo face_limit must be positive")
            smart_maximum = 10_000 if options.get("quad") else 20_000
            if options.get("smart_low_poly") and not 500 <= face_limit <= smart_maximum:
                raise ValidationError("Tripo smart low-poly face_limit is outside its documented range")
            if not options.get("smart_low_poly") and model != "P1-20260311":
                maximum = {
                    "v2.5-20250123": 500_000,
                    "v3.0-20250812": (
                        2_000_000 if options.get("geometry_quality") == "detailed" else 1_000_000
                    ),
                    "v3.1-20260211": (
                        2_000_000 if options.get("geometry_quality") == "detailed" else 1_500_000
                    ),
                }[model]
                if options.get("quad"):
                    maximum = min(maximum, 150_000)
                if face_limit > maximum:
                    raise ValidationError(
                        f"Tripo face_limit exceeds the {maximum} limit for the selected settings"
                    )

    def estimate_generation_credits(self, spec: AssetSpec) -> float | None:
        options = spec.advanced.get(self.option_namespace, {})
        model = str(options.get("model", self.default_model))
        if model not in {"v3.1-20260211", "P1-20260311"}:
            return None
        textured = bool(options.get("texture", True) or options.get("pbr", True))
        quality = str(options.get("texture_quality", "standard"))
        if quality == "extreme":
            # The docs say Extreme costs more, but do not publish the increment.
            return None
        base = 30.0 if model == "P1-20260311" else 10.0
        if spec.input_mode != InputMode.TEXT:
            base += 10.0
        if textured:
            base += 10.0 if quality == "standard" else 20.0
        if model == "v3.1-20260211":
            if options.get("geometry_quality") == "detailed":
                base += 20.0
            if options.get("quad"):
                base += 5.0
            if options.get("smart_low_poly"):
                base += 10.0
            if options.get("generate_parts"):
                base += 20.0
        return base * spec.candidate_count

    def estimate_process_credits(self, operation: str, params: dict[str, Any]) -> float | None:
        # Tripo returns authoritative credits_consumed after completion; preflight values are conservative hints.
        if operation == "animate":
            animations = params.get("animations")
            count = len(animations) if isinstance(animations, list) else 1
            return 10.0 * count
        return {
            "retopology": 20.0, "texture": 30.0, "segment": 40.0, "convert": 5.0,
            "rig_check": 0.0, "rig": 30.0,
        }.get(operation)

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
        self.validate_input_references(job.spec, job.references)
        token = self._credentials.use(self.id, self._account_profile(job.spec))
        uploaded = self._upload_references(job.references, token) if job.references else {}
        if job.spec.input_mode == InputMode.TEXT:
            endpoint = "text-to-model"
        elif job.spec.input_mode == InputMode.IMAGE:
            endpoint = "image-to-model"
        else:
            endpoint = "multiview-to-model"
        tasks: list[str] = []
        for index in range(job.spec.candidate_count):
            self._check_cancel(cancel_event)
            response = self._http.request_json(
                "POST",
                f"{self.base_url}/generation/{endpoint}",
                token=token,
                payload=self._generation_payload(job.spec, index, uploaded),
            )
            task_id = self._task_id(response)
            tasks.append(task_id)
            key = f"tripo_{index + 1}"
            if task_submitted:
                task_submitted(key, task_id)
            else:
                job.provider_task_ids[key] = task_id
        completed = self._wait_for_tasks(tasks, token, cancel_event, progress)
        return self._download_completed(job, destination, tasks, completed, progress)
    def resume(
        self,
        job: AssetJob,
        destination: Path,
        *,
        cancel_event: Event | None = None,
        progress=None,
    ) -> list[Candidate]:
        token = self._credentials.use(self.id, self._account_profile(job.spec))
        tasks = [
            value for key, value in sorted(job.provider_task_ids.items())
            if key.startswith("tripo_")
        ]
        if not tasks:
            raise ProviderProtocolError("Tripo job has no task ids to resume")
        completed = self._wait_for_tasks(tasks, token, cancel_event, progress)
        return self._download_completed(job, destination, tasks, completed, progress)

    def _download_completed(self, job, destination, tasks, completed, progress):
        candidates: list[Candidate] = []
        preferred = "fbx" if job.spec.advanced.get(self.option_namespace, {}).get("quad") else None
        for index, task in enumerate(completed, start=1):
            output = task.get("output")
            if not isinstance(output, dict) or not isinstance(output.get("model_url"), str):
                raise ProviderProtocolError("Tripo task succeeded without a model URL")
            download, model_format = download_model(
                self._http,
                output["model_url"],
                destination / f"{job.spec.asset_name}_tripo_{index}",
                declared_format=preferred,
            )
            resources = download_declared_resources(
                self._http, output.get("resources"), download.path.parent
            )
            preview_name = None
            if isinstance(output.get("rendered_image_url"), str):
                preview_path = destination / f"{job.spec.asset_name}_tripo_{index}_preview.png"
                try:
                    self._http.download(output["rendered_image_url"], preview_path, max_bytes=50_000_000)
                    preview_name = preview_path.name
                except Exception:
                    preview_name = None
            candidates.append(
                Candidate(
                    id=f"tripo_{index}", provider=self.id, label=f"Tripo candidate {index}",
                    local_model_path=str(download.path), format=model_format,
                    metadata={
                        "provider_task_id": str(task.get("task_id", tasks[index - 1])),
                        "credits_consumed": task.get("credits_consumed"),
                        "bytes": download.bytes_written,
                        "sha256": download.sha256,
                        "preview_file": preview_name,
                        "resource_files": resources,
                    },
                )
            )
        if progress:
            progress(1.0)
        return candidates

    def _generation_payload(
        self, spec: AssetSpec, index: int, uploaded: dict[str, str] | None = None
    ) -> dict[str, Any]:
        profile = get_profile(spec.asset_profile)
        options = dict(spec.advanced.get(self.option_namespace, {}))
        options.pop("account_profile", None)
        model = str(options.get("model", self.default_model))
        face_limit = int(profile.triangle_hard_cap)
        if model == "P1-20260311":
            face_limit = min(max(face_limit, 50), 20_000)
        elif options.get("smart_low_poly"):
            maximum = 10_000 if options.get("quad") else 20_000
            face_limit = min(max(face_limit, 500), maximum)
        else:
            maximum = {
                "v2.5-20250123": 500_000,
                "v3.0-20250812": (
                    2_000_000 if options.get("geometry_quality") == "detailed" else 1_000_000
                ),
                "v3.1-20260211": (
                    2_000_000 if options.get("geometry_quality") == "detailed" else 1_500_000
                ),
            }.get(model, 1_500_000)
            if options.get("quad"):
                maximum = min(maximum, 150_000)
            face_limit = min(max(face_limit, 1), maximum)
        payload: dict[str, Any] = {
            "model": model,
            "face_limit": face_limit, "texture": True, "pbr": True,
            "export_uv": True,
        }
        if model in {"v3.0-20250812", "v3.1-20260211"}:
            payload.update({
                "texture_quality": "detailed", "geometry_quality": "standard",
                "auto_size": True,
                "smart_low_poly": profile.triangle_hard_cap <= 20_000,
            })
        elif model == "P1-20260311":
            payload["texture_quality"] = "detailed"
        if spec.input_mode == InputMode.TEXT:
            payload["prompt"] = spec.prompt
        elif spec.input_mode == InputMode.IMAGE:
            payload["input"] = (uploaded or {})["front"]
        else:
            payload["inputs"] = [{view: token} for view, token in (uploaded or {}).items()]
        payload.update(options)
        for seed_name in ("model_seed", "image_seed", "texture_seed"):
            if seed_name in payload:
                payload[seed_name] = int(payload[seed_name]) + index
        return payload

    def _upload_references(self, references: list[ReferenceImage], token: str) -> dict[str, str]:
        uploaded: dict[str, str] = {}
        content_types = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
        for reference in references:
            uploaded[reference.view] = self._upload_file_token(
                Path(reference.local_path), reference.format, token,
                content_type=content_types.get(reference.format, "application/octet-stream"),
            )
        return uploaded

    def _upload_file_token(
        self, source: Path, file_format: str, token: str, *, content_type: str = "application/octet-stream"
    ) -> str:
        response = self._http.request_json(
            "POST", f"{self.base_url}/files/presign", token=token,
            payload={"format": file_format.lower()},
        )
        data = response.get("data", response)
        if not isinstance(data, dict):
            raise ProviderProtocolError("Tripo upload response was invalid")
        upload_url = data.get("presigned_url")
        file_token = data.get("file_token")
        if not isinstance(upload_url, str) or not isinstance(file_token, str):
            raise ProviderProtocolError("Tripo upload response omitted its URL or file token")
        self._http.upload_file(upload_url, source, content_type=content_type)
        return file_token

    def process(
        self,
        job: AssetJob,
        candidate: Candidate,
        operation: str,
        params: dict[str, Any],
        destination: Path,
        *,
        cancel_event: Event | None = None,
        progress=None,
        task_submitted=None,
    ) -> dict[str, Any]:
        endpoints = {
            "retopology": "mesh/decimate",
            "texture": "models/texture",
            "segment": "mesh/segment",
            "convert": "models/convert",
            "rig_check": "animations/rig-check",
            "rig": "animations/rig",
            "animate": "animations/retarget",
        }
        if operation not in endpoints:
            raise ValidationError(f"Tripo does not support {operation}")
        token = self._credentials.use(self.id, self._account_profile(job.spec))
        source_task = candidate.metadata.get("provider_task_id") if candidate.provider == self.id else None
        if operation == "animate" and not (
            self._is_task_id(source_task)
            and candidate.metadata.get("process_operation") == "rig"
        ):
            raise ValidationError("Tripo animation requires a Tripo auto-rig result candidate")
        if self._is_task_id(source_task):
            source_input = source_task
        else:
            if operation == "rig_check" and candidate.format.lower() != "glb":
                raise ValidationError("Tripo riggability check accepts GLB sources only")
            source_input = self._upload_file_token(
                Path(candidate.local_model_path), candidate.format, token
            )
        payload = self._process_payload(operation, params)
        payload["input"] = source_input
        response = self._http.request_json(
            "POST", f"{self.base_url}/{endpoints[operation]}", token=token, payload=payload
        )
        task_id = self._task_id(response)
        if task_submitted:
            task_submitted(task_id)
        completed = self._wait_for_tasks([task_id], token, cancel_event, progress)[0]
        return self._completed_process_result(operation, params, destination, task_id, completed)

    def validate_process(
        self, job: AssetJob, candidate: Candidate, operation: str, params: dict[str, Any]
    ) -> None:
        self._process_payload(operation, params)
        if operation == "animate" and candidate.metadata.get("process_operation") != "rig":
            raise ValidationError("Tripo animation requires a Tripo auto-rig result candidate")

    def resume_process(
        self, job: AssetJob, candidate: Candidate, operation: str, params: dict[str, Any],
        provider_task_id: str, destination: Path, *, cancel_event: Event | None = None,
        progress=None,
    ) -> dict[str, Any]:
        token = self._credentials.use(self.id, self._account_profile(job.spec))
        completed = self._wait_for_tasks(
            [provider_task_id], token, cancel_event, progress
        )[0]
        return self._completed_process_result(
            operation, params, destination, provider_task_id, completed
        )

    def _completed_process_result(
        self, operation: str, params: dict[str, Any], destination: Path,
        task_id: str, completed: dict[str, Any],
    ) -> dict[str, Any]:
        output = completed.get("output")
        if operation == "rig_check":
            if not isinstance(output, dict) or not isinstance(output.get("riggable"), bool):
                raise ProviderProtocolError("Tripo rig check completed without a riggability result")
            return {
                "provider_task_id": task_id,
                "diagnostics": {
                    "riggable": output["riggable"],
                    "recommended_rig_type": output.get("rig_type"),
                },
                "credits_consumed": completed.get("credits_consumed"),
            }
        if not isinstance(output, dict) or not isinstance(output.get("model_url"), str):
            raise ProviderProtocolError("Tripo processing task completed without a model URL")
        declared = str(params.get("format", params.get("out_format", ""))).lower() or None
        download, model_format = download_model(
            self._http, output["model_url"], destination / operation, declared_format=declared
        )
        resources = download_declared_resources(
            self._http, output.get("resources"), download.path.parent
        )
        preview_name = None
        if isinstance(output.get("rendered_image_url"), str):
            preview_path = destination / f"{operation}_preview.png"
            try:
                self._http.download(output["rendered_image_url"], preview_path, max_bytes=50_000_000)
                preview_name = preview_path.name
            except Exception:
                pass
        return {
            "provider_task_id": task_id,
            "local_path": str(download.path),
            "filename": download.path.name,
            "format": model_format,
            "bytes": download.bytes_written,
            "sha256": download.sha256,
            "preview_file": preview_name,
            "resource_files": resources,
            "credits_consumed": completed.get("credits_consumed"),
        }

    @staticmethod
    def _process_payload(operation: str, params: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "retopology": {"model", "face_limit", "quad", "part_names", "bake"},
            "texture": {
                "model", "texture_quality", "pbr", "texture_prompt", "texture_seed",
                "texture_alignment", "part_names", "compress", "bake",
            },
            "segment": {"model", "segmentation_granularity", "split_by_connectivity"},
            "convert": {
                "format", "quad", "force_symmetry", "face_limit", "texture_size",
                "texture_format", "pivot_to_center_bottom", "fbx_preset",
            },
            "rig_check": set(),
            "rig": {"model", "rig_type", "spec", "out_format"},
            "animate": {
                "animation", "animations", "out_format", "bake_animation",
                "export_with_geometry", "animate_in_place",
            },
        }[operation]
        unknown = set(params) - allowed
        if unknown:
            raise ValidationError(f"unsupported Tripo {operation} options: {', '.join(sorted(unknown))}")
        payload = dict(params)
        defaults = {
            "retopology": {"model": "v2.0", "face_limit": 5000, "quad": False, "bake": True},
            "texture": {
                "model": "v3.0-20250812", "texture_quality": "detailed", "pbr": True,
                "texture_alignment": "original_image", "bake": True,
            },
            "segment": {
                "model": "v2.0-20260430", "segmentation_granularity": "balanced",
                "split_by_connectivity": True,
            },
            "convert": {"format": "FBX", "pivot_to_center_bottom": False},
            "rig_check": {},
            "rig": {
                "model": "v1.0-20240301", "rig_type": "biped", "spec": "tripo",
                "out_format": "glb",
            },
            "animate": {
                "animation": "preset:idle", "out_format": "glb",
                "bake_animation": True, "export_with_geometry": True,
                "animate_in_place": False,
            },
        }[operation]
        result = {**defaults, **payload}
        if operation == "animate" and "animations" in payload:
            result.pop("animation", None)
        if operation == "retopology" and not 1000 <= int(result["face_limit"]) <= 20_000:
            raise ValidationError("Tripo retopology face_limit must be between 1000 and 20000")
        if operation == "convert" and str(result["format"]).upper() not in {"GLTF", "USDZ", "FBX", "OBJ", "STL", "3MF"}:
            raise ValidationError("Tripo conversion format is unsupported")
        if operation == "texture":
            prompt = result.get("texture_prompt")
            if prompt is not None:
                if not isinstance(prompt, dict):
                    raise ValidationError("Tripo texture_prompt must be an object")
                modes = [key for key in ("text", "image", "images") if key in prompt]
                if len(modes) != 1:
                    raise ValidationError("Tripo texture_prompt requires exactly one of text, image, or images")
                if "style_image" in prompt and modes != ["text"]:
                    raise ValidationError("Tripo style_image is valid only with texture_prompt.text")
                if set(prompt) - {"text", "image", "images", "style_image"}:
                    raise ValidationError("Tripo texture_prompt contains unsupported fields")
                if modes == ["images"] and (
                    not isinstance(prompt["images"], list) or len(prompt["images"]) != 4
                ):
                    raise ValidationError("Tripo texture_prompt.images requires exactly four views")
            if result["texture_alignment"] not in {"original_image", "geometry"}:
                raise ValidationError("unsupported Tripo texture alignment")
            if result["texture_quality"] not in {"standard", "detailed", "extreme"}:
                raise ValidationError("unsupported Tripo texture quality")
            if "compress" in result and result["compress"] != "geometry":
                raise ValidationError("unsupported Tripo texture compression")
            if "part_names" in result and (
                not isinstance(result["part_names"], list)
                or not all(isinstance(item, str) and item for item in result["part_names"])
            ):
                raise ValidationError("Tripo part_names must be non-empty strings")
        if operation == "rig":
            if result["model"] not in {"v1.0-20240301", "v2.5-20260210"}:
                raise ValidationError("unsupported Tripo rig model")
            if result["rig_type"] not in {
                "biped", "quadruped", "hexapod", "octopod", "avian", "serpentine", "aquatic",
            }:
                raise ValidationError("unsupported Tripo rig type")
            if result["model"] == "v1.0-20240301" and result["rig_type"] != "biped":
                raise ValidationError("Tripo rig v1.0 supports biped characters only")
            if result["model"] == "v2.5-20260210" and result["rig_type"] == "biped":
                raise ValidationError("Tripo rig v2.5 is for non-humanoid rig types")
            if result["spec"] not in {"tripo", "mixamo"} or result["out_format"] not in {"glb", "fbx"}:
                raise ValidationError("unsupported Tripo rig output options")
        if operation == "animate":
            has_single = isinstance(result.get("animation"), str) and bool(result["animation"])
            has_many = isinstance(result.get("animations"), list) and bool(result["animations"])
            if has_single == has_many:
                raise ValidationError("provide exactly one of animation or animations")
            if has_many and (
                len(result["animations"]) > 20
                or not all(isinstance(item, str) and item.startswith("preset:") for item in result["animations"])
            ):
                raise ValidationError("animations must contain 1-20 Tripo preset ids")
            if has_single and not result["animation"].startswith("preset:"):
                raise ValidationError("animation must be a Tripo preset id")
            if result["out_format"] not in {"glb", "fbx"}:
                raise ValidationError("Tripo animation output must be glb or fbx")
        return result

    @staticmethod
    def _is_task_id(value: object) -> bool:
        # V3 task ids are opaque: examples use task_*, while production may use UUIDs.
        return isinstance(value, str) and bool(re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value
        ))

    @classmethod
    def _task_id(cls, response: dict[str, Any]) -> str:
        code = response.get("code")
        if code is None:
            raise ProviderProtocolError(
                "Tripo response omitted its result code",
                diagnostics={"response_fields": sorted(str(key)[:64] for key in response)[:24]},
            )
        if code != 0:
            diagnostics = {"provider_code": str(code)[:64]}
            request_id = response.get("request_id")
            if isinstance(request_id, (str, int)):
                diagnostics["request_id"] = str(request_id)[:128]
            raise ProviderTaskError("Tripo rejected the generation request", diagnostics=diagnostics)
        data = response.get("data")
        task_id = data.get("task_id") if isinstance(data, dict) else None
        # Task ids are opaque. V3 examples commonly use a task_ prefix, while
        # production accounts may receive UUID-shaped ids. Validate only the
        # safe transport boundary promised by this adapter, not an example prefix.
        if not cls._is_task_id(task_id):
            fields = sorted(str(key)[:64] for key in response)[:24]
            raise ProviderProtocolError(
                "Tripo response did not contain a valid task id",
                diagnostics={"response_fields": fields},
            )
        return task_id

    @classmethod
    def _account_profile(cls, spec: AssetSpec) -> str | None:
        options = spec.advanced.get(cls.option_namespace, {})
        value = options.get("account_profile") if isinstance(options, dict) else None
        return str(value) if value else None

    def _wait_for_tasks(self, task_ids, token, cancel_event, progress):
        deadline = time.monotonic() + self._timeout
        latest: dict[str, dict[str, Any]] = {}
        pending = set(task_ids)
        while pending:
            self._check_cancel(cancel_event)
            if time.monotonic() >= deadline:
                raise ProviderTimeoutError("Tripo generation timed out")
            for task_id in list(pending):
                response = self._http.request_json("GET", f"{self.base_url}/tasks/{task_id}", token=token)
                code = response.get("code")
                if code is None:
                    raise ProviderProtocolError("Tripo task query omitted its result code")
                if code != 0:
                    diagnostics = {"provider_code": str(code)[:64]}
                    request_id = response.get("request_id")
                    if isinstance(request_id, (str, int)):
                        diagnostics["request_id"] = str(request_id)[:128]
                    raise ProviderTaskError(
                        "Tripo rejected the task query", diagnostics=diagnostics
                    )
                if not isinstance(response.get("data"), dict):
                    raise ProviderProtocolError("Tripo task query response was invalid")
                task = response["data"]
                latest[task_id] = task
                status = task.get("status")
                if status == "success":
                    pending.remove(task_id)
                elif status in {"failed", "cancelled", "banned", "expired", "unknown"}:
                    diagnostics = {"task_status": str(status)}
                    error_code = task.get("error_code")
                    if isinstance(error_code, (str, int)):
                        diagnostics["provider_code"] = str(error_code)[:64]
                    error = task.get("error")
                    if isinstance(error, dict) and isinstance(error.get("code"), (str, int)):
                        diagnostics["provider_code"] = str(error["code"])[:64]
                    raise ProviderTaskError(
                        f"Tripo task ended with status {status}", diagnostics=diagnostics
                    )
                elif status not in {"queued", "running"}:
                    raise ProviderProtocolError("Tripo returned an unknown task status")
            if progress:
                values = [float(latest.get(item, {}).get("progress", 0)) / 100.0 for item in task_ids]
                progress(min(sum(values) / len(values), 0.99))
            if pending:
                self._sleep(self._poll_interval)
        return [latest[item] for item in task_ids]

    @staticmethod
    def _check_cancel(cancel_event: Event | None) -> None:
        if cancel_event and cancel_event.is_set():
            raise JobCancelledError("job was cancelled locally")
