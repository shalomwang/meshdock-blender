from __future__ import annotations

import base64
import re
import time
from pathlib import Path
from threading import Event
from typing import Any

from ..core.errors import (
    JobCancelledError,
    ProviderProtocolError,
    ProviderTaskError,
    ProviderTimeoutError,
    ValidationError,
)
from ..core.models import AssetJob, AssetSpec, Candidate, InputMode
from .base import InputConstraint, ProviderAdapter, ProviderCapabilities, ProviderStatus
from .http import HttpTransport, SecureHttpClient
from .artifacts import download_declared_resources, download_model, normalize_format
from .tripo import CredentialReader


class TokenHubAdapter(ProviderAdapter):
    """Tencent TokenHub 3D gateway.

    Hunyuan and Tripo are model families behind this one supplier account.  Their
    task result shapes differ, so dispatch stays model-aware instead of pretending
    TokenHub is a regional Hunyuan endpoint.
    """

    id = "tokenhub_cn"
    option_namespace = "tokenhub"
    submit_url = "https://tokenhub.tencentmaas.com/v1/api/3d/submit"
    query_url = "https://tokenhub.tencentmaas.com/v1/api/3d/query"
    default_model = "hy-3d-3.1"
    _ALLOWED_ADVANCED = {
        "model", "enable_pbr", "face_count", "generate_type", "polygon_type", "result_format",
        "account_profile",
    }
    _HUNYUAN_MODELS = {"hy-3d-3.0", "hy-3d-3.1"}
    _TRIPO_MODELS = {"tripo-3d-3.1", "tripo-3d-p1"}
    _VIEWS_30 = ("front", "left", "right", "back")
    _VIEWS_31 = (
        "front", "left", "right", "back", "top", "bottom", "left_front", "right_front",
    )

    @classmethod
    def _input_constraints_for_model(cls, model: str) -> dict[str, InputConstraint]:
        multiview_views = cls._VIEWS_31 if model == "hy-3d-3.1" else cls._VIEWS_30
        return {
            "text": InputConstraint(mode="text"),
            "image": InputConstraint(
                mode="image", min_images=1, max_images=1,
                required_views=("front",), allowed_views=("front",),
                formats=("png", "jpg", "webp"), min_dimension=128, max_dimension=5000,
                max_file_bytes=6_000_000, max_total_bytes=6_000_000,
                note="One front image; raw file up to 6 MB.",
            ),
            "multiview": InputConstraint(
                mode="multiview", min_images=2, max_images=len(multiview_views),
                required_views=("front",), allowed_views=multiview_views,
                formats=("png", "jpg"), min_dimension=129, max_dimension=4999,
                max_file_bytes=6_000_000,
                max_total_bytes=6_000_000 * len(multiview_views),
                note=(
                    "HY-3D-3.1 accepts up to 8 views."
                    if model == "hy-3d-3.1" else
                    "HY-3D-3.0 accepts front plus left, right, and back."
                ),
            ),
        }

    def resolve_input_constraints(
        self, advanced: dict[str, Any] | None = None
    ) -> dict[str, InputConstraint]:
        raw_options = (advanced or {}).get(self.option_namespace, {})
        if not isinstance(raw_options, dict):
            raise ValidationError("advanced.tokenhub must be an object")
        unknown = set(raw_options) - self._ALLOWED_ADVANCED
        if unknown:
            raise ValidationError(f"unsupported Hunyuan options: {', '.join(sorted(unknown))}")
        model = str(raw_options.get("model", self.default_model))
        if model in self._TRIPO_MODELS:
            # Tencent's current public Tripo guide documents the production text
            # request contract but not an image request body.  Do not invent fields
            # or spend credits on an undocumented payload.
            return {"text": InputConstraint(mode="text")}
        if model not in self._HUNYUAN_MODELS:
            raise ValidationError("unsupported TokenHub 3D model")
        return self._input_constraints_for_model(model)

    def __init__(
        self,
        credentials: CredentialReader,
        http: HttpTransport | None = None,
        *,
        poll_interval: float = 2.0,
        timeout: float = 20 * 60,
        sleep=time.sleep,
        provider_id: str = "tokenhub_cn",
        base_url: str = "https://tokenhub.tencentmaas.com/v1/api/3d",
    ) -> None:
        if provider_id not in {"tokenhub_cn", "tokenhub_global"}:
            raise ValueError("unsupported TokenHub provider id")
        if base_url not in {
            "https://tokenhub.tencentmaas.com/v1/api/3d",
            "https://tokenhub-intl.tencentcloudmaas.com/v1/api/3d",
        }:
            raise ValueError("unsupported Hunyuan base URL")
        self.id = provider_id
        self.submit_url = f"{base_url}/submit"
        self.query_url = f"{base_url}/query"
        self._credentials = credentials
        self._http = http or SecureHttpClient()
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._sleep = sleep

    def status(self) -> ProviderStatus:
        configured = self._credentials.configured(self.id)
        return ProviderStatus(provider=self.id, configured=configured, available=configured)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.id,
            generation_modes=("text", "image", "multiview"),
            output_formats=("glb", "obj", "fbx", "stl", "usdz"),
            postprocess=("retopology", "uv", "texture", "segment", "convert", "rig", "animate"),
            input_constraints=self._input_constraints_for_model(self.default_model),
            process_schema={
                "retopology": {"defaults": {"polygon_type": "triangle", "face_level": "medium"}},
                "uv": {"defaults": {}},
                "texture": {"defaults": {"enable_pbr": True, "enable_keep_uv": False, "texture_size": 2048}},
                "segment": {"defaults": {"enable_staged_generation": False, "enable_post_process": False}, "source_formats": ["fbx"]},
                "convert": {"defaults": {"format": "FBX"}},
                "rig": {"defaults": {}, "source_formats": ["glb", "fbx"]},
                "animate": {"defaults": {"duration": 5, "enable_mesh": True, "enable_rewrite": True}},
            },
            advanced_schema={
                self.option_namespace: {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "model": {"enum": [
                            "hy-3d-3.0", "hy-3d-3.1", "tripo-3d-3.1", "tripo-3d-p1"
                        ]},
                        "enable_pbr": {"type": "boolean"},
                        "face_count": {
                            "type": "integer", "minimum": 3000, "maximum": 1500000,
                            "modeRanges": {
                                "Normal": [10000, 1500000],
                                "Geometry": [3000, 1500000],
                                "Sketch": [3000, 1500000],
                            },
                            "unsupportedModes": ["LowPoly"],
                        },
                        "generate_type": {"enum": ["Normal", "LowPoly", "Geometry", "Sketch"]},
                        "polygon_type": {"enum": ["triangle", "quadrilateral"]},
                        "result_format": {"enum": ["STL", "USDZ", "FBX"]},
                    },
                }
            },
        )

    def validate_advanced(self, spec: AssetSpec) -> None:
        super().validate_advanced(spec)
        if len(spec.prompt) > 1024:
            raise ValidationError("Hunyuan prompt exceeds 1024 characters")
        options = spec.advanced.get(self.option_namespace, {})
        if not isinstance(options, dict):
            raise ValidationError("advanced.tokenhub must be an object")
        unknown = set(options) - self._ALLOWED_ADVANCED
        if unknown:
            raise ValidationError(f"unsupported TokenHub options: {', '.join(sorted(unknown))}")
        model = options.get("model", self.default_model)
        if model not in self._HUNYUAN_MODELS | self._TRIPO_MODELS:
            raise ValidationError("unsupported TokenHub 3D model")
        if model in self._TRIPO_MODELS:
            if spec.input_mode != InputMode.TEXT:
                raise ValidationError(
                    "TokenHub Tripo image input is hidden until Tencent documents its request schema"
                )
            undocumented = set(options) - {"model", "account_profile"}
            if undocumented:
                raise ValidationError(
                    "TokenHub Tripo currently accepts only model and prompt: "
                    + ", ".join(sorted(undocumented))
                )
            return
        generation = options.get("generate_type", "Normal")
        if generation not in {"Normal", "LowPoly", "Geometry", "Sketch"}:
            raise ValidationError("unsupported Hunyuan generate_type")
        if model == "hy-3d-3.1" and generation in {"LowPoly", "Sketch"}:
            raise ValidationError("Hunyuan 3.1 does not support LowPoly or Sketch generation")
        face_count = options.get("face_count")
        if generation == "LowPoly" and face_count is not None:
            raise ValidationError(
                "Hunyuan LowPoly controls topology automatically and does not accept face_count"
            )
        if face_count is not None:
            minimum = 10_000 if generation == "Normal" else 3_000
            if not minimum <= int(face_count) <= 1_500_000:
                raise ValidationError(
                    f"Hunyuan {generation} face_count must be between {minimum} and 1500000"
                )
        profile_id = options.get("account_profile")
        if profile_id is not None and not re.fullmatch(r"[a-f0-9]{32}", str(profile_id)):
            raise ValidationError("Hunyuan credential selection is invalid")

    def estimate_generation_credits(self, spec: AssetSpec) -> float | None:
        options = spec.advanced.get(self.option_namespace, {})
        if options.get("model", self.default_model) in self._TRIPO_MODELS:
            # TokenHub reports authoritative usage, but the public Tripo guide does
            # not currently publish a stable preflight credit table.
            return None
        generation = str(options.get("generate_type", "Normal"))
        base = {"Normal": 20.0, "LowPoly": 25.0, "Geometry": 15.0, "Sketch": 25.0}.get(generation, 20.0)
        if options.get("enable_pbr", True) and generation != "Geometry":
            base += 10.0
        if spec.input_mode == InputMode.MULTIVIEW:
            base += 10.0
        if "face_count" in options and generation != "LowPoly":
            base += 10.0
        if "result_format" in options:
            base += 5.0
        return base * spec.candidate_count

    def estimate_process_credits(self, operation: str, params: dict[str, Any]) -> float | None:
        return {
            "retopology": 50.0, "uv": 10.0, "texture": 30.0, "segment": 30.0,
            "convert": 5.0, "rig": 20.0, "animate": 20.0,
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
        model = str(job.spec.advanced.get(self.option_namespace, {}).get("model", self.default_model))
        tasks: list[str] = []
        for index in range(job.spec.candidate_count):
            self._check_cancel(cancel_event)
            response = self._http.request_json(
                "POST", self.submit_url, token=token, payload=self._generation_payload(job.spec, job)
            )
            task_id = response.get("id")
            if not isinstance(task_id, (str, int)) or not str(task_id):
                diagnostics = self._safe_response_diagnostics(response)
                if diagnostics.get("provider_code"):
                    raise ProviderTaskError(
                        "TokenHub rejected the generation request", diagnostics=diagnostics
                    )
                raise ProviderProtocolError(
                    "TokenHub response did not contain a task id", diagnostics=diagnostics
                )
            task_id = str(task_id)
            tasks.append(task_id)
            key = f"tokenhub_{index + 1}"
            if task_submitted:
                task_submitted(key, task_id)
            else:
                job.provider_task_ids[key] = task_id

        completed = self._wait_for_tasks(tasks, model, token, cancel_event, progress)
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
        model = str(job.spec.advanced.get(self.option_namespace, {}).get("model", self.default_model))
        tasks = [
            value for key, value in sorted(job.provider_task_ids.items())
            if key.startswith(("tokenhub_", "hunyuan_"))
        ]
        if not tasks:
            raise ProviderProtocolError("Hunyuan job has no task ids to resume")
        completed = self._wait_for_tasks(tasks, model, token, cancel_event, progress)
        return self._download_completed(job, destination, tasks, completed, progress)

    def _download_completed(self, job, destination, tasks, completed, progress):
        if callable(getattr(progress, "stage", None)):
            progress.stage("downloading")
        candidates: list[Candidate] = []
        model = str(job.spec.advanced.get(self.option_namespace, {}).get("model", self.default_model))
        if model in self._TRIPO_MODELS:
            for index, task in enumerate(completed, start=1):
                output = task.get("output")
                if not isinstance(output, dict) or not isinstance(output.get("model_url"), str):
                    raise ProviderProtocolError("TokenHub Tripo task succeeded without a model URL")
                download, model_format = download_model(
                    self._http, output["model_url"],
                    destination / f"{job.spec.asset_name}_tokenhub_tripo_{index}",
                    declared_format="glb",
                )
                preview_name = None
                preview_url = output.get("rendered_image_url") or output.get("generated_image_url")
                if isinstance(preview_url, str):
                    preview_path = destination / f"{job.spec.asset_name}_tokenhub_tripo_{index}_preview.png"
                    try:
                        self._http.download(preview_url, preview_path, max_bytes=50_000_000)
                        preview_name = preview_path.name
                    except Exception:
                        preview_name = None
                candidates.append(Candidate(
                    id=f"tokenhub_tripo_{index}", provider=self.id,
                    label=f"TokenHub Tripo candidate {index}",
                    local_model_path=str(download.path), format=model_format,
                    metadata={
                        "provider_task_id": tasks[index - 1], "provider_model": model,
                        "bytes": download.bytes_written, "sha256": download.sha256,
                        "preview_file": preview_name,
                    },
                ))
            if progress:
                progress(1.0)
            return candidates
        requested = normalize_format(job.spec.advanced.get(self.option_namespace, {}).get("result_format")) or "glb"
        supported = {"glb", "gltf", "fbx", "obj", "stl", "usdz"}
        for index, task in enumerate(completed, start=1):
            outputs = task.get("data")
            if not isinstance(outputs, list):
                raise ProviderProtocolError("Hunyuan task succeeded without output files")
            usable = [
                item for item in outputs
                if isinstance(item, dict)
                and isinstance(item.get("url"), str)
                and normalize_format(str(item.get("type", ""))) in supported
            ]
            priority = [requested, "glb", "fbx", "obj", "gltf", "stl", "usdz"]
            selected = next(
                (
                    item for wanted in priority for item in usable
                    if normalize_format(str(item.get("type", ""))) == wanted
                ),
                None,
            )
            if selected is None:
                raise ProviderProtocolError("Hunyuan task did not return a supported model output")
            declared = normalize_format(str(selected.get("type", "")))
            download, model_format = download_model(
                self._http,
                selected["url"],
                destination / f"{job.spec.asset_name}_tokenhub_hunyuan_{index}",
                declared_format=declared,
            )
            resources = download_declared_resources(
                self._http,
                selected.get("resources", task.get("resources")),
                download.path.parent,
            )
            preview_name = None
            if isinstance(selected.get("preview_image_url"), str):
                preview_path = destination / f"{job.spec.asset_name}_tokenhub_hunyuan_{index}_preview.png"
                try:
                    self._http.download(selected["preview_image_url"], preview_path, max_bytes=50_000_000)
                    preview_name = preview_path.name
                except Exception:
                    preview_name = None
            candidates.append(
                Candidate(
                    id=f"tokenhub_hunyuan_{index}", provider=self.id,
                    label=f"TokenHub Hunyuan candidate {index}",
                    local_model_path=str(download.path), format=model_format,
                    metadata={
                        "provider_task_id": tasks[index - 1],
                        "provider_model": model,
                        "bytes": download.bytes_written,
                        "sha256": download.sha256,
                        "preview_file": preview_name,
                        "resource_files": resources,
                        "requested_format": requested,
                    },
                )
            )
        if progress:
            progress(1.0)
        return candidates

    def _generation_payload(self, spec: AssetSpec, job: AssetJob | None = None) -> dict[str, Any]:
        options = dict(spec.advanced.get(self.option_namespace, {}))
        options.pop("account_profile", None)
        model = str(options.get("model", self.default_model))
        if model in self._TRIPO_MODELS:
            return {"model": model, "prompt": spec.prompt}
        payload: dict[str, Any] = {
            "model": model, "enable_pbr": True, "generate_type": "Normal",
        }
        if spec.input_mode == InputMode.TEXT:
            payload["prompt"] = spec.prompt
        else:
            references = job.references if job else []
            front = next((item for item in references if item.view == "front"), None)
            if front is None:
                raise ValidationError("Hunyuan image generation requires a front reference")
            payload["image_base64"] = base64.b64encode(Path(front.local_path).read_bytes()).decode("ascii")
            if spec.input_mode == InputMode.MULTIVIEW:
                payload["multi_view_images"] = [
                    {
                        "view_type": item.view,
                        "view_image_base64": base64.b64encode(Path(item.local_path).read_bytes()).decode("ascii"),
                    }
                    for item in references if item.view != "front"
                ]
        generation = str(options.get("generate_type", "Normal"))
        for name, value in options.items():
            if name != "polygon_type" and not (
                name == "face_count" and generation == "LowPoly"
            ):
                payload[name] = value
        if payload["generate_type"] == "LowPoly" and "polygon_type" in options:
            payload["polygon_type"] = options["polygon_type"]
        if payload["generate_type"] == "Geometry":
            payload["enable_pbr"] = False
        return payload

    @staticmethod
    def _safe_response_diagnostics(response: dict[str, Any]) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {
            "response_fields": sorted(str(key)[:64] for key in response)[:24]
        }
        request_id = response.get("request_id") or response.get("RequestId")
        if isinstance(request_id, (str, int)):
            diagnostics["request_id"] = str(request_id)[:128]
        code = response.get("error_code") or response.get("ErrorCode") or response.get("code")
        if isinstance(code, (str, int)) and str(code) not in {"", "0", "200"}:
            diagnostics["provider_code"] = str(code)[:64]
        error = response.get("error") or response.get("Error")
        if isinstance(error, dict):
            nested_code = error.get("code") or error.get("Code")
            if isinstance(nested_code, (str, int)) and str(nested_code):
                diagnostics["provider_code"] = str(nested_code)[:64]
        return diagnostics


    def _wait_for_tasks(self, task_ids, model, token, cancel_event, progress):
        deadline = time.monotonic() + self._timeout
        latest: dict[str, dict[str, Any]] = {}
        pending = set(task_ids)
        while pending:
            self._check_cancel(cancel_event)
            if time.monotonic() >= deadline:
                raise ProviderTimeoutError("Hunyuan generation timed out")
            for task_id in list(pending):
                response = self._http.request_json(
                    "POST", self.query_url, token=token, payload={"model": model, "id": task_id}
                )
                diagnostics = self._safe_response_diagnostics(response)
                if diagnostics.get("provider_code"):
                    raise ProviderTaskError(
                        "TokenHub rejected the task query", diagnostics=diagnostics
                    )
                latest[task_id] = response
                status = str(response.get("status", "")).lower()
                success_status = "success" if model in self._TRIPO_MODELS else "completed"
                if status == success_status:
                    pending.remove(task_id)
                elif status in {
                    "failed", "cancelled", "fail", "banned", "expired", "unknown",
                }:
                    diagnostics = {"task_status": status}
                    request_id = response.get("request_id") or response.get("RequestId")
                    provider_code = response.get("error_code") or response.get("code")
                    if isinstance(request_id, (str, int)):
                        diagnostics["request_id"] = str(request_id)[:128]
                    if isinstance(provider_code, (str, int)):
                        diagnostics["provider_code"] = str(provider_code)[:64]
                    raise ProviderTaskError(
                        f"Hunyuan task ended with status {status}", diagnostics=diagnostics
                    )
                elif status not in {"queued", "in_progress", "running", "processing"}:
                    raise ProviderProtocolError("TokenHub returned an unknown task status")
            if progress:
                progress(min((len(task_ids) - len(pending)) / len(task_ids), 0.99))
            if pending:
                self._sleep(self._poll_interval)
        return [latest[item] for item in task_ids]

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
        models = {
            "retopology": "hy-3d-retopology",
            "uv": "hy-3d-uv",
            "texture": "hy-3d-texture",
            "segment": "hy-3d-component",
            "convert": "hy-3d-format",
            "rig": "hy-3d-rigging",
            "animate": "hy-3d-motion",
        }
        if operation not in models:
            raise ValidationError(f"Hunyuan does not support {operation}")
        if candidate.provider != self.id or not str(
            candidate.metadata.get("provider_model", "")
        ).startswith("hy-3d-"):
            raise ValidationError("TokenHub Hunyuan processing requires a TokenHub Hunyuan source")
        token = self._credentials.use(self.id, self._account_profile(job.spec))
        source_url, source_format = self._source_output(job, candidate, token)
        model = models[operation]
        payload = self._process_payload(operation, params, source_url, source_format, job)
        payload["model"] = model
        response = self._http.request_json("POST", self.submit_url, token=token, payload=payload)

        direct_url = response.get("result_file3d")
        task_id: str | None = None
        if isinstance(direct_url, str):
            completed = {"status": "completed", "data": [{"type": params.get("format", "glb"), "url": direct_url}]}
        else:
            raw_id = response.get("id")
            if not isinstance(raw_id, (str, int)) or not str(raw_id):
                diagnostics = self._safe_response_diagnostics(response)
                if diagnostics.get("provider_code"):
                    raise ProviderTaskError(
                        "TokenHub rejected the processing request", diagnostics=diagnostics
                    )
                raise ProviderProtocolError(
                    "TokenHub processing response did not contain a task id",
                    diagnostics=diagnostics,
                )
            task_id = str(raw_id)
            if task_submitted:
                task_submitted(task_id)
            completed = self._wait_for_tasks([task_id], model, token, cancel_event, progress)[0]

        return self._completed_process_result(operation, destination, task_id, model, completed)

    def validate_process(
        self, job: AssetJob, candidate: Candidate, operation: str, params: dict[str, Any]
    ) -> None:
        if candidate.provider != self.id or not str(
            candidate.metadata.get("provider_model", "")
        ).startswith("hy-3d-"):
            raise ValidationError("TokenHub Hunyuan processing requires a TokenHub Hunyuan source")
        if operation == "animate" and candidate.metadata.get("process_operation") != "rig":
            raise ValidationError("Hunyuan animation requires a Hunyuan rig result candidate")
        self._process_payload(
            operation, params, "https://validation.invalid/source", candidate.format, job
        )

    def resume_process(
        self, job: AssetJob, candidate: Candidate, operation: str, params: dict[str, Any],
        provider_task_id: str, destination: Path, *, cancel_event: Event | None = None,
        progress=None,
    ) -> dict[str, Any]:
        models = {
            "retopology": "hy-3d-retopology", "uv": "hy-3d-uv",
            "texture": "hy-3d-texture", "segment": "hy-3d-component",
            "convert": "hy-3d-format", "rig": "hy-3d-rigging",
            "animate": "hy-3d-motion",
        }
        model = models.get(operation)
        if model is None:
            raise ValidationError(f"Hunyuan does not support {operation}")
        token = self._credentials.use(self.id, self._account_profile(job.spec))
        completed = self._wait_for_tasks(
            [provider_task_id], model, token, cancel_event, progress
        )[0]
        return self._completed_process_result(
            operation, destination, provider_task_id, model, completed
        )

    def _completed_process_result(
        self, operation: str, destination: Path, task_id: str | None,
        model: str, completed: dict[str, Any],
    ) -> dict[str, Any]:
        outputs = completed.get("data")
        if not isinstance(outputs, list):
            raise ProviderProtocolError("Hunyuan processing task completed without output files")
        usable = [
            item for item in outputs
            if isinstance(item, dict) and isinstance(item.get("url"), str)
        ]
        if not usable:
            raise ProviderProtocolError("Hunyuan processing output contained no downloadable model")
        destination.mkdir(parents=True, exist_ok=True)
        downloaded: list[dict[str, Any]] = []
        primary = None
        for index, item in enumerate(usable, start=1):
            declared = normalize_format(str(item.get("type", "")))
            download, model_format = download_model(
                self._http, item["url"], destination / f"{operation}_{index}", declared_format=declared
            )
            entry = {
                "filename": download.path.name, "format": model_format,
                "bytes": download.bytes_written, "sha256": download.sha256,
            }
            downloaded.append(entry)
            if primary is None:
                primary = (download, model_format)
        assert primary is not None
        return {
            "provider_task_id": task_id,
            "provider_model": model,
            "local_path": str(primary[0].path),
            "filename": primary[0].path.name,
            "format": primary[1],
            "bytes": sum(item["bytes"] for item in downloaded),
            "sha256": primary[0].sha256,
            "files": downloaded,
        }

    def _source_output(self, job: AssetJob, candidate: Candidate, token: str) -> tuple[str, str]:
        task_id = candidate.metadata.get("provider_task_id")
        if not isinstance(task_id, str):
            raise ValidationError("Hunyuan source task id is unavailable")
        generation_model = str(
            candidate.metadata.get("provider_model")
            or job.spec.advanced.get(self.option_namespace, {}).get("model", self.default_model)
        )
        response = self._http.request_json(
            "POST", self.query_url, token=token, payload={"model": generation_model, "id": task_id}
        )
        outputs = response.get("data")
        if not isinstance(outputs, list):
            raise ProviderProtocolError("Hunyuan source task no longer exposes an output")
        selected = next(
            (
                item for item in outputs if isinstance(item, dict)
                and isinstance(item.get("url"), str)
                and normalize_format(str(item.get("type", ""))) == candidate.format
            ),
            next((item for item in outputs if isinstance(item, dict) and isinstance(item.get("url"), str)), None),
        )
        if selected is None:
            raise ProviderProtocolError("Hunyuan source output URL is unavailable")
        return selected["url"], normalize_format(str(selected.get("type", ""))) or candidate.format

    @staticmethod
    def _process_payload(
        operation: str, params: dict[str, Any], source_url: str, source_format: str, job: AssetJob
    ) -> dict[str, Any]:
        allowed = {
            "retopology": {"polygon_type", "face_level"},
            "uv": set(),
            "texture": {"prompt", "enable_pbr", "enable_keep_uv", "texture_size", "use_front_reference"},
            "segment": {"enable_staged_generation", "enable_post_process", "part_segmentation_info"},
            "convert": {"format"},
            "rig": {"motion_type"},
            "animate": {
                "prompt", "duration", "enable_mesh", "enable_rewrite",
                "enable_duration_est",
            },
        }[operation]
        unknown = set(params) - allowed
        if unknown:
            raise ValidationError(f"unsupported Hunyuan {operation} options: {', '.join(sorted(unknown))}")
        file3d = {"type": source_format.upper(), "url": source_url}
        if operation == "retopology":
            polygon_type = str(params.get("polygon_type", "triangle"))
            face_level = str(params.get("face_level", "medium"))
            if polygon_type not in {"triangle", "quadrilateral"}:
                raise ValidationError("Hunyuan retopology polygon_type is unsupported")
            if face_level not in {"low", "medium", "high"}:
                raise ValidationError("Hunyuan retopology face_level is unsupported")
            return {
                "file3d": file3d,
                "polygon_type": polygon_type,
                "face_level": face_level,
            }
        if operation == "uv":
            return {"file": file3d}
        if operation == "texture":
            result: dict[str, Any] = {
                "file3d": file3d,
                "prompt": str(params.get("prompt", job.spec.prompt or "enhance the existing material"))[:200],
                "enable_pbr": bool(params.get("enable_pbr", True)),
                "enable_keep_uv": bool(params.get("enable_keep_uv", False)),
                "texture_size": int(params.get("texture_size", 2048)),
            }
            if params.get("use_front_reference"):
                front = next((item for item in job.references if item.view == "front"), None)
                if front is None:
                    raise ValidationError("texture reference requested but no front reference is available")
                result.pop("prompt", None)
                result["image"] = {
                    "base64": base64.b64encode(Path(front.local_path).read_bytes()).decode("ascii")
                }
            return result
        if operation == "segment":
            if source_format.lower() != "fbx":
                raise ValidationError("Hunyuan component generation requires an FBX source")
            return {
                "file": file3d,
                "enable_staged_generation": bool(params.get("enable_staged_generation", False)),
                "enable_post_process": bool(params.get("enable_post_process", False)),
                **({"part_segmentation_info": params["part_segmentation_info"]} if "part_segmentation_info" in params else {}),
            }
        if operation == "rig":
            if source_format.lower() not in {"glb", "fbx"}:
                raise ValidationError("Hunyuan rigging requires a GLB or FBX source")
            motion_type = params.get("motion_type")
            if motion_type is not None and not 1 <= int(motion_type) <= 48:
                raise ValidationError("Hunyuan motion_type must be between 1 and 48")
            return {
                "file3d": file3d,
                **({"motion_type": int(motion_type)} if motion_type is not None else {}),
            }
        if operation == "animate":
            prompt = str(params.get("prompt", job.spec.prompt or "idle character animation")).strip()
            if not prompt or len(prompt) > 128:
                raise ValidationError("Hunyuan animation prompt must contain 1-128 characters")
            duration = int(params.get("duration", 5))
            if not 1 <= duration <= 12:
                raise ValidationError("Hunyuan animation duration must be between 1 and 12 seconds")
            return {
                "prompt": prompt,
                "retarget_file": file3d,
                "duration": duration,
                "enable_mesh": bool(params.get("enable_mesh", True)),
                "enable_rewrite": bool(params.get("enable_rewrite", True)),
                "enable_duration_est": bool(params.get("enable_duration_est", False)),
            }
        target = str(params.get("format", "FBX")).upper()
        if target not in {"STL", "USDZ", "FBX", "MP4", "GIF"}:
            raise ValidationError("Hunyuan conversion format is unsupported")
        return {"file3d": source_url, "format": target}

    @staticmethod
    def _check_cancel(cancel_event: Event | None) -> None:
        if cancel_event and cancel_event.is_set():
            raise JobCancelledError("job was cancelled locally")

    @classmethod
    def _account_profile(cls, spec: AssetSpec) -> str | None:
        options = spec.advanced.get(cls.option_namespace, {})
        value = options.get("account_profile") if isinstance(options, dict) else None
        return str(value) if value else None


# 0.7.x source compatibility.  This class was always a TokenHub transport.
HunyuanAdapter = TokenHubAdapter
